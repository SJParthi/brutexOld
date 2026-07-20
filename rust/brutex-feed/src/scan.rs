//! The PURE decision core (no pyo3): one forward scan over the raw f64/i64
//! bar columns, transcribing — block by block, cited per block — the DECISION
//! plane of `brutex/feed/replay.py` (engine-boundary finiteness + positivity,
//! DEC-STORED-DATA-VALIDATION-001 / DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001)
//! and `brutex/data/bad_tick.py::BadTickScreen.screen_mask` (the in-session
//! bad-tick screen: the head median-confirm law `DEC-BADTICK-HEAD-SEED-001`
//! plus the high/low arm `DEC-BADTICK-HILO-SCREEN-001` over the original
//! `DEC-SWEEP-DATA-EDGE-GUARDS-001` ratio law).
//!
//! ## What this module deliberately does NOT do
//!
//! * It NEVER formats a float, NEVER builds a Decimal/string/datetime, and
//!   NEVER returns a value that reaches hashed/logged/raised output — the
//!   outcome is INTEGERS ONLY (row indices + counts + reason codes). Python
//!   selects rows and builds every `CandleBar` / error message / WARN record
//!   itself with the verbatim existing expressions over the SAME
//!   `to_pylist()` objects, so the `bxsnap2` run-identity bytes
//!   (DEC-BXSNAP2-PARITY-GOLDEN-001) have nothing to drift against.
//! * It does NOT pre-validate the `CandleBar` constructor invariants
//!   (ordering / volume / tz): kept rows go through the real Python
//!   constructor, so its exception behavior can never fork.
//! * It does NOT resolve environment flags: `screen_enabled` is the
//!   Python-resolved `bad_tick_screen_enabled()` verdict; `max_ratio` is the
//!   caller-passed `bad_tick.BAD_TICK_MAX_RATIO` (a parameter so cargo tests
//!   can drive custom ratios exactly like the Python unit suite).
//! * It carries NO `seed_prev_close` input: the seed law is the `/db`
//!   page-reader's (`brutex/data/bar_screen.py`), a Python-only path — the
//!   engine read boundary this kernel serves never seeds.
//!
//! ## IEEE-754 fidelity
//!
//! Every comparison transcribes the exact CPython float expression:
//! positivity is `v <= 0.0` (NaN passes — `nan <= 0` is false in both
//! languages; `-0.0` refuses — `-0.0 <= 0.0` is true in both); the bad-tick
//! predicate is the verbatim MULTIPLY-form
//! `value >= reference * max_ratio || value * max_ratio <= reference`
//! (`bad_tick.py::is_bad_tick` — never division, never reordered; two
//! explicit f64 multiplies + compares; Rust does not contract float ops into
//! FMA, so the intermediate products are bit-identical to CPython's C-double
//! multiplies). The head MEDIAN is picked by the `(close, row)` lexicographic
//! sort — `partial_cmp` on the close (total over the head's finite-only
//! members; `-0.0 == 0.0` ties exactly like CPython tuple sort) with the row
//! as the tie-break, mirroring Python's `sorted((closes[i], i) for i in
//! head)[1]` so both arms name the SAME reference row under duplicate
//! values.
//!
//! ## Non-finite robustness (the #1897 composition)
//!
//! A NON-FINITE close is KEPT UNJUDGED and excluded from every reference
//! computation (head vote / baseline / session tallies) — the exact
//! `screen_mask` law. In production the kernel never sees one (the Python
//! seam's `nonfinite_price_column` eligibility gate degrades the whole call
//! to the oracle, whose per-row finiteness gate raises the loud corrupt-bar
//! error), so this arm is domain-totality for the cargo/fuzz surface, never
//! a served path. A non-finite HIGH/LOW rides the ordinary predicate
//! (NaN never trips — kept; `±inf` is out-of-band — dropped), same as
//! `screen_mask`; a `-inf` price STOPS at positivity first (kernel field
//! order) where bare `screen_mask` — which has no positivity plane — would
//! keep-unjudge it: an unreachable-by-construction divergence (the
//! eligibility gate), documented here so nobody "fixes" it.

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

/// Session-head confirmation depth (`bad_tick.HEAD_CONFIRM_BARS` — the
/// DEC-BADTICK-HEAD-SEED-001 evidence quorum): the first up-to-3 bars of a
/// session vote against their median close; the post-head baseline SEEDS
/// from that median.
pub const HEAD_CONFIRM_BARS: usize = 3;

/// Warn-event kind: the per-session skip summary
/// (`feed.bad_tick_skipped` Python-side).
pub const EVENT_KIND_SKIP: usize = 0;

/// Warn-event kind: a 2-bar session whose two closes are mutually
/// out-of-band — no median witness exists, BOTH bars are kept and the
/// session is disclosed unconfirmable (`feed.bad_tick_head_unconfirmed`).
pub const EVENT_KIND_HEAD_UNCONFIRMED: usize = 1;

/// Warn-event kind: the screen dropped a strict MAJORITY of the session's
/// judged rows (`feed.bad_tick_session_majority_dropped` — observe-only, the
/// mask is unchanged by this disclosure).
pub const EVENT_KIND_MAJORITY_DROPPED: usize = 2;

/// Skip-reason code: the CLOSE was out-of-band (`bad_tick.REASON_CLOSE`).
pub const REASON_CLOSE: usize = 0;

/// Skip-reason code: the HIGH was out-of-band on a sane close
/// (`bad_tick.REASON_HIGH` — DEC-BADTICK-HILO-SCREEN-001).
pub const REASON_HIGH: usize = 1;

/// Skip-reason code: the LOW was out-of-band on a sane close + high
/// (`bad_tick.REASON_LOW`).
pub const REASON_LOW: usize = 2;

/// One owed screen WARN, as integer coordinates only — the 9-int
/// decision-plane record shared with
/// `bad_tick.BadTickScreen.screen_mask` (`ScreenWarnEvent`):
/// `(kind, emit_at_row, session_first_row, a, b, c, d, e, f)`.
///
/// * `kind` — [`EVENT_KIND_SKIP`] / [`EVENT_KIND_HEAD_UNCONFIRMED`] /
///   [`EVENT_KIND_MAJORITY_DROPPED`].
/// * `emit_at_row` — the row at whose processing the flush fires (the first
///   row of the NEXT session — under the head law NOT necessarily itself a
///   kept row), or `== n_rows` for the finish flush (present only on a
///   clean, no-stop scan). Non-decreasing across the list: one session's
///   SKIP + MAJORITY_DROPPED events share an anchor, in that order.
/// * `session_first_row` — first row of the FLUSHED session (Python
///   re-derives the ISO session date from ITS OWN datetime object here).
/// * per-kind payload (`a..f`):
///   * SKIP: `(skipped, first_ref_row, first_skip_row, first_reason,
///     n_high, n_low)` — `first_ref_row` is the ROW of the reference close
///     the session's FIRST skip was judged against (the head-vote median row
///     or the last-kept-close row; under the head law it may lie AFTER
///     `first_skip_row`); `first_reason` ∈ [`REASON_CLOSE`]/[`REASON_HIGH`]/
///     [`REASON_LOW`]; `n_high`/`n_low` tally the high-/low-attributed
///     skips (`skipped - n_high - n_low` = close-attributed).
///   * HEAD_UNCONFIRMED: `(head_row_0, head_row_1, 0, 0, 0, 0)` — the two
///     mutually-inconsistent (kept) closes.
///   * MAJORITY_DROPPED: `(skipped, kept_in_session, 0, 0, 0, 0)` over the
///     session's FINITE-close rows.
pub type WarnEvent = (
    usize,
    usize,
    usize,
    usize,
    usize,
    usize,
    usize,
    usize,
    usize,
);

/// The integers-only outcome of one scan.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScanOutcome {
    /// Rows Python must materialize, ascending, in input order.
    pub kept_rows: Vec<usize>,
    /// Owed screen WARNs, non-decreasing by `emit_at_row` (see
    /// [`WarnEvent`]).
    pub warn_events: Vec<WarnEvent>,
    /// `Some((row, field))` when the scan STOPPED at a non-positive price:
    /// `field` is 0/1/2/3 = open/high/low/close (the oracle's per-field
    /// check order — the FIRST offending field is named). When `Some`:
    /// `warn_events` contains ONLY flushes anchored strictly before `row`
    /// (positivity precedes the screen consult, so no boundary flush fires
    /// AT the stop row and no finish flush exists) and `kept_rows` holds
    /// every row `< row` the screen did not skip — INCLUDING rows whose
    /// head vote was still PENDING at the stop (their keep verdict never
    /// resolved false). Downstream-unobservable either way: the Python
    /// materializer raises the stop error after the kept loop, discarding
    /// the bars, and the drain law can never reach a post-stop anchor.
    pub positivity_stop: Option<(usize, usize)>,
}

/// The verbatim multiply-form bad-tick predicate
/// (`bad_tick.py::is_bad_tick`): `value >= reference * max_ratio ||
/// value * max_ratio <= reference` — boundary = bad, NEVER division, NEVER
/// reordered.
#[inline]
fn is_bad_tick(reference: f64, value: f64, max_ratio: f64) -> bool {
    value >= reference * max_ratio || value * max_ratio <= reference
}

/// Name a row's out-of-band field vs `reference`, or `None` — the PINNED
/// reason precedence close → high → low (`bad_tick._skip_reason`,
/// DEC-BADTICK-HILO-SCREEN-001): the first failing field names the skip, so
/// a both-scaled "fat range" bar counts once as `high`.
#[inline]
fn skip_reason(reference: f64, high: f64, low: f64, close: f64, max_ratio: f64) -> Option<usize> {
    if is_bad_tick(reference, close, max_ratio) {
        return Some(REASON_CLOSE);
    }
    if is_bad_tick(reference, high, max_ratio) {
        return Some(REASON_HIGH);
    }
    if is_bad_tick(reference, low, max_ratio) {
        return Some(REASON_LOW);
    }
    None
}

/// Per-session screen state — the exact locals of
/// `BadTickScreen.screen_mask`, reset at every session boundary.
struct SessionState {
    session_first_row: usize,
    /// Pending (unjudged) head-bar row indices, `head[..head_len]`.
    head: [usize; HEAD_CONFIRM_BARS],
    head_len: usize,
    /// `(value, row)` — median-seeded, then the last KEPT close.
    baseline: Option<(f64, usize)>,
    /// Finite-close rows seen in the current session.
    session_rows: usize,
    skipped: usize,
    n_high: usize,
    n_low: usize,
    /// `(ref_row, skip_row, reason)` of the session's FIRST skip.
    first_offender: Option<(usize, usize, usize)>,
}

impl SessionState {
    fn new() -> Self {
        Self {
            session_first_row: 0,
            head: [0; HEAD_CONFIRM_BARS],
            head_len: 0,
            baseline: None,
            session_rows: 0,
            skipped: 0,
            n_high: 0,
            n_low: 0,
            first_offender: None,
        }
    }

    /// Judge row `idx` against `reference` (`bad_tick._judge`):
    /// skip-and-count or advance the baseline.
    #[allow(clippy::too_many_arguments)]
    fn judge(
        &mut self,
        reference: f64,
        ref_row: usize,
        idx: usize,
        highs: &[f64],
        lows: &[f64],
        closes: &[f64],
        max_ratio: f64,
        keep: &mut [bool],
    ) {
        match skip_reason(reference, highs[idx], lows[idx], closes[idx], max_ratio) {
            Some(reason) => {
                keep[idx] = false;
                self.skipped += 1;
                if reason == REASON_HIGH {
                    self.n_high += 1;
                } else if reason == REASON_LOW {
                    self.n_low += 1;
                }
                if self.first_offender.is_none() {
                    self.first_offender = Some((ref_row, idx, reason));
                }
                // Deliberately do NOT advance the baseline: a bar skipped
                // for ANY reason never becomes the reference.
            }
            None => self.baseline = Some((closes[idx], idx)),
        }
    }

    /// Vote the 3 buffered head bars against their median close
    /// (`bad_tick._resolve_head`): the median is the `(close, row)`
    /// lexicographic middle — deterministic under duplicate closes, the
    /// SAME rule Python's tuple sort applies, so both arms name the same
    /// reference row. The post-head baseline SEEDS from the head MEDIAN —
    /// never blindly the last kept head bar (a kept escape-band spike must
    /// not seat it) — even when the median bar itself was dropped for its
    /// high/low (the baseline is a close-derived quantity).
    fn resolve_head(
        &mut self,
        highs: &[f64],
        lows: &[f64],
        closes: &[f64],
        max_ratio: f64,
        keep: &mut [bool],
    ) {
        debug_assert_eq!(self.head_len, HEAD_CONFIRM_BARS);
        let mut trio: [(f64, usize); HEAD_CONFIRM_BARS] = [
            (closes[self.head[0]], self.head[0]),
            (closes[self.head[1]], self.head[1]),
            (closes[self.head[2]], self.head[2]),
        ];
        trio.sort_by(|a, b| {
            a.0.partial_cmp(&b.0)
                .expect("head closes are finite by the is_finite gate")
                .then(a.1.cmp(&b.1))
        });
        let (value, row) = trio[1];
        for k in 0..HEAD_CONFIRM_BARS {
            self.judge(
                value,
                row,
                self.head[k],
                highs,
                lows,
                closes,
                max_ratio,
                keep,
            );
        }
        self.baseline = Some((value, row));
        self.head_len = 0;
    }

    /// Close the current session (`bad_tick._flush`): unconfirmable-head +
    /// WARN bookkeeping, then reset the per-session tallies.
    fn flush(
        &mut self,
        emit_at: usize,
        closes: &[f64],
        max_ratio: f64,
        warns: &mut Vec<WarnEvent>,
    ) {
        if self.head_len == HEAD_CONFIRM_BARS - 1
            && is_bad_tick(closes[self.head[0]], closes[self.head[1]], max_ratio)
        {
            // 2-bar session, mutually inconsistent: no offender is
            // identifiable — keep BOTH, disclose loudly. (A 1-bar head has
            // no witness — silent keep; a 3-bar head already resolved.)
            warns.push((
                EVENT_KIND_HEAD_UNCONFIRMED,
                emit_at,
                self.session_first_row,
                self.head[0],
                self.head[1],
                0,
                0,
                0,
                0,
            ));
        }
        self.head_len = 0;
        if self.skipped > 0
            && let Some((ref_row, skip_row, reason)) = self.first_offender
        {
            warns.push((
                EVENT_KIND_SKIP,
                emit_at,
                self.session_first_row,
                self.skipped,
                ref_row,
                skip_row,
                reason,
                self.n_high,
                self.n_low,
            ));
            let kept_in_session = self.session_rows - self.skipped;
            if self.skipped > kept_in_session {
                // Strict-majority disclosure — observe-only, mask unchanged.
                warns.push((
                    EVENT_KIND_MAJORITY_DROPPED,
                    emit_at,
                    self.session_first_row,
                    self.skipped,
                    kept_in_session,
                    0,
                    0,
                    0,
                    0,
                ));
            }
        }
        self.session_rows = 0;
        self.skipped = 0;
        self.n_high = 0;
        self.n_low = 0;
        self.first_offender = None;
    }
}

/// Collect the kept rows: every row strictly before `frontier` whose keep
/// verdict is still `true` (skipped rows were marked false; pending-head +
/// non-finite-close rows stay true — see [`ScanOutcome::positivity_stop`]).
fn collect_kept(keep: &[bool], frontier: usize) -> Vec<usize> {
    (0..frontier).filter(|&i| keep[i]).collect()
}

/// Scan the raw bar columns and return the decisions, or abstain.
///
/// * `ts_us` — epoch MICROSECONDS UTC; MUST be `[]` when
///   `screen_enabled == false` (the oracle never touches timestamps with the
///   screen off — it derives session dates only for the screen consult).
/// * `opens` / `highs` / `lows` / `closes` — raw f64 lake values (the same
///   `to_pylist()` objects Python keeps for materialization). The screen
///   judges each bar's high + low against the SAME close-derived reference
///   (DEC-BADTICK-HILO-SCREEN-001) — the caller always supplies real
///   columns, there is no close-only kernel mode.
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

    let mut keep = vec![true; n];
    let mut warns: Vec<WarnEvent> = Vec::new();
    let mut cur_session: Option<i64> = None;
    let mut st = SessionState::new();

    for i in 0..n {
        // 1. Engine-boundary POSITIVITY (the `replay.py` oracle loop),
        //    checked FIRST for every row — before the screen ever sees it —
        //    in the fixed field order open → high → low → close; the FIRST
        //    offender stops the scan. `v <= 0.0`: NaN passes, `-0.0`
        //    refuses (IEEE-identical to CPython's `price <= 0`). The
        //    oracle's FINITENESS raise shares this frontier for
        //    kernel-served input: the eligibility gate keeps non-finite
        //    columns off the kernel entirely, so the first oracle raise row
        //    equals the first non-positive row here.
        for (field, v) in [opens[i], highs[i], lows[i], closes[i]]
            .into_iter()
            .enumerate()
        {
            if v <= 0.0 {
                // Positivity precedes the screen consult, so the pending
                // session flush is NOT recorded and no finish flush exists —
                // the oracle raises before either could fire.
                return Ok(Some(ScanOutcome {
                    kept_rows: collect_kept(&keep, i),
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
            //    (the oracle's `ts.astimezone(IST).date()`). The add cannot
            //    overflow: `t < MAX_SAFE_TS_US ≈ 2.54e17` and the offset is
            //    ~1.98e10, far below `i64::MAX`.
            debug_assert!(t.checked_add(IST_OFFSET_US).is_some());
            let session_day = (t + IST_OFFSET_US).div_euclid(DAY_US);

            // Session boundary (`screen_mask`): ANY inequality — including
            // a backwards date flip — flushes the owed WARNs for the
            // session just left (anchored at THIS row, the next session's
            // first row) and resets the per-session state. The head buffer
            // restarts EMPTY — cross-session comparison is forbidden (a
            // stock split must never false-trip).
            if cur_session != Some(session_day) {
                st.flush(i, closes, max_ratio, &mut warns);
                cur_session = Some(session_day);
                st.session_first_row = i;
                st.baseline = None;
            }

            // Non-finite-close robustness (`screen_mask`; module
            // docstring): keep the row UNJUDGED and exclude it from the
            // head vote / baseline / session tallies.
            // Production-unreachable (the eligibility gate) — domain
            // totality for cargo/fuzz.
            if !closes[i].is_finite() {
                continue;
            }
            st.session_rows += 1;
            match st.baseline {
                None => {
                    st.head[st.head_len] = i;
                    st.head_len += 1;
                    if st.head_len == HEAD_CONFIRM_BARS {
                        st.resolve_head(highs, lows, closes, max_ratio, &mut keep);
                    }
                }
                Some((value, row)) => {
                    st.judge(value, row, i, highs, lows, closes, max_ratio, &mut keep);
                }
            }
        }
    }

    // 4. Clean end of input (`screen_mask`'s final `_flush(n)`): flush the
    //    final session's owed WARNs, anchored at `n`. A screen-off scan has
    //    zero session state — the call is a no-op there.
    st.flush(n, closes, max_ratio, &mut warns);

    Ok(Some(ScanOutcome {
        kept_rows: collect_kept(&keep, n),
        warn_events: warns,
        positivity_stop: None,
    }))
}
