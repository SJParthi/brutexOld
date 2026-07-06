"""Golden byte-stability freeze for the sweep dedup digest.

DEC-ENGINE-DEDUP-NAN-CANON-001 froze the canonicalisation after the
``allow_nan=False`` guard landed. DEC-COMBO-DIGEST-REPRO-HASHES-001 then made
the three Golden-Rule-#4 reproducibility hashes (``data_snapshot_hash`` /
``indicator_version`` / ``code_commit_sha``) STRUCTURAL keyword-only
parameters of :func:`brutex.engine.sweep_cache.combo_digest` — previously they
reached the digest only by CONVENTION (the supervisor injected them inside
``canonical_params``), so a caller that forgot them silently ALIASED dedup
entries across data snapshots (a stale-skip bug).

The hex constants below were CAPTURED FROM THE PRE-CHANGE (main) code by
injecting the hashes inside ``canonical_params`` — the legacy convention the
live Bloom/LMDB dedup stores were keyed with. The structural-kwargs API must
reproduce them EXACTLY: byte-identity is the hard contract that existing dedup
stores and run identities are NOT invalidated (Golden Rule #4). Any future
change to the canonicalisation (field order, ``sort_keys``, separators, the
BLAKE3 primitive, the guard, or the internal repro-hash injection layout) that
moves a digest is caught byte-for-byte here.

BLAKE3 ships in the wheel-free ``blake3`` package, so this golden runs anywhere
(no Rust wheel needed — see ``sweep_cache`` module docstring).
"""

from __future__ import annotations

from brutex.engine.sweep_cache import combo_digest

#: Frozen hex of a fixed FINITE combo carrying a FINITE float param
#: (``proximity=0.25``) — exercises the ``allow_nan=False`` path on a finite
#: value. Captured pre-change with the three hashes inside ``canonical_params``.
_GOLDEN_FINITE_FLOAT = "d6605c7476833543f7434b0afd9c570ed87ca724b1529bc0b5f321c4255c634a"

#: Frozen hex of a fixed combo with ``params=None`` — pre-change this was the
#: ``{"params": {<the three hashes>}}`` payload (the hashes were ALWAYS present
#: on the production path); the structural API rebuilds it byte-identically.
_GOLDEN_NONE = "4505201a72dfa1d4c03e51ee9bae00f1ac1cf92b55522ff829bd82e020ee2f0e"


def test_combo_digest_finite_float_byte_stable() -> None:
    """A FINITE float param hashes to the frozen pre-change digest."""
    digest = combo_digest(
        0b1010110,
        "NIFTY",
        "5m",
        "bull",
        {"sl_paisa": 80, "tp_paisa": 50, "prior_n": 3, "proximity": 0.25},
        data_snapshot_hash="snap-aaaa",
        indicator_version="ind-v1",
        code_commit_sha="c0ffee01",
    )
    assert digest.hex() == _GOLDEN_FINITE_FLOAT


def test_combo_digest_none_params_byte_stable() -> None:
    """The ``params=None`` combo hashes to the frozen pre-change digest."""
    digest = combo_digest(
        7,
        "BANKNIFTY",
        "1m",
        "bear",
        None,
        data_snapshot_hash="snap-aaaa",
        indicator_version="ind-v1",
        code_commit_sha="c0ffee01",
    )
    assert digest.hex() == _GOLDEN_NONE
