"""Rolling-ATM option chunk planner — tile a date range into 30-day windows.

DEC-DHAN-ROLLING-OPTIONS-001 (ADR-0022). Dhan's expired-options / rolling endpoint
caps ``POST /v2/charts/rollingoption`` at **30 days per call** — DISTINCT from the
spot ``/charts/intraday`` 90-day cap (:mod:`.chunks`). The 30-day cap is UNVERIFIED
(host-blocked), so the CONSERVATIVE 30 is chosen: over-chunking is harmless (the
keep-first, timestamp-keyed writer dedups any window overlap), under-chunking would
silently truncate a window (Golden Rule #1). The only rolling-specific logic here is
:func:`window_days_for_rolling_interval` (interval → 30); the actual tiling reuses the
vendor-neutral, symbol-free :func:`brutex.adapters.ingest_common.plan_windows`
(CLAUDE.md §11 COMMON — no copy-paste). The rolling-ATM track fetches ``1minute``
ONLY, so only that interval is known here; an unknown interval fails loud.
"""

from __future__ import annotations

from datetime import datetime

from brutex.adapters.dhan.rolling_option_constants import (
    DHAN_ROLLING_FETCH_INTERVAL,
    DHAN_ROLLING_WINDOW_DAYS,
)
from brutex.adapters.ingest_common import DateWindow, plan_windows


def window_days_for_rolling_interval(interval: str) -> int:
    """Resolve a BruteX candle interval to its rolling-option max-window width (days).

    Parameters
    ----------
    interval
        A BruteX canonical interval — for the rolling-ATM track, ``"1minute"``.

    Returns
    -------
    int
        30 (:data:`~brutex.adapters.dhan.rolling_option_constants.DHAN_ROLLING_WINDOW_DAYS`)
        — the conservative per-request cap.

    Raises
    ------
    ValueError
        If *interval* is not the supported rolling interval (fail-loud — Golden
        Rule #1, never silently default a window width).
    """
    if interval == DHAN_ROLLING_FETCH_INTERVAL:
        return DHAN_ROLLING_WINDOW_DAYS
    raise ValueError(
        f"unsupported Dhan rolling-option interval: {interval!r} "
        f"(only {DHAN_ROLLING_FETCH_INTERVAL!r})"
    )


def plan_rolling_option_windows(
    start: datetime, end: datetime, *, interval: str
) -> list[DateWindow]:
    """Tile ``[start, end)`` into 30-day windows for *interval*.

    Returns contiguous, non-overlapping :class:`DateWindow` slices (the final
    window clipped to *end*); an empty list when ``start >= end``.
    """
    return plan_windows(start, end, window_days=window_days_for_rolling_interval(interval))


__all__ = [
    "DateWindow",
    "plan_rolling_option_windows",
    "plan_windows",
    "window_days_for_rolling_interval",
]
