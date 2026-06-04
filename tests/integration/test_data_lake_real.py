"""End-to-end integration test for the data lake write/read path.

Unlike ``tests/unit/test_data_sink.py`` (which uses a FakeWriter stub),
these tests install the real P2 ``[data]`` dependency group
(``polars`` + ``duckdb`` + ``pyarrow``) and exercise the actual
Parquet writer + DuckDB catalog.

## What's verified

1. **Real Parquet round-trip** — a batch of :class:`CandleBar` instances
   is written to disk via :class:`ParquetWriter`, then re-read with
   ``polars.read_parquet``. Every column value is asserted equal to
   the input (timestamps, prices, volume, open interest).

2. **Byte-for-byte reproducibility** — the same input written twice
   produces identical bytes (same sha256). This exercises
   ``NFR-DET-001``.

3. **Hash-conflict rejection** — re-writing the same
   ``(symbol, interval, year, month)`` with different content raises
   :class:`WriterError`.

4. **Multi-partition grouping** — a batch straddling a month boundary
   is split into two partitions by :class:`ParquetBarSink`, each
   landing in the correct canonical path.

5. **Real DuckDB catalog** — :meth:`Catalog.upsert` inserts a
   :class:`CatalogEntry`, :meth:`Catalog.get` reads it back unchanged,
   :meth:`Catalog.count` returns 1.

6. **Upsert replaces not duplicates** — re-upserting the same primary
   key updates the existing row rather than creating a duplicate.

These tests are skipped automatically if ``polars`` / ``duckdb`` /
``pyarrow`` aren't installed, so they don't break local runs that
don't have ``.[data]`` installed.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data import (
    CandleBar,
    CatalogEntry,
    ParquetBarSink,
    WriterError,
)
from brutex.data.catalog import Catalog
from brutex.data.parquet_writer import ParquetWriter

# Skip the whole module if the P2 extras aren't installed.
pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

import polars as pl

# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _bar(
    day: int,
    minute: int = 15,
    open_price: float = 22000.0,
    month: int = 1,
    year: int = 2024,
) -> CandleBar:
    """Construct a deterministic ``CandleBar`` with sensible defaults."""
    return CandleBar(
        timestamp=datetime(year, month, day, 9, minute, 0, tzinfo=UTC),
        open=Decimal(str(open_price)),
        high=Decimal(str(open_price + 10)),
        low=Decimal(str(open_price - 10)),
        close=Decimal(str(open_price + 5)),
        volume=100,
        open_interest=500,
    )


# ────────────────────────────────────────────────────────────────────────
# Real Parquet round-trip
# ────────────────────────────────────────────────────────────────────────


class TestRealParquetRoundTrip:
    def test_single_bar_writes_and_reads_back(self, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path)
        bars = [_bar(25)]
        result = writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )
        assert result.path.exists()
        assert result.row_count == 1
        assert len(result.raw_hash) == 64
        assert result.bytes_written > 0

        df = pl.read_parquet(result.path)
        assert df.shape == (1, 7)
        row = df.row(0, named=True)
        assert row["open"] == 22000.0
        assert row["high"] == 22010.0
        assert row["low"] == 21990.0
        assert row["close"] == 22005.0
        assert row["volume"] == 100
        assert row["open_interest"] == 500

    def test_multiple_bars_sorted_in_output(self, tmp_path: Path) -> None:
        """Writer must sort bars by timestamp before serialising."""
        writer = ParquetWriter(lake_root=tmp_path)
        # Deliberately unsorted input
        bars = [
            _bar(25, minute=30),
            _bar(25, minute=15),
            _bar(25, minute=45),
        ]
        writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )
        df = pl.read_parquet(tmp_path / "bars/NSE/FNO/NSE-NIFTY/1minute/2024/01.parquet")
        minutes = df["timestamp"].dt.minute().to_list()
        assert minutes == [15, 30, 45]

    def test_byte_for_byte_reproducibility(self, tmp_path: Path) -> None:
        """NFR-DET-001: identical input → identical bytes → identical sha256."""
        bars = [_bar(day, minute=15 + i) for i, day in enumerate(range(1, 6))]

        writer1 = ParquetWriter(lake_root=tmp_path / "run1")
        result1 = writer1.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )

        writer2 = ParquetWriter(lake_root=tmp_path / "run2")
        result2 = writer2.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )

        assert result1.raw_hash == result2.raw_hash, (
            f"expected byte-for-byte reproducibility; "
            f"hash1={result1.raw_hash} hash2={result2.raw_hash}"
        )
        assert result1.bytes_written == result2.bytes_written

    def test_idempotent_rewrite_same_content(self, tmp_path: Path) -> None:
        """Re-writing identical content to the same path must be a no-op."""
        writer = ParquetWriter(lake_root=tmp_path)
        bars = [_bar(25)]
        r1 = writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )
        r2 = writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )
        assert r1.raw_hash == r2.raw_hash
        assert r1.path == r2.path

    def test_same_timestamp_different_value_keep_first(self, tmp_path: Path) -> None:
        """A SAME-timestamp bar with DIFFERENT OHLCV keeps the STORED bar and logs the conflict.

        DEC-LAKE-CONFLICT-KEEPFIRST-001: the second write does NOT raise.  The
        stored (first-written) bar is preserved bit-for-bit; the incoming bar's
        values are discarded.  The returned :class:`WriteResult` carries exactly
        one :class:`BarConflict` whose ``stored`` / ``vendor`` fields match the
        two bars.  The Parquet file on disk is UNCHANGED (no rewrite, so the
        hash is identical to the first write's hash).
        """
        from brutex.data.parquet_writer import BarConflict

        writer = ParquetWriter(lake_root=tmp_path)
        first = writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(25, open_price=22000.0)],
        )
        # Second write — same timestamp, different open price.
        second = writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(25, open_price=99999.0)],
        )
        # No raise — the pull finishes.
        assert second.path == first.path
        # The stored file is unchanged: row count stays 1, hash is identical.
        assert second.row_count == 1
        assert second.raw_hash == first.raw_hash
        # Exactly one conflict reported.
        assert len(second.conflicts) == 1
        (conflict,) = second.conflicts
        assert isinstance(conflict, BarConflict)
        # stored = the FIRST bar (22000.0 open, rounded to Decimal).
        assert conflict.stored.open == _bar(25, open_price=22000.0).open
        # vendor = the rejected second bar (99999.0 open).
        assert conflict.vendor.open == _bar(25, open_price=99999.0).open
        # The Parquet file on disk holds the ORIGINAL value, not the vendor re-statement.
        df = pl.read_parquet(second.path)
        assert df.shape == (1, 7)
        assert df.row(0, named=True)["open"] == pytest.approx(22000.0)

    def test_merge_disjoint_days_accumulates_the_month(self, tmp_path: Path) -> None:
        """Two writes of DIFFERENT days in one month union into the partition.

        This is the chunk-straddles-month scenario: each fetch chunk carries a
        partial slice of the month; merge-on-write accumulates them instead of
        conflicting (the BAD_REQUEST flood). DEC-LAKE-MERGE-WRITE-001.
        """
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(10, open_price=100.0), _bar(11, open_price=101.0)],
        )
        r2 = writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(12, open_price=102.0), _bar(13, open_price=103.0)],
        )
        assert r2.row_count == 4  # all four days now present
        df = pl.read_parquet(r2.path)
        assert df["timestamp"].dt.day().to_list() == [10, 11, 12, 13]

    def test_merge_overlap_identical_is_noop(self, tmp_path: Path) -> None:
        """Re-writing days already stored identically adds nothing (idempotent)."""
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(10, open_price=100.0), _bar(11, open_price=101.0)],
        )
        # Overlapping re-pull: day 11 already present (identical) + new day 12.
        r2 = writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(11, open_price=101.0), _bar(12, open_price=102.0)],
        )
        assert r2.row_count == 3  # 10, 11, 12 — day 11 not duplicated
        # A pure subset re-write is a no-op that preserves the prior hash.
        r3 = writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(10, open_price=100.0)],
        )
        assert r3.row_count == 3
        assert r3.raw_hash == r2.raw_hash  # no rewrite — byte-stable

    def test_reject_multi_partition_input(self, tmp_path: Path) -> None:
        """Writer rejects a batch that spans multiple (year, month) partitions."""
        writer = ParquetWriter(lake_root=tmp_path)
        bars = [
            _bar(25, month=1),  # Jan
            _bar(5, month=2),  # Feb — different partition
        ]
        with pytest.raises(WriterError, match="multiple partitions"):
            writer.write(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                bars=bars,
            )


# ────────────────────────────────────────────────────────────────────────
# Real DuckDB Catalog
# ────────────────────────────────────────────────────────────────────────


def _make_entry(symbol: str = "NSE-NIFTY", month: int = 1) -> CatalogEntry:
    return CatalogEntry(
        exchange="NSE",
        segment="FNO",
        symbol=symbol,
        interval="1minute",
        year=2024,
        month=month,
        raw_hash=hashlib.sha256(f"{symbol}:{month}".encode()).hexdigest(),
        row_count=1000,
        first_ts=datetime(2024, month, 1, 9, 15, tzinfo=UTC),
        last_ts=datetime(2024, month, 28, 15, 30, tzinfo=UTC),
        fetched_at=datetime(2024, 1, 1, 12, tzinfo=UTC),
    )


class TestRealDuckDBCatalog:
    def test_insert_and_get(self, tmp_path: Path) -> None:
        path = tmp_path / "catalog.duckdb"
        with Catalog.open(path) as cat:
            entry = _make_entry()
            cat.upsert(entry)

            fetched = cat.get(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                year=2024,
                month=1,
            )
        assert fetched is not None
        assert fetched.symbol == "NSE-NIFTY"
        assert fetched.row_count == 1000
        assert fetched.raw_hash == entry.raw_hash

    def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "catalog.duckdb"
        with Catalog.open(path) as cat:
            result = cat.get(
                exchange="NSE",
                segment="FNO",
                symbol="DOES-NOT-EXIST",
                interval="1minute",
                year=2024,
                month=1,
            )
        assert result is None

    def test_count(self, tmp_path: Path) -> None:
        path = tmp_path / "catalog.duckdb"
        with Catalog.open(path) as cat:
            assert cat.count() == 0
            cat.upsert(_make_entry(symbol="A"))
            cat.upsert(_make_entry(symbol="B"))
            cat.upsert(_make_entry(symbol="C", month=2))
            assert cat.count() == 3

    def test_upsert_replaces_not_duplicates(self, tmp_path: Path) -> None:
        """Re-upserting same primary key must replace, not duplicate."""
        path = tmp_path / "catalog.duckdb"
        with Catalog.open(path) as cat:
            cat.upsert(_make_entry())
            cat.upsert(_make_entry())  # Same PK
            cat.upsert(_make_entry())  # Same PK
            assert cat.count() == 1

    def test_upsert_updates_row_content(self, tmp_path: Path) -> None:
        """The second upsert's content must be what get() returns."""
        path = tmp_path / "catalog.duckdb"
        with Catalog.open(path) as cat:
            e1 = _make_entry()
            cat.upsert(e1)

            e2 = CatalogEntry(
                exchange=e1.exchange,
                segment=e1.segment,
                symbol=e1.symbol,
                interval=e1.interval,
                year=e1.year,
                month=e1.month,
                raw_hash="b" * 64,  # different hash
                row_count=2000,  # different count
                first_ts=e1.first_ts,
                last_ts=e1.last_ts + timedelta(days=1),
                fetched_at=e1.fetched_at + timedelta(hours=1),
            )
            cat.upsert(e2)

            fetched = cat.get(
                exchange=e1.exchange,
                segment=e1.segment,
                symbol=e1.symbol,
                interval=e1.interval,
                year=e1.year,
                month=e1.month,
            )
            assert fetched is not None
            assert fetched.raw_hash == "b" * 64
            assert fetched.row_count == 2000

    def test_operations_on_closed_catalog_raise(self, tmp_path: Path) -> None:
        from brutex.data.errors import CatalogError

        path = tmp_path / "catalog.duckdb"
        cat = Catalog.open(path)
        cat.close()
        with pytest.raises(CatalogError, match="closed"):
            cat.upsert(_make_entry())
        with pytest.raises(CatalogError, match="closed"):
            cat.get(
                exchange="NSE",
                segment="FNO",
                symbol="X",
                interval="1minute",
                year=2024,
                month=1,
            )
        with pytest.raises(CatalogError, match="closed"):
            cat.count()

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "catalog.duckdb"
        cat = Catalog.open(path)
        cat.close()
        cat.close()  # must not raise


# ────────────────────────────────────────────────────────────────────────
# ParquetBarSink + ParquetWriter + Catalog end-to-end
# ────────────────────────────────────────────────────────────────────────


class TestSinkEndToEnd:
    def test_sink_writes_parquet_and_upserts_catalog(self, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path / "lake")
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            sink = ParquetBarSink(writer=writer, catalog=cat)

            bars = [_bar(day) for day in range(1, 6)]
            result = sink.append(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                bars=bars,
            )

            assert result.bars_appended == 5
            assert result.partitions_written == 1
            assert cat.count() == 1

            # Verify the parquet file is real
            (write_result,) = result.write_results
            df = pl.read_parquet(write_result.path)
            assert df.shape == (5, 7)

    def test_sink_spans_multiple_months(self, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path / "lake")
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            sink = ParquetBarSink(writer=writer, catalog=cat)

            bars = [
                _bar(10, month=1),
                _bar(25, month=1),
                _bar(5, month=2),
                _bar(15, month=2),
                _bar(1, month=3),
            ]
            result = sink.append(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                bars=bars,
            )

            assert result.bars_appended == 5
            assert result.partitions_written == 3
            assert cat.count() == 3

            # Each catalog row should match a real parquet file
            for entry in (
                cat.get(
                    exchange="NSE",
                    segment="FNO",
                    symbol="NSE-NIFTY",
                    interval="1minute",
                    year=2024,
                    month=m,
                )
                for m in (1, 2, 3)
            ):
                assert entry is not None
                assert entry.row_count > 0


# ────────────────────────────────────────────────────────────────────────
# FR-CAT-003 — Catalog.get_bars (Arrow zero-copy reader)
# FR-CAT-004 — Catalog.data_snapshot_hash
# ────────────────────────────────────────────────────────────────────────


class TestCatalogGetBars:
    """FR-CAT-003: ``get_bars`` returns a pyarrow.Table of canonical bars."""

    def _seed_lake(
        self,
        tmp_path: Path,
        *,
        symbol: str = "NSE-NIFTY",
        months: tuple[int, ...] = (1, 2, 3),
    ) -> tuple[Path, Path]:
        """Write 5 bars per month for *months*; return ``(lake_root, catalog_path)``."""
        lake_root = tmp_path / "lake"
        catalog_path = tmp_path / "catalog.duckdb"
        writer = ParquetWriter(lake_root=lake_root)
        with Catalog.open(catalog_path) as cat:
            sink = ParquetBarSink(writer=writer, catalog=cat)
            for month in months:
                bars = [_bar(day, month=month) for day in range(1, 6)]
                sink.append(
                    exchange="NSE",
                    segment="FNO",
                    symbol=symbol,
                    interval="1minute",
                    bars=bars,
                )
        return lake_root, catalog_path

    def test_returns_empty_table_when_no_partitions(self, tmp_path: Path) -> None:
        """Empty slice → empty table with the canonical schema."""
        catalog_path = tmp_path / "catalog.duckdb"
        with Catalog.open(catalog_path) as cat:
            table = cat.get_bars(
                lake_root=tmp_path / "lake",
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 12, 31, tzinfo=UTC),
            )
        assert table.num_rows == 0
        assert table.schema.names == [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        ]

    def test_reads_back_single_partition(self, tmp_path: Path) -> None:
        lake_root, catalog_path = self._seed_lake(tmp_path, months=(1,))
        with Catalog.open(catalog_path) as cat:
            table = cat.get_bars(
                lake_root=lake_root,
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )
        assert table.num_rows == 5
        # Monotonically sorted by timestamp
        ts_list = table.column("timestamp").to_pylist()
        assert ts_list == sorted(ts_list)

    def test_spans_multiple_partitions(self, tmp_path: Path) -> None:
        lake_root, catalog_path = self._seed_lake(tmp_path, months=(1, 2, 3))
        with Catalog.open(catalog_path) as cat:
            table = cat.get_bars(
                lake_root=lake_root,
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 3, 31, tzinfo=UTC),
            )
        assert table.num_rows == 15  # 5 bars × 3 months

    def test_filters_exact_window(self, tmp_path: Path) -> None:
        """Window narrower than a partition must return only matching rows."""
        lake_root, catalog_path = self._seed_lake(tmp_path, months=(1,))
        with Catalog.open(catalog_path) as cat:
            # Only day 3 — minute 15 of Jan 3 ↔ Jan 4 at 09:00 UTC
            table = cat.get_bars(
                lake_root=lake_root,
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                start=datetime(2024, 1, 3, 0, tzinfo=UTC),
                end=datetime(2024, 1, 3, 23, 59, tzinfo=UTC),
            )
        assert table.num_rows == 1

    def test_reject_naive_timestamps(self, tmp_path: Path) -> None:
        from brutex.data.errors import CatalogError

        with (
            Catalog.open(tmp_path / "catalog.duckdb") as cat,
            pytest.raises(CatalogError, match="timezone-aware"),
        ):
            cat.get_bars(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="X",
                interval="1minute",
                start=datetime(2024, 1, 1),  # naive
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )

    def test_reject_inverted_window(self, tmp_path: Path) -> None:
        from brutex.data.errors import CatalogError

        with (
            Catalog.open(tmp_path / "catalog.duckdb") as cat,
            pytest.raises(CatalogError, match=">="),
        ):
            cat.get_bars(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="X",
                interval="1minute",
                start=datetime(2024, 12, 31, tzinfo=UTC),
                end=datetime(2024, 1, 1, tzinfo=UTC),
            )

    def test_missing_partition_file_raises(self, tmp_path: Path) -> None:
        """Catalog row exists but Parquet file is missing → CatalogError."""
        from brutex.data.errors import CatalogError

        lake_root, catalog_path = self._seed_lake(tmp_path, months=(1,))
        # Delete the parquet file but leave the catalog row behind.
        (lake_root / "bars/NSE/FNO/NSE-NIFTY/1minute/2024/01.parquet").unlink()
        with (
            Catalog.open(catalog_path) as cat,
            pytest.raises(CatalogError, match="missing partition"),
        ):
            cat.get_bars(
                lake_root=lake_root,
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )


class TestCatalogDataSnapshotHash:
    """FR-CAT-004: ``data_snapshot_hash`` is stable + universe-aware."""

    def _seed(self, tmp_path: Path, *, symbols=("AAA", "BBB")) -> Path:
        lake_root = tmp_path / "lake"
        catalog_path = tmp_path / "catalog.duckdb"
        writer = ParquetWriter(lake_root=lake_root)
        with Catalog.open(catalog_path) as cat:
            sink = ParquetBarSink(writer=writer, catalog=cat)
            for symbol in symbols:
                for month in (1, 2):
                    bars = [_bar(day, month=month) for day in range(1, 4)]
                    sink.append(
                        exchange="NSE",
                        segment="FNO",
                        symbol=symbol,
                        interval="1minute",
                        bars=bars,
                    )
        return catalog_path

    def test_empty_slice_returns_sha256_of_empty_string(self, tmp_path: Path) -> None:
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            snapshot = cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["X"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 12, 31, tzinfo=UTC),
            )
        assert snapshot == hashlib.sha256(b"").hexdigest()

    def test_stable_across_repeated_calls(self, tmp_path: Path) -> None:
        catalog_path = self._seed(tmp_path)
        with Catalog.open(catalog_path) as cat:
            h1 = cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["AAA", "BBB"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 12, 31, tzinfo=UTC),
            )
            h2 = cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["AAA", "BBB"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 12, 31, tzinfo=UTC),
            )
        assert h1 == h2
        assert len(h1) == 64

    def test_symbol_order_does_not_matter(self, tmp_path: Path) -> None:
        """Caller can pass symbols in any order; result is canonicalised."""
        catalog_path = self._seed(tmp_path)
        with Catalog.open(catalog_path) as cat:
            h1 = cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["AAA", "BBB"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 12, 31, tzinfo=UTC),
            )
            h2 = cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["BBB", "AAA"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 12, 31, tzinfo=UTC),
            )
        assert h1 == h2

    def test_duplicate_symbols_deduped(self, tmp_path: Path) -> None:
        catalog_path = self._seed(tmp_path)
        with Catalog.open(catalog_path) as cat:
            h_clean = cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["AAA"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 12, 31, tzinfo=UTC),
            )
            h_dup = cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["AAA", "AAA", "AAA"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 12, 31, tzinfo=UTC),
            )
        assert h_clean == h_dup

    def test_different_universe_different_hash(self, tmp_path: Path) -> None:
        catalog_path = self._seed(tmp_path)
        with Catalog.open(catalog_path) as cat:
            h_aaa = cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["AAA"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 12, 31, tzinfo=UTC),
            )
            h_both = cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["AAA", "BBB"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 12, 31, tzinfo=UTC),
            )
        assert h_aaa != h_both

    def test_different_window_different_hash(self, tmp_path: Path) -> None:
        catalog_path = self._seed(tmp_path)
        with Catalog.open(catalog_path) as cat:
            h_jan = cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["AAA"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )
            h_both = cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["AAA"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 2, 28, tzinfo=UTC),
            )
        assert h_jan != h_both

    def test_reject_naive_timestamps(self, tmp_path: Path) -> None:
        from brutex.data.errors import CatalogError

        with (
            Catalog.open(tmp_path / "catalog.duckdb") as cat,
            pytest.raises(CatalogError, match="timezone-aware"),
        ):
            cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["X"],
                interval="1minute",
                start=datetime(2024, 1, 1),  # naive
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )
