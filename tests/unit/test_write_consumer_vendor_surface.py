"""Pooled-sink vendor-surface fold (DEC-VENDOR-CONFLICT-SURFACE-001).

The write-pool path (``WriteConsumerSink`` → ``_merge_parts``) must report the
SAME two vendor-behaviour counters the direct :class:`ParquetBarSink` reports,
via the SAME COMMON fold (:func:`brutex.data.sink.vendor_surface_from_write_results`)
— otherwise a pull routed through the pool would silently read 0/None while the
direct path surfaced real drift.

Whitebox: exercises the private ``_merge_parts`` directly (the DEC-P0-017
convention). Fail-before proof: on origin/main ``SinkAppendResult`` lacks both
fields — the assertions below raise ``AttributeError`` there.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from brutex.data.models import CandleBar
from brutex.data.parquet_writer import BarConflict, WriteResult
from brutex.data.write_consumer import WriteResultParts, _merge_parts


def _bar(close: str) -> CandleBar:
    price = Decimal(close)
    return CandleBar(
        timestamp=datetime(2024, 6, 3, 4, 1, tzinfo=UTC),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1,
        open_interest=None,
    )


def _write_result(*, dup: int, conflicts: tuple[BarConflict, ...] = ()) -> WriteResult:
    ts = datetime(2024, 6, 3, 4, 1, tzinfo=UTC)
    return WriteResult(
        path=Path("x.parquet"),
        row_count=1,
        raw_hash="0" * 64,
        first_ts=ts,
        last_ts=ts,
        bytes_written=100,
        bars_added=1,
        conflicts=conflicts,
        vendor_dup_rows_dropped=dup,
    )


def test_merge_parts_folds_vendor_surface() -> None:
    """Two partitions' counters fold: dups SUM, delta takes the exact Decimal max."""
    conflict_small = BarConflict(
        timestamp=datetime(2024, 6, 3, 4, 1, tzinfo=UTC),
        stored=_bar("100.00"),
        vendor=_bar("100.05"),
    )
    conflict_big = BarConflict(
        timestamp=datetime(2024, 7, 3, 4, 1, tzinfo=UTC),
        stored=_bar("100.00"),
        vendor=_bar("144.70"),
    )
    parts = [
        WriteResultParts(
            bars_appended=1, write_results=(_write_result(dup=2, conflicts=(conflict_small,)),)
        ),
        WriteResultParts(
            bars_appended=1, write_results=(_write_result(dup=1, conflicts=(conflict_big,)),)
        ),
    ]
    merged = _merge_parts(parts)
    assert merged.vendor_dup_rows_dropped == 3
    assert merged.max_close_delta == Decimal("44.70")
    assert merged.partitions_written == 2


def test_merge_parts_clean_reports_defaults() -> None:
    merged = _merge_parts(
        [WriteResultParts(bars_appended=1, write_results=(_write_result(dup=0),))]
    )
    assert merged.vendor_dup_rows_dropped == 0
    assert merged.max_close_delta is None
