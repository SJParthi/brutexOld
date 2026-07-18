"""Frozen rate-bridge read-verdict goldens (``DEC-RUST-PULL-STATE-001``, Wave 3H).

The committed fixtures (``rate_bridge_verdicts.json`` + the serde-free
``rate_bridge_kernel_cases.tsv`` projection) were captured by
``generate_rate_bridge_golden.py`` on the PRE-conversion tree — they are the
oracle the Rust port of ``brutex/data/rate_bridge.py``'s read-verdict plane
must satisfy bit-for-bit (Golden Rule #1).  This test recomputes the SAME
tables from the LIVE module via the shared ``_rate_bridge_harness`` and
asserts structural equality, plus pins:

* the TSV is EXACTLY the projection of the JSON (one truth, two encodings —
  the cargo fixture test consumes the TSV, so the two must never drift);
* the load-bearing CPython-quirk rows are genuinely IN the fixture
  (anti-vacuous: the numeric-string accept, the ``schema: 3.0`` accept, the
  NaN-field accept, the bool-root reject, the OverflowError Python-arm);
* every one of the ten required fields has its ABSENT reject row.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.golden._rate_bridge_harness import (
    REQUIRED_FIELDS,
    SCHEMA_VERSION,
    compute_payload,
    compute_tsv,
    f64_bits,
    f64_from_bits,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JSON_FIXTURE = FIXTURES / "rate_bridge_verdicts.json"
TSV_FIXTURE = FIXTURES / "rate_bridge_kernel_cases.tsv"


@pytest.fixture(scope="module")
def frozen() -> dict[str, Any]:
    payload = json.loads(JSON_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


@pytest.fixture(scope="module")
def live() -> dict[str, Any]:
    payload = json.loads(json.dumps(compute_payload(), allow_nan=False))
    assert isinstance(payload, dict)
    return payload


def _row(payload: dict[str, Any], case_id: str) -> dict[str, Any]:
    for row in payload["rows"]:
        if row["id"] == case_id:
            return row
    raise AssertionError(f"case {case_id!r} not in fixture")


class TestFrozenGolden:
    def test_rows_match_frozen(self, frozen: dict[str, Any], live: dict[str, Any]) -> None:
        assert live["rows"] == frozen["rows"]

    def test_schema_version_matches(self, frozen: dict[str, Any]) -> None:
        assert frozen["schema_version"] == SCHEMA_VERSION

    def test_tsv_is_the_json_projection(self, frozen: dict[str, Any]) -> None:
        assert TSV_FIXTURE.read_text(encoding="utf-8") == compute_tsv(frozen)

    def test_bits_round_trip(self) -> None:
        import math

        for value in (0.0, 3.25, -1.5, math.inf):
            assert f64_from_bits(f64_bits(value)) == value
        assert math.isnan(f64_from_bits(f64_bits(math.nan)))


class TestAntiVacuous:
    """The load-bearing hostile rows are genuinely in the frozen lattice."""

    def test_numeric_string_field_accepts(self, frozen: dict[str, Any]) -> None:
        row = _row(frozen, "valid_str_float_field")
        assert row["outcome"]["kind"] == "snapshot"
        assert row["outcome"]["effective_rps"] == {"bits": f64_bits(2.5)}

    def test_schema_float_three_accepts(self, frozen: dict[str, Any]) -> None:
        assert _row(frozen, "valid_schema_float_three")["outcome"]["kind"] == "snapshot"

    def test_nan_float_field_accepts(self, frozen: dict[str, Any]) -> None:
        row = _row(frozen, "valid_nan_float_field")
        assert row["outcome"]["kind"] == "snapshot"
        assert row["outcome"]["wall_time"] == {"bits": f64_bits(float("nan"))}

    def test_bool_root_rejects_as_non_mapping(self, frozen: dict[str, Any]) -> None:
        row = _row(frozen, "reject_root_bool")
        assert row["outcome"]["kind"] == "none"
        assert row["classified"]["root_mapping"] == 0

    def test_overflow_python_arm_rows_recorded_honestly(self, frozen: dict[str, Any]) -> None:
        for case_id in ("raises_huge_float_field", "raises_inf_in_int_field"):
            row = _row(frozen, case_id)
            assert row["outcome"] == {"kind": "raises", "type": "OverflowError"}
            assert row["classified"] is None  # the documented Python arm

    def test_bad_then_huge_stays_python_arm_but_serves_none(
        self, frozen: dict[str, Any]
    ) -> None:
        row = _row(frozen, "pythonarm_bad_then_huge")
        assert row["outcome"]["kind"] == "none"  # oracle short-circuits first
        assert row["classified"] is None  # classifier's probe sweep abstains

    def test_every_required_field_has_its_absent_row(self, frozen: dict[str, Any]) -> None:
        for key, _ in REQUIRED_FIELDS:
            row = _row(frozen, f"reject_absent_{key}")
            assert row["outcome"]["kind"] == "none"

    def test_pull_active_arm_rows_present(self, frozen: dict[str, Any]) -> None:
        assert _row(frozen, "valid_active_false")["outcome"]["pull_active"] is False
        assert _row(frozen, "valid_active_string")["outcome"]["pull_active"] is None
        assert _row(frozen, "valid_no_active_key")["outcome"]["pull_active"] is None
