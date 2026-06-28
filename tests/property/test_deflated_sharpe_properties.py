"""Hypothesis property tests for :mod:`brutex.metrics.deflated_sharpe`.

Invariants that must hold for every valid input, complementing the
known-value unit tests in ``tests/unit/test_deflated_sharpe.py``:

1. **Probability range** — the PSR and the DSR are always in ``[0, 1]`` and
   finite (no NaN / inf, per spec line 746).
2. **Deflation never raises confidence** — ``DSR = PSR(SR_0) <= PSR(0)``
   because the deflation benchmark ``SR_0 >= 0``. (This is the rigorous
   statement of "the deflated Sharpe ≤ the raw Sharpe": both DSR and the
   un-deflated probabilistic Sharpe are probabilities, so they are compared
   like-for-like rather than against the raw Sharpe-ratio scalar.)
3. **DSR decreases as the trial count grows** — more trials ⇒ a higher
   expected-maximum benchmark ``SR_0`` ⇒ a lower DSR (monotone non-increasing).
4. **SR_0 grows with the trial count** — ``expected_max_sharpe_ratio`` is
   monotone non-decreasing in ``n_trials`` and always ``>= 0``.
5. **PSR is monotone in the observed Sharpe** and **decreasing in the
   benchmark**.

The input strategies keep the estimator-variance radicand strictly positive
(``skew ∈ [-1, 1]``, ``kurtosis ∈ [5, 15]`` ⇒ radicand ``>= 0.75`` for any
observed Sharpe), so no call hits the undefined-radicand guard.
"""

from __future__ import annotations

import math
from itertools import pairwise

from brutex.metrics.deflated_sharpe import (
    deflated_sharpe_ratio,
    expected_max_sharpe_ratio,
    probabilistic_sharpe_ratio,
)
from hypothesis import given
from hypothesis import strategies as st

_sharpe = st.floats(min_value=-3.0, max_value=3.0, allow_nan=False, allow_infinity=False)
_skew = st.floats(min_value=-1.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_kurt = st.floats(min_value=5.0, max_value=15.0, allow_nan=False, allow_infinity=False)
_n = st.integers(min_value=2, max_value=5000)
_trials = st.integers(min_value=1, max_value=200_000)
_variance = st.floats(min_value=0.0, max_value=5.0, allow_nan=False, allow_infinity=False)


@given(_sharpe, _sharpe, _n, _skew, _kurt)
def test_psr_is_a_finite_probability(
    observed: float, benchmark: float, n: int, skew: float, kurt: float
) -> None:
    """The PSR is always a finite probability in ``[0, 1]``."""
    psr = probabilistic_sharpe_ratio(observed, benchmark, n, skew, kurt)
    assert 0.0 <= psr <= 1.0
    assert math.isfinite(psr)


#: A float that is either an in-domain finite value or one of the three
#: non-finite sentinels. (``st.floats`` cannot combine bounds with
#: ``allow_nan``/``allow_infinity``, so the non-finite values are sampled
#: separately and unioned in.)
_maybe_non_finite = st.one_of(
    _sharpe,
    st.sampled_from([float("nan"), float("inf"), float("-inf")]),
)


@given(_maybe_non_finite, _maybe_non_finite, _maybe_non_finite, _n)
def test_psr_rejects_any_non_finite_moment(
    observed: float, skew: float, kurt: float, n: int
) -> None:
    """A non-finite (NaN/inf) ``observed_sharpe``/``skew``/``kurtosis`` always raises.

    Hypothesis is steered to draw at least one non-finite value among the three;
    such an input must raise ``ValueError`` rather than silently returning NaN
    (DEC-METRICS-NONFINITE-GUARD-001).
    """
    if all(math.isfinite(v) for v in (observed, skew, kurt)):
        return  # in-domain draw — covered by the finite-probability property above
    try:
        probabilistic_sharpe_ratio(observed, 0.0, n, skew, kurt)
    except ValueError:
        return
    raise AssertionError("a non-finite PSR moment must raise ValueError")


@given(_sharpe, _trials, _variance, _n, _skew, _kurt)
def test_dsr_is_a_finite_probability_and_le_undeflated(
    observed: float, trials: int, variance: float, n: int, skew: float, kurt: float
) -> None:
    """The DSR is a finite probability and never exceeds the un-deflated PSR(0)."""
    result = deflated_sharpe_ratio(
        observed,
        n_trials=trials,
        trial_sharpe_variance=variance,
        sample_length=n,
        skew=skew,
        kurtosis=kurt,
    )
    assert 0.0 <= result.deflated_sharpe_ratio <= 1.0
    assert math.isfinite(result.deflated_sharpe_ratio)
    # Deflation never increases confidence (SR_0 >= 0). Tiny float slack.
    assert result.deflated_sharpe_ratio <= result.probabilistic_sharpe_ratio + 1e-12


@given(_sharpe, _variance, _n, _skew, _kurt)
def test_dsr_decreases_as_trials_grow(
    observed: float, variance: float, n: int, skew: float, kurt: float
) -> None:
    """More trials ⇒ a higher benchmark ⇒ a non-increasing DSR."""
    trial_counts = [1, 2, 10, 100, 1000, 10_000, 100_000]
    dsrs = [
        deflated_sharpe_ratio(
            observed,
            n_trials=trials,
            trial_sharpe_variance=variance,
            sample_length=n,
            skew=skew,
            kurtosis=kurt,
        ).deflated_sharpe_ratio
        for trials in trial_counts
    ]
    assert all(b <= a + 1e-12 for a, b in pairwise(dsrs))


@given(_variance)
def test_expected_max_sharpe_grows_and_is_nonnegative(variance: float) -> None:
    """``SR_0`` is monotone non-decreasing in ``n_trials`` and always ``>= 0``."""
    trial_counts = [1, 2, 5, 20, 100, 1000, 50_000]
    sr0s = [expected_max_sharpe_ratio(trials, variance) for trials in trial_counts]
    assert all(sr0 >= 0.0 for sr0 in sr0s)
    assert all(b >= a - 1e-12 for a, b in pairwise(sr0s))


@given(_sharpe, _sharpe, _n, _kurt)
def test_psr_monotone_in_observed_sharpe_when_symmetric(
    observed: float, bump: float, n: int, kurt: float
) -> None:
    """For symmetric returns (zero skew) the PSR is monotone in the observed Sharpe.

    Skew is fixed at ``0`` deliberately: the PSR is NOT globally monotone in the
    observed Sharpe when the returns are skewed, because the Sharpe also enters
    the estimator-variance radicand — ``d(PSR)/d(SR) ∝ (1 − skew·SR/2)`` flips
    sign once ``skew·SR > 2``. That is a genuine property of the estimator (a
    higher Sharpe at the same skew implies a higher estimator variance, which can
    lower the confidence), not a defect, so the monotone claim is asserted only
    for the symmetric case ``skew = 0`` where ``d(PSR)/d(SR) > 0`` always.
    """
    higher = observed + abs(bump)
    psr_low = probabilistic_sharpe_ratio(observed, 0.0, n, 0.0, kurt)
    psr_high = probabilistic_sharpe_ratio(higher, 0.0, n, 0.0, kurt)
    assert psr_high >= psr_low - 1e-12


@given(_sharpe, _n, _skew, _kurt)
def test_psr_decreasing_in_benchmark(observed: float, n: int, skew: float, kurt: float) -> None:
    """Raising the benchmark Sharpe never raises the PSR."""
    psr_low_bench = probabilistic_sharpe_ratio(observed, -1.0, n, skew, kurt)
    psr_high_bench = probabilistic_sharpe_ratio(observed, 1.0, n, skew, kurt)
    assert psr_high_bench <= psr_low_bench + 1e-12
