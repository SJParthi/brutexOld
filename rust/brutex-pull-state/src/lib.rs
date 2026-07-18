//! `brutex-pull-state` — the dashboard pull-state DECISION kernel.
//!
//! `DEC-RUST-PULL-STATE-001` (lane Phase 1): the decision plane of the
//! pull-state / rate-view modules, starting with the vendor-account SAFETY
//! guard `brutex/calendars/market_hours.py::pull_allowed_now`
//! (DEC-GROWW-MARKET-HOURS-GUARD-001, MIGRATION.md #1692 — no Groww vendor
//! pulls 08:45-15:35 IST on a trading day; the shared-TOTP protection),
//! transcribed to a pure clockless predicate ([`pull_allowed_us`]) over
//! INTEGERS ONLY — the caller's microsecond-of-day + the caller-resolved
//! trading-day flag. Python (`brutex/pull_state_rust.py` +
//! `brutex/calendars/market_hours.py`) resolves the clock and the NSE
//! calendar itself (`brutex/calendars/nse.py` stays the single source of
//! truth — the holiday sets are NEVER re-minted in Rust), so guard verdicts
//! are byte-preserved by construction against the frozen golden
//! (`tests/golden/fixtures/market_hours_guard_verdicts.json`).
//!
//! FLIP PHASE (shipped): these kernels are the ONLY decision path. The
//! pre-flip Python oracles are DELETED and the `BRUTEX_PULL_STATE_RUST`
//! opt-in knob is REMOVED (a SET value makes the facade raise
//! `PullStateFlagRemovedError`). The facade (`brutex/pull_state_rust.py`)
//! is FAIL-CLOSED: a missing/stale wheel, a kernel raise, or a
//! refused/poisoned result raises the typed `PullStateKernelError` — for
//! the guard chain protecting the shared-TOTP Groww account a pull is
//! REFUSED, never silently allowed. Throughout this crate's docs "the
//! (Python) oracle" names the pre-flip Python implementation each kernel
//! was transcribed from — the LAW reference, not a live fallback.
//!
//! ## Honest envelope (Golden Rule #10)
//!
//! The predicate is O(1) and already nanosecond-scale in Python — this port
//! is for the operator-ordered Rust uniformity program, NOT speed (the
//! ADR-0020 honesty-banner posture: expected payoff ≈ 0); the FFI hop is a
//! small SLOWDOWN, acceptable because the seam runs a handful of times per
//! pull start / per mid-run window check, never on a hot path.

// Wave 2D (DEC-RUST-PULL-STATE-001): the --auto pull-gate ladder +
// Muhurat margined-window + settle-token verdict kernels — codes only;
// every human string + the fail-loud env raise stay Python.
pub mod auto_pull_gate;
// Wave 3G (DEC-RUST-PULL-STATE-001): the auto-pull-scheduler tick-decision
// ladder + durable-state-parse verdict kernels — codes only; the ticker
// thread, store I/O, log-dedup memo and every human string stay Python.
pub mod auto_pull_scheduler;
// Wave 3G (DEC-RUST-PULL-STATE-001): the instrument-master reload predicate
// — five caller-resolved facts in, one bool out; the store probes stay
// Python.
pub mod auto_pull;
// Wave 2E (DEC-RUST-PULL-STATE-001): the pull-intent parse-shape +
// resume-decision core (DEC-PULL-INTENT-RESUME-001 re-hosted) — codes only;
// the JSON decode (CPython's whole failure taxonomy), every warn string and
// the PullIntent dataclass itself stay Python (see src/intent.rs).
pub mod intent;
pub mod market_hours;
// Wave 3G (DEC-RUST-PULL-STATE-001): the written-vs-settled census fold —
// scoped rows/pairs in, counts + capped sample triples out; the payload
// extraction (`str()`/`int()`), the `repr()` renders and the guarded
// observe-only emit stay Python.
pub mod pull_census;
// Wave 1A (DEC-RUST-PULL-STATE-001): the last-pull OUTCOME decision core
// (DEC-OPERATOR-HEALTH-TRUTH-001 re-hosted) — codes only; every human string
// + the banner colour authority stay Python.
pub mod pull_outcome;
pub mod pull_scope;
// Wave 2F (DEC-RUST-PULL-STATE-001): the pull-progress DERIVED-METRICS core
// (DEC-PULL-PROGRESS-HONESTY-001 re-hosted) — percent's CPython banker's
// round(x, 1) reproduced bit-exactly by exact integer arithmetic inside the
// spike-proven <= 2**53 count domain; the counter state machine + every
// string/clock stay Python (the pyclass flip is a LATER lane phase).
pub mod progress;
// Wave 3H (DEC-RUST-PULL-STATE-001): the rate-bridge payload-verdict core
// (DEC-CLI-RATE-BRIDGE-001 + DEC-CLI-BRIDGE-PULL-ACTIVE-001 re-hosted) —
// codes only; the file read, the JSON decode (CPython's whole failure
// taxonomy), the float()/int() coercion PROBES and the RateBridgeSnapshot
// construction stay Python (see src/rate_bridge.rs).
pub mod rate_bridge;
// Wave 3H (DEC-RUST-PULL-STATE-001): the rate-bridge-cache memo decision
// core (the design §2.3 minimal kernel — serve arm + token revalidation);
// the NonBlockingFileMemoBase thread machinery (lock, deadline guard,
// _inflight clearing, degrade latch) is PERMANENT-PYTHON.
pub mod rate_bridge_cache;
pub mod stall;

pub use auto_pull::should_reload_master;
pub use auto_pull_gate::{
    GATE_BEFORE_SETTLE, GATE_MARKET_HOURS_BLOCKED, GATE_MUHURAT_DEFERRAL, GATE_NON_TRADING_DAY,
    GATE_OK, SETTLE_BAD_SHAPE, SETTLE_BELOW_FLOOR, SETTLE_NO_VALUE, SETTLE_NOT_WALL_CLOCK,
    SETTLE_OK, auto_gate_decision, muhurat_window_active, settle_token_verdict,
};
pub use auto_pull_scheduler::{auto_state_verdict, auto_tick_decision};
pub use intent::{refused_restore_decision, resume_decision, resume_enabled, shape_verdict};
pub use market_hours::{BLOCK_END_US, BLOCK_START_US, DAY_US, pull_allowed_us};
pub use progress::{COUNT_DOMAIN_MAX, CountDomainError, progress_derived};
pub use pull_census::{CENSUS_SAMPLE_CAP, census_fold};
pub use pull_outcome::{classify_pull_outcome, verdict_correlates};
pub use rate_bridge::{
    BRIDGE_FIELD_COUNT, BRIDGE_VERDICT_BAD_INPUT, BRIDGE_VERDICT_FIELD_BASE,
    BRIDGE_VERDICT_NOT_MAPPING, BRIDGE_VERDICT_SCHEMA, BRIDGE_VERDICT_VALID, FIELD_ABSENT,
    FIELD_BAD, FIELD_OK, PA_FALSE, PA_NONE, PA_TRUE, SCHEMA_KIND_BOOL, SCHEMA_KIND_FLOAT,
    SCHEMA_KIND_INT, SCHEMA_KIND_INT_HUGE, SCHEMA_KIND_MISSING, SCHEMA_KIND_OTHER,
    bridge_payload_verdict,
};
pub use rate_bridge_cache::{
    MEMO_ARM_CACHED_HIT, MEMO_ARM_FOLLOWER, MEMO_ARM_OWNER, memo_serve_arm, token_revalidates,
};
pub use stall::{
    WEDGE_ABSTAIN_PROBE_NONE, WEDGE_ADOPT_BASELINE, WEDGE_DEFER, WEDGE_ESCALATE, WEDGE_NO_PROBE,
    WEDGE_REBASELINE, alive_log_due, wedge_decision,
};

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs`),
/// baked at build time by `build.rs` under the shared FRAMING v1 of
/// `rust/source_digest_core.rs` (DEC-RUST-SOURCE-DIGEST-TWIN-001,
/// `docs/MIGRATION.md` #1258). The PyO3 module exports it as
/// `SOURCE_DIGEST` / `__source_digest__`.
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

#[cfg(feature = "python")]
mod python;
