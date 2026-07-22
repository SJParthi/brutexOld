"""Type definitions for the Track 2 cost calculator.

Per `docs/track2/CALCULATOR_SPEC.md §11 API surface` (LOCKED 2026-05-16).

All numeric values are int64 paise. No floats anywhere — eliminates
accumulator drift across 33B-trial sweeps and matches the byte-exact
worked examples in `docs/track2/COSTS_VERIFIED.md §5`.

Public types:
- ``TradeInput`` — round-trip trade specification (entry + exit + meta)
- ``TradeCostBreakdown`` — per-component charge breakdown + roll-ups
- ``ChargeStack`` — the pinned 17-field integer charge-stack shape shared
  by both stacks and the ``brutex.exec_rust`` seam (homed here, the
  stdlib-only leaf, so the seam never imports ``calculator`` — the
  CodeQL ``py/cyclic-import`` fix; ``calculator`` re-exports it)
- ``TradeSide`` — long_call / long_put / short_call / short_put
- ``TradeOutcome`` — NORMAL_CLOSE / EXPIRY_EXERCISE / EXPIRY_ASSIGN / EXPIRY_WORTHLESS
- ``Broker`` — groww / zerodha
- ``GstSplit`` — intra_state / inter_state

Track 2 (per DEC-RISK-011 + DEC-EXEC-008) ALWAYS uses:
  - ``TradeSide`` ∈ {long_call, long_put} (BUY-only policy)
  - ``TradeOutcome`` = NORMAL_CLOSE (intraday-only; forced 15:20 exit)

The other enum values exist as forward-compat stubs per
CALCULATOR_SPEC §4.2 four-outcome state machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, Literal, NamedTuple, get_args

__all__ = [
    "Broker",
    "GstSplit",
    "Instrument",
    "OptionType",
    "TradeCostBreakdown",
    "TradeInput",
    "TradeOutcome",
    "TradeSide",
    "UnsupportedTradeOutcomeError",
    "UnverifiedCostRegimeError",
    "require_bar_extremes",
]


def require_bar_extremes(
    *,
    entry_bar_high_paisa: int | None,
    entry_bar_low_paisa: int | None,
    exit_bar_high_paisa: int | None,
    exit_bar_low_paisa: int | None,
) -> tuple[int, int, int, int]:
    """Narrow the four validated fill-bar extremes from ``int | None`` to ``int``.

    The ONE COMMON narrowing seam (Golden Rule #11) shared by
    :func:`brutex.costs.calculator.compute_trade_cost` and
    :func:`brutex.costs.futures_costs.compute_futures_trade_cost`: both
    ``TradeInput`` and ``FuturesTradeInput`` validate all four extremes
    non-``None`` at construction (the DEC-FILL-WORST-CASE-ONLY-001 refusal in
    ``__post_init__``), so a ``None`` here is a broken dataclass invariant —
    not a reachable state through a validated constructor. The explicit
    ``raise AssertionError`` (the house invariant idiom — never a bare
    ``assert``, which vanishes under ``python -O`` and trips bandit B101)
    keeps the guard fail-loud in every runtime mode.

    Returns
    -------
    tuple[int, int, int, int]
        ``(entry_high, entry_low, exit_high, exit_low)`` in paisa.

    Raises
    ------
    AssertionError
        If any extreme is ``None`` — a broken dataclass invariant (an
        instance built past ``__post_init__``, e.g. via ``object.__new__``).
    """
    if (
        entry_bar_high_paisa is None
        or entry_bar_low_paisa is None
        or exit_bar_high_paisa is None
        or exit_bar_low_paisa is None
    ):
        raise AssertionError(
            "fill-bar extremes are None past construction validation — a broken "
            "TradeInput/FuturesTradeInput invariant (DEC-FILL-WORST-CASE-ONLY-001 "
            "requires all four extremes at construction)"
        )
    return (
        entry_bar_high_paisa,
        entry_bar_low_paisa,
        exit_bar_high_paisa,
        exit_bar_low_paisa,
    )


# ─────────────────────────────────────────────────────────────────
# Domain errors
# ─────────────────────────────────────────────────────────────────


class UnsupportedTradeOutcomeError(ValueError):
    """Raised when ``compute_trade_cost`` is asked to price a non-NORMAL
    trade outcome.

    Track 2 (per DEC-RISK-011) is intraday-only with a forced 15:20 exit,
    so every trade resolves as ``NORMAL_CLOSE``. The expiry outcomes
    (``EXPIRY_EXERCISE`` / ``EXPIRY_ASSIGN`` / ``EXPIRY_WORTHLESS``) require
    the ``CALCULATOR_SPEC §4.2`` charge state machine (STT on intrinsic,
    zero-STT assignment, brokerage-only worthless), which is NOT yet
    implemented. Rather than silently return premium-based NORMAL_CLOSE
    numbers for an expiry outcome (a silently-wrong cost), the calculator
    fails loud with this specific exception.

    A ``ValueError`` subclass so existing callers that catch ``ValueError``
    for input validation still catch it, while a caller can pin this exact
    type. NOT ``NotImplementedError`` — that is coverage-excluded and we
    require a real, tested branch.
    """


#: The default remediation line carried by :class:`UnverifiedCostRegimeError`
#: — a refusal is only useful if it tells the operator how to close it.
_DEFAULT_REGIME_REMEDIATION: Final[str] = (
    "source the superseding circular's predecessor and add ONE dated "
    "RegimeRow(effective_date, rate_bps_x100, source) to the table with a "
    "MIGRATION.md ledger citation — zero code change (COSTS_VERIFIED.md §20 row 1)"
)


class UnverifiedCostRegimeError(ValueError):
    """Raised by ``rate_for_date`` when the regime row selected for a date is
    an explicit UNVERIFIED refusal row (``RegimeRow.rate_bps_x100 is None``).

    Per DEC-ETC-REGIME-HISTORY-001 (MIGRATION.md #1524) a statutory rate that
    is not citation-grounded is NEVER fabricated (Golden Rule #1): the lookup
    refuses loudly, carrying the refusal window, the identified-but-unverified
    circulars (in ``source``), and the remediation path.

    A ``ValueError`` subclass (the ``UnsupportedTradeOutcomeError`` precedent):
    existing catch-``ValueError`` callers keep working; a caller — e.g. the
    options executor's per-trade skip-and-count precheck — can pin this type.

    Parameters
    ----------
    charge : str
        Human name of the charge (e.g. ``"exchange transaction charge
        (options premium)"``).
    exchange : str
        ``"NSE"`` / ``"BSE"``; may be ``""`` when the table is not
        exchange-scoped.
    on_date : date
        The date whose rate was requested.
    window_start : date
        The refusal row's ``start`` (``date.min`` for a history-start anchor).
    verified_from : date | None
        The next verified row's ``start`` — the remediation horizon; ``None``
        for an open-ended trailing refusal window.
    source : str
        The refusal row's citation-gap stamp (what was identified, what was
        never retrieved).
    remediation : str
        How to close the window; defaults to
        :data:`_DEFAULT_REGIME_REMEDIATION`.
    """

    def __init__(
        self,
        *,
        charge: str,
        exchange: str,
        on_date: date,
        window_start: date,
        verified_from: date | None,
        source: str,
        remediation: str = _DEFAULT_REGIME_REMEDIATION,
    ) -> None:
        self.charge = charge
        self.exchange = exchange
        self.on_date = on_date
        self.window_start = window_start
        self.verified_from = verified_from
        self.source = source
        self.remediation = remediation
        exchange_label = f" ({exchange})" if exchange else ""
        window_end = verified_from.isoformat() if verified_from else "open"
        super().__init__(
            f"{charge}{exchange_label} is UNVERIFIED for {on_date}: window "
            f"[{window_start} → {window_end}) — {source}. Refusing to fabricate "
            f"a rate (Golden Rule #1). Remediation: {remediation}"
        )


# ─────────────────────────────────────────────────────────────────
# Enums (Literal types — keep them inline-able)
# ─────────────────────────────────────────────────────────────────

Instrument = Literal["NIFTY", "BANKNIFTY", "SENSEX"]
"""Track 2 universe (per CLAUDE.md Golden Rule #3). India VIX is
reference-only and never appears as a tradeable instrument here."""

OptionType = Literal["CE", "PE"]
"""Call (CE) or Put (PE). NOT used by the cost calculator (call vs
put has no charge difference), but stored for audit per CALCULATOR_SPEC §1."""

TradeSide = Literal["long_call", "long_put", "short_call", "short_put"]
"""Which leg is buy and which is sell. Track 2 ONLY uses long_call
or long_put (DEC-EXEC-008 BUY-only policy). The short_* values are
forward-compat stubs."""

Broker = Literal["groww", "zerodha"]
"""Brokerage rate selection. Default ``groww``."""

GstSplit = Literal["intra_state", "inter_state"]
"""Tax-state split for GST: ``intra_state`` → CGST 9% + SGST 9%
(displayed half-half, computed as 18% total per DEC-COST-001).
``inter_state`` → IGST 18% (single computation). Set once per trader
account, not per trade. Default ``intra_state``."""

TradeOutcome = Literal[
    "NORMAL_CLOSE",
    "EXPIRY_EXERCISE",
    "EXPIRY_ASSIGN",
    "EXPIRY_WORTHLESS",
]
"""State machine for how the trade resolved (CALCULATOR_SPEC §4.2).
Track 2 hard-codes ``NORMAL_CLOSE`` per DEC-RISK-011 (intraday-only,
forced 15:20 exit, no held-to-expiry). The other three values are
forward-compat stubs for future tracks that may hold to expiry."""


# ─────────────────────────────────────────────────────────────────
# Allowed-member sets — derived ONCE from the Literal aliases above
# via ``typing.get_args`` so there is a SINGLE source of truth (no
# hand-maintained duplicate list that could silently drift). Used by
# ``TradeInput.__post_init__`` to fail loud on a typo'd enum string.
# ─────────────────────────────────────────────────────────────────

_INSTRUMENT_VALUES: Final[frozenset[str]] = frozenset(get_args(Instrument))
_OPTION_TYPE_VALUES: Final[frozenset[str]] = frozenset(get_args(OptionType))
_TRADE_SIDE_VALUES: Final[frozenset[str]] = frozenset(get_args(TradeSide))
_BROKER_VALUES: Final[frozenset[str]] = frozenset(get_args(Broker))
_GST_SPLIT_VALUES: Final[frozenset[str]] = frozenset(get_args(GstSplit))
_TRADE_OUTCOME_VALUES: Final[frozenset[str]] = frozenset(get_args(TradeOutcome))


# ─────────────────────────────────────────────────────────────────
# Inputs
# ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TradeInput:
    """Per-round-trip trade specification.

    All times are IST (Track 1 + Track 2 are India-market only).
    All prices are int paise (1 INR = 100 paise) — never float.

    Per `CALCULATOR_SPEC.md §1` table. Field types and semantics
    are spec-locked.

    Parameters
    ----------
    entry_dt_ist : datetime
        Trade entry timestamp in IST. Determines STT-era
        (DEC-COST-002: ``entry_dt_ist.date()`` selects which of
        the 3 STT regimes applies) AND lot-size era.
    exit_dt_ist : datetime
        Trade exit timestamp in IST. For Track 2 this is always
        ``15:20:00 IST`` on the same day as entry per DEC-RISK-011.
    instrument : Instrument
        One of NIFTY / BANKNIFTY / SENSEX. Determines exchange
        (NIFTY/BANKNIFTY = NSE, SENSEX = BSE) and lot-size table.
    option_type : OptionType
        CE or PE. Stored for audit; not used in cost math.
    strike : int
        Strike price in paise. ≥ 1. Stored for audit.
    expiry_date : date
        Option expiry. Stored for audit. Per DEC-RISK-011, Track 2
        never holds across expiry.
    entry_bar_open_paisa : int
        Entry bar's OPEN price in paise. ≥ 5 (i.e. ≥ 1 tick).
        Slippage applied on top (CALCULATOR_SPEC §8).
    exit_bar_open_paisa : int
        Exit bar's OPEN price in paise. ≥ 5.
    side : TradeSide
        Which leg buys and which sells. For Track 2: ``long_call``
        or ``long_put`` only (DEC-EXEC-008).
    n_lots : int
        Number of lots traded. ≥ 1. Each lot's share-count is
        looked up from the lot-size history per
        ``brutex.options.lot_size_history`` for ``entry_dt_ist.date()``.
    broker : Broker
        Default ``groww``.
    gst_split : GstSplit
        Default ``intra_state``.
    trade_outcome : TradeOutcome
        Default ``NORMAL_CLOSE``. Track 2 always uses this default
        per DEC-RISK-011.
    intrinsic_value_paisa_at_expiry : int
        Required only when ``trade_outcome`` is ``EXPIRY_EXERCISE``
        or ``EXPIRY_ASSIGN``. Intrinsic value × n_shares. Track 2
        always uses 0 (never expires).
    entry_bar_high_paisa, entry_bar_low_paisa, exit_bar_high_paisa, exit_bar_low_paisa : int | None
        The fill bars' extremes in paisa — the worst-case fill anchors
        (the option's OWN minute bar; ``OptionBar`` carries high/low).
        REQUIRED — all four (DEC-FILL-WORST-CASE-ONLY-001: worst-case is
        the ONLY fill law, so an input without its adverse anchors is
        unpriceable and refuses fail-loud; the ``| None`` typing survives
        only so the refusal is a validated ``ValueError``, never a silent
        keyword omission). Each pair must bracket its bar's open
        (``low <= open <= high``); positivity/tick floors on the extremes
        are deliberately NOT demanded — a degenerate sub-tick LOW is
        absorbed by the fill floor in ``_apply_slippage``, never a sim
        crash.
    """

    entry_dt_ist: datetime
    exit_dt_ist: datetime
    instrument: Instrument
    option_type: OptionType
    strike: int
    expiry_date: date
    entry_bar_open_paisa: int
    exit_bar_open_paisa: int
    side: TradeSide
    n_lots: int = 1
    broker: Broker = "groww"
    gst_split: GstSplit = "intra_state"
    trade_outcome: TradeOutcome = "NORMAL_CLOSE"
    intrinsic_value_paisa_at_expiry: int = 0
    entry_bar_high_paisa: int | None = None
    entry_bar_low_paisa: int | None = None
    exit_bar_high_paisa: int | None = None
    exit_bar_low_paisa: int | None = None

    def __post_init__(self) -> None:
        # Lightweight sanity checks at boundaries only — internal
        # users (Track 2 execution simulator) construct valid inputs.
        if self.exit_dt_ist < self.entry_dt_ist:
            msg = f"exit_dt_ist {self.exit_dt_ist} must be >= entry_dt_ist {self.entry_dt_ist}"
            raise ValueError(msg)
        if self.entry_bar_open_paisa < 5:  # noqa: PLR2004 — 5 paise = 1 tick
            msg = f"entry_bar_open_paisa {self.entry_bar_open_paisa} below 1 tick (5 paise)"
            raise ValueError(msg)
        if self.exit_bar_open_paisa < 5:  # noqa: PLR2004
            msg = f"exit_bar_open_paisa {self.exit_bar_open_paisa} below 1 tick (5 paise)"
            raise ValueError(msg)
        if self.n_lots < 1:
            msg = f"n_lots {self.n_lots} must be >= 1"
            raise ValueError(msg)
        if self.strike < 1:
            msg = f"strike {self.strike} must be >= 1"
            raise ValueError(msg)
        # Fail loud on a typo'd enum string. The fields are typed
        # ``Literal[...]`` but a static type is NOT a runtime guard: a bad
        # value like ``side="banana"`` would otherwise be SILENTLY ACCEPTED
        # and route into the short-leg / inverted-PnL branch, producing a
        # silently-WRONG cost. Each allowed set is derived from its own
        # Literal alias (single source of truth — see above).
        for field_name, value, allowed in (
            ("instrument", self.instrument, _INSTRUMENT_VALUES),
            ("option_type", self.option_type, _OPTION_TYPE_VALUES),
            ("side", self.side, _TRADE_SIDE_VALUES),
            ("broker", self.broker, _BROKER_VALUES),
            ("gst_split", self.gst_split, _GST_SPLIT_VALUES),
            ("trade_outcome", self.trade_outcome, _TRADE_OUTCOME_VALUES),
        ):
            if value not in allowed:
                msg = f"{field_name} {value!r} is not a valid value; allowed: {sorted(allowed)}"
                raise ValueError(msg)
        # DEC-FILL-WORST-CASE-ONLY-001: worst-case is the ONLY fill law, so
        # the four adverse-anchor extremes are REQUIRED unconditionally — an
        # input without them cannot be priced and refuses fail-loud (never a
        # silent open-anchored fill; the retired PRECISE law is deleted).
        missing = [
            name
            for name in (
                "entry_bar_high_paisa",
                "entry_bar_low_paisa",
                "exit_bar_high_paisa",
                "exit_bar_low_paisa",
            )
            if getattr(self, name) is None
        ]
        if missing:
            msg = (
                f"the worst-case fill law needs the fill-bar extremes; missing {missing} "
                f"(worst-case is the ONLY fill law — DEC-FILL-WORST-CASE-ONLY-001)"
            )
            raise ValueError(msg)
        for label, open_p, high, low in (
            (
                "entry",
                self.entry_bar_open_paisa,
                self.entry_bar_high_paisa,
                self.entry_bar_low_paisa,
            ),
            ("exit", self.exit_bar_open_paisa, self.exit_bar_high_paisa, self.exit_bar_low_paisa),
        ):
            if high is not None and high < open_p:
                msg = f"{label}_bar_high_paisa {high} < {label}_bar_open_paisa {open_p}"
                raise ValueError(msg)
            if low is not None and low > open_p:
                msg = f"{label}_bar_low_paisa {low} > {label}_bar_open_paisa {open_p}"
                raise ValueError(msg)


# ─────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────


class ChargeStack(NamedTuple):
    """The 17 integer outputs of one round-trip charge-stack computation.

    Field ORDER is pinned — it mirrors the Rust ``exec::ChargeStack`` struct
    (DEC-EXEC-RUST-CORE-001, MIGRATION.md #2215) field-for-field, and both
    project 1:1 onto the numeric fields of
    :class:`~brutex.costs.types.TradeCostBreakdown` /
    :class:`~brutex.costs.futures_costs.FuturesCostBreakdown`. All values are
    int64 paisa. Shared by BOTH stacks (§11 COMMON — the futures twin returns
    the same shape).

    Homed in THIS stdlib-only leaf module — not ``calculator`` where the
    stacks live — so the ``brutex.exec_rust`` seam can import the shared
    shape at module level WITHOUT importing ``calculator`` (whose dispatch
    site lazily imports the seam back): the calculator ↔ exec_rust mutual
    import pair is structurally gone (CodeQL ``py/cyclic-import``; the
    #1533 ``movers_cache.revision_generation`` leaf-homing precedent).
    ``calculator`` re-exports the name for its historical consumers.
    """

    buy_fill_paisa: int
    sell_fill_paisa: int
    n_shares: int
    buy_notional_paisa: int
    sell_notional_paisa: int
    brokerage_paisa: int
    stt_paisa: int
    exch_paisa: int
    sebi_paisa: int
    ipft_paisa: int
    stamp_paisa: int
    gst_paisa: int
    slippage_paisa: int
    total_charges_paisa: int
    gross_pnl_paisa: int
    net_pnl_paisa: int
    realized_slip_per_share: int


@dataclass(frozen=True)
class TradeCostBreakdown:
    """Per-component charge breakdown + roll-ups.

    Per `CALCULATOR_SPEC.md §11 API surface` schema. Every numeric field
    is int64 paise (1 INR = 100 paise). The worked examples in
    `COSTS_VERIFIED.md §5` (Examples 1-4 — regenerated 2026-07-03 from
    the REAL calculator's output) are byte-exact test oracles, enforced
    by ``tests/golden/test_costs_verified_examples.py``; Example 5
    (Muhurat brokerage waiver) is spec-only — ``compute_trade_cost`` has
    no Muhurat branch yet, so it is NOT an enforced oracle.

    Parameters
    ----------
    n_shares_per_lot : int
        Lot size at the trade's entry date (looked up from
        ``brutex.options.lot_size_history``).
    n_shares : int
        ``n_lots * n_shares_per_lot``.
    buy_fill_paisa : int
        Entry price AFTER 1-tick slippage application (TRACK2_OPTIONS_SPEC §9).
    sell_fill_paisa : int
        Exit price AFTER 1-tick slippage application.
    buy_notional_paisa : int
        ``buy_fill_paisa * n_shares``.
    sell_notional_paisa : int
        ``sell_fill_paisa * n_shares``.
    brokerage_paisa : int
        Round-trip broker fee. For Groww / Zerodha: ``2 * 2000`` =
        ₹40 (₹20 per executed order, NOT per contract — DEC-COST §4.4).
    stt_paisa : int
        STT on sell-side premium per DEC-COST-002 + DEC-COST-003.
        Rate selected by ``entry_dt_ist.date()``. Rounded to nearest
        rupee (Section 36(1)(xv) IT Act statutory rule).
    exch_paisa : int
        Exchange transaction charge on premium both sides. NSE for
        NIFTY/BANKNIFTY, BSE for SENSEX.
    sebi_paisa : int
        SEBI turnover fee (0.0001%) on premium both sides.
    ipft_paisa : int
        NSE IPFT (0.0005%) for NIFTY/BANKNIFTY; ~zero for BSE.
    stamp_paisa : int
        Stamp duty (0.003%) on buy-side premium ONLY.
    gst_paisa : int
        GST 18% on (brokerage + exch + sebi + ipft). Total rounded
        ONCE per DEC-COST-001 (NOT CGST/SGST separately).
    slippage_paisa : int
        Informational: the slippage TRULY baked into the fills (NIT-1) —
        the realized per-share fill deltas × n_shares. Normally
        ``2 * TICK_PAISA * n_shares`` (±1 tick per leg), but ``1 *
        TICK_PAISA * n_shares`` when the 1-tick floor caps a degenerate
        sub-2-tick sell fill (the sell leg then moved 0). Always
        internally consistent with buy_fill_paisa / sell_fill_paisa.
    total_charges_paisa : int
        Sum of brokerage + stt + exch + sebi + ipft + stamp + gst.
    gross_pnl_paisa : int
        ``sell_notional_paisa - buy_notional_paisa`` (slippage baked
        into fills).
    net_pnl_paisa : int
        ``gross_pnl_paisa - total_charges_paisa``. The final
        post-cost PnL for this trade.
    used_exercise_branch : bool
        True iff CALCULATOR_SPEC §4.2 EXPIRY_EXERCISE or
        EXPIRY_ASSIGN branch was used. Track 2 always False.
    n_orders_per_leg : int
        Per CALCULATOR_SPEC §6b iceberg slicing. 1 unless the
        freeze quantity is exceeded (Track 2 with n_lots=1
        never triggers this).
    lot_size_basis : Literal["trade_date"]
        Which basis keyed the lot-size lookup. Always ``"trade_date"``
        today: the lot is the one in effect on ``entry_dt_ist.date()``
        per ``brutex.options.lot_size_history``, NOT the CONTRACT's
        vintage. DOCUMENTED LIMITATION (COSTS_VERIFIED.md §20): a real
        exchange lot revision applies per-contract from a specific
        expiry onward, so in a transition window the two bases disagree
        — e.g. NIFTY 2024-11-19 → lot 25 vs 2024-11-20 → lot 75 (SEBI
        ₹15L notional revision), a 3× sizing difference keyed purely on
        the trade date. The per-circular contract-level rollout dates
        are UNVERIFIED offline, so a ``"contract_vintage"`` basis is
        deliberately NOT faked (Golden Rule #1); this field makes the
        basis explicit so downstream consumers can audit it.
    """

    n_shares_per_lot: int
    n_shares: int
    buy_fill_paisa: int
    sell_fill_paisa: int
    buy_notional_paisa: int
    sell_notional_paisa: int
    brokerage_paisa: int
    stt_paisa: int
    exch_paisa: int
    sebi_paisa: int
    ipft_paisa: int
    stamp_paisa: int
    gst_paisa: int
    slippage_paisa: int
    total_charges_paisa: int
    gross_pnl_paisa: int
    net_pnl_paisa: int
    used_exercise_branch: bool
    n_orders_per_leg: int
    lot_size_basis: Literal["trade_date"] = "trade_date"
