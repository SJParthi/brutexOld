"""Track 1 brute-force engine — combo mask iteration + evaluation.

Implements the u64 bitmask combo iteration foundation per
`docs/TRACK1_BACKTEST_SPEC.md §"Stage 1A"`.

## The bitmask model

The 52-condition vocabulary (per `docs/TRACK1_VOCABULARY.md`) packs
into a single u64. At each bar, all 52 conditions are evaluated and
their truth values packed into one u64 — the **bar_bits**.

A **strategy mask** is also a u64: each bit set = a required condition.
The bar matches the strategy iff:

    (bar_bits & strategy_mask) == strategy_mask

This is one AND + one CMP per bar per strategy — O(1) per evaluation.
Foundation for the 33B-trial brute-force sweep.

## Why bitmasks

- Chess-engine bitboard idiom (Agent B 2026-05-16 deep research)
- Vectorizes trivially in Numba / Rust
- One u64 covers all 52 conditions (head-room to 64; ADR required to go u128)
- Bit-equality comparison is the natural reproducibility check

## Combo iteration

For a 52-condition vocab and AND-mask of size K, the count is C(52,K):

    K=1   52
    K=2   1,326
    K=3   22,100
    K=4   270,725
    K=5   2,598,960

This module exposes ``iter_masks_k_of_n(k, n)`` that yields every
u64 mask with exactly K bits set. The implementation is currently
``itertools.combinations``-based (correct, sufficient for K≤5 +
synthetic-data tests); a Gosper's-hack or LUT optimization is a
later Phase-3 throughput task.

Per CLAUDE.md Golden Rule #11 (SCALABLE) and STANDARDS_100PCT.md
§4 row 14 (throughput canary): the API is stable; the impl can
swap to a faster algorithm without breaking callers.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from itertools import combinations

import fno_math

__all__ = [
    "MAX_VOCAB_BITS",
    "VOCAB_BITS",
    "bits_to_mask",
    "count_combos",
    "count_supermasks",
    "count_supermasks_reference",
    "evaluate_mask",
    "iter_masks_k_of_n",
    "mask_to_bits",
    "popcount",
]


# ─────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────

VOCAB_BITS: int = 52
"""Current 52-condition vocabulary size (per docs/TRACK1_VOCABULARY.md).
DEC-PLUGIN-001 / DEC-PLUGIN-002 govern add/remove."""

MAX_VOCAB_BITS: int = 64
"""u64 ceiling. Exceeding requires an explicit ADR per
DEC-PLUGIN-001 (no silent transition to u128)."""


# ─────────────────────────────────────────────────────────────────
# Bit-level primitives
# ─────────────────────────────────────────────────────────────────


def popcount(mask: int) -> int:
    """Count the number of bits set in ``mask``.

    Wraps Python 3.10+ ``int.bit_count()``. O(1) in practice
    (hardware POPCNT on x86 / NEON on ARM).

    Parameters
    ----------
    mask : int
        Any non-negative integer (we use u64 in practice).

    Returns
    -------
    int
        Number of 1-bits.

    Examples
    --------
    >>> popcount(0)
    0
    >>> popcount(0b101101)
    4
    """
    if mask < 0:
        msg = f"popcount: mask must be non-negative, got {mask}"
        raise ValueError(msg)
    return mask.bit_count()


def bits_to_mask(*bit_positions: int) -> int:
    """Build a u64 mask from a set of bit positions.

    Parameters
    ----------
    *bit_positions : int
        Each in ``[0, MAX_VOCAB_BITS)``. Duplicates are deduplicated
        (set semantics — the mask has at most one of each bit).

    Returns
    -------
    int
        u64 with the specified bits set.

    Examples
    --------
    >>> bits_to_mask()
    0
    >>> bits_to_mask(0)
    1
    >>> bits_to_mask(0, 2)
    5
    >>> bits_to_mask(0, 0, 2)  # duplicate ignored
    5
    """
    mask = 0
    for pos in bit_positions:
        if pos < 0 or pos >= MAX_VOCAB_BITS:
            msg = f"bits_to_mask: position {pos} out of range [0, {MAX_VOCAB_BITS})"
            raise ValueError(msg)
        mask |= 1 << pos
    return mask


def mask_to_bits(mask: int) -> tuple[int, ...]:
    """Extract the set bit positions from a mask, ascending.

    Parameters
    ----------
    mask : int
        Non-negative u64.

    Returns
    -------
    tuple[int, ...]
        Bit positions set, in ascending order.

    Examples
    --------
    >>> mask_to_bits(0)
    ()
    >>> mask_to_bits(1)
    (0,)
    >>> mask_to_bits(5)
    (0, 2)
    >>> mask_to_bits(0b101101)
    (0, 2, 3, 5)
    """
    if mask < 0:
        msg = f"mask_to_bits: mask must be non-negative, got {mask}"
        raise ValueError(msg)
    bits: list[int] = []
    pos = 0
    m = mask
    while m:
        if m & 1:
            bits.append(pos)
        m >>= 1
        pos += 1
    return tuple(bits)


# ─────────────────────────────────────────────────────────────────
# Strategy-mask evaluation (the hot path)
# ─────────────────────────────────────────────────────────────────


def evaluate_mask(bar_bits: int, required_mask: int) -> bool:
    """Test whether the bar's truth-vector satisfies the strategy mask.

    The AND-semantics: every bit in ``required_mask`` must also be
    set in ``bar_bits``. One AND + one CMP — O(1).

    This is THE hot-path operation that runs ~33B times in a full
    sweep. The Python implementation here is correct; Rust/Numba
    versions will inline this exact two-instruction sequence.

    Parameters
    ----------
    bar_bits : int
        u64 truth-vector at one bar (all 52 conditions evaluated).
    required_mask : int
        u64 strategy mask. A bit set here means the condition is
        required to fire.

    Returns
    -------
    bool
        True iff every required bit is set in bar_bits.

    Examples
    --------
    >>> evaluate_mask(0b1111, 0b0011)  # bar has 4 bits, requires 2 of them
    True
    >>> evaluate_mask(0b1010, 0b0011)  # bar missing bit 0
    False
    >>> evaluate_mask(0, 0)  # empty mask always matches
    True
    """
    return (bar_bits & required_mask) == required_mask


# ─────────────────────────────────────────────────────────────────
# Batched SUPERSET count — the Stage-2 hot kernel
# ─────────────────────────────────────────────────────────────────


def count_supermasks_reference(bar_bits: Sequence[int], masks: Sequence[int]) -> list[int]:
    """Reference (numpy) per-mask SUPERSET count — the parity oracle.

    For each ``mask`` in ``masks`` count the bars in ``bar_bits`` that are a
    superset of it (``(bar & mask) == mask``). This is the pure-Python/numpy
    implementation of the Stage-2 hot op (see ``evaluate_mask``); it is kept
    as the **bit-exact oracle** that the Rust kernel (:func:`count_supermasks`)
    is verified against in ``tests/property``.

    Each per-mask pass is a numpy-vectorised
    ``((bar_arr & mask) == mask).sum()`` — bars resident, masks streamed —
    mirroring the loop structure of the Rust kernel.

    Parameters
    ----------
    bar_bits : Sequence[int]
        Per-bar packed u64 condition bitsets.
    masks : Sequence[int]
        Strategy masks to evaluate. Each ``out[j]`` aligns with ``masks[j]``.

    Returns
    -------
    list[int]
        Hit count per mask. Empty ``masks`` → ``[]``; empty ``bar_bits`` →
        ``0`` per mask. ``mask == 0`` matches every bar.

    Examples
    --------
    >>> count_supermasks_reference([0b11, 0b111, 0b1000, 0], [0b11, 0b1000, 0])
    [2, 1, 4]
    >>> count_supermasks_reference([], [0b1, 0b11])
    [0, 0]
    >>> count_supermasks_reference([0b1, 0b10], [])
    []
    """
    if len(masks) == 0:
        return []
    if len(bar_bits) == 0:
        return [0] * len(masks)
    # numpy uint64 — the lake's native bar_bits dtype. Pure-integer, so the
    # result is bit-exact and reduction-order independent (Golden Rule #4).
    import numpy as np

    bar_arr = np.asarray(bar_bits, dtype=np.uint64)
    return [int(((bar_arr & np.uint64(mask)) == np.uint64(mask)).sum()) for mask in masks]


def count_supermasks(bar_bits: Sequence[int], masks: Sequence[int]) -> list[int]:
    """Batched per-mask SUPERSET count — the Stage-2 brute-force accelerator.

    For each ``mask`` in ``masks`` return the number of bars in ``bar_bits``
    that satisfy ``(bar & mask) == mask``. ``out[j]`` aligns with ``masks[j]``.

    Delegates to the Rust hot kernel ``fno_math.count_supermasks_u64s``
    (ADR-0004 "Rust CPU kernel first"; a single fused branchless loop,
    masks-outer/bars-inner, memory-bandwidth-bound). The numpy reference
    :func:`count_supermasks_reference` is the parity oracle the kernel is
    verified against bit-for-bit in ``tests/property`` and remains the
    drop-in fallback (identical signature + semantics).

    Parameters
    ----------
    bar_bits : Sequence[int]
        Per-bar packed u64 condition bitsets.
    masks : Sequence[int]
        Strategy masks to evaluate.

    Returns
    -------
    list[int]
        Hit count per mask. Empty ``masks`` → ``[]``; empty ``bar_bits`` →
        ``0`` per mask. ``mask == 0`` matches every bar.

    Examples
    --------
    >>> count_supermasks([0b11, 0b111, 0b1000, 0], [0b11, 0b1000, 0])
    [2, 1, 4]
    >>> count_supermasks([], [0b1, 0b11])
    [0, 0]
    >>> count_supermasks([0b1, 0b10], [])
    []
    """
    return list(fno_math.count_supermasks_u64s(list(bar_bits), list(masks)))


# ─────────────────────────────────────────────────────────────────
# Combinatorial enumeration
# ─────────────────────────────────────────────────────────────────


def count_combos(k: int, n: int = VOCAB_BITS) -> int:
    """Count the number of u64 masks with exactly ``k`` of ``n`` bits set.

    Equivalent to C(n, k). Useful for sizing the sweep upfront.

    Parameters
    ----------
    k : int
        Number of required bits. ``0 <= k <= n``.
    n : int
        Vocabulary size. Default ``VOCAB_BITS`` (= 52).

    Returns
    -------
    int
        C(n, k).

    Examples
    --------
    >>> count_combos(0)
    1
    >>> count_combos(1)
    52
    >>> count_combos(2)
    1326
    >>> count_combos(3)
    22100
    >>> count_combos(0, n=5)
    1
    >>> count_combos(3, n=5)
    10
    """
    if k < 0 or n < 0 or k > n:
        msg = f"count_combos: invalid k={k}, n={n} (require 0 <= k <= n)"
        raise ValueError(msg)
    if n > MAX_VOCAB_BITS:
        msg = (
            f"count_combos: n={n} exceeds MAX_VOCAB_BITS={MAX_VOCAB_BITS}; "
            f"ADR required for u128 transition (DEC-PLUGIN-001)"
        )
        raise ValueError(msg)
    # math.comb in Python 3.8+; integer-exact
    from math import comb

    return comb(n, k)


def iter_masks_k_of_n(k: int, n: int = VOCAB_BITS) -> Iterator[int]:
    """Yield every u64 mask with exactly ``k`` of ``n`` bits set.

    Iteration order is lexicographic on bit-position tuples
    (matches itertools.combinations). The total count is
    ``count_combos(k, n)``.

    For Stage 1A precompute, callers typically iterate K=1..K_MAX
    and apply each yielded mask against the precomputed bar_bits
    Parquet column.

    Performance: this implementation is itertools-based — sufficient
    for K≤5 + synthetic-data testing. A Gosper's-hack or LUT version
    is a Phase-3 throughput optimization (the API is stable; impl
    can swap without breaking callers per Golden Rule #11 SCALABLE).

    Parameters
    ----------
    k : int
        Number of bits to set in each yielded mask. ``0 <= k <= n``.
    n : int
        Vocabulary size. Default ``VOCAB_BITS`` (= 52).

    Yields
    ------
    int
        u64 masks with exactly ``k`` bits set, in lexicographic order.

    Examples
    --------
    >>> list(iter_masks_k_of_n(0, n=3))
    [0]
    >>> list(iter_masks_k_of_n(1, n=3))
    [1, 2, 4]
    >>> list(iter_masks_k_of_n(2, n=3))
    [3, 5, 6]
    >>> list(iter_masks_k_of_n(3, n=3))
    [7]
    """
    if k < 0 or n < 0 or k > n:
        msg = f"iter_masks_k_of_n: invalid k={k}, n={n} (require 0 <= k <= n)"
        raise ValueError(msg)
    if n > MAX_VOCAB_BITS:
        msg = (
            f"iter_masks_k_of_n: n={n} exceeds MAX_VOCAB_BITS={MAX_VOCAB_BITS}; "
            f"ADR required for u128 transition (DEC-PLUGIN-001)"
        )
        raise ValueError(msg)
    for positions in combinations(range(n), k):
        mask = 0
        for p in positions:
            mask |= 1 << p
        yield mask
