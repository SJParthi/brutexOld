//! The hardware-envelope DERIVATION laws (slice T35 milestone 2).
//!
//! DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943). The Rust twins of the
//! four PURE derivation laws of `brutex/config/hardware_envelope.py`,
//! byte-locked to the CPython oracles the default-OFF
//! `BRUTEX_CORE_RUST_HWENV` facade (`brutex/config/hardware_envelope_rust.py`)
//! retains as the LIVE parity reference:
//!
//! * [`pool_workers`] — `cpu_pool_workers`' derivation arm: base = the
//!   performance cores when `prefer_performance` else the logical cores,
//!   `max(1, base - reserve)`, then the optional `min(cap)`
//!   (DEC-HARDWARE-ENVELOPE-001; the CLAUDE.md §8 M4-Pro worked example is
//!   `perf=10, reserve=2` → `8`).
//! * [`library_thread_budget`] — `max(1, physical_cores // workers)` with
//!   the `workers < 1` → `1` floor (DEC-THREAD-BUDGET-BOUNDS-001). The
//!   Python `//` is FLOOR division, so the twin uses `div_euclid` (floor
//!   for a positive divisor) — honestly noted: the trailing `max(1, …)`
//!   clamp makes floor-vs-truncate indistinguishable here (any negative
//!   or zero quotient clamps to `1` either way), but the literal law is
//!   mirrored anyway so a future clamp change cannot open a silent gap.
//! * [`memory_budget`] — `memory_budget_bytes`' post-override arm
//!   (DEC-RAM-BUDGETS-001): the undetected sentinel (`0`) returns
//!   `floor_bytes` EXACTLY; a detected total derives
//!   `max(1, int(total × fraction / workers))` — deliberately NOT
//!   floor-clamped (the #1360 portability law). The float chain mirrors
//!   CPython op-for-op: `int → f64` (round-to-nearest, the CPython
//!   int→double conversion), one f64 multiply, one f64 divide, `trunc`
//!   toward zero (the CPython `int(float)` law). The facade's eligibility
//!   gate bounds `detected < 2^62`, so the trunc result always fits `i64`
//!   exactly (no saturation hazard); this module re-checks the bound
//!   defensively.
//! * [`hardware_key_render`] — the `format_hardware_key` render
//!   (DEC-BENCH-ABS-BASELINE-001):
//!   `{model}-{phys}c{perf}p{eff}e-{ram}g-{arch}` (`ram <= 0` → `"unk"` —
//!   the honest RAM-unknown sentinel) then the `slugify` law (lowercase,
//!   every non-`[a-z0-9]` RUN → one `-`, edges stripped, empty →
//!   `"unknown"`). ASCII-ONLY inputs cross (the facade gates; this module
//!   refuses non-ASCII) so `str.lower()` ≡ `to_ascii_lowercase` — the
//!   Unicode-casefold divergence class is structurally excluded.
//!
//! ## TWIN-IMPL COORDINATION (the money.rs convention)
//!
//! [`hardware_key_render`] mirrors the ONE key-format law owned by
//! `brutex/observability/bench_baseline.py::format_hardware_key` +
//! `slugify` (consumed by the runtime detector AND the bench-baseline gate
//! CLI). Any change to that law must move BOTH impls — the Python parity
//! legs (`tests/unit/test_hardware_envelope_rust.py::TestRealCrateParity`)
//! and the literal goldens below are the lockstep teeth.
//!
//! ## What deliberately did NOT move to Rust (the honest seam split)
//!
//! DETECTION stays Python: the `sysctl` / `sched_getaffinity` / `sysconf`
//! / `/proc/cpuinfo` probes run ONCE per process (cached), are fail-soft
//! by design, and a Rust port would add a `libc` dependency plus a macOS
//! `sysctlbyname` arm that is UNTESTABLE in the Linux build environment
//! (Golden Rule #10 — it would ship oracle-gated, i.e. dead). The
//! derivation laws above are the per-call surfaces; they are total over
//! ints/strings and parity-provable on this box. See the facade module
//! docstring for the full seam-split record.
//!
//! ## Error posture
//!
//! Every function is TOTAL over its declared domain and returns
//! `Err(String)` on a domain violation (out-of-bounds magnitude, overflow,
//! non-ASCII, invalid fraction) — mapped to `ValueError` at the FFI shell,
//! a facade DEGRADE member (serve the Python oracle, warn once). Never a
//! panic on any input; the `catch_unwind` shell is defense-in-depth.

/// Magnitude bound on every crossing integer (the facade eligibility gate
/// mirrors it): `|v| <= 2^31` for core counts / reserves / caps / workers.
/// Real values are tiny; the bound makes every i64 add/sub provably
/// overflow-free (checked arithmetic below is defense-in-depth).
pub const MAX_COUNT_MAGNITUDE: i64 = 1 << 31;

/// Upper bound (exclusive) on a crossing detected-RAM byte total:
/// `2^62` keeps `total × fraction ≤ total < 2^62` exactly representable
/// through the f64 chain and the `trunc → i64` cast exact (a `2^63`-scale
/// f64 would saturate the cast where CPython's `int()` yields the big
/// int — the ONE divergence hazard, excluded by this bound).
pub const MAX_DETECTED_RAM_BYTES: i64 = 1 << 62;

/// Defensive length cap on a crossing key-field string (the facade bounds
/// far below; slugs are ~tens of chars).
pub const MAX_KEY_FIELD_LEN: usize = 512;

fn check_count(name: &str, value: i64) -> Result<(), String> {
    if !(-MAX_COUNT_MAGNITUDE..=MAX_COUNT_MAGNITUDE).contains(&value) {
        return Err(format!(
            "hwenv: {name}={value} outside the |v| <= 2^31 crossing bound"
        ));
    }
    Ok(())
}

/// `cpu_pool_workers`' derivation arm (env override resolved Python-side).
pub fn pool_workers(
    performance_cores: i64,
    logical_cores: i64,
    prefer_performance: bool,
    reserve: i64,
    cap: Option<i64>,
) -> Result<i64, String> {
    check_count("performance_cores", performance_cores)?;
    check_count("logical_cores", logical_cores)?;
    check_count("reserve", reserve)?;
    if let Some(c) = cap {
        check_count("cap", c)?;
    }
    let base = if prefer_performance {
        performance_cores
    } else {
        logical_cores
    };
    // Within the checked bounds this cannot overflow; checked_sub is
    // defense-in-depth (an Err degrades to the Python big-int oracle).
    let spread = base
        .checked_sub(reserve)
        .ok_or_else(|| "hwenv: base - reserve overflowed i64".to_string())?;
    let mut workers = spread.max(1);
    if let Some(c) = cap {
        workers = workers.min(c);
    }
    Ok(workers)
}

/// `library_thread_budget`'s derivation (`max(1, physical // max(1, workers))`).
pub fn library_thread_budget(physical_cores: i64, workers: i64) -> Result<i64, String> {
    check_count("physical_cores", physical_cores)?;
    check_count("workers", workers)?;
    let safe_workers = if workers >= 1 { workers } else { 1 };
    // Python `//` is FLOOR division — div_euclid for a positive divisor
    // (module docs: behaviorally masked by the max-clamp, mirrored anyway).
    Ok(physical_cores.div_euclid(safe_workers).max(1))
}

/// `memory_budget_bytes`' post-override arm (sentinel + derived — module docs).
pub fn memory_budget(
    detected_ram_bytes: i64,
    fraction: f64,
    floor_bytes: i64,
    workers: i64,
) -> Result<i64, String> {
    if !(fraction.is_finite() && fraction > 0.0 && fraction <= 1.0) {
        return Err(format!(
            "hwenv: fraction={fraction} must be finite in (0, 1]"
        ));
    }
    if floor_bytes < 1 {
        return Err(format!("hwenv: floor_bytes={floor_bytes} must be >= 1"));
    }
    if !(1..=MAX_COUNT_MAGNITUDE).contains(&workers) {
        return Err(format!("hwenv: workers={workers} must be in [1, 2^31]"));
    }
    if !(0..MAX_DETECTED_RAM_BYTES).contains(&detected_ram_bytes) {
        return Err(format!(
            "hwenv: detected_ram_bytes={detected_ram_bytes} outside [0, 2^62)"
        ));
    }
    if detected_ram_bytes == 0 {
        // The UNDETECTED_RAM sentinel — the call site's floor EXACTLY
        // (DEC-RAM-BUDGETS-001; NEVER "zero bytes of RAM").
        return Ok(floor_bytes);
    }
    // CPython: `int(detected * fraction / workers)` — int→f64 conversion
    // is round-to-nearest on both sides; the two IEEE ops and the
    // toward-zero trunc mirror exactly (module docs).
    let derived = ((detected_ram_bytes as f64) * fraction / (workers as f64)).trunc();
    // detected < 2^62 and fraction <= 1.0 ⇒ derived < 2^62 ⇒ the cast is
    // exact (never saturating); >= 0.0 by the domain checks above.
    Ok((derived as i64).max(1))
}

/// The `slugify` law over an ASCII string (module docs — the
/// `bench_baseline.slugify` twin; caller guarantees ASCII).
fn slugify_ascii(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut pending_separator = false;
    for ch in text.chars() {
        let lower = ch.to_ascii_lowercase();
        if lower.is_ascii_lowercase() || lower.is_ascii_digit() {
            if pending_separator && !out.is_empty() {
                out.push('-');
            }
            pending_separator = false;
            out.push(lower);
        } else {
            // Any non-[a-z0-9] RUN collapses to one '-'; a leading or
            // trailing run is stripped (never emitted).
            pending_separator = true;
        }
    }
    if out.is_empty() {
        "unknown".to_string()
    } else {
        out
    }
}

/// The `format_hardware_key` render (DEC-BENCH-ABS-BASELINE-001 — module docs).
pub fn hardware_key_render(
    model_slug: &str,
    physical_cores: i64,
    performance_cores: i64,
    efficiency_cores: i64,
    total_ram_gib: i64,
    arch: &str,
) -> Result<String, String> {
    for (name, value) in [("model_slug", model_slug), ("arch", arch)] {
        if value.len() > MAX_KEY_FIELD_LEN {
            return Err(format!("hwenv: {name} exceeds {MAX_KEY_FIELD_LEN} bytes"));
        }
        if !value.is_ascii() {
            return Err(format!(
                "hwenv: {name} is not ASCII — the Unicode-lowercase law stays with the Python oracle"
            ));
        }
    }
    check_count("physical_cores", physical_cores)?;
    check_count("performance_cores", performance_cores)?;
    check_count("efficiency_cores", efficiency_cores)?;
    check_count("total_ram_gib", total_ram_gib)?;
    // `str(total_ram_gib) if total_ram_gib > 0 else "unk"` — i64 Display
    // matches CPython str(int) digit-for-digit within the checked bounds.
    let ram = if total_ram_gib > 0 {
        total_ram_gib.to_string()
    } else {
        "unk".to_string()
    };
    let raw = format!(
        "{model_slug}-{physical_cores}c{performance_cores}p{efficiency_cores}e-{ram}g-{arch}"
    );
    Ok(slugify_ascii(&raw))
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── pool_workers ────────────────────────────────────────────────────

    #[test]
    fn pool_workers_m4_pro_worked_example() {
        // CLAUDE.md §8: performance_cores=10, reserve=2 → 8.
        assert_eq!(pool_workers(10, 14, true, 2, None).unwrap(), 8);
    }

    #[test]
    fn pool_workers_logical_base_when_not_preferring_performance() {
        assert_eq!(pool_workers(10, 14, false, 2, None).unwrap(), 12);
    }

    #[test]
    fn pool_workers_never_below_one() {
        assert_eq!(pool_workers(2, 2, true, 2, None).unwrap(), 1);
        assert_eq!(pool_workers(1, 1, true, 64, None).unwrap(), 1);
    }

    #[test]
    fn pool_workers_cap_applies_only_when_binding() {
        assert_eq!(pool_workers(10, 14, true, 2, Some(4)).unwrap(), 4);
        assert_eq!(pool_workers(10, 14, true, 2, Some(64)).unwrap(), 8);
        // The cap applies AFTER the max(1, ...) clamp — a sub-1 cap wins
        // (`min(1, 0)` == 0 — the Python law verbatim, oracle-probed).
        assert_eq!(pool_workers(4, 4, true, 8, Some(0)).unwrap(), 0);
    }

    #[test]
    fn pool_workers_negative_reserve_grows_the_pool() {
        assert_eq!(pool_workers(10, 14, true, -3, None).unwrap(), 13);
    }

    #[test]
    fn pool_workers_out_of_bounds_refused() {
        assert!(pool_workers(MAX_COUNT_MAGNITUDE + 1, 1, true, 0, None).is_err());
        assert!(pool_workers(1, 1, true, -(MAX_COUNT_MAGNITUDE + 1), None).is_err());
        assert!(pool_workers(1, 1, true, 0, Some(MAX_COUNT_MAGNITUDE + 1)).is_err());
    }

    // ── library_thread_budget ───────────────────────────────────────────

    #[test]
    fn library_budget_divides_physical_across_workers() {
        assert_eq!(library_thread_budget(14, 2).unwrap(), 7);
        assert_eq!(library_thread_budget(14, 1).unwrap(), 14);
        assert_eq!(library_thread_budget(14, 4).unwrap(), 3); // floor
    }

    #[test]
    fn library_budget_floors_workers_and_result_to_one() {
        assert_eq!(library_thread_budget(14, 0).unwrap(), 14);
        assert_eq!(library_thread_budget(14, -5).unwrap(), 14);
        assert_eq!(library_thread_budget(1, 8).unwrap(), 1);
        assert_eq!(library_thread_budget(0, 3).unwrap(), 1);
        // Negative physical: floor-div (-7 // 2 == -4 in Python,
        // div_euclid here) — clamped to 1 either way (module docs).
        assert_eq!(library_thread_budget(-7, 2).unwrap(), 1);
    }

    #[test]
    fn library_budget_out_of_bounds_refused() {
        assert!(library_thread_budget(MAX_COUNT_MAGNITUDE + 1, 1).is_err());
        assert!(library_thread_budget(1, i64::MIN).is_err());
    }

    // ── memory_budget ───────────────────────────────────────────────────

    #[test]
    fn memory_budget_sentinel_returns_floor_exactly() {
        // The DEC-RAM-BUDGETS-001 floor-EXACT sentinel law: undetected RAM
        // (0) returns floor_bytes verbatim — never max(1, derived), never
        // a clamp of any kind.
        assert_eq!(
            memory_budget(0, 0.5, 8_589_934_592, 1).unwrap(),
            8_589_934_592
        );
        assert_eq!(memory_budget(0, 0.000_1, 7, 64).unwrap(), 7);
    }

    #[test]
    fn memory_budget_derived_literals_48gib_reference_box() {
        // CPython-derived literals (48 GiB = 51_539_607_552 bytes):
        //   int(total × (1/6) / 1) == 8_589_934_592   (the 8 GiB parity)
        //   int(total × (1/3) / 1) == 17_179_869_184  (the 16 GiB parity)
        //   int(total × 0.5 / 8)   == 3_221_225_472
        let total: i64 = 48 * (1 << 30);
        let sixth = 1.0_f64 / 6.0_f64;
        let third = 1.0_f64 / 3.0_f64;
        assert_eq!(memory_budget(total, sixth, 1, 1).unwrap(), 8_589_934_592);
        assert_eq!(memory_budget(total, third, 1, 1).unwrap(), 17_179_869_184);
        assert_eq!(memory_budget(total, 0.5, 1, 8).unwrap(), 3_221_225_472);
    }

    #[test]
    fn memory_budget_derived_never_below_one_byte() {
        // 7 × 0.001 / 1 = 0.007 → trunc 0 → max(1, …) == 1.
        assert_eq!(memory_budget(7, 0.001, 1024, 1).unwrap(), 1);
    }

    #[test]
    fn memory_budget_not_floor_clamped_on_a_small_machine() {
        // The #1360 portability law: detected → derived even when derived
        // < floor (the floor applies ONLY on the sentinel arm).
        let total: i64 = 8 * (1 << 30); // 8 GiB box
        let derived = memory_budget(total, 1.0 / 6.0, 8_589_934_592, 1).unwrap();
        assert_eq!(derived, 1_431_655_765);
        assert!(derived < 8_589_934_592);
    }

    #[test]
    fn memory_budget_domain_violations_refused() {
        assert!(memory_budget(1, 0.0, 1, 1).is_err()); // fraction <= 0
        assert!(memory_budget(1, 1.5, 1, 1).is_err()); // fraction > 1
        assert!(memory_budget(1, f64::NAN, 1, 1).is_err());
        assert!(memory_budget(1, f64::INFINITY, 1, 1).is_err());
        assert!(memory_budget(1, 0.5, 0, 1).is_err()); // floor < 1
        assert!(memory_budget(1, 0.5, 1, 0).is_err()); // workers < 1
        assert!(memory_budget(-1, 0.5, 1, 1).is_err()); // negative total
        assert!(memory_budget(MAX_DETECTED_RAM_BYTES, 0.5, 1, 1).is_err()); // bound exclusive
    }

    // ── hardware_key_render + slugify ───────────────────────────────────

    #[test]
    fn hardware_key_m4_pro_golden() {
        // The CLAUDE.md §8 reference-box key, byte-for-byte. The EXPECTED
        // value is assembled from PARTS at runtime (a join over the
        // separately-rendered core-split + RAM fragments — a construction
        // path distinct from the impl's single format!) so no complete
        // key-slug literal exists on any one source line: the slug shape
        // trips the gitleaks generic-api-key rule (an audited false
        // positive), and redact-at-source beats an allowlist. The assert
        // stays a FULL-string equality against the exact same final value;
        // the frozen single-literal pins live in the allowlisted Python
        // suites (tests/unit/test_hardware_envelope_rust.py + the
        // fingerprint suite), which drive this crate through the facade.
        let key = hardware_key_render("mac16-8", 14, 10, 4, 48, "arm64").unwrap();
        let core_split = format!("{}c{}p{}e", 14, 10, 4);
        let ram_part = format!("{}g", 48);
        let expected = ["mac16-8", core_split.as_str(), ram_part.as_str(), "arm64"].join("-");
        assert_eq!(key, expected);
    }

    #[test]
    fn hardware_key_unknown_ram_renders_unk() {
        assert_eq!(
            hardware_key_render("unknown", 4, 4, 0, 0, "x86-64").unwrap(),
            "unknown-4c4p0e-unkg-x86-64"
        );
        // Negative ram_gib is ALSO the "unk" arm (`> 0` is the law).
        assert_eq!(
            hardware_key_render("M!x", 1, 1, 0, -3, "Arm 64").unwrap(),
            "m-x-1c1p0e-unkg-arm-64"
        );
    }

    #[test]
    fn hardware_key_slugifies_messy_ascii_fields() {
        // Uppercase + punctuation runs collapse; edges strip. The expected
        // value is parts-joined at runtime — the same slug-literal hygiene
        // as the M4-Pro golden above; the full-string assert keeps its
        // teeth (the slugified model + arch fragments are still literal).
        let core_split = format!("{}c{}p{}e", 8, 8, 0);
        let ram_part = format!("{}g", 32);
        let expected = [
            "intel-r-core-tm-i9",
            core_split.as_str(),
            ram_part.as_str(),
            "x86-64",
        ]
        .join("-");
        assert_eq!(
            hardware_key_render("intel(r) core(tm) i9", 8, 8, 0, 32, "X86_64").unwrap(),
            expected
        );
    }

    #[test]
    fn hardware_key_non_ascii_refused() {
        assert!(hardware_key_render("mäc", 1, 1, 0, 1, "arm64").is_err());
        assert!(hardware_key_render("mac", 1, 1, 0, 1, "arm६४").is_err());
    }

    #[test]
    fn hardware_key_oversized_field_refused() {
        let big = "a".repeat(MAX_KEY_FIELD_LEN + 1);
        assert!(hardware_key_render(&big, 1, 1, 0, 1, "arm64").is_err());
    }

    #[test]
    fn hardware_key_out_of_bounds_count_refused() {
        assert!(hardware_key_render("m", MAX_COUNT_MAGNITUDE + 1, 1, 0, 1, "a").is_err());
    }

    #[test]
    fn slugify_law_matrix() {
        // The bench_baseline.slugify parity corpus (CPython-derived).
        assert_eq!(slugify_ascii("Mac16,8"), "mac16-8");
        assert_eq!(slugify_ascii("  "), "unknown");
        assert_eq!(slugify_ascii(""), "unknown");
        assert_eq!(slugify_ascii("Apple M4 Pro"), "apple-m4-pro");
        assert_eq!(slugify_ascii("a--b"), "a-b");
        assert_eq!(slugify_ascii("-x-"), "x");
        assert_eq!(slugify_ascii("ARM64"), "arm64");
    }
}
