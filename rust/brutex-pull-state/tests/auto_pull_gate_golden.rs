//! Fixture-driven differential for the auto-pull-gate decision cores (Wave 2D).
//!
//! Replays the COMMITTED pre-conversion golden
//! (`tests/golden/fixtures/auto_pull_gate_verdicts.tsv` — the serde-free line
//! projection of `auto_pull_gate_verdicts.json`, sync-pinned byte-equal by the
//! Python golden test) through [`brutex_pull_state::auto_gate_decision`] /
//! [`brutex_pull_state::muhurat_window_active`] /
//! [`brutex_pull_state::settle_token_verdict`] and asserts every verdict —
//! the lane's bit-identity contract. The gate rows carry the
//! Python-RESOLVED calendar predicates (trading-day / coverage /
//! muhurat-listed), exactly what the runtime facade passes — the NSE
//! calendar itself never crosses.
//!
//! Plus a proptest invariant: the gate ladder is total over the valid
//! input lattice and its reason vocabulary is closed.

use brutex_pull_state::{
    GATE_BEFORE_SETTLE, GATE_MARKET_HOURS_BLOCKED, GATE_MUHURAT_DEFERRAL, GATE_NON_TRADING_DAY,
    GATE_OK, SETTLE_BAD_SHAPE, SETTLE_BELOW_FLOOR, SETTLE_NO_VALUE, SETTLE_NOT_WALL_CLOCK,
    SETTLE_OK, auto_gate_decision, muhurat_window_active, settle_token_verdict,
};
use proptest::prelude::*;

fn fixture_path() -> std::path::PathBuf {
    // CARGO_MANIFEST_DIR = <repo>/rust/brutex-pull-state → two levels up.
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/fixtures/auto_pull_gate_verdicts.tsv")
}

fn parse_bool01(cell: &str) -> bool {
    match cell {
        "0" => false,
        "1" => true,
        other => panic!("expected 0/1 flag cell, got {other:?}"),
    }
}

fn parse_i64(cell: &str) -> i64 {
    cell.parse()
        .unwrap_or_else(|e| panic!("bad i64 cell {cell:?}: {e}"))
}

fn gate_reason_code(token: &str) -> u8 {
    match token {
        "ok" => GATE_OK,
        "non_trading_day" => GATE_NON_TRADING_DAY,
        "muhurat_deferral" => GATE_MUHURAT_DEFERRAL,
        "before_settle" => GATE_BEFORE_SETTLE,
        "market_hours_blocked" => GATE_MARKET_HOURS_BLOCKED,
        other => panic!("unknown gate reason token {other:?}"),
    }
}

fn settle_verdict_code(token: &str) -> u8 {
    match token {
        "ok" => SETTLE_OK,
        "bad_shape" => SETTLE_BAD_SHAPE,
        "not_wall_clock" => SETTLE_NOT_WALL_CLOCK,
        "below_floor" => SETTLE_BELOW_FLOOR,
        other => panic!("unknown settle verdict token {other:?}"),
    }
}

#[test]
fn frozen_golden_rows_all_match() {
    let raw = std::fs::read_to_string(fixture_path())
        .expect("committed golden fixture auto_pull_gate_verdicts.tsv must exist");
    let (mut gate_rows, mut mwin_rows, mut settle_rows) = (0usize, 0usize, 0usize);
    for line in raw.lines() {
        let cells: Vec<&str> = line.split('\t').collect();
        match cells.first().copied() {
            Some("gate") => {
                assert_eq!(cells.len(), 10, "gate row arity: {line:?}");
                let (trading, coverage, muhurat) = (
                    parse_bool01(cells[3]),
                    parse_bool01(cells[4]),
                    parse_bool01(cells[5]),
                );
                let (now_us, settle_us) = (parse_i64(cells[6]), parse_i64(cells[7]));
                let expected_reason = gate_reason_code(cells[8]);
                let expected_allowed = parse_bool01(cells[9]);
                let (reason, coverage_warn) =
                    auto_gate_decision(trading, coverage, muhurat, now_us, settle_us)
                        .unwrap_or_else(|e| panic!("gate row refused ({line:?}): {e}"));
                assert_eq!(reason, expected_reason, "reason mismatch: {line:?}");
                assert_eq!(
                    reason == GATE_OK,
                    expected_allowed,
                    "allowed mismatch: {line:?}"
                );
                // The coverage-WARN law: fires iff a trading day's holiday
                // coverage is exhausted (the non-trading arm suppresses it).
                assert_eq!(
                    coverage_warn,
                    trading && !coverage,
                    "warn mismatch: {line:?}"
                );
                gate_rows += 1;
            }
            Some("mwin") => {
                assert_eq!(cells.len(), 8, "mwin row arity: {line:?}");
                let active = muhurat_window_active(
                    parse_i64(cells[2]),
                    parse_i64(cells[3]),
                    parse_i64(cells[4]),
                    parse_i64(cells[5]),
                    parse_i64(cells[6]),
                )
                .unwrap_or_else(|e| panic!("mwin row refused ({line:?}): {e}"));
                assert_eq!(active, parse_bool01(cells[7]), "active mismatch: {line:?}");
                mwin_rows += 1;
            }
            Some("settle") => {
                assert_eq!(cells.len(), 5, "settle row arity: {line:?}");
                let (verdict, value_us) = settle_token_verdict(cells[1], parse_i64(cells[2]))
                    .unwrap_or_else(|e| panic!("settle row refused ({line:?}): {e}"));
                assert_eq!(
                    verdict,
                    settle_verdict_code(cells[3]),
                    "verdict mismatch: {line:?}"
                );
                let expected_us = parse_i64(cells[4]);
                assert_eq!(value_us, expected_us, "value mismatch: {line:?}");
                if verdict != SETTLE_OK {
                    assert_eq!(value_us, SETTLE_NO_VALUE, "non-ok value slot: {line:?}");
                }
                settle_rows += 1;
            }
            other => panic!("unknown TSV row kind {other:?} in {line:?}"),
        }
    }
    // Anti-vacuous floors: the committed projection carries the default-env
    // grid (150) + the floor-exact grid (16), every listed session's
    // margined boundaries (6×5) and the whole settle-token matrix (18).
    assert!(gate_rows >= 166, "gate rows thin: {gate_rows}");
    assert!(mwin_rows >= 30, "mwin rows thin: {mwin_rows}");
    assert!(settle_rows >= 18, "settle rows thin: {settle_rows}");
}

proptest! {
    /// Totality + closed reason vocabulary over the valid input lattice —
    /// and `allowed == (reason == GATE_OK)` is a structural identity the
    /// Python seam relies on.
    #[test]
    fn gate_ladder_total_and_closed(
        trading in any::<bool>(),
        coverage in any::<bool>(),
        muhurat in any::<bool>(),
        now_us in 0..86_400_000_000_i64,
        settle_us in 0..86_400_000_000_i64,
    ) {
        let (reason, coverage_warn) =
            auto_gate_decision(trading, coverage, muhurat, now_us, settle_us)
                .expect("valid lattice input must never refuse");
        prop_assert!(reason <= GATE_MARKET_HOURS_BLOCKED);
        prop_assert_eq!(coverage_warn, trading && !coverage);
        if !trading {
            prop_assert_eq!(reason, GATE_NON_TRADING_DAY);
        }
    }
}
