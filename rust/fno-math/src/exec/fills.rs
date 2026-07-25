//! The worst-case fill law (DEC-EXEC-RUST-CORE-001, MIGRATION.md #2212;
//! DEC-FILL-WORST-CASE-ONLY-001 — worst-case is the ONLY fill model).
//!
//! Integer twins of `brutex.costs.calculator._apply_slippage` and
//! `brutex.costs.futures_costs.apply_futures_fill` under main's #1825 law
//! (re-derived at the club-R1 merge-reconcile — the retired PRECISE
//! bar-OPEN anchor is deleted with its mode): each fill anchors on the fill
//! minute-bar's ADVERSE EXTREME — a buy on the bar HIGH, a sell on the bar
//! LOW — then applies ONE adverse tick on top, both fills floored at one
//! tick. The two Python fill laws are NUMERICALLY IDENTICAL (the §8 ADVERSE
//! rule + the worst-case anchor): both map `long: buy ← entry HIGH,
//! sell ← exit LOW` and `short: buy (cover) ← exit HIGH, sell (opening) ←
//! entry LOW`, and recompute the realized slippage from the POST-floor
//! fills. One Rust body therefore serves both names (§11 COMMON); the
//! golden fill grids pin each name independently. On a FLAT bar
//! (`high == low == open`) the worst-case fill equals the retired
//! open-anchored fill exactly — the proven no-flatter counterexample.

/// ₹0.05 option/futures tick in paisa — mirrors
/// `brutex.costs.constants.TICK_PAISA` (constants.py:35).
pub const TICK_PAISA: i64 = 5;

/// `_apply_slippage` twin: 1-tick ADVERSE fill per side around the
/// worst-case bar-extreme anchors, both fills floored at one tick.
///
/// * `is_long`: buy ← entry HIGH, sell ← exit LOW.
/// * short: buy (cover) ← exit HIGH, sell (opening) ← entry LOW — ADVERSE
///   both legs (the resolved `# CHECK SIGN` law).
///
/// Returns `(buy_fill_paisa, sell_fill_paisa, realized_slippage_per_share)`
/// where the realized slippage is
/// `|buy_fill − buy_raw| + |sell_raw − sell_fill|` measured POST-floor
/// (2 × tick normally; a floored degenerate leg contributes less — never
/// over-stated; the open→extreme move is fill-ANCHOR semantics carried by
/// the gross P&L, not slippage).
///
/// # Errors
///
/// `Err` only when a fill or the realized slippage overflows i64 (the ± tick
/// at the i64 edge — production inputs are ≥ 1 tick and ≪ the edge; the
/// Python arm handles anything the envelope guard rejects).
pub fn apply_slippage(
    entry_high_paisa: i64,
    entry_low_paisa: i64,
    exit_high_paisa: i64,
    exit_low_paisa: i64,
    is_long: bool,
) -> Result<(i64, i64, i64), String> {
    let tick = i128::from(TICK_PAISA);
    let (buy_raw, sell_raw) = if is_long {
        // buy leg ← entry bar HIGH, sell leg ← exit bar LOW.
        (i128::from(entry_high_paisa), i128::from(exit_low_paisa))
    } else {
        // Short: the opening sell anchors the entry LOW, the cover the exit
        // HIGH — adverse on both legs.
        (i128::from(exit_high_paisa), i128::from(entry_low_paisa))
    };
    // ± one tick adverse, then floor every fill at one tick (nothing trades
    // below ₹0.05) — i128 arithmetic, so the i64 edge cannot wrap here.
    let buy_fill = (buy_raw + tick).max(tick);
    let sell_fill = (sell_raw - tick).max(tick);
    // Realized per-share slippage = the adverse TICK movement TRULY baked
    // into the floored fills (a floored leg counts its real, smaller
    // movement), measured against the worst-case fill anchors.
    let realized = (buy_fill - buy_raw).abs() + (sell_raw - sell_fill).abs();
    let buy = i64::try_from(buy_fill)
        .map_err(|_| format!("apply_slippage: buy fill {buy_fill} overflows i64"))?;
    let sell = i64::try_from(sell_fill)
        .map_err(|_| format!("apply_slippage: sell fill {sell_fill} overflows i64"))?;
    let slip = i64::try_from(realized)
        .map_err(|_| format!("apply_slippage: realized slippage {realized} overflows i64"))?;
    Ok((buy, sell, slip))
}

/// `apply_futures_fill` twin (the worst-case extremes law).
///
/// Delegates to [`apply_slippage`]: the two Python laws are numerically
/// identical (`long` byte-equal by the Python golden parity pin; `short` =
/// opening sell ← entry LOW − tick, cover ← exit HIGH + tick on both). The
/// Python side keeps two independent MECHANISMS (an options↔futures
/// independence law); the Rust twin they are both compared against is
/// deliberately ONE body (§11 COMMON) — the per-name golden grids in the
/// test module keep either name from drifting.
///
/// # Errors
///
/// As [`apply_slippage`].
pub fn apply_futures_fill(
    entry_high_paisa: i64,
    entry_low_paisa: i64,
    exit_high_paisa: i64,
    exit_low_paisa: i64,
    is_long: bool,
) -> Result<(i64, i64, i64), String> {
    apply_slippage(
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
        is_long,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Flat-bar shorthand: high == low == open — the worst-case fill equals
    /// the retired open-anchored fill exactly (the no-flatter law).
    fn flat(entry: i64, exit: i64, is_long: bool) -> Result<(i64, i64, i64), String> {
        apply_slippage(entry, entry, exit, exit, is_long)
    }

    // ── Byte pins from the merged Python oracle (oracle-executed at the
    //    club-R1 reconcile) ───────────────────────────────────────────────

    #[test]
    fn long_fill_family_flat_bars() {
        // Flat-bar long (10_000, 12_000) → (10_005, 11_995, 10) — equal to
        // the retired open-anchored family by the flat-bar law.
        assert_eq!(flat(10_000, 12_000, true), Ok((10_005, 11_995, 10)));
        // Zero-gross degenerate: 10_000 → 10_000 gives (10_005, 9_995).
        assert_eq!(flat(10_000, 10_000, true), Ok((10_005, 9_995, 10)));
    }

    #[test]
    fn long_fill_range_bars_anchor_extremes() {
        // Oracle-derived range case: entry (h 10_100, l 9_900), exit
        // (h 12_050, l 11_950) → buy = 10_100 + 5, sell = 11_950 − 5.
        assert_eq!(
            apply_slippage(10_100, 9_900, 12_050, 11_950, true),
            Ok((10_105, 11_945, 10))
        );
        // The Fixture-W range family (oracle net 64_206 downstream): entry
        // (h 12_100, l 11_900), exit (h 15_050, l 14_900).
        assert_eq!(
            apply_slippage(12_100, 11_900, 15_050, 14_900, true),
            Ok((12_105, 14_895, 10))
        );
    }

    #[test]
    fn short_fill_family_adverse_both_legs() {
        // Flat-bar short (10_000, 12_000) → buy 12_005, sell 9_995 — the
        // reconciled ADVERSE short.
        assert_eq!(flat(10_000, 12_000, false), Ok((12_005, 9_995, 10)));
        // Range short: opening sell ← entry LOW 9_900 − 5; cover ← exit
        // HIGH 12_050 + 5 (oracle-derived).
        assert_eq!(
            apply_slippage(10_100, 9_900, 12_050, 11_950, false),
            Ok((12_055, 9_895, 10))
        );
    }

    #[test]
    fn futures_fill_grid_golden_flat() {
        // The flat-bar long grid — byte-equal to the calculator slippage by
        // construction (§11 COMMON body).
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
                apply_futures_fill(entry, entry, exit, exit, true),
                apply_slippage(entry, entry, exit, exit, true),
                "long grid ({entry}, {exit})"
            );
        }
        // The hand-derived flat-bar SHORT golden table (oracle-executed):
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
                apply_futures_fill(entry, entry, exit, exit, false),
                Ok(want),
                "short grid ({entry}, {exit})"
            );
        }
    }

    #[test]
    fn one_tick_floor_and_realized_recompute() {
        // A one-tick exit LOW would sell at ₹0.00 without the floor; the
        // floored sell leg truly moved 0 → realized = 1 × tick.
        assert_eq!(
            flat(100, TICK_PAISA, true),
            Ok((105, TICK_PAISA, TICK_PAISA))
        );
        // Short entry LOW at one tick: opening sell floored, realized 1 × tick.
        assert_eq!(
            flat(TICK_PAISA, 100, false),
            Ok((105, TICK_PAISA, TICK_PAISA))
        );
        // entry = exit = 5 → buy 10, sell 5 (both laws' pinned edge).
        assert_eq!(flat(5, 5, true), Ok((10, 5, 5)));
        // Sub-2-tick sell raw (7): fill floors to 5, realized 5 + 2 = 7 —
        // the [5, 10] realized band, NOT a {5, 10} two-point set.
        assert_eq!(flat(10_000, 7, true), Ok((10_005, 5, 7)));
    }

    #[test]
    fn i64_edge_errs_not_panics() {
        // + tick at the i64 edge must Err (Python bigints would carry on —
        // the seam's envelope guard routes these to the Python arm).
        assert!(apply_slippage(i64::MAX, 10, 10, 10, true).is_err());
        assert!(apply_slippage(10, 10, i64::MAX, 10, false).is_err());
        // Realized slippage overflow from a deeply negative raw.
        assert!(apply_slippage(10, 10, 10, i64::MIN, true).is_err());
        // The edge minus one tick still computes.
        assert!(apply_slippage(i64::MAX - TICK_PAISA, 10, 10, 10, true).is_ok());
    }

    mod props {
        use super::super::*;
        use proptest::prelude::*;

        /// A bar strategy yielding (high, low) with high ≥ low.
        fn bar() -> impl Strategy<Value = (i64, i64)> {
            (5i64..10_000_000, 5i64..10_000_000).prop_map(|(a, b)| (a.max(b), a.min(b)))
        }

        proptest! {
            /// Over the production envelope (extremes ≥ 1 tick): fills ≥
            /// tick and realized ∈ [tick, 2·tick] (the floored-leg band);
            /// the buy leg can never fill below its worst-case anchor.
            #[test]
            fn fills_floored_and_realized_band(
                entry in bar(),
                exit in bar(),
                is_long in proptest::bool::ANY,
            ) {
                let (eh, el) = entry;
                let (xh, xl) = exit;
                let (buy, sell, slip) = apply_slippage(eh, el, xh, xl, is_long).unwrap();
                prop_assert!(buy >= TICK_PAISA);
                prop_assert!(sell >= TICK_PAISA);
                prop_assert!(slip >= TICK_PAISA && slip <= 2 * TICK_PAISA);
                // The buy leg anchors on the adverse HIGH and never fills
                // below it (adverse up).
                let buy_raw = if is_long { eh } else { xh };
                prop_assert!(buy >= buy_raw);
            }

            /// Worst-case dominance: the extreme-anchored buy fill is never
            /// BELOW the flat-bar (open-anchored) fill at any open inside
            /// the bar, and the sell fill never ABOVE it — the
            /// never-flatter law (DEC-FILL-WORST-CASE-ONLY-001).
            #[test]
            fn worst_case_never_flatters(
                entry in bar(),
                exit in bar(),
                is_long in proptest::bool::ANY,
            ) {
                let (eh, el) = entry;
                let (xh, xl) = exit;
                let (buy, sell, _) = apply_slippage(eh, el, xh, xl, is_long).unwrap();
                // Any open inside [low, high] — take both bracket ends.
                for (eo, xo) in [(el, xl), (eh, xh)] {
                    let (b2, s2, _) = apply_slippage(eo, eo, xo, xo, is_long).unwrap();
                    prop_assert!(buy >= b2);
                    prop_assert!(sell <= s2);
                }
            }

            /// The futures name never diverges from the calculator name.
            #[test]
            fn futures_fill_equals_slippage(
                entry in bar(),
                exit in bar(),
                is_long in proptest::bool::ANY,
            ) {
                let (eh, el) = entry;
                let (xh, xl) = exit;
                prop_assert_eq!(
                    apply_futures_fill(eh, el, xh, xl, is_long).unwrap(),
                    apply_slippage(eh, el, xh, xl, is_long).unwrap()
                );
            }
        }
    }
}
