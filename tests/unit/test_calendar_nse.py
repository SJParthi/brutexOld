"""Tests for `brutex.calendars.nse` — NSE/BSE Indian market calendar.

Covers:
- Holiday detection 2020-2026
- Weekend detection
- Trading-day filtering
- Muhurat session lookup
- Session minutes and per-TF bar count
- Forced 15:20 IST exit rule
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import ClassVar

import pytest
from brutex.calendars.nse import (
    ALL_HOLIDAYS,
    FORCED_EXIT_IST,
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
    IST,
    MUHURAT_SESSIONS,
    SESSION_CLOSE_IST,
    SESSION_DURATION_MINUTES,
    SESSION_OPEN_IST,
    SPECIAL_WEEKEND_TRADING_SESSIONS,
    TICK_SIZE_PAISA,
    HolidayDataUnavailableError,
    SessionType,
    daily_derive_cutoff,
    expected_bars_per_day_for_tf,
    forced_exit_seconds_for_session,
    forced_exit_time_for_session,
    get_session,
    get_session_checked,
    has_muhurat_session,
    is_forced_exit_bar,
    is_holiday,
    is_holiday_checked,
    is_holiday_data_available,
    is_nifty_rebalance_day,
    is_pre_open,
    is_special_weekend_session,
    is_trading_day,
    is_trading_day_checked,
    is_weekend,
    nifty_rebalance_event_on,
    previous_trading_session,
    session_minutes_per_day,
    trading_days_between,
    trading_days_between_checked,
)
from structlog.testing import capture_logs


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

    def test_holiday_list_spans_2020_2026(self) -> None:
        # At least one holiday per year (2020 added by DEC-CALENDAR-2020-DATA-001).
        from brutex.calendars.nse import (
            HOLIDAYS_2020,
            HOLIDAYS_2021,
            HOLIDAYS_2022,
            HOLIDAYS_2023,
            HOLIDAYS_2024,
            HOLIDAYS_2025,
            HOLIDAYS_2026,
        )

        for hols in [
            HOLIDAYS_2020,
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
            # Non-divisors of 375 count the truncated final bucket
            # (DEC-CALENDAR-BARS-CEIL-001 — ceil, matching the
            # resample_1m_to_tf oracle; was floor: 2->187, 30->12, 60->6).
            (1, 375),
            (2, 188),
            (5, 75),
            (15, 25),
            (30, 13),
            (60, 7),
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

    def test_budget_day_2020_is_special_weekend_session(self) -> None:
        # 2020-02-01 was a Saturday — Union Budget 2020-21 special NSE + BSE
        # live session (inside the 2020-01-01 data-pull floor).
        # DEC-CALENDAR-2020-BUDGET-SATURDAY-001.
        assert is_special_weekend_session(date(2020, 2, 1))

    def test_budget_day_2020_promotes_saturday_to_trading_day(self) -> None:
        # is_trading_day(2020-02-01) must be True (fail-before: False).
        d = date(2020, 2, 1)
        assert is_weekend(d)
        assert is_trading_day(d)

    def test_budget_day_2020_prev_trading_day_anchor(self) -> None:
        # Mon 2020-02-03's prior trading day must be the Sat Budget session
        # (2020-02-01), NOT Fri 2020-01-31 — so PDH/PDL anchors correctly.
        from brutex.indicators.prev_day_ohlc import find_prev_trading_day

        assert find_prev_trading_day(date(2020, 2, 3)) == date(2020, 2, 1)

    def test_budget_day_2020_get_session_regular_hours_forced_exit(self) -> None:
        # Full 09:15-15:30 REGULAR hours + 15:20 forced-exit, like the other
        # special Saturday sessions.
        session = get_session(date(2020, 2, 1))
        assert session is not None
        assert session.session_type == SessionType.REGULAR
        assert session.open_time_ist == time(9, 15)
        assert session.close_time_ist == time(15, 30)
        assert session.duration_minutes == 375
        assert forced_exit_time_for_session(session) == time(15, 20)

    def test_budget_day_2020_not_in_holiday_list(self) -> None:
        # 2020-02-01 must NOT be in ALL_HOLIDAYS (it is a trading day).
        from brutex.calendars.nse import ALL_HOLIDAYS

        assert date(2020, 2, 1) not in ALL_HOLIDAYS

    def test_budget_day_2025_is_special_weekend_session(self) -> None:

        # 2025-02-01 was a Saturday — Union Budget 2025-26 special session.
        assert is_special_weekend_session(date(2025, 2, 1))

    def test_budget_day_2026_is_special_weekend_session(self) -> None:

        # 2026-02-01 is a Sunday — Union Budget 2026-27 special session.
        assert is_special_weekend_session(date(2026, 2, 1))

    def test_regular_saturday_is_not_special(self) -> None:

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

    The hardcoded holiday list now starts in 2020 (DEC-CALENDAR-2020-DATA-001),
    matching the Track-1 data pull floor of 2020-01-01 (CLAUDE.md §3 /
    MIGRATION.md #722) — so the entire pull range is covered. A PRE-2020 (2019)
    weekday holiday is the new out-of-coverage case; these opt-in primitives let
    a caller DETECT the gap and refuse to guess at the new 2019/2020 boundary.
    """

    def test_coverage_range_derived_from_list(self) -> None:
        assert min(h.year for h in ALL_HOLIDAYS) == HOLIDAY_COVERAGE_FIRST_YEAR
        assert max(h.year for h in ALL_HOLIDAYS) == HOLIDAY_COVERAGE_LAST_YEAR
        assert HOLIDAY_COVERAGE_FIRST_YEAR <= HOLIDAY_COVERAGE_LAST_YEAR

    def test_coverage_starts_at_the_2020_pull_floor(self) -> None:
        # The 2020 list addition makes the holiday coverage start year match the
        # data-pull floor year — the gap DEC-CALENDAR-2020-DATA-001 closes.
        assert HOLIDAY_COVERAGE_FIRST_YEAR == 2020

    def test_in_coverage_dates_available(self) -> None:
        assert is_holiday_data_available(date(HOLIDAY_COVERAGE_FIRST_YEAR, 1, 1))
        assert is_holiday_data_available(date(HOLIDAY_COVERAGE_LAST_YEAR, 12, 31))
        assert is_holiday_data_available(date(2024, 6, 1))

    def test_2020_now_available(self) -> None:
        # 2020 is IN the data-pull scope AND now IN the holiday list.
        assert is_holiday_data_available(date(2020, 3, 10))

    def test_below_floor_2019_not_available(self) -> None:
        # 2019 is the new out-of-coverage boundary (below the 2020 list start).
        assert not is_holiday_data_available(date(2019, 12, 31))

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
        assert is_holiday(date(2019, 12, 31)) is False

    def test_is_holiday_checked_raises_below_floor(self) -> None:
        with pytest.raises(HolidayDataUnavailableError, match="2019-12-31"):
            is_holiday_checked(date(2019, 12, 31))

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

    def test_2020_now_resolves_through_checked_variant(self) -> None:
        # 2020 is now in coverage: Holi 2020-03-10 (Tue) is a listed holiday -> a
        # honest False (no raise); a 2020 regular weekday is True. The unguarded
        # and strict variants now AGREE for 2020 (gap closed).
        assert is_trading_day(date(2020, 3, 10)) is False
        assert is_trading_day_checked(date(2020, 3, 10)) is False
        assert is_trading_day_checked(date(2020, 3, 11)) is True  # Wed, no holiday

    def test_out_of_range_weekday_raises(self) -> None:
        # 2019-12-31 (Tuesday) is the new out-of-coverage weekday: the UNGUARDED
        # is_trading_day still returns True (back-compat, unchanged) — the strict
        # variant DETECTS the out-of-coverage case and refuses to guess instead.
        assert is_trading_day(date(2019, 12, 31)) is True
        with pytest.raises(HolidayDataUnavailableError):
            is_trading_day_checked(date(2019, 12, 31))

    def test_out_of_range_weekend_resolves_false_without_raising(self) -> None:
        # A weekend is non-trading INDEPENDENT of the holiday list, so its
        # verdict consults no missing data and is honest even out of coverage.
        # 2019-12-28 (below floor) and 2027-01-02 (above ceiling) are Saturdays.
        assert is_trading_day_checked(date(2019, 12, 28)) is False
        assert is_trading_day_checked(date(2027, 1, 2)) is False

    def test_out_of_range_sunday_resolves_false_without_raising(self) -> None:
        # 2019-12-29 is a Sunday — same data-complete contract.
        assert is_trading_day_checked(date(2019, 12, 29)) is False

    def test_special_weekend_session_overrides_even_out_of_coverage(self) -> None:
        # A date the package DOES hold data for resolves without raising even
        # if its year is outside the holiday span. (Currently every special
        # session is in-coverage; this guards the precedence contract.)
        d = date(2025, 2, 1)  # Budget-day Saturday, in coverage
        assert is_trading_day_checked(d) is True


class TestGetSessionChecked:
    """DEC-CALENDAR-CHECKED-TWINS-001: strict get_session refuses to FABRICATE a
    regular session for an out-of-coverage weekday holiday."""

    def test_regular_weekday_in_range_matches_unchecked(self) -> None:
        d = date(2024, 2, 20)  # Tuesday, no holiday
        checked = get_session_checked(d)
        assert checked == get_session(d)
        assert checked is not None
        assert checked.session_type is SessionType.REGULAR
        assert checked.open_time_ist == SESSION_OPEN_IST
        assert checked.close_time_ist == SESSION_CLOSE_IST

    def test_holiday_in_range_returns_none(self) -> None:
        # 2024-01-26 (Fri) Republic Day — a listed holiday inside coverage.
        assert get_session_checked(date(2024, 1, 26)) is None

    def test_weekend_in_range_returns_none(self) -> None:
        assert get_session_checked(date(2024, 5, 11)) is None  # Saturday

    def test_muhurat_session_in_range(self) -> None:
        # 2024-11-01 Diwali muhurat — precedence over the holiday flag.
        checked = get_session_checked(date(2024, 11, 1))
        assert checked is not None
        assert checked.session_type is SessionType.MUHURAT

    def test_out_of_range_weekday_raises(self) -> None:
        # 2030-06-24 (Monday) is an out-of-coverage weekday: the UNGUARDED
        # get_session FABRICATES a REGULAR session (back-compat, unchanged) — the
        # strict variant DETECTS the missing-data case and refuses to guess.
        fabricated = get_session(date(2030, 6, 24))
        assert fabricated is not None
        assert fabricated.session_type is SessionType.REGULAR
        with pytest.raises(HolidayDataUnavailableError):
            get_session_checked(date(2030, 6, 24))

    def test_out_of_range_weekend_resolves_none_without_raising(self) -> None:
        # A weekend is non-trading INDEPENDENT of the holiday list, so its
        # verdict consults no missing data and is honest even out of coverage.
        assert get_session_checked(date(2030, 6, 22)) is None  # Saturday
        assert get_session_checked(date(2030, 6, 23)) is None  # Sunday

    def test_special_weekend_session_overrides_even_out_of_coverage(self) -> None:
        # A date the package DOES hold data for resolves without raising.
        checked = get_session_checked(date(2025, 2, 1))  # Budget-day Saturday
        assert checked is not None
        assert checked.session_type is SessionType.REGULAR


class TestTradingDaysBetweenChecked:
    """DEC-CALENDAR-CHECKED-TWINS-001: strict trading_days_between refuses an
    out-of-coverage weekday instead of silently including an unknown holiday."""

    def test_in_range_matches_unchecked(self) -> None:
        start, end = date(2024, 2, 19), date(2024, 2, 25)
        assert trading_days_between_checked(start, end) == trading_days_between(start, end)

    def test_excludes_in_range_holiday(self) -> None:
        # 2024-01-26 (Fri) Republic Day falls in this range and is excluded.
        days = trading_days_between_checked(date(2024, 1, 22), date(2024, 1, 28))
        assert date(2024, 1, 26) not in days
        assert len(days) == 4  # Mon, Tue, Wed, Thu

    def test_empty_range_returns_empty(self) -> None:
        assert trading_days_between_checked(date(2024, 5, 11), date(2024, 5, 11)) == []

    def test_out_of_range_weekday_in_span_raises(self) -> None:
        # 2030-06-24 (Monday) out of coverage: the UNGUARDED variant INCLUDES it
        # (back-compat); the strict variant refuses the whole span.
        assert date(2030, 6, 24) in trading_days_between(date(2030, 6, 22), date(2030, 6, 26))
        with pytest.raises(HolidayDataUnavailableError):
            trading_days_between_checked(date(2030, 6, 22), date(2030, 6, 26))

    def test_out_of_range_weekend_only_span_resolves_without_raising(self) -> None:
        # A pure-weekend out-of-coverage span consults no missing data.
        assert trading_days_between_checked(date(2030, 6, 22), date(2030, 6, 23)) == []


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
        # 2020-02-29 (Saturday) — weekend, now IN holiday coverage (2020 added).
        d = date(2020, 2, 29)
        assert is_weekend(d)
        assert is_holiday_data_available(d)
        assert is_trading_day_checked(d) is False  # weekend, data-complete

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


class TestExpectedBarsValidation:
    """Regression: tf < 1 must raise ValueError, never ZeroDivisionError or
    a negative bar count (mirrors is_forced_exit_bar). Bug fix DEC-CALENDAR-HARDEN-001."""

    def test_zero_timeframe_raises_valueerror_not_zerodivision(self) -> None:
        with pytest.raises(ValueError, match="must be >= 1"):
            expected_bars_per_day_for_tf(0)

    def test_negative_timeframe_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="must be >= 1"):
            expected_bars_per_day_for_tf(-5)

    def test_minimum_valid_timeframe_is_one(self) -> None:
        assert expected_bars_per_day_for_tf(1) == 375

    def test_timeframe_at_session_length_is_one_bar(self) -> None:
        assert expected_bars_per_day_for_tf(375) == 1

    def test_timeframe_above_session_length_is_one_bar(self) -> None:
        assert expected_bars_per_day_for_tf(376) == 1


class TestRebalanceLookupIsO1:
    """Regression: rebalance lookups are O(1) dict lookups consistent with the
    authoritative NIFTY50_REBALANCE_EVENTS tuple (DEC-CALENDAR-HARDEN-001)."""

    def test_map_keys_match_event_effective_dates(self) -> None:
        from brutex.calendars.nse import (
            _REBALANCE_BY_DATE,
            NIFTY50_REBALANCE_EVENTS,
        )

        assert set(_REBALANCE_BY_DATE) == {e.effective_date for e in NIFTY50_REBALANCE_EVENTS}
        assert len(_REBALANCE_BY_DATE) == len(NIFTY50_REBALANCE_EVENTS)

    def test_map_lookup_resolves_every_event_by_identity(self) -> None:
        # Every authoritative event resolves through the dict by `is`-identity
        # (the prior O(n) linear scan was removed in favour of the O(1) map).
        from brutex.calendars.nse import NIFTY50_REBALANCE_EVENTS

        for event in NIFTY50_REBALANCE_EVENTS:
            assert is_nifty_rebalance_day(event.effective_date)
            assert nifty_rebalance_event_on(event.effective_date) is event

    def test_non_event_date_returns_false_and_none(self) -> None:
        assert not is_nifty_rebalance_day(date(2024, 2, 20))
        assert nifty_rebalance_event_on(date(2024, 2, 20)) is None


class TestISTOffsetFixedNoDst:
    """IST must be a fixed +05:30 offset with NO daylight saving across the
    full in-scope range (2020-2026), including a leap-year February."""

    @pytest.mark.parametrize(
        "dt",
        [
            datetime(2020, 1, 15, 12, 0, tzinfo=IST),
            datetime(2021, 7, 1, 9, 15, tzinfo=IST),
            datetime(2024, 2, 29, 15, 30, tzinfo=IST),  # leap day
            datetime(2025, 6, 21, 0, 0, tzinfo=IST),
            datetime(2026, 12, 31, 23, 59, tzinfo=IST),
        ],
    )
    def test_offset_is_exactly_5h30m_and_no_dst(self, dt: datetime) -> None:
        assert dt.utcoffset() == timedelta(hours=5, minutes=30)
        assert dt.dst() == timedelta(0)

    def test_offset_identical_in_winter_and_summer(self) -> None:
        jan = datetime(2024, 1, 1, 12, 0, tzinfo=IST).utcoffset()
        jul = datetime(2024, 7, 1, 12, 0, tzinfo=IST).utcoffset()
        assert jan == jul == timedelta(hours=5, minutes=30)


class TestForcedExitTimeForSession:
    """forced_exit_time_for_session: one COMMON rule (close - 10min) that
    yields 15:20 for a regular session and scales for short sessions."""

    def test_regular_session_yields_canonical_1520(self) -> None:
        session = get_session(date(2024, 2, 20))  # regular Tuesday
        assert session is not None
        assert forced_exit_time_for_session(session) == FORCED_EXIT_IST
        assert forced_exit_time_for_session(session) == time(15, 20)

    def test_special_weekend_session_yields_1520(self) -> None:
        session = get_session(date(2025, 2, 1))  # Budget-day full hours
        assert session is not None
        assert forced_exit_time_for_session(session) == time(15, 20)

    def test_muhurat_afternoon_2025_scales_to_close_minus_10(self) -> None:
        session = get_session(date(2025, 10, 21))  # 13:45-14:45 afternoon
        assert session is not None
        assert forced_exit_time_for_session(session) == time(14, 35)

    def test_muhurat_evening_2021_scales_to_close_minus_10(self) -> None:
        session = get_session(date(2021, 11, 4))  # 18:15-19:15 evening
        assert session is not None
        assert forced_exit_time_for_session(session) == time(19, 5)


class TestForcedExitSecondsForSession:
    """forced_exit_seconds_for_session: the integer (seconds-since-midnight)
    twin of forced_exit_time_for_session, in the unit is_forced_exit_bar
    compares against — 15:20 for a regular session, scaled for a short one."""

    def test_regular_session_yields_15200_seconds(self) -> None:
        session = get_session(date(2024, 2, 20))  # regular Tuesday
        assert session is not None
        # 15:20 IST == 15 * 3600 + 20 * 60 == 55200 seconds-since-midnight.
        assert forced_exit_seconds_for_session(session) == 55200

    def test_seconds_matches_time_twin(self) -> None:
        session = get_session(date(2024, 2, 20))
        assert session is not None
        forced = forced_exit_time_for_session(session)
        expected = forced.hour * 3600 + forced.minute * 60 + forced.second
        assert forced_exit_seconds_for_session(session) == expected

    def test_muhurat_afternoon_2025_scales_to_close_minus_10(self) -> None:
        session = get_session(date(2025, 10, 21))  # 13:45-14:45 afternoon
        assert session is not None
        # 14:35 IST == 14 * 3600 + 35 * 60 == 52500 seconds.
        assert forced_exit_seconds_for_session(session) == 52500


class TestBoundaryTimestamps:
    """Exact boundary instants for pre-open / forced-exit windows."""

    def test_preopen_start_0900_inclusive(self) -> None:
        assert is_pre_open(time(9, 0, 0))

    def test_preopen_last_second_before_open(self) -> None:
        assert is_pre_open(time(9, 14, 59))

    def test_preopen_excludes_exact_open(self) -> None:
        assert not is_pre_open(time(9, 15, 0))

    def test_preopen_excludes_just_before_0900(self) -> None:
        assert not is_pre_open(time(8, 59, 59))

    def test_forced_exit_1m_bar_opening_exactly_1519_not_forced(self) -> None:
        # [15:19:00, 15:20:00) closes exactly at 15:20 -> NOT strictly after.
        assert not is_forced_exit_bar(time(15, 19, 0), tf_minutes=1)

    def test_forced_exit_1m_bar_opening_1519_01_is_forced(self) -> None:
        # [15:19:01, 15:20:01) reaches past 15:20:00 -> forced.
        assert is_forced_exit_bar(time(15, 19, 1), tf_minutes=1)


class TestOutOfRangeDates:
    """Dates outside the curated 2020-2026 holiday window degrade gracefully
    (never crash): only the weekend rule applies, holidays read False."""

    def test_pre_2020_date_not_a_listed_holiday(self) -> None:
        assert not is_holiday(date(2019, 1, 1))

    def test_post_2026_date_not_a_listed_holiday(self) -> None:
        assert not is_holiday(date(2030, 1, 1))

    def test_far_future_weekday_is_trading_day_scope_limitation(self) -> None:
        # 2030-01-01 is a Tuesday; outside the curated holiday list it reads as
        # a trading day. Documents the honest scope limitation, not a bug.
        assert date(2030, 1, 1).weekday() == 1
        assert is_trading_day(date(2030, 1, 1))

    def test_trading_days_span_a_year_boundary(self) -> None:
        # 2024-12-30 (Mon) .. 2025-01-02 (Thu): no weekend, no listed holiday.
        days = trading_days_between(date(2024, 12, 30), date(2025, 1, 2))
        assert days == [
            date(2024, 12, 30),
            date(2024, 12, 31),
            date(2025, 1, 1),
            date(2025, 1, 2),
        ]


class TestLeapYearTradingDay:
    """2024 is a leap year; Feb 29 is a regular Thursday trading day."""

    def test_feb_29_2024_is_thursday_trading_day(self) -> None:
        d = date(2024, 2, 29)
        assert d.weekday() == 3  # Thursday
        assert is_trading_day(d)

    def test_trading_days_include_leap_day(self) -> None:
        days = trading_days_between(date(2024, 2, 26), date(2024, 3, 1))
        assert date(2024, 2, 29) in days
        assert len(days) == 5  # Mon..Fri, no holiday in span


class TestMuhuratSessionsAllYears:
    """Every Muhurat session is a valid ~1-hour window; resolution honours
    weekend precedence and the 2025 afternoon shift."""

    def test_every_muhurat_session_is_a_valid_one_hour_window(self) -> None:
        for d, (open_t, close_t) in MUHURAT_SESSIONS.items():
            assert open_t < close_t
            session = get_session(d)
            assert session is not None
            assert session.session_type == SessionType.MUHURAT
            assert 0 < session.duration_minutes <= 90
            assert session.open_time_ist == open_t
            assert session.close_time_ist == close_t

    def test_muhurat_2023_on_a_sunday_still_returns_session(self) -> None:
        # 2023-11-12 is a Sunday; Muhurat precedence overrides the weekend rule.
        d = date(2023, 11, 12)
        assert d.weekday() == 6  # Sunday
        assert is_weekend(d)
        session = get_session(d)
        assert session is not None
        assert session.session_type == SessionType.MUHURAT

    def test_muhurat_2025_is_afternoon(self) -> None:
        session = get_session(date(2025, 10, 21))
        assert session is not None
        assert session.open_time_ist == time(13, 45)
        assert session.close_time_ist == time(14, 45)

    def test_non_muhurat_date_has_no_muhurat_session(self) -> None:
        assert not has_muhurat_session(date(2024, 2, 20))


class TestGoldenTradingDaysJan2024:
    """Golden snapshot: the exact trading-day list for January 2024
    (weekends + Republic Day Jan 26 excluded)."""

    def test_january_2024_trading_days_exact(self) -> None:
        expected = [
            date(2024, 1, 1),
            date(2024, 1, 2),
            date(2024, 1, 3),
            date(2024, 1, 4),
            date(2024, 1, 5),
            date(2024, 1, 8),
            date(2024, 1, 9),
            date(2024, 1, 10),
            date(2024, 1, 11),
            date(2024, 1, 12),
            date(2024, 1, 15),
            date(2024, 1, 16),
            date(2024, 1, 17),
            date(2024, 1, 18),
            date(2024, 1, 19),
            date(2024, 1, 22),
            date(2024, 1, 23),
            date(2024, 1, 24),
            date(2024, 1, 25),
            date(2024, 1, 29),
            date(2024, 1, 30),
            date(2024, 1, 31),
        ]
        assert trading_days_between(date(2024, 1, 1), date(2024, 1, 31)) == expected
        assert date(2024, 1, 26) not in expected  # Republic Day


class TestStressLongRange:
    """Stress: the full 6-year span resolves without error and is consistent."""

    def test_full_2021_2026_span_is_sane(self) -> None:
        days = trading_days_between(date(2021, 1, 1), date(2026, 12, 31))
        assert 1400 <= len(days) <= 1560  # ~250 trading days/yr x 6
        assert days == sorted(days)
        assert len(set(days)) == len(days)
        assert all(is_trading_day(d) for d in days)
        assert date(2025, 2, 1) in days  # Budget-day special weekend session
        assert date(2026, 2, 1) in days
        assert date(2024, 1, 26) not in days  # Republic Day excluded


class TestFacadeIntegration:
    """Integration: the public facade re-exports the new helper and the
    end-to-end session -> forced-exit flow works through brutex.calendars."""

    def test_facade_exports_forced_exit_helper(self) -> None:
        import brutex.calendars as cal

        assert "forced_exit_time_for_session" in cal.__all__
        assert hasattr(cal, "forced_exit_time_for_session")

    def test_end_to_end_session_to_forced_exit_via_facade(self) -> None:
        import brutex.calendars as cal

        session = cal.get_session(date(2024, 2, 20))
        assert session is not None
        exit_t = cal.forced_exit_time_for_session(session)
        assert exit_t == cal.FORCED_EXIT_IST
        assert cal.is_forced_exit_bar(exit_t, tf_minutes=1)
        assert cal.expected_bars_per_day_for_tf(5) == 75


# ── daily_derive_cutoff (DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001) ───────────────


class TestDailyDeriveCutoff:
    """The session-close-aware forming-session cutoff for the derived 1day.

    ``today`` while the run day's session is forming (or exactly AT close —
    strict ``>``, conservative); ``today + 1`` once the session provably
    closed OR on a non-trading day — honoring the per-date close of regular
    days, SPECIAL_WEEKEND_TRADING_SESSIONS Budget weekends and the Muhurat
    afternoon/evening hours.
    """

    @staticmethod
    def _at(d: date, t: time) -> datetime:
        return datetime.combine(d, t, tzinfo=IST)

    def test_regular_weekday_pre_close_keeps_today(self) -> None:
        d = date(2026, 7, 1)  # a regular Wednesday session
        assert daily_derive_cutoff(self._at(d, time(12, 0))) == d

    def test_regular_weekday_exactly_at_close_keeps_today(self) -> None:
        # Strict `>` — exactly 15:30:00 is NOT provably past the close
        # (conservative; boundary tightening is a named follow-up).
        d = date(2026, 7, 1)
        assert daily_derive_cutoff(self._at(d, SESSION_CLOSE_IST)) == d

    def test_regular_weekday_post_close_includes_today(self) -> None:
        d = date(2026, 7, 1)
        assert daily_derive_cutoff(self._at(d, time(16, 0))) == d + timedelta(days=1)

    def test_weekend_returns_tomorrow(self) -> None:
        d = date(2026, 7, 5)  # Sunday, no special session
        assert get_session(d) is None
        assert daily_derive_cutoff(self._at(d, time(12, 0))) == d + timedelta(days=1)

    def test_holiday_returns_tomorrow(self) -> None:
        d = date(2026, 1, 26)  # Republic Day (Monday) — verified holiday
        assert is_holiday(d)
        assert daily_derive_cutoff(self._at(d, time(12, 0))) == d + timedelta(days=1)

    def test_special_weekend_budget_session_pre_and_post_close(self) -> None:
        # 2026-02-01 is a SUNDAY Budget session (SPECIAL_WEEKEND_TRADING_SESSIONS):
        # mid-session it must be protected like any forming session; post-close
        # it derives — a plain weekend rule would have wrongly returned
        # tomorrow ALL day (deriving the forming session mid-day).
        d = date(2026, 2, 1)
        open_t, close_t = SPECIAL_WEEKEND_TRADING_SESSIONS[d]
        assert daily_derive_cutoff(self._at(d, open_t)) == d
        assert daily_derive_cutoff(self._at(d, time(12, 0))) == d
        assert daily_derive_cutoff(self._at(d, close_t)) == d  # at-close: conservative
        assert daily_derive_cutoff(self._at(d, time(16, 0))) == d + timedelta(days=1)

    def test_muhurat_close_time_honored(self) -> None:
        # 2025-10-21 Muhurat runs 13:45-14:45 IST (the verified afternoon
        # shift): 14:00 is mid-session (today), 15:00 is past ITS close
        # (tomorrow) even though a regular day would still be open at 15:00.
        d = date(2025, 10, 21)
        open_t, close_t = MUHURAT_SESSIONS[d]
        assert daily_derive_cutoff(self._at(d, open_t)) == d
        assert daily_derive_cutoff(self._at(d, time(14, 0))) == d
        assert daily_derive_cutoff(
            self._at(d, (datetime.combine(d, close_t) + timedelta(minutes=15)).time())
        ) == d + timedelta(days=1)


class TestExchangeSplit:
    """DEC-CALENDAR-EXCHANGE-SPLIT-001: per-exchange NSE/BSE holiday sets over
    ONE common per-year core, with a keyword-only ``exchange`` selector on every
    lookup function (``None`` = the exchange-agnostic pre-split behaviour)."""

    def test_nse_equals_bse_equals_all_today(self) -> None:
        # NO citable NSE-vs-BSE divergent equity-segment holiday exists for
        # 2020-2026 (secondary-corroborated; primary circulars 403'd), so the
        # three sets are IDENTICAL today — the split is a divergence MECHANISM.
        from brutex.calendars.nse import BSE_HOLIDAYS, NSE_HOLIDAYS

        assert NSE_HOLIDAYS == BSE_HOLIDAYS == ALL_HOLIDAYS

    def test_only_sets_are_empty_by_verified_fact(self) -> None:
        from brutex.calendars.nse import BSE_ONLY_HOLIDAYS, NSE_ONLY_HOLIDAYS

        assert not NSE_ONLY_HOLIDAYS
        assert not BSE_ONLY_HOLIDAYS

    def test_holidays_for_exchange_dispatch(self) -> None:
        from brutex.calendars.nse import (
            BSE_HOLIDAYS,
            NSE_HOLIDAYS,
            holidays_for_exchange,
        )

        assert holidays_for_exchange("NSE") is NSE_HOLIDAYS
        assert holidays_for_exchange("BSE") is BSE_HOLIDAYS

    def test_holidays_for_exchange_invalid_raises_naming_valid_values(self) -> None:
        from brutex.calendars.nse import holidays_for_exchange

        with pytest.raises(ValueError, match="'NSE' and 'BSE'"):
            holidays_for_exchange("MCX")
        with pytest.raises(ValueError, match="unknown exchange 'nse'"):
            holidays_for_exchange("nse")  # exact match, no case folding

    @pytest.mark.parametrize("exchange", ["NSE", "BSE"])
    def test_exchange_threading_matches_default_on_every_function(self, exchange: str) -> None:
        # With NSE == BSE == ALL, every function must answer identically for
        # exchange=None / "NSE" / "BSE" — the byte-compat contract.
        holiday = date(2024, 1, 26)  # Republic Day (Fri)
        weekday = date(2024, 2, 20)  # regular Tuesday
        weekend = date(2024, 5, 11)  # Saturday
        budget = date(2025, 2, 1)  # special weekend session (SHARED)
        muhurat = date(2024, 11, 1)  # Muhurat (SHARED)
        assert is_holiday(holiday, exchange=exchange) is is_holiday(holiday) is True
        assert is_holiday(weekday, exchange=exchange) is is_holiday(weekday) is False
        assert is_holiday_checked(holiday, exchange=exchange) is True
        assert is_trading_day(weekday, exchange=exchange) is True
        assert is_trading_day(holiday, exchange=exchange) is False
        assert is_trading_day(budget, exchange=exchange) is True  # special session shared
        assert is_trading_day_checked(weekday, exchange=exchange) is True
        assert is_trading_day_checked(weekend, exchange=exchange) is False
        assert get_session(weekday, exchange=exchange) == get_session(weekday)
        assert get_session(holiday, exchange=exchange) is None
        muhurat_session = get_session(muhurat, exchange=exchange)
        assert muhurat_session is not None
        assert muhurat_session.session_type is SessionType.MUHURAT
        assert get_session_checked(weekday, exchange=exchange) == get_session_checked(weekday)
        span = (date(2024, 1, 22), date(2024, 1, 28))
        assert trading_days_between(*span, exchange=exchange) == trading_days_between(*span)
        assert trading_days_between_checked(*span, exchange=exchange) == (
            trading_days_between_checked(*span)
        )

    def test_checked_variants_still_raise_out_of_coverage_with_exchange(self) -> None:
        out_weekday = date(2019, 12, 31)  # Tuesday, below the 2020 floor
        with pytest.raises(HolidayDataUnavailableError):
            is_holiday_checked(out_weekday, exchange="NSE")
        with pytest.raises(HolidayDataUnavailableError):
            is_trading_day_checked(out_weekday, exchange="BSE")

    def test_invalid_exchange_raises_on_every_function(self) -> None:
        d = date(2024, 2, 20)
        with pytest.raises(ValueError, match="unknown exchange"):
            is_holiday(d, exchange="XSE")
        with pytest.raises(ValueError, match="unknown exchange"):
            is_holiday_checked(d, exchange="XSE")
        with pytest.raises(ValueError, match="unknown exchange"):
            is_trading_day(d, exchange="XSE")
        with pytest.raises(ValueError, match="unknown exchange"):
            is_trading_day_checked(d, exchange="XSE")
        with pytest.raises(ValueError, match="unknown exchange"):
            get_session(d, exchange="XSE")
        with pytest.raises(ValueError, match="unknown exchange"):
            get_session_checked(d, exchange="XSE")
        with pytest.raises(ValueError, match="unknown exchange"):
            trading_days_between(d, d, exchange="XSE")
        with pytest.raises(ValueError, match="unknown exchange"):
            trading_days_between_checked(d, d, exchange="XSE")

    def test_invalid_exchange_raises_even_when_date_path_short_circuits(self) -> None:
        # Eager validation: a Muhurat / special-weekend / weekend date would
        # short-circuit before the holiday set is consulted — the invalid
        # exchange must STILL raise (never a silent typo).
        with pytest.raises(ValueError, match="unknown exchange"):
            get_session(date(2024, 11, 1), exchange="XSE")  # Muhurat date
        with pytest.raises(ValueError, match="unknown exchange"):
            is_trading_day(date(2025, 2, 1), exchange="XSE")  # Budget Saturday
        with pytest.raises(ValueError, match="unknown exchange"):
            is_trading_day_checked(date(2024, 5, 11), exchange="XSE")  # Saturday
        with pytest.raises(ValueError, match="unknown exchange"):
            get_session_checked(date(2024, 5, 11), exchange="XSE")  # Saturday

    def test_facade_exports_exchange_surface(self) -> None:
        import brutex.calendars as cal

        for name in ("NSE_HOLIDAYS", "BSE_HOLIDAYS", "holidays_for_exchange"):
            assert name in cal.__all__
            assert hasattr(cal, name)


class TestHolidays2026Complete:
    """DEC-CALENDAR-2026-COMPLETE-001: the corroborated full-year 2026 list
    (secondary-corroborated across both research rounds; the primary NSE
    circular CMTR71775 PDF was unreachable — HTTP 403 — so NOT primary-verified).
    """

    #: The 15 weekday trading holidays both research rounds agree on.
    _WEEKDAY_HOLIDAYS_2026: ClassVar[list[date]] = [
        date(2026, 1, 26),  # Mon Republic Day
        date(2026, 3, 3),  # Tue Holi
        date(2026, 3, 26),  # Thu Ram Navami
        date(2026, 3, 31),  # Tue Mahavir Jayanti
        date(2026, 4, 3),  # Fri Good Friday
        date(2026, 4, 14),  # Tue Dr Ambedkar Jayanti
        date(2026, 5, 1),  # Fri Maharashtra Day
        date(2026, 5, 28),  # Thu Bakri Id
        date(2026, 6, 26),  # Fri Muharram
        date(2026, 9, 14),  # Mon Ganesh Chaturthi
        date(2026, 10, 2),  # Fri Gandhi Jayanti
        date(2026, 10, 20),  # Tue Dussehra
        date(2026, 11, 10),  # Tue Diwali Balipratipada
        date(2026, 11, 24),  # Tue Guru Nanak Jayanti
        date(2026, 12, 25),  # Fri Christmas
    ]

    #: Weekend-falling published dates (kept per the earlier-years convention).
    _WEEKEND_DATES_2026: ClassVar[list[date]] = [
        date(2026, 2, 15),  # Sun Mahashivratri
        date(2026, 3, 21),  # Sat Id-ul-Fitr
        date(2026, 8, 15),  # Sat Independence Day
        date(2026, 11, 8),  # Sun Diwali Laxmi Pujan (Muhurat day, timings TBD)
    ]

    def test_every_weekday_holiday_is_not_a_trading_day(self) -> None:
        for d in self._WEEKDAY_HOLIDAYS_2026:
            assert d.weekday() < 5, d  # genuinely a weekday
            assert is_holiday(d), d
            for exchange in (None, "NSE", "BSE"):
                assert is_trading_day(d, exchange=exchange) is False, (d, exchange)

    def test_weekend_published_dates_listed_but_weekend_anyway(self) -> None:
        for d in self._WEEKEND_DATES_2026:
            assert d.weekday() >= 5, d
            assert is_holiday(d), d
            assert is_trading_day(d) is False, d

    def test_2026_holiday_set_is_exactly_the_corroborated_19(self) -> None:
        from brutex.calendars.nse import HOLIDAYS_2026

        expected = frozenset(self._WEEKDAY_HOLIDAYS_2026 + self._WEEKEND_DATES_2026)
        assert expected == HOLIDAYS_2026

    def test_ram_navami_2026_fixed_to_march_26(self) -> None:
        # REGRESSION (the real bug this pack fixes): the prior list had Ram
        # Navami on 2026-03-27 — a FRIDAY, a normal trading day. Both research
        # rounds independently confirm Thu 2026-03-26.
        assert date(2026, 3, 26).weekday() == 3  # Thursday
        assert is_holiday(date(2026, 3, 26))
        assert not is_holiday(date(2026, 3, 27))
        for exchange in (None, "NSE", "BSE"):
            assert is_trading_day(date(2026, 3, 26), exchange=exchange) is False
            assert is_trading_day(date(2026, 3, 27), exchange=exchange) is True

    def test_january_2026_has_exactly_21_sessions(self) -> None:
        # 22 January weekdays minus Republic Day (the ONLY January holiday per
        # both rounds). The vendor-observed 20-session drift stays UNVERIFIED —
        # no date is fabricated to reconcile it (Golden Rule #1).
        days = trading_days_between(date(2026, 1, 1), date(2026, 1, 31))
        assert len(days) == 21
        assert date(2026, 1, 26) not in days

    def test_known_open_2026_weekdays_are_trading_days(self) -> None:
        assert is_trading_day(date(2026, 4, 1))  # Wed — between the Mar/Apr holidays
        assert is_trading_day(date(2026, 7, 3))  # Fri — no July holiday
        assert is_trading_day(date(2026, 12, 24))  # Thu — day before Christmas

    def test_muhurat_2026_honestly_absent_until_timings_notified(self) -> None:
        # The 2026-11-08 Muhurat session is ANNOUNCED but its timings are NOT
        # yet notified — fabricating an hour would be a guess (Golden Rule #1).
        assert not has_muhurat_session(date(2026, 11, 8))
        assert get_session(date(2026, 11, 8)) is None  # Sunday + holiday

    def test_coverage_span_still_2020_to_2026(self) -> None:
        assert HOLIDAY_COVERAGE_FIRST_YEAR == 2020
        assert HOLIDAY_COVERAGE_LAST_YEAR == 2026


class TestMuhuratClampGuard:
    """DEC-SUSPICION-SWEEP-FIXES-001 (G2) — the clamp_window_end partial-settle guard.

    ``clamp_window_end`` allows an intraday window through ``today`` from the
    15:30 IST regular close onward (DEC-DAILY-CANDLE-CLAMP-001). A CURRENT/FUTURE
    Muhurat session that closes AFTER 15:30 (the historical evening sessions)
    would let a post-15:30 pull settle that day's window SUCCESS BEFORE the
    evening session traded — the keep-settled skip rule would then never
    re-fetch the Muhurat bars (a silent partial settle). The 2025 session is
    afternoon (13:45–14:45, the verified SEBI shift), so today no live entry
    re-arms the hazard; this enumeration pins that a FUTURE evening entry cannot
    land SILENTLY — adding one (e.g. the pending 2026-11-08 timings, if notified
    as an evening session) fails this test and forces a conscious
    clamp_window_end decision instead of a quiet re-arm.
    """

    #: The pivot: entries from 2025 onward are the "current/future" regime the
    #: clamp hazard applies to (pre-2025 evening sessions are historical — any
    #: pull today fetches them long after the fact, complete).
    _PIVOT = date(2025, 1, 1)
    #: The regular NSE close the intraday clamp keys on (15:30 IST).
    _REGULAR_CLOSE = time(15, 30)

    def test_no_current_or_future_muhurat_session_closes_after_1530(self) -> None:
        offenders = {
            d: (open_t, close_t)
            for d, (open_t, close_t) in MUHURAT_SESSIONS.items()
            if d >= self._PIVOT and close_t > self._REGULAR_CLOSE
        }
        assert offenders == {}, (
            f"MUHURAT_SESSIONS entries on/after {self._PIVOT} close AFTER 15:30 IST: "
            f"{offenders} — this re-arms the clamp_window_end partial-settle hazard "
            "(a post-15:30 pull settles the day SUCCESS before the evening session "
            "trades and the skip rule never re-fetches it). Before adding such an "
            "entry, make clamp_window_end Muhurat-aware (defer the day's intraday "
            "clamp past the special-session close) — see DEC-SUSPICION-SWEEP-FIXES-001."
        )

    def test_historical_evening_sessions_still_recorded(self) -> None:
        """The guard must never be 'fixed' by deleting real history: the verified
        2020–2024 evening sessions stay recorded (Golden Rule #1 — real data)."""
        evening = [
            d
            for d, (_open_t, close_t) in MUHURAT_SESSIONS.items()
            if d < self._PIVOT and close_t > self._REGULAR_CLOSE
        ]
        assert len(evening) == 5  # 2020–2024, all evening sessions


class TestPreviousTradingSession:
    """DEC-MOVERS-PREVCLOSE-CONTIGUITY-001 — the COMMON previous-session primitive."""

    def test_plain_weekday_before(self) -> None:
        # Wed 2024-07-10 → Tue 2024-07-09 (a plain trading Tuesday).
        assert previous_trading_session(date(2024, 7, 10)) == date(2024, 7, 9)

    def test_weekend_steps_back_to_friday(self) -> None:
        # Mon 2025-09-29 → Fri 2025-09-26 (Sat/Sun stepped over) — the exact
        # snapshot geometry of the confirmed /movers prev-close defect repro.
        assert previous_trading_session(date(2025, 9, 29)) == date(2025, 9, 26)

    def test_steps_over_a_listed_holiday(self) -> None:
        # Mon 2024-03-25 is Holi (HOLIDAYS_2024); Tue 2024-03-26 walks over the
        # holiday AND the weekend to Fri 2024-03-22.
        assert previous_trading_session(date(2024, 3, 26)) == date(2024, 3, 22)

    def test_special_weekend_budget_session_is_the_prior_session(self) -> None:
        # Sat 2025-02-01 is a special Budget trading session — Mon 2025-02-03's
        # previous session is that SATURDAY, not Fri 2025-01-31.
        assert previous_trading_session(date(2025, 2, 3)) == date(2025, 2, 1)
        # The 2026 Sunday Budget session twin (2026-02-01 → Mon 2026-02-02).
        assert previous_trading_session(date(2026, 2, 2)) == date(2026, 2, 1)

    def test_muhurat_day_is_a_non_session_honest_limit(self) -> None:
        # HONEST LIMIT (Golden Rule #10): the 2025 Diwali Muhurat day
        # (2025-10-21, a listed holiday carrying a ~1h special session) is a
        # NON-session for this primitive — the walk from Thu 2025-10-23 steps
        # over BOTH holiday days (10-22 Balipratipada + 10-21 Muhurat) to Mon
        # 2025-10-20, never returning the Muhurat date.
        got = previous_trading_session(date(2025, 10, 23))
        assert got == date(2025, 10, 20)
        assert got != date(2025, 10, 21)  # the Muhurat day is never returned

    def test_out_of_coverage_abstains_none_with_one_warning(self) -> None:
        # 2020-01-01's walk immediately probes 2019-12-31 — an out-of-coverage
        # WEEKDAY — so the strict calendar refuses and the primitive ABSTAINS
        # (None, never a guessed session) with ONE named warning.
        with capture_logs() as logs:
            assert previous_trading_session(date(2020, 1, 1)) is None
        events = [e for e in logs if e["event"] == "calendar.previous_session_coverage_exhausted"]
        assert len(events) == 1
        assert events[0]["requested_date"] == "2020-01-01"
        assert events[0]["probe_date"] == "2019-12-31"

    def test_out_of_coverage_weekend_start_still_abstains(self) -> None:
        # Mon 2019-06-03: the walk crosses Sun/Sat (data-complete non-sessions,
        # no raise) then hits the out-of-coverage Friday weekday → abstain.
        with capture_logs() as logs:
            assert previous_trading_session(date(2019, 6, 3)) is None
        events = [e for e in logs if e["event"] == "calendar.previous_session_coverage_exhausted"]
        assert len(events) == 1
        assert events[0]["probe_date"] == "2019-05-31"

    @pytest.mark.parametrize("exchange", [None, "NSE", "BSE"])
    def test_exchange_parity(self, exchange: str | None) -> None:
        # NSE ≡ BSE ≡ None for every covered year (the holiday sets are
        # identical 2020-2026), so the exchange arg is inert parity plumbing.
        for d in (date(2024, 7, 10), date(2025, 9, 29), date(2024, 3, 26), date(2025, 2, 3)):
            assert previous_trading_session(d, exchange=exchange) == previous_trading_session(d)

    def test_bad_exchange_raises_value_error_eagerly(self) -> None:
        with pytest.raises(ValueError, match="unknown exchange"):
            previous_trading_session(date(2024, 7, 10), exchange="LSE")
