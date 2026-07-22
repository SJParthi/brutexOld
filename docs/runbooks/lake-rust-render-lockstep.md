# Runbook — the lake Rust-render version lockstep (roadmap PR-1 + PR-2)

**Decision:** `DEC-RUST-DATALAKE-IO-ROADMAP-001` (MIGRATION.md #1891; the
datalake conversion roadmap ADR,
`docs/adr/0020-datalake-rust-conversion-roadmap.md`) ·
**Surfaces:** `rust/brutex-lake/` (crate) · `brutex/data/lake_rust_render.py`
(facade — TWO render profiles since PR-2: `bars` = the spot writer's
zstd-3/rg-100k/stats-OFF shape via
`brutex/data/parquet_writer.py::_render_bytes`, and `options_defaults` =
the expired-options writer's ALL-DEFAULTS `df.write_parquet(path)` shape —
statistics min/max/null_count ON, Zstd(None), rg=None → the streaming
sink's 122,880-row split — via
`brutex/data/option_parquet_writer.py::_atomic_write_parquet`) ·
`.github/workflows/lake-rust-parity.yml` (the parity lane, BOTH corpora).

The Rust render is byte-identical to the Python render **ONLY under version
lockstep**: the crate's linked polars must be REV-pinned at the commit the
installed Python wheel's release tag pointed at (tags are movable, so the
manifest pins the commit — §2 step 1 below is the tag → rev resolution), and
the bundled libzstd must match on both sides.
There is NO version-independent parity (spike caveat #1). The runtime version
guard makes a broken lockstep SAFE (flag refuses, Python renders); this
runbook is how you RESTORE it.

## 0. Honest cost table (MEASURED, tagged)

MEASURED provenance: the timing figures are 2026-07-11 sandbox-spike
transcripts (median of 15) — no committed bench reproduces them, so treat
them as directional; each one DISCLOSES a cost, never a speedup.

| Item | Cost | Tag |
|---|---|---|
| Cold crate build (polars graph from source, release) | ~6.5 min wall / ~24 min CPU / ~12 GB target dir | MEASURED (spike, 2026-07-11) |
| Warm CI run (cargo cache hit) | minutes (our crate only recompiles) | MODELED |
| Per-render engaged overhead, 100k-row shape | IPC handoff ~6.6 ms + crate render ~12.5 ms ≈ 19 ms vs ~13 ms Python `write_parquet` tail → **+~6 ms (+47%) on the encode tail**, ~3% of the FULL `_render_bytes` wall (the Python-side frame build dominates) | MEASURED (2026-07-11 sandbox, median of 15) |
| Per-render engaged overhead, 10-row shape | ~1.1 ms vs ~0.5 ms Python tail (≈2×) | MEASURED (same run) |
| Engaged transient PEAK MEMORY (review-round fold) | the render concurrently holds the source frame + a FULL **uncompressed** Arrow-IPC copy (≈ the frame's in-RAM size — MEASURED 2026-07-11: 250k-row 7-col frame ≈ 14 MB → IPC ≈ 14 MB ≈ 2.3× the ~6 MB compressed parquet) + the crate's re-decoded rechunked frame + the crate parquet buffer → **≈ 2.5× the Python path's transient peak (≈ 3× during burn-in, when BOTH engines' parquet buffers are live for the compare)**; a 17-col Greeks month scales the same way | MEASURED sizes / MODELED peak (2026-07-11) |
| GIL during the engaged render | RELEASED (`Python::detach` — the review-round fold; the wheel's own `write_parquet` releases it too), so an engaged render no longer serializes sibling writer/dashboard threads. Pre-fold builds of the crate held it for the full ~12.5 ms render | MEASURED semantics (code), no timing claim |
| Thread-pool footprint | the crate is a SEPARATE polars build with its OWN global rayon pool beside the wheel's — an engaged process carries up to 2× polars threads on N cores (both pools read `POLARS_MAX_THREADS` independently; the env var bounds each, it does not de-duplicate them) — a further reason the engaged default stays OFF | MODELED (structural) |
| Burn-in (first 16 engaged renders/process PER PROFILE + once per NEW profile-prefixed schema shape) | 2× render cost, bounded, amortised-nil — AND, because the caller holds the partition's cross-process flock around the render, those renders hold that flock ~2-3× longer (a concurrent CLI-fetch process waiting on the SAME partition waits with them; bounded to the burn-in window). PR-2: the option writer's heal path can mint novel stored-schema shapes; the seen-set is capped (64) — past the cap novel shapes compare on EVERY render (more oracle compares, never fewer) | MODELED |
| DEFAULTS profile (PR-2, option month files ≈ 0.5 MB / ~16.5k rows) | the same engaged-slowdown class at smaller absolute deltas (the option render is heal-/option-pull-driven O(month-file) glue, not a hot path); statistics ON means MORE upstream code feeds the bytes — expect the options corpus to trip on version bumps the bars corpus survives (that is the apparatus working, §2 step 7) | MODELED (PR-2 probe) |
| Payoff | **≈ 0 — uniformity, not speed** (the engaged path is a small measured SLOWDOWN; the crate links the same encoder the wheel runs) | MEASURED/MODELED per the roadmap ADR §0/§4 |

## 1. The ATOMIC pin — three legs that move together

1. **Python wheel**: the installed `polars` version (repo pyproject
   range-pins `>=1.0,<2.0` with no lockfile — ADR-0020 F4 — so the installed
   version is environment truth, not a manifest fact).
2. **Crate rev**: every polars git dependency in
   `rust/brutex-lake/Cargo.toml` REV-pinned at the exact commit the
   upstream `py-<wheel version>` tag points at (tags are MOVABLE — a moved
   tag re-resolving would be silently different bytes; review-round fold).
   Single sources `PINNED_POLARS_GIT_REV` + `PINNED_POLARS_PY_VERSION` in
   `rust/brutex-lake/src/lib.rs` (cargo-tested:
   `manifest_rev_matches_pinned_rev` holds the manifest,
   `lockfile_resolves_pinned_rev` holds the committed `Cargo.lock`; every
   CI cargo step runs `--locked` and the maturin wheel build enforces the
   lock via `locked = true`).
3. **zstd trio**: the COMMITTED `rust/brutex-lake/Cargo.lock` must resolve
   the same zstd stack the wheel's own lock pins at that tag (at 1.42.1:
   zstd 0.13.3 / zstd-safe 7.2.4 / zstd-sys 2.0.16 → libzstd 1.5.7;
   cargo-tested: `linked_libzstd_matches_pin` against
   `PINNED_LIBZSTD_VERSION`).

## 2. The bump ritual (wheel bump ⇒ crate bump ⇒ re-prove ⇒ re-engage)

Run ALL steps for ANY polars wheel bump. Nothing re-engages until step 8.

1. **Pin the wheel deliberately.** Decide the target `polars==X.Y.Z`.
   The pyproject `polars==` pin (the `data` extra —
   DEC-CI-POLARS-LOCKSTEP-PIN-001) moves in the same commit.
   Resolve the git tag `py-X.Y.Z` to its COMMIT upstream
   (`git ls-remote --tags https://github.com/pola-rs/polars "py-X.Y.Z"` —
   the printed hash is the new rev; for an annotated tag take the `^{}`
   peeled line).
2. **Bump the crate rev.** In `rust/brutex-lake/src/lib.rs` set
   `PINNED_POLARS_PY_VERSION = "X.Y.Z"` AND `PINNED_POLARS_GIT_REV` to the
   resolved commit; in `Cargo.toml` move all FIVE polars git deps to
   `rev = "<that commit>"` (rev-pinned, never the movable tag — the
   review-round law). Re-check the upstream workspace's
   `[patch.crates-io]` at the new rev — the restated patches in our
   manifest (object_store fork et al.) must be updated to whatever revs the
   NEW tag's lock pins (patches do not propagate through git deps).
   Feature-subset drift is a live hazard: the minimum feature set that
   COMPILES can change between tags (at 1.42.1 `strings` was load-bearing).
3. **Re-pin the zstd trio.** Read the polars repo's `Cargo.lock` AT THE NEW
   REV for its zstd / zstd-safe / zstd-sys versions. Regenerate our lock
   (`cargo generate-lockfile`), then force the trio (and any other
   encode-path crate the new rev's lock pins differently) with
   `cargo update -p <crate> --precise <ver>`. Update
   `PINNED_LIBZSTD_VERSION` if the bundled libzstd moved. Commit the new
   `Cargo.lock`.
4. **Re-derive the DEFAULTS snapshot by hand (PR-2).** The crate's
   `defaults_writer_options()` HARDCODES what the wheel's zero-kwarg
   `df.write_parquet(path)` resolves at the pinned rev — the crate cannot
   read the wheel's Python, so a bump must re-read the NEW tag's default
   resolution: `polars/dataframe/frame.py::write_parquet` →
   `sink_parquet`'s `statistics=True` dict, the
   `polars-python/src/lazyframe/general.rs` binding (compression /
   truncation / row_group_size defaults), `conversion/mod.rs`'s
   `Wrap<StatisticsOptions>` mapping, and the streaming sink's
   `TakeableRowsProvider` default (122,880 rows at 1.42.1, env-overridable
   via `POLARS_IDEAL_SINK_MORSEL_SIZE_ROWS/_BYTES`). Update
   `defaults_writer_options()` + `DEFAULTS_ROW_GROUP_SPLIT` + their cargo
   pins if anything moved. The MECHANICAL detector for a missed move is the
   options corpus FAILING in step 7 — and because statistics widen the
   byte blast radius, expect the options corpus to trip on bumps the bars
   corpus survives (the apparatus working, not flaking).
5. **cargo gates.** `cargo fmt --check && cargo clippy --all-targets
   --locked -- -D warnings && cargo test --release --locked` in
   `rust/brutex-lake/` — the lockstep pin tests must be green (they fail
   LOUD on a manifest/lock drift, and `--locked` refuses a stale lock
   instead of silently re-resolving it).
6. **Rebuild + reinstall the wheel** in the target environment:
   `pip install ./rust/brutex-lake` (with `polars==X.Y.Z` installed).
7. **Re-run BOTH parity corpora.**
   `pytest -rs tests/golden/test_lake_rust_render_parity.py
   tests/golden/test_option_lake_rust_render_parity.py` — the 6 bars cases
   + the 12 options-defaults cases + the whole-path twins must PASS (not
   skip: a skip means the version guard still sees a mismatch — step 2 or
   6 is incomplete). CI-side, trigger `lake-rust-parity.yml` via
   workflow_dispatch and check the drift step reads "lockstep aligned".
8. **Only then re-engage** `BRUTEX_LAKE_RUST_RENDER=1` where wanted. The
   burn-in re-arms per process automatically (first 16 engaged renders PER
   PROFILE byte-compare against the Python oracle).

## 3. What a version-guard disengagement looks like in logs

One WARN per process, then permanent Python-render service (no repeats, no
errors, no behaviour change beyond which engine encodes):

```
[warning ] lake_rust_render.rust_path_disabled reason=polars_version_mismatch
           installed_polars=1.43.0 crate_pinned_polars=1.42.1
           remedy=align the wheel and the crate's pinned polars rev, then
           re-run the parity corpus
           (docs/runbooks/lake-rust-render-lockstep.md)
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
  `DEC-LAKE-EXACT-PIN-001` fallback exists for (ADR-0020 §3 LAST-MILE).
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
  `lake-rust-parity.yml` (and local operators) do. **Flip precondition
  (review-round fold):** real-crate byte-identity is therefore enforced
  ONLY by that NON-REQUIRED lane — the future default-ON flip DEC must
  either make the lane required or cite a fresh green run of it as a
  stated precondition (never flip on the lane's mere existence).
* **OOM honesty, scoped precisely (review-round fold):** a PYTHON-SURFACED
  `MemoryError` on the engaged path (BytesIO/object-list allocations, the
  IPC handoff) maps to `LakeWriteHalt` through the writer's
  `lake_write_guard`, identical to the pure-Python render. A
  Rust-encoder-INTERNAL allocation failure is NOT gracefully mapped on
  EITHER side — the wheel's own `write_parquet` is the same Rust polars
  and shares the class (allocator abort, or a panic: on the crate side
  mapped to `RuntimeError` at the FFI boundary → the facade degrades to
  the oracle; an allocator ABORT kills the process on both paths). The
  seam neither narrows nor widens that envelope.
* **Standing hazard (unproven residual):** the crate's render runs the
  polars STREAMING engine, which may execute on its own worker threads;
  `catch_unwind` unwinds only the calling thread, so the "every panic
  becomes a catchable PyErr" boundary relies on polars-stream propagating
  a worker panic back to the collector (its observed behaviour — not
  provable in a crate unit test). Bounded regardless: the facade catches
  an escaped `pyo3_runtime.PanicException` (the NARROW panic belt —
  `(Exception, <resolved PanicException class(es)>)`, never a literal
  `except BaseException` per the repo's CodeQL py/catch-base-exception
  convention; a distinct per-extension panic type object the resolver
  cannot name propagates — the disclosed narrowing in
  `lake_rust_render._panic_belt_errors`), latches `crate_panic`, and
  serves the oracle.
* The crate is in NO Cargo workspace and NO other workflow — ci.yml's rust
  jobs (fno-math / trading-core, RUST_VERSION 1.93.1) never build it;
  brutex-lake's floor is rustc 1.95 (`LAKE_RUST_VERSION` 1.97.0 in its own
  lane).
* **Morsel determinism row (PR-2):** the defaults profile's row-group split
  rides `POLARS_IDEAL_SINK_MORSEL_SIZE_ROWS` / `..._BYTES` (default 122,880
  rows) — GUARD-INVISIBLE determinism inputs, not a parity hazard: wheel
  and crate read the same process env through the same code, so parity
  holds per-identical-env. What they threaten is (a) the LANE proving
  parity on a non-production split — closed by the assert-unset workflow
  step + the in-suite RG-split pins — and (b) stored-byte reproducibility
  across differently-configured hosts, a PRE-EXISTING property of the
  flag-OFF wheel path (its streaming sink reads the same vars), not
  introduced by the seam. **Production never sets them.**
* **Binary-stats truncation (PR-2 empirical correction):** the write
  option `binary_statistics_truncate_length = None` does NOT mean
  untruncated — polars-parquet resolves it to an internal default
  (observed at 1.42.1: 64-byte truncate, max-incremented). Wheel and crate
  share the policy (parity unaffected); the o11 corpus sentinel pins it as
  data for bump-time triage.
* **Shared latch across writers (PR-2):** ONE process latch for BOTH
  profiles — a bars-path autorevert silences the options path and vice
  versa (fail-closed both directions; the oracle always serves). A stale
  PR-1-vintage crate missing `render_parquet_defaults_from_ipc` latches
  `crate_unavailable` for BOTH surfaces (the all-or-nothing `_CRATE_API`
  check — lockstep is an atomic pin; rebuild per §2).
* **No per-write runtime witness on the options path (PR-2):** the options
  writer computes no `raw_hash` (its stats-manifest rows carry
  `raw_hash=None`), so after burn-in an in-production divergence on a
  shape both the corpus and the burn-in missed would ship undetected crate
  bytes. Bounded by: corpus breadth (12 cases incl. the statistics
  surfaces), the per-profile 16-render budget + shape gate, default-OFF,
  and the oracle-always-available posture. Deliberately NOT adding a
  raw_hash to the options writer — a lake-schema/manifest behavior change
  smuggled into a render PR (scope lock).
* **Rebuilt-same-version wheel residual — strictly WIDER on the defaults
  profile:** with statistics ON, more upstream code paths feed the bytes,
  so the PR-1 residual (a same-`__version__` rebuilt/patched wheel is
  invisible to the version guard) has a larger surface here. Cover
  unchanged: the burn-in byte-compare autoreverts on first divergence
  (ships oracle bytes) + the §4 escalation. Not claimed closed.
