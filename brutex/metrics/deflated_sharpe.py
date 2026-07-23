"""Deflated Sharpe Ratio (DSR) and Probabilistic Sharpe Ratio (PSR).

Stage-4 statistical-validity metrics for ``TRACK1_BACKTEST_SPEC.md``
line 582 (``deflated_sharpe_ratio`` — "Bailey & LdP 2014"). These are
**pure functions**: no I/O, no global mutable state, fully deterministic
(closed-form — no bootstrap, so no seed is required), and dependency-light
(standard library + the stdlib-and-structlog-only
:mod:`brutex.metrics.rust_stats` seam, matching the deliberate
no-``polars``/no-``numpy`` design of :mod:`brutex.metrics.types`).

**Rust seam (DEC-METRICS-RUST-STATS-002):** behind the default-OFF
``BRUTEX_STATS_RUST`` flag the two computation TAILS (:func:`_psr_tail` +
:func:`_expected_max_tail`) are served by the ``fno_math`` kernels
(``stats_psr`` / ``stats_expected_max_sharpe``) through the per-function
burn-in + autorevert machinery of :mod:`brutex.metrics.rust_stats`; every
validation raise stays HERE (byte-pinned messages), the tails are RETAINED
FOREVER as the parity oracles, and flag-OFF is byte-identical by
construction.

**Precision rule (DEC-MONEY-2DP-001, money-only):** every value here is a
statistical metric (a probability or a Sharpe ratio) and is therefore kept
at full ``float`` precision and NEVER rounded — the 2-decimal rule applies
to money display only.

Formula sources (cited verbatim; no invented constants — Golden Rule #1)
------------------------------------------------------------------------
* **PSR** — Bailey, D. H. & López de Prado, M. (2012), "The Sharpe Ratio
  Efficient Frontier", *Journal of Risk* 15(2), 3-44. The probability that
  the true Sharpe ratio exceeds a benchmark ``SR*``, accounting for track
  length, skewness and kurtosis::

      PSR(SR*) = Z[ (SR_hat - SR*) * sqrt(n - 1)
                    / sqrt(1 - g3 * SR_hat + (g4 - 1) / 4 * SR_hat^2) ]

  where ``Z[.]`` is the standard-Normal CDF, ``SR_hat`` is the observed
  (non-annualised) Sharpe ratio measured on the same frequency as the
  ``n`` returns, ``g3`` is the skewness and ``g4`` is the **non-excess**
  kurtosis of the returns (``g4 = 3`` for a Gaussian). The radicand is the
  asymptotic variance of the Sharpe-ratio estimator (Mertens 2002;
  Lo 2002, "The Statistics of Sharpe Ratios", *FAJ* 58(4)).

* **DSR** — Bailey, D. H. & López de Prado, M. (2014), "The Deflated Sharpe
  Ratio: Correcting for Selection Bias, Backtest Overfitting, and
  Non-Normality", *Journal of Portfolio Management* 40(5), 94-107. The DSR
  is the PSR evaluated against the **expected maximum Sharpe ratio**
  ``SR_0`` produced by ``N`` independent trials under the null
  ``true SR = 0``::

      DSR = PSR(SR_0)

      SR_0 = sqrt(V) * [ (1 - gamma) * Z^-1(1 - 1/N)
                         + gamma     * Z^-1(1 - 1/(N * e)) ]

  where ``V = Var[{SR_hat_i}]`` is the variance of the estimated Sharpe
  ratios across the ``N`` trials, ``gamma`` is the Euler-Mascheroni
  constant, ``Z^-1[.]`` is the standard-Normal quantile (inverse CDF) and
  ``e`` is Euler's number. ``SR_0`` is the Gumbel / extreme-value-theory
  approximation of the expected maximum of ``N`` i.i.d. zero-mean Gaussian
  draws with variance ``V`` (Bailey & LdP 2014, eq. for ``E[max_N]``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Final

# The slice-6 Rust-statistics seam (DEC-METRICS-RUST-STATS-002). Imports only
# stdlib + structlog at module scope (its engine-flag import is lazy — no
# import-time cycle; the cpcv.py precedent).
from brutex.metrics import rust_stats

__all__ = [
    "GAUSSIAN_KURTOSIS",
    "DeflatedSharpeResult",
    "deflated_sharpe_ratio",
    "expected_max_sharpe_ratio",
    "probabilistic_sharpe_ratio",
]

#: Standard Normal used for both the CDF ``Z[.]`` and the quantile
#: ``Z^-1[.]``. ``NormalDist`` is exact to double precision and is part of
#: the standard library (Python >= 3.8), so no third-party numerics dep is
#: pulled into the dependency-light :mod:`brutex.metrics` package.
_STD_NORMAL: Final[NormalDist] = NormalDist()

#: Euler-Mascheroni constant ``gamma`` used in the expected-maximum-Sharpe
#: (``SR_0``) extreme-value approximation. Value per Bailey & LdP 2014 /
#: standard mathematical tables (``math`` has no built-in for it).
_EULER_MASCHERONI: Final[float] = 0.5772156649015329

#: Non-excess kurtosis of a Gaussian (``g4 = 3``). The PSR radicand uses the
#: *raw* (non-excess) kurtosis; callers holding **excess** kurtosis (the
#: convention of :func:`brutex.metrics.compute._excess_kurtosis`) must add
#: this baseline back before calling.
GAUSSIAN_KURTOSIS: Final[float] = 3.0

#: Minimum track length: ``sqrt(n - 1)`` requires ``n >= 2`` to be real and
#: non-zero, so the PSR is undefined for a single observation.
_MIN_SAMPLE_LENGTH: Final[int] = 2


@dataclass(frozen=True, slots=True)
class DeflatedSharpeResult:
    """The Deflated Sharpe Ratio and its supporting quantities.

    All fields are full-precision floats (never rounded — DEC-MONEY-2DP-001
    is money-only).

    Attributes
    ----------
    observed_sharpe
        The observed (non-annualised) Sharpe ratio ``SR_hat`` that was
        deflated.
    n_trials
        The number of independent strategy configurations tried, ``N``.
    expected_max_sharpe
        ``SR_0`` — the benchmark the observed Sharpe is deflated against:
        the expected maximum Sharpe ratio of ``N`` zero-edge trials
        (Bailey & LdP 2014). ``>= 0``; grows with ``n_trials``.
    deflated_sharpe_ratio
        The DSR itself: ``PSR(SR_0)``, the probability (in ``[0, 1]``) that
        the strategy's true Sharpe ratio is positive *after* correcting for
        selection across ``n_trials`` and for the return distribution's
        skewness / kurtosis.
    probabilistic_sharpe_ratio
        ``PSR(0)`` — the *un-deflated* probabilistic Sharpe ratio (benchmark
        ``SR* = 0``). Provided for reference; because ``SR_0 >= 0`` the DSR
        never exceeds it (``deflated_sharpe_ratio <= probabilistic_sharpe_ratio``).
    """

    observed_sharpe: float
    n_trials: int
    expected_max_sharpe: float
    deflated_sharpe_ratio: float
    probabilistic_sharpe_ratio: float


def probabilistic_sharpe_ratio(
    observed_sharpe: float,
    benchmark_sharpe: float,
    sample_length: int,
    skew: float,
    kurtosis: float,
) -> float:
    """Probabilistic Sharpe Ratio ``PSR(SR*)`` (Bailey & LdP 2012).

    The probability that the true Sharpe ratio exceeds ``benchmark_sharpe``,
    given the observed Sharpe, track length, and return skewness/kurtosis::

        PSR = Z[ (SR_hat - SR*) * sqrt(n - 1)
                 / sqrt(1 - g3 * SR_hat + (g4 - 1) / 4 * SR_hat^2) ]

    Parameters
    ----------
    observed_sharpe
        ``SR_hat`` — the observed, non-annualised Sharpe ratio.
    benchmark_sharpe
        ``SR*`` — the benchmark Sharpe ratio to test against (``0`` for the
        plain "is the edge positive?" question; ``SR_0`` for the DSR).
    sample_length
        ``n`` — the number of return observations. Must be ``>= 2``.
    skew
        ``g3`` — the skewness of the return series.
    kurtosis
        ``g4`` — the **non-excess** kurtosis of the return series
        (``3`` for a Gaussian; add :data:`GAUSSIAN_KURTOSIS` to an excess
        kurtosis before passing it here).

    Returns
    -------
    float
        The PSR, a probability in ``[0, 1]`` (full precision, not rounded).

    Raises
    ------
    ValueError
        If ``sample_length < 2``; any of ``observed_sharpe``,
        ``benchmark_sharpe``, ``skew`` or ``kurtosis`` is non-finite
        (NaN/inf), which would otherwise bypass the radicand guard and
        silently return ``NaN`` (per spec line 746 "No NaN/inf in result
        rows"); or the estimator-variance radicand
        ``1 - g3 * SR_hat + (g4 - 1) / 4 * SR_hat^2`` is not strictly
        positive (an invalid moment combination for which the PSR is
        undefined — surfaced rather than silently returning ``NaN``,
        per spec line 746 "No NaN/inf in result rows").
    """
    if sample_length < _MIN_SAMPLE_LENGTH:
        raise ValueError(f"sample_length must be >= {_MIN_SAMPLE_LENGTH}, got {sample_length}")
    if not all(
        math.isfinite(value) for value in (observed_sharpe, benchmark_sharpe, skew, kurtosis)
    ):
        raise ValueError(
            "PSR inputs must be finite; got "
            f"observed_sharpe={observed_sharpe!r}, "
            f"benchmark_sharpe={benchmark_sharpe!r}, "
            f"skew={skew!r}, kurtosis={kurtosis!r}"
        )
    radicand = (
        1.0 - skew * observed_sharpe + (kurtosis - 1.0) / 4.0 * observed_sharpe * observed_sharpe
    )
    if radicand <= 0.0:
        raise ValueError(
            "Sharpe-estimator variance radicand "
            "(1 - skew*SR + (kurtosis-1)/4*SR^2) must be > 0; "
            f"got {radicand} for observed_sharpe={observed_sharpe}, "
            f"skew={skew}, kurtosis={kurtosis}"
        )
    if rust_stats.stats_rust_enabled():
        # The flag-gated Rust PSR kernel (DEC-METRICS-RUST-STATS-002).
        # ``None`` — latched / wheel-absent / kernel error / refused result /
        # an argument failing the exact-type+exactness domain gates — falls
        # through to the retained-forever Python tail, so wheel state can
        # never change results or error behaviour (the rust_stats envelope).
        result = rust_stats.psr_via_kernel(
            observed_sharpe,
            benchmark_sharpe,
            sample_length,
            skew,
            kurtosis,
            oracle=lambda: _psr_tail(observed_sharpe, benchmark_sharpe, sample_length, radicand),
        )
        if result is not None:
            return result
    return _psr_tail(observed_sharpe, benchmark_sharpe, sample_length, radicand)


def _psr_tail(
    observed_sharpe: float,
    benchmark_sharpe: float,
    sample_length: int,
    radicand: float,
) -> float:
    """The pure-Python PSR tail — the retained-forever parity oracle.

    Moved VERBATIM from :func:`probabilistic_sharpe_ratio`
    (DEC-METRICS-RUST-STATS-002; zero arithmetic change): the z-score over
    the pre-validated radicand, then the standard-Normal CDF. Serves every
    call when ``BRUTEX_STATS_RUST`` is OFF (the default), every fallback
    when it is ON, and the burn-in self-check compares — never deleted.
    """
    z = (observed_sharpe - benchmark_sharpe) * math.sqrt(sample_length - 1) / math.sqrt(radicand)
    return _STD_NORMAL.cdf(z)


def expected_max_sharpe_ratio(n_trials: int, trial_sharpe_variance: float) -> float:
    """Expected maximum Sharpe ratio ``SR_0`` of ``N`` zero-edge trials.

    The Gumbel / extreme-value approximation of the expected maximum of
    ``N`` i.i.d. zero-mean Gaussian Sharpe estimates with cross-trial
    variance ``V`` (Bailey & LdP 2014)::

        SR_0 = sqrt(V) * [ (1 - gamma) * Z^-1(1 - 1/N)
                           + gamma     * Z^-1(1 - 1/(N * e)) ]

    Parameters
    ----------
    n_trials
        ``N`` — the number of independent strategy configurations tried.
        Must be ``>= 1``. For ``N == 1`` there is no selection bias, so the
        expected maximum of a single zero-mean draw is ``0.0`` (the formula
        above is singular at ``N == 1`` because ``Z^-1(0) = -inf``; the
        mathematically correct limit ``0.0`` is returned instead).
    trial_sharpe_variance
        ``V`` — the variance of the estimated Sharpe ratios *across the
        trials*. Must be ``>= 0``.

    Returns
    -------
    float
        ``SR_0 >= 0`` (full precision, not rounded). Monotonically
        non-decreasing in ``n_trials``.

    Raises
    ------
    ValueError
        If ``n_trials < 1`` or ``trial_sharpe_variance < 0``.

    Notes
    -----
    For an *astronomically* large ``n_trials`` (``>= ~2e16``, far above
    any realistic brute-force trial count) the quantile argument
    ``1 - 1/N`` rounds to exactly ``1.0`` in float64, and
    :meth:`statistics.NormalDist.inv_cdf` then raises
    :class:`statistics.StatisticsError` (a :class:`ValueError` subclass).
    This is documented, not guarded against: such a count is unreachable
    in practice, and callers already catch :class:`ValueError` (this is
    the same exception type the explicit domain guards above raise).
    """
    if n_trials < 1:
        raise ValueError(f"n_trials must be >= 1, got {n_trials}")
    if trial_sharpe_variance < 0.0:
        raise ValueError(f"trial_sharpe_variance must be >= 0, got {trial_sharpe_variance}")
    if n_trials == 1:
        # E[max] of a single zero-mean draw is its mean = 0 (no deflation).
        return 0.0
    if rust_stats.stats_rust_enabled():
        # The flag-gated Rust SR_0 kernel (DEC-METRICS-RUST-STATS-002).
        # A kernel ValueError from the AS241 raise domain refuses to the
        # oracle, which raises the identical StatisticsError (the documented
        # astronomical-``n_trials`` arm) — exception parity by construction.
        result = rust_stats.expected_max_sharpe_via_kernel(
            n_trials,
            trial_sharpe_variance,
            oracle=lambda: _expected_max_tail(n_trials, trial_sharpe_variance),
        )
        if result is not None:
            return result
    return _expected_max_tail(n_trials, trial_sharpe_variance)


def _expected_max_tail(n_trials: int, trial_sharpe_variance: float) -> float:
    """The pure-Python ``SR_0`` tail — the retained-forever parity oracle.

    Moved VERBATIM from :func:`expected_max_sharpe_ratio`
    (DEC-METRICS-RUST-STATS-002; zero arithmetic change): the two AS241
    standard-Normal quantiles + the Gumbel combination over pre-validated
    inputs (``n_trials >= 2`` — the ``== 1`` limit returns earlier). Serves
    every call when ``BRUTEX_STATS_RUST`` is OFF (the default), every
    fallback when it is ON, and the burn-in self-check compares — never
    deleted.
    """
    std = math.sqrt(trial_sharpe_variance)
    quantile_main = _STD_NORMAL.inv_cdf(1.0 - 1.0 / n_trials)
    quantile_tail = _STD_NORMAL.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return std * ((1.0 - _EULER_MASCHERONI) * quantile_main + _EULER_MASCHERONI * quantile_tail)


def deflated_sharpe_ratio(
    observed_sharpe: float,
    *,
    n_trials: int,
    trial_sharpe_variance: float,
    sample_length: int,
    skew: float,
    kurtosis: float,
) -> DeflatedSharpeResult:
    """Deflated Sharpe Ratio ``DSR = PSR(SR_0)`` (Bailey & LdP 2014).

    Combines :func:`expected_max_sharpe_ratio` (the deflation benchmark
    ``SR_0``) with :func:`probabilistic_sharpe_ratio` (evaluated at
    ``SR_0``). The result also carries the un-deflated ``PSR(0)`` for
    reference.

    Parameters
    ----------
    observed_sharpe
        ``SR_hat`` — the observed, non-annualised Sharpe ratio of the
        candidate strategy.
    n_trials
        ``N`` — the number of independent strategy configurations tried
        (the multiple-testing count this Sharpe is deflated against).
        Must be ``>= 1``.
    trial_sharpe_variance
        ``V`` — the variance of the estimated Sharpe ratios across the
        ``N`` trials. Must be ``>= 0``.
    sample_length
        ``n`` — the number of return observations in the candidate's track
        record. Must be ``>= 2``.
    skew
        ``g3`` — the skewness of the candidate's return series.
    kurtosis
        ``g4`` — the **non-excess** kurtosis of the candidate's return
        series (``3`` for a Gaussian; add :data:`GAUSSIAN_KURTOSIS` to an
        excess kurtosis before passing it here).

    Returns
    -------
    DeflatedSharpeResult
        ``expected_max_sharpe`` (``SR_0``), ``deflated_sharpe_ratio`` (the
        DSR probability in ``[0, 1]``) and ``probabilistic_sharpe_ratio``
        (``PSR(0)``).

    Raises
    ------
    ValueError
        Propagated from :func:`expected_max_sharpe_ratio` (``n_trials`` /
        ``trial_sharpe_variance`` domain) or :func:`probabilistic_sharpe_ratio`
        (``sample_length`` / radicand domain).
    """
    sr_zero = expected_max_sharpe_ratio(n_trials, trial_sharpe_variance)
    dsr = probabilistic_sharpe_ratio(observed_sharpe, sr_zero, sample_length, skew, kurtosis)
    psr = probabilistic_sharpe_ratio(observed_sharpe, 0.0, sample_length, skew, kurtosis)
    return DeflatedSharpeResult(
        observed_sharpe=observed_sharpe,
        n_trials=n_trials,
        expected_max_sharpe=sr_zero,
        deflated_sharpe_ratio=dsr,
        probabilistic_sharpe_ratio=psr,
    )
