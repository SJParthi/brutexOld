"""Tests for the dual-layer (Simple + Pro) combo decoder.

100% line + branch on ``brutex.dashboard.combo_decoder``. Every claim is proven
through the real decode path (no stubs): a frozen golden of the operator's
approved example, one crafted mask per regime rule, edge cases (empty/full
mask, both directions), and the manifest-sync coverage test that fails the
build if a future condition is added without a dual label.
"""
# Whitebox test: the manifest-sync and group-partition invariants must read the
# decoder's internal label/group tables (the ONE place they live), so private
# access is intentional here (mirrors the repo's whitebox-test convention).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import pytest
from brutex.dashboard.combo_decoder import (
    _CONDITION_LABELS as CONDITION_LABELS,
)
from brutex.dashboard.combo_decoder import (
    _GROUP_ORDER as GROUP_ORDER,
)
from brutex.dashboard.combo_decoder import (
    ComboExplanation,
    ConditionLabel,
    decode_combo,
)
from brutex.vocabulary.bits import NAME_TO_BIT, TOTAL_CONDITIONS


def _mask(*names: str) -> int:
    """Build a condition mask from condition names."""
    m = 0
    for n in names:
        m |= 1 << NAME_TO_BIT[n]
    return m


# ───────────────────────────── Manifest sync ─────────────────────────────


def test_dual_label_dict_covers_every_one_of_the_60_names() -> None:
    """The dual-label dict keys are EXACTLY the 63 manifest condition names.

    If a future condition is appended to ``conditions.toml`` / ``bits.py``
    without a Pro+Simple label, this test FAILS — no silent gaps.
    """
    assert set(CONDITION_LABELS.keys()) == set(NAME_TO_BIT.keys())
    # 66 -> 74 at DEC-FIB-TEMPLATE-EXPRESS-001 (MIGRATION.md #2205).
    assert len(CONDITION_LABELS) == TOTAL_CONDITIONS == 74


def test_every_label_is_a_nonempty_pro_and_simple_pair() -> None:
    """Every condition has a non-empty Pro AND Simple phrase."""
    for name, (pro, simple) in CONDITION_LABELS.items():
        assert pro.strip(), name
        assert simple.strip(), name


def test_group_order_partitions_every_declared_name_exactly() -> None:
    """The four sentence groups partition all 66 declared names exactly once.

    A name not in any group would be silently dropped from the sentence; this
    proves every condition contributes to the composed sentence.
    """
    covered: list[str] = [name for group in GROUP_ORDER for name in group]
    assert sorted(covered) == sorted(NAME_TO_BIT.keys())
    assert len(covered) == len(set(covered)) == TOTAL_CONDITIONS


# ───────────────────────────── Golden example ─────────────────────────────


def test_golden_operator_example() -> None:
    """The operator-approved example combo, frozen string-for-string."""
    mask = _mask(
        "close_above_vwap",
        "ema20_above_ema200",
        "near_pivot_r1",
        "near_fib_50",
        "choch_bullish",
        "early_morning",
    )
    exp = decode_combo(mask, "long", "15m")

    assert isinstance(exp, ComboExplanation)
    assert exp.direction_label == "Buy"
    assert exp.regime_label == "uptrend"
    # The VWAP member renders its RETIRED label (bits 52/53 are tombstones;
    # an OLD artifact's mask still decodes honestly, never live-sounding).
    assert exp.simple_sentence == (
        "Buy on 15m when trend is up, price beats the day's average price "
        "(retired condition), at a ceiling (resistance), at the halfway bounce zone, "
        "trend just flipped up and first hour (uptrend)."
    )
    assert exp.pro_sentence == (
        "Buy 15m [uptrend]: EMA20>EMA200, close>VWAP (retired), near R1, Fib 50%, "
        "CHoCH↑ and early morning."
    )

    # Per-condition labels — in ascending bit order.
    assert exp.conditions == (
        ConditionLabel(4, "ema20_above_ema200", "EMA20>EMA200", "trend is up"),
        ConditionLabel(7, "near_pivot_r1", "near R1", "at a ceiling (resistance)"),
        ConditionLabel(22, "near_fib_50", "Fib 50%", "at the halfway bounce zone"),
        ConditionLabel(44, "early_morning", "early morning", "first hour"),
        ConditionLabel(
            52,
            "close_above_vwap",
            "close>VWAP (retired)",
            "price beats the day's average price (retired condition)",
        ),
        ConditionLabel(58, "choch_bullish", "CHoCH↑", "trend just flipped up"),
    )


# ───────────────────────────── Regime rules ─────────────────────────────


def test_regime_uptrend() -> None:
    """ema20_above_ema200 alone → uptrend."""
    exp = decode_combo(_mask("ema20_above_ema200"), "long", "5m")
    assert exp.regime_label == "uptrend"


def test_regime_downtrend() -> None:
    """ema20_below_ema200 alone → downtrend."""
    exp = decode_combo(_mask("ema20_below_ema200"), "short", "5m")
    assert exp.regime_label == "downtrend"


def test_regime_reversal_choch_without_concordant_trend() -> None:
    """A CHoCH bit with no confirming EMA trend → reversal."""
    exp = decode_combo(_mask("choch_bearish"), "short", "1m")
    assert exp.regime_label == "reversal"


def test_regime_reversal_choch_discordant_with_trend() -> None:
    """A CHoCH↓ against an up-EMA (or vice versa) is a true flip → reversal."""
    exp = decode_combo(_mask("ema20_above_ema200", "choch_bearish"), "short", "1m")
    assert exp.regime_label == "reversal"


def test_regime_choch_concordant_is_continuation_not_reversal() -> None:
    """A CHoCH↓ confirming a down-EMA is a continuation → downtrend."""
    exp = decode_combo(_mask("ema20_below_ema200", "choch_bearish"), "short", "1m")
    assert exp.regime_label == "downtrend"


def test_regime_breakout() -> None:
    """A BOS bit (no CHoCH) → breakout."""
    exp = decode_combo(_mask("bos_bullish"), "long", "5m")
    assert exp.regime_label == "breakout"


def test_regime_volatile_large_body() -> None:
    """A large body → volatile."""
    exp = decode_combo(_mask("bar_large_body"), "long", "5m")
    assert exp.regime_label == "volatile"


def test_regime_volatile_outside_day() -> None:
    """An outside day → volatile."""
    exp = decode_combo(_mask("outside_day"), "long", "5m")
    assert exp.regime_label == "volatile"


def test_regime_sideways() -> None:
    """inside_day AND a small body → sideways."""
    exp = decode_combo(_mask("inside_day", "bar_small_body"), "long", "5m")
    assert exp.regime_label == "sideways"


def test_regime_sideways_requires_inside_day() -> None:
    """A small body WITHOUT inside_day is not sideways → mixed here."""
    exp = decode_combo(_mask("bar_small_body"), "long", "5m")
    assert exp.regime_label == "mixed"


def test_regime_mixed_no_signal() -> None:
    """No regime-driving bits → mixed."""
    exp = decode_combo(_mask("near_pivot_r1"), "long", "5m")
    assert exp.regime_label == "mixed"


def test_regime_mixed_conflicting_ema() -> None:
    """Both EMA-trend bits set (conflict) → mixed, not uptrend/downtrend."""
    exp = decode_combo(_mask("ema20_above_ema200", "ema20_below_ema200"), "long", "5m")
    assert exp.regime_label == "mixed"


# ───────────────────────────── Edge cases ─────────────────────────────


def test_empty_mask_is_direction_only() -> None:
    """Mask 0 → honest direction-only sentence, no conditions."""
    exp = decode_combo(0, "long", "15m")
    assert exp.conditions == ()
    assert exp.regime_label == "mixed"
    assert exp.direction_label == "Buy"
    assert exp.simple_sentence == "Buy on 15m (no conditions)."
    assert exp.pro_sentence == "Buy on 15m (no conditions)."


def test_sell_direction_short() -> None:
    """direction 'short' → Sell."""
    exp = decode_combo(_mask("close_below_ema200"), "short", "1d")
    assert exp.direction_label == "Sell"
    assert exp.pro_sentence.startswith("Sell 1d ")


def test_sell_direction_unknown_token_defaults_to_sell() -> None:
    """An unknown direction token defaults to Sell (not Buy)."""
    exp = decode_combo(_mask("close_below_ema200"), "FLAT", "1d")
    assert exp.direction_label == "Sell"


def test_buy_direction_buy_token() -> None:
    """direction 'buy' (any case) → Buy."""
    exp = decode_combo(_mask("close_above_ema200"), "BUY", "1d")
    assert exp.direction_label == "Buy"


def test_single_condition_no_and_join() -> None:
    """A single-condition mask joins with no 'and' (the len==1 branch)."""
    exp = decode_combo(_mask("near_pivot_s1"), "long", "5m")
    assert exp.simple_sentence == ("Buy on 5m when at a floor (support) (mixed).")
    assert exp.pro_sentence == "Buy 5m [mixed]: near S1."


def test_high_bits_ignored() -> None:
    """Bits >= TOTAL_CONDITIONS (forward-compat headroom) are ignored."""
    mask = _mask("near_pivot_r1") | (1 << 100)
    exp = decode_combo(mask, "long", "5m")
    assert [c.technical_name for c in exp.conditions] == ["near_pivot_r1"]


def test_all_implemented_bits_decode_and_group() -> None:
    """The all-conditions mask decodes every bit, in group order, no crash."""
    mask = 0
    for bit in range(TOTAL_CONDITIONS):
        mask |= 1 << bit
    exp = decode_combo(mask, "long", "15m")
    assert len(exp.conditions) == TOTAL_CONDITIONS
    # Every condition's pro + simple phrase appears in the sentences.
    for pro, simple in CONDITION_LABELS.values():
        assert pro in exp.pro_sentence
        assert simple in exp.simple_sentence
    # Sentences are non-empty and well-formed.
    assert exp.pro_sentence.endswith(".")
    assert exp.simple_sentence.endswith(".")


def test_sentence_group_ordering_trend_before_time() -> None:
    """The sentence orders trend before time even when bits are mixed."""
    exp = decode_combo(_mask("afternoon", "ema20_above_ema200"), "long", "5m")
    # 'trend is up' (trend group) precedes 'afternoon' (time group).
    assert exp.simple_sentence.index("trend is up") < exp.simple_sentence.index("afternoon")


@pytest.mark.parametrize("name", sorted(NAME_TO_BIT.keys()))
def test_every_condition_decodes_to_its_labels(name: str) -> None:
    """Each single-bit mask decodes to exactly that condition's two labels."""
    exp = decode_combo(_mask(name), "long", "5m")
    assert len(exp.conditions) == 1
    cond = exp.conditions[0]
    assert cond.technical_name == name
    assert cond.bit == NAME_TO_BIT[name]
    assert cond.pro_label == CONDITION_LABELS[name][0]
    assert cond.simple_label == CONDITION_LABELS[name][1]
