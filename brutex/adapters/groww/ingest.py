"""Groww spot ingest — pull a symbol's OHLCV window-by-window into the lake.

The Groww vendor ingest spine (PR-H). It is the thin orchestration the cutover
(PR-I) wires the cli/dashboard to: plan the fetch windows for a
``(instrument, interval)`` series, fetch each via
:meth:`~brutex.adapters.protocol.MarketAdapter.fetch_ohlcv`, and append the
parsed bars to a :class:`~brutex.data.sink.BarSink`.

Structure follows the canonical ingest spine (CLAUDE.md §11 COMMON — same shape, no
re-architecting): per-window parallel fan-out via an injected
:class:`~brutex.orchestrator.universe_phases.RunParallel` (production: a bounded
:class:`~brutex.orchestrator.parallel.PoolRunParallel`), in-run bounded
auto-retry of transient windows, the unified
:class:`~brutex.data.window_ledger.WindowLedger` as the SOLE per-window skip +
record authority, the intraday session filter, aggregated keep-first conflict
logging, deterministic order-independent aggregation, and the ``pull_id``
correlation re-bind inside each worker thread.

**Divergences from a flat single-tier spine (deliberate, per Golden Rule #1):**

* **Window tiling.** Groww's per-call date cap is interval-tiered (30/90/180
  days — :func:`~brutex.adapters.groww.chunks.window_days_for_interval`), richer
  than a flat split. We tile HERE via
  :func:`~brutex.adapters.groww.chunks.plan_candle_windows` at the SAME
  granularity the ledger records its windows at, then call ``fetch_ohlcv`` once
  per tile with that tile's ``[start, end)``. The
  :class:`~brutex.adapters.groww.adapter.GrowwMarketAdapter` re-tiles internally,
  but because each tile we hand it is already ≤ the interval's tier width, that
  inner tiling yields exactly one sub-window — so the ledger granularity (one row
  per tile) is preserved and there is no double-tiling that splits a recorded
  window. This keeps the skip authority precise (DEC-WINDOW-LEDGER-001 carries
  over verbatim).
* **Market namespace.** Every coverage :class:`~brutex.data.fetch_coverage.WindowKey`
  is stamped ``market="groww"``, so windows from different vendors
  for the same symbol never collide on the shared lake ledger.
* **Auth.** Some vendors carry a mid-pull ``AuthGuard`` token-death latch. The
  Groww adapter package provides no such latch, so we DO NOT invent one: a Groww
  auth failure (:class:`~brutex.adapters.groww.errors.GrowwAuthError`) is a
  TERMINAL non-retryable error that propagates to the outer
  :func:`~brutex.adapters.groww.run_ingest.run_groww_spot_ingest` quarantine —
  the same as any other terminal Groww error. (An honest, simpler envelope; a
  shared latch can be added if/when the Groww adapter grows one.)
* **status_log.** OMITTED. A legacy spine still carried a write-retired
  ``status_log`` branch (DEC-STATUS-SIDECAR-WRITE-RETIRE-001); the Groww spine
  drops it entirely — the unified ledger is the sole record surface.
* **Conflict summary.** :func:`~brutex.adapters.ingest_common.summarize_conflicts`
  is vendor-neutral; it is IMPORTED from the shared
  :mod:`brutex.adapters.ingest_common` home rather than copied (CLAUDE.md §11
  COMMON — no duplication), so the Groww package depends on no other adapter.

**Honest envelope (Golden Rule #10):** the fan-out lets one symbol's windows be
in flight at once so the pull saturates UP TO the rate ceiling when Groww is
healthy — it is NOT a constant rate (per-call latency + back-off step-downs lower
the achieved rate); throughput here is network-bound, NOT O(1). Groww's
backtesting rate-limit is UNVERIFIED
(:data:`~brutex.adapters.groww.constants.GROWW_RATE_LIMIT_RPS_UNVERIFIED`), so no
rate is hardcoded here — ``pace`` is injected and is the single governor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from time import sleep as _time_sleep

import structlog

from brutex.adapters.groww.chunks import window_days_for_interval
from brutex.adapters.groww.errors import GrowwError, is_rate_limit, is_retryable
from brutex.adapters.ingest_common import (
    GROWW_COVERAGE_MARKET,
    clamp_window_end,
    summarize_conflicts,
    tile_windows,
)
from brutex.adapters.ingest_common import (
    now_ist as _now_ist,
)
from brutex.adapters.protocol import MarketAdapter
from brutex.data.fetch_coverage import WindowKey
from brutex.data.session import filter_intraday_session, is_session_exempt
from brutex.data.sink import BarSink
from brutex.data.window_ledger import WindowLedger
from brutex.models import Instrument
from brutex.observability.correlation import current_pull_id, pull_id_scope
from brutex.orchestrator.parallel import PoolRunParallel, workers_to_saturate
from brutex.orchestrator.universe_phases import RunParallel

#: Structured logger — uses the canonical ``ingest.*`` namespace,
#: every event carrying the pull's ``pull_id``. Per-window detail is DEBUG so a
#: long pull never floods INFO; the per-series summary stays DEBUG too (the
#: per-instrument rollup at INFO lives one layer up in ``run_ingest``).
_LOG = structlog.get_logger("brutex.adapters.groww.ingest")

#: Pacing hook invoked once before each window fetch (e.g. token-bucket acquire).
Pacer = Callable[[], None]

#: Canonical daily interval — the only interval not session-filtered.
_DAILY: str = "1day"

#: The data-source namespace stamped on every coverage :class:`WindowKey`. ``"groww"``
#: keeps Groww windows from colliding with other vendors' on the shared lake ledger. Sourced
#: from the shared :data:`~brutex.adapters.ingest_common.GROWW_COVERAGE_MARKET` so the
#: spine AND the ledger-seeding helpers carry ONE literal (CLAUDE.md §11 COMMON).
_MARKET: str = GROWW_COVERAGE_MARKET

#: In-run transient-window auto-retry budget. After the
#: first fan-out pass, windows that returned a RETRYABLE Groww error (rate-limit /
#: timeout) are re-run as their own pass, up to this many extra passes. Windows
#: still failing after the budget stay UNMARKED (deferred to the next pull — the
#: no-skip guarantee: a window is recorded complete ONLY on a clean fetch+write).
_DEFAULT_MAX_RETRY_PASSES: int = 2

#: Base backoff (seconds) before a retry pass. Pass *k* waits ``base × 2**k``
#: clamped to :data:`_MAX_RETRY_BACKOFF_S`.
_DEFAULT_RETRY_BACKOFF_S: float = 1.0

#: Hard ceiling (seconds) on a single retry-pass backoff.
_MAX_RETRY_BACKOFF_S: float = 8.0


def _default_retry_backoff(pass_index: int) -> None:
    """Sleep the bounded exponential backoff before retry pass ``pass_index`` (0-based).

    Injected as the default ``retry_backoff`` hook so production waits a real,
    network-recovery-scale gap between passes while tests pass a no-op recorder.
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
    """Vendor re-statements detected during this run (keep-first, Golden Rule #7).

    A conflict is a timestamp already in the lake whose stored bar differed from
    the incoming bar. The stored bar is kept (never overwritten); this count is
    the total such discrepancies across all windows. Zero on a clean / skipped pull.
    """

    retried_windows: int = 0
    """Distinct windows re-attempted by the in-run auto-retry.

    A window that returned a retryable error on a pass and was re-run on a later
    pass is counted once, whether or not the retry ultimately succeeded.
    """

    still_failed_windows: int = 0
    """Windows still failing transiently AFTER the retry budget was exhausted.

    Deferred to the next pull (NEVER marked complete). Equals
    :attr:`transient_failures` (the same final-pass count, under an explicit name
    for the dashboard's "incomplete windows" view).
    """


@dataclass(frozen=True, slots=True)
class _WindowOutcome:
    """One window's contribution to the series totals (aggregated order-independently).

    Exactly one of the count fields is non-zero per window: a SKIPPED window sets
    ``skipped=1``; a TRANSIENT failure sets ``transient=1``; a clean fetch sets
    ``bars_appended`` to the rows persisted (possibly 0 when the session filter
    drops every bar). ``conflicts`` tallies vendor re-statements kept-first.
    Addition is commutative, so worker-completion order never changes the report.
    """

    bars_appended: int = 0
    transient: int = 0
    skipped: int = 0
    conflicts: int = 0
    #: The window's ``(start, end)`` — set ONLY on a transient outcome so the
    #: in-run auto-retry knows which window to re-run. ``None`` otherwise.
    window: tuple[date, date] | None = None
    #: The retryable error's class name for the per-window WARNING. ``None`` unless transient.
    error_type: str | None = None


def _windows(interval: str, start: date, end: date) -> list[tuple[date, date]]:
    """Tile ``[start, end)`` into fetch windows for *interval*'s Groww tier.

    Daily tiles at 180 days, 1-5m at 30, 10/15/30m at 90 — the interval→width
    policy is :func:`~brutex.adapters.groww.chunks.window_days_for_interval`,
    injected into the shared
    :func:`~brutex.adapters.ingest_common.tile_windows` so the tiling MECHANISM is
    vendor-neutral and there is no duplicated window-list builder (CLAUDE.md §11
    COMMON). Groww has no un-tiled daily case (every tier has a finite width), so
    *window_days_for* never returns ``None`` here.
    """
    return tile_windows(interval, start, end, window_days_for=window_days_for_interval)


def ingest_spot(
    adapter: MarketAdapter,
    sink: BarSink,
    *,
    instrument: Instrument,
    interval: str,
    start: date,
    end: date,
    pace: Pacer | None = None,
    ledger: WindowLedger | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_timeout: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
    run_parallel: RunParallel | None = None,
    max_retry_passes: int = _DEFAULT_MAX_RETRY_PASSES,
    retry_backoff: Callable[[int], None] | None = None,
    now_ist: Callable[[], datetime] | None = None,
) -> IngestReport:
    """Pull *instrument*'s spot OHLCV over ``[start, end)`` into *sink* (Groww).

    The series' fetch WINDOWS are mapped through *run_parallel* so a SINGLE
    symbol's windows are in flight at once and the pull saturates UP TO the rate
    ceiling when Groww is healthy, governed by the shared rate bucket the *pace*
    hook drives. Per-window outcomes are summed on the caller thread — addition is
    commutative, so worker-completion order never changes the report (Rule #4).

    Parameters
    ----------
    adapter, sink
        The Groww adapter (data source) and the lake write sink, shared across
        window workers.
    instrument
        The spot instrument; ``symbol_name`` is the engine symbol.
    interval
        Canonical interval (``"1minute"`` / ``"1day"`` / any tiered Groww interval).
    start, end
        Half-open date range.
    pace
        Optional per-window pacing hook (rate-limit acquire) — the SHARED rate
        bucket every worker acquires from. Groww's backtesting r/s is UNVERIFIED,
        so this is the only rate governor; nothing is hardcoded.
    on_rate_limit, on_timeout, on_success
        Optional rate-controller feedback hooks. ``on_rate_limit`` fires on a
        Groww rate-limit error, ``on_timeout`` on a non-rate-limit retryable
        (timeout), ``on_success`` after each clean window. All default ``None``.
    run_parallel
        The concurrency primitive mapping the per-window fetcher over the windows.
        ``None`` builds a bounded
        :class:`~brutex.orchestrator.parallel.PoolRunParallel`. Tests inject a
        synchronous fake.
    max_retry_passes
        In-run auto-retry budget. ``0`` reproduces the single-pass behaviour.
    retry_backoff
        Injected ``(pass_index) -> None`` sleep hook (``None`` →
        :func:`_default_retry_backoff`). Tests pass a no-op recorder.
    now_ist
        Injected IST clock for the current-day clamp (``None`` → :func:`_now_ist`).

    Returns
    -------
    IngestReport
        Window count, total bars appended, the (final-pass) transient-failure
        count, the skipped-window count, plus ``retried_windows`` and
        ``still_failed_windows``.

    Raises
    ------
    GrowwError
        On a terminal (non-retryable) fetch error — re-raised on the caller thread
        so the outer :func:`~brutex.adapters.groww.run_ingest.run_groww_spot_ingest`
        quarantine sees it (auth failures included — Groww has no fail-fast latch).
    """
    clamped_end = clamp_window_end(interval, end, now_ist=(now_ist or _now_ist)())
    if clamped_end != end:
        _LOG.info(
            "ingest.window.clamped",
            symbol=instrument.symbol_name,
            interval=interval,
            requested_end=end.isoformat(),
            clamped_end=clamped_end.isoformat(),
            reason="unsettled current-day data",
        )
    windows = _windows(interval, start, clamped_end) if start < clamped_end else []
    runner: RunParallel = (
        run_parallel
        if run_parallel is not None
        else PoolRunParallel(workers_to_saturate(_pace_rps(pace)))
    )
    pull_id = current_pull_id()
    series_fields = {"symbol": instrument.symbol_name, "interval": interval}

    def _fetch_window(window: tuple[date, date]) -> _WindowOutcome:
        """Pull ONE window — runs inside a worker thread under the fan-out.

        Re-binds *pull_id* into THIS thread's structlog context-vars (a worker
        thread does NOT inherit the caller's contextvar). A retryable error is
        counted into the outcome; a terminal :class:`GrowwError` is raised,
        captured by *run_parallel*, and re-raised on the caller thread below.
        """
        with pull_id_scope(pull_id):
            return _ingest_one_window(
                adapter,
                sink,
                instrument=instrument,
                interval=interval,
                window=window,
                pace=pace,
                ledger=ledger,
                on_rate_limit=on_rate_limit,
                on_timeout=on_timeout,
                on_success=on_success,
                pull_id=pull_id,
                series_fields=series_fields,
            )

    backoff = retry_backoff if retry_backoff is not None else _default_retry_backoff

    def _drain_pass(
        pending: list[tuple[date, date]],
    ) -> tuple[int, int, int, int, list[tuple[date, date]]]:
        """Run ONE fan-out pass over *pending*; sum outcomes + collect failed windows.

        A captured terminal GrowwError is re-raised here so the outer quarantine
        behaves exactly as it would with a sequential spine; any other captured
        exception (a real bug) is re-raised too — never swallowed. Returns
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
    # transiently-failed windows. Counts that accumulate across passes
    # (bars/skips/conflicts) are summed; ``transient`` always holds the LATEST
    # pass's still-failing count, so ``transient_failures == still_failed_windows``.
    # Worklists are sorted before each pass so the report is deterministic
    # regardless of worker-completion order (Rule #4).
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

    A rate controller exposes ``current_rps``; the production wiring passes
    ``controller.pace`` as *pace*, so we read the controller's live rate off the
    bound method's ``__self__`` when present, else fall back to the Groww
    UNVERIFIED ceiling. A pure sizing hint — the SHARED rate bucket (not the
    worker count) is the real governor, so a wrong guess only over/under-fills
    idle latency, never breaches the cap.
    """
    from brutex.adapters.groww.constants import GROWW_RATE_LIMIT_RPS_UNVERIFIED

    controller = getattr(pace, "__self__", None)
    rps = getattr(controller, "current_rps", None)
    if isinstance(rps, (int, float)) and rps > 0.0:
        return float(rps)
    return GROWW_RATE_LIMIT_RPS_UNVERIFIED


def _ingest_one_window(
    adapter: MarketAdapter,
    sink: BarSink,
    *,
    instrument: Instrument,
    interval: str,
    window: tuple[date, date],
    pace: Pacer | None,
    ledger: WindowLedger | None,
    on_rate_limit: Callable[[], None] | None,
    on_timeout: Callable[[], None] | None,
    on_success: Callable[[], None] | None,
    pull_id: str,
    series_fields: dict[str, str],
) -> _WindowOutcome:
    """Fetch + session-filter + sink-append ONE window; return its outcome.

    All side effects it touches (the *pace* bucket, the controller hooks, the
    sink) are thread-safe, so N windows of one series run it concurrently without
    a data race. A retryable Groww error is counted; a terminal :class:`GrowwError`
    (auth included) propagates to the outer quarantine.
    """
    window_start, window_end = window
    key = WindowKey(
        market=_MARKET,
        security_id=instrument.symbol_name,
        interval=interval,
        start=window_start,
        end=window_end,
    )
    # Already settled? Skip the vendor call entirely — the unified WindowLedger is
    # the SOLE skip authority (a window is skipped ONLY when its ledger row is
    # settled SUCCESS / SKIPPED). The idempotent sink would no-op a redundant
    # write anyway, so skipping is a pure quota/latency win, never a data risk.
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
    except GrowwError as exc:
        if is_retryable(exc):
            # Feed the swallowed transient to the controller so it adapts: a
            # rate-limit steps the rate down, a timeout drives the gentle timeout
            # ladder. A one-off blip is ignored by the controller's threshold.
            rate_limited = is_rate_limit(exc)
            if rate_limited:
                if on_rate_limit is not None:
                    on_rate_limit()
            elif on_timeout is not None:
                on_timeout()
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
            # Record the FAILED outcome in the unified ledger so the operator SEES
            # which windows errored + how often.
            if ledger is not None:
                ledger.record(
                    key,
                    raw_bars=0,
                    error_type=type(exc).__name__,
                    bars_appended=0,
                    now=datetime.now(UTC),
                )
            return _WindowOutcome(transient=1, window=retry_window, error_type=type(exc).__name__)
        # A TERMINAL (non-retryable) Groww error — including auth, which has no
        # fail-fast latch here (the documented divergence): it simply propagates to
        # the outer run_groww_spot_ingest quarantine, never swallowed.
        raise
    # Drop pre-open / post-close bars before they enter the lake — intraday only
    # (a daily bar has no intraday time). An exempt symbol (e.g. a ~21h instrument)
    # keeps its full-day series verbatim.
    if interval != _DAILY and not is_session_exempt(instrument.symbol_name):
        bars = filter_intraday_session(raw_bars)
    else:
        bars = raw_bars
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
    _LOG.debug(
        "ingest.lake_write",
        pull_id=pull_id,
        bars_appended=result.bars_appended,
        **series_fields,
    )
    # Conflict nook: the stored bar is kept (keep-first — never overwritten,
    # Golden Rule #7). Emit ONE aggregated WARNING per window (count + which
    # OHLCV+OI fields the vendor re-stated + max close delta) instead of one
    # WARNING per bar; per-bar detail stays at DEBUG.
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
    # Record the window's outcome in the unified ledger: SUCCESS when the vendor
    # returned bars, EMPTY on a clean 0-bar response. ``raw_bars`` (NOT
    # ``result.bars_appended``) is the signal — a re-pull whose bars are all
    # already in the lake dedups to ``bars_appended == 0`` yet the window IS
    # complete, so it must stay marked.
    if ledger is not None:
        ledger.record(
            key,
            raw_bars=len(raw_bars),
            error_type=None,
            bars_appended=result.bars_appended,
            now=datetime.now(UTC),
        )
    if on_success is not None:
        on_success()
    return _WindowOutcome(bars_appended=result.bars_appended, conflicts=window_conflicts)


__all__ = [
    "IngestReport",
    "Pacer",
    "clamp_window_end",
    "ingest_spot",
]
