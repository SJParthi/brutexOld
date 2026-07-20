"""Conflict pairs — pairs of bits that produce zero-trade strategies.

Per `docs/TRACK1_VOCABULARY.md` §11 — any pattern containing both
members of a conflict pair will fire zero trades, so the pattern
generator MUST prune them upfront. This saves ~50% of Stage 1A
compute (drops mask count from ~2.9M to ~1.5M survivors).

## Conflict types

1. **Direct opposition** — bit A and bit B describe mutually
   exclusive truths. E.g., `close > EMA20` AND `close < EMA20` —
   impossible.

2. **Mutual exclusion within group** — e.g., bullish bar AND
   bearish bar in the same bar.

The opposite of conflict is **orthogonal independence** — two bits
that can co-occur naturally (e.g., `close > EMA20` AND `close > PDH`).
These are NOT pruned.
"""

from __future__ import annotations

from typing import Final

from brutex.vocabulary.bits import (
    BIT_ABOVE_CPR_TC,
    BIT_AFTERNOON,
    BIT_BAR_BEARISH,
    BIT_BAR_BULLISH,
    BIT_BAR_DOJI,
    BIT_BAR_LARGE_BODY,
    BIT_BAR_SMALL_BODY,
    BIT_BELOW_CPR_BC,
    BIT_CLOSE_ABOVE_EMA20,
    BIT_CLOSE_ABOVE_EMA200,
    BIT_CLOSE_ABOVE_GAP_MID,
    BIT_CLOSE_ABOVE_PDH,
    BIT_CLOSE_ABOVE_PDL,
    BIT_CLOSE_ABOVE_SUPERTREND,
    BIT_CLOSE_AT_DAY_HIGH,
    BIT_CLOSE_AT_DAY_LOW,
    BIT_CLOSE_BELOW_EMA20,
    BIT_CLOSE_BELOW_EMA200,
    BIT_CLOSE_BELOW_GAP_MID,
    BIT_CLOSE_BELOW_PDH,
    BIT_CLOSE_BELOW_PDL,
    BIT_CLOSE_BELOW_SUPERTREND,
    BIT_CLOSE_IN_LOWER_THIRD,
    BIT_CLOSE_IN_UPPER_THIRD,
    BIT_EARLY_MORNING,
    BIT_EMA20_ABOVE_EMA200,
    BIT_EMA20_BELOW_EMA200,
    BIT_GAP_DOWN_DAY,
    BIT_GAP_UP_DAY,
    BIT_INSIDE_CPR,
    BIT_INSIDE_DAY,
    BIT_LONG_LOWER_WICK,
    BIT_LONG_UPPER_WICK,
    BIT_MID_MORNING,
    BIT_MIDDAY,
    BIT_OUTSIDE_DAY,
    BIT_PRIOR_ALTERNATING,
    BIT_PRIOR_N_BEARISH,
    BIT_PRIOR_N_BULLISH,
)

# Each tuple = (bit_a, bit_b) where a < b. Order matters for the
# `has_conflict` lookup (we always check the smaller bit first).
CONFLICT_PAIRS: Final[frozenset[tuple[int, int]]] = frozenset(
    {
        # Group 1: EMA position direct opposition
        (BIT_CLOSE_ABOVE_EMA20, BIT_CLOSE_BELOW_EMA20),
        (BIT_CLOSE_ABOVE_EMA200, BIT_CLOSE_BELOW_EMA200),
        (BIT_EMA20_ABOVE_EMA200, BIT_EMA20_BELOW_EMA200),
        # Group 3: PDH/PDL direct opposition
        (BIT_CLOSE_ABOVE_PDH, BIT_CLOSE_BELOW_PDH),
        (BIT_CLOSE_ABOVE_PDL, BIT_CLOSE_BELOW_PDL),
        # Group 5: Candle shape mutual exclusions
        # NOTE (DEC-STAGE1B-CONFLICT-FIX-001): (BULLISH, DOJI) and (BEARISH, DOJI)
        # were REMOVED — they are NOT conflicts. The evaluator defines DOJI as a
        # SMALL body (`body*10 < range`, evaluator.rs:239), not `open==close`, so a
        # bar with a tiny POSITIVE body is both BULLISH and DOJI (and a tiny
        # negative body is BEARISH+DOJI). Listing them as conflicts WRONGLY pruned
        # those valid masks from the sweep — a leaderboard-corruption bug (verified:
        # ~9% of random bars are bullish/bearish dojis).
        (BIT_BAR_BULLISH, BIT_BAR_BEARISH),
        (BIT_BAR_LARGE_BODY, BIT_BAR_SMALL_BODY),
        # New UNCONDITIONAL candle conflicts (DEC-STAGE1B-CONFLICT-FIX-001),
        # each provable from evaluator.rs:233-256 (body/range + wick/range bounds)
        # and verified to never co-occur across 20k random bars:
        #   DOJI (body/range < 0.1) ⊥ LARGE_BODY (body/range > 0.7)
        (BIT_BAR_DOJI, BIT_BAR_LARGE_BODY),
        #   upper_wick + body + lower_wick == range, so two halves can't both
        #   exceed range/2, and a >0.7 body leaves <0.3 for either wick:
        (BIT_LONG_UPPER_WICK, BIT_LONG_LOWER_WICK),
        (BIT_BAR_LARGE_BODY, BIT_LONG_UPPER_WICK),
        (BIT_BAR_LARGE_BODY, BIT_LONG_LOWER_WICK),
        # Group 6: Prior bar pattern mutual exclusions
        (BIT_PRIOR_N_BULLISH, BIT_PRIOR_N_BEARISH),
        (BIT_PRIOR_N_BULLISH, BIT_PRIOR_ALTERNATING),
        (BIT_PRIOR_N_BEARISH, BIT_PRIOR_ALTERNATING),
        # Group 7: Day structure mutual exclusions
        (BIT_CLOSE_AT_DAY_HIGH, BIT_CLOSE_AT_DAY_LOW),
        (BIT_CLOSE_IN_UPPER_THIRD, BIT_CLOSE_IN_LOWER_THIRD),
        (BIT_CLOSE_AT_DAY_HIGH, BIT_CLOSE_IN_LOWER_THIRD),
        (BIT_CLOSE_AT_DAY_LOW, BIT_CLOSE_IN_UPPER_THIRD),
        # Group 8: Time-of-day buckets are mutually exclusive within a single bar
        (BIT_EARLY_MORNING, BIT_MID_MORNING),
        (BIT_EARLY_MORNING, BIT_MIDDAY),
        (BIT_EARLY_MORNING, BIT_AFTERNOON),
        (BIT_MID_MORNING, BIT_MIDDAY),
        (BIT_MID_MORNING, BIT_AFTERNOON),
        (BIT_MIDDAY, BIT_AFTERNOON),
        # Group 9: Day type mutual exclusions
        (BIT_GAP_UP_DAY, BIT_GAP_DOWN_DAY),
        (BIT_INSIDE_DAY, BIT_OUTSIDE_DAY),
        # Group 15: the CPR position bits form an EXACT partition by
        # construction (evaluator.rs:422-431 — bit 60 strict `close > TC`,
        # bit 61 strict `close < BC`, bit 62 inclusive `BC <= close <= TC`
        # over the canonicalised `tc >= bc` pair of
        # pivots.rs::compute_cpr_levels), so the three are pairwise mutually
        # exclusive; `narrow_cpr_day` (63) is orthogonal (co-occurs with any).
        # The RETIRED VWAP tombstones (52/53) never fire, so they carry no
        # pair — the table prunes IMPOSSIBLE co-occurrence, and a mask
        # requiring a tombstone is already a zero-trade mask on its own.
        (BIT_ABOVE_CPR_TC, BIT_BELOW_CPR_BC),
        (BIT_ABOVE_CPR_TC, BIT_INSIDE_CPR),
        (BIT_BELOW_CPR_BC, BIT_INSIDE_CPR),
        # Group 14: the Supertrend position bits are DIRECT OPPOSITION by the
        # strict-inequality law (evaluator.rs Group 14 — close·2 > line fires
        # 64, close·2 < line fires 65, equality/absent fires NEITHER), so the
        # two can never co-occur — registered with the bits 64/65
        # implemented-flip (the PR-B vocab-66 SuperTrend DEC; the rung-1
        # layout-only declaration deliberately omitted the pair because an
        # unimplemented bit never fires).
        (BIT_CLOSE_ABOVE_SUPERTREND, BIT_CLOSE_BELOW_SUPERTREND),
        # Group 15: Gap-midpoint direct opposition (DEC-GAP-MIDPOINT-CONDITIONS-001,
        # the (GAP_UP_DAY, GAP_DOWN_DAY) precedent above). Strictly-above and
        # strictly-below the level cannot co-fire; `near_gap_mid` is deliberately
        # NOT conflicting with either — a close can be strictly above AND within
        # the near band (the same convention as the PDH/PDL family, where
        # close_above_pdh and near_pdh co-fire legally).
        (BIT_CLOSE_ABOVE_GAP_MID, BIT_CLOSE_BELOW_GAP_MID),
    }
)


def has_conflict(mask: int) -> bool:
    """Return True if `mask` contains any conflict pair.

    Args:
        mask: u128 strategy mask (``TOTAL_CONDITIONS`` = 69 declared bits).

    Returns:
        True if mask has any conflict pair (i.e., the strategy is
        guaranteed to fire zero trades and should be pruned).
    """
    return any((mask >> a) & 1 and (mask >> b) & 1 for a, b in CONFLICT_PAIRS)


def count_conflicts(mask: int) -> int:
    """Return the number of conflict pairs in `mask`.

    Useful for debugging strategy-generation pruning.
    """
    count = 0
    for a, b in CONFLICT_PAIRS:
        if (mask >> a) & 1 and (mask >> b) & 1:
            count += 1
    return count
