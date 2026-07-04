"""DEPRECATED, DORMANT Indian transaction cost model \u2014 do NOT use for new work.

.. deprecated:: 2026-07-03
    This module is **dead production code with STALE, WRONG rates**. The
    authoritative Track-2 cost model is
    :func:`brutex.costs.calculator.compute_trade_cost` (per DEC-COST-001
    and ``docs/track2/COSTS_VERIFIED.md`` \u2014 the citation-grounded spec).
    Constructing :class:`IndianCostModel` emits a ``DeprecationWarning``.

Why it is stale (vs the shipped ``brutex/costs/`` calculator):

* **STT 0.05% on options** \u2014 not ANY shipped regime. The real,
  date-regime STT is 0.0625% (pre-Oct-2024) / 0.10% (Oct-2024 \u2192
  Mar-2026) / 0.15% (Apr-2026 \u2192) per ``brutex.costs.constants``.
* **Zero stamp duty** \u2014 the real model charges 0.003% buy-side.
* **No IPFT** \u2014 the real model charges NSE 0.0005% both sides (and
  includes it in the GST base).
* **Banker's rounding** (``ROUND_HALF_EVEN``) \u2014 the money law
  (CLAUDE.md \u00a74, DEC-MONEY-2DP-001) is ``ROUND_HALF_UP``.

It survives ONLY because the equally-dormant
``brutex/execution/{simulator,driver}.py`` stack still imports and
self-tests it \u2014 nothing in the live sweep constructs any of the trio
(``engine/trade_sim.py`` is the live signal-only path). Its numeric
behaviour is deliberately FROZEN (dormant = frozen; the stale rates are
documented, not fixed). Deleting the dead
``execution/{costs,simulator,driver}`` trio as a unit is the flagged
follow-up (``execution/types.py`` + ``errors.py`` must stay \u2014 the lean
instrument store imports ``InstrumentMeta``).

Historical scope notes (rates as originally shipped, P4 kickoff):
brokerage flat \u20b920/order; STT sell-side only (intraday equity 0.025%,
FUT 0.0125%, OPT 0.05% on premium); exchange turnover per segment
(equity 0.00297%, FUT 0.00173%, OPT 0.03503%); SEBI \u20b910/crore
(0.0001%); GST 18% on (brokerage + exchange + SEBI). Stamp duty, DP
charges, STT delivery equity and STT option exercise were never
modelled here \u2014 see ``docs/track2/COSTS_VERIFIED.md`` (the retired
``docs/REQUIREMENTS.md \u00a712.2`` this module once cited is a stub per
CLAUDE.md Golden Rule #2).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
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
    """Round *amount* to the nearest paise (\u20b90.01).

    The simulator's realism assumption is that a broker quotes in
    paise, not sub-paise. Rounding here keeps the equity curve
    rational and makes the output stable across CPython / PyPy.

    The rounding mode is pinned EXPLICITLY to ``ROUND_HALF_EVEN`` \u2014
    the current effective default \u2014 so the result no longer depends
    on the ambient process-global ``decimal.getcontext().rounding``
    (a cross-module non-determinism vector). Output is byte-identical
    to the unpinned default (``NFR-DET-001``).
    """
    return amount.quantize(_PAISE, rounding=ROUND_HALF_EVEN)


# ────────────────────────────────────────────────────────────────────────
# IndianCostModel
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class IndianCostModel:
    """DEPRECATED \u2014 compute :class:`FillCosts` for a single fill (STALE rates).

    .. deprecated:: 2026-07-03
        Dormant dead-production-code cost model with stale rates (see the
        module docstring). Use
        :func:`brutex.costs.calculator.compute_trade_cost` \u2014 the
        authoritative model per DEC-COST-001 /
        ``docs/track2/COSTS_VERIFIED.md``. Construction emits a
        ``DeprecationWarning`` (at construction, NOT import, so a plain
        ``import brutex.execution`` \u2014 which re-exports this class \u2014 stays
        silent). Numeric behaviour is frozen.

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

    def __post_init__(self) -> None:
        warnings.warn(
            "IndianCostModel (brutex.execution.costs) is DEPRECATED and dormant: "
            "its rates are STALE (STT 0.05% vs the shipped 0.0625/0.10/0.15% "
            "regimes; zero stamp duty; no IPFT). Use "
            "brutex.costs.calculator.compute_trade_cost \u2014 the authoritative "
            "model per DEC-COST-001 / docs/track2/COSTS_VERIFIED.md.",
            DeprecationWarning,
            stacklevel=2,
        )

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
