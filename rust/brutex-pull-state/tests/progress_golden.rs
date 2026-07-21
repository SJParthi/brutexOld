//! Fixture-driven differential for the pull-progress derived core (Wave 2F).
//!
//! Replays the COMMITTED pre-conversion golden
//! (`tests/golden/fixtures/pull_progress_derived.tsv` — the serde-free line
//! projection of `pull_progress_derived.json`, sync-pinned byte-equal by
//! the Python golden test) through
//! [`brutex_pull_state::progress::progress_derived`] and asserts every
//! metric bit-for-bit — the lane's bit-identity contract, with floats
//! travelling as IEEE-754 bit patterns (no parse rounding anywhere).
//!
//! NaN OUTPUT cells are compared NaN-to-NaN (never bit-to-bit): IEEE leaves
//! NaN *payload* propagation implementation-defined across architectures,
//! so a bit compare of a PRODUCED NaN would be a false pin (the harness doc
//! records the same contract on the Python side).
//!
//! Plus proptest invariants: totality inside the count domain over
//! arbitrary f64 bit patterns (NaN payloads included), the percent range
//! law, and the eta honest-absence law.

use brutex_pull_state::progress::{COUNT_DOMAIN_MAX, percent, progress_derived};
use proptest::prelude::*;

fn fixture_path() -> std::path::PathBuf {
    // CARGO_MANIFEST_DIR = <repo>/rust/brutex-pull-state → two levels up.
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/fixtures/pull_progress_derived.tsv")
}

/// Decode the generator's `<f8` little-endian hex bit pattern (bit-exact).
fn f64_from_hex_bits(cell: &str) -> f64 {
    assert_eq!(
        cell.len(),
        16,
        "bit-pattern cell must be 16 hex chars: {cell:?}"
    );
    let mut bytes = [0u8; 8];
    for (i, chunk) in bytes.iter_mut().enumerate() {
        *chunk = u8::from_str_radix(&cell[2 * i..2 * i + 2], 16)
            .unwrap_or_else(|e| panic!("bad hex byte in {cell:?}: {e}"));
    }
    f64::from_le_bytes(bytes)
}

fn parse_bool(cell: &str) -> bool {
    match cell {
        "1" => true,
        "0" => false,
        other => panic!("bad bool cell: {other:?}"),
    }
}

/// Bit-equal for finite/inf expectations; NaN-to-NaN for NaN expectations.
fn assert_metric(got: f64, want: f64, what: &str, line: &str) {
    if want.is_nan() {
        assert!(got.is_nan(), "{what}: expected NaN, got {got:?} on {line}");
    } else {
        assert_eq!(
            got.to_bits(),
            want.to_bits(),
            "{what}: got {got:?} want {want:?} on {line}"
        );
    }
}

#[test]
fn frozen_golden_replays_bit_identical() {
    let body = std::fs::read_to_string(fixture_path()).expect("committed fixture readable");
    let mut rows = 0usize;
    let mut nan_rows = 0usize;
    for line in body.lines() {
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let cols: Vec<&str> = line.split('\t').collect();
        assert_eq!(cols.len(), 13, "column drift on {line:?}");
        let state_running = parse_bool(cols[0]);
        let open_ended = parse_bool(cols[1]);
        let total: u64 = cols[2].parse().expect("total");
        let completed: u64 = cols[3].parse().expect("completed");
        let base: u64 = cols[4].parse().expect("base");
        let updated_at = f64_from_hex_bits(cols[5]);
        let started_at = f64_from_hex_bits(cols[6]);
        let phase_started_at = f64_from_hex_bits(cols[7]);
        let want_percent = f64_from_hex_bits(cols[8]);
        let want_elapsed = f64_from_hex_bits(cols[9]);
        let want_ups = f64_from_hex_bits(cols[10]);
        let want_eta_present = parse_bool(cols[11]);
        let (got_percent, got_elapsed, got_ups, got_eta_present, got_eta) = progress_derived(
            state_running,
            open_ended,
            total,
            completed,
            base,
            updated_at,
            started_at,
            phase_started_at,
        )
        .expect("every golden row is inside the count domain");
        assert_metric(got_percent, want_percent, "percent", line);
        assert_metric(got_elapsed, want_elapsed, "elapsed", line);
        assert_metric(got_ups, want_ups, "units_per_sec", line);
        if want_ups.is_nan() {
            nan_rows += 1;
        }
        assert_eq!(
            got_eta_present, want_eta_present,
            "eta_present drift on {line}"
        );
        if want_eta_present {
            let want_eta = f64_from_hex_bits(cols[12]);
            assert_metric(got_eta, want_eta, "eta", line);
        } else {
            assert_eq!(cols[12], "-", "absent eta must be encoded '-' on {line}");
        }
        rows += 1;
    }
    // Anti-vacuous: the committed lattice is the full sweep, incl. NaN rows.
    assert!(rows >= 300, "suspiciously small fixture: {rows} rows");
    assert!(nan_rows > 0, "the NaN-to-NaN contract needs a NaN row");
}

proptest! {
    /// Totality + range law inside the count domain: percent is either in
    /// [0, 100] exactly (never NaN — counts are integers, total >= 1).
    #[test]
    fn percent_total_and_ranged(
        total in 1u64..=COUNT_DOMAIN_MAX,
        completed in 0u64..=COUNT_DOMAIN_MAX,
    ) {
        let p = percent(total, completed).expect("in-domain");
        prop_assert!((0.0..=100.0).contains(&p), "percent out of range: {p}");
        // One decimal place: 10*p is integral for every in-range result.
        prop_assert_eq!((p * 10.0).fract(), 0.0);
    }

    /// The bundle never panics over ARBITRARY f64 bit patterns (NaN payloads
    /// included) for in-domain counts, and eta is absent whenever the run is
    /// not running / open-ended / total-less.
    #[test]
    fn bundle_totality_and_eta_absence(
        state_running in any::<bool>(),
        open_ended in any::<bool>(),
        total in 0u64..=COUNT_DOMAIN_MAX,
        completed in 0u64..=COUNT_DOMAIN_MAX,
        base in 0u64..=COUNT_DOMAIN_MAX,
        updated_bits in any::<u64>(),
        started_bits in any::<u64>(),
        phase_bits in any::<u64>(),
    ) {
        let out = progress_derived(
            state_running,
            open_ended,
            total,
            completed,
            base,
            f64::from_bits(updated_bits),
            f64::from_bits(started_bits),
            f64::from_bits(phase_bits),
        ).expect("in-domain");
        let (_, _, _, eta_present, _) = out;
        if !state_running || open_ended || total == 0 {
            prop_assert!(!eta_present);
        }
    }

    /// The domain refusal fires for EVERY count beyond the ceiling.
    #[test]
    fn out_of_domain_counts_refuse(extra in 1u64..=(u64::MAX - (1 << 53))) {
        let big = COUNT_DOMAIN_MAX + extra;
        prop_assert!(percent(big, 0).is_err());
        prop_assert!(percent(1, big).is_err());
        prop_assert!(
            progress_derived(true, false, 1, 1, big, 1.0, 0.0, 0.0).is_err()
        );
    }
}
