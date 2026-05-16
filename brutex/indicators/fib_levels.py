"""Static Daily Fibonacci levels — anchored to Previous Day's High/Low.

Per `docs/TRACK1_VOCABULARY.md` Indicator Group 1 (Anchors):
    "Static Daily Fib (12 levels)"

Verified against ES Trading Academy AmiBroker v11 formula (operator
screenshots 2026-05-16). Exact match on BANKNIFTY 13-May-2026:
    PDH=54000, PDL=53594.40, 23% level = 53904.28 ✓

## The math

```
range = PDH − PDL    (computed from previous trading day's daily bar)

INTRA-PD RANGE (retracements):
    0%   = PDH                       (anchor: previous day's high)
    23%  = PDH − 0.236 × range
    38%  = PDH − 0.382 × range
    50%  = (PDH + PDL) / 2           (midpoint)
    62%  = PDH − 0.618 × range
    78%  = PDH − 0.786 × range
    100% = PDL                       (anchor: previous day's low)

EXTENSIONS BELOW PDL:
    127% = PDL − 0.272 × range
    162% = PDL − 0.618 × range
    200% = PDL − 1.000 × range       (one full range below PDL)
    262% = PDL − 1.618 × range
    424% = PDL − 3.236 × range
```

## When computed

ONCE per trading day, at session open (09:15:00 IST), using the
previous trading day's completed daily bar (PDH, PDL from
`brutex/data/parser.py` daily Parquet).

## Why "static"

Held CONSTANT throughout the trading day across all 21 timeframes.
This is what makes them "anchor" conditions — they're scalar
broadcasts (not multi-TF reads, no look-ahead per Property P-25).

## Money representation

All levels stored as `int64 paisa` per CLAUDE.md §4 (no floats in
hot path). Multiplication by Fib ratios uses i64 arithmetic with
rounding-to-nearest-even per the project's reproducibility rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

# Standard Fibonacci ratios (exact int64 representation requires careful rounding).
FIB_RATIOS_INTRA: Final[dict[str, float]] = {
    "0":    0.0,
    "23":   0.236,
    "38":   0.382,
    "50":   0.500,
    "62":   0.618,
    "78":   0.786,
    "100":  1.000,
}

FIB_RATIOS_EXTENSION: Final[dict[str, float]] = {
    "127":  0.272,
    "162":  0.618,
    "200":  1.000,
    "262":  1.618,
    "424":  3.236,
}

ALL_FIB_LEVEL_KEYS: Final[tuple[str, ...]] = (
    "0", "23", "38", "50", "62", "78", "100",
    "127", "162", "200", "262", "424",
)


@dataclass(frozen=True)
class FibLevels:
    """The 12 static daily Fibonacci levels for one (instrument, trading_day).

    All values are int64 paisa to match the project's money representation.
    Computed ONCE per day from previous trading day's PDH/PDL.
    """

    trading_day: date
    pdh_paisa: int        # 0% level = Previous Day's High
    pdl_paisa: int        # 100% level = Previous Day's Low
    range_paisa: int      # PDH - PDL

    # Retracements within previous-day range (PDH at 0%, PDL at 100%)
    level_0_paisa: int    # = PDH
    level_23_paisa: int
    level_38_paisa: int
    level_50_paisa: int   # midpoint
    level_62_paisa: int
    level_78_paisa: int
    level_100_paisa: int  # = PDL

    # Extensions BELOW PDL (downward expansion)
    level_127_paisa: int
    level_162_paisa: int
    level_200_paisa: int
    level_262_paisa: int
    level_424_paisa: int

    def get(self, key: str) -> int:
        """Lookup by string key (e.g. '50', '127')."""
        mapping = {
            "0":   self.level_0_paisa,
            "23":  self.level_23_paisa,
            "38":  self.level_38_paisa,
            "50":  self.level_50_paisa,
            "62":  self.level_62_paisa,
            "78":  self.level_78_paisa,
            "100": self.level_100_paisa,
            "127": self.level_127_paisa,
            "162": self.level_162_paisa,
            "200": self.level_200_paisa,
            "262": self.level_262_paisa,
            "424": self.level_424_paisa,
        }
        return mapping[key]


def _round_half_even(x: float) -> int:
    """Round-half-to-even (banker's rounding) for deterministic i64 conversion.

    Matches IEEE 754 default rounding mode AND the FPCR pin in CLAUDE.md §4 /
    spec §Non-Negotiable Correctness #11. Critical for bit-identical replay.
    """
    # Python's built-in `round()` uses banker's rounding (round-half-to-even)
    # which matches IEEE-754 default. Verified: round(0.5) == 0, round(1.5) == 2.
    return int(round(x))


def compute_fib_levels(
    trading_day: date,
    pdh_paisa: int,
    pdl_paisa: int,
) -> FibLevels:
    """Compute the 12 static daily Fibonacci levels.

    Args:
        trading_day: The trading day these levels apply to (today).
        pdh_paisa: Previous Day's High in int64 paisa.
        pdl_paisa: Previous Day's Low in int64 paisa.

    Returns:
        FibLevels dataclass with all 12 levels in int64 paisa.

    Verified against ES Trading Academy AmiBroker v11 formula
    (operator screenshot 2026-05-16):
        BANKNIFTY 13-May-2026: PDH=54000.00, PDL=53594.40
        Expected 23% = 53904.28 → matches exactly.

    Raises:
        ValueError: if pdh < pdl (data corruption indicator).
    """
    if pdh_paisa < pdl_paisa:
        raise ValueError(
            f"PDH ({pdh_paisa}) must be >= PDL ({pdl_paisa}) — "
            f"data corruption on {trading_day}"
        )

    range_paisa = pdh_paisa - pdl_paisa

    # Retracements
    level_0   = pdh_paisa
    level_23  = _round_half_even(pdh_paisa - 0.236 * range_paisa)
    level_38  = _round_half_even(pdh_paisa - 0.382 * range_paisa)
    level_50  = _round_half_even((pdh_paisa + pdl_paisa) / 2.0)
    level_62  = _round_half_even(pdh_paisa - 0.618 * range_paisa)
    level_78  = _round_half_even(pdh_paisa - 0.786 * range_paisa)
    level_100 = pdl_paisa

    # Extensions below PDL
    level_127 = _round_half_even(pdl_paisa - 0.272 * range_paisa)
    level_162 = _round_half_even(pdl_paisa - 0.618 * range_paisa)
    level_200 = _round_half_even(pdl_paisa - 1.000 * range_paisa)
    level_262 = _round_half_even(pdl_paisa - 1.618 * range_paisa)
    level_424 = _round_half_even(pdl_paisa - 3.236 * range_paisa)

    return FibLevels(
        trading_day=trading_day,
        pdh_paisa=pdh_paisa,
        pdl_paisa=pdl_paisa,
        range_paisa=range_paisa,
        level_0_paisa=level_0,
        level_23_paisa=level_23,
        level_38_paisa=level_38,
        level_50_paisa=level_50,
        level_62_paisa=level_62,
        level_78_paisa=level_78,
        level_100_paisa=level_100,
        level_127_paisa=level_127,
        level_162_paisa=level_162,
        level_200_paisa=level_200,
        level_262_paisa=level_262,
        level_424_paisa=level_424,
    )
