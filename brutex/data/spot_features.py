"""Spot derived %-change features — previous-close % + 09:15 open-gap %.

Track-1 spot-only features (DEC-SPOT-PCT-CHANGE-001 #346): computed off the
1m + 1d lake for spot underlyings (3 brute-force indexes + INDIA VIX
reference + stock spot) — **never options**.

* **previous-close % change** — ``(value − prev_day_close) / prev_day_close
  × 100`` (a running per-bar feature: pass ``bar.close``).
* **open-gap %** — the same formula applied to the **09:15:00 IST session
  open** bar's ``open`` price (the pre-market/opening gap), computed once
  per day.

Full precision — these are **statistical metrics, never money-rounded**
(CLAUDE.md §4): the result keeps Decimal precision. Division by a zero/None
previous close yields ``None`` (no spurious gap).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import time
from decimal import Decimal
from typing import Final

from brutex.data.models import CandleBar
from brutex.data.session import IST, SESSION_OPEN

#: Session open instant (IST) — the bar whose open is the 09:15 gap anchor.
#: DERIVED from the ONE COMMON :data:`brutex.data.session.SESSION_OPEN` root
#: (DEC-TIMESTAMP-IST-SINGLESOURCE-001 — the former ``time(9, 15)`` mirror).
_SESSION_OPEN: Final[time] = SESSION_OPEN

_HUNDRED: Final[Decimal] = Decimal(100)


def pct_change(value: Decimal, base: Decimal) -> Decimal | None:
    """Return ``(value − base) / base × 100`` at full precision, or ``None`` if base is 0."""
    if base == 0:
        return None
    return (value - base) / base * _HUNDRED


def session_open_bar(bars: Sequence[CandleBar]) -> CandleBar | None:
    """Return the 09:15:00 IST session-open bar from *bars*, or ``None`` if absent."""
    for bar in bars:
        if bar.timestamp.astimezone(IST).time() == _SESSION_OPEN:
            return bar
    return None


def open_gap_pct(bars: Sequence[CandleBar], prev_day_close: Decimal) -> Decimal | None:
    """Return the 09:15 open-gap % vs *prev_day_close*, or ``None`` if no 09:15 bar."""
    open_bar = session_open_bar(bars)
    if open_bar is None:
        return None
    return pct_change(open_bar.open, prev_day_close)


def open_pct_series(bars: Sequence[CandleBar]) -> list[Decimal | None]:
    """Return each bar's **open_pct** — the extra per-candle column.

    ``open_pct = (bar.close − today's 09:15 open) / today's 09:15 open × 100``
    — the intraday twin of :func:`open_gap_pct` (DEC-DHAN-OPEN-PCT-COLUMN-001):
    where the gap uses the *previous* day's close as the base, this uses the
    **same** day's 09:15:00 IST session open. The result is a list aligned 1:1
    with *bars*. When the day's 09:15 bar is absent there is no anchor, so every
    entry is ``None``; an individual entry is ``None`` only if the 09:15 open is 0.

    Full precision — a statistical metric, **never money-rounded** (CLAUDE.md
    §4). *bars* must be a SINGLE trading day's series (the caller groups by day),
    so the 09:15 anchor is unambiguous.
    """
    open_bar = session_open_bar(bars)
    if open_bar is None:
        return [None] * len(bars)
    return [pct_change(bar.close, open_bar.open) for bar in bars]


__all__ = [
    "open_gap_pct",
    "open_pct_series",
    "pct_change",
    "session_open_bar",
]
