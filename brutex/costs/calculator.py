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
    ChargeStack,
    TradeCostBreakdown,
    TradeInput,
    UnsupportedTradeOutcomeError,
    require_bar_extremes,
)
from brutex.options.lot_size_history import OptionsUnderlying, get_lot_size

# ChargeStack is re-exported (via __all__) for the historical consumers
# that import it from here; its HOME is the stdlib-only leaf
# brutex.costs.types (the CodeQL py/cyclic-import fix — see the leaf's
# class docstring).
__all__ = ["ChargeStack", "compute_trade_cost"]


# ─────────────────────────────────────────────────────────────────
# Rounding helpers (per CALCULATOR_SPEC §4.1)
# ─────────────────────────────────────────────────────────────────


def _round_levy_paisa(amount_x_bps_x100: int) -> int:
    """ADVERSE (ceiling) paisa rounding for a rate-scaled levy product.

    Rate constants in `brutex.costs.constants` use the encoding
    ``rate_decimal × 10_000_000``. So a 0.03503% rate is stored as
    3503, and ``rate_decimal = 3503 / 10_000_000 = 0.0003503``.

    DEC-FILL-WORST-CASE-ONLY-001 (retaining the absorbed #1522 design's
    fix-round-2 adverse-levy-rounding law as the PERMANENT rule): every
    rate-scaled levy in the cost stack rounds ADVERSELY — ceiling toward
    +∞; a levy is a charge, so "adverse" = larger. The retired PRECISE
    nearest-paisa half-up arm is DELETED with its mode. Conservative by
    construction: real exchanges round statutorily half-up, so the
    ceiled backtest levy is ≥ the real charge — the backtest can only
    UNDERSTATE the net, never flatter it (the operator's
    never-more-optimistic-than-reality law).
    """
    # Ceiling toward +∞ for any sign: -((-x) // u) == ceil(x / u).
    return -((-amount_x_bps_x100) // 10_000_000)


def _round_levy_rupee(amount_paisa: int) -> int:
    """ADVERSE (ceiling) statutory rupee rounding (STT / stamp duty / GST).

    DEC-FILL-WORST-CASE-ONLY-001: the statutory levy rounds to the NEXT
    whole rupee (100 paise), toward +∞ — the permanent adverse-rounding
    law (see :func:`_round_levy_paisa` for the rationale; the retired
    PRECISE nearest-rupee half-up arm is deleted with its mode).

    Example: 1101 paisa (₹11.01) → 1200 paisa (₹12); 1100 stays 1100.
    """
    # Ceiling to the next whole rupee (100 paise) toward +∞ for any sign.
    return -((-amount_paisa) // 100) * 100


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
# Slippage application (TRACK2_OPTIONS_SPEC §9 — flat 1-tick adverse
# ON TOP of the worst-case bar-extreme anchor,
# DEC-FILL-WORST-CASE-ONLY-001)
# ─────────────────────────────────────────────────────────────────


def _apply_slippage(
    side: str,
    *,
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
) -> tuple[int, int, int]:
    """Apply 1-tick adverse slippage per side around the worst-case anchor.

    Long side: buy 1 tick higher, sell 1 tick lower.
    Short side: sell 1 tick lower, buy 1 tick higher (cover).

    The ANCHOR each tick applies to is the option's own minute-bar ADVERSE
    EXTREME — the ONLY fill law (DEC-FILL-WORST-CASE-ONLY-001; the retired
    PRECISE bar-OPEN anchor is deleted with its mode): a buy anchors on the
    bar HIGH, a sell on the bar LOW (long: entry HIGH + tick / exit LOW −
    tick; short mirrored: opening sell at entry LOW − tick, cover at exit
    HIGH + tick — the DEC-TRACK2-COSTS-SPEC-RECONCILE-001 §8 adverse-both
    rule, production-unreachable under DEC-EXEC-008 BUY-only). Keeping the
    SAME ± tick ON TOP of the extreme preserves the tick discipline: on a
    flat bar (``high == open``) a bare buy-at-HIGH would be one tick
    BETTER than the retired precise fill — with the tick it is exactly
    equal, so the worst-case fill can never flatter. (The worst-case buy
    therefore deliberately prices one tick OUTSIDE the printed range — the
    honest adverse floor, not a printable price.)

    Returns ``(buy_fill_paisa, sell_fill_paisa, realized_slippage_per_share)``.

    For Track 2 (BUY-only, per DEC-EXEC-008), only the long branch
    fires. The short branch is kept for forward-compat.

    Every returned fill is floored at one tick (MINOR-3): a SELL leg
    anchored on a sub-2-tick bar LOW would otherwise slip to a ₹0.00 sell
    fill (a "free" sale), which is economically impossible — nothing
    trades below one tick. The floor only affects those degenerate
    tick-boundary cases; the normal path (prices ≥ 2 ticks) is unchanged.

    ``realized_slippage_per_share`` is the slippage TRULY baked into the
    post-floor fills (NIT-1): the buy leg's adverse rise from its raw
    anchor plus the sell leg's adverse drop to its raw anchor, both
    measured against the floored fills. On the normal path (fills ≥ 2
    ticks) it equals the nominal ``2 × tick``; when the 1-tick floor fires
    on the sell leg the realized sell-leg movement is 0, so the per-share
    value is exactly ``1 × tick`` — never over-stating the slippage that
    the notionals/PnL actually carry. The open→extreme move is
    fill-ANCHOR semantics carried by the gross P&L, not slippage.
    """
    tick = C.TICK_PAISA
    if side.startswith("long"):
        # buy leg ← entry bar HIGH, sell leg ← exit bar LOW.
        buy_raw, sell_raw = entry_high_paisa, exit_low_paisa
    else:
        # Short (production-unreachable under DEC-EXEC-008): opening sell
        # anchors the entry LOW, the cover anchors the exit HIGH — adverse
        # on both legs (the resolved "# CHECK SIGN" law).
        buy_raw, sell_raw = exit_high_paisa, entry_low_paisa
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


def _charge_stack_py(
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
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
    MIGRATION.md #1963): everything from the moment prices are integer paisa —
    the worst-case bar-extreme fills (DEC-FILL-WORST-CASE-ONLY-001), notionals,
    the full charge stack under the permanent ADVERSE (ceiling) levy rounding,
    gross/net — with every RATE resolved by the caller and passed as an
    argument (rate encoding ``rate_decimal × 10_000_000``;
    ``brokerage_round_trip_paisa`` is the already-resolved flat round-trip).
    The body is the byte-identical extraction of the inline steps of
    :func:`compute_trade_cost` under main's #1825 fill/rounding law (the
    original #1443 extraction was re-derived on top of that law at the club-R1
    merge-reconcile — the coordinator's main-wins-structurally ruling); the
    Python body stays the permanent oracle for the Rust twin
    (``fno_math.exec_option_charge_stack`` — deletion is a distinct later
    decision, DEC-EXEC-PY-BODY-DELETE-001).

    Rounding-order laws (DEC-FILL-WORST-CASE-ONLY-001 — the permanent
    adverse-levy rule; see the step comments): statutory STT/stamp/GST =
    floor-divide by 10^7 THEN ceiling to the rupee; non-statutory
    ETC/SEBI/IPFT = ceiling to the paisa PER LEG then summed; GST base = the
    SUM of the already-rounded service components (STT + stamp excluded),
    rounded ONCE (DEC-COST-001).
    """
    # ── Step 1: apply slippage (1-tick flat adverse, around the worst-case
    #    bar-extreme anchor — DEC-FILL-WORST-CASE-ONLY-001) ───────────────
    # The "long" literal below is deliberately UN-pragma'd — it is a
    # KILLABLE mutant (_apply_slippage branches on startswith("long"), so
    # mutating it routes a long trade through the SHORT fill law; killed by
    # every long-trade golden).
    # SIM108 suppressed: the mutation pragma must scope exactly ONE literal,
    # so the two literals need distinct source lines (a ternary puts both on
    # the pragma'd line — the review-round-1 over-suppression this fixes).
    if is_long:  # noqa: SIM108
        side = "long"
    else:
        # ONLY the else-literal is provably equivalent: any NON-long string
        # takes the same startswith("long") == False branch as "short".
        side = "short"  # pragma: no mutate
    buy_fill_paisa, sell_fill_paisa, realized_slip_per_share = _apply_slippage(
        side,
        entry_high_paisa=entry_high_paisa,
        entry_low_paisa=entry_low_paisa,
        exit_high_paisa=exit_high_paisa,
        exit_low_paisa=exit_low_paisa,
    )

    # ── Step 3: notionals (paise) — lot size resolved by the caller ─────
    n_shares = n_lots * lot_size
    buy_notional_paisa = buy_fill_paisa * n_shares
    sell_notional_paisa = sell_fill_paisa * n_shares

    # ── Step 4: brokerage (per-order flat, both legs — caller-resolved) ─
    brokerage_paisa = brokerage_round_trip_paisa

    # ── Step 5: STT (sell-side premium only) ────────────────────────────
    stt_paisa_raw = (sell_notional_paisa * stt_rate) // 10_000_000
    stt_paisa = _round_levy_rupee(stt_paisa_raw)

    # ── Step 6: exchange transaction charge (both sides on premium) ─────
    exch_buy_paisa = _round_levy_paisa(buy_notional_paisa * etc_rate)
    exch_sell_paisa = _round_levy_paisa(sell_notional_paisa * etc_rate)
    exch_paisa = exch_buy_paisa + exch_sell_paisa

    # ── Step 7: SEBI turnover fee (both sides) ──────────────────────────
    sebi_buy_paisa = _round_levy_paisa(buy_notional_paisa * sebi_rate)
    sebi_sell_paisa = _round_levy_paisa(sell_notional_paisa * sebi_rate)
    sebi_paisa = sebi_buy_paisa + sebi_sell_paisa

    # ── Step 8: IPFT (both sides; NSE only — BSE near-zero) ─────────────
    ipft_buy_paisa = _round_levy_paisa(buy_notional_paisa * ipft_rate)
    ipft_sell_paisa = _round_levy_paisa(sell_notional_paisa * ipft_rate)
    ipft_paisa = ipft_buy_paisa + ipft_sell_paisa

    # ── Step 9: stamp duty (buy leg only, statutory rupee rounding) ─────
    stamp_paisa_raw = (buy_notional_paisa * stamp_rate) // 10_000_000
    stamp_paisa = _round_levy_rupee(stamp_paisa_raw)

    # ── Step 10: GST 18% on services only (DEC-COST-001) ────────────────
    # Round TOTAL once. Base = sum of ALREADY-ROUNDED components; STT and
    # stamp EXCLUDED (they are transaction taxes, not services).
    gst_base_paisa = brokerage_paisa + exch_paisa + sebi_paisa + ipft_paisa
    gst_paisa_raw = (gst_base_paisa * gst_rate) // 10_000_000
    gst_paisa = _round_levy_rupee(gst_paisa_raw)

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

    # ── Step 1: apply slippage (1-tick flat adverse, around the worst-case
    #    bar-extreme anchor — DEC-FILL-WORST-CASE-ONLY-001). The TradeInput
    #    validated all four extremes non-None at construction; the COMMON
    #    `require_bar_extremes` seam narrows the Optional typing (a None here
    #    is a broken dataclass invariant, not a reachable state — it raises
    #    the explicit AssertionError, never a bare `assert` per bandit B101).
    entry_high_paisa, entry_low_paisa, exit_high_paisa, exit_low_paisa = require_bar_extremes(
        entry_bar_high_paisa=trade.entry_bar_high_paisa,
        entry_bar_low_paisa=trade.entry_bar_low_paisa,
        exit_bar_high_paisa=trade.exit_bar_high_paisa,
        exit_bar_low_paisa=trade.exit_bar_low_paisa,
    )
    # ── Step 2: lookup lot size ─────────────────────────────────────────
    # (Step 1 — slippage — is pure and raise-free, so it runs inside the
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
    # Lazy import (the materialize_trade lazy-import precedent): the seam is
    # a costs-free LEAF (it imports the shared ChargeStack shape from
    # brutex.costs.types, never this module — the CodeQL py/cyclic-import
    # fix), so this import is DEFERRAL, not cycle-avoidance: it keeps the
    # seam's engine-package + one-time fno_math import cost off plain
    # `import brutex.costs`, landing it on the first dispatch instead.
    # Default-OFF (`BRUTEX_EXEC_RUST`): the seam returns None unless the
    # kernel actually serves; the Python core below stays the permanent
    # oracle arm (DEC-EXEC-PY-BODY-DELETE-001 owns any later deletion
    # decision).
    from brutex import exec_rust

    stack = exec_rust.charge_stack_option(
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
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
            entry_high_paisa,
            entry_low_paisa,
            exit_high_paisa,
            exit_low_paisa,
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
