"""52-atomic-condition vocabulary.

Per `docs/TRACK1_VOCABULARY.md`:
- 52 boolean conditions packed into a single u64 bitmask
- 9 indicator groups (EMA, Pivot, PDH/PDL, Fib, Candle, Prior, DayStructure,
  TimeOfDay, DayType)
- Strategy mask = u64 with set bits = required-true conditions (AND)
- Direction (bull/bear) is SEPARATE from the 52 bits

This is the shared vocabulary used by:
- Backtest (BruteX, this repo) — Stage 1A precompute over 5y of bars
- Live TickVault (future) — per-tick-bar evaluation

Same code path, same bit semantics, bit-identical output.

## Modules

- `bits` — bit-position constants for each of the 52 conditions
- `conflicts` — conflict pairs (pruned at strategy generation)
- `evaluator` — `evaluate_atoms(snapshot, params) -> u64` per bar
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
This module satisfies all 5:
- COMMON: identical class used by backtest + live
- DYNAMIC: new condition = add new bit constant + branch in evaluator
- SCALABLE: O(1) per bar
- AUTOMATED: pure-function evaluator, no I/O
- COMPREHENSIVE: every bit has a unit test
"""

from brutex.vocabulary.bits import (
    ALL_CONDITION_NAMES,
    BIT_TO_NAME,
    NAME_TO_BIT,
    TOTAL_CONDITIONS,
)
from brutex.vocabulary.conflicts import CONFLICT_PAIRS, has_conflict
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
    "IndicatorSnapshot",
    "NAME_TO_BIT",
    "PriorBarHistory",
    "ProximityParams",
    "TOTAL_CONDITIONS",
    "evaluate_atoms",
    "has_conflict",
]
