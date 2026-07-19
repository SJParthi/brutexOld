"""Cross-language differential for the BORN-RUST binomial-tail kernel.

DEC-STAGE1B-LOW-SUPPORT-RESCUE-001 (LANE W3). The production value is the
Rust kernel (``fno_math.stats_binom_tail_log_sf``); Python keeps NO
production copy — these tests carry the two ORACLES that bound it:

1. an op-for-op Python TRANSCRIPTION of the kernel's log-space recurrence
   (general ``p`` — the hostile-input differential; tolerance 1e-9 log-units
   because ``ln``/``exp`` are libm calls whose last-ULP rounding is not
   cross-language pinned, the disclosed parity envelope), and
2. an EXACT ``fractions.Fraction`` oracle at the production ``p = 0.5``
   (dyadic — the tail is an exact rational; tolerance 1e-10 log-units).
"""

from __future__ import annotations

import math
from fractions import Fraction

import pytest
from brutex.math import stats_binom_tail_log_sf
from hypothesis import given
from hypothesis import strategies as st


def _oracle_transcription(n: int, k: int, p: float) -> float:
    """Op-for-op Python transcription of the kernel's log-space recurrence."""
    if k == 0:
        return 0.0
    if k > n:
        return float("-inf")
    if p == 0.0:
        return float("-inf")
    if p == 1.0:
        return 0.0
    small = min(k, n - k)
    ln_c = 0.0
    for i in range(1, small + 1):
        ln_c += math.log((n - small + i) / i)
    ln_p = math.log(p)
    ln_q = math.log(1.0 - p)
    ln_term = ln_c + k * ln_p + (n - k) * ln_q
    acc = ln_term
    ln_odds = ln_p - ln_q
    for i in range(k, n):
        ln_term += math.log((n - i) / (i + 1)) + ln_odds
        hi, lo = (acc, ln_term) if acc >= ln_term else (ln_term, acc)
        acc = hi + math.log1p(math.exp(lo - hi))
    return min(acc, 0.0)


def _exact_log_tail_half(n: int, k: int) -> float:
    """EXACT ln P(X >= k | Binomial(n, 1/2)) via integer arithmetic."""
    if k > n:
        return float("-inf")
    tail = Fraction(sum(math.comb(n, i) for i in range(k, n + 1)), 2**n)
    return math.log(tail) if tail < 1 else 0.0


@given(
    n=st.integers(min_value=0, max_value=300),
    k=st.integers(min_value=0, max_value=310),
    p=st.one_of(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        st.sampled_from([0.0, 1.0, 0.5, 1e-12, 1.0 - 1e-12]),
    ),
)
def test_kernel_matches_transcription_over_hostile_domain(n: int, k: int, p: float) -> None:
    got = stats_binom_tail_log_sf(n, k, p)
    want = _oracle_transcription(n, k, p)
    if math.isinf(want):
        assert got == want
    else:
        # 1e-9 log-units — the disclosed cross-language libm envelope.
        assert got == pytest.approx(want, abs=1e-9)


@given(n=st.integers(min_value=1, max_value=120), data=st.data())
def test_kernel_matches_exact_fraction_oracle_at_half(n: int, data: st.DataObject) -> None:
    k = data.draw(st.integers(min_value=0, max_value=n))
    got = stats_binom_tail_log_sf(n, k, 0.5)
    want = _exact_log_tail_half(n, k)
    assert got == pytest.approx(want, abs=1e-10)


@given(n=st.integers(min_value=1, max_value=200))
def test_tail_is_monotone_non_increasing_in_k(n: int) -> None:
    prev = math.inf
    for k in range(n + 1):
        value = stats_binom_tail_log_sf(n, k, 0.5)
        assert value <= 0.0
        assert value <= prev + 1e-12
        prev = value


@given(
    n=st.integers(min_value=0, max_value=50),
    k=st.integers(min_value=0, max_value=60),
    p=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_kernel_is_deterministic(n: int, k: int, p: float) -> None:
    assert stats_binom_tail_log_sf(n, k, p) == stats_binom_tail_log_sf(n, k, p)
