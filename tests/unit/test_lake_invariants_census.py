"""Census skip-fold honesty — DEC-AUDIT-CENSUS-HONESTY-001 (OPS-1, the fold half).

Relocated from ``test_audit_census_honesty.py`` so the per-module coverage
DISCOVERY gate (``scripts/ci/coverage_gate_discovery.py``, which stem-maps a
touched module to ``test_<stem>*.py`` files that IMPORT it) actually exercises
:mod:`brutex.data.lake_invariants`'s census-honesty branches under its own gate
— the orphan ``test_audit_census_honesty`` file stem-matched no module, so the
new branches read < 100% under the blocking gate (hostile-review BLOCKER-2).

Confirmed defects fixed + pinned here:

* **OPS-1** — an UNREADABLE / schema-drifted ``bars/`` partition was folded into
  ``LakeScanReport.files_skipped`` but the census verdict derived purely from
  ``findings_total == 0`` — so a corrupted ENGINE-index file read "0 findings"
  and ``engine_clean`` silently green. The fix tracks ``engine_files_skipped``,
  flips ``engine_clean``, and surfaces the skip in ``census_to_dict``.
* **MAJOR** (hostile-review) — an ENGINE-index file whose PATH fails to parse
  (a non-integer / month-13 year dir) was folded into ``files_skipped`` only
  (its coords un-parsed), so the census mis-attributed it as a reference-only
  skip. The fix derives ``(exchange, segment, symbol)`` POSITIONALLY from the
  glob tail on the path-parse-failure branch so it flips the engine verdict too
  — agreeing with ``doctor`` (which FAILs) and the CLI (which reads UNREADABLE).

Lakes are REAL trees under ``tmp_path``; corruption is planted with raw bytes
(garbage) or a raw pyarrow file with a wrong schema (drift) — never the
production writer (which refuses both).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.data.lake_invariants import (
    _engine_coord_from_path,
    build_lake_census,
    census_to_dict,
    scan_lake_invariants,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter


@pytest.fixture(autouse=True)
def _python_oracle_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin this suite to the EXPLICIT flag-OFF (Python oracle) arm.

    Conscious W-5 reconciliation (recorded in the Wave-D lane scratchpad):
    since the ``BRUTEX_LAKE_WAVED_RUST`` default flipped ON, an unset env
    routes ``scan_lake_invariants`` through the crate — but THIS suite is
    the verbatim PYTHON scanner's verification (the live oracle the burn-in
    + every degrade serve; its white-box read-count spies / injected faults
    / log-text pins are Python-arm implementation contracts the engaged
    crate deliberately does not share — deviation D-20), so it runs under
    the operator kill switch. The crate arm's gates are the scan
    differential (``tests/integration/test_waved_scan_differential.py``,
    27/27 engaged) + the waved seam suite's explicit flag-ON tests.
    """
    monkeypatch.setenv("BRUTEX_LAKE_WAVED_RUST", "0")


def _partition(
    lake_root: Path,
    *,
    exchange: str = "NSE",
    segment: str = "INDEX",
    symbol: str = "NIFTY",
    interval: str = "1minute",
    year: str = "2025",
    month: str = "09",
) -> Path:
    """A ``bars/…/<year>/<month>.parquet`` destination path (year/month are RAW
    strings so a malformed non-integer year can be planted for the MAJOR test)."""
    return lake_root / "bars" / exchange / segment / symbol / interval / year / f"{month}.parquet"


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


def _plant_out_of_range(dest: Path) -> None:
    """A sound-schema bars file whose ``Datetime`` column holds an i64 value no
    Python ``datetime`` can represent (``2**62`` µs ≈ year 146,000) — the
    round-18 crash class, now classified under ``datetime_out_of_range_errors``
    and skipped like an unreadable file (the fold's NEW skip site)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    price = [100.0]
    table = pa.table(
        {
            "timestamp": pa.array([2**62], type=pa.int64()).cast(pa.timestamp("us", tz="UTC")),
            "open": pa.array(price, type=pa.float64()),
            "high": pa.array(price, type=pa.float64()),
            "low": pa.array(price, type=pa.float64()),
            "close": pa.array(price, type=pa.float64()),
            "volume": pa.array([10], type=pa.int64()),
            "open_interest": pa.array([None], type=pa.int64()),
        }
    )
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

    def test_first_skipped_path_captured_first_in_scan_order(self, tmp_path: Path) -> None:
        """``first_skipped_path`` names the FIRST unreadable file (report + census).

        Two unreadable engine files are planted; series are scanned in sorted
        order (``BANKNIFTY`` before ``NIFTY``), so the BANKNIFTY skip wins — and
        the second skip exercises the ``_note_first_skip`` already-set arm.
        """
        banknifty = _partition(tmp_path, symbol="BANKNIFTY")
        nifty = _partition(tmp_path, symbol="NIFTY")
        _plant_garbage(banknifty)
        _plant_garbage(nifty)
        report = scan_lake_invariants(tmp_path)
        assert report.files_skipped == 2
        assert report.first_skipped_path == str(banknifty)
        census = build_lake_census(report)
        assert census.first_skipped_path == str(banknifty)

    def test_clean_lake_first_skipped_path_is_none(self, tmp_path: Path) -> None:
        _seed_clean(tmp_path)
        report = scan_lake_invariants(tmp_path)
        assert report.first_skipped_path is None
        assert build_lake_census(report).first_skipped_path is None

    def test_json_payload_surfaces_skip_counts(self, tmp_path: Path) -> None:
        _plant_garbage(_partition(tmp_path, symbol="NIFTY"))
        census = build_lake_census(scan_lake_invariants(tmp_path))
        payload = census_to_dict(census, mode="auto")
        assert payload["files_skipped"] == 1
        assert payload["engine_files_skipped"] == 1
        assert payload["engine_clean"] is False
        # MINOR (hostile-review): the FIRST offending file is named in json too,
        # not only on the human CLI disambiguation line.
        assert payload["first_skipped_path"] == census.first_skipped_path


class TestDatetimeOutOfRangeSkipFold:
    """The main-fold's NEW bars skip site (``datetime_out_of_range_errors``)
    rides the SAME census-honesty law: an out-of-range ENGINE file flips the
    verdict; a reference-only one is counted but never fakes engine distrust."""

    def test_engine_out_of_range_flips_engine_verdict(self, tmp_path: Path) -> None:
        _plant_out_of_range(_partition(tmp_path, symbol="NIFTY"))
        report = scan_lake_invariants(tmp_path)
        assert report.files_skipped == 1
        assert report.engine_files_skipped == 1
        census = build_lake_census(report)
        assert census.engine_clean is False
        assert census.first_skipped_path == str(_partition(tmp_path, symbol="NIFTY"))
        assert "UNREADABLE" in census.engine_detail

    def test_non_engine_out_of_range_stays_engine_clean(self, tmp_path: Path) -> None:
        # The FALSE arm of the engine gate on the datetime-out-of-range skip
        # site: a CASH/reference file is skipped + counted + named, but the
        # engine verdict is untouched.
        bad = _partition(tmp_path, segment="CASH", symbol="RELIANCE")
        _plant_out_of_range(bad)
        report = scan_lake_invariants(tmp_path)
        assert report.files_skipped == 1
        assert report.engine_files_skipped == 0
        assert report.first_skipped_path == str(bad)  # _note_first_skip fired
        census = build_lake_census(report)
        assert census.engine_clean is True
        assert census.files_skipped == 1  # honestly INCOMPLETE, never hidden


class TestEngineMalformedPathAttribution:
    """MAJOR: an engine file whose PATH fails to parse still flips the engine verdict."""

    def test_malformed_engine_path_counts_as_engine_skip(self, tmp_path: Path) -> None:
        # A non-integer year dir under the NIFTY partition → parse_partition_path
        # raises PartitionError BEFORE any read; the coords are un-parsed, yet the
        # file literally lives on an ENGINE series.
        _plant_garbage(_partition(tmp_path, symbol="NIFTY", year="notayear"))
        report = scan_lake_invariants(tmp_path)
        assert report.files_skipped == 1
        assert report.engine_files_skipped == 1  # the POSITIONAL attribution
        census = build_lake_census(report)
        assert census.engine_clean is False
        assert census.engine_files_skipped == 1
        assert "UNREADABLE" in census.engine_detail

    def test_malformed_reference_path_stays_engine_clean(self, tmp_path: Path) -> None:
        # A non-integer year under a CASH/reference partition → parse fails, but it
        # is NOT an engine coord, so the engine verdict is untouched (the FALSE arm
        # of the positional engine gate).
        _plant_garbage(_partition(tmp_path, segment="CASH", symbol="RELIANCE", year="notayear"))
        report = scan_lake_invariants(tmp_path)
        assert report.files_skipped == 1
        assert report.engine_files_skipped == 0
        assert build_lake_census(report).engine_clean is True


class TestEngineCoordFromPath:
    """Direct pins for the positional coord helper's defensive branches."""

    def test_engine_series_recognized(self, tmp_path: Path) -> None:
        p = _partition(tmp_path, symbol="SENSEX", exchange="BSE", segment="INDEX")
        assert _engine_coord_from_path(p, tmp_path) == ("BSE", "INDEX", "SENSEX")

    def test_path_outside_bars_returns_none(self, tmp_path: Path) -> None:
        # relative_to(lake_root/bars) raises ValueError → honest None.
        assert _engine_coord_from_path(tmp_path / "elsewhere" / "x.parquet", tmp_path) is None

    def test_too_shallow_path_returns_none(self, tmp_path: Path) -> None:
        # Under bars/ but fewer than 3 parts → honest None (never an IndexError).
        assert _engine_coord_from_path(tmp_path / "bars" / "NSE", tmp_path) is None
