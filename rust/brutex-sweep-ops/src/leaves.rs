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
}
