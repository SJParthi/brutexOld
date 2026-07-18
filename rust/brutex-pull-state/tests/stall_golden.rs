//! Fixture-driven differential for the pull-stall decision core (Wave 1C).
//!
//! Replays the COMMITTED pre-conversion golden
//! (`tests/golden/fixtures/pull_stall_verdicts.tsv` — the serde-free line
//! projection of `pull_stall_verdicts.json`, sync-pinned byte-equal by the
//! Python golden test) through [`brutex_pull_state::wedge_decision`] /
//! [`brutex_pull_state::alive_log_due`] and asserts every token/due bit —
//! the lane's bit-identity contract, served by the KERNEL with floats
//! travelling as IEEE-754 bit patterns (no parse rounding anywhere).
//!
//! Plus proptest invariants: totality (never panics over arbitrary f64
//! bit patterns incl. NaN payloads), the guard-flag dominance law, and the
//! alive law's equivalence to the negated strict `<`.

use brutex_pull_state::{
    WEDGE_ABSTAIN_PROBE_NONE, WEDGE_ADOPT_BASELINE, WEDGE_ESCALATE, WEDGE_NO_PROBE, alive_log_due,
    wedge_decision,
};
use proptest::prelude::*;

fn fixture_path() -> std::path::PathBuf {
    // CARGO_MANIFEST_DIR = <repo>/rust/brutex-pull-state → two levels up.
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/fixtures/pull_stall_verdicts.tsv")
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

fn parse_bool01(cell: &str) -> bool {
    match cell {
        "0" => false,
        "1" => true,
        other => panic!("expected 0/1 flag cell, got {other:?}"),
    }
}

/// `"\\N"` = Python `None` (a token cell may legitimately be the EMPTY string).
fn parse_token(cell: &str) -> Option<&str> {
    if cell == "\\N" { None } else { Some(cell) }
}

#[test]
fn frozen_golden_wedge_and_alive_rows_all_match() {
    let raw = std::fs::read_to_string(fixture_path())
        .expect("committed golden fixture pull_stall_verdicts.tsv must exist");
    let (mut wedge_rows, mut alive_rows) = (0usize, 0usize);
    for line in raw.lines() {
        let cells: Vec<&str> = line.split('\t').collect();
        match cells[0] {
            "wedge" => {
                assert_eq!(cells.len(), 12, "wedge row shape: {line:?}");
                let label = cells[1];
                let restart = if cells[8] == "-" {
                    None
                } else {
                    Some(f64_from_hex_bits(cells[8]))
                };
                let got = wedge_decision(
                    parse_bool01(cells[2]),
                    parse_bool01(cells[3]),
                    parse_bool01(cells[4]),
                    parse_bool01(cells[5]),
                    f64_from_hex_bits(cells[6]),
                    f64_from_hex_bits(cells[7]),
                    restart,
                    parse_token(cells[9]),
                    parse_token(cells[10]),
                );
                let expected: u8 = cells[11].parse().expect("token cell");
                assert_eq!(got, expected, "wedge golden row {label:?} diverged");
                wedge_rows += 1;
            }
            "alive" => {
                assert_eq!(cells.len(), 5, "alive row shape: {line:?}");
                let label = cells[1];
                let got = alive_log_due(f64_from_hex_bits(cells[2]), f64_from_hex_bits(cells[3]));
                assert_eq!(
                    got,
                    parse_bool01(cells[4]),
                    "alive golden row {label:?} diverged"
                );
                alive_rows += 1;
            }
            other => panic!("unknown golden row kind {other:?}"),
        }
    }
    // Anti-vacuous teeth: the committed lattice is non-trivial on both tables.
    assert!(
        wedge_rows >= 40,
        "wedge lattice unexpectedly small: {wedge_rows}"
    );
    assert!(
        alive_rows >= 10,
        "alive lattice unexpectedly small: {alive_rows}"
    );
}

/// Arbitrary f64 from raw bit patterns — the FULL plane incl. NaN payloads,
/// infinities, subnormals and signed zeros.
fn any_f64_bits() -> impl Strategy<Value = f64> {
    any::<u64>().prop_map(f64::from_bits)
}

fn any_token() -> impl Strategy<Value = Option<String>> {
    prop_oneof![
        Just(None),
        Just(Some(String::new())),
        "[a-z0-9:.-]{0,24}".prop_map(Some),
    ]
}

proptest! {
    /// Totality: no input combination panics, and the token stays in-vocabulary.
    #[test]
    fn wedge_decision_is_total_and_in_vocabulary(
        hung in any::<bool>(),
        fired in any::<bool>(),
        hook in any::<bool>(),
        captured in any::<bool>(),
        age in any_f64_bits(),
        window in any_f64_bits(),
        restart in proptest::option::of(any_f64_bits()),
        baseline in any_token(),
        current in any_token(),
    ) {
        let tok = wedge_decision(
            hung, fired, hook, captured, age, window, restart,
            baseline.as_deref(), current.as_deref(),
        );
        prop_assert!(tok <= WEDGE_ESCALATE);
    }

    /// Guard dominance: a latched / hook-less / healthy / uncaptured state is
    /// ALWAYS token 0, whatever the ages and tokens say.
    #[test]
    fn refused_guards_dominate(
        age in any_f64_bits(),
        window in any_f64_bits(),
        restart in proptest::option::of(any_f64_bits()),
        baseline in any_token(),
        current in any_token(),
        which in 0u8..4,
    ) {
        let (hung, fired, hook, captured) = match which {
            0 => (false, false, true, true),
            1 => (true, true, true, true),
            2 => (true, false, false, true),
            _ => (true, false, true, false),
        };
        let tok = wedge_decision(
            hung, fired, hook, captured, age, window, restart,
            baseline.as_deref(), current.as_deref(),
        );
        prop_assert_eq!(tok, WEDGE_NO_PROBE);
    }

    /// A `None` current ALWAYS abstains once past the guards; a `None`
    /// baseline with a real current ALWAYS adopts — the arm order law.
    #[test]
    fn none_token_arm_order(
        age in any_f64_bits(),
        window in any_f64_bits(),
        restart in proptest::option::of(any_f64_bits()),
        baseline in any_token(),
        current in "[a-z0-9:.-]{0,24}",
    ) {
        // Force past the age guard: NaN/under-window ages refuse, so pick an
        // age that never compares `<` window (the +inf age).
        let tok_none_current = wedge_decision(
            true, false, true, true, f64::INFINITY, window, restart,
            baseline.as_deref(), None,
        );
        prop_assert_eq!(tok_none_current, WEDGE_ABSTAIN_PROBE_NONE);
        let _ = age;
        let tok_none_baseline = wedge_decision(
            true, false, true, true, f64::INFINITY, window, restart,
            None, Some(current.as_str()),
        );
        prop_assert_eq!(tok_none_baseline, WEDGE_ADOPT_BASELINE);
    }

    /// `alive_log_due` ≡ `!(since < interval)` over the FULL f64 plane.
    #[test]
    #[allow(clippy::neg_cmp_op_on_partial_ord)] // the LAW is the negated strict `<` (NaN-faithful) — asserting it verbatim
    fn alive_log_due_matches_the_negation(
        since in any_f64_bits(),
        interval in any_f64_bits(),
    ) {
        prop_assert_eq!(alive_log_due(since, interval), !(since < interval));
    }
}
