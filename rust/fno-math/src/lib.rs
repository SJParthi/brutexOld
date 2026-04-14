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

// Lint policy inherited from tickvault — defense in depth.
#![cfg_attr(not(test), deny(clippy::unwrap_used))]
#![cfg_attr(not(test), deny(clippy::expect_used))]
#![deny(clippy::print_stdout)]
#![deny(clippy::print_stderr)]
#![deny(clippy::dbg_macro)]
#![deny(unsafe_code)]
#![allow(missing_docs)] // TODO: re-enable after adding docs to all public items

/// Compatibility shim — vendored copy of `tickvault-common` types
/// needed by the extracted `greeks/` / `indicators/` / `strategies/`
/// modules. See [`compat`] docs for the provenance and end-state plan.
pub mod compat;

/// Options Greeks — Black-Scholes pricing, Jaeckel IV solver, PCR,
/// buildup, aggregator. Extracted from
/// `tickvault/crates/trading/src/greeks/`.
pub mod greeks;

/// O(1) per-tick indicator engine — obi + types + engine. Extracted
/// from `tickvault/crates/trading/src/indicator/`. Bar-level indicators
/// (SMA/EMA/RSI/MACD/…) are not yet present and will be added here in
/// Phase 2 per `MG-043`.
pub mod indicators;

/// Math utilities — placeholder scaffold (not yet extracted; tickvault
/// does not yet expose a dedicated math module — utilities are inlined
/// in greeks/indicator files. Will be refactored here in Phase 2).
pub mod math;

/// FSM-based strategy evaluator — config + evaluator + hot_reload +
/// types + tests. Extracted from
/// `tickvault/crates/trading/src/strategy/`.
pub mod strategies;

/// Crate version (semver). Must be bumped on any breaking change and
/// verified against tickvault's declared version by the cross-repo
/// CI gate.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
