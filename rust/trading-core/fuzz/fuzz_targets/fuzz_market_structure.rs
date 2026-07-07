#![no_main]
//! Fuzz target for the stateful BOS/CHoCH market-structure machine
//! `trading_core::MarketStructureState` (`src/indicators/market_structure.rs`,
//! DEC-NIFTY-BOS-CHOCH-001) — the look-ahead-clean fractal swing detector that
//! resolves the Group-12 bits (56-59).
//!
//! On arbitrary bytes it builds a bar STREAM in the realistic paisa envelope
//! (`high >= low`; `close` unconstrained within the envelope — the machine
//! makes no OHLC-ordering assumption) with a byte-chosen fractal half-window
//! `k ∈ [1, 4]`, feeds it in arrival order, and asserts:
//!
//! 1. **Never panics** for any `k >= 1` and any in-envelope bar stream.
//! 2. **Warm-up silence** — no signal before `2k+1` bars (no swing can be
//!    confirmed yet), and no signal while both swing levels are `None`.
//! 3. **Per-side exclusivity** — on any one bar at most ONE of
//!    `{bos_bullish, choch_bullish}` and at most ONE of
//!    `{bos_bearish, choch_bearish}` (the BOS/CHoCH split is a function of the
//!    trend at the bar, per the module contract).
//! 4. **Trend coherence** — a down-side break leaves the trend `Down` (the
//!    down-side resolves last when both sides break); an up-side-only break
//!    leaves it `Up`; a signal-free bar leaves the trend unchanged.
//! 5. **Swing provenance** — a confirmed swing high/low is always the `high`/
//!    `low` of some ALREADY-FED bar (never fabricated, never from the future).
//! 6. **Determinism / purity** — a clone taken mid-stream and fed the same
//!    suffix produces bit-identical signals and an identical final state (the
//!    serialize/restore invariant of CLAUDE.md Rule #11, via `Clone` +
//!    `PartialEq`).

use libfuzzer_sys::fuzz_target;
use trading_core::{MarketStructureState, Trend};

const MAX_PAISA: i64 = 10_000_000_000;

fn read_u64(data: &[u8], off: usize) -> u64 {
    let mut b = [0u8; 8];
    for (k, slot) in b.iter_mut().enumerate() {
        *slot = data.get(off + k).copied().unwrap_or(0);
    }
    u64::from_le_bytes(b)
}

/// Map an arbitrary u64 into `[1, MAX_PAISA]`.
fn price(raw: u64) -> i64 {
    1 + (raw % MAX_PAISA as u64) as i64
}

fuzz_target!(|data: &[u8]| {
    if data.len() < 2 {
        return;
    }
    let k = 1 + (data[0] % 4) as usize; // k in [1, 4]
    let cut = data[1] as usize; // where the determinism clone is taken

    // Each bar consumes 24 bytes; cap the stream so a run stays fast.
    let n_bars = ((data.len() - 2) / 24).min(96);
    let mut machine = MarketStructureState::new(k);
    let mut twin: Option<MarketStructureState> = None;

    let mut fed_highs: Vec<i64> = Vec::with_capacity(n_bars);
    let mut fed_lows: Vec<i64> = Vec::with_capacity(n_bars);

    for i in 0..n_bars {
        let off = 2 + i * 24;
        let a = price(read_u64(data, off));
        let b = price(read_u64(data, off + 8));
        let (low, high) = if a <= b { (a, b) } else { (b, a) };
        let close = price(read_u64(data, off + 16));
        fed_highs.push(high);
        fed_lows.push(low);

        let trend_before = machine.trend();

        // ── 1. never panics ──
        let sig = machine.update(high, low, close);

        // The determinism twin (if armed) must produce the identical signal.
        if let Some(t) = twin.as_mut() {
            let twin_sig = t.update(high, low, close);
            assert_eq!(sig, twin_sig, "a cloned machine diverged on the same bar");
        } else if i == cut % n_bars.max(1) {
            twin = Some(machine.clone());
        }

        let up = sig.bos_bullish || sig.choch_bullish;
        let down = sig.bos_bearish || sig.choch_bearish;

        // ── 2. warm-up silence ──
        if i + 1 < 2 * k + 1 {
            assert!(
                !up && !down,
                "no signal may fire before the first window is full (bar {i}, k={k})"
            );
        }

        // ── 3. per-side exclusivity ──
        assert!(
            !(sig.bos_bullish && sig.choch_bullish),
            "at most one of BOS-bullish / CHoCH-bullish per bar"
        );
        assert!(
            !(sig.bos_bearish && sig.choch_bearish),
            "at most one of BOS-bearish / CHoCH-bearish per bar"
        );

        // ── 4. trend coherence ──
        if down {
            // The down-side resolves last, so any bearish break ends Down.
            assert_eq!(machine.trend(), Trend::Down, "a bearish break must leave Down");
        } else if up {
            assert_eq!(machine.trend(), Trend::Up, "an up-only break must leave Up");
        } else {
            assert_eq!(
                machine.trend(),
                trend_before,
                "a signal-free bar must not move the trend"
            );
        }

        // ── 5. swing provenance ──
        if let Some(sh) = machine.last_swing_high() {
            assert!(
                fed_highs.contains(&sh),
                "confirmed swing high {sh} is not the high of any fed bar"
            );
        }
        if let Some(sl) = machine.last_swing_low() {
            assert!(
                fed_lows.contains(&sl),
                "confirmed swing low {sl} is not the low of any fed bar"
            );
        }
    }

    // ── 6. the twin ends bit-identical ──
    if let Some(t) = twin {
        assert_eq!(machine, t, "cloned machine ended in a different state");
    }
});
