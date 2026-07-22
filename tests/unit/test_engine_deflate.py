"""Unit tests for the DSR-over-sweep multiple-testing penalty (DEC-DSR-SWEEP-001).

Covers ``deflate_sweep``: empty results, all-undefined-Sharpe, a valid leaderboard
(the DSR / PSR / SR_0 invariants), the per-combo skip guards (Sharpe / skew /
kurtosis ``None``, ``< 2`` trades), and the invalid-moment-radicand omit (never a
crash) — for the strict 100% ``brutex.engine`` package gate.
"""

from __future__ import annotations

from typing import Any

from brutex.engine.deflate import deflate_sweep
from brutex.metrics.compute import ResultMetrics
from brutex.metrics.deflated_sharpe import DeflatedSharpeResult


def _m(**overrides: Any) -> ResultMetrics:
    """A ResultMetrics whose moments form a VALID DSR combo by default."""
    base: dict[str, Any] = {
        "net_pnl_paisa": 0,
        "total_return_pct": 0.0,
        "cagr_pct": 0.0,
        "gross_profit_paisa": 0,
        "gross_loss_paisa": 0,
        "sharpe_ratio": 1.5,
        "sortino_ratio": 1.0,
        "calmar_ratio": 1.0,
        "mar_ratio": 0.0,
        "information_ratio": 0.0,
        "omega_ratio": 0.0,
        "max_drawdown_pct": 10.0,
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
        "kurtosis": 0.0,  # EXCESS kurtosis → g4 = 3 (Gaussian) after the baseline add
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


class TestDeflateSweep:
    def test_empty_results(self) -> None:
        assert deflate_sweep([]) == {}

    def test_all_undefined_sharpe_yields_empty(self) -> None:
        # No defined trial Sharpe → variance falls back to 0.0 AND every combo is
        # skipped → empty (exercises the `if trial_sharpes else 0.0` fallback).
        assert deflate_sweep([(1, _m(sharpe_ratio=None)), (2, _m(sharpe_ratio=None))]) == {}

    def test_valid_leaderboard_deflated(self) -> None:
        results = [(1, _m(sharpe_ratio=1.5)), (2, _m(sharpe_ratio=0.5))]
        out = deflate_sweep(results)
        assert set(out) == {1, 2}
        top = out[1]
        assert isinstance(top, DeflatedSharpeResult)
        assert top.n_trials == 2  # N = number of trials searched
        assert 0.0 <= top.deflated_sharpe_ratio <= 1.0
        assert top.expected_max_sharpe >= 0.0
        # The DSR can never exceed the un-deflated PSR (SR_0 >= 0).
        assert top.deflated_sharpe_ratio <= top.probabilistic_sharpe_ratio

    def test_fewer_than_two_trades_skipped(self) -> None:
        assert deflate_sweep([(1, _m(sharpe_ratio=1.5, total_trades=1))]) == {}

    def test_none_skewness_skipped(self) -> None:
        assert deflate_sweep([(1, _m(sharpe_ratio=1.5, skewness=None))]) == {}

    def test_none_kurtosis_skipped(self) -> None:
        assert deflate_sweep([(1, _m(sharpe_ratio=1.5, kurtosis=None))]) == {}

    def test_invalid_radicand_omitted_not_crash(self) -> None:
        # combo 2's radicand 1 - 1*2 + (1.5-1)/4*2² = -0.5 <= 0 → DSR raises
        # internally → omitted; the valid combo 1 is still returned (no crash).
        results = [
            (1, _m(sharpe_ratio=1.5)),
            (2, _m(sharpe_ratio=2.0, skewness=1.0, kurtosis=-1.5, total_trades=30)),
        ]
        assert set(deflate_sweep(results)) == {1}
