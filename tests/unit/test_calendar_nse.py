"""Tests for `brutex.calendars.nse` — NSE/BSE Indian market calendar.

Covers:
- Holiday detection 2021-2026
- Weekend detection
- Trading-day filtering
- Muhurat session lookup
- Session minutes and per-TF bar count
- Forced 15:20 IST exit rule
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from brutex.calendars.nse import (
    ALL_HOLIDAYS,
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
    IST,
    SESSION_CLOSE_IST,
    SESSION_DURATION_MINUTES,
    SESSION_OPEN_IST,
    TICK_SIZE_PAISA,
    HolidayDataUnavailableError,
    SessionType,
    expected_bars_per_day_for_tf,
    get_session,
    has_muhurat_session,
    is_forced_exit_bar,
    is_holiday,
    is_holiday_checked,
    is_holiday_data_available,
    is_pre_open,
    is_trading_day,
    is_trading_day_checked,
    is_weekend,
    session_minutes_per_day,
    trading_days_between,
)


class TestHolidays:
    def test_republic_day_2024_is_holiday(self) -> None:
        assert is_holiday(date(2024, 1, 26))

    def test_independence_day_2024_is_holiday(self) -> None:
        assert is_holiday(date(2024, 8, 15))

    def test_diwali_muhurat_2024_is_holiday(self) -> None:
        # 2024-11-01 is Diwali Laxmi Pujan — listed as holiday with Muhurat
        # special session in the evening.
        assert is_holiday(date(2024, 11, 1))
        assert has_muhurat_session(date(2024, 11, 1))

    def test_random_weekday_not_holiday(self) -> None:
        # Tuesday 2024-02-20 — no known holiday
        assert not is_holiday(date(2024, 2, 20))

    def test_holiday_list_spans_2021_2026(self) -> None:
        # At least one holiday per year
        from brutex.calendars.nse import (
            HOLIDAYS_2021,
            HOLIDAYS_2022,
            HOLIDAYS_2023,
            HOLIDAYS_2024,
            HOLIDAYS_2025,
            HOLIDAYS_2026,
        )

        for hols in [
            HOLIDAYS_2021,
            HOLIDAYS_2022,
            HOLIDAYS_2023,
            HOLIDAYS_2024,
            HOLIDAYS_2025,
            HOLIDAYS_2026,
        ]:
            assert len(hols) >= 1


class TestWeekend:
    def test_saturday_is_weekend(self) -> None:
        assert is_weekend(date(2024, 5, 11))  # Saturday

    def test_sunday_is_weekend(self) -> None:
        assert is_weekend(date(2024, 5, 12))  # Sunday

    def test_monday_is_not_weekend(self) -> None:
        assert not is_weekend(date(2024, 5, 13))


class TestTradingDay:
    def test_regular_tuesday_is_trading_day(self) -> None:
        # Tuesday 2024-02-20
        assert is_trading_day(date(2024, 2, 20))

    def test_holiday_is_not_trading_day(self) -> None:
        assert not is_trading_day(date(2024, 1, 26))  # Republic Day

    def test_weekend_is_not_trading_day(self) -> None:
        assert not is_trading_day(date(2024, 5, 11))  # Saturday


class TestSession:
    def test_regular_session_lookup(self) -> None:
        session = get_session(date(2024, 2, 20))  # Tuesday
        assert session is not None
        assert session.session_type == SessionType.REGULAR
        assert session.open_time_ist == time(9, 15)
        assert session.close_time_ist == time(15, 30)
        assert session.duration_minutes == 375

    def test_holiday_no_session(self) -> None:
        # Republic Day, no muhurat
        assert get_session(date(2024, 1, 26)) is None

    def test_muhurat_session_lookup(self) -> None:
        session = get_session(date(2024, 11, 1))  # Diwali 2024
        assert session is not None
        assert session.session_type == SessionType.MUHURAT
        assert session.duration_minutes == 60

    def test_weekend_no_session(self) -> None:
        assert get_session(date(2024, 5, 11)) is None  # Saturday


class TestTradingDaysBetween:
    def test_one_week_excludes_weekend(self) -> None:
        # Mon 2024-02-19 to Sun 2024-02-25 → 5 trading days
        days = trading_days_between(date(2024, 2, 19), date(2024, 2, 25))
        assert len(days) == 5
        assert all(d.weekday() < 5 for d in days)

    def test_excludes_holiday(self) -> None:
        # Jan 22-28 2024 includes Republic Day Jan 26 (Fri)
        days = trading_days_between(date(2024, 1, 22), date(2024, 1, 28))
        assert date(2024, 1, 26) not in days

    def test_empty_range_returns_empty(self) -> None:
        # Start > end (single day, but that day is a weekend)
        assert trading_days_between(date(2024, 5, 11), date(2024, 5, 11)) == []


class TestExpectedBarsPerDay:
    def test_session_is_375_minutes(self) -> None:
        assert session_minutes_per_day() == SESSION_DURATION_MINUTES == 375

    @pytest.mark.parametrize(
        "tf_minutes,expected_bars",
        [
            (1, 375),
            (2, 187),
            (5, 75),
            (15, 25),
            (30, 12),
            (60, 6),
            (375, 1),  # full session = 1 bar
            (1440, 1),  # 1d → 1 bar
        ],
    )
    def test_expected_bars(self, tf_minutes: int, expected_bars: int) -> None:
        assert expected_bars_per_day_for_tf(tf_minutes) == expected_bars


class TestForcedExit:
    def test_at_1520_is_forced_exit(self) -> None:
        assert is_forced_exit_bar(time(15, 20), tf_minutes=1)

    def test_after_1520_is_forced_exit(self) -> None:
        assert is_forced_exit_bar(time(15, 25), tf_minutes=5)

    def test_before_1520_is_not_forced_exit(self) -> None:
        assert not is_forced_exit_bar(time(15, 19), tf_minutes=1)
        assert not is_forced_exit_bar(time(14, 0), tf_minutes=1)


class TestForcedExitTimeframeAware:
    """Regression for the ``is_forced_exit_bar`` tf-blindness bug (DEC-TRADESIM-001).

    A bar covers ``[open, open + tf_minutes)``; it must force-exit when that
    window reaches 15:20 IST. The old ``t >= 15:20`` test ignored ``tf_minutes``
    and silently missed a coarse-TF bar whose *span* straddles 15:20.
    """

    def test_15m_bar_straddling_1520_is_forced(self) -> None:
        # 15:15 + 15m = 15:30 > 15:20 → forced. The old tf-blind check returned
        # False here (15:15 < 15:20) — this is the bug the fix closes.
        assert is_forced_exit_bar(time(15, 15), tf_minutes=15)

    def test_same_open_time_differs_by_timeframe(self) -> None:
        # 15:10 is NOT forced at 5m (ends 15:15) but IS forced at 15m (ends 15:25).
        assert not is_forced_exit_bar(time(15, 10), tf_minutes=5)
        assert is_forced_exit_bar(time(15, 10), tf_minutes=15)

    def test_5m_bar_ending_exactly_at_1520_is_not_forced(self) -> None:
        # 15:15 + 5m = 15:20 (the window's exclusive end) → NOT yet forced.
        assert not is_forced_exit_bar(time(15, 15), tf_minutes=5)

    def test_5m_bar_ending_after_1520_is_forced(self) -> None:
        # 15:16 + 5m = 15:21 > 15:20 → forced.
        assert is_forced_exit_bar(time(15, 16), tf_minutes=5)

    def test_1m_behaviour_matches_legacy_boundary(self) -> None:
        # For 1-minute bars the fix is identical to the old `t >= 15:20` test.
        assert is_forced_exit_bar(time(15, 20), tf_minutes=1)
        assert not is_forced_exit_bar(time(15, 19), tf_minutes=1)

    def test_seconds_in_open_time_are_honoured(self) -> None:
        # 15:19:30 + 1m = 15:20:30 > 15:20:00 → forced (sub-minute open times).
        assert is_forced_exit_bar(time(15, 19, 30), tf_minutes=1)

    def test_daily_bar_is_forced(self) -> None:
        # A 1-day bar (1440m) trivially spans 15:20.
        assert is_forced_exit_bar(time(9, 15), tf_minutes=1440)

    def test_invalid_timeframe_raises(self) -> None:
        with pytest.raises(ValueError, match="tf_minutes must be >= 1"):
            is_forced_exit_bar(time(15, 20), tf_minutes=0)


class TestPreOpen:
    def test_0900_is_pre_open(self) -> None:
        assert is_pre_open(time(9, 0))

    def test_0914_is_pre_open(self) -> None:
        assert is_pre_open(time(9, 14))

    def test_0915_is_not_pre_open(self) -> None:
        # First usable bar starts at session open
        assert not is_pre_open(time(9, 15))

    def test_0830_is_not_pre_open(self) -> None:
        # Earlier than pre-open auction window
        assert not is_pre_open(time(8, 30))


class TestConstants:
    def test_tick_size_is_5_paisa(self) -> None:
        """Tick size locked at paisa 5 (Rs 0.05) throughout 2021-2026."""
        assert TICK_SIZE_PAISA == 5


class TestNiftyRebalanceEvents:
    """DEC-P0-046 / Q-09: NIFTY 50 semi-annual rebalance dates 2021-2025."""

    def test_event_count_covers_semi_annual_2021_2025(self) -> None:
        from brutex.calendars.nse import NIFTY50_REBALANCE_EVENTS

        # 5 years × 2 events/year = 10 events covering March 2021 → Sep 2025.
        assert len(NIFTY50_REBALANCE_EVENTS) == 10

    def test_effective_dates_are_monotonic_strictly_increasing(self) -> None:
        from brutex.calendars.nse import NIFTY50_REBALANCE_EVENTS

        dates = [event.effective_date for event in NIFTY50_REBALANCE_EVENTS]
        assert dates == sorted(dates)
        assert len(set(dates)) == len(dates)

    def test_at_least_nine_events_verified(self) -> None:
        """Honest envelope: 9 of 10 events should have verified=True."""
        from brutex.calendars.nse import NIFTY50_REBALANCE_EVENTS

        verified = [e for e in NIFTY50_REBALANCE_EVENTS if e.verified]
        assert len(verified) >= 9

    def test_is_nifty_rebalance_day_recognises_march_2024(self) -> None:
        from brutex.calendars.nse import is_nifty_rebalance_day

        assert is_nifty_rebalance_day(date(2024, 3, 28))

    def test_is_nifty_rebalance_day_rejects_regular_session(self) -> None:
        from brutex.calendars.nse import is_nifty_rebalance_day

        # 2024-02-20 is a Tuesday with no rebalance event scheduled.
        assert not is_nifty_rebalance_day(date(2024, 2, 20))

    def test_rebalance_event_on_returns_shriram_finance_march_2024(self) -> None:
        from brutex.calendars.nse import nifty_rebalance_event_on

        event = nifty_rebalance_event_on(date(2024, 3, 28))
        assert event is not None
        assert "SHRIRAMFIN" in event.added
        assert "UPL" in event.removed
        assert event.verified is True

    def test_rebalance_event_on_returns_none_for_non_rebalance_day(self) -> None:
        from brutex.calendars.nse import nifty_rebalance_event_on

        assert nifty_rebalance_event_on(date(2024, 2, 20)) is None

    def test_march_2025_rebalance_records_zomato_jio_inclusion(self) -> None:
        from brutex.calendars.nse import nifty_rebalance_event_on

        event = nifty_rebalance_event_on(date(2025, 3, 28))
        assert event is not None
        # Zomato post-rename ticker = ETERNAL (NSE corporate-action 2025).
        assert "ETERNAL" in event.added
        assert "JIOFIN" in event.added
        assert "BPCL" in event.removed

    def test_september_2025_rebalance_records_indigo_max_inclusion(self) -> None:
        from brutex.calendars.nse import nifty_rebalance_event_on

        event = nifty_rebalance_event_on(date(2025, 9, 30))
        assert event is not None
        assert "INDIGO" in event.added
        assert "MAXHEALTH" in event.added
        assert "HEROMOTOCO" in event.removed
        assert "INDUSINDBK" in event.removed


class TestSpecialWeekendSessions:
    """DEC-P0-046 / Q-09: Budget Day Saturday/Sunday full sessions."""

    def test_budget_day_2025_is_special_weekend_session(self) -> None:
        from brutex.calendars.nse import is_special_weekend_session

        # 2025-02-01 was a Saturday — Union Budget 2025-26 special session.
        assert is_special_weekend_session(date(2025, 2, 1))

    def test_budget_day_2026_is_special_weekend_session(self) -> None:
        from brutex.calendars.nse import is_special_weekend_session

        # 2026-02-01 is a Sunday — Union Budget 2026-27 special session.
        assert is_special_weekend_session(date(2026, 2, 1))

    def test_regular_saturday_is_not_special(self) -> None:
        from brutex.calendars.nse import is_special_weekend_session

        # 2024-05-11 was a regular Saturday — no special session.
        assert not is_special_weekend_session(date(2024, 5, 11))

    def test_budget_day_promotes_saturday_to_trading_day(self) -> None:
        # Special weekend session must flip is_trading_day → True even
        # though is_weekend stays True.
        d = date(2025, 2, 1)
        assert is_weekend(d)
        assert is_trading_day(d)

    def test_budget_day_get_session_returns_regular_hours(self) -> None:
        # Full 09:15-15:30 hours classified as REGULAR (not HALF_DAY).
        session = get_session(date(2025, 2, 1))
        assert session is not None
        assert session.session_type == SessionType.REGULAR
        assert session.open_time_ist == time(9, 15)
        assert session.close_time_ist == time(15, 30)
        assert session.duration_minutes == 375

    def test_special_weekend_does_not_conflict_with_holiday_list(self) -> None:
        # 2025-02-01 must NOT be in ALL_HOLIDAYS (it is a trading day).
        from brutex.calendars.nse import ALL_HOLIDAYS

        assert date(2025, 2, 1) not in ALL_HOLIDAYS


class TestISTOffset:
    """IST is a FIXED +05:30 offset with NO daylight-saving transition."""

    @pytest.mark.parametrize(
        "instant",
        [
            datetime(2020, 1, 1, 0, 0),  # winter
            datetime(2024, 6, 21, 12, 0),  # midsummer (would shift under DST)
            datetime(2024, 12, 21, 12, 0),  # midwinter
            datetime(2025, 3, 9, 2, 30),  # US "spring forward" instant
            datetime(2025, 11, 2, 1, 30),  # US "fall back" instant
        ],
    )
    def test_offset_is_always_5h30m(self, instant: datetime) -> None:
        aware = instant.replace(tzinfo=IST)
        assert aware.utcoffset() == timedelta(hours=5, minutes=30)
        # No DST component, ever.
        assert aware.dst() == timedelta(0)

    def test_offset_constant_across_year(self) -> None:
        # Sample one instant per month; the offset must never move.
        offsets = {datetime(2024, m, 15, 9, 15, tzinfo=IST).utcoffset() for m in range(1, 13)}
        assert offsets == {timedelta(hours=5, minutes=30)}


class TestHolidayCoverageRange:
    """DEC-CALENDAR-HARDEN-001: detection primitives for out-of-coverage dates.

    The hardcoded holiday list starts in 2021 but the Track-1 data pull floor
    is 2020-01-01 (CLAUDE.md §3 / MIGRATION.md #722). A 2020 weekday holiday
    STILL reads as a trading day via the unguarded path (back-compat); these
    NEW opt-in primitives let a caller DETECT the gap and refuse to guess.
    """

    def test_coverage_range_derived_from_list(self) -> None:
        assert min(h.year for h in ALL_HOLIDAYS) == HOLIDAY_COVERAGE_FIRST_YEAR
        assert max(h.year for h in ALL_HOLIDAYS) == HOLIDAY_COVERAGE_LAST_YEAR
        assert HOLIDAY_COVERAGE_FIRST_YEAR <= HOLIDAY_COVERAGE_LAST_YEAR

    def test_in_coverage_dates_available(self) -> None:
        assert is_holiday_data_available(date(HOLIDAY_COVERAGE_FIRST_YEAR, 1, 1))
        assert is_holiday_data_available(date(HOLIDAY_COVERAGE_LAST_YEAR, 12, 31))
        assert is_holiday_data_available(date(2024, 6, 1))

    def test_below_floor_2020_not_available(self) -> None:
        # 2020 is IN the data-pull scope but OUT of the holiday list.
        assert not is_holiday_data_available(date(2020, 3, 10))

    def test_above_ceiling_2027_not_available(self) -> None:
        assert not is_holiday_data_available(date(HOLIDAY_COVERAGE_LAST_YEAR + 1, 1, 26))

    def test_is_holiday_in_range_unchanged(self) -> None:
        # Within coverage, is_holiday and is_holiday_checked agree exactly.
        assert is_holiday(date(2024, 1, 26)) is True
        assert is_holiday_checked(date(2024, 1, 26)) is True
        assert is_holiday(date(2024, 2, 20)) is False
        assert is_holiday_checked(date(2024, 2, 20)) is False

    def test_is_holiday_out_of_range_returns_false_silently(self) -> None:
        # Documented behaviour: is_holiday returns False (absence of data),
        # NOT a verified trading day. The checked variant is the honest path.
        assert is_holiday(date(2020, 3, 10)) is False

    def test_is_holiday_checked_raises_below_floor(self) -> None:
        with pytest.raises(HolidayDataUnavailableError, match="2020-03-10"):
            is_holiday_checked(date(2020, 3, 10))

    def test_is_holiday_checked_raises_above_ceiling(self) -> None:
        with pytest.raises(HolidayDataUnavailableError, match="covered span"):
            is_holiday_checked(date(HOLIDAY_COVERAGE_LAST_YEAR + 1, 1, 26))

    def test_error_is_lookup_error_subclass(self) -> None:
        assert issubclass(HolidayDataUnavailableError, LookupError)


class TestTradingDayChecked:
    """DEC-CALENDAR-HARDEN-001: strict trading-day verdict refuses unknown dates."""

    def test_in_range_matches_unchecked(self) -> None:
        assert is_trading_day_checked(date(2024, 2, 20)) is True  # Tuesday
        assert is_trading_day_checked(date(2024, 1, 26)) is False  # Republic Day
        assert is_trading_day_checked(date(2024, 5, 11)) is False  # Saturday

    def test_out_of_range_weekday_raises(self) -> None:
        # Holi 2020-03-10 (Tuesday): the UNGUARDED is_trading_day still returns
        # True (back-compat, unchanged) — the NEW strict variant DETECTS the
        # out-of-coverage case and refuses to guess instead.
        assert is_trading_day(date(2020, 3, 10)) is True
        with pytest.raises(HolidayDataUnavailableError):
            is_trading_day_checked(date(2020, 3, 10))

    def test_out_of_range_weekend_resolves_false_without_raising(self) -> None:
        # A weekend is non-trading INDEPENDENT of the holiday list, so its
        # verdict consults no missing data and is honest even out of coverage.
        # 2020-01-04 (below floor) and 2027-01-02 (above ceiling) are Saturdays.
        assert is_trading_day_checked(date(2020, 1, 4)) is False
        assert is_trading_day_checked(date(2027, 1, 2)) is False

    def test_out_of_range_sunday_resolves_false_without_raising(self) -> None:
        # 2020-01-05 is a Sunday — same data-complete contract.
        assert is_trading_day_checked(date(2020, 1, 5)) is False

    def test_special_weekend_session_overrides_even_out_of_coverage(self) -> None:
        # A date the package DOES hold data for resolves without raising even
        # if its year is outside the holiday span. (Currently every special
        # session is in-coverage; this guards the precedence contract.)
        d = date(2025, 2, 1)  # Budget-day Saturday, in coverage
        assert is_trading_day_checked(d) is True


class TestSessionBoundaries:
    """Exact session boundary timestamps (09:15:00 / 15:30:00 / 15:20 / pre-open)."""

    def test_open_is_0915_00(self) -> None:
        assert time(9, 15, 0) == SESSION_OPEN_IST

    def test_close_is_1530_00(self) -> None:
        assert time(15, 30, 0) == SESSION_CLOSE_IST

    def test_0914_59_is_pre_open(self) -> None:
        assert is_pre_open(time(9, 14, 59))

    def test_0915_00_is_not_pre_open(self) -> None:
        # Open is inclusive of the session, exclusive of pre-open.
        assert not is_pre_open(time(9, 15, 0))

    def test_forced_exit_boundary_at_1519_59(self) -> None:
        # A 1m bar opening 15:19:59 closes 15:20:59 > 15:20 → forced.
        assert is_forced_exit_bar(time(15, 19, 59), tf_minutes=1)

    def test_forced_exit_1m_bar_opening_exactly_1519_not_forced(self) -> None:
        # 15:19:00 + 1m = 15:20:00 (exclusive end) → NOT yet forced.
        assert not is_forced_exit_bar(time(15, 19, 0), tf_minutes=1)


class TestLeapYear:
    """Feb 29 on leap years is handled as an ordinary calendar date."""

    def test_feb29_2020_is_valid_date(self) -> None:
        # 2020-02-29 (Saturday) — weekend, not in holiday coverage.
        d = date(2020, 2, 29)
        assert is_weekend(d)
        assert not is_holiday_data_available(d)

    def test_feb29_2024_is_trading_day(self) -> None:
        # 2024-02-29 is a Thursday, no holiday → trading day.
        d = date(2024, 2, 29)
        assert d.weekday() == 3
        assert is_trading_day(d)
        assert is_trading_day_checked(d)

    def test_trading_days_between_spans_leap_day(self) -> None:
        days = trading_days_between(date(2024, 2, 28), date(2024, 3, 1))
        # Wed 28, Thu 29, Fri 1 — all weekday trading days.
        assert date(2024, 2, 29) in days
        assert len(days) == 3
