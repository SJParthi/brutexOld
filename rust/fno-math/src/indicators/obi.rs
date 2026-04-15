//! O(1) Order Book Imbalance (OBI) computation from 20-level depth data.
//!
//! OBI measures the imbalance between bid and ask quantities in the order book.
//! A positive OBI indicates more buying pressure, negative indicates more selling.
//!
//! # Formulas
//! - **OBI** = (total_bid_qty - total_ask_qty) / (total_bid_qty + total_ask_qty)
//!   Range: [-1.0, +1.0]. 0.0 = perfectly balanced.
//!
//! - **Weighted OBI** = same formula but each level's quantity is weighted by
//!   `1 / (1 + distance_from_mid)` where distance = level index (0-based).
//!   Levels closer to best bid/ask contribute more.
//!
//! - **Wall Detection** = level where quantity > `WALL_MULTIPLIER` × average quantity.
//!   Identifies large resting orders that may act as support/resistance.
//!
//! - **Spread** = best_ask_price - best_bid_price.
//!
//! # Performance
//! All computations are O(1) — fixed 20-level iteration, no heap allocation.
//! Input is a pair of `&[DeepDepthLevel]` slices (bid + ask sides).

use crate::compat::tick_types::DeepDepthLevel;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Maximum depth levels we process (20-level feed).
const MAX_DEPTH_LEVELS: usize = 20;

/// Wall detection multiplier: a level's quantity must exceed
/// `WALL_MULTIPLIER × average_qty` to be classified as a wall.
const WALL_MULTIPLIER: f64 = 10.0;

// ---------------------------------------------------------------------------
// OBI Snapshot — output of computation
// ---------------------------------------------------------------------------

/// Computed OBI snapshot from a single 20-level depth update.
///
/// `Copy` for zero-allocation pipeline pass-through.
#[derive(Debug, Clone, Copy)]
pub struct ObiSnapshot {
    /// Security ID this snapshot belongs to.
    pub security_id: u32,
    /// Exchange segment byte code.
    pub segment_code: u8,
    /// Simple OBI: (total_bid - total_ask) / (total_bid + total_ask).
    /// Range [-1.0, +1.0]. 0.0 when both sides empty.
    pub obi: f64,
    /// Weighted OBI: closer levels weighted more heavily.
    /// Range [-1.0, +1.0]. 0.0 when both sides empty.
    pub weighted_obi: f64,
    /// Total bid quantity across all non-empty levels.
    pub total_bid_qty: u64,
    /// Total ask quantity across all non-empty levels.
    pub total_ask_qty: u64,
    /// Count of non-empty bid levels.
    pub bid_levels: u32,
    /// Count of non-empty ask levels.
    pub ask_levels: u32,
    /// Price of the largest bid wall (0.0 if no wall detected).
    pub max_bid_wall_price: f64,
    /// Quantity of the largest bid wall (0 if no wall detected).
    pub max_bid_wall_qty: u64,
    /// Price of the largest ask wall (0.0 if no wall detected).
    pub max_ask_wall_price: f64,
    /// Quantity of the largest ask wall (0 if no wall detected).
    pub max_ask_wall_qty: u64,
    /// Spread: best_ask - best_bid. 0.0 if either side empty.
    pub spread: f64,
}

impl Default for ObiSnapshot {
    fn default() -> Self {
        Self {
            security_id: 0,
            segment_code: 0,
            obi: 0.0,
            weighted_obi: 0.0,
            total_bid_qty: 0,
            total_ask_qty: 0,
            bid_levels: 0,
            ask_levels: 0,
            max_bid_wall_price: 0.0,
            max_bid_wall_qty: 0,
            max_ask_wall_price: 0.0,
            max_ask_wall_qty: 0,
            spread: 0.0,
        }
    }
}

// ---------------------------------------------------------------------------
// Computation
// ---------------------------------------------------------------------------

/// Computes OBI from bid and ask depth levels.
///
/// # Arguments
/// * `security_id` — Dhan security ID for the instrument.
/// * `segment_code` — Exchange segment byte code.
/// * `bid_levels` — Bid-side depth levels (up to 20), ordered best→worst.
/// * `ask_levels` — Ask-side depth levels (up to 20), ordered best→worst.
///
/// # Performance
/// O(1) — iterates exactly `min(len, 20)` levels per side. No allocation.
#[allow(clippy::arithmetic_side_effects)] // APPROVED: all arithmetic is bounded f64/u64 with finite checks
pub fn compute_obi(
    security_id: u32,
    segment_code: u8,
    bid_levels: &[DeepDepthLevel],
    ask_levels: &[DeepDepthLevel],
) -> ObiSnapshot {
    let bid_count = bid_levels.len().min(MAX_DEPTH_LEVELS);
    let ask_count = ask_levels.len().min(MAX_DEPTH_LEVELS);

    // --- Accumulate quantities ---
    let mut total_bid_qty: u64 = 0;
    let mut total_ask_qty: u64 = 0;
    let mut weighted_bid: f64 = 0.0;
    let mut weighted_ask: f64 = 0.0;
    let mut non_empty_bid: u32 = 0;
    let mut non_empty_ask: u32 = 0;

    // Bid side
    for (i, level) in bid_levels.iter().take(bid_count).enumerate() {
        if level.quantity == 0 || level.price <= 0.0 || !level.price.is_finite() {
            continue;
        }
        let qty = u64::from(level.quantity);
        total_bid_qty = total_bid_qty.saturating_add(qty);
        // Weight: 1 / (1 + i) — level 0 gets weight 1.0, level 1 gets 0.5, etc.
        let weight = 1.0 / (1.0 + i as f64);
        weighted_bid += qty as f64 * weight;
        non_empty_bid = non_empty_bid.saturating_add(1);
    }

    // Ask side
    for (i, level) in ask_levels.iter().take(ask_count).enumerate() {
        if level.quantity == 0 || level.price <= 0.0 || !level.price.is_finite() {
            continue;
        }
        let qty = u64::from(level.quantity);
        total_ask_qty = total_ask_qty.saturating_add(qty);
        let weight = 1.0 / (1.0 + i as f64);
        weighted_ask += qty as f64 * weight;
        non_empty_ask = non_empty_ask.saturating_add(1);
    }

    // --- Simple OBI --- (round to 4dp: enough precision for -1..+1 range)
    let total = total_bid_qty.saturating_add(total_ask_qty);
    let obi = if total == 0 {
        0.0
    } else {
        let raw = (total_bid_qty as f64 - total_ask_qty as f64) / total as f64;
        (raw * 10000.0).round() / 10000.0
    };

    // --- Weighted OBI --- (round to 4dp)
    let weighted_total = weighted_bid + weighted_ask;
    let weighted_obi = if weighted_total <= 0.0 || !weighted_total.is_finite() {
        0.0
    } else {
        let raw = (weighted_bid - weighted_ask) / weighted_total;
        (raw * 10000.0).round() / 10000.0
    };

    // --- Wall detection --- (prices already f64 from Dhan, round to 2dp)
    let (raw_bid_wall_price, max_bid_wall_qty) = detect_wall(bid_levels, bid_count);
    let (raw_ask_wall_price, max_ask_wall_qty) = detect_wall(ask_levels, ask_count);
    let max_bid_wall_price = (raw_bid_wall_price * 100.0).round() / 100.0;
    let max_ask_wall_price = (raw_ask_wall_price * 100.0).round() / 100.0;

    // --- Spread --- (rounded to 2dp inside compute_spread)
    let spread = compute_spread(bid_levels, bid_count, ask_levels, ask_count);

    ObiSnapshot {
        security_id,
        segment_code,
        obi,
        weighted_obi,
        total_bid_qty,
        total_ask_qty,
        bid_levels: non_empty_bid,
        ask_levels: non_empty_ask,
        max_bid_wall_price,
        max_bid_wall_qty,
        max_ask_wall_price,
        max_ask_wall_qty,
        spread,
    }
}

/// Detects the largest wall in a set of depth levels.
///
/// A wall is a level where quantity > `WALL_MULTIPLIER × average_quantity`.
/// Returns (price, quantity) of the largest wall, or (0.0, 0) if none.
///
/// O(1) — fixed iteration over at most 20 levels (two passes).
#[allow(clippy::arithmetic_side_effects)] // APPROVED: bounded iteration, finite checks
fn detect_wall(levels: &[DeepDepthLevel], count: usize) -> (f64, u64) {
    if count == 0 {
        return (0.0, 0);
    }

    // First pass: compute average quantity of non-empty levels.
    let mut sum: u64 = 0;
    let mut non_empty: u32 = 0;
    for level in levels.iter().take(count) {
        if level.quantity > 0 && level.price > 0.0 && level.price.is_finite() {
            sum = sum.saturating_add(u64::from(level.quantity));
            non_empty = non_empty.saturating_add(1);
        }
    }

    if non_empty == 0 {
        return (0.0, 0);
    }

    let avg_qty = sum as f64 / f64::from(non_empty);
    let wall_threshold = avg_qty * WALL_MULTIPLIER;

    // Second pass: find the largest level exceeding the wall threshold.
    let mut max_wall_price: f64 = 0.0;
    let mut max_wall_qty: u64 = 0;

    for level in levels.iter().take(count) {
        let qty = u64::from(level.quantity);
        if qty as f64 > wall_threshold && qty > max_wall_qty {
            max_wall_price = level.price;
            max_wall_qty = qty;
        }
    }

    (max_wall_price, max_wall_qty)
}

/// Computes the spread between best ask and best bid.
///
/// Returns 0.0 if either side has no valid level.
fn compute_spread(
    bid_levels: &[DeepDepthLevel],
    bid_count: usize,
    ask_levels: &[DeepDepthLevel],
    ask_count: usize,
) -> f64 {
    // Best bid = first non-empty bid level (level 0 if valid).
    let best_bid = bid_levels
        .iter()
        .take(bid_count)
        .find(|l| l.quantity > 0 && l.price > 0.0 && l.price.is_finite())
        .map(|l| l.price);

    // Best ask = first non-empty ask level (level 0 if valid).
    let best_ask = ask_levels
        .iter()
        .take(ask_count)
        .find(|l| l.quantity > 0 && l.price > 0.0 && l.price.is_finite())
        .map(|l| l.price);

    match (best_bid, best_ask) {
        (Some(bid), Some(ask)) => {
            let s = ask - bid;
            // Round to 2dp to prevent f64 arithmetic artifacts
            // (e.g., 2732.3500000000004 → 2732.35).
            if s.is_finite() {
                (s * 100.0).round() / 100.0
            } else {
                0.0
            }
        }
        _ => 0.0,
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// Helper: create a depth level with given price and quantity.
    fn level(price: f64, quantity: u32) -> DeepDepthLevel {
        DeepDepthLevel {
            price,
            quantity,
            orders: 1,
        }
    }

    // --- OBI formula ---

    #[test]
    fn test_compute_obi_balanced_book() {
        let bids = [level(100.0, 1000); 5];
        let asks = [level(101.0, 1000); 5];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert!(
            (snap.obi).abs() < f64::EPSILON,
            "balanced book should have OBI=0"
        );
    }

    #[test]
    fn test_obi_all_bids_no_asks() {
        let bids = [level(100.0, 1000); 5];
        let asks: [DeepDepthLevel; 0] = [];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert!(
            (snap.obi - 1.0).abs() < f64::EPSILON,
            "all bids, no asks → OBI=+1.0"
        );
    }

    #[test]
    fn test_obi_all_asks_no_bids() {
        let bids: [DeepDepthLevel; 0] = [];
        let asks = [level(101.0, 1000); 5];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert!(
            (snap.obi - (-1.0)).abs() < f64::EPSILON,
            "all asks, no bids → OBI=-1.0"
        );
    }

    #[test]
    fn test_obi_both_empty() {
        let bids: [DeepDepthLevel; 0] = [];
        let asks: [DeepDepthLevel; 0] = [];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert!((snap.obi).abs() < f64::EPSILON, "empty book → OBI=0");
    }

    #[test]
    fn test_obi_asymmetric() {
        // bid=3000, ask=1000 → OBI = (3000-1000)/(3000+1000) = 0.5
        let bids = [level(100.0, 1000); 3];
        let asks = [level(101.0, 1000); 1];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert!((snap.obi - 0.5).abs() < f64::EPSILON);
    }

    #[test]
    fn test_obi_range_always_bounded() {
        // Large imbalance: 20 bids × 100000 vs 1 ask × 1
        let bids = [level(100.0, 100000); 20];
        let asks = [level(101.0, 1)];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert!(
            snap.obi >= -1.0 && snap.obi <= 1.0,
            "OBI must be in [-1, +1]"
        );
    }

    // --- Weighted OBI ---

    #[test]
    fn test_weighted_obi_balanced() {
        let bids = [level(100.0, 1000); 5];
        let asks = [level(101.0, 1000); 5];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert!(
            (snap.weighted_obi).abs() < f64::EPSILON,
            "balanced → weighted OBI=0"
        );
    }

    #[test]
    fn test_weighted_obi_front_heavy_bids() {
        // Level 0 bid has huge qty, levels 1-4 have small qty.
        // Level 0 ask has small qty.
        // Weighted OBI should be more positive than simple OBI.
        let mut bids = [level(100.0, 10); 5];
        bids[0] = level(100.0, 10000); // big qty at best bid
        let asks = [level(101.0, 100); 5];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert!(
            snap.weighted_obi > snap.obi,
            "front-heavy bids → weighted > simple"
        );
    }

    #[test]
    fn test_weighted_obi_all_bids() {
        let bids = [level(100.0, 1000); 5];
        let asks: [DeepDepthLevel; 0] = [];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert!((snap.weighted_obi - 1.0).abs() < f64::EPSILON);
    }

    // --- Level counting ---

    #[test]
    fn test_level_counting() {
        let bids = [level(100.0, 1000), level(99.0, 500), level(0.0, 0)];
        let asks = [level(101.0, 800)];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert_eq!(snap.bid_levels, 2, "2 non-empty bids");
        assert_eq!(snap.ask_levels, 1, "1 non-empty ask");
    }

    #[test]
    fn test_zero_qty_levels_skipped() {
        let bids = [level(100.0, 0), level(99.0, 0)];
        let asks = [level(101.0, 0)];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert_eq!(snap.bid_levels, 0);
        assert_eq!(snap.ask_levels, 0);
        assert_eq!(snap.total_bid_qty, 0);
        assert_eq!(snap.total_ask_qty, 0);
    }

    #[test]
    fn test_invalid_price_levels_skipped() {
        let bids = [level(-1.0, 1000), level(f64::NAN, 500)];
        let asks = [level(f64::INFINITY, 800), level(0.0, 200)];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert_eq!(snap.bid_levels, 0, "negative and NaN prices skipped");
        assert_eq!(snap.ask_levels, 0, "infinity and zero prices skipped");
    }

    // --- Wall detection ---

    #[test]
    fn test_wall_detection_large_level() {
        // Average qty = (100+100+100+100+100000)/5 = 20080
        // Threshold = 20080 * 10 = 200800
        // Level with 100000 < 200800 → no wall (not 10x average when itself is part of average)
        // Need a truly outsized level:
        // Average = (10+10+10+10+1000000)/5 = 200008
        // Threshold = 200008 * 10 = 2000080 → 1000000 < threshold → no wall
        // Need: average excluding the big one is ~10, so threshold from average including big = high
        // Actually: with 19 levels at 10 and 1 at 10000:
        // average = (19*10 + 10000)/20 = 10190/20 = 509.5
        // threshold = 509.5 * 10 = 5095
        // 10000 > 5095 → wall detected!
        let mut bids = [level(100.0, 10); 20];
        bids[5] = level(95.0, 10000);
        let (price, qty) = detect_wall(&bids, 20);
        assert!((price - 95.0).abs() < f64::EPSILON, "wall at 95.0");
        assert_eq!(qty, 10000);
    }

    #[test]
    fn test_wall_detection_no_wall() {
        // All equal quantities → no wall (each is 1x average, need >10x)
        let bids = [level(100.0, 1000); 10];
        let (price, qty) = detect_wall(&bids, 10);
        assert!((price).abs() < f64::EPSILON, "no wall → price=0");
        assert_eq!(qty, 0, "no wall → qty=0");
    }

    #[test]
    fn test_wall_detection_empty() {
        let bids: [DeepDepthLevel; 0] = [];
        let (price, qty) = detect_wall(&bids, 0);
        assert!((price).abs() < f64::EPSILON);
        assert_eq!(qty, 0);
    }

    #[test]
    fn test_wall_detection_picks_largest() {
        // Two walls — picks the one with larger quantity
        let mut bids = [level(100.0, 10); 20];
        bids[3] = level(97.0, 8000);
        bids[7] = level(93.0, 12000);
        let (price, qty) = detect_wall(&bids, 20);
        assert!((price - 93.0).abs() < f64::EPSILON, "picks larger wall");
        assert_eq!(qty, 12000);
    }

    // --- Spread ---

    #[test]
    fn test_spread_normal() {
        let bids = [level(100.0, 1000)];
        let asks = [level(100.5, 1000)];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert!((snap.spread - 0.5).abs() < 1e-10);
    }

    #[test]
    fn test_spread_zero_when_no_bids() {
        let bids: [DeepDepthLevel; 0] = [];
        let asks = [level(100.5, 1000)];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert!((snap.spread).abs() < f64::EPSILON);
    }

    #[test]
    fn test_spread_zero_when_no_asks() {
        let bids = [level(100.0, 1000)];
        let asks: [DeepDepthLevel; 0] = [];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert!((snap.spread).abs() < f64::EPSILON);
    }

    #[test]
    fn test_spread_skips_empty_first_level() {
        // First bid level is empty, second is valid
        let bids = [level(0.0, 0), level(99.5, 1000)];
        let asks = [level(100.5, 1000)];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert!(
            (snap.spread - 1.0).abs() < 1e-10,
            "spread from second bid level"
        );
    }

    // --- Security ID and segment passthrough ---

    #[test]
    fn test_security_id_passthrough() {
        let bids = [level(100.0, 1000)];
        let asks = [level(101.0, 1000)];
        let snap = compute_obi(49081, 2, &bids, &asks);
        assert_eq!(snap.security_id, 49081);
        assert_eq!(snap.segment_code, 2);
    }

    // --- Clamped to 20 levels ---

    #[test]
    fn test_more_than_20_levels_clamped() {
        // Pass 25 levels — only first 20 should be counted
        let bids: Vec<DeepDepthLevel> = (0..25).map(|i| level(100.0 - i as f64, 100)).collect();
        let asks = [level(101.0, 100)];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert_eq!(snap.bid_levels, 20, "clamped to 20");
        assert_eq!(snap.total_bid_qty, 2000, "20 × 100");
    }

    // --- Total quantity ---

    #[test]
    fn test_total_qty_sum() {
        let bids = [level(100.0, 500), level(99.0, 300), level(98.0, 200)];
        let asks = [level(101.0, 400), level(102.0, 600)];
        let snap = compute_obi(1, 2, &bids, &asks);
        assert_eq!(snap.total_bid_qty, 1000);
        assert_eq!(snap.total_ask_qty, 1000);
    }

    // --- ObiSnapshot default ---

    #[test]
    fn test_obi_snapshot_default() {
        let snap = ObiSnapshot::default();
        assert_eq!(snap.security_id, 0);
        assert!((snap.obi).abs() < f64::EPSILON);
        assert!((snap.weighted_obi).abs() < f64::EPSILON);
        assert_eq!(snap.total_bid_qty, 0);
        assert_eq!(snap.total_ask_qty, 0);
        assert_eq!(snap.bid_levels, 0);
        assert_eq!(snap.ask_levels, 0);
        assert!((snap.max_bid_wall_price).abs() < f64::EPSILON);
        assert_eq!(snap.max_bid_wall_qty, 0);
        assert!((snap.max_ask_wall_price).abs() < f64::EPSILON);
        assert_eq!(snap.max_ask_wall_qty, 0);
        assert!((snap.spread).abs() < f64::EPSILON);
    }

    // --- ObiSnapshot is Copy ---

    #[test]
    fn test_obi_snapshot_is_copy() {
        let snap = compute_obi(1, 2, &[level(100.0, 1000)], &[level(101.0, 1000)]);
        let copy = snap;
        assert_eq!(copy.security_id, snap.security_id);
        assert!((copy.obi - snap.obi).abs() < f64::EPSILON);
    }
}
