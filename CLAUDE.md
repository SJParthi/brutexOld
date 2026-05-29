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
co-development layer. 2026-05-15: the broker → Groww migration;
Track 1 spec locked after 4-agent hostile-review pass._

---

## 1. Project Identity

- **Name:** BruteX
- **Purpose:** Extreme drill-down brute-force backtesting engine for
  Indian **spot indexes** (NIFTY 50 / BANKNIFTY / SENSEX) + **India
  VIX** (volatility index). Track 1 is signal-only research; the
  Options Track translates winning signals to options for execution
  realism.
- **Data source (Track 1):** **Groww Trade API** — only two
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

1. **No hallucination.** Every factual claim about Groww, NSE, BSE, NIFTY
   composition, brokerage, taxes, or any external system must be
   traceable to a verified source. If unsure, say "unverified" and stop.
2. **Single source of truth.** All Track 1 specs live under
   `docs/TRACK1_*.md` + `docs/track2/TRACK2_*.md`. Every PR references
   a section + decision ID from `docs/MIGRATION.md`. The legacy
   `docs/REQUIREMENTS.md` is a retired stub (banner only) — do not
   read or derive new requirements from it.
3. **Scope lock.** Track 1 = Groww + spot indexes + India VIX (NIFTY 50,
   BANKNIFTY, SENSEX, INDIA VIX). **India VIX is REFERENCE ONLY** — it
   is fetched + stored and stamped as `vix_at_entry` / `vix_at_exit` on
   every trade, but is NEVER part of brute-force pattern combinations
   or the 52-condition vocabulary. 21 timeframes (1m base + 19 derived
   intraday + 1d). Rolling `2021-01-01 → today` range. No futures, no
   options, no F&O contracts. Do not silently expand scope.

   **Scope expansion (DEC-P0-063, 2026-05-22, MIGRATION.md entry
   #200):** Track 1's INGEST scope was formally expanded to additionally
   cover **200 NSE F&O eligible single stocks (spot + options across all
   4 Groww `expiryCodes`)** for the LAKE. The **BRUTE-FORCE ENGINE is
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
6. **Rate-limit safety.** Groww Data APIs: **5 req/s** per-second cap +
   **100,000/day** quota (verified 2026-05-15 from groww.in/trade-api/docs/python-sdk
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

- **Active feature branch:** `claude/backtest-system-spec-ITI4Y`
- **Never push to `main`** without explicit user instruction.
- **Commit style:** Conventional Commits.
  - `docs(track1): add 52-condition vocabulary appendix`
  - `feat(adapter/groww): implement intraday OHLC chunking`
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
  `round_money` / `paisa_to_rupees`); rounding happens at the display
  boundary only. NEVER round statistical metrics (Sharpe, DSR, p-values,
  CPCV) — they keep full precision or strategy ranking breaks. Internal
  storage stays int64 paisa.

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
│   ├── adapters/                   ← Groww adapter lands in Phase 2
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

## 6. Verified Groww Facts (cite these, don't re-derive)

> **DEC-VENDOR-PIVOT-001 (2026-05-26):** the Groww facts table that lived
> here previously was wiped along with the Groww adapter. Pre-wipe state
> preserved at `git tag v-pre-groww-pivot`. The Groww facts below
> originate from the operator's pasted Groww docs (2026-05-26) +
> HARD-VERIFIED PyPI wheel source code for `growwapi 1.5.0`. See
> `docs/TRACK1_GROWW_ADAPTER.md` for the full adapter specification and
> `docs/adr/0001-groww-vendor-pivot.md` for the pivot rationale.

| Fact | Value | Source |
|------|-------|--------|
| Python SDK | `growwapi` 1.5.0 (3 Dec 2025) | pypi.org/project/growwapi/ |
| Historical OHLC method | `groww.get_historical_candles(exchange, segment, groww_symbol, start_time, end_time, candle_interval)` | groww.in/trade-api/docs/python-sdk/backtesting |
| Expiry-discovery method | `groww.get_expiries(exchange, underlying_symbol, year, month)` | groww.in/trade-api/docs/python-sdk/backtesting |
| Contract-discovery method | `groww.get_contracts(exchange, underlying_symbol, expiry_date)` | groww.in/trade-api/docs/python-sdk/backtesting |
| Backtesting segments | **CASH and FNO only** (no MCX in backtest) | groww.in/trade-api/docs/python-sdk/backtesting |
| Historical depth | **From 2020** for Equities, Indices, FNO | groww.in/trade-api/docs/python-sdk/backtesting |
| Native intraday candle constants (12 total) | `MIN_1, MIN_2, MIN_3, MIN_5, MIN_10, MIN_15, MIN_30, HOUR_1, HOUR_4, DAY, WEEK, MONTH` | growwapi/groww/client.py:86-97 (HARD-VERIFIED from PyPI wheel) |
| 1-minute candle support | **`CANDLE_INTERVAL_MIN_1 = "1minute"`** HARD-VERIFIED | growwapi/groww/client.py:86-97 |
| Chunk window — 1m/2m/3m/5m | **30 days per call** | groww.in/trade-api/docs/python-sdk/backtesting |
| Chunk window — 10m/15m/30m | 90 days per call | groww.in/trade-api/docs/python-sdk/backtesting |
| Chunk window — 1h/4h/1d/1w/1mo | 180 days per call | groww.in/trade-api/docs/python-sdk/backtesting |
| Candle response shape | `[timestamp_str, open, high, low, close, volume, oi_or_null]` | groww.in/trade-api/docs/python-sdk/backtesting |
| **Price unit** | **RUPEES (float)** — adapter MUST × 100 at parse boundary to preserve int64 paisa | groww.in/trade-api/docs/python-sdk/backtesting "All prices in rupees" |
| Symbol format — indices | `NSE-NIFTY`, `BSE-SENSEX`, `NSE-INDIAVIX` (Phase 0 verification pending) | groww.in/trade-api/docs/python-sdk/backtesting |
| Symbol format — options | `NSE-NIFTY-02Jan25-28500-PE` (absolute strike, NOT ATM-relative) | groww.in/trade-api/docs/python-sdk/backtesting |
| Options instrument type | `CE` / `PE` (NOT `CALL`/`PUT`) | growwapi annexures |
| Exchanges | NSE, BSE, MCX | groww.in/trade-api/docs/python-sdk |
| Rate limit — Orders | 10 r/s, 250/min, no per-day cap documented | groww.in/trade-api/docs/python-sdk Rate Limits |
| Rate limit — Live Data (Quote/LTP/OHLC) | 10 r/s, 300/min, no per-day cap documented | groww.in/trade-api/docs/python-sdk Rate Limits |
| Rate limit — Non Trading | 20 r/s, 500/min, no per-day cap documented | groww.in/trade-api/docs/python-sdk Rate Limits |
| Rate limit — Backtesting bucket | **UNVERIFIED** — docs only enumerate Orders / Live Data / Non Trading; backtesting endpoints not explicitly listed | empirical probe required |
| Rate-limit grouping | **Type-level shared** — one API exhausted = all APIs of that type rate-limited | groww.in/trade-api/docs/python-sdk Rate Limits |
| Live feed subscription cap | 1000 instruments at one time | groww.in/trade-api/docs/python-sdk |
| Adapter effective cap | **Adaptive [4..8] r/s** (auto-tune via state machine — see `docs/TRACK1_GROWW_ADAPTER.md §5`) | this file §2 rule 6 + operator decree 2026-05-26 |
| Auth approach 1 — API Key + Secret | **REFUSED** (requires daily approval on Groww Cloud Console — violates §11 AUTOMATED) | groww.in/trade-api/docs/python-sdk Step 3 |
| Auth approach 2 — TOTP | **ACCEPTED** (no expiry per docs; TOTP secret stored mode-0600) | groww.in/trade-api/docs/python-sdk Step 3 |
| Instrument master URL | `https://growwapi-assets.groww.in/instruments/instrument.csv` | groww.in/trade-api/docs/python-sdk/instruments |
| Instrument master schema | `exchange, exchange_token, trading_symbol, groww_symbol, name, instrument_type, segment, series, isin, underlying_symbol, ...` | groww.in/trade-api/docs/python-sdk/instruments |
| Exception classes | 11 total: `GrowwBaseException`, `GrowwAPIException`, `GrowwAPI{Authentication,Authorisation,BadRequest,NotFound,RateLimit,Timeout}Exception`, `GrowwFeed{,Connection,NotSubscribed}Exception` | groww.in/trade-api/docs/python-sdk/exceptions |
| Time format | IST market times (convert to UTC at ingest) | unchanged from prior spec |
| INDIA VIX presence | **UNVERIFIED** — Phase 0b empirical probe is the gate before any Groww adapter code lands. If VIX absent → DEC-VENDOR-PIVOT-001 is rescinded. | red-team agent 2026-05-26 |
| Data drift Groww-vs-NSE-official | **UNQUANTIFIED** — Phase 0b probe will measure | red-team agent 2026-05-26 |
| Track 1 brute-force universe — NIFTY 50 | `groww_symbol="NSE-NIFTY"`, segment=CASH | Phase 0b verification pending |
| Track 1 brute-force universe — BANKNIFTY | `groww_symbol="NSE-BANKNIFTY"`, segment=CASH | Phase 0b verification pending |
| Track 1 brute-force universe — SENSEX | `groww_symbol="BSE-SENSEX"`, segment=CASH | Phase 0b verification pending |
| Track 1 brute-force universe — INDIA VIX | `groww_symbol="NSE-INDIAVIX"` (or alt), segment=CASH | Phase 0b HARD-GATE verification pending |

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
