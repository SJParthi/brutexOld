//! The 1-minute-path math laws (DEC-RUST-CASCADE-RESULTS-001).
//!
//! Token-for-token port of the deleted Python implementation
//! (`brutex/engine/minute_path.py` @ 415e863 — captured into the frozen
//! parity corpus BEFORE conversion): trade excursions over `[entry, exit]`
//! inclusive with the walker's exit-bar convention, the per-session
//! minute-path equity series, the INDEPENDENT paisa/pct max-drawdown fold and
//! the IST session-date axis. The Python shim keeps the type faces
//! (`MinuteIndex` / `TradePath1m` / `SessionEquityPoint` / `PathStatus`) and
//! the repr-carrying validation raises; every numeric decision lives here.
//!
//! Arithmetic envelope: bar/fill inputs are int64 paisa (the money law's
//! domain); derived equity/excursion arithmetic runs in i128 (a sum of i64
//! terms can exceed i64 — Python ints are unbounded, i128 covers every
//! reachable composition of int64-paisa inputs).

use crate::decimal::rupees_str_to_paisa;
use crate::ist::{DAY_US, ist_epoch_day, ist_midnight_us};

/// Coverage-honesty status tokens — the `PathStatus` values.
pub const STATUS_OK: &str = "ok";
pub const STATUS_NO_1M_DATA: &str = "no_1m_data";
pub const STATUS_PARTIAL: &str = "partial";

/// One instrument's parsed minute bars (the `MinuteIndex` core): parallel
/// arrays, timestamps strictly increasing (validated by the shim's
/// `from_bars` via `bars_strictly_increasing` BEFORE construction).
pub struct MinuteBars {
    pub ts_us: Vec<i64>,
    pub open_paisa: Vec<i64>,
    pub high_paisa: Vec<i64>,
    pub low_paisa: Vec<i64>,
}

impl MinuteBars {
    /// Parse rupee-string columns into paisa (the `rupees_to_paisa` law).
    pub fn from_strings(
        ts_us: Vec<i64>,
        opens: &[String],
        highs: &[String],
        lows: &[String],
    ) -> Result<Self, String> {
        let n = ts_us.len();
        if opens.len() != n || highs.len() != n || lows.len() != n {
            return Err(format!(
                "minute bars column lengths diverge: ts={} open={} high={} low={}",
                n,
                opens.len(),
                highs.len(),
                lows.len()
            ));
        }
        let parse = |col: &[String]| -> Result<Vec<i64>, String> {
            col.iter().map(|s| rupees_str_to_paisa(s)).collect()
        };
        Ok(Self {
            ts_us,
            open_paisa: parse(opens)?,
            high_paisa: parse(highs)?,
            low_paisa: parse(lows)?,
        })
    }

    pub fn len(&self) -> usize {
        self.ts_us.len()
    }

    pub fn is_empty(&self) -> bool {
        self.ts_us.is_empty()
    }
}

/// `bisect_left` over the timestamp array.
fn bisect_left(ts: &[i64], key: i64) -> usize {
    ts.partition_point(|&t| t < key)
}

/// `bisect_right` over the timestamp array.
fn bisect_right(ts: &[i64], key: i64) -> usize {
    ts.partition_point(|&t| t <= key)
}

/// `(lo, hi)` such that `ts[lo..hi]` are the bars in `[start, end]` (both
/// endpoints inclusive) — the `MinuteIndex.span_slice` law. The shim raises
/// the repr-carrying `MinutePathError` for `start > end` BEFORE calling.
pub fn span_slice(ts: &[i64], start: i64, end: i64) -> (usize, usize) {
    (bisect_left(ts, start), bisect_right(ts, end))
}

/// One trade's 1-minute-path MAE/MFE fold — `trade_excursions_1m`.
///
/// Returns `(mae_paisa, mfe_paisa, status)`; both magnitudes `None` iff the
/// span holds zero minute bars (`no_1m_data`).
pub fn trade_excursions(
    bars: &MinuteBars,
    entry_us: i64,
    exit_us: i64,
    entry_fill_paisa: i64,
    is_buy: bool,
    exit_bar_full_range: bool,
) -> (Option<i128>, Option<i128>, &'static str) {
    let (lo, hi) = span_slice(&bars.ts_us, entry_us, exit_us);
    if lo == hi {
        return (None, None, STATUS_NO_1M_DATA);
    }
    let status = if bars.ts_us[lo] == entry_us && bars.ts_us[hi - 1] == exit_us {
        STATUS_OK
    } else {
        STATUS_PARTIAL
    };
    let entry = i128::from(entry_fill_paisa);
    let mut mfe: i128 = 0;
    let mut mae: i128 = 0;
    for i in lo..hi {
        let (high_paisa, low_paisa) = if bars.ts_us[i] == exit_us && !exit_bar_full_range {
            let open = i128::from(bars.open_paisa[i]);
            (open, open)
        } else {
            (
                i128::from(bars.high_paisa[i]),
                i128::from(bars.low_paisa[i]),
            )
        };
        let (favourable, adverse) = if is_buy {
            (high_paisa - entry, entry - low_paisa)
        } else {
            (entry - low_paisa, high_paisa - entry)
        };
        mfe = mfe.max(favourable);
        mae = mae.max(adverse);
    }
    (Some(mae), Some(mfe), status)
}

/// One captured trade's equity-relevant scalars (the `EquityTradeLike`
/// projection). The shim validates chronology / non-overlap / HOLD /
/// exit-in-sessions BEFORE marshalling (its raises carry Python reprs).
pub struct EqTrade {
    pub entry_us: i64,
    pub exit_us: i64,
    pub is_buy: bool,
    pub pnl_paisa: i64,
    pub entry_paisa: i64,
}

/// One session's equity sample — `SessionEquityPoint` minus the date (the
/// caller pairs points back to its own session axis positionally).
pub struct EqPoint {
    pub close_paisa: i128,
    pub intraday_low_paisa: Option<i128>,
    pub trades_closed: i64,
}

/// `(lo, hi)` bar slice of IST calendar day *day* (half-open hi) — the
/// `_day_slice` law.
fn day_slice(ts: &[i64], day: i64) -> (usize, usize) {
    let start = ist_midnight_us(day);
    let end = start + DAY_US;
    (bisect_left(ts, start), bisect_left(ts, end))
}

/// The per-session minute-path equity series — `session_equity_series`'s
/// walk (post-validation). *sessions* are IST epoch days, strictly
/// increasing; *trades* chronological + non-overlapping.
pub fn session_equity_series(
    bars: Option<&MinuteBars>,
    trades: &[EqTrade],
    sessions: &[i64],
    start_equity_paisa: i64,
) -> (Vec<EqPoint>, &'static str) {
    let live = bars.filter(|b| !b.is_empty());
    let mut partial = false;
    let mut points: Vec<EqPoint> = Vec::with_capacity(sessions.len());
    let mut realized: i128 = i128::from(start_equity_paisa);
    let mut pointer: usize = 0;
    let n_trades = trades.len();
    for &day in sessions {
        let mut closed_today: i64 = 0;
        let mut session_low: Option<i128> = None;
        if let Some(index) = live {
            let (lo, hi) = day_slice(&index.ts_us, day);
            if lo == hi {
                partial = true;
            } else {
                let mut low = realized;
                for i in lo..hi {
                    let ts = index.ts_us[i];
                    // Flush trades fully closed strictly BEFORE this minute.
                    while pointer < n_trades && trades[pointer].exit_us < ts {
                        realized += i128::from(trades[pointer].pnl_paisa);
                        closed_today += 1;
                        pointer += 1;
                        low = low.min(realized);
                    }
                    let mut candidate = realized;
                    if pointer < n_trades {
                        let trade = &trades[pointer];
                        if trade.entry_us <= ts && ts <= trade.exit_us {
                            let entry = i128::from(trade.entry_paisa);
                            let mark = if trade.is_buy {
                                i128::from(index.low_paisa[i]) - entry
                            } else {
                                entry - i128::from(index.high_paisa[i])
                            };
                            candidate = realized + mark;
                            if ts == trade.exit_us {
                                realized += i128::from(trade.pnl_paisa);
                                closed_today += 1;
                                pointer += 1;
                            }
                        }
                    }
                    low = low.min(candidate).min(realized);
                }
                session_low = Some(low);
            }
        }
        // End-of-session flush: exits dated today whose minute bar was
        // absent (partial 1m data) or when no index exists at all.
        while pointer < n_trades && ist_epoch_day(trades[pointer].exit_us) <= day {
            realized += i128::from(trades[pointer].pnl_paisa);
            closed_today += 1;
            pointer += 1;
        }
        points.push(EqPoint {
            close_paisa: realized,
            intraday_low_paisa: session_low,
            trades_closed: closed_today,
        });
    }
    let status = if live.is_none() {
        STATUS_NO_1M_DATA
    } else if partial {
        STATUS_PARTIAL
    } else {
        STATUS_OK
    };
    (points, status)
}

/// The independent paisa/pct max-drawdown fold — `minute_path_max_drawdown`
/// (post-validation; the shim rejects a non-positive `start_equity_paisa`).
pub fn max_drawdown(
    closes: &[i128],
    lows: &[Option<i128>],
    start_equity_paisa: Option<i128>,
) -> (i128, f64) {
    let mut peak: Option<i128> = start_equity_paisa;
    let mut max_dd: i128 = 0;
    let mut max_dd_pct: f64 = 0.0;
    for (close, low) in closes.iter().zip(lows) {
        let mut trough = *close;
        if let Some(l) = low {
            trough = trough.min(*l);
        }
        if let Some(p) = peak {
            let drop = p - trough;
            max_dd = max_dd.max(drop);
            if p > 0 && drop > 0 {
                max_dd_pct = max_dd_pct.max(drop as f64 / p as f64);
            }
        }
        peak = Some(match peak {
            None => *close,
            Some(p) => p.max(*close),
        });
    }
    (max_dd, max_dd_pct)
}

/// The sorted unique IST calendar days of a bar-timestamp array —
/// `ist_session_dates`.
pub fn session_dates(ts_us: &[i64]) -> Vec<i64> {
    let mut days: Vec<i64> = ts_us.iter().map(|&t| ist_epoch_day(t)).collect();
    days.sort_unstable();
    days.dedup();
    days
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ist::days_from_civil;

    fn ist_us(day: i64, hh: i64, mm: i64) -> i64 {
        ist_midnight_us(day) + (hh * 3600 + mm * 60) * 1_000_000
    }

    fn bars() -> MinuteBars {
        // 09:16..09:20 IST of epoch day D (mirrors the corpus `_bars_regular`).
        let d = days_from_civil(2024, 1, 2);
        MinuteBars::from_strings(
            (16..=20).map(|mm| ist_us(d, 9, mm)).collect(),
            &[
                "100.00".into(),
                "100.10".into(),
                "101.90".into(),
                "98.10".into(),
                "100.00".into(),
            ],
            &[
                "101.00".into(),
                "103.00".into(),
                "102.50".into(),
                "100.75".into(),
                "102.40".into(),
            ],
            &[
                "99.00".into(),
                "98.50".into(),
                "97.25".into(),
                "98.00".into(),
                "96.80".into(),
            ],
        )
        .expect("parse")
    }

    #[test]
    fn excursions_buy_full_range() {
        let b = bars();
        let d = days_from_civil(2024, 1, 2);
        let (mae, mfe, status) =
            trade_excursions(&b, ist_us(d, 9, 16), ist_us(d, 9, 20), 10_000, true, true);
        // Highest high 103.00 → +300; lowest low 96.80 → −320.
        assert_eq!((mae, mfe, status), (Some(320), Some(300), STATUS_OK));
    }

    #[test]
    fn excursions_open_only_exit_bar() {
        let b = bars();
        let d = days_from_civil(2024, 1, 2);
        let (mae, mfe, status) =
            trade_excursions(&b, ist_us(d, 9, 16), ist_us(d, 9, 20), 10_000, true, false);
        // Exit bar contributes only its open 100.00; the 9:17 high 103.00
        // stays the MFE, the 9:19 low 98.00 the MAE.
        assert_eq!((mae, mfe, status), (Some(275), Some(300), STATUS_OK));
    }

    #[test]
    fn excursions_statuses() {
        let b = bars();
        let d = days_from_civil(2024, 1, 2);
        let (mae, mfe, status) =
            trade_excursions(&b, ist_us(d, 11, 0), ist_us(d, 11, 5), 10_000, true, true);
        assert_eq!((mae, mfe, status), (None, None, STATUS_NO_1M_DATA));
        let (_, _, status) =
            trade_excursions(&b, ist_us(d, 9, 15), ist_us(d, 9, 18), 10_000, true, true);
        assert_eq!(status, STATUS_PARTIAL);
    }

    #[test]
    fn equity_series_no_index_is_close_only() {
        let d = days_from_civil(2024, 1, 2);
        let trades = vec![EqTrade {
            entry_us: ist_us(d, 9, 16),
            exit_us: ist_us(d, 9, 18),
            is_buy: true,
            pnl_paisa: -5_000,
            entry_paisa: 10_000,
        }];
        let (points, status) = session_equity_series(None, &trades, &[d], 1_000_000);
        assert_eq!(status, STATUS_NO_1M_DATA);
        assert_eq!(points.len(), 1);
        assert_eq!(points[0].close_paisa, 995_000);
        assert_eq!(points[0].intraday_low_paisa, None);
        assert_eq!(points[0].trades_closed, 1);
    }

    #[test]
    fn equity_series_marks_open_trade_at_worst_price() {
        let b = bars();
        let d = days_from_civil(2024, 1, 2);
        let trades = vec![EqTrade {
            entry_us: ist_us(d, 9, 16),
            exit_us: ist_us(d, 9, 18),
            is_buy: true,
            pnl_paisa: -200,
            entry_paisa: 10_000,
        }];
        let (points, status) = session_equity_series(Some(&b), &trades, &[d], 1_000_000);
        assert_eq!(status, STATUS_OK);
        // Worst open mark: 9:18 low 97.25 → −275 against the base.
        assert_eq!(points[0].intraday_low_paisa, Some(999_725));
        assert_eq!(points[0].close_paisa, 999_800);
    }

    #[test]
    fn drawdown_tracks_paisa_and_pct_independently() {
        let closes = vec![100_i128, 1_000, 1_000];
        let lows = vec![Some(40_i128), Some(900), Some(850)];
        let (dd, pct) = max_drawdown(&closes, &lows, Some(100));
        // Absolute max drop: 1000 → 850 = 150; pct max: 60/100 = 0.6.
        assert_eq!(dd, 150);
        assert!((pct - 0.6).abs() < 1e-12);
    }

    #[test]
    fn session_dates_sorted_unique() {
        let d = days_from_civil(2024, 1, 2);
        let ts = vec![ist_us(d + 1, 9, 16), ist_us(d, 9, 16), ist_us(d, 9, 17)];
        assert_eq!(session_dates(&ts), vec![d, d + 1]);
    }
}
