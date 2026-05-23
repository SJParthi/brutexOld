# BruteX — Claude Code Project Rules

> Persistent instructions for Claude Code working in this repository.
> Every Claude session in `bruteX/` must read and honor this file.
>
> **Authority order:**
> 1. This file (`CLAUDE.md` — 12 Golden Rules)
> 2. Defaults
>
> The previous DEC-PROTOCOL-001 layer (tickvault adaptation doc +
> auto-loaded protocol bundle) was RETIRED in DEC-P0-026 (PR #48) —
> BruteX is a self-contained Track 1 backtest engine with no
> co-development relationship to any other repo.

_Last updated: 2026-05-18. DEC-P0-026 (PR #48) retired the tickvault
co-development layer. 2026-05-15: the broker → Dhan migration;
Track 1 spec locked after 4-agent hostile-review pass._

---

## 1. Project Identity

- **Name:** BruteX
- **Purpose:** Extreme drill-down brute-force backtesting engine for
  Indian **spot indexes** (NIFTY 50 / BANKNIFTY / SENSEX) + **India
  VIX** (volatility index). Track 1 is signal-only research; the
  Options Track translates winning signals to options for execution
  realism.
- **Data source (Track 1):** **Dhan Historical Data API** — only two
  surfaces:
  1. `POST /v2/charts/intraday` (1-minute OHLC; other timeframes
     resampled deterministically from 1m)
  2. `POST /v2/charts/historical` (daily OHLC, used for PDH/PDL anchors)
- **Out of scope (Track 1):** Live feed, WebSocket, order placement,
  portfolio, funds, F&O contracts, options, commodity segment, BSE-cash
  segment, multi-user SaaS.
- **Active branch:** `claude/backtest-system-spec-ITI4Y`

---

## 2. Golden Rules (never violate)

1. **No hallucination.** Every factual claim about Dhan, NSE, BSE, NIFTY
   composition, brokerage, taxes, or any external system must be
   traceable to a verified source. If unsure, say "unverified" and stop.
2. **Single source of truth.** All Track 1 specs live under
   `docs/TRACK1_*.md` + `docs/track2/TRACK2_*.md`. Every PR references
   a section + decision ID from `docs/MIGRATION.md`. The legacy
   `docs/REQUIREMENTS.md` is a retired stub (banner only) — do not
   read or derive new requirements from it.
3. **Scope lock.** Track 1 = Dhan + spot indexes + India VIX (NIFTY 50,
   BANKNIFTY, SENSEX, INDIA VIX). **India VIX is REFERENCE ONLY** — it
   is fetched + stored and stamped as `vix_at_entry` / `vix_at_exit` on
   every trade, but is NEVER part of brute-force pattern combinations
   or the 52-condition vocabulary. 21 timeframes (1m base + 19 derived
   intraday + 1d). Rolling `2021-01-01 → today` range. No futures, no
   options, no F&O contracts. Do not silently expand scope.

   **Scope expansion (DEC-P0-063, 2026-05-22, MIGRATION.md entry
   #200):** Track 1's INGEST scope was formally expanded to additionally
   cover **200 NSE F&O eligible single stocks (spot + options across all
   4 Dhan `expiryCodes`)** for the LAKE. The **BRUTE-FORCE ENGINE is
   UNCHANGED** — it still operates only on the 3 brute-force indexes
   (NIFTY/BANKNIFTY/SENSEX); the 200 stocks join India VIX in
   `REFERENCE_ONLY` (mirrors decision #83). Stock options sit in the
   lake ready for future brute-force expansion (separate decision; not
   authorized here). The "4 index instruments / no F&O contracts"
   wording above describes the ENGINE; the LAKE is the expanded surface
   per DEC-P0-063. Full §3 / cross-spec reconciliation across the
   ~15 remaining stale-text locations is a tracked follow-up PR; until
   then DEC-P0-063 is authoritative for the stock-options expansion.
4. **Reproducibility first.** Every run is identified by `sha256(
   strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖ canonical_params
   ‖ data_snapshot_hash ‖ indicator_version ‖ code_commit_hash)`. No
   computation without that hash recorded.
5. **O(1) discipline.** Bar lookup, indicator value lookup, combo dedup
   (Bloom + LMDB), strategy mask evaluation (u64 AND), and result
   append must all be O(1). CI gates: `p99(N=1M) / p99(N=1K) ≤ 1.1`
   for each hot path. Any regression fails CI.
6. **Rate-limit safety.** Dhan Data APIs: **5 req/s** per-second cap +
   **100,000/day** quota (verified 2026-05-15 from dhanhq.co/docs/v2
   Rate Limit table). Adapter caps at 3 r/s effective (5 × 0.6 safety
   factor). Never raise without explicit user approval. Track 1's
   typical 92-chunk first-run + 4-chunk daily incremental is < 0.1% of
   the daily quota.
7. **Idempotence.** Every ingest chunk and every backtest combo must be
   rerun-safe. Same inputs → same outputs, bit-for-bit. The audit-log
   replay test is the oracle.
8. **Audit trail.** Every material action (ingest chunk, combo run,
   error, kill) appends one line to a hash-chained JSONL audit log.
   Breaking the chain fails CI.
9. **Hostile review before code.** Major design changes get a
   parallel-agent hostile review (Plan + general-purpose + Explore)
   before implementation. See the 4-agent pattern in
   `docs/TRACK1_PHASE_PLAN.md §0`.
10. **Honest limits.** If an SLA cannot be met under the current
    hardware envelope, SAY SO. No "millions in minutes" claims without
    naming the hardware AND citing public benchmarks. The 4-agent
    review on 2026-05-15 caught a 10x-overoptimistic throughput claim;
    do not repeat that pattern.
11. **COMMON · DYNAMIC · SCALABLE · AUTOMATED** (operator decree 2026-05-16).
    Every module obeys all 5:
    - **COMPREHENSIVE** — streaming + batch interface, bit-exact oracle
      test, look-ahead invariant test, serialize/restore.
    - **COMMON** — one source of truth (this repo). Every utility
      lives in exactly one place; downstream modules import. No
      copy-paste between modules.
    - **DYNAMIC** — add new indicator/TF/condition/mask without
      re-architecting. Plugin-style state classes, u64 mask vocab.
    - **SCALABLE** — same code path Day 1 (12.6M) → Phase 10 (730B).
      O(1) per-bar / per-mask / per-dedup / per-write.
    - **AUTOMATED** — 100% automated after one-time bootstrap.
      Release Please + auto-merge + watchdog + Telegram + auto-resume.
      Operator monitoring: ZERO clicks per day.
    See `docs/TRACK1_BACKTEST_SPEC.md` §"COMMON · DYNAMIC · SCALABLE ·
    AUTOMATED" for the full rule + enforcement matrix + violations list.
12. **100% ALWAYS STANDARDS** (operator decree 2026-05-16 evening).
    Every branch, every PR, every sub-PR, every commit, every task,
    every sub-task, every plan, every doc, every spec, every agent run
    obeys 18 standing requirements:
    coverage 100% line + 100% branch; mutation ≥ 85%; type-check
    (mypy strict AND pyright); static analysis (ruff + pylint + bandit);
    zero duplication; functionality + scenario completeness; bug-fix
    regression coverage; 7 test types (unit + integration + property +
    golden + bench + stress + mutation); CI every push + nightly soak;
    O(1) per hot path (p99 ratio ≤ 1.1); throughput canary ≥ 50 M/s;
    Bloom + LMDB 4-tier dedup; audit JSONL hash-chain on every
    material action; observability 4 pillars (tracking + logging +
    dashboard + alerting); 7 explicit SLAs; deep-research protocol
    (4-agent for major, 2-agent for compliance, 1-agent hostile for
    refactor); decision ID for every locked decision in MIGRATION.md.
    See `docs/STANDARDS_100PCT.md` for the full enforcement matrix +
    tooling per gate + auto-driver-test discipline. Any PR with a
    red gate is BLOCKED; operator override requires ADR.

    **Honest envelope (Golden Rule #10 + DEC-P0-040 / DEC-P0-042
    rebaselining):** The 18-requirement decree above is the **target**.
    The actual CI enforcement matrix in `docs/STANDARDS_100PCT.md` §1
    is **9 of 21 sub-rows ✅ ENFORCED + 4 🟡 PARTIAL + 5 📋 ROADMAP +
    3 👁️ MANUAL** (~22% fully enforced). Per Golden Rule #10 no PR
    may claim "100% standards compliant" without first auditing against
    that matrix. The decree-vs-reality gap is a living ratchet, not a
    bypass — every gap row tracks a dedicated follow-up PR in
    MIGRATION.md. PRs that touch a gap row MUST update the row's
    status. See `docs/TRACK1_REQUIREMENTS_FINAL.md` §2 (DEC-P0-040)
    for the full enumeration.

---

## 3. Branching and Commits

- **Active feature branch:** `claude/backtest-system-spec-ITI4Y`
- **Never push to `main`** without explicit user instruction.
- **Commit style:** Conventional Commits.
  - `docs(track1): add 52-condition vocabulary appendix`
  - `feat(adapter/dhan): implement intraday OHLC chunking`
  - `fix(engine): O(1) regression in bar lookup`
  - `test(property): add DSR effective-N invariant`
  - `chore(claude): update CLAUDE.md rules`
- **Never skip hooks** (`--no-verify` is forbidden unless the user
  explicitly requests).
- **Never force-push** shared branches.
- **Never amend** published commits; always create new commits.

---

## 4. Coding Standards (when code phase starts)

- **Language:** Python 3.12 primary; Rust (PyO3) for hot kernels; no
  other languages without ADR.
- **Formatters:** `ruff format` (not black); `ruff check --fix`.
- **Types:** `mypy --strict` AND `pyright` both must pass.
- **Linters:** `ruff`, `pylint` (warnings-as-errors), `bandit` (security).
- **Tests:** `pytest`, `hypothesis` (property), `pytest-benchmark`
  (SLA), `mutmut` (mutation), `cargo test`, `proptest`,
  `cargo-mutants`, `cargo fuzz`.
- **Coverage gates:** 100% line + 100% branch on `brutex/` package.
  Mutation score ≥ 85% (Python AND Rust).
- **Docstrings:** every public symbol. NumPy style.
- **No `print`** — always `structlog`.
- **No bare `except`** — specific exceptions only.
- **No global mutable state** — inject dependencies.
- **No `pandas` in hot paths** — use `polars` or `numpy` + `numba`
  or Rust.
- **No look-ahead** — at bar N, the engine may only access bars 0..N.
  Property P-06 (instrumented bar accessor) enforces this.

---

## 5. Directory Conventions

```
bruteX/
├── CLAUDE.md                       ← this file
├── README.md
├── .claude/
│   ├── settings.json
│   └── agents/
├── docs/
│   ├── TRACK1_BACKTEST_SPEC.md     ← Track 1 master spec
│   ├── TRACK1_VERIFICATION_PYRAMID.md
│   ├── TRACK1_VOCABULARY.md        ← 52 atomic conditions
│   ├── TRACK1_PHASE_PLAN.md        ← 8-12 week build plan
│   ├── TRACK1_PERFORMANCE_BUDGET.md
│   ├── TRACK1_MICROSTRUCTURE.md
│   ├── MIGRATION.md                ← decision ledger
│   ├── REQUIREMENTS.md             ← RETIRED (stub)
│   ├── REQUIREMENTS_SIMPLE.md      ← RETIRED (stub)
│   ├── ROADMAP.md                  ← RETIRED (stub)
│   ├── adapters.md                 ← RETIRED (stub)
│   ├── adr/                        ← architecture decisions
│   └── runbooks/
├── brutex/                         ← Python package
│   ├── adapters/                   ← Dhan adapter lands in Phase 2
│   ├── data/                       ← Parquet + DuckDB lake (generic)
│   ├── execution/                  ← signal-only simulator (Track 1)
│   ├── feed/                       ← replay + resampler
│   ├── math/                       ← facade to Rust hot kernel
│   ├── orchestrator/               ← FSM + audit + rate limiter
│   ├── strategies/                 ← 52-vocabulary engine (Phase 3)
│   ├── dashboard/                  ← FastAPI UI
│   ├── config/
│   ├── notifications/
│   └── observability/
├── rust/                           ← Rust hot kernel scaffold (Phase 2/3 only)
│   └── fno-math/                   ← compat types + NSE calendar; Greeks/
│                                     indicators removed in DEC-P0-025
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── property/                   ← hypothesis + proptest (24 invariants)
│   ├── golden/                     ← byte-equality fixtures
│   ├── bench/                      ← O(1) ratio tests
│   ├── stress/                     ← 24h sustained
│   ├── smoke/                      ← <30s pre-push
│   └── fakes/                      ← test doubles
└── .github/workflows/
```

---

## 6. Verified Dhan Facts (cite these, don't re-derive)

| Fact | Value | Source |
|------|-------|--------|
| Historical intraday endpoint | `POST /v2/charts/intraday` | dhanhq.co/docs/v2 |
| Historical daily endpoint | `POST /v2/charts/historical` | dhanhq.co/docs/v2 |
| **Expired/rolling options endpoint** | `POST /v2/charts/rollingoption` (SDK: `expired_options_data`) | dhanhq.co/docs/v2/expired-options-data (verified 2026-05-16) |
| Native intraday timeframes | **1, 5, 15, 25, 60 minute** ONLY | dhanhq.co/docs/v2 |
| Chunk window (intraday) | 90 days per call | dhanhq.co/docs/v2 |
| **Chunk window (rollingoption)** | **30 days per call** | dhanhq.co/docs/v2/expired-options-data |
| Index segment (ALL indexes) | `IDX_I` enum 0 — single segment for NIFTY 50, BANKNIFTY, SENSEX, India VIX | dhanhq.co/docs/v2/annexure (verified 2026-05-15) |
| Instrument type (all 4 Track 1 instruments) | `INDEX` | dhanhq.co/docs/v2/annexure |
| **Options segment** | `NSE_FNO` (NIFTY/BANKNIFTY) or `BSE_FNO` (SENSEX) | dhanhq.co/docs/v2/annexure (verified 2026-05-16) |
| **Options instrument** | `OPTIDX` (index options) | dhanhq.co/docs/v2/expired-options-data |
| **Options drvOptionType** | `CALL` or `PUT` (NOT `CE`/`PE`) | Dhan SDK source |
| **Options strike addressing** | ATM-relative string: `"ATM"`, `"ATM±N"` (max ±10 index near-expiry, ±3 otherwise) | dhanhq.co/docs/v2/expired-options-data |
| **Options securityId rule** | **UNDERLYING's spot id (13/25/51) — NOT per-contract** | Dhan SDK `expired_options_data` signature (verified 2026-05-16) |
| **Options fields returned** | OHLC + volume + OI + IV + spot per minute | dhanhq.co/docs/v2/expired-options-data |
| **Options Greeks** | NOT returned — must compute via BSM | dhanhq.co/docs/v2/expired-options-data |
| **Options marketed depth** | ~5 years rolling | dhanhq.co docs |
| **Options ACTUAL depth (UNVERIFIED)** | Community evidence suggests ~2 years; **Phase 0 empirical probe REQUIRED** before locking 2021-01-01 floor | tradingqna.com "Dhan API for expired F&O data (1-min, 2 years)" thread |
| India VIX trading symbol | `INDIA VIX` (with space) | Dhan master CSV |
| India VIX update cadence | every 15 seconds (NOT tick) | NSE |
| Instrument master URL | `https://images.dhan.co/api-data/api-scrip-master-detailed.csv` | dhan.co |
| Time format | IST market times (convert to UTC at ingest) | dhanhq.co/docs/v2 |
| Rate limit (Data APIs) | **5 req/s + 100,000/day** verified | dhanhq.co/docs/v2 Rate Limit table |
| Adapter effective cap | 3 r/s (5 × 0.6 safety factor) | this file §2 rule 6 |
| securityId — NIFTY 50 | **`13`** verified | web.dhan.co/Charts?exch=IDX&seg=I&secid=13 |
| securityId — BANKNIFTY | **`25`** verified | web.dhan.co/Charts?exch=IDX&seg=I&secid=25 |
| securityId — SENSEX | **`51`** verified | web.dhan.co/Charts?exch=IDX&seg=I&secid=51 |
| securityId — India VIX | **`21`** verified | web.dhan.co/Charts?exch=IDX&seg=I&secid=21 |
| securityId policy | **Hardcoded for Track 1's 4-instrument universe** (rationale: 4-tuple is permanently locked; runtime CSV lookup adds complexity for no benefit). File lives at `brutex/adapters/dhan/constants.py` (landed in PR #51 / DEC-P0-033 Phase 2 Dhan adapter PR #1). Smoke test (`tests/smoke/test_dhan_securityid_verification.py`) verifies quarterly against web.dhan.co URLs (gated by `BRUTEX_DHAN_SMOKE=1`). **For Track 2 options: SAME 4 securityIds reused — no per-strike CSV lookup** (verified 2026-05-16). | this file §2 |

---

## 7. Verified Indian Market Microstructure Facts

| Fact | Value |
|------|-------|
| Trading hours | 09:15 - 15:30 IST (NSE + BSE equity) |
| Pre-open auction | 09:00 - 09:15 IST (exclude from backtest bars) |
| Spot index ticks from | 09:15:00 IST (post-auction opening) |
| Track 1 forced exit | 15:20 IST |
| Muhurat session (Diwali) | 1 hour per year; date list in `TRACK1_MICROSTRUCTURE.md` |
| NIFTY 50 rebalance | Semi-annual (March, September) — ~10 events in 2021-2026 |
| Stamp duty (equity intraday) | 0.003% buy side (signal-only mode: NOT applied) |
| Brokerage assumption (Track 1) | **NONE** — spot is not tradable |

---

## 8. Hardware Envelope (for honest SLA claims)

**Track 1 reference:** Apple M4 Pro, 14-core CPU (10P + 4E), 48 GB RAM,
8 TB external SSD. Daily apps run concurrently (10 cores used by sweep,
2 reserved).

**Production sweep SLA:** **2-4 days wall time** for ~33 billion trials
on this envelope, after Stage 1A → 1B → 2 → 3 → 4 pipeline. Public
benchmarks (vectorbt, NautilusTrader, Jesse) constrain the upper bound;
see `docs/TRACK1_PERFORMANCE_BUDGET.md` for the full memory-bandwidth
analysis.

Larger machines (64-128 core EPYC / Threadripper) scale linearly and
bring sweep to 12-24 hours; the spec MUST state SLA per hardware
envelope.

---

## 9. When in Doubt

1. Re-read `docs/TRACK1_BACKTEST_SPEC.md`.
2. If answer not there → ask the user with `AskUserQuestion`.
3. Never guess on scope. Never expand scope silently.
4. Never propose "improvements" outside the requested task.
5. Before major design choices, spawn a 4-agent hostile review (see
   `docs/TRACK1_PHASE_PLAN.md §0` for the pattern).

---

## 10. Session Etiquette

- Start of every session: read this file + `TRACK1_BACKTEST_SPEC.md` +
  open the locked-decisions log in `docs/MIGRATION.md`.
- Use TodoWrite for any multi-step task.
- Split long writes into multiple Write tool calls (avoid stream idle
  timeouts on docs >300 lines).
- Report honestly on failures; do not paper over errors.
- After any major refactor: run `ruff check brutex && mypy --strict
  brutex && pytest -q` before considering the change complete.
