"""OHLCV resampler for canonical :class:`CandleBar` sequences.

Implements ``FR-IND-003`` (timeframe-agnostic indicators) for the
sub-day the Dhan native intervals. Converts bars at a *source* interval into
bars at a coarser *target* interval using wall-clock UTC bucketing.

## Aggregation rules

For each bucket of bars (all bars whose timestamp falls inside the
same ``[bucket_start, bucket_start + target)`` window):

=============  ==================================================
Field          Rule
=============  ==================================================
``timestamp``  Start of the bucket (closed=left, label=left).
``open``       First bar's open (earliest timestamp in bucket).
``high``       Maximum of all bars' highs.
``low``        Minimum of all bars' lows.
``close``      Last bar's close (latest timestamp in bucket).
``volume``     Sum of all bars' volumes.
``open_interest``  Last bar's open interest (snapshot semantics,
               consistent with how Dhan reports OI per bar).
=============  ==================================================

## Scope — v1

* Only the eight sub-day the Dhan native intervals are supported as both source
  and target — :data:`RESAMPLEABLE_INTERVALS`.
* Target must be **an integer multiple** of the source. ``1minute ->
  5minute`` works; ``5minute -> 7minute`` raises
  :class:`~brutex.feed.errors.ResampleError`.
* Target = source is a legal no-op (returns a shallow copy).
* ``1day`` / ``1week`` / ``1month`` are deliberately excluded because
  their bucket boundaries are calendar-aware (session, weekend, DST)
  and land in the :mod:`brutex.feed.calendar` module — P3, ADR-0014.

## Input contract

Bars must be sorted ascending by timestamp; the resampler does **not**
re-sort. All timestamps must be timezone-aware UTC — naive datetimes
are rejected early rather than silently ambiguous.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from brutex.data.models import CandleBar
from brutex.feed.errors import ResampleError

#: The sub-day the Dhan native intervals in seconds. Keep aligned with
#: :data:`brutex.data.partition.VALID_INTERVALS` but note that
#: ``1week`` and ``1month`` are absent on purpose — see module
#: docstring (P3 calendar work).
INTERVAL_SECONDS: Final[dict[str, int]] = {
    "1minute": 60,
    "2minute": 120,
    "3minute": 180,
    "5minute": 300,
    "10minute": 600,
    "15minute": 900,
    "30minute": 1800,
    "1hour": 3600,
    "4hour": 14400,
    "1day": 86400,
}

#: The subset of :data:`INTERVAL_SECONDS` that the v1 resampler accepts
#: on both sides of the conversion. Tighter than the full set so the
#: user gets a clear ``ResampleError`` if they try ``1week`` before
#: the calendar module lands.
RESAMPLEABLE_INTERVALS: Final[frozenset[str]] = frozenset(
    {
        "1minute",
        "2minute",
        "3minute",
        "5minute",
        "10minute",
        "15minute",
        "30minute",
        "1hour",
        "4hour",
    }
)


def resample_bars(
    *,
    bars: Sequence[CandleBar],
    source_interval: str,
    target_interval: str,
) -> list[CandleBar]:
    """Aggregate *bars* from *source_interval* up to *target_interval*.

    Parameters
    ----------
    bars
        Ascending-by-timestamp sequence of :class:`CandleBar`. May be
        empty (returns ``[]``).
    source_interval
        Interval of the incoming bars, e.g. ``"1minute"``. Must be a
        key of :data:`RESAMPLEABLE_INTERVALS`.
    target_interval
        Desired output interval, e.g. ``"5minute"``. Must be a key of
        :data:`RESAMPLEABLE_INTERVALS` and an integer multiple of
        *source_interval*.

    Returns
    -------
    list[CandleBar]
        New list of aggregated bars, one per bucket. Timestamps are
        the bucket starts (closed=left, label=left).

    Raises
    ------
    ResampleError
        If an interval is unknown, up-sampling is requested, target
        is not a multiple of source, or any input bar has a naive
        timestamp.
    """
    _validate_interval(source_interval, which="source")
    _validate_interval(target_interval, which="target")

    src_s = INTERVAL_SECONDS[source_interval]
    tgt_s = INTERVAL_SECONDS[target_interval]

    if tgt_s < src_s:
        raise ResampleError(
            f"target_interval {target_interval!r} is shorter than "
            f"source_interval {source_interval!r} — up-sampling is "
            f"undefined without an interpolation model"
        )
    if tgt_s == src_s:
        return list(bars)
    if tgt_s % src_s != 0:
        raise ResampleError(
            f"target_interval ({tgt_s}s) must be an integer multiple "
            f"of source_interval ({src_s}s); got ratio "
            f"{tgt_s / src_s:.3f}"
        )
    if not bars:
        return []

    # Group bars into buckets. Input is already sorted so we can
    # walk linearly and emit a bucket whenever bucket_start changes.
    out: list[CandleBar] = []
    bucket: list[CandleBar] = []
    current_bucket_start: datetime | None = None

    for bar in bars:
        if bar.timestamp.tzinfo is None:
            raise ResampleError(
                f"resample_bars requires tz-aware timestamps, got naive {bar.timestamp!r}"
            )
        bucket_start = _bucket_start(bar.timestamp, tgt_s)
        if current_bucket_start is None or bucket_start != current_bucket_start:
            if bucket:
                out.append(_aggregate(bucket, current_bucket_start))
            bucket = [bar]
            current_bucket_start = bucket_start
        else:
            bucket.append(bar)

    if bucket:
        out.append(_aggregate(bucket, current_bucket_start))

    return out


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _validate_interval(interval: str, *, which: str) -> None:
    """Raise :class:`ResampleError` if *interval* is not resampleable."""
    if interval not in RESAMPLEABLE_INTERVALS:
        raise ResampleError(
            f"{which}_interval {interval!r} is not in the v1 "
            f"resampleable set {sorted(RESAMPLEABLE_INTERVALS)}"
        )


def _bucket_start(ts: datetime, bucket_seconds: int) -> datetime:
    """Return the UTC bucket-start timestamp for *ts*.

    We floor the POSIX timestamp to the nearest multiple of
    *bucket_seconds*, so two bars whose timestamps differ by less than
    one bucket always land in the same bucket. Because BruteX stores
    every bar in UTC, the wall-clock bucketing is unambiguous across
    DST and session boundaries.
    """
    epoch_s = int(ts.timestamp())
    bucket_epoch = (epoch_s // bucket_seconds) * bucket_seconds
    return datetime.fromtimestamp(bucket_epoch, tz=UTC)


def _aggregate(
    bucket: list[CandleBar],
    bucket_start: datetime | None,
) -> CandleBar:
    """Fold *bucket* into one :class:`CandleBar`.

    Assumes *bucket* is non-empty and sorted by timestamp (which the
    caller guarantees). *bucket_start* is the timestamp label to
    attach to the aggregate bar.
    """
    if bucket_start is None:  # pragma: no cover — guarded by caller
        raise ResampleError("internal: _aggregate called with no bucket_start")
    first = bucket[0]
    last = bucket[-1]
    return CandleBar(
        timestamp=bucket_start,
        open=first.open,
        high=max(b.high for b in bucket),
        low=min(b.low for b in bucket),
        close=last.close,
        volume=sum(b.volume for b in bucket),
        open_interest=last.open_interest,
    )


__all__ = [
    "INTERVAL_SECONDS",
    "RESAMPLEABLE_INTERVALS",
    "resample_bars",
]
