"""Integration pins for the RESAMPLE-INPUT bad-tick screen (DEC-BADTICK-RESAMPLE-INPUT-001).

The W3-R2 divergence closed end-to-end: a self-consistent x10-scaled 1-minute
bar the ENGINE read boundary (:func:`brutex.feed.replay.arrow_table_to_bars`)
drops is now ALSO dropped at the RESAMPLE input
(:func:`brutex.data.timeframe_materialize.make_lake_read_1m`), so it can no
longer LAUNDER into a derived 5m/15m/1day bucket whose own close is clean. Both
boundaries consume the identical screen, so ``engine(1m)`` and
``engine(<derived TF>)`` can never disagree about which bars are excluded.

FAILS-BEFORE on the branch base (the poison HIGH survived into the 15m/1day
buckets — the 2-bar/1-bar derived sessions were unconfirmable on the read
screen); passes after the fix. Clean data is a no-op (byte-identical derived
partitions), proven against the kill-switch OFF path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import pyarrow.parquet as pq
from brutex.data.bad_tick import BAD_TICK_ENABLED_ENV
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes
from brutex.feed.replay import arrow_table_to_bars

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
#: 09:15 IST == 03:45 UTC — the session open the resampler anchors buckets on.
_SESSION_OPEN_UTC = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)
_POISON_IDX = 7
_SCALE = 10.0
_N = 30


def _write_1m(lake_root: Path, *, poison: str | None) -> CandleBar | None:
    """Write ``_N`` in-session 1-minute bars; optionally scale bar ``_POISON_IDX``.

    *poison* ∈ ``{None, "all", "high", "low"}`` — ``None`` writes a clean series;
    ``"all"`` scales every OHLC field x10 (self-consistent, ordered, positive —
    passes price_sanity), ``"high"``/``"low"`` scale only that field x10 / ÷10
    with a sane close. Returns the poison bar (or ``None``).
    """
    bars: list[CandleBar] = []
    poison_bar: CandleBar | None = None
    for i in range(_N):
        o, h, lo, c = 100.0 + i, 110.0 + i, 90.0 + i, 105.0 + i
        if i == _POISON_IDX and poison == "all":
            o, h, lo, c = o * _SCALE, h * _SCALE, lo * _SCALE, c * _SCALE
        elif i == _POISON_IDX and poison == "high":
            h = h * _SCALE
        elif i == _POISON_IDX and poison == "low":
            lo = lo / _SCALE
        bar = CandleBar(
            timestamp=_SESSION_OPEN_UTC + timedelta(minutes=i),
            open=Decimal(str(o)),
            high=Decimal(str(h)),
            low=Decimal(str(lo)),
            close=Decimal(str(c)),
            volume=10 + i,
            open_interest=0,
        )
        if i == _POISON_IDX and poison is not None:
            poison_bar = bar
        bars.append(bar)
    ParquetBarSink(writer=ParquetWriter(lake_root=lake_root)).append(
        exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1minute", bars=bars
    )
    return poison_bar


def _materialize(lake_root: Path) -> None:
    materialize_lake_timeframes(
        lake_root=lake_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m", "15m"),
        include_daily=True,
        daily_before=date(2021, 1, 5),
    )


def _derived_highs(lake_root: Path, interval: str) -> list[float]:
    """Engine-read the derived partition; return every served bar's high."""
    part = lake_root / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / interval
    files = sorted(part.rglob("*.parquet"))
    assert files, f"no {interval} partition written"
    table = pq.read_table(files[0])
    bars = arrow_table_to_bars(table, source=f"{_EXCHANGE}/{_SEGMENT}/{_SYMBOL}/{interval}")
    return [float(b.high) for b in bars]


@pytest.mark.parametrize("poison", ["all", "high", "low"])
def test_scaled_bar_no_longer_launders_into_derived_buckets(tmp_path: Path, poison: str) -> None:
    """The W3-R2 pin: a scaled 1m spike never reaches a derived bucket's high/low."""
    poison_bar = _write_1m(tmp_path, poison=poison)
    assert poison_bar is not None
    poison_high = float(poison_bar.high)
    poison_low = float(poison_bar.low)

    # engine(1m): the poison bar is dropped at the read boundary.
    one_min = sorted(
        (tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1minute").rglob("*.parquet")
    )[0]
    served_1m = arrow_table_to_bars(
        pq.read_table(one_min), source=f"{_EXCHANGE}/{_SEGMENT}/{_SYMBOL}/1minute"
    )
    assert len(served_1m) == _N - 1
    served_1m_highs = [float(b.high) for b in served_1m]
    served_1m_lows = [float(b.low) for b in served_1m]
    assert poison_high not in served_1m_highs
    assert poison_low not in served_1m_lows

    _materialize(tmp_path)

    # engine(derived): every derived bucket's high/low is CLEAN — the poison
    # never aggregated, so engine(1m) and engine(derived) agree on the exclusion.
    clean_max_high = max(served_1m_highs)
    clean_min_low = min(served_1m_lows)
    for interval in ("5minute", "15minute", "1day"):
        highs = _derived_highs(tmp_path, interval)
        assert poison_high not in highs, f"{interval} laundered the poison HIGH"
        assert max(highs) == clean_max_high, f"{interval} high drifted"
        lows = _derived_lows(tmp_path, interval)
        assert poison_low not in lows, f"{interval} laundered the poison LOW"
        assert min(lows) == clean_min_low, f"{interval} low drifted"


def _derived_lows(lake_root: Path, interval: str) -> list[float]:
    part = lake_root / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / interval
    files = sorted(part.rglob("*.parquet"))
    table = pq.read_table(files[0])
    bars = arrow_table_to_bars(table, source=f"{_EXCHANGE}/{_SEGMENT}/{_SYMBOL}/{interval}")
    return [float(b.low) for b in bars]


def test_clean_data_screen_on_equals_screen_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clean data ⇒ the screen is a no-op ⇒ byte-identical derived partitions.

    Materialise the SAME clean 1-minute series with the screen ON (lake A) and
    OFF (lake B) and assert every derived partition's bytes are identical — the
    goldens-byte-identical requirement (the helper returns the same frame object
    on clean data).
    """
    lake_on = tmp_path / "on"
    lake_off = tmp_path / "off"
    for lake in (lake_on, lake_off):
        _write_1m(lake, poison=None)

    _materialize(lake_on)  # default: screen ON
    monkeypatch.setenv(BAD_TICK_ENABLED_ENV, "0")
    _materialize(lake_off)  # kill switch OFF

    for interval in ("5minute", "15minute", "1day"):
        on_files = sorted(
            (lake_on / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / interval).rglob("*.parquet")
        )
        off_files = sorted(
            (lake_off / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / interval).rglob("*.parquet")
        )
        assert [f.name for f in on_files] == [f.name for f in off_files]
        for on_f, off_f in zip(on_files, off_files, strict=True):
            assert on_f.read_bytes() == off_f.read_bytes(), f"{interval} bytes differ"


def test_kill_switch_off_lets_poison_launder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With BRUTEX_BAD_TICK_SCREEN=0 the poison HIGH DOES launder into 1day.

    Proves the screen is the CAUSE of the fix (and the kill switch restores the
    pre-fix laundering) — the 1day bucket is a 1-bar session unconfirmable on
    the read screen, so its high carries the poison end-to-end when OFF.
    """
    poison_bar = _write_1m(tmp_path, poison="all")
    assert poison_bar is not None
    monkeypatch.setenv(BAD_TICK_ENABLED_ENV, "0")
    _materialize(tmp_path)
    highs = _derived_highs(tmp_path, "1day")
    assert float(poison_bar.high) in highs
