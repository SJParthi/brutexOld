"""Consolidated cross-engine invariant catalog for :mod:`brutex.metrics`.

A single Hypothesis property module that ties together the *cross-engine*
invariants the Stage-4 statistical-validity surfaces must jointly satisfy
(DEC-METRICS-INVARIANTS-ROUND2-001). The individual per-module property
suites remain the primary owners of each engine's local invariants
(``test_deflated_sharpe_properties.py``, ``test_cpcv_wfo_properties.py``,
``test_spa_properties.py``, ``test_pbo_properties.py``,
``test_lockbox_mc_properties.py``, ``test_metrics_properties.py``); this
file deliberately encodes only the *ties between* engines — and the few
whole-family guarantees (e.g. "no NaN/inf escapes any statistic for finite
inputs") — that no single per-module suite expresses in one place:

* **PSR monotone in observed Sharpe** (symmetric returns, fixed ``n``) AND
  the **DSR ≤ PSR(0)** deflation tie, asserted through the *public*
  :func:`deflated_sharpe_ratio` so the two engines
  (:func:`expected_max_sharpe_ratio` + :func:`probabilistic_sharpe_ratio`)
  are checked composed, not just in isolation.
* **expected-max-Sharpe ≥ 0 and non-decreasing in ``n_trials``** — the
  deflation benchmark grows with the multiple-testing count.
* **finite-or-None** across :func:`sharpe_of_returns`,
  :func:`cpcv_pass_rate` and the :func:`compute_metrics` Sharpe — no
  ``NaN``/``inf`` ever escapes for finite inputs (spec line 746).
* **SPA p-value bracket + determinism** — ``lower ≤ consistent ≤ upper``,
  all in ``[0, 1]``, identical under a fixed seed.
* **WFO no-look-ahead** — every walk has ``train_stop ≤ test_start`` AND
  ``max(train_idx) < min(test_idx)`` across all three schemes.
* **Monte-Carlo determinism + p-value range** — ``p ∈ (0, 1]`` and a
  fixed seed reproduces the result bit-for-bit.

All randomness is seed-pinned and example counts are bounded
(``max_examples=50``) so the catalog is fast and never flaky, while still
holding the 100% line+branch gate on the metrics package via the
per-module suites (this file adds assertions, not new code paths).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import numpy.typing as npt
from brutex.metrics import ClosedTrade, EquityCurve, compute_metrics
from brutex.metrics.cpcv import cpcv_pass_rate, sharpe_of_returns
from brutex.metrics.deflated_sharpe import (
    deflated_sharpe_ratio,
    expected_max_sharpe_ratio,
    probabilistic_sharpe_ratio,
)
from brutex.metrics.monte_carlo import monte_carlo_permutation_test
from brutex.metrics.spa import BootstrapConfig, hansen_spa
from brutex.metrics.wfo import WFOScheme, walk_forward_splits
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st

_SETTINGS = settings(max_examples=50, deadline=None)

# Bounded, finite moment ranges keeping the PSR radicand strictly positive
# for the symmetric (skew=0) case: ``1 + (g4-1)/4 * SR^2 > 0`` always holds
# for ``g4 >= 1``, so any bounded Sharpe is admissible.
_sharpe = st.floats(min_value=-3.0, max_value=3.0, allow_nan=False, allow_infinity=False)
_kurt = st.floats(min_value=1.5, max_value=8.0, allow_nan=False, allow_infinity=False)
_n = st.integers(min_value=2, max_value=5_000)
_trials = st.integers(min_value=1, max_value=100_000)
_variance = st.floats(min_value=0.0, max_value=4.0, allow_nan=False, allow_infinity=False)


# ── Deflated Sharpe family (cross-engine ties) ───────────────────────────
@given(a=_sharpe, b=_sharpe, n=_n, kurt=_kurt)
@_SETTINGS
def test_psr_monotone_non_decreasing_in_observed_sharpe(
    a: float, b: float, n: int, kurt: float
) -> None:
    """For symmetric returns (skew=0) and a fixed ``n``, PSR(0) is
    monotone non-decreasing in the observed Sharpe."""
    lo, hi = (a, b) if a <= b else (b, a)
    psr_lo = probabilistic_sharpe_ratio(lo, 0.0, n, skew=0.0, kurtosis=kurt)
    psr_hi = probabilistic_sharpe_ratio(hi, 0.0, n, skew=0.0, kurtosis=kurt)
    assert psr_lo <= psr_hi + 1e-12


@given(sr=_sharpe, trials=_trials, var=_variance, n=_n, kurt=_kurt)
@_SETTINGS
def test_dsr_never_exceeds_undeflated_psr(
    sr: float, trials: int, var: float, n: int, kurt: float
) -> None:
    """The composed DSR (= PSR(SR_0)) never exceeds PSR(0), since the
    deflation benchmark ``SR_0 >= 0`` — checked through the public engine."""
    result = deflated_sharpe_ratio(
        sr, n_trials=trials, trial_sharpe_variance=var, sample_length=n, skew=0.0, kurtosis=kurt
    )
    assert result.expected_max_sharpe >= 0.0
    assert result.deflated_sharpe_ratio <= result.probabilistic_sharpe_ratio + 1e-12
    assert 0.0 <= result.deflated_sharpe_ratio <= 1.0
    assert 0.0 <= result.probabilistic_sharpe_ratio <= 1.0


@given(var=_variance, a=_trials, b=_trials)
@_SETTINGS
def test_expected_max_sharpe_nonneg_and_monotone_in_trials(var: float, a: int, b: int) -> None:
    """``SR_0`` is ``>= 0`` and non-decreasing in ``n_trials`` at fixed V."""
    lo, hi = (a, b) if a <= b else (b, a)
    sr_lo = expected_max_sharpe_ratio(lo, var)
    sr_hi = expected_max_sharpe_ratio(hi, var)
    assert sr_lo >= 0.0
    assert sr_hi >= 0.0
    assert sr_lo <= sr_hi + 1e-12


# ── Finite-or-None across the Sharpe-based engines ───────────────────────
_finite_returns = st.lists(
    st.floats(min_value=-50.0, max_value=50.0, allow_nan=False, allow_infinity=False),
    min_size=2,
    max_size=64,
)


@given(returns=_finite_returns)
@_SETTINGS
def test_sharpe_of_returns_is_none_or_finite(returns: list[float]) -> None:
    """:func:`sharpe_of_returns` never leaks a NaN/inf for finite input."""
    value = sharpe_of_returns(returns)
    assert value is None or math.isfinite(value)


@given(
    returns=st.lists(
        st.floats(min_value=-50.0, max_value=50.0, allow_nan=False, allow_infinity=False),
        min_size=4,
        max_size=80,
    )
)
@_SETTINGS
def test_cpcv_pass_rate_finite_in_unit_interval(returns: list[float]) -> None:
    """:func:`cpcv_pass_rate` is a finite fraction in ``[0, 1]``."""
    with np.errstate(all="ignore"):
        rate = cpcv_pass_rate(np.asarray(returns, dtype=np.float64), n_groups=4, n_test_groups=2)
    assert math.isfinite(rate)
    assert 0.0 <= rate <= 1.0


_T0 = datetime(2025, 1, 1, 9, 15, tzinfo=UTC)


def _make_trades(pnls: list[int]) -> list[ClosedTrade]:
    out: list[ClosedTrade] = []
    for i, pnl in enumerate(pnls):
        entry = _T0 + timedelta(minutes=10 * i)
        out.append(
            ClosedTrade(
                pnl_paisa=pnl,
                mfe_paisa=abs(pnl),
                mae_paisa=abs(pnl),
                holding_bars=3,
                entry_ts=entry,
                exit_ts=entry + timedelta(minutes=3),
                direction=Side.BUY if i % 2 == 0 else Side.SELL,
            )
        )
    return out


def _curve_from(pnls: list[int], start: int = 10_000_000) -> EquityCurve:
    equity = [start]
    running = start
    for pnl in pnls:
        running += pnl
        equity.append(running)
    return EquityCurve(equity_paisa=tuple(equity), start_equity_paisa=start, span_years=1.0)


@given(
    pnls=st.lists(st.integers(min_value=-5_000_000, max_value=5_000_000), min_size=1, max_size=40)
)
@_SETTINGS
def test_compute_metrics_sharpe_is_none_or_finite(pnls: list[int]) -> None:
    """The :func:`compute_metrics` Sharpe is ``None`` or finite (spec 746)."""
    metrics = compute_metrics(_make_trades(pnls), _curve_from(pnls), total_bars=len(pnls) * 3)
    assert metrics.sharpe_ratio is None or math.isfinite(metrics.sharpe_ratio)


# ── SPA p-value bracket + determinism ────────────────────────────────────
@st.composite
def _return_matrix(draw: st.DrawFn) -> npt.NDArray[np.float64]:
    n_obs = draw(st.integers(min_value=8, max_value=24))
    n_strat = draw(st.integers(min_value=1, max_value=4))
    flat = draw(
        st.lists(
            st.floats(min_value=-2.0, max_value=2.0, allow_nan=False, allow_infinity=False),
            min_size=n_obs * n_strat,
            max_size=n_obs * n_strat,
        )
    )
    arr = np.asarray(flat, dtype=np.float64).reshape(n_obs, n_strat)
    # Ensure every column has strictly positive variance (SPA requirement).
    arr[0, :] += 1.0
    arr[1, :] -= 1.0
    return arr


@given(data=_return_matrix())
@_SETTINGS
def test_spa_pvalue_bracket_and_determinism(data: npt.NDArray[np.float64]) -> None:
    """``lower <= consistent <= upper``, all in ``[0, 1]``, seed-deterministic."""
    config = BootstrapConfig(seed=7, n_resamples=120)
    first = hansen_spa(data, config)
    assert 0.0 <= first.pvalue_lower <= first.pvalue_consistent <= first.pvalue_upper <= 1.0
    second = hansen_spa(data, BootstrapConfig(seed=7, n_resamples=120))
    assert first == second


# ── WFO no-look-ahead across all schemes ─────────────────────────────────
@st.composite
def _wfo_config(draw: st.DrawFn) -> tuple[int, int, int, int, int]:
    train_size = draw(st.integers(min_value=2, max_value=20))
    test_size = draw(st.integers(min_value=1, max_value=10))
    step = draw(st.integers(min_value=1, max_value=8))
    embargo = draw(st.integers(min_value=0, max_value=5))
    n_samples = draw(st.integers(min_value=train_size + test_size + embargo, max_value=80))
    return n_samples, train_size, test_size, step, embargo


@given(config=_wfo_config(), scheme=st.sampled_from(list(WFOScheme)))
@_SETTINGS
def test_wfo_walks_have_no_look_ahead(
    config: tuple[int, int, int, int, int], scheme: WFOScheme
) -> None:
    """Every walk: ``train_stop <= test_start`` AND (when both windows are
    non-empty) ``max(train_idx) < min(test_idx)``."""
    n_samples, train_size, test_size, step, embargo = config
    for walk in walk_forward_splits(
        n_samples, scheme, train_size=train_size, test_size=test_size, step=step, embargo=embargo
    ):
        assert walk.train_stop <= walk.test_start
        train_idx = walk.train
        test_idx = walk.test
        if train_idx.size and test_idx.size:
            assert int(train_idx.max()) < int(test_idx.min())


# ── Monte-Carlo determinism + p-value range ──────────────────────────────
def _mean_pos(positions: npt.NDArray[np.intp]) -> float:
    return float(positions.mean())


@given(
    observed=st.floats(min_value=-500.0, max_value=500.0, allow_nan=False, allow_infinity=False),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
    n_perm=st.integers(min_value=1, max_value=200),
)
@_SETTINGS
def test_monte_carlo_pvalue_range_and_determinism(observed: float, seed: int, n_perm: int) -> None:
    """``p ∈ (0, 1]`` and a fixed seed reproduces the result exactly."""
    kwargs = {
        "n_bars": 150,
        "n_entries": 15,
        "evaluate": _mean_pos,
        "seed": seed,
        "n_permutations": n_perm,
    }
    first = monte_carlo_permutation_test(observed, **kwargs)  # type: ignore[arg-type]
    assert 0.0 < first.pvalue <= 1.0
    second = monte_carlo_permutation_test(observed, **kwargs)  # type: ignore[arg-type]
    assert first == second
