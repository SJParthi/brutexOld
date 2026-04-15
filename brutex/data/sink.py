"""Bar sink protocol + Parquet-backed implementation.

A :class:`BarSink` is the write-side interface between the Phase D
candle runner and the canonical data lake. The driver calls
:meth:`BarSink.append` with a symbol, interval, and a list of
normalised :class:`CandleBar` objects; the sink is responsible for
persisting them to the configured storage layer and updating the
catalog.

Two implementations ship in this module:

* :class:`NullBarSink` — discards input. Used as the default when
  no data lake is wired up (P1a tests, unit-test fixtures, dry-run
  CLI). Makes the ``bar_sink`` parameter in :class:`PhaseDDriver`
  a no-op-friendly injection point.

* :class:`ParquetBarSink` — the real sink. Groups incoming bars by
  ``(year, month)`` partition and delegates each group to
  :class:`ParquetWriter.write`. Upserts the resulting
  :class:`CatalogEntry` into the DuckDB catalog.

``polars`` / ``duckdb`` are only imported when
:class:`ParquetBarSink` actually writes — the module import itself
has no heavy deps.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar, CatalogEntry
from brutex.data.parquet_writer import ParquetWriter, WriteResult
from brutex.data.partition import partition_for_timestamp


@dataclass(frozen=True, slots=True)
class SinkAppendResult:
    """Outcome of one :meth:`BarSink.append` call.

    Attributes
    ----------
    bars_appended
        Total number of bars accepted by the sink.
    partitions_written
        Number of distinct ``(year, month)`` partition files touched.
    write_results
        Per-partition :class:`WriteResult` tuples, in write order.
        Empty for sinks that don't actually write (e.g.
        :class:`NullBarSink`).
    """

    bars_appended: int
    partitions_written: int
    write_results: tuple[WriteResult, ...]


@runtime_checkable
class BarSink(Protocol):
    """Write-side interface for canonical OHLCV bars."""

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
    ) -> SinkAppendResult:
        """Persist *bars* for the given partition coordinates."""
        ...


# ────────────────────────────────────────────────────────────────────────
# NullBarSink — default no-op sink
# ────────────────────────────────────────────────────────────────────────


class NullBarSink:
    """A :class:`BarSink` that discards everything.

    Used as the default when the Phase D driver is constructed without
    an explicit sink. Keeps the driver interface uniform — every
    SUCCESS path calls ``sink.append(...)`` whether or not the caller
    cares about persistence.
    """

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval
        return SinkAppendResult(
            bars_appended=len(bars),
            partitions_written=0,
            write_results=(),
        )


# ────────────────────────────────────────────────────────────────────────
# ParquetBarSink — production sink
# ────────────────────────────────────────────────────────────────────────


class ParquetBarSink:
    """Writes bars to canonical Parquet + upserts into the DuckDB catalog.

    The sink groups each incoming ``append`` batch by
    ``(year, month)`` before delegating to
    :meth:`ParquetWriter.write`, so one call can safely contain bars
    spanning multiple months — e.g. a ``1minute`` chunk that straddles
    a month boundary.

    Parameters
    ----------
    writer
        Pre-built :class:`ParquetWriter`. Typically one per run.
    catalog
        Optional :class:`Catalog` reference. If supplied, every
        successful write produces a :class:`CatalogEntry` upsert.
        If ``None``, the sink writes Parquet files but does not
        maintain the DuckDB catalog — useful for tests or for users
        who only need raw files.
    """

    def __init__(
        self, *, writer: ParquetWriter, catalog: Catalog | None = None
    ) -> None:
        self._writer = writer
        self._catalog = catalog

    @property
    def writer(self) -> ParquetWriter:
        return self._writer

    @property
    def catalog(self) -> Catalog | None:
        return self._catalog

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
    ) -> SinkAppendResult:
        """Group *bars* by partition and write each group.

        Bars spanning more than one ``(year, month)`` partition are
        handled transparently — each group becomes exactly one
        :meth:`ParquetWriter.write` call.
        """
        if not bars:
            return SinkAppendResult(
                bars_appended=0,
                partitions_written=0,
                write_results=(),
            )

        # Group by (year, month)
        groups: dict[tuple[int, int], list[CandleBar]] = defaultdict(list)
        for bar in bars:
            key = partition_for_timestamp(bar.timestamp)
            groups[key].append(bar)

        results: list[WriteResult] = []
        for (_year, _month), group in sorted(groups.items()):
            write_result = self._writer.write(
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                interval=interval,
                bars=group,
            )
            results.append(write_result)

            if self._catalog is not None:
                entry = self._writer.make_catalog_entry(
                    exchange=exchange,
                    segment=segment,
                    symbol=symbol,
                    interval=interval,
                    result=write_result,
                )
                self._catalog.upsert(entry)

        return SinkAppendResult(
            bars_appended=len(bars),
            partitions_written=len(results),
            write_results=tuple(results),
        )


__all__ = [
    "BarSink",
    "NullBarSink",
    "ParquetBarSink",
    "SinkAppendResult",
]
