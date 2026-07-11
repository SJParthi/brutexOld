//! R15 fill laws (DEC-EXEC-RUST-CORE-001, MIGRATION.md #1724).
//!
//! Integer twins of `brutex.costs.calculator._apply_slippage`
//! (calculator.py:127-186) and
//! `brutex.costs.futures_costs.apply_futures_fill` (futures_costs.py:478-536).
//! Since the DEC-TRACK2-COSTS-SPEC-RECONCILE-001 short-sign re-pin (the §8
//! ADVERSE rule — the 10_005→9_995 family) the two Python fill laws are
//! NUMERICALLY IDENTICAL: both map `long: buy←entry, sell←exit` and
//! `short: buy←exit, sell←entry`, then `buy = raw + tick`, `sell = raw −
//! tick`, floor both at one tick, and recompute the realized slippage from
//! the POST-floor fills. One Rust body therefore serves both names (§11
//! COMMON); the golden fill grids pin each name independently.

/// ₹0.05 option/futures tick in paisa — mirrors
/// `brutex.costs.constants.TICK_PAISA` (constants.py:35).
pub const TICK_PAISA: i64 = 5;

/// `_apply_slippage` twin: 1-tick ADVERSE fill per side over the entry/exit
/// minute-bar OPENs, both fills floored at one tick.
///
/// * `is_long`: buy ← entry, sell ← exit.
/// * short: buy (cover) ← exit, sell (opening) ← entry — ADVERSE both legs
///   (the reconciled law: entry SELL at `entry − tick`).
///
/// Returns `(buy_fill_paisa, sell_fill_paisa, realized_slippage_per_share)`
/// where the realized slippage is
/// `|buy_fill − buy_raw| + |sell_raw − sell_fill|` measured POST-floor
/// (2 × tick normally; a floored degenerate leg contributes less — never
/// over-stated).
///
/// # Errors
///
/// `Err` only when a fill or the realized slippage overflows i64 (the ± tick
/// at the i64 edge — production inputs are ≥ 1 tick and ≪ the edge; the
/// Python arm handles anything the envelope guard rejects).
pub fn apply_slippage(
    entry_open_paisa: i64,
    exit_open_paisa: i64,
    is_long: bool,
) -> Result<(i64, i64, i64), String> {
    let tick = i128::from(TICK_PAISA);
    let (buy_raw, sell_raw) = if is_long {
        (i128::from(entry_open_paisa), i128::from(exit_open_paisa))
    } else {
        (i128::from(exit_open_paisa), i128::from(entry_open_paisa))
    };
    // ± one tick adverse, then floor every fill at one tick (nothing trades
    // below ₹0.05) — i128 arithmetic, so the i64 edge cannot wrap here.
    let buy_fill = (buy_raw + tick).max(tick);
    let sell_fill = (sell_raw - tick).max(tick);
    // Realized per-share slippage = the adverse movement TRULY baked into the
    // floored fills (a floored leg counts its real, smaller movement).
    let realized = (buy_fill - buy_raw).abs() + (sell_raw - sell_fill).abs();
    let buy = i64::try_from(buy_fill)
        .map_err(|_| format!("apply_slippage: buy fill {buy_fill} overflows i64"))?;
    let sell = i64::try_from(sell_fill)
        .map_err(|_| format!("apply_slippage: sell fill {sell_fill} overflows i64"))?;
    let slip = i64::try_from(realized)
        .map_err(|_| format!("apply_slippage: realized slippage {realized} overflows i64"))?;
    Ok((buy, sell, slip))
}

/// `apply_futures_fill` twin (futures_costs.py:478-536).
///
/// Delegates to [`apply_slippage`]: post-reconcile the two Python laws are
/// numerically identical (`long` byte-equal by the Python golden parity pin;
/// `short` = sell ← entry − tick, buy ← exit + tick on both). The Python
/// side keeps two independent MECHANISMS (an options↔futures independence
/// law); the Rust twin they are both compared against is deliberately ONE
/// body (§11 COMMON) — the per-name golden grids in the test module keep
/// either name from drifting.
///
/// # Errors
///
/// As [`apply_slippage`].
pub fn apply_futures_fill(
    entry_open_paisa: i64,
    exit_open_paisa: i64,
    is_long: bool,
) -> Result<(i64, i64, i64), String> {
    apply_slippage(entry_open_paisa, exit_open_paisa, is_long)
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── Byte pins from the Python oracles ────────────────────────────────

    #[test]
    fn long_fill_family() {
        // test_costs_calculator §12.1 family: long (10_000, 12_000) →
        // (10_005, 11_995, 10).
        assert_eq!(
            apply_slippage(10_000, 12_000, true),
            Ok((10_005, 11_995, 10))
        );
        // COSTS_VERIFIED Ex.1 fills.
        assert_eq!(apply_slippage(10_000, 12_000, true).unwrap().0, 10_005);
        // Zero-gross degenerate: 10_000 → 10_000 gives (10_005, 9_995).
        assert_eq!(
            apply_slippage(10_000, 10_000, true),
            Ok((10_005, 9_995, 10))
        );
    }

    #[test]
    fn short_fill_family_the_10_005_to_9_995_class() {
        // The reconciled ADVERSE short (DEC-TRACK2-COSTS-SPEC-RECONCILE-001):
        // short (10_000, 12_000) → buy 12_005, sell 9_995.
        assert_eq!(
            apply_slippage(10_000, 12_000, false),
            Ok((12_005, 9_995, 10))
        );
    }

    #[test]
    fn futures_fill_grid_golden() {
        // tests/golden/test_futures_executor_golden.py long grid — the long
        // arm is byte-equal to the calculator slippage by construction.
        let grid: [(i64, i64); 7] = [
            (5, 5),
            (5, 10),
            (10, 5),
            (100, 100),
            (2_200_000, 2_210_000),
            (2_210_000, 2_200_000),
            (123_455, 987_655),
        ];
        for (entry, exit) in grid {
            assert_eq!(
                apply_futures_fill(entry, exit, true),
                apply_slippage(entry, exit, true),
                "long grid ({entry}, {exit})"
            );
        }
        // The hand-derived SHORT golden table (test_futures_executor_golden):
        // (buy_fill, sell_fill, realized_slip).
        let short_expect: [((i64, i64), (i64, i64, i64)); 7] = [
            ((5, 5), (10, 5, 5)),
            ((5, 10), (15, 5, 5)),
            ((10, 5), (10, 5, 10)),
            ((100, 100), (105, 95, 10)),
            ((2_200_000, 2_210_000), (2_210_005, 2_199_995, 10)),
            ((2_210_000, 2_200_000), (2_200_005, 2_209_995, 10)),
            ((123_455, 987_655), (987_660, 123_450, 10)),
        ];
        for ((entry, exit), want) in short_expect {
            assert_eq!(
                apply_futures_fill(entry, exit, false),
                Ok(want),
                "short grid ({entry}, {exit})"
            );
        }
    }

    #[test]
    fn one_tick_floor_and_realized_recompute() {
        // A one-tick exit open would sell at ₹0.00 without the floor; the
        // floored sell leg truly moved 0 → realized = 1 × tick.
        assert_eq!(
            apply_slippage(100, TICK_PAISA, true),
            Ok((105, TICK_PAISA, TICK_PAISA))
        );
        // Short entry at one tick: opening sell floored, realized 1 × tick.
        assert_eq!(
            apply_slippage(TICK_PAISA, 100, false),
            Ok((105, TICK_PAISA, TICK_PAISA))
        );
        // entry = exit = 5 → buy 10, sell 5 (both laws' pinned edge).
        assert_eq!(apply_slippage(5, 5, true), Ok((10, 5, 5)));
        // Sub-2-tick sell raw (7): fill floors to 5, realized 5 + 2 = 7 —
        // the [5, 10] realized band, NOT a {5, 10} two-point set.
        assert_eq!(apply_slippage(10_000, 7, true), Ok((10_005, 5, 7)));
    }

    #[test]
    fn i64_edge_errs_not_panics() {
        // + tick at the i64 edge must Err (Python bigints would carry on —
        // the seam's envelope guard routes these to the Python arm).
        assert!(apply_slippage(i64::MAX, 10, true).is_err());
        assert!(apply_slippage(10, i64::MAX, false).is_err());
        // Realized slippage overflow from a deeply negative raw.
        assert!(apply_slippage(10, i64::MIN, true).is_err());
        // The edge minus one tick still computes.
        assert!(apply_slippage(i64::MAX - TICK_PAISA, 10, true).is_ok());
    }

    mod props {
        use super::super::*;
        use proptest::prelude::*;

        proptest! {
            /// Over the production envelope (opens ≥ 1 tick): fills ≥ tick
            /// and realized ∈ [tick, 2·tick] (the floored-leg band).
            #[test]
            fn fills_floored_and_realized_band(
                entry in 5i64..10_000_000,
                exit in 5i64..10_000_000,
                is_long in proptest::bool::ANY,
            ) {
                let (buy, sell, slip) = apply_slippage(entry, exit, is_long).unwrap();
                prop_assert!(buy >= TICK_PAISA);
                prop_assert!(sell >= TICK_PAISA);
                prop_assert!(slip >= TICK_PAISA && slip <= 2 * TICK_PAISA);
                // The buy leg can never fill below its raw (adverse up).
                let buy_raw = if is_long { entry } else { exit };
                prop_assert!(buy >= buy_raw);
            }

            /// The futures name never diverges from the calculator name.
            #[test]
            fn futures_fill_equals_slippage(
                entry in 5i64..10_000_000,
                exit in 5i64..10_000_000,
                is_long in proptest::bool::ANY,
            ) {
                prop_assert_eq!(
                    apply_futures_fill(entry, exit, is_long).unwrap(),
                    apply_slippage(entry, exit, is_long).unwrap()
                );
            }
        }
    }
}
