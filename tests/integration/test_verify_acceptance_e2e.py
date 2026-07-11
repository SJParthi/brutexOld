"""Full-stack e2e: real-writer lake → run_acceptance → schema/audit/exit/deltas.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). Drives the WHOLE
pipeline (real teeth smoke + all 7 checks + the real dashboard client) on a
real-writer twin lake, twice — the second run proves the report chain
(deltas + streaks) end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the movers/latency legs drive the real app")

from brutex.acceptance.report import audit_path, render_human, validate_report_dict
from brutex.acceptance.run import RunConfig, run_acceptance
from tests.fakes.acceptance_twin import NOW, make_twin


@pytest.fixture(scope="module")
def e2e_lake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    lake = make_twin(tmp_path_factory.mktemp("acceptance-e2e"))
    from brutex.data.instrument_store import InstrumentStore

    InstrumentStore.open(lake / "instruments.duckdb").close()
    return lake


@pytest.fixture(scope="module")
def two_runs(e2e_lake: Path) -> tuple[tuple[int, dict], tuple[int, dict]]:
    cfg = RunConfig(
        lake_root=e2e_lake,
        seed="2026-07-10",
        sample_n=50,
        abstain_budget=10,  # a bare twin has honest no-ledger/no-bridge abstains
        argv=("verify-acceptance", "--seed", "2026-07-10"),
    )
    first = run_acceptance(cfg, now_utc=lambda: NOW)
    second = run_acceptance(cfg, now_utc=lambda: NOW)
    return first, second


class TestEndToEnd:
    def test_first_run_clean_exit_and_schema(self, two_runs, e2e_lake: Path) -> None:
        (code, report), _ = two_runs
        assert code == 0, report["overall"]
        assert validate_report_dict(report) == []
        assert report["self_test"]["verdict"] == "TEETH_PROVEN"
        assert {c["check_id"] for c in report["checks"]} == {
            "unique",
            "tf",
            "pull",
            "rate",
            "movers",
            "sanity",
            "latency",
        }
        assert report["overall"]["fail_checks"] == []
        assert report["overall"]["error_checks"] == []

    def test_report_txt_names_every_check(self, two_runs, e2e_lake: Path) -> None:
        (_, report), _ = two_runs
        text = (
            e2e_lake / "acceptance" / report["run_id"] / "report.txt"
        ).read_text(encoding="utf-8")
        for check_id in ("unique", "tf", "pull", "rate", "movers", "sanity", "latency"):
            assert check_id in text
        assert text == render_human(report)
        # ABSTAINs render first — the standing structural ones are visible.
        assert "no_per_call_persistence" in text
        assert "no_oi_compare_seam" in text

    def test_audit_chain_green_with_existing_verifier(self, two_runs, e2e_lake: Path) -> None:
        from brutex.audit_verify import verify_audit_file

        result = verify_audit_file(audit_path(e2e_lake))
        # 2 runs × (RUN_START + 7 CHECK + RUN_COMPLETE)
        assert result.entry_count == 18

    def test_report_sha_recorded_in_chain(self, two_runs, e2e_lake: Path) -> None:
        import hashlib

        (_, report), _ = two_runs
        lines = [
            json.loads(line)
            for line in audit_path(e2e_lake).read_text(encoding="utf-8").splitlines()
        ]
        complete = next(
            e for e in lines if e["action"] == "ACCEPTANCE_RUN_COMPLETE"
            and e["subject"] == report["run_id"]
        )
        report_path = Path(complete["payload"]["report_path"])
        assert (
            hashlib.sha256(report_path.read_bytes()).hexdigest()
            == complete["payload"]["report_sha256"]
        )

    def test_second_run_chains_deltas_and_streaks(self, two_runs) -> None:
        (_, first), (code, second) = two_runs
        assert code == 0
        assert second["previous"]["run_id"] == first["run_id"]
        assert second["deltas"]["verdict_transitions"] == []
        streaks = {
            sub["id"]: sub["abstain_streak"]
            for check in second["checks"]
            for sub in check["sub_checks"]
            if sub["verdict"] == "ABSTAIN"
        }
        assert streaks["rate.per_call_history"] == 2
        assert streaks["tf.oi"] == 2

    def test_movers_leg_ran_against_the_real_wire(self, two_runs) -> None:
        (_, report), _ = two_runs
        movers = next(c for c in report["checks"] if c["check_id"] == "movers")
        spot = next(s for s in movers["sub_checks"] if s["id"] == "movers.spot")
        assert spot["verdict"] == "PASS"
        assert spot["evidence"]["cells_compared"] > 0

    def test_coverage_blocks_required_everywhere(self, two_runs) -> None:
        (_, report), _ = two_runs
        for check in report["checks"]:
            assert check["coverage"]["mode"] in {"full", "sample", "bounded", "none"}
            for sub in check["sub_checks"]:
                assert sub["evidence"], f"{sub['id']} shipped an empty evidence block"
                assert "units_checked" in sub["evidence"]
