"""Unit tests for brutex.indicators.weekly_fib_levels (DEC-VOCAB-001)."""

from __future__ import annotations

from datetime import date

import pytest
from brutex.indicators.fib_levels import FibDirection
from brutex.indicators.weekly_fib_levels import (
    WEEKLY_FIB_RATIOS_RETRACEMENT,
    compute_weekly_fib_levels,
    previous_trading_week_bounds,
)


class TestWeeklyFibRatios:
    def test_ratios_match_standard_fibonacci_sequence(self) -> None:
        assert WEEKLY_FIB_RATIOS_RETRACEMENT["0"] == 0.0
        assert WEEKLY_FIB_RATIOS_RETRACEMENT["23"] == pytest.approx(0.236)
        assert WEEKLY_FIB_RATIOS_RETRACEMENT["38"] == pytest.approx(0.382)
        assert WEEKLY_FIB_RATIOS_RETRACEMENT["50"] == 0.5
        assert WEEKLY_FIB_RATIOS_RETRACEMENT["62"] == pytest.approx(0.618)
        assert WEEKLY_FIB_RATIOS_RETRACEMENT["78"] == pytest.approx(0.786)
        assert WEEKLY_FIB_RATIOS_RETRACEMENT["100"] == 1.0


class TestPreviousTradingWeekBounds:
    """previous_trading_week_bounds() handles weekends + holidays correctly."""

    def test_normal_mid_week_returns_previous_full_week(self) -> None:
        """Today is a Wed → previous week is the Mon-Fri before."""
        # Wed 14-May-2026.
        today = date(2026, 5, 14)
        # All weekdays in the previous week (5-9 May 2026) are trading days.
        all_trading = lambda d: d.weekday() < 5  # noqa: E731

        start, end = previous_trading_week_bounds(today, all_trading)
        # Previous week of Mon 12-May-2026 is week of Mon 05-May-2026.
        assert start == date(2026, 5, 4)  # Mon
        assert end == date(2026, 5, 8)  # Fri

    def test_skips_monday_holiday(self) -> None:
        """If Monday is a holiday, week_start moves to Tuesday."""
        today = date(2026, 5, 14)
        # Monday 4-May is a holiday; rest of week trades normally.
        def monday_holiday(d):
            return (d.weekday() < 5 and d != date(2026, 5, 4))
        start, end = previous_trading_week_bounds(today, monday_holiday)
        assert start == date(2026, 5, 5)  # Tuesday
        assert end == date(2026, 5, 8)

    def test_skips_friday_holiday(self) -> None:
        """If Friday is a holiday, week_end moves to Thursday."""
        today = date(2026, 5, 14)
        def friday_holiday(d):
            return (d.weekday() < 5 and d != date(2026, 5, 8))
        start, end = previous_trading_week_bounds(today, friday_holiday)
        assert start == date(2026, 5, 4)
        assert end == date(2026, 5, 7)  # Thursday


class TestComputeWeeklyFibLevels:
    """compute_weekly_fib_levels matches the Fib ratios bit-exact."""

    def test_simple_range_levels(self) -> None:
        # Week: PWH=10000, PWL=9000, range=1000
        levels = compute_weekly_fib_levels(
            trading_day=date(2026, 5, 14),
            week_start=date(2026, 5, 4),
            week_end=date(2026, 5, 8),
            pwh_paisa=10000,
            pwl_paisa=9000,
        )
        assert levels.pwh_paisa == 10000
        assert levels.pwl_paisa == 9000
        assert levels.range_paisa == 1000
        # 50% = midpoint
        assert levels.retrace_50_paisa == 9500
        # 38% from PWH = 10000 - 382 = 9618
        assert levels.retrace_38_paisa == 9618
        # 62% from PWH = 10000 - 618 = 9382
        assert levels.retrace_62_paisa == 9382

    def test_negative_range_raises(self) -> None:
        with pytest.raises(ValueError, match="PWH"):
            compute_weekly_fib_levels(
                trading_day=date(2026, 5, 14),
                week_start=date(2026, 5, 4),
                week_end=date(2026, 5, 8),
                pwh_paisa=9000,
                pwl_paisa=10000,
            )

    def test_retracement_lookup_bearish(self) -> None:
        levels = compute_weekly_fib_levels(
            date(2026, 5, 14),
            date(2026, 5, 4),
            date(2026, 5, 8),
            pwh_paisa=10000,
            pwl_paisa=9000,
        )
        # Bearish: 0 = PWH (top), 100 = PWL (bottom)
        assert levels.get_retracement("0", FibDirection.BEARISH) == 10000
        assert levels.get_retracement("100", FibDirection.BEARISH) == 9000
        assert levels.get_retracement("50", FibDirection.BEARISH) == 9500

    def test_retracement_lookup_bullish(self) -> None:
        levels = compute_weekly_fib_levels(
            date(2026, 5, 14),
            date(2026, 5, 4),
            date(2026, 5, 8),
            pwh_paisa=10000,
            pwl_paisa=9000,
        )
        # Bullish: 0 = PWL (bottom), 100 = PWH (top)
        assert levels.get_retracement("0", FibDirection.BULLISH) == 9000
        assert levels.get_retracement("100", FibDirection.BULLISH) == 10000
        # 50 is symmetric.
        assert levels.get_retracement("50", FibDirection.BULLISH) == 9500
        # Bullish 23 = bearish 78 (flipped).
        assert levels.get_retracement("23", FibDirection.BULLISH) == levels.retrace_78_paisa

    def test_frozen_dataclass(self) -> None:
        levels = compute_weekly_fib_levels(
            date(2026, 5, 14), date(2026, 5, 4), date(2026, 5, 8), 10000, 9000
        )
        with pytest.raises((AttributeError, TypeError)):
            levels.pwh_paisa = 99999  # type: ignore[misc]


class TestEdgeCases:
    def test_zero_range_week(self) -> None:
        """Flat week (PWH == PWL) — all retracements collapse to same level."""
        levels = compute_weekly_fib_levels(
            date(2026, 5, 14), date(2026, 5, 4), date(2026, 5, 8), 10000, 10000
        )
        assert levels.range_paisa == 0
        assert levels.retrace_at_pwh_paisa == 10000
        assert levels.retrace_at_pwl_paisa == 10000
        assert levels.retrace_50_paisa == 10000

    def test_real_world_nifty_week(self) -> None:
        """Realistic NIFTY week: PWH=24500, PWL=24100, range=400."""
        levels = compute_weekly_fib_levels(
            date(2026, 5, 14),
            date(2026, 5, 4),
            date(2026, 5, 8),
            pwh_paisa=2_450_000,
            pwl_paisa=2_410_000,
        )
        assert levels.range_paisa == 40_000
        # 50% retracement = midpoint
        assert levels.retrace_50_paisa == 2_430_000
        # 38% from PWH = 2_450_000 - round(0.382 * 40_000)
        # = 2_450_000 - 15_280 = 2_434_720
        assert levels.retrace_38_paisa == 2_434_720
