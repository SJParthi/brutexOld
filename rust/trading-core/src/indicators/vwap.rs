//! Session-anchored intraday futures VWAP — the stateful indicator the Group-10
//! `close_above_vwap` / `close_below_vwap` conditions consume (ADR-0012 D12).
//!
//! THE single VWAP source: the hand-synced bit-for-bit Python twin that used
//! to live at `brutex/indicators/vwap.py::VwapState` was DELETED by
//! DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 (`docs/MIGRATION.md` #1535) —
//! BruteX drives this state via the PyO3 pyclass. Running
//! `cum(price·vol) / cum(vol)`, anchored per trading session:
//!
//! * **reset at 09:15 IST** each trading day (the session open — a new IST date
//!   OR a backwards/zero `tod_seconds` step starts a fresh session);
//! * **excludes the 09:00-09:15 pre-open auction volume** (the caller passes
//!   `is_pre_open = true` for those bars; they are skipped entirely — CLAUDE.md
//!   §7);
//! * **resets on contract roll** (a change of the engine-injected `session_key`
//!   — per ADR-0012 D11; no VWAP ever spans a roll boundary);
//! * is **NA / `None` on the 1day timeframe** — the caller never feeds a 1day bar
//!   to this state (the evaluator's `vwap_paisa` is `None` there), so a session
//!   VWAP is never degenerate-with-one-bar.
//!
//! ## Overflow safety (the one genuine numeric hazard in PR-2)
//!
//! `cum(price·vol)` over a full session can exceed `i64` (price paisa × cumulative
//! volume). The accumulators are therefore **`i128`**; the VWAP is snapped back to
//! `i64` paisa at read with truncating integer division (`cum_pv / cum_vol`,
//! matching Python's `//` on non-negative operands — volume and price are both
//! non-negative for a real futures bar). The deleted Python twin used unbounded
//! ints and the same floor division, so the two were bit-identical for
//! non-negative operands — the identity the frozen twin-state golden fixture
//! still replays (Rust-only) against this single source.
//!
//! O(1) per bar: two `i128` adds (one multiply) + one divide. No growing history.

/// Running session-anchored VWAP accumulator (int64 paisa in, int64 paisa out).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VwapState {
    /// The current session's contract key (engine-injected). A change resets.
    session_key: u64,
    /// IST date ordinal of the current session (a change resets the session).
    session_day: i64,
    /// Σ(typical_price · volume) for the live session — `i128` to never overflow.
    cum_pv: i128,
    /// Σ(volume) for the live session.
    cum_vol: i128,
    /// True once a backwards/zero `tod_seconds` step or a new day/key reset means
    /// the running sums describe the CURRENT session only.
    warm: bool,
    /// The last `tod_seconds` seen, so a backwards step (a new session that
    /// happens to share the same `session_day` ordinal in a test) also resets.
    last_tod: u32,
}

impl Default for VwapState {
    fn default() -> Self {
        Self::new()
    }
}

impl VwapState {
    /// A fresh, cold (un-warmed) VWAP accumulator.
    #[must_use]
    pub fn new() -> Self {
        VwapState {
            session_key: 0,
            session_day: i64::MIN,
            cum_pv: 0,
            cum_vol: 0,
            warm: false,
            last_tod: 0,
        }
    }

    /// Reset the running sums to start a fresh session.
    fn start_session(&mut self, session_key: u64, session_day: i64) {
        self.session_key = session_key;
        self.session_day = session_day;
        self.cum_pv = 0;
        self.cum_vol = 0;
        self.warm = false;
    }

    /// Feed one bar; returns the session VWAP in int64 paisa, or `None` while the
    /// session has no usable (post-09:15, non-zero) volume yet.
    ///
    /// `typical_paisa` is the bar's typical price (HLC3 — the caller computes it,
    /// matching the movers turnover proxy). `volume` is the
    /// bar's traded volume. `is_pre_open` excludes the 09:00-09:15 auction.
    /// `session_key` is the engine-injected contract key (changes on roll).
    /// `session_day` is the bar's IST date ordinal. `tod_seconds` is the bar's IST
    /// time-of-day (used to detect a same-key, same-day-ordinal session restart in
    /// tests — a backwards step starts a new session).
    ///
    /// O(1): one reset check + (for a counted bar) one `i128` multiply-add + one
    /// divide.
    pub fn update(
        &mut self,
        typical_paisa: i64,
        volume: i64,
        is_pre_open: bool,
        session_key: u64,
        session_day: i64,
        tod_seconds: u32,
    ) -> Option<i64> {
        // A new contract (roll), a new IST day, or a backwards/equal-then-earlier
        // time-of-day step (a fresh session that re-opens at 09:15) all start a
        // fresh session. The day-ordinal change is the primary trigger; the
        // tod-rewind catches a same-day restart in a synthetic test stream.
        let new_session = session_key != self.session_key
            || session_day != self.session_day
            || (self.warm && tod_seconds < self.last_tod);
        if new_session {
            self.start_session(session_key, session_day);
        }
        self.last_tod = tod_seconds;

        // The 09:00-09:15 pre-open auction is EXCLUDED — its volume never enters
        // the VWAP (ADR-0012 D12 / CLAUDE.md §7). A zero-volume bar contributes
        // nothing either (and never warms the state).
        if is_pre_open || volume <= 0 {
            return self.current();
        }

        self.cum_pv += i128::from(typical_paisa) * i128::from(volume);
        self.cum_vol += i128::from(volume);
        self.warm = true;
        self.current()
    }

    /// The current session VWAP (paisa), or `None` if no volume has been counted.
    #[must_use]
    pub fn current(&self) -> Option<i64> {
        if self.cum_vol <= 0 {
            return None;
        }
        // Truncating division on non-negative operands == Python `//`. The result
        // is a real price magnitude (≤ i64::MAX for any real futures bar), so the
        // `as i64` narrowing is exact here.
        Some((self.cum_pv / self.cum_vol) as i64)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const KEY: u64 = 1;
    const DAY: i64 = 100;

    #[test]
    #[should_panic(expected = "overflow")]
    fn native_accumulator_overflows_on_huge_unbounded_inputs() {
        // DEC-ADVERSARIAL-HARDENING-001 FIX 1 — the DEFECT this documents: the native
        // `cum_pv += typical_paisa·volume` has NO magnitude guard, so a few near-`i64::MAX`
        // products overflow the `i128` accumulator. Under the RELEASE profile (`panic =
        // "abort"`) this aborts the interpreter (SIGABRT) — uncatchable by try/except. The
        // PyO3 boundary (`PyVwapState::update`) now validates the inputs against
        // `MAX_BAR_PAISA` FIRST and raises a catchable `ValueError`, so this native overflow
        // is unreachable from Python. (Dev profile unwinds, so we can assert the panic.)
        let mut v = VwapState::new();
        let huge = i64::MAX;
        for i in 0..3 {
            v.update(huge, huge, false, KEY, DAY, 9 * 3600 + 16 * 60 + i);
        }
    }

    #[test]
    fn warmup_returns_none_until_first_volume() {
        let mut v = VwapState::new();
        // First bar is pre-open auction → excluded → still None.
        assert_eq!(
            v.update(10_000, 500, true, KEY, DAY, 9 * 3600 + 5 * 60),
            None
        );
        // A zero-volume post-open bar still leaves it cold.
        assert_eq!(
            v.update(10_000, 0, false, KEY, DAY, 9 * 3600 + 15 * 60),
            None
        );
        // First real volume warms it.
        assert_eq!(
            v.update(10_000, 100, false, KEY, DAY, 9 * 3600 + 16 * 60),
            Some(10_000)
        );
    }

    #[test]
    fn vwap_is_volume_weighted() {
        let mut v = VwapState::new();
        // 100 @ 10000 then 300 @ 20000 → (100*10000 + 300*20000)/400 = 17500.
        assert_eq!(
            v.update(10_000, 100, false, KEY, DAY, 9 * 3600 + 16 * 60),
            Some(10_000)
        );
        assert_eq!(
            v.update(20_000, 300, false, KEY, DAY, 9 * 3600 + 17 * 60),
            Some(17_500)
        );
    }

    #[test]
    fn auction_volume_is_excluded() {
        let mut v = VwapState::new();
        // A huge pre-open auction print must NOT bias the session VWAP.
        v.update(99_999, 1_000_000, true, KEY, DAY, 9 * 3600 + 5 * 60);
        assert_eq!(
            v.update(10_000, 100, false, KEY, DAY, 9 * 3600 + 16 * 60),
            Some(10_000)
        );
    }

    #[test]
    fn resets_on_new_day() {
        let mut v = VwapState::new();
        v.update(50_000, 100, false, KEY, DAY, 9 * 3600 + 16 * 60);
        // New IST day → fresh session, old sums discarded.
        assert_eq!(
            v.update(10_000, 100, false, KEY, DAY + 1, 9 * 3600 + 16 * 60),
            Some(10_000)
        );
    }

    #[test]
    fn resets_on_contract_roll() {
        let mut v = VwapState::new();
        v.update(50_000, 100, false, KEY, DAY, 9 * 3600 + 16 * 60);
        // Same day, NEW contract key (roll) → fresh session.
        assert_eq!(
            v.update(10_000, 100, false, KEY + 1, DAY, 9 * 3600 + 17 * 60),
            Some(10_000)
        );
    }

    #[test]
    fn resets_on_tod_rewind_same_day() {
        let mut v = VwapState::new();
        v.update(50_000, 100, false, KEY, DAY, 14 * 3600);
        // A backwards time-of-day step (a re-opened session) resets even with the
        // same key+day ordinal.
        assert_eq!(
            v.update(10_000, 100, false, KEY, DAY, 9 * 3600 + 16 * 60),
            Some(10_000)
        );
    }

    #[test]
    fn i128_accumulator_does_not_overflow_i64() {
        let mut v = VwapState::new();
        // A full session of large futures prints: price 8e6 paisa × volume 1e11 →
        // 8e17 per bar; over 50 bars Σ(price·vol) = 4e19, comfortably PAST
        // i64::MAX (9.2e18) — i64 accumulators would wrap/panic here, i128 do not.
        // The VWAP read must still equal the (constant) price (no precision loss).
        let price = 8_000_000i64;
        for k in 0..50 {
            let got = v.update(
                price,
                100_000_000_000,
                false,
                KEY,
                DAY,
                9 * 3600 + 16 * 60 + k,
            );
            assert_eq!(got, Some(price));
        }
        // cum_pv has far exceeded i64::MAX (8e17 per bar × 50 = 4e19 > 9.2e18).
        assert!(v.cum_pv > i128::from(i64::MAX));
    }

    #[test]
    fn default_equals_new() {
        assert_eq!(VwapState::default(), VwapState::new());
    }
}
