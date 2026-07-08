"""Typed exceptions for :mod:`brutex.execution`."""

from __future__ import annotations

from brutex.models import BruteXError


class ExecutionError(BruteXError):
    """Base class for every error raised by :mod:`brutex.execution`."""


class InvalidLotSizeError(ExecutionError):
    """Raised when an order quantity is not a multiple of the instrument's lot size.

    Implements ``FR-EXEC-004``: F&O contracts trade in fixed lot
    multiples (NIFTY = 25, BANKNIFTY = 15, etc.). An order at any
    other granularity is rejected before the fill is simulated.
    """


class FreezeQuantityExceededError(ExecutionError):
    """Raised when an order quantity exceeds the symbol's freeze limit.

    Implements ``FR-EXEC-005``: NSE publishes a freeze quantity per
    F&O contract that the exchange refuses to match; a strategy
    that tries to cross it is stopped here.
    """


class InsufficientFundsError(ExecutionError):
    """Raised when an account does not have enough cash to cover an order.

    A fill consumer that tracks a cash pool and deducts
    notional + fees at fill time rejects an over-committing
    strategy with this error.
    """


class InvalidPriceError(ExecutionError):
    """Raised when a bar carries a non-tradeable price.

    Guards feed integrity: a fill / mark-to-market reference price
    must be a finite, strictly-positive :class:`~decimal.Decimal`.
    A zero / negative price or a ``NaN`` / ``Infinity`` (vendor
    corruption) is rejected with this typed error instead of
    leaking a cryptic ``ValueError`` /
    :class:`decimal.InvalidOperation` out of a cost computation.
    """


class NonMonotonicBarError(ExecutionError):
    """Raised when a symbol's bar timestamps are not strictly increasing.

    Guards feed integrity: within a single symbol the
    :class:`~brutex.feed.replay.ReplayFeed` must emit bars in
    strictly-ascending timestamp order. A backwards step (out-of-order)
    or an equal timestamp (duplicate bar) is rejected the moment it is
    seen so a corrupt feed cannot silently double-count or replay a
    bar (the sibling resampler guard raises
    :class:`~brutex.feed.errors.ResampleError` for the same
    violation). Equal timestamps ACROSS different symbols are
    legitimate multi-symbol interleave and are allowed.
    """
