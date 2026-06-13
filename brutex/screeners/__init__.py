"""BruteX screeners — reference-only lake analytics (DEC-MOVERS-VOLUME-001).

Screeners rank the **lake** (the broad REFERENCE_ONLY surface — every admitted
index plus the F&O-eligible / NIFTY-Total-Market underlyings) and NEVER touch the
brute-force engine or the 52-condition vocabulary (CLAUDE.md §3 scope lock,
DEC-NTM-GROWW-REPOINT-001). They are read-only consumers of the data lake.

The first screener is :mod:`brutex.screeners.volume_movers` — a pure
volume-surge / realised-range-volatility ranker (DEC-MOVERS-VOLUME-001).
"""

from __future__ import annotations

from brutex.screeners.volume_movers import (
    LakeBarReader,
    MoverMetrics,
    MoversReport,
    ScreenTarget,
    compute_mover_metrics,
    rank_by_realised_volatility,
    rank_by_volume_surge,
    rank_movers,
    screen_movers,
)

__all__ = [
    "LakeBarReader",
    "MoverMetrics",
    "MoversReport",
    "ScreenTarget",
    "compute_mover_metrics",
    "rank_by_realised_volatility",
    "rank_by_volume_surge",
    "rank_movers",
    "screen_movers",
]
