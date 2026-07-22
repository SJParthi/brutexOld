"""Options executor / materializer — winning spot signal → priced option leg.

The Track-2 **execution-realism** bridge (``docs/track2/TRACK2_EXECUTION_SIMULATOR.md``
+ ``TRACK2_OPTIONS_SPEC.md`` §6b, DEC-OPTIONS-EXECUTOR-001): a winning Track-1
**spot** signal carries only a direction + an entry/exit time + the underlying
spot at entry. This module *materializes* that abstract signal into a concrete,
**priced** option trade:

1. **Select** the strike via the locked 10-rule pre-trade selector
   (:mod:`brutex.options.strike_rules` — ATM±N offsets, plus the Greeks-gated
   delta-target rules) on the per-underlying strike grid
   (:mod:`brutex.options.strike_grid`).  BULL → buy CE (``long_call``); BEAR →
   buy PE (``long_put``) — option type follows the spot direction strictly, no
   straddles / spreads (DEC-EXEC-009).
2. **Load** the leg's entry + 15:20-forced-exit option bars from the lake
   through an injected :class:`OptionBarSource` seam (so the core is pure +
   100% unit-testable with a fake; the real lake reader is the CLI's job).
3. **Price** the round-trip via the already-locked Track-2 cost calculator
   (:func:`brutex.costs.calculator.compute_trade_cost`): 1-tick slippage per
   side (``TRACK2_OPTIONS_SPEC.md`` §9) around the worst-case anchor — the
   option's own minute-bar ADVERSE EXTREME, the ONLY fill law
   (DEC-FILL-WORST-CASE-ONLY-001; the retired PRECISE bar-OPEN opt-out is
   deleted with its mode) — then STT / exchange / SEBI /
   IPFT / stamp / GST (``COSTS_VERIFIED.md``), gross + **net** P&L in int64
   paisa. No pricing math is re-implemented here — this module is pure
   composition (CLAUDE.md §11 COMMON). The fill law moves fill PRICES only:
   every reject gate (incl. the §7.8 tick floor, judged on the OPENs) is
   mode-independent.

The implied volatility ``iv`` the rolling endpoint stamps per bar is carried
through onto each :class:`MaterializedTrade` (``iv_at_entry``) so downstream
analysis keeps it; Greeks (delta/gamma/theta/vega) are NOT computed here — the
BSM pipeline is a separate Phase-4 deliverable (``TRACK2_OPTIONS_SPEC.md`` §7),
so the **delta-target** rules (8/9/10) require the caller to supply a delta
chain and are NOT run by default (only the 7 offset ATM±N rules are — honest
per Golden Rule #1: we never pretend Greeks exist before they are built).

## BUY-only (DEC-EXEC-008)

Track 2 only ever BUYS options (``long_call`` / ``long_put``) — never writes /
shorts. Max loss = premium paid; no SPAN/ELM margin modelling.

## Determinism + O(1) (Golden Rules #4 / #5)

:func:`materialize_trade` is a pure function: same signal + rule + bar pair →
the same priced trade, bit-for-bit. Its per-trade cost is **O(1)** — a fixed
ALU strike computation, one bar-source probe, and one O(1) cost compute. The
aggregators (:func:`run_rule_over_signals`, :func:`execute_options_sim`) are
**O(N) over the signal count** (and O(rules × signals) for the full sweep) —
that is inherent work over the input, NEVER an O(1)-total claim (forbidden as
hallucination).

## Audit (Golden Rule #8)

:func:`execute_options_sim` appends one audit row per rule result through an
optional injected :class:`AuditAppend` sink (the material action is "an options
sim was run for rule R producing N priced trades"), so the hash-chained audit
trail covers it without coupling the pure core to a concrete writer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Final, Protocol

import structlog

from brutex.calendars.nse import IST
from brutex.data.models import OptionBar
from brutex.money import rupees_to_paisa
from brutex.options.expiry_calendar import next_monthly_expiry, next_weekly_expiry
from brutex.options.lot_size_history import OptionsUnderlying, get_lot_size
from brutex.options.strike_grid import get_strike_step, round_to_atm
from brutex.options.strike_rules import (
    OFFSET_RULES,
    StrikeRule,
    StrikeRuleKind,
    TradeDirection,
    resolve_delta_target_strike,
    resolve_offset_strike,
)

if TYPE_CHECKING:  # pragma: no cover — annotation-only imports
    # Imported lazily for runtime (inside ``materialize_trade``) to break the
    # ``costs.calculator → options.lot_size_history → options.__init__ →
    # options.executor → costs.calculator`` import cycle; here only for typing
    # (every annotation is a string under ``from __future__ import annotations``).
    from brutex.costs.types import (
        Broker,
        GstSplit,
        Instrument,
        OptionType,
        TradeCostBreakdown,
    )

__all__ = [
    "TICK_FLOOR_MULTIPLE",
    "AuditAppend",
    "DeltaChain",
    "DeltaChainProvider",
    "ExpiryKind",
    "MaterializedTrade",
    "OptionBarPair",
    "OptionBarSource",
    "OptionLeg",
    "OptionsRuleResult",
    "RejectReason",
    "RejectedTrade",
    "SpotSignal",
    "SpotTrade",
    "execute_options_sim",
    "materialize_trade",
    "resolve_current_expiry",
    "run_rule_over_signals",
    "select_option_leg",
    "signals_from_spot_trades",
]

_LOG = structlog.get_logger(__name__)

#: Audit action stamped per rule result by :func:`execute_options_sim`.
_AUDIT_ACTION: Final[str] = "OPTIONS_SIM_RULE"

#: Entry-quality floor (``TRACK2_OPTIONS_SPEC.md`` §7.8 / §8): a strike whose
#: entry premium is at or below ``TICK_FLOOR_MULTIPLE × TICK_PAISA`` (₹0.25) is
#: tick-floor-saturated — its quoted price is dominated by the ₹0.05 tick, IV is
#: unstable, and it is EXCLUDED from the entry universe.
TICK_FLOOR_MULTIPLE: Final[int] = 5

#: ``OptionsUnderlying`` → the cost calculator's ``Instrument`` literal. The enum
#: values already equal the literals; the explicit map keeps the type checker
#: happy (a plain ``.value`` would widen to ``str``).
_INSTRUMENT_LITERAL: Final[dict[OptionsUnderlying, Instrument]] = {
    OptionsUnderlying.NIFTY: "NIFTY",
    OptionsUnderlying.BANKNIFTY: "BANKNIFTY",
    OptionsUnderlying.SENSEX: "SENSEX",
}

#: A delta chain: ``[(strike, signed_delta), ...]`` for one option side at signal
#: time (CE delta positive, PE negative). Consumed only by the delta-target rules.
DeltaChain = Sequence[tuple[int, float]]


class ExpiryKind(Enum):
    """Which expiry cycle a trade uses (``TRACK2_OPTIONS_SPEC.md`` §6)."""

    WEEKLY = "weekly"
    MONTHLY = "monthly"


class RejectReason(Enum):
    """Why a candidate option trade was not priced."""

    #: The leg's option bars are absent from the lake at entry/exit — the
    #: "band-drift hole" failure mode (the pinned strike left the fetched ATM±N
    #: window, or the contract is simply not in the lake / illiquid).
    NO_DATA = "no_data"
    #: The entry (or exit) premium is at/below the tradeable tick floor
    #: (:data:`TICK_FLOOR_MULTIPLE` × tick) — excluded per §7.8.
    TICK_FLOOR = "tick_floor"
    #: The signal's entry date precedes the recorded lot-size history
    #: (``brutex.options.lot_size_history`` starts 2021-01-01 while the data
    #: pull floor is 2020-01-01, GROWW_HISTORY_FLOOR) — the trade cannot be
    #: sized honestly, so it is REJECTED per-trade instead of crashing the
    #: whole sim (2020 lot sizes are UNVERIFIED offline; never fabricated).
    LOT_SIZE_UNKNOWN = "lot_size_unknown"
    #: The entry or exit bar carries ``volume == 0`` — a stale/carried price
    #: with zero trades in the minute, so a fill at its open (even with 1-tick
    #: slippage) is unachievable. UNCONDITIONAL (DEC-FILL-WORST-CASE-ONLY-001;
    #: the ``reject_stale_bars`` opt-out is deleted — a fabricated fill can
    #: never be priced, whatever the caller mood).
    STALE_BAR = "stale_bar"
    #: The signal's entry date falls in a cost-regime window whose statutory
    #: rate is UNVERIFIED offline (pre-2024-10-01 ETC — NSE/BSE slab-era
    #: circulars identified but never officially retrieved,
    #: DEC-ETC-REGIME-HISTORY-001 / COSTS_VERIFIED §20 row 1) — the trade
    #: cannot be costed honestly, so it is REJECTED per-trade instead of
    #: charging a fabricated rate or crashing the sim; mirrors
    #: LOT_SIZE_UNKNOWN.
    COST_REGIME_UNVERIFIED = "cost_regime_unverified"


@dataclass(frozen=True, slots=True)
class SpotSignal:
    """One winning Track-1 spot signal, ready to materialize into an option leg.

    Attributes
    ----------
    instrument
        The brute-force underlying (NIFTY / BANKNIFTY / SENSEX).
    direction
        :data:`~brutex.options.strike_rules.TradeDirection.BULL` (→ buy CE) or
        ``BEAR`` (→ buy PE).
    entry_ts, exit_ts
        Entry + forced-exit timestamps, **timezone-aware IST** (the cost
        calculator selects the STT regime + lot-size era from
        ``entry_ts.date()``). ``exit_ts >= entry_ts``.
    spot_at_entry
        The underlying spot price (rupees, ``> 0``) at the entry bar — used to
        locate ATM on the strike grid.
    expiry_date
        The option expiry the trade uses (resolved by the caller via
        :func:`resolve_current_expiry`). Must be ``>= entry_ts.date()``.
    """

    instrument: OptionsUnderlying
    direction: TradeDirection
    entry_ts: datetime
    exit_ts: datetime
    spot_at_entry: Decimal
    expiry_date: date

    def __post_init__(self) -> None:
        if self.entry_ts.tzinfo is None:
            raise ValueError(f"SpotSignal.entry_ts must be timezone-aware, got {self.entry_ts!r}")
        if self.exit_ts.tzinfo is None:
            raise ValueError(f"SpotSignal.exit_ts must be timezone-aware, got {self.exit_ts!r}")
        if self.exit_ts < self.entry_ts:
            raise ValueError(
                f"SpotSignal.exit_ts ({self.exit_ts}) must be >= entry_ts ({self.entry_ts})"
            )
        if self.spot_at_entry <= 0:
            raise ValueError(f"SpotSignal.spot_at_entry must be > 0, got {self.spot_at_entry}")
        if self.expiry_date < self.entry_ts.date():
            raise ValueError(
                f"SpotSignal.expiry_date ({self.expiry_date}) must be >= entry date "
                f"({self.entry_ts.date()})"
            )


@dataclass(frozen=True, slots=True)
class OptionLeg:
    """The concrete option contract a strike rule selected for a signal.

    Attributes
    ----------
    rule_name
        The :class:`~brutex.options.strike_rules.StrikeRule` name that picked it.
    option_type
        ``"CE"`` (BULL) or ``"PE"`` (BEAR).
    side
        ``"long_call"`` / ``"long_put"`` — always BUY (DEC-EXEC-008).
    atm_strike
        The ATM strike on the grid at signal time.
    strike
        The resolved absolute strike (rupees, ``> 0``).
    strike_offset
        Signed grid offset of ``strike`` from ATM in whole steps.
    """

    rule_name: str
    option_type: OptionType
    side: str
    atm_strike: int
    strike: int
    strike_offset: int

    def __post_init__(self) -> None:
        if self.strike <= 0:
            raise ValueError(f"OptionLeg.strike must be > 0, got {self.strike}")


@dataclass(frozen=True, slots=True)
class OptionBarPair:
    """The entry + forced-exit option bars for one leg (same side)."""

    entry_bar: OptionBar
    exit_bar: OptionBar

    def __post_init__(self) -> None:
        if self.entry_bar.side != self.exit_bar.side:
            raise ValueError(
                f"OptionBarPair sides must match, got entry={self.entry_bar.side!r} "
                f"exit={self.exit_bar.side!r}"
            )


@dataclass(frozen=True, slots=True)
class MaterializedTrade:
    """A fully priced option trade — the success outcome of materialization.

    Attributes
    ----------
    leg
        The selected :class:`OptionLeg`.
    expiry_date
        The option expiry used.
    entry_open_paisa, exit_open_paisa
        The entry / exit bar OPEN premiums in int64 paisa (pre-slippage; the
        cost calculator applies the 1-tick slippage internally).
    iv_at_entry
        The implied volatility stamped on the entry bar, or ``None`` if the
        side omitted it.
    breakdown
        The full :class:`~brutex.costs.types.TradeCostBreakdown` (gross / net /
        per-charge, all paisa).
    """

    leg: OptionLeg
    expiry_date: date
    entry_open_paisa: int
    exit_open_paisa: int
    iv_at_entry: Decimal | None
    breakdown: TradeCostBreakdown

    @property
    def net_pnl_paisa(self) -> int:
        """Post-cost P&L in paisa (the deliverable number)."""
        return self.breakdown.net_pnl_paisa

    @property
    def gross_pnl_paisa(self) -> int:
        """Pre-cost P&L in paisa (slippage already baked into the fills)."""
        return self.breakdown.gross_pnl_paisa

    @property
    def total_charges_paisa(self) -> int:
        """Sum of every charge in paisa."""
        return self.breakdown.total_charges_paisa


@dataclass(frozen=True, slots=True)
class RejectedTrade:
    """A candidate that was selected but could not be priced.

    Attributes
    ----------
    leg
        The selected :class:`OptionLeg`.
    reason
        Why it was rejected (:class:`RejectReason`).
    """

    leg: OptionLeg
    reason: RejectReason


class OptionBarSource(Protocol):
    """Read-side seam: load a leg's entry + forced-exit option bars from the lake.

    Returns ``None`` when the leg's bars are not available (band-drift hole /
    not ingested / illiquid) — the trade is then rejected, never faked.
    """

    def load_pair(
        self,
        *,
        instrument: OptionsUnderlying,
        expiry_date: date,
        strike: int,
        option_type: OptionType,
        entry_ts: datetime,
        exit_ts: datetime,
    ) -> OptionBarPair | None:
        """Return the entry/exit :class:`OptionBarPair`, or ``None`` if absent."""
        ...


class AuditAppend(Protocol):
    """The ``append`` slice of the hash-chained audit writer the sim uses."""

    def append(self, *, action: str, subject: str, payload: dict[str, object]) -> object:
        """Append one audit entry; return the writer's result."""
        ...


class SpotTrade(Protocol):
    """A round-trip spot trade — structurally a ``ClosedTrade`` (entry/exit ts)."""

    @property
    def entry_ts(self) -> datetime:
        """Entry timestamp (timezone-aware)."""
        ...

    @property
    def exit_ts(self) -> datetime:
        """Exit timestamp (timezone-aware)."""
        ...


#: A callable returning the delta chain for a signal (delta-target rules only).
DeltaChainProvider = Callable[[SpotSignal], DeltaChain]


def select_option_leg(
    signal: SpotSignal,
    rule: StrikeRule,
    *,
    delta_chain: DeltaChain | None = None,
) -> OptionLeg:
    """Resolve the concrete :class:`OptionLeg` a strike *rule* picks for *signal*.

    Parameters
    ----------
    signal
        The spot signal (its direction fixes CE vs PE; its spot fixes ATM).
    rule
        One of the 10 locked :data:`~brutex.options.strike_rules.ALL_STRIKE_RULES`.
    delta_chain
        Required for a ``DELTA_TARGET`` rule — the ``(strike, delta)`` chain for
        the trade's option side at signal time. Ignored for ``OFFSET`` rules.

    Returns
    -------
    OptionLeg
        The selected leg (option type, side, resolved strike + offset).

    Raises
    ------
    ValueError
        If a ``DELTA_TARGET`` rule is given without a *delta_chain*.
    """
    if signal.direction == TradeDirection.BULL:
        option_type: OptionType = "CE"
        side = "long_call"
    else:
        option_type = "PE"
        side = "long_put"

    step = get_strike_step(signal.instrument)
    atm = round_to_atm(float(signal.spot_at_entry), signal.instrument)

    if rule.kind == StrikeRuleKind.OFFSET:
        strike = resolve_offset_strike(rule, signal.direction, atm, step)
    else:  # DELTA_TARGET
        if delta_chain is None:
            raise ValueError(
                f"strike rule {rule.name!r} is delta-target and needs a delta_chain "
                f"(the Greeks pipeline is Phase 4 — TRACK2_OPTIONS_SPEC §7)"
            )
        strike = resolve_delta_target_strike(rule, signal.direction, list(delta_chain))

    return OptionLeg(
        rule_name=rule.name,
        option_type=option_type,
        side=side,
        atm_strike=atm,
        strike=strike,
        strike_offset=(strike - atm) // step,
    )


# --- exec-rust dispatch helper (slice 7 / P8, DEC-EXEC-RUST-CORE-001) ---
def _materialize_tail_rust(
    signal: SpotSignal,
    rule: StrikeRule,
    leg: OptionLeg,
    pair: OptionBarPair,
    *,
    broker: Broker,
    gst_split: GstSplit,
    n_lots: int,
) -> MaterializedTrade | RejectedTrade | None:
    """The ``BRUTEX_EXEC_RUST`` tail of :func:`materialize_trade`, or ``None``.

    Serves the post-I/O stale/tick/cost tail from the Rust kernel via
    :mod:`brutex.exec_rust` (default-OFF — MIGRATION.md #2215). ``None``
    means "not served": the verbatim Python tail below reproduces the exact
    behaviour (including any raise) from its own natural site — so ANY prep
    or kernel fallback is behaviour-neutral. The stale gate is UNCONDITIONAL
    on both arms (DEC-FILL-WORST-CASE-ONLY-001 — the opt-out knob is
    deleted) and the fills anchor on the fill bars' adverse EXTREMES. A
    served ``trade`` verdict is re-materialized through the EXISTING frozen
    ``TradeInput`` + ``TradeCostBreakdown`` dataclasses —
    ``TradeInput.__post_init__`` re-validates the INPUTS byte-identically
    (extremes bracketing included), while ``TradeCostBreakdown`` defines NO
    ``__post_init__``: breakdown-VALUE integrity is guarded by the Rust-side
    ``ChargeStack::validated()`` construction check + the
    dual-arm/golden/differential parity gates, not a Python-side assert;
    the reject WARNs are emitted HERE with the identical event names /
    keys / cardinality.
    """
    from brutex import exec_rust

    if not exec_rust.exec_rust_enabled():
        return None
    from brutex.costs import constants as cost_constants
    from brutex.costs.types import TradeCostBreakdown, TradeInput

    try:
        # The SAME conversions/resolutions the Python tail performs — gates
        # 1-2 already proved the lot + ETC lookups succeed for this signal,
        # so none of these can raise on a well-formed pair; a malformed bar
        # (the conversion raise class) falls back and re-raises — or
        # stale-rejects first — from the Python tail exactly as today.
        entry_open_paisa = rupees_to_paisa(pair.entry_bar.open)
        exit_open_paisa = rupees_to_paisa(pair.exit_bar.open)
        entry_high_paisa = rupees_to_paisa(pair.entry_bar.high)
        entry_low_paisa = rupees_to_paisa(pair.entry_bar.low)
        exit_high_paisa = rupees_to_paisa(pair.exit_bar.high)
        exit_low_paisa = rupees_to_paisa(pair.exit_bar.low)
        entry_date = signal.entry_ts.date()
        lot_size = get_lot_size(signal.instrument, entry_date)
        instrument_literal = _INSTRUMENT_LITERAL[signal.instrument]
        tail = exec_rust.materialize_option_tail(
            entry_open_paisa,
            exit_open_paisa,
            entry_high_paisa,
            entry_low_paisa,
            exit_high_paisa,
            exit_low_paisa,
            entry_volume=pair.entry_bar.volume,
            exit_volume=pair.exit_bar.volume,
            is_long=leg.side.startswith("long"),
            lot_size=lot_size,
            n_lots=n_lots,
            brokerage_round_trip_paisa=cost_constants.brokerage_paisa(broker) * 2,
            stt_rate=cost_constants.stt_options_rate_bps_x100(entry_date),
            etc_rate=cost_constants.exchange_charge_bps_x100(instrument_literal, entry_date),
            sebi_rate=cost_constants.SEBI_FEE_BPS_X100,
            ipft_rate=cost_constants.ipft_bps_x100(instrument_literal),
            stamp_rate=cost_constants.STAMP_DUTY_OPTIONS_BPS_X100,
            gst_rate=cost_constants.GST_TOTAL_BPS_X100,
        )
    except (ValueError, TypeError, ArithmeticError):
        return None
    if tail is None:
        return None
    kind, stack = tail
    if kind == "stale_bar":
        _LOG.warning(
            "options.reject.stale_bar",
            rule=rule.name,
            instrument=signal.instrument.value,
            strike=leg.strike,
            entry_volume=pair.entry_bar.volume,
            exit_volume=pair.exit_bar.volume,
        )
        return RejectedTrade(leg=leg, reason=RejectReason.STALE_BAR)
    if kind == "tick_floor":
        return RejectedTrade(leg=leg, reason=RejectReason.TICK_FLOOR)
    if kind != "trade" or stack is None:
        # Executor-side BELT: the seam's _verdict_fault already warn-onces
        # + refuses these shapes (review round 2, R3-F3 — the structural
        # loudness law lives INSIDE brutex.exec_rust), so this branch is
        # reachable only via an injected seam; the Python oracle arm serves
        # (a wrong VALUE cannot escape — the Python arm recomputes).
        return None
    # Validation parity: construct the SAME TradeInput the Python tail
    # builds — its __post_init__ re-validates the inputs and raises
    # byte-identically (e.g. n_lots < 1, extremes bracketing); the instance
    # itself is not needed because the kernel already computed the breakdown.
    TradeInput(
        entry_dt_ist=signal.entry_ts,
        exit_dt_ist=signal.exit_ts,
        instrument=instrument_literal,
        option_type=leg.option_type,
        strike=leg.strike * 100,
        expiry_date=signal.expiry_date,
        entry_bar_open_paisa=entry_open_paisa,
        exit_bar_open_paisa=exit_open_paisa,
        side=leg.side,  # type: ignore[arg-type]  # always long_call/long_put (DEC-EXEC-008)
        n_lots=n_lots,
        broker=broker,
        gst_split=gst_split,
        trade_outcome="NORMAL_CLOSE",
        entry_bar_high_paisa=entry_high_paisa,
        entry_bar_low_paisa=entry_low_paisa,
        exit_bar_high_paisa=exit_high_paisa,
        exit_bar_low_paisa=exit_low_paisa,
    )
    breakdown = TradeCostBreakdown(
        n_shares_per_lot=lot_size,
        n_shares=stack.n_shares,
        buy_fill_paisa=stack.buy_fill_paisa,
        sell_fill_paisa=stack.sell_fill_paisa,
        buy_notional_paisa=stack.buy_notional_paisa,
        sell_notional_paisa=stack.sell_notional_paisa,
        brokerage_paisa=stack.brokerage_paisa,
        stt_paisa=stack.stt_paisa,
        exch_paisa=stack.exch_paisa,
        sebi_paisa=stack.sebi_paisa,
        ipft_paisa=stack.ipft_paisa,
        stamp_paisa=stack.stamp_paisa,
        gst_paisa=stack.gst_paisa,
        slippage_paisa=stack.slippage_paisa,
        total_charges_paisa=stack.total_charges_paisa,
        gross_pnl_paisa=stack.gross_pnl_paisa,
        net_pnl_paisa=stack.net_pnl_paisa,
        used_exercise_branch=False,
        n_orders_per_leg=1,
        lot_size_basis="trade_date",
    )
    return MaterializedTrade(
        leg=leg,
        expiry_date=signal.expiry_date,
        entry_open_paisa=entry_open_paisa,
        exit_open_paisa=exit_open_paisa,
        iv_at_entry=pair.entry_bar.iv,
        breakdown=breakdown,
    )


# --- end exec-rust dispatch helper ---


def materialize_trade(
    signal: SpotSignal,
    rule: StrikeRule,
    *,
    source: OptionBarSource,
    broker: Broker = "groww",
    gst_split: GstSplit = "intra_state",
    n_lots: int = 1,
    delta_chain: DeltaChain | None = None,
) -> MaterializedTrade | RejectedTrade:
    """Materialize + price one option trade for *signal* under *rule*.

    Pure given a deterministic *source*: same inputs → same priced trade.
    O(1) per call (strike ALU + one bar-source probe + one O(1) cost compute).

    Parameters
    ----------
    signal
        The winning spot signal to translate.
    rule
        The strike-selection rule to apply.
    source
        The :class:`OptionBarSource` that loads the leg's bars.
    broker, gst_split, n_lots
        Cost-model parameters (defaults match the Track-2 spec: Groww,
        intra-state GST, 1 lot).
    delta_chain
        Required only for delta-target rules.

    Returns
    -------
    MaterializedTrade | RejectedTrade
        The priced trade, or a rejection — checked in the PINNED
        deterministic order: lot_size → cost_regime → no_data →
        stale_bar → tick_floor.
    """
    # Lazy import (breaks the options↔costs cycle — see the TYPE_CHECKING block):
    # by pricing time the costs package is always fully initialised.
    from brutex.costs.calculator import compute_trade_cost
    from brutex.costs.constants import TICK_PAISA, exchange_charge_bps_x100
    from brutex.costs.types import TradeInput, UnverifiedCostRegimeError

    leg = select_option_leg(signal, rule, delta_chain=delta_chain)

    # Lot-size availability guard — checked FIRST (a pure date precondition,
    # no lake I/O): the lot-size history floor is 2021-01-01 while the data
    # pull floor is 2020-01-01 (GROWW_HISTORY_FLOOR), so a 2020-dated signal
    # has no honest lot size. One such signal must reject THIS trade only,
    # never abort the whole sim. The try is NARROW — it wraps ONLY the lot
    # lookup, so a ValueError from any other layer still propagates.
    try:
        get_lot_size(signal.instrument, signal.entry_ts.date())
    except ValueError as exc:
        _LOG.warning(
            "options.reject.lot_size_unknown",
            rule=rule.name,
            instrument=signal.instrument.value,
            entry_date=signal.entry_ts.date().isoformat(),
            error=str(exc),
        )
        return RejectedTrade(leg=leg, reason=RejectReason.LOT_SIZE_UNKNOWN)

    # Cost-regime availability guard (DEC-ETC-REGIME-HISTORY-001) — a pure
    # date precondition checked BEFORE any lake I/O, mirroring the lot-size
    # ordering above: a pre-2024-10-01 entry date lands on the ETC refusal
    # row, so the trade cannot be costed honestly and is rejected per-trade
    # (never priced with a fabricated rate, never a sim crash). The probe
    # duplicates one O(log 2) lookup vs the real one inside
    # compute_trade_cost — negligible, and it saves the wasted lake read for
    # every pre-boundary signal. The except is NARROW: a generic ValueError
    # from any other layer still propagates.
    try:
        exchange_charge_bps_x100(_INSTRUMENT_LITERAL[signal.instrument], signal.entry_ts.date())
    except UnverifiedCostRegimeError as exc:
        _LOG.warning(
            "options.reject.cost_regime_unverified",
            rule=rule.name,
            instrument=signal.instrument.value,
            entry_date=signal.entry_ts.date().isoformat(),
            window_start=exc.window_start.isoformat(),
            verified_from=exc.verified_from.isoformat() if exc.verified_from else None,
            error=str(exc),
        )
        return RejectedTrade(leg=leg, reason=RejectReason.COST_REGIME_UNVERIFIED)

    pair = source.load_pair(
        instrument=signal.instrument,
        expiry_date=signal.expiry_date,
        strike=leg.strike,
        option_type=leg.option_type,
        entry_ts=signal.entry_ts,
        exit_ts=signal.exit_ts,
    )
    if pair is None:
        return RejectedTrade(leg=leg, reason=RejectReason.NO_DATA)

    # --- exec-rust dispatch (slice 7 / P8, DEC-EXEC-RUST-CORE-001) ---
    # Default-OFF (`BRUTEX_EXEC_RUST`): None unless the kernel serves the
    # whole stale/tick/cost tail; the verbatim Python tail below is the
    # permanent oracle arm.
    rust_served = _materialize_tail_rust(
        signal,
        rule,
        leg,
        pair,
        broker=broker,
        gst_split=gst_split,
        n_lots=n_lots,
    )
    if rust_served is not None:
        return rust_served
    # --- end exec-rust dispatch ---

    # Zero-volume staleness guard — UNCONDITIONAL (DEC-FILL-WORST-CASE-ONLY-001,
    # superseding the opt-out knob): a minute bar with volume == 0 carries a
    # stale/carried OHLC — nothing traded, so pricing a fill at its open
    # (even 1-tick-slipped) fabricates an unachievable execution. Checked
    # BEFORE the tick floor: staleness makes the price itself meaningless.
    if pair.entry_bar.volume == 0 or pair.exit_bar.volume == 0:
        _LOG.warning(
            "options.reject.stale_bar",
            rule=rule.name,
            instrument=signal.instrument.value,
            strike=leg.strike,
            entry_volume=pair.entry_bar.volume,
            exit_volume=pair.exit_bar.volume,
        )
        return RejectedTrade(leg=leg, reason=RejectReason.STALE_BAR)

    entry_open_paisa = rupees_to_paisa(pair.entry_bar.open)
    exit_open_paisa = rupees_to_paisa(pair.exit_bar.open)
    # §7.8 entry-quality floor + a defensive sub-tick guard on the exit so a
    # malformed bar can never reach the cost calculator's ``>= 1 tick`` assert.
    if entry_open_paisa <= TICK_FLOOR_MULTIPLE * TICK_PAISA or exit_open_paisa < TICK_PAISA:
        return RejectedTrade(leg=leg, reason=RejectReason.TICK_FLOOR)

    trade_input = TradeInput(
        entry_dt_ist=signal.entry_ts,
        exit_dt_ist=signal.exit_ts,
        instrument=_INSTRUMENT_LITERAL[signal.instrument],
        option_type=leg.option_type,
        strike=leg.strike * 100,
        expiry_date=signal.expiry_date,
        entry_bar_open_paisa=entry_open_paisa,
        exit_bar_open_paisa=exit_open_paisa,
        side=leg.side,  # type: ignore[arg-type]  # always long_call/long_put (DEC-EXEC-008)
        n_lots=n_lots,
        broker=broker,
        gst_split=gst_split,
        trade_outcome="NORMAL_CLOSE",
        # The option's OWN minute-bar extremes — the worst-case adverse
        # anchors, REQUIRED (DEC-FILL-WORST-CASE-ONLY-001; the open fields
        # keep their documented OPEN semantics for the reject gates and the
        # sub-tick-LOW floor lives in ONE place, _apply_slippage). OptionBar
        # guarantees low ≤ open ≤ high, so the ordering validation cannot
        # fire on a real lake bar.
        entry_bar_high_paisa=rupees_to_paisa(pair.entry_bar.high),
        entry_bar_low_paisa=rupees_to_paisa(pair.entry_bar.low),
        exit_bar_high_paisa=rupees_to_paisa(pair.exit_bar.high),
        exit_bar_low_paisa=rupees_to_paisa(pair.exit_bar.low),
    )
    breakdown = compute_trade_cost(trade_input)
    return MaterializedTrade(
        leg=leg,
        expiry_date=signal.expiry_date,
        entry_open_paisa=entry_open_paisa,
        exit_open_paisa=exit_open_paisa,
        iv_at_entry=pair.entry_bar.iv,
        breakdown=breakdown,
    )


@dataclass(frozen=True, slots=True)
class OptionsRuleResult:
    """Aggregated priced outcome of one strike rule over many signals.

    HONESTY — liquidity SELECTION BIAS on accepted-trade P&L (Golden Rule
    #10): a signal whose entry or exit bar is missing from the lake drops
    the WHOLE trade (``NO_DATA``), and exit-bar availability correlates
    with market conditions — an option that went illiquid or stopped
    printing bars into the forced 15:20 exit is disproportionately one in
    adverse conditions. The accepted-trade sums below therefore carry an
    unstated survivorship tilt: the P&L of the trades that COULD be
    priced, not of every signal the strategy would have taken. The
    ``STALE_BAR`` guard (zero-volume bars) makes the accepted set MORE
    honest per trade but concentrates this same selection effect. Read
    ``net_pnl_paisa`` alongside ``n_rejected`` — a high reject share means
    a strongly filtered, potentially optimistic sample.

    ``COST_REGIME_UNVERIFIED`` is DATE-KEYED and SYSTEMATIC — it truncates
    an entire calendar window (every pre-2024-10-01 signal,
    DEC-ETC-REGIME-HISTORY-001), not a random sample; rule comparisons are
    therefore within-verified-window comparisons only.

    Attributes
    ----------
    rule_name
        The strike rule this row aggregates.
    n_signals
        Total signals processed (``= n_accepted + n_rejected``).
    n_accepted
        Signals priced into a :class:`MaterializedTrade`.
    n_rejected_no_data, n_rejected_tick_floor
        Rejections by reason (with the two below, sum ``= n_rejected``).
    gross_pnl_paisa, total_charges_paisa, net_pnl_paisa
        Sums over the accepted trades (int64 paisa).
    n_winners, n_losers
        Accepted trades with net P&L ``> 0`` (winners) vs ``<= 0`` (non-winners
        — a loss or the vanishingly-rare exact-zero scratch, which the non-zero
        cost stack makes unreachable in practice). ``n_winners + n_losers ==
        n_accepted``.
    n_rejected_lot_size_unknown
        Signals whose entry date precedes the recorded lot-size history
        (:data:`RejectReason.LOT_SIZE_UNKNOWN` — e.g. 2020-dated signals
        under the 2021-01-01 lot-history floor). Defaults to 0 for
        back-compat with pre-existing constructors.
    n_rejected_stale_bar
        Signals rejected on a zero-volume entry/exit bar
        (:data:`RejectReason.STALE_BAR`). Defaults to 0 for back-compat.
    n_rejected_cost_regime_unverified
        Signals whose entry date falls in an UNVERIFIED cost-regime
        refusal window (:data:`RejectReason.COST_REGIME_UNVERIFIED` —
        pre-2024-10-01 ETC, DEC-ETC-REGIME-HISTORY-001). Defaults to 0
        for back-compat.

    Raises
    ------
    ValueError
        On a negative count or a count inconsistency.
    """

    rule_name: str
    n_signals: int
    n_accepted: int
    n_rejected_no_data: int
    n_rejected_tick_floor: int
    gross_pnl_paisa: int
    total_charges_paisa: int
    net_pnl_paisa: int
    n_winners: int
    n_losers: int
    n_rejected_lot_size_unknown: int = 0
    n_rejected_stale_bar: int = 0
    n_rejected_cost_regime_unverified: int = 0

    def __post_init__(self) -> None:
        for name in (
            "n_signals",
            "n_accepted",
            "n_rejected_no_data",
            "n_rejected_tick_floor",
            "n_rejected_lot_size_unknown",
            "n_rejected_stale_bar",
            "n_rejected_cost_regime_unverified",
            "n_winners",
            "n_losers",
        ):
            if getattr(self, name) < 0:
                raise ValueError(
                    f"OptionsRuleResult.{name} must be >= 0, got {getattr(self, name)}"
                )
        if self.n_accepted + self.n_rejected != self.n_signals:
            raise ValueError(
                f"OptionsRuleResult: n_accepted ({self.n_accepted}) + n_rejected "
                f"({self.n_rejected}) must equal n_signals ({self.n_signals})"
            )
        if self.n_winners + self.n_losers > self.n_accepted:
            raise ValueError(
                f"OptionsRuleResult: n_winners ({self.n_winners}) + n_losers "
                f"({self.n_losers}) cannot exceed n_accepted ({self.n_accepted})"
            )

    @property
    def n_rejected(self) -> int:
        """Total rejected signals (no-data + tick-floor + lot-unknown +
        stale-bar + cost-regime-unverified)."""
        return (
            self.n_rejected_no_data
            + self.n_rejected_tick_floor
            + self.n_rejected_lot_size_unknown
            + self.n_rejected_stale_bar
            + self.n_rejected_cost_regime_unverified
        )

    @property
    def win_rate(self) -> float:
        """Fraction of accepted trades that were winners (``0.0`` if none accepted)."""
        if self.n_accepted == 0:
            return 0.0
        return self.n_winners / self.n_accepted


def run_rule_over_signals(
    signals: Iterable[SpotSignal],
    rule: StrikeRule,
    *,
    source: OptionBarSource,
    broker: Broker = "groww",
    gst_split: GstSplit = "intra_state",
    n_lots: int = 1,
    delta_chain_provider: DeltaChainProvider | None = None,
) -> OptionsRuleResult:
    """Price every *signal* under *rule* and aggregate into an :class:`OptionsRuleResult`.

    Parameters
    ----------
    signals
        The winning spot signals to replay.
    rule
        The strike rule to apply to each.
    source
        The option-bar source.
    broker, gst_split, n_lots
        Cost-model parameters.
    delta_chain_provider
        Required for a delta-target rule — yields the per-signal delta chain.

    Returns
    -------
    OptionsRuleResult
        The aggregated row.

    Raises
    ------
    ValueError
        If *rule* is delta-target but no *delta_chain_provider* was supplied.
    """
    if rule.kind == StrikeRuleKind.DELTA_TARGET and delta_chain_provider is None:
        raise ValueError(
            f"strike rule {rule.name!r} is delta-target; supply a delta_chain_provider "
            f"(the Greeks pipeline is Phase 4 — TRACK2_OPTIONS_SPEC §7)"
        )

    n_signals = 0
    n_accepted = 0
    n_no_data = 0
    n_tick_floor = 0
    n_lot_unknown = 0
    n_stale_bar = 0
    n_cost_regime = 0
    gross = 0
    charges = 0
    net = 0
    winners = 0
    losers = 0

    for signal in signals:
        n_signals += 1
        chain = delta_chain_provider(signal) if delta_chain_provider is not None else None
        outcome = materialize_trade(
            signal,
            rule,
            source=source,
            broker=broker,
            gst_split=gst_split,
            n_lots=n_lots,
            delta_chain=chain,
        )
        if isinstance(outcome, RejectedTrade):
            if outcome.reason == RejectReason.NO_DATA:
                n_no_data += 1
            elif outcome.reason == RejectReason.LOT_SIZE_UNKNOWN:
                n_lot_unknown += 1
            elif outcome.reason == RejectReason.STALE_BAR:
                n_stale_bar += 1
            elif outcome.reason == RejectReason.COST_REGIME_UNVERIFIED:
                n_cost_regime += 1
            else:
                n_tick_floor += 1
            continue
        n_accepted += 1
        gross += outcome.gross_pnl_paisa
        charges += outcome.total_charges_paisa
        net += outcome.net_pnl_paisa
        # Winner = strictly positive post-cost P&L; everything else (a loss, or
        # the vanishingly-rare exact-zero scratch — unreachable in practice
        # because every trade pays a non-zero brokerage+GST+slippage stack) is a
        # non-winner. Two buckets keep win_rate well-defined and the branch live.
        if outcome.net_pnl_paisa > 0:
            winners += 1
        else:
            losers += 1

    return OptionsRuleResult(
        rule_name=rule.name,
        n_signals=n_signals,
        n_accepted=n_accepted,
        n_rejected_no_data=n_no_data,
        n_rejected_tick_floor=n_tick_floor,
        gross_pnl_paisa=gross,
        total_charges_paisa=charges,
        net_pnl_paisa=net,
        n_winners=winners,
        n_losers=losers,
        n_rejected_lot_size_unknown=n_lot_unknown,
        n_rejected_stale_bar=n_stale_bar,
        n_rejected_cost_regime_unverified=n_cost_regime,
    )


def execute_options_sim(
    signals: Sequence[SpotSignal],
    *,
    source: OptionBarSource,
    rules: Sequence[StrikeRule] = OFFSET_RULES,
    broker: Broker = "groww",
    gst_split: GstSplit = "intra_state",
    n_lots: int = 1,
    delta_chain_provider: DeltaChainProvider | None = None,
    audit: AuditAppend | None = None,
) -> tuple[OptionsRuleResult, ...]:
    """Run the options sim across every *rule* and return one result per rule.

    The default *rules* are the 7 offset ATM±N rules (delta-target rules need
    the Greeks pipeline + a *delta_chain_provider*, so they are opt-in). When an
    *audit* sink is given, one hash-chained row is appended per rule result
    (Golden Rule #8).

    Parameters
    ----------
    signals
        The winning spot signals (one run's replayed trades).
    source
        The option-bar source.
    rules
        Which strike rules to sweep. Defaults to :data:`OFFSET_RULES`.
    broker, gst_split, n_lots
        Cost-model parameters.
    delta_chain_provider
        Required only if *rules* includes a delta-target rule.
    audit
        Optional audit sink; one row per rule result when supplied.

    Returns
    -------
    tuple[OptionsRuleResult, ...]
        One result per rule, in *rules* order.
    """
    results: list[OptionsRuleResult] = []
    for rule in rules:
        result = run_rule_over_signals(
            signals,
            rule,
            source=source,
            broker=broker,
            gst_split=gst_split,
            n_lots=n_lots,
            delta_chain_provider=delta_chain_provider,
        )
        results.append(result)
        if audit is not None:
            audit.append(
                action=_AUDIT_ACTION,
                subject=result.rule_name,
                payload={
                    "n_signals": result.n_signals,
                    "n_accepted": result.n_accepted,
                    "n_rejected": result.n_rejected,
                    "net_pnl_paisa": result.net_pnl_paisa,
                    "n_winners": result.n_winners,
                },
            )
    # ONE aggregate WARN per sim run when any rule rejected trades on an
    # UNVERIFIED cost-regime window (DEC-ETC-REGIME-HISTORY-001) — the
    # operator headline; the per-trade WARNs above remain (mirroring
    # LOT_SIZE_UNKNOWN — WARN-storm bounding is a named follow-up). The
    # window spans are derived from the regime-table DATA, never hardcoded.
    n_unverified = sum(r.n_rejected_cost_regime_unverified for r in results)
    if n_unverified:
        from brutex.costs.constants import etc_unverified_windows  # lazy — cycle-safe

        _LOG.warning(
            "options.sim.unverified_cost_window",
            n_rejected=n_unverified,
            windows={
                exch: [
                    f"[{'history-start' if s == date.min else s.isoformat()} → "
                    f"{e.isoformat() if e else 'open'})"
                    for s, e in spans
                ]
                for exch, spans in etc_unverified_windows().items()
            },
        )
    return tuple(results)


def resolve_current_expiry(
    instrument: OptionsUnderlying,
    on_date: date,
    kind: ExpiryKind,
) -> date:
    """Resolve the expiry a trade entering on *on_date* would use.

    Wraps the per-date historical expiry calendar
    (:mod:`brutex.options.expiry_calendar`). For ``WEEKLY`` on an underlying
    with no live weekly cycle on *on_date* (e.g. BANKNIFTY after 2024-11-13),
    falls back to the monthly expiry (``TRACK2_OPTIONS_SPEC.md`` §6). Holiday
    shifts are NOT applied here (the calendar's stated limitation).

    Parameters
    ----------
    instrument
        The underlying.
    on_date
        The trade's entry date (IST).
    kind
        :data:`ExpiryKind.WEEKLY` or :data:`ExpiryKind.MONTHLY`.

    Returns
    -------
    date
        The resolved expiry date (``>= on_date``).
    """
    if kind == ExpiryKind.WEEKLY:
        weekly = next_weekly_expiry(instrument, on_date)
        if weekly is not None:
            return weekly
        return next_monthly_expiry(instrument, on_date)
    return next_monthly_expiry(instrument, on_date)


def signals_from_spot_trades(
    spot_trades: Iterable[SpotTrade],
    *,
    instrument: OptionsUnderlying,
    direction: TradeDirection,
    spot_open_at: Callable[[datetime], Decimal],
    expiry_kind: ExpiryKind,
) -> list[SpotSignal]:
    """Turn replayed spot round-trips into :class:`SpotSignal` inputs.

    The seam between the Track-1 trade-sim output (``ClosedTrade`` objects, which
    carry only entry/exit timestamps + direction) and this module. Pure: the
    underlying spot at entry is looked up through the injected *spot_open_at*
    callable and the expiry through :func:`resolve_current_expiry`, so it is
    100% testable without a lake.

    Parameters
    ----------
    spot_trades
        The replayed round-trips (anything with ``entry_ts`` / ``exit_ts``).
    instrument
        The underlying every trade is for.
    direction
        The run's trade direction (one direction per winning combo).
    spot_open_at
        ``entry_ts -> spot price (rupees)`` — the underlying value at entry.
    expiry_kind
        Which expiry cycle to trade.

    Returns
    -------
    list[SpotSignal]
        One signal per spot trade, in input order.
    """
    signals: list[SpotSignal] = []
    for trade in spot_trades:
        entry_ist = trade.entry_ts.astimezone(IST)
        exit_ist = trade.exit_ts.astimezone(IST)
        signals.append(
            SpotSignal(
                instrument=instrument,
                direction=direction,
                entry_ts=entry_ist,
                exit_ts=exit_ist,
                spot_at_entry=spot_open_at(trade.entry_ts),
                expiry_date=resolve_current_expiry(instrument, entry_ist.date(), expiry_kind),
            )
        )
    return signals
