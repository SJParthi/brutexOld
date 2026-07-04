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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from time import sleep as _time_sleep

import structlog

from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker
from brutex.adapters.error_storm_breaker import (
    ErrorStormBreaker,
    observe_window_failure,
    observe_window_success,
)
from brutex.adapters.groww.chunks import window_days_for_interval
from brutex.adapters.groww.client import client_self_paces
from brutex.adapters.groww.errors import (
    GrowwError,
    is_rate_limit,
    is_retryable,
    steps_timeout_ladder,
)
from brutex.adapters.groww.fetch_pipeline import FetchPipeline, FetchTail, RawFetchResult
from brutex.adapters.groww.write_deadline import append_with_deadline
from brutex.adapters.ingest_common import (
    CONFIRMED_EMPTY_MIN_FETCHES,
    GROWW_COVERAGE_MARKET,
    OLD_GAP_HORIZON_DAYS,
    clamp_window_end,
    drop_bars_after,
    summarize_conflicts,
    tile_windows,
)
from brutex.adapters.ingest_common import (
    now_ist as _now_ist,
)
from brutex.adapters.protocol import MarketAdapter
from brutex.data.errors import PartitionLockTimeout, WriteDeadlineExceeded
from brutex.data.fetch_coverage import WindowKey
from brutex.data.interior_gap_detect import INTERIOR_GAP, observe_interior_gap
from brutex.data.models import CandleBar
from brutex.data.partial_window_detect import SUSPECTED_PARTIAL, observe_partial_window
from brutex.data.session import ALL_FILTERED, filter_to_trading_session, is_session_exempt
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

#: Per-window live-sync PING fired once per PROCESSED window (skipped / clean / empty /
#: transient), on the window worker thread (DEC-SPOT-WINDOW-PROGRESS-001). It lets the
#: dashboard advance its progress bar + flush /coverage per WINDOW — the missing piece
#: behind the operator-observed "stuck at N/779": ``on_progress`` only fires once a whole
#: symbol's multi-year backfill finishes, so both surfaces froze for minutes between
#: symbol completions even while windows kept landing. A no-arg ping keeps the hot path
#: O(1); ALL throttling + thread-safety is the CALLER's contract (the dashboard's
#: ``make_live_sync`` guards it). ``None`` (CLI / tests) is a no-op.
WindowTick = Callable[[], None]

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


#: In-pull EMPTY-response retry budget (DEC-EMPTY-INPULL-RETRY-001). A clean 0-bar
#: response on a Jan-2020+ historical range is AMBIGUOUS on a single call — it is EITHER
#: a genuinely-empty window (pre-listing / holiday / no data) OR a transient vendor
#: glitch ("hallucinated" empty under load) for a range that DOES have data. We cannot
#: tell which from one fetch, so re-fetch the SAME window up to this many TOTAL attempts;
#: the first attempt that returns bars WINS (the glitch is recovered — "pull until a
#: successful response"), and only an all-empty run concludes genuine no-data WITH
#: CONFIDENCE. ``3`` = the operator's "3 tries to be sure"; ``1`` reproduces the prior
#: accept-the-first-empty behaviour. Bounded, so a truly-empty window can never loop
#: forever. ORTHOGONAL to the transient-error retry (that handles 429/timeout) and to the
#: ledger's across-pull EMPTY→SKIPPED threshold (that bounds re-fetches across pulls).
_DEFAULT_EMPTY_RETRY_ATTEMPTS: int = 3


def _noop_empty_backoff(_attempt: int) -> None:
    """Default pause between EMPTY retries: a **no-op** (``_attempt`` intentionally unused).

    The shared rate bucket already spaces every physical call (~200 ms at the 5 r/s
    ceiling), so the empty re-fetches are naturally spread without an extra sleep — and
    on a bulk historical pull with thousands of genuinely-empty windows an added per-
    window sleep would be a large, pointless wall-time cost. Kept as an INJECTABLE hook
    (a recorder in tests, or a real pause at a live site) without making the default
    behaviour sleep. O(1).
    """


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


@dataclass(frozen=True, slots=True)
class _RawWindow:
    """The SUCCESS-path payload handed from ``_fetch_window_raw`` to ``_process_window_tail``.

    A clean fetch (the bars may be empty — an EMPTY window is still a tail to record) carries
    its RAW bars (post-fetch, pre-session-filter) + the per-window metadata the tail needs to
    session-filter + ``sink.append`` + conflict-aggregate + ``ledger.record``. Producer-side
    resolutions (skip / transient / terminal) never build one of these — they short-circuit
    on the producer thread. Immutable so it crosses the producer→consumer queue safely.
    """

    instrument: Instrument
    interval: str
    window: tuple[date, date]
    key: WindowKey
    raw_bars: list[CandleBar]
    fetch_count: int
    settle_empty: bool


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
    empty_retry_attempts: int = _DEFAULT_EMPTY_RETRY_ATTEMPTS,
    empty_retry_backoff: Callable[[int], None] | None = None,
    now_ist: Callable[[], datetime] | None = None,
    on_window: WindowTick | None = None,
    fetch_pipeline: FetchPipeline | None = None,
    detect_completeness_gaps: bool = False,
    empty_breaker: EmptySettleBreaker | None = None,
    storm_breaker: ErrorStormBreaker | None = None,
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
        so this is the only rate governor; nothing is hardcoded. When the
        adapter's Groww client ALREADY self-paces (the client's ``pace`` is bound
        to the shared bucket — the production wiring), the window-level acquire is
        SKIPPED so each physical fetch is admitted exactly ONCE, by the client
        (DEC-RATE-SINGLE-ADMISSION-001); *pace* is then only a pool-sizing hint.
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
    on_window
        Optional :data:`WindowTick` fired once per PROCESSED window (DEC-SPOT-WINDOW-
        PROGRESS-001) so the dashboard can advance its bar + flush /coverage per WINDOW
        instead of only when the whole series finishes. Fired on the window worker thread
        AFTER the window's ledger outcome is recorded; the caller owns throttle +
        thread-safety. ``None`` (CLI / tests) is a no-op.
    fetch_pipeline
        Optional :class:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline`
        (DEC-GROWW-FETCH-ASYNC-SEPARATION-001). When supplied, each window's SUCCESS-path
        tail (session-filter + ``sink.append`` + conflict-aggregate + ``ledger.record`` +
        ``on_success``) is handed OFF to a background consumer instead of running on the
        fetch worker thread — so internal processing can never block or slow the fetch lane.
        The fetch + the rate-controller feedback STAY on the fetch worker (the producer); the
        producer submits the raw bars and immediately re-paces + fetches the next window.
        ``None`` (the default) runs the fetch + tail as ONE synchronous call, byte-identical
        to today. The caller OWNS the pipeline's lifetime and MUST :meth:`drain` it BEFORE the
        final ledger flush. NOTE: in pipeline mode a window's bars/conflicts land on the
        pipeline's pull-level totals (logged at drain), not this per-series report — the
        producer cannot know them synchronously; the Parquet output + ledger settlement are
        byte-identical to the synchronous path.
    empty_breaker
        Optional per-pull :class:`~brutex.adapters.empty_circuit_breaker.EmptySettleBreaker`
        (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001) — the mass-empty circuit breaker shared by
        every window worker of the pass (the run-ingest layer constructs one per pass).
        Each fetched window is observed (data vs confirmed-empty); a mass-empty burst
        RAISES :class:`~brutex.adapters.empty_circuit_breaker.MassEmptySettleHalt` (a
        global stop — the pull halts loudly instead of settling poisoned windows), and a
        tripped breaker suppresses every remaining EMPTY settle at ledger-record time.
        ``None`` (unit-test / legacy callers) observes nothing — byte-identical behaviour.
    storm_breaker
        Optional per-pull :class:`~brutex.adapters.error_storm_breaker.ErrorStormBreaker`
        (DEC-VENDOR-DOWN-BREAKER-001) — the vendor-down error-storm breaker + the
        dead-credential StillStale escalator, shared by every window worker of the
        pull. Each window's LADDER-EXHAUSTED transport outcome is observed (a clean
        fetch — bars or vendor-confirmed empty — is a SUCCESS that resets both
        streaks); a sustained zero-success storm RAISES
        :class:`~brutex.adapters.error_storm_breaker.VendorDownError` and a dead
        credential :class:`~brutex.adapters.groww.errors.GrowwCredentialDeadError`
        (both global stops — the pull halts loudly). ``None`` (the default — every
        dry-run / synthetic / unit-test caller) observes nothing; ONLY the
        real-vendor production sites arm it (the ``detect_completeness_gaps``
        gating pattern).

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
    now_for_clamp = (now_ist or _now_ist)()
    clamped_end = clamp_window_end(interval, end, now_ist=now_for_clamp)
    # OLD-gap fast-settle cutoff (DEC-OLD-GAP-FAST-SETTLE-001): a window whose ``end`` is
    # strictly before this date is OLD enough that vendor data can never appear (Groww serves
    # a stable 2020+ history; backfill lag is days, not months). Computed ONCE off the SAME
    # injected clock the current-day clamp uses, so a test's injected ``now_ist`` makes the
    # decision deterministic. Passed down to ``_ingest_one_window``.
    old_cutoff = now_for_clamp.date() - timedelta(days=OLD_GAP_HORIZON_DAYS)
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
    empty_backoff = empty_retry_backoff if empty_retry_backoff is not None else _noop_empty_backoff
    # SINGLE ADMISSION (DEC-RATE-SINGLE-ADMISSION-001): when the adapter's Groww client
    # already self-paces (``run_groww_spot_ingest`` binds the shared bucket into the
    # client, whose ``_call`` then acquires ONE token per physical attempt — retry
    # attempts included), the per-window ``pace()`` below would be a SECOND admission
    # for the SAME physical fetch — halving the achieved rate and double-charging the
    # 500/min governor. The client is the canonical admission point; the window-level
    # pace stays ONLY as the fallback for an un-paceable adapter (a fake / synthetic).
    # The raw *pace* is still read above for pool SIZING (a hint, never an admission).
    window_pace = None if client_self_paces(getattr(adapter, "client", None)) else pace

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
                pace=window_pace,
                ledger=ledger,
                on_rate_limit=on_rate_limit,
                on_timeout=on_timeout,
                on_success=on_success,
                pull_id=pull_id,
                series_fields=series_fields,
                empty_retry_attempts=empty_retry_attempts,
                empty_retry_backoff=empty_backoff,
                old_cutoff=old_cutoff,
                fetch_pipeline=fetch_pipeline,
                detect_completeness_gaps=detect_completeness_gaps,
                empty_breaker=empty_breaker,
                storm_breaker=storm_breaker,
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
            # Per-window live-sync ping (DEC-SPOT-WINDOW-PROGRESS-001): the window's ledger
            # outcome is already recorded by the time its result is yielded, so a ping here
            # lets the dashboard reflect THIS window (throttled by the caller). Fired for
            # every processed window — skip / clean / empty / transient AND the terminal
            # case below (the window WAS attempted) — before the terminal re-raise.
            if on_window is not None:
                on_window()
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
    empty_retry_attempts: int = _DEFAULT_EMPTY_RETRY_ATTEMPTS,
    empty_retry_backoff: Callable[[int], None] = _noop_empty_backoff,
    old_cutoff: date | None = None,
    fetch_pipeline: FetchPipeline | None = None,
    detect_completeness_gaps: bool = False,
    empty_breaker: EmptySettleBreaker | None = None,
    storm_breaker: ErrorStormBreaker | None = None,
) -> _WindowOutcome:
    """Fetch + session-filter + sink-append ONE window; return its outcome.

    The window is processed in TWO halves (DEC-GROWW-FETCH-ASYNC-SEPARATION-001):
    :func:`_fetch_window_raw` (pace + fetch + in-pull EMPTY-retry + the rate-controller
    feedback) STAYS on the fetch (producer) thread; :func:`_process_window_tail`
    (session-filter + ``sink.append`` + conflict-aggregate + ``ledger.record`` +
    ``on_success``) is the consumer work. When *fetch_pipeline* is ``None`` (the default)
    the two run as ONE synchronous call — BYTE-IDENTICAL to the prior single-function body.
    When a pipeline is wired the producer runs only the fetch half, hands the raw bars off
    via :meth:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline.submit` (a bounded ``put``
    that blocks ONLY on back-pressure, never on durability), and returns a DEFERRED outcome
    (the tail's bars/conflicts land on the pipeline's pull-level totals); a consumer thread
    runs the SAME ``_process_window_tail`` (CLAUDE.md §11 COMMON — one tail).

    All side effects (the *pace* bucket, the controller hooks, the sink) are thread-safe,
    so N windows of one series run concurrently without a data race. A retryable Groww
    error is counted on the producer; a terminal :class:`GrowwError` (auth included)
    propagates to the outer quarantine.

    A clean **0-bar (EMPTY) response is re-fetched** up to *empty_retry_attempts* times
    (DEC-EMPTY-INPULL-RETRY-001) — see :func:`_fetch_window_raw`. *old_cutoff*
    (DEC-OLD-GAP-FAST-SETTLE-001) drives the OLD-gap fast-settle — see
    :func:`_process_window_tail`.
    """
    outcome, raw = _fetch_window_raw(
        adapter,
        instrument=instrument,
        interval=interval,
        window=window,
        pace=pace,
        ledger=ledger,
        on_rate_limit=on_rate_limit,
        on_timeout=on_timeout,
        pull_id=pull_id,
        series_fields=series_fields,
        empty_retry_attempts=empty_retry_attempts,
        empty_retry_backoff=empty_retry_backoff,
        old_cutoff=old_cutoff,
        empty_breaker=empty_breaker,
        storm_breaker=storm_breaker,
    )
    if raw is None:
        # Producer-resolved (skip / transient / terminal-raised): never touches the tail.
        return outcome
    if fetch_pipeline is None:
        # Synchronous, byte-identical path: run the tail inline on THIS thread.
        return _process_window_tail(
            sink,
            raw,
            ledger=ledger,
            on_success=on_success,
            pull_id=pull_id,
            series_fields=series_fields,
            detect_completeness_gaps=detect_completeness_gaps,
            empty_breaker=empty_breaker,
        )
    # Async separation: hand the tail off to a background consumer. The producer does NOT
    # wait on it — it returns a DEFERRED outcome immediately and re-paces + fetches the next
    # window. The consumer runs the SAME ``_process_window_tail`` then folds the window's
    # bars/conflicts into the pipeline's pull-level totals. A tail failure (disk-full / sink
    # error) is captured by the pipeline + re-raised on a later submit / drain.
    pipeline = fetch_pipeline

    # The per-series reconcile key MUST equal the key the spine stores this series'
    # ``IngestReport`` under (``run_ingest`` keys spot pairs by ``(symbol_name, interval)``), so
    # the controller's post-drain reconcile lines up the folded consumer totals with the right
    # deferred report (the completion-count fix).
    series_key = (instrument.symbol_name, interval)

    def _run_tail(_tail: FetchTail) -> None:
        result_outcome = _process_window_tail(
            sink,
            raw,
            ledger=ledger,
            on_success=on_success,
            pull_id=pull_id,
            series_fields=series_fields,
            detect_completeness_gaps=detect_completeness_gaps,
            empty_breaker=empty_breaker,
        )
        if result_outcome.transient:
            # A tail-side PartitionLockTimeout DEFERRED this window (a wedged peer held the
            # cross-process lock past the deadline). Surface it via the pipeline's tail-transient
            # tally so it is counted + reported at drain and the across-pull retry round still
            # fires — otherwise an async-mode tail-transient was silently dropped (MAJOR-2 fix).
            # The window's ledger row is already recorded FAILED, so a re-pull re-fetches it.
            pipeline.fold_tail_transient(series_key)
            return
        pipeline.fold_totals(
            bars_appended=result_outcome.bars_appended,
            conflicts=result_outcome.conflicts,
            series_key=series_key,
        )

    def _rehydrate(bars: Sequence[CandleBar]) -> None:
        # The pipeline's spill hook (DEC-FETCH-NONBLOCKING-HANDOFF-001): swap the window's bars
        # in place in the closure's captured ``raw`` so ``_process_window_tail`` reads the
        # (dropped-on-spill / restored-on-reload) bars. ``raw`` is frozen but ``raw_bars`` is a
        # mutable list — assigning its slice mutates the SAME object the tail reads.
        raw.raw_bars[:] = bars

    tail = FetchTail(
        shard_key=_series_shard_key(instrument, interval),
        process=_run_tail,
        raw_bars=raw.raw_bars,
        rehydrate=_rehydrate,
    )
    fetch_pipeline.submit(RawFetchResult(tail=tail))
    # DEFERRED: the consumer owns the bars/conflicts (pull-level totals); the producer
    # reports zero for this window — the window is neither skipped nor transient here.
    return _WindowOutcome()


def _series_shard_key(instrument: Instrument, interval: str) -> str:
    """The consumer routing key for a spot series — the resolved partition coordinates.

    Mirrors :meth:`~brutex.data.write_consumer.WriteRequest.shard_key` so all windows of one
    ``(exchange, segment, symbol, interval)`` series route to ONE consumer (the per-partition
    lock invariant). Uses the SAME field values the spot ``sink.append`` passes.
    """
    return (
        f"{instrument.exch_id.value}\x00{instrument.segment.value}"
        f"\x00{instrument.symbol_name}\x00{interval}"
    )


def _fetch_window_raw(
    adapter: MarketAdapter,
    *,
    instrument: Instrument,
    interval: str,
    window: tuple[date, date],
    pace: Pacer | None,
    ledger: WindowLedger | None,
    on_rate_limit: Callable[[], None] | None,
    on_timeout: Callable[[], None] | None,
    pull_id: str,
    series_fields: dict[str, str],
    empty_retry_attempts: int,
    empty_retry_backoff: Callable[[int], None],
    old_cutoff: date | None,
    empty_breaker: EmptySettleBreaker | None = None,
    storm_breaker: ErrorStormBreaker | None = None,
) -> tuple[_WindowOutcome, _RawWindow | None]:
    """The PRODUCER half — pace + fetch + EMPTY-retry + controller feedback. STAYS on the fetch thread.

    Returns ``(outcome, None)`` when the window is PRODUCER-resolved (a ledger skip or a
    transient error — the tail is never reached) and ``(_WindowOutcome(), raw)`` when the
    fetch succeeded (a :class:`_RawWindow` carrying the raw bars + metadata for the tail; the
    bars may be empty — an EMPTY window is still a tail to record). A terminal
    :class:`GrowwError` (auth included) is RAISED on the producer thread to the outer
    quarantine, never swallowed.

    A clean 0-bar (EMPTY) response is re-fetched up to *empty_retry_attempts* times
    (DEC-EMPTY-INPULL-RETRY-001): a Jan-2020+ historical range can return an empty ``200``
    EITHER because the window is genuinely empty OR because of a transient vendor glitch on a
    range that DOES have data — the first attempt that returns bars wins, and only an
    all-empty run records genuine no-data WITH confidence. Bounded, so a truly-empty window
    can never loop forever. The rate-controller feedback (``on_rate_limit`` / ``on_timeout``)
    + the rate ``pace`` bucket are the producer's job and STAY here (CLAUDE.md §2 Rule 6 — the
    8/500 + AIMD limiter is producer-side, untouched).
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
        return _WindowOutcome(skipped=1), None
    # In-pull EMPTY retry (DEC-EMPTY-INPULL-RETRY-001): a clean 0-bar response is
    # AMBIGUOUS on a single call — genuine no-data vs a transient vendor glitch on a range
    # that DOES have data — so re-fetch the SAME window up to empty_retry_attempts times.
    # The first attempt that returns bars WINS (the glitch is recovered — "pull until a
    # successful response"); only an all-empty run falls through to record genuine no-data
    # WITH confidence. A retryable error short-circuits to the transient path (its own
    # multi-pass retry); a terminal one propagates. Bounded → a truly-empty window can
    # never loop forever.
    raw_bars: list[CandleBar] = []
    # Count the vendor fetches THIS pull makes for this window (DEC-WINDOW-INPULL-FETCHES-001):
    # ``1`` on a first-try win, up to ``empty_retry_attempts`` when the in-pull retry fires.
    # Recorded on the ledger row so the operator SEES the 3× retry that was previously
    # DEBUG-only (the "attempts 1 but no sign of the retry" gap).
    fetch_count = 0
    # ``max(1, …)`` mirrors the option spine's guard (option_ingest.py): a degenerate
    # ``empty_retry_attempts < 1`` must still fetch EXACTLY once — never silently record an
    # EMPTY window WITHOUT calling the vendor (the out-of-box guard; the production default
    # is 3, never < 1). This also keeps ``in_pull_fetches`` honest (≥1 for any recorded
    # outcome, 0 only for a never-fetched PLANNED row).
    for empty_attempt in range(max(1, empty_retry_attempts)):
        if pace is not None:
            pace()
        fetch_count += 1
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
                # rate-limit steps the rate down, a NON-auth transient drives the gentle
                # timeout ladder (`steps_timeout_ladder`, DEC-AUTH-LADDER-EXCLUDE-001) —
                # a retryable auth-family failure (token-stale / mint-throttle) feeds
                # NEITHER hook: it is no evidence about candle-endpoint health, so it
                # must not ease the pull rate (the FAILED record + storm-breaker feed
                # below stay byte-identical). A one-off blip is ignored by the
                # controller's threshold.
                rate_limited = is_rate_limit(exc)
                if rate_limited:
                    if on_rate_limit is not None:
                        on_rate_limit()
                elif on_timeout is not None and steps_timeout_ladder(exc):
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
                        bars_net_new=0,  # the failed fetch stored nothing (a known 0)
                        now=datetime.now(UTC),
                        in_pull_fetches=fetch_count,
                    )
                # Vendor-down error-storm breaker + dead-credential StillStale escalation
                # (DEC-VENDOR-DOWN-BREAKER-001): this window's whole bounded retry ladder
                # (the client's max_attempts fast-retry) is exhausted — feed the per-pull
                # breaker AFTER the ledger record (the window stays re-pullable). The
                # ledger `key` is the window's stable identity, so a retry pass /
                # until-clean round re-observing the SAME failed window never re-counts
                # it (each distinct window counts once per pull). A sustained
                # zero-success storm across distinct symbols + distinct windows RAISES
                # VendorDownError here on the producer thread — a GLOBAL stop; K
                # consecutive StillStale outcomes over T minutes RAISE
                # GrowwCredentialDeadError (also a global stop). None (dry-run /
                # synthetic / tests) observes nothing.
                observe_window_failure(
                    storm_breaker, symbol=instrument.symbol_name, window_key=key, exc=exc
                )
                return (
                    _WindowOutcome(transient=1, window=retry_window, error_type=type(exc).__name__),
                    None,
                )
            # A TERMINAL (non-retryable) Groww error — including auth, which has no
            # fail-fast latch here (the documented divergence): it simply propagates to
            # the outer run_groww_spot_ingest quarantine, never swallowed.
            raise
        if raw_bars:
            break  # bars on this attempt — the (possibly glitched) empty is recovered
        if empty_attempt + 1 < empty_retry_attempts:
            # Still empty + a retry remains: pause briefly and re-fetch (the glitch may clear).
            _LOG.debug(
                "ingest.window.empty_retry",
                pull_id=pull_id,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
                attempt=empty_attempt + 1,
                of=empty_retry_attempts,
                **series_fields,
            )
            empty_retry_backoff(empty_attempt)
    # Mass-empty circuit breaker (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): feed the window's
    # final fetch shape to the per-pull breaker. A data-bearing window resets its
    # consecutive-empty run (and retro-clears this series' leading pre-listing prefix); a
    # final-empty window counts toward the trip rules and — the moment either rule
    # crosses — RAISES MassEmptySettleHalt on this producer thread, which propagates as a
    # GLOBAL stop (the pull halts loudly; the tripping window is never recorded, so it
    # stays retryable). A window whose OWN ledger row already says EMPTY (a prior pull saw
    # it empty — the empty-dominated RESUME shape) or is REOPENED-marked (a deliberate
    # `brutex reopen-skipped` re-check) re-confirming empty is EXPECTED, not throttle
    # evidence — observed but never counted (`is_expected_empty_recheck`). Observed BEFORE
    # the fast-settle math below so a poisoned window can never settle on the very call
    # that trips the breaker.
    # Vendor-down error-storm breaker (DEC-VENDOR-DOWN-BREAKER-001): a clean fetch —
    # bars OR a vendor-confirmed empty 200 — is a transport SUCCESS (the vendor answered:
    # it is ALIVE), so it resets BOTH the vendor-down storm and the StillStale streak.
    # Observed before the mass-EMPTY breaker feed (which may itself raise on a poisoned
    # empty — a separate, content-level verdict).
    observe_window_success(storm_breaker, symbol=instrument.symbol_name)
    if empty_breaker is not None:
        series = f"{instrument.symbol_name}:{interval}"
        if raw_bars:
            empty_breaker.observe_data(series)
        else:
            empty_breaker.observe_empty(
                series,
                symbol=instrument.symbol_name,
                window_end=window_end,
                expected=ledger is not None and ledger.is_expected_empty_recheck(key),
            )
    # OLD-gap fast-settle (DEC-OLD-GAP-FAST-SETTLE-001): an EMPTY window confirmed empty
    # ACROSS an in-pull retry (``fetch_count >= 2`` — one glitch-prone fetch is not enough;
    # this also excludes the degenerate ``empty_retry_attempts <= 1`` single-fetch case) AND
    # OLD enough that vendor data can never appear (``window_end`` strictly before
    # ``old_cutoff``) is a PERMANENT gap → settle SKIPPED in ONE pull. A RECENT empty window
    # (or one not re-hit) keeps the conservative across-pull EMPTY streak. ``old_cutoff is
    # None`` (no clock threaded) disables the fast-settle. Computed on the PRODUCER from the
    # pure fetch facts (raw bars + fetch count + clock); the tail RE-CHECKS the breaker at
    # record time (``allow_settle``), so a settle computed here but recorded after a trip is
    # still suppressed (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001).
    confirmed_empty_after_retry = (not raw_bars) and fetch_count >= CONFIRMED_EMPTY_MIN_FETCHES
    is_old = old_cutoff is not None and window_end < old_cutoff
    settle_empty = confirmed_empty_after_retry and is_old
    return (
        _WindowOutcome(),
        _RawWindow(
            instrument=instrument,
            interval=interval,
            window=window,
            key=key,
            raw_bars=raw_bars,
            fetch_count=fetch_count,
            settle_empty=settle_empty,
        ),
    )


def _process_window_tail(
    sink: BarSink,
    raw: _RawWindow,
    *,
    ledger: WindowLedger | None,
    on_success: Callable[[], None] | None,
    pull_id: str,
    series_fields: dict[str, str],
    detect_completeness_gaps: bool = False,
    empty_breaker: EmptySettleBreaker | None = None,
) -> _WindowOutcome:
    """The CONSUMER half — session-filter + sink-append + conflict-aggregate + ledger.record.

    Runs on the consumer thread (async mode) or inline on the fetch thread (sync mode) — the
    SAME code either way (CLAUDE.md §11 COMMON). All side effects (the sink, the ledger) are
    thread-safe. The durable write goes through
    :func:`~brutex.adapters.groww.write_deadline.append_with_deadline`, so a wedged
    fsync/replace that NEVER raises (a hung durable write) is bounded by
    :data:`~brutex.adapters.groww.constants.GROWW_WRITE_DEADLINE_S` and surfaces as a
    :class:`~brutex.data.errors.WriteDeadlineExceeded` instead of blocking the fetch lane
    forever (DEC-FETCH-HUNG-WRITE-DEADLINE-001). BOTH that and a
    :class:`PartitionLockTimeout` (the cross-process lock ACQUIRE bound) DEFER the window
    (record FAILED + return a transient outcome) — they are OUR contention / our-pipeline
    stall, not a fatal halt, so the window re-pulls next round and the fetch lane is freed.
    Any other sink error (a :class:`~brutex.data.errors.LakeWriteHalt` disk-full) propagates —
    in async mode the pipeline captures it into its pool-wide stop, re-raised on the next
    submit / drain so the pull halts through ``_GLOBAL_STOP_ERRORS``.
    """
    instrument = raw.instrument
    interval = raw.interval
    window = raw.window
    window_start, window_end = window
    key = raw.key
    raw_bars = raw.raw_bars
    fetch_count = raw.fetch_count
    # Breaker re-check at RECORD time (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): once the
    # pull's mass-empty breaker trips, NO further window may settle SKIPPED — including a
    # settle computed on the producer BEFORE the trip but landing here after it (an
    # async-pipeline queued tail, or a sibling in-flight worker). The suppressed empty is
    # recorded as UNTRUSTED (EMPTY, no streak bump — ``suppress_settle``), so it stays
    # fully retryable with no poisoned progress toward the across-pull promotion.
    suppress_settle = empty_breaker is not None and not empty_breaker.allow_settle()
    settle_empty = raw.settle_empty and not suppress_settle
    # CALENDAR-AWARE session filter (DEC-INGEST-BOUNDARY-INTEGRITY-001): drop bars
    # outside their OWN IST date's real NSE session before they enter the lake —
    # pre-open / post-close bars AND bars on non-trading dates (a weekend/holiday
    # leak), while an evening-muhurat / special-weekend session's bars are KEPT
    # (the prior date-blind fixed 09:15-15:30 filter wrongly dropped muhurat bars
    # and kept holiday bars whose wall clock looked in-session). Intraday only (a
    # daily bar has no intraday time — deliberately unfiltered); an exempt symbol
    # (e.g. a ~21h instrument) keeps its full-day series verbatim.
    all_session_filtered = False
    if interval != _DAILY and not is_session_exempt(instrument.symbol_name):
        session_verdict = filter_to_trading_session(raw_bars)
        bars = session_verdict.kept
        if session_verdict.dropped:
            # EXACTLY ONE aggregated WARNING per window (never per bar), carrying
            # the window identity + per-cause counts, so an out-of-session /
            # non-trading-date leak is visible without flooding a long pull's log.
            _LOG.warning(
                "ingest.session_filter.dropped",
                pull_id=pull_id,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
                dropped=session_verdict.dropped,
                off_session=session_verdict.dropped_off_session,
                non_trading_date=session_verdict.dropped_non_trading_date,
                **series_fields,
            )
        all_session_filtered = session_verdict.all_filtered
    else:
        bars = raw_bars
    # Drop any bar timestamped on a date STRICTLY AFTER the clamped window end
    # (DEC-CANDLE-DROP-FUTURE-BAR-001): a vendor bar PAST the clamped bound would write a
    # PHANTOM FUTURE partition. The window is already clamped (no current-day request), so
    # this only ever removes an out-of-window / clock-skew phantom — a bar ON the boundary
    # is KEPT, so the existing behaviour is byte-unchanged for every legitimate in-range bar.
    bars = drop_bars_after(bars, window_end)
    _LOG.debug(
        "ingest.parse",
        pull_id=pull_id,
        raw_bars=len(raw_bars),
        session_bars=len(bars),
        **series_fields,
    )
    try:
        # Bound the durable write by a wall-clock deadline so a wedged fsync/replace (a hung
        # durable write that never raises) can NEVER block this consumer — and therefore the
        # back-pressured producer / fetch lane — forever (DEC-FETCH-HUNG-WRITE-DEADLINE-001).
        result = append_with_deadline(
            sink,
            exchange=instrument.exch_id.value,
            segment=instrument.segment.value,
            symbol=instrument.symbol_name,
            interval=interval,
            bars=bars,
        )
    except (PartitionLockTimeout, WriteDeadlineExceeded) as exc:
        # DEFER this window to the next pull, exactly like a 429 / timeout transient — record
        # FAILED + return a transient outcome (NOT a symbol-quarantine, NOT a rate-controller
        # step: a lock-acquire timeout is OUR partition contention and a write-deadline is OUR
        # wedged-disk hang — both are our-pipeline stalls, not vendor pressure, so the rate
        # stays unchanged). The window re-pulls next round; the worker / fetch lane is freed
        # instead of pinned until the 600 s watchdog kill. ``error_type`` (the class name)
        # distinguishes ``PartitionLockTimeout`` (lock acquire) from ``WriteDeadlineExceeded``
        # (hung durable write) in the ledger + the WARNING below.
        defer_seconds = (
            exc.timeout_seconds if isinstance(exc, PartitionLockTimeout) else exc.deadline_seconds
        )
        _LOG.warning(
            "ingest.window.write_deferred",
            pull_id=pull_id,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            error_type=type(exc).__name__,
            defer_seconds=defer_seconds,
            **series_fields,
        )
        if ledger is not None:
            ledger.record(
                key,
                raw_bars=0,
                error_type=type(exc).__name__,
                bars_appended=0,
                bars_net_new=0,  # the deferred write stored nothing (a known 0)
                now=datetime.now(UTC),
                in_pull_fetches=fetch_count,
            )
        return _WindowOutcome(transient=1, window=window, error_type=type(exc).__name__)
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
    # Surface the in-pull retry at INFO when it fired AND stayed empty
    # (DEC-WINDOW-INPULL-FETCHES-001): the per-attempt log above is DEBUG, so a pull
    # at the operator's INFO level showed NO sign the 3× retry ran — this one line
    # makes "re-fetched N×, still empty" visible in the log, mirroring the new ledger
    # column on /coverage/windows. A first-try outcome (fetch_count == 1) stays quiet.
    # ``settled`` flags an old-gap fast-settle (DEC-OLD-GAP-FAST-SETTLE-001) so the log is
    # honest about whether this empty window became terminal SKIPPED in this one pull.
    if not raw_bars and fetch_count > 1:
        _LOG.info(
            "ingest.window.empty_after_retries",
            pull_id=pull_id,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            fetches=fetch_count,
            settled=settle_empty,
            **series_fields,
        )
    # DATA-COMPLETENESS scans (DEC-PARTIAL-WINDOW-DETECT-001 / DEC-PARTIAL-WINDOW-REPULL-001
    # + DEC-INTERIOR-GAP-DETECT-001): a SUCCESS spot window whose SESSION-FILTERED bars either
    # (a) stop materially before the expected end (a TRAILING truncation) or (b) drop out for
    # a long contiguous run of trading sessions in the MIDDLE (a confirmed INTERIOR hole
    # bookended by real bars) is NOT complete — yet the bars-returning ``SUCCESS`` classifier
    # + skip rule would never revisit it. Spot indices trade DENSELY every session, so both
    # are genuine, reliably-detectable completeness gaps with no false-positive liquidity
    # noise. Both COMMON helpers WARN (observe) AND return their verdict; a flagged window is
    # DEMOTED to a re-pullable ledger status (``degraded=True``), so the window re-pulls to
    # complete next pass — BOUNDED (it settles SKIPPED after EMPTY_TERMINAL_THRESHOLD flagged
    # pulls, so the universe pull still converges) and DATA-SAFE (the stored bars are kept,
    # the demotion only re-opens the window). The helpers are fully guarded (a scan error
    # returns ``None`` ⇒ never demote ⇒ the byte-identical observe-only fallback), so the
    # diagnostic can never block / slow / crash the pull. Spot has no expiry (``expiry=None``).
    partial_verdict = observe_partial_window(
        _LOG,
        last_bar_ts=bars[-1].timestamp if bars else None,
        window_end=window_end,
        now=datetime.now(UTC),
        expiry=None,
        symbol=instrument.symbol_name,
        interval=interval,
        pull_id=pull_id,
    )
    interior_verdict = observe_interior_gap(
        _LOG,
        bar_timestamps=[bar.timestamp for bar in bars],
        symbol=instrument.symbol_name,
        interval=interval,
        pull_id=pull_id,
    )
    truncated = partial_verdict is not None and partial_verdict.is_suspected_partial()
    interior_gap = interior_verdict is not None and interior_verdict.is_interior_gap()
    # DEMOTE only a DENSE INDEX on the FETCHED ``1minute`` interval, and only when the caller
    # OPTED IN via *detect_completeness_gaps*. Three hard gates:
    #   (1) ``detect_completeness_gaps`` — the demotion is a REAL-VENDOR completeness signal,
    #       OPT-IN at the production live-pull build sites ONLY. A ``--dry-run`` / synthetic /
    #       offline pull (``cli._DryRunGrowwSpotAdapter`` → ``synthetic_spot_bars``, which returns
    #       a few bars on the window's FIRST day regardless of span) would otherwise see every
    #       wide window as "truncated" and demote it to FAILED — a false-red on the dry-run
    #       whose whole purpose is to prove the pipe runs GREEN. Defaulting OFF keeps dry-run /
    #       synthetic / unit-test pulls byte-identical; the production dashboard + CLI-live pull
    #       set it ``True``. (The observe-only WARNING below still fires regardless — visibility
    #       is never gated, only the re-pull action.)
    #   (2) ``instrument.is_index`` — the false-positive-safety rationale (a hole/truncation is a
    #       GENUINE completeness gap, not liquidity noise) holds ONLY for the densely-traded
    #       indices (NIFTY/BANKNIFTY/SENSEX), which print every session. The spot pull ALSO fans
    #       out over the ~750 REFERENCE_ONLY underlyings (illiquid NTM small/mid/micro-caps +
    #       delisted/suspended names), where a multi-session no-print stretch is legitimately
    #       sparse-but-complete — demoting those would be a re-pull storm.
    #   (3) ``interval != _DAILY`` — the derived ``1day`` partition is RESAMPLED from the stored
    #       1-minute (DEC-1MINUTE-ONLY-DERIVE-1DAY-001), never vendor-fetched, so a "truncated"
    #       daily window is meaningless; its completeness follows the 1-minute.
    demotable = detect_completeness_gaps and instrument.is_index and interval != _DAILY
    # ALL-FILTERED demotion (DEC-INGEST-BOUNDARY-INTEGRITY-001): the vendor returned
    # bars but EVERY one was dropped by the calendar-aware session filter — a
    # would-be SUCCESS with ZERO stored bars the skip rule would never revisit.
    # INTRINSIC (vendor sent bars, all out-of-session — not a liquidity heuristic),
    # so it is NOT gated on ``instrument.is_index``; it IS gated on
    # ``detect_completeness_gaps`` because the dry-run synthetic adapter anchors its
    # bars on the window-START day regardless of the trading calendar, so a
    # weekend-start ``--dry-run`` window would otherwise demote → false-red (the
    # same protection gate #1031 chose). Orthogonal to sink dedup: a re-pull whose
    # bars all dedup to zero NET-NEW still passes bars THROUGH the filter
    # (``bars`` non-empty), so it stays SUCCESS.
    all_filtered = detect_completeness_gaps and all_session_filtered
    degraded = all_filtered or (demotable and (truncated or interior_gap))
    if all_filtered:
        degraded_reason: str | None = ALL_FILTERED
    elif demotable and interior_gap:
        degraded_reason = INTERIOR_GAP
    elif demotable and truncated:
        degraded_reason = SUSPECTED_PARTIAL
    else:
        degraded_reason = None
    # Record the window's outcome in the unified ledger: SUCCESS when the vendor returned
    # COMPLETE bars, EMPTY on a clean 0-bar response, or a DEMOTED re-pullable FAILED when the
    # bars are incomplete (interior gap / truncation). ``raw_bars`` (NOT
    # ``result.bars_appended``) is the signal — a re-pull whose bars are all already in the
    # lake dedups to ``bars_appended == 0`` yet the window IS complete, so it must stay marked.
    if ledger is not None:
        ledger.record(
            key,
            raw_bars=len(raw_bars),
            error_type=None,
            bars_appended=result.bars_appended,
            # Persist the sink's keep-first-DEDUPED net-new count alongside the
            # input count (DEC-INGEST-BOUNDARY-INTEGRITY-001 — #1031 computed it
            # but never persisted it).
            bars_net_new=result.bars_net_new,
            now=datetime.now(UTC),
            in_pull_fetches=fetch_count,
            settle_empty=settle_empty,
            degraded=degraded,
            degraded_reason=degraded_reason,
            suppress_settle=suppress_settle,
        )
    if on_success is not None:
        on_success()
    return _WindowOutcome(bars_appended=result.bars_appended, conflicts=window_conflicts)


__all__ = [
    "IngestReport",
    "Pacer",
    "WindowTick",
    "clamp_window_end",
    "ingest_spot",
]
