"""Hypothesis property tests — Rust ``sweep_enum_conflict_free`` parity.

DEC-SWEEP-ENUM-RUST-001 (MIGRATION.md #1738). The Rust kernel
``fno_math.sweep_enum_conflict_free`` fuses the sweep's mask-source segment
(:func:`brutex.engine.combo_mask.iter_masks_k_of_bits` + the per-mask
:func:`brutex.vocabulary.has_conflict` prune); this module proves it is
**bit-for-bit AND order-for-order identical** to the Python oracle for
arbitrary bits/k/pair tables.

Per CLAUDE.md Golden Rule #4 the survivor ORDER feeds the sweep's chunk
boundaries, ``SweepComboError`` attribution and stable-sort tie-breaks — a
set-equal but order-drifted list would produce different durable bytes, so
every parity law below is a plain list ``==`` (SET ∧ ORDER in one
assertion).

Invariants covered
------------------

1. **Production-pairs parity** — arbitrary ``(bits, k)`` with the REAL
   ``sorted(CONFLICT_PAIRS)`` table: kernel survivors == the filtered Python
   generator, ``enumerated == count_combos``, sum reconciliation.
2. **Pairs-as-data round-trip (FM-5)** — SYNTHETIC well-formed pair tables
   (REQUIRED to span bits ≥ 64 / 127 — the real pairs all sit < 52, so the
   high-limb prune would otherwise be untested, FM-3) against a 5-line
   in-test ``has_conflict`` transliteration over the injected pairs: nothing
   about the production table is baked into Rust — adding/removing a pair
   changes survivors exactly as the Python predicate says.
3. **Invalid-input contract (FM-13)** — an out-of-range bit / invalid k ends
   in the SAME ``ValueError`` behaviour with the flag ON as OFF (the wrapper
   pre-validates to a silent ``None`` and the untouched generator raises).
4. **Dedicated edges** — bit 127 alone; the {63, 64} limb straddle;
   ``k == n_unique`` with the single mask pruned.
"""

from __future__ import annotations

import os

import fno_math
import pytest
from brutex.engine.combo_mask import count_combos, iter_masks_k_of_bits
from brutex.engine.rust_enum import (
    ENUM_KERNEL_MAX_MASKS,
    SWEEP_RUST_ENUM_ENV,
    enum_survivors_via_kernel,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
)
from brutex.vocabulary import CONFLICT_PAIRS, has_conflict
from hypothesis import given, settings
from hypothesis import strategies as st

_PAIRS_SORTED = sorted(CONFLICT_PAIRS)

#: Arbitrary bit palettes — duplicates and unsorted order allowed (the
#: normalise law), spanning the full u128 position range.
_BITS = st.lists(st.integers(min_value=0, max_value=127), max_size=14)

#: Synthetic WELL-FORMED pair tables (a < b < 128) REQUIRED to include at
#: least one pair with a member ≥ 64 (FM-3 — the high-limb prune coverage the
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


def _kernel(bits: list[int], k: int, pairs: list[tuple[int, int]]) -> tuple[list[int], int, int]:
    return fno_math.sweep_enum_conflict_free(bits, k, pairs, ENUM_KERNEL_MAX_MASKS)


@given(bits=_BITS, k_raw=st.integers(min_value=0, max_value=16))
def test_kernel_matches_oracle_with_production_pairs(bits: list[int], k_raw: int) -> None:
    """Kernel survivors == the filtered generator — SET ∧ ORDER — over the
    REAL conflict-pair table, plus the two counter laws."""
    k = _clamped_k(bits, k_raw)
    survivors, enumerated, pruned = _kernel(bits, k, _PAIRS_SORTED)
    oracle = [m for m in iter_masks_k_of_bits(bits, k) if not has_conflict(m)]
    assert survivors == oracle
    assert enumerated == count_combos(k, len(set(bits)))
    assert enumerated == len(survivors) + pruned


@given(bits=_BITS, k_raw=st.integers(min_value=0, max_value=16), pairs=_SYNTH_PAIRS)
def test_pairs_are_data_synthetic_tables_round_trip(
    bits: list[int], k_raw: int, pairs: list[tuple[int, int]]
) -> None:
    """FM-5/FM-3: synthetic pair tables spanning bits ≥ 64 drive the prune
    exactly as the Python predicate over the SAME injected pairs says —
    nothing about ``CONFLICT_PAIRS`` is baked into the kernel."""
    k = _clamped_k(bits, k_raw)
    survivors, enumerated, pruned = _kernel(bits, k, pairs)
    oracle = [m for m in iter_masks_k_of_bits(bits, k) if not _synthetic_conflict(m, pairs)]
    assert survivors == oracle
    assert enumerated == count_combos(k, len(set(bits)))
    assert enumerated == len(survivors) + pruned


@given(bits=_BITS, k_raw=st.integers(min_value=0, max_value=16), pair=_ANY_PAIR)
def test_adding_one_pair_prunes_exactly_the_predicted_masks(
    bits: list[int], k_raw: int, pair: tuple[int, int]
) -> None:
    """The pairs-as-data ROUND-TRIP: adding one pair to an empty table drops
    exactly the masks the Python predicate flags — and removing it restores
    them (the no-table run is the union)."""
    k = _clamped_k(bits, k_raw)
    with_none, enumerated, _ = _kernel(bits, k, [])
    with_pair, _, pruned = _kernel(bits, k, [pair])
    predicted = [m for m in with_none if not _synthetic_conflict(m, [pair])]
    assert with_pair == predicted
    assert pruned == len(with_none) - len(with_pair)
    assert enumerated == count_combos(k, len(set(bits)))


@settings(max_examples=25)
@given(
    bits=st.lists(st.integers(min_value=0, max_value=200), min_size=1, max_size=8),
    k_raw=st.integers(min_value=0, max_value=10),
)
def test_invalid_input_contract_is_flag_invariant(bits: list[int], k_raw: int) -> None:
    """FM-13: for a bit ∉ [0, 128) or k > n_unique, flag ON ends in the SAME
    ``ValueError`` as OFF — the wrapper abstains silently and the untouched
    generator owns the canonical raise. (Env managed by hand — a
    function-scoped monkeypatch does not compose with ``@given``.)
    """
    invalid = any(b >= 128 for b in bits) or k_raw > len(set(bits))
    if not invalid:
        bits = [*bits, 128]  # force the out-of-range arm deterministically
    prior = os.environ.get(SWEEP_RUST_ENUM_ENV)
    os.environ[SWEEP_RUST_ENUM_ENV] = "1"
    try:
        reset_observability_memo_for_tests()
        reset_self_check_for_tests()
        assert enum_survivors_via_kernel(bits, k_raw) is None
        with pytest.raises(ValueError, match="iter_masks_k_of_bits") as on_exc:
            next(iter_masks_k_of_bits(bits, k_raw))
        del os.environ[SWEEP_RUST_ENUM_ENV]
        with pytest.raises(ValueError, match="iter_masks_k_of_bits") as off_exc:
            next(iter_masks_k_of_bits(bits, k_raw))
        assert str(on_exc.value) == str(off_exc.value)
    finally:
        if prior is None:
            os.environ.pop(SWEEP_RUST_ENUM_ENV, None)
        else:
            os.environ[SWEEP_RUST_ENUM_ENV] = prior


# ── dedicated examples (the spec's named edges) ───────────────────────────


def test_bit_127_alone() -> None:
    survivors, enumerated, pruned = _kernel([127], 1, _PAIRS_SORTED)
    assert survivors == [1 << 127]
    assert (enumerated, pruned) == (1, 0)


def test_limb_straddle_63_64() -> None:
    survivors, enumerated, pruned = _kernel([63, 64], 2, _PAIRS_SORTED)
    assert survivors == [(1 << 63) | (1 << 64)]
    assert (enumerated, pruned) == (1, 0)
    # A synthetic pair straddling the limb boundary prunes it.
    survivors, _, pruned = _kernel([63, 64], 2, [(63, 64)])
    assert survivors == []
    assert pruned == 1


def test_k_equals_n_unique_with_the_single_mask_pruned() -> None:
    # Bits {0, 1} carry the REAL (0, 1) conflict pair: the single k=2 mask
    # is pruned — empty survivors, enumerated 1, pruned 1.
    survivors, enumerated, pruned = _kernel([0, 1], 2, _PAIRS_SORTED)
    assert survivors == []
    assert (enumerated, pruned) == (1, 1)
