//! Common error types used across the tickvault workspace.
//!
//! Each crate may define its own specific error types using `thiserror`,
//! but cross-cutting errors live here.

/// Top-level application error.
#[derive(Debug, thiserror::Error)]
pub enum ApplicationError {
    /// Configuration loading or parsing failed.
    #[error("configuration error: {0}")]
    Configuration(String),

    /// Secret retrieval from SSM Parameter Store failed.
    #[error("secret retrieval failed for path '{path}': {source}")]
    SecretRetrieval { path: String, source: anyhow::Error },

    /// Market hour validation failed (attempting to trade outside hours).
    #[error("market hour violation: {0}")]
    MarketHourViolation(String),

    /// Infrastructure service unavailable.
    #[error("infrastructure unavailable: {service} at {endpoint}")]
    InfrastructureUnavailable { service: String, endpoint: String },

    /// Instrument CSV download failed after all retries and fallbacks.
    #[error("instrument CSV download failed: {reason}")]
    InstrumentDownloadFailed { reason: String },

    /// Instrument CSV parsing failed.
    #[error("instrument CSV parse error at row {row}: {reason}")]
    InstrumentParseFailed { row: usize, reason: String },

    /// F&O universe build validation failed.
    #[error("F&O universe validation failed: {check}")]
    UniverseValidationFailed { check: String },

    /// Required CSV column not found in header.
    #[error("required CSV column '{column}' not found in header")]
    CsvColumnMissing { column: String },

    /// QuestDB write operation failed.
    #[error("QuestDB write failed for table '{table}': {source}")]
    QuestDbWriteFailed {
        table: String,
        source: anyhow::Error,
    },

    /// TOTP code generation failed.
    #[error("TOTP generation failed: {reason}")]
    TotpGenerationFailed { reason: String },

    /// Dhan authentication API call failed.
    #[error("Dhan authentication failed: {reason}")]
    AuthenticationFailed { reason: String },

    /// Token renewal failed after all retries.
    #[error("token renewal failed after {attempts} attempts: {reason}")]
    TokenRenewalFailed { attempts: u32, reason: String },

    /// Authentication circuit breaker tripped — too many consecutive failures.
    #[error("auth circuit breaker tripped after {failures} consecutive failures")]
    AuthCircuitBreakerTripped { failures: u32 },

    /// Telegram notification send failed (non-fatal — logged as warn only).
    #[error("notification send failed: {reason}")]
    NotificationSendFailed { reason: String },

    /// Public IP verification failed — static IP mismatch or detection failure.
    #[error("IP verification failed: {reason}")]
    IpVerificationFailed { reason: String },
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_configuration_error_display() {
        let err = ApplicationError::Configuration("missing field".to_string());
        assert_eq!(err.to_string(), "configuration error: missing field");
    }

    #[test]
    fn test_secret_retrieval_error_display() {
        let err = ApplicationError::SecretRetrieval {
            path: "/tickvault/dev/dhan/client-id".to_string(),
            source: anyhow::anyhow!("not found"),
        };
        let msg = err.to_string();
        assert!(msg.contains("/tickvault/dev/dhan/client-id"));
        assert!(msg.contains("not found"));
    }

    #[test]
    fn test_market_hour_violation_display() {
        let err = ApplicationError::MarketHourViolation("order after 15:30".to_string());
        assert!(err.to_string().contains("15:30"));
    }

    #[test]
    fn test_instrument_download_failed_display() {
        let err = ApplicationError::InstrumentDownloadFailed {
            reason: "all sources failed".to_string(),
        };
        assert!(err.to_string().contains("all sources failed"));
    }

    #[test]
    fn test_instrument_parse_failed_display() {
        let err = ApplicationError::InstrumentParseFailed {
            row: 42,
            reason: "missing column".to_string(),
        };
        let msg = err.to_string();
        assert!(msg.contains("42"));
        assert!(msg.contains("missing column"));
    }

    #[test]
    fn test_universe_validation_failed_display() {
        let err = ApplicationError::UniverseValidationFailed {
            check: "NIFTY missing".to_string(),
        };
        assert!(err.to_string().contains("NIFTY missing"));
    }

    #[test]
    fn test_csv_column_missing_display() {
        let err = ApplicationError::CsvColumnMissing {
            column: "SECURITY_ID".to_string(),
        };
        assert!(err.to_string().contains("SECURITY_ID"));
    }

    #[test]
    fn test_questdb_write_failed_display() {
        let err = ApplicationError::QuestDbWriteFailed {
            table: "fno_underlyings".to_string(),
            source: anyhow::anyhow!("connection refused"),
        };
        let msg = err.to_string();
        assert!(msg.contains("fno_underlyings"));
        assert!(msg.contains("connection refused"));
    }

    #[test]
    fn test_totp_generation_failed_display() {
        let err = ApplicationError::TotpGenerationFailed {
            reason: "invalid base32".to_string(),
        };
        assert!(err.to_string().contains("invalid base32"));
    }

    #[test]
    fn test_authentication_failed_display() {
        let err = ApplicationError::AuthenticationFailed {
            reason: "HTTP 401".to_string(),
        };
        assert!(err.to_string().contains("HTTP 401"));
    }

    #[test]
    fn test_token_renewal_failed_display() {
        let err = ApplicationError::TokenRenewalFailed {
            attempts: 3,
            reason: "timeout".to_string(),
        };
        let msg = err.to_string();
        assert!(msg.contains("3"));
        assert!(msg.contains("timeout"));
    }

    #[test]
    fn test_auth_circuit_breaker_display() {
        let err = ApplicationError::AuthCircuitBreakerTripped { failures: 5 };
        assert!(err.to_string().contains("5"));
    }

    #[test]
    fn test_ip_verification_failed_display() {
        let err = ApplicationError::IpVerificationFailed {
            reason: "IP mismatch".to_string(),
        };
        let msg = err.to_string();
        assert!(msg.contains("IP verification failed"));
        assert!(msg.contains("IP mismatch"));
    }

    #[test]
    fn test_application_error_is_send_and_sync() {
        fn assert_send_sync<T: Send + Sync>() {}
        // This will fail at compile time if ApplicationError is not Send + Sync
        assert_send_sync::<ApplicationError>();
    }

    // =====================================================================
    // Additional coverage: Display impls for remaining variants
    // =====================================================================

    #[test]
    fn test_infrastructure_unavailable_display() {
        let err = ApplicationError::InfrastructureUnavailable {
            service: "QuestDB".to_string(),
            endpoint: "tv-questdb:9009".to_string(),
        };
        let msg = err.to_string();
        assert!(msg.contains("QuestDB"));
        assert!(msg.contains("tv-questdb:9009"));
        assert!(msg.contains("infrastructure unavailable"));
    }

    #[test]
    fn test_notification_send_failed_display() {
        let err = ApplicationError::NotificationSendFailed {
            reason: "Telegram API timeout".to_string(),
        };
        let msg = err.to_string();
        assert!(msg.contains("notification send failed"));
        assert!(msg.contains("Telegram API timeout"));
    }

    // =====================================================================
    // Additional coverage: all error variants produce non-empty Display
    // =====================================================================

    #[test]
    fn test_all_error_variants_have_nonempty_display() {
        let errors: Vec<ApplicationError> = vec![
            ApplicationError::Configuration("cfg".to_string()),
            ApplicationError::SecretRetrieval {
                path: "/p".to_string(),
                source: anyhow::anyhow!("err"),
            },
            ApplicationError::MarketHourViolation("violation".to_string()),
            ApplicationError::InfrastructureUnavailable {
                service: "svc".to_string(),
                endpoint: "ep".to_string(),
            },
            ApplicationError::InstrumentDownloadFailed {
                reason: "fail".to_string(),
            },
            ApplicationError::InstrumentParseFailed {
                row: 1,
                reason: "bad".to_string(),
            },
            ApplicationError::UniverseValidationFailed {
                check: "chk".to_string(),
            },
            ApplicationError::CsvColumnMissing {
                column: "col".to_string(),
            },
            ApplicationError::QuestDbWriteFailed {
                table: "tbl".to_string(),
                source: anyhow::anyhow!("err"),
            },
            ApplicationError::TotpGenerationFailed {
                reason: "totp".to_string(),
            },
            ApplicationError::AuthenticationFailed {
                reason: "auth".to_string(),
            },
            ApplicationError::TokenRenewalFailed {
                attempts: 1,
                reason: "renew".to_string(),
            },
            ApplicationError::AuthCircuitBreakerTripped { failures: 3 },
            ApplicationError::NotificationSendFailed {
                reason: "notify".to_string(),
            },
            ApplicationError::IpVerificationFailed {
                reason: "ip".to_string(),
            },
        ];
        for err in &errors {
            let msg = err.to_string();
            assert!(!msg.is_empty(), "empty Display for {err:?}");
        }
    }

    // =====================================================================
    // Additional coverage: error source chains
    // =====================================================================

    #[test]
    fn test_secret_retrieval_error_source_chain() {
        use std::error::Error;
        let err = ApplicationError::SecretRetrieval {
            path: "/tickvault/dev/key".to_string(),
            source: anyhow::anyhow!("SSM unreachable"),
        };
        // thiserror wires up the source automatically
        assert!(err.source().is_some());
    }

    #[test]
    fn test_questdb_write_failed_error_source_chain() {
        use std::error::Error;
        let err = ApplicationError::QuestDbWriteFailed {
            table: "ticks".to_string(),
            source: anyhow::anyhow!("connection reset"),
        };
        assert!(err.source().is_some());
    }

    #[test]
    fn test_configuration_error_has_no_source() {
        use std::error::Error;
        let err = ApplicationError::Configuration("bad config".to_string());
        assert!(err.source().is_none());
    }

    #[test]
    fn test_market_hour_violation_has_no_source() {
        use std::error::Error;
        let err = ApplicationError::MarketHourViolation("outside hours".to_string());
        assert!(err.source().is_none());
    }

    #[test]
    fn test_infrastructure_unavailable_has_no_source() {
        use std::error::Error;
        let err = ApplicationError::InfrastructureUnavailable {
            service: "Valkey".to_string(),
            endpoint: "tv-valkey:6379".to_string(),
        };
        assert!(err.source().is_none());
    }

    #[test]
    fn test_instrument_download_failed_has_no_source() {
        use std::error::Error;
        let err = ApplicationError::InstrumentDownloadFailed {
            reason: "timeout".to_string(),
        };
        assert!(err.source().is_none());
    }

    #[test]
    fn test_instrument_parse_failed_has_no_source() {
        use std::error::Error;
        let err = ApplicationError::InstrumentParseFailed {
            row: 100,
            reason: "bad data".to_string(),
        };
        assert!(err.source().is_none());
    }

    #[test]
    fn test_universe_validation_failed_has_no_source() {
        use std::error::Error;
        let err = ApplicationError::UniverseValidationFailed {
            check: "derivative count".to_string(),
        };
        assert!(err.source().is_none());
    }

    #[test]
    fn test_csv_column_missing_has_no_source() {
        use std::error::Error;
        let err = ApplicationError::CsvColumnMissing {
            column: "EXCH_ID".to_string(),
        };
        assert!(err.source().is_none());
    }

    #[test]
    fn test_totp_generation_failed_has_no_source() {
        use std::error::Error;
        let err = ApplicationError::TotpGenerationFailed {
            reason: "clock skew".to_string(),
        };
        assert!(err.source().is_none());
    }

    #[test]
    fn test_authentication_failed_has_no_source() {
        use std::error::Error;
        let err = ApplicationError::AuthenticationFailed {
            reason: "invalid credentials".to_string(),
        };
        assert!(err.source().is_none());
    }

    #[test]
    fn test_token_renewal_failed_has_no_source() {
        use std::error::Error;
        let err = ApplicationError::TokenRenewalFailed {
            attempts: 5,
            reason: "all retries exhausted".to_string(),
        };
        assert!(err.source().is_none());
    }

    #[test]
    fn test_auth_circuit_breaker_tripped_has_no_source() {
        use std::error::Error;
        let err = ApplicationError::AuthCircuitBreakerTripped { failures: 10 };
        assert!(err.source().is_none());
    }

    #[test]
    fn test_notification_send_failed_has_no_source() {
        use std::error::Error;
        let err = ApplicationError::NotificationSendFailed {
            reason: "rate limited".to_string(),
        };
        assert!(err.source().is_none());
    }

    #[test]
    fn test_ip_verification_failed_has_no_source() {
        use std::error::Error;
        let err = ApplicationError::IpVerificationFailed {
            reason: "no public IP".to_string(),
        };
        assert!(err.source().is_none());
    }

    // =====================================================================
    // Additional coverage: Debug impl produces output
    // =====================================================================

    #[test]
    fn test_application_error_debug_impl() {
        let err = ApplicationError::Configuration("test".to_string());
        let debug = format!("{err:?}");
        assert!(debug.contains("Configuration"));
        assert!(debug.contains("test"));
    }

    #[test]
    fn test_application_error_debug_structured_variant() {
        let err = ApplicationError::TokenRenewalFailed {
            attempts: 3,
            reason: "timeout".to_string(),
        };
        let debug = format!("{err:?}");
        assert!(debug.contains("TokenRenewalFailed"));
        assert!(debug.contains("3"));
        assert!(debug.contains("timeout"));
    }

    // =====================================================================
    // Additional coverage: Display format string correctness
    // =====================================================================

    #[test]
    fn test_infrastructure_unavailable_display_format() {
        let err = ApplicationError::InfrastructureUnavailable {
            service: "Prometheus".to_string(),
            endpoint: "tv-prometheus:9090".to_string(),
        };
        assert_eq!(
            err.to_string(),
            "infrastructure unavailable: Prometheus at tv-prometheus:9090"
        );
    }

    #[test]
    fn test_instrument_download_failed_display_format() {
        let err = ApplicationError::InstrumentDownloadFailed {
            reason: "HTTP 503".to_string(),
        };
        assert_eq!(err.to_string(), "instrument CSV download failed: HTTP 503");
    }

    #[test]
    fn test_instrument_parse_failed_display_format() {
        let err = ApplicationError::InstrumentParseFailed {
            row: 999,
            reason: "invalid lot size".to_string(),
        };
        assert_eq!(
            err.to_string(),
            "instrument CSV parse error at row 999: invalid lot size"
        );
    }

    #[test]
    fn test_universe_validation_failed_display_format() {
        let err = ApplicationError::UniverseValidationFailed {
            check: "zero derivatives".to_string(),
        };
        assert_eq!(
            err.to_string(),
            "F&O universe validation failed: zero derivatives"
        );
    }

    #[test]
    fn test_csv_column_missing_display_format() {
        let err = ApplicationError::CsvColumnMissing {
            column: "SEM_EXPIRY_DATE".to_string(),
        };
        assert_eq!(
            err.to_string(),
            "required CSV column 'SEM_EXPIRY_DATE' not found in header"
        );
    }

    #[test]
    fn test_totp_generation_failed_display_format() {
        let err = ApplicationError::TotpGenerationFailed {
            reason: "secret decode error".to_string(),
        };
        assert_eq!(
            err.to_string(),
            "TOTP generation failed: secret decode error"
        );
    }

    #[test]
    fn test_auth_circuit_breaker_tripped_display_format() {
        let err = ApplicationError::AuthCircuitBreakerTripped { failures: 7 };
        assert_eq!(
            err.to_string(),
            "auth circuit breaker tripped after 7 consecutive failures"
        );
    }

    #[test]
    fn test_token_renewal_failed_display_format() {
        let err = ApplicationError::TokenRenewalFailed {
            attempts: 5,
            reason: "network unreachable".to_string(),
        };
        assert_eq!(
            err.to_string(),
            "token renewal failed after 5 attempts: network unreachable"
        );
    }

    #[test]
    fn test_notification_send_failed_display_format() {
        let err = ApplicationError::NotificationSendFailed {
            reason: "HTTP 429".to_string(),
        };
        assert_eq!(err.to_string(), "notification send failed: HTTP 429");
    }

    #[test]
    fn test_ip_verification_failed_display_format() {
        let err = ApplicationError::IpVerificationFailed {
            reason: "static IP changed".to_string(),
        };
        assert_eq!(err.to_string(), "IP verification failed: static IP changed");
    }
}
