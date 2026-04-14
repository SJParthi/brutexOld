//! # fno-math
//!
//! The single source of truth for **every pure-compute layer shared
//! between tickvault (live F&O trading) and BruteX (brute-force backtest)**.
//!
//! This crate intentionally contains no I/O, no async, no logging, and no
//! allocations on hot paths. Every public function is a deterministic
//! pure function from inputs to outputs.
//!
//! ## Scope (per BruteX REQUIREMENTS.md §7A)
//!
//! - [`greeks`] — Black-Scholes, implied volatility calibration, PCR,
//!   position aggregation
//! - [`indicators`] — SMA / EMA / RSI / MACD / BB / ATR / Supertrend /
//!   VWAP / OBI and every future indicator
//! - [`strategies`] — base `Strategy` trait, signal types, state
//!   transitions, and every future strategy
//! - [`math`] — rolling stats, distributions, solvers, calendar
//!
//! ## Invariant
//!
//! **Any change to code in this crate automatically applies to both
//! tickvault and BruteX on the next version bump.** There is no
//! parallel Python implementation. There is no manual sync. The
//! bytes executing in a live Dhan tick pipeline and in a BruteX
//! backtest combo are byte-identical (modulo the PyO3 FFI boundary,
//! which is a pure forwarder).
//!
//! ## Population status
//!
//! This file is the **scaffold** committed in BruteX's initial
//! shared-math contract commit. The actual source code is populated
//! by running `scripts/extract-fno-math.sh` which copies tickvault's
//! existing `crates/trading/src/greeks/**` and `indicator/**` files
//! into this crate's `src/greeks/` and `src/indicators/` directories.
//!
//! Until extraction runs, all module stubs simply `compile!` as empty
//! modules and contain a single `todo!()` per public function so the
//! crate builds but fails fast at runtime if accidentally called.
//!
//! ## Consumers
//!
//! ### Rust (tickvault)
//! ```toml
//! [dependencies]
//! fno-math = { path = "../brutex/rust/fno-math" }
//! # or once published:
//! fno-math = "0.1"
//! ```
//!
//! ```rust,ignore
//! use fno_math::greeks::black_scholes::{delta, gamma, theta};
//! use fno_math::indicators::rsi::Rsi;
//! use fno_math::strategies::Strategy;
//! ```
//!
//! ### Python (BruteX) via PyO3 wrapper `fno-math-py`
//! ```python
//! from fno_math_py.greeks import delta, gamma, theta
//! from fno_math_py.indicators import rsi
//! from fno_math_py.strategies import Strategy
//! ```

#![deny(unsafe_code)]
#![deny(missing_docs)]
#![warn(clippy::all)]
#![warn(clippy::pedantic)]

pub mod greeks;
pub mod indicators;
pub mod math;
pub mod strategies;

/// Crate version (semver). Must be bumped on any breaking change and
/// verified against tickvault's declared version by the cross-repo
/// CI gate.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
