//! `brutex-ops` — the ops (backup) DECISION kernel.
//!
//! `DEC-BACKUP-RUST-OPS-001`: the pure decision plane of `brutex/backup/` —
//! the dead-man verdict state machine (`deadman.py::evaluate_deadman`), the
//! four golden-pinned launchd/cron template renderers (`schedule_render.py`)
//! and the restic `--json` parse + argv laws (`restic_runner.py`) —
//! transcribed token-for-token into [`backup`]. The kernel crosses
//! PRIMITIVES ONLY (arm codes, verdict tokens, strings, floats); Python
//! (`brutex/backup/backup_rust.py`) keeps ALL I/O, subprocess, alerting,
//! datetime parsing and error/detail f-strings, so every logged/raised/
//! alerted byte stays Python-owned (the `DEC-FEED-RUST-CORE-001`
//! decision-kernel shape).
//!
//! Shipped default-OFF behind `BRUTEX_BACKUP_RUST`; the retained Python
//! implementations are the LIVE parity oracle (never deleted here — oracle
//! retirement is its own later lane).
//!
//! ## Honest envelope (Golden Rule #10)
//!
//! Expected payoff ≈ 0 — the conversion is for uniformity, not speed
//! (the ADR-0020 banner, mandatory verbatim): every law here runs at
//! once-per-backup / once-per-hourly-tick frequency, and the engaged path
//! ADDS an env probe + an FFI hop per call. Ships INERT; a default flip
//! needs its own DEC.

pub mod backup;

pub use backup::{
    ARM_CLOCK_SKEW_RUNNING, ARM_CLOCK_SKEW_SUCCESS, ARM_FRESH, ARM_HUNG, ARM_NEVER_NO_FILE,
    ARM_NEVER_NO_STAMP, ARM_OVERDUE, ARM_RUNNING, ARM_STATE_CORRUPT, BACKUP_HOST, DeadmanDecision,
    SECONDS_PER_HOUR, VERDICT_CLOCK_SKEW, VERDICT_FRESH, VERDICT_HUNG, VERDICT_NEVER_SUCCEEDED,
    VERDICT_OVERDUE, VERDICT_RUNNING, VERDICT_STATE_CORRUPT, build_backup_argv,
    evaluate_deadman_decision, parse_backup_summary_snapshot_id, parse_ls_file_paths,
    parse_ls_snapshot_time_decision, parse_snapshots_latest_decision, render_backup_plist,
    render_cron_lines, render_deadman_plist, render_drill_plist, should_alert_decision,
};

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs`),
/// baked at build time by `build.rs` under the shared FRAMING v1 of
/// `rust/source_digest_core.rs` (DEC-RUST-SOURCE-DIGEST-TWIN-001,
/// `docs/MIGRATION.md` #1258). The PyO3 module exports it as
/// `SOURCE_DIGEST` / `__source_digest__`.
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

#[cfg(feature = "python")]
mod python;
