"""Unit tests for the multi-connection :class:`ClientPool` (DEC-GROWW-CLIENT-POOL-001).

The pool's whole reason to exist is *measured*, not asserted by hope: the live logs showed
the fan-out workers serialising on ONE shared SDK socket (avg 0.49 in-flight → ~3.4 r/s).
These tests PROVE — deterministically, no sleeps — that K independent connections give K-way
concurrency (a shared :class:`threading.Barrier` of K can only release if K calls are truly
simultaneously in-flight) while ONE connection serialises (a per-connection lock pins the
observed concurrency to 1). They also cover thread-affinity dispatch, the shared aggregate
meter, ``with_pace`` re-fanning, delegation of all three calls, and the constructor guards.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor

import pytest
from brutex.adapters.call_meter import CallMeter
from brutex.adapters.groww.client import GrowwClient, GrowwClientProtocol
from brutex.adapters.groww.pool import ClientPool

_HIST_KW = {
    "exchange": "NSE",
    "segment": "CASH",
    "groww_symbol": "NSE-NIFTY",
    "interval": "1day",
    "start": "2024-01-01T00:00:00",
    "end": "2024-02-01T00:00:00",
}


class _RecordingSDK:
    """A fake GrowwSDK that records how many calls it served (per-connection counter)."""

    def __init__(self) -> None:
        self.candle_calls = 0
        self.expiry_calls = 0
        self.contract_calls = 0

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:
        self.candle_calls += 1
        return {"candles": []}

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:
        self.expiry_calls += 1
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:
        self.contract_calls += 1
        return {"contracts": []}


class _BarrierSDK:
    """A connection whose call only returns once K calls are SIMULTANEOUSLY in-flight.

    Shared :class:`threading.Barrier` of K: ``wait`` releases iff K threads reach it at the
    same time. If the pool serialised (a single connection), at most one call is ever in
    flight and the barrier times out (``BrokenBarrierError``) — so a clean return is PROOF of
    real K-way concurrency, deterministically and with no sleep.
    """

    def __init__(self, barrier: threading.Barrier) -> None:
        self._barrier = barrier

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:
        self._barrier.wait(timeout=5.0)
        return {"candles": []}

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover - unused
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover - unused
        return {"contracts": []}


class _ConcurrencyProbe:
    """Shared max-concurrency observer (guards a current/peak counter)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current = 0
        self.peak = 0


class _SerialConnSDK:
    """ONE connection: a per-instance lock serialises its OWN calls (a single socket).

    Records peak concurrency into a SHARED probe, so a 1-connection pool driven by N threads
    shows peak == 1 (the real starve), and an independent-connection pool shows peak == K.
    """

    def __init__(self, probe: _ConcurrencyProbe, release: threading.Event) -> None:
        self._probe = probe
        self._release = release
        self._conn_lock = threading.Lock()

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:
        with self._conn_lock:  # one connection handles one call at a time
            with self._probe.lock:
                self._probe.current += 1
                self._probe.peak = max(self._probe.peak, self._probe.current)
            self._release.wait(timeout=5.0)  # hold the call open to widen the overlap window
            with self._probe.lock:
                self._probe.current -= 1
        return {"candles": []}

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover - unused
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover - unused
        return {"contracts": []}


def _pool_of(*sdks: object, meter: CallMeter | None = None) -> ClientPool:
    """Build a ClientPool whose K leaves each wrap one fake SDK + share ONE meter."""
    shared = meter if meter is not None else CallMeter(emit_log=False)
    leaves = [GrowwClient(sdk=sdk, sleep=lambda _s: None, meter=shared) for sdk in sdks]  # type: ignore[arg-type]
    return ClientPool(leaves)


# ── the core proof: K independent connections run K calls in parallel ───────────────────


def test_k_independent_connections_run_in_parallel() -> None:
    """K=3 distinct connections → 3 calls truly simultaneously in-flight (barrier releases)."""
    barrier = threading.Barrier(3)
    pool = _pool_of(_BarrierSDK(barrier), _BarrierSDK(barrier), _BarrierSDK(barrier))
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(lambda: pool.historical_candles(**_HIST_KW)) for _ in range(3)]
        results = [f.result(timeout=10.0) for f in futures]
    # All three returned cleanly → the barrier(3) released → 3 calls overlapped (no serialise).
    assert results == [{"candles": []}, {"candles": []}, {"candles": []}]
    assert not barrier.broken


def test_single_connection_serialises_baseline() -> None:
    """K=1 connection driven by 4 threads → peak concurrency 1 (the ~3.4 r/s starve)."""
    probe = _ConcurrencyProbe()
    release = threading.Event()
    pool = _pool_of(_SerialConnSDK(probe, release))  # ONE leaf
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(lambda: pool.historical_candles(**_HIST_KW)) for _ in range(4)]
        # Give the threads a beat to pile up on the single connection, then let them drain.
        import time

        time.sleep(0.05)
        release.set()
        for f in futures:
            f.result(timeout=10.0)
    assert probe.peak == 1  # the single shared connection admitted only one call at a time


def test_pool_meter_aggregates_across_connections() -> None:
    """All leaves share ONE meter, so completions across connections sum into one gauge."""
    meter = CallMeter(emit_log=False)
    pool = _pool_of(_RecordingSDK(), _RecordingSDK(), meter=meter)
    assert pool.meter is meter
    with ThreadPoolExecutor(max_workers=2) as ex:
        for f in [ex.submit(lambda: pool.historical_candles(**_HIST_KW)) for _ in range(6)]:
            f.result(timeout=10.0)
    snap = meter.snapshot()
    # 6 successful calls recorded into the ONE shared meter (the aggregate the dashboard reads).
    assert snap["effective_rps"] > 0.0
    assert snap["in_flight"] == 0  # every start was paired with a stop


# ── thread-affinity dispatch ────────────────────────────────────────────────────────────


def test_thread_affinity_same_thread_same_connection() -> None:
    """Repeated calls on ONE thread reuse ONE connection (keep-alive); distinct threads spread."""
    sdks = [_RecordingSDK(), _RecordingSDK()]
    pool = _pool_of(*sdks)
    # Five calls all on the CURRENT thread → all served by a single leaf (affinity).
    for _ in range(5):
        pool.historical_candles(**_HIST_KW)
    served = [s.candle_calls for s in sdks]
    assert sorted(served) == [0, 5]  # one connection took all 5; the other none


def test_round_robin_assigns_distinct_connections_to_distinct_threads() -> None:
    """The first K distinct threads each claim a DISTINCT connection (even spread)."""
    sdks = [_RecordingSDK(), _RecordingSDK(), _RecordingSDK()]
    pool = _pool_of(*sdks)
    barrier = threading.Barrier(3)

    def _one() -> None:
        barrier.wait(timeout=5.0)  # hold all three threads in flight so each claims a leaf
        pool.historical_candles(**_HIST_KW)

    with ThreadPoolExecutor(max_workers=3) as ex:
        for f in [ex.submit(_one) for _ in range(3)]:
            f.result(timeout=10.0)
    # Each connection served exactly one call → the three threads spread 1:1 across leaves.
    assert sorted(s.candle_calls for s in sdks) == [1, 1, 1]


# ── with_pace re-fans to every leaf ─────────────────────────────────────────────────────


def test_with_pace_rebinds_every_leaf_and_keeps_meter() -> None:
    """``with_pace`` returns a new pool whose EVERY leaf is re-paced; the meter is preserved."""
    meter = CallMeter(emit_log=False)
    pool = _pool_of(_RecordingSDK(), _RecordingSDK(), meter=meter)
    hits: list[str] = []

    def _pace() -> None:
        hits.append("x")

    repaced = pool.with_pace(_pace)
    assert isinstance(repaced, ClientPool)
    assert repaced.meter is meter  # the shared aggregate carries through
    assert repaced.size == 2
    # Drive every leaf so each one's rebound pace fires (one call per connection).
    barrier = threading.Barrier(2)

    def _one() -> None:
        barrier.wait(timeout=5.0)
        repaced.historical_candles(**_HIST_KW)

    with ThreadPoolExecutor(max_workers=2) as ex:
        for f in [ex.submit(_one) for _ in range(2)]:
            f.result(timeout=10.0)
    assert len(hits) == 2  # pace fired on BOTH connections (re-fanned), not just one


# ── delegation of expiries / contracts ──────────────────────────────────────────────────


def test_pool_delegates_expiries_and_contracts() -> None:
    """``expiries`` / ``contracts`` delegate to the thread's leaf and return its payload."""
    sdk = _RecordingSDK()
    pool = _pool_of(sdk)
    assert pool.expiries(exchange="NSE", underlying_symbol="NIFTY") == {"expiries": []}
    assert pool.contracts(exchange="NSE", underlying_symbol="NIFTY", expiry_date="2024-01-25") == {
        "contracts": []
    }
    assert sdk.expiry_calls == 1 and sdk.contract_calls == 1


def test_pool_satisfies_client_protocol() -> None:
    """The pool is a structural :class:`GrowwClientProtocol` (drop-in for a single client)."""
    pool = _pool_of(_RecordingSDK())
    assert isinstance(pool, GrowwClientProtocol)


def test_size_reports_connection_count() -> None:
    pool = _pool_of(_RecordingSDK(), _RecordingSDK(), _RecordingSDK())
    assert pool.size == 3


def test_pool_self_paces_only_when_every_leaf_is_paced() -> None:
    """``ClientPool.self_paces`` is True iff EVERY leaf carries a real pace hook.

    The single-admission seam (DEC-PULL-FORENSIC-WAVE1-001): the ingest spines drop
    the per-window admission only when NO connection can bypass the shared bucket.
    """
    pool = _pool_of(_RecordingSDK(), _RecordingSDK())
    assert pool.self_paces is False  # default leaves use the no-op pace sentinel
    assert pool.with_pace(lambda: None).self_paces is True  # all leaves re-paced


# ── constructor guards (fail loud on misuse) ────────────────────────────────────────────


def test_empty_pool_rejected() -> None:
    with pytest.raises(ValueError, match="at least one client"):
        ClientPool([])


def test_mismatched_meters_rejected() -> None:
    """Leaves that do NOT share one meter would split the gauge → rejected at construction."""
    a = GrowwClient(sdk=_RecordingSDK(), meter=CallMeter(emit_log=False))  # type: ignore[arg-type]
    b = GrowwClient(sdk=_RecordingSDK(), meter=CallMeter(emit_log=False))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="share ONE CallMeter"):
        ClientPool([a, b])


# ── G1: exception transparency — a raising leaf propagates, releases the meter, keeps affinity ──


class _RaisingSDK:
    """A connection whose historical_candles ALWAYS raises a terminal error."""

    def __init__(self) -> None:
        self.calls = 0

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:
        self.calls += 1
        raise ValueError("vendor said no")  # unknown → terminal GrowwError (not retryable)

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover - unused
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover - unused
        return {"contracts": []}


def test_pool_propagates_leaf_error_releases_meter_keeps_affinity() -> None:
    """A leaf error propagates through the pool, the in-flight slot is released, affinity holds."""
    from brutex.adapters.groww.errors import GrowwError

    meter = CallMeter(emit_log=False)
    sdk = _RaisingSDK()
    pool = _pool_of(sdk, meter=meter)
    with pytest.raises(GrowwError):
        pool.historical_candles(**_HIST_KW)
    # The failed call released its in-flight slot (the gauge can't leak on an exception).
    assert meter.snapshot()["in_flight"] == 0
    # Affinity survives the raise: the SAME thread still routes to the SAME (only) leaf.
    with pytest.raises(GrowwError):
        pool.historical_candles(**_HIST_KW)
    assert sdk.calls == 2 and meter.snapshot()["in_flight"] == 0


# ── G2: the aggregate rate gate is SHARED across every connection (Rule 6, proven not prose) ──


def test_every_connection_paces_through_the_one_shared_gate() -> None:
    """Every physical call on EVERY leaf goes through the ONE shared pace hook (no bypass)."""
    lock = threading.Lock()
    paced: list[int] = []

    def _pace() -> None:
        with lock:
            paced.append(threading.get_ident())

    pool = _pool_of(_RecordingSDK(), _RecordingSDK()).with_pace(_pace)
    with ThreadPoolExecutor(max_workers=3) as ex:
        for f in [ex.submit(lambda: pool.historical_candles(**_HIST_KW)) for _ in range(9)]:
            f.result(timeout=10.0)
    # All 9 calls across the 2 connections were gated by the single shared bucket — K
    # connections add concurrency, never extra rate (CLAUDE.md §2 Rule 6).
    assert len(paced) == 9


# ── G3: the PRODUCTION default size (K=2) runs two calls in parallel ─────────────────────


def test_two_connections_run_in_parallel() -> None:
    """K=2 (the default pool size) → two calls truly simultaneously in-flight."""
    barrier = threading.Barrier(2)
    pool = _pool_of(_BarrierSDK(barrier), _BarrierSDK(barrier))
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = [
            f.result(timeout=10.0)
            for f in [ex.submit(lambda: pool.historical_candles(**_HIST_KW)) for _ in range(2)]
        ]
    assert results == [{"candles": []}, {"candles": []}] and not barrier.broken


# ── G4: oversubscription (N threads > K connections) spreads evenly (round-robin wrap) ───


def test_oversubscription_spreads_evenly_across_connections() -> None:
    """6 threads on a 2-connection pool → round-robin wraps to an even 3/3 spread."""
    sdks = [_RecordingSDK(), _RecordingSDK()]
    pool = _pool_of(*sdks)
    barrier = threading.Barrier(6)

    def _one() -> None:
        barrier.wait(timeout=5.0)  # hold all 6 in flight so each claims a round-robin slot
        pool.historical_candles(**_HIST_KW)

    with ThreadPoolExecutor(max_workers=6) as ex:
        for f in [ex.submit(_one) for _ in range(6)]:
            f.result(timeout=10.0)
    # 6 distinct threads round-robin'd 0,1,0,1,0,1 across 2 leaves → exactly 3 calls each
    # (the bounded cursor wraps; no leaf re-serialises more than its fair share).
    assert sorted(s.candle_calls for s in sdks) == [3, 3]


# ── G6/G7: with_pace chained twice rebinds (not stacks) and keeps every leaf's shared meter ──


def test_with_pace_twice_rebinds_and_keeps_shared_meter() -> None:
    """Chained `with_pace` rebinds the latest pace on every leaf; the meter stays shared."""
    meter = CallMeter(emit_log=False)
    pool = _pool_of(_RecordingSDK(), _RecordingSDK(), meter=meter)
    first: list[str] = []
    second: list[str] = []
    repaced = pool.with_pace(lambda: first.append("a")).with_pace(lambda: second.append("b"))
    assert repaced.meter is meter
    # Every leaf of the twice-rebuilt pool still shares the ONE aggregate meter.
    assert all(leaf.meter is meter for leaf in repaced._clients)
    barrier = threading.Barrier(2)

    def _one() -> None:
        barrier.wait(timeout=5.0)
        repaced.historical_candles(**_HIST_KW)

    with ThreadPoolExecutor(max_workers=2) as ex:
        for f in [ex.submit(_one) for _ in range(2)]:
            f.result(timeout=10.0)
    assert len(second) == 2 and first == []  # the LATEST pace fired; the first was rebound away


def test_pool_self_reports_only_when_every_leaf_is_hooked() -> None:
    """``self_reports_rate_limits`` / ``self_reports_successes`` — the feedback
    twins of ``self_paces`` (DEC-SUSPICION-SWEEP-FIXES-001): True iff EVERY leaf
    carries the bound hook, so the ingest spines stand their own feedback down
    only when NO connection's 429s/successes can go unreported."""
    pool = _pool_of(_RecordingSDK(), _RecordingSDK())
    assert pool.self_reports_rate_limits is False
    assert pool.self_reports_successes is False
    rebound = pool.with_rate_hooks(on_rate_limit=lambda: None, on_success=lambda: None)
    assert rebound.self_reports_rate_limits is True
    assert rebound.self_reports_successes is True
    assert rebound.self_paces is False  # pace untouched (None kept the no-op)
    # A mixed pool (one hooked leaf, one not) must read False (all-leaves rule).
    shared = CallMeter(emit_log=False)
    mixed = ClientPool(
        [
            GrowwClient(sdk=_RecordingSDK(), on_rate_limit=lambda: None, meter=shared),  # type: ignore[arg-type]
            GrowwClient(sdk=_RecordingSDK(), meter=shared),  # type: ignore[arg-type]
        ]
    )
    assert mixed.self_reports_rate_limits is False
    assert mixed.self_reports_successes is False


def test_pool_with_rate_hooks_fans_triple_to_every_leaf_and_keeps_meter() -> None:
    """``with_rate_hooks`` re-fans the FULL controller hook triple into ALL K
    leaves (the spot-spine D1 seam) and preserves the ONE shared meter."""
    pool = _pool_of(_RecordingSDK(), _RecordingSDK(), _RecordingSDK())
    rebound = pool.with_rate_hooks(
        pace=lambda: None, on_rate_limit=lambda: None, on_success=lambda: None
    )
    assert rebound.self_paces is True
    assert rebound.self_reports_rate_limits is True
    assert rebound.self_reports_successes is True
    assert rebound.size == 3
    assert rebound.meter is pool.meter  # the shared aggregate meter survives the rebind
