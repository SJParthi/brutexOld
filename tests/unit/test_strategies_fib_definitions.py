"""Unit tests for the 3 Fib strategy DATA declarations (DEC-FIB-STRATEGY-REGISTRY-001).

There are exactly THREE Fib strategies (one per ANCHOR); DIRECTION (Long/Short) is a
SWEPT DIMENSION inside each, NOT a separate strategy — the sweep tries BOTH on each
anchor. Every claim is grounded on the REAL path: the registry holds the 3
module-level definitions, ``expand`` produces a Long AND a Short concrete spec per
mask, the anchors resolve via the actual ``prev_day_ohlc`` / ``weekly_anchors``
primitives into real ``fib_levels``, and the decoder bridge runs the FROZEN
``decode_combo``. 100% line + 100% branch on ``brutex.strategies.fib_definitions``.
"""

from __future__ import annotations

from datetime import date, time

import pytest
from brutex.indicators.fib_levels import FibLevels
from brutex.indicators.prev_day_ohlc import DailyCandle
from brutex.strategies import fib_definitions as F
from brutex.strategies.errors import StrategyRegistrationError
from brutex.vocabulary.bits import (
    BIT_ABOVE_CPR_TC,
    BIT_EMA20_ABOVE_EMA200,
    BIT_NEAR_FIB_50,
    IMPLEMENTED_MASK,
    NAME_TO_BIT,
    TOTAL_CONDITIONS,
)


# ─────────────────────────── fixtures ───────────────────────────
def _dc(d: date, high: int, low: int) -> DailyCandle:
    """A daily candle with the given high/low (open=close=midpoint-ish)."""
    return DailyCandle(day=d, open_paisa=100_00, high_paisa=high, low_paisa=low, close_paisa=100_00)


# 2024-01-15 is a Monday in ISO week 3; the 5 prior trading sessions are
# 2024-01-08..12 (ISO week 2), which is also the last COMPLETED ISO week.
_TODAY = date(2024, 1, 15)
_PREV_TRADING_DAY = date(2024, 1, 12)
_WEEK_DAILY: dict[date, DailyCandle] = {
    date(2024, 1, 8): _dc(date(2024, 1, 8), 110_00, 95_00),
    date(2024, 1, 9): _dc(date(2024, 1, 9), 120_00, 90_00),  # the week high
    date(2024, 1, 10): _dc(date(2024, 1, 10), 115_00, 92_00),
    date(2024, 1, 11): _dc(date(2024, 1, 11), 118_00, 88_00),  # the week low
    date(2024, 1, 12): _dc(date(2024, 1, 12), 112_00, 91_00),
}
_WEEK_HIGH = 120_00
_WEEK_LOW = 88_00


# ─────────────────────────── Direction ───────────────────────────
def test_direction_as_u8_matches_rust_discriminant() -> None:
    assert F.Direction.LONG.as_u8() == 0
    assert F.Direction.SHORT.as_u8() == 1
    # decoder tokens
    assert F.Direction.LONG.value == "long"
    assert F.Direction.SHORT.value == "short"


# ─────────────────────────── ExitConfig ───────────────────────────
def test_exit_config_default_is_forced_exit_only_mode_b() -> None:
    cfg = F.ExitConfig()
    assert cfg.forced_exit_ist == time(15, 20)
    assert cfg.stop_loss_bps is None
    assert cfg.take_profit_bps is None
    assert cfg.has_sl_tp is False


def test_exit_config_mode_a_with_sl_tp() -> None:
    assert F.ExitConfig(stop_loss_bps=50).has_sl_tp is True
    assert F.ExitConfig(take_profit_bps=80).has_sl_tp is True
    assert F.ExitConfig(stop_loss_bps=50, take_profit_bps=80).has_sl_tp is True


def test_exit_config_rejects_non_positive_sl() -> None:
    with pytest.raises(ValueError, match="stop_loss_bps must be positive"):
        F.ExitConfig(stop_loss_bps=0)


def test_exit_config_rejects_non_positive_tp() -> None:
    with pytest.raises(ValueError, match="take_profit_bps must be positive"):
        F.ExitConfig(take_profit_bps=-1)


# ─────────────────────────── StrategyDefinition validation ───────────────────────────
def _defn(**kw: object) -> F.StrategyDefinition:
    base: dict[str, object] = {
        "name": "x",
        "anchor": F.FibAnchorKind.PREV_DAY,
        "symbols": ("SYM",),
    }
    base.update(kw)
    return F.StrategyDefinition(**base)  # type: ignore[arg-type]


def test_definition_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="name must be non-empty"):
        _defn(name="")


def test_definition_rejects_empty_symbols() -> None:
    with pytest.raises(ValueError, match="must list at least one symbol"):
        _defn(symbols=())


def test_definition_rejects_empty_directions() -> None:
    with pytest.raises(ValueError, match="must list at least one direction"):
        _defn(directions=())


def test_definition_rejects_empty_timeframes() -> None:
    with pytest.raises(ValueError, match="must list at least one timeframe"):
        _defn(timeframes=())


def test_definition_rejects_unknown_condition_scope() -> None:
    with pytest.raises(ValueError, match="unknown vocabulary condition"):
        _defn(condition_scope=frozenset({"not_a_real_condition"}))


def test_definition_accepts_known_condition_scope() -> None:
    d = _defn(condition_scope=frozenset({"near_fib_50", "ema20_above_ema200"}))
    assert d.condition_scope == frozenset({"near_fib_50", "ema20_above_ema200"})


def test_definition_rejects_retired_condition_scope() -> None:
    """A KNOWN-but-RETIRED name (VWAP tombstone bit 52) is refused at construction.

    Pre-fix, ``close_above_vwap`` passed the NAME_TO_BIT membership check and
    ``candidate_mask`` then violated its own "always a subset of
    IMPLEMENTED_MASK" contract. The refusal names the entry, its bit and its
    RETIRED status (the ``parse_condition_ids`` wording family).
    """
    with pytest.raises(ValueError, match=r"'close_above_vwap' \(bit 52\) is RETIRED"):
        _defn(condition_scope=frozenset({"close_above_vwap", "near_fib_50"}))


def test_definition_accepts_live_supertrend_scope() -> None:
    """The SuperTrend names construct — the vocab-66 implemented-flip
    ACCEPTANCE pin (crate 0.5.0; the former declared-only refusal for bits
    64/65 is consciously retired — the declared-but-NOT-YET-implemented
    refusal ARM keeps its own teeth in the monkeypatched layout-only test
    below)."""
    d = _defn(condition_scope=frozenset({"close_above_supertrend", "close_below_supertrend"}))
    assert d.candidate_mask == (1 << 64) | (1 << 65)
    assert d.candidate_mask & ~IMPLEMENTED_MASK == 0


def test_definition_refuses_a_declared_unimplemented_scope_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declared-but-NOT-YET-implemented (LAYOUT-ONLY) refusal arm keeps
    its exact wording covered.

    No LAYOUT-ONLY bit exists on today's 66-bit layout (the SuperTrend pair
    left that class at the vocab-66 implemented-flip), so the arm is kept
    honest by simulating a future manifest where an implemented, non-retired
    bit (64) is layout-only — the
    ``test_parse_condition_ids_refuses_a_declared_unimplemented_bit``
    monkeypatch precedent (tests/unit/test_cli_user_error_hygiene.py).
    """
    monkeypatch.setattr(F, "IMPLEMENTED_MASK", IMPLEMENTED_MASK & ~(1 << 64))
    with pytest.raises(
        ValueError,
        match=(
            r"'close_above_supertrend' \(bit 64\) is declared but "
            r"NOT YET implemented \(LAYOUT-ONLY\)"
        ),
    ) as excinfo:
        _defn(condition_scope=frozenset({"close_above_supertrend", "near_fib_50"}))
    assert "RETIRED" not in str(excinfo.value)


def test_definition_accepts_live_cpr_scope_and_mask_stays_implemented_subset() -> None:
    """Live names (incl. sign-bit narrow_cpr_day 63 + above_cpr_tc 60) construct,

    and the explicit-scope ``candidate_mask`` honours its documented
    IMPLEMENTED_MASK-subset invariant.
    """
    d = _defn(condition_scope=frozenset({"narrow_cpr_day", "above_cpr_tc", "near_fib_50"}))
    mask = d.candidate_mask
    assert mask == (1 << 63) | (1 << BIT_ABOVE_CPR_TC) | (1 << BIT_NEAR_FIB_50)
    assert mask & ~IMPLEMENTED_MASK == 0


# ─────────────────────────── directions (swept dimension) ───────────────────────────
def test_default_directions_are_both_long_and_short() -> None:
    assert _defn().directions == (F.Direction.LONG, F.Direction.SHORT)


def test_directions_can_be_restricted_to_one() -> None:
    d = _defn(directions=(F.Direction.SHORT,))
    assert d.directions == (F.Direction.SHORT,)


def test_directions_are_deduped_order_preserving() -> None:
    d = _defn(directions=(F.Direction.SHORT, F.Direction.LONG, F.Direction.SHORT))
    # first-seen order preserved, the duplicate dropped.
    assert d.directions == (F.Direction.SHORT, F.Direction.LONG)


def test_directions_already_canonical_left_untouched() -> None:
    # No duplicate → the no-rewrite branch (deduped == directions).
    d = _defn(directions=(F.Direction.LONG, F.Direction.SHORT))
    assert d.directions == (F.Direction.LONG, F.Direction.SHORT)


# ─────────────────────────── candidate_mask ───────────────────────────
def test_candidate_mask_empty_scope_is_full_implemented() -> None:
    assert _defn().candidate_mask == IMPLEMENTED_MASK


def test_candidate_mask_explicit_scope_is_or_of_bits() -> None:
    d = _defn(condition_scope=frozenset({"near_fib_50", "ema20_above_ema200"}))
    assert d.candidate_mask == (1 << BIT_NEAR_FIB_50) | (1 << BIT_EMA20_ABOVE_EMA200)
    # Always a subset of IMPLEMENTED_MASK.
    assert d.candidate_mask & ~IMPLEMENTED_MASK == 0


# ─────────────────────────── spec_args (the Rust ABI bridge) ───────────────────────────
def test_spec_args_projects_hex_and_chosen_direction() -> None:
    mask = (1 << BIT_NEAR_FIB_50) | (1 << BIT_EMA20_ABOVE_EMA200)
    mask_hex, direction_u8 = F.PREV_DAY.spec_args(mask, F.Direction.LONG)
    assert mask_hex == f"{mask:032x}"
    assert len(mask_hex) == 32
    assert direction_u8 == 0
    # The SAME strategy sweeps the OTHER direction too — only the byte flips.
    _, short_dir = F.PREV_DAY.spec_args(mask, F.Direction.SHORT)
    assert short_dir == 1


def test_spec_args_rejects_out_of_scope_bit() -> None:
    d = _defn(condition_scope=frozenset({"near_fib_50"}))
    with pytest.raises(ValueError, match="outside strategy 'x' candidate scope"):
        d.spec_args((1 << BIT_NEAR_FIB_50) | (1 << BIT_EMA20_ABOVE_EMA200), F.Direction.LONG)


def test_spec_args_rejects_direction_not_swept() -> None:
    d = _defn(directions=(F.Direction.LONG,))  # short NOT swept
    with pytest.raises(ValueError, match="direction 'short' is not swept by strategy 'x'"):
        d.spec_args(1 << BIT_NEAR_FIB_50, F.Direction.SHORT)


def test_spec_args_in_scope_passes() -> None:
    d = _defn(condition_scope=frozenset({"near_fib_50"}))
    mask_hex, _ = d.spec_args(1 << BIT_NEAR_FIB_50, F.Direction.LONG)
    assert int(mask_hex, 16) == 1 << BIT_NEAR_FIB_50


# ─────────────────────────── expand (direction is swept) ───────────────────────────
def test_expand_produces_both_directions() -> None:
    mask = (1 << BIT_NEAR_FIB_50) | (1 << BIT_EMA20_ABOVE_EMA200)
    specs = F.PREV_DAY.expand(mask)
    # One concrete spec PER swept direction — Long AND Short.
    assert len(specs) == 2
    assert [s.direction for s in specs] == [F.Direction.LONG, F.Direction.SHORT]
    assert [s.direction_u8 for s in specs] == [0, 1]
    # Both carry the SAME mask (only the direction differs) + the strategy name.
    assert {s.condition_mask_hex for s in specs} == {f"{mask:032x}"}
    assert {s.strategy_name for s in specs} == {"fib_prev_day"}


def test_expand_respects_a_single_direction() -> None:
    d = _defn(directions=(F.Direction.SHORT,))
    specs = d.expand(1 << BIT_NEAR_FIB_50)
    assert len(specs) == 1
    assert specs[0].direction is F.Direction.SHORT
    assert specs[0].direction_u8 == 1


def test_expand_propagates_scope_error() -> None:
    d = _defn(condition_scope=frozenset({"near_fib_50"}))
    with pytest.raises(ValueError, match="outside strategy 'x' candidate scope"):
        d.expand((1 << BIT_NEAR_FIB_50) | (1 << BIT_EMA20_ABOVE_EMA200))


def test_concrete_spec_is_frozen_record() -> None:
    spec = F.PREV_DAY.expand(1 << BIT_NEAR_FIB_50)[0]
    assert isinstance(spec, F.ConcreteSpec)
    assert spec.strategy_name == "fib_prev_day"


# ─────────────────────────── for_symbols (symbol-agnostic) ───────────────────────────
def test_for_symbols_resymbols_without_rewrite() -> None:
    # The SAME declaration runs for a different symbol set — flip the tuple.
    alt = F.for_symbols(F.PREV_DAY, ["NSE-BANKNIFTY"])
    assert alt.symbols == ("NSE-BANKNIFTY",)
    assert alt.name == "fib_prev_day@NSE-BANKNIFTY"
    # Everything else (the strategy logic) is unchanged — only the symbol moved.
    assert alt.anchor is F.PREV_DAY.anchor
    assert alt.directions == F.PREV_DAY.directions
    assert alt.exit == F.PREV_DAY.exit
    assert alt.timeframes == F.PREV_DAY.timeframes


def test_for_symbols_multi_symbol_name() -> None:
    alt = F.for_symbols(F.ROLLING_WEEKLY, ("A", "B"))
    assert alt.symbols == ("A", "B")
    assert alt.name == "fib_rolling_weekly@A+B"


def test_for_symbols_rejects_empty() -> None:
    with pytest.raises(ValueError, match="requires at least one symbol"):
        F.for_symbols(F.PREV_DAY, [])


def test_same_definition_resolves_for_any_symbol() -> None:
    # Symbol-agnostic anchor resolution: a re-symboled copy resolves identically
    # (the levels depend on the daily candles, not the symbol string).
    alt = F.for_symbols(F.PREV_DAY, ["NSE-BANKNIFTY"])
    base_levels = F.resolve_fib_levels(F.PREV_DAY, _TODAY, _WEEK_DAILY)
    alt_levels = F.resolve_fib_levels(alt, _TODAY, _WEEK_DAILY)
    assert base_levels == alt_levels


# ─────────────────────────── registry / one-line add ───────────────────────────
def test_module_registry_holds_exactly_three() -> None:
    assert F.fib_registry.names() == (
        "fib_prev_day",
        "fib_rolling_weekly",
        "fib_weekly_static",
    )
    assert len(F.fib_registry.all()) == 3


def test_adding_a_fourth_strategy_is_one_register_line() -> None:
    """The operator's core ask: adding a strategy = ONE register() line."""
    reg = F.FibStrategyRegistry()
    # ── ONE line ── (a 4th ANCHOR-strategy; direction stays a swept dimension)
    fourth = reg.register(
        F.StrategyDefinition(
            name="fib_prev_day_banknifty",
            anchor=F.FibAnchorKind.PREV_DAY,
            symbols=("NSE-BANKNIFTY",),
        )
    )
    # It appears in the registry and is valid + runnable + sweeps both directions.
    assert "fib_prev_day_banknifty" in reg.names()
    assert reg.get("fib_prev_day_banknifty") is fourth
    assert fourth.candidate_mask == IMPLEMENTED_MASK
    assert fourth.directions == (F.Direction.LONG, F.Direction.SHORT)
    levels = F.resolve_fib_levels(fourth, _TODAY, _WEEK_DAILY)
    assert isinstance(levels, FibLevels)


def test_registry_rejects_duplicate_name() -> None:
    reg = F.FibStrategyRegistry()
    reg.register(_defn(name="dup"))
    with pytest.raises(StrategyRegistrationError, match="already registered"):
        reg.register(_defn(name="dup"))


def test_registry_get_unknown_raises() -> None:
    reg = F.FibStrategyRegistry()
    with pytest.raises(StrategyRegistrationError, match="no strategy registered under name 'nope'"):
        reg.get("nope")


def test_registry_clear() -> None:
    reg = F.FibStrategyRegistry()
    reg.register(_defn(name="a"))
    reg.register(_defn(name="b"))
    assert reg.names() == ("a", "b")
    reg.clear()
    assert reg.names() == ()


def test_module_level_register_uses_default_registry() -> None:
    # The module-level register() writes to fib_registry — proven WITHOUT
    # polluting the global registry: re-registering one of the canonical 3 names
    # raises, which can only happen if register() targets fib_registry.
    with pytest.raises(StrategyRegistrationError, match="already registered"):
        F.register(_defn(name="fib_prev_day"))
    # The 3 are untouched.
    assert len(F.fib_registry.all()) == 3


# ─────────────────────────── the 3 definitions: construction + identity ───────────────────────────
@pytest.mark.parametrize(
    ("defn", "anchor"),
    [
        (F.PREV_DAY, F.FibAnchorKind.PREV_DAY),
        (F.ROLLING_WEEKLY, F.FibAnchorKind.ROLLING_WEEKLY),
        (F.WEEKLY_STATIC, F.FibAnchorKind.WEEKLY_STATIC),
    ],
)
def test_each_definition_has_correct_anchor_and_sweeps_both_directions(
    defn: F.StrategyDefinition,
    anchor: F.FibAnchorKind,
) -> None:
    assert defn.anchor is anchor
    # Direction is a swept dimension — every strategy sweeps BOTH by default.
    assert defn.directions == (F.Direction.LONG, F.Direction.SHORT)
    assert defn.symbols == ("NSE-NIFTY",)
    assert defn.exit.forced_exit_ist == time(15, 20)
    assert defn.timeframes == ("1minute",)
    assert defn.description


def test_the_three_cover_the_three_anchors_each_with_both_directions() -> None:
    anchors = {d.anchor for d in F.fib_registry.all()}
    assert anchors == set(F.FibAnchorKind)
    # Every strategy sweeps both directions (3 strategies × 2 swept dirs = 6
    # concrete (anchor, direction) sweep points, but only 3 STRATEGIES).
    for d in F.fib_registry.all():
        assert set(d.directions) == set(F.Direction)


# ─────────────────────────── anchor resolution (runnable, not a stub) ───────────────────────────
def test_prev_day_anchor_resolves_to_real_levels() -> None:
    levels = F.resolve_fib_levels(F.PREV_DAY, _TODAY, _WEEK_DAILY)
    assert levels is not None
    prev = _WEEK_DAILY[_PREV_TRADING_DAY]
    assert levels.pdh_paisa == prev.high_paisa
    assert levels.pdl_paisa == prev.low_paisa
    # midpoint sanity
    assert levels.retrace_50_paisa == (prev.high_paisa + prev.low_paisa) // 2


def test_rolling_weekly_anchor_uses_trailing_5_session_extremes() -> None:
    levels = F.resolve_fib_levels(F.ROLLING_WEEKLY, _TODAY, _WEEK_DAILY)
    assert levels is not None
    assert levels.pdh_paisa == _WEEK_HIGH
    assert levels.pdl_paisa == _WEEK_LOW


def test_weekly_static_anchor_uses_last_completed_week_extremes() -> None:
    levels = F.resolve_fib_levels(F.WEEKLY_STATIC, _TODAY, _WEEK_DAILY)
    assert levels is not None
    # The completed prior ISO week is the same 5 sessions here.
    assert levels.pdh_paisa == _WEEK_HIGH
    assert levels.pdl_paisa == _WEEK_LOW


def test_levels_are_direction_agnostic() -> None:
    # Direction is a swept dimension; the LEVELS do not depend on it. Resolution
    # takes only the definition (anchor) — both swept directions of one strategy
    # share these per-day levels.
    levels = F.resolve_fib_levels(F.PREV_DAY, _TODAY, _WEEK_DAILY)
    assert levels is not None
    # Both directions render against the same mask → same regime/levels basis.
    assert F.Direction.LONG in F.PREV_DAY.directions
    assert F.Direction.SHORT in F.PREV_DAY.directions


# ─────────────────────────── cold-start (honest None, not a crash) ───────────────────────────
def test_prev_day_cold_start_returns_none() -> None:
    # No prior daily candle in the dict.
    assert F.resolve_fib_levels(F.PREV_DAY, _TODAY, {}) is None


def test_rolling_weekly_cold_start_returns_none() -> None:
    # Fewer than 5 prior sessions available.
    partial = dict(list(_WEEK_DAILY.items())[:3])
    assert F.resolve_fib_levels(F.ROLLING_WEEKLY, _TODAY, partial) is None


def test_weekly_static_cold_start_returns_none() -> None:
    # No completed prior ISO week (empty daily dict).
    assert F.resolve_fib_levels(F.WEEKLY_STATIC, _TODAY, {}) is None


# ─────────────────────────── decoder bridge (Simple + Pro) ───────────────────────────
def test_render_definition_default_timeframe_long() -> None:
    mask = (1 << BIT_NEAR_FIB_50) | (1 << BIT_EMA20_ABOVE_EMA200) | (1 << BIT_ABOVE_CPR_TC)
    exp = F.render_definition(F.PREV_DAY, mask, F.Direction.LONG)
    assert exp.direction_label == "Buy"
    assert exp.simple_sentence  # non-empty
    assert exp.pro_sentence
    # default timeframe is the definition's first declared TF.
    assert "1minute" in exp.simple_sentence
    assert "Fib 50%" in exp.pro_sentence


def test_render_definition_timeframe_override_and_short() -> None:
    mask = 1 << BIT_NEAR_FIB_50
    exp = F.render_definition(F.PREV_DAY, mask, F.Direction.SHORT, timeframe="15m")
    assert exp.direction_label == "Sell"
    assert "15m" in exp.simple_sentence
    assert "1minute" not in exp.simple_sentence


def test_render_definition_validates_scope() -> None:
    d = F.StrategyDefinition(
        name="scoped",
        anchor=F.FibAnchorKind.PREV_DAY,
        symbols=("SYM",),
        condition_scope=frozenset({"near_fib_50"}),
    )
    with pytest.raises(ValueError, match="outside strategy 'scoped' candidate scope"):
        F.render_definition(
            d, (1 << BIT_NEAR_FIB_50) | (1 << BIT_EMA20_ABOVE_EMA200), F.Direction.LONG
        )


def test_render_definition_validates_direction_swept() -> None:
    d = F.StrategyDefinition(
        name="long_only",
        anchor=F.FibAnchorKind.PREV_DAY,
        symbols=("SYM",),
        directions=(F.Direction.LONG,),
    )
    with pytest.raises(ValueError, match="direction 'short' is not swept"):
        F.render_definition(d, 1 << BIT_NEAR_FIB_50, F.Direction.SHORT)


# ─────────────────────────── module constants sanity ───────────────────────────
def test_forced_exit_constant() -> None:
    assert time(15, 20) == F.FORCED_EXIT_IST
    assert F.ROLLING_WEEKLY_SESSIONS == 5


def test_candidate_mask_never_exceeds_total_conditions() -> None:
    # Every implemented bit is below TOTAL_CONDITIONS.
    for name, bit in NAME_TO_BIT.items():
        assert bit < TOTAL_CONDITIONS, name
