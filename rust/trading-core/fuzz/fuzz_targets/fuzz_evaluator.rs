#![no_main]
//! Fuzz target for the u128 condition evaluator
//! `trading_core::evaluate_atoms` (`src/evaluator.rs`) — the bit-identical
//! Stage-3 vocabulary core shared by BruteX (PyO3) and TickVault (native).
//!
//! Reconciled to the woven 74-bit vocabulary: the VWAP bits 52-53 are
//! RETAINED (DEC-VOCAB-VWAP-RETAIN-001 — `vwap_paisa` is an `Option` input,
//! `None` = abstain), the Central Pivot Range bits 60-63 are implemented off
//! the SAME prev-day pivot anchor, and the Supertrend bits 64-65 read the
//! resolved ×2-scaled line. The Group-15 gap-midpoint (66-68) and fib/swing
//! (69-73) inputs are held at `None`/zero here (bits dark — fuzz arms for
//! them are a named Reconcile-phase obligation; no member authored them).
//!
//! On arbitrary bytes it builds a STRUCTURALLY VALID, in-domain bar snapshot
//! (see the domain note in `fuzz_pivot_fib.rs`: prices capped at `MAX_PAISA`,
//! OHLC ordered `low <= {open, close} <= high`, valid pivots/fib from ordered
//! prev-day OHLC, proximity bps in `[0, 10000]`) and asserts the evaluator:
//!
//! 1. **Never panics / overflows** — all internal products
//!    (`close·bps`, `body·10`, `(c−day_low)·3`, `pdc·50`, the i128 narrow-CPR
//!    width test, …) stay in range within the realistic envelope.
//! 2. **Only sets IMPLEMENTED bits** — the reserved headroom (74-127) is
//!    always clear.
//! 3. **Honours every mutually-exclusive condition pair** — a bar cannot be
//!    both above and below the same level (EMA20/EMA200/VWAP/PDH/PDL), both
//!    bullish and bearish, both a gap-up and a gap-down day, in both the
//!    upper and lower third, etc. The four time-of-day buckets are an
//!    `else if` chain → at most one fires; the three CPR position bits
//!    (60-62) form an exact partition — exactly ONE fires.
//! 4. **Mirrors the resolved market-structure flags exactly** — the Group-12
//!    bits (56-59) are a pure pass-through of `snapshot.market_structure`;
//!    the Supertrend bits (64-65) mirror the strict close-vs-line compare of
//!    the resolved `supertrend_x2_paisa` (`None` ⇒ both clear)
//!    (the stateful machine resolves them upstream; the evaluator must never
//!    invent or drop one).
//!
//! Bit-identity vs the Python source is covered by `tests/golden_vectors.rs`
//! (400-case answer-key); this target proves panic-freedom + the structural
//! invariants over the full in-domain input space.

use libfuzzer_sys::fuzz_target;
use trading_core::bits;
use trading_core::evaluator::DEFAULT_GAP_MID_PROXIMITY_BPS;
use trading_core::{
    IndicatorSnapshot, MarketStructureSignal, PriorBarHistory, ProximityParams, compute_fib_levels,
    compute_pivot_levels, evaluate_atoms,
};

const MAX_PAISA: i64 = 10_000_000_000;

/// Little-endian byte cursor over the fuzz input (missing bytes read as 0).
struct Reader<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self { data, pos: 0 }
    }

    fn u64(&mut self) -> u64 {
        let mut b = [0u8; 8];
        for (k, slot) in b.iter_mut().enumerate() {
            *slot = self.data.get(self.pos + k).copied().unwrap_or(0);
        }
        self.pos += 8;
        u64::from_le_bytes(b)
    }

    /// Price in `[1, MAX_PAISA]`.
    fn price(&mut self) -> i64 {
        1 + (self.u64() % MAX_PAISA as u64) as i64
    }

    /// Proximity tolerance in basis points, `[0, 10000]` (0%–100%).
    fn bps(&mut self) -> i64 {
        (self.u64() % 10_001) as i64
    }

    /// `None` (warm-up) half the time, else an in-domain price.
    fn opt_price(&mut self) -> Option<i64> {
        let v = self.u64();
        if v & 1 == 0 {
            None
        } else {
            Some(1 + ((v >> 1) % MAX_PAISA as u64) as i64)
        }
    }
}

#[inline]
fn has(mask: u128, bit: u32) -> bool {
    mask & (1u128 << bit) != 0
}

fuzz_target!(|data: &[u8]| {
    let mut r = Reader::new(data);

    // ── Current bar OHLC: low <= {open, close} <= high ──
    let (low, high) = {
        let a = r.price();
        let b = r.price();
        if a <= b { (a, b) } else { (b, a) }
    };
    let span = (high - low) as u64;
    let in_band = |raw: u64| -> i64 {
        if span == 0 {
            low
        } else {
            low + (raw % (span + 1)) as i64
        }
    };
    let open = in_band(r.u64());
    let close = in_band(r.u64());

    // ── Day envelope: day_low <= low, day_high >= high ──
    // low >= 1, so `raw % low` lands day_low in [1, low]; day_high adds an
    // in-domain delta (still far below the overflow bound).
    let day_low = low - (r.u64() % low as u64) as i64;
    let day_high = high + (r.u64() % MAX_PAISA as u64) as i64;
    let open_of_day = r.price();

    // ── Prev-day OHLC for pivots/fib: pdl <= pdc <= pdh ──
    let (pdl, pdh) = {
        let a = r.price();
        let b = r.price();
        if a <= b { (a, b) } else { (b, a) }
    };
    let pspan = (pdh - pdl) as u64;
    let pdc = if pspan == 0 {
        pdl
    } else {
        pdl + (r.u64() % (pspan + 1)) as i64
    };

    let pivots = compute_pivot_levels(pdh, pdl, pdc);
    let fib = compute_fib_levels(pdh, pdl);

    // ── Resolved market-structure flags (Group 12) ──
    // The upstream state machine guarantees at most ONE of {bos, choch} per
    // side per bar (the BOS/CHoCH split is a function of the trend); only
    // machine-producible combinations are fed, mirroring production.
    let ms_raw = r.u64();
    let market_structure = MarketStructureSignal {
        bos_bullish: ms_raw & 0b0001 != 0,
        choch_bullish: ms_raw & 0b0001 == 0 && ms_raw & 0b0010 != 0,
        bos_bearish: ms_raw & 0b0100 != 0,
        choch_bearish: ms_raw & 0b0100 == 0 && ms_raw & 0b1000 != 0,
    };

    // ── Resolved SuperTrend line (Group 14): None (warm-up / unwired) half
    // the time, else an arbitrary in-domain ×2 value around the price band so
    // the strict compare sees both sides and the exact tie.
    let supertrend_x2_paisa = {
        let v = r.u64();
        match v & 0b11 {
            0 => None,
            1 => Some(close * 2), // the exact equality edge
            _ => Some(1 + ((v >> 2) % (2 * MAX_PAISA) as u64) as i64),
        }
    };

    let snapshot = IndicatorSnapshot {
        tod_seconds: (r.u64() % 86_400) as u32,
        open_paisa: open,
        high_paisa: high,
        low_paisa: low,
        close_paisa: close,
        ema20_paisa: r.opt_price(),
        ema200_paisa: r.opt_price(),
        vwap_paisa: r.opt_price(),
        pivots,
        fib,
        day_high_paisa: day_high,
        day_low_paisa: day_low,
        open_of_day_paisa: open_of_day,
        market_structure,
        supertrend_x2_paisa,
        // Groups 15 + swing: abstain inputs (bits dark) — fuzz draw arms for
        // 66-68/72-73 were never authored by their member PRs; a named
        // Reconcile-phase obligation, not silently invented here.
        gap_mid_paisa: None,
        last_swing_high_paisa: None,
        last_swing_low_paisa: None,
    };
    let params = ProximityParams {
        pivot_proximity_bps: r.bps(),
        pdh_pdl_proximity_bps: r.bps(),
        fib_proximity_bps: r.bps(),
        // Gap/swing bands at the pyo3 signature defaults: the group inputs
        // above are abstain (None), so these knobs are inert here (the fuzz
        // draw arms are the named Reconcile-phase obligation).
        gap_mid_proximity_bps: DEFAULT_GAP_MID_PROXIMITY_BPS,
        swing_proximity_bps: 10,
    };
    // Prior-bar window of length prior_n in [0, 8], flags from the next bits.
    let prior_n = (r.u64() % 9) as usize;
    let flag_bits = r.u64();
    let flags: Vec<bool> = (0..prior_n).map(|i| (flag_bits >> i) & 1 == 1).collect();
    let prior = PriorBarHistory { prior_n, flags };

    // ── 1. Never panics / overflows ──
    let mask = evaluate_atoms(&snapshot, &params, &prior);

    // ── 2. Only IMPLEMENTED bits may be set ──
    assert_eq!(
        mask & !trading_core::bits::IMPLEMENTED_MASK,
        0,
        "evaluator set a non-implemented bit (reserved >= 74)"
    );

    // ── 3. Mutually-exclusive condition pairs ──
    assert!(
        !(has(mask, bits::BIT_CLOSE_ABOVE_EMA20) && has(mask, bits::BIT_CLOSE_BELOW_EMA20)),
        "close cannot be both above and below EMA20"
    );
    assert!(
        !(has(mask, bits::BIT_CLOSE_ABOVE_EMA200) && has(mask, bits::BIT_CLOSE_BELOW_EMA200)),
        "close cannot be both above and below EMA200"
    );
    assert!(
        !(has(mask, bits::BIT_EMA20_ABOVE_EMA200) && has(mask, bits::BIT_EMA20_BELOW_EMA200)),
        "EMA20 cannot be both above and below EMA200"
    );
    assert!(
        !(has(mask, bits::BIT_CLOSE_ABOVE_VWAP) && has(mask, bits::BIT_CLOSE_BELOW_VWAP)),
        "close cannot be both above and below VWAP"
    );
    assert!(
        !(has(mask, bits::BIT_CLOSE_ABOVE_PDH) && has(mask, bits::BIT_CLOSE_BELOW_PDH)),
        "close cannot be both above and below PDH"
    );
    assert!(
        !(has(mask, bits::BIT_CLOSE_ABOVE_PDL) && has(mask, bits::BIT_CLOSE_BELOW_PDL)),
        "close cannot be both above and below PDL"
    );
    assert!(
        !(has(mask, bits::BIT_BAR_BULLISH) && has(mask, bits::BIT_BAR_BEARISH)),
        "a bar cannot be both bullish and bearish"
    );
    assert!(
        !(has(mask, bits::BIT_GAP_UP_DAY) && has(mask, bits::BIT_GAP_DOWN_DAY)),
        "a day cannot gap both up and down"
    );
    assert!(
        !(has(mask, bits::BIT_INSIDE_DAY) && has(mask, bits::BIT_OUTSIDE_DAY)),
        "a day cannot be both an inside and an outside day"
    );
    assert!(
        !(has(mask, bits::BIT_CLOSE_IN_UPPER_THIRD) && has(mask, bits::BIT_CLOSE_IN_LOWER_THIRD)),
        "close cannot be in both the upper and lower third"
    );
    assert!(
        !(has(mask, bits::BIT_CLOSE_ABOVE_SUPERTREND) && has(mask, bits::BIT_CLOSE_BELOW_SUPERTREND)),
        "close cannot be both above and below the Supertrend line"
    );

    // A warm-up (None) VWAP must fire NEITHER Group-10 bit.
    if snapshot.vwap_paisa.is_none() {
        assert!(
            !has(mask, bits::BIT_CLOSE_ABOVE_VWAP) && !has(mask, bits::BIT_CLOSE_BELOW_VWAP),
            "VWAP bits must not fire during warm-up (vwap_paisa = None)"
        );
    }

    // The three CPR position bits (60-62) form an EXACT PARTITION: the
    // canonicalized band (tc >= bc) makes any close strictly above TC,
    // strictly below BC, or inside the inclusive band — exactly one fires.
    let cpr_pos = [
        bits::BIT_ABOVE_CPR_TC,
        bits::BIT_BELOW_CPR_BC,
        bits::BIT_INSIDE_CPR,
    ]
    .iter()
    .filter(|&&b| has(mask, b))
    .count();
    assert_eq!(cpr_pos, 1, "exactly one CPR position bit must fire");

    // Time-of-day buckets are an else-if chain → at most one is set.
    let tod_set = [
        bits::BIT_EARLY_MORNING,
        bits::BIT_MID_MORNING,
        bits::BIT_MIDDAY,
        bits::BIT_AFTERNOON,
    ]
    .iter()
    .filter(|&&b| has(mask, b))
    .count();
    assert!(tod_set <= 1, "at most one time-of-day bucket may fire");

    // ── 4. Group-12 pass-through: the bits mirror the resolved flags ──
    assert_eq!(
        has(mask, bits::BIT_BOS_BULLISH),
        market_structure.bos_bullish,
        "BOS-bullish bit must mirror the resolved flag"
    );
    assert_eq!(
        has(mask, bits::BIT_BOS_BEARISH),
        market_structure.bos_bearish,
        "BOS-bearish bit must mirror the resolved flag"
    );
    assert_eq!(
        has(mask, bits::BIT_CHOCH_BULLISH),
        market_structure.choch_bullish,
        "CHoCH-bullish bit must mirror the resolved flag"
    );
    assert_eq!(
        has(mask, bits::BIT_CHOCH_BEARISH),
        market_structure.choch_bearish,
        "CHoCH-bearish bit must mirror the resolved flag"
    );

    // ── 5. Group-14: the ST bits mirror the exact strict ×2 compare ──
    match supertrend_x2_paisa {
        None => {
            assert!(
                !has(mask, bits::BIT_CLOSE_ABOVE_SUPERTREND)
                    && !has(mask, bits::BIT_CLOSE_BELOW_SUPERTREND),
                "an absent Supertrend line must clear both Group-14 bits"
            );
        }
        Some(st) => {
            let close_x2 = i128::from(close) * 2;
            let st = i128::from(st);
            assert_eq!(
                has(mask, bits::BIT_CLOSE_ABOVE_SUPERTREND),
                close_x2 > st,
                "close-above-Supertrend must be the strict ×2 compare"
            );
            assert_eq!(
                has(mask, bits::BIT_CLOSE_BELOW_SUPERTREND),
                close_x2 < st,
                "close-below-Supertrend must be the strict ×2 compare"
            );
        }
    }
});
