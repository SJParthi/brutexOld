//! Domain types used across the Rust hot kernel.
//!
//! Per DEC-P0-025 (PR #47) the live-trading modules (`order_types`,
//! `tick_types`, `instrument_registry`) were removed; this module
//! now contains only the domain types BruteX itself uses for Track 1
//! (Groww historical backtest, spot indices):
//!
//! - [`config`] — runtime configuration containers
//! - [`constants`] — exchange segment + numeric constants
//! - [`error`] — common error types
//! - [`instrument_types`] — instrument-kind codecs (CAUTION: still
//!   carries Equity/Stock variants from the pre-DEC-P0-025 scaffold;
//!   pruning tracked as a follow-up to DEC-P0-025)
//! - [`sanitize`] — string sanitization for symbol normalization
//! - [`segment`] — ExchangeSegment helpers
//! - [`trading_calendar`] — NSE/BSE holiday calendar
//! - [`types`] — Exchange enum + OptionType enum + segment serde

pub mod config;
pub mod constants;
pub mod error;
pub mod instrument_types;
pub mod sanitize;
pub mod segment;
pub mod trading_calendar;
pub mod types;
