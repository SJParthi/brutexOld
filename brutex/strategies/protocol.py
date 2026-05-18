"""Event-driven strategy protocol + static meta declaration.

Implements the FR-STR-001 (event-driven mode) and FR-STR-002 (static
declaration) halves of the strategy contract. A concrete strategy
subclasses :class:`Strategy` and assigns a :class:`StrategyMeta`
class attribute named ``META`` — the :class:`StrategyRegistry`
refuses to register anything without one.

The vectorised (signal-array) mode from FR-STR-001(a) lands in a
follow-up commit; the event-driven mode is the one the
:class:`~brutex.strategies.runner.StrategyRunner` drives over
:class:`~brutex.feed.ReplayFeed` events.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

from brutex.feed.replay import FeedEvent
from brutex.strategies.types import Signal, StrategyState


@dataclass(frozen=True, slots=True)
class StrategyMeta:
    """Static declaration of a strategy's requirements (``FR-STR-002``).

    Attributes
    ----------
    strategy_id
        Short globally-unique identifier (e.g. ``"ma_crossover"``).
        Used as the first component of the :func:`combo_hash`.
    version
        Semver string. Bumped whenever the strategy's logic changes
        in a way that could alter results. Two runs with different
        versions are allowed to differ without the reproducibility
        gate firing.
    symbols
        Ordered tuple of Dhan trading symbols the strategy consumes. The
        runner uses this to build the :class:`ReplayFeed`'s
        ``symbols`` argument.
    interval
        One of the the Dhan interval names (e.g. ``"1minute"``). The
        runner refuses to drive a feed at a different interval.
    params_schema
        Mapping of parameter name → default value. Used by the
        brute-force orchestrator to enumerate the sweep grid. Values
        here are the factory defaults; the concrete
        ``params`` dict passed to the runner overrides them.
    lookback_bars
        Minimum number of bars the strategy needs to have observed
        before it is allowed to emit signals. The runner guarantees
        ``on_event`` is only called with state.bar_count >=
        lookback_bars when warmed up — strategies can also check
        :attr:`StrategyState.bar_count` themselves.
    description
        Optional human-readable one-liner for the dashboard /
        ``brutex strategies list`` CLI.
    """

    strategy_id: str
    version: str
    symbols: tuple[str, ...]
    interval: str
    params_schema: Mapping[str, Any] = field(default_factory=dict)
    lookback_bars: int = 0
    description: str = ""

    def __post_init__(self) -> None:
        if not self.strategy_id:
            raise ValueError("StrategyMeta.strategy_id must be non-empty")
        if not self.version:
            raise ValueError("StrategyMeta.version must be non-empty")
        if not self.symbols:
            raise ValueError("StrategyMeta.symbols must list at least one symbol")
        if self.lookback_bars < 0:
            raise ValueError(
                f"StrategyMeta.lookback_bars must be non-negative, got {self.lookback_bars}"
            )


@runtime_checkable
class Strategy(Protocol):
    """Event-driven strategy protocol (``FR-STR-001`` mode b).

    Implementations must:

    1. Expose a :class:`StrategyMeta` as the ``META`` class
       attribute. The registry reads it at registration time.
    2. Implement :meth:`on_event` — the single entry point called
       once per bar per symbol by
       :class:`~brutex.strategies.runner.StrategyRunner`. It returns
       zero or more :class:`Signal` instances.
    3. Be stateless except for fields stored in the engine-managed
       :class:`StrategyState` passed to every call.

    Constructors should accept ``(params: Mapping[str, Any])`` and
    store the frozen-dict view as ``self._params``. The runner
    builds one instance per replay and calls ``state.reset()`` and
    potentially a user-defined ``on_start(state)`` hook at the
    start of iteration.
    """

    META: ClassVar[StrategyMeta]

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        """Construct a strategy instance with optional parameter map.

        Parameters
        ----------
        params
            Strategy-specific parameter dict matching ``META.param_schema``,
            or ``None`` to use defaults. Library strategies validate via
            ``META.param_schema`` in their concrete ``__init__``.
        """
        ...

    def on_event(
        self,
        event: FeedEvent,
        state: StrategyState,
    ) -> Sequence[Signal]:
        """Compute zero or more signals for *event* given *state*.

        Parameters
        ----------
        event
            Current :class:`FeedEvent` from the driving feed. Its
            ``event.symbol`` is guaranteed to be in ``META.symbols``;
            the runner filters upstream events that don't match.
        state
            Engine-managed :class:`StrategyState`. Has already been
            mutated by the runner to include the current bar
            (``state.last_prices[event.symbol]`` is updated to the
            bar's close, and ``state.bar_count`` is incremented).

        Returns
        -------
        Sequence[Signal]
            Zero or more signals. Empty sequence means "no action on
            this bar". Every emitted signal must reference a symbol
            in ``META.symbols`` and carry a timestamp ``<=
            event.timestamp``.
        """
        raise NotImplementedError  # Protocol stub — concrete strategies override.
