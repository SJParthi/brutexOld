//! Laws D1 / D2 — the stage-stamp shape-order + verdict-matrix laws.
//!
//! Bit-for-bit mirrors of the Python oracles in
//! `brutex/observability/stage_stamp.py`:
//!
//! * **D1** — [`stamp_shape_index`]: the strict-parse acceptance-ORDER law
//!   (`_stamp_shape_index_python`). Python probes each shape check into a
//!   bool (isinstance over the parsed JSON tree — runtime introspection that
//!   cannot cross the FFI); Rust owns the acceptance ORDER + the first-reject
//!   decision; Python materializes the exact error message for the returned
//!   reject index (repr of arbitrary objects cannot be minted in Rust).
//! * **D2** — [`stamp_verdict`]: the `validate_stage_stamp` mismatch matrix
//!   (`_stamp_verdict_python`) — string equality + `Option` short-circuit
//!   arms, order-mirrored. Rust UTF-8 byte `==` ≡ Python codepoint `==` for
//!   every str PyO3 accepts (a lone-surrogate string fails conversion and
//!   degrades to the oracle facade-side).
//!
//! `json.loads` and `content_hash_of` deliberately STAY Python
//! (DEC-RUST-OBSERVABILITY-001 §6.4/§6.5 — the CPython-parse twin is the
//! named follow-up `DEC-RUST-OBSERVABILITY-STAMP-PARSE-003`; hashlib.sha256 is already
//! native and streaming a Path into Rust would violate the scalars-only FFI).

/// The D2 OK token (mirrors `StampVerdict.OK.value`).
pub const VERDICT_OK: &str = "ok";
/// The D2 mismatch token (mirrors `StampVerdict.HASH_MISMATCH.value`).
pub const VERDICT_HASH_MISMATCH: &str = "hash_mismatch";

/// Law D1 — the strict-parse acceptance-order law: first-false wins.
///
/// Index vocabulary (the frozen acceptance order of `_parse_stamp`'s raise
/// chain): `-1` accept · `0` root-not-dict · `1` stage · `2` run_id ·
/// `3` content_hash · `4` produced_at_utc · `5` extra.
pub fn stamp_shape_index(
    root_is_dict: bool,
    stage_is_str: bool,
    run_id_is_str: bool,
    hash_is_str: bool,
    produced_ok: bool,
    extra_ok: bool,
) -> i64 {
    let checks = [
        root_is_dict,
        stage_is_str,
        run_id_is_str,
        hash_is_str,
        produced_ok,
        extra_ok,
    ];
    for (index, ok) in checks.iter().enumerate() {
        if !ok {
            return index as i64;
        }
    }
    -1
}

/// Law D2 — the `validate_stage_stamp` verdict matrix over scalars.
///
/// The mismatch expression, order-mirrored from the Python oracle:
///
/// * `recomputed != content_hash` (torn/corrupt artifact);
/// * `stage != expected_stage` (swapped stamp);
/// * `expected_run_id` present AND `run_id` differs (stale stamp);
/// * BOTH snapshot sides present AND differ (G7 changed-lake refusal — a
///   stamp WITHOUT the extra is never failed on that axis; absent evidence
///   is never fabricated).
#[allow(clippy::too_many_arguments)]
pub fn stamp_verdict(
    recomputed: &str,
    content_hash: &str,
    stage: &str,
    expected_stage: &str,
    run_id: &str,
    expected_run_id: Option<&str>,
    stamped_snapshot: Option<&str>,
    expected_snapshot: Option<&str>,
) -> &'static str {
    let mismatch = recomputed != content_hash
        || stage != expected_stage
        || matches!(expected_run_id, Some(expected) if run_id != expected)
        || matches!(
            (expected_snapshot, stamped_snapshot),
            (Some(expected), Some(stamped)) if stamped != expected
        );
    if mismatch {
        VERDICT_HASH_MISMATCH
    } else {
        VERDICT_OK
    }
}
