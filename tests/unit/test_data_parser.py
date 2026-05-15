"""Unit tests for brutex.data.parser — the broker candle row → CandleBar."""

from __future__ import annotations

from datetime import UTC, timedelta, timezone
from decimal import Decimal

import pytest
from brutex.data.parser import (
    CandleRowError,
    parse_candle_row,
    parse_candle_rows,
)

_IST = timezone(timedelta(hours=5, minutes=30))


# ────────────────────────────────────────────────────────────────────────
# Happy path
# ────────────────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_fno_row_ist_to_utc(self) -> None:
        row = [
            "2024-01-25T09:15:00",
            22000.0,
            22010.0,
            21990.0,
            22005.0,
            100,
            500,
        ]
        bar = parse_candle_row(row)

        # 09:15 IST = 03:45 UTC
        assert bar.timestamp.tzinfo == UTC
        assert bar.timestamp.hour == 3
        assert bar.timestamp.minute == 45
        assert bar.open == Decimal("22000.0")
        assert bar.volume == 100
        assert bar.open_interest == 500

    def test_equity_row_none_oi(self) -> None:
        row = ["2024-01-25T09:15:00", 2500.0, 2502.0, 2498.0, 2501.0, 10000, None]
        bar = parse_candle_row(row)
        assert bar.open_interest is None

    def test_price_precision_preserved_from_string(self) -> None:
        """Floats are round-tripped via str to preserve shown precision."""
        row = ["2024-01-25T09:15:00", 100.05, 100.10, 100.00, 100.05, 1, None]
        bar = parse_candle_row(row)
        assert bar.open == Decimal("100.05")
        assert bar.close == Decimal("100.05")

    def test_t_separator_and_space_both_parse(self) -> None:
        t_row = ["2024-01-25T09:15:00", 1.0, 1.0, 1.0, 1.0, 0, None]
        space_row = ["2024-01-25 09:15:00", 1.0, 1.0, 1.0, 1.0, 0, None]
        assert parse_candle_row(t_row).timestamp == parse_candle_row(space_row).timestamp

    def test_batch_parses_all(self) -> None:
        rows = [
            ["2024-01-25T09:15:00", 1.0, 1.0, 1.0, 1.0, 0, None],
            ["2024-01-25T09:16:00", 2.0, 2.0, 2.0, 2.0, 0, None],
        ]
        bars = parse_candle_rows(rows)
        assert len(bars) == 2
        assert bars[0].open == Decimal("1.0")
        assert bars[1].open == Decimal("2.0")


# ────────────────────────────────────────────────────────────────────────
# Error paths
# ────────────────────────────────────────────────────────────────────────


class TestErrorPaths:
    def test_rejects_wrong_length(self) -> None:
        with pytest.raises(CandleRowError, match="7 elements"):
            parse_candle_row(["2024-01-25T09:15:00", 1.0, 1.0, 1.0, 1.0, 0])

    def test_rejects_non_string_timestamp(self) -> None:
        with pytest.raises(CandleRowError, match="timestamp"):
            parse_candle_row([1234567890, 1.0, 1.0, 1.0, 1.0, 0, None])

    def test_rejects_bad_iso(self) -> None:
        with pytest.raises(CandleRowError, match="ISO"):
            parse_candle_row(["not-a-date", 1.0, 1.0, 1.0, 1.0, 0, None])

    def test_rejects_non_numeric_price(self) -> None:
        with pytest.raises(CandleRowError, match="open"):
            parse_candle_row(["2024-01-25T09:15:00", None, 1.0, 1.0, 1.0, 0, None])

    def test_rejects_non_integer_volume(self) -> None:
        with pytest.raises(CandleRowError, match="volume"):
            parse_candle_row(["2024-01-25T09:15:00", 1.0, 1.0, 1.0, 1.0, "twelve", None])

    def test_rejects_non_integer_oi(self) -> None:
        with pytest.raises(CandleRowError, match="open_interest"):
            parse_candle_row(["2024-01-25T09:15:00", 1.0, 1.0, 1.0, 1.0, 0, "fifty"])

    def test_rejects_high_less_than_low(self) -> None:
        # CandleBar.__post_init__ validates OHLC ordering; parser
        # surfaces that as CandleRowError.
        with pytest.raises(CandleRowError, match="high"):
            parse_candle_row(["2024-01-25T09:15:00", 100.0, 99.0, 100.0, 100.0, 0, None])
