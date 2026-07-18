"""Dhan chunk planner — tile a date range into Dhan-window-sized windows (pure law).

DEC-DHAN-ADAPTER-001. Dhan caps the date range per Data-API call by endpoint:

* intraday (``/v2/charts/intraday``)      → **90 days** per call (VERIFIED)
* rolling-option (``/v2/charts/rollingoption``) → **30 days** per call (VERIFIED)
* daily (``/v2/charts/historical``)       → **365 days** per call (**UNVERIFIED** —
  the daily endpoint serves "from inception" but documents no per-call cap; a
  conservative tiling is used pending the live probe — over-tiling is always safe)

The only Dhan-specific logic here is :func:`window_days_for_interval` (interval →
window width). The tiling itself reuses the **vendor-neutral, symbol-free**
:func:`brutex.adapters.ingest_common.plan_windows` (CLAUDE.md §11 COMMON — no
copy-paste). A pure, I/O-free LEAF (imports only :mod:`ingest_common` + Dhan
constants) so a future ``dhan_chunks_rust.py`` facade could wrap it 1:1
(DEC-DHAN-RUST-FACADE-001).
"""

from __future__ import annotations

from datetime import datetime

from brutex.adapters.dhan.constants import (
    DHAN_DAILY_INTERVAL,
    DHAN_WINDOW_DAYS_DAILY_UNVERIFIED,
    DHAN_WINDOW_DAYS_INTRADAY,
    INTRADAY_INTERVAL_TO_DHAN,
)
from brutex.adapters.ingest_common import DateWindow, plan_windows


def window_days_for_interval(interval: str) -> int:
    """Resolve a BruteX candle interval to its Dhan max-window width (days).

    Parameters
    ----------
    interval
        A BruteX canonical interval (e.g. ``"1minute"``, ``"1day"``).

    Returns
    -------
    int
        90 for an intraday interval; 365 (UNVERIFIED, conservative) for the daily
        interval.

    Raises
    ------
    ValueError
        If *interval* is neither a Dhan intraday interval nor the daily interval
        (fail-loud — Golden Rule #1, never silently default a window width).
    """
    if interval in INTRADAY_INTERVAL_TO_DHAN:
        return DHAN_WINDOW_DAYS_INTRADAY
    if interval == DHAN_DAILY_INTERVAL:
        return DHAN_WINDOW_DAYS_DAILY_UNVERIFIED
    raise ValueError(f"unknown Dhan candle interval: {interval!r}")


def plan_candle_windows(start: datetime, end: datetime, *, interval: str) -> list[DateWindow]:
    """Tile ``[start, end)`` into windows sized for *interval*'s Dhan cap.

    An intraday pull tiles at 90 days; a daily pull at 365 days (UNVERIFIED cap).
    Returns contiguous, non-overlapping :class:`DateWindow` slices (the final window
    clipped to *end*); an empty list when ``start >= end``.
    """
    return plan_windows(start, end, window_days=window_days_for_interval(interval))


def plan_rolling_option_windows(start: datetime, end: datetime) -> list[DateWindow]:
    """Tile ``[start, end)`` into 30-day rolling-option (expired) windows (VERIFIED cap)."""
    from brutex.adapters.dhan.constants import DHAN_WINDOW_DAYS_ROLLING_OPTION

    return plan_windows(start, end, window_days=DHAN_WINDOW_DAYS_ROLLING_OPTION)


__all__ = [
    "DateWindow",
    "plan_candle_windows",
    "plan_rolling_option_windows",
    "plan_windows",
    "window_days_for_interval",
]
