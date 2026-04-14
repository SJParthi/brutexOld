//! Shared exchange segment code → string mapping.
//!
//! I-P3-01: Deduplicated from tick_persistence.rs and candle_persistence.rs.
//! Single source of truth for the binary segment code → human-readable string mapping.

use super::constants::{
    EXCHANGE_SEGMENT_BSE_CURRENCY, EXCHANGE_SEGMENT_BSE_EQ, EXCHANGE_SEGMENT_BSE_FNO,
    EXCHANGE_SEGMENT_IDX_I, EXCHANGE_SEGMENT_MCX_COMM, EXCHANGE_SEGMENT_NSE_CURRENCY,
    EXCHANGE_SEGMENT_NSE_EQ, EXCHANGE_SEGMENT_NSE_FNO,
};

/// Maps the binary exchange_segment_code to a human-readable symbol name.
///
/// Uses the same mapping as the Dhan API (Python SDK ref).
/// Note: code 6 is unused/skipped in Dhan's protocol.
pub fn segment_code_to_str(code: u8) -> &'static str {
    match code {
        EXCHANGE_SEGMENT_IDX_I => "IDX_I",
        EXCHANGE_SEGMENT_NSE_EQ => "NSE_EQ",
        EXCHANGE_SEGMENT_NSE_FNO => "NSE_FNO",
        EXCHANGE_SEGMENT_NSE_CURRENCY => "NSE_CURRENCY",
        EXCHANGE_SEGMENT_BSE_EQ => "BSE_EQ",
        EXCHANGE_SEGMENT_MCX_COMM => "MCX_COMM",
        EXCHANGE_SEGMENT_BSE_CURRENCY => "BSE_CURRENCY",
        EXCHANGE_SEGMENT_BSE_FNO => "BSE_FNO",
        _ => "UNKNOWN",
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_segment_code_to_str_known_codes() {
        assert_eq!(segment_code_to_str(EXCHANGE_SEGMENT_IDX_I), "IDX_I");
        assert_eq!(segment_code_to_str(EXCHANGE_SEGMENT_NSE_EQ), "NSE_EQ");
        assert_eq!(segment_code_to_str(EXCHANGE_SEGMENT_NSE_FNO), "NSE_FNO");
        assert_eq!(
            segment_code_to_str(EXCHANGE_SEGMENT_NSE_CURRENCY),
            "NSE_CURRENCY"
        );
        assert_eq!(segment_code_to_str(EXCHANGE_SEGMENT_BSE_EQ), "BSE_EQ");
        assert_eq!(segment_code_to_str(EXCHANGE_SEGMENT_MCX_COMM), "MCX_COMM");
        assert_eq!(
            segment_code_to_str(EXCHANGE_SEGMENT_BSE_CURRENCY),
            "BSE_CURRENCY"
        );
        assert_eq!(segment_code_to_str(EXCHANGE_SEGMENT_BSE_FNO), "BSE_FNO");
    }

    #[test]
    fn test_segment_code_to_str_unknown_code() {
        assert_eq!(segment_code_to_str(6), "UNKNOWN"); // Code 6 is unused in Dhan protocol
        assert_eq!(segment_code_to_str(9), "UNKNOWN");
        assert_eq!(segment_code_to_str(255), "UNKNOWN");
    }

    #[test]
    fn test_segment_code_to_str_all_eight_known() {
        // Ensure exactly 8 known segments produce non-UNKNOWN results
        let known_count = (0..=255u8)
            .filter(|&code| segment_code_to_str(code) != "UNKNOWN")
            .count();
        assert_eq!(known_count, 8, "expected exactly 8 known segment codes");
    }
}
