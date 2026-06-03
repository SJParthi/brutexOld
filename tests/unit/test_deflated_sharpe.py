"""Unit tests for :mod:`brutex.metrics.deflated_sharpe`.

Known-answer assertions (hand-derived or anchored on textbook standard-Normal
values) for the Probabilistic Sharpe Ratio, the expected-maximum Sharpe
``SR_0`` and the Deflated Sharpe Ratio, plus explicit edge-case coverage
(``n_trials == 1``, zero cross-trial variance, undefined-radicand and
short-track raises) so the module reaches 100% line + 100% branch coverage.

Formula sources cited in the module docstring (Bailey & LdP 2012/2014).
"""

from __future__ import annotations

import math
from itertools import pairwise
from statistics import NormalDist

import pytest
from brutex.metrics.deflated_sharpe import (
    GAUSSIAN_KURTOSIS,
    DeflatedSharpeResult,
    deflated_sharpe_ratio,
    expected_max_sharpe_ratio,
    probabilistic_sharpe_ratio,
)

#: Φ(2) to full double precision — the textbook standard-Normal CDF at 2σ.
#: Used as an independent known answer (not recomputed via the impl's path).
_PHI_2 = 0.9772498680518208


# ── Probabilistic Sharpe Ratio ──────────────────────────────────────


def test_psr_known_answer_z_equals_two() -> None:
    """Hand-derived: obs=1, bench=0, n=7, Gaussian → z = √6/√1.5 = 2 → Φ(2).

    radicand = 1 − 0·1 + (3−1)/4·1² = 1.5; z = (1−0)·√(7−1)/√1.5 = √(6/1.5)
    = √4 = 2, so PSR = Φ(2) = 0.9772498680518208.
    """
    psr = probabilistic_sharpe_ratio(1.0, 0.0, 7, 0.0, GAUSSIAN_KURTOSIS)
    assert psr == pytest.approx(_PHI_2, abs=1e-15)


def test_psr_benchmark_equals_observed_is_one_half() -> None:
    """When ``SR* == SR_hat`` the numerator is zero, so PSR = Φ(0) = 0.5 exactly."""
    assert probabilistic_sharpe_ratio(1.3, 1.3, 50, 0.2, 4.0) == 0.5


def test_psr_higher_benchmark_lowers_probability() -> None:
    """Raising the benchmark Sharpe strictly lowers the PSR (CDF is monotone)."""
    low = probabilistic_sharpe_ratio(1.5, 0.0, 100, 0.0, GAUSSIAN_KURTOSIS)
    high = probabilistic_sharpe_ratio(1.5, 1.0, 100, 0.0, GAUSSIAN_KURTOSIS)
    assert low > high


def test_psr_matches_closed_form_with_skew_and_kurtosis() -> None:
    """Cross-check the full radicand against an independent closed-form eval."""
    obs, bench, n, skew, kurt = 1.8, 0.3, 120, -0.6, 7.5
    radicand = 1.0 - skew * obs + (kurt - 1.0) / 4.0 * obs * obs
    expected = NormalDist().cdf((obs - bench) * math.sqrt(n - 1) / math.sqrt(radicand))
    assert probabilistic_sharpe_ratio(obs, bench, n, skew, kurt) == pytest.approx(
        expected, rel=1e-15
    )


def test_psr_rejects_short_track() -> None:
    """A single observation makes ``sqrt(n − 1)`` degenerate — rejected."""
    with pytest.raises(ValueError, match="sample_length must be >= 2"):
        probabilistic_sharpe_ratio(1.0, 0.0, 1, 0.0, GAUSSIAN_KURTOSIS)


def test_psr_rejects_nonpositive_radicand() -> None:
    """An invalid moment combination (radicand ≤ 0) raises rather than NaN."""
    # obs=5, skew=10, kurt=3 → 1 − 50 + 0.5·25 = −36.5 < 0.
    with pytest.raises(ValueError, match="radicand"):
        probabilistic_sharpe_ratio(5.0, 0.0, 10, 10.0, GAUSSIAN_KURTOSIS)


def test_psr_radicand_exactly_zero_raises() -> None:
    """The radicand-≤-0 guard includes the exact-zero boundary."""
    # Choose skew so that 1 − skew·obs + (kurt−1)/4·obs² == 0 with obs=2, kurt=3:
    # 1 − 2·skew + 0.5·4 = 0 → 3 − 2·skew = 0 → skew = 1.5.
    with pytest.raises(ValueError, match="must be > 0"):
        probabilistic_sharpe_ratio(2.0, 0.0, 10, 1.5, GAUSSIAN_KURTOSIS)


# ── Expected maximum Sharpe (SR_0) ──────────────────────────────────


def test_expected_max_single_trial_is_zero() -> None:
    """``N == 1`` ⇒ no selection bias ⇒ ``SR_0 = 0`` (the EVT-formula limit)."""
    assert expected_max_sharpe_ratio(1, 0.5) == 0.0


def test_expected_max_zero_variance_is_zero() -> None:
    """Zero cross-trial Sharpe dispersion ⇒ ``SR_0 = √0·[…] = 0``."""
    assert expected_max_sharpe_ratio(1000, 0.0) == 0.0


def test_expected_max_known_answer() -> None:
    """Golden value for ``N=10, V=1`` locked from the cited EVT formula."""
    assert expected_max_sharpe_ratio(10, 1.0) == pytest.approx(1.57459830134575, rel=1e-12)


def test_expected_max_grows_with_trials() -> None:
    """``SR_0`` is monotonically non-decreasing in the trial count."""
    values = [expected_max_sharpe_ratio(n, 0.7) for n in (1, 2, 5, 50, 500, 5000)]
    assert all(b >= a for a, b in pairwise(values))


def test_expected_max_scales_with_sqrt_variance() -> None:
    """``SR_0 ∝ √V`` — quadrupling the variance doubles ``SR_0``."""
    base = expected_max_sharpe_ratio(100, 1.0)
    quad = expected_max_sharpe_ratio(100, 4.0)
    assert quad == pytest.approx(2.0 * base, rel=1e-12)


def test_expected_max_rejects_zero_trials() -> None:
    """``N < 1`` is rejected."""
    with pytest.raises(ValueError, match="n_trials must be >= 1"):
        expected_max_sharpe_ratio(0, 1.0)


def test_expected_max_rejects_negative_variance() -> None:
    """A negative cross-trial variance is rejected."""
    with pytest.raises(ValueError, match="trial_sharpe_variance must be >= 0"):
        expected_max_sharpe_ratio(10, -0.1)


# ── Deflated Sharpe Ratio ───────────────────────────────────────────


def test_dsr_single_trial_equals_undeflated_psr() -> None:
    """``N == 1`` ⇒ ``SR_0 = 0`` ⇒ DSR == PSR(0); here PSR(0) = Φ(2)."""
    result = deflated_sharpe_ratio(
        1.0,
        n_trials=1,
        trial_sharpe_variance=0.25,
        sample_length=7,
        skew=0.0,
        kurtosis=GAUSSIAN_KURTOSIS,
    )
    assert isinstance(result, DeflatedSharpeResult)
    assert result.expected_max_sharpe == 0.0
    assert result.deflated_sharpe_ratio == pytest.approx(_PHI_2, abs=1e-15)
    assert result.probabilistic_sharpe_ratio == pytest.approx(_PHI_2, abs=1e-15)
    assert result.observed_sharpe == 1.0
    assert result.n_trials == 1


def test_dsr_never_exceeds_undeflated_psr() -> None:
    """Because ``SR_0 ≥ 0``, the DSR ≤ the un-deflated PSR(0)."""
    result = deflated_sharpe_ratio(
        2.0, n_trials=1000, trial_sharpe_variance=0.5, sample_length=250, skew=-0.5, kurtosis=6.0
    )
    assert result.deflated_sharpe_ratio <= result.probabilistic_sharpe_ratio


def test_dsr_known_answer_realistic() -> None:
    """Golden DSR for a realistic deflation locked from the cited formulas."""
    result = deflated_sharpe_ratio(
        2.0, n_trials=1000, trial_sharpe_variance=0.5, sample_length=250, skew=-0.5, kurtosis=6.0
    )
    assert result.expected_max_sharpe == pytest.approx(2.3017184958900594, rel=1e-12)
    assert result.deflated_sharpe_ratio == pytest.approx(0.03596957078527552, rel=1e-9)


def test_dsr_is_a_probability() -> None:
    """The DSR is a probability in ``[0, 1]``."""
    result = deflated_sharpe_ratio(
        0.8, n_trials=200, trial_sharpe_variance=0.3, sample_length=180, skew=0.1, kurtosis=4.0
    )
    assert 0.0 <= result.deflated_sharpe_ratio <= 1.0


def test_dsr_propagates_short_track_error() -> None:
    """A short track propagates the PSR ``ValueError`` through the DSR."""
    with pytest.raises(ValueError, match="sample_length must be >= 2"):
        deflated_sharpe_ratio(
            1.0,
            n_trials=10,
            trial_sharpe_variance=0.2,
            sample_length=1,
            skew=0.0,
            kurtosis=GAUSSIAN_KURTOSIS,
        )
