# ADR-0020 — Rust conversion ROADMAP for the data-lake I/O layer (operator override: Rust everywhere, byte-identity gated)

**Decision ID:** `DEC-RUST-DATALAKE-IO-ROADMAP-001` (MIGRATION.md entry #1891 — renumbered #1832 → #1891 at the club CDR2 fold; DEC-IDs are the stable handles per DEC-LEDGER-RENUMBER-001)
**Date:** 2026-07-11
**Status:** **ACCEPTED** — operator-directed. DOCS-ONLY in this commit: the
roadmap is adopted; zero code ships here; every conversion PR lands
default-OFF behind an env flag with the Python path retained as the live
parity oracle.
**Provenance:** the slice-8 assessment session (6 readers → 3-designer panel
(maximalist / conservative / byte-parity-oracle) → judge) rendered a
keep-Python verdict; the **operator OVERRODE it to Rust-everywhere**. The
panel's findings are NOT discarded — they are this roadmap's risk register
(§6) and standing-hazard list (§7), and every fact below was re-verified at
HEAD `adab6aa2`.
**Umbrella:** the Rust-first conversion program, `DEC-RUST-FIRST-MANDATE-001`
(ledger #1713 — riding the Slice-1 PR, IN FLIGHT and NOT on main at this HEAD;
cited as the program anchor, never as landed).
**Scope:** the P10 "data-lake IO wrappers" slice — 16 `brutex/data/` modules +
`brutex/feed/resampler.py` (17 total, §5). ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate/lake-schema change; F&O lake stays
REFERENCE_ONLY.

---

## 0. Honesty banner (MANDATORY wording, per the standing ADR law)

**Expected payoff ≈ 0 — conversion is for uniformity, not speed.** This layer
is IO-bound glue over native engines; Rust does not change O() for fsync /
`os.replace` / footer scans. Every payoff figure in this record carries a
MEASURED / MODELED / UNKNOWN tag (§4). The mandate's value here is
uniformity; the non-negotiable safety mechanism is byte-identity.

## 1. Directive and mechanism

Operator directive: **Rust everywhere** — slice 8 flips from
assessed-and-declined to CONVERT, with **bit-identical stored bytes vs the
Python writers as the non-negotiable gate** (rendered-parquet byte-identity;
partition-lock/flock semantics; `LakeWriteHalt` mapping).

The safety mechanism that makes this tractable: the port calls **polars'
NATIVE Rust API — the same encoder family the Python path already invokes**
(`df.write_parquet(..., use_pyarrow=False)`,
`brutex/data/parquet_writer.py:1862-1869` in `_render_bytes`) — so parity is
achievable **by construction IF the crate↔wheel version lockstep holds**: the
Rust crate must resolve the same polars encoder + zstd stack the installed
Python wheel carries, and the gate must fail LOUD (flag stays OFF) whenever
they diverge. An empirical spike verified exactly this (does a Rust polars
build byte-match the installed wheel's parquet output for the writer's render
shape?) before this commit landed; its verdict:

> **Spike result (2026-07-11; full report in the session records):
> FEASIBLE-BY-LOCKSTEP.** A Rust crate build of polars at git tag
> `py-1.42.1` (= the installed wheel 1.42.1; workspace crate version 0.54.4,
> commit `0df0c25d`) reproduced the Python wheel's parquet bytes
> **BIT-IDENTICALLY on all 5 corpus cases** — 10-row spot; 50-row 17-column
> Greeks options with nulls; 100,000 rows (the exact row-group boundary);
> 100,001 rows (2 row groups); 250,000 rows (3 row groups) — sha256 AND raw
> `cmp` equal, in debug AND release profiles, thread-count invariant
> (`POLARS_MAX_THREADS` 1/2/8 on the Python side, 1/13 on the Rust side).
> The corpus was rendered through the REAL `ParquetWriter._render_bytes`
> with real `CandleBar`/`FnoGreeksBar` objects. Load-bearing discovery: at
> 1.42.1, `write_parquet(use_pyarrow=False)` routes through the NEW
> STREAMING engine — `lazy().sink_parquet(engine=streaming)` with eager
> opt-flags (polars-python's `general.rs` at line 683 of the `py-1.42.1`
> tag) — so the port must mirror that EXACT call chain, not the old eager
> ParquetWriter. Lockstep costs, measured: rustc ≥ 1.95 required (the wheel
> builds on nightly); the polars workspace's `[patch.crates-io]`
> `object_store` fork must be RESTATED in our crate (git dependencies do
> not propagate patches); the `strings` feature is required at this tag;
> the zstd trio resolved byte-equal to the wheel's lock WITHOUT pinning
> today (zstd 0.13.3 / zstd-safe 7.2.4 / zstd-sys 2.0.16 + libzstd 1.5.7)
> but MUST be pinned in production; release build 6m26s wall / ~24 min CPU
> / ~12 GB target dir. Honest scope: this proves the RENDER byte-surface
> only, per-wheel-version — the production design therefore carries a
> wheel==tag==zstd ATOMIC pin, plus a version guard (installed wheel ≠ the
> crate's pinned version ⇒ the Rust flag REFUSES to engage with one loud
> WARN — fail-closed, never silently different bytes), plus the CI corpus
> parity gate on every bump. The empty-frame case is structurally
> unreachable (`write()` raises on empty; the healer unlinks rather than
> renders).

## 2. Facts the roadmap stands on (each re-verified at this HEAD)

| # | Fact | Verified at |
|---|------|-------------|
| F1 | The bars writer renders via **polars' Rust encoder**, NOT pyarrow: `df.write_parquet(BytesIO, compression="zstd", compression_level=ZSTD_LEVEL, row_group_size=ROW_GROUP_SIZE, use_pyarrow=False, statistics=False)`; the sha256 of the buffer BEFORE disk is the recorded `raw_hash`. (Corrects an earlier inventory claim of a "pyarrow deterministic render" — pyarrow there is a read-only footer probe.) | `brutex/data/parquet_writer.py:1862-1869` (`use_pyarrow`, in `_render_bytes`); constants `ROW_GROUP_SIZE`/`ZSTD_LEVEL` at `brutex/data/parquet_writer.py:158-161`; `raw_hash` at `brutex/data/parquet_writer.py:759` |
| F2 | The expired-options writer renders via `atomic_write_via(dest, df.write_parquet)` — polars with ALL DEFAULTS (no compression args), so its byte target is additionally sensitive to encoder-default churn across polars 1.x. | `brutex/data/option_parquet_writer.py:947` (in `_atomic_write_parquet`) |
| F3 | The window-ledger store writes `pq.write_table(table, tmp)` (pyarrow, all defaults) at BOTH sites; pyarrow's parquet encoder is Arrow **C++** with NO Rust build, and the module's own docstring disclaims byte parity — the `to_rows` row-level fixed point is the contract: "Parquet *file bytes* may differ across writers via embedded library metadata; the ROW CONTENT is the reproducible contract, and every test asserts the row fixed point, not the byte stream." | `brutex/data/window_ledger_store.py:795,927` (both `pq.write_table` call sites — the symbol is the anchor); docstring at `brutex/data/window_ledger_store.py:16-18` |
| F4 | The engine pins are RANGES with NO lockfile: `polars>=1.0,<2.0`, `duckdb>=1.0,<2.0`, `pyarrow>=16,<25`; no `*.lock` / requirements / constraints file exists in the repo root. The lockstep the byte gate needs does NOT exist yet on the Python side. | `pyproject.toml:48-50` |
| F5 | One frozen cross-language byte oracle already exists in the slice: the golden pins `render_shard`'s exact byte string (`_EXPECTED_BARS_SHARD`); the shard JSON law is stdlib compact sorted-key `json.dumps` with no floats in the payload. | `tests/golden/test_lake_stats_manifest_golden.py:66` (`render_shard`); `brutex/data/lake_stats_manifest.py:838` (`json.dumps` render) and `brutex/data/lake_stats_manifest.py:540` (the sha256 shard-path token) |
| F6 | `brutex/feed/resampler.py` is production-DEAD and AST-drift-pinned: `tests/unit/test_feed_no_epoch_resampler_in_production.py` proves no production module imports it (DEC-SUSPICION-SWEEP-FIXES-001 G3, MIGRATION.md #1492). It serves as a stdlib-pure Python test ORACLE — the mandate's own oracle carve-out. | test file present at this HEAD |
| F7 | Every parquet byte pin in the slice today is a same-process, same-engine-version A/B against the LIVE installed engine; there are no frozen on-disk parquet SHA fixtures. The roadmap's A/B gates extend this architecture across the language boundary (Rust-path bytes vs Python-path bytes in the SAME environment) rather than freezing bytes across engine versions. | panel inventory §4 + designs, judge-confirmed |

## 3. Conversion order

All conversion code lands in ONE new crate (`rust/brutex-lake` — pyo3 0.29,
`abi3-py312`, maturin, mixed layout: the exact `rust/fno-math` /
`rust/trading-core` pattern, so the CI wheel lane is a copy of an existing
one). Every PR ships: default-OFF env flag; the Python path as the LIVE
parity oracle; A/B byte gates (Rust-path bytes vs Python-path bytes,
same-process); frozen goldens where the bytes are stdlib-rendered; and a
per-process **burn-in self-check + autorevert** — the first N Rust-served
outputs are bit-compared against the Python oracle and a mismatch SHIPS THE
ORACLE VALUE and autoreverts the Rust path for the process. The in-repo
precedent for that pattern is the sweep-metrics Rust tail:
DEC-SWEEP-EXIT-INDEX-FASTPATH-001 + DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001
(MIGRATION.md #1531–#1532; ADR-0017 `0017-sweep-rust-metrics-default-on.md`).

### PR-1 — the polars-native writer render path (the seam: frame → parquet bytes)

* Scope: `parquet_writer`'s `_render_bytes` seam
  (`brutex/data/parquet_writer.py:1862-1869`) re-hosted on the crate's linked
  polars: same schema branching (incl. the conditional Greeks columns — the
  schema IS a byte contract), same `compression="zstd"` /
  `compression_level=ZSTD_LEVEL` / `row_group_size=ROW_GROUP_SIZE` /
  `statistics=False` knobs (`brutex/data/parquet_writer.py:158-161`), frames
  crossing via Arrow C-data FFI.
* Gate: default-OFF env flag; A/B byte-equality against the Python render on
  every covered fixture + a wheel-build/import-time byte-probe (render a
  canonical probe frame both ways; sha256 must match or the flag is
  force-latched OFF with one loud ERROR); burn-in + autorevert per the
  #1531/#1532 pattern. **Version guard (design requirement, per the spike):**
  the crate ships the wheel==tag==zstd ATOMIC pin, and whenever the installed
  Python wheel's polars version ≠ the crate's pinned `py-*` version the Rust
  flag REFUSES to engage with one loud WARN — fail-closed, never silently
  different bytes — with the CI corpus parity gate re-run on every bump. The
  port mirrors 1.42.1's ACTUAL render call chain — the streaming-engine
  `sink_parquet` route the spike identified — not the old eager
  ParquetWriter. `raw_hash` (sha256 of the rendered bytes,
  `brutex/data/parquet_writer.py:759`) is the natural per-write parity
  witness: any drift is caught before disk.
* Locks and error mapping stay on the Python facade in PR-1 (the facade takes
  the in-process lock + the flock, calls Rust only for the pure
  frame→bytes work) — so no concurrency law moves in the first PR.

### PR-2 — option_parquet_writer render

* Same apparatus on the all-DEFAULTS render — `_atomic_write_parquet`'s
  `atomic_write_via(dest, df.write_parquet)` call
  (`brutex/data/option_parquet_writer.py:947`); the byte-probe gate must
  additionally render a defaults-mode probe (F2 — default churn sensitivity).

### PR-3 — merge/dedup orchestration

* The writers' ts-keyed merge law, `_dedup_batch`, strictly-increasing
  assertion, and the vectorized 2dp snap expression — held to the EXISTING
  byte/value parity suites (the verbatim-legacy goldens + fuzz properties of
  DEC-LAKE-WRITE-VECTORIZE-001, MIGRATION.md #1023, now comparing Rust-path
  vs Python-path — a genuinely independent cross-implementation oracle).

### PR-4 — locks / atomic-replace

* The cross-process flock sidecar law (`partition_write_lock`,
  `brutex/data/partition_lock.py:344` — byte-identical
  `.locks/<sha256(dest)>.lock` path derivation is a unit-pinned contract),
  tmp→fsync→`os.replace` atomicity, and the `LakeWriteHalt` / `WriterError` /
  `PartitionLockTimeout` mapping. In-process lock acquisition REMAINS on the
  Python facade (Rust running beside Python callers must never fork the lock
  registry identity or order).

### Subsequent waves (same parity apparatus per PR)

* **Wave B — resampler family:** `tf_resampler` (+ `spot_resample`,
  `fno_resample` wrappers, then `fno_daily_resample` /
  `spot_daily_resample` residues) on the crate's polars API; value parity via
  the frozen bucket answer-key goldens + tiling properties; note their values
  transitively determine downstream parquet bytes, so these ride strictly
  AFTER the writer gates exist.
* **Wave C — stdlib-byte modules:** `lake_stats_manifest` (the safest port in
  the slice: the frozen `render_shard` golden, F5, becomes the cross-language
  byte pin asserted in cargo test AND pytest; serde_json/BTreeMap + a
  CPython-isoformat-exact formatter + rustix flock + guard-free atomic write;
  keep the one-`os.stat` per-write fast path in Python — a PyO3 crossing
  there is a likely net loss); `partition.py` as an internal crate helper
  (interval vocabulary single-sourced by INVERSION — exported from the crate,
  never twinned); `window_ledger` as a pyclass single-source (the
  DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 pattern, MIGRATION.md #1548 —
  Python demoted to a tests/fakes oracle; the CPython `isoformat()` merge-key
  strings are the trap, golden-pinned on both arms).
* **Wave D — policy/orchestration, LAST among the convertibles:**
  `timeframe_materialize`, `lake_heal`, `catalog_reconcile`,
  `lake_invariants` (the highest re-derivation risk in the slice — §7
  hazard 3: the whole ~170-fixture poison corpus must drive Python and Rust
  differentially before any verdict path flips), `catalog` (requires a SECOND
  exact dual-pin lockstep for libduckdb — the shared `catalog.duckdb` file is
  also opened by the dashboard through Python duckdb).
* **Oracle carve-out:** `brutex/feed/resampler.py` stays Python — it is
  production-dead (F6) and IS a test oracle, which the mandate itself
  exempts; its deletion-vs-retention remains a separate future DEC.

### LAST-MILE — `window_ledger_store` (the named escalation point)

pyarrow's parquet encoder is Arrow **C++** with no Rust build (F3), so no
Rust producer can byte-match the CURRENT bytes as written. The roadmap
records this module LAST-MILE with the route fixed in advance:

1. **Attempt the polars-native route first** — migrate the store's render
   onto the polars encoder family (both sides through the same crate-gated
   seam), bringing it under the SAME crate↔wheel lockstep parity apparatus as
   PR-1/PR-2. Note honestly: this changes the on-disk bytes going forward
   relative to today's pyarrow output (the module's own contract is the
   row-level fixed point, F3 — the byte-identity gate then holds Rust-path vs
   the NEW polars-rendered Python path, not vs historical pyarrow bytes).
2. **If bit-identity is infeasible without an exact-pin policy change**, this
   ONE module **escalates to the coordinator with the priced exact-pin DEC**
   (`DEC-LAKE-EXACT-PIN-001`: exact-pin Python polars + a lockfile + vendored
   polars at the matching `py-*` git tag + transitive-zstd pin + wheel-build
   byte-probe gate; costs per the maximalist pricing — ~5–15 min + hundreds
   of transitive deps per wheel build, a permanent two-sided upgrade tax, a
   standing single-byte-FAIL exposure). This is the named escalation point;
   it is not softened here.

## 4. Payoff honesty (per-surface, tagged)

The columnar/byte/SQL work already runs in native engines; the two per-row
Python hotspots that ever mattered were already vectorized in-place
(DEC-LAKE-WRITE-VECTORIZE-001, MIGRATION.md #1023 — MEASURED 1.78×,
byte-exact; DEC-PREBUILD-GIL-VECTORIZE-001, MIGRATION.md #1313 —
byte-identical lake, 124/124 SHA match). The conversion's expected deltas:

| Surface | Cost profile (evidence) | Rust delta | Tag |
|---|---|---|---|
| bars/options writers | zstd encode (already Rust) + fsync; the Python hotspot vectorized at 1.78× (#1023) | ~0 | MEASURED (hotspot); MODELED (residue) |
| prebuild / materialize | ~18 % Python-glue / ~14 % polars-native / ~68 % parquet-write+IO; real lever was write batching | ~0 | MEASURED (P10 kernel-track handoff profile; not a repo artifact) |
| window ledger (record model) | once-per-pull `prune_superseded` scan measured 0.48 s over a 1.2M-row ledger — in-docstring at `brutex/data/window_ledger.py:1197` | ~0 (once-per-pull) | MEASURED |
| window ledger (store) | saves are parquet-encode + fsync: ~3.94 s @ 841k rows (`_FLUSH_DUTY_FACTOR` rationale comment + constant, `brutex/data/window_ledger_store.py:1635-1641`); forensic 12.96 s `save_slow` over 68k rows (the in-body forensic comment, `brutex/data/window_ledger_store.py:881`) | ~0 (not Python-bound) | MEASURED |
| stats manifest | statx/`os.walk` syscall-bound; per-write fast path is ONE Python `os.stat` — a PyO3 crossing there is a likely net LOSS (mitigated: that probe stays Python) | ~0 to negative | MODELED |
| heal / invariants scan | anomaly-gated; clean files pay vectorized min/max only | ~0 | MODELED |
| catalog / reconcile | same libduckdb either way; sha256 C-accelerated + GIL-releasing; stat-cache eliminates warm-path hashing; a rayon parallel-hash helper on the COLD reconcile is the one real wall-clock candidate | ~0 (warm); cold win magnitude UNKNOWN (unprofiled) | MODELED / UNKNOWN |
| resamplers | polars expressions in polars' Rust core | ~0 by construction | MODELED |

## 5. Per-module roadmap table (17 modules)

| # | Module | Roadmap position | Parity strategy |
|---|--------|------------------|-----------------|
| 1 | `brutex/data/parquet_writer.py` | **PR-1** (render seam) → PR-3 (merge/dedup) → PR-4 (locks) | A/B byte gate vs the live Python render + build-time byte probe + burn-in/autorevert; `raw_hash` as per-write witness |
| 2 | `brutex/data/option_parquet_writer.py` | **PR-2** | Same, plus a defaults-mode probe (F2); heal-rail regression suites kept verbatim on the facade |
| 3 | `brutex/data/window_ledger_store.py` | **LAST-MILE** (named escalation point) | Polars-native route first; else escalate with the priced `DEC-LAKE-EXACT-PIN-001` (§3) |
| 4 | `brutex/data/lake_stats_manifest.py` | Wave C (first of the wave — the safest port) — **SHIPPED (render seam only) 2026-07-16, DEC-LAKE-RUST-STATS-MANIFEST-001 / MIGRATION.md #1914**: `render_shard` behind the default-OFF `BRUTEX_LAKE_RUST_STATS_MANIFEST` facade; flock + atomic write + loader/validator DELIBERATELY stay Python (locks = PR-4 territory — the recorded narrowing) | The frozen `render_shard` golden (F5) as the cross-language byte pin in cargo test AND pytest; isoformat formatter pinned both arms (delivered by construction — Python pre-formats, both arms consume the identical `.isoformat()` strings) |
| 5 | `brutex/data/partition.py` | Wave C (internal crate helper, never standalone) — **rider SHIPPED 2026-07-16 (#1914)**: crate-internal `option_partition_stem`/`validate_year_month` twins, cargo + pytest grid-pinned; Python `partition.py` byte-UNCHANGED; the interval-vocabulary inversion clause stays with the FULL port (not delivered) | Path-string equality over the 44-case behavioral suite; interval vocabulary single-sourced by inversion |
| 6 | `brutex/data/window_ledger.py` | Wave C | Pyclass single-source (Python demoted to tests/fakes oracle — the #1548 pattern); CPython-isoformat-exact merge keys golden-pinned; hypothesis differential suites |
| 7 | `brutex/data/lake_heal.py` | Wave D (after the writers it delegates to) | Policy over Rust-backed writer seams; exception-class isolation semantics differentially driven over the pinned suites (§7 hazard 3) |
| 8 | `brutex/data/lake_invariants.py` | Wave D (last convertible — highest verdict-flip risk) | Full ~170-fixture poison-corpus differential (Python vs Rust classification identical) BEFORE any flip; `engine_clean` verdict parity is the gate |
| 9 | `brutex/data/catalog.py` | Wave D | Requires a second exact dual-pin lockstep (libduckdb crate == Python wheel) — dashboard shares the same DB file; escalate that pin with evidence when reached |
| 10 | `brutex/data/catalog_reconcile.py` | Wave D | Detect-only; sha256 hex outputs trivially parity-testable; feeds `raw_hash` provenance — differential gate mandatory |
| 11 | `brutex/data/tf_resampler.py` | Wave B | Value parity on the frozen bucket answer-key goldens + tiling properties; Arrow C-data FFI zero-copy |
| 12 | `brutex/data/spot_resample.py` | Wave B (folds into #11's entry points) | Inherits #11 |
| 13 | `brutex/data/fno_resample.py` | Wave B (folds into #11's entry points) | Inherits #11 + the forming-bucket filter pinned |
| 14 | `brutex/data/fno_daily_resample.py` | Wave B (tail) | `Decimal(str(x))` shortest-round-trip repr edge golden-pinned; output objects stay Python-side |
| 15 | `brutex/data/spot_daily_resample.py` | Wave B (tail) | Orchestration keeps its injected seams — routes to Rust-backed callables; byte-identity rides the writer gates |
| 16 | `brutex/data/timeframe_materialize.py` | Wave D | Whole-parquet `read_bytes` parallel==sequential integration pins inherit the writer gates; derive-lock order + token-before-read TOCTOU re-proved per PR |
| 17 | `brutex/feed/resampler.py` | EXEMPT (oracle carve-out, F6) | Stays Python as the independent test oracle; deletion-vs-retention is a separate DEC |

## 6. Risk register (the panel's five flags, reframed as roadmap risks)

| # | Item | Risk to the roadmap | Mitigation in-plan |
|---|------|--------------------|--------------------|
| 1 | `parquet_writer` bytes | Parity holds ONLY under crate↔wheel encoder lockstep; a wheel bump past the crate silently moves the byte target | The spike (§1) PROVED achievability up front (FEASIBLE-BY-LOCKSTEP — bit-identical on the full 5-case corpus at `py-1.42.1`); build-time byte probe + version guard + LOUD-fail flag-stays-OFF latch (§7 hazard 1); the exact-pin DEC stays priced as the fallback |
| 2 | `option_parquet_writer` all-DEFAULTS render | Byte target moves across polars 1.x even without an encoder change | Defaults-mode probe in the PR-2 gate; convert strictly after PR-1 proves the template |
| 3 | `window_ledger_store` pyarrow C++ encoder | No Rust twin exists; bit-identity vs current bytes is infeasible by construction | LAST-MILE + the fixed two-step route (§3); escalation to the coordinator with `DEC-LAKE-EXACT-PIN-001` priced — the named escalation point |
| 4 | Twin maintenance | The mandate keeps Python as the live parity oracle ⇒ two implementations of every ported law exist under mutation gates on both sides — the drift class DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 (#1548) was paid to eliminate | Burn-in/autorevert makes drift LOUD-and-safe at runtime; Wave-C modules demote Python to tests/fakes (single-source) where the contract allows; cargo-mutants shards budgeted per PR (worst shard already 65 min — DEC-MUTATION-RUNTIME-003, MIGRATION.md #1597) |
| 5 | `feed/resampler.py` | Converting the test oracle would destroy the independent-implementation oracle | Exempt (F6); separate deletion-vs-retention DEC |

## 7. Standing hazards (live for the whole roadmap; replace the decline record's "reopening conditions")

1. **Crate↔wheel version lockstep.** The repo range-pins with no lockfile
   (F4). Every byte gate is therefore conditional: when the installed Python
   wheel moves past the crate's linked polars (or vice versa), the byte-probe
   gate must **fail LOUD and the env flag must stay OFF** — never a silent
   fallthrough, never a quietly-different byte. Upgrades become a coordinated
   two-sided change.
2. **zstd stack identity.** `compression="zstd", level=3` bytes depend on the
   zstd version bundled by the wheel build; the crate must resolve the
   identical `zstd`/`zstd-sys` versions (extracted from the polars repo's
   lock at the matching `py-*` tag). A zstd bump on either side = different
   compressed bytes = gate FAIL (loud, flag OFF).
3. **Exception-taxonomy re-derivation (Wave-D heal/scan PRs).** The
   skip-vs-crash-vs-finding classification + the `engine_clean` trust verdict
   are welded to polars-Python fault shapes (`pyo3_runtime.PanicException`
   from `to_list`, `OverflowError` from min/max, nested-dtype unhashability,
   the glob=False literal-read law) over 18 hostile-review rounds. A Rust
   reader's different fault taxonomy can silently flip the lake's trust
   verdict — the silent-wrongness class (Golden Rule #1). No Wave-D flip
   without the full poison-corpus differential green.
4. **Run-identity bytes downstream (Golden Rule #4), stated precisely.**
   Writer BYTE drift moves `raw_hash`
   (`brutex/data/parquet_writer.py:759`) and with it the catalog's FR-CAT-004
   lake-provenance rollup `Catalog.data_snapshot_hash`
   (`brutex/data/catalog.py:600-618`) plus every idempotence/reconcile pin;
   writer VALUE drift moves the stored bar fields and with them the
   RUN-IDENTITY digest `stage3_wire.data_snapshot_hash`
   (`brutex/engine/stage3_wire.py:172` — the full-content `bxsnap2` digest
   per DEC-SNAPSHOT-HASH-FULL-001, MIGRATION.md #1179, and CLAUDE.md §2
   Rule 4). The two hash chains are distinct (the catalog rollup is
   deliberately NOT wired into run identity) and BOTH sit downstream of
   writer output — which is why the burn-in gate ships the ORACLE value on
   any mismatch and autoreverts.
5. **FACADE NO-CYCLE LAW (appended 2026-07-17, caught on PR #1628 /
   DEC-DATA-RUST-PURELAW-001).** GOAL: the facade↔module import graph is
   ACYCLIC, mechanically pinned (an AST leaf-import test and/or
   fresh-interpreter import-order pins). TWO compliant shapes: **(a)**
   facade→module one-directional — any back-reference from the module is a
   type-import under `if TYPE_CHECKING:` only; **(b)** module→facade — the
   module imports the facade at module scope and the facade is a PURE
   INJECTION-ONLY LEAF that imports its routed module NOWHERE (not even
   under `TYPE_CHECKING` — that shape still trips CodeQL's module-level
   cyclic-import query): the module passes its oracle callables/constructor
   at call-time and any back-typing goes through a local structural
   Protocol, AST-pinned so the import can never return. Reference
   implementations: shape (b) = the `brutex/data/fno_symbol_rust.py` seam
   post-fix (`SymbolParts` structural Protocol + the injected
   `python_law`/`strike_law`/`build_parts` triple + the fresh-interpreter
   import-order pins and AST source pin in
   `tests/unit/test_fno_symbol_rust.py`); shape (b)-style precedent = the
   T35 brutex-core facades. Rationale: a module-scope import cycle passes
   local tests (import order happens to be favourable under pytest) but
   trips CodeQL and breaks import-order robustness in fresh interpreters.

## 8. Consequences

* This commit is docs-only: the roadmap + risk register are adopted; no
  crate, no CI lane, no flag exists yet. The §1 spike verdict
  (FEASIBLE-BY-LOCKSTEP) is recorded, so PR-1 is UNBLOCKED — it opens under
  the §3 gate set (default-OFF flag, version guard, byte probe, burn-in).
* Every conversion PR runs the full slice protocol (designers/judge →
  implement → hostile reviewers → refuters to two consecutive clean rounds →
  full local gates incl. 100 % line+branch on touched modules, golden +
  property + bench suites, P-06 + suffix-independence UNMODIFIED).
* Kernel-track ownership boundaries (sweep cores, `arrow_table_to_bars`, the
  frozen `rust/trading-core` per DEC-SHARED-CORE-001, MIGRATION.md #441) are
  untouched by this roadmap and remain owned elsewhere.

## 9. PR-1 shipped surface (2026-07-11 — appended when PR-1 landed on this branch, same DEC)

The §3 PR-1 gate set is IMPLEMENTED (full record: the PR-1 segment of
MIGRATION.md #1891): the standalone `rust/brutex-lake/` crate (polars git
deps REV-pinned at the commit the movable `py-1.42.1` tag pointed at —
`PINNED_POLARS_GIT_REV`, no `tag =` line, cargo-tested by
`manifest_rev_matches_pinned_rev`; restated object_store patch, zstd trio
via the committed `Cargo.lock`, lockstep pins cargo-tested, panics caught
at the FFI boundary), the `brutex/data/lake_rust_render.py` facade + the ONE
`_render_bytes` seam (`BRUTEX_LAKE_RUST_RENDER` default OFF; import guard /
version guard / 16-render burn-in with ship-the-oracle autorevert exactly
as §3 specifies; `MemoryError` → `lake_write_guard` → `LakeWriteHalt`
unchanged), the 5-case corpus + whole-write-path byte-parity suites
(EXECUTED locally against the built wheel — all cases sha256+raw-bytes
equal, the spike's exact digests), the dedicated non-required
`.github/workflows/lake-rust-parity.yml` lane, and
`docs/runbooks/lake-rust-render-lockstep.md` (the bump ritual + the
coordinator escalation). Deviation from §3's sketch, recorded: the frame
crosses as UNCOMPRESSED ARROW IPC bytes (the spike's proven handoff), not
Arrow C-data FFI — same zero-drift byte surface, simpler boundary; the
build-time byte-probe is subsumed by the burn-in (the first engaged renders
ARE the probe, per-process, against the live oracle). MEASURED per-render
engaged overhead: ~+6 ms (+47%) on the 100k-row encode tail vs the Python
`write_parquet` tail (~3% of the full `_render_bytes` wall) — the expected
≈0-payoff banner holds as a small measured slowdown. Locks + error mapping
did not move (the PR-1 boundary). PR-2..PR-4 and the waves remain future
work under the same apparatus.

**ADVERSARIAL-REVIEW FIX ROUND folded (2026-07-11, same branch, pre-merge —
5 hostile lenses; full per-item record in the MIGRATION.md #1891 PR-1
segment):** the facade is fail-closed against a FOREIGN/BROKEN crate (API
surface verified at import; a raising metadata pyfunction latches
`crate_metadata_error`; `AttributeError` joins the degrade set; a
`BaseException`/`PanicException` escaping the crate boundary latches
`crate_panic` and serves the oracle — KeyboardInterrupt/SystemExit/
MemoryError re-raise bare); the crate constructs its result INSIDE a panic
guard (a `PyBytes` allocation panic maps to `RuntimeError`, never a
`PanicException`) and releases the GIL for the render (`Python::detach` —
the wheel's own posture); the polars git deps are REV-pinned (tags are
movable; `PINNED_POLARS_GIT_REV`, manifest + committed `Cargo.lock` both
cargo-tested, every CI cargo step `--locked`, maturin `locked = true`); the
parity lane force-aligns the installed wheel to the crate pin so a new
upstream polars release can never turn the lane green-but-vacuous (the
drift check is now FATAL in-lane; `brutex/data/models.py` joins the path
triggers); the burn-in gains a SCHEMA-SHAPE gate (every distinct frame
shape byte-compared at least once before crate bytes ship for it — the
count budget alone would have licensed a first-ever Greeks shape after 16
spot renders) and the corpus gains the all-null-overlay case (all 6 cases
byte-MATCH on the rebuilt crate; the original 5 digests unchanged). OOM
honesty is SCOPED PRECISELY: a Python-surfaced `MemoryError` maps to
`LakeWriteHalt` as on the pure-Python path; a Rust-encoder-INTERNAL
allocation failure aborts/panics on BOTH the wheel and the crate — the
seam neither narrows nor widens that envelope. FLIP PRECONDITION: byte
identity is enforced only by the NON-REQUIRED parity lane — the default-ON
flip DEC must make it required or cite a fresh green run.

## 10. PR-2 shipped surface (2026-07-11 — appended when PR-2 landed on its branch; steward-assigned its own ledger row `DEC-RUST-DATALAKE-IO-SLICE8-PR2-001`, MIGRATION.md #1771)

The §3 PR-2 gate set is IMPLEMENTED on the stacked PR-2 branch (full record:
MIGRATION.md #1771 — DEC-RUST-DATALAKE-IO-SLICE8-PR2-001, the PR-2 row under
this roadmap DEC): the option
writer's ALL-DEFAULTS render seam — the ENTIRE `option_parquet_writer.py`
diff confined to `_atomic_write_parquet`'s body + one new module-level
oracle helper `_defaults_python_render` (flag-OFF is the LITERAL legacy
`atomic_write_via(dest, df.write_parquet)`, mechanism-verbatim,
never-routes spy-pinned; `lake_heal.py` + `atomic_io.py` ship ZERO diff) —
routed through the SAME `lake_rust_render` facade under a NEW render-PROFILE
registry (`PROFILE_BARS` default = PR-1 back-compat;
`PROFILE_OPTIONS_DEFAULTS` = the zero-kwarg `df.write_parquet(path)` byte
shape: statistics min/max/null_count ON + distinct OFF, `Zstd(None)`,
`row_group_size=None` → the streaming sink's 122,880-row split). ONE flag,
ONE shared latch (fail-closed across BOTH profiles), per-PROFILE 16-render
burn-in budgets, profile-prefixed shape tokens with a 64-entry seen-set cap
(the honest correction of PR-1's "two writer shapes" bound — the heal path
re-renders arbitrary stored schemas; saturation degrades to compare-always),
`_CRATE_API` grown to 4 all-or-nothing (a stale PR-1 crate latches BOTH
surfaces — atomic pin), `ipc_handoff_bytes` hoisted COMMON. Crate side: ONE
shared parameterized render core (`render_parquet_with_options_impl` — the
pre-refactor bars tests run UNMODIFIED as the frozen-behavior gate) + the
thin `render_parquet_defaults_from_ipc` pyfunction over the factored FFI
shell, with the defaults-mapping / 122,880-split / `dyn_sink_equals_
file_sink_defaults` (the P2 standing pin — judge-probed PASS 7/7 pre-design)
cargo pins. Gates EXECUTED: the 12-case options corpus (6 probe-origin
cases — the probe's seventh, the 130k mid-boundary multi-RG case, is
superseded by the exact-boundary pair, its multi-RG coverage kept by the
retained 300k case — + 6 new: all-null-nullables / single-side / the exact
122,880 + 122,881 boundary pair / long-string truncation sentinel / NaN+inf
heal shape; real-writer provenance) — 12/12 sha256 + raw-bytes MATCH through the REAL seam
steady-state, with the per-profile vacuous-green guard + the three
whole-path ON/OFF layers (write · heal_partition · merge_partition_frame ·
the end-to-end `heal_findings` batch twin) + statistics diagnostic pins;
the P1 standing pin (BytesIO ≡ path target — judge-probed 7/7, now pinned
crate-free in main CI); rails regressions R1–R14. EMPIRICAL CORRECTION
recorded: the probe report's "`binary_statistics_truncate_length=None` =
untruncated" reading is WRONG at the stats layer — the pinned rev resolves
it to a 64-byte truncate with max-increment (wheel and crate share it —
parity unaffected; the o11 sentinel pins the OBSERVED policy as data).
Deviations from §3's sketch: none new (the IPC handoff + burn-in-as-probe
deviations carry over from PR-1). Locks + error mapping still did not move
(PR-3/PR-4 territory); PR-3..PR-4 and the waves remain future work under
the same apparatus. Payoff ≈ 0 banner holds — the options render is
heal-/option-pull-driven O(month-file) glue; no speed claim.

## 11. PR-4 shipped surface (2026-07-16 — appended when PR-4 landed; DEC-LAKE-RUST-LOCKS-ATOMIC-001, MIGRATION.md #1912)

The §3 PR-4 gate set is IMPLEMENTED (full record: MIGRATION.md #1912):
`rust/brutex-lake/src/locks.rs` ships `lock_try_exclusive` /
`lock_release` / `atomic_commit_replace` behind the NEW default-OFF
`brutex/data/lake_rust_locks.py` facade (`BRUTEX_LAKE_RUST_LOCKS`), with
the §"PR-4 — locks / atomic-replace" laws held exactly: the
`.locks/<sha256(dest)>.lock` path derivation stays Python-owned (the
crate receives ONLY the fd — the unit-pinned byte-identical contract
cannot drift through Rust), tmp→fsync→`os.replace` atomicity is the
order-exact `commit_replace_impl` twin, the `LakeWriteHalt` /
`PartitionLockTimeout` mapping stays entirely on the Python facade (the
crate raises real `OSError(errno)` / `RuntimeError`-on-panic only), and
in-process lock acquisition REMAINS on the Python facade
(`BoundedLockRegistry` byte-untouched — the registry lock stays OUTER).
Deviations from the roadmap sketch, recorded in the DEC: (a) the routing
is attempt/commit-GRAINED, not whole-acquire — a Rust-side retry loop
would drop the per-retry watchdog liveness beats
(DEC-PULL-CONTENTION-ANTISTALL-001 GAP #15); (b) temp-file creation stays
`tempfile.mkstemp` on both arms (the `.brutex-write-` prefix +
mkstemp-ENOSPC guard laws hold byte-identically); (c) no byte burn-in /
no version guard on this facade — locks are polars-free and have no
rendered bytes; the parity contract is BEHAVIORAL (the executed
mixed-arm mutual-exclusion + two-subprocess belts + errno-parity
suites stand in). One new direct dep: `libc = "0.2"` (flock only —
already transitive in the committed `Cargo.lock`; rustix stays reserved
for Wave C per §3). Flag OFF (the default) is byte- and
call-graph-identical, bomb-spy-pinned; the default-ON flip needs its own
DEC.

## 12. Wave-D shipped surface (2026-07-19 — appended when the Wave-D lane completed; DEC-RUST-LAKE-WAVED-001, MIGRATION.md #1979)

The four Wave-D convertible rows are **DONE — and past the roadmap's own
end state**: `timeframe_materialize` (row 16), `lake_heal` (row 7),
`catalog_reconcile` (row 10) and `lake_invariants` (row 8) moved their
cores to `rust/brutex-lake/src/waved/` on `claude/rust-lake-waved-001`,
and — per the coordinator/operator greenfield ruling for this lane — the
compressed cycle ran convert → differential → default-ON → **Python-core
DELETE in-lane**, superseding the live-parity-oracle pattern for these
four modules: `brutex_lake` is the ONE production arm, the interim
`BRUTEX_LAKE_WAVED_RUST` flag is RETIRED (any set value refuses loudly),
and a missing/stale wheel is the typed fail-loud
`LakeRustWavedUnavailableError` (ci.yml's test/golden/chaos-race/run-smoke
jobs build + install the wheel). The §7 hazard-3 gate was satisfied as
written: the whole poison corpus drove Python and Rust differentially —
27/27 cases ENGAGED + IDENTICAL in both directions before any verdict
path flipped (the #1916 harness's own `ScannerImpl` seam, harness
byte-unmodified), the heal differential held 40/40 four-strength
twin-lake parity, and the frozen goldens passed default-ON with fixtures
byte-untouched. Out-of-envelope shapes carry ruled per-class
ISOLATED-error postures (heal per-finding · reconcile GAP-#9 per-file
skip · scan loud counted skip · read typed ResampleInputError); the W-6b
closing slice widened the bars-tree dup heal with the frame-level
verbatim PRESERVE arm (17-column Greeks-overlay + finite big-price dups
heal keep-first). `catalog` (row 9 — the libduckdb dual-pin escalation)
and the LAST-MILE `window_ledger_store` remain OPEN as recorded; the
honesty banner held — the conversion was for uniformity, not speed.
