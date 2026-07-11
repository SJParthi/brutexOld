//! Futures post-I/O materialize tail (DEC-EXEC-RUST-CORE-001, MIGRATION.md
//! #1724) — the verdict core of
//! `brutex.futures.executor.materialize_futures_trade` AFTER
//! `source.load_window` returned a bar window.
//!
//! Gates 1–4 of the PINNED reject order (lot_size → cost_regime →
//! no_contract → no_data) stay in Python (lazy date gates + I/O probes).
//! THIS core owns gate 5 — STALE_BAR, OPT-IN only (`reject_stale=false`
//! default FILLS volume-0 printed bars, the R14/R15 law) — then the charge
//! stack and the R22 MAE/MFE excursions over the contract's OWN minute path
//! (`_excursions_paisa` twin, executor.py:590-613). Path highs/lows arrive
//! as i64 paisa (the `rupees_to_paisa` snap stays in Python).

use crate::exec::costs_futures::futures_charge_stack;
use crate::exec::costs_options::{ChargeStack, to_i64};

/// A priced futures trade: the charge stack + notional MAE/MFE excursions.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FuturesOutcome {
    pub stack: ChargeStack,
    pub mae_paisa: i64,
    pub mfe_paisa: i64,
}

/// The futures post-I/O verdict: the opt-in stale reject, or the priced trade.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BarVerdictFutures {
    /// volume == 0 on the entry or exit bar (rejected iff `reject_stale`).
    StaleBar,
    /// Priced — stack + excursions.
    Trade(FuturesOutcome),
}

/// Direction-aware per-unit (MAE, MFE) over the printed minute path,
/// measured against the ENTRY FILL, running-max floored at 0 (init 0),
/// THEN × `n_shares` — the `_excursions_paisa` twin.
fn excursions_paisa(
    path_highs_paisa: &[i64],
    path_lows_paisa: &[i64],
    is_long: bool,
    entry_fill_paisa: i64,
    n_shares: i64,
) -> Result<(i64, i64), String> {
    let entry = i128::from(entry_fill_paisa);
    let mut mae_unit: i128 = 0;
    let mut mfe_unit: i128 = 0;
    for (&high, &low) in path_highs_paisa.iter().zip(path_lows_paisa.iter()) {
        let (favorable, adverse) = if is_long {
            (i128::from(high) - entry, entry - i128::from(low))
        } else {
            (entry - i128::from(low), i128::from(high) - entry)
        };
        mfe_unit = mfe_unit.max(favorable);
        mae_unit = mae_unit.max(adverse);
    }
    let mae = to_i64(mae_unit * i128::from(n_shares), "futures excursions: MAE")?;
    let mfe = to_i64(mfe_unit * i128::from(n_shares), "futures excursions: MFE")?;
    Ok((mae, mfe))
}

/// The futures materialize tail: stale (opt-in) → fills → cost-free/bearing
/// charge stack → MAE/MFE. The excursion basis fill is `buy_fill` for a long
/// and `sell_fill` for a short (executor.py:731).
///
/// # Errors
///
/// `Err` on mismatched path high/low lengths or arithmetic overflow
/// (out-of-envelope inputs — the Python seam routes them to the Python arm).
#[allow(clippy::too_many_arguments)]
pub fn futures_materialize_core(
    entry_open_paisa: i64,
    exit_open_paisa: i64,
    entry_volume: i64,
    exit_volume: i64,
    path_highs_paisa: &[i64],
    path_lows_paisa: &[i64],
    is_long: bool,
    reject_stale: bool,
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
) -> Result<BarVerdictFutures, String> {
    if path_highs_paisa.len() != path_lows_paisa.len() {
        return Err(format!(
            "futures_materialize_core: path highs ({}) and lows ({}) length mismatch",
            path_highs_paisa.len(),
            path_lows_paisa.len()
        ));
    }
    // Gate 5 — STALE_BAR (OPT-IN only; the default fills volume-0 bars).
    if reject_stale && (entry_volume == 0 || exit_volume == 0) {
        return Ok(BarVerdictFutures::StaleBar);
    }
    let stack = futures_charge_stack(
        entry_open_paisa,
        exit_open_paisa,
        is_long,
        lot_size,
        n_lots,
        cost_free,
        brokerage_flat_paisa,
        brokerage_turnover_bps_x100,
        stt_rate,
        etc_rate,
        sebi_rate,
        ipft_rate,
        stamp_rate,
        gst_rate,
    )?;
    let entry_fill = if is_long {
        stack.buy_fill_paisa
    } else {
        stack.sell_fill_paisa
    };
    let (mae_paisa, mfe_paisa) = excursions_paisa(
        path_highs_paisa,
        path_lows_paisa,
        is_long,
        entry_fill,
        stack.n_shares,
    )?;
    Ok(BarVerdictFutures::Trade(FuturesOutcome {
        stack,
        mae_paisa,
        mfe_paisa,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn golden_nifty_bull_full_trade() {
        // tests/golden/test_futures_executor_golden.py
        // TestAcceptPathGolden::test_full_trade_golden_field_for_field:
        // NIFTY BULL lot 75, ₹22,000.00 → ₹22,100.00, path bars
        // (entry 22000/22010/21990/…, mid 22050/22080/22040, exit 22100/…):
        // gross = net = 749_250; mae = 75_375 (1_005 × 75);
        // mfe = 824_625 (10_995 × 75).
        let highs = [2_201_000, 2_208_000, 2_211_000];
        let lows = [2_199_000, 2_204_000, 2_209_000];
        let v = futures_materialize_core(
            2_200_000, 2_210_000, 100, 100, &highs, &lows, true, false, 75, 1, true, 0, None, 0, 0,
            0, 0, 0, 0,
        )
        .unwrap();
        let BarVerdictFutures::Trade(out) = v else {
            panic!("expected Trade, got {v:?}");
        };
        assert_eq!(out.stack.buy_fill_paisa, 2_200_005);
        assert_eq!(out.stack.sell_fill_paisa, 2_209_995);
        assert_eq!(out.stack.gross_pnl_paisa, 749_250);
        assert_eq!(out.stack.net_pnl_paisa, 749_250);
        assert_eq!(out.stack.total_charges_paisa, 0);
        // MAE: worst adverse = 2_200_005 − 2_199_000 = 1_005 per unit × 75.
        assert_eq!(out.mae_paisa, 75_375);
        // MFE: best favorable = 2_211_000 − 2_200_005 = 10_995 per unit × 75.
        assert_eq!(out.mfe_paisa, 824_625);
    }

    #[test]
    fn excursions_floor_at_zero_and_short_mirror() {
        // A path that never moves favorably keeps MFE at the 0 init floor.
        let highs = [1_000];
        let lows = [900];
        let v = futures_materialize_core(
            1_000, 950, 1, 1, &highs, &lows, true, false, 10, 1, true, 0, None, 0, 0, 0, 0, 0, 0,
        )
        .unwrap();
        let BarVerdictFutures::Trade(out) = v else {
            panic!("expected Trade");
        };
        // entry fill 1_005 (long): favorable = 1_000 − 1_005 = −5 → floored 0;
        // adverse = 1_005 − 900 = 105 → × 10.
        assert_eq!(out.mfe_paisa, 0);
        assert_eq!(out.mae_paisa, 1_050);
        // Short mirror on the same path: entry fill = sell 995;
        // favorable = 995 − 900 = 95; adverse = 1_000 − 995 = 5.
        let v = futures_materialize_core(
            1_000, 950, 1, 1, &highs, &lows, false, false, 10, 1, true, 0, None, 0, 0, 0, 0, 0, 0,
        )
        .unwrap();
        let BarVerdictFutures::Trade(out) = v else {
            panic!("expected Trade");
        };
        assert_eq!(out.mfe_paisa, 950);
        assert_eq!(out.mae_paisa, 50);
    }

    #[test]
    fn stale_bar_opt_in_only() {
        let highs = [1_000];
        let lows = [900];
        // Default (reject_stale=false): volume-0 printed bars FILL (R14/R15).
        assert!(matches!(
            futures_materialize_core(
                1_000, 950, 0, 0, &highs, &lows, true, false, 10, 1, true, 0, None, 0, 0, 0, 0, 0,
                0,
            )
            .unwrap(),
            BarVerdictFutures::Trade(_)
        ));
        // Opt-in: entry OR exit volume-0 rejects.
        for (ev, xv) in [(0, 100), (100, 0)] {
            assert_eq!(
                futures_materialize_core(
                    1_000, 950, ev, xv, &highs, &lows, true, true, 10, 1, true, 0, None, 0, 0, 0,
                    0, 0, 0,
                )
                .unwrap(),
                BarVerdictFutures::StaleBar
            );
        }
    }

    #[test]
    fn mismatched_path_lengths_err() {
        assert!(
            futures_materialize_core(
                1_000,
                950,
                1,
                1,
                &[1_000, 1_100],
                &[900],
                true,
                false,
                10,
                1,
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
            /// MAE/MFE ≥ 0 always, and SUFFIX-MONOTONE: extending the path
            /// can only grow (never shrink) either excursion — the running
            /// max law (ROLL-9 family).
            #[test]
            fn excursions_nonneg_and_suffix_monotone(
                entry in 5i64..1_000_000,
                exit in 5i64..1_000_000,
                is_long in proptest::bool::ANY,
                bars in proptest::collection::vec((5i64..1_000_000, 5i64..1_000_000), 1..20),
                extra in (5i64..1_000_000, 5i64..1_000_000),
            ) {
                let highs: Vec<i64> = bars.iter().map(|&(h, l)| h.max(l)).collect();
                let lows: Vec<i64> = bars.iter().map(|&(h, l)| h.min(l)).collect();
                let run = |hs: &[i64], ls: &[i64]| -> (i64, i64) {
                    match futures_materialize_core(
                        entry, exit, 1, 1, hs, ls, is_long, false, 10, 1, true,
                        0, None, 0, 0, 0, 0, 0, 0,
                    ).unwrap() {
                        BarVerdictFutures::Trade(out) => (out.mae_paisa, out.mfe_paisa),
                        BarVerdictFutures::StaleBar => unreachable!("stale is opt-in"),
                    }
                };
                let (mae, mfe) = run(&highs, &lows);
                prop_assert!(mae >= 0 && mfe >= 0);
                let mut highs2 = highs.clone();
                let mut lows2 = lows.clone();
                highs2.push(extra.0.max(extra.1));
                lows2.push(extra.0.min(extra.1));
                let (mae2, mfe2) = run(&highs2, &lows2);
                prop_assert!(mae2 >= mae);
                prop_assert!(mfe2 >= mfe);
            }
        }
    }
}
