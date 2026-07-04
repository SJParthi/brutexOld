"""WriteResult vendor-duplicate accounting (DEC-VENDOR-CONFLICT-SURFACE-001).

The ``lake.vendor_duplicate_rows`` WARNING (DEC-LAKE-WRITE-UNIQUE-001) counted a
batch's within-batch duplicate rows but the count DIED in the log — a production
pull dropped 9,141 duplicate vendor rows visible only as log lines. The writer
now carries the SAME count on :attr:`WriteResult.vendor_dup_rows_dropped` for
BOTH branches (fresh ``_dedup_batch`` collapse + the merge dict collapse), so
the sink → spine → phase → headline fold can SUM it.

Fail-before proof: ``WriteResult`` has no ``vendor_dup_rows_dropped`` field on
origin/main — every assertion below raises ``AttributeError`` there (verified
by grep: the name does not exist anywhere on main).

Also pins the two pure fold helpers' full branch surface HERE (the stem-matched
per-module coverage suite for ``parquet_writer.py``), alongside the
property-level algebra in ``tests/property/test_conflict_delta_fold.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import (
    BarConflict,
    ParquetWriter,
    fold_max_close_delta,
    max_conflict_close_delta,
)

pytest.importorskip("polars")
pytest.importorskip("pyarrow")


_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}


def _ts(minute: int) -> datetime:
    return datetime(2024, 6, 3, 4, minute, tzinfo=UTC)


def _bar(minute: int, *, close: str = "100.00") -> CandleBar:
    price = Decimal(close)
    return CandleBar(
        timestamp=_ts(minute),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=10,
        open_interest=None,
    )


class TestWriteResultVendorDupCount:
    """The dropped-duplicate count rides the result on BOTH write branches."""

    def test_fresh_write_counts_batch_duplicates(self, tmp_path: Path) -> None:
        """A FRESH write of a batch with 2 duplicated timestamps reports dropped=2."""
        writer = ParquetWriter(lake_root=tmp_path)
        batch = [_bar(1), _bar(1), _bar(2), _bar(2), _bar(3)]
        result = writer.write(bars=batch, **_COORDS)
        assert result.vendor_dup_rows_dropped == 2
        assert result.row_count == 3

    def test_merge_write_counts_batch_duplicates(self, tmp_path: Path) -> None:
        """A MERGE write (partition exists) counts its own batch's duplicates too."""
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(bars=[_bar(1)], **_COORDS)
        result = writer.write(bars=[_bar(2), _bar(2), _bar(3)], **_COORDS)
        assert result.vendor_dup_rows_dropped == 1
        assert result.row_count == 3

    def test_duplicate_free_write_reports_zero(self, tmp_path: Path) -> None:
        """No duplicates ⇒ 0 on both branches (the byte-identical fast path)."""
        writer = ParquetWriter(lake_root=tmp_path)
        fresh = writer.write(bars=[_bar(1), _bar(2)], **_COORDS)
        merge = writer.write(bars=[_bar(3)], **_COORDS)
        assert fresh.vendor_dup_rows_dropped == 0
        assert merge.vendor_dup_rows_dropped == 0

    def test_stored_poison_heal_not_counted_as_vendor_dup(self, tmp_path: Path) -> None:
        """A same-bars RE-PULL (idempotent no-op merge) reports 0 — only THIS batch counts."""
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(bars=[_bar(1), _bar(2)], **_COORDS)
        result = writer.write(bars=[_bar(1), _bar(2)], **_COORDS)
        assert result.vendor_dup_rows_dropped == 0
        assert result.bars_added == 0


def _conflict(stored_close: str, vendor_close: str) -> BarConflict:
    return BarConflict(
        timestamp=_ts(1), stored=_bar(1, close=stored_close), vendor=_bar(1, close=vendor_close)
    )


class TestFoldHelpers:
    """Full branch surface of the two pure fold helpers (stem-suite coverage)."""

    def test_fold_none_identity_both_sides(self) -> None:
        d = Decimal("1.25")
        assert fold_max_close_delta(None, d) == d
        assert fold_max_close_delta(d, None) == d
        assert fold_max_close_delta(None, None) is None

    def test_fold_takes_exact_decimal_max(self) -> None:
        assert fold_max_close_delta(Decimal("44.70"), Decimal("0.05")) == Decimal("44.70")
        assert fold_max_close_delta(Decimal("0.05"), Decimal("44.70")) == Decimal("44.70")

    def test_max_conflict_close_delta_empty_is_none(self) -> None:
        assert max_conflict_close_delta([]) is None

    def test_max_conflict_close_delta_takes_abs_max(self) -> None:
        """|vendor − stored| both directions; the operator-observed 44.70 shape."""
        conflicts = [
            _conflict("100.00", "144.70"),  # +44.70
            _conflict("100.00", "99.95"),  # −0.05 → abs 0.05
        ]
        assert max_conflict_close_delta(conflicts) == Decimal("44.70")

    def test_zero_delta_conflict_is_zero_not_none(self) -> None:
        """Equal closes with another field differing → an honest Decimal 0, not None."""
        assert max_conflict_close_delta([_conflict("100.00", "100.00")]) == Decimal("0.00")
