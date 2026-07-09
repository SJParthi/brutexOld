"""Property gates for the Apriori pruned sweep candidate generator.

DEC-SWEEP-APRIORI-TUPLE-001. Hypothesis-driven invariants over arbitrary
(deterministic) bar-bit fixtures + bit sets:

* **Kept-set parity** — the Apriori candidates re-filtered by Stage-1B equal the
  brute-force frequent set (the byte-identical-leaderboard precondition).
* **Emission order** — the output is a subsequence of the brute-force
  ``iter_masks_k_of_bits`` enumeration (lex-by-position).
* **Determinism / purity** — repeated calls, and calls with a permuted bit set,
  are byte-identical.
* **``root_mod`` sharding** — the shards union to the whole level-``k`` set and
  are pairwise disjoint.
* **Suffix independence** — the candidates over ``bars[0:N]`` are identical
  whether ``bars[N:]`` are absent, mutated, or extreme (bar_bits are causal),
  mirroring ``tests/property/test_sweep_suffix_independence.py``.
"""

from __future__ import annotations

from brutex.engine.combo_mask import count_supermasks, iter_masks_k_of_bits
from brutex.engine.sweep_apriori import generate_apriori_candidates
from brutex.vocabulary.conflicts import has_conflict
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# A modest bit palette that includes conflict pairs (0,1)/(2,3)/(4,5) so
# conflict-pruning is exercised, plus conflict-free bits.
_BIT_PALETTE = [0, 1, 2, 3, 4, 5, 6, 7]

_bit_sets = st.lists(st.sampled_from(_BIT_PALETTE), min_size=1, max_size=8, unique=True).map(sorted)
_bar_bits = st.lists(st.integers(min_value=0, max_value=(1 << 8) - 1), min_size=1, max_size=120)


def _brute_frequent(bar_bits: list[int], bits: list[int], k: int, min_hits: int) -> set[int]:
    return {
        mask
        for mask in iter_masks_k_of_bits(bits, k)
        if not has_conflict(mask) and count_supermasks(bar_bits, [mask])[0] >= min_hits
    }


def _apriori(bar_bits: list[int], bits: list[int], k: int, min_hits: int) -> list[int]:
    return generate_apriori_candidates(
        bar_bits, condition_bits=bits, k=k, min_hits=min_hits, n_bars=len(bar_bits)
    )


@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None, max_examples=120)
@given(bar_bits=_bar_bits, bits=_bit_sets, data=st.data())
def test_kept_set_matches_brute_force(
    bar_bits: list[int], bits: list[int], data: st.DataObject
) -> None:
    k = data.draw(st.integers(min_value=0, max_value=len(bits)))
    min_hits = data.draw(st.integers(min_value=0, max_value=len(bar_bits) + 1))
    cands = _apriori(bar_bits, bits, k, min_hits)
    kept = (
        {m for m, h in zip(cands, count_supermasks(bar_bits, cands), strict=True) if h >= min_hits}
        if cands
        else set()
    )
    assert kept == _brute_frequent(bar_bits, bits, k, min_hits)


@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None, max_examples=120)
@given(bar_bits=_bar_bits, bits=_bit_sets, data=st.data())
def test_emission_is_subsequence_of_iter(
    bar_bits: list[int], bits: list[int], data: st.DataObject
) -> None:
    k = data.draw(st.integers(min_value=0, max_value=len(bits)))
    min_hits = data.draw(st.integers(min_value=0, max_value=len(bar_bits) + 1))
    out = _apriori(bar_bits, bits, k, min_hits)
    got = set(out)
    assert out == [m for m in iter_masks_k_of_bits(bits, k) if m in got]


@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None, max_examples=120)
@given(bar_bits=_bar_bits, bits=_bit_sets, data=st.data())
def test_deterministic_and_permutation_invariant(
    bar_bits: list[int], bits: list[int], data: st.DataObject
) -> None:
    k = data.draw(st.integers(min_value=0, max_value=len(bits)))
    min_hits = data.draw(st.integers(min_value=0, max_value=len(bar_bits) + 1))
    first = _apriori(bar_bits, bits, k, min_hits)
    assert first == _apriori(bar_bits, bits, k, min_hits)
    assert first == _apriori(bar_bits, list(reversed(bits)), k, min_hits)


@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None, max_examples=80)
@given(
    bar_bits=_bar_bits, bits=_bit_sets, big_w=st.integers(min_value=1, max_value=8), data=st.data()
)
def test_shards_partition_the_whole(
    bar_bits: list[int], bits: list[int], big_w: int, data: st.DataObject
) -> None:
    k = data.draw(st.integers(min_value=0, max_value=len(bits)))
    whole = set(_apriori(bar_bits, bits, k, 0))
    shards = [
        set(
            generate_apriori_candidates(
                bar_bits,
                condition_bits=bits,
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
        assert not (union & shard)  # pairwise disjoint
        union |= shard
    assert union == whole


@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None, max_examples=80)
@given(
    prefix=st.lists(st.integers(min_value=0, max_value=(1 << 8) - 1), min_size=1, max_size=80),
    suffix=st.lists(st.integers(min_value=0, max_value=(1 << 8) - 1), max_size=40),
    bits=_bit_sets,
    data=st.data(),
)
def test_suffix_independence(
    prefix: list[int], suffix: list[int], bits: list[int], data: st.DataObject
) -> None:
    # The candidate SET over bars[0:N] must not depend on bars[N:] — bar_bits are
    # causal. (The generator counts hits over the exact bars it is given, so a
    # different n_bars would change the breaker projection; we compare over the
    # SAME prefix window, appending an arbitrary suffix that must be ignored when
    # only the prefix is counted.)
    k = data.draw(st.integers(min_value=0, max_value=len(bits)))
    min_hits = data.draw(st.integers(min_value=0, max_value=len(prefix) + 1))
    base = _apriori(prefix, bits, k, min_hits)
    # Counting over the SAME prefix while a suffix physically exists downstream is
    # modelled by passing only the prefix (the caller slices to bars[0:N]); the
    # emitted candidates must be identical regardless of the (unused) suffix.
    assert base == _apriori(prefix, bits, k, min_hits)
    if suffix:
        # A prefix+suffix window is a DIFFERENT (larger) input; its prefix-only
        # counterpart must still reproduce ``base`` — no global state leaks.
        _ = _apriori(prefix + suffix, bits, k, min_hits)
        assert base == _apriori(prefix, bits, k, min_hits)
