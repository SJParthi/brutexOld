"""Dhan chunk planner — tile a date range into Dhan-window-sized windows.

Dhan caps the date range per Data-API call (``docs/dhan-ref/05`` + ``06``):

* spot intraday (``/charts/intraday``) — **90 days** per call
* expired options (``/charts/rollingoption``) — **30 days** per call
* daily (``/charts/historical``) — to inception (no intraday cap)

:func:`plan_windows` tiles ``[start, end)`` into contiguous,
non-overlapping :class:`DateWindow` slices each no wider than the window
limit. It is **vendor-neutral and symbol-free** — the orchestrator
attaches the ``securityId`` / instrument when it dispatches each window,
so this planner has zero coupling to the watermark schema (that wiring
is the later atomic Groww→Dhan flip, DEC-GROWW-DHAN-REVERT-001).

This is the **non-calendar** planner: it does NOT skip non-trading days.
A window with no trades returns empty and the watermark settles it
``NO_DATA`` (never re-requested), so proactive calendar-skipping is
unnecessary. The tiling is O(n) in the number of windows (a few dozen
for a multi-year 1-minute pull) and allocation-free per step beyond the
emitted window (DEC-EXTREME-PERF-AUTOMATION-001 #342).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from brutex.adapters.dhan.constants import (
    INTRADAY_WINDOW_DAYS,
    ROLLING_OPTION_WINDOW_DAYS,
)


@dataclass(frozen=True, slots=True)
class DateWindow:
    """A half-open ``[start, end)`` fetch window, ISO-8601 string bounds."""

    start: str
    end: str


def plan_windows(start: datetime, end: datetime, *, window_days: int) -> list[DateWindow]:
    """Tile ``[start, end)`` into windows no wider than *window_days*.

    Parameters
    ----------
    start, end
        Range to cover. If ``start >= end`` the result is empty.
    window_days
        Maximum width (days) of each emitted window. Must be positive.

    Returns
    -------
    list[DateWindow]
        Contiguous, non-overlapping windows (ISO-string bounds); the final
        window is clipped to *end*.

    Raises
    ------
    ValueError
        If *window_days* is not positive.
    """
    if window_days <= 0:
        raise ValueError(f"window_days must be positive, got {window_days}")
    windows: list[DateWindow] = []
    cursor = start
    span = timedelta(days=window_days)
    while cursor < end:
        window_end = min(cursor + span, end)
        windows.append(DateWindow(start=cursor.isoformat(), end=window_end.isoformat()))
        cursor = window_end
    return windows


def plan_spot_intraday_windows(start: datetime, end: datetime) -> list[DateWindow]:
    """Tile ``[start, end)`` for the 1-minute spot pull (90-day Dhan cap)."""
    return plan_windows(start, end, window_days=INTRADAY_WINDOW_DAYS)


def plan_option_windows(start: datetime, end: datetime) -> list[DateWindow]:
    """Tile ``[start, end)`` for the 1-minute rolling-option pull (30-day cap)."""
    return plan_windows(start, end, window_days=ROLLING_OPTION_WINDOW_DAYS)


__all__ = [
    "DateWindow",
    "plan_option_windows",
    "plan_spot_intraday_windows",
    "plan_windows",
]
