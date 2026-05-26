"""Indian transaction cost model \u2014 ``FR-EXEC-002`` (P4 kickoff scope).

Applies to **every** fill the simulator produces. The P4 kickoff
implementation covers the dominant charge categories for intraday
equity and F&O trading:

* **Brokerage** \u2014 flat per-order fee (configurable, default \u20b920).
* **STT** \u2014 sell-side only for intraday equity / FUT / OPT. Rates
  per ``docs/REQUIREMENTS.md \u00a712.2``:
  - intraday equity: 0.025% on sell notional
  - FUT: 0.0125% on sell notional
  - OPT: 0.05% on sell **premium** (= notional for options)
* **Exchange turnover charge** \u2014 per segment: equity 0.00297%,
  FUT 0.00173%, OPT 0.03503%. All applied to notional.
* **SEBI turnover fee** \u2014 \u20b910 per crore of notional (0.0001%).
* **GST** \u2014 18% on the sum of ``brokerage + exchange_fee +
  sebi_fee``.

Not yet modelled (lands in follow-up commits alongside the bits
of state they depend on):

* **Stamp duty** \u2014 buy-side only, per-segment rate, needs
  day-1-vs-delivery distinction.
* **DP charges** \u2014 \u20b913.5 + GST per ISIN per day on delivery
  sells, requires EOD ISIN accounting.
* **STT delivery equity** \u2014 0.1% on buy+sell, needs settlement
  tracking.
* **STT option exercise** \u2014 0.125% on intrinsic, needs
  option-exercise state.

A golden test against the Zerodha / Groww calculator pinning
computed costs to \u00b1\u20b90.01 lives under
``tests/golden/test_indian_costs.py`` in a follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from brutex.execution.types import FillCosts
from brutex.models import InstrumentType, Segment
from brutex.strategies.types import Side

# ────────────────────────────────────────────────────────────────────────
# Rate constants
# ────────────────────────────────────────────────────────────────────────
#
# All rates are fractions (0.00025 = 0.025%). Centralising them as
# module-level ``Final`` constants lets golden tests pin expected
# values and makes future regulatory updates a one-line change.

#: Default flat brokerage per executed order (\u20b9).
DEFAULT_BROKERAGE: Final[Decimal] = Decimal("20")

#: STT rate for the sell leg of intraday equity (percentage / 100).
STT_INTRADAY_EQUITY: Final[Decimal] = Decimal("0.00025")  # 0.025%

#: STT rate for the sell leg of a futures contract.
STT_FUTURES: Final[Decimal] = Decimal("0.000125")  # 0.0125%

#: STT rate for the sell leg of an options contract \u2014 applied to
#: the option **premium** (= notional per lot).
STT_OPTIONS: Final[Decimal] = Decimal("0.0005")  # 0.05%

#: Exchange turnover charge for NSE equity (on notional).
EXCH_NSE_EQUITY: Final[Decimal] = Decimal("0.0000297")  # 0.00297%

#: Exchange turnover charge for NSE futures (on notional).
EXCH_NSE_FUTURES: Final[Decimal] = Decimal("0.0000173")  # 0.00173%

#: Exchange turnover charge for NSE options (on premium).
EXCH_NSE_OPTIONS: Final[Decimal] = Decimal("0.0003503")  # 0.03503%

#: SEBI turnover fee rate \u2014 \u20b910 per crore = 0.0001%.
SEBI_TURNOVER_FEE: Final[Decimal] = Decimal("0.000001")  # 0.0001%

#: GST rate applied to (brokerage + exchange + SEBI).
GST_RATE: Final[Decimal] = Decimal("0.18")  # 18%

#: Quantisation exponent used to round every monetary value to
#: paise (\u20b90.01). Applying it once per line item keeps byte-for-
#: byte reproducibility when a cost model is re-run on the same
#: input.
_PAISE: Final[Decimal] = Decimal("0.01")


def _quantise(amount: Decimal) -> Decimal:
    """Round *amount* to the nearest paise (\u20b90.01), half-up.

    The simulator's realism assumption is that a broker quotes in
    paise, not sub-paise. Rounding here keeps the equity curve
    rational and makes the output stable across CPython / PyPy.
    """
    return amount.quantize(_PAISE)


# ────────────────────────────────────────────────────────────────────────
# IndianCostModel
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class IndianCostModel:
    """Compute :class:`FillCosts` for a single fill.

    Stateless \u2014 every call is a pure function of
    ``(side, qty, fill_price, segment, instrument_type)`` plus the
    constants above. Instances are cheap; the simulator creates
    one and reuses it.

    Parameters
    ----------
    brokerage_per_order
        Flat brokerage charged per executed order regardless of
        quantity. Defaults to :data:`DEFAULT_BROKERAGE`.
    """

    brokerage_per_order: Decimal = DEFAULT_BROKERAGE

    def compute(
        self,
        *,
        side: Side,
        qty: int,
        fill_price: Decimal,
        segment: Segment,
        instrument_type: InstrumentType,
    ) -> FillCosts:
        """Return the itemised charges for one fill.

        Parameters
        ----------
        side
            :class:`Side.BUY` or :class:`Side.SELL`. ``HOLD`` is
            not a fillable side \u2014 callers must not pass it.
        qty
            Strictly positive order quantity (pre-lot-size
            validation \u2014 the simulator itself enforces lot
            multiples).
        fill_price
            Post-slippage price the order was executed at. Used
            to compute the notional for every percentage-based
            charge.
        segment
            ``CASH`` or ``FNO``.
        instrument_type
            ``EQ`` / ``IDX`` for cash, ``FUT`` / ``CE`` / ``PE``
            for F&O.
        """
        if side == Side.HOLD:
            raise ValueError("IndianCostModel.compute: HOLD is not a fillable side")
        if qty <= 0:
            raise ValueError(f"IndianCostModel.compute: qty must be positive, got {qty}")
        if fill_price <= 0:
            raise ValueError(
                f"IndianCostModel.compute: fill_price must be positive, got {fill_price}"
            )

        notional = Decimal(qty) * fill_price

        brokerage = _quantise(self.brokerage_per_order)
        stt = _quantise(self._compute_stt(side, notional, segment, instrument_type))
        exchange_fee = _quantise(self._compute_exchange_fee(notional, segment, instrument_type))
        sebi_fee = _quantise(notional * SEBI_TURNOVER_FEE)
        # GST: 18% of (brokerage + exchange + SEBI). Stamp duty
        # and STT are NOT part of the GST base per SEBI schedule.
        gst_base = brokerage + exchange_fee + sebi_fee
        gst = _quantise(gst_base * GST_RATE)

        return FillCosts(
            brokerage=brokerage,
            stt=stt,
            exchange_fee=exchange_fee,
            sebi_fee=sebi_fee,
            stamp_duty=Decimal("0"),  # P4 kickoff: not yet modelled
            gst=gst,
        )

    # ── STT per instrument type (sell-side only) ──────────────────

    @staticmethod
    def _compute_stt(
        side: Side,
        notional: Decimal,
        segment: Segment,
        instrument_type: InstrumentType,
    ) -> Decimal:
        """Return the STT charge for one fill (zero on the buy leg).

        STT is **sell-side only** for intraday equity / FUT / OPT.
        The P4 kickoff assumes every trade is intraday \u2014 delivery
        equity (STT on both sides) lands in a follow-up alongside
        the T+1 settlement state machine.
        """
        if side == Side.BUY:
            return Decimal("0")

        # CASH+EQ: standard intraday-equity STT (kept for forward-compat).
        # CASH+IDX falls through to Decimal("0") below per Track 1
        # signal-only semantics (CLAUDE.md §7: "Brokerage assumption
        # (Track 1) - NONE - spot is not tradable"). Updated in DEC-P0-032.
        if segment == Segment.CASH and instrument_type == InstrumentType.EQ:
            return notional * STT_INTRADAY_EQUITY
        if segment == Segment.FNO and instrument_type == InstrumentType.FUT:
            return notional * STT_FUTURES
        if segment == Segment.FNO and instrument_type in (
            InstrumentType.CE,
            InstrumentType.PE,
        ):
            return notional * STT_OPTIONS

        # Anything else (e.g. COMMODITY, which is out of scope) \u2014
        # explicit zero is safer than a silent fallback.
        return Decimal("0")

    # ── Exchange turnover charge per segment ──────────────────────

    @staticmethod
    def _compute_exchange_fee(
        notional: Decimal,
        segment: Segment,
        instrument_type: InstrumentType,
    ) -> Decimal:
        """Return the NSE exchange turnover charge for one fill."""
        # CASH+EQ: standard cash-equity exchange charge (kept for
        # forward-compat). CASH+IDX falls through to Decimal("0") below
        # per Track 1 signal-only semantics (CLAUDE.md §7). Updated in
        # DEC-P0-032.
        if segment == Segment.CASH and instrument_type == InstrumentType.EQ:
            return notional * EXCH_NSE_EQUITY
        if segment == Segment.FNO and instrument_type == InstrumentType.FUT:
            return notional * EXCH_NSE_FUTURES
        if segment == Segment.FNO and instrument_type in (
            InstrumentType.CE,
            InstrumentType.PE,
        ):
            return notional * EXCH_NSE_OPTIONS
        return Decimal("0")
