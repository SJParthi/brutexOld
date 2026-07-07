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

import html
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as date_cls
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from brutex.dashboard.movers_resilient import log_movers_skip, movers_read_skip_errors
from brutex.dashboard.movers_stored_read import read_stored_day_frame
from brutex.data.derive_lock import derive_in_progress
from brutex.data.models import CandleBar
from brutex.data.partition import SPOT_PROBE_SEGMENTS
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
    "filter_to_ist_minute",
    "interval_minutes",
    "is_intraday_interval",
    "minute_to_hhmm",
    "normalize_min_volume",
    "parse_snapshot_date",
    "pending_asset_payload",
    "run_movers_snapshot",
    "session_bar_times",
    "session_traded_value_paisa",
    "spot_session_value_paisa",
    "value_crore_str",
    "value_meets_volume_floor",
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


def minute_to_hhmm(minute_of_day: int) -> str:
    """Return the IST ``"HH:MM"`` string for a minute-of-day (the inverse of :func:`_cutoff_minute`).

    ``minute_to_hhmm(929) == "15:29"`` · ``minute_to_hhmm(555) == "09:15"``. The
    output matches the :func:`session_bar_times` grid string EXACTLY (zero-padded
    ``HH:MM``), so a resolved latest minute is always a selectable ``#time`` option
    on the frontend and round-trips through :func:`_cutoff_minute`
    (``_cutoff_minute(minute_to_hhmm(m)) == m``). Pure + O(1)
    (DEC-MOVERS-ASOF-LATEST-DEFAULT-001 — the displayed-minute formatter that lets
    the default as-of dropdown SHOW the real latest minute while the cut stays
    whole-day; CLAUDE.md §11 COMMON: the single minute↔label formatter).
    """
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


def is_intraday_interval(interval: str) -> bool:
    """Return ``True`` iff *interval* has an intraday as-of time-of-day choice.

    ``True`` for the intraday timeframes whose bar length is shorter than the
    whole session (``1minute`` … ``30minute`` / ``1hour`` / ``4hour`` — anything
    ``< _DAILY_MIN_FLOOR``); ``False`` for ``1day`` (the bar IS the day) and any
    unknown label. The public predicate the ``/movers`` route uses to decide
    whether to resolve the display-only ``latest_time``
    (DEC-MOVERS-ASOF-LATEST-DEFAULT-001) — so the daily-vs-intraday rule has ONE
    source of truth (CLAUDE.md §11 COMMON; the twin of the empty
    :func:`session_bar_times` daily check). O(1).
    """
    minutes = interval_minutes(interval)
    return minutes is not None and minutes < _DAILY_MIN_FLOOR


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
        """Cumulative traded value rendered in ₹ crore (2dp), e.g. ``"332.05"``."""
        return value_crore_str(self.value_paisa)


def value_crore_str(value_paisa: int) -> str:
    """Render an int64-paisa traded value (turnover) as a 2dp ₹-crore string.

    The ONE COMMON crore formatter (CLAUDE.md §11) shared by the spot/options/futures
    movers ``value_crore`` properties: 1 crore rupees = 1e7 rupees = 1e9 paisa, and the
    quotient is :func:`~brutex.money.round_money`-quantised to two places so the "Value
    (Cr)" column is stable (DEC-MONEY-2DP-001).
    """
    return str(round_money(Decimal(value_paisa) / Decimal(10**9)))


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
    value_paisa: int | None = None,
) -> MoverSnapshot | None:
    """Reduce one instrument's session bars (up to the selected time) to a snapshot.

    Pure — O(n) over the session bars (one pass for the cumulative volume/value).
    *bars* must be the instrument's chronological bars from the session open up to
    AND including the selected bar; an empty sequence yields ``None`` (the caller
    reports the instrument as skipped, never fabricated — Golden Rule #1).

    The last bar's close is the LTP; ``change`` / ``pct_change`` are taken vs
    *prev_close* (the previous day's 1-day close); ``volume`` is the cumulative
    fresh-session total (no previous day).

    ``value`` (turnover) is TF-INVARIANT (DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001):
    the caller passes *value_paisa* pre-computed from the RAW 1-minute session
    frame (:func:`spot_session_value_paisa`), so the spot turnover is a session
    property identical across every display timeframe (mirroring futures/options).
    When *value_paisa* is ``None`` (no 1-minute frame, or an injected-seam / unit
    caller) it falls back to the byte-identical legacy reduction over the passed
    display *bars* — so an omitted / ``None`` argument reproduces the pre-fix
    behaviour exactly (Golden Rule #1: never fabricated, never worse).
    """
    if not bars:
        return None
    ltp = round_money(bars[-1].close)
    volume = sum(bar.volume for bar in bars)
    # DEC-MOVERS-VWAP-VALUE-001 / DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001: turnover is
    # the per-bar HLC3 typical-price × volume sum. The caller passes it pre-computed
    # from the RAW 1-minute frame (TF-invariant, mirroring futures/options); a `None`
    # falls back to the legacy reduction over the display bars (byte-identical to the
    # pre-fix behaviour), via the ONE shared helper (CLAUDE.md §11 COMMON).
    resolved_value = (
        value_paisa
        if value_paisa is not None
        else session_traded_value_paisa((bar.high, bar.low, bar.close, bar.volume) for bar in bars)
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
        value_paisa=resolved_value,
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


def normalize_min_volume(min_volume: int | None) -> int:
    """Clamp the OPT-IN ``?min_volume=`` floor to a non-negative int (the COMMON source).

    The single source of truth (CLAUDE.md §11 COMMON) shared by the spot / futures /
    options "Top Volume" rankings (DEC-MOVERS-MIN-VOLUME-001): a ``None`` / negative
    floor normalises to ``0`` (the byte-identical default — no floor, only the prior
    zero-volume exclusion applies), so a malformed value can never invert the filter.

    Parameters
    ----------
    min_volume
        The raw ``?min_volume=`` selector (``None`` ⇒ 0 = disabled).

    Returns
    -------
    int
        ``max(0, min_volume or 0)`` — the applied floor.
    """
    return max(0, min_volume or 0)


def value_meets_volume_floor(volume: int, min_volume: int) -> bool:
    """Whether *volume* clears the volume ranking's inclusion test (the COMMON predicate).

    The ONE rule the spot / futures / options "Top Volume" pools share
    (DEC-MOVERS-MIN-VOLUME-001): a contract is included iff its cumulative session
    ``volume`` is BOTH strictly positive (the long-standing zero-volume exclusion) AND
    at least the *min_volume* floor. With the default ``min_volume == 0`` this reduces
    to ``volume > 0`` — byte-identical to the pre-floor behaviour — while a positive
    floor additionally drops thin contracts below it.

    Parameters
    ----------
    volume
        The contract's cumulative fresh-session traded quantity.
    min_volume
        The applied floor (already normalised by :func:`normalize_min_volume`).

    Returns
    -------
    bool
        ``volume > 0 and volume >= min_volume``.
    """
    return volume > 0 and volume >= min_volume


def _by_volume(
    snaps: Sequence[MoverSnapshot], top_n: int, *, min_volume: int = 0
) -> list[MoverSnapshot]:
    """Top-``n`` snapshots by cumulative fresh-session volume, descending.

    With the default ``min_volume == 0`` the pool is byte-identical to the prior
    ``volume > 0`` filter; a positive OPT-IN floor (DEC-MOVERS-MIN-VOLUME-001)
    additionally drops contracts whose volume is below it, via the COMMON
    :func:`value_meets_volume_floor` predicate.
    """
    pool = [s for s in snaps if value_meets_volume_floor(s.volume, min_volume)]
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
    min_volume: int | None = None,
    stored_reads: int = 0,
    resampled_reads: int = 0,
    latest_time: str | None = None,
) -> dict[str, Any]:
    """Assemble the markets-screen-style ``/api/v1/movers`` JSON from computed snapshots.

    Builds one ranked list **per spot category** (``price_gainers`` /
    ``price_losers`` / ``volume`` / ``value`` — the redesigned page's pills), keyed
    by the category name so the page's ``j[category]`` lookup picks the active one.
    Adds the echo of the chosen ``date`` / ``interval`` / ``time`` and an honest
    ``note``. Every value is JSON-native (money is a ``₹`` string, statistical
    metrics are full-precision strings, value is a ₹-crore string) so Starlette's
    ``JSONResponse`` never chokes on a ``Decimal``.

    *min_volume* is the OPT-IN "Top Volume" floor (DEC-MOVERS-MIN-VOLUME-001): the
    ``volume`` ranking additionally drops contracts below it (``None`` / ``0`` ⇒ the
    byte-identical no-floor default). It is applied at RANK time (the cheap step), so
    a floor change re-ranks the SAME cached candidate set — never a re-scan.

    *stored_reads* / *resampled_reads* echo the candidate set's read-path
    provenance (:attr:`SpotCandidates.stored_reads` /
    :attr:`SpotCandidates.resampled_reads` — DEC-PIPELINE-AUTOPREBUILD-001
    chunk 2): how many contributing instruments were served from the pipeline
    prebuild's STORED interval partitions vs the on-the-fly 1-minute resample
    fallback — honesty visible in the payload, nothing noisy in the UI.

    *latest_time* is the ADDITIVE display-only echo of the resolved latest
    intraday minute (``"HH:MM"`` IST, DEC-MOVERS-ASOF-LATEST-DEFAULT-001) — the
    minute the DEFAULT whole-day as-of read's last kept bar carries, so the
    dropdown/label can SHOW the real latest minute (e.g. ``latest (15:29)``)
    while the cut stays whole-day (``time`` echoes ``None``). ``None`` for an
    explicit *time*, a daily interval, or an empty lake — the frontend then
    renders a plain "latest" label. It NEVER affects the ranked numbers (the cut
    is unchanged); ``echo == cut`` in value by construction.
    """
    floor = normalize_min_volume(min_volume)
    ranked: dict[str, list[dict[str, Any]]] = {
        category: [
            _row(s)
            for s in (
                _by_volume(snapshots, top_n, min_volume=floor)
                if category == "volume"
                else rank_fn(snapshots, top_n)
            )
        ]
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
            + (f" Min volume ≥ {floor:,}." if floor > 0 else "")
        )
    return {
        "interval": interval,
        "date": date,
        "time": time,
        "top_n": top_n,
        "min_volume": floor,
        "instrument_count": instrument_count,
        "skipped_count": skipped,
        "stored_reads": stored_reads,
        "resampled_reads": resampled_reads,
        "latest_time": latest_time,
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

    **Defense-in-depth (DEC-DASHBOARD-XSS-HARDENING-001):** ``asset`` is the raw
    ``?asset=`` query param echoed back into the note, so it is HTML-escaped here
    before formatting. Even though the page now sets the note via ``textContent``
    (so an unescaped tag could not execute), the JSON payload is sanitised at the
    source so any future consumer of it is safe too — a label like
    ``<img src=x onerror=alert(1)>`` becomes inert ``&lt;img …`` text.
    """
    # Title-case FIRST, then escape — escaping is the LAST step so ``.title()``
    # cannot mangle the entities (`&lt;` → `&Lt;`) and the output is byte-clean.
    safe_asset = html.escape(asset.title())
    return {
        "interval": interval,
        "date": date,
        "time": time,
        "top_n": top_n,
        "instrument_count": 0,
        "skipped_count": 0,
        "note": (
            f"{safe_asset} movers ship after Spot — this milestone wires the Spot tab; "
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


def filter_to_ist_minute(frame: pl.DataFrame, cutoff_min: int, *, ts_col: str) -> pl.DataFrame:
    """Keep only *frame*'s rows whose IST minute-of-day is ``≤ cutoff_min``.

    THE one COMMON as-of cutoff filter for every ``/movers`` tab
    (DEC-HARDENING-LEFTOVERS-001 — kills the copy-pasted Int32 minute-of-day
    expression that lived in BOTH the spot and the futures session readers,
    CLAUDE.md §11 COMMON). The **unified as-of semantic** it owns: keep every
    RAW 1-minute bar whose IST minute-of-day is ``≤ cutoff_min``; intraday
    buckets are then formed only from ELAPSED minutes — a partially-elapsed
    bucket honestly reflects only the elapsed minutes, never a forming-bucket
    leak of minute bars PAST the as-of minute (Golden Rule #1: an "as of
    13:45" snapshot must never include any bar STARTING after 13:45; the
    inclusive 13:45 bar itself is a bucket-OPEN-stamped 1-minute bar, so it
    aggregates trades through 13:45:59 — the guarantee is minute-granular,
    not instant-granular).

    Parameters
    ----------
    frame
        A polars frame of raw 1-minute bars carrying a tz-aware UTC timestamp
        column named *ts_col* (``"ts_utc"`` in the spot reader, ``"timestamp"``
        in the futures/options reader).
    cutoff_min
        Inclusive IST minute-of-day cutoff (e.g. ``13*60+45`` for 13:45).
    ts_col
        The timestamp column name to convert + compare on.
    """
    import polars as pl

    ist = pl.col(ts_col).dt.convert_time_zone(_IST)
    # Cast to Int32 BEFORE the arithmetic — `dt.hour()`/`dt.minute()` are
    # narrow signed ints (Int8), so `hour * 60` overflows (e.g. 15*60=900
    # wraps to a negative Int8); the cast gives the true minute-of-day.
    minute_of_day = ist.dt.hour().cast(pl.Int32) * 60 + ist.dt.minute().cast(pl.Int32)
    return frame.filter(minute_of_day <= cutoff_min)


def _read_cut_1m_frame(
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    day: date_cls,
    cutoff_min: int | None,
) -> pl.DataFrame | None:
    """Read *day*'s RAW 1-minute frame, cut at ``≤ cutoff_min`` — the shared read+cut.

    The ONE COMMON raw-1m read + as-of cut (CLAUDE.md §11 COMMON) shared by BOTH
    the resampled display-bar reader (:func:`_read_session_bars`) AND the
    TF-invariant session-value reader (:func:`spot_session_value_paisa`), so the
    two never drift on the read/cut logic (no copy-paste — Golden Rule #11).

    Reads *day*'s bars via :func:`~brutex.data.lake_day_glob.read_day_intraday_frame`
    — ONLY ``1minute/<yyyy>/<mm>.parquet`` for a SESSION-FILTERED symbol (an IST
    trading day's 1-minute bars all live in that one monthly file, the session
    never crossing a UTC month boundary), so the slice is byte-equivalent to a
    whole-partition read but ``O(one month)`` not ``O(all-bars)``
    (DEC-MOVERS-O1-LATENCY-001); a SESSION-EXEMPT symbol (GIFT Nifty, ~21h) falls
    back to the whole partition there to stay byte-equivalent (its full-day series
    can cross a UTC month boundary). The frame is renamed to the resampler's
    ``ts_utc`` shape (matching the COMMON
    :func:`~brutex.data.timeframe_materialize.make_lake_read_1m` columns, §11).

    **As-of cutoff (DEC-HARDENING-LEFTOVERS-001 — the unified raw-1m semantic):**
    the ``cutoff_min`` filter is applied to the RAW 1-minute frame via the COMMON
    :func:`filter_to_ist_minute` — the same instant the futures/options session
    reader cuts at. The kept minutes are exactly those with IST minute-of-day
    ``≤ cutoff_min`` (minute-granular, not instant-granular).

    Returns the cut raw 1-minute frame, or ``None`` when the symbol has no
    1-minute bars on *day*, or none at/before the cutoff (the caller reports it
    skipped / abstains, never fabricated — Golden Rule #1).
    """
    import polars as pl

    from brutex.data.lake_day_glob import read_day_intraday_frame

    base = lake_root / "bars" / exchange / segment / symbol / "1minute"
    month = read_day_intraday_frame(base, day, symbol=symbol)
    if month is None:
        return None
    df = (
        month.rename({"timestamp": "ts_utc"})
        .select("ts_utc", "open", "high", "low", "close", "volume")
        .sort("ts_utc")
    )
    ist_date = pl.col("ts_utc").dt.convert_time_zone(_IST).dt.date()
    day_df = df.filter(ist_date == day).sort("ts_utc")
    if cutoff_min is not None:
        day_df = filter_to_ist_minute(day_df, cutoff_min, ts_col="ts_utc")
    if day_df.is_empty():
        return None
    return day_df


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
    """Read *day*'s 1-minute bars, cut at ``≤ cutoff_min``, resample to *minutes*.

    Thin wrapper over the shared :func:`_read_cut_1m_frame` (the raw-1m read + the
    unified as-of cut on the RAW frame BEFORE resampling — DEC-HARDENING-LEFTOVERS-001):
    the cut minutes are then bucketed to the chosen timeframe by
    :func:`~brutex.data.tf_resampler.resample_1m_to_tf` (1minute → no-op). The
    final bucket aggregates only the elapsed minutes ``≤ cutoff_min``, so
    LTP / volume end at the close of the as-of MINUTE (the inclusive bucket-open
    bar aggregates through its final second — minute-granular, not
    instant-granular). Empty when the symbol has no 1-minute bars on *day*, or
    none at/before the cutoff (the caller reports it skipped, never fabricated —
    Golden Rule #1).
    """
    from brutex.data.tf_resampler import resample_1m_to_tf

    day_df = _read_cut_1m_frame(
        lake_root,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        day=day,
        cutoff_min=cutoff_min,
    )
    if day_df is None:
        return []
    res = resample_1m_to_tf(day_df, minutes).sort("ts_utc")
    return [_row_to_bar(row) for row in res.iter_rows(named=True)]


def spot_session_value_paisa(
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    day: date_cls,
    cutoff_min: int | None,
) -> int | None:
    """SPOT session turnover (₹ paisa) from the RAW 1-minute frame — TF-INVARIANT.

    DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001. The spot "Top Value" turnover, sourced
    from the raw 1-minute session frame (post as-of cut, PRE-resample), exactly
    mirroring the already-TF-invariant futures/options reader
    (:func:`~brutex.dashboard.movers_futures._read_future_session`). Because it
    takes NO ``interval`` / ``minutes`` argument — value is a pure function of
    ``(exchange, segment, symbol, day, cutoff_min)`` — the turnover is a genuine
    session property: at a shared effective as-of it is BYTE-identical across every
    display timeframe (1minute … 1day), curing the prior per-display-bucket-HLC3
    divergence where value(5m) ≠ value(1m) ≠ value(1day).

    Reduces via the ONE COMMON :func:`session_traded_value_paisa`
    (``Σ round_money((high+low+close)/3) × volume`` — per-bar HLC3 typical price ×
    volume, DEC-MOVERS-VWAP-VALUE-001; per-bar ``round_money`` 2dp BEFORE the ×
    volume, DEC-MONEY-2DP-001). The ``Decimal(str(...))`` per-column construction
    is IDENTICAL to :func:`_row_to_bar` and the futures reader, so the value read
    carries zero float-noise divergence from the display read.

    Returns the int64-paisa turnover, or ``None`` when the symbol has no 1-minute
    bars on *day* (or none at/before the cutoff) — never a fabricated value
    (Golden Rule #1); the caller then falls back to the display-bar value.
    """
    day_df = _read_cut_1m_frame(
        lake_root,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        day=day,
        cutoff_min=cutoff_min,
    )
    if day_df is None:
        return None
    return session_traded_value_paisa(
        (Decimal(str(high)), Decimal(str(low)), Decimal(str(close)), int(vol))
        for high, low, close, vol in zip(
            day_df.get_column("high").to_list(),
            day_df.get_column("low").to_list(),
            day_df.get_column("close").to_list(),
            day_df.get_column("volume").to_list(),
            strict=True,
        )
    )


def _stored_serves_cutoff(minutes: int, cutoff_min: int | None) -> bool:
    """``True`` iff the stored *minutes*-bucket partition can serve *cutoff_min* exactly.

    The stored partitions hold FULL resampled buckets, while the unified as-of
    semantic (DEC-HARDENING-LEFTOVERS-001 — :func:`filter_to_ist_minute` on the
    RAW 1-minute frame BEFORE resampling) makes the final bucket a PARTIAL
    aggregate of only the elapsed minutes. A stored read is therefore byte-equal
    to the resample path only when every kept bucket is fully elapsed at the
    cutoff:

    * ``cutoff_min is None`` — the whole-day read (every bucket final), or
    * the cutoff sits on a bucket's FINAL minute of the 09:15-anchored grid
      (``(cutoff_min - _SESSION_OPEN_MIN + 1) % minutes == 0`` — e.g. 09:19 /
      09:24 for 5-minute; every minute for 1-minute).

    A MID-bucket as-of must aggregate a partial forming bucket, which only the
    1-minute base can build — the caller falls back to the on-the-fly resample
    (honest ``resampled_reads`` provenance, never a forming-bucket leak of
    minutes past the as-of instant — Golden Rule #1).
    """
    if cutoff_min is None:
        return True
    return (cutoff_min - _SESSION_OPEN_MIN + 1) % minutes == 0


def _read_intraday_bars(
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    day: date_cls,
    minutes: int,
    cutoff_min: int | None,
    prefer_resample: bool = False,
) -> tuple[list[CandleBar], bool]:
    """STORED-FIRST intraday day read → ``(bars, from_stored)``.

    *prefer_resample* (DEC-MOVERS-GATE-PREBUILD-SCOPE-001): ``True`` skips the
    stored-partition read entirely and takes the always-fresh 1-minute
    resample. The ``/movers`` route passes it WHILE a dashboard pull is
    running in this process: the stored TF partitions are rewritten only by
    the pipeline's FINAL prebuild stage, so mid-fetch they can predate the
    freshly-landing 1-minute bars — the resample is byte-equal semantics per
    DEC-PIPELINE-AUTOPREBUILD-001's pinned equality, just slower and ALWAYS
    fresh. Default ``False`` keeps the stored-first read byte-identical.

    DEC-PIPELINE-AUTOPREBUILD-001 (chunk 2, the movers arm): the pipeline's
    final prebuild stage materialises every derived timeframe as its own stored
    partition, so the intraday movers read tries the STORED *interval*
    partition FIRST (:func:`~brutex.dashboard.movers_stored_read.read_stored_day_frame`
    — month-bounded, the same ``O(one month)`` envelope as the 1-minute read).
    When the stored partition yields rows for *day* they are used directly (no
    resample — ``from_stored=True``); when it is absent/empty for the day, the
    read FALLS BACK to the on-the-fly 1-minute resample
    (:func:`_read_session_bars`, byte-identical behaviour — ``from_stored=False``)
    so a CLI-only or never-prebuilt lake keeps working — the fallback is stated
    openly, never hidden (Golden Rule #10).

    **The equality contract under the unified as-of semantic
    (DEC-HARDENING-LEFTOVERS-001):** the two paths return IDENTICAL bars for the
    same coordinate — the prebuild wrote the stored partition with the SAME
    fixed-arithmetic session-anchored resampler the fallback runs (see
    :mod:`brutex.dashboard.movers_stored_read`, integration-pinned). The stored
    partition holds FULL buckets, so it is consulted only when it can reproduce
    the raw-1m as-of cut exactly (:func:`_stored_serves_cutoff` — whole-day, or
    a bucket-final-aligned cutoff, filtered on the bucket-open minute via the
    COMMON :func:`filter_to_ist_minute`); a MID-bucket as-of always takes the
    1-minute resample fallback, the only source that can aggregate the partial
    forming bucket honestly (never a leak of minutes past the as-of instant).

    **Cross-process derive probe (DEC-ENGINE-MISC-PACK-001):** before touching
    the stored partition the read probes
    :func:`~brutex.data.derive_lock.derive_in_progress` — a non-blocking O(1)
    shared-flock check of the fixed ``<lake_root>/.locks/derive.lock`` the
    materialiser holds exclusively per symbol. While a SEPARATE-process derive
    (a CLI-launched pipeline prebuild) is mid-rewrite of the TF set, the stored
    partition may be half-refreshed, so the read takes the on-the-fly 1-minute
    resample instead — the byte-equality REFERENCE path, honestly counted under
    the existing ``resampled_reads`` provenance (no new payload field). The
    in-process pipeline case is already gated by the process-local
    ``PrebuildGate``; this closes the cross-PROCESS half — the probe is a
    point-in-time signal, refreshed on every poll.
    """
    if (
        not prefer_resample
        and _stored_serves_cutoff(minutes, cutoff_min)
        and not derive_in_progress(lake_root)
    ):
        stored = read_stored_day_frame(
            lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            day=day,
        )
        if stored is not None:
            # A bucket-final-aligned cutoff keeps exactly the fully-elapsed
            # buckets when cut on the bucket-OPEN minute (open ≤ cutoff ⇒ the
            # bucket's last minute ≤ cutoff, by alignment).
            res = (
                stored
                if cutoff_min is None
                else filter_to_ist_minute(stored, cutoff_min, ts_col="ts_utc")
            )
            return [_row_to_bar(row) for row in res.iter_rows(named=True)], True
    return (
        _read_session_bars(
            lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            day=day,
            minutes=minutes,
            cutoff_min=cutoff_min,
        ),
        False,
    )


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

    from brutex.data.lake_read import dedup_bars_frame

    base = lake_root / "bars" / exchange / segment / symbol / "1day"
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return None
    # Keep-FIRST read dedup (DEC-LAKE-READ-UNIQUE-001): a poisoned-era duplicated
    # 1-day partition would otherwise double-serve a day — warned, never silent.
    frame, _dropped = dedup_bars_frame(
        pl.read_parquet(base.as_posix() + "/**/*.parquet"),
        source=f"movers_daily:{exchange}/{segment}/{symbol}",
    )
    return frame


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


#: Max calendar days to walk back searching for the prior trading day's last
#: 1-minute bar when the derived ``1day`` partition is absent (the F&O / stale-lake
#: fallback in :func:`_prior_session_last_from_minute`). ~14 covers the longest NSE
#: holiday cluster (Diwali + an adjacent weekend); past it we honestly return
#: ``None`` rather than scan unbounded history (the hostile-review bound — the
#: back-walk touches at most the day's month parquet + the prior month's, O(1) in
#: partition depth, never an open ``rglob`` over the whole tree).
_PREV_SESSION_MAX_BACK_DAYS: Final[int] = 14


def _prior_session_last_from_minute(
    lake_root: Path, *, exchange: str, segment: str, symbol: str, day: date_cls
) -> tuple[Decimal, int | None] | None:
    """Most recent prior trading day's last 1-minute ``(close, open_interest)``, bounded.

    The COMMON fallback for the prior-day baseline when the derived ``1day``
    partition is missing (a stale lake whose F&O 1-day derive never ran, or any
    contract with 1-minute but no 1-day). Walks back day-by-day from ``day − 1``
    up to :data:`_PREV_SESSION_MAX_BACK_DAYS` calendar days, returning the last
    1-minute bar's ``(close, open_interest)`` of the FIRST prior day that has data
    (weekends / holidays / gaps are skipped). ``None`` when nothing is found within
    the bound (the first day in the lake, or no 1-minute partition) — the caller
    then reports an honest ``None`` baseline, never a fabricated one (Golden Rule #1).

    For a session-filtered symbol (every admitted instrument — index / cash /
    F&O contract), the derived ``1day`` close IS the session's last 1-minute close,
    2dp-snapped by the same writer, so the recovered close is byte-equal to the
    ``1day`` value it substitutes — keeping the spot %-change unchanged where
    ``1day`` exists and making F&O %-change work where it does not. (A
    session-EXEMPT symbol like GIFT Nifty crosses the 09:15-IST bucket boundary so
    its derived-daily "last bar" can differ from the IST-calendar-day last bar — it
    is not an admitted instrument and the fallback only fires when ``1day`` is
    absent, so this is inert today.) The OI is read from the same last 1-minute bar
    (F&O 1-minute bars carry ``open_interest``; ``None`` when absent).
    """
    import polars as pl

    from brutex.data.lake_day_glob import read_day_intraday_frame

    base = lake_root / "bars" / exchange / segment / symbol / "1minute"
    if not base.is_dir():
        return None
    for back in range(1, _PREV_SESSION_MAX_BACK_DAYS + 1):
        candidate = day - timedelta(days=back)
        frame = read_day_intraday_frame(base, candidate, symbol=symbol)
        if frame is None:
            continue
        ist = pl.col("timestamp").dt.convert_time_zone(_IST)
        day_df = frame.filter(ist.dt.date() == candidate).sort("timestamp")
        if day_df.is_empty():
            continue
        last = day_df.tail(1)
        close = Decimal(str(last.get_column("close")[0]))
        raw_oi = last.get_column("open_interest")[0] if "open_interest" in day_df.columns else None
        return close, (None if raw_oi is None else int(raw_oi))
    return None


def _prev_day_close(
    lake_root: Path, *, exchange: str, segment: str, symbol: str, day: date_cls
) -> Decimal | None:
    """Return the previous trading day's close before *day*, or ``None``.

    Reads the symbol's stored ``1day`` partitions FIRST and returns the close of
    the last bar whose IST trading date is strictly before *day* (the primary path
    — byte-unchanged for every spot symbol that has a derived ``1day``). When the
    ``1day`` partition is absent or has no prior bar, falls back to the bounded
    1-minute recovery (:func:`_prior_session_last_from_minute`) so the baseline —
    and therefore the % change — still resolves for F&O contracts / stale lakes
    with 1-minute but no 1-day. ``None`` only when neither source yields a prior
    session (the first day in the lake) — an honest ``None`` % change, never faked.
    """
    import polars as pl

    df = _read_daily_partition(lake_root, exchange=exchange, segment=segment, symbol=symbol)
    if df is not None:
        prior = df.filter(pl.col("timestamp").dt.convert_time_zone(_IST).dt.date() < day).sort(
            "timestamp"
        )
        if not prior.is_empty():
            return Decimal(str(prior.tail(1).get_column("close")[0]))
    recovered = _prior_session_last_from_minute(
        lake_root, exchange=exchange, segment=segment, symbol=symbol, day=day
    )
    return None if recovered is None else recovered[0]


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
    stored_reads, resampled_reads
        Read-path provenance (DEC-PIPELINE-AUTOPREBUILD-001 chunk 2 — honesty
        visible in the payload): of the instruments that CONTRIBUTED a snapshot,
        how many were served from a STORED interval partition (the pipeline
        prebuild's output; the daily path always reads the stored ``1day``, so
        it counts here too) vs the on-the-fly 1-minute RESAMPLE fallback (a
        never-prebuilt lake). A skipped instrument counts in neither.
    """

    snapshots: tuple[MoverSnapshot, ...]
    skipped: int
    date: str
    stored_reads: int = 0
    resampled_reads: int = 0


def resolve_candle_datasets(
    lake_root: Path, datasets: list[dict[str, str]] | None
) -> list[dict[str, str]]:
    """Return *datasets* if the caller pre-discovered them, else scan the lake now.

    The O(1)-latency seam (DEC-MOVERS-DISCOVERY-MEMO-001): the ``/movers`` route
    memoises ``list_candle_datasets`` ONCE per write-generation (the proven ``/db``
    #896 pattern) and threads the cached list into spot / futures / options compute,
    so a tab / section / exchange / expiry switch within one generation REUSES that
    single ``bars/`` walk instead of re-walking the whole lake per compute. ``None``
    ⇒ scan now (back-compat for the post-pull warmer + any direct caller). The scan
    is the SAME deterministic, read-only dir-walk either way (Golden Rule #7).
    """
    if datasets is not None:
        return datasets
    from brutex.dashboard.db_view import list_candle_datasets

    return list_candle_datasets(lake_root)


def compute_spot_candidates(
    *,
    lake_root: Path,
    interval: str,
    date: str | None,
    time: str | None,
    exchange: str | None = None,
    names: Mapping[str, str] | None = None,
    datasets: list[dict[str, str]] | None = None,
    prefer_resample: bool = False,
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
      partitions, then read each symbol's day STORED-FIRST
      (:func:`_read_intraday_bars`, DEC-PIPELINE-AUTOPREBUILD-001 chunk 2): the
      pipeline prebuild's materialised *interval* partition when it has the day
      (no resample — identical bars, the same resampler wrote them), else the
      on-the-fly 1-minute resample fallback (:func:`_read_session_bars` — a
      CLI-only / never-prebuilt lake keeps working). The per-path counts are
      surfaced as :attr:`SpotCandidates.stored_reads` /
      :attr:`SpotCandidates.resampled_reads` (honesty in the payload).

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
    prefer_resample
        ``True`` makes every INTRADAY read take the always-fresh 1-minute
        resample instead of the stored-first path
        (DEC-MOVERS-GATE-PREBUILD-SCOPE-001 — the ``/movers`` route passes it
        while a dashboard pull runs, because mid-fetch the stored TF partitions
        predate the freshly-landing 1-minute bars; the DAILY path still reads
        the derived ``1day`` partitions directly — 1-minute is the only fetched
        granularity and ``1day`` is rewritten only by the prebuild, so its
        mid-pull staleness is bounded to the pull window and stated, not
        resampled away). Default ``False`` is byte-identical.

    Returns
    -------
    SpotCandidates
        The per-instrument snapshots + skipped count + normalised date.

    Raises
    ------
    ValueError
        On a malformed *date* / *time* (the route maps it to a 400).
    """
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
    # SPOT tab = SPOT ONLY: whitelist the cash/index segments (INDEX/CASH, the
    # COMMON `SPOT_PROBE_SEGMENTS`) so F&O contract partitions (`segment="FNO"` —
    # futures + options) NEVER leak into the Spot screen. Without this the
    # unfiltered `list_candle_datasets` (which returns every segment) mixed
    # `NSE-NIFTY-30Jun26-FUT` into Spot the moment an F&O partition existed for the
    # discovery interval (visible on 1-minute; latent on 1-day until the F&O 1-day
    # derive runs). Each tab now reads strictly its own instrument class
    # (Spot→INDEX/CASH, Futures→FNO/-FUT, Options→FNO/CE-PE).
    spot_datasets = [
        d
        for d in resolve_candle_datasets(lake_root, datasets)
        if d["interval"] == discovery_interval
        and d["segment"] in SPOT_PROBE_SEGMENTS
        and (exchange is None or d["exchange"] == exchange)
    ]
    if not spot_datasets:
        return SpotCandidates(snapshots=(), skipped=0, date=date_str)

    snapshots: list[MoverSnapshot] = []
    skipped = 0
    stored_reads = 0
    resampled_reads = 0
    skip_errors = movers_read_skip_errors()
    for dataset in spot_datasets:
        try:
            if is_daily:
                # The daily path reads the stored (derived) `1day` partition by
                # construction — it counts as a stored read below.
                from_stored = True
                bars = _read_daily_bars(
                    lake_root,
                    exchange=dataset["exchange"],
                    segment=dataset["segment"],
                    symbol=dataset["symbol"],
                    day=day,
                )
            else:
                bars, from_stored = _read_intraday_bars(
                    lake_root,
                    exchange=dataset["exchange"],
                    segment=dataset["segment"],
                    symbol=dataset["symbol"],
                    interval=interval,
                    day=day,
                    minutes=minutes,
                    cutoff_min=cutoff_min,
                    prefer_resample=prefer_resample,
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
            # DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001: source the "Top Value" turnover
            # from the RAW 1-minute frame (post as-of cut, pre-resample) so it is a
            # session property identical across every display TF, mirroring
            # futures/options. ONE uniform call for the daily AND intraday branches
            # (`cutoff_min` is already None for daily/whole-day at :1073). Inside the
            # same `try` so a corrupt 1-minute parquet is caught by `skip_errors`
            # below; `None` (no 1-minute) → compute_snapshot falls back to the
            # display-bar value (no-worse). Read only when the symbol had bars.
            value_paisa = (
                spot_session_value_paisa(
                    lake_root,
                    exchange=dataset["exchange"],
                    segment=dataset["segment"],
                    symbol=dataset["symbol"],
                    day=day,
                    cutoff_min=cutoff_min,
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
        snap = compute_snapshot(instrument, bars, prev_close=prev_close, value_paisa=value_paisa)
        if snap is None:  # no bars on the selected day → reported skipped, never faked.
            skipped += 1
            continue
        # Read-path provenance (DEC-PIPELINE-AUTOPREBUILD-001 chunk 2): count
        # ONLY the instruments that contributed a snapshot, by which path
        # served their bars (stored partition vs 1-minute resample fallback).
        if from_stored:
            stored_reads += 1
        else:
            resampled_reads += 1
        snapshots.append(snap)

    return SpotCandidates(
        snapshots=tuple(snapshots),
        skipped=skipped,
        date=date_str,
        stored_reads=stored_reads,
        resampled_reads=resampled_reads,
    )


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
        stored_reads=candidates.stored_reads,
        resampled_reads=candidates.resampled_reads,
    )
