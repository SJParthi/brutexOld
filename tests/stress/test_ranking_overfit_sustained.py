"""Stress test for the Stage-3 overfit gate (DEC-RANKING-OVERFIT-001).

Ranks a LARGE leaderboard (tens of thousands of combos) through the opt-in overfit
gate to prove the gate stays correct + deterministic at scale: no crash on a wide
varied pool, the survivor set is a subset of the legacy gate-survivors, every
survivor carries a valid Deflated Sharpe that cleared the bar, and the leaderboard
is byte-stable across repeated runs (Golden Rule #4 / #7).
"""

from __future__ import annotations

from typing import Any

from brutex.metrics.compute import ResultMetrics
from brutex.ranking import OverfitGateConfig, rank_combos

_POOL_SIZE = 30_000


def _m(**overrides: Any) -> ResultMetrics:
    """A gate-passing ResultMetrics; override per scenario."""
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


def _large_pool() -> list[tuple[int, ResultMetrics]]:
    # A deterministic, tightly-clustered Sharpe spread [1.0, 2.0) with long tracks
    # (500 trades) so the pool is firmly deflatable AND, against N=30,000 trials,
    # the multiple-testing bar splits it into a real partial survivor set: the
    # weak-Sharpe tail is deflated out, the strong cluster survives.
    return [
        (i, _m(sharpe_ratio=1.0 + (i % 100) / 100.0, total_trades=500)) for i in range(_POOL_SIZE)
    ]


def test_large_overfit_sweep_is_correct_and_deterministic() -> None:
    cand = _large_pool()
    cfg = OverfitGateConfig(dsr_min=0.95)

    legacy = {rc.mask for rc in rank_combos(cand)}
    ranked = rank_combos(cand, overfit=cfg)
    gated = {rc.mask for rc in ranked}

    # The gate only shrinks the leaderboard, and it DID reject some combos
    # (the low-Sharpe tail) — the multiple-testing correction is doing work.
    assert gated <= legacy
    assert 0 < len(gated) < len(legacy)

    # Every survivor carries a valid Deflated Sharpe that cleared the bar.
    for rc in ranked:
        assert rc.deflated_sharpe is not None
        assert 0.95 <= rc.deflated_sharpe <= 1.0

    # Sorted descending by composite, and byte-stable across a re-run.
    scores = [rc.composite_score for rc in ranked]
    assert scores == sorted(scores, reverse=True)
    rerun = rank_combos(cand, overfit=cfg)
    assert [rc.mask for rc in rerun] == [rc.mask for rc in ranked]
    assert [rc.composite_score for rc in rerun] == scores
