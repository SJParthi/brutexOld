"""Stage-1B conflict-prune EXACTNESS proof (DEC-STAGE1B-CONFLICT-FIX-001).

The Stage-1B prefilter drops any mask containing a `CONFLICT_PAIR`
(`sweep_run.py:676`, `has_conflict`). For that prune to be **leaderboard-safe**
(never drop a strategy that could actually fire and rank), EVERY conflict pair
must be a *genuine* impossibility — no real bar can set both bits.

This property drives the REAL evaluator (`evaluate_atoms`) over randomized valid
bars and asserts the load-bearing invariant:

    for ANY real bar:  has_conflict(evaluate_atoms(bar)) is False

i.e. no conflict pair is ever co-set on a real bar ⟺ pruning conflict-masks can
never drop a fireable mask ⟺ the prune is EXACT (leaderboard-preserving).

It also pins the DEC-STAGE1B-CONFLICT-FIX-001 fix as regressions:
* (BULLISH, DOJI) / (BEARISH, DOJI) are NOT conflicts — a tiny-bodied directional
  bar is both (the evaluator's DOJI is `body/range < 0.1`, not `open==close`).
  Listing them as conflicts WRONGLY pruned valid masks (the bug this fixes).
* the four NEW candle conflicts (doji⊥large-body, the wick pairs) never co-occur.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from brutex.indicators.fib_levels import compute_fib_levels
from brutex.indicators.pivots import compute_pivot_levels
from brutex.vocabulary import (
    IndicatorSnapshot,
    PriorBarHistory,
    ProximityParams,
    evaluate_atoms,
)
from brutex.vocabulary import (
    bits as B,
)
from brutex.vocabulary.conflicts import CONFLICT_PAIRS, has_conflict
from hypothesis import given, settings
from hypothesis import strategies as st

_IST = ZoneInfo("Asia/Kolkata")
_PROX = ProximityParams(pivot_proximity_bps=10, pdh_pdl_proximity_bps=10, fib_proximity_bps=10)


def _snapshot(
    *,
    o: int,
    c: int,
    h: int,
    lo: int,
    ema20: int,
    ema200: int,
    pdh: int,
    pdl: int,
    pdc: int,
    day_hi: int,
    day_lo: int,
    ood: int,
    minute: int,
) -> IndicatorSnapshot:
    ts = datetime(2026, 5, 13, 9, 15, tzinfo=_IST).replace(
        hour=9 + minute // 60, minute=minute % 60
    )
    today = ts.date()
    return IndicatorSnapshot(
        timestamp_ist=ts,
        open_paisa=o,
        high_paisa=h,
        low_paisa=lo,
        close_paisa=c,
        ema20_paisa=ema20,
        ema200_paisa=ema200,
        pivots=compute_pivot_levels(today, pdh, pdl, pdc),
        fib=compute_fib_levels(today, pdh, pdl),
        day_high_paisa=day_hi,
        day_low_paisa=day_lo,
        open_of_day_paisa=ood,
    )


_price = st.integers(min_value=23_000_00, max_value=25_000_00)
# half >= 1 → range >= 2 paisa (a real bar always has positive range; a fully
# degenerate zero-range bar/day is a separate, pre-existing edge — not in scope).
_span = st.integers(min_value=1, max_value=4_000)  # 0.02..40 rupees half-range
_minute = st.integers(min_value=0, max_value=375)  # 09:15 → 15:30 window


@given(
    center=_price,
    half=_span,
    o_off=st.floats(min_value=0.0, max_value=1.0),
    c_off=st.floats(min_value=0.0, max_value=1.0),
    ema20=_price,
    ema200=_price,
    pd_span=st.integers(min_value=1, max_value=5_000),
    pdc_off=st.floats(min_value=0.0, max_value=1.0),
    minute=_minute,
)
@settings(max_examples=600)
def test_conflict_prune_is_exact_over_real_bars(
    center: int,
    half: int,
    o_off: float,
    c_off: float,
    ema20: int,
    ema200: int,
    pd_span: int,
    pdc_off: float,
    minute: int,
) -> None:
    """No real bar ever co-sets a conflict pair → the Stage-1B prune is exact."""
    lo = center - half
    hi = center + half
    o = round(lo + o_off * (hi - lo))
    c = round(lo + c_off * (hi - lo))
    # day high/low bracket this bar; prior-day high > low; pdc inside the prior range.
    pdl = center - pd_span
    pdh = center + pd_span
    pdc = round(pdl + pdc_off * (pdh - pdl))
    mask = evaluate_atoms(
        _snapshot(
            o=o,
            c=c,
            h=hi,
            lo=lo,
            ema20=ema20,
            ema200=ema200,
            pdh=pdh,
            pdl=pdl,
            pdc=pdc,
            day_hi=hi,
            day_lo=lo,
            ood=center,
            minute=minute,
        ),
        _PROX,
        PriorBarHistory(prior_n=3, flags=[]),
    )
    # THE invariant: a real bar can never satisfy any conflict pair.
    assert not has_conflict(mask), (
        f"a real bar produced a conflict pair (mask={mask:#x}) — the Stage-1B "
        f"prune would WRONGLY drop a fireable strategy (leaderboard bug)"
    )


def _eval(o: int, c: int, h: int, lo: int) -> int:
    return evaluate_atoms(
        _snapshot(
            o=o,
            c=c,
            h=h,
            lo=lo,
            ema20=24_000_00,
            ema200=23_900_00,
            pdh=24_050_00,
            pdl=23_950_00,
            pdc=24_000_00,
            day_hi=h,
            day_lo=lo,
            ood=24_000_00,
            minute=75,
        ),
        _PROX,
        PriorBarHistory(prior_n=3, flags=[]),
    )


def test_bullish_and_bearish_doji_are_not_conflicts() -> None:
    """Regression (DEC-STAGE1B-CONFLICT-FIX-001): a tiny-bodied directional bar is
    BOTH directional AND a doji, so these pairs must NOT be pruned."""
    bull = _eval(24_000_00, 24_001_00, 24_020_00, 24_000_00)  # tiny +body, big range
    assert (bull >> B.BIT_BAR_BULLISH) & 1 and (bull >> B.BIT_BAR_DOJI) & 1
    bear = _eval(24_001_00, 24_000_00, 24_020_00, 24_000_00)  # tiny -body
    assert (bear >> B.BIT_BAR_BEARISH) & 1 and (bear >> B.BIT_BAR_DOJI) & 1
    assert (B.BIT_BAR_BULLISH, B.BIT_BAR_DOJI) not in CONFLICT_PAIRS
    assert (B.BIT_BAR_BEARISH, B.BIT_BAR_DOJI) not in CONFLICT_PAIRS


def test_new_candle_conflicts_are_registered() -> None:
    """The four verified UNCONDITIONAL candle conflicts are in the set."""
    for pair in (
        (B.BIT_BAR_DOJI, B.BIT_BAR_LARGE_BODY),
        (B.BIT_LONG_UPPER_WICK, B.BIT_LONG_LOWER_WICK),
        (B.BIT_BAR_LARGE_BODY, B.BIT_LONG_UPPER_WICK),
        (B.BIT_BAR_LARGE_BODY, B.BIT_LONG_LOWER_WICK),
    ):
        assert pair in CONFLICT_PAIRS
