//! Property invariants over the pure backup decision core
//! (`DEC-BACKUP-RUST-OPS-001`) — determinism + structural laws, plus a
//! differential pin of `build_backup_argv` against a naive reference.
//! The Rust-vs-PYTHON differential lives in the pytest hypothesis suite
//! (`tests/unit/test_backup_rust.py`), driven through the real wheel.

use brutex_ops::{
    ARM_FRESH, ARM_OVERDUE, ARM_RUNNING, DeadmanDecision, SECONDS_PER_HOUR, VERDICT_FRESH,
    VERDICT_RUNNING, build_backup_argv, evaluate_deadman_decision, parse_ls_file_paths,
    parse_snapshots_latest_decision,
};
use proptest::prelude::*;

fn opt_seconds() -> impl Strategy<Value = Option<f64>> {
    prop_oneof![
        Just(None),
        (-1.0e9_f64..1.0e9).prop_map(Some),
        Just(Some(0.0)),
        Just(Some(-0.0)),
    ]
}

proptest! {
    /// The decision is a pure function — identical inputs, identical output.
    #[test]
    fn deadman_decision_is_deterministic(
        state_error in any::<bool>(),
        state in any::<bool>(),
        success in opt_seconds(),
        running in opt_seconds(),
        alert in opt_seconds(),
        threshold in 0.1_f64..1000.0,
        realert in 0.1_f64..1000.0,
        skew in 0.0_f64..100_000.0,
        max_run in 0.1_f64..1000.0,
    ) {
        let a = evaluate_deadman_decision(
            state_error, state, success, running, alert, threshold, realert, skew, max_run,
        );
        let b = evaluate_deadman_decision(
            state_error, state, success, running, alert, threshold, realert, skew, max_run,
        );
        prop_assert_eq!(a, b);
    }

    /// Structural laws: RUNNING/FRESH never alert; `hours` is exactly
    /// `success_s / 3600.0` whenever a state carries a success stamp and the
    /// verdict path computes it; arm ↔ verdict pairing is consistent.
    #[test]
    fn deadman_decision_structural_laws(
        success in opt_seconds(),
        running in opt_seconds(),
        alert in opt_seconds(),
        threshold in 0.1_f64..1000.0,
        realert in 0.1_f64..1000.0,
        skew in 0.0_f64..100_000.0,
        max_run in 0.1_f64..1000.0,
    ) {
        let d: DeadmanDecision = evaluate_deadman_decision(
            false, true, success, running, alert, threshold, realert, skew, max_run,
        );
        if d.verdict == VERDICT_RUNNING {
            prop_assert_eq!(d.arm, ARM_RUNNING);
            prop_assert!(!d.should_alert, "RUNNING never pages (F17b)");
        }
        if d.verdict == VERDICT_FRESH {
            prop_assert_eq!(d.arm, ARM_FRESH);
            prop_assert!(!d.should_alert);
        }
        if let (Some(s), Some(h)) = (success, d.hours_since_success) {
            // Bit-identical division law (the oracle's total_seconds()/3600).
            prop_assert_eq!(h.to_bits(), (s / SECONDS_PER_HOUR).to_bits());
        }
        if d.arm == ARM_OVERDUE {
            prop_assert!(d.elapsed > threshold);
        }
        if d.arm == ARM_FRESH {
            prop_assert!(d.elapsed <= threshold);
            prop_assert!(d.elapsed.is_sign_positive() || d.elapsed > 0.0);
        }
    }

    /// `build_backup_argv` differential vs a naive index-based reference.
    #[test]
    fn backup_argv_matches_naive_reference(
        binary in "[a-z/]{1,12}",
        paths in prop::collection::vec("[a-zA-Z0-9/._ -]{0,16}", 0..4),
        excludes in prop::collection::vec("[a-zA-Z0-9*._-]{0,12}", 0..4),
        tags in prop::collection::vec("[a-z-]{0,8}", 0..3),
    ) {
        let mut expected: Vec<String> = Vec::new();
        expected.push(binary.clone());
        expected.push("backup".into());
        for p in &paths { expected.push(p.clone()); }
        for e in &excludes { expected.push("--exclude".into()); expected.push(e.clone()); }
        expected.push("--exclude-caches".into());
        for t in &tags { expected.push("--tag".into()); expected.push(t.clone()); }
        expected.push("--host".into());
        expected.push("brutex".into());
        expected.push("--json".into());
        prop_assert_eq!(build_backup_argv(&binary, &paths, &excludes, &tags), expected);
    }

    /// The parsers never panic on arbitrary input (total functions).
    #[test]
    fn parsers_are_total(stdout in ".{0,256}") {
        let _ = parse_snapshots_latest_decision(&stdout);
        let _ = parse_ls_file_paths(&stdout);
        let _ = brutex_ops::parse_backup_summary_snapshot_id(&stdout);
        let _ = brutex_ops::parse_ls_snapshot_time_decision(&stdout);
    }
}
