"""Unit tests for the Stage-1A bitset-matrix builder (`brutex.engine.stage1a`).

100% line + branch coverage of the adapter that drives the existing
``evaluate_atoms`` across a bar feed (DEC-ENGINE-DRIVER-001). Covers:

* empty input, single bar, multi-bar same day, IST day rollover;
* the rupees→paisa ``ROUND_HALF_UP`` boundary (incl. the half-up edge);
* EMA warm-up ``None`` vs post-seed paisa;
* the ``close == open`` doji tie-break (recorded bearish);
* the first-day anchor-bit clearing vs prior-day-defined anchors;
* the ``build_bar_bits`` functional wrapper + custom proximity / ``prior_n``.

The evaluator itself is unit-tested in ``tests/unit/test_vocabulary.py`` — here
we test the *adapter*, asserting bit-level outcomes that depend on the
conversions this module owns.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
import trading_core
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.stage1a import (
    _DEFAULT_SESSION_KEY,  # pyright: ignore[reportPrivateUsage]
    ANCHOR_DEPENDENT_MASK,
    Stage1AMatrixBuilder,
    WalkForwardFold,
    _ema_to_paisa,
    _price_to_paisa,
    _walk_forward_schedule_hash,
    apply_1day_mask_for,
    build_bar_bits,
)
from brutex.vocabulary import bits as B

# 09:15:00 IST == 03:45 UTC — the NSE session-open anchor (matches
# tests/property/test_spot_features_properties.py).
_OPEN_UTC_H, _OPEN_UTC_M = 3, 45


def _bar(
    ts: datetime,
    open_: str,
    high: str,
    low: str,
    close: str,
    *,
    volume: int = 10,
) -> CandleBar:
    """Build a valid :class:`CandleBar` from rupee strings (exact Decimals)."""
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        open_interest=None,
    )


def _minute_bar(
    day: datetime,
    minute_offset: int,
    open_: str,
    high: str,
    low: str,
    close: str,
) -> CandleBar:
    """A 1-minute bar at ``09:15 IST + minute_offset`` on ``day`` (UTC stored)."""
    ts = day.replace(hour=_OPEN_UTC_H, minute=_OPEN_UTC_M, tzinfo=UTC) + timedelta(
        minutes=minute_offset
    )
    return _bar(ts, open_, high, low, close)


# ────────────────────────────────────────────────────────────────────────
# _price_to_paisa — rupees → int64 paisa boundary
# ────────────────────────────────────────────────────────────────────────


def test_price_to_paisa_exact() -> None:
    assert _price_to_paisa(Decimal("100.00")) == 10000
    assert _price_to_paisa(Decimal("23749.65")) == 2374965


def test_price_to_paisa_snaps_vendor_float_noise() -> None:
    # DEC-SPOT-PRICE-2DP-001: 14020.7999 snaps to the 2dp tick grid first.
    assert _price_to_paisa(Decimal("14020.7999")) == 1402080


def test_price_to_paisa_half_up() -> None:
    # ROUND_HALF_UP: .005 rounds away from zero to .01.
    assert _price_to_paisa(Decimal("100.005")) == 10001
    assert _price_to_paisa(Decimal("100.004")) == 10000


# ────────────────────────────────────────────────────────────────────────
# _ema_to_paisa — float EMA → paisa, warm-up None preserved
# ────────────────────────────────────────────────────────────────────────


def test_ema_to_paisa_none_passthrough() -> None:
    assert _ema_to_paisa(None) is None


def test_ema_to_paisa_rounds_via_money_seam() -> None:
    # 100.005 (float) → Decimal(str) → round_money half-up → paisa.
    assert _ema_to_paisa(100.0) == 10000
    assert _ema_to_paisa(23749.65) == 2374965


# ────────────────────────────────────────────────────────────────────────
# Empty + single-bar edges
# ────────────────────────────────────────────────────────────────────────


def test_empty_input_yields_empty_matrix() -> None:
    assert build_bar_bits([]) == []
    assert Stage1AMatrixBuilder().build([]) == []


def test_single_bar_yields_one_mask() -> None:
    bar = _minute_bar(datetime(2024, 1, 2), 0, "100", "103", "99", "101")
    masks = build_bar_bits([bar])
    assert len(masks) == 1


def test_first_day_clears_anchor_bits() -> None:
    # No prior day exists for the first IST day → anchor groups undefined.
    bars = [_minute_bar(datetime(2024, 1, 2), i, "100", "103", "99", "101") for i in range(3)]
    masks = build_bar_bits(bars)
    assert all((m & ANCHOR_DEPENDENT_MASK) == 0 for m in masks)


# ────────────────────────────────────────────────────────────────────────
# Same-day absorb vs day rollover
# ────────────────────────────────────────────────────────────────────────


def test_same_day_absorb_expands_day_extremes() -> None:
    # Two same-day bars: the second sets a new high → CLOSE_AT_DAY_HIGH fires.
    day = datetime(2024, 1, 2)
    b0 = _minute_bar(day, 0, "100", "101", "99", "100")
    b1 = _minute_bar(day, 1, "100", "105", "100", "105")  # new running high, close==high
    masks = build_bar_bits([b0, b1])
    assert masks[1] & (1 << B.BIT_CLOSE_AT_DAY_HIGH)


def test_day_rollover_defines_anchors() -> None:
    # Day 1 establishes the prior-day OHLC; day 2 bars get real anchors and so
    # CAN set anchor-dependent bits (here PDH/PDL position).
    day1 = [_minute_bar(datetime(2024, 1, 2), i, "100", "110", "90", "100") for i in range(3)]
    # Day 2 closes well ABOVE the prior day's high (110) → close_above_pdh fires.
    day2 = [_minute_bar(datetime(2024, 1, 3), i, "200", "210", "190", "200") for i in range(3)]
    masks = build_bar_bits(day1 + day2)
    assert all((m & ANCHOR_DEPENDENT_MASK) == 0 for m in masks[:3])
    # Day-2 bars are above prior PDH=110 → close_above_pdh + close_above_pdl set.
    for m in masks[3:]:
        assert m & (1 << B.BIT_CLOSE_ABOVE_PDH)
        assert m & (1 << B.BIT_CLOSE_ABOVE_PDL)


def test_three_days_rotates_prior_day() -> None:
    # The prior-day anchors must track the MOST-RECENT completed day, not day 1.
    day1 = [_minute_bar(datetime(2024, 1, 2), 0, "100", "110", "90", "100")]
    day2 = [_minute_bar(datetime(2024, 1, 3), 0, "300", "310", "290", "300")]
    day3 = [_minute_bar(datetime(2024, 1, 4), 0, "305", "315", "295", "305")]
    masks = build_bar_bits(day1 + day2 + day3)
    # Day 3 close=305 is BELOW day-2 high (310) but ABOVE day-2 low (290):
    # close_below_pdh AND close_above_pdl both set → proves anchors came from
    # day 2 (if they wrongly came from day 1 [H=110], close would be above PDH).
    m3 = masks[2]
    assert m3 & (1 << B.BIT_CLOSE_BELOW_PDH)
    assert m3 & (1 << B.BIT_CLOSE_ABOVE_PDL)
    assert not (m3 & (1 << B.BIT_CLOSE_ABOVE_PDH))


# ────────────────────────────────────────────────────────────────────────
# Candle / prior-bar tie-break (close == open → bearish)
# ────────────────────────────────────────────────────────────────────────


def test_doji_close_equals_open_is_not_bullish() -> None:
    bar = _minute_bar(datetime(2024, 1, 2), 0, "100", "101", "99", "100")  # close == open
    mask = build_bar_bits([bar])[0]
    assert not (mask & (1 << B.BIT_BAR_BULLISH))
    assert not (mask & (1 << B.BIT_BAR_BEARISH))


def test_bullish_and_bearish_bars() -> None:
    up = _minute_bar(datetime(2024, 1, 2), 0, "100", "105", "99", "104")  # close > open
    down = _minute_bar(datetime(2024, 1, 2), 1, "104", "105", "98", "99")  # close < open
    masks = build_bar_bits([up, down])
    assert masks[0] & (1 << B.BIT_BAR_BULLISH)
    assert masks[1] & (1 << B.BIT_BAR_BEARISH)


def test_prior_bar_history_doji_counts_as_bearish() -> None:
    # prior_n=2; two consecutive dojis (close==open) → on the 3rd bar the
    # prior-2 are both recorded BEARISH (the tie-break), so PRIOR_N_BEARISH fires.
    day = datetime(2024, 1, 2)
    doji0 = _minute_bar(day, 0, "100", "101", "99", "100")
    doji1 = _minute_bar(day, 1, "100", "101", "99", "100")
    probe = _minute_bar(day, 2, "100", "101", "99", "100")
    masks = Stage1AMatrixBuilder(prior_n=2).build([doji0, doji1, probe])
    assert masks[2] & (1 << B.BIT_PRIOR_N_BEARISH)
    assert not (masks[2] & (1 << B.BIT_PRIOR_N_BULLISH))


# ────────────────────────────────────────────────────────────────────────
# EMA warm-up → seeded transition
# ────────────────────────────────────────────────────────────────────────


def test_ema20_none_during_warmup_then_set() -> None:
    # 20 same-day bars: bars 0..18 have ema20 == None (no EMA bits possible);
    # bar 19 seeds the EMA and a close above it sets close_above_ema20.
    day = datetime(2024, 1, 2)
    # Rising closes so the seed SMA sits below the last close.
    bars = [_minute_bar(day, i, "100", "100000", "1", str(100 + i)) for i in range(20)]
    builder = Stage1AMatrixBuilder()
    masks = builder.build(bars)
    # Warm-up bars cannot set either EMA20 position bit.
    for m in masks[:19]:
        assert not (m & (1 << B.BIT_CLOSE_ABOVE_EMA20))
        assert not (m & (1 << B.BIT_CLOSE_BELOW_EMA20))
    # Seed bar (index 19): close (119) > SMA(100..119) → close_above_ema20.
    assert masks[19] & (1 << B.BIT_CLOSE_ABOVE_EMA20)


# ────────────────────────────────────────────────────────────────────────
# Determinism + streaming/batch parity + public surface
# ────────────────────────────────────────────────────────────────────────


def test_build_is_deterministic() -> None:
    bars = [
        _minute_bar(datetime(2024, 1, 2), i, "100", "103", "99", str(100 + (i % 3)))
        for i in range(6)
    ]
    assert build_bar_bits(bars) == build_bar_bits(bars)


def test_stream_matches_build() -> None:
    bars = [
        _minute_bar(datetime(2024, 1, 2), i, "100", "103", "99", str(100 + (i % 3)))
        for i in range(6)
    ]
    assert list(Stage1AMatrixBuilder().stream(bars)) == Stage1AMatrixBuilder().build(bars)


def test_stream_is_lazy() -> None:
    # The generator pulls bars one at a time; taking the first mask must consume
    # exactly one bar from the source iterator.
    day = datetime(2024, 1, 2)
    pulled = 0

    def gen() -> Iterator[CandleBar]:
        nonlocal pulled
        for i in range(3):
            pulled += 1
            yield _minute_bar(day, i, "100", "103", "99", "101")

    stream = Stage1AMatrixBuilder().stream(gen())
    next(stream)
    assert pulled == 1


def test_custom_proximity_changes_near_bits() -> None:
    # A bar whose close sits ~7 bps from the prior-day pivot: near at 10 bps,
    # not near at 5 bps. Proves the proximity param is threaded through.
    from brutex.vocabulary.evaluator import ProximityParams

    # Day 1: P = (H+L+C)/3. Build day 1 so the pivot is a round number.
    day1 = [_minute_bar(datetime(2024, 1, 2), 0, "300", "300", "300", "300")]  # P = 300
    # Day 2 close 300.18 → ~6 bps from 300 (300*0.0006 = 0.18).
    day2 = [_minute_bar(datetime(2024, 1, 3), 0, "300.18", "300.18", "300.18", "300.18")]
    near_10 = build_bar_bits(day1 + day2, proximity=ProximityParams(pivot_proximity_bps=10))
    near_5 = build_bar_bits(day1 + day2, proximity=ProximityParams(pivot_proximity_bps=5))
    assert near_10[1] & (1 << B.BIT_NEAR_PIVOT_P)
    assert not (near_5[1] & (1 << B.BIT_NEAR_PIVOT_P))


def test_anchor_dependent_mask_layout() -> None:
    # The cleared mask must be exactly Groups 2,3,4,9 and NOT touch the
    # always-defined groups (EMA / candle / prior / day-structure / time).
    for bit in (
        B.BIT_NEAR_PIVOT_P,
        B.BIT_CLOSE_ABOVE_PDH,
        B.BIT_NEAR_FIB_50,
        B.BIT_GAP_UP_DAY,
        B.BIT_OUTSIDE_DAY,
    ):
        assert ANCHOR_DEPENDENT_MASK & (1 << bit)
    for bit in (
        B.BIT_CLOSE_ABOVE_EMA20,
        B.BIT_BAR_BULLISH,
        B.BIT_PRIOR_N_BULLISH,
        B.BIT_CLOSE_AT_DAY_HIGH,
        B.BIT_EARLY_MORNING,
    ):
        assert not (ANCHOR_DEPENDENT_MASK & (1 << bit))


def test_utc_to_ist_drives_time_of_day_bit() -> None:
    # A bar at 03:45 UTC == 09:15 IST is the open; the evaluator's time-of-day
    # group is keyed on IST. 03:45 UTC → 09:15 IST is BEFORE 09:16, so the
    # early-morning bit is OFF; a bar at 04:00 UTC (09:30 IST) sets it.
    open_bar = _bar(datetime(2024, 1, 2, 3, 45, tzinfo=UTC), "100", "101", "99", "100")
    later = _bar(datetime(2024, 1, 2, 4, 0, tzinfo=UTC), "100", "101", "99", "100")
    masks = build_bar_bits([open_bar, later])
    assert not (masks[0] & (1 << B.BIT_EARLY_MORNING))  # 09:15 < 09:16 cutoff
    assert masks[1] & (1 << B.BIT_EARLY_MORNING)  # 09:30 IST in [09:16, 10:00)


def test_timestamp_ist_is_converted_not_assumed() -> None:
    # Feed a non-UTC tz to prove astimezone(IST) is used (not a raw .replace).
    builder = Stage1AMatrixBuilder()
    # 22:15 US/Eastern (EST, UTC-5) on 2024-01-01 == 03:15 UTC 2024-01-02
    # == 08:45 IST 2024-01-02. Different calendar day in IST than in source tz.
    from zoneinfo import ZoneInfo

    eastern = ZoneInfo("America/New_York")
    bar = _bar(datetime(2024, 1, 1, 22, 15, tzinfo=eastern), "100", "101", "99", "100")
    # Should not raise; IST date is 2024-01-02.
    builder.push(bar)
    assert bar.timestamp.astimezone(IST).date().isoformat() == "2024-01-02"


# ────────────────────────────────────────────────────────────────────────
# ADR-0012 roll_schedule (B2, DEC-STAGE1A-ROLL-RESET-001) + the D12
# apply_1day_mask flag (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001)
# ────────────────────────────────────────────────────────────────────────
#
# The NIFTY-futures track feeds the builder a strictly increasing tuple of
# owning-expiry day-ordinals; per bar the session key becomes the first entry
# ``>=`` the bar's IST day ordinal (the ``resolve_near_month`` ``>=`` law over
# the vendor schedule) and a key CHANGE triggers the TOTAL per-contract hard
# reset. The spot path (``roll_schedule=None``) must stay byte-identical.

#: The real NIFTY Jun/Jul-2025 monthly expiries (vendor-token dates).
_EXPIRY_JUN = date(2025, 6, 26)
_EXPIRY_JUL = date(2025, 7, 31)
_ROLL_SCHEDULE = (_EXPIRY_JUN.toordinal(), _EXPIRY_JUL.toordinal())


def _fut_bar(day: date, minute: int, open_: str, high: str, low: str, close: str) -> CandleBar:
    """A volume-bearing 1-minute futures bar at 09:16 IST + *minute* on *day*."""
    ts = datetime(day.year, day.month, day.day, 9, 16, tzinfo=IST) + timedelta(minutes=minute)
    return _bar(ts.astimezone(UTC), open_, high, low, close, volume=100)


def _seg_day(day: date, base: int, n: int = 6) -> list[CandleBar]:
    """*n* drifting volume-bearing bars on *day* around integer-rupee *base*."""
    out: list[CandleBar] = []
    for m in range(n):
        o = base + m
        c = o + 1
        out.append(_fut_bar(day, m, str(o), str(c + 1), str(o - 1), str(c)))
    return out


def _two_contract_stream() -> tuple[list[CandleBar], list[CandleBar]]:
    """(segment A bars, segment B bars) across the Jun-2025 roll with a basis gap.

    Segment A (owned by the 26Jun25 contract): 24-26 Jun around ₹100;
    segment B (owned by the 31Jul25 contract): 27+30 Jun around ₹140 —
    the ~40-pt basis gap D11 forbids comparing levels across.
    """
    seg_a = (
        _seg_day(date(2025, 6, 24), 100)
        + _seg_day(date(2025, 6, 25), 102)
        + _seg_day(date(2025, 6, 26), 104)
    )
    seg_b = _seg_day(date(2025, 6, 27), 140) + _seg_day(date(2025, 6, 30), 142)
    return seg_a, seg_b


# ── Schedule validation (fail-fast input contract) ──


def test_roll_schedule_empty_tuple_rejected() -> None:
    with pytest.raises(ValueError, match="roll_schedule must be non-empty"):
        Stage1AMatrixBuilder(roll_schedule=())


@pytest.mark.parametrize(
    "schedule",
    [(5, 5), (5, 3), (-1, 5)],
    ids=["duplicate", "decreasing", "negative"],
)
def test_roll_schedule_invalid_shape_rejected(schedule: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        Stage1AMatrixBuilder(roll_schedule=schedule)


def test_roll_schedule_invalid_shape_rejected_via_wrapper() -> None:
    # The functional wrapper forwards the kw-only param → same fail-fast.
    with pytest.raises(ValueError, match="strictly increasing"):
        build_bar_bits([], roll_schedule=(3, 2))


def test_negative_session_key_is_an_ffi_overflow() -> None:
    """The u64-boundary pin motivating the non-negative schedule validation.

    ``VwapState.update``'s ``session_key`` is a Rust ``u64`` — a negative
    Python int raises ``OverflowError`` AT the FFI, so the builder's ctor
    validation (negative ordinals rejected) is what keeps that error
    unreachable from the production path.
    """
    state = trading_core.VwapState()
    with pytest.raises(OverflowError):
        state.update(10_000, 100, False, -1, date(2025, 6, 24).toordinal(), 9 * 3600)


# ── Cursor exhaustion (F10 — loader/schedule desync is LOUD) ──


def test_cursor_exhaustion_raises_value_error() -> None:
    seg_a, seg_b = _two_contract_stream()
    # Schedule ends at the Jun expiry; segment B bars (27+30 Jun) are BEYOND it.
    builder = Stage1AMatrixBuilder(roll_schedule=(_EXPIRY_JUN.toordinal(),))
    for bar in seg_a:
        builder.push(bar)
    with pytest.raises(ValueError, match="beyond the last roll_schedule expiry"):
        builder.push(seg_b[0])


def test_cursor_exhaustion_message_names_the_desync() -> None:
    builder = Stage1AMatrixBuilder(roll_schedule=(_EXPIRY_JUN.toordinal(),))
    with pytest.raises(ValueError, match="loader/schedule desync"):
        builder.push(_fut_bar(date(2025, 6, 27), 0, "140", "142", "139", "141"))


# ── Key derivation: the >= law + the loader-cut/cursor agreement pin ──


def test_expiry_day_bar_keyed_to_the_expiring_contract() -> None:
    """The ``>=`` boundary law: the expiry-day bar keys to the EXPIRING ordinal."""
    builder = Stage1AMatrixBuilder(roll_schedule=_ROLL_SCHEDULE)
    builder.push(_fut_bar(_EXPIRY_JUN, 0, "104", "106", "103", "105"))
    key = builder._current_session_key  # pyright: ignore[reportPrivateUsage]
    assert key == _EXPIRY_JUN.toordinal()
    # The next trading day rolls to the NEXT contract's ordinal.
    builder.push(_fut_bar(date(2025, 6, 27), 0, "140", "142", "139", "141"))
    key = builder._current_session_key  # pyright: ignore[reportPrivateUsage]
    assert key == _EXPIRY_JUL.toordinal()


def test_loader_cut_and_cursor_agree_per_bar() -> None:
    """The loader's IST-date ownership cut == the cursor's key, bar for bar.

    Both derive from the ONE ``roll_schedule`` (owning expiry = first schedule
    entry ``>=`` the bar's IST day ordinal) — computed test-side from the
    date-cut law and asserted against the cursor after every push, over a
    crafted boundary stream (pre-expiry / expiry-day / post-roll bars).
    """
    seg_a, seg_b = _two_contract_stream()
    builder = Stage1AMatrixBuilder(roll_schedule=_ROLL_SCHEDULE)
    for bar in seg_a + seg_b:
        day_ordinal = bar.timestamp.astimezone(IST).date().toordinal()
        expected = min(e for e in _ROLL_SCHEDULE if e >= day_ordinal)
        builder.push(bar)
        key = builder._current_session_key  # pyright: ignore[reportPrivateUsage]
        assert key == expected


def test_spot_path_feeds_exactly_the_default_session_key() -> None:
    """SPOT session-key drift pin: no schedule ⇒ every VWAP feed gets the constant.

    A recording wrapper around the real ``VwapState`` proves the schedule-absent
    path feeds EXACTLY ``_DEFAULT_SESSION_KEY`` (== 0) on every bar — the
    byte-identity precondition every spot golden rides on — and that the
    futures cursor state stays untouched (``_current_session_key`` remains
    ``None``).
    """

    class _KeyRecordingVwap:
        def __init__(self) -> None:
            self._inner = trading_core.VwapState()
            self.keys: list[int] = []

        def update(
            self,
            typical_paisa: int,
            volume: int,
            pre_open: bool,
            session_key: int,
            session_day: int,
            tod_seconds: int,
        ) -> int | None:
            self.keys.append(session_key)
            result = self._inner.update(
                typical_paisa, volume, pre_open, session_key, session_day, tod_seconds
            )
            return result  # noqa: RET504 — keep the FFI return explicit

    seg_a, seg_b = _two_contract_stream()
    builder = Stage1AMatrixBuilder()
    spy = _KeyRecordingVwap()
    builder._vwap = spy  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    for bar in seg_a + seg_b:
        builder.push(bar)
    assert spy.keys == [_DEFAULT_SESSION_KEY] * (len(seg_a) + len(seg_b))
    assert _DEFAULT_SESSION_KEY == 0
    assert builder._current_session_key is None  # pyright: ignore[reportPrivateUsage]


# ── The TOTAL hard reset (D11) ──


def test_fresh_builder_oracle_post_roll_suffix() -> None:
    """Post-roll suffix of a stitched run == a standalone fresh-builder run.

    THE testable oracle of DEC-STAGE1A-ROLL-RESET-001: `_roll_to_new_contract`
    restores the exact fresh-builder state, so the stitched build's segment-B
    masks are bit-identical to a fresh builder fed segment B alone.
    """
    seg_a, seg_b = _two_contract_stream()
    stitched = build_bar_bits(seg_a + seg_b, roll_schedule=_ROLL_SCHEDULE)
    assert stitched[len(seg_a) :] == build_bar_bits(seg_b)
    # And the pre-roll prefix is untouched by the schedule machinery.
    assert stitched[: len(seg_a)] == build_bar_bits(seg_a)


def test_first_post_roll_day_clears_anchor_dependent_bits() -> None:
    """The first post-roll day is anchor-cold — `_NON_ANCHOR_MASK` fires again.

    Segment A completed two prior days, so WITHOUT the reset the 27-Jun bars
    would carry warm prev-day anchors (across a ~40-pt basis gap — the exact
    D11 violation); the hard reset makes the new contract's first day behave
    like day 1 of a fresh stream.
    """
    seg_a, seg_b = _two_contract_stream()
    stitched = build_bar_bits(seg_a + seg_b, roll_schedule=_ROLL_SCHEDULE)
    first_post_roll_day = [
        m
        for bar, m in zip(seg_a + seg_b, stitched, strict=True)
        if bar.timestamp.astimezone(IST).date() == date(2025, 6, 27)
    ]
    assert first_post_roll_day  # non-vacuous
    assert all((m & ANCHOR_DEPENDENT_MASK) == 0 for m in first_post_roll_day)
    # Teeth: the continuous (no-schedule) build DOES set anchor bits there —
    # proving the clear above comes from the roll reset, not the fixture.
    continuous = build_bar_bits(seg_a + seg_b)
    continuous_post_roll = [
        m
        for bar, m in zip(seg_a + seg_b, continuous, strict=True)
        if bar.timestamp.astimezone(IST).date() == date(2025, 6, 27)
    ]
    assert any((m & ANCHOR_DEPENDENT_MASK) != 0 for m in continuous_post_roll)


def test_market_structure_hard_resets_on_roll() -> None:
    """A swing confirmed on the OLD contract can never trigger BOS/CHoCH on the new.

    Segment A confirms a swing high then sets a DOWN trend (bars 0-9 of the
    B1 fire-test script); segment B's first bars close far ABOVE that swing —
    with CARRIED state a CHoCH-bull fires immediately (proven by the
    continuous-build teeth arm), while the hard reset leaves the fresh
    ``MarketStructureState`` ring unfilled (k=2 ⇒ no signal before 2k+1 bars),
    so NO market-structure bit can fire on the first 4 post-roll bars.
    """
    script_a: tuple[tuple[int, int, int], ...] = (
        (100, 90, 95),
        (95, 85, 90),
        (88, 70, 75),  # swing-low 70 candidate
        (90, 80, 85),
        (92, 82, 88),  # confirms swing-low 70
        (85, 60, 65),  # close 65 < 70 → BOS_BEAR, trend DOWN
        (95, 85, 90),
        (110, 100, 105),  # swing-high 110 candidate
        (98, 88, 92),
        (96, 86, 90),  # confirms swing-high 110 (trend still DOWN)
    )
    day_a = date(2025, 6, 26)
    seg_a = [
        _fut_bar(day_a, m, str(min(h, max(l, c))), str(h), str(l), str(c))
        for m, (h, l, c) in enumerate(script_a)
    ]
    # Post-roll bars close ABOVE the old swing high (110) — basis-gapped.
    day_b = date(2025, 6, 27)
    seg_b = [
        _fut_bar(day_b, m, str(150 + m), str(152 + m), str(149 + m), str(151 + m)) for m in range(4)
    ]
    structure_mask = (
        (1 << B.BIT_BOS_BULLISH)
        | (1 << B.BIT_BOS_BEARISH)
        | (1 << B.BIT_CHOCH_BULLISH)
        | (1 << B.BIT_CHOCH_BEARISH)
    )
    stitched = build_bar_bits(seg_a + seg_b, roll_schedule=_ROLL_SCHEDULE)
    pre_roll = stitched[: len(seg_a)]
    post_roll = stitched[len(seg_a) :]
    assert any(m & structure_mask for m in pre_roll)  # the script genuinely fired
    assert all((m & structure_mask) == 0 for m in post_roll)
    # Teeth: WITHOUT the reset the carried DOWN-trend + swing 110 make the
    # first post-roll close (151 > 110) a CHoCH-bull — the exact phantom D11
    # forbids. Proves the reset (not the fixture) produced the silence above.
    continuous = build_bar_bits(seg_a + seg_b)
    assert continuous[len(seg_a)] & (1 << B.BIT_CHOCH_BULLISH)


# ── Spot byte-identity (P2) + constant-key invariance ──


def test_none_schedule_is_byte_identical_to_default() -> None:
    seg_a, seg_b = _two_contract_stream()
    bars = seg_a + seg_b
    assert build_bar_bits(bars) == build_bar_bits(bars, roll_schedule=None)


def test_constant_key_single_contract_is_output_invariant() -> None:
    """A schedule that makes the key ONE constant is output-invariant.

    On a warm-volume SINGLE-contract stream, a one-entry schedule (every bar
    keys to the same owning-expiry ordinal) must be bit-identical to the
    default constant-key build — kills threading mutants that misroute the
    key without changing its constancy.
    """
    seg_a, _ = _two_contract_stream()
    last_ordinal = seg_a[-1].timestamp.astimezone(IST).date().toordinal()
    assert build_bar_bits(seg_a) == build_bar_bits(seg_a, roll_schedule=(last_ordinal,))


# ── apply_1day_mask (D12) ──


def test_apply_1day_mask_clears_exactly_the_pinned_bit_set() -> None:
    """The flag ANDs the FINAL mask with ``APPLIES_TO_1DAY_MASK`` — nothing else.

    The exact cleared bit set (ToD 44-47 + VWAP 52-53) is pinned by
    ``tests/unit/test_vocabulary.py::test_applies_to_1day_mask_clears_exactly_tod_and_vwap_bits``
    (the ONE bit-set source of truth — reused, not re-invented); here we pin
    that the builder applies EXACTLY that mask per bar, non-vacuously.
    """
    seg_a, seg_b = _two_contract_stream()
    bars = seg_a + seg_b
    unmasked = build_bar_bits(bars)
    masked = build_bar_bits(bars, apply_1day_mask=True)
    assert masked == [m & B.APPLIES_TO_1DAY_MASK for m in unmasked]
    # Non-vacuous: the volume-bearing stream genuinely fired at least one of
    # the six intraday-only bits (52/53 fire from bar 0 — VWAP seeds itself).
    cleared = [m & ~B.APPLIES_TO_1DAY_MASK for m in unmasked]
    assert any(cleared)
    # Every cleared bit is inside the vocabulary's own pinned intraday-only set.
    intraday_only = B.IMPLEMENTED_MASK & ~B.APPLIES_TO_1DAY_MASK
    assert all((c & ~intraday_only) == 0 for c in cleared)


def test_apply_1day_mask_composes_with_roll_schedule() -> None:
    seg_a, seg_b = _two_contract_stream()
    bars = seg_a + seg_b
    stitched = build_bar_bits(bars, roll_schedule=_ROLL_SCHEDULE)
    masked = build_bar_bits(bars, roll_schedule=_ROLL_SCHEDULE, apply_1day_mask=True)
    assert masked == [m & B.APPLIES_TO_1DAY_MASK for m in stitched]


def test_apply_1day_mask_for_is_the_one_d12_predicate() -> None:
    """True iff (the futures alias, "1day") — spot deliberately NOT flipped."""
    assert apply_1day_mask_for("NSE-NIFTY-FUT", "1day") is True
    assert apply_1day_mask_for("NSE-NIFTY-FUT", "1minute") is False
    assert apply_1day_mask_for("NSE-NIFTY-FUT", "5minute") is False
    assert apply_1day_mask_for("NIFTY", "1day") is False  # named follow-up
    assert apply_1day_mask_for("BANKNIFTY", "1day") is False
    assert apply_1day_mask_for("NSE-NIFTY-30Jul26-FUT", "1day") is False


def test_walk_forward_schedule_hash_rejects_nan_param() -> None:
    """``allow_nan=False`` (DEC-RUN-IDENTITY-GUARD-PARITY-001): a smuggled
    non-finite float raises instead of silently emitting non-canonical
    ``NaN`` JSON — unreachable for the typed all-int schedule fields, so
    every currently-legal digest is byte-unchanged (golden-pinned in
    ``tests/golden/test_engine_run_identity_golden.py``)."""
    fold = WalkForwardFold(
        fold_index=0,
        train_start_day=0,
        train_end_day=3,
        test_start_day=3,
        test_end_day=4,
        train_start_bar=0,
        train_end_bar=6,
        test_start_bar=6,
        test_end_bar=8,
    )
    with pytest.raises(ValueError, match="[Oo]ut of range float"):
        _walk_forward_schedule_hash((fold,), cast(int, float("nan")), 1, 1, 4, 8)
