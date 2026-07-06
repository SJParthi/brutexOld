"""``/db`` pager × lake STATS-manifest read integration (DEC-LAKE-STATS-MANIFEST-001).

Stage-3 reader pins: when a request opts in (``use_stats_manifest=True`` — the
production routes do) the candle pagers serve the TOTAL / SPAN from the
dataset's VALIDATED stats shard and PRUNE the page query's ``read_parquet``
binding to only the files the page needs — with the anti-illusion law that ANY
validation / mid-request failure re-runs the WHOLE request on the byte-identical
legacy path and reseeds the shard best-effort. Pinned here:

* BYTE-EQUALITY — manifest-served payloads equal the legacy payloads
  cell-for-cell across bounds (none / covering / straddling incl. the 1day
  IST-vs-UTC month skew / empty-range) × direction × offsets at/over file
  boundaries and beyond the total, for spot bars, an F&O dataset AND the
  expired-options pager;
* the FALLBACK matrix — deleted file / touched file / corrupt shard /
  out-of-band new file / month-bounds poison each run the LIVE scan with a
  byte-identical payload + one ``lake.stats_manifest.fallback`` clause;
* the read-side SEED — first view of a legacy dataset rebuilds the shard
  (write-behind), the next click is manifest-served (list-bound, not glob);
  every seed-abort clause is individually exercised;
* the TOCTOU close — a file mutated between validation and the pruned read is
  caught by the post-query re-stat → whole-request legacy rerun;
* the DEGRADE law — a stat-clean-but-corrupt file fails the pruned read and
  the WHOLE request (total AND page) re-runs on the legacy resilient path;
* the KILL SWITCH — ``BRUTEX_LAKE_STATS_MANIFEST=off`` makes the opted-in
  reader byte-identical to legacy (same SQL trace, no shard, no seed);
* ``count_cache=None`` purity + generation-bump revalidation.

Whitebox per the DEC-P0-017 convention (drives the private helpers directly
for the fail-closed branches), so ``reportPrivateUsage`` is silenced.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb", reason="the /db pager reads a real Parquet lake")
pytest.importorskip("polars", reason="the lake writers render via polars")
pytest.importorskip("pyarrow", reason="poison-plant fixtures write raw parquet")

import duckdb
from brutex.dashboard import db_view
from brutex.dashboard.db_view import (
    _seed_stats_manifest,
    _stats_file_verdict,
    _stats_fingerprint,
    _stats_pruned_page,
    _stats_read_context,
    _stats_star_columns,
    _stats_utc_ts,
    _StatsReadContext,
    query_candles,
    query_option_candles,
)
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data import lake_stats_manifest as lsm
from brutex.data.errors import LakeWriteHalt
from brutex.data.models import CandleBar, OptionBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate
from brutex.data.parquet_writer import ParquetWriter
from structlog.testing import capture_logs

#: The one spot dataset most tests write into.
_COORDS: dict[str, str] = {
    "exchange": "NSE",
    "segment": "CASH",
    "symbol": "NSE-NIFTY",
    "interval": "1minute",
}

_OPT_PK = "NSE_FNO|13|WEEK|0|ATM|CALL"
_OPT_SERIES = "NSE_FNO__13__WEEK__0__ATM__CALL"


def _bar(ts: datetime, *, close: str = "100.00", volume: int = 5) -> CandleBar:
    price = Decimal(close)
    return CandleBar(
        timestamp=ts,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
        open_interest=None,
    )


def _write_months(
    root: Path,
    months: Sequence[tuple[int, int]] = ((2024, 1), (2024, 2), (2024, 3)),
    *,
    n_bars: int = 20,
    coords: Mapping[str, str] = _COORDS,
) -> None:
    """Write *n_bars* distinct-minute bars per (year, month) via the real writer.

    Bars spread over days 5–8 of the month (``5 + minute % 4``) so a mid-month
    date bound genuinely CUTS through a file — the straddler case is real, not
    a degenerate single-day partition.
    """
    writer = ParquetWriter(lake_root=root)
    for year, month in months:
        bars = [
            _bar(
                datetime(year, month, 5 + minute % 4, 4, minute, tzinfo=UTC),
                close=f"{100 + minute}.00",
            )
            for minute in range(n_bars)
        ]
        writer.write(bars=bars, **coords)


def _write_daily(root: Path, ist_days: Sequence[date], *, coords: Mapping[str, str]) -> None:
    """Write 1day bars stamped midnight-IST (= 18:30 UTC of the PRIOR day).

    An IST month-opening day therefore lands in the PRIOR UTC month's partition
    — the exact ``1day`` IST/UTC skew the straddler classifier must scan.
    """
    writer = ParquetWriter(lake_root=root)
    by_month: dict[tuple[int, int], list[CandleBar]] = {}
    for day in ist_days:
        ts = datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(hours=5, minutes=30)
        by_month.setdefault((ts.year, ts.month), []).append(_bar(ts, close="200.00"))
    for bars in by_month.values():
        writer.write(bars=bars, **coords)


def _obar(ts: datetime, side: str) -> OptionBar:
    return OptionBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=250,
        open_interest=1200,
        side=side,
        strike=Decimal("17500.00"),
        spot=Decimal("17480.55"),
        iv=Decimal("0.1523"),
    )


def _write_option_months(
    root: Path, months: Sequence[tuple[int, int]] = ((2024, 3), (2024, 4)), *, n_bars: int = 6
) -> None:
    """Write CE+PE bar pairs per month for the fixed series via the real writer."""
    seg, sec, flag, code, offset, _typ = _OPT_PK.split("|")
    coord = OptionSeriesCoordinate(
        exch_id="NSE",
        exchange_segment=seg,
        security_id=int(sec),
        underlying_symbol="NIFTY",
        expiry_flag=flag,
        expiry_code=int(code),
        strike_offset=offset,
    )
    writer = OptionParquetWriter(lake_root=root)
    for year, month in months:
        bars: list[OptionBar] = []
        for minute in range(n_bars):
            ts = datetime(year, month, 1, 4, minute, tzinfo=UTC)
            bars.extend((_obar(ts, "CE"), _obar(ts, "PE")))
        writer.write(partition_key=_OPT_PK, coordinate=coord, bars=bars)


def _q(conn: Any, root: Path, *, use: bool = False, **kw: Any) -> dict[str, Any]:
    """Typed ``query_candles`` wrapper over the fixed spot dataset."""
    params: dict[str, Any] = {"limit": 10, **kw}
    return query_candles(conn, root, use_stats_manifest=use, **_COORDS, **params)


def _qopt(conn: Any, root: Path, *, use: bool = False, **kw: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": 10, **kw}
    return query_option_candles(conn, root, series=_OPT_SERIES, use_stats_manifest=use, **params)


def _shard_path(root: Path) -> Path:
    return lsm.stats_shard_path(root, lsm.KIND_BARS, _COORDS)


def _seed_by_reading(conn: Any, root: Path) -> None:
    """First manifest read → fallback(absent) + write-behind seed."""
    _q(conn, root, use=True)
    assert _shard_path(root).exists(), "the read-side seed must have written the shard"


def _fallback_clauses(logs: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(entry["clause"]) for entry in logs if entry["event"] == "lake.stats_manifest.fallback"
    ]


def _seed_skip_reasons(logs: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(entry["reason"]) for entry in logs if entry["event"] == "db.stats_manifest.seed_skipped"
    ]


class _RecordingConn:
    """Delegates to a real conn; records the bound SOURCE of read_parquet SQL.

    The load-bearing observability: a LEGACY read binds the ``**/*.parquet``
    GLOB string, a manifest-PRUNED read binds an explicit file LIST — so the
    recorded source types prove which path served a request.
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        self.sources: list[tuple[str, Any]] = []

    def execute(self, sql: str, parameters: Any = None) -> Any:
        if parameters is not None and "read_parquet" in sql:
            self.sources.append((sql, parameters[0]))
        if parameters is None:
            return self._real.execute(sql)
        return self._real.execute(sql, parameters)

    def list_sources(self) -> list[list[str]]:
        return [src for _sql, src in self.sources if isinstance(src, list)]

    def glob_sources(self) -> list[str]:
        return [src for _sql, src in self.sources if isinstance(src, str)]

    def page_sources(self) -> list[Any]:
        """The PAGE query's bound sources (list ⇒ pruned, glob str ⇒ legacy).

        The %-change baseline reads (deliberately untouched this round) always
        bind globs over the 1-minute/1-day partitions, so page-path assertions
        must target the paged SQL — never the whole trace.
        """
        return [src for sql, src in self.sources if "LIMIT ? OFFSET ?" in sql]

    def legacy_count_ran(self) -> bool:
        """Whether the legacy whole-glob COUNT(DISTINCT …) total executed."""
        return any("COUNT(DISTINCT" in sql and isinstance(src, str) for sql, src in self.sources)

    def legacy_span_ran(self) -> bool:
        """Whether the legacy whole-glob MIN/MAX stored-life span executed."""
        return any(
            sql.lstrip().startswith("SELECT MIN(") and isinstance(src, str)
            for sql, src in self.sources
        )


class _MutatingConn:
    """Fires a one-shot side effect around a matching execute (TOCTOU rigs).

    ``fire_after=False`` mutates just BEFORE delegating (the mutation is
    visible to the query itself); ``fire_after=True`` mutates AFTER the query
    ran (visible only to the post-scan re-stat — duckdb executes eagerly, so
    the returned cursor is already materialised).
    """

    def __init__(
        self,
        real: Any,
        *,
        trigger: Callable[[str, Any], bool],
        side_effect: Callable[[], None],
        fire_after: bool = False,
    ) -> None:
        self._real = real
        self._trigger = trigger
        self._side_effect = side_effect
        self._fire_after = fire_after
        self.fired = False

    def execute(self, sql: str, parameters: Any = None) -> Any:
        matched = not self.fired and self._trigger(sql, parameters)
        if matched and not self._fire_after:
            self.fired = True
            self._side_effect()
        cursor = (
            self._real.execute(sql) if parameters is None else self._real.execute(sql, parameters)
        )
        if matched and self._fire_after:
            self.fired = True
            self._side_effect()
        return cursor


def _pruned_page_trigger(sql: str, parameters: Any) -> bool:
    """Match ONLY the manifest-pruned page query (list-bound + paged)."""
    return parameters is not None and isinstance(parameters[0], list) and "LIMIT ? OFFSET ?" in sql


def _touch(path: Path) -> None:
    """Move a file's stat token WITHOUT changing its bytes (mtime_ns bump)."""
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def _corrupt(path: Path) -> None:
    """Truncate a parquet file to garbage (size + mtime both move).

    Realistic detectable corruption: a SAME-SIZE overwrite is not only the
    documented stat-token residual — it is ALSO invisible to DuckDB's own
    size-keyed parquet-footer cache (measured: a same-size corpse can serve a
    stale COUNT from the cached footer on the same connection), so no reader
    — legacy or manifest — can degrade it gracefully. A truncation is caught
    by every layer (stat token, footer probe, read).
    """
    path.write_bytes(b"XNOTPARQUET")


def _straddler_count_trigger(sql: str, parameters: Any) -> bool:
    """Match ONLY the manifest straddler bounded-count query (list-bound COUNT)."""
    return (
        parameters is not None
        and isinstance(parameters[0], list)
        and "COUNT(DISTINCT" in sql
        and "GROUP BY" not in sql
        and "LIMIT" not in sql
    )


def _plant_extra_column_month(root: Path, year: int, month: int, *, n_rows: int = 5) -> Path:
    """Raw-pyarrow plant of a month partition carrying an EXTRA column.

    Schema = the writer's 7 columns + ``extra`` (Float64) — the heterogeneous
    star-union case: a ``SELECT *`` over the full glob unions ``extra`` in,
    so a pruned sublist must project it explicitly (or fall back).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest = (
        root
        / "bars"
        / _COORDS["exchange"]
        / _COORDS["segment"]
        / _COORDS["symbol"]
        / _COORDS["interval"]
        / f"{year:04d}"
        / f"{month:02d}.parquet"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    stamps = [datetime(year, month, 7, 4, minute, tzinfo=UTC) for minute in range(n_rows)]
    prices = [150.0 + i for i in range(n_rows)]
    table = pa.table(
        {
            "timestamp": pa.array(stamps, type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([10] * n_rows, type=pa.int64()),
            "open_interest": pa.array([None] * n_rows, type=pa.int64()),
            "extra": pa.array([1.5 * i for i in range(n_rows)], type=pa.float64()),
        }
    )
    pq.write_table(table, dest)
    return dest


@pytest.fixture
def conn() -> Any:
    """A real in-process DuckDB connection."""
    return duckdb.connect()


# ── byte-equality: manifest-served == legacy, cell for cell ─────────────────────────


class TestManifestPageByteEquality:
    """Every manifest-served payload equals the legacy payload exactly."""

    #: (start, end) bound shapes: none / fully covering / cutting THROUGH the
    #: middle AND last months (both straddle) / an open start cutting month 1 /
    #: an EMPTY matched range.
    _BOUNDS: tuple[tuple[str | None, str | None], ...] = (
        (None, None),
        ("2024-01-01", "2024-03-31"),
        ("2024-02-06", "2024-03-06"),
        (None, "2024-01-06"),
        ("2025-01-01", "2025-12-31"),
    )
    #: Offsets at zero / inside a file / exactly at a 20-row file boundary /
    #: crossing files / beyond the 60-row total.
    _OFFSETS: tuple[int, ...] = (0, 7, 20, 35, 61)

    def test_spot_matrix_cell_for_cell(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path)
        _seed_by_reading(conn, tmp_path)
        for start, end in self._BOUNDS:
            for descending in (False, True):
                for offset in self._OFFSETS:
                    kw: dict[str, Any] = {
                        "start": start,
                        "end": end,
                        "descending": descending,
                        "offset": offset,
                        "limit": 8,
                    }
                    legacy = _q(conn, tmp_path, use=False, **kw)
                    rec = _RecordingConn(conn)
                    served = _q(rec, tmp_path, use=True, **kw)
                    assert served == legacy, f"divergence at {kw}"
                    assert all(isinstance(src, list) for src in rec.page_sources()), (
                        f"a manifest-served page must bind a file LIST, never the glob: {kw}"
                    )
                    assert not rec.legacy_count_ran(), (
                        f"a manifest-served total must not re-run the glob COUNT: {kw}"
                    )

    def test_spot_explicit_columns_equality(self, conn: Any, tmp_path: Path) -> None:
        """An explicit projection pages identically (no star DESCRIBE needed)."""
        _write_months(tmp_path)
        _seed_by_reading(conn, tmp_path)
        kw: dict[str, Any] = {
            "columns": ("timestamp", "close"),
            "offset": 18,
            "limit": 5,
            "descending": True,
        }
        legacy = _q(conn, tmp_path, use=False, **kw)
        rec = _RecordingConn(conn)
        served = _q(rec, tmp_path, use=True, **kw)
        assert served == legacy
        assert all(isinstance(src, list) for src in rec.page_sources())
        assert not rec.legacy_count_ran()
        assert not any("DESCRIBE" in sql for sql, _src in rec.sources)

    def test_page_prunes_to_only_the_needed_files(self, conn: Any, tmp_path: Path) -> None:
        """The pruned page binds ONLY the files the offset window needs (O(page))."""
        _write_months(tmp_path)  # 3 months × 20 rows
        _seed_by_reading(conn, tmp_path)
        rec = _RecordingConn(conn)
        page = _q(rec, tmp_path, use=True, offset=5, limit=5, descending=True)
        assert page["total"] == 60
        (page_files,) = rec.page_sources()  # exactly ONE page read ran, list-bound
        assert [Path(f).parent.name + "/" + Path(f).name for f in page_files] == [
            "2024/03.parquet"
        ], "a DESC offset-5 limit-5 page lives wholly in the newest month file"

    def test_daily_ist_skew_straddler_equality(self, conn: Any, tmp_path: Path) -> None:
        """1day bounds classify on the IST calendar — the 18:30-UTC skew is exact.

        The IST month-opening day (2024-02-01) is STORED in the January UTC
        partition (18:30 UTC Jan 31), so an IST February filter must scan the
        January file as a straddler — never trust its UTC month blindly.
        """
        coords = {**_COORDS, "interval": "1day"}
        days = [date(2024, 1, d) for d in (29, 30, 31)] + [date(2024, 2, d) for d in (1, 2, 5)]
        _write_daily(tmp_path, days, coords=coords)
        kw: dict[str, Any] = {"limit": 10}

        def _daily(c: Any, *, use: bool, **extra: Any) -> dict[str, Any]:
            return query_candles(c, tmp_path, use_stats_manifest=use, **coords, **kw, **extra)

        _daily(conn, use=True)  # seed
        assert lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, coords).exists()
        for start, end in (("2024-02-01", "2024-02-05"), (None, "2024-01-31")):
            legacy = _daily(conn, use=False, start=start, end=end)
            served = _daily(conn, use=True, start=start, end=end)
            assert served == legacy, f"1day skew divergence at {(start, end)}"
        # The February IST filter really does need the January UTC file.
        rec = _RecordingConn(conn)
        served = _daily(rec, use=True, start="2024-02-01", end="2024-02-05")
        assert served["total"] == 3
        assert any(any(f.endswith("2024/01.parquet") for f in src) for src in rec.list_sources()), (
            "the IST-skew straddler (January UTC file) must be scanned, never guessed"
        )

    def test_fno_dataset_span_and_page_equality(self, conn: Any, tmp_path: Path) -> None:
        """An F&O dataset (blank bounds ⇒ stored-life span) folds the span
        from the validated manifest — identical payload, zero span scan."""
        coords: dict[str, Any] = {**_COORDS, "segment": "FNO", "symbol": "NSE-NIFTY-30Jan25-FUT"}
        _write_months(tmp_path, ((2024, 12), (2025, 1)), coords=coords, n_bars=6)

        def _fno(c: Any, *, use: bool) -> dict[str, Any]:
            return query_candles(c, tmp_path, use_stats_manifest=use, **coords, limit=4, offset=2)

        _fno(conn, use=True)  # seed
        legacy = _fno(conn, use=False)
        rec = _RecordingConn(conn)
        served = _fno(rec, use=True)
        assert served == legacy
        assert served["applied_range"] == {"start": "2024-12-05", "end": "2025-01-08"}
        assert all(isinstance(src, list) for src in rec.page_sources())
        assert not rec.legacy_count_ran()
        assert not rec.legacy_span_ran(), "the stored-life span must fold from the manifest"

    def test_options_matrix_cell_for_cell(self, conn: Any, tmp_path: Path) -> None:
        _write_option_months(tmp_path)
        _qopt(conn, tmp_path, use=True)  # seed
        assert lsm.stats_shard_path(tmp_path, lsm.KIND_OPTIONS, {"series": _OPT_SERIES}).exists()
        for start, end in ((None, None), ("2024-03-01", "2024-04-01"), ("2024-04-01", None)):
            for descending in (False, True):
                for offset in (0, 5, 12, 30):
                    kw: dict[str, Any] = {
                        "start": start,
                        "end": end,
                        "descending": descending,
                        "offset": offset,
                        "limit": 7,
                    }
                    legacy = _qopt(conn, tmp_path, use=False, **kw)
                    rec = _RecordingConn(conn)
                    served = _qopt(rec, tmp_path, use=True, **kw)
                    assert served == legacy, f"options divergence at {kw}"
                    assert not rec.glob_sources(), (
                        "an options manifest-served request binds no glob at all "
                        f"(no baseline reads exist on this pager): {kw}"
                    )

    def test_heterogeneous_star_union_served_or_falls_back(self, conn: Any, tmp_path: Path) -> None:
        """Mixed schemas under ``SELECT *``: served pages carry the FULL union
        column order; a sublist that cannot bind the union falls back — either
        way the payload equals legacy, never a shrunken column set."""
        _write_months(tmp_path, ((2024, 1),), n_bars=4)
        _plant_extra_column_month(tmp_path, 2024, 2, n_rows=5)
        _seed_by_reading(conn, tmp_path)
        # DESC page 0 lives in the extra-carrying February file → manifest-served.
        legacy_desc = _q(conn, tmp_path, use=False, descending=True, limit=3)
        rec = _RecordingConn(conn)
        served_desc = _q(rec, tmp_path, use=True, descending=True, limit=3)
        assert served_desc == legacy_desc
        assert "extra" in served_desc["columns"]
        assert rec.list_sources(), "the extra-column page must still be pruned-served"
        # ASC page 0 lives in the extra-LESS January file → the explicit union
        # projection cannot bind over that sublist → honest fallback to legacy.
        legacy_asc = _q(conn, tmp_path, use=False, descending=False, limit=3)
        with capture_logs() as logs:
            served_asc = _q(conn, tmp_path, use=True, descending=False, limit=3)
        assert served_asc == legacy_asc
        assert "page_read" in _fallback_clauses(logs)


# ── kill switch + purity + generation ───────────────────────────────────────────────


class TestKillSwitchAndPurity:
    def test_kill_switch_is_byte_identical_legacy(
        self, conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env off ⇒ the opted-in reader is byte-identical legacy: same SQL
        trace (glob-bound), no shard read/seeded, no fallback logged."""
        _write_months(tmp_path)
        legacy_rec = _RecordingConn(conn)
        legacy = _q(legacy_rec, tmp_path, use=False, offset=3, limit=5)
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")
        killed_rec = _RecordingConn(conn)
        with capture_logs() as logs:
            killed = _q(killed_rec, tmp_path, use=True, offset=3, limit=5)
        assert killed == legacy
        assert killed_rec.sources == legacy_rec.sources, "SQL trace must be identical"
        assert not _shard_path(tmp_path).exists(), "no seed may run under the kill switch"
        assert _fallback_clauses(logs) == []
        assert _seed_skip_reasons(logs) == []

    def test_count_cache_none_stays_pure_and_equal(self, conn: Any, tmp_path: Path) -> None:
        """No injected cache: the star DESCRIBE runs inline, output identical."""
        _write_months(tmp_path)
        _seed_by_reading(conn, tmp_path)
        legacy = _q(conn, tmp_path, use=False, offset=25, limit=5)
        rec = _RecordingConn(conn)
        served = _q(rec, tmp_path, use=True, offset=25, limit=5)
        assert served == legacy
        describes = [sql for sql, _src in rec.sources if "DESCRIBE" in sql]
        assert len(describes) == 1, "count_cache=None runs the star DESCRIBE inline"

    def test_star_describe_memoised_behind_count_cache(self, conn: Any, tmp_path: Path) -> None:
        """With the injected cache, the star union order is described ONCE."""
        _write_months(tmp_path)
        _seed_by_reading(conn, tmp_path)
        cache = MoversSnapshotCache()
        rec = _RecordingConn(conn)
        for offset in (0, 8, 16):
            _q(rec, tmp_path, use=True, offset=offset, limit=5, count_cache=cache, generation=3)
        describes = [sql for sql, _src in rec.sources if "DESCRIBE" in sql]
        assert len(describes) == 1, "repeat clicks must reuse the memoised union order"

    def test_generation_bump_reflects_new_writes(self, conn: Any, tmp_path: Path) -> None:
        """A post-seed write updates the shard (writer hook) and a generation
        bump recomputes the cached total from it — never a stale number."""
        _write_months(tmp_path, ((2024, 1),), n_bars=4)
        _seed_by_reading(conn, tmp_path)
        cache = MoversSnapshotCache()
        first = _q(conn, tmp_path, use=True, count_cache=cache, generation=1)
        assert first["total"] == 4
        _write_months(tmp_path, ((2024, 1),), n_bars=9)  # 5 net-new bars, shard upserted
        bumped = _q(conn, tmp_path, use=True, count_cache=cache, generation=2)
        assert bumped["total"] == 9, "the bumped generation must serve the fresh total"
        legacy = _q(conn, tmp_path, use=False)
        assert bumped["total"] == legacy["total"]


# ── fallback matrix: every invalidity runs the LIVE scan byte-identically ──────────


class TestFallbackMatrix:
    """Each invalidity clause ⇒ legacy live scan + byte-identical payload + reseed."""

    def _assert_fallback(
        self,
        conn: Any,
        root: Path,
        *,
        expect_clause: str,
        expect_reseed: bool = True,
        **kw: Any,
    ) -> None:
        legacy = _q(conn, root, use=False, **kw)
        rec = _RecordingConn(conn)
        with capture_logs() as logs:
            served = _q(rec, root, use=True, **kw)
        assert served == legacy, "a fallback request must serve the legacy payload"
        assert expect_clause in _fallback_clauses(logs)
        assert isinstance(rec.page_sources()[0], str), "the legacy page binds the glob"
        if expect_reseed:
            stats = lsm.load_valid_dataset_stats(root, kind=lsm.KIND_BARS, key=_COORDS)
            assert stats is not None, "the read side must have reseeded a valid shard"

    def test_deleted_file_falls_back_and_reseeds(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path)
        _seed_by_reading(conn, tmp_path)
        dataset = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS)
        (dataset / "2024" / "02.parquet").unlink()
        self._assert_fallback(conn, tmp_path, expect_clause="file_set")

    def test_touched_file_falls_back_and_reseeds(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path)
        _seed_by_reading(conn, tmp_path)
        dataset = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS)
        _touch(dataset / "2024" / "01.parquet")
        self._assert_fallback(conn, tmp_path, expect_clause="stat_token")

    def test_corrupt_shard_falls_back_and_reseeds(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path)
        _seed_by_reading(conn, tmp_path)
        _shard_path(tmp_path).write_text("{corrupt", encoding="utf-8")
        self._assert_fallback(conn, tmp_path, expect_clause="parse")

    def test_out_of_band_new_file_falls_back(self, conn: Any, tmp_path: Path) -> None:
        """A hand-copied month appears out of band ⇒ set mismatch ⇒ legacy + reseed."""
        _write_months(tmp_path, ((2024, 1), (2024, 2)))
        _seed_by_reading(conn, tmp_path)
        src = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS) / "2024" / "01.parquet"
        dest = src.parent / "03.parquet"
        dest.write_bytes(src.read_bytes())  # a cp of January masquerading as March
        legacy = _q(conn, tmp_path, use=False)
        with capture_logs() as logs:
            served = _q(conn, tmp_path, use=True)
        assert served == legacy
        assert "file_set" in _fallback_clauses(logs)
        # The reseed measured the copied file's REAL (January) bounds, which lie
        # outside its 2024/03 rel month — the seed factory REJECTS the shard, so
        # the poisoned dataset simply stays legacy-served (never a wrong count).
        assert "month_bounds" in " ".join(
            _seed_skip_reasons(logs)
        ) or not lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=_COORDS)

    def test_month_bounds_poisoned_shard_falls_back(self, conn: Any, tmp_path: Path) -> None:
        """A shard stamped with bounds OUTSIDE a file's rel month self-disables."""
        _write_months(tmp_path, ((2024, 1),))
        dataset = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS)
        stat = os.stat(dataset / "2024" / "01.parquet")
        poisoned = lsm.build_dataset_stats(
            kind=lsm.KIND_BARS,
            key=_COORDS,
            files=[
                lsm.StatsFileRow(
                    rel="2024/01.parquet",
                    rows=20,
                    min_ts=datetime(2024, 1, 5, 4, 0, tzinfo=UTC),
                    max_ts=datetime(2024, 1, 8, 4, 19, tzinfo=UTC),
                    mtime_ns=stat.st_mtime_ns,
                    size=stat.st_size,
                    raw_hash=None,
                )
            ],
        )
        shard = _shard_path(tmp_path)
        shard.parent.mkdir(parents=True, exist_ok=True)
        # Rewrite the shard JSON with a max_ts pushed into February — the
        # month-bounds clause must catch the poison at READ time even though
        # every stat token still matches.
        body = lsm.render_shard(poisoned).replace(
            "2024-01-08T04:19:00+00:00", "2024-02-02T04:19:00+00:00"
        )
        shard.write_text(body, encoding="utf-8")
        self._assert_fallback(conn, tmp_path, expect_clause="month_bounds")

    def test_stray_depth_file_falls_back_never_diverges(self, conn: Any, tmp_path: Path) -> None:
        """A stray non-month parquet (read by the legacy ``**`` glob) disables the
        manifest for the dataset — fallback + seed-reject, never a silent
        divergence between manifest counts and the legacy scan.

        The stray carries five UNIQUE June timestamps, so the legacy total is
        25 — a shard that ignored the stray would have said 20. The any-depth
        file-set clause + the seed's month-grammar reject keep the dataset
        honestly legacy-served instead.
        """
        import pyarrow as pa
        import pyarrow.parquet as pq

        _write_months(tmp_path, ((2024, 1),))
        dataset = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS)
        stamps = [datetime(2024, 6, 3, 4, minute, tzinfo=UTC) for minute in range(5)]
        prices = [90.0] * 5
        pq.write_table(
            pa.table(
                {
                    "timestamp": pa.array(stamps, type=pa.timestamp("us", tz="UTC")),
                    "open": pa.array(prices, type=pa.float64()),
                    "high": pa.array(prices, type=pa.float64()),
                    "low": pa.array(prices, type=pa.float64()),
                    "close": pa.array(prices, type=pa.float64()),
                    "volume": pa.array([1] * 5, type=pa.int64()),
                    "open_interest": pa.array([None] * 5, type=pa.int64()),
                }
            ),
            dataset / "stray.parquet",
        )
        legacy = _q(conn, tmp_path, use=False)
        with capture_logs() as logs:
            served = _q(conn, tmp_path, use=True)
        assert served == legacy
        assert served["total"] == legacy["total"] == 25  # the stray IS counted by both
        assert "absent" in _fallback_clauses(logs)
        assert "non_month_file" in _seed_skip_reasons(logs)
        assert not _shard_path(tmp_path).exists(), "no shard may exist for a stray dataset"


# ── TOCTOU close + whole-request degrade law ────────────────────────────────────────


class TestToctouAndDegradeLaw:
    def test_file_mutated_between_validate_and_read_falls_back(
        self, conn: Any, tmp_path: Path
    ) -> None:
        """The post-query re-stat catches a token that moved mid-request."""
        _write_months(tmp_path)
        _seed_by_reading(conn, tmp_path)
        dataset = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS)
        legacy = _q(conn, tmp_path, use=False, offset=3, limit=5)
        rig = _MutatingConn(
            conn,
            trigger=_pruned_page_trigger,
            side_effect=lambda: _touch(dataset / "2024" / "01.parquet"),
        )
        with capture_logs() as logs:
            served = _q(rig, tmp_path, use=True, offset=3, limit=5)
        assert rig.fired, "the rig must have mutated the file mid-request"
        assert served == legacy
        assert "stat_token_moved" in _fallback_clauses(logs)

    def test_corrupt_file_mid_request_degrades_whole_request(
        self, conn: Any, tmp_path: Path
    ) -> None:
        """A file corrupted AFTER validation fails the pruned read ⇒ the WHOLE
        request (total AND page) re-runs on the legacy resilient path — the
        manifest total is never paired with the degraded page.

        The corruption fires mid-request (validation already passed with clean
        tokens), so the pruned read is the FIRST thing to see it; the legacy
        rerun then probe-skips the corrupt file and both the served total and
        page come from the readable subset — never a half-manifest number.
        (An mtime-PRESERVING corruption is the documented stat-undetectable
        residual: it also poisons DuckDB's own mtime-keyed metadata cache, so
        even the pure legacy resilient path re-raises on it — realistic
        corruption moves the mtime, as here.)
        """
        _write_months(tmp_path)
        _seed_by_reading(conn, tmp_path)
        dataset = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS)
        rig = _MutatingConn(
            conn,
            trigger=_pruned_page_trigger,
            side_effect=lambda: _corrupt(dataset / "2024" / "03.parquet"),
        )
        with capture_logs() as logs:
            served = _q(rig, tmp_path, use=True, descending=True, limit=5)
        assert rig.fired
        legacy = _q(conn, tmp_path, use=False, descending=True, limit=5)
        assert served == legacy
        assert served["total"] == 40, "the degraded total must count ONLY the readable files"
        assert "page_read" in _fallback_clauses(logs)

    def test_corrupt_straddler_demotes_whole_request(self, conn: Any, tmp_path: Path) -> None:
        """A straddler whose bounded count fails ⇒ served=False ⇒ full legacy."""
        _write_months(tmp_path)
        _seed_by_reading(conn, tmp_path)
        dataset = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS)
        kw: dict[str, Any] = {"start": "2024-02-06", "end": "2024-03-06", "limit": 8}
        rig = _MutatingConn(
            conn,
            trigger=_straddler_count_trigger,
            side_effect=lambda: _corrupt(dataset / "2024" / "02.parquet"),
        )
        with capture_logs() as logs:
            served = _q(rig, tmp_path, use=True, **kw)
        assert rig.fired
        legacy = _q(conn, tmp_path, use=False, **kw)
        assert served == legacy
        assert "straddler_read" in _fallback_clauses(logs)

    def test_options_degrade_law(self, conn: Any, tmp_path: Path) -> None:
        """The options twin: a mid-request corruption ⇒ whole-request legacy."""
        _write_option_months(tmp_path)
        _qopt(conn, tmp_path, use=True)  # seed
        series_dir = tmp_path / "options" / _OPT_SERIES
        rig = _MutatingConn(
            conn,
            trigger=_pruned_page_trigger,
            side_effect=lambda: _corrupt(series_dir / "2024-04.parquet"),
        )
        with capture_logs() as logs:
            served = _qopt(rig, tmp_path, use=True, descending=True, limit=5)
        assert rig.fired
        legacy = _qopt(conn, tmp_path, use=False, descending=True, limit=5)
        assert served == legacy
        assert served["total"] == 12, "the degraded total counts only the readable month"
        assert "page_read" in _fallback_clauses(logs)

    def test_options_touched_file_falls_back_and_reseeds(self, conn: Any, tmp_path: Path) -> None:
        _write_option_months(tmp_path)
        _qopt(conn, tmp_path, use=True)  # seed
        _touch(tmp_path / "options" / _OPT_SERIES / "2024-03.parquet")
        legacy = _qopt(conn, tmp_path, use=False, offset=2, limit=4)
        with capture_logs() as logs:
            served = _qopt(conn, tmp_path, use=True, offset=2, limit=4)
        assert served == legacy
        assert "stat_token" in _fallback_clauses(logs)
        assert lsm.load_valid_dataset_stats(
            tmp_path, kind=lsm.KIND_OPTIONS, key={"series": _OPT_SERIES}
        ), "the options shard must have been reseeded"


# ── read-side seed: every abort clause ──────────────────────────────────────────────


class _CannedCursor:
    def __init__(self, rows: list[tuple[Any, ...]], description: Any) -> None:
        self._rows = rows
        self.description = description

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _CannedConn:
    """Returns a canned cursor (or raises) for every execute — fail-shape rigs."""

    def __init__(
        self,
        rows: list[tuple[Any, ...]] | None = None,
        *,
        description: Any = (("c",),),
        exc: Exception | None = None,
        fetchone_none: bool = False,
    ) -> None:
        self._rows = rows if rows is not None else []
        self._description = description
        self._exc = exc
        self._fetchone_none = fetchone_none

    def execute(self, sql: str, parameters: Any = None) -> Any:
        if self._exc is not None:
            raise self._exc
        cursor = _CannedCursor(self._rows, self._description)
        if self._fetchone_none:
            cursor.fetchone = lambda: None  # type: ignore[method-assign]
        return cursor


class TestSeedAborts:
    """Every seed anomaly ABORTS (logged reason) — never a wrong shard."""

    def _seed(self, conn: Any, root: Path) -> bool:
        return _seed_stats_manifest(
            conn,
            root,
            kind=lsm.KIND_BARS,
            key=_COORDS,
            base=lsm.dataset_dir(root, lsm.KIND_BARS, _COORDS),
            identity=("timestamp",),
        )

    def test_scan_oserror_aborts(
        self, conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_months(tmp_path, ((2024, 1),))

        def _boom(*args: Any, **kwargs: Any) -> set[str]:
            raise PermissionError("walk denied")

        monkeypatch.setattr(db_view, "scan_dataset_rels", _boom)
        with capture_logs() as logs:
            assert self._seed(conn, tmp_path) is False
        assert _seed_skip_reasons(logs) == ["scan_failed"]

    def test_empty_dataset_aborts(self, conn: Any, tmp_path: Path) -> None:
        with capture_logs() as logs:
            assert self._seed(conn, tmp_path) is False
        assert _seed_skip_reasons(logs) == ["no_files"]

    def test_vanished_file_prestat_aborts(
        self, conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rel reported by the scan but ALREADY gone at stat time aborts."""
        monkeypatch.setattr(db_view, "scan_dataset_rels", lambda *a, **kw: {"2024/01.parquet"})
        with capture_logs() as logs:
            assert self._seed(conn, tmp_path) is False
        assert _seed_skip_reasons(logs) == ["stat_failed"]

    def test_unreadable_file_aborts_scan(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path, ((2024, 1), (2024, 2)))
        _corrupt(lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS) / "2024" / "02.parquet")
        with capture_logs() as logs:
            assert self._seed(conn, tmp_path) is False
        assert _seed_skip_reasons(logs) == ["scan_failed"]

    def test_unexpected_filename_aborts(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path, ((2024, 1),))
        rigged = _CannedConn(
            rows=[
                (
                    "/elsewhere/2024/01.parquet",
                    5,
                    datetime(2024, 1, 5, tzinfo=UTC),
                    datetime(2024, 1, 6, tzinfo=UTC),
                )
            ]
        )
        with capture_logs() as logs:
            assert self._seed(rigged, tmp_path) is False
        assert _seed_skip_reasons(logs) == ["unexpected_file"]

    def test_duplicate_filename_aborts(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path, ((2024, 1),))
        base = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS)
        row = (
            str(base / "2024" / "01.parquet"),
            5,
            datetime(2024, 1, 5, tzinfo=UTC),
            datetime(2024, 1, 6, tzinfo=UTC),
        )
        rigged = _CannedConn(rows=[row, row])
        with capture_logs() as logs:
            assert self._seed(rigged, tmp_path) is False
        assert _seed_skip_reasons(logs) == ["unexpected_file"]

    def test_non_datetime_bounds_abort(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path, ((2024, 1),))
        base = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS)
        rigged = _CannedConn(rows=[(str(base / "2024" / "01.parquet"), 5, None, None)])
        with capture_logs() as logs:
            assert self._seed(rigged, tmp_path) is False
        assert _seed_skip_reasons(logs) == ["no_bounds"]

    def test_empty_parquet_file_aborts_with_distinct_reason(
        self, conn: Any, tmp_path: Path
    ) -> None:
        """A ZERO-row month file yields no group → the DISTINCT ``empty_file``
        abort reason (the file is still present + unmoved — a PERMANENT
        out-of-band condition, never a transient set change)."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        _write_months(tmp_path, ((2024, 1),))
        base = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS)
        empty = pa.table({"timestamp": pa.array([], type=pa.timestamp("us", tz="UTC"))})
        pq.write_table(empty, base / "2024" / "02.parquet")
        with capture_logs() as logs:
            assert self._seed(conn, tmp_path) is False
        assert _seed_skip_reasons(logs) == ["empty_file"]

    def test_missing_group_row_with_moved_file_is_file_set_changed(
        self, conn: Any, tmp_path: Path
    ) -> None:
        """A rel with no group row whose token MOVED mid-scan stays the
        transient ``file_set_changed`` reason — never mislabelled permanent."""
        _write_months(tmp_path, ((2024, 1), (2024, 2)))
        base = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS)
        jan = (
            str(base / "2024" / "01.parquet"),
            20,
            datetime(2024, 1, 5, 4, 0, tzinfo=UTC),
            datetime(2024, 1, 8, 4, 19, tzinfo=UTC),
        )
        # The canned grouped result DROPS February's row while the rig touches
        # the February file during the scan: missing + moved ⇒ transient.
        rig = _MutatingConn(
            _CannedConn(rows=[jan]),
            trigger=lambda sql, p: "GROUP BY filename" in sql,
            side_effect=lambda: _touch(base / "2024" / "02.parquet"),
        )
        with capture_logs() as logs:
            assert self._seed(rig, tmp_path) is False
        assert rig.fired
        assert _seed_skip_reasons(logs) == ["file_set_changed"]

    def test_missing_group_row_with_vanished_file_is_file_set_changed(
        self, conn: Any, tmp_path: Path
    ) -> None:
        """A rel with no group row whose file VANISHED mid-scan (the re-stat
        raises) is likewise the transient ``file_set_changed`` reason."""
        _write_months(tmp_path, ((2024, 1), (2024, 2)))
        base = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS)
        jan = (
            str(base / "2024" / "01.parquet"),
            20,
            datetime(2024, 1, 5, 4, 0, tzinfo=UTC),
            datetime(2024, 1, 8, 4, 19, tzinfo=UTC),
        )
        rig = _MutatingConn(
            _CannedConn(rows=[jan]),
            trigger=lambda sql, p: "GROUP BY filename" in sql,
            side_effect=(base / "2024" / "02.parquet").unlink,
        )
        with capture_logs() as logs:
            assert self._seed(rig, tmp_path) is False
        assert rig.fired
        assert _seed_skip_reasons(logs) == ["file_set_changed"]

    def test_file_deleted_during_scan_aborts_post_stat(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path, ((2024, 1), (2024, 2)))
        base = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS)
        rig = _MutatingConn(
            conn,
            trigger=lambda sql, p: "GROUP BY filename" in sql,
            side_effect=(base / "2024" / "02.parquet").unlink,
            fire_after=True,  # the scan itself succeeds; only the re-stat sees it
        )
        with capture_logs() as logs:
            assert self._seed(rig, tmp_path) is False
        assert rig.fired
        assert _seed_skip_reasons(logs) == ["post_stat_failed"]

    def test_file_touched_during_scan_aborts(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path, ((2024, 1), (2024, 2)))
        base = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS)
        rig = _MutatingConn(
            conn,
            trigger=lambda sql, p: "GROUP BY filename" in sql,
            side_effect=lambda: _touch(base / "2024" / "01.parquet"),
        )
        with capture_logs() as logs:
            assert self._seed(rig, tmp_path) is False
        assert rig.fired
        assert _seed_skip_reasons(logs) == ["file_moved"]

    def test_clean_scan_seeds_shard(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path, ((2024, 1), (2024, 2)))
        assert self._seed(conn, tmp_path) is True
        stats = lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=_COORDS)
        assert stats is not None
        assert stats.row_count == 40
        assert [row.rel for row in stats.files] == ["2024/01.parquet", "2024/02.parquet"]
        assert all(row.raw_hash is None for row in stats.files)

    def test_lake_write_halt_is_caught_on_the_read_thread(
        self, conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A disk-full LakeWriteHalt during the write-behind seed must NOT
        escape into the /db READ request (the routes catch only ValueError —
        an uncaught halt 500s a read-only paging click). The seed is skipped
        with the halt's reason; the payload is the legacy answer. The WRITE
        hooks' LakeWriteHalt propagation stays pinned separately
        (tests/unit/test_lake_stats_manifest.py)."""

        def _halt(*args: Any, **kwargs: Any) -> bool:
            raise LakeWriteHalt("disk_full")

        monkeypatch.setattr(db_view, "seed_dataset_stats", _halt)
        _write_months(tmp_path, ((2024, 1),))
        legacy = _q(conn, tmp_path, use=False)
        with capture_logs() as logs:
            served = _q(conn, tmp_path, use=True)  # must not raise
        assert served == legacy
        assert "disk_full" in _seed_skip_reasons(logs)
        assert not _shard_path(tmp_path).exists()


# ── seed-attempt latch: a rejected seed runs at most once per generation ────────────


def _grouped_seed_scans(rec: _RecordingConn) -> int:
    """Count the seed's ``GROUP BY filename`` full-dataset scans in a trace."""
    return sum(1 for sql, _src in rec.sources if "GROUP BY filename" in sql)


def _plant_foreign_month_copy(root: Path) -> None:
    """Copy January's partition to a March rel — the G-RACE-1 poison class.

    The seed measures the copy's REAL January bounds, which violate the
    ``2024/03.parquet`` rel month, so ``seed_dataset_stats`` REJECTS every
    seed attempt until ``lake_heal`` rewrites the partition.
    """
    dataset = lsm.dataset_dir(root, lsm.KIND_BARS, _COORDS)
    src = dataset / "2024" / "01.parquet"
    (src.parent / "03.parquet").write_bytes(src.read_bytes())


class TestSeedAttemptLatch:
    """A FAILED seed attempt is latched per (dataset, generation) — the doomed
    O(dataset-rows) grouped scan never repays on every paging click."""

    def test_rejected_seed_runs_at_most_once_per_generation(
        self, conn: Any, tmp_path: Path
    ) -> None:
        _write_months(tmp_path, ((2024, 1), (2024, 2)))
        _plant_foreign_month_copy(tmp_path)
        cache = MoversSnapshotCache()
        legacy = _q(conn, tmp_path, use=False)
        rec_first = _RecordingConn(conn)
        with capture_logs() as first_logs:
            first = _q(rec_first, tmp_path, use=True, count_cache=cache, generation=7)
        assert first == legacy
        assert _grouped_seed_scans(rec_first) == 1, "the first click pays the seed scan once"
        assert any(e["event"] == "lake.stats_manifest.seed_rejected" for e in first_logs)
        # Repeat clicks at the SAME generation: the latch skips the doomed
        # scan — the legacy page/count still serve the honest numbers.
        for offset in (0, 4, 9):
            rec = _RecordingConn(conn)
            with capture_logs() as logs:
                again = _q(rec, tmp_path, use=True, count_cache=cache, generation=7, offset=offset)
            assert again == _q(conn, tmp_path, use=False, offset=offset)
            assert _grouped_seed_scans(rec) == 0, "a latched dataset must never rescan"
            assert not any(e["event"] == "lake.stats_manifest.seed_rejected" for e in logs), (
                "the rejection WARNING must not repeat per click"
            )
            assert "attempt_blocked" in _seed_skip_reasons(logs)

    def test_generation_bump_reattempts_the_seed(self, conn: Any, tmp_path: Path) -> None:
        """A pull/heal bumps the generation — the blocked dataset re-attempts
        (a healed partition would now seed; the poison here still rejects)."""
        _write_months(tmp_path, ((2024, 1),))
        _plant_foreign_month_copy(tmp_path)
        cache = MoversSnapshotCache()
        rec_a = _RecordingConn(conn)
        _q(rec_a, tmp_path, use=True, count_cache=cache, generation=1)
        rec_b = _RecordingConn(conn)
        _q(rec_b, tmp_path, use=True, count_cache=cache, generation=1)
        rec_c = _RecordingConn(conn)
        _q(rec_c, tmp_path, use=True, count_cache=cache, generation=2)
        assert _grouped_seed_scans(rec_a) == 1
        assert _grouped_seed_scans(rec_b) == 0
        assert _grouped_seed_scans(rec_c) == 1, "a generation bump re-enables the seed"

    def test_successful_seed_never_blocks_the_reseed_contract(
        self, conn: Any, tmp_path: Path
    ) -> None:
        """An out-of-band invalidation WITHIN one generation still reseeds —
        only a FAILED attempt latches (the fallback→reseed healing contract)."""
        _write_months(tmp_path, ((2024, 1), (2024, 2)))
        cache = MoversSnapshotCache()
        _q(conn, tmp_path, use=True, count_cache=cache, generation=5)  # clean seed
        assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=_COORDS)
        _touch(lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS) / "2024" / "01.parquet")
        with capture_logs() as logs:
            _q(conn, tmp_path, use=True, count_cache=cache, generation=5)
        assert "stat_token" in _fallback_clauses(logs)
        assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=_COORDS), (
            "the same-generation reseed must have healed the shard"
        )

    def test_count_cache_none_attempts_every_click(self, conn: Any, tmp_path: Path) -> None:
        """The pure default has no latch state — behaviour-identical to before."""
        _write_months(tmp_path, ((2024, 1),))
        _plant_foreign_month_copy(tmp_path)
        for _ in range(2):
            rec = _RecordingConn(conn)
            _q(rec, tmp_path, use=True)
            assert _grouped_seed_scans(rec) == 1

    def test_options_rejected_seed_latches_too(self, conn: Any, tmp_path: Path) -> None:
        """The options pager rides the SAME latch helper."""
        _write_option_months(tmp_path)
        series_dir = tmp_path / "options" / _OPT_SERIES
        src = series_dir / "2024-03.parquet"
        (series_dir / "2024-05.parquet").write_bytes(src.read_bytes())  # foreign-month copy
        cache = MoversSnapshotCache()
        rec_a = _RecordingConn(conn)
        _qopt(rec_a, tmp_path, use=True, count_cache=cache, generation=3)
        rec_b = _RecordingConn(conn)
        _qopt(rec_b, tmp_path, use=True, count_cache=cache, generation=3)
        assert _grouped_seed_scans(rec_a) == 1
        assert _grouped_seed_scans(rec_b) == 0

    def test_seed_key_is_disjoint_per_kind_and_coords(self) -> None:
        spot = db_view._stats_seed_key(lsm.KIND_BARS, _COORDS)
        other = db_view._stats_seed_key(lsm.KIND_BARS, {**_COORDS, "interval": "1day"})
        opt = db_view._stats_seed_key(lsm.KIND_OPTIONS, {"series": _OPT_SERIES})
        assert spot.startswith("statseed|bars|")
        assert opt.startswith("statseed|options|")
        assert len({spot, other, opt}) == 3


# ── whitebox: the small helpers, branch by branch ───────────────────────────────────


def _row(
    rel: str = "2024/01.parquet",
    *,
    rows: int = 10,
    min_ts: datetime | None = None,
    max_ts: datetime | None = None,
) -> lsm.StatsFileRow:
    return lsm.StatsFileRow(
        rel=rel,
        rows=rows,
        min_ts=min_ts if min_ts is not None else datetime(2024, 1, 5, 4, 0, tzinfo=UTC),
        max_ts=max_ts if max_ts is not None else datetime(2024, 1, 8, 4, 0, tzinfo=UTC),
        mtime_ns=1,
        size=2,
        raw_hash=None,
    )


class TestStatsFileVerdict:
    def test_utc_calendar_grid(self) -> None:
        row = _row()  # UTC dates Jan 5 → Jan 8
        assert _stats_file_verdict(row, date(2024, 2, 1), None, ist_dates=False) == "outside"
        assert _stats_file_verdict(row, None, date(2024, 1, 4), ist_dates=False) == "outside"
        assert _stats_file_verdict(row, None, None, ist_dates=False) == "inside"
        assert (
            _stats_file_verdict(row, date(2024, 1, 5), date(2024, 1, 8), ist_dates=False)
            == "inside"
        )
        assert _stats_file_verdict(row, date(2024, 1, 6), None, ist_dates=False) == "straddle"
        assert _stats_file_verdict(row, None, date(2024, 1, 7), ist_dates=False) == "straddle"

    def test_ist_calendar_shifts_the_dates(self) -> None:
        """A 18:30-UTC stamp is the NEXT IST day — the two calendars disagree."""
        row = _row(
            min_ts=datetime(2024, 1, 31, 18, 30, tzinfo=UTC),  # IST Feb 1
            max_ts=datetime(2024, 2, 2, 18, 30, tzinfo=UTC),  # IST Feb 3
        )
        feb = date(2024, 2, 1)
        assert _stats_file_verdict(row, feb, None, ist_dates=True) == "inside"
        assert _stats_file_verdict(row, feb, None, ist_dates=False) == "straddle"
        assert _stats_file_verdict(row, None, date(2024, 1, 31), ist_dates=True) == "outside"


class TestStatsReadContextHelpers:
    def _ctx(self, **kw: Any) -> _StatsReadContext:
        defaults: dict[str, Any] = {
            "conn": None,
            "lake_root": Path("/nowhere"),
            "kind": lsm.KIND_BARS,
            "key": dict(_COORDS),
            "base": Path("/nowhere/ds"),
            "identity": ("timestamp",),
            "stats": None,
        }
        defaults.update(kw)
        return _StatsReadContext(**defaults)

    def test_ist_span_without_stats_is_none_pair(self) -> None:
        assert self._ctx().ist_span() == (None, None)

    def test_plan_page_walks_prefix_sums(self) -> None:
        a, b, c = _row("2024/01.parquet"), _row("2024/02.parquet"), _row("2024/03.parquet")
        ctx = self._ctx(effective=((a, 10), (b, 0), (c, 5)), served=True)
        # ASC: window inside the first file (the zero-count file is skipped).
        files, rebased = ctx.plan_page(descending=False, limit=3, offset=0)
        assert [f.rel for f in files] == ["2024/01.parquet"]
        assert rebased == 0
        # ASC crossing the file boundary rebases into the FIRST selected file.
        files, rebased = ctx.plan_page(descending=False, limit=5, offset=8)
        assert [f.rel for f in files] == ["2024/01.parquet", "2024/03.parquet"]
        assert rebased == 8
        # ASC wholly inside the last file.
        files, rebased = ctx.plan_page(descending=False, limit=2, offset=12)
        assert [f.rel for f in files] == ["2024/03.parquet"]
        assert rebased == 2
        # Beyond the total ⇒ empty plan.
        assert ctx.plan_page(descending=False, limit=5, offset=100) == ([], 0)
        # DESC: the walk reverses but the sublist returns rel-ASCENDING.
        files, rebased = ctx.plan_page(descending=True, limit=2, offset=0)
        assert [f.rel for f in files] == ["2024/03.parquet"]
        assert rebased == 0
        files, rebased = ctx.plan_page(descending=True, limit=20, offset=4)
        assert [f.rel for f in files] == ["2024/01.parquet", "2024/03.parquet"]
        assert rebased == 4

    def test_total_is_sum_of_effective(self) -> None:
        ctx = self._ctx(effective=((_row(), 10), (_row("2024/02.parquet"), 5)), served=True)
        assert ctx.total == 15

    def test_fingerprint_tracks_the_stat_tokens(self, tmp_path: Path) -> None:
        stats_a = lsm.build_dataset_stats(kind=lsm.KIND_BARS, key=_COORDS, files=[_row()])
        moved = lsm.StatsFileRow(
            rel="2024/01.parquet",
            rows=10,
            min_ts=datetime(2024, 1, 5, 4, 0, tzinfo=UTC),
            max_ts=datetime(2024, 1, 8, 4, 0, tzinfo=UTC),
            mtime_ns=999,
            size=2,
            raw_hash=None,
        )
        stats_b = lsm.build_dataset_stats(kind=lsm.KIND_BARS, key=_COORDS, files=[moved])
        assert _stats_fingerprint(stats_a) != _stats_fingerprint(stats_b)


class TestStatsUtcTs:
    def test_non_datetime_is_none(self) -> None:
        assert _stats_utc_ts(None) is None
        assert _stats_utc_ts("2024-01-01") is None

    def test_naive_gets_utc_attached(self) -> None:
        out = _stats_utc_ts(datetime(2024, 1, 5, 4, 0))
        assert out == datetime(2024, 1, 5, 4, 0, tzinfo=UTC)

    def test_aware_is_normalized(self) -> None:
        from zoneinfo import ZoneInfo

        ist = datetime(2024, 1, 5, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
        assert _stats_utc_ts(ist) == datetime(2024, 1, 5, 4, 0, tzinfo=UTC)


class TestStatsReadContextBuilder:
    def test_straddler_fetchone_none_demotes(self, conn: Any, tmp_path: Path) -> None:
        """A straddler count returning NO row demotes the whole request."""
        _write_months(tmp_path, ((2024, 1),))
        _seed_by_reading(conn, tmp_path)
        rigged = _CannedConn(fetchone_none=True)
        with capture_logs() as logs:
            ctx = _stats_read_context(
                rigged,
                tmp_path,
                kind=lsm.KIND_BARS,
                key=dict(_COORDS),
                base=lsm.dataset_dir(tmp_path, lsm.KIND_BARS, _COORDS),
                identity=("timestamp",),
                where_sql=" WHERE 1=1",
                where_params=[],
                ist_dates=False,
                start="2024-01-06",
                end=None,
                use_stats_manifest=True,
            )
        assert ctx is not None
        assert ctx.served is False
        assert "straddler_read" in _fallback_clauses(logs)


class TestStatsPrunedPageGuards:
    def _served_ctx(self, conn: Any, root: Path) -> _StatsReadContext:
        ctx = _stats_read_context(
            conn,
            root,
            kind=lsm.KIND_BARS,
            key=dict(_COORDS),
            base=lsm.dataset_dir(root, lsm.KIND_BARS, _COORDS),
            identity=("timestamp",),
            where_sql="",
            where_params=[],
            ist_dates=False,
            start=None,
            end=None,
            use_stats_manifest=True,
        )
        assert ctx is not None and ctx.served
        return ctx

    def _prune(self, ctx: _StatsReadContext | None, **kw: Any) -> Any:
        params: dict[str, Any] = {
            "select": '"timestamp"',
            "columns": ("timestamp",),
            "where_sql": "",
            "where_params": [],
            "qualify_sql": "",
            "order_sql": ' ORDER BY "timestamp"',
            "descending": False,
            "limit": 5,
            "offset": 0,
            "count_cache": None,
            "generation": 0,
        }
        params.update(kw)
        return _stats_pruned_page(ctx, **params)

    def test_none_ctx_and_unserved_ctx_return_none(self, conn: Any, tmp_path: Path) -> None:
        assert self._prune(None) is None
        unserved = _StatsReadContext(
            conn=conn,
            lake_root=tmp_path,
            kind=lsm.KIND_BARS,
            key=dict(_COORDS),
            base=tmp_path,
            identity=("timestamp",),
            stats=None,
        )
        assert self._prune(unserved) is None

    def test_wrong_cursor_description_falls_back(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path, ((2024, 1),))
        _seed_by_reading(conn, tmp_path)
        ctx = self._served_ctx(conn, tmp_path)
        ctx.conn = _CannedConn(rows=[], description=(("wrong",),))
        with capture_logs() as logs:
            assert self._prune(ctx) is None
        assert ctx.served is False
        assert "page_columns" in _fallback_clauses(logs)

    def test_none_cursor_description_falls_back(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path, ((2024, 1),))
        _seed_by_reading(conn, tmp_path)
        ctx = self._served_ctx(conn, tmp_path)
        ctx.conn = _CannedConn(rows=[], description=None)
        with capture_logs() as logs:
            assert self._prune(ctx) is None
        assert ctx.served is False
        assert "page_columns" in _fallback_clauses(logs)

    def test_star_columns_without_stats_is_none(self, conn: Any, tmp_path: Path) -> None:
        unserved = _StatsReadContext(
            conn=conn,
            lake_root=tmp_path,
            kind=lsm.KIND_BARS,
            key=dict(_COORDS),
            base=tmp_path,
            identity=("timestamp",),
            stats=None,
        )
        assert _stats_star_columns(unserved, count_cache=None, generation=0) is None

    def test_star_describe_error_falls_back(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path, ((2024, 1),))
        _seed_by_reading(conn, tmp_path)
        ctx = self._served_ctx(conn, tmp_path)
        ctx.conn = _CannedConn(exc=duckdb.InvalidInputException("boom"))
        with capture_logs() as logs:
            assert self._prune(ctx, select="*", columns=None) is None
        assert ctx.served is False
        assert "star_columns" in _fallback_clauses(logs)

    def test_empty_plan_returns_expected_columns(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path, ((2024, 1),))
        _seed_by_reading(conn, tmp_path)
        ctx = self._served_ctx(conn, tmp_path)
        out = self._prune(ctx, offset=10_000)
        assert out == (["timestamp"], [])


class TestProvenanceEvent:
    def test_read_event_names_the_page_source(self, conn: Any, tmp_path: Path) -> None:
        _write_months(tmp_path, ((2024, 1),))
        with capture_logs() as logs:
            _q(conn, tmp_path, use=True)  # fallback + seed
            _q(conn, tmp_path, use=True)  # manifest-served
        reads = [e for e in logs if e["event"] == "db.stats_manifest.read"]
        assert [e["page_source"] for e in reads] == ["live_scan", "manifest"]
        assert [e["validated"] for e in reads] == [False, True]
        # Emitted at INFO (production logging defaults to INFO): an operator
        # must be able to POSITIVELY confirm the manifest path is being taken
        # — a DEBUG-only event made the healthy path invisible in production.
        assert [e["log_level"] for e in reads] == ["info", "info"]
