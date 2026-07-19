//! brutex-sweep-ops — the sweep run-core/ops conversion crate.
//!
//! `DEC-RUST-SWEEP-OPS-001`: hosts the Python→Rust conversion of the sweep
//! run-core/ops remainder, one module file per conversion slice, behind the
//! default-OFF `BRUTEX_SWEEP_OPS_RUST` Python facade
//! (`brutex/engine/sweep_ops_rust.py`). The Python implementations stay the
//! LIVE parity oracles until the final slice's default flip (its own DEC).
//!
//! ## Module manifest law (slice 0)
//!
//! This file is a PURE MANIFEST: one `pub mod` per planned slice, each slice
//! a separate `.rs` file. Later slice workers touch ONLY their own module
//! file (and add one registration block in `python.rs`) — this file should
//! never need another edit, so it can never become a merge-collision hub.
//!
//! Slice map (see the scoping doc's slice plan for the file-level breakdown):
//! * `leaves`      — slice 1: pure leaf laws (run_layout · sweep_identity ·
//!   sweep_stall_policy …). SHIPPING (this crate revision).
//! * `audit_chain` — slice 2: the BLAKE3 hash-chain laws (sweep_audit). STUB.
//! * `artifacts`   — slice 3: durable run-artifact laws (checkpoint ·
//!   exit-status · liveness · quarantine · manifest · stamps …). STUB.
//! * `ops`         — slice 4: process/lock ops decision laws (singleflight ·
//!   scheduler · launch …). STUB.
//! * `verify`      — slice 5: verify/heal/apriori/diffcheck pure cores. STUB.
//! * `run_core`    — slice 6: driver/orchestrator/run-core pure derivations.
//!   STUB.
//!
//! CARVED OUT (never in this crate): sweep_parallel · sweep_cache ·
//! sweep_run · sweep_supervisor (in-flight PR ownership).

pub mod artifacts;
pub mod audit_chain;
pub mod leaves;
pub mod ops;
pub mod run_core;
pub mod verify;

#[cfg(feature = "python")]
mod python;
