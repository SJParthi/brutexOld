"""Sustained-load stress — the R39 tripwire engine under a 24h-shaped tick storm.

Invariants under load (DEC-STRESS-001 shape — CI nightly budget, env-raised
for local soaks via ``STRESS_EVENTS``):

1. A CLEAN storm (legal counters, finite metrics, stamped stages) fires
   ZERO tripwires across the whole run — no false positive under volume.
2. A POISONED storm (alternating NaN / recovery) never grows unbounded
   state: the events ring stays capped at :data:`EVENTS_RING_CAP`, the
   fan-out sees every edge, the SUSPECT latch holds, and no exception
   ever escapes an observation body (fail-soft law I-3).
3. The 24h-shaped interval-gated sampling (60 s reconcile cadence on an
   injected clock) keeps the dwell machinery bounded — the throughput
   watch fires exactly ONCE per sustained regime change, never per tick.

Memory-profiling note: per the house stress-test stance
(``tests/stress/test_audit_chain_sustained.py``), no RSS assertion here —
the boundedness signal is the CAPPED ring + fixed counter set.
"""

from __future__ import annotations

import math
import os

import pytest
from brutex.engine.sweep_progress import SweepProgress
from brutex.observability.run_tripwires import (
    EVENTS_RING_CAP,
    TripwireConfig,
    TripwireEvent,
    TripwireKind,
    TripwireMonitor,
)

pytestmark = pytest.mark.stress

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: dict[str, tuple[str, ...]] = {
    "test_r39_clean_storm_zero_false_positives": ("R39", "R16"),
    "test_r39_poisoned_storm_bounded_state": ("R39",),
    "test_r39_day_shaped_cadence_fires_once_per_regime": ("R39",),
}


def _n_events() -> int:
    return int(os.environ.get("STRESS_EVENTS", "20000"))


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Sink:
    def __init__(self) -> None:
        self.events: list[TripwireEvent] = []

    def __call__(self, event: TripwireEvent) -> None:
        self.events.append(event)


def test_r39_clean_storm_zero_false_positives() -> None:
    """N legal ticks + summaries + stage checks ⇒ ZERO trips, ZERO suspect."""
    n = _n_events()
    clock = _Clock()
    sink = _Sink()
    progress = SweepProgress(clock=clock)
    progress.start(enumerated=n)
    monitor = TripwireMonitor(
        config=TripwireConfig(reconcile_interval_s=1e-9),
        enumerated_total=n,
        clock=clock,
        on_trip=sink,
        ram_total_bytes=2**40,
        rss_probe=lambda: 1_000_000,  # constant RSS: zero slope, far below budget
    )
    for i in range(n):
        clock.now += 1.0
        progress.advance(survived=i % 7 == 0, skipped=i % 5 == 0)
        monitor.on_progress(progress.snapshot(), in_flight=0)
        if i % 10 == 0:
            monitor.on_tuple_summary(
                {"net_pnl_paisa": float(i % 1000), "sharpe": 1.25, "max_dd_paisa": 42.0}
            )
        if i % 100 == 0:
            monitor.on_stage("stage3", stamps_present=frozenset({"stage2"}))
    progress.finish()
    clock.now += 1.0
    monitor.on_progress(progress.snapshot(), in_flight=0)
    assert sink.events == []
    assert monitor.suspect is False
    assert monitor.events == ()


def test_r39_poisoned_storm_bounded_state() -> None:
    """NaN storm with recoveries: ring capped, every edge fanned out, no raise."""
    n = _n_events()
    sink = _Sink()
    monitor = TripwireMonitor(
        config=TripwireConfig(reconcile_interval_s=1e-9),
        enumerated_total=n,
        on_trip=sink,
        ram_total_bytes=2**40,
        rss_probe=lambda: 1_000_000,
    )
    edges = 0
    for i in range(n):
        if i % 2 == 0:
            monitor.on_tuple_summary({"sharpe": math.nan})  # breach edge
            edges += 1
        else:
            monitor.on_tuple_summary({"sharpe": 0.5})  # recovery re-arms
    assert monitor.suspect is True
    assert len(sink.events) == edges  # every EDGE fanned out exactly once
    assert all(e.name == TripwireKind.NAN_METRIC.value for e in sink.events)
    ring = monitor.events
    assert len(ring) == EVENTS_RING_CAP  # bounded forensics ring — never O(n)


def test_r39_day_shaped_cadence_fires_once_per_regime() -> None:
    """60 s-cadence day: a sustained throughput collapse fires exactly ONCE."""
    samples = max(100, _n_events() // 100)
    clock = _Clock()
    sink = _Sink()
    monitor = TripwireMonitor(
        config=TripwireConfig(
            reconcile_interval_s=60.0,
            throughput_floor_fraction=0.2,
            throughput_dwell=5,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        ),
        enumerated_total=10**9,
        clock=clock,
        on_trip=sink,
        ram_total_bytes=2**40,
    )
    from brutex.engine.sweep_progress import SweepProgressSnapshot

    def snap(evaluated: int, elapsed: float) -> SweepProgressSnapshot:
        return SweepProgressSnapshot(
            state="running",
            enumerated=10**9,
            evaluated=evaluated,
            survivors=0,
            skipped=0,
            current="",
            detail="",
            started_at=0.0,
            updated_at=elapsed,
        )

    evaluated = 0
    # Healthy warm-up: 20 minutes at ~1000 combos/min (rate ~16.7/s peak).
    for _ in range(20):
        evaluated += 1000
        clock.now += 60.0
        monitor.on_progress(snap(evaluated, clock.now), in_flight=0)
    assert sink.events == []
    # Collapsed remainder: ~1 combo per minute. The snapshot rate is the
    # CUMULATIVE average (evaluated/elapsed), so it decays through the 20%
    # floor at ~minute 81 of the collapse and STAYS below; dwell=5 must
    # fire exactly once, no matter how long the collapse persists.
    for _ in range(max(150, samples)):
        evaluated += 1
        clock.now += 60.0
        monitor.on_progress(snap(evaluated, clock.now), in_flight=0)
    trips = [e for e in sink.events if e.name == TripwireKind.THROUGHPUT_FLOOR.value]
    assert len(trips) == 1  # once per regime change — never per tick
    assert monitor.suspect is True
