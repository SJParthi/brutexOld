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
from brutex.dashboard.db_view import query_candles
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import (
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

    # Re-run: same 1m source → same derived 5m → ZERO net-new rows, byte-identical.
    again = _materialize_5m(tmp_path)
    assert again == 24  # the writer still reports the rows it merged

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
