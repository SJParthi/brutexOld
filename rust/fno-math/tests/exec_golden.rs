//! The cross-language bit-identity proof for the exec integer cores
//! (DEC-EXEC-RUST-CORE-001, MIGRATION.md #1978 — the `vocab_golden_vectors`
//! discipline).
//!
//! Reads the SAME language-neutral oracle the Python golden test reads
//! (`tests/golden/fixtures/exec_costs_golden_vectors.json`, generated
//! from the Python charge-stack/fill/money bodies) and asserts this crate
//! reproduces every frozen value bit-for-bit with zero Python in the loop.
//! A single paisa of drift fails (the coordinator's money-parity law).
//!
//! Schema v2 = the DEC-FILL-WORST-CASE-ONLY-001 (#1825) reconcile:
//! bar-extreme worst-case fills, ADVERSE-CEILING levy rounding, and the
//! UNCONDITIONAL stale gate (no `reject_stale` column).
//!
//! Money values cross as STRINGS (a JSON double cannot carry > 2^53 —
//! the vocab mask-hex precedent); everything else is < 2^53 by generator
//! construction and crosses as plain JSON integers.

use std::fs;
use std::path::Path;

use serde::Deserialize;

use fno_math::exec::exec_futures::BarVerdictFutures;
use fno_math::exec::exec_options::BarVerdict;
use fno_math::exec::{
    apply_futures_fill, apply_slippage, futures_charge_stack, futures_materialize_core,
    option_charge_stack, options_materialize_core, round_to_paisa, round_to_rupee,
};

#[derive(Deserialize)]
struct MoneyCase {
    id: String,
    op: String,
    x: String,
    expected: String,
}

#[derive(Deserialize)]
struct FillCase {
    id: String,
    op: String,
    entry_high: i64,
    entry_low: i64,
    exit_high: i64,
    exit_low: i64,
    is_long: bool,
    expected: Vec<i64>,
}

#[derive(Deserialize)]
struct OptionStackCase {
    id: String,
    entry_high: i64,
    entry_low: i64,
    exit_high: i64,
    exit_low: i64,
    is_long: bool,
    lot_size: i64,
    n_lots: i64,
    brokerage_round_trip_paisa: i64,
    stt_rate: i64,
    etc_rate: i64,
    sebi_rate: i64,
    ipft_rate: i64,
    stamp_rate: i64,
    gst_rate: i64,
    expected: Vec<i64>,
}

#[derive(Deserialize)]
struct FuturesStackCase {
    id: String,
    entry_high: i64,
    entry_low: i64,
    exit_high: i64,
    exit_low: i64,
    is_long: bool,
    lot_size: i64,
    n_lots: i64,
    cost_free: bool,
    brokerage_flat_paisa: i64,
    brokerage_turnover_bps_x100: Option<i64>,
    stt_rate: i64,
    etc_rate: i64,
    sebi_rate: i64,
    ipft_rate: i64,
    stamp_rate: i64,
    gst_rate: i64,
    expected: Vec<i64>,
}

#[derive(Deserialize)]
struct OptionsMatCase {
    id: String,
    entry_open: i64,
    exit_open: i64,
    entry_high: i64,
    entry_low: i64,
    exit_high: i64,
    exit_low: i64,
    entry_volume: i64,
    exit_volume: i64,
    is_long: bool,
    lot_size: i64,
    n_lots: i64,
    brokerage_round_trip_paisa: i64,
    stt_rate: i64,
    etc_rate: i64,
    sebi_rate: i64,
    ipft_rate: i64,
    stamp_rate: i64,
    gst_rate: i64,
    expected_kind: String,
    expected: Option<Vec<i64>>,
}

#[derive(Deserialize)]
struct FuturesMatCase {
    id: String,
    entry_high: i64,
    entry_low: i64,
    exit_high: i64,
    exit_low: i64,
    entry_volume: i64,
    exit_volume: i64,
    path_highs: Vec<i64>,
    path_lows: Vec<i64>,
    is_long: bool,
    lot_size: i64,
    n_lots: i64,
    cost_free: bool,
    brokerage_flat_paisa: i64,
    brokerage_turnover_bps_x100: Option<i64>,
    stt_rate: i64,
    etc_rate: i64,
    sebi_rate: i64,
    ipft_rate: i64,
    stamp_rate: i64,
    gst_rate: i64,
    expected_kind: String,
    expected: Option<Vec<i64>>,
}

#[derive(Deserialize)]
struct Fixture {
    schema_version: u32,
    money: Vec<MoneyCase>,
    fills: Vec<FillCase>,
    option_stacks: Vec<OptionStackCase>,
    futures_stacks: Vec<FuturesStackCase>,
    options_materialize: Vec<OptionsMatCase>,
    futures_materialize: Vec<FuturesMatCase>,
}

fn load_fixture() -> Fixture {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/fixtures/exec_costs_golden_vectors.json");
    let text =
        fs::read_to_string(&path).unwrap_or_else(|e| panic!("cannot read {}: {e}", path.display()));
    let fixture: Fixture = serde_json::from_str(&text).expect("fixture must parse");
    assert_eq!(fixture.schema_version, 2, "unknown fixture schema_version");
    fixture
}

#[test]
fn money_vectors_bit_identical() {
    let fixture = load_fixture();
    assert!(!fixture.money.is_empty());
    for case in &fixture.money {
        let expected: i64 = case.expected.parse().expect("expected must parse as i64");
        match case.op.as_str() {
            "round_to_paisa" => {
                let x: i128 = case.x.parse().expect("x must parse as i128");
                let got = round_to_paisa(x)
                    .unwrap_or_else(|e| panic!("{}: round_to_paisa errored: {e}", case.id));
                assert_eq!(got, expected, "{}: round_to_paisa({x})", case.id);
            }
            "round_to_rupee" => {
                let p: i64 = case.x.parse().expect("x must parse as i64");
                let got = round_to_rupee(p)
                    .unwrap_or_else(|e| panic!("{}: round_to_rupee errored: {e}", case.id));
                assert_eq!(got, expected, "{}: round_to_rupee({p})", case.id);
            }
            other => panic!("{}: unknown money op {other:?}", case.id),
        }
    }
}

#[test]
fn fill_vectors_bit_identical() {
    let fixture = load_fixture();
    assert!(!fixture.fills.is_empty());
    for case in &fixture.fills {
        let got = match case.op.as_str() {
            "slippage" => apply_slippage(
                case.entry_high,
                case.entry_low,
                case.exit_high,
                case.exit_low,
                case.is_long,
            ),
            "futures_fill" => apply_futures_fill(
                case.entry_high,
                case.entry_low,
                case.exit_high,
                case.exit_low,
                case.is_long,
            ),
            other => panic!("{}: unknown fill op {other:?}", case.id),
        }
        .unwrap_or_else(|e| panic!("{}: fill errored: {e}", case.id));
        assert_eq!(
            vec![got.0, got.1, got.2],
            case.expected,
            "{}: {}(entry {}/{}, exit {}/{}, is_long={})",
            case.id,
            case.op,
            case.entry_high,
            case.entry_low,
            case.exit_high,
            case.exit_low,
            case.is_long
        );
    }
}

#[test]
fn option_stack_vectors_bit_identical() {
    let fixture = load_fixture();
    assert!(!fixture.option_stacks.is_empty());
    for case in &fixture.option_stacks {
        let stack = option_charge_stack(
            case.entry_high,
            case.entry_low,
            case.exit_high,
            case.exit_low,
            case.is_long,
            case.lot_size,
            case.n_lots,
            case.brokerage_round_trip_paisa,
            case.stt_rate,
            case.etc_rate,
            case.sebi_rate,
            case.ipft_rate,
            case.stamp_rate,
            case.gst_rate,
        )
        .unwrap_or_else(|e| panic!("{}: option_charge_stack errored: {e}", case.id));
        assert_eq!(stack.to_wire().to_vec(), case.expected, "{}", case.id);
    }
}

#[test]
fn futures_stack_vectors_bit_identical() {
    let fixture = load_fixture();
    assert!(!fixture.futures_stacks.is_empty());
    for case in &fixture.futures_stacks {
        let stack = futures_charge_stack(
            case.entry_high,
            case.entry_low,
            case.exit_high,
            case.exit_low,
            case.is_long,
            case.lot_size,
            case.n_lots,
            case.cost_free,
            case.brokerage_flat_paisa,
            case.brokerage_turnover_bps_x100,
            case.stt_rate,
            case.etc_rate,
            case.sebi_rate,
            case.ipft_rate,
            case.stamp_rate,
            case.gst_rate,
        )
        .unwrap_or_else(|e| panic!("{}: futures_charge_stack errored: {e}", case.id));
        assert_eq!(stack.to_wire().to_vec(), case.expected, "{}", case.id);
    }
}

#[test]
fn options_materialize_vectors_bit_identical() {
    let fixture = load_fixture();
    assert!(!fixture.options_materialize.is_empty());
    for case in &fixture.options_materialize {
        let verdict = options_materialize_core(
            case.entry_open,
            case.exit_open,
            case.entry_high,
            case.entry_low,
            case.exit_high,
            case.exit_low,
            case.entry_volume,
            case.exit_volume,
            case.is_long,
            case.lot_size,
            case.n_lots,
            case.brokerage_round_trip_paisa,
            case.stt_rate,
            case.etc_rate,
            case.sebi_rate,
            case.ipft_rate,
            case.stamp_rate,
            case.gst_rate,
        )
        .unwrap_or_else(|e| panic!("{}: options_materialize_core errored: {e}", case.id));
        match verdict {
            BarVerdict::StaleBar => {
                assert_eq!(case.expected_kind, "stale_bar", "{}", case.id);
                assert!(case.expected.is_none(), "{}", case.id);
            }
            BarVerdict::TickFloor => {
                assert_eq!(case.expected_kind, "tick_floor", "{}", case.id);
                assert!(case.expected.is_none(), "{}", case.id);
            }
            BarVerdict::Trade(stack) => {
                assert_eq!(case.expected_kind, "trade", "{}", case.id);
                assert_eq!(Some(stack.to_wire().to_vec()), case.expected, "{}", case.id);
            }
        }
    }
}

#[test]
fn futures_materialize_vectors_bit_identical() {
    let fixture = load_fixture();
    assert!(!fixture.futures_materialize.is_empty());
    for case in &fixture.futures_materialize {
        let verdict = futures_materialize_core(
            case.entry_high,
            case.entry_low,
            case.exit_high,
            case.exit_low,
            case.entry_volume,
            case.exit_volume,
            &case.path_highs,
            &case.path_lows,
            case.is_long,
            case.lot_size,
            case.n_lots,
            case.cost_free,
            case.brokerage_flat_paisa,
            case.brokerage_turnover_bps_x100,
            case.stt_rate,
            case.etc_rate,
            case.sebi_rate,
            case.ipft_rate,
            case.stamp_rate,
            case.gst_rate,
        )
        .unwrap_or_else(|e| panic!("{}: futures_materialize_core errored: {e}", case.id));
        match verdict {
            BarVerdictFutures::StaleBar => {
                assert_eq!(case.expected_kind, "stale_bar", "{}", case.id);
                assert!(case.expected.is_none(), "{}", case.id);
            }
            BarVerdictFutures::Trade(outcome) => {
                assert_eq!(case.expected_kind, "trade", "{}", case.id);
                let mut wire = outcome.stack.to_wire().to_vec();
                wire.push(outcome.mae_paisa);
                wire.push(outcome.mfe_paisa);
                assert_eq!(Some(wire), case.expected, "{}", case.id);
            }
        }
    }
}
