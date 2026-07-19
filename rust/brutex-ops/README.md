# brutex-ops

The ops-lane Rust kernel crate (DEC-CROSSVERIFY-RUST-CORE first cut).

Re-hosts the TWO pure-law surfaces of `brutex/crossverify/` behind the
default-OFF `BRUTEX_CROSSVERIFY_RUST` facade
(`brutex/crossverify/rust_seam.py`), with the verbatim Python bodies retained
as the LIVE parity oracles:

1. **CSV byte assembly** (`crossverify_render_csv`) — the pinned TickVault
   cross-verify byte contract of `csv_render.render_day_csv`: byte-exact
   header, LF terminator + trailing newline, no BOM, comma-join with the
   no-quote-ever property, typed string-law refusal codes (empty rows /
   arity / non-increasing timestamps / metachars / control chars). The
   Decimal / datetime / session refusal laws and the `csv_safe`
   formula-guard check stay PYTHON-side (they operate on objects that never
   cross the ints+pre-formatted-strings FFI).
2. **Canonical identity + S3 key** (`crossverify_canonical_identity`,
   `crossverify_s3_key`) — the deny-by-default mapper + the ONE key law of
   `canonical.py`. The F&O grammar (`parse_fno_symbol`) is NEVER
   re-implemented — Python parses and passes the parsed fields.

## Lane structure (append-only)

One module per ops lane (`src/crossverify.rs` is the first). Future ops
lanes (backup, nightly, …) add their OWN `src/<lane>.rs` + a delimited
`crossverify_*`-style prefixed pyfunction block in `src/python.rs` — never
editing an existing lane's block.

## Build

Standalone crate (no workspace). `cargo test` builds the pure cores with
zero Python symbols; the wheel is built by maturin with the `python`
feature (`pip install ./rust/brutex-ops` inside the project venv). The
dedicated non-required CI lane is
`.github/workflows/ops-rust-parity.yml`.

## Honest envelope

Uniformity conversion (the ADR-0020 banner): expected payoff ≈ 0 — Python
still runs the full validation + cell pre-formatting and the FFI hop is a
small constant cost per file. No speedup is claimed.
