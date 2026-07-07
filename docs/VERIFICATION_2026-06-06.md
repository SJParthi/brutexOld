# BruteX Workspace Verification — 2026-06-06

> **What this is:** a from-scratch, machine-checked health + readiness
> baseline of the *entire* BruteX workspace, run on the current branch tip,
> with the four plain-English comparison tables that were requested.
>
> **Honesty contract (CLAUDE.md Golden Rule #10):** every number below is a
> **real measured result** from a command executed in this session — not a
> slogan, not an estimate, not inherited from an older doc. Where a thing is a
> *target* or *not yet proven*, it is labelled 📋 **TARGET**, never ✅.
>
> **Verified tip:** `e853096` — *"fix(adapter/dhan): deeper timeout floor so a
> rollingoption storm eases past 3 r/s (#414)"*. Branch
> `claude/beautiful-ptolemy-UjvNC` (== `origin/main` at run time, 0 ahead / 0
> behind). Toolchain: Python **3.12.3** venv, Rust stable, maturin abi3.

---

## 0. TL;DR for a human in a hurry

- The codebase is **healthy and merge-ready**. **Zero code defects** were found.
- Every blocking quality gate that *can* run in a generic Linux container
  **passes**: lint, format, both type-checkers, security, 4,446 tests, O(1)
  benchmarks, and the Rust kernels.
- Measured **line+branch coverage = 98.79%** on this tip (the prior doc quoted
  97.84% on an older tip — this run is higher, not lower).
- The only issues encountered were **environment-setup** (a fresh container
  ships nothing installed) — every one was root-caused and fixed, and **all of
  them are already automated** by the repo's own `scripts/setup/bootstrap.sh`
  and by CI. So there is **nothing to patch in the code**.
- The big honest caveat (unchanged): the **"33 billion trials in 2–4 days"
  speed SLA is a TARGET, not proven** — it is an honest `xfail` until the
  Phase-3 sweep engine + the M4 Pro self-hosted runner land. We do not claim it.

---

## 1. The verification scoreboard — every number is from a real run today

| # | Gate | Command | Result (measured) | Verdict |
|---|------|---------|-------------------|---------|
| 1 | Lint | `ruff check brutex` | **All checks passed** (180 files) | ✅ PASS |
| 2 | Format | `ruff format --check brutex` | **180 files already formatted** | ✅ PASS |
| 3 | Types (mypy) | `mypy --strict brutex` | **Success: no issues found in 180 source files** | ✅ PASS |
| 4 | Types (pyright) | `pyright brutex` | **0 errors**, 3 warnings, 0 info | ✅ PASS |
| 5 | Security | `bandit -r brutex` | **0 high / 0 medium / 0 low** across 40,683 LOC | ✅ PASS |
| 6 | Tests (functional) | `pytest -m "not stress and not network and not slow"` | **4,446 passed · 33 skipped · 0 failed** (224 s) | ✅ PASS |
| 7 | Coverage (line+branch) | `coverage report` | **98.79%** — 11,824 stmts / 126 missed · 2,652 branches / 41 partial | ✅ PASS (≥90% floor) |
| 8 | O(1) hot-path benches | `pytest tests/bench` | **62 passed · 2 skipped (M4-only) · 1 xfail (honest 33B SLA)** | ✅ PASS |
| 9 | Rust kernel — `trading_core` | `cargo test` | **4 passed** (2 unit + 2 golden) | ✅ PASS |
| 10 | Rust kernel — `fno_math` | `cargo test` | **497 passed · 1 ignored** | ✅ PASS |

**Why some tests skip (all legitimate, none hidden):**
- 33 functional skips = documented **Phase-2 roadmap stubs** in
  `tests/property/test_invariants_catalog.py` (PnL/resampler/EMA/Pivot-Fib
  property harnesses, the P-06 *instrumented-bar-accessor* strengthening,
  holiday/session calendars) — they are `pytest.skip("Phase 2 … pending")`,
  exactly as `CLAUDE.md` §4 describes.
- 2 bench skips = the **absolute** throughput / bulk-upsert gates that only run
  on the M4 Pro self-hosted runner (`BRUTEX_M4PRO_ABS=1`); the *ratio* O(1)
  gates run everywhere and passed.
- 1 bench `xfail` = the **"33B trials within SLA"** projection — honest-xfail
  until the full engine lands (DEC-P0-062).

### Test inventory (what exists, collected counts)

| Type | Collected | What it catches |
|------|----------:|-----------------|
| Unit | 3,861 | logic bugs |
| Integration | 293 | wiring / contract bugs |
| Property (Hypothesis) | 168 | edge cases you didn't think of |
| Golden | 36 | byte-level output drift |
| Benchmark | 65 | O(1) regressions |
| Smoke | 12 | fast pre-push sanity |
| Red-team | 52 | gate-integrity (plants bugs, asserts gates catch them) |
| Stress / soak | 1 | ~~24 h sustained leak (nightly)~~ **CORRECTED (DEC-SOAK-REAL-TIER-001 #1540): the nightly was a light sustained-leak regression, never a 24 h soak — its 60-min timeout structurally forbade one; the real multi-hour soak now runs on `soak-selfhosted.yml`** |
| **Rust (both crates)** | **501** | kernel correctness + golden parity |

---

## 2. Root-cause log — every issue found, and how it was resolved

Per the directive (*identify root cause → resolve completely → revalidate*).
**All four were environment-setup, not code. All are already automated.**

| # | Symptom (measured) | Root cause | Fix applied | Revalidated | Already automated by |
|---|--------------------|-----------|-------------|-------------|----------------------|
| 1 | `python3` had no pytest/numpy | Fresh container ships nothing | Created **3.12 venv**, `pip install -e ".[dev]"` | imports OK | `scripts/setup/bootstrap.sh` |
| 2 | 64 pytest collection errors + 7 mypy errors | `fno_math` / `trading_core` **PyO3 kernels not built** | `maturin develop --release` both crates | 64→13 errors; mypy 7→0 | `bootstrap.sh` steps 5–6; CI `pip install ./rust/*` |
| 3 | 13 collection errors (`import polars`) | `[data,ui]` extras absent (polars/duckdb/fastapi) | `pip install -e ".[data,ui]"` | 13→0 errors | `bootstrap.sh` (installs `[dev,data,ui]`) |
| 4 | Container Python is 3.11; project needs 3.12 | base image default interpreter | used `/usr/bin/python3.12` for the venv | full suite green | `preflight.sh` (checks python3.12) |

**Conclusion:** after a standard `make setup`, a clean machine reproduces the
green scoreboard in §1. There is **no source change required** — confirmed that
`.github/workflows/ci.yml` already builds both kernels
(`pip install ./rust/fno-math`, `pip install ./rust/trading-core`).

---

## 3. Comparison Table A — The O(1) reality (claim vs. math, no illusion)

You asked for O(1) *everything*. Here is the honest split: what is genuinely
constant-time (and CI-gated), and what **cannot** be O(1) because mathematics
forbids it. Claiming the impossible ones would be the exact hallucination
Golden Rule #10 bans.

| Operation | Honest complexity | O(1)? | Proven by (in this repo) |
|---|---|:---:|---|
| Uniqueness check (combo seen before?) | **O(1) expected** | ✅ | blake3 key → Bloom membership; bench `test_contains_*` |
| Deduplication (no double-write, no lost bar) | **O(1) expected** | ✅ | Bloom L1 → LMDB L2 confirm; `test_append_dup_noop_*` |
| Lookup / mapping (symbol→ID, bar, indicator) | **O(1)** | ✅ | hash map / indexed array |
| Cache access (membership tier) | **O(1) per op** | ✅ | memory-mapped LMDB; total state O(n), per-op RAM O(1) |
| Routing / indexing (strategy mask eval) | **O(1)** | ✅ | single `u64 AND`; supermask kernel bench |
| Result append / write | **O(1) amortized** | ✅ | append-only store; `test_record_hit_*` |
| Operational latency per hot path | **O(1)**, ratio-gated | ✅ | `p99(big)/p99(small)` bench gate (62 passed today) |
| Memory growth **per operation** | **O(1)** | ✅ | state off the Python heap, mmap tier |
| — | — | — | — |
| Full sweep over ~33B trials | **O(N)** | ❌ | you must touch every trial; constant-time can't read N inputs |
| Sorting / ranking the survivors | **O(n log n)** | ❌ | comparison-sort lower bound |
| Total storage for kept results | **O(unique results)** | ❌ | you must store what you keep |

**The one honest sentence:** *every per-item hot path is O(1) and CI-enforced;
the whole 33B-trial sweep is O(N) by the laws of computation — there is no
honest way to make reading N things take constant time.*

---

## 4. Comparison Table B — Merge-readiness gates (plain English)

The "factory that checks itself." Source of truth: `docs/STANDARDS_100PCT.md`
§1 (21 sub-rows) and `docs/MERGE_READINESS_GUARANTEE.md` §3 (16 pipeline
stages). Status quoted verbatim from those files.

**STANDARDS matrix — the official tally (21 sub-rows):**

| Status | Count | % | Plain meaning |
|---|---:|---:|---|
| 🟢 Fully enforced (CI blocks merge today) | **15** | 71% | a red one = no merge, automatically |
| 🟡 Partial (runs, advisory or below target) | 2 | 10% | rows 1–2: coverage line/branch threshold |
| 📋 Roadmap (planned, not yet wired) | 1 | 5% | row 14: absolute ≥50 M/s throughput canary |
| 👁️ Manual (PR template + human reviewer) | 3 | 14% | rows 10/17/18: bug-fix-regression, no-hallucination, decision-ID |

**The merge pipeline (16 stages) — what runs before anything merges:**

| Stage | Gate | Status |
|---|---|:---:|
| Static analysis | ruff + pylint(PL) | ✅ |
| Security scan | bandit + CodeQL + Semgrep + Trivy + gitleaks + pip-audit | ✅ |
| Duplicate / dedup | jscpd + Bloom/LMDB tests | ✅ |
| Type safety | mypy --strict + pyright | ✅ |
| Coverage | line+branch on changed `brutex/` | ✅ |
| Mutation | mutmut / cargo-mutants ≥85% (critical) | 🟡 |
| Performance / O(1) | p99 ratio bench gate | ✅ |
| Build + deps | editable build + CycloneDX SBOM + audit | ✅ |
| Unit/integration/property/golden | pytest + hypothesis | ✅/🟡 |
| Load/stress/soak/chaos/race | ~~nightly soak partial; rest roadmap~~ **CORRECTED (DEC-SOAK-REAL-TIER-001 #1540): the nightly is a ~5-min stress regression gate, never a soak; the real multi-hour soak runs weekly on `soak-selfhosted.yml`; chaos/race stay roadmap** | 📋 |
| DB/API/frontend validation | dashboard + integration tests | 🟡 |
| Runtime / prod monitoring | activates when a service is deployed | 🚫→📋 |
| Docstrings/typos/complexity/dead-code | interrogate + codespell + radon + vulture | ✅/🟡 |
| Commits + decision-ID + citations | format + DEC-ID + no-hallucination scans | ✅ |
| **Merge validation** | **`CI OK (aggregate gate)` = required check** | ✅ |
| Z+ discipline | `zplus-pr-guard` (6 markers) | ✅ |

**Tally: 10 ✅ enforced · 4 🟡 partial · 1 📋 roadmap · 1 🚫→📋 (deploy-time).**
The merge law (`MERGE_READINESS_GUARANTEE.md` §0):
**Correctness > Reliability > Recoverability > Observability > Performance > Scalability.**

---

## 5. Comparison Table C — BruteX vs. the field (honest, cited)

Competitor figures are **quoted from public sources as cited in
`docs/TRACK1_PERFORMANCE_BUDGET.md`** — they are *not* independently re-measured
by BruteX, and the units differ (configs/sec vs trials/sec vs rows/sec), so
read this as *orientation, not a benchmark shoot-out.*

| Engine | Speed (as cited) | Correctness story | Honest BruteX note |
|---|---|---|---|
| DIY pandas + loop | ~hundreds/sec, slows as data grows (O(n)) | "you hope so" | BruteX: O(1) hot paths, 4,446 tests |
| Backtesting.py | ~1K trials/sec (cited) | some tests | — |
| QuantConnect Lean | hundreds trials/sec (cited) | framework tests | — |
| Jesse-AI | 16× speedup over baseline (cited) | framework tests | — |
| vectorbt PRO | ~667 configs/sec (cited) | framework tests | different unit (configs≠trials) |
| NautilusTrader (Rust) | 5M **rows/sec ingestion** (cited) | strong | ingestion≠trial-eval; not comparable |
| **BruteX** | **80–150K trials/sec 📋 TARGET** | **98.79% cov · hash-chained audit · O(1)-gated** | **NEVER measured yet — honest `xfail`** |

**What BruteX genuinely does differently (live today, each a gate):**
runtime look-ahead guard, blake3 reproducibility key, Bloom+LMDB O(1) dedup
with zero false-drops, money-2dp / stats-full-precision split, 24-job aggregate
merge gate, and a 52-test red-team harness that plants bugs to prove the gates
bite. **What it does NOT yet do:** the full Stage-1A→4 sweep at the target
speed — that is Phase 3.

---

## 6. Comparison Table D — Track 1 feature coverage (built vs. roadmap)

From a code-level sweep of `brutex/` cross-referenced with
`docs/TRACK1_PHASE_PLAN.md`. "Implemented" = real working code with tests, not a
stub.

| Area | State | Evidence |
|---|:---:|---|
| Dhan REST adapter (auth · client · rate-control · ingest · instruments) | ✅ Implemented | `adapters/dhan/*` — adaptive 5→3 r/s bucket, SSRF guard, CSV resolver |
| Data lake (Parquet writer · DuckDB catalog · instrument store) | ✅ Implemented | `data/*` — deterministic zstd, merge-on-write, per-partition lock |
| Feed (replay · resampler · look-ahead guard) | ✅ Implemented | `feed/replay.py:ensure_no_lookahead` is a **live runtime guard**, not a stub |
| Indicators (pivots · fib · EMA · day-extremes) | ✅ Implemented | `indicators/*` — pivots bit-exact vs TradingView via Rust core |
| 52-condition vocabulary engine | ✅ Implemented | `vocabulary/evaluator.py` → shared `trading_core` Rust (DEC-SHARED-CORE-001) |
| Sweep primitives (u64 mask · Bloom+LMDB 4-tier dedup · Stage-1A builder) | ✅ Implemented | `engine/*`, `orchestrator/bloom.py` — O(1)-gated |
| Execution simulator (signal-only) | ✅ Implemented | `execution/simulator.py` — lot/slippage/cost, 15:20 forced exit |
| Metrics (DSR · SPA · CPCV · Monte-Carlo · WFO · PBO) | ✅ Implemented | `metrics/*` — every formula cited to its paper |
| Ranking (5 hard gates + composite) | ✅ Implemented | `ranking/__init__.py` — lockbox term deferred to Phase 3 |
| Dashboard (FastAPI + WebSocket, one-click pull) | ✅ Implemented | `dashboard/app.py` — `make run` → :9080 |
| Orchestrator (FSM · audit hash-chain · rate limiter · replay) | ✅ Implemented | `orchestrator/*` — tamper-evident JSONL |
| **Full Stage-1A→1B→2→3→4 cascade sweep** | 📋 **Next (Phase 3)** | primitives exist + O(1)-gated; the cascade itself is the upcoming build |
| **"33B trials in 2–4 days" SLA** | 📋 **Target, unproven** | honest `xfail` until engine + M4 Pro runner land |
| Live trading / order placement | 🚫 Out of scope | Track 1 is signal-only research |

---

## 7. The honest envelope (Golden Rule #10)

What is **true and green today:** the entire quality/verification surface — all
ten gates in §1, 98.79% coverage, 0 type errors, 0 security findings, O(1)
hot-path benches, both Rust kernels, ~4,950 tests across 9 categories.

What is **partial:** mutation ≥85% (critical modules only), the absolute
≥50 M/s throughput canary (needs the M4 Pro runner), some DB/API/frontend
validation rows.

What is **not yet built / not claimed:** the full sweep cascade engine, the
33B-trial speed SLA (honest `xfail`), live load/chaos/soak beyond the nightly,
and anything that activates only on a deployed service.

**No "100% / fully / guaranteed" is claimed for any 🟡/📋/🚫 row above.** Every
✅ in §1 is backed by a command output captured in this session.

---

*Generated 2026-06-06 against tip `e853096`. Method: clean 3.12 venv → build
both Rust kernels (maturin) → install `[dev,data,ui]` → run every gate that a
generic Linux container can run, capture the real output, and report it without
embellishment.*

---

## 8. Reproduce this baseline (one block)

```bash
make setup                 # 3.12 venv + [dev,data,ui] + both Rust kernels (maturin)
. .venv/bin/activate
ruff check brutex && ruff format --check brutex
mypy --strict brutex && pyright brutex
bandit -r brutex
pytest -m "not stress and not network and not slow" --cov=brutex --cov-branch
pytest tests/bench
( cd rust/trading-core && cargo test ) && ( cd rust/fno-math && cargo test )
```
