"""Integration round-trip oracle for derived-TF materialise (DEC-TF-MATERIALIZE-001).

The high-stakes test that proves the PRODUCTION lake I/O round-trips: write ~120
real 1-minute bars to a tmp lake via :class:`ParquetBarSink`, run the
materialise through the REAL shared builders (``make_lake_read_1m`` +
``make_lake_write_tf`` — the same code path the route uses), then read the
derived ``5minute`` partition back via :func:`brutex.dashboard.db_view.query_candles`
and assert each 5-minute bar aggregates its 5 one-minute bars correctly
(open=first, high=max, low=min, close=last, volume=sum). Re-running must be
idempotent — zero net-new rows and a byte-identical Parquet file (CLAUDE.md §7).

DEC-PREBUILD-SELECT-001 adds a FUTURES OI round-trip: F&O 1-minute bars carrying
open_interest (incl. null OI rows) flow through the OI-aware materialise and the
derived bars preserve the per-bucket carry-last OI (never dropped, never faked).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("fastapi")

import duckdb
import polars as pl
from brutex.dashboard.db_view import query_candles
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import (
    DAILY_TF_LABEL,
    PrebuildKind,
    materialize_lake_timeframes,
)

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"

#: 09:15 IST == 03:45 UTC — the session open the resampler anchors 5m buckets on.
_SESSION_OPEN_UTC = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)


def _write_1m_bars(lake_root: Path, n: int = 120) -> list[CandleBar]:
    """Write *n* deterministic in-session 1-minute bars; return them."""
    bars = [
        CandleBar(
            timestamp=_SESSION_OPEN_UTC + timedelta(minutes=i),
            # Distinct, monotonic prices so first/max/min/last are unambiguous.
            open=Decimal(str(100.0 + i)),
            high=Decimal(str(110.0 + i)),
            low=Decimal(str(90.0 + i)),
            close=Decimal(str(105.0 + i)),
            volume=10 + i,
            open_interest=0,
        )
        for i in range(n)
    ]
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval="1minute",
        bars=bars,
    )
    return bars


def _materialize_5m(lake_root: Path) -> int:
    """Run the production glue to materialise the 5m partition; return bars written."""
    result = materialize_lake_timeframes(
        lake_root=lake_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
    )
    assert result.skipped == ()
    return result.written["5m"]


def test_5m_partition_round_trips_with_correct_aggregation(tmp_path: Path) -> None:
    one_min = _write_1m_bars(tmp_path, n=120)
    written = _materialize_5m(tmp_path)
    # 120 one-minute bars in one session → 120 / 5 = 24 five-minute buckets.
    assert written == 24

    # The 5minute partition must now exist on disk under its own interval.
    part = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "5minute"
    assert any(part.rglob("*.parquet")), "5minute partition was not written"

    # Read the derived 5m bars straight back via the production candle reader.
    conn = duckdb.connect()
    try:
        page = query_candles(
            conn,
            tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval="5minute",
            columns=("open", "high", "low", "close", "volume"),
            limit=500,
        )
    finally:
        conn.close()

    assert page["total"] == 24
    cols = page["columns"]
    oi, hi, li, ci, vi = (cols.index(c) for c in ("open", "high", "low", "close", "volume"))
    rows = page["rows"]
    assert len(rows) == 24

    # Oracle: each 5m bar aggregates its 5 consecutive 1m bars.
    for bucket, row in enumerate(rows):
        group = one_min[bucket * 5 : bucket * 5 + 5]
        assert float(row[oi]) == float(group[0].open)  # open = first
        assert float(row[hi]) == max(float(b.high) for b in group)  # high = max
        assert float(row[li]) == min(float(b.low) for b in group)  # low = min
        assert float(row[ci]) == float(group[-1].close)  # close = last
        assert int(row[vi]) == sum(b.volume for b in group)  # volume = sum


def test_materialize_is_idempotent(tmp_path: Path) -> None:
    _write_1m_bars(tmp_path, n=120)
    _materialize_5m(tmp_path)

    part_dir = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "5minute"
    files = sorted(part_dir.rglob("*.parquet"))
    first_bytes = {p: p.read_bytes() for p in files}

    # Re-run: same 1m source → the INCREMENTAL stamp skips the whole derive
    # (DEC-PREBUILD-INCREMENTAL-001; the lake state is byte-identical below).
    again = materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
    )
    assert again.unchanged is True
    assert again.written == {}

    # A FORCED full re-run (incremental=False) is still idempotent: the writer
    # merges the same 24 rows and the files stay byte-identical.
    forced = materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
        incremental=False,
    )
    assert forced.unchanged is False
    assert forced.written["5m"] == 24  # the writer still reports the rows it merged

    files_after = sorted(part_dir.rglob("*.parquet"))
    assert files_after == files, "idempotent re-run must not add partition files"
    for p in files_after:
        assert p.read_bytes() == first_bytes[p], "idempotent re-run must be byte-identical"

    # And the readable row count is unchanged (no duplication).
    conn = duckdb.connect()
    try:
        page = query_candles(
            conn,
            tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval="5minute",
            columns=("open",),
            limit=500,
        )
    finally:
        conn.close()
    assert page["total"] == 24


# ── FUTURES OI round-trip (DEC-PREBUILD-SELECT-001) ───────────────────────────

_FNO_EXCHANGE = "NSE"
_FNO_SEGMENT = "FNO"
_FNO_SYMBOL = "NSE-NIFTY-30Jan25-FUT"


def _write_fno_1m_bars(lake_root: Path, n: int = 30) -> list[CandleBar]:
    """Write *n* F&O 1-minute bars carrying open_interest (a null OI mid-run)."""
    bars = [
        CandleBar(
            timestamp=_SESSION_OPEN_UTC + timedelta(minutes=i),
            open=Decimal(str(100.0 + i)),
            high=Decimal(str(110.0 + i)),
            low=Decimal(str(90.0 + i)),
            close=Decimal(str(105.0 + i)),
            volume=10 + i,
            # A real OI that climbs, with a deliberate null OI at minute 7 to prove
            # the present-but-null bucket value is handled (→ 0), never a crash.
            open_interest=None if i == 7 else 1000 + 5 * i,
        )
        for i in range(n)
    ]
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(
        exchange=_FNO_EXCHANGE,
        segment=_FNO_SEGMENT,
        symbol=_FNO_SYMBOL,
        interval="1minute",
        bars=bars,
    )
    return bars


def test_futures_oi_preserved_through_materialize(tmp_path: Path) -> None:
    """The OI-aware FUTURES path round-trips open_interest into the derived 5m bars.

    Drives the REAL ``make_lake_read_1m(with_open_interest=True)`` +
    ``make_lake_write_tf`` (the production lake glue) via the shared
    ``materialize_lake_timeframes`` FUTURES path, then reads the derived 5minute
    F&O partition back and asserts each bucket carries the carry-last OI of its
    five source minutes — NOT 0 (the spot-only OI-drop this fix closes), and not
    a crash on the null-OI minute.
    """
    src = _write_fno_1m_bars(tmp_path, n=30)
    result = materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_FNO_EXCHANGE,
        segment=_FNO_SEGMENT,
        symbol=_FNO_SYMBOL,
        kind=PrebuildKind.FUTURES,
        tfs=("5m",),
    )
    assert result.skipped == ()
    assert result.written["5m"] == 6  # 30 / 5 buckets

    conn = duckdb.connect()
    try:
        page = query_candles(
            conn,
            tmp_path,
            exchange=_FNO_EXCHANGE,
            segment=_FNO_SEGMENT,
            symbol=_FNO_SYMBOL,
            interval="5minute",
            columns=("close", "open_interest"),
            limit=500,
        )
    finally:
        conn.close()

    assert page["total"] == 6
    cols = page["columns"]
    assert "open_interest" in cols, "the derived F&O partition must carry an OI column"
    oi_idx = cols.index("open_interest")
    rows = page["rows"]
    # Oracle: each 5m bucket's OI = the last source minute's OI in that bucket
    # (carry-last snapshot); the null-OI minute (index 7, in bucket 1) does NOT
    # poison its bucket — that bucket's last minute (index 9) has a real OI.
    for bucket, row in enumerate(rows):
        group = src[bucket * 5 : bucket * 5 + 5]
        last_oi = group[-1].open_interest
        expected = 0 if last_oi is None else last_oi
        assert int(row[oi_idx]) == expected, f"bucket {bucket} OI not carried"
    # Sanity: at least one bucket carries a real non-zero OI (proves not all-zero).
    assert any(int(r[oi_idx]) > 0 for r in rows)


# ── OPTIONS materialisation + daily-in-prebuild (DEC-PIPELINE-AUTOPREBUILD-001) ──

_OPT_SYMBOL = "NSE-NIFTY-30Jan25-20000-CE"


def _write_option_1m_bars(lake_root: Path, n: int = 30) -> list[CandleBar]:
    """Write *n* OPTION 1-minute bars carrying open_interest."""
    bars = [
        CandleBar(
            timestamp=_SESSION_OPEN_UTC + timedelta(minutes=i),
            open=Decimal(str(50.0 + i)),
            high=Decimal(str(55.0 + i)),
            low=Decimal(str(45.0 + i)),
            close=Decimal(str(52.0 + i)),
            volume=20 + i,
            open_interest=2000 + 7 * i,
        )
        for i in range(n)
    ]
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(
        exchange=_FNO_EXCHANGE,
        segment=_FNO_SEGMENT,
        symbol=_OPT_SYMBOL,
        interval="1minute",
        bars=bars,
    )
    return bars


def test_options_materialize_writes_intraday_and_daily_with_oi(tmp_path: Path) -> None:
    """REGRESSION FLIP (DEC-PIPELINE-AUTOPREBUILD-001): OPTIONS now materialise.

    The old pin was ``pytest.raises(ValueError, match="on-demand")`` — options
    were never materialised. Now an option contract's 1-minute round-trips
    through the OI-aware F&O path into a derived 5minute partition AND (with
    ``include_daily=True``) a derived carry-last-OI ``1day`` partition.
    """
    src = _write_option_1m_bars(tmp_path, n=30)
    result = materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_FNO_EXCHANGE,
        segment=_FNO_SEGMENT,
        symbol=_OPT_SYMBOL,
        kind=PrebuildKind.OPTIONS,
        tfs=("5m",),
        include_daily=True,
        daily_before=datetime(2021, 6, 1, tzinfo=UTC).date(),
    )
    assert result.skipped == ()
    assert result.written["5m"] == 6  # 30 / 5 buckets
    assert result.written[DAILY_TF_LABEL] == 1  # one complete session → one daily bar

    # Intraday OI preserved (the OI-aware F&O resampler, same as futures).
    part_5m = tmp_path / "bars" / _FNO_EXCHANGE / _FNO_SEGMENT / _OPT_SYMBOL / "5minute"
    df_5m = pl.read_parquet(part_5m.as_posix() + "/**/*.parquet").sort("timestamp")
    assert df_5m.height == 6
    expected_bucket_oi = [src[b * 5 + 4].open_interest for b in range(6)]
    assert df_5m.get_column("open_interest").to_list() == expected_bucket_oi

    # Daily carries the LAST-of-day OI (carry-last snapshot, never summed).
    part_1d = tmp_path / "bars" / _FNO_EXCHANGE / _FNO_SEGMENT / _OPT_SYMBOL / "1day"
    df_1d = pl.read_parquet(part_1d.as_posix() + "/**/*.parquet")
    assert df_1d.height == 1
    assert df_1d.get_column("open_interest").to_list() == [src[-1].open_interest]


def test_spot_include_daily_writes_midnight_ist_daily(tmp_path: Path) -> None:
    """SPOT ``include_daily=True`` derives the midnight-IST spot daily (OI null path → 0-stored)."""
    _write_1m_bars(tmp_path, n=120)
    result = materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
        include_daily=True,
        daily_before=datetime(2021, 6, 1, tzinfo=UTC).date(),
    )
    assert result.written["5m"] == 24
    assert result.written[DAILY_TF_LABEL] == 1
    part_1d = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1day"
    df_1d = pl.read_parquet(part_1d.as_posix() + "/**/*.parquet")
    assert df_1d.height == 1
    # Midnight-IST stamp == 18:30 UTC of the PREVIOUS calendar day (the vendor identity).
    ts = df_1d.get_column("timestamp").to_list()[0]
    assert (ts.hour, ts.minute) == (18, 30)


def test_futures_include_daily_and_second_run_adds_zero_net_new(tmp_path: Path) -> None:
    """FUTURES daily-in-prebuild + the rerun-safety pin (CLAUDE.md §2 Rule 7):
    a second identical materialise adds ZERO net-new bars — the intraday
    partitions are keep-first byte-identical and the latest-wins derived daily
    is a silent no-op on identical values."""
    _write_fno_1m_bars(tmp_path, n=30)
    before = datetime(2021, 6, 1, tzinfo=UTC).date()

    def _run() -> dict[str, int]:
        result = materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange=_FNO_EXCHANGE,
            segment=_FNO_SEGMENT,
            symbol=_FNO_SYMBOL,
            kind=PrebuildKind.FUTURES,
            tfs=("5m",),
            include_daily=True,
            daily_before=before,
        )
        return dict(result.written)

    first = _run()
    assert first["5m"] == 6
    assert first[DAILY_TF_LABEL] == 1

    contract_root = tmp_path / "bars" / _FNO_EXCHANGE / _FNO_SEGMENT / _FNO_SYMBOL
    files = sorted(p for p in contract_root.rglob("*.parquet"))
    bytes_before = {p: p.read_bytes() for p in files}
    rows_before = {
        p: pl.read_parquet(p.as_posix()).height for p in files if "1minute" not in str(p)
    }

    _run()  # second, identical materialise

    files_after = sorted(p for p in contract_root.rglob("*.parquet"))
    assert files_after == files, "a rerun must not add partition files"
    for p in files_after:
        assert p.read_bytes() == bytes_before[p], f"{p} changed on an idempotent rerun"
    for p, rows in rows_before.items():
        assert pl.read_parquet(p.as_posix()).height == rows  # 0 net-new bars


def test_include_daily_default_before_covers_none_branch(tmp_path: Path) -> None:
    """``daily_before=None`` (production) resolves to today IST — the 2021
    fixture sessions are all complete, so the daily still derives."""
    _write_1m_bars(tmp_path, n=120)
    result = materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
        include_daily=True,
    )
    assert result.written[DAILY_TF_LABEL] == 1
