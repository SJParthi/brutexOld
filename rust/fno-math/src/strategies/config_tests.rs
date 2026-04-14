//! Tests for TOML strategy configuration parsing and validation.

use crate::strategies::config::{StrategyConfigError, parse_strategy_config};
use crate::strategies::types::{ComparisonOp, IndicatorField};

// -----------------------------------------------------------------------
// Full TOML Parsing
// -----------------------------------------------------------------------

#[test]
fn parse_full_config_with_two_strategies() {
    let toml = r#"
[indicator_params]
ema_fast_period = 10
ema_slow_period = 21
rsi_period = 14

[[strategy]]
name = "rsi_mean_reversion"
security_ids = [13, 25, 27]
position_size_fraction = 0.05
stop_loss_atr_multiplier = 1.5
target_atr_multiplier = 2.5
confirmation_ticks = 3
trailing_stop_enabled = true
trailing_stop_atr_multiplier = 1.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0

[[strategy.entry_long]]
field = "adx"
operator = "gt"
threshold = 25.0

[[strategy.entry_short]]
field = "rsi"
operator = "gt"
threshold = 70.0

[[strategy.exit]]
field = "rsi"
operator = "gt"
threshold = 50.0

[[strategy]]
name = "macd_crossover"
security_ids = [42]
position_size_fraction = 0.1

[[strategy.entry_long]]
field = "macd_histogram"
operator = "gt"
threshold = 0.0

[[strategy.entry_short]]
field = "macd_histogram"
operator = "lt"
threshold = 0.0
"#;

    let (strategies, params) = parse_strategy_config(toml).unwrap();

    // Check indicator params
    assert_eq!(params.ema_fast_period, 10);
    assert_eq!(params.ema_slow_period, 21);
    assert_eq!(params.rsi_period, 14);
    // Defaults for unspecified
    assert_eq!(params.sma_period, 20);
    assert!((params.supertrend_multiplier - 3.0).abs() < f64::EPSILON);

    // Check strategies
    assert_eq!(strategies.len(), 2);

    // First strategy
    let s1 = &strategies[0];
    assert_eq!(s1.name, "rsi_mean_reversion");
    assert_eq!(s1.security_ids, vec![13, 25, 27]);
    assert!((s1.position_size_fraction - 0.05).abs() < f64::EPSILON);
    assert!((s1.stop_loss_atr_multiplier - 1.5).abs() < f64::EPSILON);
    assert!((s1.target_atr_multiplier - 2.5).abs() < f64::EPSILON);
    assert_eq!(s1.confirmation_ticks, 3);
    assert!(s1.trailing_stop_enabled);
    assert!((s1.trailing_stop_atr_multiplier - 1.0).abs() < f64::EPSILON);
    assert_eq!(s1.entry_long_conditions.len(), 2);
    assert_eq!(s1.entry_short_conditions.len(), 1);
    assert_eq!(s1.exit_conditions.len(), 1);

    // Verify conditions
    assert_eq!(s1.entry_long_conditions[0].field, IndicatorField::Rsi);
    assert_eq!(s1.entry_long_conditions[0].operator, ComparisonOp::Lt);
    assert!((s1.entry_long_conditions[0].threshold - 30.0).abs() < f64::EPSILON);

    assert_eq!(s1.entry_long_conditions[1].field, IndicatorField::Adx);
    assert_eq!(s1.entry_long_conditions[1].operator, ComparisonOp::Gt);

    // Second strategy
    let s2 = &strategies[1];
    assert_eq!(s2.name, "macd_crossover");
    assert_eq!(s2.entry_long_conditions.len(), 1);
    assert_eq!(
        s2.entry_long_conditions[0].field,
        IndicatorField::MacdHistogram
    );
}

// -----------------------------------------------------------------------
// Minimal Config
// -----------------------------------------------------------------------

#[test]
fn parse_minimal_config() {
    let toml = r#"
[[strategy]]
name = "simple"
security_ids = [1]

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 25.0
"#;

    let (strategies, params) = parse_strategy_config(toml).unwrap();
    assert_eq!(strategies.len(), 1);
    assert_eq!(strategies[0].name, "simple");

    // Check defaults
    assert!((strategies[0].position_size_fraction - 0.1).abs() < f64::EPSILON);
    assert!((strategies[0].stop_loss_atr_multiplier - 2.0).abs() < f64::EPSILON);
    assert!((strategies[0].target_atr_multiplier - 3.0).abs() < f64::EPSILON);
    assert!(!strategies[0].trailing_stop_enabled);

    // Indicator params should be defaults
    assert_eq!(params.ema_fast_period, 12);
    assert_eq!(params.ema_slow_period, 26);
}

// -----------------------------------------------------------------------
// Empty Config
// -----------------------------------------------------------------------

#[test]
fn parse_empty_config_no_strategies() {
    let toml = "";
    let (strategies, _) = parse_strategy_config(toml).unwrap();
    assert!(strategies.is_empty());
}

// -----------------------------------------------------------------------
// All Indicator Fields
// -----------------------------------------------------------------------

#[test]
fn all_indicator_fields_parse() {
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

    for field in &fields {
        let toml = format!(
            r#"
[[strategy]]
name = "test_{field}"

[[strategy.entry_long]]
field = "{field}"
operator = "gt"
threshold = 0.0
"#
        );

        let result = parse_strategy_config(&toml);
        assert!(
            result.is_ok(),
            "Failed to parse field '{field}': {result:?}"
        );
    }
}

// -----------------------------------------------------------------------
// All Comparison Operators
// -----------------------------------------------------------------------

#[test]
fn all_operators_parse() {
    let operators = [
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

    for op in &operators {
        let toml = format!(
            r#"
[[strategy]]
name = "test_{op}"

[[strategy.entry_long]]
field = "rsi"
operator = "{op}"
threshold = 50.0
"#
        );

        let result = parse_strategy_config(&toml);
        assert!(
            result.is_ok(),
            "Failed to parse operator '{op}': {result:?}"
        );
    }
}

// -----------------------------------------------------------------------
// Validation Errors
// -----------------------------------------------------------------------

#[test]
fn unknown_field_returns_error() {
    let toml = r#"
[[strategy]]
name = "bad"

[[strategy.entry_long]]
field = "nonexistent_indicator"
operator = "gt"
threshold = 50.0
"#;

    let result = parse_strategy_config(toml);
    assert!(result.is_err());
    let err = result.unwrap_err();
    assert!(
        matches!(err, StrategyConfigError::UnknownField { .. }),
        "Expected UnknownField error, got {err:?}"
    );
}

#[test]
fn unknown_operator_returns_error() {
    let toml = r#"
[[strategy]]
name = "bad"

[[strategy.entry_long]]
field = "rsi"
operator = "equals"
threshold = 50.0
"#;

    let result = parse_strategy_config(toml);
    assert!(result.is_err());
    let err = result.unwrap_err();
    assert!(
        matches!(err, StrategyConfigError::UnknownOperator { .. }),
        "Expected UnknownOperator error, got {err:?}"
    );
}

#[test]
fn invalid_position_size_returns_error() {
    let toml = r#"
[[strategy]]
name = "bad"
position_size_fraction = 1.5

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;

    let result = parse_strategy_config(toml);
    assert!(result.is_err());
    let err = result.unwrap_err();
    assert!(
        matches!(err, StrategyConfigError::Validation { .. }),
        "Expected Validation error, got {err:?}"
    );
}

#[test]
fn negative_stop_loss_multiplier_returns_error() {
    let toml = r#"
[[strategy]]
name = "bad"
stop_loss_atr_multiplier = -1.0

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 30.0
"#;

    let result = parse_strategy_config(toml);
    assert!(result.is_err());
}

#[test]
fn no_entry_conditions_returns_error() {
    let toml = r#"
[[strategy]]
name = "bad"
"#;

    let result = parse_strategy_config(toml);
    assert!(result.is_err());
    let err = result.unwrap_err();
    assert!(
        matches!(err, StrategyConfigError::Validation { .. }),
        "Expected Validation error for missing entry conditions, got {err:?}"
    );
}

// -----------------------------------------------------------------------
// TOML Syntax Errors
// -----------------------------------------------------------------------

#[test]
fn malformed_toml_returns_parse_error() {
    let toml = "this is not valid toml [[[";
    let result = parse_strategy_config(toml);
    assert!(result.is_err());
    assert!(matches!(
        result.unwrap_err(),
        StrategyConfigError::TomlParse(_)
    ));
}

// -----------------------------------------------------------------------
// Operator Symbol Aliases
// -----------------------------------------------------------------------

#[test]
fn symbol_operators_map_correctly() {
    let toml = r#"
[[strategy]]
name = "symbols"

[[strategy.entry_long]]
field = "rsi"
operator = "<"
threshold = 30.0

[[strategy.entry_short]]
field = "rsi"
operator = ">"
threshold = 70.0

[[strategy.exit]]
field = "rsi"
operator = ">="
threshold = 50.0
"#;

    let (strategies, _) = parse_strategy_config(toml).unwrap();
    let s = &strategies[0];
    assert_eq!(s.entry_long_conditions[0].operator, ComparisonOp::Lt);
    assert_eq!(s.entry_short_conditions[0].operator, ComparisonOp::Gt);
    assert_eq!(s.exit_conditions[0].operator, ComparisonOp::Gte);
}

// -----------------------------------------------------------------------
// Hot Reload File-Based Test
// -----------------------------------------------------------------------

#[test]
fn load_from_temp_file() {
    use crate::strategies::config::load_strategy_config_file;

    let toml_content = r#"
[[strategy]]
name = "file_test"
security_ids = [99]

[[strategy.entry_long]]
field = "rsi"
operator = "lt"
threshold = 25.0
"#;

    let dir = std::env::temp_dir();
    let path = dir.join("tv_test_strategy_config.toml");
    std::fs::write(&path, toml_content).unwrap();

    let result = load_strategy_config_file(&path);
    // Clean up before asserting
    let _ = std::fs::remove_file(&path);

    let (strategies, _) = result.unwrap();
    assert_eq!(strategies.len(), 1);
    assert_eq!(strategies[0].name, "file_test");
    assert_eq!(strategies[0].security_ids, vec![99]);
}

// -----------------------------------------------------------------------
// Hot Reload Watcher Test
// -----------------------------------------------------------------------

#[test]
fn hot_reloader_loads_initial_config() {
    use crate::strategies::hot_reload::StrategyHotReloader;

    let toml_content = r#"
[[strategy]]
name = "hot_test"
security_ids = [1, 2, 3]

[[strategy.entry_long]]
field = "macd_histogram"
operator = "gt"
threshold = 0.0
"#;

    let dir = std::env::temp_dir();
    let path = dir.join("tv_test_hot_reload.toml");
    std::fs::write(&path, toml_content).unwrap();

    let result = StrategyHotReloader::new(&path);
    // Clean up before asserting
    let _ = std::fs::remove_file(&path);

    let (reloader, strategies, params) = result.unwrap();
    assert_eq!(strategies.len(), 1);
    assert_eq!(strategies[0].name, "hot_test");
    assert_eq!(strategies[0].security_ids, vec![1, 2, 3]);
    assert_eq!(params.ema_fast_period, 12); // default

    // Drain any pending reload events from file watcher startup
    while reloader.try_recv().is_some() {}
}
