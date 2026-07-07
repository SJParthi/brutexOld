"""Track 2 per-trade cost calculator.

Pure-math implementation of `docs/track2/CALCULATOR_SPEC.md` (LOCKED
2026-05-16). Computes brokerage + STT + GST + stamp + exchange +
SEBI + IPFT + slippage per round-trip options trade for a single
instrument (NIFTY / BANKNIFTY / SENSEX) under a single broker.

All numeric values are int64 paise. No floats anywhere in the
critical path (eliminates accumulator drift across 33B trial sweeps).

## Public API

``compute_trade_cost(trade: TradeInput) -> TradeCostBreakdown``

The calculator is the build-target of `claude/cost-calculator-complete`
branch; landed in the merge to ``main``.

## Assurance-round amendments baked in

- **DEC-COST-001**: GST rounded as TOTAL once at 18% (Section 170 CGST).
- **DEC-COST-002**: STT regime selected by ``entry_dt_ist.date()``.
- **DEC-COST-003**: Finance Act 2026 STT rates LOCKED (primary source).
- **DEC-COST-004**: STT-on-exercise rate split LOCKED.
- **DEC-EXEC-008**: BUY-only policy (Track 2 uses long_call / long_put only).

## Dependencies

NO external dependencies in the hot math path beyond Python stdlib
+ ``brutex.options.lot_size_history`` for the per-date lot lookup.
"""

from brutex.costs import constants
from brutex.costs.calculator import compute_trade_cost
from brutex.costs.scope import is_cost_bearing, is_cost_free, is_index_future
from brutex.costs.types import (
    Broker,
    GstSplit,
    Instrument,
    OptionType,
    TradeCostBreakdown,
    TradeInput,
    TradeOutcome,
    TradeSide,
    UnsupportedTradeOutcomeError,
    UnverifiedCostRegimeError,
)

__all__ = [
    "Broker",
    "GstSplit",
    "Instrument",
    "OptionType",
    "TradeCostBreakdown",
    "TradeInput",
    "TradeOutcome",
    "TradeSide",
    "UnsupportedTradeOutcomeError",
    "UnverifiedCostRegimeError",
    "compute_trade_cost",
    "constants",
    "is_cost_bearing",
    "is_cost_free",
    "is_index_future",
]
