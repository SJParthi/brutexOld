"""BruteX screeners — reference-only ranking views over the data lake.

Screeners read the ingested lake and rank instruments for research context.
They are **reference-only** (CLAUDE.md §3): they never touch the brute-force
engine, the 52-condition vocabulary, or trade simulation — they only read OHLC
already in the lake and order it.

Modules
-------
* :mod:`brutex.screeners.gainers_losers` — top gainers / losers by window %
  change (``DEC-MOVERS-GAINERS-001``, ``docs/MIGRATION.md`` entry #699).
"""

from __future__ import annotations

from brutex.screeners.gainers_losers import (
    InstrumentRef,
    LakeReader,
    Mover,
    ScreenResult,
    rank_movers,
    screen_gainers_losers,
)

__all__ = [
    "InstrumentRef",
    "LakeReader",
    "Mover",
    "ScreenResult",
    "rank_movers",
    "screen_gainers_losers",
]
