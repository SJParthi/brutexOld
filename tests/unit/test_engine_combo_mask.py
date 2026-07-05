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

import sys
import types
from math import comb

import pytest
from brutex.engine import combo_mask
from brutex.engine.combo_mask import (
    MAX_VOCAB_BITS,
    VOCAB_BITS,
    ResidentBarBuffer,
    bits_to_mask,
    count_combos,
    count_supermasks,
    count_supermasks_reference,
    evaluate_mask,
    iter_masks_k_of_bits,
    iter_masks_k_of_n,
    mask_to_bits,
    mask_to_entry_signals,
    popcount,
)

# ─────────────────────────────────────────────────────────────────
# Module-level constants
# ─────────────────────────────────────────────────────────────────


def test_vocab_bits_matches_total_conditions() -> None:
    """VOCAB_BITS tracks the codegen'd vocabulary size (grow-only)."""
    from brutex.vocabulary.bits import TOTAL_CONDITIONS

    assert VOCAB_BITS == TOTAL_CONDITIONS == 60


def test_max_vocab_bits_is_128() -> None:
    """u128 ceiling for the bitmask vocabulary (ADR-0012 / DEC-VOCAB-U128-CODEGEN-001)."""
    assert MAX_VOCAB_BITS == 128


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
    """Position MAX_VOCAB_BITS (= 128) is out of range."""
    with pytest.raises(ValueError, match=f"position {MAX_VOCAB_BITS} out of range"):
        bits_to_mask(MAX_VOCAB_BITS)


def test_bits_to_mask_rejects_above_max() -> None:
    with pytest.raises(ValueError, match="position 200 out of range"):
        bits_to_mask(200)


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
    """C(n, 1) = n (default n = VOCAB_BITS)."""
    assert count_combos(1) == VOCAB_BITS
    assert count_combos(1, n=5) == 5


def test_count_combos_canonical_values() -> None:
    """Verify against known C(52, k) values (explicit n=52)."""
    assert count_combos(2, n=52) == 1326
    assert count_combos(3, n=52) == 22100
    assert count_combos(4, n=52) == 270725
    assert count_combos(5, n=52) == 2598960


def test_combo_mask_doctests_pass() -> None:
    """Run every doctest in ``brutex.engine.combo_mask`` as a bounded gate.

    Regression for the stale 52-bit ``count_combos`` doctest examples
    (52/1326/22100 were the pre-u128-widening C(52, k) values; with
    ``VOCAB_BITS = 60`` the truth is 60/1770/34220). No repo-wide
    ``--doctest-modules`` policy exists, so this single-module gate is
    the only thing keeping the engine module's Examples honest.
    The ``attempted > 0`` guard prevents a silent no-op if the
    docstrings are ever stripped.
    """
    import doctest

    results = doctest.testmod(combo_mask)
    assert results.failed == 0
    assert results.attempted > 0


def test_count_combos_full_set() -> None:
    """C(n, n) = 1 (the all-bits-set combo)."""
    assert count_combos(VOCAB_BITS) == 1
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
    with pytest.raises(ValueError, match=f"n=129 exceeds MAX_VOCAB_BITS={MAX_VOCAB_BITS}"):
        count_combos(1, n=129)


def test_count_combos_accepts_n_up_to_u128_ceiling() -> None:
    """A vocab of exactly MAX_VOCAB_BITS (128) is allowed (the u128 ceiling)."""
    assert count_combos(1, n=MAX_VOCAB_BITS) == MAX_VOCAB_BITS


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
    with pytest.raises(ValueError, match=f"n=129 exceeds MAX_VOCAB_BITS={MAX_VOCAB_BITS}"):
        list(iter_masks_k_of_n(1, n=129))


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


def test_total_count_for_full_vocab_canonical() -> None:
    """Sum of C(n, k) for k=0..n = 2^n (total power-set size), n = VOCAB_BITS."""
    total = sum(count_combos(k) for k in range(VOCAB_BITS + 1))
    assert total == 1 << VOCAB_BITS


# ─────────────────────────────────────────────────────────────────
# Math.comb sanity bridge
# ─────────────────────────────────────────────────────────────────


def test_count_combos_uses_exact_arithmetic() -> None:
    """count_combos returns integer-exact results matching math.comb."""
    for n in range(10):
        for k in range(n + 1):
            assert count_combos(k, n=n) == comb(n, k)


# ─────────────────────────────────────────────────────────────────
# count_supermasks — Rust kernel + numpy reference oracle
# ─────────────────────────────────────────────────────────────────


def test_count_supermasks_reference_empty_masks() -> None:
    """Empty masks → empty list (both impls)."""
    assert count_supermasks_reference([0b1, 0b10], []) == []
    assert count_supermasks([0b1, 0b10], []) == []


def test_count_supermasks_reference_empty_bars() -> None:
    """Empty bars → zero per mask (both impls)."""
    assert count_supermasks_reference([], [0b1, 0b11, 0]) == [0, 0, 0]
    assert count_supermasks([], [0b1, 0b11, 0]) == [0, 0, 0]


def test_count_supermasks_mask_zero_matches_all() -> None:
    """mask == 0 requires nothing → every bar matches."""
    bars = [0b0001, 0b1010, 0b1111]
    assert count_supermasks_reference(bars, [0]) == [3]
    assert count_supermasks(bars, [0]) == [3]


def test_count_supermasks_all_match() -> None:
    """Every bar a superset of the mask → count == len(bars)."""
    bars = [0b0011, 0b0111, 0b1111, 0b1011]
    assert count_supermasks(bars, [0b0011]) == [4]


def test_count_supermasks_none_match() -> None:
    """No bar has the required bit → count 0."""
    bars = [0b0001, 0b0010, 0b0100, 0b0111]
    assert count_supermasks(bars, [0b1000]) == [0]


def test_count_supermasks_partial_match() -> None:
    """Only bars with all required bits count."""
    bars = [0b0011, 0b0001, 0b0010, 0b1011, 0b0100]
    assert count_supermasks(bars, [0b0011]) == [2]


def test_count_supermasks_multi_mask_aligned() -> None:
    """out[j] aligns with masks[j]; matches the reference oracle."""
    bars = [0b0011, 0b0111, 0b1000, 0b1011, 0b0000]
    masks = [0b0011, 0b1000, 0b1111, 0]
    expected = [3, 2, 0, 5]
    assert count_supermasks(bars, masks) == expected
    assert count_supermasks_reference(bars, masks) == expected


def test_count_supermasks_high_bit() -> None:
    """Bit 63 (full u64 width) handled correctly."""
    top = 1 << 63
    bars = [top, top | 1, 0, 1]
    assert count_supermasks(bars, [top]) == [2]
    assert count_supermasks_reference(bars, [top]) == [2]


def test_count_supermasks_matches_reference_via_evaluate_mask() -> None:
    """Both impls agree with the per-bar evaluate_mask ground truth."""
    bars = [0, 1, 0b1011, 0b1111, 1 << 40, 0xDEAD_BEEF]
    masks = [0, 1, 0b1010, 1 << 40, 0xFFFF_FFFF_FFFF_FFFF]
    truth = [sum(evaluate_mask(b, m) for b in bars) for m in masks]
    assert count_supermasks(bars, masks) == truth
    assert count_supermasks_reference(bars, masks) == truth


# ─────────────────────────────────────────────────────────────────
# ResidentBarBuffer — Rust-backed + numpy fallback (DEC-RUST-RESIDENT-BARS-001)
# ─────────────────────────────────────────────────────────────────
#
# The buffer has two backends — the Rust ``fno_math.ResidentBars`` (always
# present in CI) and a numpy fallback (forced via ``sys.modules``). Both
# branches of construction AND both branches of every method must be covered,
# and both must agree bit-for-bit with ``count_supermasks_reference``. The
# fallback-forcing idiom mirrors ``tests/unit/test_orchestrator_bloom.py``
# (``monkeypatch.setitem(sys.modules, "fno_math", None)``).


def _force_numpy_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import fno_math`` raise ImportError → numpy fallback branch."""
    monkeypatch.setitem(sys.modules, "fno_math", None)


def _force_attribute_error_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake ``fno_math`` lacking ``ResidentBars`` → AttributeError arm."""
    fake = types.ModuleType("fno_math")
    monkeypatch.setitem(sys.modules, "fno_math", fake)


# ── Rust-backed path (CI always has the kernel) ──


def test_resident_rust_empty_bars_zero_per_mask() -> None:
    buf = ResidentBarBuffer([])
    assert buf.n_bars == 0
    assert buf.count_supermasks([0b1, 0b11, 0]) == [0, 0, 0]


def test_resident_rust_empty_masks_empty() -> None:
    buf = ResidentBarBuffer([0b1, 0b10])
    assert buf.count_supermasks([]) == []


def test_resident_rust_mask_zero_matches_all() -> None:
    buf = ResidentBarBuffer([0b0001, 0b1010, 0b1111])
    assert buf.count_supermasks([0]) == [3]


def test_resident_rust_single_mask_via_count_supermask() -> None:
    buf = ResidentBarBuffer([0b0011, 0b0001, 0b0010, 0b1011, 0b0100])
    assert buf.count_supermask(0b0011) == 2


def test_resident_rust_n_bars_getter() -> None:
    assert ResidentBarBuffer([1, 2, 3, 4]).n_bars == 4


def test_resident_rust_high_bit_and_full_width() -> None:
    top = 1 << 63
    full = 0xFFFF_FFFF_FFFF_FFFF
    buf = ResidentBarBuffer([top, top | 1, full, 0, 1])
    assert buf.count_supermask(top) == 3  # top, top|1, full
    assert buf.count_supermask(full) == 1  # only full


def test_resident_rust_matches_reference() -> None:
    bars = [0, 1, 0b1011, 0b1111, 1 << 40, 1 << 63, 0xDEAD_BEEF]
    masks = [0, 1, 0b1010, 1 << 40, 0xFFFF_FFFF_FFFF_FFFF]
    buf = ResidentBarBuffer(bars)
    assert buf.count_supermasks(masks) == count_supermasks_reference(bars, masks)


def test_resident_rust_reuse_across_many_calls_stable() -> None:
    """REUSE: one instance, many batches — results stable & == reference."""
    bars = [0b0011, 0b0111, 0b1111, 0b1011, 0b0000, 1 << 63]
    buf = ResidentBarBuffer(bars)
    batches = [[0b0011], [0b0100, 0], [1 << 63, 0b1111], []]
    for _ in range(3):
        for batch in batches:
            assert buf.count_supermasks(batch) == count_supermasks_reference(bars, batch)


def test_resident_rust_construction_from_empty_sequence() -> None:
    buf = ResidentBarBuffer([])
    assert buf.n_bars == 0
    assert buf.count_supermask(0b11) == 0


# ── numpy fallback path (forced absent) ──


def test_resident_fallback_empty_bars_zero_per_mask(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_numpy_fallback(monkeypatch)
    buf = ResidentBarBuffer([])
    assert buf.n_bars == 0
    assert buf.count_supermasks([0b1, 0b11, 0]) == [0, 0, 0]


def test_resident_fallback_empty_masks_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_numpy_fallback(monkeypatch)
    buf = ResidentBarBuffer([0b1, 0b10])
    assert buf.count_supermasks([]) == []


def test_resident_fallback_mask_zero_matches_all(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_numpy_fallback(monkeypatch)
    buf = ResidentBarBuffer([0b0001, 0b1010, 0b1111])
    assert buf.count_supermasks([0]) == [3]


def test_resident_fallback_single_mask_via_count_supermask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_numpy_fallback(monkeypatch)
    buf = ResidentBarBuffer([0b0011, 0b0001, 0b0010, 0b1011, 0b0100])
    assert buf.count_supermask(0b0011) == 2


def test_resident_fallback_n_bars_getter(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_numpy_fallback(monkeypatch)
    assert ResidentBarBuffer([1, 2, 3, 4]).n_bars == 4


def test_resident_fallback_high_bit_and_full_width(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_numpy_fallback(monkeypatch)
    top = 1 << 63
    full = 0xFFFF_FFFF_FFFF_FFFF
    buf = ResidentBarBuffer([top, top | 1, full, 0, 1])
    assert buf.count_supermask(top) == 3
    assert buf.count_supermask(full) == 1


def test_resident_fallback_matches_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_numpy_fallback(monkeypatch)
    bars = [0, 1, 0b1011, 0b1111, 1 << 40, 1 << 63, 0xDEAD_BEEF]
    masks = [0, 1, 0b1010, 1 << 40, 0xFFFF_FFFF_FFFF_FFFF]
    buf = ResidentBarBuffer(bars)
    assert buf.count_supermasks(masks) == count_supermasks_reference(bars, masks)


def test_resident_fallback_reuse_across_many_calls_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_numpy_fallback(monkeypatch)
    bars = [0b0011, 0b0111, 0b1111, 0b1011, 0b0000, 1 << 63]
    buf = ResidentBarBuffer(bars)
    batches = [[0b0011], [0b0100, 0], [1 << 63, 0b1111], []]
    for _ in range(3):
        for batch in batches:
            assert buf.count_supermasks(batch) == count_supermasks_reference(bars, batch)


def test_resident_fallback_construction_from_empty_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_numpy_fallback(monkeypatch)
    buf = ResidentBarBuffer([])
    assert buf.n_bars == 0
    assert buf.count_supermask(0b11) == 0


def test_resident_fallback_equals_rust_backed() -> None:
    """The numpy fallback and the Rust backend produce identical results."""
    bars = [0, 1, 0b1011, 0b1111, 1 << 40, 1 << 63, 0xDEAD_BEEF, 0]
    masks = [0, 1, 0b1010, 1 << 40, 0xFFFF_FFFF_FFFF_FFFF, 0b11]
    rust_buf = ResidentBarBuffer(bars)
    rust_out = rust_buf.count_supermasks(masks)
    import unittest.mock

    with unittest.mock.patch.dict(sys.modules, {"fno_math": None}):
        fallback_out = ResidentBarBuffer(bars).count_supermasks(masks)
    assert rust_out == fallback_out == count_supermasks_reference(bars, masks)


# ── AttributeError arm (older wheel lacking ResidentBars) ──


def test_resident_attribute_error_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake fno_math WITHOUT ResidentBars hits the AttributeError arm."""
    _force_attribute_error_fallback(monkeypatch)
    bars = [0b0011, 0b0111, 0b1000, 0b1011, 0b0000]
    masks = [0b0011, 0b1000, 0b1111, 0]
    buf = ResidentBarBuffer(bars)
    # Constructed via the numpy fallback (the import succeeded but the class
    # attribute is missing → AttributeError caught).
    assert buf.n_bars == 5
    assert buf.count_supermasks(masks) == count_supermasks_reference(bars, masks)


class TestMaskToEntrySignals:
    """``mask_to_entry_signals`` — the Stage-1A → trade-sim seam (DEC-SWEEP-DRIVER-001)."""

    def test_and_semantics(self) -> None:
        assert mask_to_entry_signals([0b111, 0b101, 0b010], 0b101) == [True, True, False]

    def test_empty_mask_matches_every_bar(self) -> None:
        assert mask_to_entry_signals([1, 2, 3], 0) == [True, True, True]

    def test_empty_series_yields_empty(self) -> None:
        assert mask_to_entry_signals([], 0b1) == []

    def test_equals_per_bar_evaluate_mask(self) -> None:
        bits = [0, 1, 2, 3, 4, 5]
        assert mask_to_entry_signals(bits, 0b10) == [evaluate_mask(b, 0b10) for b in bits]


class TestIterMasksKOfBits:
    """K-of-an-ARBITRARY-condition-subset generator (DEC-SWEEP-SUBSET-001).

    Generalises ``iter_masks_k_of_n`` (first n contiguous bits) to a chosen subset
    of the 52 condition bits, so a sweep can brute-force over e.g. only the pivot +
    Fibonacci bits, k at a time.
    """

    def test_two_of_three_chosen_bits(self) -> None:
        # bits {0,3,5}, k=2 → {0,3}=9, {0,5}=33, {3,5}=40 (lexicographic).
        assert list(iter_masks_k_of_bits([0, 3, 5], 2)) == [9, 33, 40]

    def test_duplicates_ignored(self) -> None:
        assert list(iter_masks_k_of_bits([5, 5, 5], 1)) == [32]

    def test_unsorted_input_sorted_deterministically(self) -> None:
        # Order of the input bits does not change the (sorted) enumeration order.
        assert list(iter_masks_k_of_bits([5, 0, 3], 2)) == [9, 33, 40]

    def test_k_zero_yields_empty_mask(self) -> None:
        assert list(iter_masks_k_of_bits([7, 2], 0)) == [0]

    def test_k_equals_n_yields_full_mask(self) -> None:
        # All chosen bits set at once → a single mask.
        assert list(iter_masks_k_of_bits([1, 4, 9], 3)) == [bits_to_mask(1, 4, 9)]

    def test_count_matches_count_combos(self) -> None:
        bits = [2, 7, 11, 20, 41]
        for k in range(len(bits) + 1):
            assert len(list(iter_masks_k_of_bits(bits, k))) == count_combos(k, len(bits))

    def test_every_mask_has_exactly_k_chosen_bits(self) -> None:
        bits = [3, 8, 50, 63]
        for mask in iter_masks_k_of_bits(bits, 2):
            chosen = [b for b in mask_to_bits(mask) if b in bits]
            assert len(chosen) == 2
            assert popcount(mask) == 2

    def test_negative_position_raises(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            list(iter_masks_k_of_bits([-1, 3], 1))

    def test_position_at_or_above_max_raises(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            list(iter_masks_k_of_bits([0, MAX_VOCAB_BITS], 1))

    def test_k_too_large_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid k"):
            list(iter_masks_k_of_bits([0, 1, 2], 4))

    def test_k_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid k"):
            list(iter_masks_k_of_bits([0, 1, 2], -1))

    def test_empty_bits_k_zero_yields_empty_mask(self) -> None:
        assert list(iter_masks_k_of_bits([], 0)) == [0]
