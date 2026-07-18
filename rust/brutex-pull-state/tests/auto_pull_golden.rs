//! Cross-language golden pin: the committed auto-pull predicate fixture
//! (`tests/golden/fixtures/auto_pull_reload_verdicts.tsv`, captured from the
//! PRE-conversion Python by `tests/golden/generate_auto_pull_golden.py`) is
//! replayed through the Rust predicate — every kernel row must match
//! bit-for-bit.  The SAME fixture is asserted against the live Python by
//! `tests/golden/test_auto_pull_golden.py` (dependency-free TSV; Cargo.toml
//! stays frozen).

use brutex_pull_state::auto_pull::should_reload_master;

const FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../tests/golden/fixtures/auto_pull_reload_verdicts.tsv"
);

fn parse_bool(field: &str, row: &str) -> bool {
    match field {
        "1" => true,
        "0" => false,
        other => panic!("bad boolean field {other:?} in fixture row {row:?}"),
    }
}

fn parse_i64(field: &str, row: &str) -> i64 {
    field
        .parse()
        .unwrap_or_else(|e| panic!("bad i64 field {field:?} in fixture row {row:?}: {e}"))
}

#[test]
fn kernel_matches_the_frozen_python_capture() {
    let body = std::fs::read_to_string(FIXTURE)
        .unwrap_or_else(|e| panic!("cannot read the committed kernel fixture {FIXTURE}: {e}"));
    let mut rows = 0_u32;
    let mut reload_seen = false;
    let mut keep_seen = false;
    for row in body.lines() {
        if row.is_empty() || row.starts_with('#') {
            continue;
        }
        let fields: Vec<&str> = row.split('\t').collect();
        assert_eq!(fields.len(), 8, "reload row arity in {row:?}");
        assert_eq!(fields[0], "reload", "unknown fixture table in {row:?}");
        let got = should_reload_master(
            parse_i64(fields[2], row),
            parse_i64(fields[3], row),
            parse_bool(fields[4], row),
            parse_bool(fields[5], row),
            parse_bool(fields[6], row),
        );
        assert_eq!(got, parse_bool(fields[7], row), "reload row {row:?}");
        reload_seen |= got;
        keep_seen |= !got;
        rows += 1;
    }
    // Anti-vacuous: the whole in-i64 lattice replays, both verdicts present.
    assert!(rows >= 40, "reload rows: {rows}");
    assert!(reload_seen && keep_seen);
}
