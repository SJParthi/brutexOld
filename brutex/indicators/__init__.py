"""52 atomic boolean conditions + supporting indicator computations.

Per `docs/TRACK1_VOCABULARY.md` — 9 indicator groups:
- price-action
- indicator-crossover (EMA 20, EMA 200 crosses)
- VWAP (mirrored from futures volume for spot indices)
- Bollinger
- regime
- breakout
- divergence
- anchor (Static Daily Fib + Standard Pivots + PDH/PDL + Day Hi/Lo)
- candle

Modules:
- `fib_levels`     — Static Daily Fib (12 levels), bullish + bearish stored.
- `pivots`         — Standard Pivot Points (P, R1-R4, S1-S4).
- `ema`            — EMA (alpha=2/(N+1), SMA-seed at bar N-1).
- `day_extremes`   — Running Day High / Day Low from session open.
- `prev_day_ohlc`  — Previous Day OHLC anchors (calendar-aware).
- `conditions`     — Vocabulary: 52 boolean conditions per bar (TBD).
"""

from brutex.indicators.day_extremes import DayExtremesBar, compute_day_extremes
from brutex.indicators.ema import alpha, compute_ema, latest_ema
from brutex.indicators.fib_levels import (
    ALL_EXTENSION_KEYS,
    ALL_RETRACEMENT_KEYS,
    FIB_RATIOS_EXTENSION,
    FIB_RATIOS_RETRACEMENT,
    FibDirection,
    FibLevels,
    compute_fib_levels,
)
from brutex.indicators.pivots import PivotLevels, compute_pivot_levels
from brutex.indicators.prev_day_ohlc import (
    DailyCandle,
    PrevDayOHLC,
    find_prev_trading_day,
    lookup_prev_day_ohlc,
    lookup_prev_day_ohlc_series,
)

__all__ = [
    # Fib
    "ALL_EXTENSION_KEYS",
    "ALL_RETRACEMENT_KEYS",
    "FIB_RATIOS_EXTENSION",
    "FIB_RATIOS_RETRACEMENT",
    "FibDirection",
    "FibLevels",
    "compute_fib_levels",
    # Pivots
    "PivotLevels",
    "compute_pivot_levels",
    # EMA
    "alpha",
    "compute_ema",
    "latest_ema",
    # Day extremes
    "DayExtremesBar",
    "compute_day_extremes",
    # Prev Day OHLC
    "DailyCandle",
    "PrevDayOHLC",
    "find_prev_trading_day",
    "lookup_prev_day_ohlc",
    "lookup_prev_day_ohlc_series",
]
