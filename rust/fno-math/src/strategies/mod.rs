//! Trading strategies — the base `Strategy` trait plus concrete
//! strategy implementations shared between live and backtest.
//!
//! **Status:** scaffold only. Both tickvault and BruteX will consume
//! strategies from this module. Live tickvault runs them against real
//! tick streams; BruteX runs them against historical bars during
//! brute-force sweeps. **Same Rust code, zero drift.**
//!
//! This is the module the operator will edit most often from Claude
//! Code / Claude Cowork when adding new strategies. Per
//! REQUIREMENTS.md §7A.1 single-source rule, strategy code lives
//! here and **only** here.
//!
//! Planned structure:
//!
//! ```text
//! strategies/
//! ├── base.rs           — Strategy trait + Signal types + State enum
//! ├── momentum/         — trend-following strategies
//! │   ├── ma_crossover.rs
//! │   ├── donchian_breakout.rs
//! │   └── supertrend.rs
//! ├── mean_revert/      — mean-reversion strategies
//! │   ├── rsi_extremes.rs
//! │   └── bb_bounce.rs
//! ├── options/          — options-specific structures
//! │   ├── iron_condor.rs
//! │   ├── iron_fly.rs
//! │   ├── straddle.rs
//! │   ├── strangle.rs
//! │   ├── call_spread.rs
//! │   ├── put_spread.rs
//! │   ├── covered_call.rs
//! │   ├── calendar.rs
//! │   └── diagonal.rs
//! └── opening_range/    — intraday structure strategies
//!     └── orb.rs
//! ```

// TODO(extraction): populate as strategies are written.
// Initial strategies will come from user-supplied logic, not from
// tickvault (tickvault's strategy crate is live-tick-specific).
// New strategies added via Claude Code → fno-math commits → both
// products pick them up on next version bump.

// pub mod base;
// pub mod momentum;
// pub mod mean_revert;
// pub mod options;
// pub mod opening_range;
