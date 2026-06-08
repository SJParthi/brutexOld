#![no_main]
//! Fuzz targets #4 + #5 (`fuzz_pivot_resolve` / `fuzz_fib_resolve`) from the
//! sibling `fno-math/fuzz/README.md`, here against the SHARED kernel
//! `trading_core::{compute_pivot_levels, compute_fib_levels}`
//! (`src/indicators/{pivots,fib}.rs`).
//!
//! ## Domain (honest — matches the crate's documented contract)
//!
//! `trading-core`'s `lib.rs` documents two INTENTIONAL panics that are NOT
//! bugs and therefore out of fuzz scope:
//! * corrupt prev-day OHLC (`pdh < pdl`, or `pdc ∉ [pdl, pdh]`) → always-on
//!   `assert!` (mirrors Python's `ValueError`);
//! * absurd `i64`-scale magnitudes (`> ~10¹⁷` paisa) → release `overflow-checks`
//!   panic (a finance-safety choice).
//!
//! So the fuzzer maps arbitrary bytes into the REALISTIC envelope
//! (`lib.rs`: Indian index/stock paisa ≈ 10⁶–10⁹) — capped at `MAX_PAISA`
//! (₹100,000,000) with `pdh >= pdl` and `pdc ∈ [pdl, pdh]` — and asserts the
//! resolvers NEVER panic and ALWAYS produce the documented monotone ladder.
//! At that cap every internal product (`pdh + 3·(p−pdl)`, `pdh + 3.236·range`)
//! stays far below `i64::MAX`, so panic-freedom is the real invariant under
//! test. Out-of-domain inputs are a separate, asserted contract (the crate's
//! own unit tests cover the panic path).
//!
//! Python⇆Rust bit-identity is covered by `tests/golden_vectors.rs`
//! (400-case answer-key); this target adds panic-freedom + structural
//! invariants over the full in-domain space.

use libfuzzer_sys::fuzz_target;
use trading_core::{compute_fib_levels, compute_pivot_levels};

/// ₹100,000,000 in paisa — far above any Indian index/stock, far below the
/// `~10¹⁷` overflow bound, so all internal arithmetic is panic-free.
const MAX_PAISA: i64 = 10_000_000_000;

fn read_u64(data: &[u8], off: usize) -> u64 {
    let mut b = [0u8; 8];
    for (k, slot) in b.iter_mut().enumerate() {
        *slot = data.get(off + k).copied().unwrap_or(0);
    }
    u64::from_le_bytes(b)
}

/// Map an arbitrary u64 into `[1, MAX_PAISA]`.
fn price(raw: u64) -> i64 {
    1 + (raw % MAX_PAISA as u64) as i64
}

fuzz_target!(|data: &[u8]| {
    if data.len() < 24 {
        return;
    }
    let a = price(read_u64(data, 0));
    let b = price(read_u64(data, 8));
    let (pdl, pdh) = if a <= b { (a, b) } else { (b, a) };
    // pdc anywhere in [pdl, pdh].
    let span = (pdh - pdl) as u64;
    let pdc = if span == 0 {
        pdl
    } else {
        pdl + (read_u64(data, 16) % (span + 1)) as i64
    };

    // ── pivots: never panic on valid OHLC; documented monotone ladder ──
    let pv = compute_pivot_levels(pdh, pdl, pdc);
    assert!(
        pv.pdl_paisa <= pv.p_paisa && pv.p_paisa <= pv.pdh_paisa,
        "central pivot P must lie in [PDL, PDH]"
    );
    assert!(pv.r1_paisa >= pv.p_paisa, "R1 must be >= P");
    assert!(pv.s1_paisa <= pv.p_paisa, "S1 must be <= P");
    assert!(
        pv.r4_paisa >= pv.r3_paisa && pv.r3_paisa >= pv.pdh_paisa,
        "resistance ladder R4 >= R3 >= PDH must hold"
    );
    assert!(
        pv.s4_paisa <= pv.s3_paisa && pv.s3_paisa <= pv.pdl_paisa,
        "support ladder S4 <= S3 <= PDL must hold"
    );
    // Anchors are echoed through unchanged.
    assert_eq!((pv.pdh_paisa, pv.pdl_paisa, pv.pdc_paisa), (pdh, pdl, pdc));

    // ── fib: never panic; non-negative range; midpoint inside the band ──
    let fb = compute_fib_levels(pdh, pdl);
    assert!(fb.range_paisa >= 0, "fib range must be non-negative");
    assert_eq!(fb.range_paisa, pdh - pdl, "range must equal PDH - PDL");
    assert_eq!(fb.retrace_level_at_pdh_paisa, pdh, "fib 0% anchor is PDH");
    assert_eq!(fb.retrace_level_at_pdl_paisa, pdl, "fib 100% anchor is PDL");
    assert!(
        pdl <= fb.retrace_50_paisa && fb.retrace_50_paisa <= pdh,
        "the 50% retracement must lie within [PDL, PDH]"
    );
    // Up-extensions sit above PDH, down-extensions below PDL (range >= 0).
    assert!(fb.ext_up_127_paisa >= pdh, "127% up-extension must be >= PDH");
    assert!(fb.ext_down_127_paisa <= pdl, "127% down-extension must be <= PDL");
});
