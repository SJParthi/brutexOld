"""Hypothesis properties + the Rust↔Python differential for the alpha metric.

DEC-ALPHA-BUYHOLD-METRIC-001. The hostile domain is ±2**60 paisa (far past
the 2**53 exact-f64 integer range — exactly where a convert-first vs
exact-rational divergence, an operand swap or a sign flip would surface):

* the EXACT-SUBTRACTION law — ``alpha_pct`` IS the one IEEE subtraction of
  the two legs (bit-compared, signed zeros included);
* the SIGN law — ``alpha > 0 ⇔ strat_pct > bh_pct`` and
  ``alpha == 0 ⇔ strat_pct == bh_pct`` over the computed legs (IEEE-754
  gradual underflow: a subtraction of distinct finite doubles is never 0);
* the ZERO law — a strategy whose return leg IS the buy-&-hold leg
  (``net_pnl = close − anchor``, ``equity = anchor``) has alpha exactly 0;
* the ROUTED == ORACLE law — the public routed function serves the oracle
  value on every arm;
* the KERNEL differential (real wheel only) — ``fno_math.alpha_vs_buyhold_pct``
  is BIT-IDENTICAL to the oracle (``struct.pack('<d', …)`` equality).
"""

from __future__ import annotations

import struct

import pytest
from brutex.engine.alpha_metric import (
    AlphaBaseline,
    alpha_vs_buyhold_pct,
    alpha_vs_buyhold_pct_python,
    compute_alpha_values,
    reset_alpha_kernel_for_tests,
)
from hypothesis import given, settings
from hypothesis import strategies as st

#: The hostile paisa domain (task-mandated extremes: ±2**60, zero-width
#: windows via close == anchor, sign boundaries via ±1-paisa P&L).
_PNL = st.integers(min_value=-(2**60), max_value=2**60)
_EQUITY = st.integers(min_value=1, max_value=2**60)
_ANCHOR = st.integers(min_value=1, max_value=2**60)
_CLOSE = st.integers(min_value=0, max_value=2**60)


def _bits(value: float) -> bytes:
    """The IEEE-754 bit pattern — distinguishes every ULP and the zero signs."""
    return struct.pack("<d", value)


@given(pnl=_PNL, equity=_EQUITY, anchor=_ANCHOR, close=_CLOSE)
@settings(max_examples=400)
def test_exact_subtraction_and_sign_laws(pnl: int, equity: int, anchor: int, close: int) -> None:
    strat = float(pnl) / float(equity) * 100.0
    bh = float(close - anchor) / float(anchor) * 100.0
    alpha = alpha_vs_buyhold_pct_python(pnl, equity, anchor, close)
    assert _bits(alpha) == _bits(strat - bh), "alpha IS the one IEEE subtraction"
    assert (alpha > 0.0) == (strat > bh), "sign law (computed legs)"
    assert (alpha == 0.0) == (strat == bh), "zero law (computed legs)"


@given(anchor=_ANCHOR, close=_CLOSE)
@settings(max_examples=200)
def test_alpha_of_the_buyhold_strategy_is_zero(anchor: int, close: int) -> None:
    """alpha(strat, strat) == 0: a strategy that IS buy-&-hold has zero alpha."""
    alpha = alpha_vs_buyhold_pct_python(close - anchor, anchor, anchor, close)
    assert alpha == 0.0


@given(pnl=_PNL, equity=_EQUITY, anchor=_ANCHOR, close=_CLOSE)
@settings(max_examples=200)
def test_routed_equals_oracle_bitwise(pnl: int, equity: int, anchor: int, close: int) -> None:
    """The public routed function == the oracle on EVERY arm (kernel or not)."""
    reset_alpha_kernel_for_tests()
    routed = alpha_vs_buyhold_pct(pnl, equity, anchor, close)
    assert _bits(routed) == _bits(alpha_vs_buyhold_pct_python(pnl, equity, anchor, close))


@given(pnl=_PNL, equity=_EQUITY, anchor=_ANCHOR, close=_CLOSE)
@settings(max_examples=200)
def test_compute_alpha_values_invariants(pnl: int, equity: int, anchor: int, close: int) -> None:
    values = compute_alpha_values(pnl, equity, AlphaBaseline(anchor, close))
    assert _bits(values.alpha_pct) == _bits(values.strategy_return_pct - values.bh_return_pct)


class TestKernelDifferential:
    """Rust ↔ Python bit-identity over the hostile domain (real wheel only)."""

    @given(pnl=_PNL, equity=_EQUITY, anchor=_ANCHOR, close=_CLOSE)
    @settings(max_examples=500)
    def test_kernel_bit_identical_to_oracle(
        self, pnl: int, equity: int, anchor: int, close: int
    ) -> None:
        fno_math = pytest.importorskip("fno_math")
        kernel = fno_math.alpha_vs_buyhold_pct(pnl, equity, anchor, close)
        oracle = alpha_vs_buyhold_pct_python(pnl, equity, anchor, close)
        assert _bits(kernel) == _bits(oracle), (pnl, equity, anchor, close, kernel, oracle)

    def test_kernel_domain_refusals_match_the_shared_law(self) -> None:
        fno_math = pytest.importorskip("fno_math")
        for args in ((0, 0, 1, 1), (0, 1, 0, 1), (0, 1, 1, -1)):
            with pytest.raises(ValueError, match="alpha_vs_buyhold_pct"):
                fno_math.alpha_vs_buyhold_pct(*args)
