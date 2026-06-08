#![no_main]
//! Fuzz target #2 (`fuzz_mask_and`) from `fno-math/fuzz/README.md`.
//!
//! Exercises the Stage-1B AND-mask popcount kernel
//! [`fno_math::python::bitand_count_u64s`] (`src/python.rs`). On ARBITRARY
//! bytes it asserts the kernel is:
//!
//! 1. **Total** — never panics / overflows (u64 AND + popcount are total ops;
//!    the popcount sum cannot overflow u64 for any input the fuzzer can build).
//! 2. **Commutative** — `popcount(a & b) == popcount(b & a)`.
//! 3. **Bounded** — `popcount(a & b) <= min(popcount(a), popcount(b))`, since
//!    `a & b` clears bits, never sets new ones.
//!
//! The mismatched-length branch (which raises `ValueError`) is OUT of scope:
//! we feed equal-length operands by construction (one (a, b) pair per 16 input
//! bytes), so the Python error path — which would need a live interpreter — is
//! never reached. Python⇆Rust bit-identity for these primitives is covered by
//! `tests/unit/test_fno_math_bridge.py`; this target proves panic/overflow
//! freedom + the algebraic invariants on adversarial input.

use fno_math::python::{bitand_count_u64s, bitcount_u64s};
use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    // One 16-byte group -> one (a_i, b_i) u64 pair; equal lengths by build.
    let mut a: Vec<u64> = Vec::with_capacity(data.len() / 16);
    let mut b: Vec<u64> = Vec::with_capacity(data.len() / 16);
    for chunk in data.chunks_exact(16) {
        let mut x = [0u8; 8];
        let mut y = [0u8; 8];
        x.copy_from_slice(&chunk[..8]);
        y.copy_from_slice(&chunk[8..]);
        a.push(u64::from_le_bytes(x));
        b.push(u64::from_le_bytes(y));
    }

    // 1. Total: equal lengths -> always Ok, never panics / overflows.
    let ab = bitand_count_u64s(a.clone(), b.clone()).expect("equal lengths never error");

    // 2. Commutative.
    let ba = bitand_count_u64s(b.clone(), a.clone()).expect("equal lengths never error");
    assert_eq!(ab, ba, "AND popcount must be commutative");

    // 3. Bounded by each operand's own popcount.
    let pa = bitcount_u64s(a);
    let pb = bitcount_u64s(b);
    assert!(ab <= pa, "popcount(a & b) exceeds popcount(a)");
    assert!(ab <= pb, "popcount(a & b) exceeds popcount(b)");
});
