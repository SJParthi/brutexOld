"""Integration round-trip oracle for 1-day resample-on-empty (DEC-SPOT-1DAY-RESAMPLE-ON-EMPTY-001).

Proves the PRODUCTION lake I/O round-trips through the REAL builders
(:func:`~brutex.data.spot_daily_resample.make_lake_read_1m_window` +
:func:`~brutex.data.spot_daily_resample.make_lake_write_1day`): write real
1-minute bars across two IST sessions that straddle a UTC month boundary, fill the
``1day`` partition from them, then read it straight back via the production candle
reader and assert each daily bar is **midnight-IST stamped** (== 18:30 UTC of the
prior calendar day → the correct UTC-month partition) with the correct OHLCV. Also
pins: window-bounded read (out-of-window 1-minute excluded), idempotent re-run
(byte-identical), and keep-first dedup against a pre-existing vendor daily bar.
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


def test_keep_first_dedup_against_vendor_daily(tmp_path: Path) -> None:
    # A pre-existing VENDOR daily bar at midnight IST for 2025-12-31 IST.
    vendor = CandleBar(
        timestamp=datetime(2025, 12, 30, 18, 30, tzinfo=UTC),  # == 2025-12-31 00:00 IST
        open=Decimal("9999.00"),
        high=Decimal("9999.00"),
        low=Decimal("9999.00"),
        close=Decimal("9999.00"),
        volume=1,
        open_interest=None,
    )
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
    sink.append(
        exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1day", bars=[vendor]
    )

    _write_1m(tmp_path)
    _fill(tmp_path, start=date(2025, 12, 31), end=date(2026, 1, 3))

    rows = _read_daily(tmp_path)
    # Same-timestamp resample bar dedups keep-first → vendor's 9999 survives, NOT doubled.
    dec31 = [r for r in rows if float(r[1]) == 9999.0]
    assert len(dec31) == 1  # exactly one bar for the session — never a duplicate
    assert len(rows) == 2  # the vendor Dec-31 bar + the resampled Jan-02 bar
