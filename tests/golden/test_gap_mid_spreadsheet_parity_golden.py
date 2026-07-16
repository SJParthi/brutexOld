"""TF=3minute SPREADSHEET PARITY GOLDEN — the Group-13 gap-midpoint level.

DEC-GAP-MIDPOINT-CONDITIONS-001 §3.7's BINDING golden: on TF=3minute the
session-edge gap-midpoint must reproduce the ES Trading Academy spreadsheet's
``(X1 + X2) / 2`` EXACTLY, where

* ``X1`` = the prior session's LAST 3-minute candle extreme — the **15:27
  bucket** (09:15-anchored grid, 375 session minutes / 3 = 125 buckets, so the
  last bucket label is 09:15 + 372min = 15:27, a FULL bucket);
* ``X2`` = today's FIRST 3-minute candle extreme — the **09:15 bucket**;
* gap-UP day (``open_of_day > pdc``, exact sign): the two HIGHs;
  gap-DOWN day: the two LOWs; the midpoint is ``round_half_even``.

Six deterministic hand-derived cases (NIFTY / BANKNIFTY / SENSEX-shaped
prices × gap-up AND gap-down — mirroring the coordinator's 6/6 upstream
verification) PLUS the design-mandated marginal/ambiguous-direction day
(§3.1: open marginally above pdc while ``X2_hi < X1_hi`` — the sign law picks
the hi-pair; a candle-extremes classification would mint a DIFFERENT level,
so clean days alone cannot detect a direction-law divergence).

Every expected level below is HAND ARITHMETIC written as a literal — the
level SERVED TO THE EVALUATOR (``Stage1AMatrixBuilder._gap_mid_paisa``) must
equal it exactly, and an appended probe bar whose close sits EXACTLY at the
level must read (above=False, below=False, near=True) through the full
``build_bar_bits`` evaluation chain (the same level, end to end).

X1-identity teeth: each day-1 fixture plants its day's TRUE extreme on a
MID-DAY bucket, so a wrong anchor (PDH/PDL instead of the 15:27 bucket's
extreme) provably mints a different level — asserted per case.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.stage1a import (
    GAP_MID_DEPENDENT_MASK,
    Stage1AMatrixBuilder,
    build_bar_bits,
)
from brutex.vocabulary import bits as B

# 09:15:00 IST == 03:45 UTC — the NSE session-open anchor.
_OPEN_UTC_H, _OPEN_UTC_M = 3, 45

#: 375 session minutes / 3-minute buckets = 125 buckets (09:15 .. 15:27).
_BUCKETS_3M = 125

_OverrideMap = dict[int, tuple[str, str, str, str]]


def _tf3_bar(day: datetime, bucket: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    """One 3-minute TF bar at ``09:15 IST + 3*bucket`` minutes on ``day``."""
    ts = day.replace(hour=_OPEN_UTC_H, minute=_OPEN_UTC_M, tzinfo=UTC) + timedelta(
        minutes=3 * bucket
    )
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=10,
        open_interest=None,
    )


def _session_3m(day: datetime, base: str, overrides: _OverrideMap) -> list[CandleBar]:
    """A FULL 125-bucket 3-minute session: flat ``base``-priced bars with
    per-bucket ``(o, h, l, c)`` overrides on the fixture's special buckets."""
    base_d = Decimal(base)
    flat = (base, str(base_d + 2), str(base_d - 2), base)
    bars: list[CandleBar] = []
    for k in range(_BUCKETS_3M):
        o, h, lo, c = overrides.get(k, flat)
        bars.append(_tf3_bar(day, k, o, h, lo, c))
    return bars


def _gap_bits(mask: int) -> tuple[bool, bool, bool]:
    """(above, below, near) — the Group-13 bit triple of *mask*."""
    return (
        bool(mask >> B.BIT_CLOSE_ABOVE_GAP_MID & 1),
        bool(mask >> B.BIT_CLOSE_BELOW_GAP_MID & 1),
        bool(mask >> B.BIT_NEAR_GAP_MID & 1),
    )


_DAY1 = datetime(2024, 1, 2)
_DAY2 = datetime(2024, 1, 3)


def _run_case(
    *,
    base1: str,
    day1_overrides: _OverrideMap,
    day2_bars: list[tuple[str, str, str, str]],
    expected_level_paisa: int,
    x1_paisa: int,
    x2_paisa: int,
    day_extreme_paisa: int,
) -> list[int]:
    """Drive one parity case end to end; return the full mask series.

    Asserts, in order: the fixture shape (125 buckets, last labelled 15:27
    IST), the HAND-ARITHMETIC identity ``expected == (x1 + x2) / 2`` under
    round-half-even, the served level (``_gap_mid_paisa``), the wrong-anchor
    discriminator, day-1 + day-2-bar-1 darkness, and the exactly-at-level
    probe bar (bucket 1 of day 2) reading near-only.
    """
    day1 = _session_3m(_DAY1, base1, day1_overrides)
    assert len(day1) == _BUCKETS_3M
    last_ist = day1[-1].timestamp.astimezone(IST)
    assert (last_ist.hour, last_ist.minute) == (15, 27)  # the spreadsheet X1 bucket

    # The hand arithmetic itself (round-half-even of the pair sum): the
    # literal expected level IS (X1 + X2) / 2 — pinned before any engine code.
    total = x1_paisa + x2_paisa
    half, rem = divmod(total, 2)
    hand = half if rem == 0 or half % 2 == 0 else half + 1
    assert expected_level_paisa == hand

    bars = day1 + [_tf3_bar(_DAY2, k, *spec) for k, spec in enumerate(day2_bars)]

    # The level SERVED TO THE EVALUATOR: push through day-2's first bar and
    # read the minted member directly.
    builder = Stage1AMatrixBuilder()
    for bar in day1:
        builder.push(bar)
    builder.push(bars[_BUCKETS_3M])
    assert builder._gap_mid_paisa == expected_level_paisa

    # Wrong-anchor teeth: a level minted from the DAY extreme (PDH/PDL) in
    # place of the 15:27 bucket's extreme would differ.
    assert (day_extreme_paisa + x2_paisa) // 2 != expected_level_paisa

    masks = build_bar_bits(bars)
    # Day 1 (no X1 on the stream's first day) + day-2 bar 1 (arm-from-bar-2):
    # dark.
    assert all(m & GAP_MID_DEPENDENT_MASK == 0 for m in masks[: _BUCKETS_3M + 1])
    # The probe bar closing EXACTLY at the hand-derived level: near fires,
    # the strict above/below do not — the served level equals the hand
    # arithmetic through the whole evaluation chain.
    assert _gap_bits(masks[_BUCKETS_3M + 1]) == (False, False, True)
    return masks


def test_nifty_gap_up_parity() -> None:
    """NIFTY-shaped gap-up: X1_hi 22050.35 + X2_hi 22180.45 ⇒ ₹22115.40."""
    masks = _run_case(
        base1="22000",
        day1_overrides={
            # Mid-day day-HIGH 22100.00 — the PDH discriminator (≠ X1_hi).
            60: ("22000", "22100", "21995", "22005"),
            # The 15:27 bucket: X1_hi = 22050.35; day close (pdc) = 22010.00.
            124: ("22000", "22050.35", "21990.05", "22010"),
        },
        day2_bars=[
            # 09:15 bucket: open 22150.10 > pdc 22010.00 ⇒ gap-UP; X2_hi 22180.45.
            ("22150.10", "22180.45", "22140", "22160"),
            ("22120", "22130", "22110", "22115.40"),  # probe: exactly at level
            ("22200", "22310", "22190", "22300"),  # far above
            ("22000", "22010", "21940", "21950"),  # far below
        ],
        # (2205035 + 2218045) / 2 = 4423080 / 2 = 2211540 — even, exact.
        expected_level_paisa=2_211_540,
        x1_paisa=2_205_035,
        x2_paisa=2_218_045,
        day_extreme_paisa=2_210_000,
    )
    assert _gap_bits(masks[127]) == (True, False, False)  # 22300.00 above
    assert _gap_bits(masks[128]) == (False, True, False)  # 21950.00 below


def test_nifty_gap_down_parity() -> None:
    """NIFTY-shaped gap-down: X1_lo 21985.15 + X2_lo 21860.25 ⇒ ₹21922.70."""
    masks = _run_case(
        base1="22000",
        day1_overrides={
            # Mid-day day-LOW 21900.00 — the PDL discriminator (≠ X1_lo).
            40: ("22000", "22005", "21900", "21995"),
            # The 15:27 bucket: X1_lo = 21985.15; pdc = 22000.00.
            124: ("22000", "22006", "21985.15", "22000"),
        },
        day2_bars=[
            # 09:15 bucket: open 21890.00 < pdc 22000.00 ⇒ gap-DOWN; X2_lo 21860.25.
            ("21890", "21895", "21860.25", "21880"),
            ("21920", "21930", "21910", "21922.70"),  # probe: exactly at level
            ("21985", "21995", "21980", "21990"),  # above
            ("21860", "21865", "21845", "21850"),  # below
        ],
        # (2198515 + 2186025) / 2 = 4384540 / 2 = 2192270 — even, exact.
        expected_level_paisa=2_192_270,
        x1_paisa=2_198_515,
        x2_paisa=2_186_025,
        day_extreme_paisa=2_190_000,
    )
    assert _gap_bits(masks[127]) == (True, False, False)  # 21990.00 above
    assert _gap_bits(masks[128]) == (False, True, False)  # 21850.00 below


def test_banknifty_gap_up_parity_half_even_tie_to_even_floor() -> None:
    """BANKNIFTY-shaped gap-up with an ODD pair sum: the .5 midpoint ties
    HALF-EVEN to the (even) floor — (4801015 + 4812030) / 2 = 4806522.5 ⇒
    4806522 (₹48065.22)."""
    _run_case(
        base1="48000",
        day1_overrides={
            70: ("48000", "48200", "47995", "48005"),  # day-HIGH discriminator
            124: ("48000", "48010.15", "47985", "48000"),  # X1_hi 48010.15; pdc 48000
        },
        day2_bars=[
            ("48100", "48120.30", "48080", "48110"),  # open > pdc ⇒ UP; X2_hi 48120.30
            ("48060", "48070", "48055", "48065.22"),  # probe: exactly at level
        ],
        expected_level_paisa=4_806_522,
        x1_paisa=4_801_015,
        x2_paisa=4_812_030,
        day_extreme_paisa=4_820_000,
    )


def test_banknifty_gap_down_parity() -> None:
    """BANKNIFTY-shaped gap-down: X1_lo 47990.55 + X2_lo 47880.45 ⇒ ₹47935.50."""
    _run_case(
        base1="48000",
        day1_overrides={
            30: ("48000", "48005", "47800", "47995"),  # day-LOW discriminator
            124: ("48000", "48004", "47990.55", "48000"),  # X1_lo 47990.55; pdc 48000
        },
        day2_bars=[
            ("47900", "47905", "47880.45", "47890"),  # open < pdc ⇒ DOWN; X2_lo 47880.45
            ("47930", "47940", "47925", "47935.50"),  # probe: exactly at level
        ],
        # (4799055 + 4788045) / 2 = 9587100 / 2 = 4793550 — even, exact.
        expected_level_paisa=4_793_550,
        x1_paisa=4_799_055,
        x2_paisa=4_788_045,
        day_extreme_paisa=4_780_000,
    )


def test_sensex_gap_up_parity() -> None:
    """SENSEX-shaped gap-up: X1_hi 72080.20 + X2_hi 72260.40 ⇒ ₹72170.30."""
    _run_case(
        base1="72000",
        day1_overrides={
            80: ("72000", "72300", "71995", "72010"),  # day-HIGH discriminator
            124: ("72000", "72080.20", "71960", "72020"),  # X1_hi 72080.20; pdc 72020
        },
        day2_bars=[
            ("72200", "72260.40", "72150", "72210"),  # open > pdc ⇒ UP; X2_hi 72260.40
            ("72165", "72175", "72160", "72170.30"),  # probe: exactly at level
        ],
        # (7208020 + 7226040) / 2 = 14434060 / 2 = 7217030 — even, exact.
        expected_level_paisa=7_217_030,
        x1_paisa=7_208_020,
        x2_paisa=7_226_040,
        day_extreme_paisa=7_230_000,
    )


def test_sensex_gap_down_parity_half_even_tie_rounds_up_to_even() -> None:
    """SENSEX-shaped gap-down with the OTHER tie arm: (7200003 + 7190000) / 2
    = 7195001.5, floor 7195001 is ODD ⇒ half-even rounds UP to 7195002
    (₹71950.02) — together with the BANKNIFTY-up case both tie directions of
    the house rounding law are spreadsheet-pinned."""
    _run_case(
        base1="72050",
        day1_overrides={
            50: ("72050", "72055", "71880", "72045"),  # day-LOW discriminator
            124: ("72050", "72054", "72000.03", "72040"),  # X1_lo 72000.03; pdc 72040
        },
        day2_bars=[
            ("71950", "71955", "71900", "71940"),  # open < pdc ⇒ DOWN; X2_lo 71900.00
            ("71945", "71952", "71940", "71950.02"),  # probe: exactly at level
        ],
        expected_level_paisa=7_195_002,
        x1_paisa=7_200_003,
        x2_paisa=7_190_000,
        day_extreme_paisa=7_188_000,
    )


def test_marginal_ambiguous_direction_day_uses_the_sign_law_hi_pair() -> None:
    """The §3.1-mandated MARGINAL day: open = pdc + 1 paisa (gap-UP by the
    exact-sign law) while ``X2_hi < X1_hi`` — a candle-extremes-based
    classification would pick the LOW pair and mint ₹21967.50; the design's
    sign law picks the HIGH pair: (2209000 + 2201000) / 2 = ₹22050.00.
    Pinned so a silent direction-law change cannot hide behind clean days.
    """
    day1 = _session_3m(
        _DAY1,
        "22000",
        {124: ("22000", "22090", "21985", "22000")},  # X1_hi 22090.00; pdc 22000.00
    )
    day2 = [
        # open 22000.01 > pdc 22000.00 by ONE paisa ⇒ gap-UP (sign law);
        # X2_hi 22010.00 < X1_hi 22090.00 (the ambiguous shape).
        _tf3_bar(_DAY2, 0, "22000.01", "22010", "21950", "21990"),
        _tf3_bar(_DAY2, 1, "22040", "22055", "22035", "22050"),  # exactly at level
    ]
    builder = Stage1AMatrixBuilder()
    for bar in day1 + day2[:1]:
        builder.push(bar)
    level = builder._gap_mid_paisa
    assert level == 2_205_000  # the HI-pair midpoint — ₹22050.00
    # The rejected candle-extremes alternative (the LO pair) provably differs.
    assert level != (2_198_500 + 2_195_000) // 2  # ₹21967.50
    masks = build_bar_bits(day1 + day2)
    assert _gap_bits(masks[-1]) == (False, False, True)  # close exactly at level
