"""Frozen-bytes fold-corpus goldens (DEC-SWEEP-CORE-RUST-001, MIGRATION.md #1739).

Design §1(a): the corpus at ``tests/golden/fixtures/sweep_core_fold_corpus.json``
is BYTES — minted once by ``scripts/dev/mint_sweep_core_fold_corpus.py`` on
CPython 3.12 (the ``builtins.sum`` Neumaier authority, gh-100425), then
committed. This suite verifies:

1. **Tamper pin** — the fixture's sha256 equals the committed ``.sha256``.
2. **Reference ≡ corpus** — the §1(b) hand-rolled stdlib reference
   (:mod:`tests.rust_differential._reference_fold`) reproduces every
   record's frozen ints/floats/flags BYTE-for-byte. Because the corpus
   Neumaier expectations were minted with the REAL ``builtins.sum``, this
   simultaneously proves the hand-rolled guarded-final-add loop ==
   CPython's compensated summation on every vector (the Rust port's
   porting authority is thereby cross-checked, never trusted).
3. **Helper vectors** — Neumaier / Pébay moments / two-branch lerp each
   reproduce their frozen bit patterns on the streams trades cannot reach
   (−0.0 ties, denormals, the ``[1e16, 1.0, −1e16]`` compensation classic).

The Rust arm consumes the SAME numbers via the generated
``rust/fno-math/src/sweep_core_vectors.rs`` in-crate consts (the
``cargo test`` vector suite) — the corpus's ``fold_block`` teeth live there,
where the fold core is callable without the walk.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any, Final

import pytest

from tests.rust_differential._reference_fold import (
    _ref_moments,
    _ref_neumaier_sum,
    _ref_percentile,
    reference_fold_row,
)

FIXTURES: Final[Path] = Path(__file__).resolve().parent / "fixtures"
CORPUS_PATH: Final[Path] = FIXTURES / "sweep_core_fold_corpus.json"
SHA_PATH: Final[Path] = FIXTURES / "sweep_core_fold_corpus.json.sha256"


def _corpus() -> dict[str, Any]:
    """Load the frozen corpus document."""
    loaded = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _unpack_f64(hex_le: str) -> float:
    """One ``'<d'`` LE hex → float (bit-preserving)."""
    (value,) = struct.unpack("<d", bytes.fromhex(hex_le))
    return float(value)


class TestCorpusIntegrity:
    def test_sha256_pin(self) -> None:
        """The fixture bytes match the committed digest (tamper pin)."""
        digest = hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest()
        assert digest == SHA_PATH.read_text(encoding="utf-8").strip()

    def test_schema_and_population(self) -> None:
        """Schema tag + the minted population floor + minting authority."""
        corpus = _corpus()
        assert corpus["schema"] == "sweep-core-fold-corpus-v1"
        assert corpus["minted_with"].startswith("CPython 3.12")
        records = corpus["records"]
        assert len(records) >= 180
        flags = {rec["flags"] for rec in records}
        # All three flag classes are populated: plain, zero-trade, bit1.
        assert flags == {0, 1, 2}
        assert len(corpus["helper_vectors"]) >= 30

    def test_record_shapes(self) -> None:
        """Every record carries the 15×8/9×8 strides + 5-element trades."""
        for rec in _corpus()["records"]:
            assert len(bytes.fromhex(rec["ints_hex"])) == 15 * 8, rec["name"]
            assert len(bytes.fromhex(rec["floats_hex"])) == 9 * 8, rec["name"]
            assert all(len(t) == 5 for t in rec["trades"]), rec["name"]
            assert rec["n_bars"] >= 1
            assert rec["start_equity_paisa"] >= 1


class TestReferenceReproducesCorpus:
    def test_every_record_bit_exact(self) -> None:
        """The hand-rolled reference == frozen bytes on ALL records."""
        for rec in _corpus()["records"]:
            trades = [tuple(t) for t in rec["trades"]]
            ints, floats, flags = reference_fold_row(
                trades, rec["n_bars"], rec["start_equity_paisa"]
            )
            got_ints = struct.pack("<15q", *ints).hex()
            got_floats = struct.pack("<9d", *floats).hex()
            assert (got_ints, got_floats, flags) == (
                rec["ints_hex"],
                rec["floats_hex"],
                rec["flags"],
            ), rec["name"]

    def test_f3_negative_net_pnl_cell_is_present_and_plain(self) -> None:
        """The F3 break-oracle cell (negative net P&L) exists + folds plain."""
        rec = next(r for r in _corpus()["records"] if r["name"] == "negative_net_pnl_f3_cell")
        ints = struct.unpack("<15q", bytes.fromhex(rec["ints_hex"]))
        assert ints[1] < 0  # net_pnl strictly negative — the floor-div hazard feed
        assert rec["flags"] == 0

    def test_envelope_boundary_cells(self) -> None:
        """2^52 exactly is PLAIN; 2^52 + 1 is python_required (strict ``>``)."""
        by_name = {r["name"]: r for r in _corpus()["records"]}
        assert by_name["envelope_exact_2_52"]["flags"] == 0
        assert by_name["envelope_over_2_52_plus_1"]["flags"] == 2
        # A bit1 row is FULLY ZEROED — Python never reads it.
        over = by_name["envelope_over_2_52_plus_1"]
        assert set(struct.unpack("<15q", bytes.fromhex(over["ints_hex"]))) == {0}
        assert set(struct.unpack("<9d", bytes.fromhex(over["floats_hex"]))) == {0.0}


@pytest.fixture(scope="module")
def vectors() -> list[dict[str, Any]]:
    """The frozen helper-level float vectors."""
    return list(_corpus()["helper_vectors"])


class TestHelperVectors:
    def test_neumaier_matches_cpython_sum_bits(self, vectors: list[dict[str, Any]]) -> None:
        """Hand-rolled guarded Neumaier == the frozen ``builtins.sum`` bits."""
        for vec in vectors:
            values = [_unpack_f64(h) for h in vec["values_hex"]]
            got = struct.pack("<d", _ref_neumaier_sum(values)).hex()
            assert got == vec["neumaier_hex"], vec["name"]

    def test_neumaier_compensation_classic_is_nonzero(self, vectors: list[dict[str, Any]]) -> None:
        """The ``[1e16, 1.0, −1e16]`` classic reads 1.0 — a naive fold reads 0.0.

        The anti-vacuity tooth: if the corpus had been minted with a naive
        left fold this vector would be 0.0 and the whole Neumaier lane
        would be certifying the wrong algorithm.
        """
        classic = next(v for v in vectors if v["name"] == "neumaier_classic_1e16")
        assert _unpack_f64(classic["neumaier_hex"]) == 1.0

    def test_moments_match_bits(self, vectors: list[dict[str, Any]]) -> None:
        """Pébay moments reproduce the frozen 4×f64 bit patterns."""
        for vec in vectors:
            values = [_unpack_f64(h) for h in vec["values_hex"]]
            got = struct.pack("<4d", *_ref_moments(values)).hex()
            assert got == vec["moments_hex"], vec["name"]

    def test_percentiles_match_bits(self, vectors: list[dict[str, Any]]) -> None:
        """The two-branch lerp reproduces the frozen p95/p5 over sorted values."""
        for vec in vectors:
            values = [_unpack_f64(h) for h in vec["values_hex"]]
            if not values:
                continue  # percentile is defined on non-empty series only
            ordered = sorted(values)
            assert struct.pack("<d", _ref_percentile(ordered, 95.0)).hex() == vec["p95_hex"], vec[
                "name"
            ]
            assert struct.pack("<d", _ref_percentile(ordered, 5.0)).hex() == vec["p5_hex"], vec[
                "name"
            ]

    def test_negzero_tie_vectors_present(self, vectors: list[dict[str, Any]]) -> None:
        """The −0.0-tie + denormal streams exist (the sort/lerp teeth feed)."""
        names = {v["name"] for v in vectors}
        assert {"negzero_ties_interior", "negzero_pair_tail", "denormal_first"} <= names
