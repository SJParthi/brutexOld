//! Futures charge stack (DEC-EXEC-RUST-CORE-001, MIGRATION.md #1724) — the
//! exact-integer twin of `brutex.costs.futures_costs._futures_charge_stack_py`
//! (futures_costs.py:722-847 pre-extraction).
//!
//! The `cost_free` verdict and EVERY rate/refusal decision are resolved by
//! the Python caller (the refusal order STT → ETC → NSE-IPFT → stamp raises
//! there — `UnverifiedCostRegimeError` never crosses this FFI); this core is
//! TOTAL. A cost-free INDEX future short-circuits to the all-zero stack
//! (`net == gross`) WITHOUT touching any rate argument, so a pre-2024 index
//! trade prices fine with zeroed rate args.

use crate::exec::costs_options::{ChargeStack, add_i64, to_i64};
use crate::exec::fills::apply_futures_fill;
use crate::exec::money::{floor_rate_scale, gst_paisa, round_to_paisa, round_to_rupee};

/// One-leg brokerage from caller-RESOLVED broker parameters — the
/// `brutex.costs.futures_costs._brokerage_leg_paisa` twin: `None` turnover =
/// a flat-only broker (Zerodha); a rate = the Groww
/// `min(flat, half-up(notional × turnover))` arm.
///
/// # Errors
///
/// `Err` on i128/i64 overflow in the turnover arm (via `round_to_paisa`).
pub fn brokerage_leg_paisa(
    notional_paisa: i64,
    flat_paisa: i64,
    turnover_bps_x100: Option<i64>,
) -> Result<i64, String> {
    match turnover_bps_x100 {
        None => Ok(flat_paisa),
        Some(rate) => {
            let arm = round_to_paisa(i128::from(notional_paisa) * i128::from(rate))?;
            Ok(flat_paisa.min(arm))
        }
    }
}

/// `compute_futures_trade_cost`'s arithmetic twin. Divergences from the
/// options stack, preserved verbatim: `gross = sell_notional − buy_notional`
/// — ONE formula for BOTH directions (the fill law pre-assigns the legs, no
/// sign branch); brokerage = [`brokerage_leg_paisa`] PER LEG instead of the
/// options flat round trip. Same rounding laws otherwise (statutory floor
/// then rupee; non-statutory per-leg paisa half-up; GST round-ONCE,
/// DEC-COST-001).
///
/// # Errors
///
/// `Err` on any i64/i128 overflow (out-of-envelope inputs — the Python seam
/// routes them to the Python-bigint arm) or an internal law violation.
#[allow(clippy::too_many_arguments)]
pub fn futures_charge_stack(
    entry_open_paisa: i64,
    exit_open_paisa: i64,
    is_long: bool,
    lot_size: i64,
    n_lots: i64,
    cost_free: bool,
    brokerage_flat_paisa: i64,
    brokerage_turnover_bps_x100: Option<i64>,
    stt_rate: i64,
    etc_rate: i64,
    sebi_rate: i64,
    ipft_rate: i64,
    stamp_rate: i64,
    gst_rate: i64,
) -> Result<ChargeStack, String> {
    let (buy_fill_paisa, sell_fill_paisa, realized_slip_per_unit) =
        apply_futures_fill(entry_open_paisa, exit_open_paisa, is_long)?;
    let n_shares = to_i64(
        i128::from(n_lots) * i128::from(lot_size),
        "futures_charge_stack: n_shares",
    )?;
    let buy_notional_paisa = to_i64(
        i128::from(buy_fill_paisa) * i128::from(n_shares),
        "futures_charge_stack: buy notional",
    )?;
    let sell_notional_paisa = to_i64(
        i128::from(sell_fill_paisa) * i128::from(n_shares),
        "futures_charge_stack: sell notional",
    )?;
    // ONE gross formula for both directions (legs pre-assigned by the fill).
    let gross_pnl_paisa = sell_notional_paisa
        .checked_sub(buy_notional_paisa)
        .ok_or_else(|| "futures_charge_stack: gross overflows i64".to_string())?;
    let slippage_paisa = to_i64(
        i128::from(realized_slip_per_unit) * i128::from(n_shares),
        "futures_charge_stack: slippage",
    )?;

    if cost_free {
        // INDEX futures: signal-only, ZERO statutory/broker charges
        // (DEC-COST-SCOPE-INDEX-SIGNAL-ONLY-001) — no rate arg touched.
        return ChargeStack {
            buy_fill_paisa,
            sell_fill_paisa,
            n_shares,
            buy_notional_paisa,
            sell_notional_paisa,
            brokerage_paisa: 0,
            stt_paisa: 0,
            exch_paisa: 0,
            sebi_paisa: 0,
            ipft_paisa: 0,
            stamp_paisa: 0,
            gst_paisa: 0,
            slippage_paisa,
            total_charges_paisa: 0,
            gross_pnl_paisa,
            net_pnl_paisa: gross_pnl_paisa,
            realized_slip_per_share: realized_slip_per_unit,
        }
        .validated();
    }

    // Brokerage per executed order (leg): Groww min(flat, turnover) / flat.
    let brokerage_paisa = add_i64(
        brokerage_leg_paisa(
            buy_notional_paisa,
            brokerage_flat_paisa,
            brokerage_turnover_bps_x100,
        )?,
        brokerage_leg_paisa(
            sell_notional_paisa,
            brokerage_flat_paisa,
            brokerage_turnover_bps_x100,
        )?,
        "futures_charge_stack: brokerage",
    )?;
    // STT: sell-side turnover, statutory floor-then-rupee rounding.
    let stt_paisa = round_to_rupee(floor_rate_scale(
        i128::from(sell_notional_paisa),
        i128::from(stt_rate),
    )?);
    // ETC: both sides, per-leg paisa half-up then sum.
    let exch_paisa = add_i64(
        round_to_paisa(i128::from(buy_notional_paisa) * i128::from(etc_rate))?,
        round_to_paisa(i128::from(sell_notional_paisa) * i128::from(etc_rate))?,
        "futures_charge_stack: exch",
    )?;
    // SEBI: both sides.
    let sebi_paisa = add_i64(
        round_to_paisa(i128::from(buy_notional_paisa) * i128::from(sebi_rate))?,
        round_to_paisa(i128::from(sell_notional_paisa) * i128::from(sebi_rate))?,
        "futures_charge_stack: sebi",
    )?;
    // IPFT: both sides.
    let ipft_paisa = add_i64(
        round_to_paisa(i128::from(buy_notional_paisa) * i128::from(ipft_rate))?,
        round_to_paisa(i128::from(sell_notional_paisa) * i128::from(ipft_rate))?,
        "futures_charge_stack: ipft",
    )?;
    // Stamp: buy side, statutory floor-then-rupee rounding.
    let stamp_paisa = round_to_rupee(floor_rate_scale(
        i128::from(buy_notional_paisa),
        i128::from(stamp_rate),
    )?);
    // GST 18% on services only, rounded ONCE on the total (DEC-COST-001).
    let gst = gst_paisa(
        &[brokerage_paisa, exch_paisa, sebi_paisa, ipft_paisa],
        gst_rate,
    )?;

    let total_charges_paisa = to_i64(
        i128::from(brokerage_paisa)
            + i128::from(stt_paisa)
            + i128::from(exch_paisa)
            + i128::from(sebi_paisa)
            + i128::from(ipft_paisa)
            + i128::from(stamp_paisa)
            + i128::from(gst),
        "futures_charge_stack: total charges",
    )?;
    let net_pnl_paisa = gross_pnl_paisa
        .checked_sub(total_charges_paisa)
        .ok_or_else(|| "futures_charge_stack: net overflows i64".to_string())?;

    ChargeStack {
        buy_fill_paisa,
        sell_fill_paisa,
        n_shares,
        buy_notional_paisa,
        sell_notional_paisa,
        brokerage_paisa,
        stt_paisa,
        exch_paisa,
        sebi_paisa,
        ipft_paisa,
        stamp_paisa,
        gst_paisa: gst,
        slippage_paisa,
        total_charges_paisa,
        gross_pnl_paisa,
        net_pnl_paisa,
        realized_slip_per_share: realized_slip_per_unit,
    }
    .validated()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn brokerage_leg_pins() {
        // tests/unit/test_futures_costs.py TestBrokerage pins.
        assert_eq!(
            brokerage_leg_paisa(65_002_500, 2_000, Some(3_000)),
            Ok(2_000)
        ); // cap binds
        assert_eq!(brokerage_leg_paisa(10_000, 2_000, Some(3_000)), Ok(3)); // turnover arm
        assert_eq!(brokerage_leg_paisa(10_000, 2_000, None), Ok(2_000)); // Zerodha flat
    }

    #[test]
    fn cost_free_index_long_golden() {
        // test_futures_costs cost-free NIFTY long (lot 75, 2_200_000 →
        // 2_210_000): gross = net = 749_250, all charges 0, slippage 750 —
        // WITHOUT touching any rate arg (all zeroed here).
        let s = futures_charge_stack(
            2_200_000, 2_210_000, true, 75, 1, true, 0, None, 0, 0, 0, 0, 0, 0,
        )
        .unwrap();
        assert_eq!(s.buy_fill_paisa, 2_200_005);
        assert_eq!(s.sell_fill_paisa, 2_209_995);
        assert_eq!(s.gross_pnl_paisa, 749_250);
        assert_eq!(s.net_pnl_paisa, 749_250);
        assert_eq!(s.total_charges_paisa, 0);
        assert_eq!(s.brokerage_paisa + s.stt_paisa + s.exch_paisa, 0);
        assert_eq!(s.slippage_paisa, 750);
    }

    #[test]
    fn cost_free_index_short_golden() {
        // Short, same prices: gross = net = −750_750.
        let s = futures_charge_stack(
            2_200_000, 2_210_000, false, 75, 1, true, 0, None, 0, 0, 0, 0, 0, 0,
        )
        .unwrap();
        assert_eq!(s.gross_pnl_paisa, -750_750);
        assert_eq!(s.net_pnl_paisa, -750_750);
        assert_eq!(s.total_charges_paisa, 0);
    }

    #[test]
    fn synthetic_cited_cost_bearing_golden() {
        // The RELIANCE synthetic-cited fixture (test_futures_costs.py
        // TestChargePathViaSyntheticCitedFixture, hand-recomputed): lot 500,
        // 130_000 → 131_000, Groww; STT 2_000 (VERIFIED), ETC 173 / IPFT 10 /
        // stamp 200 (SYNTHETIC — never shipped as production rates).
        let s = futures_charge_stack(
            130_000,
            131_000,
            true,
            500,
            1,
            false,
            2_000,
            Some(3_000),
            2_000,
            173,
            10,
            10,
            200,
            1_800_000,
        )
        .unwrap();
        assert_eq!(s.buy_fill_paisa, 130_005);
        assert_eq!(s.sell_fill_paisa, 130_995);
        assert_eq!(s.buy_notional_paisa, 65_002_500);
        assert_eq!(s.sell_notional_paisa, 65_497_500);
        assert_eq!(s.gross_pnl_paisa, 495_000);
        assert_eq!(s.brokerage_paisa, 4_000);
        assert_eq!(s.stt_paisa, 13_100); // 13_099 raw → rupee 13_100
        assert_eq!(s.exch_paisa, 2_258); // 1_125 + 1_133 per leg
        assert_eq!(s.sebi_paisa, 130);
        assert_eq!(s.ipft_paisa, 130);
        assert_eq!(s.stamp_paisa, 1_300);
        assert_eq!(s.gst_paisa, 1_200); // 18% × 6_518 = 1_173 → 1_200
        assert_eq!(s.total_charges_paisa, 22_118);
        assert_eq!(s.net_pnl_paisa, 472_882);
    }

    #[test]
    fn zerodha_flat_arm_golden() {
        // The commit-1 Python regression twin: RELIANCE zerodha lot 1,
        // 10_000 → 11_000 — brokerage stays flat 4_000 (the Groww arm would
        // read 3 + 3 = 6), GST 700, net −3_710.
        let s = futures_charge_stack(
            10_000, 11_000, true, 1, 1, false, 2_000, None, 2_000, 173, 10, 10, 200, 1_800_000,
        )
        .unwrap();
        assert_eq!(s.brokerage_paisa, 4_000);
        assert_eq!(s.stt_paisa, 0);
        assert_eq!(s.exch_paisa, 0);
        assert_eq!(s.gst_paisa, 700);
        assert_eq!(s.total_charges_paisa, 4_700);
        assert_eq!(s.gross_pnl_paisa, 990);
        assert_eq!(s.net_pnl_paisa, -3_710);
    }

    #[test]
    fn overflow_errs_not_panics() {
        assert!(
            futures_charge_stack(
                i64::MAX / 2,
                i64::MAX / 2,
                true,
                i64::MAX,
                i64::MAX,
                true,
                0,
                None,
                0,
                0,
                0,
                0,
                0,
                0,
            )
            .is_err()
        );
    }

    mod props {
        use super::super::*;
        use proptest::prelude::*;

        proptest! {
            /// cost_free ⇒ zero charges ∧ net == gross, rates never consulted
            /// (zeroed args prove untouched-by-construction).
            #[test]
            fn cost_free_zero_charges_net_eq_gross(
                entry in 5i64..10_000_000,
                exit in 5i64..10_000_000,
                is_long in proptest::bool::ANY,
                lot in 1i64..1_000,
                n_lots in 1i64..10_000,
            ) {
                let s = futures_charge_stack(
                    entry, exit, is_long, lot, n_lots, true, 0, None,
                    0, 0, 0, 0, 0, 0,
                ).unwrap();
                prop_assert_eq!(s.total_charges_paisa, 0);
                prop_assert_eq!(s.net_pnl_paisa, s.gross_pnl_paisa);
                prop_assert_eq!(
                    s.brokerage_paisa | s.stt_paisa | s.exch_paisa | s.sebi_paisa
                        | s.ipft_paisa | s.stamp_paisa | s.gst_paisa,
                    0
                );
            }

            /// Cost-bearing laws: net == gross − total; ONE gross formula
            /// (sell − buy notionals) regardless of direction; statutory
            /// levies on whole rupees; brokerage never exceeds 2 × flat.
            #[test]
            fn cost_bearing_laws(
                entry in 5i64..10_000_000,
                exit in 5i64..10_000_000,
                is_long in proptest::bool::ANY,
                lot in 1i64..1_000,
                n_lots in 1i64..10_000,
                turnover in proptest::option::of(0i64..2_000_000),
                stt in 0i64..2_000_000,
                etc in 0i64..2_000_000,
                stamp in 0i64..2_000_000,
            ) {
                let s = futures_charge_stack(
                    entry, exit, is_long, lot, n_lots, false, 2_000, turnover,
                    stt, etc, 10, 50, stamp, 1_800_000,
                ).unwrap();
                prop_assert_eq!(s.net_pnl_paisa, s.gross_pnl_paisa - s.total_charges_paisa);
                prop_assert_eq!(
                    s.gross_pnl_paisa,
                    s.sell_notional_paisa - s.buy_notional_paisa
                );
                prop_assert_eq!(s.stt_paisa % 100, 0);
                prop_assert_eq!(s.stamp_paisa % 100, 0);
                prop_assert_eq!(s.gst_paisa % 100, 0);
                prop_assert!(s.brokerage_paisa <= 2 * 2_000);
            }
        }
    }
}
