#![no_main]
//! Fuzz target for the Black-76 Greeks kernel `trading_core::greeks`
//! (`src/greeks/mod.rs`, DEC-GREEKS-ENGINE-PR2-001) — the validating entry
//! point whose documented contract is **"Never panics"**: every
//! caller-contract violation is a clean `Err("invalid_input: …")`, every
//! honest no-solution an `Err("no_solution: …")`.
//!
//! Two phases per input:
//!
//! 1. **Raw-bits phase** — the f64 arguments are taken as RAW IEEE-754 bit
//!    patterns from the fuzz bytes (NaN, ±∞, subnormals, negative zero, …)
//!    and the ONLY assertion is the documented one: no panic, and any `Err`
//!    is one of the two documented classes.
//! 2. **In-domain phase** — the same bytes are mapped into the realistic
//!    envelope (positive-normal forward/strike, `t ∈ [T_FLOOR, ~10y]`,
//!    `|rate| ≤ 0.5`, sigma inside the clamp OR a positive price), and a
//!    returned tuple must be numerically sane: finite fields, `iv` inside
//!    the clamp, delta within its side's bounds, `gamma ≥ 0`, `vega ≥ 0`,
//!    `price ≥ 0`; on the SOLVE path (price given) the returned model price
//!    must reproduce the input price within the verified round-trip band.

use libfuzzer_sys::fuzz_target;
use trading_core::greeks::{ERR_INVALID, ERR_NO_SOLUTION, IV_CLAMP_MAX, IV_CLAMP_MIN, T_FLOOR_YEARS};
use trading_core::{GreeksOut, greeks};

fn read_u64(data: &[u8], off: usize) -> u64 {
    let mut b = [0u8; 8];
    for (k, slot) in b.iter_mut().enumerate() {
        *slot = data.get(off + k).copied().unwrap_or(0);
    }
    u64::from_le_bytes(b)
}

/// A raw IEEE-754 bit pattern — deliberately including NaN/±∞/subnormals.
fn raw_f64(data: &[u8], off: usize) -> f64 {
    f64::from_bits(read_u64(data, off))
}

/// Map an arbitrary u64 into `[lo, hi]` (uniform enough for fuzzing).
fn in_range(raw: u64, lo: f64, hi: f64) -> f64 {
    lo + (raw as f64 / u64::MAX as f64) * (hi - lo)
}

fn assert_documented_err(msg: &str) {
    assert!(
        msg.starts_with(ERR_INVALID) || msg.starts_with(ERR_NO_SOLUTION),
        "greeks() returned an undocumented error class: {msg}"
    );
}

fn assert_sane(out: &GreeksOut, is_call: bool, rate: f64, t_years: f64, scale: f64) {
    assert!(out.iv.is_finite(), "iv must be finite, got {}", out.iv);
    assert!(
        (IV_CLAMP_MIN..=IV_CLAMP_MAX).contains(&out.iv),
        "iv {} escaped the clamp [{IV_CLAMP_MIN}, {IV_CLAMP_MAX}]",
        out.iv
    );
    // Black-76 delta is df·N(d1) with df = e^{-rT}: the true envelope is
    // [0, df] (CE) / [-df, 0] (PE). NOTE: `GreeksOut`'s doc comment claims
    // CE ∈ [0, 1], which only holds for rate ≥ 0 — this fuzzer FOUND that
    // overclaim (a negative rate makes df > 1); asserted here against the
    // real mathematical bound, doc reconcile reported upstream.
    let df_bound = (-rate * t_years).exp() * (1.0 + 1e-12);
    if is_call {
        assert!(
            (0.0..=df_bound).contains(&out.delta),
            "CE delta {} must lie in [0, e^(-rT) = {df_bound}]",
            out.delta
        );
    } else {
        assert!(
            (-df_bound..=0.0).contains(&out.delta),
            "PE delta {} must lie in [-e^(-rT) = {df_bound}, 0]",
            out.delta
        );
    }
    assert!(out.gamma >= 0.0, "gamma {} must be >= 0", out.gamma);
    assert!(out.vega >= 0.0, "vega {} must be >= 0", out.vega);
    assert!(out.theta.is_finite(), "theta must be finite, got {}", out.theta);
    assert!(out.rho.is_finite(), "rho must be finite, got {}", out.rho);
    // Analytically price ≥ 0, but the deep-OTM subtraction
    // df·(F·N(d1) − K·N(d2)) can round to a TINY negative (this fuzzer
    // observed −1.2e-321; the kernel does not clamp it — reported upstream).
    // Tolerate cancellation-scale negatives only; a real negative-premium bug
    // would be on the order of the price scale and still fails here.
    assert!(
        out.price.is_finite() && out.price >= -scale * 1e-12,
        "model price {} must be finite and (up to rounding) non-negative",
        out.price
    );
}

fuzz_target!(|data: &[u8]| {
    if data.is_empty() {
        return;
    }
    let flags = data[0];
    let is_call = flags & 1 != 0;

    // ── Phase 1: raw bit patterns — the "never panics" contract ──
    let price_arg = if flags & 2 != 0 {
        Some(raw_f64(data, 1))
    } else {
        None
    };
    let sigma_arg = if flags & 4 != 0 {
        Some(raw_f64(data, 9))
    } else {
        None
    };
    match greeks(
        price_arg,
        sigma_arg,
        raw_f64(data, 17),
        raw_f64(data, 25),
        raw_f64(data, 33),
        raw_f64(data, 41),
        is_call,
    ) {
        Ok(_) => {} // raw bits CAN land in-domain; sanity is phase 2's job
        Err(msg) => assert_documented_err(&msg),
    }

    // ── Phase 2: in-domain inputs — a returned tuple must be sane ──
    let forward = in_range(read_u64(data, 17), 1e-2, 1e9);
    let strike = in_range(read_u64(data, 25), 1e-2, 1e9);
    let t_years = in_range(read_u64(data, 33), T_FLOOR_YEARS, 10.0);
    let rate = in_range(read_u64(data, 41), -0.5, 0.5);

    if flags & 8 != 0 {
        // Known-sigma path.
        let sigma = in_range(read_u64(data, 9), IV_CLAMP_MIN, IV_CLAMP_MAX);
        match greeks(None, Some(sigma), forward, strike, t_years, rate, is_call) {
            Ok(out) => {
                assert_sane(&out, is_call, rate, t_years, forward.max(strike));
                assert_eq!(out.iv, sigma, "a supplied sigma must be echoed as iv");
            }
            Err(msg) => assert_documented_err(&msg),
        }
    } else {
        // IV-solve path: a positive traded price (may or may not admit an IV —
        // below-intrinsic / out-of-clamp are HONEST no_solution errors).
        let traded = in_range(read_u64(data, 1), 1e-6, 2e9);
        match greeks(Some(traded), None, forward, strike, t_years, rate, is_call) {
            Ok(out) => {
                assert_sane(&out, is_call, rate, t_years, forward.max(strike));
                // The verified solve must reproduce the traded price. The
                // kernel verifies in UNDISCOUNTED space at 1e-9 relative; a
                // loose 1e-6 band here avoids re-deriving the discounting.
                let rel = ((out.price - traded) / traded).abs();
                assert!(
                    rel <= 1e-6,
                    "solve path did not reproduce the traded price: \
                     traded {traded:e}, model {:e}, rel {rel:e}",
                    out.price
                );
            }
            Err(msg) => assert_documented_err(&msg),
        }
    }
});
