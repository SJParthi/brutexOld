"""Shared :class:`~brutex.metrics.compute.ResultMetrics` test builder.

One place to construct a fully-populated ``ResultMetrics`` whose defaults PASS
every hard rejection gate (override any field per test) — used by the bounded
columnar-rank suites (DEC-SWEEP-STREAMING-RANK-001) so they never import a
fixture from another test module (the anti-pattern this package exists to
avoid).
"""

from __future__ import annotations

from typing import Any

from brutex.metrics.compute import ResultMetrics


def make_result_metrics(**overrides: Any) -> ResultMetrics:
    """A ``ResultMetrics`` that PASSES every gate by default; override per test."""
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
        # Exit quality (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B): a
        # concrete SL-exit share consistent with the counts above (10 SL
        # exits of 50 trades — a subset of the 20 losers); passes the
        # default-inactive max_sl_hit_rate door. Override per test.
        "sl_hit_rate": 0.2,
        # Recency (DEC-METRICS-TRAILING-WINDOW-001): defaults None — the
        # honest anchor-less legacy arm. Override per test where the
        # trailing/decay values matter.
        "trailing_sharpe_ratio": None,
        "trailing_profit_factor": None,
        "trailing_net_pnl_paisa": None,
        "decay_weighted_net_paisa": None,
    }
    base.update(overrides)
    return ResultMetrics(**base)


__all__ = ["make_result_metrics"]
