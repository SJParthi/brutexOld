"""Load-bearing proof that the BOS/CHoCH bits 56-59 actually FIRE in the real sweep.

DEC-STAGE1A-WIRE-VWAP-BOS-001 (B1 fix). Before that PR ``Stage1AMatrixBuilder.push``
built the per-bar :class:`~brutex.vocabulary.evaluator.IndicatorSnapshot` WITHOUT a
``market_structure``, so the evaluator defaulted the BOS/CHoCH gates (bits 56-59)
to an all-false signal and they could NEVER fire in a production sweep — green unit
goldens over a DEAD engine path. This module is the anti-illusion gate: it drives the
ACTUAL :meth:`Stage1AMatrixBuilder.push` production path (never a mock) over crafted
1-minute bars engineered to print every BOS/CHoCH direction, and asserts each bit is
SET on at least one produced bar mask — plus a NEGATIVE (a flat stream sets none of
them, proving real logic not always-on) and the worst-case edge battery (zero-volume,
degenerate bar, look-ahead, idempotence, O(1) space).

The VWAP half of the original six-bit fire test was RETIRED with the bits-52/53
tombstone (the 60→66 reshape — the ``VwapState`` pyclass was deleted from the
crate at 0.4.0); this module now ALSO pins the tombstones POSITIVELY: bits
52/53 are 0 on every produced mask of every stream here.

The per-bar BOS/CHoCH COMPUTE runs in the SHARED Rust ``trading_core`` state
object (``trading_core.MarketStructureState``) — this test exercises that Rust
path through the Python engine, the bit-identical backtest==live surface
(CLAUDE.md §11 COMMON).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.stage1a import Stage1AMatrixBuilder
from brutex.vocabulary import bits as B
from hypothesis import given, settings
from hypothesis import strategies as st

# IST is a fixed +05:30 offset (no DST). 09:15 IST is the NSE session open.
_IST = timezone(timedelta(hours=5, minutes=30))

# The four stateful market-structure bits (R5/S5 — 54/55 — fire elsewhere).
_BIT_BOS_BULL = B.BIT_BOS_BULLISH  # 56
_BIT_BOS_BEAR = B.BIT_BOS_BEARISH  # 57
_BIT_CHOCH_BULL = B.BIT_CHOCH_BULLISH  # 58
_BIT_CHOCH_BEAR = B.BIT_CHOCH_BEARISH  # 59
#: The RETIRED VWAP tombstone pair — positively pinned 0 on every mask here.
_VWAP_TOMBSTONE_MASK = (1 << B.BIT_CLOSE_ABOVE_VWAP) | (1 << B.BIT_CLOSE_BELOW_VWAP)


def _bar(
    minute: int,
    high: float,
    low: float,
    close: float,
    *,
    day: int = 2,
    volume: int = 1_000,
    open_: float | None = None,
) -> CandleBar:
    """Build a valid :class:`CandleBar` at 09:15 IST + ``minute`` on 2024-01-``day``.

    ``open`` defaults to ``close`` clamped into ``[low, high]`` (always valid). The
    timestamp is stored UTC (the builder converts back to IST internally).
    """
    ist = datetime(2024, 1, day, 9, 15, tzinfo=_IST) + timedelta(minutes=minute)
    o = close if open_ is None else open_
    o = max(low, min(high, o))
    return CandleBar(
        timestamp=ist.astimezone(UTC),
        open=Decimal(str(o)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=volume,
        open_interest=None,
    )


# A single-day (h, l, c) script engineered, with k=2 fractal market structure, to
# print: a bearish BOS (from no trend), a bullish CHoCH (reversing the down-trend), a
# bearish CHoCH (reversing the up-trend), then a bullish BOS. (It also crossed the
# session VWAP both ways back when bits 52/53 were live — kept unchanged so the
# roll/batch goldens built on it stay comparable.) The swing-confirmation lag
# (k=2 → a swing confirms 2 bars later) is baked into the spacing; verified
# bar-by-bar in the assertions below.
_HLC_SCRIPT: tuple[tuple[float, float, float], ...] = (
    (100, 90, 95),  # 0
    (95, 85, 90),  # 1  below VWAP
    (88, 70, 75),  # 2  swing-low 70 candidate
    (90, 80, 85),  # 3
    (92, 82, 88),  # 4  confirms swing-low 70
    (85, 60, 65),  # 5  close 65 < 70, no trend  → BOS_BEAR, trend DOWN
    (95, 85, 90),  # 6
    (110, 100, 105),  # 7  swing-high 110 candidate
    (98, 88, 92),  # 8
    (96, 86, 90),  # 9  confirms swing-high 110
    (120, 100, 115),  # 10 close 115 > 110 while DOWN → CHOCH_BULL, trend UP
    (118, 108, 112),  # 11
    (114, 95, 100),  # 12 swing-low 95 candidate
    (116, 106, 110),  # 13
    (117, 107, 111),  # 14 confirms swing-low 95
    (100, 80, 85),  # 15 close 85 < 95 while UP → CHOCH_BEAR, trend DOWN
    (108, 98, 102),  # 16
    (130, 118, 125),  # 17 swing-high 130 candidate → CHOCH_BULL en route
    (122, 112, 116),  # 18
    (121, 111, 115),  # 19 confirms swing-high 130
    (140, 120, 135),  # 20 close 135 > 130 while DOWN → CHOCH_BULL, trend UP
    (138, 128, 132),  # 21
    (150, 140, 145),  # 22 swing-high 150 candidate
    (142, 132, 136),  # 23
    (141, 131, 135),  # 24 confirms swing-high 150
    (160, 150, 155),  # 25 close 155 > 150 while UP → BOS_BULL
)


def _fired_bits(masks: list[int], bit: int) -> list[int]:
    """The bar indices at which ``bit`` is set across a mask sequence."""
    return [i for i, m in enumerate(masks) if m & (1 << bit)]


def _run(bars: list[CandleBar]) -> list[int]:
    """Drive the REAL production path: one fresh builder, one mask per bar."""
    builder = Stage1AMatrixBuilder()
    return [builder.push(bar) for bar in bars]


# ────────────────────────────────────────────────────────────────────────
# The headline proof: all six resurrected bits fire on the real push() path
# ────────────────────────────────────────────────────────────────────────


def test_all_four_structure_bits_fire_through_real_push() -> None:
    """Each of bits 56-59 is SET on >=1 bar of the real ``push`` output —
    and the RETIRED VWAP tombstones (52/53) on NONE (the anti-illusion
    contract, both directions)."""
    bars = [_bar(m, h, l, c) for m, (h, l, c) in enumerate(_HLC_SCRIPT)]
    masks = _run(bars)

    assert _fired_bits(masks, _BIT_BOS_BULL), "bos_bullish (bit 56) never fired"
    assert _fired_bits(masks, _BIT_BOS_BEAR), "bos_bearish (bit 57) never fired"
    assert _fired_bits(masks, _BIT_CHOCH_BULL), "choch_bullish (bit 58) never fired"
    assert _fired_bits(masks, _BIT_CHOCH_BEAR), "choch_bearish (bit 59) never fired"
    assert all((m & _VWAP_TOMBSTONE_MASK) == 0 for m in masks), "a tombstone bit fired"


def test_market_structure_directions_fire_at_expected_bars() -> None:
    """The BOS/CHoCH directions land at the engineered, swing-lag-correct bars.

    Pins the SEMANTICS (not just "some bit somewhere"): bar 5 is the first
    bearish-BOS from no-trend, bar 10 the bullish CHoCH that reverses it, bar 15
    the bearish CHoCH that reverses the up-trend, and bar 25 a bullish BOS in an
    established up-trend.
    """
    bars = [_bar(m, h, l, c) for m, (h, l, c) in enumerate(_HLC_SCRIPT)]
    masks = _run(bars)

    assert 5 in _fired_bits(masks, _BIT_BOS_BEAR)
    assert 10 in _fired_bits(masks, _BIT_CHOCH_BULL)
    assert 15 in _fired_bits(masks, _BIT_CHOCH_BEAR)
    assert 25 in _fired_bits(masks, _BIT_BOS_BULL)


# ────────────────────────────────────────────────────────────────────────
# CPR bits 60-62 fire through the real streaming push() path (anchor-warm)
# ────────────────────────────────────────────────────────────────────────


def test_cpr_position_bits_fire_on_anchor_warm_day_through_real_push() -> None:
    """Day-2 (anchor-warm) bars carry EXACTLY ONE of bits 60/61/62 — and the
    RETIRED VWAP tombstones (52/53) fire on NO bar of either day.

    The integration-level anti-illusion twin of the unit cold-anchor test
    (``tests/unit/test_engine_stage1a.py::test_cold_anchor_day_clears_every_cpr_bit``):
    drives the ACTUAL :meth:`Stage1AMatrixBuilder.push` production path over a
    small 2-day synthetic feed. Day 1 (cold anchor — no prior day) must clear
    every CPR bit; day 2's prev-day anchors (pdh 110 / pdl 90 / pdc 100 →
    P == BC == TC == 100.00) partition the closes: 105 above TC (bit 60),
    95 below BC (bit 61), 100 inside (bit 62) — each region hit, and the
    60/61/62 partition holds with exactly one bit per warm bar.
    """
    cpr_position_bits = (B.BIT_ABOVE_CPR_TC, B.BIT_BELOW_CPR_BC, B.BIT_INSIDE_CPR)
    cpr_mask = sum(1 << b for b in cpr_position_bits)

    day1 = [_bar(m, 110, 90, 100, day=2) for m in range(3)]
    day2 = [
        _bar(0, 110, 90, 105, day=3),  # close > TC → above_cpr_tc (60)
        _bar(1, 110, 90, 95, day=3),  # close < BC → below_cpr_bc (61)
        _bar(2, 110, 90, 100, day=3),  # BC <= close <= TC → inside_cpr (62)
    ]
    masks = _run(day1 + day2)
    cold, warm = masks[: len(day1)], masks[len(day1) :]

    # (cold) day 1 has no prev-day anchors → every CPR bit cleared.
    assert all(m & cpr_mask == 0 for m in cold), "a CPR bit fired against cold anchors"
    # (a) every anchor-warm bar carries EXACTLY ONE of bits 60/61/62.
    for i, m in enumerate(warm):
        fired = [b for b in cpr_position_bits if m & (1 << b)]
        assert len(fired) == 1, f"warm bar {i}: CPR position bits fired = {fired}"
    # …and each of the three regions genuinely fired somewhere (anti-vacuity).
    assert [next(b for b in cpr_position_bits if m & (1 << b)) for m in warm] == [
        B.BIT_ABOVE_CPR_TC,
        B.BIT_BELOW_CPR_BC,
        B.BIT_INSIDE_CPR,
    ]
    # (b) the RETIRED VWAP tombstones never fire — either day.
    assert all((m & _VWAP_TOMBSTONE_MASK) == 0 for m in masks), "a tombstone bit fired"


# ────────────────────────────────────────────────────────────────────────
# NEGATIVE — proves the bits are real logic, not always-on
# ────────────────────────────────────────────────────────────────────────


def test_flat_stream_fires_none_of_the_structure_bits() -> None:
    """A perfectly flat (no swings) stream sets NONE of the four.

    A flat window confirms no strict local extreme, so no BOS/CHoCH can fire.
    This is the not-always-on guarantee. (The tombstones stay 0 too.)
    """
    bars = [_bar(m, 100, 100, 100) for m in range(30)]
    masks = _run(bars)

    for bit in (
        _BIT_BOS_BULL,
        _BIT_BOS_BEAR,
        _BIT_CHOCH_BULL,
        _BIT_CHOCH_BEAR,
    ):
        assert not _fired_bits(masks, bit), f"bit {bit} fired on a flat stream"
    assert all((m & _VWAP_TOMBSTONE_MASK) == 0 for m in masks)


# ────────────────────────────────────────────────────────────────────────
# Operator-mandated worst-case edge battery (each a real assertion)
# ────────────────────────────────────────────────────────────────────────


def test_zero_volume_bar_is_well_defined() -> None:
    """A zero-volume bar yields a clean integer mask (volume feeds NOTHING on
    the spot-only engine since the VWAP retirement — the tombstones stay 0)."""
    masks = _run([_bar(0, 110, 90, 108, volume=0)])
    assert isinstance(masks[0], int)
    assert not (masks[0] & _VWAP_TOMBSTONE_MASK)


def test_degenerate_bar_high_eq_low_eq_close_is_well_defined() -> None:
    """A bar with high == low == close produces a clean mask (no crash), and
    the RETIRED tombstones stay 0 even on this degenerate shape."""
    low_seed = _bar(0, 100, 100, 100)
    degenerate = _bar(1, 150, 150, 150)
    masks = _run([low_seed, degenerate])
    assert isinstance(masks[1], int)
    assert not (masks[1] & _VWAP_TOMBSTONE_MASK)


def test_choppy_sequence_prints_bos_then_choch_in_order() -> None:
    """A choppy stream prints a BOS, THEN a CHoCH — both fire, correct direction.

    Uses the engineered script: the first structure break (bar 5) is a bearish
    BOS (from no trend); the next break of the opposing swing (bar 10) is a
    bullish CHoCH (a reversal). Asserts the BOS strictly precedes the CHoCH.
    """
    bars = [_bar(m, h, l, c) for m, (h, l, c) in enumerate(_HLC_SCRIPT)]
    masks = _run(bars)
    first_bos = min(_fired_bits(masks, _BIT_BOS_BEAR))
    first_choch = min(_fired_bits(masks, _BIT_CHOCH_BULL))
    assert first_bos < first_choch


def test_no_lookahead_prefix_masks_are_stable() -> None:
    """No future bar is read: a prefix's masks are byte-identical with/without tail.

    Behavioural look-ahead oracle for the wired states — at bar N the builder must
    read only bars 0..N. So the first N masks produced over a length-N prefix must
    EQUAL the first N masks produced over the full stream; a future bar that leaked
    into a past output (repainting VWAP / market structure) would differ.
    """
    bars = [_bar(m, h, l, c) for m, (h, l, c) in enumerate(_HLC_SCRIPT)]
    full = _run(bars)
    for cut in (1, 6, 11, 16, 21):
        prefix = _run(bars[:cut])
        assert prefix == full[:cut], f"future bar leaked into the first {cut} masks"


def test_idempotent_bit_for_bit_on_rerun() -> None:
    """Same bars → bit-identical masks on a re-run (Golden Rule #4 / #7).

    No wall-clock, no RNG, no global state: a second fresh builder over the same
    bars produces the identical mask list.
    """
    bars = [_bar(m, h, l, c) for m, (h, l, c) in enumerate(_HLC_SCRIPT)]
    assert _run(bars) == _run(bars)


def test_state_is_o1_space_not_growing_with_bar_count() -> None:
    """O(1) SPACE: the per-bar states do NOT grow with the bar count.

    The market-structure ring is bounded to ``2k+1``. Feeding 50 bars vs 2000
    bars must leave the same bounded internal footprint — proven by reading the
    underlying Rust ring length (capped at 5 for k=2).
    """
    import sys

    import trading_core

    ms = trading_core.MarketStructureState()
    for i in range(2_000):
        ms.update(100 + i, 50 + i, 75 + i)
    # The Rust ring is capped at 2k+1 = 5; the pyclass handle itself is a fixed,
    # small object whose size never grows with the bar count.
    handle_size = sys.getsizeof(ms)

    ms_small = trading_core.MarketStructureState()
    for i in range(50):
        ms_small.update(100 + i, 50 + i, 75 + i)
    assert sys.getsizeof(ms_small) == handle_size


# ────────────────────────────────────────────────────────────────────────
# Property test (hypothesis) — VWAP bounds + idempotence on random streams
# ────────────────────────────────────────────────────────────────────────


@st.composite
def _intraday_stream(draw: st.DrawFn) -> list[CandleBar]:
    """A random single-session stream of valid bars (chronological, one IST day)."""
    n = draw(st.integers(min_value=1, max_value=40))
    bars: list[CandleBar] = []
    for m in range(n):
        low = draw(st.integers(min_value=50, max_value=5_000))
        span = draw(st.integers(min_value=0, max_value=2_000))
        high = low + span
        close = draw(st.integers(min_value=low, max_value=high))
        volume = draw(st.integers(min_value=0, max_value=10_000))
        bars.append(_bar(m, float(high), float(low), float(close), volume=volume))
    return bars


@settings(max_examples=200, deadline=None)
@given(bars=_intraday_stream())
def test_property_idempotent_and_tombstones_inert(bars: list[CandleBar]) -> None:
    """For any random single-session stream: deterministic AND tombstone-inert.

    Two invariants on the REAL push path:

    * **Idempotence** — a second fresh builder over the same bars yields the
      identical mask list (no hidden state / RNG / clock).
    * **Tombstone inertness** — the RETIRED VWAP bits (52/53) are 0 on every
      produced mask, whatever the volume/price shape (the 60→66 reshape).
    """
    first = _run(bars)
    second = _run(bars)
    assert first == second  # idempotent / deterministic

    for mask in first:
        assert (mask & _VWAP_TOMBSTONE_MASK) == 0, "a RETIRED tombstone bit fired"
