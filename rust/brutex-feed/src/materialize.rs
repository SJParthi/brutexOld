//! The STRICT decision scan — `DEC-FEED-RUST-MATERIALIZE-002`'s kernel half.
//!
//! [`scan_bars_strict`] is the wheel-REQUIRED successor of
//! [`crate::scan::scan_bars`]: the SAME forward scan (positivity refusal
//! order + the in-session bad-tick screen), PLUS the engine-boundary
//! FINITENESS gate of `DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001` folded
//! IN-SCAN — per row, per field in the fixed order open → high → low →
//! close, finiteness is checked BEFORE positivity (transcribing
//! `brutex/feed/replay.py`'s deleted oracle loop line by line), and the
//! FIRST offending field stops the scan with a typed stop KIND so Python
//! can raise the verbatim `_nonfinite_error` / `_positivity_error`
//! f-string from its own float objects.
//!
//! Why a NEW module instead of editing [`crate::scan`]: the original
//! `scan_bars` ships frozen behind the retired two-arm seam (its NaN-kept
//! semantics were correct THERE because a Python pre-gate degraded every
//! non-finite column to the oracle before the kernel ever saw it) and the
//! append-only registration law (`DEC-FEED-RUST-CORE-001` §2) forbids
//! restructuring it. The differential proptest
//! (`tests/materialize_tests.rs`) pins `scan_bars_strict` ≡ `scan_bars`
//! over every all-finite input, so the two transcriptions cannot drift on
//! the shared plane.
//!
//! ## What this module deliberately does NOT do (unchanged laws)
//!
//! * INTEGERS ONLY out: kept row indices + warn coordinates + the stop
//!   coordinate. Python builds every `Decimal` / `CandleBar` / error /
//!   WARN itself over the SAME `to_pylist()` objects, so the `bxsnap2`
//!   run-identity bytes have nothing to drift against.
//! * NO constructor pre-validation (ordering / volume / tz): kept rows go
//!   through the real Python constructor.
//! * NO env resolution: `screen_enabled` is the Python-resolved verdict,
//!   `max_ratio` the caller-passed `bad_tick.BAD_TICK_MAX_RATIO`.
//!
//! ## IEEE-754 fidelity
//!
//! Finiteness is `!v.is_finite()` — bit-identical to CPython's
//! `not math.isfinite(price)` (both are the IEEE class predicate: NaN and
//! ±inf are non-finite, `-0.0` and subnormals are finite). Positivity
//! stays the verbatim `v <= 0.0` and the bad-tick predicate stays the
//! verbatim multiply form — see [`crate::scan`]'s header for the full
//! fidelity argument (unchanged here). Because finiteness precedes
//! positivity, no NaN/±inf ever reaches the screen: the `scan.rs`
//! "NaN poisons the baseline" arm is structurally unreachable in the
//! strict scan (exactly as it was unreachable in the Python oracle after
//! `DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001`).

use crate::scan::{DAY_US, IST_OFFSET_US, WarnEvent, ts_in_envelope};

/// Stop kind: the first offending field was a NON-POSITIVE price
/// (`v <= 0.0` — Python raises `_positivity_error`).
pub const STOP_NONPOSITIVE: usize = 0;

/// Stop kind: the first offending field was a NON-FINITE price
/// (NaN / ±inf — Python raises `_nonfinite_error`).
pub const STOP_NONFINITE: usize = 1;

/// The integers-only outcome of one strict scan.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StrictOutcome {
    /// Rows Python must materialize, ascending, in input order.
    pub kept_rows: Vec<usize>,
    /// Owed bad-tick WARNs, ascending by `emit_at_row` (see
    /// [`crate::scan::WarnEvent`] — the coordinate contract is unchanged).
    pub warn_events: Vec<WarnEvent>,
    /// `Some((row, field, kind))` when the scan STOPPED at a corrupt
    /// price: `field` is 0/1/2/3 = open/high/low/close (the fixed check
    /// order — the FIRST offending field is named); `kind` is
    /// [`STOP_NONPOSITIVE`] or [`STOP_NONFINITE`]. When `Some`:
    /// `kept_rows` / `warn_events` contain ONLY entries strictly before
    /// `row` — NO boundary flush at `row` (the field checks precede
    /// `keep()` in the deleted oracle) and NO finish flush exists.
    pub stop: Option<(usize, usize, usize)>,
}

/// Scan the raw bar columns with the strict (finiteness-first) law.
///
/// Contract identical to [`crate::scan::scan_bars`] except:
///
/// * a NON-FINITE price stops the scan with `kind == STOP_NONFINITE`
///   (checked per field BEFORE positivity — the deleted Python oracle's
///   exact interleaving);
/// * the stop coordinate carries the third `kind` element.
///
/// Returns `Err(_)` on a column-length precondition violation (the
/// caller's bug — the PyO3 edge maps it to `ValueError`), `Ok(None)` to
/// ABSTAIN (some consulted `ts_us` outside `[0, MAX_SAFE_TS_US)` — the
/// wheel-required Python seam raises a LOUD envelope refusal, it no
/// longer degrades), else `Ok(Some(outcome))`.
pub fn scan_bars_strict(
    ts_us: &[i64],
    opens: &[f64],
    highs: &[f64],
    lows: &[f64],
    closes: &[f64],
    screen_enabled: bool,
    max_ratio: f64,
) -> Result<Option<StrictOutcome>, String> {
    let n = opens.len();
    if highs.len() != n || lows.len() != n || closes.len() != n {
        return Err(format!(
            "scan_bars_strict: price column lengths diverge (open={}, high={}, low={}, close={})",
            opens.len(),
            highs.len(),
            lows.len(),
            closes.len()
        ));
    }
    if screen_enabled {
        if ts_us.len() != n {
            return Err(format!(
                "scan_bars_strict: ts_us length {} != price length {n} with screen_enabled",
                ts_us.len()
            ));
        }
    } else if !ts_us.is_empty() {
        return Err(format!(
            "scan_bars_strict: ts_us must be empty when screen_enabled is false, got length {}",
            ts_us.len()
        ));
    }

    let mut kept: Vec<usize> = Vec::new();
    let mut warns: Vec<WarnEvent> = Vec::new();

    // BadTickScreen state — the verbatim `scan.rs` transcription (see its
    // block comments; unchanged here).
    let mut cur_session: Option<i64> = None;
    let mut cur_session_first_row: usize = 0;
    let mut prev_close: Option<f64> = None;
    let mut prev_row: usize = 0;
    let mut skipped: usize = 0;
    let mut first_offender: Option<(usize, usize)> = None;

    for i in 0..n {
        // 1. Engine-boundary FINITENESS then POSITIVITY, per field in the
        //    fixed order open → high → low → close — the deleted oracle's
        //    exact per-field interleaving (`replay.py`: `isfinite` raise
        //    BEFORE the `<= 0` raise, both before the screen consult).
        //    The FIRST offender stops the scan with its kind.
        for (field, v) in [opens[i], highs[i], lows[i], closes[i]]
            .into_iter()
            .enumerate()
        {
            let kind = if !v.is_finite() {
                Some(STOP_NONFINITE)
            } else if v <= 0.0 {
                Some(STOP_NONPOSITIVE)
            } else {
                None
            };
            if let Some(kind) = kind {
                // The field checks precede `keep()` in the oracle, so the
                // pending session flush is NOT recorded and no finish
                // flush exists — the oracle raises before either fires.
                return Ok(Some(StrictOutcome {
                    kept_rows: kept,
                    warn_events: warns,
                    stop: Some((i, field, kind)),
                }));
            }
        }

        if screen_enabled {
            let t = ts_us[i];
            // 2. Lazy ABSTAIN gate — outside `[0, MAX_SAFE_TS_US)` the
            //    fixed +05:30 day-ordinal law is not provably identical to
            //    `astimezone(IST).date()` (pre-1970 Kolkata offsets;
            //    `datetime.max` overflow). The predicate is the crate's ONE
            //    shared magnitude law (`scan::ts_in_envelope`).
            if !ts_in_envelope(t) {
                return Ok(None);
            }
            debug_assert!(t.checked_add(IST_OFFSET_US).is_some());
            let session_day = (t + IST_OFFSET_US).div_euclid(DAY_US);

            // Session boundary: ANY inequality flushes the owed WARN for
            // the session just left and resets the per-session state.
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

            // The verbatim multiply-form predicate (`bad_tick.py:131`).
            if let Some(p) = prev_close {
                let c = closes[i];
                if c >= p * max_ratio || c * max_ratio <= p {
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

    // 3. Clean end of input: flush the final session's owed WARN,
    //    anchored at `n` (the `screen.finish()` transcription).
    if skipped > 0
        && let Some((fpr, fsr)) = first_offender
    {
        warns.push((n, cur_session_first_row, skipped, fpr, fsr));
    }

    Ok(Some(StrictOutcome {
        kept_rows: kept,
        warn_events: warns,
        stop: None,
    }))
}
