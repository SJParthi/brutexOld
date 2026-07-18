# brutex-identity

Run-identity (BLAKE3) + results/window-ledger store kernels for BruteX
(**DEC-RUST-RUNID-STORES-001**, the L14 conversion lane).

The computation plane of four surfaces, hosted once in Rust:

| Python surface | Kernel |
|---|---|
| `brutex/strategies/combo.py` | `combo_hash` / `canonicalise_params_json` / `find_unit_separator_field` — the 9-field BLAKE3 run-identity join + the full param canonicaliser (CPython-repr floats, CPython-`json.dumps` byte-law, the `\x1f` screen) |
| `brutex/engine/snapshot_hash.py` | `SnapshotHasher` — the streamed `bxsnap2` digest (framing + ISO-8601 render + BLAKE3) |
| `brutex/engine/results_store.py` | `classify_result_append` / `validate_run_id_error` / `mask_to_limbs` / `mask_from_limbs` / `rank_top_k` — the store laws |
| `brutex/data/window_ledger_store.py` | `merge_ledger_rows` / `render_ledger_parquet` / `read_ledger_rows` / `LedgerStringPool` — the merge law + the canonical Parquet codec (the #1917 G10 resolution, option (iii): golden re-baseline; row fixed point is the correctness law) |

Build: `pip install ./rust/brutex-identity` (maturin, abi3-py312). Pure-Rust
core tests: `cargo test --release --locked`. Honest envelope: ADR-0020's
banner applies — uniformity, not speed; nothing here is on the mask hot path.
