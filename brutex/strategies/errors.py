"""Typed exceptions for :mod:`brutex.strategies`."""

from __future__ import annotations

from brutex.models import BruteXError


class StrategyError(BruteXError):
    """Base class for every error raised by :mod:`brutex.strategies`."""


class StrategyRegistrationError(StrategyError):
    """Raised when a strategy cannot be registered with the engine.

    Typical triggers (``FR-STR-002``):

    * The strategy class is missing required :class:`StrategyMeta`
      attributes (``symbols``, ``interval``, ``params_schema``,
      ``lookback_bars``).
    * Two strategies share the same ``strategy_id``.
    * ``lookback_bars`` is negative.
    """


class StrategyContractError(StrategyError):
    """Raised when a strategy violates its declared contract at runtime.

    Typical triggers:

    * ``on_event`` returns a non-sequence, or a sequence containing
      anything other than :class:`~brutex.strategies.types.Signal`.
    * A signal's timestamp is strictly after the feed's
      ``current_ts`` (look-ahead — ``FR-INT-004``).
    * A signal references a symbol outside the strategy's declared
      ``symbols`` list.
    """
