"""Hypothesis differential for the fib-template express bits 69-73.

DEC-FIB-TEMPLATE-EXPRESS-001 §6: random snapshots — Some/None swing anchors,
degenerate ``range == 0`` fib anchors, bands ∈ {5, 10, 25, 50}, closes snapped
ONTO and ±1-paisa OFF every new level — must evaluate bit-identically across
the three FFI views:

1. ``evaluate_atoms`` — the production shim (Python marshal → Rust core);
2. ``evaluate_with_levels`` — the pre-built-levels entry point, called direct;
3. ``evaluate_from_raw`` — the independent in-Rust level recompute.

This extends the frozen 408-case golden harness (``tests/golden/
test_vocab_python_rust_bit_identity.py``) with generative coverage focused on
the five express bits; the case-dict shape and the three drivers are REUSED
from the golden modules (the ONE fixture grammar — CLAUDE.md §11 COMMON).

Whitebox: reuses the golden modules' private helpers per the DEC-P0-017
convention, so ``reportPrivateUsage`` is silenced file-wide.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import date
from typing import Any

from brutex.indicators.fib_levels import compute_fib_levels
from brutex.vocabulary.evaluator import evaluate_atoms
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.golden.test_vocab_golden import _rebuild
from tests.golden.test_vocab_python_rust_bit_identity import (
    _rust_from_raw,
    _rust_with_levels,
)


def _case(
    *,
    close_paisa: int,
    pdh_paisa: int,
    pdl_paisa: int,
    swing_high: int | None,
    swing_low: int | None,
    swing_bps: int,
    fib_bps: int,
) -> dict[str, Any]:
    """A language-neutral case dict in the golden fixture grammar."""
    lo = min(close_paisa, pdl_paisa) - 1_00
    hi = max(close_paisa, pdh_paisa) + 1_00
    return {
        "id": 0,
        "timestamp_ist": "2026-05-13T10:30:00+05:30",
        "open_paisa": close_paisa,
        "high_paisa": hi,
        "low_paisa": lo,
        "close_paisa": close_paisa,
        "ema20_paisa": None,
        "ema200_paisa": None,
        "prev_pdh_paisa": pdh_paisa,
        "prev_pdl_paisa": pdl_paisa,
        "prev_pdc_paisa": (pdh_paisa + pdl_paisa) // 2,
        "day_high_paisa": hi,
        "day_low_paisa": lo,
        "open_of_day_paisa": close_paisa,
        "proximity": {
            "pivot_bps": 10,
            "pdh_pdl_bps": 10,
            "fib_bps": fib_bps,
            "swing_bps": swing_bps,
        },
        "prior": {"prior_n": 3, "flags": []},
        "market_structure": {
            "bos_bullish": False,
            "bos_bearish": False,
            "choch_bullish": False,
            "choch_bearish": False,
        },
        "last_swing_high_paisa": swing_high,
        "last_swing_low_paisa": swing_low,
        "expected_mask": "0" * 32,  # unused — the property derives its own truth
    }


@st.composite
def _express_scenarios(draw: st.DrawFn) -> dict[str, Any]:
    """Express-focused scenarios: snapped-on/off closes + Some/None swings."""
    pdl = draw(st.integers(min_value=10_000_00, max_value=30_000_00))
    # range == 0 (pdh == pdl) is the degenerate-anchor arm — all fib levels
    # collapse onto the anchor, the express bits must STILL agree three ways.
    span = draw(st.sampled_from([0, 1_00, 50_00, 100_00, 500_00]))
    pdh = pdl + span
    fib = compute_fib_levels(date(2026, 5, 13), pdh, pdl)
    band = draw(st.sampled_from([5, 10, 25, 50]))
    fib_band = draw(st.sampled_from([5, 10, 25, 50]))

    # Close: snapped onto one of the new fib levels, ±1 paisa off it, or free.
    target_kind = draw(st.sampled_from(["bull23", "bull78", "up424", "dn424", "swing", "free"]))
    offset = draw(st.sampled_from([-1, 0, 1]))
    if target_kind == "bull23":
        close = fib.retrace_bull_23_paisa + offset
    elif target_kind == "bull78":
        close = fib.retrace_bull_78_paisa + offset
    elif target_kind == "up424":
        close = fib.ext_up_424_paisa + offset
    elif target_kind == "dn424":
        close = fib.ext_down_424_paisa + offset
    else:
        close = draw(st.integers(min_value=9_000_00, max_value=35_000_00))
    close = max(close, 1)

    # Swings: independently None or near/far from close (±1-paisa edges too).
    tol = (close * band) // 10_000
    swing_delta = draw(st.sampled_from([0, 1, tol, tol + 1, 5 * tol + 7]))
    swing_high = draw(st.sampled_from([None, close + swing_delta, close - swing_delta]))
    swing_low = draw(st.sampled_from([None, close - swing_delta, close + swing_delta]))
    if target_kind == "swing" and swing_high is None and swing_low is None:
        swing_high = close + swing_delta
    return _case(
        close_paisa=close,
        pdh_paisa=pdh,
        pdl_paisa=pdl,
        swing_high=swing_high,
        swing_low=swing_low,
        swing_bps=band,
        fib_bps=fib_band,
    )


class TestExpressDifferential:
    @given(case=_express_scenarios())
    @settings(max_examples=300, deadline=None)
    def test_three_ffi_views_agree_bit_for_bit(self, case: dict[str, Any]) -> None:
        snapshot, proximity, prior = _rebuild(case)
        shim = evaluate_atoms(snapshot, proximity, prior)
        with_levels = _rust_with_levels(case)
        from_raw = _rust_from_raw(case)
        assert shim == with_levels == from_raw, (
            f"divergence: shim={shim:#034x} with_levels={with_levels:#034x} "
            f"from_raw={from_raw:#034x} case={case!r}"
        )

    @given(case=_express_scenarios())
    @settings(max_examples=200, deadline=None)
    def test_none_swings_never_fire_72_73_and_snapped_closes_fire_their_bit(
        self, case: dict[str, Any]
    ) -> None:
        snapshot, proximity, prior = _rebuild(case)
        mask = evaluate_atoms(snapshot, proximity, prior)
        if snapshot.last_swing_high_paisa is None:
            assert not (mask >> 72) & 1  # never fabricated (the None-abstain law)
        if snapshot.last_swing_low_paisa is None:
            assert not (mask >> 73) & 1
        # The exact band law, re-derived independently per swing side.
        close = snapshot.close_paisa
        tol_swing = (close * proximity.swing_proximity_bps) // 10_000
        if snapshot.last_swing_high_paisa is not None and close > 0:
            expected = abs(close - snapshot.last_swing_high_paisa) <= tol_swing
            assert bool((mask >> 72) & 1) is expected
        if snapshot.last_swing_low_paisa is not None and close > 0:
            expected = abs(close - snapshot.last_swing_low_paisa) <= tol_swing
            assert bool((mask >> 73) & 1) is expected

    @given(case=_express_scenarios())
    @settings(max_examples=200, deadline=None)
    def test_fib_express_bits_match_the_band_law(self, case: dict[str, Any]) -> None:
        # Independent oracle for bits 69-71: the is_near law over the four
        # appended fib slots (69 ← bull23, 70 ← bull78, 71 ← up424 OR dn424).
        snapshot, proximity, prior = _rebuild(case)
        mask = evaluate_atoms(snapshot, proximity, prior)
        close = snapshot.close_paisa
        tol_fib = (close * proximity.fib_proximity_bps) // 10_000

        def near(level: int) -> bool:
            return close > 0 and abs(close - level) <= tol_fib

        fib = snapshot.fib
        assert bool((mask >> 69) & 1) is near(fib.retrace_bull_23_paisa)
        assert bool((mask >> 70) & 1) is near(fib.retrace_bull_78_paisa)
        assert bool((mask >> 71) & 1) is (
            near(fib.ext_up_424_paisa) or near(fib.ext_down_424_paisa)
        )
        # The reserved placeholders 66-68 and the retired/declared bits never
        # ride along (the grow-only law).
        assert mask & (0b111 << 66) == 0
