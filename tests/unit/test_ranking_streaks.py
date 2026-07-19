"""Unit + oracle tests for the binomial-null loss-streak quantile.

DEC-METRICS-KELLY-GROWTH-001 (the Door-5 re-derivation core). Three layers:

1. **Contract** — validation refusals, degenerate arms, hand-derived known
   values, the ``n + 1`` cap.
2. **Exact DP oracle** — the small-``n`` max-run distribution computed by an
   independent dynamic program over trailing-run states (the design pack's
   ruling: the closed form is PRODUCTION, the DP is TEST-ONLY). Measured
   quality (2026-07-18, the grid ``n ∈ {5..50 step 5} × q ∈ {0.05..0.95
   step 0.05}`` plus the large-``n`` spot row ``(n=400, q=0.5)``): the
   approximation's quantile is NEVER tighter than the
   exact quantile (the SAFE, loosening-only error direction — the door's
   ``max(10, k99)`` floor plus this direction mean the derived door can only
   ever widen pools, which the Stage-4 DSR deflation auto-tightens), and for
   ``q <= 0.5`` the two quantiles differ by AT MOST 2. For higher ``q`` the
   closed form grows increasingly CONSERVATIVE (looser — up to the ``n + 1``
   cap); stated honestly, never claimed tight.
3. **Properties** — hypothesis pins for the smallest-``k`` law and
   monotonicity.
"""

from __future__ import annotations

import math
from typing import ClassVar

import pytest
from brutex.ranking import streaks as streaks_mod
from brutex.ranking.streaks import STREAK_DOOR_ALPHA, max_loss_streak_quantile
from hypothesis import given
from hypothesis import strategies as st

# ────── The exact DP oracle (test-only — the design pack's ruling) ──────


def exact_max_run_tail(n: int, q: float, k: int) -> float:
    """Exact ``P(max loss-run >= k)`` for ``n`` i.i.d. Bernoulli(q) trades.

    Dynamic program over the trailing-run state ``0..k-1`` with an absorbing
    "reached k" mass: each step a win (prob ``1-q``) resets the run to 0 and
    a loss extends it; mass reaching ``k`` is absorbed. O(n·k) — test-only.
    """
    if k <= 0:
        return 1.0
    probs = [0.0] * k
    probs[0] = 1.0
    absorbed = 0.0
    for _ in range(n):
        new = [0.0] * k
        new[0] = sum(probs) * (1.0 - q)
        for j in range(1, k):
            new[j] = probs[j - 1] * q
        absorbed += probs[k - 1] * q
        probs = new
    return absorbed


def exact_quantile(n: int, q: float, alpha: float = STREAK_DOOR_ALPHA) -> int:
    """Smallest ``k >= 1`` with the EXACT tail below *alpha* (test oracle)."""
    k = 1
    while exact_max_run_tail(n, q, k) >= alpha:
        k += 1
    return k


def _approx_tail(n: int, q: float, k: int) -> float:
    """The production closed form, restated independently for the pins."""
    return -math.expm1(-n * (1.0 - q) * q**k)


# ────── Contract ──────


class TestValidation:
    def test_alpha_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="alpha must be in"):
            max_loss_streak_quantile(50, 0.4, alpha=0.0)

    def test_alpha_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="alpha must be in"):
            max_loss_streak_quantile(50, 0.4, alpha=1.0)

    def test_alpha_nan_rejected(self) -> None:
        with pytest.raises(ValueError, match="alpha must be in"):
            max_loss_streak_quantile(50, 0.4, alpha=math.nan)

    def test_loss_prob_nan_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite float"):
            max_loss_streak_quantile(50, math.nan)

    def test_loss_prob_inf_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite float"):
            max_loss_streak_quantile(50, math.inf)

    def test_loss_prob_one_rejected(self) -> None:
        # q >= 1 degenerates the closed form (the true max run is n with
        # certainty) — the CALLER owns that arm (streak_door_threshold).
        with pytest.raises(ValueError, match="must be < 1.0"):
            max_loss_streak_quantile(50, 1.0)

    def test_loss_prob_above_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be < 1.0"):
            max_loss_streak_quantile(50, 1.5)


class TestDegenerates:
    def test_zero_trades(self) -> None:
        assert max_loss_streak_quantile(0, 0.4) == 0

    def test_negative_trades(self) -> None:
        assert max_loss_streak_quantile(-3, 0.4) == 0

    def test_zero_loss_prob(self) -> None:
        assert max_loss_streak_quantile(50, 0.0) == 0

    def test_negative_loss_prob(self) -> None:
        assert max_loss_streak_quantile(50, -0.25) == 0


class TestKnownValues:
    """Hand-derived pins of the closed form (audited in-test).

    ``k99(50, 0.4)``: c = −ln(0.99) ≈ 0.0100503; the tail at k is
    1 − exp(−30·0.4^k); at k=8: 30·0.4^8 ≈ 0.019661 → tail ≈ 0.019469 ≥ α;
    at k=9: 30·0.4^9 ≈ 0.0078643 → tail ≈ 0.0078335 < α ⇒ k99 = 9.
    ``k99(30, 0.5)``: at k=10: 15·0.5^10 ≈ 0.014648 → tail ≈ 0.014542 ≥ α;
    at k=11: 15·0.5^11 ≈ 0.0073242 → tail ≈ 0.0072975 < α ⇒ k99 = 11 — the
    Door-5 LOOSENING example (11 > the legacy 10 floor).
    """

    def test_k99_50_trades_40pct(self) -> None:
        assert max_loss_streak_quantile(50, 0.4) == 9

    def test_k99_30_trades_50pct(self) -> None:
        assert max_loss_streak_quantile(30, 0.5) == 11

    def test_high_q_answer_is_capped_at_n_plus_one(self) -> None:
        # Uncapped, the closed form wanders to k=44 for (10, 0.9) — off-model
        # (a run cannot exceed n=10). The cap lands EXACTLY on the DP oracle
        # here: P(run >= 10) = includes the all-loss 0.9^10 ≈ 0.349 ≥ α, and
        # P(run >= 11) = 0 < α ⇒ 11 = n + 1.
        assert max_loss_streak_quantile(10, 0.9) == 11
        assert exact_quantile(10, 0.9) == 11


# ────── Exact-DP oracle comparison (measured, honestly-bounded) ──────


class TestApproximationVsExactDP:
    _GRID_N: ClassVar[range] = range(5, 51, 5)
    _GRID_Q: ClassVar[list[float]] = [qi / 100 for qi in range(5, 96, 5)]

    def test_never_tighter_than_exact(self) -> None:
        """The measured SAFE direction: approx quantile >= exact quantile.

        A tighter-than-exact approximation would REJECT streaks the true
        null calls plausible — the unsafe direction the door's design
        forbids. Swept over the full measurement grid.
        """
        for n in self._GRID_N:
            for q in self._GRID_Q:
                assert max_loss_streak_quantile(n, q) >= exact_quantile(n, q), (n, q)

    def test_never_tighter_large_n_spot(self) -> None:
        """Large-``n`` spot row (n=400, q=0.5) — the DP is O(n·k), fine.

        The grid above tops out at n=50; the derived door's whole point is
        the big loss-heavy pool (the #1975 sanctioned-loosening example is
        exactly 400 trades at loss rate 0.5), so the SAFE direction gets one
        measured row there too.
        """
        assert max_loss_streak_quantile(400, 0.5) >= exact_quantile(400, 0.5)

    def test_within_two_of_exact_for_moderate_loss_rates(self) -> None:
        """Measured band (2026-07-18): |approx − exact| <= 2 for q <= 0.5.

        The band gate-passing strategies live in (Door 4 already rejects the
        low-win-rate/low-payoff corner). Higher ``q`` grows CONSERVATIVE
        (looser) only — pinned by ``test_never_tighter_than_exact``, and
        deliberately NOT bounded here (up to the n+1 cap, stated honestly).
        """
        for n in self._GRID_N:
            for q in [qi / 100 for qi in range(5, 51, 5)]:
                assert abs(max_loss_streak_quantile(n, q) - exact_quantile(n, q)) <= 2, (n, q)

    def test_tail_probability_close_in_the_deciding_region(self) -> None:
        """|approx − exact| tail probability <= 0.08 where the exact tail
        <= 0.05 (the region the alpha=0.01 decision lives in; measured worst
        0.077 at high q on the 2026-07-18 grid — recorded, not flattered)."""
        worst = 0.0
        for n in (10, 30, 50):
            for q in (0.1, 0.3, 0.5, 0.7, 0.9):
                for k in range(1, n + 1):
                    exact = exact_max_run_tail(n, q, k)
                    if exact <= 0.05:
                        worst = max(worst, abs(exact - _approx_tail(n, q, k)))
        assert worst <= 0.08, worst


# ────── Properties ──────


class TestProperties:
    @given(
        n=st.integers(min_value=1, max_value=1_000_000),
        q=st.floats(min_value=0.01, max_value=0.99, allow_nan=False),
    )
    def test_smallest_k_law_under_the_approximation(self, n: int, q: float) -> None:
        """Below the n+1 cap the answer is the SMALLEST k with tail < alpha."""
        k = max_loss_streak_quantile(n, q)
        assert 1 <= k <= n + 1
        if k <= n:
            assert _approx_tail(n, q, k) < STREAK_DOOR_ALPHA
        if k > 1 and k <= n + 1:
            # k-1 (>= 1) must NOT satisfy the inequality — else k was not
            # smallest. At the cap, k-1 = n legitimately still fails it.
            assert _approx_tail(n, q, k - 1) >= STREAK_DOOR_ALPHA

    @given(
        n=st.integers(min_value=1, max_value=100_000),
        q=st.floats(min_value=0.01, max_value=0.95, allow_nan=False),
    )
    def test_monotone_in_n(self, n: int, q: float) -> None:
        """More trades can only need an equal-or-longer justified run."""
        assert max_loss_streak_quantile(2 * n, q) >= max_loss_streak_quantile(n, q)

    @given(
        n=st.integers(min_value=1, max_value=100_000),
        q=st.floats(min_value=0.01, max_value=0.45, allow_nan=False),
    )
    def test_monotone_in_q(self, n: int, q: float) -> None:
        """A lossier null can only justify an equal-or-longer run."""
        assert max_loss_streak_quantile(n, 2 * q) >= max_loss_streak_quantile(n, q)


class TestSeedAdjustmentBelt:
    """Fault-injection pins for the monotone-adjustment walks.

    The closed-form seed ``ceil(ln(ratio) / ln(q))`` is empirically exact on
    clean IEEE floats (a 400k-case random sweep + dense grid sweeps never
    entered either walk), so the walks are a float-rounding BELT — pinned
    here by deterministically perturbing the seed one step in each
    direction (the house bomb-kernel/burn-in fault-injection idiom) and
    asserting the walk restores the exact unperturbed answer.
    """

    def _with_ceil(self, monkeypatch: pytest.MonkeyPatch, offset: int) -> int:
        real_ceil = math.ceil

        def skewed_ceil(x: float) -> int:
            return max(1, int(real_ceil(x)) + offset)

        monkeypatch.setattr(streaks_mod.math, "ceil", skewed_ceil)
        try:
            return max_loss_streak_quantile(50, 0.4)
        finally:
            monkeypatch.undo()

    def test_down_walk_corrects_a_seed_one_too_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        expected = max_loss_streak_quantile(50, 0.4)
        assert self._with_ceil(monkeypatch, +1) == expected == 9

    def test_up_walk_corrects_a_seed_one_too_low(self, monkeypatch: pytest.MonkeyPatch) -> None:
        expected = max_loss_streak_quantile(50, 0.4)
        assert self._with_ceil(monkeypatch, -1) == expected == 9
