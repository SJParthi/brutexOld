"""GOLDEN: stored bars BYTE-UNCHANGED by the daily-open substitution observe.

DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001's hard operator pin: the flag is
OBSERVE-ONLY — the derived daily open STILL silently substitutes the earliest
present minute (this PR only NAMES it), and the lake output is byte-identical
with the scan wired in.

Two-part oracle:

* **(a) value pin** — :func:`resample_1m_to_daily` over a 09:15-absent fixture
  returns EXACTLY the hand-pinned bars: the daily ``open`` IS the 09:16 bar's
  open (the substitution is PRESERVED, never "fixed"), the stamp is midnight
  IST (18:30 UTC of the prior day), OI is ``None``.
* **(b) byte pin** — two FRESH tmp lakes seeded with IDENTICAL 1-minute data:
  lake A runs the production :func:`materialize_lake_timeframes` (scan wired,
  ``dense_symbol=True``); lake B runs the detection-free RAW write path
  (:func:`materialize_timeframes` over the shared builders +
  :func:`fill_daily_from_minute` + :func:`make_lake_write_1day` — exactly what
  the materialiser does internally MINUS the observe call). The ``1day`` AND a
  derived intraday partition must be **byte-equal** (``Path.read_bytes()``).
  The SAME explicit ``before`` cutoff feeds both paths (a midnight-crossing
  test run must not diverge on the today-IST default).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import polars as pl
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.spot_daily_resample import (
    fill_daily_from_minute,
    make_lake_read_1m_window,
    make_lake_write_1day,
    resample_1m_to_daily,
)
from brutex.data.timeframe_materialize import (
    PrebuildKind,
    make_lake_read_1m,
    make_lake_write_tf,
    materialize_lake_timeframes,
    materialize_timeframes,
)

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"

#: 09:16 IST == 03:46 UTC on 2021-01-04 — the 09:15 bar is ABSENT.
_SESSION_0916_UTC = datetime(2021, 1, 4, 3, 46, tzinfo=UTC)

#: The fixture session is COMPLETE relative to this cutoff.
_BEFORE = date(2021, 6, 1)


def _bars(n: int = 10) -> list[CandleBar]:
    return [
        CandleBar(
            timestamp=_SESSION_0916_UTC + timedelta(minutes=i),
            open=Decimal(str(100.0 + i)),
            high=Decimal(str(110.0 + i)),
            low=Decimal(str(90.0 + i)),
            close=Decimal(str(105.0 + i)),
            volume=10 + i,
            open_interest=0,
        )
        for i in range(n)
    ]


def _seed_lake(lake_root: Path) -> None:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval="1minute",
        bars=_bars(),
    )


def _frame(n: int = 10) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_utc": [_SESSION_0916_UTC + timedelta(minutes=i) for i in range(n)],
            "open": [100.0 + i for i in range(n)],
            "high": [110.0 + i for i in range(n)],
            "low": [90.0 + i for i in range(n)],
            "close": [105.0 + i for i in range(n)],
            "volume": [10 + i for i in range(n)],
        }
    )


def test_substituted_daily_open_is_preserved_hand_pinned() -> None:
    """(a) The derived daily bar over a 09:15-absent session — hand-pinned.

    The daily ``open`` == the 09:16 bar's open (100.0): the substitution is
    PRESERVED. Stamp = midnight IST 2021-01-04 == 18:30 UTC 2021-01-03. OI None.
    """
    bars = resample_1m_to_daily(_frame(), before=_BEFORE)
    assert len(bars) == 1
    bar = bars[0]
    assert bar.timestamp == datetime(2021, 1, 3, 18, 30, tzinfo=UTC)
    assert bar.open == Decimal("100.0")  # the 09:16 open — substituted, not fixed
    assert bar.high == Decimal("119.0")  # max over 10 bars
    assert bar.low == Decimal("90.0")
    assert bar.close == Decimal("114.0")  # last bar's close (105 + 9)
    assert bar.volume == sum(10 + i for i in range(10))
    assert bar.open_interest is None


def _read_partition_bytes(lake_root: Path, interval: str) -> bytes:
    part = lake_root / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / interval
    files = sorted(part.rglob("*.parquet"))
    assert len(files) == 1, f"expected exactly one {interval} partition file, got {files}"
    return files[0].read_bytes()


def test_stored_bars_byte_unchanged_vs_detection_free_raw_path(tmp_path: Path) -> None:
    """(b) Double-lake byte pin: scan-wired materialise ≡ detection-free raw path."""
    lake_a = tmp_path / "lake_a"
    lake_b = tmp_path / "lake_b"
    _seed_lake(lake_a)
    _seed_lake(lake_b)

    # Lake A — the PRODUCTION path (observe scan wired, dense per-date WARNs on).
    result = materialize_lake_timeframes(
        lake_root=lake_a,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
        include_daily=True,
        daily_before=_BEFORE,
        incremental=False,
        dense_symbol=True,
    )
    assert result.opens_substituted == 1  # the scan genuinely fired on lake A

    # Lake B — the detection-free RAW write path (never gained observation code).
    df_1m = make_lake_read_1m(
        lake_root=lake_b, exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL
    )()
    materialize_timeframes(
        tfs=("5m",),
        read_1m=lambda: df_1m,
        write_tf=make_lake_write_tf(
            lake_root=lake_b, exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL
        ),
    )
    fill_daily_from_minute(
        read_1m=make_lake_read_1m_window(
            lake_root=lake_b,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            start=date(2021, 1, 1),
            end=_BEFORE,
        ),
        write_1day=make_lake_write_1day(
            lake_root=lake_b, exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL
        ),
        before=_BEFORE,
    )

    # THE pin: the stored partitions are byte-equal — observe-only means
    # observe-only, for the daily AND the derived intraday write path.
    assert _read_partition_bytes(lake_a, "1day") == _read_partition_bytes(lake_b, "1day")
    assert _read_partition_bytes(lake_a, "5minute") == _read_partition_bytes(lake_b, "5minute")
