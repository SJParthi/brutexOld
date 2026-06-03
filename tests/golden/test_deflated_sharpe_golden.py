"""Golden-master regression test for :mod:`brutex.metrics.deflated_sharpe`.

Locks the numeric output of the PSR / ``SR_0`` / DSR formulas for fixed
inputs so any drift in the implementation (or in the standard-library
``statistics.NormalDist`` CDF / inverse-CDF it relies on) fails loudly.

The two ``Φ(2)`` anchors are math-exact (a zero-numerator or a clean
``z = 2`` derivation) and locked with ``==``; the extreme-value ``SR_0``
values flow through the platform inverse-Normal quantile and are pinned at
``rel=1e-12`` (tight enough to catch algorithmic drift, loose enough to
tolerate a last-ULP quantile difference across CPython patch releases).

Regenerate the constants only on a deliberate formula change and record it
in the corresponding ``docs/MIGRATION.md`` entry. DEC-STAT-DSR-PBO-001
(entry #430).
"""

from __future__ import annotations

import pytest
from brutex.metrics.deflated_sharpe import (
    GAUSSIAN_KURTOSIS,
    deflated_sharpe_ratio,
    expected_max_sharpe_ratio,
    probabilistic_sharpe_ratio,
)

#: Φ(2), the standard-Normal CDF at 2σ (textbook value, full double precision).
_GOLDEN_PHI_2 = 0.9772498680518208
#: expected_max_sharpe_ratio(10, 1.0) — locked from the cited EVT formula.
_GOLDEN_SR0_N10_V1 = 1.57459830134575
#: expected_max_sharpe_ratio(1000, 0.25).
_GOLDEN_SR0_N1000_V025 = 1.6275607568263615
#: deflated_sharpe_ratio(2.0, N=1000, V=0.5, n=250, skew=-0.5, kurt=6.0).
_GOLDEN_DSR_REAL = 0.03596957078527552
_GOLDEN_SR0_REAL = 2.3017184958900594


def test_golden_psr_z_equals_two() -> None:
    """PSR(obs=1, bench=0, n=7, Gaussian) = Φ(2), math-exact."""
    assert probabilistic_sharpe_ratio(1.0, 0.0, 7, 0.0, GAUSSIAN_KURTOSIS) == _GOLDEN_PHI_2


def test_golden_expected_max_sharpe_values() -> None:
    """The extreme-value ``SR_0`` is pinned for two (N, V) pairs."""
    assert expected_max_sharpe_ratio(10, 1.0) == pytest.approx(_GOLDEN_SR0_N10_V1, rel=1e-12)
    assert expected_max_sharpe_ratio(1000, 0.25) == pytest.approx(_GOLDEN_SR0_N1000_V025, rel=1e-12)


def test_golden_deflated_sharpe_single_trial() -> None:
    """N=1 ⇒ no deflation ⇒ DSR = PSR(0) = Φ(2), math-exact."""
    result = deflated_sharpe_ratio(
        1.0,
        n_trials=1,
        trial_sharpe_variance=0.25,
        sample_length=7,
        skew=0.0,
        kurtosis=GAUSSIAN_KURTOSIS,
    )
    assert result.expected_max_sharpe == 0.0
    assert result.deflated_sharpe_ratio == _GOLDEN_PHI_2


def test_golden_deflated_sharpe_realistic() -> None:
    """A realistic deflation pins the full ``(SR_0, DSR)`` pair."""
    result = deflated_sharpe_ratio(
        2.0,
        n_trials=1000,
        trial_sharpe_variance=0.5,
        sample_length=250,
        skew=-0.5,
        kurtosis=6.0,
    )
    assert result.expected_max_sharpe == pytest.approx(_GOLDEN_SR0_REAL, rel=1e-12)
    assert result.deflated_sharpe_ratio == pytest.approx(_GOLDEN_DSR_REAL, rel=1e-12)
