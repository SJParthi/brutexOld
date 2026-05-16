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
- `fib_levels` — Static Daily Fib (12 levels) anchored to PDH/PDL,
                 stores BOTH bullish and bearish extensions per day.
- `pivots`     — Standard Pivot Points (P, R1-R4, S1-S4) — to be added
- `ema`        — EMA 20, EMA 200 — to be added
- `conditions` — Vocabulary: 52 boolean conditions per bar — to be added
"""

from brutex.indicators.fib_levels import (
    ALL_EXTENSION_KEYS,
    ALL_RETRACEMENT_KEYS,
    FIB_RATIOS_EXTENSION,
    FIB_RATIOS_RETRACEMENT,
    FibDirection,
    FibLevels,
    compute_fib_levels,
)

__all__ = [
    "ALL_EXTENSION_KEYS",
    "ALL_RETRACEMENT_KEYS",
    "FIB_RATIOS_EXTENSION",
    "FIB_RATIOS_RETRACEMENT",
    "FibDirection",
    "FibLevels",
    "compute_fib_levels",
]
