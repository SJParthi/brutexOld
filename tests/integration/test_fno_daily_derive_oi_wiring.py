"""Integration: F&O 1-day derive carries last-of-day OI through the real lake.

DEC-FNO-TF-DERIVE-OI-001. Proves the PRODUCTION lake I/O round-trips F&O open
interest through the REAL builders
(:func:`~brutex.data.spot_daily_resample.make_lake_read_1m_window` with
``include_oi=True`` + :func:`~brutex.data.spot_daily_resample.make_lake_write_1day`)
when the daily resampler is bound to the carry-last-OI
:func:`~brutex.data.fno_daily_resample.resample_1m_to_fno_daily_positional`:

* the derived F&O ``1day`` bar carries the **last-of-day** OI (not None, not summed);
* a span straddling a UTC month boundary derives both daily bars correctly
  (the IST↔UTC partition glob — corrected via the bounded reader);
* the trailing-null-OI edge (corrected_point #5) round-trips as a null daily OI;
* re-deriving is idempotent (byte-identical), and a later complete-session pull
  self-heals a partial-session bar via latest-wins (which also updates OI).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")

import polars as pl
from brutex.data.fno_daily_resample import resample_1m_to_fno_daily_positional
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.spot_daily_resample import (
    DailyDeriveTarget,
    derive_daily_for_targets,
    make_lake_read_1m_window,
    make_lake_write_1day,
)

_EXCHANGE = "NSE"
_SEGMENT = "FNO"
_SYMBOL = "NSE-NIFTY-25Jan24-FUT"

# Two IST sessions that straddle a UTC month boundary:
#   2025-12-31 IST → 2025-12-30T18:30Z (December UTC partition)
#   2026-01-02 IST → 2026-01-01T18:30Z (January  UTC partition)
_SESSIONS: tuple[date, ...] = (date(2025, 12, 31), date(2026, 1, 2))


def _session_1m(day: date, base: float, ois: list[int | None]) -> list[CandleBar]:
    """In-session 1-minute bars carrying the given per-bar OI sequence."""
    open_utc = datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC)
    return [
        CandleBar(
            timestamp=open_utc + timedelta(minutes=i),
            open=Decimal(str(base + i)),
            high=Decimal(str(base + 10 + i)),
            low=Decimal(str(base - 5 + i)),
            close=Decimal(str(base + 2 + i)),
            volume=100 + i,
            open_interest=ois[i],
        )
        for i in range(len(ois))
    ]


def _write_1m(lake_root: Path, by_day: dict[date, list[CandleBar]]) -> None:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    for _day, bars in by_day.items():
        sink.append(
            exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1minute", bars=bars
        )


def _derive(lake_root: Path, *, start: date, end: date) -> int:
    target = DailyDeriveTarget(
        exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, start=start, end=end
    )
    report = derive_daily_for_targets(
        targets=[target],
        build_read_1m=lambda t: make_lake_read_1m_window(
            lake_root=lake_root,
            exchange=t.exchange,
            segment=t.segment,
            symbol=t.symbol,
            start=t.start,
            end=t.end,
            include_oi=True,
        ),
        build_write_1day=lambda t: make_lake_write_1day(
            lake_root=lake_root, exchange=t.exchange, segment=t.segment, symbol=t.symbol
        ),
        before=date(2026, 6, 19),
        resample_fn=resample_1m_to_fno_daily_positional,
    )
    return report.bars


def _read_daily(lake_root: Path) -> pl.DataFrame:
    base = lake_root / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1day"
    return pl.read_parquet(base.as_posix() + "/**/*.parquet").sort("timestamp")


def test_derived_fno_daily_carries_last_of_day_oi_across_month_boundary(tmp_path: Path) -> None:
    by_day = {
        _SESSIONS[0]: _session_1m(_SESSIONS[0], 1400.0, [500, 510, 520]),
        _SESSIONS[1]: _session_1m(_SESSIONS[1], 1500.0, [600, 590, 580, 575]),
    }
    _write_1m(tmp_path, by_day)
    written = _derive(tmp_path, start=date(2025, 12, 31), end=date(2026, 1, 3))
    assert written == 2

    # Both daily partitions exist across the UTC month boundary.
    base = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1day"
    assert (base / "2025" / "12.parquet").exists()  # 2025-12-31 IST → Dec UTC partition
    assert (base / "2026" / "01.parquet").exists()  # 2026-01-02 IST → Jan UTC partition

    daily = _read_daily(tmp_path)
    assert daily.height == 2
    # last-of-day OI for each session (520 / 575), NOT None, NOT summed.
    assert daily.get_column("open_interest").to_list() == [520, 575]


def test_trailing_null_oi_round_trips_as_null_daily_oi(tmp_path: Path) -> None:
    # corrected_point #5: real OI all session, null ONLY on the final bar → null daily OI.
    day = _SESSIONS[0]
    by_day = {day: _session_1m(day, 1400.0, [500, 510, 520, None])}
    _write_1m(tmp_path, by_day)
    written = _derive(tmp_path, start=date(2025, 12, 31), end=date(2026, 1, 1))
    assert written == 1
    daily = _read_daily(tmp_path)
    assert daily.height == 1
    assert daily.get_column("open_interest").to_list() == [None]


def test_redrive_is_idempotent_and_partial_session_self_heals_oi(tmp_path: Path) -> None:
    day = _SESSIONS[0]
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))

    # 1) Partial morning session (2 bars) → a partial daily with OI = last partial (510).
    partial = _session_1m(day, 1400.0, [500, 510])
    sink.append(
        exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1minute", bars=partial
    )
    _derive(tmp_path, start=date(2025, 12, 31), end=date(2026, 1, 1))
    daily = _read_daily(tmp_path)
    assert daily.get_column("open_interest").to_list() == [510]

    # Re-deriving over the SAME 1-minute is byte-identical (idempotent).
    base = tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1day"
    files = sorted(base.rglob("*.parquet"))
    first_bytes = {p: p.read_bytes() for p in files}
    _derive(tmp_path, start=date(2025, 12, 31), end=date(2026, 1, 1))
    for p in sorted(base.rglob("*.parquet")):
        assert p.read_bytes() == first_bytes[p]

    # 2) The COMPLETE session arrives (4 bars, last OI 575) → latest-wins restates the
    # daily AND its OI to the complete last-of-day value.
    full = _session_1m(day, 1400.0, [500, 510, 540, 575])
    sink.append(exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1minute", bars=full)
    _derive(tmp_path, start=date(2025, 12, 31), end=date(2026, 1, 1))
    daily = _read_daily(tmp_path)
    assert daily.height == 1  # never doubled
    assert daily.get_column("open_interest").to_list() == [575]  # self-healed OI
