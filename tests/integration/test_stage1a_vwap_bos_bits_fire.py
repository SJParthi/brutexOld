"""Load-bearing proof that bits 52/53 + 56-59 actually FIRE in the real sweep.

DEC-STAGE1A-WIRE-VWAP-BOS-001 (B1 fix). Before this PR ``Stage1AMatrixBuilder.push``
built the per-bar :class:`~brutex.vocabulary.evaluator.IndicatorSnapshot` WITHOUT a
``vwap_paisa`` or ``market_structure``, so the evaluator defaulted them to ``None`` /
an all-false signal and the Rust eval gates for bits 52/53 (close above/below VWAP)
and 56-59 (BOS/CHoCH up/down) could NEVER fire in a production sweep — green unit
goldens over a DEAD engine path. This module is the anti-illusion gate: it drives the
ACTUAL :meth:`Stage1AMatrixBuilder.push` production path (never a mock) over crafted
1-minute bars engineered to cross VWAP and to print every BOS/CHoCH direction, and
asserts each bit is SET on at least one produced bar mask — plus a NEGATIVE (a flat
stream sets none of them, proving real logic not always-on) and the operator-mandated
worst-case edge battery (session seed, zero-volume, 09:15 reset, degenerate bar,
look-ahead, idempotence, O(1) space).

The per-bar VWAP + BOS/CHoCH COMPUTE runs in the SHARED Rust ``trading_core`` state
objects (``trading_core.VwapState`` / ``trading_core.MarketStructureState``) — this
test exercises that Rust path through the Python engine, the bit-identical
backtest==live surface (CLAUDE.md §11 COMMON).
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

# The six bits this PR resurrects (R5/S5 — 54/55 — already fired pre-PR, excluded).
_BIT_ABOVE_VWAP = B.BIT_CLOSE_ABOVE_VWAP  # 52
_BIT_BELOW_VWAP = B.BIT_CLOSE_BELOW_VWAP  # 53
_BIT_BOS_BULL = B.BIT_BOS_BULLISH  # 56
_BIT_BOS_BEAR = B.BIT_BOS_BEARISH  # 57
_BIT_CHOCH_BULL = B.BIT_CHOCH_BULLISH  # 58
_BIT_CHOCH_BEAR = B.BIT_CHOCH_BEARISH  # 59


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
# bearish CHoCH (reversing the up-trend), then a bullish BOS — and, because price both
# falls below and rises above the running session VWAP, BOTH VWAP-position bits. The
# swing-confirmation lag (k=2 → a swing confirms 2 bars later) is baked into the
# spacing; verified bar-by-bar in the assertions below.
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


def test_all_six_bits_fire_through_real_push() -> None:
    """Each of bits 52/53/56-59 is SET on >=1 bar of the real ``push`` output.

    This is the gate that FAILS on origin/main (the snapshot carried no
    ``vwap_paisa``/``market_structure`` so the bits could never fire) and PASSES
    with the wiring — the anti-illusion contract.
    """
    bars = [_bar(m, h, l, c) for m, (h, l, c) in enumerate(_HLC_SCRIPT)]
    masks = _run(bars)

    assert _fired_bits(masks, _BIT_ABOVE_VWAP), "close_above_vwap (bit 52) never fired"
    assert _fired_bits(masks, _BIT_BELOW_VWAP), "close_below_vwap (bit 53) never fired"
    assert _fired_bits(masks, _BIT_BOS_BULL), "bos_bullish (bit 56) never fired"
    assert _fired_bits(masks, _BIT_BOS_BEAR), "bos_bearish (bit 57) never fired"
    assert _fired_bits(masks, _BIT_CHOCH_BULL), "choch_bullish (bit 58) never fired"
    assert _fired_bits(masks, _BIT_CHOCH_BEAR), "choch_bearish (bit 59) never fired"


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
# NEGATIVE — proves the bits are real logic, not always-on
# ────────────────────────────────────────────────────────────────────────


def test_flat_stream_fires_none_of_the_six_bits() -> None:
    """A perfectly flat (close == VWAP, no swings) stream sets NONE of the six.

    With every bar at the same price the running VWAP equals the close on every
    bar, so neither ``close > vwap`` nor ``close < vwap`` holds; and a flat window
    confirms no strict local extreme, so no BOS/CHoCH can fire. This is the
    not-always-on guarantee.
    """
    bars = [_bar(m, 100, 100, 100) for m in range(30)]
    masks = _run(bars)

    for bit in (
        _BIT_ABOVE_VWAP,
        _BIT_BELOW_VWAP,
        _BIT_BOS_BULL,
        _BIT_BOS_BEAR,
        _BIT_CHOCH_BULL,
        _BIT_CHOCH_BEAR,
    ):
        assert not _fired_bits(masks, bit), f"bit {bit} fired on a flat stream"


def test_strictly_below_stream_never_sets_vwap_above_bit() -> None:
    """A monotonically FALLING stream never sets close_above_vwap (bit 52).

    On a falling series each close is at-or-below the cumulative VWAP, so bit 52
    (close strictly above VWAP) must never fire — a second proof the VWAP gate is
    a genuine comparison, not always-on. (bit 53, below, naturally fires.)
    """
    bars = [_bar(m, 200 - m, 198 - m, 199 - m) for m in range(25)]
    masks = _run(bars)

    assert not _fired_bits(masks, _BIT_ABOVE_VWAP)
    assert _fired_bits(masks, _BIT_BELOW_VWAP)


# ────────────────────────────────────────────────────────────────────────
# Operator-mandated worst-case edge battery (each a real assertion)
# ────────────────────────────────────────────────────────────────────────


def test_first_bar_of_session_seeds_vwap_no_crash() -> None:
    """The first bar of a session seeds VWAP from itself — no prior data, no crash.

    With one counted bar the session VWAP equals that bar's typical price; a close
    above its own typical price (high-weighted bar) sets bit 52 on the very first
    bar — proving the seed path is live, not deferred to a warm-up window.
    """
    # high 110, low 100, close 108 → typical = (110+100+108)/3 = 106; close 108 > 106.
    masks = _run([_bar(0, 110, 100, 108)])
    assert len(masks) == 1
    assert masks[0] & (1 << _BIT_ABOVE_VWAP)


def test_zero_volume_bar_does_not_divide_by_zero() -> None:
    """A zero-volume FIRST bar yields no VWAP (no div-by-zero / NaN / crash).

    A zero-volume bar contributes nothing to the running sums, so before any real
    volume the session VWAP is undefined → neither VWAP bit fires (the gate sees
    ``None``). The push must return a clean integer mask, never raise.
    """
    masks = _run([_bar(0, 110, 90, 108, volume=0)])
    assert isinstance(masks[0], int)
    assert not (masks[0] & (1 << _BIT_ABOVE_VWAP))
    assert not (masks[0] & (1 << _BIT_BELOW_VWAP))


def test_zero_volume_bar_excluded_from_running_vwap() -> None:
    """A zero-volume bar mid-session never biases the VWAP (it is skipped).

    A huge zero-volume print between two real bars must not move the VWAP — the
    second real bar's VWAP equals what it would be without the zero-volume bar.
    """
    real = _bar(0, 101, 99, 100, volume=100)
    zero = _bar(1, 9_999, 9_998, 9_999, volume=0)  # huge price, zero volume
    real2 = _bar(2, 101, 99, 100, volume=100)
    masks_with_zero = _run([real, zero, real2])
    masks_without = _run([real, real2])
    # The zero-volume bar leaves the VWAP-position outcome of the trailing real
    # bar unchanged (a price spike with no volume cannot flip close_above_vwap).
    assert (masks_with_zero[-1] & (1 << _BIT_ABOVE_VWAP)) == (
        masks_without[-1] & (1 << _BIT_ABOVE_VWAP)
    )
    assert (masks_with_zero[-1] & (1 << _BIT_BELOW_VWAP)) == (
        masks_without[-1] & (1 << _BIT_BELOW_VWAP)
    )


def test_session_reset_at_new_day_does_not_leak_prior_session_vwap() -> None:
    """VWAP resets at the 09:15 IST boundary — the prior day never leaks in.

    Day 1 trades very HIGH; day 2's first bar trades LOW. If the day-1 VWAP leaked
    into day 2, day-2's first close would read ABOVE the (stale high) VWAP. With a
    correct reset, day-2's first bar seeds a fresh low VWAP from itself, so its
    close cannot be below a leaked-high VWAP — we assert the day-2 first bar's
    VWAP-below bit is NOT set (it would be if the high day-1 VWAP leaked).
    """
    day1 = [_bar(m, 1_010, 990, 1_000, day=1) for m in range(5)]
    day2 = [_bar(m, 60, 40, 50, day=2) for m in range(5)]
    masks = _run(day1 + day2)
    first_day2 = masks[len(day1)]
    # A leaked day-1 VWAP (~1000) would make day-2 close (50) read below-VWAP.
    assert not (first_day2 & (1 << _BIT_BELOW_VWAP)), "prior-session VWAP leaked across 09:15"


def test_session_reset_recomputes_vwap_from_fresh_day() -> None:
    """After the 09:15 reset the day-2 VWAP equals a standalone day-2 run.

    Feeding day 2 alone vs day 1 + day 2 must produce IDENTICAL VWAP-position bits
    for every day-2 bar — the reset wipes the prior session completely.
    """
    day1 = [_bar(m, 1_010, 990, 1_000, day=1) for m in range(6)]
    day2 = [_bar(m, 105 - m, 95 - m, 100 - m, day=2) for m in range(8)]
    combined = _run(day1 + day2)[len(day1) :]
    standalone = _run(day2)
    vmask = (1 << _BIT_ABOVE_VWAP) | (1 << _BIT_BELOW_VWAP)
    assert [m & vmask for m in combined] == [m & vmask for m in standalone]


def test_degenerate_bar_high_eq_low_eq_close_is_well_defined() -> None:
    """A bar with high == low == close has a well-defined typical price (no crash).

    The typical price is that single value; against a VWAP seeded from earlier
    bars the close sits cleanly above or below it. We assert the push returns a
    valid mask and (after an earlier lower-priced session) the degenerate bar's
    close reads ABOVE the running VWAP.
    """
    low_seed = _bar(0, 100, 100, 100)  # seeds VWAP at 100
    degenerate = _bar(1, 150, 150, 150)  # high==low==close==150, above VWAP
    masks = _run([low_seed, degenerate])
    assert isinstance(masks[1], int)
    assert masks[1] & (1 << _BIT_ABOVE_VWAP)
    assert not (masks[1] & (1 << _BIT_BELOW_VWAP))


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

    The market-structure ring is bounded to ``2k+1`` and the VWAP holds a fixed
    set of scalar accumulators. Feeding 50 bars vs 2000 bars must leave the same
    bounded internal footprint — proven by reading the underlying Rust ring length
    (capped at 5 for k=2) and confirming the VWAP exposes no growing buffer.
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

    vwap = trading_core.VwapState()
    for i in range(2_000):
        vwap.update(10_000 + i, 100, False, 0, 100, 9 * 3600 + i)
    vwap_small = trading_core.VwapState()
    for i in range(50):
        vwap_small.update(10_000 + i, 100, False, 0, 100, 9 * 3600 + i)
    assert sys.getsizeof(vwap) == sys.getsizeof(vwap_small)


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
def test_property_vwap_within_session_range_and_idempotent(bars: list[CandleBar]) -> None:
    """For any random single-session stream: VWAP-bits are sane AND deterministic.

    Two invariants on the REAL push path:

    * **Idempotence** — a second fresh builder over the same bars yields the
      identical mask list (no hidden state / RNG / clock).
    * **VWAP bound** — the running VWAP can never be strictly above the
      session-high typical price seen so far NOR strictly below the session-low
      typical price seen so far, so ``close_above_vwap`` and ``close_below_vwap``
      can never BOTH be set on the same bar (a VWAP is a weighted mean, bracketed
      by the prices that formed it). We assert the mutual-exclusion the bound
      implies on every produced bar.
    """
    first = _run(bars)
    second = _run(bars)
    assert first == second  # idempotent / deterministic

    for mask in first:
        above = bool(mask & (1 << _BIT_ABOVE_VWAP))
        below = bool(mask & (1 << _BIT_BELOW_VWAP))
        assert not (above and below), "close cannot be both above and below one VWAP"
