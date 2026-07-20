"""Unit gate for :mod:`brutex.data.frame_bars` — the batched frame→bar LEAF.

DEC-PREBUILD-FRAME-WRITE-001 (review fold): :func:`frame_to_candle_bars`
moved HERE from ``spot_daily_resample`` so the frame-write lane's
sink/parquet_writer degrade paths can import it without re-forming the
``sink ↔ spot_daily_resample`` CodeQL ``py/cyclic-import`` cycle (the
import-graph law itself is pinned by
``tests/unit/test_data_sink_frame.py::TestImportCycleDriftPin``; the
re-export surfaces keep their own suites). This file is the module's OWN
per-module 100% line+branch gate (the ci coverage discovery contract for a
NEW module): the conversion contract — empty input, OI column present /
absent / null, the ``absent_open_interest`` override, the Decimal memo, and
value fidelity vs the legacy per-row form.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from brutex.data.frame_bars import frame_to_candle_bars

pytest.importorskip("polars")

import polars as pl

_T0 = datetime(2024, 3, 4, 3, 45, tzinfo=UTC)  # 09:15 IST, a regular session

_SCHEMA: dict[str, Any] = {
    "ts_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
}


def _frame(n: int = 2, *, oi: list[int | None] | None = None) -> pl.DataFrame:
    rows = [
        {
            "ts_utc": _T0 + timedelta(minutes=i),
            "open": 100.0 + i,
            "high": 101.5 + i,
            "low": 99.25 + i,
            "close": 100.75 + i,
            "volume": 10 * (i + 1),
        }
        for i in range(n)
    ]
    frame = pl.DataFrame(rows, schema=_SCHEMA)
    if oi is not None:
        frame = frame.with_columns(pl.Series("open_interest", oi, dtype=pl.Int64))
    return frame


class TestFrameToCandleBars:
    def test_empty_frame_returns_empty_list(self) -> None:
        assert frame_to_candle_bars(_frame(0)) == []

    def test_no_oi_column_uses_absent_default_zero(self) -> None:
        bars = frame_to_candle_bars(_frame(2))
        assert [bar.open_interest for bar in bars] == [0, 0]

    def test_no_oi_column_absent_none_maps_none(self) -> None:
        bars = frame_to_candle_bars(_frame(2), absent_open_interest=None)
        assert [bar.open_interest for bar in bars] == [None, None]

    def test_oi_column_null_maps_to_absent_value(self) -> None:
        bars = frame_to_candle_bars(_frame(2, oi=[None, 75]))
        assert [bar.open_interest for bar in bars] == [0, 75]

    def test_oi_column_null_maps_to_absent_none(self) -> None:
        bars = frame_to_candle_bars(_frame(2, oi=[None, 42]), absent_open_interest=None)
        assert [bar.open_interest for bar in bars] == [None, 42]

    def test_values_match_legacy_per_row_form(self) -> None:
        """Every field equals the legacy ``Decimal(str(x))`` per-row construction."""
        frame = _frame(3, oi=[7, None, 9])
        bars = frame_to_candle_bars(frame)
        assert len(bars) == 3
        for bar, row in zip(bars, frame.iter_rows(named=True), strict=True):
            assert bar.timestamp == row["ts_utc"]
            assert bar.open == Decimal(str(row["open"]))
            assert bar.high == Decimal(str(row["high"]))
            assert bar.low == Decimal(str(row["low"]))
            assert bar.close == Decimal(str(row["close"]))
            assert bar.volume == int(row["volume"])
        assert [bar.open_interest for bar in bars] == [7, 0, 9]

    def test_decimal_memo_shares_instances_across_repeats(self) -> None:
        """Repeated price strings parse ONCE — the memoised Decimal is shared."""
        rows = [
            {
                "ts_utc": _T0 + timedelta(minutes=i),
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1,
            }
            for i in range(2)
        ]
        bars = frame_to_candle_bars(pl.DataFrame(rows, schema=_SCHEMA))
        assert bars[0].open is bars[1].open  # same memoised instance
        assert bars[0].open is bars[0].close  # shared within one bar too
