"""The :class:`SchemaAlienFile` finding — scanner emission + census fold.

DEC-ALIEN-SCHEMA-READ-HEAL-001 (the lake_invariants half; the healer half is
``test_lake_heal_schema_alien.py``). Fails-before evidence per behavior:

* a bar file MISSING a scan column previously WARN-skipped with NO finding
  (the healer never quarantined what the dashboard read side skipped per
  poll) — now a real finding;
* a bar file with a Utf8 OHLC column previously DETONATED the WHOLE scan
  (an uncaught ``ComputeError`` inside ``count_price_violations`` — the
  dtype gate closes that latent crash);
* the census had no alien column and the engine verdict was blind to an
  alien engine-index file.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import polars as pl
from brutex.data import lake_invariants as li
from brutex.data.lake_invariants import (
    SchemaAlienFile,
    build_lake_census,
    census_to_dict,
    scan_lake_invariants,
)
from brutex.data.partition import build_partition_path


def _ts(minute: int) -> datetime:
    return datetime(2024, 6, 3, 4, minute, tzinfo=UTC)


def _dest(
    lake: Path,
    *,
    exchange: str = "NSE",
    segment: str = "INDEX",
    symbol: str = "NIFTY",
) -> Path:
    return build_partition_path(
        lake_root=lake,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval="1minute",
        year=2024,
        month=6,
    )


def _plant_missing_columns(dest: Path) -> None:
    """A readable Parquet lacking every scan column — the alien-schema shape."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"ts": [1, 2], "px": [1.0, 2.0]}).write_parquet(dest)


def _plant_utf8_ohlc(dest: Path) -> None:
    """All scan columns PRESENT but ``open`` is Utf8 — the wrong-dtype shape."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "timestamp": pl.Series([_ts(1)], dtype=pl.Datetime("us", "UTC")),
            "open": ["100.0"],
            "high": [100.0],
            "low": [100.0],
            "close": [100.0],
            "volume": [10],
            "open_interest": [None],
        }
    ).write_parquet(dest)


def _plant_str_timestamp(dest: Path) -> None:
    """All scan columns present but ``timestamp`` is Utf8 — non-temporal dtype."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "timestamp": ["2024-06-03T04:01:00"],
            "open": [100.0],
            "high": [100.0],
            "low": [100.0],
            "close": [100.0],
        }
    ).write_parquet(dest)


class TestScannerEmission:
    def test_missing_column_alien_emits_finding_and_event(self, tmp_path: Path) -> None:
        dest = _dest(tmp_path)
        _plant_missing_columns(dest)
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        aliens = [f for f in report.findings if isinstance(f, SchemaAlienFile)]
        assert len(aliens) == 1
        finding = aliens[0]
        assert finding.path == dest
        assert finding.coords is not None
        assert finding.coords.symbol == "NIFTY"
        assert finding.series is None
        assert "missing column(s):" in finding.reason
        assert report.files_scanned == 0
        assert report.files_skipped == 1
        events = [e for e in logs if e["event"] == "lake.invariant.schema_alien_file"]
        assert len(events) == 1
        assert events[0]["partition"] == "bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet"
        assert "missing column(s)" in events[0]["reason"]
        # No bare scan_skip for the alien class — the finding replaces it.
        assert not [e for e in logs if e["event"] == "lake.invariant.scan_skip"]

    def test_utf8_ohlc_dtype_alien_no_longer_crashes_scan(self, tmp_path: Path) -> None:
        # FAILS-BEFORE: a Utf8 `open` column passed the column read and then
        # detonated count_price_violations with an uncaught ComputeError,
        # killing the WHOLE scan. Now: one dtype-alien finding, scan survives.
        dest = _dest(tmp_path)
        _plant_utf8_ohlc(dest)
        report = scan_lake_invariants(tmp_path)  # must not raise
        aliens = [f for f in report.findings if isinstance(f, SchemaAlienFile)]
        assert len(aliens) == 1
        assert "dtype" in aliens[0].reason
        assert "open" in aliens[0].reason
        assert report.files_scanned == 0
        assert report.files_skipped == 1

    def test_str_timestamp_dtype_alien(self, tmp_path: Path) -> None:
        dest = _dest(tmp_path)
        _plant_str_timestamp(dest)
        report = scan_lake_invariants(tmp_path)
        aliens = [f for f in report.findings if isinstance(f, SchemaAlienFile)]
        assert len(aliens) == 1
        assert "timestamp dtype" in aliens[0].reason

    def test_corrupt_file_stays_plain_scan_skip_no_finding(self, tmp_path: Path) -> None:
        # A genuinely unreadable file (footer garbage) is NOT the alien class:
        # the footer probe itself fails → the transient-tolerant scan_skip
        # stays, no finding (a transient I/O fault must never quarantine data).
        dest = _dest(tmp_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not a parquet file at all")
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert report.findings == ()
        assert report.files_skipped == 1
        assert len([e for e in logs if e["event"] == "lake.invariant.scan_skip"]) == 1

    def test_option_tree_missing_column_alien(self, tmp_path: Path) -> None:
        dest = tmp_path / "options" / "SERIES-A" / "2024-06.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"nothing": [1]}).write_parquet(dest)
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        aliens = [f for f in report.findings if isinstance(f, SchemaAlienFile)]
        assert len(aliens) == 1
        assert aliens[0].series == "SERIES-A"
        assert aliens[0].coords is None
        assert report.option_files_skipped == 1
        events = [e for e in logs if e["event"] == "lake.invariant.schema_alien_file"]
        assert events[0]["partition"] == "options/SERIES-A/2024-06.parquet"

    def test_transient_read_failure_with_sane_schema_stays_scan_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The read fails but the footer says the schema is CANONICAL (a data-page
        # fault / transient I/O) → NOT the alien class: plain scan_skip, NO
        # finding — a transient fault must never route data into quarantine.
        dest = _dest(tmp_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "timestamp": pl.Series([_ts(1)], dtype=pl.Datetime("us", "UTC")),
                "open": [100.0],
                "high": [100.0],
                "low": [100.0],
                "close": [100.0],
            }
        ).write_parquet(dest)

        def _boom(path: Path, columns: object) -> pl.DataFrame:
            raise pl.exceptions.ComputeError("transient data-page fault")

        monkeypatch.setattr(li, "_read_key_columns", _boom)
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert report.findings == ()
        assert report.files_skipped == 1
        assert len([e for e in logs if e["event"] == "lake.invariant.scan_skip"]) == 1

    def test_corrupt_option_file_stays_scan_skip(self, tmp_path: Path) -> None:
        dest = tmp_path / "options" / "SERIES-B" / "2024-06.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"garbage")
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert report.findings == ()
        assert report.option_files_skipped == 1
        assert len([e for e in logs if e["event"] == "lake.invariant.scan_skip"]) == 1


class TestCensusFold:
    def test_alien_counts_per_segment_and_flips_engine_verdict(self, tmp_path: Path) -> None:
        _plant_missing_columns(_dest(tmp_path))  # NSE/INDEX/NIFTY — an ENGINE series
        report = scan_lake_invariants(tmp_path)
        census = build_lake_census(report)
        rows = {row.segment: row for row in census.segments}
        assert rows["INDEX"].alien_files == 1
        assert rows["INDEX"].files == 0  # the alien file was skipped, never scanned
        assert rows["CASH"].alien_files == 0
        assert census.engine_clean is False
        assert "alien bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet" in census.engine_detail
        payload = census_to_dict(census, mode="auto")
        segments = {row["segment"]: row for row in payload["segments"]}
        assert segments["INDEX"]["alien_files"] == 1
        assert payload["engine_clean"] is False

    def test_non_engine_alien_keeps_engine_clean(self, tmp_path: Path) -> None:
        _plant_missing_columns(_dest(tmp_path, segment="CASH", symbol="RELIANCE"))
        census = build_lake_census(scan_lake_invariants(tmp_path))
        rows = {row.segment: row for row in census.segments}
        assert rows["CASH"].alien_files == 1
        assert census.engine_clean is True

    def test_options_alien_counts_on_options_row_engine_clean(self, tmp_path: Path) -> None:
        dest = tmp_path / "options" / "SERIES-C" / "2024-06.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"nothing": [1]}).write_parquet(dest)
        census = build_lake_census(scan_lake_invariants(tmp_path))
        rows = {row.segment: row for row in census.segments}
        assert rows["OPTIONS"].alien_files == 1
        assert census.engine_clean is True  # an options-tree alien is never engine bars
        assert census.findings_total == 1

    def test_engine_offender_token_total_over_options_alien(self) -> None:
        # _engine_offender stays a TOTAL function: an options-tree alien is
        # filtered by _engine_relevant in production, but the token builder
        # never assumes coords (no bare-assumption crash path).
        alien = SchemaAlienFile(path=Path("/x/2024-06.parquet"), reason="r", series="S")
        assert li._engine_offender(alien) == "alien options/S/2024-06.parquet"
