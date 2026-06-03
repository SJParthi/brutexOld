"""Hypothesis property tests for :mod:`brutex.metrics.lockbox` (Stage-4 #8)
and :mod:`brutex.metrics.monte_carlo` (Stage-4 #9).

Invariants that must hold for *every* valid input, complementing the
known-scenario unit tests and the byte-exact golden fixtures.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from brutex.metrics.lockbox import LockboxStatus, lockbox_evaluation
from brutex.metrics.monte_carlo import monte_carlo_permutation_test
from hypothesis import given, settings
from hypothesis import strategies as st

_SETTINGS = settings(max_examples=50, deadline=None)


@st.composite
def _trade_series(
    draw: st.DrawFn, max_n: int = 40
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.int64]]:
    """Draw a chronological ``(returns, day-ordinals)`` trade series."""
    n = draw(st.integers(min_value=2, max_value=max_n))
    returns = draw(
        st.lists(
            st.floats(min_value=-5.0, max_value=5.0, allow_nan=False, allow_infinity=False),
            min_size=n,
            max_size=n,
        )
    )
    deltas = draw(st.lists(st.integers(min_value=0, max_value=5), min_size=n, max_size=n))
    days = np.cumsum(np.asarray(deltas, dtype=np.int64))
    return np.asarray(returns, dtype=np.float64), days


@given(
    series=_trade_series(),
    min_trades=st.integers(min_value=1, max_value=20),
    training_sharpe=st.floats(min_value=-2.0, max_value=2.0, allow_nan=False),
    training_mdd=st.floats(min_value=0.0, max_value=20.0, allow_nan=False),
    window=st.integers(min_value=1, max_value=50),
)
@_SETTINGS
def test_lockbox_invariants(
    series: tuple[npt.NDArray[np.float64], npt.NDArray[np.int64]],
    min_trades: int,
    training_sharpe: float,
    training_mdd: float,
    window: int,
) -> None:
    returns, days = series
    kw = {
        "training_sharpe": training_sharpe,
        "training_max_drawdown": training_mdd,
        "min_trades": min_trades,
        "default_window_days": window,
    }
    result = lockbox_evaluation(returns, days, **kw)  # type: ignore[arg-type]

    assert result.lockbox_max_drawdown >= 0.0
    assert result.passed == (result.status is LockboxStatus.PASS)
    assert result.count_gate_passed == (result.n_trades >= min_trades)
    if result.status is not LockboxStatus.INSUFFICIENT_N:
        assert result.n_trades >= min_trades
    # Purity / determinism (Golden Rule #4): no RNG, same input → same result.
    assert lockbox_evaluation(returns, days, **kw) == result  # type: ignore[arg-type]


def _mean_pos(positions: npt.NDArray[np.intp]) -> float:
    return float(positions.mean())


def _below(positions: npt.NDArray[np.intp]) -> float:
    return -1.0e18  # never beats any reasonable observed


def _above(positions: npt.NDArray[np.intp]) -> float:
    return 1.0e18  # always beats any reasonable observed


@given(
    observed=st.floats(min_value=-1000.0, max_value=2000.0, allow_nan=False),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
    n_perm=st.integers(min_value=1, max_value=300),
    alpha=st.floats(min_value=0.001, max_value=0.5, allow_nan=False),
)
@_SETTINGS
def test_monte_carlo_invariants(observed: float, seed: int, n_perm: int, alpha: float) -> None:
    res = monte_carlo_permutation_test(
        observed,
        n_bars=200,
        n_entries=20,
        evaluate=_mean_pos,
        seed=seed,
        n_permutations=n_perm,
        alpha=alpha,
    )
    assert 0.0 < res.pvalue <= 1.0
    assert res.pvalue == (1 + res.n_beaten) / (n_perm + 1)
    assert res.passed == (res.pvalue < alpha)
    assert 0 <= res.n_beaten <= n_perm
    # Determinism: same seed → identical result.
    again = monte_carlo_permutation_test(
        observed,
        n_bars=200,
        n_entries=20,
        evaluate=_mean_pos,
        seed=seed,
        n_permutations=n_perm,
        alpha=alpha,
    )
    assert res == again


@given(n_perm=st.integers(min_value=1, max_value=500))
@_SETTINGS
def test_monte_carlo_extremes(n_perm: int) -> None:
    # An exit logic that never beats the observed → minimal p = 1/(B+1).
    lo = monte_carlo_permutation_test(0.0, 100, 10, _below, seed=0, n_permutations=n_perm)
    assert lo.n_beaten == 0
    assert lo.pvalue == 1 / (n_perm + 1)
    # An exit logic that always beats it → maximal p = 1.
    hi = monte_carlo_permutation_test(0.0, 100, 10, _above, seed=0, n_permutations=n_perm)
    assert hi.n_beaten == n_perm
    assert hi.pvalue == 1.0
