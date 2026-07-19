//! lake_keys — the spot-key drift-recovery candidate-ladder builders.
//!
//! ASCII-only Rust mirror of `brutex/data/lake_keys.py`'s three builders
//! (`stock_drift_fold` / `spot_key_candidates` / `spot_recovery_candidates`
//! core); DEC-DATA-RUST-PURELAW-002 (MIGRATION.md #1952, ADR-0020 slice T10
//! PR-2). Consumed ONLY through the default-OFF facade
//! `brutex/data/lake_keys_rust.py`, which pre-gates every input on
//! `str.isascii()` — non-ASCII input never reaches this module in
//! production (the facade returns the Python oracle's value directly).
//!
//! The module-docstring pledge of the Python oracle is carried verbatim:
//! "no case-folding, no `&`-encoding recovery; a genuinely unknown stray
//! STAYS unresolved" — this module contains NO decode/replace/percent arm
//! of any kind (the `%26`→`&` NO-recovery pledge, #1482).
//!
//! TWO DISTINCT whitespace vocabularies live in this module (a spec row of
//! its own — conflating them is the likeliest implementation bug):
//! * the FOLD removes ONLY ASCII space `0x20` (interior AND edge; a
//!   tab-bearing dir is junk → unverified, the oracle docstring pledge);
//! * the STRIP (laws B/C) removes the 10-byte CPython `str.strip()` ASCII
//!   set at the EDGES only ([`PY_STRIP_BYTES`]).

/// The 10-byte ASCII strip set of CPython `str.strip()` (probed 2026-07-17,
/// recorded in the design §2.4): TAB LF VT FF CR FS GS RS US SP.
///
/// Rust `str::trim()` is Unicode `White_Space`, which EXCLUDES the
/// separator controls `0x1C..=0x1F` — a naive `trim()` mirror silently
/// diverges on separator-control-wrapped dir names, which are REAL heal
/// inputs (`detect_legacy_drift` feeds on-disk dir names straight into
/// law B). NEVER use `trim()` in this module.
const PY_STRIP_BYTES: [u8; 10] = [0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x1F, 0x20];

/// True iff *b* is a member of the CPython ASCII strip set.
fn is_py_strip_byte(b: u8) -> bool {
    PY_STRIP_BYTES.contains(&b)
}

/// The CPython `str.strip()` mirror over [`PY_STRIP_BYTES`] — trims BOTH
/// ends, interior bytes untouched. Every set member is a single ASCII
/// byte, so the returned slice can never split a UTF-8 sequence (pinned
/// by the never-panics proptest over full-Unicode strings even though the
/// facade gates non-ASCII away).
fn py_strip(s: &str) -> &str {
    let bytes = s.as_bytes();
    let mut start = 0usize;
    while start < bytes.len() && is_py_strip_byte(bytes[start]) {
        start += 1;
    }
    let mut end = bytes.len();
    while end > start && is_py_strip_byte(bytes[end - 1]) {
        end -= 1;
    }
    &s[start..end]
}

/// Byte-wise ASCII `a-z` → `A-Z` map; every other char is identity.
///
/// For the facade-guaranteed ASCII domain this is EXACTLY CPython
/// `str.upper()`. On non-ASCII input (reachable only from cargo tests /
/// proptests, never production) it deliberately does NOT apply Unicode
/// case maps — `"ß"` stays `"ß"` (CPython maps it to `"SS"`), which is
/// exactly why the facade's ASCII pre-gate is load-bearing.
fn ascii_upper(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c.is_ascii_lowercase() {
                c.to_ascii_uppercase()
            } else {
                c
            }
        })
        .collect()
}

/// Law A — the irreversible-normalisation fold used ONLY for evidence
/// matching (`lake_keys.py:66-86`): `symbol.upper().replace(" ",
/// "").replace("-", "")` transcribed token-for-token (A1 upper → A2 drop
/// every `0x20` → A3 drop every `0x2D`; for ASCII the ops commute, but the
/// mirror stays order-faithful — the PR-1 backtracking-spec discipline).
///
/// ASCII SPACE ONLY is folded (interior AND edge); TAB/LF/`0x1C`-class are
/// deliberately NOT folded (the oracle docstring pledge — "a tab-bearing
/// dir is junk → unverified"). Empty in → empty out (a LEGAL output — the
/// facade refuse gate must not demand non-empty). NO `%26`→`&` arm.
pub fn stock_drift_fold(symbol: &str) -> String {
    let upper = ascii_upper(symbol); // A1
    let no_space: String = upper.chars().filter(|&c| c != ' ').collect(); // A2
    no_space.chars().filter(|&c| c != '-').collect() // A3
}

/// Law B — the ordered #911 drift-recovery candidate keys for *symbol*,
/// EXCLUDING the identity (`lake_keys.py:89-126`, body :115-126).
///
/// Ladder order: (1) the strip-set-stripped form — added ONLY when the
/// strip changed something AND left something; (2) the row's OWN
/// `"<exchange>-"` prefix stripped off the STRIPPED form (the exchange is
/// itself stripped first; a blank exchange skips the rung; the
/// pathological `"<exchange>-"` dir yields `""` and is DROPPED). Max 2
/// entries; identity excluded BY CONSTRUCTION (rung 1 only when it
/// differs from the RAW *symbol*; rung 2 is strictly shorter than the
/// stripped form).
pub fn spot_key_candidates(symbol: &str, exchange: &str) -> Vec<String> {
    let mut candidates: Vec<String> = Vec::new(); // B1
    let stripped = py_strip(symbol); // B2
    if stripped != symbol && !stripped.is_empty() {
        candidates.push(stripped.to_string()); // B3
    }
    let exch = py_strip(exchange); // B4
    if !exch.is_empty() {
        let prefix = format!("{exch}-"); // B5
        if stripped.starts_with(&prefix) {
            // B6 — the prefix applies to the STRIPPED form even when B3
            // added nothing (a clean "NSE-M&M" still yields ("M&M",)).
            // Slicing at prefix.len() is safe: starts_with matched the
            // prefix byte-for-byte, so the index is a char boundary.
            let candidate = &stripped[prefix.len()..]; // B7
            if !candidate.is_empty() {
                candidates.push(candidate.to_string());
            }
        }
    }
    candidates // B8
}

/// Law C — the `spot_recovery_candidates` CORE (`lake_keys.py:224-272`,
/// body :252-272) with `alias_pairs` + `drift_bucket` INJECTED: the
/// Python public wrapper computes `fno_index_alias_pairs()` (dict
/// insertion order becomes list order — order-preserving carriage is
/// load-bearing) and probes the `stock_drift` mapping ONCE
/// (`tuple(stock_drift.get((exchange, fold), ()))`); no Mapping ever
/// crosses the FFI — the ONE sanctioned surgery (burn-in compares LAWS,
/// not I/O).
///
/// Rung order: alias pairs (per pair: reverse C4 THEN forward C5, both
/// checks every pair — no else) → the RAW-args `"{exchange}-{underlying}"`
/// C6 (UNCONDITIONAL — unstripped args verbatim; a blank exchange still
/// adds `"-<underlying>"`) → the law-B rungs on *underlying* itself → the
/// bucket entries LAST (empty bucket ⇒ byte-identical to the pre-stock
/// ladder — the pinned prefix invariant). The `_add` law: identity compare
/// against the RAW unstripped *underlying* param; first-occurrence dedup
/// (order-preserving membership scan).
///
/// The C4/C5 compare ASYMMETRY is transcribed verbatim: `fno_key` IS
/// uppercased at compare (C4) while `spot_key` is NOT (C5) — env alias
/// KEYS are already `.strip().upper()`-ed at Python parse time, env VALUES
/// are stripped but NOT uppercased, so a lowercase env VALUE matches on C4
/// and can be EMITTED lowercase by C5's mirror direction.
pub fn spot_recovery_candidates(
    underlying: &str,
    exchange: &str,
    alias_pairs: &[(String, String)],
    drift_bucket: &[String],
) -> Vec<String> {
    // C1 — `_add`: identity compare against the RAW `underlying` param;
    // first occurrence wins (linear membership scan, order-preserving).
    fn add(candidates: &mut Vec<String>, underlying: &str, key: &str) {
        if key != underlying && !candidates.iter().any(|c| c.as_str() == key) {
            candidates.push(key.to_string());
        }
    }
    let mut candidates: Vec<String> = Vec::new();
    let upper = ascii_upper(py_strip(underlying)); // C2 — strip THEN upper
    for (spot_key, fno_key) in alias_pairs {
        // C3 — list order
        if ascii_upper(fno_key) == upper {
            add(&mut candidates, underlying, spot_key); // C4 — reverse
        }
        if spot_key.as_str() == upper.as_str() {
            add(&mut candidates, underlying, fno_key); // C5 — forward
        }
    }
    add(
        &mut candidates,
        underlying,
        &format!("{exchange}-{underlying}"), // C6 — RAW UNSTRIPPED args
    );
    for drifted in spot_key_candidates(underlying, exchange) {
        add(&mut candidates, underlying, &drifted); // C7 — the law-B rungs
    }
    for recovered in drift_bucket {
        add(&mut candidates, underlying, recovered); // C8 — bucket LAST
    }
    candidates // C9
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn s(items: &[&str]) -> Vec<String> {
        items.iter().map(|x| x.to_string()).collect()
    }

    /// The two SOURCED alias pairs (spot_key, fno_key) as the Python
    /// wrapper carries them — dict insertion order.
    fn sourced_pairs() -> Vec<(String, String)> {
        vec![
            ("NIFTYJR".to_string(), "NIFTYNXT50".to_string()),
            ("NIFTYMIDSELECT".to_string(), "MIDCPNIFTY".to_string()),
        ]
    }

    // ------------------------------------------------------------------
    // py_strip — the 10-byte set, member-by-member accept + gap-edge
    // rejects (the design §2.2 Rust-specific rows).
    // ------------------------------------------------------------------

    #[test]
    fn py_strip_accepts_each_of_the_10_bytes() {
        for &b in &PY_STRIP_BYTES {
            let c = b as char;
            let wrapped = format!("{c}X{c}");
            assert_eq!(py_strip(&wrapped), "X", "byte {b:#04x} must strip");
        }
    }

    #[test]
    fn py_strip_rejects_esc_0x1b_upper_gap_edge() {
        assert_eq!(py_strip("\x1bX\x1b"), "\x1bX\x1b");
    }

    #[test]
    fn py_strip_rejects_so_0x0e_lower_gap_edge() {
        // SO — the first byte AFTER the 0x09-0x0D run (R1 CORR NIT-1).
        assert_eq!(py_strip("\x0eX\x0e"), "\x0eX\x0e");
    }

    #[test]
    fn py_strip_rejects_nul_and_del() {
        assert_eq!(py_strip("\x00X\x00"), "\x00X\x00");
        assert_eq!(py_strip("\x7fX\x7f"), "\x7fX\x7f");
    }

    #[test]
    fn py_strip_all_strip_bytes_to_empty() {
        assert_eq!(py_strip("\x1c\x1f"), "");
    }

    #[test]
    fn ascii_upper_identity_on_non_letter_bytes() {
        assert_eq!(ascii_upper("0-9 &%._\x1f\x7f"), "0-9 &%._\x1f\x7f");
    }

    // ------------------------------------------------------------------
    // Law A — fold (the §6.1 golden twins A1-A7).
    // ------------------------------------------------------------------

    #[test]
    fn a1_hyphen_dropped() {
        assert_eq!(stock_drift_fold("BAJAJ-AUTO"), "BAJAJAUTO");
    }

    #[test]
    fn a2_interior_space_folded() {
        assert_eq!(stock_drift_fold("india vix"), "INDIAVIX");
    }

    #[test]
    fn a3_edge_spaces_folded() {
        assert_eq!(stock_drift_fold(" M&M "), "M&M");
    }

    #[test]
    fn a4_tab_not_folded_two_vocabulary_row() {
        // The fold removes ONLY 0x20; py_strip trims the 10-byte set —
        // the two whitespace vocabularies must never conflate.
        assert_eq!(stock_drift_fold("a b\tc-d"), "AB\tCD");
        assert_eq!(py_strip("\ta\t"), "a");
    }

    #[test]
    fn a5_empty_legal() {
        assert_eq!(stock_drift_fold(""), "");
    }

    #[test]
    fn a6_non_ascii_core_diverges_from_python_gate_is_load_bearing() {
        // CPython folds "straße-1" → "STRASSE1" (ß→SS, a length-changing
        // Unicode case map). The Rust core deliberately does NOT — the
        // facade's ASCII pre-gate routes such input to the oracle, so the
        // core never sees it in production. This pin documents WHY the
        // gate exists (the Python-side golden #A6 pins the oracle value).
        assert_eq!(stock_drift_fold("straße-1"), "STRAßE1");
    }

    #[test]
    fn a7_ampersand_untouched() {
        assert_eq!(stock_drift_fold("m&m"), "M&M");
    }

    // ------------------------------------------------------------------
    // Law B — candidates (the §6.1 golden twins B1-B11).
    // ------------------------------------------------------------------

    #[test]
    fn b1_whitespace_rung() {
        assert_eq!(spot_key_candidates("M&M ", "NSE"), s(&["M&M"]));
    }

    #[test]
    fn b2_prefix_rung_clean_symbol() {
        assert_eq!(spot_key_candidates("NSE-M&M", "NSE"), s(&["M&M"]));
    }

    #[test]
    fn b3_both_rungs_order_pinned() {
        assert_eq!(
            spot_key_candidates(" NSE-M&M", "NSE"),
            s(&["NSE-M&M", "M&M"])
        );
    }

    #[test]
    fn b4_hyphenated_never_mangled_clean_no_candidates() {
        assert_eq!(spot_key_candidates("BAJAJ-AUTO", "NSE"), s(&[]));
    }

    #[test]
    fn b5_strip_set_accept_twin_fs_wrapped() {
        assert_eq!(
            spot_key_candidates("\x1cNSE-M&M\x1c", "NSE"),
            s(&["NSE-M&M", "M&M"])
        );
    }

    #[test]
    fn b6_trailing_0x1f_fleet_golden() {
        assert_eq!(
            spot_key_candidates("NSE-M&M\x1f", "NSE"),
            s(&["NSE-M&M", "M&M"])
        );
    }

    #[test]
    fn b7_reject_twins_esc_and_so_prefix_no_longer_anchors() {
        assert_eq!(spot_key_candidates("\x1bNSE-M&M", "NSE"), s(&[]));
        assert_eq!(spot_key_candidates("\x0eNSE-M&M", "NSE"), s(&[]));
    }

    #[test]
    fn b8_pathological_empty_candidate_drop_both_shapes() {
        assert_eq!(spot_key_candidates("NSE-", "NSE"), s(&[]));
        assert_eq!(spot_key_candidates(" NSE- ", "NSE"), s(&["NSE-"]));
    }

    #[test]
    fn b9_byte_blind_prefix_never_case_mapped() {
        // ASCII input — the gate does NOT close; the crate value ships
        // flag-ON. The prefix strip is byte-blind: "FI" never case-mapped.
        assert_eq!(spot_key_candidates("NSE-FI", "NSE"), s(&["FI"]));
    }

    #[test]
    fn b10_empty_and_whitespace_only() {
        assert_eq!(spot_key_candidates("", "NSE"), s(&[]));
        assert_eq!(spot_key_candidates("  ", "NSE"), s(&[]));
    }

    #[test]
    fn b11_foreign_prefix_and_stripped_exchange() {
        assert_eq!(spot_key_candidates("BSE-M&M", "NSE"), s(&[]));
        assert_eq!(spot_key_candidates("NSE-M&M", " NSE "), s(&["M&M"]));
    }

    // ------------------------------------------------------------------
    // Law C — recovery (the §6.1 golden twins; env-parse rows C4/C5 live
    // Python-side — the pair MECHANICS are pinned by c7's asymmetry).
    // ------------------------------------------------------------------

    #[test]
    fn c1_sourced_reverse() {
        assert_eq!(
            spot_recovery_candidates("NIFTYNXT50", "NSE", &sourced_pairs(), &[]),
            s(&["NIFTYJR", "NSE-NIFTYNXT50"])
        );
    }

    #[test]
    fn c2_sourced_forward() {
        assert_eq!(
            spot_recovery_candidates("NIFTYJR", "NSE", &sourced_pairs(), &[]),
            s(&["NIFTYNXT50", "NSE-NIFTYJR"])
        );
    }

    #[test]
    fn c3_second_sourced_pair_both_directions() {
        assert_eq!(
            spot_recovery_candidates("MIDCPNIFTY", "NSE", &sourced_pairs(), &[]),
            s(&["NIFTYMIDSELECT", "NSE-MIDCPNIFTY"])
        );
        assert_eq!(
            spot_recovery_candidates("NIFTYMIDSELECT", "NSE", &sourced_pairs(), &[]),
            s(&["MIDCPNIFTY", "NSE-NIFTYMIDSELECT"])
        );
    }

    #[test]
    fn c6_raw_c6_vs_stripped_c2_mix_frozen_order() {
        // The C6 rung formats the RAW UNSTRIPPED args verbatim, while the
        // C2 upper works on the stripped form — the mix frozen in order.
        assert_eq!(
            spot_recovery_candidates(" NIFTYNXT50 ", "NSE", &sourced_pairs(), &[]),
            s(&["NIFTYJR", "NSE- NIFTYNXT50 ", "NIFTYNXT50"])
        );
    }

    #[test]
    fn c7_lowercase_env_value_asymmetry_both_directions() {
        // env pair ("FOO", "bar") — key uppercased at Python parse, value
        // stripped but NOT uppercased.
        let pairs = vec![("FOO".to_string(), "bar".to_string())];
        // C4: "bar".upper() == "BAR" → emits "FOO".
        assert_eq!(
            spot_recovery_candidates("BAR", "NSE", &pairs, &[]),
            s(&["FOO", "NSE-BAR"])
        );
        // C5: verbatim compare → emits the LOWERCASE "bar".
        assert_eq!(
            spot_recovery_candidates("FOO", "NSE", &pairs, &[]),
            s(&["bar", "NSE-FOO"])
        );
    }

    #[test]
    fn c8_dedup_first_occurrence_wins() {
        let pairs = vec![("BAJAJ-AUTO".to_string(), "BAJAJAUTO".to_string())];
        let bucket = s(&["BAJAJ-AUTO", "BAJAJ AUTO"]);
        assert_eq!(
            spot_recovery_candidates("BAJAJAUTO", "NSE", &pairs, &bucket),
            s(&["BAJAJ-AUTO", "NSE-BAJAJAUTO", "BAJAJ AUTO"])
        );
    }

    #[test]
    fn c9_identity_bucket_entry_excluded_drifted_appended() {
        let bucket = s(&["M&M", "M&M "]);
        assert_eq!(
            spot_recovery_candidates("M&M", "NSE", &[], &bucket),
            s(&["NSE-M&M", "M&M "])
        );
    }

    #[test]
    fn c10_blank_exchange_c6_unconditional() {
        // C6 survives a blank exchange (raw format); the law-B prefix
        // rung is skipped.
        assert_eq!(spot_recovery_candidates("Y", "", &[], &[]), s(&["-Y"]));
    }

    #[test]
    fn c11_degenerate_empty_underlying() {
        assert_eq!(spot_recovery_candidates("", "NSE", &[], &[]), s(&["NSE-"]));
    }

    #[test]
    fn c12_non_ascii_bucket_entry_core_passthrough() {
        // Bucket entries are equality-only pass-throughs — byte-safe in
        // the core. In production the facade's blanket ASCII gate routes
        // ANY non-ASCII input (bucket included) to the oracle; this pin
        // documents the core is safe regardless.
        let bucket = s(&["MÜM"]);
        assert_eq!(
            spot_recovery_candidates("X", "NSE", &[], &bucket),
            s(&["NSE-X", "MÜM"])
        );
    }

    #[test]
    fn c13_percent26_negative_pledge_pin() {
        // NO `&`-carrying candidate is ever minted from "%26" — the
        // module has no decode/replace/percent arm (#1482 pledge).
        let result = spot_recovery_candidates("M%26M", "NSE", &[], &[]);
        assert_eq!(result, s(&["NSE-M%26M"]));
        assert!(result.iter().all(|c| !c.contains('&')));
    }

    #[test]
    fn c14_drifted_shaped_underlying_walks_law_b() {
        assert_eq!(
            spot_recovery_candidates("NSE-M&M", "NSE", &[], &[]),
            s(&["NSE-NSE-M&M", "M&M"])
        );
    }

    #[test]
    fn c15_prefix_invariant_as_a_golden() {
        let pairs = vec![("BAJAJ-AUTO".to_string(), "BAJAJAUTO".to_string())];
        let bucket = s(&["BAJAJ-AUTO", "BAJAJ AUTO"]);
        let without = spot_recovery_candidates("BAJAJAUTO", "NSE", &pairs, &[]);
        let with = spot_recovery_candidates("BAJAJAUTO", "NSE", &pairs, &bucket);
        assert_eq!(without.len(), 2);
        assert_eq!(&with[..without.len()], &without[..]);
    }

    // ------------------------------------------------------------------
    // Proptests (§1.2): never-panics · prefix invariant · fold
    // idempotence · identity-exclusion + dedup structural properties.
    // ------------------------------------------------------------------

    proptest! {
        #[test]
        fn prop_never_panics_full_unicode(
            sym in ".*",
            exch in ".*",
            pairs in proptest::collection::vec((".*", ".*"), 0..4),
            bucket in proptest::collection::vec(".*", 0..4),
        ) {
            let pairs: Vec<(String, String)> = pairs;
            let bucket: Vec<String> = bucket;
            let _ = stock_drift_fold(&sym);
            let _ = spot_key_candidates(&sym, &exch);
            let _ = spot_recovery_candidates(&sym, &exch, &pairs, &bucket);
        }

        #[test]
        fn prop_recovery_prefix_invariant(
            u in ".*",
            e in ".*",
            pairs in proptest::collection::vec((".*", ".*"), 0..4),
            bucket in proptest::collection::vec(".*", 0..4),
        ) {
            let pairs: Vec<(String, String)> = pairs;
            let bucket: Vec<String> = bucket;
            let base = spot_recovery_candidates(&u, &e, &pairs, &[]);
            let full = spot_recovery_candidates(&u, &e, &pairs, &bucket);
            prop_assert!(full.len() >= base.len());
            prop_assert_eq!(&full[..base.len()], &base[..]);
        }

        #[test]
        fn prop_fold_idempotent_ascii(sym in "[\\x00-\\x7f]*") {
            let once = stock_drift_fold(&sym);
            prop_assert_eq!(stock_drift_fold(&once), once);
        }

        #[test]
        fn prop_candidates_structural(sym in ".*", exch in ".*") {
            let result = spot_key_candidates(&sym, &exch);
            // Max 2 rungs; identity excluded on the RAW param; entries
            // distinct + non-empty.
            prop_assert!(result.len() <= 2);
            for entry in &result {
                prop_assert!(!entry.is_empty());
                prop_assert_ne!(entry.as_str(), sym.as_str());
            }
            if result.len() == 2 {
                prop_assert_ne!(&result[0], &result[1]);
            }
        }

        #[test]
        fn prop_recovery_identity_excluded_and_deduped(
            u in ".*",
            e in ".*",
            pairs in proptest::collection::vec((".*", ".*"), 0..4),
            bucket in proptest::collection::vec(".*", 0..4),
        ) {
            let pairs: Vec<(String, String)> = pairs;
            let bucket: Vec<String> = bucket;
            let result = spot_recovery_candidates(&u, &e, &pairs, &bucket);
            for entry in &result {
                prop_assert_ne!(entry.as_str(), u.as_str());
            }
            for (i, entry) in result.iter().enumerate() {
                for later in &result[i + 1..] {
                    prop_assert_ne!(entry, later);
                }
            }
            // The structural output ceiling the facade refuse gate uses.
            prop_assert!(
                result.len() <= 2 * pairs.len() + 3 + bucket.len()
            );
        }
    }
}
