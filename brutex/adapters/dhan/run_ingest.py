"""Dhan multi-instrument spot-ingest orchestrator — the cli/dashboard entry.

Drives :func:`~brutex.adapters.dhan.ingest.ingest_spot` across every
``(instrument, interval)`` pair resiliently: a **terminal** Dhan error on one
pair is quarantined (recorded as failed) and the run continues to the next
pair — one bad symbol never kills the whole pull. Per-window transient errors
are already retried/skipped inside ``ingest_spot``; this layer adds the
multi-instrument fan-out + the per-pair quarantine + an aggregate report.

This is the Dhan-native analog of the legacy ``run_ingest`` the dashboard
``pull_controller`` calls — the seam the Groww→Dhan rewire (DEC-VENDOR-PIVOT-001)
swaps to. The adapter, sink and pacing hook are all injected, so it is 100%
unit-testable without a network or a real lake; pacing keeps the **5 r/s /
100k-day** Dhan limit honoured (CLAUDE.md §2 Rule 6).

Parallel fan-out (DEC-DHAN-PARALLEL-INGEST-001). The OUTER loop — one job per
``(instrument, interval)`` pair — runs through an injected
:class:`~brutex.orchestrator.universe_phases.RunParallel` (production: a bounded
:class:`~brutex.orchestrator.parallel.PoolRunParallel`) so many windows are
in flight at once and the pull **targets/saturates UP TO ~5 r/s** when Dhan is
healthy (vs the old ~1 r/s sequential stream). Each pair writes a DISTINCT lake
partition (``symbol``×``interval``), so the spot writes are path-isolated and
need no extra lock; the shared rate bucket (the injected ``pace``) is the
governor that keeps the aggregate ``≤ 5 r/s`` (Rule #6). **Honest envelope
(Golden Rule #10):** this targets up to ~5 r/s — it CANNOT guarantee a constant
5 r/s (a 429 step-down + per-call latency spikes lower the achieved rate); the
``run_parallel`` seam is the *mechanism* that lets the rate bucket, not a
single-threaded loop, be the bottleneck. The per-window ``ingest_spot`` spine is
UNCHANGED. ``run_parallel`` is injectable so tests drive a synchronous fake
(mirrors :func:`brutex.orchestrator.universe_phases.run_two_phase`).

Idempotent: ``ingest_spot``'s sink append is rerun-safe, so re-running a pull
over the same range is a no-op on already-stored bars (CLAUDE.md §2 Rule 7). The
per-instrument ``ingest.underlying`` rollup is emitted in a DETERMINISTIC
post-pass (grouped + sorted by ``symbol_name``) so the log order is reproducible
regardless of worker completion order (CLAUDE.md §2 Rule 4).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date

import structlog

from brutex.adapters.dhan.errors import DhanError, is_no_data_error
from brutex.adapters.dhan.ingest import IngestReport, Pacer, ingest_spot
from brutex.adapters.protocol import MarketAdapter
from brutex.data.fetch_coverage import FetchCoverage
from brutex.data.sink import BarSink
from brutex.data.window_status import WindowStatusLog
from brutex.models import Instrument
from brutex.observability.correlation import current_pull_id, pull_id_scope
from brutex.orchestrator.parallel import PoolRunParallel
from brutex.orchestrator.universe_phases import RunParallel

#: Structured logger — emits the per-underlying spot rollup at INFO
#: (``ingest.underlying``) + the terminal-failure cause at WARNING
#: (``ingest.quarantine``), both carrying the pull's ``pull_id``
#: (DEC-DHAN-INGEST-LOGGING-001). The per-window / per-series DEBUG detail is one
#: layer down in ``ingest_spot``.
_LOG = structlog.get_logger("brutex.adapters.dhan.run_ingest")

#: Live-progress hook fired once per ``(symbol, interval)`` pair after it is
#: processed: ``(label, bars_appended, failed)``. Lets a caller stream
#: real-time progress (e.g. into a ``PullProgress`` tracker) without coupling
#: this layer to the dashboard. ``bars_appended`` is 0 for a quarantined pair.
ProgressHook = Callable[[str, int, bool], None]

#: One unit of OUTER-loop work for the parallel fan-out: a spot
#: ``(instrument, interval)`` pair. Each maps to a distinct lake partition, so
#: the per-pair writes are path-isolated (DEC-DHAN-PARALLEL-INGEST-001).
SpotJob = tuple[Instrument, str]


def _with_paced_client(adapter: MarketAdapter, pace: Pacer | None) -> MarketAdapter:
    """Return *adapter* with its Dhan client's ``pace`` hook wired to the shared bucket.

    The rate ≤5 GUARANTEE (DEC-DHAN-RATE-REALTIME-GUARANTEE-001): the client's
    bounded fast-retry (a timeout/transport/5xx stall) is the ONE physical-POST
    path the per-window ``pace`` does not cover, so under a parallel timeout storm
    those retries could push the aggregate issue rate over 5 r/s. Injecting *pace*
    into the client (it acquires a token before each retry POST) closes that gap —
    with ``burst=1`` every physical POST is then bucket-bounded ⇒ ≤ ``current_rps``.

    A no-op (returns *adapter* unchanged) when there is nothing to wire: an
    unthrottled run (*pace* is ``None``), or any adapter that is not the live
    :class:`~brutex.adapters.dhan.adapter.DhanMarketAdapter` (e.g. the dry-run
    :class:`SyntheticDhanAdapter`). The adapter + client are frozen dataclasses, so
    this rebuilds via :func:`replace` rather than mutating (no shared-state
    surprise across workers). The import is local to avoid any import cycle and is
    paid once per pull, never on the hot path.
    """
    from brutex.adapters.dhan.adapter import DhanMarketAdapter

    if pace is None or not isinstance(adapter, DhanMarketAdapter):
        return adapter
    return replace(adapter, client=replace(adapter.client, pace=pace))


@dataclass(frozen=True, slots=True)
class PairFailure:
    """A ``(symbol, interval)`` pair that failed terminally (quarantined)."""

    symbol: str
    interval: str
    error: str


@dataclass(frozen=True, slots=True)
class DhanIngestReport:
    """Aggregate outcome of a :func:`run_dhan_spot_ingest` pass."""

    pairs: dict[tuple[str, str], IngestReport]
    failures: tuple[PairFailure, ...]

    @property
    def windows(self) -> int:
        """Total fetch windows across all successful pairs."""
        return sum(r.windows for r in self.pairs.values())

    @property
    def bars_appended(self) -> int:
        """Total bars appended across all successful pairs."""
        return sum(r.bars_appended for r in self.pairs.values())

    @property
    def transient_failures(self) -> int:
        """Total per-window transient skips across all successful pairs."""
        return sum(r.transient_failures for r in self.pairs.values())

    @property
    def skipped_windows(self) -> int:
        """Total windows skipped as already-complete across all pairs (DEC-FETCH-COVERAGE-001)."""
        return sum(r.skipped_windows for r in self.pairs.values())

    @property
    def retried_windows(self) -> int:
        """Total windows re-attempted by the in-run auto-retry (DEC-DHAN-WINDOW-RETRY-001)."""
        return sum(r.retried_windows for r in self.pairs.values())

    @property
    def still_failed_windows(self) -> int:
        """Total windows still failing after the retry budget — deferred to the next pull."""
        return sum(r.still_failed_windows for r in self.pairs.values())

    @property
    def failed_pairs(self) -> int:
        """Number of ``(symbol, interval)`` pairs quarantined on a terminal error."""
        return len(self.failures)


def run_dhan_spot_ingest(
    adapter: MarketAdapter,
    sink: BarSink,
    *,
    instruments: Iterable[Instrument],
    intervals: Sequence[str],
    start: date,
    end: date,
    pace: Pacer | None = None,
    coverage: FetchCoverage | None = None,
    status_log: WindowStatusLog | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_timeout: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
    on_progress: ProgressHook | None = None,
    run_parallel: RunParallel | None = None,
) -> DhanIngestReport:
    """Ingest every ``(instrument, interval)`` spot pair over ``[start, end)``.

    The OUTER loop — one job per ``(instrument, interval)`` pair — is mapped
    through *run_parallel* so many windows are in flight at once and the pull
    targets/saturates UP TO ~5 r/s when Dhan is healthy (Little's law:
    ``in_flight ≈ rate × latency``), governed by the shared rate bucket the
    *pace* hook drives (the aggregate stays ``≤ 5 r/s``, Rule #6). Each pair
    writes a DISTINCT lake partition, so the per-pair writes are path-isolated;
    the per-window :func:`ingest_spot` spine is unchanged. **Honest envelope
    (Golden Rule #10):** this targets ~5 r/s — it cannot guarantee a constant
    5 r/s (a 429 step-down + latency spikes lower the achieved rate).

    Parameters
    ----------
    adapter, sink
        The Dhan adapter (data source) and the lake write sink. Both are shared
        across workers; the spot sink writes path-isolated partitions and is
        already concurrency-safe (the live ``ParquetBarSink`` writes one file per
        ``symbol``×``interval``×month).
    instruments
        Spot instruments to pull (``symbol_name`` = engine symbol; raw id in ``security_id``).
    intervals
        Canonical intervals (``"1minute"`` / ``"1day"``); others are resampled.
    start, end
        Half-open date range, shared by every pair.
    pace
        Optional per-window pacing hook (rate-limit acquire), threaded into
        each :func:`ingest_spot` call. Under the fan-out this is the SHARED rate
        bucket every worker acquires from — the single governor that bounds the
        aggregate physical rate to ``≤ 5 r/s`` (Rule #6;
        :class:`~brutex.adapters.dhan.rate_control.DhanRateController` is now
        thread-safe, DEC-DHAN-PARALLEL-INGEST-001).
    coverage
        Optional :class:`~brutex.data.fetch_coverage.FetchCoverage` ledger
        threaded into every pair — a window already pulled is skipped before
        the vendor call, so a re-pull never re-burns the Dhan budget on data
        already in the lake (DEC-FETCH-COVERAGE-001). Now thread-safe under the
        shared fan-out.
    on_rate_limit, on_success
        Optional rate-controller feedback hooks threaded into every pair — a
        rate-limit 429 steps the rate down, a clean window recovers it
        (DEC-DHAN-ADAPTIVE-RPS-001).
    on_progress
        Optional live-progress hook fired once per ``(symbol, interval)`` pair
        after it is processed: ``(label, bars_appended, failed)``. A
        quarantined pair fires with ``bars=0, failed=True``. Fired on the
        CONTROLLER thread as each pair's result is collected (so the hook need
        not be thread-safe), in worker-completion order. Lets the dashboard
        stream real-time progress (DEC-PULL-PROGRESS-TRACKER-001).
    run_parallel
        The concurrency primitive mapping the per-pair fetcher over the worklist
        (a :class:`~brutex.orchestrator.universe_phases.RunParallel`). ``None``
        (the default) builds a bounded
        :class:`~brutex.orchestrator.parallel.PoolRunParallel`; callers size the
        pool to the rate budget via
        :func:`~brutex.orchestrator.parallel.workers_to_saturate` and inject it.
        Tests inject a synchronous fake to drive the fan-out with zero threads
        (mirrors :func:`brutex.orchestrator.universe_phases.run_two_phase`).

    Returns
    -------
    DhanIngestReport
        Per-pair :class:`IngestReport` for successes + the quarantined
        terminal failures. **Never raises** on a Dhan error — a terminal
        failure on one pair is recorded and the run continues. A non-Dhan
        exception (a genuine bug) is NOT swallowed — it surfaces unchanged.
    """
    pull_id = current_pull_id()
    # Rate ≤5 GUARANTEE (DEC-DHAN-RATE-REALTIME-GUARANTEE-001): wire the shared
    # bucket's *pace* into the adapter's Dhan client so a client fast-retry (a
    # timeout/5xx stall) re-acquires a token instead of bypassing the limiter —
    # closing the only un-gated physical-POST path. No-op for adapters without a
    # paceable client (dry-run / fakes) or when unthrottled (*pace* is None).
    adapter = _with_paced_client(adapter, pace)
    runner: RunParallel = run_parallel if run_parallel is not None else PoolRunParallel()

    # Deterministic worklist: one job per (instrument, interval) pair, in the
    # caller's instrument order × interval order. The OUTER fan-out unit.
    instruments = list(instruments)
    jobs: list[SpotJob] = [
        (instrument, interval) for instrument in instruments for interval in intervals
    ]

    def _run_pair(job: SpotJob) -> IngestReport:
        """Pull one ``(instrument, interval)`` pair — runs inside a worker thread.

        Re-binds *pull_id* into THIS thread's structlog context-vars: a worker
        thread does NOT inherit the controller thread's contextvar, so without
        this the ``ingest.*`` events emitted inside ``ingest_spot`` would lose
        their ``pull_id`` (regressing DEC-DHAN-INGEST-LOGGING-001). A raised
        :class:`DhanError` is captured by *run_parallel* into the job's
        :class:`ItemResult` and quarantined below; any other exception is
        captured too and re-raised on the controller thread (a real bug must
        surface, never be swallowed).
        """
        instrument, interval = job
        with pull_id_scope(pull_id):
            return ingest_spot(
                adapter,
                sink,
                instrument=instrument,
                interval=interval,
                start=start,
                end=end,
                pace=pace,
                coverage=coverage,
                status_log=status_log,
                on_rate_limit=on_rate_limit,
                on_timeout=on_timeout,
                on_success=on_success,
            )

    pairs: dict[tuple[str, str], IngestReport] = {}
    failures: list[PairFailure] = []
    # Drain the fan-out on the controller thread. on_progress + the quarantine
    # WARNING fire here (single-threaded), so neither the progress hook nor the
    # logger needs to be thread-safe. A captured non-DhanError surfaces.
    for outcome in runner(_run_pair, jobs):
        instrument, interval = outcome.item
        label = f"{instrument.symbol_name} {interval}"
        if outcome.ok:
            report = outcome.result
            if report is None:  # pragma: no cover — ok=True always carries a result
                continue
            pairs[instrument.symbol_name, interval] = report
            if on_progress is not None:
                on_progress(label, report.bars_appended, False)
            continue
        # A failed ItemResult always carries the captured worker exception.
        exc = outcome.exc
        if exc is None:  # pragma: no cover — a failed outcome always carries exc
            continue
        if not isinstance(exc, DhanError):
            # A genuine bug (e.g. ValueError) escaped a worker — preserve "never
            # swallow a real error": re-raise so the orchestrator never hides a
            # defect (only DhanError is quarantined; every other error surfaces).
            raise exc
        # Terminal Dhan error → isolate this pair, the run continues. The pair is
        # NOT marked complete, so it is re-tried on the next pull (never masked).
        failures.append(
            PairFailure(symbol=instrument.symbol_name, interval=interval, error=str(exc))
        )
        # KNOWN-GAP vs quarantine (DEC-DHAN-NO-DATA-KNOWN-GAP-001): a Dhan
        # ``DH-905`` "no data" 400 (e.g. NIFTY NEXT 50 daily, whose history starts
        # after 2021-01-01) is an EXPECTED data boundary, not a defect — log it as
        # an INFO ``ingest.known_gap`` so the operator's log is not flooded with
        # alarming quarantine WARNINGs for benign empty ranges. A genuine terminal
        # error (real 400 / 404 / auth) still logs the WARNING. EITHER way the pair
        # is isolated + re-tried next pull — data is never silently dropped.
        if is_no_data_error(exc):
            _LOG.info(
                "ingest.known_gap",
                pull_id=pull_id,
                symbol=instrument.symbol_name,
                interval=interval,
                reason=str(exc),
                note="Dhan reports no data for this range (DH-905); re-tried next pull, not masked",
            )
        else:
            # Quarantine-cause nook: WHY this pair was dropped (the bare failed=N
            # count otherwise hides the terminal Dhan error).
            _LOG.warning(
                "ingest.quarantine",
                pull_id=pull_id,
                symbol=instrument.symbol_name,
                interval=interval,
                error=str(exc),
            )
        if on_progress is not None:
            on_progress(label, 0, True)

    # Per-underlying rollup — INFO, one line per instrument (not per window) — in
    # a DETERMINISTIC post-pass grouped + sorted by symbol_name, so the log order
    # is reproducible regardless of worker completion order (Rule #4). The first
    # occurrence of each symbol in the worklist fixes its rollup order's tie-break
    # to the caller's instrument order via the stable sort key.
    failed_by_pair = {(f.symbol, f.interval) for f in failures}
    for instrument in sorted(instruments, key=lambda i: i.symbol_name):
        symbol = instrument.symbol_name
        completed = quarantined = inst_bars = inst_windows = 0
        inst_retried = inst_still_failed = 0
        for interval in intervals:
            # Every dispatched pair lands in exactly one of failed_by_pair / pairs
            # (a DhanError quarantines it, otherwise ingest_spot returned a
            # report — including a 0-bar "no data" report). A surviving report is
            # looked up directly; a missing key would be a genuine partition bug.
            if (symbol, interval) in failed_by_pair:
                quarantined += 1
                continue
            report = pairs[symbol, interval]
            completed += 1
            inst_bars += report.bars_appended
            inst_windows += report.windows
            inst_retried += report.retried_windows
            inst_still_failed += report.still_failed_windows
        _LOG.info(
            "ingest.underlying",
            pull_id=pull_id,
            symbol=symbol,
            series_completed=completed,
            series_failed=quarantined,
            bars_appended=inst_bars,
            windows=inst_windows,
            retried_windows=inst_retried,
            still_failed_windows=inst_still_failed,
        )
    return DhanIngestReport(pairs=pairs, failures=tuple(failures))


__all__ = [
    "DhanIngestReport",
    "PairFailure",
    "ProgressHook",
    "SpotJob",
    "run_dhan_spot_ingest",
]
