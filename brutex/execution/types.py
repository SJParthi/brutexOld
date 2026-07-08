"""Pure dataclasses for the execution surface.

Intentionally heavy-dep-free \u2014 no ``polars`` / ``pyarrow`` /
``lmdb`` imports \u2014 so this module participates in the unit-test
suite on a fresh checkout that hasn't installed the ``[data]``
extras. Every non-trivial validation on construction uses plain
Python, and ``decimal.Decimal`` carries all prices + money amounts
so byte-for-byte reproducibility (``NFR-DET-001``) is preserved.

The consuming ``IndianCostModel`` / ``ExecutionSimulator`` /
``BacktestDriver`` trio was deleted (DEC-EXECUTION-TRIO-DELETE-001);
these dataclasses stay as the shared, dependency-light execution
vocabulary \u2014 the live production consumer is the lean instrument
store (:mod:`brutex.data.instrument_store`), which hands out
``{symbol: InstrumentMeta}`` maps on the hot path.

## Key types

* :class:`InstrumentMeta` \u2014 per-symbol trading metadata needed at
  order time: segment, instrument type, lot size, freeze quantity.
* :class:`FillCosts` \u2014 itemised Indian transaction costs applied
  to one fill.
* :class:`Fill` \u2014 one completed order: side, quantity,
  requested/fill price, timestamp, and the :class:`FillCosts`
  charged on it.
* :class:`Position` \u2014 aggregated long/short exposure for one
  symbol, with realized + unrealized P&L tracking.
* :class:`EquityPoint` \u2014 one sample of the account's equity curve
  at a given timestamp.
* :class:`BacktestResult` \u2014 the frozen summary of one complete
  backtest run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Final

from brutex.models import InstrumentType, Segment
from brutex.strategies.types import Side

#: Reserved zero constant so dataclasses with ``Decimal`` defaults
#: can reference a canonical ``Decimal("0")`` instead of re-creating
#: it on every instance (which is harmless but clutters the source).
_ZERO: Final[Decimal] = Decimal("0")


# ────────────────────────────────────────────────────────────────────────
# InstrumentMeta
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class InstrumentMeta:
    """Per-symbol trading metadata for order sizing and validation.

    Attributes
    ----------
    symbol
        Canonical (vendor) trading symbol, e.g. ``"NSE-NIFTY-02Jan25-24000-CE"``.
    segment
        :class:`~brutex.models.Segment` \u2014 ``CASH`` or ``FNO``.
    instrument_type
        :class:`~brutex.models.InstrumentType` \u2014 ``EQ``, ``IDX``,
        ``FUT``, ``CE``, or ``PE``.
    lot_size
        Trading lot size. Order quantities must be positive integer
        multiples of this value (``FR-EXEC-004``).
    freeze_quantity
        Maximum quantity per order accepted by the exchange, or
        ``None`` if no freeze limit applies (``FR-EXEC-005``).
    """

    symbol: str
    segment: Segment
    instrument_type: InstrumentType
    lot_size: int
    freeze_quantity: int | None = None

    def __post_init__(self) -> None:
        # Indices (IDX) are reference instruments — never directly tradable
        # (brutex/universe.py) — so the vendor master legitimately carries no
        # lot_size for them (parsed as 0; see DEC-INDEX-PARSE-FIX-001). No
        # order is ever sized against an index, so a zero lot is tolerated
        # for IDX only; every tradable type stays strict.
        if self.instrument_type == InstrumentType.IDX:
            if self.lot_size < 0:
                raise ValueError(
                    f"InstrumentMeta.lot_size must not be negative, got {self.lot_size}"
                )
        elif self.lot_size <= 0:
            raise ValueError(f"InstrumentMeta.lot_size must be positive, got {self.lot_size}")
        if self.freeze_quantity is not None and self.freeze_quantity <= 0:
            raise ValueError(
                "InstrumentMeta.freeze_quantity must be positive when set, "
                f"got {self.freeze_quantity}"
            )


# ────────────────────────────────────────────────────────────────────────
# FillCosts
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FillCosts:
    """Itemised Indian transaction costs on a single fill.

    Every field is a :class:`~decimal.Decimal` so round-trip
    equality holds bit-for-bit (``NFR-DET-001``). The authoritative
    rate derivations live in :func:`brutex.costs.calculator.compute_trade_cost`
    (DEC-COST-001; the stale in-package ``IndianCostModel`` was
    deleted per DEC-EXECUTION-TRIO-DELETE-001).

    Attributes
    ----------
    brokerage
        Flat per-order broker fee (or percentage-capped value).
    stt
        Securities Transaction Tax. Only charged on the sell leg
        for intraday equity / futures / options, zero on buys.
    exchange_fee
        NSE / BSE exchange turnover charge.
    sebi_fee
        SEBI turnover fee (\u20b910 per crore on notional).
    stamp_duty
        Buy-side stamp duty. Zero in the P4 kickoff until the
        day-1-vs-delivery distinction is wired up \u2014 kept in the
        dataclass so follow-ups don't need to change
        serialisation.
    gst
        18% GST on ``(brokerage + exchange_fee + sebi_fee)``.
    """

    brokerage: Decimal = _ZERO
    stt: Decimal = _ZERO
    exchange_fee: Decimal = _ZERO
    sebi_fee: Decimal = _ZERO
    stamp_duty: Decimal = _ZERO
    gst: Decimal = _ZERO

    @property
    def total(self) -> Decimal:
        """Return the sum of every charge \u2014 the amount to deduct from cash."""
        return (
            self.brokerage
            + self.stt
            + self.exchange_fee
            + self.sebi_fee
            + self.stamp_duty
            + self.gst
        )


# ────────────────────────────────────────────────────────────────────────
# Fill
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Fill:
    """One completed order.

    Attributes
    ----------
    symbol
        Canonical (vendor) trading symbol the fill is for.
    side
        ``BUY`` or ``SELL``. ``HOLD`` is never recorded as a fill.
    qty
        Strictly positive integer quantity filled.
    requested_price
        Pre-slippage mid / reference price (typically the bar's
        close). Kept so backtest reports can separately attribute
        execution cost to slippage vs costs.
    fill_price
        Post-slippage price the order was actually executed at.
    timestamp
        Wall-clock time of the fill. Matches the bar's timestamp.
    costs
        :class:`FillCosts` charged on this fill.
    """

    symbol: str
    side: Side
    qty: int
    requested_price: Decimal
    fill_price: Decimal
    timestamp: datetime
    costs: FillCosts

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"Fill.qty must be positive, got {self.qty}")
        if self.side not in (Side.BUY, Side.SELL):
            raise ValueError(f"Fill.side must be BUY or SELL, got {self.side}")
        if self.timestamp.tzinfo is None:
            raise ValueError(f"Fill.timestamp must be timezone-aware, got naive {self.timestamp!r}")

    @property
    def notional(self) -> Decimal:
        """Return ``qty * fill_price`` \u2014 the unsigned trade value."""
        return Decimal(self.qty) * self.fill_price

    @property
    def cash_delta(self) -> Decimal:
        """Return the signed change to cash this fill induces.

        A BUY deducts ``notional + costs`` from cash; a SELL adds
        ``notional`` and deducts ``costs``. Consumers apply it to a
        running cash balance.
        """
        if self.side == Side.BUY:
            return -(self.notional + self.costs.total)
        return self.notional - self.costs.total


# ────────────────────────────────────────────────────────────────────────
# Position
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Position:
    """Aggregated exposure for one symbol, with P&L tracking.

    ``qty > 0`` means long, ``qty < 0`` means short, ``qty == 0``
    means flat. Consumers update :attr:`avg_price` on entry
    and additions; :attr:`realized_pnl` on closes and reversals;
    :attr:`unrealized_pnl` on every mark-to-market against the
    latest bar close.

    Attributes
    ----------
    symbol
        Canonical (vendor) trading symbol this position belongs to.
    qty
        Signed integer exposure. ``0`` means the position is flat.
    avg_price
        Weighted-average entry price of the currently-open side.
        Undefined when ``qty == 0`` (kept at ``Decimal("0")``).
    realized_pnl
        Cumulative realized P&L across every close / reversal of
        this position.
    unrealized_pnl
        Most recent mark-to-market value. Updated on every
        mark-to-market pass.
    last_mark_price
        Most recent price used to compute :attr:`unrealized_pnl`.
        Stored so ``BacktestResult`` can reconstruct the final
        snapshot without re-running the feed.
    """

    symbol: str
    qty: int = 0
    avg_price: Decimal = _ZERO
    realized_pnl: Decimal = _ZERO
    unrealized_pnl: Decimal = _ZERO
    last_mark_price: Decimal = _ZERO

    @property
    def is_flat(self) -> bool:
        """Return ``True`` iff no exposure is currently open."""
        return self.qty == 0

    @property
    def direction(self) -> int:
        """Return ``+1`` for long, ``-1`` for short, ``0`` for flat."""
        if self.qty > 0:
            return 1
        if self.qty < 0:
            return -1
        return 0


# ────────────────────────────────────────────────────────────────────────
# EquityPoint
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One sample of the account's equity curve.

    Attributes
    ----------
    timestamp
        Wall-clock time at which the snapshot was taken \u2014 always
        set to the latest mark-to-market bar's timestamp.
    cash
        Current cash balance, after every fill so far.
    positions_value
        Mark-to-market value of all open positions at
        *timestamp*. Computed as
        ``\u03a3 direction * qty * last_mark_price`` for every open
        position. For shorts this is negative; for longs positive.
    total_equity
        ``cash + positions_value``. Used as the equity curve
        y-value in the backtest report.
    """

    timestamp: datetime
    cash: Decimal
    positions_value: Decimal
    total_equity: Decimal


# ────────────────────────────────────────────────────────────────────────
# BacktestResult
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Frozen summary of one complete backtest run.

    Attributes
    ----------
    fills
        Every :class:`Fill` recorded by the run, in emission
        order. Ties at the same timestamp break deterministically
        by signal-emission order from the strategy.
    equity_curve
        One :class:`EquityPoint` per mark-to-market, typically one
        per in-scope bar.
    final_positions
        Every symbol with a non-zero :class:`Position` at the end
        of the run. Flat positions are omitted.
    final_cash
        Cash balance at the end of the run.
    final_equity
        ``final_cash + \u03a3 final_positions_value``. The y-value of
        the last point on the equity curve.
    total_realized_pnl
        Sum of :attr:`Position.realized_pnl` across every symbol,
        including symbols that flattened mid-run.
    total_costs
        Sum of :attr:`FillCosts.total` across every fill.
    total_fills
        Shortcut for ``len(fills)``. Exposed so callers reporting
        on many runs don't have to repeatedly compute it.
    """

    fills: tuple[Fill, ...]
    equity_curve: tuple[EquityPoint, ...]
    final_positions: Mapping[str, Position]
    final_cash: Decimal
    final_equity: Decimal
    total_realized_pnl: Decimal
    total_costs: Decimal
    total_fills: int = field(init=False)

    def __post_init__(self) -> None:
        # total_fills is a derived field \u2014 set via object.__setattr__
        # because the dataclass is frozen.
        object.__setattr__(self, "total_fills", len(self.fills))
