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
    FORCED_EXIT_IST,
    IST,
    MUHURAT_SESSIONS,
    SESSION_DURATION_MINUTES,
    TICK_SIZE_PAISA,
    SessionType,
    expected_bars_per_day_for_tf,
    forced_exit_time_for_session,
    get_session,
    has_muhurat_session,
    is_forced_exit_bar,
    is_holiday,
    is_nifty_rebalance_day,
    is_pre_open,
    is_special_weekend_session,
    is_trading_day,
    is_weekend,
    nifty_rebalance_event_on,
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

    def test_map_lookup_agrees_with_linear_scan_for_every_event(self) -> None:
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
    """Dates outside the curated 2021-2026 holiday window degrade gracefully
    (never crash): only the weekend rule applies, holidays read False."""

    def test_pre_2021_date_not_a_listed_holiday(self) -> None:
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


class TestLeapYear:
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
