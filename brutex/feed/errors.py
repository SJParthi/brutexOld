"""Typed exceptions for :mod:`brutex.feed`."""

from __future__ import annotations

from brutex.models import BruteXError


class FeedError(BruteXError):
    """Base class for every error raised by :mod:`brutex.feed`."""


class LookAheadError(FeedError):
    """Raised when a caller would read a bar whose timestamp is
    strictly after the feed's current cursor.

    This is the runtime half of ``FR-INT-004``: strategy reads must
    never reach data with ``ts > current_ts``. The compile-time half
    is the property-based test in ``tests/property``.
    """


class ResampleError(FeedError):
    """Raised on an invalid :func:`~brutex.feed.resampler.resample_bars` call.

    Triggers:

    * Unknown source or target interval name
    * Target interval shorter than the source (up-sampling is
      mathematically undefined without an interpolation model)
    * Target interval not an integer multiple of the source
    * Bars spanning a timezone-naive timestamp
    """
