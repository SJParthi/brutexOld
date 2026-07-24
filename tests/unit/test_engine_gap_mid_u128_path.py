"""Targeted ≥64 u128-path pins for the gap-midpoint bits 66-68.

DEC-GAP-MIDPOINT-CONDITIONS-001 (the coordinator addendum): bits 66-68 are the
FIRST IMPLEMENTED condition bits at or above bit 64, so a gap-bit truth-vector
or strategy mask genuinely occupies the u128 mask's SECOND u64 limb. These pins
prove — bit-specifically, through the existing u128 kernel/test machinery, NOT
as a grid-wide regime gate — that the three gap-mid bits evaluate correctly on
that path:

* the Stage-1A truth vector carries the gap bits in the high limb and the
  ``evaluate_mask`` superset test resolves ≥64 required bits correctly
  (set AND clear directions, alone and mixed with a low-limb bit);
* ``count_supermasks`` ROUTES a ≥64 mask/bar to the bit-exact two-limb
  reference (the documented ``U64_MASK`` branch — the fast u64 Rust kernel
  never sees a truncated high bit) and the counts equal both the reference
  and a hand-derived expectation;
* ``iter_masks_k_of_bits`` composes gap-bit masks past bit 64 losslessly.

The u128 REGIME machinery itself (the sweep_apriori ≥64-guard resolution + the
M4-Pro u128 throughput measurement) is owned by the vocab-66 track's PR-B and
is deliberately NOT claimed or gated here.
"""

from __future__ import annotations

from datetime import datetime

from brutex.engine.combo_mask import (
    U64_MASK,
    count_supermasks,
    count_supermasks_reference,
    evaluate_mask,
    iter_masks_k_of_bits,
)
from brutex.engine.stage1a import build_bar_bits
from brutex.vocabulary import bits as B

from .test_engine_stage1a import _gap_up_two_day_feed, _minute_bar

_ABOVE = 1 << B.BIT_CLOSE_ABOVE_GAP_MID
_BELOW = 1 << B.BIT_CLOSE_BELOW_GAP_MID
_NEAR = 1 << B.BIT_NEAR_GAP_MID
_GAP_FAMILY = _ABOVE | _BELOW | _NEAR


def _armed_masks() -> list[int]:
    """The hand-derived two-day gap-up feed's Stage-1A truth vectors."""
    return list(build_bar_bits(_gap_up_two_day_feed()))


def test_gap_bits_occupy_the_second_u64_limb() -> None:
    """The premise made explicit: bits 66-68 live PAST u64 — a fired gap bit
    makes the truth vector exceed ``U64_MASK`` (the first implemented bits
    ≥ 64 in the vocabulary; SuperTrend 64/65 are layout-only and never fire).
    """
    assert min(B.BIT_CLOSE_ABOVE_GAP_MID, B.BIT_CLOSE_BELOW_GAP_MID, B.BIT_NEAR_GAP_MID) >= 64
    masks = _armed_masks()
    fired = [m for m in masks if m & _GAP_FAMILY]
    assert fired, "the fixture must fire the gap family"
    assert all(m > U64_MASK for m in fired)
    # And the high limb is EXACTLY the gap family — no stray ≥64 bit.
    assert all((m >> 64 << 64) & ~_GAP_FAMILY == 0 for m in masks)


def test_evaluate_mask_resolves_gap_bits_across_the_limb_boundary() -> None:
    """``evaluate_mask`` (the u128 AND superset law) on ≥64 required bits:
    set AND clear directions, alone and mixed with a low-limb bit."""
    masks = _armed_masks()
    above_bar = masks[3]  # close strictly above the level (bit 66 set)
    below_bar = masks[4]  # close strictly below the level (bit 67 set)
    at_bar = masks[5]  # exactly at the level (bit 68 set, 66/67 clear)
    # Set direction — the high-limb bit alone.
    assert evaluate_mask(above_bar, _ABOVE)
    assert evaluate_mask(below_bar, _BELOW)
    assert evaluate_mask(at_bar, _NEAR)
    # Clear direction — requiring the OTHER high-limb bit must not fire.
    assert not evaluate_mask(above_bar, _BELOW)
    assert not evaluate_mask(below_bar, _ABOVE)
    assert not evaluate_mask(at_bar, _ABOVE | _BELOW)
    # Mixed low+high limb: gap-up day (bit 48, low limb) AND above-gap-mid
    # (bit 66, high limb) — the AND must span both limbs.
    mixed = (1 << B.BIT_GAP_UP_DAY) | _ABOVE
    assert above_bar & (1 << B.BIT_GAP_UP_DAY), "day 2 is a gap-up day"
    assert evaluate_mask(above_bar, mixed)
    assert not evaluate_mask(at_bar, mixed)  # bit 66 clear at the level


def test_count_supermasks_routes_gap_masks_to_the_two_limb_reference() -> None:
    """A ≥64 gap-bit mask/bar takes the documented two-limb reference branch
    (the u64-native Rust kernel would truncate it) and counts EXACTLY."""
    masks = _armed_masks()
    queries = [_ABOVE, _BELOW, _NEAR, (1 << B.BIT_GAP_UP_DAY) | _ABOVE]
    got = count_supermasks(masks, queries)
    # Bit-exact vs the two-limb reference…
    assert got == count_supermasks_reference(masks, queries)
    # …and vs the hand-derivation: exactly one bar each of above/below/near,
    # and the mixed low+high query matches only the above bar.
    assert got == [1, 1, 1, 1]
    # The pure-low-limb control stays on the fast path's answer either way.
    low_only = [1 << B.BIT_GAP_UP_DAY]
    assert count_supermasks(masks, low_only) == count_supermasks_reference(masks, low_only)


def test_count_supermasks_reference_handles_a_gap_bar_with_low_masks() -> None:
    """The routing is bar-triggered too: a BAR carrying bit 66 (> U64_MASK)
    routes even when every QUERY mask is low-limb — no high bit is dropped."""
    masks = _armed_masks()
    assert any(b > U64_MASK for b in masks)
    queries = [1 << B.BIT_GAP_UP_DAY, 1 << B.BIT_CLOSE_AT_DAY_HIGH]
    assert count_supermasks(masks, queries) == count_supermasks_reference(masks, queries)


def test_iter_masks_k_of_bits_composes_gap_bit_masks_losslessly() -> None:
    """Mask composition over the gap trio survives the limb boundary."""
    bits = [B.BIT_GAP_UP_DAY, B.BIT_CLOSE_ABOVE_GAP_MID, B.BIT_NEAR_GAP_MID]
    pairs = sorted(iter_masks_k_of_bits(bits, 2))
    assert pairs == sorted(
        [
            (1 << B.BIT_GAP_UP_DAY) | _ABOVE,
            (1 << B.BIT_GAP_UP_DAY) | _NEAR,
            _ABOVE | _NEAR,
        ]
    )
    assert all(m > U64_MASK for m in pairs)


def test_zero_gap_day_high_limb_stays_dark_through_the_u128_path() -> None:
    """The clear direction end-to-end: a ZERO-gap day (open == pdc) mints no
    level, so the high limb carries NO gap bit and a gap-gated ≥64 mask never
    fires through ``evaluate_mask`` on any bar."""
    day1 = datetime(2024, 1, 2)
    day2 = datetime(2024, 1, 3)
    feed = [
        _minute_bar(day1, 0, "100", "101", "99", "100"),
        _minute_bar(day1, 1, "100", "102", "98", "100"),
        # Day 2 opens EXACTLY at pdc 100.00 ⇒ zero gap ⇒ dark all day.
        _minute_bar(day2, 0, "100", "103", "99", "101"),
        _minute_bar(day2, 1, "101", "104", "100", "102"),
    ]
    masks = list(build_bar_bits(feed))
    assert all((m >> 64) == 0 for m in masks)
    assert not any(evaluate_mask(m, _ABOVE) for m in masks)
    assert not any(evaluate_mask(m, _NEAR) for m in masks)
