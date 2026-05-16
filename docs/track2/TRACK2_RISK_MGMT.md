# Track 2 — Risk Management Specification

**Status:** DRAFT 2026-05-16. Based on deep research agent C findings.
Verify primary-source citations marked UNVERIFIED before code-lock.

**Scope:** Risk management rules governing the Track 2 options execution
simulator. Covers SEBI position limits, margin requirements, tail-risk
controls, broker auto-square-off behavior, expiry-day rules, and circuit
breakers. Applies to retail single-client trading of index options on
NIFTY 50, BANKNIFTY, and SENSEX under Dhan (primary) or Zerodha
(fallback comparison).

**Out of scope:**
- Live order placement (signal-replay only; no execution against real markets)
- Portfolio-level cross-strategy netting (each strategy evaluated standalone)
- Counterparty default risk (clearing corp insurance assumed sufficient)
- Single-stock derivatives (Track 1 universe is indices only)

**Single source of truth for numeric constants:** this document.
Numeric constants will eventually move to `brutex/risk/limits.py`.

---

## 1. SEBI Position Limits (retail client)

### 1.1 Client-level open interest caps (effective 2025-10-01)

Per SEBI/HO/MRD/TPD-1/P/CIR/2025 framework on intraday position limits
for equity index derivatives (gazetted Sept 2025; full enforcement
2025-10-01).

| Limit | Value | Notes |
|---|---|---|
| EOD net OI per client (delta-adjusted FutEq) | ₹1,500 cr | hard cap |
| EOD gross OI per client (FutEq) | ₹10,000 cr | long or short side |
| Intraday net OI per entity (FutEq) | ₹5,000 cr | delta-adjusted |
| Intraday gross OI per entity (FutEq) | ₹10,000 cr | both sides combined |
| Monitoring snapshots | ≥4 random/day; one between 14:45-15:30 IST | SEBI mandate |
| Breach cure period | 15 minutes | persistence into next snapshot = provisional breach |

**FutEq metric definition:** delta-adjusted open interest expressed as
equivalent futures contracts. The unit changed from notional to FutEq
on 2025-10-01. Track 2 must compute position sizing in delta-adjusted
terms when computing position-limit headroom, NOT notional.

### 1.2 Freeze quantity per order (fat-finger cap)

Per NSE circular FAOP68834 / FAOP70616 effective 2025-09-01.

| Index | Freeze qty (lots) | Source |
|---|---|---|
| NIFTY 50 | 1,800 | NSE FAOP68834 |
| BANKNIFTY | 900 | NSE 2025-09-01 circular |
| SENSEX | 1,000 | UNVERIFIED — operator value; needs primary BSE circular |

Freeze quantity is the maximum number of lots per single order leg.
Orders exceeding the cap are rejected at the exchange gateway. The
backtest simulator must reject any combo trade requesting more than
the freeze quantity per leg.

### 1.3 Market-wide position limit (MWPL)

MWPL applies to single-stock derivatives, NOT index options. Index
options on NIFTY 50, BANKNIFTY, and SENSEX are exempt from the F&O
ban list mechanism. Confirmed via NSE clearing rules.

---

## 2. Margin Requirements (short option positions)

### 2.1 Margin stack composition

For any short index option position, the broker reserves:

1. **SPAN margin** — worst-case 1-day risk per SPAN methodology, 16
   scenario array, recalibrated 6× intraday by the exchange.
2. **Exposure margin** — additional buffer, typically 3-5% of contract
   value for index options.
3. **Extreme Loss Margin (ELM)** — separate add-on calibrated per
   instrument by the clearing corporation.
4. **Expiry-day additional ELM** — +2% of contract value on short
   index options OPEN AT SOD on expiry day OR opened intraday on
   the expiring contract. Effective 2024-11-20 per SEBI Oct-2024
   circular. Material cost.
5. **Mark-to-market (MTM)** — settled in cash daily; not a margin per
   se but reduces available margin headroom.
6. **Peak margin** — must be maintained throughout day, not just EOD.
   SEBI rule effective 2021-09-01, 100% upfront.

### 2.2 Peak-margin penalty schedule

Per SEBI/HO/MRD/DRMNP/CIR/P/2020/235:

| Shortfall duration / fraction | Penalty |
|---|---|
| Any shortfall, day 1-3 | 0.5% of shortfall amount per day |
| Persistent shortfall, day 4+ | 5% per day |
| Persistent ≥5 days in same month | 5%/day, continuous |

The backtest engine must model peak-margin penalty as a real cost
when simulating short positions, not just a flag. Computed against
4 random intraday snapshots per simulated day (mirror SEBI methodology).

### 2.3 Order-of-magnitude margin estimates

UNVERIFIED — must be computed live from broker SPAN file at backtest
time. These are sanity-check ranges only.

| Index | Lot size (2026) | Short ATM margin per lot (approx) |
|---|---|---|
| NIFTY 50 | 75 | ₹1.0-1.5 lakh |
| BANKNIFTY | 35 | ₹1.5-2.0 lakh |
| SENSEX | 20 | ₹1.5-2.0 lakh |

Track 2 should NOT hard-code margin constants. Either:
(a) pull a SPAN file per backtest day from exchange archives, OR
(b) use a deterministic SPAN replica (Black-Scholes shock grid + VIX bucket),
flag both in audit log with `margin_method` field.

---

## 3. Hard Risk Rules (conservative defaults)

The following rules are baked into the simulator as defaults. All
are operator-configurable but kill-switch limits cannot be raised
without explicit ADR.

| Rule | Default | Rationale |
|---|---|---|
| Max loss per day | 2-3% of capital (kill-switch) | retail circuit-breaker; aligns with SEBI's 90% retail-loser finding |
| Max consecutive losses → cool-down | 5 trades or 2 trading days | practitioner consensus |
| Per-trade risk | 0.5-1% of capital (fixed-fractional) | conservative; quarter-Kelly equivalent |
| Single-index concentration cap | ≤ 60% of capital | diversifies across 3-index universe |
| Drawdown ladder: reduce size 50% | at 10% running drawdown | early de-risk |
| Drawdown ladder: halt | at 20% running drawdown | hard stop |
| Pin-risk avoidance | force-exit by 15:15 IST if \|spot − strike\| < 0.3% of spot | redundant given Track 1's 15:20 forced exit, kept as belt-and-suspenders |

### 3.1 Position sizing methods supported

The engine supports four methods (operator selects per backtest):

1. **Fixed-rupee** — constant lot count per trade
2. **Fixed-fractional** — % of equity per trade (default: 1%)
3. **Volatility-targeting** — inversely proportional to India VIX
   bucket at entry (uses `vix_at_entry` already stamped per Track 1
   rule §2.3)
4. **Fractional Kelly** — half-Kelly or quarter-Kelly only. Full Kelly
   is REJECTED — fails on IV estimation error per arxiv:2508.16598.

### 3.2 Stop-loss model

Two stops apply, whichever fires first:
1. **Premium-based:** 30-50% of paid premium for long options (configurable per strategy mask in vocabulary).
2. **Time-based:** force-exit at 15:20 IST per Track 1 rule §7.

For short positions, premium-based stop becomes "premium grows by X%"
trigger (e.g., short premium ₹100 → exit if quote rises to ₹150).

---

## 4. Tail-Risk Specific to Index Options

### 4.1 Historical reference events

The simulator's worst-case stress test must size positions against
these documented Indian-market tail events:

| Date | Event | Impact |
|---|---|---|
| 2020-03-23 | COVID lockdown | NIFTY −12.98% intraday; India VIX 80+; short straddles wiped out |
| 2024-06-04 | Election shock | NIFTY −9% intraday; SENSEX −6,000 pts; FPI net sell ₹12,436 cr |
| 2024-08-05 | Global VIX spike (carry-trade unwind) | CBOE VIX +180% intraday; India VIX co-moved |
| 2025 Sep-Mar | Election-cycle correction | ~13% peak-to-trough |

### 4.2 Mandatory pre-deployment stress tests

Before any strategy mask is promoted from "candidate" to "deployable",
the simulator must run three gap-down stress scenarios:

1. **−3% gap-open** vs each short-option position
2. **−6% gap-open** vs each short-option position
3. **−10% gap-open** vs each short-option position

Any combo whose simulated single-event loss exceeds 5% of capital is
flagged as `tail_risk_fail` in audit log and excluded from the
deployable set (regardless of Sharpe ratio).

### 4.3 Liquidity floor

Reject any signal whose target strike has either:
- EOD open interest < 10,000 contracts, OR
- Average bid-ask spread > 5% of premium

Both fields must be encoded in the Track 1 vocabulary appendix as
`oi_at_entry` and `spread_at_entry` (currently planned as audit fields;
this spec elevates them to gating fields for Track 2).

### 4.4 Far-OTM expiry-day restrictions

No new far-OTM short option positions in the last 30 minutes of
expiry day (15:00-15:30 IST). Combined with the SEBI +2% ELM, the
risk/reward becomes asymmetric and liquidity collapses.

---

## 5. Broker Auto-Square-Off Behavior

Track 2 simulates Dhan as primary and uses Zerodha as comparison sanity-check.

| Broker | Product | Auto-SQ time IST | Per-order charge |
|---|---|---|---|
| Dhan | MIS (intraday F&O) | 15:20 (admin SQ starts 15:19 NSE / 15:18 BSE) | ₹20 + 18% GST |
| Dhan | NRML | no auto-SQ; held to expiry | n/a |
| Zerodha | MIS (F&O) | 15:26 (revised 2025; was 15:25 pre-revision) | ₹50 + 18% GST |
| Zerodha | NRML | no auto-SQ | n/a |

### 5.1 Dhan margin-shortfall auto-close

Dhan auto-closes positions when margin shortfall > ₹5,000 OR
mark-to-market risk-of-shortfall is detected. Source: dhan.co/risk-management-policy.

### 5.2 Implication for Track 1's forced-exit 15:20 lock

Track 1 rule §7 mandates forced exit at 15:20 IST. This aligns
perfectly with Dhan MIS auto-SQ. For Zerodha MIS, it is conservative
(6 minutes earlier than Zerodha's 15:26 auto-SQ). Backtest must use
15:20 strict so signals translate cleanly across brokers.

---

## 6. Expiry-Day Rules (effective 2024-11-20 onward)

Per SEBI Oct-2024 circular SEBI/HO/MRD/TPD-1/P/CIR/2024/132:

1. **+2% additional ELM** on all short index options open at SOD AND
   any short opened intraday on the expiring contract.
2. **Calendar-spread margin benefit REMOVED** on expiry day
   (effective 2025-02-01).
3. **Wider bid-ask spreads** in last 60 minutes — simulator should
   model spread inflation.
4. **Weekly-expiry rationalization:** each exchange has only ONE
   weekly index now:
   - NSE: NIFTY 50 weekly
   - BSE: SENSEX weekly
   - BANKNIFTY weekly DISCONTINUED post 2024-11-20 (monthly only)
   - FINNIFTY, MIDCPNIFTY, NIFTYNXT50 weekly DISCONTINUED (monthly only)

**Material impact on Track 2 universe:** BANKNIFTY post-Nov-2024 has
no weekly options. Track 2's expiry calendar must reflect this. The
existing `brutex/options/expiry_calendar.py` module must validate
against this rule.

---

## 7. Circuit Breakers (market-wide halt)

NSE/BSE market-wide index-level circuit breakers can pre-empt the
15:20 forced exit. Simulator must respect:

| Trigger | Time of trigger | Halt duration |
|---|---|---|
| 10% move | < 13:00 IST | 45-min halt |
| 10% move | 13:00-14:30 IST | 15-min halt |
| 10% move | > 14:30 IST | no halt (continue) |
| 15% move | < 13:00 IST | 1h 45m halt |
| 15% move | 13:00-14:00 IST | 45-min halt |
| 15% move | > 14:00 IST | DAY CLOSE |
| 20% move | any time | DAY CLOSE |
| Re-open after halt | — | pre-open call auction |

### 7.1 Backtest implication

If any 1-min bar shows index move ≥10%/15%/20% from previous close,
the simulator must:
1. Halt simulated trading per matrix above.
2. Stamp `circuit_triggered` flag in audit log.
3. If 15% post-14:00 OR 20% any-time fires, mark the day as DAY CLOSE
   and emit `circuit_close` event. Existing open positions are not
   force-exited at 15:20 (the day already closed); the next session's
   open is the next event.

The audit log must distinguish "forced 15:20 exit" from "circuit-triggered
close" in the `exit_reason` field.

---

## 8. Open Interest Surveillance (SEBI Sept 2025)

The 2025-10-01 framework introduced:
- FutEq OI (delta-adjusted) replaces notional OI as the metric.
- Intraday net 5,000 cr / gross 10,000 cr per entity (FutEq).
- Cure period 15 min; persistent breach = provisional breach → exchange action.
- Volatility input = max(prev EOD underlying ann vol, prev EOD futures ann vol).
- Glide path: 2025-07-01 → 2025-12-05; full effect 2025-10-01.

Track 2 simulator must compute positions in delta-adjusted FutEq terms
for any backtest date ≥ 2025-10-01. For dates before that, use notional
methodology. Switch is date-gated, not operator-configurable.

---

## 9. Settlement Constraints

- **F&O / index options:** cash-settled by definition. No physical
  settlement. No T+0 in SEBI roadmap.
- **Daily MTM** settled in cash each evening.
- **Spot index** (Track 1): not tradable, no settlement.

---

## 10. Decision IDs (for MIGRATION.md)

The following risk-management decisions must be logged in
`docs/MIGRATION.md` decision ledger:

| ID | Decision | Rationale |
|---|---|---|
| DEC-RISK-001 | Max loss/day = 2-3% of capital (kill-switch) | retail safety; SEBI 90% loser finding |
| DEC-RISK-002 | Per-trade risk = 0.5-1% fixed-fractional default | conservative; quarter-Kelly equivalent |
| DEC-RISK-003 | Single-index cap = 60% of capital | diversification across 3-index universe |
| DEC-RISK-004 | DD ladder: 50% reduction at 10% DD, halt at 20% | early de-risk + hard stop |
| DEC-RISK-005 | Full Kelly REJECTED; only half/quarter Kelly | IV estimation error |
| DEC-RISK-006 | Liquidity floor: OI ≥ 10,000 AND spread ≤ 5% premium | exit-cost protection |
| DEC-RISK-007 | Gap-down stress: −3%, −6%, −10% mandatory | tail-event sizing |
| DEC-RISK-008 | Forced exit 15:20 strict (Dhan MIS aligned) | broker portability |
| DEC-RISK-009 | Position sizing in FutEq from 2025-10-01 | SEBI rule |
| DEC-RISK-010 | Circuit-triggered close ≠ forced 15:20 exit | audit distinction |

---

## 11. Items Flagged UNVERIFIED — operator action required

1. **SENSEX freeze quantity 1,000 lots** — operator-known but not
   primary-source-verified here. Phase 0 Track 2 must fetch BSE
   circular and lock the value.

2. **BANKEX freeze quantity** — not researched here. If BANKEX is
   ever added to scope (currently out-of-scope per Golden Rule #3),
   primary-source fetch required.

3. **SPAN margin order-of-magnitude estimates** — not for use as
   hard values. Simulator must compute from per-day SPAN file or
   deterministic SPAN replica.

4. **Budget 2026 STT-hike effect on retail risk-of-ruin probability**
   — qualitatively material, quantitatively not modeled here. Defer
   to first empirical post-Apr-2026 backtest pass.

---

## 12. Sources

- SEBI circular SEBI/HO/MRD/TPD-1/P/CIR/2024/132 (Oct 2024) — Equity Index Derivatives Framework
- SEBI circular SEBI/HO/MRD/TPD-1/P/CIR/2025 (Sept 2025) — Intraday Position Limits Framework
- SEBI circular SEBI/HO/MRD/DRMNP/CIR/P/2020/235 — Peak Margin Rules
- NSE circular FAOP68834 / FAOP70616 (Sept 2025) — Quantity Freeze Limits
- NSE Equity Market Circuit Breakers (`nseindia.com/products-services/equity-market-circuit-breakers`)
- BSE MWPL page (`bseindia.com/markets/Derivatives/DeriReports/MWPL.html`)
- Dhan Risk Management Policy (`dhan.co/risk-management-policy`)
- Dhan Auto Square-Off FAQ (Freshdesk article 82000832236)
- Zerodha Z-Connect — SEBI's new rules for index derivatives
- Zerodha Bulletin 249809 — latest intraday leverages
- Business Standard 2025-05-29 — Sebi new OI metric
- Business Standard 2025-09-02 — stricter intraday position limits
- Business Standard 2024-06-04 — Election crash
- arxiv:2508.16598 — Sizing the Risk: Kelly, VIX, and Hybrid Approaches in Put-Writing
- Sahi Blog — Margins and Capital Requirements (peak margin penalty schedule)
- share.market — Freeze Quantity for Index F&O on BSE and NSE

---

## 13. Amendment Log

| Date | Change | Reason |
|---|---|---|
| 2026-05-16 | Initial draft | Deep research Agent C findings |
