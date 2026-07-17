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
    OptionForeignMonthRows,
    OptionIntraFileDup,
    PartitionCoords,
    build_lake_census,
    census_to_dict,
    classify_bar_keys,
    classify_option_keys,
    detect_legacy_drift,
    option_key_dtype_error,
    option_unclassifiable_message,
    scan_lake_invariants,
    utc_aware,
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


def _plant_option_raw(dest: Path, rows: Sequence[tuple[str | None, datetime | None]]) -> None:
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


def _plant_option_nested_side(
    dest: Path, timestamps: Sequence[datetime], *, side_kind: str = "list"
) -> None:
    """A RAW options file whose ``side`` column carries a NESTED dtype.

    ``side_kind="list"`` writes ``List(Utf8)`` (values read back as Python
    lists), ``"struct"`` writes ``Struct`` (dicts) — both UNHASHABLE, the
    round-12 crash class the dtype gate exists for.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    n = len(timestamps)
    if side_kind == "list":
        side = pa.array([["CE"]] * n, type=pa.list_(pa.string()))
    else:
        side = pa.array([{"v": "CE"}] * n, type=pa.struct([("v", pa.string())]))
    table = pa.table(
        {
            "side": side,
            "timestamp": pa.array(list(timestamps), type=pa.timestamp("us", tz="UTC")),
        }
    )
    pq.write_table(table, dest)


def _plant_option_out_of_range(dest: Path) -> None:
    """A RAW options file whose SOUND ``Datetime`` column holds an i64 value
    no Python ``datetime`` can represent (``2**62`` µs ≈ year 146,000).

    The round-18 crash class: the dtype gate alone passed it, then
    ``min()``/``max()`` raised ``OverflowError`` and ``to_list()`` panicked as
    the BaseException ``pyo3_runtime.PanicException``.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "side": pa.array(["CE"], type=pa.string()),
            "timestamp": pa.array([2**62], type=pa.int64()).cast(pa.timestamp("us", tz="UTC")),
        }
    )
    pq.write_table(table, dest)


def _plant_bars_out_of_range(dest: Path) -> None:
    """The bars twin of :func:`_plant_option_out_of_range` (full 7-column schema)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "timestamp": pa.array([2**62], type=pa.int64()).cast(pa.timestamp("us", tz="UTC")),
            "open": pa.array([100.0], type=pa.float64()),
            "high": pa.array([100.0], type=pa.float64()),
            "low": pa.array([100.0], type=pa.float64()),
            "close": pa.array([100.0], type=pa.float64()),
            "volume": pa.array([10], type=pa.int64()),
            "open_interest": pa.array([None], type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _option_key_frame(rows: Sequence[tuple[str | None, datetime | None]]) -> pl.DataFrame:
    """A 2-column ``(side, timestamp)`` key frame for the pure classifier."""
    return pl.DataFrame(
        {
            "side": pl.Series("side", [side for side, _ in rows], dtype=pl.Utf8),
            "timestamp": pl.Series(
                "timestamp", [ts for _, ts in rows], dtype=pl.Datetime("us", "UTC")
            ),
        }
    )


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

    def test_missing_price_columns_file_emits_alien_finding(self, tmp_path: Path) -> None:
        """A bar file WITHOUT the OHLC columns is SKIPPED and now carries a FINDING.

        DEC-ALIEN-SCHEMA-READ-HEAL-001 behavior change (supersedes the
        DEC-STORED-DATA-VALIDATION-001 bare-WARN pin this test previously
        carried): a file violating the canonical 7-column schema cannot
        satisfy the BAR_SCAN_COLUMNS read — it is still counted
        ``files_skipped``, but the skip now emits a real
        :class:`SchemaAlienFile` finding (the healer quarantines it) with the
        ``lake.invariant.schema_alien_file`` WARNING instead of the
        finding-less ``scan_skip`` that left the schema-poisoned file in
        place forever while the dashboard read side skipped it per poll.
        """
        from brutex.data.lake_invariants import SchemaAlienFile

        dest = _dest(tmp_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"timestamp": _series([_ts(1)])}).write_parquet(dest)
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert report.files_scanned == 0
        assert report.files_skipped == 1
        aliens = [f for f in report.findings if isinstance(f, SchemaAlienFile)]
        assert len(aliens) == 1
        assert aliens[0].path == dest
        assert aliens[0].coords is not None
        assert "missing column(s)" in aliens[0].reason
        assert len(_events(logs, "lake.invariant.schema_alien_file")) == 1
        assert not _events(logs, "lake.invariant.scan_skip")

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

    def test_per_file_pause_ticks_targeted_canonical_reads(self, tmp_path: Path) -> None:
        """Round-5 MINOR (refuter-confirmed surviving mutants): BOTH
        ``_canonical_option_keys`` pause arms were unpinned — deleting either
        passed every covering suite, silently unthrottling the L3 boot
        sweep's targeted canonical-sibling reads (the exact seam
        *per_file_pause* exists for; the scan docstring promises a tick
        after EVERY read including the targeted canonical one). Fixture:
        a bars July file with a June foreign ts (1 targeted bars canonical
        read — the pre-existing bars twin ``_canonical_timestamps`` rides
        the same pin) + an options July file with a June foreign key
        (1 targeted option canonical read)."""
        _write_bars(tmp_path, [_bar(_ts(1))])
        _plant_raw(_dest(tmp_path, month=7), [_ts(2, month=7, day=1), _ts(3)])
        _plant_option_raw(_opt_dest(tmp_path, "2024-06"), [("CE", _ts(1))])
        _plant_option_raw(
            _opt_dest(tmp_path, "2024-07"), [("CE", _ts(1)), ("PE", _ts(2, month=7, day=1))]
        )
        ticks = {"n": 0}

        def pause() -> None:
            ticks["n"] += 1

        report = scan_lake_invariants(tmp_path, per_file_pause=pause)
        assert report.files_scanned == 2
        assert report.option_files_scanned == 2
        foreigns = {
            type(f).__name__
            for f in report.findings
            if isinstance(f, ForeignMonthRows | OptionForeignMonthRows)
        }
        assert foreigns == {"ForeignMonthRows", "OptionForeignMonthRows"}
        # 4 scanned files + 1 targeted bars canonical + 1 targeted option
        # canonical = 6 ticks (a deleted success-arm pause reads 5 — killed).
        assert ticks["n"] == report.files_scanned + report.option_files_scanned + 2 == 6

    def test_per_file_pause_ticks_unreadable_canonical_read(self, tmp_path: Path) -> None:
        """The error-arm twin (round-5 MINOR): a garbage canonical option
        sibling still ticks the pause after its FAILED targeted read — the
        docstring's "including a failed/skipped read" promise."""
        june = _opt_dest(tmp_path, "2024-06")
        june.parent.mkdir(parents=True, exist_ok=True)
        june.write_bytes(b"not a parquet file at all")
        _plant_option_raw(
            _opt_dest(tmp_path, "2024-07"), [("CE", _ts(1)), ("PE", _ts(2, month=7, day=1))]
        )
        ticks = {"n": 0}

        def pause() -> None:
            ticks["n"] += 1

        report = scan_lake_invariants(tmp_path, per_file_pause=pause)
        assert report.option_files_scanned == 1
        assert report.option_files_skipped == 1
        foreigns = [f for f in report.findings if isinstance(f, OptionForeignMonthRows)]
        assert foreigns[0].overlap_rows is None  # honest abstain
        # June garbage scan attempt + July scan + the FAILED targeted June
        # canonical read = 3 ticks (a deleted error-arm pause reads 2 — killed).
        assert ticks["n"] == 3

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
# The per-series streaming walk (DEC-LAKE-INVARIANTS-PREINDEX-001)
# ────────────────────────────────────────────────────────────────────────


class TestStreamingWalk:
    """Walk-equivalence pins: the ``bars/`` tree is streamed ONE SERIES AT A
    TIME (the series key IS the directory structure) with the REPORT byte-
    compatible with the pre-streaming whole-tree pre-index scanner — same
    counters, same finding order, same ``first_skipped_path`` law, same
    scope semantics."""

    def test_first_skipped_prefers_parse_failure_over_read_failure(self, tmp_path: Path) -> None:
        """The pre-streaming scanner noted every PATH-parse failure during
        its whole-tree pre-index pass — BEFORE any read skip anywhere — so a
        parse skip beats a read skip even when the walk visits the unreadable
        file FIRST (CASH sorts before INDEX here; a naive walk-order note
        would name the unreadable file)."""
        garbage = _dest(tmp_path, segment="CASH", symbol="AAA")
        garbage.parent.mkdir(parents=True, exist_ok=True)
        garbage.write_bytes(b"not a parquet file at all")
        stray = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "no-year" / "06.parquet"
        _plant_raw(stray, [_ts(1)])
        report = scan_lake_invariants(tmp_path)
        assert report.files_scanned == 0
        assert report.files_skipped == 2
        assert report.first_skipped_path == str(stray)

    def test_empty_and_stray_dirs_streamed_past(self, tmp_path: Path) -> None:
        """Empty interval/year dirs, a non-dir at an intermediate level, and
        an off-depth parquet are all invisible — exactly like the old
        fixed-depth glob — and never crash the walk."""
        _write_bars(tmp_path, [_bar(_ts(1))])
        (tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "5minute").mkdir(parents=True)
        (tmp_path / "bars" / "NSE" / "INDEX" / "BANKNIFTY" / "1minute" / "2024").mkdir(parents=True)
        (tmp_path / "bars" / "NSE" / "INDEX" / "stray.txt").write_text("x", encoding="utf-8")
        loose = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "loose.parquet"
        loose.write_bytes(b"junk")  # interval depth — below the */*.parquet leaf
        report = scan_lake_invariants(tmp_path)
        assert report.files_scanned == 1
        assert report.files_skipped == 0
        assert report.series_scanned == 1
        assert report.findings == ()

    def test_scoped_out_month_still_feeds_drift_detection(self, tmp_path: Path) -> None:
        """``symbol_dirs`` is fed on PARSE, not on read: a drifted legacy dir
        whose ONLY file lies OUTSIDE the L2 months scope still surfaces
        ``LegacyKeyDrift`` (the pre-index fed it before the scope check)."""
        _write_bars(tmp_path, [_bar(_ts(1))], segment="CASH", symbol="M&M")
        _write_bars(tmp_path, [_bar(_ts(1, month=7))], segment="CASH", symbol="NSE-M&M")
        report = scan_lake_invariants(tmp_path, months={(2024, 6)})
        assert report.files_scanned == 1  # the July drifted file was never read
        drifts = [f for f in report.findings if isinstance(f, LegacyKeyDrift)]
        assert len(drifts) == 1
        assert drifts[0].drifted_symbol == "NSE-M&M"

    def test_multi_series_order_and_counters_preserved(self, tmp_path: Path) -> None:
        """Findings arrive in series-tuple order (component-sorted walk ≡ the
        old ``sorted(per_series)``), numeric month order within a series."""
        _plant_raw(_dest(tmp_path, symbol="NIFTY"), [_ts(1), _ts(1)])
        _plant_raw(_dest(tmp_path, symbol="BANKNIFTY"), [_ts(2), _ts(2)])
        _plant_raw(_dest(tmp_path, symbol="BANKNIFTY", month=7), [_ts(3, month=7), _ts(3, month=7)])
        report = scan_lake_invariants(tmp_path)
        assert report.files_scanned == 3
        assert report.series_scanned == 2
        dups = [f for f in report.findings if isinstance(f, IntraFileDup)]
        assert [d.path for d in dups] == [
            _dest(tmp_path, symbol="BANKNIFTY"),
            _dest(tmp_path, symbol="BANKNIFTY", month=7),
            _dest(tmp_path, symbol="NIFTY"),
        ]

    def test_child_dir_stat_fault_keeps_siblings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review fold (MINOR): ONE child's ``is_dir()`` OSError (EACCES /
        EIO / ESTALE — the errnos 3.12 pathlib re-raises) skips THAT entry
        only, never the whole level — the reviewer's repro: EIO on sibling B
        must yield [A, C] (pre-fix the whole-comprehension guard returned
        ``[]``, silently dropping the entire level; the old glob dropped only
        the offending entry)."""
        import errno

        parent = tmp_path / "bars"
        for name in ("A", "B", "C"):
            (parent / name).mkdir(parents=True)
        real_is_dir = Path.is_dir

        def flaky_is_dir(self: Path, **kwargs: bool) -> bool:
            if self.name == "B" and self.parent == parent:
                raise OSError(errno.EIO, "Input/output error", str(self))
            return real_is_dir(self, **kwargs)

        monkeypatch.setattr(Path, "is_dir", flaky_is_dir)
        assert li._child_dirs(parent, None) == [parent / "A", parent / "C"]
        # The name filter still applies over the surviving entries.
        assert li._child_dirs(parent, "C") == [parent / "C"]

    def test_child_dirs_vanished_parent_yields_empty(self, tmp_path: Path) -> None:
        """The whole-parent-listing guard is untouched by the per-child fold:
        a vanished parent still yields ``[]`` (the old ``base.exists()``
        guard generalised per level)."""
        assert li._child_dirs(tmp_path / "bars" / "gone", None) == []

    def test_streaming_resident_is_per_series_not_per_lake(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutant-killing memory-shape pin: the scan never materialises an
        all-series index — while series B is being read, series A's candidate
        list has already been dropped. Proven via a read spy that inspects
        the live candidate objects the generator yielded (gc-refcount-free:
        the spy records which series' lists are still referenced)."""
        import gc
        import weakref
        from collections.abc import Iterator

        class _TrackedList(list[Path]):  # a list SUBCLASS is weakref-able
            pass

        _write_bars(tmp_path, [_bar(_ts(1))], symbol="AAA")
        _write_bars(tmp_path, [_bar(_ts(2))], symbol="BBB")
        refs: list[weakref.ref[_TrackedList]] = []
        real_iter = li._iter_series_candidates

        def spying_iter(lake_root: Path, **filters: str | None) -> Iterator[list[Path]]:
            for candidates in real_iter(lake_root, **filters):
                tracked = _TrackedList(candidates)
                refs.append(weakref.ref(tracked))
                yield tracked

        monkeypatch.setattr(li, "_iter_series_candidates", spying_iter)
        alive_during_second: list[int] = []
        real_read = li._read_key_columns

        def spy(path: Path, columns: Sequence[str]) -> pl.DataFrame:
            if "BBB" in str(path):
                gc.collect()
                alive_during_second.append(sum(1 for r in refs if r() is not None))
            return real_read(path, columns)

        monkeypatch.setattr(li, "_read_key_columns", spy)
        report = scan_lake_invariants(tmp_path)
        assert report.files_scanned == 2
        assert report.series_scanned == 2
        # While series BBB was being read, ONLY its own candidate list was
        # alive — AAA's was already collected (streaming, not pre-indexed).
        assert alive_during_second == [1]


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
        # repr() — the token embeds the RAW series dir name (round-7 NIT, #907).
        assert warns[0]["partition"] == repr("options/NSE_FNO__13__WEEK__1__ATM/2024-06.parquet")
        assert warns[0]["physical_rows"] == 3
        assert warns[0]["unique_keys"] == 2

    def test_cross_side_same_timestamp_is_clean(self, tmp_path: Path) -> None:
        dest = tmp_path / "options" / "SERIES" / "2024-06.parquet"
        _plant_option_raw(dest, [("CE", _ts(1)), ("PE", _ts(1))])
        report = scan_lake_invariants(tmp_path)
        assert report.findings == ()
        assert report.option_files_scanned == 1

    @pytest.mark.parametrize("series", ["A*S", "A?S", "X[12]"])
    def test_glob_metachar_series_classifies_literal_bytes_only(
        self, tmp_path: Path, series: str
    ) -> None:
        """Round-15 MAJOR regression (refuter-confirmed): the scan read is LITERAL.

        ``_safe_segment`` legally admits polars glob metachars in a series dir
        name, and the shared ``_read_key_columns`` seam's default ``glob=True``
        made ``pl.read_parquet(options/A*S/…)`` return the UNION of every
        sibling series matching the pattern — so the scanner classified the
        glob-named series over OTHER files' bytes: a dup-poisoned SIBLING
        produced a phantom ``OptionIntraFileDup`` against the CLEAN literal
        file (whose literal-read heal then skips as stale — ``healed !=
        findings``, the L3 sentinel never stamps) and wrong census numbers on
        every sweep, no TOCTOU needed (empirically reproduced pre-fix,
        phantom finding on ``A*S`` built from ``ABS``'s rows). ``glob=False``
        pins the literal read; siblings ``ABS`` (matches all three patterns'
        shape family) + ``X1``/``X2`` cover balanced-bracket, ``*`` and ``?``.
        """
        for sibling in ("ABS", "X1", "X2"):
            sib = tmp_path / "options" / sibling / "2024-06.parquet"
            _plant_option_raw(sib, [("CE", _ts(1)), ("CE", _ts(1))])  # dup-poisoned
        literal = tmp_path / "options" / series / "2024-06.parquet"
        _plant_option_raw(literal, [("CE", _ts(5))])  # CLEAN
        report = scan_lake_invariants(tmp_path)
        assert report.option_files_scanned == 4
        dups = [f for f in report.findings if isinstance(f, OptionIntraFileDup)]
        assert len(dups) == len(report.findings) == 3  # exactly the three poisoned siblings
        flagged = {str(f.path) for f in dups}
        assert str(literal) not in flagged  # no phantom finding from sibling bytes

    def test_glob_metachar_series_canonical_sibling_read_is_literal(self, tmp_path: Path) -> None:
        """The targeted canonical-sibling overlap read rides the SAME
        ``_read_key_columns`` seam: a foreign key in ``X[12]`` whose canonical
        month sibling is CLEAN must classify ``overlap_rows == 0`` from the
        LITERAL sibling's bytes — pre-fix the glob read pulled ``X1``/``X2``'s
        July rows in, fabricating an overlap that the heal (a literal keyed
        removal) could never converge on."""
        series_dir = tmp_path / "options" / "X[12]"
        # June carries one July-routed foreign key; July sibling is clean and
        # does NOT hold that key. X1/X2's July files DO hold it — a glob read
        # of the canonical sibling would count a phantom overlap.
        foreign_ts = _ts(30, month=7, day=1)
        _plant_option_raw(series_dir / "2024-06.parquet", [("CE", _ts(1)), ("CE", foreign_ts)])
        _plant_option_raw(series_dir / "2024-07.parquet", [("PE", _ts(2, month=7, day=1))])
        for sibling in ("X1", "X2"):
            _plant_option_raw(
                tmp_path / "options" / sibling / "2024-07.parquet", [("CE", foreign_ts)]
            )
        report = scan_lake_invariants(tmp_path)
        foreigns = [f for f in report.findings if isinstance(f, OptionForeignMonthRows)]
        (finding,) = foreigns
        assert str(finding.path) == str(series_dir / "2024-06.parquet")
        assert finding.foreign_rows == 1
        assert finding.overlap_rows == 0  # literal sibling read — no phantom overlap

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
# Options tree — the pure (side, timestamp) classifier
# (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001)
# ────────────────────────────────────────────────────────────────────────


_OPT_PATH = Path("options/SERIES/2024-06.parquet")


class TestUtcAware:
    def test_naive_is_stamped_utc(self) -> None:
        naive = datetime(2024, 6, 3, 4, 1)
        assert utc_aware(naive) == datetime(2024, 6, 3, 4, 1, tzinfo=UTC)

    def test_aware_is_untouched(self) -> None:
        aware = _ts(1)
        assert utc_aware(aware) is aware


class TestOptionFileMonth:
    @pytest.mark.parametrize(
        ("stem", "expected"),
        [
            ("2024-06", (2024, 6)),
            ("2024-12", (2024, 12)),
            ("garbage", None),
            ("2024", None),
            ("2024-13", None),
            ("2024-00", None),
            ("2024-06-x", None),
            ("-06", None),
            # Unicode digit-PROPERTY chars: isdigit() True but int() raises —
            # must abstain, never ValueError (the scan-crash hostile-review MAJOR).
            ("²²-06", None),
            ("2024-²", None),
            # Parsable but NON-canonical stems abstain: they would claim the
            # SAME month as their zero-padded canonical twin (the twin-file
            # blind spot NIT) — only the writer's own f"{y:04d}-{m:02d}" parses.
            ("2024-6", None),
            ("02024-06", None),
            ("٢٠٢٤-٠٦", None),  # Arabic-Indic digits: int() ACCEPTS these — non-canonical
            # Round-13 NIT: a 5+-digit / year-0 stem zero-pad ROUND-TRIPS
            # (f"{12024:04d}" == "12024") so it passed the canonical check yet
            # no datetime month can ever route there — the writer can never
            # render it; bounded to datetime.MINYEAR..MAXYEAR (1..9999).
            ("12024-06", None),
            ("99999-12", None),
            ("0000-06", None),
            ("0001-01", (1, 1)),  # MINYEAR boundary — a writer-renderable stem parses
            ("9999-12", (9999, 12)),  # MAXYEAR boundary
        ],
    )
    def test_stem_parse_matrix(self, stem: str, expected: tuple[int, int] | None) -> None:
        assert li._option_file_month(Path(f"options/S/{stem}.parquet")) == expected


class TestClassifyOptionKeys:
    def test_empty_frame_is_clean(self) -> None:
        dup, foreign = classify_option_keys(_OPT_PATH, "SERIES", (2024, 6), _option_key_frame([]))
        assert (dup, foreign) == (None, None)

    def test_clean_frame_is_clean(self) -> None:
        frame = _option_key_frame([("CE", _ts(1)), ("CE", _ts(2))])
        assert classify_option_keys(_OPT_PATH, "SERIES", (2024, 6), frame) == (None, None)

    def test_dup_counted_exactly(self) -> None:
        frame = _option_key_frame([("CE", _ts(1)), ("CE", _ts(1)), ("PE", _ts(2))])
        dup, foreign = classify_option_keys(_OPT_PATH, "SERIES", (2024, 6), frame)
        assert foreign is None
        assert dup == OptionIntraFileDup(
            path=_OPT_PATH, series="SERIES", physical_rows=3, unique_keys=2
        )

    def test_cross_side_same_timestamp_is_not_a_dup(self) -> None:
        frame = _option_key_frame([("CE", _ts(1)), ("PE", _ts(1))])
        assert classify_option_keys(_OPT_PATH, "SERIES", (2024, 6), frame) == (None, None)

    def test_foreign_counted_with_home_month(self) -> None:
        frame = _option_key_frame([("CE", _ts(1)), ("PE", _ts(2, month=7, day=1))])
        dup, foreign = classify_option_keys(_OPT_PATH, "SERIES", (2024, 6), frame)
        assert dup is None
        assert foreign == OptionForeignMonthRows(
            path=_OPT_PATH,
            series="SERIES",
            year=2024,
            month=6,
            foreign_rows=1,
            overlap_rows=None,  # the pure core never reads siblings
        )

    def test_home_none_abstains_from_foreign(self) -> None:
        # An unparsable filename has no month to route against — dup-only.
        frame = _option_key_frame([("CE", _ts(1)), ("CE", _ts(1)), ("PE", _ts(2, month=7, day=1))])
        dup, foreign = classify_option_keys(_OPT_PATH, "SERIES", None, frame)
        assert dup is not None and dup.physical_rows == 3 and dup.unique_keys == 2
        assert foreign is None

    def test_null_timestamp_abstains_from_foreign(self) -> None:
        # NULL keys are still duplicate keys but carry no month to route.
        frame = _option_key_frame([("CE", None), ("CE", None)])
        dup, foreign = classify_option_keys(_OPT_PATH, "SERIES", (2024, 6), frame)
        assert dup is not None and dup.physical_rows == 2 and dup.unique_keys == 1
        assert foreign is None

    def test_foreign_rows_counts_dup_copies_individually(self) -> None:
        # Two physical copies of ONE foreign key = 2 foreign ROWS (mirrors bars).
        july = _ts(1, month=7, day=1)
        frame = _option_key_frame([("CE", _ts(0)), ("CE", july), ("CE", july)])
        _dup, foreign = classify_option_keys(_OPT_PATH, "SERIES", (2024, 6), frame)
        assert foreign is not None
        assert foreign.foreign_rows == 2


# ────────────────────────────────────────────────────────────────────────
# Options tree — foreign-month scan + overlap classification
# ────────────────────────────────────────────────────────────────────────


def _opt_dest(lake: Path, name: str, *, series: str = "SERIES") -> Path:
    return lake / "options" / series / f"{name}.parquet"


class TestOptionForeignScan:
    def test_foreign_detected_pure_misplacement(self, tmp_path: Path) -> None:
        # A June key planted in the JULY file; no June file exists at all.
        dest = _opt_dest(tmp_path, "2024-07")
        _plant_option_raw(dest, [("CE", _ts(1)), ("CE", _ts(2, month=7, day=1))])
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert len(report.findings) == 1
        finding = report.findings[0]
        assert isinstance(finding, OptionForeignMonthRows)
        assert finding.path == dest
        assert finding.series == "SERIES"
        assert finding.year == 2024
        assert finding.month == 7
        assert finding.foreign_rows == 1
        assert finding.overlap_rows == 0  # missing sibling = pure misplacement
        warns = _events(logs, "lake.invariant.option_foreign_month_rows")
        assert len(warns) == 1
        assert warns[0]["log_level"] == "warning"
        # repr() — the token embeds the RAW series dir name (round-7 NIT, #907).
        assert warns[0]["partition"] == repr("options/SERIES/2024-07.parquet")
        assert warns[0]["series"] == repr("SERIES")  # raw fs name — the #907 repr() convention
        assert warns[0]["foreign_rows"] == 1
        assert warns[0]["overlap_rows"] == 0

    def test_overlap_classified_against_canonical_sibling(self, tmp_path: Path) -> None:
        # The SAME (CE, June-ts) key lives in BOTH the June and July files.
        _plant_option_raw(_opt_dest(tmp_path, "2024-06"), [("CE", _ts(1)), ("PE", _ts(2))])
        _plant_option_raw(
            _opt_dest(tmp_path, "2024-07"), [("CE", _ts(1)), ("CE", _ts(3, month=7, day=1))]
        )
        report = scan_lake_invariants(tmp_path)
        foreigns = [f for f in report.findings if isinstance(f, OptionForeignMonthRows)]
        assert len(foreigns) == 1
        assert foreigns[0].foreign_rows == 1
        assert foreigns[0].overlap_rows == 1

    def test_cross_side_same_timestamp_not_overlap_matched(self, tmp_path: Path) -> None:
        # Canonical June holds (PE, ts1); the July file carries (CE, ts1) —
        # the key includes SIDE, so this is NOT an overlap.
        _plant_option_raw(_opt_dest(tmp_path, "2024-06"), [("PE", _ts(1))])
        _plant_option_raw(
            _opt_dest(tmp_path, "2024-07"), [("CE", _ts(1)), ("PE", _ts(3, month=7, day=1))]
        )
        report = scan_lake_invariants(tmp_path)
        foreigns = [f for f in report.findings if isinstance(f, OptionForeignMonthRows)]
        assert len(foreigns) == 1
        assert foreigns[0].overlap_rows == 0

    def test_unreadable_canonical_sibling_abstains(self, tmp_path: Path) -> None:
        june = _opt_dest(tmp_path, "2024-06")
        june.parent.mkdir(parents=True, exist_ok=True)
        june.write_bytes(b"not a parquet file at all")
        _plant_option_raw(
            _opt_dest(tmp_path, "2024-07"), [("CE", _ts(1)), ("CE", _ts(3, month=7, day=1))]
        )
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert report.option_files_skipped == 1  # the garbage June file itself
        foreigns = [f for f in report.findings if isinstance(f, OptionForeignMonthRows)]
        assert len(foreigns) == 1
        assert foreigns[0].overlap_rows is None  # honest abstain
        # Skipped once as a scanned file + once as the canonical targeted read.
        assert len(_events(logs, "lake.invariant.scan_skip")) == 2

    def test_canonical_read_cached_per_series(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # July AND August both carry the same June foreign key — the canonical
        # June sibling is read ONCE (per-series cache), not once per finding.
        _plant_option_raw(_opt_dest(tmp_path, "2024-06"), [("CE", _ts(1))])
        _plant_option_raw(
            _opt_dest(tmp_path, "2024-07"), [("CE", _ts(1)), ("PE", _ts(2, month=7, day=1))]
        )
        _plant_option_raw(
            _opt_dest(tmp_path, "2024-08"), [("CE", _ts(1)), ("PE", _ts(2, month=8, day=1))]
        )
        calls: list[Path] = []
        real = li._read_key_columns

        def spy(path: Path, columns: Sequence[str]) -> pl.DataFrame:
            calls.append(path)
            return real(path, columns)

        monkeypatch.setattr(li, "_read_key_columns", spy)
        report = scan_lake_invariants(tmp_path)
        foreigns = [f for f in report.findings if isinstance(f, OptionForeignMonthRows)]
        assert [f.overlap_rows for f in foreigns] == [1, 1]
        # 3 in-scope scans + exactly ONE targeted canonical re-read of June.
        assert len(calls) == 4
        assert calls.count(_opt_dest(tmp_path, "2024-06")) == 2  # scanned + canonical

    def test_clean_option_file_pays_no_extra_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The min/max fast path: a clean file costs exactly ONE key read.
        _plant_option_raw(_opt_dest(tmp_path, "2024-06"), [("CE", _ts(1)), ("PE", _ts(2))])
        _plant_option_raw(_opt_dest(tmp_path, "2024-07"), [("CE", _ts(3, month=7, day=1))])
        calls: list[Path] = []
        real = li._read_key_columns

        def spy(path: Path, columns: Sequence[str]) -> pl.DataFrame:
            calls.append(path)
            return real(path, columns)

        monkeypatch.setattr(li, "_read_key_columns", spy)
        report = scan_lake_invariants(tmp_path)
        assert report.findings == ()
        assert report.option_files_scanned == 2
        assert len(calls) == 2  # ONE read per file, ZERO canonical reads

    def test_option_utc_not_ist_month_boundary(self, tmp_path: Path) -> None:
        # 2024-06-30 20:30 UTC = 2024-07-01 02:00 IST: the bar is HOME in the
        # June file under UTC routing (the writer's own router) — never flagged.
        straddle = datetime(2024, 6, 30, 20, 30, tzinfo=UTC)
        _plant_option_raw(_opt_dest(tmp_path, "2024-06"), [("CE", straddle)])
        report = scan_lake_invariants(tmp_path)
        assert report.findings == ()

    def test_months_scope_skips_out_of_scope_before_reading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _plant_option_raw(_opt_dest(tmp_path, "2024-06"), [("CE", _ts(1))])
        _plant_option_raw(_opt_dest(tmp_path, "2024-07"), [("CE", _ts(2, month=7, day=1))])
        # An unparsable-name file cannot be proven in scope — skipped too.
        _plant_option_raw(_opt_dest(tmp_path, "junk"), [("CE", _ts(3)), ("CE", _ts(3))])
        calls: list[Path] = []
        real = li._read_key_columns

        def spy(path: Path, columns: Sequence[str]) -> pl.DataFrame:
            calls.append(path)
            return real(path, columns)

        monkeypatch.setattr(li, "_read_key_columns", spy)
        report = scan_lake_invariants(tmp_path, months={(2024, 6)}, include_bars=False)
        assert report.option_files_scanned == 1
        assert report.option_files_skipped == 0
        assert calls == [_opt_dest(tmp_path, "2024-06")]
        assert report.findings == ()

    def test_overlap_counted_across_two_canonical_months(self, tmp_path: Path) -> None:
        """MINOR regression (mutation gap): overlap is SUMMED across ALL
        affected canonical months — a ``sorted(grouped)[:1]`` truncation
        mutant in ``_classify_option_overlap`` undercounts a file whose
        foreign keys route to two different months."""
        _plant_option_raw(_opt_dest(tmp_path, "2024-05"), [("CE", _ts(1, month=5, day=2))])
        _plant_option_raw(_opt_dest(tmp_path, "2024-07"), [("CE", _ts(2, month=7, day=2))])
        _plant_option_raw(
            _opt_dest(tmp_path, "2024-06"),
            [("CE", _ts(0)), ("CE", _ts(1, month=5, day=2)), ("CE", _ts(2, month=7, day=2))],
        )
        report = scan_lake_invariants(tmp_path)
        foreigns = [f for f in report.findings if isinstance(f, OptionForeignMonthRows)]
        assert len(foreigns) == 1  # only the June file carries foreign rows
        assert foreigns[0].foreign_rows == 2
        assert foreigns[0].overlap_rows == 2  # 1 (May) + 1 (July) — both months counted

    def test_crafted_unicode_digit_filename_never_crashes_scan(self, tmp_path: Path) -> None:
        """MAJOR regression: ``'²²'.isdigit()`` is True but ``int('²²')``
        raises — pre-fix ONE crafted filename crashed the whole scan (500-ing
        the /universe chip, disabling the L3 boot sweep + ALL healing, and
        discarding the L2 batch). It must abstain to dup-only instead."""
        dest = _opt_dest(tmp_path, "²²-06")
        _plant_option_raw(dest, [("CE", _ts(1)), ("CE", _ts(1)), ("PE", _ts(2, month=7, day=1))])
        report = scan_lake_invariants(tmp_path)
        (finding,) = report.findings
        assert isinstance(finding, OptionIntraFileDup)  # dup-only honest abstain
        assert finding.physical_rows == 3
        assert finding.unique_keys == 2
        # The months-scoped L2 path parses the home BEFORE the scope check —
        # it must not crash either (unprovable-in-scope ⇒ skipped unread).
        scoped = scan_lake_invariants(tmp_path, months={(2024, 6)}, include_bars=False)
        assert scoped.option_files_scanned == 0
        assert scoped.findings == ()

    def test_non_canonical_stem_abstains_to_dup_only(self, tmp_path: Path) -> None:
        """A parsable-but-NON-canonical stem (``2024-6``) would claim the SAME
        month as its canonical twin ``2024-06`` (the hand-planted twin-file
        blind spot) — it abstains from foreign routing (``home=None``)."""
        _plant_option_raw(
            _opt_dest(tmp_path, "2024-6"), [("CE", _ts(1)), ("PE", _ts(2, month=7, day=1))]
        )
        report = scan_lake_invariants(tmp_path)
        assert report.findings == ()  # no home month — foreign abstained; no dup either
        assert report.option_files_scanned == 1

    def test_symlinked_series_dir_skipped(self, tmp_path: Path) -> None:
        """A SYMLINKED series dir is never scanned (hostile-review NIT): the
        heal primitives unlink/rmdir/create INSIDE a series dir, so a finding
        under a followed symlink would let a heal mutate files OUTSIDE the
        lake root. Skipped loudly, poison outside stays untouched-and-unseen."""
        outside = tmp_path / "outside" / "SERIES"
        _plant_option_raw(outside / "2024-06.parquet", [("CE", _ts(1)), ("CE", _ts(1))])
        lake = tmp_path / "lake"
        (lake / "options").mkdir(parents=True)
        (lake / "options" / "LINKED").symlink_to(outside, target_is_directory=True)
        with capture_logs() as logs:
            report = scan_lake_invariants(lake)
        assert report.findings == ()
        assert report.option_files_scanned == 0
        skips = _events(logs, "lake.invariant.symlink_skipped")
        assert len(skips) == 1
        # repr() — the path embeds the series dir name (round-8 fold, #907).
        assert skips[0]["path"] == repr(str(lake / "options" / "LINKED"))

    def test_symlinked_options_root_skipped(self, tmp_path: Path) -> None:
        """Round-2 MAJOR regression (Vector 1, empirically reproduced pre-fix):
        ``is_dir()`` FOLLOWS a symlinked ``options/`` ROOT and the series dirs
        inside its target are REAL dirs, so the per-series ``is_symlink()``
        probe never fired — the whole OUTSIDE tree was scanned and its
        findings handed to the default AUTO-ON healer (which then created
        ``2024-07.parquet`` outside the lake root). Skipped loudly instead,
        like a series-dir symlink; the outside poison stays untouched-and-
        unseen (the writer's mutation-boundary containment is the second,
        TOCTOU-safe rail)."""
        outside = tmp_path / "outside" / "options"
        _plant_option_raw(
            outside / "SERIES" / "2024-06.parquet",
            [("CE", _ts(1)), ("CE", _ts(1)), ("PE", _ts(2, month=7, day=1))],
        )
        lake = tmp_path / "lake"
        lake.mkdir()
        (lake / "options").symlink_to(outside, target_is_directory=True)
        with capture_logs() as logs:
            report = scan_lake_invariants(lake)
        assert report.findings == ()
        assert report.option_files_scanned == 0
        skips = _events(logs, "lake.invariant.symlink_skipped")
        assert len(skips) == 1
        # Deliberately RAW (no repr): <lake_root>/options is the
        # operator-configured root + a fixed literal — no vendor-derived
        # component (the round-8 fold guards only series-embedding paths).
        assert skips[0]["path"] == str(lake / "options")

    def test_symlinked_partition_file_skipped(self, tmp_path: Path) -> None:
        """Round-4 MINOR regression (refuter-confirmed, empirically probed):
        the root + series-dir probes never checked the PARTITION FILE — a
        symlinked ``<YYYY-MM>.parquet`` pointing OUTSIDE the lake was scanned,
        classified ``OptionForeignMonthRows``, and handed to the healer, whose
        source read followed the link and IMPORTED the outside rows into an
        in-lake canonical file (the write-leg containment passed because
        ``dest.parent`` is a REAL in-lake series dir) while the source removal
        was refused — findings=1 healed=0 errors=1 on every sweep, permanently
        non-converging. Never scanned ⇒ never healed; skipped loudly, the
        sibling REAL file still scanned."""
        outside = tmp_path / "outside"
        # Outside bytes: a Feb-2021 row under a Jan-2021 name (foreign pre-fix).
        _plant_option_raw(outside / "2021-01.parquet", [("CE", _ts(1, year=2021, month=2, day=2))])
        lake = tmp_path / "lake"
        series = lake / "options" / "SERIES"
        _plant_option_raw(series / "2021-03.parquet", [("CE", _ts(0, year=2021, month=3))])
        (series / "2021-01.parquet").symlink_to(outside / "2021-01.parquet")
        with capture_logs() as logs:
            report = scan_lake_invariants(lake)
        assert report.findings == ()  # the linked file is never classified
        assert report.option_files_scanned == 1  # the REAL sibling only
        assert report.option_files_skipped == 1  # counted, mass-rail honest
        skips = _events(logs, "lake.invariant.symlink_skipped")
        assert [s["path"] for s in skips] == [repr(str(series / "2021-01.parquet"))]

    def test_out_of_scope_symlinked_file_silently_scope_skipped(self, tmp_path: Path) -> None:
        """Round-5 NIT: the months-scope bound (a pure filename parse) runs
        BEFORE the per-file symlink probe — an OUT-of-scope symlinked option
        file is silently scope-skipped like any other out-of-scope file
        (neither warned nor counted, the L2 scope law); an IN-scope
        symlinked file is still warned + counted; the unbounded sweep
        (``test_symlinked_partition_file_skipped``) still warns."""
        outside = tmp_path / "outside"
        _plant_option_raw(outside / "2024-07.parquet", [("CE", _ts(1, month=7, day=1))])
        _plant_option_raw(_opt_dest(tmp_path, "2024-06"), [("CE", _ts(1))])
        _opt_dest(tmp_path, "2024-07").symlink_to(outside / "2024-07.parquet")
        with capture_logs() as logs:
            scoped = scan_lake_invariants(tmp_path, months={(2024, 6)}, include_bars=False)
        assert scoped.option_files_scanned == 1  # the real June file
        assert scoped.option_files_skipped == 0  # the July symlink: scope-skipped
        assert _events(logs, "lake.invariant.symlink_skipped") == []
        with capture_logs() as logs_in_scope:
            scoped_july = scan_lake_invariants(tmp_path, months={(2024, 7)}, include_bars=False)
        assert scoped_july.option_files_scanned == 0  # June scope-skipped silently
        assert scoped_july.option_files_skipped == 1  # the in-scope symlink counted
        skips = _events(logs_in_scope, "lake.invariant.symlink_skipped")
        assert [s["path"] for s in skips] == [repr(str(_opt_dest(tmp_path, "2024-07")))]

    def test_symlinked_canonical_sibling_abstains_unread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round-5 NIT: the targeted canonical-sibling overlap read now takes
        the SAME symlink skip as the main glob loop — warned loudly, NEVER
        read (spy-pinned), overlap honestly unclassified (``None``). A
        scan-time READ only (the writer's containment rail still refuses the
        heal-side target) — but the scan must not follow the link either."""
        outside = tmp_path / "outside"
        _plant_option_raw(outside / "2024-06.parquet", [("CE", _ts(1))])
        july = _opt_dest(tmp_path, "2024-07")
        _plant_option_raw(july, [("CE", _ts(1)), ("PE", _ts(2, month=7, day=1))])
        june = _opt_dest(tmp_path, "2024-06")
        june.symlink_to(outside / "2024-06.parquet")
        calls: list[Path] = []
        real = li._read_key_columns

        def spy(path: Path, columns: Sequence[str]) -> pl.DataFrame:
            calls.append(path)
            return real(path, columns)

        monkeypatch.setattr(li, "_read_key_columns", spy)
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert report.option_files_scanned == 1  # July only
        assert report.option_files_skipped == 1  # the June symlink (glob loop)
        foreigns = [f for f in report.findings if isinstance(f, OptionForeignMonthRows)]
        assert len(foreigns) == 1
        assert foreigns[0].overlap_rows is None  # honest abstain, never a guess
        assert calls == [july]  # the linked canonical was NEVER read
        skips = _events(logs, "lake.invariant.symlink_skipped")
        # Once from the glob loop, once from the targeted canonical read —
        # both repr()-guarded (round-8 fold).
        assert [s["path"] for s in skips] == [repr(str(june)), repr(str(june))]

    def test_no_scope_unparsable_name_still_dup_scanned(self, tmp_path: Path) -> None:
        # Today's behaviour pinned: without a months scope an unparsable-name
        # file is still dup-scanned (home=None → foreign abstains).
        _plant_option_raw(
            _opt_dest(tmp_path, "junk"),
            [("CE", _ts(1)), ("CE", _ts(1)), ("PE", _ts(2, month=7, day=1))],
        )
        report = scan_lake_invariants(tmp_path)
        assert len(report.findings) == 1
        finding = report.findings[0]
        assert isinstance(finding, OptionIntraFileDup)
        assert finding.physical_rows == 3
        assert finding.unique_keys == 2

    def test_null_side_foreign_key_counted_and_keyed(self, tmp_path: Path) -> None:
        # A hand-planted NULL side with a foreign ts: counted foreign (routing
        # is timestamp-only) AND carried raw in the key set — the scanner's
        # count and the healer's movable set can never diverge (liveness).
        july = _ts(1, month=7, day=1)
        _plant_option_raw(_opt_dest(tmp_path, "2024-06"), [("CE", _ts(0)), (None, july)])
        report = scan_lake_invariants(tmp_path)
        foreigns = [f for f in report.findings if isinstance(f, OptionForeignMonthRows)]
        assert len(foreigns) == 1
        assert foreigns[0].foreign_rows == 1
        keys = li._option_foreign_keys(
            (2024, 6), li._read_key_columns(_opt_dest(tmp_path, "2024-06"), ("side", "timestamp"))
        )
        assert keys == {(None, july)}

    def test_null_timestamp_abstains_from_key_set(self, tmp_path: Path) -> None:
        # A null ts has no month to route — never in the foreign key set.
        frame = _option_key_frame([("CE", None), ("PE", _ts(1, month=7, day=1))])
        keys = li._option_foreign_keys((2024, 6), frame)
        assert keys == {("PE", _ts(1, month=7, day=1))}

    def test_option_skip_event_paths_repr_guarded_control_char_series(self, tmp_path: Path) -> None:
        """Round-8 log-injection regression (refuter-confirmed MINOR): the
        option-tree ``symlink_skipped`` / ``scan_skip`` events embed
        ``str(<...>/<series>/<file>)`` where the SERIES component is
        vendor-influenceable (``_safe_segment`` strips no control chars), so
        every such path now rides ``repr()`` exactly like the option finding
        events (round-7): an ANSI-escape series name renders escaped, never
        verbatim into a literal-rendering log sink."""
        lake = tmp_path / "lake"
        # (a) A SYMLINKED series dir carrying an ESC in its name (dir probe).
        outside_dir = tmp_path / "outside" / "series"
        _plant_option_raw(outside_dir / "2024-06.parquet", [("CE", _ts(1))])
        (lake / "options").mkdir(parents=True)
        linked_dir = lake / "options" / "SER\x1bIES"
        linked_dir.symlink_to(outside_dir, target_is_directory=True)
        # (b) A REAL ESC-named series dir with a symlinked partition FILE
        # (per-file probe) and a corrupt file (the read skip).
        outside_file = tmp_path / "outside" / "2024-07.parquet"
        _plant_option_raw(outside_file, [("CE", _ts(1, month=7, day=1))])
        series2 = lake / "options" / "S\x1bERIES2"
        series2.mkdir()
        (series2 / "2024-07.parquet").symlink_to(outside_file)
        (series2 / "2024-08.parquet").write_bytes(b"not a parquet file")
        with capture_logs() as logs:
            report = scan_lake_invariants(lake)
        assert report.findings == ()
        assert report.option_files_skipped == 2  # the symlink + the corrupt file
        sym = _events(logs, "lake.invariant.symlink_skipped")
        assert {str(e["path"]) for e in sym} == {
            repr(str(linked_dir)),
            repr(str(series2 / "2024-07.parquet")),
        }
        skips = _events(logs, "lake.invariant.scan_skip")
        assert [e["path"] for e in skips] == [repr(str(series2 / "2024-08.parquet"))]
        # The load-bearing property: ESC never reaches the rendered value raw.
        assert all("\x1b" not in str(e["path"]) for e in [*sym, *skips])

    def test_canonical_sibling_skip_paths_repr_guarded(self, tmp_path: Path) -> None:
        """The round-8 twin for the TARGETED canonical-sibling reads: a
        symlinked June sibling (``symlink_skipped``) and a corrupt May sibling
        (``scan_skip``) inside an ESC-named series dir both render their paths
        ``repr()``-escaped — from the glob loop AND the targeted read."""
        series = tmp_path / "options" / "SER\x1bIES"
        # The July file carries a May stray AND a June stray.
        _plant_option_raw(
            series / "2024-07.parquet",
            [("PE", _ts(1, month=5, day=2)), ("CE", _ts(2)), ("PE", _ts(3, month=7, day=1))],
        )
        may = series / "2024-05.parquet"
        may.write_bytes(b"not a parquet file at all")
        outside = tmp_path / "outside" / "2024-06.parquet"
        _plant_option_raw(outside, [("CE", _ts(2))])
        june = series / "2024-06.parquet"
        june.symlink_to(outside)
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        foreigns = [f for f in report.findings if isinstance(f, OptionForeignMonthRows)]
        assert len(foreigns) == 1
        assert foreigns[0].overlap_rows is None  # honest abstain on both months
        # Glob-loop event first, targeted-read event second — both repr()'d.
        skips = _events(logs, "lake.invariant.scan_skip")
        assert [e["path"] for e in skips] == [repr(str(may))] * 2
        sym = _events(logs, "lake.invariant.symlink_skipped")
        assert [e["path"] for e in sym] == [repr(str(june))] * 2
        assert all("\x1b" not in str(e["path"]) for e in [*sym, *skips])

    def test_option_scan_skip_error_payload_repr_guarded(self, tmp_path: Path) -> None:
        """Round-17 log-injection regression (refuter-confirmed MINOR): the
        option-tree ``scan_skip`` ERROR payload embeds the RAW
        vendor-influenceable path too — a polars ``ColumnNotFoundError``
        dumps the resolved plan (``Parquet SCAN [<path>]``) and a
        polars-raised ``FileNotFoundError`` interpolates the path un-repr'd
        (both empirically verified) — so a newline/ESC series name forged
        log lines through ``error=`` while the paired ``path=`` field was
        already escaped (the round-16 ``partition_error`` channel). The
        ``error=`` field now rides ``repr()`` at the glob-loop site.

        MERGE RECONCILE (DEC-ALIEN-SCHEMA-READ-HEAL-001 ∪
        DEC-LAKE-HEAL-OPTIONS-FOREIGN-001): a keyless VALID parquet — the
        original fixture — is now the deterministic alien-schema class and
        emits a :class:`SchemaAlienFile` finding whose ``partition`` payload
        rides the SAME repr() escape; the glob-loop ``scan_skip`` else-arm
        keeps the repr()'d ``error=`` for a genuinely unreadable file."""
        import pyarrow as pa
        import pyarrow.parquet as pq
        from brutex.data.lake_invariants import SchemaAlienFile

        bad = tmp_path / "options" / "SER\x1b\nIES" / "2024-06.parquet"
        bad.parent.mkdir(parents=True, exist_ok=True)
        # A VALID parquet with neither key column: the alien-schema class
        # (the 2-key-column read raises ColumnNotFoundError whose message
        # embeds the raw path; the footer probe proves the columns missing).
        pq.write_table(pa.table({"x": pa.array([1], type=pa.int64())}), bad)
        # A footer-corrupt sibling: NOT the alien class — the plain
        # transient-tolerant scan_skip with the repr()'d error payload.
        corrupt = bad.parent / "2024-07.parquet"
        corrupt.write_bytes(b"not a parquet file at all")
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        (alien,) = report.findings
        assert isinstance(alien, SchemaAlienFile)
        assert alien.path == bad
        assert alien.series == "SER\x1b\nIES"  # RAW dataclass value (machine surface)
        assert report.option_files_skipped == 2
        (alien_event,) = _events(logs, "lake.invariant.schema_alien_file")
        partition = alien_event["partition"]
        assert isinstance(partition, str)
        # Pre-fix class: the raw ESC + newline from the series name would
        # ride into the rendered payload — repr()-escaped, never dropped.
        assert "\x1b" not in partition
        assert "\n" not in partition
        assert "\\x1b" in partition
        (skip,) = _events(logs, "lake.invariant.scan_skip")
        assert skip["path"] == repr(str(corrupt))
        error = skip["error"]
        assert isinstance(error, str)
        assert "\x1b" not in error
        assert "\n" not in error
        # repr()-wrapped: a str repr always renders quoted.
        assert error.startswith(("'", '"'))

    def test_canonical_sibling_scan_skip_error_payload_repr_guarded(self, tmp_path: Path) -> None:
        """The round-17 twin for the TARGETED canonical-sibling read
        (``_canonical_option_keys``): an unreadable July sibling inside a
        newline/ESC-named series dir renders its read-error message
        ``repr()``-escaped on the targeted read's ``scan_skip``, and the
        finding still abstains honestly.

        MERGE RECONCILE (DEC-ALIEN-SCHEMA-READ-HEAL-001 ∪
        DEC-LAKE-HEAL-OPTIONS-FOREIGN-001): the GLOB-loop encounter with the
        keyless July file now emits a :class:`SchemaAlienFile` finding (no
        glob-loop ``scan_skip``); the targeted sibling read keeps the plain
        repr()-escaped ``scan_skip`` — the alien classification is a
        glob-loop-only concern (the targeted read abstains, never
        quarantines)."""
        import pyarrow as pa
        import pyarrow.parquet as pq
        from brutex.data.lake_invariants import SchemaAlienFile

        series = tmp_path / "options" / "SER\x1b\nIES2"
        _plant_option_raw(
            series / "2024-06.parquet", [("CE", _ts(1)), ("CE", _ts(2, month=7, day=1))]
        )
        july = series / "2024-07.parquet"
        # Keyless valid parquet — the sibling read fails with the raw path
        # embedded in the ColumnNotFoundError plan dump.
        pq.write_table(pa.table({"x": pa.array([1], type=pa.int64())}), july)
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        foreigns = [f for f in report.findings if isinstance(f, OptionForeignMonthRows)]
        assert len(foreigns) == 1
        assert foreigns[0].overlap_rows is None  # honest abstain preserved
        aliens = [f for f in report.findings if isinstance(f, SchemaAlienFile)]
        assert [a.path for a in aliens] == [july]  # the glob-loop arm
        skips = _events(logs, "lake.invariant.scan_skip")
        # ONLY the targeted sibling read skips now (the glob loop emitted the
        # alien finding for the keyless file instead).
        assert [s["path"] for s in skips] == [repr(str(july))]
        for skip in skips:
            error = skip["error"]
            assert isinstance(error, str)
            assert "\x1b" not in error
            assert "\n" not in error
            assert "\\x1b" in error


class TestOptionKeyDtypeGate:
    """Round-12 fold: non-hashable option key dtypes are gated BEFORE classification.

    Pre-fix, ONE hand-planted ``options/`` file whose ``side`` column dtype is
    ``List(Utf8)`` / ``Struct`` crashed the WHOLE scan with ``TypeError:
    unhashable type`` (the key-set builds hash the raw Python values) — ALL
    healing inert (L3 boot sweep, L2 batches, the census surfaces). The gate
    (:func:`option_key_dtype_error`) skips such a file LOUDLY instead —
    counted, repr()-logged (the rounds-8/9 convention), never classified.
    """

    def test_list_side_file_skipped_scan_survives_sibling_found(self, tmp_path: Path) -> None:
        bad = tmp_path / "options" / "SERIES_BAD" / "2024-06.parquet"
        _plant_option_nested_side(bad, [_ts(1), _ts(2, month=7, day=1)])  # foreign shape too
        good = tmp_path / "options" / "SERIES_GOOD" / "2024-06.parquet"
        _plant_option_raw(good, [("CE", _ts(1)), ("CE", _ts(1))])
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)  # pre-fix: TypeError crash right here
        assert report.option_files_scanned == 1
        assert report.option_files_skipped == 1
        (finding,) = report.findings  # the sibling series' dup is still found
        assert isinstance(finding, OptionIntraFileDup)
        assert finding.path == good
        (skip,) = _events(logs, "lake.invariant.scan_skip")
        assert skip["path"] == repr(str(bad))  # vendor-influenceable series dir name
        error = skip["error"]
        assert isinstance(error, str)
        assert "side" in error

    def test_struct_side_file_skipped(self, tmp_path: Path) -> None:
        bad = tmp_path / "options" / "SERIES_STRUCT" / "2024-06.parquet"
        _plant_option_nested_side(bad, [_ts(1), _ts(2, month=7, day=1)], side_kind="struct")
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert report.findings == ()
        assert report.option_files_scanned == 0
        assert report.option_files_skipped == 1
        (skip,) = _events(logs, "lake.invariant.scan_skip")
        assert skip["path"] == repr(str(bad))

    def test_non_datetime_timestamp_dtype_skipped(self, tmp_path: Path) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        bad = tmp_path / "options" / "SERIES_INT_TS" / "2024-06.parquet"
        bad.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table(
                {
                    "side": pa.array(["CE", "CE"], type=pa.string()),
                    "timestamp": pa.array([1, 2], type=pa.int64()),
                }
            ),
            bad,
        )
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert report.findings == ()
        assert report.option_files_scanned == 0
        assert report.option_files_skipped == 1
        (skip,) = _events(logs, "lake.invariant.scan_skip")
        error = skip["error"]
        assert isinstance(error, str)
        assert "timestamp" in error

    def test_null_dtype_key_columns_pass_the_gate(self, tmp_path: Path) -> None:
        # An all-null hand-planted file typed pl.Null on BOTH key columns is
        # hashable (None) — it stays classifiable (here: a (None, None) dup).
        import pyarrow as pa
        import pyarrow.parquet as pq

        dest = tmp_path / "options" / "SERIES_NULL" / "2024-06.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({"side": pa.nulls(2), "timestamp": pa.nulls(2)}), dest)
        report = scan_lake_invariants(tmp_path)
        assert report.option_files_scanned == 1
        assert report.option_files_skipped == 0
        (finding,) = report.findings
        assert isinstance(finding, OptionIntraFileDup)
        assert finding.physical_rows == 2
        assert finding.unique_keys == 1

    def test_unclassifiable_canonical_sibling_abstains(self, tmp_path: Path) -> None:
        # The targeted overlap read hits a nested-dtype canonical sibling: the
        # finding stays honestly unclassified (overlap_rows=None), never a
        # crash — the unreadable-sibling abstain twin.
        june = tmp_path / "options" / "SERIES" / "2024-06.parquet"
        _plant_option_raw(june, [("CE", _ts(1)), ("CE", _ts(2, month=7, day=1))])
        july = tmp_path / "options" / "SERIES" / "2024-07.parquet"
        _plant_option_nested_side(july, [_ts(2, month=7, day=1)])
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        (finding,) = report.findings
        assert isinstance(finding, OptionForeignMonthRows)
        assert finding.path == june
        assert finding.overlap_rows is None  # honest abstain
        assert report.option_files_scanned == 1
        assert report.option_files_skipped == 1
        skips = _events(logs, "lake.invariant.scan_skip")
        # one from the main glob loop, one from the targeted sibling read
        assert [s["path"] for s in skips] == [repr(str(july)), repr(str(july))]

    def test_option_key_dtype_error_pure(self) -> None:
        good = _option_key_frame([("CE", _ts(1))])
        assert option_key_dtype_error(good) is None
        nulls = pl.DataFrame(
            {
                "side": pl.Series("side", [None], dtype=pl.Null),
                "timestamp": pl.Series("timestamp", [None], dtype=pl.Null),
            }
        )
        assert option_key_dtype_error(nulls) is None
        bad_side = pl.DataFrame(
            {
                "side": pl.Series("side", [["CE"]], dtype=pl.List(pl.Utf8)),
                "timestamp": pl.Series("timestamp", [_ts(1)], dtype=pl.Datetime("us", "UTC")),
            }
        )
        side_error = option_key_dtype_error(bad_side)
        assert side_error is not None
        assert "side" in side_error
        bad_ts = pl.DataFrame(
            {
                "side": pl.Series("side", ["CE"], dtype=pl.Utf8),
                "timestamp": pl.Series("timestamp", [1], dtype=pl.Int64),
            }
        )
        ts_error = option_key_dtype_error(bad_ts)
        assert ts_error is not None
        assert "timestamp" in ts_error

    def test_option_key_dtype_error_missing_column_is_verdict_not_keyerror(self) -> None:
        # Round-14 fold: the gate must be TOTAL over its input shapes — a
        # frame MISSING a key column (a scan→heal swap to a bars-style
        # timestamp-only parquet) returned a bare KeyError from
        # ``frame.schema["side"]``, escaping the healer's per-finding
        # isolation set and aborting the WHOLE heal batch.
        no_side = pl.DataFrame(
            {"timestamp": pl.Series("timestamp", [_ts(1)], dtype=pl.Datetime("us", "UTC"))}
        )
        side_error = option_key_dtype_error(no_side)  # pre-fix: KeyError right here
        assert side_error is not None
        assert "side" in side_error
        assert "missing" in side_error
        no_ts = pl.DataFrame({"side": pl.Series("side", ["CE"], dtype=pl.Utf8)})
        ts_error = option_key_dtype_error(no_ts)
        assert ts_error is not None
        assert "timestamp" in ts_error
        assert "missing" in ts_error
        neither = pl.DataFrame({"foo": [1, 2]})
        neither_error = option_key_dtype_error(neither)
        assert neither_error is not None
        assert "side" in neither_error  # side checked first — deterministic verdict

    def test_option_unclassifiable_message_exact_shape(self) -> None:
        # §11 COMMON (round-15 NIT fold): BOTH consumers of a non-None gate
        # verdict — the healer's consistent-read gate
        # (``lake_heal._heal_option_foreign``) and the writer's keyed-removal
        # re-read gate (``OptionParquetWriter.heal_partition``) — raise
        # ``OptionWriterError(option_unclassifiable_message(path, verdict))``,
        # so the exact composed shape is pinned here against drift. *path*
        # rides RAW (the exception-MESSAGE convention; the LOG sites repr()
        # their path fields per the rounds-8/9 log-injection folds) and
        # *reason* is a REAL gate verdict, exactly as both call sites pass it.
        path = Path("/lake/options/SERIES/2024-06.parquet")
        verdict = option_key_dtype_error(pl.DataFrame({"foo": [1]}))
        assert verdict == "option side column missing — unclassifiable"
        message = option_unclassifiable_message(path, verdict)
        assert message == (
            "unclassifiable option partition /lake/options/SERIES/2024-06.parquet: "
            "option side column missing — unclassifiable"
        )


class TestOutOfRangeTimestampGate:
    """Round-18 MAJOR (refuter-confirmed): an out-of-``datetime``-range VALUE
    inside a sound ``Datetime`` column crashed the WHOLE scan/heal.

    The rounds-12/13/14 gate validated only the DTYPE: a hand-planted
    ``2**62``-µs stamp passed it, then ``_count_foreign_rows``'s ``min()``
    raised ``OverflowError`` from OUTSIDE the per-file read try/except —
    propagating out of ``scan_lake_invariants`` entirely (L3 boot sweep
    permanently inert — the sentinel never stamps, every boot re-crashes —
    plus a 500 on every ``/universe`` lake-uniqueness cache miss and a dead
    L2 options census) — and the heal-side ``to_list()`` panicked as the
    BaseException ``pyo3_runtime.PanicException``, escaping every ``except
    Exception`` guard. The gate now value-probes ``min()``/``max()`` (months
    are contiguous — a convertible min + max bounds every value) and the
    bars loop classifies under the COMMON
    ``datetime_out_of_range_errors`` fault set.
    """

    def test_option_out_of_range_file_skipped_scan_survives(self, tmp_path: Path) -> None:
        bad = tmp_path / "options" / "SERIES_BAD" / "2024-06.parquet"
        _plant_option_out_of_range(bad)
        good = tmp_path / "options" / "SERIES_GOOD" / "2024-06.parquet"
        _plant_option_raw(good, [("CE", _ts(1)), ("CE", _ts(1))])
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)  # pre-fix: OverflowError right here
        assert report.option_files_scanned == 1
        assert report.option_files_skipped == 1
        (finding,) = report.findings  # the sibling series' dup is still found
        assert isinstance(finding, OptionIntraFileDup)
        assert finding.path == good
        (skip,) = _events(logs, "lake.invariant.scan_skip")
        assert skip["path"] == repr(str(bad))  # vendor-influenceable series dir name
        error = skip["error"]
        assert isinstance(error, str)
        assert "out of datetime range" in error

    def test_option_out_of_range_canonical_sibling_abstains(self, tmp_path: Path) -> None:
        # The targeted overlap read hits an out-of-range canonical sibling:
        # honest overlap_rows=None abstain — the nested-dtype-sibling twin.
        june = tmp_path / "options" / "SERIES" / "2024-06.parquet"
        _plant_option_raw(june, [("CE", _ts(1)), ("CE", _ts(2, month=7, day=1))])
        july = tmp_path / "options" / "SERIES" / "2024-07.parquet"
        _plant_option_out_of_range(july)
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        (finding,) = report.findings
        assert isinstance(finding, OptionForeignMonthRows)
        assert finding.path == june
        assert finding.overlap_rows is None  # honest abstain
        assert report.option_files_scanned == 1
        assert report.option_files_skipped == 1
        skips = _events(logs, "lake.invariant.scan_skip")
        # one from the main glob loop, one from the targeted sibling read
        assert [s["path"] for s in skips] == [repr(str(july)), repr(str(july))]

    def test_bars_out_of_range_file_skipped_scan_survives(self, tmp_path: Path) -> None:
        # The bars twin: no dtype gate runs on the bars loop, so the
        # classification itself is guarded under the COMMON fault set —
        # pre-fix `classify_bar_keys` → `min()` raised OverflowError out of
        # the whole scan (the identical DoS one tree over).
        _write_bars(tmp_path, [_bar(_ts(1)), _bar(_ts(2))])  # clean June, real writer
        bad = _dest(tmp_path, month=7)
        _plant_bars_out_of_range(bad)
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path, include_options=False)
        assert report.files_scanned == 1  # the clean sibling still scans
        assert report.files_skipped == 1
        assert report.findings == ()
        (skip,) = _events(logs, "lake.invariant.scan_skip")
        assert skip["path"] == str(bad)  # bars path stays raw (catalogued coords)
        assert "date value out of range" in str(skip["error"])

    def test_bars_out_of_range_canonical_sibling_abstains(self, tmp_path: Path) -> None:
        # June carries an in-range July foreign row; the July CANONICAL file
        # holds the out-of-range stamp, so `_canonical_timestamps`' `to_list()`
        # PANICS (pyo3 PanicException, a BaseException) — pre-fix the whole
        # scan died there; now the overlap honestly abstains.
        june = _dest(tmp_path)
        _plant_raw(june, [_ts(1), _ts(2, month=7, day=1)])
        july = _dest(tmp_path, month=7)
        _plant_bars_out_of_range(july)
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path, include_options=False)
        (finding,) = report.findings
        assert isinstance(finding, ForeignMonthRows)
        assert finding.path == june
        assert finding.overlap_rows is None  # honest abstain
        assert report.files_scanned == 1
        assert report.files_skipped == 1  # the July file itself (main loop)
        skips = _events(logs, "lake.invariant.scan_skip")
        # one from the main loop's July classification, one from the
        # targeted canonical-sibling read
        assert [s["path"] for s in skips] == [str(july), str(july)]

    @pytest.mark.parametrize("tz", ["UTC", None])
    def test_option_key_dtype_error_out_of_range_verdict(self, tz: str | None) -> None:
        frame = pl.DataFrame(
            {
                "side": pl.Series("side", ["CE"], dtype=pl.Utf8),
                "timestamp": pl.Series("timestamp", [2**62]).cast(pl.Datetime("us", tz)),
            }
        )
        verdict = option_key_dtype_error(frame)  # pre-fix: None (the dtype IS sound)
        assert verdict is not None
        assert "out of datetime range" in verdict

    def test_option_residual_conversion_fault_isolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defense in depth: a RESIDUAL conversion fault the gate probe cannot
        # foresee is a loud per-FILE skip, never a whole-scan abort.
        dest = tmp_path / "options" / "SERIES" / "2024-06.parquet"
        _plant_option_raw(dest, [("CE", _ts(1))])

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise OverflowError("residual conversion fault")

        monkeypatch.setattr(li, "classify_option_keys", _boom)
        with capture_logs() as logs:
            report = scan_lake_invariants(tmp_path)
        assert report.findings == ()
        assert report.option_files_scanned == 0
        assert report.option_files_skipped == 1
        (skip,) = _events(logs, "lake.invariant.scan_skip")
        assert skip["path"] == repr(str(dest))
        assert "residual conversion fault" in str(skip["error"])


class TestScanIncludeBars:
    def test_include_bars_false_scans_only_options(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bars dup poison + a drift pair + an option dup: with include_bars
        # False only the OPTION finding is emitted; bars counters stay 0.
        _plant_raw(_dest(tmp_path), [_ts(1), _ts(1)])
        _write_bars(tmp_path, [_bar(_ts(2))], segment="CASH", symbol="M&M")
        _write_bars(tmp_path, [_bar(_ts(3))], segment="CASH", symbol="NSE-M&M")
        _plant_option_raw(_opt_dest(tmp_path, "2024-06"), [("CE", _ts(4)), ("CE", _ts(4))])
        calls: list[Path] = []
        real = li._read_key_columns

        def spy(path: Path, columns: Sequence[str]) -> pl.DataFrame:
            calls.append(path)
            return real(path, columns)

        monkeypatch.setattr(li, "_read_key_columns", spy)
        report = scan_lake_invariants(tmp_path, include_bars=False)
        assert [type(f).__name__ for f in report.findings] == ["OptionIntraFileDup"]
        assert report.files_scanned == 0
        assert report.files_skipped == 0
        assert report.series_scanned == 0
        assert report.files_by_segment == ()
        assert report.engine_files == 0
        assert report.option_files_scanned == 1
        assert calls == [_opt_dest(tmp_path, "2024-06")]  # zero bars reads

    def test_include_bars_default_true_unchanged(self, tmp_path: Path) -> None:
        _plant_raw(_dest(tmp_path), [_ts(1), _ts(1)])
        report = scan_lake_invariants(tmp_path)
        assert report.files_scanned == 1
        assert len(report.findings) == 1


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

    def test_census_options_files_count_scanned_only(self, tmp_path: Path) -> None:
        # Round-10 pin (surviving mutant): the OPTIONS row's file accounting
        # counts ONLY genuinely SCANNED option files — a SKIPPED file
        # (unreadable garbage here; a symlinked / nested-dtype file rides the
        # same counter) must inflate neither `files` nor `clean`, and the
        # census-level `files_scanned` fold rides scanned-only too. The
        # `option_files_scanned + option_files_skipped` mutant survived the
        # whole covering set (every census fixture asserting `files` had zero
        # skips; the skip fixtures never asserted `files`).
        good = tmp_path / "options" / "SERIES" / "2024-07.parquet"
        _plant_option_raw(good, [("CE", _ts(1, month=7, day=1))])
        bad = tmp_path / "options" / "SERIES" / "2024-06.parquet"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"garbage - skipped, never scanned")
        report = scan_lake_invariants(tmp_path)
        assert report.option_files_scanned == 1
        assert report.option_files_skipped == 1
        census = build_lake_census(report)
        options = {row.segment: row for row in census.segments}["OPTIONS"]
        assert options.files == 1  # scanned-only — the skipped file not counted
        assert options.clean == 1
        assert census.files_scanned == 1

    def test_census_option_foreign_overlap_union_math(self, tmp_path: Path) -> None:
        # One file with dup AND overlapping-foreign keys is ONE poisoned file
        # (union math — the bars law, now real for OPTIONS); a second series'
        # PURE misplacement is foreign but NOT overlap; a clean file stays clean.
        _plant_option_raw(
            tmp_path / "options" / "A" / "2024-06.parquet", [("CE", _ts(1)), ("PE", _ts(2))]
        )
        _plant_option_raw(
            tmp_path / "options" / "A" / "2024-07.parquet",
            [("CE", _ts(1)), ("CE", _ts(1)), ("PE", _ts(3, month=7, day=1))],
        )
        _plant_option_raw(
            tmp_path / "options" / "B" / "2024-07.parquet",
            [("CE", _ts(4)), ("PE", _ts(5, month=7, day=1))],
        )
        census = build_lake_census(scan_lake_invariants(tmp_path))
        by_segment = {row.segment: row for row in census.segments}
        options = by_segment["OPTIONS"]
        assert options.files == 3
        assert options.dup_files == 1
        assert options.foreign_files == 2
        assert options.overlap_files == 1  # only A's July key overlaps canonical June
        assert options.clean == 1  # A June; the dup+foreign A July file counted ONCE
        assert options.drift_dirs == 0  # honestly N/A for series dirs
        assert census.engine_clean is True

    def test_census_option_overlap_none_abstain_folds_without_raising(self, tmp_path: Path) -> None:
        # Round-6 surviving-mutant kill (refuter-confirmed): the OPTIONS census
        # fold guards `(finding.overlap_rows or 0) > 0` — `overlap_rows=None`
        # is a REACHABLE production state (an unreadable/symlinked canonical
        # sibling makes the scanner abstain honestly) and the census backs
        # `brutex lake audit-unique`, the doctor check and the `/universe`
        # chip. Dropping the `or 0` None-guard survived the full covering
        # suite because every census fixture fed overlap_rows ∈ {0, 1}; under
        # the mutant this fold TypeError-crashes. This pin kills it.
        june = _opt_dest(tmp_path, "2024-06")
        june.parent.mkdir(parents=True, exist_ok=True)
        june.write_bytes(b"not a parquet file at all")
        _plant_option_raw(
            _opt_dest(tmp_path, "2024-07"), [("CE", _ts(1)), ("CE", _ts(3, month=7, day=1))]
        )
        report = scan_lake_invariants(tmp_path)
        foreigns = [f for f in report.findings if isinstance(f, OptionForeignMonthRows)]
        assert [f.overlap_rows for f in foreigns] == [None]  # the abstain fixture holds
        census = build_lake_census(report)  # mutant: TypeError right here
        by_segment = {row.segment: row for row in census.segments}
        options = by_segment["OPTIONS"]
        assert options.foreign_files == 1
        assert options.overlap_files == 0  # an honest abstain never counts as overlap
        assert options.dup_files == 0

    def test_census_bars_overlap_none_abstain_folds_without_raising(self, tmp_path: Path) -> None:
        # The bars twin of the pin above — the pre-existing `or 0` guard on the
        # bars census branch was equally unpinned against overlap_rows=None
        # (an unreadable canonical bars sibling abstains the same way).
        june = _dest(tmp_path)
        june.parent.mkdir(parents=True, exist_ok=True)
        june.write_bytes(b"not a parquet file at all")
        _plant_raw(_dest(tmp_path, month=7), [_ts(1), _ts(2, month=7, day=1)])
        report = scan_lake_invariants(tmp_path)
        foreigns = [f for f in report.findings if isinstance(f, ForeignMonthRows)]
        assert [f.overlap_rows for f in foreigns] == [None]
        census = build_lake_census(report)  # mutant: TypeError right here
        by_segment = {row.segment: row for row in census.segments}
        index = by_segment["INDEX"]
        assert index.foreign_files == 1
        assert index.overlap_files == 0
        assert index.dup_files == 0

    def test_census_options_row_immune_to_bars_segment_named_options(self, tmp_path: Path) -> None:
        """Round-8 NIT regression: the OPTIONS census row folds by finding
        TYPE (``isinstance``), never by the ``"OPTIONS"`` label — a BARS
        finding whose ``coords.segment`` reads ``"OPTIONS"``
        (:class:`PartitionCoords` is unvalidated and census input is any
        :class:`LakeScanReport`, even though ``parse_partition_path`` rejects
        that segment at the scan boundary today) must never cross-contaminate
        the OPTIONS row. Pre-fix the shared label-keyed dicts merged them:
        dup_files read 2, foreign/overlap leaked in, and ``clean`` went
        NEGATIVE (1 − 3 = −2)."""
        rogue = PartitionCoords(
            exchange="NSE",
            segment="OPTIONS",
            symbol="X",
            interval="1minute",
            year=2024,
            month=6,
        )
        bars_dup = IntraFileDup(
            path=tmp_path / "b1.parquet", coords=rogue, physical_rows=2, unique_keys=1
        )
        bars_foreign = ForeignMonthRows(
            path=tmp_path / "b2.parquet", coords=rogue, foreign_rows=1, overlap_rows=1
        )
        opt_dup = OptionIntraFileDup(
            path=tmp_path / "options" / "S" / "2024-06.parquet",
            series="S",
            physical_rows=2,
            unique_keys=1,
        )
        report = LakeScanReport(
            findings=(bars_dup, bars_foreign, opt_dup),
            files_scanned=2,
            files_skipped=0,
            series_scanned=1,
            option_files_scanned=1,
            option_files_skipped=0,
            duration_s=0.0,
            files_by_segment=(("OPTIONS", 2),),
            engine_files=0,
        )
        census = build_lake_census(report)
        by_segment = {row.segment: row for row in census.segments}
        assert set(by_segment) == {"INDEX", "CASH", "FNO", "OPTIONS"}
        options = by_segment["OPTIONS"]
        assert options.files == 1  # option_files_scanned — never the rogue bars count
        assert options.dup_files == 1  # the REAL option dup ONLY (pre-fix: 2)
        assert options.foreign_files == 0  # the rogue bars foreign never leaks in
        assert options.overlap_files == 0
        assert options.clean == 0  # pre-fix: 1 − 3 = −2
        # The rogue-segment bars findings get NO segment row (fixed
        # _BAR_SEGMENTS) but stay honestly counted in the total.
        assert census.findings_total == 3
        assert census.engine_clean is True

    def test_engine_relevant_option_foreign_is_false(self, tmp_path: Path) -> None:
        # The G5 totality pin: without the widened isinstance branch the
        # fall-through reads `.coords` and raises AttributeError.
        finding = OptionForeignMonthRows(
            path=tmp_path / "options" / "S" / "2024-07.parquet",
            series="S",
            year=2024,
            month=7,
            foreign_rows=1,
            overlap_rows=0,
        )
        assert li._engine_relevant(finding) is False

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
        option_foreign = OptionForeignMonthRows(
            path=tmp_path / "options" / "S" / "2024-07.parquet",
            series="S",
            year=2024,
            month=7,
            foreign_rows=1,
            overlap_rows=None,
        )
        assert li._engine_offender(option_foreign) == ("option-foreign options/S/2024-07.parquet")

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
            "alien_files",
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

    @pytest.mark.parametrize("value", [float("inf"), float("nan")], ids=["+inf", "nan"])
    def test_nonfinite_high_row_counts_as_price_finding(self, tmp_path: Path, value: float) -> None:
        """FAILS-BEFORE (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001): the census
        scanner rides ``count_price_violations``, whose pre-fix mask was BLIND
        to ``high ∈ {+inf, NaN}`` — a file whose ONLY defect is a non-finite
        HIGH read a "0 findings" verdict. The strengthened per-column
        ``~is_finite`` arm flows here transitively, so the census/heal sweeps
        now COUNT it (one PriceSanityRows finding, the bad row tallied)."""
        _plant_raw_ohlc(
            _dest(tmp_path),
            [
                (_ts(1), 100.0, 101.0, 99.0, 100.5),  # clean
                (_ts(2), 100.0, value, 99.0, 100.5),  # non-finite HIGH only
            ],
        )
        report = scan_lake_invariants(tmp_path)
        price_findings = [f for f in report.findings if isinstance(f, li.PriceSanityRows)]
        assert len(price_findings) == 1
        assert price_findings[0].bad_rows == 1
        assert price_findings[0].coords == _NIFTY_COORDS

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
