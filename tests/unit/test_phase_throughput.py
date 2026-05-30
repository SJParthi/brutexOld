"""Unit tests for :mod:`brutex.orchestrator.phase_throughput`.

A deterministic synthetic clock drives :class:`PhaseThroughput` so the achieved
r/s, per-call latency (mean/min/max), wall-elapsed, and peak in-flight counters
are asserted exactly — including the empty-phase (0/0) and zero-wall edges, and
the rounding contract of :meth:`ThroughputSnapshot.as_log_fields`.
"""

from __future__ import annotations

from brutex.orchestrator.phase_throughput import PhaseThroughput, ThroughputSnapshot


class _Clock:
    """A controllable monotonic clock: ``advance`` to simulate elapsed time."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class TestEmptyPhase:
    def test_no_calls_is_all_zero_never_divides(self) -> None:
        clk = _Clock()
        tp = PhaseThroughput(clock=clk)
        clk.advance(5.0)  # wall passes, but nothing completed
        snap = tp.snapshot()
        assert snap == ThroughputSnapshot(
            completed=0,
            wall_elapsed_s=5.0,
            achieved_rps=0.0,
            latency_mean_s=0.0,
            latency_min_s=0.0,
            latency_max_s=0.0,
            peak_in_flight=0,
        )


class TestSingleCall:
    def test_one_call_records_latency_and_rate(self) -> None:
        clk = _Clock()
        tp = PhaseThroughput(clock=clk)
        with tp.track():
            clk.advance(2.0)  # a 2 s round-trip
        clk.advance(2.0)  # snapshot 4 s after phase start
        snap = tp.snapshot()
        assert snap.completed == 1
        assert snap.wall_elapsed_s == 4.0
        assert snap.achieved_rps == 0.25  # 1 call / 4 s
        assert snap.latency_mean_s == 2.0
        assert snap.latency_min_s == 2.0
        assert snap.latency_max_s == 2.0
        assert snap.peak_in_flight == 1

    def test_zero_wall_with_a_completed_call_reports_zero_rps(self) -> None:
        # Clock never advances → wall == 0 with completed > 0: the rate falls to
        # 0.0 (no divide-by-zero) while the call still counts.
        clk = _Clock()
        tp = PhaseThroughput(clock=clk)
        with tp.track():
            pass
        snap = tp.snapshot()
        assert snap.completed == 1
        assert snap.wall_elapsed_s == 0.0
        assert snap.achieved_rps == 0.0
        assert snap.latency_mean_s == 0.0


class TestMultipleCalls:
    def test_mean_min_max_across_calls(self) -> None:
        clk = _Clock()
        tp = PhaseThroughput(clock=clk)
        for dur in (1.0, 3.0, 2.0):  # min 1, max 3, mean 2
            with tp.track():
                clk.advance(dur)
        snap = tp.snapshot()
        assert snap.completed == 3
        assert snap.latency_min_s == 1.0
        assert snap.latency_max_s == 3.0
        assert snap.latency_mean_s == 2.0
        assert snap.wall_elapsed_s == 6.0
        assert snap.achieved_rps == 0.5  # 3 / 6 s

    def test_failed_call_still_counts_toward_latency_and_concurrency(self) -> None:
        clk = _Clock()
        tp = PhaseThroughput(clock=clk)
        try:
            with tp.track():
                clk.advance(1.5)
                raise RuntimeError("vendor blew up")
        except RuntimeError:
            pass
        snap = tp.snapshot()
        assert snap.completed == 1  # the failed call consumed a worker + time
        assert snap.latency_max_s == 1.5


class TestConcurrencySaturation:
    def test_peak_in_flight_tracks_high_water_mark(self) -> None:
        # Nested track() blocks model two workers in flight at once.
        clk = _Clock()
        tp = PhaseThroughput(clock=clk)
        # Two context managers entered together → in_flight reaches 2 (models two
        # workers concurrently in flight) before either exits.
        with tp.track(), tp.track():
            pass
        snap = tp.snapshot()
        assert snap.peak_in_flight == 2
        assert snap.completed == 2

    def test_in_flight_returns_to_zero_so_peak_is_not_re_inflated(self) -> None:
        clk = _Clock()
        tp = PhaseThroughput(clock=clk)
        with tp.track():
            pass
        with tp.track():
            pass
        # Two sequential calls — never more than 1 concurrent.
        assert tp.snapshot().peak_in_flight == 1


class TestLogFields:
    def test_as_log_fields_rounds_and_types(self) -> None:
        snap = ThroughputSnapshot(
            completed=1652,
            wall_elapsed_s=601.3334,
            achieved_rps=2.74799,
            latency_mean_s=2.90011,
            latency_min_s=0.512345,
            latency_max_s=9.87654,
            peak_in_flight=8,
        )
        assert snap.as_log_fields() == {
            "completed": 1652,
            "wall_elapsed_s": 601.333,
            "achieved_rps": 2.748,
            "latency_mean_s": 2.9,
            "latency_min_s": 0.512,
            "latency_max_s": 9.877,
            "peak_in_flight": 8,
        }
