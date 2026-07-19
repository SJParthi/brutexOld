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
            # a mid-bucket null never poisons the bucket (its carry-last minute 9
            # has a REAL OI) and never crashes the materialise.
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
    # poison its bucket — that bucket's last minute (index 9) has a real OI, so
    # every bucket here reads a REAL int (the all-null bucket shape is pinned by
    # test_futures_null_last_oi_bucket_stored_null_not_zero below).
    for bucket, row in enumerate(rows):
        group = src[bucket * 5 : bucket * 5 + 5]
        last_oi = group[-1].open_interest
        assert last_oi is not None  # this fixture's buckets all end on a real OI
        assert int(row[oi_idx]) == last_oi, f"bucket {bucket} OI not carried"
    # Sanity: at least one bucket carries a real non-zero OI (proves not all-zero).
    assert any(int(r[oi_idx]) > 0 for r in rows)


_FNO_NULL_OI_SYMBOL = "NSE-BANKNIFTY-30Jan25-FUT"


def test_futures_null_last_oi_bucket_stored_null_not_zero(tmp_path: Path) -> None:
    """A bucket whose carry-last OI is NULL stores ``None`` — never a fabricated 0.

    DEC-PREOPEN-DAY-FRESHNESS-001 (the B-3 fold): the derived-intraday writer
    previously mapped a present-but-null bucket OI to a stored ``0``, diverging
    from the F&O DAILY derive (``resample_1m_to_fno_daily`` stores ``None``)
    and from the canonical resampler's own "null OI is preserved, NEVER
    fabricated as 0" law. Bucket 1's five source minutes (5–9) all carry a
    null OI, so its carry-last bucket OI is null — the derived 5m partition
    must store NULL for it (and real ints for every other bucket).
    Fails-before: the pre-fix writer stored ``0`` for bucket 1.
    """
    bars = [
        CandleBar(
            timestamp=_SESSION_OPEN_UTC + timedelta(minutes=i),
            open=Decimal(str(100.0 + i)),
            high=Decimal(str(110.0 + i)),
            low=Decimal(str(90.0 + i)),
            close=Decimal(str(105.0 + i)),
            volume=10 + i,
            # The WHOLE second bucket (minutes 5-9) is null-OI, so its
            # carry-last bucket OI is genuinely null.
            open_interest=None if 5 <= i <= 9 else 2000 + 3 * i,
        )
        for i in range(15)
    ]
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
    sink.append(
        exchange=_FNO_EXCHANGE,
        segment=_FNO_SEGMENT,
        symbol=_FNO_NULL_OI_SYMBOL,
        interval="1minute",
        bars=bars,
    )
    result = materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_FNO_EXCHANGE,
        segment=_FNO_SEGMENT,
        symbol=_FNO_NULL_OI_SYMBOL,
        kind=PrebuildKind.FUTURES,
        tfs=("5m",),
    )
    assert result.skipped == ()
    assert result.written["5m"] == 3  # 15 / 5 buckets

    conn = duckdb.connect()
    try:
        page = query_candles(
            conn,
            tmp_path,
            exchange=_FNO_EXCHANGE,
            segment=_FNO_SEGMENT,
            symbol=_FNO_NULL_OI_SYMBOL,
            interval="5minute",
            columns=("close", "open_interest"),
            limit=500,
        )
    finally:
        conn.close()

    assert page["total"] == 3
    oi_idx = page["columns"].index("open_interest")
    stored = [None if row[oi_idx] is None else int(row[oi_idx]) for row in page["rows"]]
    # Bucket 0 carry-last = minute 4 (2012); bucket 1 = minute 9 (NULL — the
    # aligned law, never 0); bucket 2 = minute 14 (2042).
    assert stored == [2000 + 3 * 4, None, 2000 + 3 * 14]


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


# ── month-bounded rebuild byte-equality (DEC-TF-REBUILD-MONTH-BOUND-001) ──────

#: Three real NSE sessions spanning a UTC month boundary: Fri 2021-01-29,
#: Mon 2021-02-01 (the IST 1st-of-month session — the boundary case the
#: session-never-crosses-UTC-month invariant must hold on) and Mon 2021-03-01.
_MONTH_SESSIONS = (
    datetime(2021, 1, 29, 3, 45, tzinfo=UTC),
    datetime(2021, 2, 1, 3, 45, tzinfo=UTC),
    datetime(2021, 3, 1, 3, 45, tzinfo=UTC),
)


def _write_multi_month_lake(lake_root: Path, *, bars_per_session: int = 60) -> None:
    """Write one 1-minute session per month partition (2021/01 + /02 + /03)."""
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    for base, session_open in enumerate(_MONTH_SESSIONS):
        bars = [
            CandleBar(
                timestamp=session_open + timedelta(minutes=i),
                open=Decimal(str(100.0 + base * 500 + i)),
                high=Decimal(str(110.0 + base * 500 + i)),
                low=Decimal(str(90.0 + base * 500 + i)),
                close=Decimal(str(105.0 + base * 500 + i)),
                volume=10 + base * 100 + i,
                open_interest=0,
            )
            for i in range(bars_per_session)
        ]
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval="1minute",
            bars=bars,
        )


def _tf_month_file(lake_root: Path, month: int) -> Path:
    return (
        lake_root
        / "bars"
        / _EXCHANGE
        / _SEGMENT
        / _SYMBOL
        / "5minute"
        / "2021"
        / f"{month:02d}.parquet"
    )


def test_month_bounded_rebuild_is_byte_identical_to_full(tmp_path: Path) -> None:
    """DEC-TF-REBUILD-MONTH-BOUND-001 byte-equality oracle: rebuilding ONE
    deleted TF month with ``months={(y, m)}`` reproduces the EXACT bytes the
    whole-tree materialise wrote — incl. the Feb month whose session is the IST
    1st-of-month boundary — and leaves every other month's file untouched."""
    _write_multi_month_lake(tmp_path)
    result = materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
    )
    assert result.written["5m"] == 36  # 3 sessions × 60 / 5
    baseline = {m: _tf_month_file(tmp_path, m).read_bytes() for m in (1, 2, 3)}

    # Heal-style targeted rebuild: delete the boundary (Feb) month, re-derive
    # ONLY it from its own 1-minute month partition.
    _tf_month_file(tmp_path, 2).unlink()
    rebuilt = materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
        months=[(2021, 2)],
    )
    assert rebuilt.written["5m"] == 12  # ONLY Feb's buckets — the bounded read
    for month in (1, 2, 3):
        assert _tf_month_file(tmp_path, month).read_bytes() == baseline[month], (
            f"2021/{month:02d} diverged from the full-rebuild bytes"
        )


def test_month_bounded_read_never_touches_other_months(tmp_path: Path) -> None:
    """TEETH: corrupt the JANUARY 1-minute file, then rebuild FEBRUARY with
    ``months={(2021, 2)}`` — the bounded read succeeds byte-identically while
    the old whole-tree ``/**/*.parquet`` scan would have raised on the corrupt
    January file (proves the read genuinely never opens other months)."""
    _write_multi_month_lake(tmp_path)
    full = materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
    )
    assert full.written["5m"] == 36
    feb_baseline = _tf_month_file(tmp_path, 2).read_bytes()

    jan_1m = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1minute" / "2021" / "01.parquet"
    jan_1m.write_bytes(b"not a parquet file")
    # W-6a: the native read surfaces the corrupt file as the typed
    # quarantining ResampleInputError (the endgame read-isolation class).
    from brutex.data.errors import ResampleInputError

    with pytest.raises(ResampleInputError):  # the whole-tree scan trips on Jan
        materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
        )

    _tf_month_file(tmp_path, 2).unlink()
    rebuilt = materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
        months=[(2021, 2)],
    )
    assert rebuilt.written["5m"] == 12
    assert _tf_month_file(tmp_path, 2).read_bytes() == feb_baseline


def test_months_matching_nothing_on_disk_raises(tmp_path: Path) -> None:
    """A months set with NO existing partition keeps the loud 'no stored
    1minute bars' ValueError contract (mirrors the empty-tree raise)."""
    _write_multi_month_lake(tmp_path)
    with pytest.raises(ValueError, match="no stored 1minute bars"):
        materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
            months=[(2030, 1)],
        )


# ── session-close-aware default cutoff (DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001) ──


def test_default_cutoff_is_session_close_aware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``daily_before=None`` (production) the default cutoff is
    session-close-aware: a MID-SESSION run on the run day defers its own
    still-forming session, a POST-CLOSE run derives it. Drives the REAL
    :func:`brutex.calendars.nse.daily_derive_cutoff` through the REAL wiring —
    ONLY the clock seam (``_now_ist``) is frozen, never the cutoff law."""
    from brutex.calendars.nse import IST
    from brutex.data import timeframe_materialize as tfm

    _write_1m_bars(tmp_path, n=120)  # one full session on Mon 2021-01-04
    run_day_noon = datetime(2021, 1, 4, 12, 0, tzinfo=IST)
    run_day_evening = datetime(2021, 1, 4, 16, 0, tzinfo=IST)

    def _run() -> int:
        result = materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
            include_daily=True,
        )
        return result.written[DAILY_TF_LABEL]

    # MID-SESSION run: the run day's session is still forming → deferred.
    monkeypatch.setattr(tfm, "_now_ist", lambda: run_day_noon)
    assert _run() == 0

    # POST-CLOSE run the SAME day: the session provably closed → derived NOW
    # (pre-DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001 this stayed 0 until the next
    # calendar day's pipeline run — the whole-day-granular clause).
    monkeypatch.setattr(tfm, "_now_ist", lambda: run_day_evening)
    assert _run() == 1


# ────────────────────────────────────────────────────────────────────────
# DEC-PREBUILD-GIL-VECTORIZE-001 — the batched frame→CandleBar conversion
# (`frame_to_candle_bars` + the vectorized spot-daily filter/re-stamp) must
# produce a BYTE-IDENTICAL lake vs the verbatim legacy per-row loops.
# ────────────────────────────────────────────────────────────────────────


def _legacy_bars_from_frame(frame: pl.DataFrame) -> list[CandleBar]:
    """The pre-vectorize `make_lake_write_tf._write` per-row conversion oracle.

    Verbatim EXCEPT the null-OI arm (DEC-PREOPEN-DAY-FRESHNESS-001, the B-3
    fold — a CONSCIOUS oracle update): a present-but-null bucket OI on an
    OI-carrying frame now stores ``None`` (aligned with the F&O daily derive
    + the canonical resampler's "null OI preserved, NEVER fabricated as 0"
    law); the pre-fold oracle's ``0`` WAS the ledgered divergence. The
    absent-column spot arm keeps the legacy stored-``0`` convention.
    """
    has_oi = "open_interest" in frame.columns

    def _oi(row: dict[str, object]) -> int | None:
        if not has_oi:
            return 0
        value = row["open_interest"]
        return None if value is None else int(value)  # type: ignore[call-overload]

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


# ────────────────────────────────────────────────────────────────────────
# Corrupt 1-minute input is QUARANTINED, never laundered
# (DEC-STORED-DATA-VALIDATION-001)
# ────────────────────────────────────────────────────────────────────────


def _plant_corrupt_1m(lake_root: Path, symbol: str) -> None:
    """Raw-plant a 1-minute partition holding one high<low poison bar.

    Raw pyarrow — the fixed writer refuses corrupt bars, exactly like a
    legacy poisoned lake (the #1067 convention).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest = (
        lake_root
        / "bars"
        / _EXCHANGE
        / _SEGMENT
        / symbol
        / "1minute"
        / f"{_SESSION_OPEN_UTC.year:04d}"
        / f"{_SESSION_OPEN_UTC.month:02d}.parquet"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    timestamps = [_SESSION_OPEN_UTC + timedelta(minutes=i) for i in range(3)]
    pq.write_table(
        pa.table(
            {
                "timestamp": pa.array(timestamps, type=pa.timestamp("us", tz="UTC")),
                "open": pa.array([100.0, 100.0, 100.0], type=pa.float64()),
                # Bar 1 is the audit's live repro: high(1.0) < low(50.0),
                # close 0.0 — previously LAUNDERED into a self-consistent
                # derived bucket emitting low: 0.0 across all 19 TFs + 1day.
                "high": pa.array([110.0, 1.0, 110.0], type=pa.float64()),
                "low": pa.array([90.0, 50.0, 90.0], type=pa.float64()),
                "close": pa.array([105.0, 0.0, 105.0], type=pa.float64()),
                "volume": pa.array([10, 10, 10], type=pa.int64()),
                "open_interest": pa.array([None, None, None], type=pa.int64()),
            }
        ),
        dest,
    )


def test_corrupt_1m_partition_refuses_to_derive(tmp_path: Path) -> None:
    """FAILS-BEFORE: the corrupt bar resampled into clean-looking 5m buckets."""
    from brutex.data.errors import ResampleInputError

    _plant_corrupt_1m(tmp_path, _SYMBOL)
    with pytest.raises(ResampleInputError, match="price-insane") as excinfo:
        materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
        )
    assert f"{_EXCHANGE}/{_SEGMENT}/{_SYMBOL}/1minute" in str(excinfo.value)
    # No derived partition was written — the corruption did not launder.
    assert not (tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "5minute").exists()


def test_prebuild_quarantines_corrupt_symbol_and_names_it(tmp_path: Path) -> None:
    """The pipeline prebuild quarantines the corrupt symbol via the EXISTING
    per-symbol machinery (ResampleInputError is already in
    ``gated_prebuild._per_symbol_data_errors``): the clean sibling still
    builds, the corrupt one is NAMED in ``failed``, and its derived TFs are
    NOT written."""
    from brutex.dashboard.fno_cascade import build_fno_cascade
    from brutex.dashboard.pipeline_prebuild import PrebuildTarget, run_pipeline_prebuild
    from brutex.data.timeframe_materialize import MaterializeResult
    from brutex.models import Exchange, Instrument, InstrumentType, Segment

    def _spot(name: str) -> Instrument:
        return Instrument(
            exch_id=Exchange.NSE,
            security_id=abs(hash(name)) & 0xFFFF,
            symbol_name=name,
            display_name=name,
            instrument=InstrumentType.IDX,
            segment=Segment.INDEX,
            series=None,
            isin=None,
            underlying_symbol=None,
            underlying_security_id=None,
            lot_size=0,
            tick_size=Decimal("0.05"),
            sm_freeze_qty=None,
            sm_expiry_date=None,
            strike_price=None,
        )

    corrupt, clean = "BADSYM", "GOODSYM"
    _plant_corrupt_1m(tmp_path, corrupt)
    _write_1m_bars_for(tmp_path, clean)

    def _materialize(target: PrebuildTarget) -> MaterializeResult:
        return materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange=target.exchange,
            segment=target.segment,
            symbol=target.symbol,
            kind=target.kind,
            tfs=("5m",),
        )

    outcome = run_pipeline_prebuild(
        instruments=[_spot(corrupt), _spot(clean)],
        cascade=build_fno_cascade([]),
        materialize=_materialize,
    )
    assert outcome.ok is False
    assert outcome.failed == (corrupt,)  # the corrupt symbol is NAMED
    assert outcome.failed_count == 1
    assert outcome.symbols_built == 1  # the clean sibling still built
    # The corrupt symbol's derived TFs were NOT written; the clean one's were.
    assert not (tmp_path / "bars" / _EXCHANGE / _SEGMENT / corrupt / "5minute").exists()
    assert (tmp_path / "bars" / _EXCHANGE / _SEGMENT / clean / "5minute").exists()


def _plant_nonfinite_1m(lake_root: Path, symbol: str, *, column: str, value: float) -> None:
    """Raw-plant a 1-minute partition holding one non-finite *column* bar.

    Raw pyarrow — the writer + the vendor parse (``to_price``'s explicit
    ``is_finite`` guard) refuse non-finite prices, so a stored non-finite
    price is only ever planted/out-of-band corrupt data (the #1067 raw-plant
    convention; DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest = (
        lake_root
        / "bars"
        / _EXCHANGE
        / _SEGMENT
        / symbol
        / "1minute"
        / f"{_SESSION_OPEN_UTC.year:04d}"
        / f"{_SESSION_OPEN_UTC.month:02d}.parquet"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    timestamps = [_SESSION_OPEN_UTC + timedelta(minutes=i) for i in range(3)]
    columns: dict[str, list[float]] = {
        "open": [100.0, 100.0, 100.0],
        "high": [110.0, 110.0, 110.0],
        "low": [90.0, 90.0, 90.0],
        "close": [105.0, 105.0, 105.0],
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
def test_nonfinite_high_1m_partition_refuses_to_derive(tmp_path: Path, value: float) -> None:
    """FAILS-BEFORE (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001, defect #2): the
    pre-fix price-sanity mask was BLIND to ``high ∈ {+inf, NaN}`` — the
    assurance session executed-repro'd ``make_lake_read_1m`` RETURNING
    ``highs=[inf, ...]`` silently and the 5-minute max aggregation minting a
    derived bucket ``high = inf``. Now the read refuses loudly and NOTHING
    derived is written."""
    from brutex.data.errors import ResampleInputError

    _plant_nonfinite_1m(tmp_path, _SYMBOL, column="high", value=value)
    with pytest.raises(ResampleInputError, match="price-insane") as excinfo:
        materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
        )
    assert f"{_EXCHANGE}/{_SEGMENT}/{_SYMBOL}/1minute" in str(excinfo.value)
    # No derived partition was written — the inf/NaN did not launder.
    assert not (tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "5minute").exists()


def test_prebuild_quarantines_nonfinite_symbol_and_names_it(tmp_path: Path) -> None:
    """The pipeline prebuild quarantines the inf-HIGH symbol via the EXISTING
    per-symbol machinery (the strengthened gate raises ResampleInputError —
    already in ``gated_prebuild._per_symbol_data_errors``): the clean sibling
    still builds, the poisoned one is NAMED in ``failed``, no derived TFs."""
    from brutex.dashboard.fno_cascade import build_fno_cascade
    from brutex.dashboard.pipeline_prebuild import PrebuildTarget, run_pipeline_prebuild
    from brutex.data.timeframe_materialize import MaterializeResult
    from brutex.models import Exchange, Instrument, InstrumentType, Segment

    def _spot(name: str) -> Instrument:
        return Instrument(
            exch_id=Exchange.NSE,
            security_id=abs(hash(name)) & 0xFFFF,
            symbol_name=name,
            display_name=name,
            instrument=InstrumentType.IDX,
            segment=Segment.INDEX,
            series=None,
            isin=None,
            underlying_symbol=None,
            underlying_security_id=None,
            lot_size=0,
            tick_size=Decimal("0.05"),
            sm_freeze_qty=None,
            sm_expiry_date=None,
            strike_price=None,
        )

    poisoned, clean = "INFSYM", "GOODSYM"
    _plant_nonfinite_1m(tmp_path, poisoned, column="high", value=float("inf"))
    _write_1m_bars_for(tmp_path, clean)

    def _materialize(target: PrebuildTarget) -> MaterializeResult:
        return materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange=target.exchange,
            segment=target.segment,
            symbol=target.symbol,
            kind=target.kind,
            tfs=("5m",),
        )

    outcome = run_pipeline_prebuild(
        instruments=[_spot(poisoned), _spot(clean)],
        cascade=build_fno_cascade([]),
        materialize=_materialize,
    )
    assert outcome.ok is False
    assert outcome.failed == (poisoned,)  # the poisoned symbol is NAMED
    assert outcome.symbols_built == 1  # the clean sibling still built
    assert not (tmp_path / "bars" / _EXCHANGE / _SEGMENT / poisoned / "5minute").exists()
    assert (tmp_path / "bars" / _EXCHANGE / _SEGMENT / clean / "5minute").exists()


def test_clean_lake_read_1m_is_pure_pass_through(tmp_path: Path) -> None:
    """HEALTHY-LAKE IDENTITY PIN (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001): on
    a clean lake the strengthened gate changes NOTHING — the reader's frame
    equals the raw parquet content under the same rename/select/sort."""
    from brutex.data.timeframe_materialize import make_lake_read_1m

    _write_1m_bars_for(tmp_path, _SYMBOL, n=10)
    frame = make_lake_read_1m(
        lake_root=tmp_path, exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL
    )()
    raw = pl.read_parquet(
        (tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1minute").as_posix()
        + "/**/*.parquet"
    )
    expected = (
        raw.rename({"timestamp": "ts_utc"})
        .select("ts_utc", "open", "high", "low", "close", "volume")
        .sort("ts_utc")
    )
    assert frame.equals(expected)


def _write_1m_bars_for(lake_root: Path, symbol: str, n: int = 10) -> None:
    """Write *n* clean in-session 1-minute bars for *symbol* via the real sink."""
    bars = [
        CandleBar(
            timestamp=_SESSION_OPEN_UTC + timedelta(minutes=i),
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
    sink.append(exchange=_EXCHANGE, segment=_SEGMENT, symbol=symbol, interval="1minute", bars=bars)


# ────────────────────────────────────────────────────────────────────────
# Prebuild bad-tick screen — a self-consistent x10 spike never poisons the
# derived TFs / 1day anchor (DEC-READ-GUARD-PARITY-001)
# ────────────────────────────────────────────────────────────────────────

#: Clean in-session 1-minute closes by minute offset (two 5-minute buckets;
#: minute 2 is deliberately absent — the spike's own slot, so the SCREENED
#: spiked frame is exactly the clean twin's frame).
_BT_CLEAN_CLOSES: dict[int, float] = {
    0: 100.0,
    1: 100.5,
    3: 100.75,
    4: 101.25,
    5: 100.9,
    6: 101.1,
    7: 100.8,
    8: 101.0,
}

#: The mid-bucket minute the self-consistent x10 spike occupies.
_BT_SPIKE_MINUTE = 2

#: max(close + 0.5) over the clean bars — the derived HIGH every arm must read.
_BT_CLEAN_HIGH = 101.75


def _seed_spiked_lake(lake_root: Path, *, spike: bool) -> None:
    """~8 clean 1m bars (close ~= 100, two 5m buckets) via the REAL writer.

    With ``spike=True`` one mid-bucket SELF-CONSISTENT x10 bar
    ``(open=1000.0, high=1001.0, low=999.0, close=1000.5)`` is added at
    minute 2 — internally ordered + positive, so it passes
    ``CandleBar._validate_price_bar`` AND the frame price-sanity gate (the
    exact DEC-SWEEP-DATA-EDGE-GUARDS-001 scaled-bar class), hence the REAL
    ``ParquetWriter`` plant (never raw pyarrow). ``spike=False`` is the
    clean twin: the identical bars MINUS the spike.
    """
    bars = [
        CandleBar(
            timestamp=_SESSION_OPEN_UTC + timedelta(minutes=minute),
            open=Decimal(str(close)),
            high=Decimal(str(close + 0.5)),
            low=Decimal(str(close - 0.5)),
            close=Decimal(str(close)),
            volume=10 + minute,
            open_interest=0,
        )
        for minute, close in sorted(_BT_CLEAN_CLOSES.items())
    ]
    if spike:
        bars.append(
            CandleBar(
                timestamp=_SESSION_OPEN_UTC + timedelta(minutes=_BT_SPIKE_MINUTE),
                open=Decimal("1000.0"),
                high=Decimal("1001.0"),
                low=Decimal("999.0"),
                close=Decimal("1000.5"),
                volume=999,
                open_interest=0,
            )
        )
        bars.sort(key=lambda bar: bar.timestamp)
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1minute", bars=bars)


def _bt_materialize(lake_root: Path) -> None:
    """The production derive: 5minute + the derived 1day, deterministic cutoff."""
    materialize_lake_timeframes(
        lake_root=lake_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
        include_daily=True,
        daily_before=datetime(2021, 6, 1, tzinfo=UTC).date(),
    )


def _bt_derived_high(lake_root: Path, interval: str) -> float:
    """The max stored HIGH of the symbol's derived *interval* partition."""
    part = lake_root / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / interval
    df = pl.read_parquet(part.as_posix() + "/**/*.parquet")
    return float(df.get_column("high").max())  # type: ignore[arg-type]


def _bt_derived_hashes(lake_root: Path) -> dict[str, bytes]:
    """Per-file bytes of every DERIVED partition (the 1minute base excluded)."""
    return {
        rel: content
        for rel, content in _hash_lake_parquet(lake_root).items()
        if "/1minute/" not in f"/{rel}"
    }


class TestPrebuildBadTickScreen:
    """GAP-1 (DEC-READ-GUARD-PARITY-001): the prebuild 1m read screens spikes."""

    def test_prebuild_excludes_x10_spike_from_derived_and_1day(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAILS-BEFORE: the self-consistent x10 spike (which passes the
        price-sanity gate) leaked into the derived 5minute bucket HIGH and the
        1day anchor HIGH; the screened derive now writes the clean twin's
        bytes, with exactly ONE ``feed.bad_tick_skipped`` WARN carrying the
        ``prebuild:``-prefixed source."""
        from structlog.testing import capture_logs

        monkeypatch.delenv("BRUTEX_BAD_TICK_SCREEN", raising=False)  # default ON
        spiked = tmp_path / "spiked"
        clean = tmp_path / "clean"
        _seed_spiked_lake(spiked, spike=True)
        _seed_spiked_lake(clean, spike=False)
        with capture_logs() as logs:
            _bt_materialize(spiked)
        _bt_materialize(clean)
        # (a) the spike bucket's derived 5minute HIGH == the clean high.
        assert _bt_derived_high(spiked, "5minute") == _BT_CLEAN_HIGH
        # (b) the derived 1day anchor HIGH == the clean high.
        assert _bt_derived_high(spiked, "1day") == _BT_CLEAN_HIGH
        # (c) screened(spiked frame) == clean frame ⇒ byte-identical derive.
        assert _bt_derived_hashes(spiked) == _bt_derived_hashes(clean)
        # (d) exactly ONE per-session WARN, boundary-distinct prebuild source.
        warns = [e for e in logs if e.get("event") == "feed.bad_tick_skipped"]
        assert len(warns) == 1
        assert warns[0]["source"] == f"prebuild:{_EXCHANGE}/{_SEGMENT}/{_SYMBOL}/1minute"
        assert warns[0]["skipped"] == 1

    def test_prebuild_kill_switch_off_keeps_spike_in_derived(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``BRUTEX_BAD_TICK_SCREEN=off`` restores the legacy (spike-poisoned)
        derive end-to-end — the operator's forensic raw-bytes escape hatch —
        and emits ZERO bad-tick WARNs (pins the M5 switch polarity)."""
        from structlog.testing import capture_logs

        monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "off")
        _seed_spiked_lake(tmp_path, spike=True)
        with capture_logs() as logs:
            _bt_materialize(tmp_path)
        assert _bt_derived_high(tmp_path, "5minute") == 1001.0
        assert _bt_derived_high(tmp_path, "1day") == 1001.0
        assert not [e for e in logs if e.get("event") == "feed.bad_tick_skipped"]

    def test_prebuild_clean_lake_screen_on_vs_off_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The clean-lake A/B: screen ON (default) vs OFF derive the SAME
        bytes — clean lakes are provably untouched by the new gate."""
        lake_on = tmp_path / "on"
        lake_off = tmp_path / "off"
        _seed_spiked_lake(lake_on, spike=False)
        _seed_spiked_lake(lake_off, spike=False)
        monkeypatch.delenv("BRUTEX_BAD_TICK_SCREEN", raising=False)
        _bt_materialize(lake_on)
        monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "off")
        _bt_materialize(lake_off)
        assert _hash_lake_parquet(lake_on) == _hash_lake_parquet(lake_off)


# ────────────────────────────────────────────────────────────────────────
# Read-boundary TIMESTAMP-range gate + bars-tree glob parity
# (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001)
# ────────────────────────────────────────────────────────────────────────

#: ~year 148108 as microseconds since epoch — within i64, far past
#: ``datetime.max``; the executed repro's poison value.
_OUT_OF_RANGE_TS_US = 2**62


def _plant_out_of_range_ts_1m(lake_root: Path, symbol: str, *, segment: str = _SEGMENT) -> None:
    """Raw-plant a 1-minute partition holding one out-of-datetime-range stamp.

    Raw pyarrow (the ``_plant_corrupt_1m`` convention): no production writer can
    mint the row — a ``CandleBar`` timestamp is a Python ``datetime``, which
    cannot represent ``2**62`` us — so the plant models hostile / bit-rotted
    STORED bytes. Prices on the poisoned row are SANE (the price gate is blind
    to it; only the ts gate can refuse it).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest = (
        lake_root
        / "bars"
        / _EXCHANGE
        / segment
        / symbol
        / "1minute"
        / f"{_SESSION_OPEN_UTC.year:04d}"
        / f"{_SESSION_OPEN_UTC.month:02d}.parquet"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    timestamps = pa.array(
        [
            int(_SESSION_OPEN_UTC.timestamp() * 1_000_000),
            int((_SESSION_OPEN_UTC + timedelta(minutes=1)).timestamp() * 1_000_000),
            _OUT_OF_RANGE_TS_US,
        ],
        type=pa.int64(),
    ).cast(pa.timestamp("us", tz="UTC"))
    pq.write_table(
        pa.table(
            {
                "timestamp": timestamps,
                "open": pa.array([100.0] * 3, type=pa.float64()),
                "high": pa.array([110.0] * 3, type=pa.float64()),
                "low": pa.array([90.0] * 3, type=pa.float64()),
                "close": pa.array([105.0] * 3, type=pa.float64()),
                "volume": pa.array([10, 11, 12], type=pa.int64()),
                "open_interest": pa.array([None, None, None], type=pa.int64()),
            }
        ),
        dest,
    )


def test_out_of_range_ts_partition_refuses_to_derive(tmp_path: Path) -> None:
    """FAILS-BEFORE: the 2**62-us stamp survived the vectorized resample and
    PANICKED the Rust→Python conversion (``pyo3_runtime.PanicException``, a
    BaseException) at ``frame_to_candle_bars``'s ``to_list()`` — past every
    ``except Exception`` per-symbol quarantine (executed repro: ONE poisoned
    symbol aborted the WHOLE crossverify prebuild leg). Now the read boundary
    refuses it deterministically as the quarantining ResampleInputError."""
    from brutex.data.errors import ResampleInputError

    _plant_out_of_range_ts_1m(tmp_path, _SYMBOL)
    with pytest.raises(ResampleInputError, match="outside the\\s+representable") as excinfo:
        materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
            months={(_SESSION_OPEN_UTC.year, _SESSION_OPEN_UTC.month)},
            incremental=False,
        )
    message = str(excinfo.value)
    assert f"{_EXCHANGE}/{_SEGMENT}/{_SYMBOL}/1minute" in message  # names the symbol
    assert str(_OUT_OF_RANGE_TS_US) in message  # names the offending physical value
    # No derived partition was written — correct-or-absent.
    assert not (tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "5minute").exists()


def test_out_of_range_ts_include_daily_also_refuses_at_read(tmp_path: Path) -> None:
    """The include_daily=True (pipeline) shape refuses at the SAME read gate —
    a deterministic ResampleInputError, never the OverflowError the
    ``_last_ist_datetime`` max() happened to raise pre-fix."""
    from brutex.data.errors import ResampleInputError

    _plant_out_of_range_ts_1m(tmp_path, _SYMBOL)
    with pytest.raises(ResampleInputError, match="outside the\\s+representable"):
        materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
            include_daily=True,
        )


def test_healthy_lake_read_frame_is_byte_identical(tmp_path: Path) -> None:
    """Healthy-lake byte-identity pin: the gated reader returns EXACTLY the raw
    parquet transform — the new ts gate + glob=False change NOTHING on a clean
    lake (the #1897 pin convention)."""
    from brutex.data.timeframe_materialize import make_lake_read_1m

    _write_1m_bars(tmp_path, n=30)
    read = make_lake_read_1m(
        lake_root=tmp_path, exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL
    )
    frame = read()
    base = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1minute"
    expected = (
        pl.read_parquet(base.as_posix() + "/**/*.parquet")
        .rename({"timestamp": "ts_utc"})
        .select("ts_utc", "open", "high", "low", "close", "volume")
        .sort("ts_utc")
    )
    assert frame.equals(expected)
    # And the months-bounded arm returns the identical frame too.
    months_read = make_lake_read_1m(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        months={(_SESSION_OPEN_UTC.year, _SESSION_OPEN_UTC.month)},
    )
    assert months_read().equals(expected)


def test_months_read_bracket_symbol_reads_literal_file_only(tmp_path: Path) -> None:
    """FAILS-BEFORE (glob parity): a symbol dir named ``NSE-X[12]`` beside real
    ``NSE-X1``/``NSE-X2`` siblings glob-read the SIBLINGS' bars into this
    symbol (executed repro: 6 foreign rows). glob=False reads the literal
    month file only."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from brutex.data.timeframe_materialize import make_lake_read_1m

    def _plant(symbol: str, close: float) -> None:
        dest = (
            tmp_path
            / "bars"
            / _EXCHANGE
            / "CASH"
            / symbol
            / "1minute"
            / f"{_SESSION_OPEN_UTC.year:04d}"
            / f"{_SESSION_OPEN_UTC.month:02d}.parquet"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        timestamps = [_SESSION_OPEN_UTC + timedelta(minutes=i) for i in range(3)]
        pq.write_table(
            pa.table(
                {
                    "timestamp": pa.array(timestamps, type=pa.timestamp("us", tz="UTC")),
                    "open": pa.array([100.0] * 3, type=pa.float64()),
                    "high": pa.array([110.0] * 3, type=pa.float64()),
                    "low": pa.array([90.0] * 3, type=pa.float64()),
                    "close": pa.array([close] * 3, type=pa.float64()),
                    "volume": pa.array([10, 11, 12], type=pa.int64()),
                    "open_interest": pa.array([None] * 3, type=pa.int64()),
                }
            ),
            dest,
        )

    _plant("NSE-X1", 100.11)
    _plant("NSE-X2", 100.22)
    _plant("NSE-X[12]", 100.55)  # the literal dir's OWN bars
    read = make_lake_read_1m(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment="CASH",
        symbol="NSE-X[12]",
        months={(_SESSION_OPEN_UTC.year, _SESSION_OPEN_UTC.month)},
    )
    frame = read()
    assert frame.height == 3
    assert set(frame.get_column("close").to_list()) == {100.55}
