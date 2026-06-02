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

_Last updated: 2026-06-01 (DEC-DASHBOARD-OMIT-AUDIT-001 — vendor docs
reconciled Groww → Dhan after the 5-agent workspace verification found
§1/§6 stale). DEC-P0-026 (PR #48) retired the tickvault co-development
layer. Vendor history: broker → Groww migration (2026-05-15), Groww →
Dhan pivot proposed then reverted — **DEC-GROWW-DHAN-REVERT-001
(2026-05-31, ADR-0002) is authoritative: the live vendor is Dhan.**
Track 1 spec locked after 4-agent hostile-review pass._

---

## 1. Project Identity

- **Name:** BruteX
- **Purpose:** Extreme drill-down brute-force backtesting engine for
  Indian **spot indexes** (NIFTY 50 / BANKNIFTY / SENSEX) + **India
  VIX** (volatility index). Track 1 is signal-only research; the
  Options Track translates winning signals to options for execution
  realism.
- **Data source (Track 1):** **Dhan Data API v2** (REST, no vendor SDK —
  plain HTTP client; base `https://api.dhan.co/v2/`) per
  DEC-GROWW-DHAN-REVERT-001 / ADR-0002. Track 1 spot uses two surfaces:
  1. `POST /v2/charts/intraday` (1-minute OHLC ONLY per
     DEC-DHAN-1M-ONLY-001; other timeframes resampled deterministically
     from 1m)
  2. `POST /v2/charts/historical` (daily OHLC, used for PDH/PDL anchors)

  Expired options (Options Track / lake) use `POST /v2/charts/rollingoption`
  exclusively (never the spot endpoints). See §6 for the full verified
  fact table.
- **Out of scope (Track 1 engine):** Live feed, WebSocket, order
  placement, portfolio, funds, multi-user SaaS. (Per DEC-P0-063 the LAKE
  additionally ingests F&O single-stock spot+options as REFERENCE_ONLY;
  the BRUTE-FORCE ENGINE remains the 3 indexes — see §2 Rule 3.)
- **Active branch:** `claude/automated-merge-ready-pipeline-IpP85`

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
   cover **200 NSE F&O eligible single stocks (spot + options across the
   Dhan rolling-option `expiryCodes`)** for the LAKE. The **BRUTE-FORCE ENGINE is
   UNCHANGED** — it still operates only on the 3 brute-force indexes
   (NIFTY/BANKNIFTY/SENSEX); the 200 stocks join India VIX in
   `REFERENCE_ONLY` (mirrors decision #83). Stock options sit in the
   lake ready for future brute-force expansion (separate decision; not
   authorized here). The "4 index instruments / no F&O contracts"
   wording above describes the ENGINE; the LAKE is the expanded surface
   per DEC-P0-063. Full §3 / cross-spec reconciliation across the
   ~15 remaining stale-text locations is a tracked follow-up PR; until
   then DEC-P0-063 is authoritative for the stock-options expansion.

   **Amended by DEC-INSTRUMENT-UNIVERSE-FAST-001 (2026-05-29, MIGRATION.md
   entry #160):** lean ingest — the `instruments` table now persists ONLY
   the ~241 unique universe underlyings (every admitted index + every
   F&O-eligible single-stock equity); the ~89,000 FUT/CE/PE legs are
   PARSED for eligibility then DISCARDED, recoverable on demand via
   `get_contracts` (contracts inherit lot/tick/freeze from the underlying
   row). The BRUTE-FORCE ENGINE is UNAFFECTED. So "the LAKE is the expanded
   surface" is narrowed: the lake = the universe underlyings; the legs are
   derived/recoverable, not stored.
4. **Reproducibility first.** Every run is identified by `blake3(
   strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖ canonical_params
   ‖ data_snapshot_hash ‖ indicator_version ‖ code_commit_hash)`. No
   computation without that hash recorded. (Was `sha256(...)` pre-DEC-CALIBRATE-001;
   corrected 2026-05-24 to match `TRACK1_BACKTEST_SPEC.md:654` which has used
   blake3 since the v2 spec lock 2026-05-16. See MIGRATION.md entry #204.)
5. **O(1) discipline.** Bar lookup, indicator value lookup, combo dedup
   (Bloom + LMDB), strategy mask evaluation (u64 AND), and result
   append must all be O(1). CI gates: `p99(N=1M) / p99(N=1K) ≤ 1.1`
   for each hot path. Any regression fails CI.
6. **Rate-limit safety.** Dhan Data APIs: **5 req/s** per-second cap +
   **100,000/day** quota (`docs/dhan-ref/01-introduction-and-rate-limits.md`;
   mirrored in `brutex/adapters/dhan/constants.py` `DATA_RATE_LIMIT_RPS=5`,
   `DATA_DAILY_QUOTA=100_000`). Adapter uses an adaptive token bucket
   capped at **[3..5] r/s** (`brutex/adapters/dhan/rate_control.py`).
   Never raise without explicit user approval. Track 1's typical
   first-run + daily incremental is < 0.1% of the daily quota.
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

13. **MERGE-READINESS CHARTER** (operator decree 2026-05-29,
    DEC-MERGE-GUARANTEE-001). Every PR / sub-PR / task / sub-task — in
    this session and every new session — obeys
    `docs/MERGE_READINESS_GUARANTEE.md`: the priority law
    (Correctness > Reliability > Recoverability > Observability >
    Performance > Scalability), the §3 merge-ready gate matrix, and the
    §4 Definition of Done. Two hard invariants: (a) **never merge over a
    red or pending gate** — `mergeable_state` must be `clean` and every
    ✅ gate green, verified not assumed; (b) **honesty** — per Golden
    Rule #10, never claim "100% / fully / guaranteed" for a 🟡/📋/🚫
    tier row; state what is green, partial, and out of scope. The
    charter is enforced on every surface via §8 (this file for sessions,
    `.github/PULL_REQUEST_TEMPLATE.md` for PRs, the `CI OK` required
    check for merges).

---

## 3. Branching and Commits

- **Active feature branch:** `claude/automated-merge-ready-pipeline-IpP85`
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
- **Money display = 2 decimals, money-only** (DEC-MONEY-2DP-001) — every
  paisa→rupee display goes through `brutex.money` (`format_inr` /
  `round_money` / `paisa_to_rupees`). NEVER round statistical metrics (Sharpe,
  DSR, p-values, CPCV) or `iv` — they keep full precision or strategy ranking
  breaks. Money amounts (costs + display) are int64 paisa; OHLC/spot/option
  **prices** are stored as Decimal rupees (Parquet Float64), NOT paisa — and
  **snapped to the 2-decimal tick grid at the lake-write boundary**
  (`round_money`, `ROUND_HALF_UP` — never truncation; spot mirrors options) so
  the lake never holds vendor float-noise like `14020.7999`
  (DEC-SPOT-PRICE-2DP-001). See §6 (DEC-PAISA-DOC-RECONCILE-001).

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
│   ├── TRACK1_DASHBOARD_DESIGN.md  ← v29 locked design spec + data contract
│   ├── dashboard/mockups/          ← v28.html + v29.html (canonical mockups)
│   ├── MIGRATION.md                ← decision ledger
│   ├── REQUIREMENTS.md             ← RETIRED (stub)
│   ├── REQUIREMENTS_SIMPLE.md      ← RETIRED (stub)
│   ├── ROADMAP.md                  ← RETIRED (stub)
│   ├── adapters.md                 ← RETIRED (stub)
│   ├── adr/                        ← architecture decisions
│   └── runbooks/
├── brutex/                         ← Python package
│   ├── adapters/dhan/              ← Dhan Data API v2 adapter (REST)
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

> **DEC-GROWW-DHAN-REVERT-001 (2026-05-31, ADR-0002):** REVERSAL of
> ADR-0001's Dhan→Groww pivot. The live vendor is **Dhan Data API v2**.
> The facts below are repo-sourced: HARD-VERIFIED from
> `brutex/adapters/dhan/constants.py` (and sibling modules) + the
> operator-supplied Dhan v2 reference at `docs/dhan-ref/`. See
> `docs/adr/0002-groww-to-dhan-revert.md` for the revert rationale.
> The legacy Groww table is preserved in git history (pre-revert);
> `docs/adr/0001-groww-vendor-pivot.md` is SUPERSEDED. (Compiled by the
> 2026-06-01 5-agent workspace verification; every row traces to file:line.)

| Fact | Value | Source |
|------|-------|--------|
| Vendor / transport | **Dhan Data API v2 — REST, NO vendor SDK** (plain HTTP via `requests`/`httpx`) | `adr/0002-groww-to-dhan-revert.md:45-46` |
| Base URL | `https://api.dhan.co/v2/` | `dhan/constants.py:24`; `dhan-ref/01...:13` |
| Historical OHLC — spot | `POST /v2/charts/intraday` (1m) + `POST /v2/charts/historical` (1d) | `dhan/constants.py:31-34`; `dhan-ref/05...:22-25` |
| Expired-options method | `POST /v2/charts/rollingoption` — **ONLY** option source (spot endpoints never used for options) | `dhan/constants.py:37`; `dhan-ref/06...:11-13` |
| Backtesting segments | `IDX_I` (indices — NIFTY/BANKNIFTY/SENSEX/India VIX), `NSE_EQ`, `NSE_FNO`, `BSE_EQ`, `BSE_FNO` | `dhan/constants.py:76`; `dhan-ref/09...:76-78` (DEC-DHAN-IDX-SEGMENT-001) |
| Instrument types | `INDEX, EQUITY, OPTIDX, OPTSTK, FUTIDX, FUTSTK` | `dhan/constants.py:74-76` |
| Historical depth — intraday | **5 years** | `dhan/constants.py:99`; `dhan-ref/05...:82` |
| Historical depth — daily | **back to instrument inception** | `dhan-ref/05...:84` |
| Candle intervals (Dhan supports) | `"1","5","15","25","60"` (minutes) | `dhan/constants.py:60`; `dhan-ref/05...:43` |
| Candle interval — Track 1 spot+options | **`"1"` (1-minute) ONLY** (DEC-DHAN-1M-ONLY-001; other TFs resampled) | `dhan/constants.py:63,66`; `dhan-ref/05...:89-91` |
| Chunk window — intraday | **90 days per call** (single cap, no tiering) | `dhan/constants.py:93`; `dhan-ref/05...:83` |
| Chunk window — options | **30 days per call** | `dhan/constants.py:96`; `dhan-ref/06...:18` |
| Candle response shape — spot | **Columnar parallel arrays** `{open[],high[],low[],close[],volume[],timestamp[],open_interest[]}` (NOT row-arrays) | `dhan/parser.py:5-15`; `dhan-ref/05...:54-63` |
| Candle response shape — options | **Per-side columnar** `{ce:{iv[],oi[],strike[],spot[],open[],...}, pe:null}` (null side allowed) | `dhan-ref/06...:54-62` |
| **Price unit** | **RUPEES (float)** from Dhan — kept as exact `Decimal` rupees through the lake (Parquet `Float64`); int64 paisa is the cost/display unit only, NOT price storage (DEC-PAISA-DOC-RECONCILE-001) | `data/candle_fields.py` (`to_price`→`Decimal`); `data/models.py:142-144`; `dhan-ref/05...:72-73` |
| Timestamp unit | **UNIX epoch (UTC seconds)** — store UTC, convert to IST at display only | `dhan/parser.py:36-57`; `dhan-ref/05...:74-76` |
| Symbol/key format | **`SECURITY_ID` (numeric, from CSV) — NEVER hardcode**; lookup Symbol/ISIN → CSV row → SECURITY_ID | `dhan/instruments.py:64-73`; `dhan-ref/09...:64-72` |
| Options strike addressing | **Relative-to-spot ATM±N** (ATM±10 index near-expiry, ATM±3 other), NOT absolute strikes | `dhan-ref/06...:43-44` |
| Options instrument type | **`CALL` / `PUT`** (NOT `CE`/`PE`) | `dhan/constants.py:79`; `dhan-ref/06...:45` |
| Exchanges | NSE, BSE, MCX | `dhan-ref/09...:34` |
| Rate limit — Data APIs | **5 r/s, 100,000/day** (single Data bucket) | `dhan/constants.py:86-89`; `dhan-ref/01...:22-26` |
| Adapter effective cap | **Adaptive [3..5] r/s** (token bucket, step-down on 429) | `dhan/rate_control.py:1-153` + this file §2 rule 6 |
| Data-API bucket isolation | **UNVERIFIED** — no explicit sharing rule in docs; treat 5 r/s as binding | `dhan-ref/01...:49-57` |
| Auth — default approach | **Manual web-generated token + `RenewToken`** (operator 2026-05-31); header `access-token` (JWT, 24h) + `dhanClientId` | `dhan/auth.py`; `dhan/constants.py:53,56`; `dhan-ref/02...:19-35,67-72` |
| Auth — TOTP alternative | `POST https://auth.dhan.co/app/generateAccessToken?dhanClientId=&pin=&totp=` (unattended runs, meets §11) | `dhan/constants.py:27`; `dhan-ref/02...:44-82` |
| Instrument master URL — compact | `https://images.dhan.co/api-data/api-scrip-master.csv` | `dhan/constants.py:44`; `dhan-ref/09...:10` |
| Instrument master URL — detailed | `https://images.dhan.co/api-data/api-scrip-master-detailed.csv` (**required for F&O**) | `dhan/constants.py:47-48`; `dhan-ref/09...:11` |
| Instrument master schema | 31 cols; keys `EXCH_ID, SEGMENT, SECURITY_ID (PK), ISIN, INSTRUMENT, UNDERLYING_SYMBOL, SYMBOL_NAME, LOT_SIZE, TICK_SIZE, SM_FREEZE_QTY, OPTION_TYPE, EXPIRY_FLAG` | `dhan/instruments.py:51-72`; `dhan-ref/09...:20-30` |
| Exception hierarchy | `DhanError, DhanAuthError, DhanRateLimitError, DhanBadRequest, DhanNotFound, DhanServerError` + `is_retryable()` | `dhan/errors.py:1-103` |
| F&O-eligible stock detection | EQUITY rows whose `UNDERLYING_SYMBOL` appears in FUTSTK/OPTSTK set (DEC-DHAN-FNO-JOIN-KEY-001) | `dhan/instruments.py:15-34,361-421` |
| Index allow-list | **31 NSE + {SENSEX} BSE = 32** (`_ADMITTED_NSE/BSE_INDEX_SYMBOLS`); India VIX in NSE list | `dhan/instruments.py:113-148` |
| Track 1 brute-force universe | NIFTY (`IDX_I`,INDEX), BANKNIFTY (`IDX_I`,INDEX), SENSEX (`IDX_I`,INDEX) — **indices use the cross-exchange `IDX_I` segment, NOT `*_EQ`** (DEC-DHAN-IDX-SEGMENT-001); resolved to SECURITY_ID via CSV | `dhan/instruments.py:113-148`; `dhan/adapter.py:128` |
| INDIA VIX presence on historical endpoint | **UNVERIFIED** — Phase 0 HARD-GATE empirical probe | `dhan/instruments.py:119`; `dhan-ref/01...:49-57` |
| Index SECURITY_ID values (NIFTY/BANKNIFTY/SENSEX) | **UNVERIFIED** — not in docs; resolve from detailed CSV at Phase 0 | `dhan-ref/09...:80-90` |
| Expiry-code numbering | **DISCREPANCY** — rolling-option uses 1/2/3 (Near/Next/Far); Annexure uses 0/1/2 (DEC-DHAN-EXPIRY-CODES-001 open) | `dhan-ref/06...:43` |
| Time format | IST market times (convert to/from UTC at ingest/display) | `dhan/parser.py:36-57` |

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
