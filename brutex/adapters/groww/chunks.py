"""Date-range chunker for the Groww backtesting API.

Groww imposes per-call maximum date ranges that vary by candle interval
(``docs/groww/api-reference/03-backtesting.md``):

* 1m / 2m / 3m / 5m → **30 calendar days** per call
* 10m / 15m / 30m   → **90 calendar days** per call
* 1h / 4h / 1d / 1w / 1mo → **180 calendar days** per call

:func:`iter_chunks` tiles ``[start, end]`` (both inclusive) into
``(chunk_start, chunk_end)`` pairs where each pair spans at most the
documented window for the given *interval*.

The caller converts each ``(chunk_start, chunk_end)`` pair to the API's
``start_time``/``end_time`` strings (``"YYYY-MM-DD HH:mm:ss"`` in IST):

.. code-block:: python

    start_time = f"{chunk_start} {MARKET_OPEN_IST}"   # "2021-01-01 09:15:00"
    end_time   = f"{chunk_end}   {MARKET_CLOSE_IST}"  # "2021-01-30 15:30:00"

This module is **pure logic** (no I/O, no mutable state).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

from brutex.adapters.groww.constants import ALL_INTERVALS, INTERVAL_CHUNK_WINDOW


def chunk_window(interval: str) -> int:
    """Return the maximum calendar-day span for one API call for *interval*.

    Parameters
    ----------
    interval:
        A BruteX canonical interval string (e.g. ``"1minute"``, ``"1day"``).

    Raises
    ------
    ValueError
        If *interval* is not a recognised Groww interval.
    """
    if interval not in ALL_INTERVALS:
        raise ValueError(
            f"Unknown interval {interval!r}. "
            f"Supported: {sorted(ALL_INTERVALS)}"
        )
    return INTERVAL_CHUNK_WINDOW[interval]


def iter_chunks(
    start: date,
    end: date,
    interval: str,
) -> Iterator[tuple[date, date]]:
    """Tile ``[start, end]`` into ``(chunk_start, chunk_end)`` pairs.

    Both *start* and *end* are **inclusive** — the yielded pairs cover
    them completely without gaps or overlaps.

    Parameters
    ----------
    start:
        First calendar day to include (inclusive).
    end:
        Last calendar day to include (inclusive). Must be ≥ *start*.
    interval:
        BruteX canonical interval string (determines window size).

    Yields
    ------
    tuple[date, date]
        ``(chunk_start, chunk_end)`` where both endpoints are inclusive
        and ``chunk_end - chunk_start < chunk_window(interval)`` days.

    Raises
    ------
    ValueError
        If *end* < *start* or *interval* is unrecognised.
    """
    if end < start:
        raise ValueError(
            f"end ({end}) must be ≥ start ({start})"
        )
    window = chunk_window(interval)
    chunk_start = start
    while chunk_start <= end:
        # chunk_end is inclusive: at most (window - 1) days after chunk_start
        chunk_end = min(chunk_start + timedelta(days=window - 1), end)
        yield chunk_start, chunk_end
        chunk_start = chunk_end + timedelta(days=1)


def chunk_count(start: date, end: date, interval: str) -> int:
    """Return the number of API calls required to fetch ``[start, end]``.

    Useful for capacity planning and audit log pre-flight checks.
    """
    return sum(1 for _ in iter_chunks(start, end, interval))


__all__ = ["chunk_count", "chunk_window", "iter_chunks"]
