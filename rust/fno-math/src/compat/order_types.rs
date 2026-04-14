//! Order-related types for the Dhan trading system.
//!
//! Types for order status, transaction type, product type, order type,
//! validity, and the live order update WebSocket message format.
//! Used by the order update parser and the OMS state machine.

use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Order Status
// ---------------------------------------------------------------------------

/// Order lifecycle status as reported by Dhan.
///
/// Variants match Dhan's annexure (docs/dhan-ref/08-annexure-enums.md):
/// TRANSIT, PENDING, CONFIRMED, TRADED, CANCELLED, REJECTED, EXPIRED,
/// PART_TRADED (partial fill), CLOSED (Super Order), TRIGGERED (Super/Forever Order).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum OrderStatus {
    /// Order in transit to exchange.
    Transit,
    /// Pending at exchange.
    Pending,
    /// Confirmed/open in exchange order book.
    /// NOTE: Not in annexure Section 5. Observed in Dhan WS order update responses
    /// and REST Order Book API. Also note: Forever Orders use a distinct `CONFIRM`
    /// status (per docs/dhan-ref/07b-forever-order.md) — different from `CONFIRMED`.
    Confirmed,
    /// Partially filled — some quantity traded, remainder still open.
    PartTraded,
    /// Fully executed.
    Traded,
    /// Cancelled by user or system.
    Cancelled,
    /// Rejected by exchange or RMS.
    Rejected,
    /// Expired at end of validity.
    /// NOTE: Not in annexure Section 5. Used in Order Book API
    /// (docs/dhan-ref/07-orders.md) and WS order update status subset.
    Expired,
    /// Super Order closed (all legs complete).
    Closed,
    /// Condition triggered (Super Order / Forever Order).
    Triggered,
}

impl OrderStatus {
    /// Returns the canonical string representation.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Transit => "TRANSIT",
            Self::Pending => "PENDING",
            Self::Confirmed => "CONFIRMED",
            Self::PartTraded => "PART_TRADED",
            Self::Traded => "TRADED",
            Self::Cancelled => "CANCELLED",
            Self::Rejected => "REJECTED",
            Self::Expired => "EXPIRED",
            Self::Closed => "CLOSED",
            Self::Triggered => "TRIGGERED",
        }
    }
}

// ---------------------------------------------------------------------------
// Transaction Type
// ---------------------------------------------------------------------------

/// Buy or sell side of an order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TransactionType {
    /// Buy order.
    Buy,
    /// Sell order.
    Sell,
}

impl TransactionType {
    /// Returns the canonical string representation.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Buy => "BUY",
            Self::Sell => "SELL",
        }
    }
}

// ---------------------------------------------------------------------------
// Product Type
// ---------------------------------------------------------------------------

/// Trading product type determining margin and settlement rules.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ProductType {
    /// Cash & Carry — delivery-based equity holding.
    Cnc,
    /// Intraday — squared off same day.
    Intraday,
    /// Margin — leveraged delivery.
    Margin,
    /// Margin Trading Facility — broker-funded leverage.
    Mtf,
    /// Cover Order — with stop-loss.
    /// NOTE: Not in annexure Section 4 (which lists CNC, INTRADAY, MARGIN only).
    /// Present in WS order update single-char Product codes: "V" = CO.
    Co,
    /// Bracket Order — with target + stop-loss.
    /// NOTE: Not in annexure Section 4 (which lists CNC, INTRADAY, MARGIN only).
    /// Present in WS order update single-char Product codes: "B" = BO.
    Bo,
}

impl ProductType {
    /// Returns the canonical string representation.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Cnc => "CNC",
            Self::Intraday => "INTRADAY",
            Self::Margin => "MARGIN",
            Self::Mtf => "MTF",
            Self::Co => "CO",
            Self::Bo => "BO",
        }
    }
}

// ---------------------------------------------------------------------------
// Order Type
// ---------------------------------------------------------------------------

/// Order execution type.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum OrderType {
    /// Limit order — price specified.
    Limit,
    /// Market order — best available price.
    Market,
    /// Stop-loss order — triggers at stop price, then limit.
    StopLoss,
    /// Stop-loss market — triggers at stop price, then market.
    StopLossMarket,
}

impl OrderType {
    /// Returns the canonical string representation.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Limit => "LIMIT",
            Self::Market => "MARKET",
            Self::StopLoss => "STOP_LOSS",
            Self::StopLossMarket => "STOP_LOSS_MARKET",
        }
    }
}

// ---------------------------------------------------------------------------
// Order Validity
// ---------------------------------------------------------------------------

/// Order time-in-force validity.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum OrderValidity {
    /// Valid for the trading day.
    Day,
    /// Immediate or Cancel.
    Ioc,
}

impl OrderValidity {
    /// Returns the canonical string representation.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Day => "DAY",
            Self::Ioc => "IOC",
        }
    }
}

// ---------------------------------------------------------------------------
// Order Update — Live WebSocket message
// ---------------------------------------------------------------------------

/// A live order update received from the Dhan order update WebSocket.
///
/// Field names use PascalCase to match Dhan's WebSocket JSON format.
/// This struct is deserialized from the `"Data"` wrapper in the WebSocket message.
///
/// Note: This is NOT Copy because it contains String fields (cold path only).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "PascalCase")]
pub struct OrderUpdate {
    /// Exchange name ("NSE", "BSE").
    #[serde(default)]
    pub exchange: String,
    /// Segment code ("E" = Equity, "D" = Derivative).
    #[serde(default)]
    pub segment: String,
    /// Dhan security identifier.
    #[serde(default)]
    pub security_id: String,
    /// Dhan client identifier.
    #[serde(default)]
    pub client_id: String,
    /// Dhan order number.
    #[serde(default)]
    pub order_no: String,
    /// Exchange order number.
    #[serde(default)]
    pub exch_order_no: String,
    /// Product type code ("C" = CNC, etc.).
    #[serde(default)]
    pub product: String,
    /// Transaction type ("B" = Buy, "S" = Sell).
    #[serde(default)]
    pub txn_type: String,
    /// Order type ("LMT", "MKT", etc.).
    #[serde(default)]
    pub order_type: String,
    /// Validity ("DAY", "IOC").
    #[serde(default)]
    pub validity: String,
    /// Total order quantity.
    #[serde(default)]
    pub quantity: i64,
    /// Filled/traded quantity.
    #[serde(default)]
    pub traded_qty: i64,
    /// Remaining unfilled quantity.
    #[serde(default)]
    pub remaining_quantity: i64,
    /// Order price.
    #[serde(default)]
    pub price: f64,
    /// Trigger price for stop-loss orders.
    #[serde(default)]
    pub trigger_price: f64,
    /// Last traded price of the fill.
    #[serde(default)]
    pub traded_price: f64,
    /// Average traded price across all fills.
    #[serde(default)]
    pub avg_traded_price: f64,
    /// Order status string ("TRANSIT", "PENDING", "TRADED", etc.).
    #[serde(default)]
    pub status: String,
    /// Trading symbol.
    #[serde(default)]
    pub symbol: String,
    /// Display-friendly instrument name.
    #[serde(default)]
    pub display_name: String,
    /// User-supplied correlation ID for idempotency.
    #[serde(default)]
    pub correlation_id: String,
    /// User-supplied remarks.
    #[serde(default)]
    pub remarks: String,
    /// Reason description ("CONFIRMED", rejection reason, etc.).
    #[serde(default)]
    pub reason_description: String,
    /// Order creation datetime ("YYYY-MM-DD HH:MM:SS").
    #[serde(default)]
    pub order_date_time: String,
    /// Exchange order timestamp.
    #[serde(default)]
    pub exch_order_time: String,
    /// Last update timestamp.
    #[serde(default)]
    pub last_updated_time: String,
    /// Instrument type ("EQUITY", "OPTIDX", etc.).
    #[serde(default)]
    pub instrument: String,
    /// Lot size.
    #[serde(default)]
    pub lot_size: i64,
    /// Strike price (derivatives only).
    #[serde(default)]
    pub strike_price: f64,
    /// Expiry date string (derivatives only).
    #[serde(default)]
    pub expiry_date: String,
    /// Option type ("CE", "PE", "XX").
    #[serde(default)]
    pub opt_type: String,
    /// ISIN code.
    #[serde(default)]
    pub isin: String,
    /// Disclosed quantity.
    #[serde(default)]
    pub disc_quantity: i64,
    /// Disclosed quantity remaining.
    #[serde(default)]
    pub disc_qty_rem: i64,
    /// Leg number.
    #[serde(default)]
    pub leg_no: i64,
    /// Product name ("CNC", "INTRADAY", etc.).
    #[serde(default)]
    pub product_name: String,
    /// Reference LTP.
    #[serde(default, rename = "refLtp")]
    pub ref_ltp: f64,
    /// Tick size.
    #[serde(default, rename = "tickSize")]
    pub tick_size: f64,
    /// Order source: "P" = API, "N" = Dhan web/app.
    /// Filter by "P" to process only our own API-placed orders.
    #[serde(default)]
    pub source: String,
    /// Off-market flag: "1" = AMO (After Market Order), "0" = normal.
    #[serde(default)]
    pub off_mkt_flag: String,
    /// Entry leg order number for tracking related legs in BO/CO/super orders.
    #[serde(default)]
    pub algo_ord_no: String,
    /// Market type: "NL" = Normal Market, "AU"/"A1"/"A2" = Auction Market.
    #[serde(default)]
    pub mkt_type: String,
    /// Exchange series (e.g., "EQ").
    #[serde(default)]
    pub series: String,
    /// Order validity date for Forever Orders.
    #[serde(default)]
    pub good_till_days_date: String,
    /// Exchange ID for special order types.
    #[serde(default)]
    pub algo_id: String,
    /// Multiplier for commodity/currency contracts.
    #[serde(default)]
    pub multiplier: i64,
}

/// Wrapper for the Dhan order update WebSocket message.
///
/// The WebSocket sends JSON with a top-level `"Data"` key containing the order fields.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "PascalCase")]
pub struct OrderUpdateMessage {
    /// The order update payload.
    pub data: OrderUpdate,
    /// Message type (e.g., "order_alert").
    #[serde(default)]
    pub r#type: String,
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // --- OrderStatus ---

    #[test]
    fn test_order_status_as_str() {
        assert_eq!(OrderStatus::Transit.as_str(), "TRANSIT");
        assert_eq!(OrderStatus::Pending.as_str(), "PENDING");
        assert_eq!(OrderStatus::Confirmed.as_str(), "CONFIRMED");
        assert_eq!(OrderStatus::PartTraded.as_str(), "PART_TRADED");
        assert_eq!(OrderStatus::Traded.as_str(), "TRADED");
        assert_eq!(OrderStatus::Cancelled.as_str(), "CANCELLED");
        assert_eq!(OrderStatus::Rejected.as_str(), "REJECTED");
        assert_eq!(OrderStatus::Expired.as_str(), "EXPIRED");
        assert_eq!(OrderStatus::Closed.as_str(), "CLOSED");
        assert_eq!(OrderStatus::Triggered.as_str(), "TRIGGERED");
    }

    #[test]
    fn test_order_status_serde_roundtrip() {
        let original = OrderStatus::Traded;
        let json = serde_json::to_string(&original).unwrap();
        assert_eq!(json, "\"TRADED\"");
        let deserialized: OrderStatus = serde_json::from_str(&json).unwrap();
        assert_eq!(original, deserialized);
    }

    #[test]
    fn test_order_status_serde_part_traded() {
        let original = OrderStatus::PartTraded;
        let json = serde_json::to_string(&original).unwrap();
        assert_eq!(json, "\"PART_TRADED\"");
        let deserialized: OrderStatus = serde_json::from_str(&json).unwrap();
        assert_eq!(original, deserialized);
    }

    #[test]
    fn test_order_status_serde_closed_and_triggered() {
        let closed = OrderStatus::Closed;
        let json = serde_json::to_string(&closed).unwrap();
        assert_eq!(json, "\"CLOSED\"");
        let deserialized: OrderStatus = serde_json::from_str(&json).unwrap();
        assert_eq!(closed, deserialized);

        let triggered = OrderStatus::Triggered;
        let json = serde_json::to_string(&triggered).unwrap();
        assert_eq!(json, "\"TRIGGERED\"");
        let deserialized: OrderStatus = serde_json::from_str(&json).unwrap();
        assert_eq!(triggered, deserialized);
    }

    // --- TransactionType ---

    #[test]
    fn test_transaction_type_as_str() {
        assert_eq!(TransactionType::Buy.as_str(), "BUY");
        assert_eq!(TransactionType::Sell.as_str(), "SELL");
    }

    #[test]
    fn test_transaction_type_serde_roundtrip() {
        let original = TransactionType::Buy;
        let json = serde_json::to_string(&original).unwrap();
        let deserialized: TransactionType = serde_json::from_str(&json).unwrap();
        assert_eq!(original, deserialized);
    }

    // --- ProductType ---

    #[test]
    fn test_product_type_as_str_all_variants() {
        assert_eq!(ProductType::Cnc.as_str(), "CNC");
        assert_eq!(ProductType::Intraday.as_str(), "INTRADAY");
        assert_eq!(ProductType::Margin.as_str(), "MARGIN");
        assert_eq!(ProductType::Mtf.as_str(), "MTF");
        assert_eq!(ProductType::Co.as_str(), "CO");
        assert_eq!(ProductType::Bo.as_str(), "BO");
    }

    // --- OrderType ---

    #[test]
    fn test_order_type_as_str_all_variants() {
        assert_eq!(OrderType::Limit.as_str(), "LIMIT");
        assert_eq!(OrderType::Market.as_str(), "MARKET");
        assert_eq!(OrderType::StopLoss.as_str(), "STOP_LOSS");
        assert_eq!(OrderType::StopLossMarket.as_str(), "STOP_LOSS_MARKET");
    }

    // --- OrderValidity ---

    #[test]
    fn test_order_validity_as_str() {
        assert_eq!(OrderValidity::Day.as_str(), "DAY");
        assert_eq!(OrderValidity::Ioc.as_str(), "IOC");
    }

    // --- OrderUpdate deserialization ---

    #[test]
    fn test_order_update_deserialize_full_message() {
        let json = r#"{
            "Data": {
                "Exchange": "NSE",
                "Segment": "D",
                "SecurityId": "52432",
                "ClientId": "1000000001",
                "OrderNo": "1234567890",
                "ExchOrderNo": "NSE-1234567",
                "Product": "I",
                "TxnType": "B",
                "OrderType": "LMT",
                "Validity": "DAY",
                "Quantity": 50,
                "TradedQty": 50,
                "RemainingQuantity": 0,
                "Price": 245.50,
                "TriggerPrice": 0.0,
                "TradedPrice": 245.50,
                "AvgTradedPrice": 245.50,
                "Status": "TRADED",
                "Symbol": "NIFTY",
                "DisplayName": "NIFTY 27MAR 24500 CE",
                "CorrelationId": "uuid-123",
                "Remarks": "",
                "ReasonDescription": "CONFIRMED",
                "OrderDateTime": "2026-03-15 10:30:45",
                "ExchOrderTime": "2026-03-15 10:30:45",
                "LastUpdatedTime": "2026-03-15 10:30:46",
                "Instrument": "OPTIDX",
                "LotSize": 50,
                "StrikePrice": 24500.0,
                "ExpiryDate": "2026-03-27",
                "OptType": "CE",
                "Isin": "",
                "DiscQuantity": 0,
                "DiscQtyRem": 0,
                "LegNo": 0,
                "ProductName": "INTRADAY",
                "refLtp": 245.0,
                "tickSize": 0.05
            }
        }"#;

        let msg: OrderUpdateMessage = serde_json::from_str(json).unwrap();
        assert_eq!(msg.data.exchange, "NSE");
        assert_eq!(msg.data.security_id, "52432");
        assert_eq!(msg.data.order_no, "1234567890");
        assert_eq!(msg.data.txn_type, "B");
        assert_eq!(msg.data.status, "TRADED");
        assert_eq!(msg.data.quantity, 50);
        assert_eq!(msg.data.traded_qty, 50);
        assert!((msg.data.price - 245.50).abs() < f64::EPSILON);
        assert!((msg.data.avg_traded_price - 245.50).abs() < f64::EPSILON);
        assert_eq!(msg.data.symbol, "NIFTY");
        assert_eq!(msg.data.opt_type, "CE");
        assert!((msg.data.strike_price - 24500.0).abs() < f64::EPSILON);
        assert!((msg.data.ref_ltp - 245.0).abs() < f64::EPSILON);
        assert!((msg.data.tick_size - 0.05).abs() < f64::EPSILON);
    }

    #[test]
    fn test_order_update_deserialize_minimal_fields() {
        let json = r#"{
            "Data": {
                "OrderNo": "999",
                "Status": "PENDING"
            }
        }"#;

        let msg: OrderUpdateMessage = serde_json::from_str(json).unwrap();
        assert_eq!(msg.data.order_no, "999");
        assert_eq!(msg.data.status, "PENDING");
        assert_eq!(msg.data.exchange, "");
        assert_eq!(msg.data.quantity, 0);
        assert_eq!(msg.data.price, 0.0);
    }

    // --- Serde roundtrip for ALL OrderStatus variants ---

    #[test]
    fn test_order_status_serde_roundtrip_all_variants() {
        let variants = [
            (OrderStatus::Transit, "\"TRANSIT\""),
            (OrderStatus::Pending, "\"PENDING\""),
            (OrderStatus::Confirmed, "\"CONFIRMED\""),
            (OrderStatus::PartTraded, "\"PART_TRADED\""),
            (OrderStatus::Traded, "\"TRADED\""),
            (OrderStatus::Cancelled, "\"CANCELLED\""),
            (OrderStatus::Rejected, "\"REJECTED\""),
            (OrderStatus::Expired, "\"EXPIRED\""),
            (OrderStatus::Closed, "\"CLOSED\""),
            (OrderStatus::Triggered, "\"TRIGGERED\""),
        ];
        for (variant, expected_json) in variants {
            let json = serde_json::to_string(&variant).unwrap();
            assert_eq!(json, expected_json);
            let deserialized: OrderStatus = serde_json::from_str(&json).unwrap();
            assert_eq!(variant, deserialized);
        }
    }

    // --- Serde roundtrip for ALL TransactionType variants ---

    #[test]
    fn test_transaction_type_serde_roundtrip_all_variants() {
        for variant in [TransactionType::Buy, TransactionType::Sell] {
            let json = serde_json::to_string(&variant).unwrap();
            let deserialized: TransactionType = serde_json::from_str(&json).unwrap();
            assert_eq!(variant, deserialized);
        }
    }

    // --- Serde roundtrip for ALL ProductType variants ---

    #[test]
    fn test_product_type_serde_roundtrip_all_variants() {
        let variants = [
            (ProductType::Cnc, "\"CNC\""),
            (ProductType::Intraday, "\"INTRADAY\""),
            (ProductType::Margin, "\"MARGIN\""),
            (ProductType::Mtf, "\"MTF\""),
            (ProductType::Co, "\"CO\""),
            (ProductType::Bo, "\"BO\""),
        ];
        for (variant, expected_json) in variants {
            let json = serde_json::to_string(&variant).unwrap();
            assert_eq!(json, expected_json);
            let deserialized: ProductType = serde_json::from_str(&json).unwrap();
            assert_eq!(variant, deserialized);
        }
    }

    // --- Serde roundtrip for ALL OrderType variants ---

    #[test]
    fn test_order_type_serde_roundtrip_all_variants() {
        let variants = [
            (OrderType::Limit, "\"LIMIT\""),
            (OrderType::Market, "\"MARKET\""),
            (OrderType::StopLoss, "\"STOP_LOSS\""),
            (OrderType::StopLossMarket, "\"STOP_LOSS_MARKET\""),
        ];
        for (variant, expected_json) in variants {
            let json = serde_json::to_string(&variant).unwrap();
            assert_eq!(json, expected_json);
            let deserialized: OrderType = serde_json::from_str(&json).unwrap();
            assert_eq!(variant, deserialized);
        }
    }

    // --- Serde roundtrip for ALL OrderValidity variants ---

    #[test]
    fn test_order_validity_serde_roundtrip_all_variants() {
        for variant in [OrderValidity::Day, OrderValidity::Ioc] {
            let json = serde_json::to_string(&variant).unwrap();
            let deserialized: OrderValidity = serde_json::from_str(&json).unwrap();
            assert_eq!(variant, deserialized);
        }
    }

    // --- OrderUpdate: source and off_mkt_flag fields ---

    #[test]
    fn test_order_update_source_and_off_mkt_flag_from_json() {
        let json = r#"{
            "Data": {
                "OrderNo": "111",
                "Status": "TRADED",
                "Source": "P",
                "OffMktFlag": "1"
            },
            "Type": "order_alert"
        }"#;
        let msg: OrderUpdateMessage = serde_json::from_str(json).unwrap();
        assert_eq!(msg.data.source, "P");
        assert_eq!(msg.data.off_mkt_flag, "1");
        assert_eq!(msg.r#type, "order_alert");
    }

    // --- OrderStatus Debug impl ---

    #[test]
    fn test_order_status_debug() {
        let debug = format!("{:?}", OrderStatus::PartTraded);
        assert!(debug.contains("PartTraded"));
    }

    // --- OrderUpdate with Type field ---

    #[test]
    fn test_order_update_message_type_field() {
        let json = r#"{
            "Data": {
                "OrderNo": "222",
                "Status": "PENDING"
            },
            "Type": "order_alert"
        }"#;
        let msg: OrderUpdateMessage = serde_json::from_str(json).unwrap();
        assert_eq!(msg.r#type, "order_alert");
    }

    // --- OrderUpdate Hash/Eq/Clone ---

    #[test]
    fn test_order_status_hash_eq() {
        use std::collections::HashSet;
        let mut set = HashSet::new();
        set.insert(OrderStatus::Transit);
        set.insert(OrderStatus::Traded);
        set.insert(OrderStatus::Transit); // duplicate
        assert_eq!(set.len(), 2);
    }

    #[test]
    fn test_transaction_type_hash_eq() {
        use std::collections::HashSet;
        let mut set = HashSet::new();
        set.insert(TransactionType::Buy);
        set.insert(TransactionType::Sell);
        assert_eq!(set.len(), 2);
    }

    #[test]
    fn test_order_update_serialize_roundtrip() {
        let update = OrderUpdate {
            exchange: "NSE".to_owned(),
            segment: "D".to_owned(),
            security_id: "52432".to_owned(),
            client_id: "100".to_owned(),
            order_no: "123".to_owned(),
            exch_order_no: "NSE-123".to_owned(),
            product: "I".to_owned(),
            txn_type: "B".to_owned(),
            order_type: "LMT".to_owned(),
            validity: "DAY".to_owned(),
            quantity: 50,
            traded_qty: 25,
            remaining_quantity: 25,
            price: 245.50,
            trigger_price: 0.0,
            traded_price: 245.50,
            avg_traded_price: 245.50,
            status: "PENDING".to_owned(),
            symbol: "NIFTY".to_owned(),
            display_name: "NIFTY CE".to_owned(),
            correlation_id: "uuid-1".to_owned(),
            remarks: String::new(),
            reason_description: "CONFIRMED".to_owned(),
            order_date_time: "2026-03-15 10:30:45".to_owned(),
            exch_order_time: "2026-03-15 10:30:45".to_owned(),
            last_updated_time: "2026-03-15 10:30:46".to_owned(),
            instrument: "OPTIDX".to_owned(),
            lot_size: 50,
            strike_price: 24500.0,
            expiry_date: "2026-03-27".to_owned(),
            opt_type: "CE".to_owned(),
            isin: String::new(),
            disc_quantity: 0,
            disc_qty_rem: 0,
            leg_no: 0,
            product_name: "INTRADAY".to_owned(),
            ref_ltp: 245.0,
            tick_size: 0.05,
            source: "P".to_owned(),
            off_mkt_flag: "0".to_owned(),
            algo_ord_no: String::new(),
            mkt_type: "NL".to_owned(),
            series: "EQ".to_owned(),
            good_till_days_date: String::new(),
            algo_id: String::new(),
            multiplier: 1,
        };

        let msg = OrderUpdateMessage {
            data: update.clone(),
            r#type: "order_alert".to_owned(),
        };
        let json = serde_json::to_string(&msg).unwrap();
        let deserialized: OrderUpdateMessage = serde_json::from_str(&json).unwrap();
        assert_eq!(msg, deserialized);
    }
}
