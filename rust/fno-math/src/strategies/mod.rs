//! Strategy evaluation — FSM-based O(1) trade signal generation.
//!
//! Each strategy is a finite state machine. `match` on enum variants
//! compiles to a jump table — O(1) dispatch regardless of state count.

pub mod config;
#[cfg(test)]
mod config_tests;
pub mod evaluator;
pub mod hot_reload;
#[cfg(test)]
mod tests;
pub mod types;

pub use config::{load_strategy_config_file, parse_strategy_config};
pub use evaluator::StrategyInstance;
pub use hot_reload::StrategyHotReloader;
pub use types::{
    ComparisonOp, Condition, ExitReason, IndicatorField, Signal, StrategyDefinition, StrategyState,
};
