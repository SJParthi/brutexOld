"""SinkAppendResult vendor-surface fold (DEC-VENDOR-CONFLICT-SURFACE-001).

One ``append`` call may touch several ``(year, month)`` partitions; the sink
folds each partition's :class:`WriteResult` counters into the call-level
``vendor_dup_rows_dropped`` (sum) + ``max_close_delta`` (exact Decimal max)
through the COMMON :func:`vendor_surface_from_write_results` — the SAME fold the
pooled ``WriteConsumerSink`` merge uses, so the two production sinks can never
drift.

Fail-before proof: ``SinkAppendResult`` has neither field on origin/main —
every assertion below raises ``AttributeError`` there.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import NullBarSink, ParquetBarSink, vendor_surface_from_write_results

pytest.importorskip("polars")
pytest.importorskip("pyarrow")


_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}


def _bar(month: int, minute: int, *, close: str = "100.00") -> CandleBar:
    price = Decimal(close)
    return CandleBar(
        timestamp=datetime(2024, month, 3, 4, minute, tzinfo=UTC),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=10,
        open_interest=None,
    )


class TestParquetBarSinkVendorSurface:
    """The production sink folds both counters across the call's partitions."""

    def test_dup_rows_sum_across_partitions(self, tmp_path: Path) -> None:
        """Duplicates in TWO month partitions sum on the call result (1 + 1 = 2)."""
        sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
        result = sink.append(
            bars=[_bar(6, 1), _bar(6, 1), _bar(7, 1), _bar(7, 1), _bar(7, 2)],
            **_COORDS,
        )
        assert result.vendor_dup_rows_dropped == 2
        assert result.max_close_delta is None  # identical duplicates — no conflict

    def test_max_close_delta_is_exact_decimal_max_across_partitions(self, tmp_path: Path) -> None:
        """Keep-first restates in two partitions max-fold to the exact larger delta."""
        sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
        sink.append(bars=[_bar(6, 1, close="100.00"), _bar(7, 1, close="200.00")], **_COORDS)
        # Re-pull re-states BOTH stored bars with different closes: June drifts
        # 0.05, July drifts 44.70 — the call folds to the exact 44.70.
        result = sink.append(
            bars=[_bar(6, 1, close="100.05"), _bar(7, 1, close="244.70")], **_COORDS
        )
        assert result.max_close_delta == Decimal("44.70")
        assert result.vendor_dup_rows_dropped == 0
        assert result.bars_net_new == 0  # keep-first: the stored bars won

    def test_clean_append_reports_zero_and_none(self, tmp_path: Path) -> None:
        sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
        result = sink.append(bars=[_bar(6, 1)], **_COORDS)
        assert result.vendor_dup_rows_dropped == 0
        assert result.max_close_delta is None

    def test_empty_append_keeps_defaults(self, tmp_path: Path) -> None:
        sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
        result = sink.append(bars=[], **_COORDS)
        assert result.vendor_dup_rows_dropped == 0
        assert result.max_close_delta is None


class TestNullSinkAndCommonFold:
    def test_null_sink_reports_defaults(self) -> None:
        """The no-op sink has nothing stored to dedupe/conflict against."""
        result = NullBarSink().append(bars=[_bar(6, 1)], **_COORDS)
        assert result.vendor_dup_rows_dropped == 0
        assert result.max_close_delta is None

    def test_common_fold_over_write_results(self, tmp_path: Path) -> None:
        """The COMMON fold sums dups + maxes deltas over raw WriteResults."""
        writer = ParquetWriter(lake_root=tmp_path)
        first = writer.write(bars=[_bar(6, 1), _bar(6, 1)], **_COORDS)  # dup=1
        second = writer.write(bars=[_bar(6, 1, close="101.25")], **_COORDS)  # delta=1.25
        dup, delta = vendor_surface_from_write_results([first, second])
        assert dup == 1
        assert delta == Decimal("1.25")

    def test_common_fold_empty_results(self) -> None:
        dup, delta = vendor_surface_from_write_results([])
        assert dup == 0
        assert delta is None
