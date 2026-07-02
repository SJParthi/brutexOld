//! Greeks golden proof (`DEC-GREEKS-ENGINE-PR2-001`; plan §10/§12, ADR-0015 D6).
//!
//! Reads the SAME language-neutral fixture the Python parity test reads
//! (`tests/golden/fixtures/greeks_golden.json` — the Greeks twin of
//! `vocab_golden_vectors.json`) and asserts, per case and per solve/sigma
//! path:
//!
//! 1. **Frozen bit-identity within a MEASURED ≤ 4-ULP link-product envelope**
//!    — the fixture's bits are frozen from the maturin WHEEL (the artifact
//!    BruteX production consumes; the Python parity test asserts them
//!    EXACTLY). This `cargo test` binary is a DIFFERENT link product of the
//!    same source (static rlib vs PIC cdylib), and a measured investigation
//!    (2026-07-02) found the two products differ ONLY on the SOLVE path (the
//!    LBR inversion; the sigma path is bit-identical on every case) by AT
//!    MOST 2 ULP across all 24×2 case-paths (a 1-ULP solved-iv difference —
//!    compiled-in codegen, runtime libm imports `exp@GLIBC_2.29` identical —
//!    propagating to ≤ 2 ULP in the derived Greeks). So THIS side asserts
//!    every value within ≤ 4 ULP of the frozen bits (2× margin over the
//!    measured 2) — the plan §13 open-decision-#6 "documented tolerance"
//!    fallback, stated honestly: strict bit-identity holds PER ARTIFACT;
//!    across link products the measured envelope is 2 ULP. Any real numeric
//!    regression (a formula or solver change) moves values by orders of
//!    magnitude more than 4 ULP and fails BOTH layers.
//! 2. **Independent-oracle tolerance** — the kernel agrees with the fixture's
//!    mpmath 50-digit reference (an arithmetic path fully independent of this
//!    crate) within the documented `|got − want| ≤ abs + rel·|want|` gate —
//!    the cross-platform contract (plan §13 open decision #6).

use std::fs;
use std::path::Path;

use serde::Deserialize;
use trading_core::greeks;

#[derive(Deserialize)]
struct Tolerance {
    rel: f64,
    abs: f64,
}

#[derive(Deserialize)]
struct Meta {
    tolerance: Tolerance,
}

#[derive(Deserialize)]
struct Reference {
    iv: f64,
    delta: f64,
    gamma: f64,
    theta: f64,
    vega: f64,
    rho: f64,
    price: f64,
    admissible: bool,
}

#[derive(Deserialize)]
struct FrozenBits {
    iv: String,
    delta: String,
    gamma: String,
    theta: String,
    vega: String,
    rho: String,
    price: String,
    admissible: bool,
}

#[derive(Deserialize)]
struct Case {
    id: String,
    forward: f64,
    strike: f64,
    t_years: f64,
    rate: f64,
    is_call: bool,
    sigma_true: f64,
    price: f64,
    reference_solve: Reference,
    reference_sigma: Reference,
    frozen_solve_bits: FrozenBits,
    frozen_sigma_bits: FrozenBits,
}

#[derive(Deserialize)]
struct Fixture {
    #[serde(rename = "_meta")]
    meta: Meta,
    cases: Vec<Case>,
}

fn bits(hex: &str) -> u64 {
    u64::from_str_radix(hex, 16).expect("frozen bit pattern must be 16 hex chars")
}

fn assert_case(
    case_id: &str,
    path: &str,
    got: trading_core::GreeksOut,
    reference: &Reference,
    frozen: &FrozenBits,
    tolerance: &Tolerance,
) {
    // Layer 1 — frozen bit-identity within the measured ≤ 4-ULP envelope
    // (module docs; the Python side asserts the wheel's bits EXACTLY).
    let pairs: [(&str, f64, &String); 7] = [
        ("iv", got.iv, &frozen.iv),
        ("delta", got.delta, &frozen.delta),
        ("gamma", got.gamma, &frozen.gamma),
        ("theta", got.theta, &frozen.theta),
        ("vega", got.vega, &frozen.vega),
        ("rho", got.rho, &frozen.rho),
        ("price", got.price, &frozen.price),
    ];
    for (name, value, frozen_hex) in pairs {
        let frozen_bits = bits(frozen_hex);
        let got_bits = value.to_bits();
        let ulp_distance = got_bits.abs_diff(frozen_bits);
        assert!(
            ulp_distance <= 4,
            "{case_id}/{path}/{name}: bit drift — got {value:?} ({got_bits:016x}) vs frozen {frozen_hex} ({ulp_distance} ULP)"
        );
    }
    assert_eq!(
        got.admissible, frozen.admissible,
        "{case_id}/{path}: admissible bit drift"
    );

    // Layer 2 — independent mpmath oracle within the documented tolerance.
    let oracle: [(&str, f64, f64); 7] = [
        ("iv", got.iv, reference.iv),
        ("delta", got.delta, reference.delta),
        ("gamma", got.gamma, reference.gamma),
        ("theta", got.theta, reference.theta),
        ("vega", got.vega, reference.vega),
        ("rho", got.rho, reference.rho),
        ("price", got.price, reference.price),
    ];
    for (name, value, want) in oracle {
        let allowed = tolerance.abs + tolerance.rel * want.abs();
        assert!(
            (value - want).abs() <= allowed,
            "{case_id}/{path}/{name}: oracle drift — got {value:?} vs mpmath {want:?} (allowed {allowed:e})"
        );
    }
    assert_eq!(
        got.admissible, reference.admissible,
        "{case_id}/{path}: admissible mismatch"
    );
}

#[test]
fn greeks_reproduce_every_golden_case_bit_for_bit_and_within_oracle_tolerance() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/fixtures/greeks_golden.json");
    let raw = fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
    let fixture: Fixture = serde_json::from_str(&raw).expect("greeks_golden.json must parse");
    assert!(
        fixture.cases.len() >= 20,
        "the golden must stay a real matrix"
    );

    for case in &fixture.cases {
        // Historical path: solve IV from the traded premium (sigma = None).
        let solved = greeks(
            Some(case.price),
            None,
            case.forward,
            case.strike,
            case.t_years,
            case.rate,
            case.is_call,
        )
        .unwrap_or_else(|e| panic!("{}: solve path must succeed: {e}", case.id));
        assert_case(
            &case.id,
            "solve",
            solved,
            &case.reference_solve,
            &case.frozen_solve_bits,
            &fixture.meta.tolerance,
        );

        // Live path: price directly off the supplied sigma (price ignored).
        let priced = greeks(
            None,
            Some(case.sigma_true),
            case.forward,
            case.strike,
            case.t_years,
            case.rate,
            case.is_call,
        )
        .unwrap_or_else(|e| panic!("{}: sigma path must succeed: {e}", case.id));
        assert_case(
            &case.id,
            "sigma",
            priced,
            &case.reference_sigma,
            &case.frozen_sigma_bits,
            &fixture.meta.tolerance,
        );
    }
}
