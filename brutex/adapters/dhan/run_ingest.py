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

Idempotent: ``ingest_spot``'s sink append is rerun-safe, so re-running a pull
over the same range is a no-op on already-stored bars (CLAUDE.md §2 Rule 7).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date

import structlog

from brutex.adapters.dhan.errors import DhanError
from brutex.adapters.dhan.ingest import IngestReport, Pacer, ingest_spot
from brutex.adapters.protocol import MarketAdapter
from brutex.data.fetch_coverage import FetchCoverage
from brutex.data.sink import BarSink
from brutex.models import Instrument
from brutex.observability.correlation import current_pull_id

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
    on_rate_limit: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
    on_progress: ProgressHook | None = None,
) -> DhanIngestReport:
    """Ingest every ``(instrument, interval)`` spot pair over ``[start, end)``.

    Parameters
    ----------
    adapter, sink
        The Dhan adapter (data source) and the lake write sink.
    instruments
        Spot instruments to pull (``symbol_name`` = engine symbol; raw id in ``security_id``).
    intervals
        Canonical intervals (``"1minute"`` / ``"1day"``); others are resampled.
    start, end
        Half-open date range, shared by every pair.
    pace
        Optional per-window pacing hook (rate-limit acquire), threaded into
        each :func:`ingest_spot` call.
    coverage
        Optional :class:`~brutex.data.fetch_coverage.FetchCoverage` ledger
        threaded into every pair — a window already pulled is skipped before
        the vendor call, so a re-pull never re-burns the Dhan budget on data
        already in the lake (DEC-FETCH-COVERAGE-001).
    on_rate_limit, on_success
        Optional rate-controller feedback hooks threaded into every pair — a
        rate-limit 429 steps the rate down, a clean window recovers it
        (DEC-DHAN-ADAPTIVE-RPS-001).
    on_progress
        Optional live-progress hook fired once per ``(symbol, interval)`` pair
        after it is processed: ``(label, bars_appended, failed)``. A
        quarantined pair fires with ``bars=0, failed=True``. Lets the dashboard
        stream real-time progress (DEC-PULL-PROGRESS-TRACKER-001).

    Returns
    -------
    DhanIngestReport
        Per-pair :class:`IngestReport` for successes + the quarantined
        terminal failures. **Never raises** on a Dhan error — a terminal
        failure on one pair is recorded and the run continues.
    """
    pull_id = current_pull_id()
    pairs: dict[tuple[str, str], IngestReport] = {}
    failures: list[PairFailure] = []
    for instrument in instruments:
        # Per-instrument accumulators for the INFO rollup below (the engine
        # symbol is the spot "underlying"; its intervals are its series).
        completed = quarantined = inst_bars = inst_windows = 0
        for interval in intervals:
            label = f"{instrument.symbol_name} {interval}"
            try:
                report = ingest_spot(
                    adapter,
                    sink,
                    instrument=instrument,
                    interval=interval,
                    start=start,
                    end=end,
                    pace=pace,
                    coverage=coverage,
                    on_rate_limit=on_rate_limit,
                    on_success=on_success,
                )
            except DhanError as exc:
                failures.append(
                    PairFailure(symbol=instrument.symbol_name, interval=interval, error=str(exc))
                )
                quarantined += 1
                # Quarantine-cause nook: WHY this pair was dropped (the bare
                # failed=N count otherwise hides the terminal Dhan error).
                _LOG.warning(
                    "ingest.quarantine",
                    pull_id=pull_id,
                    symbol=instrument.symbol_name,
                    interval=interval,
                    error=str(exc),
                )
                if on_progress is not None:
                    on_progress(label, 0, True)
                continue
            pairs[instrument.symbol_name, interval] = report
            completed += 1
            inst_bars += report.bars_appended
            inst_windows += report.windows
            if on_progress is not None:
                on_progress(label, report.bars_appended, False)
        # Per-underlying rollup — INFO, one line per instrument (not per window),
        # so a bulk pull's progress is visible without the DEBUG firehose.
        _LOG.info(
            "ingest.underlying",
            pull_id=pull_id,
            symbol=instrument.symbol_name,
            series_completed=completed,
            series_failed=quarantined,
            bars_appended=inst_bars,
            windows=inst_windows,
        )
    return DhanIngestReport(pairs=pairs, failures=tuple(failures))


__all__ = [
    "DhanIngestReport",
    "PairFailure",
    "ProgressHook",
    "run_dhan_spot_ingest",
]
