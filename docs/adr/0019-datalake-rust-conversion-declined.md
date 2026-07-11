# ADR-0019 — Rust conversion of the data-lake I/O layer: assessed and DECLINED (all 17 modules KEEP-PYTHON)

**Decision ID:** `DEC-RUST-DATALAKE-IO-DECLINE-001` (MIGRATION.md entry #1726)
**Date:** 2026-07-11
**Status:** **ACCEPTED** — as an assessment record. DOCS-ONLY: zero code ships,
zero modules convert; the five §6 operator flags are SURFACED decision points
(each carries the panel's recommendation), none is executed here, and the one
priced alternative (`DEC-LAKE-EXACT-PIN-001`) is a NAMED, NOT-FILED option.
**Author:** the slice-8 assessment session (6 readers → 3-designer panel
(maximalist / conservative / byte-parity-oracle) → judge), coordinator-directed.
**Umbrella:** the Rust-first conversion program, `DEC-RUST-FIRST-MANDATE-001`
(ledger #1713 — riding the Slice-1 PR, IN FLIGHT and NOT on main at this HEAD;
cited as the program anchor, never as landed).
**Scope:** the P10 "data-lake IO wrappers" slice — 16 `brutex/data/` modules +
`brutex/feed/resampler.py` (17 total, enumerated in §4). ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate/lake-schema change; F&O lake stays
REFERENCE_ONLY.

---

## 0. Abstract (plain language, up front)

**The lake's parquet bytes are ALREADY produced by Rust.** The spot/F&O bars
writer renders every partition through polars' native Rust zstd parquet encoder
(`use_pyarrow=False` — pyarrow appears only as a read-only footer probe), and
the resamplers' columnar math executes inside polars' own Rust core. Rewriting
the byte-producer by hand would BREAK the exact-bytes guarantee that
run-reproducibility depends on — an independent Rust encoder is *guaranteed*
to emit structurally different bytes, and re-hosting the same engine crate
re-runs the identical Rust code for zero speed. That makes the product WORSE,
not more-Rust. This layer's honest classification is **"already Rust
underneath, must stay as-called"**, not a skip.

Verbatim-faithful to the binding P10 constraints: **expected payoff ≈ 0 — this
layer is IO-bound glue over native engines; Rust does not change O() for
fsync / `os.replace` / footer scans.** Every payoff figure in this record
carries a MEASURED / MODELED / UNKNOWN tag (§5).

---

## 1. Context

* The whole-product Rust-first mandate assigned slice 8 = the data-lake I/O
  layer, pre-flagged low-value-do-last with the framing quoted above, and with
  hard conversion gates: **bit-identical stored bytes vs the Python writers**
  (rendered-parquet byte-identity; partition-lock/flock semantics;
  `LakeWriteHalt` mapping), default-OFF env-flag landing, and the Python path
  retained as the parity oracle.
* The precedent already on record: the prebuild was profiled at ~18 %
  Python-glue / ~14 % polars-native / ~68 % parquet-write+IO and DROPPED from
  the Rust queue — the real lever was write batching, a data-layer fix
  (MEASURED — the P10 kernel-track handoff profile; recorded in the slice-8
  constraints handoff, not as a repo artifact).
* Panel protocol: 6 reader analyses → 3 designs (maximalist / conservative /
  parity-oracle) → judge ruling. The judge re-verified every load-bearing fact
  against source at HEAD `adab6aa2`; this ADR re-verified each carried citation
  again before recording it.

## 2. Decision

**All 17 scoped modules: KEEP-PYTHON. The genuinely defensible conversion
subset is EMPTY. Slice 8 closes as assessed-and-declined; the deliverable is
this record.**

This is the 2-of-3 panel position (conservative + parity-oracle) and is ALSO
the maximalist design's own position once its self-stated caveats get honest
weight: its Wave 1 is offered at "zero-to-negligible speed … value = mandate
conformance", its Wave 2 only behind a policy-reversal DEC it itself calls
"the fragile core", and it refuses the two hardest modules outright. Under
Golden Rule #10 (honest limits), mandate conformance with an empty benefit
ledger does not clear the bar to ship code — it is a FLAG to the operator
(§6), not a verdict.

## 3. Facts the decision stands on (each re-verified at this HEAD)

| # | Fact | Verified at |
|---|------|-------------|
| F1 | The bars writer renders via **polars' Rust encoder**, NOT pyarrow: `df.write_parquet(BytesIO, compression="zstd", compression_level=ZSTD_LEVEL, row_group_size=ROW_GROUP_SIZE, use_pyarrow=False, statistics=False)`; the sha256 of the buffer BEFORE disk is the recorded `raw_hash`. (Corrects an earlier inventory claim of a "pyarrow deterministic render" — pyarrow there is a read-only footer probe.) | `brutex/data/parquet_writer.py:1862-1869` (`use_pyarrow`, in `_render_bytes`); constants `ROW_GROUP_SIZE`/`ZSTD_LEVEL` at `brutex/data/parquet_writer.py:158-161`; `raw_hash` at `brutex/data/parquet_writer.py:759` |
| F2 | The expired-options writer renders via `atomic_write_via(dest, df.write_parquet)` — polars with ALL DEFAULTS (no compression args), so its byte target can move across polars 1.x even without an encoder change. | `brutex/data/option_parquet_writer.py:947` (in `_atomic_write_parquet`) |
| F3 | The window-ledger store writes `pq.write_table(table, tmp)` (pyarrow, all defaults) at BOTH sites, and its own module docstring EXPLICITLY disclaims byte parity — the `to_rows` row-level fixed point is the contract: "Parquet *file bytes* may differ across writers via embedded library metadata; the ROW CONTENT is the reproducible contract, and every test asserts the row fixed point, not the byte stream." | `brutex/data/window_ledger_store.py:714,846` (`pq.write_table`); docstring at `brutex/data/window_ledger_store.py:16-18` |
| F4 | The engine pins are RANGES with NO lockfile: `polars>=1.0,<2.0`, `duckdb>=1.0,<2.0`, `pyarrow>=16,<25`; no `*.lock` / requirements / constraints file exists in the repo root. | `pyproject.toml:48-50` |
| F5 | The slice's ONE frozen cross-language byte oracle exists: the golden pins `render_shard`'s exact byte string (`_EXPECTED_BARS_SHARD`); the shard JSON law is stdlib compact sorted-key `json.dumps` with no floats in the payload. | `tests/golden/test_lake_stats_manifest_golden.py:66` (`render_shard`); `brutex/data/lake_stats_manifest.py:781` (`json.dumps` render) and `brutex/data/lake_stats_manifest.py:483` (the sha256 shard-path token) |
| F6 | `brutex/feed/resampler.py` is production-DEAD and AST-drift-pinned: `tests/unit/test_feed_no_epoch_resampler_in_production.py` proves no production module imports it (DEC-SUSPICION-SWEEP-FIXES-001 G3, MIGRATION.md #1492). | test file present at this HEAD |
| F7 | Every parquet byte pin in the slice is a same-process, same-engine-version A/B against the LIVE installed engine; there are no frozen on-disk parquet SHA fixtures anywhere in the slice. | panel inventory §4 + designs, judge-confirmed |

Consequence of F1–F4 + F7 (all three designers converge; adopted as the
slice's governing structural fact): **the "bit-identical stored bytes" hard
rule is only coherent as a same-environment A/B contract** — the Python
oracle's own bytes legally move across engine upgrades inside the range pins.
For any parquet-rendering surface there are exactly three Rust routes, and
each fails: (a) PyO3 callback into the installed Python engine —
byte-identical and not a conversion; (b) exact engine-crate lockstep (vendored
polars at the matching `py-*` git tag + the wheel's transitive zstd pins) —
requires a policy-reversal DEC, a permanent two-sided upgrade tax, and a
standing single-byte-FAIL hazard, all to re-host the SAME Rust encoder for
zero speed; (c) an independent encoder (arrow-rs/parquet-rs) — guaranteed
structurally different bytes (created_by, encodings, footer, page layout) =
instant FAIL. For the pyarrow surface (F3), route (b) does not even exist:
pyarrow's parquet encoder is Arrow **C++** with no Rust build.

## 4. Per-module rulings (17 modules)

| # | Module | Verdict | Reason (condensed) | Flag |
|---|--------|---------|--------------------|------|
| 1 | `brutex/data/parquet_writer.py` | KEEP-PYTHON | PARITY-INFEASIBLE robustly (F1/F4/F7 — bytes are already polars' Rust zstd encoder; a "conversion" can only re-host the same engine); encoder+fsync-bound, the one Python hotspot already vectorized in place (§5); `raw_hash` sits directly upstream of results-integrity surfaces (§5.4) | §6-1 |
| 2 | `brutex/data/option_parquet_writer.py` | KEEP-PYTHON | Same encoder wall, strictly worse: all-DEFAULTS render (F2) so the byte target moves across polars 1.x; heal-driven anomaly cadence — zero speed by construction; six review rounds of symlink/containment/glob=False rails would be re-proved for nothing | §6-2 |
| 3 | `brutex/data/window_ledger_store.py` | KEEP-PYTHON | PARITY-INFEASIBLE outright (F3 — pyarrow C++ encoder, no Rust twin; pyarrow 16–25 range = nine majors of legal Python-side byte drift; the module's own docstring disclaims byte parity); live flock/delta-ordering/two-fold-rule interop with concurrent Python peers = the slice's highest silent-data-loss porting risk for zero gain | §6-3 |
| 4 | `brutex/data/lake_stats_manifest.py` | KEEP-PYTHON | The ONE module where independent Rust byte parity is genuinely provable (F5); but syscall-bound (statx/walk — Rust does not speed up syscalls), the per-write fast path is ONE Python `os.stat` where a PyO3 crossing is a likely net LOSS, and a port discards a zero-survivor mutation-hardened (~460 mutants) 18-clause fail-closed validation ladder + 203 unit tests to re-prove them | §6-4 |
| 5 | `brutex/data/partition.py` | KEEP-PYTHON | Trivially parity-feasible path strings, and the clearest NEGATIVE-value standalone port in the slice: nanosecond work behind a PyO3 boundary whose marshalling exceeds the function body; a Rust copy forks the `LAKE_INTERVAL_LABELS` single source (§11 COMMON) + the `PartitionError` taxonomy; rides along ONLY if §6-4 is ever forced | — |
| 6 | `brutex/data/window_ledger.py` | KEEP-PYTHON | Parity-moot (zero bytes) with a load-bearing rider: its CPython `datetime.isoformat()` row strings are lexicographic merge KEYS downstream; zero speed motive (§5); biggest Wave-1 blast radius (65 repo test files); the mandate keeps Python alive as the oracle ⇒ a permanent behavioural twin — the drift hazard the repo paid to eliminate (DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001, MIGRATION.md #1548) | — |
| 7 | `brutex/data/lake_heal.py` | KEEP-PYTHON | ~95 % policy whose every partition mutation delegates to the two writers (inherits their parity infeasibility, multiplied); per-finding isolation is driven by Python exception CLASSES — including catching polars' own `pyo3_runtime.PanicException` — welded in over 18 hostile-review rounds; anomaly cadence, no compute to win | — |
| 8 | `brutex/data/lake_invariants.py` | KEEP-PYTHON | Writes zero bytes (parity vacuous) and is the panel's clearest actively-dangerous port: the skip-vs-crash-vs-finding classification + the `engine_clean` trust verdict are welded to polars-Python fault shapes over a ~170-fixture poison corpus; a Rust reader's different fault taxonomy silently flips the lake's trust verdict — the silent-wrongness class, worst under Golden Rule #1 | — |
| 9 | `brutex/data/catalog.py` | KEEP-PYTHON | DuckDB internal file — never byte-compared, but the dual-binding trap disqualifies: duckdb-rs bundles its own libduckdb, the Python wheel another, DuckDB storage formats are not freely cross-version compatible, and the SAME catalog file is opened by the dashboard through Python; same engine either way = zero speed; would need a SECOND exact dual-pin lockstep DEC | — |
| 10 | `brutex/data/catalog_reconcile.py` | KEEP-PYTHON | Detect-only; hosts the slice's only genuine wall-clock candidate (parallel sha256 on a COLD multi-GB reconcile) but hashlib releases the GIL and the bench-pinned stat-cache already eliminated hashing on warm launches; feeds `raw_hash` provenance — the worst place in the slice to introduce drift | — |
| 11 | `brutex/data/tf_resampler.py` | KEEP-PYTHON | ~90 % polars expressions already executing in polars' own Rust core; a port re-hosts the identical engine (zero speed, single-digit ms/TF) and is value-pinnable only under the same lockstep DEC as the writers, while its values transitively determine downstream parquet bytes | — |
| 12 | `brutex/data/spot_resample.py` | KEEP-PYTHON | 64-LOC label-map wrapper; converts nothing by itself; inherits #11 | — |
| 13 | `brutex/data/fno_resample.py` | KEEP-PYTHON | 139-LOC wrapper + one forming-bucket filter expression; inherits #11 | — |
| 14 | `brutex/data/fno_daily_resample.py` | KEEP-PYTHON | Aggregation delegated; its own per-row loop is O(complete sessions) ≈ hundreds of rows whose OUTPUT is Python CandleBar/Decimal objects for the Python sink — a Rust port spends its time constructing Python objects through PyO3 and must match CPython shortest-round-trip float repr at the `Decimal(str(x))` edge; negative value at any real scale | — |
| 15 | `brutex/data/spot_daily_resample.py` | KEEP-PYTHON | Orchestration with injected I/O seams; all bytes/locks/2dp-snap live in the delegated writer; the hot loop is already batch-vectorized (DEC-PREBUILD-GIL-VECTORIZE-001, MIGRATION.md #1313) and the residue is irreducible Python object construction; the seam design means a hypothetical future Rust writer needs ZERO change here | — |
| 16 | `brutex/data/timeframe_materialize.py` | KEEP-PYTHON | Writes no bytes itself; ≤20 dispatches + path/syscall loops around delegated native-engine work; its whole-parquet `read_bytes` integration pins (parallel==sequential) fail on a single differing writer byte — inheriting the writers' verdicts for any deeper port | — |
| 17 | `brutex/feed/resampler.py` | KEEP-PYTHON | Production-DEAD (F6) and serving as a stdlib-pure Python test ORACLE — the mandate's own carve-out exempts oracles; being trivially portable is exactly why it must not be ported | §6-5 |

Also declined WITH the crate they would have lived in: the maximalist's two
"crumbs" (a rayon-parallel `lake_content_token` walk; a `hash_files_parallel`
helper for the cold reconcile). Both accelerate rare paths whose compute is
already C-accelerated and GIL-releasing; a Python `ThreadPoolExecutor` over
`hashlib` would capture most of the win with zero new language surface if a
profile ever demands it. Standing up a `rust/brutex-lake` crate + CI wheel
lane + cargo-mutants shard time (the weekly gate's worst shard already runs
65 min — DEC-MUTATION-RUNTIME-003, MIGRATION.md #1597) to host two helpers is
the tail wagging the dog.

## 5. The four grounds

### 5.1 The byte-parity wall closes the headline targets by construction

Every parquet-rendering byte surface is polars-Rust (F1, F2) or pyarrow-C++
(F3). Under range pins with no lockfile (F4) and an all-same-process-A/B pin
architecture (F7), no independent Rust producer can be robustly
bit-identical; the pyarrow surface has no Rust twin at all and its own
docstring disclaims byte parity. The three routes and their failures are
enumerated under §3; the only conceivable route for the polars surfaces —
exact-pin lockstep — is priced and flagged in §6-1, with refusal recommended.

### 5.2 There is no Python hot loop to win (the honest-benefit table)

The columnar/byte/SQL work already runs in native engines. The two per-row
Python hotspots that ever mattered in this layer were already harvested
IN-PLACE by vectorizing into the engine, with cited, verified DECs:

* the writer's per-row Decimal 2dp-snap reconstruction —
  **DEC-LAKE-WRITE-VECTORIZE-001 (MIGRATION.md #1023)**, byte-exact polars
  expression, **MEASURED 1.78×** (60.3 ms → 33.9 ms on a realistic 7,500-row
  month partition);
* the prebuild's frame→`CandleBar` conversion loops —
  **DEC-PREBUILD-GIL-VECTORIZE-001 (MIGRATION.md #1313)**, byte-identical lake
  output (124/124 parquet SHA match), lab-measured GIL-held-share win.

The residue, per surface, with payoff tags:

| Surface | Cost profile (evidence) | Rust delta | Tag |
|---|---|---|---|
| bars/options writers | zstd encode (already Rust) + fsync; the Python hotspot vectorized at 1.78× (#1023) | ~0 | MEASURED (hotspot); MODELED (residue) |
| prebuild / materialize | ~18 % Python-glue / ~14 % polars-native / ~68 % parquet-write+IO; dropped from the Rust queue, lever was write batching | ~0 | MEASURED (P10 kernel-track handoff profile; not a repo artifact) |
| window ledger (record model) | once-per-pull `prune_superseded` scan measured 0.48 s over a 1.2M-row ledger — in-docstring at `brutex/data/window_ledger.py:1154` | ~0 (once-per-pull) | MEASURED |
| window ledger (store) | saves are parquet-encode + fsync: ~3.94 s @ 841k rows (`_FLUSH_DUTY_FACTOR` rationale, `brutex/data/window_ledger_store.py:1553-1557`); forensic 12.96 s `save_slow` over 68k rows on a degraded volume (`brutex/data/window_ledger_store.py:800`) | ~0 (not Python-bound) | MEASURED |
| stats manifest | statx/`os.walk` syscall-bound; per-write fast path is ONE Python `os.stat` — a PyO3 crossing there is a likely net LOSS | ~0 to negative | MODELED |
| heal / invariants scan | anomaly-gated; clean files pay vectorized min/max only | ~0 | MODELED |
| catalog / reconcile | same libduckdb either way; sha256 is C-accelerated + GIL-releasing, stat-cache eliminates warm-path hashing | ~0 (warm); cold-reconcile parallel-hash win real but rare-path | MODELED; magnitude UNKNOWN (unprofiled) |
| resamplers | polars expressions in polars' Rust core | ~0 by construction | MODELED |

Nothing in the table clears "what does Rust buy here". The coordinator's
IO-bound-glue framing is confirmed from both the bytes side and the profile
side.

### 5.3 The parity-feasible remainder fails the honest-benefit test

`lake_stats_manifest` + `partition` (+ `window_ledger`, parity-moot) are
convertible with provable parity — and would ship zero-to-negative value
while discarding mutation-hardened, 100 %-line+branch-covered Python only to
re-prove it in a second language. Because the slice mandate keeps the Python
path alive as the parity oracle, a behavioural twin ALWAYS exists — exactly
the two-implementations-of-one-law drift hazard the repo already paid to
eliminate (DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001, MIGRATION.md #1548),
now under zero-survivor mutation gates on BOTH sides.

### 5.4 The risk side is uniquely heavy here

* **Exception-taxonomy welding (silent verdict flips).** 18 hostile-review
  rounds welded the heal/scan trust chain to polars-Python fault shapes
  (`pyo3_runtime.PanicException` from `to_list`, `OverflowError` from
  min/max on out-of-range timestamps, nested-dtype unhashability, the
  glob=False literal-read law). A Rust reader has a different fault taxonomy
  over the poison-fixture corpus; any residual mismatch silently flips
  `files_skipped` / `engine_clean` — the lake's trust verdict.
* **Live cross-process interop.** fcntl flock sidecars
  (`partition_write_lock`, `brutex/data/partition_lock.py:344`), in-process
  lock-registry ordering, stats-hooks-under-held-locks TOCTOU law, under-lock
  delta-filename minting, and the `LakeWriteHalt` / `WriterError` /
  `PartitionLockTimeout` splits must interop bug-for-bug with concurrent
  Python processes (dashboard + CLI) on one lake.
* **Results-integrity adjacency (Golden Rule #4), stated precisely.** Writer
  BYTE drift moves `raw_hash` (the sha256 of the rendered partition bytes,
  `brutex/data/parquet_writer.py:759`) and with it the catalog's FR-CAT-004
  lake-provenance rollup `Catalog.data_snapshot_hash`
  (`brutex/data/catalog.py:600-618`) plus every idempotence/reconcile pin.
  Writer VALUE drift (a wrong snap/merge) moves the stored bar fields and
  with them the RUN-IDENTITY snapshot digest — `stage3_wire.data_snapshot_hash`
  (`brutex/engine/stage3_wire.py:172`, the full-content `bxsnap2` digest of
  every loaded bar per DEC-SNAPSHOT-HASH-FULL-001, MIGRATION.md #1179, and
  CLAUDE.md §2 Rule 4). Either way, a parity miss in a ported writer
  escalates from a test failure to a results-integrity incident. (This
  corrects the panel's shorthand "raw_hash → data_snapshot_hash → run
  identity": the catalog rollup is deliberately NOT wired into run identity —
  the two hash chains are distinct, and BOTH sit downstream of writer output.)

## 6. Operator flag register (explicit decision points — surfaced, none executed)

| # | Module(s) | Flag | Recommendation |
|---|-----------|------|----------------|
| 1 | `parquet_writer.py` | **`DEC-LAKE-EXACT-PIN-001` — a NAMED, NOT-FILED option**: exact-pin Python polars + a lockfile, vendor the polars workspace at the matching `py-*` git tag, pin the wheel's transitive zstd stack, add a wheel-build byte-probe gate. It is the ONLY route to a Rust re-host of the polars byte surfaces; it reverses the deliberate range-pin/no-lockfile policy (F4), adds ~5–15 min + hundreds of transitive deps to every wheel build, and creates a permanent two-sided upgrade tax + standing single-byte-FAIL exposure — for ZERO speed | **REFUSE the DEC** |
| 2 | `option_parquet_writer.py` | Same DEC family; strictly worse (all-defaults render, F2) | REFUSE; never convert |
| 3 | `window_ledger_store.py` | PARITY-INFEASIBLE outright (F3). A coordinator ruling adopting the module's own documented row-content contract could technically admit a port | **Do NOT seek the ruling** — highest silent-data-loss risk in the slice for zero gain |
| 4 | `lake_stats_manifest.py` | The one genuinely parity-feasible module: the frozen `render_shard` byte golden (F5) is a ready cross-language byte pin. IF the whole-product mandate is ever enforced for conformance's sake despite ~nil benefit, port THIS module first, per the maximalist's plan (serde_json/BTreeMap + a CPython-isoformat-exact formatter + rustix flock + guard-free atomic write), with `partition.py` riding along as an internal crate helper | Keep Python; if forced, this is first |
| 5 | `feed/resampler.py` | Production-dead oracle (F6) — the correct future action is a separate **deletion-vs-retention DEC**, out of conversion scope | File the deletion DEC separately; exempt from conversion |

## 7. Reopening conditions (none held today)

1. A profiled, reproducible Python-loop bottleneck in this layer that cannot
   be vectorized into the existing engines (the prior two could and were —
   #1023, #1313).
2. A repo-wide move to exact engine pins + a lockfile, making cross-language
   byte lockstep pinnable at all — i.e. the operator grants
   `DEC-LAKE-EXACT-PIN-001` knowing it buys zero speed.
3. A coordinator ruling relaxing the slice's byte rule to the row/value
   contract `window_ledger_store` already documents — which removes the FAIL
   wall but still leaves zero performance motive.

## 8. Consequences

* No code changes; no new crate; no CI lane; no flags flipped. The Python
  data-lake layer remains the single production implementation AND the parity
  oracle, unchanged.
* The five §6 flags are on record with recommendations; any future
  reversal starts from this ADR + its ledger entry, not from scratch.
* Kernel-track ownership boundaries (sweep cores, `arrow_table_to_bars`,
  frozen `rust/trading-core` per DEC-SHARED-CORE-001, MIGRATION.md #441) were
  not touched and are unaffected.
