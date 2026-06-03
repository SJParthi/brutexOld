"""Data-snooping multiple-testing tests for Stage-4 of the brute-force sweep.

Implements the three bootstrap-based tests named in
``docs/TRACK1_BACKTEST_SPEC.md`` §"Stage 4 — Statistical filters" filter
#4 (Reality Check / SPA, spec lines 389-404) and filter #6 (Romano-Wolf
step-down, spec line 406), locked by **DEC-EXTREME-001** and ledgered in
``docs/MIGRATION.md`` entry #424 (**DEC-STAT-SPA-001**):

* :func:`reality_check` — **White's Reality Check** for the null that the
  best strategy in a pool does not beat the benchmark.
* :func:`hansen_spa` — **Hansen's Superior Predictive Ability (SPA)** test,
  the studentized, null-recentred refinement of the Reality Check that
  removes the "irrelevant alternatives" power loss.
* :func:`romano_wolf` — the **Romano-Wolf step-down** procedure, a
  bootstrap multiple-testing correction that returns a per-strategy
  rejection set controlling the familywise error rate (FWER) — far more
  powerful than Bonferroni at the pool sizes Stage 4 sees (spec line 407:
  "USE THIS, NOT Bonferroni").

All three share one **deterministic** block bootstrap of the rows of the
excess-return matrix (Golden Rule #4 — identical input + identical
``seed`` ⇒ identical p-values, bit-for-bit). The rows are resampled
*jointly* across strategies so the cross-sectional dependence that
White/Hansen/Romano-Wolf rely on is preserved.

Conventions
-----------
The input is an ``(n_obs, n_strategies)`` matrix ``d`` of **excess
returns versus a benchmark**: ``d[t, k]`` is strategy ``k``'s return
minus the benchmark's return in period ``t`` (higher is better). The
family null hypothesis tested by Reality Check / SPA is

    H0:  max_k  E[d_k]  <=  0        ("no strategy beats the benchmark"),

and Romano-Wolf tests the ``m`` individual hypotheses ``H_k: E[d_k] <= 0``
simultaneously. All statistics and p-values are full-precision ``float64``
and are **never rounded** — the 2-decimal money rule (DEC-MONEY-2DP-001)
is display-only and is deliberately not imported here.

Numerical references (cite these; no invented critical values — Golden
Rule #1)
-------------------------------------------------------------------------
* White, H. (2000). "A Reality Check for Data Snooping."
  *Econometrica* 68(5):1097-1126.
* Hansen, P. R. (2005). "A Test for Superior Predictive Ability."
  *Journal of Business & Economic Statistics* 23(4):365-380.
* Romano, J. P., & Wolf, M. (2005). "Stepwise Multiple Testing as
  Formalized Data Snooping." *Econometrica* 73(4):1237-1282.
* Romano, J. P., & Wolf, M. (2016). "Efficient computation of adjusted
  p-values for resampling-based stepdown multiple testing."
  *Statistics & Probability Letters* 113:38-40 (the adjusted-p recursion
  implemented in :func:`romano_wolf`).
* Politis, D. N., & Romano, J. P. (1994). "The Stationary Bootstrap."
  *JASA* 89(428):1303-1313 (the default resampling scheme).
* Politis, D. N., & Romano, J. P. (1992). "A circular block-resampling
  procedure for stationary data." (the :attr:`BootstrapMethod.CIRCULAR`
  alternative).

The studentized statistic, the three SPA recentring schemes
(lower/consistent/upper), and the step-down recursion follow these
papers exactly; the open-source ``arch`` package (Sheppard,
``arch.bootstrap.SPA`` / ``StepM``) was used only as a cross-check
oracle, not copied.

Honest envelope (Golden Rule #10)
---------------------------------
A bootstrap test is **inherently** ``O(n_resamples * n_obs *
n_strategies)`` — resampling is not an ``O(1)`` hot path and is never
claimed to be. This is offline Stage-4 code that the spec runs on the
top ~1,000 surviving candidates (spec line 404, "~10-20 minutes"), not
the per-bar engine kernel where the ``O(1)`` discipline (CLAUDE.md rule
#5) applies. The implementation is pure NumPy with bounded memory
(``O(n_obs * n_strategies + n_resamples * n_strategies)``): bootstrap
index vectors are drawn one replication at a time rather than
materialising the full ``(B, n_obs, n_strategies)`` resample tensor.

Size (type-I / FWER) control is **asymptotic**, exactly as the source
papers prove — it is not exact in finite samples. The studentization
uses the self-consistent bootstrap variance estimate
``omega_k^2 = n * Var*[mean d*_k]`` (Hansen 2005; matches the ``arch``
reference). That estimator is mildly downward-biased at small ``n``, so
empirical size/FWER can sit above the nominal level for short series and
converges to nominal as ``n`` grows (measured here: Romano-Wolf FWER on
iid data is ~0.08 at ``n=200`` and ~0.053 at ``n=2000`` for a nominal
0.05). No finite-sample fudge factor is applied — inventing one would
violate Golden Rule #1. Callers needing tight small-sample size should
supply more observations or a longer held-out window (the Stage-4
lockbox already enforces ``>= 500`` trades, spec line 452).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Final

import numpy as np
import numpy.typing as npt

__all__ = [
    "BootstrapConfig",
    "BootstrapMethod",
    "RealityCheckResult",
    "RomanoWolfResult",
    "SPAResult",
    "expected_block_length",
    "hansen_spa",
    "reality_check",
    "romano_wolf",
]

#: Required dimensionality of the input matrix ``(n_obs, n_strategies)``.
_MATRIX_NDIM: Final[int] = 2

#: Minimum number of time observations. Below 2 the centred-mean variance
#: that every test needs is undefined.
_MIN_OBSERVATIONS: Final[int] = 2

#: Smallest ``n`` for which ``log(log(n)) >= 0`` so Hansen's consistent
#: recentring threshold ``sqrt(2 * log log n)`` is real. Below it the
#: threshold degenerates to ``0`` (the lower-variant limit).
_MIN_OBS_FOR_LOGLOG: Final[int] = 3

#: Spec block-length coefficient: ``L = floor(1.75 * n ** (1/3))`` for a
#: daily-frequency Sharpe series (``TRACK1_BACKTEST_SPEC.md`` line 396).
_BLOCK_LENGTH_COEF: Final[float] = 1.75

#: Default bootstrap replications (spec lines 396 + 400: 10,000 resamples).
_DEFAULT_RESAMPLES: Final[int] = 10_000

#: Default FWER level for :func:`romano_wolf` (spec filter #4 pass rule is
#: ``p < 0.05``; the step-down controls FWER at the same conventional 5%).
_DEFAULT_ALPHA: Final[float] = 0.05


class BootstrapMethod(Enum):
    """Block-bootstrap resampling scheme for the row indices.

    Attributes
    ----------
    STATIONARY
        Politis-Romano (1994) stationary bootstrap: geometric block
        lengths with mean ``block_length``, wrapping circularly. This is
        the scheme the spec names for White/Hansen (line 395) and the
        default.
    CIRCULAR
        Politis-Romano (1992) circular block bootstrap: fixed block
        length ``round(block_length)``, wrapping circularly.
    """

    STATIONARY = "stationary"
    CIRCULAR = "circular"


@dataclass(frozen=True, slots=True)
class BootstrapConfig:
    """Deterministic bootstrap configuration.

    ``seed`` is the **first, mandatory** field so a caller can never run a
    test without pinning the RNG (Golden Rule #4 — reproducibility first).

    Parameters
    ----------
    seed
        Non-negative integer seed for :func:`numpy.random.default_rng`.
        Same ``seed`` + same data ⇒ identical bootstrap draws ⇒ identical
        p-values.
    n_resamples
        Number of bootstrap replications ``B`` (default 10,000, the spec
        value). Must be ``>= 1``.
    block_length
        Mean block length for the stationary bootstrap (or the fixed block
        length for the circular bootstrap). ``None`` (default) uses the
        spec formula :func:`expected_block_length`. Must be a finite value
        ``>= 1.0`` when given.
    method
        The :class:`BootstrapMethod` (default :attr:`STATIONARY`).

    Raises
    ------
    ValueError
        If ``seed`` is not an ``int`` or is negative, ``n_resamples < 1``,
        or ``block_length`` is supplied and not a finite value ``>= 1.0``.
    """

    seed: int
    n_resamples: int = _DEFAULT_RESAMPLES
    block_length: float | None = None
    method: BootstrapMethod = BootstrapMethod.STATIONARY

    def __post_init__(self) -> None:
        # ``seed`` is annotated ``int`` and the whole repo is type-checked, but
        # an untyped caller passing a float would otherwise crash deep inside
        # ``default_rng`` with an opaque ``TypeError``; fail fast and clearly.
        if not isinstance(self.seed, int):
            raise ValueError(f"BootstrapConfig.seed must be an int, got {type(self.seed).__name__}")
        if self.seed < 0:
            raise ValueError(f"BootstrapConfig.seed must be >= 0, got {self.seed}")
        if self.n_resamples < 1:
            raise ValueError(f"BootstrapConfig.n_resamples must be >= 1, got {self.n_resamples}")
        # A block can never be shorter than one observation; ``block_length <
        # 1`` would silently collapse the stationary scheme to an iid bootstrap
        # (restart probability ``1/L > 1`` ⇒ every position restarts).
        if self.block_length is not None and not (
            math.isfinite(self.block_length) and self.block_length >= 1.0
        ):
            raise ValueError(
                "BootstrapConfig.block_length must be a finite value >= 1.0 when set "
                f"(a block cannot be shorter than one observation), got {self.block_length}"
            )


@dataclass(frozen=True, slots=True)
class RealityCheckResult:
    """Result of :func:`reality_check`.

    Attributes
    ----------
    pvalue
        White's Reality Check p-value for ``H0: max_k E[d_k] <= 0``. Small
        ⇒ at least one strategy beats the benchmark beyond data-snooping
        luck.
    test_statistic
        The observed statistic ``V = max_k sqrt(n) * mean(d_k)``.
    best_strategy
        Column index of the strategy with the largest sample mean.
    n_strategies, n_observations, n_resamples
        Problem dimensions echoed for provenance.
    block_length
        The mean block length actually used.
    method
        The bootstrap scheme used (``"stationary"`` / ``"circular"``).
    """

    pvalue: float
    test_statistic: float
    best_strategy: int
    n_strategies: int
    n_observations: int
    n_resamples: int
    block_length: float
    method: str


@dataclass(frozen=True, slots=True)
class SPAResult:
    """Result of :func:`hansen_spa`.

    Hansen's SPA produces three p-values that bracket the true value; the
    **consistent** one is the recommended test statistic (Hansen 2005
    §3.2). ``pvalue_lower <= pvalue_consistent <= pvalue_upper`` always
    holds (the upper variant is White's Reality Check applied to the
    studentized statistic).

    Attributes
    ----------
    pvalue_consistent
        The recommended SPA p-value (data-dependent null recentring; bad
        strategies are excluded from the null distribution).
    pvalue_lower
        Liberal bound — only strategies with non-negative sample mean
        enter the null.
    pvalue_upper
        Conservative bound — every strategy enters the null at zero
        (the studentized Reality Check).
    test_statistic
        Observed ``T = max(0, max_k sqrt(n) * mean(d_k) / omega_k)``.
    best_strategy
        Column index of the strategy with the largest sample mean.
    n_strategies, n_observations, n_resamples
        Problem dimensions echoed for provenance.
    block_length
        The mean block length actually used.
    method
        The bootstrap scheme used.
    """

    pvalue_consistent: float
    pvalue_lower: float
    pvalue_upper: float
    test_statistic: float
    best_strategy: int
    n_strategies: int
    n_observations: int
    n_resamples: int
    block_length: float
    method: str

    @property
    def pvalue(self) -> float:
        """Alias for :attr:`pvalue_consistent` — the recommended SPA p-value."""
        return self.pvalue_consistent


@dataclass(frozen=True, slots=True)
class RomanoWolfResult:
    """Result of :func:`romano_wolf`.

    All per-strategy tuples are in the **original column order** of the
    input matrix.

    Attributes
    ----------
    adjusted_pvalues
        Step-down FWER-adjusted p-value for each strategy's hypothesis
        ``H_k: E[d_k] <= 0``. ``H_k`` is rejected at level ``alpha`` iff
        ``adjusted_pvalues[k] <= alpha``.
    naive_pvalues
        Per-strategy bootstrap p-value with **no** multiplicity
        correction (testing each strategy in isolation). Always
        ``<= adjusted_pvalues`` elementwise — the correction is never more
        liberal than the naive test.
    rejected
        ``adjusted_pvalues[k] <= alpha`` per strategy.
    rejected_indices
        Column indices of the rejected strategies (ascending).
    test_statistics
        Observed studentized statistic ``t_k = sqrt(n) * mean(d_k) /
        omega_k`` per strategy.
    alpha
        The FWER level used.
    n_strategies, n_observations, n_resamples
        Problem dimensions echoed for provenance.
    block_length
        The mean block length actually used.
    method
        The bootstrap scheme used.
    """

    adjusted_pvalues: tuple[float, ...]
    naive_pvalues: tuple[float, ...]
    rejected: tuple[bool, ...]
    rejected_indices: tuple[int, ...]
    test_statistics: tuple[float, ...]
    alpha: float
    n_strategies: int
    n_observations: int
    n_resamples: int
    block_length: float
    method: str

    @property
    def n_rejected(self) -> int:
        """Number of strategies whose null hypothesis was rejected."""
        return len(self.rejected_indices)


# --------------------------------------------------------------------------- #
# Validation helpers                                                          #
# --------------------------------------------------------------------------- #
def _validate_matrix(excess_returns: npt.ArrayLike) -> npt.NDArray[np.float64]:
    """Coerce ``excess_returns`` to a validated ``(n_obs, n_strategies)`` float64 array.

    Raises
    ------
    ValueError
        If the array is not 2-D, has fewer than two observations, has no
        strategies, or contains a non-finite value (no NaN/inf —
        ``TRACK1_BACKTEST_SPEC.md`` line 746).
    """
    data = np.asarray(excess_returns, dtype=np.float64)
    if data.ndim != _MATRIX_NDIM:
        raise ValueError(
            f"excess_returns must be 2-D (n_obs, n_strategies); got ndim={data.ndim}. "
            "A single strategy is shape (n_obs, 1)."
        )
    n_obs = int(data.shape[0])
    n_strategies = int(data.shape[1])
    if n_obs < _MIN_OBSERVATIONS:
        raise ValueError(f"excess_returns needs >= {_MIN_OBSERVATIONS} observations, got {n_obs}")
    if n_strategies < 1:
        raise ValueError(f"excess_returns needs >= 1 strategy, got {n_strategies}")
    if not bool(np.all(np.isfinite(data))):
        raise ValueError("excess_returns must be finite (no NaN/inf)")
    return data


def _require_positive_variance(data: npt.NDArray[np.float64]) -> None:
    """Reject constant columns for the studentized tests (SPA, Romano-Wolf).

    A zero-variance column has no sampling variability, so its studentized
    statistic ``sqrt(n) * mean / omega`` is undefined (``finite / 0``).
    Rather than emit ``inf``/``NaN`` we raise a clear error naming the
    offending column(s).
    """
    col_var = data.var(axis=0)
    bad = np.flatnonzero(col_var <= 0.0)
    if bad.size > 0:
        raise ValueError(
            "studentized tests (hansen_spa / romano_wolf) require each strategy "
            f"to have positive variance; constant column(s): {bad.tolist()}"
        )


def _validate_alpha(alpha: float) -> None:
    """Raise ``ValueError`` unless ``0 < alpha < 1``."""
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in the open interval (0, 1), got {alpha}")


# --------------------------------------------------------------------------- #
# Block bootstrap (Politis-Romano)                                            #
# --------------------------------------------------------------------------- #
def expected_block_length(n_obs: int) -> float:
    """Return the spec default mean block length ``floor(1.75 * n ** (1/3))``.

    Parameters
    ----------
    n_obs
        Number of time observations.

    Returns
    -------
    float
        ``floor(1.75 * n_obs ** (1/3))`` floored at ``1.0`` (a block can
        never be shorter than one observation). Matches
        ``TRACK1_BACKTEST_SPEC.md`` line 396.
    """
    return max(1.0, float(math.floor(_BLOCK_LENGTH_COEF * (n_obs ** (1.0 / 3.0)))))


def _stationary_indices(
    n_obs: int, block_length: float, rng: np.random.Generator
) -> npt.NDArray[np.intp]:
    """Draw one stationary-bootstrap index vector (Politis-Romano 1994).

    Each position either continues the current block (probability
    ``1 - 1/L``, advancing circularly) or starts a fresh block at a
    uniform random origin (probability ``1/L``), giving geometric block
    lengths with mean ``L = block_length``.
    """
    restart_prob = 1.0 / block_length
    positions = np.arange(n_obs)
    restart = rng.random(n_obs) < restart_prob
    restart[0] = True  # the first position always opens a fresh block
    fresh_origin = rng.integers(0, n_obs, size=n_obs)
    # For each position, the index of the most recent block-opening position.
    block_open = np.where(restart, positions, -1)
    last_open = np.maximum.accumulate(block_open)
    offset = positions - last_open
    indices = (fresh_origin[last_open] + offset) % n_obs
    return indices.astype(np.intp, copy=False)


def _circular_indices(
    n_obs: int, block_length: float, rng: np.random.Generator
) -> npt.NDArray[np.intp]:
    """Draw one circular block-bootstrap index vector (Politis-Romano 1992).

    Fixed block length ``L = round(block_length)`` clamped to
    ``[1, n_obs - 1]``; enough blocks from uniform random circular origins
    are concatenated to cover ``n_obs`` positions, then truncated. The
    upper clamp matters: a block of length ``>= n_obs`` wraps the entire
    (circular) series, so every resample would have the original sample
    mean — zero bootstrap variance and an undefined studentized statistic.
    Clamping degrades gracefully to the longest non-degenerate block.
    """
    block = max(1, min(round(block_length), n_obs - 1))
    n_blocks = math.ceil(n_obs / block)
    origins = rng.integers(0, n_obs, size=n_blocks)
    offsets = np.arange(block)
    blocks = (origins[:, None] + offsets[None, :]) % n_obs
    return blocks.reshape(-1)[:n_obs].astype(np.intp, copy=False)


def _draw_indices(
    n_obs: int, method: BootstrapMethod, block_length: float, rng: np.random.Generator
) -> npt.NDArray[np.intp]:
    """Dispatch a single bootstrap index draw to the configured scheme."""
    if method is BootstrapMethod.STATIONARY:
        return _stationary_indices(n_obs, block_length, rng)
    return _circular_indices(n_obs, block_length, rng)


def _bootstrap_column_means(
    data: npt.NDArray[np.float64], config: BootstrapConfig
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], float]:
    """Compute the per-replication bootstrap column means.

    The rows are resampled **jointly** across strategies (one shared index
    vector per replication) so the cross-sectional dependence is kept.
    Indices are generated one replication at a time, so peak memory is
    ``O(n_obs * n_strategies + n_resamples * n_strategies)`` rather than
    the full ``(B, n_obs, n_strategies)`` tensor.

    Returns
    -------
    boot_means
        ``(n_resamples, n_strategies)`` matrix of resampled column means.
    sample_mean
        ``(n_strategies,)`` vector of the original column means.
    block_length
        The mean block length used (spec default or the config override).
    """
    n_obs = int(data.shape[0])
    n_strategies = int(data.shape[1])
    block_length = (
        config.block_length if config.block_length is not None else expected_block_length(n_obs)
    )
    rng = np.random.default_rng(config.seed)
    boot_means = np.empty((config.n_resamples, n_strategies), dtype=np.float64)
    for replication in range(config.n_resamples):
        idx = _draw_indices(n_obs, config.method, block_length, rng)
        boot_means[replication] = data[idx].mean(axis=0)
    sample_mean = np.asarray(data.mean(axis=0), dtype=np.float64)
    return boot_means, sample_mean, block_length


def _studentization(
    boot_means: npt.NDArray[np.float64], sample_mean: npt.NDArray[np.float64], n_obs: int
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Return ``(omega, t_obs, z_star)`` for the studentized tests.

    * ``omega_k = sqrt(n * Var*[mean d*_k])`` — the bootstrap estimate of
      the standard deviation of ``sqrt(n) * mean(d_k)`` (Hansen 2005,
      self-consistent with the same block bootstrap).
    * ``t_obs_k = sqrt(n) * mean(d_k) / omega_k`` — observed studentized
      statistic.
    * ``z_star[b, k] = sqrt(n) * (mean d*_{k,b} - mean d_k) / omega_k`` —
      the centred (null-imposed) studentized bootstrap statistics.

    Raises
    ------
    ValueError
        If the bootstrap variance ``omega_k^2`` collapses to zero for any
        column — i.e. every resample reproduced the sample mean, so the
        studentized statistic is undefined. With the inputs validated
        upstream this only arises from a degenerate resampling
        configuration (e.g. a stationary ``block_length`` so large that no
        block restart occurs across the run). The clear error beats
        emitting ``inf``/``NaN`` (spec line 746).
    """
    sqrt_n = math.sqrt(n_obs)
    deviations = boot_means - sample_mean
    omega_sq = n_obs * np.mean(np.square(deviations), axis=0)
    collapsed = np.flatnonzero(omega_sq <= 0.0)
    if collapsed.size > 0:
        raise ValueError(
            "bootstrap variance collapsed to zero for column(s) "
            f"{collapsed.tolist()}: the resampling is degenerate (e.g. a "
            "block_length so large relative to n_obs that every resample "
            "reproduces the full sample). Reduce block_length or add observations."
        )
    omega = np.sqrt(omega_sq)
    t_obs = sqrt_n * sample_mean / omega
    z_star = sqrt_n * deviations / omega
    return omega, t_obs, z_star


def _spa_pvalue(
    z_star: npt.NDArray[np.float64], adjustment: npt.NDArray[np.float64], observed: float
) -> float:
    """Bootstrap p-value of a single SPA recentring variant.

    The recentred bootstrap statistic is ``max(0, max_k (z_star[:, k] +
    adjustment[k]))``; the p-value is the fraction at least as large as the
    observed statistic (Hansen 2005, eq. for ``p^SPA``). The ``>=``
    convention (counting ties against rejection) is the conservative
    choice and is used uniformly across all three tests in this module.
    """
    recentred = z_star + adjustment
    boot_stat = np.maximum(recentred.max(axis=1), 0.0)
    return float(np.mean(boot_stat >= observed))


# --------------------------------------------------------------------------- #
# Public tests                                                                #
# --------------------------------------------------------------------------- #
def reality_check(excess_returns: npt.ArrayLike, config: BootstrapConfig) -> RealityCheckResult:
    """White's (2000) Reality Check for data snooping.

    Tests ``H0: max_k E[d_k] <= 0`` — "the best of the ``m`` strategies
    does not beat the benchmark once best-of-``m`` selection is accounted
    for". The statistic is ``V = max_k sqrt(n) * mean(d_k)`` and its null
    distribution is the stationary-bootstrap distribution of
    ``V* = max_k sqrt(n) * (mean d*_k - mean d_k)`` (the columns recentred
    to the least-favourable configuration ``E[d_k] = 0``). Unlike SPA this
    statistic is **not** studentized, so it tolerates constant columns.

    Parameters
    ----------
    excess_returns
        ``(n_obs, n_strategies)`` matrix of excess returns vs the
        benchmark. A single strategy is shape ``(n_obs, 1)``.
    config
        Deterministic :class:`BootstrapConfig`.

    Returns
    -------
    RealityCheckResult
        The p-value, observed statistic and provenance fields.

    Raises
    ------
    ValueError
        If ``excess_returns`` fails :func:`_validate_matrix`.
    """
    data = _validate_matrix(excess_returns)
    n_obs = int(data.shape[0])
    n_strategies = int(data.shape[1])
    boot_means, sample_mean, block_length = _bootstrap_column_means(data, config)
    sqrt_n = math.sqrt(n_obs)
    observed = float((sqrt_n * sample_mean).max())
    boot_stat = (sqrt_n * (boot_means - sample_mean)).max(axis=1)
    pvalue = float(np.mean(boot_stat >= observed))
    return RealityCheckResult(
        pvalue=pvalue,
        test_statistic=observed,
        best_strategy=int(np.argmax(sample_mean)),
        n_strategies=n_strategies,
        n_observations=n_obs,
        n_resamples=config.n_resamples,
        block_length=block_length,
        method=config.method.value,
    )


def hansen_spa(excess_returns: npt.ArrayLike, config: BootstrapConfig) -> SPAResult:
    """Hansen's (2005) test for Superior Predictive Ability (SPA).

    The studentized, null-recentred refinement of the Reality Check. The
    observed statistic is ``T = max(0, max_k sqrt(n) * mean(d_k) /
    omega_k)`` where ``omega_k`` is the bootstrap standard deviation of
    ``sqrt(n) * mean(d_k)``. Three null-recentring schemes are evaluated,
    differing only in which under-performing strategies are excluded from
    the null distribution (Hansen 2005 §3.2):

    * **lower** — exclude every strategy with negative sample mean;
    * **consistent** — exclude only strategies worse than
      ``-sqrt(2 * omega_k^2 * log log n / n)`` (the recommended,
      consistent test);
    * **upper** — exclude none (the studentized Reality Check).

    The three obey ``pvalue_lower <= pvalue_consistent <= pvalue_upper``.
    Pass rule per spec line 401: ``pvalue_consistent < 0.05`` (one-sided).

    Parameters
    ----------
    excess_returns
        ``(n_obs, n_strategies)`` matrix of excess returns vs the
        benchmark. Every column must have positive variance.
    config
        Deterministic :class:`BootstrapConfig`.

    Returns
    -------
    SPAResult
        The three p-values, observed statistic and provenance fields.

    Raises
    ------
    ValueError
        If the matrix fails :func:`_validate_matrix` or any column is
        constant (:func:`_require_positive_variance`).
    """
    data = _validate_matrix(excess_returns)
    _require_positive_variance(data)
    n_obs = int(data.shape[0])
    n_strategies = int(data.shape[1])
    boot_means, sample_mean, block_length = _bootstrap_column_means(data, config)
    _omega, t_obs, z_star = _studentization(boot_means, sample_mean, n_obs)
    observed = max(0.0, float(t_obs.max()))

    # Hansen (2005) §3.2 recentring thresholds, expressed via the observed
    # studentized statistic t_obs. The consistent threshold needs
    # log(log(n)) >= 0 (i.e. n >= 3); below that it degenerates to the
    # lower variant's zero threshold, which is the correct small-sample limit.
    consistent_threshold = (
        math.sqrt(2.0 * math.log(math.log(n_obs))) if n_obs >= _MIN_OBS_FOR_LOGLOG else 0.0
    )
    adj_upper = np.zeros(n_strategies, dtype=np.float64)
    adj_lower = np.minimum(t_obs, 0.0)
    adj_consistent = np.where(t_obs < -consistent_threshold, t_obs, 0.0)

    return SPAResult(
        pvalue_consistent=_spa_pvalue(z_star, adj_consistent, observed),
        pvalue_lower=_spa_pvalue(z_star, adj_lower, observed),
        pvalue_upper=_spa_pvalue(z_star, adj_upper, observed),
        test_statistic=observed,
        best_strategy=int(np.argmax(sample_mean)),
        n_strategies=n_strategies,
        n_observations=n_obs,
        n_resamples=config.n_resamples,
        block_length=block_length,
        method=config.method.value,
    )


def romano_wolf(
    excess_returns: npt.ArrayLike,
    config: BootstrapConfig,
    alpha: float = _DEFAULT_ALPHA,
) -> RomanoWolfResult:
    """Romano-Wolf (2005) step-down multiple-testing correction.

    Tests the ``m`` hypotheses ``H_k: E[d_k] <= 0`` simultaneously while
    controlling the familywise error rate (FWER) at ``alpha``, using the
    bootstrap to capture cross-strategy dependence (far more powerful than
    Bonferroni at large ``m`` — spec line 407). The studentized statistics
    ``t_k`` are ordered descending; at each step the critical null
    distribution is the bootstrap max over the still-active strategies, so
    once the strongest strategies are rejected the remaining ones are
    judged against a smaller (more powerful) maximum.

    The per-strategy **adjusted p-values** follow the efficient recursion
    of Romano-Wolf (2016): for the ``r``-th strongest strategy,
    ``p_adj = max(previous p_adj, mean_b[ max_{active} z*_b >= t_(r) ])``,
    the running maximum enforcing monotonicity along the step-down order.
    ``H_k`` is rejected iff ``adjusted_pvalues[k] <= alpha``.

    Parameters
    ----------
    excess_returns
        ``(n_obs, n_strategies)`` matrix of excess returns vs the
        benchmark. Every column must have positive variance.
    config
        Deterministic :class:`BootstrapConfig`.
    alpha
        FWER level in ``(0, 1)`` (default 0.05).

    Returns
    -------
    RomanoWolfResult
        Adjusted + naive p-values, the rejection set and provenance.

    Raises
    ------
    ValueError
        If ``alpha`` is out of range, the matrix fails
        :func:`_validate_matrix`, or any column is constant.
    """
    _validate_alpha(alpha)
    data = _validate_matrix(excess_returns)
    _require_positive_variance(data)
    n_obs = int(data.shape[0])
    n_strategies = int(data.shape[1])
    boot_means, sample_mean, block_length = _bootstrap_column_means(data, config)
    _omega, t_obs, z_star = _studentization(boot_means, sample_mean, n_obs)

    # Naive (unadjusted) per-strategy bootstrap p-values.
    naive = np.mean(z_star >= t_obs, axis=0)

    # Step-down over strategies ordered by descending observed statistic.
    # Vectorized in O(B * m): for each step the null distribution is the
    # bootstrap max over the still-active (lower-ranked) strategies, which is
    # a right-to-left running maximum of the stat-ordered bootstrap columns
    # (``suffix_max[:, r] = max over ranks r..m-1``). This replaces the naive
    # O(B * m^2) re-max-over-active-set loop with a single cumulative scan.
    order = np.argsort(-t_obs, kind="stable")
    z_ordered = z_star[:, order]
    suffix_max = np.maximum.accumulate(z_ordered[:, ::-1], axis=1)[:, ::-1]
    t_ordered = t_obs[order]
    raw_in_order = np.mean(suffix_max >= t_ordered, axis=0)
    # Monotone non-decreasing down the step-down order (Romano-Wolf 2016).
    adjusted_in_order = np.maximum.accumulate(raw_in_order)
    adjusted = np.empty(n_strategies, dtype=np.float64)
    adjusted[order] = adjusted_in_order

    rejected = adjusted <= alpha
    rejected_indices = tuple(int(i) for i in np.flatnonzero(rejected))
    return RomanoWolfResult(
        adjusted_pvalues=tuple(float(p) for p in adjusted),
        naive_pvalues=tuple(float(p) for p in naive),
        rejected=tuple(bool(r) for r in rejected),
        rejected_indices=rejected_indices,
        test_statistics=tuple(float(t) for t in t_obs),
        alpha=alpha,
        n_strategies=n_strategies,
        n_observations=n_obs,
        n_resamples=config.n_resamples,
        block_length=block_length,
        method=config.method.value,
    )
