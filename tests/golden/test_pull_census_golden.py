"""Frozen pull-census goldens (`DEC-RUST-PULL-STATE-001`, Wave 3G).

The committed fixtures (``pull_census_verdicts.json`` + the serde-free
``pull_census_verdicts.tsv`` projection) were captured by
``generate_pull_census_golden.py`` on the PRE-conversion tree — they are the
oracle the Rust port of ``brutex/dashboard/pull_census.py``'s fold must
satisfy bit-for-bit (Golden Rule #1).  This test recomputes the SAME table
from the LIVE module via the shared ``_pull_census_harness`` and asserts
structural equality, plus anti-vacuous pins on the load-bearing cases (the
sample cap crossing both classes, the astral/unicode sort-order law, the
zero-bar span, the scope filters).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.golden._pull_census_harness import compute_payload, compute_tsv

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JSON_FIXTURE = FIXTURES / "pull_census_verdicts.json"
TSV_FIXTURE = FIXTURES / "pull_census_verdicts.tsv"


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


def _case(payload: dict[str, Any], name: str) -> dict[str, Any]:
    (case,) = [case for case in payload["cases"] if case["name"] == name]
    assert isinstance(case, dict)
    return case


class TestFrozenGolden:
    def test_cases_match_frozen(self, frozen: dict[str, Any], live: dict[str, Any]) -> None:
        assert live["cases"] == frozen["cases"]

    def test_sample_cap_matches(self, frozen: dict[str, Any], live: dict[str, Any]) -> None:
        assert live["sample_cap"] == frozen["sample_cap"] == 20

    def test_tsv_is_the_json_projection(self, frozen: dict[str, Any]) -> None:
        assert TSV_FIXTURE.read_text(encoding="utf-8") == compute_tsv(frozen)


class TestAntiVacuous:
    def test_cap_case_spans_both_classes(self, frozen: dict[str, Any]) -> None:
        case = _case(frozen, "sample_cap_spans_both_classes")
        out = case["out"]
        assert (out["settled_unwritten"], out["written_unsettled"]) == (15, 10)
        assert len(out["samples"]) == 20
        kinds = [row[0] for row in case["kernel_samples"]]
        assert kinds == [0] * 15 + [1] * 5

    def test_cap_case_all_one_class(self, frozen: dict[str, Any]) -> None:
        case = _case(frozen, "sample_cap_all_settled_unwritten")
        assert case["out"]["settled_unwritten"] == 25
        assert len(case["out"]["samples"]) == 20

    def test_astral_sort_case_present(self, frozen: dict[str, Any]) -> None:
        case = _case(frozen, "hyphen_ampersand_and_astral_sort")
        symbols = [row[1] for row in case["kernel_samples"]]
        # Python code-point sort == UTF-8 byte sort (the kernel's BTreeSet).
        assert symbols == sorted(symbols)
        assert "NSE-\U0001f40d" in symbols

    def test_zero_bar_span_is_unwritten(self, frozen: dict[str, Any]) -> None:
        case = _case(frozen, "zero_bar_span_is_unwritten")
        assert case["kernel_written"] == []
        assert case["out"]["settled_unwritten"] == 1

    def test_scope_filters_bound_scanned(self, frozen: dict[str, Any]) -> None:
        case = _case(frozen, "symbol_scope_ignores_foreign")
        assert case["out"]["scanned"] == 1
        assert case["kernel_rows"] == [["NSE-NIFTY", "1minute", True]]
