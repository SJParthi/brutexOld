"""Stored-interval **day read** for the movers screens (DEC-PIPELINE-AUTOPREBUILD-001, chunk 2).

The pull pipeline's automatic FINAL prebuild stage materialises every derived
intraday timeframe (and the derived ``1day``) as its own stored lake partition
(``bars/<exch>/<segment>/<symbol>/<interval>/<yyyy>/<mm>.parquet`` —
:func:`~brutex.data.timeframe_materialize.materialize_lake_timeframes`). After
it, the SPOT movers intraday path reads those **prebuilt partitions FIRST**
instead of re-resampling the day's 1-minute frame on every cache miss; when the
stored partition is absent/empty for the day (a CLI-only lake, a pre-existing
lake whose pipeline prebuild never ran, a never-materialised symbol) it falls
back to the on-the-fly 1-minute resample — stated openly, never hidden
(Golden Rule #10).

## Why stored-read ≡ on-the-fly resample (the equality contract)

The prebuilt bars were produced by the SAME resampler the fallback uses
(:func:`~brutex.data.tf_resampler.resample_1m_to_tf` under
:func:`~brutex.data.spot_resample.derive_intraday_tfs`), whose bucket grid is
FIXED ARITHMETIC anchored at each bar's own IST-date 09:15 session open
(DEC-RESAMPLE-ANCHOR-002) — the grid depends only on calendar time, and a
bucket can never straddle IST days. Resampling one day's 1-minute bars in
isolation therefore yields byte-identical buckets to materialising the whole
series and filtering to that day; the equality is pinned by
``tests/integration/test_movers_stored_read.py``.

## Honest scope — SPOT only (the futures/options envelope)

The FUTURES + OPTIONS movers session reads are deliberately NOT switched to
stored TF partitions: their session reader
(:func:`~brutex.dashboard.movers_futures._read_future_session`) is a LOCKED
1-minute-native **as-of cutoff snapshot** (DEC-FNO-TF-DERIVE-OI-001
corrected_point #1b — cumulative session-to-cutoff, explicitly NOT a resampled
TF bucket) that also carries the per-bar STORED Greeks overlay off the SAME
1-minute frame (DEC-GREEKS-ENGINE-PR3B-001). A stored-TF read cannot reproduce
those numbers (TF-granular HLC3 turnover ≠ 1-minute HLC3; the TF partitions
carry no iv/delta), so switching would silently change pinned values — the
F&O movers benefit from the prebuild through their prior-day baselines instead
(:func:`~brutex.dashboard.movers_snapshot._prev_day_close` /
:func:`~brutex.dashboard.movers_futures._prev_day_oi` already read the derived
``1day`` partition FIRST, with the bounded 1-minute walk-back only as the
fallback), which the same integration suite pins.

## Month-bounded, never a whole-partition scan

The read reuses the COMMON
:func:`~brutex.data.lake_day_glob.read_day_intraday_frame` (CLAUDE.md §11): an
intraday bar's bucket label shares its IST calendar date and the IST session
never crosses a UTC month boundary, so — exactly as for the 1-minute base —
one ``<yyyy>/<mm>.parquet`` file holds the whole day for a session-filtered
symbol (``O(one month)``, DEC-MOVERS-O1-LATENCY-001; a session-exempt symbol
falls back to the whole partition inside that helper, byte-equivalent). The
derived ``1day`` partition is OUT OF SCOPE here (its bars are stamped at
midnight IST and can sit in the prior UTC month's file — the daily movers path
has its own reader, :func:`~brutex.dashboard.movers_snapshot._read_daily_bars`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brutex.data.lake_day_glob import read_day_intraday_frame

if TYPE_CHECKING:
    from datetime import date as date_cls
    from pathlib import Path

    import polars as pl

__all__ = ["read_stored_day_frame"]

#: IST is a fixed +05:30 (no DST) — mirrors ``movers_snapshot._IST`` (the lake
#: stores bar time as tz-aware UTC; the day filter converts per bar).
_IST = "Asia/Kolkata"


def read_stored_day_frame(
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    day: date_cls,
) -> pl.DataFrame | None:
    """Read *day*'s bars from the STORED *interval* partition, or ``None``.

    Returns the day's bars as a sorted polars frame in the resampler's shape
    (``ts_utc, open, high, low, close, volume`` — the same columns
    :func:`~brutex.dashboard.movers_snapshot._read_session_bars` feeds
    ``_row_to_bar``), read month-bounded via
    :func:`~brutex.data.lake_day_glob.read_day_intraday_frame`. ``None`` when
    the partition directory / month file is absent OR holds no bar whose IST
    trading date equals *day* — the caller then falls back to the on-the-fly
    1-minute resample (the honest fallback for lakes the pipeline prebuild
    never materialised), never fabricating a bar (Golden Rule #1).

    *interval* must be an INTRADAY lake label (``1minute`` … ``4hour``); the
    derived ``1day`` partition is midnight-IST-stamped and month-boundary-unsafe
    for this read (see the module docstring) — the daily movers path never
    calls this.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    exchange, segment, symbol
        The partition coordinates of the series to read.
    interval
        The stored intraday interval label whose partition to read.
    day
        The IST trading day to slice.

    Returns
    -------
    polars.DataFrame | None
        The day's stored bars (sorted, resampler-shaped), or ``None`` when the
        stored partition has no rows for the day.
    """
    import polars as pl

    base = lake_root / "bars" / exchange / segment / symbol / interval
    month = read_day_intraday_frame(base, day, symbol=symbol)
    if month is None:
        return None
    df = (
        month.rename({"timestamp": "ts_utc"})
        .select("ts_utc", "open", "high", "low", "close", "volume")
        .sort("ts_utc")
    )
    ist_date = pl.col("ts_utc").dt.convert_time_zone(_IST).dt.date()
    day_df = df.filter(ist_date == day)
    if day_df.is_empty():
        return None
    return day_df
