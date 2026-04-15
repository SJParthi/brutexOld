//! Core domain types shared across all crates.
//!
//! These types represent the fundamental building blocks of the trading system.
//! They are used by every downstream module — WebSocket, parser, OMS, storage.

use std::fmt;

use serde::{Deserialize, Serialize};

/// Exchange identifier.
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
pub enum Exchange {
    /// National Stock Exchange of India.
    NationalStockExchange,
    /// Bombay Stock Exchange.
    BombayStockExchange,
}

impl Exchange {
    /// Returns the canonical string representation for storage and display.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::NationalStockExchange => "NSE",
            Self::BombayStockExchange => "BSE",
        }
    }
}

impl fmt::Display for Exchange {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Exchange segment for subscription and routing.
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
pub enum ExchangeSegment {
    /// Index segment (both NSE and BSE indices).
    IdxI,
    /// NSE Equity segment.
    NseEquity,
    /// NSE Futures & Options segment.
    NseFno,
    /// NSE Currency derivatives segment.
    NseCurrency,
    /// BSE Equity segment.
    BseEquity,
    /// MCX Commodity segment.
    McxComm,
    /// BSE Currency derivatives segment.
    BseCurrency,
    /// BSE Futures & Options segment.
    BseFno,
}

impl ExchangeSegment {
    /// Returns the canonical string representation for storage and display.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::IdxI => "IDX_I",
            Self::NseEquity => "NSE_EQ",
            Self::NseFno => "NSE_FNO",
            Self::NseCurrency => "NSE_CURRENCY",
            Self::BseEquity => "BSE_EQ",
            Self::McxComm => "MCX_COMM",
            Self::BseCurrency => "BSE_CURRENCY",
            Self::BseFno => "BSE_FNO",
        }
    }

    /// Returns the binary wire code used in Dhan WebSocket V2 protocol.
    ///
    /// Codes sourced from Dhan API (Python SDK ref): IDX_I=0, NSE_EQ=1, NSE_FNO=2,
    /// NSE_CURRENCY=3, BSE_EQ=4, MCX_COMM=5, BSE_CURRENCY=7, BSE_FNO=8.
    pub fn binary_code(&self) -> u8 {
        match self {
            Self::IdxI => 0,
            Self::NseEquity => 1,
            Self::NseFno => 2,
            Self::NseCurrency => 3,
            Self::BseEquity => 4,
            Self::McxComm => 5,
            Self::BseCurrency => 7,
            Self::BseFno => 8,
        }
    }

    /// Converts a binary wire code (from WebSocket packet header byte 3) to an `ExchangeSegment`.
    ///
    /// Returns `None` for unknown values. Note: there is NO enum 6 in Dhan's mapping
    /// (gap between MCX_COMM=5 and BSE_CURRENCY=7). Unknown codes must not panic.
    pub fn from_byte(code: u8) -> Option<Self> {
        match code {
            0 => Some(Self::IdxI),
            1 => Some(Self::NseEquity),
            2 => Some(Self::NseFno),
            3 => Some(Self::NseCurrency),
            4 => Some(Self::BseEquity),
            5 => Some(Self::McxComm),
            7 => Some(Self::BseCurrency),
            8 => Some(Self::BseFno),
            _ => None, // includes gap at 6
        }
    }
}

impl fmt::Display for ExchangeSegment {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// WebSocket feed mode determining the data granularity.
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
pub enum FeedMode {
    /// Compact price data (16 bytes: header + LTP + LTT).
    Ticker,
    /// Price + volume + OHLC (50 bytes).
    Quote,
    /// Full: quote + OI + 5-level market depth (162 bytes).
    Full,
}

impl FeedMode {
    /// Returns the canonical string representation for storage and display.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Ticker => "Ticker",
            Self::Quote => "Quote",
            Self::Full => "Full",
        }
    }
}

impl fmt::Display for FeedMode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Instrument type classification.
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
pub enum InstrumentType {
    /// Index (e.g., NIFTY, BANKNIFTY).
    Index,
    /// Equity stock (e.g., RELIANCE, HDFCBANK).
    Equity,
    /// Futures contract.
    Future,
    /// Options contract.
    Option,
}

impl InstrumentType {
    /// Returns the canonical string representation for storage and display.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Index => "Index",
            Self::Equity => "Equity",
            Self::Future => "Future",
            Self::Option => "Option",
        }
    }

    /// Returns the Dhan REST API instrument type string for historical data requests.
    ///
    /// Dhan API uses specific instrument strings (e.g., "FUTIDX", "OPTIDX")
    /// that differ from our internal classification. The `is_index_underlying`
    /// flag disambiguates index vs stock derivatives.
    pub fn dhan_api_instrument_type(&self, is_index_underlying: bool) -> &'static str {
        match (self, is_index_underlying) {
            (Self::Index, _) => "INDEX",
            (Self::Equity, _) => "EQUITY",
            (Self::Future, true) => "FUTIDX",
            (Self::Future, false) => "FUTSTK",
            (Self::Option, true) => "OPTIDX",
            (Self::Option, false) => "OPTSTK",
        }
    }
}

impl fmt::Display for InstrumentType {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Option type for derivatives.
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
pub enum OptionType {
    /// Call option.
    Call,
    /// Put option.
    Put,
}

impl OptionType {
    /// Returns the canonical string representation for storage and display.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Call => "CE",
            Self::Put => "PE",
        }
    }
}

impl fmt::Display for OptionType {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

// ---------------------------------------------------------------------------
// Archived → Owned Conversions (for zero-copy rkyv access)
// ---------------------------------------------------------------------------

impl From<&ArchivedExchange> for Exchange {
    fn from(archived: &ArchivedExchange) -> Self {
        match archived {
            ArchivedExchange::NationalStockExchange => Self::NationalStockExchange,
            ArchivedExchange::BombayStockExchange => Self::BombayStockExchange,
        }
    }
}

impl From<&ArchivedExchangeSegment> for ExchangeSegment {
    fn from(archived: &ArchivedExchangeSegment) -> Self {
        match archived {
            ArchivedExchangeSegment::IdxI => Self::IdxI,
            ArchivedExchangeSegment::NseEquity => Self::NseEquity,
            ArchivedExchangeSegment::NseFno => Self::NseFno,
            ArchivedExchangeSegment::NseCurrency => Self::NseCurrency,
            ArchivedExchangeSegment::BseEquity => Self::BseEquity,
            ArchivedExchangeSegment::McxComm => Self::McxComm,
            ArchivedExchangeSegment::BseCurrency => Self::BseCurrency,
            ArchivedExchangeSegment::BseFno => Self::BseFno,
        }
    }
}

impl From<&ArchivedOptionType> for OptionType {
    fn from(archived: &ArchivedOptionType) -> Self {
        match archived {
            ArchivedOptionType::Call => Self::Call,
            ArchivedOptionType::Put => Self::Put,
        }
    }
}

/// Unique identifier for a security in the Dhan system.
pub type SecurityId = u32;

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // --- Exchange ---

    #[test]
    fn test_exchange_as_str() {
        assert_eq!(Exchange::NationalStockExchange.as_str(), "NSE");
        assert_eq!(Exchange::BombayStockExchange.as_str(), "BSE");
    }

    #[test]
    fn test_exchange_display() {
        assert_eq!(format!("{}", Exchange::NationalStockExchange), "NSE");
        assert_eq!(format!("{}", Exchange::BombayStockExchange), "BSE");
    }

    #[test]
    fn test_exchange_debug() {
        let debug = format!("{:?}", Exchange::NationalStockExchange);
        assert!(debug.contains("NationalStockExchange"));
    }

    #[test]
    fn test_exchange_clone_eq() {
        let a = Exchange::NationalStockExchange;
        let b = a;
        assert_eq!(a, b);
    }

    #[test]
    fn test_exchange_serde_roundtrip() {
        let original = Exchange::NationalStockExchange;
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: Exchange = serde_json::from_str(&json).unwrap();
        assert_eq!(original, deserialized);
    }

    // --- ExchangeSegment ---

    #[test]
    fn test_exchange_segment_as_str_all_variants() {
        assert_eq!(ExchangeSegment::IdxI.as_str(), "IDX_I");
        assert_eq!(ExchangeSegment::NseEquity.as_str(), "NSE_EQ");
        assert_eq!(ExchangeSegment::NseFno.as_str(), "NSE_FNO");
        assert_eq!(ExchangeSegment::NseCurrency.as_str(), "NSE_CURRENCY");
        assert_eq!(ExchangeSegment::BseEquity.as_str(), "BSE_EQ");
        assert_eq!(ExchangeSegment::McxComm.as_str(), "MCX_COMM");
        assert_eq!(ExchangeSegment::BseCurrency.as_str(), "BSE_CURRENCY");
        assert_eq!(ExchangeSegment::BseFno.as_str(), "BSE_FNO");
    }

    #[test]
    fn test_exchange_segment_display() {
        assert_eq!(format!("{}", ExchangeSegment::NseFno), "NSE_FNO");
        assert_eq!(format!("{}", ExchangeSegment::BseFno), "BSE_FNO");
    }

    #[test]
    fn test_exchange_segment_serde_roundtrip() {
        let original = ExchangeSegment::McxComm;
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: ExchangeSegment = serde_json::from_str(&json).unwrap();
        assert_eq!(original, deserialized);
    }

    // --- FeedMode ---

    #[test]
    fn test_feed_mode_as_str_all_variants() {
        assert_eq!(FeedMode::Ticker.as_str(), "Ticker");
        assert_eq!(FeedMode::Quote.as_str(), "Quote");
        assert_eq!(FeedMode::Full.as_str(), "Full");
    }

    #[test]
    fn test_feed_mode_display() {
        assert_eq!(format!("{}", FeedMode::Ticker), "Ticker");
        assert_eq!(format!("{}", FeedMode::Full), "Full");
    }

    #[test]
    fn test_feed_mode_serde_roundtrip() {
        let original = FeedMode::Quote;
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: FeedMode = serde_json::from_str(&json).unwrap();
        assert_eq!(original, deserialized);
    }

    // --- InstrumentType ---

    #[test]
    fn test_instrument_type_as_str_all_variants() {
        assert_eq!(InstrumentType::Index.as_str(), "Index");
        assert_eq!(InstrumentType::Equity.as_str(), "Equity");
        assert_eq!(InstrumentType::Future.as_str(), "Future");
        assert_eq!(InstrumentType::Option.as_str(), "Option");
    }

    #[test]
    fn test_instrument_type_display() {
        assert_eq!(format!("{}", InstrumentType::Future), "Future");
        assert_eq!(format!("{}", InstrumentType::Option), "Option");
    }

    #[test]
    fn test_instrument_type_serde_roundtrip() {
        let original = InstrumentType::Equity;
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: InstrumentType = serde_json::from_str(&json).unwrap();
        assert_eq!(original, deserialized);
    }

    // --- OptionType ---

    #[test]
    fn test_option_type_as_str() {
        assert_eq!(OptionType::Call.as_str(), "CE");
        assert_eq!(OptionType::Put.as_str(), "PE");
    }

    #[test]
    fn test_option_type_display() {
        assert_eq!(format!("{}", OptionType::Call), "CE");
        assert_eq!(format!("{}", OptionType::Put), "PE");
    }

    #[test]
    fn test_option_type_serde_roundtrip() {
        let original = OptionType::Put;
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: OptionType = serde_json::from_str(&json).unwrap();
        assert_eq!(original, deserialized);
    }

    // --- binary_code correctness (must match constants.rs) ---

    #[test]
    fn test_exchange_segment_binary_code_matches_constants() {
        use crate::compat::constants::{
            EXCHANGE_SEGMENT_BSE_CURRENCY, EXCHANGE_SEGMENT_BSE_EQ, EXCHANGE_SEGMENT_BSE_FNO,
            EXCHANGE_SEGMENT_IDX_I, EXCHANGE_SEGMENT_MCX_COMM, EXCHANGE_SEGMENT_NSE_CURRENCY,
            EXCHANGE_SEGMENT_NSE_EQ, EXCHANGE_SEGMENT_NSE_FNO,
        };

        assert_eq!(ExchangeSegment::IdxI.binary_code(), EXCHANGE_SEGMENT_IDX_I);
        assert_eq!(
            ExchangeSegment::NseEquity.binary_code(),
            EXCHANGE_SEGMENT_NSE_EQ
        );
        assert_eq!(
            ExchangeSegment::NseFno.binary_code(),
            EXCHANGE_SEGMENT_NSE_FNO
        );
        assert_eq!(
            ExchangeSegment::NseCurrency.binary_code(),
            EXCHANGE_SEGMENT_NSE_CURRENCY
        );
        assert_eq!(
            ExchangeSegment::BseEquity.binary_code(),
            EXCHANGE_SEGMENT_BSE_EQ
        );
        assert_eq!(
            ExchangeSegment::McxComm.binary_code(),
            EXCHANGE_SEGMENT_MCX_COMM
        );
        assert_eq!(
            ExchangeSegment::BseCurrency.binary_code(),
            EXCHANGE_SEGMENT_BSE_CURRENCY
        );
        assert_eq!(
            ExchangeSegment::BseFno.binary_code(),
            EXCHANGE_SEGMENT_BSE_FNO
        );
    }

    // --- from_byte (reverse mapping) ---

    #[test]
    fn test_from_byte_all_known_codes() {
        assert_eq!(ExchangeSegment::from_byte(0), Some(ExchangeSegment::IdxI));
        assert_eq!(
            ExchangeSegment::from_byte(1),
            Some(ExchangeSegment::NseEquity)
        );
        assert_eq!(ExchangeSegment::from_byte(2), Some(ExchangeSegment::NseFno));
        assert_eq!(
            ExchangeSegment::from_byte(3),
            Some(ExchangeSegment::NseCurrency)
        );
        assert_eq!(
            ExchangeSegment::from_byte(4),
            Some(ExchangeSegment::BseEquity)
        );
        assert_eq!(
            ExchangeSegment::from_byte(5),
            Some(ExchangeSegment::McxComm)
        );
        assert_eq!(
            ExchangeSegment::from_byte(7),
            Some(ExchangeSegment::BseCurrency)
        );
        assert_eq!(ExchangeSegment::from_byte(8), Some(ExchangeSegment::BseFno));
    }

    #[test]
    fn test_from_byte_gap_at_6_returns_none() {
        assert_eq!(ExchangeSegment::from_byte(6), None);
    }

    #[test]
    fn test_from_byte_unknown_returns_none() {
        assert_eq!(ExchangeSegment::from_byte(9), None);
        assert_eq!(ExchangeSegment::from_byte(255), None);
    }

    #[test]
    fn test_from_byte_roundtrips_with_binary_code() {
        let segments = [
            ExchangeSegment::IdxI,
            ExchangeSegment::NseEquity,
            ExchangeSegment::NseFno,
            ExchangeSegment::NseCurrency,
            ExchangeSegment::BseEquity,
            ExchangeSegment::McxComm,
            ExchangeSegment::BseCurrency,
            ExchangeSegment::BseFno,
        ];
        for seg in segments {
            assert_eq!(
                ExchangeSegment::from_byte(seg.binary_code()),
                Some(seg),
                "from_byte(binary_code()) must roundtrip for {:?}",
                seg
            );
        }
    }

    // --- Hash consistency (used as HashMap keys) ---

    #[test]
    fn test_exchange_segment_hash_consistency() {
        use std::collections::HashMap;
        let mut map = HashMap::new();
        map.insert(ExchangeSegment::NseFno, "nse_fno");
        map.insert(ExchangeSegment::BseFno, "bse_fno");
        assert_eq!(map.get(&ExchangeSegment::NseFno), Some(&"nse_fno"));
        assert_eq!(map.get(&ExchangeSegment::BseFno), Some(&"bse_fno"));
        assert_eq!(map.get(&ExchangeSegment::IdxI), None);
    }

    // --- InstrumentType::dhan_api_instrument_type ---

    #[test]
    fn test_dhan_api_instrument_type_index() {
        assert_eq!(
            InstrumentType::Index.dhan_api_instrument_type(true),
            "INDEX"
        );
        assert_eq!(
            InstrumentType::Index.dhan_api_instrument_type(false),
            "INDEX"
        );
    }

    #[test]
    fn test_dhan_api_instrument_type_equity() {
        assert_eq!(
            InstrumentType::Equity.dhan_api_instrument_type(true),
            "EQUITY"
        );
        assert_eq!(
            InstrumentType::Equity.dhan_api_instrument_type(false),
            "EQUITY"
        );
    }

    #[test]
    fn test_dhan_api_instrument_type_futidx() {
        assert_eq!(
            InstrumentType::Future.dhan_api_instrument_type(true),
            "FUTIDX"
        );
    }

    #[test]
    fn test_dhan_api_instrument_type_futstk() {
        assert_eq!(
            InstrumentType::Future.dhan_api_instrument_type(false),
            "FUTSTK"
        );
    }

    #[test]
    fn test_dhan_api_instrument_type_optidx() {
        assert_eq!(
            InstrumentType::Option.dhan_api_instrument_type(true),
            "OPTIDX"
        );
    }

    #[test]
    fn test_dhan_api_instrument_type_optstk() {
        assert_eq!(
            InstrumentType::Option.dhan_api_instrument_type(false),
            "OPTSTK"
        );
    }

    // --- Archived From conversions ---

    #[test]
    fn test_archived_exchange_from_roundtrip() {
        use crate::compat::instrument_types::{FnoUnderlying, UnderlyingKind};
        // Use a struct that contains Exchange to get ArchivedExchange
        let underlying = FnoUnderlying {
            underlying_symbol: "TEST".to_string(),
            underlying_security_id: 1,
            price_feed_security_id: 2,
            price_feed_segment: ExchangeSegment::NseEquity,
            derivative_segment: ExchangeSegment::NseFno,
            kind: UnderlyingKind::Stock,
            lot_size: 100,
            contract_count: 0,
        };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&underlying).unwrap();
        let archived = rkyv::access::<
            crate::compat::instrument_types::ArchivedFnoUnderlying,
            rkyv::rancor::Error,
        >(&bytes)
        .unwrap();
        assert_eq!(
            ExchangeSegment::from(&archived.price_feed_segment),
            ExchangeSegment::NseEquity
        );
        assert_eq!(
            ExchangeSegment::from(&archived.derivative_segment),
            ExchangeSegment::NseFno
        );
    }

    #[test]
    fn test_archived_exchange_segment_from_all_variants() {
        use crate::compat::instrument_types::{FnoUnderlying, UnderlyingKind};
        let segments = [
            ExchangeSegment::IdxI,
            ExchangeSegment::NseEquity,
            ExchangeSegment::NseFno,
            ExchangeSegment::NseCurrency,
            ExchangeSegment::BseEquity,
            ExchangeSegment::McxComm,
            ExchangeSegment::BseCurrency,
            ExchangeSegment::BseFno,
        ];
        for seg in segments {
            let underlying = FnoUnderlying {
                underlying_symbol: "T".to_string(),
                underlying_security_id: 1,
                price_feed_security_id: 2,
                price_feed_segment: seg,
                derivative_segment: seg,
                kind: UnderlyingKind::Stock,
                lot_size: 1,
                contract_count: 0,
            };
            let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&underlying).unwrap();
            let archived = rkyv::access::<
                crate::compat::instrument_types::ArchivedFnoUnderlying,
                rkyv::rancor::Error,
            >(&bytes)
            .unwrap();
            assert_eq!(ExchangeSegment::from(&archived.price_feed_segment), seg);
        }
    }

    #[test]
    fn test_archived_option_type_from_roundtrip() {
        use crate::compat::instrument_types::{DerivativeContract, DhanInstrumentKind};
        for opt_type in [OptionType::Call, OptionType::Put] {
            let contract = DerivativeContract {
                security_id: 1,
                underlying_symbol: "T".to_string(),
                instrument_kind: DhanInstrumentKind::OptionIndex,
                exchange_segment: ExchangeSegment::NseFno,
                expiry_date: chrono::NaiveDate::from_ymd_opt(2026, 3, 27).unwrap(),
                strike_price: 18000.0,
                option_type: Some(opt_type),
                lot_size: 25,
                tick_size: 0.05,
                symbol_name: "T".to_string(),
                display_name: "T".to_string(),
            };
            let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&contract).unwrap();
            let archived = rkyv::access::<
                crate::compat::instrument_types::ArchivedDerivativeContract,
                rkyv::rancor::Error,
            >(&bytes)
            .unwrap();
            let recovered = archived.option_type.as_ref().map(OptionType::from);
            assert_eq!(recovered, Some(opt_type));
        }
    }

    // --- From<&ArchivedExchange> for Exchange ---

    #[test]
    fn test_archived_exchange_to_exchange_roundtrip() {
        // Exercises From<&ArchivedExchange> for Exchange (lines 282-288)
        for exchange in [
            Exchange::NationalStockExchange,
            Exchange::BombayStockExchange,
        ] {
            let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&exchange).unwrap();
            let archived = rkyv::access::<ArchivedExchange, rkyv::rancor::Error>(&bytes).unwrap();
            let recovered = Exchange::from(archived);
            assert_eq!(recovered, exchange);
        }
    }

    // --- dhan_api_instrument_type: all 6 derivative combinations ---

    #[test]
    fn test_dhan_api_instrument_type_all_six_combinations() {
        // Validates ALL (InstrumentType, is_index_underlying) → Dhan API string mappings:
        // Future + index    → FUTIDX
        // Future + stock    → FUTSTK
        // Option + index    → OPTIDX
        // Option + stock    → OPTSTK
        // Index  + any      → INDEX
        // Equity + any      → EQUITY
        let cases: &[(InstrumentType, bool, &str)] = &[
            (InstrumentType::Future, true, "FUTIDX"),
            (InstrumentType::Future, false, "FUTSTK"),
            (InstrumentType::Option, true, "OPTIDX"),
            (InstrumentType::Option, false, "OPTSTK"),
            (InstrumentType::Index, true, "INDEX"),
            (InstrumentType::Equity, false, "EQUITY"),
        ];
        for &(inst_type, is_index, expected) in cases {
            assert_eq!(
                inst_type.dhan_api_instrument_type(is_index),
                expected,
                "dhan_api_instrument_type({:?}, {}) should be {}",
                inst_type,
                is_index,
                expected,
            );
        }
    }

    #[test]
    fn test_dhan_api_instrument_type_index_ignores_flag() {
        // Index type always returns "INDEX" regardless of is_index_underlying flag
        assert_eq!(
            InstrumentType::Index.dhan_api_instrument_type(true),
            InstrumentType::Index.dhan_api_instrument_type(false),
        );
    }

    #[test]
    fn test_dhan_api_instrument_type_equity_ignores_flag() {
        // Equity type always returns "EQUITY" regardless of is_index_underlying flag
        assert_eq!(
            InstrumentType::Equity.dhan_api_instrument_type(true),
            InstrumentType::Equity.dhan_api_instrument_type(false),
        );
    }

    // --- Archived From conversions: ExchangeSegment direct serialization ---

    #[test]
    fn test_archived_exchange_segment_direct_roundtrip_all_variants() {
        // Serializes each ExchangeSegment directly (not via FnoUnderlying wrapper)
        let segments = [
            ExchangeSegment::IdxI,
            ExchangeSegment::NseEquity,
            ExchangeSegment::NseFno,
            ExchangeSegment::NseCurrency,
            ExchangeSegment::BseEquity,
            ExchangeSegment::McxComm,
            ExchangeSegment::BseCurrency,
            ExchangeSegment::BseFno,
        ];
        for seg in segments {
            let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&seg).unwrap();
            let archived =
                rkyv::access::<ArchivedExchangeSegment, rkyv::rancor::Error>(&bytes).unwrap();
            let recovered = ExchangeSegment::from(archived);
            assert_eq!(recovered, seg, "Archived roundtrip failed for {:?}", seg);
        }
    }

    #[test]
    fn test_archived_option_type_direct_roundtrip() {
        // Serializes each OptionType directly
        for opt in [OptionType::Call, OptionType::Put] {
            let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&opt).unwrap();
            let archived = rkyv::access::<ArchivedOptionType, rkyv::rancor::Error>(&bytes).unwrap();
            let recovered = OptionType::from(archived);
            assert_eq!(recovered, opt, "Archived roundtrip failed for {:?}", opt);
        }
    }

    #[test]
    fn test_archived_exchange_direct_roundtrip() {
        // Serializes each Exchange directly (separate from the FnoUnderlying wrapper test)
        for exchange in [
            Exchange::NationalStockExchange,
            Exchange::BombayStockExchange,
        ] {
            let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&exchange).unwrap();
            let archived = rkyv::access::<ArchivedExchange, rkyv::rancor::Error>(&bytes).unwrap();
            let recovered = Exchange::from(archived);
            assert_eq!(
                recovered, exchange,
                "Archived roundtrip failed for {:?}",
                exchange
            );
        }
    }

    // --- dhan_api_instrument_type: FUTCOM and FUTCUR are not applicable ---
    // The function maps (InstrumentType, is_index_underlying) to Dhan API strings.
    // FUTCOM and FUTCUR are separate Dhan instrument types for commodity/currency
    // futures, which are not representable via InstrumentType::Future + bool flag.
    // The 6 valid combinations are: INDEX, EQUITY, FUTIDX, FUTSTK, OPTIDX, OPTSTK.

    #[test]
    fn test_dhan_api_instrument_type_returns_static_str() {
        // Verify all return values are non-empty &'static str
        let all_cases: &[(InstrumentType, bool)] = &[
            (InstrumentType::Index, true),
            (InstrumentType::Index, false),
            (InstrumentType::Equity, true),
            (InstrumentType::Equity, false),
            (InstrumentType::Future, true),
            (InstrumentType::Future, false),
            (InstrumentType::Option, true),
            (InstrumentType::Option, false),
        ];
        for &(inst_type, is_index) in all_cases {
            let result = inst_type.dhan_api_instrument_type(is_index);
            assert!(
                !result.is_empty(),
                "dhan_api_instrument_type({:?}, {}) returned empty string",
                inst_type,
                is_index,
            );
            // All valid Dhan API instrument strings are uppercase ASCII
            assert!(
                result.chars().all(|c| c.is_ascii_uppercase()),
                "dhan_api_instrument_type({:?}, {}) = '{}' is not uppercase ASCII",
                inst_type,
                is_index,
                result,
            );
        }
    }

    #[test]
    fn test_dhan_api_instrument_type_derivative_flag_matters() {
        // For Future and Option types, the is_index_underlying flag changes the result
        assert_ne!(
            InstrumentType::Future.dhan_api_instrument_type(true),
            InstrumentType::Future.dhan_api_instrument_type(false),
        );
        assert_ne!(
            InstrumentType::Option.dhan_api_instrument_type(true),
            InstrumentType::Option.dhan_api_instrument_type(false),
        );
    }

    #[test]
    fn test_instrument_type_serde_roundtrip_future() {
        let original = InstrumentType::Future;
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: InstrumentType = serde_json::from_str(&json).unwrap();
        assert_eq!(original, deserialized);
    }

    #[test]
    fn test_instrument_type_serde_roundtrip_option() {
        let original = InstrumentType::Option;
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: InstrumentType = serde_json::from_str(&json).unwrap();
        assert_eq!(original, deserialized);
    }

    #[test]
    fn test_feed_mode_clone_copy_eq() {
        let a = FeedMode::Ticker;
        let b = a; // Copy
        let c = a; // Copy (FeedMode is Copy)
        assert_eq!(a, b);
        assert_eq!(a, c);
        assert_ne!(FeedMode::Ticker, FeedMode::Quote);
        assert_ne!(FeedMode::Quote, FeedMode::Full);
        assert_ne!(FeedMode::Full, FeedMode::Ticker);
    }

    #[test]
    fn test_exchange_segment_serde_roundtrip_all_variants() {
        let segments = [
            ExchangeSegment::IdxI,
            ExchangeSegment::NseEquity,
            ExchangeSegment::NseFno,
            ExchangeSegment::NseCurrency,
            ExchangeSegment::BseEquity,
            ExchangeSegment::McxComm,
            ExchangeSegment::BseCurrency,
            ExchangeSegment::BseFno,
        ];
        for seg in segments {
            let json = serde_json::to_string(&seg).unwrap();
            let deserialized: ExchangeSegment = serde_json::from_str(&json).unwrap();
            assert_eq!(seg, deserialized, "Serde roundtrip failed for {:?}", seg);
        }
    }

    #[test]
    fn test_option_type_serde_roundtrip_both_variants() {
        for opt in [OptionType::Call, OptionType::Put] {
            let json = serde_json::to_string(&opt).unwrap();
            let deserialized: OptionType = serde_json::from_str(&json).unwrap();
            assert_eq!(opt, deserialized, "Serde roundtrip failed for {:?}", opt);
        }
    }

    #[test]
    fn test_exchange_hash_both_variants() {
        use std::collections::HashMap;
        let mut map = HashMap::new();
        map.insert(Exchange::NationalStockExchange, "nse");
        map.insert(Exchange::BombayStockExchange, "bse");
        assert_eq!(map.len(), 2);
        assert_eq!(map.get(&Exchange::NationalStockExchange), Some(&"nse"));
        assert_eq!(map.get(&Exchange::BombayStockExchange), Some(&"bse"));
    }

    #[test]
    fn test_from_byte_boundary_values() {
        // Test the edges around the gap and beyond valid range
        assert!(ExchangeSegment::from_byte(0).is_some()); // min valid
        assert!(ExchangeSegment::from_byte(5).is_some()); // last before gap
        assert!(ExchangeSegment::from_byte(6).is_none()); // gap
        assert!(ExchangeSegment::from_byte(7).is_some()); // first after gap
        assert!(ExchangeSegment::from_byte(8).is_some()); // max valid
        assert!(ExchangeSegment::from_byte(9).is_none()); // first invalid
        assert!(ExchangeSegment::from_byte(10).is_none());
        assert!(ExchangeSegment::from_byte(100).is_none());
        assert!(ExchangeSegment::from_byte(u8::MAX).is_none());
    }
}
