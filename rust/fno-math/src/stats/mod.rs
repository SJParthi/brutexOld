//! Stage-3/4 statistics kernels (slice 6, DEC-METRICS-RUST-STATS-001).
//!
//! Rust ports of the measured-hot PURE-PYTHON loops inside the Stage-3/4
//! statistical-validity filters (`brutex/metrics/`). Scope law: Python keeps
//! every numpy pairwise reduction, every libm call (`log`, `pow`, `erf`),
//! all validation and every user-facing raise; Rust runs only the
//! sequential/integer heavy loops whose operation order the PYTHON SOURCE
//! ITSELF fixes. Each kernel is dispatched behind the default-OFF
//! `BRUTEX_STATS_RUST` flag through the `brutex.metrics.rust_stats` seam
//! (burn-in self-check + autorevert latch), and the pure-Python originals
//! are RETAINED FOREVER as the independent parity oracles (operator
//! exception — never deleted).
//!
//! ## Claim taxonomy (every kernel carries one tag)
//!
//! * **STRUCTURAL** — the floating-point operation order is fixed by both
//!   sides' own source (a sequential scalar loop in Python transcribed
//!   operation-for-operation); stable under numpy upgrades.
//! * **PINNED-EMPIRICAL** — depends on a probed numpy-internal order; fenced
//!   by burn-in + `==` goldens (none shipped in PR-1).
//! * **REPLICATED** — mirrors a numpy-internal algorithm (WFO only, if it
//!   ever ships; harness-fenced).
//!
//! TOLERANCE is used NOWHERE: every reported float feeds a count-changing
//! comparison, a `repr`-serialized report byte, or an `==` golden.
//!
//! ## Complexity (honest envelope — NOT O(1) total)
//!
//! Each kernel keeps the SAME asymptotics as its Python original — the win
//! is a constant factor only (the Python loop interpreted per iteration vs
//! compiled); claiming otherwise is FORBIDDEN. Per-call FFI copies are
//! disclosed on each `#[pyfunction]`.
//!
//! Modules: [`cpcv`] — the C(S, k) Chan/Pébay fold pass-count (PR-1);
//! [`binom`] — the exact binomial upper-tail gate for the low-support rescue
//! lane (DEC-STAGE1B-LOW-SUPPORT-RESCUE-001, LANE W3 — BORN-RUST, no Python
//! production copy; claim class STRUCTURAL within one platform, cross-language
//! bounded by the tests-side oracles, see its module docstring).

pub mod binom;
pub mod cpcv;
