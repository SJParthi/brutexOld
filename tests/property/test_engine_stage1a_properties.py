"""Property tests for the Stage-1A matrix builder (`brutex.engine.stage1a`).

Invariants (hold for any chronologically-ordered single-(instrument, interval)
bar sequence):

* **Determinism** (Golden Rule #4) — the same bars always produce the same
  ``bar_bits`` matrix, bit-for-bit; a fresh builder is stateless across runs.
* **1:1 shape** — exactly one mask per input bar; every mask uses only the
  IMPLEMENTED condition bits (``IMPLEMENTED_MASK`` — the 62 ACTIVE bits of
  the 66-declared layout after the vocab-66 reshape: the original 52 minus
  the RETIRED VWAP tombstones 52/53, plus pivot R5/S5 54/55, the LIVE
  BOS/CHoCH 56-59 and CPR 60-63; the declared-only SuperTrend bits 64/65,
  the tombstones and the u128 headroom stay always-zero).
* **No look-ahead** (CLAUDE.md §4) — mutating bars STRICTLY AFTER index ``k``
  never changes any mask at index ``≤ k`` (at bar N the builder reads only
  bars ``0..N``).
* **paisa-conversion monotonicity** — :func:`_price_to_paisa` is monotonic
  non-decreasing in the rupee input (snapping to the 2dp grid preserves order).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.stage1a import Stage1AMatrixBuilder, _price_to_paisa, build_bar_bits
from brutex.vocabulary.bits import IMPLEMENTED_MASK
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# pyright: reportCallIssue=false
# Rationale (DEC-PYRIGHT-UNMASK-001): the Hypothesis `@st.composite` strategies
# below are invoked with no args (e.g. `_bar_sequences()`) because the decorator
# injects `draw` at runtime; pyright does not model the `@composite` signature
# rewrite and emits a spurious "missing draw". Scoped to this file only —
# `reportCallIssue` stays a hard error everywhere else (same convention as
# tests/property/test_combo_hash_uniqueness.py).

# 09:15:00 IST == 03:45 UTC (session-open anchor).
_OPEN_UTC = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)

# Integer rupee "prices" → exact Decimals, comfortably within OHLC bounds.
_price = st.integers(min_value=1, max_value=1_000_000)


def _bar_from_oc(
    minute_offset: int, open_i: int, close_i: int, *, day_offset: int = 0
) -> CandleBar:
    """A valid 1-minute bar whose high/low span its open+close.

    ``day_offset`` advances whole calendar days so the property strategies can
    span IST day boundaries.
    """
    ts = _OPEN_UTC + timedelta(days=day_offset, minutes=minute_offset)
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_i),
        high=Decimal(max(open_i, close_i)),
        low=Decimal(min(open_i, close_i)),
        close=Decimal(close_i),
        volume=0,
        open_interest=None,
    )


@st.composite
def _bar_sequences(draw: st.DrawFn) -> list[CandleBar]:
    """Draw a chronological multi-day bar sequence (1..40 bars over 1-3 days)."""
    n = draw(st.integers(min_value=1, max_value=40))
    pairs = draw(
        st.lists(st.tuples(_price, _price), min_size=n, max_size=n),
    )
    bars: list[CandleBar] = []
    minute = 0
    day = 0
    for open_i, close_i in pairs:
        bars.append(_bar_from_oc(minute, open_i, close_i, day_offset=day))
        minute += 1
        if minute >= 15:  # roll to the next IST day periodically
            minute = 0
            day += 1
    return bars


# ────────────────────────────────────────────────────────────────────────
# paisa-conversion monotonicity
# ────────────────────────────────────────────────────────────────────────


@given(a=_price, b=_price)
def test_price_to_paisa_is_monotonic(a: int, b: int) -> None:
    lo, hi = (a, b) if a <= b else (b, a)
    assert _price_to_paisa(Decimal(lo)) <= _price_to_paisa(Decimal(hi))


@given(rupees=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1000000"), places=2))
def test_price_to_paisa_round_trips_2dp(rupees: Decimal) -> None:
    # A value already on the 2dp grid converts to paisa exactly (× 100).
    assert _price_to_paisa(rupees) == int(rupees * 100)


# ────────────────────────────────────────────────────────────────────────
# Determinism + shape
# ────────────────────────────────────────────────────────────────────────


@given(bars=_bar_sequences())
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_deterministic_and_one_to_one(bars: list[CandleBar]) -> None:
    first = build_bar_bits(bars)
    second = build_bar_bits(bars)
    assert first == second
    assert len(first) == len(bars)
    # Only IMPLEMENTED condition bits may fire — every other bit is always zero.
    # The bound is ``IMPLEMENTED_MASK``: 62 active bits of the 66-declared
    # layout (BOS/CHoCH 56-59 and CPR 60-63 are LIVE; the RETIRED VWAP
    # tombstones 52/53, the declared-only SuperTrend 64/65 and the unused
    # u128 headroom stay clear).
    assert all(0 <= m <= IMPLEMENTED_MASK for m in first)
    assert all(m & ~IMPLEMENTED_MASK == 0 for m in first)


@given(bars=_bar_sequences())
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_fresh_builder_matches_functional_wrapper(bars: list[CandleBar]) -> None:
    assert Stage1AMatrixBuilder().build(bars) == build_bar_bits(bars)


# ────────────────────────────────────────────────────────────────────────
# No look-ahead — perturbing the future cannot change the past
# ────────────────────────────────────────────────────────────────────────


@given(bars=_bar_sequences(), data=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_no_lookahead_future_mutation(bars: list[CandleBar], data: st.DataObject) -> None:
    if len(bars) < 2:
        return
    k = data.draw(st.integers(min_value=0, max_value=len(bars) - 2))
    # Replace every bar AFTER index k with arbitrary new (open, close) values,
    # keeping their original timestamps so the day structure is unchanged.
    mutated = list(bars)
    for j in range(k + 1, len(bars)):
        open_i = data.draw(_price)
        close_i = data.draw(_price)
        ts = bars[j].timestamp
        mutated[j] = CandleBar(
            timestamp=ts,
            open=Decimal(open_i),
            high=Decimal(max(open_i, close_i)),
            low=Decimal(min(open_i, close_i)),
            close=Decimal(close_i),
            volume=0,
            open_interest=None,
        )
    base = build_bar_bits(bars)
    perturbed = build_bar_bits(mutated)
    # Masks at indices 0..k depend only on bars 0..k → must be identical.
    assert base[: k + 1] == perturbed[: k + 1]
