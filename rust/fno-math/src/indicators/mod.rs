//! Technical indicators — moving averages, oscillators, volatility,
//! trend, volume, and tick-level primitives like OBI.
//!
//! **Status:** scaffold only. Tickvault's current
//! `crates/trading/src/indicator/` only contains OBI (order book
//! imbalance, tick-level). The full indicator catalog will be added
//! to this crate during the extraction refactor and both products
//! pick it up on the next version bump.
//!
//! Planned modules (catalog from BruteX FR-IND-001):
//!
//! - **Moving averages:** `sma`, `ema`, `wma`, `hma`, `dema`, `tema`
//! - **Oscillators:** `rsi`, `stoch`, `macd`, `cci`, `williams_r`
//! - **Volatility:** `bollinger_bands`, `atr`, `keltner`, `donchian`,
//!   `realized_volatility`
//! - **Trend:** `adx`, `supertrend`, `psar`, `ichimoku`
//! - **Volume / order-flow:** `vwap`, `anchored_vwap`, `obv`, `cmf`,
//!   `mfi`, `obi` (from tickvault)
//! - **Price structure:** `pivot_points`, `cpr`, `opening_range`

// TODO(extraction): populate during one-shot refactor
// pub mod sma;
// pub mod ema;
// pub mod wma;
// pub mod rsi;
// pub mod macd;
// pub mod bollinger_bands;
// pub mod atr;
// pub mod adx;
// pub mod supertrend;
// pub mod vwap;
// pub mod obi;
// ... full catalog per FR-IND-001
