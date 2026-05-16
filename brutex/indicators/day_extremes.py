"""Running Day High / Day Low — intraday running extremes from session open.

Per `docs/TRACK1_VOCABULARY.md` Indicator Group 1 (Anchors):
    "Day High, Day Low (running)"

## What it computes

For each intraday bar, the running maximum HIGH and running minimum
LOW from session open (09:15 IST) through that bar — inclusive of
the current bar.

Reference Pine Script (provided by operator 2026-05-16, Pine v3):
```
t = time("1440", session.extended)
is_first = na(t[1]) and not na(t) or t[1] < t

if is_first and barstate.isnew:
    day_high := high
    day_low  := low
else:
    day_high := day_high[1]
    day_low  := day_low[1]

if high > day_high: day_high := high
if low  < day_low : day_low  := low
```

Our implementation matches this logic exactly:
- At the first bar of each trading day, reset to bar.high / bar.low.
- For every subsequent bar in the same trading day, expand to include
  the bar's high/low.

## Reset trigger

Reset on **trading-day boundary in IST**. We use the IST calendar
date (via the bar's IST timestamp) — NOT a fixed 24-hour window —
so weekends and holidays are correctly handled.

## Money representation

int64 paisa throughout per CLAUDE.md §4. No arithmetic operations
on the values, just min/max comparison — so no rounding concerns.

## No look-ahead (Property P-06)

`day_high[N]` and `day_low[N]` are computed from bars 0..N only.
Streaming-safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable


@dataclass(frozen=True)
class DayExtremesBar:
    """One bar's running day high / day low values.

    Both in int64 paisa.
    """

    timestamp_ist: datetime  # bar's open timestamp in IST
    high_paisa: int          # this bar's high
    low_paisa: int           # this bar's low
    day_high_paisa: int      # running max from session open through this bar
    day_low_paisa: int       # running min from session open through this bar


def compute_day_extremes(
    bars: Iterable[tuple[datetime, int, int]],
) -> list[DayExtremesBar]:
    """Compute running day high / day low for a sequence of intraday bars.

    Args:
        bars: Iterable of (ist_timestamp, high_paisa, low_paisa) tuples.
              Must be sorted ascending by timestamp. Caller is responsible
              for sort order — function does NOT re-sort.

    Returns:
        List of DayExtremesBar in the same order as input.

    Behavior:
        - First bar of any trading day resets day_high/day_low to bar.high/bar.low.
        - Subsequent bars expand the running range.
        - Trading-day boundary is detected by IST calendar date change.

    Example:
        bars = [
            (datetime(2026, 5, 13, 9, 15, ist), 5400000, 5395000),  # day 1 bar 1
            (datetime(2026, 5, 13, 9, 16, ist), 5410000, 5398000),  # day 1 bar 2
            (datetime(2026, 5, 14, 9, 15, ist), 5390000, 5380000),  # day 2 bar 1 (RESET)
        ]
        result[0]: day_high=5400000, day_low=5395000
        result[1]: day_high=5410000, day_low=5395000  (high expanded)
        result[2]: day_high=5390000, day_low=5380000  (new day, reset)
    """
    out: list[DayExtremesBar] = []
    current_day: date | None = None
    running_high = 0
    running_low = 0

    for ts, high, low in bars:
        bar_day = ts.date()
        if bar_day != current_day:
            # New trading day → reset
            running_high = high
            running_low = low
            current_day = bar_day
        else:
            # Same trading day → expand
            if high > running_high:
                running_high = high
            if low < running_low:
                running_low = low

        out.append(
            DayExtremesBar(
                timestamp_ist=ts,
                high_paisa=high,
                low_paisa=low,
                day_high_paisa=running_high,
                day_low_paisa=running_low,
            )
        )

    return out
