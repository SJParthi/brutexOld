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

Since DEC-MOVERS-STORED-FRESHNESS-001 the equality contract is additionally
**VERIFIED per read**, never assumed: the movers stored-first reader judges the
day slice it is about to serve against a fresh re-derive of the in-hand raw
1-minute base (:func:`stored_day_frame_is_stale`, delegating to the ONE
existing comparator :func:`brutex.data.tf_autoderive.compare_derived_freshness`)
and falls back to the on-the-fly 1-minute resample on ANY drift — a stale
prebuilt partition (a CLI-only fetch after a prebuild, a quarantined /
crashed / partial prebuild, a mid-session live-edge bucket) can no longer
serve a wrong LTP silently.

The stored partitions hold FULL buckets, so under the unified raw-1m as-of
semantic (DEC-HARDENING-LEFTOVERS-001 —
:func:`~brutex.dashboard.movers_snapshot.filter_to_ist_minute` cuts the
1-minute frame BEFORE resampling, making the final bucket a PARTIAL aggregate
of only the elapsed minutes) the stored read is consulted only for a whole-day
read or a bucket-final-aligned as-of cutoff
(``movers_snapshot._stored_serves_cutoff``); a mid-bucket as-of always takes
the 1-minute resample fallback — the only source that can build the partial
forming bucket honestly.

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

__all__ = ["read_stored_day_frame", "stored_day_frame_is_stale"]

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


def stored_day_frame_is_stale(
    stored_df: pl.DataFrame, base_1m_df: pl.DataFrame, *, tf_minutes: int
) -> bool:
    """``True`` iff the stored day frame drifts from a fresh re-derive of the raw-1m base.

    DEC-MOVERS-STORED-FRESHNESS-001 — the movers stored-first freshness verdict.
    PURE: frames in, ``bool`` out, zero I/O, zero logging — the composition seam
    for a screening bar-reader wrapper (a screen can stack before/after the
    stored-first read in either merge order). CONTRACT: feed BOTH frames from
    the SAME screening stage — screened-vs-screened or raw-vs-raw, never mixed,
    or a one-sided row drop reads as drift.

    Delegates to the ONE existing comparator
    :func:`brutex.data.tf_autoderive.compare_derived_freshness` (the ``brutex
    doctor`` probe + the DEC-SUSPICION-SWEEP-FIXES-001 G1 sweep-preflight gate
    core — exact frame equality over the canonical bar columns, height
    early-out, ``stored.cast(expected.schema)``; CLAUDE.md §11 COMMON, never a
    third comparator). Day-scoping is purely the CALLER's frame slicing (both
    inputs day-sliced + cutoff-aligned) — sound because the resampler grid is
    per-bar-IST-date anchored (DEC-RESAMPLE-ANCHOR-002: day-in-isolation ≡
    whole-series-then-filter, this module's pinned equality contract).

    ``tf_minutes == 1`` short-circuits ``False``: the stored ``1minute``
    partition IS the fetched base — trivially fresh, so no per-candidate
    validate + clone + equals is paid on the hottest interval. A base with no
    verdict (``None``) is the CALLER's abstain — this predicate takes real
    frames only (never guesses, Golden Rule #1).

    Parameters
    ----------
    stored_df
        The CUT stored day frame about to be served (resampler-shaped
        ``ts_utc``/OHLCV columns) — comparing the exact served frame leaves no
        TOCTOU gap on the served bytes.
    base_1m_df
        The raw 1-minute day frame cut at the SAME as-of instant.
    tf_minutes
        The stored interval's bar length in minutes.

    Returns
    -------
    bool
        ``True`` when the re-derive of *base_1m_df* differs from *stored_df*
        in ANY row/timestamp/value; ``False`` on exact agreement (or the
        ``1minute`` short-circuit).
    """
    if tf_minutes == 1:
        return False
    from brutex.data.tf_autoderive import compare_derived_freshness

    return compare_derived_freshness(base_1m_df, stored_df, tf_minutes) == "stale"
