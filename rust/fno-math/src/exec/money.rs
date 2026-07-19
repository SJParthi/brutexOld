//! THE Rust money law module (DEC-EXEC-RUST-CORE-001, MIGRATION.md #1963).
//!
//! Exact-integer twins of `brutex.costs.calculator._round_levy_paisa` /
//! `_round_levy_rupee` (the permanent ADVERSE ceiling levy rounding,
//! DEC-FILL-WORST-CASE-ONLY-001 — the retired PRECISE half-up arms are
//! deleted with their mode) plus the statutory floor stage and the GST
//! round-ONCE law. Standalone — zero deps on the sibling exec modules, rlib-visible so
//! future slices import rather than re-mint (the Rust twin of
//! `brutex.money`'s COMMON role).
//!
//! ## ⚠ Two-laws warning (see also `exec::mod`)
//!
//! This module is **ADVERSE CEILING toward +∞** (the levy law —
//! DEC-FILL-WORST-CASE-ONLY-001: a levy is a charge, so "adverse" =
//! larger; the backtest can only understate the net). The indicator law
//! (`trading_core::indicators::round_half_even`, half-to-EVEN on f64) is a
//! DIFFERENT law for a DIFFERENT domain — never substitute one for the other.
//!
//! ## Shape-named primitives — the statutory-floor trap
//!
//! The statutory levies (STT / stamp / GST) first FLOOR-divide the raw
//! `value × rate` product by 10^7 with NO rounding adjustment
//! ([`floor_rate_scale`]) and only then round to the rupee
//! ([`round_to_rupee`]); the non-statutory charges (ETC / SEBI / IPFT + the
//! Groww futures brokerage turnover arm) ceil at the paisa in ONE
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

/// ADVERSE (ceiling toward +∞) paisa rounding for a `paisa ×
/// 10^7-scaled-rate` product — the
/// `brutex.costs.calculator._round_levy_paisa` twin
/// (DEC-FILL-WORST-CASE-ONLY-001: every rate-scaled levy rounds ADVERSELY;
/// the retired PRECISE half-up arm is deleted with its mode). Non-statutory
/// charges: ETC / SEBI / IPFT (+ the Groww futures brokerage turnover arm).
///
/// The Python law is `-((-x) // 10_000_000)` (floor division), i.e.
/// `ceil(x / 10^7)` — mirrored here as `-((-x).div_euclid(RATE_SCALE))`
/// (`div_euclid` = true floor semantics, so the identity holds for any
/// sign).
///
/// # Errors
///
/// `Err` when `-x` overflows i128 (only `i128::MIN`) or the quotient
/// overflows i64.
pub fn round_to_paisa(x: i128) -> Result<i64, String> {
    let neg = x
        .checked_neg()
        .ok_or_else(|| format!("round_to_paisa: |{x}| overflows i128"))?;
    let q = -(neg.div_euclid(RATE_SCALE));
    i64::try_from(q).map_err(|_| format!("round_to_paisa: result {q} overflows i64"))
}

/// ADVERSE (ceiling) statutory rupee rounding — paise up to the NEXT
/// multiple of 100, toward +∞ — the
/// `brutex.costs.calculator._round_levy_rupee` twin
/// (DEC-FILL-WORST-CASE-ONLY-001; the retired PRECISE nearest-rupee
/// half-up arm is deleted with its mode). Statutory levies AFTER the floor
/// stage: STT / stamp / GST.
///
/// The Python law is `-((-p) // 100) * 100` = `ceil(p / 100) × 100`,
/// mirrored via `div_euclid` in i128. NO LONGER total over i64: the ceiling
/// of `i64::MAX` (…807 → …900) overflows i64, so the near-MAX edge returns
/// `Err` (the Python arm's bigints carry on — the seam's envelope routes
/// such inputs to the oracle; `i64::MIN` (…808 → …800) still fits).
///
/// # Errors
///
/// `Err` when the ceiled multiple overflows i64 (only within 100 paise of
/// `i64::MAX`).
pub fn round_to_rupee(p: i64) -> Result<i64, String> {
    let w = i128::from(p);
    let q = -((-w).div_euclid(100)) * 100;
    i64::try_from(q).map_err(|_| format!("round_to_rupee: result {q} overflows i64"))
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
    round_to_rupee(raw)
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── The pinned adverse-ceiling tables — derived from the merged Python
    //    oracle `_round_levy_paisa` / `_round_levy_rupee`
    //    (DEC-FILL-WORST-CASE-ONLY-001; re-derived at the club-R1
    //    merge-reconcile — every literal below reproduced by executing the
    //    Python law) ────────────────────────────────────────────────────

    #[test]
    fn round_to_paisa_ceiling_table_both_signs() {
        // Positive: ANY fractional part ceils up — 1 unit already rounds to
        // a whole paisa; exact multiples are fixed points.
        assert_eq!(round_to_paisa(1), Ok(1));
        assert_eq!(round_to_paisa(4_999_999), Ok(1));
        assert_eq!(round_to_paisa(5_000_000), Ok(1));
        assert_eq!(round_to_paisa(10_000_000), Ok(1));
        assert_eq!(round_to_paisa(10_000_001), Ok(2));
        assert_eq!(round_to_paisa(15_000_000), Ok(2));
        // Negative: ceiling toward +∞ — a sub-paisa negative reads 0, an
        // exact multiple is a fixed point.
        assert_eq!(round_to_paisa(-1), Ok(0));
        assert_eq!(round_to_paisa(-4_999_999), Ok(0));
        assert_eq!(round_to_paisa(-5_000_000), Ok(0));
        assert_eq!(round_to_paisa(-9_999_999), Ok(0));
        assert_eq!(round_to_paisa(-10_000_000), Ok(-1));
        assert_eq!(round_to_paisa(-15_000_000), Ok(-1));
    }

    #[test]
    fn round_to_rupee_ceiling_table_both_signs() {
        assert_eq!(round_to_rupee(1), Ok(100)); // any fraction ceils
        assert_eq!(round_to_rupee(49), Ok(100));
        assert_eq!(round_to_rupee(50), Ok(100));
        assert_eq!(round_to_rupee(100), Ok(100)); // fixed point
        assert_eq!(round_to_rupee(101), Ok(200));
        assert_eq!(round_to_rupee(150), Ok(200));
        assert_eq!(round_to_rupee(1_100), Ok(1_100));
        assert_eq!(round_to_rupee(1_101), Ok(1_200)); // ₹11.01 → ₹12
        assert_eq!(round_to_rupee(1_170), Ok(1_200)); // ₹11.70 → ₹12
        assert_eq!(round_to_rupee(-1), Ok(0));
        assert_eq!(round_to_rupee(-50), Ok(0));
        assert_eq!(round_to_rupee(-99), Ok(0));
        assert_eq!(round_to_rupee(-100), Ok(-100));
        assert_eq!(round_to_rupee(-101), Ok(-100));
        assert_eq!(round_to_rupee(-151), Ok(-100));
        assert_eq!(round_to_rupee(-200), Ok(-200));
    }

    // ── Boundary vectors: zero, the int64 extremes ──────────────────────

    #[test]
    fn money_law_boundary_vectors() {
        // Zero is a fixed point of every primitive.
        assert_eq!(round_to_paisa(0), Ok(0));
        assert_eq!(round_to_rupee(0), Ok(0));
        assert_eq!(floor_rate_scale(0, 3_503), Ok(0));
        // i64::MAX ceils PAST i64 (…807 → …900) — Err, never a wrap; the
        // seam's envelope guard keeps such inputs on the Python-bigint arm.
        assert!(round_to_rupee(i64::MAX).is_err());
        // The last in-range ceiling below the edge still computes.
        assert_eq!(
            round_to_rupee(9_223_372_036_854_775_800),
            Ok(9_223_372_036_854_775_800)
        );
        // i64::MIN (…808) ceils toward +∞ to …800 — still in range.
        assert_eq!(round_to_rupee(i64::MIN), Ok(-9_223_372_036_854_775_800));
        // i64::MAX-scale products through the paisa rounder stay exact:
        // ceil(i64::MAX / 1e7) = 922_337_203_686 (fractional → up).
        assert_eq!(round_to_paisa(i128::from(i64::MAX)), Ok(922_337_203_686));
        // ceil((i64::MIN + 1) / 1e7) toward +∞ = −922_337_203_685.
        assert_eq!(
            round_to_paisa(i128::from(i64::MIN) + 1),
            Ok(-922_337_203_685)
        );
    }

    #[test]
    fn round_to_paisa_overflow_edges_err_not_panic() {
        assert!(round_to_paisa(i128::MIN).is_err()); // |MIN| overflows
        // A quotient past i64 errs cleanly.
        assert!(round_to_paisa(i128::from(i64::MAX) * RATE_SCALE * 2).is_err());
        // Just inside the envelope still computes.
        assert!(round_to_paisa(i128::from(i64::MAX)).is_ok());
        // i128::MAX itself has a representable ceiling? Its quotient is far
        // past i64 — Err, never a panic.
        assert!(round_to_paisa(i128::MAX).is_err());
    }

    // ── floor_rate_scale: the floor-vs-ceiling trap made visible ────────

    #[test]
    fn floor_rate_scale_is_floor_never_ceiling() {
        // 9_999_999 / 1e7 would CEIL to 1; the statutory raw stage FLOORS —
        // the adverse rounding happens ONLY at the rupee stage after it.
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

    // ── GST round-ONCE law (adverse rupee ceiling) ──────────────────────

    #[test]
    fn gst_round_once_law_ex1() {
        // The merged-oracle Fixture-A base: 4_000 + 238 + 2 + 4 = 4_244;
        // 4_244 × 1_800_000 // 1e7 = 763 → CEIL rupee → 800 (oracle-derived
        // at the club-R1 reconcile — `_charge_stack_py` Fixture A flat).
        assert_eq!(gst_paisa(&[4_000, 238, 2, 4], 1_800_000), Ok(800));
        // Fixture-B flat base: 4_000 + 83 + 2 + 0 = 4_085; raw 735 → 800.
        assert_eq!(gst_paisa(&[4_000, 83, 2, 0], 1_800_000), Ok(800));
    }

    #[test]
    fn gst_excludes_nothing_it_is_handed_base_is_callers_law() {
        // The base slice IS the law boundary: the caller passes exactly
        // brokerage+exch+sebi+ipft (STT/stamp excluded there).
        assert_eq!(gst_paisa(&[], 1_800_000), Ok(0));
        // 4_000 × 18% = 720 raw → CEIL rupee → 800.
        assert_eq!(gst_paisa(&[4_000], 1_800_000), Ok(800));
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
            /// Ceiling law: 10^7·r ≥ x with 10^7·r − x < 10^7 — r is the
            /// LEAST integer at/above x/10^7 (kills a floor/half-up mutant).
            #[test]
            fn round_to_paisa_is_ceiling(x in -(1i128 << 80)..(1i128 << 80)) {
                let r = i128::from(round_to_paisa(x).unwrap());
                prop_assert!(r * RATE_SCALE >= x);
                prop_assert!(r * RATE_SCALE - x < RATE_SCALE);
            }

            /// Result ≡ 0 (mod 100), at/above p, within 100 paise — the
            /// least multiple of 100 at/above p (ceiling toward +∞).
            #[test]
            fn round_to_rupee_least_multiple_at_or_above(
                p in i64::MIN..=(i64::MAX - 100)
            ) {
                let r = round_to_rupee(p).unwrap();
                prop_assert_eq!(r % 100, 0);
                let diff = i128::from(r) - i128::from(p);
                prop_assert!(diff >= 0);
                prop_assert!(diff < 100);
            }

            /// Ceiling anti-symmetry: ceil(−x) == −floor(x) — pinned via the
            /// floor stage (kills a re-introduced sign-symmetric half-up).
            #[test]
            fn round_to_paisa_ceil_floor_duality(x in -(1i128 << 80)..(1i128 << 80)) {
                let c = i128::from(round_to_paisa(x).unwrap());
                let f = i128::from(floor_rate_scale(x, 1).unwrap());
                // ceil(x/u) == floor((x + u − 1)/u); floor stage is exact.
                prop_assert!(c == f || c == f + 1);
                prop_assert_eq!(c == f, x.rem_euclid(RATE_SCALE) == 0);
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
