"""The nightly box auto-deploy leg (DEC-BOX-AUTODEPLOY-LANE-001).

``brutex box-update`` / the ``cmd_nightly_run`` pre-chain leg: fast-forward the
operator box's checkout to ``origin/main`` under FAIL-SAFE guards, rebuild the
maturin wheels when the delta touched Rust/build config, assert the doctor
wheel-freshness law GREEN in a FRESH subprocess (import smoke + scrubbed-env
doctor checks), then let the caller re-exec once so the whole night runs the
deployed code.  Every outcome folds into one :class:`BoxUpdateOutcome` block
for the nightly ``report.json``.

Design law (the locked design doc, quoted where load-bearing):

* **Import surface:** stdlib + ``structlog`` + exactly ONE audited in-repo
  import — :func:`brutex.data.atomic_io.atomic_write_simple` for the sidecar
  state files.  The module MUST import cleanly with every maturin wheel
  broken (that is the state it exists to repair), so the env-flag vocabulary
  and the seconds-parse law are LOCAL equality-pinned twins of
  ``brutex.engine.sweep_lookahead`` / ``brutex.config.deadline`` (the
  ``lake_rust_render`` local-twin precedent).
* **Never raises for an operational failure** — every arm folds into the
  outcome (the ``run_chain`` never-raise posture); the ONE fail-loud knob is
  the ``BRUTEX_BOX_UPDATE`` kill switch (:class:`BoxUpdateEnvError`, a
  ``ValueError`` so the CLI's existing pre-chain catch maps it to a clean
  ``EXIT_USER_ERROR``).  ``KeyboardInterrupt``/``SystemExit`` propagate raw.
* **FAIL-OPEN outside the chain break law:** a guard refusal / fetch outage /
  rollback all proceed on the existing checkout — the nightly is never
  blocked by this leg.
* **Never destroy operator work:** restores are crash-marker PROVENANCE
  scoped (§4.5 item 2a) and PRESERVE-FIRST quarantined (every restored
  path's pre-restore bytes are copied + content-verified into
  ``<state_dir>/quarantine/<UTC-ts>/`` before any ``git checkout -f``).
"""

from __future__ import annotations

import enum
import json
import math
import os
import shutil
import sys
import time
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

import structlog

from brutex.data.atomic_io import atomic_write_simple

logger = structlog.get_logger(__name__)

__all__ = [
    "BOX_UPDATE_ENV",
    "BOX_UPDATE_STATE_ENV",
    "WHEEL_SET",
    "BoxUpdateEnvError",
    "BoxUpdateOutcome",
    "BoxUpdateStatus",
    "CommandRunner",
    "DeltaClass",
    "WheelBuildResult",
    "WheelSpec",
    "adopt_state",
    "classify_delta",
    "default_command_runner",
    "default_repo_root",
    "guard_skip_reason",
    "maybe_reexec",
    "resolve_box_update_enabled",
    "run_box_update",
]

# ── env knobs (§7) ───────────────────────────────────────────────────────
#: The fail-loud kill switch — unset/truthy ⇒ enabled, falsy ⇒ disabled,
#: junk ⇒ :class:`BoxUpdateEnvError` (the ONE fail-loud knob of this lane).
BOX_UPDATE_ENV: Final[str] = "BRUTEX_BOX_UPDATE"
#: The re-exec loop-proof sentinel + outcome carrier (§6) — PROCESS-ENV ONLY,
#: never written to any dotenv/profile/plist (the ``BRUTEX_SWEEP_DETACHED``
#: #1712 round-3 precedent).
BOX_UPDATE_STATE_ENV: Final[str] = "BRUTEX_BOX_UPDATE_STATE"
FETCH_TIMEOUT_ENV: Final[str] = "BRUTEX_BOX_UPDATE_FETCH_TIMEOUT_S"
BUILD_TIMEOUT_ENV: Final[str] = "BRUTEX_BOX_UPDATE_BUILD_TIMEOUT_S"
PIP_TIMEOUT_ENV: Final[str] = "BRUTEX_BOX_UPDATE_PIP_TIMEOUT_S"

DEFAULT_FETCH_TIMEOUT_S: Final[float] = 60.0
DEFAULT_BUILD_TIMEOUT_S: Final[float] = 2700.0
DEFAULT_PIP_TIMEOUT_S: Final[float] = 600.0
#: Local git plumbing timeout (rev-parse / status / merge / checkout).
LOCAL_GIT_TIMEOUT_S: Final[float] = 30.0
#: The §4.5 assert-green gate + the §4.6 coherence-check subprocess budget.
GATE_TIMEOUT_S: Final[float] = 120.0
#: The §4.6 post-rollback required-wheel import-smoke budget.
SMOKE_TIMEOUT_S: Final[float] = 60.0
FETCH_ATTEMPTS: Final[int] = 2
FETCH_BACKOFF_S: Final[float] = 5.0
#: The synthesized timeout rc (distinct from 127 on OSError — §1 runner law).
RC_TIMEOUT: Final[int] = 124
#: ``git merge-base --is-ancestor`` "not an ancestor" verdict rc (§2 G6).
_ANCESTOR_RC_DIVERGED: Final[int] = 1
#: One porcelain line = 2-char XY code + one space before the path.
_PORCELAIN_PATH_OFFSET: Final[int] = 3
#: A lane lock path is EXACTLY ``rust/<crate>/Cargo.lock`` — three segments.
_LANE_LOCK_SEGMENTS: Final[int] = 3

#: Bounded-detail law (the chain ``_DETAIL_MAX_CHARS`` mirror, chain.py:82).
_DETAIL_MAX_CHARS: Final[int] = 500
#: Bounded sidecar read cap.  HONEST form: an ``is_file()``-STAT guard + a
#: capped read — NOT the #1614 round-7 fd-based ``O_NONBLOCK`` + ``S_ISREG``
#: discipline; the stat→open TOCTOU residual is disclosed and accepted (the
#: sidecars live under the operator's own lake on a non-adversarial box).
_BOUNDED_READ_CAP: Final[int] = 16 * 1024

_STATE_FILE_NAME: Final[str] = "last_alarm.json"
_MARKER_FILE_NAME: Final[str] = "build_marker.json"
_QUARANTINE_DIRNAME: Final[str] = "quarantine"
_QUARANTINE_KEEP: Final[int] = 10

#: The §4.5 gate import-smoke line — ``brutex.cli`` is what ``python -m
#: brutex`` executes; BOTH required wheels are imported EXPLICITLY (the CLI's
#: top-level closure loads ``trading_core`` only — round-2 B-MINOR-1).
GATE_SMOKE_IMPORTS: Final[str] = (
    "import brutex.cli, brutex.nightly.run, brutex.nightly.chain, trading_core, fno_math"
)
_IMPORT_OK_MARKER: Final[str] = "BOX_UPDATE_IMPORT_OK"
_CHECKS_MARKER: Final[str] = "BOX_UPDATE_CHECKS "
#: The doctor escape the gate child env is SCRUBBED of (§4.5 law 2).
_SKIP_WHEEL_FRESHNESS_ENV: Final[str] = "BRUTEX_SKIP_WHEEL_FRESHNESS"

#: doctor check name → required-crate wheel name (the §4.6 coherence law).
_CHECK_TO_WHEEL: Final[Mapping[str, str]] = {
    "wheel_freshness": "trading_core",
    "wheel_freshness_fno_math": "fno_math",
    "wheel_source_digest": "trading_core",
    "wheel_source_digest_fno_math": "fno_math",
}
_DIGEST_CHECKS: Final[Mapping[str, str]] = {
    "wheel_source_digest": "trading_core",
    "wheel_source_digest_fno_math": "fno_math",
}

# ── local equality-pinned twins (§0.5 — box_update must import wheel-free) ─
#: LOCAL twin of ``brutex.engine.sweep_lookahead._TRUTHY`` (equality-pinned by
#: test — the engine package hard-imports the fno_math wheel).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
#: LOCAL twin of ``brutex.engine.sweep_lookahead._FALSY`` (equality-pinned).
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})


def _resolve_env_seconds(env: Mapping[str, str], env_name: str, default: float) -> float:
    """LOCAL twin of :func:`brutex.config.deadline.resolve_env_seconds`.

    FAIL-SAFE, never raises: unset / blank / non-numeric / non-positive /
    non-finite all silently fall back to the audited *default* — a junk
    timeout value must never block the nightly (§0.5; equality-pinned against
    the COMMON resolver by test — the §1 one-audited-import law forbids the
    ``brutex.config.deadline`` import here).
    """
    value = env.get(env_name)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    if not math.isfinite(parsed) or parsed <= 0.0:
        return default
    return parsed


class BoxUpdateEnvError(ValueError):
    """Junk ``BRUTEX_BOX_UPDATE`` value — the ONE fail-loud env knob.

    Subclasses :class:`ValueError` (the ``FillModeRemovedError`` precedent,
    ``execution/fill_mode.py``) so ``cmd_nightly_run``'s existing fail-loud
    pre-chain catch ``(NightlyEnvError, ValueError)`` maps it to a clean
    ``EXIT_USER_ERROR`` with zero cli.py catch-arm change.
    """


def resolve_box_update_enabled(env: Mapping[str, str]) -> bool:
    """Resolve the ``BRUTEX_BOX_UPDATE`` kill switch (§7).

    unset/truthy → ``True``; falsy → ``False``; anything else raises
    :class:`BoxUpdateEnvError` (fail-loud — caught pre-chain by the CLI).
    """
    raw = env.get(BOX_UPDATE_ENV)
    if raw is None:
        return True
    value = raw.strip().lower()
    if not value or value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise BoxUpdateEnvError(
        f"unrecognised {BOX_UPDATE_ENV}={raw!r} — use one of "
        f"{sorted(_TRUTHY)} / {sorted(_FALSY)} (or unset for the default ON)"
    )


class CommandRunner(Protocol):
    """The injectable subprocess seam (§1) — purpose-built for this lane.

    ``rc 124`` is SYNTHESIZED on ``subprocess.TimeoutExpired`` DISTINCT from
    ``rc 127`` on ``OSError`` (the ``_launchctl_runner`` precedent returns 127
    for both — this lane needs the distinction to report ``result="timeout"``
    honestly).
    """

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout_s: float,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Run *argv* and return ``(rc, stdout, stderr)``."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def default_command_runner(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """The production :class:`CommandRunner`: fixed argv, no shell."""
    import subprocess  # nosec B404 — fixed argv, no shell/user input

    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603 — fixed argv, no shell
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
            cwd=None if cwd is None else str(cwd),
            env=None if env is None else dict(env),
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else ""
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else ""
        return (RC_TIMEOUT, out, err)
    except OSError as exc:
        return (127, "", str(exc))
    return (completed.returncode, completed.stdout, completed.stderr)


def default_repo_root() -> Path:
    """The checkout root this module was imported from.

    ``…/<repo>/brutex/nightly/box_update.py`` → ``parents[2]`` = ``<repo>``.
    The ONE repo-root derivation both CLI call sites share (CLAUDE.md §11
    COMMON — avoids a per-site ``import brutex`` beside the top-level
    ``from brutex import __version__``, the CodeQL import-and-import-from
    class)."""
    return Path(__file__).resolve().parents[2]


# ── wheel set (§1 — the canonical box build set, Makefile-mirrored) ──────
@dataclass(frozen=True, slots=True)
class WheelSpec:
    """One box wheel build DESCRIPTOR (:class:`WheelBuildResult` is the per-run result)."""

    name: str
    crate_dir: str
    required: bool


#: The CANONICAL box wheel set — MIRRORED from the Makefile ``run-raw``
#: target's build list (Makefile:90-105: fno-math, trading-core, brutex-feed;
#: brutex-lake ABSENT — coordinator policy, excluded pending
#: DEC-BRUTEX-LAKE-BOX-WHEEL-001), drift-pinned by a test that PARSES the
#: Makefile's ``cd rust/<crate> && … maturin develop`` lines and asserts set
#: equality (§10 test 16).  Runtime behavior stays a deterministic constant
#: (the lane must run mid-update on a possibly-broken tree); CI catches drift.
WHEEL_SET: Final[tuple[WheelSpec, ...]] = (
    WheelSpec("fno_math", "rust/fno-math", required=True),
    WheelSpec("trading_core", "rust/trading-core", required=True),
    WheelSpec("brutex_feed", "rust/brutex-feed", required=False),
)


class BoxUpdateStatus(enum.StrEnum):
    """The lane's terminal status vocabulary (§1)."""

    UPDATED = "updated"
    UP_TO_DATE = "up_to_date"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"
    ERROR = "error"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class DeltaClass:
    """The §4.4 delta classification result."""

    rust: bool
    pyproject: bool


@dataclass(frozen=True, slots=True)
class WheelBuildResult:
    """One per-run wheel build result (§1)."""

    name: str
    crate_dir: str
    required: bool
    result: str  # "ok" | "failed" | "timeout" | "skipped"
    digest_ok: bool | None
    duration_s: float
    detail: str

    def to_dict(self) -> dict[str, object]:
        """Report projection — ``rebuilt`` is DERIVED here (``result == "ok"``), never stored."""
        return {
            "name": self.name,
            "crate_dir": self.crate_dir,
            "required": self.required,
            "result": self.result,
            "rebuilt": self.result == "ok",
            "digest_ok": self.digest_ok,
            "duration_s": self.duration_s,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class BoxUpdateOutcome:
    """The one immutable record of a box-update run (§1/§5)."""

    status: BoxUpdateStatus
    from_sha: str | None
    to_sha: str | None
    running_sha: str | None
    skip_reason: str | None
    delta_rust: bool
    delta_pyproject: bool
    wheels: tuple[WheelBuildResult, ...]
    gate: str  # "ok" | "stale" | "import_failed" | "skipped" | "error"
    repair: str  # "not_needed" | "rebuilt" | "failed" | "failed_stale"
    pip_restore: str  # "not_needed" | "ok" | "failed"
    lock_restored: tuple[str, ...]
    quarantined: tuple[str, ...]
    alarm_signature: str | None
    alarm_repeats: int
    reexec: bool
    duration_s: float
    detail: str

    def to_dict(self) -> dict[str, object]:
        """Stable key order, report-ready (§5 freeze).

        Mints the TWO named derived bools — the top-level ``updated``
        (``status == BoxUpdateStatus.UPDATED``) and ``wheels[].rebuilt``
        (``result == "ok"``); no other key is derived or omitted.
        """
        return {
            "status": self.status.value,
            "updated": self.status is BoxUpdateStatus.UPDATED,
            "from_sha": self.from_sha,
            "to_sha": self.to_sha,
            "running_sha": self.running_sha,
            "reexec": self.reexec,
            "skip_reason": self.skip_reason,
            "delta_rust": self.delta_rust,
            "delta_pyproject": self.delta_pyproject,
            "wheels": [w.to_dict() for w in self.wheels],
            "gate": self.gate,
            "repair": self.repair,
            "pip_restore": self.pip_restore,
            "lock_restored": list(self.lock_restored),
            "quarantined": list(self.quarantined),
            "alarm_signature": self.alarm_signature,
            "alarm_repeats": self.alarm_repeats,
            "duration_s": self.duration_s,
            "detail": self.detail,
        }


# ── pure decision core (§1) ──────────────────────────────────────────────
def _non_untracked_lines(porcelain: Sequence[str]) -> list[str]:
    """The porcelain lines whose XY code is NOT ``??`` (tracked mods/stage/delete)."""
    return [line for line in porcelain if line.strip() and not line.startswith("??")]


def guard_skip_reason(*, branch: str, porcelain: Sequence[str], ancestor_rc: int) -> str | None:
    """The §2 guard truth table as ONE pure function (``None`` = proceed).

    :func:`run_box_update` DELEGATES its G2/G5/G6 verdicts here (inputs not
    yet measured at a phase ride their proceed values).  ANY nonzero
    ``ancestor_rc`` other than the ``diverged`` verdict rc — including a
    NEGATIVE signal-kill rc — is ``ancestor_check_failed`` (fail-safe:
    an unproven ancestry never proceeds to the merge).
    """
    if branch == "HEAD":
        return "detached_head"
    if branch != "main":
        return "not_on_main"
    if _non_untracked_lines(porcelain):
        return "dirty_tree"
    if ancestor_rc == _ANCESTOR_RC_DIVERGED:
        return "diverged"
    if ancestor_rc != 0:
        return "ancestor_check_failed"
    return None


def classify_delta(changed_paths: Sequence[str]) -> DeltaClass:
    """The §4.4 pattern law over ``git diff --name-only`` output."""
    rust = False
    pyproject = False
    for path in changed_paths:
        p = path.strip()
        if not p:
            continue
        if p.startswith("rust/"):
            rust = True
        basename = p.rsplit("/", 1)[-1]
        if basename in ("Cargo.toml", "Cargo.lock"):
            rust = True
        if p == "pyproject.toml":
            pyproject = True
    return DeltaClass(rust=rust, pyproject=pyproject)


def _porcelain_path(line: str) -> str:
    """Extract the repo-relative path from one ``git status --porcelain`` line."""
    payload = line[_PORCELAIN_PATH_OFFSET:] if len(line) > _PORCELAIN_PATH_OFFSET else ""
    if " -> " in payload:
        payload = payload.split(" -> ", 1)[1]
    return payload.strip().strip('"')


def _tail(text: str, cap: int = _DETAIL_MAX_CHARS) -> str:
    """Bounded tail of *text* (the chain detail-cap law)."""
    text = text.strip()
    return text if len(text) <= cap else text[-cap:]


# ── the run context (internal, mutable) ──────────────────────────────────
@dataclass
class _Ctx:
    """Mutable per-run state for :func:`run_box_update` (internal)."""

    runner: CommandRunner
    env: dict[str, str]
    repo_root: Path
    state_dir: Path | None
    now: Callable[[], float]
    sleep: Callable[[float], None]
    started: float
    fetch_timeout: float
    build_timeout: float
    pip_timeout: float
    from_sha: str | None = None
    to_sha: str | None = None
    delta: DeltaClass = field(default_factory=lambda: DeltaClass(rust=False, pyproject=False))
    wheels: dict[str, WheelBuildResult] = field(default_factory=dict)
    gate: str = "skipped"
    repair: str = "not_needed"
    pip_restore: str = "not_needed"
    forward_pip_invoked: bool = False
    marker_paths: tuple[str, ...] = ()
    repaired_crates: set[str] = field(default_factory=set)
    lock_restored: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    detail_parts: list[str] = field(default_factory=list)
    skip_reason: str | None = None
    digest_by_crate: dict[str, bool | None] = field(default_factory=dict)
    maturin_checked: bool = False
    rollback_from_gate: bool = False


def _git(ctx: _Ctx, *args: str, timeout_s: float = LOCAL_GIT_TIMEOUT_S) -> tuple[int, str, str]:
    """Run one fixed-argv git command through the injected runner."""
    return ctx.runner(["git", *args], cwd=ctx.repo_root, timeout_s=timeout_s, env=None)


# ── sidecar state (marker + alarm dedupe, §4.5 item 2a / §4.7) ───────────
def _bounded_read_json(path: Path) -> object | None:
    """``is_file()``-guarded bounded JSON read — non-regular/oversized/corrupt ⇒ ``None``."""
    try:
        if not path.is_file():
            return None
        with path.open("rb") as fh:
            raw = fh.read(_BOUNDED_READ_CAP + 1)
        if len(raw) > _BOUNDED_READ_CAP:
            return None
        payload: object = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    return payload


def _sidecar_write(ctx: _Ctx, name: str, payload: Mapping[str, object]) -> bool:
    """Atomic sidecar JSON write under the state dir; lazy mkdir; never raises."""
    if ctx.state_dir is None:
        return False
    dest = ctx.state_dir / name
    try:
        ctx.state_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(dict(payload), sort_keys=False)
        atomic_write_simple(dest, lambda tmp: tmp.write_text(text, encoding="utf-8"))
    except OSError:
        return False
    return True


def _write_marker(ctx: _Ctx, lock_paths: Sequence[str]) -> None:
    """§4 step 4b0 — atomically write the crash marker BEFORE the first maturin call."""
    if ctx.state_dir is None:
        return
    ok = _sidecar_write(
        ctx,
        _MARKER_FILE_NAME,
        {
            "schema": 1,
            "lock_paths": list(lock_paths),
            "started_utc": datetime.now(UTC).isoformat(),
        },
    )
    if not ok:
        logger.warning(
            "box_update.build_marker_write_failed",
            marker=str(ctx.state_dir / _MARKER_FILE_NAME),
            detail="cross-run prologue protection lost for this run (same-run restores unaffected)",
        )


def _is_lane_lock_path(path: str) -> bool:
    """``True`` iff *path* has the ONE shape the lane itself ever markers.

    The marker may scope a restore ONLY to a ``rust/<crate>/Cargo.lock``
    path (defense-in-depth: a TAMPERED marker must never scope a
    ``git checkout -f`` at an arbitrary tracked path — the restore already
    intersects with the fresh dirty porcelain, this bounds the pathset
    STRUCTURALLY too).  Exactly three ``/``-segments, ``rust`` first,
    ``Cargo.lock`` last, a non-empty non-dot-token crate dir between, no
    backslash anywhere.
    """
    parts = path.split("/")
    return (
        len(parts) == _LANE_LOCK_SEGMENTS
        and parts[0] == "rust"
        and parts[2] == "Cargo.lock"
        and bool(parts[1])
        and parts[1] not in (".", "..")
        and "\\" not in path
    )


def _read_marker(ctx: _Ctx) -> tuple[str, ...] | None:
    """Read the crash marker; corrupt/non-regular/foreign-path ⇒ ABSENT + one WARN."""
    if ctx.state_dir is None:
        return None
    path = ctx.state_dir / _MARKER_FILE_NAME
    if not path.exists() and not path.is_symlink():
        return None
    payload = _bounded_read_json(path)
    if (
        isinstance(payload, dict)
        and payload.get("schema") == 1
        and isinstance(payload.get("lock_paths"), list)
        and all(isinstance(p, str) and _is_lane_lock_path(p) for p in payload["lock_paths"])
    ):
        return tuple(payload["lock_paths"])
    logger.warning(
        "box_update.build_marker_unreadable",
        marker=str(path),
        detail=(
            "corrupt/oversized/non-regular/foreign-path crash marker treated as ABSENT — no "
            "restore on unproven provenance (a dirty lane lock then takes the visible "
            "dirty_tree skip); the marker may only ever scope rust/<crate>/Cargo.lock paths"
        ),
    )
    return None


def _clear_marker(ctx: _Ctx) -> None:
    """Clear the crash marker (idempotent — clearing an absent marker is a no-op)."""
    if ctx.state_dir is None:
        return
    try:
        (ctx.state_dir / _MARKER_FILE_NAME).unlink(missing_ok=True)
    except OSError:
        logger.warning("box_update.build_marker_clear_failed", marker=str(ctx.state_dir))
    ctx.marker_paths = ()


# ── quarantine + restore (§4.5 items 2/2a — preserve-first law) ──────────
def _quarantine_dir(ctx: _Ctx) -> Path | None:
    """Mint one ``<state_dir>/quarantine/<UTC-ts>[-NN]`` dir per helper invocation."""
    if ctx.state_dir is None:
        return None
    root = ctx.state_dir / _QUARANTINE_DIRNAME
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for serial in range(100):
        candidate = root / (stamp if serial == 0 else f"{stamp}-{serial:02d}")
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        except OSError:
            return None
        return candidate
    return None


def _prune_quarantine(ctx: _Ctx) -> None:
    """Bounded retention: newest ``_QUARANTINE_KEEP`` lane-created run dirs survive."""
    if ctx.state_dir is None:
        return
    root = ctx.state_dir / _QUARANTINE_DIRNAME
    try:
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return
    for stale in dirs[:-_QUARANTINE_KEEP]:
        try:
            shutil.rmtree(stale)
        except OSError as exc:
            logger.warning(
                "box_update.quarantine_prune_failed", path=repr(str(stale)), error=repr(str(exc))
            )


def _quarantine_copy(ctx: _Ctx, qdir: Path | None, rel_path: str) -> bool:
    """PRESERVE-FIRST: copy + CONTENT-verify one path's bytes; False ⇒ NO restore."""
    if qdir is None:
        # The vacuous-fail arm (§9 row 24): state_dir=None / an uncreatable
        # quarantine run dir makes a copy IMPOSSIBLE — the restore is refused.
        logger.warning(
            "box_update.quarantine_copy_failed",
            path=repr(rel_path),
            error=repr("quarantine dir unavailable (state_dir=None or mkdir failed)"),
        )
        return False
    src = ctx.repo_root / rel_path
    dst = qdir / rel_path.replace("/", "__")
    try:
        source_bytes = src.read_bytes()
        dst.write_bytes(source_bytes)
        if dst.read_bytes() != source_bytes:
            raise OSError("quarantine content verify mismatch")
    except OSError as exc:
        logger.warning(
            "box_update.quarantine_copy_failed", path=repr(rel_path), error=repr(str(exc))
        )
        return False
    return True


def restore_lane_lock_artifacts(ctx: _Ctx, lock_paths: Sequence[str]) -> None:
    """The §4.5 ALWAYS-ON lane lock-artifact restore (marker-∩-porcelain scoped).

    Pathset = *lock_paths* (the run's crash-marker provenance) ∩ the FRESH
    porcelain's dirty non-``??`` lines; each path is quarantine-copied +
    content-verified FIRST (copy-fail ⇒ that path is NOT restored); the
    checkout argv carries the EXACT porcelain paths (never a glob, never a
    whole-tree ``checkout -f``); zero matched paths ⇒ NO checkout command.
    """
    if not lock_paths:
        return
    rc, out, err = _git(ctx, "status", "--porcelain")
    if rc != 0:
        logger.warning(
            "box_update.lock_restore_failed",
            paths=[repr(p) for p in lock_paths],
            rc=rc,
            error=repr(_tail(err)),
            detail="porcelain read failed — no restore attempted",
        )
        return
    dirty = {_porcelain_path(line) for line in _non_untracked_lines(out.splitlines())}
    targets = [p for p in lock_paths if p in dirty]
    if not targets:
        return
    qdir = _quarantine_dir(ctx)
    verified: list[str] = []
    for rel in targets:
        if _quarantine_copy(ctx, qdir, rel):
            verified.append(rel)
            ctx.quarantined.append(rel)
    if qdir is not None and verified:
        _prune_quarantine(ctx)
    if not verified:
        return
    rc, _, err = _git(ctx, "checkout", "-f", "--", *verified)
    if rc != 0:
        logger.warning(
            "box_update.lock_restore_failed",
            paths=[repr(p) for p in verified],
            rc=rc,
            error=repr(_tail(err)),
        )
        return
    ctx.lock_restored.extend(verified)
    logger.warning(
        "box_update.lock_artifact_restored",
        paths=[repr(p) for p in verified],
        detail=(
            "the committed Cargo.lock is stale vs its manifest — land the moved lock "
            "upstream; the box build regenerated it locally and the lane restored the "
            "committed bytes"
        ),
    )


def _marker_prologue(ctx: _Ctx) -> None:
    """§2 row P — the pre-G4a MARKER PROLOGUE (marker-gated; NO marker ⇒ ZERO actions)."""
    paths = _read_marker(ctx)
    if paths is None:
        return
    restore_lane_lock_artifacts(ctx, paths)
    _clear_marker(ctx)


# ── wheel builds (§4.5) ──────────────────────────────────────────────────
def _venv_bin() -> Path:
    """The venv bin dir — ``Path(sys.executable).parent`` (the Makefile shape)."""
    return Path(sys.executable).parent


def _ensure_maturin(ctx: _Ctx) -> None:
    """One ``pip install maturin`` attempt when the venv binary is missing (§4.5)."""
    if ctx.maturin_checked:
        return
    ctx.maturin_checked = True
    venv_bin = _venv_bin()
    if (venv_bin / "maturin").exists():
        return
    rc, _, err = ctx.runner(
        [str(venv_bin / "pip"), "install", "maturin"],
        cwd=ctx.repo_root,
        timeout_s=ctx.pip_timeout,
        env=None,
    )
    if rc != 0:
        logger.warning("box_update.maturin_install_failed", rc=rc, error=repr(_tail(err)))


def _build_one(ctx: _Ctx, spec: WheelSpec) -> WheelBuildResult:
    """One BARE per-crate ``maturin develop --release`` (byte-matching the Makefile)."""
    _ensure_maturin(ctx)
    venv_bin = _venv_bin()
    build_env = dict(ctx.env)
    build_env["VIRTUAL_ENV"] = str(venv_bin.parent)
    build_env["PATH"] = str(Path.home() / ".cargo" / "bin") + os.pathsep + ctx.env.get("PATH", "")
    started = ctx.now()
    rc, _, err = ctx.runner(
        [str(venv_bin / "maturin"), "develop", "--release"],
        cwd=ctx.repo_root / spec.crate_dir,
        timeout_s=ctx.build_timeout,
        env=build_env,
    )
    duration = max(0.0, ctx.now() - started)
    if rc == 0:
        result, detail = "ok", ""
    elif rc == RC_TIMEOUT:
        result, detail = "timeout", _tail(err)
    else:
        result, detail = "failed", _tail(err)
    return WheelBuildResult(
        name=spec.name,
        crate_dir=spec.crate_dir,
        required=spec.required,
        result=result,
        digest_ok=None,
        duration_s=duration,
        detail=detail,
    )


def _build_wheels(ctx: _Ctx, specs: Sequence[WheelSpec]) -> bool:
    """§4 steps 4b0-4c: marker write → per-crate builds → always-on restores.

    Returns ``True`` when no REQUIRED build failed (a required failure
    SHORT-CIRCUITS the remaining builds straight into the rollback path; the
    marker then stays for the rollback-entry restore — lifecycle point (iv)).
    """
    lock_paths = tuple(f"{s.crate_dir}/Cargo.lock" for s in specs)
    ctx.marker_paths = lock_paths
    _write_marker(ctx, lock_paths)
    for spec in specs:
        result = _build_one(ctx, spec)
        # _build_wheels runs at most ONCE per run (the forward §4 step 4b OR
        # the §7 self-heal), so this is the crate's FIRST recorded result.
        ctx.wheels[spec.name] = result
        restore_lane_lock_artifacts(ctx, ctx.marker_paths)
        if result.result != "ok":
            logger.warning(
                "box_update.wheel_build_failed",
                wheel=spec.name,
                required=spec.required,
                result=result.result,
                error=repr(result.detail),
            )
            if spec.required:
                return False
    _clear_marker(ctx)
    return True


def _pip_install(ctx: _Ctx) -> int:
    """The canonical extras-qualified editable install (ONE argv, two mouths — §4.5/§4.6)."""
    venv_bin = _venv_bin()
    rc, _, err = ctx.runner(
        [str(venv_bin / "pip"), "install", "-e", ".[data,ui,groww]"],
        cwd=ctx.repo_root,
        timeout_s=ctx.pip_timeout,
        env=None,
    )
    if rc != 0:
        logger.warning("box_update.pip_install_failed", rc=rc, error=repr(_tail(err)))
    return rc


# ── the assert-green gate + doctor-check leg (§4.5 / §4.6) ───────────────
def _child_script(include_smoke: bool) -> str:
    """Build the ONE gate/coherence child script (import smoke + doctor checks)."""
    lines: list[str] = []
    if include_smoke:
        lines.append(GATE_SMOKE_IMPORTS)
    lines += [
        f"print({_IMPORT_OK_MARKER!r})",
        "import json",
        "from brutex.doctor import (",
        "    check_fno_math_wheel_freshness,",
        "    check_fno_math_wheel_source_digest,",
        "    check_wheel_freshness,",
        "    check_wheel_source_digest,",
        ")",
        "_checks = [",
        "    check_wheel_freshness(),",
        "    check_fno_math_wheel_freshness(),",
        "    check_wheel_source_digest(),",
        "    check_fno_math_wheel_source_digest(),",
        "]",
        f"print({_CHECKS_MARKER!r} + json.dumps([[c.name, c.status, c.detail] for c in _checks]))",
    ]
    return "\n".join(lines)


def _run_doctor_child(
    ctx: _Ctx, *, include_smoke: bool
) -> tuple[str, list[tuple[str, str, str]] | None]:
    """Run the fresh gate/coherence subprocess; return ``(verdict, checks|None)``.

    The child env is the parent env MINUS ``BRUTEX_SKIP_WHEEL_FRESHNESS``
    (§4.5 law 2 — the escape must never green the gate).
    """
    child_env = {k: v for k, v in ctx.env.items() if k != _SKIP_WHEEL_FRESHNESS_ENV}
    rc, out, _err = ctx.runner(
        [sys.executable, "-c", _child_script(include_smoke)],
        cwd=ctx.repo_root,
        timeout_s=GATE_TIMEOUT_S,
        env=child_env,
    )
    if rc != 0:
        if rc != RC_TIMEOUT and _IMPORT_OK_MARKER not in out:
            return ("import_failed", None)
        return ("error", None)
    for line in out.splitlines():
        if line.startswith(_CHECKS_MARKER):
            try:
                raw = json.loads(line[len(_CHECKS_MARKER) :])
                checks = [(str(n), str(s), str(d)) for n, s, d in raw]
            except (ValueError, TypeError):
                return ("error", None)
            return (_fold_checks(checks), checks)
    return ("error", None)


def _fold_checks(checks: Sequence[tuple[str, str, str]]) -> str:
    """§4.5 law 3 — the SKIP-law fold over the four doctor check verdicts."""
    for _name, status, detail in checks:
        if status == "WARN":
            return "stale"
        if status == "SKIP" and _SKIP_WHEEL_FRESHNESS_ENV in detail:
            return "stale"  # the belt behind the primary env scrub
        if status not in ("PASS", "SKIP"):
            return "stale"
    return "ok"


def _apply_digest_verdicts(ctx: _Ctx, checks: Sequence[tuple[str, str, str]]) -> None:
    """Fold the ``wheel_source_digest`` verdicts into per-crate ``digest_ok``."""
    for name, status, _detail in checks:
        crate = _DIGEST_CHECKS.get(name)
        if crate is None:
            continue
        if status == "PASS":
            ctx.digest_by_crate[crate] = True
        elif status == "WARN":
            ctx.digest_by_crate[crate] = False
        else:
            ctx.digest_by_crate[crate] = None


def _warn_crates(checks: Sequence[tuple[str, str, str]]) -> set[str]:
    """Required crates carrying ANY WARN check (the §4.6 coherence verdict law)."""
    crates: set[str] = set()
    for name, status, _detail in checks:
        crate = _CHECK_TO_WHEEL.get(name)
        if crate is not None and status == "WARN":
            crates.add(crate)
    return crates


def _run_gate(ctx: _Ctx) -> str:
    """The §4.5 assert-green gate — ONE fresh subprocess, smoke first, then checks."""
    verdict, checks = _run_doctor_child(ctx, include_smoke=True)
    if checks is not None:
        _apply_digest_verdicts(ctx, checks)
    return verdict


def _smoke(ctx: _Ctx, imports: str = "import trading_core, fno_math") -> int:
    """The §4.6 required-wheel import smoke in a fresh subprocess (60 s)."""
    rc, _, _ = ctx.runner(
        [sys.executable, "-c", imports],
        cwd=ctx.repo_root,
        timeout_s=SMOKE_TIMEOUT_S,
        env=None,
    )
    return rc


def _required_specs() -> tuple[WheelSpec, ...]:
    """Return the wheel specs that MUST load for the box to be usable.

    The subset of :data:`WHEEL_SET` whose ``required`` flag is set (the
    load-bearing kernels — e.g. ``fno_math`` / ``trading_core``); an optional
    wheel (``brutex-feed``) is excluded. Drives the §4.6 assert-green import
    smoke and the rollback repair sweep.

    Returns
    -------
    tuple[WheelSpec, ...]
        The required wheel specs, in ``WHEEL_SET`` order.
    """
    return tuple(s for s in WHEEL_SET if s.required)


def _repair_build(ctx: _Ctx, specs: Sequence[WheelSpec]) -> bool:
    """One §4.6 repair rebuild pass (marker re-stamp + restore sweep + clear).

    Repair builds never OVERWRITE a forward build's recorded wheel result
    (§4.6 — "wheels results preserved in the outcome"); they only add a
    result for a crate that never forward-built this run.
    """
    lock_paths = tuple(f"{s.crate_dir}/Cargo.lock" for s in specs)
    ctx.marker_paths = lock_paths
    _write_marker(ctx, lock_paths)
    ok = True
    for spec in specs:
        ctx.repaired_crates.add(spec.name)
        result = _build_one(ctx, spec)
        if spec.name not in ctx.wheels:
            ctx.wheels[spec.name] = result
        restore_lane_lock_artifacts(ctx, ctx.marker_paths)
        if result.result != "ok":
            ok = False
    _clear_marker(ctx)
    return ok


# ── alarm dedupe (§4.7) ──────────────────────────────────────────────────
def _alarm_signature(ctx: _Ctx, status: BoxUpdateStatus) -> str:
    """Stable machine-token signature — never free text (§4.7 law 1)."""
    failed_wheels = ",".join(
        sorted(
            f"{w.name}:{w.result}" for w in ctx.wheels.values() if w.required and w.result != "ok"
        )
    )
    return (
        f"{status.value}|{ctx.skip_reason or ''}|{ctx.gate}|{ctx.repair}|"
        f"{ctx.pip_restore}|{failed_wheels}"
    )


def _read_alarm_state(ctx: _Ctx) -> tuple[str, str, int] | None:
    """Bounded alarm-state read → ``(signature, first_utc, nights)``.

    Corrupt / oversized / non-regular ⇒ treated as absent + one WARN
    (fail-open to FULL severity — louder, never quieter; §4.7 law 5).
    """
    if ctx.state_dir is None:
        return None
    path = ctx.state_dir / _STATE_FILE_NAME
    if not path.exists() and not path.is_symlink():
        return None
    payload = _bounded_read_json(path)
    if isinstance(payload, dict):
        signature = payload.get("signature")
        nights = payload.get("nights")
        first_utc = payload.get("first_utc")
        if (
            isinstance(signature, str)
            and isinstance(nights, int)
            and not isinstance(nights, bool)
            and nights >= 1
        ):
            return (signature, str(first_utc), nights)
    logger.warning(
        "box_update.alarm_state_unreadable",
        state=str(path),
        detail="corrupt/oversized/non-regular alarm state treated as absent — FULL severity",
    )
    return None


def _apply_alarm_dedupe(ctx: _Ctx, status: BoxUpdateStatus) -> tuple[str | None, int, bool]:
    """§4.7 severity fold: ``(signature|None, alarm_repeats, demoted)``.

    Healthy nights (updated/up_to_date) DELETE the state; disabled/skipped
    nights leave it untouched; alarm nights (rolled_back/error) compare +
    write.  ``state_dir=None`` disables the dedupe (full severity always).
    """
    if status in (BoxUpdateStatus.UPDATED, BoxUpdateStatus.UP_TO_DATE):
        if ctx.state_dir is not None:
            try:
                (ctx.state_dir / _STATE_FILE_NAME).unlink(missing_ok=True)
            except OSError:
                logger.warning("box_update.alarm_state_clear_failed", state=str(ctx.state_dir))
        return (None, 0, False)
    if status in (BoxUpdateStatus.DISABLED, BoxUpdateStatus.SKIPPED):
        return (None, 0, False)
    signature = _alarm_signature(ctx, status)
    if ctx.state_dir is None:
        return (signature, 0, False)
    state = _read_alarm_state(ctx)
    now_iso = datetime.now(UTC).isoformat()
    if state is not None and state[0] == signature:
        first_utc, nights = state[1], state[2] + 1
        wrote = _sidecar_write(
            ctx,
            _STATE_FILE_NAME,
            {"schema": 1, "signature": signature, "first_utc": first_utc, "nights": nights},
        )
        if not wrote:
            logger.warning("box_update.alarm_state_write_failed", state=str(ctx.state_dir))
        return (signature, nights - 1, True)
    wrote = _sidecar_write(
        ctx,
        _STATE_FILE_NAME,
        {"schema": 1, "signature": signature, "first_utc": now_iso, "nights": 1},
    )
    if not wrote:
        logger.warning("box_update.alarm_state_write_failed", state=str(ctx.state_dir))
    return (signature, 0, False)


def _emit_alarms(
    ctx: _Ctx, status: BoxUpdateStatus, signature: str | None, repeats: int, demoted: bool
) -> None:
    """Emit the failure-matrix alarm events at the §4.7-folded severity.

    The log/Telegram integer is the state file's TOTAL-nights count
    (``repeat = repeats + 1``); the report carries ``alarm_repeats = repeats``
    (the two-integer law, §4.7 law 3).  A demoted night logs every
    CRITICAL-class event at WARN.
    """
    if signature is None:
        return
    repeat_fields: dict[str, object] = {"signature": signature}
    if demoted:
        repeat_fields["repeat"] = repeats + 1
    critical = logger.warning if demoted else logger.critical

    if ctx.skip_reason == "postmerge_verify_failed":
        logger.warning("box_update.postmerge_verify_failed", **repeat_fields)
    if ctx.rollback_from_gate:
        critical(
            "box_update.gate_failed",
            gate=ctx.gate,
            detail=(
                "the assert-green gate refused the new tree (import_failed = the new code "
                "provably does not start; stale = doctor wheel drift) — rolled back, the "
                "nightly proceeds on old code"
            ),
            **repeat_fields,
        )
    if status is BoxUpdateStatus.ROLLED_BACK:
        logger.warning(
            "box_update.rolled_back",
            from_sha=ctx.from_sha,
            to_sha=ctx.to_sha,
            gate=ctx.gate,
            repair=ctx.repair,
            **repeat_fields,
        )
    if ctx.repair == "failed":
        critical(
            "box_update.required_wheel_unloadable",
            detail=(
                "HARD CLI-DOWN: a required wheel is unloadable and the repair rebuild "
                "failed — the next invocation cannot import; remediation: "
                "cd rust/<crate> && maturin develop --release (a fresh shell needs the "
                "env-prefixed form — see docs/runbooks/box-autodeploy.md §4 wedge-rescue)"
            ),
            **repeat_fields,
        )
    if ctx.repair == "failed_stale":
        logger.warning(
            "box_update.repair_failed_stale",
            detail=(
                "torn-version residual: the wheel is loadable but venv/tree mismatched "
                "(digest_ok=false) — re-attempted next night; manual fix: the env-prefixed "
                "wedge-rescue command (docs/runbooks/box-autodeploy.md §4)"
            ),
            **repeat_fields,
        )
    if ctx.pip_restore == "failed":
        logger.warning(
            "box_update.pip_restore_failed",
            detail=(
                "the post-rollback pip-dep restore failed — deps may sit at to_sha against "
                "the from_sha tree; remediation: pip install -e '.[data,ui,groww]'"
            ),
            **repeat_fields,
        )
    if status is BoxUpdateStatus.ERROR and ctx.skip_reason == "rollback_failed":
        critical(
            "box_update.rollback_failed",
            from_sha=ctx.from_sha,
            to_sha=ctx.to_sha,
            detail=(
                "git checkout -B refused (a concurrent writer dirtied a tracked file "
                "mid-run) — tonight's process is coherent (old-imported); the next "
                "invocation may fail to import; manual remediation required"
            ),
            **repeat_fields,
        )


# ── outcome assembly ─────────────────────────────────────────────────────
def _finish(
    ctx: _Ctx,
    status: BoxUpdateStatus,
    *,
    running_sha: str | None = None,
    reexec: bool = False,
) -> BoxUpdateOutcome:
    """Assemble the outcome; run the §4.7 dedupe + emit the alarm events."""
    wheels = []
    for spec in WHEEL_SET:
        if spec.name in ctx.wheels:
            result = ctx.wheels[spec.name]
            digest = ctx.digest_by_crate.get(spec.name, result.digest_ok)
            wheels.append(
                WheelBuildResult(
                    name=result.name,
                    crate_dir=result.crate_dir,
                    required=result.required,
                    result=result.result,
                    digest_ok=digest,
                    duration_s=result.duration_s,
                    detail=result.detail,
                )
            )
    signature, repeats, demoted = _apply_alarm_dedupe(ctx, status)
    _emit_alarms(ctx, status, signature, repeats, demoted)
    # The report lists are ORDERED-UNIQUE (a multi-build night restores the
    # same lane lock after each build — one path, never N duplicate rows).
    lock_restored = tuple(dict.fromkeys(ctx.lock_restored))
    quarantined = tuple(dict.fromkeys(ctx.quarantined))
    return BoxUpdateOutcome(
        status=status,
        from_sha=ctx.from_sha,
        to_sha=ctx.to_sha,
        running_sha=running_sha if running_sha is not None else ctx.from_sha,
        skip_reason=ctx.skip_reason,
        delta_rust=ctx.delta.rust,
        delta_pyproject=ctx.delta.pyproject,
        wheels=tuple(wheels),
        gate=ctx.gate,
        repair=ctx.repair,
        pip_restore=ctx.pip_restore,
        lock_restored=lock_restored,
        quarantined=quarantined,
        alarm_signature=signature,
        alarm_repeats=repeats,
        reexec=reexec,
        duration_s=max(0.0, ctx.now() - ctx.started),
        detail=_tail("; ".join(ctx.detail_parts)),
    )


def _skip(ctx: _Ctx, reason: str, *, evidence: str = "") -> BoxUpdateOutcome:
    """One structured guard skip — WARN + SKIPPED outcome, tree untouched (§2)."""
    ctx.skip_reason = reason
    if evidence:
        ctx.detail_parts.append(evidence)
    logger.warning("box_update.skipped", reason=reason, evidence=repr(evidence))
    return _finish(ctx, BoxUpdateStatus.SKIPPED)


# ── the §4.6 rollback + repair arm ───────────────────────────────────────
def _rollback_path(ctx: _Ctx, *, vacuous: bool = False) -> BoxUpdateOutcome:
    """§4.6 — rollback (vacuous on the §7 self-heal path), pip restore, smoke,
    repair-on-unloadable, then the always-once-loadable version-coherence check."""
    restore_lane_lock_artifacts(ctx, ctx.marker_paths)
    _clear_marker(ctx)
    if not vacuous:
        # from_sha is always resolved before any rollback-capable step; an empty
        # token would make the checkout refuse -> the honest rollback_failed arm.
        rc, _, err = _git(ctx, "checkout", "-B", "main", ctx.from_sha or "")
        if rc != 0:
            ctx.skip_reason = "rollback_failed"
            ctx.pip_restore = "not_needed"  # tree stayed at to_sha — deps tree-coherent
            ctx.detail_parts.append(f"rollback_failed: {_tail(err, 200)}")
            return _finish(ctx, BoxUpdateStatus.ERROR)
        if ctx.forward_pip_invoked:
            ctx.pip_restore = "ok" if _pip_install(ctx) == 0 else "failed"
    # Post-rollback required-wheel import smoke (§9 rows 10a/10b).
    door_repaired = False
    if _smoke(ctx) != 0:
        broken = [
            spec for spec in _required_specs() if _smoke(ctx, imports=f"import {spec.name}") != 0
        ] or list(_required_specs())
        build_ok = _repair_build(ctx, broken)
        door_repaired = True
        if not build_ok or _smoke(ctx) != 0:
            ctx.repair = "failed"
            return _finish(ctx, BoxUpdateStatus.ERROR)
    # ONCE LOADABLE — the version-coherence check over ALL required crates.
    verdict, checks = _run_doctor_child(ctx, include_smoke=False)
    if checks is None:
        logger.warning("box_update.coherence_check_error", verdict=verdict)
        ctx.repair = "rebuilt" if door_repaired else "not_needed"
        return _finish(ctx, BoxUpdateStatus.ROLLED_BACK)
    _apply_digest_verdicts(ctx, checks)
    warned = _warn_crates(checks)
    if not warned:
        ctx.repair = "rebuilt" if door_repaired else "not_needed"
        return _finish(ctx, BoxUpdateStatus.ROLLED_BACK)
    to_rebuild = [
        spec
        for spec in _required_specs()
        if spec.name in warned and spec.name not in ctx.repaired_crates
    ]
    if not to_rebuild:
        # Already rebuilt this run and still WARN — folds failed_stale (at-most-once law).
        ctx.repair = "failed_stale"
        return _finish(ctx, BoxUpdateStatus.ROLLED_BACK)
    build_ok = _repair_build(ctx, to_rebuild)
    if _smoke(ctx) != 0:
        ctx.repair = "failed"
        return _finish(ctx, BoxUpdateStatus.ERROR)
    _verdict2, checks2 = _run_doctor_child(ctx, include_smoke=False)
    if checks2 is not None:
        _apply_digest_verdicts(ctx, checks2)
    rebuilt_names = {s.name for s in to_rebuild}
    digest_still_warn = checks2 is not None and any(
        status == "WARN" and _DIGEST_CHECKS.get(name) in rebuilt_names
        for name, status, _d in checks2
    )
    if not build_ok or digest_still_warn or checks2 is None:
        ctx.repair = "failed_stale"
    else:
        ctx.repair = "rebuilt"
    return _finish(ctx, BoxUpdateStatus.ROLLED_BACK)


# ── the deploy leg (§4 flow) ─────────────────────────────────────────────
def run_box_update(
    *,
    repo_root: Path,
    runner: CommandRunner,
    env: Mapping[str, str],
    now: Callable[[], float] = time.time,
    state_dir: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> BoxUpdateOutcome:
    """Run the whole §4 deploy leg once; NEVER raises for an operational failure.

    *state_dir* = the lane state dir (production ``<lake_root>/.box_update``)
    holding the §4.7 ``last_alarm.json``, the §4.5 ``build_marker.json`` crash
    marker and the preserve-first ``quarantine/<UTC-ts>/`` tree.  ``None`` ⇒
    dedupe disabled, no durable marker, and NO restore checkout can run at
    all (the preserve-first law refuses restores without a verified copy).
    ``KeyboardInterrupt``/``SystemExit`` propagate raw; a junk kill-switch
    value raises :class:`BoxUpdateEnvError` (pre-validated by both CLI mouths).
    """
    started = now()
    ctx = _Ctx(
        runner=runner,
        env=dict(env),
        repo_root=repo_root,
        state_dir=state_dir,
        now=now,
        sleep=sleep,
        started=started,
        fetch_timeout=_resolve_env_seconds(env, FETCH_TIMEOUT_ENV, DEFAULT_FETCH_TIMEOUT_S),
        build_timeout=_resolve_env_seconds(env, BUILD_TIMEOUT_ENV, DEFAULT_BUILD_TIMEOUT_S),
        pip_timeout=_resolve_env_seconds(env, PIP_TIMEOUT_ENV, DEFAULT_PIP_TIMEOUT_S),
    )
    # G0 — the kill switch: a DISABLED lane performs ZERO actions.
    if not resolve_box_update_enabled(env):
        return _finish(ctx, BoxUpdateStatus.DISABLED)
    # G1 — repo checkout exists and IS this repo_root.
    rc, out, _ = _git(ctx, "rev-parse", "--show-toplevel")
    if rc != 0 or not (repo_root / ".git").exists():
        return _skip(ctx, "not_a_repo_checkout", evidence=f"rev-parse rc={rc}")
    try:
        toplevel_matches = Path(out.strip()).resolve() == repo_root.resolve()
    except OSError:
        toplevel_matches = False
    if not toplevel_matches:
        return _skip(ctx, "not_a_repo_checkout", evidence=f"toplevel={out.strip()!r}")
    rc, out, _ = _git(ctx, "rev-parse", "HEAD")
    if rc != 0:
        return _skip(ctx, "rev_parse_failed", evidence="rev-parse HEAD failed")
    ctx.from_sha = out.strip()
    # G2 — exactly `main`, never detached.
    rc, out, _ = _git(ctx, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        return _skip(ctx, "rev_parse_failed", evidence="rev-parse --abbrev-ref failed")
    # The G2/G5/G6 verdicts each DELEGATE to the ONE pure truth table
    # (guard_skip_reason); inputs not yet measured ride their proceed values.
    branch = out.strip()
    branch_reason = guard_skip_reason(branch=branch, porcelain=(), ancestor_rc=0)
    if branch_reason == "detached_head":
        return _skip(ctx, branch_reason)
    if branch_reason is not None:
        return _skip(ctx, branch_reason, evidence=f"branch={branch!r}")
    # P — the pre-G4a MARKER PROLOGUE (marker-gated; before G3/G4/G4a).
    _marker_prologue(ctx)
    # G3 — fetch, 2 attempts, FAIL-OPEN.
    fetched = False
    for attempt in range(FETCH_ATTEMPTS):
        rc, _, _ = _git(ctx, "fetch", "origin", "main", timeout_s=ctx.fetch_timeout)
        if rc == 0:
            fetched = True
            break
        if attempt + 1 < FETCH_ATTEMPTS:
            ctx.sleep(FETCH_BACKOFF_S)
    if not fetched:
        return _skip(ctx, "fetch_failed", evidence=f"fetch rc={rc}")
    # G4 — both SHAs resolve.
    rc, out, _ = _git(ctx, "rev-parse", "origin/main")
    if rc != 0:
        return _skip(ctx, "rev_parse_failed", evidence="rev-parse origin/main failed")
    ctx.to_sha = out.strip()
    # G4a — same-SHA fast path (gate still runs — the §7 self-heal law).
    if ctx.from_sha == ctx.to_sha:
        return _up_to_date_path(ctx)
    # G5 — porcelain guard (non-`??` blocks; untracked counted only).
    rc, out, _ = _git(ctx, "status", "--porcelain")
    if rc != 0:
        return _skip(ctx, "porcelain_failed", evidence=f"status --porcelain rc={rc}")
    lines = [line for line in out.splitlines() if line.strip()]
    blocked = _non_untracked_lines(lines)
    dirty_reason = guard_skip_reason(branch=branch, porcelain=lines, ancestor_rc=0)
    if dirty_reason is not None:
        sample = ", ".join(repr(_porcelain_path(line)) for line in blocked[:5])
        return _skip(ctx, dirty_reason, evidence=f"{len(blocked)} tracked change(s): {sample}")
    untracked = len(lines) - len(blocked)
    if untracked:
        ctx.detail_parts.append(f"untracked_files={untracked}")
    # G6 — strict ancestor.
    rc, _, _ = _git(ctx, "merge-base", "--is-ancestor", "HEAD", "origin/main")
    ancestor_reason = guard_skip_reason(branch=branch, porcelain=(), ancestor_rc=rc)
    if ancestor_reason == "diverged":
        return _skip(ctx, ancestor_reason)
    if ancestor_reason is not None:
        return _skip(ctx, ancestor_reason, evidence=f"merge-base rc={rc}")
    # §3 — ff-only merge + post-merge verify.
    rc, _, err = _git(ctx, "merge", "--ff-only", "origin/main")
    if rc != 0:
        return _skip(ctx, "merge_refused", evidence=_tail(err, 200))
    rc, out, _ = _git(ctx, "rev-parse", "HEAD")
    verified = rc == 0 and out.strip() == ctx.to_sha
    if verified:
        rc2, out2, _ = _git(ctx, "status", "--porcelain")
        verified = rc2 == 0 and not _non_untracked_lines(out2.splitlines())
    if not verified:
        ctx.skip_reason = "postmerge_verify_failed"
        ctx.detail_parts.append("postmerge_verify_failed")
        return _rollback_path(ctx)
    # §4.4 — delta classification.
    rc, out, _ = _git(ctx, "diff", "--name-only", f"{ctx.from_sha}..{ctx.to_sha}")
    if rc != 0:
        logger.warning("box_update.delta_classify_failed", rc=rc)
        ctx.delta = DeltaClass(rust=True, pyproject=True)
    else:
        ctx.delta = classify_delta(out.splitlines())
    # §4 step 4 — pip refresh + wheel rebuilds.
    if ctx.delta.pyproject:
        ctx.forward_pip_invoked = True
        if _pip_install(ctx) != 0:
            # pip failure ⇒ treated exactly like a required-wheel build failure.
            ctx.detail_parts.append("forward_pip_failed")
            return _rollback_path(ctx)
    if ctx.delta.rust and not _build_wheels(ctx, WHEEL_SET):
        return _rollback_path(ctx)
    # §4.5 step 5 — the assert-green gate (always runs, python-only deltas too).
    ctx.gate = _run_gate(ctx)
    if ctx.gate == "ok":
        return _finish(ctx, BoxUpdateStatus.UPDATED)
    ctx.rollback_from_gate = True
    return _rollback_path(ctx)


def _up_to_date_path(ctx: _Ctx) -> BoxUpdateOutcome:
    """G4a — UP_TO_DATE still runs the gate; a non-green verdict self-heals (§7)."""
    ctx.gate = _run_gate(ctx)
    if ctx.gate == "ok":
        return _finish(ctx, BoxUpdateStatus.UP_TO_DATE)
    # Self-heal: rebuild as if the delta were rust=True, ride the full machinery.
    ctx.detail_parts.append(f"self_heal: gate={ctx.gate}")
    ctx.delta = DeltaClass(rust=True, pyproject=False)
    if _build_wheels(ctx, WHEEL_SET):
        ctx.gate = _run_gate(ctx)
        if ctx.gate == "ok":
            return _finish(ctx, BoxUpdateStatus.UP_TO_DATE)
    # Rebuild failure / still-failing gate ⇒ the §4.6 arm, tree-move VACUOUS.
    ctx.rollback_from_gate = True
    return _rollback_path(ctx, vacuous=True)


# ── re-exec (§6) + state adoption ────────────────────────────────────────
def maybe_reexec(
    outcome: BoxUpdateOutcome,
    *,
    argv: Sequence[str],
    environ: MutableMapping[str, str],
) -> None:
    """Re-exec ONCE after a fully verified update (§6 shape (b)).

    ``status == UPDATED`` and sentinel ABSENT → stamp the outcome JSON into
    ``BRUTEX_BOX_UPDATE_STATE`` and ``os.execv`` the SAME plain argv — never
    returns on success.  Sentinel present → no-op (loop-proof, the
    ``BRUTEX_SWEEP_DETACHED`` #1712 precedent); non-UPDATED → no-op.

    CALLER INVARIANT: the nightly single-flight flock must be FULLY RELEASED
    (``LOCK_UN`` + fd close executed) strictly BEFORE this call — ``os.execv``
    never returns, so no context manager can release it "after".
    """
    if outcome.status is not BoxUpdateStatus.UPDATED:
        return
    if BOX_UPDATE_STATE_ENV in environ:
        return
    environ[BOX_UPDATE_STATE_ENV] = json.dumps(outcome.to_dict())
    os.execv(  # noqa: S606  # nosec B606 — fixed interpreter + plain argv, no shell
        sys.executable, [sys.executable, "-m", "brutex", *argv]
    )


def _wheel_from_dict(raw: object) -> WheelBuildResult:
    """Parse one carried wheel dict (raises on shape errors — caught by adopt_state)."""
    if not isinstance(raw, dict):
        raise ValueError(f"wheel entry is not a dict: {raw!r}")
    digest = raw.get("digest_ok")
    if digest is not None and not isinstance(digest, bool):
        raise ValueError("digest_ok must be bool|None")
    return WheelBuildResult(
        name=str(raw["name"]),
        crate_dir=str(raw["crate_dir"]),
        required=bool(raw["required"]),
        result=str(raw["result"]),
        digest_ok=digest,
        duration_s=float(raw["duration_s"]),
        detail=str(raw["detail"]),
    )


def adopt_state(env: MutableMapping[str, str]) -> BoxUpdateOutcome | None:
    """Parse + CONSUME the re-exec carrier (§6 step 2; sentinel deleted here).

    ``None`` = sentinel absent (we are NOT the re-exec'd child).  A carried
    outcome adopts ``reexec=True`` + ``running_sha=to_sha``; corrupt JSON
    returns an ERROR-status outcome naming ``state_unparseable`` — never
    raises, and sentinel presence stays the loop brake either way.
    """
    raw = env.pop(BOX_UPDATE_STATE_ENV, None)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("state payload is not an object")
        wheels = tuple(_wheel_from_dict(w) for w in payload.get("wheels", []))
        to_sha = payload.get("to_sha")
        alarm_sig = payload.get("alarm_signature")
        return BoxUpdateOutcome(
            status=BoxUpdateStatus(str(payload["status"])),
            from_sha=None if payload.get("from_sha") is None else str(payload["from_sha"]),
            to_sha=None if to_sha is None else str(to_sha),
            running_sha=None if to_sha is None else str(to_sha),
            skip_reason=(
                None if payload.get("skip_reason") is None else str(payload["skip_reason"])
            ),
            delta_rust=bool(payload["delta_rust"]),
            delta_pyproject=bool(payload["delta_pyproject"]),
            wheels=wheels,
            gate=str(payload["gate"]),
            repair=str(payload["repair"]),
            pip_restore=str(payload["pip_restore"]),
            lock_restored=tuple(str(p) for p in payload.get("lock_restored", [])),
            quarantined=tuple(str(p) for p in payload.get("quarantined", [])),
            alarm_signature=None if alarm_sig is None else str(alarm_sig),
            alarm_repeats=int(payload["alarm_repeats"]),
            reexec=True,
            duration_s=float(payload["duration_s"]),
            detail=str(payload["detail"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("box_update.state_unparseable", error=repr(str(exc)))
        return BoxUpdateOutcome(
            status=BoxUpdateStatus.ERROR,
            from_sha=None,
            to_sha=None,
            running_sha=None,
            skip_reason=None,
            delta_rust=False,
            delta_pyproject=False,
            wheels=(),
            gate="skipped",
            repair="not_needed",
            pip_restore="not_needed",
            lock_restored=(),
            quarantined=(),
            alarm_signature=None,
            alarm_repeats=0,
            reexec=True,
            duration_s=0.0,
            detail="state_unparseable",
        )
