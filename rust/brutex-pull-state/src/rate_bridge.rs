//! The rate-bridge payload-VERDICT core — pure, decode-free (Wave 3H).
//!
//! Law (`brutex/data/rate_bridge.py::read_rate_snapshot`,
//! DEC-CLI-RATE-BRIDGE-001 + DEC-CLI-BRIDGE-PULL-ACTIVE-001), transcribed
//! from the post-decode verdict lattice:
//!
//! * root must be a JSON object (mapping) — else reject;
//! * `schema` must equal 3 under PYTHON numeric equality (`3.0 == 3` is
//!   True; `True == 3` is False; a string `"3"` is never numerically
//!   equal) — else reject;
//! * every one of the TEN required fields must be `float()`/`int()`
//!   COERCIBLE — else reject, naming the FIRST violating field in the
//!   oracle's constructor-argument probe order;
//! * `pull_active` NORMALISES bool-or-None (a genuine JSON bool is
//!   carried; everything else — absent, null, strings, ints — reads
//!   `None`); the kernel echoes the arm so the cross-language golden pins
//!   the law.
//!
//! BOUNDARY (the pull-intent decode-stays-Python precedent): the file
//! read, the JSON decode (CPython's WHOLE failure taxonomy) and the
//! `float()`/`int()` coercion PROBES stay Python — the seam's mechanical
//! classifier (`rate_bridge._classify_bridge_payload_for_kernel`) projects
//! concrete-type / coercibility FACTS into the small codes below, and a
//! payload whose coercion probe raises `OverflowError` (the documented
//! pre-existing raises envelope — a 400-digit int in a float field)
//! NEVER reaches this kernel (the Python classifier owns that raises
//! envelope itself — the raise propagates, this kernel is not consulted).
//! Codes cross in; ONE `(verdict, pull_active_arm)` pair crosses out;
//! every warn string and the `RateBridgeSnapshot` construction stay
//! Python (on a VALID verdict Python constructs the snapshot — the
//! pre-flip constructor's byte-identical retained code).

/// Verdict 0 — the payload decodes: Python constructs the snapshot.
pub const BRIDGE_VERDICT_VALID: u8 = 0;
/// Verdict 1 — the decoded root is not a JSON object.
pub const BRIDGE_VERDICT_NOT_MAPPING: u8 = 1;
/// Verdict 2 — the `schema` value is not (numerically) 3.
pub const BRIDGE_VERDICT_SCHEMA: u8 = 2;
/// Verdicts 3..=12 — `BRIDGE_VERDICT_FIELD_BASE + i` = required field *i*
/// (constructor probe order) is absent or uncoercible.
pub const BRIDGE_VERDICT_FIELD_BASE: u8 = 3;
/// The out-of-vocabulary-INPUT answer (a classifier/wheel mismatch) — the
/// facade's refuse-poison gate refuses it (the typed `kernel_invalid`
/// raise), never serves it.
pub const BRIDGE_VERDICT_BAD_INPUT: u8 = 255;

/// The number of required snapshot fields (the classifier's slot count).
pub const BRIDGE_FIELD_COUNT: usize = 10;

/// `schema` kind 0 — absent key OR JSON null (the `.get` default).
pub const SCHEMA_KIND_MISSING: u8 = 0;
/// `schema` kind 1 — a JSON bool (value carried as 0/1 — `bool` subclasses
/// `int` in CPython, so it compares numerically; probed first).
pub const SCHEMA_KIND_BOOL: u8 = 1;
/// `schema` kind 2 — an i64-representable int (value carried).
pub const SCHEMA_KIND_INT: u8 = 2;
/// `schema` kind 3 — an int outside i64 (can never equal 3).
pub const SCHEMA_KIND_INT_HUGE: u8 = 3;
/// `schema` kind 4 — a float (value carried; `3.0 == 3` is True).
pub const SCHEMA_KIND_FLOAT: u8 = 4;
/// `schema` kind 5 — any other JSON type (never numerically equal).
pub const SCHEMA_KIND_OTHER: u8 = 5;

/// Field kind 0 — absent (the pre-flip constructor's `KeyError` arm).
pub const FIELD_ABSENT: u8 = 0;
/// Field kind 1 — the CPython coercion probe succeeded.
pub const FIELD_OK: u8 = 1;
/// Field kind 2 — the probe raised `TypeError`/`ValueError`.
pub const FIELD_BAD: u8 = 2;

/// `pull_active` arm 0 — not a genuine bool: Python reads `None`.
pub const PA_NONE: u8 = 0;
/// `pull_active` arm 1 — JSON `false`.
pub const PA_FALSE: u8 = 1;
/// `pull_active` arm 2 — JSON `true`.
pub const PA_TRUE: u8 = 2;

/// The accepted bridge schema version (mirrors the Python module's private
/// `_SCHEMA_VERSION` — a version bump regenerates the golden + bumps this
/// constant in the SAME change, its own DEC).
const SCHEMA_VERSION: i64 = 3;

/// The schema-equality law — Python `data.get("schema") == 3`, by kind.
fn schema_accepted(kind: u8, int_value: i64, float_value: f64) -> bool {
    match kind {
        // bool compares as its int value (0/1 — never 3, but the law is
        // carried faithfully rather than special-cased).
        SCHEMA_KIND_BOOL | SCHEMA_KIND_INT => int_value == SCHEMA_VERSION,
        // f64 equality mirrors CPython float==int here exactly: 3 is
        // f64-exact, NaN compares false, 3.0 == 3 is true.
        #[allow(clippy::float_cmp)] // DELIBERATE: the oracle's `==` law
        SCHEMA_KIND_FLOAT => float_value == SCHEMA_VERSION as f64,
        _ => false,
    }
}

/// The whole payload verdict — `(verdict, pull_active_arm)`.
///
/// Total over every classified input; an out-of-vocabulary code (wrong
/// slot count, unknown kind) answers [`BRIDGE_VERDICT_BAD_INPUT`] so the
/// facade refuses loudly (the typed `kernel_invalid` raise) instead of
/// serving a guess.
#[must_use]
pub fn bridge_payload_verdict(
    root_mapping: bool,
    schema_kind: u8,
    schema_int: i64,
    schema_float: f64,
    field_kinds: &[u8],
    pa_kind: u8,
) -> (u8, u8) {
    let vocab_ok = field_kinds.len() == BRIDGE_FIELD_COUNT
        && field_kinds.iter().all(|k| *k <= FIELD_BAD)
        && schema_kind <= SCHEMA_KIND_OTHER
        && pa_kind <= PA_TRUE;
    if !vocab_ok {
        return (BRIDGE_VERDICT_BAD_INPUT, PA_NONE);
    }
    if !root_mapping {
        return (BRIDGE_VERDICT_NOT_MAPPING, pa_kind);
    }
    if !schema_accepted(schema_kind, schema_int, schema_float) {
        return (BRIDGE_VERDICT_SCHEMA, pa_kind);
    }
    for (index, kind) in field_kinds.iter().enumerate() {
        if *kind != FIELD_OK {
            // index < 10 by the vocabulary gate, so the cast never truncates.
            #[allow(clippy::cast_possible_truncation)]
            return (BRIDGE_VERDICT_FIELD_BASE + index as u8, pa_kind);
        }
    }
    (BRIDGE_VERDICT_VALID, pa_kind)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn all_ok() -> [u8; BRIDGE_FIELD_COUNT] {
        [FIELD_OK; BRIDGE_FIELD_COUNT]
    }

    #[test]
    fn valid_payload_serves_valid_and_echoes_pull_active() {
        for pa in [PA_NONE, PA_FALSE, PA_TRUE] {
            assert_eq!(
                bridge_payload_verdict(true, SCHEMA_KIND_INT, 3, 0.0, &all_ok(), pa),
                (BRIDGE_VERDICT_VALID, pa)
            );
        }
    }

    #[test]
    fn non_mapping_root_refuses_first() {
        // Even with an otherwise-broken slate the root refusal wins.
        assert_eq!(
            bridge_payload_verdict(
                false,
                SCHEMA_KIND_MISSING,
                0,
                0.0,
                &[FIELD_ABSENT; 10],
                PA_NONE
            ),
            (BRIDGE_VERDICT_NOT_MAPPING, PA_NONE)
        );
    }

    #[test]
    fn schema_equality_is_python_numeric_equality() {
        let ok = all_ok();
        let verdict = |kind, iv, fv| bridge_payload_verdict(true, kind, iv, fv, &ok, PA_NONE).0;
        assert_eq!(verdict(SCHEMA_KIND_INT, 3, 0.0), BRIDGE_VERDICT_VALID);
        assert_eq!(verdict(SCHEMA_KIND_FLOAT, 0, 3.0), BRIDGE_VERDICT_VALID);
        assert_eq!(verdict(SCHEMA_KIND_INT, 2, 0.0), BRIDGE_VERDICT_SCHEMA);
        assert_eq!(verdict(SCHEMA_KIND_FLOAT, 0, 3.5), BRIDGE_VERDICT_SCHEMA);
        assert_eq!(
            verdict(SCHEMA_KIND_FLOAT, 0, f64::NAN),
            BRIDGE_VERDICT_SCHEMA
        );
        assert_eq!(verdict(SCHEMA_KIND_BOOL, 1, 0.0), BRIDGE_VERDICT_SCHEMA); // True == 1
        assert_eq!(verdict(SCHEMA_KIND_INT_HUGE, 0, 0.0), BRIDGE_VERDICT_SCHEMA);
        assert_eq!(verdict(SCHEMA_KIND_MISSING, 0, 0.0), BRIDGE_VERDICT_SCHEMA);
        assert_eq!(verdict(SCHEMA_KIND_OTHER, 0, 0.0), BRIDGE_VERDICT_SCHEMA);
    }

    #[test]
    fn first_violating_field_named_in_probe_order() {
        for index in 0..BRIDGE_FIELD_COUNT {
            for bad in [FIELD_ABSENT, FIELD_BAD] {
                let mut kinds = all_ok();
                kinds[index] = bad;
                // A later violation must not outrank the first one.
                if index + 1 < BRIDGE_FIELD_COUNT {
                    kinds[index + 1] = FIELD_BAD;
                }
                let (verdict, pa) =
                    bridge_payload_verdict(true, SCHEMA_KIND_INT, 3, 0.0, &kinds, PA_TRUE);
                assert_eq!(verdict, BRIDGE_VERDICT_FIELD_BASE + index as u8);
                assert_eq!(pa, PA_TRUE);
            }
        }
    }

    #[test]
    fn out_of_vocabulary_inputs_answer_bad_input() {
        let ok = all_ok();
        // Wrong slot count.
        assert_eq!(
            bridge_payload_verdict(true, SCHEMA_KIND_INT, 3, 0.0, &ok[..9], PA_NONE).0,
            BRIDGE_VERDICT_BAD_INPUT
        );
        // Unknown field code.
        let mut kinds = all_ok();
        kinds[4] = 7;
        assert_eq!(
            bridge_payload_verdict(true, SCHEMA_KIND_INT, 3, 0.0, &kinds, PA_NONE).0,
            BRIDGE_VERDICT_BAD_INPUT
        );
        // Unknown schema kind / pull_active kind.
        assert_eq!(
            bridge_payload_verdict(true, 9, 3, 0.0, &ok, PA_NONE).0,
            BRIDGE_VERDICT_BAD_INPUT
        );
        assert_eq!(
            bridge_payload_verdict(true, SCHEMA_KIND_INT, 3, 0.0, &ok, 3).0,
            BRIDGE_VERDICT_BAD_INPUT
        );
    }
}
