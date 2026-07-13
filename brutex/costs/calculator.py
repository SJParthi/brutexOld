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
- **DEC-TRACK2-COSTS-SPEC-RECONCILE-001** short-side slippage resolved
  to the CALCULATOR_SPEC §8 ADVERSE rule (short SELL entry at
  ``entry_open − 1 tick``, cover BUY at ``exit_open + 1 tick``),
  superseding the #963 MINOR-4 pin; the branch stays
  production-unreachable under DEC-EXEC-008.

All math in int64 paise. Pure function: no I/O, no global state,
deterministic. Designed to be inline-able by Rust/Numba downstream
at sweep time.
"""

from __future__ import annotations

from brutex.costs import constants as C  # noqa: N812 — C = costs constants namespace
from brutex.costs.types import (
    TradeCostBreakdown,
    TradeInput,
    UnsupportedTradeOutcomeError,
)
from brutex.execution.fill_mode import FillMode
from brutex.options.lot_size_history import OptionsUnderlying, get_lot_size

__all__ = ["compute_trade_cost"]


# ─────────────────────────────────────────────────────────────────
# Rounding helpers (per CALCULATOR_SPEC §4.1)
# ─────────────────────────────────────────────────────────────────


def _round_to_paisa(amount_x_bps_x100: int) -> int:
    """Round a (paisa × rate-scaled) product to whole paise, ROUND_HALF_UP.

    Rate constants in `brutex.costs.constants` use the encoding
    ``rate_decimal × 10_000_000``. So a 0.03503% rate is stored as
    3503, and:

        rate_decimal = 3503 / 10_000_000 = 0.0003503

    ROUND_HALF_UP here means **round half AWAY from zero**, matching
    ``brutex.money.round_money`` (Decimal ``ROUND_HALF_UP``): a tie at
    ``+0.5`` rounds to ``+1`` and a tie at ``-0.5`` rounds to ``-1``. For
    NON-NEGATIVE inputs the result is byte-identical to the historical
    ``(x + 5_000_000) // 10_000_000`` formula; the explicit sign branch
    only changes negative inputs (NIT-1 hardening — the live calculator
    never feeds a negative product, but a future signed caller no longer
    rounds toward +∞).

    Used for non-statutory items (exchange / SEBI / IPFT) where
    commercial half-up rounding applies.
    """
    if amount_x_bps_x100 >= 0:
        return (amount_x_bps_x100 + 5_000_000) // 10_000_000
    # Round half AWAY from zero for negatives (mirror the positive branch).
    return -((-amount_x_bps_x100 + 5_000_000) // 10_000_000)


def _round_to_rupee(amount_paisa: int) -> int:
    """Round to nearest whole rupee (100 paise), ROUND_HALF_UP.

    Used for STATUTORY levies: STT, stamp duty, GST. Returns paise
    rounded to the nearest multiple of 100.

    ROUND_HALF_UP = round half AWAY from zero, matching
    ``brutex.money.round_money`` (Decimal ``ROUND_HALF_UP``): ``+50`` →
    ``+100`` and ``-50`` → ``-100``. For NON-NEGATIVE inputs the result is
    byte-identical to the historical ``((x + 50) // 100) * 100`` formula;
    the explicit sign branch only changes negatives (NIT-1 hardening —
    unreachable in the live calculator, but a future signed caller no
    longer rounds toward +∞).

    Example: 1170 paisa (₹11.70) → 1200 paisa (₹12).
             50 paisa (₹0.50) → 100 paisa (₹1) [half-up rule].
    """
    if amount_paisa >= 0:
        return ((amount_paisa + 50) // 100) * 100
    # Round half AWAY from zero for negatives (mirror the positive branch).
    return -(((-amount_paisa + 50) // 100) * 100)


# ─────────────────────────────────────────────────────────────────
# Lot-size lookup (proxy to brutex.options.lot_size_history)
# ─────────────────────────────────────────────────────────────────


def _lookup_lot_size(instrument: str, trade_date: object) -> int:
    """Resolve lot size for ``(instrument, date)``.

    Returns the lot size in effect on ``trade_date`` per the
    8-transition history in ``brutex.options.lot_size_history``.

    DOCUMENTED LIMITATION (lot-size VINTAGE — see also
    ``TradeCostBreakdown.lot_size_basis`` + COSTS_VERIFIED.md §20): the
    lookup is keyed by the TRADE date, not the CONTRACT's vintage. A real
    exchange lot revision applies per-contract from a specific expiry
    onward, so around a transition the two can disagree — e.g. NIFTY
    2024-11-19 resolves lot 25 while 2024-11-20 resolves lot 75 (the
    SEBI ₹15L notional revision), a 3× sizing jump keyed purely on the
    trade date even though a still-listed pre-revision contract kept its
    old lot until its own expiry. The per-circular NSE contract-level
    rollout dates are UNVERIFIED offline (Golden Rule #1), so no vintage
    logic is faked; the breakdown carries ``lot_size_basis="trade_date"``
    so downstream consumers can see which basis priced the trade.
    """
    underlying = OptionsUnderlying(instrument)
    return get_lot_size(underlying, trade_date)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────
# Slippage application (TRACK2_OPTIONS_SPEC §9 — flat 1-tick adverse;
# DEC-FILL-WORST-CASE-001 — the same tick around the worst-case anchor)
# ─────────────────────────────────────────────────────────────────


def _apply_slippage(
    entry_open_paisa: int,
    exit_open_paisa: int,
    side: str,
    *,
    fill_mode: FillMode = FillMode.PRECISE,
    entry_high_paisa: int | None = None,
    entry_low_paisa: int | None = None,
    exit_high_paisa: int | None = None,
    exit_low_paisa: int | None = None,
) -> tuple[int, int, int]:
    """Apply 1-tick adverse slippage per side around the mode's fill anchor.

    Long side: buy 1 tick higher, sell 1 tick lower.
    Short side: sell 1 tick lower, buy 1 tick higher (cover).

    The ANCHOR each tick applies to is the fill mode's
    (DEC-FILL-WORST-CASE-001): under :data:`FillMode.PRECISE` (the default —
    every legacy call byte-identical) the bar OPEN; under
    :data:`FillMode.WORST_CASE` the option's own minute-bar ADVERSE EXTREME
    — a buy anchors on the bar HIGH, a sell on the bar LOW (long: entry
    HIGH + tick / exit LOW − tick; short mirrored: opening sell at entry
    LOW − tick, cover at exit HIGH + tick). Keeping the SAME ± tick on top
    of the extreme preserves the tick discipline: on a flat bar
    (``high == open``) a bare buy-at-HIGH would be one tick BETTER than
    today's precise fill — with the tick it is exactly equal, so worst-case
    is NEVER better than precise. (The worst-case buy therefore deliberately
    prices one tick OUTSIDE the printed range — the honest adverse floor,
    not a printable price.) The side's two adverse anchors are REQUIRED
    under WORST_CASE (fail-loud — never a silent PRECISE degrade).

    Returns ``(buy_fill_paisa, sell_fill_paisa, realized_slippage_per_share)``.

    For Track 2 (BUY-only, per DEC-EXEC-008), only the long branches
    fire. Short branches kept for forward-compat.

    Every returned fill is floored at one tick (MINOR-3): a SELL leg on a
    bar that opens at exactly one tick (₹0.05) — the long EXIT, or, since
    the DEC-TRACK2-COSTS-SPEC-RECONCILE-001 adverse re-pin, the short
    ENTRY — would otherwise slip to a ₹0.00 sell fill (a "free" sale),
    which is economically impossible — nothing trades below one tick. The
    floor only affects those degenerate tick-boundary cases (under
    WORST_CASE a sub-tick bar LOW lands here too); the normal path (prices
    ≥ 2 ticks) is unchanged.

    ``realized_slippage_per_share`` is the slippage TRULY baked into the
    post-floor fills (NIT-1): the buy leg's adverse rise from its raw
    anchor plus the sell leg's adverse drop to its raw anchor, both
    measured against the floored fills. On the normal path (fills ≥ 2
    ticks) it equals the nominal ``2 × tick``; when the 1-tick floor fires
    on the sell leg the realized sell-leg movement is 0, so the per-share
    value is exactly ``1 × tick`` — never over-stating the slippage that
    the notionals/PnL actually carry. Under WORST_CASE the open→extreme
    move is fill-ANCHOR semantics carried by the gross P&L, not slippage.
    """
    tick = C.TICK_PAISA
    worst = fill_mode is FillMode.WORST_CASE
    if side.startswith("long"):
        # buy leg ← entry bar, sell leg ← exit bar.
        if worst:
            if entry_high_paisa is None or exit_low_paisa is None:
                msg = (
                    "_apply_slippage: fill_mode=worst_case (long) needs "
                    "entry_high_paisa + exit_low_paisa — refusing to silently "
                    "price PRECISE (DEC-FILL-WORST-CASE-001)"
                )
                raise ValueError(msg)
            buy_raw, sell_raw = entry_high_paisa, exit_low_paisa
        else:
            buy_raw, sell_raw = entry_open_paisa, exit_open_paisa
    # RESOLVED (was MINOR-4): the §8 ADVERSE rule is authoritative
    # (DEC-TRACK2-COSTS-SPEC-RECONCILE-001, supersedes the #963 MINOR-4
    # pin, which had byte-pinned the favorable ``entry_open + tick`` sell
    # fill while the spec disagreed with itself): SELL entry at
    # ``entry_open − 1 tick``, BUY cover at ``exit_open + 1 tick`` —
    # adverse on both legs (buy higher, sell lower). The §4 pseudocode +
    # its "# CHECK SIGN" marker were rewritten to agree with §8. This
    # branch stays production-unreachable under DEC-EXEC-008 (BUY-only).
    # buy leg (cover) ← exit bar, sell leg ← entry bar. The WORST_CASE
    # arm mirrors: sell anchors entry LOW, cover anchors exit HIGH.
    elif worst:
        if entry_low_paisa is None or exit_high_paisa is None:
            msg = (
                "_apply_slippage: fill_mode=worst_case (short) needs "
                "entry_low_paisa + exit_high_paisa — refusing to silently "
                "price PRECISE (DEC-FILL-WORST-CASE-001)"
            )
            raise ValueError(msg)
        buy_raw, sell_raw = exit_high_paisa, entry_low_paisa
    else:
        buy_raw, sell_raw = exit_open_paisa, entry_open_paisa
    buy_fill, sell_fill = buy_raw + tick, sell_raw - tick
    # Floor every fill at one tick — no instrument trades below ₹0.05.
    buy_fill, sell_fill = max(buy_fill, tick), max(sell_fill, tick)
    # Realized per-share slippage = the adverse movement TRULY baked into the
    # floored fills: |buy_fill − buy_raw| (buy moved against us, up) +
    # |sell_raw − sell_fill| (sell moved against us, down, but the floor caps
    # the drop). On the normal path this is 2×tick; a floored sell leg yields
    # 1×tick (NIT-1: never over-states the slippage carried by the notionals).
    realized_per_share = abs(buy_fill - buy_raw) + abs(sell_raw - sell_fill)
    return buy_fill, sell_fill, realized_per_share


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
    (EXPIRY_EXERCISE / EXPIRY_ASSIGN / EXPIRY_WORTHLESS) require the
    `CALCULATOR_SPEC.md §4.2` charge state machine, which is NOT yet
    implemented — passing one raises ``UnsupportedTradeOutcomeError``
    (fail-loud) instead of silently pricing it as ``NORMAL_CLOSE``.

    Raises
    ------
    UnsupportedTradeOutcomeError
        If ``trade.trade_outcome`` is not ``"NORMAL_CLOSE"`` (MAJOR-1).
    UnverifiedCostRegimeError
        If ``entry_dt_ist.date()`` lands on an ETC refusal window
        (currently every pre-2024-10-01 date; the authoritative spans are
        DERIVED from the regime tables via ``etc_unverified_windows``,
        never this prose): the ETC regime for the date is UNVERIFIED —
        the whole trade cost is
        refused, no component fabricated (DEC-ETC-REGIME-HISTORY-001;
        raised out of Step 6's ``exchange_charge_bps_x100``). The options
        executor prechecks-and-rejects such trades BEFORE calling here,
        so this raise is the defence-in-depth choke point for direct
        callers. DEAD-CODE-AUDITOR NOTE: the VERIFIED pre-Oct-2024 STT
        options regime (6_250) is retained but becomes unreachable
        THROUGH ``compute_trade_cost`` for options (Step 5 computes it,
        Step 6 refuses first) — it stays unit-covered directly via
        ``stt_options_rate_bps_x100`` (test_costs_regime_table.py +
        test_stt_regime_boundary_oct_2024).
    """
    # ── Step 0: fail loud on an unimplemented outcome (MAJOR-1) ─────────
    # Only NORMAL_CLOSE is implemented. Pricing an expiry outcome with the
    # premium-based NORMAL_CLOSE math would be silently wrong; CALCULATOR_SPEC
    # §4.2 (STT on intrinsic / zero-STT assign / brokerage-only worthless) is
    # not yet implemented, so we refuse rather than fabricate a cost. This
    # guard is a fail-loud REFUSAL, not the state machine — see the §4.2
    # IMPLEMENTATION STATUS block (DEC-TRACK2-COSTS-SPEC-RECONCILE-001;
    # guard ≠ implementation per MIGRATION.md #963 MAJOR-1).
    if trade.trade_outcome != "NORMAL_CLOSE":
        msg = (
            f"trade_outcome {trade.trade_outcome!r} is not supported: only "
            "'NORMAL_CLOSE' is implemented (CALCULATOR_SPEC §4.2 expiry state "
            "machine not yet implemented)"
        )
        raise UnsupportedTradeOutcomeError(msg)

    # ── Step 1: apply slippage (1-tick flat adverse, around the fill
    #    mode's anchor — DEC-FILL-WORST-CASE-001) ──────────────────────────
    buy_fill_paisa, sell_fill_paisa, realized_slip_per_share = _apply_slippage(
        trade.entry_bar_open_paisa,
        trade.exit_bar_open_paisa,
        trade.side,
        fill_mode=trade.fill_mode,
        entry_high_paisa=trade.entry_bar_high_paisa,
        entry_low_paisa=trade.entry_bar_low_paisa,
        exit_high_paisa=trade.exit_bar_high_paisa,
        exit_low_paisa=trade.exit_bar_low_paisa,
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
    # Date-regime-selected like STT (COMMON rate_for_date mechanism). Only
    # the dated VERIFIED regimes are encoded; a date on an UNVERIFIED window
    # lands on an explicit refusal row and RAISES UnverifiedCostRegimeError
    # here (DEC-ETC-REGIME-HISTORY-001) — the options executor prechecks
    # and rejects such trades before reaching this point, so this raise is
    # the authoritative defence-in-depth choke point for direct callers.
    exch_rate_bps_x100 = C.exchange_charge_bps_x100(trade.instrument, trade.entry_dt_ist.date())
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
    #
    # UNVERIFIED date-boundary note (Golden Rule #1): the SEBI turnover fee
    # is included in the GST base for ALL dates. A claimed GST-on-SEBI-fee
    # introduction boundary (~2022-07-18) has NO in-repo citation, so it is
    # deliberately NOT implemented; if the claim holds, a pre-boundary trade
    # is OVER-charged by 18% of the (sub-rupee) SEBI fee — conservative.
    # See COSTS_VERIFIED.md §20 known limitations.
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

    # Slippage as informational line — derived from the REALIZED fill deltas
    # (NIT-1) so it is always internally consistent with the notionals/PnL: on
    # the normal path this is the nominal 2 ticks × n_shares (±1 tick per leg),
    # but when the 1-tick floor caps a degenerate sub-2-tick sell fill the
    # sell leg actually moved 0, so the realized slippage is 1 tick × n_shares.
    slippage_paisa = realized_slip_per_share * n_shares

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
        # Always False: the Step-0 guard rejects every non-NORMAL_CLOSE
        # outcome, so the exercise branch (§4.2) is never reached here.
        used_exercise_branch=False,
        n_orders_per_leg=1,  # iceberg slicing not yet implemented (§6b deferred)
        # The lot size above was keyed by the TRADE date, not the contract's
        # vintage — the honest basis stamp (see _lookup_lot_size's limitation).
        lot_size_basis="trade_date",
    )
