//! `fuzz_pivot_fib_resolve` — fuzz the evaluate-time indicator resolvers.
//!
//! On any valid `(pdh, pdl, pdc)` triple (the corrupt-OHLC panic is documented
//! and intentional — see common.rs), `compute_pivot_levels` and
//! `compute_fib_levels` must:
//!   1. never panic / overflow,
//!   2. echo their anchors back,
//!   3. produce the monotone pivot ladder R4>=R3>=R2>=R1>=P>=S1>=S2>=S3>=S4
//!      (algebraically true for ALL valid inputs),
//!   4. keep every fib retracement inside `[pdl, pdh]` and bracket the range with
//!      the extensions, and
//!   5. agree with the closed-form midpoint (cross-check pivots vs fib anchor).

#![no_main]

use libfuzzer_sys::fuzz_target;
use trading_core::{compute_fib_levels, compute_pivot_levels};

mod common;
use common::to_price;

fuzz_target!(|data: (i64, i64, i64)| {
    let (ra, rb, rc) = data;
    let a = to_price(ra);
    let b = to_price(rb);
    let pdh = a.max(b);
    let pdl = a.min(b);
    let span = pdh - pdl + 1;
    let pdc = pdl + rc.rem_euclid(span); // pdc in [pdl, pdh] — the valid domain

    let piv = compute_pivot_levels(pdh, pdl, pdc);
    let fib = compute_fib_levels(pdh, pdl);

    // (2) anchors echoed back.
    assert_eq!(piv.pdh_paisa, pdh);
    assert_eq!(piv.pdl_paisa, pdl);
    assert_eq!(piv.pdc_paisa, pdc);
    assert_eq!(fib.pdh_paisa, pdh);
    assert_eq!(fib.pdl_paisa, pdl);
    assert_eq!(fib.range_paisa, pdh - pdl);

    // (3) monotone pivot ladder.
    assert!(piv.r4_paisa >= piv.r3_paisa, "R4<R3");
    assert!(piv.r3_paisa >= piv.r2_paisa, "R3<R2");
    assert!(piv.r2_paisa >= piv.r1_paisa, "R2<R1");
    assert!(piv.r1_paisa >= piv.p_paisa, "R1<P");
    assert!(piv.p_paisa >= piv.s1_paisa, "P<S1");
    assert!(piv.s1_paisa >= piv.s2_paisa, "S1<S2");
    assert!(piv.s2_paisa >= piv.s3_paisa, "S2<S3");
    assert!(piv.s3_paisa >= piv.s4_paisa, "S3<S4");
    // P is the rounded mean of three in-range values, so it stays in [pdl, pdh].
    assert!(piv.p_paisa >= pdl && piv.p_paisa <= pdh, "P out of [pdl, pdh]");

    // (4) retracements within [pdl, pdh]; extensions bracket the range.
    for lvl in [
        fib.retrace_23_paisa,
        fib.retrace_38_paisa,
        fib.retrace_50_paisa,
        fib.retrace_62_paisa,
        fib.retrace_78_paisa,
    ] {
        assert!(lvl >= pdl && lvl <= pdh, "retracement {lvl} out of [pdl, pdh]");
    }
    for up in [
        fib.ext_up_127_paisa,
        fib.ext_up_162_paisa,
        fib.ext_up_200_paisa,
        fib.ext_up_262_paisa,
        fib.ext_up_424_paisa,
    ] {
        assert!(up >= pdh, "up-extension {up} below pdh");
    }
    for down in [
        fib.ext_down_127_paisa,
        fib.ext_down_162_paisa,
        fib.ext_down_200_paisa,
        fib.ext_down_262_paisa,
        fib.ext_down_424_paisa,
    ] {
        assert!(down <= pdl, "down-extension {down} above pdl");
    }

    // (5) fib midpoint == closed-form round-half-even of (pdh+pdl)/2.
    let midpoint = ((pdh + pdl) as f64 / 2.0).round_ties_even() as i64;
    assert_eq!(fib.retrace_50_paisa, midpoint, "retrace_50 != midpoint");
});
