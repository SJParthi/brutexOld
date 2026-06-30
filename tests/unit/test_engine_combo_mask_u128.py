"""u128 mask coverage for ``brutex.engine.combo_mask`` (DEC-VOCAB-U128-CODEGEN-001).

PR-1 widened the strategy mask u64 → u128. numpy has no uint128, so the vectorised
oracle / fallback represents a mask as two u64 limbs ``(lo, hi)``; the production
``count_supermasks`` routes any mask/bar that uses a bit ``>= 64`` to that two-limb
reference so the u64 Rust kernel never silently truncates a high bit. These tests
pin the >=64-bit (u128 headroom) branches that the legacy u64 suite never reached,
and prove the three views (numpy two-limb reference · production kernel-or-fallback
· Python-int scalar) agree bit-for-bit on a u128 domain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brutex.engine.combo_mask import (
    U64_MASK,
    ResidentBarBuffer,
    count_supermasks,
    count_supermasks_reference,
    evaluate_mask,
)

if TYPE_CHECKING:
    import pytest

# A bar + mask that BOTH set a bit >= 64 (the u128 headroom) and a bit < 64.
_HI_BAR = (1 << 70) | (1 << 80) | (1 << 3) | (1 << 1)
_HI_MASK = (1 << 70) | (1 << 1)


def test_u64_mask_constant() -> None:
    """``U64_MASK`` is the low-limb mask (all 64 low bits set)."""
    assert U64_MASK == (1 << 64) - 1


def test_count_supermasks_routes_high_bit_to_two_limb_reference() -> None:
    """A mask with a bit >= 64 routes to the two-limb reference (no truncation).

    Covers ``combo_mask.py`` line 414 — the production ``count_supermasks`` guard
    that detects a u128 mask/bar and defers to the bit-exact reference instead of
    the u64 Rust kernel (which would drop the high bits).
    """
    bars = [_HI_BAR, 1 << 70, 0b1010]
    masks = [_HI_MASK, 1 << 70, 0]
    got = count_supermasks(bars, masks)
    # Reference is the oracle; both must agree, and a u64-only kernel would
    # WRONGLY count the truncated low limbs.
    assert got == count_supermasks_reference(bars, masks)
    # Hand-verified: _HI_BAR ⊇ _HI_MASK (1), only _HI_BAR & (1<<70)-bar ⊇ (1<<70) (2),
    # every bar ⊇ 0 (3).
    assert got == [1, 2, 3]


def test_count_supermasks_high_bar_with_low_mask_routes_too() -> None:
    """A high-bit BAR (even with a low mask) also routes to the reference (line 414, OR arm)."""
    bars = [1 << 90, 0b11]
    masks = [0b01]  # a u64-fitting mask, but a bar exceeds u64
    got = count_supermasks(bars, masks)
    assert got == count_supermasks_reference(bars, masks)
    assert got == [1]  # only the 0b11 bar is a superset of 0b01


def test_resident_buffer_rust_path_for_u64_bars() -> None:
    """All-u64 bars build the Rust resident kernel (fast path).

    Covers ``combo_mask.py`` 483→491 — the ``fits_u64`` True branch that
    constructs ``fno_math.ResidentBars`` and returns before the numpy split.
    """
    rb = ResidentBarBuffer([0b111, 0b101, 0b010])
    assert rb.n_bars == 3
    # The Rust kernel serves u64 masks directly. Supersets of 0b101: {0b111,0b101}=2;
    # of 0b010: {0b111,0b010}=2; of 0: all 3.
    assert rb.count_supermasks([0b101, 0b010, 0]) == [2, 2, 3]
    assert rb.count_supermask(0b101) == 2


def test_resident_buffer_rust_backed_with_u128_mask_rebuilds_limbs() -> None:
    """A u64-backed (Rust) buffer asked for a u128 mask rebuilds limbs from _raw.

    Covers ``combo_mask.py`` line 520 — the ``self._bar_lo is None`` branch:
    the buffer is Rust-backed (all bars fit u64) but a queried mask sets a bit
    >= 64, so the count cannot go through the u64 kernel and must rebuild the
    two-limb arrays from the kept raw bars.
    """
    bars = [0b111, 0b101, 0b010]
    rb = ResidentBarBuffer(bars)
    # A u128 mask (bit 70 set) — no bar has it, so every count is 0.
    assert rb.count_supermasks([1 << 70, 0b101]) == [0, 2]


def test_resident_buffer_numpy_fallback_for_u128_bars() -> None:
    """A bar with a bit >= 64 forces the two-limb numpy backing (not the u64 kernel)."""
    bars = [_HI_BAR, 1 << 70, 0b1010]
    rb = ResidentBarBuffer(bars)
    assert rb.n_bars == 3
    assert rb.count_supermasks([_HI_MASK, 1 << 70, 0]) == [1, 2, 3]


def test_resident_buffer_falls_back_when_rust_kernel_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ImportError/AttributeError from the Rust kernel falls back to numpy.

    Covers ``combo_mask.py`` 487-488 — the ``except (ImportError, AttributeError):
    pass`` arm of ``ResidentBarBuffer.__init__``. When ``fno_math.ResidentBars``
    cannot be constructed (an older/incompatible wheel missing the resident type),
    the buffer degrades to the two-limb numpy backing and still serves bit-exact
    superset counts — no crash, no truncation.
    """

    def _raise(_bars: object) -> object:
        msg = "ResidentBars unavailable in this wheel"
        raise AttributeError(msg)

    monkeypatch.setattr("brutex.engine.combo_mask.fno_math.ResidentBars", _raise)
    bars = [0b111, 0b101, 0b010]  # all fit u64 -> takes the try/except path
    rb = ResidentBarBuffer(bars)
    # The Rust buffer was NOT built; the numpy two-limb backing serves the count.
    assert rb._rust is None
    assert rb.n_bars == 3
    # Identical answers to the kernel path: supersets of 0b101 -> {0b111,0b101}=2,
    # of 0b010 -> {0b111,0b010}=2, of 0 -> all 3.
    assert rb.count_supermasks([0b101, 0b010, 0]) == [2, 2, 3]


def test_resident_buffer_numpy_path_empty_masks_returns_empty() -> None:
    """A numpy-backed (u128) buffer with no masks returns ``[]``.

    Covers ``combo_mask.py`` line 512 — the ``len(masks) == 0`` early return on the
    two-limb path (the Rust fast path is skipped because a bar uses a bit >= 64).
    """
    rb = ResidentBarBuffer([1 << 70, 0b101])  # a high bit -> numpy two-limb backing
    assert rb._rust is None
    assert rb.count_supermasks([]) == []


def test_resident_buffer_numpy_path_empty_window_returns_zeros() -> None:
    """A numpy-backed buffer with zero bars returns one ``0`` per mask.

    Covers ``combo_mask.py`` line 514 — the ``self._n == 0`` early return on the
    two-limb path. An empty bar window is forced onto the numpy backing by setting
    a high-bit-capable backing with no bars (every mask matches zero bars).
    """
    rb = ResidentBarBuffer([])
    # An empty all-u64 build still constructs the Rust kernel, so query a u128 mask
    # to force the two-limb path; with no bars every count is 0.
    assert rb.n_bars == 0
    assert rb.count_supermasks([1 << 70, 0b1, 0]) == [0, 0, 0]


def test_three_view_u128_parity() -> None:
    """numpy two-limb reference == production count == Python-int scalar, on u128.

    The core parity oracle extended to 128 bits: for a set of u128 bars/masks the
    vectorised reference, the production ``count_supermasks`` (kernel-or-fallback),
    and the scalar ``(bar & mask) == mask`` all agree.
    """
    bars = [_HI_BAR, _HI_MASK, (1 << 100) | 1, 0]
    masks = [_HI_MASK, 1 << 100, 1, 0]
    ref = count_supermasks_reference(bars, masks)
    prod = count_supermasks(bars, masks)
    scalar = [sum(1 for b in bars if evaluate_mask(b, m)) for m in masks]
    assert ref == prod == scalar
