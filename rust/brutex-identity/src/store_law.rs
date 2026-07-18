//! Results-store LAWS — run-id validation, the u128 mask↔limb codec, the
//! append/VIX-merge decision, and top-k ordering.
//!
//! The Rust home of `brutex/engine/results_store.py`'s computation plane
//! (DEC-RUST-RUNID-STORES-001). The `ResultRow` dataclass SHELL stays Python
//! (the import surface for ~27 consumer modules; `dataclasses.fields` /
//! `replace` semantics); the DECISIONS compute here:
//!
//! * [`validate_run_id_error`] — the canonical-BLAKE3-hex law;
//! * [`mask_to_limbs`] / [`mask_from_limbs`] — the u128 → two-signed-Int64
//!   Parquet limb codec (`_encode_limb` / `_decode_mask` twins);
//! * [`classify_append`] — the DIRECTIONAL three-branch append rule of
//!   DEC-VIX-STAMP-WIRE-001 + the C5 gate-conflict fingerprint of
//!   DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001 (`ResultsStore.append` +
//!   `_merge_vix_backfill` twins);
//! * [`rank_top_k`] — the deterministic leaderboard order (rank column,
//!   ties by `run_id` ascending; exact int ranking above 2^53).
//!
//! Prices cross the FFI as their `str(Decimal)` texts: every stored price is
//! `round_money`-snapped to exponent −2 at row construction, so text
//! equality ⟺ numeric equality (the Python dataclass compared Decimals
//! NUMERICALLY; the snap makes the two laws coincide — pinned by the
//! append-law oracle corpus). Floats carry no NaN and no −0.0 (both rejected
//! /normalised at construction), so `f64 ==` matches Python `==` exactly.

/// The FFI projection of one `ResultRow` — field order = the dataclass
/// declaration order (`dataclasses.fields(ResultRow)`), `condition_names`
/// LAST (it is the derived `init=False` field).
#[derive(Clone, Debug, PartialEq)]
pub struct ResultRowFfi {
    pub run_id: String,
    pub instrument: String,
    pub tf: String,
    pub direction: String,
    pub mode: String,
    pub mask_bits: u128,
    pub net_pnl_paisa: i64,
    pub gross_profit_paisa: i64,
    pub gross_loss_paisa: i64,
    pub max_drawdown_paisa: i64,
    pub expectancy_paisa: i64,
    pub total_trades: i64,
    pub winning_trades: i64,
    pub losing_trades: i64,
    pub sharpe_ratio: f64,
    pub sortino_ratio: f64,
    pub calmar_ratio: f64,
    pub profit_factor: f64,
    pub win_rate: f64,
    pub max_drawdown_pct: f64,
    pub avg_entry_price: String,
    pub avg_exit_price: String,
    pub deflated_sharpe: Option<f64>,
    pub vix_mean: Option<f64>,
    pub vix_min: Option<f64>,
    pub vix_max: Option<f64>,
    pub condition_names: String,
}

/// The REFERENCE-ONLY VIX trio field names (`VIX_FIELDS` twin).
pub const VIX_FIELDS: [&str; 3] = ["vix_mean", "vix_min", "vix_max"];

/// The nullable gate-metric field names (`_NULLABLE_METRIC_FIELDS` twin).
pub const NULLABLE_METRIC_FIELDS: [&str; 1] = ["deflated_sharpe"];

/// One append decision (`ResultsStore.append`'s control flow, decided pure).
#[derive(Clone, Debug, PartialEq)]
pub enum AppendDecision {
    /// No stored row under this run_id — insert the incoming row.
    Insert,
    /// Stored row already carries everything the incoming row knows.
    Noop,
    /// Union-merge the VIX trio: `(merged_trio, backfilled_field_names)`.
    Backfill {
        merged: [Option<f64>; 3],
        backfilled: Vec<String>,
    },
    /// Same identity, differing non-VIX field(s) — a reproducibility breach.
    PlainConflict { differing: Vec<String> },
    /// The ONLY diff is nullable gate metric(s) — a gate-config re-run (C5).
    GateConflict { differing: Vec<String> },
    /// Two different KNOWN values on a VIX field — the VIX lake moved.
    VixConflict {
        differing: Vec<String>,
        conflicting: Vec<String>,
    },
}

/// Option<f64> equality under Python `==` semantics (no NaN by construction).
fn opt_eq(a: Option<f64>, b: Option<f64>) -> bool {
    match (a, b) {
        (None, None) => true,
        (Some(x), Some(y)) => x == y,
        _ => false,
    }
}

/// The differing field NAMES between two same-run_id rows, in dataclass
/// field order (the order `ResultsStore.append` logs + raises with).
fn differing_fields(existing: &ResultRowFfi, incoming: &ResultRowFfi) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let mut check = |name: &str, differs: bool| {
        if differs {
            out.push(name.to_string());
        }
    };
    check("run_id", existing.run_id != incoming.run_id);
    check("instrument", existing.instrument != incoming.instrument);
    check("tf", existing.tf != incoming.tf);
    check("direction", existing.direction != incoming.direction);
    check("mode", existing.mode != incoming.mode);
    check("mask_bits", existing.mask_bits != incoming.mask_bits);
    check(
        "net_pnl_paisa",
        existing.net_pnl_paisa != incoming.net_pnl_paisa,
    );
    check(
        "gross_profit_paisa",
        existing.gross_profit_paisa != incoming.gross_profit_paisa,
    );
    check(
        "gross_loss_paisa",
        existing.gross_loss_paisa != incoming.gross_loss_paisa,
    );
    check(
        "max_drawdown_paisa",
        existing.max_drawdown_paisa != incoming.max_drawdown_paisa,
    );
    check(
        "expectancy_paisa",
        existing.expectancy_paisa != incoming.expectancy_paisa,
    );
    check(
        "total_trades",
        existing.total_trades != incoming.total_trades,
    );
    check(
        "winning_trades",
        existing.winning_trades != incoming.winning_trades,
    );
    check(
        "losing_trades",
        existing.losing_trades != incoming.losing_trades,
    );
    check(
        "sharpe_ratio",
        existing.sharpe_ratio != incoming.sharpe_ratio,
    );
    check(
        "sortino_ratio",
        existing.sortino_ratio != incoming.sortino_ratio,
    );
    check(
        "calmar_ratio",
        existing.calmar_ratio != incoming.calmar_ratio,
    );
    check(
        "profit_factor",
        existing.profit_factor != incoming.profit_factor,
    );
    check("win_rate", existing.win_rate != incoming.win_rate);
    check(
        "max_drawdown_pct",
        existing.max_drawdown_pct != incoming.max_drawdown_pct,
    );
    check(
        "avg_entry_price",
        existing.avg_entry_price != incoming.avg_entry_price,
    );
    check(
        "avg_exit_price",
        existing.avg_exit_price != incoming.avg_exit_price,
    );
    check(
        "deflated_sharpe",
        !opt_eq(existing.deflated_sharpe, incoming.deflated_sharpe),
    );
    check("vix_mean", !opt_eq(existing.vix_mean, incoming.vix_mean));
    check("vix_min", !opt_eq(existing.vix_min, incoming.vix_min));
    check("vix_max", !opt_eq(existing.vix_max, incoming.vix_max));
    check(
        "condition_names",
        existing.condition_names != incoming.condition_names,
    );
    out
}

/// Decide one `ResultsStore.append` (`existing = None` ⇒ a fresh insert).
///
/// Mirrors the Python control flow exactly: equal rows → `Noop`; any non-VIX
/// difference → `GateConflict` iff the differing set ⊆ the nullable gate
/// metrics, else `PlainConflict`; an all-VIX difference → the directional
/// union merge — two different KNOWN values on one field → `VixConflict`,
/// else `Backfill` (which the caller downgrades to `Noop` when the merge
/// equals the stored row — the strictly-less-informed redo).
pub fn classify_append(existing: Option<&ResultRowFfi>, incoming: &ResultRowFfi) -> AppendDecision {
    let Some(existing) = existing else {
        return AppendDecision::Insert;
    };
    if existing == incoming {
        return AppendDecision::Noop;
    }
    let differing = differing_fields(existing, incoming);
    if differing
        .iter()
        .any(|name| !VIX_FIELDS.contains(&name.as_str()))
    {
        if differing
            .iter()
            .all(|name| NULLABLE_METRIC_FIELDS.contains(&name.as_str()))
        {
            return AppendDecision::GateConflict { differing };
        }
        return AppendDecision::PlainConflict { differing };
    }
    // All differences are VIX fields — the directional merge law
    // (`_merge_vix_backfill`): a known value wins over None; two DIFFERENT
    // known values conflict.
    let pairs = [
        ("vix_mean", existing.vix_mean, incoming.vix_mean),
        ("vix_min", existing.vix_min, incoming.vix_min),
        ("vix_max", existing.vix_max, incoming.vix_max),
    ];
    let conflicting: Vec<String> = pairs
        .iter()
        .filter(|(_, ours, theirs)| matches!((ours, theirs), (Some(a), Some(b)) if a != b))
        .map(|(name, _, _)| name.to_string())
        .collect();
    if !conflicting.is_empty() {
        return AppendDecision::VixConflict {
            differing,
            conflicting,
        };
    }
    let merged: [Option<f64>; 3] = [
        existing.vix_mean.or(incoming.vix_mean),
        existing.vix_min.or(incoming.vix_min),
        existing.vix_max.or(incoming.vix_max),
    ];
    let backfilled: Vec<String> = pairs
        .iter()
        .zip(merged.iter())
        .filter(|((_, ours, _), merged_value)| ours.is_none() && merged_value.is_some())
        .map(|((name, _, _), _)| name.to_string())
        .collect();
    if backfilled.is_empty() {
        // Directional no-op: the incoming row only turned known values into
        // None — the stored row already carries the union.
        return AppendDecision::Noop;
    }
    AppendDecision::Backfill { merged, backfilled }
}

/// `_validate_run_id` twin: `None` = valid, else the exact error message.
pub fn validate_run_id_error(run_id: &str, expected_len: usize) -> Option<String> {
    if run_id.chars().count() != expected_len {
        return Some(format!(
            "run_id must be {expected_len}-char BLAKE3 hex, got {} chars",
            run_id.chars().count()
        ));
    }
    if run_id.chars().any(|c| c.is_uppercase()) {
        return Some(format!("run_id must be lowercase hex, got {run_id:?}"));
    }
    if !run_id.chars().all(|c| c.is_ascii_hexdigit()) {
        return Some(format!("run_id {run_id:?} is not valid hex"));
    }
    None
}

/// `_encode_limb` twin over BOTH limbs: the u128 mask split into (lo, hi)
/// u64 limbs, each mapped into signed-Int64 range by subtracting 2^63
/// (bit-exact: `limb XOR 0x8000…` reinterpreted signed).
pub fn mask_to_limbs(mask: u128) -> (i64, i64) {
    let lo = (mask & 0xFFFF_FFFF_FFFF_FFFF) as u64;
    let hi = (mask >> 64) as u64;
    ((lo ^ (1u64 << 63)) as i64, (hi ^ (1u64 << 63)) as i64)
}

/// `_decode_mask` twin: reassemble the u128 from its signed limb columns.
pub fn mask_from_limbs(lo_signed: i64, hi_signed: i64) -> u128 {
    let lo = (lo_signed as u64) ^ (1u64 << 63);
    let hi = (hi_signed as u64) ^ (1u64 << 63);
    ((hi as u128) << 64) | (lo as u128)
}

/// One rank key — exact i64 for money/count columns, f64 for metric columns
/// (the Python `top_k` ranks ints EXACTLY, never coerced to float).
#[derive(Clone, Copy, Debug)]
pub enum RankKey {
    Int(i64),
    Float(f64),
}

impl RankKey {
    fn cmp_key(&self, other: &RankKey) -> std::cmp::Ordering {
        match (self, other) {
            (RankKey::Int(a), RankKey::Int(b)) => a.cmp(b),
            // No NaN can occur (row construction rejects it); -0.0 is
            // normalised away, so total_cmp == Python float comparison here.
            (RankKey::Float(a), RankKey::Float(b)) => a.total_cmp(b),
            // A column is homogeneous — mixed kinds never occur; ordered
            // defensively via the float projection.
            (RankKey::Int(a), RankKey::Float(b)) => (*a as f64).total_cmp(b),
            (RankKey::Float(a), RankKey::Int(b)) => a.total_cmp(&(*b as f64)),
        }
    }
}

/// The `top_k` ordering law: indices of the top `k` rows ranked by `keys`
/// (descending by default), ties broken by `run_id` ascending — identical to
/// the Python stable two-pass sort for any insertion order.
pub fn rank_top_k(run_ids: &[String], keys: &[RankKey], k: usize, descending: bool) -> Vec<usize> {
    let mut order: Vec<usize> = (0..run_ids.len()).collect();
    order.sort_by(|&a, &b| {
        let primary = if descending {
            keys[b].cmp_key(&keys[a])
        } else {
            keys[a].cmp_key(&keys[b])
        };
        primary.then_with(|| run_ids[a].cmp(&run_ids[b]))
    });
    order.truncate(k);
    order
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(run_id: &str) -> ResultRowFfi {
        ResultRowFfi {
            run_id: run_id.into(),
            instrument: "NSE-NIFTY".into(),
            tf: "5minute".into(),
            direction: "bull".into(),
            mode: "A".into(),
            mask_bits: (1 << 7) | (1 << 42),
            net_pnl_paisa: 150_000,
            gross_profit_paisa: 400_000,
            gross_loss_paisa: 250_000,
            max_drawdown_paisa: 90_000,
            expectancy_paisa: 1_500,
            total_trades: 100,
            winning_trades: 60,
            losing_trades: 40,
            sharpe_ratio: 1.25,
            sortino_ratio: 1.5,
            calmar_ratio: 0.8,
            profit_factor: 1.6,
            win_rate: 0.6,
            max_drawdown_pct: 0.09,
            avg_entry_price: "21500.10".into(),
            avg_exit_price: "21515.35".into(),
            deflated_sharpe: None,
            vix_mean: None,
            vix_min: None,
            vix_max: None,
            condition_names: "a AND b".into(),
        }
    }

    #[test]
    fn insert_noop_and_plain_conflict() {
        let a = row("aa");
        assert_eq!(classify_append(None, &a), AppendDecision::Insert);
        assert_eq!(classify_append(Some(&a), &a), AppendDecision::Noop);
        let mut b = a.clone();
        b.net_pnl_paisa += 1;
        assert_eq!(
            classify_append(Some(&a), &b),
            AppendDecision::PlainConflict {
                differing: vec!["net_pnl_paisa".into()]
            }
        );
    }

    #[test]
    fn gate_conflict_covers_both_nullable_shapes() {
        let a = row("aa");
        let mut b = a.clone();
        b.deflated_sharpe = Some(0.42);
        assert_eq!(
            classify_append(Some(&a), &b),
            AppendDecision::GateConflict {
                differing: vec!["deflated_sharpe".into()]
            }
        );
        let mut c = a.clone();
        c.deflated_sharpe = Some(0.41);
        let mut d = a.clone();
        d.deflated_sharpe = Some(0.42);
        assert!(matches!(
            classify_append(Some(&c), &d),
            AppendDecision::GateConflict { .. }
        ));
    }

    #[test]
    fn vix_merge_directional_law() {
        let a = row("aa");
        let mut stamped = a.clone();
        stamped.vix_mean = Some(14.5);
        stamped.vix_min = Some(12.0);
        stamped.vix_max = Some(17.25);
        // Backfill None → value.
        match classify_append(Some(&a), &stamped) {
            AppendDecision::Backfill { merged, backfilled } => {
                assert_eq!(merged, [Some(14.5), Some(12.0), Some(17.25)]);
                assert_eq!(backfilled, vec!["vix_mean", "vix_min", "vix_max"]);
            }
            other => panic!("expected Backfill, got {other:?}"),
        }
        // Reverse direction (value → None) is a directional no-op.
        assert_eq!(classify_append(Some(&stamped), &a), AppendDecision::Noop);
        // Two different known values conflict.
        let mut restamped = stamped.clone();
        restamped.vix_mean = Some(15.0);
        assert_eq!(
            classify_append(Some(&stamped), &restamped),
            AppendDecision::VixConflict {
                differing: vec!["vix_mean".into()],
                conflicting: vec!["vix_mean".into()],
            }
        );
        // A mixed VIX + metric diff is a PLAIN conflict (the non-VIX branch
        // wins before any merge is attempted).
        let mut mixed = stamped.clone();
        mixed.vix_mean = Some(15.0);
        mixed.sharpe_ratio = 9.9;
        assert_eq!(
            classify_append(Some(&stamped), &mixed),
            AppendDecision::PlainConflict {
                differing: vec!["sharpe_ratio".into(), "vix_mean".into()]
            }
        );
    }

    #[test]
    fn run_id_validation_message_law() {
        assert_eq!(validate_run_id_error(&"ab".repeat(32), 64), None);
        assert_eq!(
            validate_run_id_error("abc", 64),
            Some("run_id must be 64-char BLAKE3 hex, got 3 chars".into())
        );
        assert_eq!(
            validate_run_id_error(&"AB".repeat(32), 64),
            Some(format!(
                "run_id must be lowercase hex, got {:?}",
                "AB".repeat(32)
            ))
        );
        assert_eq!(
            validate_run_id_error(&"zz".repeat(32), 64),
            Some(format!("run_id {:?} is not valid hex", "zz".repeat(32)))
        );
    }

    #[test]
    fn limb_codec_round_trips_the_boundaries() {
        for mask in [
            0u128,
            1,
            (1 << 63) - 1,
            1 << 63,
            (1 << 64) - 1,
            1 << 64,
            u128::MAX,
            (1 << 60) - 1,
        ] {
            let (lo, hi) = mask_to_limbs(mask);
            assert_eq!(mask_from_limbs(lo, hi), mask, "mask {mask:#x}");
        }
        // The Python offset law: limb 0 encodes to -2^63, limb 2^63 to 0.
        assert_eq!(mask_to_limbs(0), (i64::MIN, i64::MIN));
        assert_eq!(mask_to_limbs(1u128 << 63), (0, i64::MIN));
    }

    #[test]
    fn top_k_exact_int_ranking_and_run_id_ties() {
        let run_ids: Vec<String> = ["bb", "aa", "cc"].iter().map(|s| s.to_string()).collect();
        // 2^60 and 2^60+1 collide as f64 — exact int ranking must split them.
        let keys = vec![
            RankKey::Int((1 << 60) + 1),
            RankKey::Int(1 << 60),
            RankKey::Int(1 << 60),
        ];
        assert_eq!(rank_top_k(&run_ids, &keys, 3, true), vec![0, 1, 2]);
        // Ties break by run_id ASC in both directions.
        let flat = vec![
            RankKey::Float(1.0),
            RankKey::Float(1.0),
            RankKey::Float(1.0),
        ];
        assert_eq!(rank_top_k(&run_ids, &flat, 2, true), vec![1, 0]);
        assert_eq!(rank_top_k(&run_ids, &flat, 2, false), vec![1, 0]);
    }
}
