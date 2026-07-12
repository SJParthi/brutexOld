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

## Month-bounded — plus the month-edge prior-file probe for derived-TF labels

The read reuses the COMMON
:func:`~brutex.data.lake_day_glob.read_day_intraday_frame` (CLAUDE.md §11): a
session-filtered symbol's intraday bucket label shares its IST calendar date
(labels are pulled DOWNWARD from bar times ≥ ~09:00 IST, so the earliest
possible label is 05:15 IST — a stray 09:00 pre-open bar's 4hour bucket), so
one ``<yyyy>/<mm>.parquet`` file *almost* always holds the whole day
(``O(one month)``, DEC-MOVERS-O1-LATENCY-001; a session-exempt symbol falls
back to the whole partition inside that helper, byte-equivalent). The ONE
exception (DEC-MOVERS-STORED-FRESHNESS-001, REFUTER ROUND 2 defect B2): a
label BEFORE 05:30 IST maps to the PRIOR UTC day, so on the **1st of an IST
month** the prebuild files that bucket under the PRIOR UTC month's parquet —
the executed shape is exactly the 05:15-IST 4hour label above (= 23:45 UTC of
the prior month's last day). Pre-fix the month-bounded read silently DROPPED
that bucket (a pre-existing read-completeness bug — pre-guard the served frame
was missing it; post-guard a genuinely FRESH lake read permanently false-STALE
and "re-run the prebuild" could never heal it, because the prebuild itself
produces the routing). The read therefore ALSO probes the prior UTC month's
file exactly when the shape is possible — see
:func:`_prior_month_label_probe_applies` for the derived arithmetic — and the
existing IST-day filter keeps correctness (only *day*'s rows are admitted from
either file). The derived ``1day`` partition is OUT OF SCOPE here (its bars
are stamped at midnight IST and ALWAYS sit in the prior UTC month's file on
the 1st — the daily movers path has its own reader,
:func:`~brutex.dashboard.movers_snapshot._read_daily_bars`).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from brutex.data.lake_day_glob import read_day_intraday_frame, read_day_month_frame
from brutex.data.session import is_session_exempt

if TYPE_CHECKING:
    from datetime import date as date_cls
    from pathlib import Path

    import polars as pl

__all__ = ["read_stored_day_frame"]

#: IST is a fixed +05:30 (no DST) — mirrors ``movers_snapshot._IST`` (the lake
#: stores bar time as tz-aware UTC; the day filter converts per bar).
_IST = "Asia/Kolkata"


def _prior_month_label_probe_applies(*, interval: str, day: date_cls, symbol: str) -> bool:
    """Whether *day*'s stored read must ALSO probe the PRIOR UTC month's file.

    DEC-MOVERS-STORED-FRESHNESS-001, REFUTER ROUND 2 defect B2 — the month-edge
    label-routing arithmetic. A derived-TF bucket label is 09:15-anchored
    DOWNWARD (``label(mm) = 555 + floor((mm − 555)/m)·m`` in IST minutes-of-day
    for TF minutes ``m``; the sub-09:15 rungs are ``555 − k·m``). The label
    shares the bar's own IST *date* for a session-filtered symbol, but a label
    before **05:30 IST** (minute < 330) maps to the PRIOR **UTC** day — and on
    the **1st of an IST month** the prior UTC day is the prior UTC *month*, so
    :func:`~brutex.data.partition.partition_for_timestamp` files the bucket row
    under the PRIOR month's ``<yyyy>/<mm>.parquet``. Three exact conjuncts:

    * ``day.day == 1`` — on any other day the prior UTC day is the SAME month
      file, so the month-bounded read already sees every same-IST-day label
      (the healthy path pays ZERO extra reads — spy-pinned).
    * ``interval != "1minute"`` — every DERIVED intraday TF can hold a
      sub-05:30 label: the lowest 09:15-anchored rung is ``555 mod m ∈ [0, m)``
      and every derived TF has ``m ≤ 240 < 330``, so a sufficiently early bar
      always CAN label before 05:30 IST (the executed shape needs only the
      acknowledged legacy stray 09:00 pre-open bar at 4hour — label 05:15 IST).
      The 1-minute partition's row *times* ARE bar times, and a
      session-filtered symbol's bars (~09:00–19:15 IST — session filter +
      pre-open strays + Muhurat evening) never precede 05:30 IST, so 1minute
      never crosses and skips the probe.
    * session-FILTERED symbol — a session-exempt symbol (GIFT ~21h) already
      takes the whole-partition read inside
      :func:`~brutex.data.lake_day_glob.read_day_intraday_frame` (prior month
      file included); probing again would double-read the same rows.
    """
    return day.day == 1 and interval != "1minute" and not is_session_exempt(symbol)


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
    :func:`~brutex.data.lake_day_glob.read_day_intraday_frame` — PLUS, exactly
    when :func:`_prior_month_label_probe_applies` (1st of an IST month ×
    derived TF × session-filtered symbol), one probe of the PRIOR UTC month's
    file for a sub-05:30-IST bucket label the prebuild filed there (REFUTER
    ROUND 2 defect B2 — the month-edge routing; the IST-day filter below keeps
    correctness, admitting only *day*'s rows from either file, so a genuinely
    fresh month-edge lake now reads its COMPLETE stored frame instead of a
    silently bucket-dropped one). A cross-file duplicated/foreign row (a
    healer-class poisoned lake) is NOT deduped here — the duplicated frame
    simply fails the caller's verify-equality guard and falls back to the
    1-minute resample (fail-safe; the lake healer remains the heal). ``None``
    when the partition directory / probed month file(s) are absent OR hold no
    bar whose IST trading date equals *day* — the caller then falls back to
    the on-the-fly 1-minute resample (the honest fallback for lakes the
    pipeline prebuild never materialised), never fabricating a bar
    (Golden Rule #1).

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
    frames = [] if month is None else [month]
    if _prior_month_label_probe_applies(interval=interval, day=day, symbol=symbol):
        # Month-edge probe (REFUTER ROUND 2 B2): a derived-TF bucket labeled
        # before 05:30 IST on the 1st of an IST month was FILED by the prebuild
        # under the prior UTC month's parquet — one bounded extra read on
        # qualifying coordinates only (day - 1 day = the prior month's file).
        prior = read_day_month_frame(base, day - timedelta(days=1))
        if prior is not None:
            frames.append(prior)
    if not frames:
        return None
    shaped = [
        f.rename({"timestamp": "ts_utc"}).select("ts_utc", "open", "high", "low", "close", "volume")
        for f in frames
    ]
    df = (shaped[0] if len(shaped) == 1 else pl.concat(shaped)).sort("ts_utc")
    ist_date = pl.col("ts_utc").dt.convert_time_zone(_IST).dt.date()
    day_df = df.filter(ist_date == day)
    if day_df.is_empty():
        return None
    return day_df
