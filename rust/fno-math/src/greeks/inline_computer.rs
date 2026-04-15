//! Lightweight inline Greeks computer for the tick processing hot path.
//!
//! Computes IV + delta/gamma/theta/vega for every F&O option tick in O(1):
//! 2 HashMap lookups + Jaeckel IV solver (exactly 2 iterations).
//!
//! # Design
//! - **Index/equity ticks**: update the underlying LTP cache (no Greeks).
//! - **F&O option ticks**: lazy-resolve option metadata from the instrument
//!   registry on first tick, then compute Greeks from cached metadata + spot.
//! - **F&O future ticks**: no Greeks (futures have delta=1 by definition).
//!
//! # Performance
//! - O(1) per tick: HashMap lookup + Jaeckel solve + BS Greeks formula.
//! - Zero allocation on hot path: all HashMaps pre-allocated at construction.
//! - Implements `GreeksEnricher` trait (defined in `common::tick_types`)
//!   for injection into `core::pipeline::tick_processor` without circular deps.

use std::collections::HashMap;

use chrono::NaiveDate;
use tracing::warn;

use crate::compat::instrument_registry::InstrumentRegistry;
use crate::compat::tick_types::{GreeksEnricher, ParsedTick};
use crate::compat::types::{ExchangeSegment, OptionType};

use super::black_scholes::{self, OptionSide};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Initial capacity for underlying LTP map (~50-200 underlyings).
const UNDERLYING_LTP_INITIAL_CAPACITY: usize = 256;

/// Initial capacity for option metadata cache (~2-8K F&O instruments).
const OPTION_META_INITIAL_CAPACITY: usize = 8192;

/// Minimum option price for meaningful Greeks computation.
/// Below this, IV solver produces unreliable results.
const MIN_OPTION_PRICE: f64 = 0.01;

// ---------------------------------------------------------------------------
// OptionMeta — pre-resolved option metadata (populated on first tick)
// ---------------------------------------------------------------------------

/// Pre-resolved option metadata for a single F&O contract.
///
/// Populated lazily on the first tick for each `security_id` from the
/// instrument registry. Stored in `option_meta` HashMap for O(1) reuse.
struct OptionMeta {
    /// Strike price from instrument master.
    strike_price: f64,
    /// Call or Put.
    option_side: OptionSide,
    /// Security ID of the underlying's price feed (for LTP lookup).
    underlying_security_id: u32,
    /// Expiry date for time-to-expiry calculation.
    expiry_date: NaiveDate,
}

// ---------------------------------------------------------------------------
// InlineGreeksComputer
// ---------------------------------------------------------------------------

/// Lightweight inline Greeks computer for the tick processing hot path.
///
/// O(1) per tick: 2 HashMap lookups + Jaeckel IV solver (2 iterations).
///
/// Implements `GreeksEnricher` so it can be injected into `tick_processor`
/// in `crates/core` without creating a circular dependency.
pub struct InlineGreeksComputer {
    /// Underlying spot prices: security_id -> f32 LTP.
    underlying_ltp: HashMap<u32, f32>,
    /// Per-option metadata cache: security_id -> OptionMeta.
    option_meta: HashMap<u32, OptionMeta>,
    /// Instrument registry for lazy metadata resolution.
    registry: InstrumentRegistry,
    /// Risk-free rate (from config/calibration).
    rate: f64,
    /// Dividend yield.
    div: f64,
    /// Day count convention (365.0 = calendar days per year).
    day_count: f64,
    /// Today's date (IST) for time-to-expiry calculation.
    today: NaiveDate,
}

impl InlineGreeksComputer {
    /// Creates a new inline Greeks computer.
    ///
    /// # Arguments
    /// * `registry` — Instrument registry for resolving option metadata.
    /// * `rate` — Risk-free rate (annualized, e.g., 0.065 = 6.5%).
    /// * `div` — Dividend yield (annualized, e.g., 0.012 = 1.2%).
    /// * `day_count` — Days per year for theta conversion (365.0).
    /// * `today` — Today's date in IST for time-to-expiry calculation.
    // O(1) EXEMPT: cold path — constructor allocates HashMaps once at startup.
    pub fn new(
        registry: InstrumentRegistry,
        rate: f64,
        div: f64,
        day_count: f64,
        today: NaiveDate,
    ) -> Self {
        Self {
            underlying_ltp: HashMap::with_capacity(UNDERLYING_LTP_INITIAL_CAPACITY),
            option_meta: HashMap::with_capacity(OPTION_META_INITIAL_CAPACITY),
            registry,
            rate,
            div,
            day_count,
            today,
        }
    }

    /// Updates today's date (call at midnight IST for day rollover).
    pub fn set_today(&mut self, today: NaiveDate) {
        self.today = today;
    }

    /// Core logic: compute Greeks for an F&O option tick.
    ///
    /// Mutates `tick.iv`, `tick.delta`, `tick.gamma`, `tick.theta`, `tick.vega`
    /// in place. Leaves them as NAN if computation fails (no spot, expired, etc.).
    fn compute_for_fno(&mut self, tick: &mut ParsedTick) {
        // Lazy populate option metadata from registry on first tick.
        // After first tick, this is a single HashMap lookup — O(1).
        if !self.option_meta.contains_key(&tick.security_id) {
            if let Some(meta) = self.resolve_option_meta(tick.security_id) {
                self.option_meta.insert(tick.security_id, meta);
            } else {
                return; // Cannot resolve — future (not option) or missing data
            }
        }

        // SAFETY: we just inserted or confirmed existence above.
        let meta = match self.option_meta.get(&tick.security_id) {
            Some(m) => m,
            None => return,
        };

        // Get underlying spot price.
        let spot = match self.underlying_ltp.get(&meta.underlying_security_id) {
            Some(&s) if s > 0.0 && s.is_finite() => f64::from(s),
            _ => return, // No spot price yet — first underlying tick hasn't arrived
        };

        let option_price = f64::from(tick.last_traded_price);
        if option_price <= MIN_OPTION_PRICE {
            return; // Too cheap for meaningful Greeks
        }

        // Compute time to expiry in years.
        let days = (meta.expiry_date - self.today).num_days();
        if days <= 0 {
            return; // Expired or expiry day (BS numerically unstable)
        }
        let t = days as f64 / self.day_count;

        // Compute IV + all Greeks via Jaeckel solver + BS formulas.
        // iv_solve: O(1), exactly 2 iterations. compute_greeks: O(1), pure math.
        if let Some(greeks) = black_scholes::compute_greeks(
            meta.option_side,
            spot,
            meta.strike_price,
            t,
            self.rate,
            self.div,
            option_price,
        ) {
            // Only write if IV is valid (finite, positive).
            if greeks.iv.is_finite() && greeks.iv > 0.0 {
                tick.iv = greeks.iv;
                tick.delta = greeks.delta;
                tick.gamma = greeks.gamma;
                tick.theta = greeks.theta;
                tick.vega = greeks.vega;
            }
        }
    }

    /// Resolves option metadata from the instrument registry.
    ///
    /// Returns `None` for futures (no option_type) or instruments
    /// with missing strike/expiry/underlying data.
    fn resolve_option_meta(&self, security_id: u32) -> Option<OptionMeta> {
        let inst = self.registry.get(security_id)?;

        // Must be an option (CE/PE), not a future.
        let option_type = inst.option_type?;
        let strike = inst.strike_price.filter(|&s| s > 0.0)?;
        let expiry = inst.expiry_date?;

        // Resolve underlying's price feed security_id.
        let underlying_sec_id = self
            .registry
            .get_underlying_security_id(&inst.underlying_symbol)?;

        if underlying_sec_id == 0 {
            warn!(
                security_id,
                underlying = %inst.underlying_symbol,
                "underlying security_id is zero — skipping Greeks"
            );
            return None;
        }

        let option_side = match option_type {
            OptionType::Call => OptionSide::Call,
            OptionType::Put => OptionSide::Put,
        };

        Some(OptionMeta {
            strike_price: strike,
            option_side,
            underlying_security_id: underlying_sec_id,
            expiry_date: expiry,
        })
    }
}

impl GreeksEnricher for InlineGreeksComputer {
    /// Enriches a parsed tick with Greeks data.
    ///
    /// - Index/equity ticks: updates underlying LTP cache.
    /// - F&O option ticks: computes IV + delta/gamma/theta/vega.
    /// - All other segments: no-op.
    #[inline]
    fn enrich(&mut self, tick: &mut ParsedTick) {
        let segment = ExchangeSegment::from_byte(tick.exchange_segment_code);

        match segment {
            // Index / equity: update underlying LTP cache, no Greeks.
            Some(
                ExchangeSegment::IdxI | ExchangeSegment::NseEquity | ExchangeSegment::BseEquity,
            ) => {
                if tick.last_traded_price > 0.0 && tick.last_traded_price.is_finite() {
                    self.underlying_ltp
                        .insert(tick.security_id, tick.last_traded_price);
                }
            }
            // F&O: compute Greeks for options.
            Some(ExchangeSegment::NseFno | ExchangeSegment::BseFno) => {
                self.compute_for_fno(tick);
            }
            // All other segments: no Greeks.
            _ => {}
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::compat::instrument_registry::{
        InstrumentRegistry, SubscribedInstrument, SubscriptionCategory,
    };
    use crate::compat::instrument_types::DhanInstrumentKind;
    use crate::compat::types::FeedMode;

    /// Helper: create a basic underlying instrument (index/equity).
    fn make_underlying(
        security_id: u32,
        symbol: &str,
        segment: ExchangeSegment,
    ) -> SubscribedInstrument {
        SubscribedInstrument {
            security_id,
            exchange_segment: segment,
            category: if segment == ExchangeSegment::IdxI {
                SubscriptionCategory::MajorIndexValue
            } else {
                SubscriptionCategory::StockEquity
            },
            display_label: symbol.to_string(),
            underlying_symbol: symbol.to_string(),
            instrument_kind: None,
            expiry_date: None,
            strike_price: None,
            option_type: None,
            feed_mode: FeedMode::Full,
        }
    }

    /// Helper: create a basic option instrument (F&O).
    fn make_option(
        security_id: u32,
        underlying_symbol: &str,
        strike: f64,
        option_type: OptionType,
        expiry: NaiveDate,
    ) -> SubscribedInstrument {
        SubscribedInstrument {
            security_id,
            exchange_segment: ExchangeSegment::NseFno,
            category: SubscriptionCategory::IndexDerivative,
            display_label: format!("{underlying_symbol} {strike} {option_type}"),
            underlying_symbol: underlying_symbol.to_string(),
            instrument_kind: Some(DhanInstrumentKind::OptionIndex),
            expiry_date: Some(expiry),
            strike_price: Some(strike),
            option_type: Some(option_type),
            feed_mode: FeedMode::Full,
        }
    }

    /// Helper: create a basic future instrument (F&O, no option_type).
    fn make_future(
        security_id: u32,
        underlying_symbol: &str,
        expiry: NaiveDate,
    ) -> SubscribedInstrument {
        SubscribedInstrument {
            security_id,
            exchange_segment: ExchangeSegment::NseFno,
            category: SubscriptionCategory::IndexDerivative,
            display_label: format!("{underlying_symbol} FUT"),
            underlying_symbol: underlying_symbol.to_string(),
            instrument_kind: Some(DhanInstrumentKind::FutureIndex),
            expiry_date: Some(expiry),
            strike_price: None,
            option_type: None,
            feed_mode: FeedMode::Full,
        }
    }

    /// Helper: create a tick with the given segment and security_id.
    fn make_tick(security_id: u32, segment_code: u8, ltp: f32) -> ParsedTick {
        ParsedTick {
            security_id,
            exchange_segment_code: segment_code,
            last_traded_price: ltp,
            ..ParsedTick::default()
        }
    }

    fn build_test_registry() -> InstrumentRegistry {
        let nifty = make_underlying(13, "NIFTY", ExchangeSegment::IdxI);
        let ce = make_option(
            50001,
            "NIFTY",
            23000.0,
            OptionType::Call,
            NaiveDate::from_ymd_opt(2026, 4, 30).unwrap(),
        );
        let pe = make_option(
            50002,
            "NIFTY",
            23000.0,
            OptionType::Put,
            NaiveDate::from_ymd_opt(2026, 4, 30).unwrap(),
        );
        let fut = make_future(
            60001,
            "NIFTY",
            NaiveDate::from_ymd_opt(2026, 4, 30).unwrap(),
        );
        InstrumentRegistry::from_instruments(vec![nifty, ce, pe, fut])
    }

    #[test]
    fn test_inline_greeks_underlying_ltp_update() {
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        // IDX_I tick updates underlying LTP cache.
        let mut tick = make_tick(13, 0, 23114.50); // IDX_I = 0
        computer.enrich(&mut tick);
        assert_eq!(computer.underlying_ltp.get(&13), Some(&23114.50_f32));
    }

    #[test]
    fn test_inline_greeks_no_greeks_for_equity() {
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        let mut tick = make_tick(13, 0, 23114.50);
        computer.enrich(&mut tick);
        // Greeks should remain NAN for non-F&O ticks.
        assert!(tick.iv.is_nan());
        assert!(tick.delta.is_nan());
    }

    #[test]
    fn test_inline_greeks_no_greeks_without_spot() {
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        // F&O tick BEFORE any underlying tick → no spot → no Greeks.
        let mut tick = make_tick(50001, 2, 250.0); // NSE_FNO = 2
        computer.enrich(&mut tick);
        assert!(tick.iv.is_nan(), "IV should be NAN without spot price");
    }

    #[test]
    fn test_inline_greeks_computes_for_option() {
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        // First: send underlying tick to populate LTP cache.
        let mut idx_tick = make_tick(13, 0, 23114.50);
        computer.enrich(&mut idx_tick);

        // Then: send option tick → should compute Greeks.
        let mut opt_tick = make_tick(50001, 2, 250.0);
        computer.enrich(&mut opt_tick);

        // IV should be finite and positive.
        assert!(
            opt_tick.iv.is_finite(),
            "IV should be finite, got: {}",
            opt_tick.iv
        );
        assert!(
            opt_tick.iv > 0.0,
            "IV should be positive, got: {}",
            opt_tick.iv
        );

        // Delta for call should be in [0, 1].
        assert!(
            opt_tick.delta >= 0.0 && opt_tick.delta <= 1.0,
            "delta={}",
            opt_tick.delta
        );

        // Gamma should be non-negative.
        assert!(opt_tick.gamma >= 0.0, "gamma={}", opt_tick.gamma);

        // Theta should be negative for long options.
        assert!(opt_tick.theta <= 0.0, "theta={}", opt_tick.theta);

        // Vega should be non-negative.
        assert!(opt_tick.vega >= 0.0, "vega={}", opt_tick.vega);
    }

    #[test]
    fn test_inline_greeks_put_option() {
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        let mut idx_tick = make_tick(13, 0, 23114.50);
        computer.enrich(&mut idx_tick);

        let mut put_tick = make_tick(50002, 2, 200.0);
        computer.enrich(&mut put_tick);

        assert!(
            put_tick.iv.is_finite() && put_tick.iv > 0.0,
            "iv={}",
            put_tick.iv
        );
        // Delta for put should be in [-1, 0].
        assert!(
            put_tick.delta >= -1.0 && put_tick.delta <= 0.0,
            "delta={}",
            put_tick.delta
        );
    }

    #[test]
    fn test_inline_greeks_future_skipped() {
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        let mut idx_tick = make_tick(13, 0, 23114.50);
        computer.enrich(&mut idx_tick);

        // Future tick → no option_type → no Greeks.
        let mut fut_tick = make_tick(60001, 2, 23200.0);
        computer.enrich(&mut fut_tick);
        assert!(fut_tick.iv.is_nan(), "futures should not have Greeks");
    }

    #[test]
    fn test_inline_greeks_expired_option_skipped() {
        let registry = build_test_registry();
        // Set today to AFTER expiry.
        let today = NaiveDate::from_ymd_opt(2026, 5, 1).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        let mut idx_tick = make_tick(13, 0, 23114.50);
        computer.enrich(&mut idx_tick);

        let mut opt_tick = make_tick(50001, 2, 250.0);
        computer.enrich(&mut opt_tick);
        assert!(
            opt_tick.iv.is_nan(),
            "expired option should not have Greeks"
        );
    }

    #[test]
    fn test_inline_greeks_zero_ltp_skipped() {
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        let mut idx_tick = make_tick(13, 0, 23114.50);
        computer.enrich(&mut idx_tick);

        // Zero LTP option → skip.
        let mut opt_tick = make_tick(50001, 2, 0.0);
        computer.enrich(&mut opt_tick);
        assert!(opt_tick.iv.is_nan(), "zero LTP should not produce Greeks");
    }

    #[test]
    fn test_inline_greeks_set_today() {
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        let new_today = NaiveDate::from_ymd_opt(2026, 3, 27).unwrap();
        computer.set_today(new_today);
        assert_eq!(computer.today, new_today);
    }

    #[test]
    fn test_inline_greeks_metadata_caching() {
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        let mut idx_tick = make_tick(13, 0, 23114.50);
        computer.enrich(&mut idx_tick);

        // First option tick: resolves metadata.
        let mut opt_tick = make_tick(50001, 2, 250.0);
        computer.enrich(&mut opt_tick);
        assert!(computer.option_meta.contains_key(&50001));

        // Second option tick: uses cached metadata (no re-resolve).
        let mut opt_tick2 = make_tick(50001, 2, 260.0);
        computer.enrich(&mut opt_tick2);
        assert!(opt_tick2.iv.is_finite() && opt_tick2.iv > 0.0);
    }

    #[test]
    fn test_inline_greeks_unknown_security_id() {
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        // Unknown security_id in F&O segment → no crash, no Greeks.
        let mut tick = make_tick(99999, 2, 100.0);
        computer.enrich(&mut tick);
        assert!(tick.iv.is_nan());
    }

    #[test]
    fn test_inline_greeks_negative_ltp_underlying_ignored() {
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        // Negative LTP for underlying → should not cache.
        let mut tick = make_tick(13, 0, -100.0);
        computer.enrich(&mut tick);
        assert!(!computer.underlying_ltp.contains_key(&13));
    }

    #[test]
    fn test_inline_greeks_nan_ltp_underlying_ignored() {
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        let mut tick = make_tick(13, 0, f32::NAN);
        computer.enrich(&mut tick);
        assert!(!computer.underlying_ltp.contains_key(&13));
    }

    #[test]
    fn test_inline_greeks_bse_fno_supported() {
        // BSE F&O segment (8) should also trigger Greeks computation.
        // We don't have BSE instruments in test registry, but verify no panic.
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        let mut tick = make_tick(99999, 8, 100.0); // BSE_FNO = 8
        computer.enrich(&mut tick);
        // Should not panic; Greeks remain NAN (unknown security_id).
        assert!(tick.iv.is_nan());
    }

    #[test]
    fn test_inline_greeks_bse_equity_updates_ltp() {
        // BSE equity segment (4) should update underlying LTP cache.
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        let mut tick = make_tick(5555, 4, 2500.0); // BSE_EQ = 4
        computer.enrich(&mut tick);
        assert_eq!(computer.underlying_ltp.get(&5555), Some(&2500.0));
    }

    #[test]
    fn test_inline_greeks_unknown_segment_no_op() {
        // Unknown segments (e.g. MCX=5, currency=3) should be no-op.
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);

        let mut tick = make_tick(7777, 5, 5000.0); // MCX_COMM = 5
        computer.enrich(&mut tick);
        assert!(tick.iv.is_nan());
        assert!(!computer.underlying_ltp.contains_key(&7777));

        let mut tick2 = make_tick(8888, 3, 75.0); // NSE_CURRENCY = 3
        computer.enrich(&mut tick2);
        assert!(tick2.iv.is_nan());
    }

    #[test]
    fn test_inline_greeks_set_today_updates_date() {
        let registry = build_test_registry();
        let today = NaiveDate::from_ymd_opt(2026, 3, 26).unwrap();
        let mut computer = InlineGreeksComputer::new(registry, 0.065, 0.012, 365.0, today);
        assert_eq!(computer.today, today);

        let tomorrow = NaiveDate::from_ymd_opt(2026, 3, 27).unwrap();
        computer.set_today(tomorrow);
        assert_eq!(computer.today, tomorrow);
    }
}
