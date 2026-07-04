"""Unit tests for ``brutex.adapters.call_meter.CallMeter`` (DEC-GROWW-RATE-ACHIEVED-METER-001).

100% line+branch with an injected clock: start/stop in-flight balance, achieved-rate
rolling window + eviction, rps_1s + its run-peak ratchet (taken + not-taken),
latency-p95 (empty + populated + ring eviction), the success-only completion rule,
and the throttled ``rate.achieved`` log (warm-up grace, post-interval emit, re-throttle,
emit_log=False). The meter is the honest achieved-rate instrument the dashboard reads.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import threading

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
        # ADDITIVE lifetime/failure keys (DEC-PULL-TELEMETRY-TRIAD-001) — the six keys
        # above are the pre-existing contract, pinned byte-identical by this test.
        "issued": 0,
        "completed": 0,
        "failed": 0,
        "failed_rps": 0.0,
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


def test_stop_without_start_floors_in_flight_at_zero() -> None:
    # A stray stop (a double-stop / a defect) can never drive the concurrency gauge
    # negative — it floors at 0 (DEC-GROWW-RATE-ACHIEVED-METER-001 hardening).
    meter = CallMeter(clock=_Clock(), emit_log=False)
    meter.stop(elapsed_ms=1.0, completed=False)
    assert meter.snapshot()["in_flight"] == 0


def test_in_flight_balances_under_concurrency() -> None:
    # The meter is SHARED across the fetch pool — every start/stop must be lock-safe so
    # ``in_flight`` returns to exactly 0 under interleaving (a lost ``+= 1``/``-= 1``
    # update without the lock would leave it non-zero). This is the thread-safety proof
    # the "thread-safe" headline claim needs.
    meter = CallMeter(clock=_Clock(), emit_log=False)
    workers, iterations = 8, 250

    def _hammer() -> None:
        for _ in range(iterations):
            meter.start()
            meter.stop(elapsed_ms=1.0, completed=True)

    threads = [threading.Thread(target=_hammer) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    snap = meter.snapshot()
    assert snap["in_flight"] == 0  # no lost update across 2000 interleaved brackets
    assert snap["rps_1s_max"] <= workers * iterations  # the peak can never exceed issued


# ════════════════════════════════════════════════════════════════════════
# Lifetime issued/completed/failed totals + failed_rps (DEC-PULL-TELEMETRY-TRIAD-001)
# ════════════════════════════════════════════════════════════════════════


def test_lifetime_totals_count_issued_completed_and_failed() -> None:
    """``issued``/``completed``/``failed`` are lifetime totals: 5 issued = 3 ok + 2 failed."""
    meter = CallMeter(clock=_Clock(), emit_log=False)
    for _ in range(3):
        _record(meter, completed=True)
    for _ in range(2):
        _record(meter, completed=False)
    snap = meter.snapshot()
    assert snap["issued"] == 5
    assert snap["completed"] == 3
    assert snap["failed"] == 2


def test_lifetime_totals_never_reset_after_window_eviction() -> None:
    """The rolling windows evict; the LIFETIME totals do not (the 06:00-blip accounting)."""
    clock = _Clock()
    meter = CallMeter(clock=clock, emit_log=False)
    _record(meter, completed=False)
    _record(meter, completed=True)
    clock.advance(ACHIEVED_WINDOW_S + 1.0)  # both instants age out of the windows
    snap = meter.snapshot()
    assert snap["effective_rps"] == 0.0  # windowed gauges evicted
    assert snap["failed_rps"] == 0.0
    assert snap["issued"] == 2  # lifetime totals survive
    assert snap["completed"] == 1
    assert snap["failed"] == 1


def test_failed_rps_is_a_trailing_window_average_with_eviction() -> None:
    """``failed_rps`` mirrors ``effective_rps``: failures-in-window / the FIXED window."""
    clock = _Clock()
    meter = CallMeter(clock=clock, emit_log=False)
    for _ in range(5):
        _record(meter, completed=False)
    snap = meter.snapshot()
    assert snap["failed_rps"] == round(5 / ACHIEVED_WINDOW_S, 2)
    assert snap["effective_rps"] == 0.0  # failures never feed the achieved gauges
    clock.advance(ACHIEVED_WINDOW_S + 0.5)
    assert meter.snapshot()["failed_rps"] == 0.0  # aged out


def test_failed_stop_evicts_stale_completions_too() -> None:
    """A failed stop runs the SAME eviction, so a later snapshot never over-reads."""
    clock = _Clock()
    meter = CallMeter(clock=clock, emit_log=False)
    _record(meter, completed=True)
    clock.advance(ACHIEVED_WINDOW_S + 1.0)
    _record(meter, completed=False)  # the failed bracket evicts the stale completion
    snap = meter.snapshot()
    assert snap["effective_rps"] == 0.0
    assert snap["failed_rps"] == round(1 / ACHIEVED_WINDOW_S, 2)


def test_existing_snapshot_keys_are_byte_identical_under_mixed_traffic() -> None:
    """PARITY PIN (Session-B contract): the six pre-existing keys read the SAME values a
    pre-DEC-PULL-TELEMETRY-TRIAD-001 meter produced for the same traffic — the new keys
    are STRICTLY additive (failures still never feed the achieved gauges)."""
    clock = _Clock()
    meter = CallMeter(clock=clock, emit_log=False)
    _record(meter, elapsed_ms=100.0, completed=True)
    _record(meter, elapsed_ms=999.0, completed=False)  # failed latency NEVER recorded
    clock.advance(0.5)
    _record(meter, elapsed_ms=200.0, completed=True)
    snap = meter.snapshot()
    legacy = {
        k: snap[k]
        for k in (
            "effective_rps",
            "rate_achieved",
            "rps_1s",
            "rps_1s_max",
            "latency_ms_p95",
            "in_flight",
        )
    }
    assert legacy == {
        "effective_rps": round(2 / ACHIEVED_WINDOW_S, 2),
        "rate_achieved": round(2 / ACHIEVED_WINDOW_S, 2),
        "rps_1s": 2,
        "rps_1s_max": 2,
        "latency_ms_p95": 200.0,  # p95 over {100, 200} — the failed 999 ms is absent
        "in_flight": 0,
    }
    assert set(snap) - set(legacy) == {"issued", "completed", "failed", "failed_rps"}
