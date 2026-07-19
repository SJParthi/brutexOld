//! Futures post-I/O materialize tail (DEC-EXEC-RUST-CORE-001, MIGRATION.md
//! #1960) — the verdict core of
//! `brutex.futures.executor.materialize_futures_trade` AFTER
//! `source.load_window` returned a bar window.
//!
//! Gates 1–4 of the PINNED reject order (lot_size → cost_regime →
//! no_contract → no_data) stay in Python (lazy date gates + I/O probes).
//! THIS core owns gate 5 — STALE_BAR, UNCONDITIONAL under the worst-case
//! fill law (DEC-FILL-WORST-CASE-ONLY-001: a volume-0 printed bar always
//! rejects; the former `reject_stale` opt-in knob is DELETED) — then the
//! charge stack over the fill bars' ADVERSE EXTREMES and the R22 MAE/MFE
//! excursions over the contract's OWN minute path (`_excursions_paisa`
//! twin, executor.py). Path highs/lows arrive as i64 paisa (the
//! `rupees_to_paisa` snap stays in Python).

use crate::exec::costs_futures::futures_charge_stack;
use crate::exec::costs_options::{ChargeStack, to_i64};

/// A priced futures trade: the charge stack + notional MAE/MFE excursions.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FuturesOutcome {
    pub stack: ChargeStack,
    pub mae_paisa: i64,
    pub mfe_paisa: i64,
}

/// The futures post-I/O verdict: the unconditional stale reject, or the
/// priced trade.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BarVerdictFutures {
    /// volume == 0 on the entry or exit bar (ALWAYS rejected —
    /// DEC-FILL-WORST-CASE-ONLY-001).
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

/// The futures materialize tail: stale (unconditional) → worst-case fills
/// off the fill bars' extremes → cost-free/bearing charge stack → MAE/MFE.
/// The excursion basis fill is `buy_fill` for a long and `sell_fill` for a
/// short (executor.py).
///
/// # Errors
///
/// `Err` on mismatched path high/low lengths or arithmetic overflow
/// (out-of-envelope inputs — the Python seam routes them to the Python arm).
#[allow(clippy::too_many_arguments)]
pub fn futures_materialize_core(
    entry_high_paisa: i64,
    entry_low_paisa: i64,
    exit_high_paisa: i64,
    exit_low_paisa: i64,
    entry_volume: i64,
    exit_volume: i64,
    path_highs_paisa: &[i64],
    path_lows_paisa: &[i64],
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
) -> Result<BarVerdictFutures, String> {
    if path_highs_paisa.len() != path_lows_paisa.len() {
        return Err(format!(
            "futures_materialize_core: path highs ({}) and lows ({}) length mismatch",
            path_highs_paisa.len(),
            path_lows_paisa.len()
        ));
    }
    // Gate 5 — STALE_BAR (UNCONDITIONAL — DEC-FILL-WORST-CASE-ONLY-001).
    if entry_volume == 0 || exit_volume == 0 {
        return Ok(BarVerdictFutures::StaleBar);
    }
    let stack = futures_charge_stack(
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
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
    fn golden_nifty_bull_full_trade_worst_case() {
        // Oracle: brutex.costs.futures_costs._futures_charge_stack_py on the
        // merged tree (2026-07-19) — NIFTY BULL lot 75 cost-free; entry bar
        // high/low 22,010.00/21,990.00, exit bar high/low 22,110.00/22,090.00;
        // path bars (highs 22,010/22,080/22,110, lows 21,990/22,040/22,090):
        // buy fill 2_201_005 (entry HIGH + tick), sell fill 2_208_995 (exit
        // LOW − tick); gross = net = 599_250; mae = 150_375 (2_005 × 75);
        // mfe = 749_625 (9_995 × 75).
        let highs = [2_201_000, 2_208_000, 2_211_000];
        let lows = [2_199_000, 2_204_000, 2_209_000];
        let v = futures_materialize_core(
            2_201_000, 2_199_000, 2_211_000, 2_209_000, 100, 100, &highs, &lows, true, 75, 1, true,
            0, None, 0, 0, 0, 0, 0, 0,
        )
        .unwrap();
        let BarVerdictFutures::Trade(out) = v else {
            panic!("expected Trade, got {v:?}");
        };
        assert_eq!(out.stack.buy_fill_paisa, 2_201_005);
        assert_eq!(out.stack.sell_fill_paisa, 2_208_995);
        assert_eq!(out.stack.gross_pnl_paisa, 599_250);
        assert_eq!(out.stack.net_pnl_paisa, 599_250);
        assert_eq!(out.stack.total_charges_paisa, 0);
        // MAE: worst adverse = 2_201_005 − 2_199_000 = 2_005 per unit × 75.
        assert_eq!(out.mae_paisa, 150_375);
        // MFE: best favorable = 2_211_000 − 2_201_005 = 9_995 per unit × 75.
        assert_eq!(out.mfe_paisa, 749_625);
    }

    #[test]
    fn excursions_floor_at_zero_and_short_mirror() {
        // Oracle-derived (2026-07-19): entry bar high/low 1_000/900, exit bar
        // high/low 960/950, lot 10 cost-free.
        // LONG: buy fill 1_005 (entry HIGH + tick), sell fill 945; path bar
        // (1_000, 900): favorable = 1_000 − 1_005 = −5 → floored 0;
        // adverse = 1_005 − 900 = 105 → × 10.
        let highs = [1_000];
        let lows = [900];
        let v = futures_materialize_core(
            1_000, 900, 960, 950, 1, 1, &highs, &lows, true, 10, 1, true, 0, None, 0, 0, 0, 0, 0, 0,
        )
        .unwrap();
        let BarVerdictFutures::Trade(out) = v else {
            panic!("expected Trade");
        };
        assert_eq!(out.stack.buy_fill_paisa, 1_005);
        assert_eq!(out.mfe_paisa, 0);
        assert_eq!(out.mae_paisa, 1_050);
        // SHORT mirror over a 2-bar path: entry fill = sell 895 (entry LOW −
        // tick); bar (1_000, 900): favorable −5 → 0, adverse 105;
        // bar (850, 800): favorable 895 − 800 = 95, adverse −45 → running
        // maxes 95/105 → × 10.
        let highs2 = [1_000, 850];
        let lows2 = [900, 800];
        let v = futures_materialize_core(
            1_000, 900, 960, 950, 1, 1, &highs2, &lows2, false, 10, 1, true, 0, None, 0, 0, 0, 0,
            0, 0,
        )
        .unwrap();
        let BarVerdictFutures::Trade(out) = v else {
            panic!("expected Trade");
        };
        assert_eq!(out.stack.sell_fill_paisa, 895);
        assert_eq!(out.mfe_paisa, 950);
        assert_eq!(out.mae_paisa, 1_050);
    }

    #[test]
    fn stale_bar_unconditional() {
        // DEC-FILL-WORST-CASE-ONLY-001: a volume-0 printed bar ALWAYS
        // rejects — entry OR exit.
        let highs = [1_000];
        let lows = [900];
        for (ev, xv) in [(0, 100), (100, 0), (0, 0)] {
            assert_eq!(
                futures_materialize_core(
                    1_000, 900, 960, 950, ev, xv, &highs, &lows, true, 10, 1, true, 0, None, 0, 0,
                    0, 0, 0, 0,
                )
                .unwrap(),
                BarVerdictFutures::StaleBar
            );
        }
        // Positive volumes price.
        assert!(matches!(
            futures_materialize_core(
                1_000, 900, 960, 950, 1, 1, &highs, &lows, true, 10, 1, true, 0, None, 0, 0, 0, 0,
                0, 0,
            )
            .unwrap(),
            BarVerdictFutures::Trade(_)
        ));
    }

    #[test]
    fn mismatched_path_lengths_err() {
        assert!(
            futures_materialize_core(
                1_000,
                900,
                960,
                950,
                1,
                1,
                &[1_000, 1_100],
                &[900],
                true,
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
                entry_bar in (10i64..1_000_000, 10i64..1_000_000),
                exit_bar in (10i64..1_000_000, 10i64..1_000_000),
                is_long in proptest::bool::ANY,
                bars in proptest::collection::vec((5i64..1_000_000, 5i64..1_000_000), 1..20),
                extra in (5i64..1_000_000, 5i64..1_000_000),
            ) {
                let (eh, el) = (entry_bar.0.max(entry_bar.1), entry_bar.0.min(entry_bar.1));
                let (xh, xl) = (exit_bar.0.max(exit_bar.1), exit_bar.0.min(exit_bar.1));
                let highs: Vec<i64> = bars.iter().map(|&(h, l)| h.max(l)).collect();
                let lows: Vec<i64> = bars.iter().map(|&(h, l)| h.min(l)).collect();
                let run = |hs: &[i64], ls: &[i64]| -> (i64, i64) {
                    match futures_materialize_core(
                        eh, el, xh, xl, 1, 1, hs, ls, is_long, 10, 1, true,
                        0, None, 0, 0, 0, 0, 0, 0,
                    ).unwrap() {
                        BarVerdictFutures::Trade(out) => (out.mae_paisa, out.mfe_paisa),
                        BarVerdictFutures::StaleBar => unreachable!("volumes are positive"),
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
