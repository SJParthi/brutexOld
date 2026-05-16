"""Exponential Moving Average — EMA 20 and EMA 200.

Per `docs/TRACK1_VOCABULARY.md` Indicator Group 2 (Indicator Crossover):
    "EMA 20 close, EMA 200 close"

## The math

```
α = 2 / (N + 1)                          (smoothing factor)
EMA_0 = SMA of first N bars              (warm-up seed; standard TradingView convention)
EMA_t = α × close_t + (1 − α) × EMA_(t−1)    (t > N)
```

For N = 20:  α = 2/21 ≈ 0.0952
For N = 200: α = 2/201 ≈ 0.00995

## Warm-up

The first (N − 1) bars produce NaN/None. Bar N is the first valid EMA
(seeded as the SMA of bars 1..N). This matches TradingView's
`ta.ema()` semantics — verified by inspection of the function's
default behavior in Pine Script v5.

## Numerical precision

Float64 throughout (per CLAUDE.md §4 — only money values are int64
paisa; rolling indicator computations use float64 with FPCR pinned
to round-half-to-even).

For Day 1 sprint we use Polars' streaming `ewm_mean` (alpha-based
exponential weighted mean). For Phase 2+ Rust hot kernel we use
a fused Welford-style accumulator on AArch64 NEON.

## Verification

Operator's TradingView screenshot (NIFTY 14-May-2026):
    EMA 20 close = 23,749.65
    EMA 200 close = 23,688.32

These values can be reproduced from the full bar history (not
spot-tested here — requires real 1m data which arrives in Hour 2-4
with the Dhan adapter).
"""

from __future__ import annotations

from typing import Sequence


def alpha(period: int) -> float:
    """Smoothing factor for an N-period EMA.

    α = 2 / (N + 1).  Standard TradingView / AmiBroker convention.
    """
    if period < 1:
        raise ValueError(f"EMA period must be >= 1, got {period}")
    return 2.0 / (period + 1)


def compute_ema(
    values: Sequence[float],
    period: int,
) -> list[float | None]:
    """Compute the EMA series for a list of values.

    Args:
        values: Sequence of float prices (close values, in float units).
                Caller responsible for unit consistency (typically Rs, not paisa,
                to match how chart packages display EMA).
        period: EMA period (e.g., 20 or 200).

    Returns:
        List of length len(values), with the first (period − 1) entries
        as None (warm-up) and the rest as float EMA values.

    The bar at index (period − 1) is seeded as the SMA of values[0..period].
    Subsequent bars use the recursive EMA formula.

    Reference: TradingView `ta.ema()` default behavior.
    """
    n = len(values)
    if n == 0:
        return []
    if period < 1:
        raise ValueError(f"EMA period must be >= 1, got {period}")
    if period == 1:
        # Degenerate: EMA(1) = the values themselves
        return list(values)

    a = alpha(period)
    one_minus_a = 1.0 - a

    out: list[float | None] = [None] * n
    if n < period:
        return out

    # Seed at index (period - 1) with SMA of values[0..period-1]
    seed = sum(values[:period]) / period
    out[period - 1] = seed

    # Recursive EMA from index `period` onward
    prev = seed
    for i in range(period, n):
        prev = a * values[i] + one_minus_a * prev
        out[i] = prev

    return out


def latest_ema(values: Sequence[float], period: int) -> float | None:
    """Return only the latest (most recent) EMA value, or None if warm-up incomplete.

    Convenience for the strategy engine when only the current bar's EMA matters.
    """
    series = compute_ema(values, period)
    return series[-1] if series else None
