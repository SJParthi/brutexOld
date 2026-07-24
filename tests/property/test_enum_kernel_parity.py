"""Hypothesis property battery — the ranged ``enumerate_prefilter`` kernel.

DEC-SWEEP-ENUM-RUST-001 (MIGRATION.md #1738), reworked to the design-of-record
(`DESIGN_FINAL_RUST_SWEEP_CORE.md` §6 Slice 1 / §7.3): the resident
``fno_math.ResidentSweepEnum(U128)`` kernel fuses the sweep's mask-source
segment (:func:`brutex.engine.combo_mask.iter_masks_k_of_bits` + the per-mask
:func:`brutex.vocabulary.has_conflict` prune + the Stage-1B min-hits count);
this module proves it is **bit-for-bit AND order-for-order identical** to the
Python oracle for arbitrary bits/k/pair tables/bar windows/min_hits, and that
the RANGED serving is shape-invariant.

Per CLAUDE.md Golden Rule #4 the kept-mask ORDER feeds the sweep's chunk
boundaries, ``SweepComboError`` attribution and stable-sort tie-breaks — a
set-equal but order-drifted list would produce different durable bytes, so
every parity law below is a plain list ``==`` (SET ∧ ORDER in one assertion).

Invariants covered (the §6 Slice-1 battery)
-------------------------------------------

1. **List equality INCLUDING the min_hits dimension** — random bit sets × k ×
   SYNTHETIC conflict pairs (required to span members ≥ 64 / ≤ 127 — the real
   pairs all sit < 52, so the high-limb prune would otherwise be untested) ×
   random bar windows × ``min_hits`` including 0: Rust kept list == Python
   kept list exactly, plus the tallies and the in-band ``hits_counted`` law.
2. **Ranged-vs-monolithic equality** — random range/cell knob shapes
   concatenate to the identical monolithic result (the CNS unranking is
   provably stateless), with the design's cap-respected assertions from the
   return tuple's own fields (dual-knob invariance, ≥5 shapes).
3. **STRICT ``>=`` bit-parity vs count_supermasks** — the fused in-kernel
   count's keep/drop verdict at the exact ``hits >= min_hits`` boundary
   equals the retained :func:`~brutex.engine.combo_mask.count_supermasks`
   oracle on BOTH kernel widths (Verifier B F15/F18 — ``count_hits ≡
   count_supermasks``).
4. **Production-pairs wrapper parity** — arbitrary ``(bits, k, min_hits)``
   through the REAL :func:`~brutex.engine.rust_enum.enum_prefilter_via_kernel`
   driver (shadow + ranges + belts live) equals today's spine verbatim.
5. **Invalid-input contract** — an out-of-range bit / invalid k ends in the
   SAME ``ValueError`` behaviour with the flag ON as OFF (the wrapper
   pre-validates to a silent ``None``; the untouched generator raises).
6. **Dedicated edges** — bit 127 alone; the {63, 64} limb straddle;
   ``k == n_unique`` with the single mask pruned.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import fno_math
import pytest
from brutex.engine.combo_mask import count_combos, count_supermasks, iter_masks_k_of_bits
from brutex.engine.rust_enum import (
    SWEEP_RUST_ENUM_ENV,
    enum_prefilter_via_kernel,
    reset_enum_self_check_for_tests,
    reset_observability_memo_for_tests,
)
from brutex.vocabulary import has_conflict
from hypothesis import given, settings
from hypothesis import strategies as st

#: A generous per-call budget — whole-range monolithic calls in one shot.
_NO_CAP = (1 << 63) - 1

#: Arbitrary bit palettes — duplicates and unsorted order allowed (the
#: normalise law), spanning the full u128 position range.
_BITS = st.lists(st.integers(min_value=0, max_value=127), max_size=12)

#: Low-range palettes for the wrapper-level parity (production pairs live
#: below bit 52; low bits make the prune genuinely fire).
_LOW_BITS = st.lists(st.integers(min_value=0, max_value=59), max_size=10)

#: Synthetic bar windows over the full u128 value range (small — the count
#: is O(k · n_bars/64) per conflict-free mask).
_BAR_BITS = st.lists(st.integers(min_value=0, max_value=(1 << 128) - 1), max_size=24)

#: min_hits including the 0 short-circuit arm and an unreachable ceiling.
_MIN_HITS = st.sampled_from([0, 1, 2, 3, 7, 10**6])

#: Synthetic WELL-FORMED pair tables (a < b < 128) REQUIRED to include at
#: least one pair with a member ≥ 64 (the high-limb prune coverage the
#: production table cannot provide).
_HIGH_PAIR = st.tuples(
    st.integers(min_value=0, max_value=126), st.integers(min_value=64, max_value=127)
).map(lambda p: (min(p[0], p[1] - 1), p[1]) if p[0] >= p[1] else p)
_ANY_PAIR = (
    st.tuples(st.integers(min_value=0, max_value=126), st.integers(min_value=1, max_value=127))
    .filter(lambda p: p[0] != p[1])
    .map(lambda p: (min(p), max(p)))
)
_SYNTH_PAIRS = st.tuples(
    st.lists(_ANY_PAIR, max_size=6), st.lists(_HIGH_PAIR, min_size=1, max_size=3)
).map(lambda t: [*t[0], *t[1]])


def _clamped_k(bits: list[int], k_raw: int) -> int:
    return k_raw % (len(set(bits)) + 1)


def _synthetic_conflict(mask: int, pairs: list[tuple[int, int]]) -> bool:
    """The 5-line ``has_conflict`` transliteration over INJECTED pairs."""
    return any((mask >> a) & 1 and (mask >> b) & 1 for a, b in pairs)


def _oracle(
    bar_bits: list[int],
    bits: list[int],
    k: int,
    pairs: list[tuple[int, int]],
    min_hits: int,
) -> tuple[list[int], int, int, int]:
    """Today's spine over injected pairs + the count_supermasks Stage-1B:
    ``(kept, enumerated, conflict_pruned, hits_counted)``."""
    enumerated = 0
    pruned = 0
    survivors: list[int] = []
    for mask in iter_masks_k_of_bits(bits, k):
        enumerated += 1
        if _synthetic_conflict(mask, pairs):
            pruned += 1
        else:
            survivors.append(mask)
    if min_hits > 0 and survivors:
        hits = count_supermasks(bar_bits, survivors)
        kept = [m for m, h in zip(survivors, hits, strict=True) if h >= min_hits]
    else:
        kept = survivors
    hits_counted = len(survivors) if min_hits > 0 else 0
    return kept, enumerated, pruned, hits_counted


def _core(bar_bits: list[int]) -> object:
    """The u128-width resident core (serves every bit palette in one class)."""
    return fno_math.ResidentSweepEnumU128(bar_bits)


def _monolithic(
    bar_bits: list[int], bits: list[int], k: int, pairs: list[tuple[int, int]], min_hits: int
) -> tuple[int, int, int, bool, int, list[int]]:
    return _core(bar_bits).enumerate_prefilter(  # type: ignore[attr-defined]
        sorted(set(bits)), k, pairs, min_hits, 0, _NO_CAP, _NO_CAP
    )


# ────── 1. list equality × min_hits (the §6 Slice-1 property) ──────


@given(
    bar_bits=_BAR_BITS,
    bits=_BITS,
    k_raw=st.integers(min_value=0, max_value=6),
    pairs=_SYNTH_PAIRS,
    min_hits=_MIN_HITS,
)
def test_kernel_list_equality_with_min_hits(
    bar_bits: list[int],
    bits: list[int],
    k_raw: int,
    pairs: list[tuple[int, int]],
    min_hits: int,
) -> None:
    """Rust kept list == Python kept list EXACTLY (SET ∧ ORDER), with the
    tallies and the in-band hits_counted short-circuit law, over random bit
    sets × k × synthetic pairs (spanning ≥64/≤127) × bar windows × min_hits
    incl. 0 — the design's Slice-1 list-equality property verbatim."""
    k = _clamped_k(bits, k_raw)
    enumerated, pruned, next_rank, exhausted, hits_counted, kept = _monolithic(
        bar_bits, bits, k, pairs, min_hits
    )
    o_kept, o_enum, o_pruned, o_hits = _oracle(bar_bits, bits, k, pairs, min_hits)
    assert kept == o_kept
    assert (enumerated, pruned, hits_counted) == (o_enum, o_pruned, o_hits)
    assert exhausted is True
    assert next_rank == enumerated == count_combos(k, len(set(bits)))


# ────── 2. ranged-vs-monolithic + dual-knob invariance ──────


@given(
    bar_bits=st.lists(st.integers(min_value=0, max_value=(1 << 100) - 1), max_size=12),
    n=st.integers(min_value=1, max_value=9),
    k_raw=st.integers(min_value=1, max_value=4),
    pairs=_SYNTH_PAIRS,
    min_hits=st.sampled_from([0, 2]),
    max_enumerated=st.sampled_from([1, 2, 3, 7, 4096, _NO_CAP]),
    max_work_cells=st.sampled_from([0, 1, 50, 1 << 16, _NO_CAP]),
)
@settings(deadline=None)
def test_ranged_equals_monolithic_across_knob_shapes(
    bar_bits: list[int],
    n: int,
    k_raw: int,
    pairs: list[tuple[int, int]],
    min_hits: int,
    max_enumerated: int,
    max_work_cells: int,
) -> None:
    """Any (max_enumerated, max_work_cells) shape — including degenerate 0/1
    caps — concatenates to the identical monolithic sextuple, and every call
    respects the design's cap inequalities from its own return fields
    (dual-knob invariance, ≥5 shapes per knob; the guaranteed-progress law)."""
    bits = list(range(n))
    k = max(1, _clamped_k(bits, k_raw))
    mono = _monolithic(bar_bits, bits, k, pairs, min_hits)
    core = _core(bar_bits)
    n_bars = len(bar_bits)
    rank = 0
    kept: list[int] = []
    enumerated = pruned = hits = 0
    while True:
        r_enum, r_pruned, next_rank, exhausted, r_hits, r_kept = core.enumerate_prefilter(  # type: ignore[attr-defined]
            bits, k, pairs, min_hits, rank, max_enumerated, max_work_cells
        )
        # Cap-respected assertions from the return tuple's own fields (the
        # design round-4 law: caps checked AFTER each mask's work).
        assert next_rank - rank <= max(1, max_enumerated)
        assert (next_rank - rank) * n_bars <= max_work_cells + n_bars
        assert next_rank > rank or exhausted
        kept.extend(r_kept)
        enumerated += r_enum
        pruned += r_pruned
        hits += r_hits
        rank = next_rank
        if exhausted:
            break
    assert (enumerated, pruned, rank, True, hits, kept) == mono


@given(
    n=st.integers(min_value=2, max_value=10),
    k_raw=st.integers(min_value=1, max_value=4),
    split=st.integers(min_value=0, max_value=10**6),
)
def test_two_range_split_at_any_rank_concatenates(n: int, k_raw: int, split: int) -> None:
    """Splitting the rank space at ANY boundary yields the monolithic list —
    the CNS unranking resumes exactly (stateless, no cursor)."""
    bits = list(range(n))
    k = max(1, k_raw % (n + 1))
    total = count_combos(k, n)
    boundary = split % (total + 1)
    core = _core([])
    mono = core.enumerate_prefilter(bits, k, [], 0, 0, _NO_CAP, _NO_CAP)  # type: ignore[attr-defined]
    first = core.enumerate_prefilter(bits, k, [], 0, 0, max(1, boundary), _NO_CAP)  # type: ignore[attr-defined]
    second = core.enumerate_prefilter(bits, k, [], 0, first[2], _NO_CAP, _NO_CAP)  # type: ignore[attr-defined]
    assert first[5] + second[5] == mono[5]
    assert first[0] + second[0] == mono[0] == total


# ────── 3. strict->= bit-parity vs count_supermasks, BOTH widths ──────


@given(
    bar_bits=st.lists(st.integers(min_value=0, max_value=(1 << 60) - 1), max_size=32),
    positions=st.lists(st.integers(min_value=0, max_value=59), min_size=1, max_size=5),
    delta=st.sampled_from([-1, 0, 1]),
)
def test_strict_gte_parity_with_count_supermasks_u64(
    bar_bits: list[int], positions: list[int], delta: int
) -> None:
    """``count_hits ≡ count_supermasks`` at the strict ``>=`` boundary on the
    u64 width: min_hits swept over {hits−1, hits, hits+1} — kept iff
    ``hits >= min_hits`` per the RETAINED oracle (Verifier B F15/F18)."""
    unique = sorted(set(positions))
    mask = 0
    for p in unique:
        mask |= 1 << p
    hits = count_supermasks(bar_bits, [mask])[0]
    min_hits = max(1, hits + delta)
    core = fno_math.ResidentSweepEnum(bar_bits)
    _, kept = _strict_call(core, unique, min_hits)
    assert (mask in kept) == (hits >= min_hits)


@given(
    bar_bits=st.lists(st.integers(min_value=0, max_value=(1 << 128) - 1), max_size=32),
    positions=st.lists(st.integers(min_value=0, max_value=127), min_size=1, max_size=5),
    delta=st.sampled_from([-1, 0, 1]),
)
def test_strict_gte_parity_with_count_supermasks_u128(
    bar_bits: list[int], positions: list[int], delta: int
) -> None:
    """The wide-width twin: bits/bars past 64 route through the numpy
    two-limb ``count_supermasks`` reference — the SAME strict boundary."""
    unique = sorted(set(positions))
    mask = 0
    for p in unique:
        mask |= 1 << p
    hits = count_supermasks(bar_bits, [mask])[0]
    min_hits = max(1, hits + delta)
    core = fno_math.ResidentSweepEnumU128(bar_bits)
    _, kept = _strict_call(core, unique, min_hits)
    assert (mask in kept) == (hits >= min_hits)


def _strict_call(core: object, unique: list[int], min_hits: int) -> tuple[object, list[int]]:
    """One k == n_unique pair-free call — the single-mask boundary probe."""
    raw = core.enumerate_prefilter(  # type: ignore[attr-defined]
        unique, len(unique), [], min_hits, 0, _NO_CAP, _NO_CAP
    )
    return raw, raw[5]


# ────── 4. wrapper-level parity (production pairs, live shadow) ──────


@given(
    bits=_LOW_BITS,
    k_raw=st.integers(min_value=0, max_value=4),
    min_hits=st.sampled_from([0, 1, 5]),
    bar_bits=st.lists(st.integers(min_value=0, max_value=(1 << 60) - 1), max_size=16),
)
@settings(deadline=None)
def test_wrapper_matches_the_spine_with_production_pairs(
    bits: list[int], k_raw: int, min_hits: int, bar_bits: list[int]
) -> None:
    """The REAL driver (ranges + shadow + belts live) equals today's spine
    verbatim: kept (SET ∧ ORDER), enumerated, conflict_pruned, evaluated."""
    k = _clamped_k(bits, k_raw)
    os.environ[SWEEP_RUST_ENUM_ENV] = "1"
    try:
        reset_enum_self_check_for_tests()
        reset_observability_memo_for_tests()

        def count_hits(masks: Sequence[int]) -> list[int]:
            return count_supermasks(bar_bits, list(masks))

        result = enum_prefilter_via_kernel(
            bits,
            k,
            min_hits=min_hits,
            bar_bits=bar_bits,
            count_hits=count_hits,
            digest_path=True,
        )
    finally:
        os.environ.pop(SWEEP_RUST_ENUM_ENV, None)
        reset_enum_self_check_for_tests()
        reset_observability_memo_for_tests()
    assert result is not None
    survivors = [m for m in iter_masks_k_of_bits(bits, k) if not has_conflict(m)]
    if min_hits > 0 and survivors:
        hits = count_supermasks(bar_bits, survivors)
        kept = [m for m, h in zip(survivors, hits, strict=True) if h >= min_hits]
    else:
        kept = survivors
    assert result.kept == kept
    assert result.enumerated == count_combos(k, len(set(bits)))
    assert result.evaluated == len(survivors)
    assert result.conflict_pruned == result.enumerated - len(survivors)


# ────── 5. invalid-input contract is flag-invariant ──────


@given(bits=st.lists(st.integers(min_value=128, max_value=500), min_size=1, max_size=4))
def test_invalid_input_contract_is_flag_invariant(bits: list[int]) -> None:
    """Out-of-range bits: the wrapper abstains silently (None) and the
    untouched generator raises the SAME canonical ValueError either way."""
    os.environ[SWEEP_RUST_ENUM_ENV] = "1"
    try:
        reset_enum_self_check_for_tests()
        result = enum_prefilter_via_kernel(
            bits,
            1,
            min_hits=0,
            bar_bits=[],
            count_hits=lambda masks: [0] * len(masks),
            digest_path=True,
        )
    finally:
        os.environ.pop(SWEEP_RUST_ENUM_ENV, None)
        reset_enum_self_check_for_tests()
        reset_observability_memo_for_tests()
    assert result is None
    with pytest.raises(ValueError, match="position"):
        next(iter(iter_masks_k_of_bits(bits, 1)))


# ────── 6. dedicated edges ──────


def test_bit_127_alone() -> None:
    out = _monolithic([1 << 127, 0], [127], 1, [], 1)
    assert out[5] == [1 << 127]
    assert out[4] == 1  # the count executed (min_hits > 0)


def test_limb_straddle_63_64() -> None:
    bars = [(1 << 63) | (1 << 64)] * 3
    out = _monolithic(bars, [63, 64], 2, [], 3)
    assert out[5] == [(1 << 63) | (1 << 64)]
    out = _monolithic(bars, [63, 64], 2, [], 4)
    assert out[5] == []


def test_k_equals_n_unique_with_the_single_mask_pruned() -> None:
    out = _monolithic([], [10, 20], 2, [(10, 20)], 0)
    assert out[5] == []
    assert (out[0], out[1]) == (1, 1)
