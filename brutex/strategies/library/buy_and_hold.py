"""BuyAndHold reference strategy.

The simplest possible strategy: emit one ``BUY`` on the first
post-warmup bar per symbol and never exit. Used as a baseline for
sweep orchestration smoke tests and as the canonical example of
"minimum viable :class:`Strategy`" in the documentation.

Single parameter:

``symbol``
    The the broker symbol to trade. Declared in the strategy's
    :class:`StrategyMeta` so the runner's symbol-whitelist filter
    lets exactly this one through.

``BuyAndHold`` reads zero state beyond what the runner itself
maintains \u2014 no ``scratch`` usage, no rolling window. This makes it
the cleanest way to verify the runner's event plumbing end-to-end.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from brutex.feed.replay import FeedEvent
from brutex.strategies.protocol import StrategyMeta
from brutex.strategies.registry import register_strategy
from brutex.strategies.types import Side, Signal, StrategyState


@register_strategy
class BuyAndHold:
    """Emit one BUY on the first in-scope bar, then stay idle forever."""

    META: ClassVar[StrategyMeta] = StrategyMeta(
        strategy_id="buy_and_hold",
        version="0.1.0",
        symbols=("NSE-NIFTY",),
        interval="1minute",
        params_schema={},
        lookback_bars=0,
        description="Baseline: buys the configured symbol on the first bar, holds forever.",
    )

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        """Accept and ignore any params \u2014 BuyAndHold has no tunables.

        The ``params`` argument is kept so the class satisfies the
        :data:`~brutex.strategies.sweep.StrategyFactory` callable
        protocol used by :class:`ComboSweep`; the body intentionally
        does nothing with it.
        """

    def on_event(
        self,
        event: FeedEvent,
        state: StrategyState,
    ) -> Sequence[Signal]:
        """Return a single BUY the first time a bar arrives, nothing after.

        "Already entered" is tracked via a sentinel key in
        :attr:`StrategyState.scratch` so the runner's ``reset()``
        before every replay naturally rearms the trigger.
        """
        if state.scratch.get("entered", False):
            return []
        state.scratch["entered"] = True
        return [
            Signal(
                symbol=event.symbol,
                side=Side.BUY,
                timestamp=event.timestamp,
                reason="buy_and_hold:first_bar",
            )
        ]
