"""Hypothesis property tests for the sweep dedup digest (`brutex.engine.sweep_cache`).

The membership-dedup cache key is the BLAKE3 digest of the canonicalised combo
identity (:func:`brutex.engine.sweep_cache.combo_digest`). These properties pin
the two contracts the dedup correctness rests on (CLAUDE.md §2 Rule 4 / Rule 11):

1. **Determinism + key-order invariance.** The same *logical* combo — identical
   ``(mask, instrument, tf, direction, params)`` AND identical reproducibility
   hashes, regardless of the ``params`` dict insertion order — always maps to
   the SAME 32-byte digest, so a re-run skips the same combos it skipped before
   (idempotence).
2. **One-NaN-policy (DEC-ENGINE-DEDUP-NAN-CANON-001).** A combo whose ``params``
   contain a non-finite float (``NaN`` / ``inf`` / ``-inf``) ALWAYS raises
   :class:`~brutex.engine.sweep_cache.SweepCacheError` — the membership key can
   never silently admit a non-finite float that the run-identity hash
   (`combo.canonicalise_params`) and the completion sentinel
   (`tuple_complete.render_completion_sentinel`) would refuse.
3. **Reproducibility-hash sensitivity (DEC-COMBO-DIGEST-REPRO-HASHES-001).**
   The three structural hashes (``data_snapshot_hash`` / ``indicator_version``
   / ``code_commit_sha``) ALWAYS participate in the digest: changing any one of
   them re-keys the combo, so a combo evaluated on OLD data is never falsely
   skipped on NEW data (the stale-skip bug the structural parameters kill).
"""

from __future__ import annotations

import math

import pytest
from brutex.engine.sweep_cache import SweepCacheError, combo_digest
from hypothesis import given, settings
from hypothesis import strategies as st

# A label alphabet that excludes nothing structural — combo_digest JSON-quotes
# every field, so any printable string is a valid context label.
_LABELS = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
    min_size=0,
    max_size=20,
)

# JSON-serialisable, ALWAYS-FINITE param values (the keys are str; values are
# ints / finite floats / strings / bools / None).
_FINITE_VALUES = st.one_of(
    st.integers(min_value=-(2**32), max_value=2**32),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(max_size=20),
    st.booleans(),
    st.none(),
)

# Param keys are capped at 12 chars — STRUCTURALLY shorter than every reserved
# reproducibility key (the shortest, ``code_commit_sha``, is 15 chars), so a
# generated dict can never collide with the reserved-key guard of
# DEC-COMBO-DIGEST-REPRO-HASHES-001 (which has its own dedicated tests).
_FINITE_PARAMS = st.dictionaries(
    keys=st.text(min_size=1, max_size=12),
    values=_FINITE_VALUES,
    max_size=8,
)

#: Fixed reproducibility hashes for the properties that vary OTHER inputs.
_REPRO_KW: dict[str, str] = {
    "data_snapshot_hash": "snap-prop",
    "indicator_version": "ind-prop",
    "code_commit_sha": "sha-prop",
}


@given(
    mask=st.integers(min_value=0, max_value=(1 << 52) - 1),
    instrument=_LABELS,
    tf=_LABELS,
    direction=_LABELS,
    params=_FINITE_PARAMS,
)
@settings(max_examples=200, deadline=None)
def test_digest_is_deterministic_and_key_order_independent(
    mask: int,
    instrument: str,
    tf: str,
    direction: str,
    params: dict[str, object],
) -> None:
    """Same logical combo → same digest, regardless of ``params`` insertion order."""
    first = combo_digest(mask, instrument, tf, direction, params, **_REPRO_KW)
    # Re-call with the identical inputs → byte-identical (determinism).
    assert combo_digest(mask, instrument, tf, direction, params, **_REPRO_KW) == first
    # Re-insert the params in REVERSED key order → still byte-identical (the
    # canonicaliser sorts keys, so insertion order is irrelevant).
    reordered = dict(reversed(list(params.items())))
    assert combo_digest(mask, instrument, tf, direction, reordered, **_REPRO_KW) == first
    assert len(first) == 32


@given(
    mask=st.integers(min_value=0, max_value=(1 << 52) - 1),
    instrument=_LABELS,
    tf=_LABELS,
    direction=_LABELS,
    bad=st.sampled_from([float("nan"), float("inf"), float("-inf")]),
    other_params=_FINITE_PARAMS,
)
@settings(max_examples=100, deadline=None)
def test_digest_non_finite_float_always_raises(
    mask: int,
    instrument: str,
    tf: str,
    direction: str,
    bad: float,
    other_params: dict[str, object],
) -> None:
    """A non-finite float anywhere in ``params`` ALWAYS raises SweepCacheError."""
    assert not math.isfinite(bad)
    params = {**other_params, "__non_finite__": bad}
    with pytest.raises(SweepCacheError, match="non-finite float"):
        combo_digest(mask, instrument, tf, direction, params, **_REPRO_KW)


# ── Reproducibility-hash sensitivity (DEC-COMBO-DIGEST-REPRO-HASHES-001) ─────


@given(
    mask=st.integers(min_value=0, max_value=(1 << 52) - 1),
    instrument=_LABELS,
    tf=_LABELS,
    direction=_LABELS,
    params=_FINITE_PARAMS,
    snap_a=_LABELS,
    snap_b=_LABELS,
    ind_a=_LABELS,
    ind_b=_LABELS,
    sha_a=_LABELS,
    sha_b=_LABELS,
)
@settings(max_examples=200, deadline=None)
def test_digest_always_depends_on_each_reproducibility_hash(
    mask: int,
    instrument: str,
    tf: str,
    direction: str,
    params: dict[str, object],
    snap_a: str,
    snap_b: str,
    ind_a: str,
    ind_b: str,
    sha_a: str,
    sha_b: str,
) -> None:
    """Changing ANY one reproducibility hash ALWAYS changes the digest.

    The stale-skip kill-switch: a dedup entry recorded under one data snapshot
    / indicator version / code commit can never alias the same logical combo
    under a different one (CLAUDE.md §2 Golden Rule #4 run-identity formula).
    """

    def digest(snap: str, ind: str, sha: str) -> bytes:
        return combo_digest(
            mask,
            instrument,
            tf,
            direction,
            params,
            data_snapshot_hash=snap,
            indicator_version=ind,
            code_commit_sha=sha,
        )

    base = digest(snap_a, ind_a, sha_a)
    if snap_b != snap_a:
        assert digest(snap_b, ind_a, sha_a) != base
    if ind_b != ind_a:
        assert digest(snap_a, ind_b, sha_a) != base
    if sha_b != sha_a:
        assert digest(snap_a, ind_a, sha_b) != base


# ── STEP-1 bounded auto-grow (`_auto_grow_capacity`, DEC-ENGINE-MISC-PACK-001) ─


@given(
    capacity=st.integers(min_value=1, max_value=10**8),
    persisted=st.integers(min_value=0, max_value=10**10),
    fpr=st.sampled_from([1e-9, 1e-7, 1e-3]),
)
@settings(max_examples=200, deadline=None)
def test_auto_grow_covers_persisted_and_respects_the_wall_or_raises(
    capacity: int, persisted: int, fpr: float
) -> None:
    """The grow invariant: the result always covers *persisted* AND fits the
    16 GiB wall — or the call FAILS_CLOSED (never a silent over-wall Bloom,
    never an under-sized capacity that would overflow mid-rebuild)."""
    from brutex.engine.sweep_cache import SweepCacheCapacityError, _auto_grow_capacity
    from brutex.orchestrator.bloom import MAX_BLOOM_BYTES, _compute_m_bytes

    try:
        grown = _auto_grow_capacity(capacity, persisted, fpr)
    except SweepCacheCapacityError:
        # The raise arm fires ONLY when a genuine grow was needed and its
        # sizing busts the wall — re-derived independently here. The exact
        # ``persisted == capacity`` boundary joins the FAIL_CLOSED arm
        # (DEC-SWEEP-BLOOM-EXACT-BOUNDARY-001).
        assert persisted >= capacity
        would_grow = max(math.ceil(persisted * 1.25), math.ceil(capacity * 1.25))
        assert _compute_m_bytes(would_grow, fpr) > MAX_BLOOM_BYTES
    else:
        assert grown >= persisted
        if persisted < capacity:
            assert grown == capacity  # identity arm — byte-identical behaviour.
        else:
            # C6 fix + the exact-boundary fix (DEC-SWEEP-BLOOM-EXACT-BOUNDARY-001):
            # the grow leaves HEADROOM above *persisted* (25%) so a resumed
            # sweep records NEW tuples, not just rebuilds — the old
            # ``max(persisted, …)`` sized to exactly *persisted* (zero headroom),
            # and the old ``<=`` identity arm let ``persisted == capacity`` open
            # a FULL Bloom. ``grown > persisted`` holds at the boundary too
            # (ceil(1 × 1.25) = 2).
            assert grown == max(math.ceil(persisted * 1.25), math.ceil(capacity * 1.25))
            assert grown > persisted
            assert _compute_m_bytes(grown, fpr) <= MAX_BLOOM_BYTES
