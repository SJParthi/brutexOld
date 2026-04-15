//! O(1) per-tick indicator engine.
//!
//! Updates all indicator state for a given instrument in constant time.
//! Pre-allocated at startup — zero allocation on the hot path.

pub mod engine;
pub mod obi;
#[cfg(test)]
mod tests;
pub mod types;

pub use engine::IndicatorEngine;
pub use types::{IndicatorParams, IndicatorSnapshot, IndicatorState};
