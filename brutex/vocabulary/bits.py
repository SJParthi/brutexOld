"""Bit-position constants for the 52 atomic conditions.

Per `docs/TRACK1_VOCABULARY.md` — bits 0..51, organized in 9 groups.

The u64 strategy mask layout:
    bit 0 = Group 1 condition #1 (close_above_ema20)
    ...
    bit 51 = Group 9 condition #4 (outside_day)
    bits 52-63 = RESERVED for future expansion

Total: 52 conditions in a u64 (12 bits headroom).

Never renumber bits. Adding new conditions = use bits 52, 53, ... in
the reserved range. This guarantees old strategy masks remain valid
across releases (semver).
"""

from __future__ import annotations

from typing import Final

# ────── Group 1: EMA Position (bits 0-5, 6 conditions) ──────
BIT_CLOSE_ABOVE_EMA20: Final[int] = 0
BIT_CLOSE_BELOW_EMA20: Final[int] = 1
BIT_CLOSE_ABOVE_EMA200: Final[int] = 2
BIT_CLOSE_BELOW_EMA200: Final[int] = 3
BIT_EMA20_ABOVE_EMA200: Final[int] = 4
BIT_EMA20_BELOW_EMA200: Final[int] = 5

# ────── Group 2: Pivot Proximity (bits 6-12, 7 conditions) ──────
BIT_NEAR_PIVOT_P: Final[int] = 6
BIT_NEAR_PIVOT_R1: Final[int] = 7
BIT_NEAR_PIVOT_R2: Final[int] = 8
BIT_NEAR_PIVOT_R3: Final[int] = 9
BIT_NEAR_PIVOT_S1: Final[int] = 10
BIT_NEAR_PIVOT_S2: Final[int] = 11
BIT_NEAR_PIVOT_S3: Final[int] = 12

# ────── Group 3: PDH/PDL (bits 13-18, 6 conditions) ──────
BIT_CLOSE_ABOVE_PDH: Final[int] = 13
BIT_CLOSE_BELOW_PDH: Final[int] = 14
BIT_CLOSE_ABOVE_PDL: Final[int] = 15
BIT_CLOSE_BELOW_PDL: Final[int] = 16
BIT_NEAR_PDH: Final[int] = 17
BIT_NEAR_PDL: Final[int] = 18

# ────── Group 4: Fib Proximity (bits 19-29, 11 conditions) ──────
BIT_NEAR_FIB_0: Final[int] = 19
BIT_NEAR_FIB_236: Final[int] = 20
BIT_NEAR_FIB_382: Final[int] = 21
BIT_NEAR_FIB_50: Final[int] = 22
BIT_NEAR_FIB_618: Final[int] = 23
BIT_NEAR_FIB_786: Final[int] = 24
BIT_NEAR_FIB_100: Final[int] = 25
BIT_NEAR_FIB_1272: Final[int] = 26
BIT_NEAR_FIB_1618: Final[int] = 27
BIT_NEAR_FIB_200: Final[int] = 28
BIT_NEAR_FIB_2618: Final[int] = 29

# ────── Group 5: Candle Shape (bits 30-36, 7 conditions) ──────
BIT_BAR_BULLISH: Final[int] = 30
BIT_BAR_BEARISH: Final[int] = 31
BIT_BAR_DOJI: Final[int] = 32
BIT_BAR_LARGE_BODY: Final[int] = 33
BIT_BAR_SMALL_BODY: Final[int] = 34
BIT_LONG_UPPER_WICK: Final[int] = 35
BIT_LONG_LOWER_WICK: Final[int] = 36

# ────── Group 6: Prior Bar Pattern (bits 37-39, 3 conditions) ──────
BIT_PRIOR_N_BULLISH: Final[int] = 37
BIT_PRIOR_N_BEARISH: Final[int] = 38
BIT_PRIOR_ALTERNATING: Final[int] = 39

# ────── Group 7: Day Structure (bits 40-43, 4 conditions) ──────
BIT_CLOSE_AT_DAY_HIGH: Final[int] = 40
BIT_CLOSE_AT_DAY_LOW: Final[int] = 41
BIT_CLOSE_IN_UPPER_THIRD: Final[int] = 42
BIT_CLOSE_IN_LOWER_THIRD: Final[int] = 43

# ────── Group 8: Time of Day (bits 44-47, 4 conditions) ──────
BIT_EARLY_MORNING: Final[int] = 44  # 09:16 - 10:00 IST
BIT_MID_MORNING: Final[int] = 45  # 10:00 - 11:30 IST
BIT_MIDDAY: Final[int] = 46  # 11:30 - 13:00 IST
BIT_AFTERNOON: Final[int] = 47  # 13:00 - 15:00 IST

# ────── Group 9: Day Type (bits 48-51, 4 conditions) ──────
BIT_GAP_UP_DAY: Final[int] = 48
BIT_GAP_DOWN_DAY: Final[int] = 49
BIT_INSIDE_DAY: Final[int] = 50
BIT_OUTSIDE_DAY: Final[int] = 51

# ────── Group 10: Weekly Fib (bits 52-56, 5 conditions) ──────
# DEC-VOCAB-001 (2026-05-17): added multi-anchor Fib support per operator
# rare-pattern strategy requirement. Weekly Fib levels are computed by
# `brutex/indicators/weekly_fib_levels.py` (anchored on previous trading
# week's H-L).
BIT_NEAR_WEEKLY_FIB_236: Final[int] = 52
BIT_NEAR_WEEKLY_FIB_382: Final[int] = 53
BIT_NEAR_WEEKLY_FIB_50: Final[int] = 54
BIT_NEAR_WEEKLY_FIB_618: Final[int] = 55
BIT_NEAR_WEEKLY_FIB_786: Final[int] = 56

# ────── Group 11: Pivot-Point Fib R/S (bits 57-62, 6 conditions) ──────
# DEC-VOCAB-001: Fib-scaled pivot R/S derived from previous day's H/L/C.
# Computed by `brutex/indicators/pivot_fib_levels.py`. Distinct from
# the standard floor-pivot R1/R2/R3 in bits 7-12 (those use the
# 1.0/2.0/H+2*(P-L) formulas; Pivot-Fib uses 0.382/0.618/1.000 ratios).
BIT_NEAR_PIVOT_FIB_R1: Final[int] = 57
BIT_NEAR_PIVOT_FIB_R2: Final[int] = 58
BIT_NEAR_PIVOT_FIB_R3: Final[int] = 59
BIT_NEAR_PIVOT_FIB_S1: Final[int] = 60
BIT_NEAR_PIVOT_FIB_S2: Final[int] = 61
BIT_NEAR_PIVOT_FIB_S3: Final[int] = 62

# ────── Metadata ──────
# DEC-VOCAB-001: vocabulary expanded from 52 → 63 conditions to cover
# weekly Fib + pivot-Fib R/S. Bit 63 reserved for session-Fib anchor
# (PR #31). u64 mask supports up to 64; we keep 1 bit headroom.
TOTAL_CONDITIONS: Final[int] = 63

# Name lookup tables. Generated from the constants above so they're
# always in sync.
NAME_TO_BIT: Final[dict[str, int]] = {
    "close_above_ema20": BIT_CLOSE_ABOVE_EMA20,
    "close_below_ema20": BIT_CLOSE_BELOW_EMA20,
    "close_above_ema200": BIT_CLOSE_ABOVE_EMA200,
    "close_below_ema200": BIT_CLOSE_BELOW_EMA200,
    "ema20_above_ema200": BIT_EMA20_ABOVE_EMA200,
    "ema20_below_ema200": BIT_EMA20_BELOW_EMA200,
    "near_pivot_p": BIT_NEAR_PIVOT_P,
    "near_pivot_r1": BIT_NEAR_PIVOT_R1,
    "near_pivot_r2": BIT_NEAR_PIVOT_R2,
    "near_pivot_r3": BIT_NEAR_PIVOT_R3,
    "near_pivot_s1": BIT_NEAR_PIVOT_S1,
    "near_pivot_s2": BIT_NEAR_PIVOT_S2,
    "near_pivot_s3": BIT_NEAR_PIVOT_S3,
    "close_above_pdh": BIT_CLOSE_ABOVE_PDH,
    "close_below_pdh": BIT_CLOSE_BELOW_PDH,
    "close_above_pdl": BIT_CLOSE_ABOVE_PDL,
    "close_below_pdl": BIT_CLOSE_BELOW_PDL,
    "near_pdh": BIT_NEAR_PDH,
    "near_pdl": BIT_NEAR_PDL,
    "near_fib_0": BIT_NEAR_FIB_0,
    "near_fib_236": BIT_NEAR_FIB_236,
    "near_fib_382": BIT_NEAR_FIB_382,
    "near_fib_50": BIT_NEAR_FIB_50,
    "near_fib_618": BIT_NEAR_FIB_618,
    "near_fib_786": BIT_NEAR_FIB_786,
    "near_fib_100": BIT_NEAR_FIB_100,
    "near_fib_1272": BIT_NEAR_FIB_1272,
    "near_fib_1618": BIT_NEAR_FIB_1618,
    "near_fib_200": BIT_NEAR_FIB_200,
    "near_fib_2618": BIT_NEAR_FIB_2618,
    "bar_bullish": BIT_BAR_BULLISH,
    "bar_bearish": BIT_BAR_BEARISH,
    "bar_doji": BIT_BAR_DOJI,
    "bar_large_body": BIT_BAR_LARGE_BODY,
    "bar_small_body": BIT_BAR_SMALL_BODY,
    "long_upper_wick": BIT_LONG_UPPER_WICK,
    "long_lower_wick": BIT_LONG_LOWER_WICK,
    "prior_n_bullish": BIT_PRIOR_N_BULLISH,
    "prior_n_bearish": BIT_PRIOR_N_BEARISH,
    "prior_alternating": BIT_PRIOR_ALTERNATING,
    "close_at_day_high": BIT_CLOSE_AT_DAY_HIGH,
    "close_at_day_low": BIT_CLOSE_AT_DAY_LOW,
    "close_in_upper_third": BIT_CLOSE_IN_UPPER_THIRD,
    "close_in_lower_third": BIT_CLOSE_IN_LOWER_THIRD,
    "early_morning": BIT_EARLY_MORNING,
    "mid_morning": BIT_MID_MORNING,
    "midday": BIT_MIDDAY,
    "afternoon": BIT_AFTERNOON,
    "gap_up_day": BIT_GAP_UP_DAY,
    "gap_down_day": BIT_GAP_DOWN_DAY,
    "inside_day": BIT_INSIDE_DAY,
    "outside_day": BIT_OUTSIDE_DAY,
    # Group 10: Weekly Fib (DEC-VOCAB-001)
    "near_weekly_fib_236": BIT_NEAR_WEEKLY_FIB_236,
    "near_weekly_fib_382": BIT_NEAR_WEEKLY_FIB_382,
    "near_weekly_fib_50": BIT_NEAR_WEEKLY_FIB_50,
    "near_weekly_fib_618": BIT_NEAR_WEEKLY_FIB_618,
    "near_weekly_fib_786": BIT_NEAR_WEEKLY_FIB_786,
    # Group 11: Pivot-Fib R/S (DEC-VOCAB-001)
    "near_pivot_fib_r1": BIT_NEAR_PIVOT_FIB_R1,
    "near_pivot_fib_r2": BIT_NEAR_PIVOT_FIB_R2,
    "near_pivot_fib_r3": BIT_NEAR_PIVOT_FIB_R3,
    "near_pivot_fib_s1": BIT_NEAR_PIVOT_FIB_S1,
    "near_pivot_fib_s2": BIT_NEAR_PIVOT_FIB_S2,
    "near_pivot_fib_s3": BIT_NEAR_PIVOT_FIB_S3,
}

BIT_TO_NAME: Final[dict[int, str]] = {v: k for k, v in NAME_TO_BIT.items()}

# Re-exported via brutex.vocabulary package __init__ for downstream tooling
# (e.g., strategy mask serialization). CodeQL false-positive otherwise since
# nothing in this module consumes it directly.
ALL_CONDITION_NAMES: Final[tuple[str, ...]] = tuple(NAME_TO_BIT.keys())

# Compile-time invariants
if len(NAME_TO_BIT) != TOTAL_CONDITIONS:
    raise AssertionError(f"Expected {TOTAL_CONDITIONS} conditions, got {len(NAME_TO_BIT)}")
if set(NAME_TO_BIT.values()) != set(range(TOTAL_CONDITIONS)):
    raise AssertionError(
        f"Bit positions must be 0..{TOTAL_CONDITIONS - 1} with no gaps, "
        f"got {sorted(NAME_TO_BIT.values())}"
    )
