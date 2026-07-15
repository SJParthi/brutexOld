//! THE Rust money law module (DEC-EXEC-RUST-CORE-001, MIGRATION.md #1724).
//!
//! Exact-integer twins of `brutex.costs.calculator._round_to_paisa` /
//! `_round_to_rupee` plus the statutory floor stage and the GST round-ONCE
//! law. Standalone — zero deps on the sibling exec modules, rlib-visible so
//! future slices import rather than re-mint (the Rust twin of
//! `brutex.money`'s COMMON role).
//!
//! ## ⚠ Two-laws warning (see also `exec::mod`)
//!
//! This module is **half-AWAY-FROM-ZERO** (the money law). The indicator law
//! (`trading_core::indicators::round_half_even`, half-to-EVEN on f64) is a
//! DIFFERENT law for a DIFFERENT domain — never substitute one for the other.
//!
//! ## Shape-named primitives — the statutory-floor trap
//!
//! The statutory levies (STT / stamp / GST) first FLOOR-divide the raw
//! `value × rate` product by 10^7 with NO rounding adjustment
//! ([`floor_rate_scale`]) and only then round to the rupee
//! ([`round_to_rupee`]); the non-statutory charges (ETC / SEBI / IPFT + the
//! Groww futures brokerage turnover arm) round half-up at the paisa in ONE
//! step ([`round_to_paisa`]). Making these different FUNCTION NAMES — not
//! different constants — keeps the one-paisa drift trap unexpressible by
//! accident.
//!
//! ## Deferred (doc-only — DEC-EXEC-RUST-STAGE2-001)
//!
//! `paisa_from_f64_rupees_half_up` is deliberately NOT implemented: the
//! Decimal `ROUND_HALF_UP` rupee→paisa snap stays in Python
//! (`brutex.money.rupees_to_paisa`), and this crate receives i64 paisa only.
//! `format_inr` / display never move to Rust.

/// Rate-scale divisor: rate constants encode `rate_decimal × 10_000_000`, so
/// `paisa × rate_bps_x100 / 10_000_000` yields a paisa charge
/// (`brutex.costs.calculator` rate-encoding law).
pub const RATE_SCALE: i128 = 10_000_000;

/// Half of [`RATE_SCALE`] — the half-up adjustment at the paisa scale.
pub const RATE_SCALE_HALF: i128 = 5_000_000;

/// Round a `paisa × 10^7-scaled-rate` product to whole paise, half AWAY from
/// zero — the `brutex.costs.calculator._round_to_paisa` twin
/// (calculator.py:44-68). Non-statutory charges: ETC / SEBI / IPFT (+ the
/// Groww futures brokerage turnover arm).
///
/// Each sign branch divides a NON-NEGATIVE value, where Rust `/` (truncating)
/// equals Python `//` (floor) — the sign branch mirrors the Python source
/// line-for-line. Ties are pinned both signs in the unit table below.
///
/// # Errors
///
/// `Err` when the adjusted magnitude overflows i128 (unreachable for products
/// of two i64s — |i64|² < i128::MAX/2) or the quotient overflows i64.
pub fn round_to_paisa(x: i128) -> Result<i64, String> {
    let q = if x >= 0 {
        x.checked_add(RATE_SCALE_HALF)
            .ok_or_else(|| format!("round_to_paisa: {x} + half overflows i128"))?
            / RATE_SCALE
    } else {
        let neg = x
            .checked_neg()
            .ok_or_else(|| format!("round_to_paisa: |{x}| overflows i128"))?;
        -(neg
            .checked_add(RATE_SCALE_HALF)
            .ok_or_else(|| format!("round_to_paisa: |{x}| + half overflows i128"))?
            / RATE_SCALE)
    };
    i64::try_from(q).map_err(|_| format!("round_to_paisa: result {q} overflows i64"))
}

/// Round paise to the nearest multiple of 100 (whole rupee), half AWAY from
/// zero — the `brutex.costs.calculator._round_to_rupee` twin
/// (calculator.py:71-91). Statutory levies AFTER the floor stage: STT /
/// stamp / GST.
///
/// Computed in i128 internally so `p + 50` can never overflow at the i64
/// edge. The result is total over ALL i64 inputs: the nearest multiple of 100
/// to any i64 differs from it by at most 50, and the nearest-100 neighbours
/// of `i64::MAX` (…807 → …800) and `i64::MIN` (…808 → …800) both round
/// TOWARD zero, so the i128 quotient always fits back in i64 (unit-pinned at
/// both extremes below).
pub fn round_to_rupee(p: i64) -> i64 {
    let w = i128::from(p);
    let q = if w >= 0 {
        ((w + 50) / 100) * 100
    } else {
        -(((-w + 50) / 100) * 100)
    };
    // Proven in-range above (extremes unit-pinned); the cast is exact.
    q as i64
}

/// The statutory RAW stage: `(notional × rate) // 10^7` with **NO rounding
/// adjustment** — the calculator's floor-division step for STT / stamp / GST
/// raws (calculator.py:279/306/322). The r3 §8.4 one-paisa trap made a
/// different FUNCTION NAME, not a different constant: a half-up here would
/// drift the raw by 1 paisa and occasionally flip the rupee rounding.
///
/// `div_euclid` = true floor semantics (Python `//`) even off the
/// non-negative production envelope — belt + braces.
///
/// # Errors
///
/// `Err` when the product overflows i128 or the floored quotient overflows
/// i64.
pub fn floor_rate_scale(notional: i128, rate_bps_x100: i128) -> Result<i64, String> {
    let product = notional
        .checked_mul(rate_bps_x100)
        .ok_or_else(|| format!("floor_rate_scale: {notional} × {rate_bps_x100} overflows i128"))?;
    let q = product.div_euclid(RATE_SCALE);
    i64::try_from(q).map_err(|_| format!("floor_rate_scale: result {q} overflows i64"))
}

/// GST round-ONCE law (DEC-COST-001): base = Σ of the ALREADY-ROUNDED service
/// components (brokerage + exchange + SEBI + IPFT; STT + stamp EXCLUDED),
/// × the GST rate → [`floor_rate_scale`] → ONE [`round_to_rupee`] on the
/// total — never CGST + SGST rounded separately (Section 170 CGST).
///
/// # Errors
///
/// `Err` when the scaled product overflows (via [`floor_rate_scale`]). The
/// i128 component sum itself cannot overflow (≤ 4 × |i64| ≪ i128::MAX).
pub fn gst_paisa(base_components_paisa: &[i64], gst_rate_bps_x100: i64) -> Result<i64, String> {
    let mut base: i128 = 0;
    for &component in base_components_paisa {
        base += i128::from(component);
    }
    let raw = floor_rate_scale(base, i128::from(gst_rate_bps_x100))?;
    Ok(round_to_rupee(raw))
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── The pinned tie tables — byte-copied from the Python oracle pins
    //    (tests/unit/test_costs_calculator.py:1049-1084) ──────────────────

    #[test]
    fn round_to_paisa_tie_table_both_signs() {
        // Positive: exactly-half rounds AWAY (up); just-under-half floors.
        assert_eq!(round_to_paisa(5_000_000), Ok(1));
        assert_eq!(round_to_paisa(4_999_999), Ok(0));
        assert_eq!(round_to_paisa(15_000_000), Ok(2));
        // Negative: exactly-half rounds AWAY (down) — never toward +∞.
        assert_eq!(round_to_paisa(-5_000_000), Ok(-1));
        assert_eq!(round_to_paisa(-4_999_999), Ok(0));
        assert_eq!(round_to_paisa(-15_000_000), Ok(-2));
    }

    #[test]
    fn round_to_rupee_tie_table_both_signs() {
        assert_eq!(round_to_rupee(50), 100); // the exact ROUND_HALF_UP tie
        assert_eq!(round_to_rupee(49), 0);
        assert_eq!(round_to_rupee(150), 200);
        assert_eq!(round_to_rupee(1_170), 1_200); // ₹11.70 → ₹12
        assert_eq!(round_to_rupee(-50), -100);
        assert_eq!(round_to_rupee(-49), 0);
        assert_eq!(round_to_rupee(-150), -200);
    }

    // ── Coordinator-mandated money-law boundary vectors (slice-7 parity
    //    gate (a)): the ±0.005-class half boundary at each scale, negative,
    //    zero, and the int64 extremes ────────────────────────────────────

    #[test]
    fn money_law_boundary_vectors() {
        // Zero is a fixed point of every primitive.
        assert_eq!(round_to_paisa(0), Ok(0));
        assert_eq!(round_to_rupee(0), 0);
        assert_eq!(floor_rate_scale(0, 3_503), Ok(0));
        // The rupee half-boundary class either side of a whole rupee
        // (the integer analog of the ±₹0.005 Decimal class).
        assert_eq!(round_to_rupee(149), 100);
        assert_eq!(round_to_rupee(151), 200);
        assert_eq!(round_to_rupee(-149), -100);
        assert_eq!(round_to_rupee(-151), -200);
        // int64-max/min paisa: total, rounds toward zero at both extremes.
        assert_eq!(round_to_rupee(i64::MAX), 9_223_372_036_854_775_800);
        assert_eq!(round_to_rupee(i64::MIN), -9_223_372_036_854_775_800);
        // i64::MAX-scale products through the paisa rounder stay exact.
        assert_eq!(
            round_to_paisa(i128::from(i64::MAX)),
            Ok(922_337_203_685) // (i64::MAX + 5e6) / 1e7, floored
        );
        assert_eq!(
            round_to_paisa(i128::from(i64::MIN) + 1),
            Ok(-922_337_203_685)
        );
    }

    #[test]
    fn round_to_paisa_overflow_edges_err_not_panic() {
        assert!(round_to_paisa(i128::MAX).is_err()); // + half overflows
        assert!(round_to_paisa(i128::MIN).is_err()); // |MIN| overflows
        // A quotient past i64 errs cleanly.
        assert!(round_to_paisa(i128::from(i64::MAX) * RATE_SCALE * 2).is_err());
        // Just inside the envelope still computes.
        assert!(round_to_paisa(i128::from(i64::MAX)).is_ok());
    }

    // ── floor_rate_scale: the floor-vs-half-up trap made visible ────────

    #[test]
    fn floor_rate_scale_is_floor_never_half_up() {
        // 9_999_999 / 1e7 would half-up to 1; the statutory raw stage FLOORS.
        assert_eq!(floor_rate_scale(9_999_999, 1), Ok(0));
        assert_eq!(floor_rate_scale(1, 9_999_999), Ok(0));
        // The COSTS_VERIFIED Ex.1 STT raw: 779_675 × 15_000 // 1e7 = 1_169.
        assert_eq!(floor_rate_scale(779_675, 15_000), Ok(1_169));
        // Synthetic futures STT raw: 65_497_500 × 2_000 // 1e7 = 13_099.
        assert_eq!(floor_rate_scale(65_497_500, 2_000), Ok(13_099));
    }

    #[test]
    fn floor_rate_scale_true_floor_on_negatives() {
        // div_euclid floors toward −∞ exactly like Python // (belt + braces
        // off the non-negative production envelope).
        assert_eq!(floor_rate_scale(-1, 1), Ok(-1)); // −1/1e7 floors to −1
        assert_eq!(floor_rate_scale(-9_999_999, 1), Ok(-1));
        assert_eq!(floor_rate_scale(-10_000_000, 1), Ok(-1));
        assert_eq!(floor_rate_scale(-10_000_001, 1), Ok(-2));
    }

    #[test]
    fn floor_rate_scale_overflow_errs() {
        assert!(floor_rate_scale(i128::MAX, 2).is_err()); // product overflow
        assert!(floor_rate_scale(i128::from(i64::MAX), RATE_SCALE * 2).is_err()); // i64 exit
    }

    // ── GST round-ONCE law ───────────────────────────────────────────────

    #[test]
    fn gst_round_once_law_ex1() {
        // COSTS_VERIFIED §5 Ex.1: base 4_000+501+2+7 = 4_510;
        // 4_510 × 1_800_000 // 1e7 = 811 → rupee → 800.
        assert_eq!(gst_paisa(&[4_000, 501, 2, 7], 1_800_000), Ok(800));
        // Ex.3's regenerated round-ONCE value: base 4_000+116+0+2 = 4_118;
        // raw 741 → 700 (NOT the doc's old ₹4+₹4 split).
        assert_eq!(gst_paisa(&[4_000, 116, 0, 2], 1_800_000), Ok(700));
    }

    #[test]
    fn gst_excludes_nothing_it_is_handed_base_is_callers_law() {
        // The base slice IS the law boundary: the caller passes exactly
        // brokerage+exch+sebi+ipft (STT/stamp excluded there).
        assert_eq!(gst_paisa(&[], 1_800_000), Ok(0));
        assert_eq!(gst_paisa(&[4_000], 1_800_000), Ok(700)); // 720 → 700
    }

    #[test]
    fn gst_overflow_errs() {
        assert!(gst_paisa(&[i64::MAX, i64::MAX], i64::MAX).is_err());
    }

    // ── Algebraic laws (proptest) ────────────────────────────────────────

    mod props {
        use super::super::*;
        use proptest::prelude::*;

        proptest! {
            /// Odd symmetry: f(−x) == −f(x) — half-AWAY-from-zero, never
            /// toward +∞ (kills a dropped sign branch). Domain bounded to
            /// ±2^80 so BOTH signs' quotients fit i64 (at the extreme edge
            /// Ok(i64::MIN) legitimately pairs with an Err on the mirror —
            /// pinned separately in `round_to_paisa_overflow_edges…`).
            #[test]
            fn round_to_paisa_odd_symmetry(x in -(1i128 << 80)..(1i128 << 80)) {
                let pos = round_to_paisa(x).unwrap();
                let neg = round_to_paisa(-x).unwrap();
                prop_assert_eq!(neg, -pos);
            }

            /// |10^7·r − x| ≤ 5·10^6 with AWAY-from-zero ties: r is the
            /// nearest integer, and an exact tie lands away from zero.
            #[test]
            fn round_to_paisa_nearest_with_away_ties(x in -(1i128 << 80)..(1i128 << 80)) {
                let r = i128::from(round_to_paisa(x).unwrap());
                let err = r * RATE_SCALE - x;
                prop_assert!(err.abs() <= RATE_SCALE_HALF);
                if err.abs() == RATE_SCALE_HALF {
                    // tie: |r·1e7| must be ≥ |x| (rounded AWAY from zero)
                    prop_assert!((r * RATE_SCALE).abs() >= x.abs());
                }
            }

            /// Result ≡ 0 (mod 100), within 50 paise, away-from-zero ties.
            #[test]
            fn round_to_rupee_multiple_of_100_nearest(p in i64::MIN..=i64::MAX) {
                let r = round_to_rupee(p);
                prop_assert_eq!(r % 100, 0);
                let err = i128::from(r) - i128::from(p);
                prop_assert!(err.abs() <= 50);
                if err.abs() == 50 {
                    prop_assert!(i128::from(r).abs() >= i128::from(p).abs());
                }
            }

            /// Odd symmetry for the rupee rounder (bounded so −p exists).
            #[test]
            fn round_to_rupee_odd_symmetry(p in (i64::MIN + 1)..=i64::MAX) {
                prop_assert_eq!(round_to_rupee(-p), -round_to_rupee(p));
            }

            /// floor_rate_scale == mathematical floor of x·rate/1e7
            /// (cross-checked against div_euclid identities).
            #[test]
            fn floor_rate_scale_is_floor(
                x in -(1i128 << 62)..(1i128 << 62),
                rate in 0i128..2_000_000,
            ) {
                let q = i128::from(floor_rate_scale(x, rate).unwrap());
                let product = x * rate;
                prop_assert!(q * RATE_SCALE <= product);
                prop_assert!((q + 1) * RATE_SCALE > product);
            }
        }
    }
}
