"""Pure, clock-injected sweep-scheduler core (DEC-SWEEP-SCHEDULER-001).

The §11 AUTOMATED "ZERO clicks per day" decree wants the full brute-force sweep
to run **unattended on a cadence** (intended: nightly, after the incremental
vendor pull) while keeping the existing ``brutex sweep-all`` as the **manual** trigger.
This module is the deterministic *brain* of that daemon — and **nothing else**.

What this module IS (the testable core)
---------------------------------------
A side-effect-free, dependency-injected decision kernel that answers exactly two
questions a scheduler must answer, both as pure functions of values passed in:

1. **WHEN does the next sweep fire?** — :class:`Schedule` parses an interval
   (``"24h"``, ``"90m"``, ``"@hourly"``) or a daily wall-clock time
   (``"daily@02:30"``, ``"02:30"``, ``"@daily"``) and computes the next fire
   epoch from an **injected** ``now`` (no :func:`time.time` call here).
2. **Should the next cycle RESUME or start FRESH?** — :func:`decide_resume`
   classifies a :class:`CheckpointView` (the durable resume cursor the running
   sweep already maintains, :class:`~brutex.engine.sweep_checkpoint.SweepCheckpoint`)
   into FRESH / RESUME / ALREADY_COMPLETE. It **re-implements no resume logic** —
   the per-tuple resume itself is wholly owned by the live G&A supervisor
   (:func:`~brutex.engine.sweep_supervisor.run_supervised_grid`,
   DEC-SWEEP-WIRE-GA-001); this is the daemon's *observability + skip* view of
   that cursor (CLAUDE.md §11 COMMON — one source of truth, reused not copied).

What this module is NOT (kept out, on purpose)
----------------------------------------------
No ``time.sleep``, no threads, no signal handlers, no lake I/O, no ``sweep-all``
re-implementation. The live daemon loop (``brutex sweep-daemon``) wires this core
to the real clock + the existing :func:`~brutex.cli.cmd_sweep_all` pipeline and
is ``# pragma: no cover`` (cli.py is coverage-omitted per repo norm), exactly as
every other orchestration shim in this codebase. The whole sweep + crash-resume +
audit machinery is *reused*, never duplicated.

Honest envelope (Golden Rule #10)
---------------------------------
This module bounds **WHEN** a sweep runs; it does **not** change the sweep's
``O(Σ work)``. Every function here is **O(1)** — pure arithmetic / a constant
number of comparisons, independent of how far in the future the next fire is or
how many cycles have elapsed (proven by ``tests/bench/test_engine_sweep_scheduler_o1.py``).
There is no "millions in minutes" claim anywhere: a scheduled sweep still costs
the §8 wall-time envelope (~33B trials in 2-4 days on an M4 Pro, 12-24 h on a
64-128 core box); the scheduler only decides the *start time*.
"""

from __future__ import annotations

import contextlib
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Final, Protocol

import structlog

from brutex.models import BruteXError

if TYPE_CHECKING:
    # Annotation-only: the resume-view adapter reads a SweepCheckpoint's public
    # attributes but never needs the symbol at runtime (PEP-563 string annots),
    # so the import stays out of this pure module's runtime graph.
    from collections.abc import Generator

    from brutex.engine.sweep_checkpoint import SweepCheckpoint

_LOG = structlog.get_logger(__name__)

#: India Standard Time offset (UTC+5:30) in seconds — the natural anchor for a
#: "nightly, after the incremental vendor pull" daily schedule on an IST market.
#: The CLI passes this so ``daily@02:30`` means 02:30 **IST**, not 02:30 UTC.
IST_OFFSET_SECONDS: Final[int] = 5 * 3600 + 30 * 60  # 19_800

#: Seconds in a calendar day (no leap-seconds — epoch arithmetic is UTC-linear).
_SECONDS_PER_DAY: Final[int] = 86_400

#: Interval-unit suffix → multiplier in seconds (the only accepted units).
_UNIT_SECONDS: Final[dict[str, int]] = {"s": 1, "m": 60, "h": 3_600, "d": 86_400}

#: ``<positive-int><unit>`` interval form, e.g. ``"24h"`` / ``"90m"`` / ``"3600s"``.
_INTERVAL_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+)([smhd])$")

#: ``[daily@]HH:MM`` daily wall-clock form, 00:00..23:59 (24-hour).
_DAILY_RE: Final[re.Pattern[str]] = re.compile(r"^(?:daily@)?([01]?\d|2[0-3]):([0-5]\d)$")


class SchedulerError(BruteXError):
    """Raised on an unparseable or out-of-range schedule specification."""


class ScheduleKind(Enum):
    """Which of the two cadence shapes a :class:`Schedule` represents."""

    INTERVAL = "interval"
    DAILY = "daily"


@dataclass(frozen=True, slots=True)
class Schedule:
    """An immutable, parsed sweep cadence — the pure "WHEN" half of the core.

    Construct via :meth:`parse` (the only supported entry point); the fields are
    public for audit/serialisation but a hand-built instance must keep the
    invariants below or :meth:`next_fire_after` is undefined.

    Attributes
    ----------
    kind
        :attr:`ScheduleKind.INTERVAL` (fire every :attr:`interval_seconds`) or
        :attr:`ScheduleKind.DAILY` (fire once per day at :attr:`second_of_day`).
    interval_seconds
        For an interval schedule, the strictly-positive period in seconds; ``0``
        for a daily schedule.
    second_of_day
        For a daily schedule, the fire offset from local midnight in
        ``[0, 86400)`` seconds; ``0`` for an interval schedule.
    tz_offset_seconds
        For a daily schedule, the timezone offset (e.g. :data:`IST_OFFSET_SECONDS`)
        that maps ``second_of_day`` onto the UTC epoch grid; ``0`` for interval.
    spec
        The original (normalised, lower-cased) spec string — stamped into the
        daemon's BLAKE3 audit so a run is traceable to its cadence.
    """

    kind: ScheduleKind
    interval_seconds: int
    second_of_day: int
    tz_offset_seconds: int
    spec: str

    @classmethod
    def parse(cls, spec: str, *, tz_offset_seconds: int = 0) -> Schedule:
        """Parse a cadence *spec* into a :class:`Schedule` — O(1).

        Accepted forms (case-insensitive, surrounding whitespace ignored):

        * **Interval** ``<n><unit>`` with ``unit`` in ``s|m|h|d`` and ``n`` a
          positive integer — e.g. ``"30s"``, ``"90m"``, ``"24h"``, ``"1d"``.
        * **Interval alias** ``"@hourly"`` (≡ ``"1h"``).
        * **Daily** ``"HH:MM"`` or ``"daily@HH:MM"`` (24-hour) — fires once per
          day at that wall-clock time in the *tz_offset_seconds* zone.
        * **Daily alias** ``"@daily"`` (≡ ``"00:00"``).

        Parameters
        ----------
        spec
            The cadence string.
        tz_offset_seconds
            Timezone offset applied to a *daily* schedule (ignored for interval).
            Pass :data:`IST_OFFSET_SECONDS` for IST market nights.

        Returns
        -------
        Schedule
            The parsed schedule.

        Raises
        ------
        SchedulerError
            If *spec* is blank, an interval evaluates to ``0`` seconds, or the
            string matches no accepted form.
        """
        normalised = spec.strip().lower()
        if not normalised:
            raise SchedulerError("schedule spec must be a non-empty string")

        if normalised == "@hourly":
            normalised = "1h"
        elif normalised == "@daily":
            normalised = "00:00"

        interval_match = _INTERVAL_RE.match(normalised)
        if interval_match is not None:
            seconds = int(interval_match.group(1)) * _UNIT_SECONDS[interval_match.group(2)]
            if seconds <= 0:
                raise SchedulerError(f"interval schedule must be > 0 seconds, got {spec!r}")
            return cls(
                kind=ScheduleKind.INTERVAL,
                interval_seconds=seconds,
                second_of_day=0,
                tz_offset_seconds=0,
                spec=normalised,
            )

        daily_match = _DAILY_RE.match(normalised)
        if daily_match is not None:
            second_of_day = int(daily_match.group(1)) * 3_600 + int(daily_match.group(2)) * 60
            return cls(
                kind=ScheduleKind.DAILY,
                interval_seconds=0,
                second_of_day=second_of_day,
                tz_offset_seconds=tz_offset_seconds,
                spec=normalised,
            )

        raise SchedulerError(
            f"unrecognised schedule spec {spec!r} — use an interval like '24h'/'90m' "
            "or a daily time like 'daily@02:30'"
        )

    def next_fire_after(self, now: float, *, last_fire: float | None = None) -> float:
        """Return the next fire epoch (seconds) at-or-after *now* — O(1).

        Pure: no clock is read; *now* is the injected reference. *last_fire* (the
        epoch of the previous cycle's fire, or ``None`` on the daemon's first
        loop) makes the result idempotent — recomputing immediately after a fire
        never re-fires the same instant.

        Interval cadence
            ``anchor + interval`` where ``anchor`` is *last_fire* (or *now* on the
            first loop). If the daemon fell behind (that target is already past),
            the next fire is *now* — fire as soon as possible, exactly once
            (after which *last_fire* advances, so there is no tight loop).
        Daily cadence
            The next occurrence of :attr:`second_of_day` (in the schedule's
            timezone) at-or-after *now*; if that instant ``<= last_fire`` (we just
            fired it), roll forward one day so a clean post-fire recompute returns
            tomorrow rather than re-firing today.

        Parameters
        ----------
        now
            The reference epoch (UTC seconds). Injected — never read internally.
        last_fire
            The previous fire epoch, or ``None`` if no cycle has fired yet.

        Returns
        -------
        float
            The next fire epoch in UTC seconds (``>= now``).
        """
        if self.kind is ScheduleKind.INTERVAL:
            anchor = now if last_fire is None else last_fire
            candidate = anchor + self.interval_seconds
            return candidate if candidate > now else now

        # Daily: find local midnight on-or-before `now`, add the fire offset.
        local_now = now + self.tz_offset_seconds
        day_start_utc = math.floor(local_now / _SECONDS_PER_DAY) * _SECONDS_PER_DAY
        today_fire = day_start_utc + self.second_of_day - self.tz_offset_seconds
        candidate = today_fire if today_fire >= now else today_fire + _SECONDS_PER_DAY
        if last_fire is not None and candidate <= last_fire:
            candidate += _SECONDS_PER_DAY
        return candidate

    def seconds_until(self, now: float, *, last_fire: float | None = None) -> float:
        """Return the non-negative sleep, in seconds, until the next fire — O(1).

        Convenience wrapper the daemon sleeps on: ``max(0.0, next_fire - now)``.
        A caught-up interval (next fire == *now*) yields ``0.0`` (fire now).
        """
        delta = self.next_fire_after(now, last_fire=last_fire) - now
        return delta if delta > 0.0 else 0.0


class ResumeKind(Enum):
    """The resume classification :func:`decide_resume` assigns to a cycle."""

    #: No checkpoint for this grid identity — a brand-new sweep from index 0.
    FRESH = "fresh"
    #: A matching checkpoint with work left — resume from its next index.
    RESUME = "resume"
    #: A matching checkpoint that already covers every tuple — nothing to do.
    ALREADY_COMPLETE = "already_complete"


@dataclass(frozen=True, slots=True)
class CheckpointView:
    """An immutable, I/O-free snapshot of a sweep checkpoint for the resume decision.

    The pure value object :func:`decide_resume` consumes — decoupled from the
    on-disk :class:`~brutex.engine.sweep_checkpoint.SweepCheckpointStore` so the
    decision is testable without a filesystem. Build it from a loaded checkpoint
    via :meth:`from_loaded`.

    Attributes
    ----------
    exists
        Whether any checkpoint was found for the upcoming sweep.
    sweep_id_matches
        Whether the found checkpoint's ``sweep_id`` equals the upcoming grid
        identity (a different identity ⇒ the prior checkpoint is irrelevant).
    next_index
        The checkpoint's resume cursor (``last_completed_index + 1``); ``0`` when
        none exists.
    total_combos
        The total tuples the sweep enumerates; ``0`` when none exists.
    """

    exists: bool
    sweep_id_matches: bool
    next_index: int
    total_combos: int

    @classmethod
    def from_loaded(
        cls,
        checkpoint: SweepCheckpoint | None,
        *,
        expected_sweep_id: str,
    ) -> CheckpointView:
        """Build a view from a loaded checkpoint (or ``None``) — O(1).

        Parameters
        ----------
        checkpoint
            The :class:`~brutex.engine.sweep_checkpoint.SweepCheckpoint` read from
            disk, or ``None`` when no checkpoint file exists.
        expected_sweep_id
            The grid identity the upcoming cycle will run under — compared to the
            checkpoint's ``sweep_id`` to set :attr:`sweep_id_matches`.

        Returns
        -------
        CheckpointView
            The I/O-free snapshot for :func:`decide_resume`.
        """
        if checkpoint is None:
            return cls(exists=False, sweep_id_matches=False, next_index=0, total_combos=0)
        return cls(
            exists=True,
            sweep_id_matches=checkpoint.sweep_id == expected_sweep_id,
            next_index=checkpoint.next_index,
            total_combos=checkpoint.total_combos,
        )


@dataclass(frozen=True, slots=True)
class ResumeDecision:
    """The pure verdict :func:`decide_resume` returns for one cycle.

    Attributes
    ----------
    kind
        FRESH / RESUME / ALREADY_COMPLETE (:class:`ResumeKind`).
    resume_from_index
        The tuple index the cycle should begin at (``0`` for FRESH; the
        checkpoint cursor for RESUME; ``total_combos`` for ALREADY_COMPLETE).
    reason
        A short human-readable rationale, stamped into the daemon's audit line.
    """

    kind: ResumeKind
    resume_from_index: int
    reason: str

    @property
    def should_run(self) -> bool:
        """``True`` unless the cycle is ALREADY_COMPLETE (no work to launch)."""
        return self.kind is not ResumeKind.ALREADY_COMPLETE


def decide_resume(view: CheckpointView) -> ResumeDecision:
    """Classify *view* into a FRESH / RESUME / ALREADY_COMPLETE decision — O(1).

    Pure observability over the durable resume cursor the running sweep already
    maintains (it does **not** itself resume anything — the per-tuple resume is
    :func:`~brutex.engine.sweep_supervisor.run_supervised_grid`'s job). The
    classification:

    * **FRESH** — no checkpoint, or one belonging to a different grid identity
      (new data snapshot / changed knob ⇒ a new sweep from index 0).
    * **ALREADY_COMPLETE** — a matching checkpoint whose cursor already reaches
      ``total_combos``; the daemon can skip launching redundant work.
    * **RESUME** — a matching checkpoint with tuples left; begin at its cursor.

    Parameters
    ----------
    view
        The I/O-free checkpoint snapshot.

    Returns
    -------
    ResumeDecision
        The pure verdict.
    """
    if not view.exists:
        return ResumeDecision(
            kind=ResumeKind.FRESH,
            resume_from_index=0,
            reason="no checkpoint for this grid identity — fresh sweep from tuple 0",
        )
    if not view.sweep_id_matches:
        return ResumeDecision(
            kind=ResumeKind.FRESH,
            resume_from_index=0,
            reason="checkpoint belongs to a different grid identity — fresh sweep from tuple 0",
        )
    if view.next_index >= view.total_combos:
        return ResumeDecision(
            kind=ResumeKind.ALREADY_COMPLETE,
            resume_from_index=view.total_combos,
            reason=(
                f"checkpoint already covers all {view.total_combos} tuples — "
                "nothing to run this cycle"
            ),
        )
    return ResumeDecision(
        kind=ResumeKind.RESUME,
        resume_from_index=view.next_index,
        reason=(
            f"resuming from tuple {view.next_index} of {view.total_combos} "
            "(prior cycle was interrupted)"
        ),
    )


@dataclass(frozen=True, slots=True)
class CyclePlan:
    """The pure "what should the daemon do next" plan :func:`plan_cycle` returns.

    Attributes
    ----------
    fire_at
        The epoch (UTC seconds) the next cycle should start.
    sleep_seconds
        The non-negative seconds to wait until :attr:`fire_at` (``0.0`` when
        firing immediately or already caught up).
    resume
        The :class:`ResumeDecision` for the cycle that will fire.
    """

    fire_at: float
    sleep_seconds: float
    resume: ResumeDecision


def plan_cycle(
    *,
    now: float,
    schedule: Schedule,
    checkpoint_view: CheckpointView,
    last_fire: float | None = None,
    run_immediately: bool = False,
) -> CyclePlan:
    """Combine the WHEN + the RESUME halves into one pure cycle plan — O(1).

    The single clock-injected decision the live daemon loop makes each iteration:
    compute the next fire (or fire *now* when *run_immediately* — the
    ``--once`` / on-demand manual path) and classify the resume state.

    Parameters
    ----------
    now
        The injected reference epoch (UTC seconds).
    schedule
        The parsed cadence.
    checkpoint_view
        The I/O-free checkpoint snapshot for the resume decision.
    last_fire
        The previous cycle's fire epoch, or ``None`` on the first loop.
    run_immediately
        When ``True``, fire at *now* with ``0.0`` sleep (the ``--once`` /
        manual on-demand trigger), bypassing the cadence.

    Returns
    -------
    CyclePlan
        The fire time, the sleep, and the resume decision.
    """
    if run_immediately:
        fire_at = now
        sleep_seconds = 0.0
    else:
        fire_at = schedule.next_fire_after(now, last_fire=last_fire)
        delta = fire_at - now
        sleep_seconds = delta if delta > 0.0 else 0.0
    return CyclePlan(
        fire_at=fire_at,
        sleep_seconds=sleep_seconds,
        resume=decide_resume(checkpoint_view),
    )


def cycle_audit_payload(
    *,
    cycle_index: int,
    fire_at: float,
    schedule: Schedule,
    resume: ResumeDecision,
    once: bool,
) -> dict[str, Any]:
    """Return the JSON-serialisable BLAKE3-audit payload for one daemon cycle — O(1).

    Pure builder kept here (not in the coverage-omitted CLI) so the exact audit
    shape is golden-tested. The live daemon appends this dict to its hash-chained
    JSONL audit per cycle (CLAUDE.md Golden Rule #8).

    Parameters
    ----------
    cycle_index
        Zero-based index of this cycle within the daemon's lifetime.
    fire_at
        The epoch the cycle fired (UTC seconds).
    schedule
        The cadence the daemon is running.
    resume
        The cycle's resume decision.
    once
        Whether the daemon is in single-cycle (``--once``) mode.

    Returns
    -------
    dict[str, Any]
        The audit payload (all JSON-native scalars).
    """
    return {
        "cycle_index": cycle_index,
        "fire_at": fire_at,
        "schedule_kind": schedule.kind.value,
        "schedule_spec": schedule.spec,
        "interval_seconds": schedule.interval_seconds,
        "second_of_day": schedule.second_of_day,
        "tz_offset_seconds": schedule.tz_offset_seconds,
        "resume_kind": resume.kind.value,
        "resume_from_index": resume.resume_from_index,
        "resume_reason": resume.reason,
        "should_run": resume.should_run,
        "once": once,
    }


# ── G13 escalation kernel (DEC-SWEEP-UNKILLABLE-CORE-001, design D-5) ─────────
#
# The daemon's "phone-silent failure" gap: a cycle that fails repeatedly (or an
# init that dies BEFORE the alerter exists) must escalate to the operator. The
# DECISION logic lives here — the 100%-tested daemon core — and the coverage-
# omitted ``cmd_sweep_daemon`` shim in ``brutex.cli`` stays a thin adapter
# (constructs the ``SweepAlerter`` FIRST, wraps its init in
# :func:`daemon_init_guard`, feeds each cycle outcome to a
# :class:`CycleFailureTracker`).

#: Consecutive failed daemon cycles at which the escalation edge fires —
#: mirrors :data:`brutex.notifications.sweep_alerts.CRASH_LOOP_RELAUNCH_THRESHOLD`
#: (the same operator-actionable crash-loop bar; the value is REPEATED here, not
#: imported, so this pure module keeps zero notifications-package edges — the
#: cross-module equality is pinned by a test instead).
ESCALATION_THRESHOLD: Final[int] = 3


def escalate_after(consecutive_failures: int, *, threshold: int = ESCALATION_THRESHOLD) -> bool:
    """Pure LEVEL decision: has the failure streak reached the escalation bar? — O(1).

    ``True`` iff *consecutive_failures* ``>= threshold``. This is the stateless
    half of the G13 kernel; :meth:`CycleFailureTracker.record_failure` is the
    stateful EDGE (fires exactly once, at the cycle where the streak first
    reaches the bar) so a night of failed cycles yields ONE CRITICAL, not one
    per cycle.

    Parameters
    ----------
    consecutive_failures
        The current uninterrupted failed-cycle count (``0`` after any success).
    threshold
        The escalation bar (``>= 1``).

    Raises
    ------
    SchedulerError
        If *threshold* is ``< 1`` (a bar of 0 would escalate on a clean start —
        fail loud, never a silent mis-config).
    """
    if threshold < 1:
        raise SchedulerError(f"escalation threshold must be >= 1, got {threshold}")
    return consecutive_failures >= threshold


@dataclass(slots=True)
class CycleFailureTracker:
    """Consecutive-failure counter with an edge-triggered escalation verdict (G13).

    The daemon feeds every cycle outcome in; the tracker answers "is THIS the
    cycle to fire the CRITICAL escalation?" exactly once per streak:
    :meth:`record_failure` returns ``True`` only at the cycle where the streak
    FIRST reaches :attr:`threshold` (``escalate_after(n) and not
    escalate_after(n - 1)``); later failures in the same streak return
    ``False`` (the alert already fired); any success resets the streak.

    Attributes
    ----------
    threshold
        The escalation bar (``>= 1``; default :data:`ESCALATION_THRESHOLD`).
    consecutive_failures
        The current uninterrupted failed-cycle count (read-only observability;
        mutate only via the two ``record_*`` methods).

    Raises
    ------
    SchedulerError
        If constructed with ``threshold < 1``.
    """

    threshold: int = ESCALATION_THRESHOLD
    consecutive_failures: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise SchedulerError(f"escalation threshold must be >= 1, got {self.threshold}")

    def record_failure(self) -> bool:
        """Record one failed cycle; ``True`` iff the streak JUST reached the bar — O(1)."""
        self.consecutive_failures += 1
        return self.consecutive_failures == self.threshold

    def record_success(self) -> None:
        """Record one successful cycle — resets the streak to zero — O(1)."""
        self.consecutive_failures = 0


class HaltAlerterLike(Protocol):
    """Structural type for the halt-alert surface :func:`daemon_init_guard` uses.

    Matched by :class:`brutex.notifications.sweep_alerts.SweepAlerter` (and any
    test fake) — structural so this pure module never imports the
    notifications package.
    """

    def sweep_halted(self, *, run_id: str, reason: str) -> bool:
        """Fire the CRITICAL sweep-halted alert (best-effort)."""


@contextlib.contextmanager
def daemon_init_guard(alerter: HaltAlerterLike, *, run_id: str = "") -> Generator[None, None, None]:
    """Alerter-FIRST init guard (D-5): any init failure phones, then re-raises.

    The G13 root cause was a daemon dying BEFORE its alerter existed — a
    phone-silent failure. The fix is ordering: ``cmd_sweep_daemon`` constructs
    the :class:`~brutex.notifications.sweep_alerts.SweepAlerter` FIRST, then
    runs the REST of its initialisation inside this guard, so an init crash
    fires ``sweep_halted(reason="daemon_init")`` before propagating. The alert
    call itself is best-effort (a raising fake/notifier is swallowed + logged —
    the alert can never REPLACE the real init failure); the guarded body's
    exception ALWAYS re-raises unchanged. ``KeyboardInterrupt``/``SystemExit``
    propagate WITHOUT an alert (an operator-driven stop is not a failure).

    Parameters
    ----------
    alerter
        The already-constructed alerter (alerter-first — it must exist before
        anything that can fail).
    run_id
        The run identity when known; ``""`` at daemon init (the grid identity
        is minted later in the cycle) — the dedupe key still scopes per reason.
    """
    try:
        yield
    except Exception:
        try:
            alerter.sweep_halted(run_id=run_id, reason="daemon_init")
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never masks init failure
            _LOG.exception("sweep_scheduler.daemon_init_alert_failed")
        raise


__all__ = [
    "ESCALATION_THRESHOLD",
    "IST_OFFSET_SECONDS",
    "CheckpointView",
    "CycleFailureTracker",
    "CyclePlan",
    "HaltAlerterLike",
    "ResumeDecision",
    "ResumeKind",
    "Schedule",
    "ScheduleKind",
    "SchedulerError",
    "cycle_audit_payload",
    "daemon_init_guard",
    "decide_resume",
    "escalate_after",
    "plan_cycle",
]
