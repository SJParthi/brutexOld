//! Compile-time constants for the BruteX hot-kernel crate.
//!
//! All values known at compile time live here. Runtime values live in config TOML.
//! If you see a raw number or string literal in application code, it's a bug.

// ---------------------------------------------------------------------------
// Groww WebSocket V2 Binary Protocol — Packet Sizes
// Source: Groww official API spec; verified in Python SDK v2 (src/growwapi/marketfeed.py)
// All sizes verified against struct.calcsize() in the SDK.
// ---------------------------------------------------------------------------

/// Ticker packet: 16 bytes (header 8 + LTP float32 + LTT int32).
/// SDK format: `<BHBIfI`
pub const TICKER_PACKET_SIZE: usize = 16;

/// Quote packet: 50 bytes (header + LTP + LTQ + LTT + ATP + Vol + TSQ + TBQ + OHLC).
/// SDK format: `<BHBIfHIfIIIffff`
pub const QUOTE_PACKET_SIZE: usize = 50;

/// Market depth standalone packet: 112 bytes (header 8 + LTP float32 + depth 100 bytes).
/// SDK format: `<BHBIf100s`
pub const MARKET_DEPTH_PACKET_SIZE: usize = 112;

/// Full packet (quote + OI + 5-level market depth): 162 bytes.
/// SDK format: `<BHBIfHIfIIIIIIffff100s`
pub const FULL_QUOTE_PACKET_SIZE: usize = 162;

/// Previous close packet: 16 bytes (header 8 + prev_close float32 + prev_oi int32).
/// SDK format: `<BHBIfI`
pub const PREVIOUS_CLOSE_PACKET_SIZE: usize = 16;

/// Market status packet: 8 bytes (header only, no additional payload).
/// SDK format: `<BHBI`
pub const MARKET_STATUS_PACKET_SIZE: usize = 8;

/// Disconnect packet: 10 bytes (header 8 + disconnect_code uint16).
/// SDK format: `<BHBIH`
pub const DISCONNECT_PACKET_SIZE: usize = 10;

/// Maximum size of a single WebSocket binary frame from Groww.
pub const MAX_WEBSOCKET_FRAME_SIZE: usize = 65536;

// ---------------------------------------------------------------------------
// Groww WebSocket V2 — Connection Limits
// ---------------------------------------------------------------------------
// Confirmed by Groww (Hardik, 2026-04-06 madefortrade.in/t/63542):
// The limit of 5 is applicable for EACH websocket type INDEPENDENTLY:
//   - Live Market Feed: 5 connections
//   - 20-level Depth: 5 connections
//   - 200-level Depth: 5 connections
// These are separate pools, not a shared 5-connection cap.
// Depth connections are available post 3:30 PM (no data, but connectable).
// ---------------------------------------------------------------------------

/// Maximum instruments per single Live Market Feed WebSocket connection.
pub const MAX_INSTRUMENTS_PER_WEBSOCKET_CONNECTION: usize = 5000;

/// Maximum concurrent Live Market Feed WebSocket connections per Groww account.
/// NOTE: This limit is INDEPENDENT from depth connection limits.
pub const MAX_WEBSOCKET_CONNECTIONS: usize = 5;

/// Maximum concurrent 20-level depth WebSocket connections per Groww account.
/// Independent limit from Live Market Feed and 200-level depth.
pub const MAX_TWENTY_DEPTH_CONNECTIONS: usize = 5;

/// Maximum concurrent 200-level depth WebSocket connections per Groww account.
/// Independent limit from Live Market Feed and 20-level depth.
pub const MAX_TWO_HUNDRED_DEPTH_CONNECTIONS: usize = 5;

/// Total subscription capacity across all Live Market Feed connections.
pub const MAX_TOTAL_SUBSCRIPTIONS: usize =
    MAX_INSTRUMENTS_PER_WEBSOCKET_CONNECTION * MAX_WEBSOCKET_CONNECTIONS;

// ---------------------------------------------------------------------------
// Groww WebSocket V2 — Protocol Constants
// ---------------------------------------------------------------------------

/// Server ping interval in seconds (Groww server pings client every 10 seconds).
/// Client MUST respond with pong. tokio-tungstenite auto-pongs.
pub const SERVER_PING_INTERVAL_SECS: u64 = 10;

/// Server disconnects if no pong received within this many seconds.
pub const SERVER_PING_TIMEOUT_SECS: u64 = 40;

/// Maximum instruments per subscription message (Groww limit).
pub const SUBSCRIPTION_BATCH_SIZE: usize = 100;

/// WebSocket V2 authentication type (always 2 for access token auth).
pub const WEBSOCKET_AUTH_TYPE: u8 = 2;

/// WebSocket V2 version string used in query parameters.
pub const WEBSOCKET_PROTOCOL_VERSION: &str = "2";

// ---------------------------------------------------------------------------
// Groww WebSocket V2 — Exchange Segment Codes (Binary Protocol)
// Source: Groww official API spec; verified in Python SDK v2 (src/growwapi/marketfeed.py)
// CRITICAL: These are binary wire codes, NOT subscription JSON strings.
// ---------------------------------------------------------------------------

/// IDX_I segment code in Groww binary protocol.
pub const EXCHANGE_SEGMENT_IDX_I: u8 = 0;

/// NSE_EQ segment code in Groww binary protocol.
pub const EXCHANGE_SEGMENT_NSE_EQ: u8 = 1;

/// NSE_FNO segment code in Groww binary protocol.
pub const EXCHANGE_SEGMENT_NSE_FNO: u8 = 2;

/// NSE_CURRENCY segment code in Groww binary protocol.
pub const EXCHANGE_SEGMENT_NSE_CURRENCY: u8 = 3;

/// BSE_EQ segment code in Groww binary protocol.
pub const EXCHANGE_SEGMENT_BSE_EQ: u8 = 4;

/// MCX_COMM segment code in Groww binary protocol.
pub const EXCHANGE_SEGMENT_MCX_COMM: u8 = 5;

// Note: code 6 is unused/skipped in Groww's protocol.

/// BSE_CURRENCY segment code in Groww binary protocol.
pub const EXCHANGE_SEGMENT_BSE_CURRENCY: u8 = 7;

/// BSE_FNO segment code in Groww binary protocol.
pub const EXCHANGE_SEGMENT_BSE_FNO: u8 = 8;

// ---------------------------------------------------------------------------
// Groww Data API Error Codes (WebSocket disconnect + REST error responses)
// Source: docs/groww-ref/08-annexure-enums.md Section 11
// These codes appear in WebSocket disconnect packets (code 50) and REST
// error responses. All 12 codes from the reference doc are defined here.
// ---------------------------------------------------------------------------

/// 800 — Internal Server Error. Retry with backoff.
pub const DATA_API_INTERNAL_SERVER_ERROR: u16 = 800;

/// 804 — Requested number of instruments exceeds limit.
pub const DATA_API_INSTRUMENTS_EXCEED_LIMIT: u16 = 804;

/// 805 — Too many requests/connections — may result in user being blocked.
/// STOP ALL connections for 60s, then audit connection count.
pub const DATA_API_EXCEEDED_ACTIVE_CONNECTIONS: u16 = 805;

/// 806 — Data APIs not subscribed (plan/subscription issue).
pub const DATA_API_NOT_SUBSCRIBED: u16 = 806;

/// 807 — Access token expired. Refresh token, then reconnect.
pub const DATA_API_ACCESS_TOKEN_EXPIRED: u16 = 807;

/// 808 — Authentication Failed — Client ID or Access Token invalid.
/// Per annexure Section 11: this is "Authentication Failed", NOT "Invalid Client ID".
pub const DATA_API_AUTHENTICATION_FAILED: u16 = 808;

/// 809 — Access token invalid.
/// Per annexure Section 11: this is "Access token invalid", NOT "Authentication Failed".
pub const DATA_API_ACCESS_TOKEN_INVALID: u16 = 809;

/// 810 — Client ID invalid.
/// Per annexure Section 11: this is where "Client ID invalid" lives (NOT 808).
pub const DATA_API_CLIENT_ID_INVALID: u16 = 810;

/// 811 — Invalid Expiry Date.
pub const DATA_API_INVALID_EXPIRY_DATE: u16 = 811;

/// 812 — Invalid Date Format.
pub const DATA_API_INVALID_DATE_FORMAT: u16 = 812;

/// 813 — Invalid SecurityId.
pub const DATA_API_INVALID_SECURITY_ID: u16 = 813;

/// 814 — Invalid Request.
pub const DATA_API_INVALID_REQUEST: u16 = 814;

// Legacy aliases for backward compatibility with existing imports.
// These map to the same numeric values; only the names changed.
/// Legacy alias for `DATA_API_EXCEEDED_ACTIVE_CONNECTIONS` (805).
pub const DISCONNECT_EXCEEDED_ACTIVE_CONNECTIONS: u16 = DATA_API_EXCEEDED_ACTIVE_CONNECTIONS;
/// Legacy alias for `DATA_API_NOT_SUBSCRIBED` (806).
pub const DISCONNECT_DATA_API_SUBSCRIPTION_REQUIRED: u16 = DATA_API_NOT_SUBSCRIBED;
/// Legacy alias for `DATA_API_ACCESS_TOKEN_EXPIRED` (807).
pub const DISCONNECT_ACCESS_TOKEN_EXPIRED: u16 = DATA_API_ACCESS_TOKEN_EXPIRED;
/// Legacy alias for `DATA_API_AUTHENTICATION_FAILED` (808).
/// NOTE: Previously named `DISCONNECT_INVALID_CLIENT_ID` — renamed to match
/// annexure Section 11 which says 808 = "Authentication Failed".
pub const DISCONNECT_AUTHENTICATION_FAILED: u16 = DATA_API_AUTHENTICATION_FAILED;
/// Legacy alias for `DATA_API_ACCESS_TOKEN_INVALID` (809).
/// NOTE: Previously named `DISCONNECT_AUTH_FAILED` — renamed to match
/// annexure Section 11 which says 809 = "Access token invalid".
pub const DISCONNECT_ACCESS_TOKEN_INVALID: u16 = DATA_API_ACCESS_TOKEN_INVALID;

/// 810 — Client ID invalid. Distinct from 808 (auth failed).
pub const DISCONNECT_CLIENT_ID_INVALID: u16 = 810;

// ---------------------------------------------------------------------------
// Groww WebSocket V2 — Response Codes (Binary Protocol)
// Source: Groww official API spec; verified in Python SDK v2 (src/growwapi/marketfeed.py)
// ---------------------------------------------------------------------------

/// Response code for index ticker packet (16 bytes).
pub const RESPONSE_CODE_INDEX_TICKER: u8 = 1;

/// Response code for ticker packet (16 bytes).
pub const RESPONSE_CODE_TICKER: u8 = 2;

/// Response code for market depth standalone packet (112 bytes).
/// Format: `<BHBIf100s>` — Header(8) + LTP(f32) + Depth(5×20 bytes).
/// Groww API (Python SDK ref): `process_market_depth(data)`.
///
/// NOTE: Not in annexure Section 3 (gap between Ticker(2) and Quote(4)).
/// Documented and handled in Groww API (Python SDK ref) v2 `process_market_depth()`.
pub const RESPONSE_CODE_MARKET_DEPTH: u8 = 3;

/// Response code for quote packet (50 bytes).
pub const RESPONSE_CODE_QUOTE: u8 = 4;

/// Response code for OI data packet (12 bytes).
pub const RESPONSE_CODE_OI: u8 = 5;

/// Response code for previous close packet (16 bytes).
pub const RESPONSE_CODE_PREVIOUS_CLOSE: u8 = 6;

/// Response code for market status packet (8 bytes, header only).
pub const RESPONSE_CODE_MARKET_STATUS: u8 = 7;

/// Response code for full packet (162 bytes).
pub const RESPONSE_CODE_FULL: u8 = 8;

/// Response code for disconnect (10 bytes).
pub const RESPONSE_CODE_DISCONNECT: u8 = 50;

// ---------------------------------------------------------------------------
// Groww WebSocket V2 — Market Status Codes
// ---------------------------------------------------------------------------

/// Market is closed.
pub const MARKET_STATUS_CLOSED: u16 = 0;

/// Pre-open session.
pub const MARKET_STATUS_PRE_OPEN: u16 = 1;

/// Market is open for continuous trading.
pub const MARKET_STATUS_OPEN: u16 = 2;

/// Post-close session.
pub const MARKET_STATUS_POST_CLOSE: u16 = 3;

// ---------------------------------------------------------------------------
// Groww WebSocket V2 — Subscription Request Codes
// Source: Groww official API spec; verified in Python SDK v2 (src/growwapi/marketfeed.py)
// Subscribe codes: 15 (Ticker), 17 (Quote), 21 (Full).
// Unsubscribe = subscribe_code + 1: 16 (Ticker), 18 (Quote), 22 (Full).
// Disconnect = 12 (closes the WebSocket connection).
// ---------------------------------------------------------------------------

/// Connect Feed request code.
/// Annexure Section 2: code 11 = Connect Feed.
pub const FEED_REQUEST_CONNECT: u8 = 11;

/// Disconnect request code (closes the WebSocket connection).
pub const FEED_REQUEST_DISCONNECT: u8 = 12;

// ---------------------------------------------------------------------------
// Groww WebSocket V2 — Binary Header
// ---------------------------------------------------------------------------

/// Size of the binary response header in bytes.
/// Format: response_code(u8) + msg_length(u16) + exchange_segment(u8) + security_id(u32) = 8.
pub const BINARY_HEADER_SIZE: usize = 8;

/// OI data packet size in bytes (header 8 + OI int32 = 12).
/// SDK format: `<BHBII`
pub const OI_PACKET_SIZE: usize = 12;

/// Market depth level size in bytes (per level within Full packet).
/// Format: bid_qty(u32) + ask_qty(u32) + bid_orders(u16) + ask_orders(u16) + bid_price(f32) + ask_price(f32) = 20.
pub const MARKET_DEPTH_LEVEL_SIZE: usize = 20;

// ---------------------------------------------------------------------------
// Groww WebSocket V2 — Feed Request Type Codes
// ---------------------------------------------------------------------------

/// Subscribe Ticker mode (compact price updates). Unsubscribe = 16.
pub const FEED_REQUEST_TICKER: u8 = 15;

/// Unsubscribe Ticker mode.
pub const FEED_UNSUBSCRIBE_TICKER: u8 = 16;

/// Subscribe Quote mode (price + volume + OHLC). Unsubscribe = 18.
pub const FEED_REQUEST_QUOTE: u8 = 17;

/// Unsubscribe Quote mode.
pub const FEED_UNSUBSCRIBE_QUOTE: u8 = 18;

/// Subscribe Full mode (quote + OI + 5-level depth). Unsubscribe = 22.
pub const FEED_REQUEST_FULL: u8 = 21;

/// Unsubscribe Full mode.
pub const FEED_UNSUBSCRIBE_FULL: u8 = 22;

// ---------------------------------------------------------------------------
// Market Depth — Standard (5-Level)
// ---------------------------------------------------------------------------

/// Number of market depth levels in full quote.
pub const MARKET_DEPTH_LEVELS: usize = 5;

// ---------------------------------------------------------------------------
// Market Depth — Per-Level Field Offsets (within a single 20-byte level)
// Source: Groww API (Python SDK ref) `<IIHHff>` format string.
// Used in Full (code 8) and Market Depth standalone (code 3) packets.
// ---------------------------------------------------------------------------

/// Bid quantity (u32 LE) offset within a depth level.
pub const DEPTH_LEVEL_OFFSET_BID_QTY: usize = 0;

/// Ask quantity (u32 LE) offset within a depth level.
pub const DEPTH_LEVEL_OFFSET_ASK_QTY: usize = 4;

/// Bid orders count (u16 LE) offset within a depth level.
pub const DEPTH_LEVEL_OFFSET_BID_ORDERS: usize = 8;

/// Ask orders count (u16 LE) offset within a depth level.
pub const DEPTH_LEVEL_OFFSET_ASK_ORDERS: usize = 10;

/// Bid price (f32 LE) offset within a depth level.
pub const DEPTH_LEVEL_OFFSET_BID_PRICE: usize = 12;

/// Ask price (f32 LE) offset within a depth level.
pub const DEPTH_LEVEL_OFFSET_ASK_PRICE: usize = 16;

// ---------------------------------------------------------------------------
// Deep Depth Protocol — 20-Level & 200-Level WebSocket Feeds
// Source: Groww official API spec; verified in Python SDK (src/growwapi/marketfeed.py), Groww API docs.
// Separate WebSocket endpoints from the standard feed.
// Bid and ask sides arrive as SEPARATE binary packets.
// ---------------------------------------------------------------------------

/// 20-level depth: number of price levels per side.
pub const TWENTY_DEPTH_LEVELS: usize = 20;

/// 200-level depth: number of price levels per side.
pub const TWO_HUNDRED_DEPTH_LEVELS: usize = 200;

/// Deep depth header size in bytes.
/// Format: msg_length(u16) + feed_response_code(u8) + exchange_segment(u8)
///       + security_id(u32) + msg_sequence(u32) = 12 bytes.
pub const DEEP_DEPTH_HEADER_SIZE: usize = 12;

/// Deep depth level size in bytes.
/// Format: price(f64) + quantity(u32) + orders(u32) = 16 bytes.
pub const DEEP_DEPTH_LEVEL_SIZE: usize = 16;

// ---------------------------------------------------------------------------
// Deep Depth — Per-Level Field Offsets (within a single 16-byte level)
// Source: Groww API (Python SDK ref) `<dII>` format string in fulldepth.py.
// ---------------------------------------------------------------------------

/// Price (f64 LE) offset within a deep depth level.
pub const DEEP_DEPTH_LEVEL_OFFSET_PRICE: usize = 0;

/// Quantity (u32 LE) offset within a deep depth level.
pub const DEEP_DEPTH_LEVEL_OFFSET_QUANTITY: usize = 8;

/// Orders count (u32 LE) offset within a deep depth level.
pub const DEEP_DEPTH_LEVEL_OFFSET_ORDERS: usize = 12;

/// Feed response code indicating a BID-side depth packet.
pub const DEEP_DEPTH_FEED_CODE_BID: u8 = 41;

/// Feed response code indicating an ASK-side depth packet.
pub const DEEP_DEPTH_FEED_CODE_ASK: u8 = 51;

/// 20-level depth body size per side (20 levels × 16 bytes).
pub const TWENTY_DEPTH_BODY_SIZE: usize = TWENTY_DEPTH_LEVELS * DEEP_DEPTH_LEVEL_SIZE;

/// 20-level depth total packet size per side (header + body).
pub const TWENTY_DEPTH_PACKET_SIZE: usize = DEEP_DEPTH_HEADER_SIZE + TWENTY_DEPTH_BODY_SIZE;

/// 200-level depth body size per side (200 levels × 16 bytes).
pub const TWO_HUNDRED_DEPTH_BODY_SIZE: usize = TWO_HUNDRED_DEPTH_LEVELS * DEEP_DEPTH_LEVEL_SIZE;

/// 200-level depth total packet size per side (header + body).
pub const TWO_HUNDRED_DEPTH_PACKET_SIZE: usize =
    DEEP_DEPTH_HEADER_SIZE + TWO_HUNDRED_DEPTH_BODY_SIZE;

/// Maximum instruments per 20-depth WebSocket connection (Groww limit).
pub const MAX_INSTRUMENTS_PER_TWENTY_DEPTH_CONNECTION: usize = 50;

/// Maximum instruments per 200-depth WebSocket connection (Groww limit: 1).
pub const MAX_INSTRUMENTS_PER_TWO_HUNDRED_DEPTH_CONNECTION: usize = 1;

/// Subscription request code for 20-level depth feed.
pub const FEED_REQUEST_TWENTY_DEPTH: u8 = 23;

/// Unsubscription request code for full market depth feed (both 20 and 200 level).
/// Groww Annexure: UnsubscribeFullDepth = 25. There is NO code 24.
/// Python SDK (fulldepth.py) also uses 25 for unsubscribe.
pub const FEED_UNSUBSCRIBE_TWENTY_DEPTH: u8 = 25;

// ---------------------------------------------------------------------------
// Deep Depth Protocol — Header Byte Offsets
// ---------------------------------------------------------------------------

/// Message length (u16 LE) offset in deep depth header.
pub const DEEP_DEPTH_HEADER_OFFSET_MSG_LENGTH: usize = 0;

/// Feed response code (u8) offset in deep depth header.
pub const DEEP_DEPTH_HEADER_OFFSET_FEED_CODE: usize = 2;

/// Exchange segment (u8) offset in deep depth header.
pub const DEEP_DEPTH_HEADER_OFFSET_EXCHANGE_SEGMENT: usize = 3;

/// Security ID (u32 LE) offset in deep depth header.
pub const DEEP_DEPTH_HEADER_OFFSET_SECURITY_ID: usize = 4;

/// Message sequence number (u32 LE) offset in deep depth header.
pub const DEEP_DEPTH_HEADER_OFFSET_MSG_SEQUENCE: usize = 8;

// ---------------------------------------------------------------------------
// Live Order Update WebSocket
// Source: Groww API docs, Python SDK.
// Separate JSON-based WebSocket (NOT binary).
// ---------------------------------------------------------------------------

/// Message code for order update WebSocket login request.
pub const ORDER_UPDATE_LOGIN_MSG_CODE: u16 = 42;

/// Read timeout for order update WebSocket (seconds).
/// Order updates are infrequent — use longer timeout than market feed.
pub const ORDER_UPDATE_READ_TIMEOUT_SECS: u64 = 120;

/// Read timeout for order update WebSocket outside market hours (seconds).
/// Off-hours: no orders expected, Groww sends no data. Use 10 minutes to
/// avoid noisy reconnect loops while still detecting dead connections.
pub const ORDER_UPDATE_OFF_HOURS_READ_TIMEOUT_SECS: u64 = 600;

/// Maximum reconnection attempts for order update WebSocket.
pub const ORDER_UPDATE_MAX_RECONNECT_ATTEMPTS: u32 = 10;

/// Initial reconnect delay for order update WebSocket (milliseconds).
pub const ORDER_UPDATE_RECONNECT_INITIAL_DELAY_MS: u64 = 1000;

/// Maximum reconnect delay for order update WebSocket (milliseconds).
pub const ORDER_UPDATE_RECONNECT_MAX_DELAY_MS: u64 = 60000;

/// Timeout for the order update WebSocket auth response (seconds).
/// After sending the LoginReq (MsgCode 42), the server should respond
/// within this window. 10 seconds is generous for a JSON auth handshake.
pub const ORDER_UPDATE_AUTH_TIMEOUT_SECS: u64 = 10;

// ---------------------------------------------------------------------------
// SEBI Compliance
// ---------------------------------------------------------------------------

/// Maximum orders per second allowed by SEBI regulation.
pub const SEBI_MAX_ORDERS_PER_SECOND: u32 = 10;

/// Minimum audit log retention in years (SEBI requirement).
pub const SEBI_AUDIT_RETENTION_YEARS: u32 = 5;

// ---------------------------------------------------------------------------
// OMS — Circuit Breaker
// ---------------------------------------------------------------------------

/// Consecutive API failures before the OMS circuit breaker opens.
pub const OMS_CIRCUIT_BREAKER_FAILURE_THRESHOLD: u32 = 3;

/// Seconds before the OMS circuit breaker transitions from Open to Half-Open.
pub const OMS_CIRCUIT_BREAKER_RESET_SECS: u64 = 30;

/// Maximum length for Groww `correlationId` field.
/// Groww spec: max 30 chars, charset `[a-zA-Z0-9 _-]`.
pub const GROWW_CORRELATION_ID_MAX_LENGTH: usize = 30;

// ---------------------------------------------------------------------------
// Risk — Tick Gap Detection Thresholds
// ---------------------------------------------------------------------------

/// Minimum ticks per security before gap detection activates (warmup phase).
/// Prevents false positives during initial subscription when ticks are sparse.
pub const TICK_GAP_MIN_TICKS_BEFORE_ACTIVE: u32 = 5;

/// Warning-level gap threshold in seconds. Gaps >= this trigger a WARN log.
/// 30 seconds without a tick indicates potential feed degradation.
pub const TICK_GAP_ALERT_THRESHOLD_SECS: u32 = 30;

/// Error-level gap threshold in seconds. Gaps >= this trigger an ERROR log
/// (which routes to Telegram alert). 120 seconds suggests feed disconnection.
pub const TICK_GAP_ERROR_THRESHOLD_SECS: u32 = 120;

/// Stale LTP threshold in seconds. If no tick arrives for a security within
/// this window (wall-clock time), it is considered stale/frozen. 600s = 10 min.
/// Checked periodically by `TickGapTracker::detect_stale_instruments()`.
pub const STALE_LTP_THRESHOLD_SECS: u64 = 600;

// ---------------------------------------------------------------------------
// SSM Parameter Store — Secret Path Prefixes
// ---------------------------------------------------------------------------

/// Base path for all secrets in SSM Parameter Store.
pub const SSM_SECRET_BASE_PATH: &str = "/dlt";

/// SSM service path segment for Groww credentials.
pub const SSM_GROWW_SERVICE: &str = "groww";

/// Groww credentials secret names.
pub const GROWW_CLIENT_ID_SECRET: &str = "client-id";
pub const GROWW_CLIENT_SECRET_SECRET: &str = "client-secret";
pub const GROWW_TOTP_SECRET: &str = "totp-secret";
/// SSM secret name for Groww sandbox client ID (DevPortal).
pub const GROWW_SANDBOX_CLIENT_ID_SECRET: &str = "sandbox-client-id";
/// SSM secret name for Groww sandbox access token (DevPortal, 30-day validity).
pub const GROWW_SANDBOX_TOKEN_SECRET: &str = "sandbox-token";

// ---------------------------------------------------------------------------
// SSM Parameter Store — QuestDB Service
// ---------------------------------------------------------------------------

/// SSM service path segment for QuestDB credentials.
pub const SSM_QUESTDB_SERVICE: &str = "questdb";

/// SSM key for QuestDB PG wire protocol username.
pub const QUESTDB_PG_USER_SECRET: &str = "pg-user";

/// SSM key for QuestDB PG wire protocol password.
pub const QUESTDB_PG_PASSWORD_SECRET: &str = "pg-password";

// ---------------------------------------------------------------------------
// SSM Parameter Store — Grafana Service
// ---------------------------------------------------------------------------

/// SSM service path segment for Grafana credentials.
pub const SSM_GRAFANA_SERVICE: &str = "grafana";

/// SSM key for Grafana admin username.
pub const GRAFANA_ADMIN_USER_SECRET: &str = "admin-user";

/// SSM key for Grafana admin password.
pub const GRAFANA_ADMIN_PASSWORD_SECRET: &str = "admin-password";

// ---------------------------------------------------------------------------
// SSM Parameter Store — Telegram Service
// ---------------------------------------------------------------------------

/// SSM service path segment for Telegram bot credentials.
pub const SSM_TELEGRAM_SERVICE: &str = "telegram";

/// SSM key for Telegram bot token.
pub const TELEGRAM_BOT_TOKEN_SECRET: &str = "bot-token";

/// SSM key for Telegram chat ID.
pub const TELEGRAM_CHAT_ID_SECRET: &str = "chat-id";

// ---------------------------------------------------------------------------
// SSM Parameter Store — SNS Service
// ---------------------------------------------------------------------------

/// SSM service path segment for SNS configuration.
pub const SSM_SNS_SERVICE: &str = "sns";

/// SSM key for the phone number to send SNS SMS alerts to.
/// Value must be in E.164 format: `+<country_code><number>` (e.g., "+919876543210").
pub const SNS_PHONE_NUMBER_SECRET: &str = "phone-number";

// ---------------------------------------------------------------------------
// Network / IP Verification — SSM Constants
// ---------------------------------------------------------------------------

/// SSM service path segment for network configuration.
pub const SSM_NETWORK_SERVICE: &str = "network";

/// SSM key for the expected static public IP (BSNL/ISP-assigned).
/// Value must be a valid IPv4 address string (e.g., "203.0.113.42").
pub const STATIC_IP_SECRET: &str = "static-ip";

/// Primary URL for public IP detection (AWS-owned, plain text response).
pub const PUBLIC_IP_CHECK_PRIMARY_URL: &str = "https://checkip.amazonaws.com"; // APPROVED: infrastructure constant

/// Fallback URL for public IP detection (plain text response).
pub const PUBLIC_IP_CHECK_FALLBACK_URL: &str = "https://api.ipify.org"; // APPROVED: infrastructure constant

/// Timeout in seconds for public IP detection HTTP requests.
pub const PUBLIC_IP_CHECK_TIMEOUT_SECS: u64 = 10;

/// Maximum retry attempts for public IP detection (primary + fallback).
pub const PUBLIC_IP_CHECK_MAX_RETRIES: u32 = 3;

// ---------------------------------------------------------------------------
// Docker Container Naming
// ---------------------------------------------------------------------------

/// Container name prefix for Docker services that consume this crate.
///
/// Retained from the pre-DEC-P0-025 live-trading scaffold for downstream
/// callers; BruteX (the upstream Track 1 backtest engine) does not run
/// Docker services itself.
pub const DOCKER_CONTAINER_PREFIX: &str = "dlt";

// ---------------------------------------------------------------------------
// Ring Buffer Capacities
// ---------------------------------------------------------------------------

/// Tick ring buffer capacity (SPSC, must be power of 2 for rtrb).
pub const TICK_RING_BUFFER_CAPACITY: usize = 65536;

/// Order event ring buffer capacity.
pub const ORDER_EVENT_RING_BUFFER_CAPACITY: usize = 4096;

// ---------------------------------------------------------------------------
// Instrument CSV — Download & Parse
// ---------------------------------------------------------------------------

/// Minimum expected rows in Groww instrument CSV (sanity check for row count).
pub const INSTRUMENT_CSV_MIN_ROWS: usize = 100_000;

/// Minimum expected bytes for a valid instrument CSV download.
/// Groww's CSV is ~40MB; anything under 1MB is suspiciously small.
pub const INSTRUMENT_CSV_MIN_BYTES: usize = 1_000_000;

/// Expected download time for instrument CSV in seconds.
pub const INSTRUMENT_CSV_DOWNLOAD_TIMEOUT_SECS: u64 = 120;

/// Maximum retry attempts for instrument CSV download.
pub const INSTRUMENT_CSV_MAX_DOWNLOAD_RETRIES: usize = 3;

/// Initial backoff delay for instrument CSV download retry in milliseconds.
pub const INSTRUMENT_CSV_RETRY_INITIAL_DELAY_MS: u64 = 2000;

/// Maximum backoff delay for instrument CSV download retry in milliseconds.
pub const INSTRUMENT_CSV_RETRY_MAX_DELAY_MS: u64 = 8000;

/// Filename for the instrument build freshness marker.
/// Written on successful build; contains today's IST date as `YYYY-MM-DD`.
pub const INSTRUMENT_FRESHNESS_MARKER_FILENAME: &str = "instrument-build-date.txt";

/// Filename for the rkyv binary cache of `FnoUniverse`.
/// Written alongside CSV cache; loaded first on market-hours restart for sub-ms recovery.
pub const BINARY_CACHE_FILENAME: &str = "fno-universe.rkyv";

// ---------------------------------------------------------------------------
// Instrument CSV — Column Names (for auto-detection from header)
// ---------------------------------------------------------------------------

pub const CSV_COLUMN_EXCH_ID: &str = "EXCH_ID";
pub const CSV_COLUMN_SEGMENT: &str = "SEGMENT";
pub const CSV_COLUMN_SECURITY_ID: &str = "SECURITY_ID";
pub const CSV_COLUMN_INSTRUMENT: &str = "INSTRUMENT";
pub const CSV_COLUMN_UNDERLYING_SECURITY_ID: &str = "UNDERLYING_SECURITY_ID";
pub const CSV_COLUMN_UNDERLYING_SYMBOL: &str = "UNDERLYING_SYMBOL";
pub const CSV_COLUMN_SYMBOL_NAME: &str = "SYMBOL_NAME";
pub const CSV_COLUMN_DISPLAY_NAME: &str = "DISPLAY_NAME";
pub const CSV_COLUMN_SERIES: &str = "SERIES";
pub const CSV_COLUMN_LOT_SIZE: &str = "LOT_SIZE";
pub const CSV_COLUMN_EXPIRY_DATE: &str = "SM_EXPIRY_DATE";
pub const CSV_COLUMN_STRIKE_PRICE: &str = "STRIKE_PRICE";
pub const CSV_COLUMN_OPTION_TYPE: &str = "OPTION_TYPE";
pub const CSV_COLUMN_TICK_SIZE: &str = "TICK_SIZE";
pub const CSV_COLUMN_EXPIRY_FLAG: &str = "EXPIRY_FLAG";

// ---------------------------------------------------------------------------
// Instrument CSV — Segment & Instrument Codes (text values in CSV)
// ---------------------------------------------------------------------------

/// CSV segment value for indices.
pub const CSV_SEGMENT_INDEX: &str = "I";

/// CSV segment value for equity.
pub const CSV_SEGMENT_EQUITY: &str = "E";

/// CSV segment value for derivatives.
pub const CSV_SEGMENT_DERIVATIVE: &str = "D";

/// CSV exchange value for NSE.
pub const CSV_EXCHANGE_NSE: &str = "NSE";

/// CSV exchange value for BSE.
pub const CSV_EXCHANGE_BSE: &str = "BSE";

/// CSV series value for equity stocks.
pub const CSV_SERIES_EQUITY: &str = "EQ";

/// CSV instrument value: index future.
pub const CSV_INSTRUMENT_FUTIDX: &str = "FUTIDX";

/// CSV instrument value: stock future.
pub const CSV_INSTRUMENT_FUTSTK: &str = "FUTSTK";

/// CSV instrument value: index option.
pub const CSV_INSTRUMENT_OPTIDX: &str = "OPTIDX";

/// CSV instrument value: stock option.
pub const CSV_INSTRUMENT_OPTSTK: &str = "OPTSTK";

/// CSV option type value: call.
pub const CSV_OPTION_TYPE_CALL: &str = "CE";

/// CSV option type value: put.
pub const CSV_OPTION_TYPE_PUT: &str = "PE";

/// Marker substring in symbol names indicating test instruments.
pub const CSV_TEST_SYMBOL_MARKER: &str = "TEST";

// ---------------------------------------------------------------------------
// F&O Universe — Display Indices (23 IDX_I security IDs)
// ---------------------------------------------------------------------------

use super::types::SecurityId;

/// Display indices: (display name, IDX_I security ID, subcategory string).
/// Subscribed for market dashboard and sentiment display.
/// Subcategory must match `IndexSubcategory::as_str()` values.
pub const DISPLAY_INDEX_ENTRIES: &[(&str, SecurityId, &str)] = &[
    ("INDIA VIX", 21, "Volatility"),
    ("NIFTY 100", 17, "BroadMarket"),
    ("NIFTY 200", 18, "BroadMarket"),
    ("NIFTY 500", 19, "BroadMarket"),
    ("NIFTYMCAP50", 20, "MidCap"),
    ("NIFTY MIDCAP 150", 1, "MidCap"),
    ("NIFTY SMALLCAP 50", 22, "SmallCap"),
    ("NIFTY SMALLCAP 100", 5, "SmallCap"),
    ("NIFTY SMALLCAP 250", 3, "SmallCap"),
    ("NIFTY AUTO", 14, "Sectoral"),
    ("NIFTY PVT BANK", 15, "Sectoral"),
    ("NIFTY FMCG", 28, "Sectoral"),
    ("NIFTY ENERGY", 42, "Sectoral"),
    ("NIFTYINFRA", 43, "Sectoral"),
    ("NIFTYIT", 29, "Sectoral"),
    ("NIFTY MEDIA", 30, "Sectoral"),
    ("NIFTY METAL", 31, "Sectoral"),
    ("NIFTY MNC", 44, "Sectoral"),
    ("NIFTY PHARMA", 32, "Sectoral"),
    ("NIFTY PSU BANK", 33, "Sectoral"),
    ("NIFTY REALTY", 34, "Sectoral"),
    ("NIFTY SERV SECTOR", 46, "Sectoral"),
    ("NIFTY CONSUMPTION", 40, "Thematic"),
];

/// Number of display indices.
pub const DISPLAY_INDEX_COUNT: usize = 23;

/// Total subscribed indices: 8 F&O + 23 Display = 31.
pub const TOTAL_SUBSCRIBED_INDEX_COUNT: usize = 31;

// ---------------------------------------------------------------------------
// F&O Universe — Full Chain Indices
// ---------------------------------------------------------------------------

/// Index symbols that get full option chain subscriptions.
pub const FULL_CHAIN_INDEX_SYMBOLS: &[&str] =
    &["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"];

/// Number of full-chain indices.
pub const FULL_CHAIN_INDEX_COUNT: usize = 5;

// ---------------------------------------------------------------------------
// F&O Universe — Index Aliases (FNO symbol → IDX_I symbol)
// ---------------------------------------------------------------------------

/// Aliases for indices whose FNO underlying symbol differs from IDX_I row symbol.
/// Format: (fno_underlying_symbol, idx_i_symbol_name).
pub const INDEX_SYMBOL_ALIASES: &[(&str, &str)] =
    &[("NIFTYNXT50", "NIFTY NEXT 50"), ("SENSEX50", "SNSX50")];

// ---------------------------------------------------------------------------
// F&O Universe — Validation: Must-Exist Price IDs
// ---------------------------------------------------------------------------

/// Known index security IDs that MUST exist after universe build.
/// (underlying_symbol, expected IDX_I security_id).
pub const VALIDATION_MUST_EXIST_INDICES: &[(&str, SecurityId)] = &[
    ("NIFTY", 13),
    ("BANKNIFTY", 25),
    ("FINNIFTY", 27),
    ("MIDCPNIFTY", 442),
    ("NIFTYNXT50", 38),
    ("SENSEX", 51),
    ("BANKEX", 69),
    ("SENSEX50", 83),
];

/// Known equity security IDs that MUST exist after universe build.
/// (symbol, expected NSE_EQ security_id).
pub const VALIDATION_MUST_EXIST_EQUITIES: &[(&str, SecurityId)] = &[("RELIANCE", 2885)];

/// Known stock symbols that MUST be present in the F&O universe.
pub const VALIDATION_MUST_EXIST_FNO_STOCKS: &[&str] = &["RELIANCE", "HDFCBANK", "INFY", "TCS"];

/// Minimum expected number of F&O stock underlyings.
pub const VALIDATION_FNO_STOCK_MIN_COUNT: usize = 150;

/// Maximum expected number of F&O stock underlyings.
pub const VALIDATION_FNO_STOCK_MAX_COUNT: usize = 300;

/// I-P0-02: Minimum expected derivative contract count after universe build.
/// A truncated CSV (< 100 derivatives) is almost certainly corrupt or incomplete.
/// Real NSE F&O universe has 5000+ contracts on any given day.
pub const VALIDATION_MIN_DERIVATIVE_COUNT: usize = 100;

// ---------------------------------------------------------------------------
// Application Identity
// ---------------------------------------------------------------------------

/// Application name used in logs, metrics, and tracing.
pub const APPLICATION_NAME: &str = "brutex";

/// Application version (updated with each release).
pub const APPLICATION_VERSION: &str = env!("CARGO_PKG_VERSION");

// ---------------------------------------------------------------------------
// QuestDB ILP — Table Names
// ---------------------------------------------------------------------------

/// QuestDB table: daily instrument build health and statistics.
pub const QUESTDB_TABLE_BUILD_METADATA: &str = "instrument_build_metadata";

/// QuestDB table: daily F&O underlying snapshots.
pub const QUESTDB_TABLE_FNO_UNDERLYINGS: &str = "fno_underlyings";

/// QuestDB table: daily derivative contract snapshots.
pub const QUESTDB_TABLE_DERIVATIVE_CONTRACTS: &str = "derivative_contracts";

/// QuestDB table: daily subscribed index snapshots (8 F&O + 23 Display = 31).
pub const QUESTDB_TABLE_SUBSCRIBED_INDICES: &str = "subscribed_indices";

/// QuestDB table: NSE trading calendar (holidays + Muhurat sessions).
pub const QUESTDB_TABLE_NSE_HOLIDAYS: &str = "nse_holidays";

/// QuestDB table: NSE index constituency (daily snapshot from niftyindices.com).
pub const QUESTDB_TABLE_INDEX_CONSTITUENTS: &str = "index_constituents";

// ---------------------------------------------------------------------------
// QuestDB ILP — Ingestion Configuration
// ---------------------------------------------------------------------------

/// Number of ILP rows to buffer before flushing to QuestDB.
/// Prevents unbounded memory growth when writing ~150K derivative contracts.
pub const ILP_FLUSH_BATCH_SIZE: usize = 10_000;

// ---------------------------------------------------------------------------
// Timezone — IST (India Standard Time)
// ---------------------------------------------------------------------------

/// IST offset from UTC in seconds (5 hours 30 minutes = 19,800 seconds).
pub const IST_UTC_OFFSET_SECONDS: i32 = 19_800;

// ---------------------------------------------------------------------------
// Market Hours — Tick Persistence Window
// ---------------------------------------------------------------------------
// Only ticks with exchange timestamps inside [09:00, 15:30) IST are persisted
// to QuestDB. Pre-market and post-market ticks still flow through broadcast
// and candle aggregation but are NOT stored.

/// Seconds-of-day (IST) at which tick persistence starts: 09:00:00 = 9 × 3600.
pub const TICK_PERSIST_START_SECS_OF_DAY_IST: u32 = 32_400;

/// Seconds-of-day (IST) at which tick persistence ends: 15:30:00 = 15 × 3600 + 30 × 60.
/// The end is **exclusive** — a tick at exactly 15:30:00 is NOT persisted.
pub const TICK_PERSIST_END_SECS_OF_DAY_IST: u32 = 55_800;

/// Seconds in a day (86,400). Used for modulo arithmetic in persist window check.
pub const SECONDS_PER_DAY: u32 = 86_400;

/// Drain buffer (seconds) after market close before aborting WebSocket handles.
/// Allows in-flight ticks (last 15:29 candle) to reach the tick processor channel
/// before the WebSocket read loop is killed.
pub const MARKET_CLOSE_DRAIN_BUFFER_SECS: u64 = 2;

/// Maximum acceptable boot time in seconds. If exceeded, CRITICAL Telegram alert fires.
/// Individual boot steps have their own timeouts; this is the overall ceiling.
pub const BOOT_TIMEOUT_SECS: u64 = 120;

// ---------------------------------------------------------------------------
// Authentication — TOTP Configuration
// ---------------------------------------------------------------------------

/// TOTP digit count (Groww uses 6-digit codes).
pub const TOTP_DIGITS: usize = 6;

/// TOTP time period in seconds (standard 30-second window).
pub const TOTP_PERIOD_SECS: u64 = 30;

/// TOTP skew tolerance (number of periods to accept before/after current).
pub const TOTP_SKEW: u8 = 1;

/// Maximum TOTP retry attempts before treating as permanent failure.
/// TOTP can fail due to window boundary timing — retrying with a fresh
/// 30-second window resolves transient issues without masking bad secrets.
pub const TOTP_MAX_RETRIES: u32 = 2;

// ---------------------------------------------------------------------------
// Authentication — Groww REST API Endpoint Paths
// ---------------------------------------------------------------------------

/// Path for initial token generation (appended to auth_base_url).
/// Endpoint: POST <https://auth.groww.co/app/generateAccessToken>
pub const GROWW_GENERATE_TOKEN_PATH: &str = "/app/generateAccessToken";

/// Path for token renewal (appended to rest_api_base_url).
/// Endpoint: GET <https://api.groww.co/v2/RenewToken>
pub const GROWW_RENEW_TOKEN_PATH: &str = "/RenewToken";

/// Path for intraday minute candle data (appended to rest_api_base_url).
/// Endpoint: POST <https://api.groww.co/v2/charts/intraday>
pub const GROWW_CHARTS_INTRADAY_PATH: &str = "/charts/intraday";

/// Path for daily candle data (appended to rest_api_base_url).
/// Endpoint: POST <https://api.groww.co/v2/charts/historical>
pub const GROWW_CHARTS_HISTORICAL_PATH: &str = "/charts/historical";

// ---------------------------------------------------------------------------
// Authentication — User Profile & IP Management Endpoints
// ---------------------------------------------------------------------------

/// Path for user profile (appended to rest_api_base_url).
/// Endpoint: GET <https://api.groww.co/v2/profile>
pub const GROWW_USER_PROFILE_PATH: &str = "/profile";

/// Path to set static IP for order APIs (appended to rest_api_base_url).
/// Endpoint: POST <https://api.groww.co/v2/ip/setIP>
pub const GROWW_SET_IP_PATH: &str = "/ip/setIP";

/// Path to modify static IP for order APIs (appended to rest_api_base_url).
/// Endpoint: PUT <https://api.groww.co/v2/ip/modifyIP>
/// WARNING: 7-day cooldown after modification.
pub const GROWW_MODIFY_IP_PATH: &str = "/ip/modifyIP";

/// Path to get current static IP configuration (appended to rest_api_base_url).
/// Endpoint: GET <https://api.groww.co/v2/ip/getIP>
pub const GROWW_GET_IP_PATH: &str = "/ip/getIP";

// ---------------------------------------------------------------------------
// Portfolio & Positions — REST API Endpoint Paths
// ---------------------------------------------------------------------------

/// Path for holdings list (appended to rest_api_base_url).
/// Endpoint: GET <https://api.groww.co/v2/holdings>
pub const GROWW_HOLDINGS_PATH: &str = "/holdings";

/// Path for positions list (appended to rest_api_base_url).
/// Endpoint: GET <https://api.groww.co/v2/positions>
pub const GROWW_POSITIONS_PATH: &str = "/positions";

/// Path for position conversion (appended to rest_api_base_url).
/// Endpoint: POST <https://api.groww.co/v2/positions/convert>
pub const GROWW_POSITIONS_CONVERT_PATH: &str = "/positions/convert";

// ---------------------------------------------------------------------------
// Funds & Margin — REST API Endpoint Paths
// ---------------------------------------------------------------------------

/// Path for single-instrument margin calculator (appended to rest_api_base_url).
/// Endpoint: POST <https://api.groww.co/v2/margincalculator>
pub const GROWW_MARGIN_CALCULATOR_PATH: &str = "/margincalculator";

/// Path for multi-instrument margin calculator (appended to rest_api_base_url).
/// Endpoint: POST <https://api.groww.co/v2/margincalculator/multi>
pub const GROWW_MARGIN_CALCULATOR_MULTI_PATH: &str = "/margincalculator/multi";

/// Path for fund limit query (appended to rest_api_base_url).
/// Endpoint: GET <https://api.groww.co/v2/fundlimit>
pub const GROWW_FUND_LIMIT_PATH: &str = "/fundlimit";

/// Path for kill switch activate/deactivate (appended to rest_api_base_url).
/// Endpoint: POST <https://api.groww.co/v2/killswitch?killSwitchStatus=ACTIVATE|DEACTIVATE>
pub const GROWW_KILL_SWITCH_PATH: &str = "/killswitch";

/// Path for P&L-based exit configuration (appended to rest_api_base_url).
/// Endpoint: POST/DELETE/GET <https://api.groww.co/v2/pnlExit>
pub const GROWW_PNL_EXIT_PATH: &str = "/pnlExit";

// ---------------------------------------------------------------------------
// Full Market Depth — WebSocket Base URLs
// ---------------------------------------------------------------------------

/// 20-level depth WebSocket base URL.
/// Full URL: `wss://depth-api-feed.groww.co/twentydepth?token=TOKEN&clientId=CLIENT_ID&authType=2`
pub const GROWW_TWENTY_DEPTH_WS_BASE_URL: &str = "wss://depth-api-feed.groww.co/twentydepth"; // APPROVED: infrastructure constant

/// 200-level depth WebSocket base URL.
/// Full URL: `wss://full-depth-api.groww.co/twohundreddepth?token=TOKEN&clientId=CLIENT_ID&authType=2`
///
/// NOTE: Groww support confirmed (2026-04-10, Ticket #5519522) the correct path is
/// `/twohundreddepth`. Previous SDK-based root path `/` caused `ResetWithoutClosingHandshake`.
/// Groww also confirmed: use a Security ID close to current market price (ATM), not far OTM.
pub const GROWW_TWO_HUNDRED_DEPTH_WS_BASE_URL: &str = "wss://full-depth-api.groww.co/twohundreddepth"; // APPROVED: infrastructure constant — confirmed by Groww support ticket #5519522

// ---------------------------------------------------------------------------
// Historical Data — Candle Fetch Constants
// ---------------------------------------------------------------------------

/// 1-minute candle interval identifier for Groww intraday API.
pub const GROWW_CANDLE_INTERVAL_1MIN: &str = "1";

/// 5-minute candle interval identifier for Groww intraday API.
pub const GROWW_CANDLE_INTERVAL_5MIN: &str = "5";

/// 15-minute candle interval identifier for Groww intraday API.
pub const GROWW_CANDLE_INTERVAL_15MIN: &str = "15";

/// 25-minute candle interval identifier for Groww intraday API.
/// Per docs/groww-ref/05-historical-data.md: valid intervals are "1", "5", "15", "25", "60".
pub const GROWW_CANDLE_INTERVAL_25MIN: &str = "25";

/// 60-minute (1-hour) candle interval identifier for Groww intraday API.
pub const GROWW_CANDLE_INTERVAL_60MIN: &str = "60";

/// Maximum days of intraday data per API request (Groww limit: 90 days).
pub const GROWW_INTRADAY_MAX_DAYS_PER_REQUEST: u32 = 90;

/// NSE continuous trading session start time as HH:MM:SS in IST.
///
/// Used for Groww historical API `fromDate` and cross-verification baseline.
/// Groww's historical intraday API returns candles starting from 09:15 (continuous
/// trading). Pre-market data (09:00–09:14) is only available via live WebSocket.
///
/// For live data collection start, see `data_collection_start` in config (09:00).
/// For tick/candle persistence window, see `TICK_PERSIST_START_SECS_OF_DAY_IST` (09:00).
pub const MARKET_OPEN_TIME_IST: &str = "09:15:00";

/// NSE F&O last 1-minute candle start time as HH:MM:SS in IST.
/// The last 1-minute candle runs from 15:29:00 to 15:29:59 IST.
pub const MARKET_LAST_CANDLE_START_IST: &str = "15:29:00";

/// Market close time for intraday API toDate parameter (exclusive upper bound).
/// Groww intraday API uses datetime format: "YYYY-MM-DD 15:30:00" is exclusive,
/// meaning the last candle returned starts at 15:29.
pub const MARKET_CLOSE_TIME_IST_EXCLUSIVE: &str = "15:30:00";

/// Daily reset signal time (IST). After market close at 15:30,
/// historical re-fetch + cross-verification runs. At 16:00 IST the daily
/// reset signal fires (candle aggregator reset, indicator reset, etc.).
/// NOTE: Auto-shutdown is DISABLED. App runs 24/7 until manual Ctrl+C.
/// For AWS instance lifecycle, use systemd timer or cron for restart.
pub const APP_DAILY_RESET_TIME_IST: &str = "16:00:00";

/// Number of 1-minute candles in the cross-verification window (09:15 to 15:29 = 375).
///
/// Used by `cross_verify.rs` to validate historical vs live candle coverage.
/// This covers only the continuous trading session (09:15–15:29) because Groww's
/// historical intraday API returns data starting from 09:15.
///
/// Live WebSocket data is collected and stored from 09:00 (pre-market), but
/// pre-market candles (09:00–09:14) have no historical counterpart for comparison.
/// Each candle covers [HH:MM:00, HH:MM:59]. Last candle at 15:29.
pub const CANDLES_PER_TRADING_DAY: usize = 375;

/// Batch size for QuestDB ILP flushes during historical candle ingestion.
pub const CANDLE_FLUSH_BATCH_SIZE: usize = 500;

/// Timeframe identifier for 1-minute candles in `historical_candles` table.
pub const TIMEFRAME_1M: &str = "1m";

/// Timeframe identifier for 5-minute candles in `historical_candles` table.
pub const TIMEFRAME_5M: &str = "5m";

/// Timeframe identifier for 15-minute candles in `historical_candles` table.
pub const TIMEFRAME_15M: &str = "15m";

/// Timeframe identifier for 60-minute candles in `historical_candles` table.
pub const TIMEFRAME_60M: &str = "60m";

/// Timeframe identifier for daily candles in `historical_candles` table.
pub const TIMEFRAME_1D: &str = "1d";

/// All intraday timeframes to fetch from Groww API.
/// Each tuple: (interval for API request, timeframe label for storage).
pub const INTRADAY_TIMEFRAMES: &[(&str, &str)] = &[
    ("1", "1m"),
    ("5", "5m"),
    ("15", "15m"),
    ("25", "25m"),
    ("60", "60m"),
];

// ---------------------------------------------------------------------------
// Authentication — SSM Path Construction
// ---------------------------------------------------------------------------

/// Default environment name for SSM path construction.
/// Overridden by `ENVIRONMENT` env var if present.
pub const DEFAULT_SSM_ENVIRONMENT: &str = "dev";

// ---------------------------------------------------------------------------
// Authentication — Circuit Breaker
// ---------------------------------------------------------------------------

/// Maximum consecutive token renewal failures before circuit breaker trips.
pub const TOKEN_RENEWAL_CIRCUIT_BREAKER_THRESHOLD: u32 = 3;

/// Circuit breaker reset timeout in seconds (try again after this).
pub const TOKEN_RENEWAL_CIRCUIT_BREAKER_RESET_SECS: u64 = 60;

/// Groww `generateAccessToken` cooldown in seconds.
///
/// Groww enforces an undocumented 2-minute cooldown between token generation
/// requests. We use 125 seconds (2 min + 5 sec safety margin) to ensure
/// we never hit the rate limit on retry.
pub const GROWW_TOKEN_GENERATION_COOLDOWN_SECS: u64 = 125;

/// Maximum backoff delay for boot-time auth retry in seconds.
///
/// Exponential backoff is capped at this value to prevent unbounded waits.
pub const AUTH_RETRY_MAX_BACKOFF_SECS: u64 = 300;

// ---------------------------------------------------------------------------
// Groww WebSocket V2 — Binary Packet Byte Offsets
// Source: SDK struct.unpack format strings, verified against Python SDK.
// CRITICAL: Quote and Full packets DIVERGE at offset 34.
// ---------------------------------------------------------------------------

// --- Header offsets (shared by ALL packets) ---

/// Response code byte position in binary header.
pub const HEADER_OFFSET_RESPONSE_CODE: usize = 0;

/// Message length (u16 LE) position in binary header.
pub const HEADER_OFFSET_MESSAGE_LENGTH: usize = 1;

/// Exchange segment byte position in binary header.
pub const HEADER_OFFSET_EXCHANGE_SEGMENT: usize = 3;

/// Security ID (u32 LE) position in binary header.
pub const HEADER_OFFSET_SECURITY_ID: usize = 4;

// --- Market Depth standalone packet body offsets (112 bytes total) ---
// Format: <BHBIf100s> — same LTP offset as Ticker, depth at offset 12.

/// LTP (f32 LE) offset in Market Depth standalone packet.
pub const MARKET_DEPTH_OFFSET_LTP: usize = 8;

/// Start of 5-level market depth (100 bytes) in Market Depth standalone packet.
pub const MARKET_DEPTH_OFFSET_DEPTH_START: usize = 12;

// --- Ticker packet body offsets (16 bytes total) ---

/// Last traded price (f32 LE) offset in Ticker packet.
pub const TICKER_OFFSET_LTP: usize = 8;

/// Last trade time (u32 LE, epoch seconds) offset in Ticker packet.
pub const TICKER_OFFSET_LTT: usize = 12;

// --- Quote packet body offsets (50 bytes total) ---
// Format: <BHBIfHIfIIIffff

/// LTP (f32 LE) offset in Quote packet.
pub const QUOTE_OFFSET_LTP: usize = 8;

/// Last trade quantity (u16 LE) offset in Quote packet.
pub const QUOTE_OFFSET_LTQ: usize = 12;

/// Last trade time (u32 LE) offset in Quote packet.
pub const QUOTE_OFFSET_LTT: usize = 14;

/// Average traded price (f32 LE) offset in Quote packet.
pub const QUOTE_OFFSET_ATP: usize = 18;

/// Cumulative volume (u32 LE) offset in Quote packet.
pub const QUOTE_OFFSET_VOLUME: usize = 22;

/// Total sell quantity (u32 LE) offset in Quote packet.
pub const QUOTE_OFFSET_TOTAL_SELL_QTY: usize = 26;

/// Total buy quantity (u32 LE) offset in Quote packet.
pub const QUOTE_OFFSET_TOTAL_BUY_QTY: usize = 30;

/// Day open (f32 LE) offset in Quote packet — DIVERGES from Full at this offset!
pub const QUOTE_OFFSET_OPEN: usize = 34;

/// Previous close (f32 LE) offset in Quote packet.
pub const QUOTE_OFFSET_CLOSE: usize = 38;

/// Day high (f32 LE) offset in Quote packet.
pub const QUOTE_OFFSET_HIGH: usize = 42;

/// Day low (f32 LE) offset in Quote packet.
pub const QUOTE_OFFSET_LOW: usize = 46;

// --- Full packet body offsets (162 bytes total) ---
// Format: <BHBIfHIfIIIIIIffff100s
// CRITICAL: Shares layout 0-33 with Quote, then DIVERGES at offset 34.
// Full puts OI u32 fields at 34-45, then OHLC floats at 46-61, then depth.

/// Open interest (u32 LE) offset in Full packet — Quote has Open(f32) here!
pub const FULL_OFFSET_OI: usize = 34;

/// OI day high (u32 LE) offset in Full packet — Quote has Close(f32) here!
pub const FULL_OFFSET_OI_DAY_HIGH: usize = 38;

/// OI day low (u32 LE) offset in Full packet — Quote has High(f32) here!
pub const FULL_OFFSET_OI_DAY_LOW: usize = 42;

/// Day open (f32 LE) offset in Full packet.
pub const FULL_OFFSET_OPEN: usize = 46;

/// Previous close (f32 LE) offset in Full packet.
pub const FULL_OFFSET_CLOSE: usize = 50;

/// Day high (f32 LE) offset in Full packet.
pub const FULL_OFFSET_HIGH: usize = 54;

/// Day low (f32 LE) offset in Full packet.
pub const FULL_OFFSET_LOW: usize = 58;

/// Start of 5-level market depth (100 bytes) in Full packet.
pub const FULL_OFFSET_DEPTH_START: usize = 62;

// --- OI packet body offset (12 bytes total) ---

/// Open interest (u32 LE) offset in standalone OI packet.
pub const OI_OFFSET_VALUE: usize = 8;

// --- Previous Close packet body offsets (16 bytes total) ---

/// Previous close price (f32 LE) offset.
pub const PREV_CLOSE_OFFSET_PRICE: usize = 8;

/// Previous OI (u32 LE) offset.
pub const PREV_CLOSE_OFFSET_OI: usize = 12;

// --- Disconnect packet body offset (10 bytes total) ---

/// Disconnect code (u16 LE) offset.
pub const DISCONNECT_OFFSET_CODE: usize = 8;

// ---------------------------------------------------------------------------
// QuestDB ILP — Table Names
// ---------------------------------------------------------------------------

/// QuestDB table: raw tick data from WebSocket feed.
pub const QUESTDB_TABLE_TICKS: &str = "ticks";

/// QuestDB table: 5-level market depth snapshots from Full (code 8) and Market Depth (code 3) packets.
pub const QUESTDB_TABLE_MARKET_DEPTH: &str = "market_depth";

/// QuestDB table: previous close reference data from code 6 packets.
pub const QUESTDB_TABLE_PREVIOUS_CLOSE: &str = "previous_close";

/// QuestDB table: multi-timeframe OHLCV candles from Groww historical API.
/// Stores 1m, 5m, 15m, 60m intraday candles and daily candles in a single table.
/// Discriminated by `timeframe` SYMBOL column. DEDUP on `(ts, security_id, timeframe)`.
pub const QUESTDB_TABLE_HISTORICAL_CANDLES: &str = "historical_candles";

/// QuestDB table: 1-second OHLCV candles aggregated from live ticks.
pub const QUESTDB_TABLE_CANDLES_1S: &str = "candles_1s";

/// Calendar alignment offset for QuestDB materialized views.
///
/// All timestamps are stored as IST epoch values. Live WebSocket data arrives
/// as IST epoch seconds (stored directly). Historical REST data has +19800s
/// applied. No offset needed — midnight "UTC" in our convention IS midnight IST.
pub const QUESTDB_IST_ALIGN_OFFSET: &str = "00:00";

// ---------------------------------------------------------------------------
// Pipeline — Tick Processing Constants
// ---------------------------------------------------------------------------

/// Default tick batch flush size for QuestDB ILP writes.
pub const TICK_FLUSH_BATCH_SIZE: usize = 1000;

/// Default tick flush interval in milliseconds.
pub const TICK_FLUSH_INTERVAL_MS: u64 = 1000;

/// Tick ring buffer capacity for QuestDB outage resilience.
/// Holds ticks in memory when QuestDB is down, drains on recovery.
///
/// Sized for 25K instruments (5 WS connections × 5,000 each):
/// - 600,000 ticks × ~112 bytes = ~64MB RAM
/// - At 10K ticks/sec (realistic peak) = 60 seconds before disk spill
/// - At 25K ticks/sec (extreme peak) = 24 seconds before disk spill
/// - Full-day QuestDB outage: disk spill handles overflow (~23 GB for 10K/sec)
pub const TICK_BUFFER_CAPACITY: usize = 600_000;

/// High watermark threshold for tick ring buffer (80% of capacity).
/// When buffer occupancy exceeds this, a WARN-level alert fires once
/// to signal imminent disk spill. Triggers Telegram via Loki ERROR rule.
/// 80% of 600,000 = 480,000 ticks.
pub const TICK_BUFFER_HIGH_WATERMARK: usize = TICK_BUFFER_CAPACITY * 4 / 5; // 480,000

/// Minimum free disk space (bytes) to log a warning before spill write.
/// 100 MB — below this, a WARN fires on each spill open to alert operator
/// that prolonged QuestDB outage may exhaust disk.
pub const TICK_SPILL_MIN_DISK_SPACE_BYTES: u64 = 100 * 1024 * 1024;

/// OMS HTTP client timeout (seconds). Applied at the client level as a default
/// for all Groww order API calls. Individual endpoints may override with shorter
/// per-request timeouts. 5 seconds covers normal API latency (50-200ms) with
/// margin for Groww-side processing spikes.
pub const OMS_HTTP_TIMEOUT_SECS: u64 = 5;

/// OMS HTTP connect timeout (seconds). TCP + TLS handshake deadline.
pub const OMS_HTTP_CONNECT_TIMEOUT_SECS: u64 = 3;

/// OMS HTTP connection pool idle timeout (seconds). Idle connections are
/// dropped after this duration to avoid stale sockets.
pub const OMS_HTTP_POOL_IDLE_TIMEOUT_SECS: u64 = 90;

/// Broadcast channel capacity for cold-path tick consumers (trading pipeline,
/// tick persistence, candle aggregation). Must be large enough to absorb bursts
/// during high-volatility events without lagging cold-path consumers.
///
/// CRITICAL: Overflow here = permanent tick loss (tokio broadcast evicts oldest).
/// Sized for 25K instruments:
/// - 262,144 ticks × ~112 bytes = ~28MB RAM
/// - At 10K ticks/sec = 26 seconds headroom before eviction
/// - At 25K ticks/sec = 10 seconds headroom
pub const TICK_BROADCAST_CAPACITY: usize = 262_144;

/// Broadcast channel capacity for order update consumers (OMS, risk engine).
/// 256 order updates × ~512 bytes each = ~128KB. At ~10 orders/sec = ~25 seconds.
pub const ORDER_UPDATE_BROADCAST_CAPACITY: usize = 256;

/// Resilience ring buffer capacity for live candle writer.
/// Holds candles in memory when QuestDB is down, drains on recovery.
///
/// Sized for 25K instruments (1 candle/instrument/sec):
/// - 200,000 candles × ~76 bytes = ~14.5MB RAM
/// - At 25K candles/sec = 8 seconds before disk spill
/// - Candle data is lower priority than raw ticks; 8s is sufficient
///   as disk spill handles overflow seamlessly.
pub const CANDLE_BUFFER_CAPACITY: usize = 200_000;

/// Option Chain API minimum request interval in seconds (Groww limit: 1 req / 3 sec).
pub const OPTION_CHAIN_MIN_REQUEST_INTERVAL_SECS: u64 = 3;

/// Option Chain API HTTP request timeout in seconds.
pub const OPTION_CHAIN_REQUEST_TIMEOUT_SECS: u64 = 10;

/// Default depth batch flush size for QuestDB ILP writes.
/// Each depth snapshot writes 5 rows (one per level), so effective row count = batch × 5.
pub const DEPTH_FLUSH_BATCH_SIZE: usize = 200;

/// Resilience ring buffer capacity for depth persistence writer.
/// Holds depth snapshots in memory when QuestDB is down, drains on recovery.
///
/// Sized for 200 instruments (4 connections × 50 instruments):
/// - 100,000 snapshots × 116 bytes = ~11MB RAM
/// - At ~250 snapshots/sec = ~6.7 minutes before disk spill
/// - Depth data arrives at lower rate than ticks; generous headroom.
pub const DEPTH_BUFFER_CAPACITY: usize = 100_000;

// ---------------------------------------------------------------------------
// Pipeline — Tick Validation Constants
// ---------------------------------------------------------------------------

/// Minimum valid exchange timestamp (epoch seconds).
/// Any tick with exchange_timestamp below this is a junk/initialization frame.
/// 946684800 = 2000-01-01T00:00:00Z — safely before any real market data.
pub const MINIMUM_VALID_EXCHANGE_TIMESTAMP: u32 = 946_684_800;

// ---------------------------------------------------------------------------
// IST Timezone Offset (i64)
// ---------------------------------------------------------------------------

/// IST offset from UTC in seconds (5 hours 30 minutes) as i64.
/// Used for: (1) converting Historical REST API UTC timestamps to IST,
/// (2) converting `received_at_nanos` (system clock UTC) to IST basis.
/// NOT needed for WebSocket `exchange_timestamp` which is already IST.
pub const IST_UTC_OFFSET_SECONDS_I64: i64 = 19_800;

/// IST offset from UTC in nanoseconds (5h 30m = 19,800,000,000,000 ns).
/// Used for converting `received_at_nanos` (system clock UTC) to IST basis.
pub const IST_UTC_OFFSET_NANOS: i64 = 19_800_000_000_000;

// ---------------------------------------------------------------------------
// Sandbox Guard — Live Trading Date Lock
// ---------------------------------------------------------------------------

/// Earliest date (IST) when `mode = "live"` is permitted.
/// Before this date, config validation rejects live mode at startup.
/// Change these constants ONLY with explicit approval from Parthiban.
pub const LIVE_TRADING_EARLIEST_YEAR: i32 = 2026;
pub const LIVE_TRADING_EARLIEST_MONTH: u32 = 7;
pub const LIVE_TRADING_EARLIEST_DAY: u32 = 1;

// ---------------------------------------------------------------------------
// Periodic Health Check
// ---------------------------------------------------------------------------

/// Interval between periodic health checks (disk space, memory RSS) in seconds.
pub const PERIODIC_HEALTH_CHECK_INTERVAL_SECS: u64 = 300;

/// Maximum age for spill files after successful drain (7 days in seconds).
/// Spill files older than this are auto-deleted during the periodic health check.
pub const SPILL_FILE_MAX_AGE_SECS: u64 = 7 * 24 * 3600;

// ---------------------------------------------------------------------------
// Subscription Planner — ATM Strike Range
// ---------------------------------------------------------------------------

/// Number of strikes above ATM to subscribe for stock options.
pub const STOCK_ATM_STRIKES_ABOVE: usize = 10;

/// Number of strikes below ATM to subscribe for stock options.
pub const STOCK_ATM_STRIKES_BELOW: usize = 10;

/// Total option contracts per stock per side (CE + PE) at one expiry:
/// ATM + 10 above + 10 below = 21 strikes × 2 sides = 42, plus 1 future = 43.
pub const STOCK_CONTRACTS_PER_EXPIRY: usize =
    (1 + STOCK_ATM_STRIKES_ABOVE + STOCK_ATM_STRIKES_BELOW) * 2 + 1;

// ---------------------------------------------------------------------------
// Shutdown
// ---------------------------------------------------------------------------

/// Maximum time to wait for the tick processor to complete its final QuestDB
/// flush during graceful shutdown. After this, the processor is force-aborted.
pub const GRACEFUL_SHUTDOWN_TIMEOUT_SECS: u64 = 10;

/// Maximum time to wait for initial token acquisition at startup (seconds).
/// Prevents indefinite hang if Groww API is unreachable on boot.
pub const TOKEN_INIT_TIMEOUT_SECS: u64 = 300;

/// Maximum consecutive renewal circuit-breaker cycles before the renewal loop
/// halts and raises a critical alert. Prevents infinite retry with expired token.
pub const TOKEN_RENEWAL_MAX_CIRCUIT_BREAKER_CYCLES: u32 = 5;

/// Timeout for sending a frame into the tick processor channel (seconds).
/// Used as backpressure timeout: if channel is full, WS read blocks for
/// up to this duration. Must be less than Groww's ping timeout (40s).
pub const FRAME_SEND_TIMEOUT_SECS: u64 = 5;

/// Maximum backpressure wait time (seconds) when SPSC channel is full.
/// If the tick processor is frozen for this long, the frame is lost and a
/// CRITICAL error is logged. Must be less than Groww's ping timeout (40s)
/// to prevent WebSocket disconnection during backpressure.
/// 30s < 40s (Groww timeout) gives 10s safety margin.
pub const FRAME_BACKPRESSURE_TIMEOUT_SECS: u64 = 30;

/// SPSC frame channel capacity (WebSocket reader → tick processor).
/// All 5 WS connections multiplex frames into this single channel.
///
/// Sized for 25K instruments:
/// - 131,072 slots × ~1.5KB avg frame = ~187MB RAM
/// - At 10K ticks/sec = 13 seconds backpressure-free headroom
/// - At 25K ticks/sec = 5.2 seconds headroom
/// - On overflow: backpressure (blocks WS read, WS survives 40s ping timeout)
/// - ZERO frame loss guarantee — backpressure, never drop.
pub const FRAME_CHANNEL_CAPACITY: usize = 131_072;

/// Power-of-two exponent for the tick deduplication ring buffer.
///
/// Size = 2^16 = 65,536 slots x 8 bytes = 512 KiB.
/// Catches exact duplicate ticks (same security_id + timestamp + LTP)
/// resent by Groww on WebSocket reconnection.
///
/// False negatives (missed duplicates due to hash collision/eviction) are safe:
/// QuestDB `DEDUP UPSERT KEYS(ts, security_id)` is the authoritative dedup layer.
pub const DEDUP_RING_BUFFER_POWER: u32 = 16;

// ---------------------------------------------------------------------------
// Indicator Engine — Ring Buffer & Warmup
// ---------------------------------------------------------------------------

/// Ring buffer capacity for windowed indicators (SMA, Stochastic, MFI).
/// Must be a power of two for bitmask-based O(1) indexing.
/// 64 slots covers periods up to 64 (SMA-50, RSI-14, ATR-14, etc.).
pub const INDICATOR_RING_BUFFER_CAPACITY: usize = 64;

/// Minimum ticks required before indicator values are considered reliable.
/// Equal to the longest standard indicator warmup (EMA-26 needs ~26 ticks).
/// After this many ticks, `IndicatorState::is_warm()` returns true.
pub const MAX_INDICATOR_WARMUP_TICKS: u16 = 30;

/// Maximum number of instruments the indicator engine can track.
/// Pre-allocated at startup; O(1) index lookup by security_id.
/// 25,000 covers full Groww universe (5 connections × 5,000 instruments).
pub const MAX_INDICATOR_INSTRUMENTS: usize = 25_000;

/// Maximum number of strategy FSM instances.
pub const MAX_STRATEGY_INSTANCES: usize = 256;

// ---------------------------------------------------------------------------
// Options Greeks — Black-Scholes Parameters
// ---------------------------------------------------------------------------

/// Risk-free interest rate (annualized) for Black-Scholes pricing.
/// Uses India 91-day T-Bill rate (~6.8% as of Q1 2026).
/// Update quarterly from RBI T-Bill auction results.
pub const RISK_FREE_RATE: f64 = 0.068;

/// Continuous dividend yield (annualized) for NIFTY/BANKNIFTY.
/// ~1.2% for NIFTY 50 (varies; update quarterly).
pub const DIVIDEND_YIELD: f64 = 0.012;

/// Maximum Newton-Raphson iterations for IV solver.
/// 50 iterations is sufficient for convergence to 1e-8 tolerance.
pub const IV_SOLVER_MAX_ITERATIONS: u32 = 50;

/// IV solver convergence tolerance (precision of implied volatility).
pub const IV_SOLVER_TOLERANCE: f64 = 1e-8;

/// Minimum implied volatility bound (0.1% — prevents zero/negative IV).
pub const IV_MIN_BOUND: f64 = 0.001;

/// Maximum implied volatility bound (500% — extreme but valid for penny options).
pub const IV_MAX_BOUND: f64 = 5.0;

// ---------------------------------------------------------------------------
// Frontend — Tick Broadcast Channel
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Token Cache — Fast Restart
// ---------------------------------------------------------------------------

/// File path for the cached JWT token (inside Docker container).
///
/// `/tmp` is ephemeral on container restart but survives process crashes
/// within the same container — exactly the right lifetime for crash recovery.
/// Security: file permissions 0600, container isolation, 24h TTL token.
/// Token cache file path. Uses `data/cache/` (persistent filesystem) instead of `/tmp`
/// which may be tmpfs on Linux (wiped on reboot). The `data/` directory is the app's
/// persistent data root, also used by rkyv instrument cache and log files.
pub const TOKEN_CACHE_FILE_PATH: &str = "data/cache/tv-token-cache";

/// Minimum remaining token validity (hours) to accept a cached token.
///
/// If the cached token expires in less than this, a full re-auth is performed.
/// 1 hour gives the renewal loop time to refresh before expiry.
pub const TOKEN_CACHE_MIN_REMAINING_HOURS: i64 = 1;

// ---------------------------------------------------------------------------
// Compile-Time Assertions
// ---------------------------------------------------------------------------

// Enforce: DISPLAY_INDEX_COUNT must equal DISPLAY_INDEX_ENTRIES length.
const _: () = assert!(
    DISPLAY_INDEX_COUNT == DISPLAY_INDEX_ENTRIES.len(),
    "DISPLAY_INDEX_COUNT does not match DISPLAY_INDEX_ENTRIES.len()"
);

// Enforce: FULL_CHAIN_INDEX_COUNT must equal FULL_CHAIN_INDEX_SYMBOLS length.
const _: () = assert!(
    FULL_CHAIN_INDEX_COUNT == FULL_CHAIN_INDEX_SYMBOLS.len(),
    "FULL_CHAIN_INDEX_COUNT does not match FULL_CHAIN_INDEX_SYMBOLS.len()"
);

// Enforce: TOTAL_SUBSCRIBED_INDEX_COUNT is the sum of F&O and display.
// 8 F&O indices (from VALIDATION_MUST_EXIST_INDICES) + 23 display.
const _: () = assert!(
    TOTAL_SUBSCRIBED_INDEX_COUNT == VALIDATION_MUST_EXIST_INDICES.len() + DISPLAY_INDEX_COUNT,
    "TOTAL_SUBSCRIBED_INDEX_COUNT mismatch"
);

// Sanity: ring buffer capacities must be powers of 2 (rtrb requirement).
const _: () = assert!(
    TICK_RING_BUFFER_CAPACITY.is_power_of_two(),
    "TICK_RING_BUFFER_CAPACITY must be power of 2"
);
const _: () = assert!(
    ORDER_EVENT_RING_BUFFER_CAPACITY.is_power_of_two(),
    "ORDER_EVENT_RING_BUFFER_CAPACITY must be power of 2"
);

// Sanity: deep depth packet sizes are consistent.
const _: () = assert!(
    TWENTY_DEPTH_PACKET_SIZE
        == DEEP_DEPTH_HEADER_SIZE + TWENTY_DEPTH_LEVELS * DEEP_DEPTH_LEVEL_SIZE,
    "TWENTY_DEPTH_PACKET_SIZE mismatch"
);
const _: () = assert!(
    TWO_HUNDRED_DEPTH_PACKET_SIZE
        == DEEP_DEPTH_HEADER_SIZE + TWO_HUNDRED_DEPTH_LEVELS * DEEP_DEPTH_LEVEL_SIZE,
    "TWO_HUNDRED_DEPTH_PACKET_SIZE mismatch"
);

// Sanity: dedup ring buffer power must be in range [8, 24].
// 2^8 = 256 (minimum useful), 2^24 = 16M (8 bytes × 16M = 128 MiB max).
const _: () = assert!(
    DEDUP_RING_BUFFER_POWER >= 8 && DEDUP_RING_BUFFER_POWER <= 24,
    "DEDUP_RING_BUFFER_POWER must be in [8, 24]"
);

// Sanity: tick persist window — start < end, both within a day, values match IST times.
const _: () = assert!(
    TICK_PERSIST_START_SECS_OF_DAY_IST == 9 * 3600,
    "TICK_PERSIST_START must equal 09:00 IST (32400)"
);
const _: () = assert!(
    TICK_PERSIST_END_SECS_OF_DAY_IST == 15 * 3600 + 30 * 60,
    "TICK_PERSIST_END must equal 15:30 IST (55800)"
);
const _: () = assert!(
    TICK_PERSIST_START_SECS_OF_DAY_IST < TICK_PERSIST_END_SECS_OF_DAY_IST,
    "TICK_PERSIST_START must be before TICK_PERSIST_END"
);
const _: () = assert!(
    TICK_PERSIST_END_SECS_OF_DAY_IST < SECONDS_PER_DAY,
    "TICK_PERSIST_END must be within a single day"
);
const _: () = assert!(SECONDS_PER_DAY == 86_400, "SECONDS_PER_DAY must be 86400");

// Sanity: indicator ring buffer capacity must be a power of two (bitmask indexing).
const _: () = assert!(
    INDICATOR_RING_BUFFER_CAPACITY.is_power_of_two(),
    "INDICATOR_RING_BUFFER_CAPACITY must be power of 2"
);

// ---------------------------------------------------------------------------
// Compile-Time Assertions — Binary Protocol Offset Chain Verification
// Source: Groww API (Python SDK ref) struct.unpack format strings.
// Ensures every offset = previous_offset + previous_field_size.
// ---------------------------------------------------------------------------

// Header offset chain: <BHBi> = response_code(1) + msg_length(2) + segment(1) + security_id(4) = 8
const _: () = assert!(
    HEADER_OFFSET_RESPONSE_CODE == 0,
    "header: response_code at 0"
);
const _: () = assert!(HEADER_OFFSET_MESSAGE_LENGTH == 1, "header: msg_length at 1");
const _: () = assert!(HEADER_OFFSET_EXCHANGE_SEGMENT == 3, "header: segment at 3");
const _: () = assert!(HEADER_OFFSET_SECURITY_ID == 4, "header: security_id at 4");
const _: () = assert!(BINARY_HEADER_SIZE == 8, "header total = 8");

// Ticker offset chain: header(8) + LTP(f32=4) + LTT(u32=4) = 16
const _: () = assert!(TICKER_OFFSET_LTP == 8, "ticker: LTP at 8");
const _: () = assert!(TICKER_OFFSET_LTT == 12, "ticker: LTT at 12");
const _: () = assert!(TICKER_PACKET_SIZE == 16, "ticker total = 16");

// Quote offset chain: <BHBIfHIfIIIffff> = 50
const _: () = assert!(QUOTE_OFFSET_LTP == 8, "quote: LTP at 8");
const _: () = assert!(QUOTE_OFFSET_LTQ == 12, "quote: LTQ at 12");
const _: () = assert!(QUOTE_OFFSET_LTT == 14, "quote: LTT at 14");
const _: () = assert!(QUOTE_OFFSET_ATP == 18, "quote: ATP at 18");
const _: () = assert!(QUOTE_OFFSET_VOLUME == 22, "quote: volume at 22");
const _: () = assert!(QUOTE_OFFSET_TOTAL_SELL_QTY == 26, "quote: total_sell at 26");
const _: () = assert!(QUOTE_OFFSET_TOTAL_BUY_QTY == 30, "quote: total_buy at 30");
const _: () = assert!(QUOTE_OFFSET_OPEN == 34, "quote: open at 34");
const _: () = assert!(QUOTE_OFFSET_CLOSE == 38, "quote: close at 38");
const _: () = assert!(QUOTE_OFFSET_HIGH == 42, "quote: high at 42");
const _: () = assert!(QUOTE_OFFSET_LOW == 46, "quote: low at 46");
const _: () = assert!(QUOTE_PACKET_SIZE == 50, "quote total = 50");

// Full packet offset chain: diverges from Quote at 34
// <BHBIfHIfIIIIIIffff100s> = 162
const _: () = assert!(FULL_OFFSET_OI == 34, "full: OI at 34");
const _: () = assert!(FULL_OFFSET_OI_DAY_HIGH == 38, "full: OI high at 38");
const _: () = assert!(FULL_OFFSET_OI_DAY_LOW == 42, "full: OI low at 42");
const _: () = assert!(FULL_OFFSET_OPEN == 46, "full: open at 46");
const _: () = assert!(FULL_OFFSET_CLOSE == 50, "full: close at 50");
const _: () = assert!(FULL_OFFSET_HIGH == 54, "full: high at 54");
const _: () = assert!(FULL_OFFSET_LOW == 58, "full: low at 58");
const _: () = assert!(FULL_OFFSET_DEPTH_START == 62, "full: depth at 62");
const _: () = assert!(
    FULL_QUOTE_PACKET_SIZE
        == FULL_OFFSET_DEPTH_START + MARKET_DEPTH_LEVELS * MARKET_DEPTH_LEVEL_SIZE,
    "full total = 62 + 5*20 = 162"
);

// Market Depth standalone offset chain: <BHBIf100s> = 112
const _: () = assert!(MARKET_DEPTH_OFFSET_LTP == 8, "mkt_depth: LTP at 8");
const _: () = assert!(
    MARKET_DEPTH_OFFSET_DEPTH_START == 12,
    "mkt_depth: depth at 12"
);
const _: () = assert!(
    MARKET_DEPTH_PACKET_SIZE
        == MARKET_DEPTH_OFFSET_DEPTH_START + MARKET_DEPTH_LEVELS * MARKET_DEPTH_LEVEL_SIZE,
    "mkt_depth total = 12 + 5*20 = 112"
);

// Per-level offset chain: <IIHHff> = 20 bytes
const _: () = assert!(DEPTH_LEVEL_OFFSET_BID_QTY == 0, "level: bid_qty at 0");
const _: () = assert!(DEPTH_LEVEL_OFFSET_ASK_QTY == 4, "level: ask_qty at 4");
const _: () = assert!(DEPTH_LEVEL_OFFSET_BID_ORDERS == 8, "level: bid_orders at 8");
const _: () = assert!(
    DEPTH_LEVEL_OFFSET_ASK_ORDERS == 10,
    "level: ask_orders at 10"
);
const _: () = assert!(DEPTH_LEVEL_OFFSET_BID_PRICE == 12, "level: bid_price at 12");
const _: () = assert!(DEPTH_LEVEL_OFFSET_ASK_PRICE == 16, "level: ask_price at 16");
const _: () = assert!(MARKET_DEPTH_LEVEL_SIZE == 20, "level total = 20");

// Deep depth per-level offset chain: <dII> = 16 bytes
const _: () = assert!(DEEP_DEPTH_LEVEL_OFFSET_PRICE == 0, "deep_level: price at 0");
const _: () = assert!(
    DEEP_DEPTH_LEVEL_OFFSET_QUANTITY == 8,
    "deep_level: qty at 8"
);
const _: () = assert!(
    DEEP_DEPTH_LEVEL_OFFSET_ORDERS == 12,
    "deep_level: orders at 12"
);
const _: () = assert!(DEEP_DEPTH_LEVEL_SIZE == 16, "deep_level total = 16");

// Deep depth header offset chain: <hBBiI> = 12 bytes
const _: () = assert!(
    DEEP_DEPTH_HEADER_OFFSET_MSG_LENGTH == 0,
    "deep_hdr: msg_len at 0"
);
const _: () = assert!(
    DEEP_DEPTH_HEADER_OFFSET_FEED_CODE == 2,
    "deep_hdr: feed_code at 2"
);
const _: () = assert!(
    DEEP_DEPTH_HEADER_OFFSET_EXCHANGE_SEGMENT == 3,
    "deep_hdr: segment at 3"
);
const _: () = assert!(
    DEEP_DEPTH_HEADER_OFFSET_SECURITY_ID == 4,
    "deep_hdr: security_id at 4"
);
const _: () = assert!(
    DEEP_DEPTH_HEADER_OFFSET_MSG_SEQUENCE == 8,
    "deep_hdr: sequence at 8"
);
const _: () = assert!(DEEP_DEPTH_HEADER_SIZE == 12, "deep_hdr total = 12");

// OI packet: <BHBII> = 12 bytes
const _: () = assert!(OI_OFFSET_VALUE == 8, "oi: value at 8");
const _: () = assert!(OI_PACKET_SIZE == 12, "oi total = 12");

// Previous Close packet: <BHBIfI> = 16 bytes
const _: () = assert!(PREV_CLOSE_OFFSET_PRICE == 8, "prev_close: price at 8");
const _: () = assert!(PREV_CLOSE_OFFSET_OI == 12, "prev_close: OI at 12");
const _: () = assert!(PREVIOUS_CLOSE_PACKET_SIZE == 16, "prev_close total = 16");

// Disconnect packet: <BHBIH> = 10 bytes
const _: () = assert!(DISCONNECT_OFFSET_CODE == 8, "disconnect: code at 8");
const _: () = assert!(DISCONNECT_PACKET_SIZE == 10, "disconnect total = 10");

// ---------------------------------------------------------------------------
// Index Constituency — NSE index constituent CSV download
// Source: niftyindices.com public CSV endpoints
// ---------------------------------------------------------------------------

/// Base URL for NSE index constituent CSV downloads.
// O(1) EXEMPT: compile-time constant, not runtime allocation
pub const INDEX_CONSTITUENCY_BASE_URL: &str = "https://www.niftyindices.com/IndexConstituent/";

/// Cache filename for the serialized constituency map.
pub const INDEX_CONSTITUENCY_CSV_CACHE_FILENAME: &str = "constituency-map.json";

/// Minimum number of indices that must download successfully.
/// Below this threshold, a warning is emitted (map may be incomplete).
pub const INDEX_CONSTITUENCY_MIN_INDICES: usize = 15;

/// Retry: minimum backoff delay in seconds between download attempts.
pub const INDEX_CONSTITUENCY_RETRY_MIN_DELAY_SECS: u64 = 1;

/// Retry: maximum backoff delay in seconds between download attempts.
pub const INDEX_CONSTITUENCY_RETRY_MAX_DELAY_SECS: u64 = 10;

/// Retry: maximum number of retry attempts per CSV download.
pub const INDEX_CONSTITUENCY_RETRY_MAX_TIMES: usize = 2;

/// User-Agent header for niftyindices.com requests.
/// niftyindices.com returns 403 Forbidden without a browser-like User-Agent.
pub const INDEX_CONSTITUENCY_USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

/// NSE index slugs: `(display_name, csv_slug)`.
/// URL = `{INDEX_CONSTITUENCY_BASE_URL}{slug}.csv`
///
/// Comprehensive list covering all broad-based, sectoral, and thematic NSE indices.
/// Source: niftyindices.com public CSV endpoints.
/// Individual download failures are handled gracefully (logged + skipped).
pub const INDEX_CONSTITUENCY_SLUGS: &[(&str, &str)] = &[
    // -----------------------------------------------------------------------
    // Broad-Based Indices (16)
    // -----------------------------------------------------------------------
    ("Nifty 50", "ind_nifty50list"),
    ("Nifty Next 50", "ind_niftynext50list"),
    ("Nifty 100", "ind_nifty100list"),
    ("Nifty 200", "ind_nifty200list"),
    ("Nifty 500", "ind_nifty500list"),
    ("Nifty Midcap 50", "ind_niftymidcap50list"),
    ("Nifty Midcap 100", "ind_niftymidcap100list"),
    ("Nifty Midcap 150", "ind_niftymidcap150list"),
    ("Nifty Midcap Select", "ind_niftymidcap_selectlist"),
    ("Nifty Smallcap 50", "ind_niftysmallcap50list"),
    ("Nifty Smallcap 100", "ind_niftysmallcap100list"),
    ("Nifty Smallcap 250", "ind_niftysmallcap250list"),
    ("Nifty LargeMidcap 250", "ind_niftylargemidcap250list"),
    ("Nifty MidSmallcap 400", "ind_niftymidsmallcap400list"),
    ("Nifty Microcap 250", "ind_niftymicrocap250_list"),
    ("Nifty Total Market", "ind_niftytotalmarket_list"),
    // -----------------------------------------------------------------------
    // Sectoral Indices (15)
    // -----------------------------------------------------------------------
    ("Nifty Auto", "ind_niftyautolist"),
    ("Nifty Bank", "ind_niftybanklist"),
    ("Nifty Financial Services", "ind_niftyfinancelist"),
    (
        "Nifty Financial Services 25/50",
        "ind_niftyfinancialservices25_50list",
    ),
    ("Nifty FMCG", "ind_niftyfmcglist"),
    ("Nifty Healthcare", "ind_niftyhealthcarelist"),
    ("Nifty IT", "ind_niftyitlist"),
    ("Nifty Media", "ind_niftymedialist"),
    ("Nifty Metal", "ind_niftymetallist"),
    ("Nifty Pharma", "ind_niftypharmalist"),
    ("Nifty PSU Bank", "ind_niftypsubanklist"),
    ("Nifty Private Bank", "ind_niftypvtbanklist"),
    ("Nifty Realty", "ind_niftyrealtylist"),
    ("Nifty Consumer Durables", "ind_niftyconsumerdurablelist"),
    ("Nifty Oil & Gas", "ind_niftyoilandgaslist"),
    // -----------------------------------------------------------------------
    // Thematic / Strategy Indices (18)
    // -----------------------------------------------------------------------
    ("Nifty Commodities", "ind_niftycommoditieslist"),
    ("Nifty India Consumption", "ind_niftyindiaconsumptionlist"),
    ("Nifty CPSE", "ind_niftycpselist"),
    ("Nifty Energy", "ind_niftyenergylist"),
    ("Nifty Infrastructure", "ind_niftyinfrastructurelist"),
    ("Nifty MNC", "ind_niftymnclist"),
    ("Nifty PSE", "ind_niftypselist"),
    ("Nifty Growth Sectors 15", "ind_niftyGrowthSectors15list"),
    ("Nifty100 Quality 30", "ind_nifty100_Quality30list"),
    ("Nifty50 Value 20", "ind_nifty50_value20list"),
    ("Nifty Alpha 50", "ind_niftyAlpha50list"),
    ("Nifty High Beta 50", "ind_niftyHighBeta50list"),
    ("Nifty Low Volatility 50", "ind_niftyLowVolatility50list"),
    ("Nifty India Digital", "ind_niftyIndiaDigital_list"),
    ("Nifty India Defence", "ind_niftyIndiaDefence_list"),
    ("Nifty India Manufacturing", "ind_niftyIndiaMfg_list"),
    ("Nifty Mobility", "ind_niftymobility_list"),
    (
        "Nifty500 Multicap 50:25:25",
        "ind_nifty500Multicap502525_list",
    ),
];

/// Number of slugs in `INDEX_CONSTITUENCY_SLUGS`.
pub const INDEX_CONSTITUENCY_SLUG_COUNT: usize = INDEX_CONSTITUENCY_SLUGS.len();

// ---------------------------------------------------------------------------
// Tests — Market Hours Constants
// ---------------------------------------------------------------------------

#[cfg(test)]
mod market_hours_tests {
    use super::*;

    #[test]
    fn test_tick_persist_start_matches_nine_am() {
        assert_eq!(TICK_PERSIST_START_SECS_OF_DAY_IST, 9 * 3600);
        assert_eq!(TICK_PERSIST_START_SECS_OF_DAY_IST, 32_400);
    }

    #[test]
    fn test_tick_persist_end_matches_three_thirty() {
        assert_eq!(TICK_PERSIST_END_SECS_OF_DAY_IST, 15 * 3600 + 30 * 60);
        assert_eq!(TICK_PERSIST_END_SECS_OF_DAY_IST, 55_800);
    }

    #[test]
    fn test_seconds_per_day_correct() {
        assert_eq!(SECONDS_PER_DAY, 24 * 3600);
        assert_eq!(SECONDS_PER_DAY, 86_400);
    }

    #[test]
    fn test_market_close_drain_buffer_constant() {
        const { assert!(MARKET_CLOSE_DRAIN_BUFFER_SECS >= 1) };
        const { assert!(MARKET_CLOSE_DRAIN_BUFFER_SECS <= 5) };
    }

    #[test]
    fn test_persist_window_is_subset_of_day() {
        const { assert!(TICK_PERSIST_START_SECS_OF_DAY_IST < TICK_PERSIST_END_SECS_OF_DAY_IST) };
        const { assert!(TICK_PERSIST_END_SECS_OF_DAY_IST < SECONDS_PER_DAY) };
    }
}

// ---------------------------------------------------------------------------
// Tests — Protocol & Constant Verification
// ---------------------------------------------------------------------------

#[cfg(test)]
#[allow(clippy::assertions_on_constants)] // APPROVED: S4 — schema-stability tests intentionally assert on compile-time constants as a regression guard against silent protocol-value drift. Converting to `const { assert!(..) }` blocks compiles away the check message which defeats the guard purpose.
mod tests {
    use super::*;

    #[test]
    fn test_ist_offset_seconds_is_5h30m() {
        assert_eq!(IST_UTC_OFFSET_SECONDS, 19_800);
        assert_eq!(IST_UTC_OFFSET_SECONDS_I64, 19_800);
    }

    #[test]
    fn test_ist_offset_nanos_consistent_with_seconds() {
        let expected_nanos = IST_UTC_OFFSET_SECONDS_I64 * 1_000_000_000;
        assert_eq!(IST_UTC_OFFSET_NANOS, expected_nanos);
    }

    #[test]
    fn test_sebi_max_orders_per_second() {
        assert_eq!(SEBI_MAX_ORDERS_PER_SECOND, 10);
    }

    #[test]
    fn test_sebi_audit_retention_years() {
        assert_eq!(SEBI_AUDIT_RETENTION_YEARS, 5);
    }

    #[test]
    fn test_ticker_packet_size() {
        assert_eq!(TICKER_PACKET_SIZE, 16);
    }

    #[test]
    fn test_quote_packet_size() {
        assert_eq!(QUOTE_PACKET_SIZE, 50);
    }

    #[test]
    fn test_full_packet_size() {
        assert_eq!(FULL_QUOTE_PACKET_SIZE, 162);
    }

    #[test]
    fn test_oi_packet_size() {
        assert_eq!(OI_PACKET_SIZE, 12);
    }

    #[test]
    fn test_disconnect_packet_size() {
        assert_eq!(DISCONNECT_PACKET_SIZE, 10);
    }

    #[test]
    fn test_exchange_segment_code_6_is_unused() {
        assert_ne!(EXCHANGE_SEGMENT_IDX_I, 6);
        assert_ne!(EXCHANGE_SEGMENT_NSE_EQ, 6);
        assert_ne!(EXCHANGE_SEGMENT_NSE_FNO, 6);
        assert_ne!(EXCHANGE_SEGMENT_NSE_CURRENCY, 6);
        assert_ne!(EXCHANGE_SEGMENT_BSE_EQ, 6);
        assert_ne!(EXCHANGE_SEGMENT_MCX_COMM, 6);
        assert_ne!(EXCHANGE_SEGMENT_BSE_CURRENCY, 6);
        assert_ne!(EXCHANGE_SEGMENT_BSE_FNO, 6);
    }

    #[test]
    fn test_exchange_segment_sequential_except_gap() {
        assert_eq!(EXCHANGE_SEGMENT_IDX_I, 0);
        assert_eq!(EXCHANGE_SEGMENT_NSE_EQ, 1);
        assert_eq!(EXCHANGE_SEGMENT_NSE_FNO, 2);
        assert_eq!(EXCHANGE_SEGMENT_NSE_CURRENCY, 3);
        assert_eq!(EXCHANGE_SEGMENT_BSE_EQ, 4);
        assert_eq!(EXCHANGE_SEGMENT_MCX_COMM, 5);
        assert_eq!(EXCHANGE_SEGMENT_BSE_CURRENCY, 7);
        assert_eq!(EXCHANGE_SEGMENT_BSE_FNO, 8);
    }

    #[test]
    fn test_data_api_error_codes_match_annexure_section_11() {
        // All 12 codes from docs/groww-ref/08-annexure-enums.md Section 11
        assert_eq!(DATA_API_INTERNAL_SERVER_ERROR, 800);
        assert_eq!(DATA_API_INSTRUMENTS_EXCEED_LIMIT, 804);
        assert_eq!(DATA_API_EXCEEDED_ACTIVE_CONNECTIONS, 805);
        assert_eq!(DATA_API_NOT_SUBSCRIBED, 806);
        assert_eq!(DATA_API_ACCESS_TOKEN_EXPIRED, 807);
        assert_eq!(DATA_API_AUTHENTICATION_FAILED, 808);
        assert_eq!(DATA_API_ACCESS_TOKEN_INVALID, 809);
        assert_eq!(DATA_API_CLIENT_ID_INVALID, 810);
        assert_eq!(DATA_API_INVALID_EXPIRY_DATE, 811);
        assert_eq!(DATA_API_INVALID_DATE_FORMAT, 812);
        assert_eq!(DATA_API_INVALID_SECURITY_ID, 813);
        assert_eq!(DATA_API_INVALID_REQUEST, 814);
    }

    #[test]
    fn test_disconnect_legacy_aliases_match_primary_constants() {
        assert_eq!(DISCONNECT_EXCEEDED_ACTIVE_CONNECTIONS, 805);
        assert_eq!(DISCONNECT_DATA_API_SUBSCRIPTION_REQUIRED, 806);
        assert_eq!(DISCONNECT_ACCESS_TOKEN_EXPIRED, 807);
        assert_eq!(DISCONNECT_AUTHENTICATION_FAILED, 808);
        assert_eq!(DISCONNECT_ACCESS_TOKEN_INVALID, 809);
    }

    #[test]
    fn test_response_code_disconnect_is_50() {
        assert_eq!(RESPONSE_CODE_DISCONNECT, 50);
    }

    #[test]
    fn test_max_connections_is_5() {
        assert_eq!(MAX_WEBSOCKET_CONNECTIONS, 5);
    }

    #[test]
    fn test_max_instruments_per_connection_is_5000() {
        assert_eq!(MAX_INSTRUMENTS_PER_WEBSOCKET_CONNECTION, 5000);
    }

    #[test]
    fn test_subscription_batch_size_within_spec() {
        let batch = SUBSCRIPTION_BATCH_SIZE;
        assert!(batch <= 100);
    }

    #[test]
    fn test_total_subscriptions_consistent() {
        assert_eq!(
            MAX_TOTAL_SUBSCRIPTIONS,
            MAX_WEBSOCKET_CONNECTIONS * MAX_INSTRUMENTS_PER_WEBSOCKET_CONNECTION
        );
    }

    #[test]
    fn test_deep_depth_header_is_12_not_8() {
        assert_eq!(DEEP_DEPTH_HEADER_SIZE, 12);
        assert_eq!(BINARY_HEADER_SIZE, 8);
        assert_ne!(DEEP_DEPTH_HEADER_SIZE, BINARY_HEADER_SIZE);
    }

    #[test]
    fn test_deep_depth_bid_ask_codes() {
        assert_eq!(DEEP_DEPTH_FEED_CODE_BID, 41);
        assert_eq!(DEEP_DEPTH_FEED_CODE_ASK, 51);
    }

    #[test]
    fn test_market_open_time() {
        assert_eq!(MARKET_OPEN_TIME_IST, "09:15:00");
    }

    #[test]
    fn test_order_update_login_msg_code_is_42() {
        assert_eq!(ORDER_UPDATE_LOGIN_MSG_CODE, 42);
    }

    #[test]
    fn test_order_update_auth_timeout_is_reasonable() {
        const { assert!(ORDER_UPDATE_AUTH_TIMEOUT_SECS >= 5) };
        const { assert!(ORDER_UPDATE_AUTH_TIMEOUT_SECS <= 30) };
    }

    #[test]
    fn test_indicator_warmup_ticks_positive() {
        const { assert!(MAX_INDICATOR_WARMUP_TICKS > 0) };
    }

    #[test]
    fn test_indicator_ring_buffer_capacity_power_of_two() {
        assert!(INDICATOR_RING_BUFFER_CAPACITY.is_power_of_two());
    }

    // --- Greeks Constants ---

    #[test]
    fn test_risk_free_rate_reasonable() {
        // India T-Bill rate should be between 3% and 12%
        assert!(RISK_FREE_RATE > 0.03 && RISK_FREE_RATE < 0.12);
    }

    #[test]
    fn test_dividend_yield_reasonable() {
        // NIFTY dividend yield between 0.5% and 3%
        assert!(DIVIDEND_YIELD > 0.005 && DIVIDEND_YIELD < 0.03);
    }

    #[test]
    fn test_iv_solver_max_iterations_positive() {
        assert!(IV_SOLVER_MAX_ITERATIONS > 0);
        assert!(IV_SOLVER_MAX_ITERATIONS <= 200);
    }

    #[test]
    fn test_iv_solver_tolerance_small_positive() {
        assert!(IV_SOLVER_TOLERANCE > 0.0);
        assert!(IV_SOLVER_TOLERANCE < 1e-4);
    }

    #[test]
    fn test_iv_bounds_valid_range() {
        assert!(IV_MIN_BOUND > 0.0);
        assert!(IV_MAX_BOUND > IV_MIN_BOUND);
        assert!(IV_MAX_BOUND <= 10.0);
    }

    // --- Display Index Constants ---

    #[test]
    fn test_display_index_entries_count_matches_constant() {
        assert_eq!(DISPLAY_INDEX_ENTRIES.len(), DISPLAY_INDEX_COUNT);
    }

    #[test]
    fn test_display_index_entries_have_nonzero_security_ids() {
        for &(name, security_id, _subcategory) in DISPLAY_INDEX_ENTRIES {
            assert!(
                security_id > 0,
                "Display index '{}' has zero security_id",
                name
            );
        }
    }

    #[test]
    fn test_display_index_entries_have_valid_subcategories() {
        let valid_subcategories = [
            "Volatility",
            "BroadMarket",
            "MidCap",
            "SmallCap",
            "Sectoral",
            "Thematic",
        ];
        for &(name, _, subcategory) in DISPLAY_INDEX_ENTRIES {
            assert!(
                valid_subcategories.contains(&subcategory),
                "Display index '{}' has invalid subcategory '{}'",
                name,
                subcategory
            );
        }
    }

    #[test]
    fn test_display_index_entries_unique_security_ids() {
        let mut seen = std::collections::HashSet::new();
        for &(name, security_id, _) in DISPLAY_INDEX_ENTRIES {
            assert!(
                seen.insert(security_id),
                "Duplicate security_id {} for index '{}'",
                security_id,
                name
            );
        }
    }

    // --- Full Chain Index Constants ---

    #[test]
    fn test_full_chain_index_count_matches_symbols() {
        assert_eq!(FULL_CHAIN_INDEX_SYMBOLS.len(), FULL_CHAIN_INDEX_COUNT);
    }

    #[test]
    fn test_full_chain_index_symbols_nonempty() {
        for &sym in FULL_CHAIN_INDEX_SYMBOLS {
            assert!(!sym.is_empty(), "Full chain index symbol is empty");
        }
    }

    // --- Validation Constants ---

    #[test]
    fn test_validation_must_exist_indices_nonempty() {
        assert!(!VALIDATION_MUST_EXIST_INDICES.is_empty());
        for &(sym, sid) in VALIDATION_MUST_EXIST_INDICES {
            assert!(!sym.is_empty());
            assert!(sid > 0);
        }
    }

    #[test]
    fn test_validation_fno_stock_min_less_than_max() {
        assert!(VALIDATION_FNO_STOCK_MIN_COUNT < VALIDATION_FNO_STOCK_MAX_COUNT);
    }

    // --- Computed Packet Size Constants ---

    #[test]
    fn test_twenty_depth_packet_size_computed_correctly() {
        assert_eq!(
            TWENTY_DEPTH_PACKET_SIZE,
            DEEP_DEPTH_HEADER_SIZE + TWENTY_DEPTH_LEVELS * DEEP_DEPTH_LEVEL_SIZE
        );
    }

    #[test]
    fn test_two_hundred_depth_packet_size_computed_correctly() {
        assert_eq!(
            TWO_HUNDRED_DEPTH_PACKET_SIZE,
            DEEP_DEPTH_HEADER_SIZE + TWO_HUNDRED_DEPTH_LEVELS * DEEP_DEPTH_LEVEL_SIZE
        );
    }

    #[test]
    fn test_twenty_depth_body_size() {
        assert_eq!(TWENTY_DEPTH_BODY_SIZE, 20 * 16);
        assert_eq!(TWENTY_DEPTH_BODY_SIZE, 320);
    }

    #[test]
    fn test_two_hundred_depth_body_size() {
        assert_eq!(TWO_HUNDRED_DEPTH_BODY_SIZE, 200 * 16);
        assert_eq!(TWO_HUNDRED_DEPTH_BODY_SIZE, 3200);
    }

    // --- Candle Constants ---

    #[test]
    fn test_candles_per_trading_day() {
        assert_eq!(CANDLES_PER_TRADING_DAY, 375);
    }

    #[test]
    fn test_intraday_timeframes_count() {
        assert_eq!(INTRADAY_TIMEFRAMES.len(), 5);
    }

    #[test]
    fn test_intraday_timeframes_valid_intervals() {
        for &(interval, _label) in INTRADAY_TIMEFRAMES {
            assert!(
                ["1", "5", "15", "25", "60"].contains(&interval),
                "Invalid intraday interval: '{}'",
                interval
            );
        }
    }

    // --- SSM Path Constants ---

    #[test]
    fn test_ssm_paths_nonempty() {
        assert!(!SSM_SECRET_BASE_PATH.is_empty());
        assert!(!SSM_GROWW_SERVICE.is_empty());
        assert!(!GROWW_CLIENT_ID_SECRET.is_empty());
        assert!(!GROWW_CLIENT_SECRET_SECRET.is_empty());
        assert!(!GROWW_TOTP_SECRET.is_empty());
    }

    // --- Market Status Codes ---

    #[test]
    fn test_market_status_codes_distinct() {
        let codes = [
            MARKET_STATUS_CLOSED,
            MARKET_STATUS_PRE_OPEN,
            MARKET_STATUS_OPEN,
            MARKET_STATUS_POST_CLOSE,
        ];
        let mut unique = std::collections::HashSet::new();
        for code in codes {
            assert!(
                unique.insert(code),
                "Duplicate market status code: {}",
                code
            );
        }
    }

    // --- Feed Request Codes ---

    #[test]
    fn test_feed_request_codes_correct() {
        assert_eq!(FEED_REQUEST_CONNECT, 11);
        assert_eq!(FEED_REQUEST_DISCONNECT, 12);
        assert_eq!(FEED_REQUEST_TICKER, 15);
        assert_eq!(FEED_UNSUBSCRIBE_TICKER, 16);
        assert_eq!(FEED_REQUEST_QUOTE, 17);
        assert_eq!(FEED_UNSUBSCRIBE_QUOTE, 18);
        assert_eq!(FEED_REQUEST_FULL, 21);
        assert_eq!(FEED_UNSUBSCRIBE_FULL, 22);
    }

    // --- Response Codes ---

    #[test]
    fn test_response_codes_correct() {
        assert_eq!(RESPONSE_CODE_INDEX_TICKER, 1);
        assert_eq!(RESPONSE_CODE_TICKER, 2);
        assert_eq!(RESPONSE_CODE_MARKET_DEPTH, 3);
        assert_eq!(RESPONSE_CODE_QUOTE, 4);
        assert_eq!(RESPONSE_CODE_OI, 5);
        assert_eq!(RESPONSE_CODE_PREVIOUS_CLOSE, 6);
        assert_eq!(RESPONSE_CODE_MARKET_STATUS, 7);
        assert_eq!(RESPONSE_CODE_FULL, 8);
    }

    // --- TOTP Constants ---

    #[test]
    fn test_totp_constants_valid() {
        assert_eq!(TOTP_DIGITS, 6);
        assert_eq!(TOTP_PERIOD_SECS, 30);
        assert!(TOTP_MAX_RETRIES >= 1);
    }

    // --- Network Constants ---

    #[test]
    fn test_public_ip_check_urls_are_https() {
        assert!(PUBLIC_IP_CHECK_PRIMARY_URL.starts_with("https://"));
        assert!(PUBLIC_IP_CHECK_FALLBACK_URL.starts_with("https://"));
    }

    // --- Ring Buffer Constants ---

    #[test]
    fn test_tick_ring_buffer_is_power_of_two() {
        assert!(TICK_RING_BUFFER_CAPACITY.is_power_of_two());
        assert_eq!(TICK_RING_BUFFER_CAPACITY, 65536);
    }

    #[test]
    fn test_order_event_ring_buffer_is_power_of_two() {
        assert!(ORDER_EVENT_RING_BUFFER_CAPACITY.is_power_of_two());
    }

    // --- QuestDB Table Names ---

    #[test]
    fn test_questdb_table_names_nonempty() {
        let tables = [
            QUESTDB_TABLE_BUILD_METADATA,
            QUESTDB_TABLE_FNO_UNDERLYINGS,
            QUESTDB_TABLE_DERIVATIVE_CONTRACTS,
            QUESTDB_TABLE_SUBSCRIBED_INDICES,
            QUESTDB_TABLE_NSE_HOLIDAYS,
            QUESTDB_TABLE_INDEX_CONSTITUENTS,
            QUESTDB_TABLE_TICKS,
            QUESTDB_TABLE_MARKET_DEPTH,
            QUESTDB_TABLE_PREVIOUS_CLOSE,
        ];
        for table in tables {
            assert!(!table.is_empty(), "QuestDB table name is empty");
        }
    }

    // --- Depth Level Offsets ---

    #[test]
    fn test_depth_level_offsets_within_bounds() {
        assert!(DEPTH_LEVEL_OFFSET_BID_QTY < MARKET_DEPTH_LEVEL_SIZE);
        assert!(DEPTH_LEVEL_OFFSET_ASK_QTY < MARKET_DEPTH_LEVEL_SIZE);
        assert!(DEPTH_LEVEL_OFFSET_BID_ORDERS < MARKET_DEPTH_LEVEL_SIZE);
        assert!(DEPTH_LEVEL_OFFSET_ASK_ORDERS < MARKET_DEPTH_LEVEL_SIZE);
        assert!(DEPTH_LEVEL_OFFSET_BID_PRICE < MARKET_DEPTH_LEVEL_SIZE);
        assert!(DEPTH_LEVEL_OFFSET_ASK_PRICE < MARKET_DEPTH_LEVEL_SIZE);
    }

    // --- Deep Depth Header Offsets ---

    #[test]
    fn test_deep_depth_header_offsets_within_bounds() {
        assert!(DEEP_DEPTH_HEADER_OFFSET_MSG_LENGTH < DEEP_DEPTH_HEADER_SIZE);
        assert!(DEEP_DEPTH_HEADER_OFFSET_FEED_CODE < DEEP_DEPTH_HEADER_SIZE);
        assert!(DEEP_DEPTH_HEADER_OFFSET_EXCHANGE_SEGMENT < DEEP_DEPTH_HEADER_SIZE);
        assert!(DEEP_DEPTH_HEADER_OFFSET_SECURITY_ID < DEEP_DEPTH_HEADER_SIZE);
        assert!(DEEP_DEPTH_HEADER_OFFSET_MSG_SEQUENCE < DEEP_DEPTH_HEADER_SIZE);
    }

    // --- Packet Header Offsets ---

    #[test]
    fn test_header_offsets_within_bounds() {
        assert!(HEADER_OFFSET_RESPONSE_CODE < BINARY_HEADER_SIZE);
        assert!(HEADER_OFFSET_MESSAGE_LENGTH < BINARY_HEADER_SIZE);
        assert!(HEADER_OFFSET_EXCHANGE_SEGMENT < BINARY_HEADER_SIZE);
        assert!(HEADER_OFFSET_SECURITY_ID < BINARY_HEADER_SIZE);
    }

    // --- API Endpoint Paths ---

    #[test]
    fn test_api_paths_start_with_slash() {
        let paths = [
            GROWW_GENERATE_TOKEN_PATH,
            GROWW_RENEW_TOKEN_PATH,
            GROWW_CHARTS_INTRADAY_PATH,
            GROWW_CHARTS_HISTORICAL_PATH,
            GROWW_USER_PROFILE_PATH,
            GROWW_SET_IP_PATH,
            GROWW_MODIFY_IP_PATH,
            GROWW_GET_IP_PATH,
            GROWW_HOLDINGS_PATH,
            GROWW_POSITIONS_PATH,
            GROWW_POSITIONS_CONVERT_PATH,
            GROWW_MARGIN_CALCULATOR_PATH,
            GROWW_MARGIN_CALCULATOR_MULTI_PATH,
            GROWW_FUND_LIMIT_PATH,
        ];
        for path in paths {
            assert!(
                path.starts_with('/'),
                "API path does not start with '/': '{}'",
                path
            );
        }
    }

    // --- Unsubscribe code for depth ---

    #[test]
    fn test_depth_unsubscribe_code_is_25() {
        assert_eq!(FEED_UNSUBSCRIBE_TWENTY_DEPTH, 25);
    }

    // --- Application constants ---

    #[test]
    fn test_application_name() {
        assert_eq!(APPLICATION_NAME, "brutex");
    }

    #[test]
    fn test_application_version_nonempty() {
        assert!(!APPLICATION_VERSION.is_empty());
    }
}
