"""Track 2 per-trade cost calculator.

Pure-math implementation of `docs/track2/CALCULATOR_SPEC.md` (LOCKED
2026-05-16). Computes brokerage + STT + GST + stamp + exchange +
SEBI + IPFT + slippage per round-trip options trade for a single
instrument (NIFTY / BANKNIFTY / SENSEX) under a single broker.

All numeric values are int64 paise. No floats anywhere in the
critical path (eliminates accumulator drift across 33B trial sweeps).

## Build status (2026-05-16)

This package is under active development. Currently exposes:
- `constants` — verified rate constants from COSTS_VERIFIED.md

Coming in subsequent commits on this branch:
- `types` — TradeInput, TradeCostBreakdown, TradeOutcome enum
- `calculator` — `compute_trade_cost()` core function
- 16 fixture tests per CALCULATOR_SPEC.md §12

Public API (forward-compat re-exports added as modules land).
Reading this docstring while the only export is `constants` means
this module is mid-build — see branch `claude/cost-calculator-impl`.

## Dependencies

NO external dependencies in the hot math path beyond Python stdlib
(`dataclasses`, `datetime`, `enum`, `typing`). This keeps the
calculator inline-able by Rust/Numba downstream.
"""

# Currently only constants module is implemented. Other modules
# (calculator, types) will be added in subsequent commits on this
# branch and their public symbols re-exported here.
from brutex.costs import constants

__all__ = [
    "constants",
]
