"""Tests for `brutex.indicators.prev_day_ohlc` — previous day OHLC anchors.

Verifies the calendar-aware lookup matches the operator's Pine v6 source:
    yHigh = request.security(ticker, "D", high[1], lookahead=barmerge.lookahead_on)
    (and similarly for yLow / yOpen / yClose)

The `[1]` index on the daily TF means "yesterday's completed daily bar."
We use the NSE calendar to skip weekends and holidays correctly.
"""

from __future__ import annotations

from datetime import date

import pytest

from brutex.indicators.prev_day_ohlc import (
    DailyCandle,
    find_prev_trading_day,
    lookup_prev_day_ohlc,
    lookup_prev_day_ohlc_series,
)


class TestFindPrevTradingDay:
    def test_tuesday_prev_is_monday(self) -> None:
        # Tue 2026-05-12 -> Mon 2026-05-11 (no holiday)
        result = find_prev_trading_day(date(2026, 5, 12))
        assert result == date(2026, 5, 11)

    def test_monday_prev_is_friday_skipping_weekend(self) -> None:
        # Mon 2026-05-11 -> Fri 2026-05-08 (skip Sat 9, Sun 10)
        result = find_prev_trading_day(date(2026, 5, 11))
        assert result == date(2026, 5, 8)

    def test_skips_known_holiday(self) -> None:
        # 2024-01-26 is Republic Day (NSE holiday).
        # 2024-01-27 is a Saturday.
        # 2024-01-28 is a Sunday.
        # 2024-01-29 is Monday -> prev should be Thu 2024-01-25
        result = find_prev_trading_day(date(2024, 1, 29))
        assert result == date(2024, 1, 25)


class TestLookupPrevDayOHLC:
    @pytest.fixture
    def daily_dict(self) -> dict[date, DailyCandle]:
        return {
            date(2026, 5, 8): DailyCandle(
                day=date(2026, 5, 8),
                open_paisa=5_350_000,
                high_paisa=5_400_000,
                low_paisa=5_340_000,
                close_paisa=5_390_000,
            ),
            date(2026, 5, 11): DailyCandle(
                day=date(2026, 5, 11),
                open_paisa=5_390_000,
                high_paisa=5_420_000,
                low_paisa=5_385_000,
                close_paisa=5_410_000,
            ),
            date(2026, 5, 12): DailyCandle(
                day=date(2026, 5, 12),
                open_paisa=5_410_000,
                high_paisa=5_430_000,
                low_paisa=5_395_000,
                close_paisa=5_400_000,
            ),
        }

    def test_lookup_tuesday_returns_monday(
        self, daily_dict: dict[date, DailyCandle]
    ) -> None:
        result = lookup_prev_day_ohlc(date(2026, 5, 12), daily_dict)
        assert result is not None
        assert result.today == date(2026, 5, 12)
        assert result.prev_day == date(2026, 5, 11)
        assert result.pdh_paisa == 5_420_000
        assert result.pdl_paisa == 5_385_000
        assert result.pdo_paisa == 5_390_000
        assert result.pdc_paisa == 5_410_000

    def test_lookup_monday_returns_friday(
        self, daily_dict: dict[date, DailyCandle]
    ) -> None:
        result = lookup_prev_day_ohlc(date(2026, 5, 11), daily_dict)
        assert result is not None
        assert result.prev_day == date(2026, 5, 8)
        assert result.pdh_paisa == 5_400_000
        assert result.pdl_paisa == 5_340_000

    def test_today_is_holiday_raises(
        self, daily_dict: dict[date, DailyCandle]
    ) -> None:
        # 2024-01-26 is Republic Day
        with pytest.raises(ValueError, match="not a trading day"):
            lookup_prev_day_ohlc(date(2024, 1, 26), daily_dict)

    def test_missing_daily_candle_returns_none(self) -> None:
        # Empty dict -> no prev_day lookup possible
        result = lookup_prev_day_ohlc(date(2026, 5, 12), {})
        assert result is None


class TestLookupSeries:
    def test_bulk_lookup_skips_missing(self) -> None:
        daily_dict = {
            date(2026, 5, 8): DailyCandle(
                day=date(2026, 5, 8),
                open_paisa=100_00,
                high_paisa=110_00,
                low_paisa=90_00,
                close_paisa=105_00,
            ),
        }
        # Try to look up for 2 trading days; only one will succeed
        trading_days = [date(2026, 5, 11), date(2026, 5, 12)]
        result = lookup_prev_day_ohlc_series(trading_days, daily_dict)
        # 2026-05-11 has 2026-05-08 prev_day in dict -> hit
        # 2026-05-12 has 2026-05-11 prev_day NOT in dict -> miss
        assert date(2026, 5, 11) in result
        assert date(2026, 5, 12) not in result

    def test_bulk_lookup_all_present(self) -> None:
        daily_dict = {
            date(2026, 5, 8): DailyCandle(
                day=date(2026, 5, 8),
                open_paisa=100_00, high_paisa=110_00,
                low_paisa=90_00, close_paisa=105_00,
            ),
            date(2026, 5, 11): DailyCandle(
                day=date(2026, 5, 11),
                open_paisa=105_00, high_paisa=115_00,
                low_paisa=95_00, close_paisa=110_00,
            ),
        }
        trading_days = [date(2026, 5, 11), date(2026, 5, 12)]
        result = lookup_prev_day_ohlc_series(trading_days, daily_dict)
        assert len(result) == 2


class TestNoLookAhead:
    """Property P-06: PDH/PDL must only depend on days BEFORE today."""

    def test_today_data_not_used(self) -> None:
        # Even if today's daily candle is in the dict, PDH must be from
        # yesterday's candle, never today's.
        daily_dict = {
            date(2026, 5, 11): DailyCandle(
                day=date(2026, 5, 11),
                open_paisa=100_00,
                high_paisa=110_00,
                low_paisa=90_00,
                close_paisa=105_00,
            ),
            date(2026, 5, 12): DailyCandle(  # today — must NOT leak
                day=date(2026, 5, 12),
                open_paisa=999_99,
                high_paisa=999_99,
                low_paisa=999_99,
                close_paisa=999_99,
            ),
        }
        result = lookup_prev_day_ohlc(date(2026, 5, 12), daily_dict)
        assert result is not None
        # Must be from 2026-05-11, NOT 2026-05-12
        assert result.prev_day == date(2026, 5, 11)
        assert result.pdh_paisa == 110_00
        assert result.pdh_paisa != 999_99  # explicit: today's data is not leaked
