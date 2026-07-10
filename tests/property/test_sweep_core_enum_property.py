"""Hypothesis list-equality properties for the Slice-1 `enumerate_prefilter`.

DESIGN_FINAL_RUST_SWEEP_CORE.md §6 (Slice 1): random bit sets × k × synthetic
conflict pairs × min_hits — the Rust kept list equals the Python spine's
enumerate → conflict-prune → Stage-1B prefilter list EXACTLY (set AND order),
on BOTH kernel widths; plus the ranged-vs-monolithic partition invariance and
the strict `hits >= min_hits` boundary parity (Verifier B F15/F18).
"""

from __future__ import annotations

import fno_math
import pytest
from brutex.engine.combo_mask import count_supermasks, iter_masks_k_of_bits
from hypothesis import given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.property


def _python_reference(
    bits: list[int],
    k: int,
    pairs: list[tuple[int, int]],
    min_hits: int,
    bar_bits: list[int],
) -> tuple[int, int, list[int]]:
    """Today's spine over synthetic pairs: (enumerated, pruned, kept)."""
    pair_masks = [(1 << a) | (1 << b) for a, b in pairs]
    enumerated = 0
    pruned = 0
    survivors: list[int] = []
    for mask in iter_masks_k_of_bits(bits, k):
        enumerated += 1
        if any(mask & pm == pm for pm in pair_masks):
            pruned += 1
            continue
        survivors.append(mask)
    kept = survivors
    if min_hits > 0 and survivors:
        hits = count_supermasks(bar_bits, survivors)
        kept = [m for m, h in zip(survivors, hits, strict=True) if h >= min_hits]
    return enumerated, pruned, kept


def _run_core(
    width: int,
    bits: list[int],
    k: int,
    pairs: list[tuple[int, int]],
    min_hits: int,
    bar_bits: list[int],
) -> tuple[int, int, int, list[int]]:
    """Drive the real kernel to exhaustion: (enumerated, pruned, hits_counted, kept)."""
    cls = fno_math.ResidentSweepCore if width == 64 else fno_math.ResidentSweepCoreU128
    n = len(bar_bits)
    core = cls([100] * n, [101] * n, [99] * n, [100] * n, [False] * n, [1] * n, bar_bits)
    rank = 0
    enumerated = 0
    pruned = 0
    hits_counted = 0
    kept: list[int] = []
    while True:
        enum_r, pruned_r, next_rank, exhausted, hits_r, masks = core.enumerate_prefilter(
            sorted(set(bits)), k, pairs, min_hits, rank, 1 << 20, 1 << 28
        )
        enumerated += enum_r
        pruned += pruned_r
        hits_counted += hits_r
        kept.extend(masks)
        rank = next_rank
        if exhausted:
            return enumerated, pruned, hits_counted, kept


@st.composite
def _enum_case(
    draw: st.DrawFn, max_bit: int
) -> tuple[list[int], int, list[tuple[int, int]], int, list[int]]:
    bits = draw(
        st.lists(st.integers(min_value=0, max_value=max_bit), min_size=1, max_size=9, unique=True)
    )
    k = draw(st.integers(min_value=0, max_value=min(4, len(bits))))
    n_pairs = draw(st.integers(min_value=0, max_value=4))
    pairs: list[tuple[int, int]] = []
    for _ in range(n_pairs):
        a = draw(st.sampled_from(bits))
        b = draw(st.sampled_from(bits))
        if a != b:
            pairs.append((min(a, b), max(a, b)))
    min_hits = draw(st.integers(min_value=0, max_value=6))
    n_bars = draw(st.integers(min_value=1, max_value=40))
    bar_bits = [
        draw(st.integers(min_value=0, max_value=(1 << (max_bit + 1)) - 1)) for _ in range(n_bars)
    ]
    return bits, k, pairs, min_hits, bar_bits


class TestListEqualityProperty:
    """Rust list == Python list exactly (order included), both widths."""

    @settings(max_examples=80, deadline=None)
    @given(case=_enum_case(max_bit=12))
    def test_u64_exact_equality(
        self, case: tuple[list[int], int, list[tuple[int, int]], int, list[int]]
    ) -> None:
        bits, k, pairs, min_hits, bar_bits = case
        enumerated, pruned, hits_counted, kept = _run_core(64, bits, k, pairs, min_hits, bar_bits)
        py_enum, py_pruned, py_kept = _python_reference(
            sorted(set(bits)), k, pairs, min_hits, bar_bits
        )
        assert enumerated == py_enum
        assert pruned == py_pruned
        assert kept == py_kept
        # §1.3 MIN-HITS SHORT-CIRCUIT LAW, in-band:
        expected_hits = 0 if min_hits == 0 else enumerated - pruned
        assert hits_counted == expected_hits

    @settings(max_examples=40, deadline=None)
    @given(case=_enum_case(max_bit=90))
    def test_u128_exact_equality(
        self, case: tuple[list[int], int, list[tuple[int, int]], int, list[int]]
    ) -> None:
        bits, k, pairs, min_hits, bar_bits = case
        enumerated, pruned, hits_counted, kept = _run_core(128, bits, k, pairs, min_hits, bar_bits)
        py_enum, py_pruned, py_kept = _python_reference(
            sorted(set(bits)), k, pairs, min_hits, bar_bits
        )
        assert (enumerated, pruned, kept) == (py_enum, py_pruned, py_kept)
        assert hits_counted == (0 if min_hits == 0 else enumerated - pruned)


class TestRangePartitionInvariance:
    """Ranged-vs-monolithic equality across random cap shapes (§3 dual-knob)."""

    @settings(max_examples=40, deadline=None)
    @given(
        case=_enum_case(max_bit=10),
        max_enumerated=st.integers(min_value=1, max_value=64),
        max_cells=st.integers(min_value=1, max_value=1 << 12),
    )
    def test_partition_reproduces_monolithic(
        self,
        case: tuple[list[int], int, list[tuple[int, int]], int, list[int]],
        max_enumerated: int,
        max_cells: int,
    ) -> None:
        bits, k, pairs, min_hits, bar_bits = case
        _, _, _, mono_kept = _run_core(64, bits, k, pairs, min_hits, bar_bits)
        n = len(bar_bits)
        core = fno_math.ResidentSweepCore(
            [100] * n, [101] * n, [99] * n, [100] * n, [False] * n, [1] * n, bar_bits
        )
        rank = 0
        kept: list[int] = []
        n_bars = len(bar_bits)
        while True:
            _, _, next_rank, exhausted, _, masks = core.enumerate_prefilter(
                sorted(set(bits)), k, pairs, min_hits, rank, max_enumerated, max_cells
            )
            # The §3 round-4 cap-respected pins, from the return's own fields:
            assert next_rank - rank <= max(1, max_enumerated)
            assert (next_rank - rank) * n_bars <= max_cells + n_bars
            kept.extend(masks)
            if exhausted:
                break
            assert next_rank > rank  # ≥1-mask progress law
            rank = next_rank
        assert kept == mono_kept


class TestStrictBoundaryParity:
    """Verifier B F15/F18: keep/drop parity at the exact `>=` boundary."""

    @settings(max_examples=60, deadline=None)
    @given(
        n_hit_bars=st.integers(min_value=0, max_value=12),
        n_miss_bars=st.integers(min_value=0, max_value=12),
    )
    def test_boundary_keep_and_drop(self, n_hit_bars: int, n_miss_bars: int) -> None:
        # One 2-bit mask with EXACTLY n_hit_bars supermask hits; probe
        # min_hits == hits (KEPT in both arms) and hits + 1 (DROPPED in both).
        bits = [0, 1]
        bar_bits = [0b11] * n_hit_bars + [0b01] * n_miss_bars
        if not bar_bits:
            bar_bits = [0b00]
            n_hit_bars = 0
        for min_hits, expect_kept in [
            (max(1, n_hit_bars), n_hit_bars >= max(1, n_hit_bars)),
            (n_hit_bars + 1, False),
        ]:
            _, _, _, kept = _run_core(64, bits, 2, [], min_hits, bar_bits)
            py = _python_reference(bits, 2, [], min_hits, bar_bits)[2]
            assert kept == py
            assert (0b11 in kept) == expect_kept
