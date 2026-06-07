# Wave 2 Plan — DO NOT FORGET (end-to-end brute-force sweep)

> **Purpose:** durable record of the Wave-2 workstreams so they are NEVER lost.
> Wave 1 (parallel sessions, June 2026) built the *building blocks*; Wave 2
> *assembles* them into a full, production, guaranteed end-to-end brute-force
> sweep. Until Wave 2 lands, `brutex sweep` is a single-tuple, in-memory,
> print-only vertical slice — NOT a production sweep (honest envelope, Golden
> Rule #10).

## Context: what Wave 1 delivered (the bricks)

| Wave-1 PR / session | Brick |
|---|---|
| #453 | per-second ≤5 rate GUARANTEE + `rps_1s_max` gauge + breach alarm |
| #454 | Q-LIVE: `/coverage/windows` live during a spot pull |
| #455 (S6) | gated the latent renew-token / daily-probe POSTs |
| #456 (S2) | live `rps_1s` on the dashboard rate panel |
| #457 (S3) | activated property invariants P-03/P-04/P-09/P-13/P-14 |
| #458 (S5) | options `/coverage/options` live during a pull |
| #459 (S1) | Phase-2 golden corpora #1–5 + #9 |
| S4 | Phase-3 engine slice (Stage-1A→mask→trade-sim→**results persistence**) |
| S7 | sweep G&A modules: `sweep_audit`, `sweep_progress`, `sweep_checkpoint`, `sweep_cache`, `sweep_watchdog` (standalone, unit-tested) |

## The gaps Wave 2 closes (verified absent on `main` by the 2026-06 audit fleet)

1. **Full multi-parameter sweep orchestrator** — today `rank_sweep_over_bits`
   only loops the mask space for ONE `(instrument, TF, direction, params)` tuple.
2. **Result persistence wired** — `engine/results_store.py` `ResultsStore` is
   built but **never called** by the sweep (`cli.py`: "signal-only — nothing is
   persisted, DEC-SWEEP-CLI-001").
3. **G&A wired into the sweep loop** — S7 builds audit/progress/checkpoint/
   dedup/watchdog as standalone modules; nothing calls them from the sweep yet.
4. **Throughput SLA on the REAL path** — `tests/bench/test_full_sweep_extrapolation.py::test_projected_33b_within_sla`
   is `@pytest.mark.xfail(strict=True)` (real engine projects ~23 days for 33B
   trials vs the 4-day SLA — needs resident buffers + batch eval).
5. **Overfitting gates** — `metrics/cpcv.py`, `metrics/spa.py`,
   `metrics/deflated_sharpe`, `metrics/pbo`, `metrics/cascade.py` exist but are
   NOT wired into `brutex/ranking` (the lockbox/overfit gate is "Stage-3 deferred").

## Dependency order (fire AFTER the prerequisite has merged)

```
Wave 1 (S4 + S7) ──► W2-a (orchestrator + persistence)
                       │
                       ├──► W2-b (wire S7 G&A into the loop)   [needs S4 + S7 + W2-a]
                       ├──► W2-c (throughput SLA on real path) [needs W2-a]
                       └──► W2-d (overfitting gates → ranking) [needs W2-a: persisted results]
```

W2-b / W2-c / W2-d can run in parallel **once W2-a is merged** (they touch
different surfaces). Each on its OWN branch from `origin/main`. The only shared
file is `docs/MIGRATION.md` (append) → `git merge origin/main`, keep both.

---

## W2-a — Full sweep orchestrator + result persistence

```
You are working in the BruteX repo. FIRST read CLAUDE.md (all 12 Golden Rules), docs/TRACK1_BACKTEST_SPEC.md, docs/WAVE2_PLAN.md, and docs/MIGRATION.md (latest entries). The Phase-3 engine slice (Stage-1A→mask→trade-sim→results) and engine/results_store.py ResultsStore already exist; engine/sweep_driver.py rank_sweep_over_bits sweeps the mask space for ONE (instrument, TF, direction, params) tuple.

TASK: Build the full multi-parameter sweep ORCHESTRATOR that enumerates the outer product (instrument × timeframe × direction × K × stop-loss × take-profit × proximity), and for each tuple calls rank_sweep_over_bits, then PERSISTS every ranked result via ResultsStore.append + .flush (it is built but currently uncalled — cli.py says "signal-only, nothing persisted"). Add a launch entry point (a `brutex sweep-all` CLI subcommand and/or a dashboard action) so an operator can start a full sweep. Honor CLAUDE.md Rule #5 (O(1) per-op), Rule #7 (idempotent re-run), Rule #10 (honest envelope — do NOT claim O(1) total).

BRANCH: claude/sweep-orchestrator-full (branch from origin/main).
PREREQUISITE: the Phase-3 engine slice (Wave-1 session S4) MUST be merged first.

DELIVER + DISCIPLINE: ruff format+check, mypy --strict, pyright (brutex+tests 0/0); 100% line+branch on every new brutex/* module (add the strict gate test files in .github/workflows/ci.yml); add a MIGRATION.md DEC-ID entry; the PR body MUST pass scripts/ci/zplus-pr-guard.sh + definition-of-ready-guard.sh + validate_no_hallucination.py + validate_dec_ids.py (run locally first). DRAFT PR → mark ready → MONITOR CI to green → fix any failure → MERGE on green. CI (3.12) is the oracle for the wheel/PEP-695 chain; unit-test pure pieces locally. Resolve trivial docs/MIGRATION.md append conflicts via `git merge origin/main`.
```

## W2-b — Wire the Wave-1 G&A modules into the sweep loop

```
You are working in the BruteX repo. FIRST read CLAUDE.md, docs/WAVE2_PLAN.md, and docs/MIGRATION.md. The Wave-1 sweep G&A modules exist on main: engine/sweep_audit.py (blake3 hash-chain), engine/sweep_progress.py, engine/sweep_checkpoint.py, engine/sweep_cache.py (Bloom+LMDB dedup), engine/sweep_watchdog.py. The full sweep orchestrator (W2-a) exists.

TASK: WIRE the G&A modules INTO the sweep orchestrator loop so every combo is: (1) audit-logged to the blake3 hash-chain (CLAUDE.md Rule #8), (2) counted by sweep_progress (%, ETA, combos/sec — exposed on a dashboard /api/v1/sweep-progress route + panel), (3) checkpointed after completion so a killed sweep RESUMES from the last combo (Rule #7), (4) dedup-skipped if (mask,instrument,tf,direction,params) is already in sweep_cache, (5) heartbeated to sweep_watchdog so a hung combo is detected. This is the real-time GUARANTEE + ASSURANCE layer becoming live.

BRANCH: claude/sweep-wire-ga (branch from origin/main).
PREREQUISITE: Wave-1 S4 + S7 AND W2-a all merged first.

DELIVER + DISCIPLINE: same as W2-a — ruff/mypy/pyright 0/0, 100% coverage on touched modules, MIGRATION.md DEC-ID, PR body passes the 4 scripts/ci guards, DRAFT→ready→monitor→merge on green, resolve trivial MIGRATION.md conflicts via `git merge origin/main`.
```

## W2-c — Throughput SLA on the real sweep path

```
You are working in the BruteX repo. FIRST read CLAUDE.md (Rule #5 O(1), Rule #10 honest limits), docs/TRACK1_PERFORMANCE_BUDGET.md, docs/WAVE2_PLAN.md, and tests/bench/test_full_sweep_extrapolation.py (the test_projected_33b_within_sla bench is xfail today).

TASK: Make the REAL sweep path fast enough to retire the xfail — resident bar buffers reused across masks (no re-load per combo), numpy/Rust batch mask-AND evaluation, and batch trade-sim, so the per-trial cost drops toward the SLA. Then either flip test_projected_33b_within_sla from xfail to a real gate, OR (honest envelope, Golden Rule #10) document precisely the hardware envelope it needs (the absolute 50M/s gate runs on the M4 Pro self-hosted runner; CI keeps the O(1) RATIO gate). Do NOT fake the number — measure it.

BRANCH: claude/sweep-throughput-sla (branch from origin/main).
PREREQUISITE: W2-a merged.

DELIVER + DISCIPLINE: ruff/mypy/pyright 0/0; keep all bench ratio gates green; 100% coverage on touched modules; MIGRATION.md DEC-ID; PR body passes the 4 scripts/ci guards; DRAFT→ready→monitor→merge on green; trivial MIGRATION.md conflicts via `git merge origin/main`. Be brutally honest about measured vs target throughput (no illusion).
```

## W2-d — Wire the overfitting gates into ranking (Stage 3)

```
You are working in the BruteX repo. FIRST read CLAUDE.md, docs/TRACK1_BACKTEST_SPEC.md, docs/WAVE2_PLAN.md, and brutex/ranking/__init__.py (its docstring notes the overfit/lockbox gate is "Stage-3 deferred"). The modules metrics/cpcv.py, metrics/spa.py, metrics/deflated_sharpe, metrics/pbo, and the aggregator metrics/cascade.py already exist and are unit-tested but are NOT wired into ranking or the sweep.

TASK: Wire the overfitting controls into the sweep's ranking as the Stage-3 lockbox/overfit gate — run CPCV / SPA / Deflated-Sharpe (PSR) / PBO (via cascade.py) over the persisted sweep results so combos that fail the multiple-testing / overfit checks are rejected or down-ranked. Feed it from W2-a's persisted ResultsStore output. Keep the existing 5 single-result gates; ADD this as the 6th (overfit) gate + its ranking term.

BRANCH: claude/overfit-gates-ranking (branch from origin/main).
PREREQUISITE: W2-a merged (needs persisted sweep results to validate).

DELIVER + DISCIPLINE: ruff/mypy/pyright 0/0; 100% coverage on touched brutex/* modules; MIGRATION.md DEC-ID; PR body passes the 4 scripts/ci guards; DRAFT→ready→monitor→merge on green; trivial MIGRATION.md conflicts via `git merge origin/main`. Honest envelope: state which statistical assumptions each test makes; do not over-claim significance.
```

---

## Definition of Done for the WHOLE sweep (Wave 2 complete)

A single command/dashboard action runs a full multi-instrument sweep that:
- ✅ enumerates the full parameter product and evaluates every combo (W2-a)
- ✅ persists every ranked result to Parquet, reproducibly by blake3 run-id (W2-a)
- ✅ logs every combo to a verifiable audit hash-chain (W2-b, Rule #8)
- ✅ shows live progress (%, ETA, combos/sec) and survives a kill via resume (W2-b)
- ✅ skips already-done combos in O(1) via Bloom+LMDB dedup (W2-b)
- ✅ never hangs silently (watchdog heartbeat) (W2-b)
- ✅ meets — or honestly documents — the throughput SLA (W2-c)
- ✅ rejects overfit combos via CPCV/SPA/DSR/PBO (W2-d)

Only when ALL of the above are merged and green is the end-to-end brute-force
sweep "done" — and never claim it before then (Golden Rule #10, no illusion).
