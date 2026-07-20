//! Fixture-driven differential for the rate-bridge payload verdict (Wave 3H).
//!
//! Replays the COMMITTED pre-conversion golden
//! (`tests/golden/fixtures/rate_bridge_kernel_cases.tsv` — the serde-free
//! line projection of `rate_bridge_verdicts.json`, sync-pinned byte-equal
//! by the Python golden test) through
//! [`brutex_pull_state::rate_bridge::bridge_payload_verdict`] and asserts:
//!
//! * the BINARY verdict matches the OBSERVED oracle outcome
//!   (`expected_valid == 1` ⇔ `BRIDGE_VERDICT_VALID`) — the exact contract
//!   the seam serves (an INVALID verdict answers `None`, a VALID one falls
//!   through to the oracle's construction);
//! * the echoed `pull_active` arm matches row-for-row.
//!
//! Granular first-violation codes are pinned by the crate's own unit tests
//! (`src/rate_bridge.rs`) — the TSV deliberately carries the served binary
//! contract only (the pull-intent golden precedent).

use brutex_pull_state::rate_bridge::{BRIDGE_VERDICT_VALID, bridge_payload_verdict};

fn fixture_path() -> std::path::PathBuf {
    // CARGO_MANIFEST_DIR = <repo>/rust/brutex-pull-state → two levels up.
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/fixtures/rate_bridge_kernel_cases.tsv")
}

/// Decode the generator's `<f8` little-endian hex bit pattern (bit-exact).
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

#[test]
fn frozen_golden_rows_replay_bit_for_bit() {
    let raw = std::fs::read_to_string(fixture_path()).expect("fixture readable");
    let mut rows = 0usize;
    let mut valid_rows = 0usize;
    for line in raw.lines() {
        if line.starts_with('#') || line.is_empty() {
            continue;
        }
        let cells: Vec<&str> = line.split('\t').collect();
        assert_eq!(cells.len(), 18, "row shape drifted: {line:?}");
        let id = cells[0];
        let root = cells[1] == "1";
        let schema_kind: u8 = cells[2].parse().unwrap();
        let schema_int: i64 = cells[3].parse().unwrap();
        let schema_float = f64_from_hex_bits(cells[4]);
        let field_kinds: Vec<u8> = cells[5..15].iter().map(|c| c.parse().unwrap()).collect();
        let pa_kind: u8 = cells[15].parse().unwrap();
        let expected_valid = cells[16] == "1";
        let expected_pa: u8 = cells[17].parse().unwrap();
        let (verdict, pa) = bridge_payload_verdict(
            root,
            schema_kind,
            schema_int,
            schema_float,
            &field_kinds,
            pa_kind,
        );
        assert_eq!(
            verdict == BRIDGE_VERDICT_VALID,
            expected_valid,
            "verdict drift on {id}: got {verdict}"
        );
        assert_eq!(pa, expected_pa, "pull_active arm drift on {id}");
        rows += 1;
        if expected_valid {
            valid_rows += 1;
        }
    }
    // Anti-vacuous: the frozen lattice genuinely carries both answers.
    assert!(rows >= 40, "suspiciously few golden rows: {rows}");
    assert!(
        valid_rows >= 10,
        "suspiciously few VALID rows: {valid_rows}"
    );
    assert!(valid_rows < rows, "no reject rows — vacuous fixture");
}
