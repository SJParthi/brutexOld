//! The PURE decision core (no pyo3): one forward scan over the raw f64/i64
//! bar columns, transcribing — block by block, cited per block — the DECISION
//! plane of `brutex/feed/replay.py:333-376` (engine-boundary positivity,
//! DEC-STORED-DATA-VALIDATION-001) and `brutex/data/bad_tick.py:178-227`
//! (the in-session bad-tick screen, DEC-SWEEP-DATA-EDGE-GUARDS-001).
//!
//! ## What this module deliberately does NOT do
//!
//! * It NEVER formats a float, NEVER builds a Decimal/string/datetime, and
//!   NEVER returns a value that reaches hashed/logged/raised output — the
//!   outcome is INTEGERS ONLY (row indices + counts). Python selects rows and
//!   builds every `CandleBar` / error message / WARN record itself with the
//!   verbatim existing expressions over the SAME `to_pylist()` objects, so
//!   the `bxsnap2` run-identity bytes (DEC-BXSNAP2-PARITY-GOLDEN-001) have
//!   nothing to drift against.
//! * It does NOT pre-validate the `CandleBar` constructor invariants
//!   (ordering / volume / tz): kept rows go through the real Python
//!   constructor, so its exception behavior can never fork.
//! * It does NOT resolve environment flags: `screen_enabled` is the
//!   Python-resolved `bad_tick_screen_enabled()` verdict; `max_ratio` is the
//!   caller-passed `bad_tick.BAD_TICK_MAX_RATIO` (a parameter so cargo tests
//!   can drive custom ratios exactly like the Python unit suite).
//!
//! ## IEEE-754 fidelity
//!
//! Every comparison transcribes the exact CPython float expression:
//! positivity is `v <= 0.0` (NaN passes — `nan <= 0` is false in both
//! languages; `-0.0` refuses — `-0.0 <= 0.0` is true in both); the bad-tick
//! predicate is the verbatim MULTIPLY-form
//! `close >= prev * max_ratio || close * max_ratio <= prev`
//! (`bad_tick.py:131` — never division, never reordered; two explicit f64
//! multiplies + compares; Rust does not contract float ops into FMA, so the
//! intermediate products are bit-identical to CPython's C-double multiplies).
//! A NaN close is KEPT and poisons the baseline (all subsequent comparisons
//! false); `+inf` skips as an up-spike, including the `prev * 4.0 → +inf`
//! overflow arm (`close >= +inf` only for `close == +inf`).

/// Fixed IST offset (+05:30) in microseconds. Asia/Kolkata has been uniformly
/// +05:30 for every instant since 1945 (no DST — CLAUDE.md §6/§7 pin), so for
/// all `epoch >= 0` the civil IST date equals the UTC epoch shifted by this
/// constant — the [`MAX_SAFE_TS_US`] abstain gate keeps the kernel inside
/// that provable window.
pub const IST_OFFSET_US: i64 = 19_800_000_000;

/// One civil day in microseconds.
pub const DAY_US: i64 = 86_400_000_000;

/// Exclusive upper bound of the safe timestamp envelope:
/// 9999-12-31T00:00:00Z in epoch microseconds. Below this bound
/// `ts + 05:30` stays clear of Python's `datetime.max`, so the oracle's
/// `astimezone(IST)` cannot overflow; at/above it (or below epoch 0) the
/// kernel ABSTAINS (returns `Ok(None)`) and the caller degrades to the
/// Python oracle, which produces any genuine `OverflowError` itself.
pub const MAX_SAFE_TS_US: i64 = 253_402_214_400_000_000;

/// One owed bad-tick WARN, as integer coordinates only:
/// `(emit_at_row, session_first_row, skipped, first_prev_row, first_skip_row)`.
///
/// * `emit_at_row` — the row at whose processing the flush fires (the first
///   row of the NEXT session — always itself a KEPT row), or `== n_rows` for
///   the `finish()` flush (present only on a clean, no-stop scan).
/// * `session_first_row` — first row of the FLUSHED session (Python re-derives
///   the ISO session date from ITS OWN datetime object at this index).
/// * `skipped` — count of skipped bars in the flushed session (`>= 1`).
/// * `first_prev_row` — row of the last KEPT close before the session's first
///   skip (Python logs `closes[first_prev_row]` — the same float object the
///   oracle's `first_prev_close` would carry).
/// * `first_skip_row` — the first skipped row of the session.
pub type WarnEvent = (usize, usize, usize, usize, usize);

/// The integers-only outcome of one scan.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScanOutcome {
    /// Rows Python must materialize, ascending, in input order.
    pub kept_rows: Vec<usize>,
    /// Owed bad-tick WARNs, ascending by `emit_at_row` (see [`WarnEvent`]).
    pub warn_events: Vec<WarnEvent>,
    /// `Some((row, field))` when the scan STOPPED at a non-positive price:
    /// `field` is 0/1/2/3 = open/high/low/close (the `replay.py:341` check
    /// order — the FIRST offending field is named). When `Some`:
    /// `kept_rows` / `warn_events` contain ONLY entries strictly before
    /// `row` — NO boundary flush at `row` is recorded (positivity precedes
    /// `keep()` in the oracle) and NO finish flush exists.
    pub positivity_stop: Option<(usize, usize)>,
}

/// Scan the raw bar columns and return the decisions, or abstain.
///
/// * `ts_us` — epoch MICROSECONDS UTC; MUST be `[]` when
///   `screen_enabled == false` (the oracle never touches timestamps with the
///   screen off — it calls `astimezone` only inside the screen consult).
/// * `opens` / `highs` / `lows` / `closes` — raw f64 lake values (the same
///   `to_pylist()` objects Python keeps for materialization).
/// * `screen_enabled` — the Python-resolved `bad_tick_screen_enabled()`
///   verdict (the env law never crosses the FFI).
/// * `max_ratio` — `bad_tick.BAD_TICK_MAX_RATIO` (4.0) in production.
///
/// Returns:
/// * `Err(_)` — column-length precondition violated (the caller's bug; the
///   PyO3 edge maps this to `ValueError`, and the Python seam's defensive
///   belt degrades to the oracle).
/// * `Ok(None)` — ABSTAIN: some consulted `ts_us` lies outside
///   `[0, MAX_SAFE_TS_US)`; the caller degrades the WHOLE call to the oracle.
/// * `Ok(Some(outcome))` — the decisions (see [`ScanOutcome`]).
pub fn scan_bars(
    ts_us: &[i64],
    opens: &[f64],
    highs: &[f64],
    lows: &[f64],
    closes: &[f64],
    screen_enabled: bool,
    max_ratio: f64,
) -> Result<Option<ScanOutcome>, String> {
    let n = opens.len();
    if highs.len() != n || lows.len() != n || closes.len() != n {
        return Err(format!(
            "scan_bars: price column lengths diverge (open={}, high={}, low={}, close={})",
            opens.len(),
            highs.len(),
            lows.len(),
            closes.len()
        ));
    }
    if screen_enabled {
        if ts_us.len() != n {
            return Err(format!(
                "scan_bars: ts_us length {} != price length {n} with screen_enabled",
                ts_us.len()
            ));
        }
    } else if !ts_us.is_empty() {
        return Err(format!(
            "scan_bars: ts_us must be empty when screen_enabled is false, got length {}",
            ts_us.len()
        ));
    }

    let mut kept: Vec<usize> = Vec::new();
    let mut warns: Vec<WarnEvent> = Vec::new();

    // BadTickScreen state (`bad_tick.py:163-176`): session key, last KEPT
    // in-session close (+ its row, so Python can log the same float object),
    // per-session skip tally, first offender of the session.
    let mut cur_session: Option<i64> = None;
    let mut cur_session_first_row: usize = 0;
    let mut prev_close: Option<f64> = None;
    let mut prev_row: usize = 0;
    let mut skipped: usize = 0;
    let mut first_offender: Option<(usize, usize)> = None;

    for i in 0..n {
        // 1. Engine-boundary POSITIVITY (`replay.py:341-348`), checked FIRST
        //    for every row — before the screen ever sees it — in the fixed
        //    field order open → high → low → close; the FIRST offender stops
        //    the scan. `v <= 0.0`: NaN passes, `-0.0` refuses (IEEE-identical
        //    to CPython's `price <= 0`).
        for (field, v) in [opens[i], highs[i], lows[i], closes[i]]
            .into_iter()
            .enumerate()
        {
            if v <= 0.0 {
                // Positivity precedes `keep()` in the oracle, so the pending
                // session flush is NOT recorded and no finish flush exists —
                // the oracle raises before either could fire.
                return Ok(Some(ScanOutcome {
                    kept_rows: kept,
                    warn_events: warns,
                    positivity_stop: Some((i, field)),
                }));
            }
        }

        if screen_enabled {
            let t = ts_us[i];
            // 2. Lazy ABSTAIN gate — before row i's session math. Outside
            //    `[0, MAX_SAFE_TS_US)` the fixed +05:30 day-ordinal law is
            //    not provably identical to `astimezone(IST).date()`
            //    (pre-1970 Kolkata offsets; `datetime.max` overflow), so the
            //    whole call degrades to the oracle.
            if !(0..MAX_SAFE_TS_US).contains(&t) {
                return Ok(None);
            }
            // 3. IST session key: the civil day ordinal of `t + 05:30`
            //    (`replay.py:354` `ts.astimezone(IST).date()`). The add
            //    cannot overflow: `t < MAX_SAFE_TS_US ≈ 2.54e17` and the
            //    offset is ~1.98e10, far below `i64::MAX`.
            debug_assert!(t.checked_add(IST_OFFSET_US).is_some());
            let session_day = (t + IST_OFFSET_US).div_euclid(DAY_US);

            // Session boundary (`bad_tick.py:188-196`): ANY inequality —
            // including a backwards date flip — flushes the owed WARN for
            // the session just left (anchored at THIS row, the next
            // session's first row) and resets the per-session state.
            if cur_session != Some(session_day) {
                if skipped > 0
                    && let Some((fpr, fsr)) = first_offender
                {
                    warns.push((i, cur_session_first_row, skipped, fpr, fsr));
                }
                cur_session = Some(session_day);
                cur_session_first_row = i;
                prev_close = None;
                skipped = 0;
                first_offender = None;
            }

            // The verbatim multiply-form predicate (`bad_tick.py:131`):
            // `close >= prev * max_ratio or close * max_ratio <= prev` —
            // NEVER division, NEVER reordered. The first bar of a session is
            // exempt (`prev_close` is None after the reset).
            if let Some(p) = prev_close {
                let c = closes[i];
                if c >= p * max_ratio || c * max_ratio <= p {
                    // Skip (`bad_tick.py:198-206`): count it, capture the
                    // first offender once, and deliberately do NOT advance
                    // the baseline — the next bar is judged against the
                    // last KEPT good close.
                    skipped += 1;
                    if first_offender.is_none() {
                        first_offender = Some((prev_row, i));
                    }
                    continue;
                }
            }
            prev_close = Some(closes[i]);
            prev_row = i;
        }

        kept.push(i);
    }

    // 4. Clean end of input (`replay.py:374-375` `screen.finish()`): flush
    //    the final session's owed WARN, anchored at `n`. Mirrors `_flush`'s
    //    own `skipped > 0 and first_offender is not None` guard verbatim.
    if skipped > 0
        && let Some((fpr, fsr)) = first_offender
    {
        warns.push((n, cur_session_first_row, skipped, fpr, fsr));
    }

    Ok(Some(ScanOutcome {
        kept_rows: kept,
        warn_events: warns,
        positivity_stop: None,
    }))
}
