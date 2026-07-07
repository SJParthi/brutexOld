"""The O(1) backup dead-man — pure verdict core + one-shot checker shell.

DEC-BACKUP-OFFBOX-001: the hourly ``brutex backup check`` reads ONE tiny JSON
(+ the checker-owned throttle sidecar), evaluates the pure
:func:`evaluate_deadman`, publishes at most ONE alert, and fires ONE HTTP ping
— NEVER a lake walk, never a restic call except the DAILY deep cross-check
(F21: one metadata call at most once per 24 h **on a COMPLETED comparison** —
gated on the durable ``deadman.json:last_deep_check_at`` stamp, which a FAILED
metadata call deliberately does NOT advance, so during a remote outage the
deep tier retries next tick: bounded to one call per hourly tick,
fail-toward-alarm, alerts realert-bounded; the call itself is bounded by
:data:`DEEP_CHECK_TIMEOUT_S`, never the multi-day backup deadline). SCALABLE
by construction.

Fail-toward-alarm invariants:

* silence = alarm — a missing state file is NEVER_SUCCEEDED (F12);
* a corrupt state file is a hard STATE_CORRUPT alarm and is NEVER overwritten
  by the checker — forensics preserved (F13; the re-alert throttle lives in
  the checker-OWNED sidecar);
* a FUTURE ``last_success_at`` / ``running_since`` beyond the skew tolerance
  ALARMS (F14 — the exact state-poisoning/silencing vector);
* the re-alert stamp is written IFF the publish actually reached a live
  notifier (only-stamp-on-success — a Telegram outage never suppresses the
  next tick's retry, mirroring the notifier's own rule);
* the shallow check needs NO repo config — the dead-man keeps firing when
  ``BRUTEX_BACKUP_REPO`` itself is broken (the deep tier honestly logs
  ``backup.deadman.deep_skipped`` instead).
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import structlog

from brutex.backup.errors import (
    EXIT_DEADMAN_BREACH,
    EXIT_OK,
    BackupError,
    BackupStateError,
)
from brutex.backup.restic_runner import (
    BACKUP_TAG,
    default_runner_factory,
    parse_snapshots_latest,
)
from brutex.backup.state import (
    BackupState,
    DeadmanState,
    deadman_path,
    load_deadman,
    load_state,
    save_deadman,
    state_path,
)
from brutex.config.backup_env import DEEP_CHECK_INTERVAL_HOURS
from brutex.notifications.sweep_alerts import SweepAlerter

if TYPE_CHECKING:
    from brutex.backup.restic_runner import ResticRunner
    from brutex.config.backup_env import BackupEnvConfig

_LOG = structlog.get_logger(__name__)

#: Injectable UTC clock (the ``quota_tracker`` idiom) — every skew branch is
#: unit-testable without touching the wall clock.
Clock = Callable[[], datetime]

_SECONDS_PER_HOUR: Final[float] = 3600.0

#: Bounded wall-clock for the best-effort healthcheck ping (F18).
_PING_TIMEOUT_S: Final[float] = 10.0

#: Bounded wall-clock for the deep tier's ONE ``restic snapshots`` metadata
#: call. The default runner inherits ``cfg.timeout_s`` (max_runtime_hours ×
#: 3600 = a multi-day BACKUP deadline); a wedged metadata call under that
#: bound would block the one-shot hourly checker — no ticks, no alerts, no
#: healthcheck pings — for up to 48 h (launchd never overlaps a still-running
#: one-shot). A metadata call needs seconds, not days.
DEEP_CHECK_TIMEOUT_S: Final[float] = 120.0


def _default_utc_clock() -> datetime:
    """Return the current aware-UTC time (the production clock)."""
    return datetime.now(UTC)


class DeadmanVerdict(StrEnum):
    """The seven deterministic dead-man verdicts (each maps to F-matrix rows)."""

    FRESH = "fresh"
    RUNNING = "running"
    HUNG = "hung"
    OVERDUE = "overdue"
    NEVER_SUCCEEDED = "never_succeeded"
    STATE_CORRUPT = "state_corrupt"
    CLOCK_SKEW = "clock_skew"


#: The verdicts that page (CRITICAL, realert-bounded). RUNNING deliberately
#: does NOT page — a legitimate multi-day 8 TB seed must not alarm (F17b).
ALARM_VERDICTS: Final[frozenset[DeadmanVerdict]] = frozenset(
    {
        DeadmanVerdict.HUNG,
        DeadmanVerdict.OVERDUE,
        DeadmanVerdict.NEVER_SUCCEEDED,
        DeadmanVerdict.STATE_CORRUPT,
        DeadmanVerdict.CLOCK_SKEW,
    }
)


@dataclass(frozen=True, slots=True)
class DeadmanReport:
    """One evaluated dead-man verdict (pure output of :func:`evaluate_deadman`).

    Attributes
    ----------
    verdict
        The winning :class:`DeadmanVerdict`.
    hours_since_success
        ``now − last_success_at`` in hours (may be negative under skew);
        ``None`` when no success was ever stamped.
    threshold_hours
        The freshness threshold the verdict was judged against.
    should_alert
        ``verdict ∈ ALARM_VERDICTS`` AND outside the cross-run re-alert window.
    detail
        Human-readable one-liner for logs/alerts.
    """

    verdict: DeadmanVerdict
    hours_since_success: float | None
    threshold_hours: float
    should_alert: bool
    detail: str


def _hours_since(now: datetime, stamp: datetime | None) -> float | None:
    """Return ``now − stamp`` in hours (``None`` for a missing stamp)."""
    if stamp is None:
        return None
    return (now - stamp).total_seconds() / _SECONDS_PER_HOUR


def _should_alert(
    verdict: DeadmanVerdict, sidecar: DeadmanState, now: datetime, realert_hours: float
) -> bool:
    """Apply the durable cross-run re-alert bound (§7.3 — ≤4 CRITICAL/day, never zero)."""
    if verdict not in ALARM_VERDICTS:
        return False
    last = sidecar.last_overdue_alert_at
    if last is None:
        return True
    if last > now:
        # A FUTURE re-alert stamp (a botched ~/.brutex restore, a backward
        # NTP step — the F14 vectors, one file over) would otherwise mute
        # the Telegram lane until the wall clock catches up. Treat it as
        # "no prior alert": alert now, and a successful publish restamps
        # with the CURRENT clock — fail-toward-alarm, worst case one extra
        # page (the sidecar's own "extra alert, never a missed one" law).
        return True
    return (now - last).total_seconds() >= realert_hours * _SECONDS_PER_HOUR


def evaluate_deadman(
    state: BackupState | None,
    sidecar: DeadmanState,
    *,
    threshold_hours: float,
    realert_hours: float,
    skew_tolerance_s: float,
    max_runtime_hours: float,
    clock: Clock = _default_utc_clock,
    state_error: str | None = None,
) -> DeadmanReport:
    """Evaluate the pure dead-man verdict — no I/O, no lake walk, O(1).

    Evaluation ORDER (each earlier rule wins): state_error → STATE_CORRUPT;
    no state → NEVER_SUCCEEDED; ``running_since`` set → CLOCK_SKEW (future) /
    RUNNING (< max) / HUNG (≥ max); no success stamp → NEVER_SUCCEEDED;
    future success stamp → CLOCK_SKEW; then elapsed vs *threshold_hours* →
    OVERDUE / FRESH.

    Parameters
    ----------
    state
        The loaded runner state (``None`` = missing file, F12).
    sidecar
        The checker-owned throttle sidecar (re-alert bound input).
    threshold_hours
        Freshness threshold (``BRUTEX_BACKUP_DEADMAN_HOURS``).
    realert_hours
        Cross-run re-alert bound (``BRUTEX_BACKUP_REALERT_HOURS``).
    skew_tolerance_s
        Future-stamp tolerance before CLOCK_SKEW (F14).
    max_runtime_hours
        RUNNING→HUNG boundary for a marked-running backup (F17b).
    clock
        Injectable UTC clock.
    state_error
        Set by the shell when ``load_state`` raised (F13).

    Returns
    -------
    DeadmanReport
        The verdict + alert decision (pure — the shell does the side effects).
    """
    now = clock()
    if state_error is not None:
        return DeadmanReport(
            verdict=DeadmanVerdict.STATE_CORRUPT,
            hours_since_success=None,
            threshold_hours=threshold_hours,
            should_alert=_should_alert(DeadmanVerdict.STATE_CORRUPT, sidecar, now, realert_hours),
            detail=f"state.json corrupt: {state_error}",
        )
    if state is None:
        return DeadmanReport(
            verdict=DeadmanVerdict.NEVER_SUCCEEDED,
            hours_since_success=None,
            threshold_hours=threshold_hours,
            should_alert=_should_alert(DeadmanVerdict.NEVER_SUCCEEDED, sidecar, now, realert_hours),
            detail="no backup state file — never succeeded (or state lost)",
        )
    hours = _hours_since(now, state.last_success_at)
    if state.running_since is not None:
        elapsed_run_hours = (now - state.running_since).total_seconds() / _SECONDS_PER_HOUR
        if (state.running_since - now).total_seconds() > skew_tolerance_s:
            verdict = DeadmanVerdict.CLOCK_SKEW
            detail = f"running_since {state.running_since.isoformat()} is in the future"
        elif elapsed_run_hours < max_runtime_hours:
            verdict = DeadmanVerdict.RUNNING
            detail = f"backup running since {state.running_since.isoformat()}"
        else:
            verdict = DeadmanVerdict.HUNG
            detail = (
                f"backup marked running since {state.running_since.isoformat()} "
                f"≥ {max_runtime_hours:g} h — wedged"
            )
        return DeadmanReport(
            verdict=verdict,
            hours_since_success=hours,
            threshold_hours=threshold_hours,
            should_alert=_should_alert(verdict, sidecar, now, realert_hours),
            detail=detail,
        )
    if state.last_success_at is None:
        return DeadmanReport(
            verdict=DeadmanVerdict.NEVER_SUCCEEDED,
            hours_since_success=None,
            threshold_hours=threshold_hours,
            should_alert=_should_alert(DeadmanVerdict.NEVER_SUCCEEDED, sidecar, now, realert_hours),
            detail="state present but no success ever stamped",
        )
    if (state.last_success_at - now).total_seconds() > skew_tolerance_s:
        return DeadmanReport(
            verdict=DeadmanVerdict.CLOCK_SKEW,
            hours_since_success=hours,
            threshold_hours=threshold_hours,
            should_alert=_should_alert(DeadmanVerdict.CLOCK_SKEW, sidecar, now, realert_hours),
            detail=(
                f"last_success_at {state.last_success_at.isoformat()} is in the future "
                f"vs now {now.isoformat()} — poisoned stamp or clock skew"
            ),
        )
    elapsed = max(0.0, hours if hours is not None else 0.0)
    if elapsed > threshold_hours:
        return DeadmanReport(
            verdict=DeadmanVerdict.OVERDUE,
            hours_since_success=hours,
            threshold_hours=threshold_hours,
            should_alert=_should_alert(DeadmanVerdict.OVERDUE, sidecar, now, realert_hours),
            detail=f"last success {elapsed:.1f} h ago > threshold {threshold_hours:g} h",
        )
    return DeadmanReport(
        verdict=DeadmanVerdict.FRESH,
        hours_since_success=hours,
        threshold_hours=threshold_hours,
        should_alert=False,
        detail=f"last success {elapsed:.1f} h ago ≤ threshold {threshold_hours:g} h",
    )


def _ping_url_host(url: str) -> str | None:
    """The *url*'s host for logs — NEVER the full URL.

    A healthchecks ping URL is a WRITE-capability token (anyone holding it
    can keep the external dead-man green), so logs carry only the host —
    mirroring ``backup status``'s own SET/UNSET redaction stance.
    """
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _default_ping(url: str) -> bool:
    """Best-effort GET of the healthcheck *url* — True on success, never raises (F18)."""
    if not url.startswith(("http://", "https://")):
        _LOG.warning(
            "backup.deadman.ping_failed",
            url_host=_ping_url_host(url),
            error="non-http(s) URL refused",
        )
        return False
    try:
        # S310: scheme validated http(s) above; the URL is the operator's own
        # healthchecks endpoint from BRUTEX_BACKUP_HEALTHCHECK_URL — never
        # derived from remote input.
        with urllib.request.urlopen(url, timeout=_PING_TIMEOUT_S):  # noqa: S310  # nosec B310
            return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _LOG.warning("backup.deadman.ping_failed", url_host=_ping_url_host(url), error=str(exc))
        return False


def _config_resolves_repo(cfg: BackupEnvConfig) -> bool:
    """True iff repo + exactly one password source resolve (the deep-tier gate)."""
    return cfg.repo is not None and (cfg.password_file is None) != (cfg.password_command is None)


def _snapshot_ids_match(state_id: str | None, remote_id: str) -> bool:
    """True iff the stamped and remote snapshot ids name the SAME snapshot.

    ``parse_snapshots_latest`` prefers restic's 8-hex ``short_id`` while the
    backup summary stamps the FULL id into ``state.last_snapshot_id``, so a
    non-empty prefix match in EITHER direction is identity; an id-less state
    (``None``/empty — pre-schema or forged) never matches and falls to the
    time-gap test.
    """
    if not state_id:
        return False
    return state_id.startswith(remote_id) or remote_id.startswith(state_id)


def _deep_divergence(
    cfg: BackupEnvConfig,
    state: BackupState,
    runner: ResticRunner,
) -> tuple[bool, bool, str]:
    """Run the F21 deep cross-check against the remote trust root.

    Agreement is judged FIRST by snapshot identity (the remote latest id
    matching ``state.last_snapshot_id`` — proof the remote's newest data IS
    the stamped last success), and only for an id-less/forged state by the
    time gap — measured against the NEARER of ``last_success_at`` and
    ``last_attempt_at``, because restic stamps a snapshot's ``time`` at
    backup START (≈ ``last_attempt_at``) while the success stamp lands at run
    END: a success-only gap would equal the RUN DURATION and false-CRITICAL
    any run longer than ``deadman_hours`` — the documented multi-day 8 TB
    seed (round-3 MAJOR).

    Returns
    -------
    tuple[bool, bool, str]
        ``(diverged, completed, detail)`` — a COMPLETED comparison (either
        outcome) stamps ``last_deep_check_at``; a failed metadata call is
        divergence-this-tick but NOT completed (retried next tick).
    """
    inv = runner.snapshots_latest_json(tag=BACKUP_TAG)
    if not inv.ok:
        return (
            True,
            False,
            (
                f"restic snapshots failed (rc={inv.returncode}, "
                f"timed_out={inv.timed_out}, binary_missing={inv.binary_missing})"
            ),
        )
    try:
        info = parse_snapshots_latest(inv.stdout_full)
    except BackupError as exc:
        return True, True, f"restic snapshots output unparseable: {exc}"
    if info is None:
        return True, True, "remote repo has NO snapshot with the brutex tag"
    if state.last_success_at is None:
        # Unreachable via the FRESH gate (FRESH implies a success stamp) —
        # kept as a defensive divergence, never an assert.
        return True, True, "state carries no success stamp to compare against"
    if _snapshot_ids_match(state.last_snapshot_id, info.snapshot_id):
        return (
            False,
            True,
            f"remote latest {info.snapshot_id} matches stamped last_snapshot_id",
        )
    gap_seconds = abs((state.last_success_at - info.time_utc).total_seconds())
    if state.last_attempt_at is not None:
        gap_seconds = min(gap_seconds, abs((state.last_attempt_at - info.time_utc).total_seconds()))
    gap_hours = gap_seconds / _SECONDS_PER_HOUR
    if gap_hours > cfg.deadman_hours:
        return (
            True,
            True,
            (
                f"state stamp diverges from remote latest snapshot {info.snapshot_id} "
                f"by {gap_hours:.1f} h > {cfg.deadman_hours:g} h"
            ),
        )
    return False, True, f"remote latest {info.snapshot_id} agrees ({gap_hours:.1f} h gap)"


def run_deadman_check(
    cfg: BackupEnvConfig,
    *,
    deep: bool = False,
    dry_run: bool = False,
    alerter: SweepAlerter | None = None,
    clock: Clock = _default_utc_clock,
    runner_factory: Callable[[BackupEnvConfig], ResticRunner] | None = None,
    ping: Callable[[str], bool] = _default_ping,
) -> int:
    """The one-shot dead-man checker shell (``brutex backup check``).

    Parameters
    ----------
    cfg
        The resolved backup config (the shallow check needs NO repo config).
    deep
        Force the F21 remote cross-check this tick.
    dry_run
        Compute + log the verdict only — no alert, no sidecar write, no ping,
        no deep restic call.
    alerter
        Injectable alerter; ``None`` → :class:`SweepAlerter`.
    clock
        Injectable UTC clock.
    runner_factory
        Injectable :class:`ResticRunner` factory for the deep tier; ``None`` →
        :func:`~brutex.backup.restic_runner.default_runner_factory`.
    ping
        Injectable healthcheck ping (defaults to the stdlib urllib GET).

    Returns
    -------
    int
        ``EXIT_OK`` for FRESH/RUNNING; ``EXIT_DEADMAN_BREACH`` for every alarm
        verdict. NEVER writes ``state.json``; NEVER walks the lake.
    """
    state: BackupState | None = None
    state_error: str | None = None
    try:
        state = load_state(state_path(cfg))
    except BackupStateError as exc:
        state_error = str(exc)
    sidecar = load_deadman(deadman_path(cfg))
    now = clock()
    report = evaluate_deadman(
        state,
        sidecar,
        threshold_hours=cfg.deadman_hours,
        realert_hours=cfg.realert_hours,
        skew_tolerance_s=cfg.clock_skew_tolerance_s,
        max_runtime_hours=cfg.max_runtime_hours,
        clock=lambda: now,
        state_error=state_error,
    )
    reason = report.verdict.value

    # DEEP tier (F21) — only when shallow reads FRESH (an already-alarming
    # state needs no divergence proof), never on dry-run, at most once per
    # DEEP_CHECK_INTERVAL_HOURS on a COMPLETED comparison unless forced. A
    # FAILED metadata call does NOT stamp last_deep_check_at, so it retries
    # on the NEXT hourly tick — bounded to one call/hour during a remote
    # outage (fail-toward-alarm; alerts stay realert-bounded). A FUTURE
    # last_deep_check_at (a botched ~/.brutex restore, a backward NTP step —
    # the F14 vectors, the _should_alert guard's twin one field over) reads
    # as "no prior deep check": deep is due NOW, and a completed comparison
    # restamps with the CURRENT clock — self-healing, worst case one extra
    # metadata call (fail-toward-alarm; never a muted forgery-detection lane).
    deep_due = (
        deep
        or sidecar.last_deep_check_at is None
        or sidecar.last_deep_check_at > now
        or (now - sidecar.last_deep_check_at).total_seconds()
        >= DEEP_CHECK_INTERVAL_HOURS * _SECONDS_PER_HOUR
    )
    if deep_due and not dry_run and report.verdict is DeadmanVerdict.FRESH and state is not None:
        if not _config_resolves_repo(cfg):
            _LOG.info("backup.deadman.deep_skipped", reason="no_repo_config")
        else:
            factory = runner_factory if runner_factory is not None else default_runner_factory
            # The deep runner gets its OWN short deadline: the hourly one-shot
            # checker must never inherit the multi-day backup timeout for one
            # metadata call (a wedged mount would silence ticks AND pings).
            deep_cfg = replace(cfg, timeout_s=min(cfg.timeout_s, DEEP_CHECK_TIMEOUT_S))
            diverged, completed, detail = _deep_divergence(cfg, state, factory(deep_cfg))
            if completed:
                sidecar = replace(sidecar, last_deep_check_at=now)
            if diverged:
                _LOG.error("backup.deadman.remote_divergence", detail=detail)
                reason = "state_remote_divergence"
                report = DeadmanReport(
                    verdict=DeadmanVerdict.OVERDUE,
                    hours_since_success=report.hours_since_success,
                    threshold_hours=report.threshold_hours,
                    should_alert=_should_alert(
                        DeadmanVerdict.OVERDUE, sidecar, now, cfg.realert_hours
                    ),
                    detail=detail,
                )
            else:
                _LOG.info("backup.deadman.deep_ok", detail=detail)

    log = _LOG.error if report.verdict in ALARM_VERDICTS else _LOG.info
    log(
        f"backup.deadman.{report.verdict.value}",
        detail=report.detail,
        hours_since_success=report.hours_since_success,
        threshold_hours=report.threshold_hours,
        should_alert=report.should_alert,
        dry_run=dry_run,
    )

    quiet = report.verdict in (DeadmanVerdict.FRESH, DeadmanVerdict.RUNNING)
    if dry_run:
        return EXIT_OK if quiet else EXIT_DEADMAN_BREACH

    if report.should_alert:
        active_alerter = alerter if alerter is not None else SweepAlerter()
        published = active_alerter.backup_overdue(
            reason=reason,
            hours_since_success=report.hours_since_success,
            threshold_hours=report.threshold_hours,
        )
        if published:
            # Only-stamp-on-success: a failed/disabled publish leaves the
            # stamp untouched so the NEXT tick retries (F22).
            sidecar = replace(sidecar, last_overdue_alert_at=now)
        else:
            _LOG.warning("backup.alert.not_delivered", reason=reason)

    try:
        save_deadman(deadman_path(cfg), sidecar)
    except OSError as exc:
        # The sidecar is only a throttle — losing the write can only cause an
        # EXTRA alert next tick, never a missed one (fail-toward-alarm).
        _LOG.warning("backup.deadman.sidecar_write_failed", error=str(exc))

    if cfg.healthcheck_url is not None:
        ping(cfg.healthcheck_url if quiet else cfg.healthcheck_url + "/fail")

    return EXIT_OK if quiet else EXIT_DEADMAN_BREACH


__all__ = [
    "ALARM_VERDICTS",
    "DEEP_CHECK_TIMEOUT_S",
    "Clock",
    "DeadmanReport",
    "DeadmanVerdict",
    "evaluate_deadman",
    "run_deadman_check",
]
