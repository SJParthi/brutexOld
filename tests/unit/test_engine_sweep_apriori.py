"""Unit tests for the Apriori pruned sweep candidate generator.

DEC-SWEEP-APRIORI-TUPLE-001. The generator
(:func:`brutex.engine.sweep_apriori.generate_apriori_candidates`) is a
default-OFF, drop-in replacement for the brute-force
:func:`brutex.engine.combo_mask.iter_masks_k_of_bits` enumeration whose
Stage-1B-kept set is provably identical (byte-identical leaderboard). These
tests pin:

* the ORACLE parity — for every ``k`` and a range of ``min_hits``, the
  Apriori candidates re-filtered by ``count_supermasks >= min_hits`` equal the
  brute-force frequent set (conflict-free ∩ ``hits >= min_hits``);
* the EMISSION order — a subsequence of the brute-force enumeration
  (lex-by-position);
* determinism / purity, the ``root_mod`` shard union-and-disjointness, the
  deterministic pure-integer mask·bar breaker (strict ``>``), the env resolver,
  the ``<64``-bit guard, and the ``k >= 3`` conflict-freeness that subset-prune
  guarantees for free.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
from brutex.engine import sweep_apriori
from brutex.engine.combo_mask import count_combos, count_supermasks, iter_masks_k_of_bits
from brutex.engine.sweep_apriori import (
    DEFAULT_MAX_MASK_BARS,
    MAX_MASK_BARS_ENV,
    SWEEP_APRIORI_ENV,
    apriori_enabled,
    effective_max_combos,
    generate_apriori_candidates,
    resolve_max_mask_bars,
)
from brutex.engine.sweep_run import (
    SweepAprioriBudgetError,
    SweepAprioriUnsupportedError,
    SweepComboError,
)
from brutex.vocabulary.conflicts import CONFLICT_PAIRS, has_conflict

# A bit set that INCLUDES three real conflict pairs — (0, 1), (2, 3), (4, 5) —
# plus two conflict-free bits, so conflict-pruning is genuinely exercised.
_BITS = [0, 1, 2, 3, 4, 5, 6, 7]


def _synthetic_bar_bits(seed: int, n: int = 500, width: int = 8) -> list[int]:
    """Deterministic per-bar packed bitsets over ``[0, 2**width)``."""
    rng = np.random.default_rng(seed)
    return [int(x) for x in rng.integers(0, 1 << width, size=n)]


def _brute_frequent(bar_bits: list[int], bits: list[int], k: int, min_hits: int) -> set[int]:
    """The brute-force frequent set: conflict-free ``k``-combos with hits >= min_hits."""
    out: set[int] = set()
    for mask in iter_masks_k_of_bits(bits, k):
        if has_conflict(mask):
            continue
        if count_supermasks(bar_bits, [mask])[0] >= min_hits:
            out.add(mask)
    return out


def _apriori_kept(bar_bits: list[int], bits: list[int], k: int, min_hits: int) -> set[int]:
    """The Apriori candidates re-filtered by Stage-1B (``hits >= min_hits``)."""
    cands = generate_apriori_candidates(
        bar_bits, condition_bits=bits, k=k, min_hits=min_hits, n_bars=len(bar_bits)
    )
    if not cands:
        return set()
    hits = count_supermasks(bar_bits, cands)
    return {mask for mask, hit in zip(cands, hits, strict=True) if hit >= min_hits}


# ────────────────────────── oracle parity ──────────────────────────


@pytest.mark.parametrize("seed", [1, 7, 99])
@pytest.mark.parametrize("min_hits", [0, 100, 250, 400])
def test_oracle_kept_set_matches_brute_force(seed: int, min_hits: int) -> None:
    bar_bits = _synthetic_bar_bits(seed)
    for k in range(len(_BITS) + 1):
        assert _apriori_kept(bar_bits, _BITS, k, min_hits) == _brute_frequent(
            bar_bits, _BITS, k, min_hits
        )


def test_k0_is_the_empty_mask() -> None:
    assert generate_apriori_candidates([1, 2, 3], condition_bits=_BITS, k=0, min_hits=0) == [0]


def test_k1_is_all_conflict_free_singletons_not_hit_filtered() -> None:
    # min_hits huge → no singleton is "frequent", but k==1 is NOT hit-filtered
    # (Stage-1B does that downstream), so every singleton is still emitted.
    bar_bits = _synthetic_bar_bits(1)
    got = generate_apriori_candidates(
        bar_bits, condition_bits=_BITS, k=1, min_hits=10_000, n_bars=len(bar_bits)
    )
    assert got == [1 << b for b in _BITS]


def test_high_min_hits_yields_empty_frontier() -> None:
    bar_bits = _synthetic_bar_bits(1)
    # No singleton fires on all 500 bars, so F_1 is empty and every k>=2 level
    # collapses to nothing.
    for k in (2, 3, 4):
        assert (
            generate_apriori_candidates(
                bar_bits, condition_bits=_BITS, k=k, min_hits=10_000, n_bars=len(bar_bits)
            )
            == []
        )


# ────────────────────────── emission order ──────────────────────────


@pytest.mark.parametrize("seed", [1, 7])
@pytest.mark.parametrize("min_hits", [0, 200])
def test_emission_is_iter_order_subsequence(seed: int, min_hits: int) -> None:
    bar_bits = _synthetic_bar_bits(seed)
    for k in range(len(_BITS) + 1):
        out = generate_apriori_candidates(
            bar_bits, condition_bits=_BITS, k=k, min_hits=min_hits, n_bars=len(bar_bits)
        )
        got_set = set(out)
        # The output must appear in the SAME order the brute-force enumeration
        # yields it (lex-by-position) — a literal subsequence.
        expected = [m for m in iter_masks_k_of_bits(_BITS, k) if m in got_set]
        assert out == expected


def test_lex_order_differs_from_int_order_but_matches_iter() -> None:
    # Interleaved bits where lex-by-position != ascending-by-int: (0,3)->9 sorts
    # before (1,2)->6 lexicographically but after it numerically. Bits 0-3 carry
    # the conflict pairs (0,1)/(2,3), so the expected value is the conflict-free
    # subsequence of the brute-force enumeration.
    bar_bits = [0b1111] * 40  # every mask fires on all bars → whole C_2 kept
    out = generate_apriori_candidates(bar_bits, condition_bits=[0, 1, 2, 3], k=2, min_hits=0)
    expected = [m for m in iter_masks_k_of_bits([0, 1, 2, 3], 2) if not has_conflict(m)]
    assert out == expected  # lex-by-position: [5, 9, 6, 10]
    assert out != sorted(out)  # NOT ascending-by-int (9 precedes 6)


# ────────────────────────── determinism / purity ──────────────────────────


def test_determinism_and_permutation_invariance() -> None:
    bar_bits = _synthetic_bar_bits(3)
    a = generate_apriori_candidates(bar_bits, condition_bits=_BITS, k=3, min_hits=150)
    b = generate_apriori_candidates(bar_bits, condition_bits=_BITS, k=3, min_hits=150)
    assert a == b
    # Permuting the input condition_bits (set semantics) yields the identical list.
    permuted = generate_apriori_candidates(
        bar_bits, condition_bits=list(reversed(_BITS)), k=3, min_hits=150
    )
    assert permuted == a


def test_dedups_condition_bits() -> None:
    bar_bits = _synthetic_bar_bits(3)
    assert generate_apriori_candidates(
        bar_bits, condition_bits=[6, 6, 7, 7], k=2, min_hits=0
    ) == generate_apriori_candidates(bar_bits, condition_bits=[6, 7], k=2, min_hits=0)


def test_default_counter_matches_injected() -> None:
    bar_bits = _synthetic_bar_bits(5)

    def injected(masks):  # type: ignore[no-untyped-def]
        return count_supermasks(bar_bits, masks)

    default = generate_apriori_candidates(
        bar_bits, condition_bits=_BITS, k=3, min_hits=180, n_bars=len(bar_bits)
    )
    with_injected = generate_apriori_candidates(
        bar_bits,
        condition_bits=_BITS,
        k=3,
        min_hits=180,
        count_hits=injected,
        n_bars=len(bar_bits),
    )
    assert default == with_injected


def test_n_bars_defaults_to_len_bar_bits() -> None:
    bar_bits = _synthetic_bar_bits(5, n=42)
    # A tiny mask·bar cap with the default n_bars (=42) still trips → proves n_bars
    # defaulted to len(bar_bits) (=42; count 8 × 42 = 336 > 1), not 0 (8 × 0 = 0,
    # which would never trip).
    with pytest.raises(SweepAprioriBudgetError):
        generate_apriori_candidates(
            bar_bits, condition_bits=_BITS, k=1, min_hits=0, max_mask_bars=1
        )


# ────────────────────────── conflict-freeness ──────────────────────────


@pytest.mark.parametrize("seed", [1, 7, 99])
@pytest.mark.parametrize("k", [2, 3, 4, 5])
def test_candidates_are_conflict_free(seed: int, k: int) -> None:
    bar_bits = _synthetic_bar_bits(seed)
    cands = generate_apriori_candidates(
        bar_bits, condition_bits=_BITS, k=k, min_hits=0, n_bars=len(bar_bits)
    )
    assert not any(has_conflict(m) for m in cands)


def test_conflict_pair_dropped_at_level_two() -> None:
    # Bits [0, 1] are a conflict pair; both singletons frequent, so the ONLY
    # level-2 candidate (0, 1) exists but is conflict-dropped → empty.
    assert (0, 1) in CONFLICT_PAIRS
    bar_bits = [0b11] * 50  # both bits fire on every bar
    assert generate_apriori_candidates(bar_bits, condition_bits=[0, 1], k=2, min_hits=0) == []


def test_subset_prune_drops_triple_with_infrequent_pair() -> None:
    # bits 6, 7 fire on every bar; bit 2 fires on only a few — so the pairs
    # (2,6) and (2,7) are INfrequent and the triple (2,6,7) must be subset-pruned
    # even though it is conflict-free.
    bar_bits = [0b11000000] * 100 + [0b11000100] * 3  # 6,7 always; 2 rarely
    k, min_hits = 3, 10
    cands = generate_apriori_candidates(
        bar_bits, condition_bits=[2, 6, 7], k=k, min_hits=min_hits, n_bars=len(bar_bits)
    )
    triple = (1 << 2) | (1 << 6) | (1 << 7)
    assert triple not in cands
    # Every candidate is a real conflict-free k-combo of the chosen bits.
    assert set(cands) <= set(iter_masks_k_of_bits([2, 6, 7], k))


# ────────────────────────── root_mod sharding ──────────────────────────


@pytest.mark.parametrize("big_w", [1, 2, 3, 7])
@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_shards_union_to_whole_and_are_disjoint(big_w: int, k: int) -> None:
    bar_bits = _synthetic_bar_bits(11)
    whole = set(
        generate_apriori_candidates(
            bar_bits, condition_bits=_BITS, k=k, min_hits=0, n_bars=len(bar_bits)
        )
    )
    shards = [
        set(
            generate_apriori_candidates(
                bar_bits,
                condition_bits=_BITS,
                k=k,
                min_hits=0,
                n_bars=len(bar_bits),
                root_mod=(w, big_w),
            )
        )
        for w in range(big_w)
    ]
    union: set[int] = set()
    for shard in shards:
        union |= shard
    assert union == whole
    for i in range(big_w):
        for j in range(i + 1, big_w):
            assert not (shards[i] & shards[j])


def test_k0_owned_only_by_shard_zero() -> None:
    assert generate_apriori_candidates(
        [1], condition_bits=_BITS, k=0, min_hits=0, root_mod=(0, 3)
    ) == [0]
    assert (
        generate_apriori_candidates([1], condition_bits=_BITS, k=0, min_hits=0, root_mod=(1, 3))
        == []
    )


# ────────────────────────── the breaker ──────────────────────────


def test_breaker_raises_named_sweepcomboerror() -> None:
    bar_bits = _synthetic_bar_bits(1, n=1000)
    with pytest.raises(SweepAprioriBudgetError) as ei:
        generate_apriori_candidates(
            bar_bits,
            condition_bits=_BITS,
            k=2,
            min_hits=0,
            n_bars=len(bar_bits),
            max_mask_bars=1,
            cell_label="NIFTY:1m:bull:k=2",
        )
    err = ei.value
    assert isinstance(err, SweepComboError)  # caught by the driver TUPLE_FAILED path
    msg = str(err)
    assert "NIFTY:1m:bull:k=2" in msg
    assert "k=2" in msg
    assert "1000 bars" in msg
    assert "mask·bar" in msg
    assert MAX_MASK_BARS_ENV in msg
    assert SWEEP_APRIORI_ENV in msg


def test_breaker_trips_before_materialising_giant_list(monkeypatch: pytest.MonkeyPatch) -> None:
    # A tiny stride makes the incremental in-join breaker fire early. With a
    # generous mask·bar cap the incremental check simply EXECUTES (covering the
    # modulo branch); with a tiny cap it RAISES during accumulation.
    monkeypatch.setattr(sweep_apriori, "_BUDGET_CHECK_STRIDE", 4)
    bar_bits = [0b1111111111] * 20  # dense: many level-2 candidates
    bits = list(range(10))  # no conflicts among these (6..) but includes pairs; C(10,2)=45
    # generous cap → no raise, incremental branch executed
    out = generate_apriori_candidates(
        bar_bits, condition_bits=bits, k=2, min_hits=0, n_bars=len(bar_bits), max_mask_bars=10**18
    )
    assert len(out) > 4  # crossed the stride at least once
    # tiny cap → raises mid-accumulation
    with pytest.raises(SweepAprioriBudgetError):
        generate_apriori_candidates(
            bar_bits,
            condition_bits=bits,
            k=2,
            min_hits=0,
            n_bars=len(bar_bits),
            max_mask_bars=1,
        )


# ────────────────────────── stride invariance ──────────────────────────
# THE anti-regression pin for DEC-SWEEP-APRIORI-TUPLE-001: the breaker trip
# DECISION is a pure function of the SETTLED RAW per-level candidate counts
# ``|C_j|`` and is INDEPENDENT of ``_BUDGET_CHECK_STRIDE`` (Golden Rule #7 —
# Python↔Rust failure-set bit-identity). This test FAILS on the pre-fix code,
# where an intermediate level checked the hit-filtered ``|F_j|`` (not the raw
# ``|C_j|``) so a degenerate cell tripped at small stride only.

#: ``(label, bar_bits, condition_bits, k, min_hits, n_bars, max_mask_bars)``. A
#: fuzz of degenerate + dense cells straddling the budget from both sides
#: (some raise, some don't) so the raising SET is a genuine non-trivial subset.
#:
#: The minimal repro ``repro_12singlebit_k3`` — 12 single-bit bars, ``k=3``,
#: ``min_hits=1`` — has ``|C_2| = C(12, 2) − 3 conflict pairs = 63`` (so
#: ``63 × 12 = 756 > 600`` trips on the RAW ``|C_j|``), but NO pair co-occurs on
#: any single-bit bar, so ``|F_2| = 0`` and a ``|F_j|``-based check would NOT
#: trip. On the pre-fix a00ed1a it therefore raises at ``stride=1`` (raw ``|C_j|``
#: caught by the incremental check) but NOT at ``stride=100`` (the post-loop check
#: sees the under-budget ``|F_j|``), so the raising set differs across strides and
#: this test fails; after the fix every stride raises it.
_STRIDE_INVARIANCE_CELLS: list[tuple[str, list[int], list[int], int, int, int, int]] = [
    ("repro_12singlebit_k3", [1 << i for i in range(12)], list(range(12)), 3, 1, 12, 600),
    ("repro_10singlebit_k3", [1 << i for i in range(10)], list(range(10)), 3, 1, 10, 200),
    ("repro_10singlebit_k4", [1 << i for i in range(10)], list(range(10)), 4, 1, 10, 5000),
    ("loose_never_trips", [1 << i for i in range(12)], list(range(12)), 3, 1, 12, 10**18),
    ("dense_k2_final_trips", [0b11111111] * 20, [0, 2, 4, 6, 7], 2, 0, 20, 1),
    ("dense_k3_intermediate", [0b111111000000] * 15, [6, 7, 8, 9, 10, 11], 3, 0, 15, 10),
    ("dense_k3_final_only", [0b111111000000] * 15, [6, 7, 8, 9, 10, 11], 3, 0, 15, 250),
    ("dense_k3_generous", [0b111111000000] * 15, [6, 7, 8, 9, 10, 11], 3, 0, 15, 10**18),
]

#: The stride values swept — {1, 2, 100, 65536 (the production literal), a huge
#: value}. Small strides fire the incremental check often (catching the raw
#: ``|C_j|`` early); strides ``>= |C_j|`` never fire it (only the authoritative
#: post-join checks decide) — the two must reach the IDENTICAL trip verdict.
_STRIDE_INVARIANCE_STRIDES: list[int] = [1, 2, 100, 65_536, 10**9]

#: The cells expected to raise (independently derived from the raw ``|C_j|``
#: budget arithmetic in the docstrings above), pinned so an accidental "never
#: raises at any stride" regression (a vacuously-invariant empty set) is caught.
_STRIDE_INVARIANCE_EXPECTED_RAISERS: frozenset[str] = frozenset(
    {
        "repro_12singlebit_k3",
        "repro_10singlebit_k3",
        "dense_k2_final_trips",
        "dense_k3_intermediate",
        "dense_k3_final_only",
    }
)


def _cell_raises(cell: tuple[str, list[int], list[int], int, int, int, int]) -> bool:
    _label, bar_bits, bits, k, min_hits, n_bars, cap = cell
    try:
        generate_apriori_candidates(
            bar_bits,
            condition_bits=bits,
            k=k,
            min_hits=min_hits,
            n_bars=n_bars,
            max_mask_bars=cap,
        )
        return False
    except SweepAprioriBudgetError:
        return True


def test_budget_check_stride_invariance(monkeypatch: pytest.MonkeyPatch) -> None:
    # The SET of cells that raise the breaker MUST be identical across every
    # _BUDGET_CHECK_STRIDE — the trip decision is a pure function of the settled
    # raw per-level |C_j|, never of the stride. FAILS on the pre-fix a00ed1a
    # (where the minimal-repro cells raise at small stride but not large).
    raisers_by_stride: dict[int, frozenset[str]] = {}
    for stride in _STRIDE_INVARIANCE_STRIDES:
        monkeypatch.setattr(sweep_apriori, "_BUDGET_CHECK_STRIDE", stride)
        raisers_by_stride[stride] = frozenset(
            cell[0] for cell in _STRIDE_INVARIANCE_CELLS if _cell_raises(cell)
        )

    # Every stride agrees on the raising set (the invariance property).
    reference = raisers_by_stride[_STRIDE_INVARIANCE_STRIDES[0]]
    for stride, raisers in raisers_by_stride.items():
        assert raisers == reference, (
            f"stride {stride} raised {sorted(raisers)} but stride "
            f"{_STRIDE_INVARIANCE_STRIDES[0]} raised {sorted(reference)} — "
            f"the trip decision is stride-DEPENDENT"
        )
    # …and that shared set is exactly the raw-|C_j|-derived expectation (not a
    # vacuous empty set that would make the invariance trivially true).
    assert reference == _STRIDE_INVARIANCE_EXPECTED_RAISERS


def test_minimal_repro_cell_raises_at_every_stride(monkeypatch: pytest.MonkeyPatch) -> None:
    # Explicit: the confirmed minimal repro (12 single-bit bars, k=3, min_hits=1,
    # n_bars=12, max_mask_bars=600 → raw |C_2| = 63, 63 × 12 = 756 > 600) raises
    # at ALL stride values after the fix (on a00ed1a it raised only at stride < 63).
    repro = _STRIDE_INVARIANCE_CELLS[0]
    assert repro[0] == "repro_12singlebit_k3"
    for stride in _STRIDE_INVARIANCE_STRIDES:
        monkeypatch.setattr(sweep_apriori, "_BUDGET_CHECK_STRIDE", stride)
        with pytest.raises(SweepAprioriBudgetError):
            generate_apriori_candidates(
                [1 << i for i in range(12)],
                condition_bits=list(range(12)),
                k=3,
                min_hits=1,
                n_bars=12,
                max_mask_bars=600,
            )


# ────────────────────────── shard-trip-invariance ──────────────────────────
# THE k=1 anti-regression pin for DEC-SWEEP-APRIORI-TUPLE-001: the breaker trip
# DECISION at EVERY level (k=1 AND k>=2) is a pure function of the WHOLE RAW
# pre-shard per-level ``|C_j|`` and is INDEPENDENT of the ``root_mod`` shard —
# every ``(w, big_w)`` shard of a cell reaches the IDENTICAL trip verdict
# (Golden Rule #7, Python<->Rust failure-set bit-identity). This test FAILS on
# the pre-fix k==1 branch, which checked the SHARD-OWNED singleton count
# ``len(owned)`` instead of the whole ``|C_1|``, so a k=1 cell tripped only on
# the shards that happened to own an over-budget singleton.

#: ``(label, bar_bits, condition_bits, k, min_hits, n_bars, max_mask_bars)`` — a
#: fuzz of tripping cells spanning k=1 AND k>=2. The k=1 repro
#: ``k1_singlebit_over`` (condition_bits=[10], k=1, min_hits=5, n_bars=8,
#: max_mask_bars=5) has whole ``|C_1| = 1`` -> ``1 x 8 = 8 > 5`` so it MUST trip
#: for EVERY shard; on the pre-fix ``len(owned)`` check it tripped only where
#: ``10 % big_w == w`` (shard-DEPENDENT). The k>=2 cells (already whole-|C_k|
#: correct before this fix) are the "k>=2 was already right" span.
_SHARD_TRIP_CELLS: list[tuple[str, list[int], list[int], int, int, int, int]] = [
    ("k1_singlebit_over", [1 << 10], [10], 1, 5, 8, 5),  # THE named k=1 repro
    ("k1_multibit_over", [1 << i for i in range(6)], list(range(6)), 1, 0, 8, 5),
    ("k2_dense_final", [0b11111111] * 20, [0, 2, 4, 6, 7], 2, 0, 20, 1),
    ("k3_singlebit_raw", [1 << i for i in range(12)], list(range(12)), 3, 1, 12, 600),
]


def _cell_raises_shard(
    cell: tuple[str, list[int], list[int], int, int, int, int], root_mod: tuple[int, int]
) -> bool:
    _label, bar_bits, bits, k, min_hits, n_bars, cap = cell
    try:
        generate_apriori_candidates(
            bar_bits,
            condition_bits=bits,
            k=k,
            min_hits=min_hits,
            n_bars=n_bars,
            max_mask_bars=cap,
            root_mod=root_mod,
        )
        return False
    except SweepAprioriBudgetError:
        return True


def test_breaker_trip_is_shard_invariant() -> None:
    # For every fuzz cell the trip decision MUST be IDENTICAL across every
    # root_mod shard (w, big_w) for big_w in {1, 2, 3, 4} and all w in
    # 0..big_w-1 — spanning k=1 AND k>=2. FAILS on the pre-fix k==1 branch
    # (len(owned)), where the k1_singlebit_over cell trips only on the shards
    # that own bit 10.
    for cell in _SHARD_TRIP_CELLS:
        verdicts = {
            (w, big_w): _cell_raises_shard(cell, (w, big_w))
            for big_w in (1, 2, 3, 4)
            for w in range(big_w)
        }
        assert len(set(verdicts.values())) == 1, (
            f"cell {cell[0]}: trip decision is shard-DEPENDENT — "
            f"tripped {sorted(s for s, v in verdicts.items() if v)} but not "
            f"{sorted(s for s, v in verdicts.items() if not v)}"
        )
    # …and every fuzz cell is a genuine RAISER (not a vacuous never-trip set that
    # would make the invariance trivially true) — checked on the whole cell.
    for cell in _SHARD_TRIP_CELLS:
        assert _cell_raises_shard(cell, (0, 1)), f"{cell[0]} expected to trip"


def test_k1_over_budget_trips_for_every_shard() -> None:
    # Explicit named repro: whole |C_1| = 1 singleton, 1 x 8 = 8 > 5, so the
    # breaker trips for EVERY shard regardless of which shard owns bit 10. On the
    # pre-fix len(owned) check only shards where 10 % big_w == w tripped.
    for big_w in (1, 2, 3, 4):
        for w in range(big_w):
            with pytest.raises(SweepAprioriBudgetError):
                generate_apriori_candidates(
                    [1 << 10],
                    condition_bits=[10],
                    k=1,
                    min_hits=5,
                    n_bars=8,
                    max_mask_bars=5,
                    root_mod=(w, big_w),
                )


# ────────────────────────── mutation hardening ──────────────────────────
# Deterministic boundary pins that KILL the mutmut survivors on the hit-filter
# `>=` boundaries, the breaker's strict `>`, and the incremental-check stride
# `% == 0` — each fails under exactly the single-line mutant it targets.


class TestMutationHardening:
    """Kill EVERY surviving mutmut mutant on ``sweep_apriori.py`` (zero-survivor).

    mutmut CAN run when scoped to this single file
    (``mutmut run --paths-to-mutate brutex/engine/sweep_apriori.py --runner
    "python3.12 -m pytest tests/unit/test_engine_sweep_apriori.py …"``) — parso
    only parses the ``--paths-to-mutate`` file, so the repo's PEP-695 generics
    elsewhere are irrelevant and the test-runner subprocess is plain pytest. The
    module is wired into ``mutation.yml``'s per-module zero-survivor loop, so the
    de-survivor round below (DEC-SWEEP-APRIORI-TUPLE-001) drove the measured
    survivor set to ZERO: string-wrap (``"foo"``→``"XXfooXX"``) mutants on env-var
    names / log-event names / error-message segments are killed by literal-value,
    ``capture_logs`` event-name, and EXACT-message assertions; the numeric-default
    constants by literal pins; the ``n_bars < 0`` boundary by an ``n_bars == 0``
    allowed pin; the breaker's ``count * n_bars`` by a straddle case; the two
    injected-counter ``strict=True`` zips by short-counter contract pins; the final
    shard filter ``tpl[0] % W`` (lowest set bit) by an exact-shard-set pin. Three
    genuinely-equivalent / uncompilable mutants were eliminated at the SOURCE
    instead (never masked): the ``*prefix`` unpacking → tuple concatenation (its
    ``*``→``/`` mutant is a bare SyntaxError mutmut mis-reports as survived), the
    ``apriori_enabled`` string default removed, and the always-equal-length k==1
    zip replaced by a direct comprehension — leaving exactly ONE justified
    ``# pragma: no mutate`` (the ``_BUDGET_CHECK_STRIDE`` ±1, GENUINELY
    result-equivalent because the trip decision is a pure function of the settled
    raw per-level candidate counts ``|C_j|`` — a strided fire is only a
    strictly-earlier raise of the SAME authoritative decision, so mutating the
    stride cannot change any cell's raise/return verdict, pinned by
    ``test_budget_check_stride_invariance``).
    """

    def test_f1_singleton_frequent_at_exactly_min_hits(self) -> None:
        # Pins the F_1 singleton filter ``count >= min_hits`` (NOT ``count >``):
        # bits 6 and 7 fire on EXACTLY min_hits bars, so both are frequent and the
        # k=2 candidate (6, 7) MUST be emitted. Under a ``count > min_hits`` mutant
        # F_1 is empty and (6, 7) vanishes → this asserts the boundary keeps it.
        min_hits = 3
        pair = (1 << 6) | (1 << 7)
        bar_bits = [pair] * min_hits  # both bits fire on exactly min_hits bars
        out = generate_apriori_candidates(
            bar_bits, condition_bits=[6, 7], k=2, min_hits=min_hits, n_bars=len(bar_bits)
        )
        assert out == [pair]

    def test_intermediate_frontier_frequent_at_exactly_min_hits(self) -> None:
        # Pins the INTERMEDIATE-frontier filter ``count >= min_hits`` (NOT
        # ``count >``) at the F_2 build feeding a k=3 join. Singletons 6,7,8 fire
        # strictly ABOVE min_hits (so F_1 is unaffected by either boundary), while
        # the pairs (6,7)/(6,8)/(7,8) each fire on EXACTLY min_hits bars → all three
        # frequent → the triple (6,7,8) MUST be emitted. Under ``count > min_hits``
        # at the F_2 filter the pairs drop out of F_2, the triple fails subset-prune
        # and vanishes.
        min_hits = 3
        triple = (1 << 6) | (1 << 7) | (1 << 8)
        bar_bits = (
            [triple] * min_hits  # 6,7,8 together on exactly min_hits bars
            + [1 << 6] * 4  # + bit 6 alone → count(6) = 3 + 4 = 7 > min_hits
            + [1 << 7] * 4  # count(7) = 7 > min_hits
            + [1 << 8] * 4  # count(8) = 7 > min_hits
        )
        out = generate_apriori_candidates(
            bar_bits, condition_bits=[6, 7, 8], k=3, min_hits=min_hits, n_bars=len(bar_bits)
        )
        assert out == [triple]

    def test_breaker_exactly_at_limit_does_not_trip(self) -> None:
        # FROZEN FIXTURE (a) — EXACT BOUNDARY, strict ``>``: count * n_bars ==
        # max_mask_bars EXACTLY (2 × 10 == 20), so the cell is ALLOWED — only a
        # STRICTLY-over product trips. Under a ``>=`` mutant this exactly-at-limit
        # cell would raise. Pure integer, deterministic Python↔Rust.
        out = generate_apriori_candidates(
            [(1 << 6) | (1 << 7)] * 10,
            condition_bits=[6, 7],
            k=1,
            min_hits=0,
            n_bars=10,
            max_mask_bars=20,
        )
        assert out == [1 << 6, 1 << 7]  # returned normally, no raise

    def test_breaker_one_over_limit_trips(self) -> None:
        # FROZEN FIXTURE (a) — ONE OVER: count * n_bars == max_mask_bars + 1
        # (2 × 10 == 20 > 19) → RAISES. Together with the exactly-at-limit fixture
        # this pins the ``>`` vs ``>=`` boundary deterministically from both sides.
        with pytest.raises(SweepAprioriBudgetError):
            generate_apriori_candidates(
                [(1 << 6) | (1 << 7)] * 10,
                condition_bits=[6, 7],
                k=1,
                min_hits=0,
                n_bars=10,
                max_mask_bars=19,
            )

    def test_breaker_default_limit_exact_boundary(self) -> None:
        # FROZEN FIXTURE (a) at the DEFAULT constant: with NO override, a product
        # EXACTLY at DEFAULT_MAX_MASK_BARS does NOT trip, and DEFAULT + 1 DOES —
        # exercising + pinning the default constant literal (count=1 over one bit ×
        # n_bars = the default product; a mutated default flips exactly one of the
        # two assertions).
        one_bit = [6]
        assert (
            generate_apriori_candidates(
                [0], condition_bits=one_bit, k=1, min_hits=0, n_bars=DEFAULT_MAX_MASK_BARS
            )
            == [1 << 6]  # 1 × DEFAULT == DEFAULT, not strictly over → allowed
        )
        with pytest.raises(SweepAprioriBudgetError):
            generate_apriori_candidates(
                [0], condition_bits=one_bit, k=1, min_hits=0, n_bars=DEFAULT_MAX_MASK_BARS + 1
            )

    def test_budget_check_stride_cadence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Pins the incremental in-join breaker cadence ``len(candidates) % STRIDE
        # == 0`` (NOT ``!= 0``). With STRIDE patched to 4 and a level-2 join
        # producing exactly C(5, 2) = 10 candidates, on_count must fire at running
        # counts [4, 8] (the multiples of 4). Under a ``% STRIDE != 0`` mutant it
        # would fire at every NON-multiple ([1, 2, 3, 5, 6, 7, 9, 10]) instead.
        monkeypatch.setattr(sweep_apriori, "_BUDGET_CHECK_STRIDE", 4)
        bits = [6, 7, 8, 9, 10]  # 5 conflict-free bits → C(5, 2) = 10 pairs
        frontier_tuples: list[tuple[int, ...]] = [(b,) for b in bits]
        frontier_masks = np.array(sorted(1 << b for b in bits), dtype=np.uint64)
        recorded: list[int] = []
        # Whitebox: drive the private join directly to observe the breaker-check
        # cadence (DEC-P0-017 per-site friend-access convention).
        out = sweep_apriori._join_and_prune(  # pyright: ignore[reportPrivateUsage]
            frontier_tuples, frontier_masks, drop_conflicts=False, on_count=recorded.append
        )
        assert len(out) == 10
        assert recorded == [4, 8]

    # ── literal-value pins: env-var NAMES + numeric DEFAULTS ──
    # mutmut wraps a string constant ``"foo"`` → ``"XXfooXX"`` and bumps a number
    # ±1; the resolver / env tests consume these constants SYMMETRICALLY (they set
    # and read via the SAME symbol), so only a literal-value pin catches a changed
    # constant. An operator sets ``BRUTEX_SWEEP_APRIORI`` literally, so the string
    # value is load-bearing, not cosmetic.

    def test_env_var_name_literals(self) -> None:
        assert SWEEP_APRIORI_ENV == "BRUTEX_SWEEP_APRIORI"
        assert MAX_MASK_BARS_ENV == "BRUTEX_SWEEP_APRIORI_MAX_MASK_BARS"

    def test_default_max_mask_bars_constant(self) -> None:
        # The default mask·bar cap literal (≈ 2 h × 1.61e8 mask·bar/s). A mutmut
        # ``±1`` on this constant is caught here AND by
        # ``test_breaker_default_limit_exact_boundary`` (which flips at DEFAULT ± 1).
        assert DEFAULT_MAX_MASK_BARS == 1_159_200_000_000

    def test_resolver_invalid_log_event_name(self) -> None:
        # The structlog EVENT NAME an operator alerts on: a ``"XX…XX"`` wrap mutant
        # renames it silently. ``capture_logs`` pins the exact emitted event.
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            resolve_max_mask_bars({MAX_MASK_BARS_ENV: "garbage"})
        assert [entry["event"] for entry in logs] == ["sweep.apriori.max_mask_bars.invalid"]

    # ── EXACT-message pins ──
    # mutmut wraps each f-string SEGMENT ``"seg"`` → ``"XXsegXX"``; a full
    # string-equality assertion catches ANY changed segment (the interpolated
    # ``{…}`` fields are preserved by the wrap, so a substring assert on those
    # would still pass — only exact equality kills the literal-text wraps).

    def test_breaker_message_is_exact(self) -> None:
        # k=1 over 8 bits → count=8; 8 × 10 = 80 > limit 79 → trips. No cell_label
        # passed → the message carries the DEFAULT ``''`` (pinning that default too:
        # a ``cell_label="XXXX"`` mutant would render ``'XXXX'``). Pure integers, so
        # the exact message is stable + deterministic Python↔Rust.
        with pytest.raises(SweepAprioriBudgetError) as ei:
            generate_apriori_candidates(
                [0] * 10,
                condition_bits=list(range(8)),
                k=1,
                min_hits=0,
                n_bars=10,
                max_mask_bars=79,
            )
        assert str(ei.value) == (
            "apriori cell '': k=1 would emit 8 candidate(s) over 10 bars — "
            "8 x 10 = 80 mask·bar exceeds the per-cell limit 79 mask·bar. "
            "Remediation: raise BRUTEX_SWEEP_APRIORI_MAX_MASK_BARS, tighten "
            "min_hits, reduce k, or unset BRUTEX_SWEEP_APRIORI to use the "
            "brute-force path."
        )

    def test_unsupported_bit_message_is_exact(self) -> None:
        with pytest.raises(SweepAprioriUnsupportedError) as ei:
            generate_apriori_candidates([1], condition_bits=[0, 64], k=1, min_hits=0)
        assert str(ei.value) == (
            "generate_apriori_candidates: Apriori frontier requires <64-bit "
            "masks (vocab bit 64 >= 64); unset BRUTEX_SWEEP_APRIORI for u128 "
            "vocabularies."
        )

    def test_validation_error_messages_are_exact(self) -> None:
        with pytest.raises(ValueError) as neg:
            generate_apriori_candidates([1], condition_bits=[-1, 3], k=1, min_hits=0)
        assert str(neg.value) == (
            "generate_apriori_candidates: condition bit -1 is negative (out of range [0, 64))"
        )
        with pytest.raises(ValueError) as krange:
            generate_apriori_candidates([1], condition_bits=[0, 1, 2], k=4, min_hits=0)
        assert str(krange.value) == (
            "generate_apriori_candidates: invalid k=4 for 3 unique bits (require 0 <= k <= n)"
        )
        with pytest.raises(ValueError) as rmod:
            generate_apriori_candidates([1], condition_bits=_BITS, k=1, min_hits=0, root_mod=(3, 2))
        assert str(rmod.value) == (
            "generate_apriori_candidates: root_mod (3, 2) invalid (require W >= 1 and 0 <= w < W)"
        )
        with pytest.raises(ValueError) as nbars:
            generate_apriori_candidates([1], condition_bits=_BITS, k=1, min_hits=0, n_bars=-1)
        assert str(nbars.value) == ("generate_apriori_candidates: n_bars must be >= 0, got -1")
        with pytest.raises(ValueError) as maxmb:
            generate_apriori_candidates(
                [1], condition_bits=_BITS, k=1, min_hits=0, max_mask_bars=-5
            )
        assert str(maxmb.value) == (
            "generate_apriori_candidates: max_mask_bars must be > 0, got -5"
        )

    def test_n_bars_zero_is_allowed(self) -> None:
        # Empty bar_bits → n_bars defaults to 0, which MUST be allowed (the guard is
        # ``resolved_n_bars < 0``, NOT ``<= 0`` / ``< 1``). k=1 over [6, 7] returns
        # both singleton masks; a ``<= 0`` / ``< 1`` mutant raises on the 0 default.
        assert generate_apriori_candidates([], condition_bits=[6, 7], k=1, min_hits=0) == [64, 128]

    def test_breaker_product_is_count_times_n_bars(self) -> None:
        # Pins the pure-integer trip ``count * n_bars > limit`` (NOT ``count /
        # n_bars`` / ``+`` / ``//``): count=8 (k=1 over 8 bits), n_bars=10 → 8 × 10
        # = 80 > limit 50 TRIPS, while 8 / 10 = 0.8 (or 8 + 10 = 18, or 8 // 10 = 0)
        # is ≤ 50 and would NOT trip. Asserting the RAISE kills the ``*``→``/`` (and
        # ``+`` / ``//``) mutants.
        with pytest.raises(SweepAprioriBudgetError):
            generate_apriori_candidates(
                [0] * 10,
                condition_bits=list(range(8)),
                k=1,
                min_hits=0,
                n_bars=10,
                max_mask_bars=50,
            )

    def test_f1_counter_length_mismatch_is_strict(self) -> None:
        # The F_1 frontier zip is ``strict=True``: an injected counter returning the
        # WRONG number of counts is a caller-contract violation the strict flag
        # catches LOUDLY. A ``strict=False`` mutant would silently TRUNCATE instead
        # of raising — so this pin distinguishes the two.
        def short_counter(masks: Sequence[int]) -> list[int]:
            return [500] * (len(list(masks)) - 1)  # one count short

        with pytest.raises(ValueError, match="zip"):
            generate_apriori_candidates(
                [0] * 10,
                condition_bits=[6, 7],
                k=2,
                min_hits=0,
                count_hits=short_counter,
                n_bars=10,
            )

    def test_intermediate_counter_length_mismatch_is_strict(self) -> None:
        # The INTERMEDIATE-frontier zip (the F_2 build feeding a k=3 join) is also
        # ``strict=True``: a counter correct for the F_1 singletons but SHORT for the
        # pair candidates trips the strict flag at the intermediate level (not F_1).
        # A ``strict=False`` mutant would silently truncate.
        calls: list[int] = []

        def staged_counter(masks: Sequence[int]) -> list[int]:
            m = list(masks)
            calls.append(len(m))
            if len(calls) == 1:
                return [500] * len(m)  # F_1 singletons: correct length
            return [500] * (len(m) - 1)  # intermediate pairs: one short

        with pytest.raises(ValueError, match="zip"):
            generate_apriori_candidates(
                [0] * 10,
                condition_bits=[6, 7, 8],
                k=3,
                min_hits=0,
                count_hits=staged_counter,
                n_bars=10,
            )
        assert calls[0] == 3  # F_1 zipped fine (correct length) — the trip is at F_2

    def test_f1_counts_true_singleton_masks(self) -> None:
        # The F_1 frontier must count each singleton against its TRUE mask
        # (``1 << bit``): an INfrequent singleton (fires on < min_hits bars) is
        # excluded from F_1, so no k=2 pair involving it is emitted. Under a
        # ``1 >> bit`` mutant the mask collapses to 0 for every bit >= 1, and a 0
        # mask's superset-count is the WHOLE bar window (``bar & 0 == 0`` for every
        # bar) → the bit is wrongly deemed frequent and its pair LEAKS into the
        # output. (This mutant is invisible to the dense-fixture parity tests, where
        # every singleton is frequent either way — so it needs an explicit
        # infrequent singleton.)
        # bit 6 fires on all 12 bars; bit 7 on only 2 (< min_hits 5) → INfrequent →
        # F_1 = {6}, and the k=2 join has a single frequent singleton → NO pair.
        bar_bits = [1 << 6] * 10 + [(1 << 6) | (1 << 7)] * 2
        out = generate_apriori_candidates(
            bar_bits, condition_bits=[6, 7], k=2, min_hits=5, n_bars=len(bar_bits)
        )
        assert out == []

    def test_shard_ownership_is_lowest_set_bit(self) -> None:
        # Pins the final shard filter ``tpl[0] % W == w`` (the LOWEST set bit), NOT
        # ``tpl[1]`` (the second bit): for a fixed (w=0, W=2) the owned masks are
        # EXACTLY those whose lowest-set-bit index is even — computed INDEPENDENTLY.
        # A ``tpl[1]`` mutant yields a different set (mask 192 = bits {6, 7} has
        # lowest bit 6 (even → owned) but second bit 7 (odd → mis-dropped)), so the
        # union/disjoint shard tests (which pass under ANY partition key) miss it.
        bar_bits = _synthetic_bar_bits(11)
        whole = generate_apriori_candidates(
            bar_bits, condition_bits=[6, 7, 8, 9], k=2, min_hits=0, n_bars=len(bar_bits)
        )
        shard0 = generate_apriori_candidates(
            bar_bits,
            condition_bits=[6, 7, 8, 9],
            k=2,
            min_hits=0,
            n_bars=len(bar_bits),
            root_mod=(0, 2),
        )
        expected = [m for m in whole if ((m & -m).bit_length() - 1) % 2 == 0]
        assert shard0 == expected
        assert 192 in expected  # bits {6, 7}: lowest bit 6 even → owned (tpl[1]=7 would drop)

    def test_breaker_names_level_low_and_high(self) -> None:
        # FROZEN FIXTURE (b) — SEAM COVERAGE at a LOW and a HIGH level: a k=2 tuple
        # and a k=5 tuple both trip the breaker, and the message NAMES the right
        # level (``k=2`` / ``k=5``). Pins the per-level ``_budget_check`` seam at
        # both ends of the frontier build (a low-k final-level trip and a high-k
        # multi-level build), and that ``_raise_budget`` echoes the target ``k``.
        with pytest.raises(SweepAprioriBudgetError) as low:
            generate_apriori_candidates(
                [(1 << 6) | (1 << 7)] * 10,
                condition_bits=[6, 7],
                k=2,
                min_hits=0,
                n_bars=10,
                max_mask_bars=1,
            )
        assert "k=2" in str(low.value)

        with pytest.raises(SweepAprioriBudgetError) as high:
            generate_apriori_candidates(
                [(1 << 6) | (1 << 7) | (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11)] * 10,
                condition_bits=[6, 7, 8, 9, 10, 11],
                k=5,
                min_hits=0,
                n_bars=10,
                max_mask_bars=1,
            )
        assert "k=5" in str(high.value)


# ────────────────────────── validation guards ──────────────────────────


def test_guard_rejects_bit_at_or_above_64() -> None:
    # A >= 64-bit (u128) vocab is refused GRACEFULLY: SweepAprioriUnsupportedError
    # is a SweepComboError subclass, so the grid driver skips the tuple
    # (TUPLE_FAILED) rather than crashing the whole grid with a raw ValueError.
    with pytest.raises(SweepAprioriUnsupportedError, match="u128") as ei:
        generate_apriori_candidates([1], condition_bits=[0, 64], k=1, min_hits=0)
    assert isinstance(ei.value, SweepComboError)
    assert "vocab bit 64 >= 64" in str(ei.value)
    assert SWEEP_APRIORI_ENV in str(ei.value)
    # It is NOT a ValueError — the whole point of the guard-symmetry fix.
    assert not isinstance(ei.value, ValueError)


def test_guard_rejects_negative_bit() -> None:
    # A negative bit is a caller-contract violation (never from a real vocab) →
    # a plain ValueError, deliberately NOT the graceful u128 refusal.
    with pytest.raises(ValueError, match="negative") as ei:
        generate_apriori_candidates([1], condition_bits=[-1, 3], k=1, min_hits=0)
    assert not isinstance(ei.value, SweepComboError)


def test_rejects_k_out_of_range() -> None:
    with pytest.raises(ValueError, match="invalid k"):
        generate_apriori_candidates([1], condition_bits=[0, 1, 2], k=4, min_hits=0)


@pytest.mark.parametrize("root_mod", [(0, 0), (2, 2), (-1, 3), (3, 2)])
def test_rejects_bad_root_mod(root_mod: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="root_mod"):
        generate_apriori_candidates([1], condition_bits=_BITS, k=1, min_hits=0, root_mod=root_mod)


def test_rejects_negative_n_bars() -> None:
    with pytest.raises(ValueError, match="n_bars"):
        generate_apriori_candidates([1], condition_bits=_BITS, k=1, min_hits=0, n_bars=-1)


@pytest.mark.parametrize("bad", [0, -1, -1_000_000])
def test_rejects_non_positive_max_mask_bars(bad: int) -> None:
    # ``max_mask_bars`` is a positive INTEGER; ``<= 0`` is a caller-contract
    # violation (no float / inf / nan path any more — pure integer).
    with pytest.raises(ValueError, match="max_mask_bars"):
        generate_apriori_candidates([1], condition_bits=_BITS, k=1, min_hits=0, max_mask_bars=bad)


# ────────────────────────── env flag + resolvers ──────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("", False),
        ("1", True),
        ("true", True),
        ("on", True),
        ("YES", True),
        ("0", False),
        ("off", False),
        ("garbage", False),
    ],
)
def test_apriori_enabled(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool) -> None:
    if raw is None:
        monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
    else:
        monkeypatch.setenv(SWEEP_APRIORI_ENV, raw)
    assert apriori_enabled() is expected


def test_resolve_max_mask_bars_default_and_valid() -> None:
    assert resolve_max_mask_bars({}) == DEFAULT_MAX_MASK_BARS
    assert resolve_max_mask_bars({MAX_MASK_BARS_ENV: "  "}) == DEFAULT_MAX_MASK_BARS
    assert resolve_max_mask_bars({MAX_MASK_BARS_ENV: "3600"}) == 3600


@pytest.mark.parametrize("bad", ["garbage", "3.5", "2.5e8", "nan", "inf", "-1", "0"])
def test_resolve_invalid_falls_back_loudly(bad: str) -> None:
    # Invalid (non-integer OR non-positive) → the documented default, never a raise.
    # ``int("3.5")`` / ``int("2.5e8")`` raise → the non-integer fallback; ``"-1"`` /
    # ``"0"`` are non-positive.
    assert resolve_max_mask_bars({MAX_MASK_BARS_ENV: bad}) == DEFAULT_MAX_MASK_BARS


def test_resolver_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MAX_MASK_BARS_ENV, "1234")
    assert resolve_max_mask_bars() == 1234


# ────────────────────────── internal helpers ──────────────────────────
# The subset-membership (``_in_sorted``: present / absent-mid / absent-past-end /
# empty) and subset-prune (``_all_subsets_frequent``: all-present / one-missing)
# numpy branches are exercised behaviourally by the oracle-parity + subset-prune
# tests above (they reach 100% line+branch without touching the private helpers).


# ────────────────────────── CI / mutation wiring pins ──────────────────────────


class TestWorkflowWiring:
    """Pin this module into its dedicated per-module coverage + mutation gates.

    Deterministic pins (the ``TestMutationWorkflowWiring`` /
    ``TestBenchWorkflowWiring`` precedent) that fail the unit suite if the
    module:suite pairing is ever silently dropped from the workflows — an
    unwired gate must never masquerade as an active one (Golden Rule #10).
    """

    _CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    _MUTATION = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def test_dedicated_100pct_coverage_gate_wired(self) -> None:
        text = self._CI.read_text(encoding="utf-8")
        assert "--cov=brutex.engine.sweep_apriori" in text
        assert "tests/unit/test_engine_sweep_apriori.py" in text

    def test_paired_in_the_mutmut_loop(self) -> None:
        text = self._MUTATION.read_text(encoding="utf-8")
        assert (
            '"brutex/engine/sweep_apriori.py:tests/unit/test_engine_sweep_apriori.py:"' in text
        ), "sweep_apriori.py is not wired into mutation.yml's per-module mutmut loop"


# ─────────────── run_persisted_sweep ON-path integration ───────────────
# Exercises the ``apriori_enabled()`` branch inside
# ``brutex.engine.sweep_run.run_persisted_sweep`` (the flag-ON enumeration
# source), a mini version of tests/golden/test_engine_sweep_apriori_parity.py so
# the ``--cov=brutex.engine`` (test_engine_*.py) + whole-package coverage gates
# see the branch. The byte-identity gate itself lives in the golden.


def _mini_bars() -> list:  # type: ignore[type-arg]
    from datetime import UTC, datetime, timedelta, timezone
    from decimal import Decimal

    from brutex.data.models import CandleBar

    ist = timezone(timedelta(hours=5, minutes=30))
    out = []
    for d in range(8):
        price = 100.0
        for m in range(90):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=ist) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(max(price, close) + 0.5, 2))),
                    low=Decimal(str(round(min(price, close) - 0.5, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = close
    return out


def test_run_persisted_sweep_on_path_matches_off(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("polars")  # flush needs the [data] extra
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.results_store import ResultsStore
    from brutex.engine.sweep_run import ComboDedup, SweepRunConfig, run_persisted_sweep
    from brutex.orchestrator.audit import AuditWriter
    from brutex.strategies.types import Side

    bars = _mini_bars()

    def run(tag: str) -> tuple[object, bytes]:
        d = tmp_path / tag
        d.mkdir()
        store = ResultsStore(path=d / "cell.parquet")
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        cfg = SweepRunConfig(
            instrument="NIFTY",
            tf="1m",
            direction=Side.BUY,
            mode="A",
            condition_bits=_BITS,
            k=3,
            data_snapshot_hash="snap",
            indicator_version="iv",
            code_commit_sha="cc",
            tf_minutes=1,
            sl_paisa=80,
            tp_paisa=50,
            min_hits=5,
        )
        with AuditWriter.open(d / "cell.jsonl") as audit:
            summary = run_persisted_sweep(bars, cfg, store=store, dedup=dedup, audit=audit)
        return summary, (d / "cell.parquet").read_bytes()

    monkeypatch.delenv(SWEEP_APRIORI_ENV, raising=False)
    off, off_bytes = run("off")
    monkeypatch.setenv(SWEEP_APRIORI_ENV, "1")
    on, on_bytes = run("on")

    assert on.store_content_hash == off.store_content_hash  # type: ignore[attr-defined]
    assert on_bytes == off_bytes
    assert on.enumerated < off.enumerated  # type: ignore[attr-defined]  # pruning fired


# ─────────────── driver-level breaker propagation (FIX 5) ───────────────
# Belt-and-suspenders: drive the REAL sequential grid driver with the flag ON and
# a tiny budget so a tuple TRIPS the breaker, proving the
# SweepAprioriBudgetError → SweepComboError → TUPLE_FAILED propagation works
# end-to-end through the driver (not just the unit isinstance check).


def test_driver_reports_breaker_trip_as_tuple_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("polars")  # store construction needs the [data] extra
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.sweep_orchestrator import (
        SweepGridConfig,
        Timeframe,
        make_store_for,
        run_sweep_grid,
    )
    from brutex.engine.sweep_run import ComboDedup
    from brutex.orchestrator.audit import AuditWriter
    from brutex.strategies.types import Side

    bars = _mini_bars()
    grid = SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY],
        k_values=[2],
        condition_bits=_BITS,
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap",
        indicator_version="iv",
        code_commit_sha="cc",
        min_hits=0,
    )
    # Flag ON + a microscopic mask·bar cap → any non-empty candidate set trips the
    # breaker inside generate_apriori_candidates.
    monkeypatch.setenv(SWEEP_APRIORI_ENV, "1")
    monkeypatch.setenv(MAX_MASK_BARS_ENV, "1")

    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    with AuditWriter.open(tmp_path / "grid.jsonl") as audit:
        summary = run_sweep_grid(
            grid,
            bars_for=lambda _i, _t: bars,
            store_for=make_store_for(tmp_path / "results", grid),
            dedup=dedup,
            audit=audit,
        )

    # The tripped tuple is counted as failed (non-zero verdict), NOT persisted —
    # the graceful TUPLE_FAILED skip, driven end-to-end through the real driver.
    assert summary.tuples_failed == 1
    assert summary.persisted == 0
    # And the driver recorded a TUPLE_FAILED audit line for it.
    audit_text = (tmp_path / "grid.jsonl").read_text(encoding="utf-8")
    assert "TUPLE_FAILED" in audit_text


def test_driver_reports_supertrend_bit_tuple_as_tuple_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 66-bit-vocab-shaped pin (DEC-SUPERTREND-CONDITIONS-001): an
    Apriori-ON tuple whose ``condition_bits`` include a LIVE ``>= 64`` bit
    (SuperTrend bit 64) refuses via ``SweepAprioriUnsupportedError`` and the
    REAL sequential grid driver skips the tuple GRACEFULLY — counted in
    ``tuples_failed`` + a ``TUPLE_FAILED`` audit line, never a crash and never
    a corrupt store. This is the accepted interim production story for
    Apriori over the post-flip DEFAULT pool (which now contains bits 64/65);
    the u128 two-limb frontier is the named follow-up.
    """
    pytest.importorskip("polars")  # store construction needs the [data] extra
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.sweep_orchestrator import (
        SweepGridConfig,
        Timeframe,
        make_store_for,
        run_sweep_grid,
    )
    from brutex.engine.sweep_run import ComboDedup
    from brutex.orchestrator.audit import AuditWriter
    from brutex.strategies.types import Side

    bars = _mini_bars()
    grid = SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 64],  # bit 64 is LIVE (close_above_supertrend)
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap",
        indicator_version="iv",
        code_commit_sha="cc",
        min_hits=0,
    )
    monkeypatch.setenv(SWEEP_APRIORI_ENV, "1")
    monkeypatch.delenv(MAX_MASK_BARS_ENV, raising=False)

    dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
    with AuditWriter.open(tmp_path / "grid.jsonl") as audit:
        summary = run_sweep_grid(
            grid,
            bars_for=lambda _i, _t: bars,
            store_for=make_store_for(tmp_path / "results", grid),
            dedup=dedup,
            audit=audit,
        )

    # Graceful per-tuple refusal: counted as failed, nothing persisted, the
    # TUPLE_FAILED audit line names the guard — never an unhandled raise.
    assert summary.tuples_failed == 1
    assert summary.persisted == 0
    audit_text = (tmp_path / "grid.jsonl").read_text(encoding="utf-8")
    assert "TUPLE_FAILED" in audit_text
    assert "vocab bit 64 >= 64" in audit_text


# ─────────── FOLD 1: Apriori-aware max_combos guardrail (effective_max_combos) ───────────


def test_effective_max_combos_returns_requested_when_apriori_off() -> None:
    # (a) Apriori OFF → the value is returned UNCHANGED (the brute-force pre-flight
    # is byte-identical) even for a k whose C(n, k) dwarfs the request, and even
    # when it is the built-in DEFAULT (requested_is_explicit is irrelevant when off).
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        out = effective_max_combos(
            apriori_enabled=False,
            k_values=[1, 2, 3, 4],
            n_unique=60,
            requested_max_combos=100_000,
            requested_is_explicit=False,
        )
    assert out == 100_000
    assert [e for e in logs if e["event"] == "sweep.apriori.max_combos_raised"] == []


def test_effective_max_combos_raises_default_to_admit_k4_when_on() -> None:
    # (b) Apriori ON + the value is the DEFAULT (requested_is_explicit=False) + a
    # k=4 over 100k → the effective value is RAISED to at least C(60, 4) so the
    # guards ADMIT every requested k, and ONE counts-only structlog fires.
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        out = effective_max_combos(
            apriori_enabled=True,
            k_values=[1, 2, 3, 4],
            n_unique=60,
            requested_max_combos=100_000,
            requested_is_explicit=False,
        )
    assert out == max(100_000, count_combos(4, 60))
    assert out >= count_combos(4, 60)  # C(60,4) = 487_635 > 100_000
    raised = [e for e in logs if e["event"] == "sweep.apriori.max_combos_raised"]
    assert len(raised) == 1
    assert raised[0]["requested"] == 100_000
    assert raised[0]["effective"] == out
    assert raised[0]["reason"] == "apriori_breaker_guardrail"


def test_effective_max_combos_explicit_low_operator_wins_no_raise() -> None:
    # (c) Apriori ON + an EXPLICIT operator value (requested_is_explicit=True) that
    # is deliberately LOW → returned UNCHANGED (OPERATOR WINS), NO log — even though
    # a requested k needs more than the explicit limit. The downstream guard then
    # HONORS the operator's explicit refusal.
    from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe
    from brutex.strategies.types import Side
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        out = effective_max_combos(
            apriori_enabled=True,
            k_values=[1, 2, 3, 4],
            n_unique=60,
            requested_max_combos=100,  # explicit LOW cap
            requested_is_explicit=True,
        )
    assert out == 100  # operator's explicit value honored verbatim, never raised
    assert [e for e in logs if e["event"] == "sweep.apriori.max_combos_raised"] == []

    # And the guard REFUSES a k needing > 100 masks (C(60, 2) = 1770 > 100).
    with pytest.raises(ValueError, match="max_combos"):
        SweepGridConfig(
            instruments=["NIFTY"],
            timeframes=[Timeframe("1minute", 1)],
            directions=[Side.BUY],
            k_values=[2],
            condition_bits=list(range(60)),
            stop_losses=[80],
            take_profits=[50],
            proximities=[None],
            mode="A",
            data_snapshot_hash="snap",
            indicator_version="iv",
            code_commit_sha="cc",
            min_hits=0,
            max_combos=out,
        )


def test_effective_max_combos_explicit_high_operator_value_unchanged() -> None:
    # (d) Apriori ON + an EXPLICIT HIGH operator value → returned unchanged, NO log
    # (already admits every k — but it wins because it is explicit, not because it
    # is large: the explicit branch returns BEFORE any max() computation).
    from structlog.testing import capture_logs

    big = count_combos(4, 60) + 1
    with capture_logs() as logs:
        out = effective_max_combos(
            apriori_enabled=True,
            k_values=[1, 2, 3, 4],
            n_unique=60,
            requested_max_combos=big,
            requested_is_explicit=True,
        )
    assert out == big
    assert [e for e in logs if e["event"] == "sweep.apriori.max_combos_raised"] == []


def test_effective_max_combos_default_already_sufficient_no_raise() -> None:
    # Apriori ON + DEFAULT value that already admits every k (empty k_values → the
    # max(..., default=0) arm; effective == requested) → returned unchanged, NO log.
    # This covers the on+default branch where `effective > requested` is False.
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        out = effective_max_combos(
            apriori_enabled=True,
            k_values=[],
            n_unique=60,
            requested_max_combos=123,
            requested_is_explicit=False,
        )
    assert out == 123
    assert [e for e in logs if e["event"] == "sweep.apriori.max_combos_raised"] == []


def test_effective_max_combos_empty_kvalues_zero_request_default_sentinel() -> None:
    # Pins the ``max(..., default=0)`` SENTINEL (kills mutant 18: default=0 → 1). An
    # empty k_values generator with a zero requested cap must return EXACTLY 0 with NO
    # raise log — a default of 1 would return max(0, 1) == 1 and fail this equality.
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        out = effective_max_combos(
            apriori_enabled=True,
            k_values=[],
            n_unique=60,
            requested_max_combos=0,
            requested_is_explicit=False,
        )
    assert out == 0
    assert [e for e in logs if e["event"] == "sweep.apriori.max_combos_raised"] == []


def test_effective_max_combos_all_k_out_of_range_defers_no_crash() -> None:
    # FIX 1 (flag-ON hygiene parity): every requested k is out of range (k=4 > n=3),
    # so the C(n,k) generator is EMPTY (each k filtered before ``count_combos``) →
    # needed=0 → the requested cap is returned UNCHANGED, with NO raw ValueError and
    # NO max_combos_raised log. Deferred to the downstream SweepGridConfig validation,
    # exactly as the flag-OFF path does. (Fails-before: unfiltered code raised
    # ValueError on ``count_combos(4, 3)``.)
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        out = effective_max_combos(
            apriori_enabled=True,
            k_values=[4],
            n_unique=3,
            requested_max_combos=100_000,
            requested_is_explicit=False,
        )
    assert out == 100_000
    assert [e for e in logs if e["event"] == "sweep.apriori.max_combos_raised"] == []


def test_effective_max_combos_k_zero_in_range_counted() -> None:
    # Kills mutants 18/19: ``0 <= k`` → ``1 <= k`` and ``0 <= k`` → ``0 < k`` both drop
    # the in-range k=0 case (``C(n, 0) == 1``, legitimately in count_combos's domain
    # ``0 <= k <= n``), so the raise generator would go empty → needed=0 → return the
    # requested 0 unchanged with NO raise log. In-range k=0 must contribute C(n,0)=1.
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        out = effective_max_combos(
            apriori_enabled=True,
            k_values=[0],
            n_unique=60,
            requested_max_combos=0,
            requested_is_explicit=False,
        )
    assert out == 1
    raised = [e for e in logs if e["event"] == "sweep.apriori.max_combos_raised"]
    assert len(raised) == 1


def test_effective_max_combos_k_equals_n_unique_in_range_counted() -> None:
    # Kills mutant 20: ``k <= n_unique`` → ``k < n_unique`` drops the in-range
    # k=n_unique case (``C(n, n) == 1``, legitimately in count_combos's domain
    # ``0 <= k <= n``), so the raise generator would go empty → needed=0 → return the
    # requested 0 unchanged with NO raise log. In-range k=n_unique must contribute
    # C(n,n)=1.
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        out = effective_max_combos(
            apriori_enabled=True,
            k_values=[60],
            n_unique=60,
            requested_max_combos=0,
            requested_is_explicit=False,
        )
    assert out == 1
    raised = [e for e in logs if e["event"] == "sweep.apriori.max_combos_raised"]
    assert len(raised) == 1


def test_effective_max_combos_grid_guard_interaction() -> None:
    # (e) The load-bearing interaction: the guards' LOGIC is byte-unchanged — we only
    # FEED them a value. With n_unique = 60, C(60, 4) = 487_635 > the default 100k,
    # so the raw pre-flight REFUSES k=4 unless the Apriori-aware DEFAULT value admits it.
    from brutex.engine.sweep_driver import DEFAULT_MAX_COMBOS
    from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe
    from brutex.strategies.types import Side

    bits = list(range(60))
    k_values = [1, 2, 3, 4]
    n_unique = len(set(bits))
    assert DEFAULT_MAX_COMBOS == 100_000
    assert count_combos(4, n_unique) > DEFAULT_MAX_COMBOS

    def _grid(max_combos: int) -> SweepGridConfig:
        return SweepGridConfig(
            instruments=["NIFTY"],
            timeframes=[Timeframe("1minute", 1)],
            directions=[Side.BUY],
            k_values=k_values,
            condition_bits=bits,
            stop_losses=[80],
            take_profits=[50],
            proximities=[None],
            mode="A",
            data_snapshot_hash="snap",
            indicator_version="iv",
            code_commit_sha="cc",
            min_hits=0,
            max_combos=max_combos,
        )

    # Default 100k REFUSES k=4 up front (guard byte-unchanged).
    with pytest.raises(ValueError, match="max_combos"):
        _grid(DEFAULT_MAX_COMBOS)

    # Non-Apriori path: DEFAULT returned unchanged, so the guard STILL refuses k=4.
    off_effective = effective_max_combos(
        apriori_enabled=False,
        k_values=k_values,
        n_unique=n_unique,
        requested_max_combos=DEFAULT_MAX_COMBOS,
        requested_is_explicit=False,
    )
    assert off_effective == DEFAULT_MAX_COMBOS
    with pytest.raises(ValueError, match="max_combos"):
        _grid(off_effective)

    # Apriori ON + DEFAULT: the effective value admits every k → the grid CONSTRUCTS.
    on_effective = effective_max_combos(
        apriori_enabled=True,
        k_values=k_values,
        n_unique=n_unique,
        requested_max_combos=DEFAULT_MAX_COMBOS,
        requested_is_explicit=False,
    )
    assert on_effective >= count_combos(4, n_unique)
    grid = _grid(on_effective)  # must NOT raise
    assert grid.max_combos == on_effective


# ─────────── FOLD 2: per-level survivor census (sweep.apriori.census) ───────────


def test_apriori_census_emitted_per_level_with_counts() -> None:
    # A dense all-firing cell over 4 conflict-free bits at k=3: every candidate is
    # frequent (min_hits=0), so |F_j| == |C_j| and the counts match the independent
    # brute-force C(n, j). The internal frontiers carry `frequent`; the TARGET
    # level k omits it (Stage-1B is the authority).
    from structlog.testing import capture_logs

    bar_bits = [0b1111] * 40  # every mask fires on all 40 bars
    with capture_logs() as logs:
        generate_apriori_candidates(
            bar_bits, condition_bits=[0, 2, 4, 6], k=3, min_hits=0, cell_label="cell-x"
        )
    census = [e for e in logs if e["event"] == "sweep.apriori.census"]
    by_level = {e["level"]: e for e in census}
    assert set(by_level) == {1, 2, 3}

    # Independent brute-force candidate counts: |C_1|=n, |C_2|=C(n,2), |C_3|=C(n,3).
    assert by_level[1]["candidates"] == 4
    assert by_level[1]["frequent"] == 4
    assert by_level[2]["candidates"] == count_combos(2, 4)  # 6
    assert by_level[2]["frequent"] == count_combos(2, 4)
    assert by_level[3]["candidates"] == count_combos(3, 4)  # 4
    assert "frequent" not in by_level[3]  # target level — Stage-1B decides frequency

    for e in census:
        assert e["cell"] == "cell-x"
        assert e["target_k"] == 3
        assert e["min_hits"] == 0
        assert e["n_bars"] == 40


def test_apriori_census_frequent_matches_brute_force_frontier() -> None:
    # With a real min_hits filter, the emitted level-1 `frequent` count must equal
    # the INDEPENDENT brute-force |F_1| (singletons firing on >= min_hits bars).
    from structlog.testing import capture_logs

    bar_bits = _synthetic_bar_bits(7)
    min_hits = 250
    with capture_logs() as logs:
        generate_apriori_candidates(
            bar_bits, condition_bits=_BITS, k=3, min_hits=min_hits, n_bars=len(bar_bits)
        )
    by_level = {e["level"]: e for e in logs if e["event"] == "sweep.apriori.census"}
    singleton_masks = [1 << b for b in sorted(set(_BITS))]
    brute_f1 = sum(1 for c in count_supermasks(bar_bits, singleton_masks) if c >= min_hits)
    assert by_level[1]["candidates"] == len(set(_BITS))
    assert by_level[1]["frequent"] == brute_f1
    assert by_level[1]["frequent"] <= by_level[1]["candidates"]


def test_apriori_census_k1_target_level_omits_frequent() -> None:
    # k == 1 IS the target level: exactly ONE census, candidates = |C_1|, NO frequent.
    from structlog.testing import capture_logs

    bar_bits = [0b1111] * 10
    with capture_logs() as logs:
        generate_apriori_candidates(
            bar_bits, condition_bits=[0, 2, 4], k=1, min_hits=0, cell_label="c1"
        )
    census = [e for e in logs if e["event"] == "sweep.apriori.census"]
    assert len(census) == 1
    assert census[0]["level"] == 1
    assert census[0]["target_k"] == 1
    assert census[0]["candidates"] == 3
    assert census[0]["cell"] == "c1"
    assert "frequent" not in census[0]
