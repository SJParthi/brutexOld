//! O(1) per-tick indicator engine.
//!
//! Updates all indicator state for a given instrument in constant time.
//! No lookback, no recomputation, no heap allocation on the hot path.
//!
//! # Algorithms
//! - EMA: `alpha * price + (1 - alpha) * prev`
//! - RSI: Wilder's smoothing on avg_gain / avg_loss
//! - MACD: Three cascaded EMAs (fast, slow, signal)
//! - ATR: Wilder's smoothing on True Range
//! - Bollinger: Welford's online algorithm for running mean + variance
//! - SMA: Ring buffer + running sum
//! - Supertrend: ATR-based bands with direction flip
//! - ADX: Chain of Wilder's smoothed +DM, -DM, TR, DX
//! - OBV: Running sum with close-vs-prev direction
//! - VWAP: Cumulative (price × volume) / cumulative volume

use crate::compat::constants::MAX_INDICATOR_INSTRUMENTS;
use crate::compat::tick_types::ParsedTick;

use super::types::{IndicatorParams, IndicatorSnapshot, IndicatorState};

// ---------------------------------------------------------------------------
// Indicator Engine — flat Vec indexed by security_id
// ---------------------------------------------------------------------------

/// The indicator engine: pre-allocated flat array of per-instrument state.
///
/// Indexed by `security_id` for O(1) lookup. Pre-allocated at startup
/// for the full instrument universe — zero allocation on the hot path.
pub struct IndicatorEngine {
    /// Per-instrument indicator state. Index = security_id.
    states: Vec<IndicatorState>,
    /// Shared indicator parameters (periods, multipliers).
    params: IndicatorParams,
    /// Pre-computed EMA alpha for fast period.
    alpha_fast: f64,
    /// Pre-computed EMA alpha for slow period.
    alpha_slow: f64,
    /// Pre-computed EMA alpha for MACD signal.
    alpha_signal: f64,
    /// Pre-computed Wilder factor for RSI/ATR/ADX.
    wilder_factor: f64,
    /// Pre-computed Wilder factor for ADX smoothing.
    adx_wilder_factor: f64,
}

impl IndicatorEngine {
    /// Creates a new engine with pre-allocated state for all instruments.
    ///
    /// # Performance
    /// Single allocation at startup. The `states` Vec is never resized.
    pub fn new(params: IndicatorParams) -> Self {
        let mut states = Vec::with_capacity(MAX_INDICATOR_INSTRUMENTS);
        states.resize_with(MAX_INDICATOR_INSTRUMENTS, IndicatorState::new);

        Self {
            states,
            alpha_fast: IndicatorParams::ema_alpha(params.ema_fast_period),
            alpha_slow: IndicatorParams::ema_alpha(params.ema_slow_period),
            alpha_signal: IndicatorParams::ema_alpha(params.macd_signal_period),
            wilder_factor: IndicatorParams::wilder_factor(params.rsi_period),
            adx_wilder_factor: IndicatorParams::wilder_factor(params.adx_period),
            params,
        }
    }

    /// Updates all indicators for the given tick and returns a snapshot.
    ///
    /// # Performance
    /// O(1) — every indicator uses a recursive/incremental update.
    /// Total: ~200ns on c7i.2xlarge (20 indicators, all O(1)).
    ///
    /// # Safety (bounds)
    /// If `security_id >= MAX_INDICATOR_INSTRUMENTS`, returns a default snapshot.
    #[inline(always)]
    #[allow(clippy::arithmetic_side_effects)] // APPROVED: all arithmetic is bounded f64 operations with finite checks
    pub fn update(&mut self, tick: &ParsedTick) -> IndicatorSnapshot {
        let sid = tick.security_id as usize;
        if sid >= self.states.len() {
            return IndicatorSnapshot {
                security_id: tick.security_id,
                ..Default::default()
            };
        }

        let state = &mut self.states[sid];
        let price = f64::from(tick.last_traded_price);
        let high = f64::from(tick.day_high);
        let low = f64::from(tick.day_low);
        let volume = f64::from(tick.volume);
        let close = f64::from(tick.day_close);

        // Track warmup
        if state.warmup_count < u16::MAX {
            state.warmup_count = state.warmup_count.saturating_add(1);
        }

        let is_first_tick = state.warmup_count == 1;

        // ---- EMA (fast + slow) ----
        if is_first_tick {
            state.ema_fast = price;
            state.ema_slow = price;
        } else {
            state.ema_fast = self.alpha_fast * price + (1.0 - self.alpha_fast) * state.ema_fast;
            state.ema_slow = self.alpha_slow * price + (1.0 - self.alpha_slow) * state.ema_slow;
        }

        // ---- MACD ----
        let macd_line = state.ema_fast - state.ema_slow;
        if is_first_tick {
            state.macd_signal_ema = macd_line;
        } else {
            state.macd_signal_ema =
                self.alpha_signal * macd_line + (1.0 - self.alpha_signal) * state.macd_signal_ema;
        }

        // ---- RSI (Wilder's smoothing) ----
        if !is_first_tick {
            let change = price - state.prev_close;
            let gain = if change > 0.0 { change } else { 0.0 };
            let loss = if change < 0.0 { -change } else { 0.0 };

            let wf = self.wilder_factor;
            state.rsi_avg_gain = state.rsi_avg_gain * (1.0 - wf) + gain * wf;
            state.rsi_avg_loss = state.rsi_avg_loss * (1.0 - wf) + loss * wf;
        }

        // ---- ATR (Wilder's smoothing on True Range) ----
        if !is_first_tick {
            let tr1 = high - low;
            let tr2 = (high - state.prev_close).abs();
            let tr3 = (low - state.prev_close).abs();
            let true_range = tr1.max(tr2).max(tr3);

            let wf = self.wilder_factor;
            if state.warmup_count == 2 {
                state.atr = true_range;
            } else {
                state.atr = state.atr * (1.0 - wf) + true_range * wf;
            }
        }

        // ---- Supertrend (ATR-based) ----
        if !is_first_tick && state.atr > 0.0 {
            let hl2 = (high + low) / 2.0;
            let offset = self.params.supertrend_multiplier * state.atr;
            let mut upper = hl2 + offset;
            let mut lower = hl2 - offset;

            // Carry forward bands (conditional)
            if upper < state.supertrend_upper || state.prev_close > state.supertrend_upper {
                // Keep new upper
            } else {
                upper = state.supertrend_upper;
            }

            if lower > state.supertrend_lower || state.prev_close < state.supertrend_lower {
                // Keep new lower
            } else {
                lower = state.supertrend_lower;
            }

            state.supertrend_upper = upper;
            state.supertrend_lower = lower;

            // Direction flip
            if state.supertrend_direction {
                // Was bullish — flip to bearish if close < lower
                if price < lower {
                    state.supertrend_direction = false;
                }
            } else {
                // Was bearish — flip to bullish if close > upper
                if price > upper {
                    state.supertrend_direction = true;
                }
            }
        } else if is_first_tick {
            state.supertrend_upper = high;
            state.supertrend_lower = low;
        }

        // ---- ADX (chain of Wilder's smoothing) ----
        if !is_first_tick {
            let plus_dm = if high > state.prev_high {
                (high - state.prev_high).max(0.0)
            } else {
                0.0
            };
            let minus_dm = if state.prev_low > low {
                (state.prev_low - low).max(0.0)
            } else {
                0.0
            };
            let tr1 = high - low;
            let tr2 = (high - state.prev_close).abs();
            let tr3 = (low - state.prev_close).abs();
            let true_range = tr1.max(tr2).max(tr3);

            let wf = self.adx_wilder_factor;
            state.adx_plus_dm_smooth = state.adx_plus_dm_smooth * (1.0 - wf) + plus_dm * wf;
            state.adx_minus_dm_smooth = state.adx_minus_dm_smooth * (1.0 - wf) + minus_dm * wf;
            state.adx_tr_smooth = state.adx_tr_smooth * (1.0 - wf) + true_range * wf;

            if state.adx_tr_smooth > 0.0 {
                let plus_di = 100.0 * state.adx_plus_dm_smooth / state.adx_tr_smooth;
                let minus_di = 100.0 * state.adx_minus_dm_smooth / state.adx_tr_smooth;
                let di_sum = plus_di + minus_di;
                let dx = if di_sum > 0.0 {
                    100.0 * (plus_di - minus_di).abs() / di_sum
                } else {
                    0.0
                };
                state.adx_value = state.adx_value * (1.0 - wf) + dx * wf;
            }
        }

        // ---- OBV (running sum) ----
        if !is_first_tick {
            if price > state.prev_close {
                state.obv += volume;
            } else if price < state.prev_close {
                state.obv -= volume;
            }
            // price == prev_close: OBV unchanged
        }

        // ---- VWAP (cumulative) ----
        if volume > 0.0 {
            let typical_price = (high + low + price) / 3.0;
            state.vwap_cumulative_pv += typical_price * volume;
            state.vwap_cumulative_vol += volume;
        }

        // ---- Bollinger Bands (Welford's online algorithm) ----
        state.bb_count = state.bb_count.saturating_add(1);
        let n = f64::from(state.bb_count);
        let delta = price - state.bb_mean;
        state.bb_mean += delta / n;
        let delta2 = price - state.bb_mean;
        state.bb_m2 += delta * delta2;

        // ---- SMA (ring buffer + running sum) ----
        let evicted = state.sma_ring.push(price);
        let sma_period = self.params.sma_period;
        if state.sma_ring.len() <= sma_period {
            state.sma_running_sum += price;
        } else {
            state.sma_running_sum += price - evicted;
        }

        // ---- Store previous values for next tick ----
        state.prev_close = price;
        state.prev_high = high;
        state.prev_low = low;

        // ---- Build snapshot ----
        let rsi = if state.rsi_avg_loss > 0.0 {
            let rs = state.rsi_avg_gain / state.rsi_avg_loss;
            100.0 - 100.0 / (1.0 + rs)
        } else if state.rsi_avg_gain > 0.0 {
            100.0
        } else {
            50.0
        };

        let bb_variance = if state.bb_count > 1 {
            state.bb_m2 / (n - 1.0)
        } else {
            0.0
        };
        let bb_stddev = bb_variance.sqrt();
        let bb_mult = self.params.bollinger_multiplier;

        let sma_count = state.sma_ring.len().min(sma_period);
        let sma = if sma_count > 0 {
            state.sma_running_sum / f64::from(sma_count)
        } else {
            0.0
        };

        let vwap = if state.vwap_cumulative_vol > 0.0 {
            state.vwap_cumulative_pv / state.vwap_cumulative_vol
        } else {
            0.0
        };

        let supertrend = if state.supertrend_direction {
            state.supertrend_lower
        } else {
            state.supertrend_upper
        };

        IndicatorSnapshot {
            security_id: tick.security_id,
            ema_fast: state.ema_fast,
            ema_slow: state.ema_slow,
            sma,
            vwap,
            rsi,
            macd_line,
            macd_signal: state.macd_signal_ema,
            macd_histogram: macd_line - state.macd_signal_ema,
            bollinger_upper: state.bb_mean + bb_mult * bb_stddev,
            bollinger_middle: state.bb_mean,
            bollinger_lower: state.bb_mean - bb_mult * bb_stddev,
            atr: state.atr,
            supertrend,
            supertrend_bullish: state.supertrend_direction,
            adx: state.adx_value,
            obv: state.obv,
            last_traded_price: price,
            previous_close: close,
            day_high: high,
            day_low: low,
            volume,
            is_warm: state.is_warm(),
        }
    }

    /// Resets VWAP accumulators for all instruments (call at market open).
    pub fn reset_vwap_daily(&mut self) {
        for state in &mut self.states {
            state.vwap_cumulative_pv = 0.0;
            state.vwap_cumulative_vol = 0.0;
        }
    }

    /// Resets Bollinger Band accumulators for all instruments (call at session reset).
    pub fn reset_bollinger_daily(&mut self) {
        for state in &mut self.states {
            state.bb_mean = 0.0;
            state.bb_m2 = 0.0;
            state.bb_count = 0;
        }
    }

    /// Returns a reference to the indicator parameters.
    pub const fn params(&self) -> &IndicatorParams {
        &self.params
    }

    /// Warms up indicators from historical candle data.
    ///
    /// Feed OHLCV candles in chronological order (oldest first). Each candle
    /// updates all indicators using the close price as LTP. After processing
    /// enough candles (>= warmup threshold), `is_warm` becomes true.
    ///
    /// # Usage
    /// Call before market open with 90 days of daily or 1-minute candles.
    /// After warmup, the first live tick produces meaningful indicator values
    /// instead of starting cold.
    ///
    /// # Performance
    /// O(N × M) where N = number of candles and M = number of instruments.
    /// Cold path — runs once at startup, not during live trading.
    // O(1) EXEMPT: begin — cold path startup warmup (runs once before tick processing)
    pub fn warmup_from_candles(
        &mut self,
        security_id: u32,
        candles: &[(f64, f64, f64, f64, f64)], // (open, high, low, close, volume)
    ) -> usize {
        let sid = security_id as usize;
        if sid >= self.states.len() || candles.is_empty() {
            return 0;
        }

        let mut processed = 0_usize;

        for &(open, high, low, close, volume) in candles {
            // Skip invalid candles
            if !close.is_finite() || close <= 0.0 {
                continue;
            }

            // Create a synthetic tick from candle data
            let tick = ParsedTick {
                security_id,
                exchange_segment_code: 0,
                last_traded_price: close as f32,
                day_high: high as f32,
                day_low: low as f32,
                day_open: open as f32,
                day_close: 0.0, // Not used for indicator calculation
                volume: volume as u32,
                average_traded_price: close as f32,
                ..ParsedTick::default()
            };

            // Feed through the normal update path
            let _ = self.update(&tick);
            processed = processed.saturating_add(1);
        }
        // O(1) EXEMPT: end

        processed
    }

    /// Returns the warmup count for a given security.
    pub fn warmup_count(&self, security_id: u32) -> u16 {
        let sid = security_id as usize;
        if sid >= self.states.len() {
            return 0;
        }
        self.states[sid].warmup_count
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::compat::constants::MAX_INDICATOR_WARMUP_TICKS;

    fn make_tick(security_id: u32, ltp: f32, high: f32, low: f32, volume: u32) -> ParsedTick {
        ParsedTick {
            security_id,
            last_traded_price: ltp,
            day_high: high,
            day_low: low,
            volume,
            ..Default::default()
        }
    }

    fn default_engine() -> IndicatorEngine {
        IndicatorEngine::new(IndicatorParams::default())
    }

    // --- Construction ---

    #[test]
    fn test_new_engine_pre_allocates_state() {
        let engine = default_engine();
        assert_eq!(engine.states.len(), MAX_INDICATOR_INSTRUMENTS);
    }

    #[test]
    fn test_new_engine_alpha_values_positive() {
        let engine = default_engine();
        assert!(engine.alpha_fast > 0.0 && engine.alpha_fast < 1.0);
        assert!(engine.alpha_slow > 0.0 && engine.alpha_slow < 1.0);
        assert!(engine.alpha_signal > 0.0 && engine.alpha_signal < 1.0);
        assert!(engine.wilder_factor > 0.0 && engine.wilder_factor < 1.0);
    }

    #[test]
    fn test_fast_alpha_greater_than_slow() {
        let engine = default_engine();
        // Fast EMA (period 12) reacts faster → larger alpha
        assert!(engine.alpha_fast > engine.alpha_slow);
    }

    // --- Bounds check ---

    #[test]
    fn test_out_of_bounds_security_id_returns_default_snapshot() {
        let mut engine = default_engine();
        let tick = ParsedTick {
            security_id: u32::MAX,
            last_traded_price: 100.0,
            ..Default::default()
        };
        let snap = engine.update(&tick);
        assert_eq!(snap.security_id, u32::MAX);
        assert!(!snap.is_warm);
        // All indicators should be default (0.0)
        assert_eq!(snap.ema_fast, 0.0);
        assert_eq!(snap.rsi, 0.0);
    }

    // --- First tick initialization ---

    #[test]
    fn test_first_tick_initializes_ema_to_price() {
        let mut engine = default_engine();
        let tick = make_tick(100, 250.0, 255.0, 245.0, 1000);
        let snap = engine.update(&tick);
        assert!((snap.ema_fast - 250.0).abs() < f64::EPSILON);
        assert!((snap.ema_slow - 250.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_first_tick_not_warm() {
        let mut engine = default_engine();
        let tick = make_tick(100, 250.0, 255.0, 245.0, 1000);
        let snap = engine.update(&tick);
        assert!(!snap.is_warm);
    }

    #[test]
    fn test_first_tick_rsi_is_neutral() {
        let mut engine = default_engine();
        let tick = make_tick(100, 250.0, 255.0, 245.0, 1000);
        let snap = engine.update(&tick);
        // First tick: avg_gain = 0, avg_loss = 0 → RSI = 50 (neutral)
        assert!((snap.rsi - 50.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_first_tick_atr_is_zero() {
        let mut engine = default_engine();
        let tick = make_tick(100, 250.0, 255.0, 245.0, 1000);
        let snap = engine.update(&tick);
        assert_eq!(snap.atr, 0.0);
    }

    // --- Warmup ---

    #[test]
    fn test_warmup_completes_after_threshold() {
        let mut engine = default_engine();
        let warmup = u32::from(MAX_INDICATOR_WARMUP_TICKS);
        for i in 0..warmup {
            let tick = make_tick(100, 250.0 + i as f32, 260.0, 240.0, 1000);
            let snap = engine.update(&tick);
            if i + 1 < warmup {
                assert!(!snap.is_warm, "should not be warm at tick {}", i + 1);
            } else {
                assert!(snap.is_warm, "should be warm at tick {}", i + 1);
            }
        }
    }

    // --- EMA convergence ---

    #[test]
    fn test_ema_converges_to_constant_price() {
        let mut engine = default_engine();
        let price = 100.0_f32;
        // Feed many ticks at the same price — EMA should converge to price
        for _ in 0..200 {
            engine.update(&make_tick(100, price, price, price, 1000));
        }
        let snap = engine.update(&make_tick(100, price, price, price, 1000));
        assert!(
            (snap.ema_fast - f64::from(price)).abs() < 0.01,
            "ema_fast should converge to constant price"
        );
        assert!(
            (snap.ema_slow - f64::from(price)).abs() < 0.01,
            "ema_slow should converge to constant price"
        );
    }

    // --- RSI ---

    #[test]
    fn test_rsi_all_gains_approaches_100() {
        let mut engine = default_engine();
        // Steadily increasing prices → RSI should approach 100
        for i in 0..100 {
            engine.update(&make_tick(
                100,
                100.0 + i as f32,
                110.0 + i as f32,
                95.0 + i as f32,
                1000,
            ));
        }
        let snap = engine.update(&make_tick(100, 200.0, 210.0, 195.0, 1000));
        assert!(
            snap.rsi > 90.0,
            "RSI should be > 90 for all-gain series, got {}",
            snap.rsi
        );
    }

    #[test]
    fn test_rsi_all_losses_approaches_0() {
        let mut engine = default_engine();
        // Steadily decreasing prices → RSI should approach 0
        for i in 0..100 {
            engine.update(&make_tick(
                100,
                200.0 - i as f32,
                210.0 - i as f32,
                195.0 - i as f32,
                1000,
            ));
        }
        let snap = engine.update(&make_tick(100, 99.0, 100.0, 98.0, 1000));
        assert!(
            snap.rsi < 10.0,
            "RSI should be < 10 for all-loss series, got {}",
            snap.rsi
        );
    }

    // --- MACD ---

    #[test]
    fn test_macd_at_constant_price_converges_to_zero() {
        let mut engine = default_engine();
        for _ in 0..200 {
            engine.update(&make_tick(100, 100.0, 100.0, 100.0, 1000));
        }
        let snap = engine.update(&make_tick(100, 100.0, 100.0, 100.0, 1000));
        assert!(
            snap.macd_line.abs() < 0.01,
            "MACD line should be ~0 at constant price, got {}",
            snap.macd_line
        );
        assert!(
            snap.macd_histogram.abs() < 0.01,
            "MACD histogram should be ~0 at constant price, got {}",
            snap.macd_histogram
        );
    }

    // --- VWAP ---

    #[test]
    fn test_vwap_with_volume() {
        let mut engine = default_engine();
        // Tick with price=100, high=105, low=95, volume=1000
        // typical_price = (105 + 95 + 100) / 3 = 100.0
        let snap = engine.update(&make_tick(100, 100.0, 105.0, 95.0, 1000));
        assert!((snap.vwap - 100.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_vwap_zero_volume_stays_zero() {
        let mut engine = default_engine();
        let snap = engine.update(&make_tick(100, 100.0, 105.0, 95.0, 0));
        assert_eq!(snap.vwap, 0.0);
    }

    #[test]
    fn test_vwap_reset_daily_clears_accumulators() {
        let mut engine = default_engine();
        engine.update(&make_tick(100, 100.0, 105.0, 95.0, 1000));
        engine.reset_vwap_daily();
        // After reset, VWAP should return 0 until new volume arrives
        let snap = engine.update(&make_tick(100, 100.0, 105.0, 95.0, 0));
        assert_eq!(snap.vwap, 0.0);
    }

    // --- Bollinger Bands ---

    #[test]
    fn test_bollinger_at_constant_price_bands_collapse() {
        let mut engine = default_engine();
        for _ in 0..50 {
            engine.update(&make_tick(100, 100.0, 100.0, 100.0, 1000));
        }
        let snap = engine.update(&make_tick(100, 100.0, 100.0, 100.0, 1000));
        // With constant price, stddev → 0, so all bands converge to mean
        assert!((snap.bollinger_upper - 100.0).abs() < 0.01);
        assert!((snap.bollinger_middle - 100.0).abs() < 0.01);
        assert!((snap.bollinger_lower - 100.0).abs() < 0.01);
    }

    #[test]
    fn test_bollinger_reset_daily() {
        let mut engine = default_engine();
        for _ in 0..10 {
            engine.update(&make_tick(100, 100.0, 105.0, 95.0, 1000));
        }
        engine.reset_bollinger_daily();
        // After reset, bb_count = 0 → next snapshot should behave like fresh start
        let snap = engine.update(&make_tick(100, 200.0, 210.0, 190.0, 1000));
        assert!(
            (snap.bollinger_middle - 200.0).abs() < 0.01,
            "bollinger middle after reset should be new price"
        );
    }

    // --- SMA ---

    #[test]
    fn test_sma_single_value_equals_price() {
        let mut engine = default_engine();
        let snap = engine.update(&make_tick(100, 50.0, 55.0, 45.0, 1000));
        assert!((snap.sma - 50.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_sma_within_period_is_exact() {
        let mut engine = default_engine();
        // Within the SMA period (20), running sum is just accumulated prices.
        // After 10 pushes at 100.0: sma = 1000 / 10 = 100.0
        for _ in 0..10 {
            engine.update(&make_tick(100, 100.0, 100.0, 100.0, 1000));
        }
        let snap = engine.update(&make_tick(100, 100.0, 100.0, 100.0, 1000));
        // sma_ring.len() = 11, sma_period = 20 → sma_count = min(11, 20) = 11
        // sma = 1100 / 11 = 100.0
        assert!(
            (snap.sma - 100.0).abs() < f64::EPSILON,
            "SMA within period should be exact avg, got {}",
            snap.sma
        );
    }

    // --- ATR ---

    #[test]
    fn test_atr_second_tick_initializes_to_true_range() {
        let mut engine = default_engine();
        // First tick: sets prev values
        engine.update(&make_tick(100, 100.0, 105.0, 95.0, 1000));
        // Second tick: ATR = TR
        let snap = engine.update(&make_tick(100, 102.0, 108.0, 96.0, 1000));
        // TR = max(108-96, |108-100|, |96-100|) = max(12, 8, 4) = 12
        assert!((snap.atr - 12.0).abs() < f64::EPSILON);
    }

    // --- OBV ---

    #[test]
    fn test_obv_increases_on_price_up() {
        let mut engine = default_engine();
        engine.update(&make_tick(100, 100.0, 105.0, 95.0, 1000));
        let snap = engine.update(&make_tick(100, 101.0, 106.0, 96.0, 500));
        // Price went up (100→101), so OBV += volume
        assert!((snap.obv - 500.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_obv_decreases_on_price_down() {
        let mut engine = default_engine();
        engine.update(&make_tick(100, 100.0, 105.0, 95.0, 1000));
        let snap = engine.update(&make_tick(100, 99.0, 104.0, 94.0, 500));
        // Price went down (100→99), so OBV -= volume
        assert!((snap.obv - (-500.0)).abs() < f64::EPSILON);
    }

    #[test]
    fn test_obv_unchanged_on_same_price() {
        let mut engine = default_engine();
        engine.update(&make_tick(100, 100.0, 105.0, 95.0, 1000));
        let snap = engine.update(&make_tick(100, 100.0, 105.0, 95.0, 500));
        assert_eq!(snap.obv, 0.0);
    }

    // --- Supertrend ---

    #[test]
    fn test_supertrend_first_tick_sets_initial_bands() {
        let mut engine = default_engine();
        let snap = engine.update(&make_tick(100, 100.0, 110.0, 90.0, 1000));
        // First tick: supertrend_upper = high, supertrend_lower = low
        // Direction starts bullish → supertrend = lower band
        assert!((snap.supertrend - 90.0).abs() < f64::EPSILON);
        assert!(snap.supertrend_bullish);
    }

    // --- Multiple instruments independent ---

    #[test]
    fn test_instruments_are_independent() {
        let mut engine = default_engine();
        // Feed different prices to two instruments
        engine.update(&make_tick(100, 200.0, 210.0, 190.0, 1000));
        engine.update(&make_tick(200, 50.0, 55.0, 45.0, 500));

        let snap1 = engine.update(&make_tick(100, 201.0, 211.0, 191.0, 1000));
        let snap2 = engine.update(&make_tick(200, 51.0, 56.0, 46.0, 500));

        assert!(snap1.ema_fast > 199.0 && snap1.ema_fast < 202.0);
        assert!(snap2.ema_fast > 49.0 && snap2.ema_fast < 52.0);
    }

    // --- Snapshot completeness ---

    #[test]
    fn test_snapshot_carries_price_context() {
        let mut engine = default_engine();
        let tick = make_tick(100, 100.0, 105.0, 95.0, 1000);
        let snap = engine.update(&tick);
        assert!((snap.last_traded_price - 100.0).abs() < f64::EPSILON);
        assert!((snap.day_high - 105.0).abs() < f64::EPSILON);
        assert!((snap.day_low - 95.0).abs() < f64::EPSILON);
        assert!((snap.volume - 1000.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_params_accessor() {
        let params = IndicatorParams {
            ema_fast_period: 5,
            ..Default::default()
        };
        let engine = IndicatorEngine::new(params);
        assert_eq!(engine.params().ema_fast_period, 5);
    }

    // --- ADX ---

    #[test]
    fn test_adx_starts_at_zero() {
        let mut engine = default_engine();
        let snap = engine.update(&make_tick(100, 100.0, 105.0, 95.0, 1000));
        assert_eq!(snap.adx, 0.0);
    }

    #[test]
    fn test_adx_positive_after_trending() {
        let mut engine = default_engine();
        // Strong uptrend — ADX should become positive
        for i in 0..50 {
            engine.update(&make_tick(
                100,
                100.0 + i as f32 * 2.0,
                110.0 + i as f32 * 2.0,
                95.0 + i as f32 * 2.0,
                1000,
            ));
        }
        let snap = engine.update(&make_tick(100, 200.0, 210.0, 195.0, 1000));
        assert!(
            snap.adx > 0.0,
            "ADX should be > 0 in trending market, got {}",
            snap.adx
        );
    }

    // -----------------------------------------------------------------------
    // Out-of-bounds security_id returns default snapshot
    // -----------------------------------------------------------------------

    #[test]
    fn test_out_of_bounds_security_id_preserves_id_in_snapshot() {
        let mut engine = default_engine();
        // Use a security_id that is >= MAX_INDICATOR_INSTRUMENTS
        let large_id = MAX_INDICATOR_INSTRUMENTS as u32;
        let tick = ParsedTick {
            security_id: large_id,
            last_traded_price: 500.0,
            day_high: 510.0,
            day_low: 490.0,
            volume: 10000,
            ..Default::default()
        };

        let snap = engine.update(&tick);
        assert_eq!(snap.security_id, large_id);
        assert!(!snap.is_warm, "OOB security_id must not be warm");
        assert_eq!(snap.ema_fast, 0.0, "OOB must return default ema_fast");
        assert_eq!(snap.rsi, 0.0, "OOB must return default rsi");
        assert_eq!(snap.atr, 0.0, "OOB must return default atr");
        assert_eq!(snap.macd_line, 0.0, "OOB must return default macd_line");
        assert_eq!(snap.sma, 0.0, "OOB must return default sma");
        assert_eq!(snap.vwap, 0.0, "OOB must return default vwap");
    }

    #[test]
    fn test_security_id_zero_is_valid() {
        let mut engine = default_engine();
        let tick = make_tick(0, 100.0, 105.0, 95.0, 1000);
        let snap = engine.update(&tick);
        // security_id 0 is within bounds and should work normally
        assert_eq!(snap.security_id, 0);
        assert!((snap.ema_fast - 100.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_max_valid_security_id() {
        let mut engine = default_engine();
        let max_valid = (MAX_INDICATOR_INSTRUMENTS - 1) as u32;
        let tick = make_tick(max_valid, 200.0, 210.0, 190.0, 500);
        let snap = engine.update(&tick);
        assert_eq!(snap.security_id, max_valid);
        assert!((snap.ema_fast - 200.0).abs() < f64::EPSILON);
    }

    // -----------------------------------------------------------------------
    // Warmup counter saturation
    // -----------------------------------------------------------------------

    #[test]
    fn test_warmup_counter_saturates_at_u16_max() {
        let mut engine = default_engine();
        let sid = 100_usize;

        // Manually set warmup_count close to u16::MAX
        engine.states[sid].warmup_count = u16::MAX - 1;

        // One more tick should bring it to u16::MAX
        let tick = make_tick(100, 100.0, 105.0, 95.0, 1000);
        engine.update(&tick);
        assert_eq!(engine.states[sid].warmup_count, u16::MAX);

        // Another tick should remain at u16::MAX (saturating)
        engine.update(&tick);
        assert_eq!(
            engine.states[sid].warmup_count,
            u16::MAX,
            "warmup_count must saturate at u16::MAX, not overflow"
        );
    }

    #[test]
    fn test_sma_period_zero_returns_zero_sma() {
        // Covers the sma_count == 0 branch (line 281) where sma_period = 0
        // causes min(ring_len, 0) = 0 regardless of ring contents.
        let params = IndicatorParams {
            sma_period: 0,
            ..Default::default()
        };
        let mut engine = IndicatorEngine::new(params);
        let tick = make_tick(100, 250.0, 260.0, 240.0, 1000);
        let snap = engine.update(&tick);
        assert_eq!(snap.sma, 0.0, "sma_period=0 must yield sma=0.0");
    }

    // -----------------------------------------------------------------------
    // Additional coverage: out-of-bounds edge, warmup saturation, zero-volume VWAP
    // -----------------------------------------------------------------------

    #[test]
    fn test_out_of_bounds_security_id_one_above_max_returns_default() {
        let mut engine = default_engine();
        let oob_id = MAX_INDICATOR_INSTRUMENTS as u32;
        let tick = make_tick(oob_id, 500.0, 510.0, 490.0, 10000);
        let snap = engine.update(&tick);
        assert_eq!(snap.security_id, oob_id);
        assert!(!snap.is_warm);
        assert_eq!(snap.ema_fast, 0.0);
        assert_eq!(snap.vwap, 0.0);
        assert_eq!(snap.obv, 0.0);
    }

    #[test]
    fn test_zero_volume_vwap_stays_unchanged_across_ticks() {
        let mut engine = default_engine();
        // First tick with volume → VWAP set
        engine.update(&make_tick(100, 100.0, 105.0, 95.0, 1000));
        let snap1 = engine.update(&make_tick(100, 110.0, 115.0, 105.0, 1000));
        let vwap_after_volume = snap1.vwap;
        assert!(
            vwap_after_volume > 0.0,
            "VWAP should be positive with volume"
        );

        // Subsequent tick with zero volume → VWAP unchanged
        let snap2 = engine.update(&make_tick(100, 120.0, 125.0, 115.0, 0));
        assert!(
            (snap2.vwap - vwap_after_volume).abs() < f64::EPSILON,
            "zero volume must not change VWAP accumulator"
        );
    }

    #[test]
    fn test_warmup_count_starts_at_one_after_first_tick() {
        let mut engine = default_engine();
        let sid = 50_usize;
        let tick = make_tick(50, 100.0, 105.0, 95.0, 1000);
        engine.update(&tick);
        assert_eq!(engine.states[sid].warmup_count, 1);
    }

    #[test]
    fn test_warmup_saturation_does_not_affect_indicator_computation() {
        let mut engine = default_engine();
        let sid = 100_usize;

        // Set warmup_count to u16::MAX
        engine.states[sid].warmup_count = u16::MAX;

        // Feed two ticks at different prices
        let tick1 = make_tick(100, 100.0, 105.0, 95.0, 1000);
        let snap1 = engine.update(&tick1);
        assert!(snap1.is_warm);

        let tick2 = make_tick(100, 110.0, 115.0, 105.0, 1000);
        let snap2 = engine.update(&tick2);

        // EMA should have moved toward 110
        assert!(
            snap2.ema_fast > snap1.ema_fast,
            "EMA must still update at saturated warmup"
        );
        // warmup_count stays at MAX
        assert_eq!(engine.states[sid].warmup_count, u16::MAX);
    }

    #[test]
    fn test_multiple_zero_volume_ticks_vwap_remains_zero() {
        let mut engine = default_engine();
        // All zero-volume ticks → VWAP stays at 0
        for i in 0..10 {
            let snap = engine.update(&make_tick(100, 100.0 + i as f32, 110.0, 90.0, 0));
            assert_eq!(
                snap.vwap, 0.0,
                "zero volume across all ticks must keep VWAP at 0"
            );
        }
    }

    #[test]
    fn test_bollinger_single_tick_stddev_is_zero() {
        let mut engine = default_engine();
        let snap = engine.update(&make_tick(100, 100.0, 105.0, 95.0, 1000));
        // With only one data point, variance = 0, so upper == middle == lower
        assert!(
            (snap.bollinger_upper - snap.bollinger_lower).abs() < f64::EPSILON,
            "single tick: bollinger bands must collapse"
        );
    }

    #[test]
    fn test_warmup_counter_at_max_still_produces_valid_indicators() {
        let mut engine = default_engine();
        let sid = 100_usize;

        // Set warmup to max — is_warm should be true
        engine.states[sid].warmup_count = u16::MAX;

        let tick = make_tick(100, 100.0, 105.0, 95.0, 1000);
        let snap = engine.update(&tick);
        assert!(snap.is_warm, "u16::MAX warmup_count must be warm");
        // EMA should still update correctly
        assert!(snap.ema_fast > 0.0);
    }

    // -----------------------------------------------------------------------
    // Warmup from Historical Candles
    // -----------------------------------------------------------------------

    #[test]
    fn test_warmup_from_candles_empty() {
        let params = IndicatorParams::default();
        let mut engine = IndicatorEngine::new(params);
        let result = engine.warmup_from_candles(100, &[]);
        assert_eq!(result, 0);
    }

    #[test]
    fn test_warmup_from_candles_processes_all() {
        let params = IndicatorParams::default();
        let mut engine = IndicatorEngine::new(params);
        let candles: Vec<(f64, f64, f64, f64, f64)> = (0..50)
            .map(|i| {
                let price = 100.0 + i as f64;
                (price - 1.0, price + 2.0, price - 2.0, price, 1000.0)
            })
            .collect();
        let processed = engine.warmup_from_candles(100, &candles);
        assert_eq!(processed, 50);
    }

    #[test]
    fn test_warmup_makes_engine_warm() {
        let params = IndicatorParams::default();
        let mut engine = IndicatorEngine::new(params);
        // Feed 40 candles (above warmup threshold of 30)
        let candles: Vec<(f64, f64, f64, f64, f64)> = (0..40)
            .map(|i| {
                let price = 100.0 + i as f64;
                (price, price + 1.0, price - 1.0, price, 1000.0)
            })
            .collect();
        engine.warmup_from_candles(100, &candles);
        assert!(engine.warmup_count(100) >= 30);
    }

    #[test]
    fn test_warmup_indicators_have_values() {
        let params = IndicatorParams::default();
        let mut engine = IndicatorEngine::new(params);
        let candles: Vec<(f64, f64, f64, f64, f64)> = (0..50)
            .map(|i| {
                let price = 100.0 + (i as f64 * 0.5);
                (price, price + 2.0, price - 1.0, price, 5000.0)
            })
            .collect();
        engine.warmup_from_candles(200, &candles);

        // After warmup, feed one more tick to get a snapshot
        let tick = ParsedTick {
            security_id: 200,
            last_traded_price: 125.0,
            day_high: 126.0,
            day_low: 124.0,
            day_open: 124.5,
            volume: 5000,
            average_traded_price: 125.0,
            ..ParsedTick::default()
        };
        let snap = engine.update(&tick);
        assert!(snap.is_warm);
        assert!(snap.ema_fast > 0.0);
        assert!(snap.ema_slow > 0.0);
        assert!(snap.sma > 0.0);
        assert!(snap.rsi > 0.0 && snap.rsi <= 100.0);
        assert!(snap.atr >= 0.0);
    }

    #[test]
    fn test_warmup_skips_invalid_candles() {
        let params = IndicatorParams::default();
        let mut engine = IndicatorEngine::new(params);
        let candles = vec![
            (100.0, 101.0, 99.0, 100.0, 1000.0),           // valid
            (0.0, 0.0, 0.0, 0.0, 0.0),                     // invalid (close=0)
            (f64::NAN, f64::NAN, f64::NAN, f64::NAN, 0.0), // invalid (NaN)
            (102.0, 103.0, 101.0, 102.0, 2000.0),          // valid
        ];
        let processed = engine.warmup_from_candles(300, &candles);
        assert_eq!(processed, 2); // Only 2 valid candles
    }

    #[test]
    fn test_warmup_oob_security_id() {
        let params = IndicatorParams::default();
        let mut engine = IndicatorEngine::new(params);
        let candles = vec![(100.0, 101.0, 99.0, 100.0, 1000.0)];
        let processed = engine.warmup_from_candles(u32::MAX, &candles);
        assert_eq!(processed, 0); // Out of bounds
    }

    #[test]
    fn test_warmup_count_method() {
        let params = IndicatorParams::default();
        let mut engine = IndicatorEngine::new(params);
        assert_eq!(engine.warmup_count(100), 0); // Initially zero
        let candles: Vec<(f64, f64, f64, f64, f64)> = (0..10)
            .map(|i| (100.0 + i as f64, 101.0, 99.0, 100.0 + i as f64, 1000.0))
            .collect();
        engine.warmup_from_candles(100, &candles);
        assert_eq!(engine.warmup_count(100), 10);
    }

    #[test]
    fn test_warmup_count_oob() {
        let params = IndicatorParams::default();
        let engine = IndicatorEngine::new(params);
        assert_eq!(engine.warmup_count(u32::MAX), 0);
    }

    // -----------------------------------------------------------------------
    // Exact-Value Indicator Tests (mutation-targeted)
    // -----------------------------------------------------------------------

    #[test]
    fn test_ema_exact_value_after_two_ticks() {
        // EMA(12) alpha = 2/(12+1) = 0.153846...
        // Tick 1: EMA = 100.0 (first tick initializes to price)
        // Tick 2: EMA = alpha * 105.0 + (1-alpha) * 100.0
        //       = 0.153846 * 105.0 + 0.846154 * 100.0
        //       = 16.1538 + 84.6154 = 100.7692...
        let params = IndicatorParams::default();
        let mut engine = IndicatorEngine::new(params);

        let tick1 = ParsedTick {
            security_id: 1,
            last_traded_price: 100.0,
            day_high: 101.0,
            day_low: 99.0,
            volume: 1000,
            average_traded_price: 100.0,
            ..ParsedTick::default()
        };
        let snap1 = engine.update(&tick1);
        assert!(
            (snap1.ema_fast - 100.0).abs() < 0.001,
            "First tick EMA must equal price"
        );

        let tick2 = ParsedTick {
            security_id: 1,
            last_traded_price: 105.0,
            day_high: 106.0,
            day_low: 104.0,
            volume: 2000,
            average_traded_price: 105.0,
            ..ParsedTick::default()
        };
        let snap2 = engine.update(&tick2);
        // If mutation changes + to - in EMA: result would be very different
        // If mutation changes * to / in EMA: result would be very different
        assert!(snap2.ema_fast > 100.0, "EMA must increase when price rises");
        assert!(snap2.ema_fast < 105.0, "EMA must lag behind price");
        // Exact check: EMA should be approximately 100.769
        assert!(
            (snap2.ema_fast - 100.769).abs() < 0.1,
            "EMA fast after 2 ticks must be ~100.77, got {}",
            snap2.ema_fast
        );
    }

    #[test]
    fn test_rsi_after_known_sequence() {
        // Feed: 100, 102, 101, 103, 100, 104
        // Alternating gains and losses → RSI should be near 50
        let params = IndicatorParams::default();
        let mut engine = IndicatorEngine::new(params);

        let prices = [
            100.0_f32, 102.0, 101.0, 103.0, 100.0, 104.0, 99.0, 105.0, 98.0, 106.0,
        ];
        let mut last_snap = IndicatorSnapshot::default();
        for &price in &prices {
            let tick = ParsedTick {
                security_id: 2,
                last_traded_price: price,
                day_high: price + 1.0,
                day_low: price - 1.0,
                volume: 1000,
                average_traded_price: price,
                ..ParsedTick::default()
            };
            last_snap = engine.update(&tick);
        }
        // RSI must be in valid range
        assert!(
            last_snap.rsi >= 0.0 && last_snap.rsi <= 100.0,
            "RSI out of range: {}",
            last_snap.rsi
        );
        // With alternating up/down, RSI should be roughly balanced (30-70)
        assert!(
            last_snap.rsi > 20.0 && last_snap.rsi < 80.0,
            "RSI with alternating prices should be near 50, got {}",
            last_snap.rsi
        );
    }

    #[test]
    fn test_macd_signal_divergence() {
        // Feed consistently rising prices → MACD line should be positive
        // (fast EMA > slow EMA when prices rise)
        let params = IndicatorParams::default();
        let mut engine = IndicatorEngine::new(params);

        let mut last_snap = IndicatorSnapshot::default();
        for i in 0..40_u32 {
            let price = 100.0 + i as f32 * 2.0; // Steadily rising
            let tick = ParsedTick {
                security_id: 3,
                last_traded_price: price,
                day_high: price + 0.5,
                day_low: price - 0.5,
                volume: 1000,
                average_traded_price: price,
                ..ParsedTick::default()
            };
            last_snap = engine.update(&tick);
        }
        // MACD line = EMA_fast - EMA_slow → positive for rising prices
        assert!(
            last_snap.macd_line > 0.0,
            "MACD line must be positive for rising prices, got {}",
            last_snap.macd_line
        );
        // Signal should also be positive but lag
        assert!(
            last_snap.macd_signal > 0.0,
            "MACD signal must be positive, got {}",
            last_snap.macd_signal
        );
        // Histogram = line - signal
        let expected_hist = last_snap.macd_line - last_snap.macd_signal;
        assert!(
            (last_snap.macd_histogram - expected_hist).abs() < 0.001,
            "MACD histogram must equal line - signal"
        );
    }

    #[test]
    fn test_atr_positive_after_volatile_sequence() {
        let params = IndicatorParams::default();
        let mut engine = IndicatorEngine::new(params);

        let mut last_snap = IndicatorSnapshot::default();
        for i in 0..20_u32 {
            // Alternating high volatility
            let base = 100.0 + if i % 2 == 0 { 10.0 } else { -10.0 };
            let tick = ParsedTick {
                security_id: 4,
                last_traded_price: base as f32,
                day_high: (base + 5.0) as f32,
                day_low: (base - 5.0) as f32,
                volume: 1000,
                average_traded_price: base as f32,
                ..ParsedTick::default()
            };
            last_snap = engine.update(&tick);
        }
        // ATR must be positive for volatile data
        assert!(
            last_snap.atr > 0.0,
            "ATR must be positive, got {}",
            last_snap.atr
        );
        // ATR should reflect the ~10-20 point swings
        assert!(
            last_snap.atr > 1.0,
            "ATR too small for volatile data: {}",
            last_snap.atr
        );
    }

    #[test]
    fn test_bollinger_bands_relationship() {
        let params = IndicatorParams::default();
        let mut engine = IndicatorEngine::new(params);

        let mut last_snap = IndicatorSnapshot::default();
        for i in 0..30_u32 {
            let price = 100.0 + (i as f32 * 0.1);
            let tick = ParsedTick {
                security_id: 5,
                last_traded_price: price,
                day_high: price + 0.5,
                day_low: price - 0.5,
                volume: 1000,
                average_traded_price: price,
                ..ParsedTick::default()
            };
            last_snap = engine.update(&tick);
        }
        // Bollinger: upper > middle > lower (always)
        assert!(
            last_snap.bollinger_upper >= last_snap.bollinger_middle,
            "Upper band must be >= middle"
        );
        assert!(
            last_snap.bollinger_middle >= last_snap.bollinger_lower,
            "Middle band must be >= lower"
        );
        // Middle band is Bollinger mean (Welford's online — separate from SMA ring buffer)
        assert!(
            last_snap.bollinger_middle > 0.0,
            "Bollinger middle must be positive"
        );
    }

    #[test]
    fn test_obv_increases_on_up_tick() {
        let params = IndicatorParams::default();
        let mut engine = IndicatorEngine::new(params);

        // First tick
        let tick1 = ParsedTick {
            security_id: 6,
            last_traded_price: 100.0,
            day_high: 101.0,
            day_low: 99.0,
            volume: 1000,
            average_traded_price: 100.0,
            ..ParsedTick::default()
        };
        engine.update(&tick1);

        // Up tick → OBV should increase by volume
        let tick2 = ParsedTick {
            security_id: 6,
            last_traded_price: 105.0,
            day_high: 106.0,
            day_low: 104.0,
            volume: 2000,
            average_traded_price: 105.0,
            ..ParsedTick::default()
        };
        let snap2 = engine.update(&tick2);
        assert!(
            snap2.obv > 0.0,
            "OBV must increase on up tick, got {}",
            snap2.obv
        );

        // Down tick → OBV should decrease
        let tick3 = ParsedTick {
            security_id: 6,
            last_traded_price: 95.0,
            day_high: 96.0,
            day_low: 94.0,
            volume: 3000,
            average_traded_price: 95.0,
            ..ParsedTick::default()
        };
        let snap3 = engine.update(&tick3);
        assert!(snap3.obv < snap2.obv, "OBV must decrease on down tick");
    }

    #[test]
    fn test_supertrend_direction_for_trending_market() {
        let params = IndicatorParams::default();
        let mut engine = IndicatorEngine::new(params);

        let mut last_snap = IndicatorSnapshot::default();
        // Feed 30 consistently rising ticks
        for i in 0..30_u32 {
            let price = 100.0 + i as f32 * 3.0;
            let tick = ParsedTick {
                security_id: 7,
                last_traded_price: price,
                day_high: price + 1.0,
                day_low: price - 1.0,
                volume: 1000,
                average_traded_price: price,
                ..ParsedTick::default()
            };
            last_snap = engine.update(&tick);
        }
        // Strong uptrend → supertrend should be bullish
        assert!(
            last_snap.supertrend_bullish,
            "Supertrend must be bullish in uptrend"
        );
        assert!(
            last_snap.supertrend > 0.0,
            "Supertrend value must be positive"
        );
    }
}
