# brutexd — BruteX Rust HTTP front (Wave 1)

The SERVING-track opener of the Rust-uniformity program: a small axum
binary that binds the operator port `127.0.0.1`-only, serves `/static/*`
(the `brutex/dashboard/static/` tree) and `/health` (pinned literal)
natively, and reverse-proxies every other path **verbatim** to the FastAPI
job host on a private loopback port.

- Default-OFF behind `BRUTEX_RUST_FRONT` (see
  `docs/runbooks/rust-front.md`); the launcher lives in
  `brutex/dashboard/rust_front.py`.
- Parity gates: `tests/integration/test_sidecar_parity_static.py`
  (Python, over real sockets against the real FastAPI app) +
  `tests/integration.rs` here (Rust, in-process).
- Leanness law: no duckdb / polars / parquet / arrow dependencies — brutexd
  never opens the lake in this wave (test-pinned).
- Never-500 discipline: no `unwrap()` on request paths; upstream failures
  are a plain-body 502; handler panics become a logged 500-class degrade
  via `CatchPanicLayer` (hence NO `panic = "abort"` in the release profile).

Build + test:

```sh
cargo build --release --manifest-path rust/brutexd/Cargo.toml
cargo test --manifest-path rust/brutexd/Cargo.toml
cargo clippy --all-targets --manifest-path rust/brutexd/Cargo.toml -- -D warnings
```
