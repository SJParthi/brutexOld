"""Integration round-trip oracle for derived-TF materialise (DEC-TF-MATERIALIZE-001).

The high-stakes test that proves the PRODUCTION lake I/O round-trips: write ~120
real 1-minute bars to a tmp lake via :class:`ParquetBarSink`, run the
materialise through the REAL app glue (``_make_read_1m`` + ``_make_write_tf`` —
the same code path the route uses), then read the derived ``5minute`` partition
back via :func:`brutex.dashboard.db_view.query_candles` and assert each 5-minute
bar aggregates its 5 one-minute bars correctly (open=first, high=max, low=min,
close=last, volume=sum). Re-running must be idempotent — zero net-new rows and a
byte-identical Parquet file (CLAUDE.md §7).
"""
# pyright: reportPrivateUsage=none
# (whitebox: this exercises the route's own ``_make_read_1m`` / ``_make_write_tf``
# glue so the production lake I/O path itself is integration-tested.)

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
from brutex.dashboard.app import _make_read_1m, _make_write_tf
from brutex.dashboard.db_view import query_candles
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

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
    from brutex.data.timeframe_materialize import materialize_timeframes

    read = _make_read_1m(lake_root=lake_root, exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL)
    write = _make_write_tf(
        lake_root=lake_root, exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL
    )
    result = materialize_timeframes(tfs=("5m",), read_1m=read, write_tf=write)
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
