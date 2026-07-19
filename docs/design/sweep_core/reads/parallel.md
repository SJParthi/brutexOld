# Area PARALLEL+MEMORY — sweep parallelism, memory budgets, checkpoint/resume, look-ahead barrier, exit-index fast path, Rust-metrics tail

All paths relative to /home/user/bruteX. Read 2026-07-08 on branch `claude/data-edge-guards`.

---

## 1. Two nesting levels of orchestration (critical mental model)

There are TWO loops a Rust sweep-core conversion must distinguish:

1. **Per-TUPLE loop** (grid level): `sweep_parallel.py` / `sweep_supervisor.run_supervised_grid`.
   Unit of parallelism = one `SweepTuple` (instrument, tf, direction, k, params). Checkpoint,
   cache, audit, progress all operate at TUPLE granularity.
2. **Per-MASK loop** (inside one tuple): `sweep_run.run_persisted_sweep` →
   `_iter_mask_metrics` (sweep_run.py:1021). Unit = one u128 mask. There is NO per-mask
   checkpoint — a tuple is atomic for resume purposes (completion sentinel written strictly
   last, sweep_run.py:1615-1629). **A batched Rust per-mask kernel therefore sits entirely
   INSIDE one tuple, below every checkpoint/resume boundary** — it may batch masks any way it
   likes as long as the yielded (mask, metrics) stream is in input order and byte-identical.

---

## 2. sweep_parallel.py — worker partitioning + determinism pins

### Mechanics / call order (`ParallelGridDriver.run`, :784)
- Entry: `run_parallel_grid(grid, bars_for, root, ingest_audit, guards, workers, ...)` (:1252).
  Hard-refuses `workers < 2` (`_MIN_PARALLEL_WORKERS`, :315); `workers > 1` on the legacy
  no-digest path is hard-refused by `refuse_parallel_on_legacy_path` (:453) — parallelism is
  SOUND only because the digest path sets `bypass_dedup=True` (no cross-worker dedup race;
  completion sentinel is the sole redo authority, :76-85).
- `run()`: `guards.checkpoint.load()` → `resume_from = checkpoint.next_index` (:798-799);
  `progress.start`; SUPERVISOR_START audit line (payload adds a `workers` key — the ONE
  content difference vs sequential, :809).
- Pool: `ProcessPoolExecutor(max_workers=workers-1, mp_context=spawn, initializer=_worker_init,
  initargs=(_WorkerContext(grid, root),))` (:827-840). **SPAWN never FORK** — parent holds
  writer/logging locks + watchdog thread; fork would deadlock (comment :829-836, golden test
  deadlocks under fork).
- `_worker_init` (:540): pins `OMP_NUM_THREADS=1`, `RAYON_NUM_THREADS=1`,
  `POLARS_MAX_THREADS=1` in every worker + stashes `_WORKER_CTX` module global.
  **DETERMINISM PIN**: single-threaded numerics per worker prevents order-dependent float
  reductions AND workers×num_cpus oversubscription. The resident Rust trade-sim's rayon arm is
  serialised under this pin (per-tuple speedup is the SERIAL-kernel ratio; parallelism is
  strictly ACROSS tuples, :33-39). A batched Rust metrics kernel running under the pool MUST
  respect RAYON_NUM_THREADS=1 or preserve bit-identical reduction order regardless of thread
  count.
- `_drive` (:899): interleave loop `while not exhausted or pending or heap`:
  1. **Dispatch** while `len(pending) < max_inflight` AND `len(heap) < heap_cap` (:935).
     `_gate_or_dispatch` (:962): parent-side gates — `index < resume_from` → synchronous
     `"resume"` skip outcome; `guards.cache.seen(...)` (keyed on k/instrument/tf/direction/
     params + data_snapshot_hash + indicator_version + code_commit_sha, :979-988) →
     `"cache"` skip; else `bars = self._bars_for(...)` loaded ON THE PARENT and marshalled
     with `pool.submit(_run_tuple_in_worker, index, tuple_, bars)`.
  2. **Apply** contiguous heap prefix `_apply_ready_prefix` (:1024): pop while
     `heap[0].index == next_apply`; apply; `next_apply += 1`.
  3. **Await** next future via `_futures_wait(..., FIRST_COMPLETED)` with stall deadline
     (:995-1022): no completion within `stall_seconds` (DEFAULT_STALL_SECONDS=300.0, :310)
     → raise `SweepStallError` (:434). Stall clock resets on ANY completion or apply —
     head-of-line liveness (:62-74, :908-924).

### Back-pressure / bounded memory (Golden Rule #5)
- `_INFLIGHT_MULTIPLIER = 2` (:291): outstanding futures ≤ 2×pool_size.
- `_HEAP_CAP_MULTIPLIER = 2` (:304): dispatch pauses while reorder heap ≥ 2×pool_size, so the
  heap is O(pool_size); true ceiling `pool_size × (2+2)` counting in-flight overshoot (:299-303).
  `next_apply` is always in heap or pending (dispatch is strictly in enumeration order) so
  pausing dispatch can never deadlock (:915-919).

### Worker body (`_run_tuple_in_worker`, :564)
- Rebuilds `store_for` from `(root, grid)` via `make_store_for` (no unpicklable closure crosses
  the boundary); collects audit lines into `BufferedAuditCollector` (worker NEVER opens the real
  hash chain); passes `_NoDedup()` (:610) whose `seen`/`mark_many` RAISE — structural proof the
  digest path never consults dedup. `SweepComboError` → returned as `failed_error` string
  (re-materialised on the parent); `MemoryError`/anything else propagates → future breaks →
  pool teardown.
- Returns `TupleWorkResult(index, summary: SweepRunSummary|None, failed_error: str|None,
  audit_records: tuple[BufferedAuditRecord,...])` (:492-516). `summary=None, failed_error=None`
  = no-bars skip.

### Apply order + checkpoint interaction (`_apply`, :1039)
- `_Outcome` (frozen dataclass, min-heap keyed on `index` via `__lt__`, :649-678):
  kinds `"resume"|"cache"|"no_bars"|"failed"|"done"` mirroring the 5 sequential branches.
- For worker outcomes: replay buffered SWEEP_* ingest-audit records FIRST (:1079,
  `_replay_ingest_audit` :1153), then supervisor line + checkpoint/cache/progress.
- **CONTIGUOUS-CLEAN-PREFIX checkpoint**: `_Accumulator.had_failure` latch (:1170-1191) —
  once ANY tuple fails (in enumeration order), NO further `guards.checkpoint.record(index)`
  runs (:1066, :1101, :1134), freezing the checkpoint at the last clean index BEFORE the first
  failure so resume re-runs the failed tuple. `SweepCache.record` STILL runs on post-failure
  successes (:1124) — those short-circuit on resume via the cache tier, not the checkpoint.
- Equivalence invariant (:695-703): per-tuple Parquet tree BYTE-IDENTICAL to sequential; both
  audit chains CONTENT-IDENTICAL MODULO TIMESTAMPS (+ the `workers` key); chains verify;
  checkpoint identical. `ts_utc` participates in chain hashes so chains aren't literally
  byte-equal across runs.

### Worker-count resolution (`resolve_sweep_workers`, :318)
- Env `BRUTEX_SWEEP_WORKERS`; unset/blank → AUTO = `max(1, min(cpu_count-2, 8))` via COMMON
  `cpu_pool_workers(reserve=2, cap=8, prefer_performance=False)` (:247-284).
  `AUTO_SWEEP_WORKERS_CAP = 8` (:183) — 1 writer-actor + 7 EVAL processes; the #1541 bench
  (bench-m4pro.yml worker-scaling A/B, run 28851211762) verdict was **KEEP CAP=8** (the cap
  itself is documented as "conservative operator-authorized ceiling, not a measured optimum",
  :180-182). `=1` = explicit sequential opt-out (never demoted/promoted). INVALID env → AUTO
  (ADR-0016 §4 conscious reversal, WARN-logged, :335-342). Small-grid demotion:
  `tuple_count < factor × auto` → 1; factor default `SMALL_GRID_FACTOR=2`, env
  `BRUTEX_SWEEP_SMALL_GRID_FACTOR` (0 disables) (:196-244, :397-413).

---

## 3. sweep_mem_budget.py — RAM-scaled budgets (#1360/#1490)

- Pure planning module; nothing touches the per-mask hot loop (:40).
- Knobs: `BRUTEX_SWEEP_BOUNDED_RANK` ∈ {off, auto, on}, default AUTO
  (`resolve_bounded_rank_mode` :106 — garbage RAISES ValueError, fail-loud);
  `BRUTEX_SWEEP_MEM_BUDGET_BYTES` (`resolve_mem_budget_bytes` :137 delegates to COMMON
  `config/hardware_envelope.memory_budget_bytes(fraction=1/6, floor=8 GiB)`).
- `hardware_envelope.memory_budget_bytes` (config/hardware_envelope.py:673): resolution order
  (1) non-blank env override — validated positive int, else ValueError; (2) RAM undetected
  (`detect_total_ram_bytes()==UNDETECTED_RAM==0`) → `floor_bytes` EXACTLY; (3) detected →
  `max(1, int(total × fraction / workers))`, deliberately NOT clamped to floor (:731-765).
  48 GiB × 1/6 = the old 8 GiB constant exactly (regression-pinned).
- `plan_rank_memory(n_masks, top_k, digest_path, mode, budget_bytes)` (:264): projection =
  `n_masks × BATCH_RANK_BYTES_PER_KEPT(1024)` — a MODEL with no prune credit. Decision table
  (:279-294): ON → bounded (requires digest path + finite top_k else ValueError); ≤budget →
  batch (byte-identical); >budget+OFF → refusal; >budget+AUTO → bounded when eligible else
  refusal. Returns `RankMemoryPlan(mode, budget_bytes, projected_bytes, n_masks,
  bounded_top_k|None, refusal|None)` (:169-218); `refusal` string is wrapped by the runner in
  `SweepMemoryBudgetError` (a `SweepComboError` subclass, sweep_run.py:218 — tuple-skip, never
  a raw MemoryError).
- `watermark_message` (:249): mid-flight breach message for the bounded accumulator's EXACT
  `owned_nbytes()` watermark (enforced per-append at sweep_run.py:1530-1538).
- **Determinism pin**: the plan is pure in config (n_masks = C(n,k), known pre-allocation), so
  the same grid takes the same path on every machine/resume — and results are byte-identical
  either way (bounded == batch bit-for-bit, property-pinned) (:272-277).

---

## 4. sweep_checkpoint.py — resume semantics

- `SweepCheckpoint` (frozen, :59): `{version:1, sweep_id, total_combos, last_completed_index
  ∈ [-1, total), updated_at}`. `next_index = last+1`; `-1` = fresh.
- `SweepCheckpointStore` (:166): single JSON file; `save` = tmp → fsync → `os.replace` via
  COMMON guard-free `atomic_write_simple` (raw OSError → `SweepCheckpointError`, :284-321).
- `load()` (:223): missing file → None; **different `sweep_id` → None** (a stale checkpoint
  from another grid never resumes this one — sweep_id = `grid_identity(grid)` BLAKE3,
  sweep_supervisor.py:317); adopts high-water mark on match.
- `record(completed_index)` (:254): MONOTONIC high-water — `<= current` is a no-op returning
  False (a re-delivered callback can never rewind); out-of-range raises. O(1) per op.
- **Resume correctness ASSUMES deterministic enumeration order** (:33-37): the checkpoint is
  an index into `iter_sweep_tuples(grid)`'s fixed canonical order, not a content address. A
  Rust conversion MUST NOT reorder tuple enumeration.
- `SweepGuards` (sweep_supervisor.py:182): `audit` (SweepAuditWriter), `progress`,
  `checkpoint` (SweepCheckpointStore, MUST be built with sweep_id==grid_identity(grid) and
  total_combos==count_sweep_tuples(grid)), `cache` (SweepCache Bloom→LMDB tuple dedup),
  `watchdog`, `worker_name`. Sequential loop mirrors the same `had_failure` freeze
  (sweep_supervisor.py:642-802).
- **Intra-tuple resume** is the completion SENTINEL, not the checkpoint: `_sentinel_resume`
  (sweep_run.py:840) requires the sentinel to exist, parse, carry this tuple's digest, AND
  the store content sha256 + trial-stats sidecar to PAIR — any failure → loud
  `sweep_run.sentinel_unpaired` → FULL redo with dedup probes bypassed (never a subset
  re-rank; marks are advisory — MIGRATION.md #650). Sentinel is written STRICTLY LAST after
  flush → sidecar → mark (sweep_run.py:1578-1629).

---

## 5. sweep_lookahead.py — opt-in barrier + env_flag_tristate

- Env `BRUTEX_SWEEP_LOOKAHEAD_GUARD`, default OFF; `_TRUTHY={1,true,yes,on}`,
  `_FALSY={0,false,no,off}` (:95-104) — **this module is the vocabulary HOME**; the COMMON
  classifier `env_flag_tristate(raw) -> bool|None` (:115) returns True/False/None (None =
  unset/unrecognised; caller owns default + warn policy). `rust_metrics` imports it (never
  copies).
- `maybe_guard_bars(bars, enabled=None)` (:265): resolved ONCE per sweep at setup
  (sweep_run.py:1296). **Zero-cost OFF contract: OFF returns `bars` ITSELF (identity)** —
  no per-bar flag check anywhere; A/B bench-pinned. ON wraps in `GuardedBarWindow`.
- `GuardedBarWindow` (:159): read-only `Sequence[CandleBar]` reusing the COMMON
  `LookAheadBarrier(record=False)` (O(1) memory). Iteration advances a forward-only
  consumption front; `__getitem__` int/slice validates every touched index against the front
  (ahead → `LookAheadError`; IndexError checked BEFORE the barrier, :239-262). Front is
  monotone across the WHOLE run — does NOT re-arm per phase (later-phase peeks are the
  suffix-independence property gates' layer, :50-64). Does NOT interpose on derived state
  (bar_bits, walker state).
- **Rust-conversion gotcha**: when the guard is ON, `bars` is a Python-object Sequence with
  guard semantics — a Rust core that snapshots bars into native buffers up front performs one
  full sequential pass; that is exactly the legitimate "first sequential pass" and is fine,
  but any Rust-side index-ahead read pattern that goes through this wrapper before the front
  reaches it would raise. If the Rust core bypasses the wrapper entirely (reads native
  copies), the ON-mode teeth silently vanish for those reads — an honesty/coverage change
  that must be disclosed, not broken silently.

---

## 6. exit_index.py — the timestamp↔index seam (#1531 fast path)

- Stdlib-only leaf, imports NOTHING from brutex at runtime (cycle-freedom, :26-31).
- `build_ts_index(bars) -> {timestamp: index}` (:62): last-wins on duplicate timestamps —
  byte-identical to the 3 historical inline copies. O(N_bars).
- `bars_strictly_increasing(bars)` (:74): PRODUCER certificate — strictly increasing
  timestamps ⇒ index↔ts bijection ⇒ true exit index == dict lookup provably. Checked ONCE at
  `ResidentTradeSim` construction (`_ts_certified`, rust_trade_sim.py:340).
- `exit_indices_consistent(bars, trades, exit_indices)` (:87): CONSUMER belt, O(trades) —
  length match, each idx in range, `bars[idx].timestamp == trade.exit_ts`. False ⇒ caller
  DEMOTES to the dict path (bit-identical slow arm), NEVER adopts the stream, NEVER falls to
  the Python oracle.
- Producer side (rust_trade_sim.py:546-712): `simulate_masks_with_exit_indices(masks, ...)`
  returns `list[(list[ClosedTrade], list[int]|None)]` positionally aligned with masks.
  Indices are emitted only under a DOUBLE certificate: window `_ts_certified` AND the mask's
  stream `_monotonic`; a non-monotonic stream on a certified window = kernel ordering
  regression → WARN once (`exit_indices_uncertified`) + `None` (dict path serves). Kernel
  envelope: u64 masks → u64 kernel; >64-bit → lazy WHOLE-BATCH promotion to the u128 sibling
  kernel (`_promote_wide_kernel`, reasons `mask_beyond_u128`/`wheel_missing_u128`/
  `bar_bits_beyond_u128`); pure-Python `TradeSimWalker` fallback emits TRUE walker indices
  under the same window certificate (:654-672).

---

## 7. #1531 Rust-metrics tail — EXACT mechanics

### Flag (`rust_metrics.py`)
- `SWEEP_RUST_METRICS_ENV = "BRUTEX_SWEEP_RUST_METRICS"`; `SWEEP_RUST_METRICS_DEFAULT = True`
  (:143-154, ADR-0017 flip; revert = this one constant; `=0` is the per-process kill switch).
- `sweep_rust_metrics_enabled()` (:202): `env_flag_tristate` verdict — truthy→ON, falsy→OFF,
  unset/empty→DEFAULT(True), **unrecognised non-empty → OFF + warn-once
  `flag_unrecognised`** (a typo'd kill switch like "flase" must still KILL). Read PER CALL
  (~0.1 µs) — deliberately no memo so every sweep_parallel worker reads its OWN env; an env
  flip mid-run takes effect next mask, results bit-identical either way (:22-33, :113).

### Call chain (sweep_driver.py)
`metrics_from_trades(bars, trades, start_equity_paisa, tf_minutes, exit_indices=None)` (:311):
1. `if sweep_rust_metrics_enabled():` → `_metrics_from_trades_via_kernel` (:465); non-None
   result returned. OFF/None → verbatim Python tail: `build_equity_curve` + `compute_metrics`
   (:361-364).
2. `_metrics_from_trades_via_kernel`: returns None (→ Python tail serves, raising its OWN
   exact errors — messages never duplicated) on: empty bars / start_equity≤0;
   `exceeds_exact_equity_envelope` (start + Σ|pnl| > 2^52 `MAX_EXACT_EQUITY_PAISA`,
   rust_metrics.py:171 — exact int arithmetic, O(trades)); span≤0; an exit_ts that is no bar
   timestamp. Zero trades → `FLAT_DRAWDOWN_STATS=(0, 0.0, 0, 0.0)` with NO FFI (66% of
   profiled tail, rust_metrics.py:176-181). Else `exits = _exit_pairs(bars, trades,
   exit_indices)` (:367 — validated fast path O(trades), demote-once `exit_indices_demoted`
   to `build_ts_index` dict, None on unknown exit_ts) → `_self_checked_drawdown(len(bars),
   start_equity_paisa, exits)` → `compute_metrics_from_scalars(trades, len(bars), ...,
   drawdown=stats)` (:532).

### Burn-in self-check (`_self_checked_drawdown`, sweep_driver.py:428)
- `kernel_stats = drawdown_stats_via_kernel(n_bars, start_equity_paisa, exits)` — the ONE FFI
  site (rust_metrics.py:396): latched? → warn-once `kernel_disabled` → None. Resolve
  `fno_math.sweep_drawdown_stats` (missing wheel → `wheel_missing`; stale wheel/AttributeError
  → `wheel_stale`). Narrow catch `(ValueError, TypeError, OverflowError)` → warn-once
  `kernel_error` → None; **MemoryError re-raises BARE** (global stop); anything else
  propagates. `_valid_drawdown` domain gate (arity-4, int/float types, non-negative, finite,
  dd_bars ≤ n_bars, :370-393) — invalid → ERROR `kernel_result_refused` → None. First success
  logs `sweep_rust_metrics.engaged` INFO.
- While `self_check_pending()` (`_SELF_CHECK_REMAINING[0] > 0`, starts at
  `SELF_CHECK_SAMPLES = 16` per PROCESS): also run `_oracle_drawdown` (sweep_driver.py:400 —
  fold exits into a materialised EquityCurve and run the REAL private
  `brutex.metrics.compute._drawdown_stats` scan) and compare
  `drawdown_compare_key(kernel) != drawdown_compare_key(oracle)`
  (rust_metrics.py:281 — ints by `==`, floats by `struct.pack('<d', …)` IEEE bit pattern,
  signed zeros included).
  - MISMATCH → `disable_kernel("self_check_mismatch", …)` — the AUTOREVERT LATCH
    (`_KERNEL_DISABLED_REASON[0]`, one `kernel_autoreverted` ERROR, rust_metrics.py:264-278) —
    and **the ORACLE value ships** (even the exposing mask never persists a kernel number).
    Every later call short-circuits at the latch.
  - MATCH → `consume_self_check_sample()` (clamped at 0) → kernel stats ship.
- Latch + counter are plain unlocked writes (`Final[list[…]]` single-cell) — a racing thread
  at worst runs extra self-checks, never a wrong result (:55-60).
  `reset_self_check_for_tests(remaining=…)` / `reset_observability_memo_for_tests()` restore
  fresh-process views (test-only).
- Golden pin: the default (no-env) arm reproduces the frozen `_GOLDEN_CONTENT_HASH`/
  `_GOLDEN_RUN_ID` byte-for-byte with exactly ONE `engaged` event (per CLAUDE.md #1531 blurb).

---

## 8. The per-mask spine and where a batched Rust kernel call sits

### `_iter_mask_metrics` (sweep_run.py:1021) — THE one chunked eval spine
- `chunk_size = _trade_sim_chunk_masks(len(bars))` = `max(1, min(256, (4<<20)//n_bars))`
  (:195-204) — count cap 256 for small windows, cell budget `4·2^20` masks×bars for deep
  1-minute windows. **Chunk size NEVER changes results** (order-preserving, result-invariant —
  pinned by chunk-invariance tests, :178-192).
- Per chunk: `sim.simulate_masks_with_exit_indices(chunk, direction, sl, tp)`; wrong-length
  return raises INSIDE the chunk try (batch-contract guard :1073-1078) so it rides the same
  ladder; `except MemoryError: raise` bare; any other Exception →
  `_reattribute_chunk_failure` (:954) — re-run the chunk ONE mask at a time through the
  `metrics_for_mask` ORACLE; first re-raiser → `_combo_error` fingerprint
  (`combo 0x%032x (run_id=…) in instr/tf/dir k=…`, :890-901); if EVERY mask passes, raise a
  chunk-level `SweepComboError` chained from the ORIGINAL batch error — **oracle results are
  NEVER adopted** (engine provenance stays deterministic, :976-983).
- Then per mask in chunk: `metrics_from_trades(..., exit_indices=…)`; a tail raise →
  `_combo_error`; yield `(mask, metrics)` in input order.
- Three consumers share this generator verbatim: batch `pairs` accumulation, bounded
  `SurvivorColumns.append` (+ exact `owned_nbytes()` watermark → `SweepMemoryBudgetError`
  mid-flight, :1526-1538), and the bounded finalist recompute `_rebuild_bounded_finalists`
  (:1099 — O(top_k), bit-identical because the batch sim is per-mask independent +
  deterministic; positional zip with an order-contract belt :1133-1145).

### run_persisted_sweep sequencing (the tuple body, :1157)
validate → sentinel short-circuit (digest path) → `plan_rank_memory` (AFTER sentinel, BEFORE
any O(masks) allocation) → SWEEP_START audit → Stage-1A `build_bar_bits` (or resident kernel's
prebuilt matrix; n_bars + fib_anchor guards :1393-1407) → enumerate masks
(`iter_masks_k_of_bits`), conflict-prune, digest path collects BARE masks (run_id lazy via
`_lazy_run_id`; legacy path probes dedup eagerly) → Stage-1B `min_hits` prefilter
(`kernel.count_hits` or `count_supermasks`) → `_build_trade_sim` once per tuple (failure →
tuple-skip SweepComboError; MemoryError bare) → `_iter_mask_metrics` over `kept` →
rank (`rank_combos(pairs)` or `rank_survivor_columns`) → project finalists
`ranked_combo_to_result_row` → `store.append`/`store.flush()` → trial-stats sidecar →
`mark_many` (skipped when bypass_dedup; `n_marked=0` honestly) → **completion sentinel
STRICTLY LAST** → SWEEP_FLUSH + SWEEP_COMPLETE audit (exactly 3 lines per full sweep; 1 for a
resume — O(1) audit cost in mask count).

### Where a Rust per-mask orchestration kernel must sit
- **Below**: the enumeration/conflict-prune/Stage-1B kept-set computation (or replicate it
  bit-exactly), and above nothing that writes — the store append/flush, sidecar, marks and
  sentinel MUST remain a single Python-side epilogue per tuple (they are the resume/idempotence
  contract). A Rust core replacing `_iter_mask_metrics` + rank must preserve:
  (a) input-order yield/rank equivalence; (b) the `SweepComboError` fingerprint format and
  the oracle re-attribution semantics (never adopt oracle results after a batch failure);
  (c) MemoryError-bare-at-every-level; (d) the mid-flight bounded watermark on EXACT owned
  bytes; (e) the burn-in oracle compare + autorevert latch semantics (per PROCESS — under
  sweep_parallel each spawned worker has its own fresh 16-sample budget and latch);
  (f) zero-trade FLAT short-circuit (signed +0.0 floats).
- **Relative to worker boundaries**: one worker process runs one tuple at a time; batching
  masks across TUPLES would cross the checkpoint/audit/sentinel unit and is NOT safe.
  Batching across chunks WITHIN a tuple is safe iff failure attribution and order are kept.
- **Relative to checkpoints**: no checkpoint state exists between masks; killing a tuple
  mid-kernel loses nothing but that tuple's work (sentinel absent → full deterministic redo).
  A Rust kernel may therefore hold arbitrary intra-tuple state without touching resume.

---

## 9. Invariants + failure-mode summary (conversion must not break)

1. Canonical enumeration order everywhere: `iter_sweep_tuples` (checkpoint index space) and
   `iter_masks_k_of_bits` (dedup/rank order). Checkpoints/sentinels are indices into fixed
   orders, not content addresses.
2. Checkpoint = monotonic contiguous-clean-prefix, tuple-granular, frozen after first failure;
   cache still records post-failure successes.
3. Sentinel-authoritative intra-tuple redo: FULL population re-run, dedup bypassed, artifacts
   rewritten byte-identically; sentinel written strictly last.
4. Seq==parallel: Parquet byte-identical; audit content-identical modulo ts (+ `workers` key);
   spawn-only pool; RAYON/OMP/POLARS=1 pins in workers.
5. Bounded memory: in-flight ≤ 2×pool; heap ≤ 2×pool (+in-flight overshoot); stall watchdog
   RAISES (never hangs); rank region budget = env override > RAM×1/6 > 8 GiB floor;
   `SweepMemoryBudgetError` tuple-skip vs genuine MemoryError = bare global stop.
6. Rust-metrics tail: default ON; kill switch `BRUTEX_SWEEP_RUST_METRICS=0`
   (`env_flag_tristate` vocabulary, unrecognised → OFF + warn); 2^52 exactness envelope; 16-mask
   burn-in `struct.pack` bit-compare vs `_drawdown_stats` oracle; mismatch → ship oracle +
   process-wide autorevert latch; all fallbacks land on the bit-identical Python oracle,
   warn-once per reason; MemoryError never swallowed.
7. Exit-index fast path: double certificate (window strictly-increasing + per-mask monotonic
   stream); inconsistent stream demotes to dict, NEVER to the oracle, results identical.
8. Look-ahead guard: OFF = identity object (zero cost); ON = forward-front barrier at the bar
   sequence seam only; complemented by suffix-independence property gates.
9. Engine choice is audit-blind: run_id hashes mask/params/data/code — never which engine
   computed metrics; no audit action/payload field may reveal Rust-vs-Python (path signal is
   structlog only, sweep_run.py:169-172).
