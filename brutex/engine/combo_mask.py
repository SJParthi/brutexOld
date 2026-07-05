"""Track 1 brute-force engine — combo mask iteration + evaluation.

Implements the u128 bitmask combo iteration foundation per
`docs/TRACK1_BACKTEST_SPEC.md §"Stage 1A"`.

## The bitmask model

The condition vocabulary (per `docs/TRACK1_VOCABULARY.md` +
`rust/trading-core/conditions.toml`) packs into a single **u128** mask
(DEC-VOCAB-U128-CODEGEN-001 / ADR-0012 D4). At each bar, the conditions are
evaluated and their truth values packed into one mask — the **bar_bits**.

A **strategy mask** is also a u128: each bit set = a required condition.
The bar matches the strategy iff:

    (bar_bits & strategy_mask) == strategy_mask

This is one AND + one CMP per bar per strategy — O(1) per evaluation (a u128 AND
is two u64 ANDs, still constant-time per bar). Foundation for the brute-force sweep.

## Why bitmasks

- Chess-engine bitboard idiom (Agent B 2026-05-16 deep research)
- Vectorizes trivially in Numba / Rust
- u128 covers the vocabulary with ~64 bits of headroom (ADR-0012 / DEC-PLUGIN-001
  is the satisfied gate for the u64→u128 widening — not a silent transition)
- Bit-equality comparison is the natural reproducibility check

## numpy + u128 (the honest envelope)

numpy has NO uint128 dtype. The Python scalar path uses arbitrary-precision
`int` (already u128-safe). The vectorised numpy oracle / fallback represents a
u128 as **two u64 limbs** `(lo, hi)` and tests
`((bar_lo & m_lo) == m_lo) & ((bar_hi & m_hi) == m_hi)` — two u64 ANDs per bar,
still O(1) per bar. The production Rust kernel (`fno_math`) is u64-native; when a
mask or bar exceeds 64 bits the batch count routes to the two-limb numpy
reference so no high bit is ever silently truncated (the new VWAP/R5-S5/BOS/CHoCH
bits 52-59 still fit u64; the >=64-bit headroom is exercised by the tests).

## Combo iteration

For a 60-condition vocab and AND-mask of size K, the count is C(60,K):

    K=1   60
    K=2   1,770
    K=3   34,220
    K=4   487,635
    K=5   5,461,512

This module exposes ``iter_masks_k_of_n(k, n)`` that yields every
u128 mask with exactly K bits set. The implementation is currently
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
from typing import TYPE_CHECKING

import fno_math

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

__all__ = [
    "MAX_VOCAB_BITS",
    "U64_MASK",
    "VOCAB_BITS",
    "ResidentBarBuffer",
    "bits_to_mask",
    "count_combos",
    "count_supermasks",
    "count_supermasks_reference",
    "evaluate_mask",
    "iter_masks_k_of_bits",
    "iter_masks_k_of_n",
    "mask_to_bits",
    "mask_to_entry_signals",
    "popcount",
]


# ─────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────

VOCAB_BITS: int = 60
"""Current condition vocabulary size (the dense bit count from
``rust/trading-core/conditions.toml`` / ``brutex.vocabulary.bits.TOTAL_CONDITIONS``).
DEC-PLUGIN-001 / DEC-PLUGIN-002 govern add/remove (grow-only)."""

MAX_VOCAB_BITS: int = 128
"""u128 ceiling. The u64→u128 widening is ADR-gated by ADR-0012
(DEC-NIFTY-DYNAMIC-STRATEGY-001) / DEC-VOCAB-U128-CODEGEN-001 — the explicit gate
DEC-PLUGIN-001 requires for a mask-width transition (NOT a silent change)."""

#: 64-bit limb mask — splits a u128 into ``(lo, hi)`` u64 limbs for the numpy
#: oracle/fallback (numpy has no uint128). ``lo = m & U64_MASK``, ``hi = m >> 64``.
U64_MASK: int = (1 << 64) - 1


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
        u128 truth-vector at one bar (all 60 conditions evaluated).
    required_mask : int
        u128 strategy mask. A bit set here means the condition is
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


def mask_to_entry_signals(bar_bits: Sequence[int], required_mask: int) -> list[bool]:
    """Project a strategy mask onto a bar-bits series → one entry signal per bar.

    THE seam between Stage-1A (which produces the per-bar ``bar_bits``
    truth-vectors) and :func:`brutex.engine.trade_sim.simulate_trades` (which
    consumes one ``bool`` per bar): a bar fires iff it satisfies the mask's
    AND-semantics (every required bit set). Equivalent to
    ``[evaluate_mask(b, required_mask) for b in bar_bits]`` — one AND + one CMP
    per bar (O(1) each; O(n) over the series). Same length as ``bar_bits``.

    Parameters
    ----------
    bar_bits : Sequence[int]
        Per-bar u128 truth-vectors (e.g. from
        :func:`brutex.engine.stage1a.build_bar_bits`).
    required_mask : int
        The u128 strategy mask. An empty mask (``0``) fires on every bar.

    Returns
    -------
    list[bool]
        One entry signal per bar, in input order (1:1 with ``bar_bits``).

    Examples
    --------
    >>> mask_to_entry_signals([0b111, 0b101, 0b010], 0b101)
    [True, True, False]
    >>> mask_to_entry_signals([1, 2, 3], 0)  # empty mask matches every bar
    [True, True, True]
    """
    return [(b & required_mask) == required_mask for b in bar_bits]


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
    # u128 via two u64 limbs (numpy has no uint128). Pure-integer, so the result
    # is bit-exact and reduction-order independent (Golden Rule #4). The limb
    # split + per-mask count own their numpy imports.
    bar_lo, bar_hi = _split_limbs(bar_bits)
    return [_count_two_limb(bar_lo, bar_hi, mask) for mask in masks]


def _split_limbs(values: Sequence[int]) -> tuple[npt.NDArray[np.uint64], npt.NDArray[np.uint64]]:
    """Split a sequence of u128 ints into two parallel ``uint64`` numpy arrays.

    Returns ``(lo, hi)`` where ``lo = v & U64_MASK`` and ``hi = v >> 64`` — the
    two-limb representation the numpy oracle/fallback uses because numpy has no
    uint128. Python-int masking happens BEFORE the numpy cast so a value with
    bits >= 64 never overflows a ``np.uint64`` construction (CLAUDE.md §11 COMMON
    — the ONE limb-split, reused by the free reference + the resident buffer).
    """
    import numpy as np

    lo = np.asarray([v & U64_MASK for v in values], dtype=np.uint64)
    hi = np.asarray([v >> 64 for v in values], dtype=np.uint64)
    return lo, hi


def _count_two_limb(
    bar_lo: npt.NDArray[np.uint64], bar_hi: npt.NDArray[np.uint64], mask: int
) -> int:
    """Two-limb SUPERSET count: bars whose ``(bar & mask) == mask`` over u128.

    ``((bar_lo & m_lo) == m_lo) & ((bar_hi & m_hi) == m_hi)`` — two u64 ANDs +
    two compares per bar, bit-identical to the scalar ``(bar & mask) == mask``.
    """
    import numpy as np

    m_lo = np.uint64(mask & U64_MASK)
    m_hi = np.uint64(mask >> 64)
    lo_ok = (bar_lo & m_lo) == m_lo
    hi_ok = (bar_hi & m_hi) == m_hi
    return int((lo_ok & hi_ok).sum())


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
    # The Rust hot kernel is u64-native. If any mask OR bar uses a bit >= 64
    # (the u128 headroom) the kernel would silently truncate it — so route to the
    # bit-exact two-limb numpy reference instead (no high bit is ever dropped).
    # Today's vocabulary (bits 0-59) fits u64, so the fast Rust path is the norm;
    # the >=64-bit branch is the correctness guard for future growth.
    if any(m > U64_MASK for m in masks) or any(b > U64_MASK for b in bar_bits):
        return count_supermasks_reference(bar_bits, masks)
    return list(fno_math.count_supermasks_u64s(list(bar_bits), list(masks)))


# ─────────────────────────────────────────────────────────────────
# Resident bar buffer — the Stage-2 batch accelerator
# ─────────────────────────────────────────────────────────────────


class ResidentBarBuffer:
    """Hold a pre-sliced bar-bits window resident for repeated batch counts.

    In a Stage-2 sweep the SAME ``bar_bits`` window is evaluated against many
    successive mask batches. The free :func:`count_supermasks` re-marshals the
    whole ``bar_bits`` (~94k u64) across the Python→Rust boundary on EVERY
    call (``combo_mask.py:315``). This buffer moves ``bar_bits`` across the
    boundary ONCE — into a resident Rust ``Vec<u64>`` (``fno_math.ResidentBars``)
    — and reuses it; thereafter only ``masks`` crosses per call.

    When the Rust kernel is unavailable (plain ``pip install`` without the
    maturin-built wheel) the buffer transparently falls back to a resident
    numpy ``uint64`` array reproducing :func:`count_supermasks_reference`
    bit-for-bit. Exactly ONE backing store is held (Rust ``Vec`` XOR numpy
    array, never both).

    This is a Stage-2 AGGREGATE-COUNT primitive over a PRE-SLICED window — it
    returns per-mask hit counts over a fixed bar set, NOT a per-bar feed, so it
    has no look-ahead semantics (it cannot see "future" bars; the window is
    chosen by the caller before construction).

    Complexity (honest envelope — this is NOT an "O(1) Stage-2" primitive):

    * Construction is ``O(N_bars)`` — the one-time copy into the resident
      buffer, paid ONCE here.
    * Each :meth:`count_supermasks` call is ``O(N_masks · N_bars)`` compute +
      ``O(N_masks)`` marshalling. The ``O(N_bars)`` marshalling that the free
      :func:`count_supermasks` repeats every call is paid ONCE at construction
      — that elimination, repeated across many batches, is the entire win (it
      grows with the number of batches and with ``N_bars``). The inner kernel
      is byte-identical to the shipped free functions.

    Examples
    --------
    Construct once over a bar window, then count many mask batches against it;
    each ``count_supermasks`` call returns one hit count per mask, positionally
    aligned with the masks. ``mask == 0`` matches every bar; an empty mask list
    returns an empty list; an empty bar window returns ``0`` per mask. The
    results are bit-identical to :func:`count_supermasks_reference`.
    """

    def __init__(self, bar_bits: Sequence[int]) -> None:
        """Build the resident buffer, paying the one-time ``O(N_bars)`` copy.

        Parameters
        ----------
        bar_bits : Sequence[int]
            Per-bar packed u128 condition bitsets for the pre-sliced window.
        """
        self._n = len(bar_bits)
        # The Rust resident kernel is u64-native; only use it when EVERY bar fits
        # u64 (no high-bit truncation). A bar with bits >= 64 forces the two-limb
        # numpy backing (numpy has no uint128) — bit-exact for the full u128.
        fits_u64 = all(b <= U64_MASK for b in bar_bits)
        self._rust: object | None = None
        #: Raw bar list kept alongside the Rust buffer so a (rare) u128 mask can
        #: be served via the two-limb numpy path without crossing the u64 kernel.
        self._raw: tuple[int, ...] = tuple(bar_bits)
        self._bar_lo: npt.NDArray[np.uint64] | None = None
        self._bar_hi: npt.NDArray[np.uint64] | None = None
        if fits_u64:
            try:
                self._rust = fno_math.ResidentBars(list(bar_bits))
                return
            except (ImportError, AttributeError):
                pass
        self._bar_lo, self._bar_hi = _split_limbs(bar_bits)

    @property
    def n_bars(self) -> int:
        """Number of bars in the resident window."""
        if self._rust is not None:
            return int(self._rust.n_bars)  # type: ignore[attr-defined]
        return self._n

    def count_supermasks(self, masks: Sequence[int]) -> list[int]:
        """Per-mask SUPERSET count over the resident bars.

        For each ``mask`` return how many resident bars satisfy
        ``(bar & mask) == mask``. ``out[j]`` aligns with ``masks[j]``. Empty
        ``masks`` → ``[]``; empty bar window → ``0`` per mask; ``mask == 0``
        matches every bar. Only ``masks`` crosses the FFI boundary.
        """
        # The u64 Rust kernel can serve only u64-fitting masks; a u128 mask
        # routes through the two-limb numpy reference (rebuilding the limbs once
        # for this call). The fast path is the norm (today's vocab fits u64).
        if self._rust is not None and all(m <= U64_MASK for m in masks):
            return list(self._rust.count_supermasks(list(masks)))  # type: ignore[attr-defined]
        if len(masks) == 0:
            return []
        if self._n == 0:
            return [0] * len(masks)
        if self._bar_lo is None or self._bar_hi is None:
            # Rust-backed buffer asked for a u128 mask: rebuild limbs from the
            # kept raw bars once for this call (the kernel cannot serve u128).
            bar_lo, bar_hi = _split_limbs(self._raw)
        else:
            bar_lo, bar_hi = self._bar_lo, self._bar_hi
        return [_count_two_limb(bar_lo, bar_hi, m) for m in masks]

    def count_supermask(self, mask: int) -> int:
        """Single-mask SUPERSET count over the resident bars."""
        return self.count_supermasks([mask])[0]


# ─────────────────────────────────────────────────────────────────
# Combinatorial enumeration
# ─────────────────────────────────────────────────────────────────


def count_combos(k: int, n: int = VOCAB_BITS) -> int:
    """Count the number of u128 masks with exactly ``k`` of ``n`` bits set.

    Equivalent to C(n, k). Useful for sizing the sweep upfront.

    Parameters
    ----------
    k : int
        Number of required bits. ``0 <= k <= n``.
    n : int
        Vocabulary size. Default ``VOCAB_BITS`` (= 60).

    Returns
    -------
    int
        C(n, k).

    Examples
    --------
    >>> count_combos(0)
    1
    >>> count_combos(1)
    60
    >>> count_combos(2)
    1770
    >>> count_combos(3)
    34220
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
            f"count_combos: n={n} exceeds MAX_VOCAB_BITS={MAX_VOCAB_BITS} (the u128 "
            f"ceiling). The u64→u128 widening is ALREADY gated by ADR-0012 "
            f"(DEC-NIFTY-DYNAMIC-STRATEGY-001 / DEC-VOCAB-U128-CODEGEN-001 — the "
            f"DEC-PLUGIN-001-mandated mask-width gate); going past u128 would need "
            f"a further ADR."
        )
        raise ValueError(msg)
    # math.comb in Python 3.8+; integer-exact
    from math import comb

    return comb(n, k)


def iter_masks_k_of_n(k: int, n: int = VOCAB_BITS) -> Iterator[int]:
    """Yield every u128 mask with exactly ``k`` of ``n`` bits set.

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
        Vocabulary size. Default ``VOCAB_BITS`` (= 60).

    Yields
    ------
    int
        u128 masks with exactly ``k`` bits set, in lexicographic order.

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
            f"iter_masks_k_of_n: n={n} exceeds MAX_VOCAB_BITS={MAX_VOCAB_BITS} (the "
            f"u128 ceiling). The u64→u128 widening is ALREADY gated by ADR-0012 "
            f"(DEC-NIFTY-DYNAMIC-STRATEGY-001 / DEC-VOCAB-U128-CODEGEN-001 — the "
            f"DEC-PLUGIN-001-mandated mask-width gate); going past u128 would need "
            f"a further ADR."
        )
        raise ValueError(msg)
    for positions in combinations(range(n), k):
        mask = 0
        for p in positions:
            mask |= 1 << p
        yield mask


def iter_masks_k_of_bits(bits: Sequence[int], k: int) -> Iterator[int]:
    """Yield every u128 mask with exactly ``k`` of the GIVEN condition bits set.

    Generalises :func:`iter_masks_k_of_n` — which enumerates over the first ``n``
    *contiguous* bits ``0..n-1`` — to an **arbitrary subset** of condition bits, so
    a sweep can brute-force over a chosen handful of the 60 conditions (e.g. only
    the pivot + Fibonacci bits), ``k`` at a time, instead of a contiguous prefix.
    Positions are de-duplicated and sorted ascending, so the iteration order is
    deterministic (lexicographic on the sorted positions) and the total count is
    ``count_combos(k, len(set(bits)))``.

    Parameters
    ----------
    bits : Sequence[int]
        The condition bit positions to choose from, each in
        ``[0, MAX_VOCAB_BITS)``. Duplicates are ignored (set semantics).
    k : int
        Number of bits to set in each yielded mask. ``0 <= k <= len(set(bits))``.

    Yields
    ------
    int
        u128 masks with exactly ``k`` of the chosen bits set, lexicographic order.

    Raises
    ------
    ValueError
        If any position is out of range ``[0, MAX_VOCAB_BITS)``, or ``k`` is
        outside ``[0, len(set(bits))]``.

    Examples
    --------
    >>> list(iter_masks_k_of_bits([0, 3, 5], 2))
    [9, 33, 40]
    >>> list(iter_masks_k_of_bits([5, 5, 5], 1))  # duplicates ignored
    [32]
    >>> list(iter_masks_k_of_bits([7, 2], 0))
    [0]
    """
    unique = sorted(set(bits))
    for pos in unique:
        if pos < 0 or pos >= MAX_VOCAB_BITS:
            msg = f"iter_masks_k_of_bits: position {pos} out of range [0, {MAX_VOCAB_BITS})"
            raise ValueError(msg)
    n = len(unique)
    if k < 0 or k > n:
        msg = f"iter_masks_k_of_bits: invalid k={k} for {n} unique bits (require 0 <= k <= n)"
        raise ValueError(msg)
    for positions in combinations(unique, k):
        mask = 0
        for p in positions:
            mask |= 1 << p
        yield mask
