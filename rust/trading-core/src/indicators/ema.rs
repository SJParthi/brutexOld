//! Exponential Moving Average (EMA 20 / EMA 200) — a bit-for-bit port of the
//! recurrence in `brutex/indicators/ema.py::EMAState`.
//!
//! ## Why this is in the shared core (DEC-EMA-RUST-PORT-001)
//!
//! EMA was the LAST shared evaluate-time indicator still computed in Python; the
//! evaluator consumed precomputed `ema20`/`ema200` paisa. Moving the RECURRENCE
//! here makes the COMMON indicator core fully Rust (DEC-SHARED-CORE-001), so the
//! backtested EMA equals the (future) live TickVault EMA **by construction** and
//! BruteX's `ema.py` is now a thin delegating shim (mirroring pivots/fib).
//!
//! ## The math (matches TradingView `ta.ema()` — `ema.py` lines 28-35)
//!
//! ```text
//! α = 2 / (N + 1)                          (smoothing factor)
//! warm-up (bars 0..N-2):    None
//! seed bar (bar N-1):       EMA = SMA(values[0..N-1])    (= seed_sum / N)
//! subsequent (bar t ≥ N):   EMA_t = α·close_t + (1 − α)·EMA_(t−1)
//! ```
//!
//! plus the degenerate `N == 1` identity (EMA = the input).
//!
//! ## Bit-identity (the only subtlety — pure IEEE-754 f64)
//!
//! Unlike pivots/fib (integer paisa + banker's-rounded f64 multiplies), EMA runs
//! ENTIRELY in `f64` and the result stays `f64` (the caller — BruteX's
//! `_ema_to_paisa` / TickVault — snaps to paisa separately). The three arithmetic
//! sites are mirrored token-for-token from the Python so the IEEE-754 results are
//! identical:
//!
//! * `α = 2.0 / (period + 1) as f64` — Python `2.0 / (period + 1)` (int→float div);
//! * seed `= seed_sum / period as f64` — Python `seed_sum / self.period`;
//! * step `= α·close + (1.0 − α)·prior` — same operand order.
//!
//! IEEE-754 double arithmetic is deterministic and identical across Python and
//! Rust for the same operation order, so the two produce byte-identical `f64`
//! values (proven by the golden bit-identity test + the Python parity test).
//!
//! O(1) per bar: one branch + (post-seed) two multiplies and one add. No growing
//! history; the streaming state is `period`, `alpha`, `value`, `bars_seen`,
//! `seed_sum` — exactly the Python `EMAState` fields.

/// Smoothing factor for an `N`-period EMA: `α = 2 / (N + 1)`.
///
/// Mirrors `ema.py::alpha`. Returns `Err` for `period < 1` (the Python
/// `ValueError`), so a direct FFI caller is rejected cleanly rather than dividing
/// by a degenerate period.
///
/// # Errors
/// `Err(String)` if `period < 1`, or if `period + 1` overflows `i64`
/// (`DEC-FFI-MAGNITUDE-CEILING-001` — see the `checked_add` note below).
pub fn ema_alpha(period: i64) -> Result<f64, String> {
    if period < 1 {
        return Err(format!("EMA period must be >= 1, got {period}"));
    }
    // Mirror Python `2.0 / (period + 1)` exactly: the `period + 1` is an integer
    // add, then an int→float division producing the same IEEE-754 double.
    //
    // The add is `checked_add`, NOT a bare `+`, because this crate's
    // `[profile.release]` carries BOTH `overflow-checks = true` AND
    // `panic = "abort"` (Cargo.toml). A bare `period + 1` at `i64::MAX` therefore
    // does not wrap and does not unwind into pyo3's catch-and-convert wrapper --
    // it ABORTS the CPython interpreter that called `trading_core.ema_alpha` /
    // `.compute_ema`, so `except ValueError` cannot see it and the whole
    // dashboard/CLI process dies (executed proof in the DEC entry). Routing the
    // refusal through this function's EXISTING `Err(String)` channel turns that
    // SIGABRT into the `ValueError` `python.rs::py_ema_alpha` already maps every
    // error to.
    //
    // BIT-IDENTICAL for every period that does not overflow: same integer add,
    // same int->float cast, same divide, same operand order -- so no currently
    // valid EMA value moves and `indicator_version` (which folds this crate's
    // VERSION string, unchanged) cannot re-key any run identity.
    let denominator = period
        .checked_add(1)
        .ok_or_else(|| format!("EMA period too large: {period} + 1 overflows i64"))?;
    Ok(2.0 / denominator as f64)
}

/// The SMA seed at bar `N-1`: `seed_sum / period` (mirrors `ema.py`'s
/// `self.seed_sum / self.period`). The streaming Python `EMAState` delegates its
/// seed division here so the recurrence math is single-source (Rust).
///
/// # Errors
/// `Err(String)` if `period < 1` (a zero/negative period would divide by a
/// non-positive count); mirrors the upstream `alpha`/`new` guard.
pub fn ema_seed(seed_sum: f64, period: i64) -> Result<f64, String> {
    if period < 1 {
        return Err(format!("EMA period must be >= 1, got {period}"));
    }
    Ok(seed_sum / period as f64)
}

/// The post-seed recurrence step `α·close + (1 − α)·prior` (mirrors `ema.py`'s
/// `self.alpha * close + (1.0 - self.alpha) * prior`, same operand order). The
/// streaming Python `EMAState` delegates its per-bar step here so the recurrence
/// math is single-source (Rust).
#[must_use]
pub fn ema_step(alpha: f64, close: f64, prior: f64) -> f64 {
    alpha * close + (1.0 - alpha) * prior
}

/// True iff `x` is finite (not NaN, not ±∞) — mirrors Python's `math.isfinite`
/// guard in `EMAState.update`, so a NaN/inf close can never silently poison every
/// subsequent EMA value.
#[inline]
#[must_use]
fn is_finite(x: f64) -> bool {
    x.is_finite()
}

/// Streaming-mode EMA state — the single source of the recurrence (the Rust twin
/// of `ema.py::EMAState`, and the TickVault-native path).
///
/// Hold once, call [`EmaState::update`] per new bar. Bit-identical to BruteX's
/// `EMAState` (which now delegates its per-step arithmetic here), so the backtest
/// EMA equals the live EMA by construction.
#[derive(Debug, Clone, PartialEq)]
pub struct EmaState {
    /// EMA period (e.g. 20 or 200).
    period: i64,
    /// Precomputed `2 / (period + 1)`.
    alpha: f64,
    /// Current EMA value, or `None` during warm-up.
    value: Option<f64>,
    /// Count of bars processed so far.
    bars_seen: i64,
    /// Running sum of values during warm-up (used to seed at bar `N-1`).
    seed_sum: f64,
}

impl EmaState {
    /// Construct a fresh `EmaState` for `period`.
    ///
    /// # Errors
    /// `Err(String)` if `period < 1` (mirrors `ema.py::alpha`'s `ValueError`).
    pub fn new(period: i64) -> Result<Self, String> {
        let alpha = ema_alpha(period)?;
        Ok(EmaState {
            period,
            alpha,
            value: None,
            bars_seen: 0,
            seed_sum: 0.0,
        })
    }

    /// The smoothing factor `α = 2 / (period + 1)`.
    #[must_use]
    pub fn alpha(&self) -> f64 {
        self.alpha
    }

    /// The current EMA value, or `None` during warm-up.
    #[must_use]
    pub fn value(&self) -> Option<f64> {
        self.value
    }

    /// Bars processed so far.
    #[must_use]
    pub fn bars_seen(&self) -> i64 {
        self.bars_seen
    }

    /// Running warm-up sum (used to seed at bar `N-1`).
    #[must_use]
    pub fn seed_sum(&self) -> f64 {
        self.seed_sum
    }

    /// The configured period.
    #[must_use]
    pub fn period(&self) -> i64 {
        self.period
    }

    /// Process one bar's `close`; returns the current EMA value, or `None` during
    /// warm-up (bars `0..period-2`). On bar `period-1` returns the SMA seed; on
    /// bars `≥ period` returns the recursive EMA value.
    ///
    /// Bit-for-bit mirror of `ema.py::EMAState.update` — same branch order, same
    /// IEEE-754 operand order, so the `f64` outputs are identical. The non-finite
    /// guard runs BEFORE any state mutation (a rejected close advances nothing).
    ///
    /// # Errors
    /// `Err(String)` if `close` is not finite (NaN / ±∞), matching Python's
    /// `ValueError("EMA close must be finite, …")`.
    pub fn update(&mut self, close: f64) -> Result<Option<f64>, String> {
        if !is_finite(close) {
            return Err(format!("EMA close must be finite, got {close}"));
        }
        self.bars_seen += 1;

        if self.period == 1 {
            // Degenerate: EMA period 1 collapses to identity.
            self.value = Some(close);
            return Ok(self.value);
        }

        if self.bars_seen < self.period {
            // Warm-up: accumulate for the seed.
            self.seed_sum += close;
            return Ok(None);
        }

        if self.bars_seen == self.period {
            // Seed bar: EMA = SMA of values[0..period-1] (delegated to `ema_seed`).
            // Each `close` is finite (guarded above), but a run of finite-but-huge
            // closes can OVERFLOW `seed_sum` to ±∞; re-check the computed seed so an
            // overflowed seed raises a clean `Err` (mapped to `ValueError` at the
            // FFI) instead of returning ±∞ and poisoning every subsequent bar —
            // symmetric with the per-`close` finiteness guard. Real-magnitude inputs
            // sum far below `f64::MAX`, so the golden seed is byte-unchanged.
            self.seed_sum += close;
            let seed = ema_seed(self.seed_sum, self.period)?;
            if !is_finite(seed) {
                return Err(format!(
                    "EMA seed overflowed to a non-finite value ({seed}); \
                     the warm-up close sum is too large"
                ));
            }
            self.value = Some(seed);
            return Ok(self.value);
        }

        // Post-seed recursive update. The branches above guarantee `value` is
        // `Some`; mirror Python's narrowing AssertionError as an `Err` for the
        // (unreachable) corrupted-state case so no faith is required.
        //
        // No finiteness re-check is needed here: `prior` is finite (the seed branch
        // rejected a non-finite seed) and `close` is finite (the input guard), and
        // the step `α·close + (1−α)·prior` is a CONVEX combination — `α ∈ (0, 2/3]`
        // and `(1−α) ∈ [1/3, 1)` sum to exactly 1 for any `N ≥ 2` (the `N == 1`
        // identity returned earlier) — so the result is bounded by the larger
        // finite operand and can never overflow to ±∞. The ONLY f64 overflow site
        // is the warm-up `seed_sum` accumulation, guarded above.
        let prior = self
            .value
            .ok_or_else(|| "EMA state corrupted: post-seed value is None".to_string())?;
        self.value = Some(ema_step(self.alpha, close, prior));
        Ok(self.value)
    }

    /// Reset to fresh state (forget all history) — mirrors `EMAState.reset`.
    pub fn reset(&mut self) {
        self.value = None;
        self.bars_seen = 0;
        self.seed_sum = 0.0;
    }
}

/// Batch interface: compute the full EMA series — the Rust single source of the
/// recurrence BruteX's `compute_ema` / `latest_ema` delegate to.
///
/// Wraps [`EmaState`] so the batch output is bit-identical to a streaming
/// `update()` loop (the same state object drives both). Returns a vector the same
/// length as `values`, with the first `period - 1` entries `None` (warm-up).
///
/// # Errors
/// `Err(String)` if `period < 1`, or if any value is non-finite (NaN / ±∞) —
/// both mirroring `ema.py`'s `ValueError`s.
pub fn compute_ema(values: &[f64], period: i64) -> Result<Vec<Option<f64>>, String> {
    let mut state = EmaState::new(period)?;
    let mut out = Vec::with_capacity(values.len());
    for &v in values {
        out.push(state.update(v)?);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn alpha_matches_python_convention() {
        // 2 / (20+1) = 2/21; 2 / (200+1) = 2/201; 2 / (1+1) = 1.0.
        assert_eq!(ema_alpha(20).unwrap(), 2.0 / 21.0);
        assert_eq!(ema_alpha(200).unwrap(), 2.0 / 201.0);
        assert_eq!(ema_alpha(1).unwrap(), 1.0);
    }

    #[test]
    fn alpha_rejects_non_positive_period() {
        assert!(ema_alpha(0).is_err());
        assert!(ema_alpha(-5).is_err());
    }

    #[test]
    fn ema_seed_is_sma() {
        // seed_sum 60 over period 3 -> 20.0; the period-1 case is seed_sum itself.
        assert_eq!(ema_seed(60.0, 3).unwrap(), 20.0);
        assert_eq!(ema_seed(42.0, 1).unwrap(), 42.0);
        assert!(ema_seed(10.0, 0).is_err());
        assert!(ema_seed(10.0, -1).is_err());
    }

    #[test]
    fn ema_step_matches_recurrence() {
        // alpha=0.5: 0.5*40 + 0.5*20 = 30.0.
        assert_eq!(ema_step(0.5, 40.0, 20.0), 30.0);
        // A degenerate alpha=1.0 collapses to the new close.
        assert_eq!(ema_step(1.0, 99.0, 7.0), 99.0);
    }

    #[test]
    fn empty_input_returns_empty() {
        assert_eq!(compute_ema(&[], 20).unwrap(), Vec::<Option<f64>>::new());
    }

    #[test]
    fn period_1_is_identity() {
        let values = [100.0, 101.0, 102.0];
        assert_eq!(
            compute_ema(&values, 1).unwrap(),
            vec![Some(100.0), Some(101.0), Some(102.0)]
        );
    }

    #[test]
    fn hand_computed_period_3_oracle() {
        // alpha = 0.5; [10,20,30,40,50] -> [None, None, 20, 30, 40].
        let values = [10.0, 20.0, 30.0, 40.0, 50.0];
        assert_eq!(
            compute_ema(&values, 3).unwrap(),
            vec![None, None, Some(20.0), Some(30.0), Some(40.0)]
        );
    }

    #[test]
    fn warm_up_prefix_is_none_then_sma_seed() {
        // period=5, constant 100 -> bars 0..3 None, bar 4 = seed = 100.
        let values = [100.0; 10];
        let got = compute_ema(&values, 5).unwrap();
        assert_eq!(got[..4], [None, None, None, None]);
        assert_eq!(got[4], Some(100.0));
    }

    #[test]
    fn period_20_seed_is_sma_of_first_20() {
        // values 1..=30; seed at index 19 = SMA(1..=20) = 210/20 = 10.5.
        let values: Vec<f64> = (1..=30).map(|i| i as f64).collect();
        let got = compute_ema(&values, 20).unwrap();
        assert!(got[..19].iter().all(Option::is_none));
        assert_eq!(got[19], Some(10.5));
    }

    #[test]
    fn streaming_equals_batch() {
        let values: Vec<f64> = (0..50).map(|i| (i as f64) * 1.5 + 100.0).collect();
        let batch = compute_ema(&values, 20).unwrap();
        let mut state = EmaState::new(20).unwrap();
        let streamed: Vec<Option<f64>> = values.iter().map(|&v| state.update(v).unwrap()).collect();
        assert_eq!(streamed, batch);
    }

    #[test]
    fn no_look_ahead_prefix_matches_full() {
        // EMA at bar N on the full series == EMA at bar N on the prefix [0..=N].
        let values: Vec<f64> = (0..50).map(|i| (i as f64) * 1.5 + 100.0).collect();
        let full = compute_ema(&values, 10).unwrap();
        for n in 10..50 {
            let prefix = compute_ema(&values[..=n], 10).unwrap();
            assert_eq!(prefix[n], full[n], "look-ahead at bar {n}");
        }
    }

    #[test]
    fn non_finite_close_is_rejected_before_mutation() {
        let mut state = EmaState::new(5).unwrap();
        state.update(100.0).unwrap();
        let before = state.clone();
        assert!(state.update(f64::NAN).is_err());
        assert!(state.update(f64::INFINITY).is_err());
        assert!(state.update(f64::NEG_INFINITY).is_err());
        // A rejected close advanced nothing.
        assert_eq!(state, before);
    }

    #[test]
    fn compute_ema_propagates_non_finite_and_bad_period() {
        assert!(compute_ema(&[100.0, f64::NAN, 102.0], 2).is_err());
        assert!(compute_ema(&[1.0, 2.0, 3.0], 0).is_err());
    }

    #[test]
    fn finite_but_huge_seed_overflow_is_rejected_not_inf() {
        // DEC-ADVERSARIAL-HARDENING-001 FIX 4: each close is finite (passes the
        // input guard), but their SMA seed sum overflows f64 → +inf. The seed
        // re-check must REJECT it (Err) instead of returning +inf and poisoning
        // every subsequent bar. Reproduces the adversarial-sweep finding.
        let res = compute_ema(&[1e308, 1e308], 2);
        assert!(res.is_err(), "expected an Err, got {res:?}");
        assert!(res.unwrap_err().contains("seed overflowed"));
        // Streaming path identical.
        let mut state = EmaState::new(2).unwrap();
        state.update(1e308).unwrap();
        assert!(state.update(1e308).is_err());
    }

    #[test]
    fn post_seed_step_stays_finite_for_extreme_finite_inputs() {
        // The post-seed recurrence is a CONVEX combination, so once the seed is
        // finite (guarded) every subsequent EMA stays finite even for max-magnitude
        // closes — it can never overflow to ±∞ (the rationale for having NO step
        // re-check). seed = mean(1e308, -1e308) = 0, then huge closes stay bounded.
        let res = compute_ema(&[1e308, -1e308, 1e308, 1e308], 2).unwrap();
        assert!(
            res.iter().flatten().all(|v| v.is_finite()),
            "post-seed EMA went non-finite: {res:?}"
        );
    }

    #[test]
    fn real_magnitude_series_unaffected_by_overflow_guards() {
        // The new finiteness re-checks NEVER fire for real-magnitude inputs, so the
        // output stays byte-identical to the documented oracle.
        let values = [10.0, 20.0, 30.0, 40.0, 50.0];
        assert_eq!(
            compute_ema(&values, 3).unwrap(),
            vec![None, None, Some(20.0), Some(30.0), Some(40.0)]
        );
    }

    #[test]
    fn reset_allows_clean_replay() {
        let mut state = EmaState::new(3).unwrap();
        for v in [10.0, 20.0, 30.0, 40.0, 50.0] {
            state.update(v).unwrap();
        }
        assert_eq!(state.value(), Some(40.0));
        state.reset();
        let replay: Vec<Option<f64>> = [10.0, 20.0, 30.0, 40.0, 50.0]
            .iter()
            .map(|&v| state.update(v).unwrap())
            .collect();
        assert_eq!(replay, vec![None, None, Some(20.0), Some(30.0), Some(40.0)]);
    }

    #[test]
    fn accessors_track_warmup_state() {
        let mut state = EmaState::new(5).unwrap();
        assert_eq!(state.period(), 5);
        assert_eq!(state.alpha(), 2.0 / 6.0);
        assert_eq!(state.value(), None);
        assert_eq!(state.bars_seen(), 0);
        assert_eq!(state.seed_sum(), 0.0);
        state.update(10.0).unwrap();
        state.update(20.0).unwrap();
        assert_eq!(state.bars_seen(), 2);
        assert_eq!(state.seed_sum(), 30.0);
        assert_eq!(state.value(), None);
    }
}
