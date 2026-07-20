//! slice 2 — the audit-chain / observability decision laws (DEC-RUST-SWEEP-OPS-001).
//!
//! Pure hash-chain, verification and verdict DECISION laws for the sweep
//! audit family, routed through `brutex/engine/sweep_ops_rust.py` behind the
//! default-OFF `BRUTEX_SWEEP_OPS_RUST` flag with the verbatim Python laws
//! retained as the LIVE parity oracles:
//!
//! * [`audit_entry_hash`] + [`chain_verify_step`] — the **BLAKE3** sweep
//!   chain (`brutex/engine/sweep_audit.py`). Python renders EVERY field
//!   string (`str(seq)`, the float-repr `str(ts_utc)`, the canonical payload
//!   JSON — the float-repr trap stays Python-side BY LAW); Rust only joins
//!   the already-rendered UTF-8 bytes with `b"|"` and hashes. The BLAKE3
//!   implementation is the official `blake3` crate — the same algorithm the
//!   pure-pip `blake3` package provides Python-side and the same crate
//!   `rust/fno-math` pins for `blake3_hash` (never a hand-rolled hash).
//!   NOTE: the ORCHESTRATOR audit chain (`brutex/orchestrator/audit.py`) is
//!   the DISTINCT SHA-256 family and is deliberately NOT in this crate —
//!   never conflate the two (CLAUDE.md Golden Rule #8 families).
//! * [`stamp_verdict`] — the `stage_stamp.validate_stage_stamp` mismatch
//!   decision (content hash / stage / run-id / the G7 both-sides-assert
//!   snapshot law). Artifact HASHING stays Python (streamed `hashlib`
//!   chunks — the bounded-memory law on multi-GB artifacts); only the
//!   compare DECISION routes.
//! * [`chain_link_state`] — the `chain_map._link_state` per-link priority
//!   ladder (rules 1-5 of the module's derivation law; the suspect amber
//!   floor is a trivial one-line transform that stays Python in
//!   `build_chain_map`).
//! * [`count_reconciliation`] — the `run_tripwires._reconcile_counts` D-9
//!   exact counter-identity violations fold (ints only — the detail string
//!   interpolates NO floats, so no repr trap).
//!
//! Every function is TOTAL (the crate builds with `panic = "abort"` — a
//! panic would abort the process, so fallible arms return `Err` and the
//! facade degrades to the oracle; arithmetic that could overflow is
//! checked).

/// The five chain links, in pipeline order — the Rust twin of
/// `brutex.observability.chain_map.LINKS` (mirror-pinned by the facade
/// suite; a drift latches at the pin, never silently).
pub const CHAIN_LINKS: [&str; 5] = ["preflight", "sweep", "stage3", "cascade", "artifacts"];

/// BLAKE3 entry hash over the sweep-audit 7-field `|`-joined byte layout.
///
/// The exact `SweepAuditEntry.compute_hash` law: every field arrives as its
/// ALREADY-RENDERED Python string (`str(seq)` · `prev_hash` · `str(ts_utc)`
/// · `actor` · `action` · `subject` · `canonical_json(payload)`), the
/// fields are joined with a single ASCII `|` and the BLAKE3-256 hex digest
/// (64 lowercase hex chars) is returned. UTF-8 `&str` bytes are identical
/// to Python's `.encode("utf-8")` by construction.
pub fn audit_entry_hash(
    seq: &str,
    prev_hash: &str,
    ts: &str,
    actor: &str,
    action: &str,
    subject: &str,
    payload_json: &str,
) -> String {
    let mut hasher = blake3::Hasher::new();
    let fields = [seq, prev_hash, ts, actor, action, subject, payload_json];
    for (index, field) in fields.iter().enumerate() {
        if index > 0 {
            hasher.update(b"|");
        }
        hasher.update(field.as_bytes());
    }
    hasher.finalize().to_hex().to_string()
}

/// One `verify_chain` step — the three per-entry checks, first failure wins.
///
/// Returns `(token, expected, actual)` where `token` is one of
/// `"seq_out_of_order"` / `"prev_hash_mismatch"` / `"entry_hash_mismatch"`
/// / `"ok"`; on `"ok"` `expected` carries the recomputed entry hash (which
/// equals the stored one) and `actual` is empty. Python owns the
/// `SweepAuditChainCorruption` construction + message bytes; the check
/// ORDER (seq → prev-hash linkage → recomputed content hash) is the frozen
/// `verify_chain` law. `expected_seq`/`seq` render via `to_string()`, which
/// equals CPython `str(int)` for every `i64` (an out-of-`i64` seq degrades
/// to the oracle at the PyO3 conversion, facade-side).
#[allow(clippy::too_many_arguments)] // The FFI tuple IS the entry's field list.
pub fn chain_verify_step(
    expected_seq: i64,
    prev_expected: &str,
    seq: i64,
    prev_hash: &str,
    ts: &str,
    actor: &str,
    action: &str,
    subject: &str,
    payload_json: &str,
    entry_hash: &str,
) -> (&'static str, String, String) {
    if seq != expected_seq {
        return (
            "seq_out_of_order",
            expected_seq.to_string(),
            seq.to_string(),
        );
    }
    if prev_hash != prev_expected {
        return (
            "prev_hash_mismatch",
            prev_expected.to_string(),
            prev_hash.to_string(),
        );
    }
    let recomputed = audit_entry_hash(
        &seq.to_string(),
        prev_hash,
        ts,
        actor,
        action,
        subject,
        payload_json,
    );
    if entry_hash != recomputed {
        return ("entry_hash_mismatch", recomputed, entry_hash.to_string());
    }
    ("ok", recomputed, String::new())
}

/// The `validate_stage_stamp` mismatch decision — `"ok"` / `"hash_mismatch"`.
///
/// The exact Python boolean law: ANY of recomputed-vs-stamped content hash,
/// stage, run-id (only when an expectation is given), or the
/// `data_snapshot_hash` extra (only when BOTH sides assert one — the G7
/// changed-lake refusal; absent evidence is never fabricated) mismatching
/// yields `"hash_mismatch"`. The MISSING_ARTIFACT / MISSING_STAMP /
/// MALFORMED verdicts are I/O-derived and decided Python-side BEFORE this
/// law runs.
#[allow(clippy::too_many_arguments)] // The FFI tuple IS the verdict's operand list.
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
        || expected_run_id.is_some_and(|expected| run_id != expected)
        || matches!(
            (expected_snapshot, stamped_snapshot),
            (Some(expected), Some(stamped)) if stamped != expected
        );
    if mismatch { "hash_mismatch" } else { "ok" }
}

/// Position of *link* in [`CHAIN_LINKS`], or `None` for a foreign name.
fn link_index(link: &str) -> Option<i64> {
    CHAIN_LINKS
        .iter()
        .position(|candidate| *candidate == link)
        .map(|index| index as i64)
}

/// The `chain_map._link_state` per-link priority ladder (rules 1-5).
///
/// Inputs are the primitives the Python wrapper extracts ONCE (verdict
/// token · lowered terminal · refusal/error strings); the output is
/// `(state_token, detail)` with `state_token` one of the five `LinkState`
/// values. `Err` only when an arm NEEDS `last_stage`'s pipeline index and
/// the name is not a chain link (the facade refuses such an input anyway —
/// this is the total-function belt; the crate must never panic under
/// `panic = "abort"`). Detail strings mirror the Python f-strings byte for
/// byte, including the empty-refusal and empty-error-type falsy laws
/// (`or ""` / `or "unknown error"` — an EMPTY string falls back too).
#[allow(clippy::too_many_arguments)] // The FFI tuple IS the ladder's operand list.
pub fn chain_link_state(
    link: &str,
    idx: i64,
    verdict: Option<&str>,
    running: bool,
    last_stage: Option<&str>,
    terminal: Option<&str>,
    refusal_text: Option<&str>,
    error_type: Option<&str>,
) -> Result<(&'static str, String), String> {
    if let Some(token @ ("hash_mismatch" | "malformed")) = verdict {
        return Ok(("red", format!("stamp {token}")));
    }
    if running && Some(link) == last_stage {
        return Ok(("running", "in progress".to_string()));
    }
    if verdict == Some("ok") {
        return Ok(("green", "stamp valid".to_string()));
    }
    if running {
        let Some(stage) = last_stage else {
            return Ok(("amber", "running; stage unknown".to_string()));
        };
        let stage_idx =
            link_index(stage).ok_or_else(|| format!("last_stage {stage:?} is not a chain link"))?;
        if idx < stage_idx {
            return Ok(("green", "completed (inferred)".to_string()));
        }
        return Ok(("pending", "not started".to_string()));
    }
    match terminal {
        Some("complete") | Some("suspect_complete") => Ok(("green", "run complete".to_string())),
        Some("refused") => {
            if link == "preflight" {
                // The Python law's `or ""`: None AND "" both render empty.
                let refusal = refusal_text.unwrap_or("");
                return Ok(("red", format!("refused: {refusal}")));
            }
            Ok(("pending", "not started".to_string()))
        }
        Some(dead @ ("failed" | "halted")) => {
            let Some(stage) = last_stage else {
                return Ok(("amber", format!("run {dead}; stage unknown")));
            };
            let stage_idx = link_index(stage)
                .ok_or_else(|| format!("last_stage {stage:?} is not a chain link"))?;
            if idx < stage_idx {
                return Ok(("green", "completed (inferred)".to_string()));
            }
            if link == stage {
                // The Python law's `or "unknown error"`: None AND "" fall back.
                let error = match error_type {
                    Some(text) if !text.is_empty() => text,
                    _ => "unknown error",
                };
                return Ok(("red", format!("run {dead} at this stage ({error})")));
            }
            Ok(("pending", "not started".to_string()))
        }
        _ => Ok(("pending", "no evidence".to_string())),
    }
}

/// The `run_tripwires._reconcile_counts` D-9 violations fold.
///
/// Returns the `"; "`-joined violation detail string — EMPTY means every
/// counter identity holds (the facade maps empty → the clear arm). Clause
/// ORDER and message bytes mirror the Python law exactly (ints only — no
/// float repr crosses). `Err` only on the one arithmetic that could
/// overflow `i64` (`evaluated + in_flight` — CPython's bigints cannot
/// overflow there, so the facade degrades that pathological input to the
/// oracle rather than aborting under `panic = "abort"`).
#[allow(clippy::too_many_arguments)] // The FFI tuple IS the counter list.
pub fn count_reconciliation(
    total: i64,
    enumerated: i64,
    evaluated: i64,
    skipped: i64,
    survivors: i64,
    in_flight: i64,
    prev_evaluated: Option<i64>,
    is_done: bool,
) -> Result<String, String> {
    let mut violations: Vec<String> = Vec::new();
    if in_flight < 0 {
        violations.push(format!("in_flight {in_flight} < 0"));
    }
    if enumerated != total {
        violations.push(format!(
            "snapshot enumerated {enumerated} != armed total {total}"
        ));
    }
    if skipped > evaluated {
        violations.push(format!(
            "skipped {skipped} > evaluated {evaluated} (skipped ⊆ evaluated)"
        ));
    }
    if survivors > evaluated {
        violations.push(format!("survivors {survivors} > evaluated {evaluated}"));
    }
    let issued = evaluated
        .checked_add(in_flight)
        .ok_or_else(|| "evaluated + in_flight overflows i64".to_string())?;
    if issued > total {
        violations.push(format!(
            "evaluated {evaluated} + in_flight {in_flight} > enumerated {total}"
        ));
    }
    if let Some(prev) = prev_evaluated
        && evaluated < prev
    {
        violations.push(format!("evaluated went backwards: {evaluated} < {prev}"));
    }
    if is_done && evaluated != total {
        violations.push(format!(
            "run done but evaluated {evaluated} != enumerated {total}"
        ));
    }
    Ok(violations.join("; "))
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── audit_entry_hash — the cross-language golden literals ─────────
    // The SAME literals are pinned Python-side (pip blake3) in
    // tests/integration/test_sweep_ops_rust_parity.py — the shared-literal
    // cross-language pin pattern.

    const GENESIS: &str = "0000000000000000000000000000000000000000000000000000000000000000";
    const G1: &str = "ab1df54e0d8428288ece95f9b5e462f4af17beb3046ea429c6ee86d67e33ff2e";
    const G2: &str = "4966787250e36bea06d71232fdb6e1087ed6b1757f1fa454152b7be989e087a7";
    const G3: &str = "7fb88619e479fc3cf60860936a745c9a3ec67e3aef6bcbc1d57ff410202563b8";

    fn g1_hash() -> String {
        audit_entry_hash(
            "0",
            GENESIS,
            "1700000000.5",
            "sweep",
            "SWEEP_START",
            "sweep:abc",
            "{\"total_combos\":22100}",
        )
    }

    #[test]
    fn entry_hash_matches_python_blake3_golden() {
        assert_eq!(g1_hash(), G1);
    }

    #[test]
    fn entry_hash_chains_prev_hash_golden() {
        let second = audit_entry_hash(
            "1",
            G1,
            "1700000001.25",
            "sweep",
            "COMBO_DONE",
            "combo:5e",
            "{\"mask\":7,\"trades\":42}",
        );
        assert_eq!(second, G2);
    }

    #[test]
    fn entry_hash_non_ascii_fields_hash_as_utf8_golden() {
        let hash = audit_entry_hash(
            "7",
            &"f".repeat(64),
            "123.5",
            "aktör",
            "ACT",
            "subj-ß",
            "{\"k\":\"v–\"}",
        );
        assert_eq!(hash, G3);
    }

    #[test]
    fn entry_hash_is_64_lowercase_hex() {
        let hash = g1_hash();
        assert_eq!(hash.len(), 64);
        assert!(
            hash.chars()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        );
    }

    #[test]
    fn entry_hash_field_separator_is_load_bearing() {
        // Moving one byte across the `|` boundary must change the hash —
        // the join is positional, never a plain concat.
        let a = audit_entry_hash("1", "ab", "1.0", "x", "y", "z", "{}");
        let b = audit_entry_hash("1a", "b", "1.0", "x", "y", "z", "{}");
        assert_ne!(a, b);
    }

    // ── chain_verify_step ─────────────────────────────────────────────

    #[test]
    fn verify_step_ok_returns_recomputed_hash() {
        let (token, expected, actual) = chain_verify_step(
            0,
            GENESIS,
            0,
            GENESIS,
            "1700000000.5",
            "sweep",
            "SWEEP_START",
            "sweep:abc",
            "{\"total_combos\":22100}",
            G1,
        );
        assert_eq!(token, "ok");
        assert_eq!(expected, G1);
        assert_eq!(actual, "");
    }

    #[test]
    fn verify_step_seq_out_of_order_first() {
        // Seq wins over BOTH a prev-hash and a content mismatch (check order).
        let (token, expected, actual) =
            chain_verify_step(2, "zz", 5, "yy", "1.0", "a", "B", "c", "{}", "bad");
        assert_eq!(token, "seq_out_of_order");
        assert_eq!(expected, "2");
        assert_eq!(actual, "5");
    }

    #[test]
    fn verify_step_prev_hash_mismatch_before_content() {
        let (token, expected, actual) = chain_verify_step(
            0,
            GENESIS,
            0,
            "not-genesis",
            "1.0",
            "a",
            "B",
            "c",
            "{}",
            "bad",
        );
        assert_eq!(token, "prev_hash_mismatch");
        assert_eq!(expected, GENESIS);
        assert_eq!(actual, "not-genesis");
    }

    #[test]
    fn verify_step_entry_hash_mismatch_carries_recomputed() {
        let (token, expected, actual) = chain_verify_step(
            0,
            GENESIS,
            0,
            GENESIS,
            "1700000000.5",
            "sweep",
            "SWEEP_START",
            "sweep:abc",
            "{\"total_combos\":22100}",
            "tampered",
        );
        assert_eq!(token, "entry_hash_mismatch");
        assert_eq!(expected, G1);
        assert_eq!(actual, "tampered");
    }

    #[test]
    fn verify_step_negative_seq_renders_like_python_str() {
        let (token, expected, actual) =
            chain_verify_step(-3, "p", -7, "p", "1.0", "a", "B", "c", "{}", "h");
        assert_eq!(token, "seq_out_of_order");
        assert_eq!(expected, "-3");
        assert_eq!(actual, "-7");
    }

    // ── stamp_verdict ─────────────────────────────────────────────────

    #[test]
    fn stamp_verdict_clean_is_ok() {
        assert_eq!(
            stamp_verdict("h", "h", "s", "s", "r", Some("r"), Some("d"), Some("d")),
            "ok"
        );
    }

    #[test]
    fn stamp_verdict_content_hash_mismatch() {
        assert_eq!(
            stamp_verdict("h1", "h2", "s", "s", "r", None, None, None),
            "hash_mismatch"
        );
    }

    #[test]
    fn stamp_verdict_stage_mismatch() {
        assert_eq!(
            stamp_verdict(
                "h",
                "h",
                "cascade_manifest",
                "stage3_cell",
                "r",
                None,
                None,
                None
            ),
            "hash_mismatch"
        );
    }

    #[test]
    fn stamp_verdict_run_id_checked_only_when_expected() {
        assert_eq!(
            stamp_verdict("h", "h", "s", "s", "r1", None, None, None),
            "ok"
        );
        assert_eq!(
            stamp_verdict("h", "h", "s", "s", "r1", Some("r2"), None, None),
            "hash_mismatch"
        );
    }

    #[test]
    fn stamp_verdict_snapshot_needs_both_sides() {
        // The G7 law: only BOTH-sides-assert compares; one-sided never fails.
        assert_eq!(
            stamp_verdict("h", "h", "s", "s", "r", None, Some("d1"), None),
            "ok"
        );
        assert_eq!(
            stamp_verdict("h", "h", "s", "s", "r", None, None, Some("d2")),
            "ok"
        );
        assert_eq!(
            stamp_verdict("h", "h", "s", "s", "r", None, Some("d1"), Some("d2")),
            "hash_mismatch"
        );
    }

    // ── chain_link_state ──────────────────────────────────────────────

    fn state(
        link: &str,
        idx: i64,
        verdict: Option<&str>,
        running: bool,
        last_stage: Option<&str>,
        terminal: Option<&str>,
    ) -> (String, String) {
        let (token, detail) = chain_link_state(
            link, idx, verdict, running, last_stage, terminal, None, None,
        )
        .expect("total over chain-link inputs");
        (token.to_string(), detail)
    }

    #[test]
    fn link_state_red_stamp_wins_over_everything() {
        assert_eq!(
            state(
                "sweep",
                1,
                Some("hash_mismatch"),
                true,
                Some("sweep"),
                Some("complete")
            ),
            ("red".to_string(), "stamp hash_mismatch".to_string())
        );
        assert_eq!(
            state("sweep", 1, Some("malformed"), false, None, None),
            ("red".to_string(), "stamp malformed".to_string())
        );
    }

    #[test]
    fn link_state_running_current_stage() {
        assert_eq!(
            state("stage3", 2, None, true, Some("stage3"), None),
            ("running".to_string(), "in progress".to_string())
        );
    }

    #[test]
    fn link_state_green_stamp_valid() {
        assert_eq!(
            state("sweep", 1, Some("ok"), true, Some("cascade"), None),
            ("green".to_string(), "stamp valid".to_string())
        );
    }

    #[test]
    fn link_state_running_before_and_after() {
        assert_eq!(
            state("preflight", 0, None, true, Some("stage3"), None),
            ("green".to_string(), "completed (inferred)".to_string())
        );
        assert_eq!(
            state("cascade", 3, None, true, Some("stage3"), None),
            ("pending".to_string(), "not started".to_string())
        );
        assert_eq!(
            state("cascade", 3, None, true, None, None),
            ("amber".to_string(), "running; stage unknown".to_string())
        );
    }

    #[test]
    fn link_state_terminal_arms() {
        assert_eq!(
            state("artifacts", 4, None, false, None, Some("complete")),
            ("green".to_string(), "run complete".to_string())
        );
        assert_eq!(
            state("artifacts", 4, None, false, None, Some("suspect_complete")),
            ("green".to_string(), "run complete".to_string())
        );
        assert_eq!(
            state("sweep", 1, None, false, None, Some("refused")),
            ("pending".to_string(), "not started".to_string())
        );
        assert_eq!(
            state("preflight", 0, None, false, None, None),
            ("pending".to_string(), "no evidence".to_string())
        );
    }

    #[test]
    fn link_state_refused_preflight_renders_refusal() {
        let (token, detail) = chain_link_state(
            "preflight",
            0,
            None,
            false,
            None,
            Some("refused"),
            Some("too big"),
            None,
        )
        .expect("total");
        assert_eq!(token, "red");
        assert_eq!(detail, "refused: too big");
        // None AND "" both render empty (the Python `or ""` falsy law).
        let (_, empty_detail) = chain_link_state(
            "preflight",
            0,
            None,
            false,
            None,
            Some("refused"),
            None,
            None,
        )
        .expect("total");
        assert_eq!(empty_detail, "refused: ");
        let (_, blank_detail) = chain_link_state(
            "preflight",
            0,
            None,
            false,
            None,
            Some("refused"),
            Some(""),
            None,
        )
        .expect("total");
        assert_eq!(blank_detail, "refused: ");
    }

    #[test]
    fn link_state_dead_arms() {
        assert_eq!(
            state("preflight", 0, None, false, Some("stage3"), Some("failed")),
            ("green".to_string(), "completed (inferred)".to_string())
        );
        assert_eq!(
            state("cascade", 3, None, false, Some("stage3"), Some("halted")),
            ("pending".to_string(), "not started".to_string())
        );
        assert_eq!(
            state("sweep", 1, None, false, None, Some("failed")),
            ("amber".to_string(), "run failed; stage unknown".to_string())
        );
        let (token, detail) = chain_link_state(
            "stage3",
            2,
            None,
            false,
            Some("stage3"),
            Some("halted"),
            None,
            Some("OSError"),
        )
        .expect("total");
        assert_eq!(token, "red");
        assert_eq!(detail, "run halted at this stage (OSError)");
        // None AND "" error types both fall back (the Python falsy law).
        let (_, unknown_detail) = chain_link_state(
            "stage3",
            2,
            None,
            false,
            Some("stage3"),
            Some("failed"),
            None,
            Some(""),
        )
        .expect("total");
        assert_eq!(unknown_detail, "run failed at this stage (unknown error)");
    }

    #[test]
    fn link_state_foreign_last_stage_errs_when_index_needed() {
        assert!(chain_link_state("sweep", 1, None, true, Some("nope"), None, None, None).is_err());
        assert!(
            chain_link_state(
                "sweep",
                1,
                None,
                false,
                Some("nope"),
                Some("failed"),
                None,
                None
            )
            .is_err()
        );
        // …but arms that never need the index stay total.
        assert!(chain_link_state("nope", 1, None, true, Some("nope"), None, None, None).is_ok());
    }

    // ── count_reconciliation ──────────────────────────────────────────

    #[test]
    fn count_reconciliation_clean_is_empty() {
        let detail = count_reconciliation(10, 10, 4, 1, 2, 3, Some(3), false).expect("no overflow");
        assert_eq!(detail, "");
    }

    #[test]
    fn count_reconciliation_all_clauses_in_python_order() {
        let detail = count_reconciliation(10, 11, 4, 5, 6, -1, Some(9), true).expect("no overflow");
        assert_eq!(
            detail,
            "in_flight -1 < 0; \
             snapshot enumerated 11 != armed total 10; \
             skipped 5 > evaluated 4 (skipped ⊆ evaluated); \
             survivors 6 > evaluated 4; \
             evaluated went backwards: 4 < 9; \
             run done but evaluated 4 != enumerated 10"
        );
    }

    #[test]
    fn count_reconciliation_issued_over_total() {
        let detail = count_reconciliation(10, 10, 8, 0, 0, 5, None, false).expect("no overflow");
        assert_eq!(detail, "evaluated 8 + in_flight 5 > enumerated 10");
    }

    #[test]
    fn count_reconciliation_first_sample_skips_backwards_clause() {
        let detail = count_reconciliation(10, 10, 4, 0, 0, 0, None, false).expect("no overflow");
        assert_eq!(detail, "");
    }

    #[test]
    fn count_reconciliation_overflow_errs_never_panics() {
        assert!(count_reconciliation(1, 1, i64::MAX, 0, 0, 1, None, false).is_err());
    }
}
