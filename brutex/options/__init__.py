"""Track 2 — Options realism leg.

Modules:
- `lot_size_history` — per-date lot size lookup (SEBI changes 2021-2026)
- `strike_rules` — 10 pre-trade strike selection rules (Track 2 §6b)
- `strike_grid` — strike step + ATM rounding per underlying (TBD)
- `expiry_calendar` — per-underlying weekly/monthly expiry day registry (TBD)
- `risk_free` — T-bill rate table for BSM (TBD)
- `greeks` — Black-Scholes-Merton wrapper around Rust kernel (TBD)
- `costs` — round-trip cost calculator (TBD; lives at `brutex/costs/`)

Per `docs/track2/TRACK2_OPTIONS_SPEC.md`. All modules satisfy the
COMMON · DYNAMIC · SCALABLE · AUTOMATED principle:
- COMMON — same code used by backtest + live TickVault
- DYNAMIC — add new underlying / new lot revision without re-architecting
- SCALABLE — O(1) per-bar lookup
- AUTOMATED — pure functions, no I/O
- COMPREHENSIVE — every lot transition has a test
"""

from brutex.options.lot_size_history import (
    LotSizeChange,
    OptionsUnderlying,
    all_current_lot_sizes,
    all_transitions,
    get_lot_size,
)
from brutex.options.strike_rules import (
    ALL_STRIKE_RULES,
    DELTA_TARGET_RULES,
    OFFSET_RULES,
    RULES_BY_NAME,
    StrikeRule,
    StrikeRuleKind,
    TradeDirection,
    resolve_delta_target_strike,
    resolve_offset_strike,
)

__all__ = [
    # Lot size history
    "LotSizeChange",
    "OptionsUnderlying",
    "all_current_lot_sizes",
    "all_transitions",
    "get_lot_size",
    # Strike rules
    "ALL_STRIKE_RULES",
    "DELTA_TARGET_RULES",
    "OFFSET_RULES",
    "RULES_BY_NAME",
    "StrikeRule",
    "StrikeRuleKind",
    "TradeDirection",
    "resolve_delta_target_strike",
    "resolve_offset_strike",
]
