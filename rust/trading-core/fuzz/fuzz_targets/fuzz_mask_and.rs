//! `fuzz_mask_and` — fuzz the u64 52-condition evaluator + its mask algebra.
//!
//! Two guarantees on arbitrary (domain-valid) input:
//!   1. `evaluate_atoms` never panics / overflows and NEVER sets a reserved bit
//!      (52..=63) — the strategy-mask layout stays dense 0..=51.
//!   2. The `u64` mask AND used by the supermask scan is total, idempotent, has
//!      `FULL52` as identity for a 52-bit mask, and is commutative + associative
//!      (README fno-math target #2 semantics, here proven against a real mask).

#![no_main]

use libfuzzer_sys::fuzz_target;
use trading_core::evaluate_atoms;

mod common;
use common::{FULL52, RawSnapshot};

fuzz_target!(|input: (RawSnapshot, u64, u64)| {
    let (raw, other, third) = input;
    let (snapshot, proximity, prior) = common::build(&raw);

    // (1) Evaluate — must not panic / overflow, and reserved bits stay clear.
    let mask = evaluate_atoms(&snapshot, &proximity, &prior);
    assert_eq!(
        mask & !FULL52,
        0,
        "evaluator set a reserved bit (52..=63): {mask:#018x}"
    );

    // (2) AND algebra over the produced mask + two arbitrary u64 operands.
    assert_eq!(mask & mask, mask, "AND not idempotent");
    assert_eq!(mask & FULL52, mask, "FULL52 not an identity for a 52-bit mask");
    assert_eq!(mask & 0, 0, "AND with 0 not annihilating");
    assert_eq!(mask & other, other & mask, "AND not commutative");
    assert_eq!(
        (mask & other) & third,
        mask & (other & third),
        "AND not associative"
    );

    // The AND result is a subset of both operands (the supermask-scan invariant).
    let anded = mask & other;
    assert_eq!(anded & mask, anded, "AND result not a subset of mask");
    assert_eq!(anded & other, anded, "AND result not a subset of other");
    assert!(
        anded.count_ones() <= mask.count_ones().min(other.count_ones()),
        "AND widened the popcount"
    );
});
