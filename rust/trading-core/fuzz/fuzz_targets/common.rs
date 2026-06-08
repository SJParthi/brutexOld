//! Shared input sanitisation for the trading-core fuzz targets.
//!
//! `trading-core` deliberately PANICS on two classes of input (see lib.rs
//! "Domain (honest envelope)"): corrupt prev-day OHLC (`pdh < pdl`, `pdc` out of
//! `[pdl, pdh]`) and absurd i64-scale magnitudes (release `overflow-checks`).
//! Those panics are the documented, correct behaviour — a fuzzer that simply
//! threw raw i64 at the kernel would only re-discover them. So every target
//! first folds arbitrary bytes into the *valid* domain (realistic paisa range,
//! well-ordered OHLC) and then asserts the kernel never panics or misbehaves
//! THERE — which is where a real bug would hide.
//!
//! Each fuzz target `mod`-includes this file and uses a subset of the helpers,
//! so dead-code warnings per-target are expected and silenced here.
#![allow(dead_code)]

use arbitrary::Arbitrary;
use trading_core::{
    IndicatorSnapshot, PriorBarHistory, ProximityParams, compute_fib_levels, compute_pivot_levels,
};

/// Realistic Indian index/stock price ceiling in paisa (₹20,000,000). Bounding
/// here keeps every derived level (the largest is `ext_424 = pdh + 3.236*range`,
/// ~8.5e12) far below i64's overflow wall, so the fuzzer probes LOGIC rather than
/// re-triggering the intentional overflow panic.
pub const PRICE_MAX: i64 = 2_000_000_000_000; // 2e12 paisa

/// `(1 << 52) - 1` — the dense 52-condition bit range. Bits 52..=63 are reserved
/// and the evaluator must never set them.
pub const FULL52: u64 = (1u64 << 52) - 1;

/// Fold an arbitrary i64 into `[1, PRICE_MAX]`. `rem_euclid` is total (never
/// panics, even on `i64::MIN`) and non-negative.
#[must_use]
pub fn to_price(raw: i64) -> i64 {
    1 + raw.rem_euclid(PRICE_MAX)
}

/// Fold an arbitrary i64 into `0..=2000` basis points (0%..20% proximity).
#[must_use]
pub fn to_bps(raw: i64) -> i64 {
    raw.rem_euclid(2001)
}

/// Raw, unsanitised fuzz input — every field is arbitrary; [`build`] maps it
/// into trading-core's valid domain.
#[derive(Arbitrary, Debug)]
pub struct RawSnapshot {
    pub tod_seconds: u32,
    pub open: i64,
    pub high: i64,
    pub low: i64,
    pub close: i64,
    pub ema20: Option<i64>,
    pub ema200: Option<i64>,
    pub anchor_a: i64,
    pub anchor_b: i64,
    pub anchor_c: i64,
    pub day_a: i64,
    pub day_b: i64,
    pub open_of_day: i64,
    pub pivot_bps: i64,
    pub pdh_pdl_bps: i64,
    pub fib_bps: i64,
    pub prior_n: u8,
    pub flags: Vec<bool>,
}

/// Clamp `raw` into `[lo, hi]` (both already in-domain, `lo <= hi`) via total
/// modular folding.
fn fold_into(raw: i64, lo: i64, hi: i64) -> i64 {
    let span = hi - lo + 1; // lo,hi in [1, PRICE_MAX] => span in [1, PRICE_MAX], no overflow
    lo + raw.rem_euclid(span)
}

/// Build a fully valid `(snapshot, proximity, prior)` triple from arbitrary
/// bytes. The returned values satisfy every precondition of the kernel.
#[must_use]
pub fn build(r: &RawSnapshot) -> (IndicatorSnapshot, ProximityParams, PriorBarHistory) {
    // Prev-day anchors: well-ordered with pdc in [pdl, pdh] (the valid domain).
    let a = to_price(r.anchor_a);
    let b = to_price(r.anchor_b);
    let pdh = a.max(b);
    let pdl = a.min(b);
    let pdc = fold_into(r.anchor_c, pdl, pdh);

    // Intraday day-range: well-ordered.
    let da = to_price(r.day_a);
    let db = to_price(r.day_b);
    let day_high = da.max(db);
    let day_low = da.min(db);

    let pivots = compute_pivot_levels(pdh, pdl, pdc);
    let fib = compute_fib_levels(pdh, pdl);

    let snapshot = IndicatorSnapshot {
        tod_seconds: r.tod_seconds.rem_euclid(86_400),
        open_paisa: to_price(r.open),
        high_paisa: to_price(r.high),
        low_paisa: to_price(r.low),
        close_paisa: to_price(r.close),
        ema20_paisa: r.ema20.map(to_price),
        ema200_paisa: r.ema200.map(to_price),
        pivots,
        fib,
        day_high_paisa: day_high,
        day_low_paisa: day_low,
        open_of_day_paisa: to_price(r.open_of_day),
    };
    let proximity = ProximityParams {
        pivot_proximity_bps: to_bps(r.pivot_bps),
        pdh_pdl_proximity_bps: to_bps(r.pdh_pdl_bps),
        fib_proximity_bps: to_bps(r.fib_bps),
    };
    // prior_n kept small (0..=7) so the Group-6 scans stay cheap; flags length is
    // left arbitrary so the len != prior_n branches are also exercised.
    let prior = PriorBarHistory {
        prior_n: (r.prior_n % 8) as usize,
        flags: r.flags.clone(),
    };
    (snapshot, proximity, prior)
}
