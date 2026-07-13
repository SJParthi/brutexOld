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


# ────────────────────────────────────────────────────────────────────────
# DEC-DERIVED-TF-AUDIT-001 — the audit's oracle extensions. Six legs the
# original harness did not cover: Muhurat/evening + 13:45-open minute draws,
# pre-09:15 whole-minute draws (the P2 formula-parity capture), longer
# multi-day frames across a weekend gap, the FNO STORED-lake OI leg (the
# DEC-DERIVED-OI-NULL-HONEST-001 permanent net — fails at the merge base),
# the as-of cut leg through the movers filter, and the OI strict-last
# discrimination pin (the docstring-literal semantic E1 proved: 1,670/1,670
# ambiguity buckets == strict-last; "last non-null" REFUTED).
# ────────────────────────────────────────────────────────────────────────

#: Calendar-verified off-09:15 session opens (IST wall-clock): the 2024
#: Diwali Muhurat evening (18:00) and the 2025 afternoon Muhurat (13:45).
_EVENING_POOLS: tuple[tuple[date, time], ...] = (
    (date(2024, 11, 1), time(18, 0)),
    (date(2024, 11, 1), time(18, 15)),
    (date(2025, 10, 21), time(13, 45)),
)


@st.composite
def _evening_frames(draw: st.DrawFn) -> pl.DataFrame:
    """Minute draws INSIDE a Muhurat/evening session (offsets 0..59)."""
    day, open_ist = draw(st.sampled_from(_EVENING_POOLS))
    open_utc = datetime.combine(day, open_ist) - _IST_OFFSET
    present = draw(
        st.lists(st.integers(min_value=0, max_value=59), min_size=1, max_size=25, unique=True)
    )
    rows = []
    for minute in sorted(present):
        open_, high, low, close = _draw_ohlc(draw)
        rows.append(
            (
                open_utc + timedelta(minutes=minute),
                open_,
                high,
                low,
                close,
                draw(st.integers(min_value=0, max_value=1_000_000)),
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


@given(df=_evening_frames())  # pyright: ignore[reportCallIssue]  # @st.composite injects draw
@settings(max_examples=60, deadline=None)
def test_muhurat_evening_frames_match_the_oracle(df: pl.DataFrame) -> None:
    """Off-09:15 session opens: the fixed 09:15-anchored grid puts the first
    in-session label BEFORE the open on non-divisor TFs (the E1 B-S1 class —
    17:45 @30m on an 18:00 open) — production must equal the independent
    oracle bucket-for-bucket there too."""
    for tf in (2, 5, 30, 60, 240):
        out = resample_1m_to_tf(df, tf)
        assert _frame_to_buckets(out) == _oracle_intraday(df, tf), f"tf={tf}: oracle disagreement"


@st.composite
def _pre_open_frames(draw: st.DrawFn) -> pl.DataFrame:
    """Whole-minute bars BEFORE 09:15 IST (00:00..09:14 — the P2 shape)."""
    day = date(2024, 1, 2)
    midnight_utc = datetime.combine(day, time(0, 0)) - _IST_OFFSET  # 00:00 IST
    present = draw(
        st.lists(st.integers(min_value=0, max_value=554), min_size=1, max_size=15, unique=True)
    )
    rows = []
    for minute in sorted(present):
        open_, high, low, close = _draw_ohlc(draw)
        rows.append((midnight_utc + timedelta(minutes=minute), open_, high, low, close, 10))
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


@given(df=_pre_open_frames())  # pyright: ignore[reportCallIssue]  # @st.composite injects draw
@settings(max_examples=60, deadline=None)
def test_pre_open_whole_minute_frames_match_the_oracle(df: pl.DataFrame) -> None:
    """The P2 formula-parity capture: a whole-minute bar BEFORE 09:15 IST
    floors to a NEGATIVE bucket rank on both sides — labels land on the
    pre-open (possibly PRIOR-IST-date) grid, production == oracle exactly.
    (The tf_resampler Anchor prose was corrected by DEC-DERIVED-TF-AUDIT-001
    to name this: 'buckets never straddle sessions' held only for
    in-session bars. Dormant in production — ingest session-filters
    pre-open bars; DEC-SESSION-EXEMPT-RESAMPLE-001 is the follow-up.)"""
    for tf in (2, 30, 240):
        out = resample_1m_to_tf(df, tf)
        assert _frame_to_buckets(out) == _oracle_intraday(df, tf), f"tf={tf}: oracle disagreement"


@st.composite
def _weekend_gap_frames(draw: st.DrawFn) -> pl.DataFrame:
    """3..5 sessions spanning a REAL weekend gap (Fri 2024-03-22 → Thu)."""
    pool = [
        date(2024, 3, 22),  # Friday
        date(2024, 3, 26),  # Tuesday (the 25th is Holi — a real holiday gap)
        date(2024, 3, 27),
        date(2024, 3, 28),
        date(2024, 4, 1),  # Monday across the month boundary
    ]
    n_days = draw(st.integers(min_value=3, max_value=5))
    rows = []
    for day in pool[:n_days]:
        day_open_utc = datetime.combine(day, _SESSION_OPEN_UTC)
        present = draw(
            st.lists(st.integers(min_value=0, max_value=374), min_size=1, max_size=15, unique=True)
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
                    draw(st.integers(min_value=0, max_value=1_000_000)),
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


@given(
    df=_weekend_gap_frames(),  # pyright: ignore[reportCallIssue]  # @st.composite injects draw
    cutoff_days=st.integers(min_value=0, max_value=11),
)
@settings(max_examples=60, deadline=None)
def test_weekend_gap_multiday_daily_chain_matches_the_oracle(
    df: pl.DataFrame, cutoff_days: int
) -> None:
    """>3-day frames across a real weekend + holiday + month-boundary gap:
    the midnight-IST daily chain equals the oracle at every drawn cutoff."""
    first_ts = df.get_column("ts_utc").min()
    assert isinstance(first_ts, datetime)
    before = (_to_naive_utc(first_ts) + _IST_OFFSET).date() + timedelta(days=cutoff_days)
    got = _daily_bars_to_buckets(resample_1m_to_daily(df, before=before))
    assert got == _oracle_daily_midnight(df, before=before)
    # The intraday chain holds over the same gapped frames too.
    for tf in (30, 240):
        assert _frame_to_buckets(resample_1m_to_tf(df, tf)) == _oracle_intraday(df, tf)


def _fno_session_bars(ist_day: date, *, oi_pattern: list[int | None]) -> list[CandleBar]:
    """F&O bars (2-dp grid) whose per-minute OI follows *oi_pattern*."""
    day_open_utc = datetime.combine(ist_day, _SESSION_OPEN_UTC, tzinfo=UTC)
    bars: list[CandleBar] = []
    for minute, oi in enumerate(oi_pattern):
        px = round(20_000.0 + minute * 0.05, 2)
        bars.append(
            CandleBar(
                timestamp=day_open_utc + timedelta(minutes=minute),
                open=Decimal(str(px)),
                high=Decimal(str(round(px + 0.5, 2))),
                low=Decimal(str(round(px - 0.5, 2))),
                close=Decimal(str(round(px + 0.1, 2))),
                volume=100 + minute,
                open_interest=oi,
            )
        )
    return bars


def test_fno_stored_lake_oi_matches_the_oracle_including_nulls(tmp_path: Path) -> None:
    """The FNO STORED-lake leg (DEC-DERIVED-OI-NULL-HONEST-001's permanent
    net — FAILS at the merge base, where the writer fabricated OI=0): real
    sink → real FUTURES prebuild → the STORED derived partitions' OI equals
    the oracle's carry-last OI computed FROM the stored 1-minute base,
    nulls INCLUDED."""
    lake = tmp_path / "lake"
    symbol = "NSE-NIFTY-27Jun24-FUT"
    # Dense → all-null → leading-value-then-null (strict-last: null) → mixed.
    oi_pattern: list[int | None] = (
        [1000 + i for i in range(5)] + [None] * 5 + [1010, None, None, None, None]
    )
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
    sink.append(
        exchange=_EXCHANGE,
        segment="FNO",
        symbol=symbol,
        interval="1minute",
        bars=_fno_session_bars(date(2024, 6, 12), oi_pattern=oi_pattern),
    )
    materialize_lake_timeframes(
        lake_root=lake,
        exchange=_EXCHANGE,
        segment="FNO",
        symbol=symbol,
        kind=PrebuildKind.FUTURES,
        tfs=("5m", "15m"),
        include_daily=True,
        daily_before=date(2024, 6, 13),
        incremental=False,
    )

    def _read_fno(interval: str) -> pl.DataFrame:
        path = lake / "bars" / _EXCHANGE / "FNO" / symbol / interval / "2024" / "06.parquet"
        assert path.is_file(), f"missing stored partition: {path}"
        return pl.read_parquet(path).rename({"timestamp": "ts_utc"}).sort("ts_utc")

    stored_1m = _read_fno("1minute")
    for tf in (5, 15):
        stored = _read_fno(LAKE_INTERVAL_LABELS[tf])
        assert _frame_to_buckets(stored) == _oracle_intraday(stored_1m, tf), (
            f"stored {LAKE_INTERVAL_LABELS[tf]} disagrees with the oracle (OI included)"
        )
    # The strict-last law end-to-end: [1004, None, None] — never [1004, 0, 0].
    assert _read_fno("5minute").get_column("open_interest").to_list() == [1004, None, None]
    # The derived FNO 1day carries the session-last OI — null here.
    assert _read_fno("1day").get_column("open_interest").to_list() == [None]


@given(
    df=_daily_frames(),  # pyright: ignore[reportCallIssue]  # @st.composite injects draw
    cutoff_min=st.integers(min_value=555, max_value=929),
)
@settings(max_examples=60, deadline=None)
def test_as_of_cut_then_resample_matches_the_oracle(df: pl.DataFrame, cutoff_min: int) -> None:
    """The movers as-of leg: cutting the RAW 1-minute frame at an IST
    minute-of-day (the ONE COMMON ``filter_to_ist_minute``) then resampling
    equals the oracle over the SAME cut frame — the cut and the bucketing
    laws compose without drift."""
    from brutex.dashboard.movers_snapshot import filter_to_ist_minute

    aware = df.with_columns(pl.col("ts_utc").dt.replace_time_zone("UTC"))
    cut = filter_to_ist_minute(aware, cutoff_min, ts_col="ts_utc")
    if cut.height == 0:
        return  # everything past the as-of minute — nothing to compare
    for tf in (5, 30):
        assert _frame_to_buckets(resample_1m_to_tf(cut, tf)) == _oracle_intraday(cut, tf)


def test_oi_strict_last_discrimination_pin() -> None:
    """The OI semantic E1 settled (1,670/1,670 ambiguity buckets): the
    bucket's OI is the LAST source bar's OI VERBATIM — a null last bar
    yields a null bucket OI even when an EARLIER bar in the bucket carries a
    value ('last non-null' is REFUTED as a description of production)."""
    day_open_utc = datetime.combine(date(2024, 6, 12), _SESSION_OPEN_UTC)
    df = pl.DataFrame(
        {
            "ts_utc": [day_open_utc, day_open_utc + timedelta(minutes=1)],
            "open": [100.0, 100.1],
            "high": [100.5, 100.6],
            "low": [99.5, 99.6],
            "close": [100.1, 100.2],
            "volume": [10, 20],
            "open_interest": [5, None],
        },
        schema_overrides={"open_interest": pl.Int64},
    )
    out = resample_1m_to_tf(df, 5)
    assert out.get_column("open_interest").to_list() == [None]  # strict-last, NOT 5
    # And the oracle agrees (its own strict-last law).
    assert _frame_to_buckets(out) == _oracle_intraday(df, 5)
