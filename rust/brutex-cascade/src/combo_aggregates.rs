//! The per-combo aggregate fold (DEC-RUST-CASCADE-RESULTS-001).
//!
//! Token-for-token port of the deleted `brutex/engine/combo_aggregates.py`
//! fold (@ 415e863, captured into the frozen parity corpus BEFORE
//! conversion): nearest-rank percentiles, floor-mean integer stats, the
//! `min ≤ mean ≤ max`-clamped float stats, the worst-5-days stress view, the
//! VIX win/loss stats + [p10, p90] band, the pinned ≥ 0.8 one-regime-wonder
//! rule (year axis before trend axis; the `unknown` trend bucket excluded),
//! entry-hour + event-day lenses, exit-reason tallies, the
//! consecutive-runs fold and the compute-module `win_loss_ratio` /
//! `recovery_factor` laws. The lens JSON renders are byte-identical to
//! CPython `json.dumps(sort_keys=True, separators=(",", ":"))` via
//! [`crate::pyjson`].
//!
//! The Python shim keeps the `ComboAggregate` frozen dataclass (whose
//! `__post_init__` invariants re-validate every fold output), the
//! `run_id`-mismatch refusal and the record rendering; every numeric /
//! ordering / JSON decision lives here.

use std::collections::BTreeMap;

use crate::ist::{iso_date, ist_epoch_day, ist_hour, year_of_epoch_day};
use crate::minute_path::max_drawdown;
use crate::pyjson::{dumps_pairs, dumps_str_int_map};

/// The pinned one-regime-wonder threshold (`REGIME_WONDER_SHARE`).
pub const REGIME_WONDER_SHARE: f64 = 0.8;

/// The worst-days stress-view depth (`WORST_DAYS_LIMIT`).
pub const WORST_DAYS_LIMIT: usize = 5;

/// The unlabelled-trend bucket token (`TREND_UNKNOWN`).
pub const TREND_UNKNOWN: &str = "unknown";

/// The append-only walker exit-reason vocabulary (`trade_sim.EXIT_REASONS`
/// order) — an unknown reason is the same loud `KeyError` class the Python
/// tally raised.
pub const EXIT_REASONS: [&str; 6] = [
    "stop_loss",
    "take_profit",
    "forced_1520",
    "signal",
    "end_of_data",
    "session_gap",
];

/// One captured trade's fold-relevant scalars (the `CapturedTrade`
/// projection the shim marshals).
pub struct AggTrade {
    pub entry_us: i64,
    pub exit_us: i64,
    pub pnl_paisa: i64,
    pub mae_paisa: i64,
    pub mfe_paisa: i64,
    pub mae_1m_paisa: Option<i64>,
    pub mfe_1m_paisa: Option<i64>,
    pub path_1m_ok: bool,
    pub exit_reason: String,
    /// `float(t.vix_entry)` — the Python law's own Decimal→float conversion,
    /// executed at the marshalling boundary (identical value).
    pub vix_entry: Option<f64>,
}

/// The fold's computed outputs — the `ComboAggregate` fields the fold owns
/// (the shim adds `run_id` / `composite_score` / `fingerprint` /
/// `equity_status` pass-throughs and re-validates via the dataclass).
#[derive(Debug)]
pub struct AggOut {
    pub n_trades: i64,
    pub zero_trades: bool,
    pub mae_stats: Option<(i128, i128, i128)>,
    pub mfe_stats: Option<(i128, i128, i128)>,
    pub mae_1m_stats: Option<(i128, i128, i128)>,
    pub mfe_1m_stats: Option<(i128, i128, i128)>,
    pub trades_with_1m: i64,
    pub max_dd_minute_path_paisa: Option<i128>,
    pub max_dd_minute_path_pct: Option<f64>,
    pub worst5_days_json: String,
    pub vix_win: Option<(f64, f64, f64)>,
    pub vix_loss: Option<(f64, f64, f64)>,
    pub vix_band: Option<(f64, f64)>,
    pub vix_stamped_trades: i64,
    pub pnl_by_year_json: String,
    pub pnl_by_trend_json: String,
    pub one_regime_wonder: bool,
    pub regime_wonder_axis: Option<&'static str>,
    pub entry_hour_hist_json: String,
    pub event_day_counts_json: String,
    pub event_day_pnl_json: String,
    pub exit_tally: [i64; 6],
    pub max_consecutive_losses: i64,
    pub max_consecutive_wins: i64,
    pub win_loss_ratio: Option<f64>,
    pub recovery_factor: Option<f64>,
}

/// The nearest-rank index law: `rank = ceil(pct·n/100)` via exact integer
/// arithmetic (`-(-pct·n // 100)`), returned as the 0-based index
/// `rank - 1`. Validity (`n > 0`, `1 <= pct <= 100`) is the caller's check
/// (the shim raises the exact `ComboAggregateError` messages).
pub fn nearest_rank_index(n: usize, pct: i64) -> usize {
    let n = n as i64;
    let rank = -(-pct * n).div_euclid(100);
    (rank - 1) as usize
}

/// `(floor_mean, max, nearest_rank_p95)` over non-empty int magnitudes —
/// `_int_stats` (floor division: Python `//` == `div_euclid` for the
/// positive divisor `len`).
pub fn int_stats(values: &[i64]) -> (i128, i128, i128) {
    let mut ordered: Vec<i128> = values.iter().map(|&v| i128::from(v)).collect();
    ordered.sort_unstable();
    let n = ordered.len();
    let sum: i128 = ordered.iter().sum();
    (
        sum.div_euclid(n as i128),
        ordered[n - 1],
        ordered[nearest_rank_index(n, 95)],
    )
}

/// `(min, clamped_mean, max)` over non-empty full-precision floats —
/// `_float_stats` (the ULP clamp law: left-fold `sum(values) / n`, clamped
/// into `[min(values), max(values)]` — Python's `min`/`max` scan, not a
/// sortedness assumption).
pub fn float_stats(values: &[f64]) -> (f64, f64, f64) {
    let mut lo = values[0];
    let mut hi = values[0];
    for &v in &values[1..] {
        if v < lo {
            lo = v;
        }
        if v > hi {
            hi = v;
        }
    }
    let sum: f64 = values.iter().sum();
    let mean = sum / values.len() as f64;
    (lo, mean.max(lo).min(hi), hi)
}

/// The pinned share rule — `_one_regime_dominates`: max positive regime P&L
/// ≥ 80% of the sum of positives (f64 division exactly as Python's int/int
/// true division for the < 2^53 domain).
fn one_regime_dominates<'a>(pnls: impl Iterator<Item = &'a i128>) -> bool {
    let positives: Vec<i128> = pnls.filter(|&&v| v > 0).copied().collect();
    if positives.is_empty() {
        return false;
    }
    let max = *positives.iter().max().expect("non-empty");
    let sum: i128 = positives.iter().sum();
    (max as f64) / (sum as f64) >= REGIME_WONDER_SHARE
}

/// Longest losing / winning streaks — `_consecutive_runs` (a scratch
/// `pnl == 0` breaks BOTH).
pub fn consecutive_runs(pnls: impl Iterator<Item = i64>) -> (i64, i64) {
    let (mut max_losses, mut max_wins, mut cur_losses, mut cur_wins) = (0_i64, 0_i64, 0_i64, 0_i64);
    for pnl in pnls {
        if pnl < 0 {
            cur_losses += 1;
            cur_wins = 0;
        } else if pnl > 0 {
            cur_wins += 1;
            cur_losses = 0;
        } else {
            cur_losses = 0;
            cur_wins = 0;
        }
        max_losses = max_losses.max(cur_losses);
        max_wins = max_wins.max(cur_wins);
    }
    (max_losses, max_wins)
}

/// The worst-days view — `_worst_days_json`: ≤ 5 lowest per-session P&L
/// sums over exit IST dates, most negative first, ties by date ascending.
fn worst_days_json(trades: &[AggTrade]) -> String {
    let mut by_day: BTreeMap<String, i128> = BTreeMap::new();
    for trade in trades {
        let key = iso_date(ist_epoch_day(trade.exit_us));
        *by_day.entry(key).or_insert(0) += i128::from(trade.pnl_paisa);
    }
    let mut items: Vec<(String, i128)> = by_day.into_iter().collect();
    items.sort_by(|a, b| (a.1, a.0.as_str()).cmp(&(b.1, b.0.as_str())));
    items.truncate(WORST_DAYS_LIMIT);
    dumps_pairs(&items)
}

/// The whole fold — `aggregate_combo`'s computed body (post the shim's
/// `run_id`-mismatch refusal). *trend_by_day* / *event_flags_by_day* are the
/// lens maps keyed by IST epoch day; *eq_closes*/*eq_lows* the capture's
/// session equity series. Returns `Err(reason)` only for an exit-reason
/// token outside [`EXIT_REASONS`] — the Python tally's `KeyError` class.
#[allow(clippy::too_many_arguments)]
pub fn aggregate_fold(
    trades: &[AggTrade],
    trend_by_day: &BTreeMap<i64, String>,
    event_flags_by_day: &BTreeMap<i64, Vec<String>>,
    eq_closes: &[i128],
    eq_lows: &[Option<i128>],
    row_total_trades: i64,
    row_net_pnl_paisa: i64,
    row_max_drawdown_paisa: i64,
    parity_ok: bool,
    start_equity_paisa: i64,
) -> Result<AggOut, String> {
    let n_trades = trades.len();
    let stats_live = parity_ok && n_trades > 0;

    let mut out = AggOut {
        n_trades: n_trades as i64,
        zero_trades: row_total_trades == 0,
        mae_stats: None,
        mfe_stats: None,
        mae_1m_stats: None,
        mfe_1m_stats: None,
        trades_with_1m: 0,
        max_dd_minute_path_paisa: None,
        max_dd_minute_path_pct: None,
        worst5_days_json: "[]".to_string(),
        vix_win: None,
        vix_loss: None,
        vix_band: None,
        vix_stamped_trades: 0,
        pnl_by_year_json: "{}".to_string(),
        pnl_by_trend_json: "{}".to_string(),
        one_regime_wonder: false,
        regime_wonder_axis: None,
        entry_hour_hist_json: "{}".to_string(),
        event_day_counts_json: "{}".to_string(),
        event_day_pnl_json: "{}".to_string(),
        exit_tally: [0; 6],
        max_consecutive_losses: 0,
        max_consecutive_wins: 0,
        win_loss_ratio: None,
        recovery_factor: None,
    };

    if stats_live {
        out.mae_stats = Some(int_stats(
            &trades.iter().map(|t| t.mae_paisa).collect::<Vec<_>>(),
        ));
        out.mfe_stats = Some(int_stats(
            &trades.iter().map(|t| t.mfe_paisa).collect::<Vec<_>>(),
        ));
        let ok_trades: Vec<&AggTrade> = trades.iter().filter(|t| t.path_1m_ok).collect();
        out.trades_with_1m = ok_trades.len() as i64;
        if ok_trades.len() == n_trades {
            // PathStatus.OK guarantees the 1m pair is present (the
            // CapturedTrade invariant — enforced by the dataclass before
            // marshalling); a missing pair here is a broken shim, refused.
            let mae_1m: Option<Vec<i64>> = ok_trades.iter().map(|t| t.mae_1m_paisa).collect();
            let mfe_1m: Option<Vec<i64>> = ok_trades.iter().map(|t| t.mfe_1m_paisa).collect();
            match (mae_1m, mfe_1m) {
                (Some(mae), Some(mfe)) => {
                    out.mae_1m_stats = Some(int_stats(&mae));
                    out.mfe_1m_stats = Some(int_stats(&mfe));
                }
                _ => return Err("path_1m_status OK trade lacks its 1m pair".to_string()),
            }
        }
        let (dd, pct) = max_drawdown(eq_closes, eq_lows, Some(i128::from(start_equity_paisa)));
        out.max_dd_minute_path_paisa = Some(dd);
        out.max_dd_minute_path_pct = Some(pct);
        out.worst5_days_json = worst_days_json(trades);

        let stamped: Vec<(f64, i64)> = trades
            .iter()
            .filter_map(|t| t.vix_entry.map(|v| (v, t.pnl_paisa)))
            .collect();
        out.vix_stamped_trades = stamped.len() as i64;
        let mut win_vix: Vec<f64> = stamped
            .iter()
            .filter(|(_, pnl)| *pnl > 0)
            .map(|(v, _)| *v)
            .collect();
        win_vix.sort_by(f64::total_cmp);
        let mut loss_vix: Vec<f64> = stamped
            .iter()
            .filter(|(_, pnl)| *pnl < 0)
            .map(|(v, _)| *v)
            .collect();
        loss_vix.sort_by(f64::total_cmp);
        if !win_vix.is_empty() {
            out.vix_win = Some(float_stats(&win_vix));
            out.vix_band = Some((
                win_vix[nearest_rank_index(win_vix.len(), 10)],
                win_vix[nearest_rank_index(win_vix.len(), 90)],
            ));
        }
        if !loss_vix.is_empty() {
            out.vix_loss = Some(float_stats(&loss_vix));
        }

        let mut net_pnl: i128 = 0;
        let mut pnl_by_year: BTreeMap<String, i128> = BTreeMap::new();
        let mut pnl_by_trend: BTreeMap<String, i128> = BTreeMap::new();
        let mut hour_hist: BTreeMap<String, i128> = BTreeMap::new();
        let mut event_counts: BTreeMap<String, i128> = BTreeMap::new();
        let mut event_pnl: BTreeMap<String, i128> = BTreeMap::new();
        for trade in trades {
            let pnl = i128::from(trade.pnl_paisa);
            net_pnl += pnl;
            let slot = EXIT_REASONS
                .iter()
                .position(|&r| r == trade.exit_reason)
                .ok_or_else(|| trade.exit_reason.clone())?;
            out.exit_tally[slot] += 1;
            let year_key = year_of_epoch_day(ist_epoch_day(trade.exit_us)).to_string();
            *pnl_by_year.entry(year_key).or_insert(0) += pnl;
            let entry_day = ist_epoch_day(trade.entry_us);
            let trend_key = trend_by_day
                .get(&entry_day)
                .map_or(TREND_UNKNOWN, String::as_str);
            *pnl_by_trend.entry(trend_key.to_string()).or_insert(0) += pnl;
            let hour_key = ist_hour(trade.entry_us).to_string();
            *hour_hist.entry(hour_key).or_insert(0) += 1;
            if let Some(flags) = event_flags_by_day.get(&entry_day) {
                for flag in flags {
                    *event_counts.entry(flag.clone()).or_insert(0) += 1;
                    *event_pnl.entry(flag.clone()).or_insert(0) += pnl;
                }
            }
        }
        if net_pnl > 0 {
            if one_regime_dominates(pnl_by_year.values()) {
                out.one_regime_wonder = true;
                out.regime_wonder_axis = Some("year");
            } else if one_regime_dominates(
                pnl_by_trend
                    .iter()
                    .filter(|(key, _)| key.as_str() != TREND_UNKNOWN)
                    .map(|(_, value)| value),
            ) {
                out.one_regime_wonder = true;
                out.regime_wonder_axis = Some("trend");
            }
        }
        out.pnl_by_year_json = dumps_str_int_map(&pnl_by_year);
        out.pnl_by_trend_json = dumps_str_int_map(&pnl_by_trend);
        out.entry_hour_hist_json = dumps_str_int_map(&hour_hist);
        out.event_day_counts_json = dumps_str_int_map(&event_counts);
        out.event_day_pnl_json = dumps_str_int_map(&event_pnl);

        let winners = trades.iter().filter(|t| t.pnl_paisa > 0).count() as i128;
        let losers = trades.iter().filter(|t| t.pnl_paisa < 0).count() as i128;
        let gross_profit: i128 = trades
            .iter()
            .filter(|t| t.pnl_paisa > 0)
            .map(|t| i128::from(t.pnl_paisa))
            .sum();
        let gross_loss_abs: i128 = -trades
            .iter()
            .filter(|t| t.pnl_paisa < 0)
            .map(|t| i128::from(t.pnl_paisa))
            .sum::<i128>();
        let avg_win = if winners > 0 {
            gross_profit.div_euclid(winners)
        } else {
            0
        };
        let avg_loss_abs = if losers > 0 {
            gross_loss_abs.div_euclid(losers)
        } else {
            0
        };
        if avg_loss_abs > 0 {
            out.win_loss_ratio = Some(avg_win as f64 / avg_loss_abs as f64);
        }
        if row_max_drawdown_paisa != 0 {
            out.recovery_factor = Some(row_net_pnl_paisa as f64 / row_max_drawdown_paisa as f64);
        }
    }

    let (max_losses, max_wins) = consecutive_runs(trades.iter().map(|t| t.pnl_paisa));
    out.max_consecutive_losses = max_losses;
    out.max_consecutive_wins = max_wins;
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ist::{days_from_civil, ist_midnight_us};

    fn trade(day_offset: i64, pnl: i64, reason: &str, vix: Option<f64>) -> AggTrade {
        let d = days_from_civil(2024, 1, 2) + day_offset;
        let entry = ist_midnight_us(d) + (9 * 3600 + 16 * 60) * 1_000_000;
        AggTrade {
            entry_us: entry,
            exit_us: entry + 60 * 1_000_000,
            pnl_paisa: pnl,
            mae_paisa: 10,
            mfe_paisa: 20,
            mae_1m_paisa: None,
            mfe_1m_paisa: None,
            path_1m_ok: false,
            exit_reason: reason.to_string(),
            vix_entry: vix,
        }
    }

    #[test]
    fn nearest_rank_index_law() {
        // n=20, p95 → rank 19 → index 18; p10 → rank 2 → index 1.
        assert_eq!(nearest_rank_index(20, 95), 18);
        assert_eq!(nearest_rank_index(20, 10), 1);
        assert_eq!(nearest_rank_index(1, 95), 0);
        assert_eq!(nearest_rank_index(3, 33), 0);
        assert_eq!(nearest_rank_index(3, 34), 1);
        assert_eq!(nearest_rank_index(3, 100), 2);
    }

    #[test]
    fn int_stats_floor_mean() {
        // Python: sum // n floors toward -inf: (-3 + 2) // 2 == -1.
        assert_eq!(int_stats(&[2, -3]), (-1, 2, 2));
        assert_eq!(int_stats(&[10, 20, 30]), (20, 30, 30));
    }

    #[test]
    fn float_stats_ulp_clamp() {
        // 3 × 6.15 sums to 18.450000000000003 → raw mean 6.150000000000001,
        // clamped back to 6.15.
        let (lo, mean, hi) = float_stats(&[6.15, 6.15, 6.15]);
        assert_eq!((lo, mean, hi), (6.15, 6.15, 6.15));
    }

    #[test]
    fn consecutive_runs_scratch_breaks_both() {
        assert_eq!(
            consecutive_runs([-1, -1, 0, 5, 5, 5, -1].into_iter()),
            (2, 3)
        );
    }

    #[test]
    fn zero_trades_is_the_null_record() {
        let out = aggregate_fold(
            &[],
            &BTreeMap::new(),
            &BTreeMap::new(),
            &[],
            &[],
            0,
            0,
            0,
            true,
            1_000_000,
        )
        .expect("fold");
        assert!(out.zero_trades);
        assert_eq!(out.n_trades, 0);
        assert_eq!(out.mae_stats, None);
        assert_eq!(out.worst5_days_json, "[]");
        assert_eq!(out.pnl_by_year_json, "{}");
        assert_eq!(out.exit_tally, [0; 6]);
        assert_eq!(out.win_loss_ratio, None);
        assert_eq!(out.recovery_factor, None);
    }

    #[test]
    fn year_wonder_fires_before_trend() {
        let trades = vec![
            trade(0, 90_000, "signal", Some(12.5)),
            trade(1, 5_000, "signal", None),
            trade(2, -1_000, "stop_loss", None),
        ];
        let out = aggregate_fold(
            &trades,
            &BTreeMap::new(),
            &BTreeMap::new(),
            &[],
            &[],
            3,
            94_000,
            1_000,
            true,
            1_000_000,
        )
        .expect("fold");
        // One year holds all positives → wonder on the year axis.
        assert!(out.one_regime_wonder);
        assert_eq!(out.regime_wonder_axis, Some("year"));
        assert_eq!(out.vix_stamped_trades, 1);
        assert_eq!(out.exit_tally, [1, 0, 0, 2, 0, 0]);
        assert_eq!(out.recovery_factor, Some(94.0));
    }

    #[test]
    fn unknown_exit_reason_is_refused() {
        let trades = vec![trade(0, 1, "weird", None)];
        let err = aggregate_fold(
            &trades,
            &BTreeMap::new(),
            &BTreeMap::new(),
            &[],
            &[],
            1,
            1,
            0,
            true,
            1_000_000,
        )
        .unwrap_err();
        assert_eq!(err, "weird");
    }
}
