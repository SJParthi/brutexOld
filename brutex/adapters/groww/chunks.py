"""Groww chunk planner — tile a date range into Groww-window-sized windows.

Groww caps the date range per backtesting call by **interval tier**
(``docs/groww/api-reference/03-backtesting.md`` "Backtesting Data Limits"):

* 1/2/3/5-minute  → **30 days** per call
* 10/15/30-minute → **90 days** per call
* 1h / 4h / 1d / 1w / 1mo → **180 days** per call

This is richer than Dhan's single 90/30 split, so the only Groww-specific logic
here is :func:`window_days_for_interval` (interval → window width). The actual
tiling reuses the **vendor-neutral, symbol-free**
:func:`brutex.adapters.dhan.chunks.plan_windows` (CLAUDE.md §11 COMMON — no
copy-paste; the jscpd gate enforces this). PR-F extracts that shared tiler to
``brutex/adapters/_common/`` when Dhan is deleted; until then importing it is
the minimum-new-code, duplication-free choice.
"""

from __future__ import annotations

from datetime import datetime

from brutex.adapters.dhan.chunks import DateWindow, plan_windows
from brutex.adapters.groww.constants import (
    CHUNK_WINDOW_30D_INTERVALS,
    CHUNK_WINDOW_90D_INTERVALS,
    CHUNK_WINDOW_180D_INTERVALS,
    WINDOW_DAYS_30,
    WINDOW_DAYS_90,
    WINDOW_DAYS_180,
)


def window_days_for_interval(interval: str) -> int:
    """Resolve a BruteX candle interval to its Groww max-window width (days).

    Parameters
    ----------
    interval
        A BruteX canonical interval (e.g. ``"1minute"``, ``"15minute"``,
        ``"1day"``).

    Returns
    -------
    int
        30, 90, or 180 per the Groww backtesting tier.

    Raises
    ------
    ValueError
        If *interval* is not a known Groww backtesting interval (fail-loud —
        Golden Rule #1, never silently default a window width).
    """
    if interval in CHUNK_WINDOW_30D_INTERVALS:
        return WINDOW_DAYS_30
    if interval in CHUNK_WINDOW_90D_INTERVALS:
        return WINDOW_DAYS_90
    if interval in CHUNK_WINDOW_180D_INTERVALS:
        return WINDOW_DAYS_180
    raise ValueError(f"unknown Groww backtesting interval: {interval!r}")


def plan_candle_windows(start: datetime, end: datetime, *, interval: str) -> list[DateWindow]:
    """Tile ``[start, end)`` into windows sized for *interval*'s Groww tier.

    A ``1minute`` pull tiles at 30 days; a ``1day`` pull at 180 days. Returns
    contiguous, non-overlapping :class:`DateWindow` slices (the final window
    clipped to *end*); an empty list when ``start >= end``.
    """
    return plan_windows(start, end, window_days=window_days_for_interval(interval))


__all__ = [
    "DateWindow",
    "plan_candle_windows",
    "plan_windows",
    "window_days_for_interval",
]
