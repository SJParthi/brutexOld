//! Tests for the strategy evaluator FSM.
// O(1) EXEMPT: begin — test-only module, no production hot path

use crate::indicators::IndicatorSnapshot;
use crate::strategies::evaluator::StrategyInstance;
use crate::strategies::types::{
    ComparisonOp, Condition, ExitReason, IndicatorField, Signal, StrategyDefinition, StrategyState,
};

/// Helper: create a default strategy definition for testing.
fn test_strategy() -> StrategyDefinition {
    StrategyDefinition {
        name: "test_rsi_mean_reversion".to_owned(),
        security_ids: vec![1],
        entry_long_conditions: vec![Condition {
            field: IndicatorField::Rsi,
            operator: ComparisonOp::Lt,
            threshold: 30.0,
        }],
        entry_short_conditions: vec![Condition {
            field: IndicatorField::Rsi,
            operator: ComparisonOp::Gt,
            threshold: 70.0,
        }],
        exit_conditions: vec![Condition {
            field: IndicatorField::Rsi,
            operator: ComparisonOp::Gt,
            threshold: 50.0,
        }],
        position_size_fraction: 0.1,
        stop_loss_atr_multiplier: 2.0,
        target_atr_multiplier: 3.0,
        confirmation_ticks: 0,
        trailing_stop_enabled: false,
        trailing_stop_atr_multiplier: 1.5,
    }
}

/// Helper: create an indicator snapshot with given values.
fn make_snapshot(security_id: u32, rsi: f64, ltp: f64, atr: f64) -> IndicatorSnapshot {
    IndicatorSnapshot {
        security_id,
        rsi,
        last_traded_price: ltp,
        atr,
        is_warm: true,
        ..Default::default()
    }
}

// -----------------------------------------------------------------------
// FSM State Transition Tests
// -----------------------------------------------------------------------

#[test]
fn idle_to_enter_long_when_conditions_met() {
    let def = test_strategy();
    let mut instance = StrategyInstance::new(def, 10);

    // RSI < 30 → should enter long
    let snap = make_snapshot(1, 25.0, 100.0, 5.0);
    let signal = instance.evaluate(&snap);

    match signal {
        Signal::EnterLong {
            size_fraction,
            stop_loss,
            target,
        } => {
            assert!((size_fraction - 0.1).abs() < f64::EPSILON);
            // stop = 100 - 2*5 = 90
            assert!((stop_loss - 90.0).abs() < 0.01);
            // target = 100 + 3*5 = 115
            assert!((target - 115.0).abs() < 0.01);
        }
        other => panic!("Expected EnterLong, got {other:?}"),
    }
}

#[test]
fn idle_to_enter_short_when_conditions_met() {
    let def = test_strategy();
    let mut instance = StrategyInstance::new(def, 10);

    // RSI > 70 → should enter short
    let snap = make_snapshot(1, 80.0, 100.0, 5.0);
    let signal = instance.evaluate(&snap);

    match signal {
        Signal::EnterShort {
            size_fraction,
            stop_loss,
            target,
        } => {
            assert!((size_fraction - 0.1).abs() < f64::EPSILON);
            // stop = 100 + 2*5 = 110
            assert!((stop_loss - 110.0).abs() < 0.01);
            // target = 100 - 3*5 = 85
            assert!((target - 85.0).abs() < 0.01);
        }
        other => panic!("Expected EnterShort, got {other:?}"),
    }
}

#[test]
fn idle_holds_when_no_conditions_met() {
    let def = test_strategy();
    let mut instance = StrategyInstance::new(def, 10);

    // RSI = 50 → no entry conditions met
    let snap = make_snapshot(1, 50.0, 100.0, 5.0);
    let signal = instance.evaluate(&snap);

    assert_eq!(signal, Signal::Hold);
}

#[test]
fn in_position_exits_on_stop_loss() {
    let def = test_strategy();
    let mut instance = StrategyInstance::new(def, 10);

    // Enter long
    let snap = make_snapshot(1, 25.0, 100.0, 5.0);
    instance.evaluate(&snap);

    // Price drops below stop-loss (90)
    let snap = make_snapshot(1, 40.0, 89.0, 5.0);
    let signal = instance.evaluate(&snap);

    assert_eq!(
        signal,
        Signal::Exit {
            reason: ExitReason::StopLossHit
        }
    );
}

#[test]
fn in_position_exits_on_target() {
    let def = test_strategy();
    let mut instance = StrategyInstance::new(def, 10);

    // Enter long
    let snap = make_snapshot(1, 25.0, 100.0, 5.0);
    instance.evaluate(&snap);

    // Price reaches target (115)
    let snap = make_snapshot(1, 40.0, 116.0, 5.0);
    let signal = instance.evaluate(&snap);

    assert_eq!(
        signal,
        Signal::Exit {
            reason: ExitReason::TargetHit
        }
    );
}

#[test]
fn in_position_exits_on_signal_reversal() {
    let def = test_strategy();
    let mut instance = StrategyInstance::new(def, 10);

    // Enter long (RSI < 30)
    let snap = make_snapshot(1, 25.0, 100.0, 5.0);
    instance.evaluate(&snap);

    // RSI > 50 → exit condition (signal reversal)
    // Price between stop (90) and target (115) so neither SL nor target hit
    let snap = make_snapshot(1, 55.0, 105.0, 5.0);
    let signal = instance.evaluate(&snap);

    assert_eq!(
        signal,
        Signal::Exit {
            reason: ExitReason::SignalReversal
        }
    );
}

#[test]
fn holds_position_when_price_in_range() {
    let def = test_strategy();
    let mut instance = StrategyInstance::new(def, 10);

    // Enter long
    let snap = make_snapshot(1, 25.0, 100.0, 5.0);
    instance.evaluate(&snap);

    // Price in range, RSI < 50 (exit condition not met)
    let snap = make_snapshot(1, 35.0, 105.0, 5.0);
    let signal = instance.evaluate(&snap);

    assert_eq!(signal, Signal::Hold);
}

// -----------------------------------------------------------------------
// Confirmation Ticks Tests
// -----------------------------------------------------------------------

#[test]
fn confirmation_delays_entry() {
    let mut def = test_strategy();
    def.confirmation_ticks = 3;
    let mut instance = StrategyInstance::new(def, 10);

    // First tick with entry condition: should wait
    let snap = make_snapshot(1, 25.0, 100.0, 5.0);
    let signal = instance.evaluate(&snap);
    assert_eq!(signal, Signal::Hold);

    // Second tick: still waiting
    let snap = make_snapshot(1, 25.0, 100.0, 5.0);
    let signal = instance.evaluate(&snap);
    assert_eq!(signal, Signal::Hold);

    // Third tick: still waiting
    let snap = make_snapshot(1, 25.0, 100.0, 5.0);
    let signal = instance.evaluate(&snap);
    assert_eq!(signal, Signal::Hold);

    // Fourth tick: confirmation complete, should enter
    let snap = make_snapshot(1, 25.0, 100.0, 5.0);
    let signal = instance.evaluate(&snap);

    match signal {
        Signal::EnterLong { .. } => {} // expected
        other => panic!("Expected EnterLong after confirmation, got {other:?}"),
    }
}

#[test]
fn confirmation_aborts_if_conditions_fail() {
    let mut def = test_strategy();
    def.confirmation_ticks = 3;
    let mut instance = StrategyInstance::new(def, 10);

    // First tick: entry condition met
    let snap = make_snapshot(1, 25.0, 100.0, 5.0);
    instance.evaluate(&snap);

    // Second tick: conditions no longer met (RSI back to 50)
    // Need to wait 3 ticks, but conditions should be rechecked at confirmation
    let snap = make_snapshot(1, 50.0, 100.0, 5.0);
    instance.evaluate(&snap);
    let snap = make_snapshot(1, 50.0, 100.0, 5.0);
    instance.evaluate(&snap);

    // After confirmation period, RSI = 50 → conditions not met → back to idle
    let snap = make_snapshot(1, 50.0, 100.0, 5.0);
    let signal = instance.evaluate(&snap);
    assert_eq!(signal, Signal::Hold);
}

// -----------------------------------------------------------------------
// Trailing Stop Tests
// -----------------------------------------------------------------------

#[test]
fn trailing_stop_exits_on_pullback() {
    let mut def = test_strategy();
    def.trailing_stop_enabled = true;
    def.trailing_stop_atr_multiplier = 1.0;
    let mut instance = StrategyInstance::new(def, 10);

    // Enter long at 100, stop at 90, target at 115
    let snap = make_snapshot(1, 25.0, 100.0, 5.0);
    instance.evaluate(&snap);

    // Price rises to 110 (RSI stays low to avoid exit condition)
    let snap = make_snapshot(1, 35.0, 110.0, 5.0);
    instance.evaluate(&snap);

    // Price drops to 104 — trailing stop = 110 - 1*5 = 105
    // 104 < 105, but trailing stop (105) > original stop (90), so trailing triggers
    let snap = make_snapshot(1, 35.0, 104.0, 5.0);
    let signal = instance.evaluate(&snap);

    assert_eq!(
        signal,
        Signal::Exit {
            reason: ExitReason::TrailingStop
        }
    );
}

// -----------------------------------------------------------------------
// Reset State Tests
// -----------------------------------------------------------------------

#[test]
fn reset_state_returns_to_idle() {
    let def = test_strategy();
    let mut instance = StrategyInstance::new(def, 10);

    // Enter a position
    let snap = make_snapshot(1, 25.0, 100.0, 5.0);
    instance.evaluate(&snap);

    // Reset
    instance.reset_state(1);

    // Should be back to idle — hold with neutral RSI
    let snap = make_snapshot(1, 50.0, 100.0, 5.0);
    let signal = instance.evaluate(&snap);
    assert_eq!(signal, Signal::Hold);
}

// -----------------------------------------------------------------------
// Not Warm Tests
// -----------------------------------------------------------------------

#[test]
fn not_warm_always_holds() {
    let def = test_strategy();
    let mut instance = StrategyInstance::new(def, 10);

    // RSI < 30 but not warm
    let mut snap = make_snapshot(1, 25.0, 100.0, 5.0);
    snap.is_warm = false;
    let signal = instance.evaluate(&snap);

    assert_eq!(signal, Signal::Hold);
}

// -----------------------------------------------------------------------
// Condition Evaluation Tests
// -----------------------------------------------------------------------

#[test]
fn condition_gt_evaluates_correctly() {
    let cond = Condition {
        field: IndicatorField::Rsi,
        operator: ComparisonOp::Gt,
        threshold: 70.0,
    };

    let snap = make_snapshot(1, 75.0, 100.0, 5.0);
    assert!(cond.evaluate(&snap));

    let snap = make_snapshot(1, 65.0, 100.0, 5.0);
    assert!(!cond.evaluate(&snap));
}

#[test]
fn condition_lt_evaluates_correctly() {
    let cond = Condition {
        field: IndicatorField::Rsi,
        operator: ComparisonOp::Lt,
        threshold: 30.0,
    };

    let snap = make_snapshot(1, 25.0, 100.0, 5.0);
    assert!(cond.evaluate(&snap));

    let snap = make_snapshot(1, 35.0, 100.0, 5.0);
    assert!(!cond.evaluate(&snap));
}

#[test]
fn indicator_field_reads_correct_values() {
    let snap = IndicatorSnapshot {
        security_id: 1,
        rsi: 42.0,
        ema_fast: 100.0,
        ema_slow: 95.0,
        macd_line: 5.0,
        atr: 3.5,
        adx: 25.0,
        vwap: 98.0,
        obv: 50000.0,
        is_warm: true,
        ..Default::default()
    };

    assert!((IndicatorField::Rsi.read(&snap) - 42.0).abs() < f64::EPSILON);
    assert!((IndicatorField::EmaFast.read(&snap) - 100.0).abs() < f64::EPSILON);
    assert!((IndicatorField::EmaSlow.read(&snap) - 95.0).abs() < f64::EPSILON);
    assert!((IndicatorField::MacdLine.read(&snap) - 5.0).abs() < f64::EPSILON);
    assert!((IndicatorField::Atr.read(&snap) - 3.5).abs() < f64::EPSILON);
    assert!((IndicatorField::Adx.read(&snap) - 25.0).abs() < f64::EPSILON);
    assert!((IndicatorField::Vwap.read(&snap) - 98.0).abs() < f64::EPSILON);
    assert!((IndicatorField::Obv.read(&snap) - 50000.0).abs() < f64::EPSILON);
}

// -----------------------------------------------------------------------
// Remaining IndicatorField read variants
// -----------------------------------------------------------------------

#[test]
fn indicator_field_reads_remaining_variants() {
    let snap = IndicatorSnapshot {
        security_id: 1,
        macd_signal: 2.5,
        macd_histogram: 1.5,
        sma: 99.0,
        bollinger_upper: 110.0,
        bollinger_middle: 100.0,
        bollinger_lower: 90.0,
        supertrend: 97.0,
        last_traded_price: 101.5,
        volume: 25000.0,
        day_high: 105.0,
        day_low: 96.0,
        is_warm: true,
        ..Default::default()
    };

    assert!((IndicatorField::MacdSignal.read(&snap) - 2.5).abs() < f64::EPSILON);
    assert!((IndicatorField::MacdHistogram.read(&snap) - 1.5).abs() < f64::EPSILON);
    assert!((IndicatorField::Sma.read(&snap) - 99.0).abs() < f64::EPSILON);
    assert!((IndicatorField::BollingerUpper.read(&snap) - 110.0).abs() < f64::EPSILON);
    assert!((IndicatorField::BollingerMiddle.read(&snap) - 100.0).abs() < f64::EPSILON);
    assert!((IndicatorField::BollingerLower.read(&snap) - 90.0).abs() < f64::EPSILON);
    assert!((IndicatorField::Supertrend.read(&snap) - 97.0).abs() < f64::EPSILON);
    assert!((IndicatorField::LastTradedPrice.read(&snap) - 101.5).abs() < f64::EPSILON);
    assert!((IndicatorField::Volume.read(&snap) - 25000.0).abs() < f64::EPSILON);
    assert!((IndicatorField::DayHigh.read(&snap) - 105.0).abs() < f64::EPSILON);
    assert!((IndicatorField::DayLow.read(&snap) - 96.0).abs() < f64::EPSILON);
}

// -----------------------------------------------------------------------
// Remaining ComparisonOp variants
// -----------------------------------------------------------------------

#[test]
fn condition_gte_evaluates_correctly() {
    let cond = Condition {
        field: IndicatorField::Rsi,
        operator: ComparisonOp::Gte,
        threshold: 50.0,
    };

    let snap = make_snapshot(1, 50.0, 100.0, 5.0); // equal
    assert!(cond.evaluate(&snap));

    let snap = make_snapshot(1, 51.0, 100.0, 5.0); // above
    assert!(cond.evaluate(&snap));

    let snap = make_snapshot(1, 49.0, 100.0, 5.0); // below
    assert!(!cond.evaluate(&snap));
}

#[test]
fn condition_lte_evaluates_correctly() {
    let cond = Condition {
        field: IndicatorField::Rsi,
        operator: ComparisonOp::Lte,
        threshold: 50.0,
    };

    let snap = make_snapshot(1, 50.0, 100.0, 5.0); // equal
    assert!(cond.evaluate(&snap));

    let snap = make_snapshot(1, 49.0, 100.0, 5.0); // below
    assert!(cond.evaluate(&snap));

    let snap = make_snapshot(1, 51.0, 100.0, 5.0); // above
    assert!(!cond.evaluate(&snap));
}

#[test]
fn condition_cross_above_evaluates_as_gt() {
    let cond = Condition {
        field: IndicatorField::Rsi,
        operator: ComparisonOp::CrossAbove,
        threshold: 50.0,
    };

    let snap = make_snapshot(1, 51.0, 100.0, 5.0);
    assert!(cond.evaluate(&snap));

    let snap = make_snapshot(1, 50.0, 100.0, 5.0);
    assert!(!cond.evaluate(&snap));
}

#[test]
fn condition_cross_below_evaluates_as_lt() {
    let cond = Condition {
        field: IndicatorField::Rsi,
        operator: ComparisonOp::CrossBelow,
        threshold: 50.0,
    };

    let snap = make_snapshot(1, 49.0, 100.0, 5.0);
    assert!(cond.evaluate(&snap));

    let snap = make_snapshot(1, 50.0, 100.0, 5.0);
    assert!(!cond.evaluate(&snap));
}

// -----------------------------------------------------------------------
// StrategyState Default Tests
// -----------------------------------------------------------------------

#[test]
fn strategy_state_default_is_idle() {
    let state = StrategyState::default();
    assert_eq!(state, StrategyState::Idle);
}

// -----------------------------------------------------------------------
// Out-of-bounds security_id
// -----------------------------------------------------------------------

#[test]
fn out_of_bounds_sid_holds() {
    let def = test_strategy();
    let mut instance = StrategyInstance::new(def, 5);

    let snap = make_snapshot(100, 25.0, 100.0, 5.0); // sid=100, max=5
    let signal = instance.evaluate(&snap);
    assert_eq!(signal, Signal::Hold);
}
// O(1) EXEMPT: end
