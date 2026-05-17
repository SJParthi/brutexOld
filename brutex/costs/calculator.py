"""Per-trade cost calculator core.

Implements the deterministic O(1) algorithm in
`docs/track2/CALCULATOR_SPEC.md §4` with the assurance-round
amendments locked 2026-05-16:

- **DEC-COST-001** GST rounding: round TOTAL once at 18%, not
  CGST/SGST separately at 9% each (Section 170 CGST).
- **DEC-COST-002** STT regime key: ``entry_dt_ist.date()``.
- **DEC-COST-003** Finance Act 2026 STT rates LOCKED (primary source).
- **DEC-COST-004** STT-on-exercise rate split LOCKED.
- **DEC-EXEC-008** BUY-only policy: Track 2 only sets ``side`` to
  ``long_call`` or ``long_put``. The short_* branches exist as
  forward-compat stubs but are not used in Track 2 backtests.

All math in int64 paise. Pure function: no I/O, no global state,
deterministic. Designed to be inline-able by Rust/Numba downstream
at sweep time.
"""

from __future__ import annotations

from brutex.costs import constants as C  # noqa: N812 — C = costs constants namespace
from brutex.costs.types import TradeCostBreakdown, TradeInput
from brutex.options.lot_size_history import OptionsUnderlying, get_lot_size

__all__ = ["compute_trade_cost"]


# ─────────────────────────────────────────────────────────────────
# Rounding helpers (per CALCULATOR_SPEC §4.1)
# ─────────────────────────────────────────────────────────────────


def _round_to_paisa(amount_x_bps_x100: int) -> int:
    """Round a (paisa × rate-scaled) product down to whole paise, half-up.

    Rate constants in `brutex.costs.constants` use the encoding
    ``rate_decimal × 10_000_000``. So a 0.03503% rate is stored as
    3503, and:

        rate_decimal = 3503 / 10_000_000 = 0.0003503

    The integer math (half-up rounding to whole paise) is:

        paisa = (paisa_amount * bps_x100 + 5_000_000) // 10_000_000

    The ``+ 5_000_000`` is half-up rounding in integer space.

    Used for non-statutory items (exchange / SEBI / IPFT) where
    commercial half-up rounding applies.
    """
    return (amount_x_bps_x100 + 5_000_000) // 10_000_000


def _round_to_rupee(amount_paisa: int) -> int:
    """Round to nearest whole rupee (100 paise), half-up.

    Used for STATUTORY levies: STT, stamp duty, GST. Returns paise
    rounded to the nearest multiple of 100.

    Example: 1170 paisa (₹11.70) → 1200 paisa (₹12).
             50 paisa (₹0.50) → 100 paisa (₹1) [half-up rule].
    """
    return ((amount_paisa + 50) // 100) * 100


# ─────────────────────────────────────────────────────────────────
# Lot-size lookup (proxy to brutex.options.lot_size_history)
# ─────────────────────────────────────────────────────────────────


def _lookup_lot_size(instrument: str, trade_date: object) -> int:
    """Resolve lot size for ``(instrument, date)``.

    Returns the lot size in effect on ``trade_date`` per the
    8-transition history in ``brutex.options.lot_size_history``.
    """
    underlying = OptionsUnderlying(instrument)
    return get_lot_size(underlying, trade_date)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────
# Slippage application (TRACK2_OPTIONS_SPEC §9 — flat 1-tick adverse)
# ─────────────────────────────────────────────────────────────────


def _apply_slippage(
    entry_open_paisa: int,
    exit_open_paisa: int,
    side: str,
) -> tuple[int, int]:
    """Apply 1-tick adverse slippage per side.

    Long side: buy 1 tick higher, sell 1 tick lower.
    Short side: sell 1 tick lower, buy 1 tick higher (cover).

    Returns ``(buy_fill_paisa, sell_fill_paisa)``.

    For Track 2 (BUY-only, per DEC-EXEC-008), only the long branches
    fire. Short branches kept for forward-compat.
    """
    tick = C.TICK_PAISA
    if side.startswith("long"):
        return entry_open_paisa + tick, exit_open_paisa - tick
    # short side: BUY at exit_open + tick (cover), SELL at entry_open + tick
    return exit_open_paisa + tick, entry_open_paisa + tick


# ─────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────


def compute_trade_cost(trade: TradeInput) -> TradeCostBreakdown:
    """Compute the full charge breakdown for a single round-trip trade.

    Pure function: no I/O, no global state, deterministic. O(1).

    Parameters
    ----------
    trade : TradeInput
        The round-trip trade specification.

    Returns
    -------
    TradeCostBreakdown
        Per-component charges + roll-ups. All int paise.

    Notes
    -----
    Implements `CALCULATOR_SPEC.md §4` Steps 1-13 with the four
    assurance-round amendments (DEC-COST-001/002/003/004).

    Track 2 (per DEC-RISK-011) always calls with
    ``trade.trade_outcome == "NORMAL_CLOSE"``. The expiry branches
    (EXPIRY_EXERCISE / EXPIRY_ASSIGN / EXPIRY_WORTHLESS) are
    forward-compat stubs from `CALCULATOR_SPEC.md §4.2` and are
    NOT exercised in Track 2 backtests.
    """
    # ── Step 1: apply slippage (1-tick flat adverse) ────────────────────
    buy_fill_paisa, sell_fill_paisa = _apply_slippage(
        trade.entry_bar_open_paisa,
        trade.exit_bar_open_paisa,
        trade.side,
    )

    # ── Step 2: lookup lot size ─────────────────────────────────────────
    n_shares_per_lot = _lookup_lot_size(trade.instrument, trade.entry_dt_ist.date())
    n_shares = trade.n_lots * n_shares_per_lot

    # ── Step 3: notionals (paise) ───────────────────────────────────────
    buy_notional_paisa = buy_fill_paisa * n_shares
    sell_notional_paisa = sell_fill_paisa * n_shares

    # ── Step 4: brokerage (per-order flat, both legs) ──────────────────
    per_leg_brokerage = C.brokerage_paisa(trade.broker)
    brokerage_paisa = per_leg_brokerage * 2

    # ── Step 5: STT (sell-side premium only) ────────────────────────────
    # DEC-COST-002: regime selected by entry_dt_ist.date()
    # DEC-COST-003: rates locked per Finance Act 2026
    stt_rate_bps_x100 = C.stt_options_rate_bps_x100(trade.entry_dt_ist.date())
    stt_paisa_raw = (sell_notional_paisa * stt_rate_bps_x100) // 10_000_000
    stt_paisa = _round_to_rupee(stt_paisa_raw)

    # ── Step 6: exchange transaction charge (both sides on premium) ─────
    exch_rate_bps_x100 = C.exchange_charge_bps_x100(trade.instrument)
    exch_buy_paisa = _round_to_paisa(buy_notional_paisa * exch_rate_bps_x100)
    exch_sell_paisa = _round_to_paisa(sell_notional_paisa * exch_rate_bps_x100)
    exch_paisa = exch_buy_paisa + exch_sell_paisa

    # ── Step 7: SEBI turnover fee (both sides) ──────────────────────────
    sebi_buy_paisa = _round_to_paisa(buy_notional_paisa * C.SEBI_FEE_BPS_X100)
    sebi_sell_paisa = _round_to_paisa(sell_notional_paisa * C.SEBI_FEE_BPS_X100)
    sebi_paisa = sebi_buy_paisa + sebi_sell_paisa

    # ── Step 8: IPFT (both sides; NSE only — BSE near-zero) ─────────────
    ipft_rate_bps_x100 = C.ipft_bps_x100(trade.instrument)
    ipft_buy_paisa = _round_to_paisa(buy_notional_paisa * ipft_rate_bps_x100)
    ipft_sell_paisa = _round_to_paisa(sell_notional_paisa * ipft_rate_bps_x100)
    ipft_paisa = ipft_buy_paisa + ipft_sell_paisa

    # ── Step 9: stamp duty (buy leg only, statutory rupee rounding) ─────
    stamp_paisa_raw = (buy_notional_paisa * C.STAMP_DUTY_OPTIONS_BPS_X100) // 10_000_000
    stamp_paisa = _round_to_rupee(stamp_paisa_raw)

    # ── Step 10: GST 18% on services only (DEC-COST-001) ────────────────
    # Round TOTAL once at 18%. NOT CGST + SGST separately at 9% each.
    # Section 170 CGST mandates rounding the total tax, not each component.
    # gst_split (intra/inter state) only affects how the total is DISPLAYED
    # to the operator, NOT how it's computed.
    gst_base_paisa = brokerage_paisa + exch_paisa + sebi_paisa + ipft_paisa
    gst_paisa_raw = (gst_base_paisa * C.GST_TOTAL_BPS_X100) // 10_000_000
    gst_paisa = _round_to_rupee(gst_paisa_raw)

    # ── Step 11: total charges ──────────────────────────────────────────
    total_charges_paisa = (
        brokerage_paisa + stt_paisa + exch_paisa + sebi_paisa + ipft_paisa + stamp_paisa + gst_paisa
    )

    # ── Step 12: gross PnL (slippage already baked into fills) ──────────
    if trade.side.startswith("long"):
        gross_pnl_paisa = sell_notional_paisa - buy_notional_paisa
    else:  # short — PnL inverts (sold first, buy back to close)
        gross_pnl_paisa = buy_notional_paisa - sell_notional_paisa

    # ── Step 13: net PnL ────────────────────────────────────────────────
    net_pnl_paisa = gross_pnl_paisa - total_charges_paisa

    # Slippage as informational line (2 ticks × n_shares: ±1 tick per leg)
    slippage_paisa = 2 * C.TICK_PAISA * n_shares

    return TradeCostBreakdown(
        n_shares_per_lot=n_shares_per_lot,
        n_shares=n_shares,
        buy_fill_paisa=buy_fill_paisa,
        sell_fill_paisa=sell_fill_paisa,
        buy_notional_paisa=buy_notional_paisa,
        sell_notional_paisa=sell_notional_paisa,
        brokerage_paisa=brokerage_paisa,
        stt_paisa=stt_paisa,
        exch_paisa=exch_paisa,
        sebi_paisa=sebi_paisa,
        ipft_paisa=ipft_paisa,
        stamp_paisa=stamp_paisa,
        gst_paisa=gst_paisa,
        slippage_paisa=slippage_paisa,
        total_charges_paisa=total_charges_paisa,
        gross_pnl_paisa=gross_pnl_paisa,
        net_pnl_paisa=net_pnl_paisa,
        used_exercise_branch=trade.trade_outcome in ("EXPIRY_EXERCISE", "EXPIRY_ASSIGN"),
        n_orders_per_leg=1,  # iceberg slicing not yet implemented (§6b deferred)
    )
