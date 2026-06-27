"""Golden corpus #9 — combo-hash distinct inputs (hash uniqueness, P-01/P-02).

DEC-PHASE2-CORPORA-001 (``tests/golden/fixtures/README.md`` corpus #9 —
"combo-hash distinct inputs / hash uniqueness, P-01, P-02").

Drives the REAL 9-field BLAKE3 combo identity
(:func:`brutex.strategies.combo.combo_hash`, FR-BF-003 / DEC-P0-060) and **byte-
freezes** its output. Where ``tests/property/test_combo_hash_uniqueness.py``
proves the invariants *generatively* (Hypothesis), this golden pins the EXACT
hex digests of a hand-picked corpus so any change to the canonicalisation, the
field order, the ``\\x1f`` separator, or the BLAKE3 primitive is caught
byte-for-byte (the README "SHA-256 manifest" discipline, here the digests
themselves).

The corpus is a base combo plus **nine single-field variants** — one per identity
field (direction, instrument ×2, tf, mode, params, data_snapshot_hash,
indicator_version, code_commit_sha) — so the frozen set jointly proves:

* **P-01 determinism** — recomputing any combo reproduces its pinned digest
  (BLAKE3 single-tree is deterministic; Golden Rule #4).
* **P-02 per-field sensitivity + no-duplicate emission** — flipping any one of
  the nine fields changes the digest, and the 10 distinct combos hash to 10
  distinct digests (the dedup contract the L1 Bloom + L2 LMDB tiers rely on).
* **params order-invariance** — the canonicaliser (``sort_keys=True``) makes
  param insertion order irrelevant (NFR-UNIQ-002).

BLAKE3 ships only in the Rust ``fno_math`` wheel, so this test runs under CI
(3.12, wheels built); the digests were frozen from that same wheel.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from brutex.strategies.combo import canonicalise_params, combo_hash


def _hash(fields: Mapping[str, Any]) -> str:
    """Call the real 9-field ``combo_hash`` from a field mapping (typed seam)."""
    return combo_hash(
        strategy_id=fields["strategy_id"],
        direction=fields["direction"],
        instrument=fields["instrument"],
        tf=fields["tf"],
        mode=fields["mode"],
        params=fields["params"],
        data_snapshot_hash=fields["data_snapshot_hash"],
        indicator_version=fields["indicator_version"],
        code_commit_sha=fields["code_commit_sha"],
    )


#: The base combo's 9 identity fields.
_BASE: dict[str, Any] = {
    "strategy_id": "orb",
    "direction": "bull",
    "instrument": "NIFTY",
    "tf": "5m",
    "mode": "A",
    "params": {"lookback": 20, "mult": 1.5},
    "data_snapshot_hash": "snap-2021-2026",
    "indicator_version": "ind-v1",
    "code_commit_sha": "deadbeef",
}

#: Each variant overrides exactly one field of ``_BASE`` (so all 10 are distinct).
_VARIANTS: dict[str, dict[str, Any]] = {
    "base": _BASE,
    "direction_bear": {**_BASE, "direction": "bear"},
    "instrument_banknifty": {**_BASE, "instrument": "BANKNIFTY"},
    "instrument_sensex": {**_BASE, "instrument": "SENSEX"},
    "tf_15m": {**_BASE, "tf": "15m"},
    "mode_b": {**_BASE, "mode": "B"},
    "params_mult": {**_BASE, "params": {"lookback": 20, "mult": 2.0}},
    "snapshot": {**_BASE, "data_snapshot_hash": "snap-other"},
    "indicator": {**_BASE, "indicator_version": "ind-v2"},
    "commit": {**_BASE, "code_commit_sha": "cafef00d"},
}

#: The frozen 64-char BLAKE3 hex digests (the byte-equality manifest).
_EXPECTED_DIGESTS: dict[str, str] = {
    "base": "c48b382dc97f66d03859fac94a82ae7de0347ec55761e9bf9d29cf367453ecd6",
    "direction_bear": "9f8873e752f0816121051f110965fc80c6d9e1219484ccd7bd4927a3425a18ed",
    "instrument_banknifty": "b738a17448c75ce39d48c1eab9823534ab09441271ea98453cb595c9a9a9cfcc",
    "instrument_sensex": "83aec2d9c79511bf4315cc848a48e35bed371b20f03a5a785efde086c9fded61",
    "tf_15m": "5b4b83b6fd72ced638a466ccb9724c9a7bc5a01fe31a4558ddf7f5d6f5b9fed6",
    "mode_b": "16cc607135c6e7709befc8e9d295a65be32b2b29d2f679fc581a0eb1e4a810bb",
    "params_mult": "8f8d3c7972e172873547c80036cf44d06fbd543f61c193d000981cf7f0910e38",
    "snapshot": "b8c016410437fae44a0a14359a145825bd65a800d336f096063e9ba84601f977",
    "indicator": "e584e9da4a4986b2fc692c280bac7cb62fde1a68e2f16a17ed132bd9d4f16670",
    "commit": "c5bfa82494a9a7753fa7f2c69e7dccac8c0995d0e40a1b5e56a48af640422862",
}


def test_every_combo_matches_its_frozen_digest() -> None:
    """P-01: each combo reproduces its pinned BLAKE3 digest byte-for-byte."""
    for name, fields in _VARIANTS.items():
        assert _hash(fields) == _EXPECTED_DIGESTS[name], name


def test_all_digests_are_distinct() -> None:
    """P-02: 10 distinct single-field-varied combos hash to 10 distinct digests."""
    digests = [_hash(fields) for fields in _VARIANTS.values()]
    assert len(set(digests)) == len(digests) == 10


def test_every_digest_is_64_lowercase_hex() -> None:
    """BLAKE3-256 output shape: 64 lowercase hex chars."""
    for digest in _EXPECTED_DIGESTS.values():
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


def test_params_insertion_order_does_not_change_the_digest() -> None:
    """NFR-UNIQ-002: param key order is canonicalised away."""
    reordered = {**_BASE, "params": {"mult": 1.5, "lookback": 20}}
    assert _hash(reordered) == _EXPECTED_DIGESTS["base"]


# ────────────────────────────────────────────────────────────────────────
# Hardening safety proof — the REJECT-ONLY guards add no valid digest change.
#
# DEC-STRATEGIES-CANONICAL-HARDEN: combo.py gained \x1f-injection rejection,
# non-finite float/Decimal rejection, a float-subclass guard, a top-level
# Mapping guard, and a nesting-depth cap — all REJECT-ONLY (they raise for
# never-valid input BEFORE producing any byte). The unchanged ``_BASE`` digest
# below is the byte-for-byte proof that no VALID combo's identity moved.
# ────────────────────────────────────────────────────────────────────────


def test_base_digest_unchanged_after_hardening() -> None:
    """Safety proof: the wire-locked ``_BASE`` digest survives the hardening."""
    assert (
        _hash(_BASE)
        == _EXPECTED_DIGESTS["base"]
        == "c48b382dc97f66d03859fac94a82ae7de0347ec55761e9bf9d29cf367453ecd6"
    )


# ────────────────────────────────────────────────────────────────────────
# Wire-locked canonical-STRING pins (frozen ``canonicalise_params`` output).
#
# These freeze the canonicalisation surface directly (not a blake3 digest, so
# no hand-computed hash is needed). A change to any of these is a wire break.
# ────────────────────────────────────────────────────────────────────────


def test_wire_locked_alias_frozen() -> None:
    """float / str / Decimal carrying the same text collapse to ONE token."""
    assert canonicalise_params({"sl": 1.5}) == '{"sl":"1.5"}'
    assert canonicalise_params({"sl": "1.5"}) == '{"sl":"1.5"}'
    assert canonicalise_params({"sl": Decimal("1.5")}) == '{"sl":"1.5"}'


def test_signed_zero_frozen() -> None:
    """Signed zero is COLLAPSED to +0.0 (DEC-SWEEP-DEDUP-LMDB-001) — two
    mathematically-equal zero params share ONE run-identity (Golden Rule #4
    reproducibility); ``-0.0`` normalises to ``"0.0"`` BEFORE repr."""
    assert canonicalise_params({"x": 0.0}) == '{"x":"0.0"}'
    assert canonicalise_params({"x": -0.0}) == '{"x":"0.0"}'


def test_decimal_scale_frozen() -> None:
    """Decimal scale is significant — each textual form is its own token."""
    assert canonicalise_params({"x": Decimal("1.0")}) == '{"x":"1.0"}'
    assert canonicalise_params({"x": Decimal("1.00")}) == '{"x":"1.00"}'
    assert canonicalise_params({"x": Decimal("1E+2")}) == '{"x":"1E+2"}'
    assert canonicalise_params({"x": Decimal("100")}) == '{"x":"100"}'


def test_unicode_param_frozen() -> None:
    """Non-ASCII text is escaped to its ``\\uXXXX`` ensure_ascii form."""
    assert canonicalise_params({"label": "naïveé"}) == '{"label":"na\\u00efve\\u00e9"}'
