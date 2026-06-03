"""Hypothesis property tests for :mod:`brutex.metrics.spa`.

Proves the invariants that must hold for *every* valid excess-return
matrix, complementing the known-scenario unit tests in
``tests/unit/test_spa.py`` and the byte-exact oracle in
``tests/golden/test_spa_golden.py``:

1. **Probability range** — every reported p-value is in ``[0, 1]``.
2. **Determinism** (Golden Rule #4) — identical input + identical seed
   reproduce identical results, bit-for-bit.
3. **SPA variant ordering** — ``pvalue_lower <= pvalue_consistent <=
   pvalue_upper`` (Hansen 2005 §3.2).
4. **Multiplicity never helps** — the multiple-testing-corrected p-value
   of the full pool is **not smaller** than the naive single-strategy
   p-value of the best member, for both SPA (vs the same strategy tested
   alone) and Romano-Wolf (adjusted ``>=`` naive, elementwise).
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from brutex.metrics.spa import (
    BootstrapConfig,
    BootstrapMethod,
    hansen_spa,
    reality_check,
    romano_wolf,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# A modest, fast bootstrap; determinism is exact regardless of B.
_CFG = BootstrapConfig(seed=4321, n_resamples=200)
_SETTINGS = settings(max_examples=40, deadline=None)


@st.composite
def _return_matrix(
    draw: st.DrawFn, max_obs: int = 60, max_strat: int = 8
) -> npt.NDArray[np.float64]:
    """Draw a finite ``(n_obs, n_strategies)`` excess-return matrix.

    Built from continuous Gaussian noise plus a per-column shift, so every
    column has strictly positive variance (required by the studentized
    tests) and the pool mixes real-edge and no-edge strategies.
    """
    n_obs = draw(st.integers(min_value=2, max_value=max_obs))
    n_strat = draw(st.integers(min_value=1, max_value=max_strat))
    data_seed = draw(st.integers(min_value=0, max_value=2**32 - 1))
    rng = np.random.default_rng(data_seed)
    base = rng.standard_normal((n_obs, n_strat))
    shifts = rng.uniform(-0.8, 0.8, size=n_strat)
    return base + shifts


@given(data=_return_matrix())
@_SETTINGS
def test_pvalues_in_unit_interval(data: npt.NDArray[np.float64]) -> None:
    rc = reality_check(data, _CFG)
    spa = hansen_spa(data, _CFG)
    rw = romano_wolf(data, _CFG)

    assert 0.0 <= rc.pvalue <= 1.0
    for p in (spa.pvalue_lower, spa.pvalue_consistent, spa.pvalue_upper):
        assert 0.0 <= p <= 1.0
    for p in (*rw.adjusted_pvalues, *rw.naive_pvalues):
        assert 0.0 <= p <= 1.0


@given(data=_return_matrix())
@_SETTINGS
def test_determinism_under_fixed_seed(data: npt.NDArray[np.float64]) -> None:
    assert reality_check(data, _CFG) == reality_check(data, _CFG)
    assert hansen_spa(data, _CFG) == hansen_spa(data, _CFG)
    assert romano_wolf(data, _CFG) == romano_wolf(data, _CFG)


@given(data=_return_matrix(max_strat=10))
@_SETTINGS
def test_spa_variant_ordering(data: npt.NDArray[np.float64]) -> None:
    spa = hansen_spa(data, _CFG)
    assert spa.pvalue_lower <= spa.pvalue_consistent <= spa.pvalue_upper


@given(data=_return_matrix(max_strat=10))
@_SETTINGS
def test_romano_wolf_adjusted_not_below_naive(data: npt.NDArray[np.float64]) -> None:
    """The FWER correction is never more liberal than the naive test."""
    rw = romano_wolf(data, _CFG)
    for adjusted, naive in zip(rw.adjusted_pvalues, rw.naive_pvalues, strict=True):
        assert adjusted >= naive - 1e-12


@given(data=_return_matrix(max_strat=10))
@_SETTINGS
def test_spa_pool_pvalue_not_below_single_strategy(data: npt.NDArray[np.float64]) -> None:
    """SPA on the whole pool >= SPA on just its strongest member alone.

    Because the bootstrap row indices depend only on ``n_obs`` (not on the
    number of columns) and the seed is fixed, the single-strategy run
    resamples the identical column, making this the exact, convention-clean
    statement of "data-snooping correction never lowers the p-value below
    the naive single-strategy test".
    """
    rw = romano_wolf(data, _CFG)
    best = int(np.argmax(rw.test_statistics))
    full = hansen_spa(data, _CFG)
    single = hansen_spa(data[:, [best]], _CFG)
    assert full.pvalue_upper >= single.pvalue_upper - 1e-12
    assert full.pvalue_consistent >= single.pvalue_consistent - 1e-12


@given(
    data=_return_matrix(),
    method=st.sampled_from(BootstrapMethod),
)
@_SETTINGS
def test_both_bootstrap_methods_yield_valid_pvalues(
    data: npt.NDArray[np.float64], method: BootstrapMethod
) -> None:
    cfg = BootstrapConfig(seed=99, n_resamples=150, method=method)
    rc = reality_check(data, cfg)
    spa = hansen_spa(data, cfg)
    assert 0.0 <= rc.pvalue <= 1.0
    assert 0.0 <= spa.pvalue <= 1.0
    assert rc.method == method.value
