"""Sustained-load stress: the sweep-scheduler daemon loop over many cycles + crash-resume.

Exercises the pure scheduler core (DEC-SWEEP-SCHEDULER-001) the way the live
daemon drives it — thousands of back-to-back cycles, each:

1. compute the next fire from an injected clock (no real sleep),
2. classify the resume state of an evolving checkpoint cursor, and
3. advance the clock past the fire and recompute,

asserting the loop is monotone (never rewinds, never re-fires the same instant),
the cadence never drifts, and a checkpoint that fills up flips RESUME → … →
ALREADY_COMPLETE exactly once and stays there.

Budget
------
* CI nightly: ``CYCLES = 20_000`` (default, sub-second — pure arithmetic).
* Local soak: ``STRESS_SCHED_CYCLES=5_000_000`` via ``workflow_dispatch``.

Honest envelope (Golden Rule #10): this stresses the O(1) *decision* path, not
the swept sweep — every iteration is constant-time, so 20k cycles is sub-second
and proves no per-cycle state leak / drift, not any throughput claim.
"""

from __future__ import annotations

import itertools
import os

import pytest
from brutex.engine.sweep_scheduler import (
    CheckpointView,
    ResumeKind,
    Schedule,
    decide_resume,
    plan_cycle,
)

# Marker discipline (tests/stress/__init__.py): the nightly (`-m stress`) and
# self-hosted soak (`-m 'stress or soak'`) tiers select BY MARKER — an unmarked
# stress test silently drops out of both tiers (SOAK-R7-1).
pytestmark = pytest.mark.stress

_CYCLES = int(os.environ.get("STRESS_SCHED_CYCLES", "20000"))


def test_interval_daemon_loop_is_monotone_and_drift_free() -> None:
    """Many interval cycles never rewind, never re-fire, never drift the cadence."""
    interval = 3_600
    schedule = Schedule.parse(f"{interval}s")
    now = 1_700_000_000.0
    last_fire: float | None = None
    prev_fire = float("-inf")

    for _ in range(_CYCLES):
        fire_at = schedule.next_fire_after(now, last_fire=last_fire)
        assert fire_at >= now
        assert fire_at > prev_fire  # strictly monotone — no repeat instant
        if last_fire is not None:
            # On-schedule cadence is exact (we never fall behind in this loop).
            assert fire_at - last_fire == interval
        prev_fire = fire_at
        last_fire = fire_at
        # The daemon "sleeps" to the fire, runs the cycle, then loops just after.
        now = fire_at + 0.5


def test_daily_daemon_loop_one_fire_per_day_no_double_fire() -> None:
    """A daily schedule fires exactly once per calendar day across many cycles."""
    schedule = Schedule.parse("02:30")  # UTC for a clean per-day check
    now = 0.0
    last_fire: float | None = None
    fires: list[float] = []

    for _ in range(min(_CYCLES, 4_000)):  # 4000 days ≈ 11 years, plenty
        fire_at = schedule.next_fire_after(now, last_fire=last_fire)
        fires.append(fire_at)
        last_fire = fire_at
        now = fire_at + 0.5

    # Consecutive fires are exactly one day apart — no skipped or doubled day.
    deltas = {round(b - a) for a, b in itertools.pairwise(fires)}
    assert deltas == {86_400}


def test_checkpoint_fill_flips_to_complete_exactly_once() -> None:
    """Resume classification walks RESUME… → ALREADY_COMPLETE once and stays put."""
    total = 500
    completed_indices = [-1, *range(total)]  # -1 (fresh-ish) then 0..total-1
    complete_seen = 0
    prev_kind = None

    for last_completed in completed_indices:
        view = CheckpointView(
            exists=last_completed >= -1,
            sweep_id_matches=True,
            next_index=last_completed + 1,
            total_combos=total,
        )
        decision = decide_resume(view)
        if decision.kind is ResumeKind.ALREADY_COMPLETE:
            complete_seen += 1
            assert decision.resume_from_index == total
            assert decision.should_run is False
        else:
            assert decision.kind is ResumeKind.RESUME
            assert 0 <= decision.resume_from_index < total
            # ALREADY_COMPLETE must be terminal — never flip back to RESUME.
            assert prev_kind is not ResumeKind.ALREADY_COMPLETE
        prev_kind = decision.kind

    # Only the final (next_index == total) view is ALREADY_COMPLETE.
    assert complete_seen == 1


def test_plan_cycle_loop_sleep_never_negative_under_jitter() -> None:
    """plan_cycle's sleep is always non-negative even when the clock jitters past fire."""
    schedule = Schedule.parse("60s")
    now = 1_700_000_000.0
    last_fire: float | None = None

    for i in range(min(_CYCLES, 10_000)):
        view = CheckpointView(
            exists=last_fire is not None,
            sweep_id_matches=True,
            next_index=i % 7,
            total_combos=7,
        )
        plan = plan_cycle(now=now, schedule=schedule, checkpoint_view=view, last_fire=last_fire)
        assert plan.sleep_seconds >= 0.0
        assert plan.fire_at >= now
        last_fire = plan.fire_at
        # Simulate the daemon waking slightly LATE (jitter) so it occasionally
        # has to "catch up" — sleep must still clamp to >= 0.
        now = plan.fire_at + (1.5 if i % 3 == 0 else 0.0)
