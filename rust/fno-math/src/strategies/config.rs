//! TOML strategy configuration: parse → validate → convert to `StrategyDefinition`.
//!
//! Strategies are defined declaratively in TOML files. Each file can contain
//! multiple strategy definitions. The config module handles:
//! 1. Serde deserialization from TOML
//! 2. Validation (field names, operator strings, value ranges)
//! 3. Conversion to runtime `StrategyDefinition` structs
//!
//! # TOML Format
//! ```toml
//! [[strategy]]
//! name = "rsi_mean_reversion"
//! security_ids = [13, 25, 27]
//! position_size_fraction = 0.1
//! stop_loss_atr_multiplier = 2.0
//! target_atr_multiplier = 3.0
//! confirmation_ticks = 0
//! trailing_stop_enabled = false
//! trailing_stop_atr_multiplier = 1.5
//!
//! [[strategy.entry_long]]
//! field = "rsi"
//! operator = "lt"
//! threshold = 30.0
//!
//! [[strategy.entry_short]]
//! field = "rsi"
//! operator = "gt"
//! threshold = 70.0
//!
//! [[strategy.exit]]
//! field = "rsi"
//! operator = "gt"
//! threshold = 50.0
//! ```

use serde::Deserialize;

use super::types::{ComparisonOp, Condition, IndicatorField, StrategyDefinition};
use crate::indicators::IndicatorParams;

// ---------------------------------------------------------------------------
// TOML Serde Types — intermediate deserialization layer
// ---------------------------------------------------------------------------

/// Root TOML document containing strategy definitions and optional indicator params.
#[derive(Debug, Deserialize)]
pub struct StrategyConfigFile {
    /// Optional indicator parameters override (defaults used if absent).
    #[serde(default)]
    pub indicator_params: Option<TomlIndicatorParams>,
    /// List of strategy definitions.
    #[serde(default)]
    pub strategy: Vec<TomlStrategy>,
}

/// TOML representation of indicator parameters.
#[derive(Debug, Deserialize)]
pub struct TomlIndicatorParams {
    #[serde(default = "default_ema_fast_period")]
    pub ema_fast_period: u16,
    #[serde(default = "default_ema_slow_period")]
    pub ema_slow_period: u16,
    #[serde(default = "default_macd_signal_period")]
    pub macd_signal_period: u16,
    #[serde(default = "default_rsi_period")]
    pub rsi_period: u16,
    #[serde(default = "default_sma_period")]
    pub sma_period: u16,
    #[serde(default = "default_atr_period")]
    pub atr_period: u16,
    #[serde(default = "default_adx_period")]
    pub adx_period: u16,
    #[serde(default = "default_supertrend_multiplier")]
    pub supertrend_multiplier: f64,
    #[serde(default = "default_bollinger_multiplier")]
    pub bollinger_multiplier: f64,
}

// Default value functions for serde
const fn default_ema_fast_period() -> u16 {
    12
}
const fn default_ema_slow_period() -> u16 {
    26
}
const fn default_macd_signal_period() -> u16 {
    9
}
const fn default_rsi_period() -> u16 {
    14
}
const fn default_sma_period() -> u16 {
    20
}
const fn default_atr_period() -> u16 {
    14
}
const fn default_adx_period() -> u16 {
    14
}
const fn default_supertrend_multiplier() -> f64 {
    3.0
}
const fn default_bollinger_multiplier() -> f64 {
    2.0
}

/// TOML representation of a single strategy.
#[derive(Debug, Deserialize)]
pub struct TomlStrategy {
    /// Human-readable name.
    pub name: String,
    /// Security IDs this strategy applies to.
    #[serde(default)]
    pub security_ids: Vec<u32>,
    /// Position size fraction (0.0 - 1.0).
    #[serde(default = "default_position_size")]
    pub position_size_fraction: f64,
    /// Stop-loss in ATR multiples.
    #[serde(default = "default_stop_loss_atr")]
    pub stop_loss_atr_multiplier: f64,
    /// Target in ATR multiples.
    #[serde(default = "default_target_atr")]
    pub target_atr_multiplier: f64,
    /// Confirmation ticks before entry.
    #[serde(default)]
    pub confirmation_ticks: u32,
    /// Enable trailing stop.
    #[serde(default)]
    pub trailing_stop_enabled: bool,
    /// Trailing stop ATR multiplier.
    #[serde(default = "default_trailing_stop_atr")]
    pub trailing_stop_atr_multiplier: f64,
    /// Long entry conditions (AND logic).
    #[serde(default)]
    pub entry_long: Vec<TomlCondition>,
    /// Short entry conditions (AND logic).
    #[serde(default)]
    pub entry_short: Vec<TomlCondition>,
    /// Exit conditions (OR logic).
    #[serde(default)]
    pub exit: Vec<TomlCondition>,
}

const fn default_position_size() -> f64 {
    0.1
}
const fn default_stop_loss_atr() -> f64 {
    2.0
}
const fn default_target_atr() -> f64 {
    3.0
}
const fn default_trailing_stop_atr() -> f64 {
    1.5
}

/// TOML representation of a single condition.
#[derive(Debug, Deserialize)]
pub struct TomlCondition {
    /// Indicator field name (e.g., "rsi", "macd_line", "ema_fast").
    pub field: String,
    /// Comparison operator (e.g., "gt", "lt", "gte", "lte", "cross_above", "cross_below").
    pub operator: String,
    /// Threshold value.
    pub threshold: f64,
}

// ---------------------------------------------------------------------------
// Config Errors
// ---------------------------------------------------------------------------

/// Errors that can occur during strategy configuration parsing or validation.
#[derive(Debug, thiserror::Error)]
pub enum StrategyConfigError {
    /// TOML parsing failed.
    #[error("TOML parse error: {0}")]
    TomlParse(#[from] toml::de::Error),

    /// Unknown indicator field name in condition.
    #[error("unknown indicator field '{field}' in strategy '{strategy}'")]
    UnknownField { strategy: String, field: String },

    /// Unknown comparison operator in condition.
    #[error("unknown operator '{operator}' in strategy '{strategy}'")]
    UnknownOperator { strategy: String, operator: String },

    /// Validation error.
    #[error("validation error in strategy '{strategy}': {message}")]
    Validation { strategy: String, message: String },

    /// File I/O error.
    #[error("failed to read config file: {0}")]
    Io(#[from] std::io::Error),
}

// ---------------------------------------------------------------------------
// Parsing & Conversion
// ---------------------------------------------------------------------------

/// Parses a TOML string into a list of `StrategyDefinition` and optional `IndicatorParams`.
///
/// # Cold path
/// Called at startup and on hot-reload. Heap allocation is acceptable.
pub fn parse_strategy_config(
    toml_content: &str,
) -> Result<(Vec<StrategyDefinition>, IndicatorParams), StrategyConfigError> {
    let config: StrategyConfigFile = toml::from_str(toml_content)?;

    let indicator_params = config
        .indicator_params
        .map(|p| IndicatorParams {
            ema_fast_period: p.ema_fast_period,
            ema_slow_period: p.ema_slow_period,
            macd_signal_period: p.macd_signal_period,
            rsi_period: p.rsi_period,
            sma_period: p.sma_period,
            atr_period: p.atr_period,
            adx_period: p.adx_period,
            supertrend_multiplier: p.supertrend_multiplier,
            bollinger_multiplier: p.bollinger_multiplier,
        })
        .unwrap_or_default();

    let mut definitions = Vec::with_capacity(config.strategy.len());
    for strategy in config.strategy {
        let def = convert_strategy(&strategy)?;
        definitions.push(def);
    }

    Ok((definitions, indicator_params))
}

/// Loads strategy configuration from a file path.
///
/// # Cold path
/// Called at startup and on hot-reload.
pub fn load_strategy_config_file(
    path: &std::path::Path,
) -> Result<(Vec<StrategyDefinition>, IndicatorParams), StrategyConfigError> {
    // O(1) EXEMPT: cold path, called once at config load — blocking I/O acceptable
    let content = std::fs::read_to_string(path)?;
    parse_strategy_config(&content)
}

// O(1) EXEMPT: begin — cold path, called once at config load
/// Converts a TOML strategy to a runtime `StrategyDefinition`.
fn convert_strategy(toml: &TomlStrategy) -> Result<StrategyDefinition, StrategyConfigError> {
    // Validate position size (range.contains is O(1) — not Vec.contains)
    if !(0.0..=1.0).contains(&toml.position_size_fraction) {
        return Err(StrategyConfigError::Validation {
            strategy: toml.name.clone(),
            message: format!(
                "position_size_fraction must be 0.0-1.0, got {}",
                toml.position_size_fraction
            ),
        });
    }

    // Validate ATR multipliers are positive
    if toml.stop_loss_atr_multiplier <= 0.0 {
        return Err(StrategyConfigError::Validation {
            strategy: toml.name.clone(),
            message: "stop_loss_atr_multiplier must be positive".to_owned(),
        });
    }
    if toml.target_atr_multiplier <= 0.0 {
        return Err(StrategyConfigError::Validation {
            strategy: toml.name.clone(),
            message: "target_atr_multiplier must be positive".to_owned(),
        });
    }

    // Validate at least one entry condition exists
    if toml.entry_long.is_empty() && toml.entry_short.is_empty() {
        return Err(StrategyConfigError::Validation {
            strategy: toml.name.clone(),
            message: "strategy must have at least one entry_long or entry_short condition"
                .to_owned(),
        });
    }

    let entry_long_conditions = toml
        .entry_long
        .iter()
        .map(|c| convert_condition(c, &toml.name))
        .collect::<Result<Vec<_>, _>>()?;

    let entry_short_conditions = toml
        .entry_short
        .iter()
        .map(|c| convert_condition(c, &toml.name))
        .collect::<Result<Vec<_>, _>>()?;

    let exit_conditions = toml
        .exit
        .iter()
        .map(|c| convert_condition(c, &toml.name))
        .collect::<Result<Vec<_>, _>>()?;

    Ok(StrategyDefinition {
        name: toml.name.clone(),
        security_ids: toml.security_ids.clone(),
        entry_long_conditions,
        entry_short_conditions,
        exit_conditions,
        position_size_fraction: toml.position_size_fraction,
        stop_loss_atr_multiplier: toml.stop_loss_atr_multiplier,
        target_atr_multiplier: toml.target_atr_multiplier,
        confirmation_ticks: toml.confirmation_ticks,
        trailing_stop_enabled: toml.trailing_stop_enabled,
        trailing_stop_atr_multiplier: toml.trailing_stop_atr_multiplier,
    })
}
// O(1) EXEMPT: end

/// Converts a TOML condition to a runtime `Condition`.
fn convert_condition(
    toml: &TomlCondition,
    strategy_name: &str,
) -> Result<Condition, StrategyConfigError> {
    let field = parse_indicator_field(&toml.field, strategy_name)?;
    let operator = parse_comparison_op(&toml.operator, strategy_name)?;

    Ok(Condition {
        field,
        operator,
        threshold: toml.threshold,
    })
}

// O(1) EXEMPT: begin — cold path, called once per condition at config load
/// Parses a string indicator field name to `IndicatorField`.
fn parse_indicator_field(
    name: &str,
    strategy_name: &str,
) -> Result<IndicatorField, StrategyConfigError> {
    match name {
        "rsi" => Ok(IndicatorField::Rsi),
        "macd_line" => Ok(IndicatorField::MacdLine),
        "macd_signal" => Ok(IndicatorField::MacdSignal),
        "macd_histogram" => Ok(IndicatorField::MacdHistogram),
        "ema_fast" => Ok(IndicatorField::EmaFast),
        "ema_slow" => Ok(IndicatorField::EmaSlow),
        "sma" => Ok(IndicatorField::Sma),
        "vwap" => Ok(IndicatorField::Vwap),
        "bollinger_upper" => Ok(IndicatorField::BollingerUpper),
        "bollinger_middle" => Ok(IndicatorField::BollingerMiddle),
        "bollinger_lower" => Ok(IndicatorField::BollingerLower),
        "atr" => Ok(IndicatorField::Atr),
        "supertrend" => Ok(IndicatorField::Supertrend),
        "adx" => Ok(IndicatorField::Adx),
        "obv" => Ok(IndicatorField::Obv),
        "last_traded_price" | "ltp" => Ok(IndicatorField::LastTradedPrice),
        "volume" => Ok(IndicatorField::Volume),
        "day_high" => Ok(IndicatorField::DayHigh),
        "day_low" => Ok(IndicatorField::DayLow),
        _ => Err(StrategyConfigError::UnknownField {
            strategy: strategy_name.to_owned(),
            field: name.to_owned(),
        }),
    }
}

/// Parses a string comparison operator to `ComparisonOp`.
fn parse_comparison_op(op: &str, strategy_name: &str) -> Result<ComparisonOp, StrategyConfigError> {
    match op {
        "gt" | ">" => Ok(ComparisonOp::Gt),
        "gte" | ">=" => Ok(ComparisonOp::Gte),
        "lt" | "<" => Ok(ComparisonOp::Lt),
        "lte" | "<=" => Ok(ComparisonOp::Lte),
        "cross_above" => Ok(ComparisonOp::CrossAbove),
        "cross_below" => Ok(ComparisonOp::CrossBelow),
        _ => Err(StrategyConfigError::UnknownOperator {
            strategy: strategy_name.to_owned(),
            operator: op.to_owned(),
        }),
    }
}
// O(1) EXEMPT: end

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -----------------------------------------------------------------------
    // position_size_fraction boundary values
    // -----------------------------------------------------------------------

    #[test]
    fn test_position_size_fraction_zero_accepted() {
        let toml = r#"
[[strategy]]
name = "zero_size"
security_ids = [1333]
position_size_fraction = 0.0
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let result = parse_strategy_config(toml);
        assert!(
            result.is_ok(),
            "position_size_fraction=0.0 must be accepted"
        );
        let (defs, _) = result.unwrap();
        assert!((defs[0].position_size_fraction - 0.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_position_size_fraction_one_accepted() {
        let toml = r#"
[[strategy]]
name = "full_size"
security_ids = [1333]
position_size_fraction = 1.0
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let result = parse_strategy_config(toml);
        assert!(
            result.is_ok(),
            "position_size_fraction=1.0 must be accepted"
        );
        let (defs, _) = result.unwrap();
        assert!((defs[0].position_size_fraction - 1.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_position_size_fraction_above_one_rejected() {
        let toml = r#"
[[strategy]]
name = "too_large"
security_ids = [1333]
position_size_fraction = 1.01
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let result = parse_strategy_config(toml);
        assert!(
            result.is_err(),
            "position_size_fraction=1.01 must be rejected"
        );
        match result.unwrap_err() {
            StrategyConfigError::Validation { strategy, message } => {
                assert_eq!(strategy, "too_large");
                assert!(message.contains("position_size_fraction"));
            }
            other => panic!("expected Validation, got: {other:?}"),
        }
    }

    #[test]
    fn test_position_size_fraction_negative_rejected() {
        let toml = r#"
[[strategy]]
name = "negative"
security_ids = [1333]
position_size_fraction = -0.1
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let result = parse_strategy_config(toml);
        assert!(
            result.is_err(),
            "position_size_fraction=-0.1 must be rejected"
        );
    }

    // -----------------------------------------------------------------------
    // Empty entry conditions error
    // -----------------------------------------------------------------------

    #[test]
    fn test_no_entry_conditions_rejected() {
        let toml = r#"
[[strategy]]
name = "no_entries"
security_ids = [1333]
position_size_fraction = 0.1
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0
"#;
        let result = parse_strategy_config(toml);
        assert!(
            result.is_err(),
            "strategy with no entry conditions must be rejected"
        );
        match result.unwrap_err() {
            StrategyConfigError::Validation { strategy, message } => {
                assert_eq!(strategy, "no_entries");
                assert!(message.contains("entry_long"));
            }
            other => panic!("expected Validation, got: {other:?}"),
        }
    }

    #[test]
    fn test_only_short_entry_accepted() {
        let toml = r#"
[[strategy]]
name = "short_only"
security_ids = [1333]
position_size_fraction = 0.1
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_short]]
field = "rsi"
operator = "gt"
threshold = 70.0
"#;
        let result = parse_strategy_config(toml);
        assert!(
            result.is_ok(),
            "strategy with only entry_short must be accepted"
        );
        let (defs, _) = result.unwrap();
        assert!(defs[0].entry_long_conditions.is_empty());
        assert_eq!(defs[0].entry_short_conditions.len(), 1);
    }

    // -----------------------------------------------------------------------
    // ATR multiplier validation
    // -----------------------------------------------------------------------

    #[test]
    fn test_zero_stop_loss_atr_rejected() {
        let toml = r#"
[[strategy]]
name = "bad_sl"
security_ids = [1333]
stop_loss_atr_multiplier = 0.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let result = parse_strategy_config(toml);
        assert!(
            result.is_err(),
            "stop_loss_atr_multiplier=0 must be rejected"
        );
    }

    #[test]
    fn test_negative_target_atr_rejected() {
        let toml = r#"
[[strategy]]
name = "bad_target"
security_ids = [1333]
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = -1.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let result = parse_strategy_config(toml);
        assert!(
            result.is_err(),
            "target_atr_multiplier=-1.0 must be rejected"
        );
    }

    // -----------------------------------------------------------------------
    // Unknown fields and operators
    // -----------------------------------------------------------------------

    #[test]
    fn test_unknown_field_rejected() {
        let toml = r#"
[[strategy]]
name = "bad_field"
security_ids = [1333]
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "nonexistent_indicator"
operator = "lt"
threshold = 30.0
"#;
        let result = parse_strategy_config(toml);
        assert!(result.is_err());
        match result.unwrap_err() {
            StrategyConfigError::UnknownField { strategy, field } => {
                assert_eq!(strategy, "bad_field");
                assert_eq!(field, "nonexistent_indicator");
            }
            other => panic!("expected UnknownField, got: {other:?}"),
        }
    }

    #[test]
    fn test_unknown_operator_rejected() {
        let toml = r#"
[[strategy]]
name = "bad_op"
security_ids = [1333]
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "equals"
threshold = 30.0
"#;
        let result = parse_strategy_config(toml);
        assert!(result.is_err());
        match result.unwrap_err() {
            StrategyConfigError::UnknownOperator {
                strategy, operator, ..
            } => {
                assert_eq!(strategy, "bad_op");
                assert_eq!(operator, "equals");
            }
            other => panic!("expected UnknownOperator, got: {other:?}"),
        }
    }

    // -----------------------------------------------------------------------
    // Empty TOML is valid (zero strategies)
    // -----------------------------------------------------------------------

    #[test]
    fn test_empty_toml_valid() {
        let result = parse_strategy_config("");
        assert!(result.is_ok());
        let (defs, _) = result.unwrap();
        assert!(defs.is_empty());
    }

    #[test]
    fn test_indicator_params_override() {
        let toml = r#"
[indicator_params]
ema_fast_period = 8
rsi_period = 7
"#;
        let result = parse_strategy_config(toml);
        assert!(result.is_ok());
        let (_, params) = result.unwrap();
        assert_eq!(params.ema_fast_period, 8);
        assert_eq!(params.rsi_period, 7);
        // Defaults for unspecified
        assert_eq!(params.ema_slow_period, 26);
    }

    #[test]
    fn test_invalid_toml_syntax_error() {
        let result = parse_strategy_config("this is [[[ invalid");
        assert!(result.is_err());
        assert!(matches!(
            result.unwrap_err(),
            StrategyConfigError::TomlParse(_)
        ));
    }

    // -----------------------------------------------------------------------
    // Coverage gap-fill: all indicator fields, all operators, default values,
    // multiple conditions, full config round-trip, error display
    // -----------------------------------------------------------------------

    #[test]
    fn test_all_indicator_fields_accepted() {
        let fields = [
            "rsi",
            "macd_line",
            "macd_signal",
            "macd_histogram",
            "ema_fast",
            "ema_slow",
            "sma",
            "vwap",
            "bollinger_upper",
            "bollinger_middle",
            "bollinger_lower",
            "atr",
            "supertrend",
            "adx",
            "obv",
            "last_traded_price",
            "ltp",
            "volume",
            "day_high",
            "day_low",
        ];
        for field in fields {
            let result = parse_indicator_field(field, "test");
            assert!(result.is_ok(), "field '{}' must be accepted", field);
        }
    }

    #[test]
    fn test_all_comparison_operators_accepted() {
        let ops = [
            "gt",
            ">",
            "gte",
            ">=",
            "lt",
            "<",
            "lte",
            "<=",
            "cross_above",
            "cross_below",
        ];
        for op in ops {
            let result = parse_comparison_op(op, "test");
            assert!(result.is_ok(), "operator '{}' must be accepted", op);
        }
    }

    #[test]
    fn test_indicator_params_defaults_when_absent() {
        let toml = r#"
[[strategy]]
name = "no_params"
security_ids = [1]

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let (_, params) = parse_strategy_config(toml).unwrap();
        // When indicator_params section is absent, defaults are used
        assert_eq!(params.ema_fast_period, 12);
        assert_eq!(params.ema_slow_period, 26);
        assert_eq!(params.macd_signal_period, 9);
        assert_eq!(params.rsi_period, 14);
        assert_eq!(params.sma_period, 20);
        assert_eq!(params.atr_period, 14);
        assert_eq!(params.adx_period, 14);
        assert!((params.supertrend_multiplier - 3.0).abs() < f64::EPSILON);
        assert!((params.bollinger_multiplier - 2.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_strategyegy_defaults_when_optional_fields_absent() {
        let toml = r#"
[[strategy]]
name = "minimal"
security_ids = [1]

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let (defs, _) = parse_strategy_config(toml).unwrap();
        assert_eq!(defs.len(), 1);
        let def = &defs[0];
        // Verify defaults
        assert!((def.position_size_fraction - 0.1).abs() < f64::EPSILON);
        assert!((def.stop_loss_atr_multiplier - 2.0).abs() < f64::EPSILON);
        assert!((def.target_atr_multiplier - 3.0).abs() < f64::EPSILON);
        assert_eq!(def.confirmation_ticks, 0);
        assert!(!def.trailing_stop_enabled);
        assert!((def.trailing_stop_atr_multiplier - 1.5).abs() < f64::EPSILON);
    }

    #[test]
    fn test_multiple_entry_conditions_parsed() {
        let toml = r#"
[[strategy]]
name = "multi_cond"
security_ids = [1]

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0

[[strategy.entry_long]]
field = "macd_histogram"
operator = "gt"
threshold = 0.0

[[strategy.exit]]
field = "rsi"
operator = "gt"
threshold = 70.0
"#;
        let (defs, _) = parse_strategy_config(toml).unwrap();
        assert_eq!(defs[0].entry_long_conditions.len(), 2);
        assert_eq!(defs[0].exit_conditions.len(), 1);
    }

    #[test]
    fn test_error_display_unknown_field() {
        let err = StrategyConfigError::UnknownField {
            strategy: "test_strategy".to_string(),
            field: "bogus".to_string(),
        };
        let display = format!("{err}");
        assert!(display.contains("bogus"));
        assert!(display.contains("test_strategy"));
    }

    #[test]
    fn test_error_display_unknown_operator() {
        let err = StrategyConfigError::UnknownOperator {
            strategy: "test_strategy".to_string(),
            operator: "neq".to_string(),
        };
        let display = format!("{err}");
        assert!(display.contains("neq"));
        assert!(display.contains("test_strategy"));
    }

    #[test]
    fn test_error_display_validation() {
        let err = StrategyConfigError::Validation {
            strategy: "test_strategy".to_string(),
            message: "bad value".to_string(),
        };
        let display = format!("{err}");
        assert!(display.contains("bad value"));
        assert!(display.contains("test_strategy"));
    }

    #[test]
    fn test_load_strategy_config_file_nonexistent() {
        let result = load_strategy_config_file(std::path::Path::new("/nonexistent/file.toml"));
        assert!(result.is_err());
        assert!(matches!(result.unwrap_err(), StrategyConfigError::Io(_)));
    }

    #[test]
    fn test_negative_stop_loss_atr_rejected() {
        let toml = r#"
[[strategy]]
name = "neg_sl"
security_ids = [1]
stop_loss_atr_multiplier = -0.5
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let result = parse_strategy_config(toml);
        assert!(result.is_err());
    }

    #[test]
    fn test_zero_target_atr_rejected() {
        let toml = r#"
[[strategy]]
name = "zero_target"
security_ids = [1]
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 0.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let result = parse_strategy_config(toml);
        assert!(result.is_err());
    }

    #[test]
    fn test_full_indicator_params_override() {
        let toml = r#"
[indicator_params]
ema_fast_period = 5
ema_slow_period = 13
macd_signal_period = 5
rsi_period = 7
sma_period = 10
atr_period = 7
adx_period = 7
supertrend_multiplier = 2.0
bollinger_multiplier = 1.5
"#;
        let (_, params) = parse_strategy_config(toml).unwrap();
        assert_eq!(params.ema_fast_period, 5);
        assert_eq!(params.ema_slow_period, 13);
        assert_eq!(params.macd_signal_period, 5);
        assert_eq!(params.rsi_period, 7);
        assert_eq!(params.sma_period, 10);
        assert_eq!(params.atr_period, 7);
        assert_eq!(params.adx_period, 7);
        assert!((params.supertrend_multiplier - 2.0).abs() < f64::EPSILON);
        assert!((params.bollinger_multiplier - 1.5).abs() < f64::EPSILON);
    }

    #[test]
    fn test_ltp_alias_accepted() {
        // "ltp" should be accepted as alias for "last_traded_price"
        let toml = r#"
[[strategy]]
name = "ltp_alias"
security_ids = [1]

[[strategy.entry_long]]
field = "ltp"
operator = "gt"
threshold = 100.0
"#;
        let (defs, _) = parse_strategy_config(toml).unwrap();
        assert_eq!(
            defs[0].entry_long_conditions[0].field,
            IndicatorField::LastTradedPrice
        );
    }

    #[test]
    fn test_symbol_operators_accepted() {
        // ">" and ">=" should work as operators alongside "gt"/"gte"
        let toml = r#"
[[strategy]]
name = "symbol_ops"
security_ids = [1]

[[strategy.entry_long]]
field = "rsi"
operator = "<"
threshold = 30.0

[[strategy.exit]]
field = "rsi"
operator = ">="
threshold = 70.0
"#;
        let (defs, _) = parse_strategy_config(toml).unwrap();
        assert_eq!(defs[0].entry_long_conditions[0].operator, ComparisonOp::Lt);
        assert_eq!(defs[0].exit_conditions[0].operator, ComparisonOp::Gte);
    }

    // -----------------------------------------------------------------------
    // Coverage gap-fill: error propagation in entry_short/exit conditions,
    // serde default functions, NaN/infinity edge cases, TomlIndicatorParams
    // serde defaults, StrategyConfigError From impls
    // -----------------------------------------------------------------------

    #[test]
    fn test_unknown_field_in_entry_short_rejected() {
        // Tests error propagation at line 294 (entry_short_conditions collect)
        let toml = r#"
[[strategy]]
name = "bad_short"
security_ids = [1]
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0

[[strategy.entry_short]]
field = "nonexistent_short_field"
operator = "gt"
threshold = 70.0
"#;
        let result = parse_strategy_config(toml);
        assert!(
            result.is_err(),
            "unknown field in entry_short must be rejected"
        );
        match result.unwrap_err() {
            StrategyConfigError::UnknownField { strategy, field } => {
                assert_eq!(strategy, "bad_short");
                assert_eq!(field, "nonexistent_short_field");
            }
            other => panic!("expected UnknownField, got: {other:?}"),
        }
    }

    #[test]
    fn test_unknown_operator_in_entry_short_rejected() {
        // Tests error propagation at line 294 (entry_short_conditions collect)
        let toml = r#"
[[strategy]]
name = "bad_short_op"
security_ids = [1]
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0

[[strategy.entry_short]]
field = "rsi"
operator = "invalid_op"
threshold = 70.0
"#;
        let result = parse_strategy_config(toml);
        assert!(
            result.is_err(),
            "unknown operator in entry_short must be rejected"
        );
        match result.unwrap_err() {
            StrategyConfigError::UnknownOperator { strategy, operator } => {
                assert_eq!(strategy, "bad_short_op");
                assert_eq!(operator, "invalid_op");
            }
            other => panic!("expected UnknownOperator, got: {other:?}"),
        }
    }

    #[test]
    fn test_unknown_field_in_exit_rejected() {
        // Tests error propagation at line 300 (exit_conditions collect)
        let toml = r#"
[[strategy]]
name = "bad_exit"
security_ids = [1]
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0

[[strategy.exit]]
field = "bogus_exit_field"
operator = "gt"
threshold = 50.0
"#;
        let result = parse_strategy_config(toml);
        assert!(result.is_err(), "unknown field in exit must be rejected");
        match result.unwrap_err() {
            StrategyConfigError::UnknownField { strategy, field } => {
                assert_eq!(strategy, "bad_exit");
                assert_eq!(field, "bogus_exit_field");
            }
            other => panic!("expected UnknownField, got: {other:?}"),
        }
    }

    #[test]
    fn test_unknown_operator_in_exit_rejected() {
        // Tests error propagation at line 300 (exit_conditions collect)
        let toml = r#"
[[strategy]]
name = "bad_exit_op"
security_ids = [1]
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0

[[strategy.exit]]
field = "rsi"
operator = "bad_exit_op"
threshold = 50.0
"#;
        let result = parse_strategy_config(toml);
        assert!(result.is_err(), "unknown operator in exit must be rejected");
        match result.unwrap_err() {
            StrategyConfigError::UnknownOperator { strategy, operator } => {
                assert_eq!(strategy, "bad_exit_op");
                assert_eq!(operator, "bad_exit_op");
            }
            other => panic!("expected UnknownOperator, got: {other:?}"),
        }
    }

    #[test]
    fn test_toml_indicator_params_serde_defaults_triggered() {
        // When indicator_params section exists but no fields are specified,
        // all serde defaults should fire (default_ema_fast_period, etc.)
        let toml = r#"
[indicator_params]

[[strategy]]
name = "default_params"
security_ids = [1]

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let (_, params) = parse_strategy_config(toml).unwrap();
        // These values come from the serde default functions
        assert_eq!(params.ema_fast_period, 12); // default_ema_fast_period
        assert_eq!(params.ema_slow_period, 26); // default_ema_slow_period
        assert_eq!(params.macd_signal_period, 9); // default_macd_signal_period
        assert_eq!(params.rsi_period, 14); // default_rsi_period
        assert_eq!(params.sma_period, 20); // default_sma_period
        assert_eq!(params.atr_period, 14); // default_atr_period
        assert_eq!(params.adx_period, 14); // default_adx_period
        assert!((params.supertrend_multiplier - 3.0).abs() < f64::EPSILON);
        assert!((params.bollinger_multiplier - 2.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_toml_indicator_params_partial_override_triggers_remaining_defaults() {
        // Specify only some params; the rest use their serde defaults
        let toml = r#"
[indicator_params]
ema_fast_period = 8
bollinger_multiplier = 1.0

[[strategy]]
name = "partial_params"
security_ids = [1]

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let (_, params) = parse_strategy_config(toml).unwrap();
        assert_eq!(params.ema_fast_period, 8); // overridden
        assert!((params.bollinger_multiplier - 1.0).abs() < f64::EPSILON); // overridden
        // Remaining should be serde defaults
        assert_eq!(params.ema_slow_period, 26);
        assert_eq!(params.macd_signal_period, 9);
        assert_eq!(params.rsi_period, 14);
        assert_eq!(params.sma_period, 20);
        assert_eq!(params.atr_period, 14);
        assert_eq!(params.adx_period, 14);
        assert!((params.supertrend_multiplier - 3.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_strategy_defaults_position_size_and_trailing_stop() {
        // When position_size_fraction and trailing_stop fields are absent,
        // serde defaults should fire (default_position_size, default_trailing_stop_atr)
        let toml = r#"
[[strategy]]
name = "all_defaults"
security_ids = [1]

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let (defs, _) = parse_strategy_config(toml).unwrap();
        let def = &defs[0];
        assert!((def.position_size_fraction - 0.1).abs() < f64::EPSILON); // default_position_size
        assert!((def.stop_loss_atr_multiplier - 2.0).abs() < f64::EPSILON); // default_stop_loss_atr
        assert!((def.target_atr_multiplier - 3.0).abs() < f64::EPSILON); // default_target_atr
        assert!((def.trailing_stop_atr_multiplier - 1.5).abs() < f64::EPSILON); // default_trailing_stop_atr
    }

    #[test]
    fn test_position_size_fraction_nan_rejected() {
        // NaN should not be in the range [0.0, 1.0]
        // We test via the convert_strategy validation indirectly:
        // TOML doesn't support NaN, so we test parse_strategy_config with extreme float
        let toml = r#"
[[strategy]]
name = "nan_test"
security_ids = [1]
position_size_fraction = 1.0e308
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        // 1.0e308 is outside [0.0, 1.0], so should be rejected
        let result = parse_strategy_config(toml);
        assert!(
            result.is_err(),
            "position_size_fraction=1e308 must be rejected"
        );
    }

    #[test]
    fn test_error_display_toml_parse() {
        let err = parse_strategy_config("this is not valid TOML [[[").unwrap_err();
        let display = format!("{err}");
        assert!(display.contains("TOML parse error"));
    }

    #[test]
    fn test_error_display_io() {
        let err =
            load_strategy_config_file(std::path::Path::new("/nonexistent/path.toml")).unwrap_err();
        let display = format!("{err}");
        assert!(display.contains("failed to read config file"));
    }

    #[test]
    fn test_multiple_strategies_second_fails_validation() {
        // First strategy is valid, second has bad field — should fail
        let toml = r#"
[[strategy]]
name = "good_one"
security_ids = [1]

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0

[[strategy]]
name = "bad_one"
security_ids = [2]

[[strategy.entry_long]]
field = "fake_indicator"
operator = "gt"
threshold = 50.0
"#;
        let result = parse_strategy_config(toml);
        assert!(result.is_err());
        match result.unwrap_err() {
            StrategyConfigError::UnknownField { strategy, field } => {
                assert_eq!(strategy, "bad_one");
                assert_eq!(field, "fake_indicator");
            }
            other => panic!("expected UnknownField, got: {other:?}"),
        }
    }

    #[test]
    fn test_cross_above_and_cross_below_in_conditions() {
        let toml = r#"
[[strategy]]
name = "cross_test"
security_ids = [1]

[[strategy.entry_long]]
field = "ema_fast"
operator = "cross_above"
threshold = 0.0

[[strategy.entry_short]]
field = "ema_fast"
operator = "cross_below"
threshold = 0.0
"#;
        let (defs, _) = parse_strategy_config(toml).unwrap();
        assert_eq!(
            defs[0].entry_long_conditions[0].operator,
            ComparisonOp::CrossAbove
        );
        assert_eq!(
            defs[0].entry_short_conditions[0].operator,
            ComparisonOp::CrossBelow
        );
    }

    // -----------------------------------------------------------------------
    // Additional validation boundary tests for coverage
    // -----------------------------------------------------------------------

    #[test]
    fn test_position_size_fraction_just_above_zero_accepted() {
        let toml = r#"
[[strategy]]
name = "tiny_size"
security_ids = [1]
position_size_fraction = 0.001
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let result = parse_strategy_config(toml);
        assert!(
            result.is_ok(),
            "position_size_fraction=0.001 must be accepted"
        );
        let (defs, _) = result.unwrap();
        assert!((defs[0].position_size_fraction - 0.001).abs() < f64::EPSILON);
    }

    #[test]
    fn test_position_size_fraction_just_below_one_accepted() {
        let toml = r#"
[[strategy]]
name = "almost_full"
security_ids = [1]
position_size_fraction = 0.999
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let result = parse_strategy_config(toml);
        assert!(
            result.is_ok(),
            "position_size_fraction=0.999 must be accepted"
        );
    }

    #[test]
    fn test_empty_security_ids_accepted() {
        // Empty security_ids is structurally valid (strategy just won't match any instruments)
        let toml = r#"
[[strategy]]
name = "no_sids"
security_ids = []
stop_loss_atr_multiplier = 2.0
target_atr_multiplier = 3.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let result = parse_strategy_config(toml);
        assert!(result.is_ok(), "empty security_ids must be accepted");
        let (defs, _) = result.unwrap();
        assert!(defs[0].security_ids.is_empty());
    }

    #[test]
    fn test_entry_long_only_with_exit_conditions_accepted() {
        let toml = r#"
[[strategy]]
name = "long_with_exit"
security_ids = [1]

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0

[[strategy.exit]]
field = "rsi"
operator = "gt"
threshold = 50.0

[[strategy.exit]]
field = "atr"
operator = "lt"
threshold = 1.0
"#;
        let result = parse_strategy_config(toml);
        assert!(result.is_ok());
        let (defs, _) = result.unwrap();
        assert_eq!(defs[0].exit_conditions.len(), 2);
        assert!(defs[0].entry_short_conditions.is_empty());
    }

    #[test]
    fn test_trailing_stop_fields_in_strategy() {
        let toml = r#"
[[strategy]]
name = "trail_enabled"
security_ids = [1]
trailing_stop_enabled = true
trailing_stop_atr_multiplier = 2.5
confirmation_ticks = 5

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;
        let result = parse_strategy_config(toml);
        assert!(result.is_ok());
        let (defs, _) = result.unwrap();
        assert!(defs[0].trailing_stop_enabled);
        assert!((defs[0].trailing_stop_atr_multiplier - 2.5).abs() < f64::EPSILON);
        assert_eq!(defs[0].confirmation_ticks, 5);
    }

    #[test]
    fn test_lte_operator_in_exit() {
        let toml = r#"
[[strategy]]
name = "lte_exit"
security_ids = [1]

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0

[[strategy.exit]]
field = "rsi"
operator = "<="
threshold = 20.0
"#;
        let (defs, _) = parse_strategy_config(toml).unwrap();
        assert_eq!(defs[0].exit_conditions[0].operator, ComparisonOp::Lte);
    }
}
