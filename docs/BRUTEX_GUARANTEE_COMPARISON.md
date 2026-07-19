# BruteX — the honest comparison, in one page

> **For anyone — not just engineers.** If you can read a phone plan
> comparison, you can read this. It maps *every promise* to the **automatic
> machine that proves it on every single code change** (a CI gate). If that
> machine ever goes red, the change is **blocked from shipping**.
>
> **Honesty rule (no illusion — CLAUDE.md Golden Rule #10):** we never print
> "100% / fully / guaranteed" for something only partly built. Every row is
> marked truthfully:
>
> | Badge | Meaning |
> |---|---|
> | 🟢 **live** | runs automatically on every change today; blocks the merge if it fails |
> | 🟡 **partial** | real and running, still being tightened (the gap is stated) |
> | 📋 **planned** | designed, not wired yet |
> | 🚫 **out of scope** | deliberately not built (BruteX is signal-only backtest research) |
>
> _Companions: [`WHY_BRUTEX.md`](WHY_BRUTEX.md) (plain-English scorecard) ·
> [`BRUTEX_VS_THE_FIELD.md`](BRUTEX_VS_THE_FIELD.md) (vs other engines) ·
> [`STANDARDS_100PCT.md`](STANDARDS_100PCT.md) (the full enforced-vs-target audit) ·
> [`MERGE_READINESS_GUARANTEE.md`](MERGE_READINESS_GUARANTEE.md) (the merge gate)._

---

## The one-line promise

**BruteX is a self-checking factory for trading-strategy research.** Every
change is inspected by a battery of automated gates *before* it is allowed in,
and **nothing ships unless every gate is green** — so the data stays
trustworthy and the engine stays constant-time-fast at any size.

---

## 1 · Your exact ask → what BruteX actually does

Each "extreme speed / O(1)" promise below is tied to a **real, named, automatic
check**. This is the "no illusion" table.

| What you asked for | In plain English | BruteX today | The machine that proves it (on every change) |
|---|---|---|---|
| **O(1) uniqueness** | "Spotting whether we've already seen this — instant, even among billions" | 🟢 live | a `blake3` fingerprint of every run/combo + property tests asserting no collisions — `brutex/strategies/combo.py`, `tests/property/test_combo_hash_uniqueness.py` (its scaffold-tier sibling `test_dedup_hash_properties.py` was deleted with the unwired `orchestrator/store.py` — MIGRATION.md #1969) |
| **O(1) de-duplication, zero data loss** | "Never double-write, never drop a real bar" | 🟢 live | 4-tier dedup: Bloom filter → LMDB exact-confirm → audit-replay → property invariants; a Bloom false-positive can **never** cause a false drop — `brutex/orchestrator/bloom.py` + the LMDB exact-confirm tier `brutex/data/lmdb_membership.py` / `brutex/engine/sweep_cache.py` (the unwired `orchestrator/store.py` scaffold was deleted — MIGRATION.md #1969), `tests/property/test_bloom_invariants.py`. Plus a **persisted O(1) fetch-coverage ledger** so a re-pull *skips* windows already in the lake (never re-burns the data-vendor quota) — `brutex/data/fetch_coverage_store.py` (DEC-FETCH-COVERAGE-WIRING-001) |
| **O(1) mapping / lookup** | "Finding a symbol, a bar, or an indicator is instant" | 🟢 live | numeric primary-key catalog (`SECURITY_ID`) + dict/memory-map lookups, **speed-gated by a ratio benchmark** — `tests/bench/test_throughput_canary.py`, `test_lmdb_membership_o1.py` + `test_instrument_store_get_o1.py` (the deleted `test_store_o1.py`'s successors — MIGRATION.md #1969) |
| **O(1) latency / time per hot path** | "Same speed on row 1 and row 1-billion" | 🟢 live (ratio-gated) | `p99(big) / p99(small)` ratio gates on bar lookup, the u64 strategy-mask AND, dedup, and the rate limiter — `tests/bench/test_lmdb_membership_o1.py`, `test_engine_results_store_o1.py`, `test_rate_limiter_o1.py`, `test_throughput_canary.py` *(honest note ▼: tightest 1.1× tier is 🟡)* |
| **O(1) space per operation** | "RAM per step stays flat as data grows" | 🟢 live | state lives in a memory-mapped LMDB tier off the Python heap — total storage is O(n), **per-operation RAM is O(1)** — `brutex/data/lmdb_membership.py` + `brutex/engine/sweep_cache.py` |
| **All worst cases / errors / exceptions / bugs** | "It's tested every way that matters, and bugs can't sneak back" | 🟢 live (core) · 🟡 (full invariant catalog) | property/fuzz tests + golden fixtures + a **red-team harness that plants bugs and asserts the gates catch them** + a runtime look-ahead guard + per-symbol quarantine (one bad symbol can't kill the run). The full 24-invariant catalog is 5 live + 19 planned — `tests/property/`, `tests/golden/`, `brutex/feed/` |
| **Comprehensively, fully, extremely automated** | "Humans don't babysit it" | 🟢 live | the full CI battery runs on **every push**; one master `CI OK` gate; **auto-merge only on all-green** + nightly ~5-min stress regression gate + weekly self-hosted soak (#1580) + weekly mutation — `.github/workflows/ci.yml`, `auto-merge.yml` |
| **Real-time checks, guarantee + assurance** | "You find out the instant something breaks" | 🟢 live | every change triggers the gates immediately; a tamper-proof **hash-chained audit log** (break the chain → CI fails); reproducibility fingerprint on every run — `brutex/orchestrator/`, CLAUDE.md §2 Rule 4 |
| **All kinds of tests / full coverage** | "Tested every which way, and the code is actually exercised" | 🟢 7 test types · 🟡 coverage | 7 test families live (▼ §3); coverage is a **90% hard floor + 103 modules frozen at 100% line+branch** climbing the rest — `STANDARDS_100PCT.md` §1 |

---

## 2 · BruteX vs the usual ways to backtest

| What you feel | 🟥 DIY (pandas + a loop) | 🟧 A heavyweight framework | 🟩 **BruteX** |
|---|---|---|---|
| **Re-running a data pull** | re-downloads / risks duplicates | manual cache keys | **O(1) uniqueness + dedup** — never double-writes, never drops a real bar |
| **Pulling again tomorrow** | re-downloads the whole history | hand-rolled incremental logic | **only the new day is fetched** — a persisted skip-ledger marks every settled window done; today is pulled on the next day (daily-incremental, quota-frugal) |
| **Speed as data grows** | slows down (O(n)) | varies, hand-tuned | **O(1) per bar / per signal / per dedup** — constant-time, CI-gated |
| **Memory as data grows** | loads everything into RAM | tuned by hand | **O(1) RAM per operation** — memory-mapped tier |
| **One bad symbol mid-run** | kills the whole run | depends | **quarantined** — the rest keep flowing |
| **Look-ahead "cheating"** | easy to add silently | manual care | **blocked at runtime** — at bar N the engine can read only bars 0..N |
| **"Is it correct?"** | you hope so | some tests | a full automated suite + **hash-chained audit log** + reproducibility fingerprint |
| **"Is this edge real, or just curve-fit?"** | no idea — the best of thousands of tries *always* looks great | rarely measured | **quantified** — the **Deflated Sharpe Ratio** (discounts your Sharpe for *how many* strategies you tried + fat tails) and the **Probability of Backtest Overfitting** (CSCV) put an honest number on it |
| **Shipping a change** | push and pray | CI if you wrote it | **all gates must be green**, then it auto-merges — never on trust |
| **Daily operation** | babysit it | babysit it | **designed for zero-click**: auto-merge + nightly stress gate + weekly self-hosted soak (#1580) + red-team harness |

---

## 3 · Every kind of test (most projects ship 1–2)

| Test type | What it catches | In BruteX |
|---|---|---|
| **Unit** | logic bugs | 🟢 live, runs every push |
| **Integration** | wiring / contract bugs | 🟢 live, runs every push |
| **Property (fuzz)** | edge cases you didn't think of | 🟢 live (`-x`, stops on first falsification) |
| **Golden-master** | byte-level output drift | 🟢 live (exact-equality fixtures) |
| **Benchmark** | O(1) speed regressions | 🟢 live (ratio gates) |
| **Stress / soak** | sustained leaks / drift over hours | 🟢 live — nightly ~5-min stress regression gate (`stress-nightly.yml`) + weekly self-hosted multi-hour soak (`soak-selfhosted.yml`, duration-derived timeout; DEC-SOAK-REAL-TIER-001 #1580 — the prior “24-hour (nightly)” claim was false and is retracted) |
| **Mutation** | "do the tests actually catch bugs?" | 🟢 live (85% threshold on the critical state module; weekly full run) |
| **Smoke + Red-team** | fast pre-push + gate-integrity (planted bugs) | 🟢 live |

---

## 4 · The honest scoreboard (Golden Rule #10 — live vs not)

A comparison that only lists wins is marketing, not engineering. Here is the
truth, straight from the enforcement audit.

| Capability | Status | Honest note |
|---|---|---|
| Inspection belt (lint · 2 strict type-checkers · 5 security scanners · duplication · dead-code · build) | 🟢 **live** | runs on every change; merge blocked on any red |
| O(1) uniqueness · dedup · mapping · space | 🟢 **live** | Bloom → LMDB → blake3, property-tested every push |
| O(1) latency / time ratio gates | 🟢 **live (3–10×)** | the **tightest 1.1× tier is 🟡 wired** — it switches on once a dedicated quiet M4-Pro runner is registered (shared CI is too noisy to prove 10%) |
| Tamper-proof audit log + bit-for-bit reproducibility | 🟢 **live** | break the hash chain → CI fails; the sweep→Parquet **results store** writes byte-identical files keyed by the `blake3` run-identity and **refuses to overwrite one identity with a different result** (a clash = upstream non-determinism, caught not hidden) — `brutex/engine/results_store.py` (DEC-RESULT-STORE-001), with golden + property byte-equality tests |
| Restart-safe, quota-frugal incremental pulls | 🟢 **live** | a persisted fetch-coverage ledger skips windows already in the lake across restarts; only the new day is re-fetched (DEC-FETCH-COVERAGE-WIRING-001) |
| Dashboard %-metrics consistency | 🟢 **live** | `change_pct` / `open_gap_pct` anchor on the prior day's **last 1-minute close** — internally consistent with the 1-minute feed the engine sweeps (DEC-SPOT-PREVCLOSE-1MIN-001); deliberately distinct from a broker's official daily % (honest, by design — that lives only in the separate pre-open report) |
| Test coverage | 🟡 **90% floor + 103 modules at 100%** | climbing to 100% one locked module at a time; 8 modules pending an upstream library fix |
| Standards enforcement (overall) | 🟡 **15 of 21 rows fully enforced (71%)** | + 2 partial (10%) + 1 roadmap (5%) + 3 manual-reviewer (14%) — per `STANDARDS_100PCT.md` §1, re-verified 2026-06-02. *(Older docs that say "9 of 21 / ~22%" are pre-reconciliation drift.)* |
| The brute-force **sweep engine** (Stage 1A→1B→2→3→4) | 📋 **next (Phase 3)** | the per-bar / per-mask / dedup **primitives exist and are O(1)-gated today**; the cascade itself is the upcoming build |
| **Overfitting defence** — Deflated Sharpe Ratio + Probability of Backtest Overfitting | 🟢 **metrics live** · 📋 driver next | the two institutional anti-overfit statistics are implemented as cited, deterministic, **100%-tested** pure functions (Bailey & López de Prado 2014 DSR; Bailey et al. 2017 PBO-via-CSCV) — `brutex/metrics/deflated_sharpe.py`, `brutex/metrics/pbo.py` (DEC-STAT-DSR-PBO-001); the Stage-4 driver that feeds them the real per-leaderboard trial-count / Sharpe-dispersion / CSCV matrix is part of the upcoming filter-cascade build |
| Overfitting protection — **purged cross-validation + walk-forward** | 🟢 **CV/WFO engine live** · 📋 cascade wiring | the Combinatorial Purged CV (S=16 → **12,870** purge+embargo splits, **no-look-ahead leakage property-tested**) and the rolling/anchored/expanding walk-forward **pass-rate engines** are live, **100%-covered**, and **O(1)-per-fold-gated** (a measured 1.03× flat ratio) — `brutex/metrics/cpcv.py` + `wfo.py` (DEC-STAT-CPCV-WFO-001); the DSR / PBO / Lockbox filters + result-row wiring that *consume* them are 📋 the upcoming Stage-4 build |
| "33 billion trials in 2–4 days" SLA | 📋 **target, not yet proven** | tracked as an `xfail` until the engine + M4-Pro runner land — no "millions in minutes" claim without the hardware (Golden Rule #10) |
| Live trading / order placement | 🚫 **out of scope** | Track 1 is **signal-only research**; execution realism is the later Options Track |

---

## The bottom line

- ✅ **Genuinely locked today:** the inspection belt; O(1) uniqueness / dedup /
  mapping / space; O(1) latency ratio gates; the tamper-proof audit trail;
  bit-for-bit reproducibility; auto-merge-only-on-green; and a *growing* set of
  modules frozen at 100% test coverage.
- 🟡 **Still tightening:** total coverage (90% floor → 100%); the strictest 10%
  speed proof; richer live dashboards.
- 📋 **Designed, not built:** the full sweep cascade (Phase 3) and the
  billions-in-days SLA — published as a *target*, never as a finished claim.

> **Priority law (never traded away):**
> **Correctness > Reliability > Recoverability > Observability > Performance > Scalability.**

*The BruteX bet: every promise on this page is wired to a gate that turns red
if we break it. That is the feature — boring, bulletproof, and merge-blocked
when it isn't.*
