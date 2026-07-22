//! Property invariants over the pure core (`DEC-RUST-STAGE3-REFINE-001`).
//!
//! The crate-side half of the property battery (the Python-side hypothesis
//! suites drive the SAME laws through the wheel): grid bijection/totality/
//! monotonicity over ARBITRARY valid axes, the driver's hard budget +
//! on-grid + determinism laws over arbitrary seeds/budgets, and the
//! selection differential against the verbatim two-pass reference.

use brutex_refine::{GridAxis, RefineDriver, SelectCore};
use proptest::prelude::*;

/// An arbitrary VALID axis: 1 ≤ lo, 1 ≤ tick ≤ 50, 2 ≤ steps ≤ 120.
fn arb_axis() -> impl Strategy<Value = GridAxis> {
    (1i64..1000, 1i64..50, 2i64..120).prop_map(|(lo, tick, steps)| {
        GridAxis::new("t", lo, lo + tick * (steps - 1), tick).expect("valid by construction")
    })
}

proptest! {
    #[test]
    fn grid_round_trip_identity(axis in arb_axis(), index in 0i64..119) {
        let index = index % axis.steps();
        let paisa = axis.lo_paisa + index * axis.tick_paisa;
        let u = axis.normalize(paisa).unwrap();
        prop_assert!((0.0..=1.0).contains(&u));
        prop_assert_eq!(axis.denormalize(u).unwrap(), paisa);
    }

    #[test]
    fn grid_denormalize_total_and_on_grid(axis in arb_axis(), u in proptest::num::f64::ANY) {
        if u.is_nan() {
            prop_assert!(axis.denormalize(u).is_err());
        } else {
            let paisa = axis.denormalize(u).unwrap();
            prop_assert!(paisa >= axis.lo_paisa && paisa <= axis.hi_paisa);
            prop_assert_eq!((paisa - axis.lo_paisa) % axis.tick_paisa, 0);
        }
    }

    #[test]
    fn grid_denormalize_monotone(axis in arb_axis(), a in -2.0f64..3.0, b in -2.0f64..3.0) {
        let (lo, hi) = if a <= b { (a, b) } else { (b, a) };
        prop_assert!(axis.denormalize(lo).unwrap() <= axis.denormalize(hi).unwrap());
    }

    #[test]
    fn driver_budget_grid_and_determinism(
        seed in proptest::num::u64::ANY,
        max_fevals in 1u64..60,
        max_restarts in 1u32..4,
    ) {
        let run = |seed: u64| {
            let sl = GridAxis::new("sl_paisa", 5, 200, 5).unwrap();
            let tp = GridAxis::new("tp_paisa", 10, 500, 10).unwrap();
            let mut driver = RefineDriver::new(
                [0.25, 0.75], 0.3, seed, max_fevals, max_restarts, sl, tp,
            ).unwrap();
            let mut asked = Vec::new();
            while !driver.done() {
                let pairs = driver.ask().unwrap();
                let values: Vec<f64> = pairs
                    .iter()
                    .map(|&(s, t)| ((s - 100).pow(2) + (t - 250).pow(2)) as f64 / 1e4)
                    .collect();
                asked.extend_from_slice(&pairs);
                driver.tell(&values).unwrap();
            }
            (asked, driver.restarts_run())
        };
        let (asked, restarts) = run(seed);
        // Hard budget: at most max_fevals asks per restart, all restarts run.
        prop_assert!(asked.len() as u64 <= max_fevals * u64::from(max_restarts));
        prop_assert_eq!(restarts, max_restarts);
        // Every proposal on the Mode-A grid.
        for (sl, tp) in &asked {
            prop_assert!((5..=200).contains(sl) && sl % 5 == 0);
            prop_assert!((10..=500).contains(tp) && tp % 10 == 0);
        }
        // Bit-identical replay under the same seed.
        let (again, _) = run(seed);
        prop_assert_eq!(asked, again);
    }

    #[test]
    fn select_matches_two_pass_reference(
        rows in proptest::collection::vec(
            (0u8..3, 0u8..2, -3i32..4, 0u64..12, 0u64..500),
            0..60,
        ),
        top_k in 1usize..8,
    ) {
        // (instrument, tf, sharpe, run_id, pnl-tag) tuples with heavy ties.
        let instruments = ["NIFTY", "BANKNIFTY", "SENSEX"];
        let tfs = ["5m", "15m"];
        let decoded: Vec<(&str, &str, f64, String)> = rows
            .iter()
            .map(|&(i, t, s, r, _)| {
                (
                    instruments[i as usize],
                    tfs[t as usize],
                    f64::from(s),
                    format!("{r:064x}"),
                )
            })
            .collect();
        // Streamed bounded selection.
        let mut core = SelectCore::new(top_k).unwrap();
        for (instr, tf, sharpe, run_id) in &decoded {
            core.push(instr, tf, "A", *sharpe, run_id).unwrap();
        }
        let got = core.finish().unwrap();
        // Verbatim two-pass reference (the retired Python discipline).
        use std::collections::BTreeMap;
        type Key = (String, String, String);
        let mut grouped: BTreeMap<Key, Vec<(f64, String, u64)>> = BTreeMap::new();
        for (seq, (instr, tf, sharpe, run_id)) in decoded.iter().enumerate() {
            grouped
                .entry(((*instr).into(), (*tf).into(), "A".into()))
                .or_default()
                .push((*sharpe, run_id.clone(), seq as u64));
        }
        let mut want = Vec::new();
        for (key, mut board) in grouped {
            board.sort_by(|a, b| a.1.cmp(&b.1).then(a.2.cmp(&b.2)));
            board.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());
            board.truncate(top_k);
            want.push((key, board.into_iter().map(|e| e.2).collect::<Vec<_>>()));
        }
        prop_assert_eq!(got, want);
    }
}
