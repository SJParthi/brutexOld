"""Property tests for :mod:`brutex.metrics.cpcv` and :mod:`brutex.metrics.wfo`.

Proves the invariants that must hold for *every* valid configuration,
complementing the known-value unit tests:

1. **No label leakage (the no-look-ahead guarantee)** — after purging, no
   training observation lies within ``label_span`` bars of any test
   observation, so no training label window can overlap a test label
   window. This is the property the whole purge machinery exists to
   provide (López de Prado, AFML 2018, Ch. 7 §7.4).
2. **Disjoint train/test** — a CPCV split never shares an index between
   its train and test sets.
3. **Split count** — exactly ``C(n_groups, n_test_groups)`` splits.
4. **Pass-rate domain** — every CPCV / WFO pass-rate is a probability in
   ``[0, 1]``.
5. **WFO causality** — every walk's training window ends no later than its
   test window begins (``train_stop <= test_start``).
6. **Sharpe sign** — a defined Sharpe shares the sign of the mean return.
"""

from __future__ import annotations

import math

import numpy as np
from brutex.metrics.cpcv import cpcv_pass_rate, cpcv_splits, sharpe_of_returns
from brutex.metrics.wfo import WFOScheme, walk_forward_splits, wfo_pass_rate
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# pyright: reportCallIssue=false
# Rationale (DEC-PYRIGHT-UNMASK-001): the Hypothesis `@st.composite` strategies
# below are invoked with no args (e.g. `_cpcv_config()`) because the decorator
# injects `draw` at runtime; pyright does not model the `@composite` signature
# rewrite and emits a spurious "missing draw". Scoped to this file only —
# `reportCallIssue` stays a hard error everywhere else (same convention as
# tests/property/test_engine_stage1a_properties.py).

_finite_floats = st.floats(
    allow_nan=False, allow_infinity=False, min_value=-1_000.0, max_value=1_000.0
)


@st.composite
def _cpcv_config(draw: st.DrawFn) -> tuple[int, int, int, int, int]:
    """A valid (n_samples, n_groups, n_test_groups, label_span, embargo)."""
    n_groups = draw(st.integers(min_value=2, max_value=7))
    n_test_groups = draw(st.integers(min_value=1, max_value=n_groups - 1))
    n_samples = draw(st.integers(min_value=n_groups, max_value=56))
    label_span = draw(st.integers(min_value=0, max_value=6))
    embargo = draw(st.integers(min_value=0, max_value=6))
    return n_samples, n_groups, n_test_groups, label_span, embargo


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(_cpcv_config())
def test_cpcv_no_label_leakage(config: tuple[int, int, int, int, int]) -> None:
    """Purged training labels never overlap a test label (no look-ahead)."""
    n_samples, n_groups, n_test_groups, label_span, embargo = config
    splits = cpcv_splits(
        n_samples,
        n_groups=n_groups,
        n_test_groups=n_test_groups,
        label_span=label_span,
        embargo=embargo,
    )
    for split in splits:
        if split.train.size and split.test.size:
            distances = np.abs(split.train[:, None] - split.test[None, :])
            # A train label [i, i+h] overlaps a test label [j, j+h] iff
            # |i - j| <= h; purging guarantees the strict opposite.
            assert int(distances.min()) > label_span


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(_cpcv_config())
def test_cpcv_train_test_disjoint(config: tuple[int, int, int, int, int]) -> None:
    n_samples, n_groups, n_test_groups, label_span, embargo = config
    for split in cpcv_splits(
        n_samples,
        n_groups=n_groups,
        n_test_groups=n_test_groups,
        label_span=label_span,
        embargo=embargo,
    ):
        assert set(split.train.tolist()).isdisjoint(split.test.tolist())


@settings(max_examples=200)
@given(_cpcv_config())
def test_cpcv_split_count(config: tuple[int, int, int, int, int]) -> None:
    n_samples, n_groups, n_test_groups, _, _ = config
    splits = cpcv_splits(n_samples, n_groups=n_groups, n_test_groups=n_test_groups)
    assert len(splits) == math.comb(n_groups, n_test_groups)


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(_finite_floats, min_size=4, max_size=40),
    st.integers(min_value=2, max_value=4),
)
def test_cpcv_pass_rate_in_unit_interval(returns: list[float], n_groups: int) -> None:
    n_test_groups = max(1, n_groups // 2)
    if n_groups > len(returns):  # keep the config valid
        return
    rate = cpcv_pass_rate(returns, n_groups=n_groups, n_test_groups=n_test_groups)
    assert 0.0 <= rate <= 1.0


@st.composite
def _wfo_config(draw: st.DrawFn) -> tuple[int, int, int, int, int]:
    """A valid (n_samples, train_size, test_size, step, embargo) with >= 1 walk."""
    train_size = draw(st.integers(min_value=2, max_value=30))
    test_size = draw(st.integers(min_value=2, max_value=20))
    step = draw(st.integers(min_value=1, max_value=15))
    embargo = draw(st.integers(min_value=0, max_value=10))
    # train_size + embargo + test_size guarantees >= 1 walk for all three
    # schemes (expanding's smaller min-train only starts testing earlier).
    n_samples = draw(st.integers(min_value=train_size + embargo + test_size, max_value=200))
    return n_samples, train_size, test_size, step, embargo


@settings(max_examples=200)
@given(_wfo_config(), st.sampled_from(list(WFOScheme)))
def test_wfo_train_before_test(config: tuple[int, int, int, int, int], scheme: WFOScheme) -> None:
    n_samples, train_size, test_size, step, embargo = config
    for walk in walk_forward_splits(
        n_samples,
        scheme,
        train_size=train_size,
        test_size=test_size,
        step=step,
        embargo=embargo,
    ):
        assert walk.train_stop <= walk.test_start  # causality
        assert walk.test_start - walk.train_stop == embargo  # the embargo gap


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(_wfo_config(), st.sampled_from(list(WFOScheme)), st.data())
def test_wfo_pass_rate_in_unit_interval(
    config: tuple[int, int, int, int, int], scheme: WFOScheme, data: st.DataObject
) -> None:
    n_samples, train_size, test_size, step, embargo = config
    returns = data.draw(st.lists(_finite_floats, min_size=n_samples, max_size=n_samples))
    rate = wfo_pass_rate(
        returns,
        scheme,
        train_size=train_size,
        test_size=test_size,
        step=step,
        embargo=embargo,
    )
    assert 0.0 <= rate <= 1.0


@given(st.lists(_finite_floats, min_size=2, max_size=50))
def test_sharpe_sign_matches_mean(returns: list[float]) -> None:
    sharpe = sharpe_of_returns(returns)
    if sharpe is not None:
        mean = float(np.mean(returns))
        # sign(sharpe) == sign(mean); both zero is also agreement.
        assert math.copysign(1.0, sharpe) == math.copysign(1.0, mean) or mean == 0.0
