"""Point-in-time "movers" snapshot core — the Dhan-style `/movers` view (pure).

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
  matches the Dhan screens the operator referenced):
  ``change = ltp − prev_day_close`` and
  ``pct_change = change / prev_day_close × 100`` — where ``ltp`` is the close of
  the **selected** bar (date · timeframe · time) and ``prev_day_close`` is the
  prior trading day's 1-day close. ``None`` when there is no previous day (the
  first day in the lake) — reported honestly, never faked.
* **Volume is the FRESH current session only** — NO previous day. It is the
  **cumulative** traded quantity from 09:15 up to the selected bar
  (``Σ bar.volume``). Volume resets to 0 each morning, so there is no
  "previous-close" baseline for it (the operator's exact ask).
* **Value (₹)** is the cumulative traded value over those same bars
  (``Σ round_money(close) × volume``), kept as int64 paisa via
  :mod:`brutex.money` and rendered in ₹ crore for the table.

## Honesty (Golden Rule #1 / #10)

REFERENCE_ONLY over the **ingested lake** (CLAUDE.md §3) — the numbers are
exactly as-of the date+timeframe+time the operator picks, **from the data they
pulled**, NOT a live feed (Track-1 has no live feed — CLAUDE.md §1). Prices are
2dp money (``DEC-MONEY-2DP-001``); the % / ratio metrics keep full precision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from brutex.data.models import CandleBar
from brutex.data.spot_features import pct_change
from brutex.data.timeframes import LAKE_INTERVAL_LABELS
from brutex.money import format_inr, round_money, rupees_to_paisa

__all__ = [
    "MoverSnapshot",
    "ScreenInstrument",
    "build_snapshot_payload",
    "compute_snapshot",
    "interval_minutes",
    "session_bar_times",
]

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
        Cumulative traded value over those bars as int64 paisa
        (``Σ round_money(close) × volume``).
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
    volume = 0
    value_rupees = Decimal(0)
    for bar in bars:
        volume += bar.volume
        value_rupees += round_money(bar.close) * bar.volume
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
        value_paisa=rupees_to_paisa(value_rupees),
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


def _row(snap: MoverSnapshot) -> dict[str, Any]:
    """Project one snapshot to a Dhan-style JSON row (money 2dp, metrics full-precision)."""
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
    """Assemble the Dhan-style ``/api/v1/movers`` JSON from computed snapshots.

    Builds the three ranked tables (gainers / losers / volume) the redesigned
    page renders, plus the echo of the chosen ``date`` / ``interval`` / ``time``
    and an honest ``note``. Every value is JSON-native (money is a ``₹`` string,
    statistical metrics are full-precision strings, value is a ₹-crore string) so
    Starlette's ``JSONResponse`` never chokes on a ``Decimal``.
    """
    gainers = [_row(s) for s in _gainers(snapshots, top_n)]
    losers = [_row(s) for s in _losers(snapshots, top_n)]
    volume = [_row(s) for s in _by_volume(snapshots, top_n)]
    instrument_count = len(snapshots) + skipped
    if instrument_count == 0:
        note = (
            "No spot data in the lake yet — pull spot first on the Coverage page, "
            "then refresh. (Reference-only lake view, CLAUDE.md §3.)"
        )
    elif not gainers and not losers and not volume:
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
        "gainers": gainers,
        "losers": losers,
        "volume_movers": volume,
        "note": note,
    }
