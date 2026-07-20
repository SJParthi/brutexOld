#![no_main]
//! Fuzz target for the pure decision core `brutex_feed::scan_bars`
//! (DEC-FEED-RUST-CORE-001) — the positivity + bad-tick forward scan the
//! engine read boundary consumes behind `BRUTEX_FEED_RUST`.
//!
//! On arbitrary bytes it decodes a bar-column set — RAW i64 timestamps
//! (whole i64 range, so the abstain gate is hammered) and RAW f64 bit
//! patterns for the four prices (NaN / ±inf / -0.0 / subnormals all reachable
//! by construction) plus a byte-chosen `screen_enabled` and a raw-f64
//! `max_ratio` — and asserts:
//!
//! 1. **Total function** — never panics for ANY well-lengthed input (the
//!    only `Err` arm is the length precondition, which this harness never
//!    violates by construction; overflow-checks stay armed via the profile).
//! 2. **Output shape invariants** — kept rows strictly ascending and in
//!    range; warn anchors strictly ascending with `skipped >= 1`,
//!    `first_prev_row < first_skip_row < n`; a positivity stop truncates
//!    (nothing recorded at/after the stop row, no finish event).
//! 3. **Determinism / purity** — a second call over the same input returns
//!    a bit-identical outcome.

use libfuzzer_sys::fuzz_target;

use brutex_feed::{MAX_SAFE_TS_US, scan_bars};

fn read_u64(data: &[u8], off: usize) -> u64 {
    let mut b = [0u8; 8];
    for (k, slot) in b.iter_mut().enumerate() {
        *slot = data.get(off + k).copied().unwrap_or(0);
    }
    u64::from_le_bytes(b)
}

fuzz_target!(|data: &[u8]| {
    if data.len() < 2 {
        return;
    }
    let screen_enabled = data[0] & 1 == 1;
    // Raw-f64 ratio: NaN/inf/negative ratios must all be total too.
    let max_ratio = f64::from_bits(read_u64(data, 1));

    // Each bar consumes 40 bytes (ts + 4 prices) after the 9-byte header;
    // cap the stream so a run stays fast. saturating_sub: a 2..9-byte input
    // must yield ZERO bars, not a usize underflow — the exact 2-byte
    // `\n\n` crash CI's first smoke run caught (an overflow-checks panic in
    // the HARNESS arithmetic, not the kernel; regression seed committed at
    // fuzz/seeds/fuzz_scan/crash-2-byte-header-underflow).
    let n_bars = (data.len().saturating_sub(9) / 40).min(64);
    let mut ts: Vec<i64> = Vec::with_capacity(n_bars);
    let mut opens: Vec<f64> = Vec::with_capacity(n_bars);
    let mut highs: Vec<f64> = Vec::with_capacity(n_bars);
    let mut lows: Vec<f64> = Vec::with_capacity(n_bars);
    let mut closes: Vec<f64> = Vec::with_capacity(n_bars);
    for i in 0..n_bars {
        let off = 9 + i * 40;
        ts.push(read_u64(data, off) as i64);
        opens.push(f64::from_bits(read_u64(data, off + 8)));
        highs.push(f64::from_bits(read_u64(data, off + 16)));
        lows.push(f64::from_bits(read_u64(data, off + 24)));
        closes.push(f64::from_bits(read_u64(data, off + 32)));
    }
    let ts_arg: &[i64] = if screen_enabled { &ts } else { &[] };

    let first = scan_bars(
        ts_arg,
        &opens,
        &highs,
        &lows,
        &closes,
        screen_enabled,
        max_ratio,
    )
    .expect("well-lengthed input never errs");
    let second = scan_bars(
        ts_arg,
        &opens,
        &highs,
        &lows,
        &closes,
        screen_enabled,
        max_ratio,
    )
    .expect("well-lengthed input never errs");
    assert_eq!(first, second, "determinism");

    let Some(out) = first else {
        // Abstain must be justified by an out-of-envelope timestamp.
        assert!(screen_enabled);
        assert!(ts.iter().any(|&t| !(0..MAX_SAFE_TS_US).contains(&t)));
        return;
    };

    let n = n_bars;
    let mut prev: i64 = -1;
    for &k in &out.kept_rows {
        assert!((k as i64) > prev && k < n, "kept rows ascending + in range");
        prev = k as i64;
    }
    let mut prev_emit: i64 = -1;
    for &(emit, sfr, skipped, fpr, fsr) in &out.warn_events {
        assert!((emit as i64) > prev_emit, "warn anchors ascending");
        assert!(emit <= n && sfr < n && skipped >= 1 && fpr < fsr && fsr < n);
        prev_emit = emit as i64;
    }
    if let Some((row, field)) = out.positivity_stop {
        assert!(row < n && field <= 3);
        assert!(out.kept_rows.iter().all(|&k| k < row));
        assert!(out.warn_events.iter().all(|&(emit, ..)| emit < row));
    }
});
