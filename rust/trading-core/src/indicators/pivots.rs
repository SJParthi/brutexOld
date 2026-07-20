//! Standard (Floor Trader) Pivot Points — a bit-for-bit port of
//! `brutex/indicators/pivots.py::compute_pivot_levels` — plus the Central
//! Pivot Range (CPR) derived from the SAME prev-day OHLC anchor
//! (vocabulary bits 60-63).

use crate::indicators::round_half_even;

/// Narrow-CPR width threshold in basis points of the pivot P: bit 63
/// (`narrow_cpr_day`) fires iff `(TC − BC) / P <= NARROW_CPR_WIDTH_BPS / 10_000`
/// (evaluated in exact integer form — see `evaluator.rs` Group 13).
///
/// NO vendor standard exists for "narrow": practitioners scatter across
/// 0.2%–0.5% of price with 0.3% the most-cited figure; **30 bps** was chosen by
/// the design authority (a looser threshold risks the bit firing near-always on
/// index days). A future DEC may override it — the switch is this ONE named
/// compile-time constant (e.g. a one-line 30 ↔ 50 change), which re-keys runs
/// through `indicator_version` like any other math change. `pub` so the Python
/// side can cross-pin the value.
pub const NARROW_CPR_WIDTH_BPS: i64 = 30;

/// The canonicalized Central Pivot Range pair (int64 paisa): `tc_paisa >=
/// bc_paisa` ALWAYS (see [`compute_cpr_levels`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CprLevels {
    /// Top central (the larger of the raw TC/BC pair after canonicalization).
    pub tc_paisa: i64,
    /// Bottom central (the smaller of the raw pair).
    pub bc_paisa: i64,
}

/// Compute the Central Pivot Range from the ROUNDED pivot `P` plus the RAW
/// previous-day high/low (int64 paisa) — the Ochoa / PivotBoss standard
/// (verified 2026-07-15 against PivotBoss + Zerodha Varsity):
///
/// * `BC = (PDH + PDL) / 2` — rounded half-even, the SAME rounding the pivot
///   `P` itself uses ([`compute_pivot_levels`] / `round_half_even`);
/// * `TC = 2·P − BC` — pure integer arithmetic on the two rounded values
///   (`P` is passed in as the already-rounded `PivotLevels::p_paisa`, NEVER
///   re-derived from rounded R/S rungs — that would compound rounding).
///
/// The raw `(tc, bc)` pair INVERTS (`tc_raw < bc_raw`) whenever the prev-day
/// close sits below the (PDH+PDL)/2 midpoint — roughly half of real days — so
/// the pair is CANONICALIZED here: `tc = max(tc_raw, bc_raw)`,
/// `bc = min(tc_raw, bc_raw)` (pinned by `cpr_canonicalizes_the_inverted_pair`).
///
/// Domain (mirrors [`compute_pivot_levels`]'s honest envelope): the arithmetic
/// is overflow-free for every boundary-validated caller — the FFI paths bound
/// the anchors (`|x| <= i64::MAX / 8`) so `pdh + pdl` and `2·p − bc` stay far
/// inside `i64`; an absurd unvalidated native magnitude panics via the release
/// `overflow-checks` rather than silently wrapping (finance-safety, `lib.rs`).
#[must_use]
pub fn compute_cpr_levels(p_paisa: i64, pdh_paisa: i64, pdl_paisa: i64) -> CprLevels {
    // Float only here: the midpoint of the two anchors, rounded to whole paisa
    // exactly like the pivot P (round-half-even == Python round()).
    let bc_raw = round_half_even((pdh_paisa + pdl_paisa) as f64 / 2.0);
    let tc_raw = 2 * p_paisa - bc_raw;
    CprLevels {
        tc_paisa: tc_raw.max(bc_raw),
        bc_paisa: tc_raw.min(bc_raw),
    }
}

/// The 11 pivot levels (P, R1-R5, S1-S5) + the previous-day OHLC anchors, all
/// int64 paisa. R5/S5 extend the same linear floor-trader ladder
/// (`Rn = PDH + (n−1)·(P − PDL)`, `Sn = PDL − (n−1)·(PDH − P)`) past R4/S4 —
/// added in PR-2 for the new pivot-proximity bits 54/55
/// (DEC-NIFTY-INDICATORS-001 / ADR-0012 D3).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PivotLevels {
    pub pdh_paisa: i64,
    pub pdl_paisa: i64,
    pub pdc_paisa: i64,
    pub r5_paisa: i64,
    pub r4_paisa: i64,
    pub r3_paisa: i64,
    pub r2_paisa: i64,
    pub r1_paisa: i64,
    pub p_paisa: i64,
    pub s1_paisa: i64,
    pub s2_paisa: i64,
    pub s3_paisa: i64,
    pub s4_paisa: i64,
    pub s5_paisa: i64,
}

/// Compute pivot points from the previous day's high/low/close (int64 paisa).
///
/// Only the central pivot `p` uses floating point (`(pdh+pdl+pdc)/3.0` then
/// round-half-even); every R/S level is pure integer arithmetic on the rounded
/// integer `p` — exactly mirroring `pivots.py:136-152`.
///
/// The Python source raises on `pdh < pdl` or `pdc` outside `[pdl, pdh]`; the
/// golden inputs always satisfy these, so the guards are `debug_assert!`s
/// (invalid-input handling is out of Phase-1 scope).
#[must_use]
pub fn compute_pivot_levels(pdh_paisa: i64, pdl_paisa: i64, pdc_paisa: i64) -> PivotLevels {
    // `assert!` (always-on, incl. release) so corrupt prev-day OHLC fails LOUD —
    // matching Python's `ValueError` (`pivots.py:124-132`). A `debug_assert!`
    // would be compiled out in release and silently produce a wrong result.
    assert!(pdh_paisa >= pdl_paisa, "PDH must be >= PDL");
    assert!(
        pdl_paisa <= pdc_paisa && pdc_paisa <= pdh_paisa,
        "PDC must be in [PDL, PDH]"
    );

    // Float only here: average of the three, rounded to whole paisa.
    let p = round_half_even((pdh_paisa + pdl_paisa + pdc_paisa) as f64 / 3.0);

    let range_paisa = pdh_paisa - pdl_paisa;
    let p_minus_pdl = p - pdl_paisa;
    let pdh_minus_p = pdh_paisa - p;

    PivotLevels {
        pdh_paisa,
        pdl_paisa,
        pdc_paisa,
        // R5/S5 extend the linear ladder one more step (coefficient 4 on the
        // (P − PDL)/(PDH − P) deltas), mirroring R4/S4's coefficient 3.
        r5_paisa: pdh_paisa + 4 * p_minus_pdl,
        r4_paisa: pdh_paisa + 3 * p_minus_pdl,
        r3_paisa: pdh_paisa + 2 * p_minus_pdl,
        r2_paisa: p + range_paisa,
        r1_paisa: 2 * p - pdl_paisa,
        p_paisa: p,
        s1_paisa: 2 * p - pdh_paisa,
        s2_paisa: p - range_paisa,
        s3_paisa: pdl_paisa - 2 * pdh_minus_p,
        s4_paisa: pdl_paisa - 3 * pdh_minus_p,
        s5_paisa: pdl_paisa - 4 * pdh_minus_p,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The NIFTY 14-May-2026 anchor from the operator screenshot (paisa). R1-R4 /
    /// S1-S4 are screenshot-verified; R5/S5 are CODE-ANCHORED (no operator chart
    /// pins them) — they extend the same proven linear ladder one step further.
    const PDH: i64 = 2_377_715;
    const PDL: i64 = 2_342_650;
    const PDC: i64 = 2_368_965;

    #[test]
    fn r5_s5_extend_the_linear_ladder() {
        let pv = compute_pivot_levels(PDH, PDL, PDC);
        let p_minus_pdl = pv.p_paisa - PDL;
        let pdh_minus_p = PDH - pv.p_paisa;
        // R5 = PDH + 4·(P − PDL); S5 = PDL − 4·(PDH − P) — the n=5 ladder rung.
        assert_eq!(pv.r5_paisa, PDH + 4 * p_minus_pdl);
        assert_eq!(pv.s5_paisa, PDL - 4 * pdh_minus_p);
    }

    #[test]
    fn r5_s5_continue_the_constant_step_past_r4_s4() {
        let pv = compute_pivot_levels(PDH, PDL, PDC);
        // The ladder is arithmetic: each rung is a constant step. R5−R4 == R4−R3
        // and S4−S5 == S3−S4 (the same (P−PDL)/(PDH−P) delta), so R5/S5 are the
        // honest continuation, not a new formula.
        assert_eq!(pv.r5_paisa - pv.r4_paisa, pv.r4_paisa - pv.r3_paisa);
        assert_eq!(pv.s4_paisa - pv.s5_paisa, pv.s3_paisa - pv.s4_paisa);
        // Monotone ladder: R5 > R4 > R3 and S5 < S4 < S3 (range > 0 here).
        assert!(pv.r5_paisa > pv.r4_paisa);
        assert!(pv.r4_paisa > pv.r3_paisa);
        assert!(pv.s5_paisa < pv.s4_paisa);
        assert!(pv.s4_paisa < pv.s3_paisa);
    }

    // ── Central Pivot Range (bits 60-63 support) ───────────────────────────

    #[test]
    fn cpr_exact_values_on_the_nifty_anchor() {
        // Hand-derived from the NIFTY 14-May-2026 anchor above:
        //   P  = round((2_377_715 + 2_342_650 + 2_368_965) / 3)
        //      = round(7_089_330 / 3) = 2_363_110
        //   BC = round((2_377_715 + 2_342_650) / 2) = round(4_720_365 / 2)
        //      = round(2_360_182.5) → half-even → 2_360_182
        //   TC = 2·2_363_110 − 2_360_182 = 2_366_038  (tc_raw > bc_raw: no swap)
        let pv = compute_pivot_levels(PDH, PDL, PDC);
        assert_eq!(pv.p_paisa, 2_363_110);
        let cpr = compute_cpr_levels(pv.p_paisa, PDH, PDL);
        assert_eq!(cpr.bc_paisa, 2_360_182);
        assert_eq!(cpr.tc_paisa, 2_366_038);
    }

    #[test]
    fn cpr_canonicalizes_the_inverted_pair() {
        // A prev-day close BELOW the (PDH+PDL)/2 midpoint inverts the raw pair
        // (tc_raw < bc_raw) — roughly half of real days. PDC near PDL:
        //   P  = round((2_377_715 + 2_342_650 + 2_343_000) / 3) = round(7_063_365/3)
        //      = 2_354_455
        //   BC_raw = 2_360_182 (midpoint, unchanged)
        //   TC_raw = 2·2_354_455 − 2_360_182 = 2_348_728 < BC_raw → SWAP.
        let pdc_low = 2_343_000;
        let pv = compute_pivot_levels(PDH, PDL, pdc_low);
        assert_eq!(pv.p_paisa, 2_354_455);
        let cpr = compute_cpr_levels(pv.p_paisa, PDH, PDL);
        assert_eq!(cpr.tc_paisa, 2_360_182, "canonical TC = the larger raw leg");
        assert_eq!(
            cpr.bc_paisa, 2_348_728,
            "canonical BC = the smaller raw leg"
        );
        assert!(cpr.tc_paisa >= cpr.bc_paisa);
    }

    #[test]
    fn cpr_bc_rounding_is_half_even_like_the_pivot_p() {
        // An odd PDH+PDL sum lands BC on an exact .5 paisa tie — the rounding
        // must be half-EVEN (Python round()), not half-up: 4_720_365 / 2
        // = 2_360_182.5 → 2_360_182 (even), never 2_360_183.
        let cpr = compute_cpr_levels(2_363_110, PDH, PDL);
        assert_eq!(cpr.bc_paisa, 2_360_182);
    }

    #[test]
    fn cpr_degenerate_flat_anchor_collapses_to_a_point() {
        // PDH == PDL == PDC: P == BC == TC (width 0 — the narrow-CPR extreme).
        let pv = compute_pivot_levels(1_000_000, 1_000_000, 1_000_000);
        let cpr = compute_cpr_levels(pv.p_paisa, 1_000_000, 1_000_000);
        assert_eq!(cpr.tc_paisa, 1_000_000);
        assert_eq!(cpr.bc_paisa, 1_000_000);
    }

    #[test]
    fn narrow_cpr_width_constant_is_the_designed_30_bps() {
        // The ONE named switch (a future DEC may flip it 30 ↔ 50); the exact
        // consumption law `(tc − bc)·10_000 <= p·NARROW_CPR_WIDTH_BPS` lives in
        // `evaluator.rs` Group 13 and is property-pinned in tests/proptests.rs.
        assert_eq!(NARROW_CPR_WIDTH_BPS, 30);
    }
}
