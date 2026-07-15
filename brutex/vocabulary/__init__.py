"""60-atomic-condition vocabulary.

Per `docs/TRACK1_VOCABULARY.md` (grown 52 → 60 by DEC-VOCAB-U128-CODEGEN-001 /
ADR-0012 PR-1):
- 66 declared boolean conditions packed into a u128 bitmask (bits 0..65;
  62 ACTIVE — the VWAP bits 52/53 are RETIRED tombstones and the SuperTrend
  bits 64/65 are declared layout-only)
- 14 indicator groups (EMA, Pivot, PDH/PDL, Fib, Candle, Prior, DayStructure,
  TimeOfDay, DayType, VWAP Position (retired), Pivot Proximity Extended,
  Market Structure, Central Pivot Range, Supertrend — `bits.GROUP_RANGES` is
  the one source of truth)
- Strategy mask = u128 with set bits = required-true conditions (AND)
- Direction (bull/bear) is SEPARATE from the condition bits

This is the shared vocabulary used by:
- Backtest (BruteX, this repo) — Stage 1A precompute over 5y of bars
- Live TickVault (future) — per-tick-bar evaluation

Same code path, same bit semantics, bit-identical output.

## Modules

- `bits` — bit-position constants for each of the 66 declared conditions (62 active)
- `conflicts` — conflict pairs (pruned at strategy generation)
- `evaluator` — `evaluate_atoms(snapshot, params) -> u128` per bar
- `snapshot` — `IndicatorSnapshot` input dataclass

## O(1) per bar

Each `evaluate_atoms()` call performs:
- ~10 float compares (EMA, PDH/PDL, day_high/low)
- ~11 Fib proximity checks (each = abs+mul+compare)
- ~7 Pivot proximity checks
- ~7 candle shape checks
- ~4 time-of-day branch
- ~4 day-type checks
- Total: ~50 simple ops, all on cache-resident floats/ints
- = O(1) per bar regardless of total bar count

Operator decree 2026-05-16: COMMON · DYNAMIC · SCALABLE · AUTOMATED.
This module satisfies all 5 (DEC-SHARED-CORE-001 #441 — the condition
logic (52 conditions then, 66 declared / 62 active now) lives ONCE, in the Rust `trading-core` crate, which BruteX
reaches via the `trading_core` PyO3 wheel and TickVault links natively):
- COMMON: the *same compiled core* serves backtest + live (not just an
  identical class — literally one Rust implementation, bit-identical)
- DYNAMIC: new condition = add a bit constant + a branch in the shared
  Rust evaluator (`rust/trading-core/src/evaluator.rs`); both apps inherit it
- SCALABLE: O(1) per bar (one cache-resident Rust call)
- AUTOMATED: pure-function evaluator, no I/O
- COMPREHENSIVE: every bit has a unit test (routed through the shim)
"""

from brutex.vocabulary.bits import (
    ALL_CONDITION_NAMES,
    BIT_TO_NAME,
    NAME_TO_BIT,
    TOTAL_CONDITIONS,
)
from brutex.vocabulary.conflicts import CONFLICT_PAIRS, has_conflict
from brutex.vocabulary.describe import (
    GROUP_RANGES,
    RETIRED_SUFFIX,
    MaskGroup,
    condition_display_name,
    describe_mask,
    display_condition_names,
    matched_condition_names,
)
from brutex.vocabulary.evaluator import (
    IndicatorSnapshot,
    PriorBarHistory,
    ProximityParams,
    evaluate_atoms,
)

__all__ = [
    "ALL_CONDITION_NAMES",
    "BIT_TO_NAME",
    "CONFLICT_PAIRS",
    "GROUP_RANGES",
    "NAME_TO_BIT",
    "RETIRED_SUFFIX",
    "TOTAL_CONDITIONS",
    "IndicatorSnapshot",
    "MaskGroup",
    "PriorBarHistory",
    "ProximityParams",
    "condition_display_name",
    "describe_mask",
    "display_condition_names",
    "evaluate_atoms",
    "has_conflict",
    "matched_condition_names",
]
