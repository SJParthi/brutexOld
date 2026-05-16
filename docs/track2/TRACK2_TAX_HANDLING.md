# Track 2 — Tax Handling Specification

**Status:** DRAFT 2026-05-16. Based on deep research agent D findings.
Verify items marked UNVERIFIED at primary sources before locking.

**Scope:** Annual income-tax aggregation for F&O option trading on
NIFTY 50, BANKNIFTY, and SENSEX. Computes post-tax PnL from per-trade
pre-tax PnL produced by `CALCULATOR_SPEC.md`. Applies to an individual
resident Indian retail trader.

**Out of scope:**
- Non-resident trader taxation
- Corporate / LLP / HUF structures
- GST output for service providers
- Tax planning advice (this is a simulator, not a CA)

**NOT legal advice.** Operator must validate the post-tax PnL pipeline
with a chartered accountant before relying on it for real-money decisions.

---

## 1. Income Classification

F&O trading income on a recognized stock exchange is classified as
**non-speculative business income** under Section 43(5) proviso (d)
of the Income Tax Act 1961 (carried forward as Section 66 of the
Income Tax Act 2025, effective 2026-04-01).

This means:
- Reported under "Profits and Gains of Business or Profession" (PGBP)
- NOT capital gains
- ITR-3 form required (with books of account)
- Slab rates apply (5% / 10% / 15% / 20% / 25% / 30%) + 4% Health
  & Education Cess + applicable surcharge
- Setoffs and carry-forward governed by Sections 70, 71, 72
- 8-year loss carry-forward if filed on time

### 1.1 Section 44AD presumptive scheme — NOT used

Section 44AD allows presumptive taxation at 6%/8% of turnover, but
this scheme is unsuitable for F&O traders for four reasons:

1. Turnover limit (₹2-3 Cr) often exceeded by F&O absolute-P&L.
2. Deemed profit 6% typically exceeds real F&O margins.
3. Once opted out of 44AD, a 5-year mandatory audit lock triggers
   under Section 44AD(4).
4. Practitioner consensus (ClearTax, Quicko, Tax2win) recommends
   against 44AD for F&O.

**Track 2 assumption:** trader has NEVER opted into 44AD. Logged as
DEC-TAX-003 in MIGRATION.md.

### 1.2 Income Tax Act 2025 transition

The new Income Tax Act 2025 replaces the 1961 Act effective 2026-04-01.
Section 43(5) becomes Section 66. The substance of "F&O is non-speculative
business income" is preserved. Simulator must reference:
- Section 43(5) for backtest dates < 2026-04-01
- Section 66 for backtest dates ≥ 2026-04-01

---

## 2. Tax Rate Structure (FY 2025-26)

### 2.1 New Regime (default, Section 115BAC)

| Income range | Rate |
|---|---|
| Up to ₹3 lakh | 0% |
| ₹3-7 lakh | 5% |
| ₹7-10 lakh | 10% |
| ₹10-12 lakh | 15% |
| ₹12-15 lakh | 20% |
| ₹15-24 lakh | 25% (effective FY 2025-26) |
| Above ₹24 lakh | 30% |

Section 87A rebate raises the effective tax-free threshold to
₹12 lakh under new regime.

### 2.2 Cess and surcharge

- **Health & Education Cess:** 4% on (tax + surcharge)
- **Surcharge** (new regime):
  - 10% if total income > ₹50 lakh
  - 15% if total income > ₹1 Cr
  - 25% if total income > ₹2 Cr (max under new regime — 37% removed)

### 2.3 Old Regime (override)

Old regime preserves Chapter VI-A deductions (80C, 80D, HRA, LTA, etc.)
but requires explicit opt-out via Form 10-IEA. Business-income filers
who opt out of new regime can re-enter only ONCE in their lifetime
per Section 115BAC(6) — binding lifetime constraint.

Default for Track 2 simulator: **new regime**. Old regime available
via configuration flag.

---

## 3. Turnover Computation (ICAI Revised 2025 Guidance Note)

Per ICAI Guidance Note on Tax Audit u/s 44AB (Revised 2025 Edition,
released July 2025; supersedes 8th Edition 2022):

**Turnover for F&O = sum of absolute values of profit + loss of each
squared-off trade (both futures and options).**

Key revision from 2022 edition:
- Options premium received on SALE is included **only if not already
  included** in net profit computation.
- The pre-2022 double-counting rule was removed.
- Reverse trades: difference forms part of turnover; no separate
  premium addition.

### 3.1 Track 2 implementation

For each squared-off trade emitted by the per-trade calculator:
```
trade_turnover_paisa = abs(net_pnl_paisa)
annual_turnover_paisa = SUM(trade_turnover_paisa over the FY)
```

No premium re-add. No double-counting. This matches the ICAI 2025
revised rule.

---

## 4. Tax Audit Requirements

Per Section 44AB read with ICAI Revised 2025 Guidance Note:

| Annual turnover | Cash receipts > 5% | Audit required? |
|---|---|---|
| ≤ ₹1 Cr | any | No (subject to 44AD(4)) |
| > ₹1 Cr ≤ ₹10 Cr | No (≤5%) | No |
| > ₹1 Cr ≤ ₹10 Cr | Yes (>5%) | Yes |
| > ₹10 Cr | any | Yes |

F&O trading is 100% electronic, so the cash-receipts test is always
satisfied. The effective threshold for F&O is **₹10 Cr turnover**.

### 4.1 Penalty for missing audit (Section 271B)

0.5% of turnover, capped at ₹1,50,000.

### 4.2 Track 2 implementation

The simulator raises a warning when simulated annual turnover crosses
₹10 Cr, since this implies ~₹3-5 lakh additional annual CA compliance
cost. This cost is NOT auto-deducted from post-tax PnL but is flagged
in audit log as `audit_threshold_crossed: true` for operator awareness.

---

## 5. Deductible Expenses

Business expenses are deductible against F&O business income under
BOTH old and new regimes (PGBP deductibility is preserved in new regime;
only Chapter VI-A deductions are blocked).

### 5.1 Per-trade deductible items

These are already captured by `CALCULATOR_SPEC.md` per-trade output:
- STT paid (Section 36(1)(xv))
- Brokerage
- Exchange transaction charges
- SEBI fee
- Stamp duty
- GST on services (NOT input GST — that is not claimable for individuals)

### 5.2 Annual overhead deductibles (NOT in per-trade calc)

Track 2 simulator does NOT auto-apply these — operator configures as
annual flat amounts:

- Internet / phone (apportioned share)
- Electricity (apportioned share)
- Computer / laptop depreciation: 40% WDV
- Subscriptions: data feed, research, screener, charting
- Salary to staff (if any)
- Office rent (if any)
- Accounting / audit fees

### 5.3 Disallowed under Section 40A(3)

Cash payment > ₹10,000 to a single payee is disallowed. Track 2 does
not model cash payments (all simulated trades are electronic), so this
is a no-op for the simulator. Documented for operator awareness when
filing real returns.

---

## 6. Set-Off and Carry-Forward Rules

### 6.1 Current-year set-off

| F&O loss against | Allowed? | Section |
|---|---|---|
| F&O gain (same year) | YES (intra-head) | Section 70 |
| Other non-speculative business income | YES | Section 70 |
| Capital gains | YES | Section 71 |
| House property income | YES | Section 71 |
| Other sources (interest, dividend) | YES | Section 71 |
| **Salary** | **NO** | Section 71 explicit bar |

### 6.2 Carry-forward (8 assessment years)

Per Section 72:
- F&O losses can be carried forward 8 AYs
- Only against future business income (including future F&O)
- **HARD CONDITION:** ITR must be filed on or before due date per
  Section 139(1). Late filing forfeits carry-forward.

### 6.3 Track 2 implementation

The simulator maintains a per-strategy `loss_carryforward_ledger`:
- Year N pre-tax PnL → if negative, append to ledger
- Year N+1 pre-tax PnL → if positive, offset against earliest ledger entry first
- Entries expire after 8 AYs
- A configurable flag `filed_on_time: true/false` controls whether
  losses carry forward. Default: `true` (assumes disciplined filing).

For backtest reporting, the simulator emits both:
- **Naive post-tax** (per-year tax applied to per-year pre-tax)
- **Carry-forward-adjusted post-tax** (using ledger)

These can differ materially for multi-year backtests with mixed
profit/loss years.

---

## 7. Advance Tax (Section 211)

### 7.1 Quarterly schedule

Trigger: total tax liability > ₹10,000 in the FY.

| Due date | Cumulative % of total tax |
|---|---|
| 15 June | 15% |
| 15 September | 45% |
| 15 December | 75% |
| 15 March | 100% |

### 7.2 Interest for default

- **Section 234C** (deferment): 1% per month for 3 months on shortfall
  at each of the first 3 quarters; 1 month for Q4.
- **Section 234B** (default): 1% per month from 1 April of next FY
  if total advance tax paid < 90% of liability.

### 7.3 Track 2 implementation

F&O P&L is too volatile to schedule advance tax exactly mid-year.
The simulator does NOT attempt per-quarter compliance modeling.

Instead, it applies a conservative **234C interest buffer** of ~2%
of pre-tax profit when computing post-tax PnL. Operator can adjust
this rate per backtest via `advance_tax_buffer_rate` config.

Decision logged as DEC-TAX-004.

---

## 8. Transactional Taxes (STT / Stamp / GST)

Track 2's per-trade calculator (`CALCULATOR_SPEC.md`) computes these
per-trade. Annual aggregation just sums them as deductible expenses.

### 8.1 STT rate schedule (verify before each new FY)

| Period | Options sell-side STT | Futures sell-side STT |
|---|---|---|
| Pre 2024-10-01 | 0.0625% | 0.0125% |
| 2024-10-01 → 2026-03-31 | 0.10% (VERIFIED) | 0.02% (VERIFIED) |
| **2026-04-01 onward** | **0.10% or 0.15%** (UNVERIFIED — Budget 2026 sources conflict) | **0.02% or 0.05%** (UNVERIFIED) |

UNVERIFIED items must be locked from gazetted Finance Act 2026 PDF
from `indiacode.nic.in` before Phase 2 of Track 2. Some sources
(Finnovate, ICICI Direct, BusinessToday 2026-02-02) claim 0.15% options
hike; others (Lakshmishree, ClearTax FY-26 page) still list 0.10%.

### 8.2 Stamp duty

0.003% on BUY-side options premium. Uniform since 2020-07-01 (post
the Finance Act 2019 amendment). Unchanged through 2026.

### 8.3 GST

18% on (brokerage + exchange transaction charges + SEBI fee + IPFT fee).
Calculated per-trade per CALCULATOR_SPEC.md §6.

---

## 9. Post-Tax PnL Computation Order

Per simulated FY:

```
1. gross_pnl_paisa        = SUM(trade.net_pnl over FY)
                            (trade.net_pnl is already post-charges per CALCULATOR_SPEC)

2. business_expenses      = annual_overhead_paisa (from operator config)
                            + depreciation_paisa
                            + audit_fees_paisa (if turnover > ₹10 Cr)

3. net_business_income    = gross_pnl_paisa - business_expenses

4. carry_forward_offset   = ledger.consume(net_business_income)  
                            (only if filed_on_time=true and ledger has entries)

5. taxable_income_paisa   = max(0, net_business_income - carry_forward_offset)

6. base_tax_paisa         = apply_slabs(taxable_income_paisa, regime)

7. surcharge_paisa        = base_tax_paisa * surcharge_rate(taxable_income)

8. cess_paisa             = (base_tax_paisa + surcharge_paisa) * 0.04

9. advance_tax_interest   = pre_tax_profit * advance_tax_buffer_rate
                            (default 2%, configurable)

10. total_tax_paisa       = base_tax_paisa + surcharge_paisa + cess_paisa
                            + advance_tax_interest

11. post_tax_pnl_paisa    = net_business_income - total_tax_paisa
```

If `net_business_income` is negative, no tax applies, and the loss is
appended to the carry-forward ledger (if `filed_on_time=true`).

---

## 10. Decision IDs (for MIGRATION.md)

| ID | Decision | Rationale |
|---|---|---|
| DEC-TAX-001 | New regime (Section 115BAC) as default | Default since FY 2023-24; preserves PGBP business expenses |
| DEC-TAX-002 | STT rate source = gazetted Finance Act, NOT broker page | Primary-source rule; Budget 2026 sources conflict |
| DEC-TAX-003 | Trader has never opted into Section 44AD | Avoids 5-year audit lock trap |
| DEC-TAX-004 | Advance-tax 234C buffer = 2% of pre-tax profit (configurable) | F&O P&L too volatile to model per-quarter |
| DEC-TAX-005 | Income Tax Act 2025 transition 2026-04-01 (Section 43(5) → Section 66) | Substance preserved; cosmetic spec change |
| DEC-TAX-006 | Carry-forward conditional on `filed_on_time` flag | Section 80 read with 139(1) — late filing forfeits |
| DEC-TAX-007 | ICAI Revised 2025 turnover formula (no premium re-add) | Supersedes 2022 edition |
| DEC-TAX-008 | Audit-threshold warning at ₹10 Cr turnover | Real compliance cost flag |

---

## 11. Items Flagged UNVERIFIED — operator action required

1. **Budget 2026 / Finance Act 2026 STT rates** — sources conflict
   (options 0.10% vs 0.15%, futures 0.02% vs 0.05%). Fetch gazetted
   Finance Act 2026 from `indiacode.nic.in` before locking. Blocks
   Phase 2 of Track 2 cost-model implementation.

2. **STT-on-exercise rate split** (0.125% pre-Apr-2026 vs 0.15% post)
   — not corroborated in retrieved sources beyond claude-chat prompt.
   Same Finance Act 2026 source needed.

3. **Whether NSE/BSE exchange transaction charges changed alongside
   the 2024-10-01 STT revision** — Zerodha post indicates yes, exact
   new rates not captured here. Verify in primary NSE/BSE circulars.

4. **NSE/BSE circular numbers** for the 2025-09-01 freeze-quantity
   revision — only abbreviated identifiers found; primary fetch needed.

---

## 12. Sources

- Income Tax Act 1961, Section 43(5) proviso (d)
- Income Tax Act 2025 (effective 2026-04-01), Section 66
- ICAI Guidance Note on Tax Audit u/s 44AB Revised 2025 (July 2025)
- CBDT circulars and ITR-3 instructions
- Sahi — Income Tax on F&O Trading FY 2025-26 Guide
- ClearTax — F&O Taxation, Section 115BAC, Set-Off & Carry Forward
- Tax2win — F&O Income Business Treatment, Section 234C
- Quicko — Trading Turnover Calculation, Expenses a Trader Can Claim
- Zerodha Varsity — Taxation for Traders chapter
- Zerodha Z-Connect — Advance Tax, STT & Transaction Charges Revision 2024-10-01
- Business Standard — Budget 2024 STT Hike on F&O
- ICICI Direct — STT Changes in Budget 2026 (UNVERIFIED secondary)
- Finnovate — Budget 2026 STT Hike on F&O (UNVERIFIED secondary)
- BusinessToday — Budget 2026 IT Act, STT Hike (UNVERIFIED secondary)
- Lakshmishree — STT Rates in India 2026 (UNVERIFIED secondary)
- TaxBuddy — Stock Trading Income Taxation FY 2024-25
- PL Capital — ITR-3 Derivatives Filing Guide 2025
- A2Z Taxcorp — Comprehensive Guide to Section 44AB ICAI Revised 2025
- HNLU CCLS — Section 43(5) Speculative Turn on F&O Taxation
- LawChakra — Income Tax Act 2025 Key Changes

---

## 13. Amendment Log

| Date | Change | Reason |
|---|---|---|
| 2026-05-16 | Initial draft | Deep research Agent D findings |
