"""Property pins for the CVaR tail fold + gate (DEC-RANK-CVAR-TAIL-GATE-001).

The integer-exact laws over randomized hostile int inputs:

* Rust↔Python PARITY — the ``fno_math.stats_cvar_tail_fold`` kernel is
  bit-identical to the pure-Python oracle (all-integer, so exact ``==``);
* CVaR ≤ VaR — the worst-k tail mean can never exceed its largest member
  (``tail_sum ≤ var · k``, exact cross-product);
* tail mean ≤ overall mean — the mean of the k smallest can never exceed
  the mean of all (``tail_sum · n ≤ total · k``);
* permutation invariance + translation equivariance;
* monotone in alpha — a wider tail never shrinks ``k`` and never worsens
  the per-trade tail mean (``sum_lo · k_hi ≤ sum_hi · k_lo``);
* verdict consistency — :func:`cvar_rejects` IS the strict-``<``
  cross-product law over the fold (never a divergent re-derivation).

The kernel-parity property forces the ``BRUTEX_RANK_CVAR_RUST`` flag ON for
its own calls only and resets the seam afterward; the fno_math wheel is a
transitive requirement of importing :mod:`brutex.ranking` at all, so the
property runs unconditionally wherever this file collects.
"""

from __future__ import annotations

import os

import fno_math
from brutex.ranking.cvar_gate import (
    CVAR_RUST_ENV,
    CvarGateConfig,
    _cvar_tail_fold_python,  # pyright: ignore[reportPrivateUsage]
    cvar_rejects,
    reset_cvar_observability_for_tests,
    reset_cvar_rust_for_tests,
)
from hypothesis import given
from hypothesis import strategies as st

_pnls = st.lists(st.integers(min_value=-(2**62), max_value=2**62), min_size=1, max_size=200)
_alpha = st.integers(min_value=1, max_value=10_000)


@given(pnls=_pnls, alpha_bps=_alpha)
def test_kernel_parity_with_python_oracle(pnls: list[int], alpha_bps: int) -> None:
    expected = _cvar_tail_fold_python(pnls, alpha_bps)
    k, var_paisa, tail_sum = fno_math.stats_cvar_tail_fold(pnls, alpha_bps)
    assert (k, var_paisa, tail_sum) == (expected.k, expected.var_paisa, expected.tail_sum_paisa)


@given(pnls=_pnls, alpha_bps=_alpha)
def test_seam_flag_on_serves_the_same_fold(pnls: list[int], alpha_bps: int) -> None:
    # The routed seam (flag ON, real kernel) == the oracle, fold for fold.
    # Manual env save/restore (a function-scoped monkeypatch fixture trips
    # the hypothesis health check under @given).
    from brutex.ranking.cvar_gate import cvar_tail_fold

    prior = os.environ.get(CVAR_RUST_ENV)
    os.environ[CVAR_RUST_ENV] = "1"
    reset_cvar_rust_for_tests()
    reset_cvar_observability_for_tests()
    try:
        assert cvar_tail_fold(pnls, alpha_bps) == _cvar_tail_fold_python(pnls, alpha_bps)
    finally:
        if prior is None:
            os.environ.pop(CVAR_RUST_ENV, None)
        else:
            os.environ[CVAR_RUST_ENV] = prior
        reset_cvar_rust_for_tests()
        reset_cvar_observability_for_tests()


@given(pnls=_pnls, alpha_bps=_alpha)
def test_tail_mean_at_most_var_and_overall_mean(pnls: list[int], alpha_bps: int) -> None:
    fold = _cvar_tail_fold_python(pnls, alpha_bps)
    assert fold.tail_sum_paisa <= fold.var_paisa * fold.k
    assert fold.tail_sum_paisa * fold.n <= sum(pnls) * fold.k


@given(pnls=_pnls, alpha_bps=_alpha, seed=st.randoms(use_true_random=False))
def test_permutation_invariance(pnls: list[int], alpha_bps: int, seed: object) -> None:
    shuffled = list(pnls)
    seed.shuffle(shuffled)  # type: ignore[attr-defined]
    assert _cvar_tail_fold_python(pnls, alpha_bps) == _cvar_tail_fold_python(shuffled, alpha_bps)


@given(
    pnls=st.lists(st.integers(min_value=-(2**40), max_value=2**40), min_size=1, max_size=100),
    alpha_bps=_alpha,
    c=st.integers(min_value=-(2**40), max_value=2**40),
)
def test_translation_equivariance(pnls: list[int], alpha_bps: int, c: int) -> None:
    base = _cvar_tail_fold_python(pnls, alpha_bps)
    shifted = _cvar_tail_fold_python([v + c for v in pnls], alpha_bps)
    assert shifted.k == base.k
    assert shifted.var_paisa == base.var_paisa + c
    assert shifted.tail_sum_paisa == base.tail_sum_paisa + c * base.k


@given(pnls=_pnls, a=_alpha, b=_alpha)
def test_monotone_in_alpha(pnls: list[int], a: int, b: int) -> None:
    lo, hi = min(a, b), max(a, b)
    f_lo = _cvar_tail_fold_python(pnls, lo)
    f_hi = _cvar_tail_fold_python(pnls, hi)
    assert f_lo.k <= f_hi.k
    # mean_lo ≤ mean_hi without division: sum_lo · k_hi ≤ sum_hi · k_lo.
    assert f_lo.tail_sum_paisa * f_hi.k <= f_hi.tail_sum_paisa * f_lo.k


@given(
    pnls=_pnls,
    alpha_bps=_alpha,
    threshold=st.integers(min_value=1, max_value=2**48),
)
def test_verdict_is_the_strict_cross_product_law(
    pnls: list[int], alpha_bps: int, threshold: int
) -> None:
    cfg = CvarGateConfig(threshold_paisa=threshold, alpha_bps=alpha_bps)
    fold = _cvar_tail_fold_python(pnls, alpha_bps)
    assert cvar_rejects(pnls, cfg) is (fold.tail_sum_paisa < -threshold * fold.k)


def test_empty_always_abstains() -> None:
    assert cvar_rejects([], CvarGateConfig(threshold_paisa=1)) is False
