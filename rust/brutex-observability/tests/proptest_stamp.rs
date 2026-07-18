//! Property suite for laws D1 / D2 — never-panics over arbitrary strings +
//! option arms, plus law equivalence against independent reference
//! computations (DEC-RUST-OBSERVABILITY-001 §8.2).

use brutex_observability::{VERDICT_HASH_MISMATCH, VERDICT_OK, stamp_shape_index, stamp_verdict};
use proptest::prelude::*;

proptest! {
    /// D1 equals the first-false-index reference over all boolean inputs.
    #[test]
    fn d1_first_false_wins(flags in proptest::array::uniform6(any::<bool>())) {
        let expected = flags
            .iter()
            .position(|ok| !ok)
            .map_or(-1, |index| index as i64);
        prop_assert_eq!(
            stamp_shape_index(flags[0], flags[1], flags[2], flags[3], flags[4], flags[5]),
            expected
        );
    }

    /// D2 never panics on arbitrary (unicode) strings and equals the
    /// independent mismatch-expression reference; the token vocabulary is
    /// closed over {"ok", "hash_mismatch"}.
    #[test]
    fn d2_matches_reference_expression(
        recomputed in any::<String>(),
        content_hash in any::<String>(),
        stage in any::<String>(),
        expected_stage in any::<String>(),
        run_id in any::<String>(),
        expected_run_id in proptest::option::of(any::<String>()),
        stamped_snapshot in proptest::option::of(any::<String>()),
        expected_snapshot in proptest::option::of(any::<String>()),
    ) {
        let token = stamp_verdict(
            &recomputed,
            &content_hash,
            &stage,
            &expected_stage,
            &run_id,
            expected_run_id.as_deref(),
            stamped_snapshot.as_deref(),
            expected_snapshot.as_deref(),
        );
        let mismatch = recomputed != content_hash
            || stage != expected_stage
            || expected_run_id.as_deref().is_some_and(|expected| run_id != expected)
            || (expected_snapshot.is_some()
                && stamped_snapshot.is_some()
                && stamped_snapshot != expected_snapshot);
        prop_assert_eq!(token, if mismatch { VERDICT_HASH_MISMATCH } else { VERDICT_OK });
    }

    /// D2 with every axis equal (and any option-arm presence pattern where
    /// present sides are EQUAL) is always OK — absent evidence never fails.
    #[test]
    fn d2_equal_sides_always_ok(
        hash in any::<String>(),
        stage in any::<String>(),
        run_id in any::<String>(),
        with_run in any::<bool>(),
        with_stamped in any::<bool>(),
        with_expected in any::<bool>(),
        snapshot in any::<String>(),
    ) {
        let token = stamp_verdict(
            &hash,
            &hash,
            &stage,
            &stage,
            &run_id,
            with_run.then_some(run_id.as_str()),
            with_stamped.then_some(snapshot.as_str()),
            with_expected.then_some(snapshot.as_str()),
        );
        prop_assert_eq!(token, VERDICT_OK);
    }
}
