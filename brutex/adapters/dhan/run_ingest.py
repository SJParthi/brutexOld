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
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from brutex.adapters.dhan.auto_retry import RetryOutcome
    from brutex.orchestrator.audit import AuditWriter

from brutex.adapters.dhan.auth_guard import AuthGuard
from brutex.adapters.dhan.errors import DhanError, is_auth_failure, is_no_data_error
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

#: Default bounded post-run auto-retry-ROUND budget (DEC-INGEST-AUTO-RETRY-001) —
#: the number of EXTRA full re-pulls of ONLY the leftover failed windows after the
#: original pull, when it left a transient residual. Small + finite (Golden Rule
#: #10 — never infinite). This is the ROUND layer that wraps the per-window
#: in-run retry (``_DEFAULT_MAX_RETRY_PASSES`` in ``ingest``): the in-run passes
#: self-heal a brief drop WITHIN a series; these rounds self-heal a residual a
#: multi-minute 429 storm left after the WHOLE pull.
_DEFAULT_MAX_RETRY_ROUNDS: int = 3


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
    auth_aborted: bool = False
    """True if the Dhan token died mid-pull (DEC-DHAN-AUTH-FAILFAST-RENEW-001).

    Set when the shared :class:`~brutex.adapters.dhan.auth_guard.AuthGuard` tripped
    — i.e. at least one pair failed with a dead-token error (a 401/403 OR a
    ``DH-906`` "Invalid Token" 400). Once tripped, the remaining not-yet-dispatched
    windows short-circuit to a no-op skip (ZERO further doomed calls); the affected
    pairs are STILL recorded in :attr:`failures` (re-tried next pull, never masked).
    ``False`` on a clean pull or one whose only failures were non-auth (per-pair
    terminal / no-data) errors.
    """

    auth_cause: str | None = None
    """The first dead-token cause that tripped the guard, or ``None`` if not tripped.

    The originating ``str(exc)`` of the FIRST auth failure observed (DEC-DHAN-AUTH-
    FAILFAST-RENEW-001) — carried on the single loud ``ingest.auth_aborted`` alert
    so the operator sees exactly why the pull aborted. ``None`` when
    :attr:`auth_aborted` is ``False``.
    """

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


def _maybe_audit(
    audit: AuditWriter | None,
    action: str,
    subject: str,
    payload: dict[str, object],
) -> None:
    """Append one hash-chained audit line when an ``audit`` writer is wired.

    A no-op when ``audit is None`` (the default — tests + callers that don't
    request an audit trail). Called ONLY from the single controller thread (the
    fan-out drain loop), so the not-thread-safe :class:`AuditWriter` is never
    touched concurrently (DEC-INGEST-AUDIT-CHAIN-001 / Golden Rule #8).
    """
    if audit is not None:
        audit.append(action=action, subject=subject, payload=payload)


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
    audit: AuditWriter | None = None,
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
    # ONE shared "token is dead" latch per run (DEC-DHAN-AUTH-FAILFAST-RENEW-001),
    # threaded into every ingest_spot pair. The first window to hit a dead-token
    # error trips it; every NOT-YET-DISPATCHED window then short-circuits to a no-op
    # skip (the doomed-call flood stops). The error still surfaces here so the pair
    # is recorded + the ONE loud auth-aborted alert fires. Honest envelope (Golden
    # Rule #10): the latch short-circuits NEW windows; it does NOT hard-cancel
    # threads already mid-call in the pool — those finish, then drain at the next
    # window-start check.
    auth_guard = AuthGuard()

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
                auth_guard=auth_guard,
            )

    pairs: dict[tuple[str, str], IngestReport] = {}
    failures: list[PairFailure] = []
    #: Whether the loud ``ingest.auth_aborted`` alert has fired — emitted EXACTLY
    #: ONCE (the first dead-token failure), then subsequent auth failures are
    #: recorded silently to avoid the 400-line per-symbol WARNING flood that the
    #: live DH-901 storm produced (DEC-DHAN-AUTH-FAILFAST-RENEW-001).
    auth_alerted = False
    # Golden Rule #8 audit trail (DEC-INGEST-AUDIT-CHAIN-001): one hash-chained
    # JSONL line per material ingest action, written ONLY here on the controller
    # thread (the AuditWriter is not thread-safe; the fan-out workers never touch
    # it). RUN_START records the worklist shape so a replay can reconstruct the
    # pull's scope.
    _maybe_audit(
        audit,
        "INGEST_RUN_START",
        f"pull:{pull_id}",
        {
            "instruments": len(instruments),
            "intervals": list(intervals),
            "jobs": len(jobs),
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    )
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
            _maybe_audit(
                audit,
                "INGEST_PAIR_DONE",
                f"{instrument.symbol_name}:{interval}",
                {
                    "bars_appended": report.bars_appended,
                    "windows": report.windows,
                    "still_failed_windows": report.still_failed_windows,
                },
            )
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
        _maybe_audit(
            audit,
            "INGEST_PAIR_FAILED",
            f"{instrument.symbol_name}:{interval}",
            {
                "error": str(exc),
                "auth_failure": is_auth_failure(exc),
                "known_gap": is_no_data_error(exc),
            },
        )
        # KNOWN-GAP vs quarantine (DEC-DHAN-NO-DATA-KNOWN-GAP-001): a Dhan
        # ``DH-905`` "no data" 400 (e.g. NIFTY NEXT 50 daily, whose history starts
        # after 2021-01-01) is an EXPECTED data boundary, not a defect — log it as
        # an INFO ``ingest.known_gap`` so the operator's log is not flooded with
        # alarming quarantine WARNINGs for benign empty ranges. A genuine terminal
        # error (real 400 / 404 / auth) still logs the WARNING. EITHER way the pair
        # is isolated + re-tried next pull — data is never silently dropped.
        if is_auth_failure(exc):
            # The token died (DEC-DHAN-AUTH-FAILFAST-RENEW-001). The pair is STILL
            # recorded above (re-tried next pull, never masked), but we DON'T emit a
            # per-symbol WARNING for each of the ~400 doomed symbols — that was the
            # 400-line flood. Ensure the latch is tripped (a window worker normally
            # trips it, but trip here too in case this exc reached us via another
            # path), then emit the ONE loud ``ingest.auth_aborted`` alert the FIRST
            # time only; later auth failures are recorded silently.
            auth_guard.trip(str(exc))
            if not auth_alerted:
                auth_alerted = True
                _LOG.warning(
                    "ingest.auth_aborted",
                    pull_id=pull_id,
                    symbol=instrument.symbol_name,
                    interval=interval,
                    cause=auth_guard.cause(),
                    note=(
                        "Dhan access token expired/invalid mid-pull — pull aborted; "
                        "regenerate the 24h token at web.dhan.co (or it will auto-renew "
                        "while active next run)"
                    ),
                )
        elif is_no_data_error(exc):
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
    _maybe_audit(
        audit,
        "INGEST_RUN_COMPLETE",
        f"pull:{pull_id}",
        {
            "pairs_ok": len(pairs),
            "pairs_failed": len(failures),
            "total_bars": sum(report.bars_appended for report in pairs.values()),
            "auth_aborted": auth_guard.tripped(),
        },
    )
    return DhanIngestReport(
        pairs=pairs,
        failures=tuple(failures),
        auth_aborted=auth_guard.tripped(),
        auth_cause=auth_guard.cause(),
    )


@dataclass(frozen=True, slots=True)
class SpotIngestWithRetry:
    """A pull's original report + its post-run auto-retry outcome (DEC-INGEST-AUTO-RETRY-001).

    What :func:`run_dhan_spot_ingest_with_retry` returns: the FIRST full-pull
    :class:`DhanIngestReport` (the original pass, exactly what the no-retry path
    produced) plus the :class:`~brutex.adapters.dhan.auto_retry.RetryOutcome` of
    the leftover-window recovery rounds (``None`` when no retry ran — the pull
    was already clean, or auto-retry was disabled).
    """

    report: DhanIngestReport
    retry: RetryOutcome | None

    @property
    def still_failed_windows(self) -> int:
        """Transient windows STILL failing after the auto-retry (or the original, if none ran).

        The final residual the operator cares about: the retry's
        ``final_still_failed`` when a retry ran, else the original pull's
        ``still_failed_windows``. Zero ⇒ the lake is complete for this run.
        """
        if self.retry is not None:
            return self.retry.final_still_failed
        return self.report.still_failed_windows


def run_dhan_spot_ingest_with_retry(
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
    auto_retry: bool = True,
    max_retry_rounds: int = _DEFAULT_MAX_RETRY_ROUNDS,
    retry_backoff: Callable[[int], None] | None = None,
    audit: AuditWriter | None = None,
) -> SpotIngestWithRetry:
    """Run the spot pull, then auto-retry any leftover failed windows (DEC-INGEST-AUTO-RETRY-001).

    The AUTOMATIC half of the manual⇄automatic retry duality: run one full
    :func:`run_dhan_spot_ingest` pass, and — when it leaves TRANSIENT residual
    windows (``still_failed_windows`` > 0, the 429-storm leftover) AND
    *auto_retry* is on — re-run additional bounded rounds via
    :func:`~brutex.adapters.dhan.auto_retry.retry_failed_windows`, each
    re-fetching ONLY the still-failed windows through the SAME *coverage* ledger
    (incremental + idempotent, DEC-FETCH-COVERAGE-001) and the SAME *pace* bucket
    (rate-safe, ``≤ 5 r/s``). The MANUAL ``brutex fetch --retry-failed`` path and
    the dashboard button call the SAME underlying
    :func:`~brutex.adapters.dhan.auto_retry.retry_failed_windows` (CLAUDE.md §11
    COMMON) — no duplicated round loop.

    The retry rounds re-use the original call's keyword bindings VERBATIM (same
    adapter / sink / instruments / intervals / range / coverage / pace / hooks),
    so a round is byte-for-byte the same ingest the operator's manual re-run does
    — except the coverage ledger now skips the windows the first pass completed,
    so only the leftover failed windows hit Dhan again.

    **Honest envelope (Golden Rule #10):** the retry is O(failed_windows), NOT
    O(1)-total; the round count is bounded by *max_retry_rounds* (never infinite);
    every round is rate-safe (the shared bucket caps it ``≤ 5 r/s``); a known-gap
    (``DH-905`` no-data) quarantine is NEVER retried; and a mid-pull token death
    halts the loop (RenewToken cannot revive a dead token).

    Parameters
    ----------
    adapter, sink, instruments, intervals, start, end, pace, coverage, status_log,
    on_rate_limit, on_timeout, on_success, on_progress, run_parallel
        Threaded VERBATIM into every :func:`run_dhan_spot_ingest` pass (the
        original + each retry round) — see that function's parameter docs. The
        *coverage* ledger is what makes a round re-fetch ONLY the failed windows.
    auto_retry
        When ``True`` (default) the leftover failed windows are auto-retried after
        the original pull; ``False`` (the ``--no-auto-retry`` / config opt-out)
        reproduces the prior single-pass behaviour exactly (``retry`` is ``None``).
    max_retry_rounds
        Bounded retry-round budget (default :data:`_DEFAULT_MAX_RETRY_ROUNDS`),
        passed through to :func:`~brutex.adapters.dhan.auto_retry.retry_failed_windows`.
    retry_backoff
        Injected ``(round_index) -> None`` inter-round sleep hook (``None`` → the
        bounded exponential default). Tests pass a no-op recorder (no real sleep).

    Returns
    -------
    SpotIngestWithRetry
        The original :class:`DhanIngestReport` plus the
        :class:`~brutex.adapters.dhan.auto_retry.RetryOutcome` (``None`` when no
        retry ran — auto-retry off, or the original pull was already clean).
    """
    # Local import: the auto-retry engine type-imports DhanIngestReport from THIS
    # module, so importing it at module top would be a cycle. The import is paid
    # once per pull, never on the hot path (CLAUDE.md §11 COMMON — one engine).
    from brutex.adapters.dhan.auto_retry import retry_failed_windows

    def _one_pass() -> DhanIngestReport:
        """Run ONE full spot-ingest pass with the original call's bindings."""
        return run_dhan_spot_ingest(
            adapter,
            sink,
            instruments=instruments,
            intervals=intervals,
            start=start,
            end=end,
            pace=pace,
            coverage=coverage,
            status_log=status_log,
            on_rate_limit=on_rate_limit,
            on_timeout=on_timeout,
            on_success=on_success,
            on_progress=on_progress,
            run_parallel=run_parallel,
            audit=audit,
        )

    report = _one_pass()
    # Only spend a retry round when auto-retry is ON and the original pull actually
    # left a TRANSIENT residual (a clean pull / a pull whose only failures were
    # known-gaps or terminal errors needs no re-pull — those are not retryable).
    if not auto_retry or report.still_failed_windows == 0:
        return SpotIngestWithRetry(report=report, retry=None)
    retry = retry_failed_windows(
        _one_pass,
        max_rounds=max_retry_rounds,
        backoff=retry_backoff,
    )
    return SpotIngestWithRetry(report=report, retry=retry)


__all__ = [
    "DhanIngestReport",
    "PairFailure",
    "ProgressHook",
    "SpotIngestWithRetry",
    "SpotJob",
    "run_dhan_spot_ingest",
    "run_dhan_spot_ingest_with_retry",
]
