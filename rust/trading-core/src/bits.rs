//! Bit-position constants for the 52 atomic conditions.
//!
//! A bit-for-bit port of `brutex/vocabulary/bits.py`. The u64 strategy-mask
//! layout: bit 0 = `close_above_ema20` … bit 51 = `outside_day`; bits 52-63
//! reserved. Never renumber a bit (old masks must stay valid across releases).

// Group 1: EMA Position (bits 0-5)
pub const BIT_CLOSE_ABOVE_EMA20: u32 = 0;
pub const BIT_CLOSE_BELOW_EMA20: u32 = 1;
pub const BIT_CLOSE_ABOVE_EMA200: u32 = 2;
pub const BIT_CLOSE_BELOW_EMA200: u32 = 3;
pub const BIT_EMA20_ABOVE_EMA200: u32 = 4;
pub const BIT_EMA20_BELOW_EMA200: u32 = 5;

// Group 2: Pivot Proximity (bits 6-12)
pub const BIT_NEAR_PIVOT_P: u32 = 6;
pub const BIT_NEAR_PIVOT_R1: u32 = 7;
pub const BIT_NEAR_PIVOT_R2: u32 = 8;
pub const BIT_NEAR_PIVOT_R3: u32 = 9;
pub const BIT_NEAR_PIVOT_S1: u32 = 10;
pub const BIT_NEAR_PIVOT_S2: u32 = 11;
pub const BIT_NEAR_PIVOT_S3: u32 = 12;

// Group 3: PDH/PDL (bits 13-18)
pub const BIT_CLOSE_ABOVE_PDH: u32 = 13;
pub const BIT_CLOSE_BELOW_PDH: u32 = 14;
pub const BIT_CLOSE_ABOVE_PDL: u32 = 15;
pub const BIT_CLOSE_BELOW_PDL: u32 = 16;
pub const BIT_NEAR_PDH: u32 = 17;
pub const BIT_NEAR_PDL: u32 = 18;

// Group 4: Fib Proximity (bits 19-29)
pub const BIT_NEAR_FIB_0: u32 = 19;
pub const BIT_NEAR_FIB_236: u32 = 20;
pub const BIT_NEAR_FIB_382: u32 = 21;
pub const BIT_NEAR_FIB_50: u32 = 22;
pub const BIT_NEAR_FIB_618: u32 = 23;
pub const BIT_NEAR_FIB_786: u32 = 24;
pub const BIT_NEAR_FIB_100: u32 = 25;
pub const BIT_NEAR_FIB_1272: u32 = 26;
pub const BIT_NEAR_FIB_1618: u32 = 27;
pub const BIT_NEAR_FIB_200: u32 = 28;
pub const BIT_NEAR_FIB_2618: u32 = 29;

// Group 5: Candle Shape (bits 30-36)
pub const BIT_BAR_BULLISH: u32 = 30;
pub const BIT_BAR_BEARISH: u32 = 31;
pub const BIT_BAR_DOJI: u32 = 32;
pub const BIT_BAR_LARGE_BODY: u32 = 33;
pub const BIT_BAR_SMALL_BODY: u32 = 34;
pub const BIT_LONG_UPPER_WICK: u32 = 35;
pub const BIT_LONG_LOWER_WICK: u32 = 36;

// Group 6: Prior Bar Pattern (bits 37-39)
pub const BIT_PRIOR_N_BULLISH: u32 = 37;
pub const BIT_PRIOR_N_BEARISH: u32 = 38;
pub const BIT_PRIOR_ALTERNATING: u32 = 39;

// Group 7: Day Structure (bits 40-43)
pub const BIT_CLOSE_AT_DAY_HIGH: u32 = 40;
pub const BIT_CLOSE_AT_DAY_LOW: u32 = 41;
pub const BIT_CLOSE_IN_UPPER_THIRD: u32 = 42;
pub const BIT_CLOSE_IN_LOWER_THIRD: u32 = 43;

// Group 8: Time of Day (bits 44-47)
pub const BIT_EARLY_MORNING: u32 = 44;
pub const BIT_MID_MORNING: u32 = 45;
pub const BIT_MIDDAY: u32 = 46;
pub const BIT_AFTERNOON: u32 = 47;

// Group 9: Day Type (bits 48-51)
pub const BIT_GAP_UP_DAY: u32 = 48;
pub const BIT_GAP_DOWN_DAY: u32 = 49;
pub const BIT_INSIDE_DAY: u32 = 50;
pub const BIT_OUTSIDE_DAY: u32 = 51;

/// The number of atomic conditions (dense bits 0..=51).
pub const TOTAL_CONDITIONS: u32 = 52;

#[cfg(test)]
mod tests {
    use super::*;

    /// Mirror of `bits.py::_check_invariants`: the 52 bit positions must form a
    /// dense 0..=51 range with no gaps and no duplicates.
    #[test]
    fn bit_positions_are_dense_0_to_51() {
        let all = [
            BIT_CLOSE_ABOVE_EMA20,
            BIT_CLOSE_BELOW_EMA20,
            BIT_CLOSE_ABOVE_EMA200,
            BIT_CLOSE_BELOW_EMA200,
            BIT_EMA20_ABOVE_EMA200,
            BIT_EMA20_BELOW_EMA200,
            BIT_NEAR_PIVOT_P,
            BIT_NEAR_PIVOT_R1,
            BIT_NEAR_PIVOT_R2,
            BIT_NEAR_PIVOT_R3,
            BIT_NEAR_PIVOT_S1,
            BIT_NEAR_PIVOT_S2,
            BIT_NEAR_PIVOT_S3,
            BIT_CLOSE_ABOVE_PDH,
            BIT_CLOSE_BELOW_PDH,
            BIT_CLOSE_ABOVE_PDL,
            BIT_CLOSE_BELOW_PDL,
            BIT_NEAR_PDH,
            BIT_NEAR_PDL,
            BIT_NEAR_FIB_0,
            BIT_NEAR_FIB_236,
            BIT_NEAR_FIB_382,
            BIT_NEAR_FIB_50,
            BIT_NEAR_FIB_618,
            BIT_NEAR_FIB_786,
            BIT_NEAR_FIB_100,
            BIT_NEAR_FIB_1272,
            BIT_NEAR_FIB_1618,
            BIT_NEAR_FIB_200,
            BIT_NEAR_FIB_2618,
            BIT_BAR_BULLISH,
            BIT_BAR_BEARISH,
            BIT_BAR_DOJI,
            BIT_BAR_LARGE_BODY,
            BIT_BAR_SMALL_BODY,
            BIT_LONG_UPPER_WICK,
            BIT_LONG_LOWER_WICK,
            BIT_PRIOR_N_BULLISH,
            BIT_PRIOR_N_BEARISH,
            BIT_PRIOR_ALTERNATING,
            BIT_CLOSE_AT_DAY_HIGH,
            BIT_CLOSE_AT_DAY_LOW,
            BIT_CLOSE_IN_UPPER_THIRD,
            BIT_CLOSE_IN_LOWER_THIRD,
            BIT_EARLY_MORNING,
            BIT_MID_MORNING,
            BIT_MIDDAY,
            BIT_AFTERNOON,
            BIT_GAP_UP_DAY,
            BIT_GAP_DOWN_DAY,
            BIT_INSIDE_DAY,
            BIT_OUTSIDE_DAY,
        ];
        assert_eq!(all.len(), TOTAL_CONDITIONS as usize);
        let mut sorted = all;
        sorted.sort_unstable();
        for (i, &b) in sorted.iter().enumerate() {
            assert_eq!(b, i as u32, "bit positions must be dense 0..=51");
        }
    }
}
