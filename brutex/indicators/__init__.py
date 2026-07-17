"""Supporting indicator computations for the 66-declared (62-active) vocabulary.

Per `docs/TRACK1_VOCABULARY.md` the atomic boolean conditions (66 declared on
the u128 layout; 62 ACTIVE — the VWAP bits 52/53 are RETIRED tombstones and
the SuperTrend bits 64/65 are declared layout-only) are evaluated in the
shared Rust ``trading_core`` crate; this package supplies the Python-side
indicator STATE those conditions consume:
- anchors (Static Daily Fib + Standard Pivots + PDH/PDL + Day Hi/Lo +
  the rolling-weekly / weekly-static Fib anchors)
- EMA 20 / EMA 200 position + crossover state
- market structure (BOS/CHoCH signal container)
- candle / day-structure inputs

Modules:
- `fib_levels`     — Static Daily Fib (21 levels), bullish + bearish stored
                     (4 PDL-anchored bullish retracements added by
                     DEC-FIB-BULLISH-RETRACE-001).
- `weekly_anchors` — Rolling-weekly + weekly-static Fib anchors (PR-2).
- `pivots`         — Standard Pivot Points (P, R1-R5, S1-S5 — R5/S5 added PR-2).
- `ema`            — EMA (alpha=2/(N+1), SMA-seed at bar N-1).
- `gap_mid`        — Session-edge gap-midpoint level (Group 15, bits 66-68 —
  DEC-GAP-MIDPOINT-CONDITIONS-001; the math lives in the shared Rust
  `trading_core` crate, this is the thin delegator).
- `market_structure` — the resolved BOS/CHoCH signal container (the stateful
  detector lives in the shared Rust `trading_core` crate, the single source
  since DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001; the sibling VWAP state
  was RETIRED with the bits-52/53 tombstone).
- `day_extremes`   — Running Day High / Day Low from session open.
- `prev_day_ohlc`  — Previous Day OHLC anchors (calendar-aware).

The per-bar condition VOCABULARY itself (66 declared / 62 active boolean
conditions) lives in :mod:`brutex.vocabulary` (bits + evaluator shim), never
here.
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
from brutex.indicators.gap_mid import compute_gap_mid
from brutex.indicators.market_structure import MarketStructureSignal
from brutex.indicators.pivots import PivotLevels, compute_pivot_levels
from brutex.indicators.prev_day_ohlc import (
    DailyCandle,
    PrevDayOHLC,
    find_prev_trading_day,
    lookup_prev_day_ohlc,
    lookup_prev_day_ohlc_series,
)
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
    "PivotLevels",
    "PrevDayOHLC",
    "alpha",
    "compute_day_extremes",
    "compute_ema",
    "compute_fib_levels",
    "compute_gap_mid",
    "compute_pivot_levels",
    "find_prev_trading_day",
    "latest_ema",
    "lookup_prev_day_ohlc",
    "lookup_prev_day_ohlc_series",
    "rolling_weekly_anchor",
    "weekly_static_anchor",
]
