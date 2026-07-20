"""Hypothesis property tests for the Stage-3 overfit gate (DEC-RANKING-OVERFIT-001).

Invariants that must hold for every valid input, complementing the known-value
unit tests in ``tests/unit/test_ranking.py``:

1. **DSR range** — :func:`deflated_sharpe_for` returns ``None`` or a finite
   probability in ``[0, 1]`` (never NaN / inf — spec line 746).
2. **Dispersion sanity** — :func:`pool_sharpe_dispersion` reports a non-negative
   variance and an ``n_finite`` that equals the count of finite Sharpes.
3. **DSR monotone in the observed Sharpe** — all else fixed (a deflatable pool),
   a higher Sharpe never lowers the Deflated Sharpe.
4. **Gate monotone in ``dsr_min``** — a combo that clears a strict bar also clears
   every looser one.
5. **Excess→non-excess kurtosis** — :func:`deflated_sharpe_for` is exactly the
   engine evaluated at ``excess + GAUSSIAN_KURTOSIS`` (the +3 convention).
6. **The gate can only shrink the leaderboard** — the overfit survivor set is a
   subset of the legacy (five-gate) survivor set; and an undeflatable pool
   abstains (the survivor set is unchanged and no DSR is recorded).
"""

from __future__ import annotations

import itertools
import math
from typing import Any

from brutex.metrics.compute import ResultMetrics
from brutex.metrics.deflated_sharpe import GAUSSIAN_KURTOSIS, deflated_sharpe_ratio
from brutex.ranking import OverfitGateConfig, rank_combos
from brutex.ranking.overfit import (
    deflated_sharpe_for,
    passes_overfit_gate,
    pool_sharpe_dispersion,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# Inputs that keep the PSR estimator-variance radicand strictly positive
# (skew ∈ [-1, 1], excess-kurtosis ∈ [2, 12] ⇒ non-excess ∈ [5, 15]) so the
# undefined-radicand guard is not the subject under test here.
_sharpe = st.floats(min_value=-3.0, max_value=6.0, allow_nan=False, allow_infinity=False)
_skew = st.floats(min_value=-1.0, max_value=1.0)
_excess_kurt = st.floats(min_value=2.0, max_value=12.0)
_trials = st.integers(min_value=1, max_value=1_000_000)
_variance = st.floats(min_value=0.0, max_value=4.0)
_sample = st.integers(min_value=2, max_value=5_000)


def _m(**overrides: Any) -> ResultMetrics:
    """A gate-passing ResultMetrics; override per test."""
    base: dict[str, Any] = {
        "net_pnl_paisa": 0,
        "total_return_pct": 0.0,
        "cagr_pct": 0.0,
        "gross_profit_paisa": 0,
        "gross_loss_paisa": 0,
        "sharpe_ratio": 1.0,
        "sortino_ratio": 1.0,
        "calmar_ratio": 1.0,
        "mar_ratio": 0.0,
        "information_ratio": 0.0,
        "omega_ratio": 0.0,
        "max_drawdown_pct": 5.0,
        "max_drawdown_paisa": 0,
        "max_drawdown_duration_bars": 0,
        "recovery_factor": 1.0,
        "ulcer_index": 0.0,
        "largest_losing_trade_paisa": 0,
        "max_consecutive_losses": 2,
        "max_consecutive_wins": 0,
        "total_trades": 50,
        "winning_trades": 30,
        "losing_trades": 20,
        "win_rate": 0.6,
        "profit_factor": 1.5,
        "avg_winning_trade_paisa": 0,
        "avg_losing_trade_paisa": 0,
        "win_loss_ratio": 1.5,
        "expectancy_paisa": 0,
        "avg_mfe_paisa": 0,
        "avg_mae_paisa": 0,
        "avg_holding_time_bars": 0.0,
        "max_holding_time_bars": 0,
        "time_in_market_pct": 0.0,
        "tail_ratio": 0.0,
        "skewness": 0.0,
        "kurtosis": 0.0,
        "returns_std_dev": 0.0,
        # The loss/growth trio (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 /
        # DEC-METRICS-KELLY-GROWTH-001): loss_rate consistent with the
        # counts above (losing 20 / total 50); the Kelly pair defaults
        # None (undefined) — override per test where the value matters.
        "loss_rate": 0.4,
        "kelly_fraction": None,
        "log_growth_at_half_kelly": None,
    }
    base.update(overrides)
    return ResultMetrics(**base)


@given(sr=_sharpe, n=_trials, v=_variance, length=_sample, sk=_skew, ek=_excess_kurt)
def test_dsr_is_none_or_probability(
    sr: float, n: int, v: float, length: int, sk: float, ek: float
) -> None:
    cfg = OverfitGateConfig()
    dsr = deflated_sharpe_for(sr, length, sk, ek, n_trials=n, trial_sharpe_variance=v, cfg=cfg)
    if dsr is not None:
        assert math.isfinite(dsr)
        assert 0.0 <= dsr <= 1.0


@given(st.lists(st.one_of(st.none(), _sharpe), max_size=40))
def test_dispersion_sanity(sharpes: list[float | None]) -> None:
    disp = pool_sharpe_dispersion(sharpes)
    assert disp.variance >= 0.0
    assert disp.n_finite == sum(1 for s in sharpes if s is not None and math.isfinite(s))
    assert disp.deflatable == (disp.n_finite >= 2 and disp.variance > 0.0)


# Monotonicity holds on the NON-NEGATIVE observed-Sharpe domain only. With the
# Gaussian moments this test fixes (skew=0, excess-kurtosis=0 ⇒ radicand
# 1 + SR²/2), the PSR z-score derivative wrt observed Sharpe is
# (1 + SR_0·SR/2) / radicand^1.5, which is strictly positive for SR >= 0 and the
# non-negative benchmark SR_0, but can go NEGATIVE at large-negative Sharpe (the
# estimator-variance radicand then grows faster than the numerator) — so the DSR
# is genuinely NON-monotone in observed Sharpe in the loss-making tail. Restrict
# to SR >= 0 (the regime that matters for ranking winning strategies); see
# DEC-MUTATION-GATE-RANKING-001 (MIGRATION.md #586). ``derandomize`` pins the
# run for CI determinism (STANDARDS_100PCT.md §1 row 9 closing path).
@settings(derandomize=True)
@given(
    sr_lo=st.floats(min_value=0.0, max_value=6.0, allow_nan=False, allow_infinity=False),
    delta=st.floats(min_value=0.0, max_value=5.0),
    n=st.integers(min_value=2, max_value=10_000),
    v=st.floats(min_value=0.01, max_value=4.0),
    length=_sample,
)
def test_dsr_monotone_in_observed_sharpe(
    sr_lo: float, delta: float, n: int, v: float, length: int
) -> None:
    cfg = OverfitGateConfig()
    lo = deflated_sharpe_for(sr_lo, length, 0.0, 0.0, n_trials=n, trial_sharpe_variance=v, cfg=cfg)
    hi = deflated_sharpe_for(
        sr_lo + delta, length, 0.0, 0.0, n_trials=n, trial_sharpe_variance=v, cfg=cfg
    )
    assert lo is not None and hi is not None  # Gaussian moments keep the radicand > 0
    assert hi >= lo - 1e-12


@given(dsr=st.floats(min_value=0.0, max_value=1.0), a=st.floats(0.0, 1.0), b=st.floats(0.0, 1.0))
def test_gate_monotone_in_threshold(dsr: float, a: float, b: float) -> None:
    lo_min, hi_min = (a, b) if a <= b else (b, a)
    if passes_overfit_gate(dsr, OverfitGateConfig(dsr_min=hi_min)):
        # Clearing the stricter bar implies clearing the looser one.
        assert passes_overfit_gate(dsr, OverfitGateConfig(dsr_min=lo_min))


@given(sr=_sharpe, n=_trials, v=_variance, length=_sample, sk=_skew, ek=_excess_kurt)
def test_excess_to_non_excess_kurtosis_identity(
    sr: float, n: int, v: float, length: int, sk: float, ek: float
) -> None:
    cfg = OverfitGateConfig()
    got = deflated_sharpe_for(sr, length, sk, ek, n_trials=n, trial_sharpe_variance=v, cfg=cfg)
    ref = deflated_sharpe_ratio(
        sr,
        n_trials=n,
        trial_sharpe_variance=v,
        sample_length=length,
        skew=sk,
        kurtosis=ek + GAUSSIAN_KURTOSIS,
    ).deflated_sharpe_ratio
    assert got == ref


@given(
    sharpes=st.lists(
        st.floats(min_value=-3.0, max_value=6.0, allow_nan=False, allow_infinity=False),
        min_size=0,
        max_size=20,
        unique=True,
    ),
    perm_seed=st.randoms(use_true_random=False),
)
def test_ranking_is_a_permutation_invariant_total_order(
    sharpes: list[float], perm_seed: Any
) -> None:
    """Permuting the candidate enumeration yields an identically-ORDERED ranking.

    The determinism oracle for the tie-break fix (DEC-RANKING-TIEBREAK-001): a
    leaderboard is a SET of combos, so its ranking must not depend on the order the
    sweep enumerated them in (Golden Rules #4/#7). Distinct masks + a deliberate
    proportion of DUPLICATE Sharpes (``i % 3`` reuse) force real composite ties so
    the mask tie-break is exercised, not bypassed by always-distinct scores.

    COMPOSITE-VALUE envelope (honesty reconcile, W1 finalize): the pool z-score
    columns are LEFT-FOLDED in enumeration order (``_zscores``' ``sum``), so a
    permuted pool can yield composite scores differing at ULP level — the
    PRE-EXISTING documented envelope (the ``pragma: no mutate`` rationale in
    ``composite_term_inputs``: "float rounding can differ at ULP level on some
    pools, which cannot meaningfully reorder a leaderboard"), executed-proven
    present at merge base 98b9a732 with the same falsifying pool. The exact
    LAW is the mask ORDER (asserted exactly); composite values are asserted
    equal within a tight ULP-scale tolerance, never bit-exact across a
    permuted fold.
    """
    # Reuse each Sharpe up to three times so many combos share a composite score
    # (the tie regime the stable-sort-only version mis-ordered). Masks stay unique.
    cand = [(i, _m(sharpe_ratio=sharpes[i % len(sharpes)] if sharpes else 0.0)) for i in range(15)]
    shuffled = list(cand)
    perm_seed.shuffle(shuffled)
    base = rank_combos(cand)
    other = rank_combos(shuffled)
    assert [rc.mask for rc in base] == [rc.mask for rc in other]
    assert all(
        math.isclose(a.composite_score, b.composite_score, rel_tol=1e-9, abs_tol=1e-12)
        for a, b in zip(base, other, strict=True)
    )
    # Total order: masks strictly increase within every equal-score run.
    for a, b in itertools.pairwise(base):
        if a.composite_score == b.composite_score:
            assert a.mask < b.mask


@given(sharpes=st.lists(_sharpe, min_size=1, max_size=12))
def test_gate_only_shrinks_or_abstains(sharpes: list[float]) -> None:
    cfg = OverfitGateConfig(dsr_min=0.5)
    cand = [(i, _m(sharpe_ratio=s)) for i, s in enumerate(sharpes)]
    legacy = {rc.mask for rc in rank_combos(cand)}
    gated = rank_combos(cand, overfit=cfg)
    gated_masks = {rc.mask for rc in gated}
    # The overfit gate can only remove combos (or abstain), never add.
    assert gated_masks <= legacy
    disp = pool_sharpe_dispersion(sharpes)
    if not disp.deflatable:
        # Abstain: the survivor set is unchanged and no DSR is recorded.
        assert gated_masks == legacy
        assert all(rc.deflated_sharpe is None for rc in gated)
    else:
        # Deflatable: every recorded DSR cleared the bar.
        assert all(
            rc.deflated_sharpe is not None and rc.deflated_sharpe >= cfg.dsr_min for rc in gated
        )
