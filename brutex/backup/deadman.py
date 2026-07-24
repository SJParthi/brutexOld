"""The O(1) backup dead-man — pure verdict core + one-shot checker shell.

DEC-BACKUP-OFFBOX-001: the hourly ``brutex backup check`` reads ONE tiny JSON
(+ the checker-owned throttle sidecar), evaluates the pure
:func:`evaluate_deadman`, publishes at most ONE alert, and fires ONE HTTP ping
— NEVER a lake walk, never a restic call except the DAILY deep cross-check
(F21: one metadata call at most once per 24 h **on a COMPLETED AGREEING
comparison or a DELIVERED divergence CRITICAL** — gated on the durable
``deadman.json:last_deep_check_at`` stamp, which a FAILED metadata call AND
an UNDELIVERED divergence alert deliberately do NOT advance, so during a
remote/Telegram outage the deep tier retries next tick: bounded to one call
per hourly tick, fail-toward-alarm, alerts realert-bounded; the call itself
is bounded by :data:`DEEP_CHECK_TIMEOUT_S`, never the multi-day backup
deadline). SCALABLE by construction.

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

import http.client
import struct
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import urlsplit

import structlog

from brutex.backup import backup_rust
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
    from types import ModuleType

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


# ── Detail builders — ONE source for BOTH the Python oracle and the Rust
#    seam's report materialiser (the DEC-FEED-RUST-CORE-001 "messages moved
#    verbatim" discipline; DEC-BACKUP-RUST-OPS-001). Each builder is one
#    branch's f-string, byte-for-byte. ────────────────────────────────────


def _detail_state_corrupt(state_error: str) -> str:
    """The STATE_CORRUPT detail (F13)."""
    return f"state.json corrupt: {state_error}"


def _detail_never_no_file() -> str:
    """The NEVER_SUCCEEDED missing-file detail (F12)."""
    return "no backup state file — never succeeded (or state lost)"


def _detail_clock_skew_running(running_since: datetime) -> str:
    """The CLOCK_SKEW future-``running_since`` detail (F14)."""
    return f"running_since {running_since.isoformat()} is in the future"


def _detail_running(running_since: datetime) -> str:
    """The RUNNING detail (F17b)."""
    return f"backup running since {running_since.isoformat()}"


def _detail_hung(running_since: datetime, max_runtime_hours: float) -> str:
    """The HUNG detail (F17b)."""
    return (
        f"backup marked running since {running_since.isoformat()} "
        f"≥ {max_runtime_hours:g} h — wedged"
    )


def _detail_never_no_stamp() -> str:
    """The NEVER_SUCCEEDED no-success-stamp detail."""
    return "state present but no success ever stamped"


def _detail_clock_skew_success(last_success_at: datetime, now: datetime) -> str:
    """The CLOCK_SKEW future-``last_success_at`` detail (F14)."""
    return (
        f"last_success_at {last_success_at.isoformat()} is in the future "
        f"vs now {now.isoformat()} — poisoned stamp or clock skew"
    )


def _detail_overdue(elapsed: float, threshold_hours: float) -> str:
    """The OVERDUE detail."""
    return f"last success {elapsed:.1f} h ago > threshold {threshold_hours:g} h"


def _detail_fresh(elapsed: float, threshold_hours: float) -> str:
    """The FRESH detail."""
    return f"last success {elapsed:.1f} h ago ≤ threshold {threshold_hours:g} h"


def _evaluate_deadman_python(
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
            detail=_detail_state_corrupt(state_error),
        )
    if state is None:
        return DeadmanReport(
            verdict=DeadmanVerdict.NEVER_SUCCEEDED,
            hours_since_success=None,
            threshold_hours=threshold_hours,
            should_alert=_should_alert(DeadmanVerdict.NEVER_SUCCEEDED, sidecar, now, realert_hours),
            detail=_detail_never_no_file(),
        )
    hours = _hours_since(now, state.last_success_at)
    if state.running_since is not None:
        elapsed_run_hours = (now - state.running_since).total_seconds() / _SECONDS_PER_HOUR
        if (state.running_since - now).total_seconds() > skew_tolerance_s:
            verdict = DeadmanVerdict.CLOCK_SKEW
            detail = _detail_clock_skew_running(state.running_since)
        elif elapsed_run_hours < max_runtime_hours:
            verdict = DeadmanVerdict.RUNNING
            detail = _detail_running(state.running_since)
        else:
            verdict = DeadmanVerdict.HUNG
            detail = _detail_hung(state.running_since, max_runtime_hours)
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
            detail=_detail_never_no_stamp(),
        )
    if (state.last_success_at - now).total_seconds() > skew_tolerance_s:
        return DeadmanReport(
            verdict=DeadmanVerdict.CLOCK_SKEW,
            hours_since_success=hours,
            threshold_hours=threshold_hours,
            should_alert=_should_alert(DeadmanVerdict.CLOCK_SKEW, sidecar, now, realert_hours),
            detail=_detail_clock_skew_success(state.last_success_at, now),
        )
    elapsed = max(0.0, hours if hours is not None else 0.0)
    if elapsed > threshold_hours:
        return DeadmanReport(
            verdict=DeadmanVerdict.OVERDUE,
            hours_since_success=hours,
            threshold_hours=threshold_hours,
            should_alert=_should_alert(DeadmanVerdict.OVERDUE, sidecar, now, realert_hours),
            detail=_detail_overdue(elapsed, threshold_hours),
        )
    return DeadmanReport(
        verdict=DeadmanVerdict.FRESH,
        hours_since_success=hours,
        threshold_hours=threshold_hours,
        should_alert=False,
        detail=_detail_fresh(elapsed, threshold_hours),
    )


# ── The BRUTEX_BACKUP_RUST decision seam (DEC-BACKUP-RUST-OPS-001) ─────────
#
# Rust decides the ARM + verdict + should_alert + the two floats over
# PRIMITIVE inputs; Python materialises the DeadmanReport (details via the
# shared builders above — one source, both arms). Flag OFF serves
# `_evaluate_deadman_python` verbatim, crate untouched.

#: Detail-arm codes returned by ``brutex_ops.evaluate_deadman_decision`` —
#: each names ONE oracle branch (the crate-side ``ARM_*`` twins).
_ARM_STATE_CORRUPT: Final[int] = 1
_ARM_NEVER_NO_FILE: Final[int] = 2
_ARM_CLOCK_SKEW_RUNNING: Final[int] = 3
_ARM_RUNNING: Final[int] = 4
_ARM_HUNG: Final[int] = 5
_ARM_NEVER_NO_STAMP: Final[int] = 6
_ARM_CLOCK_SKEW_SUCCESS: Final[int] = 7
_ARM_OVERDUE: Final[int] = 8
_ARM_FRESH: Final[int] = 9


def _now_minus_seconds(now: datetime, stamp: datetime | None) -> float | None:
    """``(now − stamp).total_seconds()`` — the oracle's own delta op.

    Exact RELATIVE to the oracle (both arms consume this identical float),
    not exact in absolute terms — ``total_seconds()`` is a lossy float above
    ~2**53 µs (review NIT-2 wording).
    """
    if stamp is None:
        return None
    return (now - stamp).total_seconds()


def _pack_float(value: float | None) -> bytes | None:
    """Bit-pattern key for a report float (signed zeros distinguished — #1531)."""
    if value is None:
        return None
    return struct.pack("<d", value)


def _deadman_outcome_key(outcome: tuple[str, object]) -> object:
    """Burn-in comparison key: full report fields, floats bit-compared."""
    kind, payload = outcome
    if kind != "ok" or not isinstance(payload, DeadmanReport):
        return backup_rust.default_outcome_key(outcome)
    return (
        kind,
        payload.verdict,
        _pack_float(payload.hours_since_success),
        _pack_float(payload.threshold_hours),
        payload.should_alert,
        payload.detail,
    )


def _report_from_decision(
    decision: object,
    *,
    state: BackupState | None,
    now: datetime,
    threshold_hours: float,
    max_runtime_hours: float,
    state_error: str | None,
) -> DeadmanReport:
    """Materialise a :class:`DeadmanReport` from a crate decision tuple.

    Refuse-poison gate: a wrong-typed / arm-inconsistent decision raises
    ``TypeError``/``ValueError`` into the seam's degrade set (oracle served
    with one WARN) — never a silently wrong report.
    """
    arm, verdict_token, should_alert, hours, elapsed = cast(
        "tuple[object, object, object, object, object]", decision
    )
    if (
        not isinstance(arm, int)
        or isinstance(arm, bool)
        or not isinstance(verdict_token, str)
        or not isinstance(should_alert, bool)
        or not (hours is None or isinstance(hours, float))
        or not isinstance(elapsed, float)
    ):
        raise TypeError(f"poisoned deadman decision {decision!r}")
    verdict = DeadmanVerdict(verdict_token)
    detail: str
    if arm == _ARM_STATE_CORRUPT:
        if state_error is None:
            raise ValueError("STATE_CORRUPT arm without a state_error")
        detail = _detail_state_corrupt(state_error)
    elif arm == _ARM_NEVER_NO_FILE:
        detail = _detail_never_no_file()
    elif arm in (_ARM_CLOCK_SKEW_RUNNING, _ARM_RUNNING, _ARM_HUNG):
        if state is None or state.running_since is None:
            raise ValueError("running-family arm without a running_since stamp")
        if arm == _ARM_CLOCK_SKEW_RUNNING:
            detail = _detail_clock_skew_running(state.running_since)
        elif arm == _ARM_RUNNING:
            detail = _detail_running(state.running_since)
        else:
            detail = _detail_hung(state.running_since, max_runtime_hours)
    elif arm == _ARM_NEVER_NO_STAMP:
        detail = _detail_never_no_stamp()
    elif arm == _ARM_CLOCK_SKEW_SUCCESS:
        if state is None or state.last_success_at is None:
            raise ValueError("success-skew arm without a last_success_at stamp")
        detail = _detail_clock_skew_success(state.last_success_at, now)
    elif arm == _ARM_OVERDUE:
        detail = _detail_overdue(elapsed, threshold_hours)
    elif arm == _ARM_FRESH:
        detail = _detail_fresh(elapsed, threshold_hours)
    else:
        raise ValueError(f"unknown deadman decision arm {arm!r}")
    return DeadmanReport(
        verdict=verdict,
        hours_since_success=hours,
        threshold_hours=threshold_hours,
        should_alert=should_alert,
        detail=detail,
    )


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
    """Evaluate the pure dead-man verdict (see :func:`_evaluate_deadman_python`).

    The public entry — signature + behavior byte-identical to the retained
    Python oracle. Under the default-ON ``BRUTEX_BACKUP_RUST`` flag the
    verdict DECISION routes through the ``brutex_ops`` crate
    (DEC-BACKUP-RUST-OPS-001): the clock is read ONCE here (exactly one
    ``clock()`` call either way — the ``run_deadman_check`` frozen-now
    idiom), Python pre-computes the oracle's own ``total_seconds()`` deltas
    (exact — integer-µs datetime arithmetic), Rust returns the primitive
    decision, and the report is materialised via the SAME detail builders
    the oracle uses.
    """
    now = clock()

    def _oracle() -> DeadmanReport:
        return _evaluate_deadman_python(
            state,
            sidecar,
            threshold_hours=threshold_hours,
            realert_hours=realert_hours,
            skew_tolerance_s=skew_tolerance_s,
            max_runtime_hours=max_runtime_hours,
            clock=lambda: now,
            state_error=state_error,
        )

    def _compute(crate: ModuleType) -> object:
        decision = crate.evaluate_deadman_decision(
            state_error is not None,
            state is not None,
            None if state is None else _now_minus_seconds(now, state.last_success_at),
            None if state is None else _now_minus_seconds(now, state.running_since),
            _now_minus_seconds(now, sidecar.last_overdue_alert_at),
            threshold_hours,
            realert_hours,
            skew_tolerance_s,
            max_runtime_hours,
        )

        def _materialise() -> DeadmanReport:
            return _report_from_decision(
                decision,
                state=state,
                now=now,
                threshold_hours=threshold_hours,
                max_runtime_hours=max_runtime_hours,
                state_error=state_error,
            )

        return _materialise

    return backup_rust.route_law(
        "evaluate_deadman",
        oracle=_oracle,
        compute=_compute,
        outcome_key=_deadman_outcome_key,
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
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        # Round-4 MINOR: urlopen can ALSO raise http.client.InvalidURL (an
        # HTTPException — NOT a URLError/OSError/ValueError subclass) on an
        # interior control char (a line-wrapped plist URL) or a nonnumeric
        # port; its str() EMBEDS the URL path (the WRITE-capability token),
        # so HTTPException-class errors log only the type name.
        error = type(exc).__name__ if isinstance(exc, http.client.HTTPException) else str(exc)
        _LOG.warning("backup.deadman.ping_failed", url_host=_ping_url_host(url), error=error)
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

    THREE tiers, in order:

    1. **Stamp-plausibility bound (round-4 MAJOR, re-anchored round 5 —
       the forgery keystone):** a success stamp can legitimately land at
       most one bounded run-duration (``cfg.max_runtime_hours`` — the SAME
       knob that deadlines the run itself, ``timeout_s`` default =
       ``max_runtime_hours × 3600``) after the REMOTE latest snapshot's own
       time (restic stamps ``time`` at backup START inside the same run).
       The anchor is the remote time ALONE — the one stamp no on-box write
       can move: round 5 proved that folding ``last_attempt_at`` into the
       anchor (round 4) let a TWO-field forgery (bump success AND attempt,
       keep the real id — the same forgeable ``state.json``, no remote
       knowledge needed) read AGREE forever. The F21 poisoning — bump the
       stamp(s) while keeping the real old id — passes BOTH agreement arms
       below, so this bound is screened FIRST, before the id short-circuit.
       A legit multi-day seed passes because the operator raises
       ``max_runtime_hours`` for the seed window IN ``~/.brutex/.env`` so
       the hourly checker resolves the same bound (runbook step 6) and
       keeps it raised until the next nightly success restamps.
       Fail-toward-alarm: a pathological verify tail longer than
       ``max_runtime_hours`` would cost an extra page, never silence.
    2. **Snapshot identity:** the remote latest id matching
       ``state.last_snapshot_id`` — proof the remote's newest data IS the
       stamped last success (run-duration-tolerant, round-3 MAJOR).
    3. **Time gap (id-less state):** measured against the NEARER of
       ``last_success_at`` and ``last_attempt_at``, because a success-only
       gap would equal the RUN DURATION and false-CRITICAL any run longer
       than ``deadman_hours`` — the documented multi-day 8 TB seed.

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
    # Tier 1 — stamp plausibility (must precede the id short-circuit: a stamp
    # forgery KEEPS the real old id, which the remote still holds). Anchored
    # SOLELY on the remote latest snapshot's own time — the one stamp no
    # on-box write can move (round-5 MAJOR: state.last_attempt_at lives in
    # the SAME forgeable state.json, so folding it into the anchor let a
    # two-field bump — success + attempt — read AGREE forever). No legit
    # flow needs the attempt stamp here: restic stamps ``time`` at backup
    # START inside the same run, so an honest success always lands within
    # one bounded run-duration of the remote time.
    overshoot_hours = (state.last_success_at - info.time_utc).total_seconds() / _SECONDS_PER_HOUR
    if overshoot_hours > cfg.max_runtime_hours:
        return (
            True,
            True,
            (
                f"last_success_at {state.last_success_at.isoformat()} post-dates the remote "
                f"latest snapshot's time {info.time_utc.isoformat()} by {overshoot_hours:.1f} h "
                f"> max_runtime_hours {cfg.max_runtime_hours:g} h — a success stamp cannot "
                "legitimately land more than one bounded run-duration after its own backup "
                "started (plausible-stamp forgery, F21)"
            ),
        )
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
    # FAILED metadata call — and, since round 5, a DIVERGED comparison whose
    # CRITICAL was NOT delivered — does NOT stamp last_deep_check_at, so it
    # retries on the NEXT hourly tick — bounded to one call/hour during a
    # remote/Telegram outage (fail-toward-alarm; alerts stay realert-bounded;
    # the stamp lands after the publish attempt, below). A FUTURE
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
    deep_completed = False
    deep_diverged = False
    if deep_due and not dry_run and report.verdict is DeadmanVerdict.FRESH and state is not None:
        if not _config_resolves_repo(cfg):
            _LOG.info("backup.deadman.deep_skipped", reason="no_repo_config")
        else:
            factory = runner_factory if runner_factory is not None else default_runner_factory
            # The deep runner gets its OWN short deadline: the hourly one-shot
            # checker must never inherit the multi-day backup timeout for one
            # metadata call (a wedged mount would silence ticks AND pings).
            deep_cfg = replace(cfg, timeout_s=min(cfg.timeout_s, DEEP_CHECK_TIMEOUT_S))
            diverged, deep_completed, detail = _deep_divergence(cfg, state, factory(deep_cfg))
            deep_diverged = diverged
            if diverged:
                # Cause honesty (round-9 MINOR — the same class as the round-2
                # auth_or_repo, round-6 --no-lock, and round-8 check-rc=1
                # splits): a COMPLETED comparison that disagrees IS divergence
                # (the F21 forgery/poisoning framing); a FAILED metadata call
                # (remote outage / DNS / timeout / missing binary) proved
                # nothing about the stamp — asserting "divergence" would
                # misdirect the operator at the runbook §3 forgery playbook
                # during a transient S3 blip. Both arms keep the SAME alarm +
                # no-stamp-retry semantics (fail-toward-alarm); only the
                # reason token + log event tell the truth about the cause.
                if deep_completed:
                    _LOG.error("backup.deadman.remote_divergence", detail=detail)
                    reason = "state_remote_divergence"
                else:
                    _LOG.error("backup.deadman.deep_failed", detail=detail)
                    reason = "deep_check_unavailable"
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

    alert_delivered = False
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
            alert_delivered = True
            sidecar = replace(sidecar, last_overdue_alert_at=now)
        else:
            _LOG.warning("backup.alert.not_delivered", reason=reason)

    if deep_completed and (not deep_diverged or alert_delivered):
        # Round-5 MINOR: a DIVERGED comparison advances the daily deep gate
        # ONLY when its CRITICAL actually reached a live notifier — the
        # last_overdue_alert_at only-stamp-on-success law, one field over.
        # An undelivered divergence (Telegram outage, or diverged-but-
        # realert-throttled) retries the bounded 120 s metadata call on the
        # NEXT hourly tick instead of going quiet (exit 0 + plain success
        # pings) for 24 h. AGREEMENT stamps unconditionally.
        sidecar = replace(sidecar, last_deep_check_at=now)

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
