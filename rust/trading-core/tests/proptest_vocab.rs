//! Property test for the 52-condition evaluator (DEC-TRADING-CORE-FUZZ-PROPTEST-001).
//!
//! `evaluate_atoms` is cross-checked against `reference_mask` — an INDEPENDENT
//! re-derivation of all 52 bits written from the spec, not copied from
//! `evaluator.rs`. The two differ in HOW they compute (most visibly the
//! proximity primitive: the production path uses integer floor division, the
//! reference uses `i128` cross-multiplication with no division), so agreement on
//! tens of thousands of random valid bars catches a divergence the 400 frozen
//! golden vectors might miss. A handful of structural invariants (mutually
//! exclusive bits, reserved-bit cleanliness) are checked alongside.

use proptest::prelude::*;
use trading_core::bits;
use trading_core::{
    IndicatorSnapshot, PriorBarHistory, ProximityParams, compute_fib_levels, compute_pivot_levels,
    evaluate_atoms,
};

const FULL52: u64 = (1u64 << 52) - 1;
const PRICE_MIN: i64 = 1;
const PRICE_MAX: i64 = 2_000_000_000_000; // 2e12 paisa — honest envelope ceiling

// ── independent reference ───────────────────────────────────────────────────

/// `|close - level| <= close * bps / 10000`, computed by cross-multiplication in
/// `i128` (no division) — the obvious form of the proximity definition, distinct
/// from the production floor-division path. Equivalent for `close, bps >= 0`.
fn ref_is_near(close: i64, level: i64, bps: i64) -> bool {
    if close <= 0 {
        return false;
    }
    let diff = i128::from(close) - i128::from(level);
    let lhs = diff.abs() * 10_000;
    let rhs = i128::from(close) * i128::from(bps);
    lhs <= rhs
}

fn set(mask: &mut u64, bit: u32, cond: bool) {
    if cond {
        *mask |= 1u64 << bit;
    }
}

/// Independent re-derivation of the 52-bit mask straight from the vocabulary
/// spec. Deliberately verbose and division-free where the production code uses
/// integer division.
#[allow(clippy::too_many_lines)]
fn reference_mask(s: &IndicatorSnapshot, p: &ProximityParams, prior: &PriorBarHistory) -> u64 {
    let mut m = 0u64;
    let c = s.close_paisa;

    // Group 1: EMA position.
    if let Some(e) = s.ema20_paisa {
        set(&mut m, bits::BIT_CLOSE_ABOVE_EMA20, c > e);
        set(&mut m, bits::BIT_CLOSE_BELOW_EMA20, c < e);
    }
    if let Some(e) = s.ema200_paisa {
        set(&mut m, bits::BIT_CLOSE_ABOVE_EMA200, c > e);
        set(&mut m, bits::BIT_CLOSE_BELOW_EMA200, c < e);
    }
    if let (Some(e20), Some(e200)) = (s.ema20_paisa, s.ema200_paisa) {
        set(&mut m, bits::BIT_EMA20_ABOVE_EMA200, e20 > e200);
        set(&mut m, bits::BIT_EMA20_BELOW_EMA200, e20 < e200);
    }

    // Group 2: pivot proximity.
    let pv = &s.pivots;
    let pb = p.pivot_proximity_bps;
    set(
        &mut m,
        bits::BIT_NEAR_PIVOT_P,
        ref_is_near(c, pv.p_paisa, pb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_PIVOT_R1,
        ref_is_near(c, pv.r1_paisa, pb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_PIVOT_R2,
        ref_is_near(c, pv.r2_paisa, pb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_PIVOT_R3,
        ref_is_near(c, pv.r3_paisa, pb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_PIVOT_S1,
        ref_is_near(c, pv.s1_paisa, pb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_PIVOT_S2,
        ref_is_near(c, pv.s2_paisa, pb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_PIVOT_S3,
        ref_is_near(c, pv.s3_paisa, pb),
    );

    // Group 3: PDH/PDL.
    let (pdh, pdl) = (pv.pdh_paisa, pv.pdl_paisa);
    set(&mut m, bits::BIT_CLOSE_ABOVE_PDH, c > pdh);
    set(&mut m, bits::BIT_CLOSE_BELOW_PDH, c < pdh);
    set(&mut m, bits::BIT_CLOSE_ABOVE_PDL, c > pdl);
    set(&mut m, bits::BIT_CLOSE_BELOW_PDL, c < pdl);
    let ppb = p.pdh_pdl_proximity_bps;
    set(&mut m, bits::BIT_NEAR_PDH, ref_is_near(c, pdh, ppb));
    set(&mut m, bits::BIT_NEAR_PDL, ref_is_near(c, pdl, ppb));

    // Group 4: fib proximity.
    let f = &s.fib;
    let fb = p.fib_proximity_bps;
    set(
        &mut m,
        bits::BIT_NEAR_FIB_0,
        ref_is_near(c, f.retrace_level_at_pdh_paisa, fb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_FIB_236,
        ref_is_near(c, f.retrace_23_paisa, fb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_FIB_382,
        ref_is_near(c, f.retrace_38_paisa, fb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_FIB_50,
        ref_is_near(c, f.retrace_50_paisa, fb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_FIB_618,
        ref_is_near(c, f.retrace_62_paisa, fb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_FIB_786,
        ref_is_near(c, f.retrace_78_paisa, fb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_FIB_100,
        ref_is_near(c, f.retrace_level_at_pdl_paisa, fb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_FIB_1272,
        ref_is_near(c, f.ext_up_127_paisa, fb) || ref_is_near(c, f.ext_down_127_paisa, fb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_FIB_1618,
        ref_is_near(c, f.ext_up_162_paisa, fb) || ref_is_near(c, f.ext_down_162_paisa, fb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_FIB_200,
        ref_is_near(c, f.ext_up_200_paisa, fb) || ref_is_near(c, f.ext_down_200_paisa, fb),
    );
    set(
        &mut m,
        bits::BIT_NEAR_FIB_2618,
        ref_is_near(c, f.ext_up_262_paisa, fb) || ref_is_near(c, f.ext_down_262_paisa, fb),
    );

    // Group 5: candle shape.
    let (o, h, lo) = (s.open_paisa, s.high_paisa, s.low_paisa);
    let body = (c - o).abs();
    let range = h - lo;
    set(&mut m, bits::BIT_BAR_BULLISH, c > o);
    set(&mut m, bits::BIT_BAR_BEARISH, c < o);
    set(&mut m, bits::BIT_BAR_DOJI, range > 0 && body * 10 < range);
    if range > 0 {
        set(&mut m, bits::BIT_BAR_LARGE_BODY, body * 10 > range * 7);
        set(&mut m, bits::BIT_BAR_SMALL_BODY, body * 10 < range * 3);
        let upper = h - o.max(c);
        let lower = o.min(c) - lo;
        set(&mut m, bits::BIT_LONG_UPPER_WICK, upper * 2 > range);
        set(&mut m, bits::BIT_LONG_LOWER_WICK, lower * 2 > range);
    }

    // Group 6: prior bars.
    let n = prior.prior_n;
    let len_ok = prior.flags.len() == n;
    set(
        &mut m,
        bits::BIT_PRIOR_N_BULLISH,
        len_ok && prior.flags.iter().all(|&b| b),
    );
    set(
        &mut m,
        bits::BIT_PRIOR_N_BEARISH,
        len_ok && prior.flags.iter().all(|&b| !b),
    );
    let alt = len_ok && prior.flags.windows(2).all(|w| w[0] != w[1]);
    set(&mut m, bits::BIT_PRIOR_ALTERNATING, alt);

    // Group 7: day structure.
    set(&mut m, bits::BIT_CLOSE_AT_DAY_HIGH, c == s.day_high_paisa);
    set(&mut m, bits::BIT_CLOSE_AT_DAY_LOW, c == s.day_low_paisa);
    let dr = s.day_high_paisa - s.day_low_paisa;
    if dr > 0 {
        set(
            &mut m,
            bits::BIT_CLOSE_IN_UPPER_THIRD,
            (c - s.day_low_paisa) * 3 > dr * 2,
        );
        set(
            &mut m,
            bits::BIT_CLOSE_IN_LOWER_THIRD,
            (c - s.day_low_paisa) * 3 < dr,
        );
    }

    // Group 8: time of day (seconds since IST midnight).
    let t = s.tod_seconds;
    let em = (9 * 3600 + 16 * 60, 10 * 3600);
    let mm = (10 * 3600, 11 * 3600 + 30 * 60);
    let md = (11 * 3600 + 30 * 60, 13 * 3600);
    let af = (13 * 3600, 15 * 3600);
    set(&mut m, bits::BIT_EARLY_MORNING, t >= em.0 && t < em.1);
    set(&mut m, bits::BIT_MID_MORNING, t >= mm.0 && t < mm.1);
    set(&mut m, bits::BIT_MIDDAY, t >= md.0 && t < md.1);
    set(&mut m, bits::BIT_AFTERNOON, t >= af.0 && t < af.1);

    // Group 9: day type.
    let pdc = pv.pdc_paisa;
    if pdc > 0 {
        let gap = (pdc * 50) / 10_000; // 0.5% of pdc; pdc>0 so floor == euclid
        set(
            &mut m,
            bits::BIT_GAP_UP_DAY,
            s.open_of_day_paisa > pdc + gap,
        );
        set(
            &mut m,
            bits::BIT_GAP_DOWN_DAY,
            s.open_of_day_paisa < pdc - gap,
        );
    }
    set(
        &mut m,
        bits::BIT_INSIDE_DAY,
        s.day_high_paisa < pdh && s.day_low_paisa > pdl,
    );
    set(
        &mut m,
        bits::BIT_OUTSIDE_DAY,
        s.day_high_paisa > pdh && s.day_low_paisa < pdl,
    );

    m
}

// ── independent indicator reference ─────────────────────────────────────────
//
// Pins EVERY pivot + fib level (including R4/S4 and the 424 extensions, which
// the 52-condition evaluator never consumes, so the golden/evaluator tests can't
// reach them). Re-derives each level from the spec formula; any source operator
// mutation diverges from this and is caught.

fn ref_round(x: f64) -> i64 {
    x.round_ties_even() as i64
}

/// Assert `compute_pivot_levels` matches the independent formula re-derivation.
fn assert_pivots_match(pdh: i64, pdl: i64, pdc: i64) -> Result<(), TestCaseError> {
    let p = ref_round((pdh + pdl + pdc) as f64 / 3.0);
    let range = pdh - pdl;
    let p_minus_pdl = p - pdl;
    let pdh_minus_p = pdh - p;
    let v = compute_pivot_levels(pdh, pdl, pdc);
    prop_assert_eq!(v.p_paisa, p);
    prop_assert_eq!(v.r1_paisa, 2 * p - pdl);
    prop_assert_eq!(v.r2_paisa, p + range);
    prop_assert_eq!(v.r3_paisa, pdh + 2 * p_minus_pdl);
    prop_assert_eq!(v.r4_paisa, pdh + 3 * p_minus_pdl);
    prop_assert_eq!(v.s1_paisa, 2 * p - pdh);
    prop_assert_eq!(v.s2_paisa, p - range);
    prop_assert_eq!(v.s3_paisa, pdl - 2 * pdh_minus_p);
    prop_assert_eq!(v.s4_paisa, pdl - 3 * pdh_minus_p);
    Ok(())
}

/// Assert `compute_fib_levels` matches the independent formula re-derivation.
fn assert_fib_match(pdh: i64, pdl: i64) -> Result<(), TestCaseError> {
    let range = pdh - pdl;
    let r = range as f64;
    let pdh_f = pdh as f64;
    let pdl_f = pdl as f64;
    let v = compute_fib_levels(pdh, pdl);
    prop_assert_eq!(v.range_paisa, range);
    prop_assert_eq!(v.retrace_level_at_pdh_paisa, pdh);
    prop_assert_eq!(v.retrace_23_paisa, ref_round(pdh_f - 0.236 * r));
    prop_assert_eq!(v.retrace_38_paisa, ref_round(pdh_f - 0.382 * r));
    prop_assert_eq!(v.retrace_50_paisa, ref_round((pdh + pdl) as f64 / 2.0));
    prop_assert_eq!(v.retrace_62_paisa, ref_round(pdh_f - 0.618 * r));
    prop_assert_eq!(v.retrace_78_paisa, ref_round(pdh_f - 0.786 * r));
    prop_assert_eq!(v.retrace_level_at_pdl_paisa, pdl);
    prop_assert_eq!(v.ext_up_127_paisa, ref_round(pdh_f + 0.272 * r));
    prop_assert_eq!(v.ext_up_162_paisa, ref_round(pdh_f + 0.618 * r));
    prop_assert_eq!(v.ext_up_200_paisa, ref_round(pdh_f + r));
    prop_assert_eq!(v.ext_up_262_paisa, ref_round(pdh_f + 1.618 * r));
    prop_assert_eq!(v.ext_up_424_paisa, ref_round(pdh_f + 3.236 * r));
    prop_assert_eq!(v.ext_down_127_paisa, ref_round(pdl_f - 0.272 * r));
    prop_assert_eq!(v.ext_down_162_paisa, ref_round(pdl_f - 0.618 * r));
    prop_assert_eq!(v.ext_down_200_paisa, ref_round(pdl_f - r));
    prop_assert_eq!(v.ext_down_262_paisa, ref_round(pdl_f - 1.618 * r));
    prop_assert_eq!(v.ext_down_424_paisa, ref_round(pdl_f - 3.236 * r));
    Ok(())
}

// ── strategies ──────────────────────────────────────────────────────────────

prop_compose! {
    /// A fully valid snapshot + proximity + prior history.
    fn arb_case()(
        tod in 0u32..86_400,
        open in PRICE_MIN..PRICE_MAX,
        high in PRICE_MIN..PRICE_MAX,
        low in PRICE_MIN..PRICE_MAX,
        close in PRICE_MIN..PRICE_MAX,
        ema20 in proptest::option::of(PRICE_MIN..PRICE_MAX),
        ema200 in proptest::option::of(PRICE_MIN..PRICE_MAX),
        a in PRICE_MIN..PRICE_MAX,
        b in PRICE_MIN..PRICE_MAX,
        c_raw in 0u64..u64::MAX,
        da in PRICE_MIN..PRICE_MAX,
        db in PRICE_MIN..PRICE_MAX,
        ood in PRICE_MIN..PRICE_MAX,
        pivot_bps in 0i64..2001,
        pdh_pdl_bps in 0i64..2001,
        fib_bps in 0i64..2001,
        prior_n in 0usize..6,
        flags in proptest::collection::vec(any::<bool>(), 0..8),
    ) -> (IndicatorSnapshot, ProximityParams, PriorBarHistory) {
        let pdh = a.max(b);
        let pdl = a.min(b);
        let span = (pdh - pdl + 1) as u64;
        let pdc = pdl + (c_raw % span) as i64;
        let day_high = da.max(db);
        let day_low = da.min(db);

        let snapshot = IndicatorSnapshot {
            tod_seconds: tod,
            open_paisa: open,
            high_paisa: high.max(low),
            low_paisa: high.min(low),
            close_paisa: close,
            ema20_paisa: ema20,
            ema200_paisa: ema200,
            pivots: compute_pivot_levels(pdh, pdl, pdc),
            fib: compute_fib_levels(pdh, pdl),
            day_high_paisa: day_high,
            day_low_paisa: day_low,
            open_of_day_paisa: ood,
        };
        let proximity = ProximityParams {
            pivot_proximity_bps: pivot_bps,
            pdh_pdl_proximity_bps: pdh_pdl_bps,
            fib_proximity_bps: fib_bps,
        };
        let prior = PriorBarHistory { prior_n, flags };
        (snapshot, proximity, prior)
    }
}

prop_compose! {
    /// A valid prev-day anchor triple: `pdl <= pdc <= pdh`, in-domain paisa.
    fn arb_anchors()(
        a in PRICE_MIN..PRICE_MAX,
        b in PRICE_MIN..PRICE_MAX,
        c_raw in 0u64..u64::MAX,
    ) -> (i64, i64, i64) {
        let pdh = a.max(b);
        let pdl = a.min(b);
        let span = (pdh - pdl + 1) as u64;
        let pdc = pdl + (c_raw % span) as i64;
        (pdh, pdl, pdc)
    }
}

prop_compose! {
    /// A snapshot drawn from a TINY integer domain so exact-equality boundaries
    /// (`close == ema20`, `body*10 == range*7`, `pdc == 0`, …) collide often —
    /// this is what kills the `<`/`<=` and `>`/`>=` boundary mutants that a
    /// wide random domain almost never reaches. Every OHLC/structure field is
    /// drawn INDEPENDENTLY (the evaluator's real input contract: it does not
    /// assume `low <= open,close <= high`), so degenerate bars like
    /// `high == low` with `open != close` are exercised too. Only the pivot/fib
    /// anchors are kept well-ordered, because their resolvers assert on it.
    fn arb_small_case()(
        tod in 0u32..86_400,
        open in -8i64..17,
        high in -8i64..17,
        low in -8i64..17,
        close in -8i64..17,
        ema20 in proptest::option::of(-8i64..17),
        ema200 in proptest::option::of(-8i64..17),
        pa in -8i64..17,
        pb in -8i64..17,
        pc_raw in 0u64..64,
        day_high in -8i64..17,
        day_low in -8i64..17,
        ood in -8i64..17,
        pivot_bps in 0i64..40,
        pdh_pdl_bps in 0i64..40,
        fib_bps in 0i64..40,
        prior_n in 0usize..6,
        flags in proptest::collection::vec(any::<bool>(), 0..8),
    ) -> (IndicatorSnapshot, ProximityParams, PriorBarHistory) {
        let pdh = pa.max(pb);
        let pdl = pa.min(pb);
        let span = (pdh - pdl + 1) as u64;
        let pdc = pdl + (pc_raw % span) as i64;

        let snapshot = IndicatorSnapshot {
            tod_seconds: tod,
            open_paisa: open,
            high_paisa: high,
            low_paisa: low,
            close_paisa: close,
            ema20_paisa: ema20,
            ema200_paisa: ema200,
            pivots: compute_pivot_levels(pdh, pdl, pdc),
            fib: compute_fib_levels(pdh, pdl),
            day_high_paisa: day_high,
            day_low_paisa: day_low,
            open_of_day_paisa: ood,
        };
        let proximity = ProximityParams {
            pivot_proximity_bps: pivot_bps,
            pdh_pdl_proximity_bps: pdh_pdl_bps,
            fib_proximity_bps: fib_bps,
        };
        let prior = PriorBarHistory { prior_n, flags };
        (snapshot, proximity, prior)
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(20_000))]

    /// Both indicators match an independent formula re-derivation across every
    /// level — including R4/S4 and the 424 extensions the evaluator never reads.
    #[test]
    fn indicators_match_reference((pdh, pdl, pdc) in arb_anchors()) {
        assert_pivots_match(pdh, pdl, pdc)?;
        assert_fib_match(pdh, pdl)?;
    }

    /// The production evaluator equals the independent reference, bit for bit.
    #[test]
    fn evaluator_matches_reference((s, p, prior) in arb_case()) {
        let got = evaluate_atoms(&s, &p, &prior);
        let want = reference_mask(&s, &p, &prior);
        prop_assert_eq!(
            got, want,
            "mask divergence: got {:#018x}, ref {:#018x}, xor {:#018x}",
            got, want, got ^ want
        );
    }

    /// Structural invariants that hold for every bar.
    #[test]
    fn structural_invariants((s, p, prior) in arb_case()) {
        let m = evaluate_atoms(&s, &p, &prior);
        // Reserved bits 52..=63 are never set.
        prop_assert_eq!(m & !FULL52, 0);
        // Mutually exclusive opposites can never fire together.
        let pair = |a: u32, b: u32| (m >> a) & 1 == 0 || (m >> b) & 1 == 0;
        prop_assert!(pair(bits::BIT_CLOSE_ABOVE_EMA20, bits::BIT_CLOSE_BELOW_EMA20));
        prop_assert!(pair(bits::BIT_CLOSE_ABOVE_EMA200, bits::BIT_CLOSE_BELOW_EMA200));
        prop_assert!(pair(bits::BIT_EMA20_ABOVE_EMA200, bits::BIT_EMA20_BELOW_EMA200));
        prop_assert!(pair(bits::BIT_BAR_BULLISH, bits::BIT_BAR_BEARISH));
        prop_assert!(pair(bits::BIT_GAP_UP_DAY, bits::BIT_GAP_DOWN_DAY));
        prop_assert!(pair(bits::BIT_INSIDE_DAY, bits::BIT_OUTSIDE_DAY));
        // The four time-of-day buckets are disjoint (at most one set).
        let tod_bits = ((m >> bits::BIT_EARLY_MORNING) & 1)
            + ((m >> bits::BIT_MID_MORNING) & 1)
            + ((m >> bits::BIT_MIDDAY) & 1)
            + ((m >> bits::BIT_AFTERNOON) & 1);
        prop_assert!(tod_bits <= 1);
    }
}

proptest! {
    // A large case count over the tiny domain so EVERY exact-equality boundary
    // (and thus every `<`/`>` boundary mutant) is hit many times.
    #![proptest_config(ProptestConfig::with_cases(120_000))]

    /// Small-domain twin of `evaluator_matches_reference`: same bit-for-bit
    /// cross-check, but on inputs that frequently land exactly on comparison
    /// boundaries — the cases that distinguish `>` from `>=`.
    #[test]
    fn evaluator_matches_reference_small_domain((s, p, prior) in arb_small_case()) {
        let got = evaluate_atoms(&s, &p, &prior);
        let want = reference_mask(&s, &p, &prior);
        prop_assert_eq!(
            got, want,
            "mask divergence: got {:#018x}, ref {:#018x}, xor {:#018x}",
            got, want, got ^ want
        );
    }
}
