"""Dhan spot ingest — pull a symbol's OHLCV window-by-window into the lake.

The thin orchestration the cli/dashboard call to fill the lake for one spot
instrument: plan the fetch windows (1-minute → 90-day tiles via
:func:`~brutex.adapters.dhan.chunks.plan_spot_intraday_windows`; 1-day → one
call), fetch each via :meth:`~brutex.adapters.protocol.MarketAdapter.fetch_ohlcv`,
and append the parsed bars to a :class:`~brutex.data.sink.BarSink` partitioned by
``(exchange, segment, security_id, interval)``.

Pacing is injected (``pace`` — the orchestrator's adaptive-token-bucket
``acquire`` hook) so the **5 req/s / 100k-day** Dhan limit is honoured
without coupling the limiter in here (CLAUDE.md §11 COMMON). A **transient**
(retryable) error on a window is counted and skipped (the orchestrator
re-queues it); a **terminal** error propagates. Track-1 = spot only
(indices + INDIA VIX + stock spot); options ingest is the separate
rollingoption path.

Per-window parallel fan-out (DEC-DHAN-PARALLEL-WINDOWS-001). The WINDOWS of one
``(instrument, interval)`` series are mapped through an injected
:class:`~brutex.orchestrator.universe_phases.RunParallel` (production: a bounded
:class:`~brutex.orchestrator.parallel.PoolRunParallel`) so a SINGLE symbol's
windows are in flight at once and the pull **targets/saturates UP TO ~5 r/s**
when Dhan is healthy — instead of the old ~0.5 r/s sequential spine where each
90-day window waited for the previous round-trip (measured: SENSEX 2021→2026,
23 windows, ~43 s ≈ 0.5 r/s; the fan-out drives it toward ~10 s). This complements
DEC-DHAN-PARALLEL-INGEST-001 (which parallelised the OUTER ``(instrument,
interval)`` loop): a single-symbol pull (e.g. one index re-pull) now also fans
out, where before it ran one window at a time. The SHARED
:class:`~brutex.adapters.dhan.rate_control.DhanRateController` (threaded in via
``pace``) is the single governor that keeps the AGGREGATE issue rate ``≤ 5 r/s``
(Rule #6) across both fan-out levels.

**Honest envelope (Golden Rule #10):** this lets one symbol's windows fan out so
it saturates UP TO ~5 r/s — it is NOT a constant 5 r/s (Dhan's hard cap, 429
backoff step-downs, and per-call latency spikes all lower the achieved rate);
throughput here is network-bound, NOT O(1). The ``run_parallel`` seam is the
*mechanism* that lets the shared rate bucket — not a single-threaded window loop
— be the bottleneck. Concurrent windows of one series may target the SAME
``(year, month)`` Parquet partition (adjacent 90-day windows straddle a month
boundary), so the per-partition write serialisation in
:class:`~brutex.data.parquet_writer.ParquetWriter` (same decision) prevents a
lost-update; the merge-on-write unions by timestamp, so window completion order
never affects the stored bars (Rule #7). ``run_parallel`` is injectable so tests
drive a synchronous fake with zero threads (mirrors
:func:`~brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time

import structlog

from brutex.adapters.dhan.chunks import plan_spot_intraday_windows
from brutex.adapters.dhan.errors import DhanError, is_rate_limit, is_retryable
from brutex.adapters.protocol import MarketAdapter
from brutex.data.fetch_coverage import FetchCoverage, WindowKey
from brutex.data.session import filter_intraday_session, is_session_exempt
from brutex.data.sink import BarSink
from brutex.models import Instrument
from brutex.observability.correlation import current_pull_id, pull_id_scope
from brutex.orchestrator.parallel import PoolRunParallel, workers_to_saturate
from brutex.orchestrator.universe_phases import RunParallel

#: Structured logger — lights the spot ingest spine with per-window / parse /
#: lake-write / series events under the ``ingest.*`` namespace, all carrying the
#: pull's ``pull_id`` (DEC-DHAN-INGEST-LOGGING-001). Per-window detail is DEBUG so
#: a long pull never floods INFO; the per-series summary stays DEBUG too (the
#: per-instrument rollup at INFO lives one layer up in ``run_ingest``).
_LOG = structlog.get_logger("brutex.adapters.dhan.ingest")

#: Pacing hook invoked once before each window fetch (e.g. token-bucket acquire).
Pacer = Callable[[], None]

#: Canonical daily interval — the only interval not tiled/session-filtered.
_DAILY: str = "1day"

#: The data-source namespace stamped on every coverage :class:`WindowKey`.
_MARKET: str = "dhan"


@dataclass(frozen=True, slots=True)
class IngestReport:
    """Outcome of an :func:`ingest_spot` run."""

    windows: int
    bars_appended: int
    transient_failures: int
    skipped_windows: int = 0
    conflicts: int = 0
    """Vendor re-statements detected during this run (DEC-LAKE-CONFLICT-KEEPFIRST-001).

    A conflict is a timestamp already in the lake whose stored bar differed from
    the incoming bar.  The stored bar is kept (keep-first — never overwritten,
    Golden Rule #7); this count is the total number of such discrepancies across
    all windows in the series.  Zero on a clean pull or a coverage-skipped pull.
    """


@dataclass(frozen=True, slots=True)
class _WindowOutcome:
    """One window's contribution to the series totals (aggregated order-independently).

    Exactly one of the count fields is non-zero per window: a SKIPPED window
    (already in coverage) sets ``skipped=1``; a TRANSIENT failure sets
    ``transient=1``; a clean fetch sets ``bars_appended`` to the rows persisted
    (possibly 0 when the session filter drops every bar). ``conflicts`` tallies
    vendor re-statements logged but kept-first in this window.  The controller
    thread sums these across windows — addition is commutative, so
    worker-completion order never changes the :class:`IngestReport`
    (Rule #4 / Rule #7).
    """

    bars_appended: int = 0
    transient: int = 0
    skipped: int = 0
    conflicts: int = 0


def _windows(interval: str, start: date, end: date) -> list[tuple[date, date]]:
    """Tile ``[start, end)`` into fetch windows for *interval* (1m → 90d; 1d → one)."""
    if interval == _DAILY:
        return [(start, end)]
    planned = plan_spot_intraday_windows(
        datetime.combine(start, time()), datetime.combine(end, time())
    )
    return [(date.fromisoformat(w.start[:10]), date.fromisoformat(w.end[:10])) for w in planned]


def ingest_spot(
    adapter: MarketAdapter,
    sink: BarSink,
    *,
    instrument: Instrument,
    interval: str,
    start: date,
    end: date,
    pace: Pacer | None = None,
    coverage: FetchCoverage | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
    run_parallel: RunParallel | None = None,
) -> IngestReport:
    """Pull *instrument*'s spot OHLCV over ``[start, end)`` into *sink*.

    The series' fetch WINDOWS are mapped through *run_parallel* so a SINGLE
    symbol's windows are in flight at once and the pull targets/saturates UP TO
    ~5 r/s when Dhan is healthy (Little's law: ``in_flight ≈ rate × latency``),
    governed by the shared rate bucket the *pace* hook drives (the aggregate
    stays ``≤ 5 r/s``, Rule #6). Per-window outcomes are summed on the caller
    thread — addition is commutative, so worker-completion order never changes
    the report (Rule #4). **Honest envelope (Golden Rule #10):** this targets
    ~5 r/s — it cannot guarantee a constant 5 r/s (Dhan's cap + a 429 step-down
    + latency spikes lower the achieved rate); the throughput is network-bound,
    not O(1).

    Parameters
    ----------
    adapter, sink
        The Dhan adapter (data source) and the lake write sink. Both are shared
        across the window workers; concurrent windows of one series may write
        the SAME ``(year, month)`` partition (adjacent 90-day windows straddle a
        month), so the sink's :class:`~brutex.data.parquet_writer.ParquetWriter`
        serialises same-partition writes (DEC-DHAN-PARALLEL-WINDOWS-001) — the
        merge unions by timestamp, so the result is order-independent.
    instrument
        The spot instrument; ``symbol_name`` is the engine symbol, the raw
        Dhan ``SECURITY_ID`` lives in ``security_id``.
    interval
        Canonical interval — ``"1minute"`` (90-day tiles) or ``"1day"``.
    start, end
        Half-open date range.
    pace
        Optional per-window pacing hook (rate-limit acquire). Under the fan-out
        this is the SHARED rate bucket every window worker acquires from — the
        single governor that bounds the aggregate physical rate to ``≤ 5 r/s``
        (Rule #6; :class:`~brutex.adapters.dhan.rate_control.DhanRateController`
        is thread-safe).
    coverage
        Optional :class:`~brutex.data.fetch_coverage.FetchCoverage` ledger
        (DEC-FETCH-COVERAGE-001). When given, a window already marked complete
        is **skipped before the vendor call** (no wasted Dhan hit), and a window
        is marked complete after a successful fetch + append. ``None`` (default)
        keeps the always-fetch behaviour — the idempotent sink already makes a
        re-pull safe; coverage makes it quota-frugal too. Thread-safe under the
        window fan-out (its reads/writes are lock-guarded), so a concurrent
        re-pull skips already-covered windows and adds 0 net-new fetches.
    on_rate_limit, on_success
        Optional rate-controller feedback hooks (DEC-DHAN-ADAPTIVE-RPS-001).
        ``on_rate_limit`` fires on a **rate-limit** 429 (``is_rate_limit``) so
        the controller can step the rate down; ``on_success`` fires after each
        clean window so it can recover. Both default ``None`` (no feedback).
    run_parallel
        The concurrency primitive mapping the per-window fetcher over the
        series' windows (a :class:`~brutex.orchestrator.universe_phases.RunParallel`).
        ``None`` (the default) builds a bounded
        :class:`~brutex.orchestrator.parallel.PoolRunParallel` sized to saturate
        the rate budget (:func:`~brutex.orchestrator.parallel.workers_to_saturate`).
        Tests inject a synchronous fake to drive the fan-out with zero threads
        (mirrors :func:`~brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest`).

    Returns
    -------
    IngestReport
        Window count, total bars appended, transient-failure count, and the
        count of windows skipped as already-complete.

    Raises
    ------
    DhanError
        On a terminal (non-retryable) fetch error — re-raised on the caller
        thread after *run_parallel* captures it from the worker, so the outer
        :func:`~brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest` quarantine
        sees the same terminal error it did when this spine ran sequentially.
    """
    windows = _windows(interval, start, end)
    runner: RunParallel = (
        run_parallel
        if run_parallel is not None
        else PoolRunParallel(workers_to_saturate(_pace_rps(pace)))
    )
    # Read the pull's correlation id ONCE (the controller bound it via
    # ``pull_id_scope``); stamp it + the (symbol, interval) coordinate on every
    # ``ingest.*`` event so an operator can grep one pull end-to-end. Observability
    # only — no behaviour change (DEC-DHAN-INGEST-LOGGING-001).
    pull_id = current_pull_id()
    series_fields = {"symbol": instrument.symbol_name, "interval": interval}

    def _fetch_window(window: tuple[date, date]) -> _WindowOutcome:
        """Pull ONE window — runs inside a worker thread under the fan-out.

        Re-binds *pull_id* into THIS thread's structlog context-vars (a worker
        thread does NOT inherit the caller's contextvar, so without this the
        ``ingest.*`` events would lose their ``pull_id``, regressing
        DEC-DHAN-INGEST-LOGGING-001 — exactly the re-bind ``run_dhan_spot_ingest``
        does for the outer loop). A retryable error is counted into the outcome;
        a terminal :class:`DhanError` is raised, captured by *run_parallel*, and
        re-raised on the caller thread below.
        """
        with pull_id_scope(pull_id):
            return _ingest_one_window(
                adapter,
                sink,
                instrument=instrument,
                interval=interval,
                window=window,
                pace=pace,
                coverage=coverage,
                on_rate_limit=on_rate_limit,
                on_success=on_success,
                pull_id=pull_id,
                series_fields=series_fields,
            )

    bars_appended = 0
    transient = 0
    skipped = 0
    conflicts = 0
    # Drain the window fan-out on the caller thread, summing the per-window
    # outcomes. A captured terminal DhanError is re-raised here so the outer
    # quarantine behaves exactly as it did with the sequential spine; any other
    # captured exception (a real bug) is re-raised too — never swallowed.
    for outcome in runner(_fetch_window, windows):
        if outcome.ok:
            result = outcome.result
            if result is None:  # pragma: no cover — ok=True always carries a result
                continue
            bars_appended += result.bars_appended
            transient += result.transient
            skipped += result.skipped
            conflicts += result.conflicts
            continue
        exc = outcome.exc
        if exc is None:  # pragma: no cover — a failed outcome always carries exc
            continue
        raise exc
    # Per-series summary (one (symbol, interval) pair) — DEBUG; the per-instrument
    # rollup at INFO is emitted by ``run_dhan_spot_ingest`` one layer up.
    _LOG.debug(
        "ingest.series",
        pull_id=pull_id,
        windows=len(windows),
        bars_appended=bars_appended,
        transient_failures=transient,
        skipped_windows=skipped,
        conflicts=conflicts,
        **series_fields,
    )
    return IngestReport(
        windows=len(windows),
        bars_appended=bars_appended,
        transient_failures=transient,
        skipped_windows=skipped,
        conflicts=conflicts,
    )


def _pace_rps(pace: Pacer | None) -> float:
    """Best-effort current rate of the *pace* hook, for default pool sizing.

    The default :class:`~brutex.orchestrator.parallel.PoolRunParallel` widens to
    ``workers_to_saturate(rps)``. A :class:`~brutex.adapters.dhan.rate_control.DhanRateController`
    exposes ``current_rps``; the production wiring passes ``controller.pace`` as
    *pace*, so we read the controller's live rate off the bound method's
    ``__self__`` when present, else fall back to the Dhan ceiling (5.0). This is
    a pure sizing hint — the SHARED rate bucket (not the worker count) is the
    real ``≤ 5 r/s`` governor (Rule #6), so a wrong guess only over/under-fills
    idle latency, never breaches the cap.
    """
    controller = getattr(pace, "__self__", None)
    rps = getattr(controller, "current_rps", None)
    if isinstance(rps, (int, float)) and rps > 0.0:
        return float(rps)
    return 5.0


def _ingest_one_window(
    adapter: MarketAdapter,
    sink: BarSink,
    *,
    instrument: Instrument,
    interval: str,
    window: tuple[date, date],
    pace: Pacer | None,
    coverage: FetchCoverage | None,
    on_rate_limit: Callable[[], None] | None,
    on_success: Callable[[], None] | None,
    pull_id: str,
    series_fields: dict[str, str],
) -> _WindowOutcome:
    """Fetch + session-filter + sink-append ONE window; return its outcome.

    The body of the former sequential loop, extracted verbatim so one window's
    work runs identically whether the fan-out is the real thread pool or a
    synchronous test fake. All side effects it touches (the *pace* bucket,
    *coverage* ledger, the *on_rate_limit* / *on_success* controller hooks, and
    the sink) are thread-safe, so N windows of one series run it concurrently
    without a data race (DEC-DHAN-PARALLEL-WINDOWS-001).
    """
    window_start, window_end = window
    key = WindowKey(
        market=_MARKET,
        security_id=instrument.symbol_name,
        interval=interval,
        start=window_start,
        end=window_end,
    )
    # Already pulled? Skip the vendor call entirely — no wasted Dhan hit
    # (DEC-FETCH-COVERAGE-001). The idempotent sink would no-op the write
    # anyway, so skipping is a pure quota/latency win, never a data risk.
    if coverage is not None and coverage.is_complete(key):
        _LOG.debug(
            "ingest.window",
            pull_id=pull_id,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            action="skipped",
            **series_fields,
        )
        return _WindowOutcome(skipped=1)
    if pace is not None:
        pace()
    try:
        raw_bars = adapter.fetch_ohlcv(
            symbol=instrument.symbol_name,
            exchange=instrument.exch_id,
            interval=interval,
            start=window_start,
            end=window_end,
        )
    except DhanError as exc:
        if is_retryable(exc):
            # A rate-limit 429 feeds the controller so it steps the rate
            # down under sustained pressure (DEC-DHAN-ADAPTIVE-RPS-001).
            if on_rate_limit is not None and is_rate_limit(exc):
                on_rate_limit()
            return _WindowOutcome(transient=1)
        raise
    # Drop pre-open / post-close bars before they enter the lake — intraday
    # only (a daily bar has no intraday time). DEC-SESSION-FILTER-001.
    # GIFT Nifty trades ~21h on NSE IX, so it is EXEMPT — its full-day
    # series is kept verbatim (DEC-DHAN-GIFTNIFTY-SESSION-001).
    if interval != _DAILY and not is_session_exempt(instrument.symbol_name):
        bars = filter_intraday_session(raw_bars)
    else:
        bars = raw_bars
    # Parse nook: how many bars the vendor returned vs how many survived the
    # session gate (the silent drop an operator otherwise cannot see).
    _LOG.debug(
        "ingest.parse",
        pull_id=pull_id,
        raw_bars=len(raw_bars),
        session_bars=len(bars),
        **series_fields,
    )
    result = sink.append(
        exchange=instrument.exch_id.value,
        segment=instrument.segment.value,
        symbol=instrument.symbol_name,
        interval=interval,
        bars=bars,
    )
    _LOG.debug(
        "ingest.window",
        pull_id=pull_id,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        action="fetched",
        **series_fields,
    )
    # Lake-write nook: bars actually persisted (the idempotent sink may append
    # fewer than handed it on a re-write) — the row count that reached Parquet.
    _LOG.debug(
        "ingest.lake_write",
        pull_id=pull_id,
        bars_appended=result.bars_appended,
        **series_fields,
    )
    # Conflict nook: log each vendor re-statement at WARNING so the operator can
    # see EXACTLY what Dhan changed.  The stored bar is kept (keep-first —
    # never overwritten, DEC-LAKE-CONFLICT-KEEPFIRST-001 / Golden Rule #7).
    window_conflicts = 0
    for wr in result.write_results:
        for c in wr.conflicts:
            window_conflicts += 1
            _LOG.warning(
                "ingest.conflict",
                pull_id=pull_id,
                timestamp=c.timestamp.isoformat(),
                stored_close=str(c.stored.close),
                vendor_close=str(c.vendor.close),
                **series_fields,
            )
    # The window pulled cleanly — record it so a re-pull skips it.
    if coverage is not None:
        coverage.mark_complete(key)
    # Signal a clean window so the controller can recover the rate.
    if on_success is not None:
        on_success()
    return _WindowOutcome(bars_appended=result.bars_appended, conflicts=window_conflicts)


__all__ = [
    "IngestReport",
    "Pacer",
    "ingest_spot",
]
