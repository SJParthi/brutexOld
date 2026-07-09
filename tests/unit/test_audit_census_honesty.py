"""Census / CLI silent-green honesty — DEC-AUDIT-CENSUS-HONESTY-001 (OPS-1 + OPS-2).

Two confirmed audit defects, fixed + regression-pinned here:

* **OPS-1** — an UNREADABLE / schema-drifted ``bars/`` partition was folded into
  ``LakeScanReport.files_skipped`` with one ``lake.invariant.scan_skip`` WARN, but
  the census + ``brutex lake audit-unique`` derived their verdict purely from
  ``findings_total == 0`` — so a corrupted ENGINE-index file
  (``bars/NSE/INDEX/{NIFTY,BANKNIFTY,SENSEX}/…``) read "CLEAN — 0 findings" rc=0
  and ``doctor`` PASSed: silent green over corruption. The fix tracks
  ``engine_files_skipped``, flips ``engine_clean`` + the CLI verdict to
  ``UNREADABLE``, folds ANY skip into a non-green rc=1, and surfaces the skip in
  the human table + the ``--json`` payload; the ``doctor`` lake arm FAILs.
* **OPS-2** — ``brutex lake audit-unique --lake-root <nonexistent>`` scanned 0
  files and reported "CLEAN — 0 findings over 0 files" rc=0 forever (a typo'd
  path / dead mount reading healthy). The fix reuses the sibling
  ``_require_lake_dir`` guard (rc=1 + the same remediation message).

Lakes are REAL trees under ``tmp_path``; corruption is planted with raw bytes
(garbage) or a raw pyarrow file with a wrong schema (drift) — never the
production writer (which refuses both).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex import cli
from brutex.data.lake_invariants import (
    build_lake_census,
    census_to_dict,
    scan_lake_invariants,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.doctor import check_lake_uniqueness


def _partition(
    lake_root: Path,
    *,
    exchange: str = "NSE",
    segment: str = "INDEX",
    symbol: str = "NIFTY",
    interval: str = "1minute",
    year: int = 2025,
    month: int = 9,
) -> Path:
    return (
        lake_root
        / "bars"
        / exchange
        / segment
        / symbol
        / interval
        / f"{year:04d}"
        / f"{month:02d}.parquet"
    )


def _seed_clean(lake_root: Path, *, symbol: str = "NIFTY", segment: str = "INDEX") -> None:
    """One clean engine-index month via the PRODUCTION writer (zero skips, zero dups)."""
    ParquetWriter(lake_root=lake_root).write(
        bars=[
            CandleBar(
                timestamp=datetime(2025, 9, 1, 4, m, tzinfo=UTC),
                open=Decimal("100.00"),
                high=Decimal("101.00"),
                low=Decimal("99.00"),
                close=Decimal("100.50"),
                volume=10,
                open_interest=0,
            )
            for m in (0, 1)
        ],
        exchange="NSE",
        segment=segment,
        symbol=symbol,
        interval="1minute",
    )


def _plant_garbage(dest: Path) -> None:
    """Non-parquet bytes → ``polars`` raises a caught ``PolarsError`` → skip."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"not a parquet file at all")


def _plant_schema_drift(dest: Path) -> None:
    """A VALID parquet MISSING the OHLC columns → the ``BAR_SCAN_COLUMNS`` read raises → skip."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime(2025, 9, 1, 4, 0, tzinfo=UTC)
    # Only ``timestamp`` — no open/high/low/close, so read(columns=BAR_SCAN_COLUMNS) fails.
    table = pa.table({"timestamp": pa.array([ts], type=pa.timestamp("us", tz="UTC"))})
    pq.write_table(table, dest)


def _plant_dup(dest: Path) -> None:
    """Duplicate timestamps planted VERBATIM (the writer refuses dups) — a real POISON finding."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime(2025, 9, 1, 4, 0, tzinfo=UTC)
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


# ── OPS-1: the census fold (lake_invariants) ──────────────────────────────────


class TestCensusEngineSkipFold:
    """A skipped ENGINE-index file flips ``engine_clean`` + is surfaced everywhere."""

    def test_engine_garbage_flips_engine_verdict_unreadable(self, tmp_path: Path) -> None:
        _plant_garbage(_partition(tmp_path, symbol="NIFTY"))
        report = scan_lake_invariants(tmp_path)
        assert report.files_skipped == 1
        assert report.engine_files_skipped == 1
        census = build_lake_census(report)
        assert census.engine_clean is False
        assert census.files_skipped == 1
        assert census.engine_files_skipped == 1
        assert census.findings_total == 0  # never classified — that is the whole point
        assert "UNREADABLE" in census.engine_detail
        assert "cannot certify clean" in census.engine_detail

    def test_engine_schema_drift_counts_as_engine_skip(self, tmp_path: Path) -> None:
        _plant_schema_drift(_partition(tmp_path, symbol="BANKNIFTY"))
        report = scan_lake_invariants(tmp_path)
        assert report.engine_files_skipped == 1
        assert build_lake_census(report).engine_clean is False

    def test_non_engine_skip_reported_but_engine_stays_clean(self, tmp_path: Path) -> None:
        """A skipped CASH/reference file counts in ``files_skipped`` but never fakes engine trust."""
        _plant_garbage(_partition(tmp_path, segment="CASH", symbol="RELIANCE"))
        report = scan_lake_invariants(tmp_path)
        assert report.files_skipped == 1
        assert report.engine_files_skipped == 0  # the FALSE branch of the engine gate
        census = build_lake_census(report)
        assert census.engine_clean is True  # engine verdict untouched
        assert census.files_skipped == 1  # but the census is honestly INCOMPLETE
        assert "0 findings" in census.engine_detail

    def test_engine_finding_and_skip_detail_names_both(self, tmp_path: Path) -> None:
        _plant_dup(_partition(tmp_path, symbol="NIFTY"))
        _plant_garbage(_partition(tmp_path, symbol="BANKNIFTY"))
        census = build_lake_census(scan_lake_invariants(tmp_path))
        assert census.engine_clean is False
        assert "findings" in census.engine_detail  # the dup
        assert "UNREADABLE" in census.engine_detail  # AND the skip note

    def test_clean_lake_zero_skips(self, tmp_path: Path) -> None:
        _seed_clean(tmp_path)
        census = build_lake_census(scan_lake_invariants(tmp_path))
        assert census.engine_clean is True
        assert census.files_skipped == 0
        assert census.engine_files_skipped == 0

    def test_json_payload_surfaces_skip_counts(self, tmp_path: Path) -> None:
        _plant_garbage(_partition(tmp_path, symbol="NIFTY"))
        payload = census_to_dict(build_lake_census(scan_lake_invariants(tmp_path)), mode="auto")
        assert payload["files_skipped"] == 1
        assert payload["engine_files_skipped"] == 1
        assert payload["engine_clean"] is False


# ── OPS-1: the CLI (brutex lake audit-unique) ─────────────────────────────────


class TestCliAuditUniqueSkipHonesty:
    def test_engine_garbage_non_green_table_and_verdict(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_clean(tmp_path)  # a real readable engine file beside the corruption
        _plant_garbage(_partition(tmp_path, symbol="BANKNIFTY"))
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path)])
        assert rc == cli.EXIT_USER_ERROR
        out = capsys.readouterr().out
        assert "skipped (unreadable): 1  (engine-index: 1)" in out
        assert "engine indexes (NIFTY / BANKNIFTY / SENSEX): UNREADABLE — " in out

    def test_engine_schema_drift_non_green(self, tmp_path: Path) -> None:
        _plant_schema_drift(_partition(tmp_path, symbol="NIFTY"))
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path)])
        assert rc == cli.EXIT_USER_ERROR

    def test_engine_garbage_json_surfaces_skip(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BRUTEX_LAKE_HEAL", raising=False)
        _plant_garbage(_partition(tmp_path, symbol="NIFTY"))
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path), "--json"])
        assert rc == cli.EXIT_USER_ERROR
        out = capsys.readouterr().out
        payload = json.loads(out[out.index("{") :])
        assert payload["files_skipped"] == 1
        assert payload["engine_files_skipped"] == 1
        assert payload["engine_clean"] is False

    def test_non_engine_skip_is_non_green_but_verdict_clean(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A reference-lake skip still leaves the census INCOMPLETE → rc=1, engine verdict CLEAN."""
        _seed_clean(tmp_path)
        _plant_garbage(_partition(tmp_path, segment="CASH", symbol="RELIANCE"))
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path)])
        assert rc == cli.EXIT_USER_ERROR
        out = capsys.readouterr().out
        assert "skipped (unreadable): 1  (engine-index: 0)" in out
        assert "engine indexes (NIFTY / BANKNIFTY / SENSEX): CLEAN — " in out

    def test_clean_lake_still_rc_ok_no_regression(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_clean(tmp_path)
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path)])
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "skipped (unreadable): 0  (engine-index: 0)" in out
        assert "engine indexes (NIFTY / BANKNIFTY / SENSEX): CLEAN — " in out

    def test_real_dup_still_poisoned_rc1_teeth_intact(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _plant_dup(_partition(tmp_path, symbol="NIFTY"))
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(tmp_path)])
        assert rc == cli.EXIT_USER_ERROR
        out = capsys.readouterr().out
        assert "engine indexes (NIFTY / BANKNIFTY / SENSEX): POISONED — " in out


# ── OPS-2: the missing-lake-root guard ────────────────────────────────────────


class TestCliAuditUniqueMissingRoot:
    def test_nonexistent_lake_root_refused_rc1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "no_such_lake"
        rc = cli.main(["lake", "audit-unique", "--lake-root", str(missing)])
        assert rc == cli.EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert f"lake root not found: {missing}" in err
        assert "brutex lake audit-unique" in err
        # The refusal is a HARD gate — the dead mount was never scanned / materialised.
        assert not missing.exists()


# ── OPS-1: the doctor lake arm ────────────────────────────────────────────────


class TestDoctorLakeArmSkipHonesty:
    def test_engine_garbage_fails_doctor(self, tmp_path: Path) -> None:
        _seed_clean(tmp_path)
        _plant_garbage(_partition(tmp_path, symbol="BANKNIFTY"))
        result = check_lake_uniqueness(tmp_path)
        assert result.status == "FAIL"
        assert "census INCOMPLETE" in result.detail
        assert "UNREADABLE" in result.detail

    def test_engine_schema_drift_fails_doctor(self, tmp_path: Path) -> None:
        # SENSEX is a BSE-index engine coord — plant the drift there so the
        # engine-scoped doctor scan reaches it.
        _plant_schema_drift(_partition(tmp_path, exchange="BSE", symbol="SENSEX"))
        result = check_lake_uniqueness(tmp_path)
        assert result.status == "FAIL"
        assert "census INCOMPLETE" in result.detail

    def test_dup_and_skip_fails_naming_both(self, tmp_path: Path) -> None:
        _plant_dup(_partition(tmp_path, symbol="NIFTY"))
        _plant_garbage(_partition(tmp_path, symbol="BANKNIFTY"))
        result = check_lake_uniqueness(tmp_path)
        assert result.status == "FAIL"
        assert "engine-index partitions poisoned" in result.detail
        assert "UNREADABLE (skipped)" in result.detail

    def test_clean_engine_still_passes(self, tmp_path: Path) -> None:
        _seed_clean(tmp_path)
        assert check_lake_uniqueness(tmp_path).status == "PASS"
