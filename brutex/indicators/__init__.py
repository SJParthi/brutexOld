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
- `fib_levels`     — Static Daily Fib (21 levels), bullish + bearish stored
                     (4 PDL-anchored bullish retracements added by
                     DEC-FIB-BULLISH-RETRACE-001).
- `weekly_anchors` — Rolling-weekly + weekly-static Fib anchors (PR-2).
- `pivots`         — Standard Pivot Points (P, R1-R5, S1-S5 — R5/S5 added PR-2).
- `ema`            — EMA (alpha=2/(N+1), SMA-seed at bar N-1).
- `vwap`           — Session-anchored intraday futures VWAP state (PR-2).
- `market_structure` — BOS/CHoCH fractal swing state (DEC-NIFTY-BOS-CHOCH-001).
- `day_extremes`   — Running Day High / Day Low from session open.
- `prev_day_ohlc`  — Previous Day OHLC anchors (calendar-aware).
- `conditions`     — Vocabulary: 52 boolean conditions per bar (TBD).
"""

from brutex.indicators.day_extremes import (
    DayExtremesBar,
    DayExtremesState,
    compute_day_extremes,
)
from brutex.indicators.ema import EMAState, alpha, compute_ema, latest_ema
from brutex.indicators.fib_levels import (
    ALL_EXTENSION_KEYS,
    ALL_RETRACEMENT_KEYS,
    FIB_RATIOS_EXTENSION,
    FIB_RATIOS_RETRACEMENT,
    FibDirection,
    FibLevels,
    compute_fib_levels,
)
from brutex.indicators.market_structure import (
    MarketStructureSignal,
    MarketStructureState,
    Trend,
)
from brutex.indicators.pivots import PivotLevels, compute_pivot_levels
from brutex.indicators.prev_day_ohlc import (
    DailyCandle,
    PrevDayOHLC,
    find_prev_trading_day,
    lookup_prev_day_ohlc,
    lookup_prev_day_ohlc_series,
)
from brutex.indicators.vwap import VwapState
from brutex.indicators.weekly_anchors import (
    FibAnchor,
    rolling_weekly_anchor,
    weekly_static_anchor,
)

__all__ = [
    "ALL_EXTENSION_KEYS",
    "ALL_RETRACEMENT_KEYS",
    "FIB_RATIOS_EXTENSION",
    "FIB_RATIOS_RETRACEMENT",
    "DailyCandle",
    "DayExtremesBar",
    "DayExtremesState",
    "EMAState",
    "FibAnchor",
    "FibDirection",
    "FibLevels",
    "MarketStructureSignal",
    "MarketStructureState",
    "PivotLevels",
    "PrevDayOHLC",
    "Trend",
    "VwapState",
    "alpha",
    "compute_day_extremes",
    "compute_ema",
    "compute_fib_levels",
    "compute_pivot_levels",
    "find_prev_trading_day",
    "latest_ema",
    "lookup_prev_day_ohlc",
    "lookup_prev_day_ohlc_series",
    "rolling_weekly_anchor",
    "weekly_static_anchor",
]
