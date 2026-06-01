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

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from brutex.adapters.dhan.adapter import DhanMarketAdapter
from brutex.adapters.dhan.errors import DhanError
from brutex.adapters.dhan.ingest import IngestReport, Pacer, ingest_spot
from brutex.data.sink import BarSink
from brutex.models import Instrument


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
    def failed_pairs(self) -> int:
        """Number of ``(symbol, interval)`` pairs quarantined on a terminal error."""
        return len(self.failures)


def run_dhan_spot_ingest(
    adapter: DhanMarketAdapter,
    sink: BarSink,
    *,
    instruments: Iterable[Instrument],
    intervals: Sequence[str],
    start: date,
    end: date,
    pace: Pacer | None = None,
) -> DhanIngestReport:
    """Ingest every ``(instrument, interval)`` spot pair over ``[start, end)``.

    Parameters
    ----------
    adapter, sink
        The Dhan adapter (data source) and the lake write sink.
    instruments
        Spot instruments to pull (``vendor_symbol`` = Dhan ``SECURITY_ID``).
    intervals
        Canonical intervals (``"1minute"`` / ``"1day"``); others are resampled.
    start, end
        Half-open date range, shared by every pair.
    pace
        Optional per-window pacing hook (rate-limit acquire), threaded into
        each :func:`ingest_spot` call.

    Returns
    -------
    DhanIngestReport
        Per-pair :class:`IngestReport` for successes + the quarantined
        terminal failures. **Never raises** on a Dhan error — a terminal
        failure on one pair is recorded and the run continues.
    """
    pairs: dict[tuple[str, str], IngestReport] = {}
    failures: list[PairFailure] = []
    for instrument in instruments:
        for interval in intervals:
            try:
                pairs[instrument.vendor_symbol, interval] = ingest_spot(
                    adapter,
                    sink,
                    instrument=instrument,
                    interval=interval,
                    start=start,
                    end=end,
                    pace=pace,
                )
            except DhanError as exc:
                failures.append(
                    PairFailure(symbol=instrument.vendor_symbol, interval=interval, error=str(exc))
                )
    return DhanIngestReport(pairs=pairs, failures=tuple(failures))


__all__ = [
    "DhanIngestReport",
    "PairFailure",
    "run_dhan_spot_ingest",
]
