//! The pull-stall wedge/alive DECISION core — pure, clockless (Wave 1C).
//!
//! Laws (`brutex/dashboard/pull_stall.py` — DEC-PULL-STALL-DETECTION-001 +
//! DEC-PULL-WEDGE-ESCALATE-001 + DEC-PULL-TELEMETRY-TRIAD-001), transcribed
//! token-for-token from `_escalation_wants_probe_locked` /
//! `_maybe_escalate_locked` / `_alive_payload_locked`'s throttle refusal:
//!
//! * [`wedge_decision`] — the DUAL-signal wedge-escalation verdict. Given
//!   the guard flags plus PRE-COMPUTED ages (the caller reads its monotonic
//!   clock ONCE and subtracts — timers, locks and wall clocks stay Python),
//!   it names EXACTLY which arm the Python oracle would take. The caller
//!   (`PullWatchdog`) maps the token to the arm's ACTIONS — baseline
//!   mutation, window-restart stamps, the CRITICAL log + `on_wedged` hook —
//!   verbatim in Python, so log lines + event payloads stay byte-identical.
//! * [`alive_log_due`] — the throttled alive-beat emit-now? law: the
//!   VERBATIM negation of the Python refusal `now - last < interval`
//!   (spelled `!(since < interval)`, NEVER `since >= interval` — a NaN age
//!   compares `false` either way in IEEE-754, so the oracle FALLS THROUGH
//!   and emits; the negation preserves that, the `>=` spelling would not).
//!
//! Every comparison is a strict `<` against the pre-multiplied wedge window
//! (`multiplier × timeout`, an IEEE-identical f64 product computed by the
//! caller) with early-return-on-true — the exact shape of the Python
//! guards, so NaN/inf ages fall through IDENTICALLY on both arms (pinned by
//! the frozen golden's NaN/inf/negative-skew rows).
//!
//! The two `# pragma: no cover` narrowing arms of the Python oracle
//! (`last_beat_at is None` / the post-guard `hook is None` re-check) are
//! guarded-above dead code there; here they are GENUINELY unreachable — the
//! caller only consults the kernel with a real last-beat age, and the flags
//! subsume the hook re-check (documented, not replicated).

/// Token 0 — the `_escalation_wants_probe_locked` guards refuse (healthy /
/// already latched / no hook / under-window silence / baseline not captured).
pub const WEDGE_NO_PROBE: u8 = 0;
/// Token 1 — this poll's probe token is `None`: the DUAL signal cannot be
/// confirmed, never escalate on heartbeat silence alone.
pub const WEDGE_ABSTAIN_PROBE_NONE: u8 = 1;
/// Token 2 — the HUNG-edge baseline was `None` (the edge probe abstained):
/// adopt the first later real token + restart the wedge window.
pub const WEDGE_ADOPT_BASELINE: u8 = 2;
/// Token 3 — the token moved (durable progress despite heartbeat silence):
/// re-baseline + restart the wedge window.
pub const WEDGE_REBASELINE: u8 = 3;
/// Token 4 — the restarted window has not yet spanned the full wedge window:
/// defer the verdict.
pub const WEDGE_DEFER: u8 = 4;
/// Token 5 — the DUAL signal holds: fire `on_wedged` (the terminal latch).
pub const WEDGE_ESCALATE: u8 = 5;

/// The DUAL-signal wedge-escalation decision — pure + clockless.
///
/// Arguments mirror the Python oracle's state at one poll: `hung` = the
/// underlying watchdog reads HUNG; `already_fired` = the terminal latch;
/// `hook_bound` = `on_wedged is not None`; `baseline_captured` = the
/// HUNG-edge capture ran (its token may still have been `None`);
/// `last_beat_age_s` / `restart_age_s` = `now - stored` computed by the
/// caller from ONE clock read; `wedge_window_s` = `multiplier × timeout`
/// (caller-computed f64 product); `baseline` / `current` = the revision
/// tokens (`None` = the probe abstained).
#[allow(clippy::too_many_arguments, clippy::fn_params_excessive_bools)] // 1:1 with the oracle's state — a struct would blur the FFI mirror
#[must_use]
pub fn wedge_decision(
    hung: bool,
    already_fired: bool,
    hook_bound: bool,
    baseline_captured: bool,
    last_beat_age_s: f64,
    wedge_window_s: f64,
    restart_age_s: Option<f64>,
    baseline: Option<&str>,
    current: Option<&str>,
) -> u8 {
    // `_escalation_wants_probe_locked` (:319-:327), guard order preserved.
    if already_fired || !hook_bound {
        return WEDGE_NO_PROBE;
    }
    if !hung {
        return WEDGE_NO_PROBE;
    }
    // Strict `<` (:325): an age EXACTLY at the window is over it; a NaN age
    // compares false and FALLS THROUGH (the oracle's IEEE behaviour).
    if last_beat_age_s < wedge_window_s {
        return WEDGE_NO_PROBE;
    }
    if !baseline_captured {
        return WEDGE_NO_PROBE;
    }
    // `_maybe_escalate_locked` (:356-:388), arm order preserved.
    let Some(current) = current else {
        return WEDGE_ABSTAIN_PROBE_NONE;
    };
    let Some(baseline) = baseline else {
        return WEDGE_ADOPT_BASELINE;
    };
    if current != baseline {
        return WEDGE_REBASELINE;
    }
    if let Some(restart_age) = restart_age_s {
        // Strict `<` (:378): a NaN restart age compares false → escalate,
        // exactly like the oracle's compound condition.
        if restart_age < wedge_window_s {
            return WEDGE_DEFER;
        }
    }
    WEDGE_ESCALATE
}

/// The throttled alive-beat emit-now? law — `!(since < interval)`, verbatim.
///
/// The first-check `-inf` seed yields `since == +inf` → due; a NaN `since`
/// (poisoned clock) falls through the strict `<` → due, exactly like the
/// Python oracle's refusal `now - last < interval`.
#[must_use]
#[allow(clippy::neg_cmp_op_on_partial_ord)] // DELIBERATE: `>=` flips the NaN arm (IEEE) — the oracle's law is the negated strict `<`
pub fn alive_log_due(since_last_s: f64, interval_s: f64) -> bool {
    !(since_last_s < interval_s)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The escalate-shaped baseline inputs (every guard satisfied).
    fn escalate_args() -> (bool, bool, bool, bool, f64, f64) {
        (true, false, true, true, 3601.0, 3600.0)
    }

    #[test]
    fn guard_flags_refuse_before_the_probe_arms() {
        let (_, _, _, _, age, w) = escalate_args();
        for hung in [false, true] {
            for fired in [false, true] {
                for hook in [false, true] {
                    for captured in [false, true] {
                        let tok = wedge_decision(
                            hung,
                            fired,
                            hook,
                            captured,
                            age,
                            w,
                            None,
                            Some("revA"),
                            Some("revA"),
                        );
                        if hung && !fired && hook && captured {
                            assert_eq!(tok, WEDGE_ESCALATE);
                        } else {
                            assert_eq!(tok, WEDGE_NO_PROBE, "h{hung} f{fired} k{hook} c{captured}");
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn age_boundary_is_strict_less_than() {
        let (h, f, k, c, _, w) = escalate_args();
        let eps = 2f64.powi(-10);
        let dec = |age: f64| wedge_decision(h, f, k, c, age, w, None, Some("a"), Some("a"));
        assert_eq!(dec(w - eps), WEDGE_NO_PROBE);
        assert_eq!(dec(w), WEDGE_ESCALATE); // exactly at the window = over it
        assert_eq!(dec(w + eps), WEDGE_ESCALATE);
        assert_eq!(dec(-10.0), WEDGE_NO_PROBE); // clock-skew negative
        assert_eq!(dec(f64::NAN), WEDGE_ESCALATE); // NaN falls through `<`
        assert_eq!(dec(f64::INFINITY), WEDGE_ESCALATE);
    }

    #[test]
    fn token_arms_in_oracle_order() {
        let (h, f, k, c, age, w) = escalate_args();
        let dec =
            |b: Option<&str>, cur: Option<&str>| wedge_decision(h, f, k, c, age, w, None, b, cur);
        assert_eq!(dec(None, None), WEDGE_ABSTAIN_PROBE_NONE); // abstain outranks adopt
        assert_eq!(dec(Some("a"), None), WEDGE_ABSTAIN_PROBE_NONE);
        assert_eq!(dec(None, Some("b")), WEDGE_ADOPT_BASELINE);
        assert_eq!(dec(None, Some("")), WEDGE_ADOPT_BASELINE); // "" is a REAL token
        assert_eq!(dec(Some("a"), Some("b")), WEDGE_REBASELINE);
        assert_eq!(dec(Some(""), Some("a")), WEDGE_REBASELINE);
        assert_eq!(dec(Some(""), Some("")), WEDGE_ESCALATE); // equal empties
        assert_eq!(dec(Some("a"), Some("a")), WEDGE_ESCALATE);
    }

    #[test]
    fn restart_window_defers_until_spanned() {
        let (h, f, k, c, age, w) = escalate_args();
        let dec = |restart: Option<f64>| {
            wedge_decision(h, f, k, c, age, w, restart, Some("a"), Some("a"))
        };
        assert_eq!(dec(None), WEDGE_ESCALATE);
        assert_eq!(dec(Some(0.0)), WEDGE_DEFER);
        assert_eq!(dec(Some(w - 2f64.powi(-10))), WEDGE_DEFER);
        assert_eq!(dec(Some(w)), WEDGE_ESCALATE); // strict `<` — spanned exactly
        assert_eq!(dec(Some(-4.0)), WEDGE_DEFER); // future restart (skew) defers
        assert_eq!(dec(Some(f64::NAN)), WEDGE_ESCALATE); // NaN falls through `<`
        assert_eq!(dec(Some(f64::INFINITY)), WEDGE_ESCALATE);
    }

    #[test]
    fn restart_never_outranks_token_arms() {
        let (h, f, k, c, age, w) = escalate_args();
        let restart = Some(1.0); // well inside the window
        assert_eq!(
            wedge_decision(h, f, k, c, age, w, restart, Some("a"), Some("b")),
            WEDGE_REBASELINE
        );
        assert_eq!(
            wedge_decision(h, f, k, c, age, w, restart, None, Some("b")),
            WEDGE_ADOPT_BASELINE
        );
        assert_eq!(
            wedge_decision(h, f, k, c, age, w, restart, Some("a"), None),
            WEDGE_ABSTAIN_PROBE_NONE
        );
    }

    #[test]
    fn alive_log_due_is_the_negated_strict_less_than() {
        assert!(alive_log_due(f64::INFINITY, 200.0)); // the -inf seed shape
        assert!(!alive_log_due(0.0, 200.0));
        assert!(!alive_log_due(200.0 - 2f64.powi(-10), 200.0));
        assert!(alive_log_due(200.0, 200.0)); // exactly at the interval = due
        assert!(alive_log_due(200.0 + 2f64.powi(-10), 200.0));
        assert!(!alive_log_due(-5.0, 200.0)); // clock-skew negative throttles
        assert!(alive_log_due(f64::NAN, 200.0)); // NaN falls through — emits
        assert!(alive_log_due(0.0, f64::NAN)); // NaN interval also falls through
    }
}
