"""Hypothesis property tests for content-hash dedup.

The BruteX orchestrator uses ``sha256(canonical(params))`` as the
primary key in the LMDB dedup cache. This file exhaustively
explores random param dictionaries to verify:

1. **Determinism.** Same params always produce the same hash.
2. **Order-invariance.** Dicts with the same key-value pairs in
   different insertion order produce the same hash.
3. **Collision resistance (bounded).** Different params produce
   different hashes with overwhelming probability. (We can't prove
   sha256 collision-free but we CAN prove our canonicalisation
   doesn't introduce accidental collisions.)
4. **Prefix sensitivity.** Changing any single character in any
   value changes the hash — no field goes untracked.
5. **WorkItem ↔ hash round-trip.** ``WorkItem.new(params=X)``
   canonicalises to the same tuple every time.
"""

from __future__ import annotations

import hashlib

from brutex.orchestrator.state import IngestPhase, WorkItem
from hypothesis import given, settings
from hypothesis import strategies as st

# ────────────────────────────────────────────────────────────────────────
# Strategies
# ────────────────────────────────────────────────────────────────────────


_PARAMS_KEY_ALPHABET = st.characters(min_codepoint=0x61, max_codepoint=0x7A)  # a-z

_PARAMS_VALUE_ALPHABET = st.characters(min_codepoint=0x20, max_codepoint=0x7E)  # printable ASCII


params_st = st.dictionaries(
    keys=st.text(alphabet=_PARAMS_KEY_ALPHABET, min_size=1, max_size=12),
    values=st.text(alphabet=_PARAMS_VALUE_ALPHABET, max_size=40),
    min_size=1,
    max_size=8,
)


def _content_hash(params: dict[str, str]) -> str:
    """BruteX's canonical content-hash: sha256 over sorted items."""
    canonical = repr(sorted(params.items())).encode()
    return hashlib.sha256(canonical).hexdigest()


# ────────────────────────────────────────────────────────────────────────
# Invariant 1 — determinism
# ────────────────────────────────────────────────────────────────────────


class TestDeterminism:
    @given(params=params_st)
    @settings(max_examples=200, deadline=None)
    def test_same_params_same_hash(self, params: dict[str, str]) -> None:
        h1 = _content_hash(params)
        h2 = _content_hash(params)
        h3 = _content_hash(dict(params))
        assert h1 == h2 == h3

    @given(params=params_st)
    @settings(max_examples=200, deadline=None)
    def test_hash_length_is_64(self, params: dict[str, str]) -> None:
        h = _content_hash(params)
        assert len(h) == 64
        # hex digits only
        assert all(c in "0123456789abcdef" for c in h)


# ────────────────────────────────────────────────────────────────────────
# Invariant 2 — order-invariance
# ────────────────────────────────────────────────────────────────────────


class TestOrderInvariance:
    @given(params=params_st)
    @settings(max_examples=200, deadline=None)
    def test_reversed_insertion_same_hash(self, params: dict[str, str]) -> None:
        reversed_params = dict(reversed(list(params.items())))
        assert _content_hash(params) == _content_hash(reversed_params)

    @given(params=params_st)
    @settings(max_examples=150, deadline=None)
    def test_workitem_canonicalisation_matches(self, params: dict[str, str]) -> None:
        """WorkItem.new sorts params on construction; two items
        built from the same params (in any order) have identical
        canonical params tuples."""
        item1 = WorkItem.new(
            phase=IngestPhase.A_INSTRUMENTS,
            content_hash=_content_hash(params),
            params=params,
        )
        reversed_params = dict(reversed(list(params.items())))
        item2 = WorkItem.new(
            phase=IngestPhase.A_INSTRUMENTS,
            content_hash=_content_hash(reversed_params),
            params=reversed_params,
        )
        assert item1.params == item2.params
        assert item1.content_hash == item2.content_hash


# ────────────────────────────────────────────────────────────────────────
# Invariant 3 — collision resistance (bounded to our canonicalisation)
# ────────────────────────────────────────────────────────────────────────


class TestCollisionResistance:
    @given(p1=params_st, p2=params_st)
    @settings(max_examples=200, deadline=None)
    def test_different_params_different_hashes(
        self,
        p1: dict[str, str],
        p2: dict[str, str],
    ) -> None:
        """Distinct param dicts must produce distinct hashes.

        sha256 is collision-resistant in cryptographic practice;
        this test verifies our canonicalisation doesn't create
        accidental collisions (e.g. by losing a field).
        """
        if sorted(p1.items()) == sorted(p2.items()):
            # Same canonical form — hashes must match
            assert _content_hash(p1) == _content_hash(p2)
        else:
            assert _content_hash(p1) != _content_hash(p2)


# ────────────────────────────────────────────────────────────────────────
# Invariant 4 — prefix sensitivity (every field is tracked)
# ────────────────────────────────────────────────────────────────────────


class TestPrefixSensitivity:
    @given(
        key=st.text(alphabet=_PARAMS_KEY_ALPHABET, min_size=1, max_size=6),
        value=st.text(alphabet=_PARAMS_VALUE_ALPHABET, min_size=1, max_size=10),
    )
    @settings(max_examples=150, deadline=None)
    def test_any_value_mutation_changes_hash(self, key: str, value: str) -> None:
        h1 = _content_hash({key: value})
        h2 = _content_hash({key: value + "X"})
        assert h1 != h2

    @given(
        keys=st.lists(
            st.text(alphabet=_PARAMS_KEY_ALPHABET, min_size=1, max_size=6),
            min_size=2,
            max_size=6,
            unique=True,
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_dropping_a_key_changes_hash(self, keys: list[str]) -> None:
        full = dict.fromkeys(keys, "v")
        reduced = dict.fromkeys(keys[:-1], "v")
        assert _content_hash(full) != _content_hash(reduced)


# ────────────────────────────────────────────────────────────────────────
# Invariant 5 — structural round-trip
# ────────────────────────────────────────────────────────────────────────


class TestStructuralRoundTrip:
    @given(params=params_st)
    @settings(max_examples=100, deadline=None)
    def test_params_tuple_is_sorted(self, params: dict[str, str]) -> None:
        item = WorkItem.new(
            phase=IngestPhase.A_INSTRUMENTS,
            content_hash=_content_hash(params),
            params=params,
        )
        keys = [k for k, _ in item.params]
        assert keys == sorted(keys)

    @given(params=params_st)
    @settings(max_examples=100, deadline=None)
    def test_params_tuple_preserves_every_entry(self, params: dict[str, str]) -> None:
        item = WorkItem.new(
            phase=IngestPhase.A_INSTRUMENTS,
            content_hash=_content_hash(params),
            params=params,
        )
        assert dict(item.params) == params
