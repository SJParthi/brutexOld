# Track 2 Plan Finalization — 2026-05-16

**Trigger:** operator request 2026-05-16: "let us finalize the plan and
do a deep through research". 4 parallel research agents dispatched on
the 4 biggest open questions. All 4 returned. This doc consolidates
their findings, lists decisions, and maps to spec edits.

**Scope:** Track 2 (Options) only. Track 1 (signal-only) is separately
locked.

---

## 0. Executive Summary

| # | Stream | Major finding | Spec impact |
|---|---|---|---|
| A | Dhan options data API | `securityId` = UNDERLYING's id (e.g., 13 for NIFTY), NOT per-contract. Marketed 5yr depth, but community evidence suggests ~2yr reality. | §3 simplification + Phase 0 empirical probe |
| B | NSE/BSE expiry calendar | Current spec days correct BUT historical changes (Sept 2025, Nov 2024) not modeled. Settlement is 30-min VWAP, not single tick. Holiday shifts to PREVIOUS trading day. | §6 historical effective-date footnotes |
| C | Risk-free rate `r` | FBIL daily T-bill curve best source. q≠0 is a **modeling error** for price indexes — material 1-4% bias. Monthly snapshot too coarse. | §7 major rewrite |
| D | BSM Greeks validation | T-floor 10 min; vega-floor 1e-6; |delta|≥0.02 filter; dual oracle (Jäckel + QuantLib); tick-floor saturation flag. q=0 acceptable IF clearly documented + fills are observed (not BSM-priced). | §7 expansion |

**Net effect on Phase 3 build-readiness:** 3 critical decisions still
require operator input before code-lock; 5 spec sections need amendment.

---

## 1. Stream A — Dhan Options Data API

### 1.1 Findings (verified)

| Item | Finding | Source |
|---|---|---|
| Endpoint | `POST /v2/charts/rollingoption` (SDK: `expired_options_data`) | dhanhq.co/docs/v2/expired-options-data |
| Fields returned | OHLC + volume + **OI** + **IV** + spot per minute | Same |
| Greeks | NOT returned — must compute from IV via BSM | Same |
| Strike addressing | ATM-relative `"ATM"`, `"ATM±N"` — max ±10 for index near-expiry, ±3 otherwise | Same |
| Rate limit | 5 req/s + 100,000/day (same as spot) | Same |
| **securityId** | **UNDERLYING's id** (e.g., 13 for NIFTY) — NOT per-strike | SDK source on GitHub |
| Marketed depth | ~5 years rolling | dhanhq.co docs |

### 1.2 CRITICAL UNVERIFIED

| Item | Why it matters | Evidence |
|---|---|---|
| Actual historical depth | Track 2 floor of 2021-01-01 may be unachievable | tradingqna.com thread titled "Dhan API for expired F&O data (1-min, **2 years**)" |
| SENSEX coverage depth | If shorter, SENSEX scope shrinks | Not enumerated in docs |
| Per-contract data gaps | Non-idempotent responses suggested by community-built ExpiryFlow having duplicate-detection | Community project, not docs |

### 1.3 Decisions

1. ✅ **Use UNDERLYING `securityId` only** — major simplification. No per-strike CSV lookup.
2. ✅ **Greeks computed locally** via Rust BSM kernel (already in §7 spec).
3. ⚠️ **Phase 0 EMPIRICAL PROBE required** before locking 2021-01-01 floor.
4. ⚠️ **Examine ExpiryFlow source** (`github.com/marketcalls/ExpiryFlow`) for ingest design hints.

### 1.4 Spec edits required

- §3 endpoint section: add note "securityId is UNDERLYING's spot id"
- Add §3.x: "Phase 0 empirical probe to discover real historical floor"
- §3 unverified items: add SENSEX coverage depth + per-bar duplicates
- `CLAUDE.md §6 Verified Dhan Facts`: add `rollingoption` endpoint row

---

## 2. Stream B — NSE/BSE Expiry Calendar (post-2024 changes)

### 2.1 Findings (verified)

| Instrument | Current (2026-05) state | Historical changes |
|---|---|---|
| **NIFTY weekly** | Tuesday | Was Thursday pre-Nov 2024 → Tuesday from Nov 2024 → reaffirmed Sept 1, 2025 |
| **BANKNIFTY weekly** | DISCONTINUED | Last weekly expiry Nov 13, 2024; effective Nov 20, 2024 |
| **BANKNIFTY monthly** | Last Tuesday of month | Was last Thursday pre-Sept 2025 |
| **SENSEX weekly** | Thursday | Was Tuesday pre-Sept 4, 2025 |
| **SENSEX monthly** | Last Thursday | Same as weekly |
| **Strike intervals** | NIFTY 50, BNX 100, SENSEX 100 | Unchanged |
| **Strikes per expiry** | NIFTY 61 (30+1+30); BNX 81 (40+1+40); SENSEX 41 (20+1+20) | Unchanged |
| **Settlement** | **Last 30-min VWAP** of underlying at 15:30 IST | Unchanged |
| **Holiday rule** | Shifts to **PREVIOUS trading day** (never forward) | Unchanged |
| **Muhurat session** | Separate symbolic session, NEVER an expiry day | Unchanged |

### 2.2 CRITICAL FINDING — Historical correctness gap

Current spec assumes today's expiry days apply across the entire
2021-01-01 → today backfill. **This is wrong** for any backtest
period before:
- Sept 1, 2025 (NIFTY weekly moved to Tuesday)
- Nov 20, 2024 (BANKNIFTY weekly discontinued)
- Sept 4, 2025 (SENSEX weekly moved to Thursday)

For 2021-2024 backtests, the engine must apply HISTORICAL expiry
day rules per date.

### 2.3 Decisions

1. ✅ **Build per-date expiry calendar table** keyed on `(instrument, effective_date)`.
2. ✅ **Holiday shift logic**: always to previous trading day (never forward).
3. ✅ **Settlement formula**: `settlement_price = VWAP(underlying, 15:00-15:30 IST)` on expiry day.
4. ✅ **Muhurat session: NOT an expiry day, NOT a regular trading bar.**
5. ⚠️ **Consume official NSE FAOP series + BSE notices** for ground-truth expiry calendar (don't rule-compute).

### 2.4 Spec edits required

- §6 trade horizon: add effective-date footnotes for each instrument
- §6 (NEW subsection): "Historical expiry calendar table"
- §6: add settlement formula (30-min VWAP, not single close)
- §6: add holiday-shift rule
- §6: cross-reference muhurat handling from `TRACK1_MICROSTRUCTURE.md`

---

## 3. Stream C — Risk-Free Rate `r` for BSM

### 3.1 Findings (verified)

| Item | Finding |
|---|---|
| Best source | **FBIL daily T-bill curve** (primary), RBI DBIE weekly auction (fallback) |
| Benchmark | **91-day T-bill yield** — standard for Indian index options |
| MM→CC conversion | `r_cc = (365/n) × ln(1 + y_mm × n/365)`. For 91-day T-bill at 6.50% MM → r_cc = 6.454% |
| **Dividend yield `q`** | **`q=0` is INCORRECT** — material 1-4% bias on calls/puts |
| Snapshot frequency | **Daily recommended** (current spec: monthly is too coarse — 2022 RBI hiking cycle had 50+bp jumps in single months) |
| Rho sensitivity | 100bp `r` error → ~1.3% premium impact on weekly NIFTY ATM, 2.5% monthly, 4.4% 3-month |

### 3.2 The q=0 problem (cross-confirmed with Stream D)

NIFTY 50 / BANKNIFTY / SENSEX are PRICE indices — when constituents
go ex-dividend, the index drops mechanically. BSM with `q=0` assumes
NO dividend leakage and over-prices calls / under-prices puts by:

| Tenor | q=0 bias (NIFTY ATM, σ=15%, r=7%) |
|---|---|
| Daily (T=1/365) | <0.4% of premium |
| Weekly (T=7/365) | ~1.7% of premium |
| Monthly (T=30/365) | ~3.8% of premium |

NIFTY 50 trailing dividend yield ≈ 1.3% (Trendlyne). Some Indian
broker calculators (Zerodha Varsity) use q=0 for simplicity.

### 3.3 Decisions (CRITICAL — needs operator input)

**OPTION A — Keep q=0 (simplicity):**
- Pro: matches Zerodha Varsity + most retail platforms
- Pro: bias mostly cancels for **relative delta ranking** (Track 2 strike selection by delta-bucket)
- Con: theoretical price differs ~1-4% from market — but Track 2 uses Dhan-observed prices for FILLS, so this doesn't affect P&L directly
- Con: degrades BSM-derived Greeks slightly (delta off by ~0.005-0.02)
- **Conditional acceptable** if spec explicitly documents the bias direction + restricts BSM to delta-ranking only

**OPTION B — Use NIFTY trailing dividend yield (rigor):**
- Pro: academically correct
- Pro: matches institutional desks
- Con: introduces dependency on `niftyindices.com` dividend yield series
- Con: small quarterly refresh maintenance

**Recommendation:** OPTION A (keep q=0) with explicit documentation
of the bias. Track 2 uses observed Dhan prices for all fills, so the
BSM bias only affects delta-strike selection (relative ranking is
preserved). Defer to OPTION B only if operator wants institutional-grade
pricing rigor.

### 3.4 Snapshot frequency: daily

Switch spec from "60-row monthly table" to "**daily T+1 snapshot from
FBIL with RBI weekly auction fallback**". Storage: 1,300 rows × 8 bytes
= 10 KB total. Trivial.

### 3.5 Spec edits required

- §7 risk-free rate row: change "monthly snapshot, 60-row table" to "daily T+1 FBIL snapshot, ~1,300-row table for 2021-2026"
- §7 (NEW): add MM→CC conversion formula explicitly
- §7 dividend yield row: add decision flag (Option A vs B) per operator
- §7: cite FBIL + RBI DBIE as canonical sources

---

## 4. Stream D — BSM Greeks Validation

### 4.1 Findings (verified)

| Item | Finding |
|---|---|
| Option style | All 3 indexes EUROPEAN, cash-settled → BSM closed-form is EXACT (no American adjustment) |
| Settlement | T+1 cash credit of intrinsic value × lot × n_lots |
| Dhan IV method | UNVERIFIED. Recommend ingest as-is + cross-check at 50bp tolerance |
| IV stability deep OTM | Newton-Raphson fails when vega→0. Use Jäckel's `lets_be_rational` (rational + Householder) |
| Strike filters | |delta| ≥ 0.02 AND vega ≥ 1e-6 OR reject from universe |
| Greeks near expiry | Mathematical divergence as T→0. Hard T-floor of 600 seconds (10 min). Track 2 exits at 15:20 IST → never inside this window. |
| Dividend yield | Confirms Stream C: q=0 is a modeling choice with 1-4% bias |
| Validation oracles | Dual: `lets_be_rational` (IV) + QuantLib `AnalyticEuropeanEngine` (prices/Greeks). py_vollib wraps Jäckel → NOT independent |
| Tick size | ₹0.05 confirmed unchanged for index options post-Apr-2025 |

### 4.2 Tick-floor saturation problem

For very-OTM strikes where BSM theoretical < ₹0.05, the market quote
is "stuck" at the ₹0.05 floor. This produces:
- Theoretical ₹0.0473 → quoted ₹0.05 (5.7% markup, manageable)
- Theoretical ₹0.0094 → quoted ₹0.05 (5.3× markup, unusable)

Track 2 design (observed Dhan prices for FILLS + BSM-from-Dhan-IV for
GREEKS) is correct. Add safeguard: flag any strike at ₹0.05 floor for
≥ N consecutive bars as "tick-floor saturated" and exclude from entry
universe.

### 4.3 IV cross-check

Ingest Dhan's `implied_volatility` as authoritative. ALSO recompute IV
in Rust BSM from observed mid-price. Assert agreement within 50bp.
If mismatch: flag the bar in audit log but DO NOT replace Dhan's value
(reproducibility > pedantic correctness).

### 4.4 Decisions

1. ✅ **BSM closed-form is exact** (European options, cash-settled)
2. ✅ **Use `lets_be_rational` semantics in Rust kernel** for IV
3. ✅ **Strike filters: |delta| ≥ 0.02 AND vega ≥ 1e-6**
4. ✅ **Hard T-floor of 600 seconds** + property test
5. ✅ **Dual validation oracle**: Jäckel + QuantLib (NOT py_vollib alone)
6. ✅ **IV cross-check at 50bp tolerance** + audit-log flag on mismatch
7. ✅ **Tick-floor saturation flag** + exclude from entry universe

### 4.5 Spec edits required

- §7 (NEW): T-floor of 600 seconds + property test
- §7 (NEW): vega-floor + delta-floor strike filters
- §7 (NEW): dual oracle validation (Jäckel + QuantLib)
- §7 (NEW): tick-floor saturation flag spec
- §7 (NEW): IV cross-check assertion spec
- §15 verification gates: add Greeks property tests

---

## 5. Consolidated UNVERIFIED items (updated)

Across CALCULATOR_SPEC §16 (8 items) + new from this research:

| # | Item | Stream | Blocker for? |
|---|---|---|---|
| 1 | Dhan rate card (₹20 brokerage) | (earlier) | Calculator |
| 2 | Finance Act 2026 STT 0.15% gazette | (earlier) | Calculator post-Apr-2026 |
| 3 | BSE Bankex options exchange rate | (earlier) | SENSEX only |
| 4 | STT-on-exercise rate post-Apr-2026 | (earlier) | Held-to-expiry tracks only |
| 5 | SENSEX freeze qty post-Mar-2026 | (earlier) | High-lot SENSEX |
| 6 | Dhan Call & Trade exact rate | (earlier) | Event-ledger only |
| 7 | Dhan API-vs-UI brokerage parity | (earlier) | Blanket assumption |
| 8 | Dhan auto-SqOff trigger conditions | (earlier) | Event-ledger only |
| **9** | **Dhan historical depth — does 2021-01-01 floor work?** | **A** | **Entire 2021-2024 backfill** |
| **10** | **Dhan SENSEX coverage depth** | **A** | **SENSEX backfill** |
| **11** | **Dhan IV computation methodology** | **D** | **IV reproducibility** |
| **12** | **FBIL programmatic API availability** | **C** | **Risk-free rate ingest** |
| **13** | **NIFTY trailing dividend yield series (if Option B chosen)** | **C** | **BSM pricing rigor** |
| **14** | **Operator decision on q=0 vs q=NIFTY-yield** | **C+D** | **BSM Greeks** |

**Items 9-14 are NEW** from this research. **Item 14 requires operator decision NOW**.

---

## 6. Phase 0 prerequisites (operator action checklist)

Before any code commits, operator must:

### CRITICAL (block all options work)

- [ ] **9.** Run empirical probe: fetch NIFTY weekly ATM CE for 2020-12-31, 2021-01-01, 2022-01-01, 2023-01-01, 2024-01-01 to discover actual Dhan historical floor
- [ ] **10.** Same probe for SENSEX (oldest plausible BSE weekly expiry)
- [ ] **14.** DECIDE: q=0 (simplicity) OR q=NIFTY-trailing-yield (rigor) for BSM dividend treatment

### BLOCKERS for calculator (per earlier work)

- [ ] **1.** Visit `dhan.co/pricing` in browser, screenshot, confirm ₹20/order brokerage
- [ ] **2.** Verify Finance Act 2026 STT 0.15% gazette
- [ ] **4.** Same gazette → STT-on-exercise rate

### MEDIUM (can defer to relevant module)

- [ ] **11.** Open Dhan support ticket: IV computation method (Newton-Raphson? Mid-price? LTP?)
- [ ] **12.** Verify FBIL programmatic API exists (else build HTML scraper)
- [ ] **13.** IF Option B chosen: identify dividend yield data source
- [ ] **3.** BSE Bankex options rate (SENSEX only)
- [ ] **5.** SENSEX freeze qty post-Mar-2026
- [ ] **6.** Dhan Call & Trade rate
- [ ] **7.** Dhan API-vs-UI brokerage parity
- [ ] **8.** Dhan auto-SqOff trigger conditions

---

## 7. Plan finalization summary

### What's locked

1. Calculator (per-trade): algorithm, constants, rounding, slippage, 23 fixtures
2. Strike model: 50/100/100 intervals; 21-strike ATM±10 chain
3. Trade horizon: current weekly (NIFTY/SENSEX) or current monthly (BANKNIFTY)
4. Strike selection rules: 10 rules R1-R10 (7 offset + 3 delta-target)
5. Storage: Parquet + DuckDB lake
6. Audit trail: hash-chain JSONL (inherits Track 1)
7. Connection to Track 1: signals + India VIX from same store

### What's amended this round

1. §3 Dhan endpoint: securityId clarification + Phase 0 probe
2. §6 trade horizon: historical effective-date footnotes + settlement formula + holiday shift
3. §7 Greeks: 7 new subsections (T-floor, vega-floor, dual oracle, tick saturation, IV cross-check, MM→CC conversion, daily snapshot)

### What's still OPEN

- Operator decision #14 (q=0 vs q=NIFTY-yield)
- Phase 0 empirical probe results (items 9, 10)
- 11 unverified items from full list

### What's outside this finalization

- TRACK2_PHASE_PLAN.md (sequencing) — does not yet exist; can be drafted next session
- Risk management spec (position sizing, drawdown limits) — open
- Annual income tax module deep spec — open (skeleton in COSTS_VERIFIED §18)
- Track 2 vocabulary (inherits Track 1's 52 conditions or extends?) — open

---

---

## 8. Round 2 — Claude-chat prompt verification (added 2026-05-16)

Operator shared a Claude-chat-generated cost-module prompt and asked
for deep verification. 2 parallel agents dispatched. Both returned.

### 8.1 Prompt-claim verification (4 high-risk items)

| # | Claim | Verdict |
|---|---|---|
| 1 | Futures STT 0.05% post-Apr-2026 (Budget 2026) | ✅ **VERIFIED** — multiple Q1 2026 sources |
| 2 | Dhan futures brokerage = min(₹20, 0.03% × turnover) | ✅ **VERIFIED** — same MIS + NRML |
| 3 | Options exercise = ₹20 per CONTRACT | ❌ **CONTRADICTED** — actual is per ORDER. Prompt would over-count 100-lot exercise by ₹1,980 |
| 4 | Physical settlement = 0.10% of contract value | ✅ **VERIFIED** — stock derivs only; index F&O is cash-settled |

### 8.2 Extended instrument scope verification (Stream 2)

| Instrument | Exists? | Lot Jan 2026 | Freeze | Notes |
|---|---|---:|---|---|
| FINNIFTY | ✅ Monthly only | 60 ✅ | UNVERIFIED | Weekly discontinued Nov 19, 2024 |
| MIDCPNIFTY | ✅ Monthly only | 120 ✅ | UNVERIFIED | Weekly discontinued Nov 18, 2024 |
| BANKEX (BSE) | ✅ Monthly only | 30 ✅ | UNVERIFIED | Last Thursday since Sept 1, 2025 |
| NIFTYNXT50 | ✅ Monthly only | 25 ✅ | 600 ✅ | Liquidity historically thin |

All 4 instrument lot sizes match prompt. Freeze quantities for
FINNIFTY/MIDCPNIFTY/BANKEX are UNVERIFIED (NSE circulars HTTP 403).

### 8.3 Dhan-specific gotcha — ₹20 on worthless options

**NEW FINDING:** Dhan charges ₹20 per executed order EVEN when an
option expires worthless. Unlike Zerodha which dropped this to ₹0 in
June 2024. Material for short-OTM held-to-expiry strategies (each
worthless short costs ₹23.60 = ₹20 + 18% GST).

### 8.4 Operator scope decision: ADOPT STRUCTURE ONLY

Operator chose "Adopt structure only" (recommended option). Means:

- **KEEP** current Track 2 scope: NIFTY / BANKNIFTY / SENSEX options only
- **DO NOT** add futures (separate scope expansion required)
- **DO NOT** add FINNIFTY / MIDCPNIFTY / BANKEX / NIFTYNXT50 (separate
  scope expansion required)
- **ADOPT** the prompt's 5-action TradeOutcome enum for forward-
  compatibility with future tracks that hold to expiry
- **ADOPT** the prompt's per-leg modular file structure (12 files)
  as the Phase 3 implementation plan (when code starts)

### 8.5 Spec amendments this round (CALCULATOR_SPEC.md)

- §1 inputs: replaced `held_to_expiry_exercise: bool` with
  `trade_outcome: TradeOutcome` enum
- §4.2 rewritten as 4-outcome state machine
- §4.3 added: Dhan ₹20-on-worthless gotcha
- §4.4 added: prompt-error correction recorded (per-contract WRONG)
- §11 API: TradeOutcome enum added
- §16 unverified items: 14 → 17 (added items 15-17)
- §18 amendment log: +6 entries

### 8.6 Items deferred for separate scope expansion

If operator later expands scope (futures or other instruments), the
following research is needed BEFORE code-lock:

- [ ] Futures lot sizes per-date (NIFTY/BNX/SENSEX futures may use
  different lot history than options)
- [ ] Futures STT rate-era table (0.0125% pre-Oct-2024 → 0.02%
  post-Oct-2024 → 0.05% post-Apr-2026)
- [ ] Futures exchange transaction charges per contract
- [ ] Freeze qty for FINNIFTY / MIDCPNIFTY / BANKEX (NSE/BSE
  primary-source PDFs)
- [ ] Strike intervals for FINNIFTY / MIDCPNIFTY / BANKEX /
  NIFTYNXT50 (likely 50/25/100/n, but not verified)
- [ ] NIFTYNXT50 monthly expiry day post-Sept-2025 alignment

---

**END OF FINALIZATION DOC v2.**
