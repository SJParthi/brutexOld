"""Monte-Carlo permutation final guard — Stage-4 filter #9.

Implements the Monte-Carlo permutation test of ``docs/TRACK1_BACKTEST_SPEC.md``
§"Stage 4 — Statistical filters" item #9 (spec lines 464-480), added by
**DEC-EXTREME-001** and ledgered under **DEC-STAT-LOCKBOX-MC-001**.

The question it answers (spec lines 473-478): *"is the ENTRY signal actually
informative, or could random entries with this same exit logic look just as
good?"* — the failure mode where a SL/TP grid's exit logic does all the work
and the entry mask is noise. It is the only test that directly answers this.

How it works (spec lines 466-472)
---------------------------------
The real strategy's statistic (its Sharpe) is compared against a null
distribution built by **randomly relocating the entry signals** ``B`` times
(default 1,000) and re-running the *same* exit logic each time. The exit
logic is **injected** as the ``evaluate`` callable: given a set of entry-bar
positions it returns the resulting statistic. Keeping the exit logic external
makes this module pure, deterministic, and decoupled from the (separate) trade
simulator — exactly the seam the spec describes ("re-run the same exit logic").

Pass criterion (spec lines 471-472): the real statistic must beat ``>= 99%``
of the permuted statistics — i.e. a one-sided upper-tail permutation p-value
``< alpha`` (default 0.01).

The p-value uses the **bias-corrected** estimator
``(1 + #{perm >= observed}) / (B + 1)`` (Phipson & Smyth 2010, *"Permutation
P-values Should Never Be Zero"*, Stat. Appl. Genet. Mol. Biol. 9(1)) — the
standard, valid permutation p-value that can never be exactly 0; no invented
constant. Determinism is guaranteed by the mandatory explicit ``seed``
(Golden Rule #4): same inputs + same ``evaluate`` + same ``seed`` ⇒ identical
p-value, bit-for-bit.

Honest envelope (Golden Rule #10)
---------------------------------
Cost is ``B`` calls to ``evaluate`` (``O(B)`` plus whatever ``evaluate``
costs) — an offline Stage-4 batch run once per surviving candidate (spec:
~1,000 shuffles × top 10 candidates), NOT an ``O(1)`` per-bar hot path. The
test only re-locates entries and re-scores; it does not own the exit logic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np

if TYPE_CHECKING:
    import numpy.typing as npt

__all__ = [
    "MonteCarloResult",
    "monte_carlo_permutation_test",
]

#: Default number of entry-signal permutations (spec line 467: 1,000 shuffles).
_DEFAULT_PERMUTATIONS: Final[int] = 1000

#: Default one-sided upper-tail significance level (spec line 472: p < 0.01).
_DEFAULT_ALPHA: Final[float] = 0.01


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    """Result of :func:`monte_carlo_permutation_test`.

    Attributes
    ----------
    pvalue
        One-sided upper-tail permutation p-value
        ``(1 + n_beaten) / (n_permutations + 1)``. Small ⇒ the real entry
        signal carries information beyond the exit logic.
    passed
        ``True`` iff ``pvalue < alpha`` (the real statistic beats
        ``>= (1 - alpha)`` of the permutations).
    observed_statistic
        The real strategy's statistic (e.g. its Sharpe).
    n_beaten
        Number of permutations whose statistic was ``>= observed_statistic``.
    n_permutations
        Number of permutations drawn (``B``).
    alpha
        The significance level used.
    n_bars, n_entries
        Problem dimensions echoed for provenance: bars in the test period and
        the number of entry signals relocated each permutation.
    """

    pvalue: float
    passed: bool
    observed_statistic: float
    n_beaten: int
    n_permutations: int
    alpha: float
    n_bars: int
    n_entries: int


def monte_carlo_permutation_test(
    observed_statistic: float,
    n_bars: int,
    n_entries: int,
    evaluate: Callable[[npt.NDArray[np.intp]], float],
    *,
    seed: int,
    n_permutations: int = _DEFAULT_PERMUTATIONS,
    alpha: float = _DEFAULT_ALPHA,
) -> MonteCarloResult:
    """Run the Monte-Carlo entry-permutation guard (spec #9).

    Parameters
    ----------
    observed_statistic
        The real strategy's statistic (its Sharpe over the test period).
        Must be finite.
    n_bars
        Number of bars in the test period (entries are relocated within
        ``[0, n_bars)``). Must be ``>= 1``.
    n_entries
        Number of entry signals to relocate each permutation. Must satisfy
        ``1 <= n_entries <= n_bars``.
    evaluate
        The injected exit logic: given a sorted array of ``n_entries`` distinct
        entry-bar positions, returns the resulting statistic (Sharpe). A NaN or
        ``-inf`` permuted statistic cannot beat a finite observed one (``nan >=
        x`` and ``-inf >= x`` are both False), so it is treated as "did not
        beat"; a ``+inf`` permuted statistic IS counted as beating the observed
        one (``+inf >= x`` is True), which only raises the p-value — the
        conservative direction.
    seed
        Non-negative integer seed for :func:`numpy.random.default_rng`
        (Golden Rule #4 — reproducibility).
    n_permutations
        Number of permutations ``B`` (default 1,000). Must be ``>= 1``.
    alpha
        One-sided significance level in ``(0, 1)`` (default 0.01).

    Returns
    -------
    MonteCarloResult
        The permutation p-value, the pass boolean, and provenance fields.

    Raises
    ------
    ValueError
        If ``seed`` is not a non-negative int, ``observed_statistic`` is
        non-finite, ``n_bars < 1``, ``n_entries`` is out of
        ``[1, n_bars]``, ``n_permutations < 1``, or ``alpha`` is not in
        ``(0, 1)``.
    """
    if not isinstance(seed, int):
        raise ValueError(f"seed must be an int, got {type(seed).__name__}")
    if seed < 0:
        raise ValueError(f"seed must be >= 0, got {seed}")
    if not np.isfinite(observed_statistic):
        raise ValueError(f"observed_statistic must be finite, got {observed_statistic}")
    if n_bars < 1:
        raise ValueError(f"n_bars must be >= 1, got {n_bars}")
    if not 1 <= n_entries <= n_bars:
        raise ValueError(f"n_entries must be in [1, n_bars]={n_bars}, got {n_entries}")
    if n_permutations < 1:
        raise ValueError(f"n_permutations must be >= 1, got {n_permutations}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in the open interval (0, 1), got {alpha}")

    rng = np.random.default_rng(seed)
    n_beaten = 0
    for _ in range(n_permutations):
        # Relocate the entry signals: n_entries distinct bar positions, sorted
        # into chronological order (entries fire in time order).
        positions = np.sort(rng.choice(n_bars, size=n_entries, replace=False)).astype(
            np.intp, copy=False
        )
        permuted = evaluate(positions)
        # A NaN or -inf permuted statistic cannot beat a finite observed one
        # (``nan >= observed`` and ``-inf >= observed`` are both False), so it
        # is a non-beat; a +inf permuted statistic IS counted as beating it
        # (``+inf >= observed`` is True), which only raises the p-value — the
        # conservative direction.
        if permuted >= observed_statistic:
            n_beaten += 1

    pvalue = (1 + n_beaten) / (n_permutations + 1)
    return MonteCarloResult(
        pvalue=pvalue,
        passed=pvalue < alpha,
        observed_statistic=observed_statistic,
        n_beaten=n_beaten,
        n_permutations=n_permutations,
        alpha=alpha,
        n_bars=n_bars,
        n_entries=n_entries,
    )
