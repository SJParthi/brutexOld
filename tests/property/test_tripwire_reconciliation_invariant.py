"""Property — the R39 count-reconciliation invariant (D-9 exact counters).

Random legal interleavings of a REAL :class:`~brutex.engine.sweep_progress.SweepProgress`
(start → advance× → finish) with legal ``in_flight`` values NEVER trip the
count-reconciliation tripwire; any injected illegality (in-flight
overflow, backwards counter) ALWAYS trips it exactly once per breach
edge. The monitor reads the SweepProgress snapshot counters EXACTLY —
never recorder record counts (design D-9).
"""

from __future__ import annotations

from brutex.engine.sweep_progress import SweepProgress
from brutex.observability.run_tripwires import (
    TripwireConfig,
    TripwireEvent,
    TripwireKind,
    TripwireMonitor,
)
from hypothesis import given, settings
from hypothesis import strategies as st

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: dict[str, tuple[str, ...]] = {
    "test_r39_legal_interleavings_never_trip": ("R39",),
    "test_r39_in_flight_overflow_always_trips_once": ("R39",),
    "test_r39_backwards_counter_always_trips": ("R39",),
}


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


def _counts_only_config() -> TripwireConfig:
    """Isolate the count-reconciliation watch (trend watches disabled)."""
    return TripwireConfig(
        reconcile_interval_s=1e-9,
        throughput_floor_fraction=0.0,
        rss_budget_fraction=0.0,
        memory_slope_bytes_per_tuple=0,
    )


def _armed(total: int) -> tuple[TripwireMonitor, _Sink, _Clock, SweepProgress]:
    clock = _Clock()
    sink = _Sink()
    monitor = TripwireMonitor(
        config=_counts_only_config(),
        enumerated_total=total,
        clock=clock,
        on_trip=sink,
        ram_total_bytes=1,
    )
    progress = SweepProgress(clock=clock)
    progress.start(enumerated=total)
    return monitor, sink, clock, progress


@settings(max_examples=150, deadline=None)
@given(st.data())
def test_r39_legal_interleavings_never_trip(data: st.DataObject) -> None:
    """Any LEGAL advance/in-flight interleaving keeps every count identity."""
    total = data.draw(st.integers(min_value=1, max_value=40), label="total")
    monitor, sink, clock, progress = _armed(total)
    n_ops = data.draw(st.integers(min_value=0, max_value=total), label="n_ops")
    for _ in range(n_ops):
        progress.advance(
            survived=data.draw(st.booleans(), label="survived"),
            skipped=data.draw(st.booleans(), label="skipped"),
        )
        snapshot = progress.snapshot()
        legal_max = total - snapshot.evaluated
        in_flight = data.draw(st.integers(min_value=0, max_value=legal_max), label="in_flight")
        clock.now += 1.0
        monitor.on_progress(snapshot, in_flight=in_flight)
    if progress.snapshot().evaluated == total:
        progress.finish()
        clock.now += 1.0
        monitor.on_progress(progress.snapshot(), in_flight=0)
    assert sink.events == []
    assert monitor.suspect is False


@settings(max_examples=150, deadline=None)
@given(
    total=st.integers(min_value=1, max_value=40),
    done=st.integers(min_value=0, max_value=40),
    excess=st.integers(min_value=1, max_value=5),
)
def test_r39_in_flight_overflow_always_trips_once(total: int, done: int, excess: int) -> None:
    """evaluated + in_flight > enumerated ALWAYS trips — exactly one edge."""
    done = min(done, total)
    monitor, sink, clock, progress = _armed(total)
    for _ in range(done):
        progress.advance()
    snapshot = progress.snapshot()
    overflow = (total - snapshot.evaluated) + excess
    clock.now += 1.0
    monitor.on_progress(snapshot, in_flight=overflow)
    clock.now += 1.0
    monitor.on_progress(snapshot, in_flight=overflow)  # still breached: no re-fire
    assert len(sink.events) == 1
    assert sink.events[0].name == TripwireKind.COUNT_RECONCILIATION.value
    assert monitor.suspect is True


@settings(max_examples=100, deadline=None)
@given(
    total=st.integers(min_value=2, max_value=40),
    first=st.integers(min_value=1, max_value=40),
    drop=st.integers(min_value=1, max_value=40),
)
def test_r39_backwards_counter_always_trips(total: int, first: int, drop: int) -> None:
    """A shrinking ``evaluated`` (impossible on a healthy run) ALWAYS trips."""
    first = min(first, total)
    drop = min(drop, first)
    monitor, sink, clock, progress = _armed(total)
    for _ in range(first):
        progress.advance()
    clock.now += 1.0
    monitor.on_progress(progress.snapshot(), in_flight=0)
    assert sink.events == []
    # Simulate a torn/rewound counter by re-arming the SAME progress object —
    # the reader now observes a smaller `evaluated` for the same run.
    progress.start(enumerated=total)
    for _ in range(first - drop):
        progress.advance()
    clock.now += 1.0
    monitor.on_progress(progress.snapshot(), in_flight=0)
    assert len(sink.events) == 1
    assert sink.events[0].name == TripwireKind.COUNT_RECONCILIATION.value
    assert f"evaluated went backwards: {first - drop} < {first}" in sink.events[0].detail
    assert monitor.suspect is True
