"""Golden corpus #2 — BANKNIFTY, one week (session window + holiday pruning).

DEC-PHASE2-CORPORA-001 (``tests/golden/fixtures/README.md`` corpus #2 —
"BANKNIFTY — 1 week / session window + holiday-calendar pruning, P-22, P-23").

Two real components, two properties:

* **P-23 holiday-calendar pruning** — :func:`brutex.calendars.nse.trading_days_between`
  over a calendar week that contains a verified NSE holiday returns only the
  genuine trading days. The chosen week is **2024-08-12 → 2024-08-18**:
  Independence Day **2024-08-15 (Thu)** is a published NSE holiday
  (``calendars/nse.py`` ``HOLIDAYS_2024``; CLAUDE.md §7) and **17/18 Aug** are
  the weekend, so the five-weekday slot prunes to the **4 trading days**
  Mon 12 / Tue 13 / Wed 14 / Fri 16.
* **P-22 session window + multi-day rollover** — the REAL Stage-1A builder
  (:func:`brutex.engine.stage1a.build_bar_bits`) walks a feed restricted to the
  09:15-15:30 IST session window across those 4 pruned days, exercising the IST
  day-rollover that turns each completed day's OHLC into the next day's anchors.

Why the masks are hand-derivable (correct by construction)
----------------------------------------------------------
Each trading day carries **4 flat-doji bars** at one well-separated price
(day ``k`` = ``50000 + 1000·k`` rupees — BANKNIFTY scale, Aug-2024), one per
time-of-day band: 09:16 (EARLY 44), 10:00 (MID 45), 11:30 (MIDDAY 46),
13:00 (AFTERNOON 47). 16 bars total — deliberately **< 20** so the EMA(20)
never warms (no EMA bits to hand-compute), and the day-to-day price gap is far
wider than the 10 bps ``near`` tolerance and the 0.5 % gap threshold, so:

* **flat doji** ⇒ ``CLOSE_AT_DAY_HIGH`` (40) + ``CLOSE_AT_DAY_LOW`` (41), zero
  range ⇒ no candle / day-third bit (same closed form as corpus #1);
* **time-of-day** ⇒ exactly the band bit for that bar;
* **prior-bar** ⇒ ``PRIOR_N_BEARISH`` (38) from the 4th bar (index ≥ 3) on
  (every doji flag is bearish, ``prior_n == 3``);
* **day 1** (first day, bars 0-3) ⇒ all anchor-dependent bits cleared;
* **days 2-4** (rising price vs the prior flat day) ⇒ ``CLOSE_ABOVE_PDH`` (13)
  + ``CLOSE_ABOVE_PDL`` (15) + ``GAP_UP_DAY`` (48); the prior day was flat so
  every pivot/Fib level collapses onto that single price, far from today's
  close ⇒ no ``near_*`` bit; inside/outside need a two-sided range ⇒ neither.
  The 60→66 reshape adds the CPR pair: a FLAT prior day makes
  ``P = BC = TC =`` that single price (width 0), so today's higher close is
  strictly above TC ⇒ ``ABOVE_CPR_TC`` (60), and the zero width is trivially
  ≤ 30 bps of P ⇒ ``NARROW_CPR_DAY`` (63).

So bar ``i`` (day ``i // 4``, band ``i % 4``) is the closed form in
:func:`_expected_mask`, asserted bar-for-bar AND pinned by SHA-256.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.calendars.nse import IST, is_holiday, is_weekend, trading_days_between
from brutex.data.models import CandleBar
from brutex.engine.stage1a import ANCHOR_DEPENDENT_MASK, build_bar_bits
from brutex.vocabulary import bits as B

#: The probe week (Mon → Sun) straddling Independence Day + the weekend.
_WEEK_START = date(2024, 8, 12)
_WEEK_END = date(2024, 8, 18)
#: The verified NSE holiday inside that week (CLAUDE.md §7 / HOLIDAYS_2024).
_HOLIDAY = date(2024, 8, 15)
#: The hand-derived pruned trading days (holiday + weekend removed).
_EXPECTED_TRADING_DAYS = [
    date(2024, 8, 12),
    date(2024, 8, 13),
    date(2024, 8, 14),
    date(2024, 8, 16),
]

#: Time-of-day bands probed per day — (hour, minute, expected Group-8 bit).
_BANDS = [
    (9, 16, B.BIT_EARLY_MORNING),
    (10, 0, B.BIT_MID_MORNING),
    (11, 30, B.BIT_MIDDAY),
    (13, 0, B.BIT_AFTERNOON),
]
_BARS_PER_DAY = len(_BANDS)

#: SHA-256 of the frozen 16-mask matrix (the README "SHA-256 manifest" guard).
# DELIBERATE RE-LOCK (the vocab-66 SuperTrend flip — bars 9-11 gain the
# hand-derived bit 65, bars 12-15 bit 64; see the closed form + the in-file
# ×2-domain audit above). Old digests:
# 60→66 reshape: a925eb6be4a0dc8beac67ab435db0a87eb9e9d65a3c88ad03f73289a295183c3;
# pre-reshape:  c5d1c3c2523c519a13d7c36f0be62539c80182cf44ba69a965897c0a2a63da5c.
_EXPECTED_SHA256 = "3b56968a1807ca7ea2f29c089a90d3ff3a98f721e6c1f0ed523804a363f84a98"


def _price(day_index: int) -> Decimal:
    """Well-separated flat-doji price for trading day ``day_index`` (BANKNIFTY scale)."""
    return Decimal(50000 + 1000 * day_index)


def _bar(d: date, hour: int, minute: int, price: Decimal) -> CandleBar:
    ts = datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=0,
        open_interest=None,
    )


def _feed() -> list[CandleBar]:
    """4 flat-doji bars (one per band) for each of the 4 pruned trading days."""
    bars: list[CandleBar] = []
    for day_index, d in enumerate(_EXPECTED_TRADING_DAYS):
        price = _price(day_index)
        for hour, minute, _bit in _BANDS:
            bars.append(_bar(d, hour, minute, price))
    return bars


def _expected_mask(i: int) -> int:
    """Closed-form expected ``u64`` for bar ``i`` (see module docstring)."""
    day_index, band = divmod(i, _BARS_PER_DAY)
    mask = (1 << B.BIT_CLOSE_AT_DAY_HIGH) | (1 << B.BIT_CLOSE_AT_DAY_LOW)
    mask |= 1 << _BANDS[band][2]
    if i >= 3:  # prior_n == 3 filled with all-bearish doji flags
        mask |= 1 << B.BIT_PRIOR_N_BEARISH
    if day_index >= 1:  # rising vs the prior (flat) day → above-PDH/PDL + gap-up
        mask |= (1 << B.BIT_CLOSE_ABOVE_PDH) | (1 << B.BIT_CLOSE_ABOVE_PDL)
        mask |= 1 << B.BIT_GAP_UP_DAY
        # The flat prior day's CPR collapses to one price: P = BC = TC =
        # prev price ⇒ close (1000 pts higher) is strictly above TC (60),
        # and width 0 ≤ 30 bps of P ⇒ narrow (63) — the 60→66 reshape.
        mask |= (1 << B.BIT_ABOVE_CPR_TC) | (1 << B.BIT_NARROW_CPR_DAY)
    # Group 14 (the vocab-66 SuperTrend flip) — the hand-derived ×2-domain
    # walk in the module docstring: warm at bar 9 on the DOWN-seeded upper
    # band above the flat close (bars 9-11), the day-4 gap-up breaks the
    # ratcheted upper band at bar 12 → flip UP, lower band under the close.
    if 9 <= i <= 11:
        mask |= 1 << B.BIT_CLOSE_BELOW_SUPERTREND
    elif i >= 12:
        mask |= 1 << B.BIT_CLOSE_ABOVE_SUPERTREND
    return mask


def _manifest(masks: list[int]) -> str:
    return hashlib.sha256(",".join(str(m) for m in masks).encode("utf-8")).hexdigest()


def test_holiday_calendar_prunes_the_week() -> None:
    """P-23: the week prunes to exactly the 4 genuine trading days."""
    assert trading_days_between(_WEEK_START, _WEEK_END) == _EXPECTED_TRADING_DAYS
    assert is_holiday(_HOLIDAY)
    assert is_weekend(date(2024, 8, 17))
    assert is_weekend(date(2024, 8, 18))


def test_week_matrix_is_one_to_one_with_bars() -> None:
    """One mask per session-window bar across the pruned week (4 days × 4 bars)."""
    masks = build_bar_bits(_feed())
    assert len(masks) == _BARS_PER_DAY * len(_EXPECTED_TRADING_DAYS) == 16


def test_week_matrix_matches_hand_derived_closed_form() -> None:
    """Every mask equals its hand-derived closed form across the day rollover."""
    masks = build_bar_bits(_feed())
    assert masks == [_expected_mask(i) for i in range(16)]


def test_first_day_clears_anchors_later_days_define_them() -> None:
    """P-22 rollover: day-1 anchors cleared; days 2-4 fire the PDH anchor bit."""
    masks = build_bar_bits(_feed())
    # Day 1 (bars 0-3): every anchor-dependent bit cleared (no prior day).
    assert all((masks[i] & ANCHOR_DEPENDENT_MASK) == 0 for i in range(_BARS_PER_DAY))
    # Days 2-4 (bars 4-15): the rollover defines the prior-day anchor → CLOSE_ABOVE_PDH.
    assert all((masks[i] & (1 << B.BIT_CLOSE_ABOVE_PDH)) != 0 for i in range(_BARS_PER_DAY, 16))


def test_week_matrix_sha256_matches_pinned_manifest() -> None:
    """The 16-mask matrix hashes to the pinned drift manifest."""
    assert _manifest(build_bar_bits(_feed())) == _EXPECTED_SHA256
