# Engine Driver + Trade-Sim — Scoping Proposal (2026-06-03, rev. 2 post-§9 review)

> **STATUS: PROPOSAL — not authorized for implementation.** Revised after a
> CLAUDE.md §9 4-agent hostile review (feasibility, correctness/look-ahead,
> contract-fit, gap-finder). No engine code is written until (a) the operator
> authorizes a phase and (b) PR-0 records `DEC-ENGINE-DRIVER-001` in
> `docs/MIGRATION.md`. Nothing here edits existing code.
> **Base branch:** `claude/nice-sagan-0V6ou`.

---

## 0. §9 review outcome — what changed from rev. 1

The hostile review caught material errors in the first draft; this revision
corrects them (each cited):

- **C1 (CRITICAL):** the 52-condition vocabulary→bitset producer **already
  exists** — `brutex/vocabulary/evaluator.py:137` `evaluate_atoms()` (52
  conditions, `bits.py:93`), unit-tested (`tests/unit/test_vocabulary.py`),
  with real indicators (`brutex/indicators/{ema,pivots,fib_levels,prev_day_ohlc,day_extremes}.py`).
  → PR-1 is **"wire the existing evaluator into a matrix builder + adapter
  layer,"** NOT "build the vocabulary."
- **C2 (CRITICAL):** `ExecutionSimulator` must **NOT** be reused for the spot
  fill/cost path — it fills at bar **CLOSE** (`simulator.py:176`), applies **5
  bps slippage** (`:113`) and **₹20 brokerage + STT** (`:125`) in `Decimal`,
  which **violates the Track-1 spot spec** (exact OPEN, no slippage, no costs,
  int64 paisa — `TRACK1_BACKTEST_SPEC.md:640-657`). The spot walker is a
  **new** signal-only component emitting int64-paisa `ClosedTrade` directly.
- **C3 (HIGH):** PR-0 must reconcile not just storage (C-02) but also **C-06**
  (`TRACK1_REQUIREMENTS_FINAL.md:31` — Stage 1B `coarse_score` look-ahead
  contamination, which **poisons PR-3**), plus C-03 and C-07; and must
  **CREATE** (not "confirm") the same-bar SL/TP tie-break, the intrabar
  exit-fill timing, and the UTC→IST convention (the spec leaves all three
  open).

---

## 1. Why this, why now

The 5-stage funnel is not wired end-to-end. The shipped kernel counts mask
hits (`rust/fno-math/src/python.rs:155-223`) but nothing turns a hit into a
trade P&L, and there is no funnel driver. The highest-leverage gap — which
unblocks both a real sweep and Stage 2.5 — is (1) a **Stage-2 trade-sim
walker** (mask hit → round-trip `ClosedTrade` → Sharpe) and (2) the
**end-to-end driver** wiring Stage 1A → 1B → 2 → result store.

This proposal **excludes** Stages 3/4 and all SIMD/Rayon/GPU acceleration, and
makes **no SLA claim**: even fully built, the single-threaded path will not
meet the 2–4 day / 33 B SLA (~22.8 days raw measured). The milestone is a
**correct, deterministic, single-instrument sweep**, not full speed.

---

## 2. Contracts that already exist (corrected reuse map)

| Contract | Where | Reuse for |
|---|---|---|
| **52-condition evaluator** `evaluate_atoms` | `brutex/vocabulary/evaluator.py:137`; bits `bits.py:93` | Stage 1A: bar → u64 condition bitset (REAL, unit-tested) |
| **Indicators** EMA/pivots/Fib/PDH-PDL/day-extremes | `brutex/indicators/*` | feed the `IndicatorSnapshot` (REAL) |
| u64 mask eval + supermask count (consumer) | `brutex/engine/combo_mask.py:231,286`; `ResidentBars` `:329-419` | Stage 2: "which bars fire mask M" |
| BLAKE3 9-field run identity | `brutex/strategies/combo.py:91-180` | stamp every result row |
| Closed-trade + Welford metrics (int64 paisa) | `brutex/metrics/types.py:40`; `compute.py:104-530` | round-trip → Sharpe (consumer is ready) |
| Hash-chained audit log | `brutex/orchestrator/audit.py` | one audit line per stage transition |
| Replay feed + timestamp look-ahead guard | `brutex/feed/replay.py:184-208` | bar-by-bar walk (necessary, **not sufficient** — see §5.3) |
| O(1) bench tier helper | `tests/bench/_o1_tiers.py:36` | per-op O(1) ratio gates |

**NOT reusable for Track-1 spot (annotate, do not wire):** `ExecutionSimulator`
(`brutex/execution/simulator.py`), `BacktestDriver` (`brutex/execution/driver.py:48-142`),
`StrategyRunner` (`brutex/strategies/runner.py`) — all are `Decimal` +
slippage + cost + close-fill (Track-2 / tradable-venue oriented). Only the
position-netting *math* is conceptually reusable, and even that is `Decimal`.
The spot walker is **new** code.

---

## 3. PR breakdown (corrected, honest dependency chain)

- **PR-0 — design-lock (docs/decisions only).** The §9 review (this rev. is
  its output) + reconcile conflicts **C-02 / C-03 / C-06 / C-07**
  (`TRACK1_REQUIREMENTS_FINAL.md:22-32`) + **CREATE** the SL/TP tie-break,
  exit-fill-timing, and UTC→IST conventions + **fix the `is_forced_exit_bar`
  tf-blindness bug** (`brutex/calendars/nse.py:291` ignores `tf_minutes`) as a
  prerequisite + record `DEC-ENGINE-DRIVER-001`.
- **PR-1 — Stage 1A matrix builder (the cleanly-greenfield seam).** Drive the
  **existing** `evaluate_atoms` across the bar feed and pack the `bar_bits`
  matrix the kernel consumes — including the real **adapter layer**:
  Decimal-rupee→paisa (`brutex.money.round_money`, ROUND_HALF_UP),
  float-EMA→paisa, `PriorBarHistory`, daily-scalar (PDH/PDL/pivots/Fib)
  broadcast across intraday bars, EMA warm-up `None`s, and **UTC→IST**. (This
  is the bulk of PR-1 — not "glue".)
- **PR-2 — spot trade-sim walker (highest risk; may split 2a/2b).** Entry @
  N+1 OPEN, SL/TP, 15:20 forced exit; emit int64-paisa `ClosedTrade`
  **directly** (NO simulator, NO slippage, NO costs) with **MFE/MAE
  intra-trade excursion** tracking; + build the **instrumented `evaluate(N)`
  accessor** that makes look-ahead Property **P-06** a real enforcer (it does
  not exist today — `combo_mask.py` exposes only stateless `evaluate_mask`).
  Golden oracle = hand-computed round-trips; mutation on the
  SL/TP/forced-exit precedence.
- **PR-3 — Stage 1B coarse filter.** ONLY after PR-0 resolves **C-06**
  (coarse_score look-ahead contamination). Pin whether the coarse score is
  Sharpe-based (hard-dependent on PR-2) or a cheap hit-count proxy
  (independent).
- **PR-4 — runner FSM + `brutex sweep` CLI.** Wraps PR-1/PR-2; stamps **all 9**
  BLAKE3 fields (enumerate `canonical_params`: proximity bps, `prior_n`, Mode
  A SL/TP grid cell) — replacing the literal `"N/A"` placeholders
  (`strategies/sweep.py:446-459`); decide `ComboSweep`'s fate (retire vs
  absorb — it has zero non-test callers). `brutex/engine/runner.py` is
  **distinct** from `brutex/strategies/runner.py` (docstring must
  disambiguate).
- **PR-5 — result store + reproducibility oracle.** Deterministic Parquet;
  byte-identical re-run test. Re-anchor the repro-hash gap to a **live** source
  (the prior `MIGRATION.md:271` / DEC-PROTOCOL-001 anchor was RETIRED in
  DEC-P0-026).

**Dependency chain (rev. 1's "all independently merge-ready" was wrong):**
PR-1 → PR-2 → PR-4; PR-3 depends on PR-0's C-06 resolution. Only **PR-1** and
the **PR-5 store skeleton** are truly independent.

---

## 4. Test gates (CLAUDE.md §12 — per PR)

100% line + branch on each new module; 7 test types; mutation ≥ 85%;
`mypy --strict` + `pyright` + `ruff` + `pylint` + `bandit` clean; hash-chained
audit on every material action; the no-look-ahead invariant via the new
instrumented accessor. **Plus** (§9 finding F-5/M3): an **absolute per-trial
micro-bench** for the walker — the existing O(1) ratio gates measure
*scaling*, not *throughput*, so a correct-but-slow walker passes them silently;
state O(1) only for the **per-operation** cost, never for the per-trade walk.

---

## 5. Open questions PR-0 must DECIDE (the spec leaves these open)

1. **Same-bar SL+TP tie-break** — spec silent (`TRACK1_BACKTEST_SPEC.md:240-260,658`);
   recommend pessimistic (SL-first).
2. **Intrabar SL/TP exit-fill** — fill at the touched level vs next-bar OPEN
   (spec ambiguous at `:640-641`).
3. **UTC→IST conversion boundary** — lake is UTC; 15:20 forced-exit + time-of-day
   bits are IST. A naive UTC compare forces exit ~5.5 h early.
4. **`is_forced_exit_bar` tf-blindness** — `nse.py:291` ignores `tf_minutes`;
   coarse-TF bars crossing 15:20 are missed. Fix + regression test.
5. **C-06 Stage 1B coarse_score look-ahead contamination** (`TRACK1_REQUIREMENTS_FINAL.md:31`).
6. **Storage envelope** — C-02 (50-100 GB vs 2.5 TB vs 29 TB) is **UNVERIFIED**
   (`TRACK1_REQUIREMENTS_FINAL.md:27`); PR-5's layout can't be sized until pinned.
7. **`canonical_params` surface** for reproducibility (which params enter the hash).

---

## 6. Honest envelope (Golden Rule #10)

- This unlocks a **correct, deterministic, single-instrument** sweep — it does
  **not** meet the 33 B / 2–4 day SLA. That needs Stages 2.5/3/4 (pruning) +
  Rayon/GPU (parallelism), all later phases.
- It is a **multi-PR effort** (PR-0..PR-5, and PR-1/PR-2 may each split); not a
  single drop.
- "Guaranteed brute-force on a MacBook" stays an **aspiration** until the
  pruning + parallelism phases land; this is the necessary foundation, not the
  finish line.
- Implementation begins only after operator authorization + `DEC-ENGINE-DRIVER-001`.
