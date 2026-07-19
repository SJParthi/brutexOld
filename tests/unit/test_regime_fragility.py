"""Unit tests for the regime-fragility law leaf (DEC-REGIME-FRAGILITY-GATE-001).

Every law branch boundary-exact: the 80% concentration bar with
exact-fraction fixtures (79.9% / 80.0% / 80.1% of the summed positive P&L),
the era_break sign arms, the insufficient_strata abstain, the UNKNOWN-stratum
exclusion, the MIN_STRATUM_TRADES support boundary (29/30), and the
degenerate/total-function arms (empty series, absent axes, misaligned axes).
"""

from __future__ import annotations

from brutex.metrics.regime_fragility import (
    MIN_STRATUM_TRADES,
    REGIME_CONCENTRATION_MAX,
    TAXONOMY_VERSION,
    FragilityVerdict,
    evaluate_fragility,
)
from brutex.metrics.regime_taxonomy import TAXONOMY_VERSION as LEAF_TAXONOMY_VERSION


def _stratum(total: int, n: int = MIN_STRATUM_TRADES) -> tuple[int, ...]:
    """*n* all-positive pnls summing EXACTLY to *total* (exact-fraction fixture)."""
    head = total - (n - 1)
    assert head >= 1, "fixture: total too small for all-positive pnls"
    return (head,) + (1,) * (n - 1)


def _two_stratum_trend(up_total: int, down_total: int) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """Two supported trend strata with exact positive-P&L totals."""
    up = _stratum(up_total)
    down = _stratum(down_total)
    pnls = up + down
    tokens = ("up",) * len(up) + ("down",) * len(down)
    return pnls, tokens


def _axis_strata(verdict: FragilityVerdict, axis: str) -> dict[str, object]:
    axes = verdict.stats["axes"]
    assert isinstance(axes, dict)
    axis_stats = axes[axis]
    assert isinstance(axis_stats, dict)
    strata = axis_stats["strata"]
    assert isinstance(strata, dict)
    return strata


# ────── constants + re-export laws ──────


def test_taxonomy_version_reexported_from_the_leaf() -> None:
    assert TAXONOMY_VERSION == LEAF_TAXONOMY_VERSION


def test_concentration_constant_matches_exact_rational() -> None:
    # The law compares with integer cross-multiplication at 4/5 — the float
    # constant must be EXACTLY that rational or the two would drift.
    assert REGIME_CONCENTRATION_MAX == 4 / 5


def test_stats_fold_the_taxonomy_version() -> None:
    v = evaluate_fragility((), trend_tokens=None, era_labels=None)
    assert v.stats["taxonomy_version"] == TAXONOMY_VERSION


# ────── LAW regime_concentration (boundary-exact) ──────


def test_concentration_801_of_1000_fails() -> None:
    pnls, tokens = _two_stratum_trend(801, 199)
    v = evaluate_fragility(pnls, trend_tokens=tokens, era_labels=None)
    assert v.status == "fail"
    assert v.reasons == ("regime_concentration:trend:up",)
    assert "regime_concentration:trend:up" in v.detail


def test_concentration_exactly_800_of_1000_passes() -> None:
    # Strict ``>`` — EXACTLY 80.0% of the positive P&L still passes.
    pnls, tokens = _two_stratum_trend(800, 200)
    v = evaluate_fragility(pnls, trend_tokens=tokens, era_labels=None)
    assert v.status == "ok"
    assert v.reasons == ()


def test_concentration_799_of_1000_passes() -> None:
    pnls, tokens = _two_stratum_trend(799, 201)
    v = evaluate_fragility(pnls, trend_tokens=tokens, era_labels=None)
    assert v.status == "ok"
    assert v.reasons == ()


def test_concentration_needs_two_supported_strata() -> None:
    # 100%-concentrated "up" beside a 29-trade "down": only ONE supported
    # stratum → the axis is not judgeable → abstain, never a reject.
    up = _stratum(1000)
    down = _stratum(100, n=MIN_STRATUM_TRADES - 1)
    pnls = up + down
    tokens = ("up",) * len(up) + ("down",) * len(down)
    v = evaluate_fragility(pnls, trend_tokens=tokens, era_labels=None)
    assert v.status == "insufficient_strata"
    assert v.reasons == ()


def test_support_boundary_30_makes_the_axis_judgeable() -> None:
    # The SAME shape with the second stratum at exactly MIN_STRATUM_TRADES
    # flips the axis judgeable — and the concentrated stratum now fails.
    up = _stratum(1000)
    down = _stratum(100, n=MIN_STRATUM_TRADES)
    pnls = up + down
    tokens = ("up",) * len(up) + ("down",) * len(down)
    v = evaluate_fragility(pnls, trend_tokens=tokens, era_labels=None)
    assert v.status == "fail"
    assert v.reasons == ("regime_concentration:trend:up",)


def test_concentration_abstains_on_zero_positive_pnl() -> None:
    # All-losing candidate: total positive P&L is 0 — no concentration
    # denominator, no reason; the axis IS judgeable → "ok".
    pnls = (-10,) * MIN_STRATUM_TRADES + (-20,) * MIN_STRATUM_TRADES
    tokens = ("up",) * MIN_STRATUM_TRADES + ("down",) * MIN_STRATUM_TRADES
    v = evaluate_fragility(pnls, trend_tokens=tokens, era_labels=None)
    assert v.status == "ok"
    strata = _axis_strata(v, "trend")
    up_stats = strata["up"]
    assert isinstance(up_stats, dict)
    assert up_stats["positive_pnl_share"] is None  # no denominator


def test_concentration_fires_on_the_era_axis_too() -> None:
    era_a = _stratum(900)
    era_b = _stratum(100)
    pnls = era_a + era_b
    labels = ("era_a",) * len(era_a) + ("era_b",) * len(era_b)
    v = evaluate_fragility(pnls, trend_tokens=None, era_labels=labels)
    assert v.status == "fail"
    assert "regime_concentration:era:era_a" in v.reasons


# ────── LAW era_break ──────


def test_era_break_opposite_sign_expectancy_fails() -> None:
    # era_a: all +10 (mean > 0, pos 300); era_b: 10×(+10) + 20×(-20)
    # (mean < 0, pos 100) — shares 75%/25%, so ONLY era_break fires.
    era_a = (10,) * MIN_STRATUM_TRADES
    era_b = (10,) * 10 + (-20,) * 20
    pnls = era_a + era_b
    labels = ("era_a",) * len(era_a) + ("era_b",) * len(era_b)
    v = evaluate_fragility(pnls, trend_tokens=None, era_labels=labels)
    assert v.status == "fail"
    assert v.reasons == ("era_break",)


def test_era_break_both_positive_is_ok() -> None:
    pnls = (10,) * MIN_STRATUM_TRADES + (10,) * MIN_STRATUM_TRADES
    labels = ("era_a",) * MIN_STRATUM_TRADES + ("era_b",) * MIN_STRATUM_TRADES
    v = evaluate_fragility(pnls, trend_tokens=None, era_labels=labels)
    assert v.status == "ok"
    assert v.reasons == ()


def test_era_break_zero_expectancy_is_neither_sign() -> None:
    # era_b nets EXACTLY zero (mean == 0) — zero is neither > 0 nor < 0,
    # so no break; shares are 300 vs 150 (66.7%/33.3%) — no concentration.
    era_a = (10,) * MIN_STRATUM_TRADES
    era_b = (10, -10) * (MIN_STRATUM_TRADES // 2)
    pnls = era_a + era_b
    labels = ("era_a",) * len(era_a) + ("era_b",) * len(era_b)
    v = evaluate_fragility(pnls, trend_tokens=None, era_labels=labels)
    assert v.status == "ok"
    assert v.reasons == ()


def test_era_break_needs_supported_strata() -> None:
    # The negative era stratum holds only 29 trades — unsupported, so the
    # sign flip is not evidence; the axis has ONE supported stratum →
    # abstain.
    era_a = (10,) * MIN_STRATUM_TRADES
    era_b = (-10,) * (MIN_STRATUM_TRADES - 1)
    pnls = era_a + era_b
    labels = ("era_a",) * len(era_a) + ("era_b",) * len(era_b)
    v = evaluate_fragility(pnls, trend_tokens=None, era_labels=labels)
    assert v.status == "insufficient_strata"
    assert v.reasons == ()


def test_era_break_never_fires_on_the_trend_axis() -> None:
    # Opposite-SIGN trend expectancies: era_break is an ERA-axis-only law.
    # The losing stratum still carries positive prints so neither stratum
    # holds > 80% of the summed positive P&L (up 300 vs down 150 — 66.7%),
    # keeping the concentration law quiet: the sign flip ALONE decides,
    # and on the TREND axis it must decide NOTHING.
    pnls = (10,) * MIN_STRATUM_TRADES + (-20, 10) * (MIN_STRATUM_TRADES // 2)
    tokens = ("up",) * MIN_STRATUM_TRADES + ("down",) * MIN_STRATUM_TRADES
    v = evaluate_fragility(pnls, trend_tokens=tokens, era_labels=None)
    assert v.status == "ok"
    assert v.reasons == ()


# ────── UNKNOWN-stratum exclusion (trend axis) ──────


def test_unknown_stratum_excluded_from_concentration_denominator() -> None:
    # "unknown" holds 900 of the raw positive P&L, but it is EXCLUDED from
    # the law: eligible shares are up 60 / down 40 → no concentration.
    unk = _stratum(900)
    up = _stratum(60)
    down = _stratum(40)
    pnls = unk + up + down
    tokens = ("unknown",) * len(unk) + ("up",) * len(up) + ("down",) * len(down)
    v = evaluate_fragility(pnls, trend_tokens=tokens, era_labels=None)
    assert v.status == "ok"
    assert v.reasons == ()
    strata = _axis_strata(v, "trend")
    unk_stats = strata["unknown"]
    assert isinstance(unk_stats, dict)
    assert unk_stats["trades"] == len(unk)  # counted in stats
    assert unk_stats["positive_pnl_share"] is None  # excluded from the law


def test_unknown_stratum_never_counts_toward_support() -> None:
    # "unknown" (30) + "up" (30): ONE eligible supported stratum → abstain.
    pnls = _stratum(500) + _stratum(500)
    tokens = ("unknown",) * MIN_STRATUM_TRADES + ("up",) * MIN_STRATUM_TRADES
    v = evaluate_fragility(pnls, trend_tokens=tokens, era_labels=None)
    assert v.status == "insufficient_strata"


def test_junk_trend_token_degrades_to_unknown() -> None:
    # An unrecognized lens token folds into the UNKNOWN bucket (the
    # taxonomy's degrade law) — visible in stats, excluded from laws.
    pnls = (10, 20)
    tokens = ("zigzag", "sideways")
    v = evaluate_fragility(pnls, trend_tokens=tokens, era_labels=None)
    strata = _axis_strata(v, "trend")
    assert set(strata) == {"unknown", "chop"}
    assert v.status == "insufficient_strata"


# ────── degenerate / total-function arms ──────


def test_empty_pnls_is_insufficient_strata() -> None:
    v = evaluate_fragility((), trend_tokens=(), era_labels=())
    assert v.status == "insufficient_strata"
    assert v.reasons == ()


def test_both_axes_absent_is_insufficient_strata() -> None:
    v = evaluate_fragility((1, 2, 3), trend_tokens=None, era_labels=None)
    assert v.status == "insufficient_strata"
    axes = v.stats["axes"]
    assert isinstance(axes, dict)
    assert axes["trend"] == {"skipped": True, "reason": "no_input"}
    assert axes["era"] == {"skipped": True, "reason": "no_input"}


def test_misaligned_axis_is_skipped_never_raises() -> None:
    # A length-mismatched axis is SKIPPED (total function); the other axis
    # still judges — here the era axis fails on concentration.
    era_a = _stratum(900)
    era_b = _stratum(100)
    pnls = era_a + era_b
    labels = ("era_a",) * len(era_a) + ("era_b",) * len(era_b)
    v = evaluate_fragility(pnls, trend_tokens=("up",), era_labels=labels)
    axes = v.stats["axes"]
    assert isinstance(axes, dict)
    assert axes["trend"] == {"skipped": True, "reason": "length_mismatch"}
    assert v.status == "fail"
    assert v.reasons == ("regime_concentration:era:era_a",)


def test_stats_carry_trades_expectancy_and_share() -> None:
    pnls, tokens = _two_stratum_trend(801, 199)
    v = evaluate_fragility(pnls, trend_tokens=tokens, era_labels=None)
    strata = _axis_strata(v, "trend")
    up_stats = strata["up"]
    assert isinstance(up_stats, dict)
    assert up_stats["trades"] == MIN_STRATUM_TRADES
    assert up_stats["expectancy_paisa"] == 801 / MIN_STRATUM_TRADES
    assert up_stats["positive_pnl_share"] == 801 / 1000


def test_determinism() -> None:
    pnls, tokens = _two_stratum_trend(801, 199)
    labels = ("era_a",) * len(pnls)
    a = evaluate_fragility(pnls, trend_tokens=tokens, era_labels=labels)
    b = evaluate_fragility(pnls, trend_tokens=tokens, era_labels=labels)
    assert a == b
