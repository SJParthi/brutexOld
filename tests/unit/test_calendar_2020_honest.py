"""DEC-CALENDAR-2020-HONEST-001 + DEC-CALENDAR-2020-DATA-001 — honest
out-of-coverage trading-day handling at the 2019/2020 boundary.

The Track-1 data-pull floor is ``2020-01-01`` (``GROWW_HISTORY_FLOOR``,
CLAUDE.md §3). DEC-CALENDAR-2020-HONEST-001 first wired the spot-anchor
consumers ``prev_day_ohlc`` + ``weekly_anchors`` to the STRICT
``is_trading_day_checked`` so an out-of-coverage weekday raised
``HolidayDataUnavailableError`` instead of being SILENTLY read as a trading day.
At that point 2020 was OUT of coverage (the list started 2021), so a 2020 sweep
hit the loud-guard and refused to run.

DEC-CALENDAR-2020-DATA-001 ADDS the verified NSE/BSE 2020 holiday list (+ the
2020 Diwali Muhurat session). This module's PREMISE therefore INVERTS:

* the coverage span now STARTS in 2020 — ``HOLIDAY_COVERAGE_FIRST_YEAR == 2020``;
* 2020 weekdays now resolve HONESTLY (a 2020 holiday is False, a 2020 regular
  weekday is True) — the cases that used to raise now SUCCEED;
* the loud-guard moves DOWN to the NEW boundary: a PRE-2020 (2019 and earlier)
  weekday is the genuinely-ambiguous out-of-coverage case that still raises.

We still do NOT fabricate missing holiday dates (Golden Rule #1): the loud-guard
remains the honest answer for any year outside the published span. These tests
drive the REAL production functions (``find_prev_trading_day`` /
``lookup_prev_day_ohlc`` / ``rolling_weekly_anchor`` / ``weekly_static_anchor``
and the underlying ``is_trading_day_checked``) and assert the honest behaviour at
the new 2019/2020 boundary.
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.calendars.nse import (
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
    HolidayDataUnavailableError,
    is_holiday,
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
    """The 2020 floor is NOW inside the holiday list; the span is derived, not hardcoded."""

    def test_coverage_span_starts_at_the_2020_pull_floor(self) -> None:
        # The data-pull floor is 2020 and the holiday list now starts 2020 too —
        # the gap this PR closes. (Derived from the per-year frozensets in nse.py.)
        assert HOLIDAY_COVERAGE_FIRST_YEAR == 2020
        assert HOLIDAY_COVERAGE_LAST_YEAR >= 2026
        assert HOLIDAY_COVERAGE_FIRST_YEAR <= 2020


class TestUnguardedStillSilentlyWrongForPre2020:
    """Pin the documented back-compat hazard the strict path exists to avoid.

    The UNGUARDED ``is_trading_day`` is intentionally byte-identical to before:
    for an out-of-coverage (now PRE-2020) weekday it returns ``True`` (an absence
    of holiday data read as a trading day). This is the silent-wrong behaviour;
    the fix routes the anchor consumers through the STRICT variant, NOT a change
    to ``is_trading_day``.
    """

    def test_unguarded_is_trading_day_silently_true_for_2019_weekday(self) -> None:
        # 2019-12-31 is a Tuesday; out of coverage -> unguarded silently says True.
        assert date(2019, 12, 31).weekday() == 1  # Tuesday
        assert is_trading_day(date(2019, 12, 31)) is True  # silent (documented hazard)


class TestStrict2020NowResolvesHonestly:
    """The cases that USED to raise (2020 weekdays) now resolve — the gap is closed."""

    def test_2020_regular_weekday_is_now_a_trading_day(self) -> None:
        # 2020-03-11 is a Wednesday, NOT a listed holiday -> honest True, no raise
        # (this RAISED before DEC-CALENDAR-2020-DATA-001 added the 2020 list).
        assert date(2020, 3, 11).weekday() == 2  # Wednesday
        assert is_trading_day_checked(date(2020, 3, 11)) is True

    def test_2020_listed_holiday_is_not_a_trading_day(self) -> None:
        # 2020-03-10 (Tue) is the verified 2020 Holi holiday -> honest False.
        assert date(2020, 3, 10).weekday() == 1  # Tuesday
        assert is_holiday(date(2020, 3, 10)) is True
        assert is_trading_day_checked(date(2020, 3, 10)) is False

    def test_2020_weekend_is_not_a_trading_day_without_raising(self) -> None:
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

    def test_2020_year_boundary_jan_first_is_now_in_coverage(self) -> None:
        # 2020-01-01 is a Wednesday; previously out of coverage (raised), now a
        # covered regular weekday (not a listed holiday) -> honest True.
        assert date(2020, 1, 1).weekday() == 2  # Wednesday
        assert is_trading_day_checked(date(2020, 1, 1)) is True


class TestLoudGuardMovesToPre2020Boundary:
    """The loud-guard now bites at the NEW 2019/2020 boundary, not at 2020."""

    def test_2019_weekday_out_of_coverage_raises_loud(self) -> None:
        # 2019-12-31 is a Tuesday weekday, out of coverage -> loud (was 2020 before).
        assert date(2019, 12, 31).weekday() == 1
        with pytest.raises(HolidayDataUnavailableError):
            is_trading_day_checked(date(2019, 12, 31))

    def test_2019_year_boundary_dec_30_weekday_raises(self) -> None:
        # 2019-12-30 is a Monday weekday, out of coverage -> loud.
        assert date(2019, 12, 30).weekday() == 0
        with pytest.raises(HolidayDataUnavailableError):
            is_trading_day_checked(date(2019, 12, 30))

    def test_2019_weekend_is_not_a_trading_day_without_raising(self) -> None:
        # A weekend is non-trading independent of the (missing) holiday list, so
        # the answer is data-complete even out of span: honest False, no raise.
        sat = date(2019, 12, 28)  # Saturday
        assert sat.weekday() == 5
        assert is_trading_day_checked(sat) is False

    def test_uncovered_future_year_weekday_raises(self) -> None:
        # An uncovered FUTURE year is handled identically — never silent-wrong.
        future = date(2030, 6, 3)  # Monday
        assert future.weekday() == 0
        with pytest.raises(HolidayDataUnavailableError):
            is_trading_day_checked(future)


class TestFindPrevTradingDay2020Honest:
    """The shared backward walk SUCCEEDS inside 2020, is loud only crossing into 2019."""

    def test_walk_into_2020_succeeds_skipping_the_holi_holiday(self) -> None:
        # find_prev_trading_day(2020-03-11 Wed) skips the 2020 Holi holiday
        # (2020-03-10 Tue) and the prior session is 2020-03-09 (Mon) — SUCCESS.
        # (This RAISED before the 2020 list was added.)
        assert find_prev_trading_day(date(2020, 3, 11)) == date(2020, 3, 9)

    def test_walk_from_2021_into_2020_now_succeeds(self) -> None:
        # 2021-01-01 (Fri) is the first 2021 session; its prior trading day is
        # 2020-12-31 (Thu), now IN coverage and not a listed holiday -> returned.
        assert find_prev_trading_day(date(2021, 1, 1)) == date(2020, 12, 31)

    def test_walk_from_early_2020_into_2019_raises(self) -> None:
        # 2020-01-02 (Thu) is the first usable 2020 weekday; walking strictly
        # before it crosses 2020-01-01 (Wed, trading) — so it RETURNS that, not a
        # raise. Use 2020-01-01 itself to force the walk into 2019: its prior day
        # is 2019-12-31 (Tue weekday, out of coverage) -> loud.
        with pytest.raises(HolidayDataUnavailableError):
            find_prev_trading_day(date(2020, 1, 1))

    def test_2026_in_coverage_unchanged(self) -> None:
        # Regression: 2021-2026 behaviour byte-identical to before the change.
        assert find_prev_trading_day(date(2026, 5, 12)) == date(2026, 5, 11)
        assert find_prev_trading_day(date(2026, 5, 11)) == date(2026, 5, 8)


class TestPrevDayOHLC2020Honest:
    """``lookup_prev_day_ohlc`` resolves real 2020 prior days; loud only into 2019."""

    def test_real_2020_lookup_returns_prior_session(self) -> None:
        # today 2020-03-11 (Wed); prior trading day is 2020-03-09 (Mon, skipping
        # the Holi holiday). With its candle provided the lookup SUCCEEDS.
        daily = {
            date(2020, 3, 9): DailyCandle(
                day=date(2020, 3, 9),
                open_paisa=100_00,
                high_paisa=110_00,
                low_paisa=95_00,
                close_paisa=108_00,
            )
        }
        result = lookup_prev_day_ohlc(date(2020, 3, 11), daily)
        assert result is not None
        assert result.prev_day == date(2020, 3, 9)
        assert result.pdh_paisa == 110_00

    def test_walk_from_2021_into_2020_returns_none_when_candle_missing(self) -> None:
        # today 2021-01-01 (Fri, in coverage); prior trading day 2020-12-31 is now
        # IN coverage so NO raise — but the empty dict means its candle is missing,
        # so the lookup honestly returns None (NOT a holiday-as-prev-day).
        assert lookup_prev_day_ohlc(date(2021, 1, 1), {}) is None

    def test_today_2020_01_01_walk_into_2019_raises_loud(self) -> None:
        # today 2020-01-01 (Wed, in coverage, trading); its prior-day walk reaches
        # 2019-12-31 (out of coverage weekday) -> loud.
        with pytest.raises(HolidayDataUnavailableError):
            lookup_prev_day_ohlc(date(2020, 1, 1), {})

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
    """Weekly Fib anchors compute inside 2020, are loud only walking into 2019."""

    def test_rolling_weekly_real_2020_week_is_warm(self) -> None:
        # A normal 2020 week (Mon-Fri) with 5 prior daily candles is warm and
        # returns the H/L anchor — this is the 2020 SUCCESS path.
        days = [date(2020, 6, d) for d in (8, 9, 10, 11, 12)]  # Mon-Fri
        daily = {
            d: DailyCandle(day=d, open_paisa=1, high_paisa=10 + i, low_paisa=1, close_paisa=5)
            for i, d in enumerate(days)
        }
        anchor = rolling_weekly_anchor(date(2020, 6, 15), daily, sessions=5)  # next Mon
        assert anchor is not None
        assert anchor.high_paisa == 14  # 10 + 4
        assert anchor.low_paisa == 1

    def test_weekly_static_real_2020_week_is_warm(self) -> None:
        days = [date(2020, 6, d) for d in (8, 9, 10, 11, 12)]  # prior ISO week
        daily = {
            d: DailyCandle(day=d, open_paisa=1, high_paisa=20, low_paisa=2, close_paisa=5)
            for d in days
        }
        anchor = weekly_static_anchor(date(2020, 6, 15), daily)  # Mon of next week
        assert anchor is not None
        assert anchor.high_paisa == 20
        assert anchor.low_paisa == 2

    def test_rolling_weekly_today_2019_raises(self) -> None:
        # today out of coverage (2019 weekday) -> loud before any window walk.
        assert date(2019, 12, 31).weekday() == 1
        with pytest.raises(HolidayDataUnavailableError):
            rolling_weekly_anchor(date(2019, 12, 31), {})

    def test_weekly_static_today_2019_raises(self) -> None:
        with pytest.raises(HolidayDataUnavailableError):
            weekly_static_anchor(date(2019, 12, 31), {})

    def test_rolling_weekly_walk_from_early_2020_into_2019_raises(self) -> None:
        # In-coverage 2020 today, but the trailing-5 walk (empty dict) crosses the
        # 2019/2020 boundary into a 2019 weekday -> loud.
        with pytest.raises(HolidayDataUnavailableError):
            rolling_weekly_anchor(date(2020, 1, 6), {})

    def test_weekly_static_walk_from_early_2020_into_2019_raises(self) -> None:
        with pytest.raises(HolidayDataUnavailableError):
            weekly_static_anchor(date(2020, 1, 6), {})

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
