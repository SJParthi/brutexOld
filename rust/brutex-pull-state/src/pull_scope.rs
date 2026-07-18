//! `pull_scope` note-surgery + scope-token laws (lane Wave 1B).
//!
//! Transcribed token-for-token from the PURE surface of
//! `brutex/dashboard/pull_scope.py` (DEC-PULL-SCOPED-ALL-ONECLICK-001,
//! MIGRATION.md #1779): the scope-token normalization verdict
//! ([`scope_token`]), the market-hours deferral-note rewrite
//! ([`market_hours_note_rewrite`]) and the two completion-note surgeries
//! ([`spot_only_note_surgery`] / [`fno_only_note_surgery`]). Outputs ARE
//! strings here, so byte-exactness is the whole contract: every anchor
//! literal, replacement, prefix-preservation and pass-through must be
//! byte-identical to the Python oracle (UTF-8 — the real notes carry
//! `—` em-dashes; `String::replacen(_, _, 1)` is the exact twin of CPython
//! `str.replace(old, new, 1)` first-occurrence semantics, and UTF-8's
//! self-synchronizing property guarantees a valid needle only ever matches
//! on char boundaries).
//!
//! The anchor literals are DUPLICATED from Python by design (a Rust crate
//! cannot import them) and drift-pinned three ways: the committed golden
//! fixture (`tests/golden/fixtures/pull_scope_note_surgery_rust.tsv`,
//! consumed by `tests/pull_scope_golden.rs` via `include_str!`), the
//! Python-side cross-language source pin
//! (`tests/unit/test_pull_scope_rust_seam.py` asserts these exact literals
//! appear in THIS file AND in the real `pipeline_pull` producer output),
//! and the flag-ON differential parity tests. A `_complete_note` wording
//! refactor therefore forces a conscious re-pin on BOTH languages, never a
//! silent desync (the design §4.5 risk).
//!
//! Python-permanent (never ported): the three no-op report builders
//! (dataclass constructors), `run_crossverify_tail` (exception-taxonomy
//! fence), the `note_surgery_miss` WARN (fired Python-side off the booleans
//! this module returns) and the step-3 `", " + GREEKS_NOT_RUN_NOTE +
//! crossverify_clause(tail)` append (composes the tail outcome).

// ── scope-token vocabulary (Python: SCOPE_* / KNOWN_PULL_SCOPES) ─────────────

/// `normalize_pull_scope` verdict code: the full default chain.
pub const SCOPE_CODE_FULL: u8 = 0;
/// Verdict code: the scoped-ALL spot pull.
pub const SCOPE_CODE_SPOT_ONLY: u8 = 1;
/// Verdict code: the F&O-only pull (round-6 two-button split).
pub const SCOPE_CODE_FNO_ONLY: u8 = 2;
/// Verdict code: unknown token — Python composes the `{token!r}` refusal
/// (CPython repr is never re-minted in Rust).
pub const SCOPE_CODE_UNKNOWN: u8 = 255;

/// The default scope token (Python `SCOPE_FULL`).
pub const SCOPE_FULL: &str = "full";
/// The spot-only scope token (Python `SCOPE_SPOT_ONLY`).
pub const SCOPE_SPOT_ONLY: &str = "spot-only";
/// The fno-only scope token (Python `SCOPE_FNO_ONLY`).
pub const SCOPE_FNO_ONLY: &str = "fno-only";

// ── anchor literals (Python pull_scope.py — drift-pinned, see module docs) ───

/// Python `GREEKS_NOT_RUN_NOTE` (appended Python-side; frozen here for the pin).
pub const GREEKS_NOT_RUN_NOTE: &str = "Greeks: not run — no options in this pull";
/// Python `FNO_SKIPPED_CLAUSE`.
pub const FNO_SKIPPED_CLAUSE: &str = ", F&O skipped by design (spot-only scope)";
/// Python `SPOT_SKIPPED_CLAUSE`.
pub const SPOT_SKIPPED_CLAUSE: &str = "spot skipped by design (F&O-only scope)";
/// Python `_NOOP_SPOT_CLAUSE` — the exact `_complete_note` render of the
/// all-zeros fno-only spot report.
pub const NOOP_SPOT_CLAUSE: &str = "spot clean (0 pair(s))";
/// Python `_NOOP_FNO_CLAUSE` — the exact `_complete_note` render of the
/// all-zeros spot-only no-op F&O reports.
pub const NOOP_FNO_CLAUSE: &str =
    ", metadata 0 expiries + 0 contracts, historical 0 bars over 0 contract(s)";
/// Python `_STOP_FNO_CLAUSE` — the exact `_spot_stop_note` trailer.
pub const STOP_FNO_CLAUSE: &str = "metadata + historical NOT started";
/// Python `_STOP_FNO_SPOT_ONLY_CLAUSE`.
pub const STOP_FNO_SPOT_ONLY_CLAUSE: &str = "F&O not applicable (spot-only scope)";
/// Python `_FULL_PREFIX` — the `_complete_note` completed-chain prefix.
pub const FULL_PREFIX: &str = "pipeline complete — ";
/// Python `_SPOT_ONLY_PREFIX`.
pub const SPOT_ONLY_PREFIX: &str = "spot-only pipeline complete — ";
/// Python `_FNO_ONLY_PREFIX`.
pub const FNO_ONLY_PREFIX: &str = "F&O-only pipeline complete — ";
/// Python `_MARKET_HOURS_WINDOW_CLAUSE` — the `_market_hours_stop_note`
/// bare clock claim.
pub const MARKET_HOURS_WINDOW_CLAUSE: &str = "inside NSE market-hours window (08:45-15:35 IST)";
/// Python `_MARKET_HOURS_WINDOW_SPOT_ONLY_CLAUSE` — the both-arms wording.
pub const MARKET_HOURS_WINDOW_SPOT_ONLY_CLAUSE: &str =
    "market-session protection window active (NSE hours 08:45-15:35 IST or a Muhurat session)";
/// Python `_MARKET_HOURS_PHASES_CLAUSE` — the generic three-phase list.
pub const MARKET_HOURS_PHASES_CLAUSE: &str = "(spot / metadata / historical skipped to keep the shared Groww account free for the live feed)";
/// Python `_MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE`.
pub const MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE: &str = "(spot deferred to keep the shared Groww \
account free for the live feed; F&O not applicable — spot-only scope)";
/// Python `_MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE`.
pub const MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE: &str = "(metadata / historical deferred to keep \
the shared Groww account free for the live feed; spot not applicable — F&O-only scope)";
/// Python `_MARKET_HOURS_RESUME_CLAUSE` — the self-contradictory-during-
/// Muhurat resume advice.
pub const MARKET_HOURS_RESUME_CLAUSE: &str = "resume on the next out-of-hours run";
/// Python `_MARKET_HOURS_RESUME_SPOT_ONLY_CLAUSE`.
pub const MARKET_HOURS_RESUME_SPOT_ONLY_CLAUSE: &str = "resume once the protection window clears";

// ── the pure laws ────────────────────────────────────────────────────────────

/// CPython `str.strip()` whitespace parity predicate.
///
/// Rust `char::is_whitespace` is the Unicode `White_Space` property;
/// CPython `str.isspace()` ADDITIONALLY treats the bidirectional-class
/// B/S separators U+001C..U+001F as whitespace (verified exhaustively over
/// the full codepoint range, 2026-07-18: the delta is EXACTLY those four).
/// The trim must match CPython or a `"\x1c"`-padded token would normalize
/// differently across the FFI.
fn is_cpython_strip_whitespace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// The `normalize_pull_scope` VERDICT (Python pull_scope.py:98-112).
///
/// `None` / blank / whitespace-only → [`SCOPE_CODE_FULL`] (the byte-identical
/// default); a known token (post the CPython-parity strip) → its code;
/// anything else → [`SCOPE_CODE_UNKNOWN`] — the Python seam falls through to
/// the retained oracle, which composes the `ValueError` refusal text (it
/// embeds `{token!r}` CPython-repr, deliberately never reproduced here).
#[must_use]
pub fn scope_token(token: Option<&str>) -> u8 {
    let stripped = token
        .unwrap_or("")
        .trim_matches(is_cpython_strip_whitespace);
    match stripped {
        "" => SCOPE_CODE_FULL,
        SCOPE_FULL => SCOPE_CODE_FULL,
        SCOPE_SPOT_ONLY => SCOPE_CODE_SPOT_ONLY,
        SCOPE_FNO_ONLY => SCOPE_CODE_FNO_ONLY,
        _ => SCOPE_CODE_UNKNOWN,
    }
}

/// `rewrite_market_hours_note` (Python pull_scope.py:310-328) — token-for-token.
///
/// Replaces the three exact deferral-note clauses (first occurrence each —
/// CPython `str.replace(old, new, 1)`) with wording true for BOTH composed
/// belt arms; a drifted note (anchors absent) passes through UNCHANGED
/// (diagnostic, never load-bearing — no WARN by design). Pure; idempotent on
/// an already-rewritten note (the replacement strings contain no anchor).
#[must_use]
pub fn market_hours_note_rewrite(note: &str, phases_replacement: &str) -> String {
    let note = note.replacen(
        MARKET_HOURS_WINDOW_CLAUSE,
        MARKET_HOURS_WINDOW_SPOT_ONLY_CLAUSE,
        1,
    );
    let note = note.replacen(MARKET_HOURS_PHASES_CLAUSE, phases_replacement, 1);
    note.replacen(
        MARKET_HOURS_RESUME_CLAUSE,
        MARKET_HOURS_RESUME_SPOT_ONLY_CLAUSE,
        1,
    )
}

/// The `build_spot_only_note` SURGERY steps (Python pull_scope.py:425-469)
/// MINUS the Python-side step-3 append + WARN.
///
/// Returns `(rewritten_note, prefix_present, clause_present)` — the two
/// booleans are computed AFTER the market-hours rewrite + stop-clause
/// replacement, exactly where the Python oracle computes them; the seam
/// fires the `note_surgery_miss` WARN off `prefix_present != clause_present`
/// and appends the Greeks + crossverify clauses itself.
#[must_use]
pub fn spot_only_note_surgery(base_note: &str) -> (String, bool, bool) {
    let mut note = market_hours_note_rewrite(base_note, MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE);
    if note.contains(STOP_FNO_CLAUSE) {
        note = note.replacen(STOP_FNO_CLAUSE, STOP_FNO_SPOT_ONLY_CLAUSE, 1);
    }
    let prefix_present = note.contains(FULL_PREFIX);
    let clause_present = note.contains(NOOP_FNO_CLAUSE);
    if clause_present {
        note = note.replacen(NOOP_FNO_CLAUSE, FNO_SKIPPED_CLAUSE, 1);
    }
    if prefix_present {
        note = note.replacen(FULL_PREFIX, SPOT_ONLY_PREFIX, 1);
    }
    (note, prefix_present, clause_present)
}

/// The `build_fno_only_note` SURGERY (Python pull_scope.py:472-515) MINUS
/// the Python-side WARN — the exact spot-axis mirror (no appends at all:
/// Greeks genuinely run on an fno-only pull and the crossverify tail is
/// Spot-exclusive by ruling).
#[must_use]
pub fn fno_only_note_surgery(base_note: &str) -> (String, bool, bool) {
    let mut note = market_hours_note_rewrite(base_note, MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE);
    let prefix_present = note.contains(FULL_PREFIX);
    let clause_present = note.contains(NOOP_SPOT_CLAUSE);
    if clause_present {
        note = note.replacen(NOOP_SPOT_CLAUSE, SPOT_SKIPPED_CLAUSE, 1);
    }
    if prefix_present {
        note = note.replacen(FULL_PREFIX, FNO_ONLY_PREFIX, 1);
    }
    (note, prefix_present, clause_present)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scope_token_vocabulary_and_defaults() {
        assert_eq!(scope_token(None), SCOPE_CODE_FULL);
        assert_eq!(scope_token(Some("")), SCOPE_CODE_FULL);
        assert_eq!(scope_token(Some("   ")), SCOPE_CODE_FULL);
        assert_eq!(scope_token(Some("full")), SCOPE_CODE_FULL);
        assert_eq!(scope_token(Some("spot-only")), SCOPE_CODE_SPOT_ONLY);
        assert_eq!(scope_token(Some("fno-only")), SCOPE_CODE_FNO_ONLY);
        assert_eq!(scope_token(Some("  spot-only  ")), SCOPE_CODE_SPOT_ONLY);
        assert_eq!(scope_token(Some("SPOT-ONLY")), SCOPE_CODE_UNKNOWN);
        assert_eq!(scope_token(Some("spot_only")), SCOPE_CODE_UNKNOWN);
        assert_eq!(scope_token(Some("spot—only")), SCOPE_CODE_UNKNOWN);
    }

    #[test]
    fn scope_token_cpython_strip_parity_controls() {
        // U+001C..U+001F are CPython-str.strip whitespace but NOT Rust
        // char::is_whitespace — the parity trim must strip them.
        assert_eq!(scope_token(Some("\u{1c}full\u{1d}")), SCOPE_CODE_FULL);
        assert_eq!(
            scope_token(Some("\u{1e}spot-only\u{1f}")),
            SCOPE_CODE_SPOT_ONLY
        );
        // Shared White_Space members (NEL, NBSP, ideographic space) strip too.
        assert_eq!(
            scope_token(Some("\u{85}fno-only\u{a0}")),
            SCOPE_CODE_FNO_ONLY
        );
        assert_eq!(scope_token(Some("\u{3000}full\u{2003}")), SCOPE_CODE_FULL);
        // NUL is NOT whitespace on either side.
        assert_eq!(scope_token(Some("spot-only\u{0}")), SCOPE_CODE_UNKNOWN);
    }

    #[test]
    fn market_hours_rewrite_is_idempotent_and_passthrough() {
        let real = format!(
            "deferred — {MARKET_HOURS_WINDOW_CLAUSE}; no vendor pull issued \
             {MARKET_HOURS_PHASES_CLAUSE}; {MARKET_HOURS_RESUME_CLAUSE}"
        );
        let once = market_hours_note_rewrite(&real, MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE);
        assert!(once.contains(MARKET_HOURS_WINDOW_SPOT_ONLY_CLAUSE));
        assert!(once.contains(MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE));
        assert!(once.contains(MARKET_HOURS_RESUME_SPOT_ONLY_CLAUSE));
        assert!(!once.contains(MARKET_HOURS_PHASES_CLAUSE));
        // Idempotent: the replacements contain no anchor.
        let twice = market_hours_note_rewrite(&once, MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE);
        assert_eq!(once, twice);
        // Drifted note (no anchors) passes through byte-identically.
        let drifted = "deferred — some future wording";
        assert_eq!(market_hours_note_rewrite(drifted, "x"), drifted);
    }

    #[test]
    fn spot_surgery_first_occurrence_only_and_booleans() {
        let base = format!("{FULL_PREFIX}spot clean (1 pair(s)){NOOP_FNO_CLAUSE}{NOOP_FNO_CLAUSE}");
        let (note, prefix, clause) = spot_only_note_surgery(&base);
        assert!(prefix);
        assert!(clause);
        assert!(note.starts_with(SPOT_ONLY_PREFIX));
        // First occurrence replaced; the second survives verbatim (the
        // CPython replace(old, new, 1) law).
        assert!(note.contains(FNO_SKIPPED_CLAUSE));
        assert!(note.contains(NOOP_FNO_CLAUSE));
        assert_eq!(note.matches(NOOP_FNO_CLAUSE).count(), 1);
    }

    #[test]
    fn spot_surgery_stop_note_trailer_and_half_drift() {
        let stop =
            format!("stopped after spot — clean (3 window(s) still failing); {STOP_FNO_CLAUSE}");
        let (note, prefix, clause) = spot_only_note_surgery(&stop);
        assert!(!prefix);
        assert!(!clause);
        assert!(note.contains(STOP_FNO_SPOT_ONLY_CLAUSE));
        assert!(!note.contains(STOP_FNO_CLAUSE));
        // Half drift: prefix present, clause absent → booleans diverge (the
        // Python seam turns that into the note_surgery_miss WARN).
        let (_, p, c) = spot_only_note_surgery("pipeline complete — went sideways");
        assert!(p);
        assert!(!c);
    }

    #[test]
    fn fno_surgery_mirror_and_reverse_half_drift() {
        let base = format!(
            "{FULL_PREFIX}{NOOP_SPOT_CLAUSE}, metadata 4 expiries + 40 contracts, \
             historical 0 bars over 0 contract(s)"
        );
        let (note, prefix, clause) = fno_only_note_surgery(&base);
        assert!(prefix);
        assert!(clause);
        assert!(note.starts_with(FNO_ONLY_PREFIX));
        assert!(note.contains(SPOT_SKIPPED_CLAUSE));
        assert!(!note.contains(NOOP_SPOT_CLAUSE));
        // Reverse half drift: clause present, prefix absent.
        let (_, p, c) = fno_only_note_surgery(&format!("new prefix {NOOP_SPOT_CLAUSE}"));
        assert!(!p);
        assert!(c);
        // Full drift passes through untouched.
        let drifted = "stopped after metadata — 1 expiry failure(s); historical NOT started";
        let (out, p2, c2) = fno_only_note_surgery(drifted);
        assert_eq!(out, drifted);
        assert!(!p2);
        assert!(!c2);
    }

    #[test]
    fn utf8_em_dash_boundaries_survive_byte_exact() {
        // The ledger prefix + quarantine clause carry em-dashes AROUND the
        // anchors — the surgery must never split a UTF-8 boundary.
        let base = format!(
            "window ledger reload FAILED — running full re-verify pull (no skip); \
             {FULL_PREFIX}1 symbol(s) quarantined: BADSYM — spot x (1 pair(s)){NOOP_FNO_CLAUSE}"
        );
        let (note, prefix, clause) = spot_only_note_surgery(&base);
        assert!(prefix);
        assert!(clause);
        assert!(note.starts_with("window ledger reload FAILED — "));
        assert!(note.contains(SPOT_ONLY_PREFIX));
        assert!(std::str::from_utf8(note.as_bytes()).is_ok());
    }
}
