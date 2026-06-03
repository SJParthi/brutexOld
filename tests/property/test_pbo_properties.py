"""Hypothesis property tests for :mod:`brutex.metrics.pbo` — PBO via CSCV.

Invariants that must hold for every valid performance matrix, complementing
the known-value unit tests in ``tests/unit/test_pbo.py``:

1. **Probability range** — the PBO is always a finite value in ``[0, 1]``
   and the diagnostics are self-consistent (``len(logits) == n_combinations
   == C(S, S/2)``).
2. **Oracle parity** — an independent *naive* CSCV reference that recomputes
   each chunk's Sharpe directly from the concatenated rows (no per-block
   pre-aggregation and no ``OOS = total − IS`` shortcut) reproduces the
   production PBO. This is the look-ahead-free numeric oracle pattern of
   ``TRACK1_VERIFICATION_PYRAMID.md`` §6.2 applied to CSCV: the two code
   paths share only the *definition* of the statistic, not its computation.
   Compared with a small tolerance because a genuine algorithmic discrepancy
   moves the PBO by at least ``1 / C(S, S/2)`` (orders of magnitude above
   float summation-order noise).
3. **Scale invariance** — scaling every performance value by a positive
   constant leaves the PBO unchanged (the Sharpe ratio is scale-invariant).
"""

# pyright: reportCallIssue=false
# Rationale (DEC-PYRIGHT-UNMASK-001): the Hypothesis `@st.composite` strategy
# `_matrices` below is called WITHOUT the `draw` argument — Hypothesis injects
# it at runtime. pyright does not model the `@composite` signature transform and
# reports a false "Argument missing for parameter draw" at every `@given(...)`
# site. The strategy is correct (the suite passes). Scoped to this file only;
# `reportCallIssue` stays a hard error everywhere else.
from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import combinations

from brutex.metrics.pbo import pbo_probability
from hypothesis import given
from hypothesis import strategies as st


@st.composite
def _matrices(draw: st.DrawFn) -> tuple[list[list[float]], int]:
    """Draw a rectangular ``(T, N)`` matrix with ``T`` a multiple of ``S``."""
    n_splits = draw(st.sampled_from([2, 4]))
    rows_per_block = draw(st.integers(min_value=2, max_value=4))
    n_obs = n_splits * rows_per_block
    n_strat = draw(st.integers(min_value=2, max_value=5))
    # Integer-valued cells (cast to float): integer sums are exact and
    # order-independent in float64 (values < 2^53), so the block-aggregation
    # path and the naive direct-concatenation path produce bit-identical
    # chunk moments — letting the oracle parity assert exact equality with no
    # summation-order flakiness while still exercising the full CSCV logic.
    cell = st.integers(min_value=-10, max_value=10).map(float)
    row = st.lists(cell, min_size=n_strat, max_size=n_strat)
    matrix = draw(st.lists(row, min_size=n_obs, max_size=n_obs))
    return matrix, n_splits


def _naive_pbo(matrix: Sequence[Sequence[float]], n_splits: int) -> float:
    """Independent CSCV reference: recompute every chunk Sharpe from raw rows."""
    n_obs = len(matrix)
    n_strat = len(matrix[0])
    block_size = n_obs // n_splits
    blocks = [list(range(b * block_size, (b + 1) * block_size)) for b in range(n_splits)]

    def chunk_sharpe(rows: list[int], strat: int) -> float:
        values = [matrix[r][strat] for r in rows]
        count = len(values)
        total = sum(values)
        total_sq = sum(v * v for v in values)
        mean = total / count
        variance = total_sq / count - mean * mean
        if variance <= 0.0:
            return 0.0
        return mean / math.sqrt(variance)

    overfit = 0
    n_combinations = 0
    for is_blocks in combinations(range(n_splits), n_splits // 2):
        is_rows = [r for b in is_blocks for r in blocks[b]]
        oos_rows = [r for b in range(n_splits) if b not in is_blocks for r in blocks[b]]
        is_stat = [chunk_sharpe(is_rows, n) for n in range(n_strat)]
        oos_stat = [chunk_sharpe(oos_rows, n) for n in range(n_strat)]
        n_star = max(range(n_strat), key=is_stat.__getitem__)
        star = oos_stat[n_star]
        less = sum(1 for v in oos_stat if v < star)
        equal = sum(1 for v in oos_stat if v == star)
        rank = less + (equal + 1) / 2.0
        omega = rank / (n_strat + 1)
        if math.log(omega / (1.0 - omega)) <= 0.0:
            overfit += 1
        n_combinations += 1
    return overfit / n_combinations


@given(_matrices())
def test_pbo_is_a_finite_probability(case: tuple[list[list[float]], int]) -> None:
    """The PBO is a finite probability and the diagnostics are consistent."""
    matrix, n_splits = case
    result = pbo_probability(matrix, n_splits=n_splits)
    assert 0.0 <= result.pbo <= 1.0
    assert math.isfinite(result.pbo)
    assert result.n_combinations == math.comb(n_splits, n_splits // 2)
    assert len(result.logits) == result.n_combinations


@given(_matrices())
def test_pbo_matches_naive_reference(case: tuple[list[list[float]], int]) -> None:
    """The production block-aggregation PBO equals the naive CSCV reference."""
    matrix, n_splits = case
    production = pbo_probability(matrix, n_splits=n_splits).pbo
    reference = _naive_pbo(matrix, n_splits)
    assert production == reference


@given(_matrices(), st.sampled_from([0.25, 0.5, 2.0, 4.0, 8.0]))
def test_pbo_scale_invariant(case: tuple[list[list[float]], int], scale: float) -> None:
    """Scaling every performance value by a positive constant preserves the PBO.

    The scale is a power of two so the rescaling is exact in float64 (a pure
    exponent shift): the Sharpe ratio is mathematically scale-invariant, and
    the power-of-two scale lets the test assert *exact* equality rather than
    chasing rounding noise.
    """
    matrix, n_splits = case
    base = pbo_probability(matrix, n_splits=n_splits).pbo
    scaled_matrix = [[value * scale for value in row] for row in matrix]
    scaled = pbo_probability(scaled_matrix, n_splits=n_splits).pbo
    assert scaled == base
