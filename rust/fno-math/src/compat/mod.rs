//! Compatibility shim — vendored copy of `tickvault-common` for use by
//! the extracted greeks/indicators/strategies modules.
//!
//! This module exists because tickvault's `greeks/`, `indicator/`, and
//! `strategy/` source files reference types and constants from its
//! sibling crate `tickvault-common`. To keep `fno-math` a self-contained
//! Rust crate that can be consumed identically by both tickvault (Rust)
//! and BruteX (PyO3), we vendor those common files here.
//!
//! **Provenance:** copied from `tickvault/crates/common/src/` at commit
//! `aad528a7e7f58df0bda69e349a48e31442115fd7` on 2026-04-14 via
//! `scripts/extract-fno-math.sh`.
//!
//! **End state (Phase 2 polish):** the types in this module will be
//! promoted to first-class `fno-math` types (e.g., `fno_math::tick`
//! instead of `fno_math::compat::tick_types`). At that point tickvault's
//! `tickvault-common` crate is deleted and both products depend only on
//! `fno-math`. Until that polish happens, tickvault keeps its own
//! `tickvault-common` crate and the two stay in sync via the drift-check
//! script.

pub mod config;
pub mod constants;
pub mod error;
pub mod instrument_registry;
pub mod instrument_types;
pub mod order_types;
pub mod sanitize;
pub mod segment;
pub mod tick_types;
pub mod trading_calendar;
pub mod types;
