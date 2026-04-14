//! Options Greeks — Black-Scholes, calibration, aggregation, PCR.
//!
//! **Status:** scaffold only. The actual implementations live in
//! tickvault's `crates/trading/src/greeks/` and are copied into this
//! directory by `scripts/extract-fno-math.sh`. After extraction, this
//! `mod.rs` re-exports the concrete modules.
//!
//! Until extraction runs, all functions are placeholder `todo!()`s
//! so the crate compiles but fails loudly if called.
//!
//! Planned modules (to be populated by extraction):
//!
//! - `black_scholes` — delta, gamma, theta, vega, rho, option price
//! - `calibration` — implied volatility via Newton-Raphson and bisection
//! - `pcr` — put-call ratio aggregator
//! - `aggregator` — portfolio-level Greeks rollup
//! - `buildup` — OI buildup analysis
//! - `historical_rates` — risk-free rate curve

// TODO(extraction): populate via `scripts/extract-fno-math.sh`
// pub mod black_scholes;
// pub mod calibration;
// pub mod pcr;
// pub mod aggregator;
// pub mod buildup;
// pub mod historical_rates;
