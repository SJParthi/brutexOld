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

import fcntl
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter, _tick_round
from brutex.data.partition_lock import partition_lock_path
from structlog.testing import capture_logs


def _partition_lock_is_held(lock_path: Path) -> bool:
    """True iff a SEPARATE fd cannot take the partition's exclusive lock (it is held).

    A second ``open`` of the lock path is a distinct open file description, so
    ``flock`` treats it like another PROCESS — the exact cross-process contention
    the lake's partition lock must win (DEC-LAKE-PARTITION-FLOCK-001).
    """
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


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


class TestBarsAddedDedup:
    """DEC-SINK-DEDUP-COUNT-001: ``WriteResult.bars_added`` is the DEDUPED count of bars
    this write actually contributed — distinct from the partition's total ``row_count``."""

    def _one(self, minute: int) -> CandleBar:
        return _bar(minute, open_="100.0", high="110.0", low="90.0", close="105.0")

    def test_fresh_write_adds_every_bar(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        result = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[self._one(0), self._one(1)],
        )
        assert result.bars_added == 2  # a fresh partition adds all incoming bars
        assert result.row_count == 2

    def test_full_repull_adds_zero(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        bars = [self._one(0), self._one(1)]
        writer.write(
            exchange="NSE", segment="CASH", symbol="NSE-NIFTY", interval="1minute", bars=bars
        )
        # Re-pull the SAME two bars — all already stored → ZERO added (the dedup-count fix).
        second = writer.write(
            exchange="NSE", segment="CASH", symbol="NSE-NIFTY", interval="1minute", bars=bars
        )
        assert second.bars_added == 0  # NOT len(bars)=2 — the honest deduped count
        assert second.row_count == 2  # the partition still holds both

    def test_partial_overlap_adds_only_the_new_bars(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[self._one(0), self._one(1)],
        )
        # Re-pull minute 1 (already stored) + minute 2 (new) → only ONE bar is added.
        second = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[self._one(1), self._one(2)],
        )
        assert second.bars_added == 1
        assert second.row_count == 3

    def test_keep_first_conflict_adds_zero(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(0, open_="100.0", high="110.0", low="90.0", close="105.0")],
        )
        # Re-pull the SAME timestamp with a DIFFERENT (in-range) close → keep-first conflict.
        second = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(0, open_="100.0", high="110.0", low="90.0", close="101.0")],
        )
        assert second.bars_added == 0  # the stored bar is kept; the vendor restate adds nothing
        assert len(second.conflicts) == 1


class TestFreshWriteBatchDedup:
    """Within-batch duplicate-timestamp de-dup on a FRESH partition write.

    Forensic-audit finding #11 (DEC-FRESH-WRITE-BATCH-DEDUP-001): ``write``
    sorted the incoming batch but never de-duplicated it, and the fresh branch
    rendered ``sorted_bars`` directly — so a corrupt vendor payload repeating a
    timestamp stored BOTH rows on a first write, and a settled past month is
    never rewritten, so the duplicate persisted. The fresh branch now applies
    the SAME per-``merge_mode`` keep-first/latest-wins collision rule
    ``_merge_write``'s timestamp-keyed dict already applies, so both branches
    see de-duplicated input consistently.
    """

    def test_duplicate_ts_fresh_write_stores_exactly_one_row(self, tmp_path: Path) -> None:
        """FAILS-BEFORE regression: an identical duplicate-ts payload on a fresh
        partition stores exactly ONE row (the unfixed writer stored two)."""
        pl = pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        dupe = _bar(0, open_="100.00", high="110.00", low="90.00", close="105.00")
        result = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[dupe, dupe],
        )
        assert result.row_count == 1
        assert result.bars_added == 1  # ONE distinct bar contributed, not the input count 2
        assert result.conflicts == ()  # an identical duplicate is a silent drop (merge parity)
        df = pl.read_parquet(result.path)
        assert df.height == 1  # the lake holds ONE row for the duplicated minute

    def test_duplicate_ts_differing_values_keep_first_records_conflict(
        self, tmp_path: Path
    ) -> None:
        """A differing-value duplicate keeps the FIRST occurrence in sorted order
        and records the dropped duplicate as a BarConflict (the existing
        observability surface — logged downstream, never silently discarded)."""
        pl = pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        result = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[
                _bar(0, open_="100.00", high="110.00", low="90.00", close="105.00"),  # kept
                _bar(0, open_="200.00", high="210.00", low="190.00", close="205.00"),  # dropped
            ],
        )
        assert result.row_count == 1
        assert result.bars_added == 1
        (conflict,) = result.conflicts
        assert conflict.stored.open == Decimal("100.00")  # the kept first occurrence
        assert conflict.vendor.open == Decimal("200.00")  # the discarded duplicate
        df = pl.read_parquet(result.path)
        assert df["open"][0] == 100.0  # first-in-sorted-order wins (keep-first)

    def test_duplicate_ts_latest_wins_keeps_last_and_converges_with_merge(
        self, tmp_path: Path
    ) -> None:
        """Under latest-wins the LATER duplicate wins (no conflict) — the same rule
        the merge applies, so a re-pull of the same batch is byte-stable (Rule #7)."""
        pl = pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        bars = [
            _bar(0, open_="100.00", high="110.00", low="90.00", close="105.00"),
            _bar(0, open_="200.00", high="210.00", low="190.00", close="205.00"),
        ]
        coords = {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1day",
        }
        result = writer.write(bars=bars, merge_mode="latest-wins", **coords)
        assert result.row_count == 1
        assert result.conflicts == ()  # an intentional restate, never a conflict
        assert pl.read_parquet(result.path)["open"][0] == 200.0  # the LATER restate won
        # Re-pulling the SAME duplicate batch through the MERGE path converges on
        # the same bytes — the fresh dedup mirrors the merge rule per mode.
        again = writer.write(bars=bars, merge_mode="latest-wins", **coords)
        assert again.raw_hash == result.raw_hash

    def test_distinct_ts_fresh_write_byte_identical_to_direct_render(self, tmp_path: Path) -> None:
        """A duplicate-free batch passes the dedup unchanged: the stored bytes equal
        a direct render of the sorted tick-rounded bars — byte-identical pre-fix."""
        import hashlib

        pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        bars = [
            _bar(1, open_="106.00", high="111.00", low="91.00", close="107.00"),
            _bar(0, open_="100.00", high="110.00", low="90.00", close="105.00"),
        ]
        result = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )
        expected = writer._render_bytes(
            sorted((_tick_round(b) for b in bars), key=lambda b: b.timestamp)
        )
        assert result.raw_hash == hashlib.sha256(expected).hexdigest()
        assert result.row_count == 2
        assert result.bars_added == 2
        assert result.conflicts == ()

    def test_merge_path_still_conflicts_against_the_stored_bar(self, tmp_path: Path) -> None:
        """Merge path UNCHANGED: within-batch duplicates against an EXISTING partition
        each conflict against the STORED bar (both ``stored`` = the lake value) — the
        batch is NOT pre-deduped before the merge, which would re-pair the conflicts."""
        pl = pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        coords = {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        }
        writer.write(
            bars=[_bar(0, open_="100.00", high="110.00", low="90.00", close="105.00")], **coords
        )
        second = writer.write(
            bars=[
                _bar(0, open_="101.00", high="111.00", low="91.00", close="106.00"),
                _bar(0, open_="102.00", high="112.00", low="92.00", close="107.00"),
            ],
            **coords,
        )
        assert second.row_count == 1
        assert second.bars_added == 0
        # BOTH restates conflict against the STORED 100.00 bar (keep-first), proving
        # the raw batch reached the merge — not a within-batch (101 vs 102) pairing.
        assert [c.stored.open for c in second.conflicts] == [Decimal("100.00")] * 2
        assert [c.vendor.open for c in second.conflicts] == [Decimal("101.00"), Decimal("102.00")]
        assert pl.read_parquet(second.path)["open"][0] == 100.0

    def test_fresh_dedup_oom_maps_to_lake_write_halt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OOM inside the fresh dedup loop (the per-duplicate value check) maps to
        a graceful LakeWriteHalt — DEC-LAKE-MERGE-OOM-HALT-001 parity with the merge
        loop, never a raw worker crash."""
        pytest.importorskip("polars")
        from brutex.data.errors import LakeWriteHalt

        writer = ParquetWriter(lake_root=tmp_path)

        def _oom(*_a: object, **_k: object) -> object:
            raise MemoryError("fresh dedup OOM")

        monkeypatch.setattr(writer, "_bar_values_equal", _oom)
        dupe = _bar(0, open_="100.00", high="110.00", low="90.00", close="105.00")
        with pytest.raises(LakeWriteHalt) as exc_info:
            writer.write(
                exchange="NSE",
                segment="CASH",
                symbol="NSE-NIFTY",
                interval="1minute",
                bars=[dupe, dupe],  # the duplicate reaches the value-equality check
            )
        assert exc_info.value.reason == "out_of_memory"


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

    def test_atomic_write_cleans_up_on_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_atomic_write must unlink the tempfile and re-raise on write failure.

        _atomic_write delegates to the shared atomic_io helper (DEC-LAKE-WRITE-HALT-001),
        so the rename happens there — patch os.replace via the module path. A generic
        OSError (no ENOSPC errno) is NOT mapped to LakeWriteHalt; it re-raises as-is.
        """
        pytest.importorskip("polars")
        from brutex.data.parquet_writer import ParquetWriter

        def _fail_replace(src: str, dst: str) -> None:
            raise OSError("simulated replace failure")

        monkeypatch.setattr("brutex.data.atomic_io.os.replace", _fail_replace)
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
        assert list(tmp_path.rglob("*.tmp")) == []

    def test_merge_read_oom_halts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A MemoryError during the merge READ (steady-state re-pull) → LakeWriteHalt.

        The merge read loads the WHOLE existing partition; an OOM there must become a
        graceful LakeWriteHalt pull-wide stop, not a raw worker crash
        (DEC-LAKE-WRITE-HALT-001 — the merge-read guard).
        """
        pl = pytest.importorskip("polars")
        from brutex.data.errors import LakeWriteHalt
        from brutex.data.parquet_writer import ParquetWriter

        writer = ParquetWriter(lake_root=tmp_path)
        coords = {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        }
        # First write creates the partition (no merge read).
        writer.write(bars=[_bar(0, open_="100", high="110", low="90", close="105")], **coords)

        def _oom(*_a: object, **_k: object) -> object:
            raise MemoryError("merge read OOM")

        monkeypatch.setattr(pl, "read_parquet", _oom)
        # The SECOND write merges → reads the existing partition → OOM → LakeWriteHalt.
        with pytest.raises(LakeWriteHalt) as exc_info:
            writer.write(bars=[_bar(1, open_="101", high="111", low="91", close="106")], **coords)
        assert exc_info.value.reason == "out_of_memory"

    def test_merge_loop_oom_halts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A MemoryError INSIDE the merge dict-build / sort body → LakeWriteHalt, not a raw crash.

        DEC-LAKE-MERGE-OOM-HALT-001: the read + render were already guarded, but the in-Python
        merge loop + the final ``sorted()`` (which materialise a potentially-large merged dict)
        were NOT — an OOM there crashed the whole pull raw. The loop is now wrapped in
        ``lake_write_guard``, so an OOM in the SAME-timestamp value-equality check (called per
        bar in the loop) maps to a graceful ``LakeWriteHalt("out_of_memory")``.
        """
        pytest.importorskip("polars")
        from brutex.data.errors import LakeWriteHalt
        from brutex.data.parquet_writer import ParquetWriter

        writer = ParquetWriter(lake_root=tmp_path)
        coords = {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        }
        # First write creates the partition; the merge read SUCCEEDS on the second write.
        writer.write(bars=[_bar(0, open_="100", high="110", low="90", close="105")], **coords)

        def _oom(*_a: object, **_k: object) -> object:
            raise MemoryError("merge loop OOM")

        # Force the OOM INSIDE the merge loop (the per-bar same-timestamp value check), AFTER
        # the guarded read — re-pulling timestamp 0 with a different value reaches that branch.
        monkeypatch.setattr(writer, "_bar_values_equal", _oom)
        with pytest.raises(LakeWriteHalt) as exc_info:
            writer.write(bars=[_bar(0, open_="101", high="111", low="91", close="106")], **coords)
        assert exc_info.value.reason == "out_of_memory"

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
            writer._render_bytes(  # pyright: ignore[reportPrivateUsage]
                [_bar(0, open_="100", high="110", low="90", close="105")]
            )


class TestCorruptExistingPartition:
    """A corrupt existing partition quarantines its symbol, never crashes the pull (GAP #1).

    The merge READ of the EXISTING partition was unguarded, so a single corrupt /
    version-incompatible Parquet on disk crashed the WHOLE pull. The read now raises a
    QUARANTINABLE ``WriterError`` naming the bad path, so the ingest spine skips that one
    symbol's window and every OTHER symbol still pulls.
    """

    def test_corrupt_existing_partition_raises_writer_error(self, tmp_path: Path) -> None:
        """A garbage existing partition → a quarantinable WriterError naming the path."""
        from brutex.data.errors import WriterError
        from brutex.data.parquet_writer import ParquetWriter

        writer = ParquetWriter(lake_root=tmp_path)
        coords = {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        }
        # First write creates a real partition.
        result = writer.write(
            bars=[_bar(0, open_="100", high="110", low="90", close="105")], **coords
        )
        # Overwrite the on-disk Parquet with garbage so the REAL pl.read_parquet raises a
        # PolarsError on the next (merge) write.
        result.path.write_bytes(b"this is not a parquet file at all")
        with pytest.raises(WriterError, match="corrupt existing partition"):
            writer.write(bars=[_bar(1, open_="101", high="111", low="91", close="106")], **coords)

    def test_non_finite_existing_price_quarantines(self, tmp_path: Path) -> None:
        """A readable partition holding a non-finite (inf) price → quarantinable WriterError.

        DEC-LAKE-WRITE-VECTORIZE-001: a non-finite price makes the columnar
        ``cast(String).cast(Decimal)`` snap raise a ``PolarsError``, so the reader falls
        back to the exact per-row reconstruction, where building a ``CandleBar`` from
        ``inf`` raises ``ValueError`` (the ``low <= open <= high`` invariant) — re-raised
        as a quarantinable ``WriterError`` naming the path. A strict improvement on the
        original per-row path, whose un-caught error would have crashed the whole pull.
        """
        import polars as pl
        from brutex.data.errors import WriterError
        from brutex.data.parquet_writer import ParquetWriter

        writer = ParquetWriter(lake_root=tmp_path)
        coords = {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        }
        result = writer.write(
            bars=[_bar(0, open_="100", high="110", low="90", close="105")], **coords
        )
        # Overwrite the partition with a structurally-valid Parquet whose `open` is +inf —
        # readable by `pl.read_parquet` but un-snappable by the columnar Decimal cast.
        corrupt = pl.DataFrame(
            {
                "timestamp": [datetime(2021, 1, 4, tzinfo=UTC)],
                "open": [float("inf")],
                "high": [110.0],
                "low": [90.0],
                "close": [105.0],
                "volume": [0],
                "open_interest": [None],
            },
            schema={
                "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
                "open_interest": pl.Int64,
            },
        )
        corrupt.write_parquet(result.path)
        with pytest.raises(WriterError, match="corrupt existing partition"):
            writer.write(bars=[_bar(1, open_="101", high="111", low="91", close="106")], **coords)

    def test_overflowing_price_falls_back_to_per_row_and_merges(self, tmp_path: Path) -> None:
        """A finite price whose repr overflows the snap's Decimal → exact per-row fallback.

        DEC-LAKE-WRITE-VECTORIZE-001: a value so large its ``x * 100`` overflows the
        intermediate ``Decimal(precision=38)`` (overflow is empirically between 1e15 and
        1e16 at the scale-20 parse; this test uses 1e22, well past it — unreachable for a real bar price but a
        valid finite ``CandleBar``) makes the columnar snap raise a ``PolarsError``; the
        reader must FALL BACK to the exact per-row reconstruction (NOT quarantine it) and
        still merge a new bar into the partition, with the stored large value preserved on
        the 2dp grid. This pins the correctness-over-speed fallback branch.
        """
        import polars as pl
        from brutex.data.parquet_writer import ParquetWriter

        writer = ParquetWriter(lake_root=tmp_path)
        coords = {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        }
        # Seed a normal partition, then overwrite it with a structurally-valid frame whose
        # price is a finite 1e22 (low <= open <= high holds), forcing the snap to overflow.
        result = writer.write(
            bars=[_bar(0, open_="100", high="110", low="90", close="105")], **coords
        )
        huge = 1e22
        big_frame = pl.DataFrame(
            {
                "timestamp": [datetime(2021, 1, 4, 0, 0, tzinfo=UTC)],
                "open": [huge],
                "high": [huge],
                "low": [huge],
                "close": [huge],
                "volume": [0],
                "open_interest": [None],
            },
            schema={
                "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
                "open_interest": pl.Int64,
            },
        )
        big_frame.write_parquet(result.path)
        # Merging a new bar re-reads the partition → the snap overflows → per-row fallback.
        merge_res = writer.write(
            bars=[_bar(1, open_="101", high="111", low="91", close="106")], **coords
        )
        # Both bars are present (the huge stored bar reconstructed via the fallback +
        # the merged bar), and the huge value is preserved exactly (already integral).
        stored = pl.read_parquet(merge_res.path).sort("timestamp")
        assert merge_res.row_count == 2
        assert stored["open"].to_list() == [huge, 101.0]

    def test_unreadable_existing_partition_bytes_raises_writer_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-ENOSPC OSError reading the partition bytes → a quarantinable WriterError.

        A vanished / I/O-faulting partition file is a per-symbol problem, so it
        quarantines (WriterError) rather than crashing the pull — distinct from an
        ENOSPC/OOM which stays a pull-wide LakeWriteHalt.
        """
        from pathlib import Path as _Path

        from brutex.data.errors import WriterError
        from brutex.data.parquet_writer import ParquetWriter

        writer = ParquetWriter(lake_root=tmp_path)
        coords = {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        }
        writer.write(bars=[_bar(0, open_="100", high="110", low="90", close="105")], **coords)

        original_read_bytes = _Path.read_bytes

        def _io_fault(self: _Path) -> bytes:
            if self.suffix == ".parquet":
                raise OSError("simulated read I/O fault")
            return original_read_bytes(self)

        monkeypatch.setattr(_Path, "read_bytes", _io_fault)
        with pytest.raises(WriterError, match="unreadable existing partition"):
            writer.write(bars=[_bar(1, open_="101", high="111", low="91", close="106")], **coords)


class TestNoOpReusesExistingBytes:
    """The idempotent no-op merge reuses the already-read bytes — no second full read (GAP #11).

    The ``added == 0 & not rewrite`` path used to ``dest.read_bytes()`` + ``dest.stat()``
    a SECOND time (OUTSIDE the write guard) just to hash a file it had already loaded — an
    unguarded OOM site on a deep partition. It now reuses the bytes read for the merge.
    """

    def test_idempotent_repull_does_not_reread_the_partition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pure re-pull reads the partition's bytes EXACTLY once (no second read for the hash)."""
        from brutex.data.parquet_writer import ParquetWriter

        writer = ParquetWriter(lake_root=tmp_path)
        coords = {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        }
        bar = _bar(0, open_="100", high="110", low="90", close="105")
        result = writer.write(bars=[bar], **coords)

        reads: list[str] = []
        original = ParquetWriter._read_existing_bytes  # the plain underlying function

        def _counting_read(dest: Path) -> bytes:
            reads.append(str(dest))
            return original(dest)

        monkeypatch.setattr(ParquetWriter, "_read_existing_bytes", staticmethod(_counting_read))
        # Re-pull the SAME bar — added == 0, not rewrite → the no-op return path.
        again = writer.write(bars=[bar], **coords)
        # The bytes were read exactly ONCE (the merge read); the no-op hash reused them.
        assert reads == [str(result.path)]
        # The reused-byte hash + size match the first write's exactly (byte-stable no-op).
        assert again.raw_hash == result.raw_hash
        assert again.bytes_written == result.bytes_written

    def test_no_op_repull_oom_on_byte_read_halts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OOM reading the partition bytes on an identical re-pull → LakeWriteHalt.

        GAP #11 regression: the no-op path's stat/hash now reuses the GUARDED single byte
        read, so an OOM there is a graceful pull-wide LakeWriteHalt('out_of_memory'),
        never a raw crash.
        """
        from pathlib import Path as _Path

        from brutex.data.errors import LakeWriteHalt
        from brutex.data.parquet_writer import ParquetWriter

        writer = ParquetWriter(lake_root=tmp_path)
        coords = {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        }
        bar = _bar(0, open_="100", high="110", low="90", close="105")
        writer.write(bars=[bar], **coords)

        def _oom_read_bytes(self: _Path) -> bytes:
            raise MemoryError("partition byte read OOM")

        monkeypatch.setattr(_Path, "read_bytes", _oom_read_bytes)
        with pytest.raises(LakeWriteHalt) as exc_info:
            writer.write(bars=[bar], **coords)
        assert exc_info.value.reason == "out_of_memory"


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


class TestLatestWinsMerge:
    """``merge_mode="latest-wins"`` for the derived ``1day`` partition (FIX-2).

    DEC-1MINUTE-ONLY-DERIVE-1DAY-001: a re-derivation of the SAME timestamp OVERWRITES
    the stored derived bar (no conflict recorded) so a later complete-session pull
    corrects a partial-session frozen daily. Scoped to the derived ``1day`` write path
    only — the default keep-first path is unchanged (covered by ``TestBarConflict``).
    """

    def test_latest_wins_overwrites_stored_bar(self, tmp_path: Path) -> None:
        """A same-ts/different-value bar OVERWRITES the stored one, no conflict recorded."""
        pl = pytest.importorskip("polars")

        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1day",
            bars=[_bar(0, open_="100.00", high="110.00", low="90.00", close="105.00", volume=5)],
            merge_mode="latest-wins",
        )
        second = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1day",
            bars=[_bar(0, open_="200.00", high="210.00", low="190.00", close="205.00", volume=9)],
            merge_mode="latest-wins",
        )
        # latest-wins: NO conflict recorded, file IS rewritten with the corrected value.
        assert second.conflicts == ()
        assert second.row_count == 1
        df = pl.read_parquet(second.path)
        assert df["open"][0] == 200.0  # the corrected (latest) value won
        assert df["volume"][0] == 9

    def test_latest_wins_identical_rerun_is_noop(self, tmp_path: Path) -> None:
        """latest-wins over IDENTICAL bars adds nothing + rewrites nothing (idempotent)."""
        pytest.importorskip("polars")

        writer = ParquetWriter(lake_root=tmp_path)
        bars = [_bar(0, open_="100.00", high="110.00", low="90.00", close="105.00", volume=5)]
        first = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1day",
            bars=bars,
            merge_mode="latest-wins",
        )
        second = writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1day",
            bars=bars,
            merge_mode="latest-wins",
        )
        # Identical re-derive → no rewrite (added==0 and rewrite==False) → byte-stable.
        assert second.conflicts == ()
        assert second.raw_hash == first.raw_hash

    def test_unknown_merge_mode_raises(self, tmp_path: Path) -> None:
        """An unrecognised merge_mode fails loud (WriterError) — no silent fallback."""
        from brutex.data.errors import WriterError

        writer = ParquetWriter(lake_root=tmp_path)
        with pytest.raises(WriterError, match="unknown merge_mode"):
            writer.write(
                exchange="NSE",
                segment="CASH",
                symbol="NSE-NIFTY",
                interval="1day",
                bars=[_bar(0, open_="100.00", high="110.00", low="90.00", close="105.00")],
                merge_mode="bogus",
            )


class TestPartitionLock:
    """Per-partition write serialisation (DEC-DHAN-PARALLEL-WINDOWS-001).

    The merge-on-write is a read-modify-write, so when the per-window spot fetch
    is parallelised two windows of ONE series can target the SAME month
    partition (adjacent 90-day windows straddle a month boundary) and a naive
    concurrent ``os.replace`` would drop the loser's bars. A per-PATH lock
    serialises only same-file writers; different partitions never contend.
    """

    def test_same_path_serialises(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import _PARTITION_LOCKS

        dest = tmp_path / "NSE" / "CASH" / "NIFTY" / "1minute" / "2021-01.parquet"
        # The lock is interned by path: a re-entrant acquire of the SAME path would
        # deadlock (one shared non-reentrant Lock object), so a nested non-blocking
        # acquire of the underlying lock must FAIL while the outer one is held.
        with _PARTITION_LOCKS.acquire(dest):
            with _PARTITION_LOCKS._guard:  # pyright: ignore[reportPrivateUsage]
                entry = _PARTITION_LOCKS._locks[dest]  # pyright: ignore[reportPrivateUsage]
            nested_acquired = entry.lock.acquire(blocking=False)
            assert nested_acquired is False

    def test_different_paths_do_not_contend(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import _PARTITION_LOCKS

        a = tmp_path / "a" / "2021-01.parquet"
        b = tmp_path / "b" / "2021-02.parquet"
        # Distinct partitions get distinct locks → holding a's lock never blocks b's
        # (the fan-out keeps its parallelism). b's underlying lock acquires freely.
        with _PARTITION_LOCKS.acquire(a):
            with _PARTITION_LOCKS._guard:  # pyright: ignore[reportPrivateUsage]
                b_entry = _PARTITION_LOCKS._locks.get(b)  # pyright: ignore[reportPrivateUsage]
            # b was never acquired, so it is not even interned yet — proving no contention.
            assert b_entry is None

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


class TestCrossProcessPartitionLock:
    """Cross-process partition write-lock (DEC-LAKE-PARTITION-FLOCK-001).

    The intra-process ``threading.Lock`` above only serialises threads in ONE
    interpreter; a concurrent CLI ``brutex fetch`` + a dashboard pull sharing the
    lake are SEPARATE processes and would still lost-update the same month
    partition (both read the old file, the later ``os.replace`` clobbers the
    earlier writer's new bars). The advisory ``flock`` must be HELD across the
    whole read-modify-write so the later process reads AFTER the earlier rename.
    """

    def test_flock_is_held_across_the_merge_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The partition flock is held while the writer reads the existing partition.

        Regression for the silent cross-process lost-update: a separate fd (≈ another
        process) is probed DURING the merge read and must find the lock HELD. WITHOUT
        the cross-process flock the probe would succeed and this assertion fails.
        """
        pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        coords = {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        }
        # First write creates the partition so the SECOND write takes the merge
        # (read-modify-write) path that reads the existing file.
        writer.write(bars=[_bar(0, open_="100", high="100", low="100", close="100")], **coords)

        observed: dict[str, bool] = {}
        original_read = writer._read_existing_bars

        def _spy_read(dest: Path, existing_bytes: bytes) -> dict[datetime, CandleBar]:
            observed["held_during_read"] = _partition_lock_is_held(
                partition_lock_path(tmp_path, dest)
            )
            return original_read(dest, existing_bytes)

        monkeypatch.setattr(writer, "_read_existing_bars", _spy_read)
        writer.write(bars=[_bar(1, open_="101", high="101", low="101", close="101")], **coords)

        assert observed["held_during_read"] is True
        # And released afterwards (a fresh acquire succeeds) — no leaked lock.
        (only,) = tmp_path.rglob("*.parquet")
        assert _partition_lock_is_held(partition_lock_path(tmp_path, only)) is False


#: Shared partition coordinates for the telemetry tests (module-level so it is
#: not a mutable class attribute — avoids RUF012).
_TELEMETRY_COORDS: dict[str, str] = {
    "exchange": "NSE",
    "segment": "CASH",
    "symbol": "NSE-NIFTY",
    "interval": "1minute",
}


class TestLakePartitionWriteTelemetry:
    """`lake.partition.write` instrumentation (DEC-LAKE-WRITE-INSTRUMENT-001).

    The 28-hour pull-log forensic proved the pull is write/lake-bound DOWNSTREAM
    of fetching but the write path emitted ZERO telemetry, so the serial-write
    cost could only be INFERRED. Each successful :meth:`ParquetWriter.write` now
    emits exactly one ``lake.partition.write`` event carrying the wall-clock
    write cost (``append_ms``), the lock-acquire wait (``lock_wait_ms``), the
    bars in this append (``rows``), the full partition size (``partition_rows``),
    and the destination (``partition``). These tests capture the structlog output
    and assert the event + its fields on BOTH the fresh-write and merge paths.
    """

    @staticmethod
    def _write_events(
        logs: Sequence[Mapping[str, object]],
    ) -> list[Mapping[str, object]]:
        return [e for e in logs if e.get("event") == "lake.partition.write"]

    def test_fresh_write_emits_one_event_with_fields(self, tmp_path: Path) -> None:
        """A first (fresh) write emits exactly one event with all six fields."""
        pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        with capture_logs() as logs:
            result = writer.write(
                bars=[
                    _bar(0, open_="100", high="110", low="90", close="105"),
                    _bar(1, open_="106", high="111", low="91", close="107"),
                ],
                **_TELEMETRY_COORDS,
            )

        events = self._write_events(logs)
        assert len(events) == 1
        event = events[0]
        assert event["log_level"] == "info"
        # rows = bars in THIS append batch; partition_rows = full partition size.
        assert event["rows"] == 2
        assert event["partition_rows"] == result.row_count == 2
        # partition is the destination path string.
        assert event["partition"] == str(result.path)
        # Latencies are present, non-negative floats (wall-clock perf_counter deltas).
        append_ms = event["append_ms"]
        lock_wait_ms = event["lock_wait_ms"]
        assert isinstance(append_ms, float) and append_ms >= 0.0
        assert isinstance(lock_wait_ms, float) and lock_wait_ms >= 0.0

    def test_merge_write_emits_event_with_full_partition_rows(self, tmp_path: Path) -> None:
        """The merge (read-modify-write) path also emits, with the merged total.

        ``rows`` reflects only the bars in the second append (1), while
        ``partition_rows`` reflects the full merged month (2) — the real
        disk-write-cost signal the forensic needs.
        """
        pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            bars=[_bar(0, open_="100", high="110", low="90", close="105")], **_TELEMETRY_COORDS
        )

        with capture_logs() as logs:
            result = writer.write(
                bars=[_bar(1, open_="106", high="111", low="91", close="107")], **_TELEMETRY_COORDS
            )

        events = self._write_events(logs)
        assert len(events) == 1
        event = events[0]
        assert event["rows"] == 1  # only this append's bars
        assert event["partition_rows"] == result.row_count == 2  # full merged month

    def test_idempotent_repull_still_emits_event(self, tmp_path: Path) -> None:
        """A no-op re-pull (every bar already stored) STILL emits one event.

        The merge short-circuits without rewriting the file, but the write call
        succeeded — so the telemetry fires, reporting the unchanged partition.
        """
        pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        bars = [_bar(0, open_="100", high="110", low="90", close="105")]
        writer.write(bars=bars, **_TELEMETRY_COORDS)

        with capture_logs() as logs:
            writer.write(bars=bars, **_TELEMETRY_COORDS)  # identical re-pull → no rewrite

        events = self._write_events(logs)
        assert len(events) == 1
        assert events[0]["rows"] == 1
        assert events[0]["partition_rows"] == 1

    def test_event_count_matches_partitions_for_multi_partition_sink(self, tmp_path: Path) -> None:
        """One event per partition: a sink batch spanning two months → two events.

        ``ParquetBarSink`` splits a cross-month batch into one
        ``ParquetWriter.write`` call per ``(year, month)`` partition, so the
        telemetry is one event per physical partition write (O(1) per write,
        never per bar).
        """
        pytest.importorskip("polars")
        from brutex.data.sink import ParquetBarSink

        sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
        jan = _bar(0, open_="100", high="110", low="90", close="105")
        feb = CandleBar(
            timestamp=datetime(2021, 2, 1, 4, 0, tzinfo=UTC),
            open=Decimal("200"),
            high=Decimal("210"),
            low=Decimal("190"),
            close=Decimal("205"),
            volume=0,
            open_interest=None,
        )
        with capture_logs() as logs:
            sink.append(bars=[jan, feb], **_TELEMETRY_COORDS)

        assert len(self._write_events(logs)) == 2


class TestPartitionLogLevel:
    """DEC-PREBUILD-LOG-FLOOD-001: the per-partition write telemetry's level is
    a per-writer construction choice — INFO on the live-pull vendor writers
    (rare + valuable), DEBUG on the prebuild's derived-TF writers (whose ~10⁵
    per-partition lines at INFO rotated the fetch-phase audit trail away in
    ~26 minutes). Event NAME + FIELDS are identical at either level."""

    def test_default_writer_emits_partition_write_at_info(self, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path)
        with capture_logs() as logs:
            writer.write(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                bars=[_bar(0, open_="1", high="2", low="1", close="2")],
            )
        events = [e for e in logs if e["event"] == "lake.partition.write"]
        assert len(events) == 1
        assert events[0]["log_level"] == "info"  # the LIVE-PULL default
        assert {"append_ms", "lock_wait_ms", "rows", "partition_rows", "partition"} <= set(
            events[0]
        )

    def test_debug_writer_emits_same_event_and_fields_at_debug(self, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path, partition_log_level="debug")
        with capture_logs() as logs:
            writer.write(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="5minute",
                bars=[_bar(0, open_="1", high="2", low="1", close="2")],
            )
        events = [e for e in logs if e["event"] == "lake.partition.write"]
        assert len(events) == 1
        assert events[0]["log_level"] == "debug"  # the derived-TF prebuild level
        # Same fields — the telemetry is demoted, never dropped (Golden Rule #1).
        assert {"append_ms", "lock_wait_ms", "rows", "partition_rows", "partition"} <= set(
            events[0]
        )

    def test_unknown_partition_log_level_raises_writer_error(self, tmp_path: Path) -> None:
        from brutex.data.errors import WriterError

        with pytest.raises(WriterError, match="partition_log_level"):
            ParquetWriter(lake_root=tmp_path, partition_log_level="warn")
