"""Sampling-law pins: seed determinism, rotation, engine force-include, ledger.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from brutex.acceptance.sampling import (
    ENGINE_COORDS,
    VIX_COORD,
    coverage_ledger_path,
    coverage_ledger_summary,
    dataset_key,
    enumerate_fno_contracts,
    enumerate_spot_datasets,
    load_coverage_ledger,
    sample_datasets,
    update_coverage_ledger,
)

POP = [
    ("NSE", "INDEX", "NIFTY"),
    ("NSE", "INDEX", "BANKNIFTY"),
    ("BSE", "INDEX", "SENSEX"),
    ("NSE", "INDEX", "INDIAVIX"),
    *[("NSE", "CASH", f"SYM{i:03d}") for i in range(30)],
]


class TestSampler:
    def test_engine_force_included(self) -> None:
        plan = sample_datasets(POP, seed="2026-07-10", sample_n=3, full=False)
        assert set(ENGINE_COORDS) <= set(plan.selected)
        assert VIX_COORD in plan.selected
        assert plan.mode == "sample"
        assert len(plan.sampled) == 3

    def test_seed_determinism(self) -> None:
        a = sample_datasets(POP, seed="2026-07-10", sample_n=5, full=False)
        b = sample_datasets(POP, seed="2026-07-10", sample_n=5, full=False)
        assert a.selected == b.selected

    def test_rotation_moves_with_seed(self) -> None:
        a = sample_datasets(POP, seed="2026-07-10", sample_n=5, full=False)
        b = sample_datasets(POP, seed="2026-07-11", sample_n=5, full=False)
        assert a.sampled != b.sampled  # daily rotation actually rotates

    def test_full_flag_lifts_sampling(self) -> None:
        plan = sample_datasets(POP, seed="s", sample_n=2, full=True)
        assert plan.mode == "full"
        assert set(plan.selected) == set(POP)

    def test_n_covering_population_reads_full(self) -> None:
        plan = sample_datasets(POP, seed="s", sample_n=999, full=False)
        assert plan.mode == "full"

    def test_selection_keys(self) -> None:
        plan = sample_datasets(POP[:4], seed="s", sample_n=0, full=False)
        assert dataset_key(("NSE", "INDEX", "NIFTY")) in plan.selection_keys

    def test_engine_missing_from_population_not_invented(self) -> None:
        plan = sample_datasets(POP[4:], seed="s", sample_n=2, full=False)
        assert plan.engine == ()


class TestEnumeration:
    def test_enumerate_spot_skips_fno(self, tmp_path: Path) -> None:
        for seg, sym in (("INDEX", "NIFTY"), ("CASH", "TCS"), ("FNO", "NSE-NIFTY-30Sep25-FUT")):
            (tmp_path / "bars" / "NSE" / seg / sym).mkdir(parents=True)
        assert enumerate_spot_datasets(tmp_path) == [
            ("NSE", "CASH", "TCS"),
            ("NSE", "INDEX", "NIFTY"),
        ]

    def test_enumerate_spot_missing_tree(self, tmp_path: Path) -> None:
        assert enumerate_spot_datasets(tmp_path) == []

    def test_enumerate_fno(self, tmp_path: Path) -> None:
        (tmp_path / "bars" / "NSE" / "FNO" / "NSE-NIFTY-30Sep25-FUT").mkdir(parents=True)
        assert enumerate_fno_contracts(tmp_path) == ["NSE-NIFTY-30Sep25-FUT"]

    def test_enumerate_fno_missing(self, tmp_path: Path) -> None:
        assert enumerate_fno_contracts(tmp_path) == []


class TestCoverageLedger:
    def test_path(self, tmp_path: Path) -> None:
        assert coverage_ledger_path(tmp_path) == tmp_path / "acceptance" / "coverage_ledger.json"

    def test_roundtrip_and_summary(self, tmp_path: Path) -> None:
        path = coverage_ledger_path(tmp_path)
        update_coverage_ledger(
            path,
            check_id="run",
            dataset_keys=["NSE/INDEX/NIFTY", "NSE/CASH/TCS"],
            run_date=date(2026, 7, 10),
        )
        ledger = load_coverage_ledger(path)
        assert ledger["run"]["NSE/INDEX/NIFTY"] == "2026-07-10"
        summary = coverage_ledger_summary(
            ledger,
            population_keys=["NSE/INDEX/NIFTY", "NSE/CASH/TCS", "NSE/CASH/NEVER"],
            run_date=date(2026, 7, 12),
        )
        assert summary["datasets_sampled_window_pct"] == 66.7
        assert summary["oldest_unsampled"] == "NSE/CASH/NEVER"

    def test_summary_outside_window_not_counted(self) -> None:
        ledger = {"run": {"a": "2026-01-01"}}
        summary = coverage_ledger_summary(
            ledger, population_keys=["a"], run_date=date(2026, 7, 10)
        )
        assert summary["datasets_sampled_window_pct"] == 0.0
        assert summary["oldest_unsampled"] is None  # sampled EVER, just not recently

    def test_summary_empty_population(self) -> None:
        summary = coverage_ledger_summary({}, population_keys=[], run_date=date(2026, 7, 10))
        assert summary == {"datasets_sampled_window_pct": None, "oldest_unsampled": None}

    def test_load_corrupt_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_coverage_ledger(path) == {}

    def test_load_non_dict_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        path.write_text(json.dumps([1, 2]), encoding="utf-8")
        assert load_coverage_ledger(path) == {}

    def test_load_non_dict_cells_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        path.write_text(json.dumps({"run": {"a": "2026-07-10"}, "junk": [1]}), encoding="utf-8")
        assert load_coverage_ledger(path) == {"run": {"a": "2026-07-10"}}

    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        assert load_coverage_ledger(tmp_path / "absent.json") == {}

    def test_update_write_failure_warns_never_raises(self, tmp_path: Path, monkeypatch) -> None:
        import brutex.acceptance.sampling as sampling_mod

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(sampling_mod, "atomic_write_simple", _boom)
        update_coverage_ledger(
            tmp_path / "ledger.json",
            check_id="run",
            dataset_keys=["a"],
            run_date=date(2026, 7, 10),
        )  # must not raise — the ledger is a visibility aid only
