"""Per-window parallel fan-out for ONE series — throughput, idempotence, order.

DEC-DHAN-PARALLEL-WINDOWS-001. PR #341 parallelised the OUTER ``(instrument,
interval)`` loop but deliberately left the per-window spine of a SINGLE series
sequential, so a single-symbol spot pull ran at ~0.5 r/s (each 90-day window
waited for the previous round-trip: measured SENSEX 2021→2026, 23 windows,
~43 s). These tests prove the WINDOWS of one series now fan out through the
injected :class:`~brutex.orchestrator.universe_phases.RunParallel` so a single
symbol **targets/saturates UP TO ~5 r/s** when Dhan is healthy WITHOUT exceeding
the 5 r/s ceiling (Rule #6) — deterministically, with NO real network and NO
real sleeping.

The throughput proof reuses the discrete-event simulator pattern from
``test_dhan_spot_parallel.py`` (:class:`_SimPool`): it runs the real
``ingest_spot`` → adapter → sink wiring (so the whole spine is exercised) but
MODELS the timing — ``max_workers`` slots + a token-bucket rate gate. By
Little's law the achieved admission rate converges to
``min(rate, max_workers / latency)``: one worker is latency-bound (~1/latency
r/s, the old sequential spine), and ``workers_to_saturate(5, latency)`` workers
make the rate bucket the bottleneck (~5 r/s).

**Honest envelope (Golden Rule #10):** this is the *mechanism* proof — it shows
the fan-out lets the shared rate bucket, not a single-threaded window loop, be
the governor. The real run cannot guarantee a constant 5 r/s (Dhan's cap + 429
step-down + per-call latency spikes); the throughput is network-bound, not O(1).

Whitebox: ``TestDefaultPoolSizing`` exercises the private ``_pace_rps`` sizing
helper, so per the DEC-P0-017 convention the file silences ``reportPrivateUsage``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import heapq
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.dhan.errors import DhanNotFoundError
from brutex.adapters.dhan.ingest import ingest_spot
from brutex.adapters.dhan.rate_control import BURST, CEILING_RPS, DhanRateController
from brutex.data.fetch_coverage import FetchCoverage
from brutex.data.models import CandleBar
from brutex.data.sink import NullBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.observability.correlation import current_pull_id, pull_id_scope
from brutex.orchestrator.parallel import workers_to_saturate
from brutex.orchestrator.universe_phases import ItemResult
from structlog.testing import capture_logs

# A wide range so the 1-minute pull tiles into MANY 90-day windows of ONE
# series (the unit the per-window fan-out parallelises). 2021-01-01 → 2026-06-03
# tiles to 22 windows — the SENSEX-scale single-symbol pull the task measured.
_START = date(2021, 1, 1)
_END = date(2026, 6, 3)
_EXPECTED_WINDOWS = 22


def _index(symbol: str = "SENSEX", *, token: int = 1) -> Instrument:
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


class _CountingAdapter:
    """Fake spot adapter: returns zero bars per window, counting fetch calls.

    Zero bars is fine — these tests measure call CADENCE, not bar counts; the
    timing is modelled by :class:`_SimPool`, never by this adapter (it never
    sleeps). The call counter is lock-guarded so a real thread-pool run counts
    correctly too.
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
        return []


@dataclass
class _SimResult:
    """The discrete-event simulator's measured cadence over one window fan-out."""

    n_calls: int
    start_span: float  # last_start - first_start (admission window)
    burst: float
    peak_in_flight: int

    @property
    def achieved_rps(self) -> float:
        """Mean call-ADMISSION rate ``(n-1) / start_span`` (0 if instant).

        The inter-arrival rate over the calls' start times — the same
        ``(N-1)/span`` measure :class:`~brutex.adapters.dhan.client.CallMeter`
        reports. Converges to ``min(rate, workers/latency)``.
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
    in-flight count + the total virtual wall time. The Little's-law proof harness;
    it does not touch the real clock. (Mirrors the proven harness in
    ``test_dhan_spot_parallel.py`` so both fan-out levels are measured the same
    way — CLAUDE.md §11 COMMON.)
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
        free_at = [0.0] * self._max_workers
        tokens = self._burst
        last_refill = 0.0
        starts: list[float] = []
        ends: list[float] = []
        outcomes: list[ItemResult[ItemT, ResultT]] = []
        for item in items:
            heapq.heapify(free_at)
            slot_free = free_at[0]
            now = max(slot_free, last_refill)
            tokens = min(self._burst, tokens + (now - last_refill) * self._rate)
            last_refill = now
            if tokens < 1.0:
                wait = (1.0 - tokens) / self._rate
                now += wait
                tokens = min(self._burst, tokens + wait * self._rate)
                last_refill = now
            tokens -= 1.0
            heapq.heapreplace(free_at, now + self._latency)
            starts.append(now)
            ends.append(now + self._latency)
            try:
                value = fn(item)
            except Exception as exc:  # captured into ItemResult like the real pool
                outcomes.append(ItemResult(item=item, ok=False, result=None, exc=exc))
            else:
                outcomes.append(ItemResult(item=item, ok=True, result=value, exc=None))
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
    events.sort(key=lambda ev: (ev[0], ev[1]))
    cur = peak = 0
    for _t, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


_LATENCY_S = 2.0  # modelled per-call round-trip
_RATE = CEILING_RPS  # 5.0


class TestSingleSeriesWindowThroughput:
    """One series' windows fan out toward ~5 r/s; the 1-worker control is ~1/latency."""

    def test_parallel_windows_saturate_vs_sequential_spine(self) -> None:
        instrument = _index()
        intervals_windows = _EXPECTED_WINDOWS

        # CONTROL: one worker — the OLD sequential spine, latency-bound at
        # ~1/latency = 0.5 r/s (exactly the ~43 s / 22-window SENSEX measurement).
        control_adapter = _CountingAdapter()
        control_pool = _SimPool(max_workers=1, rate=_RATE, latency=_LATENCY_S)
        report = ingest_spot(
            control_adapter,  # type: ignore[arg-type]
            NullBarSink(),
            instrument=instrument,
            interval="1minute",
            start=_START,
            end=_END,
            run_parallel=control_pool,
        )
        assert report.windows == intervals_windows
        assert control_adapter.calls == intervals_windows
        assert control_pool.result is not None
        assert control_pool.result.peak_in_flight == 1  # a single window at a time
        control_rps = control_pool.result.achieved_rps
        assert control_rps == pytest.approx(1.0 / _LATENCY_S, rel=0.05)  # ~0.5 r/s

        # PARALLEL: pool sized to saturate the bucket (5 r/s × 2 s = 10 workers).
        max_workers = workers_to_saturate(_RATE, _LATENCY_S)
        assert max_workers == 10
        parallel_adapter = _CountingAdapter()
        parallel_pool = _SimPool(max_workers=max_workers, rate=_RATE, latency=_LATENCY_S)
        ingest_spot(
            parallel_adapter,  # type: ignore[arg-type]
            NullBarSink(),
            instrument=instrument,
            interval="1minute",
            start=_START,
            end=_END,
            run_parallel=parallel_pool,
        )
        assert parallel_adapter.calls == intervals_windows
        assert parallel_pool.result is not None
        parallel_rps = parallel_pool.result.achieved_rps
        # With 22 windows and 10 slots the pool fills to its full width (Little's
        # law: every slot busy while work remains) — peak in-flight == workers.
        assert parallel_pool.result.peak_in_flight == max_workers
        # Achieved rate trends toward ~5 r/s — an order of magnitude over the
        # sequential spine (the ~43 s SENSEX pull collapses toward ~10 s).
        assert parallel_rps == pytest.approx(_RATE, rel=0.1)
        assert parallel_rps > control_rps * 5.0
        # The burst-corrected steady-state rate NEVER exceeds the 5 r/s ceiling
        # (Rule #6) — the shared token bucket is the governor, not the worker count.
        assert parallel_pool.result.steady_state_rps <= _RATE + 1e-9

    def test_window_rate_never_exceeds_ceiling_at_max_fanout(self) -> None:
        # Even over-provisioned to the 32-socket cap, the modelled rate gate
        # holds one series' window rate at/under 5 r/s (Rule #6 guard).
        adapter = _CountingAdapter()
        pool = _SimPool(max_workers=32, rate=_RATE, latency=_LATENCY_S)
        ingest_spot(
            adapter,  # type: ignore[arg-type]
            NullBarSink(),
            instrument=_index(),
            interval="1minute",
            start=_START,
            end=_END,
            run_parallel=pool,
        )
        assert adapter.calls == _EXPECTED_WINDOWS
        assert pool.result is not None
        # The burst-corrected steady-state rate is the token bucket's hard
        # guarantee and NEVER exceeds 5 r/s (Rule #6). Honest envelope (Golden
        # Rule #10): on a SHORT single-symbol series (22 windows) the 5-token
        # burst is admitted at t=0, so the RAW admission count over the span can
        # momentarily read > 5 r/s — the bucket never sustains > 5 (this is why
        # the achieved-rate≈5 assertion only holds for a long stream, not a
        # 22-window pull). The steady-state bound is the correct invariant here.
        assert pool.result.steady_state_rps <= _RATE + 1e-9
        assert pool.result.achieved_rps > _RATE  # burst inflates a short series


class TestRealRateControllerAcrossWindows:
    """The shared bucket holds ≤ 5 r/s when one series' windows pace concurrently.

    Uses the REAL DhanRateController + AdaptiveTokenBucket over a VIRTUAL clock,
    driven through the real ``ingest_spot`` window fan-out on real threads, and
    asserts the measured physical inter-arrival rate never exceeds the ceiling.
    """

    def test_paced_window_fanout_within_ceiling(self) -> None:
        clock_lock = threading.Lock()
        now = [1000.0]

        def clock() -> float:
            with clock_lock:
                return now[0]

        def sleep(seconds: float) -> None:
            with clock_lock:
                now[0] += seconds

        controller = DhanRateController(clock=clock, sleep=sleep)
        starts: list[float] = []
        rec_lock = threading.Lock()

        class _PacedAdapter:
            def fetch_ohlcv(
                self,
                *,
                symbol: str,
                exchange: Exchange,
                interval: str,
                start: date,
                end: date,
            ) -> list[CandleBar]:
                del symbol, exchange, interval, start, end
                with rec_lock:
                    starts.append(clock())
                return []

        # Real thread pool sized to the controller rate; the shared bucket is the
        # only rate gate. 22 windows over a 10-wide pool exercises real contention.
        from brutex.orchestrator.parallel import PoolRunParallel

        pool = PoolRunParallel(workers_to_saturate(_RATE, _LATENCY_S))
        report = ingest_spot(
            _PacedAdapter(),  # type: ignore[arg-type]
            NullBarSink(),
            instrument=_index(),
            interval="1minute",
            start=_START,
            end=_END,
            pace=controller.pace,
            run_parallel=pool,
        )
        assert report.windows == _EXPECTED_WINDOWS
        assert len(starts) == _EXPECTED_WINDOWS
        span = max(starts) - min(starts)
        assert span > 0.0
        # Token-bucket guarantee: ``(N - burst)/span`` never exceeds 5 r/s even
        # with all window workers pacing on the SAME thread-safe bucket (Rule #6).
        steady_rps = (_EXPECTED_WINDOWS - BURST) / span
        assert steady_rps <= CEILING_RPS + 1e-9


class TestIdempotentRepullWindows:
    """A re-pull over the same series yields zero net-new window fetches (Rule #7)."""

    def test_second_window_pull_is_a_noop(self) -> None:
        coverage = FetchCoverage()

        def _pull(adapter: _CountingAdapter) -> None:
            pool = _SimPool(max_workers=10, rate=_RATE, latency=0.5)
            ingest_spot(
                adapter,  # type: ignore[arg-type]
                NullBarSink(),
                instrument=_index(),
                interval="1minute",
                start=_START,
                end=_END,
                coverage=coverage,
                run_parallel=pool,
            )

        first = _CountingAdapter()
        _pull(first)
        first_records = coverage.to_records()
        assert first.calls == _EXPECTED_WINDOWS  # every window fetched once
        assert coverage.completed_count == _EXPECTED_WINDOWS
        # SECOND pull over the same range + shared coverage: every window is now
        # marked complete, so NOT A SINGLE vendor fetch is issued — 0 net-new
        # (quota-frugal, idempotent; the ledger short-circuits before the call).
        second = _CountingAdapter()
        _pull(second)
        assert second.calls == 0
        assert coverage.to_records() == first_records  # byte-identical state


class TestConcurrentWindowWriteCorrectness:
    """Real concurrent windows of one series store the SAME bars regardless of order.

    Drives the real ``ingest_spot`` window fan-out into a REAL ParquetBarSink
    (real ParquetWriter on a tmp lake) on real threads, where adjacent 90-day
    windows straddle a month boundary and so two windows WRITE the same
    ``(year, month)`` partition concurrently. The per-partition lock
    (DEC-DHAN-PARALLEL-WINDOWS-001) + the order-independent merge mean the stored
    lake is identical to a single-threaded run — window completion order never
    affects the data (Rule #7).
    """

    def test_parallel_window_writes_match_sequential(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")  # the real ParquetWriter needs polars
        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.sink import ParquetBarSink

        # An adapter that emits one in-session bar PER DAY of the requested
        # window, so windows overlap real month partitions (the boundary case
        # the lock guards). Deterministic, so the two runs are comparable.
        class _DailyBarAdapter:
            def fetch_ohlcv(
                self,
                *,
                symbol: str,
                exchange: Exchange,
                interval: str,
                start: date,
                end: date,
            ) -> list[CandleBar]:
                del symbol, exchange, interval
                return list(_session_bars(start, end))

        instrument = _index(token=7)

        from brutex.orchestrator.universe_phases import RunParallel

        def _run(lake_root: Path, *, parallel: bool) -> None:
            sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
            runner: RunParallel
            if parallel:
                # A REAL pool gives genuine thread concurrency on the writes, so
                # two windows hit the shared month partition at once.
                from brutex.orchestrator.parallel import PoolRunParallel

                runner = PoolRunParallel(16)
            else:
                runner = _SimPool(max_workers=1, rate=_RATE, latency=0.5)
            ingest_spot(
                _DailyBarAdapter(),  # type: ignore[arg-type]
                sink,
                instrument=instrument,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 9, 1),  # ~3 windows, sharing month partitions
                run_parallel=runner,
            )

        seq_root = tmp_path / "seq"
        par_root = tmp_path / "par"
        _run(seq_root, parallel=False)
        _run(par_root, parallel=True)

        seq = _read_lake(seq_root)
        par = _read_lake(par_root)
        assert seq, "sequential run wrote at least one partition"
        # Same partition files + byte-identical, timestamp-sorted content: the
        # concurrent writes lost nothing and stored the same merged result.
        assert set(seq) == set(par)
        for rel, rows in seq.items():
            assert par[rel] == rows, f"partition {rel} differs between runs"


def _session_bars(start: date, end: date) -> Iterator[CandleBar]:
    """One in-session (09:35 IST = 04:05 UTC) bar per day in ``[start, end)``."""
    from datetime import UTC, datetime, timedelta

    cur = start
    while cur < end:
        yield CandleBar(
            timestamp=datetime(cur.year, cur.month, cur.day, 4, 5, tzinfo=UTC),
            open=Decimal("100.00"),
            high=Decimal("101.00"),
            low=Decimal("99.00"),
            close=Decimal("100.50"),
            volume=10,
            open_interest=None,
        )
        cur = cur + timedelta(days=1)


def _read_lake(root: Path) -> dict[str, list[tuple[object, ...]]]:
    """Read every partition under *root* into ``{relpath: sorted-rows}``."""
    import polars as pl

    out: dict[str, list[tuple[object, ...]]] = {}
    for path in sorted(root.rglob("*.parquet")):
        df = pl.read_parquet(path).sort("timestamp")
        out[str(path.relative_to(root))] = [tuple(r) for r in df.iter_rows()]
    return out


class TestWindowPullIdPropagation:
    """pull_id is re-bound inside each window worker so ingest.* events keep it."""

    def test_pull_id_propagates_into_window_workers(self) -> None:
        seen: list[str] = []
        rec_lock = threading.Lock()

        class _RecordingAdapter:
            def fetch_ohlcv(
                self,
                *,
                symbol: str,
                exchange: Exchange,
                interval: str,
                start: date,
                end: date,
            ) -> list[CandleBar]:
                del symbol, exchange, interval, start, end
                with rec_lock:
                    seen.append(current_pull_id())
                return []

        def _runner[ItemT, ResultT](
            fn: Callable[[ItemT], ResultT],
            items: Sequence[ItemT],
        ) -> list[ItemResult[ItemT, ResultT]]:
            # Run each window in a FRESH context (no inherited pull_id), so only
            # the worker's own re-bind can restore it — this proves the re-bind,
            # not contextvar inheritance (mirrors the outer-loop wiring test).
            import contextvars

            results: list[ItemResult[ItemT, ResultT]] = []
            for item in items:
                ctx = contextvars.Context()
                results.append(ctx.run(lambda it=item: _one(fn, it)))
            return results

        def _one[ItemT, ResultT](
            fn: Callable[[ItemT], ResultT], item: ItemT
        ) -> ItemResult[ItemT, ResultT]:
            try:
                value = fn(item)
            except Exception as exc:
                return ItemResult(item=item, ok=False, result=None, exc=exc)
            return ItemResult(item=item, ok=True, result=value, exc=None)

        with capture_logs() as logs, pull_id_scope("windowpull42"):
            ingest_spot(
                _RecordingAdapter(),  # type: ignore[arg-type]
                NullBarSink(),
                instrument=_index(),
                interval="1minute",
                start=_START,
                end=_END,
                run_parallel=_runner,
            )
        # Every window worker observed the bound id (re-bound inside its scope).
        assert len(seen) == _EXPECTED_WINDOWS
        assert all(pid == "windowpull42" for pid in seen)
        # And every ingest.window event captured carries it.
        windows = [e for e in logs if e["event"] == "ingest.window"]
        assert len(windows) == _EXPECTED_WINDOWS
        assert all(e["pull_id"] == "windowpull42" for e in windows)


class TestTerminalErrorPropagatesUnderFanout:
    """A terminal DhanError on a window surfaces on the caller thread (not swallowed)."""

    def test_terminal_error_reraised_from_worker(self) -> None:
        class _BadAdapter:
            def fetch_ohlcv(
                self,
                *,
                symbol: str,
                exchange: Exchange,
                interval: str,
                start: date,
                end: date,
            ) -> list[CandleBar]:
                del symbol, exchange, interval, start, end
                raise DhanNotFoundError("HTTP 404 unknown security")

        pool = _SimPool(max_workers=4, rate=_RATE, latency=0.5)
        # The captured terminal error is re-raised on the caller thread, so the
        # outer run_dhan_spot_ingest quarantine sees the same DhanError it did
        # when this spine ran sequentially.
        with pytest.raises(DhanNotFoundError):
            ingest_spot(
                _BadAdapter(),  # type: ignore[arg-type]
                NullBarSink(),
                instrument=_index(),
                interval="1minute",
                start=_START,
                end=_END,
                run_parallel=pool,
            )


class TestDefaultPoolSizing:
    """The default pool is sized from the pace hook's live rate (Rule #6)."""

    def test_default_pool_runs_when_run_parallel_omitted(self) -> None:
        # No run_parallel → the real bounded PoolRunParallel runs the windows on
        # threads; with a DhanRateController pace it sizes to the controller rate.
        controller = DhanRateController()
        adapter = _CountingAdapter()
        report = ingest_spot(
            adapter,  # type: ignore[arg-type]
            NullBarSink(),
            instrument=_index(),
            interval="1minute",
            start=_START,
            end=_END,
            pace=controller.pace,
        )
        assert report.windows == _EXPECTED_WINDOWS
        assert adapter.calls == _EXPECTED_WINDOWS  # every window fetched once

    def test_default_pool_sizes_from_plain_pace_and_none(self) -> None:
        from brutex.adapters.dhan.ingest import _pace_rps

        # A DhanRateController-bound pace exposes current_rps (→ that rate).
        controller = DhanRateController()
        assert _pace_rps(controller.pace) == pytest.approx(CEILING_RPS)
        # A plain callable with no controller __self__ → the 5.0 ceiling fallback.
        assert _pace_rps(lambda: None) == pytest.approx(5.0)
        # No pace at all → the 5.0 ceiling fallback.
        assert _pace_rps(None) == pytest.approx(5.0)

    def test_default_pool_with_no_pace_still_fans_out(self) -> None:
        # No pace + no run_parallel → default pool sized at the 5.0 fallback runs
        # the windows on real threads (every window fetched exactly once).
        adapter = _CountingAdapter()
        report = ingest_spot(
            adapter,  # type: ignore[arg-type]
            NullBarSink(),
            instrument=_index(),
            interval="1minute",
            start=_START,
            end=_END,
        )
        assert report.windows == _EXPECTED_WINDOWS
        assert adapter.calls == _EXPECTED_WINDOWS
