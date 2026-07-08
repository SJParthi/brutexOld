"""Static Daily Fibonacci levels — anchored to Previous Day's High/Low.

Per `docs/TRACK1_VOCABULARY.md` Indicator Group 1 (Anchors):
    "Static Daily Fib (12 levels)"

## Direction-aware (verified against ES Trading Academy AmiBroker v11)

Operator screenshots (2026-05-16, multiple days) show the indicator
flips between BULLISH and BEARISH conventions depending on the day's
bias:

BEARISH setup (gap-down / downtrend, e.g. BANKNIFTY 30-Apr-2026):
- 0% = PDH (top of retracement)
- 100% = PDL (bottom of retracement)
- Extensions go DOWN below PDL: 127%, 162%, 200%, 262%, 424%

BULLISH setup (gap-up / uptrend, e.g. NIFTY 07-May-2026):
- 0% = PDL (bottom of retracement)
- 100% = PDH (top of retracement)
- Extensions go UP above PDH: 127%, 162%, 200%, 262%, 424%

In BOTH cases the same PDH/PDL range is the anchor — only the
"direction" of extensions flips.

## What we compute

For each trading day we compute **21 anchor price levels**:
- 7 bearish-convention retracement levels INSIDE the PDH-PDL range
  (PDH-anchored: ``pdh - ratio * range``)
- 4 bullish-convention retracement levels INSIDE the PDH-PDL range
  (PDL-anchored: ``pdl + ratio * range`` — DEC-FIB-BULLISH-RETRACE-001;
  the bullish 0%/50%/100% share the PDL / midpoint / PDH fields, and the
  bullish ladder is NOT a relabeling of the bearish one because the Fib
  ratios are asymmetric: 0.236 + 0.786 = 1.022 != 1.000. The 38/62 pair
  coincides numerically only because 0.382 + 0.618 = 1.000 exactly in
  IEEE — an observation, never relied on.)
- 5 bullish extension levels ABOVE PDH (price targets when trending up)
- 5 bearish extension levels BELOW PDL (price targets when trending down)

The strategy engine references whichever subset matches its mask.
Vocabulary conditions like:
- `close > fib_50` — retracement (direction-agnostic)
- `close > fib_ext_127_up` — above bullish extension
- `close < fib_ext_127_down` — below bearish extension

(The 52->60-condition evaluator reads only the BEARISH retracements +
extensions; the bullish retracement fields are not in any vocabulary bit.)

## Verification status (Golden Rule #10 — honest envelope)

Chart-verified bit-exact against operator screenshots (2026-05-16):

1. BANKNIFTY 13-May-2026 (BEARISH):
   PDH=54,000  PDL=53,594.40  range=405.60
   23% level = 53,904.28 ✓ (matches chart)

2. BANKNIFTY 30-Apr-2026 (BEARISH):
   PDH=56,502  PDL=55,650.00  range=852.00
   38% level = 56,176.54 ✓ (matches chart)
   200% down extension = 54,798.00 ✓ (matches chart)

3. NIFTY 07-May-2026 (BULLISH):
   PDH=24,313  PDL=24,056  range=257
   200% UP extension = 24,570.00 ✓ (matches chart)
   BULLISH 78% retracement = 24,258.00 ✓ (matches chart —
   ``pdl + 0.786 * range`` exactly; the pre-fix bearish-alias mapping
   returned 24,252.35, a 565-paisa error)

NOT screenshot-verified: the bullish 23/38/62 retracements are
formula-derived (``pdl + ratio * range``, kernel-frozen goldens in
``tests/unit/test_fib_levels.py``) — no operator chart shows those three
rungs, so they carry formula provenance only.

## Money representation

All levels stored as `int64 paisa` per CLAUDE.md §4 (no floats in
hot path). Multiplication by Fib ratios uses i64 arithmetic with
banker's rounding (round-half-to-even) per CLAUDE.md §4 reproducibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Final

import trading_core

# Standard Fibonacci ratios.
FIB_RATIOS_RETRACEMENT: Final[dict[str, float]] = {
    "0": 0.0,
    "23": 0.236,
    "38": 0.382,
    "50": 0.500,
    "62": 0.618,
    "78": 0.786,
    "100": 1.000,
}

FIB_RATIOS_EXTENSION: Final[dict[str, float]] = {
    "127": 0.272,
    "162": 0.618,
    "200": 1.000,
    "262": 1.618,
    "424": 3.236,
}

ALL_RETRACEMENT_KEYS: Final[tuple[str, ...]] = (
    "0",
    "23",
    "38",
    "50",
    "62",
    "78",
    "100",
)

ALL_EXTENSION_KEYS: Final[tuple[str, ...]] = (
    "127",
    "162",
    "200",
    "262",
    "424",
)


class FibDirection(Enum):
    """Which direction the Fib extensions point.

    BEARISH = extensions BELOW PDL (downtrend convention: 0%=PDH, 100%=PDL).
    BULLISH = extensions ABOVE PDH (uptrend convention: 0%=PDL, 100%=PDH).
    """

    BEARISH = "bearish"
    BULLISH = "bullish"


@dataclass(frozen=True)
class FibLevels:
    """The full set of static daily Fibonacci anchor levels for one
    (instrument, trading_day).

    Stores BOTH bullish and bearish convention extensions so the strategy
    engine can reference whichever direction matches its mask.

    All values in int64 paisa.
    """

    trading_day: date
    pdh_paisa: int  # Previous Day's High
    pdl_paisa: int  # Previous Day's Low
    range_paisa: int  # PDH - PDL

    # ───── BEARISH retracement levels (inside PDH-PDL range) ─────
    # PDH-anchored, top-down: round_half_even(pdh - ratio * range).
    # Only the 0/50/100 anchors are shared with the bullish convention
    # (PDL / exact midpoint / PDH); the 23/38/62/78 rungs are NOT — the
    # bullish ladder has its own retrace_bull_* fields below
    # (DEC-FIB-BULLISH-RETRACE-001).
    retrace_level_at_pdh_paisa: int  # = PDH (top)
    retrace_23_paisa: int  # 23.6% from PDH downward
    retrace_38_paisa: int  # 38.2% from PDH downward
    retrace_50_paisa: int  # midpoint
    retrace_62_paisa: int  # 61.8% from PDH downward
    retrace_78_paisa: int  # 78.6% from PDH downward
    retrace_level_at_pdl_paisa: int  # = PDL (bottom)

    # ───── BULLISH retracement levels (PDL-anchored, DEC-FIB-BULLISH-RETRACE-001) ─────
    # round_half_even(pdl + ratio * range) — a REAL bottom-up ladder, NOT the
    # reversed bearish one (0.236 + 0.786 = 1.022 != 1.000). The bullish
    # 0%/50%/100% share retrace_level_at_pdl / retrace_50 / retrace_level_at_pdh.
    retrace_bull_23_paisa: int  # 23.6% up from PDL
    retrace_bull_38_paisa: int  # 38.2% up from PDL
    retrace_bull_62_paisa: int  # 61.8% up from PDL
    retrace_bull_78_paisa: int  # 78.6% up from PDL

    # ───── Bullish extensions ABOVE PDH ─────
    # Price targets when the day is trending up (gap-up open).
    ext_up_127_paisa: int  # PDH + 0.272 × range
    ext_up_162_paisa: int  # PDH + 0.618 × range
    ext_up_200_paisa: int  # PDH + 1.000 × range
    ext_up_262_paisa: int  # PDH + 1.618 × range
    ext_up_424_paisa: int  # PDH + 3.236 × range

    # ───── Bearish extensions BELOW PDL ─────
    # Price targets when the day is trending down (gap-down open).
    ext_down_127_paisa: int  # PDL - 0.272 × range
    ext_down_162_paisa: int  # PDL - 0.618 × range
    ext_down_200_paisa: int  # PDL - 1.000 × range
    ext_down_262_paisa: int  # PDL - 1.618 × range
    ext_down_424_paisa: int  # PDL - 3.236 × range

    def get_by_bearish_label(self, key: str) -> int:
        """Lookup level by BEARISH convention label.

        Bearish: 0% = PDH (top), 100% = PDL (bottom), extensions go DOWN.
        Example: BANKNIFTY 30-Apr-2026 chart.
        """
        bearish_map = {
            "0": self.retrace_level_at_pdh_paisa,
            "23": self.retrace_23_paisa,
            "38": self.retrace_38_paisa,
            "50": self.retrace_50_paisa,
            "62": self.retrace_62_paisa,
            "78": self.retrace_78_paisa,
            "100": self.retrace_level_at_pdl_paisa,
            "127": self.ext_down_127_paisa,
            "162": self.ext_down_162_paisa,
            "200": self.ext_down_200_paisa,
            "262": self.ext_down_262_paisa,
            "424": self.ext_down_424_paisa,
        }
        return bearish_map[key]

    def get_by_bullish_label(self, key: str) -> int:
        """Lookup level by BULLISH convention label.

        Bullish: 0% = PDL (bottom), 100% = PDH (top), extensions go UP.
        Example: NIFTY 07-May-2026 chart (bullish 78% = 24,258.00,
        chart-verified).

        The retracement rungs are the dedicated PDL-anchored
        ``retrace_bull_*`` fields (``pdl + ratio * range``), NEVER the
        reversed bearish fields: the Fib ratios are asymmetric
        (0.236 + 0.786 = 1.022 != 1.000), so the old alias mapping was
        wrong by 0.022 x range on the 23/78 rungs
        (DEC-FIB-BULLISH-RETRACE-001). The 50% midpoint is the shared
        ``retrace_50_paisa`` (exact integer-sum midpoint in both
        conventions).
        """
        bullish_map = {
            "0": self.retrace_level_at_pdl_paisa,  # PDL is the bottom (0% in bullish)
            "23": self.retrace_bull_23_paisa,  # pdl + 0.236 × range
            "38": self.retrace_bull_38_paisa,  # pdl + 0.382 × range
            "50": self.retrace_50_paisa,  # midpoint (same in both conventions)
            "62": self.retrace_bull_62_paisa,  # pdl + 0.618 × range
            "78": self.retrace_bull_78_paisa,  # pdl + 0.786 × range
            "100": self.retrace_level_at_pdh_paisa,  # PDH is the top (100% in bullish)
            "127": self.ext_up_127_paisa,
            "162": self.ext_up_162_paisa,
            "200": self.ext_up_200_paisa,
            "262": self.ext_up_262_paisa,
            "424": self.ext_up_424_paisa,
        }
        return bullish_map[key]

    def get(self, key: str, direction: FibDirection) -> int:
        """Lookup level by key + direction convention."""
        if direction == FibDirection.BEARISH:
            return self.get_by_bearish_label(key)
        return self.get_by_bullish_label(key)


def compute_fib_levels(
    trading_day: date,
    pdh_paisa: int,
    pdl_paisa: int,
) -> FibLevels:
    """Compute all 21 static daily Fibonacci anchor levels.

    The arithmetic is delegated to the shared Rust ``trading_core`` crate
    (``DEC-SHARED-CORE-001``, ``docs/MIGRATION.md`` #441) — the **single source
    of truth** for the Fib math. This wrapper keeps the data-corruption
    validation and packs the result into the :class:`FibLevels` dataclass
    (``retrace@pdh = pdh``, ``retrace@pdl = pdl``, ``range = pdh - pdl`` are
    derived here; the 19 computed levels come from Rust — the original 15
    plus the four PDL-anchored bullish retracements appended last,
    DEC-FIB-BULLISH-RETRACE-001).

    Args:
        trading_day: The trading day these levels apply to (today).
        pdh_paisa: Previous Day's High in int64 paisa.
        pdl_paisa: Previous Day's Low in int64 paisa.

    Returns:
        FibLevels with 7 bearish-convention retracements + 4 bullish-convention
        retracements + 5 bullish extensions + 5 bearish extensions, all in
        int64 paisa.

    Chart-verified against three operator screenshots (2026-05-16):
    - BANKNIFTY 13-May-2026 (BEARISH): PDH=54000 PDL=53594.40 -> 23%=53904.28
    - BANKNIFTY 30-Apr-2026 (BEARISH): PDH=56502 PDL=55650.00 -> 38%=56176.54
                                                              -> 200%down=54798.00
    - NIFTY 07-May-2026 (BULLISH): PDH=24313 PDL=24056 -> 200%up=24570.00
                                                       -> bullish 78%=24258.00
    (Bullish 23/38/62 are formula-derived, kernel-frozen — NOT
    screenshot-verified; see the module docstring.)

    Raises:
        ValueError: if pdh < pdl (data corruption indicator).
    """
    if pdh_paisa < pdl_paisa:
        raise ValueError(
            f"PDH ({pdh_paisa}) must be >= PDL ({pdl_paisa}) — data corruption on {trading_day}"
        )

    range_paisa = pdh_paisa - pdl_paisa

    # Shared Rust core returns the 19 computed levels in order:
    # [retrace_23,38,50,62,78, ext_up_127,162,200,262,424,
    #  ext_down_127,162,200,262,424, retrace_bull_23,38,62,78]
    # — the original 15 order untouched, the bullish retracements appended
    # last (DEC-FIB-BULLISH-RETRACE-001).
    (
        retrace_23,
        retrace_38,
        retrace_50,
        retrace_62,
        retrace_78,
        ext_up_127,
        ext_up_162,
        ext_up_200,
        ext_up_262,
        ext_up_424,
        ext_down_127,
        ext_down_162,
        ext_down_200,
        ext_down_262,
        ext_down_424,
        retrace_bull_23,
        retrace_bull_38,
        retrace_bull_62,
        retrace_bull_78,
    ) = trading_core.compute_fib_levels(pdh_paisa, pdl_paisa)

    return FibLevels(
        trading_day=trading_day,
        pdh_paisa=pdh_paisa,
        pdl_paisa=pdl_paisa,
        range_paisa=range_paisa,
        retrace_level_at_pdh_paisa=pdh_paisa,
        retrace_23_paisa=retrace_23,
        retrace_38_paisa=retrace_38,
        retrace_50_paisa=retrace_50,
        retrace_62_paisa=retrace_62,
        retrace_78_paisa=retrace_78,
        retrace_level_at_pdl_paisa=pdl_paisa,
        retrace_bull_23_paisa=retrace_bull_23,
        retrace_bull_38_paisa=retrace_bull_38,
        retrace_bull_62_paisa=retrace_bull_62,
        retrace_bull_78_paisa=retrace_bull_78,
        ext_up_127_paisa=ext_up_127,
        ext_up_162_paisa=ext_up_162,
        ext_up_200_paisa=ext_up_200,
        ext_up_262_paisa=ext_up_262,
        ext_up_424_paisa=ext_up_424,
        ext_down_127_paisa=ext_down_127,
        ext_down_162_paisa=ext_down_162,
        ext_down_200_paisa=ext_down_200,
        ext_down_262_paisa=ext_down_262,
        ext_down_424_paisa=ext_down_424,
    )
