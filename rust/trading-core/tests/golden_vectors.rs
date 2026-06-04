//! The cross-language bit-identity proof (Phase 1 of DEC-SHARED-CORE-001).
//!
//! Reads the SAME language-neutral oracle the Python test reads
//! (`tests/golden/fixtures/vocab_golden_vectors.json`, 400 cases) and asserts
//! this Rust crate reproduces every frozen `u64` bit-for-bit. Both engines
//! reading the same vectors is what makes the backtest = live guarantee real.

use std::fs;
use std::path::Path;

use serde::Deserialize;
use trading_core::{
    IndicatorSnapshot, PriorBarHistory, ProximityParams, compute_fib_levels, compute_pivot_levels,
    evaluate_atoms,
};

const TOTAL_CONDITIONS: u32 = 52;

#[derive(Deserialize)]
struct Proximity {
    pivot_bps: i64,
    pdh_pdl_bps: i64,
    fib_bps: i64,
}

#[derive(Deserialize)]
struct Prior {
    prior_n: usize,
    flags: Vec<bool>,
}

#[derive(Deserialize)]
struct Case {
    id: i64,
    timestamp_ist: String,
    open_paisa: i64,
    high_paisa: i64,
    low_paisa: i64,
    close_paisa: i64,
    ema20_paisa: Option<i64>,
    ema200_paisa: Option<i64>,
    prev_pdh_paisa: i64,
    prev_pdl_paisa: i64,
    prev_pdc_paisa: i64,
    day_high_paisa: i64,
    day_low_paisa: i64,
    open_of_day_paisa: i64,
    proximity: Proximity,
    prior: Prior,
    expected_u64: u64,
}

#[derive(Deserialize)]
struct Fixture {
    cases: Vec<Case>,
}

/// IST wall-clock time-of-day in seconds. The Python evaluator reads only
/// `timestamp.time()` (local HH:MM:SS, tz stripped — never the UTC instant), so
/// we take the time portion before the `+`/`-`/`Z` offset and ignore the offset.
fn parse_tod_seconds(ts: &str) -> u32 {
    let time_part = ts
        .split('T')
        .nth(1)
        .expect("ISO timestamp must contain 'T'");
    let end = time_part.find(['+', '-', 'Z']).unwrap_or(time_part.len());
    let hms = &time_part[..end];
    let mut parts = hms.split(':');
    let h: u32 = parts.next().expect("hour").parse().expect("hour int");
    let m: u32 = parts.next().expect("minute").parse().expect("minute int");
    // Floor any fractional second (e.g. "00.5") — Group-8 cutoffs are whole-second,
    // so the sub-second part never changes a bucket (matches the evaluator's
    // `tod_seconds` flooring). Robust even though the fixtures are whole-minute.
    let s: u32 = parts.next().map_or(0, |x| {
        x.split('.')
            .next()
            .unwrap_or("0")
            .parse()
            .expect("second int")
    });
    h * 3600 + m * 60 + s
}

fn load_fixture() -> Fixture {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/fixtures/vocab_golden_vectors.json");
    let raw = fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("read fixture {}: {e}", path.display()));
    serde_json::from_str(&raw).expect("parse golden fixture JSON")
}

fn eval_case(c: &Case) -> u64 {
    let pivots = compute_pivot_levels(c.prev_pdh_paisa, c.prev_pdl_paisa, c.prev_pdc_paisa);
    let fib = compute_fib_levels(c.prev_pdh_paisa, c.prev_pdl_paisa);
    let snapshot = IndicatorSnapshot {
        tod_seconds: parse_tod_seconds(&c.timestamp_ist),
        open_paisa: c.open_paisa,
        high_paisa: c.high_paisa,
        low_paisa: c.low_paisa,
        close_paisa: c.close_paisa,
        ema20_paisa: c.ema20_paisa,
        ema200_paisa: c.ema200_paisa,
        pivots,
        fib,
        day_high_paisa: c.day_high_paisa,
        day_low_paisa: c.day_low_paisa,
        open_of_day_paisa: c.open_of_day_paisa,
    };
    let proximity = ProximityParams {
        pivot_proximity_bps: c.proximity.pivot_bps,
        pdh_pdl_proximity_bps: c.proximity.pdh_pdl_bps,
        fib_proximity_bps: c.proximity.fib_bps,
    };
    let prior = PriorBarHistory {
        prior_n: c.prior.prior_n,
        flags: c.prior.flags.clone(),
    };
    evaluate_atoms(&snapshot, &proximity, &prior)
}

#[test]
fn rust_reproduces_every_golden_vector() {
    let fixture = load_fixture();
    assert!(!fixture.cases.is_empty(), "fixture has no cases");
    let mut mismatches: Vec<String> = Vec::new();
    for c in &fixture.cases {
        let got = eval_case(c);
        if got != c.expected_u64 {
            mismatches.push(format!(
                "case {}: expected {:#015x}, got {:#015x}",
                c.id, c.expected_u64, got
            ));
        }
    }
    assert!(
        mismatches.is_empty(),
        "Rust vocab diverges from the Python oracle ({} / {} cases):\n{}",
        mismatches.len(),
        fixture.cases.len(),
        mismatches
            .iter()
            .take(20)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
}

#[test]
fn rust_evaluator_exercises_all_52_bits_both_ways() {
    let fixture = load_fixture();
    let full: u64 = (1u64 << TOTAL_CONDITIONS) - 1;
    let mut set_any: u64 = 0;
    let mut clear_any: u64 = 0;
    for c in &fixture.cases {
        let mask = eval_case(c);
        set_any |= mask;
        clear_any |= (!mask) & full;
    }
    assert_eq!(
        set_any, full,
        "a condition never fires across the Rust evaluation"
    );
    assert_eq!(
        clear_any, full,
        "a condition is never false across the Rust evaluation"
    );
}
