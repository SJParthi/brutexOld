"""Regression for DEC-DAILY-WINDOW-BADTICK-001.

Plants a self-consistent x10 scaled 1-minute spike (finite, positive,
OHLC-ordered, so it passes the range + price-sanity gates) and proves
:func:`~brutex.data.spot_daily_resample.make_lake_read_1m_window` DROPS it with
the bad-tick screen ON (the default) and SERVES it with
``BRUTEX_BAD_TICK_SCREEN=0`` (kill-switch parity — the byte-identical pre-fix
behaviour). Fails at the pre-fix source, where the window reader ran no screen.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.spot_daily_resample import make_lake_read_1m_window

_EXCHANGE = "NSE"
_SEGMENT = "CASH"  # the vendor-quirk segment (cash equities)
_SYMBOL = "RELIANCE"

_DAY = date(2025, 12, 31)  # one in-session IST day
_OPEN_UTC = datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC)  # 09:15 IST
_SPIKE_IDX = 5  # a NON-first in-session bar (the first bar is screen-exempt)
_SPIKE_TS = _OPEN_UTC + timedelta(minutes=_SPIKE_IDX)


def _session_1m(day: date, base: float) -> list[CandleBar]:
    """30 in-session 1-minute bars (09:15 IST -> 03:45 UTC + i minutes)."""
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


def _write_spiked_lake(lake_root: Path) -> None:
    """Write one session of 1-minute bars, one bar replaced by a x10 scaled copy.

    Scaling every OHLC leg by 10 keeps the bar finite, positive and OHLC-ordered
    (it passes the range + price-sanity gates), so ONLY the bad-tick close-ratio
    screen (``BAD_TICK_MAX_RATIO == 4.0`` vs the ~x10 jump) can catch it.
    """
    bars = _session_1m(_DAY, 1400.0)
    spike = bars[_SPIKE_IDX]
    bars[_SPIKE_IDX] = dataclasses.replace(
        spike,
        open=spike.open * 10,
        high=spike.high * 10,
        low=spike.low * 10,
        close=spike.close * 10,
    )
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1minute", bars=bars)


def _read_window(lake_root: Path):
    return make_lake_read_1m_window(
        lake_root=lake_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        start=_DAY,
        end=_DAY + timedelta(days=1),
    )()


def test_window_reader_screens_scaled_bad_tick(tmp_path: Path) -> None:
    """Screen ON (default): the x10 spike row is dropped from the window read."""
    _write_spiked_lake(tmp_path)
    frame = _read_window(tmp_path)
    stamps = frame["ts_utc"].to_list()
    assert _SPIKE_TS not in stamps
    assert frame.height == 29  # 30 written minus the one dropped spike


def test_window_reader_kill_switch_off_keeps_bad_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill switch OFF: byte-identical pre-fix behaviour -- the spike is served."""
    monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "0")
    _write_spiked_lake(tmp_path)
    frame = _read_window(tmp_path)
    stamps = frame["ts_utc"].to_list()
    assert _SPIKE_TS in stamps
    assert frame.height == 30
