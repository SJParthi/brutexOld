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
