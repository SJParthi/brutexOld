//! Domain types for the instrument universe.
//!
//! Block 01 produces these types. Every downstream block (WebSocket, OMS,
//! storage, API) consumes them. Types live in `common` so all crates
//! can depend on them without importing `core`.

use std::collections::HashMap;
use std::fmt;
use std::time::Duration;

use chrono::{DateTime, Datelike, FixedOffset, NaiveDate, Utc};
use serde::{Deserialize, Serialize};

use super::types::{Exchange, ExchangeSegment, OptionType, SecurityId};

// ---------------------------------------------------------------------------
// Underlying Classification
// ---------------------------------------------------------------------------

/// Classification of an F&O underlying.
#[derive(
    Debug,
    Clone,
    Copy,
    PartialEq,
    Eq,
    Hash,
    Serialize,
    Deserialize,
    rkyv::Archive,
    rkyv::Serialize,
    rkyv::Deserialize,
)]
#[rkyv(compare(PartialEq))]
pub enum UnderlyingKind {
    /// NSE index with F&O derivatives (e.g., NIFTY, BANKNIFTY).
    NseIndex,
    /// BSE index with F&O derivatives (e.g., SENSEX, BANKEX).
    BseIndex,
    /// Individual stock with F&O derivatives (e.g., RELIANCE, HDFCBANK).
    Stock,
}

impl UnderlyingKind {
    /// Returns the canonical string representation for storage and display.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::NseIndex => "NseIndex",
            Self::BseIndex => "BseIndex",
            Self::Stock => "Stock",
        }
    }
}

impl fmt::Display for UnderlyingKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Classification of a derivative instrument as it appears in the Dhan CSV.
#[derive(
    Debug,
    Clone,
    Copy,
    PartialEq,
    Eq,
    Hash,
    Serialize,
    Deserialize,
    rkyv::Archive,
    rkyv::Serialize,
    rkyv::Deserialize,
)]
#[rkyv(compare(PartialEq))]
pub enum DhanInstrumentKind {
    /// Index future (FUTIDX).
    FutureIndex,
    /// Stock future (FUTSTK).
    FutureStock,
    /// Index option (OPTIDX).
    OptionIndex,
    /// Stock option (OPTSTK).
    OptionStock,
}

impl DhanInstrumentKind {
    /// Returns the canonical string representation for storage and display.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::FutureIndex => "FutureIndex",
            Self::FutureStock => "FutureStock",
            Self::OptionIndex => "OptionIndex",
            Self::OptionStock => "OptionStock",
        }
    }

    /// S5-A2: Returns the Dhan REST API enum value for the `instrument`
    /// request field on `/v2/charts/intraday` and `/v2/charts/historical`.
    ///
    /// These are the exact strings Dhan expects per
    /// `docs/dhan-ref/05-historical-data.md`:
    ///   - `"FUTIDX"`: Index future (e.g., NIFTY 30-JAN-26)
    ///   - `"FUTSTK"`: Stock future (e.g., RELIANCE 30-JAN-26)
    ///   - `"OPTIDX"`: Index option (e.g., NIFTY 22000 CE 30-JAN-26)
    ///   - `"OPTSTK"`: Stock option (e.g., RELIANCE 2800 PE 30-JAN-26)
    // TEST-EXEMPT: trivial 4-arm match; covered indirectly by the backfill integration tests (test_backfill_worker_happy_path, test_backfill_worker_handles_empty_fetch) which exercise the full hint pass-through chain
    pub fn as_dhan_api_string(&self) -> &'static str {
        match self {
            Self::FutureIndex => "FUTIDX",
            Self::FutureStock => "FUTSTK",
            Self::OptionIndex => "OPTIDX",
            Self::OptionStock => "OPTSTK",
        }
    }
}

impl fmt::Display for DhanInstrumentKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

// ---------------------------------------------------------------------------
// Expiry Code (Dhan Annexure Section 9)
// ---------------------------------------------------------------------------

/// Expiry code for derivative historical data requests.
///
/// Dhan API `expiryCode` field — exactly 3 values per Annexure Section 9:
/// `0` = Current/Near, `1` = Next, `2` = Far.
///
/// Serializes as a bare integer (0, 1, 2) for JSON request bodies.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum ExpiryCode {
    /// Current/Near month expiry.
    Current = 0,
    /// Next month expiry.
    Next = 1,
    /// Far month expiry.
    Far = 2,
}

impl Serialize for ExpiryCode {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_u8(self.as_u8())
    }
}

impl<'de> Deserialize<'de> for ExpiryCode {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = u8::deserialize(deserializer)?;
        Self::from_u8(value)
            .ok_or_else(|| serde::de::Error::custom(format!("invalid expiry code: {value}")))
    }
}

impl ExpiryCode {
    /// Converts from a raw `u8`. Returns `None` for invalid values.
    // TEST-EXEMPT: tested by test_expiry_code_all_variants, test_expiry_code_from_u8_invalid_rejected, test_expiry_code_roundtrip
    pub fn from_u8(value: u8) -> Option<Self> {
        match value {
            0 => Some(Self::Current),
            1 => Some(Self::Next),
            2 => Some(Self::Far),
            _ => None,
        }
    }

    /// Returns the numeric value as `u8`.
    // TEST-EXEMPT: tested by test_expiry_code_serialize_to_u8, test_expiry_code_roundtrip
    pub fn as_u8(self) -> u8 {
        self as u8
    }
}

impl fmt::Display for ExpiryCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Current => f.write_str("Current"),
            Self::Next => f.write_str("Next"),
            Self::Far => f.write_str("Far"),
        }
    }
}

// ---------------------------------------------------------------------------
// Subscribed Index (8 F&O + 23 Display = 31 total)
// ---------------------------------------------------------------------------

/// Category of a subscribed index.
#[derive(
    Debug,
    Clone,
    Copy,
    PartialEq,
    Eq,
    Hash,
    Serialize,
    Deserialize,
    rkyv::Archive,
    rkyv::Serialize,
    rkyv::Deserialize,
)]
#[rkyv(compare(PartialEq))]
pub enum IndexCategory {
    /// F&O underlying index (has derivatives, e.g., NIFTY, BANKNIFTY, SENSEX).
    FnoUnderlying,
    /// Display-only index (market dashboard, no derivatives, e.g., INDIA VIX).
    DisplayIndex,
}

impl IndexCategory {
    /// Returns the canonical string representation for storage and display.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::FnoUnderlying => "FnoUnderlying",
            Self::DisplayIndex => "DisplayIndex",
        }
    }
}

impl fmt::Display for IndexCategory {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Subcategory of a display index for dashboard grouping.
#[derive(
    Debug,
    Clone,
    Copy,
    PartialEq,
    Eq,
    Hash,
    Serialize,
    Deserialize,
    rkyv::Archive,
    rkyv::Serialize,
    rkyv::Deserialize,
)]
#[rkyv(compare(PartialEq))]
pub enum IndexSubcategory {
    /// Volatility indices (e.g., INDIA VIX).
    Volatility,
    /// Broad market indices (e.g., NIFTY 100, NIFTY 500).
    BroadMarket,
    /// Mid cap indices (e.g., NIFTYMCAP50).
    MidCap,
    /// Small cap indices (e.g., NIFTY SMALLCAP 50).
    SmallCap,
    /// Sectoral indices (e.g., NIFTY AUTO, NIFTYIT).
    Sectoral,
    /// Thematic indices (e.g., NIFTY CONSUMPTION).
    Thematic,
    /// F&O index (category is FnoUnderlying, subcategory is Fno).
    Fno,
}

impl IndexSubcategory {
    /// Returns the canonical string representation for storage and display.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Volatility => "Volatility",
            Self::BroadMarket => "BroadMarket",
            Self::MidCap => "MidCap",
            Self::SmallCap => "SmallCap",
            Self::Sectoral => "Sectoral",
            Self::Thematic => "Thematic",
            Self::Fno => "Fno",
        }
    }
}

impl fmt::Display for IndexSubcategory {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

// ---------------------------------------------------------------------------
// Archived → Owned Conversions (for zero-copy rkyv access)
// ---------------------------------------------------------------------------

impl From<&ArchivedUnderlyingKind> for UnderlyingKind {
    fn from(archived: &ArchivedUnderlyingKind) -> Self {
        match archived {
            ArchivedUnderlyingKind::NseIndex => Self::NseIndex,
            ArchivedUnderlyingKind::BseIndex => Self::BseIndex,
            ArchivedUnderlyingKind::Stock => Self::Stock,
        }
    }
}

impl From<&ArchivedDhanInstrumentKind> for DhanInstrumentKind {
    fn from(archived: &ArchivedDhanInstrumentKind) -> Self {
        match archived {
            ArchivedDhanInstrumentKind::FutureIndex => Self::FutureIndex,
            ArchivedDhanInstrumentKind::FutureStock => Self::FutureStock,
            ArchivedDhanInstrumentKind::OptionIndex => Self::OptionIndex,
            ArchivedDhanInstrumentKind::OptionStock => Self::OptionStock,
        }
    }
}

impl From<&ArchivedIndexCategory> for IndexCategory {
    fn from(archived: &ArchivedIndexCategory) -> Self {
        match archived {
            ArchivedIndexCategory::FnoUnderlying => Self::FnoUnderlying,
            ArchivedIndexCategory::DisplayIndex => Self::DisplayIndex,
        }
    }
}

/// Reconstruct a `NaiveDate` from an archived rkyv `i32_le` (days from CE).
///
/// # Safety contract
/// The caller must ensure the `i32_le` value was produced by [`NaiveDateAsI32`]
/// serialization of a valid `NaiveDate`.
#[allow(clippy::expect_used)] // APPROVED: archived value validated at cache write time
pub fn naive_date_from_archived_i32(days: &rkyv::rend::i32_le) -> NaiveDate {
    NaiveDate::from_num_days_from_ce_opt((*days).into())
        .expect("valid days-from-CE in validated rkyv archive") // APPROVED: archived value validated at cache write time
}

/// A subscribed index — one of the 31 documented indices.
///
/// 8 F&O indices (NIFTY, BANKNIFTY, etc.) + 23 display indices (VIX, sectoral, etc.).
/// All subscribed on IDX_I exchange segment.
#[derive(
    Debug, Clone, Serialize, Deserialize, rkyv::Archive, rkyv::Serialize, rkyv::Deserialize,
)]
pub struct SubscribedIndex {
    /// Index symbol (e.g., "NIFTY", "INDIA VIX", "NIFTY AUTO").
    pub symbol: String,

    /// IDX_I security ID for WebSocket subscription.
    pub security_id: SecurityId,

    /// Exchange (NSE or BSE).
    pub exchange: Exchange,

    /// Category: FnoUnderlying or DisplayIndex.
    pub category: IndexCategory,

    /// Subcategory for dashboard grouping.
    pub subcategory: IndexSubcategory,
}

// ---------------------------------------------------------------------------
// F&O Underlying
// ---------------------------------------------------------------------------

/// A single underlying in the F&O universe.
///
/// One entry per unique `UNDERLYING_SYMBOL` that has derivatives.
/// Contains everything needed to subscribe to the underlying's live price
/// feed and to look up its derivative contracts.
#[derive(
    Debug, Clone, Serialize, Deserialize, rkyv::Archive, rkyv::Serialize, rkyv::Deserialize,
)]
pub struct FnoUnderlying {
    /// Underlying symbol (e.g., "NIFTY", "RELIANCE").
    pub underlying_symbol: String,

    /// Security ID from the CSV `UNDERLYING_SECURITY_ID` column.
    /// For indices, this is the "phantom" FNO ID (e.g., NIFTY = 26000),
    /// NOT the IDX_I price feed ID.
    pub underlying_security_id: SecurityId,

    /// Security ID for live price feed subscription.
    /// For indices: IDX_I ID (e.g., NIFTY = 13).
    /// For stocks: NSE_EQ ID (e.g., RELIANCE = 2885).
    pub price_feed_security_id: SecurityId,

    /// Exchange segment for the live price feed.
    /// IdxI for indices, NseEquity for stocks.
    pub price_feed_segment: ExchangeSegment,

    /// Exchange segment for derivatives.
    /// NseFno or BseFno.
    pub derivative_segment: ExchangeSegment,

    /// Classification of this underlying.
    pub kind: UnderlyingKind,

    /// Contract lot size.
    pub lot_size: u32,

    /// Total number of derivative contracts for this underlying.
    pub contract_count: usize,
}

// ---------------------------------------------------------------------------
// Derivative Contract
// ---------------------------------------------------------------------------

/// A single derivative contract (future or option).
#[derive(
    Debug, Clone, Serialize, Deserialize, rkyv::Archive, rkyv::Serialize, rkyv::Deserialize,
)]
pub struct DerivativeContract {
    /// This contract's own security ID (unique across all contracts).
    pub security_id: SecurityId,

    /// Symbol of the underlying (e.g., "NIFTY", "RELIANCE").
    pub underlying_symbol: String,

    /// Kind of derivative instrument.
    pub instrument_kind: DhanInstrumentKind,

    /// Exchange segment (NseFno or BseFno).
    pub exchange_segment: ExchangeSegment,

    /// Expiry date of this contract.
    #[rkyv(with = NaiveDateAsI32)]
    pub expiry_date: NaiveDate,

    /// Strike price. 0.0 for futures.
    pub strike_price: f64,

    /// Option type. `None` for futures.
    pub option_type: Option<OptionType>,

    /// Contract lot size.
    pub lot_size: u32,

    /// Minimum price movement.
    pub tick_size: f64,

    /// Full symbol from the CSV (e.g., "NIFTY-Mar2026-18000-CE").
    pub symbol_name: String,

    /// Human-readable display name.
    pub display_name: String,
}

// ---------------------------------------------------------------------------
// Option Chain
// ---------------------------------------------------------------------------

/// A single option chain: all contracts for one (underlying, expiry).
#[derive(
    Debug, Clone, Serialize, Deserialize, rkyv::Archive, rkyv::Serialize, rkyv::Deserialize,
)]
pub struct OptionChain {
    /// Underlying symbol.
    pub underlying_symbol: String,

    /// Expiry date for this chain.
    #[rkyv(with = NaiveDateAsI32)]
    pub expiry_date: NaiveDate,

    /// Call options sorted by strike price ascending.
    pub calls: Vec<OptionChainEntry>,

    /// Put options sorted by strike price ascending.
    pub puts: Vec<OptionChainEntry>,

    /// Corresponding future security ID for this expiry, if one exists.
    pub future_security_id: Option<SecurityId>,
}

/// One strike in an option chain.
#[derive(
    Debug, Clone, Serialize, Deserialize, rkyv::Archive, rkyv::Serialize, rkyv::Deserialize,
)]
pub struct OptionChainEntry {
    /// Security ID of this option contract.
    pub security_id: SecurityId,

    /// Strike price.
    pub strike_price: f64,

    /// Contract lot size.
    pub lot_size: u32,
}

// ---------------------------------------------------------------------------
// Option Chain Key
// ---------------------------------------------------------------------------

/// Composite key for option chain lookups.
#[derive(
    Debug,
    Clone,
    PartialEq,
    Eq,
    Hash,
    Serialize,
    Deserialize,
    rkyv::Archive,
    rkyv::Serialize,
    rkyv::Deserialize,
)]
#[rkyv(derive(Hash, PartialEq, Eq))]
pub struct OptionChainKey {
    /// Underlying symbol.
    pub underlying_symbol: String,

    /// Expiry date.
    #[rkyv(with = NaiveDateAsI32)]
    pub expiry_date: NaiveDate,
}

// ---------------------------------------------------------------------------
// Expiry Calendar
// ---------------------------------------------------------------------------

/// Expiry calendar for a single underlying.
#[derive(
    Debug, Clone, Serialize, Deserialize, rkyv::Archive, rkyv::Serialize, rkyv::Deserialize,
)]
pub struct ExpiryCalendar {
    /// Underlying symbol.
    pub underlying_symbol: String,

    /// Sorted expiry dates (ascending), all >= today at build time.
    #[rkyv(with = rkyv::with::Map<NaiveDateAsI32>)]
    pub expiry_dates: Vec<NaiveDate>,
}

// ---------------------------------------------------------------------------
// Instrument Info (global lookup)
// ---------------------------------------------------------------------------

/// Generic instrument info for any security ID.
///
/// Used by the WebSocket binary parser to decode what a security ID
/// represents when receiving tick data.
#[derive(
    Debug, Clone, Serialize, Deserialize, rkyv::Archive, rkyv::Serialize, rkyv::Deserialize,
)]
pub enum InstrumentInfo {
    /// An index (IDX_I segment).
    Index {
        security_id: SecurityId,
        symbol: String,
        exchange: Exchange,
    },
    /// An equity stock (NSE_EQ segment).
    Equity {
        security_id: SecurityId,
        symbol: String,
    },
    /// A derivative contract (NSE_FNO or BSE_FNO segment).
    Derivative {
        security_id: SecurityId,
        underlying_symbol: String,
        instrument_kind: DhanInstrumentKind,
        exchange_segment: ExchangeSegment,
        #[rkyv(with = NaiveDateAsI32)]
        expiry_date: NaiveDate,
        strike_price: f64,
        option_type: Option<OptionType>,
    },
}

// ---------------------------------------------------------------------------
// Build Metadata
// ---------------------------------------------------------------------------

/// Metadata about the universe build for logging and monitoring.
#[derive(
    Debug, Clone, Serialize, Deserialize, rkyv::Archive, rkyv::Serialize, rkyv::Deserialize,
)]
pub struct UniverseBuildMetadata {
    /// Which CSV URL was used (primary or fallback).
    pub csv_source: String,

    /// Total rows in the raw CSV file.
    pub csv_row_count: usize,

    /// Rows remaining after (NSE I/E/D + BSE I/D) filtering.
    pub parsed_row_count: usize,

    /// Pass 1 result: number of indices found.
    pub index_count: usize,

    /// Pass 2 result: number of equities found.
    pub equity_count: usize,

    /// Pass 3 result: number of F&O underlyings found.
    pub underlying_count: usize,

    /// Pass 5 result: number of derivative contracts.
    pub derivative_count: usize,

    /// Pass 5 result: number of unique option chains.
    pub option_chain_count: usize,

    /// Total universe build duration.
    #[serde(with = "duration_serde")]
    #[rkyv(with = DurationAsMillis)]
    pub build_duration: Duration,

    /// Timestamp when the build completed (IST).
    #[rkyv(with = DateTimeFixedOffsetAsI64)]
    pub build_timestamp: DateTime<FixedOffset>,
}

/// Serde helper for `Duration` (stored as milliseconds).
mod duration_serde {
    use std::time::Duration;

    use serde::{Deserialize, Deserializer, Serialize, Serializer};

    pub fn serialize<S: Serializer>(duration: &Duration, serializer: S) -> Result<S::Ok, S::Error> {
        duration.as_millis().serialize(serializer)
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Duration, D::Error> {
        let millis = u64::deserialize(deserializer)?;
        Ok(Duration::from_millis(millis))
    }
}

/// rkyv wrapper for `NaiveDate` — stored as `i32` (days from CE epoch).
///
/// chrono's rkyv-64 feature uses rkyv 0.7; we use rkyv 0.8. This bridges the gap.
/// Used via `#[rkyv(with = NaiveDateAsI32)]`.
pub struct NaiveDateAsI32;

impl rkyv::with::ArchiveWith<NaiveDate> for NaiveDateAsI32 {
    type Archived = rkyv::rend::i32_le;
    type Resolver = ();

    fn resolve_with(
        field: &NaiveDate,
        _resolver: Self::Resolver,
        out: rkyv::Place<Self::Archived>,
    ) {
        let value = rkyv::rend::i32_le::from_native(field.num_days_from_ce());
        rkyv::Archive::resolve(&value, (), out);
    }
}

impl<S: rkyv::rancor::Fallible + ?Sized> rkyv::with::SerializeWith<NaiveDate, S>
    for NaiveDateAsI32
{
    fn serialize_with(
        _field: &NaiveDate,
        _serializer: &mut S,
    ) -> Result<Self::Resolver, <S as rkyv::rancor::Fallible>::Error> {
        Ok(())
    }
}

impl<D: rkyv::rancor::Fallible + ?Sized>
    rkyv::with::DeserializeWith<rkyv::rend::i32_le, NaiveDate, D> for NaiveDateAsI32
where
    <D as rkyv::rancor::Fallible>::Error: rkyv::rancor::Source,
{
    fn deserialize_with(
        archived: &rkyv::rend::i32_le,
        _deserializer: &mut D,
    ) -> Result<NaiveDate, <D as rkyv::rancor::Fallible>::Error> {
        NaiveDate::from_num_days_from_ce_opt((*archived).into())
            .ok_or_else(|| rkyv::rancor::Source::new(NaiveDateRkyvError))
    }
}

/// Error type for invalid NaiveDate deserialization from rkyv.
#[derive(Debug)]
struct NaiveDateRkyvError;

impl fmt::Display for NaiveDateRkyvError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("invalid NaiveDate days-from-CE value in rkyv archive")
    }
}

impl std::error::Error for NaiveDateRkyvError {}

/// rkyv wrapper for `DateTime<FixedOffset>` — stored as `(i64, i32)` (timestamp secs + UTC offset secs).
///
/// Used via `#[rkyv(with = DateTimeFixedOffsetAsI64)]`.
pub struct DateTimeFixedOffsetAsI64;

/// Archived form of `DateTime<FixedOffset>`: timestamp seconds + UTC offset seconds.
#[derive(rkyv::Archive, rkyv::Serialize, rkyv::Deserialize)]
#[rkyv(compare(PartialEq))]
pub struct ArchivedDateTimeFixedOffset {
    pub timestamp_secs: i64,
    pub offset_secs: i32,
}

impl rkyv::with::ArchiveWith<DateTime<FixedOffset>> for DateTimeFixedOffsetAsI64 {
    type Archived = <ArchivedDateTimeFixedOffset as rkyv::Archive>::Archived;
    type Resolver = <ArchivedDateTimeFixedOffset as rkyv::Archive>::Resolver;

    fn resolve_with(
        field: &DateTime<FixedOffset>,
        resolver: Self::Resolver,
        out: rkyv::Place<Self::Archived>,
    ) {
        let proxy = ArchivedDateTimeFixedOffset {
            timestamp_secs: field.timestamp(),
            offset_secs: field.offset().local_minus_utc(),
        };
        rkyv::Archive::resolve(&proxy, resolver, out);
    }
}

impl<S: rkyv::rancor::Fallible + rkyv::ser::Writer + ?Sized>
    rkyv::with::SerializeWith<DateTime<FixedOffset>, S> for DateTimeFixedOffsetAsI64
{
    fn serialize_with(
        field: &DateTime<FixedOffset>,
        serializer: &mut S,
    ) -> Result<Self::Resolver, <S as rkyv::rancor::Fallible>::Error> {
        let proxy = ArchivedDateTimeFixedOffset {
            timestamp_secs: field.timestamp(),
            offset_secs: field.offset().local_minus_utc(),
        };
        rkyv::Serialize::serialize(&proxy, serializer)
    }
}

impl<D: rkyv::rancor::Fallible + ?Sized>
    rkyv::with::DeserializeWith<
        <ArchivedDateTimeFixedOffset as rkyv::Archive>::Archived,
        DateTime<FixedOffset>,
        D,
    > for DateTimeFixedOffsetAsI64
where
    <D as rkyv::rancor::Fallible>::Error: rkyv::rancor::Source,
{
    fn deserialize_with(
        archived: &<ArchivedDateTimeFixedOffset as rkyv::Archive>::Archived,
        deserializer: &mut D,
    ) -> Result<DateTime<FixedOffset>, <D as rkyv::rancor::Fallible>::Error> {
        let proxy: ArchivedDateTimeFixedOffset =
            rkyv::Deserialize::deserialize(archived, deserializer)?;
        let offset = FixedOffset::east_opt(proxy.offset_secs)
            .ok_or_else(|| rkyv::rancor::Source::new(DateTimeRkyvError))?;
        DateTime::from_timestamp(proxy.timestamp_secs, 0)
            .map(|dt| dt.with_timezone(&offset))
            .ok_or_else(|| rkyv::rancor::Source::new(DateTimeRkyvError))
    }
}

/// Error type for invalid DateTime deserialization from rkyv.
#[derive(Debug)]
struct DateTimeRkyvError;

impl fmt::Display for DateTimeRkyvError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("invalid DateTime<FixedOffset> value in rkyv archive")
    }
}

impl std::error::Error for DateTimeRkyvError {}

/// rkyv wrapper for `Duration` — stored as `u64` milliseconds.
///
/// Mirrors `duration_serde` above. Used via `#[rkyv(with = DurationAsMillis)]`.
pub struct DurationAsMillis;

impl rkyv::with::ArchiveWith<Duration> for DurationAsMillis {
    type Archived = rkyv::rend::u64_le;
    type Resolver = ();

    fn resolve_with(field: &Duration, _resolver: Self::Resolver, out: rkyv::Place<Self::Archived>) {
        #[allow(clippy::arithmetic_side_effects)]
        // APPROVED: Duration::as_millis() truncation u128→u64 is safe — won't overflow until year 584M+
        let millis = field.as_millis() as u64;
        let value = rkyv::rend::u64_le::from_native(millis);
        rkyv::Archive::resolve(&value, (), out);
    }
}

impl<S: rkyv::rancor::Fallible + ?Sized> rkyv::with::SerializeWith<Duration, S>
    for DurationAsMillis
{
    fn serialize_with(
        _field: &Duration,
        _serializer: &mut S,
    ) -> Result<Self::Resolver, <S as rkyv::rancor::Fallible>::Error> {
        Ok(())
    }
}

impl<D: rkyv::rancor::Fallible + ?Sized>
    rkyv::with::DeserializeWith<rkyv::rend::u64_le, Duration, D> for DurationAsMillis
{
    fn deserialize_with(
        archived: &rkyv::rend::u64_le,
        _deserializer: &mut D,
    ) -> Result<Duration, <D as rkyv::rancor::Fallible>::Error> {
        Ok(Duration::from_millis((*archived).into()))
    }
}

// ---------------------------------------------------------------------------
// Index Constituency (niftyindices.com data)
// ---------------------------------------------------------------------------

/// A single constituent stock within an NSE index.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IndexConstituent {
    /// Index name (e.g., "Nifty 50", "Nifty Bank").
    pub index_name: String,
    /// Stock symbol (e.g., "RELIANCE", "HDFCBANK").
    pub symbol: String,
    /// ISIN code (e.g., "INE002A01018").
    pub isin: String,
    /// Weight in the index (percentage, 0.0 if not available).
    pub weight: f64,
    /// Industry/sector classification from the CSV.
    pub sector: String,
    /// Date when this constituency data was last downloaded.
    pub last_updated: NaiveDate,
}

/// Bidirectional index ↔ stock mapping built from niftyindices.com CSVs.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct IndexConstituencyMap {
    /// Forward map: index name → list of constituent stocks.
    pub index_to_constituents: HashMap<String, Vec<IndexConstituent>>,
    /// Reverse map: stock symbol → list of index names containing this stock.
    pub stock_to_indices: HashMap<String, Vec<String>>,
    /// Build metadata for diagnostics.
    pub build_metadata: ConstituencyBuildMetadata,
}

impl IndexConstituencyMap {
    /// O(1) forward lookup: get all constituents of an index.
    pub fn get_constituents(&self, index_name: &str) -> Option<&[IndexConstituent]> {
        self.index_to_constituents
            .get(index_name)
            .map(|v| v.as_slice())
    }

    /// O(1) reverse lookup: get all indices containing a stock.
    pub fn get_indices_for_stock(&self, symbol: &str) -> Option<&[String]> {
        self.stock_to_indices.get(symbol).map(|v| v.as_slice())
    }

    /// Number of indices in the map.
    pub fn index_count(&self) -> usize {
        self.index_to_constituents.len()
    }

    /// Number of unique stocks across all indices.
    pub fn stock_count(&self) -> usize {
        self.stock_to_indices.len()
    }

    /// Sorted list of all index names.
    pub fn all_index_names(&self) -> Vec<&str> {
        let mut names: Vec<&str> = self
            .index_to_constituents
            .keys()
            .map(|s| s.as_str())
            .collect();
        names.sort_unstable();
        names
    }

    /// Quick membership check for a stock symbol.
    pub fn contains_stock(&self, symbol: &str) -> bool {
        self.stock_to_indices.contains_key(symbol)
    }

    /// Quick membership check for an index name.
    pub fn contains_index(&self, index_name: &str) -> bool {
        self.index_to_constituents.contains_key(index_name)
    }
}

/// Build metadata for index constituency download and parsing.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConstituencyBuildMetadata {
    /// Total time spent downloading and parsing CSVs.
    #[serde(with = "duration_serde")]
    pub download_duration: Duration,
    /// Number of indices successfully downloaded.
    pub indices_downloaded: usize,
    /// Number of indices that failed to download.
    pub indices_failed: usize,
    /// Number of unique stocks across all indices.
    pub unique_stocks: usize,
    /// Total number of (index, stock) mappings.
    pub total_mappings: usize,
    /// Timestamp when the build completed (IST).
    pub build_timestamp: DateTime<FixedOffset>,
}

impl Default for ConstituencyBuildMetadata {
    fn default() -> Self {
        let ist = super::trading_calendar::ist_offset();
        Self {
            download_duration: Duration::default(),
            indices_downloaded: 0,
            indices_failed: 0,
            unique_stocks: 0,
            total_mappings: 0,
            build_timestamp: Utc::now().with_timezone(&ist),
        }
    }
}

// ---------------------------------------------------------------------------
// F&O Universe (the single output artifact)
// ---------------------------------------------------------------------------

/// The complete F&O universe produced by Block 01.
///
/// This is the single artifact consumed by all downstream blocks:
/// WebSocket subscription, binary parser decoding, OMS order validation,
/// storage persistence, and API endpoints.
#[derive(
    Debug, Clone, Serialize, Deserialize, rkyv::Archive, rkyv::Serialize, rkyv::Deserialize,
)]
pub struct FnoUniverse {
    /// All F&O underlyings, keyed by underlying symbol.
    pub underlyings: HashMap<String, FnoUnderlying>,

    /// All derivative contracts, keyed by their own security ID.
    pub derivative_contracts: HashMap<SecurityId, DerivativeContract>,

    /// Global lookup: any security ID to what it represents.
    /// Covers indices, equities, AND derivatives.
    pub instrument_info: HashMap<SecurityId, InstrumentInfo>,

    /// Option chains grouped by (underlying symbol, expiry date).
    pub option_chains: HashMap<OptionChainKey, OptionChain>,

    /// Expiry calendars per underlying symbol.
    pub expiry_calendars: HashMap<String, ExpiryCalendar>,

    /// All 31 subscribed indices (8 F&O + 23 Display), keyed by security ID.
    pub subscribed_indices: Vec<SubscribedIndex>,

    /// Build metadata for diagnostics.
    pub build_metadata: UniverseBuildMetadata,
}

impl FnoUniverse {
    /// O(1) lookup: symbol → equity price feed security ID.
    /// For "RELIANCE" → 2885, for "NIFTY" → 13.
    /// Returns `None` if the symbol is not an F&O underlying.
    pub fn symbol_to_security_id(&self, symbol: &str) -> Option<SecurityId> {
        self.underlyings
            .get(symbol)
            .map(|u| u.price_feed_security_id)
    }

    /// O(N) scan: symbol → all derivative contracts for that underlying.
    /// Returns futures + options contracts as security IDs.
    /// N = total derivative contracts in the universe.
    /// Cold path only — called during subscription planning, not on tick path.
    pub fn derivative_security_ids_for_symbol(&self, symbol: &str) -> Vec<SecurityId> {
        self.derivative_contracts
            .values()
            .filter(|c| c.underlying_symbol == symbol)
            .map(|c| c.security_id)
            .collect()
    }

    /// O(1) lookup: symbol → underlying details (lot size, segment, etc.).
    pub fn get_underlying(&self, symbol: &str) -> Option<&FnoUnderlying> {
        self.underlyings.get(symbol)
    }

    /// O(1) lookup: security_id → symbol string.
    /// Works for indices, equities, and derivatives (returns underlying_symbol for derivatives).
    pub fn security_id_to_symbol(&self, security_id: SecurityId) -> Option<&str> {
        self.instrument_info
            .get(&security_id)
            .map(|info| match info {
                InstrumentInfo::Index { symbol, .. } => symbol.as_str(),
                InstrumentInfo::Equity { symbol, .. } => symbol.as_str(),
                InstrumentInfo::Derivative {
                    underlying_symbol, ..
                } => underlying_symbol.as_str(),
            })
    }

    /// O(N) scan: index symbol → index security ID from subscribed_indices.
    /// "NIFTY" → 13, "BANKNIFTY" → 29.
    /// N = subscribed index count (31 max). Cold path only.
    pub fn index_symbol_to_security_id(&self, symbol: &str) -> Option<SecurityId> {
        self.subscribed_indices
            .iter()
            .find(|idx| idx.symbol == symbol)
            .map(|idx| idx.security_id)
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // --- UnderlyingKind ---

    #[test]
    fn test_underlying_kind_as_str_all_variants() {
        assert_eq!(UnderlyingKind::NseIndex.as_str(), "NseIndex");
        assert_eq!(UnderlyingKind::BseIndex.as_str(), "BseIndex");
        assert_eq!(UnderlyingKind::Stock.as_str(), "Stock");
    }

    #[test]
    fn test_underlying_kind_display() {
        assert_eq!(format!("{}", UnderlyingKind::NseIndex), "NseIndex");
        assert_eq!(format!("{}", UnderlyingKind::Stock), "Stock");
    }

    #[test]
    fn test_underlying_kind_serde_roundtrip() {
        let original = UnderlyingKind::BseIndex;
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: UnderlyingKind = serde_json::from_str(&json).unwrap();
        assert_eq!(original, deserialized);
    }

    // --- DhanInstrumentKind ---

    #[test]
    fn test_dhan_instrument_kind_as_str_all_variants() {
        assert_eq!(DhanInstrumentKind::FutureIndex.as_str(), "FutureIndex");
        assert_eq!(DhanInstrumentKind::FutureStock.as_str(), "FutureStock");
        assert_eq!(DhanInstrumentKind::OptionIndex.as_str(), "OptionIndex");
        assert_eq!(DhanInstrumentKind::OptionStock.as_str(), "OptionStock");
    }

    #[test]
    fn test_dhan_instrument_kind_display() {
        assert_eq!(
            format!("{}", DhanInstrumentKind::FutureIndex),
            "FutureIndex"
        );
        assert_eq!(
            format!("{}", DhanInstrumentKind::OptionStock),
            "OptionStock"
        );
    }

    #[test]
    fn test_dhan_instrument_kind_serde_roundtrip() {
        let original = DhanInstrumentKind::OptionIndex;
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: DhanInstrumentKind = serde_json::from_str(&json).unwrap();
        assert_eq!(original, deserialized);
    }

    // --- IndexCategory ---

    #[test]
    fn test_index_category_as_str_all_variants() {
        assert_eq!(IndexCategory::FnoUnderlying.as_str(), "FnoUnderlying");
        assert_eq!(IndexCategory::DisplayIndex.as_str(), "DisplayIndex");
    }

    #[test]
    fn test_index_category_display() {
        assert_eq!(format!("{}", IndexCategory::FnoUnderlying), "FnoUnderlying");
        assert_eq!(format!("{}", IndexCategory::DisplayIndex), "DisplayIndex");
    }

    #[test]
    fn test_index_category_serde_roundtrip() {
        let original = IndexCategory::DisplayIndex;
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: IndexCategory = serde_json::from_str(&json).unwrap();
        assert_eq!(original, deserialized);
    }

    // --- IndexSubcategory ---

    #[test]
    fn test_index_subcategory_as_str_all_variants() {
        assert_eq!(IndexSubcategory::Volatility.as_str(), "Volatility");
        assert_eq!(IndexSubcategory::BroadMarket.as_str(), "BroadMarket");
        assert_eq!(IndexSubcategory::MidCap.as_str(), "MidCap");
        assert_eq!(IndexSubcategory::SmallCap.as_str(), "SmallCap");
        assert_eq!(IndexSubcategory::Sectoral.as_str(), "Sectoral");
        assert_eq!(IndexSubcategory::Thematic.as_str(), "Thematic");
        assert_eq!(IndexSubcategory::Fno.as_str(), "Fno");
    }

    #[test]
    fn test_index_subcategory_display() {
        assert_eq!(format!("{}", IndexSubcategory::Volatility), "Volatility");
        assert_eq!(format!("{}", IndexSubcategory::Fno), "Fno");
    }

    #[test]
    fn test_index_subcategory_serde_roundtrip_all_variants() {
        let variants = [
            IndexSubcategory::Volatility,
            IndexSubcategory::BroadMarket,
            IndexSubcategory::MidCap,
            IndexSubcategory::SmallCap,
            IndexSubcategory::Sectoral,
            IndexSubcategory::Thematic,
            IndexSubcategory::Fno,
        ];
        for variant in variants {
            let json = serde_json::to_string(&variant).unwrap();
            let deserialized: IndexSubcategory = serde_json::from_str(&json).unwrap();
            assert_eq!(
                variant, deserialized,
                "Serde roundtrip failed for {:?}",
                variant
            );
        }
    }

    #[test]
    fn test_underlying_kind_serde_roundtrip_all_variants() {
        let variants = [
            UnderlyingKind::NseIndex,
            UnderlyingKind::BseIndex,
            UnderlyingKind::Stock,
        ];
        for variant in variants {
            let json = serde_json::to_string(&variant).unwrap();
            let deserialized: UnderlyingKind = serde_json::from_str(&json).unwrap();
            assert_eq!(
                variant, deserialized,
                "Serde roundtrip failed for {:?}",
                variant
            );
        }
    }

    #[test]
    fn test_dhan_instrument_kind_serde_roundtrip_all_variants() {
        let variants = [
            DhanInstrumentKind::FutureIndex,
            DhanInstrumentKind::FutureStock,
            DhanInstrumentKind::OptionIndex,
            DhanInstrumentKind::OptionStock,
        ];
        for variant in variants {
            let json = serde_json::to_string(&variant).unwrap();
            let deserialized: DhanInstrumentKind = serde_json::from_str(&json).unwrap();
            assert_eq!(
                variant, deserialized,
                "Serde roundtrip failed for {:?}",
                variant
            );
        }
    }

    #[test]
    fn test_index_category_serde_roundtrip_all_variants() {
        let variants = [IndexCategory::FnoUnderlying, IndexCategory::DisplayIndex];
        for variant in variants {
            let json = serde_json::to_string(&variant).unwrap();
            let deserialized: IndexCategory = serde_json::from_str(&json).unwrap();
            assert_eq!(
                variant, deserialized,
                "Serde roundtrip failed for {:?}",
                variant
            );
        }
    }

    #[test]
    fn test_index_subcategory_serde_roundtrip() {
        let original = IndexSubcategory::Sectoral;
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: IndexSubcategory = serde_json::from_str(&json).unwrap();
        assert_eq!(original, deserialized);
    }

    // --- OptionChainKey equality and hash ---

    #[test]
    fn test_option_chain_key_equality() {
        let key1 = OptionChainKey {
            underlying_symbol: "NIFTY".to_string(),
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
        };
        let key2 = OptionChainKey {
            underlying_symbol: "NIFTY".to_string(),
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
        };
        assert_eq!(key1, key2);
    }

    #[test]
    fn test_option_chain_key_inequality_symbol() {
        let key1 = OptionChainKey {
            underlying_symbol: "NIFTY".to_string(),
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
        };
        let key2 = OptionChainKey {
            underlying_symbol: "BANKNIFTY".to_string(),
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
        };
        assert_ne!(key1, key2);
    }

    #[test]
    fn test_option_chain_key_inequality_expiry() {
        let key1 = OptionChainKey {
            underlying_symbol: "NIFTY".to_string(),
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
        };
        let key2 = OptionChainKey {
            underlying_symbol: "NIFTY".to_string(),
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 4, 30).unwrap(),
        };
        assert_ne!(key1, key2);
    }

    #[test]
    fn test_option_chain_key_hash_map_usage() {
        use std::collections::HashMap;
        let key = OptionChainKey {
            underlying_symbol: "NIFTY".to_string(),
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
        };
        let mut map = HashMap::new();
        map.insert(key.clone(), 42);
        assert_eq!(map.get(&key), Some(&42));
    }

    // --- Duration serde helper ---

    #[test]
    fn test_build_metadata_duration_serde_roundtrip() {
        use chrono::Utc;
        let ist = crate::compat::trading_calendar::ist_offset();
        let metadata = UniverseBuildMetadata {
            csv_source: "primary".to_string(),
            csv_row_count: 100,
            parsed_row_count: 50,
            index_count: 10,
            equity_count: 20,
            underlying_count: 5,
            derivative_count: 100,
            option_chain_count: 10,
            build_duration: std::time::Duration::from_millis(1234),
            build_timestamp: Utc::now().with_timezone(&ist),
        };

        let json = serde_json::to_string(&metadata).unwrap();
        let deserialized: UniverseBuildMetadata = serde_json::from_str(&json).unwrap();

        assert_eq!(
            deserialized.build_duration,
            std::time::Duration::from_millis(1234)
        );
        assert_eq!(deserialized.csv_source, "primary");
        assert_eq!(deserialized.csv_row_count, 100);
    }

    // =====================================================================
    // FnoUniverse method tests — exhaustive coverage
    // =====================================================================

    fn make_test_build_metadata() -> UniverseBuildMetadata {
        use chrono::{FixedOffset, Utc};
        let ist = FixedOffset::east_opt(19_800).unwrap();
        UniverseBuildMetadata {
            csv_source: "test".to_string(),
            csv_row_count: 0,
            parsed_row_count: 0,
            index_count: 0,
            equity_count: 0,
            underlying_count: 0,
            derivative_count: 0,
            option_chain_count: 0,
            build_duration: std::time::Duration::ZERO,
            build_timestamp: Utc::now().with_timezone(&ist),
        }
    }

    fn make_test_fno_universe() -> FnoUniverse {
        use crate::compat::types::{Exchange, ExchangeSegment, OptionType};

        let mut underlyings = HashMap::new();
        underlyings.insert(
            "RELIANCE".to_string(),
            FnoUnderlying {
                underlying_symbol: "RELIANCE".to_string(),
                underlying_security_id: 26000,
                price_feed_security_id: 2885,
                price_feed_segment: ExchangeSegment::NseEquity,
                derivative_segment: ExchangeSegment::NseFno,
                kind: UnderlyingKind::Stock,
                lot_size: 250,
                contract_count: 3,
            },
        );
        underlyings.insert(
            "NIFTY".to_string(),
            FnoUnderlying {
                underlying_symbol: "NIFTY".to_string(),
                underlying_security_id: 26000,
                price_feed_security_id: 13,
                price_feed_segment: ExchangeSegment::IdxI,
                derivative_segment: ExchangeSegment::NseFno,
                kind: UnderlyingKind::NseIndex,
                lot_size: 25,
                contract_count: 2,
            },
        );
        underlyings.insert(
            "HDFCBANK".to_string(),
            FnoUnderlying {
                underlying_symbol: "HDFCBANK".to_string(),
                underlying_security_id: 27000,
                price_feed_security_id: 1333,
                price_feed_segment: ExchangeSegment::NseEquity,
                derivative_segment: ExchangeSegment::NseFno,
                kind: UnderlyingKind::Stock,
                lot_size: 550,
                contract_count: 1,
            },
        );

        let mut derivative_contracts = HashMap::new();
        derivative_contracts.insert(
            49001,
            DerivativeContract {
                security_id: 49001,
                underlying_symbol: "RELIANCE".to_string(),
                instrument_kind: DhanInstrumentKind::FutureStock,
                exchange_segment: ExchangeSegment::NseFno,
                expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
                strike_price: 0.0,
                option_type: None,
                lot_size: 250,
                tick_size: 0.05,
                symbol_name: "RELIANCE-Mar2026-FUT".to_string(),
                display_name: "RELIANCE Mar2026 Future".to_string(),
            },
        );
        derivative_contracts.insert(
            49002,
            DerivativeContract {
                security_id: 49002,
                underlying_symbol: "RELIANCE".to_string(),
                instrument_kind: DhanInstrumentKind::OptionStock,
                exchange_segment: ExchangeSegment::NseFno,
                expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
                strike_price: 3000.0,
                option_type: Some(OptionType::Call),
                lot_size: 250,
                tick_size: 0.05,
                symbol_name: "RELIANCE-Mar2026-3000-CE".to_string(),
                display_name: "RELIANCE Mar2026 3000 CE".to_string(),
            },
        );
        derivative_contracts.insert(
            50001,
            DerivativeContract {
                security_id: 50001,
                underlying_symbol: "NIFTY".to_string(),
                instrument_kind: DhanInstrumentKind::FutureIndex,
                exchange_segment: ExchangeSegment::NseFno,
                expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
                strike_price: 0.0,
                option_type: None,
                lot_size: 25,
                tick_size: 0.05,
                symbol_name: "NIFTY-Mar2026-FUT".to_string(),
                display_name: "NIFTY Mar2026 Future".to_string(),
            },
        );

        let mut instrument_info = HashMap::new();
        instrument_info.insert(
            13,
            InstrumentInfo::Index {
                security_id: 13,
                symbol: "NIFTY".to_string(),
                exchange: Exchange::NationalStockExchange,
            },
        );
        instrument_info.insert(
            2885,
            InstrumentInfo::Equity {
                security_id: 2885,
                symbol: "RELIANCE".to_string(),
            },
        );
        instrument_info.insert(
            1333,
            InstrumentInfo::Equity {
                security_id: 1333,
                symbol: "HDFCBANK".to_string(),
            },
        );
        instrument_info.insert(
            49001,
            InstrumentInfo::Derivative {
                security_id: 49001,
                underlying_symbol: "RELIANCE".to_string(),
                instrument_kind: DhanInstrumentKind::FutureStock,
                exchange_segment: ExchangeSegment::NseFno,
                expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
                strike_price: 0.0,
                option_type: None,
            },
        );

        let subscribed_indices = vec![
            SubscribedIndex {
                symbol: "NIFTY".to_string(),
                security_id: 13,
                exchange: Exchange::NationalStockExchange,
                category: IndexCategory::FnoUnderlying,
                subcategory: IndexSubcategory::Fno,
            },
            SubscribedIndex {
                symbol: "BANKNIFTY".to_string(),
                security_id: 29,
                exchange: Exchange::NationalStockExchange,
                category: IndexCategory::FnoUnderlying,
                subcategory: IndexSubcategory::Fno,
            },
        ];

        FnoUniverse {
            underlyings,
            derivative_contracts,
            instrument_info,
            option_chains: HashMap::new(),
            expiry_calendars: HashMap::new(),
            subscribed_indices,
            build_metadata: make_test_build_metadata(),
        }
    }

    #[test]
    fn test_symbol_to_security_id_stock() {
        let u = make_test_fno_universe();
        assert_eq!(u.symbol_to_security_id("RELIANCE"), Some(2885));
        assert_eq!(u.symbol_to_security_id("HDFCBANK"), Some(1333));
    }

    #[test]
    fn test_symbol_to_security_id_index() {
        let u = make_test_fno_universe();
        assert_eq!(u.symbol_to_security_id("NIFTY"), Some(13));
    }

    #[test]
    fn test_symbol_to_security_id_unknown() {
        let u = make_test_fno_universe();
        assert_eq!(u.symbol_to_security_id("NONEXISTENT"), None);
        assert_eq!(u.symbol_to_security_id(""), None);
    }

    #[test]
    fn test_symbol_to_security_id_case_sensitive() {
        let u = make_test_fno_universe();
        assert!(u.symbol_to_security_id("Reliance").is_none());
        assert!(u.symbol_to_security_id("nifty").is_none());
        assert!(u.symbol_to_security_id("RELIANCE").is_some());
    }

    #[test]
    fn test_symbol_to_security_id_empty_universe() {
        let u = FnoUniverse {
            underlyings: HashMap::new(),
            derivative_contracts: HashMap::new(),
            instrument_info: HashMap::new(),
            option_chains: HashMap::new(),
            expiry_calendars: HashMap::new(),
            subscribed_indices: Vec::new(),
            build_metadata: make_test_build_metadata(),
        };
        assert_eq!(u.symbol_to_security_id("RELIANCE"), None);
    }

    #[test]
    fn test_security_id_to_symbol_equity() {
        let u = make_test_fno_universe();
        assert_eq!(u.security_id_to_symbol(2885), Some("RELIANCE"));
        assert_eq!(u.security_id_to_symbol(1333), Some("HDFCBANK"));
    }

    #[test]
    fn test_security_id_to_symbol_index() {
        let u = make_test_fno_universe();
        assert_eq!(u.security_id_to_symbol(13), Some("NIFTY"));
    }

    #[test]
    fn test_security_id_to_symbol_derivative_returns_underlying() {
        let u = make_test_fno_universe();
        assert_eq!(u.security_id_to_symbol(49001), Some("RELIANCE"));
    }

    #[test]
    fn test_security_id_to_symbol_unknown() {
        let u = make_test_fno_universe();
        assert_eq!(u.security_id_to_symbol(99999), None);
        assert_eq!(u.security_id_to_symbol(0), None);
    }

    #[test]
    fn test_symbol_security_id_roundtrip() {
        let u = make_test_fno_universe();
        for sym in &["RELIANCE", "HDFCBANK", "NIFTY"] {
            let sid = u.symbol_to_security_id(sym).unwrap();
            let back = u.security_id_to_symbol(sid).unwrap();
            assert_eq!(*sym, back, "roundtrip failed for {sym}");
        }
    }

    #[test]
    fn test_get_underlying_full_details() {
        let u = make_test_fno_universe();
        let underlying = u.get_underlying("RELIANCE").unwrap();
        assert_eq!(underlying.lot_size, 250);
        assert_eq!(underlying.kind, UnderlyingKind::Stock);
        assert_eq!(underlying.price_feed_security_id, 2885);
    }

    #[test]
    fn test_get_underlying_unknown() {
        let u = make_test_fno_universe();
        assert!(u.get_underlying("TCS").is_none());
    }

    #[test]
    fn test_derivative_ids_for_stock() {
        let u = make_test_fno_universe();
        let mut ids = u.derivative_security_ids_for_symbol("RELIANCE");
        ids.sort();
        assert_eq!(ids, vec![49001, 49002]);
    }

    #[test]
    fn test_derivative_ids_for_index() {
        let u = make_test_fno_universe();
        let ids = u.derivative_security_ids_for_symbol("NIFTY");
        assert_eq!(ids, vec![50001]);
    }

    #[test]
    fn test_derivative_ids_unknown_empty() {
        let u = make_test_fno_universe();
        assert!(u.derivative_security_ids_for_symbol("UNKNOWN").is_empty());
    }

    #[test]
    fn test_derivative_ids_no_cross_contamination() {
        let u = make_test_fno_universe();
        let reliance_ids = u.derivative_security_ids_for_symbol("RELIANCE");
        assert!(!reliance_ids.contains(&50001)); // NIFTY future
    }

    #[test]
    fn test_index_symbol_to_security_id_known() {
        let u = make_test_fno_universe();
        assert_eq!(u.index_symbol_to_security_id("NIFTY"), Some(13));
        assert_eq!(u.index_symbol_to_security_id("BANKNIFTY"), Some(29));
    }

    #[test]
    fn test_index_symbol_to_security_id_unknown() {
        let u = make_test_fno_universe();
        assert_eq!(u.index_symbol_to_security_id("NONEXISTENT"), None);
    }

    #[test]
    fn test_idempotent_lookups_100_iterations() {
        let u = make_test_fno_universe();
        for _ in 0..100 {
            assert_eq!(u.symbol_to_security_id("RELIANCE"), Some(2885));
            assert_eq!(u.security_id_to_symbol(2885), Some("RELIANCE"));
            assert_eq!(u.index_symbol_to_security_id("NIFTY"), Some(13));
        }
    }

    #[test]
    fn test_cross_day_security_id_reuse() {
        let day1 = make_test_fno_universe();
        assert_eq!(day1.security_id_to_symbol(49001), Some("RELIANCE"));

        let mut day2 = make_test_fno_universe();
        day2.instrument_info.insert(
            49001,
            InstrumentInfo::Derivative {
                security_id: 49001,
                underlying_symbol: "HDFCBANK".to_string(),
                instrument_kind: DhanInstrumentKind::FutureStock,
                exchange_segment: ExchangeSegment::NseFno,
                expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 4, 24).unwrap(),
                strike_price: 0.0,
                option_type: None,
            },
        );
        assert_eq!(day1.security_id_to_symbol(49001), Some("RELIANCE"));
        assert_eq!(day2.security_id_to_symbol(49001), Some("HDFCBANK"));
    }

    #[test]
    fn test_all_underlyings_have_nonzero_price_feed_id() {
        let u = make_test_fno_universe();
        for (sym, underlying) in &u.underlyings {
            assert!(
                underlying.price_feed_security_id > 0,
                "{sym} has zero price_feed_security_id"
            );
        }
    }

    #[test]
    fn test_all_derivatives_reference_valid_underlying() {
        let u = make_test_fno_universe();
        for (sid, contract) in &u.derivative_contracts {
            assert!(
                u.underlyings.contains_key(&contract.underlying_symbol),
                "derivative {sid} references unknown underlying: {}",
                contract.underlying_symbol
            );
        }
    }

    #[test]
    fn test_instrument_info_no_duplicate_security_ids() {
        let u = make_test_fno_universe();
        let count = u.instrument_info.len();
        let unique: std::collections::HashSet<_> = u.instrument_info.keys().collect();
        assert_eq!(count, unique.len());
    }

    // =====================================================================
    // rkyv roundtrip tests — NaiveDateAsI32, DateTimeFixedOffsetAsI64, DurationAsMillis
    // =====================================================================

    #[test]
    fn test_rkyv_naive_date_roundtrip() {
        // Create a struct that uses NaiveDateAsI32
        let original = DerivativeContract {
            security_id: 49001,
            underlying_symbol: "NIFTY".to_string(),
            instrument_kind: DhanInstrumentKind::FutureIndex,
            exchange_segment: ExchangeSegment::NseFno,
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
            strike_price: 0.0,
            option_type: None,
            lot_size: 25,
            tick_size: 0.05,
            symbol_name: "NIFTY-FUT".to_string(),
            display_name: "NIFTY FUT".to_string(),
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&original).unwrap();
        let archived =
            rkyv::access::<ArchivedDerivativeContract, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: DerivativeContract =
            rkyv::deserialize::<DerivativeContract, rkyv::rancor::Error>(archived).unwrap();
        assert_eq!(deserialized.expiry_date, original.expiry_date);
        assert_eq!(deserialized.security_id, 49001);
        assert_eq!(deserialized.underlying_symbol, "NIFTY");
    }

    #[test]
    fn test_rkyv_datetime_fixed_offset_roundtrip() {
        let ist = crate::compat::trading_calendar::ist_offset();
        let original = UniverseBuildMetadata {
            csv_source: "test".to_string(),
            csv_row_count: 42,
            parsed_row_count: 20,
            index_count: 5,
            equity_count: 10,
            underlying_count: 3,
            derivative_count: 50,
            option_chain_count: 8,
            build_duration: std::time::Duration::from_millis(1234),
            build_timestamp: chrono::Utc::now().with_timezone(&ist),
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&original).unwrap();
        let archived =
            rkyv::access::<ArchivedUniverseBuildMetadata, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: UniverseBuildMetadata =
            rkyv::deserialize::<UniverseBuildMetadata, rkyv::rancor::Error>(archived).unwrap();
        assert_eq!(
            deserialized.build_timestamp.timestamp(),
            original.build_timestamp.timestamp()
        );
        assert_eq!(
            deserialized.build_timestamp.offset().local_minus_utc(),
            19_800
        );
        assert_eq!(deserialized.csv_row_count, 42);
    }

    #[test]
    fn test_rkyv_duration_as_millis_roundtrip() {
        let ist = crate::compat::trading_calendar::ist_offset();
        let original = UniverseBuildMetadata {
            csv_source: "test".to_string(),
            csv_row_count: 0,
            parsed_row_count: 0,
            index_count: 0,
            equity_count: 0,
            underlying_count: 0,
            derivative_count: 0,
            option_chain_count: 0,
            build_duration: std::time::Duration::from_millis(5678),
            build_timestamp: chrono::Utc::now().with_timezone(&ist),
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&original).unwrap();
        let archived =
            rkyv::access::<ArchivedUniverseBuildMetadata, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: UniverseBuildMetadata =
            rkyv::deserialize::<UniverseBuildMetadata, rkyv::rancor::Error>(archived).unwrap();
        assert_eq!(
            deserialized.build_duration,
            std::time::Duration::from_millis(5678)
        );
    }

    // =====================================================================
    // Archived → Owned From conversion tests
    // =====================================================================

    #[test]
    fn test_archived_underlying_kind_from_all_variants() {
        // Roundtrip through rkyv archive to get ArchivedUnderlyingKind
        let underlying = FnoUnderlying {
            underlying_symbol: "NIFTY".to_string(),
            underlying_security_id: 26000,
            price_feed_security_id: 13,
            price_feed_segment: ExchangeSegment::IdxI,
            derivative_segment: ExchangeSegment::NseFno,
            kind: UnderlyingKind::NseIndex,
            lot_size: 25,
            contract_count: 2,
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&underlying).unwrap();
        let archived = rkyv::access::<ArchivedFnoUnderlying, rkyv::rancor::Error>(&bytes).unwrap();
        assert_eq!(
            UnderlyingKind::from(&archived.kind),
            UnderlyingKind::NseIndex
        );

        // BseIndex variant
        let mut bse = underlying.clone();
        bse.kind = UnderlyingKind::BseIndex;
        let bytes2 = rkyv::to_bytes::<rkyv::rancor::Error>(&bse).unwrap();
        let archived2 =
            rkyv::access::<ArchivedFnoUnderlying, rkyv::rancor::Error>(&bytes2).unwrap();
        assert_eq!(
            UnderlyingKind::from(&archived2.kind),
            UnderlyingKind::BseIndex
        );

        // Stock variant
        let mut stock = underlying;
        stock.kind = UnderlyingKind::Stock;
        let bytes3 = rkyv::to_bytes::<rkyv::rancor::Error>(&stock).unwrap();
        let archived3 =
            rkyv::access::<ArchivedFnoUnderlying, rkyv::rancor::Error>(&bytes3).unwrap();
        assert_eq!(UnderlyingKind::from(&archived3.kind), UnderlyingKind::Stock);
    }

    #[test]
    fn test_archived_dhan_instrument_kind_from_all_variants() {
        let make_contract = |kind: DhanInstrumentKind| DerivativeContract {
            security_id: 49001,
            underlying_symbol: "TEST".to_string(),
            instrument_kind: kind,
            exchange_segment: ExchangeSegment::NseFno,
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
            strike_price: 0.0,
            option_type: None,
            lot_size: 25,
            tick_size: 0.05,
            symbol_name: "T".to_string(),
            display_name: "T".to_string(),
        };

        for kind in [
            DhanInstrumentKind::FutureIndex,
            DhanInstrumentKind::FutureStock,
            DhanInstrumentKind::OptionIndex,
            DhanInstrumentKind::OptionStock,
        ] {
            let contract = make_contract(kind);
            let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&contract).unwrap();
            let archived =
                rkyv::access::<ArchivedDerivativeContract, rkyv::rancor::Error>(&bytes).unwrap();
            assert_eq!(DhanInstrumentKind::from(&archived.instrument_kind), kind);
        }
    }

    #[test]
    fn test_archived_index_category_from_all_variants() {
        let make_index = |cat: IndexCategory| SubscribedIndex {
            symbol: "TEST".to_string(),
            security_id: 13,
            exchange: Exchange::NationalStockExchange,
            category: cat,
            subcategory: IndexSubcategory::Fno,
        };

        for cat in [IndexCategory::FnoUnderlying, IndexCategory::DisplayIndex] {
            let idx = make_index(cat);
            let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&idx).unwrap();
            let archived =
                rkyv::access::<ArchivedSubscribedIndex, rkyv::rancor::Error>(&bytes).unwrap();
            assert_eq!(IndexCategory::from(&archived.category), cat);
        }
    }

    // =====================================================================
    // Error type Display + Error impl tests
    // =====================================================================

    #[test]
    fn test_naive_date_from_archived_i32_valid() {
        let date = chrono::NaiveDate::from_ymd_opt(2026, 3, 20).unwrap();
        let days = rkyv::rend::i32_le::from_native(date.num_days_from_ce());
        let result = naive_date_from_archived_i32(&days);
        assert_eq!(result, date);
    }

    // =====================================================================
    // ConstituencyBuildMetadata::default test
    // =====================================================================

    #[test]
    fn test_constituency_build_metadata_default() {
        let meta = ConstituencyBuildMetadata::default();
        assert_eq!(meta.indices_downloaded, 0);
        assert_eq!(meta.indices_failed, 0);
        assert_eq!(meta.unique_stocks, 0);
        assert_eq!(meta.total_mappings, 0);
        assert_eq!(meta.download_duration, std::time::Duration::default());
        // build_timestamp should be IST (UTC+5:30)
        assert_eq!(meta.build_timestamp.offset().local_minus_utc(), 19_800);
    }

    // =====================================================================
    // IndexSubcategory Display for all variants
    // =====================================================================

    #[test]
    fn test_index_subcategory_display_all_variants() {
        assert_eq!(format!("{}", IndexSubcategory::Volatility), "Volatility");
        assert_eq!(format!("{}", IndexSubcategory::BroadMarket), "BroadMarket");
        assert_eq!(format!("{}", IndexSubcategory::MidCap), "MidCap");
        assert_eq!(format!("{}", IndexSubcategory::SmallCap), "SmallCap");
        assert_eq!(format!("{}", IndexSubcategory::Sectoral), "Sectoral");
        assert_eq!(format!("{}", IndexSubcategory::Thematic), "Thematic");
        assert_eq!(format!("{}", IndexSubcategory::Fno), "Fno");
    }

    // =====================================================================
    // UnderlyingKind Display for BseIndex (was uncovered)
    // =====================================================================

    #[test]
    fn test_underlying_kind_display_bse_index() {
        assert_eq!(format!("{}", UnderlyingKind::BseIndex), "BseIndex");
    }

    // =====================================================================
    // DhanInstrumentKind Display for all variants
    // =====================================================================

    #[test]
    fn test_dhan_instrument_kind_display_all_variants() {
        assert_eq!(
            format!("{}", DhanInstrumentKind::FutureIndex),
            "FutureIndex"
        );
        assert_eq!(
            format!("{}", DhanInstrumentKind::FutureStock),
            "FutureStock"
        );
        assert_eq!(
            format!("{}", DhanInstrumentKind::OptionIndex),
            "OptionIndex"
        );
        assert_eq!(
            format!("{}", DhanInstrumentKind::OptionStock),
            "OptionStock"
        );
    }

    // =====================================================================
    // FnoUniverse rkyv roundtrip (exercises all nested rkyv wrappers)
    // =====================================================================

    #[test]
    fn test_fno_universe_rkyv_full_roundtrip() {
        let original = make_test_fno_universe();
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&original).unwrap();
        let archived = rkyv::access::<ArchivedFnoUniverse, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: FnoUniverse =
            rkyv::deserialize::<FnoUniverse, rkyv::rancor::Error>(archived).unwrap();

        assert_eq!(deserialized.underlyings.len(), original.underlyings.len());
        assert_eq!(
            deserialized.derivative_contracts.len(),
            original.derivative_contracts.len()
        );
        assert_eq!(
            deserialized.subscribed_indices.len(),
            original.subscribed_indices.len()
        );

        // Verify a derivative contract survived the roundtrip including NaiveDate
        let contract = deserialized.derivative_contracts.get(&49001).unwrap();
        assert_eq!(
            contract.expiry_date,
            chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap()
        );
        assert_eq!(contract.underlying_symbol, "RELIANCE");
    }

    // =====================================================================
    // IndexConstituencyMap tests
    // =====================================================================

    #[test]
    fn test_index_constituency_map_forward_lookup() {
        let mut map = IndexConstituencyMap::default();
        let constituent = IndexConstituent {
            index_name: "Nifty 50".to_string(),
            symbol: "RELIANCE".to_string(),
            isin: "INE002A01018".to_string(),
            weight: 10.5,
            sector: "Oil & Gas".to_string(),
            last_updated: chrono::NaiveDate::from_ymd_opt(2026, 3, 20).unwrap(),
        };
        map.index_to_constituents
            .insert("Nifty 50".to_string(), vec![constituent]);
        assert_eq!(map.get_constituents("Nifty 50").unwrap().len(), 1);
        assert!(map.get_constituents("Nifty Bank").is_none());
    }

    #[test]
    fn test_index_constituency_map_reverse_lookup() {
        let mut map = IndexConstituencyMap::default();
        map.stock_to_indices.insert(
            "RELIANCE".to_string(),
            vec!["Nifty 50".to_string(), "Nifty 100".to_string()],
        );
        assert_eq!(map.get_indices_for_stock("RELIANCE").unwrap().len(), 2);
        assert!(map.get_indices_for_stock("TCS").is_none());
    }

    #[test]
    fn test_index_constituency_map_counts_and_membership() {
        let mut map = IndexConstituencyMap::default();
        map.index_to_constituents
            .insert("Nifty 50".to_string(), vec![]);
        map.index_to_constituents
            .insert("Nifty Bank".to_string(), vec![]);
        map.stock_to_indices.insert("RELIANCE".to_string(), vec![]);
        assert_eq!(map.index_count(), 2);
        assert_eq!(map.stock_count(), 1);
        assert!(map.contains_index("Nifty 50"));
        assert!(!map.contains_index("Nifty Auto"));
        assert!(map.contains_stock("RELIANCE"));
        assert!(!map.contains_stock("TCS"));
    }

    #[test]
    fn test_index_constituency_map_get_constituents_returns_slice() {
        let mut map = IndexConstituencyMap::default();
        let c1 = IndexConstituent {
            index_name: "Nifty 50".to_string(),
            symbol: "RELIANCE".to_string(),
            isin: "INE002A01018".to_string(),
            weight: 10.5,
            sector: "Oil & Gas".to_string(),
            last_updated: chrono::NaiveDate::from_ymd_opt(2026, 3, 20).unwrap(),
        };
        let c2 = IndexConstituent {
            index_name: "Nifty 50".to_string(),
            symbol: "TCS".to_string(),
            isin: "INE467B01029".to_string(),
            weight: 5.0,
            sector: "IT".to_string(),
            last_updated: chrono::NaiveDate::from_ymd_opt(2026, 3, 20).unwrap(),
        };
        map.index_to_constituents
            .insert("Nifty 50".to_string(), vec![c1, c2]);
        let constituents = map.get_constituents("Nifty 50").unwrap();
        assert_eq!(constituents.len(), 2);
        assert_eq!(constituents[0].symbol, "RELIANCE");
        assert_eq!(constituents[1].symbol, "TCS");
    }

    #[test]
    fn test_index_constituency_map_get_indices_for_stock_returns_slice() {
        let mut map = IndexConstituencyMap::default();
        map.stock_to_indices.insert(
            "HDFCBANK".to_string(),
            vec![
                "Nifty 50".to_string(),
                "Nifty Bank".to_string(),
                "Nifty 100".to_string(),
            ],
        );
        let indices = map.get_indices_for_stock("HDFCBANK").unwrap();
        assert_eq!(indices.len(), 3);
    }

    #[test]
    fn test_index_constituency_map_all_index_names_sorted() {
        let mut map = IndexConstituencyMap::default();
        map.index_to_constituents
            .insert("Nifty Bank".to_string(), vec![]);
        map.index_to_constituents
            .insert("Nifty 50".to_string(), vec![]);
        map.index_to_constituents
            .insert("Nifty Auto".to_string(), vec![]);
        let names = map.all_index_names();
        assert_eq!(names, vec!["Nifty 50", "Nifty Auto", "Nifty Bank"]);
    }

    // =====================================================================
    // Additional coverage tests — NaiveDateRkyvError, DateTimeRkyvError,
    // duration_serde, rkyv wrappers, OptionChain roundtrip, ExpiryCalendar
    // =====================================================================

    #[test]
    fn test_naive_date_rkyv_error_display() {
        let err = NaiveDateRkyvError;
        let msg = format!("{err}");
        assert!(msg.contains("invalid NaiveDate"));
    }

    #[test]
    fn test_naive_date_rkyv_error_is_std_error() {
        let err: &dyn std::error::Error = &NaiveDateRkyvError;
        assert!(err.to_string().contains("NaiveDate"));
    }

    #[test]
    fn test_datetime_rkyv_error_display() {
        let err = DateTimeRkyvError;
        let msg = format!("{err}");
        assert!(msg.contains("invalid DateTime"));
    }

    #[test]
    fn test_datetime_rkyv_error_is_std_error() {
        let err: &dyn std::error::Error = &DateTimeRkyvError;
        assert!(err.to_string().contains("DateTime"));
    }

    #[test]
    fn test_duration_serde_zero() {
        let ist = crate::compat::trading_calendar::ist_offset();
        let metadata = UniverseBuildMetadata {
            csv_source: "test".to_string(),
            csv_row_count: 0,
            parsed_row_count: 0,
            index_count: 0,
            equity_count: 0,
            underlying_count: 0,
            derivative_count: 0,
            option_chain_count: 0,
            build_duration: std::time::Duration::ZERO,
            build_timestamp: chrono::Utc::now().with_timezone(&ist),
        };
        let json = serde_json::to_string(&metadata).unwrap();
        let deserialized: UniverseBuildMetadata = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.build_duration, std::time::Duration::ZERO);
    }

    #[test]
    fn test_duration_serde_large_value() {
        let ist = crate::compat::trading_calendar::ist_offset();
        let dur = std::time::Duration::from_millis(u64::MAX / 2);
        let metadata = UniverseBuildMetadata {
            csv_source: "test".to_string(),
            csv_row_count: 0,
            parsed_row_count: 0,
            index_count: 0,
            equity_count: 0,
            underlying_count: 0,
            derivative_count: 0,
            option_chain_count: 0,
            build_duration: dur,
            build_timestamp: chrono::Utc::now().with_timezone(&ist),
        };
        let json = serde_json::to_string(&metadata).unwrap();
        let deserialized: UniverseBuildMetadata = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.build_duration, dur);
    }

    #[test]
    fn test_option_chain_rkyv_roundtrip() {
        let chain = OptionChain {
            underlying_symbol: "NIFTY".to_string(),
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
            calls: vec![OptionChainEntry {
                security_id: 49001,
                strike_price: 24000.0,
                lot_size: 75,
            }],
            puts: vec![OptionChainEntry {
                security_id: 49002,
                strike_price: 24000.0,
                lot_size: 75,
            }],
            future_security_id: Some(50001),
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&chain).unwrap();
        let archived = rkyv::access::<ArchivedOptionChain, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: OptionChain =
            rkyv::deserialize::<OptionChain, rkyv::rancor::Error>(archived).unwrap();
        assert_eq!(deserialized.underlying_symbol, "NIFTY");
        assert_eq!(deserialized.calls.len(), 1);
        assert_eq!(deserialized.puts.len(), 1);
        assert_eq!(deserialized.future_security_id, Some(50001));
        assert_eq!(
            deserialized.expiry_date,
            chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap()
        );
    }

    #[test]
    fn test_expiry_calendar_rkyv_roundtrip() {
        let cal = ExpiryCalendar {
            underlying_symbol: "BANKNIFTY".to_string(),
            expiry_dates: vec![
                chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
                chrono::NaiveDate::from_ymd_opt(2026, 4, 30).unwrap(),
            ],
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&cal).unwrap();
        let archived = rkyv::access::<ArchivedExpiryCalendar, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: ExpiryCalendar =
            rkyv::deserialize::<ExpiryCalendar, rkyv::rancor::Error>(archived).unwrap();
        assert_eq!(deserialized.underlying_symbol, "BANKNIFTY");
        assert_eq!(deserialized.expiry_dates.len(), 2);
        assert_eq!(
            deserialized.expiry_dates[0],
            chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap()
        );
    }

    #[test]
    fn test_index_constituency_map_empty_default() {
        let map = IndexConstituencyMap::default();
        assert_eq!(map.index_count(), 0);
        assert_eq!(map.stock_count(), 0);
        assert!(map.all_index_names().is_empty());
        assert!(!map.contains_stock("RELIANCE"));
        assert!(!map.contains_index("Nifty 50"));
        assert!(map.get_constituents("Nifty 50").is_none());
        assert!(map.get_indices_for_stock("RELIANCE").is_none());
    }

    #[test]
    fn test_instrument_info_index_variant_rkyv() {
        use crate::compat::types::Exchange;
        let info = InstrumentInfo::Index {
            security_id: 13,
            symbol: "NIFTY".to_string(),
            exchange: Exchange::NationalStockExchange,
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&info).unwrap();
        let archived = rkyv::access::<ArchivedInstrumentInfo, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: InstrumentInfo =
            rkyv::deserialize::<InstrumentInfo, rkyv::rancor::Error>(archived).unwrap();
        assert!(matches!(
            deserialized,
            InstrumentInfo::Index {
                security_id: 13,
                ref symbol,
                ..
            } if symbol == "NIFTY"
        ));
    }

    #[test]
    fn test_instrument_info_equity_variant_rkyv() {
        let info = InstrumentInfo::Equity {
            security_id: 2885,
            symbol: "RELIANCE".to_string(),
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&info).unwrap();
        let archived = rkyv::access::<ArchivedInstrumentInfo, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: InstrumentInfo =
            rkyv::deserialize::<InstrumentInfo, rkyv::rancor::Error>(archived).unwrap();
        assert!(matches!(
            deserialized,
            InstrumentInfo::Equity {
                security_id: 2885,
                ref symbol,
            } if symbol == "RELIANCE"
        ));
    }

    #[test]
    fn test_index_constituent_serde_roundtrip() {
        let constituent = IndexConstituent {
            index_name: "Nifty 50".to_string(),
            symbol: "RELIANCE".to_string(),
            isin: "INE002A01018".to_string(),
            weight: 10.5,
            sector: "Oil & Gas".to_string(),
            last_updated: chrono::NaiveDate::from_ymd_opt(2026, 3, 20).unwrap(),
        };
        let json = serde_json::to_string(&constituent).unwrap();
        let deserialized: IndexConstituent = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.symbol, "RELIANCE");
        assert_eq!(deserialized.index_name, "Nifty 50");
        assert!((deserialized.weight - 10.5).abs() < f64::EPSILON);
        assert_eq!(deserialized.isin, "INE002A01018");
        assert_eq!(deserialized.sector, "Oil & Gas");
        assert_eq!(
            deserialized.last_updated,
            chrono::NaiveDate::from_ymd_opt(2026, 3, 20).unwrap()
        );
    }

    #[test]
    fn test_index_constituent_debug() {
        let constituent = IndexConstituent {
            index_name: "Nifty 50".to_string(),
            symbol: "TCS".to_string(),
            isin: "INE467B01029".to_string(),
            weight: 5.0,
            sector: "IT".to_string(),
            last_updated: chrono::NaiveDate::from_ymd_opt(2026, 3, 20).unwrap(),
        };
        let debug = format!("{:?}", constituent);
        assert!(debug.contains("IndexConstituent"));
        assert!(debug.contains("TCS"));
    }

    #[test]
    fn test_index_constituency_map_serde_roundtrip() {
        let mut map = IndexConstituencyMap::default();
        let constituent = IndexConstituent {
            index_name: "Nifty 50".to_string(),
            symbol: "RELIANCE".to_string(),
            isin: "INE002A01018".to_string(),
            weight: 10.5,
            sector: "Oil & Gas".to_string(),
            last_updated: chrono::NaiveDate::from_ymd_opt(2026, 3, 20).unwrap(),
        };
        map.index_to_constituents
            .insert("Nifty 50".to_string(), vec![constituent]);
        map.stock_to_indices
            .insert("RELIANCE".to_string(), vec!["Nifty 50".to_string()]);

        let json = serde_json::to_string(&map).unwrap();
        let deserialized: IndexConstituencyMap = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.index_count(), 1);
        assert_eq!(deserialized.stock_count(), 1);
        assert!(deserialized.contains_stock("RELIANCE"));
        assert!(deserialized.contains_index("Nifty 50"));
    }

    #[test]
    fn test_fno_underlying_serde_roundtrip() {
        let underlying = FnoUnderlying {
            underlying_symbol: "NIFTY".to_string(),
            underlying_security_id: 26000,
            price_feed_security_id: 13,
            price_feed_segment: ExchangeSegment::IdxI,
            derivative_segment: ExchangeSegment::NseFno,
            kind: UnderlyingKind::NseIndex,
            lot_size: 25,
            contract_count: 100,
        };
        let json = serde_json::to_string(&underlying).unwrap();
        let deserialized: FnoUnderlying = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.underlying_symbol, "NIFTY");
        assert_eq!(deserialized.lot_size, 25);
        assert_eq!(deserialized.contract_count, 100);
    }

    #[test]
    fn test_derivative_contract_serde_roundtrip() {
        let contract = DerivativeContract {
            security_id: 49001,
            underlying_symbol: "NIFTY".to_string(),
            instrument_kind: DhanInstrumentKind::OptionIndex,
            exchange_segment: ExchangeSegment::NseFno,
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
            strike_price: 24000.0,
            option_type: Some(crate::compat::types::OptionType::Call),
            lot_size: 75,
            tick_size: 0.05,
            symbol_name: "NIFTY-27MAR26-24000-CE".to_string(),
            display_name: "NIFTY 24000 CE Mar26".to_string(),
        };
        let json = serde_json::to_string(&contract).unwrap();
        let deserialized: DerivativeContract = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.security_id, 49001);
        assert!((deserialized.strike_price - 24000.0).abs() < f64::EPSILON);
        assert_eq!(
            deserialized.option_type,
            Some(crate::compat::types::OptionType::Call)
        );
    }

    #[test]
    fn test_option_chain_serde_roundtrip() {
        let chain = OptionChain {
            underlying_symbol: "BANKNIFTY".to_string(),
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 4, 30).unwrap(),
            calls: vec![OptionChainEntry {
                security_id: 55001,
                strike_price: 48000.0,
                lot_size: 15,
            }],
            puts: vec![OptionChainEntry {
                security_id: 55002,
                strike_price: 48000.0,
                lot_size: 15,
            }],
            future_security_id: Some(55000),
        };
        let json = serde_json::to_string(&chain).unwrap();
        let deserialized: OptionChain = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.underlying_symbol, "BANKNIFTY");
        assert_eq!(deserialized.calls.len(), 1);
        assert_eq!(deserialized.puts.len(), 1);
        assert_eq!(deserialized.future_security_id, Some(55000));
    }

    #[test]
    fn test_option_chain_entry_debug() {
        let entry = OptionChainEntry {
            security_id: 55001,
            strike_price: 48000.0,
            lot_size: 15,
        };
        let debug = format!("{:?}", entry);
        assert!(debug.contains("OptionChainEntry"));
    }

    #[test]
    fn test_expiry_calendar_serde_roundtrip() {
        let cal = ExpiryCalendar {
            underlying_symbol: "NIFTY".to_string(),
            expiry_dates: vec![
                chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
                chrono::NaiveDate::from_ymd_opt(2026, 4, 30).unwrap(),
            ],
        };
        let json = serde_json::to_string(&cal).unwrap();
        let deserialized: ExpiryCalendar = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.underlying_symbol, "NIFTY");
        assert_eq!(deserialized.expiry_dates.len(), 2);
    }

    #[test]
    fn test_subscribed_index_serde_roundtrip() {
        use crate::compat::types::Exchange;
        let idx = SubscribedIndex {
            symbol: "INDIA VIX".to_string(),
            security_id: 21,
            exchange: Exchange::NationalStockExchange,
            category: IndexCategory::DisplayIndex,
            subcategory: IndexSubcategory::Volatility,
        };
        let json = serde_json::to_string(&idx).unwrap();
        let deserialized: SubscribedIndex = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.symbol, "INDIA VIX");
        assert_eq!(deserialized.security_id, 21);
    }

    #[test]
    fn test_instrument_info_derivative_serde_roundtrip() {
        let info = InstrumentInfo::Derivative {
            security_id: 49001,
            underlying_symbol: "NIFTY".to_string(),
            instrument_kind: DhanInstrumentKind::OptionIndex,
            exchange_segment: ExchangeSegment::NseFno,
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
            strike_price: 24000.0,
            option_type: Some(crate::compat::types::OptionType::Call),
        };
        let json = serde_json::to_string(&info).unwrap();
        let deserialized: InstrumentInfo = serde_json::from_str(&json).unwrap();
        assert!(matches!(
            deserialized,
            InstrumentInfo::Derivative {
                security_id: 49001,
                ..
            }
        ));
    }

    #[test]
    fn test_option_chain_key_serde_roundtrip() {
        let key = OptionChainKey {
            underlying_symbol: "NIFTY".to_string(),
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
        };
        let json = serde_json::to_string(&key).unwrap();
        let deserialized: OptionChainKey = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.underlying_symbol, "NIFTY");
        assert_eq!(
            deserialized.expiry_date,
            chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap()
        );
    }

    #[test]
    fn test_constituency_build_metadata_serde_roundtrip() {
        let meta = ConstituencyBuildMetadata::default();
        let json = serde_json::to_string(&meta).unwrap();
        let deserialized: ConstituencyBuildMetadata = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.indices_downloaded, 0);
        assert_eq!(deserialized.indices_failed, 0);
        assert_eq!(
            deserialized.download_duration,
            std::time::Duration::default()
        );
    }

    #[test]
    fn test_subscribed_index_rkyv_roundtrip() {
        use crate::compat::types::Exchange;
        let idx = SubscribedIndex {
            symbol: "INDIA VIX".to_string(),
            security_id: 26017,
            exchange: Exchange::NationalStockExchange,
            category: IndexCategory::DisplayIndex,
            subcategory: IndexSubcategory::Volatility,
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&idx).unwrap();
        let archived =
            rkyv::access::<ArchivedSubscribedIndex, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: SubscribedIndex =
            rkyv::deserialize::<SubscribedIndex, rkyv::rancor::Error>(archived).unwrap();
        assert_eq!(deserialized.symbol, "INDIA VIX");
        assert_eq!(deserialized.security_id, 26017);
    }

    // =====================================================================
    // Coverage: duration_serde::deserialize (line 532)
    // =====================================================================

    #[test]
    fn test_duration_serde_deserialize_roundtrip() {
        // Exercises duration_serde::deserialize by round-tripping through JSON
        let ist = crate::compat::trading_calendar::ist_offset();
        let original = UniverseBuildMetadata {
            csv_source: "test".to_string(),
            csv_row_count: 1,
            parsed_row_count: 1,
            index_count: 0,
            equity_count: 0,
            underlying_count: 0,
            derivative_count: 0,
            option_chain_count: 0,
            build_duration: std::time::Duration::from_millis(4567),
            build_timestamp: chrono::Utc::now().with_timezone(&ist),
        };
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: UniverseBuildMetadata = serde_json::from_str(&json).unwrap();
        assert_eq!(
            deserialized.build_duration,
            std::time::Duration::from_millis(4567)
        );
    }

    #[test]
    fn test_duration_serde_deserialize_invalid_type_fails() {
        // Exercises the error path of duration_serde::deserialize (line 532 `?`)
        // by providing a string instead of a u64 for build_duration.
        let json = r#"{
            "csv_source": "test",
            "csv_row_count": 0,
            "parsed_row_count": 0,
            "index_count": 0,
            "equity_count": 0,
            "underlying_count": 0,
            "derivative_count": 0,
            "option_chain_count": 0,
            "build_duration": "not_a_number",
            "build_timestamp": "2026-01-01T00:00:00+05:30"
        }"#;
        let result: Result<UniverseBuildMetadata, _> = serde_json::from_str(json);
        assert!(result.is_err());
    }

    // =====================================================================
    // Coverage: NaiveDateAsI32 deserialization error path (line 578)
    // =====================================================================

    #[test]
    fn test_naive_date_as_i32_invalid_days_from_ce() {
        // Forge an rkyv buffer with an invalid days-from-CE value that
        // NaiveDate::from_num_days_from_ce_opt rejects (i32::MIN).
        let original = DerivativeContract {
            security_id: 1,
            underlying_symbol: "X".to_string(),
            instrument_kind: DhanInstrumentKind::FutureIndex,
            exchange_segment: ExchangeSegment::NseFno,
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
            strike_price: 0.0,
            option_type: None,
            lot_size: 1,
            tick_size: 0.05,
            symbol_name: "X".to_string(),
            display_name: "X".to_string(),
        };
        let mut bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&original)
            .unwrap()
            .to_vec();

        // The expiry_date field is archived as an i32_le.
        // Corrupt it with i32::MIN which is invalid for NaiveDate.
        let valid_days = chrono::NaiveDate::from_ymd_opt(2026, 3, 27)
            .unwrap()
            .num_days_from_ce();
        let valid_bytes = valid_days.to_le_bytes();

        let mut found = false;
        for i in 0..bytes.len().saturating_sub(3) {
            if bytes[i..i + 4] == valid_bytes {
                let invalid_bytes = i32::MIN.to_le_bytes();
                bytes[i..i + 4].copy_from_slice(&invalid_bytes);
                found = true;
                break;
            }
        }
        assert!(found, "could not find date bytes in archive");

        let archived =
            rkyv::access::<ArchivedDerivativeContract, rkyv::rancor::Error>(&bytes).unwrap();
        let result = rkyv::deserialize::<DerivativeContract, rkyv::rancor::Error>(archived);
        assert!(
            result.is_err(),
            "expected error for invalid NaiveDate days-from-CE"
        );
    }

    // =====================================================================
    // Coverage: DateTimeFixedOffsetAsI64 error paths (lines 653-658)
    // =====================================================================

    #[test]
    fn test_datetime_fixed_offset_as_i64_invalid_offset() {
        // Forge bytes with an invalid UTC offset (> 86400 seconds) to trigger
        // FixedOffset::east_opt returning None (line 655).
        let ist = crate::compat::trading_calendar::ist_offset();
        let original = UniverseBuildMetadata {
            csv_source: "t".to_string(),
            csv_row_count: 0,
            parsed_row_count: 0,
            index_count: 0,
            equity_count: 0,
            underlying_count: 0,
            derivative_count: 0,
            option_chain_count: 0,
            build_duration: std::time::Duration::from_millis(0),
            build_timestamp: chrono::Utc::now().with_timezone(&ist),
        };
        let mut bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&original)
            .unwrap()
            .to_vec();

        // The offset is 19800 (IST = +5:30). Corrupt to invalid value.
        let valid_offset = 19800_i32.to_le_bytes();
        let invalid_offset = 999_999_i32.to_le_bytes();

        let mut found = false;
        for i in 0..bytes.len().saturating_sub(3) {
            if bytes[i..i + 4] == valid_offset {
                bytes[i..i + 4].copy_from_slice(&invalid_offset);
                found = true;
                break;
            }
        }
        assert!(found, "could not find offset bytes in archive");

        let archived =
            rkyv::access::<ArchivedUniverseBuildMetadata, rkyv::rancor::Error>(&bytes).unwrap();
        let result = rkyv::deserialize::<UniverseBuildMetadata, rkyv::rancor::Error>(archived);
        assert!(result.is_err(), "expected error for invalid UTC offset");
    }

    #[test]
    fn test_datetime_fixed_offset_as_i64_invalid_timestamp() {
        // Forge bytes with an invalid timestamp to trigger
        // DateTime::from_timestamp returning None (line 657-658).
        let ist = crate::compat::trading_calendar::ist_offset();
        let original = UniverseBuildMetadata {
            csv_source: "t".to_string(),
            csv_row_count: 0,
            parsed_row_count: 0,
            index_count: 0,
            equity_count: 0,
            underlying_count: 0,
            derivative_count: 0,
            option_chain_count: 0,
            build_duration: std::time::Duration::from_millis(0),
            build_timestamp: chrono::Utc::now().with_timezone(&ist),
        };
        let mut bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&original)
            .unwrap()
            .to_vec();

        // The timestamp_secs is an i64. Replace with i64::MAX.
        let ts = original.build_timestamp.timestamp();
        let ts_bytes = ts.to_le_bytes();
        let invalid_ts = i64::MAX.to_le_bytes();

        let mut found = false;
        for i in 0..bytes.len().saturating_sub(7) {
            if bytes[i..i + 8] == ts_bytes {
                bytes[i..i + 8].copy_from_slice(&invalid_ts);
                found = true;
                break;
            }
        }
        assert!(found, "could not find timestamp bytes in archive");

        let archived =
            rkyv::access::<ArchivedUniverseBuildMetadata, rkyv::rancor::Error>(&bytes).unwrap();
        let result = rkyv::deserialize::<UniverseBuildMetadata, rkyv::rancor::Error>(archived);
        assert!(result.is_err(), "expected error for invalid timestamp");
    }

    // =====================================================================
    // Additional coverage: rkyv roundtrips for edge cases
    // =====================================================================

    #[test]
    fn test_rkyv_naive_date_boundary_dates_roundtrip() {
        // Test boundary dates: epoch start, far future, leap day
        let dates = [
            chrono::NaiveDate::from_ymd_opt(1970, 1, 1).unwrap(),
            chrono::NaiveDate::from_ymd_opt(2099, 12, 31).unwrap(),
            chrono::NaiveDate::from_ymd_opt(2024, 2, 29).unwrap(), // leap day
            chrono::NaiveDate::from_ymd_opt(2000, 1, 1).unwrap(),
        ];
        for date in dates {
            let contract = DerivativeContract {
                security_id: 1,
                underlying_symbol: "TEST".to_string(),
                instrument_kind: DhanInstrumentKind::FutureIndex,
                exchange_segment: ExchangeSegment::NseFno,
                expiry_date: date,
                strike_price: 0.0,
                option_type: None,
                lot_size: 25,
                tick_size: 0.05,
                symbol_name: "T".to_string(),
                display_name: "T".to_string(),
            };
            let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&contract).unwrap();
            let archived =
                rkyv::access::<ArchivedDerivativeContract, rkyv::rancor::Error>(&bytes).unwrap();
            let deserialized: DerivativeContract =
                rkyv::deserialize::<DerivativeContract, rkyv::rancor::Error>(archived).unwrap();
            assert_eq!(deserialized.expiry_date, date);
        }
    }

    #[test]
    fn test_rkyv_datetime_fixed_offset_utc_roundtrip() {
        // Test with UTC offset (0 seconds) instead of IST
        let utc = chrono::FixedOffset::east_opt(0).unwrap();
        let original = UniverseBuildMetadata {
            csv_source: "utc-test".to_string(),
            csv_row_count: 1,
            parsed_row_count: 1,
            index_count: 0,
            equity_count: 0,
            underlying_count: 0,
            derivative_count: 0,
            option_chain_count: 0,
            build_duration: std::time::Duration::from_millis(100),
            build_timestamp: chrono::Utc::now().with_timezone(&utc),
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&original).unwrap();
        let archived =
            rkyv::access::<ArchivedUniverseBuildMetadata, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: UniverseBuildMetadata =
            rkyv::deserialize::<UniverseBuildMetadata, rkyv::rancor::Error>(archived).unwrap();
        assert_eq!(
            deserialized.build_timestamp.offset().local_minus_utc(),
            0,
            "UTC offset should be 0"
        );
        assert_eq!(
            deserialized.build_timestamp.timestamp(),
            original.build_timestamp.timestamp()
        );
    }

    #[test]
    fn test_rkyv_datetime_fixed_offset_negative_offset_roundtrip() {
        // Test with negative UTC offset (e.g., US Eastern = -5:00 = -18000)
        let offset = chrono::FixedOffset::west_opt(18_000).unwrap();
        let original = UniverseBuildMetadata {
            csv_source: "neg-offset-test".to_string(),
            csv_row_count: 0,
            parsed_row_count: 0,
            index_count: 0,
            equity_count: 0,
            underlying_count: 0,
            derivative_count: 0,
            option_chain_count: 0,
            build_duration: std::time::Duration::from_millis(50),
            build_timestamp: chrono::Utc::now().with_timezone(&offset),
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&original).unwrap();
        let archived =
            rkyv::access::<ArchivedUniverseBuildMetadata, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: UniverseBuildMetadata =
            rkyv::deserialize::<UniverseBuildMetadata, rkyv::rancor::Error>(archived).unwrap();
        assert_eq!(
            deserialized.build_timestamp.offset().local_minus_utc(),
            -18_000,
            "negative UTC offset should roundtrip"
        );
    }

    #[test]
    fn test_rkyv_duration_as_millis_zero_roundtrip() {
        let ist = crate::compat::trading_calendar::ist_offset();
        let original = UniverseBuildMetadata {
            csv_source: "dur-zero".to_string(),
            csv_row_count: 0,
            parsed_row_count: 0,
            index_count: 0,
            equity_count: 0,
            underlying_count: 0,
            derivative_count: 0,
            option_chain_count: 0,
            build_duration: std::time::Duration::ZERO,
            build_timestamp: chrono::Utc::now().with_timezone(&ist),
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&original).unwrap();
        let archived =
            rkyv::access::<ArchivedUniverseBuildMetadata, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: UniverseBuildMetadata =
            rkyv::deserialize::<UniverseBuildMetadata, rkyv::rancor::Error>(archived).unwrap();
        assert_eq!(deserialized.build_duration, std::time::Duration::ZERO);
    }

    #[test]
    fn test_rkyv_duration_as_millis_large_roundtrip() {
        let ist = crate::compat::trading_calendar::ist_offset();
        // 24 hours in milliseconds
        let dur = std::time::Duration::from_millis(86_400_000);
        let original = UniverseBuildMetadata {
            csv_source: "dur-large".to_string(),
            csv_row_count: 0,
            parsed_row_count: 0,
            index_count: 0,
            equity_count: 0,
            underlying_count: 0,
            derivative_count: 0,
            option_chain_count: 0,
            build_duration: dur,
            build_timestamp: chrono::Utc::now().with_timezone(&ist),
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&original).unwrap();
        let archived =
            rkyv::access::<ArchivedUniverseBuildMetadata, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: UniverseBuildMetadata =
            rkyv::deserialize::<UniverseBuildMetadata, rkyv::rancor::Error>(archived).unwrap();
        assert_eq!(deserialized.build_duration, dur);
    }

    // =====================================================================
    // Additional coverage: InstrumentInfo::Derivative rkyv roundtrip
    // =====================================================================

    #[test]
    fn test_instrument_info_derivative_variant_rkyv() {
        use crate::compat::types::OptionType;
        let info = InstrumentInfo::Derivative {
            security_id: 49001,
            underlying_symbol: "RELIANCE".to_string(),
            instrument_kind: DhanInstrumentKind::OptionStock,
            exchange_segment: ExchangeSegment::NseFno,
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
            strike_price: 3000.0,
            option_type: Some(OptionType::Call),
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&info).unwrap();
        let archived = rkyv::access::<ArchivedInstrumentInfo, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: InstrumentInfo =
            rkyv::deserialize::<InstrumentInfo, rkyv::rancor::Error>(archived).unwrap();
        assert!(matches!(
            deserialized,
            InstrumentInfo::Derivative {
                security_id: 49001,
                ref underlying_symbol,
                instrument_kind: DhanInstrumentKind::OptionStock,
                exchange_segment: ExchangeSegment::NseFno,
                option_type: Some(OptionType::Call),
                ..
            } if underlying_symbol == "RELIANCE"
        ));
    }

    // =====================================================================
    // Additional coverage: OptionChainKey rkyv roundtrip
    // =====================================================================

    #[test]
    fn test_option_chain_key_rkyv_roundtrip() {
        let key = OptionChainKey {
            underlying_symbol: "BANKNIFTY".to_string(),
            expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 4, 30).unwrap(),
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&key).unwrap();
        let archived = rkyv::access::<ArchivedOptionChainKey, rkyv::rancor::Error>(&bytes).unwrap();
        let deserialized: OptionChainKey =
            rkyv::deserialize::<OptionChainKey, rkyv::rancor::Error>(archived).unwrap();
        assert_eq!(deserialized.underlying_symbol, "BANKNIFTY");
        assert_eq!(
            deserialized.expiry_date,
            chrono::NaiveDate::from_ymd_opt(2026, 4, 30).unwrap()
        );
    }

    // =====================================================================
    // Additional coverage: Display impls — exhaustive variant coverage
    // =====================================================================

    #[test]
    fn test_underlying_kind_display_all_variants_exhaustive() {
        // Ensure every variant produces distinct non-empty output
        let variants = [
            (UnderlyingKind::NseIndex, "NseIndex"),
            (UnderlyingKind::BseIndex, "BseIndex"),
            (UnderlyingKind::Stock, "Stock"),
        ];
        for (variant, expected) in variants {
            let display = format!("{variant}");
            assert_eq!(display, expected);
            assert!(!display.is_empty());
        }
    }

    #[test]
    fn test_dhan_instrument_kind_display_matches_as_str() {
        // Verify Display output matches as_str() for all variants
        let variants = [
            DhanInstrumentKind::FutureIndex,
            DhanInstrumentKind::FutureStock,
            DhanInstrumentKind::OptionIndex,
            DhanInstrumentKind::OptionStock,
        ];
        for variant in variants {
            assert_eq!(format!("{variant}"), variant.as_str());
        }
    }

    #[test]
    fn test_index_category_display_matches_as_str() {
        let variants = [IndexCategory::FnoUnderlying, IndexCategory::DisplayIndex];
        for variant in variants {
            assert_eq!(format!("{variant}"), variant.as_str());
        }
    }

    #[test]
    fn test_index_subcategory_display_matches_as_str() {
        let variants = [
            IndexSubcategory::Volatility,
            IndexSubcategory::BroadMarket,
            IndexSubcategory::MidCap,
            IndexSubcategory::SmallCap,
            IndexSubcategory::Sectoral,
            IndexSubcategory::Thematic,
            IndexSubcategory::Fno,
        ];
        for variant in variants {
            assert_eq!(format!("{variant}"), variant.as_str());
        }
    }

    // =====================================================================
    // Additional coverage: Error Display and Error trait impls
    // =====================================================================

    #[test]
    fn test_naive_date_rkyv_error_display_exact_message() {
        let err = NaiveDateRkyvError;
        assert_eq!(
            err.to_string(),
            "invalid NaiveDate days-from-CE value in rkyv archive"
        );
    }

    #[test]
    fn test_datetime_rkyv_error_display_exact_message() {
        let err = DateTimeRkyvError;
        assert_eq!(
            err.to_string(),
            "invalid DateTime<FixedOffset> value in rkyv archive"
        );
    }

    #[test]
    fn test_naive_date_rkyv_error_source_is_none() {
        use std::error::Error;
        let err = NaiveDateRkyvError;
        assert!(err.source().is_none());
    }

    #[test]
    fn test_datetime_rkyv_error_source_is_none() {
        use std::error::Error;
        let err = DateTimeRkyvError;
        assert!(err.source().is_none());
    }

    // =====================================================================
    // Additional coverage: ConstituencyBuildMetadata::default
    // =====================================================================

    #[test]
    fn test_constituency_build_metadata_default_timestamp_is_recent() {
        let before = chrono::Utc::now();
        let meta = ConstituencyBuildMetadata::default();
        let after = chrono::Utc::now();
        // The default timestamp should be between before and after (in UTC terms)
        let meta_utc = meta.build_timestamp.timestamp();
        assert!(meta_utc >= before.timestamp());
        assert!(meta_utc <= after.timestamp());
    }

    // =====================================================================
    // Additional coverage: FnoUniverse::derivative_security_ids_for_symbol
    // =====================================================================

    #[test]
    fn test_derivative_security_ids_for_symbol_filters_correctly() {
        let u = make_test_fno_universe();

        // RELIANCE should get exactly 2 contracts (49001 future + 49002 option)
        let mut reliance_ids = u.derivative_security_ids_for_symbol("RELIANCE");
        reliance_ids.sort();
        assert_eq!(reliance_ids.len(), 2);
        assert_eq!(reliance_ids, vec![49001, 49002]);

        // NIFTY should get exactly 1 contract (50001 future)
        let nifty_ids = u.derivative_security_ids_for_symbol("NIFTY");
        assert_eq!(nifty_ids.len(), 1);
        assert_eq!(nifty_ids[0], 50001);

        // HDFCBANK has an underlying but no derivative contracts in the test data
        let hdfc_ids = u.derivative_security_ids_for_symbol("HDFCBANK");
        assert!(hdfc_ids.is_empty());

        // Unknown symbol returns empty
        let unknown_ids = u.derivative_security_ids_for_symbol("TATAMOTORS");
        assert!(unknown_ids.is_empty());

        // Empty string returns empty
        let empty_ids = u.derivative_security_ids_for_symbol("");
        assert!(empty_ids.is_empty());
    }

    #[test]
    fn test_derivative_security_ids_for_symbol_empty_universe() {
        let u = FnoUniverse {
            underlyings: HashMap::new(),
            derivative_contracts: HashMap::new(),
            instrument_info: HashMap::new(),
            option_chains: HashMap::new(),
            expiry_calendars: HashMap::new(),
            subscribed_indices: Vec::new(),
            build_metadata: make_test_build_metadata(),
        };
        assert!(u.derivative_security_ids_for_symbol("RELIANCE").is_empty());
    }

    // =====================================================================
    // Additional coverage: naive_date_from_archived_i32 edge cases
    // =====================================================================

    #[test]
    fn test_naive_date_from_archived_i32_multiple_dates() {
        let dates = [
            chrono::NaiveDate::from_ymd_opt(2026, 1, 1).unwrap(),
            chrono::NaiveDate::from_ymd_opt(1970, 1, 1).unwrap(),
            chrono::NaiveDate::from_ymd_opt(2024, 2, 29).unwrap(), // leap day
        ];
        for date in dates {
            let days = rkyv::rend::i32_le::from_native(date.num_days_from_ce());
            let result = naive_date_from_archived_i32(&days);
            assert_eq!(result, date);
        }
    }

    // =====================================================================
    // Additional coverage: IndexConstituencyMap rich data tests
    // =====================================================================

    /// Helper to build an `IndexConstituencyMap` with realistic multi-index,
    /// multi-stock data for thorough method coverage.
    fn make_test_constituency_map() -> IndexConstituencyMap {
        let today = chrono::NaiveDate::from_ymd_opt(2026, 3, 22).unwrap();
        let reliance_n50 = IndexConstituent {
            index_name: "Nifty 50".to_string(),
            symbol: "RELIANCE".to_string(),
            isin: "INE002A01018".to_string(),
            weight: 10.5,
            sector: "Oil & Gas".to_string(),
            last_updated: today,
        };
        let hdfc_n50 = IndexConstituent {
            index_name: "Nifty 50".to_string(),
            symbol: "HDFCBANK".to_string(),
            isin: "INE040A01034".to_string(),
            weight: 8.2,
            sector: "Banking".to_string(),
            last_updated: today,
        };
        let hdfc_bank = IndexConstituent {
            index_name: "Nifty Bank".to_string(),
            symbol: "HDFCBANK".to_string(),
            isin: "INE040A01034".to_string(),
            weight: 25.0,
            sector: "Banking".to_string(),
            last_updated: today,
        };

        let mut map = IndexConstituencyMap::default();
        map.index_to_constituents
            .insert("Nifty 50".to_string(), vec![reliance_n50, hdfc_n50]);
        map.index_to_constituents
            .insert("Nifty Bank".to_string(), vec![hdfc_bank]);
        map.stock_to_indices
            .insert("RELIANCE".to_string(), vec!["Nifty 50".to_string()]);
        map.stock_to_indices.insert(
            "HDFCBANK".to_string(),
            vec!["Nifty 50".to_string(), "Nifty Bank".to_string()],
        );
        map
    }

    #[test]
    fn test_constituency_map_get_constituents_data_integrity() {
        let map = make_test_constituency_map();
        let n50 = map.get_constituents("Nifty 50").unwrap();
        assert_eq!(n50.len(), 2);
        assert_eq!(n50[0].symbol, "RELIANCE");
        assert_eq!(n50[0].isin, "INE002A01018");
        assert!(n50[0].weight > 10.0);
        assert_eq!(n50[1].symbol, "HDFCBANK");

        let bank = map.get_constituents("Nifty Bank").unwrap();
        assert_eq!(bank.len(), 1);
        assert_eq!(bank[0].symbol, "HDFCBANK");
        assert!(bank[0].weight > 20.0);
    }

    #[test]
    fn test_constituency_map_get_constituents_missing_returns_none() {
        let map = make_test_constituency_map();
        assert!(map.get_constituents("Nifty IT").is_none());
        assert!(map.get_constituents("").is_none());
    }

    #[test]
    fn test_constituency_map_reverse_lookup_multi_index_stock() {
        let map = make_test_constituency_map();
        let hdfc_indices = map.get_indices_for_stock("HDFCBANK").unwrap();
        assert_eq!(hdfc_indices.len(), 2);
        assert!(hdfc_indices.contains(&"Nifty 50".to_string()));
        assert!(hdfc_indices.contains(&"Nifty Bank".to_string()));
    }

    #[test]
    fn test_constituency_map_reverse_lookup_single_index_stock() {
        let map = make_test_constituency_map();
        let reliance_indices = map.get_indices_for_stock("RELIANCE").unwrap();
        assert_eq!(reliance_indices.len(), 1);
        assert_eq!(reliance_indices[0], "Nifty 50");
    }

    #[test]
    fn test_constituency_map_reverse_lookup_missing_returns_none() {
        let map = make_test_constituency_map();
        assert!(map.get_indices_for_stock("TCS").is_none());
        assert!(map.get_indices_for_stock("").is_none());
    }

    #[test]
    fn test_constituency_map_counts_with_data() {
        let map = make_test_constituency_map();
        assert_eq!(map.index_count(), 2);
        assert_eq!(map.stock_count(), 2);
    }

    #[test]
    fn test_constituency_map_all_index_names_with_data() {
        let map = make_test_constituency_map();
        let names = map.all_index_names();
        assert_eq!(names.len(), 2);
        // Must be sorted alphabetically
        assert_eq!(names[0], "Nifty 50");
        assert_eq!(names[1], "Nifty Bank");
    }

    #[test]
    fn test_constituency_map_contains_checks() {
        let map = make_test_constituency_map();
        assert!(map.contains_stock("RELIANCE"));
        assert!(map.contains_stock("HDFCBANK"));
        assert!(!map.contains_stock("INFY"));
        assert!(map.contains_index("Nifty 50"));
        assert!(map.contains_index("Nifty Bank"));
        assert!(!map.contains_index("Nifty IT"));
    }

    // =====================================================================
    // Additional coverage: ConstituencyBuildMetadata non-default fields
    // =====================================================================

    #[test]
    fn test_constituency_build_metadata_with_populated_fields() {
        let ist = crate::compat::trading_calendar::ist_offset();
        let meta = ConstituencyBuildMetadata {
            download_duration: std::time::Duration::from_millis(500),
            indices_downloaded: 5,
            indices_failed: 1,
            unique_stocks: 200,
            total_mappings: 350,
            build_timestamp: chrono::Utc::now().with_timezone(&ist),
        };
        assert_eq!(meta.indices_downloaded, 5);
        assert_eq!(meta.indices_failed, 1);
        assert_eq!(meta.unique_stocks, 200);
        assert_eq!(meta.total_mappings, 350);
        assert_eq!(
            meta.download_duration,
            std::time::Duration::from_millis(500)
        );
    }

    // =====================================================================
    // Additional coverage: IndexConstituent field access
    // =====================================================================

    #[test]
    fn test_index_constituent_all_fields() {
        let date = chrono::NaiveDate::from_ymd_opt(2026, 3, 22).unwrap();
        let constituent = IndexConstituent {
            index_name: "Nifty 50".to_string(),
            symbol: "RELIANCE".to_string(),
            isin: "INE002A01018".to_string(),
            weight: 10.5,
            sector: "Oil & Gas".to_string(),
            last_updated: date,
        };
        assert_eq!(constituent.index_name, "Nifty 50");
        assert_eq!(constituent.symbol, "RELIANCE");
        assert_eq!(constituent.isin, "INE002A01018");
        assert!((constituent.weight - 10.5).abs() < f64::EPSILON);
        assert_eq!(constituent.sector, "Oil & Gas");
        assert_eq!(constituent.last_updated, date);
    }

    #[test]
    fn test_index_constituent_zero_weight() {
        let constituent = IndexConstituent {
            index_name: "Test".to_string(),
            symbol: "TEST".to_string(),
            isin: "INE000000000".to_string(),
            weight: 0.0,
            sector: "Unknown".to_string(),
            last_updated: chrono::NaiveDate::from_ymd_opt(2026, 1, 1).unwrap(),
        };
        assert!((constituent.weight - 0.0).abs() < f64::EPSILON);
    }

    // =====================================================================
    // Additional coverage: enum Clone, Copy, PartialEq, Eq, Hash traits
    // =====================================================================

    #[test]
    fn test_underlying_kind_clone_and_copy() {
        let kind = UnderlyingKind::NseIndex;
        let cloned = kind;
        let copied = kind;
        assert_eq!(kind, cloned);
        assert_eq!(kind, copied);
    }

    #[test]
    fn test_dhan_instrument_kind_clone_and_copy() {
        let kind = DhanInstrumentKind::OptionIndex;
        let cloned = kind;
        let copied = kind;
        assert_eq!(kind, cloned);
        assert_eq!(kind, copied);
    }

    #[test]
    fn test_index_category_clone_and_copy() {
        let cat = IndexCategory::FnoUnderlying;
        let cloned = cat;
        let copied = cat;
        assert_eq!(cat, cloned);
        assert_eq!(cat, copied);
    }

    #[test]
    fn test_index_subcategory_clone_and_copy() {
        let sub = IndexSubcategory::Sectoral;
        let cloned = sub;
        let copied = sub;
        assert_eq!(sub, cloned);
        assert_eq!(sub, copied);
    }

    #[test]
    fn test_underlying_kind_hash_all_distinct() {
        use std::collections::HashSet;
        let mut set = HashSet::new();
        set.insert(UnderlyingKind::NseIndex);
        set.insert(UnderlyingKind::BseIndex);
        set.insert(UnderlyingKind::Stock);
        assert_eq!(set.len(), 3);
    }

    #[test]
    fn test_dhan_instrument_kind_hash_all_distinct() {
        use std::collections::HashSet;
        let mut set = HashSet::new();
        set.insert(DhanInstrumentKind::FutureIndex);
        set.insert(DhanInstrumentKind::FutureStock);
        set.insert(DhanInstrumentKind::OptionIndex);
        set.insert(DhanInstrumentKind::OptionStock);
        assert_eq!(set.len(), 4);
    }

    #[test]
    fn test_index_category_hash_all_distinct() {
        use std::collections::HashSet;
        let mut set = HashSet::new();
        set.insert(IndexCategory::FnoUnderlying);
        set.insert(IndexCategory::DisplayIndex);
        assert_eq!(set.len(), 2);
    }

    #[test]
    fn test_index_subcategory_hash_all_distinct() {
        use std::collections::HashSet;
        let mut set = HashSet::new();
        set.insert(IndexSubcategory::Volatility);
        set.insert(IndexSubcategory::BroadMarket);
        set.insert(IndexSubcategory::MidCap);
        set.insert(IndexSubcategory::SmallCap);
        set.insert(IndexSubcategory::Sectoral);
        set.insert(IndexSubcategory::Thematic);
        set.insert(IndexSubcategory::Fno);
        assert_eq!(set.len(), 7);
    }

    // =====================================================================
    // Additional coverage: IndexConstituencyMap serde roundtrip
    // =====================================================================

    #[test]
    fn test_index_constituency_map_serde_roundtrip_multi_index() {
        let map = make_test_constituency_map();
        let json = serde_json::to_string(&map).unwrap();
        let deserialized: IndexConstituencyMap = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.index_count(), 2);
        assert_eq!(deserialized.stock_count(), 2);
        assert!(deserialized.contains_stock("RELIANCE"));
        assert!(deserialized.contains_index("Nifty Bank"));
        let n50 = deserialized.get_constituents("Nifty 50").unwrap();
        assert_eq!(n50.len(), 2);
    }

    // =====================================================================
    // Additional coverage: SubscribedIndex field access
    // =====================================================================

    #[test]
    fn test_subscribed_index_display_index_variant() {
        let idx = SubscribedIndex {
            symbol: "INDIA VIX".to_string(),
            security_id: 26017,
            exchange: Exchange::NationalStockExchange,
            category: IndexCategory::DisplayIndex,
            subcategory: IndexSubcategory::Volatility,
        };
        assert_eq!(idx.category, IndexCategory::DisplayIndex);
        assert_eq!(idx.subcategory, IndexSubcategory::Volatility);
        assert_eq!(idx.symbol, "INDIA VIX");
    }

    #[test]
    fn test_subscribed_index_fno_underlying_variant() {
        let idx = SubscribedIndex {
            symbol: "NIFTY".to_string(),
            security_id: 13,
            exchange: Exchange::NationalStockExchange,
            category: IndexCategory::FnoUnderlying,
            subcategory: IndexSubcategory::Fno,
        };
        assert_eq!(idx.category, IndexCategory::FnoUnderlying);
        assert_eq!(idx.subcategory, IndexSubcategory::Fno);
    }

    // =====================================================================
    // Additional coverage: FnoUniverse methods — edge cases
    // =====================================================================

    #[test]
    fn test_fno_universe_index_symbol_to_security_id_case_sensitive() {
        let u = make_test_fno_universe();
        assert!(u.index_symbol_to_security_id("nifty").is_none());
        assert!(u.index_symbol_to_security_id("Nifty").is_none());
        assert!(u.index_symbol_to_security_id("NIFTY").is_some());
    }

    #[test]
    fn test_fno_universe_index_symbol_to_security_id_empty_string() {
        let u = make_test_fno_universe();
        assert!(u.index_symbol_to_security_id("").is_none());
    }

    #[test]
    fn test_fno_universe_security_id_to_symbol_all_types() {
        let u = make_test_fno_universe();
        // Index
        assert_eq!(u.security_id_to_symbol(13), Some("NIFTY"));
        // Equity
        assert_eq!(u.security_id_to_symbol(2885), Some("RELIANCE"));
        assert_eq!(u.security_id_to_symbol(1333), Some("HDFCBANK"));
        // Derivative (returns underlying symbol)
        assert_eq!(u.security_id_to_symbol(49001), Some("RELIANCE"));
        // Unknown
        assert!(u.security_id_to_symbol(0).is_none());
        assert!(u.security_id_to_symbol(u32::MAX).is_none());
    }

    #[test]
    fn test_fno_universe_get_underlying_all_entries() {
        let u = make_test_fno_universe();
        let nifty = u.get_underlying("NIFTY").unwrap();
        assert_eq!(nifty.kind, UnderlyingKind::NseIndex);
        assert_eq!(nifty.lot_size, 25);
        assert_eq!(nifty.price_feed_segment, ExchangeSegment::IdxI);

        let reliance = u.get_underlying("RELIANCE").unwrap();
        assert_eq!(reliance.kind, UnderlyingKind::Stock);
        assert_eq!(reliance.price_feed_segment, ExchangeSegment::NseEquity);

        let hdfc = u.get_underlying("HDFCBANK").unwrap();
        assert_eq!(hdfc.lot_size, 550);
    }

    // -----------------------------------------------------------------------
    // rkyv roundtrip tests — exercises NaiveDateAsI32, DurationAsMillis,
    // and DateTimeFixedOffset serialize/deserialize impls.
    // -----------------------------------------------------------------------

    #[test]
    fn test_rkyv_roundtrip_derivative_contract() {
        // Exercises NaiveDateAsI32 serialize_with (line 564) and
        // deserialize_with for NaiveDate fields.
        let contract = DerivativeContract {
            security_id: 49081,
            underlying_symbol: "NIFTY".to_string(),
            instrument_kind: DhanInstrumentKind::OptionIndex,
            exchange_segment: ExchangeSegment::NseFno,
            expiry_date: NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
            strike_price: 23500.0,
            option_type: Some(crate::compat::types::OptionType::Call),
            lot_size: 25,
            tick_size: 0.05,
            symbol_name: "NIFTY-Mar2026-23500-CE".to_string(),
            display_name: "NIFTY 27MAR 23500 CE".to_string(),
        };

        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&contract).unwrap();
        let archived = rkyv::access::<
            <DerivativeContract as rkyv::Archive>::Archived,
            rkyv::rancor::Error,
        >(&bytes)
        .unwrap();
        let deserialized: DerivativeContract =
            rkyv::deserialize::<DerivativeContract, rkyv::rancor::Error>(archived).unwrap();

        assert_eq!(deserialized.security_id, 49081);
        assert_eq!(
            deserialized.expiry_date,
            NaiveDate::from_ymd_opt(2026, 3, 27).unwrap()
        );
        assert_eq!(deserialized.strike_price, 23500.0);
        assert_eq!(deserialized.lot_size, 25);
    }

    #[test]
    fn test_rkyv_roundtrip_universe_build_metadata() {
        use std::time::Duration;
        // Exercises DurationAsMillis serialize_with (line 699) and
        // DateTimeFixedOffsetAsI64 serialize/deserialize for DateTime fields.
        let ist = chrono::FixedOffset::east_opt(19800).unwrap();
        let meta = UniverseBuildMetadata {
            csv_source: "https://example.com/scrip.csv".to_string(),
            csv_row_count: 100_000,
            parsed_row_count: 80_000,
            index_count: 10,
            equity_count: 2000,
            underlying_count: 50,
            derivative_count: 5000,
            option_chain_count: 200,
            build_duration: Duration::from_millis(1234),
            build_timestamp: chrono::Utc::now().with_timezone(&ist),
        };

        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&meta).unwrap();
        let archived = rkyv::access::<
            <UniverseBuildMetadata as rkyv::Archive>::Archived,
            rkyv::rancor::Error,
        >(&bytes)
        .unwrap();
        let deserialized: UniverseBuildMetadata =
            rkyv::deserialize::<UniverseBuildMetadata, rkyv::rancor::Error>(archived).unwrap();

        assert_eq!(deserialized.csv_row_count, 100_000);
        assert_eq!(deserialized.derivative_count, 5000);
        assert_eq!(deserialized.build_duration, Duration::from_millis(1234));
    }

    // -----------------------------------------------------------------------
    // ExpiryCode tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_expiry_code_serialize_to_u8() {
        assert_eq!(ExpiryCode::Current.as_u8(), 0);
        assert_eq!(ExpiryCode::Next.as_u8(), 1);
        assert_eq!(ExpiryCode::Far.as_u8(), 2);
    }

    #[test]
    fn test_expiry_code_all_variants() {
        let current = ExpiryCode::from_u8(0).unwrap();
        let next = ExpiryCode::from_u8(1).unwrap();
        let far = ExpiryCode::from_u8(2).unwrap();
        assert_eq!(current, ExpiryCode::Current);
        assert_eq!(next, ExpiryCode::Next);
        assert_eq!(far, ExpiryCode::Far);
    }

    #[test]
    fn test_expiry_code_from_u8_invalid_rejected() {
        assert!(ExpiryCode::from_u8(3).is_none());
        assert!(ExpiryCode::from_u8(255).is_none());
    }

    #[test]
    fn test_expiry_code_roundtrip() {
        for val in 0..=2 {
            let code = ExpiryCode::from_u8(val).unwrap();
            assert_eq!(code.as_u8(), val);
        }
    }

    #[test]
    fn test_expiry_code_display() {
        assert_eq!(ExpiryCode::Current.to_string(), "Current");
        assert_eq!(ExpiryCode::Next.to_string(), "Next");
        assert_eq!(ExpiryCode::Far.to_string(), "Far");
    }
}
