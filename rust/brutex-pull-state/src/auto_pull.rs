//! The instrument-master RELOAD predicate kernel (Wave 3G,
//! `DEC-RUST-PULL-STATE-001`).
//!
//! Re-hosts the ONE pure decision law of `brutex/dashboard/auto_pull.py`
//! (DEC-AUTO-UNIVERSE-001 / DEC-STALE-ECONOMICS-HEAL-001 /
//! DEC-ELIGIBILITY-AUTOHEAL-001): whether `cmd_dashboard`'s auto-heal should
//! (re)load the keyless instrument master.  Transcribed VERBATIM — including
//! the oracle's redundant `lake_is_empty` term (subsumed by
//! `indices_missing`; kept token-for-token so the transcription is an
//! auditable 1:1, never a "simplification").
//!
//! Counts cross as i64 (an out-of-i64 CPython int stays on the DOCUMENTED
//! Python arm — the seam's representability gate, no warn); the store
//! probes that RESOLVE the five inputs stay Python.

/// `should_reload_instrument_master`'s five-input law — pure, TOTAL.
#[must_use]
pub fn should_reload_master(
    index_count: i64,
    stock_count: i64,
    legs_present: bool,
    stale_economics: bool,
    stale_eligibility: bool,
) -> bool {
    let lake_is_empty = index_count == 0 && stock_count == 0;
    let indices_missing = index_count == 0;
    lake_is_empty || indices_missing || legs_present || stale_economics || stale_eligibility
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clean_populated_lake_never_reloads() {
        assert!(!should_reload_master(3, 5, false, false, false));
    }

    #[test]
    fn empty_or_indices_missing_reloads() {
        assert!(should_reload_master(0, 0, false, false, false));
        assert!(should_reload_master(0, 5, false, false, false));
        // Zero STOCKS with indices present is NOT a trigger (the law keys
        // on index_count only) — the golden pins this asymmetry.
        assert!(!should_reload_master(3, 0, false, false, false));
    }

    #[test]
    fn each_staleness_flag_alone_triggers() {
        assert!(should_reload_master(3, 5, true, false, false));
        assert!(should_reload_master(3, 5, false, true, false));
        assert!(should_reload_master(3, 5, false, false, true));
    }

    #[test]
    fn negative_counts_are_not_zero() {
        assert!(!should_reload_master(-2, 7, false, false, false));
    }
}
