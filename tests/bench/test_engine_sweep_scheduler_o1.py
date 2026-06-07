"""O(1) benchmark for the sweep-scheduler next-fire calculation (DEC-SWEEP-SCHEDULER-001).

:meth:`Schedule.next_fire_after` is pure arithmetic — its per-call cost must be
**independent** of how far in the future the next fire is or how many cycles have
elapsed. A naive scheduler that *iterated* from an anchor toward ``now`` (e.g.
``while t < now: t += interval``) would be O(gap) and blow up when the daemon
fell behind by thousands of intervals; the closed-form
:meth:`~brutex.engine.sweep_scheduler.Schedule.next_fire_after` must not.

Strategy: time ``next_fire_after`` with a ``last_fire`` that is *one* interval
behind ``now`` versus *one million* intervals behind. O(1) ⇒ the ratio ≈ 1.0;
an accidental O(gap) loop ⇒ the ratio explodes. The ceiling routes through the
shared two-tier :func:`tests.bench._o1_tiers.ratio_ceiling` (1.1× on the M4-Pro
strict runner, a robust 5× on the noisy shared runner).

Honest envelope (Golden Rule #10): this proves the *scheduling decision* is
O(1); it says nothing about the swept sweep's ``O(Σ work)`` wall-time, which the
scheduler only bounds the *start* of (CLAUDE.md §8 envelope, never "millions in
minutes").
"""

from __future__ import annotations

import time

from brutex.engine.sweep_scheduler import Schedule

from tests.bench._o1_tiers import ratio_ceiling

_HOUR = 3_600.0


def test_next_fire_after_is_constant_time_regardless_of_gap() -> None:
    """Per-call cost must not grow with how far behind the daemon fell."""
    schedule = Schedule.parse("1h")
    iterations = 100_000

    def _time_next_fire(intervals_behind: int) -> float:
        now = 1_700_000_000.0
        last_fire = now - intervals_behind * _HOUR
        # Warm the interpreter caches.
        for _ in range(100):
            schedule.next_fire_after(now, last_fire=last_fire)
        t0 = time.perf_counter_ns()
        for _ in range(iterations):
            schedule.next_fire_after(now, last_fire=last_fire)
        t1 = time.perf_counter_ns()
        return (t1 - t0) / iterations

    t_near = _time_next_fire(1)
    t_far = _time_next_fire(1_000_000)
    ratio = t_far / max(t_near, 1.0)

    assert ratio < ratio_ceiling(5.0), (
        f"next_fire_after cost scales with the catch-up gap: ratio {ratio:.2f}× "
        f"(1e6-behind vs 1-behind). t_near={t_near:.0f}ns, t_far={t_far:.0f}ns."
    )


def test_daily_next_fire_is_constant_time_far_in_future() -> None:
    """A daily schedule far from the epoch must cost the same as one near it."""
    schedule = Schedule.parse("daily@02:30")
    iterations = 100_000

    def _time_daily(now: float) -> float:
        for _ in range(100):
            schedule.next_fire_after(now)
        t0 = time.perf_counter_ns()
        for _ in range(iterations):
            schedule.next_fire_after(now)
        t1 = time.perf_counter_ns()
        return (t1 - t0) / iterations

    t_epoch = _time_daily(0.0)
    t_distant = _time_daily(4_102_444_800.0)  # year 2100
    ratio = t_distant / max(t_epoch, 1.0)

    assert ratio < ratio_ceiling(5.0), (
        f"daily next_fire_after cost scales with the epoch distance: ratio {ratio:.2f}×. "
        f"t_epoch={t_epoch:.0f}ns, t_distant={t_distant:.0f}ns."
    )
