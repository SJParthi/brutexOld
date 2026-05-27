"""Donchian-channel breakout strategy (N-bar high / low).

Buys when the current close is strictly **greater** than the maximum
close of the previous ``n`` bars (the Donchian upper band), sells
when the current close is strictly **less** than the minimum close of
the previous ``n`` bars (the Donchian lower band). Otherwise
silent.

## Why this is FR-MATH-001 compliant

The strategy uses ``max`` and ``min`` over a ``collections.deque`` \u2014
both are CPython builtins, not indicator formulas. Donchian channels
are structurally a rolling max/min, **not** the named SMA / EMA /
RSI / MACD / BB / ATR family of indicators. No Python reimplementation
of an indicator formula occurs here.

## State

- ``state.scratch["window"]`` \u2014 a ``list[float]`` of the most
  recent ``n`` close prices. List (not ``deque``) for
  pickle-friendliness; we pop-front manually.
- ``state.scratch["position"]`` \u2014 one of ``"flat"``, ``"long"``,
  ``"short"``. Tracked so consecutive breakouts in the same
  direction don't emit redundant signals.
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
class DonchianBreakout:
    """N-bar Donchian breakout \u2014 rolling max/min on closes."""

    META: ClassVar[StrategyMeta] = StrategyMeta(
        strategy_id="donchian_breakout",
        version="0.1.0",
        symbols=("NSE-NIFTY",),
        interval="1minute",
        params_schema={"n": 20},
        lookback_bars=20,
        description="Buy on close > N-bar high, sell on close < N-bar low.",
    )

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        """Configure the rolling window length.

        Parameters
        ----------
        params
            Mapping with optional ``n`` key. Must be a positive int.
            Defaults to the factory value in
            :attr:`META.params_schema` (``n=20``).
        """
        eff_params = dict(params or {})
        n = int(eff_params.get("n", self.META.params_schema["n"]))
        if n <= 0:
            raise StrategyError(f"DonchianBreakout: n must be positive, got {n}")
        self._n = n

    @property
    def n(self) -> int:
        """Return the rolling window length configured at construction."""
        return self._n

    def on_event(
        self,
        event: FeedEvent,
        state: StrategyState,
    ) -> Sequence[Signal]:
        """Push the close into the rolling window and emit a breakout signal.

        The strategy only acts **after** the window is full (i.e.
        ``len(window) == n``). The runner's ``lookback_bars=n`` gate
        already skips warmup bars, but the same-day boundary is
        enforced here too as belt-and-braces.
        """
        window = self._window(state)
        close_float = float(event.bar.close)

        # The current bar's close is compared against the PREVIOUS
        # window contents so the latest bar never trivially sits on
        # its own band.
        signal: Signal | None = None
        if len(window) >= self._n:
            prev_max = max(window)
            prev_min = min(window)
            position = state.scratch.get("position", "flat")

            if close_float > prev_max and position != "long":
                state.scratch["position"] = "long"
                signal = Signal(
                    symbol=event.symbol,
                    side=Side.BUY,
                    timestamp=event.timestamp,
                    reason=f"donchian:close>{prev_max:.4f}",
                )
            elif close_float < prev_min and position != "short":
                state.scratch["position"] = "short"
                signal = Signal(
                    symbol=event.symbol,
                    side=Side.SELL,
                    timestamp=event.timestamp,
                    reason=f"donchian:close<{prev_min:.4f}",
                )

        # Slide the window forward (drop oldest, append newest).
        window.append(close_float)
        if len(window) > self._n:
            del window[0]
        return [signal] if signal is not None else []

    # ── Private ────────────────────────────────────────────────────

    def _window(self, state: StrategyState) -> list[float]:
        """Lazily create the rolling close-price window on first use."""
        window = state.scratch.get("window")
        if window is None:
            window = []
            state.scratch["window"] = window
        # Cast required because state.scratch is Mapping[str, Any];
        # we own the key so the narrowing is safe.
        if not isinstance(window, list):  # pragma: no cover \u2014 defensive
            raise StrategyError(
                "DonchianBreakout: state.scratch['window'] corrupted to non-list "
                f"type {type(window).__name__}"
            )
        return window
