//! The auto-pull-scheduler TICK-DECISION + DURABLE-STATE-PARSE kernel
//! (Wave 3G, `DEC-RUST-PULL-STATE-001`).
//!
//! Re-hosts the two pure decision laws of
//! `brutex/dashboard/auto_pull_scheduler.py`
//! (DEC-PULL-SCOPED-ALL-ONECLICK-001's round-5 self-arming scheduler):
//!
//! * [`auto_tick_decision`] — `tick_once`'s decision LADDER, clockless:
//!   `disarmed → already_succeeded → already_attempted → gate → launcher
//!   bound → fire` over five caller-resolved booleans.  The ladder ORDER is
//!   the law (a both-marks state must read `already_succeeded`; a disarmed
//!   state must decide BEFORE any gate consult).  The Python seam exploits
//!   exactly that order for its two-stage consult: the PRE-GATE stage passes
//!   `gate_allowed=false, launcher_bound=false` placeholders (they cannot
//!   affect the earlier arms — pinned by the exhaustive `2**5` lattice
//!   below + the frozen golden), so the injected gate/launcher seams keep
//!   the oracle's LAZY evaluation order (a disarmed tick never consults the
//!   gate).  Fired-vs-refused is resolved AFTER the launcher call,
//!   Python-side (the launcher outcome is not a kernel input); every log
//!   line, `_last_outcome` string, state write and the per-(day, reason)
//!   log-dedup memo stay Python.
//! * [`auto_state_verdict`] — `_state_from_payload`'s validation lattice
//!   over MECHANICALLY-classified codes.  The JSON decode (CPython's whole
//!   failure taxonomy) and the READ layer (`read_sidecar_bytes` — the
//!   non-regular-inode / oversized / invalid-UTF-8 corruption posture of
//!   refuter rounds 6-7) deliberately STAY Python: every decode failure
//!   folds BEFORE any kernel consult, so the corrupt ⇒ DISARMED + one WARN
//!   posture is preserved by CONSTRUCTION.  The value law lives here:
//!   CPython's `schema == 1` is NUMERIC equality across bool/int/float
//!   (`true` and `1.0` legitimately PASS — the frozen golden pins both
//!   puns), `armed` must be EXACTLY a bool, and the optional strings refuse
//!   FIRST-WINS in the oracle's own constructor-arg order (`armed_at` →
//!   `last_attempt_date` → `last_success_date`).
//!
//! Codes only cross back; the ticker thread, store I/O, status strings and
//! the fail-LOUD `BRUTEX_AUTO_PULL_TICK_S` resolver stay Python permanently
//! (the design §3.3 endgame boundary).

use crate::intent::{KIND_ABSENT, KIND_NULL, KIND_OTHER, KIND_STR};

// ── tick-decision codes (ladder order == numeric order for the state arms) ──

/// The scheduler is disarmed — the tick is an O(1) no-op.
pub const TICK_DISARMED: u8 = 0;
/// Today already has a SUCCESSFUL run — nothing to do until rollover.
pub const TICK_ALREADY_SUCCEEDED: u8 = 1;
/// Today already burned its ONE started attempt (whatever its outcome).
pub const TICK_ALREADY_ATTEMPTED: u8 = 2;
/// The gate said not-now (Python logs the reason + detail, deduped per day).
pub const TICK_DEFERRED: u8 = 3;
/// No launcher bound (test/partial wiring) — refused, retried later.
pub const TICK_LAUNCHER_UNBOUND: u8 = 4;
/// FIRE: call the launcher.  Fired-vs-refused (and the started-marks /
/// refused-marks-nothing split) resolves AFTER the call, Python-side.
pub const TICK_FIRE: u8 = 5;

// ── state-parse schema-kind codes (the FFI input contract) ───────────────────

/// `schema` reads `None` — absent key AND JSON `null` alike (the oracle's
/// `payload.get("schema")` cannot distinguish them; neither can equal 1).
pub const SCHEMA_MISSING: u8 = 0;
/// `schema` is a JSON bool (value in the i64 slot: CPython `True == 1`).
pub const SCHEMA_BOOL: u8 = 1;
/// `schema` is an int representable in i64 (value in the i64 slot).
pub const SCHEMA_INT: u8 = 2;
/// `schema` is an int OUTSIDE i64 (can never equal 1 — value not carried).
pub const SCHEMA_INT_HUGE: u8 = 3;
/// `schema` is a float (IEEE-754 bits in the bits slot: `1.0 == 1` passes).
pub const SCHEMA_FLOAT: u8 = 4;
/// `schema` is any other JSON type (str/list/object — never equal to 1).
pub const SCHEMA_OTHER: u8 = 5;

// ── state-verdict codes ──────────────────────────────────────────────────────

/// The payload is a valid state shape (Python constructs `AutoPullState`).
pub const STATE_VALID: u8 = 0;
/// The root is not a JSON object.
pub const STATE_NOT_OBJECT: u8 = 1;
/// `schema` failed the `== 1` numeric-equality law.
pub const STATE_BAD_SCHEMA: u8 = 2;
/// `armed` is not EXACTLY a bool (an int `1` is refused — `isinstance`).
pub const STATE_ARMED_NOT_BOOL: u8 = 3;
/// `armed_at` violated the string-or-null law (first-wins order).
pub const STATE_BAD_ARMED_AT: u8 = 4;
/// `last_attempt_date` violated the string-or-null law.
pub const STATE_BAD_LAST_ATTEMPT: u8 = 5;
/// `last_success_date` violated the string-or-null law.
pub const STATE_BAD_LAST_SUCCESS: u8 = 6;
/// An input code was outside its vocabulary — a classifier/wheel mismatch,
/// NEVER a verdict: the facade's refuse-poison gate maps it to the typed
/// `kernel_invalid` raise (fail-closed, never a served verdict).
pub const STATE_BAD_INPUT: u8 = 255;

/// `tick_once`'s decision ladder — clockless, pure, TOTAL.
///
/// The five booleans are caller-resolved facts (`succeeded_today` /
/// `attempted_today` are the Python-side ISO-date comparisons — dates never
/// cross the FFI).  Ladder order is the LAW; the golden's exhaustive `2**5`
/// lattice pins every row against the frozen Python capture.
#[must_use]
pub fn auto_tick_decision(
    armed: bool,
    succeeded_today: bool,
    attempted_today: bool,
    gate_allowed: bool,
    launcher_bound: bool,
) -> u8 {
    if !armed {
        return TICK_DISARMED;
    }
    if succeeded_today {
        return TICK_ALREADY_SUCCEEDED;
    }
    if attempted_today {
        return TICK_ALREADY_ATTEMPTED;
    }
    if !gate_allowed {
        return TICK_DEFERRED;
    }
    if !launcher_bound {
        return TICK_LAUNCHER_UNBOUND;
    }
    TICK_FIRE
}

/// `_state_from_payload`'s validation lattice over classified codes.
///
/// Value slots are kind-gated (the classifier contract): `schema_i64` may be
/// non-zero ONLY under [`SCHEMA_BOOL`] / [`SCHEMA_INT`], `schema_f64_bits`
/// ONLY under [`SCHEMA_FLOAT`] — anything else is [`STATE_BAD_INPUT`].
#[allow(clippy::too_many_arguments)] // 1:1 FFI mirror of the classified payload
#[must_use]
pub fn auto_state_verdict(
    root_mapping: bool,
    schema_kind: u8,
    schema_i64: i64,
    schema_f64_bits: u64,
    armed_is_bool: bool,
    armed_at_kind: u8,
    last_attempt_kind: u8,
    last_success_kind: u8,
) -> u8 {
    // Input-vocabulary gate first: an out-of-vocabulary code can only mean
    // a classifier/wheel mismatch — refuse the CALL, never mint a verdict.
    let fields = [armed_at_kind, last_attempt_kind, last_success_kind];
    if schema_kind > SCHEMA_OTHER
        || fields.iter().any(|&code| code > KIND_OTHER)
        || (!matches!(schema_kind, SCHEMA_BOOL | SCHEMA_INT) && schema_i64 != 0)
        || (schema_kind != SCHEMA_FLOAT && schema_f64_bits != 0)
    {
        return STATE_BAD_INPUT;
    }
    if !root_mapping {
        return STATE_NOT_OBJECT;
    }
    // The `schema == AUTO_PULL_STATE_SCHEMA` law — CPython `==` is NUMERIC
    // across bool/int/float, so `true` and `1.0` pass alongside `1`; NaN,
    // out-of-i64 ints and every non-numeric type fail.
    let schema_ok = match schema_kind {
        SCHEMA_BOOL | SCHEMA_INT => schema_i64 == 1,
        SCHEMA_FLOAT => f64::from_bits(schema_f64_bits) == 1.0,
        _ => false,
    };
    if !schema_ok {
        return STATE_BAD_SCHEMA;
    }
    if !armed_is_bool {
        return STATE_ARMED_NOT_BOOL;
    }
    // The three optional strings: absent ≡ null ≡ None, a string is a
    // value, anything else is corrupt — first violation wins, in the
    // oracle's constructor-arg order.
    for (index, &code) in fields.iter().enumerate() {
        if !matches!(code, KIND_ABSENT | KIND_NULL | KIND_STR) {
            #[allow(clippy::cast_possible_truncation)] // index < 3 by construction
            return STATE_BAD_ARMED_AT + index as u8;
        }
    }
    STATE_VALID
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ladder_order_is_the_law() {
        // Disarmed decides FIRST — every later flag is irrelevant.
        assert_eq!(
            auto_tick_decision(false, true, true, true, true),
            TICK_DISARMED
        );
        // A both-marks state reads already_succeeded (order pin).
        assert_eq!(
            auto_tick_decision(true, true, true, true, true),
            TICK_ALREADY_SUCCEEDED
        );
        assert_eq!(
            auto_tick_decision(true, false, true, true, true),
            TICK_ALREADY_ATTEMPTED
        );
        assert_eq!(
            auto_tick_decision(true, false, false, false, true),
            TICK_DEFERRED
        );
        assert_eq!(
            auto_tick_decision(true, false, false, true, false),
            TICK_LAUNCHER_UNBOUND
        );
        assert_eq!(
            auto_tick_decision(true, false, false, true, true),
            TICK_FIRE
        );
    }

    #[test]
    fn pre_gate_placeholders_cannot_reach_the_late_arms() {
        // The seam's two-stage law: with `gate_allowed=false,
        // launcher_bound=false` placeholders the kernel can ONLY answer a
        // state arm (0/1/2) or DEFERRED (3 = "past the state checks") —
        // never UNBOUND/FIRE, so no placeholder can leak a late decision.
        for armed in [false, true] {
            for succ in [false, true] {
                for att in [false, true] {
                    let token = auto_tick_decision(armed, succ, att, false, false);
                    assert!(token <= TICK_DEFERRED, "placeholder leak: {token}");
                }
            }
        }
    }

    #[test]
    fn state_valid_shapes() {
        // int 1 · bool true · float 1.0 all pass the numeric-equality law.
        for (kind, i, bits) in [
            (SCHEMA_INT, 1_i64, 0_u64),
            (SCHEMA_BOOL, 1, 0),
            (SCHEMA_FLOAT, 0, 1.0_f64.to_bits()),
        ] {
            assert_eq!(
                auto_state_verdict(true, kind, i, bits, true, KIND_ABSENT, KIND_NULL, KIND_STR),
                STATE_VALID
            );
        }
    }

    #[test]
    fn state_refusal_ladder() {
        let ok = 1.0_f64.to_bits();
        assert_eq!(
            auto_state_verdict(false, SCHEMA_MISSING, 0, 0, false, 0, 0, 0),
            STATE_NOT_OBJECT
        );
        for (kind, i, bits) in [
            (SCHEMA_MISSING, 0_i64, 0_u64),
            (SCHEMA_INT, 2, 0),
            (SCHEMA_BOOL, 0, 0),
            (SCHEMA_INT_HUGE, 0, 0),
            (SCHEMA_FLOAT, 0, 0.5_f64.to_bits()),
            (SCHEMA_FLOAT, 0, f64::NAN.to_bits()),
            (SCHEMA_OTHER, 0, 0),
        ] {
            assert_eq!(
                auto_state_verdict(true, kind, i, bits, true, 0, 0, 0),
                STATE_BAD_SCHEMA,
                "kind {kind} i {i}"
            );
        }
        assert_eq!(
            auto_state_verdict(true, SCHEMA_FLOAT, 0, ok, false, 0, 0, 0),
            STATE_ARMED_NOT_BOOL
        );
        // First-wins field order: armed_at → last_attempt → last_success.
        assert_eq!(
            auto_state_verdict(
                true, SCHEMA_INT, 1, 0, true, KIND_OTHER, KIND_OTHER, KIND_OTHER
            ),
            STATE_BAD_ARMED_AT
        );
        assert_eq!(
            auto_state_verdict(
                true, SCHEMA_INT, 1, 0, true, KIND_STR, KIND_OTHER, KIND_OTHER
            ),
            STATE_BAD_LAST_ATTEMPT
        );
        assert_eq!(
            auto_state_verdict(
                true, SCHEMA_INT, 1, 0, true, KIND_STR, KIND_NULL, KIND_OTHER
            ),
            STATE_BAD_LAST_SUCCESS
        );
    }

    #[test]
    fn state_input_vocabulary_gate() {
        // Out-of-vocabulary codes + kind-gated value slots refuse the CALL.
        assert_eq!(
            auto_state_verdict(true, 6, 0, 0, true, 0, 0, 0),
            STATE_BAD_INPUT
        );
        assert_eq!(
            auto_state_verdict(true, SCHEMA_INT, 1, 0, true, 4, 0, 0),
            STATE_BAD_INPUT
        );
        assert_eq!(
            auto_state_verdict(true, SCHEMA_MISSING, 1, 0, true, 0, 0, 0),
            STATE_BAD_INPUT
        );
        assert_eq!(
            auto_state_verdict(true, SCHEMA_INT, 1, 1, true, 0, 0, 0),
            STATE_BAD_INPUT
        );
    }
}
