"""Chunk planner — split a fetch range into Groww-window-sized chunks.

Groww's ``get_historical_candles`` caps the date range per call by
interval (``docs/groww/api-reference/03-backtesting.md`` "Backtesting
Data Limits"):

* 1m / 2m / 3m / 5m  → **30 days** per call
* 10m / 15m / 30m    → **90 days** per call
* 1h / 4h / 1d / 1w / 1mo → **180 days** per call

:func:`plan_chunks` tiles ``[start, end)`` into contiguous, non-overlapping
:class:`~brutex.data.watermark.ChunkKey` windows each no wider than the
interval's limit, ready to hand to
:func:`~brutex.adapters.groww.fetch_loop.run_fetch`.

This is the **non-calendar** planner: it does NOT skip non-trading days
(holidays/weekends). That is intentional — a window with no trades comes
back empty and the watermark records it ``NO_DATA`` (settled, never
re-requested), so proactive calendar-skipping is unnecessary.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from brutex.data.watermark import ChunkKey

#: Max window size (days) per candle interval — from Groww backtesting limits.
WINDOW_DAYS: Final[dict[str, int]] = {
    "1m": 30,
    "2m": 30,
    "3m": 30,
    "5m": 30,
    "10m": 90,
    "15m": 90,
    "30m": 90,
    "1h": 180,
    "4h": 180,
    "1d": 180,
    "1w": 180,
    "1mo": 180,
}

#: Groww native short interval → the lake partition's canonical long name
#: (must equal ``brutex/data/partition.py::VALID_INTERVALS``). The fetch layer
#: + watermark work in the short keys; the lake stores under these long names.
#: One source of truth for the short↔long bridge (CLAUDE.md §11 COMMON) — a
#: drift here is caught by ``test_groww_chunks.py`` against ``VALID_INTERVALS``.
GROWW_INTERVAL_TO_PARTITION: Final[dict[str, str]] = {
    "1m": "1minute",
    "2m": "2minute",
    "3m": "3minute",
    "5m": "5minute",
    "10m": "10minute",
    "15m": "15minute",
    "30m": "30minute",
    "1h": "1hour",
    "4h": "4hour",
    "1d": "1day",
    "1w": "1week",
    "1mo": "1month",
}

#: The fetch set — **1-minute ONLY** (DEC-CANONICAL-1M-SOURCE-001, operator
#: 2026-05-31). Groww's native higher timeframes are NOT fetched: only the 1m
#: bar is trusted (broker-tape; the native 2m/5m/15m/… aggregations are
#: untrusted and were unstable across re-pulls), and every other timeframe is
#: derived from 1m by deterministic resampling — never pulled from the vendor.
#: ``WINDOW_DAYS`` / ``GROWW_INTERVAL_TO_PARTITION`` retain the full interval
#: maps (still valid lookups; only ``"1m"`` is iterated for the pull).
ALL_GROWW_INTERVALS: Final[tuple[str, ...]] = ("1m",)


def plan_chunks(
    groww_symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    *,
    window_days: int | None = None,
) -> list[ChunkKey]:
    """Tile ``[start, end)`` into ChunkKeys no wider than the window limit.

    Parameters
    ----------
    groww_symbol
        Instrument symbol, e.g. ``"NSE-NIFTY"``.
    interval
        Candle interval key (must be in :data:`WINDOW_DAYS` unless
        *window_days* is given), e.g. ``"1m"``.
    start, end
        Range to cover. If ``start >= end`` the result is empty.
    window_days
        Override the per-interval window cap (mainly for tests). When
        ``None``, looked up from :data:`WINDOW_DAYS`.

    Returns
    -------
    list[ChunkKey]
        Contiguous, non-overlapping windows (start/end as ISO strings).
        The final window is clipped to *end*.

    Raises
    ------
    ValueError
        If *interval* is unknown and no *window_days* override is given,
        or if *window_days* is not positive.
    """
    if window_days is None:
        try:
            limit = WINDOW_DAYS[interval]
        except KeyError as exc:
            raise ValueError(
                f"unknown interval {interval!r}; known: {sorted(WINDOW_DAYS)} "
                f"(or pass window_days=...)"
            ) from exc
    else:
        limit = window_days
    if limit <= 0:
        raise ValueError(f"window_days must be positive, got {limit}")

    chunks: list[ChunkKey] = []
    cursor = start
    span = timedelta(days=limit)
    while cursor < end:
        chunk_end = min(cursor + span, end)
        chunks.append(
            ChunkKey(
                groww_symbol=groww_symbol,
                interval=interval,
                start=cursor.isoformat(),
                end=chunk_end.isoformat(),
            )
        )
        cursor = chunk_end
    return chunks


__all__ = [
    "ALL_GROWW_INTERVALS",
    "GROWW_INTERVAL_TO_PARTITION",
    "WINDOW_DAYS",
    "plan_chunks",
]
