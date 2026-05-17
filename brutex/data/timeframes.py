"""Track 1 timeframe canonical list — all 21 timeframes.

Per `docs/TRACK1_BACKTEST_SPEC.md` §Workload Math:
    "21 timeframes (1m base + 19 derived intraday + 1d)"

The brute-force engine evaluates strategies at all 21 of these
timeframes. Bar lookup is O(1) per TF (per CLAUDE.md §2 #5).

## Composition

* 15 minute-level intraday TFs: 1m, 2m, ... 15m
* 5 multi-minute intraday TFs:  30m, 60m (=1h), 120m (=2h), 180m (=3h), 240m (=4h)
* 1 daily TF: 1d
* Total: 21

## Anchor convention

All resampling uses **left-closed, left-labeled** buckets anchored at
09:15 IST (session open). Example: 5m bar at 09:15 IST covers
[09:15:00, 09:20:00).
"""

from __future__ import annotations

from typing import Final

# 15 minute-by-minute intraday timeframes (1m to 15m).
INTRADAY_MINUTE_TFS: Final[tuple[int, ...]] = (
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    15,
)

# 5 multi-minute intraday timeframes.
INTRADAY_MULTIMINUTE_TFS: Final[tuple[int, ...]] = (
    30,
    60,
    120,
    180,
    240,
)

# 1 daily timeframe (in minutes — 6h 15m session).
DAILY_TF_MINUTES: Final[int] = 375

#: Track 1 timeframe count per spec (1m base + 19 derived intraday + 1d).
EXPECTED_TF_COUNT: Final[int] = 21

# All 21 timeframes (in minutes).
ALL_TF_MINUTES: Final[tuple[int, ...]] = (
    INTRADAY_MINUTE_TFS + INTRADAY_MULTIMINUTE_TFS + (DAILY_TF_MINUTES,)
)

if len(ALL_TF_MINUTES) != EXPECTED_TF_COUNT:
    raise AssertionError(
        f"Expected {EXPECTED_TF_COUNT} timeframes per spec, got {len(ALL_TF_MINUTES)}"
    )


# Canonical string labels (used as Parquet partition keys and config IDs).
TF_LABELS: Final[dict[int, str]] = {
    1: "1m",
    2: "2m",
    3: "3m",
    4: "4m",
    5: "5m",
    6: "6m",
    7: "7m",
    8: "8m",
    9: "9m",
    10: "10m",
    11: "11m",
    12: "12m",
    13: "13m",
    14: "14m",
    15: "15m",
    30: "30m",
    60: "60m",
    120: "120m",
    180: "180m",
    240: "240m",
    375: "1d",
}

if set(TF_LABELS.keys()) != set(ALL_TF_MINUTES):
    raise AssertionError("TF_LABELS keys must match ALL_TF_MINUTES exactly")


def label(tf_minutes: int) -> str:
    """Map minute-count to canonical TF label.

    Examples:
        >>> label(1)
        '1m'
        >>> label(60)
        '60m'
        >>> label(375)
        '1d'
    """
    if tf_minutes not in TF_LABELS:
        raise ValueError(
            f"Unsupported timeframe: {tf_minutes} minutes. "
            f"Must be one of {sorted(TF_LABELS.keys())}."
        )
    return TF_LABELS[tf_minutes]


def parse_label(s: str) -> int:
    """Parse a canonical TF label back to its minute count.

    Examples:
        >>> parse_label("1m")
        1
        >>> parse_label("1d")
        375
    """
    inverse = {v: k for k, v in TF_LABELS.items()}
    if s not in inverse:
        raise ValueError(
            f"Unknown timeframe label: {s!r}. Must be one of {sorted(inverse.keys())}."
        )
    return inverse[s]


def is_intraday(tf_minutes: int) -> bool:
    """True if the TF is intraday (< full session length)."""
    if tf_minutes not in TF_LABELS:
        raise ValueError(f"Unsupported timeframe: {tf_minutes}")
    return tf_minutes < DAILY_TF_MINUTES


def is_minute_multiple_of(target: int, source: int) -> bool:
    """True if `target` is an integer multiple of `source` in minutes.

    Used by the resampler to validate that 1m → 5m is allowed
    (5 % 1 == 0) but 1m → 7m is also allowed (7 % 1 == 0).
    Source minute count must divide target minute count exactly.

    The daily TF (375m) is special: it's a multiple of all intraday TFs
    that divide 375 (1, 3, 5, 15, 25, 75, 125, 375). For TFs that don't
    divide 375 cleanly (2, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 30, 60,
    120, 180, 240), daily resampling uses calendar-aware bucketing.
    """
    if source <= 0 or target <= 0:
        raise ValueError("Timeframes must be positive minute counts")
    return target % source == 0
