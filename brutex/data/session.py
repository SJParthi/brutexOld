"""NSE/BSE trading-session filter — keep only in-session intraday bars.

Track-1 rule (operator 2026-05-31; CLAUDE.md §7): the lake must hold **only**
regular-session data — **never** pre-open (09:00–09:15) or post-close bars.
The session is **09:15:00 IST inclusive → 15:30:00 IST exclusive**.

* **Intraday** bars (1m and anything derived from it) are filtered to that
  window — a bar's UTC timestamp is converted to IST and kept iff its
  time-of-day is in ``[09:15, 15:30)``. The 09:15–15:30 IST session maps to
  the **same UTC calendar date** (03:45–10:00 UTC), so there is no
  day-boundary rollover to worry about.
* **Daily** (1-day) bars carry no meaningful intraday time, so they are
  **not** time-filtered.

Applied at the **pull/ingest boundary** (before the lake write) so bad
bars never enter the lake — and reused by every write path (CLAUDE.md §11
COMMON).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time
from typing import Final
from zoneinfo import ZoneInfo

from brutex.data.models import CandleBar

#: India Standard Time (NSE/BSE wall clock).
IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

#: Regular session open — 09:15:00 IST (inclusive).
SESSION_OPEN: Final[time] = time(9, 15)

#: Regular session close — 15:30:00 IST (exclusive).
SESSION_CLOSE: Final[time] = time(15, 30)


def in_session(timestamp: datetime) -> bool:
    """True iff *timestamp* (UTC, tz-aware) falls in ``[09:15, 15:30)`` IST."""
    ist_time = timestamp.astimezone(IST).time()
    return SESSION_OPEN <= ist_time < SESSION_CLOSE


def filter_intraday_session(bars: Sequence[CandleBar]) -> list[CandleBar]:
    """Return only the *bars* whose IST time-of-day is in ``[09:15, 15:30)``.

    For **intraday** series only — drops pre-open / post-close bars. Daily
    series must NOT be passed here (a daily bar has no intraday time).
    """
    return [bar for bar in bars if in_session(bar.timestamp)]


__all__ = [
    "IST",
    "SESSION_CLOSE",
    "SESSION_OPEN",
    "filter_intraday_session",
    "in_session",
]
