#![no_main]
//! Fuzz target for the Stage-2 SUPERSET-count hot kernel
//! ([`fno_math::python::count_supermask_u64s`] /
//! [`fno_math::python::count_supermasks_u64s`], `src/python.rs`) — the
//! `(bar & mask) == mask` brute-force accelerator (README target family #2,
//! superset variant).
//!
//! On ARBITRARY bytes it asserts:
//!
//! 1. **Total + bounded** — the count never panics and is always `<= n_bars`.
//! 2. **`mask == 0` matches every bar** — the vacuous-superset contract
//!    (`(bar & 0) == 0` always), so the count equals `n_bars`.
//! 3. **Batch == single, positionally** — `count_supermasks_u64s` must equal
//!    the element-wise `count_supermask_u64s` for `[mask, 0, !mask]` (an
//!    arbitrary mask, the all-match, and the complement) and preserve order.
//!
//! These mirror the unit tests in `src/python.rs` but over the full adversarial
//! u64 space rather than hand-picked vectors.

use fno_math::python::{count_supermask_u64s, count_supermasks_u64s};
use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    if data.len() < 8 {
        return;
    }
    let mut m = [0u8; 8];
    m.copy_from_slice(&data[..8]);
    let mask = u64::from_le_bytes(m);

    let bars: Vec<u64> = data[8..]
        .chunks_exact(8)
        .map(|c| {
            let mut x = [0u8; 8];
            x.copy_from_slice(c);
            u64::from_le_bytes(x)
        })
        .collect();
    let n = bars.len() as u64;

    // 1. Total + bounded.
    let single = count_supermask_u64s(bars.clone(), mask);
    assert!(single <= n, "superset count {single} exceeds bar count {n}");

    // 2. mask == 0 matches every bar.
    assert_eq!(
        count_supermask_u64s(bars.clone(), 0),
        n,
        "mask == 0 must match every bar (vacuous superset)"
    );

    // 3. Batch == single element-wise, order preserved.
    let masks = vec![mask, 0u64, !mask];
    let batch = count_supermasks_u64s(bars.clone(), masks.clone());
    assert_eq!(batch.len(), masks.len(), "batch length must equal masks length");
    for (j, &mj) in masks.iter().enumerate() {
        assert_eq!(
            batch[j],
            count_supermask_u64s(bars.clone(), mj),
            "batch arm {j} diverged from the single-mask kernel"
        );
    }
    // The first batch arm is exactly the single-mask result computed above.
    assert_eq!(batch[0], single);
});
