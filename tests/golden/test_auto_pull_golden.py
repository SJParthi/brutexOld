"""Frozen auto-pull predicate goldens (`DEC-RUST-PULL-STATE-001`, Wave 3G).

The committed fixtures (``auto_pull_reload_verdicts.json`` + the serde-free
``auto_pull_reload_verdicts.tsv`` projection) were captured by
``generate_auto_pull_golden.py`` on the PRE-conversion tree — they are the
oracle the Rust port of ``brutex/dashboard/auto_pull.py``'s
``should_reload_instrument_master`` predicate must satisfy bit-for-bit
(Golden Rule #1).  This test recomputes the SAME table from the LIVE module
via the shared ``_auto_pull_harness`` and asserts structural equality, plus
anti-vacuous pins on the load-bearing rows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.golden._auto_pull_harness import compute_payload, compute_tsv

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JSON_FIXTURE = FIXTURES / "auto_pull_reload_verdicts.json"
TSV_FIXTURE = FIXTURES / "auto_pull_reload_verdicts.tsv"


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


def _by_name(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in payload["rows"]}


class TestFrozenGolden:
    def test_rows_match_frozen(self, frozen: dict[str, Any], live: dict[str, Any]) -> None:
        assert live["rows"] == frozen["rows"]

    def test_tsv_is_the_json_projection(self, frozen: dict[str, Any]) -> None:
        assert TSV_FIXTURE.read_text(encoding="utf-8") == compute_tsv(frozen)


class TestAntiVacuous:
    def test_lattice_is_exhaustive(self, frozen: dict[str, Any]) -> None:
        assert len(frozen["rows"]) == 48
        assert sum(1 for row in frozen["rows"] if not row["kernel_row"]) == 8

    def test_populated_clean_lake_never_reloads(self, frozen: dict[str, Any]) -> None:
        assert _by_name(frozen)["populated_l0e0g0"]["verdict"] is False

    def test_empty_and_indices_missing_always_reload(self, frozen: dict[str, Any]) -> None:
        rows = _by_name(frozen)
        assert rows["empty_l0e0g0"]["verdict"] is True
        assert rows["indices_missing_l0e0g0"]["verdict"] is True

    def test_stocks_missing_alone_does_not_reload(self, frozen: dict[str, Any]) -> None:
        # Zero STOCKS with indices present is NOT a reload trigger (the
        # oracle's indices_missing law keys on index_count only).
        assert _by_name(frozen)["stocks_missing_l0e0g0"]["verdict"] is False

    def test_each_staleness_flag_alone_triggers(self, frozen: dict[str, Any]) -> None:
        rows = _by_name(frozen)
        assert rows["populated_l1e0g0"]["verdict"] is True
        assert rows["populated_l0e1g0"]["verdict"] is True
        assert rows["populated_l0e0g1"]["verdict"] is True

    def test_huge_counts_stay_on_the_python_arm(self, frozen: dict[str, Any]) -> None:
        row = _by_name(frozen)["huge_counts_l0e0g0"]
        assert row["kernel_row"] is False
        assert row["verdict"] is False
