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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.stage1a import (
    ANCHOR_DEPENDENT_MASK,
    Stage1AMatrixBuilder,
    WalkForwardFold,
    _ema_to_paisa,
    _price_to_paisa,
    _walk_forward_schedule_hash,
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
