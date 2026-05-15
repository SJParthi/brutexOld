# Phase 3 — Brute-Force Engine (Stages 1A → 1B → 2 → 3)

## Status: ☐ Not started

## Effort: 2-3 weeks
## Owner: Claude

## Goal

Build the 4-stage search pipeline (Stages 1A, 1B, 2, 3) hitting the
throughput SLA. At end of Phase 3, BruteX can run a small test sweep
end-to-end and measure actual trials/sec on real M4 Pro hardware.

## Prerequisites

- [x] Phase 0 complete
- [ ] Phase 1 complete
- [ ] Phase 2 complete (data + vocabulary live)
- [ ] Phase 2.5 complete (Stage 2.5 early-stop live)
- [ ] Read `TRACK1_BACKTEST_SPEC.md §12-14` (search pipeline + dedup)
- [ ] Read `TRACK1_PERFORMANCE_BUDGET.md` (throughput math)
- [ ] User confirms "start Phase 3"

## Checklist

### Week 8 — Engine core

- [ ] `rust/fno-math/src/engine/runner.rs`:
  - [ ] Single-trial walker (per bar, per Mode A or B)
  - [ ] Entry at bar (N+1) OPEN after candle CLOSE signal
  - [ ] Exit triggers: SL hit / TP hit / 15:20 forced
  - [ ] Position size: % of notional
- [ ] `rust/fno-math/src/engine/parallel.rs`:
  - [ ] Rayon work-stealing
  - [ ] Memory tiling by (instrument, TF, day-window) bucket
  - [ ] 8-wide SIMD bitmask AND (NEON on Apple Silicon)
- [ ] First micro-benchmark: 100K trials end-to-end
  - [ ] Confirm 80-150K trials/sec sustained on M4 Pro
  - [ ] **Write canary artifact to `.brutex/bench/canary_throughput.json`**

### Week 9 — Stages 1A + 1B + 2

#### Stage 1A — Pattern discovery (NIFTY 15m only)

- [ ] ~5.8M trials, target ~90 sec wall
- [ ] Outputs top 50,000 patterns
- [ ] Pattern generator already produces conflict-pruned set

#### Stage 1B — Expansion to all 63 (inst × TF) combos

- [ ] Top 50K × 63 inst-TF = 3.15M trials
- [ ] Target ~56 sec wall
- [ ] Outputs top 10,000 (pattern × dir × inst × TF) combos

#### Stage 2 — Full parameter brute force (~33B trials, with Stage 2.5 → ~13B effective)

- [ ] Mode A: 10K combos × 2.76M params = 27.6B raw
- [ ] Mode B: 10K combos × 500K params = 5.0B raw
- [ ] Stage 2.5 active per Phase 2.5 — prunes ~70% of combos
- [ ] Result Parquet streaming + threshold filter (drops 95% poor rows)
- [ ] Dedup unified across stages (Bloom + LMDB)
- [ ] Resumable checkpoints every 100M trials (decision #96)

#### Property tests Phase 3

- [ ] P-01, P-02 (combo_hash canonicalisation + change detection) green
- [ ] P-05 (Bloom no FN) green
- [ ] P-06 (no look-ahead) green
- [ ] P-19, P-20 (deterministic Parquet + replay byte-identical) green
- [ ] All 6 O(1) hot-path benchmarks ratio ≤ 1.1

#### Oracle tests

- [ ] **`test_replay_audit_log.py`** green — first reproducibility win
- [ ] **`test_python_rust_parity.py`** green — correctness oracle
- [ ] Phase 3 canary `test_full_sweep_extrapolation.py` green

### Week 10 — Stage 3 + sign-off

#### Stage 3 — Bayesian refine via CMA-ES (Optuna)

- [ ] Top 1K × CMA-ES with 1-pt SL/TP precision
- [ ] ~50K trials, ~15-20 min wall
- [ ] Outputs top 100 candidates
- [ ] Optuna SQLite storage (per spec §16)

#### Smoke test the full pipeline

- [ ] First micro-sweep: 1 day × 4 inst × 100 patterns × full params
- [ ] End-to-end pipeline produces results.parquet
- [ ] Audit chain verifies clean
- [ ] Replay oracle byte-identical

#### Phase 3 sign-off

- [ ] **C11 canary throughput.canary_fresh GREEN**
- [ ] Published SLA band confirmed per `TRACK1_BACKTEST_SPEC.md §2.1`
- [ ] If band needs amendment (canary measures <150K t/s), MIGRATION row added
- [ ] **Spawn 4-agent hostile review** on Phase 3
- [ ] **User signs off** → ready for Phase 3.5

## Verification gates

- [ ] All Phase 1 + Phase 2 + Phase 2.5 property tests still green
- [ ] **All 6 O(1) hot-path bench ratios ≤ 1.1**
- [ ] Stage 1A wall ≤ 90 sec
- [ ] Stage 1B wall ≤ 56 sec (63 inst-TF)
- [ ] Stage 2 throughput ≥ 80K t/s sustained on M4 Pro
- [ ] Stage 3 wall ≤ 30 min for 1K refine
- [ ] Replay oracle byte-identical
- [ ] Python-Rust parity oracle ≤ 1 paisa diff
- [ ] CI 22 gates green
- [ ] 4-agent hostile review report

## Exit criteria

- [ ] Stages 1A-3 produce a `results.parquet` for a small test sweep
- [ ] SHA256 reproducibility confirmed (audit-replay oracle)
- [ ] Python-Rust parity holds
- [ ] O(1) bench passes at all 6 hot paths
- [ ] Phase 3 canary measured and recorded
- [ ] **Published SLA band amended if needed**
- [ ] User signs off

## Files to create / modify

```
rust/fno-math/src/engine/runner.rs                 (new)
rust/fno-math/src/engine/parallel.rs               (new)
rust/fno-math/src/engine/checkpoint.rs             (new, resumable)
rust/fno-math/src/engine/dedup.rs                  (Bloom + LMDB)
brutex/strategies/result_writer.py                 (Parquet streaming + threshold)
brutex/orchestrator/sweep.py                       (replace skeleton, wire Stage 1A/1B/2/3)
brutex/strategies/stage3_cma_es.py                 (Optuna integration)
tests/bench/test_hotpath_o1.py                     (full impl, 6 hot paths)
tests/bench/test_full_sweep_extrapolation.py       (Phase 3 canary, full impl)
tests/integration/test_replay_audit_log.py         (full impl)
tests/integration/test_python_rust_parity.py       (full impl)
```

## Risk register

| Risk | Mitigation |
|---|---|
| Throughput < 80K t/s sustained | Canary CI gate; SLA self-amends per decision #98 |
| Memory tiling abstraction leaks | Bench at varying cache pressure; profile with `instruments` |
| Python-Rust parity diverges | Sample 100 random combos at every commit; assert ≤ 1 paisa diff |
| Stage 1A NIFTY-only choice misses cross-instrument patterns | Stage 1B expands to all 63 — recovers cross-instrument signal |
| Resumable checkpoint corruption | Audit-replay test catches; checkpoint includes audit chain SHA |

## Decisions to lock (#131-#140 estimated)

- #131: Engine core (runner + parallel) shipped
- #132: 8-wide SIMD bitmask AND on Apple Silicon NEON
- #133: Memory tiling by (inst, TF, day-window)
- #134: Stage 1A pattern discovery
- #135: Stage 1B expansion to 63 inst-TF
- #136: Stage 2 full brute force with Stage 2.5 active
- #137: Stage 3 Bayesian refine via Optuna CMA-ES
- #138: Resumable checkpoints every 100M trials
- #139: Phase 3 canary throughput measured (specific value)
- #140: Phase 3 4-agent hostile review pass

## Phase 3 entry / exit summary

| | Before | After |
|---|---|---|
| Engine | absent | Stages 1A/1B/2/3 working |
| Result Parquet | absent | streaming + threshold filter |
| Throughput measured | UNVERIFIED | actual value on canary |
| Published SLA | TARGET only | CONFIRMED or AMENDED |
| Oracle tests | skeletons | all 4 green |
| O(1) bench | not measured | all 6 ratios ≤ 1.1 |
| MIGRATION decisions | ~130 | ~140 |
| Phase 3.5 readiness | n/a | READY |
