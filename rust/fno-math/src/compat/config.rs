//! Configuration structs deserialized from `config/base.toml`.
//!
//! Every runtime value that might differ between environments lives here.
//! Secrets are NEVER in config — they come from AWS SSM Parameter Store.

use anyhow::{Result, bail};
use chrono::NaiveTime;
use serde::Deserialize;

use super::constants::SEBI_MAX_ORDERS_PER_SECOND;
use super::trading_calendar::TradingCalendar;

/// Root application configuration.
#[derive(Debug, Deserialize)]
pub struct ApplicationConfig {
    pub trading: TradingConfig,
    pub dhan: DhanConfig,
    pub websocket: WebSocketConfig,
    pub questdb: QuestDbConfig,
    pub valkey: ValkeyConfig,
    pub prometheus: PrometheusConfig,
    pub network: NetworkConfig,
    pub token: TokenConfig,
    pub risk: RiskConfig,
    pub logging: LoggingConfig,
    pub instrument: InstrumentConfig,
    pub api: ApiConfig,
    #[serde(default)]
    pub subscription: SubscriptionConfig,
    #[serde(default)]
    pub notification: NotificationConfig,
    #[serde(default)]
    pub observability: ObservabilityConfig,
    #[serde(default)]
    pub historical: HistoricalDataConfig,
    #[serde(default)]
    pub strategy: StrategyConfig,
    #[serde(default)]
    pub index_constituency: IndexConstituencyConfig,
    #[serde(default)]
    pub greeks: GreeksConfig,
    #[serde(default)]
    pub infrastructure: InfrastructureConfig,
    #[serde(default)]
    pub partition_retention: PartitionRetentionConfig,
}

/// Trading execution mode — controls how orders are routed.
///
/// - `Paper`: Zero HTTP calls. Orders simulated locally with PAPER-{counter} IDs.
///   Use for strategy development with real market data.
/// - `Sandbox`: HTTP calls to `sandbox.dhan.co/v2/`. Orders fill at ₹100 (simulated).
///   Use for API integration testing before going live.
/// - `Live`: HTTP calls to `api.dhan.co/v2/`. Real exchange orders, real money.
///   Requires static IP. Use only when ready for production.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum TradingMode {
    /// Local simulation — no HTTP calls, PAPER-{counter} order IDs.
    /// Default: safe — never touch real money.
    #[default]
    Paper,
    /// Dhan sandbox — HTTP to sandbox.dhan.co, orders fill at ₹100.
    Sandbox,
    /// Real trading — HTTP to api.dhan.co, real exchange orders.
    Live,
}

impl TradingMode {
    /// Returns true if this mode makes real HTTP calls to Dhan (sandbox or live).
    pub fn is_http_active(self) -> bool {
        matches!(self, Self::Sandbox | Self::Live)
    }

    /// Returns true if this mode is paper trading (no HTTP calls).
    pub fn is_paper(self) -> bool {
        self == Self::Paper
    }

    /// Returns true if this is the live production mode.
    pub fn is_live(self) -> bool {
        self == Self::Live
    }

    /// Returns true if this is the sandbox testing mode.
    pub fn is_sandbox(self) -> bool {
        self == Self::Sandbox
    }

    /// Returns the display name for logging.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Paper => "paper",
            Self::Sandbox => "sandbox",
            Self::Live => "live",
        }
    }
}

/// Strategy and paper-trading configuration.
#[derive(Debug, Deserialize)]
pub struct StrategyConfig {
    /// Path to the strategy TOML config file (relative to working directory).
    #[serde(default = "default_strategy_config_path")]
    pub config_path: String,
    /// Trading capital in rupees (for risk engine daily loss calculation).
    #[serde(default = "default_capital")]
    pub capital: f64,
    /// Dry-run mode: when true, NO real orders are placed. All orders are simulated.
    /// DEFAULT: true. This is a developer-only tool.
    /// DEPRECATED: Use `mode` instead. Kept for backward compatibility.
    #[serde(default = "default_dry_run")]
    pub dry_run: bool,
    /// Trading execution mode: paper (local sim), sandbox (Dhan DevPortal), live (real).
    /// Overrides dry_run when set. Default: paper.
    #[serde(default)]
    pub mode: TradingMode,
    /// S6-Step4: Sandbox-only enforcement until this date. If the current
    /// date is BEFORE this value, `mode = Live` is forbidden — the boot
    /// sequence panics. Format: `YYYY-MM-DD`. Default `2026-06-30` per
    /// Parthiban's "no real orders until June end" requirement.
    ///
    /// Set to `1970-01-01` (or any past date) to disable the gate.
    #[serde(default = "default_sandbox_only_until")]
    pub sandbox_only_until: String,
}

fn default_sandbox_only_until() -> String {
    // Per Parthiban — sandbox-only until June end 2026.
    "2026-06-30".to_string()
}

impl Default for StrategyConfig {
    fn default() -> Self {
        Self {
            config_path: default_strategy_config_path(),
            capital: default_capital(),
            dry_run: default_dry_run(),
            mode: TradingMode::default(),
            sandbox_only_until: default_sandbox_only_until(),
        }
    }
}

impl StrategyConfig {
    /// S6-Step4: Returns Ok if the current IST date is past
    /// `sandbox_only_until` OR the trading mode is Paper/Sandbox/DryRun.
    /// Returns Err if Live trading is requested before the cutoff.
    ///
    /// Called at boot from `crates/app/src/main.rs`. A failure here is
    /// FATAL — the process panics rather than risk a real-money order
    /// in the sandbox-only window.
    ///
    /// # Errors
    /// Returns `Err(String)` describing the violation.
    // TEST-EXEMPT: covered by test_sandbox_only_until_blocks_real_orders, test_sandbox_date_parses, test_sandbox_already_past_returns_ok in this module
    pub fn check_sandbox_window(&self, today_ist: chrono::NaiveDate) -> Result<(), String> {
        // Live mode check: only enforce on Live trading.
        if !self.mode.is_live() && self.dry_run {
            return Ok(());
        }
        if !self.mode.is_live() {
            return Ok(());
        }

        let cutoff = chrono::NaiveDate::parse_from_str(&self.sandbox_only_until, "%Y-%m-%d")
            .map_err(|e| {
                format!(
                    "invalid sandbox_only_until '{}': {e}",
                    self.sandbox_only_until
                )
            })?;
        if today_ist <= cutoff {
            return Err(format!(
                "S6-Step4 SANDBOX-ONLY VIOLATION: today is {today_ist}, sandbox_only_until={cutoff}, \
                 mode=Live. Real orders are FORBIDDEN until {cutoff}. Set mode=sandbox or mode=paper, \
                 or wait until {cutoff} passes."
            ));
        }
        Ok(())
    }
}

fn default_strategy_config_path() -> String {
    "config/strategies.toml".to_string()
}

const fn default_capital() -> f64 {
    1_000_000.0
}

const fn default_dry_run() -> bool {
    true
}

/// Trading session timing configuration.
#[derive(Debug, Deserialize)]
pub struct TradingConfig {
    /// Market open time in IST (e.g., "09:00:00").
    pub market_open_time: String,
    /// Market close time in IST (e.g., "15:30:00").
    pub market_close_time: String,
    /// Order submission cutoff in IST (e.g., "15:29:00").
    pub order_cutoff_time: String,
    /// Data collection start time in IST (e.g., "09:00:00").
    pub data_collection_start: String,
    /// Data collection end time in IST (e.g., "16:00:00").
    pub data_collection_end: String,
    /// Timezone identifier (always "Asia/Kolkata").
    pub timezone: String,
    /// Maximum orders per second (SEBI limit).
    pub max_orders_per_second: u32,
    /// NSE trading holidays with names for display.
    /// Source: official NSE circular (update annually).
    #[serde(default)]
    pub nse_holidays: Vec<NseHolidayEntry>,
    /// Muhurat Trading dates — special sessions on otherwise closed days.
    #[serde(default)]
    pub muhurat_trading_dates: Vec<NseHolidayEntry>,
    /// NSE mock trading session dates (Saturdays, ~monthly).
    /// Source: NSE Contingency Drill / Mock Trading Calendar (update annually).
    /// Mock sessions are NOT real trading days — no real orders, no settlement.
    /// Used for operational awareness and system testing readiness.
    #[serde(default)]
    pub nse_mock_trading_dates: Vec<NseHolidayEntry>,
}

/// A single NSE holiday or Muhurat trading date with display name.
#[derive(Debug, Clone, Deserialize)]
pub struct NseHolidayEntry {
    /// Date in YYYY-MM-DD format (IST).
    pub date: String,
    /// Human-readable holiday name for display.
    pub name: String,
}

/// Dhan API and WebSocket connection configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct DhanConfig {
    /// WebSocket V2 binary feed URL.
    pub websocket_url: String,
    /// Order update WebSocket URL (JSON-based, separate from binary feed).
    pub order_update_websocket_url: String,
    /// REST API base URL (for trading, data, renewal).
    pub rest_api_base_url: String,
    /// Sandbox API base URL (for sandbox mode order testing).
    /// Set in config/base.toml — no default in code (must come from config).
    #[serde(default)]
    pub sandbox_base_url: String,
    /// Auth base URL (for token generation — separate from REST API).
    /// Dhan uses `https://auth.dhan.co` for authentication endpoints.
    pub auth_base_url: String,
    /// Primary instrument CSV download URL.
    pub instrument_csv_url: String,
    /// Fallback instrument CSV download URL.
    pub instrument_csv_fallback_url: String,
    /// Maximum instruments per WebSocket connection.
    pub max_instruments_per_connection: usize,
    /// Maximum concurrent WebSocket connections.
    pub max_websocket_connections: usize,
}

/// WebSocket keep-alive and reconnection configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct WebSocketConfig {
    /// Expected server ping interval in seconds (Dhan server pings every 10s).
    pub ping_interval_secs: u64,
    /// Server disconnects after this many seconds with no pong.
    pub pong_timeout_secs: u64,
    /// Reserved for future use (server ping monitoring).
    pub max_consecutive_pong_failures: u32,
    /// Initial reconnection delay in milliseconds.
    pub reconnect_initial_delay_ms: u64,
    /// Maximum reconnection delay in milliseconds.
    pub reconnect_max_delay_ms: u64,
    /// Maximum reconnection attempts before giving up.
    pub reconnect_max_attempts: u32,
    /// Maximum instruments per subscription message (Dhan limit: 100).
    pub subscription_batch_size: usize,
    /// Delay in milliseconds between spawning successive WebSocket connections.
    /// Prevents all connections from hitting Dhan's server simultaneously at startup.
    /// 0 = no stagger (all spawn immediately). Only affects initial startup, not reconnects.
    pub connection_stagger_ms: u64,
}

/// QuestDB connection configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct QuestDbConfig {
    /// QuestDB Docker hostname.
    pub host: String,
    /// HTTP API port (web console + REST).
    pub http_port: u16,
    /// PostgreSQL wire protocol port.
    pub pg_port: u16,
    /// InfluxDB Line Protocol port (high-speed ingestion).
    pub ilp_port: u16,
}

impl QuestDbConfig {
    /// Builds the ILP connection string with retry and timeout settings.
    ///
    /// All 15+ ILP writers in the storage crate SHOULD use this method
    /// instead of raw `format!("tcp::addr=...")` to get consistent:
    /// - Retry timeout: 30s (recovers from transient QuestDB restarts)
    /// - Init buffer size: 64KB (matches WAL segment tuning)
    /// - Request timeout: 60s (generous for large batch flushes)
    ///
    /// Builds the ILP TCP connection string.
    ///
    /// NOTE: `retry_timeout`, `init_buf_size`, `request_timeout` are HTTP-only
    /// parameters in questdb-rs 6.1.0. TCP mode only supports `addr`.
    /// Connection resilience is handled by our writers (ring buffer + reconnect).
    pub fn build_ilp_conf_string(&self) -> String {
        format!("tcp::addr={}:{};", self.host, self.ilp_port)
    }
}

/// Partition retention configuration (separate from QuestDbConfig to avoid breaking existing code).
#[derive(Debug, Clone, Deserialize)]
pub struct PartitionRetentionConfig {
    /// Hot partition retention in days. Partitions older than this are detached.
    /// Default: 90 days. Set to 0 to disable auto-detach.
    #[serde(default = "default_retention_days")]
    pub retention_days: u32,
}

impl Default for PartitionRetentionConfig {
    fn default() -> Self {
        Self {
            retention_days: default_retention_days(),
        }
    }
}

/// Default retention: 90 days of hot data.
const fn default_retention_days() -> u32 {
    90
}

/// Valkey (cache) connection configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct ValkeyConfig {
    /// Valkey Docker hostname.
    pub host: String,
    /// Valkey port.
    pub port: u16,
    /// Maximum connections in the connection pool.
    pub max_connections: u32,
    /// Authentication password (matches `--requirepass` in Valkey server config).
    /// Empty string = no auth (NOT recommended). Default: "tv-dev-only".
    #[serde(default = "default_valkey_password")]
    pub password: String,
}

/// Default Valkey password — empty (loaded from SSM at boot in production).
fn default_valkey_password() -> String {
    String::new()
}

/// Prometheus metrics endpoint configuration.
#[derive(Debug, Deserialize)]
pub struct PrometheusConfig {
    /// Prometheus Docker hostname.
    pub host: String,
    /// Prometheus port.
    pub port: u16,
}

/// Network timeout and retry configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct NetworkConfig {
    /// HTTP request timeout in milliseconds.
    pub request_timeout_ms: u64,
    /// WebSocket connection timeout in milliseconds.
    pub websocket_connect_timeout_ms: u64,
    /// Initial retry delay in milliseconds (exponential backoff).
    pub retry_initial_delay_ms: u64,
    /// Maximum retry delay in milliseconds.
    pub retry_max_delay_ms: u64,
    /// Maximum number of retry attempts.
    pub retry_max_attempts: u32,
}

/// JWT token lifecycle configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct TokenConfig {
    /// Hours before expiry to trigger token refresh.
    pub refresh_before_expiry_hours: u64,
    /// Token validity duration in hours.
    pub token_validity_hours: u64,
}

/// Risk management configuration.
#[derive(Debug, Deserialize)]
pub struct RiskConfig {
    /// Maximum allowed daily loss as percentage of capital.
    pub max_daily_loss_percent: f64,
    /// Maximum position size in lots.
    pub max_position_size_lots: u32,
}

/// Logging configuration.
#[derive(Debug, Deserialize)]
pub struct LoggingConfig {
    /// Log level filter (trace, debug, info, warn, error).
    pub level: String,
    /// Log output format (json, pretty).
    pub format: String,
}

/// API server configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct ApiConfig {
    /// Bind address for the HTTP server.
    pub host: String,
    /// HTTP server port.
    pub port: u16,
    /// Allowed CORS origins. Defaults to localhost dev origins.
    #[serde(default = "default_allowed_origins")]
    pub allowed_origins: Vec<String>,
}

fn default_allowed_origins() -> Vec<String> {
    vec![
        "http://localhost:3000".to_string(),
        "http://localhost:3001".to_string(),
    ]
}

/// Instrument CSV download and universe build configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct InstrumentConfig {
    /// Time of day (IST) to download fresh instrument CSV. Format: "HH:MM:SS".
    pub daily_download_time: String,
    /// Directory path for caching the last successful CSV download.
    pub csv_cache_directory: String,
    /// Cached CSV filename.
    pub csv_cache_filename: String,
    /// Download timeout in seconds (overrides network timeout for this large file).
    pub csv_download_timeout_secs: u64,
    /// Start of instrument build window (IST). Format: "HH:MM:SS".
    pub build_window_start: String,
    /// End of instrument build window (IST). Format: "HH:MM:SS".
    pub build_window_end: String,
}

/// Notification (Telegram alert) configuration.
///
/// Secrets (bot token, chat ID) come from SSM — never in config.
/// This struct holds non-secret settings only.
#[derive(Debug, Clone, Deserialize)]
pub struct NotificationConfig {
    /// Telegram Bot API base URL.
    pub telegram_api_base_url: String,
    /// HTTP send timeout in milliseconds for notification POSTs.
    pub send_timeout_ms: u64,
    /// Enable SMS alerts via AWS SNS for Critical/High severity events.
    /// Phone number is fetched from SSM at `/tickvault/{env}/sns/phone-number`.
    pub sns_enabled: bool,
}

impl Default for NotificationConfig {
    fn default() -> Self {
        Self {
            // APPROVED: config default — overridable via TOML config file
            telegram_api_base_url: "https://api.telegram.org".to_string(),
            send_timeout_ms: 10_000,
            sns_enabled: false,
        }
    }
}

/// Observability stack configuration.
///
/// Controls Prometheus metrics export and OpenTelemetry tracing.
/// Metrics are served via an HTTP endpoint for Prometheus to scrape.
/// Traces are exported via OTLP gRPC to Jaeger.
#[derive(Debug, Clone, Deserialize)]
pub struct ObservabilityConfig {
    /// Port for the Prometheus metrics HTTP endpoint served by the application.
    pub metrics_port: u16,
    /// OTLP gRPC endpoint for trace export (e.g., tv-jaeger:4317).
    pub otlp_endpoint: String,
    /// Enable Prometheus metrics export.
    pub metrics_enabled: bool,
    /// Enable OpenTelemetry trace export to Jaeger.
    pub tracing_enabled: bool,
}

impl Default for ObservabilityConfig {
    fn default() -> Self {
        Self {
            metrics_port: 9091,
            otlp_endpoint: "http://tv-jaeger:4317".to_string(), // APPROVED: Docker DNS hostname for OTLP collector
            metrics_enabled: true,
            tracing_enabled: true,
        }
    }
}

/// Subscription planner configuration.
///
/// Controls which instruments are subscribed and at what feed mode.
/// Indices get full chain (all expiries, all strikes). Stocks get current
/// expiry only with ATM ± N strike filtering.
#[derive(Debug, Clone, Deserialize)]
pub struct SubscriptionConfig {
    /// Feed mode for all subscriptions. Always Full for maximum data (LTP, OI, depth).
    /// IDX_I instruments are forced to Ticker at connection level (Dhan limitation).
    /// Valid values: "Ticker", "Quote", "Full".
    pub feed_mode: String,

    /// Whether to subscribe index derivatives (full chain, all expiries).
    pub subscribe_index_derivatives: bool,

    /// Whether to subscribe stock derivatives (current expiry, ATM ± N).
    pub subscribe_stock_derivatives: bool,

    /// Whether to subscribe display-only indices (INDIA VIX, sectoral, etc.).
    pub subscribe_display_indices: bool,

    /// Whether to subscribe stock equity price feeds (NSE_EQ segment).
    pub subscribe_stock_equities: bool,

    /// Number of strikes above ATM for stock options.
    pub stock_atm_strikes_above: usize,

    /// Number of strikes below ATM for stock options.
    pub stock_atm_strikes_below: usize,

    /// Default LTP to use for ATM calculation when no live price is available.
    /// When the system first starts, there are no live prices yet.
    /// This fallback ensures we subscribe to a reasonable strike range.
    /// Once live prices arrive, dynamic rebalancing (Phase 2) will adjust.
    pub stock_default_atm_fallback_enabled: bool,

    /// Enable 20-level depth feed (separate WebSocket, uses 1 of 5 connection slots).
    /// Subscribes ATM ± 5 strikes for NIFTY and BANKNIFTY on the depth endpoint.
    #[serde(default)]
    pub enable_twenty_depth: bool,

    /// Maximum instruments to subscribe on the 20-level depth feed (max 50 per connection).
    #[serde(default = "default_twenty_depth_max_instruments")]
    pub twenty_depth_max_instruments: usize,
}

fn default_twenty_depth_max_instruments() -> usize {
    50
}

impl Default for SubscriptionConfig {
    fn default() -> Self {
        Self {
            feed_mode: "Full".to_string(),
            subscribe_index_derivatives: true,
            subscribe_stock_derivatives: true,
            subscribe_display_indices: true,
            subscribe_stock_equities: true,
            stock_atm_strikes_above: 10,
            stock_atm_strikes_below: 10,
            stock_default_atm_fallback_enabled: true,
            enable_twenty_depth: false,
            twenty_depth_max_instruments: 50,
        }
    }
}

impl SubscriptionConfig {
    /// Parses the feed_mode string into a `FeedMode` enum.
    ///
    /// # Errors
    /// Returns error if the string is not a recognized feed mode.
    pub fn parsed_feed_mode(&self) -> Result<super::types::FeedMode> {
        match self.feed_mode.as_str() {
            "Ticker" => Ok(super::types::FeedMode::Ticker),
            "Quote" => Ok(super::types::FeedMode::Quote),
            "Full" => Ok(super::types::FeedMode::Full),
            other => bail!(
                "subscription.feed_mode must be Ticker/Quote/Full, got '{}'",
                other
            ),
        }
    }
}

/// Historical data fetching configuration.
///
/// Controls automated fetching of 1-minute OHLCV candles from Dhan's
/// intraday charts API for cross-verification with live tick data.
#[derive(Debug, Clone, Deserialize)]
pub struct HistoricalDataConfig {
    /// Enable automated historical candle fetching after market close.
    pub enabled: bool,
    /// Number of past trading days to fetch on startup for cross-verification.
    /// Dhan allows up to 90 days per request.
    pub lookback_days: u32,
    /// HTTP request timeout in seconds for historical data API calls.
    pub request_timeout_secs: u64,
    /// Maximum retry attempts for failed API requests.
    pub max_retries: u32,
    /// Delay in milliseconds between consecutive API requests (rate limiting).
    pub request_delay_ms: u64,
}

impl Default for HistoricalDataConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            lookback_days: 90,
            request_timeout_secs: 30,
            max_retries: 3,
            request_delay_ms: 500,
        }
    }
}

/// Index constituency download configuration.
///
/// Controls the download of NSE index constituent CSVs from niftyindices.com.
/// Used to build a bidirectional index-stock mapping for the trading system.
#[derive(Debug, Clone, Deserialize)]
pub struct IndexConstituencyConfig {
    /// Whether constituency download is enabled.
    #[serde(default = "default_constituency_enabled")]
    pub enabled: bool,
    /// HTTP request timeout in seconds for individual CSV downloads.
    #[serde(default = "default_constituency_download_timeout_secs")]
    pub download_timeout_secs: u64,
    /// Maximum concurrent CSV downloads.
    #[serde(default = "default_constituency_max_concurrent_downloads")]
    pub max_concurrent_downloads: usize,
    /// Delay in milliseconds between batches of concurrent downloads.
    /// Default 200ms to be respectful to niftyindices.com when downloading ~50 indices.
    #[serde(default = "default_constituency_inter_batch_delay_ms")]
    pub inter_batch_delay_ms: u64,
}

impl Default for IndexConstituencyConfig {
    fn default() -> Self {
        Self {
            enabled: default_constituency_enabled(),
            download_timeout_secs: default_constituency_download_timeout_secs(),
            max_concurrent_downloads: default_constituency_max_concurrent_downloads(),
            inter_batch_delay_ms: default_constituency_inter_batch_delay_ms(),
        }
    }
}

const fn default_constituency_enabled() -> bool {
    true
}

const fn default_constituency_download_timeout_secs() -> u64 {
    30
}

const fn default_constituency_max_concurrent_downloads() -> usize {
    5
}

const fn default_constituency_inter_batch_delay_ms() -> u64 {
    200
}

/// Infrastructure configuration — controls Docker auto-start behavior.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct InfrastructureConfig {
    /// Whether to auto-start Docker services on boot.
    /// When true (default): one-click Run — app auto-launches Docker Desktop
    /// and containers. Probes first, skips if already running. Zero manual steps.
    /// When false: run "Docker Restart" in IntelliJ first, then run the app.
    #[serde(default = "default_auto_start")]
    pub auto_start_docker: bool,
}

impl Default for InfrastructureConfig {
    fn default() -> Self {
        Self {
            auto_start_docker: true,
        }
    }
}

const fn default_auto_start() -> bool {
    true
}

/// Greeks engine configuration.
///
/// Controls Black-Scholes pricing parameters, IV solver settings,
/// and the periodic option chain fetch pipeline.
/// Defaults match Indian market conditions (NIFTY/BANKNIFTY).
#[derive(Debug, Clone, Deserialize)]
pub struct GreeksConfig {
    /// Enable the greeks pipeline (option chain fetch + compute + persist).
    #[serde(default = "default_greeks_enabled")]
    pub enabled: bool,
    /// Interval between option chain fetch cycles (seconds).
    #[serde(default = "default_greeks_fetch_interval_secs")]
    pub fetch_interval_secs: u64,
    /// Risk-free interest rate (annualized). India 91-day T-Bill rate.
    #[serde(default = "default_risk_free_rate")]
    pub risk_free_rate: f64,
    /// Continuous dividend yield (annualized). ~1.2% for NIFTY 50.
    #[serde(default = "default_dividend_yield")]
    pub dividend_yield: f64,
    /// Maximum Newton-Raphson iterations for IV solver.
    #[serde(default = "default_iv_solver_max_iterations")]
    pub iv_solver_max_iterations: u32,
    /// IV solver convergence tolerance.
    #[serde(default = "default_iv_solver_tolerance")]
    pub iv_solver_tolerance: f64,
    /// Day count divisor for theta conversion (365.0 = calendar, 252.0 = trading days).
    /// Calibrated to match Dhan's computation. Default: 365.0.
    #[serde(default = "default_day_count")]
    pub day_count: f64,
    /// Rate mode: "dhan" = fixed 10% (match Dhan/NSE), "theoretical" = RBI repo rate lookup.
    /// Default: "dhan".
    #[serde(default = "default_rate_mode")]
    pub rate_mode: String,
}

impl Default for GreeksConfig {
    fn default() -> Self {
        Self {
            enabled: default_greeks_enabled(),
            fetch_interval_secs: default_greeks_fetch_interval_secs(),
            risk_free_rate: default_risk_free_rate(),
            dividend_yield: default_dividend_yield(),
            iv_solver_max_iterations: default_iv_solver_max_iterations(),
            iv_solver_tolerance: default_iv_solver_tolerance(),
            day_count: default_day_count(),
            rate_mode: default_rate_mode(),
        }
    }
}

const fn default_greeks_enabled() -> bool {
    true
}

const fn default_greeks_fetch_interval_secs() -> u64 {
    60
}

// Calibrated against Dhan's live option chain data (2026-03-23).
// Dhan's theta best matches at r ≈ 0.10. Gamma/vega insensitive to rate for short-dated.
const fn default_risk_free_rate() -> f64 {
    0.10
}

// Calibrated: Dhan uses q=0.0 for index options (no continuous dividend).
const fn default_dividend_yield() -> f64 {
    0.0
}

const fn default_iv_solver_max_iterations() -> u32 {
    50
}

const fn default_iv_solver_tolerance() -> f64 {
    1e-8
}

const fn default_day_count() -> f64 {
    365.0
}

fn default_rate_mode() -> String {
    String::from("dhan")
}

// ---------------------------------------------------------------------------
// Configuration Validation
// ---------------------------------------------------------------------------

impl ApplicationConfig {
    /// Validates all configuration values at startup.
    ///
    /// Catches invalid time formats, SEBI violations, and nonsensical ranges
    /// before the system starts trading.
    ///
    /// # Errors
    /// Returns descriptive error for the first validation failure found.
    pub fn validate(&self) -> Result<()> {
        // Timezone must be Asia/Kolkata — SEBI requirement.
        if self.trading.timezone != "Asia/Kolkata" {
            bail!(
                "trading.timezone must be 'Asia/Kolkata', got '{}'",
                self.trading.timezone
            );
        }

        // Validate time string formats.
        // Helper: parse and validate a single time field.
        let parse_time = |field_name: &str, value: &str| -> Result<NaiveTime> {
            NaiveTime::parse_from_str(value, "%H:%M:%S").map_err(|_| {
                anyhow::anyhow!("{} is not a valid HH:MM:SS time: '{}'", field_name, value)
            })
        };

        parse_time("trading.market_open_time", &self.trading.market_open_time)?;
        parse_time("trading.market_close_time", &self.trading.market_close_time)?;
        parse_time("trading.order_cutoff_time", &self.trading.order_cutoff_time)?;
        parse_time(
            "trading.data_collection_start",
            &self.trading.data_collection_start,
        )?;
        parse_time(
            "trading.data_collection_end",
            &self.trading.data_collection_end,
        )?;
        parse_time(
            "instrument.daily_download_time",
            &self.instrument.daily_download_time,
        )?;
        // Parse and retain build window times for the comparison below.
        let window_start = parse_time(
            "instrument.build_window_start",
            &self.instrument.build_window_start,
        )?;
        let window_end = parse_time(
            "instrument.build_window_end",
            &self.instrument.build_window_end,
        )?;

        // SEBI: max_orders_per_second must not exceed the SEBI limit.
        if self.trading.max_orders_per_second > SEBI_MAX_ORDERS_PER_SECOND {
            bail!(
                "trading.max_orders_per_second ({}) exceeds SEBI limit ({})",
                self.trading.max_orders_per_second,
                SEBI_MAX_ORDERS_PER_SECOND
            );
        }

        // Token: refresh_before_expiry must be less than token validity.
        if self.token.refresh_before_expiry_hours >= self.token.token_validity_hours {
            bail!(
                "token.refresh_before_expiry_hours ({}) must be less than token.token_validity_hours ({})",
                self.token.refresh_before_expiry_hours,
                self.token.token_validity_hours
            );
        }

        // Network: timeouts and retries must be positive.
        if self.network.request_timeout_ms == 0 {
            bail!("network.request_timeout_ms must be > 0");
        }
        if self.network.retry_max_attempts == 0 {
            bail!("network.retry_max_attempts must be > 0");
        }

        // Risk: daily loss must be a finite positive number in (0, 100].
        // NaN/Inf must be rejected — NaN defeats comparisons (all return false)
        // and would corrupt financial calculations.
        if !self.risk.max_daily_loss_percent.is_finite()
            || self.risk.max_daily_loss_percent <= 0.0
            || self.risk.max_daily_loss_percent > 100.0
        {
            bail!(
                "risk.max_daily_loss_percent must be a finite value in (0, 100], got {}",
                self.risk.max_daily_loss_percent
            );
        }

        // Instrument: download timeout must be positive.
        if self.instrument.csv_download_timeout_secs == 0 {
            bail!("instrument.csv_download_timeout_secs must be > 0");
        }

        // Instrument: build window start must be before end.
        // `window_start` and `window_end` already parsed above — no redundant re-parse.
        if window_start >= window_end {
            bail!(
                "instrument.build_window_start ({}) must be before build_window_end ({})",
                self.instrument.build_window_start,
                self.instrument.build_window_end
            );
        }

        // WebSocket: ping interval must be positive.
        if self.websocket.ping_interval_secs == 0 {
            bail!("websocket.ping_interval_secs must be > 0");
        }

        // WebSocket: subscription batch must be in [1, 100] (Dhan limit).
        if self.websocket.subscription_batch_size == 0
            || self.websocket.subscription_batch_size > super::constants::SUBSCRIPTION_BATCH_SIZE
        {
            bail!(
                "websocket.subscription_batch_size must be in [1, {}], got {}",
                super::constants::SUBSCRIPTION_BATCH_SIZE,
                self.websocket.subscription_batch_size
            );
        }

        // WebSocket: reconnect_max_attempts == 0 means infinite retries (production default).
        // No validation needed — 0 is a valid sentinel for "never give up".

        // Dhan: max_websocket_connections must be positive (prevents division-by-zero in pool).
        if self.dhan.max_websocket_connections == 0 {
            bail!("dhan.max_websocket_connections must be > 0");
        }

        // WebSocket: pong_timeout_secs must be positive (used in read timeout calculation).
        if self.websocket.pong_timeout_secs == 0 {
            bail!("websocket.pong_timeout_secs must be > 0");
        }

        // WebSocket: computed read timeout must be reasonable relative to Dhan's
        // 40s server timeout. We allow up to 2× the server timeout as an upper
        // bound — beyond that, the config is likely misconfigured.
        let computed_read_timeout = self
            .websocket
            .ping_interval_secs
            .saturating_mul(u64::from(self.websocket.max_consecutive_pong_failures) + 1)
            .saturating_add(self.websocket.pong_timeout_secs);
        let max_reasonable_timeout = super::constants::SERVER_PING_TIMEOUT_SECS * 2;
        if computed_read_timeout > max_reasonable_timeout {
            bail!(
                "websocket read timeout ({}s = ping_interval × (max_failures+1) + pong_timeout) \
                 exceeds {}s — Dhan server disconnects at {}s",
                computed_read_timeout,
                max_reasonable_timeout,
                super::constants::SERVER_PING_TIMEOUT_SECS
            );
        }

        // Notification: send timeout must be positive.
        if self.notification.send_timeout_ms == 0 {
            bail!("notification.send_timeout_ms must be > 0");
        }

        // Trading calendar: validate all holiday date strings parse correctly
        // and none fall on weekends. This also constructs the calendar to verify
        // internal consistency.
        TradingCalendar::from_config(&self.trading)?;

        // Historical: validate if enabled.
        if self.historical.enabled {
            if self.historical.lookback_days == 0
                || self.historical.lookback_days
                    > super::constants::DHAN_INTRADAY_MAX_DAYS_PER_REQUEST
            {
                bail!(
                    "historical.lookback_days must be in [1, {}], got {}",
                    super::constants::DHAN_INTRADAY_MAX_DAYS_PER_REQUEST,
                    self.historical.lookback_days
                );
            }
            if self.historical.request_timeout_secs == 0 {
                bail!("historical.request_timeout_secs must be > 0");
            }
        }

        // D1: Sandbox enforcement — prevent live trading before LIVE_TRADING_EARLIEST_DATE.
        // This is a fail-fast guard at config validation time. If someone sets
        // mode = "live" before the date, the application refuses to start.
        if self.strategy.mode.is_live() {
            let today = (chrono::Utc::now()
                + chrono::TimeDelta::seconds(super::constants::IST_UTC_OFFSET_SECONDS_I64))
            .date_naive();
            let earliest = match chrono::NaiveDate::from_ymd_opt(
                super::constants::LIVE_TRADING_EARLIEST_YEAR,
                super::constants::LIVE_TRADING_EARLIEST_MONTH,
                super::constants::LIVE_TRADING_EARLIEST_DAY,
            ) {
                Some(d) => d,
                None => bail!("LIVE_TRADING_EARLIEST_DATE constants are invalid"),
            };
            if today < earliest {
                // E1 (deferred): a `tv_sandbox_gate_blocks_total` counter
                // would require pulling the `metrics` crate into common,
                // which is currently framework-free. The bail!() already
                // fires an ERROR log via anyhow chain at the boot caller,
                // and the ERROR log path fires Telegram via the existing
                // hook — so the operator already gets notified. Revisit
                // if we ever need a Prometheus time-series of block count.
                bail!(
                    "SANDBOX GUARD: live trading mode is locked until {}. \
                     Current date (IST): {}. Use mode = \"sandbox\" or \"paper\" until then.",
                    earliest,
                    today
                );
            }
        }

        // Gap 6: URL format validation — fail-fast on invalid URLs.
        // Catches typos and misconfiguration at boot instead of cryptic runtime errors.
        let validate_url = |name: &str, url: &str, required_scheme: &str| -> Result<()> {
            if url.is_empty() {
                bail!("{name} must not be empty");
            }
            if !url.starts_with(required_scheme) {
                bail!("{name} must start with '{required_scheme}', got '{url}'");
            }
            Ok(())
        };

        validate_url(
            "dhan.rest_api_base_url",
            &self.dhan.rest_api_base_url,
            "https://",
        )?;
        validate_url("dhan.auth_base_url", &self.dhan.auth_base_url, "https://")?;
        validate_url("dhan.websocket_url", &self.dhan.websocket_url, "wss://")?;
        validate_url(
            "dhan.order_update_websocket_url",
            &self.dhan.order_update_websocket_url,
            "wss://",
        )?;
        validate_url(
            "dhan.instrument_csv_url",
            &self.dhan.instrument_csv_url,
            "https://",
        )?;
        // sandbox_base_url is optional (empty when mode=paper).
        if !self.dhan.sandbox_base_url.is_empty() {
            validate_url(
                "dhan.sandbox_base_url",
                &self.dhan.sandbox_base_url,
                "https://",
            )?;
        }

        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // =======================================================================
    // S6-Step4: Sandbox-only enforcement tests
    // =======================================================================

    fn make_sandbox_config(mode: TradingMode, dry_run: bool, until: &str) -> StrategyConfig {
        StrategyConfig {
            config_path: "test.toml".to_string(),
            capital: 100_000.0,
            dry_run,
            mode,
            sandbox_only_until: until.to_string(),
        }
    }

    #[test]
    fn test_sandbox_only_until_blocks_real_orders() {
        // Live trading + sandbox window not yet expired → BLOCKED.
        let cfg = make_sandbox_config(TradingMode::Live, false, "2026-06-30");
        let today = chrono::NaiveDate::from_ymd_opt(2026, 4, 14).unwrap();
        let result = cfg.check_sandbox_window(today);
        assert!(
            result.is_err(),
            "live trading before cutoff must be blocked"
        );
        let err = result.unwrap_err();
        assert!(err.contains("SANDBOX-ONLY VIOLATION"), "error: {err}");
        assert!(err.contains("2026-06-30"), "error must cite cutoff: {err}");
    }

    #[test]
    fn test_sandbox_already_past_returns_ok() {
        // Live trading + sandbox window already expired → OK.
        let cfg = make_sandbox_config(TradingMode::Live, false, "2026-06-30");
        let today = chrono::NaiveDate::from_ymd_opt(2026, 7, 1).unwrap();
        assert!(cfg.check_sandbox_window(today).is_ok());
    }

    #[test]
    fn test_sandbox_paper_mode_always_ok() {
        // Paper mode is always allowed regardless of cutoff.
        let cfg = make_sandbox_config(TradingMode::Paper, false, "2030-01-01");
        let today = chrono::NaiveDate::from_ymd_opt(2026, 4, 14).unwrap();
        assert!(cfg.check_sandbox_window(today).is_ok());
    }

    #[test]
    fn test_sandbox_sandbox_mode_always_ok() {
        // Sandbox mode is always allowed.
        let cfg = make_sandbox_config(TradingMode::Sandbox, false, "2030-01-01");
        let today = chrono::NaiveDate::from_ymd_opt(2026, 4, 14).unwrap();
        assert!(cfg.check_sandbox_window(today).is_ok());
    }

    #[test]
    fn test_sandbox_dry_run_with_paper_ok() {
        // Even with cutoff in the future, dry_run + Paper is OK.
        let cfg = make_sandbox_config(TradingMode::Paper, true, "2030-01-01");
        let today = chrono::NaiveDate::from_ymd_opt(2026, 4, 14).unwrap();
        assert!(cfg.check_sandbox_window(today).is_ok());
    }

    #[test]
    fn test_sandbox_date_parses_invalid() {
        // Bad date string returns Err with parse details.
        let cfg = make_sandbox_config(TradingMode::Live, false, "not-a-date");
        let today = chrono::NaiveDate::from_ymd_opt(2026, 4, 14).unwrap();
        let result = cfg.check_sandbox_window(today);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("invalid sandbox_only_until"));
    }

    #[test]
    fn test_sandbox_date_parses_valid() {
        // Valid YYYY-MM-DD parses correctly.
        let cfg = make_sandbox_config(TradingMode::Live, false, "2026-12-31");
        let today = chrono::NaiveDate::from_ymd_opt(2027, 1, 1).unwrap();
        assert!(cfg.check_sandbox_window(today).is_ok());
    }

    #[test]
    fn test_sandbox_default_value_is_2026_06_30() {
        // The default value is exactly the date Parthiban specified.
        assert_eq!(default_sandbox_only_until(), "2026-06-30");
    }

    #[test]
    fn test_sandbox_exact_cutoff_day_still_blocks() {
        // On the cutoff date itself, Live is still blocked (inclusive).
        let cfg = make_sandbox_config(TradingMode::Live, false, "2026-06-30");
        let today = chrono::NaiveDate::from_ymd_opt(2026, 6, 30).unwrap();
        let result = cfg.check_sandbox_window(today);
        assert!(
            result.is_err(),
            "cutoff day must be blocked; only days AFTER are allowed"
        );
    }

    // =======================================================================

    /// Helper: creates a valid ApplicationConfig for testing.
    /// Modify individual fields to test specific validation failures.
    fn make_valid_config() -> ApplicationConfig {
        ApplicationConfig {
            trading: TradingConfig {
                market_open_time: "09:00:00".to_string(),
                market_close_time: "15:30:00".to_string(),
                order_cutoff_time: "15:29:00".to_string(),
                data_collection_start: "09:00:00".to_string(),
                data_collection_end: "15:30:00".to_string(),
                timezone: "Asia/Kolkata".to_string(),
                max_orders_per_second: 10,
                nse_holidays: vec![
                    NseHolidayEntry {
                        date: "2026-01-26".to_string(),
                        name: "Republic Day".to_string(),
                    },
                    NseHolidayEntry {
                        date: "2026-03-03".to_string(),
                        name: "Holi".to_string(),
                    },
                ],
                muhurat_trading_dates: vec![NseHolidayEntry {
                    date: "2026-11-08".to_string(),
                    name: "Diwali 2026".to_string(),
                }],
                nse_mock_trading_dates: vec![],
            },
            dhan: DhanConfig {
                websocket_url: "wss://api-feed.dhan.co".to_string(),
                order_update_websocket_url: "wss://api-order-update.dhan.co".to_string(),
                rest_api_base_url: "https://api.dhan.co/v2".to_string(),
                sandbox_base_url: "https://sandbox.dhan.co/v2".to_string(),
                auth_base_url: "https://auth.dhan.co".to_string(),
                instrument_csv_url: "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
                    .to_string(),
                instrument_csv_fallback_url: "https://images.dhan.co/api-data/api-scrip-master.csv"
                    .to_string(),
                max_instruments_per_connection: 5000,
                max_websocket_connections: 5,
            },
            websocket: WebSocketConfig {
                ping_interval_secs: 10,
                pong_timeout_secs: 10,
                max_consecutive_pong_failures: 2,
                reconnect_initial_delay_ms: 500,
                reconnect_max_delay_ms: 30000,
                reconnect_max_attempts: 10,
                subscription_batch_size: 100,
                connection_stagger_ms: 10000,
            },
            questdb: QuestDbConfig {
                host: "tv-questdb".to_string(),
                http_port: 9000,
                pg_port: 8812,
                ilp_port: 9009,
            },
            valkey: ValkeyConfig {
                host: "tv-valkey".to_string(),
                port: 6379,
                max_connections: 16,
                password: String::new(),
            },
            prometheus: PrometheusConfig {
                host: "tv-prometheus".to_string(),
                port: 9090,
            },
            network: NetworkConfig {
                request_timeout_ms: 5000,
                websocket_connect_timeout_ms: 10000,
                retry_initial_delay_ms: 100,
                retry_max_delay_ms: 30000,
                retry_max_attempts: 5,
            },
            token: TokenConfig {
                refresh_before_expiry_hours: 1,
                token_validity_hours: 24,
            },
            risk: RiskConfig {
                max_daily_loss_percent: 2.0,
                max_position_size_lots: 100,
            },
            logging: LoggingConfig {
                level: "info".to_string(),
                format: "json".to_string(),
            },
            instrument: InstrumentConfig {
                daily_download_time: "08:55:00".to_string(),
                csv_cache_directory: "/tmp/tv-cache".to_string(),
                csv_cache_filename: "instruments.csv".to_string(),
                csv_download_timeout_secs: 120,
                build_window_start: "08:25:00".to_string(),
                build_window_end: "08:55:00".to_string(),
            },
            api: ApiConfig {
                host: "0.0.0.0".to_string(),
                port: 3001,
                allowed_origins: default_allowed_origins(),
            },
            subscription: SubscriptionConfig::default(),
            notification: NotificationConfig::default(),
            observability: ObservabilityConfig::default(),
            historical: HistoricalDataConfig::default(),
            strategy: StrategyConfig::default(),
            index_constituency: IndexConstituencyConfig::default(),
            greeks: GreeksConfig::default(),
            infrastructure: InfrastructureConfig::default(),
            partition_retention: PartitionRetentionConfig::default(),
        }
    }

    // -----------------------------------------------------------------------
    // TradingMode tests
    // -----------------------------------------------------------------------
    #[test]
    fn test_trading_mode_default_is_paper() {
        assert_eq!(TradingMode::default(), TradingMode::Paper);
    }

    #[test]
    fn test_trading_mode_paper_no_http() {
        assert!(!TradingMode::Paper.is_http_active());
        assert!(TradingMode::Paper.is_paper());
        assert!(!TradingMode::Paper.is_live());
        assert!(!TradingMode::Paper.is_sandbox());
    }

    #[test]
    fn test_trading_mode_sandbox_has_http() {
        assert!(TradingMode::Sandbox.is_http_active());
        assert!(!TradingMode::Sandbox.is_paper());
        assert!(!TradingMode::Sandbox.is_live());
        assert!(TradingMode::Sandbox.is_sandbox());
    }

    #[test]
    fn test_trading_mode_live_has_http() {
        assert!(TradingMode::Live.is_http_active());
        assert!(!TradingMode::Live.is_paper());
        assert!(TradingMode::Live.is_live());
        assert!(!TradingMode::Live.is_sandbox());
    }

    #[test]
    fn test_trading_mode_as_str() {
        assert_eq!(TradingMode::Paper.as_str(), "paper");
        assert_eq!(TradingMode::Sandbox.as_str(), "sandbox");
        assert_eq!(TradingMode::Live.as_str(), "live");
    }

    #[test]
    fn test_trading_mode_deserialize_lowercase() {
        let paper: TradingMode = serde_json::from_str("\"paper\"").unwrap();
        assert_eq!(paper, TradingMode::Paper);
        let sandbox: TradingMode = serde_json::from_str("\"sandbox\"").unwrap();
        assert_eq!(sandbox, TradingMode::Sandbox);
        let live: TradingMode = serde_json::from_str("\"live\"").unwrap();
        assert_eq!(live, TradingMode::Live);
    }

    #[test]
    fn test_trading_mode_serialize_lowercase() {
        assert_eq!(
            serde_json::to_string(&TradingMode::Paper).unwrap(),
            "\"paper\""
        );
        assert_eq!(
            serde_json::to_string(&TradingMode::Sandbox).unwrap(),
            "\"sandbox\""
        );
        assert_eq!(
            serde_json::to_string(&TradingMode::Live).unwrap(),
            "\"live\""
        );
    }

    // -----------------------------------------------------------------------

    #[test]
    fn test_valid_config_passes_validation() {
        let config = make_valid_config();
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_wrong_timezone_fails() {
        let mut config = make_valid_config();
        config.trading.timezone = "UTC".to_string();
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("Asia/Kolkata"));
    }

    #[test]
    fn test_invalid_time_format_fails() {
        let mut config = make_valid_config();
        config.trading.market_open_time = "9:15".to_string(); // Missing seconds
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("market_open_time"));
    }

    #[test]
    fn test_sebi_order_limit_exceeded_fails() {
        let mut config = make_valid_config();
        config.trading.max_orders_per_second = 11; // SEBI limit is 10
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("SEBI"));
    }

    #[test]
    fn test_sebi_order_limit_at_boundary_passes() {
        let mut config = make_valid_config();
        config.trading.max_orders_per_second = 10; // Exactly at limit
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_token_refresh_exceeds_validity_fails() {
        let mut config = make_valid_config();
        config.token.refresh_before_expiry_hours = 25;
        config.token.token_validity_hours = 24;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("refresh_before_expiry_hours"));
    }

    #[test]
    fn test_token_refresh_equals_validity_fails() {
        let mut config = make_valid_config();
        config.token.refresh_before_expiry_hours = 24;
        config.token.token_validity_hours = 24;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("refresh_before_expiry_hours"));
    }

    #[test]
    fn test_zero_request_timeout_fails() {
        let mut config = make_valid_config();
        config.network.request_timeout_ms = 0;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("request_timeout_ms"));
    }

    #[test]
    fn test_zero_retry_attempts_fails() {
        let mut config = make_valid_config();
        config.network.retry_max_attempts = 0;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("retry_max_attempts"));
    }

    #[test]
    fn test_zero_daily_loss_percent_fails() {
        let mut config = make_valid_config();
        config.risk.max_daily_loss_percent = 0.0;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("max_daily_loss_percent"));
    }

    #[test]
    fn test_negative_daily_loss_percent_fails() {
        let mut config = make_valid_config();
        config.risk.max_daily_loss_percent = -5.0;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("max_daily_loss_percent"));
    }

    #[test]
    fn test_daily_loss_percent_over_100_fails() {
        let mut config = make_valid_config();
        config.risk.max_daily_loss_percent = 100.1;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("max_daily_loss_percent"));
    }

    #[test]
    fn test_daily_loss_percent_at_100_passes() {
        let mut config = make_valid_config();
        config.risk.max_daily_loss_percent = 100.0;
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_daily_loss_percent_nan_fails() {
        let mut config = make_valid_config();
        config.risk.max_daily_loss_percent = f64::NAN;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("max_daily_loss_percent"));
    }

    #[test]
    fn test_daily_loss_percent_inf_fails() {
        let mut config = make_valid_config();
        config.risk.max_daily_loss_percent = f64::INFINITY;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("max_daily_loss_percent"));
    }

    #[test]
    fn test_daily_loss_percent_neg_inf_fails() {
        let mut config = make_valid_config();
        config.risk.max_daily_loss_percent = f64::NEG_INFINITY;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("max_daily_loss_percent"));
    }

    #[test]
    fn test_zero_csv_download_timeout_fails() {
        let mut config = make_valid_config();
        config.instrument.csv_download_timeout_secs = 0;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("csv_download_timeout_secs"));
    }

    #[test]
    fn test_invalid_instrument_download_time_fails() {
        let mut config = make_valid_config();
        config.instrument.daily_download_time = "not-a-time".to_string();
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("daily_download_time"));
    }

    // --- WebSocket Config Validation ---

    #[test]
    fn test_websocket_zero_ping_interval_fails() {
        let mut config = make_valid_config();
        config.websocket.ping_interval_secs = 0;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("ping_interval_secs"));
    }

    #[test]
    fn test_websocket_zero_subscription_batch_size_fails() {
        let mut config = make_valid_config();
        config.websocket.subscription_batch_size = 0;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("subscription_batch_size"));
    }

    #[test]
    fn test_websocket_subscription_batch_size_over_100_fails() {
        let mut config = make_valid_config();
        config.websocket.subscription_batch_size = 101;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("subscription_batch_size"));
    }

    #[test]
    fn test_websocket_zero_reconnect_max_attempts_means_infinite() {
        let mut config = make_valid_config();
        config.websocket.reconnect_max_attempts = 0;
        // 0 = infinite retries (production default). Must pass validation.
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_websocket_subscription_batch_size_exactly_100_passes() {
        let mut config = make_valid_config();
        config.websocket.subscription_batch_size = 100;
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_websocket_subscription_batch_size_exactly_1_passes() {
        let mut config = make_valid_config();
        config.websocket.subscription_batch_size = 1;
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_dhan_zero_max_websocket_connections_fails() {
        let mut config = make_valid_config();
        config.dhan.max_websocket_connections = 0;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("max_websocket_connections"));
    }

    #[test]
    fn test_websocket_zero_pong_timeout_fails() {
        let mut config = make_valid_config();
        config.websocket.pong_timeout_secs = 0;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("pong_timeout_secs"));
    }

    #[test]
    fn test_notification_zero_send_timeout_fails() {
        let mut config = make_valid_config();
        config.notification.send_timeout_ms = 0;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("notification.send_timeout_ms"));
    }

    #[test]
    fn test_build_window_start_after_end_fails() {
        let mut config = make_valid_config();
        config.instrument.build_window_start = "09:00:00".to_string();
        config.instrument.build_window_end = "08:30:00".to_string();
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("build_window_start"));
    }

    #[test]
    fn test_build_window_start_equals_end_fails() {
        let mut config = make_valid_config();
        config.instrument.build_window_start = "08:30:00".to_string();
        config.instrument.build_window_end = "08:30:00".to_string();
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("build_window_start"));
    }

    #[test]
    fn test_invalid_build_window_start_format_fails() {
        let mut config = make_valid_config();
        config.instrument.build_window_start = "8:25".to_string();
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("build_window_start"));
    }

    // -----------------------------------------------------------------------
    // Cross-field and boundary validation tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_market_close_before_open_time_values() {
        // Verify that market_close_time > market_open_time in the default config.
        // The validator doesn't currently reject this cross-field case,
        // but the parsed times should have the expected ordering.
        let config = make_valid_config();
        let open = NaiveTime::parse_from_str(&config.trading.market_open_time, "%H:%M:%S").unwrap();
        let close =
            NaiveTime::parse_from_str(&config.trading.market_close_time, "%H:%M:%S").unwrap();
        assert!(
            close > open,
            "market_close_time ({close}) must be after market_open_time ({open})"
        );
    }

    #[test]
    fn test_order_cutoff_before_close() {
        // Verify that order_cutoff_time < market_close_time in the default config.
        let config = make_valid_config();
        let cutoff =
            NaiveTime::parse_from_str(&config.trading.order_cutoff_time, "%H:%M:%S").unwrap();
        let close =
            NaiveTime::parse_from_str(&config.trading.market_close_time, "%H:%M:%S").unwrap();
        assert!(
            cutoff < close,
            "order_cutoff_time ({cutoff}) must be before market_close_time ({close})"
        );
    }

    #[test]
    fn test_historical_lookback_zero_fails() {
        let mut config = make_valid_config();
        config.historical.enabled = true;
        config.historical.lookback_days = 0;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("lookback_days"));
    }

    #[test]
    fn test_historical_lookback_over_90_fails() {
        let mut config = make_valid_config();
        config.historical.enabled = true;
        config.historical.lookback_days = 91;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("lookback_days"));
    }

    #[test]
    fn test_historical_lookback_at_90_passes() {
        let mut config = make_valid_config();
        config.historical.enabled = true;
        config.historical.lookback_days = 90;
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_historical_lookback_at_1_passes() {
        let mut config = make_valid_config();
        config.historical.enabled = true;
        config.historical.lookback_days = 1;
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_historical_request_timeout_zero_fails() {
        let mut config = make_valid_config();
        config.historical.enabled = true;
        config.historical.request_timeout_secs = 0;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("request_timeout_secs"));
    }

    #[test]
    fn test_historical_disabled_skips_validation() {
        let mut config = make_valid_config();
        config.historical.enabled = false;
        config.historical.lookback_days = 999; // Would fail if validated
        config.historical.request_timeout_secs = 0; // Would fail if validated
        assert!(
            config.validate().is_ok(),
            "disabled historical config should skip validation"
        );
    }

    #[test]
    fn test_websocket_excessive_read_timeout_fails() {
        let mut config = make_valid_config();
        // ping_interval_secs * (max_consecutive_pong_failures + 1) + pong_timeout_secs
        // = 30 * (10 + 1) + 30 = 360s, which exceeds 2 * SERVER_PING_TIMEOUT_SECS
        config.websocket.ping_interval_secs = 30;
        config.websocket.max_consecutive_pong_failures = 10;
        config.websocket.pong_timeout_secs = 30;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("websocket read timeout"));
    }

    // Mutation-targeted tests: catch `replace + with -`, `replace * with +`,
    // `replace + with *`, `replace > with >=` in validate() read timeout logic.

    #[test]
    fn test_websocket_read_timeout_exactly_at_boundary_passes() {
        // computed = ping * (failures + 1) + pong
        // With ping=10, failures=3, pong=10: computed = 10*(3+1)+10 = 50
        // max = SERVER_PING_TIMEOUT_SECS * 2 = 80
        // 50 < 80 → should pass
        let mut config = make_valid_config();
        config.websocket.ping_interval_secs = 10;
        config.websocket.max_consecutive_pong_failures = 3;
        config.websocket.pong_timeout_secs = 10;
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_websocket_read_timeout_exactly_at_max_passes() {
        // computed = ping * (failures + 1) + pong = 80 exactly
        // max = 80
        // 80 > 80 is FALSE → should PASS (equal is OK)
        // This catches `replace > with >=` mutation — if >= were used, this would fail
        let mut config = make_valid_config();
        // 10 * (6 + 1) + 10 = 10 * 7 + 10 = 80
        config.websocket.ping_interval_secs = 10;
        config.websocket.max_consecutive_pong_failures = 6;
        config.websocket.pong_timeout_secs = 10;
        assert!(
            config.validate().is_ok(),
            "computed=80, max=80 → equal should PASS (> not >=)"
        );
    }

    #[test]
    fn test_websocket_read_timeout_one_above_max_fails() {
        // computed = 81, max = 80
        // 81 > 80 → should FAIL
        let mut config = make_valid_config();
        // 10 * (6 + 1) + 11 = 81
        config.websocket.ping_interval_secs = 10;
        config.websocket.max_consecutive_pong_failures = 6;
        config.websocket.pong_timeout_secs = 11;
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("websocket read timeout"));
    }

    #[test]
    fn test_websocket_read_timeout_plus_one_matters() {
        // This catches `replace + with -` and `replace + with *` on the `+ 1`
        // With failures=0: computed = ping * (0 + 1) + pong = ping + pong
        // Mutation `+ 1` → `- 1`: computed = ping * (0 - 1) + pong = UNDERFLOW or 0 + pong
        // Mutation `+ 1` → `* 1`: computed = ping * (0 * 1) + pong = 0 + pong
        // Both mutations would give a DIFFERENT computed value
        let mut config = make_valid_config();
        // Normal: 40 * (0 + 1) + 41 = 40 + 41 = 81 > 80 → FAIL
        // Mutant (-1): 40 * (0 - 1) + 41 → saturating = 0 + 41 = 41 ≤ 80 → PASS (wrong!)
        // Mutant (*1): 40 * (0 * 1) + 41 = 0 + 41 = 41 ≤ 80 → PASS (wrong!)
        config.websocket.ping_interval_secs = 40;
        config.websocket.max_consecutive_pong_failures = 0;
        config.websocket.pong_timeout_secs = 41;
        let err = config.validate().unwrap_err();
        assert!(
            err.to_string().contains("websocket read timeout"),
            "failures=0 with +1 should compute 81 > 80"
        );
    }

    #[test]
    fn test_websocket_read_timeout_times_two_matters() {
        // This catches `replace * with +` on `SERVER_PING_TIMEOUT_SECS * 2`
        // Normal max = 40 * 2 = 80
        // Mutant max = 40 + 2 = 42
        // With computed = 50: normal 50 ≤ 80 → PASS, mutant 50 > 42 → FAIL (wrong!)
        let mut config = make_valid_config();
        // computed = 10 * (3 + 1) + 10 = 50
        config.websocket.ping_interval_secs = 10;
        config.websocket.max_consecutive_pong_failures = 3;
        config.websocket.pong_timeout_secs = 10;
        // 50 ≤ 80 → PASS (correct)
        // If max were 42 (mutant): 50 > 42 → FAIL (mutation caught!)
        assert!(
            config.validate().is_ok(),
            "computed=50 should be within max=80"
        );
    }

    #[test]
    fn test_feed_mode_ticker_passes() {
        let config = SubscriptionConfig {
            feed_mode: "Ticker".to_string(),
            ..SubscriptionConfig::default()
        };
        assert!(config.parsed_feed_mode().is_ok());
    }

    #[test]
    fn test_feed_mode_quote_passes() {
        let config = SubscriptionConfig {
            feed_mode: "Quote".to_string(),
            ..SubscriptionConfig::default()
        };
        assert!(config.parsed_feed_mode().is_ok());
    }

    #[test]
    fn test_feed_mode_full_passes() {
        let config = SubscriptionConfig {
            feed_mode: "Full".to_string(),
            ..SubscriptionConfig::default()
        };
        assert!(config.parsed_feed_mode().is_ok());
    }

    #[test]
    fn test_feed_mode_invalid_string_fails() {
        let config = SubscriptionConfig {
            feed_mode: "invalid".to_string(),
            ..SubscriptionConfig::default()
        };
        let err = config.parsed_feed_mode().unwrap_err();
        assert!(err.to_string().contains("Ticker/Quote/Full"));
    }

    #[test]
    fn test_feed_mode_case_sensitive() {
        let config = SubscriptionConfig {
            feed_mode: "ticker".to_string(), // lowercase — must fail
            ..SubscriptionConfig::default()
        };
        assert!(
            config.parsed_feed_mode().is_err(),
            "feed_mode is case-sensitive — 'ticker' should fail"
        );
    }

    // =====================================================================
    // Additional coverage: default impls, edge cases, more validation paths
    // =====================================================================

    #[test]
    fn test_strategy_config_default() {
        let config = StrategyConfig::default();
        assert_eq!(config.config_path, "config/strategies.toml");
        assert!((config.capital - 1_000_000.0).abs() < f64::EPSILON);
        assert!(config.dry_run, "dry_run must default to true");
    }

    #[test]
    fn test_notification_config_default() {
        let config = NotificationConfig::default();
        assert_eq!(config.telegram_api_base_url, "https://api.telegram.org");
        assert_eq!(config.send_timeout_ms, 10_000);
        assert!(!config.sns_enabled);
    }

    #[test]
    fn test_observability_config_default() {
        let config = ObservabilityConfig::default();
        assert_eq!(config.metrics_port, 9091);
        assert!(config.metrics_enabled);
        assert!(config.tracing_enabled);
        assert!(config.otlp_endpoint.contains("4317"));
    }

    #[test]
    fn test_historical_data_config_default() {
        let config = HistoricalDataConfig::default();
        assert!(config.enabled);
        assert_eq!(config.lookback_days, 90);
        assert_eq!(config.request_timeout_secs, 30);
        assert_eq!(config.max_retries, 3);
        assert_eq!(config.request_delay_ms, 500);
    }

    #[test]
    fn test_index_constituency_config_default() {
        let config = IndexConstituencyConfig::default();
        assert!(config.enabled);
        assert_eq!(config.download_timeout_secs, 30);
        assert_eq!(config.max_concurrent_downloads, 5);
        assert_eq!(config.inter_batch_delay_ms, 200);
    }

    #[test]
    fn test_greeks_config_default() {
        let config = GreeksConfig::default();
        assert!(config.enabled);
        assert_eq!(config.fetch_interval_secs, 60);
        assert!((config.risk_free_rate - 0.10).abs() < f64::EPSILON);
        assert!((config.dividend_yield - 0.0).abs() < f64::EPSILON);
        assert_eq!(config.iv_solver_max_iterations, 50);
        assert!((config.iv_solver_tolerance - 1e-8).abs() < f64::EPSILON);
        assert!((config.day_count - 365.0).abs() < f64::EPSILON);
        assert_eq!(config.rate_mode, "dhan");
    }

    #[test]
    fn test_subscription_config_default() {
        let config = SubscriptionConfig::default();
        assert_eq!(config.feed_mode, "Full");
        assert!(config.subscribe_index_derivatives);
        assert!(config.subscribe_stock_derivatives);
        assert!(config.subscribe_display_indices);
        assert!(config.subscribe_stock_equities);
        assert_eq!(config.stock_atm_strikes_above, 10);
        assert_eq!(config.stock_atm_strikes_below, 10);
        assert!(config.stock_default_atm_fallback_enabled);
    }

    #[test]
    fn test_default_allowed_origins() {
        let origins = default_allowed_origins();
        assert_eq!(origins.len(), 2);
        assert!(origins.contains(&"http://localhost:3000".to_string()));
        assert!(origins.contains(&"http://localhost:3001".to_string()));
    }

    #[test]
    fn test_feed_mode_empty_string_fails() {
        let config = SubscriptionConfig {
            feed_mode: String::new(),
            ..SubscriptionConfig::default()
        };
        assert!(config.parsed_feed_mode().is_err());
    }

    #[test]
    fn test_invalid_market_close_time_fails() {
        let mut config = make_valid_config();
        config.trading.market_close_time = "bad".to_string();
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("market_close_time"));
    }

    #[test]
    fn test_invalid_order_cutoff_time_fails() {
        let mut config = make_valid_config();
        config.trading.order_cutoff_time = "bad".to_string();
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("order_cutoff_time"));
    }

    #[test]
    fn test_invalid_data_collection_start_fails() {
        let mut config = make_valid_config();
        config.trading.data_collection_start = "bad".to_string();
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("data_collection_start"));
    }

    #[test]
    fn test_invalid_data_collection_end_fails() {
        let mut config = make_valid_config();
        config.trading.data_collection_end = "bad".to_string();
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("data_collection_end"));
    }

    #[test]
    fn test_invalid_build_window_end_format_fails() {
        let mut config = make_valid_config();
        config.instrument.build_window_end = "bad".to_string();
        let err = config.validate().unwrap_err();
        assert!(err.to_string().contains("build_window_end"));
    }

    #[test]
    fn test_invalid_holiday_date_fails_validation() {
        // Exercises the TradingCalendar::from_config error propagation (line 638)
        let mut config = make_valid_config();
        config.trading.nse_holidays = vec![NseHolidayEntry {
            date: "not-a-date".to_string(),
            name: "Bad Holiday".to_string(),
        }];
        let err = config.validate().unwrap_err();
        assert!(
            err.to_string().contains("not-a-date"),
            "error should mention the bad date: {}",
            err
        );
    }

    // -----------------------------------------------------------------------
    // D1: Sandbox Guard Tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_sandbox_guard_blocks_live_before_july() {
        let mut config = make_valid_config();
        config.strategy.mode = TradingMode::Live;
        // The guard uses the current system date. Since we're before July 2026,
        // this should fail. If running after July 2026, the guard is no longer
        // active and this test should be updated.
        let today = (chrono::Utc::now()
            + chrono::TimeDelta::seconds(crate::compat::constants::IST_UTC_OFFSET_SECONDS_I64))
        .date_naive();
        let earliest = chrono::NaiveDate::from_ymd_opt(
            crate::compat::constants::LIVE_TRADING_EARLIEST_YEAR,
            crate::compat::constants::LIVE_TRADING_EARLIEST_MONTH,
            crate::compat::constants::LIVE_TRADING_EARLIEST_DAY,
        )
        .unwrap();
        if today < earliest {
            let err = config.validate().unwrap_err();
            assert!(
                err.to_string().contains("SANDBOX GUARD"),
                "should mention SANDBOX GUARD: {}",
                err
            );
        }
        // If today >= earliest, live mode is permitted — test passes trivially.
    }

    #[test]
    fn test_sandbox_and_paper_modes_always_pass_guard() {
        let mut config = make_valid_config();
        config.strategy.mode = TradingMode::Sandbox;
        assert!(config.validate().is_ok(), "sandbox mode should always pass");

        config.strategy.mode = TradingMode::Paper;
        assert!(config.validate().is_ok(), "paper mode should always pass");
    }

    #[test]
    fn test_base_config_dry_run_is_true() {
        let config = make_valid_config();
        assert!(
            config.strategy.dry_run,
            "default config must have dry_run = true for safety"
        );
    }

    #[test]
    fn test_base_config_mode_is_not_live() {
        let config = make_valid_config();
        assert!(
            !config.strategy.mode.is_live(),
            "default config must NOT be in live mode"
        );
    }

    // -------------------------------------------------------------------
    // GAP 28: Depth config validation tests
    // -------------------------------------------------------------------

    #[test]
    fn test_subscription_config_default_has_depth_disabled() {
        let config = SubscriptionConfig::default();
        assert!(!config.enable_twenty_depth);
    }

    #[test]
    fn test_subscription_config_default_depth_max_instruments() {
        let config = SubscriptionConfig::default();
        assert_eq!(config.twenty_depth_max_instruments, 50);
    }

    #[test]
    fn test_subscription_config_depth_max_instruments_matches_dhan_limit() {
        // Dhan docs: max 50 instruments per 20-level depth connection
        let config = SubscriptionConfig::default();
        assert!(config.twenty_depth_max_instruments <= 50);
    }

    #[test]
    fn test_subscription_config_all_fields_present() {
        let config = SubscriptionConfig::default();
        assert_eq!(config.feed_mode, "Full");
        assert!(config.subscribe_index_derivatives);
        assert!(config.subscribe_stock_derivatives);
        assert!(config.subscribe_display_indices);
        assert!(config.subscribe_stock_equities);
        assert_eq!(config.stock_atm_strikes_above, 10);
        assert_eq!(config.stock_atm_strikes_below, 10);
        assert!(config.stock_default_atm_fallback_enabled);
        // Depth fields
        assert!(!config.enable_twenty_depth);
        assert_eq!(config.twenty_depth_max_instruments, 50);
    }

    #[test]
    fn test_default_config_trading_mode_is_paper_not_live() {
        let config = make_valid_config();
        assert!(config.strategy.mode.is_paper());
        assert!(!config.strategy.mode.is_live());
        assert!(!config.strategy.mode.is_sandbox());
        assert!(!config.strategy.mode.is_http_active());
    }

    #[test]
    fn test_build_ilp_conf_string_tcp_only() {
        let config = QuestDbConfig {
            host: "tv-questdb".to_string(),
            http_port: 9000,
            pg_port: 8812,
            ilp_port: 9009,
        };
        let conf = config.build_ilp_conf_string();
        assert_eq!(conf, "tcp::addr=tv-questdb:9009;");
        // TCP mode does NOT support retry_timeout, init_buf_size, request_timeout
        // (those are HTTP-only in questdb-rs 6.1.0)
        assert!(!conf.contains("retry_timeout"));
        assert!(!conf.contains("init_buf_size"));
        assert!(!conf.contains("request_timeout"));
    }

    #[test]
    fn test_build_ilp_conf_string_custom_port() {
        let config = QuestDbConfig {
            host: "10.0.1.5".to_string(),
            http_port: 9000,
            pg_port: 8812,
            ilp_port: 19009,
        };
        let conf = config.build_ilp_conf_string();
        assert!(conf.contains("tcp::addr=10.0.1.5:19009;"));
    }
}
