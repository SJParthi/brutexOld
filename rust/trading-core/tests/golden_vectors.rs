//! The cross-language bit-identity proof (Phase 1 of DEC-SHARED-CORE-001).
//!
//! Reads the SAME language-neutral oracle the Python test reads
//! (`tests/golden/fixtures/vocab_golden_vectors.json`, 400 seeded + 8 express cases) and asserts
//! this Rust crate reproduces every frozen `u64` bit-for-bit. Both engines
//! reading the same vectors is what makes the backtest = live guarantee real.

use std::fs;
use std::path::Path;

use serde::Deserialize;
use trading_core::bits::{IMPLEMENTED_MASK, TOTAL_CONDITIONS};
use trading_core::{
    IndicatorSnapshot, MarketStructureSignal, PriorBarHistory, ProximityParams, compute_fib_levels,
    compute_pivot_levels, evaluate_atoms,
};

/// Parse a 32-nibble hex mask STRING into a `u128`. The fixture serialises the
/// mask as a string (not a JSON number) so a bit >= 53 keeps full precision.
fn parse_mask_hex(s: &str) -> u128 {
    u128::from_str_radix(s, 16).expect("expected_mask must be valid hex")
}

#[derive(Deserialize)]
struct Proximity {
    pivot_bps: i64,
    pdh_pdl_bps: i64,
    fib_bps: i64,
    /// Group-15 gap-midpoint tolerance (DEC-GAP-MIDPOINT-CONDITIONS-001).
    /// serde-defaulted so a pre-gap fixture snapshot still parses (10 = the
    /// evaluator default).
    #[serde(default = "default_gap_mid_bps")]
    gap_mid_bps: i64,
}

fn default_gap_mid_bps() -> i64 {
    /// DEC-FIB-TEMPLATE-EXPRESS-001 — the swing band (bits 72/73). Defaulted
    /// so the parser stays tolerant of the pre-express fixture shape.
    #[serde(default = "default_swing_bps")]
    swing_bps: i64,
}

fn default_swing_bps() -> i64 {
    10
}

#[derive(Deserialize)]
struct Prior {
    prior_n: usize,
    flags: Vec<bool>,
}

/// The resolved BOS/CHoCH flags (Group 12, bits 56-59) carried in the fixture —
/// the engine resolves these from the look-ahead-clean `MarketStructureState`;
/// the language-neutral oracle carries them verbatim so both languages set the
/// same bits (DEC-NIFTY-BOS-CHOCH-001).
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
    /// Session-edge gap-midpoint level (Group 15, bits 66-68) — resolved
    /// upstream and carried VERBATIM like the VWAP
    /// (DEC-GAP-MIDPOINT-CONDITIONS-001). serde-defaulted (`None`) so a
    /// pre-gap fixture snapshot still parses.
    #[serde(default)]
    gap_mid_paisa: Option<i64>,
    /// DEC-FIB-TEMPLATE-EXPRESS-001 — the confirmed-swing anchors (bits
    /// 72/73). Defaulted (None = no confirmed swing) for pre-express shapes.
    #[serde(default)]
    last_swing_high_paisa: Option<i64>,
    #[serde(default)]
    last_swing_low_paisa: Option<i64>,
    expected_mask: String,
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

fn eval_case(c: &Case) -> u128 {
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
        last_swing_high_paisa: c.last_swing_high_paisa,
        last_swing_low_paisa: c.last_swing_low_paisa,
    };
    let proximity = ProximityParams {
        pivot_proximity_bps: c.proximity.pivot_bps,
        pdh_pdl_proximity_bps: c.proximity.pdh_pdl_bps,
        fib_proximity_bps: c.proximity.fib_bps,
        gap_mid_proximity_bps: c.proximity.gap_mid_bps,
        swing_proximity_bps: c.proximity.swing_bps,
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
        let expected = parse_mask_hex(&c.expected_mask);
        if got != expected {
            mismatches.push(format!(
                "case {}: expected {expected:#034x}, got {got:#034x}",
                c.id
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
fn rust_evaluator_exercises_all_implemented_bits_both_ways() {
    // The both-ways gate spans the FULL IMPLEMENTED_MASK — 0..=51 and
    // 54..=65 (R5-S5 since PR-2, BOS/CHoCH since DEC-NIFTY-BOS-CHOCH-001,
    // CPR 60-63 since the 66-bit reshape, SuperTrend 64-65 since the
    // vocab-66 implemented-flip's version-4 fixture regen — the interim
    // FIXTURE_COVERABLE scope-down is retired; the VWAP pair 52-53 stays
    // TOMBSTONED outside the mask).
    let fixture = load_fixture();
    let mut set_any: u128 = 0;
    let mut clear_any: u128 = 0;
    for c in &fixture.cases {
        let mask = eval_case(c);
        set_any |= mask;
        clear_any |= (!mask) & IMPLEMENTED_MASK;
    }
    assert_eq!(
        set_any, IMPLEMENTED_MASK,
        "an implemented condition never fires across the Rust evaluation"
    );
    assert_eq!(
        clear_any, IMPLEMENTED_MASK,
        "an implemented condition is never false across the Rust evaluation"
    );
}

#[test]
fn layout_only_bits_are_always_clear() {
    // Every bit OUTSIDE IMPLEMENTED_MASK — the tombstoned VWAP pair (52-53)
    // and the reserved u128 headroom (bits 66..127) — must be 0 in EVERY
    // evaluated case. (The Supertrend pair 64-65 left this set at the PR-B
    // implemented-flip.)
    let fixture = load_fixture();
    let full: u128 = (1u128 << TOTAL_CONDITIONS) - 1;
    let layout_only = full & !IMPLEMENTED_MASK;
    for c in &fixture.cases {
        let mask = eval_case(c);
        assert_eq!(
            mask & !IMPLEMENTED_MASK,
            0,
            "case {}: a layout-only/reserved bit fired (mask={mask:#034x}, layout_only={layout_only:#034x})",
            c.id
        );
    }
}
