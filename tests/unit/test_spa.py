"""Unit tests for :mod:`brutex.metrics.spa` — Hansen SPA, White Reality Check,
Romano-Wolf step-down.

Targets 100% line + branch coverage of ``brutex/metrics/spa.py`` (Golden
Rule #12). The behavioural anchors are the four scenarios the task pins:
single strategy, many strategies, all-noise → high p-value, and one
clearly-superior strategy → low p-value. Validation, config, and both
bootstrap schemes are exercised for branch completeness.

Determinism (Golden Rule #4) is asserted here and proven exactly in the
golden fixtures (``tests/golden/test_spa_golden.py``); distributional
invariants live in ``tests/property/test_spa_properties.py``.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest
from brutex.metrics.spa import (
    BootstrapConfig,
    BootstrapMethod,
    RealityCheckResult,
    RomanoWolfResult,
    SPAResult,
    expected_block_length,
    hansen_spa,
    reality_check,
    romano_wolf,
)

# Small replication counts keep the suite fast; determinism is exact under
# a fixed seed regardless of B.
_FAST = BootstrapConfig(seed=2024, n_resamples=800)


def _noise(n_obs: int, n_strat: int, seed: int) -> np.ndarray:
    """An iid standard-normal excess-return matrix (the complete null)."""
    return np.random.default_rng(seed).standard_normal((n_obs, n_strat))


def _with_edge(n_obs: int, n_strat: int, col: int, edge: float, seed: int) -> np.ndarray:
    """Noise with a single strategy given a large positive mean edge."""
    data = _noise(n_obs, n_strat, seed)
    data[:, col] += edge
    return data


# --------------------------------------------------------------------------- #
# BootstrapConfig validation                                                  #
# --------------------------------------------------------------------------- #
def test_config_defaults() -> None:
    cfg = BootstrapConfig(seed=0)
    assert cfg.seed == 0
    assert cfg.n_resamples == 10_000
    assert cfg.block_length is None
    assert cfg.method is BootstrapMethod.STATIONARY


def test_config_explicit_block_length_ok() -> None:
    cfg = BootstrapConfig(seed=1, n_resamples=10, block_length=4.5, method=BootstrapMethod.CIRCULAR)
    assert cfg.block_length == 4.5
    assert cfg.method is BootstrapMethod.CIRCULAR


@pytest.mark.parametrize(
    ("make", "match"),
    [
        (lambda: BootstrapConfig(seed=1.5), "seed must be an int"),  # type: ignore[arg-type]
        (lambda: BootstrapConfig(seed=-1), "seed must be >= 0"),
        (lambda: BootstrapConfig(seed=0, n_resamples=0), "n_resamples must be >= 1"),
        (lambda: BootstrapConfig(seed=0, block_length=0.0), "block_length must be a finite"),
        (lambda: BootstrapConfig(seed=0, block_length=-2.0), "block_length must be a finite"),
        (lambda: BootstrapConfig(seed=0, block_length=0.5), "block_length must be a finite"),
        (
            lambda: BootstrapConfig(seed=0, block_length=float("inf")),
            "block_length must be a finite",
        ),
        (
            lambda: BootstrapConfig(seed=0, block_length=float("nan")),
            "block_length must be a finite",
        ),
    ],
)
def test_config_invalid(make: Callable[[], BootstrapConfig], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        make()


# --------------------------------------------------------------------------- #
# Matrix validation                                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("test_fn", [reality_check, hansen_spa, romano_wolf])
def test_rejects_non_2d(test_fn: Callable[..., object]) -> None:
    with pytest.raises(ValueError, match="must be 2-D"):
        test_fn(np.zeros(5), _FAST)
    with pytest.raises(ValueError, match="must be 2-D"):
        test_fn(np.zeros((2, 2, 2)), _FAST)


def test_rejects_too_few_observations() -> None:
    with pytest.raises(ValueError, match="needs >= 2 observations"):
        reality_check(np.ones((1, 3)), _FAST)


def test_rejects_no_strategies() -> None:
    with pytest.raises(ValueError, match="needs >= 1 strategy"):
        reality_check(np.empty((5, 0)), _FAST)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_rejects_non_finite(bad: float) -> None:
    data = _noise(20, 3, 0)
    data[3, 1] = bad
    with pytest.raises(ValueError, match="must be finite"):
        reality_check(data, _FAST)


@pytest.mark.parametrize("test_fn", [hansen_spa, romano_wolf])
def test_studentized_tests_reject_constant_column(test_fn: Callable[..., object]) -> None:
    data = _noise(40, 3, 1)
    data[:, 2] = 7.0  # constant → zero variance
    with pytest.raises(ValueError, match="constant column"):
        test_fn(data, _FAST)


def test_reality_check_allows_constant_column() -> None:
    """White's RC is not studentized, so a constant column is fine."""
    data = _noise(40, 3, 1)
    data[:, 2] = 7.0
    result = reality_check(data, _FAST)
    assert 0.0 <= result.pvalue <= 1.0


@pytest.mark.parametrize("alpha", [0.0, 1.0, -0.1, 1.5])
def test_romano_wolf_rejects_bad_alpha(alpha: float) -> None:
    with pytest.raises(ValueError, match="alpha must be"):
        romano_wolf(_noise(30, 3, 0), _FAST, alpha=alpha)


# --------------------------------------------------------------------------- #
# expected_block_length                                                       #
# --------------------------------------------------------------------------- #
def test_expected_block_length_spec_formula() -> None:
    # floor(1.75 * 1000 ** (1/3)) = floor(1.75 * 10) = 17
    assert expected_block_length(1000) == 17.0
    # floor(1.75 * 250 ** (1/3)) = floor(1.75 * 6.299...) = 11
    assert expected_block_length(250) == 11.0


def test_expected_block_length_floored_at_one() -> None:
    assert expected_block_length(0) == 1.0
    assert expected_block_length(1) == 1.0


# --------------------------------------------------------------------------- #
# Scenario: single strategy                                                   #
# --------------------------------------------------------------------------- #
def test_single_strategy_all_tests_run() -> None:
    data = _with_edge(120, 1, col=0, edge=0.4, seed=3)
    rc = reality_check(data, _FAST)
    spa = hansen_spa(data, _FAST)
    rw = romano_wolf(data, _FAST)
    assert rc.n_strategies == spa.n_strategies == rw.n_strategies == 1
    assert rc.best_strategy == spa.best_strategy == 0
    for p in (rc.pvalue, spa.pvalue_consistent, spa.pvalue_lower, spa.pvalue_upper):
        assert 0.0 <= p <= 1.0
    assert len(rw.adjusted_pvalues) == 1
    assert len(rw.naive_pvalues) == 1


# --------------------------------------------------------------------------- #
# Scenario: many strategies, complete null (all noise) → high p-value         #
# --------------------------------------------------------------------------- #
def test_all_noise_high_pvalue() -> None:
    data = _noise(250, 25, seed=42)
    rc = reality_check(data, _FAST)
    spa = hansen_spa(data, _FAST)
    rw = romano_wolf(data, _FAST, alpha=0.05)
    # No real edge: the data-snooping-corrected p-values must be far from 0.
    assert rc.pvalue > 0.10
    assert spa.pvalue_consistent > 0.10
    # FWER control: nothing (or almost nothing) rejected under the null.
    assert rw.n_rejected == 0


# --------------------------------------------------------------------------- #
# Scenario: one clearly-superior strategy → low p-value                       #
# --------------------------------------------------------------------------- #
def test_one_superior_low_pvalue() -> None:
    data = _with_edge(250, 25, col=11, edge=1.0, seed=7)
    rc = reality_check(data, _FAST)
    spa = hansen_spa(data, _FAST)
    rw = romano_wolf(data, _FAST, alpha=0.05)

    assert rc.pvalue < 0.05
    assert spa.pvalue_consistent < 0.05
    assert rc.best_strategy == 11
    assert spa.best_strategy == 11
    # The superior strategy must be rejected and carry the smallest adjusted p.
    assert 11 in rw.rejected_indices
    assert rw.rejected[11] is True
    assert rw.adjusted_pvalues[11] == min(rw.adjusted_pvalues)


# --------------------------------------------------------------------------- #
# SPA variant ordering + the pvalue alias                                     #
# --------------------------------------------------------------------------- #
def test_spa_variant_ordering() -> None:
    data = _with_edge(200, 15, col=4, edge=0.3, seed=9)
    spa = hansen_spa(data, _FAST)
    assert spa.pvalue_lower <= spa.pvalue_consistent <= spa.pvalue_upper
    assert spa.pvalue == spa.pvalue_consistent


def test_spa_small_sample_loglog_branch() -> None:
    """n_obs == 2 takes the degenerate (threshold = 0) recentring branch."""
    data = np.array([[0.5, -0.2], [1.5, 0.4]])  # 2 obs, both columns vary
    spa = hansen_spa(data, BootstrapConfig(seed=5, n_resamples=200))
    assert spa.n_observations == 2
    for p in (spa.pvalue_lower, spa.pvalue_consistent, spa.pvalue_upper):
        assert 0.0 <= p <= 1.0


# --------------------------------------------------------------------------- #
# Bootstrap method coverage                                                   #
# --------------------------------------------------------------------------- #
def test_circular_bootstrap_runs() -> None:
    data = _with_edge(150, 10, col=2, edge=0.5, seed=11)
    cfg = BootstrapConfig(seed=3, n_resamples=600, method=BootstrapMethod.CIRCULAR)
    rc = reality_check(data, cfg)
    spa = hansen_spa(data, cfg)
    rw = romano_wolf(data, cfg)
    assert rc.method == spa.method == rw.method == "circular"
    assert rc.block_length == expected_block_length(150)


def test_circular_minimum_block_length() -> None:
    """A length-1 block (the smallest valid) is an iid-style circular bootstrap."""
    data = _noise(30, 4, 0)
    cfg = BootstrapConfig(
        seed=1, n_resamples=200, block_length=1.0, method=BootstrapMethod.CIRCULAR
    )
    result = reality_check(data, cfg)
    assert result.block_length == 1.0  # echoed as configured
    assert 0.0 <= result.pvalue <= 1.0


def test_explicit_block_length_overrides_default() -> None:
    data = _noise(100, 5, 0)
    cfg = BootstrapConfig(seed=1, n_resamples=300, block_length=12.0)
    assert reality_check(data, cfg).block_length == 12.0


def test_circular_block_clamped_below_series_length() -> None:
    """A circular block_length >= n_obs is clamped so the bootstrap stays
    non-degenerate (every-resample-is-the-full-sample would zero the variance)."""
    data = _with_edge(8, 4, col=1, edge=1.0, seed=2)
    cfg = BootstrapConfig(
        seed=4, n_resamples=400, block_length=50.0, method=BootstrapMethod.CIRCULAR
    )
    # Without the clamp this would collapse omega; with it the studentized
    # tests run cleanly.
    spa = hansen_spa(data, cfg)
    assert 0.0 <= spa.pvalue_consistent <= 1.0


def test_studentized_tests_raise_on_collapsed_bootstrap_variance() -> None:
    """A stationary block_length so large that no block restart ever fires
    makes every resample reproduce the sample mean → zero bootstrap variance.
    The studentized tests must raise rather than emit NaN/inf (spec line 746)."""
    data = _noise(2, 2, seed=3)  # positive sample variance, so the check passes
    cfg = BootstrapConfig(seed=0, n_resamples=50, block_length=1.0e12)
    with pytest.raises(ValueError, match="bootstrap variance collapsed"):
        hansen_spa(data, cfg)
    with pytest.raises(ValueError, match="bootstrap variance collapsed"):
        romano_wolf(data, cfg)


# --------------------------------------------------------------------------- #
# Determinism + result-type metadata                                          #
# --------------------------------------------------------------------------- #
def test_determinism_same_seed_identical() -> None:
    data = _with_edge(160, 12, col=3, edge=0.4, seed=8)
    cfg = BootstrapConfig(seed=555, n_resamples=400)
    assert reality_check(data, cfg) == reality_check(data, cfg)
    assert hansen_spa(data, cfg) == hansen_spa(data, cfg)
    assert romano_wolf(data, cfg) == romano_wolf(data, cfg)


def test_different_seed_changes_bootstrap() -> None:
    data = _noise(200, 20, seed=1)
    a = reality_check(data, BootstrapConfig(seed=1, n_resamples=500))
    b = reality_check(data, BootstrapConfig(seed=2, n_resamples=500))
    # Same observed statistic (data unchanged) but a different null draw.
    assert a.test_statistic == b.test_statistic
    assert a.pvalue != b.pvalue


def test_result_metadata_echoed() -> None:
    data = _noise(80, 6, 0)
    cfg = BootstrapConfig(seed=1, n_resamples=321)
    rc = reality_check(data, cfg)
    assert isinstance(rc, RealityCheckResult)
    assert rc.n_observations == 80
    assert rc.n_strategies == 6
    assert rc.n_resamples == 321
    assert rc.method == "stationary"

    spa = hansen_spa(data, cfg)
    assert isinstance(spa, SPAResult)
    assert spa.n_resamples == 321

    rw = romano_wolf(data, cfg)
    assert isinstance(rw, RomanoWolfResult)
    assert rw.alpha == 0.05
    assert rw.n_rejected == len(rw.rejected_indices)
    assert len(rw.test_statistics) == 6


def test_romano_wolf_rejected_indices_match_adjusted() -> None:
    data = _with_edge(220, 18, col=5, edge=0.9, seed=13)
    rw = romano_wolf(data, _FAST, alpha=0.05)
    expected = tuple(i for i, p in enumerate(rw.adjusted_pvalues) if p <= 0.05)
    assert rw.rejected_indices == expected
    assert all(rw.rejected[i] for i in expected)
