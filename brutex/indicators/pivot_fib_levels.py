"""Pivot-Point Fibonacci R/S Levels — Fib-scaled support/resistance.

Per DEC-VOCAB-001: the second multi-anchor Fib variant. Unlike static
daily Fib (anchored on PDH/PDL) and weekly Fib (PWH/PWL), pivot-Fib R/S
is computed from yesterday's H/L/C using **Fibonacci ratios** instead
of the standard 1.0 / 2.0 / 3.0 multipliers.

## Formulas

Given previous-day H, L, C:

    P = (H + L + C) / 3                                  (pivot point)
    R1 = P + 0.382 * (H - L)                             (38.2% Fib R)
    R2 = P + 0.618 * (H - L)                             (61.8% Fib R)
    R3 = P + 1.000 * (H - L)                             (100% Fib R)
    S1 = P - 0.382 * (H - L)
    S2 = P - 0.618 * (H - L)
    S3 = P - 1.000 * (H - L)

This is the **standard "Fibonacci Pivot Point" formula** used widely in
Indian charting tools (TradingView, AmiBroker, Investing.com).

It differs from the standard floor-pivot R1/R2/R3 (already in
`brutex/indicators/pivots.py`) which uses:

    R1 = 2*P - L         (standard floor pivot)
    R2 = P + (H - L)
    R3 = H + 2*(P - L)

The Fib-scaled version gives tighter clustering around P + R1 and
wider gaps to R2/R3 — favored by mean-reversion traders.

## Money representation

All levels stored as int64 paisa per CLAUDE.md §4. Banker's rounding
on Fib ratios for bit-exact reproducibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

from brutex.indicators.fib_levels import _round_half_even

# Fib ratios for pivot-point R/S scaling (different from retracement ratios).
PIVOT_FIB_R1_RATIO: Final[float] = 0.382
PIVOT_FIB_R2_RATIO: Final[float] = 0.618
PIVOT_FIB_R3_RATIO: Final[float] = 1.000


@dataclass(frozen=True)
class PivotFibLevels:
    """Pivot-Point Fibonacci levels for one (instrument, trading_day).

    Derived from previous trading day's H/L/C using Fib ratios on the
    H-L range.
    """

    trading_day: date
    pdh_paisa: int  # Previous Day's High
    pdl_paisa: int  # Previous Day's Low
    pdc_paisa: int  # Previous Day's Close
    range_paisa: int  # H - L
    p_paisa: int  # Pivot point

    # Fib-scaled resistances (above P)
    r1_paisa: int  # P + 0.382 * range
    r2_paisa: int  # P + 0.618 * range
    r3_paisa: int  # P + 1.000 * range

    # Fib-scaled supports (below P)
    s1_paisa: int  # P - 0.382 * range
    s2_paisa: int  # P - 0.618 * range
    s3_paisa: int  # P - 1.000 * range

    def get(self, key: str) -> int:
        """Lookup level by key.

        Keys: P, R1, R2, R3, S1, S2, S3.
        """
        return {
            "P": self.p_paisa,
            "R1": self.r1_paisa,
            "R2": self.r2_paisa,
            "R3": self.r3_paisa,
            "S1": self.s1_paisa,
            "S2": self.s2_paisa,
            "S3": self.s3_paisa,
        }[key]


def compute_pivot_fib_levels(
    trading_day: date,
    pdh_paisa: int,
    pdl_paisa: int,
    pdc_paisa: int,
) -> PivotFibLevels:
    """Compute all 7 Pivot-Point Fibonacci levels (P + 3 R + 3 S).

    Args:
        trading_day: Today's date.
        pdh_paisa: Previous Day's High in int64 paisa.
        pdl_paisa: Previous Day's Low.
        pdc_paisa: Previous Day's Close.

    Returns:
        PivotFibLevels with P + R1/R2/R3 + S1/S2/S3.

    Raises:
        ValueError: if pdh < pdl OR pdc outside [pdl, pdh] range
            (data corruption indicator).
    """
    if pdh_paisa < pdl_paisa:
        raise ValueError(f"PDH ({pdh_paisa}) must be >= PDL ({pdl_paisa})")
    if pdc_paisa > pdh_paisa or pdc_paisa < pdl_paisa:
        raise ValueError(f"PDC ({pdc_paisa}) must be within [PDL={pdl_paisa}, PDH={pdh_paisa}]")

    range_paisa = pdh_paisa - pdl_paisa
    # Pivot point: arithmetic mean of HLC. Round half-even for det.
    p_paisa = _round_half_even((pdh_paisa + pdl_paisa + pdc_paisa) / 3.0)

    r1 = _round_half_even(p_paisa + PIVOT_FIB_R1_RATIO * range_paisa)
    r2 = _round_half_even(p_paisa + PIVOT_FIB_R2_RATIO * range_paisa)
    r3 = _round_half_even(p_paisa + PIVOT_FIB_R3_RATIO * range_paisa)

    s1 = _round_half_even(p_paisa - PIVOT_FIB_R1_RATIO * range_paisa)
    s2 = _round_half_even(p_paisa - PIVOT_FIB_R2_RATIO * range_paisa)
    s3 = _round_half_even(p_paisa - PIVOT_FIB_R3_RATIO * range_paisa)

    return PivotFibLevels(
        trading_day=trading_day,
        pdh_paisa=pdh_paisa,
        pdl_paisa=pdl_paisa,
        pdc_paisa=pdc_paisa,
        range_paisa=range_paisa,
        p_paisa=p_paisa,
        r1_paisa=r1,
        r2_paisa=r2,
        r3_paisa=r3,
        s1_paisa=s1,
        s2_paisa=s2,
        s3_paisa=s3,
    )
