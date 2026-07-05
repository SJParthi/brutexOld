"""Tests for the ``brutex doctor`` health-check command (DEC-DOCTOR-001).

Covers the public surface of :mod:`brutex.doctor` end-to-end:

* :func:`check_audit_chain` against real + corrupt + missing files
* :func:`check_runtime_file` against present + absent + malformed
* :func:`run_doctor` aggregating both checks
* :func:`main` returning the right exit code on PASS/FAIL
* :class:`DoctorReport.format` produces the expected emoji shape
* :func:`check_lake_uniqueness` — the bounded engine-index duplicate census
  (DEC-LAKE-HEAL-001)
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
from pathlib import Path

import pytest
from brutex import doctor as doctor_module
from brutex.doctor import (
    CheckResult,
    DoctorReport,
    check_audit_chain,
    check_audit_replay_consistency,
    check_derived_freshness,
    check_lake_uniqueness,
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

    def test_all_skip_returns_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Skip the wheel-freshness stages via their shared env override so
        # this aggregate test stays hermetic (all-SKIP regardless of wheel
        # state). ONE escape hatch covers BOTH wheels deliberately.
        monkeypatch.setenv("BRUTEX_SKIP_WHEEL_FRESHNESS", "1")
        report = run_doctor(
            audit_file=None,
            audit_dir=tmp_path / "no_audit",
            runtime_file=tmp_path / "no_runtime.json",
            lake_root=tmp_path / "no_lake",
        )
        assert report.ok is True
        # 7 checks: audit_chain + runtime_file + audit_replay + derived_freshness
        # + lake_uniqueness (DEC-LAKE-HEAL-001) + wheel_freshness (R2-CC-G1)
        # + wheel_freshness_fno_math (DEC-FNOMATH-WHEEL-FRESHNESS-001)
        assert len(report.checks) == 7
        statuses = {c.name: c.status for c in report.checks}
        assert statuses == {
            "audit_chain": "SKIP",
            "runtime_file": "SKIP",
            "audit_replay": "SKIP",
            "derived_freshness": "SKIP",
            "lake_uniqueness": "SKIP",
            "wheel_freshness": "SKIP",
            "wheel_freshness_fno_math": "SKIP",
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


# ────────────────────────────────────────────────────────────────────────
# lake_uniqueness (DEC-LAKE-HEAL-001)
# ────────────────────────────────────────────────────────────────────────


def _write_engine_partition(lake_root: Path, *, symbol: str = "NIFTY", month: int = 9) -> None:
    """One clean 1-minute engine-index month written through the production writer."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter

    ParquetWriter(lake_root=lake_root).write(
        bars=[
            CandleBar(
                timestamp=datetime(2025, month, 1, 4, 0, tzinfo=UTC),
                open=Decimal("100.00"),
                high=Decimal("101.00"),
                low=Decimal("99.00"),
                close=Decimal("100.50"),
                volume=10,
                open_interest=0,
            )
        ],
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        interval="1minute",
    )


def _plant_dup_partition(
    lake_root: Path, *, segment: str = "INDEX", symbol: str = "NIFTY", month: int = 9
) -> Path:
    """Plant a duplicate-timestamp month file with raw pyarrow.

    The fixed writer refuses to store duplicates (DEC-LAKE-WRITE-UNIQUE-001),
    so the poison is planted exactly like the operator's pre-fix lake.
    """
    from datetime import UTC, datetime

    import pyarrow as pa
    import pyarrow.parquet as pq

    dest = (
        lake_root / "bars" / "NSE" / segment / symbol / "1minute" / "2025" / f"{month:02d}.parquet"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime(2025, month, 1, 4, 0, tzinfo=UTC)
    prices = [100.0, 100.0]
    table = pa.table(
        {
            "timestamp": pa.array([ts, ts], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([10, 10], type=pa.int64()),
            "open_interest": pa.array([None, None], type=pa.int64()),
        }
    )
    pq.write_table(table, dest)
    return dest


class TestLakeUniqueness:
    """The bounded ENGINE-index duplicate census check (DEC-LAKE-HEAL-001)."""

    def test_skip_when_no_lake(self, tmp_path: Path) -> None:
        result = check_lake_uniqueness(tmp_path / "no_lake")
        assert result.status == "SKIP"
        assert "nothing to verify" in result.detail

    def test_pass_on_clean_engine_series(self, tmp_path: Path) -> None:
        _write_engine_partition(tmp_path)
        result = check_lake_uniqueness(tmp_path)
        assert result.status == "PASS"
        assert "0 findings over 1 files" in result.detail
        assert "last full sweep" not in result.detail

    def test_pass_detail_includes_last_sweep_sentinel(self, tmp_path: Path) -> None:
        from brutex.data.lake_heal import SENTINEL_RELPATH

        _write_engine_partition(tmp_path)
        sentinel = tmp_path / SENTINEL_RELPATH
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(
            json.dumps(
                {
                    "version": 1,
                    "lake_token": "a" * 64,
                    "completed_utc": "2026-07-03T05:00:00Z",
                    "findings": 2,
                    "healed": 2,
                }
            ),
            encoding="utf-8",
        )
        result = check_lake_uniqueness(tmp_path)
        assert result.status == "PASS"
        assert "last full sweep: 2026-07-03T05:00:00Z, findings=2, healed=2" in result.detail

    def test_bounded_by_design_ignores_non_engine_poison(self, tmp_path: Path) -> None:
        """A poisoned non-engine symbol never flips the doctor — whole-lake is the CLI's job."""
        _write_engine_partition(tmp_path)
        _plant_dup_partition(tmp_path, segment="CASH", symbol="RELIANCE")
        result = check_lake_uniqueness(tmp_path)
        assert result.status == "PASS"

    def test_fail_names_offenders_capped_with_remediation(self, tmp_path: Path) -> None:
        for month in (6, 7, 8, 9):
            _plant_dup_partition(tmp_path, month=month)
        result = check_lake_uniqueness(tmp_path)
        assert result.status == "FAIL"
        assert "engine-index partitions poisoned" in result.detail
        # Up to 3 offenders named, the 4th folded into the honest "+1 more".
        assert "bars/NSE/INDEX/NIFTY/1minute/2025/06.parquet" in result.detail
        assert "bars/NSE/INDEX/NIFTY/1minute/2025/08.parquet" in result.detail
        assert "bars/NSE/INDEX/NIFTY/1minute/2025/09.parquet" not in result.detail
        assert "(+1 more)" in result.detail
        # Remediation pointers: the auto-heal note, the census CLI, the quarantine dir.
        assert "BRUTEX_LAKE_HEAL=auto" in result.detail
        assert "brutex lake audit-unique --json" in result.detail
        assert ".quarantine" in result.detail

    def test_fail_when_probe_errors(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.data import lake_invariants

        (tmp_path / "bars").mkdir()

        def _boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("scanner exploded")

        monkeypatch.setattr(lake_invariants, "scan_lake_invariants", _boom)
        result = check_lake_uniqueness(tmp_path)
        assert result.status == "FAIL"
        assert "lake uniqueness probe errored: scanner exploded" in result.detail

    def test_offender_naming_both_branches(self) -> None:
        """``_lake_offender``: coords-carrying findings by relpath, others by repr."""

        class _Coords:
            def relpath(self) -> str:
                return "bars/NSE/INDEX/NIFTY/1minute/2025/09.parquet"

        class _WithCoords:
            coords = _Coords()

        named = doctor_module._lake_offender(_WithCoords())
        assert named == "bars/NSE/INDEX/NIFTY/1minute/2025/09.parquet"
        assert doctor_module._lake_offender("stray") == "'stray'"
