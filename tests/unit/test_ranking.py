"""Unit tests for the composite ranking + hard rejection gates (DEC-RANKING-001).

Every gate branch, every z-score edge (empty / zero-variance / clamp), and the
full rank_combos path are exercised for the strict 100% line+branch gate.
"""

from __future__ import annotations

from typing import Any

from brutex.metrics.compute import ResultMetrics
from brutex.ranking import passes_gates, rank_combos


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
