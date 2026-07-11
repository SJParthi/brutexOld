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
from typing import ClassVar, Never

import pytest
from brutex.calendars import nse as nse_module
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
    daily_derive_cutoff_grounded,
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
    now_ist,
    previous_trading_session,
    session_last_bar_complete,
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


class TestNowIst:
    """The ONE production IST clock leaf (DEC-DAILY-CUTOFF-GROUNDED-001)."""

    def test_now_ist_is_ist_aware_and_current(self) -> None:
        from datetime import UTC

        before = datetime.now(UTC)
        instant = now_ist()
        after = datetime.now(UTC)
        assert instant.tzinfo is not None
        assert "Kolkata" in str(instant.tzinfo)
        assert before <= instant <= after  # aware comparison — same instant, IST label


class TestSessionLastBarComplete:
    """The +1min completeness predicate (DEC-DAILY-CUTOFF-GROUNDED-001):
    the ingest filter is half-open [open, close), so a COMPLETE regular
    session's LAST stored 1m bar is 15:29:00 — completeness must be tested
    as ``last_bar + 1min >= close``, never ``last_bar >= close``."""

    @staticmethod
    def _at(d: date, t: time) -> datetime:
        return datetime.combine(d, t, tzinfo=IST)

    def test_regular_session_last_bar_1529_is_complete(self) -> None:
        d = date(2026, 7, 1)  # regular Wednesday
        session = get_session(d)
        assert session is not None
        last = self._at(d, time(15, 29))
        # The +1min rule is LOAD-BEARING: the raw >= comparison never fires.
        assert last < self._at(d, SESSION_CLOSE_IST)
        assert session_last_bar_complete(session, last)

    def test_regular_session_last_bar_1528_is_incomplete(self) -> None:
        d = date(2026, 7, 1)
        session = get_session(d)
        assert session is not None
        assert not session_last_bar_complete(session, self._at(d, time(15, 28)))

    def test_sub_minute_label_within_final_minute_is_complete(self) -> None:
        # A 15:29:30-labelled bar: +1min = 15:30:30 >= 15:30 ⇒ complete.
        d = date(2026, 7, 1)
        session = get_session(d)
        assert session is not None
        assert session_last_bar_complete(session, self._at(d, time(15, 29, 30)))

    def test_muhurat_close_honored(self) -> None:
        # 2025-10-21 Muhurat 13:45-14:45: the final stored bar is 14:44.
        d = date(2025, 10, 21)
        session = get_session(d)
        assert session is not None
        assert session.session_type is SessionType.MUHURAT
        assert session_last_bar_complete(session, self._at(d, time(14, 44)))
        assert not session_last_bar_complete(session, self._at(d, time(14, 43)))


class TestDailyDeriveCutoffGrounded:
    """The DEFER-only data-grounded cutoff (DEC-DAILY-CUTOFF-GROUNDED-001):
    ``result ∈ {clock, run_day}`` and ``result <= clock`` always — it can
    demote a post-close "run day closed" clock verdict when the frame's own
    last bar proves the session incomplete, never promote past it."""

    @staticmethod
    def _at(d: date, t: time) -> datetime:
        return datetime.combine(d, t, tzinfo=IST)

    # ── abstentions: every unknown falls back to the clock verdict ──────

    def test_none_last_bar_is_clock_parity(self) -> None:
        now = self._at(date(2026, 7, 1), time(16, 0))  # post-close
        assert daily_derive_cutoff_grounded(now, last_bar_ist=None) == daily_derive_cutoff(now)

    def test_mid_session_clock_verdict_short_circuits(self) -> None:
        # clock <= today ⇒ the run day is already excluded — grounding never
        # evaluates the frame (byte-identical mid-session by construction),
        # even for a provably-incomplete run-day last bar.
        d = date(2026, 7, 1)
        now = self._at(d, time(12, 0))
        assert daily_derive_cutoff(now) == d
        assert daily_derive_cutoff_grounded(now, last_bar_ist=self._at(d, time(10, 0))) == d

    def test_exactly_at_close_keeps_clock_today(self) -> None:
        # The strict-`>` at-close pin rides through the grounded arm unchanged.
        d = date(2026, 7, 1)
        now = self._at(d, SESSION_CLOSE_IST)
        assert daily_derive_cutoff_grounded(now, last_bar_ist=self._at(d, time(15, 29))) == d

    def test_prior_day_last_bar_pre_and_post_close_is_clock_parity(self) -> None:
        # Run-day-only scoping: yesterday's (even incomplete) last bar never
        # re-opens history — no permanent-deferral spiral.
        d = date(2026, 7, 1)
        stale = self._at(d - timedelta(days=1), time(12, 0))
        post_close = self._at(d, time(16, 0))
        assert daily_derive_cutoff_grounded(post_close, last_bar_ist=stale) == d + timedelta(days=1)
        mid = self._at(d, time(12, 0))
        assert daily_derive_cutoff_grounded(mid, last_bar_ist=stale) == d

    def test_future_dated_last_bar_is_clock_parity(self) -> None:
        # Clock skew / corruption escaping drop_bars_after: abstain, never defer.
        d = date(2026, 7, 1)
        now = self._at(d, time(16, 0))
        future = self._at(d + timedelta(days=1), time(10, 0))
        assert daily_derive_cutoff_grounded(now, last_bar_ist=future) == d + timedelta(days=1)

    def test_run_day_bar_on_non_trading_date_is_clock_parity(self) -> None:
        # Bars stored on a calendar-None date are filter-impossible — defensive.
        d = date(2026, 7, 5)  # Sunday, no special session
        assert get_session(d) is None
        now = self._at(d, time(12, 0))
        assert daily_derive_cutoff_grounded(now, last_bar_ist=self._at(d, time(10, 0))) == (
            d + timedelta(days=1)
        )

    # ── the grounded verdicts on the run day, post-close ─────────────────

    def test_complete_run_day_session_keeps_clock(self) -> None:
        # Last bar 15:29 (+1min == the 15:30 close) ⇒ the clock's today+1 stands.
        d = date(2026, 7, 1)
        now = self._at(d, time(16, 0))
        assert daily_derive_cutoff_grounded(
            now, last_bar_ist=self._at(d, time(15, 29))
        ) == d + timedelta(days=1)

    def test_incomplete_run_day_session_defers_to_today(self) -> None:
        # The sanctioned defer: a pull died mid-day (last bar 12:xx / 15:28) —
        # never finalize a partial daily bar. Kills the >=-vs-> and
        # +1min-dropped mutations together with the 15:29-complete pin above.
        d = date(2026, 7, 1)
        now = self._at(d, time(16, 0))
        assert daily_derive_cutoff_grounded(now, last_bar_ist=self._at(d, time(15, 28))) == d
        assert daily_derive_cutoff_grounded(now, last_bar_ist=self._at(d, time(12, 0))) == d

    def test_muhurat_grounding_against_its_own_close(self) -> None:
        # 2025-10-21 Muhurat 13:45-14:45: complete = last bar 14:44; a 14:43
        # last bar defers — the close comes from get_session, never 15:30.
        d = date(2025, 10, 21)
        now = self._at(d, time(20, 0))
        assert daily_derive_cutoff_grounded(
            now, last_bar_ist=self._at(d, time(14, 44))
        ) == d + timedelta(days=1)
        assert daily_derive_cutoff_grounded(now, last_bar_ist=self._at(d, time(14, 43))) == d

    def test_budget_sunday_special_weekend_grounding(self) -> None:
        # 2026-02-01 Sunday Budget session (09:15-15:30): the special-weekend
        # close drives the predicate on a date a plain weekend rule would skip.
        d = date(2026, 2, 1)
        assert d in SPECIAL_WEEKEND_TRADING_SESSIONS
        now = self._at(d, time(16, 0))
        assert daily_derive_cutoff_grounded(
            now, last_bar_ist=self._at(d, time(15, 29))
        ) == d + timedelta(days=1)
        assert daily_derive_cutoff_grounded(now, last_bar_ist=self._at(d, time(11, 0))) == d


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

    # ── observed-sessions overlay kwarg (DEC-PREOPEN-PREVDAY-COMMON-002) ──

    def test_observed_default_is_byte_identical_parity(self) -> None:
        # The new kwarg's default frozenset() keeps every existing caller
        # byte-identical: explicit-empty == omitted for every walk shape
        # (plain weekday / weekend / holiday cluster / Budget Saturday /
        # coverage abstain).
        for d in (
            date(2024, 7, 10),
            date(2025, 9, 29),
            date(2024, 3, 26),
            date(2025, 2, 3),
        ):
            assert previous_trading_session(d, observed=frozenset()) == previous_trading_session(d)
        with capture_logs():
            assert previous_trading_session(date(2020, 1, 1), observed=frozenset()) is None

    def test_observed_member_trading_day_is_stepped_over(self) -> None:
        # REMOVE-ONLY: an overlaid Tuesday 2026-05-12 (a derived trading day)
        # stops being a session — the walk from Wed 2026-05-13 steps over it
        # to Mon 2026-05-11, exactly like a holiday.
        assert previous_trading_session(date(2026, 5, 13)) == date(2026, 5, 12)
        assert previous_trading_session(
            date(2026, 5, 13), observed=frozenset({date(2026, 5, 12)})
        ) == date(2026, 5, 11)

    def test_observed_is_remove_only_never_add(self) -> None:
        # A member can only flip True -> False: overlaying a weekend/holiday
        # (already a non-session) changes NOTHING, and the result with any
        # overlay is never AFTER the un-overlaid result and never a member.
        plain = previous_trading_session(date(2025, 9, 29))
        assert plain == date(2025, 9, 26)
        # Overlaying the in-between Saturday/Sunday is a no-op (False stays False).
        weekend = frozenset({date(2025, 9, 27), date(2025, 9, 28)})
        assert previous_trading_session(date(2025, 9, 29), observed=weekend) == plain
        # Overlaying the answer itself pushes the walk STRICTLY further back.
        overlaid = previous_trading_session(
            date(2025, 9, 29), observed=frozenset({date(2025, 9, 26)})
        )
        assert overlaid == date(2025, 9, 25)
        assert plain is not None and overlaid is not None
        assert overlaid < plain

    def test_observed_large_set_still_terminates(self) -> None:
        # Termination with a LARGE overlay: every 2025 trading session
        # overlaid -> the walk from 2026-01-01 crosses the whole year and
        # lands on the last 2024 session (bounded by the coverage floor, so
        # the loop can never spin forever).
        all_2025 = frozenset(trading_days_between(date(2025, 1, 1), date(2025, 12, 31)))
        assert len(all_2025) > 200
        got = previous_trading_session(date(2026, 1, 1), observed=all_2025)
        assert got == date(2024, 12, 31)

    @pytest.mark.parametrize("exchange", [None, "NSE", "BSE"])
    def test_observed_exchange_parity(self, exchange: str | None) -> None:
        # NSE ≡ BSE ≡ None with an overlay too (the holiday sets are identical
        # for every covered year; the overlay is exchange-agnostic data).
        obs = frozenset({date(2026, 5, 12)})
        assert previous_trading_session(
            date(2026, 5, 13), exchange=exchange, observed=obs
        ) == previous_trading_session(date(2026, 5, 13), observed=obs)

    def test_bad_exchange_with_observed_still_raises_eagerly(self) -> None:
        with pytest.raises(ValueError, match="unknown exchange"):
            previous_trading_session(
                date(2024, 7, 10), exchange="LSE", observed=frozenset({date(2024, 7, 9)})
            )


# ────────────────────────────────────────────────────────────────────────
# Rust calendar-verdict kernel seam (DEC-CALENDAR-VERDICT-RUST-001, ADR-0019)
# ────────────────────────────────────────────────────────────────────────


class _FakeEngine:
    """A wrong-on-purpose engine for the burn-in autorevert arms."""

    def __init__(self, **overrides: object) -> None:
        self._overrides = overrides

    def __getattr__(self, name: str) -> object:
        if name in self._overrides:
            value = self._overrides[name]
            return lambda *args: value
        raise AssertionError(f"fake engine method {name!r} must not be called")


def _fake_kernel(**overrides: object) -> nse_module._RustCalendarKernel:
    """A `_RustCalendarKernel` around :class:`_FakeEngine` (scalar fns raise)."""

    def _never(*args: int) -> Never:
        raise AssertionError("fake scalar fn must not be called")

    return nse_module._RustCalendarKernel(
        engine=_FakeEngine(**overrides),
        expected_bars=_never,
        forced_exit_bar=_never,
        forced_exit_seconds=_never,
        last_bar_complete=_never,
    )


class TestRustCalendarSeam:
    """The flag-gated Rust dispatch: flag arms, engine build, both-arms
    verdict identity across every ported function, burn-in + autorevert."""

    REGULAR_MON: ClassVar[date] = date(2026, 7, 6)
    SATURDAY: ClassVar[date] = date(2026, 7, 4)
    HOLIDAY: ClassVar[date] = date(2026, 1, 26)
    MUHURAT: ClassVar[date] = date(2025, 10, 21)
    BUDGET_SAT: ClassVar[date] = date(2025, 2, 1)
    OOC_WEEKDAY: ClassVar[date] = date(2027, 1, 1)
    OOC_WEEKEND: ClassVar[date] = date(2027, 1, 2)
    PRE_COVERAGE: ClassVar[date] = date(2019, 12, 30)

    PROBES: ClassVar[tuple[date, ...]] = (
        REGULAR_MON,
        SATURDAY,
        HOLIDAY,
        MUHURAT,
        BUDGET_SAT,
        OOC_WEEKDAY,
        OOC_WEEKEND,
        PRE_COVERAGE,
    )

    @pytest.fixture(autouse=True)
    def _fresh_seam(self, monkeypatch: pytest.MonkeyPatch) -> object:
        """Every test starts and ends with a fresh-process seam view."""
        monkeypatch.delenv(nse_module.RUST_CALENDAR_ENV, raising=False)
        nse_module._reset_rust_calendar_for_tests()
        yield
        nse_module._reset_rust_calendar_for_tests()

    def _arm(self, monkeypatch: pytest.MonkeyPatch, *, burn_in_remaining: int = 16) -> None:
        monkeypatch.setenv(nse_module.RUST_CALENDAR_ENV, "1")
        nse_module._reset_rust_calendar_for_tests(burn_in_remaining=burn_in_remaining)

    # ── flag + resolution arms ───────────────────────────────────────────

    def test_flag_default_off_resolves_no_kernel(self) -> None:
        assert nse_module._rust_calendar() is None
        # Memoized: the second probe hits the resolved cell.
        assert nse_module._rust_calendar() is None

    def test_flag_falsy_off_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(nse_module.RUST_CALENDAR_ENV, "off")
        nse_module._reset_rust_calendar_for_tests()
        with capture_logs() as logs:
            assert nse_module._rust_calendar() is None
        assert logs == []

    def test_flag_junk_warns_once_and_stays_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(nse_module.RUST_CALENDAR_ENV, "0n")
        nse_module._reset_rust_calendar_for_tests()
        with capture_logs() as logs:
            assert nse_module._cal_flag_enabled() is False
            assert nse_module._cal_flag_enabled() is False  # warn once
        assert [r["event"] for r in logs] == ["calendar.rust_flag_unrecognised"]

    def test_flag_on_builds_the_real_engine_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("fno_math")
        self._arm(monkeypatch)
        kernel = nse_module._rust_calendar()
        assert kernel is not None
        assert nse_module._rust_calendar() is kernel  # memoized

    def test_wheel_missing_warns_once_and_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._arm(monkeypatch)

        def _no_wheel() -> object:
            raise ImportError("no fno_math")

        monkeypatch.setattr(nse_module, "_import_fno_math", _no_wheel)
        with capture_logs() as logs:
            assert nse_module._rust_calendar() is None
        assert [r["event"] for r in logs] == ["calendar.rust_kernel_unavailable"]
        assert logs[0]["reason"] == "wheel_missing"
        # The verdicts still serve from the Python oracle.
        assert is_trading_day(self.REGULAR_MON) is True

    @pytest.mark.parametrize(
        "stale_attrs",
        [
            (),
            ("CalendarVerdictEngine",),
            ("CalendarVerdictEngine", "expected_bars_per_day_for_tf"),
            (
                "CalendarVerdictEngine",
                "expected_bars_per_day_for_tf",
                "is_forced_exit_bar_s",
            ),
            (
                "CalendarVerdictEngine",
                "expected_bars_per_day_for_tf",
                "is_forced_exit_bar_s",
                "forced_exit_seconds_s",
            ),
        ],
        ids=["empty", "engine-only", "no-forced-exit", "no-seconds", "no-last-bar"],
    )
    def test_wheel_stale_warns_and_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, stale_attrs: tuple[str, ...]
    ) -> None:
        import types

        self._arm(monkeypatch)
        ns = types.SimpleNamespace(**{name: (lambda *a: 0) for name in stale_attrs})
        monkeypatch.setattr(nse_module, "_import_fno_math", lambda: ns)
        with capture_logs() as logs:
            assert nse_module._rust_calendar() is None
        assert [r["event"] for r in logs] == ["calendar.rust_kernel_unavailable"]
        assert logs[0]["reason"] == "wheel_stale"

    # ── both-arms verdict identity (burn-in ACTIVE — the compare window) ──

    def test_every_ported_function_matches_the_oracle_under_burn_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("fno_math")
        self._arm(monkeypatch, burn_in_remaining=10_000)
        for d in self.PROBES:
            for exchange in (None, "NSE", "BSE"):
                assert (
                    is_holiday(d, exchange=exchange)
                    == nse_module._py_is_holiday_checked(d, exchange)
                    if is_holiday_data_available(d)
                    else True
                )
                assert is_trading_day(d, exchange=exchange) == nse_module._py_is_trading_day(
                    d, exchange
                )
                assert get_session(d, exchange=exchange) == nse_module._py_get_session(d, exchange)
            assert is_weekend(d) == (d.weekday() >= 5)
            assert has_muhurat_session(d) == (d in MUHURAT_SESSIONS)
            assert is_special_weekend_session(d) == (d in SPECIAL_WEEKEND_TRADING_SESSIONS)
        # The checked variants: value AND raise arms both compare clean.
        assert is_trading_day_checked(self.REGULAR_MON) is True
        assert is_trading_day_checked(self.HOLIDAY) is False
        assert is_holiday_checked(self.HOLIDAY) is True
        assert get_session_checked(self.MUHURAT) is not None
        assert get_session_checked(self.HOLIDAY) is None
        with pytest.raises(HolidayDataUnavailableError):
            is_trading_day_checked(self.OOC_WEEKDAY)
        with pytest.raises(HolidayDataUnavailableError):
            get_session_checked(self.OOC_WEEKDAY)
        # Walks + scalars.
        assert previous_trading_session(date(2026, 7, 7)) == self.REGULAR_MON
        assert previous_trading_session(date(2020, 1, 1)) is None  # abstain
        assert trading_days_between(date(2026, 7, 3), date(2026, 7, 7)) == [
            date(2026, 7, 3),
            self.REGULAR_MON,
            date(2026, 7, 7),
        ]
        assert trading_days_between_checked(date(2026, 7, 3), date(2026, 7, 7)) == [
            date(2026, 7, 3),
            self.REGULAR_MON,
            date(2026, 7, 7),
        ]
        with pytest.raises(HolidayDataUnavailableError):
            trading_days_between_checked(date(2026, 12, 28), date(2027, 1, 5))
        assert daily_derive_cutoff(datetime(2026, 7, 6, 15, 30, 0)) == self.REGULAR_MON
        assert expected_bars_per_day_for_tf(60) == 7
        assert is_forced_exit_bar(time(15, 19), 1) is False
        assert is_forced_exit_bar(time(15, 19, 1), 1) is True
        session = get_session(self.REGULAR_MON)
        assert session is not None
        assert forced_exit_seconds_for_session(session) == 55_200
        assert session_last_bar_complete(session, datetime(2026, 7, 6, 15, 29)) is True
        # ZERO mismatches: the kernel is still engaged and EVERY surface's
        # own budget was consumed (per-surface dict — the audit pattern).
        assert nse_module._CAL_DISABLED_REASON[0] is None
        assert all(v < 10_000 for v in nse_module._CAL_BURN_IN_REMAINING.values())

    # ── steady state (burn-in exhausted — genuinely Rust-served) ─────────

    def test_steady_state_serves_rust_verdicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("fno_math")
        self._arm(monkeypatch, burn_in_remaining=0)
        assert is_trading_day(self.REGULAR_MON) is True
        assert is_trading_day(self.HOLIDAY) is False
        assert is_holiday(self.HOLIDAY) is True
        assert is_weekend(self.SATURDAY) is True
        assert has_muhurat_session(self.MUHURAT) is True
        assert is_special_weekend_session(self.BUDGET_SAT) is True
        assert is_holiday_checked(self.HOLIDAY) is True
        assert is_trading_day_checked(self.BUDGET_SAT) is True
        session = get_session(self.MUHURAT)
        assert session is not None and session.session_type is SessionType.MUHURAT
        assert get_session(self.HOLIDAY) is None
        checked = get_session_checked(self.REGULAR_MON)
        assert checked is not None and checked.open_time_ist == SESSION_OPEN_IST
        assert get_session_checked(self.HOLIDAY) is None
        assert previous_trading_session(date(2026, 7, 7)) == self.REGULAR_MON
        assert trading_days_between(date(2026, 7, 4), date(2026, 7, 5)) == []
        assert trading_days_between_checked(date(2026, 7, 4), date(2026, 7, 5)) == []
        assert expected_bars_per_day_for_tf(375) == 1
        assert is_forced_exit_bar(time(15, 20), 5) is True
        assert daily_derive_cutoff(datetime(2026, 7, 6, 15, 30, 0, 1)) == date(2026, 7, 7)
        assert daily_derive_cutoff(datetime(2026, 7, 6, 15, 30, 0)) == self.REGULAR_MON
        session = get_session(self.REGULAR_MON)
        assert session is not None
        assert forced_exit_seconds_for_session(session) == 55_200
        assert session_last_bar_complete(session, datetime(2026, 7, 6, 15, 28)) is False
        assert nse_module._CAL_DISABLED_REASON[0] is None

    def test_steady_state_checked_raise_matches_the_python_arm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Post burn-in verdict-2 routes through the SHARED WARN+raise —
        event fields and message byte-equal to the Python arm."""
        pytest.importorskip("fno_math")

        def _observe() -> tuple[dict[str, object], str]:
            with capture_logs() as logs, pytest.raises(HolidayDataUnavailableError) as exc_info:
                is_trading_day_checked(self.OOC_WEEKDAY)
            (record,) = logs
            return dict(record), str(exc_info.value)

        python_event, python_msg = _observe()  # flag OFF (fixture default)
        self._arm(monkeypatch, burn_in_remaining=0)
        rust_event, rust_msg = _observe()
        assert rust_event == python_event
        assert rust_msg == python_msg

    def test_steady_state_prev_session_abstain_warns_identically(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("fno_math")

        def _observe() -> tuple[list[dict[str, object]], date | None]:
            with capture_logs() as logs:
                result = previous_trading_session(date(2020, 1, 1))
            return [dict(r) for r in logs], result

        python_logs, python_result = _observe()
        self._arm(monkeypatch, burn_in_remaining=0)
        rust_logs, rust_result = _observe()
        assert rust_result is None and python_result is None
        assert rust_logs == python_logs
        # BOTH arms emit the probe's unavailable WARN then the exhausted WARN.
        assert [r["event"] for r in rust_logs] == [
            "calendar.holiday_data_unavailable",
            "calendar.previous_session_coverage_exhausted",
        ]

    def test_steady_state_range_checked_raises_on_the_offending_date(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("fno_math")
        self._arm(monkeypatch, burn_in_remaining=0)
        with pytest.raises(HolidayDataUnavailableError, match="2027-01-01"):
            trading_days_between_checked(date(2026, 12, 28), date(2027, 1, 5))

    # ── burn-in autorevert arms (fake kernels, wrong on purpose) ─────────

    def test_serve_value_mismatch_ships_python_and_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._arm(monkeypatch)
        nse_module._set_rust_calendar_for_tests(_fake_kernel(is_trading_day=False))
        with capture_logs() as logs:
            assert is_trading_day(self.REGULAR_MON) is True  # the PYTHON verdict ships
        assert [r["event"] for r in logs] == ["calendar.kernel_autoreverted"]
        assert nse_module._CAL_DISABLED_REASON[0] == "is_trading_day"
        # Latched: the kernel handle is gone; later calls are pure Python.
        assert nse_module._rust_calendar() is None
        assert is_trading_day(self.REGULAR_MON) is True
        # A second disable is a no-op (no duplicate ERROR).
        with capture_logs() as logs:
            nse_module._disable_rust_calendar("again", rust="r", python="p")
        assert logs == []

    def test_serve_checked_false_raise_verdict_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rust says RAISE for an in-coverage date → python value ships."""
        self._arm(monkeypatch)
        nse_module._set_rust_calendar_for_tests(_fake_kernel(is_trading_day_checked=2))
        with capture_logs() as logs:
            assert is_trading_day_checked(self.REGULAR_MON) is True
        assert [r["event"] for r in logs] == ["calendar.kernel_autoreverted"]

    def test_serve_checked_missed_raise_verdict_latches_and_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rust says True for an OOC weekday the oracle refuses → the oracle's
        raise replays (WARN once, from the oracle) and the kernel latches."""
        self._arm(monkeypatch)
        nse_module._set_rust_calendar_for_tests(_fake_kernel(is_trading_day_checked=1))
        with capture_logs() as logs, pytest.raises(HolidayDataUnavailableError):
            is_trading_day_checked(self.OOC_WEEKDAY)
        events = [r["event"] for r in logs]
        assert events == ["calendar.holiday_data_unavailable", "calendar.kernel_autoreverted"]

    def test_serve_checked_matched_raise_consumes_burn_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("fno_math")
        self._arm(monkeypatch, burn_in_remaining=2)
        with pytest.raises(HolidayDataUnavailableError):
            is_trading_day_checked(self.OOC_WEEKDAY)
        assert nse_module._CAL_BURN_IN_REMAINING["is_trading_day_checked"] == 1
        # Only ITS surface consumed — every other budget is untouched.
        assert all(
            v == 2
            for s, v in nse_module._CAL_BURN_IN_REMAINING.items()
            if s != "is_trading_day_checked"
        )
        assert nse_module._CAL_DISABLED_REASON[0] is None

    def test_prev_session_mismatch_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._arm(monkeypatch)
        nse_module._set_rust_calendar_for_tests(
            _fake_kernel(previous_trading_session=(1, date(2020, 1, 1).toordinal()))
        )
        with capture_logs() as logs:
            assert previous_trading_session(date(2026, 7, 7)) == self.REGULAR_MON
        assert [r["event"] for r in logs] == ["calendar.kernel_autoreverted"]

    def test_post_burn_in_fake_verdict_is_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With the budget exhausted the (fake) Rust verdict serves directly —
        the proof the steady state genuinely serves the kernel."""
        self._arm(monkeypatch, burn_in_remaining=0)
        nse_module._set_rust_calendar_for_tests(_fake_kernel(is_trading_day=False))
        assert is_trading_day(self.REGULAR_MON) is False  # the fake verdict

    # ── guards + plumbing ────────────────────────────────────────────────

    def test_invalid_exchange_raises_eagerly_before_any_rust_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._arm(monkeypatch, burn_in_remaining=0)
        nse_module._set_rust_calendar_for_tests(_fake_kernel())  # any touch would raise
        with pytest.raises(ValueError, match="unknown exchange"):
            is_trading_day(self.REGULAR_MON, exchange="LSE")

    def test_tf_validation_raises_before_any_rust_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._arm(monkeypatch, burn_in_remaining=0)
        nse_module._set_rust_calendar_for_tests(_fake_kernel())
        with pytest.raises(ValueError, match="tf_minutes must be >= 1"):
            expected_bars_per_day_for_tf(0)
        with pytest.raises(ValueError, match="tf_minutes must be >= 1"):
            is_forced_exit_bar(time(15, 0), 0)

    def test_huge_in_i64_tf_saturates_never_aborts_and_matches_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Slice-9 edge MINOR-1 regression: ``tf=10**18`` previously
        SIGABRTed the whole process (`tf * 60` overflow under the release
        ``overflow-checks = true`` / ``panic = "abort"`` profile). The
        kernel now SATURATES; the burn-in compare (budget > 0 here) proves
        the Rust verdict equals the Python oracle's bigint result — and no
        autorevert latches."""
        pytest.importorskip("fno_math")
        self._arm(monkeypatch, burn_in_remaining=4)
        assert is_forced_exit_bar(time(9, 15), 10**18) is True
        assert is_forced_exit_bar(time(9, 15), 10**18, 55_200) is True
        assert is_forced_exit_bar(time(23, 59, 59), 2**63 - 1) is True  # i64::MAX exactly
        assert nse_module._CAL_DISABLED_REASON[0] is None

    def test_tf_beyond_i64_routes_to_the_oracle_never_the_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Slice-9 edge NIT-2: a ``tf > i64::MAX`` cannot cross the FFI
        (PyO3 would raise ``OverflowError`` where the Python arm computes
        fine) — both scalar wrappers route it to the oracle. The fake
        kernel's raising scalar fns prove the bypass."""
        self._arm(monkeypatch, burn_in_remaining=0)
        nse_module._set_rust_calendar_for_tests(_fake_kernel())
        beyond_i64 = 2**63  # _RUST_TF_MAX + 1
        assert expected_bars_per_day_for_tf(beyond_i64) == 1
        assert is_forced_exit_bar(time(9, 15), beyond_i64) is True
        assert nse_module._CAL_DISABLED_REASON[0] is None

    def test_boundary_beyond_u32_routes_to_the_oracle_never_the_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Slice-9 refuter-round-1 R2-NIT: a resolved ``boundary``
        (forced_exit_seconds) outside the kernel's ``u32`` domain cannot
        cross the FFI (PyO3 would raise ``OverflowError`` flag-ON where the
        flag-OFF Python arm returns a bool) — the wrapper now mirrors the
        ``_RUST_TF_MAX`` guard for it. The fake kernel's raising scalar fns
        prove the bypass on BOTH out-of-range directions."""
        self._arm(monkeypatch, burn_in_remaining=0)
        nse_module._set_rust_calendar_for_tests(_fake_kernel())
        beyond_u32 = 2**32  # _RUST_BOUNDARY_MAX + 1
        # 09:15 + 1m closes at 33_360s — far below the huge boundary → False.
        assert is_forced_exit_bar(time(9, 15), 1, beyond_u32) is False
        # A negative boundary equally cannot convert to u32 → oracle (True:
        # every close is strictly after a negative instant).
        assert is_forced_exit_bar(time(9, 15), 1, -1) is True
        assert nse_module._CAL_DISABLED_REASON[0] is None

    def test_boundary_beyond_u32_real_kernel_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refuter's flag-ON repro, killed: with the REAL wheel armed a
        ``forced_exit_seconds > u32::MAX`` returns the oracle's bool instead
        of an ``OverflowError`` at the PyO3 seam."""
        pytest.importorskip("fno_math")
        self._arm(monkeypatch, burn_in_remaining=4)
        assert is_forced_exit_bar(time(15, 15), 15, 2**32) is False
        assert is_forced_exit_bar(time(15, 15), 15, -1) is True
        assert nse_module._CAL_DISABLED_REASON[0] is None

    def test_cross_date_last_bar_stays_on_the_python_arm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cross-date pair never touches the kernel (the seconds-of-day
        port is same-date-only); the fake kernel raising proves the bypass."""
        self._arm(monkeypatch, burn_in_remaining=0)
        nse_module._set_rust_calendar_for_tests(_fake_kernel())
        session = nse_module._py_get_session(self.REGULAR_MON, None)
        assert session is not None
        next_day = datetime(2026, 7, 7, 9, 15)
        assert session_last_bar_complete(session, next_day) is True

    def test_consume_burnin_clamps_at_zero(self) -> None:
        nse_module._reset_rust_calendar_for_tests(burn_in_remaining=0)
        nse_module._cal_consume_burnin("is_trading_day")
        assert nse_module._CAL_BURN_IN_REMAINING["is_trading_day"] == 0

    def test_burn_in_budgets_are_independent_per_surface(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each surface owns its OWN burn-in budget (slice-9 refuter
        round-2 NIT, the audit MINOR-1 pattern — a shared budget let a hot
        ``is_trading_day`` loop exhaust the window before a rarer surface
        was ever validated)."""
        pytest.importorskip("fno_math")
        self._arm(monkeypatch, burn_in_remaining=2)
        assert is_trading_day(self.REGULAR_MON) is True
        assert is_trading_day(self.HOLIDAY) is False
        assert is_trading_day(self.SATURDAY) is False  # exhausted — clamps at 0
        assert is_weekend(self.SATURDAY) is True
        assert nse_module._CAL_BURN_IN_REMAINING["is_trading_day"] == 0
        assert nse_module._CAL_BURN_IN_REMAINING["is_weekend"] == 1
        # Every never-called surface's budget is untouched.
        assert all(
            v == 2
            for s, v in nse_module._CAL_BURN_IN_REMAINING.items()
            if s not in {"is_trading_day", "is_weekend"}
        )
        assert nse_module._CAL_DISABLED_REASON[0] is None

    def test_hot_surface_exhaustion_still_validates_a_rarer_surface(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The audit-MINOR-1 regression, calendar twin: a hot
        ``is_trading_day`` loop (that surface's budget exhausted) must
        STILL burn-in-validate the process's first ``is_weekend`` — a wrong
        kernel verdict ships the PYTHON value and latches, never serving
        the kernel value unvalidated."""
        self._arm(monkeypatch, burn_in_remaining=1)
        nse_module._set_rust_calendar_for_tests(
            _fake_kernel(is_trading_day=True, is_weekend=False)  # weekend wrong on purpose
        )
        # The hot-loop shape: is_trading_day calls only, exhausting THAT budget.
        assert is_trading_day(self.REGULAR_MON) is True
        assert nse_module._CAL_BURN_IN_REMAINING["is_trading_day"] == 0
        # is_weekend still owes its own burn-in: the wrong kernel verdict is
        # caught, the Python verdict ships, the kernel latches.
        with capture_logs() as logs:
            assert is_weekend(self.SATURDAY) is True
        assert [r["event"] for r in logs] == ["calendar.kernel_autoreverted"]
        assert nse_module._CAL_DISABLED_REASON[0] == "is_weekend"

    def test_reset_seam_restores_a_fresh_view(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._arm(monkeypatch, burn_in_remaining=0)
        nse_module._set_rust_calendar_for_tests(_fake_kernel())
        nse_module._disable_rust_calendar("test", rust="r", python="p")
        nse_module._reset_rust_calendar_for_tests()
        assert nse_module._CAL_DISABLED_REASON[0] is None
        assert all(
            v == nse_module.CALENDAR_BURN_IN_SAMPLES
            for v in nse_module._CAL_BURN_IN_REMAINING.values()
        )
        assert nse_module._CAL_KERNEL_RESOLVED[0] is False
