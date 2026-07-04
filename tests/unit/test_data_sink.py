"""Unit tests for brutex.data.sink — BarSink protocol + implementations.

Exercises :class:`NullBarSink` and the partition-grouping logic in
:class:`ParquetBarSink` without requiring ``polars`` or ``duckdb`` —
we inject a tiny fake writer/catalog pair that records calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from brutex.data import (
    BarSink,
    CandleBar,
    CatalogEntry,
)
from brutex.data.parquet_writer import WriteResult
from brutex.data.sink import NullBarSink, ParquetBarSink

# ────────────────────────────────────────────────────────────────────────
# Fake writer + catalog (no polars, no duckdb)
# ────────────────────────────────────────────────────────────────────────


@dataclass
class FakeWriter:
    """Stands in for :class:`ParquetWriter` — records calls, returns
    a deterministic :class:`WriteResult` without touching the FS."""

    lake_root: Path
    calls: list[dict[str, Any]] = field(default_factory=list)

    def write(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: list[CandleBar],
        merge_mode: str = "keep-first",
    ) -> WriteResult:
        self.calls.append(
            {
                "exchange": exchange,
                "segment": segment,
                "symbol": symbol,
                "interval": interval,
                "count": len(bars),
                "merge_mode": merge_mode,
            }
        )
        first = bars[0].timestamp
        last = bars[-1].timestamp
        return WriteResult(
            path=self.lake_root / f"{symbol}-{first.year}-{first.month:02d}.parquet",
            row_count=len(bars),
            raw_hash="f" * 64,
            first_ts=first,
            last_ts=last,
            bytes_written=100,
            bars_added=len(bars),  # fresh-write fake: every incoming bar is added (deduped count)
        )

    def make_catalog_entry(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        result: WriteResult,
    ) -> CatalogEntry:
        return CatalogEntry(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            year=result.first_ts.year,
            month=result.first_ts.month,
            raw_hash=result.raw_hash,
            row_count=result.row_count,
            first_ts=result.first_ts,
            last_ts=result.last_ts,
            fetched_at=datetime.now(UTC),
        )


@dataclass
class FakeCatalog:
    """Stand-in for :class:`Catalog` — records upsert calls."""

    upserts: list[CatalogEntry] = field(default_factory=list)

    def upsert(self, entry: CatalogEntry) -> None:
        self.upserts.append(entry)


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _bar(year: int, month: int, day: int = 1, minute: int = 15) -> CandleBar:
    return CandleBar(
        timestamp=datetime(year, month, day, 9, minute, 0, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=1000,
        open_interest=None,
    )


# ────────────────────────────────────────────────────────────────────────
# NullBarSink
# ────────────────────────────────────────────────────────────────────────


class TestNullBarSink:
    def test_satisfies_bar_sink_protocol(self) -> None:
        sink = NullBarSink()
        assert isinstance(sink, BarSink)

    def test_append_discards_but_reports_count(self) -> None:
        sink = NullBarSink()
        bars = [_bar(2024, 1, 25, 15), _bar(2024, 1, 25, 16)]
        result = sink.append(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )
        assert result.bars_appended == 2
        assert result.partitions_written == 0
        assert result.write_results == ()


# ────────────────────────────────────────────────────────────────────────
# ParquetBarSink — partition grouping
# ────────────────────────────────────────────────────────────────────────


class TestParquetBarSinkGrouping:
    def test_empty_batch_no_ops(self, tmp_path: Path) -> None:
        writer = FakeWriter(lake_root=tmp_path)
        catalog = FakeCatalog()
        sink = ParquetBarSink(writer=writer, catalog=catalog)  # type: ignore[arg-type]

        result = sink.append(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[],
        )
        assert result.bars_appended == 0
        assert result.partitions_written == 0
        assert writer.calls == []
        assert catalog.upserts == []

    def test_single_partition_writes_once(self, tmp_path: Path) -> None:
        writer = FakeWriter(lake_root=tmp_path)
        catalog = FakeCatalog()
        sink = ParquetBarSink(writer=writer, catalog=catalog)  # type: ignore[arg-type]

        bars = [_bar(2024, 1, 10), _bar(2024, 1, 25)]
        result = sink.append(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )
        assert result.bars_appended == 2
        assert result.partitions_written == 1
        assert len(writer.calls) == 1
        assert writer.calls[0]["count"] == 2
        assert writer.calls[0]["merge_mode"] == "keep-first"  # default keep-first preserved
        assert len(catalog.upserts) == 1
        assert catalog.upserts[0].year == 2024
        assert catalog.upserts[0].month == 1

    def test_latest_wins_merge_mode_is_forwarded(self, tmp_path: Path) -> None:
        """``merge_mode="latest-wins"`` is forwarded verbatim to every writer.write call (FIX-2)."""
        writer = FakeWriter(lake_root=tmp_path)
        sink = ParquetBarSink(writer=writer)  # type: ignore[arg-type]

        sink.append(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1day",
            bars=[_bar(2024, 1, 10), _bar(2024, 2, 5)],  # two partitions
            merge_mode="latest-wins",
        )
        assert [c["merge_mode"] for c in writer.calls] == ["latest-wins", "latest-wins"]

    def test_multi_partition_splits(self, tmp_path: Path) -> None:
        writer = FakeWriter(lake_root=tmp_path)
        catalog = FakeCatalog()
        sink = ParquetBarSink(writer=writer, catalog=catalog)  # type: ignore[arg-type]

        bars = [
            _bar(2024, 1, 10),
            _bar(2024, 1, 25),
            _bar(2024, 2, 5),
            _bar(2024, 3, 1),
        ]
        result = sink.append(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )
        assert result.bars_appended == 4
        assert result.partitions_written == 3
        assert [c["count"] for c in writer.calls] == [2, 1, 1]
        assert len(catalog.upserts) == 3
        months = [e.month for e in catalog.upserts]
        assert months == [1, 2, 3]

    def test_no_catalog_still_writes(self, tmp_path: Path) -> None:
        """Catalog is optional — sink must still work without one."""
        writer = FakeWriter(lake_root=tmp_path)
        sink = ParquetBarSink(writer=writer, catalog=None)  # type: ignore[arg-type]

        bars = [_bar(2024, 1, 10)]
        result = sink.append(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )
        assert result.partitions_written == 1
        assert len(writer.calls) == 1


class TestParquetBarSinkProperties:
    """The ``writer`` / ``catalog`` accessors return the wired collaborators."""

    def test_writer_property_returns_wired_writer(self, tmp_path: Path) -> None:
        writer = FakeWriter(lake_root=tmp_path)
        sink = ParquetBarSink(writer=writer, catalog=None)  # type: ignore[arg-type]
        assert sink.writer is writer

    def test_catalog_property_returns_wired_catalog(self, tmp_path: Path) -> None:
        writer = FakeWriter(lake_root=tmp_path)
        catalog = FakeCatalog()
        sink = ParquetBarSink(writer=writer, catalog=catalog)  # type: ignore[arg-type]
        assert sink.catalog is catalog

    def test_catalog_property_is_none_when_unwired(self, tmp_path: Path) -> None:
        sink = ParquetBarSink(writer=FakeWriter(lake_root=tmp_path), catalog=None)  # type: ignore[arg-type]
        assert sink.catalog is None


# ────────────────────────────────────────────────────────────────────────
# Real-writer dedup count (DEC-SINK-DEDUP-COUNT-001)
# ────────────────────────────────────────────────────────────────────────


class TestParquetBarSinkDedupCount:
    """``SinkAppendResult.bars_net_new`` sums the per-partition DEDUPED ``bars_added`` —
    a re-pull of bars already in the lake reports 0 net-new, while ``bars_appended`` (the
    historical input-count contract that ``> 0`` gates depend on) stays the input size."""

    def test_fresh_append_net_new_equals_input(self, tmp_path: Path) -> None:
        import pytest

        pytest.importorskip("polars")
        from brutex.data.parquet_writer import ParquetWriter

        sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path), catalog=None)
        result = sink.append(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(2024, 1, 10), _bar(2024, 2, 5)],  # two partitions, both fresh
        )
        assert result.bars_appended == 2  # input count (the unchanged gate contract)
        assert result.bars_net_new == 2  # fresh → every bar is net-new
        assert result.partitions_written == 2

    def test_full_repull_net_new_is_zero_but_appended_stays_input(self, tmp_path: Path) -> None:
        import pytest

        pytest.importorskip("polars")
        from brutex.data.parquet_writer import ParquetWriter

        sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path), catalog=None)
        bars = [_bar(2024, 1, 10), _bar(2024, 2, 5)]
        sink.append(
            exchange="NSE", segment="CASH", symbol="NSE-NIFTY", interval="1minute", bars=bars
        )
        # Re-pull the SAME bars — every one already stored → net-new is 0, the honest count.
        second = sink.append(
            exchange="NSE", segment="CASH", symbol="NSE-NIFTY", interval="1minute", bars=bars
        )
        assert second.bars_net_new == 0  # the honest deduped count
        assert second.bars_appended == 2  # the input-count gate contract is UNCHANGED
        assert second.partitions_written == 2  # both partitions were still touched (merge read)
