"""Hypothesis properties for the Group-15 session-edge gap-midpoint level.

DEC-GAP-MIDPOINT-CONDITIONS-001 §3.7 (property row):

* the level lies INCLUSIVELY between its two source extremes —
  ``min(X1, X2) <= gap_mid <= max(X1, X2)`` — and is strictly interior
  whenever ``|X1 − X2| >= 2`` (a 1-paisa pair legally lands on the even
  endpoint under round-half-even);
* the DIRECTION law: a gap-UP day uses the HI pair, a gap-DOWN day the LO
  pair, and flipping the gap's sign flips the pair;
* zero gap (``open_of_day == pdc``) ⇒ ``None``, always;
* Rust-vs-Python parity: the ``brutex.indicators.gap_mid`` delegator (the
  shared-crate call) is bit-identical to an INDEPENDENT pure-Python
  integer round-half-even recompute — the delegation carries no arithmetic
  of its own to drift;
* the strictly-above / strictly-below bits can never co-fire (the
  registered conflict pair, exercised end-to-end through ``build_bar_bits``
  over arbitrary multi-day series).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.stage1a import build_bar_bits
from brutex.indicators.gap_mid import compute_gap_mid
from brutex.vocabulary import bits as B
from hypothesis import given, settings
from hypothesis import strategies as st

# pyright: reportCallIssue=false
# Rationale (DEC-PYRIGHT-UNMASK-001): the Hypothesis `@st.composite` strategy
# below is invoked with no args (the decorator injects `draw` at runtime);
# pyright does not model the `@composite` signature rewrite. Scoped to this
# file only — the tests/property house convention.

_IST = timezone(timedelta(hours=5, minutes=30))
_SESSION_OPEN_IST = datetime(2026, 1, 5, 9, 16, tzinfo=_IST)

#: Paisa magnitudes well inside the kernel's overflow-safe bar envelope
#: (`MAX_BAR_PAISA = i64::MAX / 65536`), including the tiny edge.
_paisa = st.integers(min_value=1, max_value=10**9)


@st.composite
def _candles(draw: st.DrawFn) -> tuple[int, int, int, int]:
    """(x1_hi, x1_lo, x2_hi, x2_lo) with each candle's hi >= lo."""
    x1_lo = draw(_paisa)
    x1_hi = x1_lo + draw(st.integers(min_value=0, max_value=10**7))
    x2_lo = draw(_paisa)
    x2_hi = x2_lo + draw(st.integers(min_value=0, max_value=10**7))
    return x1_hi, x1_lo, x2_hi, x2_lo


def _half_even_mid(a: int, b: int) -> int:
    """INDEPENDENT round-half-even midpoint over non-negative paisa ints.

    ``(a + b) / 2`` exactly: an even sum divides cleanly; an odd sum's
    ``k + 0.5`` ties to the even neighbour (``k`` when even, else ``k + 1``).
    Pure stdlib integer arithmetic — no trading_core call — so the parity
    property below genuinely cross-checks the Rust kernel.
    """
    half, rem = divmod(a + b, 2)
    return half if rem == 0 or half % 2 == 0 else half + 1


@given(candles=_candles(), open_pdc=st.tuples(_paisa, _paisa))
@settings(deadline=None)
def test_level_lies_inclusively_between_its_source_extremes(
    candles: tuple[int, int, int, int], open_pdc: tuple[int, int]
) -> None:
    x1_hi, x1_lo, x2_hi, x2_lo = candles
    open_of_day, pdc = open_pdc
    result = compute_gap_mid(
        x1_hi_paisa=x1_hi,
        x1_lo_paisa=x1_lo,
        x2_hi_paisa=x2_hi,
        x2_lo_paisa=x2_lo,
        open_of_day_paisa=open_of_day,
        pdc_paisa=pdc,
    )
    if open_of_day == pdc:
        assert result is None  # zero gap ⇒ no level, always
        return
    assert result is not None
    level, direction = result
    a, b = (x1_hi, x2_hi) if direction == 1 else (x1_lo, x2_lo)
    assert min(a, b) <= level <= max(a, b)
    if abs(a - b) >= 2:
        assert min(a, b) < level < max(a, b)  # strictly interior


@given(candles=_candles(), open_pdc=st.tuples(_paisa, _paisa))
@settings(deadline=None)
def test_direction_law_and_python_rust_parity(
    candles: tuple[int, int, int, int], open_pdc: tuple[int, int]
) -> None:
    """Gap-up ⇒ the HI pair, gap-down ⇒ the LO pair (independent recompute);
    swapping open/pdc flips the sign and therefore the pair."""
    x1_hi, x1_lo, x2_hi, x2_lo = candles
    open_of_day, pdc = open_pdc
    if open_of_day == pdc:
        return  # the zero-gap arm is pinned by the property above

    def _level(o: int, p: int) -> tuple[int, int]:
        result = compute_gap_mid(
            x1_hi_paisa=x1_hi,
            x1_lo_paisa=x1_lo,
            x2_hi_paisa=x2_hi,
            x2_lo_paisa=x2_lo,
            open_of_day_paisa=o,
            pdc_paisa=p,
        )
        assert result is not None
        return result

    level, direction = _level(open_of_day, pdc)
    expected_direction = 1 if open_of_day > pdc else -1
    assert direction == expected_direction
    expected_pair = (x1_hi, x2_hi) if direction == 1 else (x1_lo, x2_lo)
    assert level == _half_even_mid(*expected_pair)  # Rust == independent Python

    # Flipping the gap's sign (swap open/pdc) flips the pair.
    flipped_level, flipped_direction = _level(pdc, open_of_day)
    assert flipped_direction == -expected_direction
    flipped_pair = (x1_lo, x2_lo) if direction == 1 else (x1_hi, x2_hi)
    assert flipped_level == _half_even_mid(*flipped_pair)


# ── above/below mutual exclusion, end to end through the builder ──

_price = st.integers(min_value=1, max_value=1_000_000)
_BARS_PER_DAY = 6

_ABOVE_BELOW = (1 << B.BIT_CLOSE_ABOVE_GAP_MID) | (1 << B.BIT_CLOSE_BELOW_GAP_MID)


def _bar_at(day: int, minute: int, open_i: int, close_i: int) -> CandleBar:
    ts = (_SESSION_OPEN_IST + timedelta(days=day, minutes=minute)).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_i),
        high=Decimal(max(open_i, close_i)),
        low=Decimal(min(open_i, close_i)),
        close=Decimal(close_i),
        volume=100,
        open_interest=None,
    )


@given(
    pairs=st.lists(
        st.tuples(_price, _price), min_size=2 * _BARS_PER_DAY, max_size=4 * _BARS_PER_DAY
    )
)
@settings(deadline=None)
def test_close_above_and_below_gap_mid_never_cofire(
    pairs: list[tuple[int, int]],
) -> None:
    """Strictly-above and strictly-below are mutually exclusive under
    arbitrary multi-day bars (the registered conflicts pair, proven at the
    produced-mask level — never both bits in any bar's truth-vector)."""
    bars = [
        _bar_at(i // _BARS_PER_DAY, i % _BARS_PER_DAY, open_i, close_i)
        for i, (open_i, close_i) in enumerate(pairs)
    ]
    for mask in build_bar_bits(bars):
        assert mask & _ABOVE_BELOW != _ABOVE_BELOW
