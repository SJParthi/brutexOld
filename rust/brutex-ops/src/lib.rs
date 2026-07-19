//! `brutex-ops` — the BruteX ops/automation Rust kernels (unified crate).
//!
//! ONE crate, THREE ops lanes, hand-unified from the three lane PRs
//! (the `integration/brutex-ops-unified` vehicle):
//!
//! * [`backup`] — `DEC-BACKUP-RUST-OPS-001`: the dead-man verdict state
//!   machine (`deadman.py::evaluate_deadman`), the four golden-pinned
//!   launchd/cron template renderers (`schedule_render.py`) and the restic
//!   `--json` parse + argv laws (`restic_runner.py`), transcribed
//!   token-for-token. Facade: `brutex/backup/backup_rust.py`
//!   (`BRUTEX_BACKUP_RUST`, default OFF).
//! * [`crossverify`] — `DEC-CROSSVERIFY-RUST-CORE-001`: the pinned CSV byte
//!   assembly of `csv_render.render_day_csv` + the canonical-identity
//!   mapping / S3-key law of `canonical.py`. Facade:
//!   `brutex/crossverify/rust_seam.py` (`BRUTEX_CROSSVERIFY_RUST`).
//! * [`nightly`] — `DEC-NIGHTLY-RUST-CORE-001`: the nightly chain's pure
//!   decision laws — the R42 break/exit-code law + the audit-reconcile
//!   tripwire (`chain.py`), the verdict/Telegram summary builder
//!   (`report.py`), the launchd plist render + HH:MM parse + pmset-wake /
//!   since-fire arithmetic (`schedule.py`). Facade:
//!   `brutex/nightly/nightly_rust.py` (`BRUTEX_NIGHTLY_RUST`).
//!
//! Every kernel crosses PRIMITIVES / pre-formatted strings ONLY; the
//! retained Python implementations are the LIVE parity oracle (never
//! deleted here — oracle retirement is its own later lane), and every
//! logged / raised / alerted byte stays Python-owned (the
//! `DEC-FEED-RUST-CORE-001` decision-kernel shape).
//!
//! This file is MODULE REGISTRATION + re-export ONLY — the append-only
//! spine for the ops lanes. New lanes APPEND their own `pub mod <lane>;`
//! line + a delimited block in `src/python.rs` — never editing an existing
//! lane's block (the brutex-lake §4.1 concurrent-sibling additive law).
//!
//! ## Honest envelope (Golden Rule #10)
//!
//! Expected payoff ≈ 0 — the conversion is for uniformity, not speed
//! (the ADR-0020 banner, mandatory verbatim): every law here runs at
//! once-per-backup / once-per-export / once-per-nightly-tick frequency,
//! and the engaged path ADDS an env probe + an FFI hop per call. Every
//! seam ships INERT (default OFF); a default flip needs its own DEC.

pub mod backup;
pub mod crossverify;
pub mod nightly;

pub use backup::{
    ARM_CLOCK_SKEW_RUNNING, ARM_CLOCK_SKEW_SUCCESS, ARM_FRESH, ARM_HUNG, ARM_NEVER_NO_FILE,
    ARM_NEVER_NO_STAMP, ARM_OVERDUE, ARM_RUNNING, ARM_STATE_CORRUPT, BACKUP_HOST, DeadmanDecision,
    SECONDS_PER_HOUR, VERDICT_CLOCK_SKEW, VERDICT_FRESH, VERDICT_HUNG, VERDICT_NEVER_SUCCEEDED,
    VERDICT_OVERDUE, VERDICT_RUNNING, VERDICT_STATE_CORRUPT, build_backup_argv,
    evaluate_deadman_decision, parse_backup_summary_snapshot_id, parse_ls_file_paths,
    parse_ls_snapshot_time_decision, parse_snapshots_latest_decision, render_backup_plist,
    render_cron_lines, render_deadman_plist, render_drill_plist, should_alert_decision,
};

pub use crossverify::{
    CSV_CELLS_PER_ROW, CSV_REFUSAL_CONTROL_CHAR, CSV_REFUSAL_EMPTY_ROWS, CSV_REFUSAL_METACHAR,
    CSV_REFUSAL_NON_INCREASING_TS, CSV_REFUSAL_WRONG_ARITY, CSV_TS_CELL_INDEX, CsvRefusal,
    IdentityFields, canonical_identity_fields, render_csv_bytes, s3_key_assemble, symbol_key_safe,
};

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs` —
/// ALL THREE lanes' sources under the ONE scheme), baked at build time by
/// `build.rs` under the shared FRAMING v1 of `rust/source_digest_core.rs`
/// (DEC-RUST-SOURCE-DIGEST-TWIN-001, `docs/MIGRATION.md` #1258). The PyO3
/// module exports it as `SOURCE_DIGEST` / `__source_digest__` (a doctor
/// wheel-freshness stage is future work, not claimed here).
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

/// Crate version re-exported to Python as `brutex_ops.__version__`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

#[cfg(feature = "python")]
mod python;

#[cfg(test)]
mod tests {
    #[test]
    fn version_matches_manifest() {
        assert_eq!(super::VERSION, "0.1.0");
    }
}
