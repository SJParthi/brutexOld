"""Unit + property tests for the BOS/CHoCH market-structure state (DEC-NIFTY-BOS-CHOCH-001).

Covers the ADR-0012 D3/D4 contract: fractal swing confirmation (a swing is only
exposed k bars LATER), BOS = continuation break in the prevailing trend, CHoCH =
first break opposite the trend (reversal), mutual exclusivity per side, the O(1)
bounded ring, warm-up, ``k`` validation, and serialize/restore. 100% line +
branch on ``brutex.indicators.market_structure``. The look-ahead (P-06) proof
lives in ``tests/property/test_market_structure_lookahead.py``.
"""

from __future__ import annotations

import pytest
from brutex.indicators.market_structure import (
    MarketStructureSignal,
    MarketStructureState,
    Trend,
)
from hypothesis import given
from hypothesis import strategies as st


def _feed(m: MarketStructureState, bars: list[tuple[int, int, int]]) -> MarketStructureSignal:
    """Feed every (high, low, close) bar; return the LAST bar's signal."""
    sig = MarketStructureSignal()
    for h, low, c in bars:
        sig = m.update(h, low, c)
    return sig


# ── construction / validation ────────────────────────────────────────────────


def test_default_k_is_two() -> None:
    assert MarketStructureState().k == 2


def test_k_zero_raises() -> None:
    with pytest.raises(ValueError, match="k must be >= 1"):
        MarketStructureState(k=0)


def test_k_negative_raises() -> None:
    with pytest.raises(ValueError, match="k must be >= 1"):
        MarketStructureState(k=-1)


# ── warm-up: no signal before a confirmed swing ──────────────────────────────


def test_no_signal_before_any_confirmed_swing() -> None:
    m = MarketStructureState(k=2)
    # Fewer than 2k+1 = 5 bars → nothing can be confirmed.
    for h, low, c in [(100, 90, 95), (200, 80, 150), (300, 70, 250)]:
        assert m.update(h, low, c) == MarketStructureSignal()
    assert m.last_swing_high is None
    assert m.last_swing_low is None
    assert m.trend is Trend.NONE


def test_flat_window_confirms_no_swing() -> None:
    m = MarketStructureState(k=2)
    for _ in range(6):
        assert m.update(100, 100, 100) == MarketStructureSignal()
    assert m.last_swing_high is None
    assert m.last_swing_low is None


# ── BOS (continuation) ───────────────────────────────────────────────────────


def test_bullish_bos_after_confirmed_swing_high() -> None:
    m = MarketStructureState(k=2)
    # A swing HIGH at bar 2 (the highest), confirmed by bars 3 & 4.
    last = _feed(
        m,
        [
            (100, 90, 95),
            (110, 100, 105),
            (130, 120, 125),  # swing-high candidate
            (115, 105, 110),
            (112, 102, 108),  # confirms bar 2 as swing high (130)
        ],
    )
    assert not last.bos_bullish  # the confirming bar's close (108) didn't break 130
    assert m.last_swing_high == 130
    sig = m.update(140, 120, 135)  # close 135 > 130 → bullish BOS
    assert sig.bos_bullish
    assert not sig.choch_bullish
    assert m.trend is Trend.UP


def test_bearish_bos_after_confirmed_swing_low() -> None:
    m = MarketStructureState(k=2)
    _feed(
        m,
        [
            (130, 120, 125),
            (120, 110, 115),
            (100, 80, 90),  # swing-low candidate
            (115, 105, 110),
            (118, 108, 112),  # confirms bar 2 as swing low (80)
        ],
    )
    assert m.last_swing_low == 80
    sig = m.update(90, 70, 75)  # close 75 < 80 → bearish BOS
    assert sig.bos_bearish
    assert not sig.choch_bearish
    assert m.trend is Trend.DOWN


def test_continuation_bos_in_uptrend_is_not_choch() -> None:
    m = MarketStructureState(k=2)
    _feed(
        m,
        [
            (100, 90, 95),
            (110, 100, 105),
            (130, 120, 125),  # swing-high (130)
            (115, 105, 110),
            (112, 102, 108),  # confirms 130
        ],
    )
    first = m.update(140, 120, 135)  # break 130 → Up (BOS)
    assert first.bos_bullish
    assert m.trend is Trend.UP
    # Confirm a NEW higher swing high, then break it again → another BOS (still Up).
    _feed(m, [(150, 130, 145), (142, 122, 128), (141, 121, 127)])  # confirms 150
    assert m.last_swing_high == 150
    second = m.update(160, 140, 155)  # break 150 while UP → BOS, not CHoCH
    assert second.bos_bullish
    assert not second.choch_bullish
    assert m.trend is Trend.UP


# ── CHoCH (reversal) ─────────────────────────────────────────────────────────


def test_bullish_choch_reverses_a_downtrend() -> None:
    m = MarketStructureState(k=2)
    _feed(
        m,
        [
            (100, 90, 95),
            (110, 100, 105),
            (130, 120, 125),  # swing-high (130)
            (118, 95, 100),
            (116, 85, 90),  # confirms swing-high@2
            (108, 70, 75),  # swing-low candidate (70)
            (112, 100, 105),
            (114, 102, 108),  # confirms swing-low@5 (70)
        ],
    )
    assert m.last_swing_high == 130
    assert m.last_swing_low == 70
    bear = m.update(90, 60, 65)  # close 65 < 70 → Down (bearish BOS, no trend yet)
    assert bear.bos_bearish
    trend_after_bear: Trend = m.trend
    assert trend_after_bear == Trend.DOWN
    choch = m.update(140, 120, 135)  # close 135 > 130 while DOWN → bullish CHoCH
    assert choch.choch_bullish
    assert not choch.bos_bullish
    trend_after_choch: Trend = m.trend
    assert trend_after_choch == Trend.UP


def test_bearish_choch_reverses_an_uptrend() -> None:
    m = MarketStructureState(k=2)
    _feed(
        m,
        [
            (130, 120, 125),
            (120, 110, 115),
            (100, 80, 90),  # swing-low (80)
            (118, 108, 112),
            (140, 110, 135),  # confirms swing-low@2 (80)
            (150, 130, 145),  # swing-high candidate (150)
            (138, 118, 122),
            (136, 116, 120),  # confirms swing-high@5 (150)
        ],
    )
    assert m.last_swing_low == 80
    assert m.last_swing_high == 150
    bull = m.update(160, 140, 155)  # break 150 → Up (bullish BOS)
    assert bull.bos_bullish
    trend_after_bull: Trend = m.trend
    assert trend_after_bull == Trend.UP
    choch = m.update(90, 60, 65)  # close 65 < 80 while UP → bearish CHoCH
    assert choch.choch_bearish
    assert not choch.bos_bearish
    trend_after_choch2: Trend = m.trend
    assert trend_after_choch2 == Trend.DOWN


# ── up-side / down-side are resolved independently ───────────────────────────


def test_up_side_break_does_not_fire_down_side() -> None:
    m = MarketStructureState(k=2)
    # Confirm a swing low (90) at bar 2, then a swing high (150) at bar 5, so BOTH
    # `last_swing_low` and `last_swing_high` are set before the break bar.
    _feed(
        m,
        [
            (120, 110, 115),
            (118, 108, 112),
            (116, 90, 100),  # swing-low candidate (90)
            (117, 107, 110),
            (119, 109, 114),  # confirms swing-low@2 (90)
            (150, 130, 140),  # swing-high candidate (150)
            (140, 120, 130),
            (141, 121, 131),  # confirms swing-high@5 (150)
        ],
    )
    assert m.last_swing_high == 150
    assert m.last_swing_low == 90
    # A single close is ONE number: breaking above 150 fires ONLY the up-side; the
    # down `if` (close < 90) is independently false — proving the two sides resolve
    # from separate branches (not coupled).
    sig = m.update(160, 60, 155)  # close 155 > 150; 155 is NOT < 90
    assert sig.bos_bullish
    assert not sig.bos_bearish
    assert not sig.choch_bearish


# ── mutual exclusivity per side (property) ───────────────────────────────────


@given(
    bars=st.lists(
        st.tuples(
            st.integers(min_value=50, max_value=500),
            st.integers(min_value=1, max_value=49),
            st.integers(min_value=1, max_value=500),
        ),
        min_size=1,
        max_size=80,
    ),
    k=st.integers(min_value=1, max_value=4),
)
def test_per_side_mutual_exclusivity(bars: list[tuple[int, int, int]], k: int) -> None:
    """A single bar never fires BOTH bos_bullish AND choch_bullish (same side),
    nor BOTH bos_bearish AND choch_bearish — the BOS-vs-CHoCH partition is on the
    prevailing trend, so exactly one of each pair is possible per break."""
    m = MarketStructureState(k=k)
    for h, low, c in bars:
        sig = m.update(max(h, low), low, c)
        assert not (sig.bos_bullish and sig.choch_bullish)
        assert not (sig.bos_bearish and sig.choch_bearish)


@given(
    bars=st.lists(
        st.tuples(
            st.integers(min_value=50, max_value=500),
            st.integers(min_value=1, max_value=49),
            st.integers(min_value=1, max_value=500),
        ),
        min_size=6,
        max_size=80,
    )
)
def test_signal_implies_a_confirmed_swing_exists(bars: list[tuple[int, int, int]]) -> None:
    """Any bullish break implies a confirmed swing high existed; any bearish break
    implies a confirmed swing low existed (no signal can fire on `None`)."""
    m = MarketStructureState(k=2)
    for h, low, c in bars:
        sig = m.update(max(h, low), low, c)
        if sig.bos_bullish or sig.choch_bullish:
            assert m.last_swing_high is not None
        if sig.bos_bearish or sig.choch_bearish:
            assert m.last_swing_low is not None


# ── O(1) memory: the ring is bounded ─────────────────────────────────────────


def test_ring_is_bounded() -> None:
    m = MarketStructureState(k=2)
    for i in range(1000):
        m.update(100 + i, 50 + i, 75 + i)
    assert len(m.window) == 5  # 2k+1
    assert m.count == 1000


@given(k=st.integers(min_value=1, max_value=6), n=st.integers(min_value=0, max_value=200))
def test_ring_never_exceeds_window(k: int, n: int) -> None:
    m = MarketStructureState(k=k)
    for i in range(n):
        m.update(100 + i, 50 + i, 75 + i)
    assert len(m.window) <= 2 * k + 1
    assert m.count == n


# ── reset / serialize-restore ────────────────────────────────────────────────


def test_reset_clears_state() -> None:
    m = MarketStructureState(k=2)
    _feed(m, [(130, 120, 125), (120, 110, 115), (100, 80, 90), (118, 108, 112), (140, 110, 135)])
    m.reset()
    assert m.count == 0
    assert len(m.window) == 0
    assert m.last_swing_high is None
    assert m.last_swing_low is None
    assert m.trend is Trend.NONE


def test_to_from_dict_roundtrip_after_swings() -> None:
    m = MarketStructureState(k=3)
    _feed(
        m,
        [
            (100, 90, 95),
            (110, 100, 105),
            (130, 120, 125),
            (140, 130, 135),  # swing-high (140)
            (120, 110, 115),
            (118, 108, 112),
            (116, 106, 110),  # confirms 140
        ],
    )
    restored = MarketStructureState.from_dict(m.to_dict())
    assert restored == m
    assert restored.last_swing_high == m.last_swing_high
    # The restored state continues identically from here.
    a = m.update(150, 130, 145)
    b = restored.update(150, 130, 145)
    assert a == b


def test_to_from_dict_roundtrip_cold() -> None:
    m = MarketStructureState(k=2)
    restored = MarketStructureState.from_dict(m.to_dict())
    assert restored == m
    assert restored.last_swing_high is None
    assert restored.last_swing_low is None
    assert restored.trend is Trend.NONE


def test_to_dict_serializes_trend_and_window() -> None:
    m = MarketStructureState(k=2)
    _feed(m, [(130, 120, 125), (120, 110, 115), (100, 80, 90), (118, 108, 112), (140, 110, 135)])
    m.update(150, 130, 145)  # establish a trend
    d = m.to_dict()
    assert d["trend"] in {"none", "up", "down"}
    assert d["k"] == 2
    assert all(isinstance(b, list) and len(b) == 2 for b in d["window"])


def test_signal_is_frozen() -> None:
    sig = MarketStructureSignal(bos_bullish=True)
    with pytest.raises((AttributeError, Exception)):
        sig.bos_bullish = False  # type: ignore[misc]  # frozen dataclass


def test_default_signal_is_all_false() -> None:
    sig = MarketStructureSignal()
    assert not (sig.bos_bullish or sig.bos_bearish or sig.choch_bullish or sig.choch_bearish)
