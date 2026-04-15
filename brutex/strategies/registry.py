"""Strategy registry — static registration + duplicate-id enforcement.

Concrete strategies register themselves via :func:`register_strategy`
(or via a default :class:`StrategyRegistry` for apps that need
isolated scopes, e.g. tests). The registry validates the
:class:`StrategyMeta` declaration at registration time and rejects:

* Duplicate :attr:`StrategyMeta.strategy_id`.
* Strategies missing a ``META`` class attribute.
* ``META`` values with an invalid ``lookback_bars`` or empty
  ``symbols`` list.

This is the enforcement half of ``FR-STR-002``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brutex.strategies.errors import StrategyRegistrationError
from brutex.strategies.protocol import StrategyMeta

if TYPE_CHECKING:  # pragma: no cover
    from brutex.strategies.protocol import Strategy


class StrategyRegistry:
    """In-memory registry mapping ``strategy_id`` → strategy class.

    Each :class:`StrategyRegistry` instance is independent — use
    :data:`default_registry` for the module-wide default scope and
    construct new instances inside tests / apps that want isolation.
    """

    def __init__(self) -> None:
        self._strategies: dict[str, type[Strategy]] = {}

    def register(self, strategy_cls: type[Strategy]) -> type[Strategy]:
        """Add *strategy_cls* to the registry.

        Parameters
        ----------
        strategy_cls
            Class (not instance) implementing the
            :class:`~brutex.strategies.protocol.Strategy` protocol.
            Must have a class-level ``META`` attribute of type
            :class:`StrategyMeta`.

        Returns
        -------
        type[Strategy]
            The same class, so :meth:`register` can be used as a
            decorator: ``@default_registry.register``.

        Raises
        ------
        StrategyRegistrationError
            If ``META`` is missing or another strategy already
            registered the same ``strategy_id``.
        """
        meta = getattr(strategy_cls, "META", None)
        if not isinstance(meta, StrategyMeta):
            raise StrategyRegistrationError(
                f"{strategy_cls.__name__} is missing a StrategyMeta class attribute "
                f"named 'META'. Every strategy must declare its symbols/interval/"
                f"params/lookback statically (FR-STR-002)."
            )
        if meta.strategy_id in self._strategies:
            existing = self._strategies[meta.strategy_id]
            raise StrategyRegistrationError(
                f"strategy_id {meta.strategy_id!r} is already registered "
                f"to {existing.__name__}; cannot re-register "
                f"{strategy_cls.__name__}"
            )
        self._strategies[meta.strategy_id] = strategy_cls
        return strategy_cls

    def get(self, strategy_id: str) -> type[Strategy]:
        """Return the class registered under *strategy_id*.

        Raises
        ------
        StrategyRegistrationError
            If no strategy is registered under *strategy_id*.
        """
        try:
            return self._strategies[strategy_id]
        except KeyError as exc:
            raise StrategyRegistrationError(
                f"no strategy registered under id {strategy_id!r}; "
                f"known ids: {sorted(self._strategies.keys())}"
            ) from exc

    def ids(self) -> tuple[str, ...]:
        """Return every registered ``strategy_id`` in insertion order."""
        return tuple(self._strategies.keys())

    def clear(self) -> None:
        """Drop every registration. Used by tests to reset between cases."""
        self._strategies.clear()


#: Module-wide default registry. ``register_strategy`` writes here.
default_registry: StrategyRegistry = StrategyRegistry()


def register_strategy(strategy_cls: type[Strategy]) -> type[Strategy]:
    """Register *strategy_cls* in the module-wide :data:`default_registry`.

    Usable as a decorator::

        @register_strategy
        class MyStrategy:
            META = StrategyMeta(...)

            def on_event(self, event, state):
                ...
    """
    return default_registry.register(strategy_cls)
