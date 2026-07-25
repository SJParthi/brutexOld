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


def test_cold_anchor_day_clears_every_cpr_bit() -> None:
    """G-CPRCOR-01: the CPR bits 60-63 ride the anchor-dependent clear.

    On a day-1 / placeholder-anchor stream the evaluator sees the ZERO
    placeholder pivots — ``close > TC == 0`` would fire ``above_cpr_tc``
    trivially — so ``_pivot_dependent_bits()`` MUST include bits 60-63 and
    the builder MUST clear them on every cold-anchor bar. Pinned per bar
    (not just via ``ANCHOR_DEPENDENT_MASK`` membership) so a masking
    regression can never hide behind the membership pin.
    """
    cpr_mask = 0b1111 << 60
    assert ANCHOR_DEPENDENT_MASK & cpr_mask == cpr_mask  # membership
    bars = [_minute_bar(datetime(2024, 1, 2), i, "100", "103", "99", "101") for i in range(5)]
    masks = build_bar_bits(bars)
    assert masks, "non-vacuous: the cold-anchor stream produced masks"
    for i, m in enumerate(masks):
        assert m & cpr_mask == 0, f"bar {i}: a CPR bit fired against placeholder anchors"
    # The warm counterfactual: a second day (prior day completed) DOES fire a
    # CPR position bit (the 60/61/62 partition covers every warm bar), so the
    # cold-day clear above is proven non-vacuous.
    day2 = [_minute_bar(datetime(2024, 1, 3), i, "100", "103", "99", "101") for i in range(3)]
    warm = build_bar_bits(bars + day2)[len(bars) :]
    assert all(m & cpr_mask for m in warm)


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


def test_spot_path_never_derives_a_roll_key() -> None:
    """SPOT drift pin: no schedule ⇒ NO per-bar roll key, NO roll boundary.

    With the retired VWAP feed gone the spot arm computes no session key at
    all — the constant :data:`_DEFAULT_SESSION_KEY` (== 0) survives only as
    the documented former-feed record. This pins that a schedule-less build
    never touches the futures cursor state (``_current_session_key`` stays
    ``None`` and the roll cursor never advances), so no D11 hard reset can
    ever fire on the continuous spot-index series — the byte-identity
    precondition every spot golden rides on.
    """
    seg_a, seg_b = _two_contract_stream()
    builder = Stage1AMatrixBuilder()
    for bar in seg_a + seg_b:
        builder.push(bar)
    assert _DEFAULT_SESSION_KEY == 0
    assert builder._current_session_key is None  # pyright: ignore[reportPrivateUsage]
    assert builder._roll_cursor == 0  # pyright: ignore[reportPrivateUsage]


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

    The exact cleared bit set (ToD 44-47; the RETIRED VWAP tombstones 52-53
    sit outside the mask too but never fire) is pinned by
    ``tests/unit/test_vocabulary.py::test_applies_to_1day_mask_clears_exactly_tod_and_vwap_bits``
    (the ONE bit-set source of truth — reused, not re-invented); here we pin
    that the builder applies EXACTLY that mask per bar, non-vacuously.
    """
    seg_a, seg_b = _two_contract_stream()
    bars = seg_a + seg_b
    unmasked = build_bar_bits(bars)
    masked = build_bar_bits(bars, apply_1day_mask=True)
    assert masked == [m & B.APPLIES_TO_1DAY_MASK for m in unmasked]
    # Non-vacuous: the intraday stream genuinely fired at least one of the
    # intraday-only ToD bits (bit 44 fires from the 09:15 open).
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


# ────────────────────────────────────────────────────────────────────────
# Muhurat-safe prev-day anchor (DEC-SWEEP-DATA-EDGE-GUARDS-001, D-MAJ-1)
# ────────────────────────────────────────────────────────────────────────
#
# A Diwali *Muhurat* session is a ~1-hour special session, NOT a regular
# trading day. Its distorted 1-hour OHLC must never become the NEXT regular
# day's pivot / PDH-PDL / Fib / day-type anchor. These tests pin that the
# stage1a roll seam carries the prior REGULAR session forward across a Muhurat
# boundary, byte-identical to ``lookup_prev_day_ohlc``.

from datetime import date as _date  # noqa: E402

from brutex.calendars.nse import MUHURAT_SESSIONS  # noqa: E402
from brutex.engine.stage1a import _MUHURAT_ANCHOR_SKIP_EVENT  # noqa: E402
from brutex.indicators.pivots import compute_pivot_levels  # noqa: E402
from brutex.indicators.prev_day_ohlc import DailyCandle, lookup_prev_day_ohlc  # noqa: E402

# (prev regular session, muhurat session, next regular session) — the six real
# NSE Diwali Muhurat boundaries 2020-2025, verified against the calendar.
_MUHURAT_BOUNDARIES: tuple[tuple[_date, _date, _date], ...] = (
    (_date(2020, 11, 13), _date(2020, 11, 14), _date(2020, 11, 17)),
    (_date(2021, 11, 3), _date(2021, 11, 4), _date(2021, 11, 8)),
    (_date(2022, 10, 21), _date(2022, 10, 24), _date(2022, 10, 25)),
    (_date(2023, 11, 10), _date(2023, 11, 12), _date(2023, 11, 13)),
    (_date(2024, 10, 31), _date(2024, 11, 1), _date(2024, 11, 4)),
    (_date(2025, 10, 20), _date(2025, 10, 21), _date(2025, 10, 23)),
)


def _day_bar(d: _date, o: str, h: str, low: str, c: str) -> CandleBar:
    """One bar at 09:15 IST on IST date *d* (the guard keys on the date only)."""
    ts = datetime(d.year, d.month, d.day, _OPEN_UTC_H, _OPEN_UTC_M, tzinfo=UTC)
    return _bar(ts, o, h, low, c)


def test_muhurat_boundaries_cover_all_six_calendar_sessions() -> None:
    # Guard against calendar drift: the fixture's middle dates ARE exactly the
    # six MUHURAT_SESSIONS keys.
    assert {m for _, m, _ in _MUHURAT_BOUNDARIES} == set(MUHURAT_SESSIONS)


@pytest.mark.parametrize(("prev_reg", "muhurat", "next_reg"), _MUHURAT_BOUNDARIES)
def test_muhurat_prev_day_anchor_carries_prior_regular_session(
    prev_reg: _date, muhurat: _date, next_reg: _date
) -> None:
    # Regular session with a WIDE range (PDH 110 / PDL 90 / PDC 105); the
    # Muhurat 1-hour session with a NARROW range near its close (104.x). If the
    # roll poisoned the anchor, the next regular day's prev-day OHLC would be
    # the narrow Muhurat bar, not the wide regular one.
    builder = Stage1AMatrixBuilder()
    builder.build(
        [
            _day_bar(prev_reg, "100.00", "110.00", "90.00", "105.00"),
            _day_bar(muhurat, "104.00", "104.50", "103.50", "104.20"),
            _day_bar(next_reg, "104.00", "104.00", "104.00", "104.00"),
        ]
    )

    prev = builder._prev_day_ohlc
    assert prev is not None
    # The prior-REGULAR session (PDH 11000 / PDL 9000 / PDC 10500 in paisa)
    # carried through — NOT the Muhurat bar (10450 / 10350 / 10420).
    assert (prev.high_paisa, prev.low_paisa, prev.close_paisa) == (11000, 9000, 10500)

    # Parity pin: stage1a's anchor equals lookup_prev_day_ohlc's answer across
    # the same Muhurat boundary (both skip the Muhurat session).
    candles = {
        prev_reg: DailyCandle(prev_reg, 10000, 11000, 9000, 10500),
        muhurat: DailyCandle(muhurat, 10400, 10450, 10350, 10420),
    }
    looked = lookup_prev_day_ohlc(next_reg, candles)
    assert looked is not None
    assert (looked.pdh_paisa, looked.pdl_paisa, looked.pdc_paisa) == (11000, 9000, 10500)

    # The next regular day's pivots are the ones computed FROM the regular
    # session (fixed), never from the poisoning Muhurat bar (buggy).
    fixed = compute_pivot_levels(next_reg, pdh_paisa=11000, pdl_paisa=9000, pdc_paisa=10500)
    buggy = compute_pivot_levels(next_reg, pdh_paisa=10450, pdl_paisa=10350, pdc_paisa=10420)
    assert builder._anchors is not None
    assert builder._anchors.pivots.p_paisa == fixed.p_paisa
    assert fixed.p_paisa != buggy.p_paisa  # the poison would have moved P


def test_muhurat_roll_emits_one_warn_naming_the_skipped_date() -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        Stage1AMatrixBuilder().build(
            [
                _day_bar(_date(2020, 11, 13), "100.00", "110.00", "90.00", "105.00"),
                _day_bar(_date(2020, 11, 14), "104.00", "104.50", "103.50", "104.20"),
                _day_bar(_date(2020, 11, 17), "104.00", "104.00", "104.00", "104.00"),
            ]
        )
    skips = [entry for entry in logs if entry.get("event") == _MUHURAT_ANCHOR_SKIP_EVENT]
    assert len(skips) == 1
    assert skips[0]["completed_day"] == "2020-11-14"
    assert skips[0]["new_day"] == "2020-11-17"


def test_regular_day_rollover_still_rolls_prev_day_and_emits_no_muhurat_warn() -> None:
    # The non-Muhurat (else) arm: a plain regular→regular rollover overwrites
    # _prev_day_ohlc as before and emits NO Muhurat warn (byte-identical path).
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        builder = Stage1AMatrixBuilder()
        builder.build(
            [
                _day_bar(_date(2024, 1, 2), "100.00", "110.00", "90.00", "105.00"),
                _day_bar(_date(2024, 1, 3), "200.00", "210.00", "190.00", "205.00"),
            ]
        )
    assert not [e for e in logs if e.get("event") == _MUHURAT_ANCHOR_SKIP_EVENT]
    prev = builder._prev_day_ohlc
    assert prev is not None
    # Day 1 (the completed prior REGULAR day) rolled through normally when day 2
    # started — 110 / 90 / 105 rupees → 11000 / 9000 / 10500 paisa. (Day 2 is
    # still the accumulating _today_ohlc; it rolls only on the NEXT day.)
    assert (prev.high_paisa, prev.low_paisa, prev.close_paisa) == (11000, 9000, 10500)


def test_muhurat_day_excluded_from_rolling_weekly_daily_history() -> None:
    # For a NON-prev-day Fib anchor, a completed Muhurat session must ALSO be
    # excluded from the multi-day rolling history (mirroring the weekly-anchor
    # Muhurat skip) — the muhurat branch takes no _daily_history write. We drive
    # the roll seam directly (not build(), which would compute the Muhurat day's
    # OWN weekly anchor — a separate, pre-existing non-trading-`today` refusal in
    # the weekly-anchor resolver, outside this fix's scope): patch out the
    # anchor recompute so we can observe the roll's history bookkeeping alone.
    from brutex.strategies.fib_definitions import FibAnchorKind

    builder = Stage1AMatrixBuilder(fib_anchor=FibAnchorKind.ROLLING_WEEKLY)
    builder._compute_anchors = lambda *a, **k: None  # type: ignore[method-assign]
    # Seed + complete a regular day (D-1) by rolling into the Muhurat day M.
    builder._roll_to_new_day(_date(2020, 11, 13), 10000, 11000, 9000, 10500)
    builder._roll_to_new_day(_date(2020, 11, 14), 10400, 10450, 10350, 10420)
    assert _date(2020, 11, 13) in builder._daily_history
    # Complete the Muhurat day M by rolling into the next regular day D+1.
    builder._roll_to_new_day(_date(2020, 11, 17), 10400, 10400, 10400, 10400)
    assert _date(2020, 11, 14) not in builder._daily_history
    # And the prior REGULAR session's anchor carried through the Muhurat day.
    prev = builder._prev_day_ohlc
    assert prev is not None
    assert (prev.high_paisa, prev.low_paisa, prev.close_paisa) == (11000, 9000, 10500)


# ────────────────────────────────────────────────────────────────────────
# Muhurat weekly-anchor ABSTAIN (DEC-MUHURAT-WEEKLY-ANCHOR-ABSTAIN-001)
# ────────────────────────────────────────────────────────────────────────
#
# The weekly resolvers deliberately REFUSE a non-trading `today` (ValueError —
# a documented, test-pinned honest refusal for direct callers, byte-untouched:
# tests/unit/test_weekly_anchors.py pins it). But the ingest boundary KEEPS
# Muhurat bars (#1294) and every 2020→today sweep span contains Muhurat
# sessions, so Stage-1A calling the resolver on the Muhurat day's OWN roll
# aborted EVERY full-scope ROLLING_WEEKLY / WEEKLY_STATIC grid (the ValueError
# is not a SweepComboError — the grid drivers' tuple-skip quarantine never
# engaged, killing even the PREV_DAY tuples of the same grid). Stage-1A now
# honestly ABSTAINS (fib=None → the existing _NON_FIB_MASK clear) with ONE
# WARN per Muhurat session. These tests FAIL with the fix reverted (the
# pre-fix builder raises ValueError).

from pathlib import Path  # noqa: E402

from brutex.engine.stage1a import (  # noqa: E402
    _MUHURAT_WEEKLY_ANCHOR_EVENT,
    FIB_DEPENDENT_MASK,
)
from brutex.strategies.fib_definitions import FibAnchorKind  # noqa: E402

_WEEKLY_ANCHORS = (FibAnchorKind.ROLLING_WEEKLY, FibAnchorKind.WEEKLY_STATIC)


@pytest.mark.parametrize("anchor", _WEEKLY_ANCHORS)
@pytest.mark.parametrize(("prev_reg", "muhurat", "next_reg"), _MUHURAT_BOUNDARIES)
def test_weekly_anchor_survives_every_real_muhurat_boundary(
    anchor: FibAnchorKind, prev_reg: _date, muhurat: _date, next_reg: _date
) -> None:
    # All six real NSE Diwali Muhurat boundaries 2020-2025, both weekly
    # anchors: build_bar_bits COMPLETES (pre-fix: uncaught ValueError from the
    # weekly resolver's non-trading-`today` refusal) and the Muhurat day's own
    # Group-4 Fib bits read honestly CLEARED, never fabricated.
    from structlog.testing import capture_logs

    bars = [
        _day_bar(prev_reg, "100.00", "110.00", "90.00", "105.00"),
        _day_bar(muhurat, "104.00", "104.50", "103.50", "104.20"),
        _day_bar(next_reg, "104.00", "104.00", "104.00", "104.00"),
    ]
    with capture_logs() as logs:
        masks = build_bar_bits(bars, fib_anchor=anchor)
    assert len(masks) == 3
    # The Muhurat bar's Fib-proximity bits are cleared (combo-ineligible).
    assert masks[1] & FIB_DEPENDENT_MASK == 0
    warns = [e for e in logs if e.get("event") == _MUHURAT_WEEKLY_ANCHOR_EVENT]
    assert len(warns) == 1  # exactly ONE per Muhurat session
    assert warns[0]["day"] == muhurat.isoformat()
    assert warns[0]["fib_anchor"] == anchor.value


def test_muhurat_weekly_abstain_keeps_warm_pivots() -> None:
    # The abstain clears ONLY the Group-4 Fib bits — the Muhurat day's pivots
    # (anchored on the prior REGULAR session) stay warm, exactly like a cold
    # rolling-weekly week (the R-2 convention this rides).
    builder = Stage1AMatrixBuilder(fib_anchor=FibAnchorKind.ROLLING_WEEKLY)
    builder.build(
        [
            _day_bar(_date(2024, 10, 31), "100.00", "110.00", "90.00", "105.00"),
            _day_bar(_date(2024, 11, 1), "104.00", "104.50", "103.50", "104.20"),
        ]
    )
    assert builder._anchors is not None
    assert builder._anchors.fib is None  # honest abstain, never fabricated
    # Pivots are the prior REGULAR session's (PDH 11000 / PDL 9000 / PDC 10500).
    fixed = compute_pivot_levels(
        _date(2024, 11, 1), pdh_paisa=11000, pdl_paisa=9000, pdc_paisa=10500
    )
    assert builder._anchors.pivots.p_paisa == fixed.p_paisa


def test_prev_day_anchor_over_muhurat_bars_unchanged_no_abstain_warn() -> None:
    # PREV_DAY (the sweep default) is byte-identical: the abstain branch lives
    # ONLY on the non-PREV_DAY arm — no warn, no raise, same masks as before.
    from structlog.testing import capture_logs

    bars = [
        _day_bar(_date(2024, 10, 31), "100.00", "110.00", "90.00", "105.00"),
        _day_bar(_date(2024, 11, 1), "104.00", "104.50", "103.50", "104.20"),
        _day_bar(_date(2024, 11, 4), "104.00", "104.00", "104.00", "104.00"),
    ]
    with capture_logs() as logs:
        masks = build_bar_bits(bars)  # default PREV_DAY
    assert len(masks) == 3
    assert not [e for e in logs if e.get("event") == _MUHURAT_WEEKLY_ANCHOR_EVENT]


@pytest.mark.parametrize("anchor", _WEEKLY_ANCHORS)
@pytest.mark.parametrize(("muhurat", "next_reg"), [(m, n) for _, m, n in _MUHURAT_BOUNDARIES])
def test_first_day_muhurat_stream_emits_the_abstain_warn(
    anchor: FibAnchorKind, muhurat: _date, next_reg: _date
) -> None:
    # Round-2 review fix: a bar stream whose FIRST day IS the Muhurat session
    # (an operator from-date landing exactly on e.g. 2024-11-01) used to hit
    # the prev_ohlc-is-None early return BEFORE the abstain branch and roll
    # the session with ZERO warns — silently violating the "one WARN per
    # Muhurat session" law. The abstain WARN now fires on the no-prior-day
    # arm too; the None anchor bundle still clears ALL anchor-dependent bits
    # (a strictly WIDER abstain than fib=None — no prior day exists, so the
    # pivots are cold too). FAILS with the fix reverted (zero warns pre-fix).
    # Additions-cycle widening: ALL SIX real Muhurat dates 2020-2025 are
    # exercised as the stream START (each × both weekly anchors), not just the
    # 2024 boundary — the fix-forward register's D3 completeness item.
    from structlog.testing import capture_logs

    bars = [
        _day_bar(muhurat, "104.00", "104.50", "103.50", "104.20"),
        _day_bar(next_reg, "104.00", "104.00", "104.00", "104.00"),
    ]
    with capture_logs() as logs:
        masks = build_bar_bits(bars, fib_anchor=anchor)
    assert len(masks) == 2
    assert masks[0] & FIB_DEPENDENT_MASK == 0  # no anchors at all on day 1
    warns = [e for e in logs if e.get("event") == _MUHURAT_WEEKLY_ANCHOR_EVENT]
    assert len(warns) == 1  # exactly ONE per Muhurat session — first-day arm
    assert warns[0]["day"] == muhurat.isoformat()
    assert warns[0]["fib_anchor"] == anchor.value


def test_first_day_muhurat_prev_day_stream_stays_silent() -> None:
    # The first-day-Muhurat WARN is weekly-anchor-specific: a PREV_DAY stream
    # starting on the Muhurat date stays silent + byte-identical (the event
    # names an ineligible WEEKLY anchor; PREV_DAY has none to be ineligible).
    from structlog.testing import capture_logs

    bars = [
        _day_bar(_date(2024, 11, 1), "104.00", "104.50", "103.50", "104.20"),
        _day_bar(_date(2024, 11, 4), "104.00", "104.00", "104.00", "104.00"),
    ]
    with capture_logs() as logs:
        masks = build_bar_bits(bars)  # default PREV_DAY
    assert len(masks) == 2
    assert not [e for e in logs if e.get("event") == _MUHURAT_WEEKLY_ANCHOR_EVENT]


def test_weekly_anchor_regular_days_still_resolve_via_common_resolver() -> None:
    # The non-Muhurat arm still dispatches to the COMMON resolver: with 5
    # completed regular sessions in history, the 6th day's rolling-weekly Fib
    # is WARM (non-None) — the abstain guard never fires on a regular day.
    from structlog.testing import capture_logs

    days = [
        _date(2024, 1, 1),
        _date(2024, 1, 2),
        _date(2024, 1, 3),
        _date(2024, 1, 4),
        _date(2024, 1, 5),
        _date(2024, 1, 8),
    ]
    bars = [_day_bar(d, "100.00", "110.00", "90.00", "105.00") for d in days]
    with capture_logs() as logs:
        builder = Stage1AMatrixBuilder(fib_anchor=FibAnchorKind.ROLLING_WEEKLY)
        builder.build(bars)
    assert builder._anchors is not None
    assert builder._anchors.fib is not None  # warm weekly Fib on a regular day
    assert not [e for e in logs if e.get("event") == _MUHURAT_WEEKLY_ANCHOR_EVENT]


def test_weekly_anchor_grid_completes_across_muhurat_day(tmp_path: Path) -> None:
    # The whole-grid abort regression (fails with the fix reverted): a REAL
    # run_sweep_grid over a bar stream containing the 2024-11-01 Muhurat
    # session completes with ZERO failed tuples for a ROLLING_WEEKLY grid —
    # pre-fix the resolver's ValueError escaped build_bar_bits OUTSIDE the
    # SweepComboError tuple-skip quarantine and aborted the WHOLE grid.
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.sweep_orchestrator import (
        SweepGridConfig,
        Timeframe,
        make_store_for,
        run_sweep_grid,
    )
    from brutex.engine.sweep_run import ComboDedup
    from brutex.orchestrator.audit import AuditWriter
    from brutex.strategies.types import Side

    bars = [
        _bar(
            datetime(2024, 10, 31, _OPEN_UTC_H, _OPEN_UTC_M, tzinfo=UTC) + timedelta(minutes=m),
            "100.00",
            "101.00",
            "99.00",
            "100.50",
        )
        for m in range(10)
    ] + [
        # The real 2024-11-01 Muhurat evening session (~18:00 IST == 12:30 UTC).
        _bar(
            datetime(2024, 11, 1, 12, 30, tzinfo=UTC) + timedelta(minutes=m),
            "104.00",
            "104.50",
            "103.50",
            "104.20",
        )
        for m in range(5)
    ]
    grid = SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        fib_anchors=[FibAnchorKind.ROLLING_WEEKLY],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )
    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    with AuditWriter.open(tmp_path / "audit.jsonl") as audit:
        summary = run_sweep_grid(
            grid,
            bars_for=lambda _i, _t: bars,
            store_for=make_store_for(tmp_path / "stores", grid),
            dedup=dedup,
            audit=audit,
        )
    assert summary.tuples_failed == 0


@pytest.mark.parametrize("anchor", _WEEKLY_ANCHORS)
def test_leaked_non_trading_date_bar_still_raises_weekly_resolver_refusal(
    anchor: FibAnchorKind,
) -> None:
    # RESIDUAL HONEST LIMIT pin (disclosed in #1625/#1626 + the
    # _compute_anchors comment — round-1 review): the Muhurat abstain keys
    # ONLY on has_muhurat_session(today). A leaked stored bar on any OTHER
    # non-trading date — e.g. a weekend/holiday bar written by the pre-#1294
    # date-blind ingest filter and never retro-cleaned (arrow_table_to_bars
    # performs no session-date validation) — still drives the non-PREV_DAY
    # branch into the weekly resolvers' plain ValueError, OUTSIDE the grid
    # drivers' SweepComboError quarantine (the same whole-grid abort mechanism
    # the Muhurat abstain closes for Muhurat dates). Pinned as the DOCUMENTED
    # residual behavior, not fixed: remediation is a re-pull / lake census of
    # the leaked partition; a typed quarantinable error is a NAMED follow-up.
    bars = [
        _day_bar(_date(2024, 11, 7), "100.00", "110.00", "90.00", "105.00"),
        _day_bar(_date(2024, 11, 8), "104.00", "105.00", "103.00", "104.50"),
        # A leaked SATURDAY bar (2024-11-09 is not an NSE trading day and not
        # a Muhurat session date).
        _day_bar(_date(2024, 11, 9), "104.00", "104.50", "103.50", "104.20"),
    ]
    with pytest.raises(ValueError, match="not a trading day"):
        build_bar_bits(bars, fib_anchor=anchor)


# ────────────────────────────────────────────────────────────────────────
# collect_batch_inputs — the batch-Rust prepass (DEC-STAGE1A-RUST-BATCH-001)
# ────────────────────────────────────────────────────────────────────────
#
# The prepass duplicates push()'s steps 1-4 and drives the builder's OWN
# roll/day/anchor methods; the field-for-field marshal parity lives in
# tests/unit/test_engine_rust_stage1a.py (the oracle-probe pin). These tests
# cover the prepass's OWN branch matrix: empty input, the spot constant key,
# cold-day placeholders, the three fib anchors, EMA segment restarts at
# rolls, and the G-7 beyond-schedule raise.

from brutex.engine.stage1a import (  # noqa: E402
    _PLACEHOLDER_FIB,  # pyright: ignore[reportPrivateUsage]
    _PLACEHOLDER_PIVOTS,  # pyright: ignore[reportPrivateUsage]
)
from brutex.vocabulary.evaluator import fib_levels_list, pivot_levels_list  # noqa: E402


def test_collect_batch_inputs_empty_bars_yield_empty_columns() -> None:
    inputs = Stage1AMatrixBuilder().collect_batch_inputs([])
    assert inputs.open_paisa == []
    assert inputs.day_has_pivot == []
    assert inputs.day_pivot_levels == []
    assert inputs.ema20_paisa == []


def test_collect_batch_inputs_spot_constant_key_and_no_resets() -> None:
    day1 = [_minute_bar(datetime(2024, 1, 2), i, "100", "110", "90", "105") for i in range(2)]
    day2 = [_minute_bar(datetime(2024, 1, 3), i, "106", "112", "104", "108") for i in range(2)]
    inputs = Stage1AMatrixBuilder().collect_batch_inputs(day1 + day2)
    # The retired session_key/session_day columns are gone (the VWAP wipe) —
    # the spot invariant is now simply "no roll boundary ever fires".
    assert inputs.contract_reset == [False, False, False, False]
    assert inputs.day_index == [0, 0, 1, 1]
    assert inputs.day_has_pivot == [False, True]
    assert inputs.day_has_fib == [False, True]  # prev-day fib warms with pivots
    # Day-1 (cold) rows carry the placeholder levels; day-2 rows are real.
    assert inputs.day_pivot_levels[:12] == pivot_levels_list(_PLACEHOLDER_PIVOTS)
    assert inputs.day_fib_levels[:19] == fib_levels_list(_PLACEHOLDER_FIB)
    assert inputs.day_pivot_levels[12:] != pivot_levels_list(_PLACEHOLDER_PIVOTS)


def test_collect_batch_inputs_cold_weekly_fib_keeps_warm_pivots() -> None:
    # ROLLING_WEEKLY needs 5 completed sessions; a 3-day feed keeps the fib
    # anchor COLD on every day while day-2+ pivots are warm (R-2) — the fib
    # rows stay the placeholder, the pivot rows go real.
    from brutex.strategies.fib_definitions import FibAnchorKind

    days = [_date(2024, 1, 2), _date(2024, 1, 3), _date(2024, 1, 4)]
    bars = [
        _minute_bar(datetime(d.year, d.month, d.day), 0, "100", "110", "90", "105") for d in days
    ]
    inputs = Stage1AMatrixBuilder(fib_anchor=FibAnchorKind.ROLLING_WEEKLY).collect_batch_inputs(
        bars
    )
    assert inputs.day_has_pivot == [False, True, True]
    assert inputs.day_has_fib == [False, False, False]  # cold week
    assert inputs.day_fib_levels[19:38] == fib_levels_list(_PLACEHOLDER_FIB)
    assert inputs.day_pivot_levels[12:24] != pivot_levels_list(_PLACEHOLDER_PIVOTS)


@pytest.mark.parametrize("anchor_name", ["prev_day", "rolling_weekly", "weekly_static"])
def test_collect_batch_inputs_all_three_anchors_produce_valid_tables(anchor_name: str) -> None:
    from brutex.strategies.fib_definitions import FibAnchorKind

    anchor = FibAnchorKind(anchor_name)
    days = [_date(2024, 1, d) for d in (2, 3, 4, 5, 8, 9, 10, 11)]  # 8 trading days
    bars = [
        _minute_bar(datetime(d.year, d.month, d.day), m, "100", "110", "90", "105")
        for d in days
        for m in range(2)
    ]
    inputs = Stage1AMatrixBuilder(fib_anchor=anchor).collect_batch_inputs(bars)
    n_days = len(inputs.day_has_pivot)
    assert n_days == len(days)
    assert len(inputs.day_pivot_levels) == 12 * n_days
    assert len(inputs.day_fib_levels) == 19 * n_days
    assert inputs.day_index == [i for i in range(n_days) for _ in range(2)]
    # Every anchor family eventually warms its fib on this 8-session feed.
    assert inputs.day_has_fib[-1] is True


def test_collect_batch_inputs_ema_segments_restart_at_rolls() -> None:
    # 25 bars per contract day with a roll between: the EMA(20) column must
    # warm within each segment and RESTART (None) after the roll — exactly
    # the streaming builder's per-contract EMA recreation.
    d1, d2 = _date(2025, 6, 26), _date(2025, 6, 27)
    schedule = (d1.toordinal(), _date(2025, 7, 31).toordinal())
    bars = [
        _minute_bar(datetime(d.year, d.month, d.day), m, "100", "110", "90", "105")
        for d in (d1, d2)
        for m in range(25)
    ]
    inputs = Stage1AMatrixBuilder(roll_schedule=schedule).collect_batch_inputs(bars)
    assert inputs.contract_reset[25] is True
    assert sum(inputs.contract_reset) == 1
    # Pre-roll: warm at bar 19 (the 20th bar of segment A).
    assert inputs.ema20_paisa[18] is None
    assert inputs.ema20_paisa[19] is not None
    # Post-roll: the warm-up restarts — None until 20 bars into segment B.
    assert inputs.ema20_paisa[25 + 18] is None
    assert inputs.ema20_paisa[25 + 19] is not None
    # The roll boundary is the ONE reset row — the owning-expiry key itself
    # is no longer a batch column (the VWAP wipe); the cursor agreement is
    # pinned by test_loader_cut_and_cursor_agree_per_bar.


def test_collect_batch_inputs_beyond_schedule_raises_the_push_message() -> None:
    # G-7: the ONE duplicated f-string raises identically from both walks.
    bars = [
        _minute_bar(datetime(2024, 1, 2), 0, "100", "110", "90", "105"),
        _minute_bar(datetime(2024, 1, 3), 0, "100", "110", "90", "105"),
    ]
    schedule = (_date(2024, 1, 2).toordinal(),)
    with pytest.raises(ValueError, match="beyond the last roll_schedule expiry") as prepass_exc:
        Stage1AMatrixBuilder(roll_schedule=schedule).collect_batch_inputs(bars)
    with pytest.raises(ValueError, match="beyond the last roll_schedule expiry") as push_exc:
        Stage1AMatrixBuilder(roll_schedule=schedule).build(bars)
    assert str(prepass_exc.value) == str(push_exc.value)


# ────────────────────────────────────────────────────────────────────────
# Group-15 session-edge gap-midpoint wiring (DEC-GAP-MIDPOINT-CONDITIONS-001)
# ────────────────────────────────────────────────────────────────────────
#
# The STAGE-2 wiring pins: the X1/X2 capture + arm-from-bar-2 ordinal + the
# per-day level mint through the shared Rust ``compute_gap_mid``. The full
# §3.7 battery (the TF=3minute spreadsheet parity golden, the coarse-TF
# zero-fill pin, the P-06 generator extension) lands with the test stage.

from brutex.engine.stage1a import GAP_MID_DEPENDENT_MASK  # noqa: E402


def _gap_bits(mask: int) -> tuple[bool, bool, bool]:
    """(above, below, near) — the Group-15 bit triple of *mask*."""
    return (
        bool(mask >> B.BIT_CLOSE_ABOVE_GAP_MID & 1),
        bool(mask >> B.BIT_CLOSE_BELOW_GAP_MID & 1),
        bool(mask >> B.BIT_NEAR_GAP_MID & 1),
    )


def _gap_up_two_day_feed() -> list[CandleBar]:
    """Day 1 flat-ish; day 2 gaps UP. Hand-derived level:

    X1 = day 1's LAST bar (H 102.00 / L 98.00); X2 = day 2's FIRST bar
    (H 112.00 / L 108.00); open-of-day 110.00 > pdc 100.00 ⇒ gap-UP ⇒
    level = (10200 + 11200) / 2 = 10700 paisa = ₹107.00.
    """
    day1 = datetime(2024, 1, 2)
    day2 = datetime(2024, 1, 3)
    return [
        _minute_bar(day1, 0, "100", "101", "99", "100"),
        _minute_bar(day1, 1, "100", "102", "98", "100"),
        _minute_bar(day2, 0, "110", "112", "108", "111"),
        _minute_bar(day2, 1, "111", "113", "109", "112"),  # close 112 > 107
        _minute_bar(day2, 2, "104", "105", "103", "104"),  # close 104 < 107
        _minute_bar(day2, 3, "107", "107.50", "106.50", "107"),  # close == level
    ]


def test_gap_mid_arms_from_the_second_bar_and_fires_above_below_near() -> None:
    """The headline wiring pin: arm-from-bar-2 + the hand-derived up-level.

    Day 1 (no X1) is dark; day 2's FIRST bar — which mints the level from its
    own extremes — is dark (the arm-from-bar-2 law); later bars compare their
    close against the hand-derived ₹107.00 midpoint: strictly above, strictly
    below, and exactly-at (near fires; the strict above/below do not).
    """
    masks = build_bar_bits(_gap_up_two_day_feed())
    assert all(m & GAP_MID_DEPENDENT_MASK == 0 for m in masks[:2])  # no X1 yet
    assert masks[2] & GAP_MID_DEPENDENT_MASK == 0  # first bar of day 2: dark
    assert _gap_bits(masks[3]) == (True, False, False)  # 112.00 above 107.00
    assert _gap_bits(masks[4]) == (False, True, False)  # 104.00 below 107.00
    assert _gap_bits(masks[5]) == (False, False, True)  # exactly at the level


def test_gap_down_day_uses_the_low_pair() -> None:
    """Gap-DOWN mints from the LOWs: X1_lo 98.00 + X2_lo 88.00 ⇒ ₹93.00."""
    day1 = datetime(2024, 1, 2)
    day2 = datetime(2024, 1, 3)
    masks = build_bar_bits(
        [
            _minute_bar(day1, 0, "100", "101", "99", "100"),
            _minute_bar(day1, 1, "100", "102", "98", "100"),
            # open 90.00 < pdc 100.00 ⇒ gap-DOWN; X2_lo = 88.00.
            _minute_bar(day2, 0, "90", "91", "88", "90"),
            _minute_bar(day2, 1, "92", "92.50", "91.50", "92"),  # close 92 > 93? NO: below
            _minute_bar(day2, 2, "94", "94.50", "93.50", "94"),  # close 94 > 93 ⇒ above
        ]
    )
    # level = (9800 + 8800) / 2 = 9300 paisa = ₹93.00.
    assert _gap_bits(masks[3]) == (False, True, False)  # 92.00 below 93.00
    assert _gap_bits(masks[4]) == (True, False, False)  # 94.00 above 93.00


def test_zero_gap_day_has_no_level_all_day() -> None:
    """``open_of_day == pdc`` exactly ⇒ the kernel returns None ⇒ dark all day."""
    day1 = datetime(2024, 1, 2)
    day2 = datetime(2024, 1, 3)
    masks = build_bar_bits(
        [
            _minute_bar(day1, 0, "100", "101", "99", "100"),
            # Day 2 opens EXACTLY at the prior close (100.00) — zero gap.
            _minute_bar(day2, 0, "100", "103", "97", "102"),
            _minute_bar(day2, 1, "102", "104", "101", "103"),
            _minute_bar(day2, 2, "103", "105", "102", "104"),
        ]
    )
    assert all(m & GAP_MID_DEPENDENT_MASK == 0 for m in masks)
    # Teeth: the pivot anchors ARE warm on day 2 (only the gap level is absent).
    assert any(m & ANCHOR_DEPENDENT_MASK for m in masks[1:])


def test_one_day_shaped_stream_stays_dark_via_the_arming_ordinal() -> None:
    """The load-bearing 1day abstain (design §3.2 mechanism correction).

    On a 1day-shaped stream EVERY bar is its session's FIRST TF bar, so the
    arm-from-bar-2 ordinal keeps bits 66-68 dark — the manifest
    ``applies_to_1day`` flag is production-dormant on the spot-only engine,
    so the ordinal delivers the abstain, NOT the mask.
    """
    bars = [
        _minute_bar(datetime(2024, 1, 2 + i), 0, o, h, lo, c)
        for i, (o, h, lo, c) in enumerate(
            [
                ("100", "101", "99", "100"),
                ("110", "112", "108", "111"),  # gap-up vs 100
                ("104", "105", "103", "104"),  # gap-down vs 111
                ("115", "116", "114", "115"),  # gap-up vs 104
            ]
        )
    ]
    masks = build_bar_bits(bars)
    assert all(m & GAP_MID_DEPENDENT_MASK == 0 for m in masks)
    # Teeth: the day-type gap bits DO fire (anchors warm; only 66-68 abstain).
    assert masks[1] & (1 << B.BIT_GAP_UP_DAY)
    assert masks[2] & (1 << B.BIT_GAP_DOWN_DAY)


def test_muhurat_session_never_becomes_x1() -> None:
    """The #1616 skip law on the edge: the prior REGULAR session's last TF bar
    serves as X1 across a Diwali Muhurat boundary.

    Hand-derived discriminator: correct X1_hi = the regular session's last bar
    high 106.00 ⇒ level = (10600 + 10900) / 2 = ₹107.50 ⇒ a 107.00 close reads
    BELOW. A poisoned X1 (the Muhurat bar's high 104.50) would give
    (10450 + 10900) / 2 = ₹106.75 ⇒ the same close would read ABOVE.
    """
    prev_reg, muhurat, next_reg = _date(2020, 11, 13), _date(2020, 11, 14), _date(2020, 11, 17)

    def _two(d: _date, specs: list[tuple[str, str, str, str]]) -> list[CandleBar]:
        return [
            _minute_bar(datetime(d.year, d.month, d.day), m, o, h, lo, c)
            for m, (o, h, lo, c) in enumerate(specs)
        ]

    bars = (
        _two(prev_reg, [("100", "110", "90", "105"), ("105", "106", "104", "105")])
        + _two(muhurat, [("104", "104.50", "103.50", "104.20")])
        # open 108.00 > the REGULAR session's pdc 105.00 ⇒ gap-UP.
        + _two(next_reg, [("108", "109", "107", "108"), ("107", "107.20", "106.80", "107")])
    )
    masks = build_bar_bits(bars)
    assert _gap_bits(masks[-1]) == (False, True, False)  # 107.00 below 107.50


def test_sub_threshold_gap_still_mints_a_level() -> None:
    """The no-threshold direction law (design §3.1): a gap below the day-type
    bits' 0.5% threshold still mints a gap-mid level — bits 66-68 and 48/49
    legitimately disagree on sub-threshold days.
    """
    day1 = datetime(2024, 1, 2)
    day2 = datetime(2024, 1, 3)
    masks = build_bar_bits(
        [
            _minute_bar(day1, 0, "100", "100.10", "99.90", "100"),
            # +3 paisa gap (< the 50-paisa 0.5% threshold of bits 48/49).
            _minute_bar(day2, 0, "100.03", "100.20", "99.95", "100.10"),
            # level = (10010 + 10020) / 2 = 10015; close 100.50 is above.
            _minute_bar(day2, 1, "100.40", "100.60", "100.30", "100.50"),
        ]
    )
    assert _gap_bits(masks[2]) == (True, False, False)
    assert not masks[2] & (1 << B.BIT_GAP_UP_DAY)  # sub-threshold: 48 stays 0


def test_contract_roll_clears_the_gap_mid_state_members() -> None:
    """D11 total-reset membership: the three Group-15 members join the
    ``_roll_to_new_contract`` enumerated clear list — post-roll state ==
    fresh-builder state (the roll-segmentation equivalence oracle's premise).
    """
    builder = Stage1AMatrixBuilder(roll_schedule=_ROLL_SCHEDULE)
    seg_a, _seg_b = _two_contract_stream()
    for bar in seg_a:
        builder.push(bar)
    # Warm gap state exists pre-roll (segment A spans two days).
    assert builder._session_edge is not None
    assert builder._prev_session_edge is not None
    builder._roll_to_new_contract()
    fresh = Stage1AMatrixBuilder()
    assert builder._session_edge is None is fresh._session_edge
    assert builder._prev_session_edge is None is fresh._prev_session_edge
    assert builder._gap_mid_paisa is None is fresh._gap_mid_paisa


def test_collect_batch_inputs_emits_the_x1_day_tables() -> None:
    """The prepass emits the Group-15 X1 day rows + the bps knob — the batch
    core's mint inputs (the lockstep half of the wiring; a missing emission
    would burn-in-mismatch and autorevert).
    """
    bars = _gap_up_two_day_feed()
    inputs = Stage1AMatrixBuilder().collect_batch_inputs(bars)
    assert inputs.day_has_x1 == [False, True]
    assert inputs.day_x1_hi_paisa == [0, 10200]  # day 1's last bar H 102.00
    assert inputs.day_x1_lo_paisa == [0, 9800]  # day 1's last bar L 98.00
    assert inputs.gap_mid_bps == 10  # the ProximityParams default
    # A custom bps knob threads through the SAME field.
    from brutex.vocabulary.evaluator import ProximityParams

    custom = Stage1AMatrixBuilder(
        proximity=ProximityParams(gap_mid_proximity_bps=25)
    ).collect_batch_inputs(bars)
    assert custom.gap_mid_bps == 25


# ────────────────────────────────────────────────────────────────────────
# Group-15 §3.7 battery (the test stage, DEC-GAP-MIDPOINT-CONDITIONS-001)
# ────────────────────────────────────────────────────────────────────────

from brutex.engine.combo_mask import mask_to_entry_signals  # noqa: E402
from brutex.engine.trade_sim import simulate_trades  # noqa: E402
from brutex.strategies.types import Side  # noqa: E402


def _tf_bar(
    day: datetime,
    bucket: int,
    tf_minutes: int,
    o: str,
    h: str,
    lo: str,
    c: str,
) -> CandleBar:
    """One TF bar at ``09:15 IST + tf_minutes * bucket`` on ``day``."""
    return _minute_bar(day, tf_minutes * bucket, o, h, lo, c)


@pytest.mark.parametrize("tf_minutes", [3, 5, 15, 30])
def test_gap_mid_arms_from_bar_2_on_each_tf(tf_minutes: int) -> None:
    """The arm-from-bar-2 law holds PER TIMEFRAME (design §3.3): on each of
    several TF-shaped bar streams the Group-15 bits are 0 on the session's
    first TF bar and can arm from the second — the per-TF generalization the
    operator steer mandates (Stage-1A evaluates whatever TF bars the sweep
    loads; the arming ordinal is bucket-grid-agnostic).
    """
    day1, day2 = datetime(2024, 1, 2), datetime(2024, 1, 3)
    bars = [
        _tf_bar(day1, 0, tf_minutes, "100", "101", "99", "100"),
        _tf_bar(day1, 1, tf_minutes, "100", "102", "98", "100"),
        # Gap-UP: open 110.00 > pdc 100.00; X1_hi 102.00 + X2_hi 112.00 ⇒ ₹107.00.
        _tf_bar(day2, 0, tf_minutes, "110", "112", "108", "111"),
        _tf_bar(day2, 1, tf_minutes, "111", "113", "109", "112"),  # 112.00 above
        _tf_bar(day2, 2, tf_minutes, "104", "105", "103", "104"),  # 104.00 below
    ]
    masks = build_bar_bits(bars)
    assert masks[2] & GAP_MID_DEPENDENT_MASK == 0  # session's FIRST TF bar: dark
    assert _gap_bits(masks[3]) == (True, False, False)  # second TF bar: armed
    assert _gap_bits(masks[4]) == (False, True, False)


def test_first_stream_day_has_no_x1_and_stays_dark() -> None:
    """The dedicated first-stream-day pin (§3.7): with no prior session there
    is NO X1, so bits 66-68 stay 0 on every bar of the stream's first day —
    including bars 2+ (the arming ordinal alone would have armed them)."""
    day = datetime(2024, 1, 2)
    bars = [
        _minute_bar(day, 0, "100", "101", "99", "100"),
        _minute_bar(day, 1, "110", "112", "108", "111"),
        _minute_bar(day, 2, "104", "105", "103", "104"),
    ]
    builder = Stage1AMatrixBuilder()
    masks = [builder.push(bar) for bar in bars]
    assert all(m & GAP_MID_DEPENDENT_MASK == 0 for m in masks)
    assert builder._prev_session_edge is None  # no prior session ⇒ no X1
    assert builder._gap_mid_paisa is None


def test_near_gap_mid_exact_bps_boundary() -> None:
    """The ``is_near`` law at its EXACT edge (§3.7): near fires iff
    ``|close − level| <= close * bps div 10_000`` — inclusive at equality,
    off one paisa past it (default 10 bps).

    Hand arithmetic: level = (X1_hi 998000 + X2_hi 1000000) / 2 = 999000
    paisa (₹9990.00). Probe A closes at ₹10000.00: tol = 1000000·10 div
    10000 = 1000 paisa and |close − level| = 1000 — EXACTLY at the edge ⇒
    near fires (co-firing with strictly-above, the PDH/PDL convention).
    Probe B closes ONE paisa further (₹10000.01): tol = 1000001·10 div
    10000 = 1000 paisa still, but |close − level| = 1001 ⇒ near is off.
    """
    day1, day2 = datetime(2024, 1, 2), datetime(2024, 1, 3)
    masks = build_bar_bits(
        [
            _minute_bar(day1, 0, "9900", "9905", "9895", "9900"),
            _minute_bar(day1, 1, "9900", "9980", "9890", "9900"),  # X1_hi ₹9980
            # open 10000.00 > pdc 9900.00 ⇒ gap-UP; X2_hi ₹10000.00.
            _minute_bar(day2, 0, "10000", "10000", "9950", "9960"),
            _minute_bar(day2, 1, "9995", "10005", "9990", "10000"),  # probe A
            _minute_bar(day2, 2, "10000", "10005", "9995", "10000.01"),  # probe B
        ]
    )
    assert _gap_bits(masks[3]) == (True, False, True)  # diff == tol: near fires
    assert _gap_bits(masks[4]) == (True, False, False)  # diff == tol + 1: near off


def test_truncated_final_bucket_tf_uses_the_last_stored_bucket_as_x1() -> None:
    """The truncated-final-bucket pin (§3.7): on TF=30minute the session's
    last 09:15-anchored bucket is 15:15 (covering 15:15-15:29 — TRUNCATED,
    375 % 30 != 0) and it — not the last FULL bucket (14:45) — is X1.

    Discriminator: the 14:45 bucket carries a HIGHER high (108.00) than the
    15:15 bucket (105.00); the served level (₹108.50) comes from the 15:15
    extreme — a last-FULL-bucket anchor would mint ₹110.00 and flip the
    probe's verdict.
    """
    day1, day2 = datetime(2024, 1, 2), datetime(2024, 1, 3)
    day1_bars = [
        _tf_bar(day1, k, 30, "100", "102", "98", "100")
        for k in range(11)  # 09:15 .. 14:15
    ] + [
        _tf_bar(day1, 11, 30, "100", "108", "98", "100"),  # 14:45 — last FULL bucket
        _tf_bar(day1, 12, 30, "100", "105", "98", "100"),  # 15:15 — last STORED bucket
    ]
    ist_last = day1_bars[-1].timestamp.astimezone(IST)
    assert (ist_last.hour, ist_last.minute) == (15, 15)
    bars = [
        *day1_bars,
        # Gap-UP: open 110.00 > pdc 100.00; X2_hi 112.00.
        _tf_bar(day2, 0, 30, "110", "112", "108", "111"),
        _tf_bar(day2, 1, 30, "108", "110", "107", "109"),  # probe close ₹109.00
    ]
    builder = Stage1AMatrixBuilder()
    for bar in bars[:-1]:
        builder.push(bar)
    # level = (10500 + 11200) / 2 = 10850 — from the 15:15 (truncated) bucket.
    assert builder._gap_mid_paisa == 10850
    masks = build_bar_bits(bars)
    # close 109.00 is ABOVE ₹108.50; under the wrong (14:45) anchor the level
    # would be (10800 + 11200) / 2 = ₹110.00 and the same close would read
    # BELOW — the probe discriminates the bucket identity.
    assert _gap_bits(masks[-1]) == (True, False, False)


def test_data_hole_prior_session_tail_missing_x1_is_last_stored_bar() -> None:
    """The honest data-hole limit (§3.3): when the prior session's TAIL is
    missing from the lake (a #1031-class hole), X1 = the last STORED TF bar
    of that session — data-as-is, no fabrication, no NSE-clock
    reconstruction."""
    day1, day2 = datetime(2024, 1, 2), datetime(2024, 1, 3)
    bars = [
        # Day 1 ends at 09:21 IST — the rest of the session is a lake hole.
        _minute_bar(day1, 0, "100", "101", "99", "100"),
        _minute_bar(day1, 3, "100", "102", "98", "100"),
        _minute_bar(day1, 6, "101", "103", "100", "101"),  # last STORED bar
        # Gap-UP: open 105.00 > pdc 101.00; X2_hi 107.00.
        _minute_bar(day2, 0, "105", "107", "104", "106"),
        _minute_bar(day2, 1, "106", "108", "105", "106"),  # probe close ₹106.00
    ]
    builder = Stage1AMatrixBuilder()
    for bar in bars[:-1]:
        builder.push(bar)
    # level = (10300 + 10700) / 2 = 10500 — the 09:21 stored bar is X1.
    assert builder._gap_mid_paisa == 10500
    masks = build_bar_bits(bars)
    assert _gap_bits(masks[-1]) == (True, False, False)  # 106.00 above 105.00


# ── The disclosed coarse-TF trade-inertness (design §3.4, posture (a)) ──


def _coarse_tf_gap_corpus(tf_minutes: int) -> list[CandleBar]:
    """A three-day gap corpus on the 09:15-anchored ``tf_minutes`` grid.

    Day 2 gaps UP (open 110.00 > pdc 100.00; level = (X1_hi 10200 + X2_hi
    11600) / 2 = ₹109.00) and every armed day-2 bar closes at ₹115.00 —
    strictly above — so a ``close_above_gap_mid``-gated mask genuinely
    SIGNALS; whether a signal can ever FILL is exactly the per-TF structure
    under test. Day 3 exists so a last-bar signal's next bar is a LATER
    session (the overnight-gap drop, R2-OC-2), never merely end-of-stream.
    """
    day1, day2, day3 = (datetime(2024, 1, 2 + i) for i in range(3))
    buckets = 375 // tf_minutes + (1 if 375 % tf_minutes else 0)
    bars: list[CandleBar] = []
    for k in range(buckets):
        bars.append(_tf_bar(day1, k, tf_minutes, "100", "102", "98", "100"))
    bars.append(_tf_bar(day2, 0, tf_minutes, "110", "116", "108", "115"))
    for k in range(1, buckets):
        bars.append(_tf_bar(day2, k, tf_minutes, "115", "117", "113", "115"))
    for k in range(buckets):
        bars.append(_tf_bar(day3, k, tf_minutes, "120", "122", "118", "120"))
    return bars


def test_gap_mid_gated_masks_are_zero_trade_on_tf_240() -> None:
    """The §3.4 disclosed inertness on TF=240, pinned deterministically under
    the DEC-FILL-EXEC-GRID-1M-001 execution grid: a mask whose ENTRY is gated
    by bits 66-68 is structurally ZERO-TRADE.

    TF=240 has session buckets {09:15, 13:15}. Bucket 0 (09:15) is the
    session's FIRST bar so the gap bits stay dark (the §3.3 arm-from-bar-2
    law), leaving the only armed+signalling bar at 13:15 — the session's LAST
    bucket. Its fill bar is the first exec bar at/after its close (18:15),
    which lands in the NEXT session, so the ENTRY fill is cross-session and
    DROPPED. Both day-2 and day-3 signals share this shape, so no position
    ever opens. The bits stay EVALUABLE (honest mask semantics; the signals
    genuinely fire — the non-vacuity assert) — the sweep simply scores the
    mask zero-trade.
    """
    bars = _coarse_tf_gap_corpus(240)
    signals = mask_to_entry_signals(build_bar_bits(bars), 1 << B.BIT_CLOSE_ABOVE_GAP_MID)
    assert any(signals)  # non-vacuous: the gap-gated mask DOES signal
    trades = simulate_trades(
        bars,
        signals,
        direction=Side.BUY,
        tf_minutes=240,
        exec_bars_1m=bars,
        stop_loss_paisa=80,
        take_profit_paisa=50,
    )
    assert trades == []  # …and can structurally never fill (cross-session entry)


def test_gap_mid_gated_mask_terminal_close_on_tf_180() -> None:
    """TF=180 is NOT zero-trade under the DEC-FILL-EXEC-GRID-1M-001 grid — the
    honest measured behavior, pinned (the pre-exec-grid ``== []`` pin was
    stale; the session_gap/terminal-close law (§2.4/B3, #1743 finish() basis)
    postdates it).

    TF=180 has session buckets {09:15, 12:15, 15:15}. The 12:15-armed signal
    fills into the 15:15 bar (its close instant 15:15 is reached by the next
    exec bar in the SAME session), the position OPENS at that bar's adverse
    high — but 15:15 is the session's LAST bucket so no later same-session bar
    exists to exit into, and it CLOSES via ``session_gap`` at that bar's CLOSE
    (the B3 terminal carve-out, exit_idx == entry_idx). The day-3 12:15 signal
    fills at the stream's final 15:15 bar and CLOSES via ``end_of_data`` by the
    same carve-out. Each is a −200 loss (entry at the fill bar's worst-case
    high 117/122, exit at that bar's close 115/120). The two 15:15-armed
    signals still drop cross-session (no next exec bar in-session).
    """
    bars = _coarse_tf_gap_corpus(180)
    signals = mask_to_entry_signals(build_bar_bits(bars), 1 << B.BIT_CLOSE_ABOVE_GAP_MID)
    assert any(signals)  # non-vacuous: the gap-gated mask DOES signal
    trades = simulate_trades(
        bars,
        signals,
        direction=Side.BUY,
        tf_minutes=180,
        exec_bars_1m=bars,
        stop_loss_paisa=80,
        take_profit_paisa=50,
    )
    # ClosedTrade carries pnl/holding (the exit reason lives on TradeDetail);
    # the two terminal-close carve-outs are each a −200 one-bar loss.
    assert [(t.pnl_paisa, t.holding_bars) for t in trades] == [(-200, 1), (-200, 1)]


def test_gap_mid_gated_mask_can_trade_on_tf_120() -> None:
    """The TF=120 contrast (partial inertness only): buckets {09:15, 11:15,
    13:15, 15:15} — an 11:15-armed signal fills at the 13:15 bar (its
    [13:15, 15:15) window never reaches 15:20 ⇒ not forced, same session),
    so the zero-trade pins above are about the 180/240 session-bucket
    structure, not a vacuously dead mask."""
    bars = _coarse_tf_gap_corpus(120)
    signals = mask_to_entry_signals(build_bar_bits(bars), 1 << B.BIT_CLOSE_ABOVE_GAP_MID)
    trades = simulate_trades(
        bars,
        signals,
        direction=Side.BUY,
        tf_minutes=120,
        exec_bars_1m=bars,
        stop_loss_paisa=80,
        take_profit_paisa=50,
    )
    assert len(trades) >= 1
