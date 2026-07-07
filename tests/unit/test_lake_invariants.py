"""Unit coverage for :mod:`brutex.data.lake_invariants` (DEC-LAKE-HEAL-001).

100% line + branch on ``brutex/data/lake_invariants.py`` — the ONE bounded lake
invariant scanner + census. Poison is planted with RAW pyarrow (the fixed
writer refuses to store duplicates, exactly like the operator's lake —
the #1067 ``_poison_partition`` convention); clean data is seeded through the
REAL :class:`~brutex.data.parquet_writer.ParquetWriter` /
:class:`~brutex.data.option_parquet_writer.OptionParquetWriter` stack.

Pinned here: the UTC-month invariant (the 1day midnight-IST straddle and the
session-exempt GIFTNIFTY early-IST bars are NEVER false-flagged), the
one-read-per-file scan bound (spy), the months/coordinate scope bounds, the
#911 drift detection scope (SPOT segments only — FNO groww_symbol dirs never
touched), and every ``lake.invariant.*`` log event's fields.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import polars as pl
from brutex.data import lake_invariants as li
from brutex.data.lake_invariants import (
    ENGINE_INDEX_COORDS,
    ForeignMonthRows,
    IntraFileDup,
    LakeScanReport,
    LegacyKeyDrift,
    OptionIntraFileDup,
    PartitionCoords,
    build_lake_census,
    census_to_dict,
    classify_bar_keys,
    detect_legacy_drift,
    scan_lake_invariants,
)
from brutex.data.models import CandleBar, OptionBar
from brutex.data.option_parquet_writer import (
    OptionParquetWriter,
    OptionSeriesCoordinate,
)
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import build_partition_path

# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _ts(minute: int, *, year: int = 2024, month: int = 6, day: int = 3) -> datetime:
    """A UTC 1-minute instant inside the 2024-06 partition by default."""
    return datetime(year, month, day, 4, minute, tzinfo=UTC)


def _bar(ts: datetime, *, close: str = "100.00", volume: int = 10) -> CandleBar:
    c = Decimal(close)
    return CandleBar(
        timestamp=ts, open=c, high=c, low=c, close=c, volume=volume, open_interest=None
    )


def _write_bars(
    lake: Path,
    bars: Sequence[CandleBar],
    *,
    exchange: str = "NSE",
    segment: str = "INDEX",
    symbol: str = "NIFTY",
    interval: str = "1minute",
) -> None:
    """Seed bars through the REAL writer, one write per UTC month group."""
    writer = ParquetWriter(lake_root=lake)
    groups: dict[tuple[int, int], list[CandleBar]] = {}
    for bar in bars:
        key = (bar.timestamp.year, bar.timestamp.month)
        groups.setdefault(key, []).append(bar)
    for _, group in sorted(groups.items()):
        writer.write(
            exchange=exchange, segment=segment, symbol=symbol, interval=interval, bars=group
        )


def _dest(
    lake: Path,
    *,
    exchange: str = "NSE",
    segment: str = "INDEX",
    symbol: str = "NIFTY",
    interval: str = "1minute",
    year: int = 2024,
    month: int = 6,
) -> Path:
    return build_partition_path(
        lake_root=lake,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
        year=year,
        month=month,
    )


def _plant_raw(dest: Path, timestamps: Sequence[datetime | None], *, price: float = 100.0) -> None:
    """Write a RAW pyarrow bar partition VERBATIM (dups/foreign rows preserved).

    The fixed writer refuses to store duplicates (#1067), so poison must be
    planted with raw pyarrow, exactly like the operator's lake.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    n = len(timestamps)
    prices = [price] * n
    table = pa.table(
        {
            "timestamp": pa.array(list(timestamps), type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([10] * n, type=pa.int64()),
            "open_interest": pa.array([None] * n, type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _plant_option_raw(dest: Path, rows: Sequence[tuple[str, datetime]]) -> None:
    """Write a RAW options-tree file with only the two scanner key columns."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "side": pa.array([side for side, _ in rows], type=pa.string()),
            "timestamp": pa.array([ts for _, ts in rows], type=pa.timestamp("us", tz="UTC")),
        }
    )
    pq.write_table(table, dest)


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    return [e for e in logs if e["event"] == name]


def _series(values: Sequence[datetime | None]) -> pl.Series:
    return pl.Series("timestamp", list(values), dtype=pl.Datetime("us", "UTC"))


_NIFTY_COORDS = PartitionCoords(
    exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", year=2024, month=6
)


# ────────────────────────────────────────────────────────────────────────
# classify_bar_keys (the pure L1 core)
# ────────────────────────────────────────────────────────────────────────


class TestClassifyBarKeys:
    def test_empty_column_is_clean(self) -> None:
        assert classify_bar_keys(_NIFTY_COORDS, _series([])) == (None, None)

    def test_clean_column_is_clean(self) -> None:
        assert classify_bar_keys(_NIFTY_COORDS, _series([_ts(1), _ts(2)])) == (None, None)

    def test_dup_detected_with_relative_path(self) -> None:
        dup, foreign = classify_bar_keys(_NIFTY_COORDS, _series([_ts(1), _ts(1), _ts(2)]))
        assert foreign is None
        assert dup is not None
        assert dup == IntraFileDup(
            path=Path("bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet"),
            coords=_NIFTY_COORDS,
            physical_rows=3,
            unique_keys=2,
        )
        assert dup.dup_rows == 1

    def test_foreign_detected_overlap_unclassified(self) -> None:
        july = _ts(1, month=7)
        dup, foreign = classify_bar_keys(_NIFTY_COORDS, _series([_ts(1), july]))
        assert dup is None
        assert foreign == ForeignMonthRows(
            path=Path("bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet"),
            coords=_NIFTY_COORDS,
            foreign_rows=1,
            overlap_rows=None,
        )

    def test_dup_and_foreign_can_coexist(self) -> None:
        july = _ts(1, month=7)
        dup, foreign = classify_bar_keys(_NIFTY_COORDS, _series([_ts(1), _ts(1), july]))
        assert dup is not None and dup.physical_rows == 3 and dup.unique_keys == 2
        assert foreign is not None and foreign.foreign_rows == 1

    def test_all_null_timestamps_abstain_from_month_routing(self) -> None:
        # Two NULL keys are still duplicate keys (physical > n_unique) but carry
        # no month to route — the foreign classifier honestly abstains.
        dup, foreign = classify_bar_keys(_NIFTY_COORDS, _series([None, None]))
        assert dup is not None and dup.physical_rows == 2 and dup.unique_keys == 1
        assert foreign is None

    def test_naive_timestamp_is_routed_as_utc_never_raises(self) -> None:
        naive = pl.Series("timestamp", [datetime(2024, 7, 1, 4, 0)], dtype=pl.Datetime("us"))
        dup, foreign = classify_bar_keys(_NIFTY_COORDS, naive)
        assert dup is None
        assert foreign is not None and foreign.foreign_rows == 1

    def test_partition_coords_series_and_relpath(self) -> None:
        assert _NIFTY_COORDS.series == ("NSE", "INDEX", "NIFTY", "1minute")
        assert _NIFTY_COORDS.relpath() == "bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet"


# ────────────────────────────────────────────────────────────────────────
# scan: dup + foreign + overlap classification
# ────────────────────────────────────────────────────────────────────────


class TestScanFindings:
    def test_empty_lake_zero_everything(self, tmp_path: Path) -> None:
        report = scan_lake_invariants(tmp_path)
        assert report == LakeScanReport(
            findings=(),
            files_scanned=0,
            files_skipped=0,
            series_scanned=0,
            option_files_scanned=0,
            option_files_skipped=0,
            duration_s=report.duration_s,
            files_by_segment=(),
            engine_files=0,
        )

    def test_clean_lake_zero_findings(self, tmp_path: Path) -> None:
        # Spot INDEX (2 NIFTY months + 1 BANKNIFTY 1day), CASH, FNO, and a REAL
        # options-tree series — a fully clean lake reports zero findings.
        _write_bars(tmp_path, [_bar(_ts(1)), _bar(_ts(2)), _bar(_ts(1, month=7))])
        _write_bars(tmp_path, [_bar(_ts(3))], symbol="BANKNIFTY", interval="1day")
        _write_bars(tmp_path, [_bar(_ts(4))], segment="CASH", symbol="M&M")
        _write_bars(tmp_path, [_bar(_ts(5))], segment="FNO", symbol="NSE-NIFTY-30Jan25-FUT")
        coordinate = OptionSeriesCoordinate(
            exch_id="NSE",
            exchange_segment="NSE_FNO",
            security_id=13,
            underlying_symbol="NIFTY",
            expiry_flag="WEEK",
            expiry_code=1,
            strike_offset="ATM",
        )
        option_bars = [
            OptionBar(
                timestamp=_ts(6),
                open=Decimal("10.00"),
                high=Decimal("10.00"),
                low=Decimal("10.00"),
                close=Decimal("10.00"),
                volume=5,
                open_interest=100,
                side=side,
                strike=Decimal("24000"),
                spot=None,
                iv=None,
            )
            for side in ("CE", "PE")
        ]
        OptionParquetWriter(lake_root=tmp_path).write(
            partition_key="NSE_FNO|13|WEEK|1|ATM", coordinate=coordinate, bars=option_bars
        )
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert report.findings == ()
        assert report.files_scanned == 5
        assert report.files_skipped == 0
        assert report.series_scanned == 4
        assert report.option_files_scanned == 1
        assert report.option_files_skipped == 0
        assert report.files_by_segment == (("CASH", 1), ("FNO", 1), ("INDEX", 3))
        assert report.engine_files == 3  # NIFTY×2 + BANKNIFTY×1
        done = _events(logs, "lake.invariant.scan_complete")
        assert len(done) == 1
        assert done[0]["files"] == 6 and done[0]["findings"] == 0

    def test_intra_file_dup_detected(self, tmp_path: Path) -> None:
        _plant_raw(_dest(tmp_path), [_ts(1), _ts(1), _ts(2), _ts(3)])
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert len(report.findings) == 1
        finding = report.findings[0]
        assert isinstance(finding, IntraFileDup)
        assert finding.path == _dest(tmp_path)
        assert finding.physical_rows == 4
        assert finding.unique_keys == 3
        assert finding.dup_rows == 1
        warns = _events(logs, "lake.invariant.intra_file_dup")
        assert len(warns) == 1
        assert warns[0]["log_level"] == "warning"
        assert warns[0]["partition"] == "bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet"
        assert warns[0]["physical_rows"] == 4
        assert warns[0]["unique_timestamps"] == 3

    def test_foreign_month_rows_detected_pure_misplacement(self, tmp_path: Path) -> None:
        # A June bar planted in the JULY file; no June file exists at all —
        # pure misplacement classifies overlap_rows=0.
        july_dest = _dest(tmp_path, month=7)
        _plant_raw(july_dest, [_ts(1), _ts(2, month=7, day=1), _ts(3, month=7, day=1)])
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert len(report.findings) == 1
        finding = report.findings[0]
        assert isinstance(finding, ForeignMonthRows)
        assert finding.path == july_dest
        assert finding.foreign_rows == 1
        assert finding.overlap_rows == 0
        warns = _events(logs, "lake.invariant.foreign_month_rows")
        assert len(warns) == 1
        assert warns[0]["partition"] == "bars/NSE/INDEX/NIFTY/1minute/2024/07.parquet"
        assert warns[0]["foreign_rows"] == 1
        assert warns[0]["overlap_rows"] == 0

    def test_foreign_month_overlap_classified(self, tmp_path: Path) -> None:
        # The SAME June timestamp lives in BOTH the June file and the July file —
        # the cross-file-overlap sub-case reads overlap_rows=1.
        _write_bars(tmp_path, [_bar(_ts(1)), _bar(_ts(2))])
        _plant_raw(_dest(tmp_path, month=7), [_ts(1), _ts(3, month=7, day=1)])
        report = scan_lake_invariants(tmp_path)
        assert len(report.findings) == 1
        finding = report.findings[0]
        assert isinstance(finding, ForeignMonthRows)
        assert finding.foreign_rows == 1
        assert finding.overlap_rows == 1

    def test_overlap_canonical_read_is_cached_per_series(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # July AND August both carry the same June foreign key — the canonical
        # June sibling is read ONCE (per-series cache), not once per finding.
        _write_bars(tmp_path, [_bar(_ts(1))])
        _plant_raw(_dest(tmp_path, month=7), [_ts(1), _ts(2, month=7, day=1)])
        _plant_raw(_dest(tmp_path, month=8), [_ts(1), _ts(2, month=8, day=1)])
        calls: list[Path] = []
        real = li._read_key_columns

        def spy(path: Path, columns: Sequence[str]) -> pl.DataFrame:
            calls.append(path)
            return real(path, columns)

        monkeypatch.setattr(li, "_read_key_columns", spy)
        report = scan_lake_invariants(tmp_path)
        foreigns = [f for f in report.findings if isinstance(f, ForeignMonthRows)]
        assert [f.overlap_rows for f in foreigns] == [1, 1]
        # 3 in-scope scans + exactly ONE targeted canonical re-read of June.
        assert len(calls) == 4
        assert calls.count(_dest(tmp_path)) == 2  # scanned once + canonical once

    def test_overlap_unreadable_canonical_abstains(self, tmp_path: Path) -> None:
        # The canonical June file is garbage: the July finding stays honestly
        # UNCLASSIFIED (overlap_rows=None) instead of guessing.
        june = _dest(tmp_path)
        june.parent.mkdir(parents=True, exist_ok=True)
        june.write_bytes(b"not a parquet file at all")
        _plant_raw(_dest(tmp_path, month=7), [_ts(1), _ts(2, month=7, day=1)])
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert report.files_skipped == 1  # the garbage June file itself
        foreigns = [f for f in report.findings if isinstance(f, ForeignMonthRows)]
        assert len(foreigns) == 1
        assert foreigns[0].overlap_rows is None
        # Skipped once as a scanned file + once as the canonical targeted read.
        assert len(_events(logs, "lake.invariant.scan_skip")) == 2

    def test_out_of_range_foreign_year_reads_as_misplacement(self, tmp_path: Path) -> None:
        # A pre-2000 timestamp routes to a partition that cannot exist
        # (PartitionError) — classified as pure misplacement, never a crash.
        ancient = datetime(1999, 6, 3, 4, 1, tzinfo=UTC)
        _plant_raw(_dest(tmp_path), [ancient, _ts(2)])
        report = scan_lake_invariants(tmp_path)
        foreigns = [f for f in report.findings if isinstance(f, ForeignMonthRows)]
        assert len(foreigns) == 1
        assert foreigns[0].overlap_rows == 0

    def test_null_plus_foreign_rows_classify_together(self, tmp_path: Path) -> None:
        # NULL keys (dup) + a real June key in the July file (foreign) coexist.
        _plant_raw(_dest(tmp_path, month=7), [None, None, _ts(1)])
        report = scan_lake_invariants(tmp_path)
        assert len(report.findings) == 2
        dup, foreign = report.findings
        assert isinstance(dup, IntraFileDup) and dup.physical_rows == 3
        assert isinstance(foreign, ForeignMonthRows)
        assert foreign.foreign_rows == 1
        assert foreign.overlap_rows == 0

    def test_zero_row_file_is_clean(self, tmp_path: Path) -> None:
        dest = _dest(tmp_path)
        _plant_raw(dest, [])  # empty CANONICAL 7-column schema (the real writer shape)
        report = scan_lake_invariants(tmp_path)
        assert report.findings == ()
        assert report.files_scanned == 1

    def test_missing_price_columns_file_is_skipped(self, tmp_path: Path) -> None:
        """A bar file WITHOUT the OHLC columns is honestly SKIPPED (not dup-scanned).

        DEC-STORED-DATA-VALIDATION-001 behavior change, documented in the
        module docstring: the scan now reads BAR_SCAN_COLUMNS (timestamp +
        OHLC) in ONE pass; a file violating the canonical 7-column schema
        cannot satisfy the read and is counted ``files_skipped`` with the
        ``lake.invariant.scan_skip`` WARNING — never silently ignored.
        """
        dest = _dest(tmp_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"timestamp": _series([_ts(1)])}).write_parquet(dest)
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert report.files_scanned == 0
        assert report.files_skipped == 1
        assert len(_events(logs, "lake.invariant.scan_skip")) == 1

    def test_utc_month_rule_no_false_flag_1day(self, tmp_path: Path) -> None:
        # A derived 1day bar is stamped midnight IST = 18:30 UTC of the PREVIOUS
        # day: the July-1-IST daily bar lives in the JUNE UTC month file. The
        # UTC-month rule must NOT flag it (comparing IST months would).
        straddle = datetime(2024, 6, 30, 18, 30, tzinfo=UTC)  # 2024-07-01 IST
        _write_bars(tmp_path, [_bar(straddle)], interval="1day")
        assert _dest(tmp_path, interval="1day", month=6).exists()
        report = scan_lake_invariants(tmp_path)
        assert report.findings == ()

    def test_utc_month_rule_no_false_flag_giftnifty(self, tmp_path: Path) -> None:
        # Session-exempt GIFTNIFTY trades early-IST: 03:00 IST July 1 = 21:30 UTC
        # June 30 — lands in the June UTC file, consistent with the UTC rule.
        early = datetime(2024, 6, 30, 21, 30, tzinfo=UTC)  # 2024-07-01 03:00 IST
        _write_bars(tmp_path, [_bar(early)], segment="CASH", symbol="GIFTNIFTY")
        report = scan_lake_invariants(tmp_path)
        assert report.findings == ()

    def test_unreadable_file_skipped_counted(self, tmp_path: Path) -> None:
        _write_bars(tmp_path, [_bar(_ts(1))])
        garbage = _dest(tmp_path, symbol="BANKNIFTY")
        garbage.parent.mkdir(parents=True, exist_ok=True)
        garbage.write_bytes(b"not a parquet file at all")
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert report.files_scanned == 1
        assert report.files_skipped == 1
        assert report.series_scanned == 1  # the garbage-only series is not counted
        skips = _events(logs, "lake.invariant.scan_skip")
        assert len(skips) == 1
        assert "BANKNIFTY" in str(skips[0]["path"])
        assert skips[0]["log_level"] == "warning"

    def test_unparseable_partition_path_skipped(self, tmp_path: Path) -> None:
        bad = tmp_path / "bars" / "ZZ" / "INDEX" / "NIFTY" / "1minute" / "2024" / "06.parquet"
        _plant_raw(bad, [_ts(1)])
        report = scan_lake_invariants(tmp_path)
        assert report.files_scanned == 0
        assert report.files_skipped == 1


# ────────────────────────────────────────────────────────────────────────
# scan: scope bounds + read/pause discipline
# ────────────────────────────────────────────────────────────────────────


class TestScanBounds:
    def test_months_filter_bounds_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_bars(tmp_path, [_bar(_ts(1)), _bar(_ts(1, month=7))])
        calls: list[Path] = []
        real = li._read_key_columns

        def spy(path: Path, columns: Sequence[str]) -> pl.DataFrame:
            calls.append(path)
            return real(path, columns)

        monkeypatch.setattr(li, "_read_key_columns", spy)
        report = scan_lake_invariants(tmp_path, months={(2024, 6)})
        assert report.files_scanned == 1
        assert report.files_skipped == 0
        assert calls == [_dest(tmp_path)]  # the July file was never read

    def test_coordinate_filters(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_bars(tmp_path, [_bar(_ts(1))])
        _write_bars(tmp_path, [_bar(_ts(2))], symbol="BANKNIFTY")
        calls: list[Path] = []
        real = li._read_key_columns

        def spy(path: Path, columns: Sequence[str]) -> pl.DataFrame:
            calls.append(path)
            return real(path, columns)

        monkeypatch.setattr(li, "_read_key_columns", spy)
        report = scan_lake_invariants(
            tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute"
        )
        assert report.files_scanned == 1
        assert calls == [_dest(tmp_path)]

    def test_single_read_per_file_spy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Clean lake: exactly ONE key-column read per file — the scan bound.
        _write_bars(tmp_path, [_bar(_ts(1)), _bar(_ts(1, month=7))])
        _write_bars(tmp_path, [_bar(_ts(2))], symbol="BANKNIFTY")
        calls: list[Path] = []
        real = li._read_key_columns

        def spy(path: Path, columns: Sequence[str]) -> pl.DataFrame:
            calls.append(path)
            return real(path, columns)

        monkeypatch.setattr(li, "_read_key_columns", spy)
        report = scan_lake_invariants(tmp_path)
        assert report.files_scanned == 3
        assert len(calls) == 3
        assert len(set(calls)) == 3

    def test_per_file_pause_called_per_file(self, tmp_path: Path) -> None:
        _write_bars(tmp_path, [_bar(_ts(1)), _bar(_ts(1, month=7))])
        _plant_option_raw(
            tmp_path / "options" / "NSE_FNO__13__WEEK__1__ATM" / "2024-06.parquet",
            [("CE", _ts(1)), ("PE", _ts(1))],
        )
        ticks = {"n": 0}

        def pause() -> None:
            ticks["n"] += 1

        report = scan_lake_invariants(tmp_path, per_file_pause=pause)
        assert ticks["n"] == report.files_scanned + report.option_files_scanned == 3

    def test_include_options_false_skips_options_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opt = tmp_path / "options" / "SERIES" / "2024-06.parquet"
        _plant_option_raw(opt, [("CE", _ts(1)), ("CE", _ts(1))])
        calls: list[Path] = []
        real = li._read_key_columns

        def spy(path: Path, columns: Sequence[str]) -> pl.DataFrame:
            calls.append(path)
            return real(path, columns)

        monkeypatch.setattr(li, "_read_key_columns", spy)
        report = scan_lake_invariants(tmp_path, include_options=False)
        assert report.findings == ()
        assert report.option_files_scanned == 0
        assert calls == []


# ────────────────────────────────────────────────────────────────────────
# Options tree
# ────────────────────────────────────────────────────────────────────────


class TestOptionsTree:
    def test_option_tree_dup_detected(self, tmp_path: Path) -> None:
        # Duplicate (side, timestamp) key → flagged; the SAME timestamp across
        # CE and PE sides is NOT a dup (the option writer's own merge key).
        dest = tmp_path / "options" / "NSE_FNO__13__WEEK__1__ATM" / "2024-06.parquet"
        _plant_option_raw(dest, [("CE", _ts(1)), ("CE", _ts(1)), ("PE", _ts(1))])
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert len(report.findings) == 1
        finding = report.findings[0]
        assert isinstance(finding, OptionIntraFileDup)
        assert finding.path == dest
        assert finding.series == "NSE_FNO__13__WEEK__1__ATM"
        assert finding.physical_rows == 3
        assert finding.unique_keys == 2
        warns = _events(logs, "lake.invariant.option_intra_file_dup")
        assert len(warns) == 1
        assert warns[0]["partition"] == ("options/NSE_FNO__13__WEEK__1__ATM/2024-06.parquet")
        assert warns[0]["physical_rows"] == 3
        assert warns[0]["unique_keys"] == 2

    def test_cross_side_same_timestamp_is_clean(self, tmp_path: Path) -> None:
        dest = tmp_path / "options" / "SERIES" / "2024-06.parquet"
        _plant_option_raw(dest, [("CE", _ts(1)), ("PE", _ts(1))])
        report = scan_lake_invariants(tmp_path)
        assert report.findings == ()
        assert report.option_files_scanned == 1

    def test_unreadable_option_file_skipped(self, tmp_path: Path) -> None:
        dest = tmp_path / "options" / "SERIES" / "2024-06.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"garbage")
        # A stray FILE directly under options/ and an EMPTY series dir are both
        # ignored (dir-with-parquet only), never crash the walk.
        (tmp_path / "options" / "README.txt").write_text("stray")
        (tmp_path / "options" / "EMPTY").mkdir()
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert report.option_files_scanned == 0
        assert report.option_files_skipped == 1
        assert len(_events(logs, "lake.invariant.scan_skip")) == 1


# ────────────────────────────────────────────────────────────────────────
# Legacy-key drift
# ────────────────────────────────────────────────────────────────────────


class TestLegacyDrift:
    def test_legacy_drift_detected_sibling_dir(self, tmp_path: Path) -> None:
        _write_bars(tmp_path, [_bar(_ts(1))], segment="CASH", symbol="M&M")
        _write_bars(tmp_path, [_bar(_ts(2))], segment="CASH", symbol="NSE-M&M")
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        drifts = [f for f in report.findings if isinstance(f, LegacyKeyDrift)]
        assert len(drifts) == 1
        drift = drifts[0]
        assert drift.exchange == "NSE"
        assert drift.segment == "CASH"
        assert drift.drifted_symbol == "NSE-M&M"
        assert drift.canonical_symbol == "M&M"
        assert drift.drifted_dir == tmp_path / "bars" / "NSE" / "CASH" / "NSE-M&M"
        assert drift.intervals == ("1minute",)
        assert drift.files == 1
        warns = _events(logs, "lake.invariant.legacy_key_drift")
        assert len(warns) == 1
        assert warns[0]["drifted"] == "'NSE-M&M'"
        assert warns[0]["canonical"] == "'M&M'"
        assert warns[0]["exchange"] == "NSE"
        assert warns[0]["segment"] == "CASH"
        assert warns[0]["files"] == 1

    def test_legacy_drift_whitespace_class(self, tmp_path: Path) -> None:
        _write_bars(tmp_path, [_bar(_ts(1))], segment="CASH", symbol="M&M")
        _write_bars(tmp_path, [_bar(_ts(2))], segment="CASH", symbol="M&M ")
        report = scan_lake_invariants(tmp_path)
        drifts = [f for f in report.findings if isinstance(f, LegacyKeyDrift)]
        assert len(drifts) == 1
        assert drifts[0].drifted_symbol == "M&M "
        assert drifts[0].canonical_symbol == "M&M"

    def test_legacy_drift_canonical_keys(self, tmp_path: Path) -> None:
        # No canonical sibling dir exists — the instrument-master key universe
        # still resolves the drift; WITHOUT it the stray honestly stays unflagged.
        _write_bars(tmp_path, [_bar(_ts(1))], segment="CASH", symbol="NSE-M&M")
        unaided = scan_lake_invariants(tmp_path)
        assert unaided.findings == ()
        aided = scan_lake_invariants(tmp_path, canonical_keys={"M&M"})
        drifts = [f for f in aided.findings if isinstance(f, LegacyKeyDrift)]
        assert len(drifts) == 1
        assert drifts[0].canonical_symbol == "M&M"

    def test_fno_segment_never_flagged(self, tmp_path: Path) -> None:
        # FNO dirs are groww_symbol-keyed and legitimately "<exch>-"-prefixed —
        # the prefix-strip ladder must never touch them.
        _write_bars(tmp_path, [_bar(_ts(1))], segment="FNO", symbol="FOO")
        _write_bars(tmp_path, [_bar(_ts(2))], segment="FNO", symbol="NSE-FOO")
        report = scan_lake_invariants(tmp_path)
        assert report.findings == ()

    def test_interior_space_symbol_not_flagged(self, tmp_path: Path) -> None:
        _write_bars(tmp_path, [_bar(_ts(1))], segment="CASH", symbol="INDIA VIX")
        _write_bars(tmp_path, [_bar(_ts(2))], segment="CASH", symbol="M&M")
        report = scan_lake_invariants(tmp_path)
        assert report.findings == ()

    def test_detect_legacy_drift_missing_dir_profile(self, tmp_path: Path) -> None:
        # Direct unit call: a drifted name whose dir vanished profiles as empty.
        findings = detect_legacy_drift(
            {("NSE", "CASH"): ["NSE-M&M"]},
            lake_root=tmp_path,
            canonical_keys=["M&M"],
        )
        assert len(findings) == 1
        assert findings[0].intervals == ()
        assert findings[0].files == 0


# ────────────────────────────────────────────────────────────────────────
# Census + census_to_dict
# ────────────────────────────────────────────────────────────────────────


class TestCensus:
    def test_census_fold_and_engine_verdict_clean(self, tmp_path: Path) -> None:
        _write_bars(tmp_path, [_bar(_ts(1))])
        _write_bars(tmp_path, [_bar(_ts(2))], segment="CASH", symbol="M&M")
        report = scan_lake_invariants(tmp_path)
        census = build_lake_census(report)
        assert census.engine_clean is True
        assert census.engine_detail == "NIFTY/BANKNIFTY/SENSEX: 0 findings over 1 files"
        assert census.findings_total == 0
        assert census.files_scanned == 2
        by_segment = {row.segment: row for row in census.segments}
        assert set(by_segment) == {"INDEX", "CASH", "FNO", "OPTIONS"}
        assert by_segment["INDEX"].files == 1 and by_segment["INDEX"].clean == 1
        assert by_segment["CASH"].files == 1 and by_segment["CASH"].clean == 1
        assert by_segment["FNO"].files == 0
        assert by_segment["OPTIONS"].files == 0

    def test_census_engine_poison_flips_verdict(self, tmp_path: Path) -> None:
        # Poison on ("NSE","INDEX","NIFTY") — the ENGINE series — is called out.
        _plant_raw(_dest(tmp_path), [_ts(1), _ts(1)])
        _write_bars(tmp_path, [_bar(_ts(2))], segment="CASH", symbol="M&M")
        report = scan_lake_invariants(tmp_path)
        census = build_lake_census(report)
        assert census.engine_clean is False
        assert "NIFTY/BANKNIFTY/SENSEX: 1 findings" in census.engine_detail
        assert "dup bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet" in census.engine_detail
        by_segment = {row.segment: row for row in census.segments}
        assert by_segment["INDEX"].dup_files == 1
        assert by_segment["INDEX"].clean == 0
        assert by_segment["CASH"].clean == 1

    def test_census_counts_dup_foreign_overlap_drift(self, tmp_path: Path) -> None:
        # One CASH file with BOTH dup + overlapping-foreign rows is ONE poisoned
        # file; the drift dir is a DIR-level count on top.
        _write_bars(tmp_path, [_bar(_ts(1))], segment="CASH", symbol="M&M")
        _plant_raw(
            _dest(tmp_path, segment="CASH", symbol="M&M", month=7),
            [_ts(1), _ts(2, month=7, day=1), _ts(2, month=7, day=1)],
        )
        _write_bars(tmp_path, [_bar(_ts(3))], segment="CASH", symbol="NSE-M&M")
        # A second symbol with a PURE misplacement (overlap 0) — counted foreign
        # but NOT overlap.
        _plant_raw(
            _dest(tmp_path, segment="CASH", symbol="FOO", month=7),
            [_ts(4), _ts(5, month=7, day=1)],
        )
        report = scan_lake_invariants(tmp_path)
        census = build_lake_census(report)
        by_segment = {row.segment: row for row in census.segments}
        cash = by_segment["CASH"]
        assert cash.files == 4
        assert cash.dup_files == 1
        assert cash.foreign_files == 2
        assert cash.overlap_files == 1
        assert cash.clean == 2  # dup+foreign hit the SAME M&M July file; FOO July poisoned
        assert cash.drift_dirs == 1
        assert census.findings_total == 4
        assert census.engine_clean is True

    def test_census_option_dup_row(self, tmp_path: Path) -> None:
        _plant_option_raw(
            tmp_path / "options" / "SERIES" / "2024-06.parquet",
            [("CE", _ts(1)), ("CE", _ts(1))],
        )
        _plant_option_raw(
            tmp_path / "options" / "SERIES" / "2024-07.parquet",
            [("PE", _ts(1, month=7))],
        )
        census = build_lake_census(scan_lake_invariants(tmp_path))
        by_segment = {row.segment: row for row in census.segments}
        options = by_segment["OPTIONS"]
        assert options.files == 2
        assert options.dup_files == 1
        assert options.clean == 1
        assert census.engine_clean is True  # options never touch the engine verdict

    def test_census_engine_drift_and_offender_cap(self, tmp_path: Path) -> None:
        # 6 poisoned NIFTY months + a drifted NSE-NIFTY dir: the detail names at
        # most 5 offenders then "(+N more)"; the drift token uses repr arrows.
        for month in range(1, 7):
            _plant_raw(
                _dest(tmp_path, month=month),
                [_ts(1, month=month), _ts(1, month=month)],
            )
        _write_bars(tmp_path, [_bar(_ts(2, month=7))], symbol="NSE-NIFTY")
        report = scan_lake_invariants(tmp_path)
        census = build_lake_census(report)
        assert census.engine_clean is False
        assert "7 findings" in census.engine_detail
        assert "(+2 more)" in census.engine_detail
        drifts = [f for f in report.findings if isinstance(f, LegacyKeyDrift)]
        assert len(drifts) == 1
        assert li._engine_offender(drifts[0]) == "drift 'NSE-NIFTY'->'NIFTY'"

    def test_engine_offender_tokens(self, tmp_path: Path) -> None:
        coords = _NIFTY_COORDS
        dup = IntraFileDup(path=Path("x"), coords=coords, physical_rows=2, unique_keys=1)
        foreign = ForeignMonthRows(path=Path("x"), coords=coords, foreign_rows=1, overlap_rows=None)
        option = OptionIntraFileDup(
            path=tmp_path / "options" / "S" / "2024-06.parquet",
            series="S",
            physical_rows=2,
            unique_keys=1,
        )
        assert li._engine_offender(dup).startswith("dup bars/")
        assert li._engine_offender(foreign).startswith("foreign bars/")
        # An options finding is never engine-relevant, but the token is total.
        assert li._engine_offender(option) == "option-dup options/S/2024-06.parquet"
        assert li._engine_relevant(option) is False

    def test_engine_coords_constant(self) -> None:
        assert ENGINE_INDEX_COORDS == (
            ("NSE", "INDEX", "NIFTY"),
            ("NSE", "INDEX", "BANKNIFTY"),
            ("BSE", "INDEX", "SENSEX"),
        )


class TestCensusToDict:
    def test_shape_with_no_stats(self, tmp_path: Path) -> None:
        census = build_lake_census(scan_lake_invariants(tmp_path))
        payload = census_to_dict(census, mode="auto")
        assert payload["present"] is True
        assert payload["mode"] == "auto"
        assert payload["engine_clean"] is True
        assert payload["findings_total"] == 0
        assert payload["files_scanned"] == 0
        assert payload["healed_total"] == 0
        assert payload["quarantined_files_total"] == 0
        assert payload["last_heal_utc"] is None
        assert payload["last_sweep_utc"] is None
        segments = payload["segments"]
        assert [row["segment"] for row in segments] == ["INDEX", "CASH", "FNO", "OPTIONS"]
        assert set(segments[0]) == {
            "segment",
            "files",
            "clean",
            "dup_files",
            "foreign_files",
            "overlap_files",
            "drift_dirs",
            "price_files",
        }

    def test_stats_and_sweep_folded(self, tmp_path: Path) -> None:
        census = build_lake_census(scan_lake_invariants(tmp_path))
        payload = census_to_dict(
            census,
            mode="observe",
            stats={
                "healed_partitions_total": 7,
                "quarantined_files_total": 9,
                "last_heal_utc": "2026-07-03T04:11:02Z",
            },
            last_sweep={"completed_utc": "2026-07-03T10:00:00Z"},
        )
        assert payload["mode"] == "observe"
        assert payload["healed_total"] == 7
        assert payload["quarantined_files_total"] == 9
        assert payload["last_heal_utc"] == "2026-07-03T04:11:02Z"
        assert payload["last_sweep_utc"] == "2026-07-03T10:00:00Z"

    def test_foreign_shaped_stats_degrade(self, tmp_path: Path) -> None:
        census = build_lake_census(scan_lake_invariants(tmp_path))
        payload = census_to_dict(
            census,
            mode="auto",
            stats={"healed_partitions_total": "7", "last_heal_utc": 4},
            last_sweep={"completed_utc": 5},
        )
        assert payload["healed_total"] == 0
        assert payload["quarantined_files_total"] == 0  # key absent
        assert payload["last_heal_utc"] is None
        assert payload["last_sweep_utc"] is None


# ────────────────────────────────────────────────────────────────────────
# Price-sanity findings (DEC-STORED-DATA-VALIDATION-001)
# ────────────────────────────────────────────────────────────────────────


def _plant_raw_ohlc(
    dest: Path, rows: Sequence[tuple[datetime, float, float, float, float]]
) -> None:
    """Write a RAW bar partition with explicit per-row OHLC (poison-capable)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "timestamp": pa.array([r[0] for r in rows], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array([r[1] for r in rows], type=pa.float64()),
            "high": pa.array([r[2] for r in rows], type=pa.float64()),
            "low": pa.array([r[3] for r in rows], type=pa.float64()),
            "close": pa.array([r[4] for r in rows], type=pa.float64()),
            "volume": pa.array([10] * len(rows), type=pa.int64()),
            "open_interest": pa.array([None] * len(rows), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


class TestPriceSanityFindings:
    """The scanner is no longer BLIND to price corruption (fails-before: the
    round-2 audit found the census read NO price column, so its "0 findings"
    verdict could not see a stored negative / high<low bar)."""

    def test_price_corrupt_file_yields_finding_and_warn(self, tmp_path: Path) -> None:
        _plant_raw_ohlc(
            _dest(tmp_path),
            [
                (_ts(1), 100.0, 101.0, 99.0, 100.5),  # clean
                (_ts(2), -5.0, -1.0, -10.0, -2.0),  # negative but ordered (L1-a shape)
                (_ts(3), 100.0, 1.0, 50.0, 60.0),  # high < low (L1-b shape)
            ],
        )
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        price_findings = [f for f in report.findings if isinstance(f, li.PriceSanityRows)]
        assert len(price_findings) == 1
        assert price_findings[0].bad_rows == 2
        assert price_findings[0].coords == _NIFTY_COORDS
        events = _events(logs, "lake.invariant.price_sanity_rows")
        assert len(events) == 1
        assert events[0]["bad_rows"] == 2
        assert events[0]["partition"] == _NIFTY_COORDS.relpath()

    def test_clean_file_yields_no_price_finding(self, tmp_path: Path) -> None:
        _plant_raw_ohlc(_dest(tmp_path), [(_ts(1), 100.0, 101.0, 99.0, 100.5)])
        report = scan_lake_invariants(tmp_path)
        assert report.findings == ()

    def test_census_counts_price_files_and_flips_engine_verdict(self, tmp_path: Path) -> None:
        # NIFTY is an ENGINE_INDEX_COORDS series — a price finding on it must
        # flip engine_clean (the census verdict is no longer blind).
        _plant_raw_ohlc(_dest(tmp_path), [(_ts(1), -5.0, -1.0, -10.0, -2.0)])
        census = build_lake_census(scan_lake_invariants(tmp_path))
        index_row = next(row for row in census.segments if row.segment == "INDEX")
        assert index_row.price_files == 1
        assert index_row.clean == 0  # a price-corrupt file is NOT clean
        assert census.engine_clean is False
        assert "price bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet" in census.engine_detail
        payload = census_to_dict(census, mode="auto")
        assert payload["segments"][0]["price_files"] == 1

    def test_price_finding_coexists_with_dup_finding_one_poisoned_file(
        self, tmp_path: Path
    ) -> None:
        # A file carrying BOTH a duplicate timestamp AND a bad price is ONE
        # poisoned file in the census `clean` arithmetic (set union).
        _plant_raw_ohlc(
            _dest(tmp_path),
            [
                (_ts(1), 100.0, 101.0, 99.0, 100.5),
                (_ts(1), 100.0, 101.0, 99.0, 100.5),  # dup ts
                (_ts(2), 0.0, 0.0, 0.0, 0.0),  # zero prices
            ],
        )
        census = build_lake_census(scan_lake_invariants(tmp_path))
        index_row = next(row for row in census.segments if row.segment == "INDEX")
        assert index_row.files == 1
        assert index_row.dup_files == 1
        assert index_row.price_files == 1
        assert index_row.clean == 0  # one file, poisoned once — never negative

    def test_engine_offender_token_for_price_finding(self) -> None:
        finding = li.PriceSanityRows(path=Path("x"), coords=_NIFTY_COORDS, bad_rows=3)
        assert li._engine_offender(finding) == f"price {_NIFTY_COORDS.relpath()}"
        assert li._engine_relevant(finding) is True

    def test_non_engine_price_finding_not_engine_relevant(self) -> None:
        coords = PartitionCoords(
            exchange="NSE",
            segment="CASH",
            symbol="RELIANCE",
            interval="1minute",
            year=2024,
            month=6,
        )
        assert (
            li._engine_relevant(li.PriceSanityRows(path=Path("x"), coords=coords, bad_rows=1))
            is False
        )
