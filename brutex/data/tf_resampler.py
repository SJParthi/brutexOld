"""Polars-vectorized 1m → 21-TF resampler.

Per `docs/TRACK1_BACKTEST_SPEC.md` §Strategy Specification and
spec §Workload Math (21 timeframes):
- 1m base (no resampling needed)
- 14 minute-multiple intraday: 2m, 3m, ..., 15m
- 5 multi-minute intraday: 30m, 60m, 120m, 180m, 240m
- 1 daily: 1d (full trading-day aggregation)

## Aggregation rules

For each bucket [bucket_start, bucket_start + tf):

| Field    | Rule                                           |
|----------|------------------------------------------------|
| ts_utc   | bucket_start (left-closed, left-labeled)       |
| open     | first row's open (earliest in bucket)          |
| high     | max of all rows' high                          |
| low      | min of all rows' low                           |
| close    | last row's close (latest in bucket)            |
| volume   | sum of all rows' volume                        |

## Anchor

All buckets anchored at 09:15 IST (session open) by FIXED ARITHMETIC
(DEC-RESAMPLE-ANCHOR-002): ``bucket = session_open + floor((ts -
session_open)/tf)*tf``, where ``session_open`` is the bar's IST date at
09:15. The grid depends only on calendar time — a missing 09:15 bar can
NEVER shift it (the prior first-bar anchoring could). A 5m bar at
09:15:00 IST covers [09:15:00, 09:20:00).

For daily TF: one bar per trading day, covering [session_open,
session_close), labeled at the 09:15 IST session open.

## Polars implementation

Uses `pl.DataFrame.group_by_dynamic()` for minute-multiple TFs (Polars
native fast aggregation, all 21 TFs in seconds for a 5-year 1m
dataset).

Per Agent A research (2026-05-16): Polars 100M-row groupby completes
in 23 sec on M-series (medium.com/codastra benchmark). For Track 1
(~500K 1m bars per instrument over 5 years), groupby latency is
single-digit ms per TF.

## Determinism

Polars groupby is deterministic for sorted input. Caller MUST provide
1m DataFrame sorted ascending by `ts_utc` (this function does NOT
re-sort — `df.sort('ts_utc')` is the caller's responsibility).

No look-ahead — Polars buckets are computed from left-closed
intervals, so bar at time T uses only rows [T, T+tf). Property P-06 holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import polars as pl

from brutex.data.timeframes import ALL_TF_MINUTES, is_intraday

# Required schema for input 1m DataFrame.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "ts_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


def _validate_input(df_1m) -> None:  # type: ignore[no-untyped-def]
    """Validate input DataFrame schema and sort order."""
    import polars as pl

    if not isinstance(df_1m, pl.DataFrame):
        raise TypeError(f"Expected polars.DataFrame, got {type(df_1m).__name__}")
    missing = [c for c in REQUIRED_COLUMNS if c not in df_1m.columns]
    if missing:
        raise ValueError(
            f"Input DataFrame missing required columns: {missing}. "
            f"Required: {REQUIRED_COLUMNS}, got: {df_1m.columns}"
        )
    # Verify ts_utc is datetime type
    ts_dtype = df_1m.schema["ts_utc"]
    if not isinstance(ts_dtype, pl.Datetime):
        raise ValueError(
            f"ts_utc must be polars.Datetime, got {ts_dtype}. "
            f"Convert with `pl.col('ts_utc').cast(pl.Datetime)` first."
        )


def resample_1m_to_tf(df_1m, target_tf_minutes: int):  # type: ignore[no-untyped-def]
    """Resample a 1-minute bar DataFrame to a target timeframe.

    Args:
        df_1m: Polars DataFrame with columns [ts_utc, open, high, low,
               close, volume]. ts_utc must be Datetime dtype. Caller
               must sort by ts_utc ascending before calling.
        target_tf_minutes: Target timeframe in minutes. Must be one of
            the 21 TFs in `brutex.data.timeframes.ALL_TF_MINUTES`.
            Special value 375 = daily (1d).

    Returns:
        Polars DataFrame with same column schema, but resampled to the
        target TF. Bucket timestamps are left-closed, left-labeled.

    Examples:
        >>> df_5m = resample_1m_to_tf(df_1m, 5)
        >>> df_15m = resample_1m_to_tf(df_1m, 15)
        >>> df_daily = resample_1m_to_tf(df_1m, 375)
    """
    import polars as pl

    _validate_input(df_1m)
    if target_tf_minutes not in ALL_TF_MINUTES:
        raise ValueError(
            f"Unsupported target TF: {target_tf_minutes} minutes. "
            f"Must be one of {sorted(ALL_TF_MINUTES)}."
        )

    # 1m -> 1m is a no-op (shallow copy preserves immutability semantics).
    if target_tf_minutes == 1:
        return df_1m.clone()

    # FIXED-ARITHMETIC session anchor (DEC-RESAMPLE-ANCHOR-002, hardening
    # DEC-RESAMPLE-ANCHOR-001): each row's bucket label is its IST date's
    # 09:15 session open plus floor-of-elapsed-minutes-over-tf whole buckets.
    # Pure column math — the grid depends ONLY on the calendar TIME, never on
    # which bars happen to exist. The prior ``group_by_dynamic(start_by=
    # "datapoint")`` anchored each session's grid to its FIRST BAR, so a
    # vendor gap at the 09:15 open silently shifted the WHOLE day's grid for
    # every timeframe (the hazard the 2026-06-11 4-agent review flagged).
    # Buckets can never straddle sessions: every label shares the bar's IST
    # trading date by construction. IST is a fixed +05:30 (no DST), so the
    # arithmetic needs no calendar module for Track 1's intraday TFs.
    ist = pl.col("ts_utc") + pl.duration(hours=5, minutes=30)
    session_open_ist = ist.dt.truncate("1d") + pl.duration(hours=9, minutes=15)

    if is_intraday(target_tf_minutes):
        offset_minutes = (ist - session_open_ist).dt.total_minutes()
        bucket_minutes = (offset_minutes // target_tf_minutes) * target_tf_minutes
        bucket_utc = (session_open_ist + pl.duration(minutes=bucket_minutes)) - pl.duration(
            hours=5, minutes=30
        )
    else:
        # Daily TF (375 minutes): one bucket per IST trading day, labeled at
        # the 09:15 session open (NOT the first bar — same gap immunity).
        bucket_utc = session_open_ist - pl.duration(hours=5, minutes=30)

    return (
        df_1m.with_columns(bucket_utc.alias("_bucket_ts"))
        .group_by("_bucket_ts", maintain_order=True)
        .agg(
            pl.col("open").first().alias("open"),
            pl.col("high").max().alias("high"),
            pl.col("low").min().alias("low"),
            pl.col("close").last().alias("close"),
            pl.col("volume").sum().alias("volume"),
        )
        .rename({"_bucket_ts": "ts_utc"})
        .sort("ts_utc")
    )


def resample_all_21_tfs(df_1m) -> dict[int, pl.DataFrame]:  # type: ignore[no-untyped-def]
    """Resample 1m bars to all 21 timeframes in one call.

    Args:
        df_1m: 1m Polars DataFrame, sorted by ts_utc.

    Returns:
        Dict mapping `tf_minutes -> DataFrame`. Includes 1m as identity.

    Example:
        all_tfs = resample_all_21_tfs(df_1m)
        df_5m = all_tfs[5]
        df_daily = all_tfs[375]
    """
    return {tf: resample_1m_to_tf(df_1m, tf) for tf in ALL_TF_MINUTES}
