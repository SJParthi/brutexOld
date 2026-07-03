"""Golden byte-equality oracle for :mod:`brutex.metrics.lockbox` (#8) and
:mod:`brutex.metrics.monte_carlo` (#9).

Hand-fixed inputs + pinned seed lock the outputs bit-for-bit (Golden Rule
#4). Lockbox is pure (no RNG); Monte-Carlo is deterministic under its
mandatory seed. p-values are exact rationals (``count / (B + 1)``); the
Sharpe is a full-precision float asserted to a tight tolerance.
"""

from __future__ import annotations

import numpy as np
import pytest
from brutex.metrics.lockbox import LockboxStatus, lockbox_evaluation
from brutex.metrics.monte_carlo import monte_carlo_permutation_test

# Hand-fixed 8-trade series. Default window (4 days back from day 8 → days
# >= 5) holds exactly the 4 trades on days {5, 5, 6, 8}: returns
# [-0.15, 0.25, 0.10, 0.00] → cumulative equity never dips below its running
# peak ⇒ drawdown 0; Sharpe of those four ⇒ 0.342997...
_RETURNS = np.array([0.3, -0.1, 0.2, 0.05, -0.15, 0.25, 0.1, 0.0])
_DAYS = np.array([1, 1, 2, 3, 5, 5, 6, 8])


def test_golden_lockbox() -> None:
    r = lockbox_evaluation(
        _RETURNS,
        _DAYS,
        training_sharpe=0.2,
        training_max_drawdown=0.5,
        min_trades=4,
        default_window_days=4,
    )
    assert r.status is LockboxStatus.PASS
    assert r.passed is True
    assert r.n_trades == 4
    assert r.extended is False
    assert r.window_start_day == 5
    assert r.lockbox_sharpe == pytest.approx(0.34299717028501764, abs=1e-9, rel=1e-9)
    assert r.lockbox_max_drawdown == 0.0
    assert (r.count_gate_passed, r.sharpe_gate_passed, r.mdd_gate_passed) == (True, True, True)


def test_golden_lockbox_determinism() -> None:
    kw = {
        "training_sharpe": 0.2,
        "training_max_drawdown": 0.5,
        "min_trades": 4,
        "default_window_days": 4,
    }
    assert lockbox_evaluation(_RETURNS, _DAYS, **kw) == lockbox_evaluation(_RETURNS, _DAYS, **kw)  # type: ignore[arg-type]


def test_golden_lockbox_with_trade_evidence() -> None:
    # DEC-STATS-GATE-INTEGRITY-001 (R2-SR-F2): the SAME series gated by TRUE
    # trade evidence. Two trades (days 6, 8) in the 4-day default window
    # (days >= 5) sit below min_trades=3, so the window extends back to the
    # 3rd-most-recent TRADE's day (3): lockbox slice = returns on days >= 3
    # ([0.05, -0.15, 0.25, 0.1, 0.0]); n_trades counts the 3 TRADES in-window,
    # not the 5 observations. Sharpe over the 5-observation slice: mean 0.05,
    # population variance 0.017 ⇒ 0.05/sqrt(0.017) = 0.3834824944236852
    # (recomputed independently, no repo code); cumulative path dips 0.15
    # below its 0.05 peak ⇒ drawdown 0.15.
    r = lockbox_evaluation(
        _RETURNS,
        _DAYS,
        training_sharpe=0.2,
        training_max_drawdown=0.5,
        min_trades=3,
        default_window_days=4,
        evidence_days=np.array([3, 6, 8]),
    )
    assert r.status is LockboxStatus.PASS
    assert r.n_trades == 3
    assert r.extended is True
    assert r.window_start_day == 3
    assert r.lockbox_sharpe == pytest.approx(0.3834824944236852, abs=1e-12, rel=1e-12)
    assert r.lockbox_max_drawdown == pytest.approx(0.15, abs=1e-12)
    assert (r.count_gate_passed, r.sharpe_gate_passed, r.mdd_gate_passed) == (True, True, True)


def _mean_position(positions: np.ndarray) -> float:
    """Deterministic exit-logic oracle: the mean entry-bar position."""
    return float(positions.mean())


def test_golden_monte_carlo() -> None:
    res = monte_carlo_permutation_test(
        observed_statistic=100.0,
        n_bars=300,
        n_entries=20,
        evaluate=_mean_position,
        seed=20240603,
        n_permutations=2000,
    )
    # observed (100) sits well below the ~150 mean of 20 random positions in
    # [0, 300) ⇒ almost every permutation beats it ⇒ p ≈ 1.
    assert res.n_beaten == 1992
    assert res.pvalue == 1993 / 2001
    assert res.passed is False


def test_golden_monte_carlo_determinism() -> None:
    def evaluate(positions: np.ndarray) -> float:
        return float(positions.mean())

    a = monte_carlo_permutation_test(100.0, 300, 20, evaluate, seed=20240603, n_permutations=2000)
    b = monte_carlo_permutation_test(100.0, 300, 20, evaluate, seed=20240603, n_permutations=2000)
    assert a == b
