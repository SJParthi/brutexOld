"""Dhan universe-ingest — the single top-level entry the cli/dashboard call.

Composes the two merged Dhan phases into one resilient pass (Step B of the
operator-sequenced Groww→Dhan flip; DEC-VENDOR-PIVOT-001):

1. **Refresh the instrument catalog** —
   :func:`~brutex.adapters.dhan.instrument_refresh.refresh_instruments`
   downloads the detailed scrip master, selects the brute-force universe
   (every index + F&O-eligible single-stock SPOT), and full-snapshot-upserts
   it into the store (lean lake — DEC-INSTRUMENT-UNIVERSE-FAST-001).
2. **Pull the universe's spot bars** —
   :func:`~brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest` fetches
   ``1minute`` + ``1day`` for every instrument the adapter holds, resiliently
   (a terminal failure on one pair is quarantined; the run continues).

Returns a combined :class:`UniverseIngestReport` (the refresh accounting + the
ingest outcome). The adapter, sink, store, ``http_get`` and pacing hook are
all injected, so this is 100% unit-testable without a network or a real lake;
``snapshot_date`` + ``run_id`` are caller-supplied (deterministic, no hidden
clock).

The derived-timeframe persist (1m → the 19 intraday TFs via
:func:`~brutex.data.spot_resample.derive_intraday_tfs`) is the next phase — it
needs the derived-TF lake layout decided, so it is a clearly-scoped follow-up
(honest envelope), not silently skipped.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from brutex.adapters.dhan.adapter import DhanMarketAdapter
from brutex.adapters.dhan.constants import INSTRUMENT_MASTER_DETAILED_URL
from brutex.adapters.dhan.ingest import Pacer
from brutex.adapters.dhan.instrument_loader import HttpGet
from brutex.adapters.dhan.instrument_refresh import (
    InstrumentSink,
    RefreshResult,
    refresh_instruments,
)
from brutex.adapters.dhan.run_ingest import DhanIngestReport, run_dhan_spot_ingest
from brutex.data.sink import BarSink


@dataclass(frozen=True, slots=True)
class UniverseIngestReport:
    """Combined outcome of a :func:`run_dhan_universe_ingest` pass."""

    refresh: RefreshResult
    ingest: DhanIngestReport


def run_dhan_universe_ingest(
    adapter: DhanMarketAdapter,
    sink: BarSink,
    store: InstrumentSink,
    http_get: HttpGet,
    *,
    snapshot_date: date,
    run_id: str,
    start: date,
    end: date,
    intervals: Sequence[str],
    pace: Pacer | None = None,
    instrument_url: str = INSTRUMENT_MASTER_DETAILED_URL,
) -> UniverseIngestReport:
    """Refresh the instrument catalog, then pull the universe's spot bars.

    Phase 1 refreshes the catalog (download → select universe → persist);
    phase 2 pulls ``intervals`` for every instrument the *adapter* holds,
    resiliently. **Never raises** on a Dhan ingest error — a terminal failure
    on one pair is recorded in the report and the run continues.

    Parameters
    ----------
    adapter, sink
        The Dhan adapter (its ``instruments`` are the pull universe) and the
        lake write sink.
    store, http_get
        The instrument store + the SSRF-guarded scrip-master fetcher.
    snapshot_date, run_id
        Caller-supplied, deterministic refresh lineage (no hidden clock).
    start, end
        Half-open date range for the spot pull.
    intervals
        Canonical intervals to fetch — typically ``("1minute", "1day")``;
        the 19 derived intraday TFs are resampled from 1m, never fetched.
    pace
        Optional per-window pacing hook (rate-limit acquire).
    instrument_url
        Scrip-master URL override (defaults to the verified Dhan URL).

    Returns
    -------
    UniverseIngestReport
        The refresh accounting + the resilient ingest outcome.
    """
    refresh = refresh_instruments(
        store,
        http_get,
        snapshot_date=snapshot_date,
        run_id=run_id,
        url=instrument_url,
    )
    ingest = run_dhan_spot_ingest(
        adapter,
        sink,
        instruments=adapter.instruments,
        intervals=intervals,
        start=start,
        end=end,
        pace=pace,
    )
    return UniverseIngestReport(refresh=refresh, ingest=ingest)


__all__ = [
    "UniverseIngestReport",
    "run_dhan_universe_ingest",
]
