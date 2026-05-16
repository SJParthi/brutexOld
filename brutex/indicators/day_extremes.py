"""Running Day High / Day Low — intraday running extremes from session open.

Per `docs/TRACK1_VOCABULARY.md` Indicator Group 1 (Anchors):
    "Day High, Day Low (running)"

## ONE indicator, TWO interfaces (online/offline parity)

Same pattern as EMA: a streaming state object + a batch helper that
delegates to it. Live TickVault and backtest backups produce
**bit-identical** day_high / day_low values at every bar.

### Streaming / live mode

    state = DayExtremesState()
    for tick_bar in live_feed:
        result = state.update(ts_ist=tick_bar.ts_ist,
                              high=tick_bar.high,
                              low=tick_bar.low)
        # result has running day_high and day_low

### Batch / backtest mode

    bars = compute_day_extremes(historical_bars)
    # internally instantiates a DayExtremesState and feeds in order

## The math (matches operator's Pine v3 source)

```pine
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

Our implementation:
- At first bar of any new IST trading day: reset to (high, low) of that bar.
- For every subsequent bar in the same trading day: expand to include
  the new bar's (high, low).

## O(1) per-step

Each `update()` call:
- 1 date comparison (`bar_day != current_day`)
- Either: 2 assignments (new day) OR 2 comparisons + 0-2 assignments (same day)
- Total: ≤ 5 ops per bar
- State: 24 bytes (1 date + 2 int64)

## No look-ahead (Property P-06)

`day_high(t)` and `day_low(t)` depend only on bars [0..t]. Streaming-safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable


@dataclass(frozen=True)
class DayExtremesBar:
    """One bar's running day high / day low values (output record).

    Both in int64 paisa.
    """

    timestamp_ist: datetime  # bar's open timestamp in IST
    high_paisa: int          # this bar's high
    low_paisa: int           # this bar's low
    day_high_paisa: int      # running max from session open through this bar
    day_low_paisa: int       # running min from session open through this bar


@dataclass
class DayExtremesState:
    """Streaming-mode running Day High / Day Low state object.

    Hold once, call `.update()` per new bar. Works identically in:
    - Live TickVault loop (each bar close → update)
    - Backtest replay (historical bar → update)
    - Resumable sweeps (serialize/restore between checkpoints)

    Attributes:
        current_day: IST date of the currently-tracked trading day.
        day_high_paisa: Running max for current_day.
        day_low_paisa: Running min for current_day.
    """

    current_day: date | None = None
    day_high_paisa: int = 0
    day_low_paisa: int = 0

    def update(
        self,
        ts_ist: datetime,
        high_paisa: int,
        low_paisa: int,
    ) -> tuple[int, int]:
        """Process one bar. Returns (day_high, day_low) in int64 paisa.

        Args:
            ts_ist: Bar's IST timestamp (open or close — caller's choice,
                must be consistent; only the .date() part is used to
                detect day boundary).
            high_paisa: Bar's high in int64 paisa.
            low_paisa: Bar's low in int64 paisa.

        Returns:
            Tuple of (running_day_high_paisa, running_day_low_paisa)
            AFTER this bar is incorporated.
        """
        bar_day = ts_ist.date()
        if bar_day != self.current_day:
            # New trading day → reset to this bar's H/L
            self.current_day = bar_day
            self.day_high_paisa = high_paisa
            self.day_low_paisa = low_paisa
        else:
            # Same trading day → expand
            if high_paisa > self.day_high_paisa:
                self.day_high_paisa = high_paisa
            if low_paisa < self.day_low_paisa:
                self.day_low_paisa = low_paisa
        return self.day_high_paisa, self.day_low_paisa

    def reset(self) -> None:
        """Forget all history. Used at start-of-session-replay or in tests."""
        self.current_day = None
        self.day_high_paisa = 0
        self.day_low_paisa = 0

    def to_dict(self) -> dict:
        """Serialize for checkpoint persistence."""
        return {
            "current_day": self.current_day.isoformat() if self.current_day else None,
            "day_high_paisa": self.day_high_paisa,
            "day_low_paisa": self.day_low_paisa,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DayExtremesState":
        """Restore from checkpoint dict."""
        cd = date.fromisoformat(d["current_day"]) if d["current_day"] else None
        return cls(
            current_day=cd,
            day_high_paisa=d["day_high_paisa"],
            day_low_paisa=d["day_low_paisa"],
        )


def compute_day_extremes(
    bars: Iterable[tuple[datetime, int, int]],
) -> list[DayExtremesBar]:
    """Batch interface: compute running day extremes for a bar sequence.

    Wraps `DayExtremesState` so backtest replay produces bit-identical
    output to a live `DayExtremesState.update()` loop.

    Args:
        bars: Iterable of (ist_timestamp, high_paisa, low_paisa) tuples.
              Must be sorted ascending by timestamp.

    Returns:
        List of DayExtremesBar in same order as input.
    """
    out: list[DayExtremesBar] = []
    state = DayExtremesState()
    for ts, high, low in bars:
        day_high, day_low = state.update(ts, high, low)
        out.append(
            DayExtremesBar(
                timestamp_ist=ts,
                high_paisa=high,
                low_paisa=low,
                day_high_paisa=day_high,
                day_low_paisa=day_low,
            )
        )
    return out
