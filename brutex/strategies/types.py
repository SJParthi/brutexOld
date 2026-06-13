"""Core dataclasses + enums for the BruteX strategy engine.

Intentionally pure — no ``polars`` / ``pyarrow`` imports — so this
module can participate in the unit-test suite on a fresh checkout
without any Rust toolchain present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Side(StrEnum):
    """Signal direction.

    ``HOLD`` is explicitly modeled (not represented as "no signal")
    because strategies in the vectorised mode need a dense side
    array; a "no signal" is expressed by not emitting a
    :class:`Signal` at all.
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True, slots=True)
class Signal:
    """One trade signal emitted by a :class:`~brutex.strategies.Strategy`.

    Attributes
    ----------
    symbol
        Canonical (vendor) trading symbol the signal applies to. Must be one
        of the strategy's declared :attr:`StrategyMeta.symbols`.
    side
        :class:`Side` — ``BUY``, ``SELL``, or ``HOLD``.
    timestamp
        The bar timestamp the signal was generated on. Must be
        timezone-aware and must not be strictly after the
        :attr:`~brutex.feed.ReplayFeed.current_ts` of the driving
        feed (``FR-INT-004``).
    strength
        Free-form numeric strength in ``[0.0, 1.0]``. The engine does
        not interpret this — it is passed through to sizing /
        execution layers.
    reason
        Short human-readable justification string (e.g. ``"rsi<30"``).
        Used in audit trails and the dashboard trade blotter.
    """

    symbol: str
    side: Side
    timestamp: datetime
    strength: float = 1.0
    reason: str = ""

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError(
                f"Signal.timestamp must be timezone-aware, got naive datetime {self.timestamp!r}"
            )
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"Signal.strength must be in [0.0, 1.0], got {self.strength}")


@dataclass(slots=True)
class StrategyState:
    """Engine-managed mutable state carried across ``on_event`` calls.

    Per ``FR-STR-003``, concrete strategies **must** be stateless
    except for fields they store inside this container. The runner
    resets the state at the start of every replay, which is what
    makes ``replay(A) == replay(A)`` hold.

    Attributes
    ----------
    last_prices
        Per-symbol last-seen close, keyed by symbol. Updated by the
        runner before each ``on_event`` call so strategies don't
        need to re-derive it.
    bar_count
        Total number of bars observed so far. Strategies use this
        to wait out their declared ``lookback_bars`` before emitting
        signals.
    scratch
        Strategy-private scratchpad. The engine treats it as opaque
        — anything a concrete strategy wants to persist across
        events (EMA running totals, flag state, …) lives here. Kept
        as ``dict[str, Any]`` so tests can assert on specific keys.
    """

    last_prices: dict[str, Decimal] = field(default_factory=dict)
    bar_count: int = 0
    scratch: dict[str, Any] = field(default_factory=dict)

    def reset(self) -> None:
        """Restore the state to its just-constructed form.

        Called by :class:`~brutex.strategies.runner.StrategyRunner`
        before every replay so two back-to-back runs over the same
        feed yield bit-identical results.
        """
        self.last_prices.clear()
        self.bar_count = 0
        self.scratch.clear()
