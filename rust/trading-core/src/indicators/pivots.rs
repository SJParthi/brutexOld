//! Standard (Floor Trader) Pivot Points — a bit-for-bit port of
//! `brutex/indicators/pivots.py::compute_pivot_levels`.

use crate::indicators::round_half_even;

/// The 9 pivot levels + the previous-day OHLC anchors, all int64 paisa.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PivotLevels {
    pub pdh_paisa: i64,
    pub pdl_paisa: i64,
    pub pdc_paisa: i64,
    pub r4_paisa: i64,
    pub r3_paisa: i64,
    pub r2_paisa: i64,
    pub r1_paisa: i64,
    pub p_paisa: i64,
    pub s1_paisa: i64,
    pub s2_paisa: i64,
    pub s3_paisa: i64,
    pub s4_paisa: i64,
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
        r4_paisa: pdh_paisa + 3 * p_minus_pdl,
        r3_paisa: pdh_paisa + 2 * p_minus_pdl,
        r2_paisa: p + range_paisa,
        r1_paisa: 2 * p - pdl_paisa,
        p_paisa: p,
        s1_paisa: 2 * p - pdh_paisa,
        s2_paisa: p - range_paisa,
        s3_paisa: pdl_paisa - 2 * pdh_minus_p,
        s4_paisa: pdl_paisa - 3 * pdh_minus_p,
    }
}
