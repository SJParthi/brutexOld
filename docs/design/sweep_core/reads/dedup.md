# AREA DEDUP — combo dedup tier map (read-only survey, 2026-07-08)

Files read: `brutex/orchestrator/bloom.py`, `brutex/data/option_dedup.py`,
`brutex/data/lmdb_membership.py`, `brutex/engine/sweep_cache.py`,
`brutex/engine/sweep_run.py`, `brutex/engine/sweep_launch.py`,
`brutex/engine/sweep_orchestrator.py`, `brutex/engine/sweep_supervisor.py`,
`brutex/strategies/combo.py`.

## 0. Big picture — THREE distinct dedup surfaces, TWO grains

1. **MASK grain (per-combo)** — `ComboDedup(OptionDedupIndex(Bloom, exact))`,
   consulted inside `run_persisted_sweep`'s enumeration loop
   (`brutex/engine/sweep_run.py:1438-1460`). **DEAD on the production digest
   path** — see §5. The production stand-ins are the RAISING no-alloc
   `DigestPathNoDedup` (`sweep_launch.py:99-166`) and the parallel driver's
   private `_NoDedup` (`sweep_parallel.py:610-640`).
2. **TUPLE grain** — `SweepCache` (Bloom L1 + LMDB `LmdbMembership` L2),
   consulted once per grid tuple by the supervisor
   (`sweep_supervisor.py:709-728` seen / `:791-800` record). This is the ONLY
   production Bloom actually built (`sweep_cache.py:229-230` blast-radius note).
3. **Tuple COMPLETION SENTINEL** — the redo authority on the digest path
   (`sweep_run.py:1310-1333` check; `:1615-1629` write). Not a Bloom/LMDB
   structure at all; a per-store sidecar file.

## 1. Exact mechanics / call order

### 1.1 BloomFilter (`brutex/orchestrator/bloom.py`)
- Wraps `fastbloom_rs.BloomFilter` (Rust PyO3 wheel), loaded LAZILY at first
  construction (`_load_rust_bloom`, bloom.py:126-151 — abi3 modules may init
  only once per process; import of the module itself must stay side-effect free,
  DEC-ORCH-LAZY-BLOOM-001).
- Construction (`__post_init__`, bloom.py:354-404):
  1. validate `capacity >= 1`, `fpr_target ∈ [MIN_FPR_TARGET=1e-9, MAX_FPR_TARGET=0.5)`;
  2. sizing math with `ceil` both times (bloom.py:369-374):
     `m_bits = ceil(-n·ln p / ln²2)`, `k = max(1, ceil((m_bits/n)·ln 2))`;
  3. **BloomTooLargeError** if `m_bytes > MAX_BLOOM_BYTES` (bloom.py:377-383)
     — REFUSAL happens BEFORE `_load_rust_bloom` allocation;
  4. build the Rust impl from `(capacity, fpr_target)` (it computes its own
     internal m,k — the Python-computed values are observability only);
  5. one `bloom_init` structlog line (audit-replayable config).
- `add(content_hash: bytes)` (bloom.py:408-443): TypeError on non-bytes; under
  `self._lock`, **FAIL_CLOSED capacity gate**: if
  `_insertions_attempted >= capacity` raise `BloomCapacityExceeded`
  (bloom.py:433-441) — the counter counts ADD CALLS, not distinct keys
  (bloom.py:486-508 docstring: honest only if the caller adds each key at most
  once; both production callers guarantee that — `SweepCache.record_digest`
  probes exact first, and `WorkItemStore.put` adds only on first LMDB insert,
  DEC-DEDUP-CAPACITY-UNIQUE-001). Then `_impl.add_bytes(hash)` + counter++.
- `might_contain(content_hash: bytes) -> bool` (bloom.py:445-481): **lockless
  read**. Contract: `False` ⇒ definitely absent; `True` ⇒ maybe (confirm at L2).
  Documented concurrent-race: a reader racing its own writer's non-atomic k
  bit-flips MAY see a false negative — tolerated ONLY because L2 is ground truth
  (bloom.py:464-474). Hash-agnostic: never re-hashes; caller supplies the
  32-byte blake3 digest (bloom.py:36-39, 335-339).
- Capacities/ceilings:
  - `DEFAULT_CAPACITY = 100_000_000`, `DEFAULT_FPR = 1e-7` (bloom.py:177-183).
  - `MAX_BLOOM_BYTES` = RAM-derived ONCE at import
    (`resolve_max_bloom_bytes()`, bloom.py:208-245): env
    `BRUTEX_MAX_BLOOM_BYTES` override (fail-loud) > detected-RAM × 1/3 >
    16 GiB floor `MAX_BLOOM_BYTES_FLOOR` when RAM undetectable. Results-safe
    by construction — Bloom is a prefilter, never in run identity.
  - `unique_count_estimate` / `empirical_fpr` are O(m_bytes) scans
    (`fno_math.popcount_bytes` kernel with stdlib fallback, bloom.py:154-167) —
    audit/dashboard only, NEVER hot path.

### 1.2 OptionDedupIndex (`brutex/data/option_dedup.py`)
- Composition: `_bloom: BloomMembership` (default `BloomFilter(capacity,
  fpr_target)`) + `_exact: ExactMembership` (default in-memory `set`; inject
  `LmdbMembership` for the persistent tier) + ONE `threading.Lock`
  (option_dedup.py:193-207).
- **Probe ordering LAW (bloom-before-exact-confirm — VERIFIED + PINNED,
  DEC-OPTION-DEDUP-BLOOM-VERIFY-PIN-001, MIGRATION #1547)**:
  `_seen_locked` (option_dedup.py:209-218):
  1. `if not bloom.might_contain(key): return False` — negative short-circuit,
     the exact tier is NEVER touched for a fresh key (keeps the common case
     free of an LMDB read);
  2. else `return key in exact` — EVERY Bloom positive (real or false) is
     exact-confirmed, so a false positive can never phantom-drop a new key.
  Regression pins: a spy exact tier proves the negative short-circuit; an
  always-true Bloom proves no-phantom fall-through at both public probe entries
  (`add_if_new` / `unique_option_bars`) (option_dedup.py:180-183).
- `_add_locked` ordering is **Bloom FIRST then exact** (option_dedup.py:220-223)
  — the OPPOSITE of SweepCache's exact-first durability ordering; flagged as a
  Rule-11 COMMON follow-up in sweep_cache.py:638-642, deliberately NOT changed.
- Public ops, each atomic O(1) under the one lock: `seen(key)`, `add(key)`,
  `add_if_new(key) -> bool` (check-then-act is ONE critical section so a key is
  admitted exactly once across concurrent workers,
  DEC-DHAN-PARALLEL-INGEST-001; option_dedup.py:240-251). No I/O under the lock
  for the in-memory tiers; an injected LMDB exact backing has its own locking.
- Key derivation (option-bar surface): `option_bar_key` =
  `fno_math.blake3_hash("\x1f".join([underlying, expiry, strike, side,
  str(epoch)]).encode())` → raw 32 bytes (option_dedup.py:80-130). Guard: any
  field containing `\x1f` raises `CandleParseError` ("reserved unit separator")
  — DELIBERATELY NOT migrated onto `strategies.combo.reject_unit_separator`
  (different pinned error contract; reject-only semantics identical,
  option_dedup.py:109-116).

### 1.3 LmdbMembership (`brutex/data/lmdb_membership.py`) — the exact/L2 store
- A pure `set[bytes]` over LMDB: key = the 32-byte digest, value = `b""`
  (`_PRESENT`, lmdb_membership.py:54). Off-heap (memory-mapped), per-op RAM
  O(1); B+tree get/put O(log n) theoretical, O(1) practical for a dense working
  set (bench-pinned `tests/bench/test_lmdb_membership_o1.py`;
  lmdb_membership.py:8-11).
- `open(path, map_size_mb=None)` (lmdb_membership.py:152-186): resolution
  explicit arg > env `BRUTEX_SWEEP_CACHE_MAP_MB` > module default
  `DEFAULT_MAP_SIZE_MB = 10*1024` MiB (this module) — NOTE SweepCache has its
  OWN 2 GiB default (`sweep_cache.py:75`); `resolve_positive_int_env` returns
  `None`-when-unset so one shared env var can't collapse the two distinct
  defaults (lmdb_membership.py:84-136). `lmdb.open(..., max_dbs=1, subdir=True,
  create=True, lock=True)`.
- Ops: `__contains__` = read txn point get (lmdb_membership.py:235-239);
  `add` = write txn `txn.put(key, b"")` — re-add is a no-op overwrite,
  idempotent (lmdb_membership.py:241-257); `add_many(keys)` = ONE write txn for
  the whole batch, aborts atomically on map-full (lmdb_membership.py:259-282);
  `__len__` = O(1) LMDB stat `entries` (lmdb_membership.py:284-294);
  `iter_keys()` = cursor walk holding ONE read txn open the whole time — caller
  must consume promptly (lmdb_membership.py:296-310); used only for the resume
  Bloom rebuild.
- Failure modes: `LmdbMembershipError` on use-after-close (`_require_open`,
  lmdb_membership.py:208-218); `lmdb.MapFullError` → `LmdbMapFullError`
  (actionable message naming the LIVE map size from `env.info()['map_size']`
  and the `--sweep-cache-map-mb` remedy; fatal by design — propagates to the
  supervisor abort path, never retried; lmdb_membership.py:61-75, 220-233).
  Other `lmdb.Error` propagates natively.

### 1.4 ComboDedup (`brutex/engine/sweep_run.py:296-365`) — mask-grain adapter
- A thin re-keying adapter over `OptionDedupIndex`: hex run-id str → raw bytes.
  - `seen(run_id_hex) -> bool` = `index.seen(bytes.fromhex(run_id_hex))`
    (sweep_run.py:318-332).
  - `mark(run_id_hex)` = `index.add(...)` (sweep_run.py:334-342).
  - `mark_many(hexes) -> int` = loop `index.add(...)`, returns count PROCESSED
    (not net-new; re-mark is a harmless no-op absorbed by the tiers;
    sweep_run.py:344-365).
- Satisfies the structural `DedupSink` Protocol (`seen` + `mark_many`,
  sweep_run.py:275-293, `@runtime_checkable`) — the seam `run_persisted_sweep`
  is annotated with, so raising stand-ins drop in.

### 1.5 Mask key derivation (run identity)
- `mask_run_id` (sweep_run.py:368-426) = `combo_hash(strategy_id=
  f"mask:{mask:032x}", direction, instrument, tf, mode, params,
  data_snapshot_hash, indicator_version, code_commit_sha)` — 64-char lowercase
  BLAKE3 hex. `combo_hash` (strategies/combo.py:172-289): non-empty-field
  validation + `reject_unit_separator` over all 8 string fields
  (combo.py:257-270), `canonicalise_params(params)` (sorted-key JSON,
  NaN-rejected, depth-capped 64), then payload =
  `"\x1f".join([strategy_id, direction, instrument, tf, mode, canonical_params,
  data_snapshot_hash, indicator_version, code_commit_sha])` →
  `fno_math.blake3_hash(payload.encode()).hex()` (combo.py:272-289).
- The dedup key stored in Bloom/LMDB by ComboDedup is `bytes.fromhex(that hex)`
  = the raw 32-byte digest.

### 1.6 SweepCache (`brutex/engine/sweep_cache.py`) — tuple-grain Bloom+LMDB
- Key: `combo_digest(mask, instrument, tf, direction, params, *,
  data_snapshot_hash, indicator_version, code_commit_sha)` (sweep_cache.py:288-409):
  the 3 repro hashes are STRUCTURAL kw-only params
  (DEC-COMBO-DIGEST-REPRO-HASHES-001) — refuses loudly if `params` carries the
  reserved keys (`SweepCacheError`), then injects them under the SAME keys and
  digests `blake3(json.dumps({"mask": int(mask), "instrument", "tf",
  "direction", "params": merged}, sort_keys=True, separators=(",",":"),
  ensure_ascii=False, allow_nan=False))` → raw 32 bytes. Uses the WHEEL-FREE
  `blake3` PyPI package, not `fno_math` (module must run without the Rust
  kernel; sweep_cache.py:21-23). Byte-identity with the pre-change convention
  is golden-pinned — **existing on-disk LMDB stores would be invalidated by any
  serialisation change**.
- NOTE the supervisor passes `tuple_.k` as the `mask` argument
  (sweep_supervisor.py:709-718, 791-800) — at the tuple grain the "mask" slot
  carries `k`; the real distinguishers (sl/tp/proximity/fib_anchor/prior_n/
  condition_bits/top_k semantics) travel in `_tuple_cache_params(tuple_, grid)`
  (sweep_supervisor.py:340-391 — fib_anchor included; keying without it would
  FALSELY skip the second anchor, sweep_supervisor.py:380-382).
- `SweepCache.open(path, bloom_capacity=None, bloom_fpr=1e-7, map_size_mb=None)`
  (sweep_cache.py:444-529):
  1. resolve knobs: capacity explicit > `BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY` >
     `DEFAULT_BLOOM_CAPACITY = 5_000_000`; map explicit >
     `BRUTEX_SWEEP_CACHE_MAP_MB` > `DEFAULT_MAP_SIZE_MB = 2*1024` MiB;
  2. open L2 `LmdbMembership` at `path/"exact"` FIRST;
  3. `_auto_grow_capacity(configured, len(exact), fpr)` (sweep_cache.py:201-277):
     `persisted <= configured` → configured (byte-identical); else
     `grown = max(persisted, ceil(configured × 1.25))`, refused with
     `SweepCacheCapacityError` if `_compute_m_bytes(grown, fpr) >
     MAX_BLOOM_BYTES` (FAIL_CLOSED — above the wall a capacity raise cannot
     help; remedies: loosen fpr / sharded follow-up); a grow logs ONE
     `sweep_cache.bloom_auto_grow` WARN;
  4. build the fresh in-RAM Bloom, then `_rebuild_bloom_from_exact` — replay
     EVERY persisted digest via `exact.iter_keys()` into the Bloom
     (O(persisted), once per open; the count-vs-capacity pre-check kept as
     defence-in-depth, sweep_cache.py:531-564). Without the rebuild every
     persisted digest would read Bloom-negative and the whole prior sweep
     would re-run.
  5. any failure → `exact.close()` before re-raise (no leaked LMDB env,
     sweep_cache.py:523-528).
- `seen_digest` (sweep_cache.py:591-620): under `self._lock` — Bloom
  `might_contain` first (negative → `False`, counter `_bloom_negatives++`),
  positive → `digest in exact` (hit counter). Probe + confirm are ONE critical
  section.
- `record_digest` (sweep_cache.py:622-659): under the SAME lock —
  `if digest in exact: return False` (idempotent, never re-inserts into the
  Bloom so duplicates never erode FPR or the FAIL_CLOSED counter), else
  **exact.add FIRST, bloom.add SECOND** (durability ordering R2-SL-F4-F5: a
  map-full fault propagates with the Bloom untouched → no phantom Bloom entry
  can outlive the failure; Bloom has no delete so the reverse order could never
  roll back).
- Observability: `snapshot()` returns
  `{checks, bloom_negatives, hits, records, bloom_insertions, bloom_bytes, closed}`
  (sweep_cache.py:726-742).

## 2. How the sweep consumes dedup per combo — call order

### 2.1 The per-mask loop (`run_persisted_sweep`, sweep_run.py:1157-1670)
Order of operations (docstring sweep_run.py:1169-1217 + code):
1. validate bars / HOLD / `max_combos` (raises BEFORE Stage-1A build);
2. **sentinel check** (digest path only): `_sentinel_resume(store.path, digest)`
   — a PAIRED sentinel → one `SWEEP_RESUMED` audit line + short-circuit return
   (`resumed_complete=True`, all tallies 0; sweep_run.py:1310-1333);
3. `plan_rank_memory` pre-flight; `SWEEP_START` audit; Stage-1A `bar_bits`
   build or kernel reuse (fail-fast on n_bars / fib_anchor mismatch,
   sweep_run.py:1388-1408);
4. `bypass_dedup = trial_stats_digest is not None` (sweep_run.py:1415);
5. enumeration loop over `iter_masks_k_of_bits(cfg.condition_bits, cfg.k)`
   (sweep_run.py:1438-1460):
   - `enumerated += 1`; `has_conflict(mask)` → `conflict_pruned++, continue`;
   - **digest path**: `survivors.append(mask); continue` — NO run_id computed,
     NO dedup probe (lazy-run-id optimisation deletes O(survivors) BLAKE3 work
     + the (mask, run_id) retention, DEC-SWEEP-STREAMING-RANK-001);
   - **legacy no-digest path**: `run_id = _lazy_run_id(mask)` (eager, the probe
     needs it), then `if dedup.seen(run_id): skipped_dedup++, continue`; else
     append to `survivors` + `survivor_run_ids` (parallel lists).
6. Stage-1B min_hits pre-filter over survivors (kernel `count_hits` or
   `count_supermasks`); `evaluated = len(survivors)`;
7. `run_id_of` resolver: digest path = `_lazy_run_id` (recompute on demand,
   pure); legacy = `dict(zip(survivors, survivor_run_ids)).__getitem__`
   (sweep_run.py:1481-1485). Consumers: SweepComboError fingerprints + finalist
   rows ONLY;
8. chunked Rust trade-sim → metrics → rank → persist finalists → `store.flush()`;
9. trial-stats sidecar (digest path) BETWEEN flush and mark;
10. **mark**: `marked = 0 if bypass_dedup else dedup.mark_many(survivor_run_ids)`
    (sweep_run.py:1614) — marks are written AFTER the flush (redo-not-skip: a
    crash between evaluation and flush re-does rather than skips); on the
    digest path marking is SKIPPED entirely (pure wasted LMDB writes,
    DEC-SWEEP-DEDUP-LMDB-001) and `n_marked` honestly reads 0;
11. completion sentinel written STRICTLY LAST (digest path) — its presence
    proves flush + sidecar + mark all completed (sweep_run.py:1615-1629);
12. `SWEEP_FLUSH` + `SWEEP_COMPLETE` audit lines (exactly 3 audit lines per
    full sweep regardless of combo count — O(1) audit).

### 2.2 Who builds which sink (launch wiring)
- `plan_launch_dedup(grid, *, digest_path, exact=None)`
  (sweep_launch.py:204-278) — the ONE launch decision point:
  - `digest_path=True` (BOTH live CLI flows — `run_one_tuple` always passes a
    non-None `trial_stats_digest` via `_tuple_digest(tuple_, grid)`,
    sweep_orchestrator.py:868-878): return `DigestPathNoDedup()` — no Bloom, no
    OptionDedupIndex, no LMDB, O(1) alloc-free. Every probe RAISES
    `SweepLaunchUsageError` (belt-and-braces: a real call means the
    digest/bypass wiring regressed; fabricated verdicts forbidden).
  - `digest_path=False` (defensive legacy arm, unreachable from live CLI):
    `capacity = bloom_capacity_hint(grid)` (=`Σ_k C(n_unique,k) ×
    Π(other axes)`, an upper bound on enumerated masks,
    sweep_orchestrator.py:626-661), then
    `ComboDedup(OptionDedupIndex(capacity=cap, fpr_target=1e-7, exact=exact))`;
    a `BloomTooLargeError` maps to the operator-facing
    `SweepLaunchCapacityError` naming the ≈4.1e9-identity ceiling implied by
    the 16 GiB floor (`launch_bloom_identity_ceiling`, sweep_launch.py:169-201).
- Parallel driver workers (`sweep_parallel.py:591, 610-640`): each worker calls
  `run_persisted_sweep(dedup=_NoDedup(), ...)` — a private raising twin of
  `DigestPathNoDedup`. The parallel path is REFUSED on the legacy no-digest arm
  (`refuse_parallel_on_legacy_path`, sweep_parallel.py:457-482): workers without
  the shared dedup would rank an UNFILTERED survivor set → bit-divergent stores.

### 2.3 The tuple-grain flow (supervisor, sweep_supervisor.py:697-820)
Per tuple: watchdog beat → checkpoint-prefix skip →
`guards.cache.seen(k, instrument, tf.label, direction_label,
_tuple_cache_params, *, 3 repro hashes)` → hit ⇒ `skipped_cache++`,
checkpoint, audit `TUPLE_CACHE`, continue. Miss ⇒ `run_one_tuple(...)`;
`SweepComboError` ⇒ counted `tuples_failed`, cache + checkpoint DELIBERATELY
NOT recorded (failed tuple must never be permanently skipped — sweep_heal C5);
`None` (no bars) ⇒ counted, checkpointed; success ⇒ accumulate tallies, THEN
`guards.cache.record(...)` (post-success only), checkpoint, audit `TUPLE_DONE`.
Note (sweep_parallel.py:1047): `SweepCache.record` still runs on a
post-failure success tuple, but `checkpoint.record` is gated on
`not had_failure`.

## 3. Data structures & shapes (summary)

| Structure | Key | Value | Backing | Locking |
|---|---|---|---|---|
| `BloomFilter` | 32-byte digest (pre-hashed, raw bytes) | k bit positions | fastbloom_rs bit array, in-RAM | `add` locked; `might_contain` lockless |
| `OptionDedupIndex` | 32-byte digest | — | Bloom + `ExactMembership` (set or LmdbMembership) | ONE `threading.Lock` around probe+act |
| `LmdbMembership` | 32-byte digest | `b""` | LMDB B+tree, mmap, subdir env | LMDB txns (+ caller's lock) |
| `ComboDedup` | 64-hex run-id → `bytes.fromhex` | — | delegates to OptionDedupIndex | inherited |
| `SweepCache` | `combo_digest` 32 bytes | — | BloomFilter + LmdbMembership at `<root>/exact` | ONE `threading.Lock` around probe/record |
| completion sentinel | tuple 16-hex path digest | `TupleCompletionSentinel{tuple_digest, store_content_hash, sidecar_written, n_marked}` | sidecar file next to the tuple store | n/a |

## 4. Invariants (the laws a Rust conversion must preserve)

1. **Bloom-before-exact-confirm (MIGRATION #1547, pinned):** negative
   short-circuits with NO exact read; EVERY positive is exact-confirmed. Never
   drop a key on a bare Bloom positive (false positive ⇒ silent data loss /
   false skip). Pinned by spy-exact + always-true-Bloom tests at `add_if_new`
   and `unique_option_bars`.
2. **Atomic check-then-act:** `add_if_new` / `record_digest` are single
   critical sections; a digest is admitted to the Bloom EXACTLY once under
   concurrency (no double-count against the FAIL_CLOSED capacity, no
   double-evaluation).
3. **FAIL_CLOSED capacity:** Bloom `add` past `capacity` RAISES
   `BloomCapacityExceeded` (never silent FPR drift); constructor RAISES
   `BloomTooLargeError` above `MAX_BLOOM_BYTES` BEFORE allocation. Capacity
   counter honesty depends on callers adding each key at most once.
4. **Exact-first durability in `SweepCache.record_digest`:** L2 LMDB write
   BEFORE the L1 Bloom insert (a write fault leaves no phantom Bloom entry).
   `OptionDedupIndex._add_locked` is bloom-first — a KNOWN, deliberately
   unchanged divergence (flagged COMMON follow-up).
5. **Mark-after-flush (redo-not-skip):** legacy-path `dedup.mark_many` runs
   only AFTER `store.flush()`; a crash between evaluation and flush re-does
   work, never skips it.
6. **Sentinel-authoritative redo (digest path, DEC-MARK-MANY-SENTINEL-001):**
   the sentinel — never the marks — decides completion; absent/unpaired ⇒ FULL
   re-run with probes BYPASSED (a partially-marked dedup must never produce a
   subset re-rank ⇒ subset trial-stats population, ADR-0008 cond. 2).
   `skipped_dedup == 0` by construction on this path.
7. **Key byte-identity:** `combo_hash` payload = `"\x1f"`-joined 9 fields →
   `fno_math.blake3_hash`; `combo_digest` = canonical JSON (sort_keys,
   `(",",":")` separators, ensure_ascii=False, allow_nan=False) → wheel-free
   `blake3`. Both are golden-pinned; ANY re-serialisation difference in Rust
   invalidates persisted LMDB stores and run identities.
8. **Unit-separator reject-only guards:** `\x1f` in any identity field raises
   (`StrategyError` via `reject_unit_separator` for run identity;
   `CandleParseError` for option-bar keys) — two DISTINCT pinned error
   contracts, never merged.
9. **Resume Bloom rebuild:** on `SweepCache.open` with non-empty L2, the fresh
   Bloom is repopulated from `iter_keys()` (auto-grow ≤ the RAM wall) — a
   Bloom-negative must never mask a persisted digest.
10. **Digest path never consults dedup:** `DigestPathNoDedup` / `_NoDedup`
    RAISE on any call — the regression tripwire, not a no-op.

## 5. Per-combo probe cost (what a Rust kernel would absorb)

- **Production digest path (the only live sweep path): ZERO dedup cost per
  mask.** No run_id hash, no Bloom probe, no LMDB read, no mark. Per enumerated
  mask the loop does: `iter_masks_k_of_bits` next + `has_conflict(mask)` +
  `list.append`. Run-ids are recomputed lazily ONLY for finalists + error
  fingerprints (O(top_k + errors) BLAKE3).
- **Legacy no-digest path (per enumerated non-conflict mask):**
  1 × `mask_run_id` (canonicalise_params JSON + `\x1f` join +
  `fno_math.blake3_hash` + `.hex()`), 1 × `bytes.fromhex`, 1 × lock acquire,
  1 × Rust Bloom `contains`; on Bloom-positive + exact=LMDB: 1 read txn point
  get. Post-flush: `mark_many` = per survivor 1 lock + Bloom `add_bytes` + 1
  LMDB **write txn each** (ComboDedup.mark_many loops `index.add`, it does NOT
  use `LmdbMembership.add_many` batching).
- **Tuple grain:** 1 `seen` + (on success) 1 `record` per tuple — negligible;
  plus O(persisted) Bloom rebuild once per `SweepCache.open`.

## 6. Gotchas for a Rust sweep-core conversion (per-mask loop → PyO3 kernel)

1. **Do not port the per-mask dedup probe into the hot loop at all** on the
   digest path — it is structurally bypassed (`bypass_dedup = digest is not
   None`). The Rust kernel must preserve the tallies exactly:
   `enumerated` (every mask incl. conflicts), `conflict_pruned`,
   `skipped_dedup` (MUST be 0 on the digest path), `evaluated =
   len(survivors)`, `minhits_dropped = evaluated - len(kept)`. These feed the
   hash-chained audit payload + `SweepRunSummary` + trial-stats
   (`n_enumerated`, `n_conflict_pruned`, `n_minhits_dropped`) — any drift
   changes audit bytes and trial-stats sidecars.
2. **The raising stand-ins are a tripwire, not dead code.** If the Rust kernel
   takes over the loop it must NOT call into the `DedupSink` on the digest
   path — but the legacy path's semantics (eager per-mask `seen` BEFORE the
   trade sim, probe order preserved, survivor_run_ids parallel to survivors,
   `mark_many` after flush) must survive if the legacy arm is kept. Simplest
   safe shape: keep the legacy arm in Python; Rust owns only the digest arm.
3. **Enumeration order is load-bearing:** `survivors` order determines chunk
   composition, rank tie-handling inputs, and the legacy
   `dict(zip(survivors, survivor_run_ids))`. `iter_masks_k_of_bits` order must
   be reproduced bit-exactly.
4. **Run-id laziness must stay lazy** (or at least not change bytes): if Rust
   precomputes run_ids it must produce the identical 64-hex BLAKE3 of the
   identical `\x1f` payload with the identical `canonicalise_params` JSON
   (sorted keys, ensure_ascii=True in combo.py:168 — note combo.py uses
   `ensure_ascii=True` while sweep_cache's combo_digest uses
   `ensure_ascii=False`; two different canonicalisers, do not unify).
5. **GIL/lock model:** `OptionDedupIndex` / `SweepCache` locks are Python
   `threading.Lock`s; `BloomFilter.might_contain` is deliberately lockless
   with a documented same-key writer-race false-negative tolerated ONLY
   because L2 confirms. A Rust port batching probes must keep probe+insert
   atomicity per key (invariant 2) and must not turn the tolerated
   Bloom-false-negative race into an exact-tier miss.
6. **Capacity counter semantics:** `insertions_attempted` counts calls, not
   distinct keys. A Rust batch-add that retries or double-adds would trip
   `BloomCapacityExceeded` early (or dishonestly inflate). Callers guarantee
   at-most-once adds — preserve that contract.
7. **LMDB single-writer / txn shape:** `ComboDedup.mark_many` = one write txn
   per key today. Batching through `LmdbMembership.add_many` (one txn) is
   result-identical and cheaper — but the map-full abort then loses the WHOLE
   batch atomically (documented contract) vs today's per-key partial progress;
   `n_marked` reporting semantics would shift on the fault path. Also
   `iter_keys` holds one read txn open across the whole walk — a Rust resume
   rebuild must not interleave writes into the same env mid-walk.
8. **fastbloom_rs abi3 once-per-process init** (bloom.py:126-151): any Rust
   kernel embedding its own Bloom must not re-init the PyO3 module; if the
   sweep-core kernel replaces fastbloom_rs with an internal filter, the
   `bloom_init` structlog replay event and the (capacity, fpr, m_bits, k)
   observability contract still need to be emitted for the audit oracle.
9. **MAX_BLOOM_BYTES is resolved ONCE at import** (bloom.py:239-245) — env
   read at process start; a Rust kernel reading the env lazily/differently
   would diverge from the pinned once-per-process law.
10. **Byte-identity of persisted stores:** LMDB keys (32-byte digests) and the
    Bloom rebuild path assume the exact digest derivations of §1.5/§1.6. A Rust
    reimplementation of blake3/json canonicalisation must be bit-identical or
    every existing exact store silently stops matching (stale re-runs — safe
    but expensive) and golden pins fail.
11. **Error taxonomy must survive the FFI boundary:** `BloomCapacityExceeded`,
    `BloomTooLargeError`/`BloomConfigError`, `LmdbMapFullError` (fatal,
    supervisor abort), `SweepCacheCapacityError` (FAIL_CLOSED resume refusal),
    `SweepLaunchUsageError` (wiring regression), `SweepComboError` (tuple-skip
    contract), `CandleParseError` vs `StrategyError` unit-separator contracts —
    each has a pinned type + message shape consumed by tests and the
    supervisor's skip/abort branching. A Rust panic or generic exception in
    their place breaks the "skip + continue" vs "global abort" split.
12. **Mark/flush/sidecar/sentinel ORDER** (flush → sidecar → mark → sentinel,
    sentinel STRICTLY LAST) is the crash-recovery protocol. If the Rust kernel
    absorbs any of these steps, the order — and the fact that the sentinel
    write happens only after `mark_many` returns — must not be reordered or
    made concurrent.
