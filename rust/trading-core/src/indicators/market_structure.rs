//! Market structure — Break of Structure (BOS) + Change of Character (CHoCH).
//!
//! The stateful indicator the Group-12 `bos_bullish` / `bos_bearish` /
//! `choch_bullish` / `choch_bearish` conditions (bits 56-59) consume
//! (ADR-0012 D3/D4 — DEC-NIFTY-BOS-CHOCH-001). THE single detector source:
//! the hand-synced bit-for-bit Python twin that used to live at
//! `brutex/indicators/market_structure.py::MarketStructureState` was DELETED
//! by DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 (`docs/MIGRATION.md` #1548)
//! — BruteX drives this state via the PyO3 pyclass, and the frozen twin-state
//! golden fixture is replayed Rust-only against this source.
//!
//! ## What BOS and CHoCH are
//!
//! Working off CONFIRMED swing points (fractal pivots), with the prevailing
//! trend tracked as a running state. Like every other atomic condition in the
//! vocabulary, these are PER-BAR LEVEL-POSITION PREDICATES (re-evaluated on
//! every bar against the levels known so far), NOT one-shot "events" or edges —
//! a bit reads `true` on each bar whose close holds beyond the relevant
//! confirmed swing, and reads `false` again once the close no longer does (or
//! until a newer swing is confirmed):
//!
//! * **BOS** (Break of Structure — trend CONTINUATION): bullish on any bar whose
//!   close is above the most recent confirmed swing HIGH while the prevailing
//!   trend is up (or not yet set); bearish on any bar whose close is below the
//!   most recent confirmed swing LOW while the prevailing trend is down (or not
//!   yet set). The break also sets/confirms the trend to match.
//! * **CHoCH** (Change of Character — trend REVERSAL): bullish on a bar whose
//!   close takes out the last confirmed swing HIGH while the prevailing trend is
//!   DOWN (which flips the trend up); bearish on a bar whose close takes out the
//!   last confirmed swing LOW while the prevailing trend is UP (which flips the
//!   trend down). Because crossing flips the trend, the very next held-beyond bar
//!   reads as a BOS (continuation) rather than a CHoCH — the CHoCH/BOS split is a
//!   function of the trend AT THE BAR, not a stateful "first time only" latch.
//!
//! On any one bar at most ONE of `{bos_bullish, choch_bullish}` and at most ONE
//! of `{bos_bearish, choch_bearish}` reads `true` (which one depends only on the
//! prevailing trend at that bar). The up-side and down-side are independent: a
//! bar that takes out BOTH the last swing high AND the last swing low (a large
//! engulfing bar) can read one up-bit and one down-bit.
//!
//! ## Look-ahead safety (the crux — ADR-0012 §4.5 #13, Property P-06)
//!
//! A fractal swing at the centre of a `(2k+1)`-bar window is only CONFIRMED once
//! the `k` bars AFTER it have arrived. The state machine confirms the centre bar
//! of the window only when the window is full — i.e. when the bar `k` steps
//! later has been fed. So when evaluating bar `N` the engine has fed bars
//! `0..=N`, and the newest swing the machine can possibly expose sits at index
//! `N − k` (its confirmation needed bars `N−k+1 ..= N`, all `≤ N`). It NEVER
//! references a swing whose confirmation would need a bar `> N`. The current
//! bar's own level is NEVER treated as a confirmed swing — only a strictly
//! past, fully-windowed bar is. This is structurally identical to the existing
//! streaming indicators (`VwapState`, `DayExtremesState`): bars are fed once, in
//! arrival order, and nothing in the future is read.
//!
//! ## O(1) per bar (CLAUDE.md Rule #5)
//!
//! State = a fixed `(2k+1)`-element ring of `(high, low)` + a handful of `i64`
//! and an enum. Each `update` does one ring write, an `O(k)`-with-k-CONSTANT
//! local-extreme scan over the fixed window, and a few comparisons — O(1) per
//! bar (k is a small fixed parameter, NOT the bar count). No growing history,
//! O(1) memory.

/// The prevailing market-structure trend.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Trend {
    /// No trend established yet (before the first structure break).
    None,
    /// Up-trend (the last structure break was bullish).
    Up,
    /// Down-trend (the last structure break was bearish).
    Down,
}

/// The four resolved market-structure booleans for one bar — fed into
/// `evaluate_atoms` exactly like `PriorBarHistory`'s resolved flags.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct MarketStructureSignal {
    /// Bullish break of structure (close took out the last confirmed swing high
    /// in an up / no trend).
    pub bos_bullish: bool,
    /// Bearish break of structure (close took out the last confirmed swing low
    /// in a down / no trend).
    pub bos_bearish: bool,
    /// Bullish change of character (close took out the last confirmed swing high
    /// while the trend was DOWN — a reversal up).
    pub choch_bullish: bool,
    /// Bearish change of character (close took out the last confirmed swing low
    /// while the trend was UP — a reversal down).
    pub choch_bearish: bool,
}

/// One bar's high / low (int64 paisa) held in the confirmation ring.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Bar {
    high: i64,
    low: i64,
}

/// Streaming BOS/CHoCH detector. Hold once per (instrument, timeframe, contract),
/// call [`MarketStructureState::update`] per new bar in arrival order.
///
/// Bit-identical to the Python `MarketStructureState`, so the backtested BOS/CHoCH
/// equals the live one by construction (DEC-SHARED-CORE-001 / ADR-0012 D7).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarketStructureState {
    /// Fractal half-window: a swing needs `k` bars on EACH side to confirm.
    k: usize,
    /// Ring of the last `2k+1` bars (`high`, `low`), oldest→newest by `count`.
    window: Vec<Bar>,
    /// Total bars fed (so the centre of a full window is at `count − 1 − k`).
    count: usize,
    /// The most recent CONFIRMED swing high (paisa), or `None` until one forms.
    last_swing_high: Option<i64>,
    /// The most recent CONFIRMED swing low (paisa), or `None` until one forms.
    last_swing_low: Option<i64>,
    /// The prevailing trend.
    trend: Trend,
}

impl MarketStructureState {
    /// A fresh detector with fractal half-window `k` (k ≥ 1).
    ///
    /// `k = 2` (a 5-bar fractal) is the production default — a swing high is a
    /// bar strictly higher than its 2 neighbours on each side.
    ///
    /// # Panics
    /// Panics if `k == 0` (a 1-bar "window" would confirm the current bar with no
    /// neighbours — a look-ahead-meaningless degenerate; rejected loudly).
    #[must_use]
    pub fn new(k: usize) -> Self {
        assert!(k >= 1, "fractal half-window k must be >= 1");
        MarketStructureState {
            k,
            window: Vec::with_capacity(2 * k + 1),
            count: 0,
            last_swing_high: None,
            last_swing_low: None,
            trend: Trend::None,
        }
    }

    /// The current prevailing trend (introspection / tests).
    #[must_use]
    pub fn trend(&self) -> Trend {
        self.trend
    }

    /// The last confirmed swing high (paisa), or `None`.
    #[must_use]
    pub fn last_swing_high(&self) -> Option<i64> {
        self.last_swing_high
    }

    /// The last confirmed swing low (paisa), or `None`.
    #[must_use]
    pub fn last_swing_low(&self) -> Option<i64> {
        self.last_swing_low
    }

    /// Feed one bar (high/low/close in int64 paisa); return this bar's BOS/CHoCH
    /// signal.
    ///
    /// The order of operations is the load-bearing look-ahead invariant:
    /// 1. push the bar into the confirmation ring (so the window now ends at the
    ///    CURRENT bar);
    /// 2. if the window is full, CONFIRM the centre bar (index `count−1−k`, a
    ///    STRICTLY PAST bar) as a swing high/low — never the current bar;
    /// 3. test the CURRENT bar's `close` against the levels confirmed in step 2
    ///    (or earlier) and resolve BOS/CHoCH + update the trend.
    ///
    /// O(1) per bar (k is a fixed constant).
    pub fn update(&mut self, high: i64, low: i64, close: i64) -> MarketStructureSignal {
        self.push_bar(high, low);
        self.confirm_centre_swing();
        self.resolve_break(close)
    }

    /// Push the bar into the fixed-size ring, dropping the oldest once full.
    fn push_bar(&mut self, high: i64, low: i64) {
        let cap = 2 * self.k + 1;
        if self.window.len() == cap {
            self.window.remove(0);
        }
        self.window.push(Bar { high, low });
        self.count += 1;
    }

    /// If the ring is full, confirm its CENTRE bar as a swing high/low.
    ///
    /// The centre is index `k` of the `2k+1`-element ring — a bar `k` steps in
    /// the past relative to the just-pushed current bar. A strict local maximum
    /// over the `2k` neighbours is a confirmed swing high; a strict local minimum
    /// is a confirmed swing low (a bar can be neither, or — on a flat window —
    /// both-not). The current bar (ring index `2k`) is never the centre, so its
    /// level is never confirmed here — the look-ahead-clean guarantee.
    fn confirm_centre_swing(&mut self) {
        if self.window.len() < 2 * self.k + 1 {
            return; // not enough future bars yet to confirm the centre
        }
        let centre = self.window[self.k];
        let is_high = self
            .window
            .iter()
            .enumerate()
            .all(|(i, b)| i == self.k || b.high < centre.high);
        let is_low = self
            .window
            .iter()
            .enumerate()
            .all(|(i, b)| i == self.k || b.low > centre.low);
        if is_high {
            self.last_swing_high = Some(centre.high);
        }
        if is_low {
            self.last_swing_low = Some(centre.low);
        }
    }

    /// Test the current `close` against the confirmed swings → BOS/CHoCH + trend.
    ///
    /// Up-side and down-side are resolved independently (a single engulfing bar
    /// can break both). Whether a given break is a BOS (continuation) or a CHoCH
    /// (reversal) depends ONLY on the prevailing trend at entry: a bullish break
    /// in a Down-trend is a CHoCH; otherwise it is a BOS. The trend is then moved
    /// to match the break (bullish → Up, bearish → Down). When both sides break in
    /// the same bar the trend follows the bullish side last (deterministic; the
    /// signals for that bar are already resolved against the entry trend).
    fn resolve_break(&mut self, close: i64) -> MarketStructureSignal {
        let entry_trend = self.trend;
        let mut sig = MarketStructureSignal::default();

        if let Some(sh) = self.last_swing_high
            && close > sh
        {
            if entry_trend == Trend::Down {
                sig.choch_bullish = true;
            } else {
                sig.bos_bullish = true;
            }
            self.trend = Trend::Up;
        }
        if let Some(sl) = self.last_swing_low
            && close < sl
        {
            if entry_trend == Trend::Up {
                sig.choch_bearish = true;
            } else {
                sig.bos_bearish = true;
            }
            self.trend = Trend::Down;
        }
        sig
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Feed a flat ramp then a clear higher-high break → bullish BOS once the
    /// swing high is confirmed.
    #[test]
    fn bullish_bos_after_confirmed_swing_high() {
        let mut m = MarketStructureState::new(2);
        // A pivot HIGH at index 2: lows/highs rise then fall around it so the
        // centre bar is a strict local max once 2 more bars arrive.
        // bars (high, low, close): build a swing-high at the 3rd bar.
        let bars = [
            (100, 90, 95),   // 0
            (110, 100, 105), // 1
            (130, 120, 125), // 2  ← swing-high candidate (highest)
            (115, 105, 110), // 3
            (112, 102, 108), // 4  ← confirms bar 2 as swing high (130)
        ];
        let mut last = MarketStructureSignal::default();
        for (h, l, c) in bars {
            last = m.update(h, l, c);
        }
        // The confirming bar's close (108) did NOT exceed the swing high (130) yet.
        assert!(!last.bos_bullish);
        assert_eq!(m.last_swing_high(), Some(130));
        // Now a bar whose close breaks above 130 → bullish BOS, trend → Up.
        let sig = m.update(140, 120, 135);
        assert!(sig.bos_bullish);
        assert!(!sig.choch_bullish);
        assert_eq!(m.trend(), Trend::Up);
    }

    /// A confirmed swing low taken out → bearish BOS (from no trend).
    #[test]
    fn bearish_bos_after_confirmed_swing_low() {
        let mut m = MarketStructureState::new(2);
        let bars = [
            (130, 120, 125), // 0
            (120, 110, 115), // 1
            (100, 80, 90),   // 2  ← swing-low candidate (lowest)
            (115, 105, 110), // 3
            (118, 108, 112), // 4  ← confirms bar 2 as swing low (80)
        ];
        for (h, l, c) in bars {
            m.update(h, l, c);
        }
        assert_eq!(m.last_swing_low(), Some(80));
        let sig = m.update(90, 70, 75); // close 75 < 80
        assert!(sig.bos_bearish);
        assert!(!sig.choch_bearish);
        assert_eq!(m.trend(), Trend::Down);
    }

    /// In a Down-trend, taking out the last swing high is a bullish CHoCH (reversal).
    #[test]
    fn bullish_choch_reverses_a_downtrend() {
        let mut m = MarketStructureState::new(2);
        // First establish a confirmed swing high AND a confirmed swing low, then
        // break the low (→ Down-trend), then break the high (→ bullish CHoCH).
        // Window of 5; craft a swing high at idx2 then a swing low at idx5.
        let setup = [
            (100, 90, 95),   // 0
            (110, 100, 105), // 1
            (130, 120, 125), // 2 swing-high (130)
            (118, 95, 100),  // 3
            (116, 85, 90),   // 4 confirms swing-high@2; close 90
            (108, 70, 75),   // 5 swing-low candidate (70)
            (112, 100, 105), // 6
            (114, 102, 108), // 7 confirms swing-low@5 (70)
        ];
        for (h, l, c) in setup {
            m.update(h, l, c);
        }
        assert_eq!(m.last_swing_high(), Some(130));
        assert_eq!(m.last_swing_low(), Some(70));
        // Break the low → Down-trend (bearish BOS, no trend yet).
        let bear = m.update(90, 60, 65); // close 65 < 70
        assert!(bear.bos_bearish);
        assert_eq!(m.trend(), Trend::Down);
        // Now break the swing high while DOWN → bullish CHoCH.
        let choch = m.update(140, 120, 135); // close 135 > 130
        assert!(choch.choch_bullish);
        assert!(!choch.bos_bullish);
        assert_eq!(m.trend(), Trend::Up);
    }

    /// In an Up-trend, taking out the last swing low is a bearish CHoCH.
    #[test]
    fn bearish_choch_reverses_an_uptrend() {
        let mut m = MarketStructureState::new(2);
        let setup = [
            (130, 120, 125), // 0
            (120, 110, 115), // 1
            (100, 80, 90),   // 2 swing-low (80)
            (118, 108, 112), // 3
            (140, 110, 135), // 4 confirms swing-low@2 (80); also high candidate
            (150, 130, 145), // 5 swing-high candidate (150)
            (138, 118, 122), // 6
            (136, 116, 120), // 7 confirms swing-high@5 (150)
        ];
        for (h, l, c) in setup {
            m.update(h, l, c);
        }
        assert_eq!(m.last_swing_low(), Some(80));
        assert_eq!(m.last_swing_high(), Some(150));
        // Break the high → Up-trend (bullish BOS).
        let bull = m.update(160, 140, 155); // close 155 > 150
        assert!(bull.bos_bullish);
        assert_eq!(m.trend(), Trend::Up);
        // Break the low while UP → bearish CHoCH.
        let choch = m.update(90, 60, 65); // close 65 < 80
        assert!(choch.choch_bearish);
        assert!(!choch.bos_bearish);
        assert_eq!(m.trend(), Trend::Down);
    }

    /// No confirmed swing yet → no signal (warm-up).
    #[test]
    fn no_signal_before_any_confirmed_swing() {
        let mut m = MarketStructureState::new(2);
        // Fewer than 2k+1 = 5 bars → no confirmation possible.
        for (h, l, c) in [(100, 90, 95), (200, 80, 150), (300, 70, 250)] {
            let sig = m.update(h, l, c);
            assert_eq!(sig, MarketStructureSignal::default());
        }
        assert_eq!(m.last_swing_high(), None);
        assert_eq!(m.last_swing_low(), None);
        assert_eq!(m.trend(), Trend::None);
    }

    /// A flat window confirms NEITHER swing (no strict local extreme) — the
    /// degenerate-window guard.
    #[test]
    fn flat_window_confirms_no_swing() {
        let mut m = MarketStructureState::new(2);
        for _ in 0..6 {
            let sig = m.update(100, 100, 100);
            assert_eq!(sig, MarketStructureSignal::default());
        }
        assert_eq!(m.last_swing_high(), None);
        assert_eq!(m.last_swing_low(), None);
    }

    /// A continuation BOS in an established Up-trend stays a BOS (not a CHoCH).
    #[test]
    fn continuation_bos_in_uptrend_is_not_choch() {
        let mut m = MarketStructureState::new(2);
        let setup = [
            (100, 90, 95),
            (110, 100, 105),
            (130, 120, 125), // swing-high (130)
            (115, 105, 110),
            (112, 102, 108), // confirms swing-high (130)
        ];
        for (h, l, c) in setup {
            m.update(h, l, c);
        }
        let first = m.update(140, 120, 135); // break 130 → Up (BOS)
        assert!(first.bos_bullish);
        assert_eq!(m.trend(), Trend::Up);
        // Confirm a NEW higher swing high, then break it again → another BOS.
        let more = [
            (150, 130, 145), // new swing-high candidate (150)
            (142, 122, 128),
            (141, 121, 127), // confirms 150
        ];
        for (h, l, c) in more {
            m.update(h, l, c);
        }
        assert_eq!(m.last_swing_high(), Some(150));
        let second = m.update(160, 140, 155); // break 150 while UP → BOS
        assert!(second.bos_bullish);
        assert!(!second.choch_bullish);
        assert_eq!(m.trend(), Trend::Up);
    }

    /// k must be >= 1.
    #[test]
    #[should_panic(expected = "fractal half-window k must be >= 1")]
    fn k_zero_panics() {
        let _ = MarketStructureState::new(0);
    }

    /// The ring never grows past `2k+1` even after many bars (O(1) memory).
    #[test]
    fn ring_is_bounded() {
        let mut m = MarketStructureState::new(2);
        for i in 0..1000i64 {
            m.update(100 + i, 50 + i, 75 + i);
        }
        assert_eq!(m.window.len(), 5);
        assert_eq!(m.count, 1000);
    }
}
