"""Tests for `brutex.adapters.groww.parser` — Groww candle-row → CandleBar.

Covers the two correctness rules (IST→UTC timestamps, exact Decimal
prices), OI for FNO vs null for cash, and every malformed-row guard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.adapters.groww.parser import (
    CandleParseError,
    EmptyCandleRow,
    parse_candle_row,
)


def _row(
    ts: object = "2021-01-04 09:15:00",
    o: object = 100.0,
    h: object = 101.0,
    low: object = 99.0,
    c: object = 100.5,
    vol: object = 1000,
    oi: object = None,
) -> list[object]:
    return [ts, o, h, low, c, vol, oi]


class TestTimestamp:
    def test_naive_ist_converted_to_utc(self) -> None:
        # 09:15 IST == 03:45 UTC (IST = UTC+5:30)
        bar = parse_candle_row(_row(ts="2021-01-04 09:15:00"))
        assert bar.timestamp == datetime(2021, 1, 4, 3, 45, tzinfo=UTC)
        assert bar.timestamp.tzinfo is not None

    def test_iso_t_separator_accepted(self) -> None:
        bar = parse_candle_row(_row(ts="2021-01-04T09:15:00"))
        assert bar.timestamp == datetime(2021, 1, 4, 3, 45, tzinfo=UTC)

    def test_aware_timestamp_converted_to_utc(self) -> None:
        # already-aware (+05:30) → UTC as-is
        bar = parse_candle_row(_row(ts="2021-01-04T09:15:00+05:30"))
        assert bar.timestamp == datetime(2021, 1, 4, 3, 45, tzinfo=UTC)

    def test_unparseable_timestamp_raises(self) -> None:
        with pytest.raises(CandleParseError, match="unparseable candle timestamp"):
            parse_candle_row(_row(ts="not-a-date"))

    def test_non_string_timestamp_raises(self) -> None:
        with pytest.raises(CandleParseError, match="must be a string"):
            parse_candle_row(_row(ts=12345))


class TestPrices:
    def test_prices_are_exact_decimal(self) -> None:
        bar = parse_candle_row(_row(o=245.95, h=246.15, low=245.05, c=245.6))
        assert bar.open == Decimal("245.95")
        assert bar.high == Decimal("246.15")
        assert bar.low == Decimal("245.05")
        assert bar.close == Decimal("245.6")

    def test_string_price_accepted(self) -> None:
        bar = parse_candle_row(_row(o="100.00", h="100.00", low="100.00", c="100.00"))
        assert bar.open == Decimal("100.00")

    def test_unparseable_price_raises(self) -> None:
        with pytest.raises(CandleParseError, match="unparseable open price"):
            parse_candle_row(_row(o="abc"))

    def test_infinite_price_rejected(self) -> None:
        with pytest.raises(CandleParseError, match="non-finite open price"):
            parse_candle_row(_row(o=float("inf")))

    def test_nan_price_rejected(self) -> None:
        with pytest.raises(CandleParseError, match="non-finite high price"):
            parse_candle_row(_row(h=float("nan")))


class TestVolumeAndOI:
    def test_cash_null_oi(self) -> None:
        bar = parse_candle_row(_row(oi=None))
        assert bar.open_interest is None

    def test_fno_oi_int(self) -> None:
        bar = parse_candle_row(_row(oi=12345))
        assert bar.open_interest == 12345

    def test_volume_int(self) -> None:
        bar = parse_candle_row(_row(vol=735060))
        assert bar.volume == 735060

    def test_index_null_volume_maps_to_zero(self) -> None:
        # Spot INDICES carry no traded volume — Groww returns volume=null
        # (CLAUDE.md §12). A null volume is a valid bar → 0, not a parse error.
        bar = parse_candle_row(_row(vol=None))
        assert bar.volume == 0

    def test_bool_volume_rejected(self) -> None:
        with pytest.raises(CandleParseError, match="volume must be an integer"):
            parse_candle_row(_row(vol=True))

    def test_unparseable_volume_raises(self) -> None:
        with pytest.raises(CandleParseError, match="unparseable volume"):
            parse_candle_row(_row(vol="xyz"))

    def test_infinite_volume_raises(self) -> None:
        # int(float('inf')) raises OverflowError — must be caught, not escape.
        with pytest.raises(CandleParseError, match="unparseable volume"):
            parse_candle_row(_row(vol=float("inf")))

    def test_non_numeric_volume_type_raises(self) -> None:
        with pytest.raises(CandleParseError, match="unparseable volume"):
            parse_candle_row(_row(vol=[1, 2]))


class TestRowShape:
    def test_wrong_length_raises(self) -> None:
        with pytest.raises(CandleParseError, match="must have 7 fields"):
            parse_candle_row([1, 2, 3])

    def test_too_many_fields_raises(self) -> None:
        with pytest.raises(CandleParseError, match="must have 7 fields"):
            parse_candle_row(["2021-01-04 09:15:00", 1, 1, 1, 1, 1, None, "extra"])


class TestOHLCValidationGuard:
    def test_high_below_low_propagates_value_error(self) -> None:
        # CandleBar.__post_init__ guards high >= low — a mismatch raises
        with pytest.raises(ValueError, match="must be >="):
            parse_candle_row(_row(h=98.0, low=99.0))

    def test_negative_volume_propagates_value_error(self) -> None:
        with pytest.raises(ValueError, match="volume must be non-negative"):
            parse_candle_row(_row(vol=-5))


class TestEmptyBar:
    """No-data bars (null/empty/zero OHLC) are a GRACEFUL skip (EmptyCandleRow),
    distinct from a partially-malformed bar (DEC-CANDLE-EMPTY-BAR-001)."""

    def test_all_null_ohlc_is_empty_candle(self) -> None:
        with pytest.raises(EmptyCandleRow):
            parse_candle_row(_row(o=None, h=None, low=None, c=None, vol=None))

    def test_all_empty_string_ohlc_is_empty_candle(self) -> None:
        with pytest.raises(EmptyCandleRow):
            parse_candle_row(_row(o="", h="  ", low="", c=""))

    def test_all_zero_ohlc_is_empty_candle(self) -> None:
        # A traded Indian instrument never prices at 0 → all-zero == no trade.
        with pytest.raises(EmptyCandleRow):
            parse_candle_row(_row(o=0, h=0.0, low="0", c=0))

    def test_partial_null_ohlc_is_malformed_not_empty(self) -> None:
        # open absent but high/low/close present → a MALFORMED bar (warned),
        # NOT an empty one (EmptyCandleRow is a CandleParseError subclass).
        with pytest.raises(CandleParseError) as ei:
            parse_candle_row(_row(o=None))
        assert not isinstance(ei.value, EmptyCandleRow)
        assert "unparseable open price" in str(ei.value)

    def test_empty_string_volume_maps_to_zero(self) -> None:
        assert parse_candle_row(_row(vol="")).volume == 0

    def test_empty_string_oi_is_none(self) -> None:
        assert parse_candle_row(_row(oi="  ")).open_interest is None
