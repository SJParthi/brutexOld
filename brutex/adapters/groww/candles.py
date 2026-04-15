"""Phase D runner — fetch historical candles for every contract.

Implements ``PhaseDriverProtocol`` for :attr:`IngestPhase.D_CANDLES`.

## The chunker

Groww's ``get_historical_candles`` has per-request time limits
per-interval (see ``docs/groww-sdk/backtesting.md §6``):

==================  =======================
Interval            Max duration per request
==================  =======================
1m / 2m / 3m / 5m   30 days
10m / 15m / 30m     90 days
1h / 4h / 1d / 1w   180 days
==================  =======================

Phase D splits each contract's requested date range into chunks
bounded by the relevant max-duration. Each chunk becomes one work
item and is fetched independently, with its own dedup + retry.

## Flow

```
PhaseDDriver.run()
  1. Enumerate one work item per (contract, interval, chunk).
     For each (exchange, segment, groww_symbol, interval) tuple:
       chunks = chunk_date_range(start, end, max_duration_for(interval))
       for (chunk_start, chunk_end) in chunks:
         yield WorkItem(...)
  2. Each item calls adapter.pull_historical_candles(...).
     Response: {"candles": [[ts, o, h, l, c, v, oi], ...], ...}
  3. On SUCCESS, the raw candle rows are collected into the
     per-contract result bucket so the caller can persist them
     to the canonical data lake (Parquet/DuckDB — later phase).
  4. PhaseRunner drives retries + completeness verification.
```

## Input

The driver takes a list of :class:`CandleRequest` objects — each
specifies one (contract, interval, range) tuple. Typically:

    requests = [
        CandleRequest(
            exchange="NSE",
            segment="FNO",
            groww_symbol=symbol,
            interval="1minute",
            start=date(2020, 1, 1),
            end=date(2020, 12, 31),
        )
        for symbol in phase_c.iter_all_contracts()
    ]

## Persistence

Phase D always collects the raw candle arrays in memory (for legacy
callers that read via :meth:`PhaseDDriver.collected_candles`) and,
when a :class:`~brutex.data.sink.BarSink` is supplied, *also* parses
them into canonical :class:`~brutex.data.models.CandleBar` objects
and hands them to the sink. The production sink is
:class:`~brutex.data.sink.ParquetBarSink`, which writes one Parquet
file per ``(year, month)`` partition and upserts a catalog row via
the DuckDB :class:`~brutex.data.catalog.Catalog`.

When no sink is supplied the driver behaves exactly as it did before
this commit — raw candles stay in memory and nothing is written to
disk. That keeps every existing unit test green and makes the data
lake strictly opt-in for P1a callers.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from brutex.adapters.groww.client import GrowwAdapter
from brutex.data.parser import parse_groww_candle_rows
from brutex.data.sink import BarSink, SinkAppendResult
from brutex.models import BruteXError
from brutex.orchestrator.audit import AuditWriter
from brutex.orchestrator.pipeline import (
    EventSubscriber,
    PhaseResult,
    PhaseRunner,
)
from brutex.orchestrator.state import IngestPhase, WorkItem, WorkItemState
from brutex.orchestrator.store import WorkItemStore

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Groww interval constants (see docs/groww-sdk/backtesting.md §6)
# ────────────────────────────────────────────────────────────────────────

#: Maximum duration per request, in days, keyed by the Groww interval
#: string as emitted by the SDK.
MAX_DURATION_DAYS: Final[dict[str, int]] = {
    "1minute": 30,
    "2minute": 30,
    "3minute": 30,
    "5minute": 30,
    "10minute": 90,
    "15minute": 90,
    "30minute": 90,
    "1hour": 180,
    "4hour": 180,
    "1day": 180,
    "1week": 180,
    "1month": 180,
}


# ────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────


class CandleChunkerError(BruteXError):
    """Raised when :func:`chunk_date_range` receives an unsupported
    interval or an invalid date range."""


# ────────────────────────────────────────────────────────────────────────
# Chunker
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CandleChunk:
    """One (start, end) window for a ``get_historical_candles`` call."""

    start: date
    end: date

    def start_time(self) -> str:
        """Inclusive start in Groww's ``yyyy-MM-dd HH:mm:ss`` format."""
        return f"{self.start.isoformat()} 09:15:00"

    def end_time(self) -> str:
        """Inclusive end at session close."""
        return f"{self.end.isoformat()} 15:30:00"


def chunk_date_range(
    *,
    start: date,
    end: date,
    interval: str,
) -> list[CandleChunk]:
    """Split ``[start, end]`` into chunks bounded by the interval's limit.

    Parameters
    ----------
    start
        Inclusive start date.
    end
        Inclusive end date.
    interval
        Groww interval string (e.g. ``"1minute"``, ``"1day"``). Must
        be a key of :data:`MAX_DURATION_DAYS`.

    Returns
    -------
    list[CandleChunk]
        At least one chunk, each bounded by ``max_duration_days``.

    Raises
    ------
    CandleChunkerError
        If the interval is unknown or ``end < start``.
    """
    if interval not in MAX_DURATION_DAYS:
        raise CandleChunkerError(
            f"Unknown interval {interval!r}. Known: {sorted(MAX_DURATION_DAYS.keys())}"
        )
    if end < start:
        raise CandleChunkerError(f"end ({end}) must be >= start ({start})")

    max_days = MAX_DURATION_DAYS[interval]
    step = timedelta(days=max_days - 1)  # inclusive → -1

    chunks: list[CandleChunk] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + step, end)
        chunks.append(CandleChunk(start=cursor, end=chunk_end))
        # Advance by one day past chunk_end so the next chunk is
        # strictly after the previous one.
        cursor = chunk_end + timedelta(days=1)
    return chunks


# ────────────────────────────────────────────────────────────────────────
# Candle request dataclass
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CandleRequest:
    """One (contract, interval, range) tuple — the input to Phase D."""

    exchange: str
    segment: str
    groww_symbol: str
    interval: str
    start: date
    end: date


# ────────────────────────────────────────────────────────────────────────
# Result dataclass
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CandlesResult:
    """Rich summary of a Phase D run."""

    phase_result: PhaseResult
    total_requests: int  # distinct CandleRequest inputs
    total_chunks: int  # total work items after chunking
    total_candle_rows: int  # sum of rows across all chunks
    contracts_with_data: int
    contracts_empty: int
    per_contract_row_counts: dict[str, int]
    persisted_at: datetime
    bars_persisted: int = 0  # total bars handed to the sink (0 if no sink wired)
    partitions_written: int = 0  # total (year, month) partitions touched


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _content_hash(
    exchange: str,
    segment: str,
    groww_symbol: str,
    interval: str,
    chunk_start: date,
    chunk_end: date,
) -> str:
    """Compute the deterministic dedup hash for a Phase D candle chunk."""
    content = (
        f"get_historical_candles:{exchange}:{segment}:{groww_symbol}"
        f":{interval}:{chunk_start.isoformat()}:{chunk_end.isoformat()}"
    )
    return hashlib.sha256(content.encode()).hexdigest()


def _make_item(
    *,
    request: CandleRequest,
    chunk: CandleChunk,
) -> WorkItem:
    """Build a Phase D :class:`WorkItem` from one (request, chunk) pair."""
    return WorkItem.new(
        phase=IngestPhase.D_CANDLES,
        content_hash=_content_hash(
            request.exchange,
            request.segment,
            request.groww_symbol,
            request.interval,
            chunk.start,
            chunk.end,
        ),
        params={
            "method": "get_historical_candles",
            "exchange": request.exchange,
            "segment": request.segment,
            "groww_symbol": request.groww_symbol,
            "interval": request.interval,
            "chunk_start": chunk.start.isoformat(),
            "chunk_end": chunk.end.isoformat(),
        },
    )


# ────────────────────────────────────────────────────────────────────────
# PhaseDDriver
# ────────────────────────────────────────────────────────────────────────


class PhaseDDriver:
    """Phase D — fetch historical candles for every (contract, interval, range).

    Parameters
    ----------
    adapter
        Pre-built :class:`GrowwAdapter`.
    store
        Shared :class:`WorkItemStore`.
    audit
        Shared :class:`AuditWriter`.
    run_id
        Current ingest run UUID.
    requests
        Sequence of :class:`CandleRequest` — typically derived from
        Phase C's iter_all_contracts.
    subscribers
        Pipeline event subscribers.
    bar_sink
        Optional :class:`~brutex.data.sink.BarSink`. When supplied,
        every SUCCESS chunk has its raw candle rows parsed into
        canonical :class:`~brutex.data.models.CandleBar` objects and
        handed to the sink. When ``None`` (the default) nothing is
        persisted — legacy callers keep reading via
        :meth:`collected_candles`.
    """

    phase: IngestPhase = IngestPhase.D_CANDLES

    def __init__(
        self,
        *,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
        run_id: str,
        requests: Sequence[CandleRequest],
        subscribers: Sequence[EventSubscriber] = (),
        bar_sink: BarSink | None = None,
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._audit = audit
        self._run_id = run_id
        self._requests = list(requests)
        self._subscribers = list(subscribers)
        self._bar_sink = bar_sink

        # Populated during run()
        self._collected_candles: dict[str, list[list[Any]]] = defaultdict(list)
        self._last_result: CandlesResult | None = None
        self._bars_persisted: int = 0
        self._partitions_written: int = 0

    # ── PhaseDriverProtocol ────────────────────────────────────────

    def enumerate_items(self) -> Iterable[WorkItem]:
        """Yield one work item per (request, chunk) pair."""
        for request in self._requests:
            chunks = chunk_date_range(
                start=request.start,
                end=request.end,
                interval=request.interval,
            )
            for chunk in chunks:
                yield _make_item(request=request, chunk=chunk)

    def run(self) -> PhaseResult:
        """Drive Phase D to completion."""
        items = list(self.enumerate_items())
        total_chunks = len(items)

        def invoke(work_item: WorkItem) -> tuple[WorkItem, Any]:
            """Drive a single candle work item through the adapter (Phase D dispatch hook)."""
            params = dict(work_item.params)
            exchange = params["exchange"]
            segment = params["segment"]
            groww_symbol = params["groww_symbol"]
            interval = params["interval"]
            chunk_start_iso = params["chunk_start"]
            chunk_end_iso = params["chunk_end"]
            chunk_start = date.fromisoformat(chunk_start_iso)
            chunk_end = date.fromisoformat(chunk_end_iso)

            new_item, response = self._adapter.pull_historical_candles(
                work_item=work_item,
                exchange=exchange,
                segment=segment,
                groww_symbol=groww_symbol,
                start_time=f"{chunk_start.isoformat()} 09:15:00",
                end_time=f"{chunk_end.isoformat()} 15:30:00",
                candle_interval=interval,
            )

            if new_item.state == WorkItemState.SUCCESS and isinstance(response, dict):
                candles = response.get("candles", [])
                if isinstance(candles, list):
                    self._collected_candles[groww_symbol].extend(candles)
                    if self._bar_sink is not None and candles:
                        sink_result = self._persist_chunk(
                            exchange=exchange,
                            segment=segment,
                            groww_symbol=groww_symbol,
                            interval=interval,
                            raw_candles=candles,
                        )
                        self._bars_persisted += sink_result.bars_appended
                        self._partitions_written += sink_result.partitions_written

            return new_item, response

        runner = PhaseRunner(
            phase=IngestPhase.D_CANDLES,
            store=self._store,
            audit=self._audit,
            invoke=invoke,
            subscribers=self._subscribers,
            run_id=self._run_id,
        )
        phase_result = runner.run(items)

        # Stats
        per_contract_row_counts = {
            symbol: len(rows) for symbol, rows in self._collected_candles.items()
        }
        total_candle_rows = sum(per_contract_row_counts.values())
        contracts_with_data = sum(1 for n in per_contract_row_counts.values() if n > 0)
        # All requested contracts minus ones that had data = empty
        requested_symbols = {req.groww_symbol for req in self._requests}
        contracts_empty = len(requested_symbols) - contracts_with_data

        result = CandlesResult(
            phase_result=phase_result,
            total_requests=len(self._requests),
            total_chunks=total_chunks,
            total_candle_rows=total_candle_rows,
            contracts_with_data=contracts_with_data,
            contracts_empty=contracts_empty,
            per_contract_row_counts=per_contract_row_counts,
            persisted_at=datetime.now(UTC),
            bars_persisted=self._bars_persisted,
            partitions_written=self._partitions_written,
        )
        self._last_result = result

        self._audit.append(
            action="CANDLES_SUMMARY",
            subject=f"phase:D:run:{self._run_id}",
            payload={
                "run_id": self._run_id,
                "total_requests": len(self._requests),
                "total_chunks": total_chunks,
                "total_candle_rows": total_candle_rows,
                "contracts_with_data": contracts_with_data,
                "contracts_empty": contracts_empty,
                "bars_persisted": self._bars_persisted,
                "partitions_written": self._partitions_written,
            },
        )

        return phase_result

    # ── Sink wiring ───────────────────────────────────────────────

    def _persist_chunk(
        self,
        *,
        exchange: str,
        segment: str,
        groww_symbol: str,
        interval: str,
        raw_candles: list[Any],
    ) -> SinkAppendResult:
        """Parse *raw_candles* and append them to the configured sink.

        Precondition: ``self._bar_sink is not None``.

        A parse failure is a schema mismatch against Groww's documented
        row layout — we let :class:`~brutex.data.parser.CandleRowError`
        propagate so the phase halts loudly rather than silently
        corrupting the lake.
        """
        if self._bar_sink is None:  # pragma: no cover — guarded by caller
            raise RuntimeError("_persist_chunk called with no bar_sink configured")
        bars = parse_groww_candle_rows(raw_candles)
        return self._bar_sink.append(
            exchange=exchange,
            segment=segment,
            symbol=groww_symbol,
            interval=interval,
            bars=bars,
        )

    # ── Accessors ──────────────────────────────────────────────────

    @property
    def last_result(self) -> CandlesResult | None:
        """Return the most recent :class:`CandlesResult`, or None before run()."""
        return self._last_result

    @property
    def collected_candles(self) -> dict[str, list[list[Any]]]:
        """Raw candle rows per ``groww_symbol``.

        Each row is the flat [ts, open, high, low, close, volume, oi]
        array as returned by Groww's SDK. See
        ``docs/groww-sdk/backtesting.md §5`` for the canonical layout.
        """
        return {k: list(v) for k, v in self._collected_candles.items()}


__all__ = [
    "MAX_DURATION_DAYS",
    "CandleChunk",
    "CandleChunkerError",
    "CandleRequest",
    "CandlesResult",
    "PhaseDDriver",
    "chunk_date_range",
]
