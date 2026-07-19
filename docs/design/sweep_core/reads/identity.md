# Area IDENTITY+RESULTS — run identity, results persistence, audit chain, frozen goldens

Read-only map for the Rust sweep-core conversion (per-mask orchestration loop → PyO3 kernel).
All anchors verified against the working tree at /home/user/bruteX (branch claude/data-edge-guards).

---

## 1. The blake3 run-identity formula

Spec: `docs/TRACK1_BACKTEST_SPEC.md:743` and `:1096` —
`blake3(strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖ canonical_params ‖ data_snapshot_hash ‖ indicator_version ‖ code_commit_hash)`.

### 1.1 combo_hash — the ONE production digest (brutex/strategies/combo.py)
- `UNIT_SEPARATOR = "\x1f"` (`combo.py:104`). Payload = `"\x1f".join([strategy_id, direction, instrument, tf, mode, canonical_params_json, data_snapshot_hash, indicator_version, code_commit_sha])` (`combo.py:273-285`), digest = `fno_math.blake3_hash(payload.encode("utf-8")).hex()` (`combo.py:289`) — the Rust wheel primitive, proven ≡ pip `blake3` by the run-identity golden's 3-route oracle.
- Empty-field rejection for all 8 string fields (`combo.py:240-255`); `reject_unit_separator("combo_hash", fields, error=StrategyError)` screens all 8 raw strings BEFORE any digest byte (`combo.py:257-270`). `reject_unit_separator` (`combo.py:107-143`) is the ONE COMMON reject-only guard (DEC-RUN-IDENTITY-GUARD-PARITY-001); the caller injects its own error class.
- `canonicalise_params` (`combo.py:146-169`): `json.dumps(_canonicalise_value(params), sort_keys=True, separators=(",", ":"), ensure_ascii=True)`. **Note ensure_ascii=True here** (differs from audit/sweep_cache which use `ensure_ascii=False` — see §5).
- `_canonicalise_value` (`combo.py:297-359`): depth cap 64 (`_MAX_CANONICALISE_DEPTH`, `combo.py:97`); bool/None/int pass through; **float → `repr(value)` STRING token** after rejecting float subclasses (numpy.float64) and non-finite, with **signed-zero normalised `-0.0 → +0.0` via `value = value + 0.0` BEFORE repr** (`combo.py:331`); Decimal → `str(value)` (scale-significant: `Decimal("1.0") != Decimal("1.00")`); non-str dict keys rejected; list/tuple/range → JSON array. **GOTCHA:** the module docstring's "Honest envelope" item 3 (`combo.py:66-68`) still says signed zero is SPLIT — that is STALE; the code (line 331 + its comment) merges `-0.0` into `"0.0"`. A Rust port must replicate the CODE.
- Wire-locked aliasing (frozen, do NOT "fix", `combo.py:52-69`): `1.5 ≡ "1.5" ≡ Decimal("1.5")` (same token); tuple/list/range all → array.

### 1.2 mask_run_id (brutex/engine/sweep_run.py:368-426)
- `strategy_id = f"mask:{mask:032x}"` — **u128, 32 lowercase zero-padded hex** (DEC-MASK-IDENTITY-001; was `%016x` pre-u128 — that widening moved `_GOLDEN_RUN_ID` by design). All other 8 fields forwarded verbatim to `combo_hash`.

### 1.3 canonical_trade_params (sweep_run.py:626-675) — the ONE params layout
Returns a flat dict of ints/strings/None only (no floats ever enter the sweep run-identity):
```
{"sl_paisa": int|None, "tp_paisa": int|None, "prior_n": int,
 "proximity_bps": [pivot,pdh_pdl,fib] | None, "fib_anchor": <enum .value str, UNCONDITIONAL>,
 "start_equity_paisa": int}
```
Canonical JSON key order (sorted): `fib_anchor, prior_n, proximity_bps, sl_paisa, start_equity_paisa, tp_paisa`. Stage-3's `recover_survivor_cell` (`stage3_wire.py:324-394`) re-derives run_ids over the whole `sl×tp×proximity×fib_anchor` grid and matches `row.run_id` — so a Rust kernel that changes ONE byte of this layout breaks cell recovery, not just goldens.

### 1.4 data_snapshot_hash (brutex/engine/stage3_wire.py:171-234)
- Streamed blake3 (pip `blake3` package here, not fno_math), preimage version `bxsnap2`:
  - `hasher.update(f"bxsnap2|{exchange}|{segment}|{start}|{end}".encode())` (`:223`)
  - per `sorted(slices.items())` on `(symbol, interval)`: `f"\n{symbol}|{interval}|{len(bars)}"` (`:225`)
  - per bar: `f"\n{bar.timestamp.isoformat()}|{bar.open}|{bar.high}|{bar.low}|{bar.close}|{bar.volume}|{bar.open_interest}"` (`:226-230`) — Decimal `__str__` exact rendering (2dp lake snap), `None` OI renders literal `"None"`, timestamps are UTC-aware `datetime.isoformat()`.
- O(total bars), computed ONCE per launch/Stage-3 pass — never in a per-combo hot path.
- Retired v1 sampled derivation kept private as `_data_snapshot_hash_v1` (`:237-266`) — recount diagnostic ONLY, never identity.
- `Catalog.data_snapshot_hash` (FR-CAT-004) is a DIFFERENT file-bytes lake-provenance hash, never run identity (spec `TRACK1_BACKTEST_SPEC.md:1102-1110`).
- Frozen pin: `tests/golden/test_snapshot_hash_golden.py:29` — `_EXPECTED_V2_HEX = "dd776d5204b72c8857b029cd207a357142454c55cc830850336a8596afb05f8b"` over a fixed 3-bar NIFTY fixture. Regenerating = preimage-version change requiring a new DEC id.

### 1.5 The three engine run-identity mirrors (deliberately PARALLEL, not shared)
All use `fno_math.blake3_hash` + canonical-JSON kwargs `sort_keys=True, separators=(",",":"), ensure_ascii=True, allow_nan=False` + (where strings join raw) the `\x1f` join + `reject_unit_separator`:
1. `stage3_refine.refinement_run_id` — payload `"stage3-refine-v1" ‖ mask_run_id ‖ "fevals=..;restarts=..;sigma0=.." ‖ "seed=N" ‖ "cma=X" ‖ "numpy=Y"` (\x1f-joined; library versions are the LITERAL strings passed in, never live `__version__` at hash time).
2. `sweep_driver._walk_forward_run_hash` (`sweep_driver.py:977-1040`) — payload `"walk_forward" ‖ schedule_hash ‖ canonical-JSON{mask,direction:.name("BUY"/"SELL"),tf_minutes,stop_loss_paisa,take_profit_paisa,start_equity_paisa,pivot/pdh_pdl/fib_proximity_bps,prior_n}`; `proximity=None` deliberately ALIASES `ProximityParams()` defaults (`:1020`, golden-pinned `test_engine_run_identity_golden.py:260-269`); only `schedule_hash` is screened by `reject_unit_separator` (`:1015-1019`).
3. `stage1a._walk_forward_schedule_hash` (`stage1a.py:936-985`) — single JSON doc, no \x1f join: `{"v":1,"train_days","test_days","step_days","n_trading_days","n_bars","folds":[[train_start_bar,train_end_bar,test_start_bar,test_end_bar],…]}` (day bounds deliberately NOT hashed).
- Consolidation onto `canonicalise_params` was REJECTED (DEC-RUN-IDENTITY-GUARD-PARITY-001, `sweep_driver.py:997-1002`) — a Rust port must keep them byte-parallel, not merge them.

### 1.6 allow_nan=False census (brutex/, exact sites)
`orchestrator/audit.py:203` (canonical audit JSON, ensure_ascii=False) · `engine/tuple_complete.py:171` (sentinel bytes) · `engine/sweep_heal.py:309` · `engine/trial_stats.py:397` (sidecar bytes) · `engine/cascade_wire.py:1015,1405` · `engine/sweep_cache.py:399` (combo_digest, ensure_ascii=False) · `engine/stage1a.py:983` · `engine/sweep_driver.py:1037`. `strategies/combo.py` rejects non-finite in `_canonicalise_value` itself (equivalent guard). NOTE: `sweep_orchestrator._tuple_digest`'s json.dumps (`sweep_orchestrator.py:704`) uses DEFAULT allow_nan (payload is typed ints/strings) — do not "fix" its bytes.

### 1.7 combo_digest — the dedup-key sibling (engine/sweep_cache.py:288-406)
32 RAW bytes of `blake3(canonical_json({"mask":int,"instrument","tf","direction","params":merged}))` with the three repro hashes injected INTO params under keys `data_snapshot_hash/indicator_version/code_commit_sha` (structural kw-only args; reserved-key overlap in caller params → SweepCacheError). `ensure_ascii=False`, `allow_nan=False`. Byte-identical to the legacy convention — dedup stores not invalidated (golden-pinned in tests/unit/test_engine_sweep_cache.py).

### 1.8 Production identity-field sources (why goldens don't move per commit)
- `cli._resolve_code_commit_sha()` (`cli.py:5124-5147`): `git rev-parse HEAD`, fallback `f"brutex-{__version__}"`.
- `cli._indicator_version()` (`cli.py:5150-5157`): `f"brutex-{__version__}+trading_core-{trading_core.__version__}"` (crate bump 0.3.0→0.3.1 moved it — #1546-#1550; NO math change, new runs re-key by design).
- **How goldens pin bytes despite code_commit_hash in run_id:** the identity fields are pure INJECTED PARAMETERS (`SweepRunConfig.data_snapshot_hash/indicator_version/code_commit_sha`, and combo_hash kwargs) — there is NO ambient/global read inside any hash function. Every golden passes fixed literals (`"snap0001"`, `"ind-v1"`, `"deadbeef"` in `test_engine_sweep_run_golden.py:55-63`; `"ab"*32` / `"cd"*32` fixtures in `test_engine_run_identity_golden.py:51-54`; literal `"4.0.0"`/`"2.1.0"` version strings), so the digests are functions of constants and never see the live git SHA. Production run_ids DO move per commit — that is the intended re-key. A Rust kernel MUST keep the fields injected (never read git/env/version itself).

---

## 2. run_persisted_sweep — the per-mask orchestration loop (sweep_run.py:1157-1670)

Exact call order (the thing being ported):
1. `bars` non-empty check; `maybe_guard_bars(bars)` (opt-in lookahead barrier — OFF returns the SAME object identity, `:1296`); `_direction_label` (BUY→"bull", SELL→"bear", HOLD→ValueError, `:237-261`).
2. `count_combos(k, n_unique)` vs `cfg.max_combos` (raise BEFORE Stage-1A build).
3. `params = _canonical_params(cfg)`; `subject = f"sweep:{instrument}:{tf}:{direction}:k={k}"`.
4. **Sentinel resume** (digest path only, `trial_stats_digest is not None`): `_sentinel_resume(store.path, digest)` (`:840-884`) — PAIRED iff sentinel exists, parses, `tuple_digest` matches, `sha256(store bytes) == sentinel.store_content_hash`, and (if `sidecar_written`) sidecar reads + pairs. Paired → ONE `SWEEP_RESUMED` audit line, return summary with `resumed_complete=True`, zero tallies, `store_content_hash` from sentinel. Unpaired → full redo.
5. `plan_rank_memory(...)` (bounded-rank decision, AFTER sentinel, BEFORE any O(masks) alloc); refusal → `SweepMemoryBudgetError` (a SweepComboError subclass → grid tuple-skip, never a raw MemoryError).
6. Audit `SWEEP_START` (payload includes the 3 repro hashes + params dict).
7. `bar_bits = build_bar_bits(bars, proximity, prior_n, fib_anchor)` ONCE — or `kernel.bar_bits` if a `ResidentSweepKernel` was injected (guards: `kernel.n_bars == len(bars)` and `kernel.fib_anchor is cfg.fib_anchor`, both ValueError).
8. Enumeration `iter_masks_k_of_bits(condition_bits, k)`: `enumerated+=1`; `has_conflict(mask)` → `conflict_pruned+=1, continue`; then **two arms**:
   - digest path (`bypass_dedup=True`): collect bare mask — **NO per-mask blake3** (probes bypassed, sentinel-authoritative redo per DEC-MARK-MANY-SENTINEL-001; `skipped_dedup==0` by construction).
   - legacy path: eager `_lazy_run_id(mask)`; `dedup.seen(run_id)` (ComboDedup → OptionDedupIndex bloom→exact over `bytes.fromhex`, `:296-365`) → `skipped_dedup+=1` or append to `survivors` + `survivor_run_ids`.
9. Stage-1B: `min_hits>0` → `kernel.count_hits(survivors)` or `count_supermasks(bar_bits, survivors)`; `kept = [m for m,h in zip if h>=min_hits]`. `evaluated = len(survivors)` (min-hits drops still count evaluated).
10. `run_id_of`: lazy `mask_run_id` closure on digest path; dict lookup of probe-time values on legacy path — byte-identical because `mask_run_id` is pure.
11. Eval spine `_iter_mask_metrics` (`:1021-1096`): ONE `ResidentTradeSim` per tuple (`_build_trade_sim`, `:904-951` — logs `sweep_run.trade_sim_path` with `rust_active`; construction failure → SweepComboError; MemoryError bare); chunk size `_trade_sim_chunk_masks(len(bars))`; per chunk `sim.simulate_masks_with_exit_indices(chunk, direction, sl, tp)` → list of `(trades, exit_indices)` pairs (length must equal chunk — else ValueError inside the try → same ladder); chunk failure → `_reattribute_chunk_failure` (`:954-1018`): replay each mask through the Python `metrics_for_mask` ORACLE, first raiser gets `_combo_error` fingerprint (`combo 0x{mask:032x} (run_id=…) in {inst}/{tf}/{dir} k={k}: {exc}`, `:887-901`); if all pass → loud chunk-level SweepComboError, **oracle results NEVER adopted** (engine provenance determinism). Per-mask tail `metrics_from_trades(bars, trades, start_equity_paisa, tf_minutes, exit_indices=…)` — exit_indices thread the DEC-SWEEP-EXIT-INDEX-FASTPATH-001 O(trades) path (`None`/inconsistent → demote to the ts→index dict, bytes identical). MemoryError re-raises BARE at every level (global stop).
12. Rank: batch arm `rank_combos(pairs, overfit)` then `[:top_k]`; bounded arm `SurvivorColumns.append` per mask + owned-bytes watermark (`> plan.budget_bytes` → SweepMemoryBudgetError) then `rank_survivor_columns` + `_rebuild_bounded_finalists` (`:1099-1154`, recompute ≤top_k full metrics through the SAME spine; positional pairing + mask-equality belt) — pinned bit-identical to batch.
13. Persist: per finalist `ranked_combo_to_result_row(rc, run_id=run_id_of(rc.mask), …)` → `store.append(row)`.
14. `flush = store.flush()`; **then** trial-stats sidecar (digest path; `tuple_trial_stats_from_sharpes(sidecar_sharpes, n_enumerated, n_conflict_pruned, n_minhits_dropped=evaluated-len(kept), tuple_digest, store_content_hash=flush.content_hash)` → `write_tuple_trial_stats`); **then** `marked = 0 if bypass_dedup else dedup.mark_many(survivor_run_ids)` (mark AFTER flush = redo-not-skip; skipped entirely on digest path); **then** completion sentinel STRICTLY LAST (`write_completion_sentinel`).
15. Audit `SWEEP_FLUSH` (tallies + store_content_hash) then `SWEEP_COMPLETE` (tallies). Exactly **3 audit lines per full sweep** regardless of combo count (Golden Rule #8, O(1) audit); 1 line on resume.

`SweepRunSummary` fields (`:563+`): enumerated, conflict_pruned, skipped_dedup, evaluated, ranked, persisted, store_content_hash, backtested, minhits_dropped, trial_n_finite, trial_sum_sharpe, trial_sum_sharpe_sq, trial_sidecar_written, resumed_complete, sentinel_written.

### 2.1 ranked_combo_to_result_row (sweep_run.py:690-826)
- Re-simulates the finalist's trades via the **pure-Python `simulate_trades` oracle** (deliberate non-goal to Rust-ify — runs once per finalist; the kernel↔oracle bit-identity proof makes the cross-engine contract hold; these price anchors are part of the hashed store bytes).
- Price anchors: keep-FIRST ts map `bars_by_ts_first` (dup-ts poison guard); `avg_entry = round_money(mean of entry-bar OPENs)`, `avg_exit = round_money(mean of exit-bar CLOSEs)` — Decimal arithmetic, ROUND_HALF_UP 2dp. Zero trades (Stage-3 ungated evals) → `round_money(Decimal("0"))` pair.
- `None` metrics (sortino/calmar/profit_factor) → `0.0` via `_metric_or_zero`; real values full precision.

---

## 3. ResultsStore (engine/results_store.py)

- `ResultRow` (`:176-347`): frozen dataclass; validation: run_id exactly 64 lowercase hex (`_validate_run_id:623`); non-empty context labels; `0 <= mask_bits <= MASK_BITS_MAX = (1<<TOTAL_CONDITIONS)-1` with TOTAL_CONDITIONS=60 (`:102`); money/counts int64-range, magnitudes ≥0, `winning+losing <= total`; metrics finite with **`-0.0 → +0.0` normalisation via `value + 0.0`** (`:327,338` — byte-determinism); nullable `deflated_sharpe` (None ↔ Parquet null; TUPLE-scoped `dsr@tupleN`, superseded by the cascade); prices finite ≥0 snapped `round_money` at construction. `condition_names` is `init=False`, DERIVED from mask_bits via `matched_condition_names` joined by `" AND "` — never read back from disk.
- `append` (`:404-442`): O(1) dict insert; identical re-append → False (idempotent no-op); **same run_id + different fields → ResultsStoreError** (`results_store.conflict` log; a collision = upstream non-determinism, never overwritten). This conflict raise is the reproducibility tripwire a Rust kernel would hit if it ever produces different metrics for the same identity.
- `rows()`: run_id-sorted tuple. `top_k(k, by="sharpe_ratio", descending=True)` (`:456-491`): by ∈ `_RANKABLE_FIELDS` (metrics+money+counts); **stable two-pass sort — sort by run_id ASC first, then stable-sort by the ranking column** — ties break run_id-ascending, insertion-order-independent; int fields rank exactly (no float coercion, paisa > 2^53 safe). The dashboard `/results` leaderboard is golden-pinned against `ResultsStore.top_k` order (MIGRATION #1533).
- `flush` (`:493-521`) → `_render_bytes` (`:560-611`): rows sorted by run_id; column insertion order = `_STRING_FIELDS`(run_id,instrument,tf,direction,mode,condition_names) → `mask_bits_lo`,`mask_bits_hi` → money → counts → metrics → deflated_sharpe → prices; pinned polars schema (Utf8/Int64/Float64); parquet `compression="zstd", compression_level=3, row_group_size=100_000, use_pyarrow=False, statistics=False`; atomic tmp→fsync→`os.replace` (`atomic_write_simple`, guard-free — raw ENOSPC propagates, no LakeWriteHalt). `FlushResult.content_hash = sha256(file bytes)` hex — this is `store_content_hash` everywhere (sentinel, sidecar, audit, goldens). **Note the store hash is sha256, not blake3.**
- u128 mask on disk: two signed Int64 limbs, `_encode_limb = limb - 2**63` (`:656-665`), `_decode_mask = ((hi+2**63)<<64)|(lo+2**63)` (`:668-676`).
- `load` (`:523-556`): read back through `_row_from_record` — prices `Decimal(str(float))` + construction snap (bit-stable round trip); `load_or_create_store` (`sweep_orchestrator.py:737+`) pre-loads existing Parquet so a full-skip re-run re-flushes byte-identically.

### 3.1 Tuple addressing
- `_tuple_digest` (`sweep_orchestrator.py:664-708`): canonical JSON descriptor {instrument, tf(label+minutes), direction, k, sorted condition_bits, sl/tp, prior_n, proximity_bps, fib_anchor, start_equity_paisa, mode, **data_snapshot_hash, indicator_version, code_commit_sha**} → pip-blake3 hexdigest **truncated to `_PATH_DIGEST_LEN` (16 hex)**. `tuple_store_path` = `<root>/<instrument>/<tf>/<direction>_k<k>_<digest16>.parquet`.
- Sidecar `<store>.trials.json` (`trial_stats.py:367-374`), sentinel `<store>.complete.json` (`tuple_complete.py:146-153`) — both deterministic bytes: `json.dumps(payload, sort_keys=True, separators=(",",":"), allow_nan=False)` with schema_version keys (`trial_stats.py:377-399`; `tuple_complete.py:156-173`). Sentinel fields: tuple_digest(16 hex), store_content_hash(64 hex), sidecar_written(bool), n_marked(≥0).

---

## 4. Audit JSONL hash chain (orchestrator/audit.py)

- Entry fields: seq, prev_hash, ts_utc(float), actor(default "brutex"), action, subject, payload, entry_hash.
- `entry_hash = sha256(str(seq) | prev_hash | str(ts_utc) | actor | action | subject | canonical_json(payload))` with literal `b"|"` joins (`:264-284`). **SHA-256, not blake3.** `str(ts_utc)` = Python float `str()` (repr) — a Rust re-implementation must reproduce Python float formatting exactly or verify_chain breaks.
- `_canonical_json` (`:176-208`): `sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False` — ValueError → AuditPayloadError, refused BEFORE any write.
- `|` forbidden in actor/action/subject (`AuditFieldError`, `__post_init__:239-261`) on write AND read paths — preimage injectivity.
- Genesis `prev_hash = "0"*64`; chain law `prev_hash(n) == entry_hash(n-1)`, seq starts 0 and increments by 1.
- `AuditWriter.append` (`:487-571`): NOT thread-safe (single-writer law); flush + fsync per entry (DEC-AUDIT-FSYNC-DURABILITY-001, fsync OSError suppressed best-effort); chain state advanced only after a successful write. `open(resume=True)` tail-reads the last committed line O(1) and truncates a torn tail (`:412-445`); `resume=False` on an existing file raises.
- `verify_chain(path) -> int` (`:1133+`): streaming O(1)-memory re-verification (seq order, linkage, recomputed hash); mismatch → `AuditChainCorruption(seq, expected, actual, reason)`.
- Sweep audit actions: `SWEEP_START`/`SWEEP_FLUSH`/`SWEEP_COMPLETE`/`SWEEP_RESUMED` (sweep_run `_ACTION_*`); Stage-3 brackets `STAGE_3_PASS_START`/`STAGE_3_PASS_COMPLETE` (`stage3_wire.py:127-128`) around per-mask `STAGE_3_REFINE_*` triples on the SAME chain via `_WriterAuditSink` (`stage3_wire.py:417-431`). Parallel workers use `BufferedAuditCollector` (AuditSink seam).
- New payload keys are chain-safe (per-line hash), so a Rust kernel may ADD tallies but must not rename/remove existing ones (tests pin payload contents).

---

## 5. Canonical-JSON kwargs matrix (drift here = broken digests)

| Site | sort_keys | separators | ensure_ascii | allow_nan | hash |
|---|---|---|---|---|---|
| combo.canonicalise_params | ✓ | (",",":") | **True** | (rejects in canonicaliser) | fno_math blake3, \x1f join |
| audit._canonical_json | ✓ | (",",":") | **False** | False | sha256, \| join |
| sweep_cache.combo_digest | ✓ | (",",":") | **False** | False | pip blake3, raw 32 bytes |
| stage1a schedule hash | ✓ | (",",":") | True | False | fno_math blake3 |
| sweep_driver WF run hash | ✓ | (",",":") | True | False | fno_math blake3, \x1f join |
| sweep_orchestrator._tuple_digest | ✓ | (",",":") | default(True) | default(True) | pip blake3 → [:16] |
| trial_stats / tuple_complete / sweep_heal / cascade_wire renders | ✓ | (",",":") | default(True) | False | bytes on disk (sha256'd indirectly via store pairing) |

---

## 6. Frozen goldens the conversion MUST NOT move

1. **`tests/golden/test_engine_sweep_run_golden.py`**
   - `_GOLDEN_RUN_ID = "f588194f3f107bec68ee48975171041dd77c8f5ceec7e0625534b352d2d5921a"` (`:142`) — `mask_run_id(mask=0b101, params=_PARAMS, **_CTX)` with `_CTX` literals (`snap0001`/`ind-v1`/`deadbeef`) and `_PARAMS` incl. `"fib_anchor": "prev_day"`.
   - `_GOLDEN_CONTENT_HASH = "2387cc884944768ee271fad117edb953f538d4d6d2d3b3c1c4b9ca3f4bd4d147"` (`:170`) — sha256 of the flushed store for the fixed 12-day synthetic fixture, k=1 over bits 0-4, sl=80/tp=50. **Generated on the PURE-PYTHON per-mask loop pre-wiring**; the wired-Rust-trade-sim run, the default-ON Rust metrics tail (ADR-0017 / #1531-#1532), and the forced-Python fallback ALL must reproduce it byte-for-byte (`:195,240,251-252,323,349-350,389,450`), each arm ALSO asserting genuine kernel engagement (`rust_active is True` / exactly one `sweep_rust_metrics.engaged` event — the anti-vacuous-gate teeth) and zero `kernel_autoreverted`. Burn-in poison tests: domain-valid poison past burn-in MOVES the hash (`:418`, proves the kernel output is truly consumed); the same poison DURING burn-in autoreverted → pinned bytes (`:450`). A ported Rust sweep-core must preserve: engagement observability events, the 16-mask burn-in bit-compare vs the Python `_drawdown_stats` oracle (struct.pack float compare, signed zeros), autorevert latch + `kernel_disabled` short-circuit, and byte-identity of the store.
   - Sensitive to polars/zstd builds (documented caveat) — determinism + summary assertions are the version-robust layer.
2. **`tests/golden/test_engine_run_identity_golden.py`** (#1546, DEC-RUN-IDENTITY-GUARD-PARITY-001) — 10 pairwise-distinct frozen digests for `refinement_run_id` (4), `_walk_forward_run_hash` (4: 8247d678…, 024a796e…, fe81367f…, fc45f2ef…), `_walk_forward_schedule_hash` (2: b7992a6c…, ca5b1b16…); each asserted via 3 independent routes (production fn / fno_math over hand-assembled payload bytes / pip blake3 over the same bytes) — pins the PAYLOAD BYTES, not just digests. Landed BEFORE the guards so the guards had to pass unchanged; the None-proximity≡defaults alias is separately pinned.
3. **`tests/golden/fixtures/vocab_golden_vectors.json`** — the frozen 400-case cross-language answer key (keys: cases[400], implemented_mask, full_mask, seed, version, bits_set/clear_somewhere; each case = paisa inputs + market_structure bools + proximity + prior flags + `expected_mask` as a **32-hex u128 string**). Consumed by `tests/golden/test_vocab_golden.py` (Python `evaluate_atoms` bit-for-bit; both-ways coverage over IMPLEMENTED_MASK; layout-only bits always clear) AND `rust/trading-core/tests/golden_vectors.rs` — the DEC-SHARED-CORE-001 contract both BruteX and TickVault satisfy. Fixture byte-untouched is a standing PR requirement (`git diff origin/main -- tests/golden/fixtures/` empty — the #1560 precedent).
4. **`tests/golden/test_snapshot_hash_golden.py`** — bxsnap2 preimage pin (dd776d52…).
5. **`tests/golden/test_combo_hash_distinct_golden.py`** — corpus #9: base combo + 9 single-field variants, 10 distinct frozen hex digests (P-01 determinism, P-02 per-field sensitivity, params order-invariance).
6. Related but adjacent: `test_engine_results_store_golden.py` (store byte determinism), `test_engine_combo_digest_golden.py` (dedup key), `test_trade_sim_rust_golden.py`, `test_engine_stage1a_golden.py`, `test_engine_sweep_orchestrator_golden.py`.

---

## 7. Gotchas for the Rust sweep-core conversion

1. **Keep identity fields injected.** No git/env/version reads inside the kernel; `code_commit_sha`/`indicator_version`/`data_snapshot_hash` arrive as strings from `SweepRunConfig`/grid. Goldens pin literals exactly because of this.
2. **`mask:%032x`** — lowercase, exactly 32 hex, u128. A `%016x` or uppercase or unpadded encoding silently re-keys everything.
3. **Params JSON bytes** — sorted keys, `(",",":")`, `ensure_ascii=True`, the 6 fixed keys of `canonical_trade_params` incl. UNCONDITIONAL `fib_anchor`; `None → null`; the sweep params are all int/str/None so the float-repr path never fires in the sweep loop — but `combo_hash` in general uses Python `repr(float)` tokens with `-0.0→0.0` pre-normalisation; Rust must match CPython repr (shortest round-trip) if it ever canonicalises floats.
4. **Two dedup arms with different hashing volume.** Digest path = ZERO per-mask blake3 (lazy `run_id_of` only for finalists + error fingerprints); legacy path = eager per-enumerated-mask blake3 + `seen` probe + `mark_many` AFTER flush. Do not merge the arms; `skipped_dedup==0` on digest path is structural (sidecar full-population law, ADR-0008 cond 2).
5. **Ordering laws:** flush → sidecar → mark_many → sentinel STRICTLY LAST; audit lines exactly START/FLUSH/COMPLETE (+RESUMED alone); `_iter_mask_metrics` preserves input mask order (finalist recompute pairs positionally with a mask-equality belt).
6. **Error taxonomy:** per-mask failure → `SweepComboError` with the exact `_combo_error` fingerprint format (grid TUPLE_FAILED audit lines format-pinned); chunk failure → oracle re-attribution, oracle results NEVER adopted; `MemoryError` bare at EVERY level (global stop); budget breach → `SweepMemoryBudgetError` naming knobs. KeyboardInterrupt/BaseException must escape untouched.
7. **Store rendering stays byte-pinned:** run_id-sorted rows, fixed column order, polars zstd-3/rowgroup-100k/no-stats/no-pyarrow, sha256 content hash, `-0.0→+0.0` on every float metric, `None→0.0` for the 3 nullable ratios (but `deflated_sharpe` stays nullable), Decimal 2dp ROUND_HALF_UP prices from the PYTHON oracle re-simulation with keep-FIRST duplicate-ts map. Cheapest safe split: keep row projection + store + audit in Python; port only the enumerate→conflict→(dedup)→minhits→simulate→metrics→rank pipeline, returning finalists (mask, metrics, composite, dsr) in deterministic order.
8. **Burn-in / autorevert semantics** (rust_metrics): first-16-mask bit-compare vs Python oracle via `struct.pack` (signed zeros significant), mismatch ships the ORACLE value + latches kernel off for the process (`kernel_autoreverted` ERROR + `kernel_disabled`), `BRUTEX_SWEEP_RUST_METRICS` tristate kill switch. Goldens assert the exact event names and one-engaged-event counts.
9. **ResultsStore.append conflict raise** is the last-line tripwire: any nondeterminism in a ported kernel (same run_id, different metrics — e.g. float summation order changes) surfaces as `ResultsStoreError` naming differing fields, or as the store-hash golden moving. Sharpe sums for sidecars use `math.fsum` in merges (`trial_stats.py:361-362`) — summation-order sensitivity is real.
10. **Stale docstring trap:** combo.py honest-envelope item 3 (signed-zero "split") contradicts the implemented merge at `combo.py:331`; the code + goldens are authoritative.
11. **`reject_unit_separator` guard parity** must survive the port: every raw string joined by `\x1f` is screened; reject-only (no legal digest changes). Engine mirrors keep their OWN error classes (ValueError / Stage3RefineError / StrategyError).
12. **top_k tie-break** is run_id-ASC via a stable two-pass sort; a Rust re-rank must use a stable sort keyed identically or leaderboard order (golden-pinned, #1533 order parity) drifts.
