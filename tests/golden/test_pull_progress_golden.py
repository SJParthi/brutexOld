"""Frozen pull-progress derived-metrics goldens (`DEC-RUST-PULL-STATE-001`, Wave 2F).

The committed fixtures (``pull_progress_derived.json`` + the serde-free
``pull_progress_derived.tsv`` projection) were captured by
``generate_pull_progress_golden.py`` on the PRE-conversion tree — they are
the oracle the Rust port of ``brutex/dashboard/pull_progress.py``'s
derived-metrics plane must satisfy bit-for-bit (Golden Rule #1). This test
recomputes the SAME table from the LIVE module via the shared
``_pull_progress_harness`` and asserts structural equality, plus pins:

* the TSV is EXACTLY the projection of the JSON (one truth, two encodings —
  the cargo fixture test consumes the TSV, so the two must never drift);
* the bit-pattern encodings round-trip (``f64_bits`` ∘ ``f64_from_bits``);
* the load-bearing tie rows are genuinely IN the fixture (anti-vacuous:
  half-even DOWN 6.25 -> 6.2, half-even UP 18.75 -> 18.8, the
  99.96 -> 100.0 round-up-to-clamp, and the completed>total 100.0 pun);
* a NaN OUTPUT row exists (the NaN-to-NaN comparison contract has teeth).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from tests.golden._pull_progress_harness import (
    COUNT_DOMAIN_MAX,
    compute_payload,
    compute_tsv,
    f64_bits,
    f64_from_bits,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JSON_FIXTURE = FIXTURES / "pull_progress_derived.json"
TSV_FIXTURE = FIXTURES / "pull_progress_derived.tsv"


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


def _percent_by_counts(payload: dict[str, Any], total: int, completed: int) -> set[str]:
    return {
        row["out"]["percent"]["bits"]
        for row in payload["rows"]
        if row["case"]["total"] == total and row["case"]["completed"] == completed
    }


class TestFrozenGolden:
    def test_rows_match_frozen(self, frozen: dict[str, Any], live: dict[str, Any]) -> None:
        assert live["rows"] == frozen["rows"]

    def test_domain_ceiling_matches(self, frozen: dict[str, Any]) -> None:
        assert frozen["count_domain_max"] == COUNT_DOMAIN_MAX

    def test_tsv_is_the_json_projection(self, frozen: dict[str, Any]) -> None:
        assert TSV_FIXTURE.read_text(encoding="utf-8") == compute_tsv(frozen)

    def test_bits_round_trip(self) -> None:
        for value in (0.0, 6.2, 18.8, 100.0, math.inf, 5e-324):
            assert f64_from_bits(f64_bits(value)) == value
        assert math.isnan(f64_from_bits(f64_bits(math.nan)))


class TestAntiVacuous:
    """The load-bearing hostile rows are genuinely in the frozen lattice."""

    def test_half_even_down_tie(self, frozen: dict[str, Any]) -> None:
        # 1/16 = 6.25% — exact tie, banker's rounds DOWN to 6.2.
        assert f64_bits(6.2) in _percent_by_counts(frozen, 16, 1)

    def test_half_even_up_tie(self, frozen: dict[str, Any]) -> None:
        # 3/16 = 18.75% — exact tie, banker's rounds UP to 18.8.
        assert f64_bits(18.8) in _percent_by_counts(frozen, 16, 3)

    def test_round_up_to_clamp_boundary(self, frozen: dict[str, Any]) -> None:
        # 2499/2500 = 99.96% — rounds to 100.0 (the min-clamp boundary).
        assert _percent_by_counts(frozen, 2500, 2499) == {f64_bits(100.0)}

    def test_completed_over_total_clamps(self, frozen: dict[str, Any]) -> None:
        assert _percent_by_counts(frozen, 4, 9) == {f64_bits(100.0)}

    def test_zero_total_percent_is_zero(self, frozen: dict[str, Any]) -> None:
        assert _percent_by_counts(frozen, 0, 5) == {f64_bits(0.0)}

    def test_a_nan_output_row_exists(self, frozen: dict[str, Any]) -> None:
        assert any(
            math.isnan(f64_from_bits(row["out"]["units_per_sec"]["bits"])) for row in frozen["rows"]
        )

    def test_huge_count_rows_exist(self, frozen: dict[str, Any]) -> None:
        assert any(row["case"]["total"] == COUNT_DOMAIN_MAX for row in frozen["rows"])
