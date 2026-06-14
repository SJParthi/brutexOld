"""Fold captured live ticks into 1-minute :class:`~brutex.data.models.CandleBar`.

The single question this tool answers is "**is our live feed's 1-minute bar the
same as the historical 1-minute bar we backtest on?**". So the builder mirrors the
backtest candle exactly:

* **window** — only the regular cash session, **09:15:00 → 15:29:00 IST**: the
  375 one-minute bars whose *open* minute is in ``[09:15, 15:29]`` (operator spec
  "9.15 am till 3.29 pm alone"). Pre-open (09:00–09:15) and the closing instant
  (15:30) are excluded, matching :data:`brutex.calendars.nse.SESSION_DURATION_MINUTES`.
* **label** — the bar timestamp is the minute's start in **UTC** (closed=left,
  label=left), identical to the lake's historical candle label, so live and
  backtest bars align by ``==`` on the timestamp.
* **OHLC** — open = earliest tick value in the minute, high = max, low = min,
  close = latest tick value; **volume = 0** and **open_interest = None** (indices
  do not trade — they mirror the IDX historical bar, per
  ``DEC-VENDOR-VOLUME-NORMALIZE-001``).

Pure function over already-captured ticks → fully unit-testable, no feed/network.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, time
from decimal import Decimal

from brutex.calendars.nse import IST, SESSION_OPEN_IST
from brutex.data.models import CandleBar
from brutex.live.tick import LiveTick

#: The open-minute of the LAST 1-minute candle of the session: 15:29 IST. The
#: 15:29 bar covers 15:29:00–15:29:59; 15:30:00 is the close instant (no bar).
LAST_CANDLE_OPEN_IST: time = time(15, 29)


def _ist_minute_floor(recv_ts_utc: datetime) -> datetime:
    """Floor a UTC tick time to its IST minute start, returned in IST.

    IST is a whole +05:30 offset (0 seconds), so the IST minute boundary is the
    same instant as the UTC minute boundary — we floor in IST purely so the
    session-window comparison reads in wall-clock terms.
    """
    ist = recv_ts_utc.astimezone(IST)
    return ist.replace(second=0, microsecond=0)


def _in_session_window(ist_minute: datetime) -> bool:
    """True if *ist_minute* (an IST minute start) is a session bar open."""
    wall = ist_minute.time()
    return SESSION_OPEN_IST <= wall <= LAST_CANDLE_OPEN_IST


def build_minute_candles(
    ticks: Iterable[LiveTick], *, session_date: object | None = None
) -> dict[str, list[CandleBar]]:
    """Aggregate *ticks* into per-instrument 1-minute candles (session window only).

    Parameters
    ----------
    ticks
        Captured :class:`~brutex.live.tick.LiveTick` values, any order (they are
        bucketed and sorted internally by receive time).
    session_date
        Optional IST :class:`datetime.date`. When given, only ticks whose IST
        minute falls on that date contribute — handy for isolating one trading
        day from a multi-day log.

    Returns
    -------
    dict[str, list[CandleBar]]
        Instrument key → ascending-by-timestamp list of 1-minute candles. Only
        minutes that actually received a tick produce a bar (gaps are not
        synthesised — a missing live minute is itself a parity signal).
    """
    # instrument -> minute_label_utc -> list[(recv_ts, value)]
    buckets: dict[str, dict[datetime, list[tuple[datetime, Decimal]]]] = {}
    for tick in ticks:
        ist_minute = _ist_minute_floor(tick.recv_ts)
        if not _in_session_window(ist_minute):
            continue
        if session_date is not None and ist_minute.date() != session_date:
            continue
        label_utc = ist_minute.astimezone(tick.recv_ts.tzinfo)
        per_instrument = buckets.setdefault(tick.instrument_key, {})
        per_instrument.setdefault(label_utc, []).append((tick.recv_ts, tick.value))

    out: dict[str, list[CandleBar]] = {}
    for instrument_key, minute_map in buckets.items():
        bars: list[CandleBar] = []
        for label_utc in sorted(minute_map):
            observations = sorted(minute_map[label_utc], key=lambda item: item[0])
            values = [value for _ts, value in observations]
            bars.append(
                CandleBar(
                    timestamp=label_utc,
                    open=values[0],
                    high=max(values),
                    low=min(values),
                    close=values[-1],
                    volume=0,
                    open_interest=None,
                )
            )
        out[instrument_key] = bars
    return out


__all__ = ["LAST_CANDLE_OPEN_IST", "build_minute_candles"]
