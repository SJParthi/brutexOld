"""Backup env-config leaf — the ONE place the ``BRUTEX_BACKUP_*`` surface lives.

DEC-BACKUP-OFFBOX-001: every knob of the off-box restic backup system
(:mod:`brutex.backup`) is named + defaulted HERE (the
:mod:`brutex.config.hardware_envelope` env-leaf style — ``*_ENV`` constants +
ONE pure resolver with an injectable ``env`` mapping), so the backup runner,
the dead-man checker, the restore drill, and the CLI all resolve the SAME
config in ONE place (CLAUDE.md §11 COMMON).

Resolution law (fail-safe, never fail-crash):

* :func:`resolve_backup_env` NEVER raises for a missing repo / password source
  — ``brutex backup run`` / ``restore-drill`` enforce their own requirements at
  entry, while ``check`` / ``status`` must keep working with a broken backup
  env (the dead-man keeps firing even when ``BRUTEX_BACKUP_REPO`` itself is
  misconfigured).
* A malformed numeric value falls back to its default LOUDLY (one structlog
  WARN — the :func:`brutex.adapters.empty_circuit_breaker.resolve_int_env`
  fail-safe idiom, never a silent misconfig; Golden Rule #1). "Malformed"
  INCLUDES a non-finite float (``nan``/``inf`` — which would silently mute the
  dead-man: ``elapsed > nan`` is always False) and, for the hour/deadline
  knobs, a non-positive one (round-8 MINOR). A ``< 1`` retention keep count
  likewise falls back LOUDLY (round-10 MINOR — restic treats ``--keep-* 0``
  as no-keep, silently disabling that retention tier on the one
  data-destroying verb, ``forget --prune``).
* BOTH password sources set is ambiguous (F6): both fields resolve to ``None``
  plus ONE ``backup.config.ambiguous_password_source`` WARN, so run/drill then
  refuse with ``EXIT_CONFIG``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.config.backup_env_rust import backupenv_rust_enabled, route_verify_subset
from brutex.config.paths import brutex_home

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOG = structlog.get_logger(__name__)

#: restic repo URL, e.g. ``s3:s3.ap-south-1.amazonaws.com/<bucket>/brutex``. No
#: default. REQUIRED by run/restore-drill; NOT required by check/status. Never
#: embed credentials in the URL (S3/B2 creds ride ``~/.brutex/.env`` → child
#: env passthrough).
BACKUP_REPO_ENV: Final[str] = "BRUTEX_BACKUP_REPO"

#: Path of a 0600 file holding the restic repo password (no default).
BACKUP_PASSWORD_FILE_ENV: Final[str] = "BRUTEX_BACKUP_PASSWORD_FILE"  # noqa: S105 — env var NAME, not a secret

#: Command printing the restic repo password (e.g. macOS keychain
#: ``security find-generic-password -s brutex-restic -w``). EXACTLY ONE of
#: FILE/COMMAND must be set for run/drill; both or neither → ``EXIT_CONFIG``.
BACKUP_PASSWORD_COMMAND_ENV: Final[str] = "BRUTEX_BACKUP_PASSWORD_COMMAND"  # noqa: S105 — env var NAME, not a secret

#: The restic binary to invoke (default ``restic`` — resolved on ``PATH``).
BACKUP_RESTIC_BIN_ENV: Final[str] = "BRUTEX_BACKUP_RESTIC_BIN"

#: State directory (default ``~/.brutex/backup``) — holds ``state.json``,
#: ``deadman.json``, ``backup.lock``, ``canary/``, ``drill-tmp/``.
BACKUP_STATE_DIR_ENV: Final[str] = "BRUTEX_BACKUP_STATE_DIR"

#: Dead-man freshness threshold in hours (default 30 = nightly + 6 h grace).
BACKUP_DEADMAN_HOURS_ENV: Final[str] = "BRUTEX_BACKUP_DEADMAN_HOURS"

#: Cross-run re-alert bound in hours (default 6 → ≤4 CRITICAL pages/day while
#: broken, never zero).
BACKUP_REALERT_HOURS_ENV: Final[str] = "BRUTEX_BACKUP_REALERT_HOURS"

#: Tolerated future-stamp clock skew in seconds (default 300).
BACKUP_CLOCK_SKEW_TOLERANCE_S_ENV: Final[str] = "BRUTEX_BACKUP_CLOCK_SKEW_TOLERANCE_S"

#: RUNNING→HUNG boundary in hours for a marked-running backup (default 48;
#: F17b — a legitimate multi-day 8 TB seed must not page).
BACKUP_MAX_RUNTIME_HOURS_ENV: Final[str] = "BRUTEX_BACKUP_MAX_RUNTIME_HOURS"

#: Subprocess wall-clock deadline in seconds. Default unset → derived as
#: ``max_runtime_hours × 3600`` (one knob, two guards — F17a belt to F17b braces).
BACKUP_TIMEOUT_S_ENV: Final[str] = "BRUTEX_BACKUP_TIMEOUT_S"

#: Retention: ``restic forget --keep-daily`` (default 14; ``< 1`` → default +
#: WARN — restic treats 0 as no-keep, round-10 MINOR).
BACKUP_KEEP_DAILY_ENV: Final[str] = "BRUTEX_BACKUP_KEEP_DAILY"

#: Retention: ``restic forget --keep-weekly`` (default 8; ``< 1`` → default +
#: WARN).
BACKUP_KEEP_WEEKLY_ENV: Final[str] = "BRUTEX_BACKUP_KEEP_WEEKLY"

#: Retention: ``restic forget --keep-monthly`` (default 12; ``< 1`` → default
#: + WARN).
BACKUP_KEEP_MONTHLY_ENV: Final[str] = "BRUTEX_BACKUP_KEEP_MONTHLY"

#: Python ``weekday()`` (0=Mon..6=Sun, judged in IST — the box TZ law) on which
#: the nightly ``forget`` adds ``--prune`` (default 6 = Sunday).
BACKUP_PRUNE_WEEKDAY_ENV: Final[str] = "BRUTEX_BACKUP_PRUNE_WEEKDAY"

#: Read-data verification tier: ``auto`` (default) → NIGHTLY rotating
#: ``{cursor % 56 + 1}/56`` (the state cursor advances only after a SUCCESSFUL
#: subset check — no bucket ever skipped; full repo read ≈ every 8 weeks);
#: ``none`` → structure check only; anything else is SHAPE-VALIDATED against
#: the restic ``--read-data-subset`` forms (``n/t`` positive ints with
#: ``n ≤ t ≤ 256``, a ``x%`` percentage in (0, 100], or a positive
#: ``<int><k|m|g|t|b>`` size) then passed to restic verbatim (``2%``,
#: ``1/10``, …). An invalid shape falls back to ``auto`` LOUDLY (round-7
#: MINOR: restic rejects a malformed flag with the SAME rc=1 as genuine
#: corruption, so an unvalidated typo would page repo-corrupt every night).
BACKUP_VERIFY_SUBSET_ENV: Final[str] = "BRUTEX_BACKUP_VERIFY_SUBSET"

#: External dead-man ping URL (healthchecks.io); default unset → ping lane OFF.
BACKUP_HEALTHCHECK_URL_ENV: Final[str] = "BRUTEX_BACKUP_HEALTHCHECK_URL"

#: Restore-drill stratified sample size (default 16 files + the 1 MiB canary).
BACKUP_DRILL_SAMPLE_ENV: Final[str] = "BRUTEX_BACKUP_DRILL_SAMPLE"

#: ``:``-separated globs appended to the fixed exclude set (default
#: ``logs:dedup-cache``). The DYNAMIC mechanism: a new scratch dir needs no
#: code change; an operator who WANTS lake logs backed up sets it to ``""``.
BACKUP_EXTRA_EXCLUDES_ENV: Final[str] = "BRUTEX_BACKUP_EXTRA_EXCLUDES"

#: Rotating read-data-subset bucket count — a constant, deliberately NOT an env
#: var (fewest knobs; §15 R1 of the design).
VERIFY_SUBSET_BUCKETS: Final[int] = 56

#: The daily auto-deep cross-check gate inside the hourly checker (F21).
DEEP_CHECK_INTERVAL_HOURS: Final[float] = 24.0

_DEFAULT_RESTIC_BIN: Final[str] = "restic"
_DEFAULT_DEADMAN_HOURS: Final[float] = 30.0
_DEFAULT_REALERT_HOURS: Final[float] = 6.0
_DEFAULT_CLOCK_SKEW_TOLERANCE_S: Final[float] = 300.0
_DEFAULT_MAX_RUNTIME_HOURS: Final[float] = 48.0
_DEFAULT_KEEP_DAILY: Final[int] = 14
_DEFAULT_KEEP_WEEKLY: Final[int] = 8
_DEFAULT_KEEP_MONTHLY: Final[int] = 12
_DEFAULT_PRUNE_WEEKDAY: Final[int] = 6
_DEFAULT_VERIFY_SUBSET: Final[str] = "auto"
_DEFAULT_DRILL_SAMPLE: Final[int] = 16
_DEFAULT_EXTRA_EXCLUDES: Final[str] = "logs:dedup-cache"

#: Valid Python ``weekday()`` range for the prune gate (0=Mon .. 6=Sun).
_MAX_WEEKDAY: Final[int] = 6

#: restic ``--read-data-subset`` ``n/t`` form (checkFlags: positive ints,
#: ``n ≤ t``, ``t ≤ 256`` — empirically probed against restic 0.16.4).
_SUBSET_NT_RE: Final[re.Pattern[str]] = re.compile(r"^([0-9]+)/([0-9]+)$")

#: restic ``--read-data-subset`` ``x%`` form — a plain decimal percentage
#: (restic's Go ``ParseFloat`` also accepts exotic spellings like ``2e1%``;
#: those fall back to ``auto`` LOUDLY — the conservative direction: a false
#: fallback costs one WARN + the safe default, a false pass costs a fake
#: repo-corrupt CRITICAL).
_SUBSET_PERCENT_RE: Final[re.Pattern[str]] = re.compile(r"^([0-9]+(?:\.[0-9]*)?|\.[0-9]+)%$")

#: restic ``--read-data-subset`` size form — a positive integer + ONE suffix
#: (``ui.ParseBytes``; a bare integer with NO suffix is restic-INVALID — it
#: parses as a 1-element ``n/t`` slice and is refused, probed on 0.16.4).
_SUBSET_SIZE_RE: Final[re.Pattern[str]] = re.compile(r"^([0-9]+)([bBkKmMgGtT])$")

#: restic checkFlags bound: ``--read-data-subset=n/t`` requires ``t ≤ 256``.
_SUBSET_MAX_BUCKETS: Final[int] = 256

#: restic checkFlags bound: ``--read-data-subset=x%`` requires ``0 < x ≤ 100``.
_SUBSET_MAX_PERCENT: Final[float] = 100.0

#: ``ui.ParseBytes`` byte multipliers per size suffix (binary, per restic).
_SUBSET_SIZE_MULTIPLIERS: Final[dict[str, int]] = {
    "b": 1,
    "k": 2**10,
    "m": 2**20,
    "g": 2**30,
    "t": 2**40,
}

#: restic size-form overflow bound — Go ``int64`` max. ``ui.ParseBytes``
#: rejects a byte total above it with the SAME rc=1 as genuine corruption
#: (round-9 MINOR, empirically probed on 0.16.4: ``8388607t`` → rc=0 while
#: ``8388608t`` — exactly 2^63 bytes — ``9999999999t`` and
#: ``99999999999999999999b`` → rc=1 flag-invalid).
_SUBSET_MAX_SIZE_BYTES: Final[int] = 2**63 - 1


def _valid_verify_subset(value: str) -> bool:
    """True iff *value* is a shape restic's ``--read-data-subset`` accepts.

    Mirrors restic's own flag validation (``checkFlags``, empirically probed
    against the pinned restic 0.16.4): ``n/t`` with positive ints and
    ``n ≤ t ≤ 256``, ``x%`` with ``0 < x ≤ 100``, or a positive
    ``<int><b|k|m|g|t>`` size whose byte total fits Go ``int64``
    (``ui.ParseBytes`` overflows above ``2^63 − 1`` with the SAME rc=1 —
    round-9 MINOR completing the round-7 fix at the overflow boundary).
    Restic rejects a malformed flag with rc=1 BEFORE reading any repo data —
    indistinguishable in rc from genuine corruption — so an invalid
    passthrough must never reach the nightly verify (round-7 MINOR; the
    F3-not-F11 honesty law of the ``binary_missing`` arm, one arm over).

    Rust seam (``DEC-CONFIG-RUST-LEAVES-001``, MIGRATION.md #1943 —
    milestone 3): when — and ONLY when — ``BRUTEX_CORE_RUST_BACKUPENV``
    is set truthy, the shape law routes through the default-ON,
    burn-in-checked, fail-closed :mod:`brutex.config.backup_env_rust`
    facade to the ``rust/brutex-core`` crate; flag-OFF is the verbatim
    ``_valid_verify_subset_python`` oracle — behavior-identical AND
    crate-untouched (bomb-crate spy-pinned). Honesty banner (ADR-0020,
    mandatory verbatim): expected payoff ≈ 0 — conversion is for
    uniformity, not speed.
    """
    if not backupenv_rust_enabled():
        return _valid_verify_subset_python(value)
    return route_verify_subset(value, _valid_verify_subset_python)


def _valid_verify_subset_python(value: str) -> bool:
    """The verbatim :func:`_valid_verify_subset` body — the LIVE oracle.

    Token-for-token the pre-seam body (DEC-CONFIG-RUST-LEAVES-001); every
    flag-OFF / ineligible / degraded / burn-in call serves this.
    """
    nt = _SUBSET_NT_RE.match(value)
    if nt is not None:
        return 1 <= int(nt.group(1)) <= int(nt.group(2)) <= _SUBSET_MAX_BUCKETS
    pct = _SUBSET_PERCENT_RE.match(value)
    if pct is not None:
        return 0.0 < float(pct.group(1)) <= _SUBSET_MAX_PERCENT
    size = _SUBSET_SIZE_RE.match(value)
    if size is not None:
        total_bytes = int(size.group(1)) * _SUBSET_SIZE_MULTIPLIERS[size.group(2).lower()]
        return 0 < total_bytes <= _SUBSET_MAX_SIZE_BYTES
    return False


@dataclass(frozen=True, slots=True)
class BackupEnvConfig:
    """The fully-resolved ``BRUTEX_BACKUP_*`` configuration (immutable).

    Attributes
    ----------
    repo
        restic repo URL, or ``None`` when unset (run/drill refuse; check/status
        proceed — the shallow dead-man needs no repo config).
    password_file
        Path of the restic password file, or ``None``.
    password_command
        Command printing the restic password, or ``None``. When BOTH sources
        were set, BOTH fields are ``None`` (ambiguous — F6).
    restic_bin
        The restic binary name/path.
    state_dir
        Directory holding ``state.json`` / ``deadman.json`` / ``backup.lock`` /
        ``canary/`` / ``drill-tmp/``.
    deadman_hours
        Dead-man freshness threshold (hours).
    realert_hours
        Cross-run re-alert bound (hours).
    clock_skew_tolerance_s
        Future-stamp tolerance (seconds) before CLOCK_SKEW alarms.
    max_runtime_hours
        RUNNING→HUNG boundary (hours) for a marked-running backup.
    timeout_s
        Resolved subprocess deadline (env override or derived
        ``max_runtime_hours × 3600``).
    keep_daily, keep_weekly, keep_monthly
        ``restic forget`` retention counts; a ``< 1`` env value falls back to
        the default + WARN (restic treats ``--keep-* 0`` as no-keep — a
        silent retention disable, round-10 MINOR).
    prune_weekday
        Python ``weekday()`` (0=Mon..6=Sun) on which ``forget`` prunes;
        an out-of-range env value falls back to the default 6 + WARN.
    verify_subset
        ``"auto"`` | ``"none"`` | a shape-validated restic
        ``--read-data-subset`` value passed verbatim; an invalid shape falls
        back to ``auto`` + WARN (a restic flag-validation failure is rc=1 —
        the same rc as genuine corruption — so it must never reach verify).
    healthcheck_url
        External dead-man ping URL, or ``None`` (lane off).
    drill_sample
        Restore-drill stratified sample size.
    extra_excludes
        Operator exclude globs appended to the fixed exclude set.
    """

    repo: str | None
    password_file: Path | None
    password_command: str | None
    restic_bin: str
    state_dir: Path
    deadman_hours: float
    realert_hours: float
    clock_skew_tolerance_s: float
    max_runtime_hours: float
    timeout_s: float
    keep_daily: int
    keep_weekly: int
    keep_monthly: int
    prune_weekday: int
    verify_subset: str
    healthcheck_url: str | None
    drill_sample: int
    extra_excludes: tuple[str, ...]


def _optional(env: Mapping[str, str], name: str) -> str | None:
    """Return the stripped value of *name*, or ``None`` when unset/blank."""
    raw = env.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _float_env(
    env: Mapping[str, str], name: str, default: float, *, require_positive: bool = False
) -> float:
    """Read a float env override; unset/blank → *default*; malformed → *default* + WARN.

    Non-finite values (``nan`` / ``inf`` / ``-inf``) parse cleanly through
    ``float()`` yet are ALWAYS rejected here (round-8 MINOR): a non-finite
    threshold SILENTLY DISABLES the dead-man — ``elapsed > nan`` is False for
    a year-stale backup (verdict FRESH, exit 0, plain success pings), and
    ``max_runtime_hours=inf`` (a plausible "no limit" seed-raise value) mutes
    the HUNG lane AND unbounds every restic subprocess deadline. The exact
    inversion of the "silence = alarm" law, so it falls back LOUDLY like any
    other malformed numeric. With ``require_positive`` (the hour/deadline
    knobs, where a ≤0 value is equally nonsensical) a non-positive value is
    rejected the same way.
    """
    raw = _optional(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        _LOG.warning("backup.config.malformed_numeric", env_var=name, value=raw, default=default)
        return default
    if not math.isfinite(value) or (require_positive and value <= 0.0):
        _LOG.warning("backup.config.malformed_numeric", env_var=name, value=raw, default=default)
        return default
    return value


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    """Read an int env override; unset/blank → *default*; malformed → *default* + WARN."""
    raw = _optional(env, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _LOG.warning("backup.config.malformed_numeric", env_var=name, value=raw, default=default)
        return default


def _keep_env(env: Mapping[str, str], name: str, default: int, *, knob: str) -> int:
    """Read a ``restic forget --keep-*`` retention count; ``< 1`` → *default* + WARN.

    Round-10 MINOR: a 0/negative keep count parses as a VALID int — the
    malformed-numeric loud-fallback never fires — yet it flows verbatim into
    ``restic forget --keep-daily/--keep-weekly/--keep-monthly``
    (:meth:`brutex.backup.restic_runner.ResticRunner.forget`) where restic
    treats ``0`` as "keep NONE for this bucket", silently DISABLING that
    retention tier with rc=0 and no warning — on the ONE verb
    (``forget --prune``) that can destroy repository data. Same law as the
    prune_weekday / drill_sample guards: a nonsensical value falls back to
    its default LOUDLY (``backup.config.<knob>_out_of_range`` naming the env
    var), never a quiet no-keep policy.
    """
    value = _int_env(env, name, default)
    if value < 1:
        _LOG.warning(
            f"backup.config.{knob}_out_of_range",
            env_var=name,
            value=value,
            default=default,
        )
        return default
    return value


def _split_excludes(raw: str) -> tuple[str, ...]:
    """Split the ``:``-separated exclude list, dropping blank segments."""
    return tuple(part.strip() for part in raw.split(":") if part.strip())


def _expand_user_path(raw: str, env_var: str) -> Path:
    """``Path(raw).expanduser()`` made TOTAL (the resolver's never-raises law).

    ``Path.expanduser()`` raises ``RuntimeError`` (Python ≥ 3.12; older
    pathlib raised ``KeyError``) for a ``~user`` naming a nonexistent user —
    a plausible operator typo that must NOT crash the hourly dead-man
    ``check``/``status`` (graft A). The unexpandable value keeps the RAW,
    unexpanded path with ONE ``backup.config.malformed_path`` WARN — the
    malformed-numeric loud-fallback idiom; run/drill then surface the bogus
    path through restic itself (F4), never a traceback here.
    """
    path = Path(raw)
    try:
        return path.expanduser()
    except (RuntimeError, KeyError):
        _LOG.warning("backup.config.malformed_path", env_var=env_var, value=raw)
        return path


def resolve_backup_env(
    env: Mapping[str, str] | None = None, *, home: Path | None = None
) -> BackupEnvConfig:
    """Resolve the full backup configuration from *env* (pure, total, never raises).

    Parameters
    ----------
    env
        Environment mapping (defaults to :data:`os.environ` — the
        :mod:`brutex.config.paths` idiom).
    home
        Override for the user's home directory (tests inject a tmp path); used
        only for the default ``state_dir`` under :func:`brutex.config.paths.\
brutex_home`.

    Returns
    -------
    BackupEnvConfig
        The resolved, immutable configuration. Missing repo/password sources
        resolve to ``None`` (run/drill enforce their own requirements at
        entry); a malformed numeric falls back to its default with ONE WARN;
        BOTH password sources set resolves both to ``None`` with ONE
        ``backup.config.ambiguous_password_source`` WARN (F6).
    """
    if env is None:
        import os

        env = os.environ

    repo = _optional(env, BACKUP_REPO_ENV)
    password_file_raw = _optional(env, BACKUP_PASSWORD_FILE_ENV)
    password_command = _optional(env, BACKUP_PASSWORD_COMMAND_ENV)
    password_file = (
        _expand_user_path(password_file_raw, BACKUP_PASSWORD_FILE_ENV)
        if password_file_raw
        else None
    )
    if password_file is not None and password_command is not None:
        _LOG.warning(
            "backup.config.ambiguous_password_source",
            detail=(
                f"both {BACKUP_PASSWORD_FILE_ENV} and {BACKUP_PASSWORD_COMMAND_ENV} are set; "
                "exactly one is required — run/restore-drill will refuse (EXIT_CONFIG)"
            ),
        )
        password_file = None
        password_command = None

    state_dir_raw = _optional(env, BACKUP_STATE_DIR_ENV)
    state_dir = (
        _expand_user_path(state_dir_raw, BACKUP_STATE_DIR_ENV)
        if state_dir_raw
        else brutex_home(home=home) / "backup"
    )

    max_runtime_hours = _float_env(
        env, BACKUP_MAX_RUNTIME_HOURS_ENV, _DEFAULT_MAX_RUNTIME_HOURS, require_positive=True
    )
    timeout_s = _float_env(
        env, BACKUP_TIMEOUT_S_ENV, max_runtime_hours * 3600.0, require_positive=True
    )

    prune_weekday = _int_env(env, BACKUP_PRUNE_WEEKDAY_ENV, _DEFAULT_PRUNE_WEEKDAY)
    if not 0 <= prune_weekday <= _MAX_WEEKDAY:
        _LOG.warning(
            "backup.config.prune_weekday_out_of_range",
            value=prune_weekday,
            default=_DEFAULT_PRUNE_WEEKDAY,
        )
        prune_weekday = _DEFAULT_PRUNE_WEEKDAY

    drill_sample = _int_env(env, BACKUP_DRILL_SAMPLE_ENV, _DEFAULT_DRILL_SAMPLE)
    if drill_sample < 1:
        # Round-6 NIT: a nonsensical (≤0) sample count parses as a VALID int,
        # so the malformed-numeric loud-fallback never fires — yet downstream
        # it silently degrades the monthly drill to canary-only (the
        # stratified byte-verification loop runs zero times) while the drill
        # still exits 0 with backup.drill.ok. Same law as the prune_weekday
        # guard above: a nonsensical value falls back to its default LOUDLY.
        _LOG.warning(
            "backup.config.drill_sample_out_of_range",
            value=drill_sample,
            default=_DEFAULT_DRILL_SAMPLE,
        )
        drill_sample = _DEFAULT_DRILL_SAMPLE

    verify_subset = _optional(env, BACKUP_VERIFY_SUBSET_ENV) or _DEFAULT_VERIFY_SUBSET
    if verify_subset not in ("auto", "none") and not _valid_verify_subset(verify_subset):
        # Round-7 MINOR: restic refuses a malformed --read-data-subset with
        # rc=1 on FLAG VALIDATION — reading ZERO repo data yet
        # indistinguishable in rc from genuine corruption — so an
        # unvalidated typo would page repo-corrupt (exit 6 + the
        # restore-from-older / re-seed CRITICAL) every night, with success
        # never stamped → a dead-man OVERDUE cascade. Same law as the
        # prune_weekday / drill_sample guards above: fall back LOUDLY.
        _LOG.warning(
            "backup.config.verify_subset_invalid",
            value=verify_subset,
            default=_DEFAULT_VERIFY_SUBSET,
        )
        verify_subset = _DEFAULT_VERIFY_SUBSET

    extra_raw = env.get(BACKUP_EXTRA_EXCLUDES_ENV)
    extra_excludes = _split_excludes(
        extra_raw if extra_raw is not None else _DEFAULT_EXTRA_EXCLUDES
    )

    return BackupEnvConfig(
        repo=repo,
        password_file=password_file,
        password_command=password_command,
        restic_bin=_optional(env, BACKUP_RESTIC_BIN_ENV) or _DEFAULT_RESTIC_BIN,
        state_dir=state_dir,
        deadman_hours=_float_env(
            env, BACKUP_DEADMAN_HOURS_ENV, _DEFAULT_DEADMAN_HOURS, require_positive=True
        ),
        realert_hours=_float_env(
            env, BACKUP_REALERT_HOURS_ENV, _DEFAULT_REALERT_HOURS, require_positive=True
        ),
        clock_skew_tolerance_s=_float_env(
            env, BACKUP_CLOCK_SKEW_TOLERANCE_S_ENV, _DEFAULT_CLOCK_SKEW_TOLERANCE_S
        ),
        max_runtime_hours=max_runtime_hours,
        timeout_s=timeout_s,
        keep_daily=_keep_env(env, BACKUP_KEEP_DAILY_ENV, _DEFAULT_KEEP_DAILY, knob="keep_daily"),
        keep_weekly=_keep_env(
            env, BACKUP_KEEP_WEEKLY_ENV, _DEFAULT_KEEP_WEEKLY, knob="keep_weekly"
        ),
        keep_monthly=_keep_env(
            env, BACKUP_KEEP_MONTHLY_ENV, _DEFAULT_KEEP_MONTHLY, knob="keep_monthly"
        ),
        prune_weekday=prune_weekday,
        verify_subset=verify_subset,
        healthcheck_url=_optional(env, BACKUP_HEALTHCHECK_URL_ENV),
        drill_sample=drill_sample,
        extra_excludes=extra_excludes,
    )


__all__ = [
    "BACKUP_CLOCK_SKEW_TOLERANCE_S_ENV",
    "BACKUP_DEADMAN_HOURS_ENV",
    "BACKUP_DRILL_SAMPLE_ENV",
    "BACKUP_EXTRA_EXCLUDES_ENV",
    "BACKUP_HEALTHCHECK_URL_ENV",
    "BACKUP_KEEP_DAILY_ENV",
    "BACKUP_KEEP_MONTHLY_ENV",
    "BACKUP_KEEP_WEEKLY_ENV",
    "BACKUP_MAX_RUNTIME_HOURS_ENV",
    "BACKUP_PASSWORD_COMMAND_ENV",
    "BACKUP_PASSWORD_FILE_ENV",
    "BACKUP_PRUNE_WEEKDAY_ENV",
    "BACKUP_REALERT_HOURS_ENV",
    "BACKUP_REPO_ENV",
    "BACKUP_RESTIC_BIN_ENV",
    "BACKUP_STATE_DIR_ENV",
    "BACKUP_TIMEOUT_S_ENV",
    "BACKUP_VERIFY_SUBSET_ENV",
    "DEEP_CHECK_INTERVAL_HOURS",
    "VERIFY_SUBSET_BUCKETS",
    "BackupEnvConfig",
    "resolve_backup_env",
]
