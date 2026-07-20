"""Dhan chunk planner — tile a date range into Dhan-window-sized windows.

Dhan caps ``POST /charts/intraday`` at a FLAT **90 days per call** for any interval
(PARTIAL: official-doc snippet), simpler than Groww's interval-tiered 30/90/180
split. The only Dhan-specific logic here is :func:`window_days_for_interval`
(interval → 90); the actual tiling reuses the vendor-neutral, symbol-free
:func:`brutex.adapters.ingest_common.plan_windows` (CLAUDE.md §11 COMMON — no
copy-paste). The NATIVE-daily track (DEC-DHAN-NATIVE-DAILY-001) additionally maps
``1day`` → a DEFENSIVE 365-day tile (the daily endpoint's per-request range cap is
UNDOCUMENTED/UNVERIFIED). Any other interval fails loud (Golden Rule #1 — never
silently default a window width).
"""

from __future__ import annotations

from datetime import datetime

from brutex.adapters.dhan.constants import (
    DHAN_DAILY_INTERVAL,
    DHAN_DAILY_WINDOW_DAYS,
    DHAN_FETCH_INTERVAL,
    DHAN_INTRADAY_WINDOW_DAYS,
)
from brutex.adapters.ingest_common import DateWindow, plan_windows


def window_days_for_interval(interval: str) -> int:
    """Resolve a BruteX candle interval to its Dhan max-window width (days).

    Parameters
    ----------
    interval
        A BruteX canonical interval — ``"1minute"`` (intraday) or ``"1day"``
        (the DEC-DHAN-NATIVE-DAILY-001 native daily endpoint).

    Returns
    -------
    int
        90 (:data:`~brutex.adapters.dhan.constants.DHAN_INTRADAY_WINDOW_DAYS`) for
        ``1minute`` — the flat documented per-request cap; 365
        (:data:`~brutex.adapters.dhan.constants.DHAN_DAILY_WINDOW_DAYS`) for
        ``1day`` — a DEFENSIVE tile, the daily endpoint's per-request range cap is
        UNDOCUMENTED/UNVERIFIED (see the constant's docstring).

    Raises
    ------
    ValueError
        If *interval* is not a supported Dhan interval (fail-loud — Golden
        Rule #1, never silently default a window width).
    """
    if interval == DHAN_FETCH_INTERVAL:
        return DHAN_INTRADAY_WINDOW_DAYS
    if interval == DHAN_DAILY_INTERVAL:
        return DHAN_DAILY_WINDOW_DAYS
    raise ValueError(
        f"unsupported Dhan interval: {interval!r}"
        f" (only {DHAN_FETCH_INTERVAL!r} / {DHAN_DAILY_INTERVAL!r})"
    )


def plan_candle_windows(start: datetime, end: datetime, *, interval: str) -> list[DateWindow]:
    """Tile ``[start, end)`` into *interval*-sized windows (90-day intraday / 365-day daily).

    Returns contiguous, non-overlapping :class:`DateWindow` slices (the final window
    clipped to *end*); an empty list when ``start >= end``.
    """
    return plan_windows(start, end, window_days=window_days_for_interval(interval))


__all__ = [
    "DateWindow",
    "plan_candle_windows",
    "plan_windows",
    "window_days_for_interval",
]
