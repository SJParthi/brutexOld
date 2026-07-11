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

from typing import NamedTuple

from brutex.costs import constants as C  # noqa: N812 — C = costs constants namespace
from brutex.costs.types import (
    TradeCostBreakdown,
    TradeInput,
    UnsupportedTradeOutcomeError,
)
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
    # pragma: no mutate on the sign test: at 0 BOTH branches yield 0
    # ((0 + half) // scale == 0 == -((0 + half) // scale)), so every
    # comparison mutant (>, >= 1) is provably output-equivalent.
    if amount_x_bps_x100 >= 0:  # pragma: no mutate
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
    # pragma: no mutate — the same at-zero equivalence as _round_to_paisa.
    if amount_paisa >= 0:  # pragma: no mutate
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
# Slippage application (TRACK2_OPTIONS_SPEC §9 — flat 1-tick adverse)
# ─────────────────────────────────────────────────────────────────


def _apply_slippage(
    entry_open_paisa: int,
    exit_open_paisa: int,
    side: str,
) -> tuple[int, int, int]:
    """Apply 1-tick adverse slippage per side.

    Long side: buy 1 tick higher, sell 1 tick lower.
    Short side: sell 1 tick lower, buy 1 tick higher (cover).

    Returns ``(buy_fill_paisa, sell_fill_paisa, realized_slippage_per_share)``.

    For Track 2 (BUY-only, per DEC-EXEC-008), only the long branches
    fire. Short branches kept for forward-compat.

    Every returned fill is floored at one tick (MINOR-3): a SELL leg on a
    bar that opens at exactly one tick (₹0.05) — the long EXIT, or, since
    the DEC-TRACK2-COSTS-SPEC-RECONCILE-001 adverse re-pin, the short
    ENTRY — would otherwise slip to a ₹0.00 sell fill (a "free" sale),
    which is economically impossible — nothing trades below one tick. The
    floor only affects those degenerate tick-boundary cases; the normal
    path (prices ≥ 2 ticks) is unchanged.

    ``realized_slippage_per_share`` is the slippage TRULY baked into the
    post-floor fills (NIT-1): the buy leg's adverse rise from its raw open
    plus the sell leg's adverse drop to its raw open, both measured against
    the floored fills. On the normal path (fills ≥ 2 ticks) it equals the
    nominal ``2 × tick``; when the 1-tick floor fires on the sell leg the
    realized sell-leg movement is 0, so the per-share value is exactly
    ``1 × tick`` — never over-stating the slippage that the notionals/PnL
    actually carry.
    """
    tick = C.TICK_PAISA
    if side.startswith("long"):
        # buy leg ← entry bar, sell leg ← exit bar.
        buy_raw, sell_raw = entry_open_paisa, exit_open_paisa
        buy_fill, sell_fill = buy_raw + tick, sell_raw - tick
    else:
        # RESOLVED (was MINOR-4): the §8 ADVERSE rule is authoritative
        # (DEC-TRACK2-COSTS-SPEC-RECONCILE-001, supersedes the #963 MINOR-4
        # pin, which had byte-pinned the favorable ``entry_open + tick`` sell
        # fill while the spec disagreed with itself): SELL entry at
        # ``entry_open − 1 tick``, BUY cover at ``exit_open + 1 tick`` —
        # adverse on both legs (buy higher, sell lower). The §4 pseudocode +
        # its "# CHECK SIGN" marker were rewritten to agree with §8. This
        # branch stays production-unreachable under DEC-EXEC-008 (BUY-only).
        # buy leg (cover) ← exit bar, sell leg ← entry bar.
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
# The dispatchable integer charge-stack core (DEC-EXEC-RUST-CORE-001)
# ─────────────────────────────────────────────────────────────────


class ChargeStack(NamedTuple):
    """The 17 integer outputs of one round-trip charge-stack computation.

    Field ORDER is pinned — it mirrors the Rust ``exec::ChargeStack`` struct
    (DEC-EXEC-RUST-CORE-001, MIGRATION.md #1724) field-for-field, and both
    project 1:1 onto the numeric fields of
    :class:`~brutex.costs.types.TradeCostBreakdown` /
    :class:`~brutex.costs.futures_costs.FuturesCostBreakdown`. All values are
    int64 paisa. Shared by BOTH stacks (§11 COMMON — the futures twin returns
    the same shape).
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


def _charge_stack_py(
    entry_open_paisa: int,
    exit_open_paisa: int,
    *,
    is_long: bool,
    lot_size: int,
    n_lots: int,
    brokerage_round_trip_paisa: int,
    stt_rate: int,
    etc_rate: int,
    sebi_rate: int,
    ipft_rate: int,
    stamp_rate: int,
    gst_rate: int,
) -> ChargeStack:
    """CALCULATOR_SPEC §4 Steps 1, 3–13 as ONE pure integer function.

    The dispatchable options charge-stack core (DEC-EXEC-RUST-CORE-001,
    MIGRATION.md #1724): everything from the moment prices are integer paisa —
    fills, notionals, the full charge stack, gross/net — with every RATE
    resolved by the caller and passed as an argument (rate encoding
    ``rate_decimal × 10_000_000``; ``brokerage_round_trip_paisa`` is the
    already-resolved flat round-trip). The body is the byte-identical
    extraction of the historical inline steps of :func:`compute_trade_cost`;
    the Python body stays the permanent oracle for the Rust twin
    (``fno_math.exec_option_charge_stack`` — deletion is a distinct later
    decision, DEC-EXEC-PY-BODY-DELETE-001).

    Rounding-order laws (see the step comments): statutory STT/stamp/GST =
    floor-divide by 10^7 THEN half-up to the rupee; non-statutory
    ETC/SEBI/IPFT = half-up to the paisa PER LEG then summed; GST base = the
    SUM of the already-rounded service components (STT + stamp excluded),
    rounded ONCE (DEC-COST-001).
    """
    # ── Step 1: apply slippage (1-tick flat adverse) ────────────────────
    buy_fill_paisa, sell_fill_paisa, realized_slip_per_share = _apply_slippage(
        entry_open_paisa,
        exit_open_paisa,
        # pragma: no mutate on the else-literal: _apply_slippage branches on
        # startswith("long") ONLY, so ANY non-long string is behaviourally
        # identical — the literal mutant is provably equivalent.
        "long" if is_long else "short",  # pragma: no mutate
    )

    # ── Step 3: notionals (paise) — lot size resolved by the caller ─────
    n_shares = n_lots * lot_size
    buy_notional_paisa = buy_fill_paisa * n_shares
    sell_notional_paisa = sell_fill_paisa * n_shares

    # ── Step 4: brokerage (per-order flat, both legs — caller-resolved) ─
    brokerage_paisa = brokerage_round_trip_paisa

    # ── Step 5: STT (sell-side premium only) ────────────────────────────
    stt_paisa_raw = (sell_notional_paisa * stt_rate) // 10_000_000
    stt_paisa = _round_to_rupee(stt_paisa_raw)

    # ── Step 6: exchange transaction charge (both sides on premium) ─────
    exch_buy_paisa = _round_to_paisa(buy_notional_paisa * etc_rate)
    exch_sell_paisa = _round_to_paisa(sell_notional_paisa * etc_rate)
    exch_paisa = exch_buy_paisa + exch_sell_paisa

    # ── Step 7: SEBI turnover fee (both sides) ──────────────────────────
    sebi_buy_paisa = _round_to_paisa(buy_notional_paisa * sebi_rate)
    sebi_sell_paisa = _round_to_paisa(sell_notional_paisa * sebi_rate)
    sebi_paisa = sebi_buy_paisa + sebi_sell_paisa

    # ── Step 8: IPFT (both sides; NSE only — BSE near-zero) ─────────────
    ipft_buy_paisa = _round_to_paisa(buy_notional_paisa * ipft_rate)
    ipft_sell_paisa = _round_to_paisa(sell_notional_paisa * ipft_rate)
    ipft_paisa = ipft_buy_paisa + ipft_sell_paisa

    # ── Step 9: stamp duty (buy leg only, statutory rupee rounding) ─────
    stamp_paisa_raw = (buy_notional_paisa * stamp_rate) // 10_000_000
    stamp_paisa = _round_to_rupee(stamp_paisa_raw)

    # ── Step 10: GST 18% on services only (DEC-COST-001) ────────────────
    # Round TOTAL once. Base = sum of ALREADY-ROUNDED components; STT and
    # stamp EXCLUDED (they are transaction taxes, not services).
    gst_base_paisa = brokerage_paisa + exch_paisa + sebi_paisa + ipft_paisa
    gst_paisa_raw = (gst_base_paisa * gst_rate) // 10_000_000
    gst_paisa = _round_to_rupee(gst_paisa_raw)

    # ── Step 11: total charges ──────────────────────────────────────────
    total_charges_paisa = (
        brokerage_paisa + stt_paisa + exch_paisa + sebi_paisa + ipft_paisa + stamp_paisa + gst_paisa
    )

    # ── Step 12: gross PnL (slippage already baked into fills) ──────────
    if is_long:
        gross_pnl_paisa = sell_notional_paisa - buy_notional_paisa
    else:  # short — PnL inverts (sold first, buy back to close)
        gross_pnl_paisa = buy_notional_paisa - sell_notional_paisa

    # ── Step 13: net PnL ────────────────────────────────────────────────
    net_pnl_paisa = gross_pnl_paisa - total_charges_paisa

    # Slippage as informational line — realized (post-floor) fill deltas.
    slippage_paisa = realized_slip_per_share * n_shares

    return ChargeStack(
        buy_fill_paisa=buy_fill_paisa,
        sell_fill_paisa=sell_fill_paisa,
        n_shares=n_shares,
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
        realized_slip_per_share=realized_slip_per_share,
    )


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

    # ── Step 2: lookup lot size ─────────────────────────────────────────
    # (Step 1 — slippage — is pure and raise-free, so it now runs inside the
    # charge-stack core AFTER the lot/rate resolution below; the reorder is
    # unobservable — DEC-EXEC-RUST-CORE-001 extraction.)
    n_shares_per_lot = _lookup_lot_size(trade.instrument, trade.entry_dt_ist.date())

    # ── Steps 4/5/6/8 rate + brokerage RESOLUTION (Python stays the single
    # authoritative rate source; the resolved integers feed the stack) ────
    # Step 4: brokerage (per-order flat, both legs).
    per_leg_brokerage = C.brokerage_paisa(trade.broker)
    # Step 5 rate: STT — DEC-COST-002 regime selected by entry_dt_ist.date();
    # DEC-COST-003 rates locked per Finance Act 2026.
    stt_rate_bps_x100 = C.stt_options_rate_bps_x100(trade.entry_dt_ist.date())
    # Step 6 rate: exchange transaction charge — date-regime-selected like STT
    # (COMMON rate_for_date mechanism). Only the dated VERIFIED regimes are
    # encoded; a date on an UNVERIFIED window lands on an explicit refusal row
    # and RAISES UnverifiedCostRegimeError here (DEC-ETC-REGIME-HISTORY-001) —
    # the options executor prechecks and rejects such trades before reaching
    # this point, so this raise is the authoritative defence-in-depth choke
    # point for direct callers.
    exch_rate_bps_x100 = C.exchange_charge_bps_x100(trade.instrument, trade.entry_dt_ist.date())
    # Step 8 rate: IPFT (NSE only — BSE near-zero).
    ipft_rate_bps_x100 = C.ipft_bps_x100(trade.instrument)

    # ── Steps 1, 3–13: the pure integer charge-stack core ───────────────
    # GST note (DEC-COST-001): rounded ONCE at 18% on the services total —
    # NOT CGST + SGST separately at 9% each (Section 170 CGST). gst_split
    # (intra/inter state) only affects how the total is DISPLAYED, never how
    # it is computed. UNVERIFIED boundary note (Golden Rule #1): the SEBI fee
    # is in the GST base for ALL dates — the claimed ~2022-07-18 GST-on-SEBI
    # introduction has NO in-repo citation, so it is deliberately NOT
    # implemented (a pre-boundary trade is OVER-charged by 18% of the
    # sub-rupee SEBI fee — conservative; COSTS_VERIFIED.md §20).
    # --- exec-rust dispatch (slice 7 / P8, DEC-EXEC-RUST-CORE-001) ---
    # Lazy import: brutex.exec_rust imports THIS module's ChargeStack shape,
    # so a top-level import here would be a cycle (the materialize_trade
    # lazy-import precedent). Default-OFF (`BRUTEX_EXEC_RUST`): the seam
    # returns None unless the kernel actually serves; the Python core below
    # stays the permanent oracle arm (DEC-EXEC-PY-BODY-DELETE-001 owns any
    # later deletion decision).
    from brutex import exec_rust

    stack = exec_rust.charge_stack_option(
        trade.entry_bar_open_paisa,
        trade.exit_bar_open_paisa,
        is_long=trade.side.startswith("long"),
        lot_size=n_shares_per_lot,
        n_lots=trade.n_lots,
        brokerage_round_trip_paisa=per_leg_brokerage * 2,
        stt_rate=stt_rate_bps_x100,
        etc_rate=exch_rate_bps_x100,
        sebi_rate=C.SEBI_FEE_BPS_X100,
        ipft_rate=ipft_rate_bps_x100,
        stamp_rate=C.STAMP_DUTY_OPTIONS_BPS_X100,
        gst_rate=C.GST_TOTAL_BPS_X100,
    )
    # --- end exec-rust dispatch ---
    if stack is None:
        stack = _charge_stack_py(
            trade.entry_bar_open_paisa,
            trade.exit_bar_open_paisa,
            is_long=trade.side.startswith("long"),
            lot_size=n_shares_per_lot,
            n_lots=trade.n_lots,
            brokerage_round_trip_paisa=per_leg_brokerage * 2,
            stt_rate=stt_rate_bps_x100,
            etc_rate=exch_rate_bps_x100,
            sebi_rate=C.SEBI_FEE_BPS_X100,
            ipft_rate=ipft_rate_bps_x100,
            stamp_rate=C.STAMP_DUTY_OPTIONS_BPS_X100,
            gst_rate=C.GST_TOTAL_BPS_X100,
        )

    return TradeCostBreakdown(
        n_shares_per_lot=n_shares_per_lot,
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
        # Always False: the Step-0 guard rejects every non-NORMAL_CLOSE
        # outcome, so the exercise branch (§4.2) is never reached here.
        used_exercise_branch=False,
        n_orders_per_leg=1,  # iceberg slicing not yet implemented (§6b deferred)
        # The lot size above was keyed by the TRADE date, not the contract's
        # vintage — the honest basis stamp (see _lookup_lot_size's limitation).
        lot_size_basis="trade_date",
    )
