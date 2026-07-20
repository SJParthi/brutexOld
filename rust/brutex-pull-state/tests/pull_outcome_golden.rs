//! Cross-language golden pin: the committed kernel fixture
//! (`tests/golden/fixtures/pull_outcome_kernel_cases.tsv`, captured from the
//! PRE-conversion Python by `tests/golden/generate_pull_outcome_golden.py`)
//! is replayed through the Rust decision core — every classify row's
//! `(outcome, tone, headline_code, detail_code)` and every correlation row's
//! verdict must match bit-for-bit. The SAME fixture is asserted against the
//! live Python by `tests/golden/test_pull_outcome_golden.py`, so the two
//! implementations are pinned to ONE frozen capture (dependency-free TSV —
//! the crate takes no JSON dependency).

use brutex_pull_state::pull_outcome::{classify_pull_outcome, verdict_correlates};

const FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../tests/golden/fixtures/pull_outcome_kernel_cases.tsv"
);

fn parse_bool(field: &str, row: &str) -> bool {
    match field {
        "1" => true,
        "0" => false,
        other => panic!("bad boolean field {other:?} in fixture row {row:?}"),
    }
}

#[test]
fn kernel_matches_the_frozen_python_capture() {
    let body = std::fs::read_to_string(FIXTURE)
        .unwrap_or_else(|e| panic!("cannot read the committed kernel fixture {FIXTURE}: {e}"));
    let mut classify_rows = 0usize;
    let mut correlate_rows = 0usize;
    let mut saw_verdict_marker = false;
    let mut saw_prepull_marker = false;
    for row in body.lines() {
        let fields: Vec<&str> = row.split('\t').collect();
        match fields.first() {
            Some(&"classify") => {
                assert_eq!(fields.len(), 11, "malformed classify row {row:?}");
                let failed: i64 = fields[3]
                    .parse()
                    .unwrap_or_else(|e| panic!("bad failed count in {row:?}: {e}"));
                let got = classify_pull_outcome(
                    fields[1],
                    fields[2],
                    failed,
                    parse_bool(fields[4], row),
                    parse_bool(fields[5], row),
                    parse_bool(fields[6], row),
                );
                let want = (
                    fields[7].to_owned(),
                    fields[8].to_owned(),
                    fields[9].to_owned(),
                    fields[10].to_owned(),
                );
                assert_eq!(got, want, "kernel drift on fixture row {row:?}");
                saw_verdict_marker |= fields[7] == "@verdict";
                saw_prepull_marker |= fields[7] == "@verdict_prepull";
                classify_rows += 1;
            }
            Some(&"correlates") => {
                assert_eq!(fields.len(), 4, "malformed correlates row {row:?}");
                let want = parse_bool(fields[3], row);
                assert_eq!(
                    verdict_correlates(fields[1], fields[2]),
                    want,
                    "correlation drift on fixture row {row:?}"
                );
                correlate_rows += 1;
            }
            other => panic!("unknown fixture record type {other:?} in row {row:?}"),
        }
    }
    // Anti-vacuous: the capture genuinely exercises the whole contract.
    assert!(
        classify_rows >= 80,
        "thin fixture: {classify_rows} classify rows"
    );
    assert!(
        correlate_rows >= 6,
        "thin fixture: {correlate_rows} correlate rows"
    );
    assert!(
        saw_verdict_marker && saw_prepull_marker,
        "verdict markers missing from capture"
    );
}
