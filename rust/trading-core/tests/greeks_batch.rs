//! Batch ≡ scalar equivalence proof (`DEC-GREEKS-RUST-BATCH-001`).
//!
//! Two layers, both under DEFAULT features (no libpython) so the per-PR
//! `cargo test --release` exercises them:
//!
//! 1. **Frozen-corpus parity** — the SAME language-neutral fixture the kernel
//!    golden reads (`tests/golden/fixtures/greeks_golden.json`, read-only) is
//!    routed COLUMNAR through [`greeks_batch_impl`] and elementwise through
//!    the scalar [`greeks`] entry; every element must agree bit-for-bit
//!    (`f64::to_bits` on all 7 floats + the bool + verbatim error strings).
//!    The fixture is never regenerated here — it is the input corpus, and
//!    the oracle is the scalar kernel itself (batch-vs-scalar, not
//!    batch-vs-fixture, so the cross-link-product ULP envelope documented in
//!    `greeks_golden.rs` cannot blur this test's EXACT equality).
//! 2. **Property** — proptest drives random valid/invalid columns (finite +
//!    non-finite + None mixes) through both faces and demands elementwise
//!    identity, killing loop-bound / index / tuple-order / error-arm mutants.

use std::fs;
use std::path::Path;

use proptest::prelude::*;
use serde::Deserialize;
use trading_core::greeks;
use trading_core::greeks::batch::{GreeksBatchElem, greeks_batch_impl};

#[derive(Deserialize)]
struct Case {
    forward: f64,
    strike: f64,
    t_years: f64,
    rate: f64,
    is_call: bool,
    sigma_true: f64,
    price: f64,
}

#[derive(Deserialize)]
struct Fixture {
    cases: Vec<Case>,
}

/// One row of scalar-entry arguments.
type Row = (Option<f64>, Option<f64>, f64, f64, f64, f64, bool);

fn run_batch(rows: &[Row]) -> Vec<GreeksBatchElem> {
    greeks_batch_impl(
        &rows.iter().map(|r| r.0).collect::<Vec<_>>(),
        &rows.iter().map(|r| r.1).collect::<Vec<_>>(),
        &rows.iter().map(|r| r.2).collect::<Vec<_>>(),
        &rows.iter().map(|r| r.3).collect::<Vec<_>>(),
        &rows.iter().map(|r| r.4).collect::<Vec<_>>(),
        &rows.iter().map(|r| r.5).collect::<Vec<_>>(),
        &rows.iter().map(|r| r.6).collect::<Vec<_>>(),
    )
    .expect("equal-length columns must never whole-call fail")
}

fn assert_rows_match_scalar(rows: &[Row]) {
    let out = run_batch(rows);
    assert_eq!(out.len(), rows.len(), "one element per input row");
    for (i, (row, elem)) in rows.iter().zip(&out).enumerate() {
        match greeks(row.0, row.1, row.2, row.3, row.4, row.5, row.6) {
            Ok(g) => {
                let got = elem
                    .0
                    .unwrap_or_else(|| panic!("row {i}: scalar Ok ⇒ values Some"));
                assert_eq!(elem.1, None, "row {i}: scalar Ok ⇒ error None");
                let want = [g.iv, g.delta, g.gamma, g.theta, g.vega, g.rho, g.price];
                let have = [got.0, got.1, got.2, got.3, got.4, got.5, got.6];
                for (name, (w, h)) in ["iv", "delta", "gamma", "theta", "vega", "rho", "price"]
                    .iter()
                    .zip(want.iter().zip(have.iter()))
                {
                    assert_eq!(
                        w.to_bits(),
                        h.to_bits(),
                        "row {i} {name}: bit drift — scalar {w:?} vs batch {h:?}"
                    );
                }
                assert_eq!(g.admissible, got.7, "row {i}: admissible drift");
            }
            Err(e) => {
                assert_eq!(elem.0, None, "row {i}: scalar Err ⇒ values None");
                assert_eq!(
                    elem.1.as_deref(),
                    Some(e.as_str()),
                    "row {i}: error string must cross VERBATIM"
                );
            }
        }
    }
}

/// The frozen golden corpus routed columnar: BOTH the solve path
/// (`price=Some, sigma=None`) and the sigma-direct path per case, in one
/// mixed batch — every element bit-identical to the scalar entry.
#[test]
fn frozen_golden_corpus_batch_equals_scalar() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/fixtures/greeks_golden.json");
    let raw = fs::read_to_string(&path).expect("greeks_golden.json must be readable");
    let fixture: Fixture = serde_json::from_str(&raw).expect("greeks_golden.json must parse");
    assert!(
        fixture.cases.len() >= 20,
        "corpus unexpectedly small — {} cases",
        fixture.cases.len()
    );
    let mut rows: Vec<Row> = Vec::new();
    for case in &fixture.cases {
        // Solve path (historical enrich shape).
        rows.push((
            Some(case.price),
            None,
            case.forward,
            case.strike,
            case.t_years,
            case.rate,
            case.is_call,
        ));
        // Sigma-direct path (live shape).
        rows.push((
            None,
            Some(case.sigma_true),
            case.forward,
            case.strike,
            case.t_years,
            case.rate,
            case.is_call,
        ));
    }
    assert_rows_match_scalar(&rows);
}

/// Any-junk scalar strategy: finite in-domain values, boundary values, and
/// hostile non-finite values all appear.
fn any_f64_hostile() -> impl Strategy<Value = f64> {
    prop_oneof![
        1.0f64..1e6,
        -1e6f64..1.0,
        Just(f64::NAN),
        Just(f64::INFINITY),
        Just(f64::NEG_INFINITY),
        Just(0.0f64),
        Just(-0.0f64),
        Just(f64::MIN_POSITIVE / 2.0), // subnormal — the check_positive_normal edge
    ]
}

fn any_row() -> impl Strategy<Value = Row> {
    (
        proptest::option::of(prop_oneof![1.0f64..1e5, any_f64_hostile()]),
        proptest::option::of(prop_oneof![0.001f64..5.0, any_f64_hostile()]),
        prop_oneof![100.0f64..1e6, any_f64_hostile()],
        prop_oneof![100.0f64..1e6, any_f64_hostile()],
        prop_oneof![1e-4f64..2.0, any_f64_hostile()],
        prop_oneof![-0.5f64..0.5, any_f64_hostile()],
        proptest::bool::ANY,
    )
}

proptest! {
    /// Elementwise batch ≡ scalar over arbitrary valid/invalid columns —
    /// values bit-for-bit, error strings verbatim, order preserved.
    #[test]
    fn batch_equals_scalar_elementwise(rows in proptest::collection::vec(any_row(), 0..32)) {
        assert_rows_match_scalar(&rows);
    }
}
