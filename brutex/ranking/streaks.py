"""Binomial-null loss-streak quantile — the derived Door-5 threshold core.

DEC-METRICS-KELLY-GROWTH-001 (the item-8 streak-door re-derivation): the
fixed ``max_consecutive_losses <= 10`` rejection gate judged every strategy
against ONE constant regardless of its trade count or loss probability — a
high-frequency strategy legitimately produces longer loss runs by pure
chance. This module supplies the statistics: the smallest run length ``k``
whose probability of appearing at all, in ``n`` i.i.d. trades under the
strategy's OWN loss probability ``q``, falls below a small ``alpha`` — i.e.
the ``(1 − alpha)`` quantile boundary of the max-loss-run null distribution.
A strategy whose observed streak stays at-or-below that boundary is
CONSISTENT with luck under its own loss rate; only a streak the null calls
essentially impossible is an account-blower signal.

The approximation (the standard Feller/Schilling-style closed form for the
longest success run): ``P(max loss-run >= k) ≈ 1 − exp(−n·(1−q)·q^k)``.
Production evaluates it in CLOSED FORM — a logarithm seed plus a bounded
monotone adjustment, O(1) per call, never a scan — matching the pinned
O(1)-per-combo gate cost; the exact small-``n`` dynamic program lives ONLY
in the test oracle (``tests/unit/test_ranking_streaks.py``), which measures
the approximation's quality against it and records the observed tolerance
honestly.

The DOOR composition (floor at the legacy 10, the win/loss-ratio exemption)
lives beside the other gates in :mod:`brutex.ranking` — this module is the
pure quantile only, so the statistics has exactly ONE home (CLAUDE.md §11
COMMON) for the ranking gate AND the ``top100_artifact`` gate mirror.
"""

from __future__ import annotations

import math
from typing import Final

__all__ = ["STREAK_DOOR_ALPHA", "max_loss_streak_quantile"]

#: The Door-5 quantile level: the null probability below which a loss-run
#: length is judged "not luck" (``P(max run >= k) < alpha`` ⇒ reject-worthy).
#: 0.01 is the lane's PROPOSED level — the design map marks the exact level
#: [NEEDS OPERATOR RULING] (recorded in DEC-METRICS-KELLY-GROWTH-001); a
#: ruling changes this ONE constant.
STREAK_DOOR_ALPHA: Final[float] = 0.01


def max_loss_streak_quantile(n: int, loss_prob: float, *, alpha: float = STREAK_DOOR_ALPHA) -> int:
    """Smallest ``k >= 1`` with ``P(max loss-run >= k in n trades) < alpha``.

    The null model: ``n`` i.i.d. trades, each a loss with probability
    ``loss_prob = q``; the longest loss run's tail probability is
    approximated by the closed form ``P(max run >= k) ≈
    1 − exp(−n·(1−q)·q^k)`` (evaluated via ``expm1`` for small-argument
    accuracy). The returned ``k`` is the exact smallest integer satisfying
    the inequality UNDER THAT APPROXIMATION — strictly decreasing in ``k``
    for ``q ∈ (0, 1)``, so the answer is found from a logarithm closed-form
    seed plus a bounded monotone adjustment (O(1), no scan; the seed is off
    by at most float-rounding, and the adjustment loops are each a step or
    two — hypothesis-pinned).

    The answer is CAPPED at ``n + 1``: a run cannot exceed the sequence
    length, so the TRUE tail is identically zero for every ``k > n`` — the
    closed form does not know that and can otherwise wander far past ``n``
    for high ``q``. The cap keeps the approximation's measured
    NEVER-TIGHTER-than-exact direction (the exact quantile is itself always
    ``<= n + 1``), while the raw formula's answer below the cap is returned
    untouched.

    Degenerate arms (the DOOR owns the ``q >= 1`` all-loss case — see
    ``brutex.ranking``):

    * ``n <= 0`` → ``0`` (no trades — no run is ever observable; the door
      is vacuous).
    * ``loss_prob <= 0.0`` → ``0`` (a loss is impossible under the null;
      any observed loss run at all is already off-model — the door's
      ``max(10, …)`` floor keeps this loosening-only).
    * ``loss_prob >= 1.0`` → :class:`ValueError`: the approximation's
      ``(1−q)`` factor degenerates to a constant-0 tail, which is FALSE
      (the true max run is ``n`` with certainty) — the caller must branch
      its own degenerate arm instead of consuming a wrong number
      (Golden Rule #1).

    Parameters
    ----------
    n
        Trade count (the null's sequence length).
    loss_prob
        Per-trade loss probability ``q`` under the null — the strategy's
        own observed loss rate. Must be finite and ``< 1.0``.
    alpha
        The quantile level (:data:`STREAK_DOOR_ALPHA` default). Must be a
        finite float in ``(0, 1)``.

    Raises
    ------
    ValueError
        If *alpha* is outside ``(0, 1)`` or non-finite, or *loss_prob* is
        ``>= 1.0`` or non-finite.
    """
    if not math.isfinite(alpha) or not 0.0 < alpha < 1.0:
        raise ValueError(f"max_loss_streak_quantile: alpha must be in (0, 1), got {alpha}")
    if not math.isfinite(loss_prob):
        raise ValueError(
            f"max_loss_streak_quantile: loss_prob must be a finite float, got {loss_prob}"
        )
    if loss_prob >= 1.0:
        raise ValueError(
            f"max_loss_streak_quantile: loss_prob must be < 1.0, got {loss_prob} — the "
            f"closed-form null degenerates at q >= 1 (the true max run is n with "
            f"certainty); the caller owns that degenerate arm"
        )
    if n <= 0 or loss_prob <= 0.0:
        return 0

    def tail(k: int) -> float:
        # P(max loss-run >= k) ≈ 1 − exp(−n·(1−q)·q^k), via expm1: exact to
        # the formula for tiny arguments where 1 − exp(−x) would cancel.
        return -math.expm1(-n * (1.0 - loss_prob) * loss_prob**k)

    # Closed-form seed: tail(k) < alpha ⇔ n·(1−q)·q^k < −ln(1−alpha) = c
    # ⇔ q^k < c / (n·(1−q)) ⇔ k > ln(ratio) / ln(q) (ln q < 0 flips the
    # inequality). ratio >= 1 means even k = 1 satisfies it.
    c = -math.log1p(-alpha)
    ratio = c / (n * (1.0 - loss_prob))
    k = 1 if ratio >= 1.0 else max(1, math.ceil(math.log(ratio) / math.log(loss_prob)))
    # Monotone adjustment (float-rounding belt around the seed): walk DOWN
    # while the previous k already satisfies the inequality, then UP while
    # the current k does not. tail() is strictly decreasing in k, and
    # q**k underflows to 0.0 for large k (tail → −expm1(−0) = 0 < alpha),
    # so both walks terminate; in practice each is 0-1 steps.
    while k > 1 and tail(k - 1) < alpha:
        k -= 1
    while k <= n and tail(k) >= alpha:
        k += 1
    # The n+1 cap (docstring): the true tail is identically zero past n, so
    # the formula's off-model positive tail can never push the answer beyond
    # the exact quantile's own ceiling.
    return min(k, n + 1)
