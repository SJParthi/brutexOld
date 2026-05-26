"""Type definitions for the Track 2 cost calculator.

Per `docs/track2/CALCULATOR_SPEC.md §11 API surface` (LOCKED 2026-05-16).

All numeric values are int64 paise. No floats anywhere — eliminates
accumulator drift across 33B-trial sweeps and matches the byte-exact
worked examples in `docs/track2/COSTS_VERIFIED.md §5`.

Public types:
- ``TradeInput`` — round-trip trade specification (entry + exit + meta)
- ``TradeCostBreakdown`` — per-component charge breakdown + roll-ups
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
from typing import Literal

__all__ = [
    "Broker",
    "GstSplit",
    "Instrument",
    "OptionType",
    "TradeCostBreakdown",
    "TradeInput",
    "TradeOutcome",
    "TradeSide",
]


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


# ─────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TradeCostBreakdown:
    """Per-component charge breakdown + roll-ups.

    Per `CALCULATOR_SPEC.md §11 API surface` schema. Every field is
    int64 paise (1 INR = 100 paise). The byte-exact worked examples
    in `COSTS_VERIFIED.md §5` are the test oracles.

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
        Informational: ``2 * TICK_PAISA * n_shares`` (already baked
        into buy_fill_paisa / sell_fill_paisa).
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
