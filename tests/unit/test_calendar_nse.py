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

from datetime import date, time

import pytest
from brutex.calendars.nse import (
    SESSION_DURATION_MINUTES,
    TICK_SIZE_PAISA,
    SessionType,
    expected_bars_per_day_for_tf,
    get_session,
    has_muhurat_session,
    is_forced_exit_bar,
    is_holiday,
    is_pre_open,
    is_trading_day,
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
