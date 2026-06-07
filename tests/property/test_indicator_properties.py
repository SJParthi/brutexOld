"""Property tests for the evaluate-time indicators (EMA, pivots, Fibonacci).

Two verification-pyramid invariants (``docs/TRACK1_VERIFICATION_PYRAMID.md`` §3),
promoted from the ``test_invariants_catalog.py`` ROADMAP stubs by
DEC-PROPERTY-INVARIANTS-LIVE-001 (``docs/MIGRATION.md``):

* **P-13** (``test_p13_ema_is_contraction``) — the EMA operator
  (:func:`brutex.indicators.ema.compute_ema`) is a contraction in the ``ℓ∞``
  norm: perturbing the input close series by at most ``δ`` perturbs every
  defined EMA value by at most ``δ``. Proves the smoother can never *amplify* an
  input difference (guarantee G4), so a single bad tick cannot blow up a level.
* **P-14** (``test_p14_pivot_levels_pure_no_state_leak`` /
  ``test_p14_fib_levels_pure_no_state_leak``) — pivot and Fibonacci levels are
  *pure functions* of the previous-day anchors (``(PDH, PDL, PDC)`` for pivots,
  ``(PDH, PDL)`` for Fib): identical anchors always yield identical levels
  (determinism), and the levels never depend on the ``trading_day`` they are
  computed for (no intraday / per-day state leak — guarantee G3).

All three target modules are sandbox-safe: they import the shared Rust
``trading_core`` crate (the single source of truth for the pivot/Fib math,
DEC-SHARED-CORE-001) but never :mod:`brutex.orchestrator`.
"""

from __future__ import annotations

from datetime import date

from brutex.indicators.ema import compute_ema
from brutex.indicators.fib_levels import (
    ALL_EXTENSION_KEYS,
    ALL_RETRACEMENT_KEYS,
    FibDirection,
    compute_fib_levels,
)
from brutex.indicators.pivots import compute_pivot_levels
from hypothesis import given
from hypothesis import strategies as st

# pyright: reportCallIssue=false
# Rationale (same as tests/property/test_engine_trade_sim_properties.py): the
# `@st.composite` strategies are invoked with no args because the decorator
# injects `draw` at runtime; pyright does not model the signature rewrite.

# ────────────────────────────────────────────────────────────────────────────
# P-13 — EMA is an ℓ∞ contraction
# ────────────────────────────────────────────────────────────────────────────

#: Bounded, finite close prices. The bound keeps the contraction's float
#: round-off (which scales with the magnitudes involved) safely inside the
#: tolerance below, while still spanning six orders of magnitude.
_CLOSE = st.floats(min_value=-1.0e6, max_value=1.0e6, allow_nan=False, allow_infinity=False)

#: Contraction tolerance: a small absolute floor plus a relative term, to absorb
#: the float64 round-off of the recursive update (``α·c + (1−α)·prev``) without
#: ever masking a genuine > δ amplification.
_CONTRACTION_ABS_TOL = 1.0e-6
_CONTRACTION_REL_TOL = 1.0e-9


@given(
    data=st.data(),
    period=st.integers(min_value=1, max_value=50),
)
def test_p13_ema_is_contraction(data: st.DataObject, period: int) -> None:
    """P-13: ``‖EMA(α,x) − EMA(α,y)‖∞ ≤ ‖x − y‖∞`` for any equal-length series."""
    n = data.draw(st.integers(min_value=1, max_value=80))
    x = data.draw(st.lists(_CLOSE, min_size=n, max_size=n))
    y = data.draw(st.lists(_CLOSE, min_size=n, max_size=n))

    # ℓ∞ distance of the inputs (the contraction bound).
    inf_in = max(abs(a - b) for a, b in zip(x, y, strict=True))

    ema_x = compute_ema(x, period)
    ema_y = compute_ema(y, period)

    tol = _CONTRACTION_ABS_TOL + inf_in * _CONTRACTION_REL_TOL
    # Equal-length series share an identical warm-up mask, so the two output
    # series go non-None at exactly the same positions; compare those.
    for a, b in zip(ema_x, ema_y, strict=True):
        if a is not None and b is not None:
            assert abs(a - b) <= inf_in + tol


# ────────────────────────────────────────────────────────────────────────────
# P-14 — pivot / Fib levels are pure functions of the anchors (no state leak)
# ────────────────────────────────────────────────────────────────────────────

#: Two independent trading days, used to prove the levels do not depend on the
#: day they are computed for.
_DAY = st.dates(min_value=date(2021, 1, 1), max_value=date(2026, 12, 31))

_PIVOT_KEYS = ("P", "R1", "R2", "R3", "R4", "S1", "S2", "S3", "S4")
_FIB_KEYS = (*ALL_RETRACEMENT_KEYS, *ALL_EXTENSION_KEYS)


@st.composite
def _pivot_anchors(draw: st.DrawFn) -> tuple[int, int, int]:
    """Draw a valid ``(pdh, pdl, pdc)`` triple in int64 paisa (``pdl ≤ pdc ≤ pdh``)."""
    pdl = draw(st.integers(min_value=1, max_value=100_000_000))
    pdh = pdl + draw(st.integers(min_value=0, max_value=100_000_000))
    pdc = draw(st.integers(min_value=pdl, max_value=pdh))
    return pdh, pdl, pdc


@st.composite
def _fib_anchors(draw: st.DrawFn) -> tuple[int, int]:
    """Draw a valid ``(pdh, pdl)`` pair in int64 paisa (``pdl ≤ pdh``)."""
    pdl = draw(st.integers(min_value=1, max_value=100_000_000))
    pdh = pdl + draw(st.integers(min_value=0, max_value=100_000_000))
    return pdh, pdl


@given(anchors=_pivot_anchors(), day1=_DAY, day2=_DAY)
def test_p14_pivot_levels_pure_no_state_leak(
    anchors: tuple[int, int, int], day1: date, day2: date
) -> None:
    """P-14: pivot levels depend only on ``(PDH, PDL, PDC)`` — deterministic, no day leak."""
    pdh, pdl, pdc = anchors
    first = compute_pivot_levels(day1, pdh, pdl, pdc)
    # Determinism: identical anchors → identical levels, bit-for-bit.
    assert compute_pivot_levels(day1, pdh, pdl, pdc) == first
    # No state leak: a DIFFERENT trading day yields identical level prices.
    other_day = compute_pivot_levels(day2, pdh, pdl, pdc)
    for key in _PIVOT_KEYS:
        assert other_day.get(key) == first.get(key)


@given(anchors=_fib_anchors(), day1=_DAY, day2=_DAY)
def test_p14_fib_levels_pure_no_state_leak(
    anchors: tuple[int, int], day1: date, day2: date
) -> None:
    """P-14: Fibonacci levels depend only on ``(PDH, PDL)`` — deterministic, no day leak."""
    pdh, pdl = anchors
    first = compute_fib_levels(day1, pdh, pdl)
    # Determinism: identical anchors → identical levels, bit-for-bit.
    assert compute_fib_levels(day1, pdh, pdl) == first
    # No state leak: a DIFFERENT trading day yields identical level prices in both
    # the bullish and bearish convention, plus identical anchors / range.
    other_day = compute_fib_levels(day2, pdh, pdl)
    for direction in FibDirection:
        for key in _FIB_KEYS:
            assert other_day.get(key, direction) == first.get(key, direction)
    assert (other_day.pdh_paisa, other_day.pdl_paisa, other_day.range_paisa) == (
        first.pdh_paisa,
        first.pdl_paisa,
        first.range_paisa,
    )
