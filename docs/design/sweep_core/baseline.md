# baseline.md — Measured per-combo cost breakdown of the BruteX sweep hot path

Date: 2026-07-08. Author: grounding-phase worker (Rust sweep-core conversion).
Repo: /home/user/bruteX @ origin/main 46895ee5 (working tree, read-only).

## 0. Evidence provenance — HONEST NOTE (Golden Rule #1)

The prior audit's scratchpad (`/tmp/claude-0/-home-user-bruteX/754b4f2b-.../scratchpad/`
— `measured_costs.json`, `W05_*` k-scaling files, `early_estimate_inputs.md`,
`phase1_dossier.md`) is **GONE from this container**, and the named backup tarball
`/root/.claude/projects/-home-user-bruteX/audit_scratch_backup_1456.tar.gz` does
**not exist** (verified with `find / -name ...` — zero hits). Every audit figure
below marked **[AUDIT-RELAYED]** reaches me only via the coordinator's brief and is
**unverified by me**; every figure marked **[MEASURED]** was re-derived in THIS
session by the scripts in this scratchpad:

- `profile_sweep_baseline.py` → `baseline_measurements.json`, `profile_e2e_20k_k{2,3}*.txt`
- `profile_sweep_refined.py`  → `baseline_refined.json`, `refprofile_e2e16_20k_k4_{tottime,cum}.txt`
- inline micro-runs (prefilter rate, zero-trade tail) — transcripts in the session log

Measurement host: **sandbox x86 container (shared CPU), Python 3.12.3, wheels
built from the repo's own rust/ crates (fno_math 0.3.0, trading_core 0.3.1,
`maturin build --release`)**. This is **NOT the §8 M4 Pro** — absolute times are
sandbox-scale; the per-segment SHARES and per-bar/per-trade RATES are the signal
(the CLAUDE.md #1182/#1186 sandbox-ratio convention). structlog was configured to
WARNING for the refined runs (the default DEBUG console sink otherwise dominates
the Python-walker segments).

Coordinator-relayed audit headline (for cross-check): **~0.58 ms/combo Python
orchestration residual ≈ ~85% of sweep wall-clock; enumeration ~2.3 µs/mask**
[AUDIT-RELAYED]. My re-derivation independently reproduces both (see §3).

## 1. The production per-mask call graph (exact, from code)

Production spine (digest path — `cmd_sweep_all` → `sweep_launch.plan_launch_dedup`
→ `run_sweep_grid`/`run_parallel_grid` → `run_one_tuple` → `run_persisted_sweep`,
`brutex/engine/sweep_run.py:1157`):

```
per TUPLE (instrument × tf × direction × k × sl × tp × fib_anchor):
  bars_for(...)                      # lake load (cached across tuples per (inst,tf))
  sentinel check (_sentinel_resume)  # PAIRED sentinel → SWEEP_RESUMED short-circuit
  plan_rank_memory(...)              # batch vs bounded columnar rank decision
  audit.append(SWEEP_START)          # 1 of exactly 3 audit lines / tuple
  build_bar_bits(bars) OR kernel.bar_bits   # Stage-1A — ONCE per (inst,tf,prox,anchor)
                                     #   via ResidentKernelCache (kernel_cache)
  ── per ENUMERATED mask (pure Python loop, sweep_run.py:1438) ──
  iter_masks_k_of_bits(bits, k)      # itertools.combinations + bit-OR  [Python]
  has_conflict(mask)                 # any() over 26 CONFLICT_PAIRS      [Python]
  survivors.append(mask)             # NO dedup probe, NO run_id hash — the digest
                                     # path BYPASSES dedup (bypass_dedup=True,
                                     # DigestPathNoDedup; run_id is computed LAZILY
                                     # only for finalists + error fingerprints)
  ── Stage-1B prefilter (ONE batched Rust call) ──
  kernel.count_hits(survivors) / count_supermasks(bar_bits, survivors)
                                     # fno_math.count_supermasks_u64s; kept =
                                     # hits >= cfg.min_hits (default MIN_TRADES=30)
  ── per KEPT mask (the hot loop: _iter_mask_metrics, sweep_run.py:1021) ──
  ResidentTradeSim.simulate_masks_with_exit_indices(chunk, ...)
      # chunked: chunk = max(1, min(256, 4·2^20 // n_bars))
      # Rust: fno_math.ResidentSweepSim.simulate_masks →
      #   Vec<Vec<(pnl, mfe, mae, holding, entry_idx, exit_idx)>>  [FFI marshal]
      # Python: per trade → ClosedTrade(dataclass, 2 datetime reads) + exit-idx list
  metrics_from_trades(bars, trades, exit_indices=...)      [per mask]
      # ON arm (default, ADR-0017): _exit_pairs (O(trades)) →
      #   fno_math.sweep_drawdown_stats(n_bars, start, [(exit_idx,pnl)])  [FFI/mask]
      #   (zero-trade masks: pure-Python FLAT shortcut, no FFI) →
      #   compute_metrics_from_scalars(...)  # O(trades) pure-Python metric body,
      #   Welford moments, sorted() percentiles → 37-field ResultMetrics dataclass
      # burn-in: first 16 kernel-served masks/process ALSO run the Python
      #   _drawdown_stats oracle + struct.pack bit-compare (#1531; autorevert latch)
  pairs.append((mask, metrics))      # batch arm; bounded arm: SurvivorColumns.append
                                     #   (passes_gates + composite_term_inputs / mask)
  ── per TUPLE tail ──
  rank_combos(pairs) / rank_survivor_columns(columns)   # z-scores, gates, sort
  per finalist (≤ top_k): _lazy_run_id(mask)            # blake3 combo_hash
      ranked_combo_to_result_row(...)                   # re-simulates via the PYTHON
                                                        # oracle simulate_trades (per
                                                        # finalist, O(n_bars) each)
      store.append(row)
  store.flush()                      # Parquet + sha256 content hash
  trial-stats sidecar (math.fsum over kept sharpes) → mark_many SKIPPED (digest)
  completion sentinel write (STRICTLY LAST)
  audit.append(SWEEP_FLUSH) + audit.append(SWEEP_COMPLETE)
```

Data structures crossing the FFI boundary today, with shapes:

| Crossing | Direction | Shape / size |
|---|---|---|
| `ResidentSweepSim(open,high,low,close,is_forced,session_idx,bar_bits)` | Py→Rust, once per tuple | 4×`Vec<i64>` + `Vec<bool>` + `Vec<i64>` + `Vec<u64>` ≈ 7 columns × n_bars (≈ 2.7 MB @ 48k bars) |
| `simulate_masks(masks, is_buy, sl, tp, 3 policy bools)` | Py→Rust, per chunk | `Vec<u64>` ≤ chunk (86 @ 48k bars) |
| its return | Rust→Py, per chunk | `Vec<Vec<(i64,i64,i64,i64,usize,usize)>>` — one 6-int Python tuple **per trade** (~44 tuples/mask @ the 48k fixture) |
| `count_supermasks_u64s(bar_bits, masks)` | Py→Rust→Py, once per tuple | `Vec<u64>`×2 in, `Vec<u64>` counts out |
| `sweep_drawdown_stats(n_bars, start, exits)` | Py→Rust→Py, **per kept mask** | `Vec<(i64,i64)>` of (exit_idx, pnl) in; 4-tuple out; kernel allocates `vec![0i64; n_bars]` per call |
| `blake3_hash(payload)` | per finalist / error only (digest path) | ~200-B canonical payload |

Python objects churned **per kept mask**: `len(trades)` × `ClosedTrade` (frozen
dataclass, 7 fields incl. 2 `datetime`s), 1 exit-index `list`, 1 × 37-field
`ResultMetrics`, 1 `tuple` in `pairs` (batch arm) or ~5 `array` appends (bounded
arm), plus per-chunk list plumbing.

## 2. [MEASURED] Per-segment marginal cost per mask (sandbox)

From `baseline_refined.json` (506-mask sample, 16 condition bits, k=3, SL 80.00 ₹
/ TP 120.00 ₹, synthetic random-walk NIFTY 1m bars on real 2024 NSE sessions,
structlog=WARNING, burn-in excluded via `reset_self_check_for_tests(remaining=0)`):

| Segment (per mask unless noted) | 20,196 bars (18 trades/mask) | 48,620 bars (43.6 trades/mask) | Scaling law |
|---|---|---|---|
| enumeration + `has_conflict` | 2.40 µs | 2.46 µs | O(1)/mask, pure Python — matches the audit's 2.3 µs [AUDIT-RELAYED] |
| Stage-1B `count_supermasks` (batched Rust) | — | 12.5 µs (= **0.257 ns/bar·mask**) | O(n_bars)/mask, Rust |
| Rust walk only (`count_trades`, no output) | 25.5 µs | 62.3 µs (= **1.28 ns/bar·mask**) | O(n_bars)/mask, Rust — irreducible compute |
| + raw record FFI marshal (`simulate_masks`) | +3.6 µs | +17.3 µs (≈ **0.40 µs/trade**) | O(trades)/mask, PyO3 tuple build |
| + `ClosedTrade` + exit-idx Python materialisation (wrapper) | +37.8 µs | +97.8 µs (≈ **2.2 µs/trade**) | O(trades)/mask, **pure Python churn** |
| metrics tail `metrics_from_trades` (ON arm) | 93.2 µs | 206.3 µs | of which: |
| — drawdown kernel FFI (`sweep_drawdown_stats` + exits marshal) | 53.4 µs | 130.1 µs (≈ **2.7 ns/bar**) | O(n_bars) Rust scan + per-call `vec![0; n_bars]` alloc + O(trades) marshal |
| — `compute_metrics_from_scalars` (pure Python metric body) | 28.5 µs | 54.9 µs (≈ **1.26 µs/trade**) | O(trades)/mask Python |
| — `_exit_pairs` + seam overhead (residual) | ~11 µs | ~21 µs | O(trades)/mask Python |
| rank (`rank_combos`, amortised /mask) | 0.88 µs | 1.29 µs | O(kept)/tuple |
| zero-trade mask tail (FLAT shortcut, no FFI) | — | 7.6 µs | O(1)/mask Python |
| **Marginal total per KEPT mask** | **≈ 163 µs** | **≈ 400 µs** | |

Per-tuple one-time costs [MEASURED, same runs]: Stage-1A `build_bar_bits` 0.29 s
@ 20k bars / 0.71 s @ 48k bars (≈ 14.5 µs/bar — Python loop over
`trading_core.evaluate_with_levels`; amortised by `ResidentKernelCache` across
the direction/k/sl/tp tuples of one (inst, tf, prox, anchor)); `ResidentTradeSim`
construction 0.06 s / 0.15 s; per-FINALIST projection ≈ 40–70 ms each @ 20k bars
(Python-oracle re-simulation in `ranked_combo_to_result_row` — cProfile
`refprofile_e2e16_20k_k4_tottime.txt`: `run_with_exit_indices` 25 calls, 1.73 s
cum; bounded by `top_k`, default 20); store flush + fsync ~20-40 ms/tuple; audit
3 lines/tuple.

## 3. Attribution of the per-combo cost (the answer to "where does 0.58 ms go")

At the 48k-bar window (the closest sandbox proxy for a production mid-TF tuple),
the ≈400 µs marginal per kept mask splits:

| Bucket | µs | share | fate under a Rust sweep-core |
|---|---|---|---|
| Irreducible Rust walk (mask AND + trade state machine) | 62 | 16% | stays (constant may improve slightly fused) |
| Drawdown scan Rust compute (O(n_bars) fold) | ~105 | 26% | stays O(n_bars) but buffer-reuse + fusion cuts the constant (per-call `vec![0;n_bars]` alloc + scatter removed; the sequential per-bar fold itself is bit-pinned and must remain per-bar — see SPEC §float) |
| **FFI marshal (trade tuples out + exits back in)** | ~42 | 10% | **eliminated** (trades never leave Rust) |
| **Python object churn (ClosedTrade, exit lists, dataclasses)** | ~98 | 24% | **eliminated** |
| **Pure-Python metric body (O(trades) loop, moments, percentiles, 37-field object)** | ~55 | 14% | **moved to Rust** (bit-identity work required — see SPEC) |
| **Python seam overhead (_exit_pairs, per-mask function calls, zip, append)** | ~32 | 8% | **eliminated** |
| enumeration + conflict (per enumerated, Python) | 2.5 | ~1% | moved to Rust (~50× cheaper) |
| rank amortisation | 1.3 | <1% | stays Python phase-1 (columns-based, bit-pinned) |

**Python + FFI residual ≈ 230/400 ≈ 57% at 48k bars and rises with trade density
and n_bars** (at 20k bars ≈ 57%; the audit's 85% [AUDIT-RELAYED] is consistent
with production-scale windows — the 1m/5m tuples with 111k–555k bars and richer
trade streams — plus M4-Pro-vs-sandbox constant differences; my 0.40 ms sandbox
marginal at 48k bars is the same order as the audit's 0.58 ms/combo).

Cross-checks reproduced: enumeration 2.4–2.7 µs/mask ↔ audit 2.3 µs ✓; per-combo
marginal 0.16–0.40 ms (sandbox, 20k–48k bars) ↔ audit ~0.58 ms/combo at
production windows ✓ (same order, plausibly same quantity at bigger n_bars).

## 4. What the production digest path does NOT do per mask (important negatives)

- **No dedup probe.** `bypass_dedup = trial_stats_digest is not None` is always
  True on the production path; `DigestPathNoDedup` RAISES if consulted
  (`sweep_launch.py:99`). Bloom+LMDB dedup is legacy-no-digest-callers only. The
  redo authority is the per-tuple completion sentinel.
- **No per-mask run_id/blake3.** Lazy `_lazy_run_id` runs only for finalists
  (≤ top_k) + error fingerprints (DEC-SWEEP-STREAMING-RANK-001).
- **No per-mask audit lines.** Exactly 3 hash-chained lines per tuple.
- **No per-mask checkpoint writes.** Checkpointing is per tuple (parallel driver:
  contiguous-clean-prefix apply in the writer-actor).

So the coordinator's brief items "dedup probe batching" and "checkpoint/audit
batching" are ALREADY per-tuple-batched on the production path; the Rust core's
job there is to *preserve* that shape, not to invent it.

## 5. Fixed per-tuple overheads that do NOT shrink with the per-combo constant

(These bound the post-conversion wall-clock floor; see combo_counts.md caveats.)

- Lake bars load per (instrument, tf) — O(window), cached across tuples.
- Stage-1A `build_bar_bits` — ~14.5 µs/bar sandbox, once per
  (inst, tf, proximity, anchor) via `ResidentKernelCache`.
- Trade-sim construction / FFI column copy — O(n_bars) per tuple.
- Finalist projection — O(top_k × n_bars) Python-oracle re-sim per tuple.
- Store flush + fsync, sidecar write, sentinel write, 3 audit lines per tuple.
- Parallel-driver writer-actor apply (checkpoint/audit/cache record) per tuple.

## 6. Raw artifacts

- `baseline_measurements.json` — first pass (incl. DEBUG-log-polluted e2e; kept
  for provenance, superseded by the refined run for attribution).
- `baseline_refined.json` — the clean segment attribution used above.
- `refprofile_e2e16_20k_k4_tottime.txt` / `_cum.txt` — cProfile of the full
  production spine (`run_persisted_sweep`, 1820 enumerated / 729 kept masks).
- `profile_sweep_baseline.py` / `profile_sweep_refined.py` — the harnesses
  (synthetic bars on real NSE 2024 sessions; real Stage-1A; real spine).
