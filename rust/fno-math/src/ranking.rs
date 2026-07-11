//! Ranking kernels — the Rust twins of `brutex.ranking`'s rank-time math
//! (slice 4, PR-1: `feat(ranking): Rust ranking conversion`).
//!
//! Every function here is a TRANSLITERATION of its Python original in
//! `brutex/ranking/__init__.py` / `brutex/ranking/overfit.py` — same
//! operations, same ORDER, same literals — under the slice-4 float-parity
//! ruling:
//!
//! * **(a) bit-exact** — gates + gate reasons (comparisons only, zero float
//!   arithmetic), the rank-order comparator (float negation + integer
//!   tie-break), the overfit verdict (`>=`).
//! * **(b) bit-exact-by-construction** — composite term inputs (one
//!   subtract, one multiply, one correctly-rounded i64/i64 division) and
//!   the left-associated weighted composite fold: only `+ − × ÷` (hardware
//!   correctly rounded) on identical operand bits in identical order.
//! * **(b\*) host-adjudicated** — `_zscores` (and everything embedding it:
//!   the composite pipeline, the DSR z-term): Python's `x ** 2` / `x ** 0.5`
//!   route through the platform libm `pow`; this kernel uses `x * x` /
//!   `f64::sqrt`. The identities `pow(x, 2) == x*x` and
//!   `pow(x, 0.5) == sqrt(x)` are MODELED, never cited as documented — they
//!   are adjudicated by the Python-side host-probe test
//!   (`tests/unit/test_pow_host_probe.py`) on every CI host, and at runtime
//!   every (b\*) call runs under the ALWAYS-ON per-call differential in
//!   `brutex/engine/rust_parity_core.py` (ship-the-oracle on any bit
//!   mismatch + per-family autorevert), so a divergent bit can never reach
//!   a ranking, a persisted row, or a redo comparison.
//!
//! ## Transliteration laws (each pinned by a test)
//!
//! * **The oracle's OWN summation discipline, preserved token-for-token**
//!   (M3, corrected): `_zscores`' `sum(...)` calls are CPython ≥ 3.12's
//!   builtin `sum`, which is the NEUMAIER-compensated float fold of
//!   gh-100425 — NOT the naive left fold the slice-4 design transcribed
//!   (see [`py_float_sum`] for the live-caught divergence + the exact
//!   transliteration). Still strictly sequential: NO pairwise / SIMD /
//!   `mul_add` / rayon — those would change bits (AST-pinned). The
//!   composite weighted fold below is a plain `+` EXPRESSION chain in
//!   Python (not builtin sum) and stays a literal left-associated chain.
//! * **The composite fold is strictly LEFT-ASSOCIATED** — seven multiplies
//!   + seven adds written literally in the spec's order, DSR term last.
//! * **The `_zscores` NaN/±inf clamp law** (M6): Python
//!   `max(-3.0, min(3.0, z))` maps `NaN → +3.0` (argument-order dependent),
//!   `+inf → +3.0`, `−inf → −3.0`. Implemented as the literal
//!   [`py_min`] / [`py_max`] helpers — NEVER `f64::clamp` (NaN-propagating)
//!   or `f64::min`/`f64::max` (non-NaN-arg-preferring).
//! * **Ordering semantics** (M5): stable sorts on index permutations; the
//!   comparator uses IEEE `partial_cmp` semantics where `-0.0 == +0.0`
//!   (NEVER `total_cmp` — it splits the zeros); a non-finite composite is a
//!   typed error (unreachable upstream — every z is clamped finite); the
//!   mask tie-break is exact `u128` comparison.
//! * **Correctly-rounded i64/i64 → f64 division** (M1, FAST PATH):
//!   CPython's `int.__truediv__` is correctly rounded for ALL ints; Rust's
//!   naive `(a as f64) / (b as f64)` double-rounds past 2⁵³. [`cr_div_i64`]
//!   ships the fast path only — both `|a|, |b| ≤ 2⁵³` convert to f64
//!   EXACTLY, so the hardware IEEE division IS the correctly-rounded
//!   quotient. Anything larger is a typed error and the Python wrapper
//!   serves the call from the bit-exact oracle (the u128
//!   round-to-nearest-even slow path is PR-2 / `exact_stats.rs` territory).
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Under the crate's `overflow-checks = true` + `panic = "abort"` release
//! profile an out-of-bounds index or unchecked overflow would SIGABRT the
//! host interpreter. Every boundary violation returns `Result<_, String>`
//! mapped to a clean Python `ValueError`; the Python wrapper treats ANY
//! kernel error as "serve this call from the Python oracle".
//!
//! ## Complexity (honest envelope — Golden Rule #10)
//!
//! All functions are per-finalist / per-leaderboard RANK-TIME code — NOT
//! the 33-billion-trial hot loop. O(survivors) per call (the sort is
//! O(n log n)); no speedup is claimed — the payoff is one-language
//! consistency, and the (b\*) rows deliberately double-compute forever
//! under the always-on differential (bench-pinned bound Python-side).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

// ── Constants — decimal literals identical to brutex/ranking/__init__.py ──
// (decimal → f64 rounding is identical in CPython and Rust, so the bits
// match; pinned by tests/unit/test_ranking.py:437-464 on the Python side
// and the known-value pins below on this side.)

/// Twin of `brutex.ranking.MIN_TRADES` (exact-int gate).
pub const MIN_TRADES: i64 = 30;
const MIN_PROFIT_FACTOR: f64 = 1.2;
const MAX_DRAWDOWN_PCT: f64 = 30.0;
const MIN_WIN_RATE: f64 = 0.25;
const MIN_WIN_LOSS_RATIO: f64 = 2.0;
const MAX_CONSECUTIVE_LOSSES: i64 = 10;

const W_SHARPE: f64 = 0.25;
const W_SORTINO: f64 = 0.20;
const W_CALMAR: f64 = 0.15;
const W_PROFIT_FACTOR: f64 = 0.15;
const W_RECOVERY: f64 = 0.10;
const W_WIN_WLR: f64 = 0.10;
const W_RISK_EFFICIENCY: f64 = 0.15;

const Z_CLAMP: f64 = 3.0;

/// Twin of `brutex.ranking.N_COMPOSITE_TERMS`.
pub const N_COMPOSITE_TERMS: usize = 7;

/// The f64-exactness ceiling for the [`cr_div_i64`] FAST path: every
/// integer with `|x| ≤ 2⁵³` converts to f64 exactly (2⁵³ itself is exactly
/// representable), so the hardware division of the exact operands is the
/// correctly-rounded quotient — CPython's `int / int` result bit-for-bit.
pub const MAX_EXACT_DIV_OPERAND: u64 = 1 << 53;

// ── Gate-reason wire tokens (STABLE — byte-identical to the Python
//    vocabulary in brutex/ranking/__init__.py; renaming is a breaking
//    contract change, pinned by the differential suite) ──
const GATE_NON_FINITE_INPUT: &str = "non_finite_input";
const GATE_MIN_TRADES: &str = "min_trades";
const GATE_PROFIT_FACTOR: &str = "profit_factor";
const GATE_MAX_DRAWDOWN: &str = "max_drawdown";
const GATE_WIN_RATE_WLR: &str = "win_rate_wlr";
const GATE_CONSECUTIVE_LOSSES: &str = "consecutive_losses";

// ────────────────────────────────────────────────────────────────────────
// Pure cores — NO PyO3 types anywhere below this line until the wrapper
// section (rlib-first discipline, §2.1 of the slice-4 design: the future
// trading-core migration moves these files verbatim).
// ────────────────────────────────────────────────────────────────────────

/// Literal twin of CPython's two-argument `min(a, b)`: `b if b < a else a`.
///
/// NaN law: `py_min(3.0, NaN)` → `NaN < 3.0` is false → `3.0` (the FIRST
/// argument wins on a NaN comparison) — exactly CPython's argument-order
/// dependent behaviour. NEVER replace with `f64::min` (returns the non-NaN
/// argument regardless of position) or `f64::clamp` (propagates NaN).
#[inline]
pub fn py_min(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// Literal twin of CPython's two-argument `max(a, b)`: `b if b > a else a`.
///
/// Same argument-order NaN law as [`py_min`].
#[inline]
pub fn py_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Correctly-rounded `i64 / i64 → f64` division — the M1 FAST PATH.
///
/// Errors when the denominator is zero (Python raises
/// `ZeroDivisionError`; production floors the denominator at 1) or when
/// either operand's magnitude exceeds [`MAX_EXACT_DIV_OPERAND`] (2⁵³ — the
/// boundary past which `as f64` casts round and the hardware quotient can
/// double-round off CPython's correctly-rounded `int.__truediv__`). The
/// Python wrapper serves an out-of-envelope call from the bit-exact
/// oracle; the u128 RNE slow path is PR-2 territory.
pub fn cr_div_i64(numerator: i64, denominator: i64) -> Result<f64, String> {
    if denominator == 0 {
        return Err("cr_div_i64: division by zero".to_string());
    }
    if numerator.unsigned_abs() > MAX_EXACT_DIV_OPERAND
        || denominator.unsigned_abs() > MAX_EXACT_DIV_OPERAND
    {
        return Err(format!(
            "cr_div_i64: operand magnitude beyond the 2^53 fast-path exactness \
             envelope (numerator {numerator}, denominator {denominator})"
        ));
    }
    // Both operands convert exactly ⇒ the IEEE division is the correctly
    // rounded quotient of the true rational — CPython's result bit-for-bit.
    Ok((numerator as f64) / (denominator as f64))
}

/// Twin of `brutex.ranking._non_finite_gate_input`: present-but-non-finite.
///
/// `None` is NOT non-finite (it keeps its per-gate `None` handling) —
/// `Option<f64>` carries the distinction; `None` is NEVER NaN-encoded.
#[inline]
pub fn non_finite_gate_input(value: Option<f64>) -> bool {
    match value {
        Some(v) => !v.is_finite(),
        None => false,
    }
}

/// One candidate's gate inputs — the exact scalar set `passes_gates` /
/// `gate_reasons` read from `ResultMetrics` (`Option` mirrors `None`).
#[derive(Clone, Copy, Debug)]
pub struct GateInputs {
    pub total_trades: i64,
    pub profit_factor: Option<f64>,
    pub max_drawdown_pct: f64,
    pub win_rate: f64,
    pub win_loss_ratio: Option<f64>,
    pub max_consecutive_losses: i64,
}

impl GateInputs {
    /// The fail-closed guard shared by both twins: any PRESENT NaN/±inf
    /// among the four float gate inputs (a NaN silently passes `<` / `>`).
    fn any_non_finite(&self) -> bool {
        non_finite_gate_input(self.profit_factor)
            || non_finite_gate_input(Some(self.max_drawdown_pct))
            || non_finite_gate_input(Some(self.win_rate))
            || non_finite_gate_input(self.win_loss_ratio)
    }
}

/// Twin of `brutex.ranking.passes_gates` — the 5 hard doors + the
/// fail-closed guard, identical comparison order + pass-at-equal boundary
/// laws (exactly-at-threshold PASSES on every door).
pub fn passes_gates_core(m: &GateInputs) -> bool {
    if m.any_non_finite() {
        return false;
    }
    if m.total_trades < MIN_TRADES {
        return false;
    }
    match m.profit_factor {
        None => return false,
        Some(pf) if pf < MIN_PROFIT_FACTOR => return false,
        Some(_) => {}
    }
    if m.max_drawdown_pct > MAX_DRAWDOWN_PCT {
        return false;
    }
    // The SAME `None` → 0.0 projection as Python (a low hit-rate is only
    // acceptable if the winners are much bigger than the losers).
    let wlr = m.win_loss_ratio.unwrap_or(0.0);
    if m.win_rate < MIN_WIN_RATE && wlr < MIN_WIN_LOSS_RATIO {
        return false;
    }
    m.max_consecutive_losses <= MAX_CONSECUTIVE_LOSSES
}

/// Twin of `brutex.ranking.gate_reasons` — every failed door, named, in
/// `GATE_REASON_ORDER`; the non-finite guard short-circuits ALONE; empty
/// ⇔ pass. The twin law `passes_gates_core(m) == reasons.is_empty()` is
/// property-gated in BOTH languages.
pub fn gate_reasons_core(m: &GateInputs) -> Vec<&'static str> {
    if m.any_non_finite() {
        return vec![GATE_NON_FINITE_INPUT];
    }
    let mut reasons: Vec<&'static str> = Vec::new();
    if m.total_trades < MIN_TRADES {
        reasons.push(GATE_MIN_TRADES);
    }
    let pf_fails = match m.profit_factor {
        None => true,
        Some(pf) => pf < MIN_PROFIT_FACTOR,
    };
    if pf_fails {
        reasons.push(GATE_PROFIT_FACTOR);
    }
    if m.max_drawdown_pct > MAX_DRAWDOWN_PCT {
        reasons.push(GATE_MAX_DRAWDOWN);
    }
    let wlr = m.win_loss_ratio.unwrap_or(0.0);
    if m.win_rate < MIN_WIN_RATE && wlr < MIN_WIN_LOSS_RATIO {
        reasons.push(GATE_WIN_RATE_WLR);
    }
    if m.max_consecutive_losses > MAX_CONSECUTIVE_LOSSES {
        reasons.push(GATE_CONSECUTIVE_LOSSES);
    }
    reasons
}

/// Assemble per-candidate [`GateInputs`] rows from equal-length columns —
/// the row-assembly core of BOTH batched gate wrappers.
///
/// Lives in the PURE-CORE section (round-4 review): the `#[pyfunction]`
/// wrappers construct `PyErr`, so under the crate's `extension-module`
/// no-libpython `cargo test` link NO Rust test can call them — every
/// killable branch (the length guard + the positional field assembly)
/// must therefore live in a plain-`Result` core the in-file mutation
/// tests can drive (the `python.rs` `add_u64_checked` split).
fn gate_rows(
    total_trades: &[i64],
    profit_factor: &[Option<f64>],
    max_drawdown_pct: &[f64],
    win_rate: &[f64],
    win_loss_ratio: &[Option<f64>],
    max_consecutive_losses: &[i64],
) -> Result<Vec<GateInputs>, String> {
    let n = total_trades.len();
    if profit_factor.len() != n
        || max_drawdown_pct.len() != n
        || win_rate.len() != n
        || win_loss_ratio.len() != n
        || max_consecutive_losses.len() != n
    {
        return Err("ranking gates: unequal column lengths".to_string());
    }
    Ok((0..n)
        .map(|i| GateInputs {
            total_trades: total_trades[i],
            profit_factor: profit_factor[i],
            max_drawdown_pct: max_drawdown_pct[i],
            win_rate: win_rate[i],
            win_loss_ratio: win_loss_ratio[i],
            max_consecutive_losses: max_consecutive_losses[i],
        })
        .collect())
}

/// Twin of `brutex.ranking._f`: a `None` composite term contributes 0.
#[inline]
fn f_or_zero(value: Option<f64>) -> f64 {
    value.unwrap_or(0.0)
}

/// Transliteration of CPython ≥ 3.12's builtin `sum()` over floats — the
/// Neumaier (improved Kahan–Babuška) compensated fold of CPython
/// gh-100425 (`Python/bltinmodule.c` `builtin_sum_impl`, float fast path).
///
/// **This is NOT a naive left fold.** The slice-4 design (and the ranking
/// reader map) transcribed `sum(values)` as "a strict left-to-right f64
/// fold" — that was true through CPython 3.11 and is FALSE on the repo's
/// 3.12 baseline: `sum([1e16, 1.0, -1e16]) == 1.0` (compensated), where
/// the naive fold yields `0.0`. The empirically-caught consequence in
/// `_zscores`: an all-equal column like `[0.8999999999999999; 200]` has a
/// compensated mean EXACTLY equal to the element (→ the `variance == 0.0`
/// all-zeros short-circuit) while the naive mean is one ulp off (→ every
/// z becomes ±1.0) — a 0.1-scale composite shift, caught live by the
/// always-on differential before this transliteration replaced the naive
/// fold. Op-for-op parity with the ORACLE is the higher law; the fold is
/// therefore CPython's, token-for-token:
///
/// ```c
/// double t = f_result + x;
/// if (fabs(f_result) >= fabs(x)) c += (f_result - t) + x;
/// else                           c += (x - t) + f_result;
/// f_result = t;
/// // ... at exhaustion:
/// if (c && isfinite(c)) f_result += c;   // sign-of--0.0 + inf guards
/// ```
///
/// The exit guard is load-bearing: `c == ±0.0` skips the add (adding
/// `+0.0` would flip a `-0.0` total's sign) and a non-finite `c` (an
/// inf-bearing pool) never converts an infinite sum to NaN. Python's
/// `int 0` start is exact (`0.0` here — the first iteration adds onto
/// `+0.0` exactly as CPython's int→double switch does).
fn py_float_sum(values: impl Iterator<Item = f64>) -> f64 {
    let mut f_result = 0.0f64;
    let mut c = 0.0f64;
    for x in values {
        let t = f_result + x;
        if f_result.abs() >= x.abs() {
            c += (f_result - t) + x;
        } else {
            c += (x - t) + f_result;
        }
        f_result = t;
    }
    if c != 0.0 && c.is_finite() {
        f_result += c;
    }
    f_result
}

/// Twin of `brutex.ranking._zscores` — population z-scores clamped to
/// `[-3, +3]`, transliterated op-for-op: the CPython-3.12 compensated
/// builtin-`sum` folds ([`py_float_sum`]), the exact `variance == 0.0`
/// short-circuit, `x*x` / `f64::sqrt` for the (b\*) pow sites, the
/// [`py_min`]/[`py_max`] clamp.
///
/// Errors on an EMPTY input — Python raises `ZeroDivisionError` there
/// (`sum([]) / 0`), and the wrapper's oracle fallback reproduces that.
pub fn zscores_core(values: &[f64]) -> Result<Vec<f64>, String> {
    if values.is_empty() {
        return Err("zscores: empty input (Python raises ZeroDivisionError)".to_string());
    }
    let n = values.len() as f64; // exact for any real leaderboard (n ≪ 2⁵³)
    // `sum(values)` — CPython 3.12's compensated builtin sum, NOT a naive
    // fold (see py_float_sum; still strictly sequential — no pairwise/
    // SIMD/mul_add/rayon, which would change bits).
    let total = py_float_sum(values.iter().copied());
    let mean = total / n;
    // `sum((v - mean) ** 2 ...)` — same builtin-sum fold over the mapped
    // generator; `** 2` as `d * d` (the (b*) pow-identity site,
    // host-probe adjudicated).
    let sq = py_float_sum(values.iter().map(|&v| {
        let d = v - mean;
        d * d
    }));
    let variance = sq / n;
    if variance == 0.0 {
        return Ok(vec![0.0; values.len()]);
    }
    // `variance ** 0.5` as `sqrt` (the second (b*) pow-identity site).
    let std = variance.sqrt();
    Ok(values
        .iter()
        .map(|&v| py_max(-Z_CLAMP, py_min(Z_CLAMP, (v - mean) / std)))
        .collect())
}

/// One survivor's composite term-input scalars (the `ResultMetrics`
/// projection `composite_term_inputs` reads; `Option` mirrors `None`).
#[derive(Clone, Copy, Debug)]
pub struct TermInputs {
    pub sharpe_ratio: Option<f64>,
    pub sortino_ratio: Option<f64>,
    pub calmar_ratio: Option<f64>,
    pub profit_factor: Option<f64>,
    pub recovery_factor: Option<f64>,
    pub win_rate: f64,
    pub win_loss_ratio: Option<f64>,
    pub avg_mfe_paisa: i64,
    pub avg_mae_paisa: i64,
}

/// Twin of `brutex.ranking.composite_term_inputs` — the 7 z-term inputs in
/// LOCKED column order: `(sharpe, sortino, calmar, PF − 1.0, recovery,
/// win_rate × wlr, risk_efficiency)`, each `None` → 0.0 projected.
///
/// The risk-efficiency term is `cr_div_i64(avg_mfe, max(avg_mae, 1))`
/// (M1) — an out-of-envelope paisa magnitude is a typed error and the
/// wrapper serves the whole call from the Python oracle.
pub fn composite_term_inputs_core(m: &TermInputs) -> Result<[f64; N_COMPOSITE_TERMS], String> {
    let risk_efficiency = cr_div_i64(m.avg_mfe_paisa, m.avg_mae_paisa.max(1))?;
    Ok([
        f_or_zero(m.sharpe_ratio),
        f_or_zero(m.sortino_ratio),
        f_or_zero(m.calmar_ratio),
        f_or_zero(m.profit_factor) - 1.0,
        f_or_zero(m.recovery_factor),
        m.win_rate * f_or_zero(m.win_loss_ratio),
        risk_efficiency,
    ])
}

/// Assemble per-survivor [`TermInputs`] rows from equal-length columns and
/// project each through [`composite_term_inputs_core`] — the batched
/// `composite_term_inputs` twin's row core (one 7-element row per
/// survivor; the `Vec<Vec<f64>>` row container is deliberate — a 7-tuple
/// return type was measured to explode the cargo-mutants
/// value-enumeration ~45× on the wrapper and the same law holds here).
///
/// Lives in the PURE-CORE section (round-4 review): the guard + assembly
/// previously sat INLINE in the PyErr-constructing `#[pyfunction]`
/// wrapper, where no `cargo test` can link a call — their mutants were
/// structurally unkillable. Plain `Result<_, String>` here restores the
/// in-file mutation teeth (the `python.rs` `add_u64_checked` split).
#[allow(clippy::too_many_arguments)]
fn composite_term_input_rows(
    sharpe_ratio: &[Option<f64>],
    sortino_ratio: &[Option<f64>],
    calmar_ratio: &[Option<f64>],
    profit_factor: &[Option<f64>],
    recovery_factor: &[Option<f64>],
    win_rate: &[f64],
    win_loss_ratio: &[Option<f64>],
    avg_mfe_paisa: &[i64],
    avg_mae_paisa: &[i64],
) -> Result<Vec<Vec<f64>>, String> {
    let n = sharpe_ratio.len();
    if sortino_ratio.len() != n
        || calmar_ratio.len() != n
        || profit_factor.len() != n
        || recovery_factor.len() != n
        || win_rate.len() != n
        || win_loss_ratio.len() != n
        || avg_mfe_paisa.len() != n
        || avg_mae_paisa.len() != n
    {
        return Err("ranking_composite_term_inputs: unequal column lengths".to_string());
    }
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let terms = composite_term_inputs_core(&TermInputs {
            sharpe_ratio: sharpe_ratio[i],
            sortino_ratio: sortino_ratio[i],
            calmar_ratio: calmar_ratio[i],
            profit_factor: profit_factor[i],
            recovery_factor: recovery_factor[i],
            win_rate: win_rate[i],
            win_loss_ratio: win_loss_ratio[i],
            avg_mfe_paisa: avg_mfe_paisa[i],
            avg_mae_paisa: avg_mae_paisa[i],
        })?;
        out.push(terms.to_vec());
    }
    Ok(out)
}

/// Twin of `brutex.ranking.composite_scores` — z-normalise the 7 term
/// columns ([`zscores_core`] each) and fold the weighted composite per
/// survivor, strictly LEFT-ASSOCIATED with the DSR term LAST (no
/// `mul_add`, no reordering — the exact spec chain).
pub fn composite_scores_core(
    term_columns: &[Vec<f64>],
    dsr_contrib: &[f64],
) -> Result<Vec<f64>, String> {
    if term_columns.len() != N_COMPOSITE_TERMS {
        // Byte-identical message to the Python ValueError.
        return Err(format!(
            "composite_scores expects {N_COMPOSITE_TERMS} term columns, got {}",
            term_columns.len()
        ));
    }
    let mut z: Vec<Vec<f64>> = Vec::with_capacity(N_COMPOSITE_TERMS);
    for column in term_columns {
        z.push(zscores_core(column)?);
    }
    for (term, column) in z.iter().enumerate() {
        if column.len() < dsr_contrib.len() {
            // Python would IndexError here; the wrapper's oracle fallback
            // reproduces that (a typed error, never an OOB panic → SIGABRT).
            return Err(format!(
                "composite_scores: term column {term} has {} survivors, \
                 dsr_contrib has {}",
                column.len(),
                dsr_contrib.len()
            ));
        }
    }
    let mut out = Vec::with_capacity(dsr_contrib.len());
    for (i, &dsr) in dsr_contrib.iter().enumerate() {
        // Strictly left-associated: ((((((w0·z0 + w1·z1) + w2·z2) + …) +
        // w6·z6) + dsr) — Python's `+` chain rounds after every add.
        let score = W_SHARPE * z[0][i]
            + W_SORTINO * z[1][i]
            + W_CALMAR * z[2][i]
            + W_PROFIT_FACTOR * z[3][i]
            + W_RECOVERY * z[4][i]
            + W_WIN_WLR * z[5][i]
            + W_RISK_EFFICIENCY * z[6][i]
            + dsr;
        out.push(score);
    }
    Ok(out)
}

/// Twin of the ACTIVE arm of `brutex.ranking.dsr_z_contributions` — the
/// per-survivor weighted Deflated-Sharpe z-term (`_zscores` then one
/// multiply per survivor). The inactive `None` arm (`[0.0] * n`) stays in
/// Python (trivial, zero float arithmetic).
pub fn dsr_z_contributions_core(dsr_by_survivor: &[f64], weight: f64) -> Result<Vec<f64>, String> {
    let z = zscores_core(dsr_by_survivor)?;
    Ok(z.iter().map(|&v| weight * v).collect())
}

/// Twin of `brutex.ranking.rank_combos`' total-order sort — an INDEX
/// PERMUTATION over `(-composite_score, mask)` (score DESCENDING via exact
/// float negation, unique mask ASCENDING as the final tie-break).
///
/// Comparator law (M5): IEEE `partial_cmp` semantics where
/// `-0.0 == +0.0` (the mask then decides) — NEVER `total_cmp`, which
/// splits the zeros where Python tuple compare does not. A non-finite
/// composite is a typed error: unreachable upstream (every z is clamped
/// finite, so every composite is finite), but a NaN key would break strict
/// weak ordering, so it is refused rather than debug-asserted away.
pub fn rank_order_core(composites: &[f64], masks: &[u128]) -> Result<Vec<usize>, String> {
    if composites.len() != masks.len() {
        return Err(format!(
            "rank_order: {} composites vs {} masks",
            composites.len(),
            masks.len()
        ));
    }
    for (i, &score) in composites.iter().enumerate() {
        if !score.is_finite() {
            return Err(format!(
                "rank_order: non-finite composite score at position {i} ({score})"
            ));
        }
    }
    let mut order: Vec<usize> = (0..composites.len()).collect();
    // Stable sort (masks are unique upstream ⇒ a strict total order, so
    // stability is belt only — mirroring Python's stable Timsort).
    order.sort_by(|&i, &j| {
        let a = -composites[i];
        let b = -composites[j];
        match a.partial_cmp(&b) {
            Some(core::cmp::Ordering::Less) => core::cmp::Ordering::Less,
            Some(core::cmp::Ordering::Greater) => core::cmp::Ordering::Greater,
            // Equal — including `-0.0 == +0.0` — falls to the exact mask
            // tie-break (`None` is unreachable: finiteness validated above,
            // and treating it as a tie keeps the comparator total anyway).
            _ => masks[i].cmp(&masks[j]),
        }
    });
    Ok(order)
}

/// Twin of `brutex.ranking.overfit.passes_overfit_gate` over a batch:
/// `dsr is not None and dsr >= dsr_min` per survivor (boundary INCLUSIVE;
/// a NaN DSR fails the `>=` exactly as it does in Python).
pub fn overfit_verdicts_core(dsrs: &[Option<f64>], dsr_min: f64) -> Vec<bool> {
    dsrs.iter()
        .map(|d| match d {
            Some(v) => *v >= dsr_min,
            None => false,
        })
        .collect()
}

// ────────────────────────────────────────────────────────────────────────
// Thin #[pyfunction] wrappers — argument conversion + `map_err` ONLY
// (no math past this line; the pure cores above are the contract, and
// every wrapper's body constructs a `PyErr`, so — measured, see
// python.rs's test-section header — no `cargo test` can link a call to
// any of them: NOTHING killable may live below this banner).
// ────────────────────────────────────────────────────────────────────────

/// Batched `passes_gates` twin over scalar columns (slice-4 PR-1).
#[pyfunction]
pub fn ranking_passes_gates(
    total_trades: Vec<i64>,
    profit_factor: Vec<Option<f64>>,
    max_drawdown_pct: Vec<f64>,
    win_rate: Vec<f64>,
    win_loss_ratio: Vec<Option<f64>>,
    max_consecutive_losses: Vec<i64>,
) -> PyResult<Vec<bool>> {
    let rows = gate_rows(
        &total_trades,
        &profit_factor,
        &max_drawdown_pct,
        &win_rate,
        &win_loss_ratio,
        &max_consecutive_losses,
    )
    .map_err(PyValueError::new_err)?;
    Ok(rows.iter().map(passes_gates_core).collect())
}

/// Batched `gate_reasons` twin over scalar columns (slice-4 PR-1).
#[pyfunction]
pub fn ranking_gate_reasons(
    total_trades: Vec<i64>,
    profit_factor: Vec<Option<f64>>,
    max_drawdown_pct: Vec<f64>,
    win_rate: Vec<f64>,
    win_loss_ratio: Vec<Option<f64>>,
    max_consecutive_losses: Vec<i64>,
) -> PyResult<Vec<Vec<String>>> {
    let rows = gate_rows(
        &total_trades,
        &profit_factor,
        &max_drawdown_pct,
        &win_rate,
        &win_loss_ratio,
        &max_consecutive_losses,
    )
    .map_err(PyValueError::new_err)?;
    Ok(rows
        .iter()
        .map(|m| {
            gate_reasons_core(m)
                .into_iter()
                .map(str::to_owned)
                .collect()
        })
        .collect())
}

/// `_zscores` twin (slice-4 PR-1, class (b\*) — always-on differential).
#[pyfunction]
pub fn ranking_zscores(values: Vec<f64>) -> PyResult<Vec<f64>> {
    zscores_core(&values).map_err(PyValueError::new_err)
}

/// Batched `composite_term_inputs` twin over scalar columns.
///
/// Returns one 7-element ROW per survivor (a `list[list[float]]` on the
/// Python side — the wrapper compares/consumes by VALUE, so the row
/// container type is not load-bearing; a 7-tuple return type was measured
/// to explode the cargo-mutants value-enumeration ~45× and was
/// deliberately avoided). Pure pass-through to
/// [`composite_term_input_rows`] — the guard + assembly live in the
/// mutation-gated core above.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn ranking_composite_term_inputs(
    sharpe_ratio: Vec<Option<f64>>,
    sortino_ratio: Vec<Option<f64>>,
    calmar_ratio: Vec<Option<f64>>,
    profit_factor: Vec<Option<f64>>,
    recovery_factor: Vec<Option<f64>>,
    win_rate: Vec<f64>,
    win_loss_ratio: Vec<Option<f64>>,
    avg_mfe_paisa: Vec<i64>,
    avg_mae_paisa: Vec<i64>,
) -> PyResult<Vec<Vec<f64>>> {
    composite_term_input_rows(
        &sharpe_ratio,
        &sortino_ratio,
        &calmar_ratio,
        &profit_factor,
        &recovery_factor,
        &win_rate,
        &win_loss_ratio,
        &avg_mfe_paisa,
        &avg_mae_paisa,
    )
    .map_err(PyValueError::new_err)
}

/// `composite_scores` twin (z-normalise 7 columns + weighted fold).
#[pyfunction]
pub fn ranking_composite_scores(
    term_columns: Vec<Vec<f64>>,
    dsr_contrib: Vec<f64>,
) -> PyResult<Vec<f64>> {
    composite_scores_core(&term_columns, &dsr_contrib).map_err(PyValueError::new_err)
}

/// ACTIVE-arm `dsr_z_contributions` twin (`_zscores` × weight).
#[pyfunction]
pub fn ranking_dsr_z_contributions(dsr_by_survivor: Vec<f64>, weight: f64) -> PyResult<Vec<f64>> {
    dsr_z_contributions_core(&dsr_by_survivor, weight).map_err(PyValueError::new_err)
}

/// `rank_combos` total-order sort twin — returns the INDEX PERMUTATION.
#[pyfunction]
pub fn ranking_rank_order(composites: Vec<f64>, masks: Vec<u128>) -> PyResult<Vec<usize>> {
    rank_order_core(&composites, &masks).map_err(PyValueError::new_err)
}

/// Batched `passes_overfit_gate` twin.
#[pyfunction]
pub fn ranking_overfit_verdicts(dsrs: Vec<Option<f64>>, dsr_min: f64) -> PyResult<Vec<bool>> {
    Ok(overfit_verdicts_core(&dsrs, dsr_min))
}

/// Correctly-rounded `i64 / i64 → f64` division (M1 fast path) — exposed
/// for the Python-side hypothesis differential suite.
#[pyfunction]
pub fn ranking_cr_div_i64(numerator: i64, denominator: i64) -> PyResult<f64> {
    cr_div_i64(numerator, denominator).map_err(PyValueError::new_err)
}

// ────────────────────────────────────────────────────────────────────────
// Tests — pure-core, no Python link (the rlib-purity teeth).
// ────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use core::cmp::Ordering;

    fn bits(x: f64) -> u64 {
        x.to_bits()
    }

    fn gate_ok() -> GateInputs {
        GateInputs {
            total_trades: 50,
            profit_factor: Some(1.5),
            max_drawdown_pct: 5.0,
            win_rate: 0.6,
            win_loss_ratio: Some(1.5),
            max_consecutive_losses: 2,
        }
    }

    // ── py_min / py_max — the CPython argument-order NaN law ────────────

    #[test]
    fn py_min_nan_second_arg_returns_first() {
        assert_eq!(bits(py_min(3.0, f64::NAN)), bits(3.0));
    }

    #[test]
    fn py_min_nan_first_arg_returns_nan() {
        // CPython: min(nan, 3.0) → 3.0 < nan is False → nan (first arg).
        assert!(py_min(f64::NAN, 3.0).is_nan());
    }

    #[test]
    fn py_max_nan_second_arg_returns_first() {
        assert_eq!(bits(py_max(-3.0, f64::NAN)), bits(-3.0));
    }

    #[test]
    fn py_min_max_plain_ordering() {
        assert_eq!(bits(py_min(1.0, 2.0)), bits(1.0));
        assert_eq!(bits(py_min(2.0, 1.0)), bits(1.0));
        assert_eq!(bits(py_max(1.0, 2.0)), bits(2.0));
        assert_eq!(bits(py_max(2.0, 1.0)), bits(2.0));
    }

    #[test]
    fn py_min_max_tie_returns_first_argument_bits() {
        // min(+0.0, -0.0): -0.0 < +0.0 is False → +0.0 (first arg) —
        // exactly CPython; a tie preserves the FIRST argument's bits.
        assert_eq!(bits(py_min(0.0, -0.0)), bits(0.0));
        assert_eq!(bits(py_min(-0.0, 0.0)), bits(-0.0));
        assert_eq!(bits(py_max(0.0, -0.0)), bits(0.0));
        assert_eq!(bits(py_max(-0.0, 0.0)), bits(-0.0));
    }

    // ── cr_div_i64 ──────────────────────────────────────────────────────

    #[test]
    fn cr_div_known_values() {
        assert_eq!(bits(cr_div_i64(1, 2).unwrap()), bits(0.5));
        assert_eq!(bits(cr_div_i64(10, 4).unwrap()), bits(2.5));
        assert_eq!(bits(cr_div_i64(-1, 3).unwrap()), bits(-1.0f64 / 3.0));
        assert_eq!(bits(cr_div_i64(0, 7).unwrap()), bits(0.0));
    }

    #[test]
    fn cr_div_zero_denominator_is_rejected() {
        let err = cr_div_i64(1, 0).unwrap_err();
        assert!(err.contains("division by zero"), "{err}");
    }

    #[test]
    fn cr_div_envelope_boundary_is_inclusive() {
        let max = MAX_EXACT_DIV_OPERAND as i64;
        assert!(cr_div_i64(max, 1).is_ok());
        assert!(cr_div_i64(1, max).is_ok());
        assert!(cr_div_i64(-max, 1).is_ok());
        let err = cr_div_i64(max + 1, 1).unwrap_err();
        assert!(err.contains("2^53"), "{err}");
        let err = cr_div_i64(1, max + 1).unwrap_err();
        assert!(err.contains("2^53"), "{err}");
    }

    #[test]
    fn cr_div_i64_min_is_rejected_not_aborted() {
        // i64::MIN.unsigned_abs() is safe (no checked_abs overflow trap).
        let err = cr_div_i64(i64::MIN, 1).unwrap_err();
        assert!(err.contains("2^53"), "{err}");
    }

    // ── gates twin ──────────────────────────────────────────────────────

    #[test]
    fn gates_pass_at_every_exact_threshold() {
        // Exactly-at-threshold PASSES on every door (the Python boundary law).
        let m = GateInputs {
            total_trades: 30,
            profit_factor: Some(1.2),
            max_drawdown_pct: 30.0,
            win_rate: 0.25,
            win_loss_ratio: Some(0.0),
            max_consecutive_losses: 10,
        };
        assert!(passes_gates_core(&m));
        assert!(gate_reasons_core(&m).is_empty());
    }

    #[test]
    fn gates_reject_each_door() {
        assert!(!passes_gates_core(&GateInputs {
            total_trades: 29,
            ..gate_ok()
        }));
        assert!(!passes_gates_core(&GateInputs {
            profit_factor: None,
            ..gate_ok()
        }));
        assert!(!passes_gates_core(&GateInputs {
            profit_factor: Some(1.1999),
            ..gate_ok()
        }));
        assert!(!passes_gates_core(&GateInputs {
            max_drawdown_pct: 30.0001,
            ..gate_ok()
        }));
        assert!(!passes_gates_core(&GateInputs {
            win_rate: 0.2,
            win_loss_ratio: Some(1.9),
            ..gate_ok()
        }));
        assert!(!passes_gates_core(&GateInputs {
            win_rate: 0.2,
            win_loss_ratio: None,
            ..gate_ok()
        }));
        assert!(!passes_gates_core(&GateInputs {
            max_consecutive_losses: 11,
            ..gate_ok()
        }));
    }

    #[test]
    fn low_win_rate_with_big_wlr_passes() {
        let m = GateInputs {
            win_rate: 0.1,
            win_loss_ratio: Some(2.0),
            ..gate_ok()
        };
        assert!(passes_gates_core(&m));
    }

    #[test]
    fn non_finite_guard_fails_closed_and_masks_other_doors() {
        for poisoned in [
            GateInputs {
                profit_factor: Some(f64::NAN),
                ..gate_ok()
            },
            GateInputs {
                max_drawdown_pct: f64::INFINITY,
                ..gate_ok()
            },
            GateInputs {
                win_rate: f64::NEG_INFINITY,
                ..gate_ok()
            },
            GateInputs {
                win_loss_ratio: Some(f64::NAN),
                ..gate_ok()
            },
        ] {
            assert!(!passes_gates_core(&poisoned));
            assert_eq!(gate_reasons_core(&poisoned), vec![GATE_NON_FINITE_INPUT]);
        }
    }

    #[test]
    fn none_is_not_non_finite() {
        assert!(!non_finite_gate_input(None));
        assert!(non_finite_gate_input(Some(f64::NAN)));
        assert!(non_finite_gate_input(Some(f64::INFINITY)));
        assert!(!non_finite_gate_input(Some(1.0)));
    }

    #[test]
    fn gate_reasons_all_doors_in_order() {
        let m = GateInputs {
            total_trades: 1,
            profit_factor: Some(1.0),
            max_drawdown_pct: 90.0,
            win_rate: 0.1,
            win_loss_ratio: Some(0.5),
            max_consecutive_losses: 99,
        };
        assert_eq!(
            gate_reasons_core(&m),
            vec![
                GATE_MIN_TRADES,
                GATE_PROFIT_FACTOR,
                GATE_MAX_DRAWDOWN,
                GATE_WIN_RATE_WLR,
                GATE_CONSECUTIVE_LOSSES,
            ]
        );
        assert!(!passes_gates_core(&m));
    }

    // ── gate_rows / composite_term_input_rows — the batched row-assembly
    //    cores (round-4 review). The #[pyfunction] wrappers construct
    //    PyErr, so no cargo test can LINK a call to them (the measured
    //    extension-module boundary in python.rs) — every killable branch
    //    lives in these plain-Result cores and is mutation-gated HERE.
    //    Assertion values sit OUTSIDE every cargo-mutants replacement
    //    constant (never 0, 1, -1, empty, or default). ──────────────────

    #[test]
    fn gate_rows_assembles_every_field_positionally() {
        // Kills `gate_rows -> Ok(vec![])` (content + length asserted) and
        // every `!=`→`==` guard flip (equal-length columns MUST pass).
        let rows = gate_rows(
            &[50, 7],
            &[Some(1.5), None],
            &[5.25, 42.5],
            &[0.6, 0.35],
            &[Some(1.75), Some(9.25)],
            &[2, 12],
        )
        .unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].total_trades, 50);
        assert_eq!(rows[0].profit_factor, Some(1.5));
        assert_eq!(bits(rows[0].max_drawdown_pct), bits(5.25));
        assert_eq!(bits(rows[0].win_rate), bits(0.6));
        assert_eq!(rows[0].win_loss_ratio, Some(1.75));
        assert_eq!(rows[0].max_consecutive_losses, 2);
        assert_eq!(rows[1].total_trades, 7);
        assert_eq!(rows[1].profit_factor, None);
        assert_eq!(bits(rows[1].max_drawdown_pct), bits(42.5));
        assert_eq!(bits(rows[1].win_rate), bits(0.35));
        assert_eq!(rows[1].win_loss_ratio, Some(9.25));
        assert_eq!(rows[1].max_consecutive_losses, 12);
    }

    #[test]
    fn gate_rows_rejects_each_short_column_alone() {
        // ONE column short per case — kills every `||`→`&&` in the guard
        // chain (a && arm needs TWO simultaneous mismatches to fire).
        let tt = [50i64, 7];
        let pf = [Some(1.5), None];
        let dd = [5.25, 42.5];
        let wr = [0.6, 0.35];
        let wlr = [Some(1.75), Some(9.25)];
        let mcl = [2i64, 12];
        let expect_err = |r: Result<Vec<GateInputs>, String>| {
            assert_eq!(r.unwrap_err(), "ranking gates: unequal column lengths");
        };
        expect_err(gate_rows(&tt, &pf[..1], &dd, &wr, &wlr, &mcl));
        expect_err(gate_rows(&tt, &pf, &dd[..1], &wr, &wlr, &mcl));
        expect_err(gate_rows(&tt, &pf, &dd, &wr[..1], &wlr, &mcl));
        expect_err(gate_rows(&tt, &pf, &dd, &wr, &wlr[..1], &mcl));
        expect_err(gate_rows(&tt, &pf, &dd, &wr, &wlr, &mcl[..1]));
    }

    #[test]
    fn gate_rows_empty_columns_yield_an_empty_ok() {
        // The n = 0 boundary: every guard arm compares 0 == 0 → Ok([]).
        assert_eq!(gate_rows(&[], &[], &[], &[], &[], &[]).unwrap().len(), 0);
    }

    #[test]
    fn composite_term_input_rows_matches_the_per_row_core() {
        // Kills the fabricated `-> Ok(vec![…])` replacements (content +
        // length) and every `!=`→`==` guard flip; row values chosen so no
        // projected term is a cargo-mutants constant (row 0 term 7 is
        // 750/300 = 2.5, term 4 is 1.8 − 1.0 = 0.8, …).
        let rows = composite_term_input_rows(
            &[Some(1.25), None],
            &[Some(0.75), Some(-2.5)],
            &[None, Some(3.5)],
            &[Some(1.8), Some(0.6)],
            &[Some(4.5), None],
            &[0.6, 0.35],
            &[Some(2.5), None],
            &[750, -125],
            &[300, 0],
        )
        .unwrap();
        let expected: Vec<Vec<f64>> = [
            TermInputs {
                sharpe_ratio: Some(1.25),
                sortino_ratio: Some(0.75),
                calmar_ratio: None,
                profit_factor: Some(1.8),
                recovery_factor: Some(4.5),
                win_rate: 0.6,
                win_loss_ratio: Some(2.5),
                avg_mfe_paisa: 750,
                avg_mae_paisa: 300,
            },
            TermInputs {
                sharpe_ratio: None,
                sortino_ratio: Some(-2.5),
                calmar_ratio: Some(3.5),
                profit_factor: Some(0.6),
                recovery_factor: None,
                win_rate: 0.35,
                win_loss_ratio: None,
                avg_mfe_paisa: -125,
                avg_mae_paisa: 0,
            },
        ]
        .iter()
        .map(|m| composite_term_inputs_core(m).unwrap().to_vec())
        .collect();
        let as_bits = |rs: &[Vec<f64>]| -> Vec<Vec<u64>> {
            rs.iter()
                .map(|r| r.iter().map(|&v| bits(v)).collect())
                .collect()
        };
        assert_eq!(as_bits(&rows), as_bits(&expected));
        // Landmark spot-values (independent of the core call above).
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].len(), N_COMPOSITE_TERMS);
        assert_eq!(bits(rows[0][6]), bits(2.5)); // 750 / 300
        assert_eq!(bits(rows[0][3]), bits(1.8 - 1.0)); // PF − 1.0
        assert_eq!(bits(rows[1][5]), bits(0.35 * 0.0)); // None wlr → 0.0
    }

    #[test]
    fn composite_term_input_rows_rejects_each_short_column_alone() {
        // ONE column short per case — kills every `||`→`&&` guard flip.
        let sh = [Some(1.25), None];
        let so = [Some(0.75), Some(-2.5)];
        let ca = [None, Some(3.5)];
        let pf = [Some(1.8), Some(0.6)];
        let re = [Some(4.5), None];
        let wr = [0.6, 0.35];
        let wl = [Some(2.5), None];
        let mfe = [750i64, -125];
        let mae = [300i64, 0];
        let expect_err = |r: Result<Vec<Vec<f64>>, String>| {
            assert_eq!(
                r.unwrap_err(),
                "ranking_composite_term_inputs: unequal column lengths"
            );
        };
        expect_err(composite_term_input_rows(
            &sh,
            &so[..1],
            &ca,
            &pf,
            &re,
            &wr,
            &wl,
            &mfe,
            &mae,
        ));
        expect_err(composite_term_input_rows(
            &sh,
            &so,
            &ca[..1],
            &pf,
            &re,
            &wr,
            &wl,
            &mfe,
            &mae,
        ));
        expect_err(composite_term_input_rows(
            &sh,
            &so,
            &ca,
            &pf[..1],
            &re,
            &wr,
            &wl,
            &mfe,
            &mae,
        ));
        expect_err(composite_term_input_rows(
            &sh,
            &so,
            &ca,
            &pf,
            &re[..1],
            &wr,
            &wl,
            &mfe,
            &mae,
        ));
        expect_err(composite_term_input_rows(
            &sh,
            &so,
            &ca,
            &pf,
            &re,
            &wr[..1],
            &wl,
            &mfe,
            &mae,
        ));
        expect_err(composite_term_input_rows(
            &sh,
            &so,
            &ca,
            &pf,
            &re,
            &wr,
            &wl[..1],
            &mfe,
            &mae,
        ));
        expect_err(composite_term_input_rows(
            &sh,
            &so,
            &ca,
            &pf,
            &re,
            &wr,
            &wl,
            &mfe[..1],
            &mae,
        ));
        expect_err(composite_term_input_rows(
            &sh,
            &so,
            &ca,
            &pf,
            &re,
            &wr,
            &wl,
            &mfe,
            &mae[..1],
        ));
    }

    #[test]
    fn composite_term_input_rows_propagates_the_envelope_error() {
        // An out-of-envelope paisa magnitude aborts the WHOLE call via `?`
        // (the Python wrapper then serves the call from the oracle).
        let big = (1i64 << 53) + 1;
        let err = composite_term_input_rows(
            &[Some(1.25)],
            &[None],
            &[None],
            &[None],
            &[None],
            &[0.5],
            &[None],
            &[big],
            &[3],
        )
        .unwrap_err();
        assert!(err.contains("2^53"), "{err}");
    }

    // ── py_float_sum — the CPython-3.12 builtin-sum transliteration ─────

    #[test]
    fn py_float_sum_is_compensated_not_naive() {
        // CPython 3.12: sum([1e16, 1.0, -1e16]) == 1.0 (Neumaier); the
        // naive fold loses the 1.0 to rounding and yields 0.0.
        let compensated = py_float_sum([1e16, 1.0, -1e16].into_iter());
        assert_eq!(bits(compensated), bits(1.0));
        let naive = [1e16f64, 1.0, -1e16].iter().fold(0.0, |a, &v| a + v);
        assert_eq!(bits(naive), bits(0.0)); // the trap this fold exists for
    }

    #[test]
    fn py_float_sum_constant_column_mean_is_exact() {
        // The live-caught divergence shape: 200 × 0.8999999999999999 must
        // average back to EXACTLY the element (CPython does), so the
        // zscores variance short-circuit fires identically.
        let v = 0.8999999999999999f64;
        let total = py_float_sum(core::iter::repeat_n(v, 200));
        assert_eq!(bits(total / 200.0), bits(v));
        let z = zscores_core(&[v; 200]).unwrap();
        assert!(z.iter().all(|&x| bits(x) == bits(0.0)));
    }

    #[test]
    fn py_float_sum_infinite_total_is_not_converted_to_nan() {
        // The CPython exit guard: a non-finite compensation never turns an
        // infinite sum into NaN.
        let total = py_float_sum([f64::INFINITY, 1.0].into_iter());
        assert_eq!(bits(total), bits(f64::INFINITY));
        // inf + -inf inside the fold IS NaN on both sides (Python: nan).
        assert!(py_float_sum([f64::INFINITY, f64::NEG_INFINITY].into_iter()).is_nan());
    }

    #[test]
    fn py_float_sum_zero_compensation_preserves_the_running_sign() {
        // c == ±0.0 must SKIP the final add: sum([-0.0, -0.0]) is +0.0 in
        // CPython (the 0.0 start), and a genuinely negative-zero running
        // total must not be flipped by adding +0.0.
        assert_eq!(bits(py_float_sum([-0.0f64, -0.0].into_iter())), bits(0.0));
        assert_eq!(bits(py_float_sum(core::iter::empty())), bits(0.0));
    }

    // ── zscores twin ────────────────────────────────────────────────────

    #[test]
    fn zscores_symmetric_pair_is_exactly_plus_minus_one() {
        let z = zscores_core(&[1.0, 3.0]).unwrap();
        assert_eq!(bits(z[0]), bits(-1.0));
        assert_eq!(bits(z[1]), bits(1.0));
    }

    #[test]
    fn zscores_zero_variance_short_circuits_to_zeros() {
        let z = zscores_core(&[2.5, 2.5, 2.5]).unwrap();
        assert_eq!(z, vec![0.0, 0.0, 0.0]);
        // Single survivor: variance 0.0 by construction.
        assert_eq!(zscores_core(&[7.0]).unwrap(), vec![0.0]);
    }

    #[test]
    fn zscores_extreme_outlier_clamps_at_exactly_three() {
        let mut values = vec![0.0; 100];
        values.push(1_000_000.0);
        let z = zscores_core(&values).unwrap();
        assert_eq!(bits(z[100]), bits(3.0));
    }

    #[test]
    fn zscores_nan_input_clamps_to_exactly_positive_three() {
        // NaN propagates through mean/std into every z; the py_min/py_max
        // clamp maps NaN → exactly +3.0 (the CPython argument-order law).
        let z = zscores_core(&[1.0, f64::NAN, 2.0]).unwrap();
        for v in z {
            assert_eq!(bits(v), bits(3.0));
        }
    }

    #[test]
    fn zscores_pos_inf_clamps_to_plus_three_neg_inf_to_minus_three() {
        // inf in the pool → mean/variance NaN → all-NaN z → all +3.0
        // (matches Python: (v-mean)/std with NaN std is NaN everywhere).
        let z = zscores_core(&[f64::INFINITY, 0.0]).unwrap();
        for v in z {
            assert_eq!(bits(v), bits(3.0));
        }
    }

    #[test]
    fn zscores_empty_is_rejected() {
        let err = zscores_core(&[]).unwrap_err();
        assert!(err.contains("empty input"), "{err}");
    }

    // ── composite fold twin ─────────────────────────────────────────────

    fn seven_columns(n: usize) -> Vec<Vec<f64>> {
        (0..N_COMPOSITE_TERMS)
            .map(|t| (0..n).map(|i| (i as f64) * 0.5 + (t as f64)).collect())
            .collect()
    }

    #[test]
    fn composite_wrong_column_count_is_rejected_with_python_message() {
        let err = composite_scores_core(&seven_columns(2)[..6], &[0.0, 0.0]).unwrap_err();
        assert_eq!(err, "composite_scores expects 7 term columns, got 6");
    }

    #[test]
    fn composite_short_column_is_rejected() {
        let mut cols = seven_columns(2);
        cols[3] = vec![1.0];
        let err = composite_scores_core(&cols, &[0.0, 0.0]).unwrap_err();
        assert!(err.contains("term column 3"), "{err}");
    }

    #[test]
    fn composite_fold_matches_the_literal_left_chain() {
        let cols = seven_columns(3);
        let dsr = [0.01, -0.02, 0.03];
        let got = composite_scores_core(&cols, &dsr).unwrap();
        let z: Vec<Vec<f64>> = cols.iter().map(|c| zscores_core(c).unwrap()).collect();
        for i in 0..3 {
            let expected = 0.25 * z[0][i]
                + 0.20 * z[1][i]
                + 0.15 * z[2][i]
                + 0.15 * z[3][i]
                + 0.10 * z[4][i]
                + 0.10 * z[5][i]
                + 0.15 * z[6][i]
                + dsr[i];
            assert_eq!(bits(got[i]), bits(expected));
        }
    }

    #[test]
    fn composite_empty_dsr_with_nonempty_columns_returns_empty() {
        // Python z-scores ALL columns first, then folds over range(0) → [].
        let cols = seven_columns(2);
        assert_eq!(
            composite_scores_core(&cols, &[]).unwrap(),
            Vec::<f64>::new()
        );
    }

    #[test]
    fn composite_empty_column_is_rejected_like_pythons_zerodivision() {
        let cols: Vec<Vec<f64>> = (0..N_COMPOSITE_TERMS).map(|_| Vec::new()).collect();
        assert!(composite_scores_core(&cols, &[]).is_err());
    }

    // ── term inputs twin ────────────────────────────────────────────────

    #[test]
    fn term_inputs_project_none_to_zero_and_divide_exactly() {
        let terms = composite_term_inputs_core(&TermInputs {
            sharpe_ratio: None,
            sortino_ratio: Some(1.5),
            calmar_ratio: None,
            profit_factor: None,
            recovery_factor: None,
            win_rate: 0.5,
            win_loss_ratio: None,
            avg_mfe_paisa: 300,
            avg_mae_paisa: 0, // floored to 1
        })
        .unwrap();
        assert_eq!(bits(terms[0]), bits(0.0));
        assert_eq!(bits(terms[1]), bits(1.5));
        assert_eq!(bits(terms[3]), bits(-1.0)); // 0.0 - 1.0
        assert_eq!(bits(terms[5]), bits(0.0)); // 0.5 * 0.0
        assert_eq!(bits(terms[6]), bits(300.0)); // 300 / max(0, 1)
    }

    #[test]
    fn term_inputs_beyond_envelope_is_rejected() {
        let err = composite_term_inputs_core(&TermInputs {
            sharpe_ratio: Some(1.0),
            sortino_ratio: Some(1.0),
            calmar_ratio: Some(1.0),
            profit_factor: Some(1.5),
            recovery_factor: Some(1.0),
            win_rate: 0.5,
            win_loss_ratio: Some(1.0),
            avg_mfe_paisa: (1i64 << 53) + 1,
            avg_mae_paisa: 1,
        })
        .unwrap_err();
        assert!(err.contains("2^53"), "{err}");
    }

    // ── dsr z twin ──────────────────────────────────────────────────────

    #[test]
    fn dsr_z_is_zscores_times_weight() {
        let got = dsr_z_contributions_core(&[1.0, 3.0], 0.05).unwrap();
        // The symmetric pair z-scores to exactly ∓1.0 (pinned above), so the
        // weighted terms are exactly `weight * z` — written as the runtime
        // multiplication (weight-first operand order, the Python token order).
        let (z_lo, z_hi) = (-1.0f64, 1.0f64);
        assert_eq!(bits(got[0]), bits(0.05 * z_lo));
        assert_eq!(bits(got[1]), bits(0.05 * z_hi));
    }

    // ── rank order twin ─────────────────────────────────────────────────

    #[test]
    fn rank_order_descending_score_then_ascending_mask() {
        let composites = [0.5, 2.0, 0.5, -1.0];
        let masks = [8u128, 1, 2, 4];
        let order = rank_order_core(&composites, &masks).unwrap();
        // 2.0 first (idx 1); tie 0.5/0.5 → mask 2 (idx 2) before mask 8
        // (idx 0); -1.0 last.
        assert_eq!(order, vec![1, 2, 0, 3]);
    }

    #[test]
    fn rank_order_signed_zero_scores_compare_equal() {
        // -(-0.0) = +0.0 and -(+0.0) = -0.0; the comparator MUST treat them
        // equal (Python tuple compare does) and fall to the mask tie-break.
        let composites = [0.0, -0.0];
        let masks = [2u128, 1];
        let order = rank_order_core(&composites, &masks).unwrap();
        assert_eq!(order, vec![1, 0]); // mask 1 first — never split by sign
    }

    #[test]
    fn rank_order_length_mismatch_is_rejected() {
        let err = rank_order_core(&[1.0], &[1u128, 2]).unwrap_err();
        assert!(err.contains("1 composites vs 2 masks"), "{err}");
    }

    #[test]
    fn rank_order_non_finite_score_is_rejected() {
        for bad in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let err = rank_order_core(&[0.0, bad], &[1u128, 2]).unwrap_err();
            assert!(err.contains("non-finite composite"), "{err}");
        }
    }

    #[test]
    fn rank_order_empty_is_empty() {
        assert_eq!(rank_order_core(&[], &[]).unwrap(), Vec::<usize>::new());
    }

    #[test]
    fn rank_order_u128_masks_beyond_u64_tiebreak_exactly() {
        let composites = [1.0, 1.0];
        let masks = [(1u128 << 100) + 1, 1u128 << 100];
        let order = rank_order_core(&composites, &masks).unwrap();
        assert_eq!(order, vec![1, 0]);
    }

    // ── overfit verdicts twin ───────────────────────────────────────────

    #[test]
    fn overfit_verdict_boundary_inclusive_none_and_nan_fail() {
        let verdicts = overfit_verdicts_core(
            &[Some(0.95), Some(0.9499999), None, Some(f64::NAN), Some(1.0)],
            0.95,
        );
        assert_eq!(verdicts, vec![true, false, false, false, true]);
    }

    // ── comparator-law proptests ────────────────────────────────────────

    use proptest::prelude::*;

    proptest! {
        #[test]
        fn prop_twin_law_passes_iff_no_reasons(
            total_trades in 0i64..100,
            pf in proptest::option::of(-2.0f64..4.0),
            dd in 0.0f64..60.0,
            wr in 0.0f64..1.0,
            wlr in proptest::option::of(0.0f64..4.0),
            losses in 0i64..20,
        ) {
            let m = GateInputs {
                total_trades,
                profit_factor: pf,
                max_drawdown_pct: dd,
                win_rate: wr,
                win_loss_ratio: wlr,
                max_consecutive_losses: losses,
            };
            prop_assert_eq!(passes_gates_core(&m), gate_reasons_core(&m).is_empty());
        }

        #[test]
        fn prop_rank_order_is_a_permutation_sorted_by_the_key_law(
            scores in proptest::collection::vec(-1000.0f64..1000.0, 0..24),
        ) {
            // Unique masks (ascending by construction — index-derived).
            let masks: Vec<u128> = (0..scores.len() as u128).collect();
            let order = rank_order_core(&scores, &masks).unwrap();
            let mut seen = order.clone();
            seen.sort_unstable();
            prop_assert_eq!(seen, (0..scores.len()).collect::<Vec<_>>());
            for w in order.windows(2) {
                let (i, j) = (w[0], w[1]);
                let a = -scores[i];
                let b = -scores[j];
                let ok = match a.partial_cmp(&b) {
                    Some(Ordering::Less) => true,
                    Some(Ordering::Greater) => false,
                    _ => masks[i] < masks[j],
                };
                prop_assert!(ok, "order violates the (-score, mask) law");
            }
        }

        #[test]
        fn prop_zscores_are_clamped_or_exactly_zero(
            values in proptest::collection::vec(-1e12f64..1e12, 1..32),
        ) {
            let z = zscores_core(&values).unwrap();
            prop_assert_eq!(z.len(), values.len());
            for v in z {
                prop_assert!((-3.0..=3.0).contains(&v));
            }
        }

        #[test]
        fn prop_cr_div_in_envelope_equals_hardware_division(
            a in -(1i64 << 53)..=(1i64 << 53),
            b in 1i64..=(1i64 << 53),
        ) {
            // Inside the envelope the fast path IS the hardware division of
            // exactly-converted operands (the correctness proof is that the
            // casts are exact; asserted here as a consistency pin).
            let got = cr_div_i64(a, b).unwrap();
            prop_assert_eq!(got.to_bits(), ((a as f64) / (b as f64)).to_bits());
        }
    }
}
