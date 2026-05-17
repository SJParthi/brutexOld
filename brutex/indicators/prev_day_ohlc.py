"""Previous Day OHLC anchors — fetched from yesterday's daily candle.

Per `docs/TRACK1_VOCABULARY.md` Indicator Group 1 (Anchors):
    "PDH (Prev Day High), PDL (Prev Day Low), PDO (Prev Day Open),
     PDC (Prev Day Close)"

## What it computes

For a given (instrument, trading_day), look up the immediately
prior trading day's daily candle and return its OHLC values.

Reference Pine Script (operator-provided 2026-05-16, Pine v6):
```
yHigh  = request.security(syminfo.tickerid, "D", high[1],  lookahead=barmerge.lookahead_on)
yLow   = request.security(syminfo.tickerid, "D", low[1],   lookahead=barmerge.lookahead_on)
yOpen  = request.security(syminfo.tickerid, "D", open[1],  lookahead=barmerge.lookahead_on)
yClose = request.security(syminfo.tickerid, "D", close[1], lookahead=barmerge.lookahead_on)
```

The `[1]` index on the daily timeframe means "the bar BEFORE today" —
i.e., yesterday's completed daily bar.

## Trading-day calendar

Uses the NSE/BSE trading-day calendar from `brutex.calendars.nse` to
find the prior trading day. Weekends and holidays are skipped. Example:

    Today is Monday  → prior trading day = Friday  (skip Sat/Sun)
    Today is Tuesday after a Monday holiday → prior = previous Friday

## No look-ahead

PDH/PDL/PDO/PDC are computed from the COMPLETED daily candle of the
prior trading day. Today's developing bars are never read. This
satisfies Property P-06 (instrumented bar accessor).

## Money representation

int64 paisa throughout per CLAUDE.md §4.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from brutex.calendars.nse import is_trading_day


@dataclass(frozen=True)
class PrevDayOHLC:
    """Previous trading day's OHLC anchors for one (instrument, today)."""

    today: date  # today (the trading day these anchors apply to)
    prev_day: date  # the trading day from which OHLC was fetched
    pdo_paisa: int  # Previous Day Open
    pdh_paisa: int  # Previous Day High
    pdl_paisa: int  # Previous Day Low
    pdc_paisa: int  # Previous Day Close


@dataclass(frozen=True)
class DailyCandle:
    """One day's OHLC, in int64 paisa.

    Used as input to the PrevDayOHLC lookup. Caller is responsible for
    parsing daily Parquet into these.
    """

    day: date
    open_paisa: int
    high_paisa: int
    low_paisa: int
    close_paisa: int


def find_prev_trading_day(today: date, lookback_max: int = 14) -> date | None:
    """Find the most recent trading day STRICTLY BEFORE `today`.

    Walks backwards day-by-day, skipping weekends and holidays.
    Returns None if no trading day found within `lookback_max` calendar
    days (defensive against extended holiday clusters; max NSE holiday
    cluster is < 14 calendar days).

    Examples:
        find_prev_trading_day(Mon) -> previous Fri
        find_prev_trading_day(Tue after long weekend) -> previous Fri
    """
    from datetime import timedelta

    d = today - timedelta(days=1)
    for _ in range(lookback_max):
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    return None


def lookup_prev_day_ohlc(
    today: date,
    daily_candles_by_date: dict[date, DailyCandle],
) -> PrevDayOHLC | None:
    """Look up previous trading day's OHLC for a given today.

    Args:
        today: The trading day for which we want the prior day's anchors.
        daily_candles_by_date: Dict mapping `date -> DailyCandle`. Caller
            provides this — typically loaded from a daily-bars Parquet.

    Returns:
        PrevDayOHLC with the prior trading day's OHLC, or None if no
        prior trading day is found OR the prior trading day isn't in
        the provided dict.

    Raises:
        ValueError: if `today` itself is not a trading day (likely
            programmer error — should not be asking for PDH on a
            holiday).
    """
    if not is_trading_day(today):
        raise ValueError(f"today ({today}) is not a trading day — PDH/PDL lookup invalid")

    prev_day = find_prev_trading_day(today)
    if prev_day is None:
        return None

    candle = daily_candles_by_date.get(prev_day)
    if candle is None:
        # Daily candle missing from the provided data — caller's
        # responsibility to handle (might be the start of the lookback
        # window, or data fetch gap).
        return None

    return PrevDayOHLC(
        today=today,
        prev_day=prev_day,
        pdo_paisa=candle.open_paisa,
        pdh_paisa=candle.high_paisa,
        pdl_paisa=candle.low_paisa,
        pdc_paisa=candle.close_paisa,
    )


def lookup_prev_day_ohlc_series(
    trading_days: list[date],
    daily_candles_by_date: dict[date, DailyCandle],
) -> dict[date, PrevDayOHLC]:
    """Bulk variant — compute PrevDayOHLC for a series of trading days.

    Returns dict mapping today -> PrevDayOHLC. Skips days where lookup
    returns None (e.g., the very first day of the available history
    has no prior day in the dict).
    """
    out: dict[date, PrevDayOHLC] = {}
    for today in trading_days:
        result = lookup_prev_day_ohlc(today, daily_candles_by_date)
        if result is not None:
            out[today] = result
    return out
