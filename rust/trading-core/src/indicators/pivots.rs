//! Standard (Floor Trader) Pivot Points — a bit-for-bit port of
//! `brutex/indicators/pivots.py::compute_pivot_levels`.

use crate::indicators::round_half_even;

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
}
