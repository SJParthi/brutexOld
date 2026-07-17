//! brutex-core — the BruteX root-leaf Rust conversion crate (slice T35).
//!
//! DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943). Milestone 1 hosts the
//! MONEY laws of `brutex/money.py` (DEC-MONEY-2DP-001) as exact
//! decimal-string arithmetic — see [`money`] for the algorithm, the probed
//! CPython parity hazards, the HALF_UP-not-half-even negative pin and the
//! twin-impl coordination note (the same half-away-from-zero law also
//! lives in `brutex/data/parquet_writer.py::_snap_price_expr`, whose
//! output the `rust/brutex-lake` seams consume — the two move together).
//!
//! Honesty banner (ADR-0020, mandatory verbatim): expected payoff ≈ 0 —
//! conversion is for uniformity, not speed. Each of these functions is a
//! sub-microsecond `Decimal` op in Python; the FFI hop is a small
//! SLOWDOWN, acceptable because the seam ships DEFAULT-OFF behind
//! `BRUTEX_CORE_RUST_MONEY` (a default flip needs its own DEC) and the
//! conversion buys uniformity across the Rust-first program.
//!
//! ## The strings-only FFI law
//!
//! ONLY strings cross this boundary, in BOTH directions. The Python
//! facade (`brutex/money_rust.py`) pre-normalizes each crossing `Decimal`
//! to its fixed-point `format(d, "f")` render and each paisa `int` to
//! `str(int)`, gates ELIGIBILITY Python-side (non-finite values,
//! out-of-bounds exponents, `|paisa| >= 2**63`, context-precision /
//! trap hazards all route to the `Decimal` oracle WITHOUT touching this
//! crate), and rebuilds `Decimal(...)` / `int(...)` from the returned
//! strings. Rust never sees a float, never builds a Python numeric.
//!
//! ## Error mapping at the boundary
//!
//! * A parse/shape failure (`money::*` returns `Err`) → `ValueError` — a
//!   facade DEGRADE member (serve the oracle, warn once).
//! * A panic → caught by `catch_unwind` → `RuntimeError` — likewise a
//!   degrade member; never a process abort mid-write (the brutex-lake
//!   boundary convention).

use std::panic::{AssertUnwindSafe, catch_unwind};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

pub mod backupenv;
pub mod dotenv;
pub mod hwenv;
pub mod models;
pub mod money;
pub mod paths;

/// Crate version — surfaced as `brutex_core.__version__`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// The ONE FFI boundary shell shared by every money pyfunction: run the
/// pure core under `catch_unwind`, map `Err` → `ValueError` and a panic →
/// `RuntimeError` (both facade degrade members — module docs).
fn money_ffi_shell(
    name: &str,
    input: &str,
    f: fn(&str) -> Result<String, String>,
) -> PyResult<String> {
    match catch_unwind(AssertUnwindSafe(|| f(input))) {
        Ok(Ok(out)) => Ok(out),
        Ok(Err(msg)) => Err(PyValueError::new_err(msg)),
        Err(_) => Err(PyRuntimeError::new_err(format!(
            "brutex-core panic in {name} (caught at the FFI boundary)"
        ))),
    }
}

/// `round_money` twin — 2dp HALF_UP (away from zero) on a fixed-point string.
#[pyfunction]
fn round_money_str(rupees: &str) -> PyResult<String> {
    money_ffi_shell("round_money_str", rupees, money::round_money_2dp)
}

/// `rupees_to_paisa` twin — round to 2dp then the exact ×100 digit shift.
#[pyfunction]
fn rupees_to_paisa_str(rupees: &str) -> PyResult<String> {
    money_ffi_shell("rupees_to_paisa_str", rupees, money::rupees_to_paisa)
}

/// `paisa_to_rupees` twin — the exact ÷100 digit shift to a 2dp string.
#[pyfunction]
fn paisa_to_rupees_str(paisa: &str) -> PyResult<String> {
    money_ffi_shell("paisa_to_rupees_str", paisa, money::paisa_to_rupees)
}

/// `format_inr` twin — `₹` + comma-grouped-by-3 + always two decimals.
#[pyfunction]
fn format_inr_str(paisa: &str) -> PyResult<String> {
    money_ffi_shell("format_inr_str", paisa, money::format_inr)
}

/// The generic FFI boundary shell for the hwenv derivation laws (milestone
/// 2) AND the milestone-3 config laws (paths / dotenv / backupenv): run
/// the pure core under `catch_unwind`, map `Err` → `ValueError` and a
/// panic → `RuntimeError` (both facade degrade members). The money shell
/// above stays untouched — its `fn(&str)` shape is milestone-1 surface;
/// this generic twin serves every later mixed-signature seam.
fn core_ffi_shell<T>(name: &str, f: impl FnOnce() -> Result<T, String>) -> PyResult<T> {
    match catch_unwind(AssertUnwindSafe(f)) {
        Ok(Ok(out)) => Ok(out),
        Ok(Err(msg)) => Err(PyValueError::new_err(msg)),
        Err(_) => Err(PyRuntimeError::new_err(format!(
            "brutex-core panic in {name} (caught at the FFI boundary)"
        ))),
    }
}

/// `cpu_pool_workers` derivation twin (milestone 2 — env override + the
/// envelope detection stay Python; see `src/hwenv.rs`).
#[pyfunction]
#[pyo3(signature = (performance_cores, logical_cores, prefer_performance, reserve, cap))]
fn hwenv_pool_workers(
    performance_cores: i64,
    logical_cores: i64,
    prefer_performance: bool,
    reserve: i64,
    cap: Option<i64>,
) -> PyResult<i64> {
    core_ffi_shell("hwenv_pool_workers", || {
        hwenv::pool_workers(
            performance_cores,
            logical_cores,
            prefer_performance,
            reserve,
            cap,
        )
    })
}

/// `library_thread_budget` derivation twin.
#[pyfunction]
fn hwenv_library_thread_budget(physical_cores: i64, workers: i64) -> PyResult<i64> {
    core_ffi_shell("hwenv_library_thread_budget", || {
        hwenv::library_thread_budget(physical_cores, workers)
    })
}

/// `memory_budget_bytes` post-override twin (sentinel + derived arms).
/// The `fraction` f64 crosses bit-exact via PyO3 (the ONE non-int/str
/// crossing — see the facade module docstring's documented deviation).
#[pyfunction]
fn hwenv_memory_budget(
    detected_ram_bytes: i64,
    fraction: f64,
    floor_bytes: i64,
    workers: i64,
) -> PyResult<i64> {
    core_ffi_shell("hwenv_memory_budget", || {
        hwenv::memory_budget(detected_ram_bytes, fraction, floor_bytes, workers)
    })
}

/// `format_hardware_key` render twin (DEC-BENCH-ABS-BASELINE-001 law).
#[pyfunction]
fn hwenv_hardware_key(
    model_slug: &str,
    physical_cores: i64,
    performance_cores: i64,
    efficiency_cores: i64,
    total_ram_gib: i64,
    arch: &str,
) -> PyResult<String> {
    core_ffi_shell("hwenv_hardware_key", || {
        hwenv::hardware_key_render(
            model_slug,
            physical_cores,
            performance_cores,
            efficiency_cores,
            total_ram_gib,
            arch,
        )
    })
}

/// `resolve_lake_root` decision twin (milestone 3 — `src/paths.rs`; the
/// env lookup + `expanduser` stay Python).
#[pyfunction]
#[pyo3(signature = (override_value, home))]
fn paths_lake_root_str(override_value: Option<&str>, home: &str) -> PyResult<String> {
    core_ffi_shell("paths_lake_root_str", || {
        paths::lake_root(override_value, home)
    })
}

/// `lake_root_identity` volume-parent twin (milestone 3 — `""` = no
/// volume parent; the mount/stat probes stay Python).
#[pyfunction]
fn paths_volume_parent_str(path: &str) -> PyResult<String> {
    core_ffi_shell("paths_volume_parent_str", || paths::volume_parent(path))
}

/// `parse_dotenv` line-parse twin (milestone 3 — `src/dotenv.rs`; file
/// reads + the never-override apply law stay Python). Returns the
/// deduped ordered pairs, exactly `list(parse_dotenv(text).items())`.
#[pyfunction]
fn dotenv_parse_pairs(text: &str) -> PyResult<Vec<(String, String)>> {
    core_ffi_shell("dotenv_parse_pairs", || dotenv::parse_pairs(text))
}

/// `_valid_verify_subset` shape twin (milestone 3 — `src/backupenv.rs`;
/// the resolver's fail-safe WARN laws stay Python).
#[pyfunction]
fn backupenv_verify_subset_valid(value: &str) -> PyResult<bool> {
    core_ffi_shell("backupenv_verify_subset_valid", || {
        backupenv::verify_subset_valid(value)
    })
}

/// `normalise_tick_size` decision twin (T35 PR-2 — the models seam,
/// `src/models.rs`; the Python-type dispatch + the `TickSizeFormatError`
/// message bytes stay Python — a `tick_refuse:*` `ValueError` is a LAW
/// refusal the facade maps to the oracle's raise, any other error a
/// degrade-member fault).
#[pyfunction]
fn models_normalise_tick_size_str(kind: &str, payload: &str) -> PyResult<String> {
    core_ffi_shell("models_normalise_tick_size_str", || {
        models::normalise_tick_size(kind, payload)
    })
}

// `#[pymodule]` registers the `_brutex_core` Python submodule. Maturin
// produces a cdylib whose extern "C" `PyInit__brutex_core` symbol matches
// this function name; under the mixed layout it installs at
// `site-packages/brutex_core/_brutex_core.<abi>.so` with
// `brutex_core/__init__.py` re-exporting the symbols (the fno-math
// DEC-P0-055 convention, via rust/brutex-lake).
#[pymodule]
fn _brutex_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", VERSION)?;
    // Milestone 1 — the money seam (DEC-CONFIG-RUST-LEAVES-001). Later
    // T35 milestones register ADDITIVELY beside these, never in place of
    // (the brutex-lake §4.1 concurrent-sibling merge law).
    m.add_function(wrap_pyfunction!(round_money_str, m)?)?;
    m.add_function(wrap_pyfunction!(rupees_to_paisa_str, m)?)?;
    m.add_function(wrap_pyfunction!(paisa_to_rupees_str, m)?)?;
    m.add_function(wrap_pyfunction!(format_inr_str, m)?)?;
    // Milestone 2 — the hardware-envelope derivation seam
    // (BRUTEX_CORE_RUST_HWENV; DEC-CONFIG-RUST-LEAVES-001).
    m.add_function(wrap_pyfunction!(hwenv_pool_workers, m)?)?;
    m.add_function(wrap_pyfunction!(hwenv_library_thread_budget, m)?)?;
    m.add_function(wrap_pyfunction!(hwenv_memory_budget, m)?)?;
    m.add_function(wrap_pyfunction!(hwenv_hardware_key, m)?)?;
    // Milestone 3 — the plain-config-leaf seams (BRUTEX_CORE_RUST_PATHS /
    // BRUTEX_CORE_RUST_DOTENV / BRUTEX_CORE_RUST_BACKUPENV;
    // DEC-CONFIG-RUST-LEAVES-001 milestone 3).
    m.add_function(wrap_pyfunction!(paths_lake_root_str, m)?)?;
    m.add_function(wrap_pyfunction!(paths_volume_parent_str, m)?)?;
    m.add_function(wrap_pyfunction!(dotenv_parse_pairs, m)?)?;
    m.add_function(wrap_pyfunction!(backupenv_verify_subset_valid, m)?)?;
    // T35 PR-2 — the models seam (BRUTEX_CORE_RUST_MODELS): the
    // normalise_tick_size decision law (src/models.rs).
    m.add_function(wrap_pyfunction!(models_normalise_tick_size_str, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_matches_manifest() {
        // 0.4.0: the wheel API grew again (money + hwenv + the
        // milestone-3 config laws -> + the T35 PR-2 models tick-size
        // law) — a stale wheel must not masquerade as the grown surface
        // (review-r1 stale-wheel hazard; the facades' handshakes check
        // callables, not versions, so the bump is the human-visible
        // provenance marker).
        assert_eq!(VERSION, "0.4.0");
    }

    #[test]
    fn ffi_shell_maps_err_to_valueerror_shape() {
        // The shell itself is exercised end-to-end only through Python;
        // here we pin the pure-core routing it wraps (Ok and Err arms).
        assert_eq!(money::round_money_2dp("1.005").unwrap(), "1.01");
        assert!(money::round_money_2dp("junk").is_err());
    }
}
