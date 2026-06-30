//! Cross-language EMA bit-identity proof (DEC-EMA-RUST-PORT-001).
//!
//! Reads the SAME language-neutral oracle the Python golden test reads
//! (`tests/golden/fixtures/indicators_golden.json`, the `ema` section) and
//! asserts this Rust crate's `compute_ema` reproduces every frozen EMA series
//! BIT-FOR-BIT (the warm-up `null` prefix included). Both engines reading the
//! same fixture is what makes "the COMMON indicator core is fully Rust" real:
//! BruteX's `compute_ema` now delegates to this same Rust function, so the
//! Python golden + this Rust golden are one and the same recurrence.

use std::fs;
use std::path::Path;

use serde::Deserialize;
use trading_core::compute_ema;

#[derive(Deserialize)]
struct EmaCase {
    id: String,
    values: Vec<f64>,
    period: i64,
    /// `null` (warm-up) becomes `None`; a value becomes `Some(f64)`.
    expected: Vec<Option<f64>>,
}

#[derive(Deserialize)]
struct Fixture {
    ema: Vec<EmaCase>,
}

#[test]
fn ema_reproduces_every_golden_vector_bit_for_bit() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/fixtures/indicators_golden.json");
    let raw = fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
    let fixture: Fixture = serde_json::from_str(&raw).expect("indicators_golden.json must parse");

    assert!(
        !fixture.ema.is_empty(),
        "the ema golden section must be non-empty"
    );

    for case in &fixture.ema {
        let got = compute_ema(&case.values, case.period)
            .unwrap_or_else(|e| panic!("compute_ema failed for case {}: {e}", case.id));
        // Bit-for-bit: `assert_eq!` on `Vec<Option<f64>>` compares the exact IEEE
        // bit patterns (a finite `f64` `==` is bit-exact; no `null` mismatches the
        // `None` warm-up entries).
        assert_eq!(
            got, case.expected,
            "EMA drift in golden case {}: {got:?} != {:?}",
            case.id, case.expected
        );
    }
}
