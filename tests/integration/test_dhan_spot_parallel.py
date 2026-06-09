"""Parallel spot-ingest fan-out — throughput, rate-cap, idempotence, pull_id.

DEC-DHAN-PARALLEL-INGEST-001. These tests prove the OUTER ``(instrument,
interval)`` fan-out in :func:`brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest`
**targets/saturates UP TO ~5 r/s** when Dhan is healthy (vs the old ~1 r/s
sequential stream) WITHOUT exceeding Dhan's 5 r/s ceiling (Rule #6), and that
the per-pair quarantine + idempotence + ``pull_id`` correlation survive the
fan-out — all deterministically, with NO real network and NO real sleeping.

The throughput proof is a **discrete-event simulator** (:class:`_SimPool`, a
synchronous :class:`~brutex.orchestrator.universe_phases.RunParallel`): it runs
the real fetcher (so the whole ``run_dhan_spot_ingest`` → ``ingest_spot`` →
adapter wiring is exercised) but models the timing — ``max_workers`` slots + a
token-bucket rate gate (tokens refill at ``rate``/s; a call starts only when a
slot is free AND a token is available; the call holds the slot for ``latency``
virtual seconds). The achieved r/s is then ``n_calls / virtual_wall``, which by
Little's law converges to ``min(rate, max_workers / latency)``. With one worker
that is latency-bound (~1/latency r/s); with
``workers_to_saturate(5, latency)`` workers the rate bucket is the bottleneck
(~5 r/s). Honest envelope (Golden Rule #10): this is the *mechanism* proof —
the real run cannot guarantee a constant 5 r/s (429 step-down + latency spikes).
"""

from __future__ import annotations

import heapq
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.adapters.dhan.errors import DhanNotFoundError
from brutex.adapters.dhan.rate_control import BURST, CEILING_RPS, DhanRateController
from brutex.adapters.dhan.run_ingest import run_dhan_spot_ingest
from brutex.data.models import CandleBar
from brutex.data.sink import NullBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.observability.correlation import pull_id_scope
from brutex.orchestrator.parallel import workers_to_saturate
from brutex.orchestrator.universe_phases import ItemResult
from structlog.testing import capture_logs

_START = date(2021, 1, 1)
_END = date(2021, 2, 1)


def _index(symbol: str, *, token: int = 1) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=token,
        symbol_name=symbol,
        display_name=None,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


#: One in-session 1-minute bar so a FETCHED window is marked complete — the ingest
#: marks a window only when the vendor returned bars (DEC-LAKE-RECONCILE-001; an
#: empty 0-bar response is left un-marked → re-pulled). The :class:`NullBarSink`
#: discards it, so call cadence is unchanged; only the coverage MARK (and thus the
#: idempotent-re-pull skip) depends on ``raw_bars > 0``.
_ONE_BAR = CandleBar(
    timestamp=datetime(2021, 1, 4, 4, 5, tzinfo=UTC),  # 04:05 UTC = 09:35 IST, in session
    open=Decimal("100.00"),
    high=Decimal("110.00"),
    low=Decimal("90.00"),
    close=Decimal("105.00"),
    volume=1,
    open_interest=None,
)


class _CountingAdapter:
    """Fake spot adapter: returns ONE in-session bar per window, counting fetch calls.

    The single :class:`CandleBar` exists so a fetched window is marked complete
    (DEC-LAKE-RECONCILE-001 marks only on ``raw_bars > 0``); the
    :class:`NullBarSink` discards it, so it never affects bar counts. The timing
    is modelled by the simulator, not by this adapter (it never sleeps).
    """

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        exchange: Exchange,
        interval: str,
        start: date,
        end: date,
    ) -> list[CandleBar]:
        with self._lock:
            self.calls += 1
        del symbol, exchange, interval, start, end
        return [_ONE_BAR]


@dataclass
class _SimResult:
    """The discrete-event simulator's measured cadence over one fan-out."""

    n_calls: int
    start_span: float  # last_start - first_start (admission window)
    burst: float
    peak_in_flight: int

    @property
    def achieved_rps(self) -> float:
        """Mean call-ADMISSION rate ``(n-1) / start_span`` (0 if instant).

        The inter-arrival rate over the calls' start times — the same
        ``(N-1)/span`` measure :class:`~brutex.adapters.dhan.client.CallMeter`
        reports, so it never overstates a steady stream (apart from the small,
        amortising burst headroom). Converges to ``min(rate, workers/latency)``.
        """
        return (self.n_calls - 1) / self.start_span if self.start_span > 0.0 else 0.0

    @property
    def steady_state_rps(self) -> float:
        """The token bucket's hard upper bound ``(n - burst) / start_span``.

        At most ``burst + rate·T`` calls may be admitted in any window of length
        ``T``, so ``(n - burst)/span ≤ rate`` ALWAYS — the burst-corrected rate
        the bucket guarantees (Rule #6). For a saturated pool it equals ``rate``.
        """
        return (self.n_calls - self.burst) / self.start_span if self.start_span > 0.0 else 0.0


class _SimPool:
    """A synchronous ``RunParallel`` that MODELS rate-gated concurrent timing.

    Runs every ``fn(item)`` immediately (single-threaded, so completion order ==
    input order and no thread races), but assigns each call a virtual
    ``[start, start+latency)`` interval under two constraints — a free worker
    slot AND an available rate-bucket token — and records the peak simultaneous
    in-flight count + the total virtual wall time. This is the Little's-law
    proof harness; it does not touch the real clock.
    """

    def __init__(
        self, *, max_workers: int, rate: float, latency: float, burst: int = BURST
    ) -> None:
        self._max_workers = max_workers
        self._rate = rate
        self._latency = latency
        self._burst = float(burst)  # mirrors the real AdaptiveTokenBucket burst (5)
        self.result: _SimResult | None = None

    def __call__[ItemT, ResultT](
        self,
        fn: Callable[[ItemT], ResultT],
        items: Sequence[ItemT],
    ) -> Iterator[ItemResult[ItemT, ResultT]]:
        # Worker slots become free at these virtual times (all free at t=0).
        free_at = [0.0] * self._max_workers
        # Token-bucket model (mirrors AdaptiveTokenBucket): a burst of ``_burst``
        # tokens at t=0, refilling at ``rate``/s — the shared rate gate.
        tokens = self._burst
        last_refill = 0.0
        starts: list[float] = []
        ends: list[float] = []
        outcomes: list[ItemResult[ItemT, ResultT]] = []
        for item in items:
            # Earliest a worker slot frees up.
            heapq.heapify(free_at)
            slot_free = free_at[0]
            now = max(slot_free, last_refill)
            # Refill tokens to ``now`` then wait until one is available.
            tokens = min(self._burst, tokens + (now - last_refill) * self._rate)
            last_refill = now
            if tokens < 1.0:
                wait = (1.0 - tokens) / self._rate
                now += wait
                tokens = min(self._burst, tokens + wait * self._rate)
                last_refill = now
            tokens -= 1.0
            # This call occupies the freed slot for ``latency`` virtual seconds.
            heapq.heapreplace(free_at, now + self._latency)
            starts.append(now)
            ends.append(now + self._latency)
            try:
                value = fn(item)
            except Exception as exc:  # captured into ItemResult like the real pool
                outcomes.append(ItemResult(item=item, ok=False, result=None, exc=exc))
            else:
                outcomes.append(ItemResult(item=item, ok=True, result=value, exc=None))
        # Peak in-flight = max overlap of the [start, end) intervals.
        peak = _peak_overlap(starts, ends)
        span = (max(starts) - min(starts)) if starts else 0.0
        self.result = _SimResult(
            n_calls=len(starts), start_span=span, burst=self._burst, peak_in_flight=peak
        )
        return iter(outcomes)


def _peak_overlap(starts: list[float], ends: list[float]) -> int:
    """Max number of half-open ``[start, end)`` intervals overlapping at once."""
    events: list[tuple[float, int]] = []
    for s, e in zip(starts, ends, strict=True):
        events.append((s, 1))
        events.append((e, -1))
    # Process ends (-1) before starts (+1) at the same instant (half-open).
    events.sort(key=lambda ev: (ev[0], ev[1]))
    cur = peak = 0
    for _t, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


# 1-minute spot over a month tiles to a single 90-day window, so each
# (instrument, interval) pair issues exactly ONE fetch — handy for counting.
_LATENCY_S = 2.0  # modelled per-call round-trip
_RATE = CEILING_RPS  # 5.0


def _many_instruments(n: int) -> list[Instrument]:
    return [_index(f"SYM{i}", token=i + 1) for i in range(n)]


class TestThroughputSaturation:
    """Parallel saturates toward ~5 r/s; the 1-worker control is ~1/latency r/s."""

    def test_parallel_saturates_toward_ceiling_vs_sequential(self) -> None:
        # Enough pairs that the steady-state rate dominates the burst/edge.
        n = 200
        instruments = _many_instruments(n)
        intervals = ["1minute"]  # 1 window/pair → n fetches total

        # CONTROL: one worker — latency-bound, ~1/latency = 0.5 r/s here.
        control_adapter = _CountingAdapter()
        control_pool = _SimPool(max_workers=1, rate=_RATE, latency=_LATENCY_S)
        run_dhan_spot_ingest(
            control_adapter,  # type: ignore[arg-type]
            NullBarSink(),
            instruments=instruments,
            intervals=intervals,
            start=_START,
            end=_END,
            run_parallel=control_pool,
        )
        assert control_adapter.calls == n
        assert control_pool.result is not None
        control_rps = control_pool.result.achieved_rps
        assert control_pool.result.peak_in_flight == 1
        # ~1/latency = 0.5 r/s, never above 1 (a single worker can't pipeline).
        assert control_rps == pytest.approx(1.0 / _LATENCY_S, rel=0.05)

        # PARALLEL: pool sized to saturate the bucket (5 r/s × 2 s = 10 workers).
        max_workers = workers_to_saturate(_RATE, _LATENCY_S)
        assert max_workers == 10
        parallel_adapter = _CountingAdapter()
        parallel_pool = _SimPool(max_workers=max_workers, rate=_RATE, latency=_LATENCY_S)
        run_dhan_spot_ingest(
            parallel_adapter,  # type: ignore[arg-type]
            NullBarSink(),
            instruments=instruments,
            intervals=intervals,
            start=_START,
            end=_END,
            run_parallel=parallel_pool,
        )
        assert parallel_adapter.calls == n
        assert parallel_pool.result is not None
        parallel_rps = parallel_pool.result.achieved_rps
        # The pool fills to its width (Little's law: all slots busy in steady state).
        assert parallel_pool.result.peak_in_flight == max_workers
        # Achieved rate trends to ~5 r/s — an order of magnitude over control.
        assert parallel_rps == pytest.approx(_RATE, rel=0.05)
        assert parallel_rps > control_rps * 5.0
        # The burst-corrected steady-state rate NEVER exceeds the 5 r/s ceiling
        # (Rule #6) — the token bucket is the governor, not the worker count.
        assert parallel_pool.result.steady_state_rps <= _RATE + 1e-9

    def test_rate_never_exceeds_ceiling_at_max_fanout(self) -> None:
        # Even over-provisioned to the 32-socket cap, the modelled rate gate
        # holds the steady-state rate at/under 5 r/s (Rule #6 guard).
        instruments = _many_instruments(200)
        adapter = _CountingAdapter()
        pool = _SimPool(max_workers=32, rate=_RATE, latency=_LATENCY_S)
        run_dhan_spot_ingest(
            adapter,  # type: ignore[arg-type]
            NullBarSink(),
            instruments=instruments,
            intervals=["1minute"],
            start=_START,
            end=_END,
            run_parallel=pool,
        )
        assert pool.result is not None
        # 32 workers over-provisions (10 saturate) — the bucket still caps it.
        assert pool.result.steady_state_rps <= _RATE + 1e-9
        # And the achieved admission rate is right at the 5 r/s ceiling.
        assert pool.result.achieved_rps == pytest.approx(_RATE, rel=0.05)


class TestRateCapUnderRealConcurrency:
    """The shared token bucket holds ≤ 5 r/s even with 32 workers pacing at once.

    Uses the REAL DhanRateController + AdaptiveTokenBucket over a VIRTUAL clock
    (injected clock/sleep), driven from many real threads, and asserts the
    measured physical inter-arrival rate never exceeds the 5 r/s ceiling.
    """

    def test_32_workers_paced_rate_within_ceiling(self) -> None:
        # A virtual clock the bucket reads; sleeps advance it. A lock serialises
        # the clock so the bucket's own lock is the only rate gate under test.
        clock_lock = threading.Lock()
        now = [1000.0]

        def clock() -> float:
            with clock_lock:
                return now[0]

        def sleep(seconds: float) -> None:
            with clock_lock:
                now[0] += seconds

        controller = DhanRateController(clock=clock, sleep=sleep)
        n_workers = 32
        per_worker = 10
        barrier = threading.Barrier(n_workers)
        start_times: list[float] = []
        rec_lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            for _ in range(per_worker):
                controller.pace()
                with rec_lock:
                    start_times.append(clock())

        threads = [threading.Thread(target=worker) for _ in range(n_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        total = n_workers * per_worker
        assert len(start_times) == total
        span = max(start_times) - min(start_times)
        # Token-bucket guarantee: at most ``burst + rate·T`` calls are admitted
        # in any window of length T, so the burst-corrected rate
        # ``(N - burst)/span`` NEVER exceeds the 5 r/s ceiling (Rule #6) — even
        # with all 32 workers pacing on the SAME thread-safe bucket at once.
        assert span > 0.0
        steady_rps = (total - BURST) / span
        assert steady_rps <= CEILING_RPS + 1e-9


class TestIdempotenceUnderParallel:
    """A re-run over the same range yields zero net-new bars (Rule #7)."""

    def test_second_parallel_pull_is_a_noop(self) -> None:
        from brutex.data.window_ledger import WindowLedger

        instruments = _many_instruments(8)
        intervals = ["1minute", "1day"]
        ledger = WindowLedger()

        def _pull(adapter: _CountingAdapter) -> None:
            pool = _SimPool(max_workers=10, rate=_RATE, latency=0.5)
            run_dhan_spot_ingest(
                adapter,  # type: ignore[arg-type]
                NullBarSink(),
                instruments=instruments,
                intervals=intervals,
                start=_START,
                end=_END,
                ledger=ledger,
                run_parallel=pool,
            )

        first = _CountingAdapter()
        _pull(first)
        first_rows = ledger.to_rows()
        assert first.calls > 0  # the first pull fetched every window
        # SECOND pull over the same range + shared ledger: every window is now
        # settled SUCCESS, so NOT A SINGLE vendor fetch is issued (quota-frugal,
        # idempotent — the ledger is the SOLE skip authority, short-circuiting
        # before the call, DEC-WINDOW-LEDGER-001 b3).
        second = _CountingAdapter()
        _pull(second)
        assert second.calls == 0
        # The ledger is byte-identical (deterministic serialisation) — no new state.
        assert ledger.to_rows() == first_rows


class TestQuarantineUnderParallel:
    """One terminal-DhanError pair is quarantined; the rest succeed, run completes."""

    def test_one_bad_symbol_quarantined_rest_succeed(self) -> None:
        good = _many_instruments(5)
        bad = _index("BAD", token=999)

        class _OneBadAdapter:
            def fetch_ohlcv(
                self,
                *,
                symbol: str,
                exchange: Exchange,
                interval: str,
                start: date,
                end: date,
            ) -> list[CandleBar]:
                del exchange, interval, start, end
                if symbol == "BAD":
                    raise DhanNotFoundError("HTTP 404 unknown security")
                return []

        pool = _SimPool(max_workers=10, rate=_RATE, latency=0.5)
        with capture_logs() as logs, pull_id_scope("spotparallel1"):
            report = run_dhan_spot_ingest(
                _OneBadAdapter(),  # type: ignore[arg-type]
                NullBarSink(),
                instruments=[*good, bad],
                intervals=["1minute"],
                start=_START,
                end=_END,
                run_parallel=pool,
            )
        # The bad pair is quarantined; the 5 good pairs completed; run finished.
        assert report.failed_pairs == 1
        assert report.failures[0].symbol == "BAD"
        assert len(report.pairs) == 5
        quarantines = [e for e in logs if e["event"] == "ingest.quarantine"]
        assert len(quarantines) == 1
        assert quarantines[0]["symbol"] == "BAD"
        assert quarantines[0]["pull_id"] == "spotparallel1"
