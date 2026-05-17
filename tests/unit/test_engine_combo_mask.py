"""Unit tests for ``brutex.engine.combo_mask``.

Covers u64 bitmask iteration + evaluation primitives. Per Golden Rule
#12 the new ``brutex/engine/`` module ships at 100% line + 100% branch
coverage from day one.

Tests are organized as:
1. Bit primitives — popcount, bits_to_mask, mask_to_bits
2. Strategy-mask evaluation (the hot path)
3. Combinatorial enumeration — count_combos, iter_masks_k_of_n
4. Validation edge cases (raise on bad inputs)
5. Cross-invariant property tests (sanity)
"""

from __future__ import annotations

from math import comb

import pytest
from brutex.engine import combo_mask
from brutex.engine.combo_mask import (
    MAX_VOCAB_BITS,
    VOCAB_BITS,
    bits_to_mask,
    count_combos,
    evaluate_mask,
    iter_masks_k_of_n,
    mask_to_bits,
    popcount,
)

# ─────────────────────────────────────────────────────────────────
# Module-level constants
# ─────────────────────────────────────────────────────────────────


def test_vocab_bits_is_52() -> None:
    """The current 52-condition vocabulary size."""
    assert VOCAB_BITS == 52


def test_max_vocab_bits_is_64() -> None:
    """u64 ceiling for the bitmask vocabulary."""
    assert MAX_VOCAB_BITS == 64


def test_module_alias_exists() -> None:
    """The ``combo_mask`` submodule is re-exported from package."""
    assert combo_mask.VOCAB_BITS == VOCAB_BITS


# ─────────────────────────────────────────────────────────────────
# popcount
# ─────────────────────────────────────────────────────────────────


def test_popcount_zero() -> None:
    assert popcount(0) == 0


def test_popcount_one_bit() -> None:
    assert popcount(1) == 1
    assert popcount(2) == 1
    assert popcount(1 << 63) == 1


def test_popcount_multiple_bits() -> None:
    assert popcount(0b101101) == 4
    assert popcount(0xFFFF_FFFF_FFFF_FFFF) == 64


def test_popcount_rejects_negative() -> None:
    with pytest.raises(ValueError, match="popcount: mask must be non-negative"):
        popcount(-1)


# ─────────────────────────────────────────────────────────────────
# bits_to_mask
# ─────────────────────────────────────────────────────────────────


def test_bits_to_mask_empty() -> None:
    assert bits_to_mask() == 0


def test_bits_to_mask_single() -> None:
    assert bits_to_mask(0) == 1
    assert bits_to_mask(1) == 2
    assert bits_to_mask(7) == 128


def test_bits_to_mask_multiple() -> None:
    assert bits_to_mask(0, 2) == 0b101
    assert bits_to_mask(0, 2, 5) == 0b100101


def test_bits_to_mask_dedupes() -> None:
    """Duplicate positions are idempotent (set semantics)."""
    assert bits_to_mask(0, 0, 2) == bits_to_mask(0, 2) == 0b101


def test_bits_to_mask_at_max_bit() -> None:
    """Position MAX_VOCAB_BITS - 1 is valid."""
    assert bits_to_mask(MAX_VOCAB_BITS - 1) == 1 << (MAX_VOCAB_BITS - 1)


def test_bits_to_mask_rejects_negative_position() -> None:
    with pytest.raises(ValueError, match="position -1 out of range"):
        bits_to_mask(-1)


def test_bits_to_mask_rejects_at_max() -> None:
    """Position MAX_VOCAB_BITS (= 64) is out of range."""
    with pytest.raises(ValueError, match=f"position {MAX_VOCAB_BITS} out of range"):
        bits_to_mask(MAX_VOCAB_BITS)


def test_bits_to_mask_rejects_above_max() -> None:
    with pytest.raises(ValueError, match="position 100 out of range"):
        bits_to_mask(100)


# ─────────────────────────────────────────────────────────────────
# mask_to_bits
# ─────────────────────────────────────────────────────────────────


def test_mask_to_bits_empty() -> None:
    assert mask_to_bits(0) == ()


def test_mask_to_bits_single() -> None:
    assert mask_to_bits(1) == (0,)
    assert mask_to_bits(2) == (1,)
    assert mask_to_bits(4) == (2,)


def test_mask_to_bits_multiple_ascending() -> None:
    """Bits are returned in ascending order, no matter the mask layout."""
    assert mask_to_bits(0b101) == (0, 2)
    assert mask_to_bits(0b101101) == (0, 2, 3, 5)


def test_mask_to_bits_high_bit() -> None:
    """High bit positions work correctly."""
    assert mask_to_bits(1 << 63) == (63,)


def test_mask_to_bits_rejects_negative() -> None:
    with pytest.raises(ValueError, match="mask_to_bits: mask must be non-negative"):
        mask_to_bits(-1)


# ─────────────────────────────────────────────────────────────────
# evaluate_mask (the hot path)
# ─────────────────────────────────────────────────────────────────


def test_evaluate_mask_empty_always_matches() -> None:
    """Empty mask (requires nothing) always matches any bar."""
    assert evaluate_mask(0, 0) is True
    assert evaluate_mask(0xFFFF_FFFF_FFFF_FFFF, 0) is True


def test_evaluate_mask_all_bits_match() -> None:
    """Bar has all required bits set → match."""
    assert evaluate_mask(0b1111, 0b0011) is True
    assert evaluate_mask(0b1111, 0b1111) is True


def test_evaluate_mask_missing_required_bit() -> None:
    """Bar missing at least one required bit → no match."""
    assert evaluate_mask(0b1010, 0b0011) is False
    assert evaluate_mask(0, 0b0001) is False


def test_evaluate_mask_extra_bits_ignored() -> None:
    """Bar can have extra bits set beyond what's required."""
    assert evaluate_mask(0b1111_1111, 0b0001_0001) is True


# ─────────────────────────────────────────────────────────────────
# count_combos
# ─────────────────────────────────────────────────────────────────


def test_count_combos_zero() -> None:
    """C(n, 0) = 1 (empty combo)."""
    assert count_combos(0) == 1
    assert count_combos(0, n=5) == 1


def test_count_combos_one() -> None:
    """C(n, 1) = n."""
    assert count_combos(1) == 52
    assert count_combos(1, n=5) == 5


def test_count_combos_canonical_values() -> None:
    """Verify against known C(52, k) values."""
    assert count_combos(2) == 1326
    assert count_combos(3) == 22100
    assert count_combos(4) == 270725
    assert count_combos(5) == 2598960


def test_count_combos_full_set() -> None:
    """C(n, n) = 1 (the all-bits-set combo)."""
    assert count_combos(52) == 1
    assert count_combos(5, n=5) == 1


def test_count_combos_rejects_negative_k() -> None:
    with pytest.raises(ValueError, match="invalid k=-1"):
        count_combos(-1)


def test_count_combos_rejects_negative_n() -> None:
    with pytest.raises(ValueError, match=r"invalid k=0, n=-1"):
        count_combos(0, n=-1)


def test_count_combos_rejects_k_greater_than_n() -> None:
    with pytest.raises(ValueError, match=r"invalid k=10, n=5"):
        count_combos(10, n=5)


def test_count_combos_rejects_n_above_max() -> None:
    with pytest.raises(ValueError, match=f"n=65 exceeds MAX_VOCAB_BITS={MAX_VOCAB_BITS}"):
        count_combos(1, n=65)


# ─────────────────────────────────────────────────────────────────
# iter_masks_k_of_n
# ─────────────────────────────────────────────────────────────────


def test_iter_masks_k_zero() -> None:
    """K=0 yields the empty mask (one entry)."""
    masks = list(iter_masks_k_of_n(0, n=5))
    assert masks == [0]


def test_iter_masks_k_one() -> None:
    """K=1 yields all single-bit masks in ascending position order."""
    masks = list(iter_masks_k_of_n(1, n=5))
    assert masks == [1, 2, 4, 8, 16]


def test_iter_masks_k_two_small_n() -> None:
    masks = list(iter_masks_k_of_n(2, n=4))
    assert masks == [0b0011, 0b0101, 0b1001, 0b0110, 0b1010, 0b1100]


def test_iter_masks_k_three_full() -> None:
    """C(3, 3) = 1, the all-bits-set mask."""
    masks = list(iter_masks_k_of_n(3, n=3))
    assert masks == [0b111]


def test_iter_masks_count_matches_count_combos() -> None:
    """The number of masks yielded matches count_combos exactly."""
    for k in range(5):
        for n in range(k, 8):
            masks = list(iter_masks_k_of_n(k, n=n))
            assert len(masks) == count_combos(k, n=n)


def test_iter_masks_every_mask_has_exactly_k_bits() -> None:
    """Every yielded mask satisfies popcount == k."""
    for k in range(1, 5):
        for mask in iter_masks_k_of_n(k, n=8):
            assert popcount(mask) == k


def test_iter_masks_all_yielded_masks_unique() -> None:
    """No duplicates."""
    masks = list(iter_masks_k_of_n(3, n=8))
    assert len(masks) == len(set(masks))


def test_iter_masks_rejects_negative_k() -> None:
    with pytest.raises(ValueError, match="invalid k=-1"):
        list(iter_masks_k_of_n(-1))


def test_iter_masks_rejects_negative_n() -> None:
    with pytest.raises(ValueError, match=r"invalid k=0, n=-1"):
        list(iter_masks_k_of_n(0, n=-1))


def test_iter_masks_rejects_k_greater_than_n() -> None:
    with pytest.raises(ValueError, match=r"invalid k=10, n=5"):
        list(iter_masks_k_of_n(10, n=5))


def test_iter_masks_rejects_n_above_max() -> None:
    with pytest.raises(ValueError, match=f"n=65 exceeds MAX_VOCAB_BITS={MAX_VOCAB_BITS}"):
        list(iter_masks_k_of_n(1, n=65))


# ─────────────────────────────────────────────────────────────────
# Cross-primitive invariants
# ─────────────────────────────────────────────────────────────────


def test_mask_to_bits_round_trip() -> None:
    """For any mask, bits_to_mask(mask_to_bits(m)) == m."""
    for mask in [0, 1, 0b101, 0b1111, 0xDEAD, (1 << 63) | (1 << 0)]:
        assert bits_to_mask(*mask_to_bits(mask)) == mask


def test_popcount_matches_mask_to_bits_length() -> None:
    """popcount(m) == len(mask_to_bits(m)) for any mask."""
    for mask in [0, 1, 0b101, 0xDEAD, (1 << 63) | (1 << 0)]:
        assert popcount(mask) == len(mask_to_bits(mask))


def test_evaluate_mask_against_iter_masks() -> None:
    """Sanity: every K=2 mask is satisfied by a bar with all of its bits set."""
    for mask in iter_masks_k_of_n(2, n=6):
        assert evaluate_mask(mask, mask) is True
        # And NOT satisfied by a bar missing the lowest bit
        bar_without_lowest = mask & (mask - 1)  # drop lowest set bit
        assert evaluate_mask(bar_without_lowest, mask) is False


def test_total_count_for_52_vocab_canonical() -> None:
    """Sum of C(52, k) for k=0..52 = 2^52 (total power-set size)."""
    total = sum(count_combos(k) for k in range(53))
    assert total == 1 << 52


# ─────────────────────────────────────────────────────────────────
# Math.comb sanity bridge
# ─────────────────────────────────────────────────────────────────


def test_count_combos_uses_exact_arithmetic() -> None:
    """count_combos returns integer-exact results matching math.comb."""
    for n in range(10):
        for k in range(n + 1):
            assert count_combos(k, n=n) == comb(n, k)
