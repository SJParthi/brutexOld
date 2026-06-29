"""Standard (Floor Trader) Pivot Points — P, R1-R5, S1-S5.

Per `docs/TRACK1_VOCABULARY.md` Indicator Group 1 (Anchors):
    "Standard Pivot Points (P, R1-R4, S1-S4)" — extended to R5/S5 in PR-2
    (DEC-NIFTY-INDICATORS-001 / ADR-0012 D3) on the same linear ladder.

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
R5 = PDH + 4*(P − PDL)             (fifth resistance — extension, PR-2)
S5 = PDL − 4*(PDH − P)             (fifth support — extension, PR-2)
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

import trading_core


@dataclass(frozen=True)
class PivotLevels:
    """11 standard pivot levels for one (instrument, trading_day).

    All values in int64 paisa. R5/S5 extend the same linear floor-trader ladder
    past R4/S4 (PR-2 — DEC-NIFTY-INDICATORS-001 / ADR-0012 D3); they are
    CODE-ANCHORED (no operator screenshot pins them), the honest continuation of
    the screenshot-verified R1-R4 / S1-S4 rungs.
    """

    trading_day: date

    # Previous day's anchors (used to compute pivot)
    pdh_paisa: int  # Previous Day's High
    pdl_paisa: int  # Previous Day's Low
    pdc_paisa: int  # Previous Day's Close

    # The 11 pivot levels (5 resistance + pivot + 5 support, top to bottom):
    r5_paisa: int  # 5th resistance (extension — PR-2)
    r4_paisa: int  # 4th resistance (extension)
    r3_paisa: int  # 3rd resistance
    r2_paisa: int  # 2nd resistance
    r1_paisa: int  # 1st resistance
    p_paisa: int  # Pivot (central)
    s1_paisa: int  # 1st support
    s2_paisa: int  # 2nd support
    s3_paisa: int  # 3rd support
    s4_paisa: int  # 4th support (extension)
    s5_paisa: int  # 5th support (extension — PR-2)

    def get(self, key: str) -> int:
        """Lookup by string key.

        Keys: 'P', 'R1'..'R5', 'S1'..'S5'.
        """
        mapping = {
            "P": self.p_paisa,
            "R1": self.r1_paisa,
            "R2": self.r2_paisa,
            "R3": self.r3_paisa,
            "R4": self.r4_paisa,
            "R5": self.r5_paisa,
            "S1": self.s1_paisa,
            "S2": self.s2_paisa,
            "S3": self.s3_paisa,
            "S4": self.s4_paisa,
            "S5": self.s5_paisa,
        }
        return mapping[key]


def compute_pivot_levels(
    trading_day: date,
    pdh_paisa: int,
    pdl_paisa: int,
    pdc_paisa: int,
) -> PivotLevels:
    """Compute Floor Trader pivot points for a trading day.

    The arithmetic is delegated to the shared Rust ``trading_core`` crate
    (``DEC-SHARED-CORE-001``, ``docs/MIGRATION.md`` #441) — the **single source
    of truth** for the pivot math, so BruteX and TickVault compute identical
    levels by construction. This wrapper keeps the data-corruption validation
    (which the Rust core assumes the caller performs) and packs the result into
    the :class:`PivotLevels` dataclass.

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

    # Shared Rust core returns [r5, r4, r3, r2, r1, p, s1, s2, s3, s4, s5]
    # (int64 paisa — R5/S5 added in PR-2, DEC-NIFTY-INDICATORS-001).
    r5, r4, r3, r2, r1, p, s1, s2, s3, s4, s5 = trading_core.compute_pivot_levels(
        pdh_paisa, pdl_paisa, pdc_paisa
    )

    return PivotLevels(
        trading_day=trading_day,
        pdh_paisa=pdh_paisa,
        pdl_paisa=pdl_paisa,
        pdc_paisa=pdc_paisa,
        r5_paisa=r5,
        r4_paisa=r4,
        r3_paisa=r3,
        r2_paisa=r2,
        r1_paisa=r1,
        p_paisa=p,
        s1_paisa=s1,
        s2_paisa=s2,
        s3_paisa=s3,
        s4_paisa=s4,
        s5_paisa=s5,
    )
