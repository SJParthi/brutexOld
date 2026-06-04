# BruteX — Shared Core & Workspace Status (the honest comparison)

> **One promise:** *what you backtest is EXACTLY what trades — proven bit-for-bit,
> machine-checked on every change.*
>
> This page is the **no-illusion** scorecard. Every ✅ is a gate that runs and a
> number that was observed; every 🟡/🔴 is stated plainly. Compiled from a
> 4-agent workspace verification (2026-06-04): whole-workspace health, the O(1)
> guarantee audit, the test-coverage/automation audit, and the brute-force-engine
> roadmap. Reconciled against `DEC-SHARED-CORE-001` (`docs/MIGRATION.md` #441).

---

## 1. The shared "brain" — Before vs After

| | ❌ BEFORE | ✅ AFTER (merged) | 💡 Why a human cares |
|---|---|---|---|
| 52-condition rulebook | Python, could drift from live | **One Rust core**, Python deleted | Research = live, by construction |
| Pivot + Fibonacci math | typed **twice** (Python + Rust) | **one Rust copy**; Python only validates + calls | can't compute two different answers |
| Speed of the brain | interpreted Python | **compiled Rust** (one call/bar; pivots/Fib once/day) | native-code hot path |
| Add / change a condition | edit 2 places, hope | edit **1 place** → both apps inherit | zero copy-paste bugs |
| Proof it's right | "trust me" | **400-case answer-key + TradingView exact values**, CI-locked | machine truth, not a promise |
| If someone breaks it | might slip through | **CI blocks the merge** | can't regress silently |

**The journey — all merged:** Phase 0 (golden answer-key, #343) → Phase 1 (Rust
crate, #348) → Phase 2a (Python binding, #349) → Phase 2b (BruteX delegates, #350)
→ Phase 2c (production-path parity + bindings, #353) → Phase 2d (pivot/Fib math
single-source, #354).

---

## 2. Workspace health — verified green (2026-06-04)

| Check | Result |
|---|---|
| Full suite (unit + integration + property + golden) | **3,917 passed, 31 skipped, 0 failed** |
| O(1) benchmark gates | **62 passed**, 2 skipped (M4-only), 1 xfail (honest 33B-SLA) |
| Smoke (pre-push) | **12 passed** |
| `ruff check` + `ruff format --check` | clean (406 files) |
| `mypy --strict brutex` | **0 issues** (166 files) |
| `pyright brutex tests` | **0 errors** |
| Rust `trading-core` (fmt + clippy + golden) | pass (400/400 bit-identity) |
| Rust `fno-math` | **496 tests pass** |
| Python↔Rust vocab parity | **400 / 400, 0 mismatches** |

Skips/xfail are all documented roadmap stubs (Phase-2 property invariants, M4-only
absolute-throughput, the honest 33B-SLA `xfail`) — none are breakage.

---

## 3. The O(1) scorecard (your #1 demand — no illusion)

**What is genuinely PROVEN on every PR** (shared CI, required `ci-ok` gate, a real
`p99(N_big)/p99(N_small)` ratio assertion that fails on a linear-scan regression):

| Hot path | O(1) proven? |
|---|---|
| Mask AND evaluation (u64, vectorized) | ✅ |
| 52-condition `evaluate_atoms` (one Rust call/bar) | ✅ |
| Combo dedup — Bloom `seen` + LMDB `contains` | ✅ (strongest path) |
| Result append + `WorkItemStore` get/exists | ✅ |
| Rate-limiter `acquire` | ✅ |
| Dashboard **cached**-summary read | ✅ |

**The honest caveats (stated, not hidden):**

| Gap | Reality |
|---|---|
| The decreed **1.1× ceiling** | 🟡 **Dormant.** Per-PR gates run at robust **5–10×**; the strict 1.1× tier runs ONLY on a self-hosted M4-Pro runner that **is not registered** → it proves nothing today. The O(1) *shape* is proven; the exact 1.1× *constant* is aspirational. |
| **Bar lookup** (random access) | 🔴 **Biggest gap.** No O(1) accessor exists; `ReplayFeed` does an O(n) eager materialization; the only "bar lookup" bench is a synthetic dict that never touches the feed; the named instrumented accessor (Property P-06) is a `pytest.skip` stub. |
| **symbol→security_id mapping** | 🟡 Claimed O(1) (a CPython dict — plausible) but **zero bench**. |
| **Quota tracker / instrument-row uniqueness** | 🟡 Credible O(1), **unbenched** on shared CI. |
| Uncached `/db` scan | 🔴 Inherently O(rows); the read/write split is a **concurrency** guarantee (proven by an integration test), not an O(1)-latency gate. |

---

## 4. "Comprehensively automated" — the honest envelope

| Dimension | Reality |
|---|---|
| Total tests | **~3,558** across 225 files (unit/integration/property/golden/bench/smoke/stress/redteam) |
| Blocking CI gates | **26 jobs** in the required `ci-ok` aggregate (mypy+pyright strict, ruff, bandit, CodeQL, semgrep, trivy, SBOM, vulture, duplication, rust, golden, …) |
| Coverage | global floor **90%** (not 100%); **103 modules** held at strict **100% line+branch**; 8 orchestrator modules excluded (PyO3 ABI) |
| Mutation testing | per-PR on **`state.py` only** (≥98%); broader Python + Rust mutation is **weekly/advisory** |
| Fuzzing | 🔴 **stub only** (README, zero targets, no CI) |
| Stress / soak | 1 nightly test (not per-PR) |
| Standards decree-vs-reality | repo's own honest count: **15 🟢 fully-enforced / 2 🟡 partial / 1 📋 roadmap / 3 👁️ manual of 21 (~71%)** |

**Verdict:** the automation is genuinely broad and real for the code that exists —
far above typical — but the decree's absolutes ("100% always", "real-time
guaranteed") are a tracked **target**, not a current fact. We never claim the
absolute; we show the gate.

---

## 5. The road to "find the top combo" (your real goal)

**Honest verdict: every brick of the funnel is built and tested — but the funnel is
not yet assembled.** There is no code path today that turns a strategy mask + real
bars into a *ranked combo list*.

| Pipeline stage | Status |
|---|---|
| Ingest (Dhan → Parquet/DuckDB lake) | ✅ BUILT |
| Resampling (1m → 21 timeframes) | ✅ BUILT |
| Indicators (EMA, pivots, Fib, day-extremes, PDH/PDL) | ✅ BUILT (pivot/Fib Rust-routed) |
| **Stage 1A** (per-bar 52-condition u64 masks) | ✅ BUILT (most complete) |
| Combo dedup (Bloom + LMDB) | ✅ BUILT |
| Supermask Rust kernel (AND-count) | ✅ BUILT |
| Trade simulation (SL/TP, 15:20 forced exit) | ✅ BUILT |
| Metrics (win-rate, profit-factor, Sharpe, drawdown) | ✅ BUILT |
| Results store (Parquet sink + top-K query) | ✅ BUILT |
| **The Stage 1B/2/3/4 funnel — the connective tissue** | 🔴 **NOT BUILT** |
| **`ranking/` (gates + composite score)** | 🔴 **spec text only, zero code** |
| `brutex sweep` CLI command | 🔴 **does not exist** |

**The single missing seam:** converting a mask → a per-bar `entry_signals: bool[]`.
Today there is `count_supermasks` (aggregate counts, for coarse pruning) and
`evaluate_mask` (one bar) — but **nothing builds the full signal stream the trade
simulator needs**, so Stage-1A's masks and the simulator literally cannot be joined.

**The next build (in flight):** `brutex/engine/sweep_driver.py` — a
`one combo → one ranked ResultRow` walker: `bar_bits` → **new `mask_to_entry_signals`**
→ `simulate_trades` → `compute_metrics` → implement the written **ranking** formula
→ `ResultsStore` top-K. Run on SENSEX 1m = **the first genuine ranked top-combo
table.** Statistical robustness (Stage-1B coarse pre-filter, then DSR/CPCV) layers on
afterward without changing this contract.

---

## 6. The honest one-liner

> **The market-reading brain — the 52 conditions + pivots + Fibonacci — is now ONE
> compiled Rust core, proven bit-identical to the chart and to a frozen answer-key,
> CI-locked so it can never drift.** The brute-force *sweep* that ranks the
> top-performing combo is the next build (every component exists; the driver that
> joins them is in flight). EMA / day-extremes stay in Python on purpose (cheap,
> stateful — Rust FFI per bar would be slower, not faster). TickVault adoption is
> not started. We state what is green, what is partial, and what is out of scope —
> never an illusion.
