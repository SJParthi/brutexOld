# brutex-sweep-ops

The sweep run-core/ops conversion crate (DEC-RUST-SWEEP-OPS-001): hosts the
Python→Rust conversion of the BruteX sweep run-core/ops remainder, one module
file per conversion slice, behind the default-OFF `BRUTEX_SWEEP_OPS_RUST`
facade (`brutex/engine/sweep_ops_rust.py`). The verbatim Python
implementations are RETAINED as the live parity oracles until the final
slice's default flip (its own DEC).

- Layout law: `src/lib.rs` is a frozen module manifest — one `pub mod` per
  slice, each slice a separate `.rs` file; `src/python.rs` grows by disjoint
  per-slice registration blocks.
- Carved out (never in this crate): sweep_parallel · sweep_cache · sweep_run
  · sweep_supervisor (in-flight PR ownership).
- Build: `maturin develop --release` (venv active). Tests: `cargo test`
  (default features — no libpython), `cargo clippy --all-targets -- -D warnings`.
- Flag OFF (the default) is byte-identical: the crate is never imported.
