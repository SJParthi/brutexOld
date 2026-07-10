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


def test_window_read_seam_matches_the_shared_seam(tmp_path: Path) -> None:
    """The 4th seam (review fix round): ``make_lake_read_1m_window`` screens too.

    ``spot_daily_resample.make_lake_read_1m_window`` — the lake-heal daily
    RECALL's injected 1-minute reader — was a genuine UNSCREENED resample
    input: over this poisoned fixture the SHARED seam (``make_lake_read_1m``)
    served 29 rows with the poison ABSENT while the window seam served 30 with
    the poison PRESENT (the reviewer's demonstration). Both seams now serve
    the IDENTICAL screened row set, so the heal's daily re-derive consumes the
    same exclusions as the prebuild's. FAILS-BEFORE with the window-seam
    wiring reverted (window read: 30 rows, poison high present).
    """
    from datetime import date as _date

    from brutex.data.spot_daily_resample import make_lake_read_1m_window
    from brutex.data.timeframe_materialize import make_lake_read_1m

    poison_bar = _write_1m(tmp_path, poison="all")
    assert poison_bar is not None

    shared = make_lake_read_1m(
        lake_root=tmp_path, exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL
    )()
    window = make_lake_read_1m_window(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        start=_date(2021, 1, 4),
        end=_date(2021, 1, 5),
    )()

    assert shared.height == _N - 1  # the shared seam drops the poison bar
    assert window.height == _N - 1  # pre-fix: _N (30) — the poison SERVED
    assert float(poison_bar.high) not in window.get_column("high").to_list()
    assert float(poison_bar.close) not in window.get_column("close").to_list()
    # Identical surviving row set — the seam-parity law.
    assert window.get_column("ts_utc").to_list() == shared.get_column("ts_utc").to_list()


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


# ════════════════════════════════════════════════════════════════════════
# Instrument-class scope (DEC-BADTICK-RESAMPLE-INPUT-001 fix round 2):
# SPOT + FNO FUT screened; FNO CE/PE deliberately NOT screened.
# ════════════════════════════════════════════════════════════════════════

_FNO_SEGMENT = "FNO"
_OPTION_SYMBOL = "NSE-NIFTY-30Sep25-24650-CE"
_FUTURE_SYMBOL = "NSE-NIFTY-30Sep25-FUT"
#: A LEGIT deep-OTM re-pricing: a clean ₹2 → ₹9 one-minute print is 4.5x —
#: real option-premium physics, NOT corruption (the refuter's captured repro).
_OPTION_CLOSES = [2.0, 2.05, 2.1, 2.15, 9.0, 9.1, 9.2, 9.3, 9.4, 9.5]


def _write_fno_1m(lake_root: Path, symbol: str, closes: list[float]) -> None:
    """Write one in-session 1-minute series under an FNO contract coordinate."""
    bars = [
        CandleBar(
            timestamp=_SESSION_OPEN_UTC + timedelta(minutes=i),
            open=Decimal(str(c)),
            high=Decimal(str(round(c + 0.05, 2))),
            low=Decimal(str(round(max(c - 0.05, 0.05), 2))),
            close=Decimal(str(c)),
            volume=100 + i,
            open_interest=50,
        )
        for i, c in enumerate(closes)
    ]
    ParquetBarSink(writer=ParquetWriter(lake_root=lake_root)).append(
        exchange=_EXCHANGE, segment=_FNO_SEGMENT, symbol=symbol, interval="1minute", bars=bars
    )


def _stored_closes(lake_root: Path, symbol: str, interval: str) -> list[float]:
    """Raw polars read of a derived FNO partition's closes (no engine screen)."""
    import polars as pl

    part = lake_root / "bars" / _EXCHANGE / _FNO_SEGMENT / symbol / interval
    files = sorted(part.rglob("*.parquet"))
    assert files, f"no {interval} partition written for {symbol}"
    frame = pl.read_parquet([f.as_posix() for f in files]).sort("timestamp")
    return [float(v) for v in frame.get_column("close").to_list()]


def test_option_legit_move_survives_resample_seams(tmp_path: Path) -> None:
    """FIX ROUND 2 regression (fails-before on fe81ef83): an OPTION session

    with a legit > 4x clean premium move materialises UNCHANGED through seam 1
    (``make_lake_read_1m``) AND seam 4 (``make_lake_read_1m_window``). Pre-fix
    the index-calibrated screen DROPPED the ₹2 → ₹9 print and — because a
    skipped bar never advances the baseline — cascade-dropped the session's
    re-priced 9.x remainder too (captured: 4 of 10 rows served), silently
    filtering legit bars out of the REFERENCE_ONLY derived partitions and
    diverging from the deliberately-unscreened F&O movers display path.
    """
    from datetime import date as _date

    from brutex.data.spot_daily_resample import make_lake_read_1m_window
    from brutex.data.timeframe_materialize import make_lake_read_1m

    _write_fno_1m(tmp_path, _OPTION_SYMBOL, _OPTION_CLOSES)

    # Seam 1: the shared prebuild read serves EVERY bar (pre-fix: 4 of 10).
    shared = make_lake_read_1m(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_FNO_SEGMENT,
        symbol=_OPTION_SYMBOL,
        with_open_interest=True,
    )()
    assert shared.height == len(_OPTION_CLOSES)
    assert shared.get_column("close").to_list() == _OPTION_CLOSES

    # Seam 4: the lake-heal daily-recall window read serves EVERY bar too.
    window = make_lake_read_1m_window(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_FNO_SEGMENT,
        symbol=_OPTION_SYMBOL,
        start=_date(2021, 1, 4),
        end=_date(2021, 1, 5),
        include_oi=True,
    )()
    assert window.height == len(_OPTION_CLOSES)
    assert window.get_column("close").to_list() == _OPTION_CLOSES

    # End-to-end: the materialised derived TFs carry the re-priced 9.x closes.
    materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_FNO_SEGMENT,
        symbol=_OPTION_SYMBOL,
        kind=PrebuildKind.OPTIONS,
        tfs=("5m",),
        include_daily=True,
        daily_before=date(2021, 1, 5),
    )
    assert 9.5 in _stored_closes(tmp_path, _OPTION_SYMBOL, "5minute")
    assert _stored_closes(tmp_path, _OPTION_SYMBOL, "1day") == [9.5]


def test_futures_poison_still_screened_through_resample_seams(tmp_path: Path) -> None:
    """The FUT arm of the class gate: a x10 poison bar under an FNO FUT

    coordinate is STILL dropped at seam 1 + seam 4 and never reaches a derived
    bucket — futures move like their underlying (no legit > 4x/min under the
    NSE circuit bands) and NIFTY futures are ENGINE scope, so the engine-truth
    invariant keeps derived futures timeframes screened.
    """
    from datetime import date as _date

    from brutex.data.spot_daily_resample import make_lake_read_1m_window
    from brutex.data.timeframe_materialize import make_lake_read_1m

    closes = [100.0 + i for i in range(10)]
    closes[7] = closes[7] * _SCALE  # x10 poison, post-head
    _write_fno_1m(tmp_path, _FUTURE_SYMBOL, closes)

    shared = make_lake_read_1m(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_FNO_SEGMENT,
        symbol=_FUTURE_SYMBOL,
        with_open_interest=True,
    )()
    assert shared.height == len(closes) - 1
    assert closes[7] not in shared.get_column("close").to_list()

    window = make_lake_read_1m_window(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_FNO_SEGMENT,
        symbol=_FUTURE_SYMBOL,
        start=_date(2021, 1, 4),
        end=_date(2021, 1, 5),
        include_oi=True,
    )()
    assert window.height == len(closes) - 1
    assert window.get_column("ts_utc").to_list() == shared.get_column("ts_utc").to_list()

    materialize_lake_timeframes(
        lake_root=tmp_path,
        exchange=_EXCHANGE,
        segment=_FNO_SEGMENT,
        symbol=_FUTURE_SYMBOL,
        kind=PrebuildKind.FUTURES,
        tfs=("5m",),
        include_daily=True,
        daily_before=date(2021, 1, 5),
    )
    for interval in ("5minute", "1day"):
        stored = _stored_closes(tmp_path, _FUTURE_SYMBOL, interval)
        assert closes[7] not in stored, f"{interval} laundered the futures poison"
