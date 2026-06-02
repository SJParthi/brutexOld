"""Hypothesis property tests — Rust ``count_supermasks_u64s`` parity.

Closes the determinism guarantee for the Stage-2 hot kernel shipped under
DEC-RUST-SUPERMASK-KERNEL-001 (ADR-0004 "Rust CPU kernel first"). The Rust
kernel ``fno_math.count_supermasks_u64s`` replaces the inner numpy pass of
``brutex.engine.combo_mask.count_supermasks``; this module proves it is
**bit-for-bit identical** to the numpy reference oracle
``count_supermasks_reference`` for arbitrary bars/masks.

Per CLAUDE.md Golden Rule #4 (reproducibility-first) the Stage-2 hit count
feeds the blake3 run-identity hash; a single off-by-one between the Rust
and numpy paths would silently invalidate every downstream result. The
kernel is pure integer arithmetic — no floats, no reduction-order
ambiguity — so the two paths must agree exactly on every input.

Invariants covered
------------------

1. **Bit-exact parity** — for arbitrary ``(bar_bits, masks)`` the Rust
   kernel equals the numpy reference equals the per-bar ``evaluate_mask``
   ground truth.
2. **Edge cases** — empty masks → ``[]``; empty bars → ``0`` per mask;
   ``mask == 0`` → all bars; full-width (bit 63) masks.
3. **Determinism** — repeated calls on the same input return the same
   result (no hidden state / nondeterminism).
"""

from __future__ import annotations

import fno_math
from brutex.engine.combo_mask import (
    count_supermasks,
    count_supermasks_reference,
    evaluate_mask,
)
from hypothesis import given
from hypothesis import strategies as st

_U64 = st.integers(min_value=0, max_value=(1 << 64) - 1)
_BARS = st.lists(_U64, max_size=200)
_MASKS = st.lists(_U64, max_size=64)


@given(bars=_BARS, masks=_MASKS)
def test_rust_kernel_matches_numpy_reference(bars: list[int], masks: list[int]) -> None:
    """Rust kernel == numpy reference oracle, bit-for-bit."""
    rust = fno_math.count_supermasks_u64s(bars, masks)
    reference = count_supermasks_reference(bars, masks)
    assert list(rust) == reference


@given(bars=_BARS, masks=_MASKS)
def test_facade_matches_reference(bars: list[int], masks: list[int]) -> None:
    """The combo_mask facade (Rust-backed) == numpy reference."""
    assert count_supermasks(bars, masks) == count_supermasks_reference(bars, masks)


@given(bars=_BARS, masks=_MASKS)
def test_kernel_matches_per_bar_ground_truth(bars: list[int], masks: list[int]) -> None:
    """Both paths agree with the per-bar evaluate_mask ground truth."""
    truth = [sum(evaluate_mask(b, m) for b in bars) for m in masks]
    assert count_supermasks(bars, masks) == truth
    assert count_supermasks_reference(bars, masks) == truth


@given(bars=_BARS, masks=_MASKS)
def test_kernel_is_deterministic(bars: list[int], masks: list[int]) -> None:
    """Repeated calls on the same input yield identical output."""
    first = fno_math.count_supermasks_u64s(bars, masks)
    second = fno_math.count_supermasks_u64s(bars, masks)
    assert list(first) == list(second)


@given(bars=_BARS)
def test_mask_zero_matches_all_bars(bars: list[int]) -> None:
    """mask == 0 matches every bar (count == len(bars))."""
    assert fno_math.count_supermasks_u64s(bars, [0]) == [len(bars)]
    assert fno_math.count_supermask_u64s(bars, 0) == len(bars)


@given(masks=_MASKS)
def test_empty_bars_zero_per_mask(masks: list[int]) -> None:
    """Empty bars → 0 per mask."""
    assert fno_math.count_supermasks_u64s([], masks) == [0] * len(masks)


@given(bars=_BARS, mask=_U64)
def test_single_mask_matches_batched(bars: list[int], mask: int) -> None:
    """The single-mask convenience equals the batched kernel for that mask."""
    single = fno_math.count_supermask_u64s(bars, mask)
    batched = fno_math.count_supermasks_u64s(bars, [mask])
    assert [single] == list(batched)
