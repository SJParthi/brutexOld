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
from datetime import UTC, date, datetime, timedelta
from time import sleep as _time_sleep

import structlog

from brutex.adapters.groww.chunks import window_days_for_interval
from brutex.adapters.groww.errors import GrowwError, is_rate_limit, is_retryable
from brutex.adapters.groww.fetch_pipeline import FetchPipeline, FetchTail, RawFetchResult
from brutex.adapters.ingest_common import (
    CONFIRMED_EMPTY_MIN_FETCHES,
    GROWW_COVERAGE_MARKET,
    OLD_GAP_HORIZON_DAYS,
    clamp_window_end,
    summarize_conflicts,
    tile_windows,
)
from brutex.adapters.ingest_common import (
    now_ist as _now_ist,
)
from brutex.adapters.protocol import MarketAdapter
from brutex.data.errors import PartitionLockTimeout
from brutex.data.fetch_coverage import WindowKey
from brutex.data.models import CandleBar
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
                empty_retry_attempts=empty_retry_attempts,
                empty_retry_backoff=empty_backoff,
                old_cutoff=old_cutoff,
                fetch_pipeline=fetch_pipeline,
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
        )
    # Async separation: hand the tail off to a background consumer. The producer does NOT
    # wait on it — it returns a DEFERRED outcome immediately and re-paces + fetches the next
    # window. The consumer runs the SAME ``_process_window_tail`` then folds the window's
    # bars/conflicts into the pipeline's pull-level totals. A tail failure (disk-full / sink
    # error) is captured by the pipeline + re-raised on a later submit / drain.
    pipeline = fetch_pipeline

    def _run_tail(_tail: FetchTail) -> None:
        result_outcome = _process_window_tail(
            sink,
            raw,
            ledger=ledger,
            on_success=on_success,
            pull_id=pull_id,
            series_fields=series_fields,
        )
        pipeline.fold_totals(
            bars_appended=result_outcome.bars_appended,
            conflicts=result_outcome.conflicts,
        )

    tail = FetchTail(
        shard_key=_series_shard_key(instrument, interval),
        process=_run_tail,
        raw_bars=raw.raw_bars,
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
                        in_pull_fetches=fetch_count,
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
    # OLD-gap fast-settle (DEC-OLD-GAP-FAST-SETTLE-001): an EMPTY window confirmed empty
    # ACROSS an in-pull retry (``fetch_count >= 2`` — one glitch-prone fetch is not enough;
    # this also excludes the degenerate ``empty_retry_attempts <= 1`` single-fetch case) AND
    # OLD enough that vendor data can never appear (``window_end`` strictly before
    # ``old_cutoff``) is a PERMANENT gap → settle SKIPPED in ONE pull. A RECENT empty window
    # (or one not re-hit) keeps the conservative across-pull EMPTY streak. ``old_cutoff is
    # None`` (no clock threaded) disables the fast-settle. Computed on the PRODUCER from the
    # pure fetch facts (raw bars + fetch count + clock), so the consumer's tail records the
    # same byte-identical ``settle_empty`` it would have synchronously.
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
) -> _WindowOutcome:
    """The CONSUMER half — session-filter + sink-append + conflict-aggregate + ledger.record.

    Runs on the consumer thread (async mode) or inline on the fetch thread (sync mode) — the
    SAME code either way (CLAUDE.md §11 COMMON). All side effects (the sink, the ledger) are
    thread-safe. A :class:`PartitionLockTimeout` DEFERS the window (records FAILED + returns a
    transient outcome) exactly as before; any other sink error (a
    :class:`~brutex.data.errors.LakeWriteHalt` disk-full) propagates — in async mode the
    pipeline captures it into its pool-wide stop, re-raised on the next submit / drain so the
    pull halts through ``_GLOBAL_STOP_ERRORS``.
    """
    instrument = raw.instrument
    interval = raw.interval
    window = raw.window
    window_start, window_end = window
    key = raw.key
    raw_bars = raw.raw_bars
    fetch_count = raw.fetch_count
    settle_empty = raw.settle_empty
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
    try:
        result = sink.append(
            exchange=instrument.exch_id.value,
            segment=instrument.segment.value,
            symbol=instrument.symbol_name,
            interval=interval,
            bars=bars,
        )
    except PartitionLockTimeout as exc:
        # A WEDGED peer held this partition's cross-process lock past the bounded
        # deadline (DEC-PULL-CONTENTION-ANTISTALL-001): DEFER this window to the next
        # pull, exactly like a 429 / timeout transient — record FAILED + return a
        # transient outcome (NOT a symbol-quarantine, NOT a rate-controller step: this is
        # OUR contention, not vendor pressure, so the rate stays unchanged). The peer
        # genuinely progressing finishes its write and this window succeeds on the
        # re-pull; the worker is freed instead of pinned until the 600 s watchdog kill.
        _LOG.warning(
            "ingest.window.lock_deferred",
            pull_id=pull_id,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            timeout_seconds=exc.timeout_seconds,
            **series_fields,
        )
        if ledger is not None:
            ledger.record(
                key,
                raw_bars=0,
                error_type=type(exc).__name__,
                bars_appended=0,
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
    if ledger is not None:
        ledger.record(
            key,
            raw_bars=len(raw_bars),
            error_type=None,
            bars_appended=result.bars_appended,
            now=datetime.now(UTC),
            in_pull_fetches=fetch_count,
            settle_empty=settle_empty,
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
