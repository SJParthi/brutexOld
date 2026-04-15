//! Tests for the O(1) indicator engine.
//!
//! Verifies correctness of all incremental indicator algorithms
//! against known values and mathematical properties.

use crate::compat::constants::INDICATOR_RING_BUFFER_CAPACITY;
use crate::compat::tick_types::ParsedTick;
use crate::indicators::engine::IndicatorEngine;
use crate::indicators::types::{IndicatorParams, RingBuffer};

/// Helper: create a tick with the given LTP, high, low, volume.
fn make_tick(security_id: u32, ltp: f32, high: f32, low: f32, volume: u32) -> ParsedTick {
    ParsedTick {
        security_id,
        last_traded_price: ltp,
        day_high: high,
        day_low: low,
        day_close: ltp, // use LTP as close for tests
        volume,
        exchange_timestamp: 1_000_000_000,
        ..Default::default()
    }
}

// -----------------------------------------------------------------------
// Ring Buffer Tests
// -----------------------------------------------------------------------

#[test]
fn ring_buffer_new_is_empty() {
    let rb = RingBuffer::new();
    assert!(rb.is_empty());
    assert_eq!(rb.len(), 0);
}

#[test]
fn ring_buffer_push_and_len() {
    let mut rb = RingBuffer::new();
    rb.push(1.0);
    assert_eq!(rb.len(), 1);
    rb.push(2.0);
    assert_eq!(rb.len(), 2);
}

#[test]
fn ring_buffer_wraps_at_capacity() {
    let mut rb = RingBuffer::new();
    for i in 0..INDICATOR_RING_BUFFER_CAPACITY {
        rb.push(i as f64);
    }
    assert_eq!(rb.len() as usize, INDICATOR_RING_BUFFER_CAPACITY);

    // Push one more — should evict the oldest (0.0)
    let evicted = rb.push(999.0);
    assert!((evicted - 0.0).abs() < f64::EPSILON);
    assert_eq!(rb.len() as usize, INDICATOR_RING_BUFFER_CAPACITY);
}

#[test]
fn ring_buffer_oldest_returns_correct_value() {
    let mut rb = RingBuffer::new();
    for i in 0..INDICATOR_RING_BUFFER_CAPACITY {
        rb.push(i as f64);
    }
    // After filling: oldest is at head position = 0.0
    assert!((rb.oldest() - 0.0).abs() < f64::EPSILON);

    // Push one more, now oldest should be 1.0
    rb.push(999.0);
    assert!((rb.oldest() - 1.0).abs() < f64::EPSILON);
}

// -----------------------------------------------------------------------
// EMA Tests
// -----------------------------------------------------------------------

#[test]
fn ema_first_tick_equals_price() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());
    let tick = make_tick(1, 100.0, 101.0, 99.0, 1000);
    let snap = engine.update(&tick);

    assert!((snap.ema_fast - 100.0).abs() < f64::EPSILON);
    assert!((snap.ema_slow - 100.0).abs() < f64::EPSILON);
}

#[test]
fn ema_converges_to_constant_price() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    // Feed 100 ticks at constant price
    let mut snap = Default::default();
    for _ in 0..100 {
        let tick = make_tick(1, 50.0, 51.0, 49.0, 1000);
        snap = engine.update(&tick);
    }

    // EMA should converge to the constant price
    assert!((snap.ema_fast - 50.0).abs() < 0.01);
    assert!((snap.ema_slow - 50.0).abs() < 0.01);
}

#[test]
fn ema_fast_reacts_faster_than_slow() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    // Warm up at 100
    for _ in 0..50 {
        engine.update(&make_tick(1, 100.0, 101.0, 99.0, 1000));
    }

    // Price jumps to 200
    let snap = engine.update(&make_tick(1, 200.0, 201.0, 199.0, 1000));

    // Fast EMA should have moved more than slow EMA
    assert!(snap.ema_fast > snap.ema_slow);
    assert!(snap.ema_fast > 100.0);
    assert!(snap.ema_slow > 100.0);
}

// -----------------------------------------------------------------------
// RSI Tests
// -----------------------------------------------------------------------

#[test]
fn rsi_all_gains_approaches_100() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    // Monotonically increasing prices
    let mut snap = Default::default();
    for i in 0..50 {
        let price = 100.0 + i as f32;
        snap = engine.update(&make_tick(1, price, price + 1.0, price - 1.0, 1000));
    }

    assert!(
        snap.rsi > 90.0,
        "RSI should be near 100 for all gains, got {}",
        snap.rsi
    );
}

#[test]
fn rsi_all_losses_approaches_0() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    // Monotonically decreasing prices
    let mut snap = Default::default();
    for i in 0..50 {
        let price = 200.0 - i as f32;
        snap = engine.update(&make_tick(1, price, price + 1.0, price - 1.0, 1000));
    }

    assert!(
        snap.rsi < 10.0,
        "RSI should be near 0 for all losses, got {}",
        snap.rsi
    );
}

#[test]
fn rsi_constant_price_is_50() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    let mut snap = Default::default();
    for _ in 0..50 {
        snap = engine.update(&make_tick(1, 100.0, 101.0, 99.0, 1000));
    }

    // Constant price → zero gain, zero loss → RSI = 50
    assert!(
        (snap.rsi - 50.0).abs() < 1.0,
        "RSI should be ~50 for constant price, got {}",
        snap.rsi
    );
}

// -----------------------------------------------------------------------
// MACD Tests
// -----------------------------------------------------------------------

#[test]
fn macd_line_is_ema_fast_minus_slow() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    let mut snap = Default::default();
    for i in 0..30 {
        let price = 100.0 + (i as f32) * 0.5;
        snap = engine.update(&make_tick(1, price, price + 1.0, price - 1.0, 1000));
    }

    let expected_macd = snap.ema_fast - snap.ema_slow;
    assert!((snap.macd_line - expected_macd).abs() < 1e-10);
}

#[test]
fn macd_histogram_is_line_minus_signal() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    let mut snap = Default::default();
    for i in 0..50 {
        let price = 100.0 + (i as f32) * 0.3;
        snap = engine.update(&make_tick(1, price, price + 1.0, price - 1.0, 1000));
    }

    let expected = snap.macd_line - snap.macd_signal;
    assert!((snap.macd_histogram - expected).abs() < 1e-10);
}

// -----------------------------------------------------------------------
// ATR Tests
// -----------------------------------------------------------------------

#[test]
fn atr_constant_range_converges() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    // Constant H-L range of 2.0
    let mut snap = Default::default();
    for i in 0..50 {
        let mid = 100.0 + i as f32 * 0.1;
        snap = engine.update(&make_tick(1, mid, mid + 1.0, mid - 1.0, 1000));
    }

    // ATR should converge to ~2.0 (the constant H-L range)
    assert!(snap.atr > 1.5, "ATR should be near 2.0, got {}", snap.atr);
    assert!(snap.atr < 2.5, "ATR should be near 2.0, got {}", snap.atr);
}

// -----------------------------------------------------------------------
// Bollinger Bands Tests (Welford's)
// -----------------------------------------------------------------------

#[test]
fn bollinger_constant_price_bands_collapse() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    let mut snap = Default::default();
    for _ in 0..50 {
        snap = engine.update(&make_tick(1, 100.0, 101.0, 99.0, 1000));
    }

    // Constant price → zero variance → bands collapse to mean
    assert!((snap.bollinger_middle - 100.0).abs() < 0.01);
    assert!((snap.bollinger_upper - snap.bollinger_lower).abs() < 0.01);
}

#[test]
fn bollinger_upper_greater_than_lower() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    let mut snap = Default::default();
    for i in 0..50 {
        let price = 100.0 + (i % 10) as f32; // varying price
        snap = engine.update(&make_tick(1, price, price + 1.0, price - 1.0, 1000));
    }

    assert!(snap.bollinger_upper >= snap.bollinger_lower);
    assert!(snap.bollinger_upper >= snap.bollinger_middle);
    assert!(snap.bollinger_lower <= snap.bollinger_middle);
}

// -----------------------------------------------------------------------
// SMA Tests
// -----------------------------------------------------------------------

#[test]
fn sma_equals_simple_average() {
    let params = IndicatorParams {
        sma_period: 5,
        ..Default::default()
    };
    let mut engine = IndicatorEngine::new(params);

    let prices = [10.0_f32, 20.0, 30.0, 40.0, 50.0];
    let mut snap = Default::default();
    for &p in &prices {
        snap = engine.update(&make_tick(1, p, p + 1.0, p - 1.0, 1000));
    }

    let expected = (10.0 + 20.0 + 30.0 + 40.0 + 50.0) / 5.0;
    assert!(
        (snap.sma - expected).abs() < 0.01,
        "SMA should be {expected}, got {}",
        snap.sma
    );
}

// -----------------------------------------------------------------------
// VWAP Tests
// -----------------------------------------------------------------------

#[test]
fn vwap_single_tick() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());
    let tick = make_tick(1, 100.0, 105.0, 95.0, 1000);
    let snap = engine.update(&tick);

    // VWAP = typical_price × volume / volume = (H+L+C)/3
    let expected = (105.0 + 95.0 + 100.0) / 3.0;
    assert!((snap.vwap - expected).abs() < 0.01);
}

#[test]
fn vwap_daily_reset_works() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    // Feed some ticks
    for _ in 0..10 {
        engine.update(&make_tick(1, 100.0, 105.0, 95.0, 1000));
    }

    // Reset VWAP
    engine.reset_vwap_daily();

    // Next tick should start fresh VWAP
    let snap = engine.update(&make_tick(1, 200.0, 210.0, 190.0, 500));
    let expected = (210.0 + 190.0 + 200.0) / 3.0;
    assert!((snap.vwap - expected).abs() < 0.01);
}

// -----------------------------------------------------------------------
// OBV Tests
// -----------------------------------------------------------------------

#[test]
fn obv_increases_on_up_tick() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    engine.update(&make_tick(1, 100.0, 101.0, 99.0, 1000));
    let snap = engine.update(&make_tick(1, 110.0, 111.0, 109.0, 2000));

    assert!(snap.obv > 0.0, "OBV should increase on up-tick");
}

#[test]
fn obv_decreases_on_down_tick() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    engine.update(&make_tick(1, 100.0, 101.0, 99.0, 1000));
    let snap = engine.update(&make_tick(1, 90.0, 91.0, 89.0, 2000));

    assert!(snap.obv < 0.0, "OBV should decrease on down-tick");
}

// -----------------------------------------------------------------------
// Supertrend Tests
// -----------------------------------------------------------------------

#[test]
fn supertrend_direction_flips_on_trend_change() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    // Uptrend
    let mut snap = Default::default();
    for i in 0..30 {
        let price = 100.0 + i as f32 * 2.0;
        snap = engine.update(&make_tick(1, price, price + 1.0, price - 1.0, 1000));
    }
    assert!(snap.supertrend_bullish, "Should be bullish in uptrend");

    // Sharp downtrend
    for i in 0..30 {
        let price = 160.0 - i as f32 * 3.0;
        snap = engine.update(&make_tick(1, price, price + 1.0, price - 1.0, 1000));
    }
    assert!(!snap.supertrend_bullish, "Should be bearish in downtrend");
}

// -----------------------------------------------------------------------
// ADX Tests
// -----------------------------------------------------------------------

#[test]
fn adx_strong_trend_is_high() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    // Strong uptrend
    let mut snap = Default::default();
    for i in 0..60 {
        let price = 100.0 + i as f32 * 2.0;
        snap = engine.update(&make_tick(1, price, price + 1.0, price - 1.0, 1000));
    }

    assert!(
        snap.adx > 20.0,
        "ADX should be high in strong trend, got {}",
        snap.adx
    );
}

// -----------------------------------------------------------------------
// Multi-instrument isolation
// -----------------------------------------------------------------------

#[test]
fn separate_instruments_have_independent_state() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    // Feed different prices to two instruments
    for _ in 0..30 {
        engine.update(&make_tick(1, 100.0, 101.0, 99.0, 1000));
        engine.update(&make_tick(2, 200.0, 201.0, 199.0, 2000));
    }

    let snap1 = engine.update(&make_tick(1, 100.0, 101.0, 99.0, 1000));
    let snap2 = engine.update(&make_tick(2, 200.0, 201.0, 199.0, 2000));

    assert!((snap1.ema_fast - 100.0).abs() < 1.0);
    assert!((snap2.ema_fast - 200.0).abs() < 1.0);
    assert_ne!(snap1.security_id, snap2.security_id);
}

// -----------------------------------------------------------------------
// Warmup Tests
// -----------------------------------------------------------------------

#[test]
fn warmup_false_until_threshold() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());

    let snap = engine.update(&make_tick(1, 100.0, 101.0, 99.0, 1000));
    assert!(!snap.is_warm, "Should not be warm after 1 tick");

    let mut snap = Default::default();
    for _ in 0..30 {
        snap = engine.update(&make_tick(1, 100.0, 101.0, 99.0, 1000));
    }
    assert!(snap.is_warm, "Should be warm after 30+ ticks");
}

// -----------------------------------------------------------------------
// Out-of-bounds security_id
// -----------------------------------------------------------------------

#[test]
fn out_of_bounds_security_id_returns_default() {
    let mut engine = IndicatorEngine::new(IndicatorParams::default());
    let tick = make_tick(u32::MAX, 100.0, 101.0, 99.0, 1000);
    let snap = engine.update(&tick);

    assert_eq!(snap.security_id, u32::MAX);
    assert!(!snap.is_warm);
    assert!((snap.ema_fast).abs() < f64::EPSILON);
}

// -----------------------------------------------------------------------
// IndicatorParams helpers
// -----------------------------------------------------------------------

#[test]
fn ema_alpha_12_period() {
    let alpha = IndicatorParams::ema_alpha(12);
    let expected = 2.0 / 13.0;
    assert!((alpha - expected).abs() < 1e-10);
}

#[test]
fn wilder_factor_14_period() {
    let wf = IndicatorParams::wilder_factor(14);
    let expected = 1.0 / 14.0;
    assert!((wf - expected).abs() < 1e-10);
}
