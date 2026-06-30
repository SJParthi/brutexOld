"""DEC-CALENDAR-2020-HONEST-001 — honest 2020 trading-day handling.

The Track-1 data-pull floor is ``2020-01-01`` (``GROWW_HISTORY_FLOOR``,
CLAUDE.md §3) but the hardcoded NSE/BSE holiday + Muhurat lists in
``brutex.calendars.nse`` only span ``[HOLIDAY_COVERAGE_FIRST_YEAR,
HOLIDAY_COVERAGE_LAST_YEAR]`` (2021-2026). On ``origin/main`` the spot-anchor
consumers ``prev_day_ohlc`` + ``weekly_anchors`` used the UNGUARDED
``is_trading_day``, so an out-of-coverage 2020 weekday was *silently* read as a
trading day — a wrong answer the operator can never see (a 2020 holiday became
a "prior trading day" with a real daily candle).

We do NOT fabricate the missing 2020 holiday dates (Golden Rule #1: no
hallucinated facts). Instead these consumers are wired to the STRICT
``is_trading_day_checked``, which raises ``HolidayDataUnavailableError`` for an
out-of-coverage WEEKDAY rather than guess "trading day". This is the loud-guard
(option b) fix: never a silent wrong verdict for an uncovered year.

These tests drive the REAL production functions (``find_prev_trading_day`` /
``lookup_prev_day_ohlc`` / ``rolling_weekly_anchor`` / ``weekly_static_anchor``
and the underlying ``is_trading_day_checked``) and assert:

* the 2020 / future-uncovered cases are now LOUD (this FAILS on origin/main,
  where ``is_trading_day(2020-03-10)`` silently returns ``True``);
* the 2021-2026 in-coverage behaviour is UNCHANGED (regression);
* every requested extreme 2020 edge case (weekend, year boundary, leap day,
  known-real-holiday weekday, future uncovered year) resolves honestly.

Note on the 2020-03-10 date used below: it is the calendar weekday that NSE
observed as the Holi trading holiday in 2020 — but this PR deliberately does
NOT add it to the holiday list (no verified in-repo source for the full 2020
NSE circular). The assertion is the loud-guard verdict, NOT a holiday claim:
because 2020 is out of the published coverage span, the strict path REFUSES to
return a trading-day verdict for that weekday. The date being a real-world
holiday is the motivation for why a silent ``True`` is dangerous; the test
itself only asserts "out of coverage ⇒ loud", which holds for ANY 2020 weekday.
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.calendars.nse import (
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
    HolidayDataUnavailableError,
    is_trading_day,
    is_trading_day_checked,
)
from brutex.indicators.prev_day_ohlc import (
    DailyCandle,
    find_prev_trading_day,
    lookup_prev_day_ohlc,
    lookup_prev_day_ohlc_series,
)
from brutex.indicators.weekly_anchors import rolling_weekly_anchor, weekly_static_anchor


class TestCoverageSpanIsTheSingleSourceOfTruth:
    """The 2020 floor predates the holiday list; the span is derived, not hardcoded."""

    def test_coverage_span_starts_after_the_2020_pull_floor(self) -> None:
        # The data-pull floor is 2020; the holiday list starts 2021 — exactly the
        # gap this PR makes honest. (Derived from the per-year frozensets in nse.py.)
        assert HOLIDAY_COVERAGE_FIRST_YEAR == 2021
        assert HOLIDAY_COVERAGE_LAST_YEAR >= 2026
        assert HOLIDAY_COVERAGE_FIRST_YEAR > 2020


class TestUnguardedStillSilentlyWrongFor2020:
    """Pin the documented back-compat hazard the strict path exists to avoid.

    The UNGUARDED ``is_trading_day`` is intentionally byte-identical to before:
    for a 2020 weekday it returns ``True`` (an absence of holiday data read as a
    trading day). This is the silent-wrong behaviour; the fix is to route the
    anchor consumers through the STRICT variant, NOT to change ``is_trading_day``.
    """

    def test_unguarded_is_trading_day_silently_true_for_2020_weekday(self) -> None:
        # 2020-03-10 is a Tuesday; out of coverage -> unguarded silently says True.
        assert date(2020, 3, 10).weekday() == 1  # Tuesday
        assert is_trading_day(date(2020, 3, 10)) is True  # silent (documented hazard)


class TestStrictTradingDay2020EdgeCases:
    """Every extreme 2020 edge case, asserted through ``is_trading_day_checked``."""

    def test_2020_weekday_out_of_coverage_raises_loud(self) -> None:
        # FAILS on origin/main where the anchor path used the unguarded variant.
        with pytest.raises(HolidayDataUnavailableError):
            is_trading_day_checked(date(2020, 3, 10))  # Tuesday, out of coverage

    def test_2020_weekend_is_not_a_trading_day_without_raising(self) -> None:
        # A weekend is non-trading independent of the (missing) holiday list, so
        # the answer is data-complete: honest False, no raise.
        sat = date(2020, 3, 7)  # Saturday
        sun = date(2020, 3, 8)  # Sunday
        assert sat.weekday() == 5 and sun.weekday() == 6
        assert is_trading_day_checked(sat) is False
        assert is_trading_day_checked(sun) is False

    def test_2020_leap_day_is_saturday_not_a_trading_day(self) -> None:
        # 2020-02-29 is a leap-day Saturday — weekend, honest False, no raise.
        leap = date(2020, 2, 29)
        assert leap.weekday() == 5  # Saturday
        assert is_trading_day_checked(leap) is False

    def test_year_boundary_2019_12_31_weekday_raises(self) -> None:
        # 2019-12-31 is a Tuesday weekday, out of coverage -> loud.
        assert date(2019, 12, 31).weekday() == 1
        with pytest.raises(HolidayDataUnavailableError):
            is_trading_day_checked(date(2019, 12, 31))

    def test_year_boundary_2020_01_01_weekday_raises(self) -> None:
        # 2020-01-01 is a Wednesday weekday, out of coverage -> loud.
        assert date(2020, 1, 1).weekday() == 2
        with pytest.raises(HolidayDataUnavailableError):
            is_trading_day_checked(date(2020, 1, 1))

    def test_uncovered_future_year_weekday_raises(self) -> None:
        # An uncovered FUTURE year is handled identically — never silent-wrong.
        future = date(2030, 6, 3)  # Monday
        assert future.weekday() == 0
        with pytest.raises(HolidayDataUnavailableError):
            is_trading_day_checked(future)


class TestFindPrevTradingDay2020Honest:
    """The shared backward walk is loud for an out-of-coverage 2020 weekday."""

    def test_walk_into_2020_weekday_raises(self) -> None:
        # FAILS on origin/main: there find_prev_trading_day(2020-03-11) silently
        # returned 2020-03-10 (a real NSE holiday) as the prior trading day.
        with pytest.raises(HolidayDataUnavailableError):
            find_prev_trading_day(date(2020, 3, 11))

    def test_2021_lookup_does_not_walk_into_2020(self) -> None:
        # Regression: the first trading day of 2021 (Fri 2021-01-01, in coverage,
        # NSE traded) is returned WITHOUT touching 2020 — no spurious raise.
        assert find_prev_trading_day(date(2021, 1, 4)) == date(2021, 1, 1)

    def test_2026_in_coverage_unchanged(self) -> None:
        # Regression: 2021-2026 behaviour byte-identical to before the fix.
        assert find_prev_trading_day(date(2026, 5, 12)) == date(2026, 5, 11)
        assert find_prev_trading_day(date(2026, 5, 11)) == date(2026, 5, 8)


class TestPrevDayOHLC2020Honest:
    """``lookup_prev_day_ohlc`` never silently uses a 2020 non-trading-day."""

    def test_today_2020_weekday_raises_loud(self) -> None:
        with pytest.raises(HolidayDataUnavailableError):
            lookup_prev_day_ohlc(date(2020, 3, 11), {})

    def test_prev_walk_across_2020_holiday_raises(self) -> None:
        # today is in-coverage 2021 but the available daily candles only go back
        # into 2020 — the backward walk into a 2020 weekday is loud, NOT a silent
        # holiday-as-prev-day. (A non-trading day is never used as 'prev day'.)
        # Use a today that has no in-coverage prior trading day within lookback by
        # walking from the very first 2021 session backwards.
        with pytest.raises(HolidayDataUnavailableError):
            # 2021-01-01 (Fri) is the first in-coverage session; its prior walk
            # immediately reaches 2020-12-31 (Thu weekday, out of coverage).
            lookup_prev_day_ohlc(date(2021, 1, 1), {})

    def test_in_coverage_lookup_unchanged(self) -> None:
        # Regression through the real path with a provided daily dict.
        daily = {
            date(2026, 5, 11): DailyCandle(
                day=date(2026, 5, 11),
                open_paisa=100_00,
                high_paisa=110_00,
                low_paisa=95_00,
                close_paisa=108_00,
            )
        }
        result = lookup_prev_day_ohlc(date(2026, 5, 12), daily)
        assert result is not None
        assert result.prev_day == date(2026, 5, 11)
        assert result.pdh_paisa == 110_00

    def test_series_in_coverage_unchanged(self) -> None:
        daily = {
            date(2026, 5, 11): DailyCandle(
                day=date(2026, 5, 11),
                open_paisa=1,
                high_paisa=2,
                low_paisa=0,
                close_paisa=1,
            )
        }
        out = lookup_prev_day_ohlc_series([date(2026, 5, 12)], daily)
        assert date(2026, 5, 12) in out


class TestWeeklyAnchors2020Honest:
    """Weekly Fib anchors are loud across a 2020 holiday week, unchanged for 2021+."""

    def test_rolling_weekly_today_2020_raises(self) -> None:
        with pytest.raises(HolidayDataUnavailableError):
            rolling_weekly_anchor(date(2020, 3, 11), {})

    def test_weekly_static_today_2020_raises(self) -> None:
        with pytest.raises(HolidayDataUnavailableError):
            weekly_static_anchor(date(2020, 3, 11), {})

    def test_rolling_weekly_walk_into_2020_raises(self) -> None:
        # In-coverage today, but the trailing-5 walk crosses into 2020 -> loud.
        with pytest.raises(HolidayDataUnavailableError):
            rolling_weekly_anchor(date(2021, 1, 1), {})

    def test_weekly_static_walk_into_2020_raises(self) -> None:
        with pytest.raises(HolidayDataUnavailableError):
            weekly_static_anchor(date(2021, 1, 1), {})

    def test_rolling_weekly_in_coverage_unchanged(self) -> None:
        # Regression: a normal 2026 week with 5 prior daily candles is warm.
        days = [date(2026, 5, d) for d in (4, 5, 6, 7, 8)]  # Mon-Fri
        daily = {
            d: DailyCandle(day=d, open_paisa=1, high_paisa=10 + i, low_paisa=1, close_paisa=5)
            for i, d in enumerate(days)
        }
        anchor = rolling_weekly_anchor(date(2026, 5, 11), daily, sessions=5)
        assert anchor is not None
        assert anchor.high_paisa == 14  # 10 + 4
        assert anchor.low_paisa == 1

    def test_weekly_static_in_coverage_unchanged(self) -> None:
        days = [date(2026, 5, d) for d in (4, 5, 6, 7, 8)]  # prior ISO week
        daily = {
            d: DailyCandle(day=d, open_paisa=1, high_paisa=20, low_paisa=2, close_paisa=5)
            for d in days
        }
        anchor = weekly_static_anchor(date(2026, 5, 11), daily)  # Mon of next week
        assert anchor is not None
        assert anchor.high_paisa == 20
        assert anchor.low_paisa == 2
