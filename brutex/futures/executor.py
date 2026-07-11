"""Futures translation executor — winning INDEX spot signal → priced futures leg.

DEC-FUTURES-EXEC-TRANSLATION-001 (Phase 5 of the one-click sweep law — R13 / R14 /
R15): after the sweep ranks winners on the PURE spot signal, an INDEX winner's
trades are re-priced on the FRONT-MONTH-AT-TRADE-TIME futures contract — the
nearest UNEXPIRED monthly contract as of each trade's ENTRY timestamp, resolved
from the REAL F&O registry expiry calendar (NEVER a computed last-Thursday
formula). The futures twin of :mod:`brutex.options.executor`, mirroring its
seams (injected bar source / resolver Protocols, reject-and-count discipline,
pinned reject order, hash-chained audit) without touching the options path (R12).

## The law this module implements

* **R13 — annotation-never-reranks:** the pure spot signal row is NEVER mutated;
  the futures leg is a downstream annotation. Nothing here writes to the results
  store or influences ranking.
* **R14 — front-month-at-trade-time:** each trade resolves its OWN contract from
  the registry calendar keyed by its entry IST date; expiry-day trades stay on
  the EXPIRING contract (``expiry >= entry_date`` keeps it live; the 15:20
  forced exit < 15:30 close means no trade straddles expiry); M/M+1 co-trading
  resolves by earliest-expiry-≥-entry-date — deterministic, never
  liquidity-picked. A missing futures bar at the exact entry/exit minute is an
  honest counted ``NO_DATA`` reject — never a nearest-bar substitute.
* **R15 — OPTIMISTIC = PRECISE fill only:** entry/exit fill at the minute bar's
  OPEN ± one tick, adverse per side (never the favorable bar extreme). A
  gap-open beyond an SL/TP fills at the REAL open (the spot sweep already fixed
  the exit timestamp; this module never re-evaluates levels). A volume-0
  PRINTED futures bar is REAL and TRADABLE (the ``STALE_BAR`` reject is OPT-IN
  via ``reject_stale_bars=True``, default OFF). The pessimistic/worst leg is
  DEFERRED — structurally absent, not faked.

## Routing lives ABOVE the executor

:func:`route_execution` classifies an instrument label (INDEX → futures; STOCK →
spot, deferred per the future scope decree; junk → unsupported) so the executor
core stays instrument-agnostic. HONEST reachability (Golden Rule #10): the
``COST_REGIME_UNVERIFIED`` gate is exercised today ONLY via the SYNTHETIC
cited-regime test fixture (a stock-future signal with a monkeypatched lot fed
straight to :func:`materialize_futures_trade`) — NO natural input reaches it:
index futures are cost-free per the verified table and skip the gate, and a
natural stock future has no verifiable lot history so ``LOT_SIZE_UNKNOWN``
fires first. The branch is the DESIGNED forward path, deliberately kept — it
becomes naturally reachable only when a cost-bearing (stock) futures scope
decree + cited RegimeRows land.

## Determinism + O(1) (Golden Rules #4 / #5)

:func:`materialize_futures_trade` is pure given deterministic seams: same signal
+ resolver + source → the same priced trade, bit-for-bit. Per-trade cost is
O(1)-effective after the resolver/source caches warm (one front-month pick —
O(M log M) over the underlying's FULL cached FUT calendar, since the SHARED
picker SORTS and the calendar retains expired months: M ~dozens, bounded,
trade-count-independent, see the honest bound in
:mod:`brutex.futures.contract_resolver` — + two O(1) dict bar lookups); the
MAE/MFE path walk is O(bars-in-trade) (≤ ~375 intraday minutes) — inherent
per-trade work, honestly stated, never an O(1)-total claim.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Final, Protocol

import structlog

from brutex.calendars.nse import IST
from brutex.costs.futures_costs import (
    FuturesSide,
    FuturesTradeInput,
    compute_futures_trade_cost,
    futures_exchange_charge_bps_x100,
    is_cost_free_future_underlying,
)
from brutex.costs.types import Broker, GstSplit, UnverifiedCostRegimeError
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.models import CandleBar
from brutex.money import rupees_to_paisa
from brutex.options.lot_size_history import OptionsUnderlying, get_lot_size
from brutex.options.strike_rules import TradeDirection

__all__ = [
    "STOCK_SPOT_DEFERRAL_NOTE",
    "AuditAppend",
    "ExecutionKind",
    "FrontMonthResolver",
    "FuturesBarSource",
    "FuturesBarWindow",
    "FuturesLeg",
    "FuturesRejectReason",
    "FuturesRuleResult",
    "FuturesSignal",
    "MaterializedFuturesTrade",
    "RejectedFuturesTrade",
    "SpotTrade",
    "execute_futures_sim",
    "futures_lot_size",
    "materialize_futures_trade",
    "route_execution",
    "run_futures_over_signals",
    "select_futures_leg",
    "signals_from_spot_trades",
]

_LOG = structlog.get_logger(__name__)

#: Audit action stamped by :func:`execute_futures_sim` (Golden Rule #8).
_AUDIT_ACTION: Final[str] = "FUTURES_EXEC_SIM"

#: The honest deferral note a STOCK winner's row is annotated with (R13/R14):
#: STOCK strategies execute on SPOT per the execution-translation mapping, and a
#: stock-futures leg additionally needs a scope decree (CLAUDE.md §2 Rule 3 —
#: stocks are REFERENCE_ONLY; R11 "stocks need a future scope decree").
STOCK_SPOT_DEFERRAL_NOTE: Final[str] = (
    "deferred — STOCK strategy executes on SPOT per the execution-translation "
    "mapping (R13/R14, brutex-execution-translation-mapping); a stock leg awaits "
    "the future stock scope decree (R11 / CLAUDE.md §2 Rule 3) — never fabricated"
)


class FuturesRejectReason(Enum):
    """Why a candidate futures trade was not priced (counted, never silent)."""

    #: The signal's entry date has no verifiable derivative lot size (a
    #: 2020-dated signal under the 2021-01-01 recorded-history floor, or a
    #: non-index underlying with no in-repo lot history) — the trade cannot be
    #: sized honestly (R24: futures trade in LOTS), so it is rejected per-trade,
    #: never sized with an invented lot (the options LOT_SIZE_UNKNOWN precedent).
    LOT_SIZE_UNKNOWN = "lot_size_unknown"
    #: A COST-BEARING future whose entry date lands on an UNVERIFIED cost-regime
    #: refusal window (today: EVERY futures-ETC window —
    #: :mod:`brutex.costs.futures_costs`) — the trade cannot be costed honestly
    #: (the DEC-ETC-REGIME-HISTORY-001 precedent). Cost-free INDEX futures never
    #: hit this gate.
    COST_REGIME_UNVERIFIED = "cost_regime_unverified"
    #: No live FUT contract exists for the underlying as of the entry date —
    #: neither the registry calendar nor the lake fallback lists one whose
    #: expiry is ``>= entry_date``. Never fabricated from a formula.
    NO_CONTRACT = "no_contract"
    #: The resolved contract has no bar at the EXACT entry or exit UTC minute —
    #: honest reject, never a nearest-bar substitution (R15).
    NO_DATA = "no_data"
    #: OPT-IN ONLY (``reject_stale_bars=True``): the entry or exit bar printed
    #: with ``volume == 0``. The R14/R15 DEFAULT treats a volume-0 printed
    #: futures bar as REAL and TRADABLE, so this never fires unless explicitly
    #: requested.
    STALE_BAR = "stale_bar"


class ExecutionKind(Enum):
    """How a winner's instrument translates to an execution leg (R13/R14)."""

    #: An INDEX winner — re-priced on its front-month futures contract.
    INDEX_FUTURES = "index_futures"
    #: A STOCK winner — executes on SPOT; gracefully DEFERRED today
    #: (:data:`STOCK_SPOT_DEFERRAL_NOTE`), never routed to futures.
    STOCK_SPOT_DEFERRED = "stock_spot_deferred"
    #: Not a tradable underlying (blank label, or the reference-only India VIX).
    UNSUPPORTED = "unsupported"


#: India VIX label — REFERENCE_ONLY, never tradable (CLAUDE.md §2 Rule 3).
_VIX_LABEL: Final[str] = "INDIAVIX"


def route_execution(instrument_label: str) -> ExecutionKind:
    """Classify a winner's instrument label into its execution-translation kind.

    The automatic instrument-kind rule (R14 — no UI knob): an INDEX label
    (NIFTY / BANKNIFTY / SENSEX) routes to the futures leg; any other real
    underlying is a STOCK — spot per the mapping, DEFERRED today (the future
    scope decree, :data:`STOCK_SPOT_DEFERRAL_NOTE`); a blank label or the
    reference-only India VIX is UNSUPPORTED. Routing lives ABOVE the executor so
    the executor core stays instrument-agnostic.

    Parameters
    ----------
    instrument_label
        The winner row's instrument label (e.g. ``ResultRow.instrument``).

    Returns
    -------
    ExecutionKind
        The translation route. Never raises — an unroutable label is an honest
        :data:`ExecutionKind.UNSUPPORTED`, not a crash.
    """
    label = instrument_label.strip()
    if not label or label.upper() == _VIX_LABEL:
        return ExecutionKind.UNSUPPORTED
    if is_cost_free_future_underlying(label):
        return ExecutionKind.INDEX_FUTURES
    return ExecutionKind.STOCK_SPOT_DEFERRED


def futures_lot_size(instrument: str, on_date: date) -> int:
    """Resolve the date-keyed derivative lot size for *instrument* on *on_date*.

    Delegates to the ONE recorded per-underlying derivative-lot history
    (:mod:`brutex.options.lot_size_history` — its sourced circulars, e.g.
    SEBI/HO/MRD-PoD2/CIR/P/2024/00181, set the UNDERLYING-level index-derivatives
    market lot; reused as the single per-date lot source, CLAUDE.md §11 COMMON).
    The recorded history covers the 3 index underlyings from 2021-01-01; a
    2020-dated signal (the GROWW_HISTORY_FLOOR era) or a non-index underlying
    has NO verifiable lot and raises — the executor converts that into a
    counted ``LOT_SIZE_UNKNOWN`` reject (never an invented lot, Golden Rule #1).

    Parameters
    ----------
    instrument
        The underlying symbol label.
    on_date
        The trade's entry date (IST).

    Returns
    -------
    int
        The lot size in effect on *on_date*.

    Raises
    ------
    ValueError
        For an underlying with no recorded lot history, or a date before the
        recorded history starts.
    """
    try:
        underlying = OptionsUnderlying(instrument)
    except ValueError:
        msg = (
            f"no verifiable futures lot-size history for {instrument!r} — the recorded "
            f"derivative-lot history covers {sorted(u.value for u in OptionsUnderlying)} only; "
            f"refusing to size the trade (Golden Rule #1)"
        )
        raise ValueError(msg) from None
    return get_lot_size(underlying, on_date)


@dataclass(frozen=True, slots=True)
class FuturesSignal:
    """One winning Track-1 spot round-trip, ready to re-price on a futures leg.

    Attributes
    ----------
    instrument
        The underlying symbol label (``"NIFTY"``). The executor core is
        instrument-agnostic — production routing (:func:`route_execution`)
        feeds it index labels only.
    direction
        :data:`~brutex.options.strike_rules.TradeDirection.BULL` (→ long the
        future) or ``BEAR`` (→ short the future).
    entry_ts, exit_ts
        The trade's entry + exit timestamps (timezone-aware; ``exit_ts >=
        entry_ts``) — inherited VERBATIM from the pure spot trade (the sweep
        already applied N+1-open entry, SL/TP and the forced 15:20 exit; R18).
    vix_at_entry, vix_at_exit
        The reference-only India VIX stamps carried through from the spot trade
        (R31) — ``None`` when no VIX bar existed (honest NULL, never zero).
    exit_reason
        Why the spot trade exited (``"signal"`` / ``"sl"`` / ``"tp"`` /
        ``"forced_1520"`` …) — carried onto the trade record (R24).

    Raises
    ------
    ValueError
        On a blank instrument, naive timestamps, ``exit_ts < entry_ts``, or a
        non-finite VIX Decimal.
    """

    instrument: str
    direction: TradeDirection
    entry_ts: datetime
    exit_ts: datetime
    vix_at_entry: Decimal | None = None
    vix_at_exit: Decimal | None = None
    exit_reason: str = "signal"

    def __post_init__(self) -> None:
        if not self.instrument.strip():
            raise ValueError("FuturesSignal.instrument must be a non-blank underlying label")
        if self.entry_ts.tzinfo is None:
            raise ValueError(f"FuturesSignal.entry_ts must be timezone-aware: {self.entry_ts!r}")
        if self.exit_ts.tzinfo is None:
            raise ValueError(f"FuturesSignal.exit_ts must be timezone-aware: {self.exit_ts!r}")
        if self.exit_ts < self.entry_ts:
            raise ValueError(
                f"FuturesSignal.exit_ts ({self.exit_ts}) must be >= entry_ts ({self.entry_ts})"
            )
        for name, value in (("vix_at_entry", self.vix_at_entry), ("vix_at_exit", self.vix_at_exit)):
            if value is not None and not value.is_finite():
                raise ValueError(
                    f"FuturesSignal.{name} must be a finite Decimal or None: {value!r}"
                )
        if not self.exit_reason.strip():
            raise ValueError("FuturesSignal.exit_reason must be non-blank")


@dataclass(frozen=True, slots=True)
class FuturesLeg:
    """The concrete futures contract a signal resolved to (front-month-at-entry).

    Attributes
    ----------
    instrument
        The underlying symbol label.
    direction
        The spot direction the leg follows.
    side
        ``"long"`` (BULL) or ``"short"`` (BEAR).
    contract_symbol
        The resolved contract ``groww_symbol`` (``NSE-NIFTY-30Sep25-FUT``) —
        the lake partition key, carried VERBATIM from the registry / lake
        discovery (never synthesized from a formula).
    expiry_date
        The contract's real (holiday-shifted, vendor-listed) expiry — decoded
        verbatim from the symbol's ``DDMmmYY`` token.
    """

    instrument: str
    direction: TradeDirection
    side: FuturesSide
    contract_symbol: str
    expiry_date: date

    def __post_init__(self) -> None:
        if not self.contract_symbol.strip():
            raise ValueError("FuturesLeg.contract_symbol must be non-blank")


@dataclass(frozen=True, slots=True)
class FuturesBarWindow:
    """The resolved contract's bars for one trade: exact entry + exit + the path.

    ``path`` is every PRINTED 1-minute bar of the contract in
    ``[entry_ts, exit_ts]`` inclusive, ascending; minutes the contract did not
    trade are honestly absent (never interpolated). ``entry_bar`` / ``exit_bar``
    are the exact-minute endpoints and must be the path's first / last bars.

    Raises
    ------
    ValueError
        On an empty path, endpoint/path mismatch, or a non-ascending path.
    """

    entry_bar: CandleBar
    exit_bar: CandleBar
    path: tuple[CandleBar, ...]

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("FuturesBarWindow.path must be non-empty")
        if self.path[0].timestamp != self.entry_bar.timestamp:
            raise ValueError("FuturesBarWindow.path[0] must be the entry bar")
        if self.path[-1].timestamp != self.exit_bar.timestamp:
            raise ValueError("FuturesBarWindow.path[-1] must be the exit bar")
        for prev, cur in zip(self.path, self.path[1:], strict=False):
            if cur.timestamp <= prev.timestamp:
                raise ValueError(
                    f"FuturesBarWindow.path must be strictly ascending: "
                    f"{prev.timestamp} then {cur.timestamp}"
                )


@dataclass(frozen=True, slots=True)
class MaterializedFuturesTrade:
    """A fully priced futures trade — the R24 every-field-visible record.

    Money fields are int64 paisa. ``mae_paisa`` / ``mfe_paisa`` are NOTIONAL
    excursions (per-unit excursion × lot_size × n_lots, ``>= 0``) measured
    direction-aware against the ENTRY FILL over the contract's own 1-minute
    path (R22) — annotation-only, never a ranking input.

    Raises
    ------
    ValueError
        On a violated sizing/consistency invariant (``net != gross − charges``
        is unrepresentable).
    """

    leg: FuturesLeg
    entry_ts: datetime
    exit_ts: datetime
    entry_open_paisa: int
    exit_open_paisa: int
    buy_fill_paisa: int
    sell_fill_paisa: int
    lot_size: int
    n_lots: int
    gross_pnl_paisa: int
    total_charges_paisa: int
    net_pnl_paisa: int
    mae_paisa: int
    mfe_paisa: int
    vix_at_entry: Decimal | None
    vix_at_exit: Decimal | None
    cost_free: bool
    exit_reason: str

    def __post_init__(self) -> None:
        if self.lot_size < 1 or self.n_lots < 1:
            raise ValueError(
                f"MaterializedFuturesTrade: lot_size {self.lot_size} and n_lots "
                f"{self.n_lots} must be >= 1"
            )
        if self.mae_paisa < 0 or self.mfe_paisa < 0:
            raise ValueError(
                f"MaterializedFuturesTrade: mae_paisa {self.mae_paisa} and mfe_paisa "
                f"{self.mfe_paisa} must be >= 0"
            )
        if self.net_pnl_paisa != self.gross_pnl_paisa - self.total_charges_paisa:
            raise ValueError(
                f"MaterializedFuturesTrade: net {self.net_pnl_paisa} != gross "
                f"{self.gross_pnl_paisa} - charges {self.total_charges_paisa}"
            )


@dataclass(frozen=True, slots=True)
class RejectedFuturesTrade:
    """A signal that could not be priced — a NAMED, counted reject (R24/R28).

    ``leg`` is ``None`` for the pre-resolution rejects (lot-size / cost-regime
    fire before any contract exists; ``NO_CONTRACT`` means no leg could be
    built) and carries the resolved leg for the post-resolution ones
    (``NO_DATA`` / ``STALE_BAR``).
    """

    leg: FuturesLeg | None
    reason: FuturesRejectReason


class FuturesBarSource(Protocol):
    """Read-side seam: load a contract's entry/exit bars + minute path from the lake.

    Returns ``None`` when the contract has no bar at the EXACT entry or exit
    minute — the trade is then rejected ``NO_DATA``, never faked (R15: no
    nearest-bar substitution).
    """

    def load_window(
        self, *, contract_symbol: str, entry_ts: datetime, exit_ts: datetime
    ) -> FuturesBarWindow | None:
        """Return the trade's :class:`FuturesBarWindow`, or ``None`` if absent."""
        ...


class FrontMonthResolver(Protocol):
    """Resolution seam: the front-month FUT contract symbol as of a date.

    Returns the contract ``groww_symbol`` whose expiry is the EARLIEST
    ``>= on_date`` (deterministic; ties break on the symbol string), or ``None``
    when no live contract exists — the trade is then rejected ``NO_CONTRACT``.
    """

    def resolve(self, *, instrument: str, on_date: date) -> str | None:
        """Return the front-month contract symbol, or ``None``."""
        ...


class AuditAppend(Protocol):
    """The ``append`` slice of the hash-chained audit writer (Golden Rule #8)."""

    def append(self, *, action: str, subject: str, payload: dict[str, object]) -> object:
        """Append one audit entry; return the writer's result."""
        ...


class SpotTrade(Protocol):
    """A round-trip spot trade — structurally a ``ClosedTrade`` (R12: NOT
    imported from the options executor; the futures executor owns its seam)."""

    @property
    def entry_ts(self) -> datetime:
        """Entry timestamp (timezone-aware)."""
        ...

    @property
    def exit_ts(self) -> datetime:
        """Exit timestamp (timezone-aware)."""
        ...

    @property
    def vix_at_entry(self) -> Decimal | None:
        """India VIX at entry, or ``None`` (reference-only, R31)."""
        ...

    @property
    def vix_at_exit(self) -> Decimal | None:
        """India VIX at exit, or ``None`` (reference-only, R31)."""
        ...


def signals_from_spot_trades(
    spot_trades: Iterable[SpotTrade],
    *,
    instrument: str,
    direction: TradeDirection,
) -> list[FuturesSignal]:
    """Lift replayed spot round-trips into :class:`FuturesSignal` inputs.

    The seam between the Track-1 trade-sim output
    (:class:`~brutex.metrics.types.ClosedTrade`) and this module. Timestamps are
    normalised to IST; the VIX stamps pass through verbatim (R31). A trade
    object that additionally carries a non-blank string ``exit_reason``
    attribute passes it through (R24); ``ClosedTrade`` does not carry one, so
    those default to ``"signal"`` — an honest generic label, never a fabricated
    specific reason.

    Parameters
    ----------
    spot_trades
        The replayed round-trips.
    instrument
        The underlying every trade is for.
    direction
        The run's trade direction (one direction per winning combo).

    Returns
    -------
    list[FuturesSignal]
        One signal per spot trade, in input order.
    """
    signals: list[FuturesSignal] = []
    for trade in spot_trades:
        raw_reason = getattr(trade, "exit_reason", None)
        exit_reason = raw_reason if isinstance(raw_reason, str) and raw_reason.strip() else "signal"
        signals.append(
            FuturesSignal(
                instrument=instrument,
                direction=direction,
                entry_ts=trade.entry_ts.astimezone(IST),
                exit_ts=trade.exit_ts.astimezone(IST),
                vix_at_entry=trade.vix_at_entry,
                vix_at_exit=trade.vix_at_exit,
                exit_reason=exit_reason,
            )
        )
    return signals


def select_futures_leg(
    signal: FuturesSignal, *, resolver: FrontMonthResolver
) -> FuturesLeg | RejectedFuturesTrade:
    """Resolve the front-month-at-entry :class:`FuturesLeg` for *signal*.

    The contract is the resolver's earliest-expiry-``>=``-entry-IST-date pick
    (R14 — registry-calendar-driven, deterministic, never liquidity-picked; the
    ``>=`` keeps an expiry-day trade on the EXPIRING contract through its
    session). The leg's ``expiry_date`` is decoded VERBATIM from the resolved
    symbol's ``DDMmmYY`` token (the vendor's real, holiday-shifted date) via the
    canonical :func:`~brutex.data.fno_symbol.parse_fno_symbol` decoder.

    Parameters
    ----------
    signal
        The spot signal to translate.
    resolver
        The :class:`FrontMonthResolver` seam.

    Returns
    -------
    FuturesLeg | RejectedFuturesTrade
        The resolved leg, or an honest ``NO_CONTRACT`` reject when the resolver
        returns ``None`` OR returns a symbol that does not decode as a FUT
        contract (never a fabricated expiry).
    """
    entry_date = signal.entry_ts.astimezone(IST).date()
    symbol = resolver.resolve(instrument=signal.instrument, on_date=entry_date)
    if symbol is None:
        return RejectedFuturesTrade(leg=None, reason=FuturesRejectReason.NO_CONTRACT)
    parts = parse_fno_symbol(symbol)
    if parts is None or parts.instrument_type != "FUT":
        _LOG.warning(
            "futures.reject.unparseable_contract",
            instrument=signal.instrument,
            contract_symbol=repr(symbol),
            entry_date=entry_date.isoformat(),
        )
        return RejectedFuturesTrade(leg=None, reason=FuturesRejectReason.NO_CONTRACT)
    side: FuturesSide = "long" if signal.direction == TradeDirection.BULL else "short"
    return FuturesLeg(
        instrument=signal.instrument,
        direction=signal.direction,
        side=side,
        contract_symbol=symbol,
        expiry_date=parts.expiry,
    )


def _excursions_paisa(
    window: FuturesBarWindow, *, side: FuturesSide, entry_fill_paisa: int, n_shares: int
) -> tuple[int, int]:
    """Direction-aware notional (MAE, MFE) over the contract's printed minute path.

    R22: excursions come from the resolved contract's OWN 1-minute path across
    ``[entry_ts, exit_ts]`` — never from the spot series and never across a roll
    seam (the window is a single contract by construction). Measured against the
    ENTRY FILL; per-unit excursions × ``n_shares``; floored at 0. O(bars-in-trade).
    """
    mae_unit = 0
    mfe_unit = 0
    for bar in window.path:
        high_p = rupees_to_paisa(bar.high)
        low_p = rupees_to_paisa(bar.low)
        if side == "long":
            favorable = high_p - entry_fill_paisa
            adverse = entry_fill_paisa - low_p
        else:
            favorable = entry_fill_paisa - low_p
            adverse = high_p - entry_fill_paisa
        mfe_unit = max(mfe_unit, favorable)
        mae_unit = max(mae_unit, adverse)
    return mae_unit * n_shares, mfe_unit * n_shares


# --- exec-rust dispatch helper (slice 7 / P8, DEC-EXEC-RUST-CORE-001) ---
def _materialize_tail_rust(
    signal: FuturesSignal,
    leg: FuturesLeg,
    window: FuturesBarWindow,
    lot_size: int,
    *,
    broker: Broker,
    gst_split: GstSplit,
    n_lots: int,
    reject_stale_bars: bool,
) -> MaterializedFuturesTrade | RejectedFuturesTrade | None:
    """The ``BRUTEX_EXEC_RUST`` tail of :func:`materialize_futures_trade`.

    ``None`` means "not served": the verbatim Python tail below reproduces
    the exact behaviour (including any raise) from its own natural site —
    so ANY prep or kernel fallback is behaviour-neutral (default-OFF —
    MIGRATION.md #1724). A served ``trade`` verdict is re-materialized
    through the EXISTING frozen ``FuturesTradeInput`` +
    ``MaterializedFuturesTrade`` dataclasses, so every validation the
    Python arm runs (intraday-only R18, dead-contract, ≥-one-tick opens,
    ``net == gross − charges``) still runs; the stale-reject WARN is
    emitted HERE with identical event name / keys / cardinality.
    """
    from brutex import exec_rust

    if not exec_rust.exec_rust_enabled():
        return None
    from brutex.costs.constants import (
        GROWW_BROKERAGE_PAISA,
        GST_TOTAL_BPS_X100,
        SEBI_FEE_BPS_X100,
        ZERODHA_BROKERAGE_PAISA,
    )
    from brutex.costs.futures_costs import (
        GROWW_FUTURES_BROKERAGE_TURNOVER_BPS_X100,
        ipft_futures_bps_x100,
        stamp_futures_rate_bps_x100,
        stt_futures_rate_bps_x100,
    )

    try:
        entry_ist = signal.entry_ts.astimezone(IST)
        entry_date = entry_ist.date()
        entry_open_paisa = rupees_to_paisa(window.entry_bar.open)
        exit_open_paisa = rupees_to_paisa(window.exit_bar.open)
        path_highs = [rupees_to_paisa(bar.high) for bar in window.path]
        path_lows = [rupees_to_paisa(bar.low) for bar in window.path]
        # COST SCOPE first, then rate resolution in the PINNED refusal order
        # STT → ETC → NSE-IPFT → stamp — a refusal raise here falls back and
        # re-raises from the Python tail's compute exactly as today (gate 2
        # already proved the ETC probe for a cost-bearing signal).
        cost_free = is_cost_free_future_underlying(signal.instrument)
        if cost_free:
            stt_rate = etc_rate = ipft_rate = stamp_rate = 0
        else:
            stt_rate = stt_futures_rate_bps_x100(entry_date)
            etc_rate = futures_exchange_charge_bps_x100(signal.instrument, entry_date)
            ipft_rate = ipft_futures_bps_x100(signal.instrument, entry_date)
            stamp_rate = stamp_futures_rate_bps_x100(entry_date)
        if broker == "zerodha":
            brokerage_flat = ZERODHA_BROKERAGE_PAISA
            brokerage_turnover: int | None = None
        else:
            brokerage_flat = GROWW_BROKERAGE_PAISA
            brokerage_turnover = GROWW_FUTURES_BROKERAGE_TURNOVER_BPS_X100
        tail = exec_rust.materialize_futures_tail(
            entry_open_paisa,
            exit_open_paisa,
            entry_volume=window.entry_bar.volume,
            exit_volume=window.exit_bar.volume,
            path_highs_paisa=path_highs,
            path_lows_paisa=path_lows,
            is_long=leg.side == "long",
            reject_stale=reject_stale_bars,
            lot_size=lot_size,
            n_lots=n_lots,
            cost_free=cost_free,
            brokerage_flat_paisa=brokerage_flat,
            brokerage_turnover_bps_x100=brokerage_turnover,
            stt_rate=stt_rate,
            etc_rate=etc_rate,
            sebi_rate=SEBI_FEE_BPS_X100,
            ipft_rate=ipft_rate,
            stamp_rate=stamp_rate,
            gst_rate=GST_TOTAL_BPS_X100,
        )
    except (ValueError, TypeError, ArithmeticError):
        return None
    if tail is None:
        return None
    kind, stack, mae_paisa, mfe_paisa = tail
    if kind == "stale_bar":
        _LOG.warning(
            "futures.reject.stale_bar",
            instrument=signal.instrument,
            contract_symbol=leg.contract_symbol,
            entry_volume=window.entry_bar.volume,
            exit_volume=window.exit_bar.volume,
        )
        return RejectedFuturesTrade(leg=leg, reason=FuturesRejectReason.STALE_BAR)
    if kind != "trade" or stack is None:
        # A kernel contract fault — the Python oracle arm serves instead.
        return None
    # Validation parity: the SAME FuturesTradeInput the Python tail builds —
    # its __post_init__ re-validates the inputs and raises byte-identically.
    FuturesTradeInput(
        entry_dt_ist=entry_ist,
        exit_dt_ist=signal.exit_ts.astimezone(IST),
        instrument=signal.instrument,
        contract_symbol=leg.contract_symbol,
        expiry_date=leg.expiry_date,
        entry_bar_open_paisa=entry_open_paisa,
        exit_bar_open_paisa=exit_open_paisa,
        side=leg.side,
        lot_size=lot_size,
        n_lots=n_lots,
        broker=broker,
        gst_split=gst_split,
    )
    return MaterializedFuturesTrade(
        leg=leg,
        entry_ts=signal.entry_ts,
        exit_ts=signal.exit_ts,
        entry_open_paisa=entry_open_paisa,
        exit_open_paisa=exit_open_paisa,
        buy_fill_paisa=stack.buy_fill_paisa,
        sell_fill_paisa=stack.sell_fill_paisa,
        lot_size=lot_size,
        n_lots=n_lots,
        gross_pnl_paisa=stack.gross_pnl_paisa,
        total_charges_paisa=stack.total_charges_paisa,
        net_pnl_paisa=stack.net_pnl_paisa,
        mae_paisa=mae_paisa,
        mfe_paisa=mfe_paisa,
        vix_at_entry=signal.vix_at_entry,
        vix_at_exit=signal.vix_at_exit,
        cost_free=cost_free,
        exit_reason=signal.exit_reason,
    )


# --- end exec-rust dispatch helper ---


def materialize_futures_trade(
    signal: FuturesSignal,
    *,
    resolver: FrontMonthResolver,
    source: FuturesBarSource,
    broker: Broker = "groww",
    gst_split: GstSplit = "intra_state",
    n_lots: int = 1,
    reject_stale_bars: bool = False,
) -> MaterializedFuturesTrade | RejectedFuturesTrade:
    """Materialize + price one futures trade for *signal*.

    Pure given deterministic seams. Rejections check in the PINNED deterministic
    order — **lot_size → cost_regime → no_contract → no_data → stale_bar** —
    with the two pure DATE gates first, BEFORE any registry/lake I/O (the
    options-executor ordering precedent; pinned by a never-called-seam test).

    Parameters
    ----------
    signal
        The winning spot signal to translate.
    resolver
        The front-month contract resolver seam.
    source
        The futures bar source seam.
    broker, gst_split, n_lots
        Cost-model parameters (Groww, intra-state GST, 1 lot).
    reject_stale_bars
        OPT-IN (default ``False``): reject a volume-0 entry/exit bar as
        ``STALE_BAR``. The default FILLS it — R14/R15 lock "a volume-0 printed
        futures bar is real + tradable" (expiry-afternoon volume collapse is
        counted as-is).

    Returns
    -------
    MaterializedFuturesTrade | RejectedFuturesTrade
        The priced trade, or a named counted reject.
    """
    entry_ist = signal.entry_ts.astimezone(IST)
    entry_date = entry_ist.date()

    # Gate 1 — LOT_SIZE_UNKNOWN (pure date precondition, no I/O). The try is
    # NARROW: it wraps ONLY the lot lookup, so a ValueError from any other
    # layer still propagates (mutation-killer pinned).
    try:
        lot_size = futures_lot_size(signal.instrument, entry_date)
    except ValueError as exc:
        _LOG.warning(
            "futures.reject.lot_size_unknown",
            instrument=signal.instrument,
            entry_date=entry_date.isoformat(),
            error=str(exc),
        )
        return RejectedFuturesTrade(leg=None, reason=FuturesRejectReason.LOT_SIZE_UNKNOWN)

    # Gate 2 — COST_REGIME_UNVERIFIED (pure date precondition, no I/O), for a
    # COST-BEARING future only: the futures-ETC lookup refuses every window
    # today (brutex.costs.futures_costs), so a cost-bearing trade is skipped
    # BEFORE any wasted registry/lake read. A cost-free INDEX future never hits
    # this gate (the live path). The except is NARROW (only the typed refusal).
    if not is_cost_free_future_underlying(signal.instrument):
        try:
            futures_exchange_charge_bps_x100(signal.instrument, entry_date)
        except UnverifiedCostRegimeError as exc:
            _LOG.warning(
                "futures.reject.cost_regime_unverified",
                instrument=signal.instrument,
                entry_date=entry_date.isoformat(),
                window_start=exc.window_start.isoformat(),
                verified_from=exc.verified_from.isoformat() if exc.verified_from else None,
                error=str(exc),
            )
            return RejectedFuturesTrade(leg=None, reason=FuturesRejectReason.COST_REGIME_UNVERIFIED)

    # Gate 3 — NO_CONTRACT (first I/O: the registry/lake calendar).
    leg = select_futures_leg(signal, resolver=resolver)
    if isinstance(leg, RejectedFuturesTrade):
        return leg

    # Gate 4 — NO_DATA (exact-minute bars; never a nearest-bar substitute).
    window = source.load_window(
        contract_symbol=leg.contract_symbol, entry_ts=signal.entry_ts, exit_ts=signal.exit_ts
    )
    if window is None:
        return RejectedFuturesTrade(leg=leg, reason=FuturesRejectReason.NO_DATA)

    # --- exec-rust dispatch (slice 7 / P8, DEC-EXEC-RUST-CORE-001) ---
    # Default-OFF (`BRUTEX_EXEC_RUST`): None unless the kernel serves the
    # whole stale/cost/excursion tail; the verbatim Python tail below is the
    # permanent oracle arm.
    rust_served = _materialize_tail_rust(
        signal,
        leg,
        window,
        lot_size,
        broker=broker,
        gst_split=gst_split,
        n_lots=n_lots,
        reject_stale_bars=reject_stale_bars,
    )
    if rust_served is not None:
        return rust_served
    # --- end exec-rust dispatch ---

    # Gate 5 — STALE_BAR (OPT-IN only; the default fills volume-0 bars, R14/R15).
    if reject_stale_bars and (window.entry_bar.volume == 0 or window.exit_bar.volume == 0):
        _LOG.warning(
            "futures.reject.stale_bar",
            instrument=signal.instrument,
            contract_symbol=leg.contract_symbol,
            entry_volume=window.entry_bar.volume,
            exit_volume=window.exit_bar.volume,
        )
        return RejectedFuturesTrade(leg=leg, reason=FuturesRejectReason.STALE_BAR)

    entry_open_paisa = rupees_to_paisa(window.entry_bar.open)
    exit_open_paisa = rupees_to_paisa(window.exit_bar.open)
    breakdown = compute_futures_trade_cost(
        FuturesTradeInput(
            entry_dt_ist=entry_ist,
            exit_dt_ist=signal.exit_ts.astimezone(IST),
            instrument=signal.instrument,
            contract_symbol=leg.contract_symbol,
            expiry_date=leg.expiry_date,
            entry_bar_open_paisa=entry_open_paisa,
            exit_bar_open_paisa=exit_open_paisa,
            side=leg.side,
            lot_size=lot_size,
            n_lots=n_lots,
            broker=broker,
            gst_split=gst_split,
        )
    )
    entry_fill = breakdown.buy_fill_paisa if leg.side == "long" else breakdown.sell_fill_paisa
    mae_paisa, mfe_paisa = _excursions_paisa(
        window, side=leg.side, entry_fill_paisa=entry_fill, n_shares=breakdown.n_shares
    )
    return MaterializedFuturesTrade(
        leg=leg,
        entry_ts=signal.entry_ts,
        exit_ts=signal.exit_ts,
        entry_open_paisa=entry_open_paisa,
        exit_open_paisa=exit_open_paisa,
        buy_fill_paisa=breakdown.buy_fill_paisa,
        sell_fill_paisa=breakdown.sell_fill_paisa,
        lot_size=lot_size,
        n_lots=n_lots,
        gross_pnl_paisa=breakdown.gross_pnl_paisa,
        total_charges_paisa=breakdown.total_charges_paisa,
        net_pnl_paisa=breakdown.net_pnl_paisa,
        mae_paisa=mae_paisa,
        mfe_paisa=mfe_paisa,
        vix_at_entry=signal.vix_at_entry,
        vix_at_exit=signal.vix_at_exit,
        cost_free=breakdown.cost_free,
        exit_reason=signal.exit_reason,
    )


@dataclass(frozen=True, slots=True)
class FuturesRuleResult:
    """Aggregated priced outcome of the futures translation over one run's signals.

    The per-reason reject counters project 1:1 from the executor's count-dict
    tally (no if/elif catch-all, no unreachable fail-loud else — every
    :class:`FuturesRejectReason` member has exactly one field).

    Raises
    ------
    ValueError
        On a negative count or a count inconsistency
        (``n_accepted + n_rejected != n_signals``; winners+losers exceeding
        accepted).
    """

    instrument: str
    direction: str
    n_signals: int
    n_accepted: int
    n_rejected_lot_size_unknown: int
    n_rejected_cost_regime_unverified: int
    n_rejected_no_contract: int
    n_rejected_no_data: int
    n_rejected_stale_bar: int
    gross_pnl_paisa: int
    total_charges_paisa: int
    net_pnl_paisa: int
    n_winners: int
    n_losers: int
    n_vix_missing: int = 0

    def __post_init__(self) -> None:
        for name in (
            "n_signals",
            "n_accepted",
            "n_rejected_lot_size_unknown",
            "n_rejected_cost_regime_unverified",
            "n_rejected_no_contract",
            "n_rejected_no_data",
            "n_rejected_stale_bar",
            "n_winners",
            "n_losers",
            "n_vix_missing",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"FuturesRuleResult.{name} must be >= 0")
        if self.n_accepted + self.n_rejected != self.n_signals:
            raise ValueError(
                f"FuturesRuleResult: n_accepted ({self.n_accepted}) + n_rejected "
                f"({self.n_rejected}) must equal n_signals ({self.n_signals})"
            )
        if self.n_winners + self.n_losers > self.n_accepted:
            raise ValueError(
                f"FuturesRuleResult: n_winners ({self.n_winners}) + n_losers "
                f"({self.n_losers}) cannot exceed n_accepted ({self.n_accepted})"
            )

    @property
    def n_rejected(self) -> int:
        """Total rejected signals across every named reason."""
        return (
            self.n_rejected_lot_size_unknown
            + self.n_rejected_cost_regime_unverified
            + self.n_rejected_no_contract
            + self.n_rejected_no_data
            + self.n_rejected_stale_bar
        )

    @property
    def win_rate(self) -> float:
        """Fraction of accepted trades with strictly positive net P&L (0.0 if none)."""
        if self.n_accepted == 0:
            return 0.0
        return self.n_winners / self.n_accepted


#: FuturesRejectReason → FuturesRuleResult field name — the 1:1 projection the
#: count-dict tally lands on (a new enum member without a field fails loud here).
_REJECT_FIELD_BY_REASON: Final[dict[FuturesRejectReason, str]] = {
    FuturesRejectReason.LOT_SIZE_UNKNOWN: "n_rejected_lot_size_unknown",
    FuturesRejectReason.COST_REGIME_UNVERIFIED: "n_rejected_cost_regime_unverified",
    FuturesRejectReason.NO_CONTRACT: "n_rejected_no_contract",
    FuturesRejectReason.NO_DATA: "n_rejected_no_data",
    FuturesRejectReason.STALE_BAR: "n_rejected_stale_bar",
}


def run_futures_over_signals(
    signals: Iterable[FuturesSignal],
    *,
    instrument: str,
    direction: TradeDirection,
    resolver: FrontMonthResolver,
    source: FuturesBarSource,
    broker: Broker = "groww",
    gst_split: GstSplit = "intra_state",
    n_lots: int = 1,
    reject_stale_bars: bool = False,
) -> FuturesRuleResult:
    """Price every *signal* and aggregate into a :class:`FuturesRuleResult`.

    O(N signals) — inherent work over the input (never an O(1)-total claim).
    Rejects tally through a count-dict keyed by :class:`FuturesRejectReason`
    and project 1:1 onto the result fields — no catch-all bucket can miscount.

    Parameters
    ----------
    signals
        The winning spot signals (uniform instrument + direction).
    instrument, direction
        The run's row identity — every signal must match (fail-loud on a mixed
        batch; the result row aggregates ONE combo's trades).
    resolver, source, broker, gst_split, n_lots, reject_stale_bars
        Threaded to :func:`materialize_futures_trade`.

    Returns
    -------
    FuturesRuleResult
        The aggregated row.

    Raises
    ------
    ValueError
        If a signal's instrument/direction differs from the stated row identity.
    """
    counts: dict[FuturesRejectReason, int] = dict.fromkeys(FuturesRejectReason, 0)
    n_signals = 0
    n_accepted = 0
    gross = 0
    charges = 0
    net = 0
    winners = 0
    losers = 0
    vix_missing = 0

    for signal in signals:
        if signal.instrument != instrument or signal.direction != direction:
            raise ValueError(
                f"run_futures_over_signals: signal ({signal.instrument!r}, "
                f"{signal.direction.value}) does not match the stated row identity "
                f"({instrument!r}, {direction.value})"
            )
        n_signals += 1
        outcome = materialize_futures_trade(
            signal,
            resolver=resolver,
            source=source,
            broker=broker,
            gst_split=gst_split,
            n_lots=n_lots,
            reject_stale_bars=reject_stale_bars,
        )
        if isinstance(outcome, RejectedFuturesTrade):
            counts[outcome.reason] += 1
            continue
        n_accepted += 1
        gross += outcome.gross_pnl_paisa
        charges += outcome.total_charges_paisa
        net += outcome.net_pnl_paisa
        if outcome.vix_at_entry is None:
            vix_missing += 1
        # Winner = strictly positive net; everything else is a non-winner (a
        # loss or an exact-zero scratch — reachable on the cost-free index path
        # when entry/exit fills coincide, so both buckets stay live).
        if outcome.net_pnl_paisa > 0:
            winners += 1
        else:
            losers += 1

    # The 1:1 projection (counts[reason] → its n_rejected_* field). The
    # count-dict was seeded with EVERY enum member, so a new reason can never
    # silently fold into a catch-all; :data:`_REJECT_FIELD_BY_REASON` is the
    # test-pinned completeness oracle (every member ↔ exactly one field).
    return FuturesRuleResult(
        instrument=instrument,
        direction=direction.value,
        n_signals=n_signals,
        n_accepted=n_accepted,
        n_rejected_lot_size_unknown=counts[FuturesRejectReason.LOT_SIZE_UNKNOWN],
        n_rejected_cost_regime_unverified=counts[FuturesRejectReason.COST_REGIME_UNVERIFIED],
        n_rejected_no_contract=counts[FuturesRejectReason.NO_CONTRACT],
        n_rejected_no_data=counts[FuturesRejectReason.NO_DATA],
        n_rejected_stale_bar=counts[FuturesRejectReason.STALE_BAR],
        gross_pnl_paisa=gross,
        total_charges_paisa=charges,
        net_pnl_paisa=net,
        n_winners=winners,
        n_losers=losers,
        n_vix_missing=vix_missing,
    )


def execute_futures_sim(
    signals: Sequence[FuturesSignal],
    *,
    instrument: str,
    direction: TradeDirection,
    run_id: str,
    resolver: FrontMonthResolver,
    source: FuturesBarSource,
    n_lots: int = 1,
    audit: AuditAppend | None = None,
    reject_stale_bars: bool = False,
    spot_check_k: int = 8,
) -> FuturesRuleResult:
    """Run the futures translation sim for one winning run.

    Aggregates via :func:`run_futures_over_signals`, appends ONE hash-chained
    audit row stamped with the run's single *run_id* (R28/R41/R43), emits ONE
    aggregate ``futures.sim.unverified_cost_window`` WARN when any trade fell
    in an unverified cost window (spans DERIVED from the regime-table DATA),
    and re-simulates a deterministic *run_id*-seeded sample through the
    SEPARATE naive reference simulator (R45) — a mismatch logs a LOUD
    ``futures.sim.differential_mismatch`` ERROR and stamps ``suspect`` onto the
    audit payload.

    Parameters
    ----------
    signals
        The run's replayed spot signals.
    instrument, direction, run_id
        The run's row identity + the ONE run id threading every artifact.
    resolver, source, n_lots, reject_stale_bars
        Threaded to the materializer.
    audit
        Optional hash-chained audit sink; one row when supplied.
    spot_check_k
        Differential sample size (``min(k, len(signals))``; ``<= 0`` disables).

    Returns
    -------
    FuturesRuleResult
        The aggregated result row.
    """
    # Lazy import — reference_sim imports this module's types, so the runtime
    # import lives here to keep the module graph acyclic at import time.
    from brutex.futures.reference_sim import differential_spot_check

    result = run_futures_over_signals(
        signals,
        instrument=instrument,
        direction=direction,
        resolver=resolver,
        source=source,
        n_lots=n_lots,
        reject_stale_bars=reject_stale_bars,
    )

    if result.n_rejected_cost_regime_unverified:
        from brutex.costs.futures_costs import futures_etc_unverified_windows

        _LOG.warning(
            "futures.sim.unverified_cost_window",
            n_rejected=result.n_rejected_cost_regime_unverified,
            windows={
                exch: [
                    f"[{'history-start' if s == date.min else s.isoformat()} → "
                    f"{e.isoformat() if e else 'open'})"
                    for s, e in spans
                ]
                for exch, spans in futures_etc_unverified_windows().items()
            },
        )

    suspect = False
    n_checked = 0
    if spot_check_k > 0 and signals:
        sample = _deterministic_sample(signals, k=spot_check_k, run_id=run_id)
        production: list[int | None] = []
        policy_skipped: list[bool] = []
        for sig in sample:
            outcome = materialize_futures_trade(
                sig,
                resolver=resolver,
                source=source,
                n_lots=n_lots,
                reject_stale_bars=reject_stale_bars,
            )
            if (
                isinstance(outcome, RejectedFuturesTrade)
                and outcome.reason == FuturesRejectReason.STALE_BAR
            ):
                # A POLICY reject (opt-in staleness), not a data/math outcome —
                # excluded from the arithmetic differential, counted openly.
                production.append(None)
                policy_skipped.append(True)
            else:
                production.append(
                    None if isinstance(outcome, RejectedFuturesTrade) else outcome.net_pnl_paisa
                )
                policy_skipped.append(False)
        report = differential_spot_check(
            sample,
            production,
            resolver=resolver,
            source=source,
            n_lots=n_lots,
            policy_skipped=policy_skipped,
        )
        suspect = report.suspect
        n_checked = report.n_checked
        if report.suspect:
            _LOG.error(
                "futures.sim.differential_mismatch",
                run_id=run_id,
                mismatches=list(report.mismatches),
            )

    if audit is not None:
        audit.append(
            action=_AUDIT_ACTION,
            subject=f"{instrument}:{direction.value}:{run_id}",
            payload={
                "run_id": run_id,
                "n_signals": result.n_signals,
                "n_accepted": result.n_accepted,
                "n_rejected": result.n_rejected,
                "net_pnl_paisa": result.net_pnl_paisa,
                "n_winners": result.n_winners,
                "differential_checked": n_checked,
                "suspect": suspect,
            },
        )
    return result


def _deterministic_sample(
    signals: Sequence[FuturesSignal], *, k: int, run_id: str
) -> list[FuturesSignal]:
    """A *run_id*-seeded deterministic sample of ``min(k, len)`` signals (R45).

    Reproducible bit-for-bit across re-runs of the SAME run identity (Golden
    Rule #4): indices derive from :class:`random.Random` seeded with *run_id*.
    """
    import random

    if k >= len(signals):
        return list(signals)
    rng = random.Random(run_id)  # noqa: S311  # nosec B311 — deterministic R45 sampling, non-crypto
    indices = sorted(rng.sample(range(len(signals)), k))
    return [signals[i] for i in indices]
