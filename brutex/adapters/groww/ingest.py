"""End-to-end ingest orchestration — wire the spine for a set of symbols.

This is §13.9 step 3: tie the pieces together so ``brutex fetch`` can pull
candles for the 3 brute-force indices (NIFTY / BANKNIFTY / SENSEX) — and,
once the expiries/contracts surfaces land, the whole F&O universe — through
the proven spine:

    plan_chunks → run_fetch(PersistingFetcher(GrowwCandleFetcher), watermark)

:func:`run_ingest` is the testable core: for each ``(symbol, interval)`` it
plans the chunk windows, drives :func:`~brutex.adapters.groww.fetch_loop.run_fetch`,
and aggregates the per-key outcomes into an :class:`IngestReport`. It's pure
orchestration — the fetcher, watermark, sleep and backoff are all injected —
so it unit-tests with a fake fetcher (no SDK, no network, no real sleep).

:class:`SyntheticCandleFetcher` powers ``brutex fetch --dry-run``: it emits
valid synthetic candle rows so the FULL pipeline (parse → OHLC-guard → Parquet
sink → watermark) runs offline and produces real logs + sample lake data for
verification, with no Groww credentials.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

from brutex.adapters.groww.chunks import plan_chunks
from brutex.adapters.groww.fetch_loop import CandleFetcher, FetchOutcome, run_fetch
from brutex.data.watermark import ChunkKey, FetchWatermark
from brutex.orchestrator.retry import backoff_delay

#: The 3 brute-force indices — the §13.9 "indices first" milestone universe.
BRUTE_FORCE_INDEX_SYMBOLS: tuple[str, ...] = ("NSE-NIFTY", "NSE-BANKNIFTY", "BSE-SENSEX")

_LOG = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestReport:
    """Aggregate outcome of a :func:`run_ingest` pass."""

    outcomes: dict[tuple[str, str], FetchOutcome]

    @property
    def completed(self) -> int:
        """Total chunks that fetched + persisted successfully across all keys."""
        return sum(o.completed for o in self.outcomes.values())

    @property
    def no_data(self) -> int:
        """Total chunks the vendor reported as empty (NO_DATA) across all keys."""
        return sum(o.no_data for o in self.outcomes.values())

    @property
    def failed(self) -> int:
        """Total chunks that failed terminally (quarantined) across all keys."""
        return sum(o.failed for o in self.outcomes.values())

    @property
    def skipped(self) -> int:
        """Total chunks skipped as already-settled (resume) across all keys."""
        return sum(o.skipped for o in self.outcomes.values())


def run_ingest(
    fetcher: CandleFetcher,
    symbols: Iterable[str],
    intervals: Iterable[str],
    start: datetime,
    end: datetime,
    watermark: FetchWatermark,
    *,
    window_days: int | None = None,
    reauth: Callable[[], None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    delay_fn: Callable[[int], float] = backoff_delay,
) -> IngestReport:
    """Fetch every ``(symbol, interval)`` over ``[start, end)`` resiliently.

    Plans chunk windows per pair, drives the resilient loop, and aggregates the
    outcomes. Idempotent + resumable: re-running with the same *watermark* skips
    already-settled chunks. Never raises on a known Groww error (the loop
    quarantines and moves on).
    """
    outcomes: dict[tuple[str, str], FetchOutcome] = {}
    for symbol in symbols:
        for interval in intervals:
            keys = plan_chunks(symbol, interval, start, end, window_days=window_days)
            _LOG.info("ingest.pair.start", symbol=symbol, interval=interval, chunks=len(keys))
            outcome = run_fetch(
                fetcher,
                keys,
                watermark,
                reauth=reauth,
                sleep_fn=sleep_fn,
                delay_fn=delay_fn,
            )
            outcomes[symbol, interval] = outcome
            _LOG.info(
                "ingest.pair.done",
                symbol=symbol,
                interval=interval,
                completed=outcome.completed,
                no_data=outcome.no_data,
                failed=outcome.failed,
                skipped=outcome.skipped,
            )
    return IngestReport(outcomes=outcomes)


@dataclass(slots=True)
class SyntheticCandleFetcher:
    """A :class:`CandleFetcher` that emits valid synthetic rows (``--dry-run``).

    Generates ``rows_per_chunk`` 1-minute candle rows starting at each chunk's
    ``start``, so the real parse → sink pipeline runs offline and writes sample
    lake data. No SDK, no network — the dry-run proof that the pipe works.
    """

    rows_per_chunk: int = 5
    base_price: float = 100.0

    def fetch_chunk(self, key: ChunkKey) -> Sequence[object]:
        """Return ``rows_per_chunk`` valid ``[ts, o, h, l, c, vol, oi]`` rows."""
        start = datetime.fromisoformat(key.start)
        rows: list[list[object]] = []
        for i in range(self.rows_per_chunk):
            ts = (start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
            open_px = self.base_price + i
            rows.append([ts, open_px, open_px + 1.0, open_px - 1.0, open_px + 0.5, 1000 + i, None])
        return rows


__all__ = [
    "BRUTE_FORCE_INDEX_SYMBOLS",
    "IngestReport",
    "SyntheticCandleFetcher",
    "run_ingest",
]
