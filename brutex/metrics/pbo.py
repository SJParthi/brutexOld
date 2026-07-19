"""Probability of Backtest Overfitting (PBO) via CSCV.

Stage-4 statistical-validity metric for ``TRACK1_BACKTEST_SPEC.md``
line 583 (``pbo_probability`` — "Bailey 2017") and Stage-4 pipeline
filter #5 (spec line 405, "PBO via CSCV"). A **pure function**: no I/O, no
global mutable state, fully deterministic (CSCV is an *exhaustive* split
enumeration — there is no bootstrap, hence no random seed), and
dependency-light (standard library only, matching :mod:`brutex.metrics.types`).

**Precision rule (DEC-MONEY-2DP-001, money-only):** the PBO is a
probability and is kept at full ``float`` precision — NEVER rounded.

Formula source (cited verbatim; no invented constants — Golden Rule #1)
-----------------------------------------------------------------------
Bailey, D. H., Borwein, J. M., López de Prado, M. & Zhu, Q. J. (2017),
"The Probability of Backtest Overfitting", *Journal of Computational
Finance* 20(4), 39-69. The Combinatorially-Symmetric Cross-Validation
(CSCV) procedure (§3-4):

1. Arrange the trial performances in a matrix ``M`` of shape ``(T, N)``:
   ``T`` time-ordered performance observations (e.g. per-period returns)
   for each of ``N`` strategy configurations.
2. Partition the ``T`` rows into ``S`` disjoint, equal-size sub-matrices
   (``S`` even).
3. For every way of choosing ``S/2`` sub-matrices as the in-sample (IS)
   set ``J`` — there are ``C(S, S/2)`` of them — the complement ``J_bar``
   is the out-of-sample (OOS) set. For each split ``c``:

   * compute a performance statistic (the Sharpe ratio) for every
     strategy on IS and on OOS;
   * let ``n*`` be the strategy with the best IS performance;
   * let ``w_c`` be the relative rank of ``n*``'s OOS performance among
     all ``N`` strategies, mapped to ``(0, 1)``. The 2017 paper writes the
     relative rank as ``rank / N``; this module uses the de-facto reference
     convention ``rank / (N + 1)`` with **average ranks** for ties (López de
     Prado, *Advances in Financial Machine Learning* 2018, Ch. 11 /
     ``mlfinlab``), which keeps ``w_c`` strictly inside ``(0, 1)`` so the
     logit is always finite (``rank / N`` would put the OOS-best at ``w = 1``
     ⇒ an infinite logit);
   * form the logit ``lambda_c = ln(w_c / (1 - w_c))``.

4. ``PBO = P(lambda_c <= 0)`` — the fraction of splits in which the
   IS-best strategy lands at or below the OOS median. ``PBO`` near
   ``0.5`` means IS selection is no better than chance (severe
   overfitting); near ``0`` means the IS choice generalises out-of-sample.

Performance note (Golden Rule #10 — honest envelope)
----------------------------------------------------
CSCV is inherently ``O(C(S, S/2) * S * N)`` (``C(S, S/2)`` splits ×
``O(S * N)`` per split) plus a one-time ``O(T * N)`` block pre-accumulation —
a Stage-4 batch reduction run *once per leaderboard*, NOT a per-bar /
per-mask hot path, so the O(1) hot-path discipline (CLAUDE.md Rule #5) does
not apply. To keep the
per-split cost independent of ``T``, the first two moments are
pre-accumulated **per sub-matrix once** and combined additively per split
(``O(S * N)`` per split). Both halves are summed *directly* from the
pre-accumulated block moments — the OOS half is deliberately NOT recovered
as ``total - IS``, because that subtraction of two nearly-equal positives
suffers catastrophic cancellation near zero variance and would make the PBO
sensitive to a benign rescaling of the inputs. A future numpy/Rust
vectorisation is a roadmap option if the default ``S = 16`` ever becomes a
bottleneck.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Final

# The slice-6 Rust-statistics seam (DEC-METRICS-RUST-STATS-002). Imports only
# stdlib + structlog at module scope (its engine-flag import is lazy — no
# import-time cycle; the cpcv.py precedent).
from brutex.metrics import rust_stats

__all__ = [
    "PBOResult",
    "pbo_probability",
    "sharpe_from_moments",
]

#: A per-chunk performance statistic expressed from the first two raw
#: moments ``(sum, sum_of_squares, count)`` of a strategy's returns within
#: a chunk. Expressing the statistic from additive moments is what lets
#: CSCV combine sub-matrices in ``O(1)`` per block (see the module
#: performance note). The default is :func:`sharpe_from_moments`.
#:
#: This is a public, explicitly-importable alias
#: (``from brutex.metrics.pbo import MomentEvaluator``) but is intentionally
#: NOT listed in ``__all__``: it is a PEP 695 ``type`` statement, which some
#: static analysers (CodeQL) do not yet model as an ``__all__`` binding, and
#: type aliases are idiomatically not part of the ``import *`` surface anyway.
type MomentEvaluator = Callable[[float, float, int], float]

#: Minimum number of CSCV sub-matrices. ``S`` must be even (so IS and OOS
#: are equal halves) and ``>= 2`` (so at least one split exists).
_MIN_SPLITS: Final[int] = 2

#: Minimum number of strategies. Ranking the IS-best strategy out-of-sample
#: needs at least two columns to rank against.
_MIN_STRATEGIES: Final[int] = 2

#: ``lambda_c <= 0`` is the overfit indicator (equivalently the IS-best
#: strategy ranks at or below the OOS median). Named to avoid a bare
#: magic-value comparison.
_OVERFIT_LOGIT_THRESHOLD: Final[float] = 0.0


def sharpe_from_moments(total: float, total_sq: float, count: int) -> float:
    """Population Sharpe ratio of a return chunk from its first two moments.

    ``mean = total / count`` and ``variance = total_sq / count - mean^2``;
    the Sharpe ratio is ``mean / sqrt(variance)``.

    Parameters
    ----------
    total
        Sum of the chunk's returns.
    total_sq
        Sum of the squared returns.
    count
        Number of returns in the chunk (``>= 1``).

    Returns
    -------
    float
        The Sharpe ratio, or ``0.0`` when the chunk variance is zero (a
        flat return stream carries no risk-adjusted signal — a deliberate
        degenerate-case convention that keeps the rank arithmetic finite).

    Notes
    -----
    The variance uses the additively-combinable ``E[x^2] - E[x]^2`` form so
    that sub-matrix moments can be summed in ``O(1)`` per block. This is a
    *ranking* statistic over ``O(1)``-magnitude returns, where the mild
    cancellation of that form is immaterial; a Welford pass (used by
    :func:`brutex.metrics.compute._accumulate_moments` for the reported
    moments) is unnecessary here.
    """
    mean = total / count
    variance = total_sq / count - mean * mean
    if variance <= 0.0:
        return 0.0
    return mean / math.sqrt(variance)


@dataclass(frozen=True, slots=True)
class PBOResult:
    """The Probability of Backtest Overfitting and its CSCV diagnostics.

    All floats are full precision (never rounded — DEC-MONEY-2DP-001 is
    money-only).

    Attributes
    ----------
    pbo
        The Probability of Backtest Overfitting: the fraction of CSCV
        splits whose logit ``lambda_c <= 0``. In ``[0, 1]``.
    n_splits
        ``S`` — the number of sub-matrices the timeline was partitioned
        into.
    n_combinations
        ``C(S, S/2)`` — the number of symmetric IS/OOS splits evaluated.
    n_strategies
        ``N`` — the number of strategy configurations ranked.
    logits
        The per-split logits ``lambda_c`` (length ``n_combinations``), in
        the deterministic order produced by :func:`itertools.combinations`
        over the sub-matrix indices. Negative entries are the overfit
        splits counted by :attr:`pbo`.
    """

    pbo: float
    n_splits: int
    n_combinations: int
    n_strategies: int
    logits: tuple[float, ...]


def _validate(performance: Sequence[Sequence[float]], n_splits: int) -> tuple[int, int]:
    """Validate the CSCV inputs and return ``(n_observations, n_strategies)``.

    Raises
    ------
    ValueError
        If ``n_splits`` is not an even integer ``>= 2``; the matrix is
        empty; the rows are ragged; there are fewer than two strategies;
        any cell is non-finite (NaN/inf), which would fabricate a finite
        mid-rank; or the row count is not a positive multiple of
        ``n_splits`` (the sub-matrices must be non-empty and of equal
        size).
    """
    if n_splits < _MIN_SPLITS or n_splits % 2 != 0:
        raise ValueError(f"n_splits must be an even integer >= {_MIN_SPLITS}, got {n_splits}")
    n_obs = len(performance)
    if n_obs == 0:
        raise ValueError("performance matrix must have at least one row")
    n_strat = len(performance[0])
    if n_strat < _MIN_STRATEGIES:
        raise ValueError(f"performance matrix must have >= {_MIN_STRATEGIES} strategies (columns)")
    for row_index, row in enumerate(performance):
        if len(row) != n_strat:
            raise ValueError(
                f"performance matrix is ragged: row {row_index} has {len(row)} "
                f"columns, expected {n_strat}"
            )
    for row_index, row in enumerate(performance):
        for col_index, value in enumerate(row):
            if not math.isfinite(value):
                raise ValueError(
                    "performance matrix has a non-finite entry at "
                    f"[{row_index}][{col_index}]: {value!r}; "
                    "CSCV requires finite real per-period returns"
                )
    if n_obs % n_splits != 0:
        raise ValueError(
            f"row count ({n_obs}) must be a positive multiple of n_splits "
            f"({n_splits}) so the sub-matrices are equal-size; trim the "
            f"oldest rows before calling if necessary"
        )
    return n_obs, n_strat


def _block_moments(
    performance: Sequence[Sequence[float]],
    n_splits: int,
    n_obs: int,
    n_strat: int,
) -> tuple[list[list[float]], list[list[float]], int]:
    """Pre-accumulate per-sub-matrix ``(sum, sum_of_squares)`` per strategy.

    Returns ``(block_sum, block_sumsq, block_size)`` where ``block_sum`` and
    ``block_sumsq`` are ``n_splits``-long lists of ``n_strat``-long rows.
    Splitting the contiguous timeline into ``n_splits`` equal blocks is the
    CSCV partition step; doing the moment accumulation once here makes each
    later split ``O(S * N)`` rather than ``O(T * N)``.
    """
    block_size = n_obs // n_splits
    block_sum: list[list[float]] = []
    block_sumsq: list[list[float]] = []
    for block_index in range(n_splits):
        start = block_index * block_size
        sums = [0.0] * n_strat
        sumsqs = [0.0] * n_strat
        for offset in range(block_size):
            row = performance[start + offset]
            for strat_index in range(n_strat):
                value = row[strat_index]
                sums[strat_index] += value
                sumsqs[strat_index] += value * value
        block_sum.append(sums)
        block_sumsq.append(sumsqs)
    return block_sum, block_sumsq, block_size


def _cscv_logits(
    block_sum: list[list[float]],
    block_sumsq: list[list[float]],
    n_splits: int,
    n_strat: int,
    is_count: int,
    oos_count: int,
    denom_rank: int,
    evaluator: MomentEvaluator,
) -> tuple[int, list[float]]:
    """The pure-Python CSCV split loop — the retained-forever parity oracle.

    Moved VERBATIM from :func:`pbo_probability`
    (DEC-METRICS-RUST-STATS-002; zero arithmetic change): one IS/OOS moment
    combination + rank + logit per :func:`itertools.combinations` split.
    Returns ``(overfit_count, logits)``. Serves every call when the
    ``BRUTEX_STATS_RUST`` flag is OFF (the default), every fallback when it
    is ON, and the burn-in self-check compares — never deleted.
    """
    all_blocks = range(n_splits)
    half = n_splits // 2
    logits: list[float] = []
    overfit = 0
    for is_blocks in combinations(all_blocks, half):
        is_set = set(is_blocks)
        oos_blocks = [b for b in all_blocks if b not in is_set]
        is_stat: list[float] = []
        oos_stat: list[float] = []
        for n in range(n_strat):
            is_sum = 0.0
            is_sumsq = 0.0
            for s in is_blocks:
                is_sum += block_sum[s][n]
                is_sumsq += block_sumsq[s][n]
            oos_sum = 0.0
            oos_sumsq = 0.0
            for s in oos_blocks:
                oos_sum += block_sum[s][n]
                oos_sumsq += block_sumsq[s][n]
            is_stat.append(evaluator(is_sum, is_sumsq, is_count))
            oos_stat.append(evaluator(oos_sum, oos_sumsq, oos_count))

        n_star = max(range(n_strat), key=is_stat.__getitem__)
        star_oos = oos_stat[n_star]
        # Average-rank of the IS-best strategy's OOS performance, in one pass:
        # ``less`` strategies rank strictly below it; the ``equal`` ties (incl.
        # itself) share the mid-rank ``less + (equal + 1) / 2`` (1-based).
        less = 0
        equal = 0
        for value in oos_stat:
            if value < star_oos:
                less += 1
            elif value == star_oos:
                equal += 1
        rank = less + (equal + 1) / 2.0
        omega = rank / denom_rank
        lambda_c = math.log(omega / (1.0 - omega))
        logits.append(lambda_c)
        if lambda_c <= _OVERFIT_LOGIT_THRESHOLD:
            overfit += 1
    return overfit, logits


def pbo_probability(
    performance: Sequence[Sequence[float]],
    *,
    n_splits: int = 16,
    evaluator: MomentEvaluator = sharpe_from_moments,
) -> PBOResult:
    """Probability of Backtest Overfitting via CSCV (Bailey et al. 2017).

    Parameters
    ----------
    performance
        The ``(T, N)`` performance matrix: ``performance[t][n]`` is the
        ``t``-th time-ordered performance observation (e.g. a per-period
        return) of strategy ``n``. Must be rectangular with ``N >= 2``
        columns and ``T`` a positive multiple of ``n_splits``.
    n_splits
        ``S`` — the number of equal sub-matrices to partition the timeline
        into. Must be an even integer ``>= 2``. The spec default is ``16``
        (``C(16, 8) = 12,870`` splits; spec line 387).
    evaluator
        The per-chunk performance statistic, expressed from
        ``(sum, sum_of_squares, count)``. Defaults to
        :func:`sharpe_from_moments` (the Sharpe ratio, per the paper).

    Returns
    -------
    PBOResult
        The PBO (``result.pbo``) and the CSCV diagnostics.

    Raises
    ------
    ValueError
        For any invalid input — including a non-finite (NaN/inf) cell;
        see :func:`_validate`.
    """
    n_obs, n_strat = _validate(performance, n_splits)
    block_sum, block_sumsq, block_size = _block_moments(performance, n_splits, n_obs, n_strat)

    half = n_splits // 2
    is_count = half * block_size
    oos_count = is_count  # equal halves
    denom_rank = n_strat + 1

    kernel_result: tuple[int, list[float]] | None = None
    if rust_stats.stats_rust_enabled():
        # The flag-gated Rust CSCV kernel (DEC-METRICS-RUST-STATS-002).
        # ``None`` — latched / wheel-absent / kernel error / refused result /
        # a CUSTOM evaluator / past the combination cap / a gate refusal —
        # falls through to the retained-forever Python loop, so wheel state
        # can never change results or error behaviour (rust_stats envelope).
        kernel_result = rust_stats.pbo_cscv_via_kernel(
            block_sum,
            block_sumsq,
            n_splits=n_splits,
            n_strat=n_strat,
            is_count=is_count,
            n_total=math.comb(n_splits, half),
            evaluator_is_default=evaluator is sharpe_from_moments,
            oracle=lambda: _cscv_logits(
                block_sum,
                block_sumsq,
                n_splits,
                n_strat,
                is_count,
                oos_count,
                denom_rank,
                evaluator,
            ),
        )
    if kernel_result is None:
        overfit, logits = _cscv_logits(
            block_sum,
            block_sumsq,
            n_splits,
            n_strat,
            is_count,
            oos_count,
            denom_rank,
            evaluator,
        )
    else:
        overfit, logits = kernel_result

    n_combinations = len(logits)
    return PBOResult(
        pbo=overfit / n_combinations,
        n_splits=n_splits,
        n_combinations=n_combinations,
        n_strategies=n_strat,
        logits=tuple(logits),
    )
