"""Golden-master regression test for :mod:`brutex.orchestrator.bloom`.

Catches accidental drift in the underlying ``fastbloom_rs`` bit-derivation
algorithm or our wrapper's sizing math. Locks the bit-array output for a
fixed deterministic input sequence so that any change to:

* ``fastbloom_rs`` major version (bit layout shift),
* ``BloomFilter`` constructor sizing math (``m_bits`` / ``k`` derivation),
* the input hash convention (``add_bytes`` vs other modes),

produces a different ``sha256`` digest of the serialized bit array and
fails this test loudly. Operator can then evaluate whether the change is
intentional (update the golden) or a regression (fix the code).

DEC-P0-049 / PR-J.
"""

from __future__ import annotations

import hashlib

from brutex.orchestrator.bloom import BloomFilter

# Locked golden digest. Regenerate via
#   python -c 'from brutex.orchestrator.bloom import BloomFilter; ...'
# only when a deliberate sizing / backend change is made; document the
# regeneration in the corresponding MIGRATION.md DEC-P0-NNN entry.
_GOLDEN_CAPACITY = 1000
_GOLDEN_FPR_TARGET = 1e-6
_GOLDEN_INPUTS = 500
_GOLDEN_M_BITS = 28756
_GOLDEN_K = 20
_GOLDEN_SHA256 = "969f03cc51263d8e5d43c6e6ad2ea9eb098f6eccdae44de758fc5bd0d6abcb70"


def test_bloom_bit_array_golden() -> None:
    """Insert 500 deterministic sha256 hashes; sha256 the bit array."""
    bf = BloomFilter(capacity=_GOLDEN_CAPACITY, fpr_target=_GOLDEN_FPR_TARGET)
    assert bf.m_bits == _GOLDEN_M_BITS
    assert bf.k == _GOLDEN_K
    for i in range(_GOLDEN_INPUTS):
        bf.add(hashlib.sha256(f"item:{i}".encode()).digest())
    # Serialise the bit array. Accessing the underlying impl directly
    # is intentional — the golden contract is "bit-for-bit equality of
    # the wire format" and is the operative regression oracle.
    raw_bits = bf._impl.get_bytes()
    digest = hashlib.sha256(raw_bits).hexdigest()
    assert digest == _GOLDEN_SHA256, (
        f"Bloom golden digest drift:\n"
        f"  expected: {_GOLDEN_SHA256}\n"
        f"  observed: {digest}\n"
        f"If this change is intentional, update _GOLDEN_SHA256 in "
        f"tests/golden/test_bloom_golden.py and reference the corresponding "
        f"DEC-P0-NNN entry in docs/MIGRATION.md."
    )


def test_bloom_serialized_size_matches_m_bytes() -> None:
    """Sanity check: the wire-format size matches the wrapper's m_bytes
    (modulo any header bytes the impl adds)."""
    bf = BloomFilter(capacity=_GOLDEN_CAPACITY, fpr_target=_GOLDEN_FPR_TARGET)
    raw = bf._impl.get_bytes()
    # fastbloom-rs serialises the bit array directly; size ≥ m_bytes.
    # (Small variance OK — impl may pad to word boundaries.)
    assert len(raw) >= bf.m_bytes
    assert len(raw) <= bf.m_bytes + 64  # bounded slack for any header
