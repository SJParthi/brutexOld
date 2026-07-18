//! The pull-intent PARSE-SHAPE + RESUME-DECISION kernel (Wave 2E,
//! `DEC-RUST-PULL-STATE-001`).
//!
//! Re-hosts the four pure decision laws of
//! `brutex/dashboard/pull_intent.py` (DEC-PULL-INTENT-RESUME-001):
//!
//! * [`shape_verdict`] — `PullIntent.from_payload`'s strict JSON-shape
//!   lattice over MECHANICALLY-classified field codes.  The JSON DECODE
//!   (and therefore CPython's ENTIRE failure taxonomy — `UnicodeDecodeError`
//!   ⊂ `ValueError`, `json.JSONDecodeError`, nesting-bomb `RecursionError`,
//!   `OverflowError`) deliberately STAYS Python: the seam classifies the
//!   already-decoded payload's concrete types into small integer codes, and
//!   every decode failure folds into the ONE not-a-Mapping root code — so
//!   taxonomy parity holds by CONSTRUCTION (the design-risk-#3 posture:
//!   no serde / hand-rolled parser exists to drift; the crate takes no JSON
//!   dependency).  The LAW lives here: absent ≡ null for the five request
//!   fields, a JSON `true` is NOT a count (the bool-pun refusal), the
//!   non-negative-int law (arbitrary-precision CPython ints outside i64
//!   arrive as pure representability codes and keep the sign law), and
//!   `created_at`'s absent-defaults-empty vs present-null-is-corrupt split.
//! * [`resume_decision`] — `run_auto_resume`'s arm lattice with the
//!   load-bearing ORDER pinned: no-intent → peer-probe → cap → proceed
//!   (a peer-active boot must never burn a restart token).
//! * [`refused_restore_decision`] — the
//!   `restore_intent_after_refused_start` lattice
//!   (DEC-PULL-SCOPED-ALL-ONECLICK-001 refuter-r3 F-1): no-action /
//!   leave-absent / clear-orphan / restore-prior-verbatim.
//! * [`resume_enabled`] — the `resolve_pull_intent_resume` env-token law
//!   (default ON; the case-insensitive falsy set disables), with the
//!   STRIP implemented over CPython's `str.isspace` character set — a
//!   SUPERSET of Unicode `White_Space` (U+001C..U+001F carry bidi class
//!   B/S, so CPython strips them while `str::trim` would not; the golden
//!   env corpus pins exactly this class).
//!
//! Codes only cross back: every warn string, structlog event and the
//! `PullIntent` construction itself stay Python (the dataclass's frozen
//! `==` cleanup law in `run_legacy_resume` depends on it).

// ── shape-verdict field-kind codes (the FFI input contract; mirrored by the
//    facade constants in brutex/pull_state_rust.py + the golden harness) ────

/// Field kind: the key is absent from the payload mapping.
pub const KIND_ABSENT: u8 = 0;
/// Field kind: the key is present with a JSON `null` value.
pub const KIND_NULL: u8 = 1;
/// Field kind: the key is present with a string value.
pub const KIND_STR: u8 = 2;
/// Field kind: the key is present with any other value type.
pub const KIND_OTHER: u8 = 3;

/// `restarts` kind: absent key.
pub const RESTARTS_ABSENT: u8 = 0;
/// `restarts` kind: present `null`.
pub const RESTARTS_NULL: u8 = 1;
/// `restarts` kind: a JSON bool — the concrete CPython type tag (`bool`
/// subclasses `int`, so the classifier probes it first); the "a `true` is
/// not a count" LAW is this kernel's refusal below.
pub const RESTARTS_BOOL: u8 = 2;
/// `restarts` kind: a real int representable in i64 (value carried).
pub const RESTARTS_INT: u8 = 3;
/// `restarts` kind: a real int above `i64::MAX` (value NOT carried — the
/// representability fact; necessarily non-negative, so the sign law still
/// decides here).
pub const RESTARTS_INT_HUGE_POS: u8 = 4;
/// `restarts` kind: a real int below `i64::MIN` (necessarily negative).
pub const RESTARTS_INT_HUGE_NEG: u8 = 5;
/// `restarts` kind: any other value type (float, string, array, object).
pub const RESTARTS_OTHER: u8 = 6;

// ── shape-verdict codes ──────────────────────────────────────────────────────

/// The payload is a valid intent shape (Python constructs the dataclass).
pub const SHAPE_VALID: u8 = 0;
/// The root is not a mapping — includes the `None` every decode failure
/// folds to (the taxonomy-parity chokepoint).
pub const SHAPE_NOT_MAPPING: u8 = 1;
/// `symbols` violated the string-or-null law.
pub const SHAPE_BAD_SYMBOLS: u8 = 2;
/// `symbol` violated the string-or-null law.
pub const SHAPE_BAD_SYMBOL: u8 = 3;
/// `from_date` violated the string-or-null law.
pub const SHAPE_BAD_FROM_DATE: u8 = 4;
/// `to_date` violated the string-or-null law.
pub const SHAPE_BAD_TO_DATE: u8 = 5;
/// `scope` violated the string-or-null law (absent stays valid — the
/// pre-scope back-compat file must parse to the full chain, never a
/// "helpful" default token).
pub const SHAPE_BAD_SCOPE: u8 = 6;
/// `restarts` violated the non-negative-real-int law (bool pun included).
pub const SHAPE_BAD_RESTARTS: u8 = 7;
/// `created_at` violated the string-if-present law.
pub const SHAPE_BAD_CREATED_AT: u8 = 8;
/// An input code was outside its vocabulary — a classifier/wheel mismatch,
/// NEVER a verdict: the facade's refuse-poison gate maps it to the typed
/// `kernel_invalid` raise (fail-closed, never a served verdict).
pub const SHAPE_BAD_INPUT: u8 = 255;

// ── resume-decision arm tokens ───────────────────────────────────────────────

/// No readable intent — nothing died here.
pub const ARM_NO_INTENT: u8 = 0;
/// The single-flight flock is held — a peer pull is genuinely running.
pub const ARM_PEER_ACTIVE: u8 = 1;
/// `restarts >= max_restarts` — refuse, alert, keep the intent.
pub const ARM_OVER_CAP: u8 = 2;
/// Proceed to bump-then-launch (the effects stay Python).
pub const ARM_PROCEED: u8 = 3;

// ── refused-start restore arm tokens ─────────────────────────────────────────

/// A dry-run or a STARTED launch — nothing to undo.
pub const RESTORE_NO_ACTION: u8 = 0;
/// The on-disk intent is absent — the running winner's clear raced in;
/// "no intent" IS the correct durable record, restore nothing.
pub const RESTORE_LEAVE_ABSENT: u8 = 1;
/// No prior snapshot — clear the refused click's orphan clobber.
pub const RESTORE_CLEAR_ORPHAN: u8 = 2;
/// Re-write the prior snapshot VERBATIM (scope/restarts/created_at intact).
pub const RESTORE_RESTORE_PRIOR: u8 = 3;

/// `PullIntent.from_payload`'s strict shape lattice over classified codes.
///
/// Violations are reported FIRST-WINS in the oracle's own probe order —
/// the five request fields (`symbols`, `symbol`, `from_date`, `to_date`,
/// `scope`), then `restarts`, then `created_at` — so the verdict code can
/// compose a field-naming warn Python-side without re-deriving the lattice.
/// `restarts_value` may be non-zero ONLY under [`RESTARTS_INT`] (the
/// classifier contract; anything else is [`SHAPE_BAD_INPUT`]).
#[allow(clippy::too_many_arguments)] // 1:1 FFI mirror of the classified payload
#[must_use]
pub fn shape_verdict(
    root_mapping: bool,
    symbols: u8,
    symbol: u8,
    from_date: u8,
    to_date: u8,
    scope: u8,
    restarts_kind: u8,
    restarts_value: i64,
    created_at_kind: u8,
) -> u8 {
    // Input-vocabulary gate first: an out-of-vocabulary code can only mean
    // a classifier/wheel mismatch — refuse the CALL, never mint a verdict.
    let fields = [symbols, symbol, from_date, to_date, scope];
    if fields.iter().any(|&code| code > KIND_OTHER)
        || restarts_kind > RESTARTS_OTHER
        || created_at_kind > KIND_OTHER
        || (restarts_kind != RESTARTS_INT && restarts_value != 0)
    {
        return SHAPE_BAD_INPUT;
    }
    if !root_mapping {
        return SHAPE_NOT_MAPPING;
    }
    // The five request fields: absent ≡ null ≡ None, a string is a value,
    // anything else is corrupt — first violation wins, in probe order.
    for (index, &code) in fields.iter().enumerate() {
        if code == KIND_OTHER {
            #[allow(clippy::cast_possible_truncation)] // index < 5 by construction
            return SHAPE_BAD_SYMBOLS + index as u8;
        }
    }
    // `restarts`: a non-negative REAL int (a JSON `true` is NOT a count).
    let restarts_ok = match restarts_kind {
        RESTARTS_INT => restarts_value >= 0,
        RESTARTS_INT_HUGE_POS => true,
        _ => false,
    };
    if !restarts_ok {
        return SHAPE_BAD_RESTARTS;
    }
    // `created_at`: absent defaults to "" (valid); present must be a string.
    if !matches!(created_at_kind, KIND_ABSENT | KIND_STR) {
        return SHAPE_BAD_CREATED_AT;
    }
    SHAPE_VALID
}

/// `run_auto_resume`'s arm lattice — the load-bearing ORDER pinned:
/// no-intent → peer-probe → cap → proceed (a peer-active boot never burns
/// a restart token and never fails a fresh job's progress).
#[must_use]
pub fn resume_decision(
    has_intent: bool,
    probe_acquirable: bool,
    restarts: u64,
    max_restarts: u64,
) -> u8 {
    if !has_intent {
        return ARM_NO_INTENT;
    }
    if !probe_acquirable {
        return ARM_PEER_ACTIVE;
    }
    if restarts >= max_restarts {
        return ARM_OVER_CAP;
    }
    ARM_PROCEED
}

/// The `restore_intent_after_refused_start` lattice
/// (DEC-PULL-SCOPED-ALL-ONECLICK-001 refuter-r3 F-1 / r4 G-1).
#[must_use]
pub fn refused_restore_decision(
    dry_run: bool,
    started: bool,
    current_present: bool,
    prior_present: bool,
) -> u8 {
    if dry_run || started {
        return RESTORE_NO_ACTION;
    }
    if !current_present {
        return RESTORE_LEAVE_ABSENT;
    }
    if !prior_present {
        return RESTORE_CLEAR_ORPHAN;
    }
    RESTORE_RESTORE_PRIOR
}

/// `true` iff the character is whitespace to CPython's `str.strip()`.
///
/// CPython strips the `str.isspace()` set: Unicode categories Zs/Zl/Zp PLUS
/// bidirectional classes WS/B/S — a strict SUPERSET of the Unicode
/// `White_Space` property `str::trim` uses (U+001C..U+001F are bidi B/S,
/// NOT `White_Space`).  The set below is the exact enumeration measured on
/// the pinned CPython 3.12 (`[c for c in range(0x110000) if chr(c).isspace()]`
/// — 29 codepoints), drift-pinned by the golden env corpus + the seam's
/// real-wheel hypothesis differential.
#[must_use]
pub const fn is_python_space(c: char) -> bool {
    matches!(
        c,
        '\u{0009}'..='\u{000D}'   // TAB LF VT FF CR
        | '\u{001C}'..='\u{001F}' // FS GS RS US — bidi B/S, CPython-only
        | ' '
        | '\u{0085}'              // NEL
        | '\u{00A0}'              // NBSP
        | '\u{1680}'
        | '\u{2000}'..='\u{200A}'
        | '\u{2028}'
        | '\u{2029}'
        | '\u{202F}'
        | '\u{205F}'
        | '\u{3000}'
    )
}

/// The falsy token set (case-insensitive) that DISABLES the intent resume.
const FALSY: [&str; 4] = ["0", "false", "no", "off"];

/// `resolve_pull_intent_resume`'s env-token law — default ON; only an
/// explicit falsy token (stripped + lowercased) disables.
///
/// Strip = [`is_python_space`] (the CPython `str.strip` twin); lowercase =
/// `str::to_lowercase` (Unicode full lowercase, the `str.lower` twin — the
/// falsy tokens are pure ASCII, and no non-ASCII codepoint lowercases INTO
/// any of them, so UCD-version skew cannot flip membership; a fullwidth
/// `ｎｏ` stays fullwidth and stays truthy, exactly as CPython).  A raw
/// value carrying lone surrogates never reaches this kernel — the pyo3
/// `&str` edge refuses it and the facade's belt raises the typed
/// `PullStateKernelError` (fail-closed).
#[must_use]
pub fn resume_enabled(raw: Option<&str>) -> bool {
    let Some(raw) = raw else {
        return true;
    };
    let lowered = raw.trim_matches(is_python_space).to_lowercase();
    !FALSY.contains(&lowered.as_str())
}

#[cfg(test)]
mod tests {
    use super::*;

    const VALID_INPUT: (bool, u8, u8, u8, u8, u8, u8, i64, u8) = (
        true,
        KIND_STR,
        KIND_NULL,
        KIND_STR,
        KIND_STR,
        KIND_STR,
        RESTARTS_INT,
        1,
        KIND_STR,
    );

    fn verdict_of(input: (bool, u8, u8, u8, u8, u8, u8, i64, u8)) -> u8 {
        let (root, s, sym, f, t, sc, rk, rv, ck) = input;
        shape_verdict(root, s, sym, f, t, sc, rk, rv, ck)
    }

    #[test]
    fn valid_shape_passes() {
        assert_eq!(verdict_of(VALID_INPUT), SHAPE_VALID);
    }

    #[test]
    fn absent_and_null_request_fields_are_both_valid() {
        for code in [KIND_ABSENT, KIND_NULL, KIND_STR] {
            let mut input = VALID_INPUT;
            input.5 = code; // scope
            input.1 = code; // symbols
            assert_eq!(verdict_of(input), SHAPE_VALID, "code {code}");
        }
    }

    #[test]
    fn non_mapping_root_refuses_first() {
        // Even beside otherwise-valid field codes the root wins.
        let mut input = VALID_INPUT;
        input.0 = false;
        assert_eq!(verdict_of(input), SHAPE_NOT_MAPPING);
    }

    #[test]
    fn field_violations_report_first_wins_in_probe_order() {
        // symbols OTHER beats symbol OTHER beats scope OTHER…
        let mut input = VALID_INPUT;
        input.1 = KIND_OTHER;
        input.2 = KIND_OTHER;
        input.5 = KIND_OTHER;
        assert_eq!(verdict_of(input), SHAPE_BAD_SYMBOLS);
        input.1 = KIND_STR;
        assert_eq!(verdict_of(input), SHAPE_BAD_SYMBOL);
        input.2 = KIND_NULL;
        assert_eq!(verdict_of(input), SHAPE_BAD_SCOPE);
        // …and every single-field violation names ITS field.
        for (slot, expected) in [
            (1usize, SHAPE_BAD_SYMBOLS),
            (2, SHAPE_BAD_SYMBOL),
            (3, SHAPE_BAD_FROM_DATE),
            (4, SHAPE_BAD_TO_DATE),
            (5, SHAPE_BAD_SCOPE),
        ] {
            let mut single = VALID_INPUT;
            match slot {
                1 => single.1 = KIND_OTHER,
                2 => single.2 = KIND_OTHER,
                3 => single.3 = KIND_OTHER,
                4 => single.4 = KIND_OTHER,
                _ => single.5 = KIND_OTHER,
            }
            assert_eq!(verdict_of(single), expected);
        }
    }

    #[test]
    fn restarts_lattice() {
        let with_restarts = |kind: u8, value: i64| {
            let mut input = VALID_INPUT;
            input.6 = kind;
            input.7 = value;
            verdict_of(input)
        };
        assert_eq!(with_restarts(RESTARTS_INT, 0), SHAPE_VALID);
        assert_eq!(with_restarts(RESTARTS_INT, i64::MAX), SHAPE_VALID);
        assert_eq!(with_restarts(RESTARTS_INT, -1), SHAPE_BAD_RESTARTS);
        assert_eq!(with_restarts(RESTARTS_INT, i64::MIN), SHAPE_BAD_RESTARTS);
        // arbitrary-precision representability codes keep the sign law
        assert_eq!(with_restarts(RESTARTS_INT_HUGE_POS, 0), SHAPE_VALID);
        assert_eq!(with_restarts(RESTARTS_INT_HUGE_NEG, 0), SHAPE_BAD_RESTARTS);
        // a JSON `true` is NOT a count (the bool-pun refusal)
        assert_eq!(with_restarts(RESTARTS_BOOL, 0), SHAPE_BAD_RESTARTS);
        assert_eq!(with_restarts(RESTARTS_ABSENT, 0), SHAPE_BAD_RESTARTS);
        assert_eq!(with_restarts(RESTARTS_NULL, 0), SHAPE_BAD_RESTARTS);
        assert_eq!(with_restarts(RESTARTS_OTHER, 0), SHAPE_BAD_RESTARTS);
    }

    #[test]
    fn created_at_lattice() {
        let with_created = |kind: u8| {
            let mut input = VALID_INPUT;
            input.8 = kind;
            verdict_of(input)
        };
        assert_eq!(with_created(KIND_ABSENT), SHAPE_VALID); // defaults ""
        assert_eq!(with_created(KIND_STR), SHAPE_VALID);
        assert_eq!(with_created(KIND_NULL), SHAPE_BAD_CREATED_AT);
        assert_eq!(with_created(KIND_OTHER), SHAPE_BAD_CREATED_AT);
    }

    #[test]
    fn restarts_and_created_at_rank_after_fields_before_each_other() {
        let mut input = VALID_INPUT;
        input.6 = RESTARTS_BOOL;
        input.7 = 0;
        input.8 = KIND_OTHER;
        assert_eq!(verdict_of(input), SHAPE_BAD_RESTARTS);
        input.6 = RESTARTS_INT;
        assert_eq!(verdict_of(input), SHAPE_BAD_CREATED_AT);
    }

    #[test]
    fn out_of_vocabulary_inputs_refuse_the_call() {
        let mut input = VALID_INPUT;
        input.5 = 4; // field code out of vocabulary
        assert_eq!(verdict_of(input), SHAPE_BAD_INPUT);
        let mut input = VALID_INPUT;
        input.6 = 7; // restarts kind out of vocabulary
        assert_eq!(verdict_of(input), SHAPE_BAD_INPUT);
        let mut input = VALID_INPUT;
        input.8 = 9; // created_at kind out of vocabulary
        assert_eq!(verdict_of(input), SHAPE_BAD_INPUT);
        // a carried value under a non-INT kind is a classifier-contract break
        let mut input = VALID_INPUT;
        input.6 = RESTARTS_INT_HUGE_POS;
        input.7 = 5;
        assert_eq!(verdict_of(input), SHAPE_BAD_INPUT);
    }

    #[test]
    fn resume_order_is_intent_then_probe_then_cap() {
        // no intent wins over everything (probe must never be consulted —
        // the caller never even calls it; the lattice mirrors that)
        assert_eq!(resume_decision(false, false, 99, 0), ARM_NO_INTENT);
        assert_eq!(resume_decision(false, true, 0, 2), ARM_NO_INTENT);
        // peer-active BEFORE the cap: an over-cap intent behind a held flock
        // reads PEER, never OVER_CAP (no token burnt, no alert fired)
        assert_eq!(resume_decision(true, false, 99, 2), ARM_PEER_ACTIVE);
        assert_eq!(resume_decision(true, false, 0, 2), ARM_PEER_ACTIVE);
        // the cap law is >= (cap 0 refuses even a fresh intent)
        assert_eq!(resume_decision(true, true, 0, 0), ARM_OVER_CAP);
        assert_eq!(resume_decision(true, true, 2, 2), ARM_OVER_CAP);
        assert_eq!(resume_decision(true, true, u64::MAX, 2), ARM_OVER_CAP);
        assert_eq!(resume_decision(true, true, 1, 2), ARM_PROCEED);
        assert_eq!(resume_decision(true, true, 0, u64::MAX), ARM_PROCEED);
    }

    #[test]
    fn refused_restore_lattice() {
        // dry-run / started short-circuit everything
        assert_eq!(
            refused_restore_decision(true, false, true, true),
            RESTORE_NO_ACTION
        );
        assert_eq!(
            refused_restore_decision(false, true, false, false),
            RESTORE_NO_ACTION
        );
        assert_eq!(
            refused_restore_decision(true, true, true, false),
            RESTORE_NO_ACTION
        );
        // absent current = the winner's clear raced in — leave it absent
        assert_eq!(
            refused_restore_decision(false, false, false, true),
            RESTORE_LEAVE_ABSENT
        );
        assert_eq!(
            refused_restore_decision(false, false, false, false),
            RESTORE_LEAVE_ABSENT
        );
        // no prior snapshot = orphan clobber — clear it
        assert_eq!(
            refused_restore_decision(false, false, true, false),
            RESTORE_CLEAR_ORPHAN
        );
        // else restore the prior verbatim
        assert_eq!(
            refused_restore_decision(false, false, true, true),
            RESTORE_RESTORE_PRIOR
        );
    }

    #[test]
    fn resume_enabled_default_on_and_falsy_set() {
        assert!(resume_enabled(None));
        assert!(resume_enabled(Some("")));
        assert!(resume_enabled(Some("1")));
        assert!(resume_enabled(Some("junk")));
        assert!(resume_enabled(Some("00")));
        for falsy in ["0", "false", "no", "off", "FALSE", "No", " off ", "\t0\n"] {
            assert!(!resume_enabled(Some(falsy)), "{falsy:?}");
        }
    }

    #[test]
    fn resume_enabled_strips_the_cpython_isspace_superset() {
        // U+001C..U+001F: CPython-only whitespace (bidi B/S) — a bare
        // str::trim port would read these as truthy junk.
        assert!(!resume_enabled(Some("\u{1c}0")));
        assert!(!resume_enabled(Some("0\u{1d}")));
        assert!(!resume_enabled(Some("\u{1e}\u{1f}off\u{1c}")));
        assert!(!resume_enabled(Some("\u{85}0")));
        assert!(!resume_enabled(Some("\u{a0}no\u{a0}")));
        assert!(!resume_enabled(Some(" false\u{3000}")));
        // ZERO WIDTH SPACE is NOT whitespace to either side — stays truthy.
        assert!(resume_enabled(Some("\u{200b}0")));
        // fullwidth lowercases to fullwidth, never ASCII — stays truthy.
        assert!(resume_enabled(Some("ｎｏ")));
        assert!(resume_enabled(Some("ＯＦＦ")));
        // ligature ﬀ survives to_lowercase (casefold would decompose — and
        // CPython .lower() does not) — stays truthy.
        assert!(resume_enabled(Some("oﬀ")));
    }

    #[test]
    fn is_python_space_matches_the_measured_29_codepoint_set() {
        let expected: Vec<u32> = [
            0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x85, 0xA0, 0x1680, 0x2000,
            0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200A, 0x2028,
            0x2029, 0x202F, 0x205F, 0x3000,
        ]
        .to_vec();
        let mut got: Vec<u32> = Vec::new();
        for code in 0..=0x0011_0000_u32 {
            if let Some(c) = char::from_u32(code)
                && is_python_space(c)
            {
                got.push(code);
            }
        }
        assert_eq!(got, expected);
    }
}
