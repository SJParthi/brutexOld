"""Golden byte-equality oracle for the Stage-1A matrix builder.

A small, fully hand-derived two-day fixture whose every set bit is verified by
hand below, locked as literal ``u64`` masks. Any change to the adapter (paisa
rounding, UTC→IST, daily-anchor broadcast, the tie-break) that alters output
breaks this test (DEC-ENGINE-DRIVER-001 / NFR-DET-001 bit-exactness).

Fixture (1-minute bars, prices in rupees):

* **Day 1** — one bar at 09:15 IST (03:45 UTC), ``O=H=L=C=200`` (a flat doji).
  First IST day → all anchor groups (pivots / PDH-PDL / Fib / day-type) are
  cleared. Establishes the prior-day OHLC ``PDH=PDL=PDC=200``.
* **Day 2** — one bar at 09:30 IST (04:00 UTC), ``O=210 H=215 L=205 C=212``.

Hand-derived expected bits
--------------------------
``mask[0]`` (day-1 doji, first day): range == 0 so no candle-shape / day-third
bits; ``close == day_high == day_low`` → ``CLOSE_AT_DAY_HIGH`` (40) +
``CLOSE_AT_DAY_LOW`` (41); EMA warming up; anchors cleared; 09:15 IST is before
the 09:16 early-morning cutoff → no time bit. ⇒ bits {40, 41}.

``mask[1]`` (day-2 bar, prior ``PDH=PDL=PDC=200``):

* ``close 212 > PDH 200`` and ``> PDL 200`` → ``CLOSE_ABOVE_PDH`` (13) +
  ``CLOSE_ABOVE_PDL`` (15); zero-range prior day ⇒ no ``near_*`` pivot/PDH/Fib.
* ``close 212 > open 210`` → ``BAR_BULLISH`` (30); body 2 vs range 10 →
  ``body·10 = 20 < range·3 = 30`` → ``BAR_SMALL_BODY`` (34); wicks are not
  long (upper 3·2 = 6 ≤ 10, lower 5·2 = 10 ≤ 10).
* day extremes = this bar; ``close 212`` sits in the upper third of
  ``[205, 215]`` (``(212-205)·3 = 21 > range·2 = 20``) → ``CLOSE_IN_UPPER_THIRD``
  (42).
* 09:30 IST ∈ [09:16, 10:00) → ``EARLY_MORNING`` (44).
* ``open 210 > PDC 200 + 0.5%`` (gap threshold ``200·50/10000 = 1`` rupee) →
  ``GAP_UP_DAY`` (48); not inside/outside (``day_low 205 > PDL 200``).
* the flat prior day collapses the CPR onto one price:
  ``P = (200+200+200)/3 = 200``, ``BC = (200+200)/2 = 200``,
  ``TC = 2·200 − 200 = 200`` (width 0) → ``close 212 > TC 200`` ⇒
  ``ABOVE_CPR_TC`` (60), and ``0 ≤ P·30 bps`` trivially ⇒
  ``NARROW_CPR_DAY`` (63) — the 60→66 reshape.
* EMA still warming up (2 bars). ⇒ bits {13, 15, 30, 34, 42, 44, 48, 60, 63}.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.stage1a import build_bar_bits
from brutex.vocabulary import bits as B


def _bar(ts: datetime, open_: str, high: str, low: str, close: str) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=0,
        open_interest=None,
    )


def _mask_of(*bit_positions: int) -> int:
    """OR the given bit positions into a u64 mask."""
    mask = 0
    for bit in bit_positions:
        mask |= 1 << bit
    return mask


#: Day 1 — 09:15 IST flat doji (03:45 UTC); first IST day (anchors cleared).
_DAY1 = _bar(datetime(2024, 1, 2, 3, 45, tzinfo=UTC), "200", "200", "200", "200")
#: Day 2 — 09:30 IST bullish small-body bar (04:00 UTC).
_DAY2 = _bar(datetime(2024, 1, 3, 4, 0, tzinfo=UTC), "210", "215", "205", "212")

#: The hand-derived expected matrix (literal u64 masks — the oracle).
_EXPECTED_MASK_0 = _mask_of(B.BIT_CLOSE_AT_DAY_HIGH, B.BIT_CLOSE_AT_DAY_LOW)
_EXPECTED_MASK_1 = _mask_of(
    B.BIT_CLOSE_ABOVE_PDH,
    B.BIT_CLOSE_ABOVE_PDL,
    B.BIT_BAR_BULLISH,
    B.BIT_BAR_SMALL_BODY,
    B.BIT_CLOSE_IN_UPPER_THIRD,
    B.BIT_EARLY_MORNING,
    B.BIT_GAP_UP_DAY,
    B.BIT_ABOVE_CPR_TC,
    B.BIT_NARROW_CPR_DAY,
)


def test_stage1a_golden_matrix() -> None:
    masks = build_bar_bits([_DAY1, _DAY2])
    assert masks == [_EXPECTED_MASK_0, _EXPECTED_MASK_1]


def test_stage1a_golden_literal_values() -> None:
    # Pin the exact integers so a renumbering of bits OR an adapter regression
    # is caught even if the bit-name constants drift (defence in depth).
    # (Re-derived for the 60→66 reshape: day 2 gains bits 60 + 63.)
    assert build_bar_bits([_DAY1, _DAY2]) == [3298534883328, 10376597024924540928]
