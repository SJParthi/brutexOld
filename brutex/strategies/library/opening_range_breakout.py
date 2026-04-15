"""Session-aware Opening Range Breakout (ORB) strategy.

Classic intraday pattern: the first ``range_bars`` bars of each UTC
day define that day's "opening range" (high + low). For the rest
of the day, a **close above** the range high emits a BUY, a
**close below** the range low emits a SELL. The range is
recomputed at every UTC-date rollover.

## State

- ``state.scratch["day"]`` \u2014 the ``datetime.date`` of the current
  session. Any change re-initialises the range.
- ``state.scratch["bars_this_day"]`` \u2014 counter of bars seen in
  the current session so far.
- ``state.scratch["range_high"]`` \u2014 highest high seen during the
  first ``range_bars`` bars of the current day.
- ``state.scratch["range_low"]`` \u2014 lowest low seen during the same
  window.
- ``state.scratch["position"]`` \u2014 ``"flat"`` | ``"long"`` |
  ``"short"`` so a single close above the range only emits one
  signal per day.

## Why this is FR-MATH-001 compliant

Pure bar-level comparisons and daily min / max. No rolling
indicator, no statistical estimator \u2014 only wall-clock date
grouping and per-day boundary tracking. No named-indicator
formula is re-implemented.

## Scope \u2014 v1

The UTC-date rollover is intentionally **session-naive** \u2014 a
strategy that wants to key off IST market hours (09:15 to 15:30)
can wrap the output or pass a pre-filtered feed. The full
trading-calendar-aware rollover lands with the
:mod:`brutex.feed.calendar` module in P3 (ADR-0014), at which
point this strategy gets a ``calendar: TradingCalendar | None``
parameter.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, ClassVar

from brutex.feed.replay import FeedEvent
from brutex.strategies.errors import StrategyError
from brutex.strategies.protocol import StrategyMeta
from brutex.strategies.registry import register_strategy
from brutex.strategies.types import Side, Signal, StrategyState


@register_strategy
class OpeningRangeBreakout:
    """First ``range_bars`` of each UTC day \u2192 break-above/below emitter."""

    META: ClassVar[StrategyMeta] = StrategyMeta(
        strategy_id="opening_range_breakout",
        version="0.1.0",
        symbols=("NSE-NIFTY",),
        interval="1minute",
        params_schema={"range_bars": 15},
        lookback_bars=0,
        description="Define day's range in first N bars; signal on break outside the range.",
    )

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        """Configure the number of bars that define the opening range.

        Parameters
        ----------
        params
            Mapping with optional ``range_bars`` key. Must be a
            positive int. Defaults to the factory value in
            :attr:`META.params_schema` (``range_bars=15``).
        """
        eff_params = dict(params or {})
        rb = int(eff_params.get("range_bars", self.META.params_schema["range_bars"]))
        if rb <= 0:
            raise StrategyError(f"OpeningRangeBreakout: range_bars must be positive, got {rb}")
        self._range_bars = rb

    @property
    def range_bars(self) -> int:
        """Return the opening-range length in bars."""
        return self._range_bars

    def on_event(
        self,
        event: FeedEvent,
        state: StrategyState,
    ) -> Sequence[Signal]:
        """Grow the per-day range during warmup, then check breakouts.

        All state is keyed by the UTC date of ``event.timestamp``. A
        new date resets the range + position. The runner already
        guarantees bars arrive in chronological order so a simple
        ``!=`` check on the cached date is enough to detect a
        rollover.
        """
        current_day: date = event.timestamp.date()
        cached_day = state.scratch.get("day")

        if cached_day != current_day:
            # New session — reset everything for the day.
            state.scratch["day"] = current_day
            state.scratch["bars_this_day"] = 0
            state.scratch["range_high"] = None
            state.scratch["range_low"] = None
            state.scratch["position"] = "flat"

        bars_this_day = int(state.scratch.get("bars_this_day", 0)) + 1
        state.scratch["bars_this_day"] = bars_this_day

        bar_high = float(event.bar.high)
        bar_low = float(event.bar.low)
        bar_close = float(event.bar.close)

        if bars_this_day <= self._range_bars:
            # Still inside the opening range window — extend the
            # range and stay silent.
            prev_high = state.scratch.get("range_high")
            prev_low = state.scratch.get("range_low")
            state.scratch["range_high"] = (
                bar_high if prev_high is None else max(float(prev_high), bar_high)
            )
            state.scratch["range_low"] = (
                bar_low if prev_low is None else min(float(prev_low), bar_low)
            )
            return []

        # Post-warmup: range is locked. Check for a breakout.
        range_high = state.scratch.get("range_high")
        range_low = state.scratch.get("range_low")
        if range_high is None or range_low is None:  # pragma: no cover \u2014 guarded by counter
            return []

        rh = float(range_high)
        rl = float(range_low)
        position = state.scratch.get("position", "flat")

        if bar_close > rh and position != "long":
            state.scratch["position"] = "long"
            return [
                Signal(
                    symbol=event.symbol,
                    side=Side.BUY,
                    timestamp=event.timestamp,
                    reason=f"orb:close>{rh:.4f}",
                )
            ]
        if bar_close < rl and position != "short":
            state.scratch["position"] = "short"
            return [
                Signal(
                    symbol=event.symbol,
                    side=Side.SELL,
                    timestamp=event.timestamp,
                    reason=f"orb:close<{rl:.4f}",
                )
            ]
        return []
