//! Cross-language golden replay + properties for the batch Stage-1A kernel.
//!
//! DEC-STAGE1A-RUST-BATCH-001 §6.4 — the twin-state fixture discipline: the
//! committed `tests/fixtures/stage1a_batch_golden.json` was generated ONCE by
//! the Python oracle (`tests/golden/generate_stage1a_batch_golden.py`, run
//! against `Stage1AMatrixBuilder.build`) over a 6-day / 2-roll futures feed,
//! and is replayed here RUST-ONLY: `stage1a_bar_bits_batch_impl` must
//! reproduce every frozen u128 mask bit-for-bit. Plus the in-crate prefix
//! property (suffix independence natively) and a bounded-envelope proptest
//! totality gate (no panic over fuzzed VALID-envelope inputs — the wrapper's
//! value guards own the out-of-envelope rejections).

use std::fs;
use std::path::Path;

use proptest::prelude::*;
use serde::Deserialize;
use trading_core::stage1a_batch::{
    FIB_ROW_LEN, PIVOT_ROW_LEN, Stage1aBatchInputs, stage1a_bar_bits_batch_impl,
};

#[derive(Deserialize)]
struct Columns {
    open_paisa: Vec<i64>,
    high_paisa: Vec<i64>,
    low_paisa: Vec<i64>,
    close_paisa: Vec<i64>,
    tod_seconds: Vec<u32>,
    contract_reset: Vec<bool>,
    day_index: Vec<u32>,
    ema20_paisa: Vec<Option<i64>>,
    ema200_paisa: Vec<Option<i64>>,
}

#[derive(Deserialize)]
struct DayTables {
    day_pivot_levels: Vec<i64>,
    day_fib_levels: Vec<i64>,
    day_has_pivot: Vec<bool>,
    day_has_fib: Vec<bool>,
}

#[derive(Deserialize)]
struct Config {
    pivot_bps: i64,
    pdh_pdl_bps: i64,
    fib_bps: i64,
    prior_n: usize,
    fractal_k: usize,
    apply_1day_mask: bool,
    anchor_dependent_mask_hex: String,
    fib_dependent_mask_hex: String,
    applies_to_1day_mask_hex: String,
}

#[derive(Deserialize)]
struct Fixture {
    schema: String,
    columns: Columns,
    day_tables: DayTables,
    config: Config,
    expected_masks_hex: Vec<String>,
}

fn parse_mask_hex(s: &str) -> u128 {
    u128::from_str_radix(s, 16).expect("fixture mask must be valid hex")
}

fn load_fixture() -> Fixture {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("stage1a_batch_golden.json");
    let text = fs::read_to_string(&path).expect("fixture readable");
    let fixture: Fixture = serde_json::from_str(&text).expect("fixture parses");
    assert_eq!(fixture.schema, "stage1a-batch-golden-v2");
    fixture
}

fn inputs<'a>(f: &'a Fixture) -> Stage1aBatchInputs<'a> {
    Stage1aBatchInputs {
        open_paisa: &f.columns.open_paisa,
        high_paisa: &f.columns.high_paisa,
        low_paisa: &f.columns.low_paisa,
        close_paisa: &f.columns.close_paisa,
        tod_seconds: &f.columns.tod_seconds,
        contract_reset: &f.columns.contract_reset,
        day_index: &f.columns.day_index,
        ema20_paisa: &f.columns.ema20_paisa,
        ema200_paisa: &f.columns.ema200_paisa,
        day_pivot_levels: &f.day_tables.day_pivot_levels,
        day_fib_levels: &f.day_tables.day_fib_levels,
        day_has_pivot: &f.day_tables.day_has_pivot,
        day_has_fib: &f.day_tables.day_has_fib,
        pivot_bps: f.config.pivot_bps,
        pdh_pdl_bps: f.config.pdh_pdl_bps,
        fib_bps: f.config.fib_bps,
        prior_n: f.config.prior_n,
        fractal_k: f.config.fractal_k,
        apply_1day_mask: f.config.apply_1day_mask,
        anchor_dependent_mask: parse_mask_hex(&f.config.anchor_dependent_mask_hex),
        fib_dependent_mask: parse_mask_hex(&f.config.fib_dependent_mask_hex),
        applies_to_1day_mask: parse_mask_hex(&f.config.applies_to_1day_mask_hex),
    }
}

#[test]
fn golden_replay_reproduces_every_python_oracle_mask() {
    let fixture = load_fixture();
    let expected: Vec<u128> = fixture
        .expected_masks_hex
        .iter()
        .map(|s| parse_mask_hex(s))
        .collect();
    let got = stage1a_bar_bits_batch_impl(&inputs(&fixture)).expect("golden inputs are valid");
    assert_eq!(got.len(), expected.len());
    for (i, (g, e)) in got.iter().zip(expected.iter()).enumerate() {
        assert_eq!(g, e, "mask {i} diverged: got {g:#x}, expected {e:#x}");
    }
}

#[test]
fn golden_fixture_exercises_the_reset_and_warm_anchor_arms() {
    // Corpus honesty: the frozen feed genuinely crosses 2 rolls and carries
    // both cold AND warm anchor days (a fixture that never exercised the
    // clear/reset arms would prove nothing about them).
    let fixture = load_fixture();
    let resets: usize = fixture
        .columns
        .contract_reset
        .iter()
        .filter(|&&r| r)
        .count();
    assert_eq!(resets, 2);
    assert!(fixture.day_tables.day_has_pivot.iter().any(|&w| w));
    assert!(fixture.day_tables.day_has_pivot.iter().any(|&w| !w));
    assert_eq!(fixture.day_tables.day_has_pivot.len(), 6);
    assert_eq!(fixture.day_tables.day_pivot_levels.len(), PIVOT_ROW_LEN * 6);
    assert_eq!(fixture.day_tables.day_fib_levels.len(), FIB_ROW_LEN * 6);
    // Refuter RF-B F1: the DIRECTION dimension must be non-degenerate — the
    // original corpus was 156/156 dojis (open == close on every bar), so the
    // Group-6 prior-flag deque was untested by the replay. Bull, bear AND
    // doji bars must all be present in the regenerated corpus.
    let bull = fixture
        .columns
        .open_paisa
        .iter()
        .zip(&fixture.columns.close_paisa)
        .filter(|(o, c)| c > o)
        .count();
    let bear = fixture
        .columns
        .open_paisa
        .iter()
        .zip(&fixture.columns.close_paisa)
        .filter(|(o, c)| c < o)
        .count();
    let doji = fixture.columns.open_paisa.len() - bull - bear;
    assert!(
        bull > 0 && bear > 0 && doji > 0,
        "degenerate direction corpus: bull={bull} bear={bear} doji={doji}"
    );
}

#[test]
fn golden_prefix_replay_is_suffix_independent() {
    // masks(prefix) == prefix of masks(full) for every prefix that ends on a
    // consumed-day boundary-consistent cut (day tables truncated to the
    // prefix's last day row — the G-2 terminal law).
    let fixture = load_fixture();
    let full = stage1a_bar_bits_batch_impl(&inputs(&fixture)).unwrap();
    for k in [1usize, 26, 27, 60, 155, 156] {
        let f = &fixture;
        let n_days = (f.columns.day_index[k - 1] as usize) + 1;
        let prefix_inputs = Stage1aBatchInputs {
            open_paisa: &f.columns.open_paisa[..k],
            high_paisa: &f.columns.high_paisa[..k],
            low_paisa: &f.columns.low_paisa[..k],
            close_paisa: &f.columns.close_paisa[..k],
            tod_seconds: &f.columns.tod_seconds[..k],
            contract_reset: &f.columns.contract_reset[..k],
            day_index: &f.columns.day_index[..k],
            ema20_paisa: &f.columns.ema20_paisa[..k],
            ema200_paisa: &f.columns.ema200_paisa[..k],
            day_pivot_levels: &f.day_tables.day_pivot_levels[..PIVOT_ROW_LEN * n_days],
            day_fib_levels: &f.day_tables.day_fib_levels[..FIB_ROW_LEN * n_days],
            day_has_pivot: &f.day_tables.day_has_pivot[..n_days],
            day_has_fib: &f.day_tables.day_has_fib[..n_days],
            ..inputs(f)
        };
        let prefix = stage1a_bar_bits_batch_impl(&prefix_inputs).unwrap();
        assert_eq!(prefix.as_slice(), &full[..k], "prefix k={k}");
    }
}

// ────────────────────────────────────────────────────────────────────────
// Proptest totality over the VALID envelope (overflow-checks discipline)
// ────────────────────────────────────────────────────────────────────────

/// The bounded finance-safe paisa domain (mirrors the crate's proptest
/// convention `MAX_PAISA = 10^12` — far inside the wrapper's
/// `MAX_BAR_PAISA` envelope, so the impl must be panic-free here).
const MAX_PAISA: i64 = 1_000_000_000_000;

/// `(bars_in_day, has_pivot, has_fib, pivot_row, fib_row)` — one generated day.
type DaySpec = (usize, bool, bool, [i64; PIVOT_ROW_LEN], [i64; FIB_ROW_LEN]);

/// `(open, a, b, close, volume, tod_seconds, pre_open, contract_reset)` — one
/// generated bar (`a`/`b` are unordered price extremes, sorted into high/low).
type BarSpec = (i64, i64, i64, i64, i64, u32, bool, bool);

#[derive(Debug, Clone)]
struct StreamSpec {
    days: Vec<DaySpec>,
    bars: Vec<BarSpec>,
    prior_n: usize,
    bps: (i64, i64, i64),
    apply_1day: bool,
}

fn stream_spec() -> impl Strategy<Value = StreamSpec> {
    let price = 1i64..MAX_PAISA;
    let level = 0i64..MAX_PAISA;
    let day = (
        1usize..4,     // bars in the day
        any::<bool>(), // has_pivot
        any::<bool>(), // has_fib
        proptest::array::uniform12(level.clone()),
        proptest::array::uniform15(level),
    );
    let bar = (
        price.clone(),   // open
        price.clone(),   // a
        price.clone(),   // b
        price.clone(),   // close
        0i64..1_000_000, // volume
        0u32..86_400,    // tod_seconds
        any::<bool>(),   // pre_open
        any::<bool>(),   // contract_reset (re-anchored below)
    );
    (
        proptest::collection::vec(day, 1..4),
        proptest::collection::vec(bar, 12),
        1usize..=4,
        (0i64..200, 0i64..200, 0i64..200),
        any::<bool>(),
    )
        .prop_map(|(days, bars, prior_n, bps, apply_1day)| StreamSpec {
            days,
            bars,
            prior_n,
            bps,
            apply_1day,
        })
}

proptest! {
    /// Totality: for ANY structurally-valid, envelope-bounded input the impl
    /// returns Ok (never panics — the `overflow-checks = true` discipline)
    /// and yields one mask per bar.
    #[test]
    fn impl_is_total_over_the_valid_envelope(spec in stream_spec()) {
        let mut open = Vec::new();
        let mut high = Vec::new();
        let mut low = Vec::new();
        let mut close = Vec::new();
        let mut tod = Vec::new();
        let mut contract_reset = Vec::new();
        let mut day_index = Vec::new();
        let mut day_pivots = Vec::new();
        let mut day_fibs = Vec::new();
        let mut day_has_pivot = Vec::new();
        let mut day_has_fib = Vec::new();
        let mut bar_cursor = 0usize;
        for (d, (n_bars, has_pivot, has_fib, pivot_row, fib_row)) in
            spec.days.iter().enumerate()
        {
            day_has_pivot.push(*has_pivot);
            day_has_fib.push(*has_fib);
            day_pivots.extend_from_slice(pivot_row);
            day_fibs.extend_from_slice(fib_row);
            for b in 0..*n_bars {
                let (o, a, bb, c, _v, t, _po, cr) =
                    spec.bars[(bar_cursor + b) % spec.bars.len()];
                let hi = o.max(a).max(bb).max(c);
                let lo = o.min(a).min(bb).min(c);
                open.push(o);
                high.push(hi);
                low.push(lo);
                close.push(c);
                tod.push(t);
                // A reset only makes sense on a day's FIRST bar (the prepass
                // law); elsewhere it is structurally absent.
                contract_reset.push(b == 0 && d > 0 && cr);
                day_index.push(d as u32);
            }
            bar_cursor += n_bars;
        }
        let inputs = Stage1aBatchInputs {
            open_paisa: &open,
            high_paisa: &high,
            low_paisa: &low,
            close_paisa: &close,
            tod_seconds: &tod,
            contract_reset: &contract_reset,
            day_index: &day_index,
            ema20_paisa: &vec![None; open.len()],
            ema200_paisa: &vec![None; open.len()],
            day_pivot_levels: &day_pivots,
            day_fib_levels: &day_fibs,
            day_has_pivot: &day_has_pivot,
            day_has_fib: &day_has_fib,
            pivot_bps: spec.bps.0,
            pdh_pdl_bps: spec.bps.1,
            fib_bps: spec.bps.2,
            prior_n: spec.prior_n,
            fractal_k: 2,
            apply_1day_mask: spec.apply_1day,
            anchor_dependent_mask: (1u128 << 60) - 1,
            fib_dependent_mask: 0x3FF8_0000,
            applies_to_1day_mask: u128::MAX,
        };
        let got = stage1a_bar_bits_batch_impl(&inputs).expect("valid envelope is total");
        prop_assert_eq!(got.len(), open.len());
    }
}
