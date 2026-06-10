"""Unit tests for :mod:`brutex.metrics.lockbox` — Stage-4 filter #8.

Targets 100% line + branch coverage. Exercises the three statuses
(PASS / FAIL / INSUFFICIENT_N), the default-vs-extended window branches,
every gate combination, the validation guards, and the private
``_max_drawdown`` edge cases.
"""
# Whitebox: imports the private ``_max_drawdown`` to cover its empty-array
# branch (unreachable via the public API). Silence the pyright friend-access
# warning file-wide (DEC-P0-017 whitebox-test convention).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import numpy as np
import pytest
from brutex.metrics.lockbox import (
    _DEFAULT_WINDOW_DAYS,
    LOCKBOX_DEFAULT_WINDOW_DAYS,
    LockboxResult,
    LockboxStatus,
    _max_drawdown,
    lockbox_evaluation,
)


def test_public_window_constant_is_the_default_window() -> None:
    # DEC-STAGE3-WIRE-001 (review F1): the Stage-3 train-slice helper imports
    # THIS constant — pin it to the private default and the spec's ~6 months
    # so the shared boundary can never silently drift.
    assert LOCKBOX_DEFAULT_WINDOW_DAYS == _DEFAULT_WINDOW_DAYS
    assert LOCKBOX_DEFAULT_WINDOW_DAYS == 182
    # And the evaluation's default window IS this constant: the window start
    # lands exactly at max_day - LOCKBOX_DEFAULT_WINDOW_DAYS + 1.
    days = np.arange(1000, 1000 + 400)
    r = lockbox_evaluation(
        np.full(400, 0.01),
        days,
        training_sharpe=0.0,
        training_max_drawdown=0.0,
        min_trades=1,
    )
    assert r.window_start_day == int(days[-1]) - LOCKBOX_DEFAULT_WINDOW_DAYS + 1


def test_pass_default_window() -> None:
    rets = np.array([0.10, 0.20, 0.15, 0.05, 0.12])
    days = np.array([10, 11, 12, 13, 14])
    r = lockbox_evaluation(
        rets,
        days,
        training_sharpe=0.1,
        training_max_drawdown=10.0,
        min_trades=5,
        default_window_days=5,
    )
    assert isinstance(r, LockboxResult)
    assert r.status is LockboxStatus.PASS
    assert r.passed is True
    assert r.n_trades == 5
    assert r.extended is False
    assert r.count_gate_passed and r.sharpe_gate_passed and r.mdd_gate_passed
    assert r.lockbox_sharpe is not None and r.lockbox_sharpe > 0


def test_fail_on_sharpe_gate() -> None:
    # Mean ~ 0 → low lockbox Sharpe; high training Sharpe → floor not met.
    rets = np.array([0.1, -0.1, 0.1, -0.1, 0.1, -0.1])
    days = np.arange(6)
    r = lockbox_evaluation(
        rets,
        days,
        training_sharpe=2.0,
        training_max_drawdown=100.0,
        min_trades=6,
        default_window_days=10,
    )
    assert r.status is LockboxStatus.FAIL
    assert r.passed is False
    assert r.count_gate_passed is True
    assert r.sharpe_gate_passed is False
    assert r.mdd_gate_passed is True  # generous training MDD


def test_fail_on_mdd_gate() -> None:
    # Positive trend (Sharpe gate passes) but a dip creates MDD > 0; training
    # MDD of 0 makes the ceiling 0 → MDD gate fails.
    rets = np.array([0.1, 0.1, -0.05, 0.1, 0.1])
    days = np.arange(5)
    r = lockbox_evaluation(
        rets,
        days,
        training_sharpe=0.05,
        training_max_drawdown=0.0,
        min_trades=5,
        default_window_days=10,
    )
    assert r.status is LockboxStatus.FAIL
    assert r.sharpe_gate_passed is True
    assert r.mdd_gate_passed is False
    assert r.lockbox_max_drawdown > 0.0


def test_insufficient_n() -> None:
    rets = np.array([0.1, 0.2, 0.1, 0.05, 0.1])  # only 5 trades
    days = np.arange(5)
    r = lockbox_evaluation(
        rets, days, training_sharpe=0.1, training_max_drawdown=10.0, min_trades=500
    )
    assert r.status is LockboxStatus.INSUFFICIENT_N
    assert r.passed is False
    assert r.count_gate_passed is False
    assert r.extended is True  # tried to extend across all history
    assert r.n_trades == 5


def test_extended_window() -> None:
    # Default 2-day window holds only 2 trades (< min 4); extend back to reach 4.
    rets = np.array([0.1, 0.2, 0.15, 0.1, 0.12])
    days = np.array([1, 1, 1, 5, 6])  # last 2 days (5,6) hold only 2 trades
    r = lockbox_evaluation(
        rets,
        days,
        training_sharpe=0.1,
        training_max_drawdown=10.0,
        min_trades=4,
        default_window_days=2,
    )
    assert r.extended is True
    assert r.n_trades >= 4
    assert r.window_start_day == 1  # extended back to the earliest needed day
    assert r.count_gate_passed is True


def test_sharpe_none_single_trade() -> None:
    # A 1-trade window → Sharpe undefined (None) → Sharpe gate fails.
    r = lockbox_evaluation(
        np.array([0.1]),
        np.array([7]),
        training_sharpe=0.0,
        training_max_drawdown=10.0,
        min_trades=1,
        default_window_days=1,
    )
    assert r.lockbox_sharpe is None
    assert r.sharpe_gate_passed is False
    assert r.status is LockboxStatus.FAIL
    assert r.count_gate_passed is True


@pytest.mark.parametrize(
    ("returns", "days", "match"),
    [
        (np.zeros((2, 2)), np.array([1, 2]), "must both be 1-D"),
        (np.array([0.1, 0.2]), np.zeros((2, 1)), "must both be 1-D"),
        (np.array([0.1, 0.2]), np.array([1]), "equal length"),
        (np.array([]), np.array([]), "non-empty"),
        (np.array([0.1, np.nan]), np.array([1, 2]), "must be finite"),
        (np.array([0.1, np.inf]), np.array([1, 2]), "must be finite"),
        (np.array([0.1, 0.2]), np.array([5, 1]), "non-decreasing"),
    ],
)
def test_input_validation(returns: np.ndarray, days: np.ndarray, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        lockbox_evaluation(returns, days, training_sharpe=0.1, training_max_drawdown=1.0)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"default_window_days": 0}, "default_window_days must be >= 1"),
        ({"min_trades": 0}, "min_trades must be >= 1"),
        ({"sharpe_tolerance": -0.1}, "sharpe_tolerance must be >= 0"),
        ({"mdd_tolerance": -0.1}, "mdd_tolerance must be >= 0"),
        ({"training_max_drawdown": -1.0}, "training_max_drawdown must be a finite"),
        ({"training_max_drawdown": float("inf")}, "training_max_drawdown must be a finite"),
        ({"training_max_drawdown": float("nan")}, "training_max_drawdown must be a finite"),
        ({"training_sharpe": float("inf")}, "training_sharpe must be finite"),
        ({"training_sharpe": float("nan")}, "training_sharpe must be finite"),
    ],
)
def test_config_validation(kwargs: dict[str, float], match: str) -> None:
    base = {"training_sharpe": 0.1, "training_max_drawdown": 1.0}
    base.update(kwargs)
    with pytest.raises(ValueError, match=match):
        lockbox_evaluation(np.array([0.1, 0.2, 0.3]), np.array([1, 2, 3]), **base)  # type: ignore[arg-type]


def test_max_drawdown_helper() -> None:
    assert _max_drawdown(np.array([])) == 0.0
    # Monotonically non-decreasing equity → no drawdown.
    assert _max_drawdown(np.array([0.1, 0.2, 0.3])) == 0.0
    # Peak at +0.3 then down to -0.2 cumulative → drawdown 0.5.
    assert _max_drawdown(np.array([0.3, -0.2, -0.3])) == pytest.approx(0.5)


def test_metadata_echoed() -> None:
    r = lockbox_evaluation(
        np.array([0.1, 0.2, 0.3, 0.1, 0.2]),
        np.arange(5),
        training_sharpe=0.3,
        training_max_drawdown=2.0,
        min_trades=5,
        default_window_days=5,
    )
    assert r.training_sharpe == 0.3
    assert r.training_max_drawdown == 2.0
    assert r.min_trades == 5
