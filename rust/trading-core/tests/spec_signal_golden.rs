//! The DEPLOY-CONTRACT golden: `evaluate(spec)` is bit-consistent with the FROZEN
//! `evaluate_atoms` kernel + the 400-case answer-key (DEC-STRATEGYSPEC-CORE-001).
//!
//! For EVERY one of the 400 `vocab_golden_vectors.json` cases we build a
//! [`StrategySpec`] whose `condition_mask` IS that case's frozen `expected_mask`
//! and prove:
//!
//! 1. `evaluate(spec, snapshot, prior).bar_bits == expected` — the wrapper echoes
//!    the kernel verbatim (it never perturbs the arithmetic).
//! 2. `signal.fires == true` — a spec requiring EXACTLY the bits the bar produces
//!    always fires (the superset test `(bar & mask) == mask` is self-consistent
//!    with the frozen golden BY CONSTRUCTION; there is no new hand-blessed truth).
//!
//! Plus a NEGATIVE: for every case with a clear IMPLEMENTED bit, a spec requiring
//! that absent bit does NOT fire — so "fires" is a real test, not a tautology.
//!
//! This is the backtest == live proof: TickVault's native `evaluate` and the
//! frozen oracle agree on the same `(spec, snapshot, prior)`. It EXTENDS — never
//! replaces — `golden_vectors.rs`, which keeps proving the kernel itself is
//! frozen.

use std::fs;
use std::path::Path;

use serde::Deserialize;
use trading_core::bits::IMPLEMENTED_MASK;
use trading_core::{
    Direction, IndicatorSnapshot, MarketStructureSignal, PriorBarHistory, ProximityParams, Signal,
    StrategySpec, compute_fib_levels, compute_pivot_levels, evaluate,
};

fn parse_mask_hex(s: &str) -> u128 {
    u128::from_str_radix(s, 16).expect("expected_mask must be valid hex")
}

#[derive(Deserialize)]
struct Proximity {
    pivot_bps: i64,
    pdh_pdl_bps: i64,
    fib_bps: i64,
    /// Group-15 gap-midpoint tolerance (DEC-GAP-MIDPOINT-CONDITIONS-001);
    /// serde-defaulted (10 = the evaluator default) for pre-gap parses.
    #[serde(default = "default_gap_mid_bps")]
    gap_mid_bps: i64,
}

fn default_gap_mid_bps() -> i64 {
    10
}

#[derive(Deserialize)]
struct Prior {
    prior_n: usize,
    flags: Vec<bool>,
}

#[derive(Deserialize)]
struct MarketStructure {
    bos_bullish: bool,
    bos_bearish: bool,
    choch_bullish: bool,
    choch_bearish: bool,
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
    market_structure: MarketStructure,
    /// The SuperTrend line in the ×2-scaled paisa domain (fixture version 4 —
    /// the vocab-66 implemented-flip; `None` = warm-up, bits 64/65 clear).
    supertrend_x2_paisa: Option<i64>,
    /// Group-15 gap-midpoint level, carried VERBATIM
    /// (DEC-GAP-MIDPOINT-CONDITIONS-001); serde-defaulted for pre-gap parses.
    #[serde(default)]
    gap_mid_paisa: Option<i64>,
    expected_mask: String,
}

#[derive(Deserialize)]
struct Fixture {
    cases: Vec<Case>,
}

/// IST wall-clock seconds-of-day (tz offset stripped — mirrors the kernel golden).
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

/// Rebuild the case's `(snapshot, proximity, prior)` exactly as `golden_vectors.rs`
/// does — the ONE rebuild logic, so the deploy golden reads the SAME inputs.
fn rebuild(c: &Case) -> (IndicatorSnapshot, ProximityParams, PriorBarHistory) {
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
        market_structure: MarketStructureSignal {
            bos_bullish: c.market_structure.bos_bullish,
            bos_bearish: c.market_structure.bos_bearish,
            choch_bullish: c.market_structure.choch_bullish,
            choch_bearish: c.market_structure.choch_bearish,
        },
        // Fixture version 4 (the vocab-66 SuperTrend implemented-flip)
        // carries the appended ST input per case — threaded verbatim.
        supertrend_x2_paisa: c.supertrend_x2_paisa,
        gap_mid_paisa: c.gap_mid_paisa,
    };
    let proximity = ProximityParams {
        pivot_proximity_bps: c.proximity.pivot_bps,
        pdh_pdl_proximity_bps: c.proximity.pdh_pdl_bps,
        fib_proximity_bps: c.proximity.fib_bps,
        gap_mid_proximity_bps: c.proximity.gap_mid_bps,
    };
    let prior = PriorBarHistory {
        prior_n: c.prior.prior_n,
        flags: c.prior.flags.clone(),
    };
    (snapshot, proximity, prior)
}

#[test]
fn spec_requiring_its_own_bits_fires_and_reproduces_the_oracle() {
    let fixture = load_fixture();
    assert_eq!(fixture.cases.len(), 400, "deploy golden expects 400 cases");
    let mut failures: Vec<String> = Vec::new();
    for c in &fixture.cases {
        let (snapshot, proximity, prior) = rebuild(c);
        let expected = parse_mask_hex(&c.expected_mask);
        // The spec's condition_mask IS the case's frozen expected mask. Every set
        // bit is IMPLEMENTED (the kernel never sets a reserved bit, proven by the
        // sibling golden), so construction must accept it.
        let spec = StrategySpec::new(expected, Direction::Long, proximity, prior.prior_n, 1)
            .unwrap_or_else(|e| panic!("case {}: spec construction rejected: {e}", c.id));
        let sig: Signal = evaluate(&spec, &snapshot, &prior);
        if sig.bar_bits != expected {
            failures.push(format!(
                "case {}: bar_bits {:#034x} != expected {expected:#034x}",
                c.id, sig.bar_bits
            ));
        }
        if !sig.fires {
            failures.push(format!(
                "case {}: a spec requiring exactly the bar's own bits did NOT fire",
                c.id
            ));
        }
        if sig.direction != Direction::Long {
            failures.push(format!("case {}: direction not echoed", c.id));
        }
    }
    assert!(
        failures.is_empty(),
        "deploy-contract golden failed ({} / {} cases):\n{}",
        failures.len(),
        fixture.cases.len(),
        failures
            .iter()
            .take(20)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
}

#[test]
fn spec_requiring_an_absent_bit_does_not_fire() {
    let fixture = load_fixture();
    let mut checked = 0usize;
    for c in &fixture.cases {
        let (snapshot, proximity, prior) = rebuild(c);
        let expected = parse_mask_hex(&c.expected_mask);
        // The IMPLEMENTED bits this bar does NOT have.
        let absent = IMPLEMENTED_MASK & !expected;
        if absent == 0 {
            // A (rare) case that sets every implemented bit — no absent bit to test.
            continue;
        }
        let want = absent & absent.wrapping_neg(); // lowest absent implemented bit
        let spec = StrategySpec::new(want, Direction::Short, proximity, prior.prior_n, 1)
            .expect("a single implemented bit is a valid mask");
        let sig = evaluate(&spec, &snapshot, &prior);
        assert_eq!(sig.bar_bits, expected, "case {}: kernel echo", c.id);
        assert!(
            !sig.fires,
            "case {}: spec requiring absent bit {want:#034x} wrongly fired (bar={:#034x})",
            c.id, sig.bar_bits
        );
        checked += 1;
    }
    assert!(
        checked > 0,
        "the negative test exercised no case — fixture has no bar with a clear implemented bit"
    );
}
