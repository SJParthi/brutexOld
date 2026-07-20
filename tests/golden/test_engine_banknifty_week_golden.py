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
  ≤ 30 bps of P ⇒ ``NARROW_CPR_DAY`` (63);
* **gap-midpoint** (``DEC-GAP-MIDPOINT-CONDITIONS-001``, hand-derived): on
  day ``k ≥ 1`` X1 = the prior flat day's last bar = ``P_{k-1}``, X2 = today's
  first flat bar = ``P_k``, gap-UP (``open P_k > pdc P_{k-1}``) ⇒ level =
  ``(P_{k-1} + P_k) / 2 = P_k − ₹500`` — today's close ``P_k`` is strictly
  ABOVE it ⇒ ``CLOSE_ABOVE_GAP_MID`` (66) on every bar EXCEPT the day's first
  (the arm-from-bar-2 law leaves band 0 dark); ₹500 ≫ the 10 bps ``near``
  tolerance (~₹51) ⇒ no ``NEAR_GAP_MID``.

So bar ``i`` (day ``i // 4``, band ``i % 4``) is the closed form in
:func:`_expected_mask`, asserted bar-for-bar AND pinned by SHA-256.

SuperTrend ×2-domain audit (bits 64/65) — the in-file hand-derivation walk
---------------------------------------------------------------------------
The 16-bar feed warms the ATR-10 SuperTrend at 0-based bar 9, so the Group-14
bits fire on bars 9-15 (the futures_roll golden's audit style). Everything
below is in the **×2-scaled paisa domain** (one unit = half a paisa;
``rust/trading-core/src/indicators/supertrend.rs``): a flat doji at ``P``
rupees has ``hl2_x2 = close_x2 = 2 · P · 100`` paisa, so the four day prices
50,000 / 51,000 / 52,000 / 53,000 give ``close_x2`` = 10,000,000 /
10,200,000 / 10,400,000 / 10,600,000.

TR (paisa; first bar ``H−L``, then ``max(H−L, |H−C_prev|, |L−C_prev|)``) is
0 on every flat bar EXCEPT each day's first bar after a gap: bars 4, 8 and 12
carry ``TR = 100,000`` (the 1,000-rupee day gap), so ``tr_x2 = 200,000``
there and 0 elsewhere.

* **bar 9** — ATR seed = SMA(10) of ``tr_x2[0..=9]`` =
  ``(200,000 + 200,000) / 10`` = **40,000** (truncating). basicUpper =
  ``10,400,000 + 3·40,000`` = **10,520,000**; ``atr[1]`` is still na so the
  direction seeds +1 (DOWN) and the line sits on the upper band.
  ``close_x2`` 10,400,000 < 10,520,000 ⇒ **bit 65**.
* **bar 10** — Wilder: atr = ``(40,000·9 + 0)/10`` = 36,000; basicUpper =
  ``10,400,000 + 108,000`` = **10,508,000** < prevFinalUpper 10,520,000 ⇒
  the band ratchets down; close 10,400,000 ≯ it ⇒ stays DOWN ⇒ bit 65.
* **bar 11** — atr = ``(36,000·9)/10`` = 32,400; basicUpper =
  ``10,400,000 + 97,200`` = **10,497,200** (ratchets again); bit 65.
* **bar 12** — the day-4 gap (``close_x2`` 10,600,000; ``tr_x2`` 200,000):
  atr = ``(32,400·9 + 200,000)/10`` = ``491,600/10`` = 49,160. finalUpper
  HOLDS at 10,497,200 (basicUpper 10,747,480 is looser and ``close[1]``
  10,400,000 never closed above the band); close 10,600,000 > 10,497,200 ⇒
  **FLIP** to −1 (UP) ⇒ line = finalLower = basicLower =
  ``10,600,000 − 147,480`` = **10,452,520** (> prevFinalLower 10,302,800,
  so the basic band IS the final band); close > line ⇒ **bit 64**.
* **bar 13** — atr = ``(49,160·9)/10`` = 44,244; basicLower =
  ``10,600,000 − 132,732`` = **10,467,268** (ratchets up); bit 64.
* **bar 14** — atr = ``(44,244·9)/10`` = 39,819 (truncating 39,819.6);
  basicLower = ``10,600,000 − 119,457`` = **10,480,543**; bit 64.
* **bar 15** — atr = ``(39,819·9)/10`` = 35,837 (truncating 35,837.1);
  basicLower = ``10,600,000 − 107,511`` = **10,492,489**; bit 64.

Every line value above is reproduced EXACTLY by the installed
``trading_core`` 0.5.0 kernel (re-derived from this fixture's bars,
2026-07-17), giving the closed form in :func:`_expected_mask`: bars 9-11
carry bit 65 (close below the upper-band line), bars 12-15 bit 64.
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
# DELIBERATE RE-LOCK, composed at the vocab-train fold: the vocab-66
# SuperTrend flip added the hand-derived bit 65 on bars 9-11 and bit 64 on
# bars 12-15 (the in-file x2-domain audit), and DEC-GAP-MIDPOINT-CONDITIONS-001
# added bit 66 on bars {5, 6, 7, 9, 10, 11, 13, 14, 15} — the non-first bars
# of days 2-4 (the arm-from-bar-2 law; the gap-mid level is the flat midpoint
# P_k − ₹500, see module docstring). Every pre-existing bit byte-identical.
# Digest recomputed from the composed closed form (machine-checked against the
# wheel replay by this suite). Old digests:
# ST-era (vocab-66 flip): 3b56968a1807ca7ea2f29c089a90d3ff3a98f721e6c1f0ed523804a363f84a98;
# 60→66 reshape: a925eb6be4a0dc8beac67ab435db0a87eb9e9d65a3c88ad03f73289a295183c3;
# pre-reshape:  c5d1c3c2523c519a13d7c36f0be62539c80182cf44ba69a965897c0a2a63da5c.
_EXPECTED_SHA256 = "60a8bacd3d5dc09e830792d806cbf22d0ffb30598aa7f37961a2ca0260bb597c"

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
    """Closed-form expected ``u128`` for bar ``i`` (see module docstring)."""
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
        if band >= 1:
            # DEC-GAP-MIDPOINT-CONDITIONS-001: the gap-mid level is the flat
            # midpoint P_k − ₹500 (see module docstring); the close P_k is
            # strictly above it from the SECOND bar of the day onward (the
            # arm-from-bar-2 law keeps the day's first bar dark).
            mask |= 1 << B.BIT_CLOSE_ABOVE_GAP_MID
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
