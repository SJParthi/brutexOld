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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from time import sleep as _time_sleep
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import structlog

if TYPE_CHECKING:
    from brutex.data.parquet_writer import BarConflict

from brutex.adapters.dhan.auth_guard import AuthGuard
from brutex.adapters.dhan.chunks import plan_spot_intraday_windows
from brutex.adapters.dhan.errors import (
    DhanError,
    is_auth_failure,
    is_no_data_error,
    is_rate_limit,
    is_retryable,
)
from brutex.adapters.protocol import MarketAdapter
from brutex.data.fetch_coverage import WindowKey
from brutex.data.session import filter_intraday_session, is_session_exempt
from brutex.data.sink import BarSink
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_status import WindowStatusLog
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

#: IST clock pieces for the settled-data clamp (DEC-DAILY-CANDLE-CLAMP-001).
_IST_ZONE = ZoneInfo("Asia/Kolkata")
_MARKET_CLOSE_IST: time = time(15, 30)


def _now_ist() -> datetime:
    """The current IST wall-clock (the clamp's production default)."""
    return datetime.now(tz=_IST_ZONE)


def clamp_window_end(interval: str, end: date, *, now_ist: datetime) -> date:
    """Clamp an end-EXCLUSIVE pull bound so unsettled CURRENT-DAY data is never requested.

    Dhan reality (DEC-DAILY-CANDLE-CLAMP-001, operator-verified 2026-06-10 —
    the live window ledger showed a same-evening 1day request for "today"
    rejected with ``DhanBadRequestError``):

    * the CURRENT day's **1-day** candle is published only the NEXT day —
      requesting it same-day burns a quota hit and 400s, so the daily bound
      is clamped to ``today`` (end-exclusive ⇒ the last requested candle is
      yesterday's);
    * the CURRENT day's **1-minute** bars are partial until the 15:30 IST
      close — pulling a partial day would settle the window SUCCESS in the
      ledger and the skip rule would never re-fetch the rest of the day, so
      before close the intraday bound is clamped to ``today`` too; from
      15:30 IST the full session exists and ``today + 1 day`` is allowed.

    Pure + clock-injected — every branch unit-testable offline.
    """
    today = now_ist.date()
    if interval == _DAILY:
        return min(end, today)
    if now_ist.time() >= _MARKET_CLOSE_IST:
        return min(end, today + timedelta(days=1))
    return min(end, today)


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
    status_log: WindowStatusLog | None = None,
    ledger: WindowLedger | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_timeout: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
    run_parallel: RunParallel | None = None,
    max_retry_passes: int = _DEFAULT_MAX_RETRY_PASSES,
    retry_backoff: Callable[[int], None] | None = None,
    auth_guard: AuthGuard | None = None,
    now_ist: Callable[[], datetime] | None = None,
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
    auth_guard
        Optional shared "token is dead" latch (DEC-DHAN-AUTH-FAILFAST-RENEW-001).
        When given, a window worker checks it BEFORE the vendor call and
        short-circuits (returns a no-op skipped outcome, ZERO doomed calls) once
        the token is known dead, and TRIPS it on the first auth failure
        (:func:`~brutex.adapters.dhan.errors.is_auth_failure` — a 401/403 OR a
        ``DH-906`` "Invalid Token" 400). The auth error is then re-raised (auth is
        non-retryable) so the outer
        :func:`~brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest` quarantine
        sees it. ``None`` (default) keeps the prior per-symbol terminal behaviour.

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
    clamped_end = clamp_window_end(interval, end, now_ist=(now_ist or _now_ist)())
    if clamped_end != end:
        _LOG.info(
            "ingest.window.clamped",
            symbol=instrument.symbol_name,
            interval=interval,
            requested_end=end.isoformat(),
            clamped_end=clamped_end.isoformat(),
            reason="unsettled current-day data (DEC-DAILY-CANDLE-CLAMP-001)",
        )
    windows = _windows(interval, start, clamped_end) if start < clamped_end else []
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
                status_log=status_log,
                ledger=ledger,
                on_rate_limit=on_rate_limit,
                on_timeout=on_timeout,
                on_success=on_success,
                pull_id=pull_id,
                series_fields=series_fields,
                auth_guard=auth_guard,
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


#: OHLCV+OI fields compared bar-for-bar to detect a keep-first conflict — the
#: SAME tuple :meth:`ParquetWriter._bar_values_equal` compares, kept here so the
#: summary reports WHICH field a vendor re-statement touched (CLAUDE.md §11 COMMON).
_CONFLICT_FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "open_interest")


def summarize_conflicts(conflicts: Sequence[BarConflict]) -> dict[str, object]:
    """Aggregate a window's keep-first conflicts into ONE log payload.

    DEC-LAKE-CONFLICT-LOG-AGG-001. The prior per-bar ``ingest.conflict`` WARNING
    flooded the log (hundreds of lines on a re-pull) AND printed only ``close`` —
    so a bar whose ``open``/``high``/``low`` was re-stated by the vendor looked
    like a *false* conflict (equal closes). This collapses a window's conflicts to
    one summary: the count, the sorted set of OHLCV+OI fields that ACTUALLY differ,
    the max absolute close delta, and the first/last conflicting timestamps.

    Parameters
    ----------
    conflicts
        The window's :class:`~brutex.data.parquet_writer.BarConflict` records
        (stored kept, vendor discarded — keep-first, Golden Rule #7).

    Returns
    -------
    dict
        ``conflicts`` (count), ``differing_fields`` (comma-joined, sorted),
        ``max_close_delta`` (str Decimal), ``first_ts`` / ``last_ts`` (ISO or None).
    """
    differing: set[str] = set()
    max_close_delta = Decimal(0)
    for c in conflicts:
        for field_name in _CONFLICT_FIELDS:
            if getattr(c.stored, field_name) != getattr(c.vendor, field_name):
                differing.add(field_name)
        max_close_delta = max(max_close_delta, abs(c.vendor.close - c.stored.close))
    timestamps = [c.timestamp for c in conflicts]
    return {
        "conflicts": len(conflicts),
        "differing_fields": ",".join(sorted(differing)),
        "max_close_delta": str(max_close_delta),
        "first_ts": min(timestamps).isoformat() if timestamps else None,
        "last_ts": max(timestamps).isoformat() if timestamps else None,
    }


def _ingest_one_window(
    adapter: MarketAdapter,
    sink: BarSink,
    *,
    instrument: Instrument,
    interval: str,
    window: tuple[date, date],
    pace: Pacer | None,
    status_log: WindowStatusLog | None,
    ledger: WindowLedger | None,
    on_rate_limit: Callable[[], None] | None,
    on_timeout: Callable[[], None] | None,
    on_success: Callable[[], None] | None,
    pull_id: str,
    series_fields: dict[str, str],
    auth_guard: AuthGuard | None = None,
) -> _WindowOutcome:
    """Fetch + session-filter + sink-append ONE window; return its outcome.

    The body of the former sequential loop, extracted verbatim so one window's
    work runs identically whether the fan-out is the real thread pool or a
    synchronous test fake. All side effects it touches (the *pace* bucket,
    the *on_rate_limit* / *on_success* controller hooks, and
    the sink) are thread-safe, so N windows of one series run it concurrently
    without a data race (DEC-DHAN-PARALLEL-WINDOWS-001).

    When *auth_guard* is supplied (DEC-DHAN-AUTH-FAILFAST-RENEW-001) and is
    already tripped, the window short-circuits to a no-op ``skipped`` outcome
    BEFORE pacing or any vendor call — so once the token is known dead the rest
    of the pull drains instantly with ZERO doomed 401 calls. On the FIRST auth
    failure (:func:`~brutex.adapters.dhan.errors.is_auth_failure` — a 401/403 OR
    a ``DH-906`` "Invalid Token" 400) it trips the guard, then re-raises (auth is
    non-retryable, so it must surface to the outer quarantine).
    """
    window_start, window_end = window
    key = WindowKey(
        market=_MARKET,
        security_id=instrument.symbol_name,
        interval=interval,
        start=window_start,
        end=window_end,
    )
    # Already settled? Skip the vendor call entirely — no wasted Dhan hit. The
    # unified WindowLedger is now the SOLE skip authority (DEC-WINDOW-LEDGER-001,
    # Stage b3): a window is skipped ONLY when its ledger row is settled
    # (SUCCESS / SKIPPED) — the precise per-window record, NOT the coarse
    # span-enclosure FetchCoverage set that masked internal-gap FAILED windows
    # (#598). A FAILED / PLANNED / EMPTY-below-threshold / MISSING window is
    # fetched. The idempotent sink would no-op a redundant write anyway, so
    # skipping is a pure quota/latency win, never a data risk.
    if ledger is not None and ledger.is_skippable(key):
        _LOG.debug(
            "ingest.window",
            pull_id=pull_id,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            action="skipped",
            **series_fields,
        )
        return _WindowOutcome(skipped=1)
    # Token already known dead (a prior window tripped the guard)? Drain this
    # window WITHOUT a vendor call (DEC-DHAN-AUTH-FAILFAST-RENEW-001) — once the
    # 24h token expires mid-pull EVERY remaining window would otherwise fire its
    # own doomed 401, so the guard short-circuits new windows to a no-op skip
    # the instant the token is known dead. (In-flight threads finish their
    # current call; the check here drains the rest fast — it does NOT hard-cancel
    # the pool, the honest envelope per Golden Rule #10.)
    if auth_guard is not None and auth_guard.tripped():
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
            # Feed the swallowed transient to the controller so it adapts: a
            # rate-limit 429 steps the rate down under sustained pressure
            # (DEC-DHAN-ADAPTIVE-RPS-001), and a timeout / transport / 5xx — a
            # slow, struggling Dhan, not a quota breach — drives the GENTLE
            # timeout ladder (DEC-DHAN-SPOT-TIMEOUT-BACKOFF-001, the spot-spine
            # parallel of the options fix DEC-DHAN-TIMEOUT-BACKOFF-001 #494). A
            # one-off blip is ignored by the controller's threshold, so steady
            # throughput is unchanged.
            rate_limited = is_rate_limit(exc)
            if rate_limited:
                if on_rate_limit is not None:
                    on_rate_limit()
            elif on_timeout is not None:
                on_timeout()
            # In-run auto-retry (DEC-DHAN-WINDOW-RETRY-001, AMENDED by
            # DEC-DHAN-429-RETRY-001): re-run the window WITHIN the pull — a NETWORK
            # drop (transport / timeout / 5xx that outlived the client's in-call
            # fast-retry) AND a 429 alike. The 429 is now retried because DEFERRING
            # it to the next pull does NOT clear it: every pull re-opens at the full
            # r/s and re-bursts, so a window caught in the opening storm — notably
            # the single cheap 1-day call — 429s again and again, stuck at no_data
            # FOREVER (the operator's recurring 1-day failure). The retry runs AFTER
            # the seconds-scale backoff (``_default_retry_backoff``: 1 s → 2 s), by
            # when the opening burst is over and the controller has STEPPED DOWN
            # (DEC-DHAN-ADAPTIVE-RPS-001, fed by the ``on_rate_limit`` above), and is
            # paced by the token bucket — so the retry WAITS the limiter out instead
            # of fighting it. A window still failing after the retry budget stays
            # transient + NEVER marked complete (the no-skip guarantee — deferred to
            # the next pull). The per-window WARNING surfaces the exact window + error
            # the bare ``transient_failures=N`` count hid.
            retry_window = window
            _LOG.warning(
                "ingest.window.transient",
                pull_id=pull_id,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
                error_type=type(exc).__name__,
                in_run_retry=True,
                **series_fields,
            )
            # Record the FAILED outcome in the browsable status table (DEC-WINDOW-
            # STATUS-001) + the unified ledger (DEC-WINDOW-LEDGER-001) so the
            # operator SEES which windows errored + how often. Both share the
            # identical record() signature, so they never drift.
            _failed_at = datetime.now(UTC)
            if status_log is not None:
                status_log.record(
                    key,
                    raw_bars=0,
                    error_type=type(exc).__name__,
                    bars_appended=0,
                    now=_failed_at,
                )
            if ledger is not None:
                ledger.record(
                    key,
                    raw_bars=0,
                    error_type=type(exc).__name__,
                    bars_appended=0,
                    now=_failed_at,
                )
            return _WindowOutcome(transient=1, window=retry_window, error_type=type(exc).__name__)
        # A Dhan DH-905 "no data present" is the vendor's DEFINITIVE answer that
        # the range has no bars (a genuine gap, e.g. NIFTY NEXT 50 daily) — a
        # TERMINAL non-retryable error, but NOT a failure. Settle it as SKIPPED in
        # the unified ledger (DEC-WINDOW-LEDGER-001) so the ledger reflects the true
        # settled state (and, once the ledger drives skip, it is not re-fetched
        # forever). It is STILL re-raised below to the run_dhan_spot_ingest
        # known-gap quarantine (the verdict counts it as no_data) — behaviour
        # unchanged; this only enriches the ledger record.
        if ledger is not None and is_no_data_error(exc):
            ledger.settle_known_gap(key, now=datetime.now(UTC), reason=type(exc).__name__)
        # A TERMINAL (non-retryable) error. If it is the token dying — a 401/403
        # OR a ``DH-906`` "Invalid Token" 400 (is_auth_failure) — trip the shared
        # latch so every NOT-YET-DISPATCHED window short-circuits above instead of
        # firing its own doomed 401 (DEC-DHAN-AUTH-FAILFAST-RENEW-001). Auth is
        # non-retryable, so we still RE-RAISE: the error must surface to the outer
        # run_dhan_spot_ingest quarantine (which records the pair + emits the ONE
        # loud auth-aborted alert), never swallowed.
        if auth_guard is not None and is_auth_failure(exc):
            auth_guard.trip(str(exc))
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
    # Conflict nook (DEC-LAKE-CONFLICT-LOG-AGG-001): the stored bar is kept
    # (keep-first — never overwritten, DEC-LAKE-CONFLICT-KEEPFIRST-001 / Golden
    # Rule #7). Emit ONE aggregated WARNING per window (count + WHICH fields the
    # vendor re-stated + max close delta) instead of one WARNING per bar — a
    # vendor re-statement of a whole window used to flood the log with hundreds
    # of lines that showed only ``close`` (so an open/high/low revision looked
    # like a false equal-close conflict). Per-bar detail stays available at DEBUG.
    all_conflicts = [c for wr in result.write_results for c in wr.conflicts]
    window_conflicts = len(all_conflicts)
    if all_conflicts:
        for c in all_conflicts:
            _LOG.debug(
                "ingest.conflict",
                pull_id=pull_id,
                timestamp=c.timestamp.isoformat(),
                stored_close=str(c.stored.close),
                vendor_close=str(c.vendor.close),
                **series_fields,
            )
        _LOG.warning(
            "ingest.conflict.summary",
            pull_id=pull_id,
            **summarize_conflicts(all_conflicts),
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
    # Record the window's outcome in the browsable status table (DEC-WINDOW-
    # STATUS-001) + the unified ledger (DEC-WINDOW-LEDGER-001): SUCCESS when the
    # vendor returned bars, EMPTY on a clean 0-bar response. The mark below
    # (skip-ledger) and these records share the same ``raw_bars > 0`` SUCCESS
    # condition, so they never disagree.
    _done_at = datetime.now(UTC)
    if status_log is not None:
        status_log.record(
            key,
            raw_bars=len(raw_bars),
            error_type=None,
            bars_appended=result.bars_appended,
            now=_done_at,
        )
    if ledger is not None:
        ledger.record(
            key,
            raw_bars=len(raw_bars),
            error_type=None,
            bars_appended=result.bars_appended,
            now=_done_at,
        )
    # Signal a clean window so the controller can recover the rate.
    if on_success is not None:
        on_success()
    return _WindowOutcome(bars_appended=result.bars_appended, conflicts=window_conflicts)


__all__ = [
    "IngestReport",
    "Pacer",
    "ingest_spot",
]
