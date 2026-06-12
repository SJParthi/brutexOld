"""Tests for `brutex.adapters.groww.parser` — row-array → CandleBar.

100% line+branch: IST→UTC conversion, float→Decimal precision, volume
normalisation (null + drop_volume), OI column (present/absent/null),
short-row error, non-string timestamp error, and unparseable timestamp.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.adapters.groww.parser import parse_candle_rows
from brutex.data.models import CandleBar


def _row(
    ts: str = "2024-06-03T09:15:00",
    open_: float = 22000.0,
    high: float = 22100.0,
    low: float = 21900.0,
    close: float = 22050.0,
    volume: int | None = 1500,
    oi: int | None = None,
) -> list[object]:
    """Build a candle row list matching the Groww response format."""
    row: list[object] = [ts, open_, high, low, close, volume]
    if oi is not None:
        row.append(oi)
    return row


class TestTimestampConversion:
    def test_ist_to_utc_morning(self) -> None:
        # 09:15 IST = 03:45 UTC
        bars = parse_candle_rows([_row(ts="2024-06-03T09:15:00")])
        assert bars[0].timestamp == datetime(2024, 6, 3, 3, 45, tzinfo=UTC)

    def test_ist_to_utc_afternoon(self) -> None:
        # 15:29 IST = 09:59 UTC
        bars = parse_candle_rows([_row(ts="2024-06-03T15:29:00")])
        assert bars[0].timestamp == datetime(2024, 6, 3, 9, 59, tzinfo=UTC)

    def test_timestamp_is_utc_aware(self) -> None:
        bars = parse_candle_rows([_row()])
        assert bars[0].timestamp.tzinfo is not None
        assert bars[0].timestamp.utcoffset() is not None

    def test_non_string_timestamp_raises(self) -> None:
        row: list[object] = [20240603091500, 100.0, 110.0, 90.0, 105.0, 1000]
        with pytest.raises(ValueError, match="not a string"):
            parse_candle_rows([row])

    def test_unparseable_timestamp_raises(self) -> None:
        row: list[object] = ["not-a-date", 100.0, 110.0, 90.0, 105.0, 1000]
        with pytest.raises(ValueError):
            parse_candle_rows([row])


class TestPriceConversion:
    def test_float_price_to_decimal_without_drift(self) -> None:
        bars = parse_candle_rows([_row(open_=22499.95, high=22500.0, low=22490.1, close=22495.05)])
        assert bars[0].open == Decimal("22499.95")
        assert bars[0].high == Decimal("22500.0")
        assert bars[0].low == Decimal("22490.1")
        assert bars[0].close == Decimal("22495.05")

    def test_int_prices_work(self) -> None:
        row: list[object] = ["2024-06-03T09:15:00", 22000, 22100, 21900, 22050, 1000]
        bars = parse_candle_rows([row])
        assert bars[0].open == Decimal("22000")


class TestVolume:
    def test_volume_preserved(self) -> None:
        bars = parse_candle_rows([_row(volume=7500)])
        assert bars[0].volume == 7500

    def test_null_volume_normalised_to_zero(self) -> None:
        row: list[object] = ["2024-06-03T09:15:00", 22000.0, 22100.0, 21900.0, 22050.0, None]
        bars = parse_candle_rows([row])
        assert bars[0].volume == 0

    def test_drop_volume_forces_zero(self) -> None:
        bars = parse_candle_rows([_row(volume=9999)], drop_volume=True)
        assert bars[0].volume == 0

    def test_drop_volume_also_zeroes_null(self) -> None:
        row: list[object] = ["2024-06-03T09:15:00", 100.0, 110.0, 90.0, 105.0, None]
        bars = parse_candle_rows([row], drop_volume=True)
        assert bars[0].volume == 0


class TestOpenInterest:
    def test_oi_present_and_set(self) -> None:
        row: list[object] = ["2024-06-03T09:15:00", 100.0, 110.0, 90.0, 105.0, 1000, 50000]
        bars = parse_candle_rows([row])
        assert bars[0].open_interest == 50000

    def test_oi_null_yields_none(self) -> None:
        row: list[object] = ["2024-06-03T09:15:00", 100.0, 110.0, 90.0, 105.0, 1000, None]
        bars = parse_candle_rows([row])
        assert bars[0].open_interest is None

    def test_oi_absent_yields_none(self) -> None:
        # Row has exactly 6 columns — no OI field
        row: list[object] = ["2024-06-03T09:15:00", 100.0, 110.0, 90.0, 105.0, 1000]
        bars = parse_candle_rows([row])
        assert bars[0].open_interest is None


class TestShortRow:
    def test_5_columns_raises_value_error(self) -> None:
        row: list[object] = ["2024-06-03T09:15:00", 100.0, 110.0, 90.0, 105.0]
        with pytest.raises(ValueError, match="5 columns"):
            parse_candle_rows([row])

    def test_error_mentions_row_index(self) -> None:
        good: list[object] = ["2024-06-03T09:15:00", 100.0, 110.0, 90.0, 105.0, 1000]
        bad: list[object] = ["2024-06-03T09:16:00", 100.0, 110.0, 90.0, 105.0]
        with pytest.raises(ValueError, match="row 1"):
            parse_candle_rows([good, bad])


class TestEmptyInput:
    def test_empty_candles_returns_empty_list(self) -> None:
        assert parse_candle_rows([]) == []


class TestMultipleRows:
    def test_returns_one_bar_per_row(self) -> None:
        rows = [_row(ts=f"2024-06-03T09:{15 + i:02d}:00") for i in range(5)]
        bars = parse_candle_rows(rows)
        assert len(bars) == 5

    def test_bars_in_order(self) -> None:
        rows = [_row(ts="2024-06-03T09:15:00"), _row(ts="2024-06-03T09:16:00")]
        bars = parse_candle_rows(rows)
        assert bars[0].timestamp < bars[1].timestamp

    def test_returns_candle_bar_instances(self) -> None:
        bars = parse_candle_rows([_row()])
        assert isinstance(bars[0], CandleBar)
