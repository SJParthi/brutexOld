"""DEC-CALENDAR-2020-DATA-001 — verified NSE/BSE 2020 trading-holiday data.

These tests pin the 2020 holiday list added to ``brutex.calendars.nse`` to close
the 2020 loud-guard gap (the data-pull floor is 2020-01-01 — ``GROWW_HISTORY_FLOOR``;
CLAUDE.md §3 — so a 2020 weekday previously hit ``HolidayDataUnavailableError``
and any sweep over 2020 refused to run).

The 12 holiday dates + the 2020 Diwali Muhurat session are asserted exactly; the
2 standing-annual November dates (Balipratipada, Guru Nanak) carry an honest
``# UNVERIFIED`` tag in the source per Golden Rule #1 — the assertions here pin
the dates the engine WILL use, not a confirmation claim.
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.calendars.nse import (
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAYS_2020,
    has_muhurat_session,
    is_holiday,
    is_trading_day,
    is_trading_day_checked,
    is_weekend,
)

#: The 12 verified NSE/BSE 2020 full-day trading holidays (DEC-CALENDAR-2020-DATA-001).
EXPECTED_2020_HOLIDAYS: list[date] = [
    date(2020, 2, 21),  # Mahashivratri (Fri)
    date(2020, 3, 10),  # Holi (Tue)
    date(2020, 4, 2),  # Ram Navami (Thu)
    date(2020, 4, 6),  # Mahavir Jayanti (Mon)
    date(2020, 4, 10),  # Good Friday (Fri)
    date(2020, 4, 14),  # Dr. Ambedkar Jayanti (Tue)
    date(2020, 5, 1),  # Maharashtra Day (Fri)
    date(2020, 5, 25),  # Id-Ul-Fitr / Ramzan Id (Mon)
    date(2020, 10, 2),  # Mahatma Gandhi Jayanti (Fri)
    date(2020, 12, 25),  # Christmas (Fri)
    date(2020, 11, 16),  # Diwali Balipratipada (Mon, UNVERIFIED in source)
    date(2020, 11, 30),  # Guru Nanak Jayanti (Mon, UNVERIFIED in source)
]


class TestHolidays2020List:
    """The 2020 frozenset matches the verified list exactly."""

    def test_frozenset_equals_expected_set(self) -> None:
        assert frozenset(EXPECTED_2020_HOLIDAYS) == HOLIDAYS_2020

    def test_holiday_count_is_twelve(self) -> None:
        assert len(HOLIDAYS_2020) == 12

    def test_2020_now_in_coverage(self) -> None:
        assert HOLIDAY_COVERAGE_FIRST_YEAR == 2020

    @pytest.mark.parametrize("holiday", EXPECTED_2020_HOLIDAYS)
    def test_each_2020_date_is_holiday(self, holiday: date) -> None:
        assert is_holiday(holiday) is True

    @pytest.mark.parametrize("holiday", EXPECTED_2020_HOLIDAYS)
    def test_each_2020_holiday_is_not_a_trading_day(self, holiday: date) -> None:
        assert is_trading_day(holiday) is False
        # And the STRICT variant agrees without raising (2020 is now covered).
        assert is_trading_day_checked(holiday) is False

    @pytest.mark.parametrize("holiday", EXPECTED_2020_HOLIDAYS)
    def test_each_2020_holiday_is_a_weekday(self, holiday: date) -> None:
        # Every listed full-day holiday falls on a trading weekday (Mon-Fri);
        # a weekend "holiday" would be redundant and is excluded by construction.
        assert holiday.weekday() < 5


class TestNormalTradingDays2020:
    """Spot-check that ordinary 2020 weekdays are trading days."""

    @pytest.mark.parametrize(
        "day",
        [
            date(2020, 1, 15),  # Wednesday, no holiday
            date(2020, 7, 1),  # Wednesday, no holiday
        ],
    )
    def test_normal_2020_weekday_is_trading_day(self, day: date) -> None:
        assert day.weekday() < 5
        assert is_holiday(day) is False
        assert is_trading_day(day) is True
        assert is_trading_day_checked(day) is True


class TestWeekend2020:
    """A 2020 weekend is non-trading, now data-complete (no raise)."""

    @pytest.mark.parametrize(
        "day",
        [
            date(2020, 3, 7),  # Saturday
            date(2020, 3, 8),  # Sunday
        ],
    )
    def test_2020_weekend_is_not_trading(self, day: date) -> None:
        assert is_weekend(day) is True
        assert is_trading_day(day) is False
        assert is_trading_day_checked(day) is False


class TestMuhurat2020:
    """The 2020 Diwali Muhurat session is recognised."""

    def test_2020_muhurat_date_recognised(self) -> None:
        muhurat = date(2020, 11, 14)  # Saturday — Diwali Laxmi Pujan evening session
        assert muhurat.weekday() == 5  # Saturday
        assert has_muhurat_session(muhurat) is True

    def test_a_non_muhurat_2020_date_is_not_a_muhurat(self) -> None:
        assert has_muhurat_session(date(2020, 11, 13)) is False
