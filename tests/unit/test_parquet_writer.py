"""Tests for ``brutex.data.parquet_writer`` 2dp tick-grid rounding.

DEC-SPOT-PRICE-2DP-001 — the spot lake stores OHLC snapped to the 2-decimal
rupee tick grid (``ROUND_HALF_UP``, never truncation), mirroring the option
writer, so ``/db`` shows clean values and re-pulls stay bit-idempotent.

Whitebox: deliberately exercises private helpers (``_tick_round`` rounding +
``_partition_lock`` interning, DEC-DHAN-PARALLEL-WINDOWS-001), so per the
DEC-P0-017 convention the file silences ``reportPrivateUsage`` at the top.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter, _tick_round


def _bar(
    minute: int,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: int = 0,
) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2021, 1, 4, 4, minute, tzinfo=UTC),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        open_interest=None,
    )


class TestTickRound:
    def test_rounds_half_up_not_truncated(self) -> None:
        """14020.7999 → 14020.80 — proper round-half-up, NOT truncation to .79."""
        out = _tick_round(
            _bar(0, open_="14020.7999", high="14020.7999", low="14020.7999", close="14020.7999")
        )
        assert out.open == Decimal("14020.80")
        assert out.high == Decimal("14020.80")
        assert out.low == Decimal("14020.80")
        assert out.close == Decimal("14020.80")

    def test_exact_half_rounds_up(self) -> None:
        """A clean .005 rounds UP to .01 (HALF_UP, not bankers'/HALF_EVEN)."""
        out = _tick_round(_bar(0, open_="1.005", high="1.005", low="1.005", close="1.005"))
        assert out.open == Decimal("1.01")

    def test_below_half_rounds_down(self) -> None:
        out = _tick_round(_bar(0, open_="1.004", high="1.004", low="1.004", close="1.004"))
        assert out.open == Decimal("1.00")

    def test_volume_oi_timestamp_untouched(self) -> None:
        src = _bar(7, open_="100.126", high="100.126", low="100.126", close="100.126", volume=42)
        out = _tick_round(src)
        assert out.volume == 42
        assert out.timestamp == src.timestamp
        assert out.open == Decimal("100.13")


class TestWriterRoundTrip:
    def test_lake_stores_two_decimals(self, tmp_path: Path) -> None:
        pl = pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[
                _bar(0, open_="14020.7999", high="14021.0004", low="14019.4996", close="14020.5001")
            ],
        )
        files = list(tmp_path.rglob("*.parquet"))
        assert len(files) == 1
        df = pl.read_parquet(files[0])
        assert df["open"][0] == 14020.80
        assert df["high"][0] == 14021.00
        assert df["low"][0] == 14019.50
        assert df["close"][0] == 14020.50

    def test_repull_of_raw_4dp_is_idempotent(self, tmp_path: Path) -> None:
        """Re-pulling the SAME raw 4dp bar is a no-op — no false conflict."""
        pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        bars = [
            _bar(0, open_="14020.7999", high="14021.0004", low="14019.4996", close="14020.5001")
        ]
        first = writer.write(
            exchange="NSE", segment="CASH", symbol="NSE-NIFTY", interval="1minute", bars=bars
        )
        second = writer.write(
            exchange="NSE", segment="CASH", symbol="NSE-NIFTY", interval="1minute", bars=bars
        )
        assert second.row_count == 1
        assert first.raw_hash == second.raw_hash  # byte-identical → idempotent


class TestWriterErrors:
    """Empty-bars and multi-partition guard paths in ParquetWriter.write."""

    def test_empty_bars_raises_writer_error(self) -> None:
        """Calling write() with an empty list must raise WriterError immediately."""
        from brutex.data.errors import WriterError

        writer = ParquetWriter(lake_root=Path("/tmp/never-used"))
        with pytest.raises(WriterError, match="empty bars"):
            writer.write(
                exchange="NSE",
                segment="CASH",
                symbol="NSE-NIFTY",
                interval="1minute",
                bars=[],
            )

    def test_multi_partition_bars_raises_writer_error(self) -> None:
        """Bars spanning two calendar months must raise WriterError."""
        from brutex.data.errors import WriterError

        jan_bar = _bar(0, open_="100", high="110", low="90", close="105")
        # Build a Feb bar by overriding the timestamp manually.
        from datetime import UTC, datetime
        from decimal import Decimal

        feb_bar = CandleBar(
            timestamp=datetime(2021, 2, 1, 4, 0, tzinfo=UTC),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
            volume=0,
            open_interest=None,
        )
        writer = ParquetWriter(lake_root=Path("/tmp/never-used"))
        with pytest.raises(WriterError, match="multiple partitions"):
            writer.write(
                exchange="NSE",
                segment="CASH",
                symbol="NSE-NIFTY",
                interval="1minute",
                bars=[jan_bar, feb_bar],
            )

    def test_atomic_write_cleans_up_on_error(self, tmp_path: Path) -> None:
        """_atomic_write must unlink the tempfile and re-raise on write failure."""
        from brutex.data.parquet_writer import ParquetWriter

        # We trigger the exception by monkeypatching os.replace after the
        # tempfile is written, so the cleanup path (os.unlink) is exercised.

        def _fail_replace(src: str, dst: str) -> None:
            raise OSError("simulated replace failure")

        # Patch at the module level so the writer sees our fake.
        import brutex.data.parquet_writer as _pw

        original = _pw.os.replace
        _pw.os.replace = _fail_replace  # type: ignore[assignment]
        try:
            pytest.importorskip("polars")
            writer = ParquetWriter(lake_root=tmp_path)
            with pytest.raises(OSError, match="simulated"):
                writer.write(
                    exchange="NSE",
                    segment="CASH",
                    symbol="NSE-NIFTY",
                    interval="1minute",
                    bars=[_bar(0, open_="100", high="110", low="90", close="105")],
                )
            # The tempfile was cleaned up — no leftover .tmp files.
            tmp_files = list(tmp_path.rglob("*.tmp"))
            assert tmp_files == []
        finally:
            _pw.os.replace = original

    def test_render_bytes_raises_on_missing_polars(self, tmp_path: Path) -> None:
        """_render_bytes raises WriterError with install hint when polars is absent."""
        import sys
        import unittest.mock

        from brutex.data.errors import WriterError
        from brutex.data.parquet_writer import ParquetWriter

        writer = ParquetWriter(lake_root=tmp_path)
        # Temporarily hide polars so the lazy import inside _render_bytes raises.
        with (
            unittest.mock.patch.dict(sys.modules, {"polars": None}),
            pytest.raises(WriterError, match="polars"),
        ):
            writer._render_bytes(  # type: ignore[reportPrivateUsage]
                [_bar(0, open_="100", high="110", low="90", close="105")]
            )


class TestBarConflict:
    """Keep-first conflict recording (DEC-LAKE-CONFLICT-KEEPFIRST-001).

    A same-timestamp / different-value bar must NOT raise, must NOT overwrite
    the stored bar, and must return a BarConflict in WriteResult.conflicts so
    the caller can log and continue the pull (Golden Rule #7).
    """

    def test_conflict_returns_bar_conflict_and_keeps_stored_bar(self, tmp_path: Path) -> None:
        """A pure conflict-only write: stored bar kept, file NOT rewritten, conflict returned."""
        pl = pytest.importorskip("polars")
        from brutex.data.parquet_writer import BarConflict

        writer = ParquetWriter(lake_root=tmp_path)
        # First write — the authoritative bar.
        first = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(0, open_="100.00", high="110.00", low="90.00", close="105.00")],
        )
        # Second write — same timestamp, different OHLC (vendor re-statement).
        second = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(0, open_="200.00", high="210.00", low="190.00", close="205.00")],
        )
        # Does NOT raise.
        assert len(second.conflicts) == 1
        (c,) = second.conflicts
        assert isinstance(c, BarConflict)
        # stored = the FIRST bar.
        assert c.stored.open == Decimal("100.00")
        # vendor = the rejected re-statement.
        assert c.vendor.open == Decimal("200.00")
        # Row count unchanged, hash == first hash (no rewrite — added==0).
        assert second.row_count == 1
        assert second.raw_hash == first.raw_hash
        # On-disk value is the ORIGINAL 100.00, NOT the vendor 200.00.
        df = pl.read_parquet(second.path)
        assert df["open"][0] == 100.0

    def test_conflict_alongside_new_bar_rewrites_but_keeps_stored_value(
        self, tmp_path: Path
    ) -> None:
        """Conflict + a genuinely-new bar: file IS rewritten; conflicting ts keeps stored value."""
        pl = pytest.importorskip("polars")

        writer = ParquetWriter(lake_root=tmp_path)
        # First write — one bar at minute 0.
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(0, open_="100.00", high="110.00", low="90.00", close="105.00")],
        )
        # Second write — conflict for minute 0 PLUS a new bar at minute 1.
        second = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[
                _bar(0, open_="200.00", high="210.00", low="190.00", close="205.00"),  # conflict
                _bar(1, open_="101.00", high="111.00", low="91.00", close="106.00"),  # new
            ],
        )
        # File was rewritten (added == 1 for the new bar).
        assert second.row_count == 2
        # One conflict for the re-stated bar.
        assert len(second.conflicts) == 1
        assert second.conflicts[0].stored.open == Decimal("100.00")
        assert second.conflicts[0].vendor.open == Decimal("200.00")
        # On-disk: minute 0 keeps 100.00; minute 1 is the new bar at 101.00.
        df = pl.read_parquet(second.path)
        rows = {r["timestamp"].minute: r["open"] for r in df.iter_rows(named=True)}
        assert rows[0] == 100.0  # stored value preserved
        assert rows[1] == 101.0  # new bar appended

    def test_identical_rewrite_produces_empty_conflicts(self, tmp_path: Path) -> None:
        """Re-writing an IDENTICAL bar is a no-op with conflicts=()."""
        pytest.importorskip("polars")

        writer = ParquetWriter(lake_root=tmp_path)
        bars = [_bar(0, open_="100.00", high="110.00", low="90.00", close="105.00")]
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )
        second = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )
        # No conflict for an identical re-write.
        assert second.conflicts == ()

    def test_fresh_write_produces_empty_conflicts(self, tmp_path: Path) -> None:
        """The very first write (no existing file) always returns conflicts=()."""
        pytest.importorskip("polars")

        writer = ParquetWriter(lake_root=tmp_path)
        result = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(0, open_="100.00", high="110.00", low="90.00", close="105.00")],
        )
        assert result.conflicts == ()

    def test_lake_root_property(self, tmp_path: Path) -> None:
        """The lake_root property returns the configured root path."""
        writer = ParquetWriter(lake_root=tmp_path)
        assert writer.lake_root == tmp_path

    def test_make_catalog_entry_round_trips_write_result(self, tmp_path: Path) -> None:
        """make_catalog_entry projects WriteResult fields into CatalogEntry correctly."""
        from brutex.data.models import CatalogEntry

        pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        result = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(0, open_="100.00", high="110.00", low="90.00", close="105.00")],
        )
        entry = writer.make_catalog_entry(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            result=result,
        )
        assert isinstance(entry, CatalogEntry)
        assert entry.exchange == "NSE"
        assert entry.segment == "CASH"
        assert entry.symbol == "NSE-NIFTY"
        assert entry.interval == "1minute"
        assert entry.raw_hash == result.raw_hash
        assert entry.row_count == result.row_count
        assert entry.first_ts == result.first_ts
        assert entry.last_ts == result.last_ts


class TestPartitionLock:
    """Per-partition write serialisation (DEC-DHAN-PARALLEL-WINDOWS-001).

    The merge-on-write is a read-modify-write, so when the per-window spot fetch
    is parallelised two windows of ONE series can target the SAME month
    partition (adjacent 90-day windows straddle a month boundary) and a naive
    concurrent ``os.replace`` would drop the loser's bars. A per-PATH lock
    serialises only same-file writers; different partitions never contend.
    """

    def test_same_path_returns_same_lock(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import _partition_lock

        dest = tmp_path / "NSE" / "CASH" / "NIFTY" / "1minute" / "2021-01.parquet"
        # The lock is interned by path: two calls for the same file → the SAME
        # lock object (so two ParquetWriter instances serialise on one file).
        assert _partition_lock(dest) is _partition_lock(dest)

    def test_different_paths_get_distinct_locks(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import _partition_lock

        a = tmp_path / "a" / "2021-01.parquet"
        b = tmp_path / "b" / "2021-02.parquet"
        # Distinct partitions get distinct locks → writers to different files
        # never block each other (the fan-out keeps its parallelism).
        assert _partition_lock(a) is not _partition_lock(b)

    def test_concurrent_same_partition_writes_lose_no_bars(self, tmp_path: Path) -> None:
        """Two threads merge-writing the same month from a barrier keep ALL bars.

        Without the per-partition lock this is a lost-update race (both read the
        empty/partial file, each unions only its own bar, the later rename
        wins). With the lock the merges serialise and the partition holds the
        UNION of every thread's bars regardless of completion order (Rule #7).
        """
        import threading

        pl = pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        n_threads = 16
        barrier = threading.Barrier(n_threads)

        def _write(minute: int) -> None:
            # Each thread writes a DISTINCT timestamp into the SAME (year, month)
            # partition — the union must end up with all n_threads rows.
            barrier.wait()  # maximise the race window
            writer.write(
                exchange="NSE",
                segment="CASH",
                symbol="NIFTY",
                interval="1minute",
                bars=[_bar(minute, open_="100", high="100", low="100", close="100")],
            )

        threads = [threading.Thread(target=_write, args=(m,)) for m in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        files = list(tmp_path.rglob("*.parquet"))
        assert len(files) == 1  # all wrote one month partition
        df = pl.read_parquet(files[0])
        # No lost update: every distinct-timestamp bar survived the merge.
        assert df.height == n_threads
        minutes = sorted(ts.minute for ts in df["timestamp"].to_list())
        assert minutes == list(range(n_threads))
