"""Frozen auto-pull predicate goldens (`DEC-RUST-PULL-STATE-001`, Wave 3G).

The committed fixtures (``auto_pull_reload_verdicts.json`` + the serde-free
``auto_pull_reload_verdicts.tsv`` projection) were captured by
``generate_auto_pull_golden.py`` on the PRE-conversion tree — they are the
oracle the Rust kernel of ``brutex/dashboard/auto_pull.py``'s
``should_reload_instrument_master`` predicate must satisfy bit-for-bit
(Golden Rule #1).  This test recomputes the kernel-domain table from the
LIVE (now kernel-only) module via the shared ``_auto_pull_harness`` and
asserts structural equality, plus anti-vacuous pins on the load-bearing
rows.

Flip phase (DEC-RUST-PULL-STATE-001): the fixture stays BYTE-FROZEN, so its
``kernel_row=False`` out-of-i64 rows still document the DELETED pre-flip
silent Python arm; the live module now REFUSES those counts loudly
(``PullStateKernelError``, the facade's input-domain gate), pinned here in
place of a verdict comparison.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from brutex import pull_state_rust
from brutex.dashboard.auto_pull import should_reload_instrument_master

from tests.golden._auto_pull_harness import HUGE_COUNT, compute_payload, compute_tsv

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
    def test_kernel_rows_match_frozen(self, frozen: dict[str, Any], live: dict[str, Any]) -> None:
        """The live (kernel-only) recompute equals every frozen KERNEL row.

        The frozen ``kernel_row=False`` rows document the deleted pre-flip
        Python arm — the live module refuses their counts loudly instead
        (pinned by ``test_huge_counts_refuse_loudly``), so they are excluded
        from the row-for-row equality.
        """
        assert live["rows"] == [row for row in frozen["rows"] if row["kernel_row"]]

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

    def test_huge_counts_documented_in_the_frozen_fixture(self, frozen: dict[str, Any]) -> None:
        """The fixture (byte-frozen) still records the pre-flip Python arm."""
        row = _by_name(frozen)["huge_counts_l0e0g0"]
        assert row["kernel_row"] is False
        assert row["verdict"] is False

    def test_huge_counts_refuse_loudly(self) -> None:
        """Flip phase: an out-of-i64 count RAISES the typed input-domain
        refusal naming the domain — the pre-flip silent Python arm the
        frozen row above documents was deleted with the oracle
        (DEC-RUST-PULL-STATE-001)."""
        with pytest.raises(
            pull_state_rust.PullStateKernelError, match="outside the kernel i64 domain"
        ):
            should_reload_instrument_master(index_count=HUGE_COUNT, stock_count=HUGE_COUNT)
        with pytest.raises(
            pull_state_rust.PullStateKernelError, match="outside the kernel i64 domain"
        ):
            should_reload_instrument_master(index_count=0, stock_count=-HUGE_COUNT)
