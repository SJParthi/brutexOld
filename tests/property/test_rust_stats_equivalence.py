"""Hypothesis ON≡OFF equivalence for the Rust CPCV fold kernel (slice 6).

DEC-METRICS-RUST-STATS-001 PR-1. Drives adversarial returns series through
:func:`brutex.metrics.cpcv.cpcv_pass_rate` with ``BRUTEX_STATS_RUST=0`` then
``=1`` and asserts:

* the returned RATE is BIT-equal (``struct.pack('<d', …)`` — never approx;
  the rate is ``repr``-serialized into cascade-report bytes downstream);
* the integer ``n_pass`` is EXACTLY equal at the seam level (the
  count-exactness law — a single fold flip moves a cascade verdict at the
  0.5 threshold);
* the ON arm genuinely ENGAGED the Rust kernel (anti-vacuous-pass law: CI
  always builds the wheel via ``pip install ./rust/fno-math``; a wheel-free
  environment fails HERE loudly — install the wheel — never passes
  vacuously);
* no burn-in autorevert fired (bit-equality alone cannot see a burn-in
  mismatch — the seam ships the oracle value on mismatch by design, so the
  latch state is the observable).

Adversarial input classes: near-zero-mean folds (the sign of the fold mean
IS the pass verdict at ``min_sharpe=0``), exact-constant series (zero
variance → undefined folds), 1e300-scale overflow (the inf/NaN fail-safe
must flow IEEE-identically), and the tiny ``n == n_groups`` boundary
(singleton groups). ``min_sharpe`` is drawn from floats ∪ INTS — including
non-float-representable ints past 2^53, the refuter-round-1 exactness-leak
class the seam's domain guard refuses to the oracle. The strict-``>``
boundary at an ACHIEVED fold Sharpe is pinned deterministically below.
"""

from __future__ import annotations

import math
import struct

import numpy as np
import pytest
from brutex.metrics import rust_stats
from brutex.metrics.cpcv import (
    _combine_moments,  # pyright: ignore[reportPrivateUsage]
    _fold_pass_count,  # pyright: ignore[reportPrivateUsage]
    _group_bounds,  # pyright: ignore[reportPrivateUsage]
    _group_moments,  # pyright: ignore[reportPrivateUsage]
    cpcv_pass_rate,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs


@st.composite
def _cases(draw: st.DrawFn) -> tuple[list[float], int, int, float]:
    """(returns, n_groups, n_test_groups, min_sharpe) — adversarial classes."""
    s = draw(st.integers(min_value=2, max_value=8))
    k = draw(st.integers(min_value=1, max_value=s - 1))
    # n == s is the singleton-group boundary (count-1 groups → undefined
    # k=1 folds); larger n exercises multi-observation merges.
    n = draw(st.integers(min_value=s, max_value=64))
    kind = draw(st.sampled_from(["general", "near_zero", "constant", "huge"]))
    if kind == "general":
        vals = draw(
            st.lists(
                st.floats(min_value=-1.0, max_value=1.0, allow_nan=False),
                min_size=n,
                max_size=n,
            )
        )
    elif kind == "near_zero":
        # Fold means within a few ULP of zero — the sign-flip territory the
        # STRUCTURAL claim must hold on.
        vals = draw(
            st.lists(
                st.floats(min_value=-1e-12, max_value=1e-12, allow_nan=False),
                min_size=n,
                max_size=n,
            )
        )
    elif kind == "constant":
        c = draw(st.floats(min_value=-10.0, max_value=10.0, allow_nan=False))
        vals = [c] * n
    else:
        # 1e300-scale: squared deviations / merge terms overflow to inf —
        # the fail-safe non-pass must be IEEE-identical on both arms.
        vals = draw(
            st.lists(
                st.sampled_from([1e300, -1e300, 2e300, 5e299, 0.0]),
                min_size=n,
                max_size=n,
            )
        )
    # min_sharpe over floats ∪ INTS (refuter-round-1 fold): the ON≡OFF law
    # must hold for EVERY type-legal threshold. Ints past 2^53 (2^53+3,
    # 2^60+1, and random draws up to ±2^60) are not float-representable —
    # the seam's exactness domain guard refuses the kernel for those
    # (never a rounded FFI compare against the oracle's EXACT int↔float
    # compare); value-exact ints (0, ±1, 2^53) stay kernel-served.
    min_sharpe: float = draw(
        st.one_of(
            st.sampled_from([0.0, 0.5, -0.5]),
            st.sampled_from([0, 1, -1, 2**53, 2**53 + 3, -(2**53 + 3), 2**60 + 1]),
            st.integers(min_value=-(2**60), max_value=2**60),
        )
    )
    return vals, s, k, min_sharpe


def _rate_under_flag(
    flag: str, returns: list[float], s: int, k: int, min_sharpe: float, *, burn_in: bool
) -> tuple[float, bool]:
    """Run ``cpcv_pass_rate`` under *flag*; return (rate, engaged)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(rust_stats.STATS_RUST_ENV, flag)
        rust_stats.reset_observability_memo_for_tests()
        remaining = None if burn_in else 0
        rust_stats.reset_self_check_for_tests("cpcv", remaining=remaining)
        with capture_logs() as logs, np.errstate(all="ignore"):
            rate = cpcv_pass_rate(returns, n_groups=s, n_test_groups=k, min_sharpe=min_sharpe)
        engaged = any(e["event"] == "rust_stats.engaged" for e in logs)
        # A burn-in mismatch would ship the oracle value (rates equal!) and
        # LATCH — so the latch state is the mismatch observable.
        assert not rust_stats.kernel_disabled("cpcv"), "burn-in autorevert fired: kernel≠oracle"
    return rate, engaged


@settings(max_examples=150)
@given(case=_cases(), burn_in=st.booleans())
def test_on_off_rate_bit_equal_and_count_exact(
    case: tuple[list[float], int, int, float], burn_in: bool
) -> None:
    returns, s, k, min_sharpe = case
    # Value-exact conversion ⇒ the kernel engages; an inexact threshold (an
    # int past 2^53) is domain-refused to the oracle — refuter-round-1 fold.
    exact = float(min_sharpe) == min_sharpe
    off_rate, off_engaged = _rate_under_flag("0", returns, s, k, min_sharpe, burn_in=burn_in)
    on_rate, on_engaged = _rate_under_flag("1", returns, s, k, min_sharpe, burn_in=burn_in)
    assert not off_engaged
    # Anti-vacuous-pass law wherever the kernel CAN legally engage.
    assert on_engaged == exact
    assert struct.pack("<d", on_rate) == struct.pack("<d", off_rate)

    # Count-exactness at the seam: the integer n_pass, not just the rate.
    with np.errstate(all="ignore"):
        arr = np.asarray(returns, dtype=np.float64)
        count, mean, m2 = _group_moments(arr, _group_bounds(len(returns), s))
        oracle_count = _fold_pass_count(count, mean, m2, s, k, min_sharpe)
        rust_stats.reset_self_check_for_tests("cpcv", remaining=0)
        kernel_count = rust_stats.cpcv_pass_count_via_kernel(
            count,
            mean,
            m2,
            k,
            min_sharpe,
            n_total=math.comb(s, k),
            oracle=lambda: oracle_count,
        )
    if exact:
        assert kernel_count == oracle_count
    else:
        # The domain guard refused the kernel — the CALLER's oracle serves.
        assert kernel_count is None


def test_strict_gt_boundary_at_achieved_fold_sharpe() -> None:
    """``min_sharpe`` set EXACTLY at an achieved fold Sharpe: the strict-``>``
    predicate makes that fold a NON-pass on BOTH arms, bit-equal."""
    returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.005, -0.015, 0.025]
    s, k = 4, 2
    arr = np.asarray(returns, dtype=np.float64)
    count, mean, m2 = _group_moments(arr, _group_bounds(len(returns), s))
    # Pick a real achieved fold Sharpe as the threshold.
    achieved = _combine_moments(count, mean, m2, (0, 1))
    assert achieved is not None
    off_rate, _ = _rate_under_flag("0", returns, s, k, achieved, burn_in=True)
    on_rate, on_engaged = _rate_under_flag("1", returns, s, k, achieved, burn_in=True)
    assert on_engaged
    assert struct.pack("<d", on_rate) == struct.pack("<d", off_rate)
    # The threshold fold itself does not pass (strict >) but one ulp below
    # it does — pinned through BOTH arms.
    below = math.nextafter(achieved, -math.inf)
    off_b, _ = _rate_under_flag("0", returns, s, k, below, burn_in=True)
    on_b, _ = _rate_under_flag("1", returns, s, k, below, burn_in=True)
    assert struct.pack("<d", on_b) == struct.pack("<d", off_b)
    assert on_b > on_rate


def test_production_shape_s16_k8_bit_equal() -> None:
    """One full production-shape case (S=16, k=8 → 12,870 folds) through
    both arms, burn-in inclusive."""
    rng = np.random.default_rng(20240603)
    returns = rng.normal(0.0, 0.01, 512).tolist()
    off_rate, _ = _rate_under_flag("0", returns, 16, 8, 0.0, burn_in=True)
    on_rate, on_engaged = _rate_under_flag("1", returns, 16, 8, 0.0, burn_in=True)
    assert on_engaged
    assert struct.pack("<d", on_rate) == struct.pack("<d", off_rate)
