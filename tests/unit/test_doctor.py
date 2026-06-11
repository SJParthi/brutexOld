"""Tests for the ``brutex doctor`` health-check command (DEC-DOCTOR-001).

Covers the public surface of :mod:`brutex.doctor` end-to-end:

* :func:`check_audit_chain` against real + corrupt + missing files
* :func:`check_runtime_file` against present + absent + malformed
* :func:`run_doctor` aggregating both checks
* :func:`main` returning the right exit code on PASS/FAIL
* :class:`DoctorReport.format` produces the expected emoji shape
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from brutex.doctor import (
    CheckResult,
    DoctorReport,
    check_audit_chain,
    check_audit_replay_consistency,
    check_derived_freshness,
    check_runtime_file,
    main,
    run_doctor,
)
from brutex.orchestrator.audit import AuditWriter

# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def good_audit_log(tmp_path: Path) -> Path:
    """Write a small valid audit log and return its path."""
    log_path = tmp_path / "audit.jsonl"
    with AuditWriter.open(log_path) as log:
        log.append(action="RUN_START", subject="run:test", payload={"k": 1})
        log.append(action="PHASE_START", subject="phase:A", payload={"k": 2})
        log.append(action="RUN_COMPLETE", subject="run:test", payload={"k": 3})
    return log_path


@pytest.fixture
def good_audit_dir(tmp_path: Path, good_audit_log: Path) -> Path:
    """Return a directory containing one valid audit log."""
    # ``good_audit_log`` already wrote into tmp_path/audit.jsonl; just return parent.
    return good_audit_log.parent


# ────────────────────────────────────────────────────────────────────────
# CheckResult / DoctorReport
# ────────────────────────────────────────────────────────────────────────


class TestDoctorReport:
    """Aggregate report dataclass behaviour."""

    def test_empty_report_is_ok(self) -> None:
        report = DoctorReport()
        assert report.ok is True
        assert report.checks == []

    def test_pass_only_is_ok(self) -> None:
        report = DoctorReport()
        report.add(CheckResult(name="x", status="PASS", detail="green"))
        assert report.ok is True

    def test_skip_does_not_fail(self) -> None:
        report = DoctorReport()
        report.add(CheckResult(name="x", status="SKIP", detail="absent"))
        assert report.ok is True

    def test_single_fail_makes_report_not_ok(self) -> None:
        report = DoctorReport()
        report.add(CheckResult(name="x", status="PASS", detail="green"))
        report.add(CheckResult(name="y", status="FAIL", detail="broken"))
        assert report.ok is False

    def test_format_includes_emojis_and_summary(self) -> None:
        report = DoctorReport()
        report.add(CheckResult(name="x", status="PASS", detail="green"))
        text = report.format()
        assert "✅" in text
        assert "x" in text
        assert "OVERALL:" in text
        assert "PASS" in text

    def test_format_shows_overall_fail_on_red(self) -> None:
        report = DoctorReport()
        report.add(CheckResult(name="x", status="FAIL", detail="broken"))
        text = report.format()
        assert "❌" in text
        assert "FAIL" in text


# ────────────────────────────────────────────────────────────────────────
# check_audit_chain
# ────────────────────────────────────────────────────────────────────────


class TestCheckAuditChain:
    """The first health check — audit chain integrity."""

    def test_pass_on_valid_log_via_audit_file(self, good_audit_log: Path) -> None:
        result = check_audit_chain(audit_file=good_audit_log, audit_dir=Path("/nonexistent"))
        assert result.status == "PASS"
        assert "3 entries verified" in result.detail
        assert result.name == "audit_chain"

    def test_pass_on_valid_log_via_audit_dir(self, good_audit_dir: Path) -> None:
        result = check_audit_chain(audit_file=None, audit_dir=good_audit_dir)
        assert result.status == "PASS"
        assert "entries verified" in result.detail

    def test_skip_when_no_audit_logs_anywhere(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "empty_audit"
        empty_dir.mkdir()
        result = check_audit_chain(audit_file=None, audit_dir=empty_dir)
        assert result.status == "SKIP"
        assert "no audit logs" in result.detail

    def test_skip_when_audit_dir_does_not_exist(self, tmp_path: Path) -> None:
        result = check_audit_chain(audit_file=None, audit_dir=tmp_path / "nope")
        assert result.status == "SKIP"

    def test_fail_when_explicit_audit_file_missing(self, tmp_path: Path) -> None:
        result = check_audit_chain(audit_file=tmp_path / "missing.jsonl", audit_dir=tmp_path)
        assert result.status == "FAIL"
        assert "does not exist" in result.detail

    def test_fail_on_corrupt_chain(self, good_audit_log: Path) -> None:
        # Tamper with the second entry's payload to break the hash chain.
        lines = good_audit_log.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[1])
        tampered["payload"] = {"k": 999}  # changes entry_hash w/o recomputing
        lines[1] = json.dumps(tampered)
        good_audit_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = check_audit_chain(audit_file=good_audit_log, audit_dir=Path("/nonexistent"))
        assert result.status == "FAIL"
        assert "corrupt" in result.detail


# ────────────────────────────────────────────────────────────────────────
# check_runtime_file
# ────────────────────────────────────────────────────────────────────────


class TestCheckRuntimeFile:
    """The second health check — runtime file presence + parseability."""

    def test_skip_when_absent(self, tmp_path: Path) -> None:
        result = check_runtime_file(runtime_file=tmp_path / "absent.json")
        assert result.status == "SKIP"
        assert "no runtime file" in result.detail

    def test_pass_on_valid_object(self, tmp_path: Path) -> None:
        rt = tmp_path / "runtime.json"
        rt.write_text(json.dumps({"pid": 1234, "run_id": "abc"}), encoding="utf-8")
        result = check_runtime_file(runtime_file=rt)
        assert result.status == "PASS"
        assert "2 keys" in result.detail

    def test_fail_on_malformed_json(self, tmp_path: Path) -> None:
        rt = tmp_path / "broken.json"
        rt.write_text("{not valid json", encoding="utf-8")
        result = check_runtime_file(runtime_file=rt)
        assert result.status == "FAIL"
        assert "malformed JSON" in result.detail

    def test_fail_when_not_a_json_object(self, tmp_path: Path) -> None:
        rt = tmp_path / "array.json"
        rt.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        result = check_runtime_file(runtime_file=rt)
        assert result.status == "FAIL"
        assert "not a JSON object" in result.detail


# ────────────────────────────────────────────────────────────────────────
# run_doctor (aggregator)
# ────────────────────────────────────────────────────────────────────────


class TestCheckAuditReplayConsistency:
    """L3 audit-replay oracle (DEC-L3-REPLAY-ORACLE-001) doctor check."""

    def test_skip_when_no_live_subjects_file(self, tmp_path: Path) -> None:
        result = check_audit_replay_consistency(
            audit_file=None,
            audit_dir=tmp_path,
            live_subjects_file=None,
        )
        assert result.status == "SKIP"
        assert "opt-in" in result.detail

    def test_fail_when_live_subjects_file_missing(self, tmp_path: Path) -> None:
        result = check_audit_replay_consistency(
            audit_file=None,
            audit_dir=tmp_path,
            live_subjects_file=tmp_path / "missing.txt",
        )
        assert result.status == "FAIL"
        assert "does not exist" in result.detail

    def test_skip_when_no_audit_log_in_dir(self, tmp_path: Path) -> None:
        live = tmp_path / "live.txt"
        live.write_text("subj-a\n", encoding="utf-8")
        result = check_audit_replay_consistency(
            audit_file=None,
            audit_dir=tmp_path / "empty",
            live_subjects_file=live,
        )
        assert result.status == "SKIP"
        assert "no audit logs found" in result.detail

    def test_fail_when_audit_file_missing(self, tmp_path: Path) -> None:
        live = tmp_path / "live.txt"
        live.write_text("subj-a\n", encoding="utf-8")
        result = check_audit_replay_consistency(
            audit_file=tmp_path / "ghost.jsonl",
            audit_dir=tmp_path,
            live_subjects_file=live,
        )
        assert result.status == "FAIL"
        assert "does not exist" in result.detail

    def test_pass_when_subjects_align(self, good_audit_log: Path, tmp_path: Path) -> None:
        """Empty audit-add-subjects + empty live snapshot → PASS."""
        live = tmp_path / "live.txt"
        live.write_text("", encoding="utf-8")
        result = check_audit_replay_consistency(
            audit_file=good_audit_log,
            audit_dir=tmp_path,
            live_subjects_file=live,
        )
        assert result.status == "PASS"
        assert "match audit" in result.detail

    def test_pass_with_custom_action_filter(self, good_audit_log: Path, tmp_path: Path) -> None:
        # good_audit_log has actions RUN_START, PHASE_START, RUN_COMPLETE
        # with subjects run:test, phase:A, run:test. Use action_filter
        # to project just RUN_START → {run:test}.
        live = tmp_path / "live.txt"
        live.write_text("run:test\n", encoding="utf-8")
        result = check_audit_replay_consistency(
            audit_file=good_audit_log,
            audit_dir=tmp_path,
            live_subjects_file=live,
            action_filter="RUN_START",
        )
        assert result.status == "PASS"

    def test_fail_on_missing_from_live(self, good_audit_log: Path, tmp_path: Path) -> None:
        # Audit RUN_START subject is run:test; live is empty → missing.
        live = tmp_path / "live.txt"
        live.write_text("", encoding="utf-8")
        result = check_audit_replay_consistency(
            audit_file=good_audit_log,
            audit_dir=tmp_path,
            live_subjects_file=live,
            action_filter="RUN_START",
        )
        assert result.status == "FAIL"
        assert "missing_from_live=1" in result.detail

    def test_fail_on_extra_in_live(self, good_audit_log: Path, tmp_path: Path) -> None:
        live = tmp_path / "live.txt"
        live.write_text("rogue\n", encoding="utf-8")
        result = check_audit_replay_consistency(
            audit_file=good_audit_log,
            audit_dir=tmp_path,
            live_subjects_file=live,
            action_filter="add",  # default; no add-subjects in audit
        )
        assert result.status == "FAIL"
        assert "extra_in_live=1" in result.detail

    def test_blank_lines_in_live_file_are_skipped(
        self, good_audit_log: Path, tmp_path: Path
    ) -> None:
        live = tmp_path / "live.txt"
        live.write_text("\n\n  \nrun:test\n\n", encoding="utf-8")
        result = check_audit_replay_consistency(
            audit_file=good_audit_log,
            audit_dir=tmp_path,
            live_subjects_file=live,
            action_filter="RUN_START",
        )
        assert result.status == "PASS"


class TestRunDoctor:
    """End-to-end aggregator behaviour."""

    def test_all_skip_returns_ok(self, tmp_path: Path) -> None:
        report = run_doctor(
            audit_file=None,
            audit_dir=tmp_path / "no_audit",
            runtime_file=tmp_path / "no_runtime.json",
            lake_root=tmp_path / "no_lake",
        )
        assert report.ok is True
        # 4 checks: audit_chain + runtime_file + audit_replay + derived_freshness
        assert len(report.checks) == 4
        statuses = {c.name: c.status for c in report.checks}
        assert statuses == {
            "audit_chain": "SKIP",
            "runtime_file": "SKIP",
            "audit_replay": "SKIP",
            "derived_freshness": "SKIP",
        }

    def test_audit_pass_runtime_skip(self, good_audit_log: Path, tmp_path: Path) -> None:
        report = run_doctor(
            audit_file=good_audit_log,
            audit_dir=tmp_path,
            runtime_file=tmp_path / "no_runtime.json",
            lake_root=tmp_path / "no_lake",
        )
        assert report.ok is True
        statuses = {c.name: c.status for c in report.checks}
        assert statuses["audit_chain"] == "PASS"
        assert statuses["runtime_file"] == "SKIP"
        assert statuses["audit_replay"] == "SKIP"

    def test_audit_fail_marks_overall_not_ok(self, tmp_path: Path) -> None:
        report = run_doctor(
            audit_file=tmp_path / "missing.jsonl",
            audit_dir=tmp_path,
            runtime_file=tmp_path / "no_runtime.json",
            lake_root=tmp_path / "no_lake",
        )
        assert report.ok is False

    def test_replay_pass_when_subjects_match(self, good_audit_log: Path, tmp_path: Path) -> None:
        """When live_subjects_file matches the audit's add-subjects,
        the replay check passes."""
        # good_audit_log has actions RUN_START / PHASE_START / RUN_COMPLETE,
        # NOT "add" — so default action_filter="add" projects empty set.
        # An empty live_subjects_file matches → PASS.
        live = tmp_path / "live.txt"
        live.write_text("", encoding="utf-8")
        report = run_doctor(
            audit_file=good_audit_log,
            audit_dir=tmp_path,
            runtime_file=tmp_path / "no_runtime.json",
            live_subjects_file=live,
            lake_root=tmp_path / "no_lake",
        )
        statuses = {c.name: c.status for c in report.checks}
        assert statuses["audit_replay"] == "PASS"

    def test_replay_fail_when_subjects_mismatch(self, good_audit_log: Path, tmp_path: Path) -> None:
        # Live snapshot claims a subject the audit log never recorded
        live = tmp_path / "live.txt"
        live.write_text("rogue-subject\n", encoding="utf-8")
        report = run_doctor(
            audit_file=good_audit_log,
            audit_dir=tmp_path,
            runtime_file=tmp_path / "no_runtime.json",
            live_subjects_file=live,
            lake_root=tmp_path / "no_lake",
        )
        assert report.ok is False
        statuses = {c.name: c.status for c in report.checks}
        assert statuses["audit_replay"] == "FAIL"


# ────────────────────────────────────────────────────────────────────────
# main (CLI entry point)
# ────────────────────────────────────────────────────────────────────────


class TestMain:
    """The ``brutex doctor`` CLI exit-code contract."""

    def test_returns_zero_on_all_pass(
        self, good_audit_log: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            audit_file=good_audit_log,
            audit_dir=tmp_path,
            runtime_file=tmp_path / "absent.json",
            lake_root=tmp_path / "no_lake",
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert "OVERALL:" in captured.out
        assert "PASS" in captured.out

    def test_returns_one_on_any_fail(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            audit_file=tmp_path / "missing.jsonl",
            audit_dir=tmp_path,
            runtime_file=tmp_path / "absent.json",
            lake_root=tmp_path / "no_lake",
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "FAIL" in captured.out


# ────────────────────────────────────────────────────────────────────────
# derived_freshness (DEC-TF-MATERIALIZE-002)
# ────────────────────────────────────────────────────────────────────────


def _write_lake_partition(lake_root: Path, interval: str, frame: object) -> None:
    """Write one synthetic month partition in the canonical lake layout."""
    import polars as pl

    out = lake_root / "bars" / "NSE" / "INDEX" / "NIFTY" / interval / "2025"
    out.mkdir(parents=True, exist_ok=True)
    assert isinstance(frame, pl.DataFrame)
    frame.rename({"ts_utc": "timestamp"}).write_parquet(out / "06.parquet")


def _synthetic_1m() -> object:
    from datetime import UTC, datetime, timedelta

    import polars as pl

    open_utc = datetime(2025, 6, 9, 3, 45, tzinfo=UTC).replace(tzinfo=None)
    closes = [100.0 + i * 0.1 for i in range(30)]
    return pl.DataFrame(
        {
            "ts_utc": [open_utc + timedelta(minutes=i) for i in range(30)],
            "open": [c - 0.05 for c in closes],
            "high": [c + 0.05 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1000] * 30,
        },
        schema={
            "ts_utc": pl.Datetime,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )


class TestDerivedFreshness:
    def test_skip_when_no_lake(self, tmp_path: Path) -> None:
        result = check_derived_freshness(tmp_path / "no_lake")
        assert result.status == "SKIP"
        assert "nothing to verify" in result.detail

    def test_skip_when_no_derived_partitions(self, tmp_path: Path) -> None:
        _write_lake_partition(tmp_path, "1minute", _synthetic_1m())
        result = check_derived_freshness(tmp_path)
        assert result.status == "SKIP"
        assert "no symbol has stored derived timeframes" in result.detail

    def test_pass_when_stored_equals_rederivation(self, tmp_path: Path) -> None:
        import polars as pl
        from brutex.data.tf_resampler import resample_1m_to_tf

        df_1m = _synthetic_1m()
        assert isinstance(df_1m, pl.DataFrame)
        _write_lake_partition(tmp_path, "1minute", df_1m)
        _write_lake_partition(tmp_path, "5minute", resample_1m_to_tf(df_1m, 5))
        result = check_derived_freshness(tmp_path)
        assert result.status == "PASS"
        assert "NIFTY" in result.detail

    def test_fail_when_stored_table_is_stale(self, tmp_path: Path) -> None:
        import polars as pl
        from brutex.data.tf_resampler import resample_1m_to_tf

        df_1m = _synthetic_1m()
        assert isinstance(df_1m, pl.DataFrame)
        _write_lake_partition(tmp_path, "1minute", df_1m)
        stale = resample_1m_to_tf(df_1m, 5).with_columns((pl.col("close") + 1.0).alias("close"))
        _write_lake_partition(tmp_path, "5minute", stale)
        result = check_derived_freshness(tmp_path)
        assert result.status == "FAIL"
        assert "5minute" in result.detail

    def test_skip_when_symbol_has_derived_but_no_1minute_base(self, tmp_path: Path) -> None:
        # A derived partition with NO 1minute base: the symbol cannot be
        # verified, so it is passed over (covering the no-base continue) and
        # the probe lands on the honest SKIP.
        import polars as pl
        from brutex.data.tf_resampler import resample_1m_to_tf

        df_1m = _synthetic_1m()
        assert isinstance(df_1m, pl.DataFrame)
        _write_lake_partition(tmp_path, "5minute", resample_1m_to_tf(df_1m, 5))
        result = check_derived_freshness(tmp_path)
        assert result.status == "SKIP"
        assert "no symbol has stored derived timeframes" in result.detail

    def test_fail_when_probe_errors_on_corrupt_parquet(self, tmp_path: Path) -> None:
        # A corrupt 1minute parquet makes the probe raise — the check must
        # REPORT the error as a FAIL, never crash the doctor run.
        import polars as pl
        from brutex.data.tf_resampler import resample_1m_to_tf

        df_1m = _synthetic_1m()
        assert isinstance(df_1m, pl.DataFrame)
        _write_lake_partition(tmp_path, "5minute", resample_1m_to_tf(df_1m, 5))
        corrupt = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2025"
        corrupt.mkdir(parents=True, exist_ok=True)
        (corrupt / "06.parquet").write_bytes(b"this is not a parquet file")
        result = check_derived_freshness(tmp_path)
        assert result.status == "FAIL"
        assert "freshness probe errored" in result.detail
