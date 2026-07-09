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

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.combo_mask import count_supermasks, iter_masks_k_of_bits
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_apriori import generate_apriori_candidates
from brutex.vocabulary.conflicts import has_conflict
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# A modest bit palette that includes conflict pairs (0,1)/(2,3)/(4,5) so
# conflict-pruning is exercised, plus conflict-free bits.
_BIT_PALETTE = [0, 1, 2, 3, 4, 5, 6, 7]

_IST = timezone(timedelta(hours=5, minutes=30))
_SESSION_OPEN_IST = datetime(2026, 1, 5, 9, 16, tzinfo=_IST)
_BARS_PER_DAY = 15
# Integer-rupee prices INCLUDING the extreme edges, so the "extreme suffix" arm
# is genuinely exercised (mirrors tests/property/test_sweep_suffix_independence.py).
_price = st.one_of(
    st.just(1),
    st.just(1_000_000),
    st.integers(min_value=1, max_value=1_000_000),
)


def _bar_at(day: int, minute: int, open_i: int, close_i: int) -> CandleBar:
    ts = (_SESSION_OPEN_IST + timedelta(days=day, minutes=minute)).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_i),
        high=Decimal(max(open_i, close_i)),
        low=Decimal(min(open_i, close_i)),
        close=Decimal(close_i),
        volume=100,
        open_interest=None,
    )


def _series_from_pairs(pairs: list[tuple[int, int]]) -> list[CandleBar]:
    return [
        _bar_at(i // _BARS_PER_DAY, i % _BARS_PER_DAY, open_i, close_i)
        for i, (open_i, close_i) in enumerate(pairs)
    ]


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
    prefix_pairs=st.lists(st.tuples(_price, _price), min_size=1, max_size=45),
    suffix_pairs=st.lists(st.tuples(_price, _price), max_size=30),
    bits=_bit_sets,
    data=st.data(),
)
def test_suffix_independence(
    prefix_pairs: list[tuple[int, int]],
    suffix_pairs: list[tuple[int, int]],
    bits: list[int],
    data: st.DataObject,
) -> None:
    """The COMPOSED ``build_bar_bits`` → generator pipeline over ``bars[0:N]`` is
    causal: its candidates never depend on ``bars[N:]``.

    Mirrors ``tests/property/test_sweep_suffix_independence.py``. The vacuous form
    (computing ``_apriori(prefix + suffix, …)`` and discarding it) is REPLACED by
    a genuine composed comparison: build the truth matrix over the FULL series and
    over the PREFIX series, assert (1) ``build_bar_bits`` is suffix-independent on
    the prefix window (the Stage-1A causal property) and (2) the generator over
    ``bar_bits_full[:N]`` is BIT-IDENTICAL to the generator over the
    prefix-built ``bar_bits`` — whether the suffix is ABSENT, MUTATED, or EXTREME
    (the ``_price`` strategy weights the ₹1 / ₹1,000,000 edges). This FAILS if any
    stage of the pipeline ever smuggled out-of-window data into an earlier result.
    """
    prefix = _series_from_pairs(prefix_pairs)
    n = len(prefix)
    k = data.draw(st.integers(min_value=0, max_value=len(bits)))
    min_hits = data.draw(st.integers(min_value=0, max_value=n + 1))

    bits_prefix = build_bar_bits(prefix)
    base = generate_apriori_candidates(
        bits_prefix, condition_bits=bits, k=k, min_hits=min_hits, n_bars=n
    )

    # Suffix ABSENT: the prefix built alone must reproduce ``base`` (determinism).
    assert base == generate_apriori_candidates(
        bits_prefix, condition_bits=bits, k=k, min_hits=min_hits, n_bars=n
    )

    if suffix_pairs:
        # A FULL series = prefix + an arbitrary (extreme-capable) suffix. Stage-1A
        # must agree with the prefix build on the prefix window …
        full = _series_from_pairs(prefix_pairs + suffix_pairs)
        bits_full = build_bar_bits(full)
        assert bits_full[:n] == bits_prefix  # Stage-1A causal (suffix-independent)
        # … and the generator over the prefix window of the FULL build is
        # bit-identical to ``base`` — the composed causal property, non-vacuous.
        assert base == generate_apriori_candidates(
            bits_full[:n], condition_bits=bits, k=k, min_hits=min_hits, n_bars=n
        )
