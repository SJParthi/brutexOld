"""Execution simulator \u2014 the heart of the backtest engine.

Consumes :class:`~brutex.strategies.Signal` events + bar data and
produces :class:`Fill` records, per-symbol :class:`Position`
snapshots, and a running :class:`EquityPoint` curve. The output of
this class is what :class:`~brutex.execution.driver.BacktestDriver`
assembles into a :class:`BacktestResult`.

## Flow per call to :meth:`execute`

1. **Lot-size validation** \u2014 reject if ``qty % lot_size != 0``
   (``FR-EXEC-004``).
2. **Freeze-quantity validation** \u2014 reject if
   ``qty > freeze_quantity`` (``FR-EXEC-005``).
3. **Slippage** \u2014 compute ``fill_price`` from the current bar's
   close plus the configured bps.
4. **Cost model** \u2014 itemised :class:`FillCosts` via
   :class:`IndianCostModel`.
5. **Cash check** \u2014 raise :class:`InsufficientFundsError` on
   under-funded BUY orders.
6. **Position update** \u2014 open, extend, close, or reverse the
   :class:`Position` for the symbol. Realized P&L accrues on every
   close / reversal via average-cost accounting.
7. **Cash update** \u2014 apply ``fill.cash_delta`` to the running
   cash balance.
8. **Fill record** \u2014 append the :class:`Fill` to the in-memory
   fills list.

``mark_to_market(symbol, price, ts)`` is a separate entry point
that the :class:`BacktestDriver` calls once per bar for the event
symbol. It updates :attr:`Position.unrealized_pnl` in place,
records a new :class:`EquityPoint`, and does not emit fills.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Final

from brutex.data.models import CandleBar
from brutex.execution.costs import IndianCostModel
from brutex.execution.errors import (
    FreezeQuantityExceededError,
    InsufficientFundsError,
    InvalidLotSizeError,
    InvalidPriceError,
)
from brutex.execution.types import (
    EquityPoint,
    Fill,
    InstrumentMeta,
    Position,
)
from brutex.strategies.types import Side, Signal

#: Divisor used to convert basis points to a fraction.
_BPS_DIVISOR: Final[Decimal] = Decimal("10000")

#: Default quantity used when a signal does not carry one. The P4
#: kickoff uses a fixed per-signal qty \u2014 smarter sizing
#: (Kelly, fixed-fraction, vol-targeting) lands in a follow-up.
DEFAULT_ORDER_QTY: Final[int] = 1


class ExecutionSimulator:
    """Turn signals + bars into fills, positions, and an equity curve.

    Parameters
    ----------
    initial_cash
        Starting cash balance in rupees. Must be strictly positive.
    instruments
        Mapping from symbol to :class:`InstrumentMeta`. Every
        symbol a signal may reference must be present; strategies
        that touch an unknown symbol raise at
        :meth:`execute` time.
    cost_model
        Optional :class:`IndianCostModel`. Defaults to
        ``IndianCostModel()`` (flat \u20b920 brokerage).
    slippage_bps
        Bar-VWAP \u00b1 bps slippage. Applied symmetrically: BUY fills
        above the reference price, SELL fills below. Defaults to
        5 bps \u2014 deliberately conservative for the kickoff so
        reported P&L isn't flattered.
    default_qty
        Fixed quantity used for every order. Must be a positive
        multiple of the relevant instrument's lot size. Defaults
        to :data:`DEFAULT_ORDER_QTY`.

    Attributes
    ----------
    cash
        Running cash balance. Starts at *initial_cash*, decrements
        on BUY fills (notional + costs), increments on SELL fills
        (notional \u2212 costs).
    positions
        Dict of symbol \u2192 :class:`Position`. Flat positions are
        kept in the dict with ``qty == 0`` so callers can still
        inspect realized P&L on closed positions.
    fills
        Tuple-view of every recorded :class:`Fill`.
    equity_curve
        Tuple-view of every :class:`EquityPoint` recorded so far.
    """

    def __init__(
        self,
        *,
        initial_cash: Decimal,
        instruments: Mapping[str, InstrumentMeta],
        cost_model: IndianCostModel | None = None,
        slippage_bps: Decimal = Decimal("5"),
        default_qty: int = DEFAULT_ORDER_QTY,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError(f"initial_cash must be positive, got {initial_cash}")
        if slippage_bps < 0:
            raise ValueError(f"slippage_bps must be non-negative, got {slippage_bps}")
        if default_qty <= 0:
            raise ValueError(f"default_qty must be positive, got {default_qty}")

        self._initial_cash = initial_cash
        self._cash = initial_cash
        self._instruments = dict(instruments)
        self._cost_model = cost_model or IndianCostModel()
        self._slippage_bps = slippage_bps
        self._default_qty = default_qty

        self._positions: dict[str, Position] = {}
        self._fills: list[Fill] = []
        self._equity_curve: list[EquityPoint] = []

    # ── Public API ────────────────────────────────────────────────

    @property
    def cash(self) -> Decimal:
        """Return the current cash balance."""
        return self._cash

    @property
    def positions(self) -> Mapping[str, Position]:
        """Return a read-only view of the position book."""
        return self._positions

    @property
    def fills(self) -> tuple[Fill, ...]:
        """Return every :class:`Fill` recorded so far, in emission order."""
        return tuple(self._fills)

    @property
    def equity_curve(self) -> tuple[EquityPoint, ...]:
        """Return every :class:`EquityPoint` recorded so far."""
        return tuple(self._equity_curve)

    def reset(self) -> None:
        """Restore the simulator to its freshly-constructed state.

        Clears every fill, equity point, and open position, and
        restores the cash balance to the ``initial_cash`` passed at
        construction. Enables a deterministic re-run on the same
        instance (``FR-STR-003``): a second
        :meth:`brutex.execution.driver.BacktestDriver.run` over the
        same feed reproduces the first byte-for-byte instead of
        concatenating fills / carrying cash. On a never-used
        simulator this is a no-op-equivalent.
        """
        self._cash = self._initial_cash
        self._positions.clear()
        self._fills.clear()
        self._equity_curve.clear()

    def execute(self, signal: Signal, bar: CandleBar) -> Fill | None:
        """Try to execute *signal* against the current *bar*.

        Returns the resulting :class:`Fill` on success, or ``None``
        when the signal was a ``HOLD`` (and therefore non-fillable).
        Raises on a contract violation \u2014 unknown symbol, invalid
        lot size, freeze quantity exceeded, insufficient funds, or a
        non-tradeable bar price.
        """
        if signal.side == Side.HOLD:
            return None

        self._require_tradeable_price(bar.close, symbol=signal.symbol, timestamp=bar.timestamp)

        meta = self._instruments.get(signal.symbol)
        if meta is None:
            raise InvalidLotSizeError(
                f"unknown symbol {signal.symbol!r}; register it in the "
                f"ExecutionSimulator's instruments map before executing signals"
            )

        qty = self._default_qty
        self._validate_order(qty=qty, meta=meta)

        requested_price = bar.close
        fill_price = self._apply_slippage(requested_price, signal.side)
        costs = self._cost_model.compute(
            side=signal.side,
            qty=qty,
            fill_price=fill_price,
            segment=meta.segment,
            instrument_type=meta.instrument_type,
        )

        fill = Fill(
            symbol=signal.symbol,
            side=signal.side,
            qty=qty,
            requested_price=requested_price,
            fill_price=fill_price,
            timestamp=signal.timestamp,
            costs=costs,
        )

        if signal.side == Side.BUY:
            required_cash = fill.notional + costs.total
            if required_cash > self._cash:
                raise InsufficientFundsError(
                    f"cannot execute BUY {qty} {signal.symbol} @ {fill_price}: "
                    f"required {required_cash}, available {self._cash}"
                )

        self._update_position(fill)
        self._cash += fill.cash_delta
        self._fills.append(fill)
        return fill

    def mark_to_market(
        self,
        *,
        symbol: str,
        price: Decimal,
        timestamp: datetime,
    ) -> EquityPoint:
        """Re-mark the open position for *symbol* and record an equity point.

        Called once per bar by :class:`BacktestDriver` for the
        event's symbol. Updates :attr:`Position.unrealized_pnl` in
        place, then appends and returns a fresh
        :class:`EquityPoint` reflecting the new portfolio value.
        """
        self._require_tradeable_price(price, symbol=symbol, timestamp=timestamp)
        pos = self._positions.get(symbol)
        if pos is not None and pos.qty != 0:
            pos.last_mark_price = price
            # Unrealized P&L: direction * qty * (mark - avg)
            pos.unrealized_pnl = Decimal(pos.qty) * (price - pos.avg_price)
        elif pos is not None:
            pos.last_mark_price = price
            pos.unrealized_pnl = Decimal("0")
        return self._snapshot_equity(timestamp)

    def snapshot(self, timestamp: datetime) -> EquityPoint:
        """Append and return a fresh :class:`EquityPoint` at *timestamp*.

        Useful when the driver wants to sample equity even on bars
        where no position is open for the event symbol. Uses the
        most recently cached mark price for every open position.
        """
        return self._snapshot_equity(timestamp)

    # ── Private ────────────────────────────────────────────────────

    @staticmethod
    def _require_tradeable_price(
        price: Decimal,
        *,
        symbol: str,
        timestamp: datetime,
    ) -> None:
        """Reject a non-finite or non-positive reference *price*.

        A fill / mark-to-market price must be a finite, strictly
        positive :class:`~decimal.Decimal`. ``is_finite()`` is checked
        FIRST so the ``price <= 0`` comparison runs only on a finite
        value — comparing a ``NaN`` ``Decimal`` would otherwise raise
        :class:`decimal.InvalidOperation`. A bad price raises a typed
        :class:`InvalidPriceError`, consistent with the simulator's
        other order-validation contracts.
        """
        if not price.is_finite() or price <= 0:
            raise InvalidPriceError(
                f"non-tradeable price {price} for {symbol} at {timestamp.isoformat()}: "
                f"bar prices must be finite and strictly positive"
            )

    def _validate_order(self, *, qty: int, meta: InstrumentMeta) -> None:
        """Enforce FR-EXEC-004 and FR-EXEC-005 before touching state."""
        if (
            qty <= 0
        ):  # pragma: no cover — defensive; ExecutionSimulator.__init__ rejects default_qty <= 0 (line 120-121) so the only caller execute() cannot pass qty <= 0
            raise InvalidLotSizeError(f"order qty must be positive, got {qty}")
        if qty % meta.lot_size != 0:
            raise InvalidLotSizeError(
                f"order qty {qty} is not a multiple of lot size {meta.lot_size} for {meta.symbol}"
            )
        if meta.freeze_quantity is not None and qty > meta.freeze_quantity:
            raise FreezeQuantityExceededError(
                f"order qty {qty} exceeds freeze quantity {meta.freeze_quantity} for {meta.symbol}"
            )

    def _apply_slippage(self, requested: Decimal, side: Side) -> Decimal:
        """Apply symmetric bps slippage \u2014 BUY pays up, SELL gets cut."""
        factor = self._slippage_bps / _BPS_DIVISOR
        if side == Side.BUY:
            return requested * (Decimal("1") + factor)
        return requested * (Decimal("1") - factor)

    def _update_position(self, fill: Fill) -> None:
        """Apply *fill* to the :class:`Position` for its symbol.

        Handles the four state transitions:

        * **Open from flat** \u2014 fresh long or short; set
          ``qty``, ``avg_price``.
        * **Extend existing same-side** \u2014 weighted-average the
          entry price.
        * **Close / partial-close same-side** \u2014 realized P&L
          accrues on the closed portion.
        * **Reverse** (BUY beyond a short, or SELL beyond a long)
          \u2014 close the existing leg (realizing P&L) then open the
          opposite with the remaining qty at the fill price.
        """
        pos = self._positions.setdefault(fill.symbol, Position(symbol=fill.symbol))
        signed_delta = fill.qty if fill.side == Side.BUY else -fill.qty

        if pos.qty == 0:
            # Fresh entry.
            pos.qty = signed_delta
            pos.avg_price = fill.fill_price
            return

        same_side = (pos.qty > 0 and signed_delta > 0) or (pos.qty < 0 and signed_delta < 0)
        if same_side:
            # Weighted-average into the existing leg.
            old_qty = abs(pos.qty)
            new_qty = old_qty + abs(signed_delta)
            pos.avg_price = (
                pos.avg_price * Decimal(old_qty) + fill.fill_price * Decimal(abs(signed_delta))
            ) / Decimal(new_qty)
            pos.qty += signed_delta
            return

        # Opposite side \u2014 close (possibly reverse).
        closing_qty = min(abs(pos.qty), abs(signed_delta))
        direction = 1 if pos.qty > 0 else -1
        realized = Decimal(closing_qty * direction) * (fill.fill_price - pos.avg_price)
        # Long close realises (fill - avg) * qty; short close
        # realises (avg - fill) * qty. The direction multiplier
        # above handles both cases.
        pos.realized_pnl += realized

        if abs(signed_delta) < abs(pos.qty):
            # Partial close \u2014 avg_price unchanged, qty shrinks.
            pos.qty += signed_delta
            if (
                pos.qty == 0
            ):  # pragma: no cover \u2014 defensive; partial-close branch requires strict |signed_delta| < |pos.qty|, so pos.qty cannot reach 0 here
                pos.avg_price = Decimal("0")  # pragma: no cover \u2014 same rationale
            return

        # Full close or reversal.
        remainder = abs(signed_delta) - abs(pos.qty)
        pos.qty = 0
        pos.avg_price = Decimal("0")
        if remainder > 0:
            # Reversal \u2014 open the opposite leg with the remainder.
            pos.qty = remainder if fill.side == Side.BUY else -remainder
            pos.avg_price = fill.fill_price

    def _snapshot_equity(self, timestamp: datetime) -> EquityPoint:
        """Compute + record an :class:`EquityPoint` for *timestamp*."""
        positions_value = _compute_positions_value(self._positions.values())
        point = EquityPoint(
            timestamp=timestamp,
            cash=self._cash,
            positions_value=positions_value,
            total_equity=self._cash + positions_value,
        )
        self._equity_curve.append(point)
        return point


def _compute_positions_value(positions: Iterable[Position]) -> Decimal:
    """Return the signed mark-to-market value of every open position."""
    total = Decimal("0")
    for pos in positions:
        if pos.qty == 0:
            continue
        total += Decimal(pos.qty) * pos.last_mark_price
    return total
