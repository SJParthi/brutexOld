"""Groww multi-instrument spot-ingest orchestrator (ADDITIVE mirror of Dhan's).

The Groww twin of :mod:`brutex.adapters.dhan.run_ingest` for the vendor
re-migration (PR-H). Drives :func:`~brutex.adapters.groww.ingest.ingest_spot`
across every ``(instrument, interval)`` pair resiliently: a **terminal** Groww
error on one pair is quarantined (recorded as failed) and the run continues to
the next pair — one bad symbol never kills the whole pull. Per-window transient
errors are already retried/skipped inside ``ingest_spot``; this layer adds the
multi-instrument fan-out + per-pair quarantine + an aggregate report.

**No caller is wired yet** — the CLI/dashboard keep calling the Dhan spine until
the cutover (PR-I). The adapter, sink and pacing hook are all injected, so this
is 100% unit-testable without a network or a real lake.

Parallel fan-out mirrors the Dhan spine: the OUTER loop — one job per
``(instrument, interval)`` pair — runs through an injected
:class:`~brutex.orchestrator.universe_phases.RunParallel` (production: a bounded
:class:`~brutex.orchestrator.parallel.PoolRunParallel`). Each pair writes a
DISTINCT lake partition, so the per-pair writes are path-isolated; the shared
rate bucket (the injected ``pace``) is the governor.

**Divergences from the Dhan spine (deliberate, Golden Rule #1):**

* **No auth fail-fast latch.** Dhan carries a shared ``AuthGuard`` + an
  ``auth_aborted`` / ``auth_cause`` report field for a mid-pull token death
  (DH-906). The Groww adapter provides no such latch, so a Groww auth failure is
  a plain TERMINAL per-pair quarantine — recorded + re-tried next pull, like any
  other terminal error. The report carries no auth-abort fields.
* **No known-gap branch.** Dhan special-cases a ``DH-905`` "no data" 400 as a
  benign KNOWN_GAP. Groww has no such marker (Golden Rule #1 — no invented vendor
  code), so every terminal Groww error is a uniform quarantine.
* **No ``status_log``.** Dropped (the unified ledger is the sole record surface).

**Honest envelope (Golden Rule #10):** this targets UP TO the rate ceiling — it
cannot guarantee a constant rate (back-off step-downs + latency spikes lower the
achieved rate). Groww's backtesting r/s is UNVERIFIED, so no rate is hardcoded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from brutex.adapters.ingest_common import RetryOutcome
    from brutex.orchestrator.audit import AuditWriter

from brutex.adapters.groww.errors import GrowwError, is_auth_failure
from brutex.adapters.groww.ingest import IngestReport, Pacer, ingest_spot
from brutex.adapters.protocol import MarketAdapter
from brutex.data.sink import BarSink
from brutex.data.window_ledger import WindowLedger
from brutex.models import Instrument
from brutex.observability.correlation import current_pull_id, pull_id_scope
from brutex.orchestrator.parallel import PoolRunParallel
from brutex.orchestrator.universe_phases import RunParallel

#: Structured logger — per-underlying spot rollup at INFO (``ingest.underlying``)
#: + the terminal-failure cause at WARNING (``ingest.quarantine``), both carrying
#: the pull's ``pull_id``. The per-window / per-series DEBUG detail is one layer
#: down in ``ingest_spot``.
_LOG = structlog.get_logger("brutex.adapters.groww.run_ingest")

#: Live-progress hook fired once per ``(symbol, interval)`` pair after it is
#: processed: ``(label, bars_appended, failed)``. ``bars_appended`` is 0 for a
#: quarantined pair. (No Groww ``gap`` flag — there is no benign no-data boundary.)
ProgressHook = Callable[[str, int, bool], None]

#: One unit of OUTER-loop work for the parallel fan-out: a spot
#: ``(instrument, interval)`` pair. Each maps to a distinct lake partition.
SpotJob = tuple[Instrument, str]

#: Default bounded post-run auto-retry-ROUND budget — the number of EXTRA full
#: re-pulls of ONLY the leftover failed windows after the original pull, when it
#: left a transient residual. Small + finite (Golden Rule #10).
_DEFAULT_MAX_RETRY_ROUNDS: int = 3


def _with_paced_client(adapter: MarketAdapter, pace: Pacer | None) -> MarketAdapter:
    """Return *adapter* with its Groww client's ``pace`` hook wired to the shared bucket.

    Closes the only physical-call path the per-window ``pace`` does not cover — the
    Groww client's bounded fast-retry of a transient timeout: injecting *pace* into
    the client (it acquires a token before each retry call) keeps every physical
    call bucket-bounded.

    A no-op (returns *adapter* unchanged) when there is nothing to wire: an
    unthrottled run (*pace* is ``None``), or any adapter that is not the live
    :class:`~brutex.adapters.groww.adapter.GrowwMarketAdapter`. The adapter +
    client are frozen dataclasses, so this rebuilds via :func:`replace` rather than
    mutating. The import is local to avoid an import cycle and is paid once per pull.
    """
    from brutex.adapters.groww.adapter import GrowwMarketAdapter

    if pace is None or not isinstance(adapter, GrowwMarketAdapter):
        return adapter
    return replace(adapter, client=replace(adapter.client, pace=pace))


@dataclass(frozen=True, slots=True)
class PairFailure:
    """A ``(symbol, interval)`` pair that failed terminally (quarantined)."""

    symbol: str
    interval: str
    error: str
    auth_failure: bool = False
    """Whether the terminal error was a Groww auth failure (a dead/invalid token).

    Captured from the live exception at quarantine time (Groww has no error-string
    no-data marker to re-derive it from, unlike Dhan), so the auto-retry wrapper
    can halt on a token death — retrying is futile until re-auth.
    """


@dataclass(frozen=True, slots=True)
class GrowwIngestReport:
    """Aggregate outcome of a :func:`run_groww_spot_ingest` pass."""

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
        """Total windows skipped as already-complete across all pairs."""
        return sum(r.skipped_windows for r in self.pairs.values())

    @property
    def retried_windows(self) -> int:
        """Total windows re-attempted by the in-run auto-retry."""
        return sum(r.retried_windows for r in self.pairs.values())

    @property
    def still_failed_windows(self) -> int:
        """Total windows still failing after the retry budget — deferred to the next pull."""
        return sum(r.still_failed_windows for r in self.pairs.values())

    @property
    def failed_pairs(self) -> int:
        """Number of ``(symbol, interval)`` pairs quarantined on a terminal error."""
        return len(self.failures)

    @property
    def auth_aborted(self) -> bool:
        """True iff any quarantined failure was a Groww auth failure.

        Groww has no fail-fast latch (the documented divergence), so this is a
        DERIVED read over the quarantined failures' captured ``auth_failure`` flag
        rather than a latch state. It lets the auto-retry wrapper halt the loop on
        a token death exactly as the Dhan path does — retrying is futile until re-auth.
        """
        return any(f.auth_failure for f in self.failures)


def _maybe_audit(
    audit: AuditWriter | None,
    action: str,
    subject: str,
    payload: dict[str, object],
) -> None:
    """Append one hash-chained audit line when an ``audit`` writer is wired.

    A no-op when ``audit is None``. Called ONLY from the single controller thread
    (the fan-out drain loop), so the not-thread-safe :class:`AuditWriter` is never
    touched concurrently (Golden Rule #8).
    """
    if audit is not None:
        audit.append(action=action, subject=subject, payload=payload)


def run_groww_spot_ingest(
    adapter: MarketAdapter,
    sink: BarSink,
    *,
    instruments: Iterable[Instrument],
    intervals: Sequence[str],
    start: date,
    end: date,
    pace: Pacer | None = None,
    ledger: WindowLedger | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_timeout: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
    on_progress: ProgressHook | None = None,
    run_parallel: RunParallel | None = None,
    audit: AuditWriter | None = None,
) -> GrowwIngestReport:
    """Ingest every ``(instrument, interval)`` spot pair over ``[start, end)``.

    The OUTER loop is mapped through *run_parallel* so many windows are in flight
    at once and the pull targets UP TO the rate ceiling when Groww is healthy,
    governed by the shared rate bucket the *pace* hook drives. Each pair writes a
    DISTINCT lake partition. **Never raises** on a Groww error — a terminal
    failure on one pair is recorded and the run continues. A non-Groww exception
    (a genuine bug) is NOT swallowed.

    Parameters
    ----------
    adapter, sink
        The Groww adapter (data source) and the lake write sink, shared across
        workers (the spot sink writes path-isolated partitions).
    instruments
        Spot instruments to pull.
    intervals
        Canonical intervals (``"1minute"`` / ``"1day"`` / tiered Groww intervals).
    start, end
        Half-open date range, shared by every pair.
    pace
        Optional per-window pacing hook, threaded into each :func:`ingest_spot`
        call AND wired into the adapter's Groww client. Groww's r/s is UNVERIFIED,
        so this injected bucket is the only rate governor.
    on_rate_limit, on_timeout, on_success
        Optional rate-controller feedback hooks threaded into every pair.
    on_progress
        Optional live-progress hook fired once per pair: ``(label, bars, failed)``.
        A quarantined pair fires with ``bars=0, failed=True``. Fired on the
        CONTROLLER thread (so it need not be thread-safe).
    run_parallel
        The concurrency primitive mapping the per-pair fetcher over the worklist.
        ``None`` builds a bounded
        :class:`~brutex.orchestrator.parallel.PoolRunParallel`. Tests inject a
        synchronous fake.

    Returns
    -------
    GrowwIngestReport
        Per-pair :class:`IngestReport` for successes + the quarantined failures.
    """
    pull_id = current_pull_id()
    # Wire the shared bucket's *pace* into the adapter's Groww client so a client
    # fast-retry re-acquires a token instead of bypassing the limiter. No-op for
    # adapters without a paceable client (fakes) or when unthrottled.
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

        Re-binds *pull_id* into THIS thread's structlog context-vars. A raised
        :class:`GrowwError` is captured by *run_parallel* and quarantined below;
        any other exception is captured too and re-raised on the controller thread.
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
                ledger=ledger,
                on_rate_limit=on_rate_limit,
                on_timeout=on_timeout,
                on_success=on_success,
            )

    pairs: dict[tuple[str, str], IngestReport] = {}
    failures: list[PairFailure] = []
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
    # WARNING fire here (single-threaded). A captured non-GrowwError surfaces.
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
        if not isinstance(exc, GrowwError):
            # A genuine bug (e.g. ValueError) escaped a worker — never swallow a
            # real error: re-raise so the orchestrator never hides a defect (only
            # GrowwError is quarantined; every other error surfaces).
            raise exc
        # Terminal Groww error → isolate this pair, the run continues. The pair is
        # NOT marked complete, so it is re-tried on the next pull (never masked).
        auth_failed = is_auth_failure(exc)
        failures.append(
            PairFailure(
                symbol=instrument.symbol_name,
                interval=interval,
                error=str(exc),
                auth_failure=auth_failed,
            )
        )
        _maybe_audit(
            audit,
            "INGEST_PAIR_FAILED",
            f"{instrument.symbol_name}:{interval}",
            {"error": str(exc), "auth_failure": auth_failed},
        )
        # Quarantine-cause nook: WHY this pair was dropped (the bare failed=N count
        # otherwise hides the terminal Groww error).
        _LOG.warning(
            "ingest.quarantine",
            pull_id=pull_id,
            symbol=instrument.symbol_name,
            interval=interval,
            error=str(exc),
        )
        if on_progress is not None:
            on_progress(label, 0, True)

    # Per-underlying rollup — INFO, one line per instrument — in a DETERMINISTIC
    # post-pass grouped + sorted by symbol_name, so the log order is reproducible
    # regardless of worker completion order (Rule #4).
    failed_by_pair = {(f.symbol, f.interval) for f in failures}
    for instrument in sorted(instruments, key=lambda i: i.symbol_name):
        symbol = instrument.symbol_name
        completed = quarantined = inst_bars = inst_windows = 0
        inst_retried = inst_still_failed = 0
        for interval in intervals:
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
        },
    )
    return GrowwIngestReport(pairs=pairs, failures=tuple(failures))


@dataclass(frozen=True, slots=True)
class SpotIngestWithRetry:
    """A pull's original report + its post-run auto-retry outcome.

    What :func:`run_groww_spot_ingest_with_retry` returns: the FIRST full-pull
    :class:`GrowwIngestReport` plus the
    :class:`~brutex.adapters.ingest_common.RetryOutcome` of the leftover-window
    recovery rounds (``None`` when no retry ran).
    """

    report: GrowwIngestReport
    retry: RetryOutcome | None

    @property
    def still_failed_windows(self) -> int:
        """Transient windows STILL failing after the auto-retry (or the original, if none ran).

        The retry's ``final_still_failed`` when a retry ran, else the original
        pull's ``still_failed_windows``. Zero ⇒ the lake is complete for this run.
        """
        if self.retry is not None:
            return self.retry.final_still_failed
        return self.report.still_failed_windows


def run_groww_spot_ingest_with_retry(
    adapter: MarketAdapter,
    sink: BarSink,
    *,
    instruments: Iterable[Instrument],
    intervals: Sequence[str],
    start: date,
    end: date,
    pace: Pacer | None = None,
    ledger: WindowLedger | None = None,
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
    """Run the spot pull, then auto-retry any leftover failed windows.

    Run one full :func:`run_groww_spot_ingest` pass, and — when it leaves
    TRANSIENT residual windows (``still_failed_windows`` > 0) AND *auto_retry* is
    on — re-run additional bounded rounds via the SHARED
    :func:`~brutex.adapters.ingest_common.retry_failed_windows` engine (CLAUDE.md
    §11 COMMON — that round-loop is vendor-neutral; it reads only
    ``still_failed_windows`` / ``auth_aborted`` / ``failures`` off the report, all
    of which :class:`GrowwIngestReport` exposes). Each round re-fetches ONLY the
    still-failed windows through the SAME *ledger* skip authority (incremental +
    idempotent) and the SAME *pace* bucket (rate-safe).

    **Honest envelope (Golden Rule #10):** the retry is O(failed_windows), NOT
    O(1)-total; the round count is bounded by *max_retry_rounds* (never infinite);
    every round is rate-safe; and a Groww auth death halts the loop (re-auth needed).

    Returns
    -------
    SpotIngestWithRetry
        The original :class:`GrowwIngestReport` plus the
        :class:`~brutex.adapters.ingest_common.RetryOutcome` (``None`` when no
        retry ran).
    """
    # Local import: the auto-retry engine is the shared, vendor-neutral round-loop
    # (CLAUDE.md §11 COMMON — one engine for both vendors); imported here, paid once
    # per pull, never on the hot path.
    from brutex.adapters.ingest_common import retry_failed_windows

    def _one_pass() -> GrowwIngestReport:
        """Run ONE full spot-ingest pass with the original call's bindings."""
        return run_groww_spot_ingest(
            adapter,
            sink,
            instruments=instruments,
            intervals=intervals,
            start=start,
            end=end,
            pace=pace,
            ledger=ledger,
            on_rate_limit=on_rate_limit,
            on_timeout=on_timeout,
            on_success=on_success,
            on_progress=on_progress,
            run_parallel=run_parallel,
            audit=audit,
        )

    report = _one_pass()
    # Only spend a retry round when auto-retry is ON and the original pull left a
    # TRANSIENT residual (a clean pull / one whose only failures were terminal
    # needs no re-pull — those are not retryable).
    if not auto_retry or report.still_failed_windows == 0:
        return SpotIngestWithRetry(report=report, retry=None)
    # No ``is_known_gap`` predicate: Groww has no no-data marker (Golden Rule #1 —
    # no invented vendor code), so the shared engine's default treats nothing as a
    # known gap (every Groww terminal failure is a uniform quarantine).
    retry = retry_failed_windows(
        _one_pass,
        max_rounds=max_retry_rounds,
        backoff=retry_backoff,
    )
    return SpotIngestWithRetry(report=report, retry=retry)


__all__ = [
    "GrowwIngestReport",
    "PairFailure",
    "ProgressHook",
    "SpotIngestWithRetry",
    "SpotJob",
    "run_groww_spot_ingest",
    "run_groww_spot_ingest_with_retry",
]
