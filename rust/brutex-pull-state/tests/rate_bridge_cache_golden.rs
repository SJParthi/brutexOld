//! Fixture-driven differential for the rate-bridge-cache memo laws (Wave 3H).
//!
//! Replays the COMMITTED pre-conversion golden
//! (`tests/golden/fixtures/rate_bridge_cache_kernel_cases.tsv` — the
//! serde-free projection of `rate_bridge_cache_decisions.json`, sync-pinned
//! byte-equal by the Python golden test) through
//! [`brutex_pull_state::rate_bridge_cache::memo_serve_arm`] and
//! [`brutex_pull_state::rate_bridge_cache::token_revalidates`], asserting
//! every arm/verdict row-for-row — ages travel as IEEE-754 bit patterns
//! (no parse rounding), token cells as `s:`-prefixed strings with the
//! `\N` None sentinel (the empty string stays distinguishable).

use brutex_pull_state::rate_bridge_cache::{memo_serve_arm, token_revalidates};

fn fixture_path() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/fixtures/rate_bridge_cache_kernel_cases.tsv")
}

fn f64_from_hex_bits(cell: &str) -> f64 {
    assert_eq!(
        cell.len(),
        16,
        "bit-pattern cell must be 16 hex chars: {cell:?}"
    );
    let mut bytes = [0u8; 8];
    for (i, chunk) in bytes.iter_mut().enumerate() {
        *chunk = u8::from_str_radix(&cell[2 * i..2 * i + 2], 16)
            .unwrap_or_else(|e| panic!("bad hex byte in {cell:?}: {e}"));
    }
    f64::from_le_bytes(bytes)
}

fn opt_token(cell: &str) -> Option<&str> {
    match cell {
        "\\N" => None,
        other => Some(
            other
                .strip_prefix("s:")
                .unwrap_or_else(|| panic!("token cell must be \\N or s:-prefixed: {other:?}")),
        ),
    }
}

#[test]
fn frozen_golden_rows_replay_arm_for_arm() {
    let raw = std::fs::read_to_string(fixture_path()).expect("fixture readable");
    let (mut serve_rows, mut token_rows) = (0usize, 0usize);
    let mut arms_seen = [false; 3];
    let mut revalidate_seen = [false; 2];
    for line in raw.lines() {
        if line.starts_with('#') || line.is_empty() {
            continue;
        }
        let cells: Vec<&str> = line.split('\t').collect();
        match cells[0] {
            "serve" => {
                assert_eq!(cells.len(), 7, "serve row shape drifted: {line:?}");
                let id = cells[1];
                let age = match cells[2] {
                    "0" => None,
                    "1" => Some(f64_from_hex_bits(cells[3])),
                    other => panic!("bad age_kind {other:?} on {id}"),
                };
                let ttl = f64_from_hex_bits(cells[4]);
                let inflight = cells[5] == "1";
                let expected: u8 = cells[6].parse().unwrap();
                let got = memo_serve_arm(age, ttl, inflight);
                assert_eq!(got, expected, "serve-arm drift on {id}");
                arms_seen[usize::from(expected)] = true;
                serve_rows += 1;
            }
            "token" => {
                assert_eq!(cells.len(), 5, "token row shape drifted: {line:?}");
                let id = cells[1];
                let prior = opt_token(cells[2]);
                let token = opt_token(cells[3]);
                let expected = cells[4] == "1";
                assert_eq!(
                    token_revalidates(token, prior),
                    expected,
                    "token-law drift on {id}"
                );
                revalidate_seen[usize::from(expected)] = true;
                token_rows += 1;
            }
            other => panic!("unknown row family {other:?}"),
        }
    }
    // Anti-vacuous: every arm + both token answers genuinely exercised.
    assert!(
        serve_rows >= 12,
        "suspiciously few serve rows: {serve_rows}"
    );
    assert!(token_rows >= 6, "suspiciously few token rows: {token_rows}");
    assert!(
        arms_seen.iter().all(|s| *s),
        "an arm is missing from the lattice"
    );
    assert!(
        revalidate_seen.iter().all(|s| *s),
        "a token answer is missing"
    );
}
