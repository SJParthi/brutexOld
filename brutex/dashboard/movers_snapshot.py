"""Point-in-time "movers" snapshot core — the markets-screen-style `/movers` view (pure).

``DEC-MOVERS-SNAPSHOT-VIEW-001``. The redesigned ``/movers`` page asks ONE
question — *"who are the movers as of a single date, on one timeframe, at one
time?"* — instead of the prior `[from, to]` window. This module is the **pure,
I/O-free core** that answers it; the live lake read + resample + the route wire
it up separately (so 100% of this logic is unit-testable with plain
:class:`~brutex.data.models.CandleBar` lists).

## The locked calculation rules (operator-confirmed 2026-06-13)

The trading session is **09:15 → 15:30 IST** (CLAUDE.md §7); every intraday
metric anchors at the 09:15 open.

* **% change is vs the PREVIOUS day's close** (the standard market convention,
  matches the broker markets screens the operator referenced):
  ``change = ltp − prev_day_close`` and
  ``pct_change = change / prev_day_close × 100`` — where ``ltp`` is the close of
  the **selected** bar (date · timeframe · time) and ``prev_day_close`` is the
  prior trading day's 1-day close. ``None`` when there is no previous day (the
  first day in the lake) — reported honestly, never faked.
* **Volume is the FRESH current session only** — NO previous day. It is the
  **cumulative** traded quantity from 09:15 up to the selected bar
  (``Σ bar.volume``). Volume resets to 0 each morning, so there is no
  "previous-close" baseline for it (the operator's exact ask).
* **Value (₹)** is the cumulative traded value (turnover) over those same bars
  (``Σ round_money((high+low+close)/3) × volume`` — the per-bar HLC3 typical price ×
  volume, the closest-to-broker VWAP-proxy turnover of DEC-MOVERS-VWAP-VALUE-001),
  kept as int64 paisa via :mod:`brutex.money` and rendered in ₹ crore for the table.

## Honesty (Golden Rule #1 / #10)

REFERENCE_ONLY over the **ingested lake** (CLAUDE.md §3) — the numbers are
exactly as-of the date+timeframe+time the operator picks, **from the data they
pulled**, NOT a live feed (Track-1 has no live feed — CLAUDE.md §1). Prices are
2dp money (``DEC-MONEY-2DP-001``); the % / ratio metrics keep full precision.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as date_cls
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from brutex.dashboard.movers_resilient import log_movers_skip, movers_read_skip_errors
from brutex.data.models import CandleBar
from brutex.data.spot_features import pct_change
from brutex.data.timeframes import LAKE_INTERVAL_LABELS
from brutex.money import format_inr, round_money, rupees_to_paisa

if TYPE_CHECKING:
    import polars as pl

__all__ = [
    "DEFAULT_INTERVAL",
    "DEFAULT_TOP_N",
    "MoverSnapshot",
    "ScreenInstrument",
    "SpotCandidates",
    "build_snapshot_payload",
    "compute_snapshot",
    "compute_spot_candidates",
    "interval_minutes",
    "parse_snapshot_date",
    "pending_asset_payload",
    "run_movers_snapshot",
    "session_bar_times",
    "session_traded_value_paisa",
]

#: The page opens on the coarse 1-day timeframe (cheapest, always populated if
#: ANY spot was pulled) and a generous depth (the broker markets screens list many rows).
DEFAULT_INTERVAL: Final[str] = "1day"
#: Top-N default is 100 (DEC-MOVERS-COMPLETENESS-001 — operator-directed "minimum 100
#: always"); the broker markets screens list deep, and the candidate set is computed
#: once per coordinate then ranked cheaply, so a larger N costs only the in-memory sort.
DEFAULT_TOP_N: Final[int] = 100

#: IST is a fixed +05:30 (no DST) — the lake stores bar time as tz-aware UTC.
_IST: Final[str] = "Asia/Kolkata"

#: Lake interval label → its bar length in minutes (the inverse of the single
#: source of truth :data:`brutex.data.timeframes.LAKE_INTERVAL_LABELS`, so the
#: minute↔label vocab can never drift — CLAUDE.md §11 COMMON).
_INTERVAL_MINUTES: Final[dict[str, int]] = {
    label: minutes for minutes, label in LAKE_INTERVAL_LABELS.items()
}

#: NSE/BSE regular session bounds as minutes-from-midnight IST: 09:15 inclusive
#: → 15:30 exclusive (CLAUDE.md §7 — `brutex.data.session` enforces the same).
_SESSION_OPEN_MIN: Final[int] = 9 * 60 + 15
_SESSION_CLOSE_MIN: Final[int] = 15 * 60 + 30

#: A timeframe whose bar length spans the whole session (1day = 375 min) has no
#: intraday time-of-day choice — the bar IS the day.
_DAILY_MIN_FLOOR: Final[int] = _SESSION_CLOSE_MIN - _SESSION_OPEN_MIN  # 375


def interval_minutes(interval: str) -> int | None:
    """Return *interval*'s bar length in minutes, or ``None`` if unknown.

    Looks the canonical lake label (``"1minute"`` / ``"5minute"`` / ``"1day"`` …)
    up in the inverted :data:`brutex.data.timeframes.LAKE_INTERVAL_LABELS` vocab.
    ``None`` for any label outside the 21-timeframe set.
    """
    return _INTERVAL_MINUTES.get(interval)


def session_bar_times(interval: str) -> list[str]:
    """Return the ``HH:MM`` IST start-times of every bar of *interval* in a session.

    The "time" dropdown auto-fills from the chosen timeframe: a 5-minute
    timeframe yields ``["09:15", "09:20", … "15:25"]``; 1-hour yields
    ``["09:15", "10:15", … "15:15"]``. A daily (or unknown) timeframe yields
    ``[]`` — there is no intraday time to pick, the bar is the whole day.

    Every returned time ``t`` satisfies ``09:15 ≤ t < 15:30`` (the regular
    session, CLAUDE.md §7); the list is ascending and starts at 09:15.
    """
    minutes = interval_minutes(interval)
    if minutes is None or minutes >= _DAILY_MIN_FLOOR:
        return []
    times: list[str] = []
    cursor = _SESSION_OPEN_MIN
    while cursor < _SESSION_CLOSE_MIN:
        times.append(f"{cursor // 60:02d}:{cursor % 60:02d}")
        cursor += minutes
    return times


@dataclass(frozen=True, slots=True)
class ScreenInstrument:
    """One lake series to snapshot — its partition coordinates + display name."""

    symbol: str
    exchange: str
    segment: str
    display_name: str


@dataclass(frozen=True, slots=True)
class MoverSnapshot:
    """One instrument's as-of-snapshot metrics (the locked rules, full precision).

    Attributes
    ----------
    instrument
        The screened series (symbol / exchange / segment / display name).
    ltp
        Last traded price — the **selected bar's** close, 2dp money.
    prev_close
        The previous trading day's 1-day close (2dp money), or ``None`` when
        there is no prior day in the lake.
    change
        ``ltp − prev_close`` (2dp money), or ``None`` when ``prev_close`` is
        ``None`` / zero.
    pct_change
        ``change / prev_close × 100`` at FULL precision (a statistical metric,
        never money-rounded — CLAUDE.md §4), or ``None``.
    volume
        Cumulative FRESH-session traded quantity 09:15 → selected bar.
    value_paisa
        Cumulative traded value (turnover) over those bars as int64 paisa —
        ``Σ round_money((high+low+close)/3) × volume`` (the per-bar HLC3 typical
        price × volume, the VWAP-proxy turnover of DEC-MOVERS-VWAP-VALUE-001).
    bar_count
        Number of session bars up to (and including) the selected bar.
    """

    instrument: ScreenInstrument
    ltp: Decimal
    prev_close: Decimal | None
    change: Decimal | None
    pct_change: Decimal | None
    volume: int
    value_paisa: int
    bar_count: int

    @property
    def ltp_inr(self) -> str:
        """The LTP as a 2dp ``₹`` display string."""
        return format_inr(rupees_to_paisa(self.ltp))

    @property
    def value_crore(self) -> str:
        """Cumulative traded value rendered in ₹ crore (2dp), e.g. ``"332.05"``.

        1 crore rupees = 1e7 rupees = 1e9 paisa; the quotient is quantised to
        two places so the table column is stable.
        """
        return str(round_money(Decimal(self.value_paisa) / Decimal(10**9)))


#: The HLC3 "typical price" divisor — ``(high + low + close) / 3``, the standard
#: per-bar VWAP proxy for turnover (DEC-MOVERS-VWAP-VALUE-001).
_HLC3_DIVISOR: Final = Decimal(3)


def session_traded_value_paisa(rows: Iterable[tuple[Decimal, Decimal, Decimal, int]]) -> int:
    """Cumulative session traded VALUE (turnover) over OHLCV bars, as int64 paisa.

    DEC-MOVERS-VWAP-VALUE-001 — the ONE COMMON "Top Value" formula (CLAUDE.md §11),
    shared by the spot snapshot AND the options screen so the two never drift. The
    turnover is::

        Σ over bars ( round_money((high + low + close) / 3) × volume )

    i.e. each bar's **typical price** (HLC3, the standard VWAP proxy) × that bar's
    volume, summed. This supersedes the prior ``Σ round_money(close) × volume``
    (last-print × volume), which priced a whole minute's volume at its CLOSING tick
    and so systematically diverged from a broker's true intraday turnover whenever
    the close sat away from the minute's volume-weighted average.

    Each *rows* entry is ``(high, low, close, volume)`` — ``Decimal`` rupees + an
    ``int`` quantity. The per-bar typical price is :func:`~brutex.money.round_money`
    -snapped to the 2dp tick grid BEFORE the ``× volume`` (DEC-MONEY-2DP-001), so each
    bar's contribution stays on the grid and the paisa total is exact (never a
    truncated float).

    **Honest envelope (Golden Rule #10):** this is the CLOSEST-to-broker turnover
    obtainable from 1-minute OHLCV — NOT a bit-exact match. A broker sums actual trade
    price × qty per TICK; without tick data the HLC3 × volume sum is the best
    volume-weighted-price approximation, and a residual divergence (the unknown
    intrabar volume distribution) remains. It degenerates to the old answer for a
    zero-range bar (``high == low == close``).
    """
    value_rupees = Decimal(0)
    for high, low, close, volume in rows:
        value_rupees += round_money((high + low + close) / _HLC3_DIVISOR) * volume
    return rupees_to_paisa(value_rupees)


def compute_snapshot(
    instrument: ScreenInstrument,
    bars: Sequence[CandleBar],
    *,
    prev_close: Decimal | None,
) -> MoverSnapshot | None:
    """Reduce one instrument's session bars (up to the selected time) to a snapshot.

    Pure — O(n) over the session bars (one pass for the cumulative volume/value).
    *bars* must be the instrument's chronological bars from the session open up to
    AND including the selected bar; an empty sequence yields ``None`` (the caller
    reports the instrument as skipped, never fabricated — Golden Rule #1).

    The last bar's close is the LTP; ``change`` / ``pct_change`` are taken vs
    *prev_close* (the previous day's 1-day close); ``volume`` / ``value`` are the
    cumulative fresh-session totals (no previous day).
    """
    if not bars:
        return None
    ltp = round_money(bars[-1].close)
    volume = sum(bar.volume for bar in bars)
    # DEC-MOVERS-VWAP-VALUE-001: turnover is the per-bar typical-price (HLC3) × volume
    # sum, via the ONE shared helper (spot + options share it — CLAUDE.md §11 COMMON).
    value_paisa = session_traded_value_paisa(
        (bar.high, bar.low, bar.close, bar.volume) for bar in bars
    )
    snapped_prev = round_money(prev_close) if prev_close is not None else None
    change = (ltp - snapped_prev) if snapped_prev is not None else None
    pct = pct_change(ltp, snapped_prev) if snapped_prev is not None else None
    return MoverSnapshot(
        instrument=instrument,
        ltp=ltp,
        prev_close=snapped_prev,
        change=change,
        pct_change=pct,
        volume=volume,
        value_paisa=value_paisa,
        bar_count=len(bars),
    )


def _sort_key(snap: MoverSnapshot) -> tuple[str, str, str]:
    """Deterministic tiebreak so equal metrics never reorder run-to-run (Rule #4)."""
    return (snap.instrument.symbol, snap.instrument.exchange, snap.instrument.segment)


def _gainers(snaps: Sequence[MoverSnapshot], top_n: int) -> list[MoverSnapshot]:
    """Top-``n`` snapshots with ``pct_change > 0``, descending (stable tiebreak)."""
    pool = [s for s in snaps if s.pct_change is not None and s.pct_change > 0]
    pool.sort(key=lambda s: (-(s.pct_change or Decimal(0)), _sort_key(s)))
    return pool[:top_n]


def _losers(snaps: Sequence[MoverSnapshot], top_n: int) -> list[MoverSnapshot]:
    """Top-``n`` snapshots with ``pct_change < 0``, ascending (most negative first)."""
    pool = [s for s in snaps if s.pct_change is not None and s.pct_change < 0]
    pool.sort(key=lambda s: ((s.pct_change or Decimal(0)), _sort_key(s)))
    return pool[:top_n]


def _by_volume(snaps: Sequence[MoverSnapshot], top_n: int) -> list[MoverSnapshot]:
    """Top-``n`` snapshots by cumulative fresh-session volume, descending."""
    pool = [s for s in snaps if s.volume > 0]
    pool.sort(key=lambda s: (-s.volume, _sort_key(s)))
    return pool[:top_n]


def _by_value(snaps: Sequence[MoverSnapshot], top_n: int) -> list[MoverSnapshot]:
    """Top-``n`` snapshots by cumulative fresh-session traded value, descending."""
    pool = [s for s in snaps if s.value_paisa > 0]
    pool.sort(key=lambda s: (-s.value_paisa, _sort_key(s)))
    return pool[:top_n]


#: Spot category key → ranking function (the redesigned page's pill set).
_SPOT_CATEGORIES: Final[dict[str, Any]] = {
    "price_gainers": _gainers,
    "price_losers": _losers,
    "volume": _by_volume,
    "value": _by_value,
}


def _row(snap: MoverSnapshot) -> dict[str, Any]:
    """Project one snapshot to a markets-screen-style JSON row (money 2dp, metrics full-precision)."""
    return {
        "symbol": snap.instrument.symbol,
        "name": snap.instrument.display_name,
        "exchange": snap.instrument.exchange,
        "segment": snap.instrument.segment,
        "ltp": snap.ltp_inr,
        "change": format_inr(rupees_to_paisa(snap.change)) if snap.change is not None else None,
        "pct_change": str(snap.pct_change) if snap.pct_change is not None else None,
        "value_cr": snap.value_crore,
        "volume": snap.volume,
        "bar_count": snap.bar_count,
    }


def build_snapshot_payload(
    snapshots: Sequence[MoverSnapshot],
    *,
    interval: str,
    date: str,
    time: str | None,
    top_n: int,
    skipped: int,
) -> dict[str, Any]:
    """Assemble the markets-screen-style ``/api/v1/movers`` JSON from computed snapshots.

    Builds one ranked list **per spot category** (``price_gainers`` /
    ``price_losers`` / ``volume`` / ``value`` — the redesigned page's pills), keyed
    by the category name so the page's ``j[category]`` lookup picks the active one.
    Adds the echo of the chosen ``date`` / ``interval`` / ``time`` and an honest
    ``note``. Every value is JSON-native (money is a ``₹`` string, statistical
    metrics are full-precision strings, value is a ₹-crore string) so Starlette's
    ``JSONResponse`` never chokes on a ``Decimal``.
    """
    ranked: dict[str, list[dict[str, Any]]] = {
        category: [_row(s) for s in rank_fn(snapshots, top_n)]
        for category, rank_fn in _SPOT_CATEGORIES.items()
    }
    instrument_count = len(snapshots) + skipped
    if instrument_count == 0:
        note = (
            "No spot data in the lake yet — pull spot first on the Coverage page, "
            "then refresh. (Reference-only lake view, CLAUDE.md §3.)"
        )
    elif not any(ranked.values()):
        note = (
            f"{instrument_count} instrument(s) in the lake, but none had a bar at "
            f"{date} {time or '(daily)'} on {interval} — pick another time/day, or pull more."
        )
    else:
        note = (
            f"As-of {date} {time or '(daily)'} · {interval} — reference-only lake view "
            "(CLAUDE.md §3); % is vs the previous day's close, volume is the fresh session."
        )
    return {
        "interval": interval,
        "date": date,
        "time": time,
        "top_n": top_n,
        "instrument_count": instrument_count,
        "skipped_count": skipped,
        "note": note,
        **ranked,
    }


def pending_asset_payload(
    asset: str, *, interval: str, date: str, time: str | None, top_n: int
) -> dict[str, Any]:
    """An honest "not wired yet" payload for the Futures / Options tabs.

    This milestone ships the **Spot** tab; Futures + Options follow. Returns the
    same echo shape as :func:`build_snapshot_payload` (so the page's as-of line +
    note still render) with no rows and a note that names the asset — never a
    500, never a fabricated row (Golden Rule #1 / #10). The page reads
    ``j[category]`` and falls back to ``[]`` for the absent category keys.
    """
    return {
        "interval": interval,
        "date": date,
        "time": time,
        "top_n": top_n,
        "instrument_count": 0,
        "skipped_count": 0,
        "note": (
            f"{asset.title()} movers ship after Spot — this milestone wires the Spot tab; "
            "Futures + Options follow. Reference-only lake view (CLAUDE.md §3)."
        ),
    }


# ────────────────────────────────────────────────────────────────────────
# Live orchestration — reads the lake, resamples, snapshots (spot)
# ────────────────────────────────────────────────────────────────────────


def parse_snapshot_date(date_str: str | None) -> date_cls:
    """Parse an ISO ``YYYY-MM-DD`` *date_str* (blank ⇒ today UTC), or raise ``ValueError``.

    The single date the snapshot is taken on (the redesigned page's one date
    picker, not a range). A malformed value raises :class:`ValueError` naming the
    bad input (the route maps it to a 400).
    """
    if not date_str:
        return datetime.now(UTC).date()
    try:
        return date_cls.fromisoformat(date_str)
    except ValueError as exc:
        raise ValueError(f"malformed date {date_str!r} — expected ISO YYYY-MM-DD") from exc


def _cutoff_minute(time_str: str | None) -> int | None:
    """Parse an ``HH:MM`` IST time to a minute-of-day cutoff, or ``None`` for whole-day.

    Raises :class:`ValueError` on a malformed time (the route maps it to a 400).
    """
    if not time_str:
        return None
    try:
        hh, mm = time_str.split(":")  # wrong arity → ValueError (unpack), as does a bad int.
        return int(hh) * 60 + int(mm)
    except ValueError as exc:
        raise ValueError(f"malformed time {time_str!r} — expected HH:MM") from exc


def _row_to_bar(row: Mapping[str, Any]) -> CandleBar:
    """Convert one resampled polars row (``ts_utc``/OHLCV) to a :class:`CandleBar`."""
    ts = row["ts_utc"]
    if ts.tzinfo is None:  # the resampler may drop tz — the lake instant is UTC.
        ts = ts.replace(tzinfo=UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        volume=int(row["volume"]),
        open_interest=None,
    )


def _read_session_bars(
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    day: date_cls,
    minutes: int,
    cutoff_min: int | None,
) -> list[CandleBar]:
    """Read *day*'s 1-minute bars, resample to *minutes*, slice to ``≤ cutoff_min``.

    Reads *day*'s bars via :func:`~brutex.data.lake_day_glob.read_day_intraday_frame`
    — ONLY ``1minute/<yyyy>/<mm>.parquet`` for a SESSION-FILTERED symbol (an IST
    trading day's 1-minute bars all live in that one monthly file, the session
    never crossing a UTC month boundary), so the slice is byte-equivalent to a
    whole-partition read but ``O(one month)`` not ``O(all-bars)``
    (DEC-MOVERS-O1-LATENCY-001); a SESSION-EXEMPT symbol (GIFT Nifty, ~21h) falls
    back to the whole partition there to stay byte-equivalent (its full-day series
    can cross a UTC month boundary). The frame is renamed to the resampler's
    ``ts_utc`` shape (matching the COMMON
    :func:`~brutex.data.timeframe_materialize.make_lake_read_1m` columns, §11),
    then :func:`~brutex.data.tf_resampler.resample_1m_to_tf` buckets to the chosen
    timeframe (1minute → no-op, 1day → the single session bar). For an intraday
    timeframe the bars are kept up to (and including) the bar whose IST
    open-minute ``≤ cutoff_min``. Empty when the symbol has no 1-minute bars on
    *day* (the caller reports it skipped, never fabricated — Golden Rule #1).
    """
    import polars as pl

    from brutex.data.lake_day_glob import read_day_intraday_frame
    from brutex.data.tf_resampler import resample_1m_to_tf

    base = lake_root / "bars" / exchange / segment / symbol / "1minute"
    month = read_day_intraday_frame(base, day, symbol=symbol)
    if month is None:
        return []
    df = (
        month.rename({"timestamp": "ts_utc"})
        .select("ts_utc", "open", "high", "low", "close", "volume")
        .sort("ts_utc")
    )
    ist_date = pl.col("ts_utc").dt.convert_time_zone(_IST).dt.date()
    day_df = df.filter(ist_date == day).sort("ts_utc")
    if day_df.is_empty():
        return []
    res = resample_1m_to_tf(day_df, minutes).sort("ts_utc")
    if cutoff_min is not None:
        ist = pl.col("ts_utc").dt.convert_time_zone(_IST)
        # Cast to Int32 BEFORE the arithmetic — `dt.hour()`/`dt.minute()` are
        # narrow signed ints (Int8), so `hour * 60` overflows (e.g. 15*60=900
        # wraps to a negative Int8); the cast gives the true minute-of-day.
        minute_of_day = ist.dt.hour().cast(pl.Int32) * 60 + ist.dt.minute().cast(pl.Int32)
        res = res.filter(minute_of_day <= cutoff_min)
    if res.is_empty():
        return []
    return [_row_to_bar(row) for row in res.iter_rows(named=True)]


def _read_daily_partition(
    lake_root: Path, *, exchange: str, segment: str, symbol: str
) -> pl.DataFrame | None:
    """Return the symbol's full stored ``1day`` frame, or ``None`` when absent.

    The one place that opens the ``<lake>/bars/.../1day`` partitions — both the
    prior-day-close lookup and the daily-snapshot reader go through it, so the
    path + the parquet-existence guard live in exactly one spot (CLAUDE.md §11
    COMMON). ``None`` when the directory is missing or holds no parquet (a
    never-pulled symbol), so every caller degrades honestly (Golden Rule #1).
    """
    import polars as pl

    base = lake_root / "bars" / exchange / segment / symbol / "1day"
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return None
    return pl.read_parquet(base.as_posix() + "/**/*.parquet")


def _read_daily_bars(
    lake_root: Path, *, exchange: str, segment: str, symbol: str, day: date_cls
) -> list[CandleBar]:
    """Read *day*'s stored ``1day`` bar(s) directly — NO 1-minute resample here.

    1-minute is the ONLY fetched granularity and the ``1day`` partition is DERIVED
    from it (``DEC-1MINUTE-ONLY-DERIVE-1DAY-001``), so the daily movers timeframe
    reads the already-derived ``1day`` bars straight off disk (the same partitions
    :func:`_prev_day_close` reads) instead of re-resampling the ``1minute`` series
    here. For a daily timeframe "the bar IS the day": there is no intraday
    cutoff. Returns the (≤1) bar whose IST trading date equals *day*, or ``[]``
    when the symbol has no 1-day bar on *day* (the caller reports it skipped,
    never fabricated — Golden Rule #1).
    """
    import polars as pl

    df = _read_daily_partition(lake_root, exchange=exchange, segment=segment, symbol=symbol)
    if df is None:
        return []
    sel = df.filter(pl.col("timestamp").dt.convert_time_zone(_IST).dt.date() == day).sort(
        "timestamp"
    )
    if sel.is_empty():
        return []
    # The 1-day partition stores the lake instant in `timestamp`; the shared
    # `_row_to_bar` reads `ts_utc` — rename so the one converter serves both
    # the daily and the resampled-intraday readers (CLAUDE.md §11 COMMON).
    return [_row_to_bar(row) for row in sel.rename({"timestamp": "ts_utc"}).iter_rows(named=True)]


def _prev_day_close(
    lake_root: Path, *, exchange: str, segment: str, symbol: str, day: date_cls
) -> Decimal | None:
    """Return the previous trading day's 1-day close before *day*, or ``None``.

    Reads the symbol's stored ``1day`` partitions and returns the close of the
    last bar whose IST trading date is strictly before *day*. ``None`` when there
    is no 1-day data or no prior day (the first day in the lake) — the snapshot
    then reports an honest ``None`` % change, never a fabricated base.
    """
    import polars as pl

    df = _read_daily_partition(lake_root, exchange=exchange, segment=segment, symbol=symbol)
    if df is None:
        return None
    prior = df.filter(pl.col("timestamp").dt.convert_time_zone(_IST).dt.date() < day).sort(
        "timestamp"
    )
    if prior.is_empty():
        return None
    return Decimal(str(prior.tail(1).get_column("close")[0]))


@dataclass(frozen=True, slots=True)
class SpotCandidates:
    """The cacheable spot candidate set for one snapshot coordinate (pre-ranking).

    The expensive, ``top_n``-independent product of one lake scan — the per-instrument
    :class:`MoverSnapshot` list + the skipped count + the normalised snapshot date.
    Caching THIS (keyed by ``(exchange, interval, date, time)`` via
    :class:`~brutex.dashboard.movers_cache.MoversSnapshotCache`) makes re-clicking
    a sub-filter pill or nudging ``top_n`` an O(1) hit + a cheap re-rank
    (:func:`build_snapshot_payload`), never a re-scan (DEC-MOVERS-O1-LATENCY-001).

    Attributes
    ----------
    snapshots
        Every screened instrument's :class:`MoverSnapshot` (those with a bar on
        the day); ranked per category at build time.
    skipped
        Count of instruments OMITTED from the ranking — either no bar on the
        selected day/time OR an unreadable Parquet file (logged as
        ``movers.parquet_skip``; DEC-MOVERS-CORRUPT-SKIP-001) — reported
        honestly, never fabricated (Golden Rule #1).
    date
        The normalised ISO ``YYYY-MM-DD`` snapshot day (``date=None`` ⇒ today).
    """

    snapshots: tuple[MoverSnapshot, ...]
    skipped: int
    date: str


def compute_spot_candidates(
    *,
    lake_root: Path,
    interval: str,
    date: str | None,
    time: str | None,
    exchange: str | None = None,
    names: Mapping[str, str] | None = None,
) -> SpotCandidates:
    """Scan the lake → the spot :class:`SpotCandidates` for one snapshot coordinate.

    The expensive, cacheable core extracted from :func:`run_movers_snapshot`:
    discovers every spot instrument that has data at the timeframe being asked
    for (:func:`~brutex.dashboard.db_view.list_candle_datasets`, optionally scoped
    to *exchange*), reads each one's selected-day bar(s) at *interval* up to
    *time*, takes the previous day's 1-day close, and computes the per-instrument
    :class:`MoverSnapshot`. Ranking + the per-category payload is NOT done here —
    it is the cheap, ``top_n``-dependent step done by :func:`build_snapshot_payload`.

    The discovery + read path depends on the timeframe (so the page works for
    EITHER timeframe — ``DEC-1MINUTE-ONLY-DERIVE-1DAY-001``):

    * **Daily** (``1day``, the page default) → discover + read the stored
      ``1day`` partitions DIRECTLY (:func:`_read_daily_bars`). The ``1day`` series
      is DERIVED from the fetched 1-minute, so a daily snapshot reads the derived
      bars off disk and never re-resamples here.
    * **Intraday** (``1minute`` … ``4hour``) → discover the ``1minute``
      partitions and resample to *interval* up to *time* (:func:`_read_session_bars`).

    An empty lake (or an unknown interval) yields an empty :class:`SpotCandidates`
    — never a 500, never a fabricated mover (Golden Rule #1).

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    interval, date, time
        The snapshot coordinates (date blank ⇒ today; time blank/daily ⇒ whole day).
    exchange
        Restrict the screened population to one exchange (``NSE`` / ``BSE``) for an
        exact per-exchange ranking; ``None`` screens every exchange.
    names
        Optional ``symbol → display name`` map.

    Returns
    -------
    SpotCandidates
        The per-instrument snapshots + skipped count + normalised date.

    Raises
    ------
    ValueError
        On a malformed *date* / *time* (the route maps it to a 400).
    """
    from brutex.dashboard.db_view import list_candle_datasets

    minutes = interval_minutes(interval)
    day = parse_snapshot_date(date)
    date_str = day.isoformat()
    if minutes is None:
        return SpotCandidates(snapshots=(), skipped=0, date=date_str)

    is_daily = minutes >= _DAILY_MIN_FLOOR
    cutoff_min = None if is_daily else _cutoff_minute(time)
    resolved_names = names or {}
    # A daily snapshot reads the `1day` partitions directly (the `1day` series is
    # DERIVED from the fetched 1-minute — DEC-1MINUTE-ONLY-DERIVE-1DAY-001); an
    # intraday snapshot resamples the `1minute` partitions. The exchange scope (when
    # given) narrows discovery so only that exchange's partitions are ever read.
    discovery_interval = "1day" if is_daily else "1minute"
    datasets = [
        d
        for d in list_candle_datasets(lake_root)
        if d["interval"] == discovery_interval and (exchange is None or d["exchange"] == exchange)
    ]
    if not datasets:
        return SpotCandidates(snapshots=(), skipped=0, date=date_str)

    snapshots: list[MoverSnapshot] = []
    skipped = 0
    skip_errors = movers_read_skip_errors()
    for dataset in datasets:
        try:
            if is_daily:
                bars = _read_daily_bars(
                    lake_root,
                    exchange=dataset["exchange"],
                    segment=dataset["segment"],
                    symbol=dataset["symbol"],
                    day=day,
                )
            else:
                bars = _read_session_bars(
                    lake_root,
                    exchange=dataset["exchange"],
                    segment=dataset["segment"],
                    symbol=dataset["symbol"],
                    day=day,
                    minutes=minutes,
                    cutoff_min=cutoff_min,
                )
            # Skip the prior-day 1-day read entirely when the symbol had no bars on
            # the selected day (no point computing a baseline for nothing).
            prev_close = (
                _prev_day_close(
                    lake_root,
                    exchange=dataset["exchange"],
                    segment=dataset["segment"],
                    symbol=dataset["symbol"],
                    day=day,
                )
                if bars
                else None
            )
        except skip_errors as exc:
            # A corrupt / unreadable Parquet for ONE symbol must not 500 the whole
            # screen — skip it (logged + counted), never fabricate (DEC-MOVERS-CORRUPT-SKIP-001).
            log_movers_skip(source="spot", symbol=dataset["symbol"], error=str(exc))
            skipped += 1
            continue
        instrument = ScreenInstrument(
            symbol=dataset["symbol"],
            exchange=dataset["exchange"],
            segment=dataset["segment"],
            display_name=resolved_names.get(dataset["symbol"], dataset["symbol"]),
        )
        snap = compute_snapshot(instrument, bars, prev_close=prev_close)
        if snap is None:  # no bars on the selected day → reported skipped, never faked.
            skipped += 1
            continue
        snapshots.append(snap)

    return SpotCandidates(snapshots=tuple(snapshots), skipped=skipped, date=date_str)


def run_movers_snapshot(
    *,
    lake_root: Path,
    interval: str,
    date: str | None,
    time: str | None,
    top_n: int,
    exchange: str | None = None,
    names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compute the spot movers snapshot over the lake → the ``/api/v1/movers`` payload.

    The thin compose of :func:`compute_spot_candidates` (the expensive, cacheable
    scan) + :func:`build_snapshot_payload` (the cheap, ``top_n``-dependent rank).
    Behaviour is unchanged from the pre-split version — an empty lake (or an
    unknown interval) yields an honest "pull spot first" payload, never a 500
    (Golden Rule #1).

    Parameters
    ----------
    lake_root, interval, date, time, top_n, exchange, names
        See :func:`compute_spot_candidates` (plus *top_n*, the per-category
        ranking depth).

    Raises
    ------
    ValueError
        On a malformed *date* / *time* (the route maps it to a 400).
    """
    candidates = compute_spot_candidates(
        lake_root=lake_root,
        interval=interval,
        date=date,
        time=time,
        exchange=exchange,
        names=names,
    )
    return build_snapshot_payload(
        candidates.snapshots,
        interval=interval,
        date=candidates.date,
        time=time,
        top_n=top_n,
        skipped=candidates.skipped,
    )
