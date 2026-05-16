"""Track 2 per-trade cost calculator.

Pure-math implementation of `docs/track2/CALCULATOR_SPEC.md` (LOCKED
2026-05-16). Computes brokerage + STT + GST + stamp + exchange +
SEBI + IPFT + slippage per round-trip options trade for a single
instrument (NIFTY / BANKNIFTY / SENSEX) under a single broker.

All numeric values are int64 paise. No floats anywhere in the
critical path (eliminates accumulator drift across 33B trial sweeps).

Public entry point: ``compute_trade_cost(trade: TradeInput) -> TradeCostBreakdown``.

This package has NO external dependencies beyond `dataclasses`,
`datetime`, `enum`, and `typing` — keeps the math hot-path
inline-able by Rust/Numba downstream.
"""

from brutex.costs.calculator import compute_trade_cost
from brutex.costs.types import (
    Broker,
    GstSplit,
    TradeCostBreakdown,
    TradeInput,
    TradeOutcome,
    TradeSide,
)

__all__ = [
    "Broker",
    "GstSplit",
    "TradeCostBreakdown",
    "TradeInput",
    "TradeOutcome",
    "TradeSide",
    "compute_trade_cost",
]
