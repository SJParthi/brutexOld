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
from time import sleep as _time_sleep

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

#: In-run transient-window auto-retry budget (DEC-DHAN-WINDOW-RETRY-001). After the
#: first fan-out pass, windows that returned a RETRYABLE error (a transient network
#: drop / timeout / 5xx that outlived the client's in-call fast-retry) are re-run as
#: their own pass, up to this many extra passes. The operator in a flaky-network
#: region (seconds-long drops mid-pull) gets a self-heal WITHIN the run instead of
#: waiting for the next pull. Windows still failing after the budget stay UNMARKED
#: (deferred to the next pull — the no-skip guarantee: a window is recorded complete
#: ONLY on a clean fetch+write, never on retry exhaustion).
_DEFAULT_MAX_RETRY_PASSES: int = 2

#: Base backoff (seconds) before a retry pass — SECONDS-scale (vs the client's
#: 0.2 s in-call hygiene) so a multi-second network outage has time to recover
#: between passes. Pass *k* waits ``base × 2**k`` clamped to :data:`_MAX_RETRY_BACKOFF_S`.
_DEFAULT_RETRY_BACKOFF_S: float = 1.0

#: Hard ceiling (seconds) on a single retry-pass backoff (defends an operator-set
#: large pass budget from driving the backoff toward an unbounded sleep).
_MAX_RETRY_BACKOFF_S: float = 8.0


def _default_retry_backoff(pass_index: int) -> None:
    """Sleep the bounded exponential backoff before retry pass ``pass_index`` (0-based).

    Injected as the default ``retry_backoff`` hook so production waits a real,
    network-recovery-scale gap between passes while tests pass a no-op recorder
    (no real sleep) — the same inject-the-sleep pattern as
    :class:`~brutex.adapters.dhan.client.DhanClient`.
    """
    _time_sleep(min(_DEFAULT_RETRY_BACKOFF_S * (2.0**pass_index), _MAX_RETRY_BACKOFF_S))


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

    retried_windows: int = 0
    """Distinct windows re-attempted by the in-run auto-retry (DEC-DHAN-WINDOW-RETRY-001).

    A window that returned a retryable error on a pass and was re-run on a later
    pass is counted once here, whether or not the retry ultimately succeeded.
    Zero when no window failed transiently (or ``max_retry_passes=0``).
    """

    still_failed_windows: int = 0
    """Windows still failing transiently AFTER the retry budget was exhausted.

    These are deferred to the next pull (NEVER marked complete — the no-skip
    guarantee). Equals :attr:`transient_failures` (the same final-pass count,
    surfaced under an explicit name for the dashboard's "incomplete windows"
    view). Zero on a fully clean / fully recovered pull.
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
    #: The window's ``(start, end)`` — set ONLY on a transient outcome so the
    #: in-run auto-retry (DEC-DHAN-WINDOW-RETRY-001) knows which window to re-run.
    #: ``None`` on a clean / skipped outcome (nothing to retry).
    window: tuple[date, date] | None = None
    #: The retryable error's class name (e.g. ``"DhanTransportError"``) for the
    #: per-window WARNING + the deferred-window audit. ``None`` unless transient.
    error_type: str | None = None


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
    max_retry_passes: int = _DEFAULT_MAX_RETRY_PASSES,
    retry_backoff: Callable[[int], None] | None = None,
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
    max_retry_passes
        In-run auto-retry budget (DEC-DHAN-WINDOW-RETRY-001). After the first
        fan-out pass, windows that returned a RETRYABLE error are re-run as their
        own pass, up to this many EXTRA passes (default
        :data:`_DEFAULT_MAX_RETRY_PASSES`). ``0`` reproduces the old single-pass
        behaviour. A window still failing after the budget stays UNMARKED
        (deferred to the next pull — the no-skip guarantee).
    retry_backoff
        Injected ``(pass_index) -> None`` sleep hook called before each retry
        pass (``None`` → :func:`_default_retry_backoff`, a bounded exponential
        seconds-scale wait so a multi-second network drop recovers between
        passes). Tests pass a no-op recorder so no real sleep occurs.

    Returns
    -------
    IngestReport
        Window count, total bars appended, the (final-pass) transient-failure
        count, the count of windows skipped as already-complete, plus
        ``retried_windows`` (distinct windows re-attempted) and
        ``still_failed_windows`` (windows deferred to the next pull).

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

    backoff = retry_backoff if retry_backoff is not None else _default_retry_backoff

    def _drain_pass(
        pending: list[tuple[date, date]],
    ) -> tuple[int, int, int, int, list[tuple[date, date]]]:
        """Run ONE fan-out pass over *pending*; sum outcomes + collect failed windows.

        Drains the window fan-out on the caller thread. A captured terminal
        DhanError is re-raised here so the outer quarantine behaves exactly as it
        did with the sequential spine; any other captured exception (a real bug)
        is re-raised too — never swallowed. Returns
        ``(bars_appended, transient, skipped, conflicts, transient_windows)``.
        """
        b = t = s = c = 0
        failed: list[tuple[date, date]] = []
        for outcome in runner(_fetch_window, pending):
            if outcome.ok:
                result = outcome.result
                if result is None:  # pragma: no cover — ok=True always carries a result
                    continue
                b += result.bars_appended
                t += result.transient
                s += result.skipped
                c += result.conflicts
                if result.transient and result.window is not None:
                    failed.append(result.window)
                continue
            exc = outcome.exc
            if exc is None:  # pragma: no cover — a failed outcome always carries exc
                continue
            raise exc
        return b, t, s, c, failed

    # First pass over ALL windows, then bounded in-run auto-retry of ONLY the
    # windows that failed transiently (DEC-DHAN-WINDOW-RETRY-001) — so a multi-
    # second network drop self-heals WITHIN the run instead of waiting for the
    # next pull. Counts that accumulate across passes (bars/skips/conflicts) are
    # summed; ``transient`` always holds the LATEST pass's still-failing count, so
    # ``transient_failures == still_failed_windows`` = the windows deferred to the
    # next pull (NEVER marked complete — the no-skip guarantee). Worklists are
    # sorted before each pass so the report is deterministic regardless of
    # worker-completion order (Rule #4).
    bars_appended = skipped = conflicts = transient = 0
    ever_retried: set[tuple[date, date]] = set()
    pending = windows
    pass_idx = 0
    while True:
        pass_bars, transient, pass_skipped, pass_conflicts, failed = _drain_pass(pending)
        bars_appended += pass_bars
        skipped += pass_skipped
        conflicts += pass_conflicts
        failed_sorted = sorted(set(failed))
        if not failed_sorted or pass_idx >= max_retry_passes:
            break
        ever_retried.update(failed_sorted)
        pass_idx += 1
        _LOG.info(
            "ingest.retry_pass",
            pull_id=pull_id,
            retry_pass=pass_idx,
            windows=len(failed_sorted),
            **series_fields,
        )
        backoff(pass_idx - 1)
        pending = failed_sorted
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
        retried_windows=len(ever_retried),
        still_failed_windows=transient,
        **series_fields,
    )
    return IngestReport(
        windows=len(windows),
        bars_appended=bars_appended,
        transient_failures=transient,
        skipped_windows=skipped,
        conflicts=conflicts,
        retried_windows=len(ever_retried),
        still_failed_windows=transient,
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
            rate_limited = is_rate_limit(exc)
            if on_rate_limit is not None and rate_limited:
                on_rate_limit()
            # In-run auto-retry (DEC-DHAN-WINDOW-RETRY-001) targets a NETWORK drop
            # (transport / timeout / 5xx that outlived the client's in-call
            # fast-retry) — the operator's seconds-long connectivity blips — and
            # DELIBERATELY EXCLUDES a 429 (``window=None`` ⇒ not re-run in-run),
            # mirroring ``client._should_fast_retry``: the rate controller's
            # step-down (above) + the token bucket own 429 backoff, so retrying a
            # 429 here would fight the limiter. EITHER way the window is counted
            # transient and NEVER marked complete (no-skip) — a 429 window is
            # simply deferred to the next pull, by when the stepped-down rate lets
            # it through. The per-window WARNING surfaces the exact window + error
            # the bare ``transient_failures=N`` count hid.
            retry_window = None if rate_limited else window
            _LOG.warning(
                "ingest.window.transient",
                pull_id=pull_id,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
                error_type=type(exc).__name__,
                in_run_retry=not rate_limited,
                **series_fields,
            )
            return _WindowOutcome(transient=1, window=retry_window, error_type=type(exc).__name__)
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
    # Record the window as pulled so a re-pull skips it — BUT ONLY when the
    # vendor actually returned bars (DEC-LAKE-RECONCILE-001). A 0-bar response
    # (an empty 200 under a 429 storm, or a genuinely-empty range) is NOT marked
    # complete, so it is re-pulled next run instead of being silently stuck (the
    # NIFTY / ADANIENT / COALINDIA 1-day "no_data" bug). ``raw_bars`` (NOT
    # ``result.bars_appended``) is the signal: a re-pull whose bars are all
    # already in the lake dedups to ``bars_appended == 0`` via keep-first yet the
    # window IS complete, so it must stay marked.
    if coverage is not None and len(raw_bars) > 0:
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
