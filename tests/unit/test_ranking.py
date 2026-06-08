"""Unit tests for the composite ranking + hard rejection gates (DEC-RANKING-001).

Every gate branch, every z-score edge (empty / zero-variance / clamp), the full
rank_combos path, and the risk-efficiency term (DEC-RANKING-RISK-EFFICIENCY-001)
are exercised for the strict 100% line+branch gate. The Stage-3 multiple-testing
overfit gate + Deflated-Sharpe ranking term (DEC-RANKING-OVERFIT-001) — the 6th
gate + 8th term — and the pure :mod:`brutex.ranking.overfit` engine are covered
here too (the strict ``--cov=brutex.ranking`` gate runs ONLY this file).
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from brutex.metrics.compute import ResultMetrics
from brutex.metrics.deflated_sharpe import GAUSSIAN_KURTOSIS, deflated_sharpe_ratio
from brutex.ranking import OverfitGateConfig, RankedCombo, passes_gates, rank_combos
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
