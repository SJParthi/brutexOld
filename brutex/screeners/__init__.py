"""BruteX screeners — reference-only ranking views over the data lake.

Screeners read the ingested lake and rank instruments for research context.
They are **reference-only** (CLAUDE.md §3): they never touch the brute-force
engine, the 52-condition vocabulary, or trade simulation — they only read OHLC
already in the lake and order it.

Modules
-------
* :mod:`brutex.screeners.gainers_losers` — top gainers / losers by window %
  change (``DEC-MOVERS-GAINERS-001``, ``docs/MIGRATION.md`` entry #701).
* :mod:`brutex.screeners.volume_movers` — volume-surge / realised intraday
  range-volatility movers (``DEC-MOVERS-VOLUME-001``, ``docs/MIGRATION.md``
  entry #702). Its pure ranker is exported here as ``rank_volume_movers`` (the
  module-level name ``rank_movers`` is aliased to avoid clashing with the
  gainers/losers screener's ``rank_movers``).
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
from brutex.screeners.volume_movers import (
    LakeBarReader,
    MoverMetrics,
    MoversReport,
    ScreenTarget,
    compute_mover_metrics,
    rank_by_realised_volatility,
    rank_by_volume_surge,
    screen_movers,
)
from brutex.screeners.volume_movers import (
    rank_movers as rank_volume_movers,
)

__all__ = [
    "InstrumentRef",
    "LakeBarReader",
    "LakeReader",
    "Mover",
    "MoverMetrics",
    "MoversReport",
    "ScreenResult",
    "ScreenTarget",
    "compute_mover_metrics",
    "rank_by_realised_volatility",
    "rank_by_volume_surge",
    "rank_movers",
    "rank_volume_movers",
    "screen_gainers_losers",
    "screen_movers",
]
