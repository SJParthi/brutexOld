//! Cross-language golden pin: the committed auto-pull-scheduler fixture
//! (`tests/golden/fixtures/auto_pull_scheduler_verdicts.tsv`, captured from
//! the PRE-conversion Python by
//! `tests/golden/generate_auto_pull_scheduler_golden.py`) is replayed
//! through the Rust decision cores — every tick-lattice row's ladder token
//! and every classified state row's verdict must match bit-for-bit.  The
//! SAME fixture is asserted against the live Python by
//! `tests/golden/test_auto_pull_scheduler_golden.py`, so the two
//! implementations are pinned to ONE frozen capture (dependency-free TSV —
//! the crate takes no serde dependency; Cargo.toml stays frozen).

use brutex_pull_state::auto_pull_scheduler::{auto_state_verdict, auto_tick_decision};

const FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../tests/golden/fixtures/auto_pull_scheduler_verdicts.tsv"
);

fn parse_bool(field: &str, row: &str) -> bool {
    match field {
        "1" => true,
        "0" => false,
        other => panic!("bad boolean field {other:?} in fixture row {row:?}"),
    }
}

fn parse_u8(field: &str, row: &str) -> u8 {
    field
        .parse()
        .unwrap_or_else(|e| panic!("bad u8 field {field:?} in fixture row {row:?}: {e}"))
}

fn parse_i64(field: &str, row: &str) -> i64 {
    field
        .parse()
        .unwrap_or_else(|e| panic!("bad i64 field {field:?} in fixture row {row:?}: {e}"))
}

fn parse_bits(field: &str, row: &str) -> u64 {
    u64::from_str_radix(field, 16)
        .unwrap_or_else(|e| panic!("bad f64-bits field {field:?} in fixture row {row:?}: {e}"))
}

#[test]
fn kernel_matches_the_frozen_python_capture() {
    let body = std::fs::read_to_string(FIXTURE)
        .unwrap_or_else(|e| panic!("cannot read the committed kernel fixture {FIXTURE}: {e}"));
    let mut tick_rows = 0_u32;
    let mut state_rows = 0_u32;
    for row in body.lines() {
        if row.is_empty() || row.starts_with('#') {
            continue;
        }
        let fields: Vec<&str> = row.split('\t').collect();
        match fields[0] {
            "tick" => {
                assert_eq!(fields.len(), 8, "tick row arity in {row:?}");
                let got = auto_tick_decision(
                    parse_bool(fields[2], row),
                    parse_bool(fields[3], row),
                    parse_bool(fields[4], row),
                    parse_bool(fields[5], row),
                    parse_bool(fields[6], row),
                );
                assert_eq!(got, parse_u8(fields[7], row), "tick row {row:?}");
                tick_rows += 1;
            }
            "state" => {
                assert_eq!(fields.len(), 11, "state row arity in {row:?}");
                let got = auto_state_verdict(
                    parse_bool(fields[2], row),
                    parse_u8(fields[3], row),
                    parse_i64(fields[4], row),
                    parse_bits(fields[5], row),
                    parse_bool(fields[6], row),
                    parse_u8(fields[7], row),
                    parse_u8(fields[8], row),
                    parse_u8(fields[9], row),
                );
                assert_eq!(got, parse_u8(fields[10], row), "state row {row:?}");
                state_rows += 1;
            }
            other => panic!("unknown fixture table {other:?} in row {row:?}"),
        }
    }
    // Anti-vacuous: the exhaustive 2**5 lattice (+ the outcome-split and
    // none-dates extras) and a real state corpus are genuinely replayed.
    assert!(tick_rows >= 34, "tick rows: {tick_rows}");
    assert!(state_rows >= 26, "state rows: {state_rows}");
}
