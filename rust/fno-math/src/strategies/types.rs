//! Strategy types: FSM states, signals, and configuration.
//!
//! Strategy evaluation is a state machine transition — Rust enum `match`
//! compiles to a jump table for O(1) dispatch.

use crate::indicators::IndicatorSnapshot;

// ---------------------------------------------------------------------------
// Trade Signal — output of strategy evaluation
// ---------------------------------------------------------------------------

/// A trade signal emitted by a strategy FSM.
///
/// `Copy` for zero-allocation passing to the OMS.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Signal {
    /// Enter a long position.
    EnterLong {
        /// Suggested position size as fraction of capital (0.0 - 1.0).
        size_fraction: f64,
        /// Stop-loss price.
        stop_loss: f64,
        /// Target price.
        target: f64,
    },
    /// Enter a short position.
    EnterShort {
        /// Suggested position size as fraction of capital (0.0 - 1.0).
        size_fraction: f64,
        /// Stop-loss price.
        stop_loss: f64,
        /// Target price.
        target: f64,
    },
    /// Exit current position.
    Exit {
        /// Reason for exit.
        reason: ExitReason,
    },
    /// No action — hold current position (or stay flat).
    Hold,
}

/// Reason for exiting a position.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExitReason {
    /// Target price reached.
    TargetHit,
    /// Stop-loss triggered.
    StopLossHit,
    /// Strategy signal reversed.
    SignalReversal,
    /// End of trading session.
    SessionEnd,
    /// Trailing stop triggered.
    TrailingStop,
}

// ---------------------------------------------------------------------------
// Strategy FSM State — enum for O(1) jump-table dispatch
// ---------------------------------------------------------------------------

/// FSM state for a single strategy instance.
///
/// The Rust compiler generates a jump table for `match` on this enum,
/// giving O(1) state dispatch regardless of the number of states.
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub enum StrategyState {
    /// Waiting for entry conditions.
    #[default]
    Idle,

    /// Entry conditions partially met — waiting for confirmation.
    WaitingForConfirmation {
        /// The tick count when the signal was first detected.
        signal_tick: u32,
        /// Signal strength (0.0 - 1.0).
        strength: f64,
        /// Whether the anticipated direction is long (true) or short (false).
        is_long: bool,
    },

    /// Currently in a position.
    InPosition {
        /// Entry price.
        entry_price: f64,
        /// Stop-loss price.
        stop_loss: f64,
        /// Target price.
        target: f64,
        /// Whether long (true) or short (false).
        is_long: bool,
        /// Highest price since entry (for trailing stop).
        highest_since_entry: f64,
        /// Lowest price since entry (for trailing stop).
        lowest_since_entry: f64,
    },

    /// Position exit pending — waiting for OMS confirmation.
    ExitPending {
        /// Reason for the exit.
        reason: ExitReason,
    },
}

// ---------------------------------------------------------------------------
// Strategy Condition — declarative entry/exit rules
// ---------------------------------------------------------------------------

/// A single condition that can be evaluated against an indicator snapshot.
///
/// Conditions are loaded from TOML configuration and evaluated in O(1)
/// per condition (single field access + comparison).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Condition {
    /// Which indicator field to read.
    pub field: IndicatorField,
    /// Comparison operator.
    pub operator: ComparisonOp,
    /// Threshold value.
    pub threshold: f64,
}

impl Condition {
    /// Evaluates this condition against an indicator snapshot.
    ///
    /// O(1) — one field read + one comparison.
    #[inline(always)]
    pub fn evaluate(&self, snapshot: &IndicatorSnapshot) -> bool {
        let value = self.field.read(snapshot);
        self.operator.compare(value, self.threshold)
    }
}

/// Which indicator field to read from a snapshot.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IndicatorField {
    Rsi,
    MacdLine,
    MacdSignal,
    MacdHistogram,
    EmaFast,
    EmaSlow,
    Sma,
    Vwap,
    BollingerUpper,
    BollingerMiddle,
    BollingerLower,
    Atr,
    Supertrend,
    Adx,
    Obv,
    LastTradedPrice,
    Volume,
    DayHigh,
    DayLow,
}

impl IndicatorField {
    /// Reads the corresponding value from a snapshot.
    ///
    /// O(1) — compiles to a jump table or direct offset load.
    #[inline(always)]
    pub fn read(self, snapshot: &IndicatorSnapshot) -> f64 {
        match self {
            Self::Rsi => snapshot.rsi,
            Self::MacdLine => snapshot.macd_line,
            Self::MacdSignal => snapshot.macd_signal,
            Self::MacdHistogram => snapshot.macd_histogram,
            Self::EmaFast => snapshot.ema_fast,
            Self::EmaSlow => snapshot.ema_slow,
            Self::Sma => snapshot.sma,
            Self::Vwap => snapshot.vwap,
            Self::BollingerUpper => snapshot.bollinger_upper,
            Self::BollingerMiddle => snapshot.bollinger_middle,
            Self::BollingerLower => snapshot.bollinger_lower,
            Self::Atr => snapshot.atr,
            Self::Supertrend => snapshot.supertrend,
            Self::Adx => snapshot.adx,
            Self::Obv => snapshot.obv,
            Self::LastTradedPrice => snapshot.last_traded_price,
            Self::Volume => snapshot.volume,
            Self::DayHigh => snapshot.day_high,
            Self::DayLow => snapshot.day_low,
        }
    }
}

/// Comparison operator for conditions.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ComparisonOp {
    /// Greater than.
    Gt,
    /// Greater than or equal.
    Gte,
    /// Less than.
    Lt,
    /// Less than or equal.
    Lte,
    /// Crosses above (requires previous state — approximated as Gt).
    CrossAbove,
    /// Crosses below (requires previous state — approximated as Lt).
    CrossBelow,
}

impl ComparisonOp {
    /// Compares two values.
    #[inline(always)]
    pub fn compare(self, value: f64, threshold: f64) -> bool {
        match self {
            Self::Gt | Self::CrossAbove => value > threshold,
            Self::Gte => value >= threshold,
            Self::Lt | Self::CrossBelow => value < threshold,
            Self::Lte => value <= threshold,
        }
    }
}

// ---------------------------------------------------------------------------
// Strategy Definition — loaded from TOML
// ---------------------------------------------------------------------------

/// A complete strategy definition with entry/exit rules.
///
/// Loaded from TOML at startup or via hot-reload.
/// Contains no heap allocations in the evaluation path.
#[derive(Debug, Clone)]
pub struct StrategyDefinition {
    /// Human-readable strategy name.
    pub name: String,
    /// Security IDs this strategy applies to.
    pub security_ids: Vec<u32>,
    /// All entry conditions must be true to trigger entry (AND logic).
    pub entry_long_conditions: Vec<Condition>,
    /// All short entry conditions must be true (AND logic).
    pub entry_short_conditions: Vec<Condition>,
    /// Any exit condition triggers an exit (OR logic).
    pub exit_conditions: Vec<Condition>,
    /// Position size as fraction of capital (0.0 - 1.0).
    pub position_size_fraction: f64,
    /// ATR multiplier for stop-loss calculation.
    pub stop_loss_atr_multiplier: f64,
    /// ATR multiplier for target calculation.
    pub target_atr_multiplier: f64,
    /// Number of ticks to wait for confirmation after initial signal.
    pub confirmation_ticks: u32,
    /// Enable trailing stop-loss.
    pub trailing_stop_enabled: bool,
    /// Trailing stop ATR multiplier (only used if trailing_stop_enabled).
    pub trailing_stop_atr_multiplier: f64,
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::indicators::IndicatorSnapshot;

    // --- Helper: build a snapshot with specific field values ---

    fn snapshot_with(field: IndicatorField, value: f64) -> IndicatorSnapshot {
        let mut snap = IndicatorSnapshot::default();
        match field {
            IndicatorField::Rsi => snap.rsi = value,
            IndicatorField::MacdLine => snap.macd_line = value,
            IndicatorField::MacdSignal => snap.macd_signal = value,
            IndicatorField::MacdHistogram => snap.macd_histogram = value,
            IndicatorField::EmaFast => snap.ema_fast = value,
            IndicatorField::EmaSlow => snap.ema_slow = value,
            IndicatorField::Sma => snap.sma = value,
            IndicatorField::Vwap => snap.vwap = value,
            IndicatorField::BollingerUpper => snap.bollinger_upper = value,
            IndicatorField::BollingerMiddle => snap.bollinger_middle = value,
            IndicatorField::BollingerLower => snap.bollinger_lower = value,
            IndicatorField::Atr => snap.atr = value,
            IndicatorField::Supertrend => snap.supertrend = value,
            IndicatorField::Adx => snap.adx = value,
            IndicatorField::Obv => snap.obv = value,
            IndicatorField::LastTradedPrice => snap.last_traded_price = value,
            IndicatorField::Volume => snap.volume = value,
            IndicatorField::DayHigh => snap.day_high = value,
            IndicatorField::DayLow => snap.day_low = value,
        }
        snap
    }

    // --- IndicatorField::read() — all 19 variants ---

    #[test]
    fn test_indicator_field_read_rsi() {
        let snap = snapshot_with(IndicatorField::Rsi, 65.5);
        assert!((IndicatorField::Rsi.read(&snap) - 65.5).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_macd_line() {
        let snap = snapshot_with(IndicatorField::MacdLine, -1.23);
        assert!((IndicatorField::MacdLine.read(&snap) - -1.23).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_macd_signal() {
        let snap = snapshot_with(IndicatorField::MacdSignal, 0.45);
        assert!((IndicatorField::MacdSignal.read(&snap) - 0.45).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_macd_histogram() {
        let snap = snapshot_with(IndicatorField::MacdHistogram, -0.78);
        assert!((IndicatorField::MacdHistogram.read(&snap) - -0.78).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_ema_fast() {
        let snap = snapshot_with(IndicatorField::EmaFast, 100.25);
        assert!((IndicatorField::EmaFast.read(&snap) - 100.25).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_ema_slow() {
        let snap = snapshot_with(IndicatorField::EmaSlow, 99.75);
        assert!((IndicatorField::EmaSlow.read(&snap) - 99.75).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_sma() {
        let snap = snapshot_with(IndicatorField::Sma, 101.0);
        assert!((IndicatorField::Sma.read(&snap) - 101.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_vwap() {
        let snap = snapshot_with(IndicatorField::Vwap, 102.5);
        assert!((IndicatorField::Vwap.read(&snap) - 102.5).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_bollinger_upper() {
        let snap = snapshot_with(IndicatorField::BollingerUpper, 110.0);
        assert!((IndicatorField::BollingerUpper.read(&snap) - 110.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_bollinger_middle() {
        let snap = snapshot_with(IndicatorField::BollingerMiddle, 100.0);
        assert!((IndicatorField::BollingerMiddle.read(&snap) - 100.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_bollinger_lower() {
        let snap = snapshot_with(IndicatorField::BollingerLower, 90.0);
        assert!((IndicatorField::BollingerLower.read(&snap) - 90.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_atr() {
        let snap = snapshot_with(IndicatorField::Atr, 2.5);
        assert!((IndicatorField::Atr.read(&snap) - 2.5).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_supertrend() {
        let snap = snapshot_with(IndicatorField::Supertrend, 98.0);
        assert!((IndicatorField::Supertrend.read(&snap) - 98.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_adx() {
        let snap = snapshot_with(IndicatorField::Adx, 35.0);
        assert!((IndicatorField::Adx.read(&snap) - 35.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_obv() {
        let snap = snapshot_with(IndicatorField::Obv, 1_000_000.0);
        assert!((IndicatorField::Obv.read(&snap) - 1_000_000.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_last_traded_price() {
        let snap = snapshot_with(IndicatorField::LastTradedPrice, 21004.95);
        assert!((IndicatorField::LastTradedPrice.read(&snap) - 21004.95).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_volume() {
        let snap = snapshot_with(IndicatorField::Volume, 5_000_000.0);
        assert!((IndicatorField::Volume.read(&snap) - 5_000_000.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_day_high() {
        let snap = snapshot_with(IndicatorField::DayHigh, 21050.0);
        assert!((IndicatorField::DayHigh.read(&snap) - 21050.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_indicator_field_read_day_low() {
        let snap = snapshot_with(IndicatorField::DayLow, 20950.0);
        assert!((IndicatorField::DayLow.read(&snap) - 20950.0).abs() < f64::EPSILON);
    }

    // --- IndicatorField::read() — reads correct field, not another ---

    #[test]
    fn test_indicator_field_read_isolation() {
        // Set rsi=70, everything else default (0.0). Verify only Rsi reads 70.
        let snap = snapshot_with(IndicatorField::Rsi, 70.0);
        assert!((IndicatorField::Rsi.read(&snap) - 70.0).abs() < f64::EPSILON);
        assert!((IndicatorField::MacdLine.read(&snap) - 0.0).abs() < f64::EPSILON);
        assert!((IndicatorField::Atr.read(&snap) - 0.0).abs() < f64::EPSILON);
        assert!((IndicatorField::Volume.read(&snap) - 0.0).abs() < f64::EPSILON);
    }

    // --- ComparisonOp::compare() — all 6 variants ---

    #[test]
    fn test_comparison_op_gt_true() {
        assert!(ComparisonOp::Gt.compare(10.0, 5.0));
    }

    #[test]
    fn test_comparison_op_gt_false_equal() {
        assert!(!ComparisonOp::Gt.compare(5.0, 5.0));
    }

    #[test]
    fn test_comparison_op_gt_false_less() {
        assert!(!ComparisonOp::Gt.compare(3.0, 5.0));
    }

    #[test]
    fn test_comparison_op_gte_true_greater() {
        assert!(ComparisonOp::Gte.compare(10.0, 5.0));
    }

    #[test]
    fn test_comparison_op_gte_true_equal() {
        assert!(ComparisonOp::Gte.compare(5.0, 5.0));
    }

    #[test]
    fn test_comparison_op_gte_false() {
        assert!(!ComparisonOp::Gte.compare(3.0, 5.0));
    }

    #[test]
    fn test_comparison_op_lt_true() {
        assert!(ComparisonOp::Lt.compare(3.0, 5.0));
    }

    #[test]
    fn test_comparison_op_lt_false_equal() {
        assert!(!ComparisonOp::Lt.compare(5.0, 5.0));
    }

    #[test]
    fn test_comparison_op_lt_false_greater() {
        assert!(!ComparisonOp::Lt.compare(10.0, 5.0));
    }

    #[test]
    fn test_comparison_op_lte_true_less() {
        assert!(ComparisonOp::Lte.compare(3.0, 5.0));
    }

    #[test]
    fn test_comparison_op_lte_true_equal() {
        assert!(ComparisonOp::Lte.compare(5.0, 5.0));
    }

    #[test]
    fn test_comparison_op_lte_false() {
        assert!(!ComparisonOp::Lte.compare(10.0, 5.0));
    }

    #[test]
    fn test_comparison_op_cross_above_behaves_as_gt() {
        assert!(ComparisonOp::CrossAbove.compare(10.0, 5.0));
        assert!(!ComparisonOp::CrossAbove.compare(5.0, 5.0));
        assert!(!ComparisonOp::CrossAbove.compare(3.0, 5.0));
    }

    #[test]
    fn test_comparison_op_cross_below_behaves_as_lt() {
        assert!(ComparisonOp::CrossBelow.compare(3.0, 5.0));
        assert!(!ComparisonOp::CrossBelow.compare(5.0, 5.0));
        assert!(!ComparisonOp::CrossBelow.compare(10.0, 5.0));
    }

    // --- Condition::evaluate() ---

    #[test]
    fn test_condition_evaluate_rsi_gt_70_true() {
        let cond = Condition {
            field: IndicatorField::Rsi,
            operator: ComparisonOp::Gt,
            threshold: 70.0,
        };
        let snap = snapshot_with(IndicatorField::Rsi, 75.0);
        assert!(cond.evaluate(&snap));
    }

    #[test]
    fn test_condition_evaluate_rsi_gt_70_false() {
        let cond = Condition {
            field: IndicatorField::Rsi,
            operator: ComparisonOp::Gt,
            threshold: 70.0,
        };
        let snap = snapshot_with(IndicatorField::Rsi, 65.0);
        assert!(!cond.evaluate(&snap));
    }

    #[test]
    fn test_condition_evaluate_macd_lt_zero() {
        let cond = Condition {
            field: IndicatorField::MacdHistogram,
            operator: ComparisonOp::Lt,
            threshold: 0.0,
        };
        let snap = snapshot_with(IndicatorField::MacdHistogram, -0.5);
        assert!(cond.evaluate(&snap));
    }

    #[test]
    fn test_condition_evaluate_ltp_gte_threshold() {
        let cond = Condition {
            field: IndicatorField::LastTradedPrice,
            operator: ComparisonOp::Gte,
            threshold: 100.0,
        };
        let snap = snapshot_with(IndicatorField::LastTradedPrice, 100.0);
        assert!(cond.evaluate(&snap));
    }

    #[test]
    fn test_condition_evaluate_atr_lte_threshold() {
        let cond = Condition {
            field: IndicatorField::Atr,
            operator: ComparisonOp::Lte,
            threshold: 5.0,
        };
        let snap = snapshot_with(IndicatorField::Atr, 5.0);
        assert!(cond.evaluate(&snap));
    }

    #[test]
    fn test_condition_evaluate_cross_above_composition() {
        let cond = Condition {
            field: IndicatorField::EmaFast,
            operator: ComparisonOp::CrossAbove,
            threshold: 100.0,
        };
        let snap = snapshot_with(IndicatorField::EmaFast, 100.5);
        assert!(cond.evaluate(&snap));
    }

    #[test]
    fn test_condition_evaluate_cross_below_composition() {
        let cond = Condition {
            field: IndicatorField::EmaSlow,
            operator: ComparisonOp::CrossBelow,
            threshold: 100.0,
        };
        let snap = snapshot_with(IndicatorField::EmaSlow, 99.5);
        assert!(cond.evaluate(&snap));
    }

    // --- Signal enum ---

    #[test]
    fn test_signal_enter_long_construction() {
        let sig = Signal::EnterLong {
            size_fraction: 0.5,
            stop_loss: 95.0,
            target: 110.0,
        };
        assert_eq!(
            sig,
            Signal::EnterLong {
                size_fraction: 0.5,
                stop_loss: 95.0,
                target: 110.0,
            }
        );
    }

    #[test]
    fn test_signal_enter_short_construction() {
        let sig = Signal::EnterShort {
            size_fraction: 0.3,
            stop_loss: 105.0,
            target: 90.0,
        };
        assert_eq!(
            sig,
            Signal::EnterShort {
                size_fraction: 0.3,
                stop_loss: 105.0,
                target: 90.0,
            }
        );
    }

    #[test]
    fn test_signal_exit_construction() {
        let sig = Signal::Exit {
            reason: ExitReason::TargetHit,
        };
        assert_eq!(
            sig,
            Signal::Exit {
                reason: ExitReason::TargetHit,
            }
        );
    }

    #[test]
    fn test_signal_hold_construction() {
        let sig = Signal::Hold;
        assert_eq!(sig, Signal::Hold);
    }

    #[test]
    fn test_signal_variants_not_equal() {
        let long = Signal::EnterLong {
            size_fraction: 0.5,
            stop_loss: 95.0,
            target: 110.0,
        };
        let hold = Signal::Hold;
        assert_ne!(long, hold);
    }

    #[test]
    fn test_signal_is_copy() {
        let sig = Signal::Hold;
        let copy = sig;
        assert_eq!(sig, copy);
    }

    // --- ExitReason enum ---

    #[test]
    fn test_exit_reason_all_variants() {
        let reasons = [
            ExitReason::TargetHit,
            ExitReason::StopLossHit,
            ExitReason::SignalReversal,
            ExitReason::SessionEnd,
            ExitReason::TrailingStop,
        ];
        // All variants are distinct
        for i in 0..reasons.len() {
            for j in (i + 1)..reasons.len() {
                assert_ne!(reasons[i], reasons[j]);
            }
        }
    }

    #[test]
    fn test_exit_reason_is_copy() {
        let reason = ExitReason::StopLossHit;
        let copy = reason;
        assert_eq!(reason, copy);
    }

    // --- StrategyState ---

    #[test]
    fn test_strategy_state_default_is_idle() {
        let state = StrategyState::default();
        assert_eq!(state, StrategyState::Idle);
    }

    #[test]
    fn test_strategy_state_waiting_for_confirmation() {
        let state = StrategyState::WaitingForConfirmation {
            signal_tick: 42,
            strength: 0.85,
            is_long: true,
        };
        assert_eq!(
            state,
            StrategyState::WaitingForConfirmation {
                signal_tick: 42,
                strength: 0.85,
                is_long: true,
            }
        );
    }

    #[test]
    fn test_strategy_state_in_position() {
        let state = StrategyState::InPosition {
            entry_price: 100.0,
            stop_loss: 95.0,
            target: 110.0,
            is_long: true,
            highest_since_entry: 102.0,
            lowest_since_entry: 99.0,
        };
        assert_eq!(
            state,
            StrategyState::InPosition {
                entry_price: 100.0,
                stop_loss: 95.0,
                target: 110.0,
                is_long: true,
                highest_since_entry: 102.0,
                lowest_since_entry: 99.0,
            }
        );
    }

    #[test]
    fn test_strategy_state_exit_pending() {
        let state = StrategyState::ExitPending {
            reason: ExitReason::SessionEnd,
        };
        assert_eq!(
            state,
            StrategyState::ExitPending {
                reason: ExitReason::SessionEnd,
            }
        );
    }

    #[test]
    fn test_strategy_state_variants_not_equal() {
        assert_ne!(
            StrategyState::Idle,
            StrategyState::ExitPending {
                reason: ExitReason::TargetHit,
            }
        );
    }

    #[test]
    fn test_strategy_state_is_copy() {
        let state = StrategyState::Idle;
        let copy = state;
        assert_eq!(state, copy);
    }

    // --- Condition is Copy ---

    #[test]
    fn test_condition_is_copy() {
        let cond = Condition {
            field: IndicatorField::Rsi,
            operator: ComparisonOp::Gt,
            threshold: 70.0,
        };
        let copy = cond;
        assert_eq!(cond, copy);
    }

    // --- ComparisonOp boundary: exact equality ---

    #[test]
    fn test_comparison_op_boundary_values() {
        // Gt: exactly equal returns false
        assert!(!ComparisonOp::Gt.compare(70.0, 70.0));
        // Gte: exactly equal returns true
        assert!(ComparisonOp::Gte.compare(70.0, 70.0));
        // Lt: exactly equal returns false
        assert!(!ComparisonOp::Lt.compare(70.0, 70.0));
        // Lte: exactly equal returns true
        assert!(ComparisonOp::Lte.compare(70.0, 70.0));
    }

    // --- ComparisonOp with negative values ---

    #[test]
    fn test_comparison_op_negative_values() {
        assert!(ComparisonOp::Gt.compare(-1.0, -5.0));
        assert!(ComparisonOp::Lt.compare(-5.0, -1.0));
        assert!(ComparisonOp::Gte.compare(-1.0, -1.0));
        assert!(ComparisonOp::Lte.compare(-1.0, -1.0));
    }
}
