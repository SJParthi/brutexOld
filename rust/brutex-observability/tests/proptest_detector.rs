//! Property suite for laws A / B / C — never-panics over raw-bit f64s +
//! arbitrary strings, plus the state-law invariants
//! (DEC-OBS-RUST-DETECTOR-CORE-001 §8.2).

use brutex_observability::{
    TIER_CRITICAL, TIER_OK, TIER_WARNED, loop_watchdog_check_once, predicate_holds, sla_decide,
    watchdog_decide,
};
use proptest::prelude::*;

/// Every f64 bit pattern — NaNs (all payloads), ±inf, ±0.0, subnormals.
fn raw_f64() -> impl Strategy<Value = f64> {
    any::<u64>().prop_map(f64::from_bits)
}

/// A tier token: usually one of the three real tokens, sometimes junk.
fn tier_token() -> impl Strategy<Value = String> {
    prop_oneof![
        Just(TIER_OK.to_string()),
        Just(TIER_WARNED.to_string()),
        Just(TIER_CRITICAL.to_string()),
        any::<String>(),
    ]
}

proptest! {
    /// Law A never panics and honours its state-law invariants over the
    /// full raw-f64 plane × arbitrary tier tokens. `episodes` is bounded
    /// below `u64::MAX` — the MAX+warn-edge overflow panic is the
    /// separately-pinned disclosed edge (edge_matrix.rs).
    #[test]
    fn loop_law_invariants(
        now in raw_f64(),
        last_beat in raw_f64(),
        tier in tier_token(),
        ssb in raw_f64(),
        lla in raw_f64(),
        laa in raw_f64(),
        episodes in 0..u64::MAX,
        warn_after_s in raw_f64(),
        critical_after_s in raw_f64(),
        relog_every_s in raw_f64(),
        realert_every_s in raw_f64(),
    ) {
        let v = loop_watchdog_check_once(
            now, last_beat, &tier, ssb, lla, laa, episodes,
            warn_after_s, critical_after_s, relog_every_s, realert_every_s,
        );
        // lag is clamped: never NaN, never negative.
        prop_assert!(!v.lag_s.is_nan());
        prop_assert!(v.lag_s >= 0.0);
        // The RETURNED tier is always one of the three tokens.
        prop_assert!([TIER_OK, TIER_WARNED, TIER_CRITICAL].contains(&v.tier));
        // Episodes are monotone: +1 iff warn_edge, carried otherwise.
        prop_assert_eq!(v.episodes, episodes + u64::from(v.warn_edge));
        // A warn edge only ever fires from the literal "ok" token.
        if v.warn_edge {
            prop_assert_eq!(tier.as_str(), TIER_OK);
        }
        // Recovery resets to the fresh state (stamps +0.0, tier ok) and is
        // exclusive with every stalled-sample flag.
        if v.recovered {
            prop_assert_eq!(v.tier, TIER_OK);
            prop_assert_eq!(v.stall_started_beat.to_bits(), 0.0f64.to_bits());
            prop_assert_eq!(v.last_logged_at.to_bits(), 0.0f64.to_bits());
            prop_assert_eq!(v.last_alerted_at.to_bits(), 0.0f64.to_bits());
            prop_assert!(!(v.warn_edge || v.critical_edge || v.relog || v.realert));
            prop_assert!(!v.stall_total_s.is_nan());
            prop_assert!(v.stall_total_s >= 0.0);
        } else {
            prop_assert_eq!(v.stall_total_s.to_bits(), 0.0f64.to_bits());
        }
        // Stamp provenance: each output stamp is bitwise one of
        // {carried, now, last_beat, 0.0} — the replace() law's whole range.
        let among = |out: f64, carried: f64| {
            out.to_bits() == carried.to_bits()
                || out.to_bits() == now.to_bits()
                || out.to_bits() == last_beat.to_bits()
                || out.to_bits() == 0.0f64.to_bits()
        };
        prop_assert!(among(v.stall_started_beat, ssb));
        prop_assert!(among(v.last_logged_at, lla));
        prop_assert!(among(v.last_alerted_at, laa));
    }

    /// Law A is a pure function — bit-identical on repeat calls.
    #[test]
    fn loop_law_deterministic(
        now in raw_f64(),
        last_beat in raw_f64(),
        tier in tier_token(),
        ssb in raw_f64(),
        lla in raw_f64(),
        laa in raw_f64(),
        episodes in 0..u64::MAX,
    ) {
        let a = loop_watchdog_check_once(
            now, last_beat, &tier, ssb, lla, laa, episodes, 5.0, 30.0, 60.0, 300.0,
        );
        let b = loop_watchdog_check_once(
            now, last_beat, &tier, ssb, lla, laa, episodes, 5.0, 30.0, 60.0, 300.0,
        );
        // Bitwise field compare — `PartialEq` would read NaN != NaN, but
        // determinism here means BIT-identical outputs (NaN payloads too).
        prop_assert_eq!(a.tier, b.tier);
        prop_assert_eq!(a.stall_started_beat.to_bits(), b.stall_started_beat.to_bits());
        prop_assert_eq!(a.last_logged_at.to_bits(), b.last_logged_at.to_bits());
        prop_assert_eq!(a.last_alerted_at.to_bits(), b.last_alerted_at.to_bits());
        prop_assert_eq!(a.episodes, b.episodes);
        prop_assert_eq!(a.lag_s.to_bits(), b.lag_s.to_bits());
        prop_assert_eq!(
            (a.warn_edge, a.critical_edge, a.relog, a.realert, a.recovered),
            (b.warn_edge, b.critical_edge, b.relog, b.realert, b.recovered)
        );
        prop_assert_eq!(a.stall_total_s.to_bits(), b.stall_total_s.to_bits());
    }

    /// Law B: elapsed is the RAW bit-exact difference (no clamp), hung is
    /// the strict `>`, edge is the two-state law — over the full f64 plane.
    #[test]
    fn watchdog_law_bit_exact(
        now in raw_f64(),
        last_beat in raw_f64(),
        timeout in raw_f64(),
        prev_hung in any::<bool>(),
    ) {
        let (elapsed, hung, edge) = watchdog_decide(now, last_beat, timeout, prev_hung);
        prop_assert_eq!(elapsed.to_bits(), (now - last_beat).to_bits());
        prop_assert_eq!(hung, elapsed > timeout);
        prop_assert_eq!(edge, hung != prev_hung);
    }

    /// Law C never panics on arbitrary tokens; any token outside the two
    /// named comparisons behaves EXACTLY like "equals" (the fallthrough
    /// law); a NaN value never breaches under any token.
    #[test]
    fn sla_law_fallthrough_and_nan(
        token in any::<String>(),
        value in raw_f64(),
        threshold in raw_f64(),
        prev_breach in any::<bool>(),
    ) {
        let breached = predicate_holds(&token, value, threshold);
        if token != "greater_than" && token != "less_than" {
            prop_assert_eq!(breached, predicate_holds("equals", value, threshold));
        }
        if value.is_nan() {
            prop_assert!(!breached);
        }
        let (b2, edge) = sla_decide(&token, value, threshold, prev_breach);
        prop_assert_eq!(b2, breached);
        prop_assert_eq!(edge, breached != prev_breach);
    }
}
