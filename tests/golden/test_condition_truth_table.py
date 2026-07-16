"""Golden consumer test for the condition truth table.

DEC-PHASE2-TRUTH-TABLE-001 (Phase 2, ``docs/TRACK1_PHASE_PLAN.md`` corpus #7 —
"condition truth table / per-condition determinism (G9)"; the table now
spans the 69-declared / 65-active layout — rows exist only for IMPLEMENTED
bits).

``tests/golden/condition_truth_table.csv`` is the flat, human-auditable
projection of the frozen ``input → u128`` oracle
(``fixtures/vocab_golden_vectors.json``) emitted by
``scripts/gen_condition_truth_table.py``. This module is its golden gate. It
asserts, with NO call to the evaluator (so it runs even where the Rust
``trading_core`` wheel is absent — the values are decoded from the already-proven
``expected_mask`` in the corpus):

* the table is well-formed (canonical header, ≥ ``MIN_ROWS`` rows, no dup
  ``(snippet, condition)`` pair, only ``true``/``false`` booleans);
* every ``condition_name`` is a real vocabulary condition and its
  ``bit_position`` matches the ONE source of truth, ``brutex/vocabulary/bits.py``;
* every ``expected_bool`` equals the bit decoded from the referenced golden
  case's frozen ``expected_mask`` — so the table cannot drift from the oracle;
* the G9 both-ways guarantee: each implemented condition fires ``true`` at least
  once AND ``false`` at least once across the table, and all are present;
* no drift: the on-disk CSV is byte-identical to a fresh generation, and its
  SHA-256 matches the pinned manifest (the README's "SHA-256 manifest" rule).

Because the contract ``evaluate_atoms == expected_mask`` is itself locked by
``test_vocab_golden.py`` (which runs the live evaluator in CI), this CSV is
transitively a golden assertion against the real evaluator while staying
evaluator-free here.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover — sys.path may already carry the root
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import gen_condition_truth_table as gen  # noqa: E402  (needs the path shim above)

_CSV_PATH = Path(__file__).parent / "condition_truth_table.csv"
_VECTORS_PATH = Path(__file__).parent / "fixtures" / "vocab_golden_vectors.json"

#: SHA-256 of the frozen truth table (the README "SHA-256 manifest" drift guard).
#: Regenerate with ``python scripts/gen_condition_truth_table.py`` then update this.
# DELIBERATE RE-LOCK (the 60→66 vocab reshape): the table now projects the
# IMPLEMENTED bits per selected case; the tombstoned VWAP bits (52/53) + the
# declared-only SuperTrend bits (64/65) carry no row. Old digest:
# e2d89a4e5e1afd6b8c547378349ab0d45f2ff311f097b3040cff13152fbc3c33.
# RE-LOCKED again at DEC-GAP-MIDPOINT-CONDITIONS-001 (the fixture regen
# appended the three Group-15 gap-midpoint conditions at bits 66-68 — the
# diff is +66 rows, additive; 22 cases × 65 implemented bits = 1430 rows).
# Pre-gap digest: ea1671b986d5a95dfd304432d8d0e8c6c90c261c59e68c1264067728973df126.
_EXPECTED_SHA256 = "0438918c703850f666b9ff47c176e1b7fc1b8f35b76e8ea0dd2d2b49314e34c0"


def _read_rows() -> list[dict[str, str]]:
    """Parse the on-disk truth table into a list of column dicts."""
    with _CSV_PATH.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _cases_by_id() -> dict[int, int]:
    """Map golden case id → its frozen mask int (from the ``expected_mask`` hex)."""
    data = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))
    return {case["id"]: int(case["expected_mask"], 16) for case in data["cases"]}


def _snippet_to_case_id(snippet_id: str) -> int:
    """Parse ``CASE-007`` → ``7`` (the golden case id the row was decoded from)."""
    prefix, _, number = snippet_id.partition("-")
    assert prefix == "CASE", f"unexpected snippet_id prefix: {snippet_id!r}"
    return int(number)


class TestTruthTableStructure:
    """The CSV is well-formed and large enough."""

    def test_header_is_canonical(self) -> None:
        """The first line is exactly the generator's header."""
        with _CSV_PATH.open(encoding="utf-8", newline="") as handle:
            header = next(csv.reader(handle))
        assert tuple(header) == gen.HEADER

    def test_has_at_least_min_rows(self) -> None:
        """The table meets the ≥ MIN_ROWS (200) phase-plan floor."""
        rows = _read_rows()
        assert len(rows) >= gen.MIN_ROWS

    def test_no_duplicate_snippet_condition_pairs(self) -> None:
        """Each (snippet, condition) coordinate appears at most once."""
        rows = _read_rows()
        pairs = [(r["snippet_id"], r["condition_name"]) for r in rows]
        assert len(pairs) == len(set(pairs))

    def test_expected_bool_is_strictly_true_or_false(self) -> None:
        """The boolean column is only the lowercase literals ``true`` / ``false``."""
        rows = _read_rows()
        assert {r["expected_bool"] for r in rows} == {"true", "false"}


class TestTruthTableMatchesRegistry:
    """Every row's condition + bit agrees with ``brutex/vocabulary/bits.py``."""

    def test_condition_names_and_bits_match_source_of_truth(self) -> None:
        """``condition_name`` is real and ``bit_position`` matches the registry."""
        bit_to_name, total, _impl = gen.load_bit_registry()
        name_to_bit = {name: bit for bit, name in bit_to_name.items()}
        for row in _read_rows():
            name = row["condition_name"]
            assert name in name_to_bit, f"unknown condition {name!r}"
            assert int(row["bit_position"]) == name_to_bit[name]
            assert 0 <= int(row["bit_position"]) < total


class TestTruthTableMatchesOracle:
    """Every ``expected_bool`` is decoded from the frozen oracle u128 mask."""

    def test_every_row_matches_the_frozen_u64(self) -> None:
        """``expected_bool`` == bit of the referenced case's ``expected_mask``."""
        cases = _cases_by_id()
        mismatches: list[str] = []
        for row in _read_rows():
            case_id = _snippet_to_case_id(row["snippet_id"])
            assert case_id in cases, f"snippet {row['snippet_id']} has no golden case"
            bit = int(row["bit_position"])
            decoded = bool((cases[case_id] >> bit) & 1)
            expected = row["expected_bool"] == "true"
            if decoded != expected:
                mismatches.append(
                    f"{row['snippet_id']}/{row['condition_name']}@{bit}: "
                    f"csv={expected} oracle={decoded}"
                )
        assert not mismatches, "truth-table drift from oracle:\n" + "\n".join(mismatches[:20])


class TestTruthTableBothWaysCoverage:
    """G9 — every IMPLEMENTED condition is exercised true AND false."""

    def test_all_implemented_conditions_fire_and_clear(self) -> None:
        """Each IMPLEMENTED condition appears with ``true`` and with ``false``.

        LAYOUT-ONLY conditions (``implemented=false``) are excluded — the
        evaluator never sets them, so they carry no truth-table row.
        """
        bit_to_name, _total, implemented_mask = gen.load_bit_registry()
        impl_names = {bit_to_name[b] for b in bit_to_name if (implemented_mask >> b) & 1}
        rows = _read_rows()
        seen_true: set[str] = set()
        seen_false: set[str] = set()
        for row in rows:
            (seen_true if row["expected_bool"] == "true" else seen_false).add(row["condition_name"])
        assert seen_true == impl_names, f"never true: {sorted(impl_names - seen_true)}"
        assert seen_false == impl_names, f"never false: {sorted(impl_names - seen_false)}"
        # No LAYOUT-ONLY condition leaked a row into the table.
        layout_names = set(bit_to_name.values()) - impl_names
        assert (seen_true | seen_false).isdisjoint(layout_names)


class TestTruthTableNoDrift:
    """The on-disk artifact is exactly what the generator produces."""

    def test_csv_is_byte_identical_to_a_fresh_generation(self) -> None:
        """Re-running the generator yields the committed bytes (deterministic)."""
        assert _CSV_PATH.read_text(encoding="utf-8") == gen.generate()

    def test_sha256_matches_pinned_manifest(self) -> None:
        """The committed CSV's SHA-256 matches the pinned drift manifest."""
        digest = hashlib.sha256(_CSV_PATH.read_bytes()).hexdigest()
        assert digest == _EXPECTED_SHA256
