"""Tests for `brutex.adapters.dhan.parser` — columnar → CandleBar transpose.

Targets 100% line+branch: timestamp-epoch parsing, column validation,
length checks, empty-bar skipping, and the optional volume/OI columns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.adapters.dhan.parser import (
    CandleParseError,
    parse_candle_columns,
)
from brutex.adapters.dhan.parser import _parse_timestamp_epoch as parse_ts


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "open": [100.0, 101.0],
        "high": [110.0, 111.0],
        "low": [90.0, 91.0],
        "close": [105.0, 106.0],
        "volume": [1000, 2000],
        "timestamp": [1609733100, 1609733160],
        "open_interest": [10, 20],
    }
    base.update(overrides)
    return base


class TestParseTimestampEpoch:
    def test_int_epoch(self) -> None:
        assert parse_ts(1609733100) == datetime(2021, 1, 4, 4, 5, tzinfo=UTC)

    def test_float_and_string_epoch(self) -> None:
        assert parse_ts(1609733100.0) == datetime(2021, 1, 4, 4, 5, tzinfo=UTC)
        assert parse_ts("1609733100") == datetime(2021, 1, 4, 4, 5, tzinfo=UTC)

    def test_bool_rejected(self) -> None:
        with pytest.raises(CandleParseError, match="got bool"):
            parse_ts(True)

    def test_wrong_type_rejected(self) -> None:
        with pytest.raises(CandleParseError, match="must be epoch seconds"):
            parse_ts([1])

    def test_unparseable_string_rejected(self) -> None:
        with pytest.raises(CandleParseError, match="unparseable candle timestamp"):
            parse_ts("not-a-number")

    @pytest.mark.parametrize("value", [float("inf"), float("nan")])
    def test_non_finite_rejected(self, value: float) -> None:
        with pytest.raises(CandleParseError, match="non-finite candle timestamp"):
            parse_ts(value)

    def test_out_of_range_rejected(self) -> None:
        with pytest.raises(CandleParseError, match="out-of-range candle timestamp"):
            parse_ts(10**30)


class TestParseCandleColumns:
    def test_happy_path_multi_bar(self) -> None:
        bars = parse_candle_columns(_payload())
        assert len(bars) == 2
        assert bars[0].open == Decimal("100.0")
        assert bars[0].timestamp == datetime(2021, 1, 4, 4, 5, tzinfo=UTC)
        assert bars[1].open_interest == 20

    def test_empty_payload_returns_no_bars(self) -> None:
        empty: dict[str, object] = {k: [] for k in ("open", "high", "low", "close", "timestamp")}
        assert parse_candle_columns(empty) == []

    def test_no_trade_bar_is_skipped(self) -> None:
        # Second candle is all-zero (no trade) → skipped, not raised.
        payload = _payload(
            open=[100.0, 0],
            high=[110.0, 0],
            low=[90.0, 0],
            close=[105.0, 0],
        )
        bars = parse_candle_columns(payload)
        assert len(bars) == 1

    def test_missing_required_column_raises(self) -> None:
        payload = _payload()
        del payload["close"]
        with pytest.raises(CandleParseError, match="missing required candle column 'close'"):
            parse_candle_columns(payload)

    def test_non_array_column_raises(self) -> None:
        with pytest.raises(CandleParseError, match="must be an array"):
            parse_candle_columns(_payload(high="110"))

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(CandleParseError, match="length 1 != timestamp length 2"):
            parse_candle_columns(_payload(open=[100.0]))

    def test_absent_volume_defaults_to_zero(self) -> None:
        payload = _payload()
        del payload["volume"]
        bars = parse_candle_columns(payload)
        assert bars[0].volume == 0

    def test_absent_open_interest_is_none(self) -> None:
        payload = _payload()
        del payload["open_interest"]
        bars = parse_candle_columns(payload)
        assert bars[0].open_interest is None

    def test_malformed_price_propagates(self) -> None:
        with pytest.raises(CandleParseError, match="unparseable open price"):
            parse_candle_columns(_payload(open=["abc", 101.0]))
