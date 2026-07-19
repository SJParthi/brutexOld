# brutex-strategies — combo run-identity byte-law kernels

`DEC-STRATEGIES-RUST-COMBO-001` (T16 PR-1). A byte-identity-first Rust port
of EXACTLY two pure laws of `brutex/strategies/combo.py`:

* **`first_unit_separator`** — the `\x1f` first-offender scan of
  `reject_unit_separator` (DEC-RUN-IDENTITY-GUARD-PARITY-001). The kernel
  returns the INDEX of the first offending VALUE (or `None`); Python owns
  the field-name lookup, the error-message bytes (the literal-`\x1f`
  f-string) and the injected exception class, so the exception taxonomy is
  unchanged by construction.
* **`canonical_json`** — the canonical-JSON RENDER of `canonicalise_params`
  (`json.dumps(lowered, sort_keys=True, separators=(",", ":"),
  ensure_ascii=True)`) over the ALREADY-lowered tree. Canonicalisation
  (`_canonicalise_value`) stays Python: the crossing tree is
  dict/list/str/int/bool/None — float-free and Decimal-free BY LAW, so this
  crate never reproduces `float.__repr__` / `Decimal.__str__` (the
  load-bearing byte-safety property; #1914 precedent applied to a tree).

The serializer is the CPython `py_encode_basestring_ascii` byte law — a
SECOND Rust copy of `rust/brutex-lake/src/stats_shard.rs::push_json_string`
(no shared workspace; anti-drift via the SHARED-LITERAL cross-crate parity
corpus in `tests/canon_tests.rs`; consolidation is the named follow-up
`DEC-RUST-JSON-ESCAPE-COMMON-001`).

Consumed by `brutex/strategies/combo_rust.py` behind the default-OFF
`BRUTEX_STRATEGIES_RUST_COMBO` flag; any degrade condition (wheel
missing/stale, extraction failure, out-of-i128 int, lone surrogate,
non-exact-str dict key, kernel error/panic) falls back to the byte-identical
retained Python oracle.

**Honest envelope (ADR-0020 §0 banner):** expected payoff ≈ 0 — conversion
is for uniformity, not speed. Both laws run per combo MATERIALISATION
(launch / persist / verify / re-derivation), never inside the ~33B-trial
mask/bar hot loop.

Layout mirrors `rust/brutex-feed`: pure-Rust core (`cargo test` links no
libpython), PyO3 edge behind the optional `python` feature, maturin mixed
Python+Rust wheel (`python/brutex_strategies/` shim with the stale-wheel
handshake), `build.rs` source-digest stamp (DEC-RUST-SOURCE-DIGEST-TWIN-001),
cargo-mutants scope in `.cargo/mutants.toml` — with ONE deliberate profile
divergence: NO `panic = "abort"` (the `catch_unwind` FFI boundary needs
unwinding — the brutex-lake profile reasoning), `overflow-checks = true`
kept.
