"""Independent from-scratch resampler oracle — OI, daily chain, and STORED lake.

Preserved from an independent verification session. The oracle below is a
SECOND, from-scratch implementation of the resampling spec (pure-Python dicts,
its own ``+05:30`` IST arithmetic — it never imports the production bucket
formula), asserted equal to production over hypothesis inputs AND over a REAL
Parquet lake. It deliberately covers ONLY what the existing suites do not:

* **OI-bearing intraday frames** — the P-ORACLE cross-check in
  ``test_tf_resampler_tiling_properties.py`` draws frames with NO
  ``open_interest`` column, and the unit suite pins the carry-last OI on one
  fixture; here hypothesis F&O-shaped frames (incl. null-OI rows — snapshot
  semantics) are checked against the oracle across every intraday TF.
* **The midnight-IST DAILY chain over multi-day frames** —
  ``test_derived_daily_properties.py`` proves the aggregation for a single
  fixed day with no cutoff variation; here random multi-day frames (incl. a
  year-boundary anchor) with a DRAWN forming-session cutoff are checked
  against an independent midnight-IST oracle (label = 18:30 UTC of the prior
  calendar day, sessions ``>= before`` dropped).
* **STORED-vs-oracle over a real lake** — real ``ParquetBarSink`` 1-minute
  writes → the real ``materialize_lake_timeframes`` prebuild → the STORED
  derived partitions read back and compared to the oracle computed FROM the
  stored 1-minute base, so the whole write→derive→store chain (including the
  2-dp write snap and the Jan-1-IST daily bar landing in the DECEMBER UTC
  month partition) is proven, not just the in-memory resampler.

Deliberately TRIMMED as duplicates of existing coverage: the no-OI intraday
hypothesis oracle (P-ORACLE), deterministic session-shape scenarios + bucket
label pins (tiling properties + the unit resampler suites), malformed-input
rejection (unit suites), the FRESH daily month-boundary pin
(``test_spot_daily_resample.py``), and the session-filter boundary (unit
session suites).
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest
from brutex.data.models import CandleBar
from brutex.data.spot_daily_resample import resample_1m_to_daily
from brutex.data.tf_resampler import resample_1m_to_tf
from brutex.data.timeframes import ALL_TF_MINUTES, DAILY_TF_MINUTES, LAKE_INTERVAL_LABELS
from hypothesis import given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    import polars as pl  # type-only: real module so `pl.X` annotations resolve
else:
    pl = pytest.importorskip("polars")  # runtime: frame construction needs the [data] extra

pytest.importorskip("pyarrow")  # the stored-lake tests write real Parquet

from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

#: The oracle's OWN IST arithmetic (never imported from production).
_IST_OFFSET = timedelta(hours=5, minutes=30)
#: NSE session open, IST wall clock — the fixed bucket anchor.
_SESSION_OPEN = time(9, 15)
#: 09:15 IST == 03:45 UTC on the same calendar day.
_SESSION_OPEN_UTC = time(3, 45)
#: Every intraday TF (the daily 375 rides the separate midnight oracle).
_INTRADAY_TFS: tuple[int, ...] = tuple(t for t in sorted(ALL_TF_MINUTES) if t != DAILY_TF_MINUTES)

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"


class _Bucket(NamedTuple):
    """One comparable output bar (naive-UTC label + OHLCV + carry-last OI)."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    open_interest: int | None


def _to_naive_utc(ts: datetime) -> datetime:
    """Normalize a timestamp to naive UTC (drop tz, keep the instant)."""
    if ts.tzinfo is not None:
        return ts.astimezone(UTC).replace(tzinfo=None)
    return ts


def _oracle_intraday(df: pl.DataFrame, tf_minutes: int) -> list[_Bucket]:
    """The independent intraday oracle: floor-bucket off each IST date's 09:15.

    Re-derived from the spec with the oracle's own arithmetic: a bar buckets at
    ``floor(minutes_since_0915 / tf) * tf`` past its OWN IST date's 09:15 open,
    labeled at the bucket's left edge re-expressed in UTC. Aggregation:
    open = first-by-ts, high = max, low = min, close = last-by-ts,
    volume = sum, open_interest = last-by-ts (snapshot semantics, nulls kept).
    """
    has_oi = "open_interest" in df.columns
    buckets: dict[datetime, list[dict[str, Any]]] = {}
    for row in df.iter_rows(named=True):
        ts = _to_naive_utc(row["ts_utc"])
        ist = ts + _IST_OFFSET
        session_open_ist = datetime.combine(ist.date(), _SESSION_OPEN)
        elapsed = int((ist - session_open_ist).total_seconds() // 60)
        bucket_ist = session_open_ist + timedelta(minutes=(elapsed // tf_minutes) * tf_minutes)
        buckets.setdefault(bucket_ist - _IST_OFFSET, []).append({**row, "ts": ts})
    out: list[_Bucket] = []
    for label in sorted(buckets):
        members = sorted(buckets[label], key=lambda r: r["ts"])
        last_oi: int | None = None
        if has_oi:
            raw_oi = members[-1]["open_interest"]
            last_oi = None if raw_oi is None else int(raw_oi)
        out.append(
            _Bucket(
                ts=label,
                open=float(members[0]["open"]),
                high=max(float(m["high"]) for m in members),
                low=min(float(m["low"]) for m in members),
                close=float(members[-1]["close"]),
                volume=sum(int(m["volume"]) for m in members),
                open_interest=last_oi,
            )
        )
    return out


def _oracle_daily_midnight(df: pl.DataFrame, *, before: date) -> list[_Bucket]:
    """The independent midnight-IST daily oracle.

    One bar per COMPLETE IST trading date (``ist_date < before``; a session at
    or past the cutoff is DROPPED — the forming-session guard), aggregated over
    the whole session and labeled at midnight IST — which is 18:30 UTC of the
    PRIOR calendar day. Spot daily bars carry no OI (always ``None``).
    """
    by_date: dict[date, list[dict[str, Any]]] = {}
    for row in df.iter_rows(named=True):
        ts = _to_naive_utc(row["ts_utc"])
        by_date.setdefault((ts + _IST_OFFSET).date(), []).append({**row, "ts": ts})
    out: list[_Bucket] = []
    for ist_date in sorted(by_date):
        if ist_date >= before:
            continue
        members = sorted(by_date[ist_date], key=lambda r: r["ts"])
        out.append(
            _Bucket(
                ts=datetime.combine(ist_date, time(0, 0)) - _IST_OFFSET,
                open=float(members[0]["open"]),
                high=max(float(m["high"]) for m in members),
                low=min(float(m["low"]) for m in members),
                close=float(members[-1]["close"]),
                volume=sum(int(m["volume"]) for m in members),
                open_interest=None,
            )
        )
    return out


def _frame_to_buckets(df: pl.DataFrame) -> list[_Bucket]:
    """A production OUTPUT frame (``ts_utc`` + OHLCV [+ OI]) as comparable rows."""
    has_oi = "open_interest" in df.columns
    out: list[_Bucket] = []
    for row in df.iter_rows(named=True):
        raw_oi = row["open_interest"] if has_oi else None
        out.append(
            _Bucket(
                ts=_to_naive_utc(row["ts_utc"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
                open_interest=None if raw_oi is None else int(raw_oi),
            )
        )
    return out


def _daily_bars_to_buckets(bars: list[CandleBar]) -> list[_Bucket]:
    """Production daily output (``CandleBar`` list) as comparable rows."""
    out: list[_Bucket] = []
    for bar in bars:
        assert bar.open_interest is None  # spot daily bars carry no OI
        out.append(
            _Bucket(
                ts=_to_naive_utc(bar.timestamp),
                open=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                volume=bar.volume,
                open_interest=None,
            )
        )
    return out


def _draw_ohlc(draw: st.DrawFn) -> tuple[float, float, float, float]:
    """Four 2-dp prices with ``low <= open, close <= high`` by order statistics."""
    price = st.floats(min_value=1.0, max_value=100_000.0, allow_nan=False, allow_infinity=False)
    quadruple = sorted(round(draw(price), 2) for _ in range(4))
    return quadruple[1], quadruple[3], quadruple[0], quadruple[2]  # open, high, low, close


@st.composite
def _oi_frames(draw: st.DrawFn) -> pl.DataFrame:
    """1..2 gapped F&O-shaped sessions carrying a nullable ``open_interest``."""
    n_days = draw(st.integers(min_value=1, max_value=2))
    rows: list[tuple[datetime, float, float, float, float, int, int | None]] = []
    for day_index in range(n_days):
        day = date(2024, 3, 27) + timedelta(days=day_index)
        day_open_utc = datetime.combine(day, _SESSION_OPEN_UTC)
        present = draw(
            st.lists(st.integers(min_value=0, max_value=374), min_size=1, max_size=30, unique=True)
        )
        for minute in sorted(present):
            open_, high, low, close = _draw_ohlc(draw)
            rows.append(
                (
                    day_open_utc + timedelta(minutes=minute),
                    open_,
                    high,
                    low,
                    close,
                    draw(st.integers(min_value=0, max_value=10_000_000)),
                    draw(st.one_of(st.none(), st.integers(min_value=0, max_value=5_000_000))),
                )
            )
    return pl.DataFrame(
        rows,
        schema={
            "ts_utc": pl.Datetime(time_unit="us"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
            "open_interest": pl.Int64,
        },
        orient="row",
    ).sort("ts_utc")


@st.composite
def _daily_frames(draw: st.DrawFn) -> pl.DataFrame:
    """1..3 gapped spot sessions (no OI), optionally spanning the year boundary."""
    n_days = draw(st.integers(min_value=1, max_value=3))
    base = draw(st.sampled_from([date(2024, 3, 27), date(2024, 12, 30)]))
    rows: list[tuple[datetime, float, float, float, float, int]] = []
    for day_index in range(n_days):
        day = base + timedelta(days=day_index)
        day_open_utc = datetime.combine(day, _SESSION_OPEN_UTC)
        present = draw(
            st.lists(st.integers(min_value=0, max_value=374), min_size=1, max_size=30, unique=True)
        )
        for minute in sorted(present):
            open_, high, low, close = _draw_ohlc(draw)
            rows.append(
                (
                    day_open_utc + timedelta(minutes=minute),
                    open_,
                    high,
                    low,
                    close,
                    draw(st.integers(min_value=0, max_value=10_000_000)),
                )
            )
    return pl.DataFrame(
        rows,
        schema={
            "ts_utc": pl.Datetime(time_unit="us"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
        orient="row",
    ).sort("ts_utc")


@given(df=_oi_frames())  # pyright: ignore[reportCallIssue]  # @st.composite injects draw
@settings(max_examples=100, deadline=None)
def test_oi_carrying_frames_match_the_oracle_across_all_intraday_tfs(df: pl.DataFrame) -> None:
    """Carry-last (snapshot) OI + OHLCV: production == the independent oracle
    for every intraday TF over random F&O-shaped frames with null-OI rows."""
    for tf in _INTRADAY_TFS:
        out = resample_1m_to_tf(df, tf)
        assert "open_interest" in out.columns, f"tf={tf}: OI column dropped"
        assert _frame_to_buckets(out) == _oracle_intraday(df, tf), f"tf={tf}: oracle disagreement"


@given(
    df=_daily_frames(),  # pyright: ignore[reportCallIssue]  # @st.composite injects draw
    cutoff_days=st.integers(min_value=0, max_value=4),
)
@settings(max_examples=150, deadline=None)
def test_multiday_daily_chain_matches_the_midnight_oracle(
    df: pl.DataFrame, cutoff_days: int
) -> None:
    """``resample_1m_to_daily`` == the independent midnight-IST oracle over
    random multi-day frames with a DRAWN forming-session cutoff (dropping
    none / some / all sessions), including the year-boundary anchor."""
    first_ts = df.get_column("ts_utc").min()
    assert isinstance(first_ts, datetime)
    before = (_to_naive_utc(first_ts) + _IST_OFFSET).date() + timedelta(days=cutoff_days)
    got = _daily_bars_to_buckets(resample_1m_to_daily(df, before=before))
    assert got == _oracle_daily_midnight(df, before=before)


def _session_bars(ist_day: date, *, seed: int) -> list[CandleBar]:
    """One full deterministic 375-bar session as 2-dp-grid ``CandleBar``s."""
    rng = random.Random(seed)
    day_open_utc = datetime.combine(ist_day, _SESSION_OPEN_UTC, tzinfo=UTC)
    bars: list[CandleBar] = []
    price = 20_000.0
    for minute in range(375):
        open_ = round(price, 2)
        close = round(price + rng.uniform(-8.0, 8.0), 2)
        high = max(round(price + rng.uniform(0.0, 15.0), 2), open_, close)
        low = min(round(price - rng.uniform(0.0, 15.0), 2), open_, close)
        bars.append(
            CandleBar(
                timestamp=day_open_utc + timedelta(minutes=minute),
                open=Decimal(str(open_)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                volume=rng.randint(1, 5_000),
                open_interest=None,
            )
        )
        price = close + rng.uniform(-3.0, 3.0)
    return bars


def _write_1m(lake_root: Path, bars: list[CandleBar]) -> None:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1minute", bars=bars)


def _read_stored(lake_root: Path, interval: str, year: int, month: int) -> pl.DataFrame:
    """A stored partition normalized for the oracle: ``ts_utc`` column, OI dropped.

    Every stored partition carries an ``open_interest`` column; spot bars hold
    only the lake's placeholder (``0`` / null — never a real value). The
    convention is ASSERTED, then the column is dropped so the oracle
    comparison covers exactly the OHLCV+label chain.
    """
    path = (
        lake_root
        / "bars"
        / _EXCHANGE
        / _SEGMENT
        / _SYMBOL
        / interval
        / f"{year:04d}"
        / f"{month:02d}.parquet"
    )
    assert path.is_file(), f"missing stored partition: {path}"
    df = pl.read_parquet(path).rename({"timestamp": "ts_utc"}).sort("ts_utc")
    assert "open_interest" in df.columns, "stored partitions always carry the OI column"
    assert set(df.get_column("open_interest").to_list()) <= {0, None}, (
        "spot stored OI must be the 0/null placeholder"
    )
    return df.drop("open_interest")


def test_stored_derived_tfs_and_daily_match_the_oracle_over_a_real_lake(
    tmp_path: Path,
) -> None:
    """The whole write→derive→store chain equals the independent oracle.

    Two real sessions written through the production sink, derived through the
    production prebuild, then every STORED derived partition (five intraday
    TFs + the midnight-IST daily) is compared to the oracle computed FROM the
    stored (2-dp-snapped) 1-minute base — so the comparison proves the
    derivation chain, never the write snap.
    """
    lake = tmp_path / "lake"
    _write_1m(
        lake, _session_bars(date(2024, 3, 27), seed=1) + _session_bars(date(2024, 3, 28), seed=2)
    )
    result = materialize_lake_timeframes(
        lake_root=lake,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("3m", "5m", "15m", "60m", "180m"),
        include_daily=True,
        daily_before=date(2024, 3, 29),  # both sessions complete
    )
    assert result.unchanged is False
    assert sum(result.written.values()) > 0

    stored_1m = _read_stored(lake, "1minute", 2024, 3)
    for tf in (3, 5, 15, 60, 180):
        stored = _read_stored(lake, LAKE_INTERVAL_LABELS[tf], 2024, 3)
        assert _frame_to_buckets(stored) == _oracle_intraday(stored_1m, tf), (
            f"stored {LAKE_INTERVAL_LABELS[tf]} disagrees with the oracle"
        )

    stored_daily = _read_stored(lake, "1day", 2024, 3)
    daily_buckets = _frame_to_buckets(stored_daily)
    assert daily_buckets == _oracle_daily_midnight(stored_1m, before=date(2024, 3, 29))
    # Midnight-IST labels: 18:30 UTC of the prior calendar day, both in March.
    assert [b.ts for b in daily_buckets] == [
        datetime(2024, 3, 26, 18, 30),
        datetime(2024, 3, 27, 18, 30),
    ]


def test_stored_daily_jan1_ist_bar_lands_in_the_december_utc_partition(
    tmp_path: Path,
) -> None:
    """The STORED month-boundary pin: the Jan-1-IST session's daily bar stamps
    2024-12-31T18:30Z (midnight-IST labeling) and must be PHYSICALLY stored in
    the December UTC month partition — with NO January ``1day`` partition.

    (The FRESH primitive labeling is already pinned in
    ``test_spot_daily_resample.py``; this proves the physical placement
    through the real sink + prebuild.)
    """
    lake = tmp_path / "lake"
    _write_1m(
        lake, _session_bars(date(2024, 12, 31), seed=3) + _session_bars(date(2025, 1, 1), seed=4)
    )
    result = materialize_lake_timeframes(
        lake_root=lake,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
        include_daily=True,
        daily_before=date(2025, 1, 2),
    )
    assert result.unchanged is False

    december_daily = _read_stored(lake, "1day", 2024, 12)
    labels = [b.ts for b in _frame_to_buckets(december_daily)]
    assert datetime(2024, 12, 31, 18, 30) in labels  # the Jan-1-IST session's bar
    assert datetime(2024, 12, 30, 18, 30) in labels  # the Dec-31-IST session's bar
    january_daily = lake / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "1day" / "2025" / "01.parquet"
    assert not january_daily.exists(), "Jan-1-IST daily wrongly stored in a January partition"
