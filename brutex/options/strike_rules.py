"""Pre-trade strike selection rules — 10 locked rules per Track 2 spec §6b.

Per `docs/track2/TRACK2_OPTIONS_SPEC.md` §6b — for each Track 1
signal that fires, the engine picks a strike using ONE of these 10
rules. The rule is fixed BEFORE the trade (no hindsight bias).

## The 10 rules

OFFSET-BASED (work from Phase 2, no Greeks needed):
  1. `atm`                    — strike closest to spot at signal time
  2. `atm_plus_1_in_dir`      — 1 step further OTM in trade direction
  3. `atm_plus_2_in_dir`      — 2 steps further OTM
  4. `atm_plus_5_in_dir`      — 5 steps further OTM
  5. `atm_minus_1_in_dir`     — 1 step ITM in trade direction
  6. `atm_minus_2_in_dir`     — 2 steps ITM
  7. `atm_minus_5_in_dir`     — 5 steps ITM (deep)

DELTA-TARGET (require Greeks, enabled Phase 4):
  8. `delta_target_030`       — strike where |delta| ≈ 0.30
  9. `delta_target_050`       — strike where |delta| ≈ 0.50 (~ATM)
  10. `delta_target_015`      — strike where |delta| ≈ 0.15

## Direction semantics

For CE (call, bull trade):
  - `atm_plus_N`  → strike = ATM + N × step (HIGHER price, more OTM)
  - `atm_minus_N` → strike = ATM - N × step (LOWER price, more ITM)

For PE (put, bear trade):
  - `atm_plus_N`  → strike = ATM - N × step (LOWER price, more OTM)
  - `atm_minus_N` → strike = ATM + N × step (HIGHER price, more ITM)

So `atm_plus_N_in_dir` always means "further OTM in the trade direction."

## COMMON · DYNAMIC · SCALABLE · AUTOMATED compliance

- **COMMON**: same module imported by backtest engine + future TickVault live engine
- **DYNAMIC**: add a new rule by appending; no re-architecting
- **SCALABLE**: O(1) per-signal rule lookup (10 ALU ops for offset, scan-21 for delta)
- **AUTOMATED**: pure function (rule, spot, expiry_chain, direction) → strike
- **COMPREHENSIVE**: every rule has a unit test

## Strategy mask integration

The strike rule is identified by NAME (not index) so strategy masks
remain valid across releases. New rules can be appended without breaking
existing strategies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from brutex import optcost_rust


class TradeDirection(Enum):
    """Bull (buy CE) or bear (buy PE)."""

    BULL = "bull"  # CE
    BEAR = "bear"  # PE


class StrikeRuleKind(Enum):
    """How the rule computes the strike."""

    OFFSET = "offset"  # Rules 1-7: integer step offset from ATM
    DELTA_TARGET = "delta_target"  # Rules 8-10: pick strike with |delta| closest to target


#: Phase 2 enables OFFSET rules (no Greeks needed).
_OFFSET_RULES_AVAILABLE_PHASE: Final[int] = 2
#: Phase 4 enables DELTA_TARGET rules (Greeks pipeline online).
_DELTA_RULES_AVAILABLE_PHASE: Final[int] = 4
#: Track 2 spec §6b locks exactly 10 pre-trade strike-selection rules.
_EXPECTED_RULE_COUNT: Final[int] = 10


@dataclass(frozen=True)
class StrikeRule:
    """One pre-trade strike-selection rule.

    Attributes:
        name: Stable identifier (e.g., 'atm_plus_2_in_dir').
        kind: OFFSET or DELTA_TARGET.
        param: For OFFSET: signed integer step count (negative = ITM direction).
               For DELTA_TARGET: float target |delta| (e.g., 0.30).
        requires_greeks: True for DELTA_TARGET rules (need BSM pipeline).
    """

    name: str
    kind: StrikeRuleKind
    param: float
    requires_greeks: bool

    def applies_to_phase(self, phase: int) -> bool:
        """Rules 1-7 (OFFSET) work from Phase 2. Rules 8-10 need Phase 4 Greeks."""
        if self.requires_greeks:
            return phase >= _DELTA_RULES_AVAILABLE_PHASE
        return phase >= _OFFSET_RULES_AVAILABLE_PHASE


# The 10 locked pre-trade rules. Order matches Track 2 spec §6b table.
ALL_STRIKE_RULES: Final[tuple[StrikeRule, ...]] = (
    StrikeRule("atm", StrikeRuleKind.OFFSET, 0, False),
    StrikeRule("atm_plus_1_in_dir", StrikeRuleKind.OFFSET, 1, False),
    StrikeRule("atm_plus_2_in_dir", StrikeRuleKind.OFFSET, 2, False),
    StrikeRule("atm_plus_5_in_dir", StrikeRuleKind.OFFSET, 5, False),
    StrikeRule("atm_minus_1_in_dir", StrikeRuleKind.OFFSET, -1, False),
    StrikeRule("atm_minus_2_in_dir", StrikeRuleKind.OFFSET, -2, False),
    StrikeRule("atm_minus_5_in_dir", StrikeRuleKind.OFFSET, -5, False),
    StrikeRule("delta_target_030", StrikeRuleKind.DELTA_TARGET, 0.30, True),
    StrikeRule("delta_target_050", StrikeRuleKind.DELTA_TARGET, 0.50, True),
    StrikeRule("delta_target_015", StrikeRuleKind.DELTA_TARGET, 0.15, True),
)

if (
    len(ALL_STRIKE_RULES) != _EXPECTED_RULE_COUNT
):  # pragma: no cover — defensive module-load assertion
    raise AssertionError(
        f"Expected {_EXPECTED_RULE_COUNT} strike rules per Track 2 spec §6b, "
        f"got {len(ALL_STRIKE_RULES)}"
    )
if (
    len({r.name for r in ALL_STRIKE_RULES}) != _EXPECTED_RULE_COUNT
):  # pragma: no cover — defensive module-load assertion
    raise AssertionError("rule names must be unique")

# Convenience lookups (re-exported via brutex.options package __init__ +
# consumed by tests/unit/test_strike_rules.py — CodeQL false-positive otherwise).
RULES_BY_NAME: Final[dict[str, StrikeRule]] = {r.name: r for r in ALL_STRIKE_RULES}
OFFSET_RULES: Final[tuple[StrikeRule, ...]] = tuple(
    r for r in ALL_STRIKE_RULES if r.kind == StrikeRuleKind.OFFSET
)
DELTA_TARGET_RULES: Final[tuple[StrikeRule, ...]] = tuple(
    r for r in ALL_STRIKE_RULES if r.kind == StrikeRuleKind.DELTA_TARGET
)

# Explicit public API (suppresses CodeQL "unused global variable" false positives
# for the three convenience-lookup constants above).
__all__ = [
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


def resolve_offset_strike(
    rule: StrikeRule,
    direction: TradeDirection,
    atm_strike: int,
    strike_step: int,
) -> int:
    """Resolve a strike from an OFFSET-kind rule.

    Args:
        rule: Must be an OFFSET rule (raises if not).
        direction: BULL (CE) or BEAR (PE).
        atm_strike: The ATM strike at signal time (rounded to grid).
        strike_step: The strike step for the underlying (NIFTY=50, BN=100, SX=100).

    Returns:
        The resolved strike (integer, in the underlying's price scale).

    Semantics:
        - BULL CE atm_plus_N  → strike = ATM + N × step (higher, more OTM)
        - BULL CE atm_minus_N → strike = ATM - N × step (lower, more ITM)
        - BEAR PE atm_plus_N  → strike = ATM - N × step (lower, more OTM)
        - BEAR PE atm_minus_N → strike = ATM + N × step (higher, more ITM)

    The OTM/ITM mapping is symmetric across direction: "plus" always
    means "further OTM in the trade direction."

    DEC-OPTCOST-TAILS-RUST-001: with ``BRUTEX_OPTCOST_TAILS_RUST`` truthy the
    arithmetic runs on the ``fno_math`` ``optcost_offset_strike`` kernel (the
    non-OFFSET-rule ``ValueError`` stays decided HERE, pre-FFI, byte-identical);
    flag OFF — the default — is byte-identical.
    """
    if rule.kind != StrikeRuleKind.OFFSET:
        raise ValueError(f"resolve_offset_strike called on non-OFFSET rule {rule.name!r}")
    step_count = int(rule.param)
    if optcost_rust.optcost_rust_enabled():
        return optcost_rust.offset_strike(
            atm_strike,
            step_count,
            strike_step,
            is_bull=direction == TradeDirection.BULL,
            python=lambda: _resolve_offset_strike_python(
                step_count, direction, atm_strike, strike_step
            ),
        )
    return _resolve_offset_strike_python(step_count, direction, atm_strike, strike_step)


def _resolve_offset_strike_python(
    step_count: int,
    direction: TradeDirection,
    atm_strike: int,
    strike_step: int,
) -> int:
    """The verbatim arithmetic arm of :func:`resolve_offset_strike` — the live oracle."""
    if direction == TradeDirection.BULL:
        # CE: OTM means higher strike
        return atm_strike + step_count * strike_step
    # BEAR: PE OTM means lower strike — invert sign
    return atm_strike - step_count * strike_step


def resolve_delta_target_strike(
    rule: StrikeRule,
    direction: TradeDirection,
    strike_chain: list[tuple[int, float]],
) -> int:
    """Resolve a strike from a DELTA_TARGET rule.

    Args:
        rule: Must be a DELTA_TARGET rule (raises if not).
        direction: BULL (CE) or BEAR (PE).
        strike_chain: List of (strike, delta) tuples for the underlying's
            chain at signal time. Delta is signed: CE positive, PE negative.

    Returns:
        The strike whose |delta| is closest to rule.param. Ties broken
        by preferring the strike closer to the chain's median (which is
        ATM-equivalent for symmetric chains).

    The strike_chain must contain ONLY the option type matching the trade
    direction (caller filters to CEs for BULL or PEs for BEAR).
    """
    if rule.kind != StrikeRuleKind.DELTA_TARGET:
        raise ValueError(f"resolve_delta_target_strike called on non-DELTA rule {rule.name!r}")
    if not strike_chain:
        raise ValueError("strike_chain is empty; cannot resolve delta target")
    target = rule.param
    # Find strike with min |abs(delta) - target|.
    # Tie-break: prefer strike closer to chain median (ATM-equivalent for
    # symmetric chains). Round delta-distance to 6 decimals so true ties
    # (within FP precision) get resolved by the secondary key — without
    # rounding, FP error of ~1e-17 can flip the tie-break.
    median_strike = sorted(strike_chain)[len(strike_chain) // 2][0]
    best = min(
        strike_chain,
        key=lambda sd: (round(abs(abs(sd[1]) - target), 6), abs(sd[0] - median_strike)),
    )
    return best[0]
