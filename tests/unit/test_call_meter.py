"""Unit tests for ``brutex.adapters.call_meter.CallMeter`` (DEC-GROWW-RATE-ACHIEVED-METER-001).

100% line+branch with an injected clock: start/stop in-flight balance, achieved-rate
rolling window + eviction, rps_1s + its run-peak ratchet (taken + not-taken),
latency-p95 (empty + populated + ring eviction), the success-only completion rule,
and the throttled ``rate.achieved`` log (warm-up grace, post-interval emit, re-throttle,
emit_log=False). The meter is the honest achieved-rate instrument the dashboard reads.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from brutex.adapters.call_meter import (
    ACHIEVED_WINDOW_S,
    LATENCY_SAMPLES,
    CallMeter,
)
from structlog.testing import capture_logs


class _Clock:
    """A deterministic, advanceable monotonic clock."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _record(meter: CallMeter, *, elapsed_ms: float = 10.0, completed: bool = True) -> None:
    """One full physical-call bracket (start → stop)."""
    meter.start()
    meter.stop(elapsed_ms=elapsed_ms, completed=completed)


def test_empty_snapshot_is_all_zero() -> None:
    snap = CallMeter(clock=_Clock(), emit_log=False).snapshot()
    assert snap == {
        "effective_rps": 0.0,
        "rate_achieved": 0.0,
        "rps_1s": 0,
        "rps_1s_max": 0,
        "latency_ms_p95": 0.0,
        "in_flight": 0,
    }


def test_start_increments_in_flight() -> None:
    meter = CallMeter(clock=_Clock(), emit_log=False)
    meter.start()
    meter.start()
    assert meter.snapshot()["in_flight"] == 2


def test_completed_call_records_and_releases_in_flight() -> None:
    clk = _Clock()
    meter = CallMeter(clock=clk, emit_log=False)
    _record(meter, elapsed_ms=100.0, completed=True)
    snap = meter.snapshot()
    assert snap["in_flight"] == 0
    assert snap["rps_1s"] == 1
    assert snap["rps_1s_max"] == 1
    assert snap["latency_ms_p95"] == 100.0
    # One completion in the fixed ACHIEVED_WINDOW_S (10 s) → 1/10 = 0.1 r/s.
    assert snap["effective_rps"] == 0.1
    assert snap["rate_achieved"] == 0.1


def test_failed_call_releases_in_flight_but_is_not_achieved() -> None:
    meter = CallMeter(clock=_Clock(), emit_log=False)
    _record(meter, elapsed_ms=50.0, completed=False)
    snap = meter.snapshot()
    assert snap["in_flight"] == 0
    assert snap["rps_1s"] == 0  # a failed attempt is NOT achieved throughput
    assert snap["rps_1s_max"] == 0
    assert snap["effective_rps"] == 0.0
    assert snap["latency_ms_p95"] == 0.0  # no latency recorded for a failure


def test_effective_rps_is_a_trailing_window_average_with_eviction() -> None:
    clk = _Clock()
    meter = CallMeter(clock=clk, emit_log=False)
    for _ in range(8):
        _record(meter)
        clk.advance(0.125)  # 8 calls over 1 s
    # All 8 still inside the 10 s window → 8 / 10 = 0.8 r/s.
    assert meter.snapshot()["effective_rps"] == 0.8
    # Advance past the window → every completion evicted → back to 0.
    clk.advance(ACHIEVED_WINDOW_S + 1.0)
    assert meter.snapshot()["effective_rps"] == 0.0


def test_rps_1s_window_and_peak_ratchet_taken_and_not_taken() -> None:
    clk = _Clock()
    meter = CallMeter(clock=clk, emit_log=False)
    for _ in range(3):
        _record(meter)
        clk.advance(0.2)  # 3 within a trailing second → peak ratchets 1→2→3
    snap = meter.snapshot()
    assert snap["rps_1s"] == 3
    assert snap["rps_1s_max"] == 3
    # A lone completion >1 s later: rps_1s falls to 1 but the peak HOLDS (ratchet
    # not-taken branch: 1 > 3 is False).
    clk.advance(2.0)
    _record(meter)
    snap2 = meter.snapshot()
    assert snap2["rps_1s"] == 1
    assert snap2["rps_1s_max"] == 3


def test_latency_p95_over_populated_ring() -> None:
    clk = _Clock()
    meter = CallMeter(clock=clk, emit_log=False)
    for i in range(1, 101):  # latencies 1..100 ms
        _record(meter, elapsed_ms=float(i))
        clk.advance(0.001)
    # idx = min(99, int(0.95*100)) = 95 → sorted[95] = 96.0.
    assert meter.snapshot()["latency_ms_p95"] == 96.0


def test_latency_ring_is_bounded_to_latency_samples() -> None:
    clk = _Clock()
    meter = CallMeter(clock=clk, emit_log=False)
    for i in range(LATENCY_SAMPLES + 1):  # one over the cap → triggers a popleft
        _record(meter, elapsed_ms=float(i))
        clk.advance(0.001)
    assert len(meter._latencies) == LATENCY_SAMPLES  # the ring evicted the oldest


def test_no_achieved_log_during_warmup_grace() -> None:
    clk = _Clock()
    meter = CallMeter(clock=clk, emit_log=True)
    with capture_logs() as logs:
        _record(meter)  # at t=0, within the interval since construction → no log
    assert not [e for e in logs if e["event"] == "rate.achieved"]


def test_achieved_log_emits_after_interval_then_rethrottles() -> None:
    clk = _Clock()
    meter = CallMeter(clock=clk, emit_log=True)
    clk.advance(20.0)  # past the 15 s throttle since construction
    with capture_logs() as logs:
        _record(meter, elapsed_ms=10.0)
    events = [e for e in logs if e["event"] == "rate.achieved"]
    assert len(events) == 1
    assert events[0]["effective_rps"] == 0.1
    assert events[0]["in_flight"] == 0
    # A second completion within the interval → throttled, no second log.
    with capture_logs() as logs2:
        _record(meter)
    assert not [e for e in logs2 if e["event"] == "rate.achieved"]


def test_emit_log_false_never_logs() -> None:
    clk = _Clock()
    meter = CallMeter(clock=clk, emit_log=False)
    clk.advance(100.0)  # well past the interval, but logging is OFF
    with capture_logs() as logs:
        _record(meter)
    assert not [e for e in logs if e["event"] == "rate.achieved"]
