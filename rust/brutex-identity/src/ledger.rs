//! Window-ledger STORE laws — the merge-on-save conflict rule + legacy
//! derivations + row coercion.
//!
//! The Rust home of `brutex/data/window_ledger_store.py`'s pure laws
//! (DEC-RUST-RUNID-STORES-001): `merge_ledger_rows` + `_row_preference` +
//! `_row_key` (DEC-WINDOW-LEDGER-RESCUE-001), and the legacy optional-column
//! derivations `_derive_empty_streak` / `_derive_in_pull_fetches`
//! (`bars_net_new` / the retry split stay an honest `None` — the
//! DEC-INGEST-BOUNDARY-INTEGRITY-001 / DEC-RETRY-COUNTER-SPLIT-001 laws,
//! applied by the codec reader). The laws already Rust-hosted in
//! `rust/brutex-lake/src/window_ledger.rs` (status classification + the
//! in-memory `WindowLedger` merge KEY, DEC-LAKE-RUST-WINDOW-LEDGER-001
//! #1915) are DIFFERENT laws of the sibling `window_ledger.py` module —
//! nothing here duplicates them.

/// One serialised ledger row — the exact 16-field tuple
/// `WindowLedger.to_rows` emits (`LedgerRowTuple` on the Python side).
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct LedgerRow {
    pub market: String,
    pub security_id: String,
    pub interval: String,
    pub window_start: String,
    pub window_end: String,
    pub status: String,
    pub bars: i64,
    pub attempts: i64,
    pub last_error: Option<String>,
    pub first_seen_at: String,
    pub last_attempt_at: Option<String>,
    pub empty_streak: i64,
    pub in_pull_fetches: i64,
    pub bars_net_new: Option<i64>,
    pub empty_retries: Option<i64>,
    pub transient_retries: Option<i64>,
}

/// The fixed on-disk column order (`window_ledger_store._COLUMNS` twin — the
/// Python tuple now DERIVES from this constant, one source of truth).
pub const LEDGER_COLUMNS: [&str; 16] = [
    "market",
    "security_id",
    "interval",
    "window_start",
    "window_end",
    "status",
    "bars",
    "attempts",
    "last_error",
    "first_seen_at",
    "last_attempt_at",
    "empty_streak",
    "in_pull_fetches",
    "bars_net_new",
    "empty_retries",
    "transient_retries",
];

/// The CORE columns every ledger Parquet MUST carry (`_CORE_COLUMNS` twin).
pub const LEDGER_CORE_COLUMNS: [&str; 11] = [
    "market",
    "security_id",
    "interval",
    "window_start",
    "window_end",
    "status",
    "bars",
    "attempts",
    "last_error",
    "first_seen_at",
    "last_attempt_at",
];

/// The 5-field window identity of one row (`_row_key` twin).
pub type RowKey = (String, String, String, String, String);

impl LedgerRow {
    /// The window identity — the leading 5 fields.
    pub fn key(&self) -> RowKey {
        (
            self.market.clone(),
            self.security_id.clone(),
            self.interval.clone(),
            self.window_start.clone(),
            self.window_end.clone(),
        )
    }

    /// The `tuple("" if v is None else str(v) for v in row)` determinism
    /// tie-break projection of `_row_preference` — field order = row order.
    fn full_row_strings(&self) -> [String; 16] {
        [
            self.market.clone(),
            self.security_id.clone(),
            self.interval.clone(),
            self.window_start.clone(),
            self.window_end.clone(),
            self.status.clone(),
            self.bars.to_string(),
            self.attempts.to_string(),
            self.last_error.clone().unwrap_or_default(),
            self.first_seen_at.clone(),
            self.last_attempt_at.clone().unwrap_or_default(),
            self.empty_streak.to_string(),
            self.in_pull_fetches.to_string(),
            self.bars_net_new.map(|v| v.to_string()).unwrap_or_default(),
            self.empty_retries
                .map(|v| v.to_string())
                .unwrap_or_default(),
            self.transient_retries
                .map(|v| v.to_string())
                .unwrap_or_default(),
        ]
    }
}

/// Settledness rank (`_STATUS_SETTLE_RANK` twin): a MORE-SETTLED status wins;
/// an UNKNOWN status ranks BELOW everything (defensive).
fn status_rank(status: &str) -> i32 {
    match status {
        "SUCCESS" | "SKIPPED" => 3,
        "EMPTY" => 2,
        "FAILED" => 1,
        "PLANNED" => 0,
        _ => -1,
    }
}

/// The total order deciding a same-key merge conflict (`_row_preference` twin):
/// (1) settledness rank, (2) newer `last_attempt_at` (`None` → "" sorts
/// oldest — ISO-8601 UTC strings compare lexicographically), (3) higher
/// `attempts`, (4) the full-row string tuple (pure determinism tie-break).
/// Python `str` tuple comparison is code-point order == Rust's UTF-8 byte
/// order, so the two implementations agree on every input.
fn row_preference(row: &LedgerRow) -> (i32, String, i64, [String; 16]) {
    (
        status_rank(&row.status),
        row.last_attempt_at.clone().unwrap_or_default(),
        row.attempts,
        row.full_row_strings(),
    )
}

/// Merge the in-memory rows with the on-disk rows per window key — pure
/// (`merge_ledger_rows` twin, DEC-WINDOW-LEDGER-RESCUE-001).
///
/// * memory-only → the in-memory row; disk-only → kept UNLESS pruned;
/// * both → the [`row_preference`] winner, EXCEPT a reopened/degraded key
///   (a deliberate demotion) keeps the in-memory row outright;
/// * output sorted by window key.
pub fn merge_ledger_rows(
    memory_rows: Vec<LedgerRow>,
    disk_rows: Vec<LedgerRow>,
    pruned_keys: &std::collections::HashSet<RowKey>,
    reopened_keys: &std::collections::HashSet<RowKey>,
    degraded_keys: &std::collections::HashSet<RowKey>,
) -> Vec<LedgerRow> {
    let mut merged: std::collections::HashMap<RowKey, LedgerRow> = memory_rows
        .into_iter()
        .map(|row| (row.key(), row))
        .collect();
    for row in disk_rows {
        let key = row.key();
        match merged.get(&key) {
            None => {
                if !pruned_keys.contains(&key) {
                    merged.insert(key, row);
                }
            }
            Some(mine) => {
                if reopened_keys.contains(&key) || degraded_keys.contains(&key) {
                    continue; // deliberate demotion — the in-memory row wins
                }
                if row_preference(&row) > row_preference(mine) {
                    merged.insert(key, row);
                }
            }
        }
    }
    let mut out: Vec<LedgerRow> = merged.into_values().collect();
    out.sort_by_key(|row| row.key());
    out
}

/// Best-effort `empty_streak` for a LEGACY row predating the column
/// (`_derive_empty_streak` twin; `skipped_streak` = the module-constant
/// `EMPTY_TERMINAL_THRESHOLD` the Python caller passes — never the load's
/// per-call threshold, preserving the retired derivation exactly).
pub fn derive_empty_streak(status: &str, attempts: i64, skipped_streak: i64) -> i64 {
    match status {
        "SKIPPED" => skipped_streak,
        "EMPTY" => attempts,
        _ => 0,
    }
}

/// Best-effort `in_pull_fetches` for a LEGACY row predating the column
/// (`_derive_in_pull_fetches` twin): PLANNED was never fetched (0); every
/// other status was fetched at least once (1).
pub fn derive_in_pull_fetches(status: &str) -> i64 {
    if status == "PLANNED" { 0 } else { 1 }
}

/// A raw row as decoded from one ledger Parquet — the three legacy-derivable
/// optional columns still unresolved (absent column OR null cell → `None`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RawLedgerRow {
    pub market: String,
    pub security_id: String,
    pub interval: String,
    pub window_start: String,
    pub window_end: String,
    pub status: String,
    pub bars: i64,
    pub attempts: i64,
    pub last_error: Option<String>,
    pub first_seen_at: String,
    pub last_attempt_at: Option<String>,
    pub empty_streak: Option<i64>,
    pub in_pull_fetches: Option<i64>,
    pub bars_net_new: Option<i64>,
    pub empty_retries: Option<i64>,
    pub transient_retries: Option<i64>,
}

/// Resolve a raw decoded row into the 16-field record (`_rows_to_records`
/// twin): a `None` `empty_streak` / `in_pull_fetches` derives from the row's
/// terminal state; `bars_net_new` / `empty_retries` / `transient_retries`
/// stay an honest `None` (any number would be a fabrication — Golden Rule #1).
pub fn coerce_raw_row(raw: RawLedgerRow, skipped_streak: i64) -> LedgerRow {
    let streak = raw
        .empty_streak
        .unwrap_or_else(|| derive_empty_streak(&raw.status, raw.attempts, skipped_streak));
    let fetches = raw
        .in_pull_fetches
        .unwrap_or_else(|| derive_in_pull_fetches(&raw.status));
    LedgerRow {
        market: raw.market,
        security_id: raw.security_id,
        interval: raw.interval,
        window_start: raw.window_start,
        window_end: raw.window_end,
        status: raw.status,
        bars: raw.bars,
        attempts: raw.attempts,
        last_error: raw.last_error,
        first_seen_at: raw.first_seen_at,
        last_attempt_at: raw.last_attempt_at,
        empty_streak: streak,
        in_pull_fetches: fetches,
        bars_net_new: raw.bars_net_new,
        empty_retries: raw.empty_retries,
        transient_retries: raw.transient_retries,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    fn row(security_id: &str, status: &str, attempts: i64) -> LedgerRow {
        LedgerRow {
            market: "groww".into(),
            security_id: security_id.into(),
            interval: "1minute".into(),
            window_start: "2024-01-01".into(),
            window_end: "2024-01-31".into(),
            status: status.into(),
            bars: 10,
            attempts,
            last_error: None,
            first_seen_at: "2024-01-31T16:00:00+00:00".into(),
            last_attempt_at: Some("2024-01-31T16:00:00+00:00".into()),
            empty_streak: 0,
            in_pull_fetches: 1,
            bars_net_new: Some(10),
            empty_retries: Some(0),
            transient_retries: Some(0),
        }
    }

    #[test]
    fn settled_beats_planned_and_unknown_ranks_below_everything() {
        let empty = HashSet::new();
        let merged = merge_ledger_rows(
            vec![row("A", "PLANNED", 0)],
            vec![row("A", "SUCCESS", 1)],
            &empty,
            &empty,
            &empty,
        );
        assert_eq!(merged[0].status, "SUCCESS");
        let merged = merge_ledger_rows(
            vec![row("A", "GARBAGE", 9)],
            vec![row("A", "PLANNED", 0)],
            &empty,
            &empty,
            &empty,
        );
        assert_eq!(merged[0].status, "PLANNED");
    }

    #[test]
    fn reopened_and_degraded_keys_keep_the_memory_row() {
        let empty = HashSet::new();
        let mut demoted = HashSet::new();
        demoted.insert(row("A", "FAILED", 1).key());
        let merged = merge_ledger_rows(
            vec![row("A", "FAILED", 1)],
            vec![row("A", "SKIPPED", 5)],
            &empty,
            &demoted,
            &empty,
        );
        assert_eq!(merged[0].status, "FAILED");
        let merged = merge_ledger_rows(
            vec![row("A", "FAILED", 1)],
            vec![row("A", "SUCCESS", 9)],
            &empty,
            &empty,
            &demoted,
        );
        assert_eq!(merged[0].status, "FAILED");
    }

    #[test]
    fn pruned_disk_only_rows_drop_and_output_sorts_by_key() {
        let mut pruned = HashSet::new();
        pruned.insert(row("A", "SUCCESS", 1).key());
        let empty = HashSet::new();
        let merged = merge_ledger_rows(
            vec![row("C", "SUCCESS", 1)],
            vec![row("A", "SUCCESS", 1), row("B", "SUCCESS", 1)],
            &pruned,
            &empty,
            &empty,
        );
        let ids: Vec<&str> = merged.iter().map(|r| r.security_id.as_str()).collect();
        assert_eq!(ids, vec!["B", "C"]);
    }

    #[test]
    fn none_last_attempt_sorts_oldest_and_full_row_breaks_ties() {
        let empty = HashSet::new();
        let mut newer = row("A", "EMPTY", 1);
        newer.bars = 0;
        let mut none_stamp = newer.clone();
        none_stamp.last_attempt_at = None;
        let merged = merge_ledger_rows(
            vec![none_stamp],
            vec![newer.clone()],
            &empty,
            &empty,
            &empty,
        );
        assert_eq!(merged[0], newer);
        // Same rank/stamp/attempts — the greater full-row tuple wins.
        let mut low = row("A", "SUCCESS", 2);
        low.bars = 1;
        let mut high = row("A", "SUCCESS", 2);
        high.bars = 2;
        let merged = merge_ledger_rows(vec![low], vec![high.clone()], &empty, &empty, &empty);
        assert_eq!(merged[0], high);
    }

    #[test]
    fn legacy_derivations_match_the_python_law() {
        assert_eq!(derive_empty_streak("SKIPPED", 1, 3), 3);
        assert_eq!(derive_empty_streak("EMPTY", 2, 3), 2);
        assert_eq!(derive_empty_streak("SUCCESS", 7, 3), 0);
        assert_eq!(derive_empty_streak("GARBAGE", 7, 3), 0);
        assert_eq!(derive_in_pull_fetches("PLANNED"), 0);
        for status in ["SUCCESS", "EMPTY", "FAILED", "SKIPPED", "GARBAGE"] {
            assert_eq!(derive_in_pull_fetches(status), 1);
        }
    }

    #[test]
    fn coerce_derives_only_the_derivable_columns() {
        let raw = RawLedgerRow {
            market: "groww".into(),
            security_id: "A".into(),
            interval: "1minute".into(),
            window_start: "2024-01-01".into(),
            window_end: "2024-01-31".into(),
            status: "EMPTY".into(),
            bars: 0,
            attempts: 2,
            last_error: None,
            first_seen_at: "t".into(),
            last_attempt_at: None,
            empty_streak: None,
            in_pull_fetches: None,
            bars_net_new: None,
            empty_retries: None,
            transient_retries: None,
        };
        let resolved = coerce_raw_row(raw, 3);
        assert_eq!(resolved.empty_streak, 2);
        assert_eq!(resolved.in_pull_fetches, 1);
        assert_eq!(resolved.bars_net_new, None); // honest unknown, never 0
        assert_eq!(resolved.empty_retries, None);
        assert_eq!(resolved.transient_retries, None);
    }
}
