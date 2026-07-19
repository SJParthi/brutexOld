//! `brutex/calendars/observed_sessions.py` decision laws — the pure
//! `classify_month_drift` core + the overlay-sidecar whole-accept /
//! whole-reject validation (DEC-RUST-CALENDAR-LEAVES-001).
//!
//! I/O, logging (which WARN fires), the sidecar merge/diff and the
//! byte-deterministic serializer all STAY PYTHON — this module answers only
//! the two pure questions: "which days does this month's evidence flag, and
//! do the caps trip?" and "does this sidecar payload load as an overlay,
//! and to which day-set?".

use serde_json::Value;
use std::collections::BTreeSet;

use crate::pyiso::is_canonical_iso_date;

/// `_ENGINE_INDEX_COUNT` — the corroboration bar (3 engine indices).
pub const ENGINE_INDEX_COUNT: i64 = 3;
/// `MIN_CLEAN_SUCCESS_WINDOWS` — a CONSTANT in the admission rule (the
/// Python law reads the module constant, never a kwarg).
pub const MIN_CLEAN_SUCCESS_WINDOWS: i64 = 1;
/// `OBSERVED_LABEL` — stamped on every entry; the loader rejects without it.
pub const OBSERVED_LABEL: &str = "observed-not-official";
/// `OBSERVED_OVERLAY_VERSION`.
pub const OBSERVED_OVERLAY_VERSION: i64 = 1;

/// One evidence row — `(day_iso, zero_bars_all_indices,
/// indices_with_month_data, clean_success_windows, failed_windows_touching)`
/// in `DayEvidence` field order. The day string is an opaque token here
/// (the classifier never parses it — exactly like the Python core, which
/// only ever regroups `row.day` into the verdict tuples).
pub type EvidenceRow = (String, bool, i64, i64, i64);

/// The pure verdict — `DriftVerdict` minus the passthrough fields
/// (`month`, `detected_at`, `zero_bar_indices`) Python re-attaches.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClassifyOut {
    /// Zero-bar derived trading days, in evidence order.
    pub candidates: Vec<String>,
    /// Admitted-and-not-capped flags: `(day_iso, clean_success_windows)`.
    pub flagged: Vec<(String, i64)>,
    /// Candidates killed by the FAILED veto.
    pub vetoed_failed: Vec<String>,
    /// Candidates lacking corroboration / clean-SUCCESS cover.
    pub no_evidence: Vec<String>,
    /// More than `max_flags_per_month` admitted — the WHOLE month refused.
    pub over_month_cap: bool,
    /// Year total would exceed `max_overlay_days_per_year` — refused
    /// (computed ONLY when the month cap did not trip; month cap wins).
    pub over_year_cap: bool,
}

/// `classify_month_drift` — the exact Python decision order:
/// skip non-candidates → veto (`failed > 0`) → no-evidence
/// (`indices != 3 or clean < MIN_CLEAN_SUCCESS_WINDOWS` — the CONSTANT) →
/// admitted; month cap first (over ⇒ `flagged=()`), year cap only when the
/// month cap did not trip.
pub fn classify_month_drift(
    evidence: &[EvidenceRow],
    existing_year_overlay_days: i64,
    max_flags_per_month: i64,
    max_overlay_days_per_year: i64,
) -> ClassifyOut {
    let mut candidates: Vec<String> = Vec::new();
    let mut vetoed: Vec<String> = Vec::new();
    let mut no_evidence: Vec<String> = Vec::new();
    let mut admitted: Vec<(String, i64)> = Vec::new();
    for (day, zero_bars, indices, clean, failed) in evidence {
        if !zero_bars {
            continue;
        }
        candidates.push(day.clone());
        if *failed > 0 {
            vetoed.push(day.clone());
            continue;
        }
        if *indices != ENGINE_INDEX_COUNT || *clean < MIN_CLEAN_SUCCESS_WINDOWS {
            no_evidence.push(day.clone());
            continue;
        }
        admitted.push((day.clone(), *clean));
    }
    let n_admitted = i128::try_from(admitted.len()).expect("evidence length fits i128");
    let over_month = n_admitted > i128::from(max_flags_per_month);
    let over_year = !over_month
        && (i128::from(existing_year_overlay_days) + n_admitted
            > i128::from(max_overlay_days_per_year));
    let flagged = if over_month || over_year {
        Vec::new()
    } else {
        admitted
    };
    ClassifyOut {
        candidates,
        flagged,
        vetoed_failed: vetoed,
        no_evidence,
        over_month_cap: over_month,
        over_year_cap: over_year,
    }
}

/// `payload.get("version") == OBSERVED_OVERLAY_VERSION` under Python `==`
/// semantics — the int/bool/float PUNNING is load-bearing (a JSON `true`
/// version PASSES because `True == 1`; a `1.0` passes because `1.0 == 1`) —
/// the DELIBERATE asymmetry against settle_evidence's exact-int schema gate.
fn version_matches(v: Option<&Value>) -> bool {
    match v {
        Some(Value::Bool(b)) => *b, // True == 1; False == 0 != 1
        Some(Value::Number(n)) => {
            let t = n.to_string();
            if crate::pyrepr::number_token_is_int(&t) {
                t == "1" // canonical JSON int token ("-0" is the only alias, != 1)
            } else {
                t.parse::<f64>().is_ok_and(|f| f == 1.0)
            }
        }
        _ => false,
    }
}

/// `_valid_entry_day` — dict + exact label + str date + canonical-form
/// round-trip (see [`is_canonical_iso_date`]).
fn valid_entry_day(entry: &Value) -> Option<&str> {
    let Value::Object(map) = entry else {
        return None;
    };
    match map.get("label") {
        Some(Value::String(label)) if label == OBSERVED_LABEL => {}
        _ => return None,
    }
    let Some(Value::String(raw)) = map.get("date") else {
        return None;
    };
    if !is_canonical_iso_date(raw) {
        return None;
    }
    Some(raw)
}

/// The `load_observed_holidays` whole-overlay validation decision over an
/// already-parsed payload: `Some(sorted unique ISO days)` iff EVERY rule
/// passes; `None` on any failure (wrong shape/version, entries not a list,
/// ONE invalid entry rejects the WHOLE overlay). Canonical ISO strings sort
/// chronologically, so the sorted Vec is a deterministic set rendering.
pub fn parse_observed_sidecar(payload: &Value) -> Option<Vec<String>> {
    let Value::Object(map) = payload else {
        return None;
    };
    if !version_matches(map.get("version")) {
        return None;
    }
    let Some(Value::Array(entries)) = map.get("entries") else {
        return None;
    };
    let mut days: BTreeSet<String> = BTreeSet::new();
    for entry in entries {
        let day = valid_entry_day(entry)?;
        days.insert(day.to_owned());
    }
    Some(days.into_iter().collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(day: &str, zero: bool, idx: i64, clean: i64, failed: i64) -> EvidenceRow {
        (day.to_owned(), zero, idx, clean, failed)
    }

    #[test]
    fn decision_order_matrix() {
        // veto beats no-evidence (checked FIRST): idx=2 AND failed=1 → vetoed.
        let out = classify_month_drift(&[row("2026-05-13", true, 2, 0, 1)], 0, 3, 8);
        assert_eq!(out.vetoed_failed, vec!["2026-05-13"]);
        assert!(out.no_evidence.is_empty());
        assert!(out.flagged.is_empty());
        // non-candidate rows appear NOWHERE.
        let out = classify_month_drift(&[row("2026-05-13", false, 3, 1, 1)], 0, 3, 8);
        assert!(out.candidates.is_empty());
        assert!(out.vetoed_failed.is_empty());
        // indices != 3 (over-count too) → no_evidence.
        for idx in [0, 2, 4] {
            let out = classify_month_drift(&[row("2026-05-13", true, idx, 1, 0)], 0, 3, 8);
            assert_eq!(out.no_evidence, vec!["2026-05-13"], "idx={idx}");
        }
        // clean < 1 (the CONSTANT, not a kwarg) → no_evidence.
        let out = classify_month_drift(&[row("2026-05-13", true, 3, 0, 0)], 0, 3, 8);
        assert_eq!(out.no_evidence, vec!["2026-05-13"]);
        // admitted carries the row's clean_success_windows.
        let out = classify_month_drift(&[row("2026-05-13", true, 3, 7, 0)], 0, 3, 8);
        assert_eq!(out.flagged, vec![("2026-05-13".to_owned(), 7)]);
    }

    #[test]
    fn month_cap_wins_over_year_cap() {
        let rows: Vec<EvidenceRow> = (11..=14)
            .map(|d| row(&format!("2026-05-{d}"), true, 3, 1, 0))
            .collect();
        // 4 admitted > 3 AND existing 8 would also trip the year cap — the
        // month cap wins and over_year_cap stays FALSE (mutual exclusion).
        let out = classify_month_drift(&rows, 8, 3, 8);
        assert!(out.over_month_cap);
        assert!(!out.over_year_cap);
        assert!(out.flagged.is_empty());
        assert_eq!(out.candidates.len(), 4);
    }

    #[test]
    fn year_cap_boundary() {
        let rows: Vec<EvidenceRow> = (11..=13)
            .map(|d| row(&format!("2026-05-{d}"), true, 3, 1, 0))
            .collect();
        // 5 + 3 == 8 — NOT over (strict >).
        let out = classify_month_drift(&rows, 5, 3, 8);
        assert!(!out.over_year_cap);
        assert_eq!(out.flagged.len(), 3);
        // 6 + 3 == 9 > 8 — over, flagged emptied, admission lists intact.
        let out = classify_month_drift(&rows, 6, 3, 8);
        assert!(out.over_year_cap);
        assert!(!out.over_month_cap);
        assert!(out.flagged.is_empty());
        assert_eq!(out.candidates.len(), 3);
    }

    #[test]
    fn caps_never_overflow() {
        // Python int math is unbounded: i64::MAX existing + 1 admitted IS
        // over an i64::MAX year cap. A wrapping i64 add would read
        // i64::MIN < cap → false — the i128 widening keeps the Python
        // answer (and `overflow-checks = true` would panic on a wrap).
        let rows = vec![row("2026-05-13", true, 3, 1, 0)];
        let out = classify_month_drift(&rows, i64::MAX, 3, i64::MAX);
        assert!(out.over_year_cap);
        assert!(out.flagged.is_empty());
        let out = classify_month_drift(&rows, i64::MAX, i64::MIN, i64::MAX);
        assert!(out.over_month_cap);
        // Boundary sanity: existing + admitted == cap exactly is NOT over.
        let out = classify_month_drift(&rows, i64::MAX - 1, 3, i64::MAX);
        assert!(!out.over_year_cap);
        assert_eq!(out.flagged.len(), 1);
    }

    fn v(json: &str) -> Value {
        serde_json::from_str(json).expect("valid test JSON")
    }

    fn entry(date: &str) -> String {
        format!("{{\"date\": \"{date}\", \"label\": \"observed-not-official\"}}")
    }

    #[test]
    fn sidecar_version_punning() {
        let e = entry("2026-01-13");
        for good in ["1", "true", "1.0"] {
            let p = v(&format!("{{\"version\": {good}, \"entries\": [{e}]}}"));
            assert_eq!(
                parse_observed_sidecar(&p),
                Some(vec!["2026-01-13".to_owned()]),
                "version={good}"
            );
        }
        // 1e0 == 1.0 == 1 in Python — the float pun covers exponent forms.
        let p = v(&format!("{{\"version\": 1e0, \"entries\": [{e}]}}"));
        assert_eq!(
            parse_observed_sidecar(&p),
            Some(vec!["2026-01-13".to_owned()])
        );
        for bad in ["\"1\"", "2", "false", "0", "-0", "null", "[1]", "1.5"] {
            let p = v(&format!("{{\"version\": {bad}, \"entries\": [{e}]}}"));
            assert_eq!(parse_observed_sidecar(&p), None, "version={bad}");
        }
        let p = v(&format!("{{\"entries\": [{e}]}}"));
        assert_eq!(parse_observed_sidecar(&p), None, "missing version");
    }

    #[test]
    fn sidecar_one_bad_entry_rejects_all() {
        let good = entry("2026-01-13");
        let bad = "{\"date\": \"2026-05-13\", \"label\": \"wrong\"}";
        let p = v(&format!("{{\"version\": 1, \"entries\": [{good}, {bad}]}}"));
        assert_eq!(parse_observed_sidecar(&p), None);
    }

    #[test]
    fn sidecar_compact_and_noncanonical_dates_reject() {
        for bad_date in [
            "20260513",
            "2026-5-13",
            "2026-W20-3",
            "2026-02-30",
            "0000-01-01",
        ] {
            let p = v(&format!(
                "{{\"version\": 1, \"entries\": [{}]}}",
                entry(bad_date)
            ));
            assert_eq!(parse_observed_sidecar(&p), None, "date={bad_date}");
        }
    }

    #[test]
    fn sidecar_valid_shapes() {
        let p = v("{\"version\": 1, \"entries\": []}");
        assert_eq!(parse_observed_sidecar(&p), Some(vec![]));
        // duplicates collapse; output sorted.
        let p = v(&format!(
            "{{\"version\": 1, \"entries\": [{}, {}, {}]}}",
            entry("2026-05-13"),
            entry("2026-01-13"),
            entry("2026-05-13"),
        ));
        assert_eq!(
            parse_observed_sidecar(&p),
            Some(vec!["2026-01-13".to_owned(), "2026-05-13".to_owned()])
        );
        // extra entry keys ignored (the Python .get() law).
        let p = v("{\"version\": 1, \"entries\": [{\"date\": \"2026-01-13\", \
             \"label\": \"observed-not-official\", \"junk\": {\"deep\": [1]}}]}");
        assert_eq!(
            parse_observed_sidecar(&p),
            Some(vec!["2026-01-13".to_owned()])
        );
    }

    #[test]
    fn sidecar_wrong_shapes_reject() {
        for bad in [
            "[1, 2]",
            "\"x\"",
            "{\"version\": 1}",
            "{\"version\": 1, \"entries\": {}}",
            "{\"version\": 1, \"entries\": [\"x\"]}",
            "{\"version\": 1, \"entries\": [{\"date\": 20260113, \
             \"label\": \"observed-not-official\"}]}",
            "{\"version\": 1, \"entries\": [{\"label\": \"observed-not-official\"}]}",
            "{\"version\": 1, \"entries\": [{\"date\": \"2026-01-13\"}]}",
            "{\"version\": 1, \"entries\": [{\"date\": \"2026-01-13\", \"label\": 7}]}",
        ] {
            assert_eq!(parse_observed_sidecar(&v(bad)), None, "payload={bad}");
        }
    }
}
