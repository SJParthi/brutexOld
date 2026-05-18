"""K-consecutive-up-close momentum strategy.

Trivial bar-comparison momentum: when ``k`` consecutive bars each
close **above** the previous bar's close, emit a BUY; the mirror
condition (``k`` consecutive down-closes) emits a SELL. Otherwise
silent.

## Why this is FR-MATH-001 compliant

Pure bar-level comparison. No rolling mean, no filter, no smoothing
\u2014 nothing that matches a named indicator formula. Only comparisons of
adjacent :class:`~brutex.data.models.CandleBar` closes live here.

## State

- ``state.scratch["prev_close"]`` \u2014 the previous bar's close (or
  ``None`` until the second bar arrives)
- ``state.scratch["up_streak"]`` \u2014 count of consecutive
  bars with close > prev_close
- ``state.scratch["down_streak"]`` \u2014 count of consecutive bars
  with close < prev_close
- ``state.scratch["position"]`` \u2014 ``"flat"`` | ``"long"`` |
  ``"short"`` so a single breakout emits one signal, not
  ``k, k+1, k+2, ...``
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from brutex.feed.replay import FeedEvent
from brutex.strategies.errors import StrategyError
from brutex.strategies.protocol import StrategyMeta
from brutex.strategies.registry import register_strategy
from brutex.strategies.types import Side, Signal, StrategyState


@register_strategy
class ClosePreviousMomentum:
    """Fire on ``k`` consecutive bar-over-bar close movements."""

    META: ClassVar[StrategyMeta] = StrategyMeta(
        strategy_id="close_prev_momentum",
        version="0.1.0",
        symbols=("NSE-NIFTY",),
        interval="1minute",
        params_schema={"k": 3},
        lookback_bars=1,
        description="Buy after K consecutive up-closes; sell after K consecutive down-closes.",
    )

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        """Configure the streak length ``k``.

        Parameters
        ----------
        params
            Mapping with optional ``k`` key. Must be a positive int.
            Defaults to the factory value in
            :attr:`META.params_schema` (``k=3``).
        """
        eff_params = dict(params or {})
        k = int(eff_params.get("k", self.META.params_schema["k"]))
        if k <= 0:
            raise StrategyError(f"ClosePreviousMomentum: k must be positive, got {k}")
        self._k = k

    @property
    def k(self) -> int:
        """Return the streak length configured at construction."""
        return self._k

    def on_event(
        self,
        event: FeedEvent,
        state: StrategyState,
    ) -> Sequence[Signal]:
        """Update the streak counters and emit a signal on a new breakout.

        The first in-scope bar after ``reset()`` initialises
        ``prev_close`` and returns silent. Every subsequent bar
        compares against the previous close and bumps exactly one of
        ``up_streak`` / ``down_streak`` (the other resets to zero).
        A BUY fires when ``up_streak`` first crosses ``k`` from a
        non-long state; a SELL fires when ``down_streak`` first
        crosses ``k`` from a non-short state.
        """
        prev_close = state.scratch.get("prev_close")
        current_close = float(event.bar.close)

        if prev_close is None:
            state.scratch["prev_close"] = current_close
            state.scratch["up_streak"] = 0
            state.scratch["down_streak"] = 0
            state.scratch["position"] = "flat"
            return []

        if current_close > prev_close:
            state.scratch["up_streak"] = int(state.scratch.get("up_streak", 0)) + 1
            state.scratch["down_streak"] = 0
        elif current_close < prev_close:
            state.scratch["down_streak"] = int(state.scratch.get("down_streak", 0)) + 1
            state.scratch["up_streak"] = 0
        else:
            # Flat bar — reset both streaks (neutral).
            state.scratch["up_streak"] = 0
            state.scratch["down_streak"] = 0

        state.scratch["prev_close"] = current_close
        up = int(state.scratch["up_streak"])
        down = int(state.scratch["down_streak"])
        position = state.scratch.get("position", "flat")

        if up >= self._k and position != "long":
            state.scratch["position"] = "long"
            return [
                Signal(
                    symbol=event.symbol,
                    side=Side.BUY,
                    timestamp=event.timestamp,
                    reason=f"momentum:{up}-up-closes",
                )
            ]
        if down >= self._k and position != "short":
            state.scratch["position"] = "short"
            return [
                Signal(
                    symbol=event.symbol,
                    side=Side.SELL,
                    timestamp=event.timestamp,
                    reason=f"momentum:{down}-down-closes",
                )
            ]

        return []
