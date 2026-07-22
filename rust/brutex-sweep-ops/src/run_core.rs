//! slice 6 — run-core/orchestrator pure derivations (DEC-RUST-SWEEP-OPS-001).
//!
//! The ONLY pure decision folds of the #1712 unkillable-run-core family:
//! everything else in `sweep_run_core` / `sweep_orchestrator` /
//! `sweep_driver` is CM/thread/OS/durable-I/O land, run-identity boundary
//! (`grid_identity` / `_tuple_digest` — NEVER converted), or an existing
//! Rust story (`sweep_driver` metrics — the fno_math kernels own that).
//!
//! * `tuple_record_stride` — the D-9 TUPLE_DONE decimation stride. The
//!   CPython oracle computes `math.ceil(max(0, t) / 10_000)` with TRUE
//!   (f64) division, so the fold is mirrored in f64 here; the facade gates
//!   `total_tuples` to the shared |v| <= 2^50 band (the slice-4 scheduler
//!   band law) where `t as f64` is exact and IEEE division + ceil are
//!   bit-identical cross-language. (This single-division law is itself exact
//!   well past that band — see the `stride_exact_band_edge` crate test — but
//!   the facade uses ONE conservative band, sized for the scheduler's
//!   tighter 3-term-sum need.)
//! * `per_tuple_diffcheck_budget` — the R45 K-budget distribution
//!   (design D-11): positive//positive floor division ≡ Rust integer `/`.
//! * `grid_tuple_count` — the grid outer-product cardinality over
//!   PRE-EXTRACTED axis lengths (Python owns the object marshaling);
//!   checked u128 product, `Err` past u64 (the facade degrades to the
//!   Python bigint oracle — a legitimate astronomically-large grid is
//!   oracle territory, never a panic).

/// The D-9 decimation target — `ceil(total / 10_000)` keeps at most
/// ~10k TUPLE_DONE records per grid (the Python constant's f64 mirror).
pub const DECIMATION_TARGET_RECORDS: f64 = 10_000.0;

/// The D-9 TUPLE_DONE decimation stride: `max(1, ceil(max(0, t) / 10_000))`.
///
/// f64 arithmetic BY DESIGN — the CPython oracle's `/` is true division;
/// inputs are facade-gated to the shared |v| <= 2^50 band so the f64 mirror
/// is exact (this single-division law is exact wider still — see
/// `stride_exact_band_edge`).
#[must_use]
pub fn tuple_record_stride(total_tuples: i64) -> u64 {
    let clamped = total_tuples.max(0) as f64;
    let stride = (clamped / DECIMATION_TARGET_RECORDS).ceil();
    stride.max(1.0) as u64
}

/// The R45 per-tuple diffcheck budget: `0` disabled, else
/// `max(1, k_per_run // max(1, total_tuples))`.
#[must_use]
pub fn per_tuple_diffcheck_budget(k_per_run: i64, total_tuples: i64) -> u64 {
    if k_per_run <= 0 {
        return 0;
    }
    let total = total_tuples.max(1) as u64;
    let per = (k_per_run as u64) / total;
    per.max(1)
}

/// The grid outer-product tuple count over pre-extracted axis cardinalities.
///
/// Checked u128 accumulation; `Err` when the product exceeds `u64::MAX`
/// (the facade degrades to the Python bigint oracle). An empty axis list
/// folds to 1 (the empty product), matching `math.prod(())`.
pub fn grid_tuple_count(axes: &[u64]) -> Result<u64, String> {
    let mut acc: u128 = 1;
    for &n in axes {
        acc = acc
            .checked_mul(u128::from(n))
            .ok_or_else(|| "axis product overflows u128".to_string())?;
        if acc > u128::from(u64::MAX) {
            return Err("axis product exceeds u64".to_string());
        }
    }
    Ok(acc as u64)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stride_small_grids_record_every_tuple() {
        for t in [i64::MIN, -1, 0, 1, 9_999, 10_000] {
            assert_eq!(tuple_record_stride(t), 1, "t={t}");
        }
    }

    #[test]
    fn stride_ceil_law_hand_derived() {
        assert_eq!(tuple_record_stride(10_001), 2);
        assert_eq!(tuple_record_stride(25_000), 3); // ceil(2.5) = 3
        assert_eq!(tuple_record_stride(100_000), 10);
        assert_eq!(tuple_record_stride(100_001), 11);
    }

    #[test]
    fn stride_exact_band_edge() {
        // 2^52 is exactly representable; ceil(2^52 / 1e4) hand-derived. This
        // deliberately exceeds the facade's |v| <= 2^50 gate — the crate's
        // single-division stride law is exact wider than the shared band the
        // facade conservatively routes within (sized for the scheduler).
        let t: i64 = 1 << 52;
        let expected = ((t as f64) / 10_000.0).ceil() as u64;
        assert_eq!(tuple_record_stride(t), expected);
        assert!(expected >= 450_359_962_737);
    }

    #[test]
    fn budget_disabled_is_zero() {
        for k in [i64::MIN, -1, 0] {
            assert_eq!(per_tuple_diffcheck_budget(k, 10), 0, "k={k}");
        }
    }

    #[test]
    fn budget_floor_division_with_floor_one() {
        assert_eq!(per_tuple_diffcheck_budget(7, 3), 2);
        assert_eq!(per_tuple_diffcheck_budget(2, 5), 1); // floor 1
        assert_eq!(per_tuple_diffcheck_budget(200, 1), 200);
        // Non-positive totals degrade to 1 tuple.
        assert_eq!(per_tuple_diffcheck_budget(9, 0), 9);
        assert_eq!(per_tuple_diffcheck_budget(9, -4), 9);
    }

    #[test]
    fn count_empty_axes_is_the_empty_product() {
        assert_eq!(grid_tuple_count(&[]), Ok(1));
    }

    #[test]
    fn count_product_hand_derived() {
        assert_eq!(grid_tuple_count(&[3, 21, 2, 6, 5, 5, 1, 3, 1]), Ok(56_700));
        assert_eq!(grid_tuple_count(&[2, 0, 9]), Ok(0));
    }

    #[test]
    fn count_u64_boundary() {
        assert_eq!(grid_tuple_count(&[u64::MAX, 1]), Ok(u64::MAX));
        assert!(grid_tuple_count(&[u64::MAX, 2]).is_err());
        assert!(grid_tuple_count(&[u64::MAX, u64::MAX, u64::MAX]).is_err());
    }
}
