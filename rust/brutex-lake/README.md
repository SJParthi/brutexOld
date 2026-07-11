# brutex-lake — the data-lake Rust render seam (ADR-0019 PR-1)

`DEC-RUST-DATALAKE-IO-ROADMAP-001` (MIGRATION.md #1726). This crate re-hosts
the ONE bars-writer parquet render seam
(`brutex/data/parquet_writer.py::_render_bytes` — frame → parquet bytes) on a
Rust build of polars REV-pinned at the commit the installed Python wheel's
release tag pointed at (tags are movable, so the manifest pins the commit —
see the ATOMIC-pin table below), so its output is **bit-identical** to the wheel's
`DataFrame.write_parquet(use_pyarrow=False)` under the writer's options
(zstd level 3, `row_group_size=100_000`, statistics off).

## Honesty banner (mandatory, per ADR-0019 §0)

**Expected payoff ≈ 0 — conversion is for uniformity, not speed.** The
encoder this crate links IS the same Rust code the Python wheel already runs;
the boundary additionally pays an Arrow-IPC handoff per render. The value is
the Rust-everywhere mandate's uniformity; the safety mechanism is
byte-identity under version lockstep.

## The ATOMIC pin

| Leg | Pin | Enforced by |
|---|---|---|
| Python wheel | `polars==1.42.1` (installed) | the facade's runtime **version guard** (`brutex/data/lake_rust_render.py` — engages ONLY when `polars.__version__ == pinned_polars_version()`) |
| This crate | every polars git dep REV-pinned at the commit the `py-1.42.1` tag pointed at (tags are movable; review-round fold) | `manifest_rev_matches_pinned_rev` + `lockfile_resolves_pinned_rev` (cargo tests) against `PINNED_POLARS_GIT_REV`; every CI cargo step `--locked`, maturin `locked = true` |
| zstd trio | zstd 0.13.3 / zstd-safe 7.2.4 / zstd-sys 2.0.16 (libzstd 1.5.7) via the **committed `Cargo.lock`** | `linked_libzstd_matches_pin` (cargo test) |

Bump ritual: `docs/runbooks/lake-rust-render-lockstep.md` — wheel bump ⇒ rev
bump ⇒ zstd re-pin (Cargo.lock) ⇒ parity corpus re-run ⇒ only then re-engage.

## Python surface

```python
import brutex_lake
brutex_lake.render_parquet_from_ipc(ipc_bytes)  # -> parquet bytes
brutex_lake.pinned_polars_version()             # -> "1.42.1"
brutex_lake.linked_zstd_version()               # -> "1.5.7"
```

Errors cross the FFI as `ValueError`/`RuntimeError` (the render AND the
result construction are each caught at the boundary — never a
`pyo3_runtime.PanicException`, which as a `BaseException` would escape the
facade's degrade catch; the facade additionally carries a narrow panic belt
— `Exception` plus the by-name-resolved `PanicException` class — as
defense-in-depth). The render runs GIL-free (`Python::detach`),
matching the wheel's own `write_parquet` posture.

## Build / test

```bash
cd rust/brutex-lake
cargo fmt --check && cargo clippy --all-targets --locked -- -D warnings && cargo test --release --locked
pip install maturin && pip install .   # or: maturin develop --release
```

Standalone crate — in NO Cargo workspace; built only by its own CI lane
(`.github/workflows/lake-rust-parity.yml`), never by ci.yml's fno-math /
trading-core jobs. First build compiles the polars graph from source
(~6.5 min wall / ~24 min CPU / ~12 GB target dir, measured in the spike).
Requires rustc ≥ 1.95 and network access to the pinned git deps (incl. the
restated `object_store` fork patch — cargo patches do not propagate through
git dependencies).
