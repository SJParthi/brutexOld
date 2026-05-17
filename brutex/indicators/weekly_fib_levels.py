"""Weekly Fibonacci levels — anchored to Previous Week's High/Low.

Per DEC-VOCAB-001 (DEC entry in MIGRATION.md): extends the static Fib
indicator family to a weekly anchor. Same level structure as
`brutex/indicators/fib_levels.py` (daily) but anchored on the
**previous trading week's** high-to-low rather than yesterday's.

## Why weekly anchor?

Per operator (DEC-P0-006 follow-up question): manual rare-pattern
strategies often reference *multiple* Fib anchors simultaneously
(daily AND weekly AND pivot-based). The brute-force vocabulary must
cover all common anchors or it will miss whichever the operator's
real edge depends on.

## Trading week definition

A "week" for this indicator is **Monday-through-Friday IST trading
sessions**, excluding weekends + NSE/BSE holidays:

- Previous week's HIGH = max(high) across all trading sessions in the
  most recent completed Mon-Fri block strictly before `trading_day`.
- Previous week's LOW = min(low) across the same block.
- If the previous "week" had only 4 trading days (a Mon or Fri holiday
  shifted the boundary), we still use Mon-through-last-trading-day-of-week.

This matches the convention used by Indian institutional desks per
ETSA AmiBroker scripts (verified via operator screenshots referenced
in `brutex/indicators/fib_levels.py` docstring).

## Money representation

All levels in int64 paisa. Banker's rounding applied. Bit-exact
reproducibility per CLAUDE.md §4.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from brutex.indicators.fib_levels import FibDirection, _round_half_even

# Re-export shared Fib ratio constants.
WEEKLY_FIB_RATIOS_RETRACEMENT: Final[dict[str, float]] = {
    "0": 0.0,
    "23": 0.236,
    "38": 0.382,
    "50": 0.500,
    "62": 0.618,
    "78": 0.786,
    "100": 1.000,
}


@dataclass(frozen=True)
class WeeklyFibLevels:
    """Static weekly Fibonacci anchor levels for one (instrument, trading_day).

    Mirrors :class:`FibLevels` but anchored on the previous trading
    week's high/low rather than yesterday's.
    """

    trading_day: date
    week_start: date  # Monday of the *previous* completed week
    week_end: date  # Friday (or last trading day) of the previous week
    pwh_paisa: int  # Previous Week's High
    pwl_paisa: int  # Previous Week's Low
    range_paisa: int

    # Retracement levels (same in both bullish + bearish conventions —
    # only the label flips). Per `fib_levels.py` convention.
    retrace_at_pwh_paisa: int
    retrace_23_paisa: int
    retrace_38_paisa: int
    retrace_50_paisa: int
    retrace_62_paisa: int
    retrace_78_paisa: int
    retrace_at_pwl_paisa: int

    def get_retracement(self, key: str, direction: FibDirection) -> int:
        """Lookup retracement level by key + direction convention.

        Bearish reads top-down (0=PWH..100=PWL); bullish flips.
        """
        bearish_map = {
            "0": self.retrace_at_pwh_paisa,
            "23": self.retrace_23_paisa,
            "38": self.retrace_38_paisa,
            "50": self.retrace_50_paisa,
            "62": self.retrace_62_paisa,
            "78": self.retrace_78_paisa,
            "100": self.retrace_at_pwl_paisa,
        }
        if direction == FibDirection.BEARISH:
            return bearish_map[key]
        # Bullish reads bottom-up — flip the keys.
        flip = {"0": "100", "23": "78", "38": "62", "50": "50", "62": "38", "78": "23", "100": "0"}
        return bearish_map[flip[key]]


def previous_trading_week_bounds(
    trading_day: date,
    is_trading_day_fn: object,  # callable: (date) -> bool — injected for testability
) -> tuple[date, date]:
    """Compute the (start, end) date pair for the previous trading week.

    Definition: the most recent Mon-through-Fri block strictly before
    ``trading_day``, with non-trading days (weekends + holidays)
    skipped at the boundaries.

    Args:
        trading_day: Today's date.
        is_trading_day_fn: Callable returning True if a date is a trading
            day (passes NSE/BSE calendar gate). Injected so callers can
            wire either the production calendar or a test fixture.

    Returns:
        (week_start_monday, week_end_friday_or_earlier) — both inclusive
        + guaranteed to be trading days.

    Raises:
        ValueError: if no trading days found in the lookback window (60
            days max — should never happen for valid Indian market data).
    """
    # Step 1: walk back from trading_day to the most recent Monday that's
    # strictly before this week's Monday.
    today_dow = trading_day.weekday()  # Mon=0, Sun=6
    days_back_to_this_monday = today_dow
    this_monday = trading_day - timedelta(days=days_back_to_this_monday)
    last_monday = this_monday - timedelta(days=7)

    # Step 2: advance from last_monday to first trading day (handle
    # week-starting holidays).
    week_start = last_monday
    for _ in range(7):  # Mon-Sun search window
        if is_trading_day_fn(week_start):  # type: ignore[operator]
            break
        week_start = week_start + timedelta(days=1)
    else:
        raise ValueError(
            f"No trading day found in week starting {last_monday} — calendar data issue?"
        )

    # Step 3: find last trading day of that week (Mon + 4 = Fri, walk back
    # if Fri is holiday).
    last_friday = last_monday + timedelta(days=4)
    week_end = last_friday
    for _ in range(7):
        if is_trading_day_fn(week_end):  # type: ignore[operator]
            break
        week_end = week_end - timedelta(days=1)
    else:
        raise ValueError(
            f"No trading day found in week ending {last_friday} — calendar data issue?"
        )

    if week_end < week_start:
        raise ValueError(f"Week end {week_end} before start {week_start} — calendar logic bug?")
    return week_start, week_end


def compute_weekly_fib_levels(
    trading_day: date,
    week_start: date,
    week_end: date,
    pwh_paisa: int,
    pwl_paisa: int,
) -> WeeklyFibLevels:
    """Compute all 7 weekly Fibonacci retracement levels.

    Args:
        trading_day: Today.
        week_start: Monday of previous trading week (inclusive).
        week_end: Friday/last-trading-day of previous trading week (inclusive).
        pwh_paisa: Previous Week's High in int64 paisa.
        pwl_paisa: Previous Week's Low in int64 paisa.

    Returns:
        WeeklyFibLevels with 7 retracements in int64 paisa.

    Raises:
        ValueError: if pwh < pwl.
    """
    if pwh_paisa < pwl_paisa:
        raise ValueError(
            f"PWH ({pwh_paisa}) must be >= PWL ({pwl_paisa}) — week {week_start} to {week_end}"
        )
    range_paisa = pwh_paisa - pwl_paisa

    retrace_pwh = pwh_paisa
    retrace_23 = _round_half_even(pwh_paisa - 0.236 * range_paisa)
    retrace_38 = _round_half_even(pwh_paisa - 0.382 * range_paisa)
    retrace_50 = _round_half_even((pwh_paisa + pwl_paisa) / 2.0)
    retrace_62 = _round_half_even(pwh_paisa - 0.618 * range_paisa)
    retrace_78 = _round_half_even(pwh_paisa - 0.786 * range_paisa)
    retrace_pwl = pwl_paisa

    return WeeklyFibLevels(
        trading_day=trading_day,
        week_start=week_start,
        week_end=week_end,
        pwh_paisa=pwh_paisa,
        pwl_paisa=pwl_paisa,
        range_paisa=range_paisa,
        retrace_at_pwh_paisa=retrace_pwh,
        retrace_23_paisa=retrace_23,
        retrace_38_paisa=retrace_38,
        retrace_50_paisa=retrace_50,
        retrace_62_paisa=retrace_62,
        retrace_78_paisa=retrace_78,
        retrace_at_pwl_paisa=retrace_pwl,
    )
