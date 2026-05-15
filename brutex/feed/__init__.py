"""BruteX replay feed + on-demand resampler (L3 virtual market feed).

Implements ``FR-FWD-001`` (chronological replay from the data lake) and
``FR-IND-003`` (timeframe-agnostic indicators via on-demand resampling).

## Module layout

* :mod:`brutex.feed.errors` — typed feed exceptions
  (:class:`FeedError`, :class:`LookAheadError`, :class:`ResampleError`).
* :mod:`brutex.feed.resampler` — pure :class:`~brutex.data.models.CandleBar`
  → :class:`~brutex.data.models.CandleBar` OHLCV aggregation for the
  sub-day the broker intervals. Fails loudly on unknown or unaligned
  intervals; no silent up/down-sampling.
* :mod:`brutex.feed.replay` — :class:`ReplayFeed`: heap-based k-way
  merge over :meth:`~brutex.data.catalog.Catalog.get_bars` results so
  a strategy sees multi-symbol events in market order. Tracks
  ``current_ts`` and exposes :meth:`ReplayFeed.ensure_no_lookahead`
  (the runtime half of FR-INT-004's look-ahead guard).

## Scope — v1

Session-naive UTC bucketing only; 1week / 1month targets are out of
scope for the resampler until the ``trading_calendar`` module lands
(P3 — ADR-0014). Wall-clock pacing is supported via an injectable
``pacer`` callback; the default ``None`` means instant (accelerated)
replay, which is the mode every unit test uses.

``polars`` / ``pyarrow`` are imported lazily inside
:class:`ReplayFeed` so the pure-Python :mod:`resampler` and
:mod:`errors` submodules keep participating in the unit-test suite
without the ``[data]`` extras installed.
"""

from __future__ import annotations

from brutex.feed.errors import FeedError, LookAheadError, ResampleError
from brutex.feed.replay import FeedEvent, ReplayFeed
from brutex.feed.resampler import (
    INTERVAL_SECONDS,
    RESAMPLEABLE_INTERVALS,
    resample_bars,
)

__all__ = [
    "INTERVAL_SECONDS",
    "RESAMPLEABLE_INTERVALS",
    "FeedError",
    "FeedEvent",
    "LookAheadError",
    "ReplayFeed",
    "ResampleError",
    "resample_bars",
]
