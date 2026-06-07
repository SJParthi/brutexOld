"""Golden corpus #1 — NIFTY 50, one full trading day (reproducibility + tick-rounding).

DEC-PHASE2-CORPORA-001 (``tests/golden/fixtures/README.md`` corpus #1 —
"NIFTY 50 — 1 trading day / reproducibility round-trip + tick-rounding,
P-19, P-21").

Drives the REAL Stage-1A matrix builder (:func:`brutex.engine.stage1a.build_bar_bits`)
over a **correct-by-construction, fully hand-derivable** 375-bar single trading
day, then locks the result with a SHA-256 manifest. Two properties:

* **P-19 reproducibility round-trip** — the same feed yields a bit-identical
  ``u64`` matrix on every run (batch ``build`` twice + the streaming ``stream``
  path all agree), and that matrix's SHA-256 is pinned so any adapter regression
  drifts the manifest.
* **P-21 tick-rounding** — the Decimal-rupees → int64-paisa lake-boundary seam
  snaps every price to the 2-decimal tick grid with ``ROUND_HALF_UP``
  (``DEC-SPOT-PRICE-2DP-001`` / ``DEC-MONEY-2DP-001``), so vendor float-noise
  like ``14025.7999`` can never reach the engine.

Why the 375 masks are hand-derivable (no un-runnable hand-math)
--------------------------------------------------------------
Every bar is the SAME **flat doji** ``O==H==L==C`` at one clean price on the
FIRST (and only) IST day, so each per-bar ``u64`` is a closed form:

* **Group 7 day structure** — ``close == day_high == day_low`` on a flat day ⇒
  ``CLOSE_AT_DAY_HIGH`` (40) + ``CLOSE_AT_DAY_LOW`` (41); the day range is 0 so
  no upper/lower-third bit.
* **Group 1 EMA** — every close is identical, so once the EMA is defined it
  equals the close exactly ⇒ no strict above/below ⇒ **no EMA bit ever**.
* **Group 5 candle** — a zero-range bar (``total_range == 0``) sets none of the
  shape bits (the Rust core guards every shape bit on ``total_range > 0``).
* **Groups 2/3/4/9 anchors** — undefined on the first day of a stream, so the
  builder clears every anchor-dependent bit (:data:`ANCHOR_DEPENDENT_MASK`).
* **Group 6 prior-bar** — each flat doji records a *bearish* flag
  (``close > open`` is False); the all-bearish window needs ``prior_n == 3``
  filled, so ``PRIOR_N_BEARISH`` (38) fires from the 4th bar (index ≥ 3) on.
* **Group 8 time-of-day** — the one varying bit: the bar's IST minute selects
  EARLY_MORNING (44, ``[09:16, 10:00)``), MID_MORNING (45, ``[10:00, 11:30)``),
  MIDDAY (46, ``[11:30, 13:00)``) or AFTERNOON (47, ``[13:00, 15:00)``); the
  09:15 open bar and every bar from 15:00 on carry no time bit.

So bar ``i`` (09:15 IST + ``i`` minutes, ``i = 0 .. 374``) has mask
``{40, 41} ∪ time_bit(i) ∪ ({38} if i >= 3 else {})`` — computed by
:func:`_expected_mask` below and asserted bar-for-bar AND pinned by SHA-256.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.stage1a import (
    ANCHOR_DEPENDENT_MASK,
    Stage1AMatrixBuilder,
    _price_to_paisa,  # pyright: ignore[reportPrivateUsage]
    build_bar_bits,
)
from brutex.vocabulary import bits as B

#: One NIFTY trading day — Monday 2021-01-04 (a regular NSE session).
_DAY = datetime(2021, 1, 4, 9, 15, tzinfo=IST)
#: 375 one-minute bars = the full 09:15-15:29 IST regular session.
_N_BARS = 375
#: Clean flat-doji price (rupees) — well inside the early-2021 NIFTY range.
_PRICE = Decimal("14000.00")

#: SHA-256 of the frozen 375-mask matrix (the README "SHA-256 manifest" guard).
#: Regenerate by running this fixture's builder and re-pinning if the engine's
#: per-bar logic legitimately changes (which would also break the bar-for-bar
#: hand-derived assertion below).
_EXPECTED_SHA256 = "fdb26e264df22eb1f5d285772421fea6f09fb283cd63eba29ab15592d9b89b65"


def _bar(i: int) -> CandleBar:
    """The ``i``-th flat-doji NIFTY bar (09:15 IST + ``i`` minutes)."""
    ts = (_DAY + timedelta(minutes=i)).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=_PRICE,
        high=_PRICE,
        low=_PRICE,
        close=_PRICE,
        volume=0,
        open_interest=None,
    )


def _feed() -> list[CandleBar]:
    return [_bar(i) for i in range(_N_BARS)]


def _time_bit(i: int) -> int | None:
    """The Group-8 time-of-day bit for the bar at 09:15 IST + ``i`` minutes.

    Boundaries mirror the Rust core exactly: EARLY ``[09:16, 10:00)``,
    MID ``[10:00, 11:30)``, MIDDAY ``[11:30, 13:00)``, AFTERNOON
    ``[13:00, 15:00)``; everything else (the 09:15 open + 15:00 onward) is None.
    """
    seconds = (_DAY + timedelta(minutes=i)).hour * 3600 + (_DAY + timedelta(minutes=i)).minute * 60
    if 9 * 3600 + 16 * 60 <= seconds < 10 * 3600:
        return B.BIT_EARLY_MORNING
    if 10 * 3600 <= seconds < 11 * 3600 + 30 * 60:
        return B.BIT_MID_MORNING
    if 11 * 3600 + 30 * 60 <= seconds < 13 * 3600:
        return B.BIT_MIDDAY
    if 13 * 3600 <= seconds < 15 * 3600:
        return B.BIT_AFTERNOON
    return None


def _expected_mask(i: int) -> int:
    """Closed-form expected ``u64`` for flat-doji bar ``i`` (see module docstring)."""
    mask = (1 << B.BIT_CLOSE_AT_DAY_HIGH) | (1 << B.BIT_CLOSE_AT_DAY_LOW)
    time_bit = _time_bit(i)
    if time_bit is not None:
        mask |= 1 << time_bit
    if i >= 3:  # prior_n == 3 filled with all-bearish doji flags
        mask |= 1 << B.BIT_PRIOR_N_BEARISH
    return mask


def _manifest(masks: list[int]) -> str:
    return hashlib.sha256(",".join(str(m) for m in masks).encode("utf-8")).hexdigest()


def test_full_day_matrix_is_one_to_one_with_bars() -> None:
    """The builder emits exactly one mask per input bar (375)."""
    masks = build_bar_bits(_feed())
    assert len(masks) == _N_BARS


def test_full_day_matrix_matches_hand_derived_closed_form() -> None:
    """Every one of the 375 masks equals its hand-derived closed form."""
    masks = build_bar_bits(_feed())
    assert masks == [_expected_mask(i) for i in range(_N_BARS)]


def test_first_day_clears_every_anchor_dependent_bit() -> None:
    """On a single (first) day, pivots/PDH-PDL/Fib/day-type bits are all cleared."""
    masks = build_bar_bits(_feed())
    assert all((mask & ANCHOR_DEPENDENT_MASK) == 0 for mask in masks)


def test_reproducibility_round_trip_is_bit_identical() -> None:
    """P-19: batch (twice) and the streaming path all yield the identical matrix."""
    first = build_bar_bits(_feed())
    second = build_bar_bits(_feed())
    streamed = list(Stage1AMatrixBuilder().stream(_feed()))
    assert first == second == streamed


def test_matrix_sha256_matches_pinned_manifest() -> None:
    """The 375-mask matrix hashes to the pinned drift manifest."""
    assert _manifest(build_bar_bits(_feed())) == _EXPECTED_SHA256


def test_tick_rounding_snaps_to_two_decimal_grid() -> None:
    """P-21: the rupees→paisa seam snaps to 2 dp with ROUND_HALF_UP.

    Hand-derived: vendor float-noise ``14025.7999`` rounds UP to ``14025.80``
    (1402580 paisa); ``14025.794`` rounds down to ``14025.79`` (1402579); the
    exact half ``14025.795`` rounds half-up to ``14025.80`` (1402580). The clean
    flat-doji price is exact, so it converts loss-free.
    """
    assert _price_to_paisa(Decimal("14025.7999")) == 1402580
    assert _price_to_paisa(Decimal("14025.794")) == 1402579
    assert _price_to_paisa(Decimal("14025.795")) == 1402580
    assert _price_to_paisa(_PRICE) == 1400000
