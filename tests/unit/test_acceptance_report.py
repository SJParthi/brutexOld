"""Report-layer pins: schema freeze, tamper tooth, audit chain, deltas, streaks.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from brutex.acceptance.model import (
    SCHEMA_VERSION,
    AbstainReason,
    Verdict,
    make_check,
    make_coverage,
    make_subcheck,
    rollup_overall,
    totals_for,
)
from brutex.acceptance.model import TeethVerdict as TeethV
from brutex.acceptance.report import (
    AcceptanceAudit,
    ReportWriteError,
    acceptance_dir,
    audit_path,
    compute_deltas,
    load_previous,
    mint_run_id,
    previous_subcheck_index,
    render_human,
    streak_for,
    validate_report_dict,
    write_report_files,
)

NOW = datetime(2026, 7, 10, 6, 0, tzinfo=UTC)


def _check(check_id: str = "unique", verdict: Verdict = Verdict.PASS, **evidence: object):
    sub = make_subcheck(
        f"{check_id}.census",
        verdict,
        units_checked=0 if verdict in (Verdict.ABSTAIN, Verdict.ERROR) else 5,
        evidence={"findings_total": 0, **evidence},
        abstain_reason=AbstainReason.NO_LAKE if verdict is Verdict.ABSTAIN else None,
    )
    return make_check(
        check_id,
        [sub],
        coverage=make_coverage(mode="full", population=5, examined=5, seed="2026-07-10"),
    )


def _report_dict(checks=None, *, run_id: str = "acceptance-20260710T060000Z-abc123") -> dict:
    checks = checks if checks is not None else [_check()]
    overall = rollup_overall(checks, teeth=TeethV.TEETH_PROVEN, abstain_budget=3)
    return {
        "schema": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_utc": NOW.isoformat(),
        "duration_s": 1.0,
        "box": {"hostname": "test", "pull_active": False},
        "inputs": {
            "lake_root": "/lake",
            "checks_requested": ["unique"],
            "seed": "2026-07-10",
            "seed_mode": "fixed",
            "full": False,
            "months_bound": 6,
            "sample_n": 12,
            "latency_budget_overrides": {},
            "argv": [],
        },
        "self_test": {
            "mode": "smoke",
            "poisons_planted": 3,
            "poisons_detected": 3,
            "false_fails_on_clean": 0,
            "verdict": "TEETH_PROVEN",
            "duration_s": 1.0,
        },
        "overall": overall.to_dict(),
        "checks": [c.to_dict() for c in checks],
        "totals": totals_for(checks),
        "coverage_ledger": {"datasets_sampled_window_pct": 10.0, "oldest_unsampled": None},
        "previous": None,
        "deltas": None,
    }


class TestRunId:
    def test_shape(self) -> None:
        run_id = mint_run_id(NOW)
        assert run_id.startswith("acceptance-20260710T060000Z-")
        assert len(run_id.split("-")[-1]) == 6

    def test_paths(self, tmp_path: Path) -> None:
        assert acceptance_dir(tmp_path) == tmp_path / "acceptance"
        assert audit_path(tmp_path) == tmp_path / "acceptance" / "audit" / "acceptance.jsonl"


class TestValidate:
    def test_valid_report_clean(self) -> None:
        assert validate_report_dict(_report_dict()) == []

    def test_missing_top_key(self) -> None:
        report = _report_dict()
        del report["totals"]
        assert any("totals" in e for e in validate_report_dict(report))

    def test_foreign_schema(self) -> None:
        report = _report_dict()
        report["schema"] = "v99"
        assert any("schema token" in e for e in validate_report_dict(report))

    def test_checks_not_list(self) -> None:
        report = _report_dict()
        report["checks"] = "nope"
        assert validate_report_dict(report) == [*validate_report_dict(report)]
        assert any("not a list" in e for e in validate_report_dict(report))

    def test_bad_check_verdict_and_coverage(self) -> None:
        report = _report_dict()
        report["checks"][0]["verdict"] = "MAYBE"
        report["checks"][0]["coverage"] = {"mode": "everything"}
        errors = validate_report_dict(report)
        assert any("bad verdict" in e for e in errors)
        assert any("bad coverage" in e for e in errors)

    def test_empty_subchecks(self) -> None:
        report = _report_dict()
        report["checks"][0]["sub_checks"] = []
        assert any("empty sub_checks" in e for e in validate_report_dict(report))

    def test_subcheck_violations(self) -> None:
        report = _report_dict()
        report["checks"][0]["sub_checks"][0]["verdict"] = "ABSTAIN"
        report["checks"][0]["sub_checks"][0]["abstain_reason"] = None
        report["checks"][0]["sub_checks"].append({"id": "x", "verdict": "NOPE", "evidence": {}})
        errors = validate_report_dict(report)
        assert any("ABSTAIN without reason" in e for e in errors)
        assert any("bad verdict" in e for e in errors)
        assert any("empty evidence" in e for e in errors)


class TestWriteFiles:
    def test_writes_all_artifacts_and_latest(self, tmp_path: Path) -> None:
        report = _report_dict()
        path, sha = write_report_files(report, lake_root=tmp_path)
        assert path.is_file()
        assert (path.parent / "report.txt").is_file()
        assert (path.parent / "evidence" / "unique.json").is_file()
        latest = (tmp_path / "acceptance" / "LATEST").read_text(encoding="utf-8").strip()
        assert latest == report["run_id"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha

    def test_tamper_tooth(self, tmp_path: Path) -> None:
        report = _report_dict()
        path, sha = write_report_files(report, lake_root=tmp_path)
        raw = bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 0x01  # one flipped byte
        path.write_bytes(bytes(raw))
        assert hashlib.sha256(path.read_bytes()).hexdigest() != sha

    def test_malformed_report_refused(self, tmp_path: Path) -> None:
        report = _report_dict()
        del report["overall"]
        with pytest.raises(ReportWriteError, match="schema self-validation"):
            write_report_files(report, lake_root=tmp_path)
        assert not (tmp_path / "acceptance" / "LATEST").exists()

    def test_symlinked_acceptance_dir_refused(self, tmp_path: Path) -> None:
        # Round-2 NIT (house .quarantine precedent): the evidence tree is
        # never written through an operator-plantable link.
        target = tmp_path / "elsewhere"
        target.mkdir()
        (tmp_path / "acceptance").symlink_to(target)
        with pytest.raises(ReportWriteError, match="symlink"):
            write_report_files(_report_dict(), lake_root=tmp_path)
        assert list(target.iterdir()) == []

    def test_out_dir_override(self, tmp_path: Path) -> None:
        out = tmp_path / "elsewhere"
        path, _sha = write_report_files(_report_dict(), lake_root=tmp_path, out_dir=out)
        assert path.parent == out.resolve()
        # Round-3 fix (C-F3): an --out report is OFF-CHAIN — LATEST is never
        # stamped with a run id that has no report under <lake>/acceptance.
        assert not (tmp_path / "acceptance" / "LATEST").exists()

    def test_out_dir_override_leaves_existing_latest_untouched(self, tmp_path: Path) -> None:
        first = _report_dict()
        write_report_files(first, lake_root=tmp_path)
        before = (tmp_path / "acceptance" / "LATEST").read_text(encoding="utf-8")
        write_report_files(_report_dict(), lake_root=tmp_path, out_dir=tmp_path / "elsewhere")
        assert (tmp_path / "acceptance" / "LATEST").read_text(encoding="utf-8") == before


class TestPreviousChain:
    def test_load_previous_roundtrip(self, tmp_path: Path) -> None:
        report = _report_dict()
        write_report_files(report, lake_root=tmp_path)
        assert load_previous(tmp_path) is not None

    def test_load_previous_absent(self, tmp_path: Path) -> None:
        assert load_previous(tmp_path) is None

    def test_load_previous_corrupt(self, tmp_path: Path) -> None:
        base = tmp_path / "acceptance"
        base.mkdir(parents=True)
        (base / "LATEST").write_text("run-x\n", encoding="utf-8")
        (base / "run-x").mkdir()
        (base / "run-x" / "report.json").write_text("{broken", encoding="utf-8")
        assert load_previous(tmp_path) is None

    def test_load_previous_non_dict(self, tmp_path: Path) -> None:
        base = tmp_path / "acceptance"
        (base / "run-x").mkdir(parents=True)
        (base / "LATEST").write_text("run-x\n", encoding="utf-8")
        (base / "run-x" / "report.json").write_text("[1]", encoding="utf-8")
        assert load_previous(tmp_path) is None

    def test_previous_index_foreign_schema_empty(self) -> None:
        assert previous_subcheck_index({"schema": "other"}) == {}
        assert previous_subcheck_index(None) == {}

    def test_streak_carry_law(self) -> None:
        prev = {"unique.census": ("ABSTAIN", 2), "tf.oi": ("PASS", 0)}
        assert streak_for("unique.census", Verdict.ABSTAIN, prev) == 3
        assert streak_for("tf.oi", Verdict.ABSTAIN, prev) == 1  # prior non-abstain
        assert streak_for("new.sub", Verdict.ABSTAIN, prev) == 1
        assert streak_for("unique.census", Verdict.PASS, prev) == 0

    def test_previous_index_reads_streak_carry_block(self) -> None:
        # Round-4 fix (B4-4): sub-checks absent from the previous run (a
        # --checks subset / TEETH_FAILED report) index via its streak_carry
        # block; that run's OWN sub_checks overlay the carry.
        previous = _report_dict([_check("unique", Verdict.ABSTAIN)])
        previous["streak_carry"] = {
            "pull.replan_zero": {"verdict": "ABSTAIN", "abstain_streak": 3},
            "unique.census": {"verdict": "PASS", "abstain_streak": 0},  # overlaid
            "junk": "not-a-mapping",  # tolerated, skipped
        }
        index = previous_subcheck_index(previous)
        assert index["pull.replan_zero"] == ("ABSTAIN", 3)
        # The run's own sub_checks WIN over the carry cell.
        assert index["unique.census"][0] == "ABSTAIN"
        assert "junk" not in index

    def test_streak_carry_block_union_law(self) -> None:
        # Prior cells ride through unchanged; this run's sub-checks overwrite.
        from brutex.acceptance.report import streak_carry_block

        prev = {"pull.replan_zero": ("ABSTAIN", 3), "unique.census": ("PASS", 0)}
        block = streak_carry_block(prev, [_check("unique", Verdict.ABSTAIN)])
        assert block["pull.replan_zero"] == {"verdict": "ABSTAIN", "abstain_streak": 3}
        assert block["unique.census"]["verdict"] == "ABSTAIN"  # overwritten


class TestDeltas:
    def test_none_previous(self) -> None:
        assert compute_deltas(None, [_check()]) is None

    def test_foreign_schema_refused(self) -> None:
        deltas = compute_deltas({"schema": "other"}, [_check()])
        assert deltas is not None
        assert "refused" in deltas

    def test_transitions_and_metric_deltas(self) -> None:
        previous = _report_dict([_check("unique", Verdict.PASS, findings_total=2)])
        now_checks = [_check("unique", Verdict.FAIL, findings_total=5)]
        deltas = compute_deltas(previous, now_checks)
        assert deltas is not None
        assert deltas["verdict_transitions"] == [
            {"sub_check": "unique.census", "from": "PASS", "to": "FAIL"}
        ]
        cell = deltas["metric_deltas"]["unique.census.findings_total"]
        assert cell == {"prev": 2, "now": 5, "delta": 3}

    def test_no_transition_when_same(self) -> None:
        previous = _report_dict()
        deltas = compute_deltas(previous, [_check()])
        assert deltas is not None
        assert deltas["verdict_transitions"] == []


class TestHumanRender:
    def test_render_sections(self) -> None:
        checks = [
            _check("unique"),
            make_check(
                "rate",
                [
                    make_subcheck(
                        "rate.per_call_history",
                        Verdict.ABSTAIN,
                        units_checked=0,
                        evidence={"law": "x"},
                        abstain_reason=AbstainReason.NO_PER_CALL_PERSISTENCE,
                    ),
                    make_subcheck(
                        "rate.constants", Verdict.FAIL, units_checked=3, evidence={"k": 1}
                    ),
                ],
                coverage=make_coverage(mode="sample", population=9, examined=3, seed="s"),
            ),
        ]
        report = _report_dict(checks)
        text = render_human(report)
        # ABSTAINs render ABOVE the pass table; sample verdicts carry the
        # NOT-a-full-lake-claim disclaimer; FAIL sub-checks named.
        assert text.index("ABSTAINS (read these first)") < text.index("CHECK")
        assert "structural" in text
        assert "NOT a full-lake claim" in text
        assert "! rate.constants: FAIL" in text
        assert "TOTALS" in text

    def test_render_teeth_failed_headline(self) -> None:
        report = _report_dict()
        report["self_test"]["verdict"] = "TEETH_FAILED"
        assert "nothing below is trustworthy" in render_human(report)

    def test_render_teeth_skipped(self) -> None:
        report = _report_dict()
        report["self_test"]["verdict"] = "TEETH_SKIPPED"
        assert "teeth not demonstrated" in render_human(report)

    def test_render_no_ledger_pct(self) -> None:
        report = _report_dict()
        report["coverage_ledger"] = {"datasets_sampled_window_pct": None}
        assert "coverage ledger" not in render_human(report)

    def test_render_deltas_line(self) -> None:
        report = _report_dict()
        report["deltas"] = {
            "verdict_transitions": [{"sub_check": "a.b", "from": "PASS", "to": "FAIL"}]
        }
        assert "changed: a.b PASS→FAIL" in render_human(report)

    def test_render_limits_section(self) -> None:
        # Round-3 NIT (A-4): the honest envelope must be readable in
        # report.txt — check-level AND sub-check-level limits render.
        check = make_check(
            "tf",
            [
                make_subcheck(
                    "tf.daily",
                    Verdict.PASS,
                    units_checked=3,
                    evidence={"equal": 3},
                    limits=["forming sessions excluded from BOTH sides"],
                )
            ],
            coverage=make_coverage(mode="full", population=3, examined=3, seed="s"),
            limits=["a check-level envelope line"],
        )
        text = render_human(_report_dict([check]))
        assert "LIMITS (honest envelope)" in text
        assert "  tf: a check-level envelope line" in text
        assert "  tf.daily: forming sessions excluded from BOTH sides" in text

    def test_render_routed_series_count_visible(self) -> None:
        # Round-3 NIT (A-2): a PASS check with abstain-ROUTED series must
        # show the count on the table, not only in JSON evidence.
        check = _check("pull", non_default_shape_series=2)
        text = render_human(_report_dict([check]))
        assert "~ pull.census: 2 series routed non_default_window_shape" in text

    def test_render_muhurat_unverified_visible(self) -> None:
        check = _check(
            "tf",
            muhurat_days_unverified_total=2,
            muhurat_days_unverified=["2022-10-24", "2024-11-01"],
        )
        text = render_human(_report_dict([check]))
        assert "~ tf.census: 2 Muhurat date(s) NOT verified: 2022-10-24, 2024-11-01" in text

    def test_render_zero_counts_stay_silent(self) -> None:
        check = _check("pull", non_default_shape_series=0, muhurat_days_unverified_total=0)
        text = render_human(_report_dict([check]))
        assert "routed non_default_window_shape" not in text
        assert "Muhurat date(s) NOT verified" not in text


class TestAuditChain:
    def test_chain_written_resumed_and_verifiable(self, tmp_path: Path) -> None:
        audit = AcceptanceAudit(tmp_path)
        audit.run_start(run_id="r1", argv=["verify-acceptance"], seed="s")
        audit.check(run_id="r1", check=_check())
        audit.run_complete(run_id="r1", overall="PASS", report_sha256="00", report_path="/x")
        audit.close()
        # resume appends to the SAME chain
        audit2 = AcceptanceAudit(tmp_path)
        audit2.run_start(run_id="r2", argv=[], seed="s")
        audit2.close()
        from brutex.audit_verify import verify_audit_file

        result = verify_audit_file(audit_path(tmp_path))
        assert result.entry_count == 4
        lines = [
            json.loads(line)
            for line in audit_path(tmp_path).read_text(encoding="utf-8").splitlines()
        ]
        assert [e["action"] for e in lines] == [
            "ACCEPTANCE_RUN_START",
            "ACCEPTANCE_CHECK",
            "ACCEPTANCE_RUN_COMPLETE",
            "ACCEPTANCE_RUN_START",
        ]
        assert lines[1]["payload"]["check_id"] == "unique"

    def test_abstain_counts_in_check_line(self, tmp_path: Path) -> None:
        audit = AcceptanceAudit(tmp_path)
        audit.check(run_id="r", check=_check(verdict=Verdict.ABSTAIN))
        audit.check(run_id="r", check=_check("pull", Verdict.FAIL))
        audit.close()
        lines = [
            json.loads(line)
            for line in audit_path(tmp_path).read_text(encoding="utf-8").splitlines()
        ]
        assert lines[0]["payload"]["abstain_count"] == 1
        assert lines[1]["payload"]["fail_count"] == 1


class TestAuditCloseIdempotent:
    def test_double_close_is_safe(self, tmp_path: Path) -> None:
        audit = AcceptanceAudit(tmp_path)
        audit.close()
        audit.close()  # second close is a no-op (every exit path may close)


class TestRenderInjection:
    def test_newline_dataset_key_cannot_forge_a_totals_line(self) -> None:
        # Round 1 MN3 regression (refuter-confirmed forge): a newline-named
        # dataset key rendered raw emitted a standalone forged TOTALS line;
        # repr() keeps it one cell on one line.
        report = _report_dict()
        report["coverage_ledger"] = {
            "datasets_sampled_window_pct": 50.0,
            "oldest_unsampled": "NSE/CASH/FOO\nTOTALS pass=999 fail=0 EVERYTHING GREEN",
        }
        text = render_human(report)
        forged = [line for line in text.splitlines() if line.startswith("TOTALS pass=999")]
        assert forged == []
        assert "\\nTOTALS pass=999" in text  # repr()'d into the ledger line

    def test_none_oldest_unsampled_renders_bare(self) -> None:
        report = _report_dict()
        report["coverage_ledger"] = {
            "datasets_sampled_window_pct": 100.0,
            "oldest_unsampled": None,
        }
        assert "oldest unsampled: None" in render_human(report)


class TestLoadPreviousBombs:
    def test_deep_nested_json_bomb_returns_none(self, tmp_path: Path) -> None:
        # Round 1 MN5 regression: a deep-nested JSON bomb raised an UNCAUGHT
        # RecursionError and crashed the run; now an honest None.
        base = tmp_path / "acceptance"
        (base / "bomb").mkdir(parents=True)
        (base / "LATEST").write_text("bomb\n", encoding="utf-8")
        (base / "bomb" / "report.json").write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
        assert load_previous(tmp_path) is None
