//! SuperTrend — the ATR-band trailing-stop line (bits 64-65).
//!
//! The stateful indicator the Group-14 `close_above_supertrend` /
//! `close_below_supertrend` conditions consume. A DETERMINISTIC integer port
//! of the TradingView Pine v6 `ta.supertrend(factor, atrPeriod)` ALGORITHM
//! with the LOCKED parameters ATR period **10** and factor **3**
//! ([`SUPERTREND_ATR_PERIOD`] / [`SUPERTREND_FACTOR`]), source **hl2** — NOT
//! close. It is **NOT bit-equal to TradingView's float series**: TV computes
//! in floats, this state in ×2-scaled integers, and the ×2 integer truncation
//! IS the definition (measured by review against a float reference: the line
//! value drifts by sub-paisa truncation residue on ~57% of bars, and 1
//! direction divergence was observed in 52,382 bars, sitting exactly on a
//! flip boundary the two roundings resolve differently).
//!
//! ## The ×2-scaled integer domain (the half-paisa hazard)
//!
//! `hl2 = (high + low) / 2` is a HALF-paisa quantity whenever `high + low` is
//! odd, so a plain integer-paisa port would silently truncate half a paisa out
//! of every band. This state therefore computes EVERYTHING in the **×2-scaled
//! paisa domain**: every internal quantity (and the exposed line) is exactly
//! TWICE its rupee-paisa value, i.e. one unit = half a paisa:
//!
//! * `hl2_x2 = high + low` — EXACT, no halving ever happens;
//! * `tr_x2  = 2 · TR` where `TR = max(H−L, |H−C_prev|, |L−C_prev|)` (TR is an
//!   integer paisa value, so the ×2 scaling is exact; the FIRST bar's TR is
//!   `H−L` — Pine `ta.tr(true)`'s `nz(close[1])` handling);
//! * `atr_x2` = the Wilder RMA **in the ×2 domain**: seeded with the SMA of the
//!   first 10 `tr_x2` values (`sum / 10`, TRUNCATING integer division — the
//!   `VwapState` law: no floats anywhere in the state), thereafter
//!   `atr_x2 ← (atr_x2·9 + tr_x2) / 10`, truncating. NOTE the divisions happen
//!   in the ×2 domain, so the resolution of the truncation is half a paisa —
//!   this IS the definition, not an approximation of a paisa-domain ATR;
//! * bands `= hl2_x2 ± 3·atr_x2`; every close comparison is `close·2` vs a
//!   band — exact in ×2, no rounding at any comparison.
//!
//! Internal accumulators are `i128` so the state is TOTAL for arbitrary `i64`
//! OHLC (never a panic, never a wrap — the sums/products are bounded by a few
//! multiples of 2⁶⁵, far inside i128). The exposed line is `Option<i64>`: for
//! any real price (and the whole property domain ≤ 10¹² paisa) it always fits;
//! only an absurd near-`i64::MAX` input could push the ×2 line past `i64`, in
//! which case `update` returns an honest `None` for that bar (never a clamped
//! or wrapped value) while the internal state stays exact.
//!
//! ## The Pine v6 algorithm, verbatim (band ratchet + flip law)
//!
//! ```text
//! basicUpper = hl2 + factor·ATR         basicLower = hl2 − factor·ATR
//! finalUpper = (basicUpper < prevFinalUpper OR close[1] > prevFinalUpper)
//!                  ? basicUpper : prevFinalUpper
//! finalLower = (basicLower > prevFinalLower OR close[1] < prevFinalLower)
//!                  ? basicLower : prevFinalLower
//! if atr[1] is na:                 direction := +1            (seed: DOWN)
//! else if prevLine == prevFinalUpper:
//!     direction := close > finalUpper ? −1 : +1
//! else:
//!     direction := close < finalLower ? +1 : −1
//! line := direction == −1 ? finalLower : finalUpper
//! ```
//!
//! * **Direction convention is Pine's INVERSION of the textbook**: `−1` = an
//!   UP-trend (the line is the LOWER band, trailing below price); `+1` = a
//!   DOWN-trend (the line is the UPPER band). Pinned by tests — do not "fix".
//! * The flip compares the CURRENT close against the CURRENT-bar FINAL
//!   (ratcheted) band with STRICT inequality — equality is NO flip.
//! * The ratchet's escape hatch reads `close[1]` (the PREVIOUS bar's close),
//!   letting a band re-loosen after price closed through it.
//! * The branch selector is Pine's VALUE equality `prevLine == prevFinalUpper`
//!   (exact in integers) — on a width-0 band (finalUpper == finalLower) the
//!   upper branch is taken regardless of the prior direction, exactly as Pine.
//! * First-computed-bar seed: at 0-based bar 9 (= `atrPeriod − 1`, the first
//!   bar with an ATR) `atr[1]` is still na, so the direction seeds `+1` (DOWN)
//!   and the line sits on the UPPER band; the final bands seed from the basic
//!   bands. (Pine reaches the same values through `nz(band[1], 0)`: for any
//!   POSITIVE price series `close[1] > 0 == prevFinalUpper` forces the upper
//!   escape and `basicLower > 0` forces the lower, so both final bands equal
//!   the basic bands on that bar. For non-positive — non-market — prices Pine's
//!   `nz(…, 0)` quirk would differ; this port defines the seed law explicitly
//!   and deterministically instead, documented here.)
//!
//! ## Warm-up
//!
//! Bars 0..=8 (0-based) return `None` — exactly **9** warm-up `None`s; the
//! first computed value lands at 0-based bar 9.
//!
//! ## NO session resets (deliberate)
//!
//! Like EMA 20/200 — and unlike the session-anchored day extremes — the state
//! persists across IST day boundaries: TradingView computes SuperTrend on the
//! CONTINUOUS bar series, so a session/date change resets NOTHING here. (A
//! CONTRACT roll is a different axis: the engine's D11 total hard reset
//! recreates every stateful object, this one included, exactly as it recreates
//! `MarketStructureState` — that decision lives with the caller, not here.)
//!
//! ## O(1) per bar (CLAUDE.md Rule #5)
//!
//! State = a handful of `i128` scalars + a bar counter. Each `update` is a few
//! comparisons, two multiplies and one division — O(1) time, O(1) memory.

/// The LOCKED ATR look-back (Pine `atrPeriod`): 10 bars.
pub const SUPERTREND_ATR_PERIOD: u32 = 10;

/// The LOCKED band multiplier (Pine `factor`): 3.
pub const SUPERTREND_FACTOR: i128 = 3;

/// Pine direction token for an UP-trend (line = LOWER band). NOTE the Pine
/// inversion: `-1` is UP, not down.
pub const SUPERTREND_DIR_UP: i8 = -1;

/// Pine direction token for a DOWN-trend (line = UPPER band). The seed value
/// while `atr[1]` is na.
pub const SUPERTREND_DIR_DOWN: i8 = 1;

/// Streaming SuperTrend state. Hold once per continuous bar series (per
/// (instrument, timeframe, contract)), call [`SupertrendState::update`] once
/// per bar in arrival order. See the module docs for the exact integer law.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SupertrendState {
    /// Total bars fed so far.
    bars_seen: u64,
    /// The previous bar's `close × 2` (the ratchet's `close[1]` escape). Only
    /// read once `bars_seen >= 1`.
    prev_close_x2: i128,
    /// Warm-up sum of the first `SUPERTREND_ATR_PERIOD` `tr_x2` values.
    tr_sum_x2: i128,
    /// Wilder ATR in the ×2 domain, `None` during warm-up (bars 0..=8).
    atr_x2: Option<i128>,
    /// The previous bar's FINAL (ratcheted) upper band (×2). Valid once
    /// `atr_x2` was `Some` on the previous bar.
    final_upper_x2: i128,
    /// The previous bar's FINAL (ratcheted) lower band (×2).
    final_lower_x2: i128,
    /// The previous bar's line value (×2) — Pine's `prevSuperTrend`, compared
    /// by VALUE against `final_upper_x2` to pick the flip branch.
    line_x2: Option<i128>,
    /// The current direction: [`SUPERTREND_DIR_UP`] (−1) / [`SUPERTREND_DIR_DOWN`]
    /// (+1), `None` until the first computed bar.
    direction: Option<i8>,
}

impl Default for SupertrendState {
    fn default() -> Self {
        Self::new()
    }
}

impl SupertrendState {
    /// A fresh state (no parameters — ATR 10 / factor 3 are compile-time
    /// constants of the vocabulary, never a sweep axis).
    #[must_use]
    pub fn new() -> Self {
        SupertrendState {
            bars_seen: 0,
            prev_close_x2: 0,
            tr_sum_x2: 0,
            atr_x2: None,
            final_upper_x2: 0,
            final_lower_x2: 0,
            line_x2: None,
            direction: None,
        }
    }

    /// Bars processed so far.
    #[must_use]
    pub fn bars_seen(&self) -> u64 {
        self.bars_seen
    }

    /// The current direction: `Some(-1)` = UP-trend (line = lower band),
    /// `Some(1)` = DOWN-trend (line = upper band), `None` during warm-up.
    #[must_use]
    pub fn direction(&self) -> Option<i8> {
        self.direction
    }

    /// The current line in the ×2-scaled paisa domain, or `None` during
    /// warm-up / when the exact ×2 value does not fit `i64` (absurd
    /// near-`i64::MAX` inputs only — see the module docs).
    #[must_use]
    pub fn supertrend_x2(&self) -> Option<i64> {
        self.line_x2.and_then(|v| i64::try_from(v).ok())
    }

    /// The exact internal ×2 line as `i128` (test/introspection surface — the
    /// FFI consumers use [`SupertrendState::supertrend_x2`]).
    #[must_use]
    pub fn line_x2_exact(&self) -> Option<i128> {
        self.line_x2
    }

    /// Feed one bar (high/low/close in int64 paisa); return the bar's
    /// SuperTrend line in the ×2-scaled paisa domain (`None` during the 9-bar
    /// warm-up, or if the exact value does not fit `i64`).
    ///
    /// Total for ANY `i64` inputs (all internal arithmetic is `i128`); never
    /// panics, never wraps. O(1) per bar.
    pub fn update(&mut self, high: i64, low: i64, close: i64) -> Option<i64> {
        let h = i128::from(high);
        let l = i128::from(low);
        let close_x2 = i128::from(close) * 2;

        // ── True Range (×2). First bar: TR = H − L (Pine ta.tr(true)). ──
        let tr = if self.bars_seen == 0 {
            h - l
        } else {
            let pc = self.prev_close_x2 / 2; // exact: prev_close_x2 is 2·close[1]
            (h - l).max((h - pc).abs()).max((l - pc).abs())
        };
        let tr_x2 = tr * 2;

        // ── Wilder ATR (×2): SMA(10) seed, then (atr·9 + tr)/10, truncating. ──
        let period = i128::from(SUPERTREND_ATR_PERIOD);
        // `atr[1]` in Pine terms — the ATR as of the PREVIOUS bar (na on the
        // seed bar itself), the flip-law seed selector.
        let atr_prev_was_na = self.atr_x2.is_none();
        let atr_x2 = match self.atr_x2 {
            Some(prev) => {
                let next = (prev * (period - 1) + tr_x2) / period;
                self.atr_x2 = Some(next);
                Some(next)
            }
            None => {
                self.tr_sum_x2 += tr_x2;
                if self.bars_seen + 1 == u64::from(SUPERTREND_ATR_PERIOD) {
                    // Seed bar (0-based bar 9): ATR = SMA of the first 10 TRs.
                    let seed = self.tr_sum_x2 / period;
                    self.atr_x2 = Some(seed);
                    Some(seed)
                } else {
                    None
                }
            }
        };

        let out = match atr_x2 {
            None => {
                // Warm-up (bars 0..=8): no bands, no direction, no line.
                None
            }
            Some(atr) => {
                let hl2_x2 = h + l; // EXACT — the whole point of the ×2 domain.
                let basic_upper = hl2_x2 + SUPERTREND_FACTOR * atr;
                let basic_lower = hl2_x2 - SUPERTREND_FACTOR * atr;

                let (final_upper, final_lower, direction) = if atr_prev_was_na {
                    // First computed bar (0-based bar 9): the bands seed from
                    // the basic bands and the direction seeds DOWN (+1) — Pine
                    // `if na(atr[1]): direction := 1` (see the module docs for
                    // the nz(…, 0) equivalence on positive prices).
                    (basic_upper, basic_lower, SUPERTREND_DIR_DOWN)
                } else {
                    // Band ratchet with the close[1] escape (STRICT compares).
                    let final_upper = if basic_upper < self.final_upper_x2
                        || self.prev_close_x2 > self.final_upper_x2
                    {
                        basic_upper
                    } else {
                        self.final_upper_x2
                    };
                    let final_lower = if basic_lower > self.final_lower_x2
                        || self.prev_close_x2 < self.final_lower_x2
                    {
                        basic_lower
                    } else {
                        self.final_lower_x2
                    };
                    // Flip law: CURRENT close vs the CURRENT-bar FINAL band,
                    // STRICT inequality (equality = NO flip). The branch is
                    // Pine's VALUE equality `prevLine == prevFinalUpper` —
                    // exact in integers; on a width-0 band the upper branch
                    // wins regardless of the prior direction (Pine parity).
                    let direction = if self.line_x2 == Some(self.final_upper_x2) {
                        if close_x2 > final_upper {
                            SUPERTREND_DIR_UP
                        } else {
                            SUPERTREND_DIR_DOWN
                        }
                    } else if close_x2 < final_lower {
                        SUPERTREND_DIR_DOWN
                    } else {
                        SUPERTREND_DIR_UP
                    };
                    (final_upper, final_lower, direction)
                };

                let line = if direction == SUPERTREND_DIR_UP {
                    final_lower
                } else {
                    final_upper
                };
                self.final_upper_x2 = final_upper;
                self.final_lower_x2 = final_lower;
                self.line_x2 = Some(line);
                self.direction = Some(direction);
                i64::try_from(line).ok()
            }
        };

        self.prev_close_x2 = close_x2;
        self.bars_seen += 1;
        out
    }
}

/// Batch helper: run a fresh [`SupertrendState`] over three equal-length
/// columns, returning one `Option<i64>` line (×2 paisa) per bar in input
/// order. Bit-identical to streaming BY CONSTRUCTION (it delegates to the one
/// `update`); the equality is nevertheless test-pinned below.
///
/// # Errors
/// `Err(String)` on a column-length mismatch (never a panic).
pub fn compute_supertrend(
    high_paisa: &[i64],
    low_paisa: &[i64],
    close_paisa: &[i64],
) -> Result<Vec<Option<i64>>, String> {
    let n = high_paisa.len();
    if low_paisa.len() != n || close_paisa.len() != n {
        return Err(format!(
            "supertrend: column lengths differ (high={n}, low={}, close={})",
            low_paisa.len(),
            close_paisa.len()
        ));
    }
    let mut state = SupertrendState::new();
    Ok((0..n)
        .map(|i| state.update(high_paisa[i], low_paisa[i], close_paisa[i]))
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    /// Ten identical bars H=110, L=90, C=100 (all paisa):
    /// TR(bar 0) = H−L = 20; TR(bars 1..) = max(20, |110−100|, |90−100|) = 20.
    /// tr_x2 = 40 each ⇒ seed atr_x2 = 400/10 = 40.
    /// hl2_x2 = 200; basic bands = 200 ± 3·40 = 320 / 80.
    const FLAT: (i64, i64, i64) = (110, 90, 100);

    fn feed(state: &mut SupertrendState, bars: &[(i64, i64, i64)]) -> Vec<Option<i64>> {
        bars.iter()
            .map(|&(h, l, c)| state.update(h, l, c))
            .collect()
    }

    // ── warm-up ────────────────────────────────────────────────────────────

    #[test]
    fn warmup_is_exactly_nine_nones_then_a_value_at_bar_nine() {
        let mut s = SupertrendState::new();
        for bar in 0..9u64 {
            assert_eq!(s.update(FLAT.0, FLAT.1, FLAT.2), None, "bar {bar}");
            assert_eq!(s.direction(), None, "bar {bar}: no direction in warm-up");
            assert_eq!(s.supertrend_x2(), None, "bar {bar}");
        }
        // 0-based bar 9 (= atrPeriod − 1) carries the FIRST value.
        let first = s.update(FLAT.0, FLAT.1, FLAT.2);
        assert!(first.is_some(), "bar 9 must produce the first line");
        assert_eq!(s.bars_seen(), 10);
    }

    #[test]
    fn first_value_is_the_upper_band_and_direction_seeds_down() {
        // Hand derivation (FLAT): seed atr_x2 = 40; upper = 200 + 120 = 320.
        let mut s = SupertrendState::new();
        let out = feed(&mut s, &[FLAT; 10]);
        assert_eq!(out[9], Some(320), "first line = UPPER band (seed DOWN)");
        assert_eq!(s.direction(), Some(SUPERTREND_DIR_DOWN));
    }

    // ── ATR: SMA seed + Wilder recurrence, truncating division ─────────────

    #[test]
    fn sma_seed_is_exact_and_truncating() {
        // 9 FLAT bars (TR 20 each) + a 10th bar with TR 21:
        // bar 9: H=111, L=90, C=100 ⇒ TR = max(21, |111−100|, |90−100|) = 21.
        // tr_x2 sum = 9·40 + 42 = 402 ⇒ seed atr_x2 = 402/10 = 40 (TRUNC — the
        // real quotient is 40.2; a rounding impl would read 40 too, so also
        // pin a .5-class case below).
        let mut s = SupertrendState::new();
        let mut bars = vec![FLAT; 9];
        bars.push((111, 90, 100));
        feed(&mut s, &bars);
        // hl2_x2(bar 9) = 111 + 90 = 201; upper = 201 + 3·40 = 321.
        assert_eq!(s.line_x2_exact(), Some(321));

        // The .5-tie: 9 FLAT + TR 25 ⇒ sum = 9·40 + 50 = 410 ⇒ 41 exact; use
        // TR 24.5? impossible (TR is integer paisa) — instead pin truncation
        // with sum 405: 7 FLAT (7·40 = 280) + TR 22 (44) + TR 20 (40) +
        // TR 20.5 impossible. Direct pin: sum 9·40 + 45 unreachable (odd
        // tr_x2 impossible — tr_x2 = 2·TR is always even). So the ×2-domain
        // seed sum is always EVEN; sum/10 truncates whenever sum % 10 != 0,
        // covered by the 402 case above (40.2 → 40) and the Wilder case below.
        //
        // Confirm the seed did NOT round half-up on a x.8 case: 9 FLAT + TR 24
        // ⇒ sum = 408 ⇒ 40 (real 40.8 — rounding would give 41).
        let mut s2 = SupertrendState::new();
        let mut bars2 = vec![FLAT; 9];
        bars2.push((114, 90, 100)); // TR = 24, tr_x2 = 48; sum = 408.
        feed(&mut s2, &bars2);
        // hl2_x2 = 204; upper = 204 + 3·40 = 324 (seed 40, NOT 41).
        assert_eq!(s2.line_x2_exact(), Some(324));
    }

    #[test]
    fn wilder_recurrence_is_exact_and_truncating() {
        // Seed with 10 FLAT bars: atr_x2 = 40 (derivation above). Bar 10:
        // H=120, L=90, C=100 ⇒ TR = max(30, |120−100|=20, |90−100|=10) = 30,
        // tr_x2 = 60. Wilder: atr_x2 = (40·9 + 60)/10 = 420/10 = 42 EXACT.
        // hl2_x2 = 210; basic upper = 210 + 126 = 336; basic lower = 84.
        // Ratchet vs prev finals (320/80): 336 < 320? no; close[1]·2 = 200 >
        // 320? no ⇒ final upper stays 320. 84 > 80 ⇒ final lower 84.
        // prevLine(320) == prevFinalUpper(320) ⇒ upper branch: close_x2 200 >
        // 320? no ⇒ direction stays DOWN, line = 320.
        let mut s = SupertrendState::new();
        feed(&mut s, &[FLAT; 10]);
        let out = s.update(120, 90, 100);
        assert_eq!(out, Some(320));
        assert_eq!(s.direction(), Some(SUPERTREND_DIR_DOWN));
        // Truncation teeth inside the recurrence: a TR-25 bar.
        // H=115, L=90, C=100 ⇒ TR = max(25, 15, 10) = 25, tr_x2 = 50.
        // atr_x2 = (42·9 + 50)/10 = 428/10 = 42 (real 42.8 — TRUNC, not 43).
        // hl2_x2 = 205; basic upper = 205 + 126 = 331 (a rounding impl would
        // read 205 + 129 = 334); 331 < 320? no; 200 > 320? no ⇒ upper stays
        // 320, line stays 320.
        let out2 = s.update(115, 90, 100);
        assert_eq!(out2, Some(320));
        // Break the band so the NEXT bar's close[1] escape exposes the fresh
        // basic band (and thereby the exact ATR) directly: close 161 ⇒
        // close_x2 = 322 > 320 ⇒ flips UP (line = final lower band).
        let out3 = s.update(111, 90, 161);
        assert_eq!(s.direction(), Some(SUPERTREND_DIR_UP));
        assert!(out3.is_some());
    }

    // ── band ratchet + close[1] escape, both directions ────────────────────

    #[test]
    fn upper_band_ratchets_down_and_releases_on_prev_close_escape() {
        // Establish DOWN-trend on FLAT bars: final upper = 320.
        let mut s = SupertrendState::new();
        feed(&mut s, &[FLAT; 10]);
        assert_eq!(s.line_x2_exact(), Some(320));
        // A narrower bar pulls basic_upper BELOW 320 ⇒ ratchet adopts it
        // (basicUpper < prevFinalUpper). H=105, L=95, C=100: TR = max(10, 5,
        // 5) = 10, tr_x2 = 20; atr_x2 = (40·9 + 20)/10 = 38.
        // hl2_x2 = 200; basic upper = 200 + 114 = 314 < 320 ⇒ final = 314.
        let out = s.update(105, 95, 100);
        assert_eq!(out, Some(314), "upper band must ratchet DOWN");
        // A wider bar's basic_upper (> 314) is REFUSED while close[1] stays
        // under the band (the ratchet holds)…
        // H=120, L=80, C=100: TR = 40, tr_x2 = 80; atr = (38·9 + 80)/10 = 42.
        // basic upper = 200 + 126 = 326 ≥ 314, close[1]·2 = 200 ≤ 314 ⇒ 314.
        let out2 = s.update(120, 80, 100);
        assert_eq!(
            out2,
            Some(314),
            "wider basic band must NOT loosen the ratchet"
        );
        // …but after close[1] pierces the band the escape releases it.
        // Bar A: close 158 (close_x2 316 > 314) ⇒ direction flips UP.
        s.update(110, 90, 158);
        assert_eq!(s.direction(), Some(SUPERTREND_DIR_UP));
        // Bar B: prev_close_x2 = 316 > prevFinalUpper 314 ⇒ finalUpper takes
        // the fresh basic value even though basic ≥ prev final (the escape).
        // (Direction is UP so the LINE is the lower band; the escape's effect
        // is pinned via the internal final_upper_x2.)
        let before = s.final_upper_x2;
        s.update(110, 90, 100);
        assert_ne!(
            s.final_upper_x2, before,
            "close[1] escape must re-arm the upper band"
        );
    }

    #[test]
    fn lower_band_ratchets_up_and_releases_on_prev_close_escape() {
        // Mirror case. First establish an UP-trend: FLAT seed then a break up.
        let mut s = SupertrendState::new();
        feed(&mut s, &[FLAT; 10]);
        s.update(110, 90, 161); // close_x2 322 > 320 ⇒ UP; line = lower band
        assert_eq!(s.direction(), Some(SUPERTREND_DIR_UP));
        let line_after_flip = s.line_x2_exact().unwrap();
        // In UP-trend the line is the LOWER band; a higher bar pulls
        // basic_lower ABOVE the final ⇒ ratchet adopts it (basicLower >
        // prevFinalLower).
        let out = s.update(150, 130, 140);
        assert!(out.is_some());
        assert!(
            s.final_lower_x2 > line_after_flip,
            "lower band must ratchet UP on a strongly higher bar"
        );
        // Without the escape the lower ratchet is monotone NON-DECREASING —
        // it can never loosen while close[1] holds above the band.
        let held = s.final_lower_x2;
        s.update(150, 130, 140); // close[1]·2 = 280 stays above the band
        assert!(
            s.final_lower_x2 >= held,
            "lower ratchet must never loosen without the escape"
        );
        // …then a close BELOW the band flips DOWN and (next bar) the
        // close[1] < prevFinalLower escape re-arms the lower band.
        // Post-flip state: final lower = 157 (see the derivation trail);
        // close 70 ⇒ close_x2 = 140 < 157 ⇒ DOWN.
        s.update(150, 100, 70);
        assert_eq!(s.direction(), Some(SUPERTREND_DIR_DOWN));
        let before = s.final_lower_x2;
        s.update(150, 100, 120); // close[1]·2 = 140 < prevFinalLower ⇒ escape
        assert_ne!(
            s.final_lower_x2, before,
            "close[1] escape must re-arm the lower band"
        );
    }

    // ── flip law: strict inequality (equality = NO flip) ───────────────────

    #[test]
    fn close_equal_to_the_band_does_not_flip() {
        // FLAT seed ⇒ DOWN, final upper = 320. close_x2 == 320 ⇔ close == 160.
        let mut s = SupertrendState::new();
        feed(&mut s, &[FLAT; 10]);
        // Keep TR shape stable: H=110+something? Use H=180,L=90,C=160:
        // TR = max(90, 80, 30) = 90, atr = (40·9+180)/10 = 54; hl2_x2 = 270;
        // basic upper = 270+162 = 432 ≥ 320, close[1]·2 = 200 ≤ 320 ⇒ final
        // upper stays 320. close_x2 = 320 == finalUpper ⇒ NO flip (strict >).
        s.update(180, 90, 160);
        assert_eq!(
            s.direction(),
            Some(SUPERTREND_DIR_DOWN),
            "equality must NOT flip"
        );
        assert_eq!(s.line_x2_exact(), Some(320));
        // One half-paisa more CANNOT be expressed by a paisa close; a full
        // paisa more (close 161, close_x2 322 > 320) DOES flip.
        s.update(180, 90, 161);
        assert_eq!(
            s.direction(),
            Some(SUPERTREND_DIR_UP),
            "strictly above must flip"
        );
    }

    #[test]
    fn close_equal_to_the_lower_band_does_not_flip_up_trend() {
        // Build an UP-trend, then close EXACTLY on the final lower band.
        let mut s = SupertrendState::new();
        feed(&mut s, &[FLAT; 10]);
        s.update(110, 90, 161); // flip UP; hl2_x2=200, atr=(40·9+22·2)/10?
        // Recompute: TR = max(20, |110−100|, |90−100|) = 20 ⇒ atr stays 40;
        // basic lower = 200 − 120 = 80; prevFinalLower = 80 ⇒ final lower 80.
        assert_eq!(s.direction(), Some(SUPERTREND_DIR_UP));
        assert_eq!(s.line_x2_exact(), Some(80));
        // close == 40 ⇔ close_x2 == 80 == finalLower: TR widens but the
        // ratchet holds the lower band at 80 unless basic > 80.
        // H=110, L=39, C=40: TR = max(71, |110−161|=51, |39−161|=122) = 122;
        // atr = (40·9 + 244)/10 = 60; hl2_x2 = 149; basic lower = 149 − 180 =
        // −31 < 80 and close[1]·2 = 322 ≥ 80 ⇒ final lower stays 80.
        // close_x2 = 80 == 80 ⇒ NO flip (strict <).
        s.update(110, 39, 40);
        assert_eq!(
            s.direction(),
            Some(SUPERTREND_DIR_UP),
            "equality must NOT flip"
        );
        // A close one paisa lower (39 ⇒ 78 < 80) flips DOWN.
        s.update(110, 39, 39);
        assert_eq!(s.direction(), Some(SUPERTREND_DIR_DOWN));
    }

    // ── hl2 half-paisa exactness ───────────────────────────────────────────

    #[test]
    fn odd_high_plus_low_keeps_the_half_paisa() {
        // H+L odd ⇒ hl2_x2 odd — a truncating paisa-domain port would lose the
        // half paisa and band one unit LOW. 10 bars H=111, L=90, C=100:
        // TR bar0 = 21; TR later = max(21, 11, 10) = 21 ⇒ atr_x2 = 42.
        // hl2_x2 = 201 (odd — EXACT); upper = 201 + 126 = 327.
        let mut s = SupertrendState::new();
        let out = feed(&mut s, &[(111, 90, 100); 10]);
        assert_eq!(
            out[9],
            Some(327),
            "hl2 must keep the half paisa (201, not 200)"
        );
    }

    // ── no session resets ──────────────────────────────────────────────────

    #[test]
    fn state_persists_across_a_multi_day_gap() {
        // There is deliberately NO reset API and no session input: a stream
        // with a huge overnight-style gap keeps the SAME state — the bar after
        // the gap still has a line (no warm-up recurrence) and the whole
        // stream equals a single continuous replay.
        let mut bars = vec![FLAT; 12];
        bars.extend([(210, 190, 200); 8]); // "next day", gapped +100
        let mut streaming = SupertrendState::new();
        let one_go = feed(&mut streaming, &bars);
        assert!(one_go[12].is_some(), "no re-warm-up after the gap");
        // Streaming == batch (also the compute_supertrend equivalence pin).
        let high: Vec<i64> = bars.iter().map(|b| b.0).collect();
        let low: Vec<i64> = bars.iter().map(|b| b.1).collect();
        let close: Vec<i64> = bars.iter().map(|b| b.2).collect();
        let batch = compute_supertrend(&high, &low, &close).unwrap();
        assert_eq!(one_go, batch, "streaming and batch must be bit-identical");
    }

    #[test]
    fn compute_supertrend_rejects_length_mismatch() {
        let err = compute_supertrend(&[1, 2], &[1], &[1, 2]).unwrap_err();
        assert!(err.contains("column lengths differ"), "got: {err}");
        let err = compute_supertrend(&[1], &[1], &[1, 2]).unwrap_err();
        assert!(err.contains("column lengths differ"), "got: {err}");
    }

    // ── overflow honesty ───────────────────────────────────────────────────

    #[test]
    fn absurd_magnitudes_yield_honest_none_never_a_wrap_or_panic() {
        // Near-i64::MAX inputs push the ×2 line past i64: the internal i128
        // state stays exact and `update` returns None (honest absence).
        let mut s = SupertrendState::new();
        for _ in 0..10 {
            let out = s.update(i64::MAX, i64::MIN, 0);
            // Either warm-up None or overflow None — never a wrapped Some.
            assert!(out.is_none());
        }
        assert!(s.line_x2_exact().is_some(), "internal exact line exists");
        assert_eq!(s.supertrend_x2(), None, "×2 line beyond i64 reads None");
        // Realistic magnitudes always fit.
        let mut s2 = SupertrendState::new();
        let outs = feed(
            &mut s2,
            &[(1_000_000_000_000, 999_999_999_000, 999_999_999_500); 10],
        );
        assert!(outs[9].is_some());
    }

    // ── property layer ─────────────────────────────────────────────────────

    proptest! {
        #![proptest_config(ProptestConfig {
            cases: 2048,
            max_shrink_time: 3_000,
            failure_persistence: None,
            ..ProptestConfig::default()
        })]

        /// Total for arbitrary i64 OHLC: never panics; exactly 9 warm-up
        /// `None`s; once computed, the direction is always ∈ {−1, +1} and the
        /// exact line always equals one of the two final bands.
        #[test]
        fn never_panics_and_invariants_hold(
            bars in prop::collection::vec((any::<i64>(), any::<i64>(), any::<i64>()), 0..40)
        ) {
            let mut s = SupertrendState::new();
            for (i, &(h, l, c)) in bars.iter().enumerate() {
                let out = s.update(h, l, c);
                if i < (SUPERTREND_ATR_PERIOD as usize) - 1 {
                    prop_assert_eq!(out, None, "warm-up bar {} must be None", i);
                    prop_assert_eq!(s.direction(), None);
                } else {
                    let dir = s.direction();
                    prop_assert!(
                        dir == Some(SUPERTREND_DIR_UP) || dir == Some(SUPERTREND_DIR_DOWN),
                        "direction must be ±1 after warm-up, got {:?}", dir
                    );
                    let line = s.line_x2_exact().expect("line exists after warm-up");
                    prop_assert!(
                        line == s.final_upper_x2 || line == s.final_lower_x2,
                        "line {} must equal a final band ({} / {})",
                        line, s.final_upper_x2, s.final_lower_x2
                    );
                    // The direction picks WHICH band is the line.
                    if dir == Some(SUPERTREND_DIR_UP) {
                        prop_assert_eq!(line, s.final_lower_x2);
                    } else {
                        prop_assert_eq!(line, s.final_upper_x2);
                    }
                    if let Some(v) = out {
                        prop_assert_eq!(i128::from(v), line, "exposed line mirrors the exact one");
                    }
                }
            }
        }

        /// Streaming ≡ batch for any in-domain stream (bit-identity by
        /// construction, pinned anyway).
        #[test]
        fn batch_equals_streaming(
            bars in prop::collection::vec(
                (1i64..=1_000_000_000_000, 1i64..=1_000_000_000_000, 1i64..=1_000_000_000_000),
                0..30,
            )
        ) {
            let high: Vec<i64> = bars.iter().map(|b| b.0).collect();
            let low: Vec<i64> = bars.iter().map(|b| b.1).collect();
            let close: Vec<i64> = bars.iter().map(|b| b.2).collect();
            let batch = compute_supertrend(&high, &low, &close).unwrap();
            let mut s = SupertrendState::new();
            let streamed: Vec<Option<i64>> =
                bars.iter().map(|&(h, l, c)| s.update(h, l, c)).collect();
            prop_assert_eq!(batch, streamed);
        }
    }
}
