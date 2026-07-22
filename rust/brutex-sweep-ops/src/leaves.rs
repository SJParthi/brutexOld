//! Slice 1 — pure leaf laws (DEC-RUST-SWEEP-OPS-001).
//!
//! Token-for-token Rust twins of three PURE Python leaf laws, each retained
//! in Python as the live parity oracle and routed through the default-OFF
//! `brutex/engine/sweep_ops_rust.py` facade with a 16-call burn-in
//! bit-compare + ship-the-oracle autorevert:
//!
//! 1. [`join_path_segments`] — `brutex/engine/run_layout.py`'s path-join law
//!    (`run_dir` + both `mirror_run_dir` joins). POSIX-only, mirroring
//!    `pathlib.Path.__truediv__` for the shapes the run layout ever joins
//!    (relative, non-empty, slash-free segments — the facade's exact-type
//!    gates refuse anything else to the oracle). The facade re-wraps the
//!    returned string in `Path(...)`, whose constructor normalisation makes
//!    the result object-equal to the chained-`/` oracle for every accepted
//!    input shape (burn-in-verified, autoreverted on any divergence).
//! 2. [`gate_config_fragment`] — `brutex/engine/sweep_identity.py`'s
//!    gate-config identity-fragment EMISSION law (overfit leg iff the gate is
//!    set; stage3 leg iff the pass is enabled; empty ⇒ `None`). Values cross
//!    the FFI as `f64`/`i64` (bit-exact roundtrip — the greeks_batch
//!    precedent); the facade's exact-type gates (`float`/`int`, `bool`
//!    excluded) refuse anything lossy to the oracle.
//! 3. [`stall_scaled_timeout`] — `brutex/engine/sweep_stall_policy.py`'s
//!    raise-only scaled-timeout tail: `scaled = factor × estimate`; above the
//!    base ⇒ `(scaled, true)`, else `(base, false)`. The fail-loud env parses
//!    + `StallPolicy` construction stay Python.
//!
//! Slice 1b — the deferred leaf batch (same machinery, same facade):
//!
//! 4. [`progress_derived`] — `brutex/engine/sweep_progress.py`'s snapshot
//!    derived-metrics cluster (percent · elapsed · combos/sec · ETA ·
//!    survivor rate). Counts cross as `i64` gated `|v| ≤ 2^53` Python-side so
//!    CPython's correctly-rounded `int / int` true division and the `as f64`
//!    conversions here are provably identical; the CPython `round(x, 1)`
//!    (correctly-rounded decimal, ties-to-even) is realised via Rust's
//!    fixed-precision formatting + exact parse-back (see
//!    [`round_half_even_1`]).
//! 5. [`sla_decision`] — `brutex/engine/sweep_sla_guard.py`'s projection
//!    decision (`projected = tuple_count × per_tuple_seconds` → allowed /
//!    acknowledged / refused). Env parses, validation raises, message
//!    rendering and the structlog event stay Python.
//! 6. [`rank_plan_decision`] — `brutex/engine/sweep_mem_budget.py`'s
//!    `plan_rank_memory` decision table (`projected = n_masks × 1024` in
//!    exact `i128` → batch / bounded / one of three named refusals). The
//!    mode `on` config raises + the refusal message rendering stay Python.
//! 7. [`preflight_missing`] — `brutex/engine/sweep_preflight.py`'s zero-bar
//!    pair fold (request-order symbols × intervals against the pre-loaded
//!    slice sizes).
//! 8. [`freshness_stale`] — `brutex/engine/sweep_preflight.py`'s
//!    derived-freshness fold (anything not exactly `"fresh"` is stale,
//!    sorted — fail-closed). UTF-8 byte order == code-point order, so the
//!    sort matches Python's `sorted` over `(str, str)` pairs.
//! 9. [`liveness_tail`] — `brutex/engine/sweep_liveness.py`'s
//!    `progress_verdict` decision tail AFTER the Python-side payload typing
//!    and the `os.kill` pid probe: dead pid ⇒ `not_running`; strictly past
//!    the freshness window ⇒ `stale`; else `running`. The fail-closed
//!    UNKNOWN arm (the G4 law — Python-type introspection of an untyped
//!    payload) stays Python by construction.

/// The results-root subdirectory hosting every per-run artifact — the Rust
/// twin of `brutex.engine.run_layout.RUN_DIR_SUBDIR` (drift-pinned by the
/// Python parity suite).
pub const RUN_DIR_SUBDIR: &str = "runs";

/// Join `segments` onto `base` with single `/` separators (POSIX).
///
/// The `pathlib`-join law for the run-layout shapes: an empty `base` yields a
/// relative join (never a leading `/`); a `base` already ending in `/` gains
/// no second separator. Segments are assumed relative, non-empty and
/// slash-free — the Python facade refuses anything else to the oracle before
/// this function is ever consulted.
pub fn join_path_segments(base: &str, segments: &[String]) -> String {
    let mut acc = String::from(base);
    for segment in segments {
        if !acc.is_empty() && !acc.ends_with('/') {
            acc.push('/');
        }
        acc.push_str(segment);
    }
    acc
}

/// The gate-config identity-fragment emission law
/// (`sweep_identity._gate_config_fragment`).
///
/// * `overfit` — the four gate knobs `(dsr_min, weight, default_skew,
///   default_excess_kurtosis)`, or `None` when the overfit gate is off.
/// * `stage3_enable` + the three stage-3 knobs — the stage3 leg is emitted
///   ONLY when the pass is enabled (a disabled pass's knobs are inert and
///   never re-key a light grid — the byte-stable-at-defaults law).
///
/// Returns `(overfit_leg, stage3_leg)`; both `None` ⇒ the Python facade
/// renders the whole fragment as `None` (the `fragment or None` tail).
#[allow(clippy::type_complexity)] // The FFI tuple IS the law's shape.
pub fn gate_config_fragment(
    overfit: Option<(f64, f64, f64, f64)>,
    stage3_enable: bool,
    stage3_top_k: i64,
    stage3_max_fevals: i64,
    stage3_max_restarts: i64,
) -> (Option<(f64, f64, f64, f64)>, Option<(i64, i64, i64)>) {
    let stage3 = if stage3_enable {
        Some((stage3_top_k, stage3_max_fevals, stage3_max_restarts))
    } else {
        None
    };
    (overfit, stage3)
}

/// The raise-only scaled stall-timeout tail
/// (`sweep_stall_policy.resolve_stall_policy`'s final clause).
///
/// `scaled = factor × estimate`; strictly above `base` ⇒ `(scaled, true)`
/// (source `"scaled"`), else `(base, false)` (source `"default"`). Scaling
/// only ever RAISES the timeout above the base — the G2 law.
pub fn stall_scaled_timeout(factor: f64, estimate: f64, base: f64) -> (f64, bool) {
    let scaled = factor * estimate;
    if scaled > base {
        (scaled, true)
    } else {
        (base, false)
    }
}

// ───────────────────── slice 1b — the deferred leaf batch ─────────────────────

/// The batch-rank retention model's bytes-per-kept-mask constant — the Rust
/// twin of `brutex.engine.sweep_mem_budget.BATCH_RANK_BYTES_PER_KEPT`
/// (drift-pinned by the Python parity suite).
pub const BATCH_RANK_BYTES_PER_KEPT: i128 = 1024;

/// CPython `round(value, 1)`: correctly-rounded decimal rounding at ONE
/// fractional digit with ties-to-even, returned as the nearest double.
///
/// Realised via Rust's fixed-precision formatting (`flt2dec` is correctly
/// rounded with ties-to-even — the same law as CPython's `_Py_dg_dtoa` mode
/// 3) followed by the exact parse-back (`strtod`'s correctly-rounded twin).
/// The `unwrap_or` arm is unreachable for any finite input (the formatter's
/// output always re-parses) — a pure belt, never a behavior fork.
fn round_half_even_1(value: f64) -> f64 {
    format!("{value:.1}").parse::<f64>().unwrap_or(value)
}

/// The sweep-progress snapshot derived-metrics cluster
/// (`sweep_progress.SweepProgressSnapshot`'s five derived properties).
///
/// Returns `(percent, elapsed_seconds, combos_per_sec, eta_seconds,
/// survivor_rate)` — each arm token-for-token the Python property law. The
/// facade gates every count to `|v| ≤ 2^53` (exact-int band) so the `as f64`
/// conversions and divisions here are bit-identical to CPython's
/// correctly-rounded `int / int` true division.
pub fn progress_derived(
    enumerated: i64,
    evaluated: i64,
    survivors: i64,
    started_at: f64,
    updated_at: f64,
) -> (f64, f64, f64, Option<f64>, f64) {
    let percent = if enumerated <= 0 {
        0.0
    } else {
        let rounded = round_half_even_1(evaluated as f64 / enumerated as f64 * 100.0);
        // Python `min(100.0, rounded)`: rounded wins iff strictly below 100.
        if rounded < 100.0 { rounded } else { 100.0 }
    };
    let raw_elapsed = updated_at - started_at;
    let elapsed = if raw_elapsed > 0.0 { raw_elapsed } else { 0.0 };
    let rate = if elapsed <= 0.0 || evaluated <= 0 {
        0.0
    } else {
        evaluated as f64 / elapsed
    };
    let eta = if rate <= 0.0 || enumerated <= 0 {
        None
    } else {
        let remaining = enumerated - evaluated;
        if remaining <= 0 {
            Some(0.0)
        } else {
            Some(remaining as f64 / rate)
        }
    };
    let survivor_rate = if evaluated <= 0 {
        0.0
    } else {
        survivors as f64 / evaluated as f64
    };
    (percent, elapsed, rate, eta, survivor_rate)
}

/// The SLA launch-guard projection decision
/// (`sweep_sla_guard.check_sweep_sla`'s decision clause).
///
/// `projected = tuple_count × per_tuple_seconds` (the `as f64` conversion is
/// round-nearest — identical to CPython's `int * float` conversion for every
/// `i64`); within the envelope ⇒ `"allowed"`, past it with the operator
/// acknowledgement ⇒ `"acknowledged"`, else `"refused"` (the
/// `SlaDecision` wire values — drift-pinned by the Python parity suite).
pub fn sla_decision(
    tuple_count: i64,
    per_tuple_seconds: f64,
    envelope_seconds: f64,
    acknowledge: bool,
) -> (f64, &'static str) {
    let projected = tuple_count as f64 * per_tuple_seconds;
    let decision = if projected <= envelope_seconds {
        "allowed"
    } else if acknowledge {
        "acknowledged"
    } else {
        "refused"
    };
    (projected, decision)
}

/// The rank-memory pre-flight decision table
/// (`sweep_mem_budget.plan_rank_memory`'s pure decision — the mode-`on`
/// config raises and the refusal message RENDERING stay Python).
///
/// `projected = n_masks × 1024` computed in `i128` (exact for every `i64`
/// input — CPython's arbitrary-precision `int` twin). Verdict tokens:
/// `"bounded"` (mode `on` — its digest/top-k config raises already fired
/// Python-side) · `"batch"` (within budget) · `"refuse_off"` /
/// `"refuse_no_top_k"` / `"refuse_no_digest"` (the three named refusal arms,
/// in the Python law's exact branch order). An unknown *mode* token returns
/// `None` (the pyfunction maps it to `ValueError` — the facade degrades to
/// the oracle; the Python wiring gates the token, so this arm is a belt).
pub fn rank_plan_decision(
    n_masks: i64,
    budget_bytes: i64,
    mode: &str,
    has_top_k: bool,
    digest_path: bool,
) -> Option<(i128, &'static str)> {
    let projected = i128::from(n_masks) * BATCH_RANK_BYTES_PER_KEPT;
    let verdict = match mode {
        "on" => "bounded",
        "off" | "auto" => {
            if projected <= i128::from(budget_bytes) {
                "batch"
            } else if mode == "off" {
                "refuse_off"
            } else if !has_top_k {
                "refuse_no_top_k"
            } else if !digest_path {
                "refuse_no_digest"
            } else {
                "bounded"
            }
        }
        _ => return None,
    };
    Some((projected, verdict))
}

/// The sweep-preflight zero-bar pair fold (`sweep_preflight.preflight_slices`).
///
/// *sizes* is the pre-loaded `(symbol, interval) → bar count` mapping
/// flattened to triples (a Python `Mapping` cannot carry duplicate keys, so
/// the rebuild is collision-free); a requested pair absent from the mapping
/// counts as zero — exactly the Python `slice_sizes.get(pair, 0) == 0` law.
/// Output order: symbols in request order, intervals in request order within
/// each symbol (the Python comprehension's deterministic order).
pub fn preflight_missing(
    sizes: &[(String, String, i64)],
    symbols: &[String],
    intervals: &[String],
) -> Vec<(String, String)> {
    let map: std::collections::HashMap<(&str, &str), i64> = sizes
        .iter()
        .map(|(symbol, interval, count)| ((symbol.as_str(), interval.as_str()), *count))
        .collect();
    let mut missing = Vec::new();
    for symbol in symbols {
        for interval in intervals {
            if map
                .get(&(symbol.as_str(), interval.as_str()))
                .copied()
                .unwrap_or(0)
                == 0
            {
                missing.push((symbol.clone(), interval.clone()));
            }
        }
    }
    missing
}

/// The derived-freshness stale fold
/// (`sweep_preflight.preflight_derived_freshness`).
///
/// Anything not EXACTLY `"fresh"` counts stale (fail-closed — an unexpected
/// verdict string can never silently pass), sorted ascending. `String`'s
/// byte-wise `Ord` over UTF-8 equals Python's code-point `str` ordering, so
/// the sort is byte-identical to `sorted(...)` over `(str, str)` pairs.
pub fn freshness_stale(verdicts: &[(String, String, String)]) -> Vec<(String, String)> {
    let mut stale: Vec<(String, String)> = verdicts
        .iter()
        .filter(|(_, _, verdict)| verdict != "fresh")
        .map(|(symbol, interval, _)| (symbol.clone(), interval.clone()))
        .collect();
    stale.sort();
    stale
}

/// The sweep-liveness verdict tail (`sweep_liveness.progress_verdict` steps
/// 3-5, AFTER the Python-side payload typing + the `running is False` arm +
/// the `os.kill` pid probe).
///
/// Dead pid ⇒ `"not_running"`; strictly past the freshness window ⇒
/// `"stale"`; else `"running"` (the `RunVerdict` wire values — drift-pinned
/// by the Python parity suite). The fail-closed UNKNOWN arm never reaches
/// this tail by construction.
pub fn liveness_tail(pid_ok: bool, now_wall: f64, wall: f64, fresh_seconds: f64) -> &'static str {
    if !pid_ok {
        return "not_running";
    }
    if now_wall - wall > fresh_seconds {
        return "stale";
    }
    "running"
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn join_plain_root() {
        let segs = vec!["runs".to_string(), "abc123".to_string()];
        assert_eq!(
            join_path_segments("/lake/sweeps", &segs),
            "/lake/sweeps/runs/abc123"
        );
    }

    #[test]
    fn join_trailing_slash_root_gains_no_double_separator() {
        let segs = vec!["runs".to_string(), "abc".to_string()];
        assert_eq!(
            join_path_segments("/lake/sweeps/", &segs),
            "/lake/sweeps/runs/abc"
        );
    }

    #[test]
    fn join_empty_base_stays_relative() {
        let segs = vec!["runs".to_string(), "abc".to_string()];
        assert_eq!(join_path_segments("", &segs), "runs/abc");
    }

    #[test]
    fn join_single_segment() {
        let segs = vec!["abc".to_string()];
        assert_eq!(join_path_segments("/mirror", &segs), "/mirror/abc");
    }

    #[test]
    fn join_root_base() {
        // A bare "/" base already ends with the separator.
        let segs = vec!["runs".to_string(), "id".to_string()];
        assert_eq!(join_path_segments("/", &segs), "/runs/id");
    }

    #[test]
    fn run_dir_subdir_is_runs() {
        assert_eq!(RUN_DIR_SUBDIR, "runs");
    }

    #[test]
    fn gate_fragment_both_off_is_empty() {
        assert_eq!(gate_config_fragment(None, false, 1, 2, 3), (None, None));
    }

    #[test]
    fn gate_fragment_overfit_only() {
        let overfit = Some((0.95, 0.05, 0.0, 0.0));
        assert_eq!(
            gate_config_fragment(overfit, false, 1, 2, 3),
            (overfit, None)
        );
    }

    #[test]
    fn gate_fragment_stage3_only_carries_knobs() {
        assert_eq!(
            gate_config_fragment(None, true, 10, 200, 3),
            (None, Some((10, 200, 3)))
        );
    }

    #[test]
    fn gate_fragment_both_legs() {
        let overfit = Some((0.9, 0.1, -0.5, 2.0));
        assert_eq!(
            gate_config_fragment(overfit, true, 5, 100, 2),
            (overfit, Some((5, 100, 2)))
        );
    }

    #[test]
    fn stall_scaled_above_base() {
        assert_eq!(stall_scaled_timeout(10.0, 60.0, 300.0), (600.0, true));
    }

    #[test]
    fn stall_at_base_boundary_is_default() {
        // scaled == base is NOT strictly above — the default arm (the I-1
        // identity: 10.0 × 30.0 == 300.0 resolves to the unscaled base).
        assert_eq!(stall_scaled_timeout(10.0, 30.0, 300.0), (300.0, false));
    }

    #[test]
    fn stall_below_base_floors_at_base() {
        assert_eq!(stall_scaled_timeout(10.0, 1.0, 300.0), (300.0, false));
    }

    // ─────────────────── slice 1b — deferred leaf batch ───────────────────

    #[test]
    fn round1_ties_to_even() {
        // 0.25 is exactly representable — a TRUE decimal tie at 1 digit.
        // CPython round(0.25, 1) == 0.2 (half-even); round(0.75, 1) == 0.8.
        assert_eq!(round_half_even_1(0.25), 0.2);
        assert_eq!(round_half_even_1(0.75), 0.8);
        // 0.35 is stored below the tie (0.34999…) — correct rounding, 0.3.
        assert_eq!(round_half_even_1(0.35), 0.3);
        // Already at 1 fractional digit — identity.
        assert_eq!(round_half_even_1(12.5), 12.5);
    }

    #[test]
    fn progress_derived_idle_snapshot_is_all_zero() {
        assert_eq!(
            progress_derived(0, 0, 0, 0.0, 0.0),
            (0.0, 0.0, 0.0, None, 0.0)
        );
    }

    #[test]
    fn progress_derived_mid_run() {
        // 25 of 200 in 10 s: percent 12.5, rate 2.5, eta 70, survivors 4/25.
        let (percent, elapsed, rate, eta, survivor_rate) =
            progress_derived(200, 25, 4, 100.0, 110.0);
        assert_eq!(percent, 12.5);
        assert_eq!(elapsed, 10.0);
        assert_eq!(rate, 2.5);
        assert_eq!(eta, Some(70.0));
        assert_eq!(survivor_rate, 0.16);
    }

    #[test]
    fn progress_derived_percent_caps_at_100_and_eta_zero_when_over() {
        let (percent, _, _, eta, _) = progress_derived(10, 10, 0, 0.0, 5.0);
        assert_eq!(percent, 100.0);
        assert_eq!(eta, Some(0.0));
    }

    #[test]
    fn progress_derived_percent_rounds_to_one_decimal() {
        // 1/3 → 33.333…% → round(…, 1) == 33.3.
        let (percent, ..) = progress_derived(3, 1, 0, 0.0, 1.0);
        assert_eq!(percent, 33.3);
        // 1/400 → 0.25% — the exact half-even tie → 0.2.
        let (percent_tie, ..) = progress_derived(400, 1, 0, 0.0, 1.0);
        assert_eq!(percent_tie, 0.2);
    }

    #[test]
    fn progress_derived_negative_or_zero_elapsed_clamps() {
        // updated_at behind started_at (clock injection edge) → elapsed 0,
        // rate 0, eta None even with evaluated > 0.
        let (_, elapsed, rate, eta, _) = progress_derived(10, 5, 1, 100.0, 90.0);
        assert_eq!(elapsed, 0.0);
        assert_eq!(rate, 0.0);
        assert_eq!(eta, None);
    }

    #[test]
    fn sla_decision_allowed_within_envelope() {
        assert_eq!(
            sla_decision(100, 30.0, 345_600.0, false),
            (3000.0, "allowed")
        );
    }

    #[test]
    fn sla_decision_boundary_is_allowed() {
        // projected == envelope is NOT past it (the <= law).
        assert_eq!(
            sla_decision(11_520, 30.0, 345_600.0, false),
            (345_600.0, "allowed")
        );
    }

    #[test]
    fn sla_decision_over_envelope_refused_or_acknowledged() {
        assert_eq!(
            sla_decision(11_521, 30.0, 345_600.0, false),
            (345_630.0, "refused")
        );
        assert_eq!(
            sla_decision(11_521, 30.0, 345_600.0, true),
            (345_630.0, "acknowledged")
        );
    }

    #[test]
    fn rank_plan_on_mode_is_always_bounded() {
        // Mode `on` bounds even a projection far past budget.
        assert_eq!(
            rank_plan_decision(i64::MAX, 1, "on", true, true),
            Some((i128::from(i64::MAX) * 1024, "bounded"))
        );
    }

    #[test]
    fn rank_plan_within_budget_is_batch() {
        assert_eq!(
            rank_plan_decision(1000, 8 * 1024 * 1024, "auto", true, true),
            Some((1_024_000, "batch"))
        );
        // Boundary: projected == budget is within (the <= law).
        assert_eq!(
            rank_plan_decision(1000, 1_024_000, "off", false, false),
            Some((1_024_000, "batch"))
        );
    }

    #[test]
    fn rank_plan_over_budget_refusal_arms_in_branch_order() {
        assert_eq!(
            rank_plan_decision(1000, 1, "off", true, true),
            Some((1_024_000, "refuse_off"))
        );
        assert_eq!(
            rank_plan_decision(1000, 1, "auto", false, true),
            Some((1_024_000, "refuse_no_top_k"))
        );
        assert_eq!(
            rank_plan_decision(1000, 1, "auto", true, false),
            Some((1_024_000, "refuse_no_digest"))
        );
        assert_eq!(
            rank_plan_decision(1000, 1, "auto", true, true),
            Some((1_024_000, "bounded"))
        );
    }

    #[test]
    fn rank_plan_unknown_mode_is_none() {
        assert_eq!(rank_plan_decision(1, 1, "banana", true, true), None);
    }

    fn owned(pairs: &[(&str, &str, i64)]) -> Vec<(String, String, i64)> {
        pairs
            .iter()
            .map(|(a, b, n)| (a.to_string(), b.to_string(), *n))
            .collect()
    }

    fn names(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn preflight_missing_names_zero_and_absent_pairs_in_request_order() {
        let sizes = owned(&[
            ("NSE-NIFTY", "1minute", 100),
            ("NSE-NIFTY", "5minute", 0),
            ("BSE-SENSEX", "1minute", 7),
        ]);
        let symbols = names(&["NSE-NIFTY", "BSE-SENSEX"]);
        let intervals = names(&["1minute", "5minute"]);
        assert_eq!(
            preflight_missing(&sizes, &symbols, &intervals),
            vec![
                ("NSE-NIFTY".to_string(), "5minute".to_string()),
                ("BSE-SENSEX".to_string(), "5minute".to_string()),
            ]
        );
    }

    #[test]
    fn preflight_missing_all_present_is_empty() {
        let sizes = owned(&[("A", "1minute", 1)]);
        assert_eq!(
            preflight_missing(&sizes, &names(&["A"]), &names(&["1minute"])),
            Vec::<(String, String)>::new()
        );
    }

    #[test]
    fn freshness_stale_fail_closed_and_sorted() {
        let verdicts = vec![
            ("Z".to_string(), "5minute".to_string(), "stale".to_string()),
            ("A".to_string(), "1day".to_string(), "fresh".to_string()),
            ("A".to_string(), "5minute".to_string(), "weird".to_string()),
        ];
        // "weird" counts stale (fail-closed); output sorted ascending.
        assert_eq!(
            freshness_stale(&verdicts),
            vec![
                ("A".to_string(), "5minute".to_string()),
                ("Z".to_string(), "5minute".to_string()),
            ]
        );
    }

    #[test]
    fn liveness_tail_dead_pid_is_not_running() {
        assert_eq!(liveness_tail(false, 1000.0, 999.0, 600.0), "not_running");
    }

    #[test]
    fn liveness_tail_strictly_past_window_is_stale() {
        assert_eq!(liveness_tail(true, 1600.5, 1000.0, 600.0), "stale");
        // Exactly AT the window is NOT strictly past — running.
        assert_eq!(liveness_tail(true, 1600.0, 1000.0, 600.0), "running");
    }

    #[test]
    fn liveness_tail_fresh_is_running() {
        assert_eq!(liveness_tail(true, 1000.0, 999.0, 600.0), "running");
    }
}
