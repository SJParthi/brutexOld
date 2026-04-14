//! Candle-aligned Greeks aggregation from live tick data.
//!
//! Continuously updates internal state from every tick (O(1) per tick),
//! then emits Greeks/PCR snapshots aligned to candle close boundaries.
//! This ensures `candle.ts == greeks.ts == pcr.ts` for exact JOINs.
//!
//! # Two-Phase Design
//! - **Phase A (per-tick, O(1))**: Update `underlying_ltp` and `option_state` maps.
//! - **Phase B (on candle close)**: Compute Greeks for all tracked options,
//!   emit snapshots with `ts = candle_ts`.
//!
//! # Performance
//! - O(1) per tick update (HashMap lookup + assignment)
//! - O(N) per candle close (N = active options, typically 2-5K)
//! - No allocation on hot path (pre-allocated maps)

use std::collections::HashMap;

use chrono::NaiveDate;
use tracing::{debug, warn};

use crate::compat::config::GreeksConfig;
use crate::compat::instrument_registry::InstrumentRegistry;
use crate::compat::tick_types::ParsedTick;
use crate::compat::types::{ExchangeSegment, OptionType, SecurityId};

use crate::greeks::black_scholes::{self, OptionGreeks, OptionSide};
use crate::greeks::pcr;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Initial capacity for option state map (~2-5K F&O instruments).
const OPTION_STATE_INITIAL_CAPACITY: usize = 8192;

/// Initial capacity for underlying LTP map (~50-200 underlyings).
const UNDERLYING_LTP_INITIAL_CAPACITY: usize = 256;

/// Minimum time-to-expiry in years to compute Greeks.
/// Below this, BS model is numerically unstable.
const MIN_TIME_TO_EXPIRY_YEARS: f64 = 1.0 / (365.0 * 24.0 * 60.0); // ~1 minute

/// IST offset from UTC in seconds (5h30m).
const IST_OFFSET_SECS: u32 = 19800;

// ---------------------------------------------------------------------------
// CandleCloseEvent — signal from candle aggregator
// ---------------------------------------------------------------------------

/// Signal emitted when a candle completes. Carries the candle boundary timestamp.
#[derive(Debug, Clone, Copy)]
pub struct CandleCloseEvent {
    /// Candle boundary timestamp (IST epoch seconds from exchange clock).
    pub candle_ts: u32,
    /// Candle interval in seconds (e.g., 1 for 1s, 60 for 1m).
    pub interval_secs: u32,
}

// ---------------------------------------------------------------------------
// Internal State Types
// ---------------------------------------------------------------------------

/// Per-option tick state (updated on every F&O tick).
///
/// Stores all data needed for Greeks computation + QuestDB output.
/// `underlying_symbol` is stored here (set once on first tick) to avoid
/// repeated registry lookups and String cloning on the snapshot path.
#[derive(Debug, Clone)]
struct OptionTickState {
    /// Latest option LTP from tick.
    ltp: f32,
    /// Latest OI from tick.
    oi: u32,
    /// Latest volume from tick.
    volume: u32,
    /// Strike price from instrument registry.
    strike_price: f64,
    /// Option side (Call/Put).
    option_side: OptionSide,
    /// Option type (Call/Put) for output.
    option_type: OptionType,
    /// Security ID of the underlying's price feed (for LTP lookup).
    underlying_security_id: SecurityId,
    /// Underlying symbol (e.g., "NIFTY"). Stored here to avoid .clone() on snapshot.
    underlying_symbol: String,
    /// Expiry date for time-to-expiry calculation.
    expiry_date: NaiveDate,
    /// Exchange segment code for QuestDB.
    exchange_segment_code: u8,
}

/// Computed Greeks snapshot for one option, ready for persistence.
#[derive(Debug, Clone)]
pub struct GreeksSnapshot {
    pub security_id: SecurityId,
    pub exchange_segment_code: u8,
    pub underlying_symbol: String,
    pub underlying_security_id: SecurityId,
    pub strike_price: f64,
    pub option_type: OptionType,
    pub expiry_date: NaiveDate,
    pub spot_price: f64,
    pub option_ltp: f64,
    pub oi: u32,
    pub volume: u32,
    pub greeks: OptionGreeks,
    /// Candle boundary timestamp (IST epoch seconds).
    pub candle_ts: u32,
}

/// PCR snapshot for one underlying+expiry, ready for persistence.
#[derive(Debug, Clone)]
pub struct PcrSnapshot {
    pub underlying_symbol: String,
    pub expiry_date: NaiveDate,
    pub pcr_oi: Option<f64>,
    pub pcr_volume: Option<f64>,
    pub total_put_oi: u64,
    pub total_call_oi: u64,
    pub total_put_volume: u64,
    pub total_call_volume: u64,
    pub sentiment: pcr::PcrSentiment,
    /// Candle boundary timestamp (IST epoch seconds).
    pub candle_ts: u32,
}

/// Output from a candle-close snapshot cycle.
#[derive(Debug)]
pub struct GreeksEmission {
    pub greeks_snapshots: Vec<GreeksSnapshot>,
    pub pcr_snapshots: Vec<PcrSnapshot>,
}

// ---------------------------------------------------------------------------
// GreeksAggregator
// ---------------------------------------------------------------------------

/// Candle-aligned Greeks computation engine.
///
/// Phase A: per-tick state updates (O(1)).
/// Phase B: on `CandleCloseEvent`, compute and emit Greeks/PCR snapshots.
pub struct GreeksAggregator {
    /// Per-option state, updated on every F&O tick.
    option_state: HashMap<SecurityId, OptionTickState>,

    /// Underlying spot prices, updated on every IDX_I / NSE_EQ tick.
    underlying_ltp: HashMap<SecurityId, f32>,

    /// Instrument registry for enrichment (shared, immutable).
    registry: InstrumentRegistry,

    /// Greeks configuration (risk-free rate, dividend yield, etc.).
    config: GreeksConfig,

    /// Metrics: ticks processed.
    ticks_processed: u64,

    /// Metrics: Greeks snapshots emitted.
    snapshots_emitted: u64,
}

impl GreeksAggregator {
    /// Creates a new aggregator with pre-allocated capacity.
    // TEST-EXEMPT: constructor, tested indirectly by every test in this module
    pub fn new(registry: InstrumentRegistry, config: GreeksConfig) -> Self {
        Self {
            option_state: HashMap::with_capacity(OPTION_STATE_INITIAL_CAPACITY),
            underlying_ltp: HashMap::with_capacity(UNDERLYING_LTP_INITIAL_CAPACITY),
            registry,
            config,
            ticks_processed: 0,
            snapshots_emitted: 0,
        }
    }

    /// Phase A: Update internal state from a tick. O(1).
    ///
    /// - IDX_I / NSE_EQ ticks → update `underlying_ltp` map
    /// - NSE_FNO ticks → update `option_state` map
    #[inline]
    pub fn update(&mut self, tick: &ParsedTick) {
        self.ticks_processed = self.ticks_processed.saturating_add(1);

        let segment = ExchangeSegment::from_byte(tick.exchange_segment_code);

        match segment {
            // Index or equity tick → update underlying LTP cache
            Some(ExchangeSegment::IdxI | ExchangeSegment::NseEquity) => {
                if tick.last_traded_price > 0.0 && tick.last_traded_price.is_finite() {
                    self.underlying_ltp
                        .insert(tick.security_id, tick.last_traded_price);
                }
            }
            // F&O tick → update option state
            Some(ExchangeSegment::NseFno) => {
                self.update_option_state(tick);
            }
            _ => {}
        }
    }

    /// Phase B: Compute Greeks for all tracked options at a candle boundary.
    ///
    /// Returns `GreeksEmission` with Greeks and PCR snapshots, all stamped
    /// with `candle_ts` from the event.
    pub fn snapshot(&mut self, event: CandleCloseEvent) -> GreeksEmission {
        let option_count = self.option_state.len();
        let mut greeks_snapshots = Vec::with_capacity(option_count);
        // Cold path: snapshot runs once per candle close (~1/sec or ~1/min).
        // Pre-allocate PCR accum for typical 4-8 underlyings.
        let mut pcr_accum: HashMap<(&str, NaiveDate), PcrAccumulator> = HashMap::with_capacity(16);

        let rate = self.config.risk_free_rate;
        let div = self.config.dividend_yield;

        for (&security_id, state) in &self.option_state {
            // Accumulate OI/volume for PCR (ALWAYS — regardless of Greeks success).
            // Uses &str reference to avoid String allocation on cold path.
            let pcr_key = (state.underlying_symbol.as_str(), state.expiry_date);
            let accum = pcr_accum.entry(pcr_key).or_default();
            match state.option_type {
                OptionType::Call => {
                    accum.call_oi = accum.call_oi.saturating_add(state.oi as u64);
                    accum.call_volume = accum.call_volume.saturating_add(state.volume as u64);
                }
                OptionType::Put => {
                    accum.put_oi = accum.put_oi.saturating_add(state.oi as u64);
                    accum.put_volume = accum.put_volume.saturating_add(state.volume as u64);
                }
            }

            // Get underlying spot price (required for Greeks)
            let spot = match self.underlying_ltp.get(&state.underlying_security_id) {
                Some(&ltp) if ltp > 0.0 && ltp.is_finite() => ltp as f64,
                _ => continue, // Skip Greeks: no underlying price yet
            };

            let option_ltp = state.ltp as f64;
            if option_ltp <= 0.01 {
                continue; // Skip Greeks: near-zero option price
            }

            // Time to expiry
            let time_to_expiry = compute_time_to_expiry(state.expiry_date, event.candle_ts);
            if time_to_expiry < MIN_TIME_TO_EXPIRY_YEARS {
                continue; // Skip Greeks: too close to expiry for BS
            }

            // Compute Greeks
            let greeks = match black_scholes::compute_greeks(
                state.option_side,
                spot,
                state.strike_price,
                time_to_expiry,
                rate,
                div,
                option_ltp,
            ) {
                Some(g) => g,
                None => continue, // IV solver didn't converge
            };

            greeks_snapshots.push(GreeksSnapshot {
                security_id,
                exchange_segment_code: state.exchange_segment_code,
                // O(1) EXEMPT: cold path — snapshot runs once per candle close (~1/sec or ~1/min).
                underlying_symbol: state.underlying_symbol.to_owned(), // O(1) EXEMPT: cold path
                underlying_security_id: state.underlying_security_id,
                strike_price: state.strike_price,
                option_type: state.option_type,
                expiry_date: state.expiry_date,
                spot_price: spot,
                option_ltp,
                oi: state.oi,
                volume: state.volume,
                greeks,
                candle_ts: event.candle_ts,
            });
        }

        // Build PCR snapshots from accumulated OI/volume.
        let mut pcr_snapshots = Vec::with_capacity(pcr_accum.len());
        for ((underlying_symbol, expiry_date), accum) in pcr_accum {
            let pcr_oi = pcr::compute_pcr(accum.put_oi, accum.call_oi);
            let pcr_volume = pcr::compute_pcr(accum.put_volume, accum.call_volume);
            let sentiment = match pcr_oi {
                Some(p) => pcr::classify_pcr(p),
                None => pcr::PcrSentiment::Neutral,
            };
            pcr_snapshots.push(PcrSnapshot {
                // O(1) EXEMPT: cold path — one allocation per underlying per candle close.
                underlying_symbol: underlying_symbol.to_owned(), // O(1) EXEMPT: cold path
                expiry_date,
                pcr_oi,
                pcr_volume,
                total_put_oi: accum.put_oi,
                total_call_oi: accum.call_oi,
                total_put_volume: accum.put_volume,
                total_call_volume: accum.call_volume,
                sentiment,
                candle_ts: event.candle_ts,
            });
        }

        let greeks_count = greeks_snapshots.len();
        let pcr_count = pcr_snapshots.len();
        self.snapshots_emitted = self.snapshots_emitted.saturating_add(greeks_count as u64);

        if greeks_count > 0 {
            debug!(
                greeks_count,
                pcr_count,
                candle_ts = event.candle_ts,
                "greeks snapshot emitted at candle boundary"
            );
        }

        GreeksEmission {
            greeks_snapshots,
            pcr_snapshots,
        }
    }

    /// Returns number of ticks processed since startup.
    // TEST-EXEMPT: trivial accessor, tested indirectly via test_metrics_tracking
    pub fn ticks_processed(&self) -> u64 {
        self.ticks_processed
    }

    /// Returns number of Greeks snapshots emitted since startup.
    // TEST-EXEMPT: trivial accessor, tested indirectly via test_metrics_tracking
    pub fn snapshots_emitted(&self) -> u64 {
        self.snapshots_emitted
    }

    /// Returns number of options currently tracked.
    // TEST-EXEMPT: trivial accessor, tested indirectly via test_greeks_aggregator_updates_state_on_tick
    pub fn tracked_options(&self) -> usize {
        self.option_state.len()
    }

    /// Returns number of underlyings with cached LTP.
    // TEST-EXEMPT: trivial accessor, tested indirectly via test_underlying_ltp_cache_update
    pub fn tracked_underlyings(&self) -> usize {
        self.underlying_ltp.len()
    }

    // -----------------------------------------------------------------------
    // Internal helpers
    // -----------------------------------------------------------------------

    /// Updates option_state from an F&O tick.
    fn update_option_state(&mut self, tick: &ParsedTick) {
        if let Some(existing) = self.option_state.get_mut(&tick.security_id) {
            // Fast path: update existing state
            existing.ltp = tick.last_traded_price;
            existing.oi = tick.open_interest;
            existing.volume = tick.volume;
            return;
        }

        // Slow path: first tick for this security_id — enrich from registry
        let inst = match self.registry.get(tick.security_id) {
            Some(i) => i,
            None => return, // Not in registry (unlikely)
        };

        // Must be an option (not a future)
        let option_type = match inst.option_type {
            Some(ot) => ot,
            None => return, // Future, skip
        };

        let strike = match inst.strike_price {
            Some(s) if s > 0.0 => s,
            _ => return, // No valid strike
        };

        let expiry = match inst.expiry_date {
            Some(d) => d,
            None => return, // No expiry
        };

        // Find the underlying's price_feed_security_id
        let underlying_sec_id = match self
            .registry
            .get_underlying_security_id(&inst.underlying_symbol)
        {
            Some(id) => id,
            None => {
                warn!(
                    security_id = tick.security_id,
                    underlying = %inst.underlying_symbol,
                    "underlying security_id not found in registry"
                );
                return;
            }
        };

        let option_side = match option_type {
            OptionType::Call => OptionSide::Call,
            OptionType::Put => OptionSide::Put,
        };

        // O(1) EXEMPT: cold path — first tick per security_id only (once per instrument lifetime).
        let underlying_symbol = inst.underlying_symbol.to_owned(); // O(1) EXEMPT: cold path

        self.option_state.insert(
            tick.security_id,
            OptionTickState {
                ltp: tick.last_traded_price,
                oi: tick.open_interest,
                volume: tick.volume,
                strike_price: strike,
                option_side,
                option_type,
                underlying_security_id: underlying_sec_id,
                underlying_symbol,
                expiry_date: expiry,
                exchange_segment_code: tick.exchange_segment_code,
            },
        );
    }
}

// ---------------------------------------------------------------------------
// PCR Accumulator
// ---------------------------------------------------------------------------

#[derive(Debug, Default)]
struct PcrAccumulator {
    put_oi: u64,
    call_oi: u64,
    put_volume: u64,
    call_volume: u64,
}

// ---------------------------------------------------------------------------
// Time-to-expiry calculation
// ---------------------------------------------------------------------------

/// Computes fractional years to expiry from IST epoch seconds timestamp.
///
/// Uses hour-level precision: expiry at NSE close (15:30 IST) on expiry date.
fn compute_time_to_expiry(expiry_date: NaiveDate, current_ist_epoch_secs: u32) -> f64 {
    // Expiry is at NSE close: 15:30 IST on expiry_date
    // NaiveDate epoch = days since 1970-01-01
    let epoch_origin = NaiveDate::from_ymd_opt(1970, 1, 1).unwrap_or_default();
    let expiry_midnight_utc = expiry_date
        .signed_duration_since(epoch_origin)
        .num_seconds();
    // 15:30 IST = 10:00 UTC → expiry_midnight_utc + 36000 seconds
    let expiry_utc_secs = expiry_midnight_utc + 36000; // 10:00 UTC = 15:30 IST

    // Current time in UTC: IST epoch seconds - IST offset
    let current_utc_secs = (current_ist_epoch_secs as i64).saturating_sub(IST_OFFSET_SECS as i64);

    let remaining_secs = expiry_utc_secs.saturating_sub(current_utc_secs);
    if remaining_secs <= 0 {
        return 0.0;
    }

    remaining_secs as f64 / (365.0 * 24.0 * 3600.0)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::compat::instrument_registry::{SubscribedInstrument, SubscriptionCategory};
    use crate::compat::instrument_types::DhanInstrumentKind;
    use crate::compat::types::FeedMode;

    fn default_config() -> GreeksConfig {
        GreeksConfig {
            enabled: true,
            fetch_interval_secs: 60,
            risk_free_rate: 0.10,
            dividend_yield: 0.0,
            iv_solver_max_iterations: 50,
            iv_solver_tolerance: 1e-8,
            day_count: 365.0,
            rate_mode: "dhan".to_string(),
        }
    }

    fn make_index_instrument(security_id: SecurityId, symbol: &str) -> SubscribedInstrument {
        SubscribedInstrument {
            security_id,
            exchange_segment: ExchangeSegment::IdxI,
            category: SubscriptionCategory::MajorIndexValue,
            display_label: symbol.to_string(),
            underlying_symbol: symbol.to_string(),
            instrument_kind: None,
            expiry_date: None,
            strike_price: None,
            option_type: None,
            feed_mode: FeedMode::Ticker,
        }
    }

    fn make_option_instrument(
        security_id: SecurityId,
        underlying: &str,
        strike: f64,
        opt_type: OptionType,
        expiry: NaiveDate,
    ) -> SubscribedInstrument {
        SubscribedInstrument {
            security_id,
            exchange_segment: ExchangeSegment::NseFno,
            category: SubscriptionCategory::IndexDerivative,
            display_label: format!("{underlying} {strike} {}", opt_type.as_str()),
            underlying_symbol: underlying.to_string(),
            instrument_kind: Some(DhanInstrumentKind::OptionIndex),
            expiry_date: Some(expiry),
            strike_price: Some(strike),
            option_type: Some(opt_type),
            feed_mode: FeedMode::Full,
        }
    }

    fn make_tick(
        security_id: u32,
        segment_code: u8,
        ltp: f32,
        exchange_timestamp: u32,
    ) -> ParsedTick {
        ParsedTick {
            security_id,
            exchange_segment_code: segment_code,
            last_traded_price: ltp,
            last_trade_quantity: 50,
            exchange_timestamp,
            received_at_nanos: 0,
            average_traded_price: ltp,
            volume: 100_000,
            total_sell_quantity: 5000,
            total_buy_quantity: 5000,
            day_open: ltp,
            day_close: ltp,
            day_high: ltp,
            day_low: ltp,
            open_interest: 500_000,
            oi_day_high: 600_000,
            oi_day_low: 400_000,
            iv: f64::NAN,
            delta: f64::NAN,
            gamma: f64::NAN,
            theta: f64::NAN,
            vega: f64::NAN,
        }
    }

    /// OTM strike for realistic BS convergence at our test parameters.
    const TEST_STRIKE: f64 = 25500.0;
    /// Realistic CE premium for 25500 strike with spot~25042, ~37d, IV~10%.
    const TEST_CE_LTP: f32 = 230.0;
    /// Realistic PE premium for 25500 strike with spot~25042, ~37d, IV~10%.
    const TEST_PE_LTP: f32 = 430.0;

    fn build_test_registry() -> InstrumentRegistry {
        let nifty_idx = make_index_instrument(13, "NIFTY");
        let nifty_ce = make_option_instrument(
            50001,
            "NIFTY",
            TEST_STRIKE,
            OptionType::Call,
            NaiveDate::from_ymd_opt(2026, 4, 30).unwrap_or_default(),
        );
        let nifty_pe = make_option_instrument(
            50002,
            "NIFTY",
            TEST_STRIKE,
            OptionType::Put,
            NaiveDate::from_ymd_opt(2026, 4, 30).unwrap_or_default(),
        );
        InstrumentRegistry::from_instruments(vec![nifty_idx, nifty_ce, nifty_pe])
    }

    // IST epoch seconds for 2026-03-24 09:15:00 IST.
    // 2026-03-24 03:45:00 UTC = 1774323900 UTC epoch.
    // IST epoch = UTC epoch + 19800 = 1774343700.
    const SAMPLE_TS: u32 = 1_774_343_700;

    #[test]
    fn test_underlying_ltp_cache_update() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        // IDX_I tick for NIFTY (segment code 0)
        let tick = make_tick(13, 0, 25042.5, SAMPLE_TS);
        agg.update(&tick);

        assert_eq!(agg.tracked_underlyings(), 1);
        assert_eq!(*agg.underlying_ltp.get(&13).unwrap(), 25042.5);
    }

    #[test]
    fn test_underlying_ltp_cache_miss_skips_greeks() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        // F&O tick arrives BEFORE any underlying tick
        let fno_tick = make_tick(50001, 2, TEST_CE_LTP, SAMPLE_TS);
        agg.update(&fno_tick);

        assert_eq!(agg.tracked_options(), 1);

        let event = CandleCloseEvent {
            candle_ts: SAMPLE_TS,
            interval_secs: 1,
        };
        let emission = agg.snapshot(event);
        assert!(
            emission.greeks_snapshots.is_empty(),
            "no Greeks without underlying LTP"
        );
    }

    #[test]
    fn test_greeks_aggregator_updates_state_on_tick() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        let tick = make_tick(50001, 2, TEST_CE_LTP, SAMPLE_TS);
        agg.update(&tick);

        assert_eq!(agg.tracked_options(), 1);
        let state = agg.option_state.get(&50001).unwrap();
        assert_eq!(state.ltp, TEST_CE_LTP);
        assert_eq!(state.strike_price, TEST_STRIKE);
    }

    #[test]
    fn test_greeks_aggregator_emits_on_candle_close() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        // Step 1: Feed underlying LTP
        agg.update(&make_tick(13, 0, 25042.5, SAMPLE_TS));

        // Step 2: Feed option ticks
        agg.update(&make_tick(50001, 2, TEST_CE_LTP, SAMPLE_TS));
        agg.update(&make_tick(50002, 2, TEST_PE_LTP, SAMPLE_TS));

        // Step 3: Candle closes
        let event = CandleCloseEvent {
            candle_ts: SAMPLE_TS,
            interval_secs: 1,
        };
        let emission = agg.snapshot(event);

        // Should have 2 Greeks snapshots (CE + PE)
        assert_eq!(emission.greeks_snapshots.len(), 2);
        // Should have 1 PCR snapshot (NIFTY 2026-04-30)
        assert_eq!(emission.pcr_snapshots.len(), 1);

        // All timestamps match candle
        for snap in &emission.greeks_snapshots {
            assert_eq!(snap.candle_ts, SAMPLE_TS);
        }
        for snap in &emission.pcr_snapshots {
            assert_eq!(snap.candle_ts, SAMPLE_TS);
        }
    }

    #[test]
    fn test_greeks_aggregator_timestamp_matches_candle() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        agg.update(&make_tick(13, 0, 25042.5, SAMPLE_TS));
        agg.update(&make_tick(50001, 2, TEST_CE_LTP, SAMPLE_TS));

        let candle_ts = SAMPLE_TS + 60; // 1 minute later
        let event = CandleCloseEvent {
            candle_ts,
            interval_secs: 60,
        };
        let emission = agg.snapshot(event);

        assert!(!emission.greeks_snapshots.is_empty());
        assert_eq!(emission.greeks_snapshots[0].candle_ts, candle_ts);
    }

    #[test]
    fn test_greeks_aggregator_skips_equity_tick() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        // NSE_EQ tick (segment code 1) — should only update underlying LTP
        let tick = make_tick(2885, 1, 1200.0, SAMPLE_TS);
        agg.update(&tick);

        assert_eq!(agg.tracked_options(), 0);
        assert_eq!(agg.tracked_underlyings(), 1);
    }

    #[test]
    fn test_greeks_aggregator_skips_greeks_when_no_underlying_ltp() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        // Only option tick, no underlying
        agg.update(&make_tick(50001, 2, TEST_CE_LTP, SAMPLE_TS));

        let event = CandleCloseEvent {
            candle_ts: SAMPLE_TS,
            interval_secs: 1,
        };
        let emission = agg.snapshot(event);
        // Greeks require underlying LTP → should be empty
        assert!(emission.greeks_snapshots.is_empty());
        // PCR accumulates OI regardless of underlying LTP
        assert_eq!(emission.pcr_snapshots.len(), 1);
        assert_eq!(emission.pcr_snapshots[0].total_call_oi, 500_000);
    }

    #[test]
    fn test_pcr_updates_on_oi_change() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        // Underlying
        agg.update(&make_tick(13, 0, 25042.5, SAMPLE_TS));

        // CE with OI=500000
        let mut ce_tick = make_tick(50001, 2, TEST_CE_LTP, SAMPLE_TS);
        ce_tick.open_interest = 500_000;
        agg.update(&ce_tick);

        // PE with OI=600000
        let mut pe_tick = make_tick(50002, 2, TEST_PE_LTP, SAMPLE_TS);
        pe_tick.open_interest = 600_000;
        agg.update(&pe_tick);

        let event = CandleCloseEvent {
            candle_ts: SAMPLE_TS,
            interval_secs: 1,
        };
        let emission = agg.snapshot(event);
        assert_eq!(emission.pcr_snapshots.len(), 1);

        let pcr_snap = &emission.pcr_snapshots[0];
        assert_eq!(pcr_snap.total_call_oi, 500_000);
        assert_eq!(pcr_snap.total_put_oi, 600_000);
        // PCR = 600000/500000 = 1.2
        let pcr_val = pcr_snap.pcr_oi.unwrap();
        assert!((pcr_val - 1.2).abs() < 0.001);
    }

    #[test]
    fn test_pcr_uses_exchange_timestamp() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        agg.update(&make_tick(13, 0, 25042.5, SAMPLE_TS));
        agg.update(&make_tick(50001, 2, TEST_CE_LTP, SAMPLE_TS));
        agg.update(&make_tick(50002, 2, TEST_PE_LTP, SAMPLE_TS));

        let event = CandleCloseEvent {
            candle_ts: SAMPLE_TS + 120,
            interval_secs: 60,
        };
        let emission = agg.snapshot(event);
        assert_eq!(emission.pcr_snapshots[0].candle_ts, SAMPLE_TS + 120);
    }

    #[test]
    fn test_compute_time_to_expiry_future_date() {
        let expiry = NaiveDate::from_ymd_opt(2026, 4, 30).unwrap();
        let t = compute_time_to_expiry(expiry, SAMPLE_TS);
        // 2026-03-24 09:15 IST → 2026-04-30 15:30 IST ≈ 37 days
        assert!(t > 0.0, "time to expiry should be positive");
        assert!(t < 0.15, "should be less than ~2 months in years: {t}");
        assert!(t > 0.08, "should be more than ~1 month in years: {t}");
    }

    #[test]
    fn test_compute_time_to_expiry_past_date() {
        let expiry = NaiveDate::from_ymd_opt(2026, 3, 20).unwrap();
        let t = compute_time_to_expiry(expiry, SAMPLE_TS);
        assert_eq!(t, 0.0, "expired option should return 0");
    }

    #[test]
    fn test_compute_time_to_expiry_same_day() {
        let expiry = NaiveDate::from_ymd_opt(2026, 3, 24).unwrap();
        // 09:15 IST → 15:30 IST = 6h15m remaining
        let t = compute_time_to_expiry(expiry, SAMPLE_TS);
        assert!(t > 0.0, "should have some time remaining on expiry day");
        // 6.25 hours / (365*24) ≈ 0.000713
        assert!(t < 0.001, "should be less than 1 day in years: {t}");
    }

    #[test]
    fn test_empty_aggregator_snapshot() {
        let registry = InstrumentRegistry::empty();
        let mut agg = GreeksAggregator::new(registry, default_config());

        let event = CandleCloseEvent {
            candle_ts: SAMPLE_TS,
            interval_secs: 1,
        };
        let emission = agg.snapshot(event);
        assert!(emission.greeks_snapshots.is_empty());
        assert!(emission.pcr_snapshots.is_empty());
    }

    #[test]
    fn test_metrics_tracking() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        assert_eq!(agg.ticks_processed(), 0);
        assert_eq!(agg.snapshots_emitted(), 0);

        agg.update(&make_tick(13, 0, 25042.5, SAMPLE_TS));
        agg.update(&make_tick(50001, 2, TEST_CE_LTP, SAMPLE_TS));

        assert_eq!(agg.ticks_processed(), 2);

        let event = CandleCloseEvent {
            candle_ts: SAMPLE_TS,
            interval_secs: 1,
        };
        let emission = agg.snapshot(event);
        assert_eq!(
            agg.snapshots_emitted(),
            emission.greeks_snapshots.len() as u64
        );
    }

    // -----------------------------------------------------------------------
    // Coverage: near-zero option LTP skips Greeks computation
    // -----------------------------------------------------------------------

    #[test]
    fn test_near_zero_option_ltp_skips_greeks() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        // Feed underlying LTP
        agg.update(&make_tick(13, 0, 25042.5, SAMPLE_TS));

        // Feed option tick with near-zero LTP (0.005 < 0.01 threshold)
        agg.update(&make_tick(50001, 2, 0.005, SAMPLE_TS));

        let event = CandleCloseEvent {
            candle_ts: SAMPLE_TS,
            interval_secs: 1,
        };
        let emission = agg.snapshot(event);
        // Greeks should be empty (LTP too low), but PCR should still accumulate
        assert!(
            emission.greeks_snapshots.is_empty(),
            "near-zero LTP should skip Greeks computation"
        );
        assert_eq!(emission.pcr_snapshots.len(), 1, "PCR still accumulates");
    }

    // -----------------------------------------------------------------------
    // Coverage: unknown exchange segment tick is silently ignored
    // -----------------------------------------------------------------------

    #[test]
    fn test_unknown_segment_tick_ignored() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        // Segment code 6 (gap in enum) — should be ignored
        agg.update(&make_tick(9999, 6, 100.0, SAMPLE_TS));

        assert_eq!(agg.tracked_options(), 0);
        assert_eq!(agg.tracked_underlyings(), 0);
        assert_eq!(agg.ticks_processed(), 1); // Still counted as processed
    }

    // -----------------------------------------------------------------------
    // Coverage: option state fast-path update (second tick for same sec_id)
    // -----------------------------------------------------------------------

    #[test]
    fn test_option_state_fast_path_update() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        // First tick: slow path (enrichment from registry)
        agg.update(&make_tick(50001, 2, TEST_CE_LTP, SAMPLE_TS));
        assert_eq!(agg.tracked_options(), 1);
        let state_v1 = agg.option_state.get(&50001).unwrap();
        assert_eq!(state_v1.ltp, TEST_CE_LTP);

        // Second tick: fast path (existing entry update)
        let new_ltp = 250.0_f32;
        agg.update(&make_tick(50001, 2, new_ltp, SAMPLE_TS + 1));
        assert_eq!(agg.tracked_options(), 1); // Still 1 option
        let state_v2 = agg.option_state.get(&50001).unwrap();
        assert_eq!(state_v2.ltp, new_ltp);
    }

    // -----------------------------------------------------------------------
    // Coverage: FNO tick for unknown security_id (not in registry)
    // -----------------------------------------------------------------------

    #[test]
    fn test_fno_tick_for_unknown_security_skips() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        // Security ID 99999 is not in our test registry
        agg.update(&make_tick(99999, 2, 100.0, SAMPLE_TS));

        // Should NOT create an option state entry
        assert_eq!(agg.tracked_options(), 0);
    }

    // -----------------------------------------------------------------------
    // Coverage: negative/zero underlying LTP ignored
    // -----------------------------------------------------------------------

    #[test]
    fn test_negative_underlying_ltp_ignored() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        // Feed a negative LTP for underlying
        agg.update(&make_tick(13, 0, -1.0, SAMPLE_TS));
        assert_eq!(
            agg.tracked_underlyings(),
            0,
            "negative LTP should be ignored"
        );

        // Feed zero LTP
        agg.update(&make_tick(13, 0, 0.0, SAMPLE_TS));
        assert_eq!(agg.tracked_underlyings(), 0, "zero LTP should be ignored");
    }

    // -----------------------------------------------------------------------
    // Coverage: NaN underlying LTP ignored
    // -----------------------------------------------------------------------

    #[test]
    fn test_nan_underlying_ltp_ignored() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        agg.update(&make_tick(13, 0, f32::NAN, SAMPLE_TS));
        assert_eq!(agg.tracked_underlyings(), 0, "NaN LTP should be ignored");
    }

    // -----------------------------------------------------------------------
    // Coverage: expired option skips Greeks (time_to_expiry too small)
    // -----------------------------------------------------------------------

    #[test]
    fn test_expired_option_skips_greeks() {
        // Build registry with already-expired option
        let nifty_idx = make_index_instrument(13, "NIFTY");
        let expired_ce = make_option_instrument(
            60001,
            "NIFTY",
            22000.0,
            OptionType::Call,
            NaiveDate::from_ymd_opt(2026, 3, 1).unwrap_or_default(), // expired
        );
        let registry = InstrumentRegistry::from_instruments(vec![nifty_idx, expired_ce]);
        let mut agg = GreeksAggregator::new(registry, default_config());

        // Feed underlying and option ticks
        agg.update(&make_tick(13, 0, 25042.5, SAMPLE_TS));
        agg.update(&make_tick(60001, 2, 200.0, SAMPLE_TS));

        let event = CandleCloseEvent {
            candle_ts: SAMPLE_TS,
            interval_secs: 1,
        };
        let emission = agg.snapshot(event);
        // Expired option → time_to_expiry < MIN → skipped
        assert!(
            emission.greeks_snapshots.is_empty(),
            "expired option should skip Greeks"
        );
    }

    // -----------------------------------------------------------------------
    // Coverage: CandleCloseEvent debug and copy
    // -----------------------------------------------------------------------

    #[test]
    fn test_candle_close_event_debug_and_copy() {
        let event = CandleCloseEvent {
            candle_ts: SAMPLE_TS,
            interval_secs: 60,
        };
        let copy = event; // Copy trait
        assert_eq!(copy.candle_ts, SAMPLE_TS);
        let dbg = format!("{event:?}");
        assert!(dbg.contains("CandleCloseEvent"));
    }

    // -----------------------------------------------------------------------
    // Coverage: MCX commodity segment tick is silently ignored
    // -----------------------------------------------------------------------

    #[test]
    fn test_commodity_segment_tick_ignored() {
        let registry = build_test_registry();
        let mut agg = GreeksAggregator::new(registry, default_config());

        // MCX_COMM segment code = 5
        agg.update(&make_tick(7777, 5, 50000.0, SAMPLE_TS));
        assert_eq!(agg.tracked_options(), 0);
        assert_eq!(agg.tracked_underlyings(), 0);
    }
}
