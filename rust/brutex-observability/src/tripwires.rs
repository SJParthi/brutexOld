//! Laws T2-T6 + T9 — the run-tripwires pure decision laws.
//!
//! `DEC-RUST-OBSERVABILITY-TRIPWIRES-002`: bit-for-bit mirrors of the PURE
//! decision cores of `brutex/observability/run_tripwires.py`'s
//! `TripwireMonitor` (the facade's live parity oracles):
//!
//! * **T2** — [`tripwire_reconcile_counts`]: the D-9 count-reconciliation
//!   identity (`_reconcile_counts`' seven predicates → a violation bitmask,
//!   bit i == the source append order).
//! * **T3** — [`tripwire_throughput_watch`]: the dwell-gated throughput
//!   floor watch (`_watch_throughput`'s peak fold + strict-`<` floor +
//!   `==`-dwell fire edge).
//! * **T4a** — [`tripwire_memory_budget`]: the RSS budget arm
//!   (`budget = fraction × ram`, strict `rss > budget`).
//! * **T4b** — [`tripwire_memory_slope`]: the RSS growth-slope arm +
//!   `_slope_breach`'s dwell counter.
//! * **T5** — [`tripwire_summary_classify`]: the per-value NaN/absurd
//!   screen of `on_tuple_summary` (batch; codes aligned with input order).
//! * **T6** — [`tripwire_stage_missing`]: the stage-prerequisite membership
//!   law (`required - stamps_present` as a per-token missing mask).
//! * **T9** — [`normalized_ru_maxrss_bytes`]: the platform unit law
//!   (`darwin` → bytes verbatim, else KiB × 1024).
//!
//! Python keeps EVERYTHING that reaches logged output: the f-string detail
//! templates, `repr`, `sorted`, the `_in_breach` set + `_edge_fire`/`_clear`
//! (the breach set NEVER crosses the FFI — the crate returns per-key codes
//! from the [`TW_NONE`]/[`TW_CLEAR`]/[`TW_FIRE`] vocabulary and the
//! untouched Python edge law owns membership + re-arm), the SUSPECT latch,
//! the RSS probe, and every enable/interval/state-existence gate.
//!
//! ## Integer-op audit (the IMPL-3 discipline — every non-f64 op enumerated)
//!
//! * T2 bit 4: `evaluated + in_flight` computed in **i128** (two gated i64s
//!   can overflow i64; i128 is lossless — exact ≡ Python's
//!   arbitrary-precision compare; no error arm needed).
//! * T3 / T4b: `below`/`slope_high` `checked_add(1)` →
//!   [`CounterOverflow`] (mapped to `PyValueError` at the FFI edge — a
//!   facade degrade-set member; the facade's input gates make the arm
//!   unreachable in practice, the oracle's unbounded int is the honest
//!   value).
//! * T4b: the two slope diffs computed in **i128** (lossless — an i64
//!   subtraction of two gated i64s can overflow) then `as f64`; the facade
//!   gates both diffs to |diff| ≤ 2^53 so the converts-first `f64` division
//!   agrees exactly with CPython's correctly-rounded `int / int`.
//! * T5: a values/screened length mismatch is refused as
//!   [`SummaryLenMismatch`] (→ `PyValueError` → degrade; the facade builds
//!   both arrays from one iteration, so the arm is a belt).
//! * T9: `ru_maxrss.checked_mul(1024)` → [`RssOverflow`] (→ `PyValueError`
//!   → degrade → the oracle's unbounded-int product).
//! * T5/T6: `usize` indexes bounded by iteration; everything else is IEEE
//!   f64 (never panics). `overflow-checks = true` stays the belt for any
//!   FUTURE unchecked op.

/// The per-key "no decision" code — neither fire nor clear (e.g. a
/// still-below-dwell sample, or the absurd slot of a non-finite value).
pub const TW_NONE: u8 = 0;
/// The per-key CLEAR code — Python `_clear`s (re-arms) the breach key.
pub const TW_CLEAR: u8 = 1;
/// The per-key FIRE-CANDIDATE code — Python `_edge_fire`s the breach key
/// (the once-per-edge decision stays in the untouched `_edge_fire`).
pub const TW_FIRE: u8 = 2;

/// The T3/T4b dwell-counter overflow refusal message (names the field —
/// the facade's degrade-set `ValueError` carries it verbatim).
pub const COUNTER_OVERFLOW_MSG: &str =
    "tripwire dwell counter overflow: counter + 1 exceeds i64::MAX";

/// The T9 KiB→bytes overflow refusal message.
pub const RSS_OVERFLOW_MSG: &str =
    "ru_maxrss normalization overflow: ru_maxrss * 1024 exceeds i64 range";

/// The T5 aligned-arrays refusal message.
pub const SUMMARY_LEN_MISMATCH_MSG: &str =
    "summary screen length mismatch: values and screened must be equal length";

/// A T3/T4b dwell counter `checked_add(1)` overflow (the
/// [`crate::EpisodesOverflow`] template — mapped to `PyValueError` at the
/// FFI edge, a facade degrade-set member; the oracle's unbounded Python int
/// is the honest value).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CounterOverflow;

impl core::fmt::Display for CounterOverflow {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.write_str(COUNTER_OVERFLOW_MSG)
    }
}

impl core::error::Error for CounterOverflow {}

/// The T9 `ru_maxrss * 1024` `checked_mul` overflow (same FFI mapping).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RssOverflow;

impl core::fmt::Display for RssOverflow {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.write_str(RSS_OVERFLOW_MSG)
    }
}

impl core::error::Error for RssOverflow {}

/// A T5 values/screened length mismatch (belt — the facade builds both
/// arrays from ONE iteration; same FFI mapping as the overflow refusals).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SummaryLenMismatch;

impl core::fmt::Display for SummaryLenMismatch {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.write_str(SUMMARY_LEN_MISMATCH_MSG)
    }
}

impl core::error::Error for SummaryLenMismatch {}

/// Law T2 — the D-9 count-reconciliation identity as a violation bitmask.
///
/// Bit i == predicate i in the `_reconcile_counts` append order
/// (`run_tripwires.py:529-553`):
///
/// * bit 0 — `in_flight < 0`
/// * bit 1 — `enumerated != total`
/// * bit 2 — `skipped > evaluated`
/// * bit 3 — `survivors > evaluated`
/// * bit 4 — `evaluated + in_flight > total` (the sum in **i128** — two
///   gated i64s can overflow i64; i128 is lossless, so the compare is
///   exact ≡ Python's arbitrary-precision compare)
/// * bit 5 — `prev_evaluated` present AND `evaluated < prev_evaluated`
/// * bit 6 — `is_done && evaluated != total`
///
/// `is_done` (= `snapshot.state == STATE_DONE`) is computed Python-side —
/// the str compare stays with its import. The `prev_evaluated` update and
/// every detail template stay Python (module docs).
#[allow(clippy::too_many_arguments)]
pub fn tripwire_reconcile_counts(
    in_flight: i64,
    enumerated: i64,
    evaluated: i64,
    skipped: i64,
    survivors: i64,
    total: i64,
    prev_evaluated: Option<i64>,
    is_done: bool,
) -> u32 {
    let mut mask: u32 = 0;
    if in_flight < 0 {
        mask |= 1 << 0;
    }
    if enumerated != total {
        mask |= 1 << 1;
    }
    if skipped > evaluated {
        mask |= 1 << 2;
    }
    if survivors > evaluated {
        mask |= 1 << 3;
    }
    if i128::from(evaluated) + i128::from(in_flight) > i128::from(total) {
        mask |= 1 << 4;
    }
    if matches!(prev_evaluated, Some(prev) if evaluated < prev) {
        mask |= 1 << 5;
    }
    if is_done && evaluated != total {
        mask |= 1 << 6;
    }
    mask
}

/// Law T3 — the dwell-gated throughput floor watch:
/// `(rate, peak, below, fraction, dwell) → (new_peak, new_below, floor, code)`.
///
/// Called only when `fraction != 0` (the Python pre-gate — `_watch_throughput`
/// :574-576 stays Python, preserving the `snapshot.combos_per_sec` read
/// order). The law mirror of :577-596:
///
/// * `new_peak = if rate > peak { rate } else { peak }` — the Python
///   `max(self._peak_rate, rate)` argument-order mirror (a NaN rate keeps
///   the peak; the explicit form is the documented signed-zero/NaN-exact
///   idiom from law A).
/// * `new_peak <= 0.0` → `(new_peak, below, 0.0, TW_NONE)` — the peak is
///   WRITTEN, nothing else happens (neither fire nor clear).
/// * `floor = fraction * new_peak`; `rate < floor` STRICT (exactly-at-floor
///   is NOT below) → `new_below = below + 1` (`checked_add` →
///   [`CounterOverflow`]), code = [`TW_FIRE`] iff `new_below == dwell` (the
///   `==` dwell-EDGE law — NOT `>=`; past-dwell samples are [`TW_NONE`],
///   the once-per-edge decision lives in Python's `_edge_fire`) else
///   [`TW_NONE`]. Else arm: `new_below = 0`, code = [`TW_CLEAR`].
pub fn tripwire_throughput_watch(
    rate: f64,
    peak: f64,
    below: i64,
    fraction: f64,
    dwell: i64,
) -> Result<(f64, i64, f64, u8), CounterOverflow> {
    let new_peak = if rate > peak { rate } else { peak };
    if new_peak <= 0.0 {
        return Ok((new_peak, below, 0.0, TW_NONE));
    }
    let floor = fraction * new_peak;
    if rate < floor {
        let new_below = below.checked_add(1).ok_or(CounterOverflow)?;
        let code = if new_below == dwell { TW_FIRE } else { TW_NONE };
        Ok((new_peak, new_below, floor, code))
    } else {
        Ok((new_peak, 0, floor, TW_CLEAR))
    }
}

/// Law T4a — the RSS budget arm: `(rss, ram_total, fraction) → (budget, code)`.
///
/// Called only when `budget_enabled && ram_total > 0 && !probe_disabled` and
/// AFTER the Python probe succeeded (the enable/probe/ram-unknown gates stay
/// Python — `_watch_memory` :600-622). The law mirror of :624-635:
/// `budget = fraction * (ram_total as f64)`; `(rss as f64) > budget` STRICT
/// → [`TW_FIRE`] else [`TW_CLEAR`] (exactly-at-budget is legal). The
/// i64→f64 conversions are round-to-nearest in both CPython (`float(int)`)
/// and Rust (`as f64`) — identical within the facade's i64 gate.
pub fn tripwire_memory_budget(rss: i64, ram_total: i64, fraction: f64) -> (f64, u8) {
    let budget = fraction * (ram_total as f64);
    let code = if (rss as f64) > budget {
        TW_FIRE
    } else {
        TW_CLEAR
    };
    (budget, code)
}

/// Law T4b — the RSS growth-slope arm + `_slope_breach`'s dwell counter:
/// `(rss, evaluated, prev_rss, prev_evaluated, threshold, high, dwell) →
/// (slope, new_high, code)`.
///
/// Called only when slope-enabled + probe ok + `prev is not None and
/// evaluated > prev_evaluated` (the Python gates; the `_prev_mem` stamp at
/// :645 stays Python). The law mirror of :638-644 + :647-659:
///
/// * `slope = (rss − prev_rss) / (evaluated − prev_evaluated)` — the diffs
///   in **i128** (lossless; an i64 subtraction could overflow) then
///   `as f64`. **Precision law:** CPython `int / int` is the correctly
///   rounded double of the true rational; Rust converts each diff to f64
///   FIRST — the two agree exactly when both diffs are ≤ 2^53 in magnitude,
///   which the facade's input gate enforces (else it serves the oracle).
/// * `slope > threshold` STRICT (exactly-at-threshold is sane; the
///   `float(memory_slope_bytes_per_tuple)` conversion happens Python-side).
///   Over → `new_high = high + 1` (`checked_add` → [`CounterOverflow`]),
///   code = [`TW_FIRE`] iff `new_high == dwell` else [`TW_NONE`] — the
///   over-slope arm NEVER clears (mirrors `_slope_breach`, which only
///   counts + fires). Else → `new_high = 0`, code = [`TW_CLEAR`].
pub fn tripwire_memory_slope(
    rss: i64,
    evaluated: i64,
    prev_rss: i64,
    prev_evaluated: i64,
    slope_threshold: f64,
    slope_high: i64,
    dwell: i64,
) -> Result<(f64, i64, u8), CounterOverflow> {
    let rss_diff = i128::from(rss) - i128::from(prev_rss);
    let eval_diff = i128::from(evaluated) - i128::from(prev_evaluated);
    let slope = (rss_diff as f64) / (eval_diff as f64);
    if slope > slope_threshold {
        let new_high = slope_high.checked_add(1).ok_or(CounterOverflow)?;
        let code = if new_high == dwell { TW_FIRE } else { TW_NONE };
        Ok((slope, new_high, code))
    } else {
        Ok((slope, 0, TW_CLEAR))
    }
}

/// Law T5 — the per-value NaN/absurd screen (batch):
/// `(values, screened, threshold) → (nan_codes, absurd_codes)`, both
/// aligned with the input order.
///
/// Python owns the `sorted(tallies)` iteration, the `float()` coercion
/// (whose raise IS the I-3 fail-soft input), the screened-name membership
/// (`name in absurd_value_names`), the breach-key strings and the detail
/// templates; the crate owns the per-element classification mirror of
/// `on_tuple_summary` :473-494:
///
/// * `!v.is_finite()` → nan = [`TW_FIRE`], absurd = [`TW_NONE`] (the
///   `continue` — a non-finite value neither clears nor fires the absurd
///   key; mirrored exactly).
/// * Finite → nan = [`TW_CLEAR`]; then screened → `v.abs() > threshold`
///   STRICT → absurd = [`TW_FIRE`] else [`TW_CLEAR`]; unscreened → absurd =
///   [`TW_NONE`]. (`f64::abs` ≡ Python `abs` incl. `-0.0` → `+0.0`; NaN is
///   unreachable in the finite arm.)
pub fn tripwire_summary_classify(
    values: &[f64],
    screened: &[bool],
    absurd_threshold: f64,
) -> Result<(Vec<u8>, Vec<u8>), SummaryLenMismatch> {
    if values.len() != screened.len() {
        return Err(SummaryLenMismatch);
    }
    let mut nan_codes = Vec::with_capacity(values.len());
    let mut absurd_codes = Vec::with_capacity(values.len());
    for (value, is_screened) in values.iter().zip(screened.iter()) {
        if !value.is_finite() {
            nan_codes.push(TW_FIRE);
            absurd_codes.push(TW_NONE);
            continue;
        }
        nan_codes.push(TW_CLEAR);
        if *is_screened {
            absurd_codes.push(if value.abs() > absurd_threshold {
                TW_FIRE
            } else {
                TW_CLEAR
            });
        } else {
            absurd_codes.push(TW_NONE);
        }
    }
    Ok((nan_codes, absurd_codes))
}

/// Law T6 — the stage-prerequisite membership law: a per-token MISSING mask
/// over the Python-pre-sorted required list.
///
/// Python owns the `STAGE_PREREQUISITES.get(stage)` map get + the
/// unknown-stage early return, the `sorted(required)` (codepoint sort ≡
/// Rust UTF-8 byte order), the `missing` fold + the `{sorted(missing)}`
/// list-repr detail, and fire/clear. The crate owns the membership scan:
/// per required token, `!present.contains(tok)` (Rust UTF-8 byte `==` ≡
/// Python codepoint `==` for every str PyO3 accepts — a lone-surrogate
/// token fails conversion and degrades to the oracle facade-side).
///
/// Honest payoff note: the smallest law in the lane (one membership scan);
/// carried for uniformity, independently droppable (spec §10.3).
pub fn tripwire_stage_missing(required_sorted: &[String], present: &[String]) -> Vec<bool> {
    required_sorted
        .iter()
        .map(|token| !present.contains(token))
        .collect()
}

/// Law T9 — the `normalized_ru_maxrss_bytes` platform micro-law.
///
/// The mirror of `run_tripwires.normalized_ru_maxrss_bytes` :322-324:
/// `platform == "darwin"` → the reading verbatim (macOS reports BYTES);
/// anything else → KiB × 1024 (`checked_mul` → [`RssOverflow`] → a
/// `PyValueError` at the FFI edge → the facade degrades to the oracle's
/// unbounded-int product). The uniformity class of `date_isoformat`;
/// independently droppable (spec §10.3).
pub fn normalized_ru_maxrss_bytes(ru_maxrss: i64, platform: &str) -> Result<i64, RssOverflow> {
    if platform == "darwin" {
        return Ok(ru_maxrss);
    }
    ru_maxrss.checked_mul(1024).ok_or(RssOverflow)
}
