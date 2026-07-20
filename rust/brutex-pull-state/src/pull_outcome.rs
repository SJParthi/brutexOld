//! The last-pull OUTCOME classifier — the pure DECISION core (Wave 1A).
//!
//! Law (DEC-OPERATOR-HEALTH-TRUTH-001, MIGRATION.md #1747, re-hosted under
//! DEC-RUST-PULL-STATE-001): classify the retained last-PULL run's recorded
//! state into the honest outcome vocabulary. This core decides WHICH arm
//! fires — the state machine (`running`{wedged > stalled > running} →
//! `error` → `done`{deferral-prefix BEFORE verdict; `failed > 0` outranks a
//! verdict} → idle fall-through) and the error-detail classification (the
//! market-hours deferral prefix, the `LakeWriteHalt` reason split, the
//! halt-name table, the generic-error arm) — and returns MACHINE CODES only:
//!
//! `(outcome, tone, headline_code, detail_code)`
//!
//! * `outcome` / `tone` — the module's stable wire tokens, verbatim.
//! * `headline_code` — which pinned WORDING Python must compose (`halt:<Name>`
//!   keys the Python-side `_HALT_NAME_OUTCOMES` table — headlines are never
//!   minted here; the two `halted_quota` wordings are why the code carries
//!   the exception NAME, not the outcome).
//! * `detail_code` — how Python must build the detail (`input` = the recorded
//!   detail verbatim · `empty` · `completed` = detail-or-canned-fallback).
//!
//! The VERDICT-consuming arms return the `@verdict` / `@verdict_prepull`
//! markers with empty companions: the banner fold, the colour authority
//! (`verdict_banner.banner_for` — CLAUDE.md §11 COMMON) and every human
//! string stay Python. Strings cross the FFI as UTF-8 `&str` (byte-faithful:
//! `starts_with` / `splitn(2, ':')` / `contains` are exactly Python's
//! `startswith` / `split(":", 1)[0]` / `in` over the same code points).
//!
//! Pinned bit-identical by the committed kernel fixture
//! (`tests/golden/fixtures/pull_outcome_kernel_cases.tsv` — read by BOTH the
//! Python golden gate and this crate's `pull_outcome_golden` test: one
//! frozen capture, two consumers).

/// The market-hours START-gate deferral detail prefix — a BYTE-COPY of
/// `brutex.dashboard.pull_outcome.MARKET_HOURS_DEFERRAL_PREFIX` (drift-pinned
/// by the committed kernel fixture's prefix + near-miss rows).
pub const MARKET_HOURS_DEFERRAL_PREFIX: &str = "deferred — inside NSE market-hours window";

/// The verdict-arm marker (state `done`): Python folds the correlated
/// verdict via its retained `_from_verdict` (banner colour authority).
pub const VERDICT_MARKER: &str = "@verdict";

/// The pre-pull verdict-arm marker (the idle / never-ran slot): Python folds
/// with the "stamped before any pull started" provenance.
pub const VERDICT_PREPULL_MARKER: &str = "@verdict_prepull";

/// The halt-name table — `(exception name, outcome, tone)`, a decision-only
/// twin of the Python `_HALT_NAME_OUTCOMES` (whose HEADLINE column stays
/// Python-side; the committed kernel fixture enumerates every name, so the
/// two tables cannot drift apart silently).
const HALT_NAME_OUTCOMES: [(&str, &str, &str); 8] = [
    ("MarketHoursHalt", "halted_market_hours", "warn"),
    ("DailyQuotaExceeded", "halted_quota", "bad"),
    ("QuotaInvariantError", "halted_quota", "bad"),
    ("GreeksEnrichPoolBroken", "halted_pool_broken", "bad"),
    ("MassEmptySettleHalt", "halted_mass_empty", "bad"),
    ("VendorDownError", "halted_vendor_down", "bad"),
    ("GrowwCredentialDeadError", "halted_auth", "bad"),
    ("PullAlreadyRunningError", "refused_busy", "warn"),
];

/// One classified decision — codes only, no human strings (see the module
/// docs for each field's contract).
type Codes = (String, String, String, String);

fn codes(outcome: &str, tone: &str, headline_code: &str, detail_code: &str) -> Codes {
    (
        outcome.to_owned(),
        tone.to_owned(),
        headline_code.to_owned(),
        detail_code.to_owned(),
    )
}

/// Classify a FAILED run's recorded `detail` string — the
/// `_from_error_detail` law, codes only.
fn from_error_detail(detail: &str) -> Codes {
    if detail.starts_with(MARKET_HOURS_DEFERRAL_PREFIX) {
        return codes(
            "deferred_market_hours",
            "warn",
            "deferred_market_hours",
            "input",
        );
    }
    // Python: `detail.split(":", 1)[0]` — the leading exception-NAME token
    // (only the FIRST split field is read, so a plain `split` is identical).
    let name = detail.split(':').next().unwrap_or(detail);
    if name == "LakeWriteHalt" {
        if detail.contains("disk_full") {
            return codes("halted_disk_full", "bad", "halted_disk_full", "input");
        }
        if detail.contains("out_of_memory") {
            return codes("halted_oom", "bad", "halted_oom", "input");
        }
        return codes("halted_lake_write", "bad", "halted_lake_write", "input");
    }
    for (table_name, outcome, tone) in HALT_NAME_OUTCOMES {
        if name == table_name {
            let headline_code = format!("halt:{table_name}");
            return (
                outcome.to_owned(),
                tone.to_owned(),
                headline_code,
                "input".to_owned(),
            );
        }
    }
    codes("error", "bad", "failed", "input")
}

/// The whole `classify_last_pull_outcome` DECISION plane — total over every
/// input (no error arm: an unknown `state` token takes the idle fall-through
/// exactly like Python's `if`-chain).
#[must_use]
pub fn classify_pull_outcome(
    state: &str,
    detail: &str,
    failed: i64,
    stalled: bool,
    wedged: bool,
    has_verdict: bool,
) -> Codes {
    if state == "running" {
        if wedged {
            return codes("wedged", "bad", "wedged", "input");
        }
        if stalled {
            return codes("running_stalled_suspect", "warn", "stalled", "input");
        }
        return codes("running", "ok", "running", "input");
    }
    if state == "error" {
        return from_error_detail(detail);
    }
    if state == "done" {
        if detail.starts_with(MARKET_HOURS_DEFERRAL_PREFIX) {
            // The pipeline's CAUGHT between-phases stop — checked BEFORE the
            // verdict fold (a stamped clean verdict must never paint a
            // deferred run green).
            return codes(
                "deferred_market_hours",
                "warn",
                "deferred_market_hours",
                "input",
            );
        }
        if failed > 0 {
            // THIS run's own quarantine tally outranks the verdict.
            return codes(
                "completed_with_failures",
                "warn",
                "completed_failures",
                "input",
            );
        }
        if has_verdict {
            return codes(VERDICT_MARKER, "", "", "");
        }
        return codes("completed", "ok", "completed", "completed");
    }
    // `idle` (and any unknown state token) — the fall-through arm.
    if has_verdict {
        return codes(VERDICT_PREPULL_MARKER, "", "", "");
    }
    codes("never_run", "idle", "never_run", "empty")
}

/// The run-id correlation law of `classify_retained_pull_outcome`: a verdict
/// is consulted ONLY when the retained snapshot carries a NON-empty run id
/// that equals the verdict holder's stamped id (a run-less deferral/refusal
/// record carries `""` and correlates with nothing).
#[must_use]
pub fn verdict_correlates(retained_id: &str, verdict_id: &str) -> bool {
    !retained_id.is_empty() && verdict_id == retained_id
}

#[cfg(test)]
mod tests {
    use super::*;

    fn outcome(state: &str, detail: &str, failed: i64, flags: (bool, bool, bool)) -> Codes {
        let (stalled, wedged, has_verdict) = flags;
        classify_pull_outcome(state, detail, failed, stalled, wedged, has_verdict)
    }

    #[test]
    fn running_precedence_wedged_outranks_stalled() {
        let (o, t, h, d) = outcome("running", "x", 0, (true, true, false));
        assert_eq!(
            (o.as_str(), t.as_str(), h.as_str(), d.as_str()),
            ("wedged", "bad", "wedged", "input")
        );
        let (o, t, ..) = outcome("running", "x", 0, (true, false, false));
        assert_eq!(
            (o.as_str(), t.as_str()),
            ("running_stalled_suspect", "warn")
        );
        let (o, t, ..) = outcome("running", "x", 0, (false, false, true));
        assert_eq!((o.as_str(), t.as_str()), ("running", "ok"));
    }

    #[test]
    fn error_prefix_beats_name_table() {
        // A deferral note that ALSO smuggles a colon still reads deferred.
        let detail = format!("{MARKET_HOURS_DEFERRAL_PREFIX}: extra");
        let (o, ..) = outcome("error", &detail, 0, (false, false, false));
        assert_eq!(o, "deferred_market_hours");
    }

    #[test]
    fn error_prefix_near_misses_do_not_match() {
        let truncated = &MARKET_HOURS_DEFERRAL_PREFIX[..MARKET_HOURS_DEFERRAL_PREFIX.len() - 1];
        let (o, ..) = outcome("error", truncated, 0, (false, false, false));
        assert_eq!(o, "error");
        let padded = format!(" {MARKET_HOURS_DEFERRAL_PREFIX}");
        let (o, ..) = outcome("error", &padded, 0, (false, false, false));
        assert_eq!(o, "error");
    }

    #[test]
    fn lake_write_halt_reason_split() {
        for (needle, expect) in [
            ("disk_full", "halted_disk_full"),
            ("out_of_memory", "halted_oom"),
            ("mystery", "halted_lake_write"),
        ] {
            let detail = format!("LakeWriteHalt: lake write halted: {needle}");
            let (o, t, h, d) = outcome("error", &detail, 0, (false, false, false));
            assert_eq!(o, expect);
            assert_eq!(t, "bad");
            assert_eq!(h, expect);
            assert_eq!(d, "input");
        }
        // The name with NO colon still keys the LakeWriteHalt arm (Python's
        // `split(":", 1)[0]` of a colon-less string is the whole string).
        let (o, ..) = outcome("error", "LakeWriteHalt", 0, (false, false, false));
        assert_eq!(o, "halted_lake_write");
        // …but a reason substring under a DIFFERENT name never reroutes.
        let (o, ..) = outcome(
            "error",
            "ValueError: disk_full elsewhere",
            0,
            (false, false, false),
        );
        assert_eq!(o, "error");
    }

    #[test]
    fn halt_table_names_classify_with_name_keyed_headline_code() {
        for (name, expected_outcome, expected_tone) in HALT_NAME_OUTCOMES {
            let detail = format!("{name}: boom");
            let (o, t, h, d) = outcome("error", &detail, 0, (false, false, false));
            assert_eq!(o, expected_outcome);
            assert_eq!(t, expected_tone);
            assert_eq!(h, format!("halt:{name}"));
            assert_eq!(d, "input");
        }
    }

    #[test]
    fn done_precedence_deferral_then_failed_then_verdict_then_completed() {
        let (o, ..) = outcome(
            "done",
            MARKET_HOURS_DEFERRAL_PREFIX,
            3,
            (false, false, true),
        );
        assert_eq!(o, "deferred_market_hours");
        let (o, t, h, d) = outcome("done", "note", 2, (false, false, true));
        assert_eq!(
            (o.as_str(), t.as_str(), h.as_str(), d.as_str()),
            (
                "completed_with_failures",
                "warn",
                "completed_failures",
                "input"
            )
        );
        let (o, t, h, d) = outcome("done", "note", 0, (false, false, true));
        assert_eq!(
            (o.as_str(), t.as_str(), h.as_str(), d.as_str()),
            ("@verdict", "", "", "")
        );
        let (o, t, h, d) = outcome("done", "note", 0, (false, false, false));
        assert_eq!(
            (o.as_str(), t.as_str(), h.as_str(), d.as_str()),
            ("completed", "ok", "completed", "completed")
        );
    }

    #[test]
    fn idle_and_unknown_states_fall_through() {
        for state in ["idle", "limbo-token", ""] {
            let (o, t, h, d) = outcome(state, "", 0, (false, false, false));
            assert_eq!(
                (o.as_str(), t.as_str(), h.as_str(), d.as_str()),
                ("never_run", "idle", "never_run", "empty")
            );
            let (o, ..) = outcome(state, "", 0, (false, false, true));
            assert_eq!(o, "@verdict_prepull");
        }
        // The running flags are ignored outside `running` (Python parity).
        let (o, ..) = outcome("done", "note", 0, (true, true, false));
        assert_eq!(o, "completed");
    }

    #[test]
    fn negative_failed_is_not_a_failure_count() {
        // Python's `failed > 0` — a (never-produced) negative count falls to
        // the verdict/completed arms, bit-identically.
        let (o, ..) = outcome("done", "", -1, (false, false, false));
        assert_eq!(o, "completed");
    }

    #[test]
    fn correlation_law() {
        assert!(verdict_correlates("p1", "p1"));
        assert!(!verdict_correlates("p1", "p2"));
        assert!(!verdict_correlates("p1", ""));
        assert!(!verdict_correlates("", ""));
        assert!(!verdict_correlates("", "p1"));
        assert!(verdict_correlates("run-α", "run-α"));
    }
}
