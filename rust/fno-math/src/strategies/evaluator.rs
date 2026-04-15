//! Strategy evaluator — O(1) FSM state machine per strategy instance.
//!
//! Each strategy instance maintains a `StrategyState` enum. Evaluation
//! is a `match` on the enum — compiles to a jump table for O(1) dispatch.

use crate::indicators::IndicatorSnapshot;

use super::types::{ExitReason, Signal, StrategyDefinition, StrategyState};

// ---------------------------------------------------------------------------
// Strategy Instance — single FSM
// ---------------------------------------------------------------------------

/// A single strategy instance: definition + current FSM state.
///
/// Each instance tracks one strategy on one or more instruments.
/// The FSM state is `Copy` — zero allocation on state transitions.
pub struct StrategyInstance {
    /// The strategy rules (loaded from TOML).
    definition: StrategyDefinition,
    /// Current FSM state per security_id.
    /// Flat Vec indexed by security_id for O(1) lookup.
    states: Vec<StrategyState>,
    /// Tick counter (monotonically increasing).
    tick_counter: u32,
}

impl StrategyInstance {
    /// Creates a new strategy instance from a definition.
    ///
    /// Pre-allocates state for all tracked security IDs.
    pub fn new(definition: StrategyDefinition, max_security_id: usize) -> Self {
        // O(1) EXEMPT: constructor pre-allocation, called once at startup
        let states = vec![StrategyState::default(); max_security_id];
        Self {
            definition,
            states,
            tick_counter: 0,
        }
    }

    /// Evaluates the strategy for a given indicator snapshot.
    ///
    /// # Performance
    /// O(C) where C = number of conditions (typically 2-5).
    /// FSM dispatch is O(1) via jump table. Total: ~5-50ns.
    ///
    /// # Returns
    /// The resulting `Signal` (Hold, EnterLong, EnterShort, or Exit).
    #[allow(clippy::arithmetic_side_effects)] // APPROVED: tick_counter saturating_add; ATR multiplications are bounded
    pub fn evaluate(&mut self, snapshot: &IndicatorSnapshot) -> Signal {
        let sid = snapshot.security_id as usize;
        if sid >= self.states.len() || !snapshot.is_warm {
            return Signal::Hold;
        }

        self.tick_counter = self.tick_counter.saturating_add(1);
        let state = self.states[sid];
        let (new_state, signal) = self.transition(state, snapshot);
        self.states[sid] = new_state;
        signal
    }

    /// FSM transition function — the core O(1) dispatch.
    ///
    /// `match` on `StrategyState` compiles to a jump table.
    #[allow(clippy::arithmetic_side_effects)] // APPROVED: ATR-based price calculations are bounded finite f64
    fn transition(
        &self,
        state: StrategyState,
        snapshot: &IndicatorSnapshot,
    ) -> (StrategyState, Signal) {
        match state {
            StrategyState::Idle => self.evaluate_idle(snapshot),

            StrategyState::WaitingForConfirmation {
                signal_tick,
                strength: _,
                is_long,
            } => self.evaluate_waiting(snapshot, signal_tick, is_long),

            StrategyState::InPosition {
                entry_price,
                stop_loss,
                target,
                is_long,
                highest_since_entry,
                lowest_since_entry,
            } => self.evaluate_in_position(
                snapshot,
                entry_price,
                stop_loss,
                target,
                is_long,
                highest_since_entry,
                lowest_since_entry,
            ),

            StrategyState::ExitPending { reason } => {
                // Stay in ExitPending until OMS confirms the exit.
                // The OMS will reset state to Idle after fill.
                (StrategyState::ExitPending { reason }, Signal::Hold)
            }
        }
    }

    /// Idle state: check entry conditions.
    fn evaluate_idle(&self, snapshot: &IndicatorSnapshot) -> (StrategyState, Signal) {
        // Check long entry conditions (AND logic)
        let long_entry = !self.definition.entry_long_conditions.is_empty()
            && self
                .definition
                .entry_long_conditions
                .iter()
                .all(|cond| cond.evaluate(snapshot));

        // Check short entry conditions (AND logic)
        let short_entry = !self.definition.entry_short_conditions.is_empty()
            && self
                .definition
                .entry_short_conditions
                .iter()
                .all(|cond| cond.evaluate(snapshot));

        if long_entry {
            if self.definition.confirmation_ticks == 0 {
                return self.generate_entry_signal(snapshot, true);
            }
            return (
                StrategyState::WaitingForConfirmation {
                    signal_tick: self.tick_counter,
                    strength: snapshot.rsi / 100.0,
                    is_long: true,
                },
                Signal::Hold,
            );
        }

        if short_entry {
            if self.definition.confirmation_ticks == 0 {
                return self.generate_entry_signal(snapshot, false);
            }
            return (
                StrategyState::WaitingForConfirmation {
                    signal_tick: self.tick_counter,
                    strength: 1.0 - snapshot.rsi / 100.0,
                    is_long: false,
                },
                Signal::Hold,
            );
        }

        (StrategyState::Idle, Signal::Hold)
    }

    /// Waiting state: re-check conditions after confirmation period.
    #[allow(clippy::arithmetic_side_effects)] // APPROVED: tick_counter subtraction checked by saturating
    fn evaluate_waiting(
        &self,
        snapshot: &IndicatorSnapshot,
        signal_tick: u32,
        is_long: bool,
    ) -> (StrategyState, Signal) {
        let ticks_elapsed = self.tick_counter.saturating_sub(signal_tick);

        if ticks_elapsed >= self.definition.confirmation_ticks {
            // Re-validate conditions
            let conditions = if is_long {
                &self.definition.entry_long_conditions
            } else {
                &self.definition.entry_short_conditions
            };

            if conditions.iter().all(|cond| cond.evaluate(snapshot)) {
                return self.generate_entry_signal(snapshot, is_long);
            }
            // Conditions no longer met — back to idle
            return (StrategyState::Idle, Signal::Hold);
        }

        // Still waiting
        (
            StrategyState::WaitingForConfirmation {
                signal_tick,
                strength: if is_long {
                    snapshot.rsi / 100.0
                } else {
                    1.0 - snapshot.rsi / 100.0
                },
                is_long,
            },
            Signal::Hold,
        )
    }

    /// In-position state: check exit conditions, stop-loss, target, trailing stop.
    #[allow(clippy::arithmetic_side_effects)] // APPROVED: price arithmetic is bounded finite f64
    #[allow(clippy::too_many_arguments)] // APPROVED: FSM state fields passed individually for zero-allocation Copy semantics
    fn evaluate_in_position(
        &self,
        snapshot: &IndicatorSnapshot,
        entry_price: f64,
        stop_loss: f64,
        target: f64,
        is_long: bool,
        highest_since_entry: f64,
        lowest_since_entry: f64,
    ) -> (StrategyState, Signal) {
        let price = snapshot.last_traded_price;

        // Update tracking extremes
        let new_highest = highest_since_entry.max(price);
        let new_lowest = lowest_since_entry.min(price);

        // Check stop-loss
        if is_long && price <= stop_loss {
            return (
                StrategyState::ExitPending {
                    reason: ExitReason::StopLossHit,
                },
                Signal::Exit {
                    reason: ExitReason::StopLossHit,
                },
            );
        }
        if !is_long && price >= stop_loss {
            return (
                StrategyState::ExitPending {
                    reason: ExitReason::StopLossHit,
                },
                Signal::Exit {
                    reason: ExitReason::StopLossHit,
                },
            );
        }

        // Check target
        if is_long && price >= target {
            return (
                StrategyState::ExitPending {
                    reason: ExitReason::TargetHit,
                },
                Signal::Exit {
                    reason: ExitReason::TargetHit,
                },
            );
        }
        if !is_long && price <= target {
            return (
                StrategyState::ExitPending {
                    reason: ExitReason::TargetHit,
                },
                Signal::Exit {
                    reason: ExitReason::TargetHit,
                },
            );
        }

        // Check trailing stop
        if self.definition.trailing_stop_enabled && snapshot.atr > 0.0 {
            let trail_offset = self.definition.trailing_stop_atr_multiplier * snapshot.atr;
            if is_long {
                let trailing_stop = new_highest - trail_offset;
                if price <= trailing_stop && trailing_stop > stop_loss {
                    return (
                        StrategyState::ExitPending {
                            reason: ExitReason::TrailingStop,
                        },
                        Signal::Exit {
                            reason: ExitReason::TrailingStop,
                        },
                    );
                }
            } else {
                let trailing_stop = new_lowest + trail_offset;
                if price >= trailing_stop && trailing_stop < stop_loss {
                    return (
                        StrategyState::ExitPending {
                            reason: ExitReason::TrailingStop,
                        },
                        Signal::Exit {
                            reason: ExitReason::TrailingStop,
                        },
                    );
                }
            }
        }

        // Check exit conditions (OR logic — any condition triggers exit)
        for cond in &self.definition.exit_conditions {
            if cond.evaluate(snapshot) {
                return (
                    StrategyState::ExitPending {
                        reason: ExitReason::SignalReversal,
                    },
                    Signal::Exit {
                        reason: ExitReason::SignalReversal,
                    },
                );
            }
        }

        // Hold position — update tracking extremes
        (
            StrategyState::InPosition {
                entry_price,
                stop_loss,
                target,
                is_long,
                highest_since_entry: new_highest,
                lowest_since_entry: new_lowest,
            },
            Signal::Hold,
        )
    }

    /// Generates an entry signal with ATR-based stop-loss and target.
    #[allow(clippy::arithmetic_side_effects)] // APPROVED: ATR multiplications are bounded finite f64
    fn generate_entry_signal(
        &self,
        snapshot: &IndicatorSnapshot,
        is_long: bool,
    ) -> (StrategyState, Signal) {
        let price = snapshot.last_traded_price;
        let atr = snapshot.atr;

        let (stop_loss, target) = if is_long {
            (
                price - self.definition.stop_loss_atr_multiplier * atr,
                price + self.definition.target_atr_multiplier * atr,
            )
        } else {
            (
                price + self.definition.stop_loss_atr_multiplier * atr,
                price - self.definition.target_atr_multiplier * atr,
            )
        };

        let signal = if is_long {
            Signal::EnterLong {
                size_fraction: self.definition.position_size_fraction,
                stop_loss,
                target,
            }
        } else {
            Signal::EnterShort {
                size_fraction: self.definition.position_size_fraction,
                stop_loss,
                target,
            }
        };

        let new_state = StrategyState::InPosition {
            entry_price: price,
            stop_loss,
            target,
            is_long,
            highest_since_entry: price,
            lowest_since_entry: price,
        };

        (new_state, signal)
    }

    /// Returns a reference to the strategy definition.
    pub fn definition(&self) -> &StrategyDefinition {
        &self.definition
    }

    /// Resets the FSM state for a given security_id to Idle.
    /// Called by the OMS after a position is fully closed.
    pub fn reset_state(&mut self, security_id: u32) {
        let sid = security_id as usize;
        if sid < self.states.len() {
            self.states[sid] = StrategyState::Idle;
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::strategies::types::{ComparisonOp, Condition, IndicatorField};

    /// Builds a minimal strategy definition with long entry on RSI < threshold.
    fn make_long_only_definition(rsi_threshold: f64) -> StrategyDefinition {
        StrategyDefinition {
            name: "test_long".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Lt,
                threshold: rsi_threshold,
            }],
            entry_short_conditions: vec![],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        }
    }

    fn make_warm_snapshot(security_id: u32, ltp: f64, rsi: f64, atr: f64) -> IndicatorSnapshot {
        IndicatorSnapshot {
            security_id,
            last_traded_price: ltp,
            rsi,
            atr,
            is_warm: true,
            ..Default::default()
        }
    }

    // -----------------------------------------------------------------------
    // Out-of-bounds security_id returns Hold
    // -----------------------------------------------------------------------

    #[test]
    fn test_out_of_bounds_security_id_returns_hold() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 10); // max_security_id = 10

        // security_id=100 is way beyond the pre-allocated state array
        let snap = make_warm_snapshot(100, 250.0, 25.0, 5.0);
        let signal = instance.evaluate(&snap);
        assert_eq!(signal, Signal::Hold, "OOB security_id must return Hold");
    }

    #[test]
    fn test_cold_snapshot_returns_hold() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 200);

        // is_warm = false
        let snap = IndicatorSnapshot {
            security_id: 100,
            last_traded_price: 250.0,
            rsi: 25.0,
            atr: 5.0,
            is_warm: false,
            ..Default::default()
        };
        let signal = instance.evaluate(&snap);
        assert_eq!(
            signal,
            Signal::Hold,
            "cold snapshot must return Hold regardless of conditions"
        );
    }

    // -----------------------------------------------------------------------
    // Trailing stop crossing SL edge case
    // -----------------------------------------------------------------------

    #[test]
    fn test_trailing_stop_long_triggers_exit() {
        let mut def = make_long_only_definition(30.0);
        def.trailing_stop_enabled = true;
        def.trailing_stop_atr_multiplier = 1.0;
        def.stop_loss_atr_multiplier = 2.0;
        def.target_atr_multiplier = 5.0;

        let mut instance = StrategyInstance::new(def, 200);

        // Enter long: RSI=25 < 30
        let entry_snap = make_warm_snapshot(100, 100.0, 25.0, 5.0);
        let signal = instance.evaluate(&entry_snap);
        assert!(
            matches!(signal, Signal::EnterLong { .. }),
            "should enter long"
        );

        // Price rises to 120, new high = 120
        let high_snap = make_warm_snapshot(100, 120.0, 50.0, 5.0);
        let signal = instance.evaluate(&high_snap);
        assert_eq!(signal, Signal::Hold, "should hold while price rises");

        // Price drops to 114: trailing_stop = 120 - 1.0*5.0 = 115 > stop_loss (100 - 2*5 = 90)
        // price=114 <= 115 → trailing stop fires
        let drop_snap = make_warm_snapshot(100, 114.0, 50.0, 5.0);
        let signal = instance.evaluate(&drop_snap);
        assert!(
            matches!(
                signal,
                Signal::Exit {
                    reason: ExitReason::TrailingStop
                }
            ),
            "trailing stop must fire when price drops below trail"
        );
    }

    #[test]
    fn test_trailing_stop_not_triggered_when_below_stop_loss() {
        let mut def = make_long_only_definition(30.0);
        def.trailing_stop_enabled = true;
        def.trailing_stop_atr_multiplier = 3.0; // Wide trail
        def.stop_loss_atr_multiplier = 1.0; // Tight SL
        def.target_atr_multiplier = 10.0;

        let mut instance = StrategyInstance::new(def, 200);

        // Enter long at 100 with ATR=5
        // SL = 100 - 1*5 = 95, target = 100 + 10*5 = 150
        let entry_snap = make_warm_snapshot(100, 100.0, 25.0, 5.0);
        let _signal = instance.evaluate(&entry_snap);

        // Price at 101, highest=101, trailing = 101 - 3*5 = 86 < SL(95)
        // So trailing stop condition (trailing_stop > stop_loss) is NOT met.
        // But regular SL hasn't hit either (101 > 95).
        let snap = make_warm_snapshot(100, 101.0, 50.0, 5.0);
        let signal = instance.evaluate(&snap);
        assert_eq!(signal, Signal::Hold, "trailing stop below SL must not fire");
    }

    // -----------------------------------------------------------------------
    // Zero ATR disables trailing
    // -----------------------------------------------------------------------

    #[test]
    fn test_zero_atr_disables_trailing_stop() {
        let mut def = make_long_only_definition(30.0);
        def.trailing_stop_enabled = true;
        def.trailing_stop_atr_multiplier = 1.0;
        def.stop_loss_atr_multiplier = 2.0;
        def.target_atr_multiplier = 5.0;

        let mut instance = StrategyInstance::new(def, 200);

        // Enter long with ATR=5
        let entry_snap = make_warm_snapshot(100, 100.0, 25.0, 5.0);
        let _signal = instance.evaluate(&entry_snap);

        // Now ATR drops to 0 — trailing stop should be disabled
        // Price at 105 (in position, no exit conditions)
        let snap = make_warm_snapshot(100, 105.0, 50.0, 0.0);
        let signal = instance.evaluate(&snap);
        assert_eq!(signal, Signal::Hold, "zero ATR must disable trailing stop");
    }

    // -----------------------------------------------------------------------
    // Dual entry conditions (both long+short true) — long takes priority
    // -----------------------------------------------------------------------

    #[test]
    fn test_dual_entry_both_true_long_takes_priority() {
        let def = StrategyDefinition {
            name: "dual_entry".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Lt,
                threshold: 50.0, // RSI < 50
            }],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::MacdHistogram,
                operator: ComparisonOp::Lt,
                threshold: 0.0, // MACD hist < 0
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // Both conditions true: RSI=40 < 50 AND MACD hist = -1.0 < 0
        let snap = IndicatorSnapshot {
            security_id: 100,
            last_traded_price: 250.0,
            rsi: 40.0,
            macd_histogram: -1.0,
            atr: 5.0,
            is_warm: true,
            ..Default::default()
        };

        let signal = instance.evaluate(&snap);
        // Long is checked first in evaluate_idle, so it should win
        assert!(
            matches!(signal, Signal::EnterLong { .. }),
            "when both long and short conditions are true, long must take priority"
        );
    }

    // -----------------------------------------------------------------------
    // Short entry
    // -----------------------------------------------------------------------

    #[test]
    fn test_short_entry_signal() {
        let def = StrategyDefinition {
            name: "short_only".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.2,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        let snap = make_warm_snapshot(100, 300.0, 80.0, 10.0);
        let signal = instance.evaluate(&snap);

        match signal {
            Signal::EnterShort {
                size_fraction,
                stop_loss,
                target,
            } => {
                assert!((size_fraction - 0.2).abs() < f64::EPSILON);
                // SL = 300 + 2*10 = 320 (short SL is above entry)
                assert!((stop_loss - 320.0).abs() < f64::EPSILON);
                // Target = 300 - 3*10 = 270 (short target is below entry)
                assert!((target - 270.0).abs() < f64::EPSILON);
            }
            other => panic!("expected EnterShort, got {:?}", other),
        }
    }

    // -----------------------------------------------------------------------
    // ExitPending state returns Hold
    // -----------------------------------------------------------------------

    #[test]
    fn test_exit_pending_returns_hold() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 200);

        // Manually set state to ExitPending
        instance.states[100] = StrategyState::ExitPending {
            reason: ExitReason::TargetHit,
        };

        let snap = make_warm_snapshot(100, 250.0, 25.0, 5.0);
        let signal = instance.evaluate(&snap);
        assert_eq!(signal, Signal::Hold, "ExitPending must return Hold");
    }

    // -----------------------------------------------------------------------
    // reset_state sets to Idle
    // -----------------------------------------------------------------------

    #[test]
    fn test_reset_state_sets_idle() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 200);

        instance.states[100] = StrategyState::InPosition {
            entry_price: 250.0,
            stop_loss: 240.0,
            target: 270.0,
            is_long: true,
            highest_since_entry: 260.0,
            lowest_since_entry: 245.0,
        };

        instance.reset_state(100);
        assert_eq!(instance.states[100], StrategyState::Idle);
    }

    #[test]
    fn test_reset_state_out_of_bounds_noop() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 10);
        // Should not panic
        instance.reset_state(999);
    }

    // -----------------------------------------------------------------------
    // Stop loss and target hit
    // -----------------------------------------------------------------------

    #[test]
    fn test_long_stop_loss_hit() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 200);

        // Enter long
        let entry = make_warm_snapshot(100, 100.0, 25.0, 5.0);
        let _signal = instance.evaluate(&entry);

        // Price drops to stop_loss level (100 - 2*5 = 90)
        let sl_snap = make_warm_snapshot(100, 90.0, 50.0, 5.0);
        let signal = instance.evaluate(&sl_snap);
        assert!(
            matches!(
                signal,
                Signal::Exit {
                    reason: ExitReason::StopLossHit
                }
            ),
            "must exit on stop loss hit"
        );
    }

    #[test]
    fn test_long_target_hit() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 200);

        // Enter long at 100 with ATR=5 → target = 100 + 3*5 = 115
        let entry = make_warm_snapshot(100, 100.0, 25.0, 5.0);
        let _signal = instance.evaluate(&entry);

        // Price rises to target
        let target_snap = make_warm_snapshot(100, 115.0, 50.0, 5.0);
        let signal = instance.evaluate(&target_snap);
        assert!(
            matches!(
                signal,
                Signal::Exit {
                    reason: ExitReason::TargetHit
                }
            ),
            "must exit on target hit"
        );
    }

    // -----------------------------------------------------------------------
    // Coverage gap-fill: short position exits, confirmation ticks path,
    // exit conditions (signal reversal), entry signal ATR calculations,
    // definition accessor, trailing stop on short positions
    // -----------------------------------------------------------------------

    #[test]
    fn test_short_stop_loss_hit() {
        let def = StrategyDefinition {
            name: "short_test".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };
        let mut instance = StrategyInstance::new(def, 200);

        // Enter short at 300 with ATR=10 → SL = 300 + 2*10 = 320
        let entry = make_warm_snapshot(100, 300.0, 80.0, 10.0);
        let signal = instance.evaluate(&entry);
        assert!(matches!(signal, Signal::EnterShort { .. }));

        // Price rises to stop loss
        let sl_snap = make_warm_snapshot(100, 320.0, 50.0, 10.0);
        let signal = instance.evaluate(&sl_snap);
        assert!(
            matches!(
                signal,
                Signal::Exit {
                    reason: ExitReason::StopLossHit
                }
            ),
            "short SL must trigger when price rises to stop level"
        );
    }

    #[test]
    fn test_short_target_hit() {
        let def = StrategyDefinition {
            name: "short_target".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };
        let mut instance = StrategyInstance::new(def, 200);

        // Enter short at 300, target = 300 - 3*10 = 270
        let entry = make_warm_snapshot(100, 300.0, 80.0, 10.0);
        let _signal = instance.evaluate(&entry);

        // Price drops to target
        let target_snap = make_warm_snapshot(100, 270.0, 50.0, 10.0);
        let signal = instance.evaluate(&target_snap);
        assert!(
            matches!(
                signal,
                Signal::Exit {
                    reason: ExitReason::TargetHit
                }
            ),
            "short target must trigger when price drops to target"
        );
    }

    #[test]
    fn test_confirmation_ticks_delay_entry() {
        let mut def = make_long_only_definition(30.0);
        def.confirmation_ticks = 3;

        let mut instance = StrategyInstance::new(def, 200);

        // First evaluation: conditions met → WaitingForConfirmation, not entry
        let snap1 = make_warm_snapshot(100, 250.0, 25.0, 5.0);
        let signal = instance.evaluate(&snap1);
        assert_eq!(signal, Signal::Hold, "must wait for confirmation ticks");

        // Second evaluation (still within confirmation period)
        let snap2 = make_warm_snapshot(100, 251.0, 25.0, 5.0);
        let signal = instance.evaluate(&snap2);
        assert_eq!(signal, Signal::Hold, "still waiting");

        // Third evaluation (still within confirmation period)
        let snap3 = make_warm_snapshot(100, 252.0, 25.0, 5.0);
        let signal = instance.evaluate(&snap3);
        assert_eq!(signal, Signal::Hold, "still waiting for 3 ticks");

        // Fourth evaluation: confirmation period elapsed, conditions still true
        let snap4 = make_warm_snapshot(100, 253.0, 25.0, 5.0);
        let signal = instance.evaluate(&snap4);
        assert!(
            matches!(signal, Signal::EnterLong { .. }),
            "must enter after confirmation ticks elapsed"
        );
    }

    #[test]
    fn test_confirmation_ticks_conditions_no_longer_met_returns_idle() {
        let mut def = make_long_only_definition(30.0);
        def.confirmation_ticks = 2;

        let mut instance = StrategyInstance::new(def, 200);

        // First: conditions met → WaitingForConfirmation
        let snap1 = make_warm_snapshot(100, 250.0, 25.0, 5.0);
        let _signal = instance.evaluate(&snap1);

        // Second tick: still waiting
        let snap2 = make_warm_snapshot(100, 251.0, 25.0, 5.0);
        let _signal = instance.evaluate(&snap2);

        // Third tick: confirmation elapsed, but RSI now above threshold
        let snap3 = make_warm_snapshot(100, 252.0, 50.0, 5.0); // RSI=50 > 30
        let signal = instance.evaluate(&snap3);
        assert_eq!(
            signal,
            Signal::Hold,
            "must return to idle when conditions no longer met"
        );
    }

    #[test]
    fn test_exit_condition_triggers_signal_reversal() {
        let def = StrategyDefinition {
            name: "with_exit".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Lt,
                threshold: 30.0,
            }],
            entry_short_conditions: vec![],
            exit_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 10.0, // Wide target to not hit it
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // Enter long
        let entry = make_warm_snapshot(100, 100.0, 25.0, 5.0);
        let signal = instance.evaluate(&entry);
        assert!(matches!(signal, Signal::EnterLong { .. }));

        // Price between SL and target, but RSI crosses exit threshold
        let exit_snap = make_warm_snapshot(100, 105.0, 75.0, 5.0); // RSI=75 > 70
        let signal = instance.evaluate(&exit_snap);
        assert!(
            matches!(
                signal,
                Signal::Exit {
                    reason: ExitReason::SignalReversal
                }
            ),
            "exit condition must trigger SignalReversal"
        );
    }

    #[test]
    fn test_long_entry_signal_atr_calculations() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 200);

        let snap = make_warm_snapshot(100, 200.0, 25.0, 10.0);
        let signal = instance.evaluate(&snap);

        match signal {
            Signal::EnterLong {
                size_fraction,
                stop_loss,
                target,
            } => {
                // SL = 200 - 2*10 = 180, target = 200 + 3*10 = 230
                assert!((size_fraction - 0.1).abs() < f64::EPSILON);
                assert!(
                    (stop_loss - 180.0).abs() < f64::EPSILON,
                    "SL = price - sl_mult * atr"
                );
                assert!(
                    (target - 230.0).abs() < f64::EPSILON,
                    "target = price + target_mult * atr"
                );
            }
            other => panic!("expected EnterLong, got {:?}", other),
        }
    }

    #[test]
    fn test_definition_accessor() {
        let def = make_long_only_definition(30.0);
        let instance = StrategyInstance::new(def, 200);
        assert_eq!(instance.definition().name, "test_long");
        assert_eq!(instance.definition().security_ids, vec![100]);
    }

    #[test]
    fn test_tick_counter_increments() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 200);

        assert_eq!(instance.tick_counter, 0);
        let snap = make_warm_snapshot(100, 250.0, 50.0, 5.0); // RSI=50 > 30, no entry
        let _ = instance.evaluate(&snap);
        assert_eq!(instance.tick_counter, 1);

        let _ = instance.evaluate(&snap);
        assert_eq!(instance.tick_counter, 2);
    }

    #[test]
    fn test_trailing_stop_short_triggers_exit() {
        let def = StrategyDefinition {
            name: "short_trail".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 5.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: true,
            trailing_stop_atr_multiplier: 1.0,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // Enter short at 300, SL = 320, target = 250
        let entry = make_warm_snapshot(100, 300.0, 80.0, 10.0);
        let signal = instance.evaluate(&entry);
        assert!(matches!(signal, Signal::EnterShort { .. }));

        // Price drops to 280 (new low = 280)
        let low_snap = make_warm_snapshot(100, 280.0, 50.0, 10.0);
        let signal = instance.evaluate(&low_snap);
        assert_eq!(signal, Signal::Hold);

        // Price rises to 291: trailing_stop = 280 + 1.0*10 = 290 < SL(320)
        // price=291 >= 290 AND 290 < 320 → trailing stop fires
        let rise_snap = make_warm_snapshot(100, 291.0, 50.0, 10.0);
        let signal = instance.evaluate(&rise_snap);
        assert!(
            matches!(
                signal,
                Signal::Exit {
                    reason: ExitReason::TrailingStop
                }
            ),
            "short trailing stop must fire when price rises above trail"
        );
    }

    #[test]
    fn test_no_entry_when_empty_conditions() {
        let def = StrategyDefinition {
            name: "no_conditions".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);
        let snap = make_warm_snapshot(100, 250.0, 25.0, 5.0);
        let signal = instance.evaluate(&snap);
        assert_eq!(
            signal,
            Signal::Hold,
            "empty conditions must always return Hold"
        );
    }

    #[test]
    fn test_in_position_updates_highest_lowest() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 200);

        // Enter long at 100
        let entry = make_warm_snapshot(100, 100.0, 25.0, 5.0);
        let _signal = instance.evaluate(&entry);

        // Price goes to 110 (new high)
        let high_snap = make_warm_snapshot(100, 110.0, 50.0, 5.0);
        let _signal = instance.evaluate(&high_snap);

        // Verify state tracks highest/lowest
        match instance.states[100] {
            StrategyState::InPosition {
                highest_since_entry,
                lowest_since_entry,
                ..
            } => {
                assert!((highest_since_entry - 110.0).abs() < f64::EPSILON);
                assert!((lowest_since_entry - 100.0).abs() < f64::EPSILON);
            }
            other => panic!("expected InPosition, got {:?}", other),
        }
    }

    #[test]
    fn test_short_confirmation_ticks() {
        let def = StrategyDefinition {
            name: "short_confirm".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 2,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // First: RSI=80 > 70 → WaitingForConfirmation (short)
        let snap1 = make_warm_snapshot(100, 300.0, 80.0, 10.0);
        let signal = instance.evaluate(&snap1);
        assert_eq!(signal, Signal::Hold, "short must wait for confirmation");

        // Second tick: still waiting
        let snap2 = make_warm_snapshot(100, 301.0, 80.0, 10.0);
        let signal = instance.evaluate(&snap2);
        assert_eq!(signal, Signal::Hold);

        // Third tick: confirmation elapsed, conditions still true
        let snap3 = make_warm_snapshot(100, 302.0, 80.0, 10.0);
        let signal = instance.evaluate(&snap3);
        assert!(
            matches!(signal, Signal::EnterShort { .. }),
            "must enter short after confirmation"
        );
    }

    // -----------------------------------------------------------------------
    // Bounds check: security_id at exact boundary
    // -----------------------------------------------------------------------

    #[test]
    fn test_security_id_at_exact_max_returns_hold() {
        let def = make_long_only_definition(30.0);
        let max_sid = 50;
        let mut instance = StrategyInstance::new(def, max_sid);

        // security_id == max_security_id (states.len() == max_sid, valid indices 0..max_sid-1)
        let snap = make_warm_snapshot(max_sid as u32, 250.0, 25.0, 5.0);
        let signal = instance.evaluate(&snap);
        assert_eq!(
            signal,
            Signal::Hold,
            "security_id == states.len() must return Hold (out of bounds)"
        );
    }

    #[test]
    fn test_security_id_at_max_minus_one_works() {
        let def = make_long_only_definition(30.0);
        let max_sid = 50;
        let mut instance = StrategyInstance::new(def, max_sid);

        // security_id == max_security_id - 1 (last valid index)
        let snap = make_warm_snapshot((max_sid - 1) as u32, 250.0, 25.0, 5.0);
        let signal = instance.evaluate(&snap);
        assert!(
            matches!(signal, Signal::EnterLong { .. }),
            "security_id at max-1 must be valid and evaluate entry"
        );
    }

    // -----------------------------------------------------------------------
    // Trailing stop math at exact crossing point
    // -----------------------------------------------------------------------

    #[test]
    fn test_trailing_stop_long_at_exact_crossing_point() {
        let mut def = make_long_only_definition(30.0);
        def.trailing_stop_enabled = true;
        def.trailing_stop_atr_multiplier = 1.0;
        def.stop_loss_atr_multiplier = 2.0;
        def.target_atr_multiplier = 10.0;

        let mut instance = StrategyInstance::new(def, 200);

        // Enter long at 100 with ATR=5 → SL = 90, target = 150
        let entry_snap = make_warm_snapshot(100, 100.0, 25.0, 5.0);
        let _signal = instance.evaluate(&entry_snap);

        // Price rises to 110, then drops to exactly trailing_stop = 110 - 1*5 = 105
        // trailing_stop (105) > stop_loss (90), so trail is active
        let high_snap = make_warm_snapshot(100, 110.0, 50.0, 5.0);
        let _ = instance.evaluate(&high_snap);

        // Price exactly at trailing_stop: 105 <= 105 → fires
        let exact_snap = make_warm_snapshot(100, 105.0, 50.0, 5.0);
        let signal = instance.evaluate(&exact_snap);
        assert!(
            matches!(
                signal,
                Signal::Exit {
                    reason: ExitReason::TrailingStop
                }
            ),
            "trailing stop must fire when price == trailing_stop level"
        );
    }

    #[test]
    fn test_trailing_stop_short_at_exact_crossing_point() {
        let def = StrategyDefinition {
            name: "short_trail_exact".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 10.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: true,
            trailing_stop_atr_multiplier: 1.0,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // Enter short at 300, SL = 320, target = 200
        let entry = make_warm_snapshot(100, 300.0, 80.0, 10.0);
        let _signal = instance.evaluate(&entry);

        // Price drops to 280 (lowest = 280)
        let low_snap = make_warm_snapshot(100, 280.0, 50.0, 10.0);
        let _ = instance.evaluate(&low_snap);

        // Trailing stop for short = lowest + trail_mult * ATR = 280 + 1*10 = 290
        // Price exactly at 290 → 290 >= 290 AND 290 < 320 → fires
        let exact_snap = make_warm_snapshot(100, 290.0, 50.0, 10.0);
        let signal = instance.evaluate(&exact_snap);
        assert!(
            matches!(
                signal,
                Signal::Exit {
                    reason: ExitReason::TrailingStop
                }
            ),
            "short trailing stop must fire when price == trailing_stop level"
        );
    }

    // -----------------------------------------------------------------------
    // Zero ATR: entry signal with zero ATR collapses SL/target to entry price
    // -----------------------------------------------------------------------

    #[test]
    fn test_zero_atr_entry_collapses_sl_and_target_to_price() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 200);

        // Enter long with ATR=0 → SL = price - 2*0 = price, target = price + 3*0 = price
        let snap = make_warm_snapshot(100, 200.0, 25.0, 0.0);
        let signal = instance.evaluate(&snap);
        match signal {
            Signal::EnterLong {
                stop_loss, target, ..
            } => {
                assert!(
                    (stop_loss - 200.0).abs() < f64::EPSILON,
                    "zero ATR → SL collapses to entry price"
                );
                assert!(
                    (target - 200.0).abs() < f64::EPSILON,
                    "zero ATR → target collapses to entry price"
                );
            }
            other => panic!("expected EnterLong, got {:?}", other),
        }
    }

    // -----------------------------------------------------------------------
    // Dual entry conditions: both long AND short conditions satisfied
    // -----------------------------------------------------------------------

    #[test]
    fn test_dual_entry_only_long_conditions_met() {
        let def = StrategyDefinition {
            name: "dual_long_only".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Lt,
                threshold: 50.0,
            }],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 80.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // RSI=40: long cond met (40 < 50), short cond NOT met (40 < 80, not >80)
        let snap = make_warm_snapshot(100, 250.0, 40.0, 5.0);
        let signal = instance.evaluate(&snap);
        assert!(
            matches!(signal, Signal::EnterLong { .. }),
            "only long condition met → must enter long"
        );
    }

    #[test]
    fn test_dual_entry_only_short_conditions_met() {
        let def = StrategyDefinition {
            name: "dual_short_only".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Lt,
                threshold: 20.0,
            }],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // RSI=80: long cond NOT met (80 > 20), short cond met (80 > 70)
        let snap = make_warm_snapshot(100, 250.0, 80.0, 5.0);
        let signal = instance.evaluate(&snap);
        assert!(
            matches!(signal, Signal::EnterShort { .. }),
            "only short condition met → must enter short"
        );
    }

    // -----------------------------------------------------------------------
    // Additional FSM edge cases for coverage
    // -----------------------------------------------------------------------

    #[test]
    fn test_security_id_zero_evaluates_correctly() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 200);

        // security_id=0 is a valid index (first element of Vec)
        let snap = make_warm_snapshot(0, 250.0, 25.0, 5.0);
        let signal = instance.evaluate(&snap);
        assert!(
            matches!(signal, Signal::EnterLong { .. }),
            "security_id=0 must evaluate normally"
        );
    }

    #[test]
    fn test_tick_counter_saturates_at_u32_max() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 200);
        instance.tick_counter = u32::MAX - 1;

        let snap = make_warm_snapshot(100, 250.0, 50.0, 5.0); // RSI=50 > 30 → no entry
        let _ = instance.evaluate(&snap);
        assert_eq!(instance.tick_counter, u32::MAX);

        // Another tick: must saturate, not overflow
        let _ = instance.evaluate(&snap);
        assert_eq!(
            instance.tick_counter,
            u32::MAX,
            "tick_counter must saturate at u32::MAX"
        );
    }

    #[test]
    fn test_zero_atr_short_entry_collapses_sl_and_target() {
        let def = StrategyDefinition {
            name: "short_zero_atr".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // Enter short with ATR=0 → SL = price + 2*0 = price, target = price - 3*0 = price
        let snap = make_warm_snapshot(100, 300.0, 80.0, 0.0);
        let signal = instance.evaluate(&snap);
        match signal {
            Signal::EnterShort {
                stop_loss, target, ..
            } => {
                assert!(
                    (stop_loss - 300.0).abs() < f64::EPSILON,
                    "zero ATR short → SL collapses to entry price"
                );
                assert!(
                    (target - 300.0).abs() < f64::EPSILON,
                    "zero ATR short → target collapses to entry price"
                );
            }
            other => panic!("expected EnterShort, got {:?}", other),
        }
    }

    #[test]
    fn test_short_confirmation_conditions_not_met_returns_idle() {
        let def = StrategyDefinition {
            name: "short_confirm_fail".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 2,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // RSI=80 > 70 → WaitingForConfirmation (short)
        let snap1 = make_warm_snapshot(100, 300.0, 80.0, 10.0);
        let signal = instance.evaluate(&snap1);
        assert_eq!(signal, Signal::Hold);

        // Wait one more tick
        let snap2 = make_warm_snapshot(100, 301.0, 80.0, 10.0);
        let _ = instance.evaluate(&snap2);

        // Confirmation elapsed but conditions no longer met (RSI=50 < 70)
        let snap3 = make_warm_snapshot(100, 302.0, 50.0, 10.0);
        let signal = instance.evaluate(&snap3);
        assert_eq!(
            signal,
            Signal::Hold,
            "short confirmation must revert to idle when conditions fail"
        );
    }

    #[test]
    fn test_long_entry_multiple_and_conditions() {
        // Two entry_long conditions, both must be true (AND logic)
        let def = StrategyDefinition {
            name: "multi_and".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![
                Condition {
                    field: IndicatorField::Rsi,
                    operator: ComparisonOp::Lt,
                    threshold: 30.0,
                },
                Condition {
                    field: IndicatorField::MacdHistogram,
                    operator: ComparisonOp::Gt,
                    threshold: 0.0,
                },
            ],
            entry_short_conditions: vec![],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // RSI=25 < 30 (true), MACD hist = -1.0 < 0 (false) → no entry
        let snap_partial = IndicatorSnapshot {
            security_id: 100,
            last_traded_price: 250.0,
            rsi: 25.0,
            macd_histogram: -1.0,
            atr: 5.0,
            is_warm: true,
            ..Default::default()
        };
        let signal = instance.evaluate(&snap_partial);
        assert_eq!(
            signal,
            Signal::Hold,
            "AND logic: one false condition must prevent entry"
        );

        // Both true: RSI=25 < 30 AND MACD hist = 1.0 > 0
        let snap_both = IndicatorSnapshot {
            security_id: 100,
            last_traded_price: 250.0,
            rsi: 25.0,
            macd_histogram: 1.0,
            atr: 5.0,
            is_warm: true,
            ..Default::default()
        };
        let signal = instance.evaluate(&snap_both);
        assert!(
            matches!(signal, Signal::EnterLong { .. }),
            "AND logic: both conditions true must enter"
        );
    }

    #[test]
    fn test_dual_entry_neither_conditions_met() {
        let def = StrategyDefinition {
            name: "dual_neither".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Lt,
                threshold: 20.0,
            }],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 80.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // RSI=50: neither condition met
        let snap = make_warm_snapshot(100, 250.0, 50.0, 5.0);
        let signal = instance.evaluate(&snap);
        assert_eq!(signal, Signal::Hold, "neither condition met → must hold");
    }

    // -----------------------------------------------------------------------
    // Coverage: short entry with confirmation ticks
    // -----------------------------------------------------------------------

    #[test]
    fn test_short_entry_with_confirmation_ticks() {
        let def = StrategyDefinition {
            name: "short_confirm".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 2,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // First tick: RSI=80 > 70 → enters WaitingForConfirmation (short)
        let snap1 = make_warm_snapshot(100, 250.0, 80.0, 5.0);
        let signal = instance.evaluate(&snap1);
        assert_eq!(signal, Signal::Hold, "should wait for confirmation");

        // Second tick: still waiting (1 tick elapsed, need 2)
        let snap2 = make_warm_snapshot(100, 251.0, 82.0, 5.0);
        let signal = instance.evaluate(&snap2);
        assert_eq!(signal, Signal::Hold, "still waiting for confirmation");

        // Third tick: 2 ticks elapsed, conditions re-validated → enter short
        let snap3 = make_warm_snapshot(100, 252.0, 81.0, 5.0);
        let signal = instance.evaluate(&snap3);
        assert!(
            matches!(signal, Signal::EnterShort { .. }),
            "should enter short after confirmation: got {signal:?}"
        );
    }

    // -----------------------------------------------------------------------
    // Coverage: short in-position stop-loss hit (with entry conditions)
    // -----------------------------------------------------------------------

    #[test]
    fn test_short_stop_loss_hit_via_rsi_entry() {
        let def = StrategyDefinition {
            name: "short_sl".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // Enter short: RSI=80 > 70, price=100, ATR=5
        // SL = 100 + 2*5 = 110, target = 100 - 3*5 = 85
        let entry = make_warm_snapshot(100, 100.0, 80.0, 5.0);
        let signal = instance.evaluate(&entry);
        assert!(matches!(signal, Signal::EnterShort { .. }));

        // Price rises to 110 (hits SL)
        let sl_snap = make_warm_snapshot(100, 110.0, 60.0, 5.0);
        let signal = instance.evaluate(&sl_snap);
        assert!(
            matches!(
                signal,
                Signal::Exit {
                    reason: ExitReason::StopLossHit
                }
            ),
            "short SL must fire: got {signal:?}"
        );
    }

    // -----------------------------------------------------------------------
    // Coverage: short target hit (with explicit ATR-based target)
    // -----------------------------------------------------------------------

    #[test]
    fn test_short_target_hit_via_rsi_entry() {
        let def = StrategyDefinition {
            name: "short_target".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // Enter short: price=100, ATR=5 → target=85
        let entry = make_warm_snapshot(100, 100.0, 80.0, 5.0);
        let signal = instance.evaluate(&entry);
        assert!(matches!(signal, Signal::EnterShort { .. }));

        // Price drops to 85 (hits target)
        let target_snap = make_warm_snapshot(100, 85.0, 50.0, 5.0);
        let signal = instance.evaluate(&target_snap);
        assert!(
            matches!(
                signal,
                Signal::Exit {
                    reason: ExitReason::TargetHit
                }
            ),
            "short target must fire: got {signal:?}"
        );
    }

    // -----------------------------------------------------------------------
    // Coverage: short trailing stop
    // -----------------------------------------------------------------------

    #[test]
    fn test_short_trailing_stop_triggers_exit() {
        let def = StrategyDefinition {
            name: "short_trail".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 5.0,
            target_atr_multiplier: 10.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: true,
            trailing_stop_atr_multiplier: 1.0,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // Enter short: price=200, ATR=5, SL=225, target=150
        let entry = make_warm_snapshot(100, 200.0, 80.0, 5.0);
        let signal = instance.evaluate(&entry);
        assert!(matches!(signal, Signal::EnterShort { .. }));

        // Price drops to 180, new lowest = 180
        let low_snap = make_warm_snapshot(100, 180.0, 60.0, 5.0);
        let signal = instance.evaluate(&low_snap);
        assert_eq!(signal, Signal::Hold, "should hold while price drops");

        // Price rises to 186: trailing = 180 + 1.0*5 = 185 < SL(225)
        // price=186 >= 185 AND 185 < 225 → trailing stop fires
        let trail_snap = make_warm_snapshot(100, 186.0, 60.0, 5.0);
        let signal = instance.evaluate(&trail_snap);
        assert!(
            matches!(
                signal,
                Signal::Exit {
                    reason: ExitReason::TrailingStop
                }
            ),
            "short trailing stop must fire: got {signal:?}"
        );
    }

    // -----------------------------------------------------------------------
    // Coverage: signal reversal exit (OR logic on exit conditions)
    // -----------------------------------------------------------------------

    #[test]
    fn test_exit_condition_or_logic_triggers_signal_reversal() {
        let def = StrategyDefinition {
            name: "with_exit".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Lt,
                threshold: 30.0,
            }],
            entry_short_conditions: vec![],
            exit_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 60.0,
            }],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 10.0, // high target so it won't hit
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // Enter long: RSI=25 < 30
        let entry = make_warm_snapshot(100, 100.0, 25.0, 5.0);
        let signal = instance.evaluate(&entry);
        assert!(matches!(signal, Signal::EnterLong { .. }));

        // RSI rises to 65 > 60 → exit condition triggers
        let exit_snap = make_warm_snapshot(100, 105.0, 65.0, 5.0);
        let signal = instance.evaluate(&exit_snap);
        assert!(
            matches!(
                signal,
                Signal::Exit {
                    reason: ExitReason::SignalReversal
                }
            ),
            "exit condition must trigger SignalReversal: got {signal:?}"
        );
    }

    // -----------------------------------------------------------------------
    // Coverage: ExitPending state stays in ExitPending
    // -----------------------------------------------------------------------

    #[test]
    fn test_exit_pending_stays_pending() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 200);

        // Enter long
        let entry = make_warm_snapshot(100, 100.0, 25.0, 5.0);
        let _signal = instance.evaluate(&entry);

        // Force SL hit to enter ExitPending
        // SL = 100 - 2*5 = 90, price drops to 89
        let sl_snap = make_warm_snapshot(100, 89.0, 25.0, 5.0);
        let signal = instance.evaluate(&sl_snap);
        assert!(matches!(
            signal,
            Signal::Exit {
                reason: ExitReason::StopLossHit
            }
        ));

        // Next evaluate should return Hold (ExitPending stays)
        let next = make_warm_snapshot(100, 88.0, 25.0, 5.0);
        let signal = instance.evaluate(&next);
        assert_eq!(signal, Signal::Hold, "ExitPending must return Hold");
    }

    // -----------------------------------------------------------------------
    // Coverage: reset_state returns to Idle
    // -----------------------------------------------------------------------

    #[test]
    fn test_reset_state_returns_to_idle() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 200);

        // Enter long
        let entry = make_warm_snapshot(100, 100.0, 25.0, 5.0);
        let _signal = instance.evaluate(&entry);

        // Reset state
        instance.reset_state(100);

        // Should be idle again — same conditions should trigger new entry
        let snap = make_warm_snapshot(100, 100.0, 25.0, 5.0);
        let signal = instance.evaluate(&snap);
        assert!(
            matches!(signal, Signal::EnterLong { .. }),
            "after reset, should re-enter"
        );
    }

    // -----------------------------------------------------------------------
    // Coverage: reset_state with out-of-bounds security_id (no-op)
    // -----------------------------------------------------------------------

    #[test]
    fn test_reset_state_oob_security_id_is_noop() {
        let def = make_long_only_definition(30.0);
        let mut instance = StrategyInstance::new(def, 10);
        // No panic, just a no-op
        instance.reset_state(9999);
    }

    // -----------------------------------------------------------------------
    // Coverage: confirmation period conditions no longer met → back to Idle
    // -----------------------------------------------------------------------

    #[test]
    fn test_confirmation_conditions_no_longer_met_returns_to_idle() {
        let def = StrategyDefinition {
            name: "confirm_fail".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Lt,
                threshold: 30.0,
            }],
            entry_short_conditions: vec![],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 1,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // First tick: RSI=25 < 30 → WaitingForConfirmation
        let snap1 = make_warm_snapshot(100, 250.0, 25.0, 5.0);
        let signal = instance.evaluate(&snap1);
        assert_eq!(signal, Signal::Hold);

        // Second tick: RSI=50 > 30 → conditions no longer met → back to Idle
        let snap2 = make_warm_snapshot(100, 250.0, 50.0, 5.0);
        let signal = instance.evaluate(&snap2);
        assert_eq!(signal, Signal::Hold, "conditions failed → back to Idle");
    }

    // -----------------------------------------------------------------------
    // Coverage: definition() accessor returns correct strategy name
    // -----------------------------------------------------------------------

    #[test]
    fn test_definition_accessor_returns_name() {
        let def = make_long_only_definition(30.0);
        let instance = StrategyInstance::new(def, 200);
        assert_eq!(instance.definition().name, "test_long");
        assert_eq!(instance.definition().security_ids, vec![100]);
    }

    // -----------------------------------------------------------------------
    // Coverage: short confirmation "still waiting" branch strength calculation
    // -----------------------------------------------------------------------

    #[test]
    fn test_short_confirmation_still_waiting_updates_strength() {
        let def = StrategyDefinition {
            name: "short_wait_strength".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 3, // Need 3 ticks
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // First tick: RSI=80 > 70 -> WaitingForConfirmation (short)
        let snap1 = make_warm_snapshot(100, 300.0, 80.0, 10.0);
        let signal = instance.evaluate(&snap1);
        assert_eq!(signal, Signal::Hold);

        // Second tick: still within confirmation period (need 3 ticks)
        // RSI=85 -> strength = 1.0 - 85/100 = 0.15
        let snap2 = make_warm_snapshot(100, 301.0, 85.0, 10.0);
        let signal = instance.evaluate(&snap2);
        assert_eq!(signal, Signal::Hold, "still waiting for short confirmation");

        // Verify the state is WaitingForConfirmation with is_long=false
        match instance.states[100] {
            StrategyState::WaitingForConfirmation { is_long, .. } => {
                assert!(!is_long, "must be short confirmation");
            }
            other => panic!("expected WaitingForConfirmation, got {:?}", other),
        }
    }

    // -----------------------------------------------------------------------
    // Coverage: long confirmation "still waiting" updates strength
    // -----------------------------------------------------------------------

    #[test]
    fn test_long_confirmation_still_waiting_updates_strength() {
        let mut def = make_long_only_definition(30.0);
        def.confirmation_ticks = 3;

        let mut instance = StrategyInstance::new(def, 200);

        // First tick: RSI=25 < 30 -> WaitingForConfirmation (long)
        let snap1 = make_warm_snapshot(100, 250.0, 25.0, 5.0);
        let signal = instance.evaluate(&snap1);
        assert_eq!(signal, Signal::Hold);

        // Second tick: still waiting, strength = RSI/100 = 20/100 = 0.2
        let snap2 = make_warm_snapshot(100, 251.0, 20.0, 5.0);
        let signal = instance.evaluate(&snap2);
        assert_eq!(signal, Signal::Hold, "still waiting for long confirmation");

        match instance.states[100] {
            StrategyState::WaitingForConfirmation {
                is_long, strength, ..
            } => {
                assert!(is_long, "must be long confirmation");
                assert!(
                    (strength - 0.2).abs() < f64::EPSILON,
                    "strength = RSI/100 = 0.2"
                );
            }
            other => panic!("expected WaitingForConfirmation, got {:?}", other),
        }
    }

    // -----------------------------------------------------------------------
    // Coverage: short confirmation fails when conditions no longer met
    // -----------------------------------------------------------------------

    #[test]
    fn test_short_confirmation_conditions_no_longer_met() {
        let def = StrategyDefinition {
            name: "short_confirm_fail".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![],
            entry_short_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Gt,
                threshold: 70.0,
            }],
            exit_conditions: vec![],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 3.0,
            confirmation_ticks: 2,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // First: RSI=80 > 70 -> WaitingForConfirmation
        let snap1 = make_warm_snapshot(100, 300.0, 80.0, 10.0);
        let _signal = instance.evaluate(&snap1);

        // Second: still waiting
        let snap2 = make_warm_snapshot(100, 301.0, 80.0, 10.0);
        let _signal = instance.evaluate(&snap2);

        // Third: confirmation elapsed but RSI dropped to 60 < 70
        let snap3 = make_warm_snapshot(100, 302.0, 60.0, 10.0);
        let signal = instance.evaluate(&snap3);
        assert_eq!(
            signal,
            Signal::Hold,
            "short conditions no longer met -> back to Idle"
        );

        // State should be Idle
        assert_eq!(instance.states[100], StrategyState::Idle);
    }

    // -----------------------------------------------------------------------
    // Coverage: multiple exit conditions — first match triggers
    // -----------------------------------------------------------------------

    #[test]
    fn test_multiple_exit_conditions_first_match_triggers() {
        let def = StrategyDefinition {
            name: "multi_exit".to_owned(),
            security_ids: vec![100],
            entry_long_conditions: vec![Condition {
                field: IndicatorField::Rsi,
                operator: ComparisonOp::Lt,
                threshold: 30.0,
            }],
            entry_short_conditions: vec![],
            exit_conditions: vec![
                Condition {
                    field: IndicatorField::Rsi,
                    operator: ComparisonOp::Gt,
                    threshold: 90.0, // Won't match
                },
                Condition {
                    field: IndicatorField::Rsi,
                    operator: ComparisonOp::Gt,
                    threshold: 60.0, // Will match
                },
            ],
            position_size_fraction: 0.1,
            stop_loss_atr_multiplier: 2.0,
            target_atr_multiplier: 10.0,
            confirmation_ticks: 0,
            trailing_stop_enabled: false,
            trailing_stop_atr_multiplier: 1.5,
        };

        let mut instance = StrategyInstance::new(def, 200);

        // Enter long
        let entry = make_warm_snapshot(100, 100.0, 25.0, 5.0);
        let _signal = instance.evaluate(&entry);

        // RSI=65 > 60 but < 90 — second exit condition triggers
        let exit_snap = make_warm_snapshot(100, 105.0, 65.0, 5.0);
        let signal = instance.evaluate(&exit_snap);
        assert!(
            matches!(
                signal,
                Signal::Exit {
                    reason: ExitReason::SignalReversal
                }
            ),
            "second exit condition should trigger SignalReversal"
        );
    }
}
