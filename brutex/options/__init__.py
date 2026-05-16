"""Track 2 — Options realism leg.

Modules:
- `lot_size_history` — per-date lot size lookup (SEBI changes 2021-2026)
- `strike_rules` — 10 pre-trade strike selection rules (Track 2 §6b)
- `strike_grid` — strike step + ATM rounding per underlying
- `expiry_calendar` — per-underlying weekly + monthly expiry day registry
- `risk_free` — T-bill rate table for BSM (TBD)
- `greeks` — Black-Scholes-Merton wrapper around Rust kernel (TBD)

Per `docs/track2/TRACK2_OPTIONS_SPEC.md`. All modules satisfy the
COMMON · DYNAMIC · SCALABLE · AUTOMATED principle:
- COMMON — same code used by backtest + live TickVault
- DYNAMIC — add new underlying / new lot revision without re-architecting
- SCALABLE — O(1) per-bar lookup
- AUTOMATED — pure functions, no I/O
- COMPREHENSIVE — every transition has a test
"""

from brutex.options.expiry_calendar import (
    FRIDAY,
    MONDAY,
    NO_WEEKLY,
    THURSDAY,
    TUESDAY,
    WEDNESDAY,
    get_monthly_dow,
    get_weekly_dow,
    has_weekly,
    next_monthly_expiry,
    next_weekly_expiry,
)
from brutex.options.lot_size_history import (
    LotSizeChange,
    OptionsUnderlying,
    all_current_lot_sizes,
    all_transitions,
    get_lot_size,
)
from brutex.options.strike_grid import (
    STRIKE_STEP,
    get_strike_step,
    grid_around_atm,
    round_to_atm,
    strike_offset_from_atm,
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
    # Strike grid
    "STRIKE_STEP",
    "get_strike_step",
    "grid_around_atm",
    "round_to_atm",
    "strike_offset_from_atm",
    # Expiry calendar
    "FRIDAY",
    "MONDAY",
    "NO_WEEKLY",
    "THURSDAY",
    "TUESDAY",
    "WEDNESDAY",
    "get_monthly_dow",
    "get_weekly_dow",
    "has_weekly",
    "next_monthly_expiry",
    "next_weekly_expiry",
]
