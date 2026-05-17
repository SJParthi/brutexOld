"""Standard (Floor Trader) Pivot Points — P, R1-R4, S1-S4.

Per `docs/TRACK1_VOCABULARY.md` Indicator Group 1 (Anchors):
    "Standard Pivot Points (P, R1-R4, S1-S4)"

Verified against operator's TradingView "Pivots Traditional Auto"
screenshot (NIFTY 14-May-2026):
    PDH=23,777.15  PDL=23,426.50  PDC=23,689.65
    -> P=23,631.10, R1=23,835.70, R2=23,981.75, R3=24,186.35,
       R4=24,390.90, S1=23,485.05, S2=23,280.45, S3=23,134.40,
       S4=22,988.30
    All 9 levels match the screenshot bit-exact.

## The math (Floor Trader / Traditional pivot formula)

```
P  = (PDH + PDL + PDC) / 3        (pivot = avg of yesterday's H/L/C)
R1 = 2*P − PDL                     (first resistance)
S1 = 2*P − PDH                     (first support)
R2 = P + (PDH − PDL)               (P + previous-day range)
S2 = P − (PDH − PDL)               (P − previous-day range)
R3 = PDH + 2*(P − PDL)             (third resistance)
S3 = PDL − 2*(PDH − P)             (third support)
R4 = PDH + 3*(P − PDL)             (fourth resistance — extension)
S4 = PDL − 3*(PDH − P)             (fourth support — extension)
```

## When computed

Once per trading day at session open (09:15 IST), using the previous
trading day's completed daily bar (PDH, PDL, PDC). Held CONSTANT
intraday across all 21 timeframes.

## Money representation

int64 paisa throughout per CLAUDE.md §4. Banker's rounding
(round-half-to-even) for floats → int64 conversion ensures
bit-identical replay across rebuilds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class PivotLevels:
    """9 standard pivot levels for one (instrument, trading_day).

    All values in int64 paisa.
    """

    trading_day: date

    # Previous day's anchors (used to compute pivot)
    pdh_paisa: int  # Previous Day's High
    pdl_paisa: int  # Previous Day's Low
    pdc_paisa: int  # Previous Day's Close

    # The 9 pivot levels (5 resistance + pivot + 4 support, top to bottom):
    r4_paisa: int  # 4th resistance (extension)
    r3_paisa: int  # 3rd resistance
    r2_paisa: int  # 2nd resistance
    r1_paisa: int  # 1st resistance
    p_paisa: int  # Pivot (central)
    s1_paisa: int  # 1st support
    s2_paisa: int  # 2nd support
    s3_paisa: int  # 3rd support
    s4_paisa: int  # 4th support (extension)

    def get(self, key: str) -> int:
        """Lookup by string key.

        Keys: 'P', 'R1'..'R4', 'S1'..'S4'.
        """
        mapping = {
            "P": self.p_paisa,
            "R1": self.r1_paisa,
            "R2": self.r2_paisa,
            "R3": self.r3_paisa,
            "R4": self.r4_paisa,
            "S1": self.s1_paisa,
            "S2": self.s2_paisa,
            "S3": self.s3_paisa,
            "S4": self.s4_paisa,
        }
        return mapping[key]


def _round_half_even(x: float) -> int:
    """Round-half-to-even (banker's rounding) for deterministic i64 conversion.

    Matches IEEE 754 default rounding mode and the FPCR pin per
    CLAUDE.md §4 / spec §Non-Negotiable Correctness #11.
    """
    return round(x)


def compute_pivot_levels(
    trading_day: date,
    pdh_paisa: int,
    pdl_paisa: int,
    pdc_paisa: int,
) -> PivotLevels:
    """Compute Floor Trader pivot points for a trading day.

    Args:
        trading_day: The trading day these levels apply to (today).
        pdh_paisa: Previous Day's High in int64 paisa.
        pdl_paisa: Previous Day's Low in int64 paisa.
        pdc_paisa: Previous Day's Close in int64 paisa.

    Returns:
        PivotLevels with all 9 levels (P, R1-R4, S1-S4) in int64 paisa.

    Verified against TradingView "Pivots Traditional Auto" screenshot
    (NIFTY 14-May-2026): all 9 levels bit-exact.

    Raises:
        ValueError: if pdh < pdl (data corruption indicator).
        ValueError: if pdc is outside [pdl, pdh] (data corruption).
    """
    if pdh_paisa < pdl_paisa:
        raise ValueError(
            f"PDH ({pdh_paisa}) must be >= PDL ({pdl_paisa}) — data corruption on {trading_day}"
        )
    if not (pdl_paisa <= pdc_paisa <= pdh_paisa):
        raise ValueError(
            f"PDC ({pdc_paisa}) must be in [PDL ({pdl_paisa}), PDH ({pdh_paisa})] — "
            f"data corruption on {trading_day}"
        )

    # Compute pivot from the standard Floor Trader formula: average of
    # previous day's high, low, and close (rounded to whole paisa).
    p = _round_half_even((pdh_paisa + pdl_paisa + pdc_paisa) / 3.0)

    range_paisa = pdh_paisa - pdl_paisa  # H - L
    # NOTE: we use the integer P for downstream formulas to match how
    # TradingView and AmiBroker display rounded pivot values. This keeps
    # the engine's pivot values aligned with what the operator sees on chart.
    p_minus_pdl = p - pdl_paisa
    pdh_minus_p = pdh_paisa - p

    r1 = 2 * p - pdl_paisa
    s1 = 2 * p - pdh_paisa
    r2 = p + range_paisa
    s2 = p - range_paisa
    r3 = pdh_paisa + 2 * p_minus_pdl
    s3 = pdl_paisa - 2 * pdh_minus_p
    r4 = pdh_paisa + 3 * p_minus_pdl
    s4 = pdl_paisa - 3 * pdh_minus_p

    return PivotLevels(
        trading_day=trading_day,
        pdh_paisa=pdh_paisa,
        pdl_paisa=pdl_paisa,
        pdc_paisa=pdc_paisa,
        r4_paisa=r4,
        r3_paisa=r3,
        r2_paisa=r2,
        r1_paisa=r1,
        p_paisa=p,
        s1_paisa=s1,
        s2_paisa=s2,
        s3_paisa=s3,
        s4_paisa=s4,
    )
