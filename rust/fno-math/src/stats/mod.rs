//! Stage-3/4 statistics kernels (slice 6, DEC-METRICS-RUST-STATS-001).
//!
//! Rust ports of the measured-hot PURE-PYTHON loops inside the Stage-3/4
//! statistical-validity filters (`brutex/metrics/`). Scope law: Python keeps
//! all validation and every user-facing raise; Rust runs the
//! sequential/integer heavy loops whose operation order the PYTHON SOURCE
//! ITSELF fixes. PR-2 (DEC-METRICS-RUST-STATS-002) amends the original
//! "Python keeps every numpy pairwise reduction and every libm call" clause
//! with two proven-safe exceptions: `log`/`exp` lower to the SAME libm
//! symbols CPython calls (no transcription exists to drift), `erf` is BOUND
//! to that same symbol ([`norm`]'s FFI law — a transcription was MEASURED
//! non-bit-exact on glibc ≥ 2.28), and the ONE numpy pairwise reduction
//! shipped ([`wfo`]) is a REPLICATED mirror of numpy's own pairwise_sum,
//! probe-proven + burn-in-fenced. Each kernel is dispatched behind the default-OFF
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
//! [`norm`] — shared standard-normal numerics (system-libm `erf` + the
//! AS241 inverse-CDF transcription); [`deflated_sharpe`] — the PSR + SR_0
//! DSR kernels; [`pbo`] — the CSCV split-enumeration logit loop; [`wfo`] —
//! the walk-forward pass-count with the numpy-pairwise REPLICATED mean
//! (all PR-2, DEC-METRICS-RUST-STATS-002).

pub mod cpcv;
pub mod deflated_sharpe;
pub mod norm;
pub mod pbo;
pub mod wfo;
