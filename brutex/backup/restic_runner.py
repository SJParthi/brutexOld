"""The ONE restic subprocess seam — fixed argv, injectable runner (DEC-BACKUP-OFFBOX-001).

Every restic invocation in :mod:`brutex.backup` flows through
:class:`ResticRunner` — fixed-argv builders (never a shell), an injectable
``run`` callable (the :mod:`brutex.observability.power_keepawake` seam idiom),
and child-env hygiene by construction (F19):

* the repo password NEVER exists as a value this code handles — only
  ``RESTIC_PASSWORD_FILE`` (a 0600 path) or ``RESTIC_PASSWORD_COMMAND`` is
  exported to the CHILD env;
* the child env is built ALLOWLIST-style from the base env (``HOME`` /
  ``PATH`` / ``TMPDIR`` + ``AWS_*`` / ``B2_*`` passthrough), so an inherited
  ``RESTIC_PASSWORD`` is STRIPPED by construction;
* argv contains no secret — safe to log verbatim;
* ``stdin=subprocess.DEVNULL`` on every call (restic must NEVER hang on an
  interactive prompt).

A hang is bounded by the subprocess ``timeout`` (F17a — the ``run_with_deadline``
freeze-guard philosophy); a missing binary maps to
``ResticInvocation.binary_missing`` (F3). Neither raises out of the seam — the
orchestrators map every outcome to a deterministic exit code.
"""

from __future__ import annotations

import io
import json
import subprocess  # nosec B404 — fixed argv, no shell; restic/fake-restic only (power_keepawake.py idiom)
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import structlog

from brutex.backup import backup_rust
from brutex.backup.errors import BackupError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from types import ModuleType

    from brutex.config.backup_env import BackupEnvConfig

_LOG = structlog.get_logger(__name__)

#: Injectable subprocess runner — the default wraps :func:`subprocess.run`.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]

#: The tag every scheduled backup snapshot carries (forget/deep-check universe).
BACKUP_TAG: Final[str] = "brutex"

#: The tag the restore drill's tiny known-bytes canary snapshot carries —
#: OUTSIDE the ``--tag brutex`` forget/deep-check universe by construction.
CANARY_TAG: Final[str] = "brutex-drill-canary"

#: Fixed ``--host``: keeps ``forget`` grouping stable across machine renames.
BACKUP_HOST: Final[str] = "brutex"

#: restic ``backup`` exit code 3 = snapshot CREATED, some source files
#: unreadable (restic docs "Exit codes"; probed against the installed binary
#: at install — ADR-0017 External-tool facts). Handled by the CALLER (F8).
BACKUP_EXIT_PARTIAL: Final[int] = 3

#: Tail lengths captured from the child's streams (bounded — never a full log).
_STDOUT_TAIL_CHARS: Final[int] = 2000
_STDERR_TAIL_CHARS: Final[int] = 500

#: Child-env keys copied verbatim from the base env when present.
_PASSTHROUGH_EXACT: Final[tuple[str, ...]] = ("HOME", "PATH", "TMPDIR")

#: Child-env key PREFIXES passed through (S3/B2 credentials ride
#: ``~/.brutex/.env`` → ``bootstrap_env()`` → the base env).
_PASSTHROUGH_PREFIXES: Final[tuple[str, ...]] = ("AWS_", "B2_")

#: ``brutex_ops`` decision-code vocabulary (DEC-BACKUP-RUST-OPS-001): 0 is
#: always the ORACLE code (abstain / raise-shaped — the facade serves the
#: Python oracle verbatim); the value codes are per-law.
_CODE_ORACLE: Final[int] = 0
_CODE_SNAPSHOTS_EMPTY: Final[int] = 1
_CODE_SNAPSHOTS_OK: Final[int] = 2
_CODE_SUMMARY_FOUND: Final[int] = 1
_CODE_LS_TIME_STRING: Final[int] = 3


@dataclass(frozen=True, slots=True)
class ResticInvocation:
    """The outcome of ONE restic invocation (never raises past the seam).

    Attributes
    ----------
    op
        The verb token (``"cat-config"`` | ``"init"`` | ``"unlock"`` |
        ``"backup"`` | ``"forget"`` | ``"forget-snapshot"`` | ``"check"`` |
        ``"snapshots"`` | ``"ls"`` | ``"restore"``).
    argv
        The exact argv tuple — NEVER contains a secret, safe to log verbatim.
    ok
        ``returncode == 0`` (rc 3 on ``backup`` is handled by the CALLER, F8).
    returncode
        The child's exit code; ``None`` when ``binary_missing`` / ``timed_out``.
    stdout_tail
        Last :data:`_STDOUT_TAIL_CHARS` chars of stdout (for logs).
    stderr_tail
        Last :data:`_STDERR_TAIL_CHARS` chars of stderr (for logs).
    stdout_full
        The COMPLETE stdout — the parse input for ``--json`` verbs (``ls`` of
        a real lake emits far more than any log tail; never truncated here).
        HONEST envelope: this holds the whole child stdout in memory
        (O(child stdout) — hundreds of MB for ``ls`` of a full-universe
        lake); the parse helpers iterate it LAZILY (no per-node dict list),
        but true subprocess streaming would be a runner-seam redesign.
    timed_out
        The subprocess deadline expired (F17a).
    binary_missing
        The restic binary was not found OR could not be executed (a
        not-executable install / ENOEXEC / fork failure — F3): every
        exec-time failure routes to the same restic_missing/M1 triple.
    """

    op: str
    argv: tuple[str, ...]
    ok: bool
    returncode: int | None
    stdout_tail: str
    stderr_tail: str
    stdout_full: str
    timed_out: bool
    binary_missing: bool


def _full(raw: object) -> str:
    """Return the WHOLE subprocess stream as text (bytes/str/None safe)."""
    if raw is None:
        return ""
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)


def _tail(raw: object, limit: int) -> str:
    """Return the last *limit* chars of a subprocess stream (bytes/str/None safe)."""
    return _full(raw)[-limit:]


def _default_run(
    argv: Sequence[str], *, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """The default runner — one bounded, shell-free, stdin-closed ``subprocess.run``."""
    # S603/B603: argv is a fixed list built from config constants + validated
    # paths (never user-concatenated shell text), shell=False, stdin closed.
    return subprocess.run(  # noqa: S603  # nosec B603 — fixed argv, no shell, bounded timeout
        list(argv),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _build_backup_argv_python(
    binary: str,
    paths: Sequence[Path],
    *,
    excludes: Sequence[str],
    tags: Sequence[str],
) -> tuple[str, ...]:
    """Build the exact ``restic backup`` argv (pure — shared with the dry-run log)."""
    return (
        binary,
        "backup",
        *(str(p) for p in paths),
        *chain.from_iterable(("--exclude", e) for e in excludes),
        "--exclude-caches",
        *chain.from_iterable(("--tag", t) for t in tags),
        "--host",
        BACKUP_HOST,
        "--json",
    )


def _str_tuple_thunk(result: object) -> Callable[[], tuple[str, ...]]:
    """Refuse-poison thunk: a non-``list[str]`` crate result is refused."""

    def _materialise() -> tuple[str, ...]:
        if not isinstance(result, list) or not all(isinstance(a, str) for a in result):
            raise TypeError(f"crate result is not a list of str: {result!r}")
        return tuple(result)

    return _materialise


def build_backup_argv(
    binary: str,
    paths: Sequence[Path],
    *,
    excludes: Sequence[str],
    tags: Sequence[str],
) -> tuple[str, ...]:
    """Build the exact ``restic backup`` argv (pure — shared with the dry-run log).

    Under the default-OFF ``BRUTEX_BACKUP_RUST`` flag the build routes
    through the ``brutex_ops`` twin (DEC-BACKUP-RUST-OPS-001); ``Path``
    rendering (``str(p)``) stays Python-side — only strings cross the FFI.
    """

    def _oracle() -> tuple[str, ...]:
        return _build_backup_argv_python(binary, paths, excludes=excludes, tags=tags)

    def _compute(crate: ModuleType) -> object:
        return _str_tuple_thunk(
            crate.build_backup_argv(binary, [str(p) for p in paths], list(excludes), list(tags))
        )

    return backup_rust.route_law("build_backup_argv", oracle=_oracle, compute=_compute)


class ResticRunner:
    """Builds child env + argv and executes restic verbs through ONE seam.

    Parameters
    ----------
    binary
        The restic binary name/path (``BRUTEX_BACKUP_RESTIC_BIN``).
    repo
        The restic repo URL — exported as ``RESTIC_REPOSITORY`` (never argv).
    password_file, password_command
        EXACTLY ONE must be non-``None`` — exported as ``RESTIC_PASSWORD_FILE``
        XOR ``RESTIC_PASSWORD_COMMAND`` (the value of the password itself is
        never read by this code — F19).
    timeout_s
        Wall-clock deadline per invocation (F17a).
    run
        Injectable runner (tests inject a recorder / a fake-restic wrapper);
        ``None`` → :func:`_default_run`.
    base_env
        Base environment for the allowlist child-env build; ``None`` →
        :data:`os.environ`.

    Raises
    ------
    BackupError
        When password sources ≠ exactly one (construction refuses — the
        orchestrators validate earlier, this is the belt).
    """

    __slots__ = ("_binary", "_env", "_run", "_timeout_s")

    def __init__(
        self,
        *,
        binary: str,
        repo: str,
        password_file: Path | None,
        password_command: str | None,
        timeout_s: float,
        run: Runner | None = None,
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        if (password_file is None) == (password_command is None):
            raise BackupError(
                "exactly one of password_file / password_command must be set "
                "(F6 — ambiguous or missing password source)"
            )
        if base_env is None:
            import os

            base_env = os.environ
        child: dict[str, str] = {}
        for key in _PASSTHROUGH_EXACT:
            if key in base_env:
                child[key] = base_env[key]
        for key, value in base_env.items():
            if key.startswith(_PASSTHROUGH_PREFIXES):
                child[key] = value
        # Allowlist build: an inherited RESTIC_PASSWORD is stripped by
        # construction (F19) — it is simply never copied.
        child["RESTIC_REPOSITORY"] = repo
        if password_file is not None:
            child["RESTIC_PASSWORD_FILE"] = str(password_file)
        else:
            child["RESTIC_PASSWORD_COMMAND"] = str(password_command)
        self._binary = binary
        self._env = child
        self._run = run if run is not None else _default_run
        self._timeout_s = timeout_s

    def _invoke(self, op: str, argv: tuple[str, ...]) -> ResticInvocation:
        """Execute *argv*; map missing-binary / timeout to flags, never raise."""
        _LOG.info("backup.restic.invoke", op=op, argv=argv)
        try:
            completed = self._run(argv, env=self._env, timeout=self._timeout_s)
        except FileNotFoundError:
            _LOG.error("backup.restic.missing", op=op, binary=self._binary)
            return ResticInvocation(
                op=op,
                argv=argv,
                ok=False,
                returncode=None,
                stdout_tail="",
                stderr_tail="",
                stdout_full="",
                timed_out=False,
                binary_missing=True,
            )
        except subprocess.TimeoutExpired as exc:
            _LOG.error("backup.restic.timeout", op=op, timeout_s=self._timeout_s)
            return ResticInvocation(
                op=op,
                argv=argv,
                ok=False,
                returncode=None,
                stdout_tail=_tail(exc.stdout, _STDOUT_TAIL_CHARS),
                stderr_tail=_tail(exc.stderr, _STDERR_TAIL_CHARS),
                stdout_full=_full(exc.stdout),
                timed_out=True,
                binary_missing=False,
            )
        except OSError as exc:
            # Exec-time failure OTHER than not-found (PermissionError on a
            # non-executable binary, ENOEXEC, ETXTBSY, fork ENOMEM) — mapped
            # INSIDE the seam like FileNotFoundError, never raised past it:
            # an escaped exec OSError left running_since STAMPED (dead-man
            # read RUNNING, healthcheck pinged success) and was mis-labelled
            # a state_write failure by run_backup's lock-scope handler.
            _LOG.error("backup.restic.exec_failed", op=op, binary=self._binary, error=str(exc))
            return ResticInvocation(
                op=op,
                argv=argv,
                ok=False,
                returncode=None,
                stdout_tail="",
                stderr_tail=str(exc)[-_STDERR_TAIL_CHARS:],
                stdout_full="",
                timed_out=False,
                binary_missing=True,
            )
        return ResticInvocation(
            op=op,
            argv=argv,
            ok=completed.returncode == 0,
            returncode=completed.returncode,
            stdout_tail=_tail(completed.stdout, _STDOUT_TAIL_CHARS),
            stderr_tail=_tail(completed.stderr, _STDERR_TAIL_CHARS),
            stdout_full=_full(completed.stdout),
            timed_out=False,
            binary_missing=False,
        )

    def cat_config(self) -> ResticInvocation:
        """Probe repo reachability + key (``restic cat config``; F5/F7)."""
        return self._invoke("cat-config", (self._binary, "cat", "config"))

    def init(self) -> ResticInvocation:
        """Initialise a fresh repo (``restic init``; idempotent first-run path F5)."""
        return self._invoke("init", (self._binary, "init"))

    def unlock(self) -> ResticInvocation:
        """Remove STALE repo locks of dead processes (``restic unlock``; best-effort)."""
        return self._invoke("unlock", (self._binary, "unlock"))

    def backup(
        self, paths: Sequence[Path], *, excludes: Sequence[str], tags: Sequence[str]
    ) -> ResticInvocation:
        """Snapshot *paths* with the plan's excludes + tags (``--json`` summary)."""
        return self._invoke(
            "backup", build_backup_argv(self._binary, paths, excludes=excludes, tags=tags)
        )

    def forget(
        self, *, keep_daily: int, keep_weekly: int, keep_monthly: int, prune: bool
    ) -> ResticInvocation:
        """Apply the retention policy to the ``--tag brutex`` universe (F23).

        ``--group-by host`` (NOT ``host,tags``): the nightly tag SET varies
        with the F16 pull-active/pull-quiet probe, so grouping by tags would
        split retention into TWO independent groups — each keeping its own
        14/8/12 (~2× the documented policy) and retaining a dormant tag
        group's last snapshots FOREVER (restic keep-N has no age cutoff).
        One host group = the documented global policy. The drill's canary
        never enters this universe (it lacks the ``brutex`` tag).
        """
        argv: tuple[str, ...] = (
            self._binary,
            "forget",
            "--keep-daily",
            str(keep_daily),
            "--keep-weekly",
            str(keep_weekly),
            "--keep-monthly",
            str(keep_monthly),
            "--tag",
            BACKUP_TAG,
            "--group-by",
            "host",
        )
        if prune:
            argv = (*argv, "--prune")
        return self._invoke("forget", argv)

    def forget_snapshot(self, snapshot_id: str) -> ResticInvocation:
        """Forget ONE snapshot by id (the drill's canary cleanup; best-effort)."""
        return self._invoke("forget-snapshot", (self._binary, "forget", snapshot_id))

    def check(self, *, read_data_subset: str | None = None) -> ResticInvocation:
        """Verify repo structure; optionally read one data subset (§6 step 8)."""
        argv: tuple[str, ...] = (self._binary, "check")
        if read_data_subset is not None:
            argv = (*argv, f"--read-data-subset={read_data_subset}")
        return self._invoke("check", argv)

    def snapshots_latest_json(self, *, tag: str = BACKUP_TAG) -> ResticInvocation:
        """Fetch the latest snapshot's metadata (the F21 deep cross-check).

        ``--no-lock`` is LOAD-BEARING (round-6 MAJOR): this read-only metadata
        call runs from the hourly dead-man checker, and the deep-due tick
        naturally lands in/near the nightly maintenance window (step 9 stamps
        success → shallow FRESH → deep gate open, while step 10 still holds
        the repo's EXCLUSIVE lock through ``forget``/Sunday ``forget --prune``
        — hours on an 8 TB repo; the drill's canary forget is a second such
        window). Without ``--no-lock`` the lock conflict read rc=1 → a false
        CRITICAL ``deep_check_unavailable`` page (the failed-call arm's own
        honest token since round 9; before that it was mislabelled
        ``state_remote_divergence``) + healthcheck ``/fail``
        pings EVERY subsequent tick until the prune ended (a failed call
        never advances ``last_deep_check_at``); conversely the call's own
        shared lock could fail the nightly's ``forget`` (false maintenance
        WARN). Verified on restic 0.16.4: ``snapshots --no-lock`` succeeds
        with correct JSON while an exclusive lock is held.
        """
        return self._invoke(
            "snapshots",
            (self._binary, "snapshots", "--no-lock", "--json", "--latest", "1", "--tag", tag),
        )

    def ls_json(self, snapshot_id: str) -> ResticInvocation:
        """List a snapshot's nodes as JSON lines (the drill's sample source)."""
        return self._invoke("ls", (self._binary, "ls", "--json", snapshot_id))

    def restore(
        self, snapshot_id: str, *, target: Path, includes: Sequence[str]
    ) -> ResticInvocation:
        """Restore *includes* of *snapshot_id* under *target* (the drill)."""
        return self._invoke(
            "restore",
            (
                self._binary,
                "restore",
                snapshot_id,
                "--target",
                str(target),
                *chain.from_iterable(("--include", i) for i in includes),
            ),
        )


def default_runner_factory(cfg: BackupEnvConfig) -> ResticRunner:
    """Build the production :class:`ResticRunner` from a resolved config.

    Raises
    ------
    BackupError
        When ``cfg.repo`` is unset or the password sources are not exactly one
        — callers (run / drill / the deep check) gate on config BEFORE calling,
        so this raise is the belt, not the primary refusal (F6).
    """
    if cfg.repo is None:
        raise BackupError("BRUTEX_BACKUP_REPO is not set — no restic repo to talk to")
    return ResticRunner(
        binary=cfg.restic_bin,
        repo=cfg.repo,
        password_file=cfg.password_file,
        password_command=cfg.password_command,
        timeout_s=cfg.timeout_s,
    )


def _json_lines(stdout: str) -> Iterator[dict[str, object]]:
    """Lazily parse restic ``--json`` line output, skipping non-JSON / non-object lines.

    A GENERATOR by design (never a materialised list): ``restic ls --json``
    of the full-universe lake emits one node object per lake file (10^5-10^6
    lines), and buffering every node as a Python dict multiplied the already
    O(child stdout) ``stdout_full`` RSS several-fold in the monthly drill.
    Callers keep only what they need (file-path strings / the first summary
    line); ``io.StringIO`` iterates lines without a ``splitlines()`` copy.
    """
    for line in io.StringIO(stdout):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def _parse_backup_summary_snapshot_id_python(stdout: str) -> str:
    """Extract the ``summary`` line's ``snapshot_id`` from ``backup --json`` output.

    Raises
    ------
    BackupError
        When no ``message_type == "summary"`` line with a snapshot id exists —
        a created-but-unidentifiable snapshot is a failure, fail loud.
    """
    for obj in _json_lines(stdout):
        if obj.get("message_type") == "summary":
            snapshot_id = obj.get("snapshot_id")
            if isinstance(snapshot_id, str) and snapshot_id:
                return snapshot_id
    raise BackupError("restic backup output carries no summary snapshot_id")


def parse_backup_summary_snapshot_id(stdout: str) -> str:
    """Extract the ``summary`` line's ``snapshot_id`` (see the ``_python`` oracle).

    DEC-BACKUP-RUST-OPS-001: under the default-OFF ``BRUTEX_BACKUP_RUST``
    flag the scan routes through the ``brutex_ops`` twin. Code 0 (abstain —
    a line serde cannot parse — OR the no-summary raise arm) serves the
    Python oracle VERBATIM, so the raised :class:`BackupError` message stays
    byte-identical by construction.
    """

    def _oracle() -> str:
        return _parse_backup_summary_snapshot_id_python(stdout)

    def _compute(crate: ModuleType) -> object:
        code, snapshot_id = cast(
            "tuple[object, object]", crate.parse_backup_summary_snapshot_id(stdout)
        )
        if code == _CODE_ORACLE:
            return backup_rust.USE_ORACLE
        if code != _CODE_SUMMARY_FOUND or not isinstance(snapshot_id, str) or not snapshot_id:
            raise TypeError(f"poisoned summary-id decision ({code!r}, {snapshot_id!r})")
        found = snapshot_id
        return lambda: found

    return backup_rust.route_law(
        "parse_backup_summary_snapshot_id", oracle=_oracle, compute=_compute
    )


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    """One snapshot's identity + creation time (the deep cross-check's remote truth).

    Attributes
    ----------
    snapshot_id
        The snapshot's (short or full) id.
    time_utc
        The snapshot's creation time, normalised to UTC.
    """

    snapshot_id: str
    time_utc: datetime


def _parse_snapshot_time(raw: object) -> datetime:
    """Parse a restic snapshot ``time`` string to an aware UTC datetime."""
    from datetime import UTC

    if not isinstance(raw, str):
        raise BackupError(f"restic snapshot time is not a string: {raw!r}")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError(f"restic snapshot time is unparseable: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise BackupError(f"restic snapshot time is naive: {raw!r}")
    return parsed.astimezone(UTC)


def _parse_snapshots_latest_python(stdout: str) -> SnapshotInfo | None:
    """Parse ``restic snapshots --json --latest 1`` output.

    Returns
    -------
    SnapshotInfo | None
        ``None`` for an empty snapshot list (an empty repo is a REAL answer —
        the deep check treats it as divergence).

    Raises
    ------
    BackupError
        On malformed JSON / a malformed entry — fail loud, never guess.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BackupError(f"restic snapshots output is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise BackupError(f"restic snapshots output is not a JSON array: {type(data).__name__}")
    if not data:
        return None
    last = data[-1]
    if not isinstance(last, dict):
        raise BackupError("restic snapshots entry is not a JSON object")
    snapshot_id = last.get("short_id") or last.get("id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise BackupError("restic snapshots entry carries no id")
    return SnapshotInfo(snapshot_id=snapshot_id, time_utc=_parse_snapshot_time(last.get("time")))


def parse_snapshots_latest(stdout: str) -> SnapshotInfo | None:
    """Parse ``restic snapshots --json --latest 1`` output (see the ``_python`` oracle).

    DEC-BACKUP-RUST-OPS-001: under the default-OFF ``BRUTEX_BACKUP_RUST``
    flag the DECISION routes through the ``brutex_ops`` twin. Code 0 covers
    every raise-shaped arm (each embeds a CPython repr / ``json`` error
    text) AND every serde-dialect abstain — the Python oracle re-runs and
    raises the exact bytes. On code 2 the RAW ``time`` string crosses back
    and Python parses it via the oracle's own :func:`_parse_snapshot_time`,
    so time-parse ``BackupError``\\ s are byte-identical by construction.
    """

    def _oracle() -> SnapshotInfo | None:
        return _parse_snapshots_latest_python(stdout)

    def _compute(crate: ModuleType) -> object:
        code, snapshot_id, time_raw = cast(
            "tuple[object, object, object]", crate.parse_snapshots_latest_decision(stdout)
        )
        if code == _CODE_ORACLE:
            return backup_rust.USE_ORACLE
        if code == _CODE_SNAPSHOTS_EMPTY:
            return lambda: None
        if (
            code == _CODE_SNAPSHOTS_OK
            and isinstance(snapshot_id, str)
            and snapshot_id
            and isinstance(time_raw, str)
        ):
            found_id, found_time = snapshot_id, time_raw

            def _materialise() -> SnapshotInfo:
                return SnapshotInfo(snapshot_id=found_id, time_utc=_parse_snapshot_time(found_time))

            return _materialise
        raise TypeError(
            f"poisoned snapshots-latest decision ({code!r}, {snapshot_id!r}, {time_raw!r})"
        )

    return backup_rust.route_law("parse_snapshots_latest", oracle=_oracle, compute=_compute)


def _parse_ls_file_paths_python(stdout: str) -> tuple[str, ...]:
    """Return the FILE node paths from ``restic ls --json`` output (dirs skipped)."""
    paths: list[str] = []
    for obj in _json_lines(stdout):
        if obj.get("type") == "file":
            path = obj.get("path")
            if isinstance(path, str) and path:
                paths.append(path)
    return tuple(paths)


def parse_ls_file_paths(stdout: str) -> tuple[str, ...]:
    """Return the FILE node paths from ``restic ls --json`` output (dirs skipped).

    DEC-BACKUP-RUST-OPS-001: under the default-OFF ``BRUTEX_BACKUP_RUST``
    flag the scan routes through the ``brutex_ops`` twin (``None`` = the
    serde-dialect abstain — the Python oracle serves verbatim).
    """

    def _oracle() -> tuple[str, ...]:
        return _parse_ls_file_paths_python(stdout)

    def _compute(crate: ModuleType) -> object:
        result = crate.parse_ls_file_paths(stdout)
        if result is None:
            return backup_rust.USE_ORACLE
        return _str_tuple_thunk(result)

    return backup_rust.route_law("parse_ls_file_paths", oracle=_oracle, compute=_compute)


def _parse_ls_snapshot_time_python(stdout: str) -> datetime | None:
    """Return the snapshot header's creation time from ``restic ls --json`` output.

    ``restic ls --json`` prints the snapshot's own struct as its first JSON
    line (it carries ``time`` and no node ``type``); the drill uses it for the
    live-mtime-vs-snapshot-time law (§8 step 6). ``None`` when absent — the
    drill falls back to the restored file's own preserved mtime.
    """
    for obj in _json_lines(stdout):
        if obj.get("type") is None and "time" in obj:
            try:
                return _parse_snapshot_time(obj.get("time"))
            except BackupError:
                return None
    return None


def parse_ls_snapshot_time(stdout: str) -> datetime | None:
    """Return the snapshot header's creation time (see the ``_python`` oracle).

    DEC-BACKUP-RUST-OPS-001: under the default-OFF ``BRUTEX_BACKUP_RUST``
    flag the header DECISION routes through the ``brutex_ops`` twin; a code-3
    RAW time string is parsed Python-side under the oracle's own
    ``_parse_snapshot_time`` + ``except BackupError → None`` arm, so values
    AND fallbacks are byte-identical by construction. Code 0 = the
    serde-dialect abstain (oracle verbatim); codes 1/2 are the oracle's
    constant-``None`` outcomes, decided crate-side.
    """

    def _oracle() -> datetime | None:
        return _parse_ls_snapshot_time_python(stdout)

    def _compute(crate: ModuleType) -> object:
        code, time_raw = cast(
            "tuple[object, object]", crate.parse_ls_snapshot_time_decision(stdout)
        )
        if code == _CODE_ORACLE:
            return backup_rust.USE_ORACLE
        if code in (1, 2):  # no-header / non-string-time — both the constant None
            return lambda: None
        if code == _CODE_LS_TIME_STRING and isinstance(time_raw, str):
            found_time = time_raw

            def _materialise() -> datetime | None:
                try:
                    return _parse_snapshot_time(found_time)
                except BackupError:
                    return None

            return _materialise
        raise TypeError(f"poisoned ls-snapshot-time decision ({code!r}, {time_raw!r})")

    return backup_rust.route_law("parse_ls_snapshot_time", oracle=_oracle, compute=_compute)


__all__ = [
    "BACKUP_EXIT_PARTIAL",
    "BACKUP_HOST",
    "BACKUP_TAG",
    "CANARY_TAG",
    "ResticInvocation",
    "ResticRunner",
    "Runner",
    "SnapshotInfo",
    "build_backup_argv",
    "default_runner_factory",
    "parse_backup_summary_snapshot_id",
    "parse_ls_file_paths",
    "parse_ls_snapshot_time",
    "parse_snapshots_latest",
]
