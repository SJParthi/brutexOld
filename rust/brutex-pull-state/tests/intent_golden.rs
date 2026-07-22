//! Cross-language golden pin: the committed pull-intent kernel fixture
//! (`tests/golden/fixtures/pull_intent_kernel_cases.tsv`, captured from the
//! PRE-conversion Python by `tests/golden/generate_pull_intent_golden.py`)
//! is replayed through the Rust decision core — every classified shape row's
//! BINARY verdict (valid-intent vs corrupt/absent; the fine violation codes
//! are pinned by the crate's unit tests), every resume-decision arm, every
//! refused-start restore arm and every env-token answer must match
//! bit-for-bit.  The SAME fixture is asserted against the live Python by
//! `tests/golden/test_pull_intent_golden.py`, so the two implementations are
//! pinned to ONE frozen capture (dependency-free TSV + a hand-rolled hex
//! decode for the env raw values — the crate takes no serde/base64
//! dependency; Cargo.toml stays frozen).

use brutex_pull_state::intent::{
    SHAPE_VALID, refused_restore_decision, resume_decision, resume_enabled, shape_verdict,
};

const FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../tests/golden/fixtures/pull_intent_kernel_cases.tsv"
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

/// UTF-8-hex → String (`@none` handled by the caller; `@empty` = "").
fn decode_hex_utf8(field: &str, row: &str) -> String {
    if field == "@empty" {
        return String::new();
    }
    assert!(
        field.len().is_multiple_of(2),
        "odd hex length in fixture row {row:?}"
    );
    let mut bytes = Vec::with_capacity(field.len() / 2);
    let raw = field.as_bytes();
    for pair in raw.chunks_exact(2) {
        let hex = std::str::from_utf8(pair).expect("ascii hex");
        let byte = u8::from_str_radix(hex, 16)
            .unwrap_or_else(|e| panic!("bad hex {hex:?} in fixture row {row:?}: {e}"));
        bytes.push(byte);
    }
    String::from_utf8(bytes).unwrap_or_else(|e| panic!("non-UTF-8 env raw in row {row:?}: {e}"))
}

#[test]
fn kernel_matches_the_frozen_python_capture() {
    let body = std::fs::read_to_string(FIXTURE)
        .unwrap_or_else(|e| panic!("cannot read the committed kernel fixture {FIXTURE}: {e}"));
    let mut shape_rows = 0usize;
    let mut resume_rows = 0usize;
    let mut restore_rows = 0usize;
    let mut env_rows = 0usize;
    let mut saw_valid = false;
    let mut saw_invalid = false;
    for row in body.lines() {
        if row.starts_with('#') {
            continue;
        }
        let fields: Vec<&str> = row.split('\t').collect();
        match fields.first() {
            Some(&"shape") => {
                assert_eq!(fields.len(), 12, "malformed shape row {row:?}");
                let value: i64 = fields[9]
                    .parse()
                    .unwrap_or_else(|e| panic!("bad restarts value in {row:?}: {e}"));
                let verdict = shape_verdict(
                    parse_bool(fields[2], row),
                    parse_u8(fields[3], row),
                    parse_u8(fields[4], row),
                    parse_u8(fields[5], row),
                    parse_u8(fields[6], row),
                    parse_u8(fields[7], row),
                    parse_u8(fields[8], row),
                    value,
                    parse_u8(fields[10], row),
                );
                let expected_valid = parse_bool(fields[11], row);
                assert_eq!(
                    verdict == SHAPE_VALID,
                    expected_valid,
                    "shape-verdict drift on fixture row {row:?} (got code {verdict})"
                );
                saw_valid |= expected_valid;
                saw_invalid |= !expected_valid;
                shape_rows += 1;
            }
            Some(&"resume") => {
                assert_eq!(fields.len(), 7, "malformed resume row {row:?}");
                let restarts: u64 = fields[4]
                    .parse()
                    .unwrap_or_else(|e| panic!("bad restarts in {row:?}: {e}"));
                let cap: u64 = fields[5]
                    .parse()
                    .unwrap_or_else(|e| panic!("bad max_restarts in {row:?}: {e}"));
                let arm = resume_decision(
                    parse_bool(fields[2], row),
                    parse_bool(fields[3], row),
                    restarts,
                    cap,
                );
                assert_eq!(
                    arm,
                    parse_u8(fields[6], row),
                    "resume-arm drift on row {row:?}"
                );
                resume_rows += 1;
            }
            Some(&"restore") => {
                assert_eq!(fields.len(), 7, "malformed restore row {row:?}");
                let arm = refused_restore_decision(
                    parse_bool(fields[2], row),
                    parse_bool(fields[3], row),
                    parse_bool(fields[4], row),
                    parse_bool(fields[5], row),
                );
                assert_eq!(
                    arm,
                    parse_u8(fields[6], row),
                    "restore-arm drift on row {row:?}"
                );
                restore_rows += 1;
            }
            Some(&"env") => {
                assert_eq!(fields.len(), 4, "malformed env row {row:?}");
                let raw_owned;
                let raw = if fields[2] == "@none" {
                    None
                } else {
                    raw_owned = decode_hex_utf8(fields[2], row);
                    Some(raw_owned.as_str())
                };
                let enabled = resume_enabled(raw);
                assert_eq!(
                    enabled,
                    parse_bool(fields[3], row),
                    "env-law drift on row {row:?}"
                );
                env_rows += 1;
            }
            other => panic!("unknown fixture row kind {other:?} in {row:?}"),
        }
    }
    // Anti-vacuity: the corpus genuinely covers both shape verdicts and a
    // meaningful spread of every table (the Python golden test pins the
    // exact counts against the SAME committed file).
    assert!(
        saw_valid && saw_invalid,
        "shape corpus lost a verdict class"
    );
    assert!(shape_rows >= 40, "shape corpus shrank: {shape_rows}");
    assert!(resume_rows >= 50, "resume corpus shrank: {resume_rows}");
    assert_eq!(restore_rows, 16, "the restore lattice is 2^4 combos");
    assert!(env_rows >= 30, "env corpus shrank: {env_rows}");
}
