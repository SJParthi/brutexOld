# Runbook — the lake Rust-render version lockstep (ADR-0019 PR-1)

**Decision:** `DEC-RUST-DATALAKE-IO-ROADMAP-001` (MIGRATION.md #1726) ·
**Surfaces:** `rust/brutex-lake/` (crate) · `brutex/data/lake_rust_render.py`
(facade) · `brutex/data/parquet_writer.py::_render_bytes` (seam) ·
`.github/workflows/lake-rust-parity.yml` (the parity lane).

The Rust render is byte-identical to the Python render **ONLY under version
lockstep**: the crate's linked polars must be the git tag matching the
installed Python wheel, and the bundled libzstd must match on both sides.
There is NO version-independent parity (spike caveat #1). The runtime version
guard makes a broken lockstep SAFE (flag refuses, Python renders); this
runbook is how you RESTORE it.

## 0. Honest cost table (MEASURED, tagged)

| Item | Cost | Tag |
|---|---|---|
| Cold crate build (polars graph from source, release) | ~6.5 min wall / ~24 min CPU / ~12 GB target dir | MEASURED (spike, 2026-07-11) |
| Warm CI run (cargo cache hit) | minutes (our crate only recompiles) | MODELED |
| Per-render engaged overhead, 100k-row shape | IPC handoff ~6.6 ms + crate render ~12.5 ms ≈ 19 ms vs ~13 ms Python `write_parquet` tail → **+~6 ms (+47%) on the encode tail**, ~3% of the FULL `_render_bytes` wall (the Python-side frame build dominates) | MEASURED (2026-07-11 sandbox, median of 15) |
| Per-render engaged overhead, 10-row shape | ~1.1 ms vs ~0.5 ms Python tail (≈2×) | MEASURED (same run) |
| Burn-in (first 16 engaged renders/process) | 2× render cost, bounded, amortised-nil | MODELED |
| Payoff | **≈ 0 — uniformity, not speed** (the engaged path is a small measured SLOWDOWN; the crate links the same encoder the wheel runs) | MEASURED/MODELED per ADR-0019 §0/§4 |

## 1. The ATOMIC pin — three legs that move together

1. **Python wheel**: the installed `polars` version (repo pyproject
   range-pins `>=1.0,<2.0` with no lockfile — ADR-0019 F4 — so the installed
   version is environment truth, not a manifest fact).
2. **Crate tag**: every polars git dependency in
   `rust/brutex-lake/Cargo.toml` at `tag = "py-<wheel version>"`, single
   source `PINNED_POLARS_PY_VERSION` in `rust/brutex-lake/src/lib.rs`
   (cargo-tested: `manifest_tag_matches_pinned_version`).
3. **zstd trio**: the COMMITTED `rust/brutex-lake/Cargo.lock` must resolve
   the same zstd stack the wheel's own lock pins at that tag (at 1.42.1:
   zstd 0.13.3 / zstd-safe 7.2.4 / zstd-sys 2.0.16 → libzstd 1.5.7;
   cargo-tested: `linked_libzstd_matches_pin` against
   `PINNED_LIBZSTD_VERSION`).

## 2. The bump ritual (wheel bump ⇒ crate bump ⇒ re-prove ⇒ re-engage)

Run ALL steps for ANY polars wheel bump. Nothing re-engages until step 7.

1. **Pin the wheel deliberately.** Decide the target `polars==X.Y.Z`. Verify
   the git tag `py-X.Y.Z` exists upstream
   (`git ls-remote --tags https://github.com/pola-rs/polars "py-X.Y.Z"`).
2. **Bump the crate tag.** In `rust/brutex-lake/src/lib.rs` set
   `PINNED_POLARS_PY_VERSION = "X.Y.Z"`; in `Cargo.toml` move all FIVE
   polars git deps to `tag = "py-X.Y.Z"`. Re-check the upstream workspace's
   `[patch.crates-io]` at the new tag — the restated patches in our
   manifest (object_store fork et al.) must be updated to whatever revs the
   NEW tag's lock pins (patches do not propagate through git deps).
   Feature-subset drift is a live hazard: the minimum feature set that
   COMPILES can change between tags (at 1.42.1 `strings` was load-bearing).
3. **Re-pin the zstd trio.** Read the polars repo's `Cargo.lock` AT THE NEW
   TAG for its zstd / zstd-safe / zstd-sys versions. Regenerate our lock
   (`cargo generate-lockfile`), then force the trio (and any other
   encode-path crate the new tag's lock pins differently) with
   `cargo update -p <crate> --precise <ver>`. Update
   `PINNED_LIBZSTD_VERSION` if the bundled libzstd moved. Commit the new
   `Cargo.lock`.
4. **cargo gates.** `cargo fmt --check && cargo clippy --all-targets -- -D
   warnings && cargo test --release` in `rust/brutex-lake/` — the lockstep
   pin tests must be green (they fail LOUD on a manifest/lock drift).
5. **Rebuild + reinstall the wheel** in the target environment:
   `pip install ./rust/brutex-lake` (with `polars==X.Y.Z` installed).
6. **Re-run the parity corpus.**
   `pytest -rs tests/golden/test_lake_rust_render_parity.py` — all 5 corpus
   cases + the whole-write-path test must PASS (not skip: a skip means the
   version guard still sees a mismatch — step 2 or 5 is incomplete).
   CI-side, trigger `lake-rust-parity.yml` via workflow_dispatch and check
   the drift step reads "lockstep aligned".
7. **Only then re-engage** `BRUTEX_LAKE_RUST_RENDER=1` where wanted. The
   burn-in re-arms per process automatically (first 16 engaged renders
   byte-compare against the Python oracle).

## 3. What a version-guard disengagement looks like in logs

One WARN per process, then permanent Python-render service (no repeats, no
errors, no behaviour change beyond which engine encodes):

```
[warning ] lake_rust_render.rust_path_disabled reason=polars_version_mismatch
           installed_polars=1.43.0 crate_pinned_polars=1.42.1
           remedy=align the wheel and the crate tag, then re-run the parity
           corpus (docs/runbooks/lake-rust-render-lockstep.md)
```

Sibling shapes: `reason=crate_unavailable` (crate not installed — remedy
`pip install rust/brutex-lake`); `lake_rust_render.fallback
reason=render_error` (a per-process degrade, not a latch);
`lake_rust_render.render_autoreverted reason=burn_in_byte_mismatch` (ERROR —
see §4). A healthy engagement logs exactly one
`lake_rust_render.engaged` INFO (with the pinned polars + linked zstd) and,
16 clean compares later, one `lake_rust_render.burn_in_complete`.

## 4. Escalation

* **Parity FAIL at a MATCHING tag** (the corpus diverges, or a production
  process logs `lake_rust_render.render_autoreverted` while versions agree)
  → **escalate to the coordinator** with the failing case's two sha256s and
  the environment's `polars.__version__` / `brutex_lake.linked_zstd_version()`.
  This is the "encoder changed under an unchanged tag" class (a rebuilt
  wheel with a different bundled libzstd, a patched distro wheel, a
  transitive encode-path crate drifting past our lock) — it may require
  extending the pinned set, and it is exactly the hazard the priced
  `DEC-LAKE-EXACT-PIN-001` fallback exists for (ADR-0019 §3 LAST-MILE).
  Do NOT paper over it by regenerating expectations: the Python bytes are
  the contract.
* **A wheel version with NO matching `py-*` tag** (yanked/renamed) → stay
  disengaged (the guard already keeps you safe) and escalate: the lockstep
  cannot be restored without an upstream tag.
* Burn-in autorevert in production is ALWAYS report-worthy even though it is
  self-healing — the oracle bytes shipped, but the crate disagreed on real
  data the corpus missed; attach the logged `rust_len`/`python_len`.

## 5. Standing facts

* Default OFF (`BRUTEX_LAKE_RUST_RENDER` unset ⇒ pure Python path,
  byte-identical, zero new imports). A junk flag value is loud-OFF.
* The Python render is the LIVE parity oracle — it is never removed while
  this seam exists (constraint P10; the burn-in and every degrade path
  depend on it).
* The main CI test job never installs the crate; only
  `lake-rust-parity.yml` (and local operators) do.
* `MemoryError` on the engaged path maps to `LakeWriteHalt` through the
  writer's `lake_write_guard`, identical to the pure-Python render.
* The crate is in NO Cargo workspace and NO other workflow — ci.yml's rust
  jobs (fno-math / trading-core, RUST_VERSION 1.93.1) never build it;
  brutex-lake's floor is rustc 1.95 (`LAKE_RUST_VERSION` 1.97.0 in its own
  lane).
