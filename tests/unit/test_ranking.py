"""Unit tests for the composite ranking + hard rejection gates (DEC-RANKING-001).

Every gate branch, every z-score edge (empty / zero-variance / clamp), the full
rank_combos path, and the risk-efficiency term (DEC-RANKING-RISK-EFFICIENCY-001)
are exercised for the strict 100% line+branch gate. The Stage-3 multiple-testing
overfit gate + Deflated-Sharpe ranking term (DEC-RANKING-OVERFIT-001) — the 6th
gate + 8th term — and the pure :mod:`brutex.ranking.overfit` engine are covered
here too (the strict ``--cov=brutex.ranking`` gate runs ONLY this file).
"""

# Whitebox: `_zscores` is imported directly to pin the z-normalisation
# arithmetic byte-exact for the mutation gate (DEC-MUTATION-GATE-RANKING-002).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import dataclasses
import math
from typing import Any

import pytest
from brutex.metrics.compute import ResultMetrics
from brutex.metrics.deflated_sharpe import GAUSSIAN_KURTOSIS, deflated_sharpe_ratio
from brutex.ranking import (
    GATE_CONSECUTIVE_LOSSES,
    STREAK_DOOR_WLR_EXEMPT,
    OverfitGateConfig,
    RankedCombo,
    _zscores,
    gate_reasons,
    passes_gates,
    rank_combos,
    streak_door_threshold,
)
from brutex.ranking.overfit import (
    PoolDispersion,
    deflated_sharpe_for,
    passes_overfit_gate,
    pool_sharpe_dispersion,
)


def _m(**overrides: Any) -> ResultMetrics:
    """A ResultMetrics that PASSES every gate by default; override per test."""
    base: dict[str, Any] = {
        "net_pnl_paisa": 0,
        "total_return_pct": 0.0,
        "cagr_pct": 0.0,
        "gross_profit_paisa": 0,
        "gross_loss_paisa": 0,
        "sharpe_ratio": 1.0,
        "sortino_ratio": 1.0,
        "calmar_ratio": 1.0,
        "mar_ratio": 0.0,
        "information_ratio": 0.0,
        "omega_ratio": 0.0,
        "max_drawdown_pct": 5.0,
        "max_drawdown_paisa": 0,
        "max_drawdown_duration_bars": 0,
        "recovery_factor": 1.0,
        "ulcer_index": 0.0,
        "largest_losing_trade_paisa": 0,
        "max_consecutive_losses": 2,
        "max_consecutive_wins": 0,
        "total_trades": 50,
        "winning_trades": 30,
        "losing_trades": 20,
        "win_rate": 0.6,
        "profit_factor": 1.5,
        "avg_winning_trade_paisa": 0,
        "avg_losing_trade_paisa": 0,
        "win_loss_ratio": 1.5,
        "expectancy_paisa": 0,
        "avg_mfe_paisa": 0,
        "avg_mae_paisa": 0,
        "avg_holding_time_bars": 0.0,
        "max_holding_time_bars": 0,
        "time_in_market_pct": 0.0,
        "tail_ratio": 0.0,
        "skewness": 0.0,
        "kurtosis": 0.0,
        "returns_std_dev": 0.0,
        # The loss/growth trio (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 /
        # DEC-METRICS-KELLY-GROWTH-001): loss_rate consistent with the
        # counts above (losing 20 / total 50); the Kelly pair defaults
        # None (undefined) — override per test where the value matters.
        "loss_rate": 0.4,
        "kelly_fraction": None,
        "log_growth_at_half_kelly": None,
    }
    base.update(overrides)
    return ResultMetrics(**base)


# ────── passes_gates ──────


class TestPassesGates:
    def test_default_passes(self) -> None:
        assert passes_gates(_m()) is True

    def test_reject_too_few_trades(self) -> None:
        assert passes_gates(_m(total_trades=29)) is False

    def test_accept_exactly_min_trades(self) -> None:
        # Boundary: total_trades == MIN_TRADES (30) is the lowest rankable
        # count and must PASS. Pins the threshold against an off-by-one
        # (MIN_TRADES 30 → 31) mutant that would silently reject valid combos
        # at exactly the gate (DEC-MUTATION-GATE-RANKING-001).
        assert passes_gates(_m(total_trades=30)) is True

    def test_reject_profit_factor_none(self) -> None:
        assert passes_gates(_m(profit_factor=None)) is False

    def test_reject_profit_factor_below_floor(self) -> None:
        assert passes_gates(_m(profit_factor=1.19)) is False

    def test_reject_drawdown_too_deep(self) -> None:
        assert passes_gates(_m(max_drawdown_pct=30.01)) is False

    def test_reject_low_winrate_and_low_wlr(self) -> None:
        # win_rate < 25% AND win_loss_ratio < 2.0 → reject.
        assert passes_gates(_m(win_rate=0.20, win_loss_ratio=1.9)) is False

    def test_accept_low_winrate_but_high_wlr(self) -> None:
        # low hit-rate is OK if winners are big enough (WLR >= 2.0).
        assert passes_gates(_m(win_rate=0.20, win_loss_ratio=2.0)) is True

    def test_low_winrate_with_none_wlr_rejected(self) -> None:
        # win_loss_ratio None → treated as 0.0 → low-and-low → reject.
        assert passes_gates(_m(win_rate=0.20, win_loss_ratio=None)) is False

    def test_reject_too_many_consecutive_losses(self) -> None:
        assert passes_gates(_m(max_consecutive_losses=11)) is False

    def test_accept_profit_factor_exactly_at_floor(self) -> None:
        # Boundary: profit_factor == 1.2 is the lowest rankable PF and must
        # PASS — pins the `<` against a `<=` flip that would silently reject
        # combos sitting exactly on the gate (DEC-MUTATION-GATE-RANKING-002).
        assert passes_gates(_m(profit_factor=1.2)) is True

    def test_accept_drawdown_exactly_at_cap(self) -> None:
        # Boundary: max_drawdown_pct == 30.0 exactly is still investable and
        # must PASS — pins the `>` against a `>=` flip.
        assert passes_gates(_m(max_drawdown_pct=30.0)) is True

    def test_accept_win_rate_exactly_at_floor_with_low_wlr(self) -> None:
        # Boundary: win_rate == 25% exactly is NOT "low hit-rate", so a low
        # win/loss-ratio alone must not reject — pins `<` against `<=`.
        assert passes_gates(_m(win_rate=0.25, win_loss_ratio=0.5)) is True

    def test_accept_exactly_max_consecutive_losses(self) -> None:
        # Boundary: max_consecutive_losses == 10 is the worst acceptable
        # streak and must PASS — pins the `<=` against a `<` flip. (Under the
        # derived door the fixture's threshold is still 10: k99(50, 0.4) = 9
        # floors at the legacy 10 — test_ranking_streaks pins the math.)
        assert passes_gates(_m(max_consecutive_losses=10)) is True


# ────── Door 5 — the derived binomial-null streak door (DEC-METRICS-KELLY-GROWTH-001) ──────


class TestStreakDoorDerived:
    """The Door-5 re-derivation: ``max(10, k99)`` + the WLR >= 3.0 exemption.

    The statistics itself (closed form vs the exact DP oracle) is pinned in
    ``tests/unit/test_ranking_streaks.py``; here the DOOR composition —
    threshold arms, the exemption union (a recorded lane choice), the
    degenerate all-loss arm, the fail-closed loss_rate guard, and the
    ONLY-LOOSENS law vs the legacy fixed-10 door.
    """

    def test_threshold_floors_at_legacy_ten(self) -> None:
        # q = 0 → k99 = 0; q small → k99 < 10: both floor at the legacy 10 —
        # the map's Interactions-cell justification (an unfloored door would
        # TIGHTEN for high-win-rate combos, flipping the safe direction).
        assert streak_door_threshold(50, 0.0) == 10
        assert streak_door_threshold(50, 0.05) == 10
        assert streak_door_threshold(50, 0.4) == 10  # k99 = 9, floored

    def test_threshold_derives_above_floor(self) -> None:
        # Hand-derived k99(30, 0.5) = 11 (audited in test_ranking_streaks).
        assert streak_door_threshold(30, 0.5) == 11

    def test_threshold_all_loss_degenerate_keeps_fixed_ten(self) -> None:
        # loss_rate >= 1.0 is the quantile's refusal domain — the door keeps
        # the FIXED legacy bound (a 100%-loss strategy must never ride its
        # "expected" n-run through the door).
        assert streak_door_threshold(200, 1.0) == 10
        assert streak_door_threshold(200, 1.5) == 10

    def test_loosened_streak_now_passes(self) -> None:
        # FAILS-BEFORE evidence for the sanctioned re-derivation: an 11-loss
        # streak over 30 trades at a 50% loss rate was REJECTED by the fixed
        # 10 and is CONSISTENT with the strategy's own null (threshold 11).
        m = _m(
            total_trades=30,
            winning_trades=15,
            losing_trades=15,
            loss_rate=0.5,
            max_consecutive_losses=11,
        )
        assert passes_gates(m) is True
        assert GATE_CONSECUTIVE_LOSSES not in gate_reasons(m)

    def test_beyond_derived_threshold_rejects(self) -> None:
        m = _m(
            total_trades=30,
            winning_trades=15,
            losing_trades=15,
            loss_rate=0.5,
            max_consecutive_losses=12,
        )
        assert passes_gates(m) is False
        assert GATE_CONSECUTIVE_LOSSES in gate_reasons(m)

    def test_high_payoff_exemption_at_exactly_three(self) -> None:
        # WLR == 3.0 exactly EXEMPTS (>= — the map-pinned bound), even a
        # 20-loss streak; 3.0 − ulp does not.
        assert passes_gates(_m(max_consecutive_losses=20, win_loss_ratio=3.0)) is True
        below = math.nextafter(STREAK_DOOR_WLR_EXEMPT, 0.0)
        assert passes_gates(_m(max_consecutive_losses=20, win_loss_ratio=below)) is False

    def test_none_wlr_never_exempts(self) -> None:
        # A None ratio never exempts (consistent with Door 4's None → 0.0
        # projection); win_rate 0.6 keeps Door 4 itself green, so the reject
        # is Door 5's alone.
        m = _m(max_consecutive_losses=20, win_loss_ratio=None)
        assert passes_gates(m) is False
        assert gate_reasons(m) == (GATE_CONSECUTIVE_LOSSES,)

    def test_all_loss_metrics_reject_past_fixed_ten(self) -> None:
        m = _m(loss_rate=1.0, max_consecutive_losses=11)
        assert passes_gates(m) is False
        m10 = _m(loss_rate=1.0, max_consecutive_losses=10)
        assert GATE_CONSECUTIVE_LOSSES not in gate_reasons(m10)

    def test_nan_loss_rate_fails_closed(self) -> None:
        # loss_rate rides the widened _junk_loss_rate guard arm: a NaN would
        # silently corrupt the derived threshold, so the guard rejects up
        # front.
        m = _m(loss_rate=math.nan)
        assert passes_gates(m) is False
        assert gate_reasons(m) == ("non_finite_input",)

    def test_inf_loss_rate_fails_closed(self) -> None:
        m = _m(loss_rate=math.inf)
        assert passes_gates(m) is False
        assert gate_reasons(m) == ("non_finite_input",)

    def test_type_illegal_none_loss_rate_fails_closed(self) -> None:
        # Refuter round 1 (totality): loss_rate is NON-nullable, so None is
        # junk — pre-fix both twins crashed with TypeError inside the derived
        # Door-5 threshold; now the SAME fail-closed guard exit as NaN/inf.
        m = _m(loss_rate=None)
        assert passes_gates(m) is False
        assert gate_reasons(m) == ("non_finite_input",)

    def test_only_loosens_vs_the_legacy_fixed_door(self) -> None:
        """The ONLY-LOOSENS law: every metrics row that passed the legacy
        fixed-10 Door 5 passes the derived door — swept over streaks 0..10 ×
        a loss-rate grid incl. both degenerate ends × None/low/high WLR
        (gate_reasons isolates Door 5 from the other doors)."""
        for streak in range(11):
            for loss_rate in (0.0, 0.1, 0.5, 0.9, 1.0):
                for wlr in (None, 0.5, 1.5, 3.5):
                    m = _m(
                        max_consecutive_losses=streak,
                        loss_rate=loss_rate,
                        win_loss_ratio=wlr,
                    )
                    assert GATE_CONSECUTIVE_LOSSES not in gate_reasons(m), (
                        streak,
                        loss_rate,
                        wlr,
                    )


# ────── passes_gates — fail-closed on non-finite inputs (DEC-RANKING-TIEBREAK-001) ──────


class TestNonFiniteGatesFailClosed:
    """A NaN / ±inf in a float-valued gate input must REJECT, not silently PASS.

    ``NaN < x`` and ``NaN > x`` are both ``False``, and ``inf``/``-inf`` skew the
    comparisons, so an unguarded ``<`` / ``>`` gate would wrongly ACCEPT malformed
    data. The fail-closed guard rejects any present non-finite gate input; behaviour
    for FINITE values and for ``None`` is byte-identical (pinned below).
    """

    def test_nan_profit_factor_rejected(self) -> None:
        # NaN would pass `pf < 1.2` (False) AND the `is None` check (False) → without
        # the guard this WRONGLY accepts. The guard must REJECT.
        assert passes_gates(_m(profit_factor=math.nan)) is False

    def test_positive_inf_max_drawdown_rejected(self) -> None:
        # +inf drawdown would pass `dd > 30.0`? No — +inf > 30 is True → already
        # rejected by the drawdown gate. So force the case the guard uniquely catches
        # by also proving -inf (which `dd > 30.0` does NOT catch) is rejected below.
        assert passes_gates(_m(max_drawdown_pct=math.inf)) is False

    def test_negative_inf_max_drawdown_rejected(self) -> None:
        # -inf drawdown PASSES `dd > 30.0` (-inf > 30 is False) → without the guard
        # this wrongly accepts. The guard must REJECT (the uniquely-caught case).
        assert passes_gates(_m(max_drawdown_pct=-math.inf)) is False

    def test_inf_win_rate_rejected(self) -> None:
        assert passes_gates(_m(win_rate=math.inf)) is False

    def test_negative_inf_win_rate_rejected(self) -> None:
        assert passes_gates(_m(win_rate=-math.inf)) is False

    def test_nan_win_loss_ratio_rejected(self) -> None:
        # NaN WLR would pass `wlr < 2.0` (False) → could wrongly accept a low-win-rate
        # combo. The guard rejects the NaN before that comparison.
        assert passes_gates(_m(win_rate=0.20, win_loss_ratio=math.nan)) is False

    def test_finite_values_unchanged(self) -> None:
        # Every finite default still PASSES — the guard never touches finite inputs.
        assert passes_gates(_m()) is True
        assert passes_gates(_m(profit_factor=1.2, max_drawdown_pct=30.0)) is True

    def test_none_values_unchanged(self) -> None:
        # None handling is byte-identical: None profit_factor still REJECTS via the
        # existing `is None` path (not the new non-finite guard); None win_loss_ratio
        # with a healthy win_rate still PASSES.
        assert passes_gates(_m(profit_factor=None)) is False
        assert passes_gates(_m(win_loss_ratio=None)) is True


# ────── rank_combos ──────


class TestRankCombos:
    def test_empty_candidates(self) -> None:
        assert rank_combos([]) == []

    def test_all_rejected_yields_empty(self) -> None:
        assert rank_combos([(1, _m(total_trades=5)), (2, _m(profit_factor=1.0))]) == []

    def test_single_survivor_scores_zero(self) -> None:
        # One survivor → zero variance on every term → composite 0.0.
        ranked = rank_combos([(7, _m(sharpe_ratio=3.0))])
        assert len(ranked) == 1
        assert ranked[0].mask == 7
        assert ranked[0].composite_score == 0.0

    def test_ranks_higher_sharpe_first(self) -> None:
        # Two survivors identical except Sharpe → the higher-Sharpe combo wins.
        cand = [
            (1, _m(sharpe_ratio=0.5)),
            (2, _m(sharpe_ratio=2.5)),
        ]
        ranked = rank_combos(cand)
        assert [rc.mask for rc in ranked] == [2, 1]
        assert ranked[0].composite_score > ranked[1].composite_score

    def test_rejected_combos_are_dropped(self) -> None:
        cand = [
            (1, _m(sharpe_ratio=2.0)),
            (2, _m(total_trades=10)),  # rejected
            (3, _m(sharpe_ratio=0.1)),
        ]
        ranked = rank_combos(cand)
        assert {rc.mask for rc in ranked} == {1, 3}

    def test_extreme_outlier_zscore_is_clamped(self) -> None:
        # A huge Sharpe outlier is clamped to z=+3 — it cannot dominate without bound.
        cand = [
            (1, _m(sharpe_ratio=0.0)),
            (2, _m(sharpe_ratio=0.0)),
            (3, _m(sharpe_ratio=0.0)),
            (4, _m(sharpe_ratio=0.0)),
            (5, _m(sharpe_ratio=1_000_000.0)),
        ]
        ranked = rank_combos(cand)
        top = ranked[0]
        assert top.mask == 5
        # composite is bounded by 0.25 (Sharpe weight) × 3 (z-clamp) for that term.
        assert top.composite_score <= 0.25 * 3.0 + 1e-9

    def test_none_metric_terms_treated_as_zero(self) -> None:
        # A survivor whose composite terms are None must still rank (terms → 0).
        cand = [
            (1, _m(sortino_ratio=None, calmar_ratio=None, recovery_factor=None)),
            (2, _m(sharpe_ratio=2.0)),
        ]
        ranked = rank_combos(cand)
        assert len(ranked) == 2

    def test_max_sl_hit_rate_knob_gates_the_survivor_pool(self) -> None:
        """Door 6 (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B): an active
        ceiling drops the over-ceiling AND the unmeasured (``None``) rows;
        the default knob keeps every row (byte-identical pre-door)."""
        cand = [
            (1, _m(sl_hit_rate=0.2)),
            (2, _m(sl_hit_rate=0.8)),
            (3, _m(sl_hit_rate=None)),
        ]
        assert {rc.mask for rc in rank_combos(cand)} == {1, 2, 3}
        assert {rc.mask for rc in rank_combos(cand, max_sl_hit_rate=0.5)} == {1}


# ────── tie-break determinism (DEC-RANKING-TIEBREAK-001) ──────


class TestTieBreakDeterminism:
    """The ranking is a TOTAL order: ties break on the unique mask, so a leaderboard
    ranks bit-identically regardless of input enumeration order (Golden Rules #4/#7).

    The pre-fix sort keyed on ``composite_score`` alone left tied scores in INPUT
    order (``list.sort`` is stable), so permuting the candidates permuted the output
    for any tie. These cases pin the deterministic ``(-composite_score, mask)`` key.
    """

    def test_identical_metrics_tie_break_ascending_mask(self) -> None:
        # Three combos with byte-identical metrics → identical composite (0.0, zero
        # variance). They MUST come out in ascending-mask order, not input order.
        cand = [(30, _m(sharpe_ratio=2.0)), (10, _m(sharpe_ratio=2.0)), (20, _m(sharpe_ratio=2.0))]
        ranked = rank_combos(cand)
        assert [rc.mask for rc in ranked] == [10, 20, 30]

    def test_tied_scores_are_input_order_independent(self) -> None:
        # The same leaderboard in a DIFFERENT enumeration order yields the SAME
        # ranking (the determinism the stable-sort-only version violated).
        base = [(10, _m(sharpe_ratio=2.0)), (20, _m(sharpe_ratio=2.0)), (30, _m(sharpe_ratio=2.0))]
        forward = [rc.mask for rc in rank_combos(base)]
        reverse = [rc.mask for rc in rank_combos(list(reversed(base)))]
        assert forward == reverse == [10, 20, 30]

    def test_score_dominates_mask(self) -> None:
        # The mask tie-break is STRICTLY secondary: a higher composite always wins
        # even when its mask is larger. mask=99 (high Sharpe) must beat mask=1.
        cand = [(99, _m(sharpe_ratio=3.0)), (1, _m(sharpe_ratio=0.1))]
        ranked = rank_combos(cand)
        assert [rc.mask for rc in ranked] == [99, 1]
        assert ranked[0].composite_score > ranked[1].composite_score

    def test_partial_tie_groups_break_on_mask(self) -> None:
        # Two score groups: the high-Sharpe pair ties at one composite, the
        # low-Sharpe pair ties at another. Within each tied group the order is
        # mask-ascending; the high group precedes the low group.
        cand = [
            (40, _m(sharpe_ratio=3.0)),
            (10, _m(sharpe_ratio=3.0)),
            (30, _m(sharpe_ratio=0.5)),
            (20, _m(sharpe_ratio=0.5)),
        ]
        ranked = rank_combos(cand)
        assert [rc.mask for rc in ranked] == [10, 40, 20, 30]
        # The two distinct score tiers really are distinct (not a single tie).
        assert ranked[0].composite_score == ranked[1].composite_score
        assert ranked[2].composite_score == ranked[3].composite_score
        assert ranked[1].composite_score > ranked[2].composite_score

    def test_overfit_path_tie_break_ascending_mask(self) -> None:
        # The tie-break also governs the opt-in overfit path: survivors with an
        # identical DSR-inclusive composite still break on ascending mask.
        cfg = OverfitGateConfig(dsr_min=0.5)
        cand = [
            (30, _m(sharpe_ratio=2.0, total_trades=100)),
            (10, _m(sharpe_ratio=2.0, total_trades=100)),
            (20, _m(sharpe_ratio=2.0, total_trades=100)),
        ]
        ranked = rank_combos(cand, overfit=cfg)
        # All three share metrics ⇒ zero cross-trial variance ⇒ ABSTAIN, legacy
        # 7-term composite (all 0.0) ⇒ pure mask tie-break.
        assert [rc.mask for rc in ranked] == [10, 20, 30]

    def test_duplicate_survivor_mask_raises(self) -> None:
        # The total-order tie-break keys on the mask, so a DUPLICATE mask among
        # gate-passing survivors would silently reintroduce input-order-dependent
        # ordering. rank_combos must FAIL LOUD (ValueError) instead — pins the
        # enforced unique-mask precondition (DEC-RANKING-TIEBREAK-001).
        cand = [(5, _m(sharpe_ratio=2.0)), (5, _m(sharpe_ratio=1.0))]
        with pytest.raises(ValueError, match="unique mask") as exc:
            rank_combos(cand)
        # Byte-exact message naming the offending mask (mutation-gate discipline:
        # a substring `match=` still passes mutmut's "XX…XX"-wrapped string mutant).
        assert str(exc.value) == (
            "rank_combos requires a unique mask per candidate (the deterministic "
            "tie-break keys on it); duplicate survivor mask(s): [5]"
        )

    def test_duplicate_message_names_only_the_duplicated_mask(self) -> None:
        # Three survivors: mask 5 twice (the genuine duplicate) plus a UNIQUE
        # bystander mask 7. The error must list ONLY the duplicated mask —
        # ``[5]`` — never the unique survivor. Kills the chronic
        # ``count > 1`` → ``count >= 1`` dupes-filter survivor (mutmut mutant
        # 190 of the ranking/DSR math pass, DEC-MUTATION-SURVIVOR-KILL-001):
        # that mutant reports EVERY survivor mask (``[5, 7]``), accusing a
        # non-duplicated mask — invisible to the existing duplicate test whose
        # survivors all share one mask (``[5]`` either way).
        cand = [
            (5, _m(sharpe_ratio=2.0)),
            (5, _m(sharpe_ratio=1.0)),
            (7, _m(sharpe_ratio=1.5)),
        ]
        with pytest.raises(ValueError) as exc:
            rank_combos(cand)
        assert str(exc.value) == (
            "rank_combos requires a unique mask per candidate (the deterministic "
            "tie-break keys on it); duplicate survivor mask(s): [5]"
        )

    def test_duplicate_among_rejected_does_not_raise(self) -> None:
        # The uniqueness check is over the gate SURVIVORS only — a duplicate mask
        # among gate-REJECTED combos never reaches the sort and must NOT raise.
        cand = [
            (5, _m(total_trades=10)),  # rejected (too few trades)
            (5, _m(profit_factor=1.0)),  # rejected (no edge) — same mask, but rejected
            (7, _m(sharpe_ratio=2.0)),  # the sole survivor
        ]
        ranked = rank_combos(cand)
        assert [rc.mask for rc in ranked] == [7]

    def test_unique_mask_path_unaffected(self) -> None:
        # The normal unique-mask path is byte-unchanged by the precondition check.
        cand = [(3, _m(sharpe_ratio=2.0)), (1, _m(sharpe_ratio=2.0)), (2, _m(sharpe_ratio=2.0))]
        ranked = rank_combos(cand)
        assert [rc.mask for rc in ranked] == [1, 2, 3]


# ────── _zscores — the z-normalisation engine behind every composite term ──────


class TestZScores:
    """Pin the z-score arithmetic + the [-3, +3] clamp byte-exact.

    Every input is chosen so each intermediate (mean / variance / std / z) is
    an exact binary float, so the assertions are ``==``, never approx — a
    mutated mean/variance/std/clamp cannot hide inside float noise
    (DEC-MUTATION-GATE-RANKING-002).
    """

    def test_symmetric_pair_is_plus_minus_one(self) -> None:
        # mean 1.5, variance 1.0, std 1.0 → z exactly ±1.
        assert _zscores([2.5, 0.5]) == [1.0, -1.0]

    def test_quarter_variance_pair_is_plus_minus_one(self) -> None:
        # values 1.5/0.5: mean 1.0, variance 0.25, std 0.5 → z exactly ±1.
        # Variance ≠ 1.0 on purpose: it kills the std arithmetic mutants
        # (variance*0.5, variance**1.5, /n → *n) that are INVISIBLE at unit
        # variance, where x**0.5 == x*... degenerates to the same value.
        assert _zscores([1.5, 0.5]) == [1.0, -1.0]

    def test_clamps_positive_outlier_at_exactly_three(self) -> None:
        # 17 values: mean 1.0, variance 16.0, std 4.0 — all exact — so the
        # outlier's raw z is exactly +4.0 and must clamp to exactly +3.0
        # (pins _Z_CLAMP == 3.0 against the 4.0 mutant); the 16 others sit
        # at exactly -0.25, far from either clamp.
        assert _zscores([0.0] * 16 + [17.0]) == [-0.25] * 16 + [3.0]

    def test_clamps_negative_outlier_at_exactly_minus_three(self) -> None:
        # Mirror case: raw z exactly -4.0 → clamped to exactly -3.0 (pins the
        # lower clamp arm `max(-_Z_CLAMP, ...)`).
        assert _zscores([0.0] * 16 + [-17.0]) == [0.25] * 16 + [-3.0]


# ────── composite weight pins (DEC-MUTATION-GATE-RANKING-002) ──────


class TestCompositeWeightPins:
    """Each composite weight is pinned byte-exact through the public API.

    Two survivors differ in exactly ONE metric, chosen so that term's
    z-scores are exactly ±1.0 and every other term has zero variance
    (z = 0). The composite is then exactly ±weight — killing the
    weight-constant (+1.0) mutants and the `+` → `-` sign flips in the
    composite sum, which pure ordering assertions cannot distinguish.
    """

    def _pin(self, hi: dict[str, Any], lo: dict[str, Any], weight: float) -> None:
        # The hi-override combo must rank first with composite exactly
        # +weight, the lo-override combo last with exactly -weight.
        ranked = rank_combos([(1, _m(**hi)), (2, _m(**lo))])
        assert [rc.mask for rc in ranked] == [1, 2]
        assert ranked[0].composite_score == weight
        assert ranked[1].composite_score == -weight

    def test_sharpe_weight_exactly_0_25(self) -> None:
        self._pin({"sharpe_ratio": 2.5}, {"sharpe_ratio": 0.5}, 0.25)

    def test_sortino_weight_exactly_0_20(self) -> None:
        self._pin({"sortino_ratio": 3.0}, {"sortino_ratio": 1.0}, 0.20)

    def test_calmar_weight_exactly_0_15(self) -> None:
        self._pin({"calmar_ratio": 3.0}, {"calmar_ratio": 1.0}, 0.15)

    def test_profit_factor_weight_exactly_0_15(self) -> None:
        # PF 2.5/1.5 → (PF - 1.0) values 1.5/0.5: term variance 0.25 ≠ 1.0,
        # which also kills the std mutants invisible at unit variance.
        self._pin({"profit_factor": 2.5}, {"profit_factor": 1.5}, 0.15)

    def test_recovery_weight_exactly_0_10(self) -> None:
        self._pin({"recovery_factor": 3.0}, {"recovery_factor": 1.0}, 0.10)

    def test_win_wlr_weight_exactly_0_10(self) -> None:
        # win_rate fixed at 0.5 so the win_rate × WLR products are exactly
        # 2.0 vs 1.0; a win_rate/WLR quotient mutant gives 0.125 vs 0.25 and
        # FLIPS the ordering, so the mask assertion kills it too.
        self._pin(
            {"win_rate": 0.5, "win_loss_ratio": 4.0},
            {"win_rate": 0.5, "win_loss_ratio": 2.0},
            0.10,
        )

    def test_risk_efficiency_weight_exactly_0_15(self) -> None:
        # MFE/MAE ratios exactly 3.0 vs 1.0 (int paisa → exact floats).
        self._pin(
            {"avg_mfe_paisa": 300, "avg_mae_paisa": 100},
            {"avg_mfe_paisa": 100, "avg_mae_paisa": 100},
            0.15,
        )

    def test_none_term_contributes_exactly_zero(self) -> None:
        # A None sortino must contribute exactly like 0.0 — the {None, 0.0}
        # sortino pair has zero variance, so ONLY the Sharpe term scores: a
        # `_f(None) → 1.0` fallback mutant would add a spurious ±0.20 and
        # break the exact ±0.25.
        ranked = rank_combos(
            [
                (1, _m(sortino_ratio=None, sharpe_ratio=2.5)),
                (2, _m(sortino_ratio=0.0, sharpe_ratio=0.5)),
            ]
        )
        assert [rc.mask for rc in ranked] == [1, 2]
        assert ranked[0].composite_score == 0.25
        assert ranked[1].composite_score == -0.25


# ────── RankedCombo dataclass discipline ──────


class TestRankedComboDataclass:
    def _rc(self) -> RankedCombo:
        return RankedCombo(mask=1, metrics=_m(), composite_score=0.0)

    def test_frozen(self) -> None:
        # frozen=True — a leaderboard row is immutable once scored.
        with pytest.raises(dataclasses.FrozenInstanceError):
            self._rc().mask = 2  # type: ignore[misc]

    def test_slots(self) -> None:
        # slots=True — a slotted instance carries no per-instance __dict__.
        assert not hasattr(self._rc(), "__dict__")
        assert hasattr(RankedCombo, "__slots__")

    def test_deflated_sharpe_defaults_to_none(self) -> None:
        # Legacy construction defaults the DSR field to None (NOT "" or any
        # sentinel) — pins the default whose line-scoped `pragma: no mutate`
        # (equivalent annotation flip) hides it from mutmut.
        assert self._rc().deflated_sharpe is None

    def test_dsr_n_scope_none_without_deflated_sharpe(self) -> None:
        # DEC-STATS-GATE-INTEGRITY-001 (R2-SR-F3): no DSR (legacy / abstain /
        # Stage-3 refined-row construction) ⇒ no scope stamp.
        assert self._rc().dsr_n_scope is None
        stage3_style = RankedCombo(mask=1, metrics=_m(), composite_score=0.0, deflated_sharpe=None)
        assert stage3_style.dsr_n_scope is None

    def test_dsr_n_scope_tuple_with_deflated_sharpe(self) -> None:
        # A present DSR is ALWAYS the tuple-scoped figure (rank_combos is the
        # only src site that sets it non-None) — the property derives the
        # stamp from the value, so the two can never drift.
        rc = RankedCombo(mask=1, metrics=_m(), composite_score=0.0, deflated_sharpe=0.97)
        assert rc.dsr_n_scope == "tuple"


# ────── risk-efficiency term (DEC-RANKING-RISK-EFFICIENCY-001) ──────


class TestRiskEfficiency:
    """The "tight SL + massive profit" ranking term: avg MFE / avg MAE."""

    def _re(self, **overrides: Any) -> float:
        # Exercise _risk_efficiency through the public RankedCombo property.
        return RankedCombo(mask=0, metrics=_m(**overrides), composite_score=0.0).risk_efficiency

    def test_ratio_is_mfe_over_mae(self) -> None:
        # avg MFE 900 / avg MAE 300 = 3.0 (ran 3x as far for us as against us).
        assert self._re(avg_mfe_paisa=900, avg_mae_paisa=300) == 3.0

    def test_zero_mae_floors_to_one_paisa_finite(self) -> None:
        # A never-adverse combo (avg MAE 0) → MFE / 1 paisa: large but FINITE,
        # so the z-score never sees inf and the leaderboard stays well-defined.
        assert self._re(avg_mfe_paisa=500, avg_mae_paisa=0) == 500.0

    def test_drives_order_all_else_equal(self) -> None:
        # Identical combos except excursion: the tighter-stop / bigger-run one
        # wins purely on the risk-efficiency term (every other term ties to 0).
        cand = [
            (1, _m(avg_mfe_paisa=1_000, avg_mae_paisa=100)),  # ratio 10
            (2, _m(avg_mfe_paisa=100, avg_mae_paisa=100)),  # ratio 1
        ]
        ranked = rank_combos(cand)
        assert [rc.mask for rc in ranked] == [1, 2]
        assert ranked[0].composite_score > ranked[1].composite_score


# ────── overfit gate config (DEC-RANKING-OVERFIT-001) ──────


class TestOverfitGateConfig:
    def test_defaults(self) -> None:
        cfg = OverfitGateConfig()
        assert cfg.dsr_min == 0.95
        assert cfg.weight == 0.05
        assert cfg.default_skew == 0.0
        assert cfg.default_excess_kurtosis == 0.0

    def test_dsr_min_boundaries_ok(self) -> None:
        assert OverfitGateConfig(dsr_min=0.0).dsr_min == 0.0
        assert OverfitGateConfig(dsr_min=1.0).dsr_min == 1.0

    def test_dsr_min_below_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="dsr_min"):
            OverfitGateConfig(dsr_min=-0.01)

    def test_dsr_min_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="dsr_min"):
            OverfitGateConfig(dsr_min=1.01)

    def test_weight_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="weight"):
            OverfitGateConfig(weight=-0.1)

    def test_default_skew_non_finite_raises(self) -> None:
        with pytest.raises(ValueError, match="default_skew"):
            OverfitGateConfig(default_skew=math.inf)

    def test_default_excess_kurtosis_non_finite_raises(self) -> None:
        with pytest.raises(ValueError, match="default_excess_kurtosis"):
            OverfitGateConfig(default_excess_kurtosis=math.nan)

    def test_weight_zero_boundary_ok(self) -> None:
        # Boundary: weight == 0.0 is a valid no-op DSR weight and must NOT
        # raise — pins `<` against a `<=` flip (DEC-MUTATION-GATE-RANKING-002).
        assert OverfitGateConfig(weight=0.0).weight == 0.0

    def test_dsr_min_error_message_byte_exact(self) -> None:
        # Byte-exact str(exc): a substring `match=` still passes mutmut's
        # "XX…XX"-wrapped string mutant (same discipline as
        # test_deflated_sharpe.py — DEC-MUTATION-GATE-RANKING-001).
        with pytest.raises(ValueError) as exc:
            OverfitGateConfig(dsr_min=1.5)
        assert str(exc.value) == (
            "OverfitGateConfig.dsr_min must be a probability in [0, 1], got 1.5"
        )

    def test_weight_error_message_byte_exact(self) -> None:
        with pytest.raises(ValueError) as exc:
            OverfitGateConfig(weight=-0.5)
        assert str(exc.value) == "OverfitGateConfig.weight must be >= 0, got -0.5"

    def test_skew_error_message_byte_exact(self) -> None:
        with pytest.raises(ValueError) as exc:
            OverfitGateConfig(default_skew=math.inf)
        assert str(exc.value) == "OverfitGateConfig.default_skew must be finite, got inf"

    def test_kurtosis_error_message_byte_exact(self) -> None:
        with pytest.raises(ValueError) as exc:
            OverfitGateConfig(default_excess_kurtosis=math.nan)
        assert str(exc.value) == (
            "OverfitGateConfig.default_excess_kurtosis must be finite, got nan"
        )

    def test_frozen(self) -> None:
        # frozen=True — a gate config is immutable once constructed.
        with pytest.raises(dataclasses.FrozenInstanceError):
            OverfitGateConfig().dsr_min = 0.5  # type: ignore[misc]

    def test_slots(self) -> None:
        # slots=True — no per-instance __dict__.
        assert not hasattr(OverfitGateConfig(), "__dict__")
        assert hasattr(OverfitGateConfig, "__slots__")


# ────── pool dispersion ──────


class TestPoolSharpeDispersion:
    def test_empty_pool(self) -> None:
        disp = pool_sharpe_dispersion([])
        assert disp == PoolDispersion(variance=0.0, n_finite=0)
        assert disp.deflatable is False

    def test_single_finite_not_deflatable(self) -> None:
        disp = pool_sharpe_dispersion([1.0, None])
        assert disp.n_finite == 1
        assert disp.variance == 0.0
        assert disp.deflatable is False

    def test_none_and_non_finite_excluded(self) -> None:
        disp = pool_sharpe_dispersion([1.0, None, math.inf, math.nan, 3.0])
        assert disp.n_finite == 2  # only 1.0 and 3.0 are finite
        assert disp.variance == 1.0  # population variance of {1, 3}

    def test_zero_variance_not_deflatable(self) -> None:
        disp = pool_sharpe_dispersion([2.0, 2.0])
        assert disp.variance == 0.0
        assert disp.n_finite == 2
        assert disp.deflatable is False  # >= 2 finite but no dispersion

    def test_positive_variance_deflatable(self) -> None:
        disp = pool_sharpe_dispersion([1.0, 3.0])
        assert disp.variance == 1.0
        assert disp.deflatable is True

    def test_frozen(self) -> None:
        # frozen=True — a measured pool dispersion is immutable.
        disp = PoolDispersion(variance=0.0, n_finite=0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            disp.variance = 1.0  # type: ignore[misc]

    def test_slots(self) -> None:
        # slots=True — no per-instance __dict__.
        disp = PoolDispersion(variance=0.0, n_finite=0)
        assert not hasattr(disp, "__dict__")
        assert hasattr(PoolDispersion, "__slots__")


# ────── per-combo deflation ──────


class TestDeflatedSharpeFor:
    _CFG = OverfitGateConfig()

    def test_none_sharpe_returns_none(self) -> None:
        assert (
            deflated_sharpe_for(
                None, 50, 0.0, 0.0, n_trials=5, trial_sharpe_variance=1.0, cfg=self._CFG
            )
            is None
        )

    def test_non_finite_sharpe_returns_none(self) -> None:
        assert (
            deflated_sharpe_for(
                math.inf, 50, 0.0, 0.0, n_trials=5, trial_sharpe_variance=1.0, cfg=self._CFG
            )
            is None
        )

    def test_sample_length_below_two_returns_none(self) -> None:
        # total_trades=1 → PSR sample_length < 2 → ValueError → None (unreachable
        # from rank_combos since survivors have >= MIN_TRADES, but guarded here).
        assert (
            deflated_sharpe_for(
                1.0, 1, 0.0, 0.0, n_trials=5, trial_sharpe_variance=1.0, cfg=self._CFG
            )
            is None
        )

    def test_radicand_out_of_domain_returns_none(self) -> None:
        # An extreme skew drives the PSR estimator-variance radicand <= 0 → None.
        assert (
            deflated_sharpe_for(
                2.0, 50, 10.0, 0.0, n_trials=5, trial_sharpe_variance=1.0, cfg=self._CFG
            )
            is None
        )

    def test_happy_path_is_a_probability(self) -> None:
        dsr = deflated_sharpe_for(
            2.0, 50, 0.0, 0.0, n_trials=5, trial_sharpe_variance=0.5, cfg=self._CFG
        )
        assert dsr is not None
        assert 0.0 <= dsr <= 1.0

    def test_skew_none_uses_config_default(self) -> None:
        cfg = OverfitGateConfig(default_skew=0.3)
        with_none = deflated_sharpe_for(
            1.5, 60, None, 0.0, n_trials=5, trial_sharpe_variance=0.5, cfg=cfg
        )
        explicit = deflated_sharpe_for(
            1.5, 60, 0.3, 0.0, n_trials=5, trial_sharpe_variance=0.5, cfg=cfg
        )
        assert with_none == explicit

    def test_kurtosis_none_uses_config_default(self) -> None:
        cfg = OverfitGateConfig(default_excess_kurtosis=1.2)
        with_none = deflated_sharpe_for(
            1.5, 60, 0.0, None, n_trials=5, trial_sharpe_variance=0.5, cfg=cfg
        )
        explicit = deflated_sharpe_for(
            1.5, 60, 0.0, 1.2, n_trials=5, trial_sharpe_variance=0.5, cfg=cfg
        )
        assert with_none == explicit

    def test_excess_to_non_excess_kurtosis_conversion(self) -> None:
        # deflated_sharpe_for takes EXCESS kurtosis and adds GAUSSIAN_KURTOSIS (=3)
        # before the engine, which expects NON-excess kurtosis. Pin the +3.
        got = deflated_sharpe_for(
            1.8, 80, 0.1, 2.0, n_trials=6, trial_sharpe_variance=0.4, cfg=self._CFG
        )
        ref = deflated_sharpe_ratio(
            1.8,
            n_trials=6,
            trial_sharpe_variance=0.4,
            sample_length=80,
            skew=0.1,
            kurtosis=2.0 + GAUSSIAN_KURTOSIS,
        ).deflated_sharpe_ratio
        assert got == ref
        # And the Gaussian default (excess 0.0) maps to non-excess 3.0.
        got0 = deflated_sharpe_for(
            1.8, 80, 0.1, 0.0, n_trials=6, trial_sharpe_variance=0.4, cfg=self._CFG
        )
        ref0 = deflated_sharpe_ratio(
            1.8,
            n_trials=6,
            trial_sharpe_variance=0.4,
            sample_length=80,
            skew=0.1,
            kurtosis=GAUSSIAN_KURTOSIS,
        ).deflated_sharpe_ratio
        assert got0 == ref0


# ────── overfit gate predicate ──────


class TestPassesOverfitGate:
    _CFG = OverfitGateConfig(dsr_min=0.95)

    def test_none_fails(self) -> None:
        assert passes_overfit_gate(None, self._CFG) is False

    def test_below_min_fails(self) -> None:
        assert passes_overfit_gate(0.94, self._CFG) is False

    def test_at_boundary_passes(self) -> None:
        assert passes_overfit_gate(0.95, self._CFG) is True

    def test_above_passes(self) -> None:
        assert passes_overfit_gate(0.99, self._CFG) is True


# ────── rank_combos with the opt-in overfit gate + term ──────


def _sharpe_pool(masks_sharpes: list[tuple[int, float]]) -> list[tuple[int, ResultMetrics]]:
    """Build a candidate pool varying ONLY the Sharpe (all pass the 5 single gates)."""
    return [(mask, _m(sharpe_ratio=sr)) for mask, sr in masks_sharpes]


class TestRankCombosOverfit:
    _CFG = OverfitGateConfig(dsr_min=0.95)

    def test_legacy_default_byte_identical(self) -> None:
        # overfit=None must keep the legacy 7-term ranking and leave deflated_sharpe None.
        cand = _sharpe_pool([(1, 0.5), (2, 2.5)])
        legacy = rank_combos(cand)
        assert [rc.deflated_sharpe for rc in legacy] == [None, None]
        # Re-running with overfit=None is identical to the positional legacy call.
        again = rank_combos(cand, overfit=None)
        assert [rc.mask for rc in again] == [rc.mask for rc in legacy]
        assert [rc.composite_score for rc in again] == [rc.composite_score for rc in legacy]

    def test_overfit_empty_candidates(self) -> None:
        assert rank_combos([], overfit=self._CFG) == []

    def test_overfit_all_fail_single_gates(self) -> None:
        # No survivor clears the 5 single gates → [] before the overfit gate runs.
        cand = [(1, _m(total_trades=5)), (2, _m(profit_factor=1.0))]
        assert rank_combos(cand, overfit=self._CFG) == []

    def test_abstain_zero_variance_pool(self) -> None:
        # All candidate Sharpes equal → pool not deflatable → ABSTAIN: survivors
        # pass through with the legacy 7-term score and deflated_sharpe None.
        cand = _sharpe_pool([(1, 1.0), (2, 1.0)])
        ranked = rank_combos(cand, overfit=self._CFG)
        legacy = rank_combos(cand)
        assert {rc.mask for rc in ranked} == {1, 2}
        assert all(rc.deflated_sharpe is None for rc in ranked)
        # Abstain ⇒ identical scores to the legacy 7-term ranking.
        by_mask = {rc.mask: rc.composite_score for rc in legacy}
        assert all(rc.composite_score == by_mask[rc.mask] for rc in ranked)

    def test_abstain_fewer_than_two_finite_sharpes(self) -> None:
        # Survivors with None Sharpe → < 2 finite → not deflatable → abstain (kept).
        cand = [(1, _m(sharpe_ratio=None)), (2, _m(sharpe_ratio=None))]
        ranked = rank_combos(cand, overfit=self._CFG)
        assert {rc.mask for rc in ranked} == {1, 2}
        assert all(rc.deflated_sharpe is None for rc in ranked)

    def test_low_dsr_survivors_rejected(self) -> None:
        # P7 pool (N=7, V≈0.92): low-Sharpe combos are deflated below 0.95 and
        # dropped; only Sharpe >= 2.0 survive the multiple-testing gate.
        cand = _sharpe_pool([(1, 0.2), (2, 0.5), (3, 1.0), (4, 1.5), (5, 2.0), (6, 2.5), (7, 3.0)])
        ranked = rank_combos(cand, overfit=self._CFG)
        assert {rc.mask for rc in ranked} == {5, 6, 7}
        # Survivors carry their Deflated Sharpe and it cleared the bar.
        assert all(rc.deflated_sharpe is not None and rc.deflated_sharpe >= 0.95 for rc in ranked)

    def test_dsr_n_scope_tuple_on_every_deflated_survivor(self) -> None:
        # R2-SR-F3 end-to-end: a deflatable overfit run stamps EVERY ranked
        # combo "tuple" (deflated_sharpe non-None ⇒ scope tuple); an abstain
        # run (zero-variance pool) stamps None on every combo.
        deflated = rank_combos(
            _sharpe_pool([(1, 0.2), (2, 1.0), (3, 2.0), (4, 2.5), (5, 3.0)]), overfit=self._CFG
        )
        assert deflated  # fixture sanity: survivors exist
        assert all(rc.dsr_n_scope == "tuple" for rc in deflated)
        abstained = rank_combos(_sharpe_pool([(1, 1.0), (2, 1.0)]), overfit=self._CFG)
        assert abstained
        assert all(rc.dsr_n_scope is None for rc in abstained)

    def test_all_survivors_rejected_yields_empty(self) -> None:
        # Two low-Sharpe survivors deflated against a high-variance pool (padded by
        # gate-failing high-Sharpe candidates) → both rejected → [].
        cand = [
            (1, _m(sharpe_ratio=0.5)),
            (2, _m(sharpe_ratio=1.0)),
            (3, _m(sharpe_ratio=4.0, profit_factor=1.0)),  # fails PF gate (not a survivor)
            (4, _m(sharpe_ratio=5.0, profit_factor=1.0)),  # fails PF gate (not a survivor)
        ]
        assert rank_combos(cand, overfit=self._CFG) == []

    def test_none_sharpe_survivor_dropped_when_deflatable(self) -> None:
        # A deflatable pool with one None-Sharpe survivor: the None one is dropped
        # (undeflatable → fails the gate), the real-Sharpe ones survive.
        cfg = OverfitGateConfig(dsr_min=0.5)
        cand = [
            (1, _m(sharpe_ratio=2.0)),
            (2, _m(sharpe_ratio=3.0)),
            (3, _m(sharpe_ratio=None)),
        ]
        ranked = rank_combos(cand, overfit=cfg)
        assert {rc.mask for rc in ranked} == {1, 2}

    def test_dsr_term_orders_by_skew(self) -> None:
        # Two survivors identical on every base term (same Sharpe etc.) but with
        # different SKEWNESS — which feeds the DSR but NONE of the 7 base terms. So
        # ordering is driven purely by the 8th (Deflated-Sharpe) term: the
        # higher-skew (higher-DSR) combo wins. Pool padded for positive variance.
        cfg = OverfitGateConfig(dsr_min=0.5)
        cand = [
            (1, _m(sharpe_ratio=2.0, skewness=-0.5, total_trades=100)),
            (2, _m(sharpe_ratio=2.0, skewness=0.5, total_trades=100)),
            (3, _m(sharpe_ratio=0.2, profit_factor=1.0)),  # non-survivor (variance)
            (4, _m(sharpe_ratio=3.5, profit_factor=1.0)),  # non-survivor (variance)
        ]
        ranked = rank_combos(cand, overfit=cfg)
        assert [rc.mask for rc in ranked] == [2, 1]
        top, bottom = ranked
        assert top.deflated_sharpe is not None and bottom.deflated_sharpe is not None
        assert top.deflated_sharpe > bottom.deflated_sharpe
        assert top.composite_score > bottom.composite_score

    def test_single_deflatable_survivor_scores_zero_dsr_term(self) -> None:
        # One survivor passes the gate; the lone-survivor DSR z-term is 0 (no
        # dispersion), so the composite is the single-survivor 0.0 — but the
        # deflated_sharpe is still recorded.
        cand = [
            (1, _m(sharpe_ratio=3.0)),
            (2, _m(sharpe_ratio=0.2)),  # survivor but deflated below 0.95 → dropped
            (3, _m(sharpe_ratio=0.3)),  # survivor but dropped
        ]
        ranked = rank_combos(cand, overfit=self._CFG)
        assert len(ranked) == 1
        assert ranked[0].mask == 1
        assert ranked[0].composite_score == 0.0
        assert ranked[0].deflated_sharpe is not None


# ────── shared rank internals (DEC-SWEEP-STREAMING-RANK-001) ──────
#
# The bounded columnar rank (brutex.engine.sweep_stream) reuses these public
# internals; the direct pins below keep them honest INDEPENDENTLY of the two
# rank paths (whose bit-identity is property-gated in
# tests/property/test_sweep_stream_bounded_rank.py).


class TestSharedRankInternals:
    def test_composite_term_inputs_order_and_projection(self) -> None:
        from brutex.ranking import composite_term_inputs

        m = _m(
            sharpe_ratio=1.5,
            sortino_ratio=None,
            calmar_ratio=0.25,
            profit_factor=2.0,
            recovery_factor=None,
            win_rate=0.4,
            win_loss_ratio=3.0,
            avg_mfe_paisa=600,
            avg_mae_paisa=0,  # floored at 1 paisa by _risk_efficiency
        )
        assert composite_term_inputs(m) == (1.5, 0.0, 0.25, 1.0, 0.0, 0.4 * 3.0, 600.0)

    def test_composite_scores_matches_manual_weighted_sum(self) -> None:
        from brutex.ranking import N_COMPOSITE_TERMS, composite_scores

        columns = tuple([float(i), float(i) * 2.0] for i in range(N_COMPOSITE_TERMS))
        dsr_contrib = [0.25, -0.25]
        scores = composite_scores(columns, dsr_contrib)
        weights = (0.25, 0.20, 0.15, 0.15, 0.10, 0.10, 0.15)
        for i in range(2):
            expected = 0.0
            for column, weight in zip(columns, weights, strict=True):
                expected += weight * _zscores(column)[i]
            expected += dsr_contrib[i]
            assert scores[i] == expected

    def test_composite_scores_wrong_column_count_rejected(self) -> None:
        from brutex.ranking import composite_scores

        with pytest.raises(ValueError, match=r"^composite_scores expects 7 term columns, got 2$"):
            composite_scores(([0.0], [0.0]), [0.0])

    def test_dsr_z_contributions_inactive_is_all_zero(self) -> None:
        from brutex.ranking import dsr_z_contributions

        assert dsr_z_contributions(None, 3, None) == [0.0, 0.0, 0.0]

    def test_dsr_z_contributions_scales_zscores_by_weight(self) -> None:
        from brutex.ranking import dsr_z_contributions

        cfg = OverfitGateConfig(weight=0.5)
        dsrs = [0.1, 0.9]
        expected = [0.5 * z for z in _zscores(dsrs)]
        assert dsr_z_contributions(dsrs, 2, cfg) == expected

    def test_ensure_unique_survivor_masks_accepts_unique(self) -> None:
        from brutex.ranking import ensure_unique_survivor_masks

        ensure_unique_survivor_masks([3, 1, 2])  # no raise

    def test_ensure_unique_survivor_masks_names_only_duplicates(self) -> None:
        from brutex.ranking import ensure_unique_survivor_masks

        with pytest.raises(ValueError) as excinfo:
            ensure_unique_survivor_masks([5, 5, 7])
        assert str(excinfo.value) == (
            "rank_combos requires a unique mask per candidate (the deterministic "
            "tie-break keys on it); duplicate survivor mask(s): [5]"
        )

    def test_overfit_gate_positions_abstains_on_undeflatable_pool(self) -> None:
        from brutex.ranking import overfit_gate_positions

        cfg = OverfitGateConfig()
        # A single finite Sharpe → dispersion not deflatable → abstain (None).
        assert overfit_gate_positions([1.0, None], [(1.0, 50, 0.0, 0.0)], cfg) is None

    def test_overfit_gate_positions_returns_positions_and_dsrs(self) -> None:
        from brutex.ranking import overfit_gate_positions

        cfg = OverfitGateConfig(dsr_min=0.5)
        pool: list[float | None] = [2.0, 0.1, 3.5, 0.2]
        survivors = [
            (2.0, 100, 0.5, 0.0),  # strong — passes
            (0.1, 100, 0.0, 0.0),  # weak — deflated below 0.5
        ]
        outcome = overfit_gate_positions(pool, survivors, cfg)
        assert outcome is not None
        positions, dsrs = outcome
        assert positions == [0]
        assert len(dsrs) == 1
        expected = deflated_sharpe_for(
            2.0,
            100,
            0.5,
            0.0,
            n_trials=4,
            trial_sharpe_variance=pool_sharpe_dispersion(pool).variance,
            cfg=cfg,
        )
        assert dsrs[0] == expected


class TestRustDispatchSeam:
    """The ONE flag-gated Rust dispatch seam in ``rank_combos`` (slice 4 PR-1).

    These tests live in THIS file deliberately: the weekly Python mutation
    math pass (DEC-MUTATION-GATE-RANKING-001, ``.github/workflows/mutation.yml``)
    mutates ``brutex/ranking/__init__.py`` with exactly this file (+
    ``test_deflated_sharpe.py``) as its runner, so every mutant on the
    dispatch lines must be killable HERE — e.g. the ``is not None`` →
    ``is None`` flip on the Rust-result check, which under
    ``test_enabled_dispatch_returns_the_rust_result`` skips the ``return``
    and falls through to the Python pipeline, failing the identity
    assertion. The seam functions are monkeypatched on
    :mod:`brutex.ranking.rust_ranking` (the in-function lazy import resolves
    module attributes at call time), so the tests are WHEEL-INDEPENDENT and
    never touch the env flag, burn-in state, or the compiled kernels.
    """

    def test_enabled_dispatch_returns_the_rust_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.ranking import rust_ranking

        sentinel = [RankedCombo(mask=0b1, metrics=_m(), composite_score=1.25, deflated_sharpe=None)]
        seen: list[tuple[Any, Any]] = []

        def fake_rank(
            candidates: Any, *, overfit: OverfitGateConfig | None = None
        ) -> list[RankedCombo]:
            seen.append((candidates, overfit))
            return sentinel

        monkeypatch.setattr(rust_ranking, "ranking_rust_enabled", lambda: True)
        monkeypatch.setattr(rust_ranking, "rank_combos_via_rust", fake_rank)
        pool = [(0b1, _m())]
        cfg = OverfitGateConfig()
        # IDENTITY, not equality: a mutant that skips the `return rust_ranked`
        # arm runs the Python pipeline instead and cannot produce this object.
        assert rank_combos(pool, overfit=cfg) is sentinel
        # The seam threads the caller's exact candidates + overfit config.
        assert seen == [(pool, cfg)]

    def test_enabled_wheel_missing_none_falls_back_to_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.ranking import rust_ranking

        pool = [(0b01, _m(sharpe_ratio=1.0)), (0b10, _m(sharpe_ratio=3.0))]
        monkeypatch.delenv("BRUTEX_RANKING_RUST", raising=False)
        expected = rank_combos(pool)
        calls: list[int] = []

        def fake_rank(candidates: Any, *, overfit: OverfitGateConfig | None = None) -> None:
            calls.append(1)

        monkeypatch.setattr(rust_ranking, "ranking_rust_enabled", lambda: True)
        monkeypatch.setattr(rust_ranking, "rank_combos_via_rust", fake_rank)
        got = rank_combos(pool)
        assert calls == [1]
        assert got == expected

    def test_disabled_never_consults_the_rust_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.ranking import rust_ranking

        monkeypatch.delenv("BRUTEX_RANKING_RUST", raising=False)

        def _boom(candidates: Any, *, overfit: OverfitGateConfig | None = None) -> None:
            raise AssertionError("rank_combos_via_rust must not run with the flag OFF")

        monkeypatch.setattr(rust_ranking, "rank_combos_via_rust", _boom)
        ranked = rank_combos([(0b1, _m())])
        assert [rc.mask for rc in ranked] == [0b1]
