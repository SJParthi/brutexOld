"""Sampling-law pins: seed determinism, rotation, engine force-include, ledger.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1958).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from brutex.acceptance import sampling
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
        summary = coverage_ledger_summary(ledger, population_keys=["a"], run_date=date(2026, 7, 10))
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


class TestCoverageLedgerRound2:
    def test_oversized_ledger_starts_fresh_with_warn(self, tmp_path: Path) -> None:
        # Round-2 fix 7: the read is BOUNDED — an oversized file is a fresh
        # start (heuristic aid, never evidence), not an unbounded read.
        path = tmp_path / "coverage_ledger.json"
        path.write_text("x" * (sampling.COVERAGE_LEDGER_MAX_BYTES + 1), encoding="utf-8")
        assert sampling.load_coverage_ledger(path) == {}

    def test_prune_on_save_drops_out_of_population_keys(self, tmp_path: Path) -> None:
        # Round-2 fix 7: expired contracts / deleted datasets no longer
        # accumulate forever — keys absent from the CURRENT population are
        # pruned on save; this run's own examined keys always survive.
        path = tmp_path / "coverage_ledger.json"
        update_coverage_ledger(
            path,
            check_id="run",
            dataset_keys=["NSE/FNO/EXPIRED-OLD", "NSE/INDEX/NIFTY"],
            run_date=date(2026, 7, 1),
        )
        update_coverage_ledger(
            path,
            check_id="run",
            dataset_keys=["NSE/CASH/NEWLY-EXAMINED"],
            run_date=date(2026, 7, 10),
            population_keys=["NSE/INDEX/NIFTY"],
        )
        ledger = load_coverage_ledger(path)
        assert "NSE/FNO/EXPIRED-OLD" not in ledger["run"]
        assert ledger["run"]["NSE/INDEX/NIFTY"] == "2026-07-01"
        assert ledger["run"]["NSE/CASH/NEWLY-EXAMINED"] == "2026-07-10"

    def test_symlinked_acceptance_dir_refused(self, tmp_path: Path) -> None:
        # House .quarantine precedent (round-2 NIT): never write the ledger
        # through an operator-plantable link.
        target = tmp_path / "elsewhere"
        target.mkdir()
        link = tmp_path / "acceptance"
        link.symlink_to(target)
        update_coverage_ledger(
            link / "coverage_ledger.json",
            check_id="run",
            dataset_keys=["a"],
            run_date=date(2026, 7, 10),
        )
        assert not (target / "coverage_ledger.json").exists()


class TestFnoContractRotation:
    def test_sample_rotates_with_seed(self) -> None:
        # Round-2 fix 8 (refuter 2C MINOR): the lexicographic head-6 was
        # seed-INVARIANT — the fill must rotate like the spot sampler.
        contracts = [f"NSE-NIFTY-30Jul26-{k}-CE" for k in range(23000, 23400, 10)]
        a = sampling.sample_fno_contracts(contracts, seed="2026-07-11", sample_n=6)
        b = sampling.sample_fno_contracts(contracts, seed="2026-07-12", sample_n=6)
        assert len(a) == len(b) == 6
        assert a != b  # rotation — different nights sample different contracts
        # Deterministic per seed (any night reproducible via --seed).
        assert a == sampling.sample_fno_contracts(contracts, seed="2026-07-11", sample_n=6)
        assert a != contracts[:6]  # never the fixed lexicographic head

    def test_small_population_returns_all_sorted(self) -> None:
        contracts = ["B", "A"]
        assert sampling.sample_fno_contracts(contracts, seed="s", sample_n=6) == ["A", "B"]


class TestEngineFuturesContracts:
    """Round-3 fix (C-F1): the ADR-0012 engine surface is code-selectable."""

    def test_filters_nse_nifty_fut_only(self) -> None:
        contracts = [
            "NSE-NIFTY-31Jul26-FUT",
            "NSE-NIFTY-27Aug26-FUT",
            "NSE-BANKNIFTY-31Jul26-FUT",  # not the engine futures track
            "NSE-NIFTY-31Jul26-24000-CE",  # option leg, never engine
            "NSE-RELIANCE-31Jul26-FUT",  # stock future, REFERENCE_ONLY
            "BSE-NIFTY-31Jul26-FUT",  # wrong exchange (no such vendor symbol)
            "junk-dir-name",  # unparseable → skipped
        ]
        assert sampling.engine_futures_contracts(contracts) == [
            "NSE-NIFTY-27Aug26-FUT",
            "NSE-NIFTY-31Jul26-FUT",
        ]

    def test_empty_population(self) -> None:
        assert sampling.engine_futures_contracts([]) == []


class TestRecentMonths:
    def test_year_boundary(self) -> None:
        from datetime import date

        got = sampling.recent_months(date(2026, 2, 15), 4)
        assert got == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]


class TestCoverageLedgerBomb:
    def test_deep_nested_json_bomb_returns_empty(self, tmp_path: Path) -> None:
        # Round 1 MN5 regression: RecursionError on a nested-JSON bomb must
        # fold to the empty ledger, never crash the run.
        path = tmp_path / "coverage_ledger.json"
        path.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
        assert sampling.load_coverage_ledger(path) == {}


class TestMonthBoundedRead:
    def _seed(self, tmp_path: Path) -> Path:
        from datetime import date

        from brutex.acceptance.selftest import _twin_bars
        from brutex.data.parquet_writer import ParquetWriter

        lake = tmp_path / "lake"
        writer = ParquetWriter(lake_root=lake)
        for day in (date(2026, 2, 4), date(2026, 7, 9)):
            writer.write(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                bars=_twin_bars(day, base=100.0),
            )
        return lake / "bars" / "NSE" / "INDEX" / "NIFTY"

    def test_month_bounded_files_filters_by_filename(self, tmp_path: Path) -> None:
        symbol_dir = self._seed(tmp_path)
        root = symbol_dir / "1minute"
        all_files = sampling.month_bounded_files(root, None)
        assert [f.name for f in all_files] == ["02.parquet", "07.parquet"]
        only_july = sampling.month_bounded_files(root, {(2026, 7)})
        assert [f.name for f in only_july] == ["07.parquet"]
        # a non-grammar entry is skipped under a bound
        (root / "2026" / "junk.parquet").write_bytes(b"x")
        assert sampling.month_bounded_files(root, {(2026, 7)}) == only_july
        assert sampling.month_bounded_files(tmp_path / "nowhere", None) == []

    def test_read_bounded_matches_seam_when_unbounded(self, tmp_path: Path) -> None:
        from brutex.data.tf_autoderive import read_lake_bars_frame

        symbol_dir = self._seed(tmp_path)
        unbounded = sampling.read_bars_frame_bounded(symbol_dir, "1minute", None)
        assert unbounded is not None
        assert unbounded.equals(read_lake_bars_frame(symbol_dir, "1minute"))

    def test_read_bounded_reads_only_in_scope_months(self, tmp_path: Path) -> None:
        symbol_dir = self._seed(tmp_path)
        july = sampling.read_bars_frame_bounded(symbol_dir, "1minute", {(2026, 7)})
        assert july is not None
        assert july.height == 31  # one twin session only
        assert sampling.read_bars_frame_bounded(symbol_dir, "1minute", {(2025, 1)}) is None
        assert sampling.read_bars_frame_bounded(symbol_dir, "5minute", None) is None
