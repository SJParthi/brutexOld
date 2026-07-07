"""The nightly backup-flow orchestrator (``brutex backup run``) — §6 of the design.

Pure logic over injected runner/alerter/clock/state-io. The order is
LOAD-BEARING and pinned by tests:

* preflight (F1/F2) runs BEFORE any restic verb — critically before
  ``forget --prune``, the one path by which the backup system itself could
  destroy data (a phantom-mount skeleton lake must never age real snapshots
  out);
* the success stamp is written BEFORE maintenance (F23 — a retention failure
  never retracts data-success: the dead-man must not fire when the data IS
  safely off-box);
* verify (``restic check`` + the rotating read-data subset) sits INSIDE the
  success contract (F11 — an unverifiable snapshot must not reset the
  dead-man).

Every failure path maps to the deterministic triple: exit code + ``backup.*``
structlog event + alert (or an explicit no-alert rationale — the F15 busy
path is benign).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final

import structlog

from brutex.backup.errors import (
    EXIT_BUSY,
    EXIT_CONFIG,
    EXIT_CORRUPTION,
    EXIT_MAINTENANCE,
    EXIT_OK,
    EXIT_PREFLIGHT,
    EXIT_RESTIC,
    BackupBusyError,
    BackupError,
    BackupStateError,
)
from brutex.backup.plan import backup_singleflight_lock, build_backup_plan, preflight_lake
from brutex.backup.restic_runner import (
    BACKUP_EXIT_PARTIAL,
    build_backup_argv,
    default_runner_factory,
    parse_backup_summary_snapshot_id,
)
from brutex.backup.state import (
    BackupState,
    empty_backup_state,
    load_state,
    save_state,
    state_path,
)
from brutex.config.backup_env import VERIFY_SUBSET_BUCKETS
from brutex.config.paths import resolve_lake_root
from brutex.notifications.sweep_alerts import SweepAlerter

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from brutex.backup.deadman import Clock
    from brutex.backup.plan import BackupPlan, PreflightVerdict
    from brutex.backup.restic_runner import ResticRunner
    from brutex.config.backup_env import BackupEnvConfig

_LOG = structlog.get_logger(__name__)

#: IST — the box TZ law (fixed +05:30, no DST); the weekly prune gate is
#: judged in IST so "Sunday" means the operator's Sunday.
_IST: Final[timezone] = timezone(timedelta(hours=5, minutes=30))

#: Consecutive rc-3 partial runs at (and past) which the WARN escalates to a
#: CRITICAL ``partial_streak`` alert (F8 — a systemic reader problem).
PARTIAL_STREAK_CRITICAL: Final[int] = 3

#: File-mode bits that make a password file group/other-readable (F19 WARN).
_PERMISSIVE_MODE_BITS: Final[int] = 0o077


def _default_utc_clock() -> datetime:
    """Return the current aware-UTC time (the production clock)."""
    from datetime import UTC

    return datetime.now(UTC)


def _log_would_run_plan(cfg: BackupEnvConfig, plan: BackupPlan) -> None:
    """Log the dry-run plan: includes/excludes/tags + the WOULD-run argv."""
    _LOG.info(
        "backup.run.dry_run",
        paths=[str(p) for p in plan.paths],
        excludes=list(plan.excludes),
        tags=list(plan.tags),
        pull_active=plan.pull_active,
        would_run_argv=list(
            build_backup_argv(cfg.restic_bin, plan.paths, excludes=plan.excludes, tags=plan.tags)
        ),
    )


class _RunShell:
    """One backup run's mutable shell — carries state + seams between §6 steps."""

    __slots__ = ("_alerter", "_cfg", "_clock", "_state", "_state_file")

    def __init__(
        self,
        cfg: BackupEnvConfig,
        *,
        alerter: SweepAlerter,
        clock: Clock,
        baseline: BackupState,
    ) -> None:
        self._cfg = cfg
        self._alerter = alerter
        self._clock = clock
        self._state = baseline
        self._state_file = state_path(cfg)

    @property
    def state(self) -> BackupState:
        """The current in-memory state (last saved or pending)."""
        return self._state

    @property
    def cfg(self) -> BackupEnvConfig:
        """The run's resolved backup config."""
        return self._cfg

    def save(self, state: BackupState) -> None:
        """Persist *state* (raises ``OSError`` raw — the caller maps F10)."""
        save_state(self._state_file, state)
        self._state = state

    def save_best_effort(self, state: BackupState, *, context: str) -> None:
        """Persist *state*; an ``OSError`` is logged, never masks the caller's exit."""
        try:
            self.save(state)
        except OSError as exc:
            _LOG.error("backup.state.write_failed", context=context, error_type=type(exc).__name__)

    def fail(
        self,
        reason: str,
        *,
        exit_code: int = EXIT_RESTIC,
        detail: str = "",
        repo_corrupt: bool = False,
    ) -> int:
        """The §6 FAIL path: stamp failure best-effort, alert, return *exit_code*."""
        now = self._clock()
        self.save_best_effort(
            replace(
                self._state,
                running_since=None,
                last_failure_at=now,
                last_failure_reason=reason,
                consecutive_failures=self._state.consecutive_failures + 1,
            ),
            context=f"fail:{reason}",
        )
        if repo_corrupt:
            self._alerter.backup_repo_corrupt(detail=detail or reason)
        else:
            self._alerter.backup_failed(reason=reason, detail=detail)
        return exit_code


def _password_source_ok(cfg: BackupEnvConfig) -> bool:
    """True iff exactly one password source is configured (F6)."""
    return (cfg.password_file is None) != (cfg.password_command is None)


def _warn_permissive_password_file(cfg: BackupEnvConfig) -> None:
    """WARN (proceed) when the password file is group/other-readable (F19)."""
    if cfg.password_file is None:
        return
    try:
        mode = cfg.password_file.stat().st_mode
    except OSError:
        return  # missing/unreadable file surfaces via restic itself (F4)
    if mode & _PERMISSIVE_MODE_BITS:
        _LOG.warning(
            "backup.password.permissive_mode",
            path=str(cfg.password_file),
            mode=oct(mode & 0o777),
        )


def _check_config(cfg: BackupEnvConfig, alerter: SweepAlerter) -> int | None:
    """§6 step 1 — refuse an unusable config BEFORE touching restic (F6/F19)."""
    if cfg.repo is None:
        _LOG.error("backup.config.no_repo", env_var="BRUTEX_BACKUP_REPO")
        alerter.backup_failed(reason="config", detail="BRUTEX_BACKUP_REPO is not set.")
        return EXIT_CONFIG
    if not _password_source_ok(cfg):
        # The ambiguous both-set case was already normalised (+ WARNed) to
        # None/None by resolve_backup_env — both cases land here (F6).
        _LOG.error("backup.config.no_password_source")
        alerter.backup_failed(
            reason="config",
            detail=(
                "exactly one of BRUTEX_BACKUP_PASSWORD_FILE / "
                "BRUTEX_BACKUP_PASSWORD_COMMAND must be set."
            ),
        )
        return EXIT_CONFIG
    _warn_permissive_password_file(cfg)
    return None


def _preflight_or_refuse(
    cfg: BackupEnvConfig,
    lake: Path,
    *,
    allow_empty: bool,
    alerter: SweepAlerter,
    preflight_fn: Callable[..., PreflightVerdict],
    prior: BackupState | None,
) -> PreflightVerdict | int:
    """§6 step 2 — the F1/F2 gate BEFORE any restic verb; int = refusal exit."""
    verdict = preflight_fn(lake, allow_empty=allow_empty)
    if not verdict.ok:
        event = {
            "lake_unmounted": "backup.preflight.lake_unmounted",
            "backup_set_empty": "backup.preflight.empty_set",
        }.get(verdict.reason, "backup.preflight.lake_missing")
        _LOG.error(
            event,
            path=str(lake),
            volume_parent=(
                str(verdict.identity.volume_parent)
                if verdict.identity.volume_parent is not None
                else None
            ),
            device=verdict.identity.device,
        )
        alerter.backup_failed(reason=verdict.reason, detail=f"lake root {lake}")
        return EXIT_PREFLIGHT
    if (
        prior is not None
        and prior.lake_device is not None
        and verdict.identity.device is not None
        and verdict.identity.device != prior.lake_device
    ):
        _LOG.warning(
            "backup.preflight.device_drift",
            recorded_device=prior.lake_device,
            current_device=verdict.identity.device,
        )
    return verdict


def run_backup(
    cfg: BackupEnvConfig,
    *,
    lake_root: Path | None = None,
    allow_empty: bool = False,
    dry_run: bool = False,
    alerter: SweepAlerter | None = None,
    clock: Clock = _default_utc_clock,
    runner_factory: Callable[[BackupEnvConfig], ResticRunner] = default_runner_factory,
    plan_fn: Callable[..., BackupPlan] = build_backup_plan,
    preflight_fn: Callable[..., PreflightVerdict] = preflight_lake,
) -> int:
    """Run one nightly backup (§6 flow) — returns an exit code, raises nothing.

    Parameters
    ----------
    cfg
        The resolved backup config.
    lake_root
        Lake root override; ``None`` → :func:`brutex.config.paths.resolve_lake_root`.
    allow_empty
        First-install escape for the F2 empty-set refusal (logged loudly).
    dry_run
        Config + preflight + plan render only: zero restic calls, zero state
        writes, no lock.
    alerter
        Injectable alerter; ``None`` → :class:`SweepAlerter`.
    clock
        Injectable UTC clock.
    runner_factory
        Injectable :class:`~brutex.backup.restic_runner.ResticRunner` factory.
    plan_fn, preflight_fn
        Injectable plan/preflight seams (tests drive every branch).

    Returns
    -------
    int
        The deterministic §1 exit code (0/2/3/4/5/6/7).
    """
    active_alerter = alerter if alerter is not None else SweepAlerter()
    lake = lake_root if lake_root is not None else resolve_lake_root()
    _LOG.info("backup.run.start", lake_root=str(lake), dry_run=dry_run)

    config_exit = _check_config(cfg, active_alerter)
    if config_exit is not None:
        return config_exit

    prior: BackupState | None
    try:
        prior = load_state(state_path(cfg))
    except BackupStateError as exc:
        # The RUNNER legitimately rewrites its own file every run; a corrupt
        # baseline resets counters loudly (the CHECKER never overwrites — F13).
        _LOG.warning("backup.run.state_corrupt_reset", error=str(exc))
        prior = None

    verdict = _preflight_or_refuse(
        cfg,
        lake,
        allow_empty=allow_empty,
        alerter=active_alerter,
        preflight_fn=preflight_fn,
        prior=prior,
    )
    if isinstance(verdict, int):
        return verdict

    if dry_run:
        plan = plan_fn(cfg, lake)
        _log_would_run_plan(cfg, plan)
        return EXIT_OK

    try:
        with backup_singleflight_lock(cfg.state_dir):
            return _run_locked(
                cfg,
                lake,
                verdict,
                prior=prior,
                alerter=active_alerter,
                clock=clock,
                runner_factory=runner_factory,
                plan_fn=plan_fn,
            )
    except BackupBusyError:
        _LOG.info("backup.singleflight.busy", state_dir=str(cfg.state_dir))
        return EXIT_BUSY
    except OSError as exc:
        # Lock/state-dir creation failed (e.g. ENOSPC) — the F10 direction:
        # alarm-over-silence, nothing was snapshotted or stamped.
        _LOG.error("backup.state.write_failed", context="singleflight", error=str(exc))
        active_alerter.backup_failed(reason="state_write", detail=str(exc))
        return EXIT_RESTIC


def _probe_and_init_repo(shell: _RunShell, runner: ResticRunner) -> int | None:
    """§6 step 5 — ``cat config`` → ``init`` once → stale ``unlock`` (F5/F7)."""
    probe = runner.cat_config()
    if probe.binary_missing:
        return shell.fail("restic_missing", detail="restic binary not found — install it (M1).")
    if probe.timed_out:
        return shell.fail("timeout", detail="restic cat config timed out.")
    if not probe.ok:
        init = runner.init()
        if init.binary_missing:
            return shell.fail("restic_missing", detail="restic binary not found — install it (M1).")
        if init.timed_out:
            return shell.fail("timeout", detail="restic init timed out.")
        if not init.ok:
            # Wrong password vs corrupt repo config is indistinguishable
            # on-box without parsing localized stderr — deliberately not
            # attempted (F7: no fragile string matching for control flow).
            _LOG.error(
                "backup.restic.failed",
                op="init",
                rc=init.returncode,
                stderr_tail=init.stderr_tail,
            )
            return shell.fail("auth_or_repo", detail="restic init/cat-config failed.")
        _LOG.info("backup.repo.initialized")
    unlock = runner.unlock()
    if not unlock.ok:
        _LOG.warning("backup.unlock.failed", rc=unlock.returncode, stderr_tail=unlock.stderr_tail)
    return None


def _backup_and_parse(
    shell: _RunShell, runner: ResticRunner, plan: BackupPlan
) -> tuple[str, bool] | int:
    """§6 step 7 — the snapshot itself; returns ``(snapshot_id, partial)`` or an exit."""
    inv = runner.backup(plan.paths, excludes=plan.excludes, tags=plan.tags)
    if inv.binary_missing:
        return shell.fail("restic_missing", detail="restic binary not found — install it (M1).")
    if inv.timed_out:
        _LOG.error("backup.restic.timeout", op="backup", timeout_s=shell.cfg.timeout_s)
        return shell.fail("timeout", detail="restic backup exceeded the runtime deadline.")
    partial = inv.returncode == BACKUP_EXIT_PARTIAL
    if not inv.ok and not partial:
        _LOG.error(
            "backup.restic.failed", op="backup", rc=inv.returncode, stderr_tail=inv.stderr_tail
        )
        return shell.fail(
            "remote_unreachable",
            detail=f"restic backup failed (rc={inv.returncode}).",
        )
    if partial:
        _LOG.warning("backup.run.partial", rc=inv.returncode, stderr_tail=inv.stderr_tail)
    try:
        snapshot_id = parse_backup_summary_snapshot_id(inv.stdout_full)
    except BackupError as exc:
        _LOG.error("backup.restic.failed", op="backup", rc=inv.returncode, error=str(exc))
        return shell.fail("summary_unparseable", detail=str(exc))
    return snapshot_id, partial


def _verify_repo(shell: _RunShell, runner: ResticRunner) -> tuple[bool, str] | int:
    """§6 step 8 — structure check + the read-data tier; returns (subset_passed, label)."""
    structure = runner.check()
    if structure.timed_out:
        return shell.fail("timeout", detail="restic check timed out.")
    if structure.binary_missing:
        return shell.fail("restic_missing", detail="restic binary not found — install it (M1).")
    if not structure.ok:
        _LOG.error(
            "backup.check.failed",
            subset=None,
            rc=structure.returncode,
            stderr_tail=structure.stderr_tail,
        )
        return shell.fail(
            "check_failed",
            exit_code=EXIT_CORRUPTION,
            detail=f"restic check failed (rc={structure.returncode}).",
            repo_corrupt=True,
        )
    verify_subset = shell.cfg.verify_subset
    if verify_subset == "none":
        return False, "none"
    if verify_subset == "auto":
        bucket = shell.state.verify_subset_cursor % VERIFY_SUBSET_BUCKETS + 1
        subset = f"{bucket}/{VERIFY_SUBSET_BUCKETS}"
    else:
        subset = verify_subset
    data_check = runner.check(read_data_subset=subset)
    if data_check.timed_out:
        return shell.fail("timeout", detail=f"restic check --read-data-subset={subset} timed out.")
    if not data_check.ok:
        _LOG.error(
            "backup.check.failed",
            subset=subset,
            rc=data_check.returncode,
            stderr_tail=data_check.stderr_tail,
        )
        return shell.fail(
            "check_failed",
            exit_code=EXIT_CORRUPTION,
            detail=f"restic check --read-data-subset={subset} failed.",
            repo_corrupt=True,
        )
    return verify_subset == "auto", subset


def _run_locked(
    cfg: BackupEnvConfig,
    lake: Path,
    verdict: PreflightVerdict,
    *,
    prior: BackupState | None,
    alerter: SweepAlerter,
    clock: Clock,
    runner_factory: Callable[[BackupEnvConfig], ResticRunner],
    plan_fn: Callable[..., BackupPlan],
) -> int:
    """§6 steps 4-11 under the single-flight lock."""
    baseline = prior if prior is not None else empty_backup_state()
    shell = _RunShell(cfg, alerter=alerter, clock=clock, baseline=baseline)
    start = clock()

    # Step 4 — mark running (the F17b arm). A state-write failure HERE is the
    # F10 hard path: nothing has been snapshotted, fail loud.
    try:
        shell.save(replace(baseline, running_since=start, last_attempt_at=start))
    except OSError as exc:
        _LOG.error("backup.state.write_failed", context="mark_running", error=str(exc))
        alerter.backup_failed(reason="state_write", detail=str(exc))
        return EXIT_RESTIC

    runner = runner_factory(cfg)

    # Step 5 — repo probe / init / stale unlock.
    early_exit = _probe_and_init_repo(shell, runner)
    if early_exit is not None:
        return early_exit

    # Step 6 — plan + consistency tag (F16).
    plan = plan_fn(cfg, lake)
    if plan.pull_active:
        _LOG.info("backup.run.pull_active")

    # Step 7 — the snapshot.
    backed = _backup_and_parse(shell, runner, plan)
    if isinstance(backed, int):
        return backed
    snapshot_id, partial = backed

    # Step 8 — verify (inside the success contract, F11).
    verified = _verify_repo(shell, runner)
    if isinstance(verified, int):
        return verified
    subset_advanced, subset_label = verified

    # Step 9 — stamp success (BEFORE maintenance — F23/graft A ordering).
    now = clock()
    partial_streak = shell.state.consecutive_partials + 1 if partial else 0
    success = replace(
        shell.state,
        running_since=None,
        last_success_at=now,
        last_snapshot_id=snapshot_id,
        consecutive_failures=0,
        consecutive_partials=partial_streak,
        verify_subset_cursor=(
            shell.state.verify_subset_cursor + 1
            if subset_advanced
            else shell.state.verify_subset_cursor
        ),
        maintenance_ok=True,
        lake_device=verdict.identity.device,
    )
    try:
        shell.save(success)
    except OSError as exc:
        _LOG.error("backup.state.write_failed", context="stamp_success", error=str(exc))
        alerter.backup_failed(reason="state_write", detail=str(exc))
        return EXIT_RESTIC
    if partial:
        if partial_streak >= PARTIAL_STREAK_CRITICAL:
            alerter.backup_failed(
                reason="partial_streak",
                detail=f"{partial_streak} consecutive partial (rc-3) backups.",
            )
        else:
            alerter.backup_degraded(
                aspect="partial",
                detail=f"restic read some source files partially (streak {partial_streak})",
            )

    # Step 10 — maintenance AFTER the stamp (F23: never retracts data-success).
    ist_weekday = clock().astimezone(_IST).weekday()
    forget = runner.forget(
        keep_daily=cfg.keep_daily,
        keep_weekly=cfg.keep_weekly,
        keep_monthly=cfg.keep_monthly,
        prune=ist_weekday == cfg.prune_weekday,
    )
    if not forget.ok:
        _LOG.warning(
            "backup.maintenance.failed", rc=forget.returncode, stderr_tail=forget.stderr_tail
        )
        shell.save_best_effort(replace(success, maintenance_ok=False), context="maintenance")
        alerter.backup_degraded(
            aspect="maintenance",
            detail=f"restic forget failed (rc={forget.returncode}) — data stamp stands",
        )
        return EXIT_MAINTENANCE

    # Step 11 — done.
    _LOG.info(
        "backup.run.ok",
        snapshot_id=snapshot_id,
        duration_s=(clock() - start).total_seconds(),
        tags=list(plan.tags),
        partial=partial,
        subset=subset_label,
    )
    return EXIT_OK


__all__ = ["PARTIAL_STREAK_CRITICAL", "run_backup"]
