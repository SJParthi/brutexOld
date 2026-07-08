# BruteX — Guarantee Charter (the honest scoreboard)

> **Purpose.** The operator demands "100% — extreme, comprehensive,
> automated, guaranteed, with proof, no hallucination" across code,
> testing, automation, GPU, hardware, infra, and environment. This doc
> is the single honest scoreboard that answers, for every demand:
> **is it GUARANTEED today, or is it still a PROMISE?**
>
> **Status:** living doc. Created 2026-05-28 (DEC-GUARANTEE-CHARTER-001).
> Companion to `docs/STANDARDS_100PCT.md` (the CI-gate enforcement matrix)
> and `docs/STANDARDS_100PCT_HONEST_AUDIT.md`.

---

## 0. How to read this (the only honest definition of "guarantee")

My **word** is hallucination-prone. A **CI gate is machine proof** — it
*blocks the merge* if the thing is not true, on every PR forever
(including every Claude-session PR). So:

> **"Guaranteed" = wired as a fail-closed CI gate that is green.**
> Anything not yet a gate is a **PROMISE**, not a guarantee — and this
> doc says so plainly (Golden Rule #10: honest limits).

Legend:

| Mark | Meaning |
|---|---|
| 🟢 **GUARANTEED** | a fail-closed CI gate enforces it on every PR today |
| 🟡 **PARTIAL** | runs but advisory / below-target / not fully wired |
| 🔴 **PROMISE** | designed/desired but NOT enforced or NOT built yet |
| 👁️ **HUMAN** | reviewer-enforced; cannot be 100% automated |

**Today's honest headline: ~71% of the CI-quality demands are 🟢; the
runtime-automation + GPU + AWS demands are mostly 🔴 (not built).**
Source: the 3-agent + 2-agent audits run in this session (2026-05-28).

---

## 1. Code quality & testing

| Demand | Status | Proof / closing path |
|---|---|---|
| Lint (ruff E+W+F+I+N+PL) | 🟢 | `ci.yml::lint` blocks merge |
| Type-check (mypy --strict + pyright) | 🟢 | `ci.yml::type` blocks (full repo) |
| Security scanning | 🟢 | bandit + CodeQL + Semgrep + Trivy + gitleaks all block |
| Code duplication | 🟢 | jscpd @3% blocks |
| Dead-code | 🟢 | vulture blocks |
| Docstring coverage | 🟢 | interrogate `--fail-under=100` |
| Dedup / uniqueness | 🟢 | Bloom L1–L3 + property tests (DEC-L3-REPLAY-ORACLE-001) |
| Testing types: unit/property/golden/bench | 🟢 | all block merge |
| **Code coverage** | 🟡 | 90% global floor + **51 modules @100%**; 8 orchestrator modules blocked by `fastbloom_rs` ABI3 (DEC-COV-BACKFILL-LIMIT-001). Closing: vendor swap / per-test subprocess CI |
| **Mutation testing** | 🟡 | mutmut on `state.py` (98%) + nightly; not all modules. Closing: extend gate |
| **Bug-fix regression** | 🟡 | `validate_bugfix_regression.py` scan (partial). Closing: full automation |
| **Stress / soak** | 🟡 | `stress-nightly.yml` (~5-min ubuntu regression gate; refuses `STRESS_EVENTS > 200000`) + `soak-selfhosted.yml` (real multi-hour soak on the self-hosted M4-Pro — weekly cron / `workflow_dispatch(soak_minutes)`, duration-derived timeout; DEC-SOAK-REAL-TIER-001 #1580 — the prior “24h via dispatch” claim was FALSE and is retracted) |
| **O(1) latency** | 🟡 | ratio gates LIVE on 5 hot paths; absolute 1.1× needs M4 Pro runner (DEC-M4PRO-ABSOLUTE-001) |
| Code-review depth | 👁️ | reviewer + Z+ 6-marker PR guard (blocks) — judgement can't be 100% auto |
| No-hallucination (every claim cited) | 👁️🟡 | `validate_no_hallucination.py` scan blocks uncited external claims; correctness is reviewer-judged |

---

## 2. Automation & runtime (the app running itself)

| Demand | Status | Proof / closing path |
|---|---|---|
| Auto-merge on green CI | 🟢 | `auto-merge.yml` (proven on ~17 PRs this session) |
| Audit hash-chain (every material action) | 🟢 | `orchestrator/audit.py` + `brutex doctor`; persistent in `~/.brutex/audit/` when a run executes |
| SessionStart context hook | 🟢 | `.claude/hooks/session-start.sh` prints branch/commits/decisions |
| **One-click run** (clone → ▶ Run → dashboard) | 🟡 **landing** | PR #174: `python -m brutex run` + `make run` + IntelliJ config + `ui` extra (DEC-ONECLICK-RUN-001) |
| **Logs to a file** (not just stderr) | 🔴 | structlog → stderr only today. Closing: log-file sink → `~/.brutex/logs/run-<ts>.log` (next PR) |
| **Live monitoring + Telegram alerts** | 🔴 **promise** | `notifications/telegram.py` + `watchdog.py` + `StandardMonitor` EXIST but are UNPLUGGED (fire only via manual `brutex monitor`). Closing: DEC-ONECLICK-RUN-002 (wire into `brutex run`) |
| **Auto-ingest on start** | 🔴 | fetcher not built (Phase 2, gated by Phase 0b + creds) |
| **Auto-sweep** | 🔴 | brute-force engine not built (Phase 3) |
| **Auto-resume on crash** | 🔴 | no checkpoint-resume; manual restart |
| **Scheduled (zero-click daily)** | 🔴 | no launchd/cron. Closing: macOS launchd plist |

---

## 3. Claude-session / data access ("every session does everything automatically")

| Demand | Status | Proof / closing path |
|---|---|---|
| Claude session auto-reads logs / queries / DBs | 🔴 **promise** | no MCP bridge — a session reads only via Bash/Read in its sandbox. Closing: a `brutex`-MCP server exposing audit/lake/runtime as tools |
| Claude session auto-accesses AWS | 🔴 **promise** | no AWS anywhere (see §5) |
| Per-session project context | 🟢 | SessionStart hook (read-only summary) |

---

## 4. GPU & performance

| Demand | Status | Proof / closing path |
|---|---|---|
| **GPU runs everything (Metal)** | 🔴 **promise** | Phase 3.5 Metal GPU kernel is DESIGNED (`TRACK1_OPTIMIZATIONS.md §3`, superseded by ADR-0004) but **does NOT exist**. *[Corrected 2026-07-06, DEC-GPU-GATE-RECONCILE-001: the Rust CPU hot kernel is BUILT + MEASURED at microbench scale (1,575 M ops·s⁻¹ on a 94K-bar/~752 KB cache-resident bitset, single-core, dev/CI runner — MIGRATION.md #411; Rayon scaling on a 4-core dev/CI container #417; SLA-gated on the M4-Pro runner) — the prior "empty stub" claim was stale since #411.]* GPU (Metal/MLX/CuPy) code does NOT exist — gated behind ADR-0004 §6 (retained fail-closed precondition, mechanically pinned by `tests/unit/test_gpu_gate_drift_pin.py`). No GPU code today. |
| Rust hot kernel (PyO3; scalar-autovec + Rayon, not hand-NEON) | 🟢 | BUILT + MEASURED *[corrected 2026-07-06, DEC-GPU-GATE-RECONCILE-001 — the prior "scaffold / not implemented (G-01)" claim was stale since #411]*: `count_supermasks_u64s` 964 → 1,575 M ops·s⁻¹ bit-exact (a 94K-bar/~752 KB cache-resident MICROBENCH, single-core, dev/CI runner — MIGRATION.md #411, `tests/bench/test_supermask_kernel_bench.py`; the ADR-0004 §6-scale ~770 MB multi-threaded baseline measurement stays owed by the future §6 benchmark); resident bars (#413); Rayon multi-core measured on a 4-core dev/CI container (#417); 33B SLA a hard gate on the M4-Pro runner (`tests/bench/test_full_sweep_extrapolation.py`) |
| O(1) per hot path | 🟡 | ratio gates live (see §1) |
| Throughput canary | 🟡 | ratio gate live; absolute 50 M/s needs M4 Pro runner |

> **Honest note (Golden Rule #10):** "our GPU runs everything" is the
> single biggest gap between the vision and reality. There is **no GPU
> acceleration today** — it is Phase 3.5 future work that depends on the
> Rust kernel (Phase 3) existing first *(that dependency is satisfied
> since #411/#417; GPU itself stays gated behind ADR-0004 §6's retained
> fail-closed precondition — DEC-GPU-GATE-RECONCILE-001, 2026-07-06)*.
> Anyone claiming the GPU runs the
> sweep today would be selling an illusion.

---

## 5. Hardware, infra & environment

| Item | Status | Reality |
|---|---|---|
| Hardware envelope | 📋 documented | M4 Pro 14-core / 48 GB / 8 TB SSD (`CLAUDE.md §8`). **External SSD not working → running on local MacBook Pro** (operator 2026-05-28) |
| Database server | 🟢 **none needed** | DuckDB + LMDB + Parquet are **embedded** (in-process, file-based) — nothing to start/manage |
| Docker | 🟢 **deliberately none** | blocks Apple Metal GPU + adds overhead that breaks the SLAs; embedded DB needs no container. Native venv is the runtime |
| Dependency env | 🟡 | `pip install -e ".[dev,data,ui]"` today; `uv` is in the stack target |
| **AWS runtime** | 🔴 **promise** | zero AWS code; config locked to `127.0.0.1`. Mac-only today. AWS is a future cloud-deploy option, not built |
| Reproducibility (blake3 9-field hash) | 🟢 | `combo_hash` 9-field BLAKE3 (DEC-P0-060); deterministic |

---

## 6. The honest bottom line + how your 100% becomes REAL

- **Today:** ~71% of code-quality demands are 🟢 machine-guaranteed. The
  runtime-automation, live-monitoring, GPU, AWS, and Claude-auto-access
  demands are 🔴 **promises** — designed, not built.
- **The ratchet (the only honest path to 100%):** each 🔴/🟡 row becomes
  a fail-closed CI gate or a shipped+tested capability via its own PR.
  When the row flips 🟢, that item is **provably guaranteed forever** —
  not by my word, by the machine. You watch this table climb.
- **No row is marked 🟢 unless a gate enforces it.** That is the
  anti-hallucination contract.

### Closing-PR backlog (the path to green)

| Next PR | Flips to 🟢 |
|---|---|
| Log-file sink | logs-to-file |
| DEC-ONECLICK-RUN-002 (wire monitor into `run`) | live monitoring + Telegram |
| Groww fetcher + verification | auto-ingest (partial) + the timeframe/timestamp proof |
| launchd plist | scheduled zero-click |
| `brutex`-MCP server | Claude-session auto-access to logs/DBs |
| ~~Rust kernel (Phase 3)~~ *(landed — #411/#413/#417)* → Metal GPU *(dormant: ADR-0004 §6 retained precondition, DEC-GPU-GATE-RECONCILE-001; GPU PR must land the §6 benchmark first)* | GPU runs the sweep |
| M4 Pro self-hosted runner | O(1) absolute + throughput-canary 🟢 |
| AWS deploy (if/when wanted) | cloud runtime |

**Decision ID:** DEC-GUARANTEE-CHARTER-001. See `docs/MIGRATION.md`.
