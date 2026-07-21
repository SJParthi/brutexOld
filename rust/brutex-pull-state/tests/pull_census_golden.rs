//! Cross-language golden pin: the committed pull-census fixture
//! (`tests/golden/fixtures/pull_census_verdicts.tsv`, captured from the
//! PRE-conversion Python by `tests/golden/generate_pull_census_golden.py`)
//! is replayed through the Rust fold — every case's counts AND capped
//! sample triples must match bit-for-bit.  The SAME fixture is asserted
//! against the live Python by `tests/golden/test_pull_census_golden.py`, so
//! the two implementations are pinned to ONE frozen capture
//! (dependency-free TSV + a hand-rolled hex decode — the crate takes no
//! serde/base64 dependency; Cargo.toml stays frozen).

use brutex_pull_state::pull_census::census_fold;

const FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../tests/golden/fixtures/pull_census_verdicts.tsv"
);

/// UTF-8-hex → String (`@empty` = "").
fn decode_hex_utf8(field: &str, row: &str) -> String {
    if field == "@empty" {
        return String::new();
    }
    assert!(
        field.len().is_multiple_of(2),
        "odd hex length in fixture row {row:?}"
    );
    let mut bytes = Vec::with_capacity(field.len() / 2);
    for pair in field.as_bytes().chunks_exact(2) {
        let hex = std::str::from_utf8(pair).expect("ascii hex");
        let byte = u8::from_str_radix(hex, 16)
            .unwrap_or_else(|e| panic!("bad hex {hex:?} in fixture row {row:?}: {e}"));
        bytes.push(byte);
    }
    String::from_utf8(bytes).unwrap_or_else(|e| panic!("non-UTF-8 field in row {row:?}: {e}"))
}

fn parse_u64(field: &str, row: &str) -> u64 {
    field
        .parse()
        .unwrap_or_else(|e| panic!("bad u64 field {field:?} in fixture row {row:?}: {e}"))
}

#[test]
fn kernel_matches_the_frozen_python_capture() {
    let body = std::fs::read_to_string(FIXTURE)
        .unwrap_or_else(|e| panic!("cannot read the committed kernel fixture {FIXTURE}: {e}"));
    let mut cases = 0_u32;
    let mut capped_seen = false;
    for row in body.lines() {
        if row.is_empty() || row.starts_with('#') {
            continue;
        }
        let fields: Vec<&str> = row.split('\t').collect();
        assert_eq!(fields.len(), 7, "census row arity in {row:?}");
        assert_eq!(fields[0], "census", "unknown fixture table in {row:?}");
        let rows: Vec<(String, String, bool)> = if fields[2] == "@none" {
            Vec::new()
        } else {
            fields[2]
                .split(',')
                .map(|item| {
                    let parts: Vec<&str> = item.split('.').collect();
                    assert_eq!(parts.len(), 3, "row triple arity in {row:?}");
                    (
                        decode_hex_utf8(parts[0], row),
                        decode_hex_utf8(parts[1], row),
                        parts[2] == "1",
                    )
                })
                .collect()
        };
        let written: Vec<(String, String)> = if fields[3] == "@none" {
            Vec::new()
        } else {
            fields[3]
                .split(',')
                .map(|item| {
                    let parts: Vec<&str> = item.split('.').collect();
                    assert_eq!(parts.len(), 2, "written pair arity in {row:?}");
                    (
                        decode_hex_utf8(parts[0], row),
                        decode_hex_utf8(parts[1], row),
                    )
                })
                .collect()
        };
        let expected_samples: Vec<(u8, String, String)> = if fields[6] == "@none" {
            Vec::new()
        } else {
            fields[6]
                .split(',')
                .map(|item| {
                    let parts: Vec<&str> = item.split('.').collect();
                    assert_eq!(parts.len(), 3, "sample triple arity in {row:?}");
                    (
                        parts[0].parse().expect("sample kind"),
                        decode_hex_utf8(parts[1], row),
                        decode_hex_utf8(parts[2], row),
                    )
                })
                .collect()
        };
        let (su, wu, samples) = census_fold(&rows, &written);
        assert_eq!(
            su,
            parse_u64(fields[4], row),
            "settled_unwritten in {row:?}"
        );
        assert_eq!(
            wu,
            parse_u64(fields[5], row),
            "written_unsettled in {row:?}"
        );
        assert_eq!(samples, expected_samples, "samples in {row:?}");
        capped_seen |= samples.len() == 20 && (su + wu) > 20;
        cases += 1;
    }
    // Anti-vacuous: the corpus genuinely replays, incl. an over-cap case.
    assert!(cases >= 12, "census cases: {cases}");
    assert!(capped_seen, "no over-cap case replayed");
}
