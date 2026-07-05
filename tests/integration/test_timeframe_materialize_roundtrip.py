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

from datetime import UTC, date, datetime, timedelta
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
    a second identical materialise adds ZERO net-new bars — the derived
    intraday partitions AND the derived daily are both latest-wins
    (DEC-TF-REMAT-LATEST-WINS-001), a silent byte-identical no-op on
    identical values."""
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


# ── Stale partial-day bucket un-freeze (DEC-TF-REMAT-LATEST-WINS-001) ─────────


def _read_5m_frame(lake_root: Path, exchange: str, segment: str, symbol: str) -> pl.DataFrame:
    part = lake_root / "bars" / exchange / segment / symbol / "5minute"
    return pl.read_parquet(part.as_posix() + "/**/*.parquet").sort("timestamp")


def test_rematerialize_replaces_stale_partial_day_bucket(tmp_path: Path) -> None:
    """REGRESSION (DEC-TF-REMAT-LATEST-WINS-001, audit risk #3): a partial-day
    bucket materialised mid-day must NOT freeze.

    Before the fix the derived intraday write was keep-first, so a 5m bucket
    derived from only its first 3 source minutes was stored, and the next
    re-materialise — which recomputed the CORRECT full-bucket bar — hit the
    keep-first conflict and was DISCARDED (the stale bucket froze forever).
    Now the derived intraday write is latest-wins (mirroring the derived
    ``1day``): the recomputed bucket replaces the stale one, and a further
    re-materialise of unchanged data is byte-identical (idempotence pin).
    """
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
    all_bars = [
        CandleBar(
            timestamp=_SESSION_OPEN_UTC + timedelta(minutes=i),
            open=Decimal(str(100.0 + i)),
            high=Decimal(str(110.0 + i)),
            low=Decimal(str(90.0 + i)),
            close=Decimal(str(105.0 + i)),
            volume=10 + i,
            open_interest=0,
        )
        for i in range(5)
    ]

    # Mid-day pull: only the first 3 minutes of the 5m bucket exist yet.
    sink.append(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval="1minute",
        bars=all_bars[:3],
    )
    first = materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
        incremental=False,
    )
    assert first.written["5m"] == 1
    partial = _read_5m_frame(tmp_path, _EXCHANGE, _SEGMENT, _SYMBOL)
    assert partial.height == 1
    assert float(partial.get_column("close")[0]) == float(all_bars[2].close)
    assert int(partial.get_column("volume")[0]) == sum(b.volume for b in all_bars[:3])

    # The rest of the day lands; the bucket's TRUE bar now spans all 5 minutes.
    sink.append(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval="1minute",
        bars=all_bars[3:],
    )
    second = materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
        incremental=False,
    )
    assert second.written["5m"] == 1
    full = _read_5m_frame(tmp_path, _EXCHANGE, _SEGMENT, _SYMBOL)
    assert full.height == 1, "still exactly one 5m bucket — replaced, never duplicated"
    # The bucket now reflects the FULL day (fails before the fix: keep-first
    # discarded the recompute and the stale 3-minute bucket froze).
    assert float(full.get_column("close")[0]) == float(all_bars[-1].close)
    assert float(full.get_column("high")[0]) == max(float(b.high) for b in all_bars)
    assert float(full.get_column("low")[0]) == min(float(b.low) for b in all_bars)
    assert int(full.get_column("volume")[0]) == sum(b.volume for b in all_bars)

    # Idempotence pin: a third materialise over unchanged 1-minute data is a
    # byte-identical no-op (CLAUDE.md §2 Rule 7).
    part_dir = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "5minute"
    files = sorted(part_dir.rglob("*.parquet"))
    bytes_before = {p: p.read_bytes() for p in files}
    third = materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
        incremental=False,
    )
    assert third.written["5m"] == 1
    files_after = sorted(part_dir.rglob("*.parquet"))
    assert files_after == files
    for p in files_after:
        assert p.read_bytes() == bytes_before[p], f"{p} changed on an idempotent rerun"


def test_vendor_1minute_base_stays_keep_first(tmp_path: Path) -> None:
    """PIN (DEC-TF-REMAT-LATEST-WINS-001 scope guard): the VENDOR 1-minute base
    keeps its keep-first identity — latest-wins applies ONLY to the DERIVED
    intraday partitions written by ``make_lake_write_tf``. A conflicting
    re-append of the same 1-minute timestamp must keep the STORED bar
    (DEC-LAKE-CONFLICT-KEEPFIRST-001, reproducibility per CLAUDE.md §2 Rule 4).
    """
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
    original = CandleBar(
        timestamp=_SESSION_OPEN_UTC,
        open=Decimal("100.0"),
        high=Decimal("110.0"),
        low=Decimal("90.0"),
        close=Decimal("105.0"),
        volume=10,
        open_interest=0,
    )
    sink.append(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval="1minute",
        bars=[original],
    )
    conflicting = CandleBar(
        timestamp=_SESSION_OPEN_UTC,
        open=Decimal("999.0"),
        high=Decimal("999.0"),
        low=Decimal("999.0"),
        close=Decimal("999.0"),
        volume=999,
        open_interest=0,
    )
    result = sink.append(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval="1minute",
        bars=[conflicting],
    )
    assert result.bars_net_new == 0, "keep-first: the conflicting bar adds nothing"
    part = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1minute"
    df = pl.read_parquet(part.as_posix() + "/**/*.parquet")
    assert df.height == 1
    assert float(df.get_column("close")[0]) == 105.0, "stored vendor bar must win"


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


# ────────────────────────────────────────────────────────────────────────
# DEC-PREBUILD-GIL-VECTORIZE-001 — the batched frame→CandleBar conversion
# (`frame_to_candle_bars` + the vectorized spot-daily filter/re-stamp) must
# produce a BYTE-IDENTICAL lake vs the verbatim legacy per-row loops.
# ────────────────────────────────────────────────────────────────────────


def _legacy_bars_from_frame(frame: pl.DataFrame) -> list[CandleBar]:
    """Verbatim pre-change `make_lake_write_tf._write` per-row conversion."""
    has_oi = "open_interest" in frame.columns

    def _oi(row: dict[str, object]) -> int:
        if not has_oi:
            return 0
        value = row["open_interest"]
        return 0 if value is None else int(value)  # type: ignore[call-overload]

    return [
        CandleBar(
            timestamp=row["ts_utc"],
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=int(row["volume"]),
            open_interest=_oi(row),
        )
        for row in frame.iter_rows(named=True)
    ]


def _legacy_spot_daily_bars(df_1m: pl.DataFrame, *, before) -> list[CandleBar]:  # type: ignore[no-untyped-def]
    """Verbatim pre-change `resample_1m_to_daily` per-row filter + re-stamp."""
    from datetime import timezone

    from brutex.data.tf_resampler import resample_1m_to_tf
    from brutex.data.timeframes import DAILY_TF_MINUTES

    ist = timezone(timedelta(hours=5, minutes=30))
    if df_1m.is_empty():
        return []
    daily = resample_1m_to_tf(df_1m, DAILY_TF_MINUTES)
    bars: list[CandleBar] = []
    for row in daily.iter_rows(named=True):
        ist_date = row["ts_utc"].astimezone(ist).date()
        if ist_date >= before:
            continue
        stamp_utc = datetime(ist_date.year, ist_date.month, ist_date.day, tzinfo=ist).astimezone(
            UTC
        )
        bars.append(
            CandleBar(
                timestamp=stamp_utc,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row["volume"]),
                open_interest=None,
            )
        )
    return bars


def _hash_lake_parquet(lake_root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(lake_root)): p.read_bytes() for p in sorted(lake_root.rglob("*.parquet"))
    }


def _seed_boundary_1m(lake_root: Path, *, segment: str, symbol: str, with_oi: bool) -> None:
    """Seed a month-boundary + IST-date-roll 1-minute partition (2 UTC months)."""
    bars: list[CandleBar] = []
    for day_start, base in (
        (datetime(2021, 1, 29, 3, 45, tzinfo=UTC), Decimal("14020.75")),
        (datetime(2021, 2, 1, 3, 45, tzinfo=UTC), Decimal("14100.35")),
    ):
        for i in range(75):
            px = base + Decimal(i % 37) * Decimal("0.05")
            oi = None if (with_oi and i % 9 == 0) else (i * 3 if with_oi else 0)
            bars.append(
                CandleBar(
                    timestamp=day_start + timedelta(minutes=i),
                    open=px,
                    high=px + Decimal("1.35"),
                    low=px - Decimal("2.15"),
                    close=px + Decimal("0.6"),
                    volume=10 + 7 * i,
                    open_interest=oi,
                )
            )
    # An 18:31 UTC bar: its IST calendar date ROLLS to the next day (00:01 IST).
    bars.append(
        CandleBar(
            timestamp=datetime(2021, 1, 31, 18, 31, tzinfo=UTC),
            open=Decimal("14022.8"),
            high=Decimal("14023.15"),
            low=Decimal("14021.7"),
            close=Decimal("14022.95"),
            volume=6,
            open_interest=0,
        )
    )
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(exchange="NSE", segment=segment, symbol=symbol, interval="1minute", bars=bars)


@pytest.mark.parametrize(
    ("kind", "segment", "symbol", "with_oi"),
    [
        (PrebuildKind.SPOT, "INDEX", "NIFTY", False),
        (PrebuildKind.FUTURES, "FNO", "NIFTY-Jul2021-FUT", True),
    ],
)
def test_vectorized_conversion_writes_byte_identical_lake(
    tmp_path: Path, kind: PrebuildKind, segment: str, symbol: str, with_oi: bool
) -> None:
    """Golden byte-equality: production (batched) lake == legacy (per-row) lake.

    Seeds the SAME month-boundary/IST-roll 1-minute data into two lakes; lake A
    is materialised by the production `materialize_lake_timeframes` (the batched
    `frame_to_candle_bars` path, incl. the vectorized spot daily); lake B is
    written through the SAME sink but with bars built by the VERBATIM legacy
    per-row loops. Every derived parquet must be byte-for-byte identical
    (DEC-PREBUILD-GIL-VECTORIZE-001, Golden Rule #7).
    """
    from brutex.data.timeframe_materialize import (
        INTRADAY_TF_INTERVALS,
        daily_resample_fn_for,
        intraday_resampler_for,
        make_lake_read_1m,
    )

    before = date(2026, 7, 4)
    lake_new = tmp_path / "new"
    lake_legacy = tmp_path / "legacy"
    for lake in (lake_new, lake_legacy):
        _seed_boundary_1m(lake, segment=segment, symbol=symbol, with_oi=with_oi)

    # Production path (batched conversion).
    result = materialize_lake_timeframes(
        lake_root=lake_new,
        exchange="NSE",
        segment=segment,
        symbol=symbol,
        kind=kind,
        include_daily=True,
        daily_before=before,
    )
    assert result.written[DAILY_TF_LABEL] >= 1

    # Legacy path: same read + same resamplers + same sink, per-row conversion.
    df_1m = make_lake_read_1m(
        lake_root=lake_legacy,
        exchange="NSE",
        segment=segment,
        symbol=symbol,
        with_open_interest=kind is not PrebuildKind.SPOT,
    )()
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_legacy))
    for tf, frame in intraday_resampler_for(kind)(df_1m).items():
        interval = INTRADAY_TF_INTERVALS.get(tf)
        if interval is None:
            continue
        sink.append(
            exchange="NSE",
            segment=segment,
            symbol=symbol,
            interval=interval,
            bars=_legacy_bars_from_frame(frame),
        )
    if kind is PrebuildKind.SPOT:
        daily_bars = _legacy_spot_daily_bars(df_1m, before=before)
    else:
        # The F&O daily derivation was NOT touched by the vectorization — the
        # real resampler IS the legacy behaviour for this leg.
        daily_bars = daily_resample_fn_for(kind)(df_1m, before)
    sink.append(
        exchange="NSE",
        segment=segment,
        symbol=symbol,
        interval="1day",
        bars=daily_bars,
        merge_mode="latest-wins",
    )

    new_files = _hash_lake_parquet(lake_new)
    legacy_files = _hash_lake_parquet(lake_legacy)
    assert new_files.keys() == legacy_files.keys()
    for rel, content in legacy_files.items():
        assert new_files[rel] == content, f"derived partition diverged: {rel}"


def test_prebuild_log_volume_is_o_symbols_not_o_partitions(tmp_path: Path) -> None:
    """DEC-PREBUILD-LOG-FLOOD-001 pin: a prebuild of N derived partitions emits
    O(symbols) INFO lines, never O(partitions).

    The 2026-07-04/05 forensics measured 205,609 per-partition
    ``lake.partition.write`` INFO lines (~132/s) from the prebuild — rolling
    the 10 MiB × 5 log rotation over in ~26 min and WIPING the fetch phase's
    rate/429/window-settle audit trail. On the DERIVED write path that event
    now rides DEBUG (same name + fields), and the surviving INFO record is ONE
    per-symbol ``lake.derived.write_summary`` aggregate (timeframe count +
    total bars)."""
    from structlog.testing import capture_logs

    _write_1m_bars(tmp_path, n=120)
    with capture_logs() as logs:
        result = materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            kind=PrebuildKind.SPOT,
            tfs=("5m", "15m", "30m"),
            include_daily=True,
            daily_before=date(2021, 1, 5),
            incremental=False,
        )
    partition_writes = [e for e in logs if e["event"] == "lake.partition.write"]
    # Multiple derived partitions were written (3 intraday TFs + the 1day)…
    assert len(partition_writes) >= 4
    # …but EVERY per-partition event is DEBUG on the derived path — zero INFO.
    assert all(e["log_level"] == "debug" for e in partition_writes)
    # Exactly ONE per-symbol aggregate INFO carries the forensic fields.
    summaries = [e for e in logs if e["event"] == "lake.derived.write_summary"]
    assert len(summaries) == 1
    assert summaries[0]["log_level"] == "info"
    assert summaries[0]["symbol"] == _SYMBOL
    assert summaries[0]["timeframes_written"] == len(result.written) == 4
    assert summaries[0]["total_bars"] == result.total_bars
    assert summaries[0]["include_daily"] is True


def test_intraday_only_materialize_also_emits_one_summary(tmp_path: Path) -> None:
    """The non-daily return path emits the SAME single per-symbol aggregate
    (DEC-PREBUILD-LOG-FLOOD-001 — both completion paths are O(symbols))."""
    from structlog.testing import capture_logs

    _write_1m_bars(tmp_path, n=120)
    with capture_logs() as logs:
        _materialize_5m(tmp_path)
    summaries = [e for e in logs if e["event"] == "lake.derived.write_summary"]
    assert len(summaries) == 1
    assert summaries[0]["log_level"] == "info"
    assert summaries[0]["include_daily"] is False
    assert summaries[0]["timeframes_written"] == 1
    assert [e["log_level"] for e in logs if e["event"] == "lake.partition.write"] == ["debug"]
