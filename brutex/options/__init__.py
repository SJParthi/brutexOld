"""Track 2 — Options realism leg.

.. note::

    **Scope:** This package is **TRACK 2 reference data**. Track 1 (the
    locked spot-index brute-force backtest per CLAUDE.md §3) does NOT
    use options data in its 52-condition vocabulary or its 5-stage
    funnel. The package lives inside ``brutex/`` because the cost
    calculator (`brutex/costs/calculator.py`) needs lot-size + expiry
    lookups when Track 2 lands. The DEC-P0-040 requirements review
    locked Q-08 = **annotate** (don't delete) to avoid cascading test
    breaks across 6+ test files that import these modules.

Modules:
- `lot_size_history` — per-date lot size lookup (SEBI changes 2021-2026)
- `strike_rules` — 10 pre-trade strike selection rules (Track 2 §6b)
- `strike_grid` — strike step + ATM rounding per underlying
- `expiry_calendar` — per-underlying weekly + monthly expiry day registry
- `risk_free` — T-bill rate table for BSM (TBD)
- `greeks` — Black-Scholes-Merton wrapper around Rust kernel (TBD)

Per ``docs/track2/TRACK2_OPTIONS_SPEC.md``. All modules satisfy the
COMMON · DYNAMIC · SCALABLE · AUTOMATED principle:
- COMMON — same code used by backtest + live (when Track 2 lands)
- DYNAMIC — add new underlying / new lot revision without re-architecting
- SCALABLE — O(1) per-bar lookup
- AUTOMATED — pure functions, no I/O
- COMPREHENSIVE — every transition has a test
"""

from brutex.options.executor import (
    AuditAppend,
    ExpiryKind,
    MaterializedTrade,
    OptionBarPair,
    OptionBarSource,
    OptionLeg,
    OptionsRuleResult,
    RejectedTrade,
    RejectReason,
    SpotSignal,
    SpotTrade,
    execute_options_sim,
    materialize_trade,
    resolve_current_expiry,
    run_rule_over_signals,
    select_option_leg,
    signals_from_spot_trades,
)
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
    "ALL_STRIKE_RULES",
    "DELTA_TARGET_RULES",
    "FRIDAY",
    "MONDAY",
    "NO_WEEKLY",
    "OFFSET_RULES",
    "RULES_BY_NAME",
    "STRIKE_STEP",
    "THURSDAY",
    "TUESDAY",
    "WEDNESDAY",
    "AuditAppend",
    "ExpiryKind",
    "LotSizeChange",
    "MaterializedTrade",
    "OptionBarPair",
    "OptionBarSource",
    "OptionLeg",
    "OptionsRuleResult",
    "OptionsUnderlying",
    "RejectReason",
    "RejectedTrade",
    "SpotSignal",
    "SpotTrade",
    "StrikeRule",
    "StrikeRuleKind",
    "TradeDirection",
    "all_current_lot_sizes",
    "all_transitions",
    "execute_options_sim",
    "get_lot_size",
    "get_monthly_dow",
    "get_strike_step",
    "get_weekly_dow",
    "grid_around_atm",
    "has_weekly",
    "materialize_trade",
    "next_monthly_expiry",
    "next_weekly_expiry",
    "resolve_current_expiry",
    "resolve_delta_target_strike",
    "resolve_offset_strike",
    "round_to_atm",
    "run_rule_over_signals",
    "select_option_leg",
    "signals_from_spot_trades",
    "strike_offset_from_atm",
]
