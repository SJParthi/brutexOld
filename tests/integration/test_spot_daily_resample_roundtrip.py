"""Integration round-trip oracle for the always-on 1-day derive (DEC-1MINUTE-ONLY-DERIVE-1DAY-001).

Proves the PRODUCTION lake I/O round-trips through the REAL builders
(:func:`~brutex.data.spot_daily_resample.make_lake_read_1m_window` +
:func:`~brutex.data.spot_daily_resample.make_lake_write_1day`): write real
1-minute bars across two IST sessions that straddle a UTC month boundary, fill the
``1day`` partition from them, then read it straight back via the production candle
reader and assert each daily bar is **midnight-IST stamped** (== 18:30 UTC of the
prior calendar day → the correct UTC-month partition) with the correct OHLCV. Also
pins: window-bounded read (out-of-window 1-minute excluded), idempotent re-run
(byte-identical), and the FIX-2 latest-wins self-heal — a daily derived from a
PARTIAL session is CORRECTED by a later COMPLETE-session re-derive at the same
midnight-IST stamp (DEC-1MINUTE-ONLY-DERIVE-1DAY-001), superseding the old keep-first
dedup against a now-nonexistent vendor daily.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

import duckdb
from brutex.dashboard.db_view import query_candles
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.spot_daily_resample import (
    fill_daily_from_minute,
    make_lake_read_1m_window,
    make_lake_write_1day,
)

_EXCHANGE = "NSE"
_SEGMENT = "CASH"  # the vendor-quirk segment (cash equities)
_SYMBOL = "RELIANCE"

# Two IST sessions that straddle a UTC month boundary:
#   2025-12-31 IST  → 2025-12-30T18:30Z  (December UTC partition)
#   2026-01-02 IST  → 2026-01-01T18:30Z  (January  UTC partition)
_SESSIONS: tuple[date, ...] = (date(2025, 12, 31), date(2026, 1, 2))


def _session_1m(day: date, base: float) -> list[CandleBar]:
    """30 in-session 1-minute bars (09:15 IST → 03:45 UTC + i minutes)."""
    open_utc = datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC)
    return [
        CandleBar(
            timestamp=open_utc + timedelta(minutes=i),
            open=Decimal(str(base + i)),
            high=Decimal(str(base + 10 + i)),
            low=Decimal(str(base - 5 + i)),
            close=Decimal(str(base + 2 + i)),
            volume=100 + i,
            open_interest=None,
        )
        for i in range(30)
    ]


def _write_1m(lake_root: Path) -> dict[date, list[CandleBar]]:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    by_day: dict[date, list[CandleBar]] = {}
    for idx, day in enumerate(_SESSIONS):
        bars = _session_1m(day, 1400.0 + idx * 100)
        by_day[day] = bars
        sink.append(
            exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1minute", bars=bars
        )
    return by_day


def _fill(lake_root: Path, *, start: date, end: date) -> int:
    read = make_lake_read_1m_window(
        lake_root=lake_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        start=start,
        end=end,
    )
    write = make_lake_write_1day(
        lake_root=lake_root, exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL
    )
    return fill_daily_from_minute(
        read_1m=read, write_1day=write, before=date(2026, 6, 19)
    ).bars_written


def _read_daily(lake_root: Path) -> list[Any]:
    conn = duckdb.connect()
    try:
        page = query_candles(
            conn,
            lake_root,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval="1day",
            columns=("timestamp", "open", "high", "low", "close", "volume"),
            limit=500,
        )
    finally:
        conn.close()
    return page["rows"]


def test_daily_resample_round_trips_midnight_ist(tmp_path: Path) -> None:
    by_day = _write_1m(tmp_path)
    written = _fill(tmp_path, start=date(2025, 12, 31), end=date(2026, 1, 3))
    assert written == 2

    # Both 1day partitions exist — across the UTC month boundary.
    base = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1day"
    assert (base / "2025" / "12.parquet").exists()  # 2025-12-31 IST → Dec UTC partition
    assert (base / "2026" / "01.parquet").exists()  # 2026-01-02 IST → Jan UTC partition

    rows = _read_daily(tmp_path)
    assert len(rows) == 2
    # Oracle: the candle reader renders a 1day bar at its IST *date* (midnight-IST
    # stamping → IST date == the session date). The exact UTC instant
    # (18:30Z prior-day) is pinned by the unit test + the partition placement above.
    for row, day in zip(rows, _SESSIONS, strict=True):
        assert row[0] == day.isoformat()
        group = by_day[day]
        assert float(row[1]) == float(group[0].open)  # open = first
        assert float(row[2]) == max(float(b.high) for b in group)  # high = max
        assert float(row[3]) == min(float(b.low) for b in group)  # low = min
        assert float(row[4]) == float(group[-1].close)  # close = last
        assert int(row[5]) == sum(b.volume for b in group)  # volume = sum


def test_window_bound_excludes_out_of_range_minute(tmp_path: Path) -> None:
    _write_1m(tmp_path)
    # Window covers ONLY the first session — the 2026-01-02 1m must be excluded.
    written = _fill(tmp_path, start=date(2025, 12, 31), end=date(2026, 1, 1))
    assert written == 1
    rows = _read_daily(tmp_path)
    assert len(rows) == 1


def test_daily_resample_is_idempotent(tmp_path: Path) -> None:
    _write_1m(tmp_path)
    _fill(tmp_path, start=date(2025, 12, 31), end=date(2026, 1, 3))
    base = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1day"
    files = sorted(base.rglob("*.parquet"))
    first_bytes = {p: p.read_bytes() for p in files}

    # Re-run: same 1m → same daily → byte-identical, zero net-new partition files.
    _fill(tmp_path, start=date(2025, 12, 31), end=date(2026, 1, 3))
    files_after = sorted(base.rglob("*.parquet"))
    assert files_after == files
    for p in files_after:
        assert p.read_bytes() == first_bytes[p]


def _partial_morning_1m(day: date, base: float) -> list[CandleBar]:
    """ONLY the first 5 in-session 1-minute bars (a partial session — mid-session death)."""
    open_utc = datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC)
    return [
        CandleBar(
            timestamp=open_utc + timedelta(minutes=i),
            open=Decimal(str(base + i)),
            high=Decimal(str(base + 10 + i)),
            low=Decimal(str(base - 5 + i)),
            close=Decimal(str(base + 2 + i)),
            volume=100 + i,
            open_interest=None,
        )
        for i in range(5)
    ]


def test_partial_session_daily_is_corrected_by_complete_repull(tmp_path: Path) -> None:
    """FIX-2 latest-wins: a daily derived from a PARTIAL session is CORRECTED by a later
    COMPLETE-session derive at the SAME midnight-IST stamp — no frozen wrong bar.

    Supersedes the old ``test_keep_first_dedup_against_vendor_daily``: 1-day is now ALWAYS
    derived from 1-minute (there is NO vendor daily to protect with keep-first), so the
    derived ``1day`` partition is written latest-wins and a complete re-pull restates a
    partial-session daily instead of freezing it.
    """
    day = date(2025, 12, 31)
    base = 1400.0
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))

    # 1) A PARTIAL pull lands only the morning 1-minute → a WRONG (partial) daily bar.
    partial = _partial_morning_1m(day, base)
    sink.append(
        exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1minute", bars=partial
    )
    _fill(tmp_path, start=date(2025, 12, 31), end=date(2026, 1, 1))
    rows = _read_daily(tmp_path)
    assert len(rows) == 1
    # The partial daily reflects ONLY the morning 5 minutes (the frozen-bar hazard).
    assert float(rows[0][2]) == max(float(b.high) for b in partial)  # partial high
    assert int(rows[0][5]) == sum(b.volume for b in partial)  # partial volume

    # 2) The COMPLETE session's 1-minute arrives (a later full pull) → re-derive same date.
    full = _session_1m(day, base)
    sink.append(exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1minute", bars=full)
    _fill(tmp_path, start=date(2025, 12, 31), end=date(2026, 1, 1))
    rows = _read_daily(tmp_path)
    # Still exactly ONE daily bar for the session — never doubled.
    assert len(rows) == 1
    # latest-wins: the persisted daily is now the COMPLETE/correct bar, NOT the frozen partial.
    assert float(rows[0][1]) == float(full[0].open)  # open = first minute
    assert float(rows[0][2]) == max(float(b.high) for b in full)  # high = max over FULL session
    assert float(rows[0][3]) == min(float(b.low) for b in full)  # low  = min over FULL session
    assert float(rows[0][4]) == float(full[-1].close)  # close = last minute
    assert int(rows[0][5]) == sum(b.volume for b in full)  # volume = sum over FULL session
    # The corrected bar genuinely differs from the frozen partial (proof the fix engaged).
    assert int(rows[0][5]) > sum(b.volume for b in partial)


def _write_1m_across_months(lake_root: Path) -> None:
    """Write 1-minute across SIX month partitions (incl. an untouched 2024 month).

    The whole-tree read globbed ALL of these on every derive; the bounded read
    (DEC-RESAMPLE-BOUND-GLOB-001) reads ONLY the months the IST span covers.
    """
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    days = (
        date(2024, 6, 3),  # far-past, never in any tested span
        date(2025, 11, 5),
        date(2025, 12, 31),
        date(2026, 1, 2),
        date(2026, 2, 14),
    )
    for idx, day in enumerate(days):
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval="1minute",
            bars=_session_1m(day, 1400.0 + idx * 100),
        )


def test_bounded_read_reads_strictly_fewer_files_than_whole_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded month-glob reads STRICTLY FEWER Parquet files than the whole tree.

    Spies :func:`polars.read_parquet` (the call the REAL ``make_lake_read_1m_window``
    makes) to capture the path-list it is handed, and compares against the on-disk
    partition count. A single-day span must read only its own month(s), never the whole
    multi-year tree, and never the far-past 2024 month (DEC-RESAMPLE-BOUND-GLOB-001).
    """
    import polars as pl

    _write_1m_across_months(tmp_path)
    base = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1minute"
    whole_tree_files = sorted(base.rglob("*.parquet"))
    assert len(whole_tree_files) == 5  # five month partitions on disk

    captured: list[list[str]] = []
    real_scan_parquet = pl.scan_parquet

    def _spy(source: list[str], **kwargs: object) -> pl.LazyFrame:
        # The bounded reader passes an EXPLICIT list of file paths (not a glob string)
        # to the schema-tolerant scan (DEC-GREEKS-ENGINE-PR2B-001 — a mixed
        # 7/17-column option tree reads cleanly; the bounding contract is unchanged).
        captured.append(list(source))
        return real_scan_parquet(source, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pl, "scan_parquet", _spy)
    read = make_lake_read_1m_window(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        start=date(2025, 12, 31),
        end=date(2026, 1, 1),  # ONLY the 2025-12-31 IST session
    )
    read()

    assert len(captured) == 1
    globbed = captured[0]
    # STRICTLY fewer than the whole tree, and never reads the far-past 2024 month.
    assert 0 < len(globbed) < len(whole_tree_files)
    assert not any("2024" in p for p in globbed)


def test_bounded_read_byte_identical_to_whole_tree(tmp_path: Path) -> None:
    """The bounded read's resampled 1-day bars are BYTE-IDENTICAL to the whole-tree read.

    Drives the REAL ``make_lake_read_1m_window`` (bounded) and a faithful replica of the
    OLD whole-tree read over the SAME multi-month + cross-year lake, then compares the
    :class:`~brutex.data.models.CandleBar` lists ``resample_1m_to_daily`` produces.
    """
    import polars as pl
    from brutex.data.spot_daily_resample import resample_1m_to_daily

    _write_1m_across_months(tmp_path)
    base = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1minute"
    before = date(2026, 6, 19)

    def _old_whole_tree(start: date, end: date) -> pl.DataFrame:
        frame = pl.read_parquet(base.as_posix() + "/**/*.parquet")
        ist = (pl.col("ts_utc") + pl.duration(hours=5, minutes=30)).dt.date()
        return (
            frame.rename({"timestamp": "ts_utc"})
            .with_columns(ist.alias("_ist_date"))
            .filter((pl.col("_ist_date") >= start) & (pl.col("_ist_date") < end))
            .select("ts_utc", "open", "high", "low", "close", "volume")
            .sort("ts_utc")
        )

    spans = (
        (date(2025, 11, 1), date(2026, 3, 1)),  # multi-month + cross-year
        (date(2025, 12, 20), date(2026, 1, 10)),  # cross-year boundary
        (date(2024, 6, 1), date(2024, 7, 1)),  # single far-past month
        (date(2030, 1, 1), date(2030, 2, 1)),  # absent span → empty
    )
    for start, end in spans:
        bounded = make_lake_read_1m_window(
            lake_root=tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            start=start,
            end=end,
        )()
        new_bars = resample_1m_to_daily(bounded, before=before)
        old_bars = resample_1m_to_daily(_old_whole_tree(start, end), before=before)
        assert new_bars == old_bars, f"daily output diverged for [{start}, {end})"


# ────────────────────────────────────────────────────────────────────────
# Window-reader price-sanity gate (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001,
# Phase-1 finding #12): make_lake_read_1m_window ran NEITHER price_sanity
# NOR bad-tick pre-fix, so a corrupt bar yielded a CLEAN derived 1day via the
# full prebuild but a POISONED 1day via the heal-recall / post-pull /
# crossverify window path. The strengthened COMMON gate now runs here too.
# ────────────────────────────────────────────────────────────────────────


def _plant_nonfinite_window_1m(
    lake_root: Path, *, column: str = "high", value: float, day: date = date(2025, 12, 31)
) -> None:
    """Raw-plant one non-finite *column* bar inside *day*'s 1-minute partition.

    Raw pyarrow — the writer + the vendor parse (``to_price``) refuse
    non-finite prices, so this shape is only ever planted/out-of-band
    corrupt stored data (the #1067 raw-plant convention).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    open_utc = datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC)
    dest = (
        lake_root
        / "bars"
        / _EXCHANGE
        / _SEGMENT
        / _SYMBOL
        / "1minute"
        / f"{open_utc.year:04d}"
        / f"{open_utc.month:02d}.parquet"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    timestamps = [open_utc + timedelta(minutes=i) for i in range(3)]
    columns: dict[str, list[float]] = {
        "open": [1400.0, 1401.0, 1402.0],
        "high": [1410.0, 1411.0, 1412.0],
        "low": [1395.0, 1396.0, 1397.0],
        "close": [1402.0, 1403.0, 1404.0],
    }
    columns[column][0] = value  # bar 0 carries the non-finite poison
    pq.write_table(
        pa.table(
            {
                "timestamp": pa.array(timestamps, type=pa.timestamp("us", tz="UTC")),
                "open": pa.array(columns["open"], type=pa.float64()),
                "high": pa.array(columns["high"], type=pa.float64()),
                "low": pa.array(columns["low"], type=pa.float64()),
                "close": pa.array(columns["close"], type=pa.float64()),
                "volume": pa.array([10, 10, 10], type=pa.int64()),
                "open_interest": pa.array([None, None, None], type=pa.int64()),
            }
        ),
        dest,
    )


@pytest.mark.parametrize("value", [float("inf"), float("nan")], ids=["+inf", "nan"])
def test_window_reader_rejects_nonfinite_rows(tmp_path: Path, value: float) -> None:
    """FAILS-BEFORE (finding #12): pre-fix the window reader RETURNED the
    frame with ``high ∈ {inf, NaN}`` silently — no raise, no skip, no count —
    and the daily max aggregation minted a poisoned derived ``1day``."""
    from brutex.data.errors import ResampleInputError

    _plant_nonfinite_window_1m(tmp_path, value=value)
    read = make_lake_read_1m_window(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        start=date(2025, 12, 31),
        end=date(2026, 1, 1),
    )
    with pytest.raises(ResampleInputError, match="price-insane") as excinfo:
        read()
    assert f"{_EXCHANGE}/{_SEGMENT}/{_SYMBOL}/1minute" in str(excinfo.value)


def test_window_reader_misordered_bar_also_rejected(tmp_path: Path) -> None:
    """The wired gate is the FULL price-sanity law (finiteness + positivity +
    ordering), not a finiteness-only sliver: a ``high < low`` bar in the
    window is refused too (pre-fix it laundered into the derived 1day)."""
    from brutex.data.errors import ResampleInputError

    # high below low: plant low above the constant highs.
    _plant_nonfinite_window_1m(tmp_path, column="low", value=99999.0)
    read = make_lake_read_1m_window(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        start=date(2025, 12, 31),
        end=date(2026, 1, 1),
    )
    with pytest.raises(ResampleInputError, match="price-insane"):
        read()


def test_window_reader_corrupt_row_outside_window_reads_clean(tmp_path: Path) -> None:
    """VALIDATE-WHAT-YOU-RETURN (the DEC-READ-GUARD-PARITY-001 law): the gate
    runs on the WINDOW-CUT frame only, so a corrupt bar strictly OUTSIDE
    ``[start, end)`` never blanks an earlier window's read."""
    _write_1m(tmp_path)  # clean 2025-12-31 + 2026-01-02 sessions
    # Poison a LATER session (2026-01-05) the window below excludes.
    _plant_nonfinite_window_1m(tmp_path, value=float("inf"), day=date(2026, 1, 5))
    read = make_lake_read_1m_window(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        start=date(2025, 12, 31),
        end=date(2026, 1, 1),
    )
    frame = read()
    assert frame.height == 30  # the clean 2025-12-31 session, served intact
    assert frame.get_column("high").is_finite().all()


def test_window_reader_clean_lake_is_pure_pass_through(tmp_path: Path) -> None:
    """HEALTHY-LAKE IDENTITY PIN (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001): on
    a clean lake the wired gate changes NOTHING — the window reader's frame
    equals the raw parquet content under the same rename/filter/select/sort."""
    import polars as pl

    _write_1m(tmp_path)
    read = make_lake_read_1m_window(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        start=date(2025, 12, 31),
        end=date(2026, 1, 3),
    )
    frame = read()
    raw = pl.read_parquet(
        (tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1minute").as_posix()
        + "/**/*.parquet"
    )
    ist_date = (pl.col("ts_utc") + pl.duration(hours=5, minutes=30)).dt.date()
    expected = (
        raw.rename({"timestamp": "ts_utc"})
        .with_columns(ist_date.alias("_ist_date"))
        .filter(
            (pl.col("_ist_date") >= date(2025, 12, 31)) & (pl.col("_ist_date") < date(2026, 1, 3))
        )
        .select("ts_utc", "open", "high", "low", "close", "volume")
        .sort("ts_utc")
    )
    assert frame.equals(expected)


def test_derive_daily_quarantines_nonfinite_target_no_crash(tmp_path: Path) -> None:
    """END-TO-END (the heal-recall / crossverify shape): a poisoned window
    target is counted ``errored`` by ``derive_daily_for_targets``'s
    per-target quarantine — the pass completes, NO ``1day`` is written and
    nothing crashes (pre-fix the poison silently derived a corrupt 1day)."""
    from brutex.data.spot_daily_resample import DailyDeriveTarget, derive_daily_for_targets

    _plant_nonfinite_window_1m(tmp_path, value=float("inf"))
    target = DailyDeriveTarget(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        start=date(2025, 12, 31),
        end=date(2026, 1, 1),
    )
    report = derive_daily_for_targets(
        targets=[target],
        build_read_1m=lambda t: make_lake_read_1m_window(
            lake_root=tmp_path,
            exchange=t.exchange,
            segment=t.segment,
            symbol=t.symbol,
            start=t.start,
            end=t.end,
        ),
        build_write_1day=lambda t: make_lake_write_1day(
            lake_root=tmp_path, exchange=t.exchange, segment=t.segment, symbol=t.symbol
        ),
        before=date(2026, 6, 19),
    )
    assert report.errored == 1
    assert report.derived == 0
    assert not (tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1day").exists()


# ────────────────────────────────────────────────────────────────────────
# Window reader — bars-tree glob parity + the ts-range belt
# (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001)
# ────────────────────────────────────────────────────────────────────────


def test_window_read_bracket_symbol_reads_literal_file_only(tmp_path: Path) -> None:
    """FAILS-BEFORE (the #1594 round-15 metachar law): a symbol dir named
    ``NSE-X[12]`` beside real ``NSE-X1``/``NSE-X2`` siblings made the window
    reader's month-file list glob-interpret and pull the SIBLINGS' bars into
    this symbol's derived 1day. glob=False reads the literal file only."""
    import polars as pl

    day = date(2024, 1, 3)

    def _plant(symbol: str, close: float) -> None:
        dest = (
            tmp_path
            / "bars"
            / "NSE"
            / "CASH"
            / symbol
            / "1minute"
            / f"{day.year:04d}"
            / f"{day.month:02d}.parquet"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        stamps = [datetime(2024, 1, 3, 4, i, tzinfo=UTC) for i in range(3)]
        pl.DataFrame(
            {
                "timestamp": stamps,
                "open": [close] * 3,
                "high": [close + 1] * 3,
                "low": [close - 1] * 3,
                "close": [close] * 3,
                "volume": [10, 11, 12],
            }
        ).write_parquet(dest)

    _plant("NSE-X1", 100.11)
    _plant("NSE-X2", 100.22)
    _plant("NSE-X[12]", 100.55)  # the literal dir's OWN bars
    read = make_lake_read_1m_window(
        lake_root=tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-X[12]",
        start=day,
        end=day + timedelta(days=1),
    )
    frame = read()
    assert frame.height == 3
    assert set(frame.get_column("close").to_list()) == {100.55}


def test_window_read_excludes_out_of_range_ts_row_structurally(tmp_path: Path) -> None:
    """The window cut structurally excludes an out-of-datetime-range stored
    stamp (its IST date can never fall inside a real ``[start, end)``), so the
    window read returns ONLY the in-window rows and never panics; the ts gate
    behind the filter is the defense-in-depth belt
    (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001)."""
    import polars as pl

    day = date(2024, 1, 3)
    dest = (
        tmp_path
        / "bars"
        / "NSE"
        / "INDEX"
        / "NIFTY"
        / "1minute"
        / f"{day.year:04d}"
        / f"{day.month:02d}.parquet"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    stamps = pl.Series(
        "timestamp",
        [
            int(datetime(2024, 1, 3, 4, 0, tzinfo=UTC).timestamp() * 1_000_000),
            int(datetime(2024, 1, 3, 4, 1, tzinfo=UTC).timestamp() * 1_000_000),
            2**62,  # ~year 148108 — far past datetime.max, within i64
        ],
        dtype=pl.Int64,
    ).cast(pl.Datetime(time_unit="us", time_zone="UTC"))
    pl.DataFrame(
        {
            "timestamp": stamps,
            "open": [100.0] * 3,
            "high": [101.0] * 3,
            "low": [99.0] * 3,
            "close": [100.5] * 3,
            "volume": [10, 11, 12],
        }
    ).write_parquet(dest)
    read = make_lake_read_1m_window(
        lake_root=tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        start=day,
        end=day + timedelta(days=1),
    )
    frame = read()
    assert frame.height == 2  # the poison row is outside every real window
    # And converting the returned frame to Python instants is SAFE — the very
    # operation the poison row panicked pre-fix.
    assert len(frame.get_column("ts_utc").to_list()) == 2
