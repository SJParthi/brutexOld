"""Sustained-load stress: the resilient fetch loop + watermark at scale.

Drives `run_ingest` over the 3 brute-force indices × all 12 Groww timeframes
across a multi-year span — hundreds of chunks through the full
parse → OHLC-guard → sink → watermark pipe (synthetic fetcher, no network).

Invariants under load:

1. Every chunk settles — ``failed == 0`` (no chunk left un-handled).
2. ``completed > 0`` (the run actually did work).
3. **Resume at scale** — re-running with the same watermark settles everything
   as ``skipped`` (``second.completed == 0`` and ``second.skipped ==
   first.completed``): the crash-safe idempotence guarantee under volume.

Budget
------
* CI: ``BRUTEX_STRESS_YEARS`` defaults to ``2`` (~hundreds of chunks, < 2 s).
* Local soak: ``BRUTEX_STRESS_YEARS=10`` (or more) via the same env var.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from brutex.adapters.groww.chunks import ALL_GROWW_INTERVALS
from brutex.adapters.groww.ingest import (
    BRUTE_FORCE_INDEX_SYMBOLS,
    SyntheticCandleFetcher,
    run_ingest,
)
from brutex.adapters.groww.persist import PersistingFetcher
from brutex.data.sink import NullBarSink
from brutex.data.watermark import FetchWatermark

_STRESS_YEARS = int(os.environ.get("BRUTEX_STRESS_YEARS", "2"))


def test_sustained_ingest_settles_and_resumes() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=365 * _STRESS_YEARS)
    watermark = FetchWatermark()
    fetcher = PersistingFetcher(inner=SyntheticCandleFetcher(rows_per_chunk=3), sink=NullBarSink())

    first = run_ingest(
        fetcher, BRUTE_FORCE_INDEX_SYMBOLS, ALL_GROWW_INTERVALS, start, end, watermark
    )
    assert first.failed == 0  # every chunk handled, none lost
    assert first.completed > 0  # real work happened
    assert first.skipped == 0  # fresh watermark → nothing pre-settled

    # Re-run with the SAME watermark — everything is already settled.
    second = run_ingest(
        fetcher, BRUTE_FORCE_INDEX_SYMBOLS, ALL_GROWW_INTERVALS, start, end, watermark
    )
    assert second.completed == 0
    assert second.failed == 0
    assert second.skipped == first.completed  # idempotent resume at scale
