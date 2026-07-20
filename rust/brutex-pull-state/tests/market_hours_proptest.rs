//! Property invariants over the pure market-hours core
//! (DEC-RUST-PULL-STATE-001 — the brutex-feed proptest shape).

use brutex_pull_state::{BLOCK_END_US, BLOCK_START_US, DAY_US, pull_allowed_us};
use proptest::prelude::*;

proptest! {
    /// The predicate is TOTAL over valid instants and matches the closed-form
    /// law: allowed ⇔ non-trading OR outside `[BLOCK_START_US, BLOCK_END_US)`.
    #[test]
    fn matches_closed_form_law(us in 0..DAY_US, trading in any::<bool>()) {
        let expected = !trading || !(BLOCK_START_US..BLOCK_END_US).contains(&us);
        prop_assert_eq!(pull_allowed_us(us, trading), Ok(expected));
    }

    /// Non-trading days are ALWAYS allowed (weekend/holiday law).
    #[test]
    fn non_trading_always_allowed(us in 0..DAY_US) {
        prop_assert_eq!(pull_allowed_us(us, false), Ok(true));
    }

    /// Determinism: the same input always yields the same verdict.
    #[test]
    fn deterministic(us in 0..DAY_US, trading in any::<bool>()) {
        prop_assert_eq!(pull_allowed_us(us, trading), pull_allowed_us(us, trading));
    }

    /// Every out-of-range instant is refused for BOTH flag values — never a
    /// silent clamp, never a panic (the core is total over all of i64).
    #[test]
    fn out_of_range_refused(us in prop_oneof![i64::MIN..0, DAY_US..i64::MAX], trading in any::<bool>()) {
        prop_assert!(pull_allowed_us(us, trading).is_err());
    }
}
