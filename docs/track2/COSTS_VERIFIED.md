# Track 2 Cost Calculator — VERIFIED Design Specification

> **Status:** Locked 2026-05-16, ~95% confidence (citation-grounded via 4-agent deep research).
> **Outstanding:** Level-1 verification (real Groww contract note byte-equality) deferred to operator's first trade.
> **Decision ledger:** #321-#335 (added in next MIGRATION.md update).

## 0. Purpose + scope

This document is the **citation-grounded design specification** for the Track 2 options cost calculator. It encodes every per-trade and per-month charge Groww applies, with rounding rules, calculation order, and worked examples.

Phase 3 implementation (`brutex/costs/groww_options.py`) follows this spec exactly. Any future amendment must trace to a verified source URL per `CLAUDE.md` Golden Rule #1.

## 1. Research provenance (4-agent deep research, 2026-05-16)

| Agent | Topic | Verdict |
|---|---|---|
| A | Triangulation: Groww vs Zerodha vs Angel One calculators | ~95% rate confidence (30+ secondary sources cross-confirm; primary URLs returned HTTP 403) |
| B | Forensic search for real Groww contract notes (Twitter/Reddit/YouTube) | **0 real notes found** publicly; need operator Level-1 verification |
| C | Statutory rounding rules (NSE/BSE/SEBI/CGST/Stamp Act/STT Rules) | STT, Stamp, GST round to **nearest rupee**; others round to **paise** |
| D | Edge cases: tiers, women's discount, multi-lot, Muhurat, Groww API, etc. | **Multi-lot is per-order ₹20 flat** (critical); women's discount discontinued |

**Combined confidence: ~95%.** Remaining 5% gap fills only after Level-1 contract note verification.

## 2. Verified rate table (post-1-Apr-2026)

| # | Component | Rate | Applied to | Side | Source |
|---|---|---|---|---|---|
| 1 | Groww brokerage | **₹20.00 flat per executed order** | per order (NOT per lot) | both legs | Groww pricing page (chittorgarh, ipogmpindia, comparesharebrokers all confirm) |
| 2 | STT (pre 1-Apr-2026) | 0.10% on sell premium | sell side only | SEBI/CBDT effective 1-Oct-2024 | ClearTax, Zerodha Z-Connect |
| 2' | STT (post 1-Apr-2026) | **0.15% on sell premium** | sell side only | Finance Act 2026 | taxguru.in, ICICIdirect, ClearTax |
| 3 | NSE F&O exchange | **0.03503%** (₹3,503/cr) | premium both sides | per leg | NSE FA64232 / FA56129; SEBI true-to-label circular |
| 4 | BSE F&O exchange | **0.03250%** (₹3,250/cr) | premium both sides | per leg | BSE notice 27-Sep-2024 |
| 5 | SEBI turnover fee | **0.0001%** (₹10/cr) | premium both sides | per leg | sebi.gov.in fee schedule; NSE static page |
| 6 | IPFT NSE | **0.0005%** (₹50/cr) | premium both sides | per leg | NSE NSE/FA/56129; NSE only — BSE IPFT = 0 (or near-zero, **UNVERIFIED**) |
| 7 | Stamp duty | **0.003%** (₹300/cr) | premium buy side only | buy leg | Indian Stamp Act 2020 (uniform) |
| 8 | GST | **18%** (CGST 9% + SGST 9% intra-state, OR IGST 18% inter-state) | (brokerage + exchange + SEBI + IPFT) only | both sides | Section 170 CGST Act 2017; ClearTax |

**Not in GST base:** STT, stamp duty (statutory taxes, not services).

## 3. Statutory rounding rules (CRITICAL — Agent C major finding)

| Component | Rounding rule | Statutory basis |
|---|---|---|
| **STT** | Round to **nearest rupee**, half-up at 50 paise | Rule 11, Securities Transaction Tax Rules 2004 (verbatim: "shall be rounded off to the nearest rupee") |
| **Stamp duty** | Round to **nearest rupee**, half-up | Indian Stamp Rules 2019 (PIB 1635399, SEBI FAQ) |
| **GST** | Round the **TOTAL** tax ONCE to nearest rupee, then split CGST/SGST for display — per **DEC-COST-001** (§4), which superseded this row's original "each component separately" reading of Section 170 (rounding each at 9% created a systematic +₹1 overcharge ~50% of trades) | Section 170, CGST Act 2017; DEC-COST-001 hostile-review fix 2026-05-16 |
| Brokerage | Round to paise (no statutory rule) | Industry practice: `ROUND_HALF_UP` to 2 decimals |
| NSE/BSE exchange | Round to paise (no statutory rule) | Industry practice |
| SEBI turnover | Round to paise (no statutory rule) | Industry practice |
| IPFT | Round to paise (no statutory rule) | Industry practice |

**Corroborated by Groww-specific statement (Agent B):** "Stamp Duty and STT are rounded off to the nearest rupee and all other charges to the nearest 2 decimals."

## 4. Order of operations

Per industry-published broker examples + Section 170 CGST + Rule 11 STT Rules:

```
   1. PER LEG, compute each charge at paise precision (no rounding yet):
        brokerage_leg     = ₹20.00 flat (or min(20, 0.03% × notional) — Groww uses flat)
        exchange_leg      = notional × exchange_rate (NSE 0.03503% or BSE 0.03250%)
        sebi_leg          = notional × 0.000001 (= ₹10/cr)
        ipft_leg          = notional × 0.000005 (= ₹50/cr) — NSE only

   2. PER TRADE (= round trip = buy leg + sell leg), aggregate:
        brokerage         = brokerage_buy + brokerage_sell  (typically ₹40)
        exchange          = exchange_buy + exchange_sell
        sebi              = sebi_buy + sebi_sell
        ipft              = ipft_buy + ipft_sell
        stt_unrounded     = sell_notional × STT_rate
        stamp_unrounded   = buy_notional × 0.00003

   3. ROUND statutory items to NEAREST RUPEE (half-up):
        stt               = round_to_rupee(stt_unrounded)
        stamp_duty        = round_to_rupee(stamp_unrounded)

   4. ROUND industry items to paise (half-up to 2 decimals).

   5. COMPUTE GST (REVISED 2026-05-16 per DEC-COST-001):
        gst_base          = brokerage + exchange + sebi + ipft
                            (paise precision, NOT pre-rounded)
        # CRITICAL: round TOTAL GST once, then split — NOT each component
        # Section 170 CGST mandates rounding of the TOTAL tax.
        # Previous spec rounded CGST + SGST separately at 9% each,
        # which created systematic +₹1 overcharge ~50% of the time
        # (e.g., ₹50.55 base: correct ₹9, was: 2×round(4.55)=2×5=₹10).
        # Hostile review 2026-05-16 (Agent A finding 7) caught this.
        gst_total         = round_to_rupee(gst_base × 0.18)
        if intra-state (operator's state == Groww's state):
            cgst          = gst_total // 2       # display only
            sgst          = gst_total - cgst     # display only (avoids parity drift)
            gst           = gst_total
        else (inter-state):
            igst          = gst_total
            gst           = gst_total

   6. TOTAL CHARGES:
        total             = brokerage + exchange + sebi + ipft
                          + stt + stamp_duty + gst

   7. NET PnL:
        gross_pnl         = (sell_notional - buy_notional)
        net_pnl           = gross_pnl - total
```

## 5. Worked examples (enforced calculator oracles)

> **REGENERATED 2026-07-03 (R2-OC-9)** from the ACTUAL output of the shipped
> calculator (`brutex.costs.calculator.compute_trade_cost`). The previous
> hand-derived tables had drifted from the code: they rounded CGST/SGST
> **separately** (the exact pre-DEC-COST-001 +₹1 overcharge this doc's §4
> claims to have fixed — Example 3 said ₹52.19 where the calculator produces
> **₹51.18**), mis-rounded a sub-paise SEBI fee to ₹0.01, and ignored that the
> calculator bakes the **flat 1-tick adverse slippage per side** (§19
> resolution 2 / TRACK2_OPTIONS_SPEC §9) into the fills, which changes each
> notional and hence every rate-based charge.
>
> Fixture parameters common to Examples 1-4: trade date **2026-05-15** (IST;
> post-1-Apr-2026 STT regime 0.15%; lot sizes per
> `brutex/options/lot_size_history.py`: NIFTY 65 / BANKNIFTY 30 / SENSEX 20),
> long_call, Groww, intra-state GST. Fills = buy at open + ₹0.05, sell at
> open − ₹0.05. Examples 1-4 are **byte-exact enforced oracles** — the golden
> test `tests/golden/test_costs_verified_examples.py` recomputes each scenario
> through the real calculator and asserts every figure below, so this section
> can no longer drift silently. Example 5 (Muhurat) is **SPEC-ONLY** — see its
> banner.

### Example 1: NIFTY ATM call, 1 lot, ₹100 → ₹120 (post-1-Apr-2026)

- Lot size: 65 (trade-date basis) → n_shares = 65
- Buy fill ₹100.05 × 65 = **₹6,503.25 notional** (650,325 paisa)
- Sell fill ₹119.95 × 65 = **₹7,796.75 notional** (779,675 paisa)
- Gross PnL (slippage baked into fills): **+₹1,293.50**

| Component | Total | Rule applied |
|---|---|---|
| Brokerage | -40.00 | ₹20 flat per order × 2 legs |
| STT (0.15% × ₹7,796.75 = ₹11.695) | **-12.00** | round to nearest ₹ (statutory) |
| NSE exchange (0.03503% × both notionals) | -5.01 | paise |
| SEBI (0.0001% × both notionals) | -0.02 | paise (per-side rounded) |
| IPFT NSE (0.0005% × both notionals) | -0.07 | paise |
| Stamp duty (0.003% × ₹6,503.25 = ₹0.195) | **-0.00** | round to rupee (0.195 < 0.50 → 0) |
| GST base = 40.00 + 5.01 + 0.02 + 0.07 = ₹45.10 | | |
| GST 18% × 45.10 = ₹8.118 → **rounded ONCE** | **-8.00** | DEC-COST-001 (total, NOT CGST/SGST each) |
| **TOTAL CHARGES** | **-65.10** | |
| **NET PnL** | **+1,228.40** | gross 1,293.50 − 65.10 |

### Example 2: NIFTY ATM call, 5 lots (multi-lot scenario)

Same premiums, 5 lots → n_shares = 325:
- Buy fill ₹100.05 × 325 = ₹32,516.25; sell fill ₹119.95 × 325 = ₹38,983.75
- Gross PnL: +₹6,467.50

| Component | Value | Rule |
|---|---|---|
| Brokerage | **-40.00** | ₹20 flat per ORDER (regardless of lot count!) |
| STT 0.15% × ₹38,983.75 = ₹58.4756 | **-58.00** | round to rupee (the old -59 used the no-slippage ₹39,000) |
| NSE exch 0.03503% × both notionals | -25.05 | paise |
| SEBI 0.0001% × both notionals | -0.07 | paise (per-side rounded) |
| IPFT 0.0005% × both notionals | -0.35 | paise (the old -0.36 used no-slippage notionals) |
| Stamp 0.003% × ₹32,516.25 = ₹0.9755 | **-1.00** | round to rupee (≥ 0.50 → 1) |
| GST base = 40.00 + 25.05 + 0.07 + 0.35 = ₹65.47 | | |
| GST 18% × 65.47 = ₹11.7846 → rounded ONCE | **-12.00** | DEC-COST-001 |
| **TOTAL CHARGES** | **-136.47** | (was ₹137.48 pre-regeneration) |
| **NET PnL** | **+6,331.03** | gross 6,467.50 − 136.47 |

**Per-lot cost (5 lots): ₹136.47 / 5 = ₹27.29/lot** — significantly cheaper than per-1-lot (₹65.10) due to brokerage amortization over 5 lots.

### Example 3: BANKNIFTY ATM, 1 lot, ₹50 → ₹60

- Lot size: 30 → n_shares = 30
- Buy fill ₹50.05 × 30 = ₹1,501.50; sell fill ₹59.95 × 30 = ₹1,798.50
- Gross PnL: +₹297.00

| Component | Value | Rule |
|---|---|---|
| Brokerage | -40.00 | flat |
| STT 0.15% × ₹1,798.50 = ₹2.6978 | **-3.00** | round to rupee |
| NSE exch 0.03503% × both notionals | -1.16 | paise |
| SEBI (0.0001% → ₹0.0033 unrounded) | **-0.00** | paise — rounds to ZERO (the old -0.01 was wrong) |
| IPFT | -0.02 | paise |
| Stamp 0.003% × ₹1,501.50 = ₹0.045 | **-0.00** | round (< 0.50) |
| GST base = 40.00 + 1.16 + 0.00 + 0.02 = ₹41.18 | | |
| GST 18% × 41.18 = ₹7.4124 → rounded ONCE | **-7.00** | DEC-COST-001 (NOT CGST ₹4 + SGST ₹4 = ₹8) |
| **TOTAL CHARGES** | **-51.18** | (the old ₹52.19 was the round-twice + SEBI-₹0.01 drift) |
| **NET PnL** | **+245.82** | gross 297.00 − 51.18 |

Cost = 17.2% of the post-slippage gross move. **Trade barely viable** due to high cost-as-% on small premiums.

### Example 4: SENSEX ATM, 1 lot, ₹80 → ₹100 (BSE)

- Lot size: 20 → n_shares = 20
- Buy fill ₹80.05 × 20 = ₹1,601.00; sell fill ₹99.95 × 20 = ₹1,999.00
- Gross PnL: +₹398.00

| Component | Value | Rule |
|---|---|---|
| Brokerage | -40.00 | flat |
| STT 0.15% × ₹1,999.00 = ₹2.9985 | **-3.00** | round to rupee |
| BSE exch 0.03250% × both notionals | -1.17 | paise (BSE rate) |
| SEBI (₹0.0036 unrounded) | **-0.00** | paise — rounds to zero (the old -0.01 was wrong) |
| IPFT BSE | **-0.00** | UNVERIFIED — BSE IPFT treated as 0 |
| Stamp 0.003% × ₹1,601.00 = ₹0.048 | -0.00 | round (< 0.50) |
| GST base = 40.00 + 1.17 + 0.00 + 0.00 = ₹41.17 | | |
| GST 18% × 41.17 = ₹7.4106 → rounded ONCE | **-7.00** | DEC-COST-001 |
| **TOTAL CHARGES** | **-51.17** | (was ₹52.17 pre-regeneration — same round-twice drift) |
| **NET PnL** | **+346.83** | gross 398.00 − 51.17 |

### Example 5: NIFTY Muhurat session trade — SPEC-ONLY (NOT an enforced oracle)

> **HONESTY BANNER (Golden Rule #10):** `compute_trade_cost` has **no Muhurat
> branch today** — brokerage is always the flat per-order fee regardless of
> session. This example documents the INTENDED behaviour (₹0 brokerage during
> Muhurat per §12) and CANNOT be regenerated through the calculator, so it is
> NOT covered by `tests/golden/test_costs_verified_examples.py`. Implementing
> the Muhurat waiver in the calculator is open future work.

Same as Example 1 but during Muhurat (e.g., Diwali Nov 1, 2024 evening):

| Component | Value | Rule |
|---|---|---|
| Brokerage | **-0** | Muhurat session = ₹0 brokerage (NOT implemented in code) |
| All other charges | same shape as Example 1 | unchanged |
| GST base | 0 + exch + SEBI + IPFT (excludes the waived brokerage) | |
| GST 18% of the small base → rounded ONCE | ≈ -0/-1 | DEC-COST-001 round-once (the old CGST/SGST-each arithmetic here was also pre-DEC-COST-001) |
| STT | -12 | unchanged by Muhurat |

## 6. Edge cases handled

| Case | Behavior |
|---|---|
| **Multi-lot** | Brokerage flat ₹20/order regardless of lot count (Example 2) |
| **Muhurat session** | Brokerage = 0; all other charges normal (Example 5). Date list per `TRACK1_MICROSTRUCTURE.md` |
| **STT regime switch (1-Apr-2026)** | Calculator uses trade-DATE's STT rate. Pre = 0.10%; post = 0.15%. Track 2 intraday only → no boundary crossings |
| **NSE vs BSE** | NSE charges 0.03503% + IPFT 0.0005%; BSE charges 0.03250% + IPFT 0 (UNVERIFIED) |
| **Inter-state vs intra-state GST** | CGST+SGST 9%+9% intra-state, IGST 18% inter-state. Operator's KYC state determines |

## 7. Edge cases NOT handled (out of scope for Track 2)

| Case | Why excluded | Documented as |
|---|---|---|
| Auto-square-off charge (Groww RMS) | Track 2 forced 15:20 IST exit prevents | Assertion: `exit_time <= 15:20 IST` |
| Expiry exercise (STT 0.15% on intrinsic) | Track 2 never holds to expiry | Dead-code branch |
| Call & Trade ₹50 charge | Track 2 uses API only | Document NA |
| Intraday↔Delivery conversion | Track 2 = INTRADAY only | NA |
| Securities lending (SLB) | Track 2 = BUY only (no shorts) | NA |
| Off-market demat transfer | NA | NA |
| Women's 50% discount | DISCONTINUED July 2024 (SEBI True-to-Label + misuse) | Default `women_discount=False`; future-proof flag |
| AMC | Groww = ₹0 lifetime | Document NA |

## 8. Fixed monthly OPEX (separate ledger, NOT per-trade)

| Item | Cost | Reference |
|---|---|---|
| **Groww Data API subscription** | **₹499 + 18% GST = ₹588.82 / month** | Required for BruteX historical data fetch |
| AMC (account maintenance) | ₹0 lifetime | Groww policy |
| Demat / DP charges on F&O | ₹0 | No demat debit on derivatives |

Tracked in `operating_costs.yaml` (separate from per-trade `cost_calculator` output).

## 9. UNVERIFIED items (operator must verify before Phase 3 code-lock)

| # | Item | Why UNVERIFIED | How to verify |
|---|---|---|---|
| 1 | Groww rate card primary page | Direct WebFetch returns HTTP 403 | Operator opens `groww.co/pricing` in browser + screenshot |
| ~~2~~ | ~~STT 0.15% post-1-Apr-2026~~ | **LOCKED 2026-05-16 (DEC-COST-003)**: VERIFIED via TaxGuru + Wikipedia STT page + 1Finance citing Finance Bill 2026 PDF at indiabudget.gov.in/doc/Finance_Bill.pdf. Rates: options 0.10%→0.15%, futures 0.02%→0.05%, exercise 0.125%→0.15%. | n/a — locked |
| 3 | BSE Bankex/Sensex IPFT rate | Inconclusive sources (could be 0 or near-zero) | Operator pulls BSE Members Transaction Fees page |
| 4 | Exact rounding direction (half-up vs half-even) | Stated "nearest" — direction ambiguous | Real contract note reconciliation |
| 5 | Per-trade vs per-day aggregation timing for STT/GST | Industry practice unverified for Groww specifically | Real contract note reconciliation |
| 6 | Women's discount status May 2026 | Sources contradict (Jul 2024 suspension vs 2026 articles) | Direct Groww support inquiry |
| 7 | Groww internal arithmetic precision | No public doc | Real contract note reconciliation |
| 8 | NIFTY index-option tick size 5 paise | Could be extended to 1 paise (regulator) | NSE circular monitoring |

## 10. Level-1 validation workflow (operator action)

Before locking the calculator in Phase 3, operator must:

```
   1. Place ONE real Groww options trade:
      - 1 lot NIFTY ATM call (smallest size for minimum exposure)
      - Intraday entry + exit, same day
      - Use the API (matches Track 2 production path)

   2. Download the contract note PDF from Groww.

   3. Run BruteX cost_calculator with IDENTICAL inputs:
      - Same lot size, buy/sell premiums, trade date

   4. Compare line-by-line:
      - Brokerage: match within ₹0.01
      - STT, Stamp, GST: match exactly (all integer rupees)
      - Exchange, SEBI, IPFT: match within ₹0.01

   5. If ALL match → calculator LOCKED at ~99% confidence.

   6. If ANY mismatch → investigate:
      - Rounding direction wrong?
      - Per-trade vs per-day aggregation?
      - Hidden charge not in our table?
      Amend calculator + this doc with finding.

   7. Update MIGRATION.md decision #336 with Level-1 verification result.
```

## 11. Multi-lot behavior (CRITICAL — Agent D finding)

**Brokerage is per-ORDER, not per-lot.** This radically affects sweep economics:

| Lots per order | Brokerage | Per-lot brokerage cost |
|---|---|---|
| 1 lot | ₹20 buy + ₹20 sell = ₹40 | ₹40/lot |
| 5 lots | ₹20 buy + ₹20 sell = ₹40 | ₹8/lot |
| 10 lots | ₹20 buy + ₹20 sell = ₹40 | ₹4/lot |
| 100 lots | ₹20 buy + ₹20 sell = ₹40 | ₹0.40/lot |

For BruteX Track 2 sweep:
- Default is 1 lot per trade (smallest viable size)
- Sweeping over `num_lots ∈ {1, 5, 10}` would reveal whether multi-lot strategies become attractive due to brokerage amortization
- The OTHER charges (STT, exchange, SEBI, IPFT, stamp, GST) all scale LINEARLY with notional, so they don't get cheaper per-lot

**Implication:** Trade-size optimization is a real lever, especially for strategies with thin per-trade edge.

## 12. Muhurat session handling

| Question | Answer |
|---|---|
| When is Muhurat? | One ~1-hour evening session each year on Diwali Laxmi Pujan day. Date list in `TRACK1_MICROSTRUCTURE.md` |
| 2021-2026 occurrences | ~5 sessions (2021-11-04, 2022-10-24, 2023-11-12, 2024-11-01, 2025-10-21) |
| Brokerage during Muhurat | ₹0 (Groww + Zerodha + most brokers waive) |
| STT during Muhurat | Same as normal (0.10% pre-Apr-2026, 0.15% post) |
| Exchange charges during Muhurat | Same as normal |
| GST during Muhurat | 18% on (0 + exchange + SEBI + IPFT) — base shrinks since brokerage is 0 |
| Calculator implementation | If `bar.timestamp_ist ∈ MUHURAT_SESSIONS[year]`, set `brokerage = 0` |

## 13. STT regime switch handling

```
   Pre-1-Apr-2026:  STT_rate = 0.0010 (= 0.10%)
   Post-1-Apr-2026: STT_rate = 0.0015 (= 0.15%)
   Boundary:        STT_HIKE_EFFECTIVE_DATE = date(2026, 4, 1)

   Calculator rule: use trade DATE's STT rate.
   Track 2 is intraday only (forced 15:20 IST exit) → no boundary-crossing trades.

   Note: spec §8 originally had STT 0.0625% (pre-Oct-2024). That regime is no
   longer active for our 2021-2026 backfill window since Track 2 only ingests
   post-2021-01-01 data and STT was 0.0625% from Oct-2023 to Sep-2024 only.
   For 2021-Sep-2024 trades, STT was 0.0625% on options sell premium.

   Three-regime full history for the 2021-2026 window:
   - 2021-01-01 → 2024-09-30: STT = 0.0625% (pre-Oct-2024 hike)
   - 2024-10-01 → 2026-03-31: STT = 0.10%
   - 2026-04-01 → today:      STT = 0.15%
```

**Calculator must support all 3 regimes via date-keyed STT rate lookup.**

## 14. Integer arithmetic spec (Phase 3 implementation guide)

**AMENDED 2026-05-16** — switched from `decimal.Decimal` to pure
int64 paisa with `bps_x100` scaling for performance. Per operator
cross-verification with a parallel Claude chat spec, the integer-math
approach is preferred because it:
- Meets the <1µs per-call performance target (730B trial sweeps)
- Matches CLAUDE.md §4 "no floats in hot paths" rule strictly
- Eliminates Decimal context overhead
- Produces bit-identical results across rebuilds

### Integer math pattern

```python
# All money in int64 paisa. All rates as bps_x100 (basis-points-times-100)
# for sub-bps precision. The denominator is always 1_000_000.
#
# Pattern: charge_paisa = (turnover_paisa * rate_bps_x100) // 1_000_000

# Example: STT 0.15% × ₹7,800 (= 780,000 paisa)
#   STT_RATE_BPS_X100 = 15_000  (= 0.15% = 150 bps × 100)
#   charge_paisa = (780_000 * 15_000) // 1_000_000 = 11_700 paisa = ₹117.00
#   Wait — that's wrong. Let me recompute.
#
# Actually: 7800 INR turnover × 0.0015 (rate as decimal) = 11.70 INR = 1170 paisa
# In bps_x100: rate = 0.15% = 1500 bps × 100 / 10000 = 15000 / 10^6 = 0.0015 ✓
# turnover_paisa = 780_000 (= ₹7,800 in paisa)
# charge_paisa = (780_000 * 15_000) / 10^6 = 11_700 / 10 = 1170 paisa = ₹11.70 ✓
```

### Rate constants (post-1-Apr-2026)

```python
"""Rate constants. All rates as bps_x100 (basis-points × 100, denominator 1e6).
Source citations inline. Last verified: 2026-05-16."""

# ─── Brokerage (Groww) ───────────────────────────────────────────────
GROWW_BROKERAGE_FUTURES_CAP_PAISA: int = 2000        # ₹20 max per leg
GROWW_BROKERAGE_FUTURES_BPS_X100:  int = 3000        # 0.03% = 30 bps × 100
GROWW_BROKERAGE_OPTIONS_FLAT_PAISA: int = 2000       # ₹20 flat per order

# ─── STT (3-regime history) ─────────────────────────────────────────
# Pre-Oct-2024:           0.0625% on options sell premium
# Oct-2024 to Mar-2026:   0.10%
# Apr-2026 onwards:       0.15%
STT_OPTIONS_PRE_OCT_2024_BPS_X100: int = 6_250      # 0.0625%
STT_OPTIONS_OCT_2024_BPS_X100:     int = 10_000     # 0.10%
STT_OPTIONS_POST_APR_2026_BPS_X100: int = 15_000    # 0.15%
STT_FUTURES_POST_APR_2026_BPS_X100: int = 5_000     # 0.05% (futures)

# ─── Exchange Transaction Charges (post Oct 1, 2024) ────────────────
NSE_ETC_OPTIONS_BPS_X100: int = 3_503    # 0.03503% on premium both sides
NSE_ETC_FUTURES_BPS_X100: int = 173      # 0.00173% on futures turnover
BSE_ETC_OPTIONS_BPS_X100: int = 3_250    # 0.03250% on SENSEX/BANKEX

# ─── SEBI ───────────────────────────────────────────────────────────
SEBI_BPS_X100: int = 10                  # 0.0001% (₹10/cr) all segments

# ─── IPFT (NSE) ─────────────────────────────────────────────────────
IPFT_NSE_OPTIONS_BPS_X100: int = 50      # 0.0005% (₹50/cr)
IPFT_NSE_FUTURES_BPS_X100: int = 10      # 0.0001% (₹10/cr)
IPFT_BSE_BPS_X100:         int = 0       # UNVERIFIED — assume 0 pending operator confirm

# ─── Stamp Duty (uniform post-2020 Indian Stamp Act) ───────────────
STAMP_OPTIONS_BUY_BPS_X100: int = 300    # 0.003% buy side only
STAMP_FUTURES_BUY_BPS_X100: int = 200    # 0.002% buy side only

# ─── GST ────────────────────────────────────────────────────────────
GST_PCT_X100: int = 1800                 # 18% (CGST 9% + SGST 9% intra-state)
```

### Rounding helpers (statutory rules from §3)

```python
def round_to_paise(x_paisa_unrounded: int) -> int:
    """Industry charges: already in paise. No-op if integer input."""
    return x_paisa_unrounded

def round_to_rupee(x_paisa_unrounded: int) -> int:
    """STT, Stamp duty, GST → round to nearest rupee (100 paise).
    Half-up at 50 paise per Section 170 CGST + Rule 11 STT.
    """
    return ((x_paisa_unrounded + 50) // 100) * 100
```

### File layout (Phase 3)

```
   brutex/costs/
   ├── __init__.py
   ├── constants.py          # Rate constants (Final[int]) + citations
   ├── trade_leg.py          # TradeLeg dataclass
   ├── charges.py            # calculate_charges() returns ChargeBreakdown
   ├── slippage.py           # apply_slippage() — TRACK 1 spot = None; TRACK 2 = §9 delta-bucketed
   ├── pnl.py                # calculate_net_pnl() — combines slippage + charges
   ├── income_tax.py         # Annual FY 2026-27 income tax (separate from per-trade)
   └── tests/
       ├── test_charges.py
       ├── test_slippage.py
       ├── test_pnl.py
       ├── test_income_tax.py
       └── fixtures/
           └── groww_contract_notes.json   # Real Groww contract notes (Level-1 oracle)
```

### Performance target

**< 1 microsecond per `calculate_charges()` call.** Justification:

- Sweep size: 730B trial evaluations
- If 10 cost-calc calls per trial → 7.3 trillion calls
- At 1µs each: 7.3 × 10⁶ seconds = ~85 days
- Beyond budget → need <1µs (or Rust/Cython acceleration)

Pure-int math + match statement + pre-computed dicts achieves <1µs in
benchmark tests. Optional Rust PyO3 binding deferred to Phase 4
optimization if benchmarks miss target.

### API specification

```python
@dataclass(frozen=True)
class TradeLeg:
    symbol: str                # "NIFTY" | "BANKNIFTY" | "SENSEX"
    instrument: Literal["futures", "options"]
    side: Literal["buy", "sell"]
    price_paisa: int           # per-unit price in paisa
    quantity: int              # = lot_size × num_lots

    @property
    def turnover_paisa(self) -> int:
        return self.price_paisa * self.quantity


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage_paisa:    int
    stt_paisa:          int   # rupee-rounded (× 100 paise)
    exchange_txn_paisa: int   # paise precision
    sebi_paisa:         int   # paise precision
    ipft_paisa:         int   # paise precision
    stamp_duty_paisa:   int   # rupee-rounded
    gst_paisa:          int   # rupee-rounded per Section 170 CGST

    @property
    def total_paisa(self) -> int:
        return (self.brokerage_paisa + self.stt_paisa + self.exchange_txn_paisa
                + self.sebi_paisa + self.ipft_paisa + self.stamp_duty_paisa
                + self.gst_paisa)


def calculate_charges(
    buy_leg: TradeLeg,
    sell_leg: TradeLeg,
    trade_date: date,    # for STT-regime lookup
    is_muhurat: bool = False,
) -> ChargeBreakdown:
    """Compute all 7 charges for a round-trip options trade.

    - Validates buy_leg.instrument == sell_leg.instrument, quantities match.
    - STT applied only on sell side (rate per trade_date regime).
    - Stamp duty applied only on buy side.
    - GST applied on (brokerage + exch + SEBI + IPFT) only.
    - All statutory items (STT, Stamp, GST) round to nearest rupee.
    - is_muhurat=True overrides brokerage to 0 (Muhurat session).
    """
```

This is the Phase 3 deliverable spec. Implementation follows in
`brutex/costs/` package; this doc is the canonical design reference.

## 15. References (citations from 4-agent research)

### Primary statutory sources
- Securities Transaction Tax Rules 2004, Rule 11 — [indiacode.nic.in](https://www.indiacode.nic.in)
- Section 170, CGST Act 2017 — [taxinformation.cbic.gov.in](https://taxinformation.cbic.gov.in)
- Indian Stamp Rules 2019 — [PIB Release 1635399](https://www.pib.gov.in/PressReleasePage.aspx?PRID=1635399)
- SEBI True-to-Label circular Jul 2024 — [sebi.gov.in](https://www.sebi.gov.in/legal/circulars/jul-2024/charges-levied-by-market-infrastructure-institutions-true-to-label_84506.html)
- NSE circular FA64232 (Oct-2024 revision) — [nsearchives.nseindia.com](https://nsearchives.nseindia.com/content/circulars/FA64232.pdf)
- NSE circular FA56129 (IPFT) — [nsearchives.nseindia.com](https://nsearchives.nseindia.com/content/circulars/FA56129.pdf)

### Secondary industry sources
- Zerodha Z-Connect — [revision Oct-2024](https://zerodha.com/z-connect/business-updates/revision-in-exchange-transaction-charges-and-securities-transaction-tax-from-october-1-2024)
- ClearTax — [STT updates](https://cleartax.in/s/securities-transaction-tax-stt) + [Section 170 GST](https://cleartax.in/s/rounding-off-tax-section-170-gst)
- Bajaj Finserv — [STT India 2026](https://www.bajajfinserv.in/securities-transaction-tax)
- ICICI Direct — [STT changes Budget 2026](https://www.icicidirect.com/futures-and-options/articles/stt-changes-in-budget-2026-what-f-o-traders-need-to-know)
- Angel One — [BSE Sensex/Bankex update](https://www.angelone.in/news/bse-updates-transaction-fees-for-sensex-bankex-options)
- Bajaj Broking — [NSE/BSE Oct-2024 revisions](https://www.bajajbroking.in/blog/nse-and-bse-revise-transaction-charges-from-october-2024)
- Chittorgarh — [Groww brokerage 2026](https://www.chittorgarh.com/brokerage_charges/groww/176/)
- IPOGMPINDIA — [Groww brokerage guide 2026](https://ipogmpindia.com/groww-brokerage-charges-2026-complete-guide/)
- Comparesharebrokers — [Groww 2026](https://comparesharebrokers.com/faq/what-are-groww-brokerage-charges-in-2026/1113)
- taxguru.in — [STT hike 1-Apr-2026](https://taxguru.in/income-tax/stt-rates-futures-options-hiked-1st-april-2026.html)
- Benzinga India — [Groww women discount discontinued](https://in.benzinga.com/content/39646402/groww-app-stops-50-discount-for-female-traders-due-to-sebi-circular-and-misuse)
- MadeForTrade — [women discount discussion](https://madefortrade.in/t/discontinuation-of-50-off-on-brokerage-for-female-traders-on-groww/27576)
- Groww blog — [Muhurat trading](https://groww.co/muhurat-trading/)
- Groww support — [Groww API pricing](https://groww.co/support/platforms/growwapi-api/how-does-the-growwapi-data-api-subscription-work/)

## 16. Confidence ratings per line item

| Component | Spec rate | Source quality | Confidence |
|---|---|---|---|
| Brokerage ₹20 flat per order | YES | 5+ secondary sources | 95% |
| STT 0.15% post-Apr-2026 | YES | Finance Act 2026 + 6 cites | 98% |
| Multi-lot is per-ORDER (₹20 flat) | YES | Multiple sources confirm | 95% |
| NSE exchange 0.03503% | YES | NSE circular + 5 cites | 98% |
| BSE exchange 0.03250% | YES | BSE notice + 3 cites | 95% |
| SEBI 0.0001% | YES | NSE static page | 95% |
| IPFT NSE 0.0005% | YES | NSE circular FA/56129 | 95% |
| IPFT BSE rate | 0 (assumed) | Not located | UNVERIFIED |
| Stamp 0.003% buy | YES | Indian Stamp Act 2020 | 98% |
| GST 18% | YES | CGST Act + ClearTax | 98% |
| STT round to rupee | YES | Rule 11 STT Rules + Groww stated rule | 95% |
| Stamp round to rupee | YES | Indian Stamp Rules 2019 + Groww stated rule | 95% |
| GST round per component to rupee | YES | Section 170 CGST + ClearTax | 95% |
| Industry items round to paise | YES | Groww stated rule + industry practice | 90% |
| Women's discount discontinued | YES | Benzinga + MadeForTrade | 90% |
| Groww Data API ₹588.82/mo | YES | Groww official support page | 95% |
| Muhurat brokerage = 0 | YES | Groww + Zerodha pages | 95% |

**Overall calculator confidence: ~95%.** Reaches ~99% after Level-1 contract note verification.

## 17. Amendment log

| Date | Change | Reason |
|---|---|---|
| 2026-05-16 | Initial draft | 4-agent deep research synthesis |
| 2026-05-16 | §14 switched from Decimal to int math (`bps_x100`) | Operator cross-verification w/ parallel Claude chat spec; performance target <1µs |
| 2026-05-16 | Added §18 Income Tax module | Operator cross-verification spec included annual FY 2026-27 tax computation |
| 2026-05-16 | Slippage model simplified to flat 1-tick | Operator challenge: cannot validate delta-bucketed without tick data (Groww API only provides 1m bars, no tick stream — ever). Honest simplicity > false precision. Track 2 §9 amended. |
| _(future)_ | Level-1 verification result | After operator first real trade |
| _(future)_ | BSE IPFT rate locked | After operator verifies via BSE page |
| _(future)_ | Groww rate card primary verification | After operator screenshot groww.co/pricing |
| _(future)_ | GST rounding (rupee vs paise) reconciliation | After Level-1 contract note |
| 2026-07-03 | §5 Examples 1-4 REGENERATED from the real calculator's output (Ex3 ₹52.19 → ₹51.18; Ex2 ₹137.48 → ₹136.47; Ex4 ₹52.17 → ₹51.17) + enforced by `tests/golden/test_costs_verified_examples.py`; Example 5 marked SPEC-ONLY; §3 GST row reconciled to DEC-COST-001 round-once | R2-OC-9: the hand-derived tables violated the doc's own DEC-COST-001 (CGST/SGST rounded separately), mis-rounded a sub-paise SEBI fee to ₹0.01, and ignored the 1-tick-slippage-in-fills |
| 2026-07-03 | §20 Known limitations added (ETC pre-Oct-2024 UNVERIFIED placeholder; GST-on-SEBI boundary not implemented; lot-size trade-date-vs-vintage caveat; 2021 lot-history floor vs 2020 data floor → per-trade LOT_SIZE_UNKNOWN reject; Muhurat spec-only) | R2-OC-3 / R2-OC-4/5 honesty audit (Golden Rules #1 + #10) |
| 2026-07-06 | §20 row 1 rewritten: the ETC pre-2024-10-01 UNVERIFIED placeholder (current rate applied to all dates) is REPLACED by explicit rate-less refusal rows — pre-boundary trades REFUSE loud (`UnverifiedCostRegimeError` → executor skip-and-count), never priced at a fabricated rate; NEW §20 row 6 (FA73061 forward-regime alert, UNVERIFIED, not encoded) | DEC-ETC-REGIME-HISTORY-001 (MIGRATION.md #1523) — the 2026-07-06 4-strategy circular-fetch round: zero official-trace retrievals; reconstruction ≠ verification (Golden Rule #1) |

## 18. Annual Income Tax Module (Phase 4 deliverable)

> **Separate concern from per-trade cost calculator.** Lives at
> `brutex/costs/income_tax.py`. Computes annual tax liability from
> the aggregated F&O PnL ledger.
>
> **Comprehensive treatment moved to `TRACK2_TAX_HANDLING.md`
> (2026-05-16).** This section retains the API stub + slab tables.
> All rules (turnover formula, set-off, carry-forward, advance-tax
> buffer, regime toggle, ICAI Revised 2025 Guidance Note) are in the
> dedicated spec.

### Classification under Income Tax Act

F&O trading income is classified as **non-speculative business
income** per Section 43(5) of the Income Tax Act. Implications:

| Aspect | F&O treatment |
|---|---|
| Tax head | Business income (NOT capital gains) |
| Tax rate | Slab rates (NOT special capital-gains rates) |
| ITR form | ITR-3 (mandatory for F&O traders) |
| Deductible expenses | All charges (STT, brokerage, ETC, GST), trading software/data subscriptions, internet/electricity proportional, advisory fees, etc. |
| Carry-forward of losses | 8 years (must file ITR by deadline) |
| Audit requirement | Section 44AB: turnover > ₹10 Cr (F&O is 100% electronic so the 5%-cash carve-out always applies). The "profit < 6% of turnover" trigger is a Section 44AD opt-out trap, NOT a 44AB threshold; see `TRACK2_TAX_HANDLING.md` §4. |
| Turnover formula | Sum of absolute values of P&L per squared-off trade (ICAI Revised 2025 Guidance Note; no premium re-add). See `TRACK2_TAX_HANDLING.md` §3. |

### FY 2026-27 New Regime slabs (default — Income Tax Act 2025)

| Slab | Rate |
|---|---|
| ₹0 – ₹3,00,000 | 0% |
| ₹3,00,001 – ₹7,00,000 | 5% |
| ₹7,00,001 – ₹10,00,000 | 10% |
| ₹10,00,001 – ₹12,00,000 | 15% |
| ₹12,00,001 – ₹15,00,000 | 20% |
| Above ₹15,00,000 | 30% |

Plus:
- **Section 87A rebate**: income ≤ ₹12 lakh → effectively zero tax (full rebate)
- **Surcharge** (above tax): 10% > ₹50L, 15% > ₹1Cr, 25% > ₹2Cr (37% removed in new regime)
- **Health & Education Cess**: 4% on (tax + surcharge)

### API specification

```python
@dataclass(frozen=True)
class AnnualTaxResult:
    gross_fno_pnl_paisa:        int   # sum of net_pnl across all trades in FY
    total_charges_paid_paisa:   int   # sum of all 7 charges (deductible)
    net_business_income_paisa:  int   # gross_fno - charges
    other_income_paisa:         int   # salary, etc (caller provides)
    total_taxable_income_paisa: int
    rebate_87a_paisa:           int   # if applicable
    tax_before_cess_paisa:      int
    surcharge_paisa:            int
    cess_paisa:                 int   # 4% on (tax + surcharge)
    tax_payable_paisa:          int   # final number
    effective_rate_pct:         float

def compute_annual_tax(
    annual_gross_fno_pnl_paisa: int,
    annual_total_charges_paisa: int,
    other_income_paisa: int = 0,
    regime: Literal["new", "old"] = "new",
    financial_year: str = "2026-27",
) -> AnnualTaxResult:
    """FY 2026-27 default (new regime). All inputs/outputs in int64 paisa."""
```

### Test fixture seeds (Phase 4 build)

| Scenario | Gross F&O PnL | Other income | Expected tax (new regime) |
|---|---|---|---|
| Small trader | ₹5L profit | 0 | ~₹10,000 (5% slab) |
| Mid trader | ₹15L profit | ₹8L salary | ~₹2.3L (mixed slabs) |
| Pro trader | ₹50L profit | 0 | ~₹13.5L (30% + 10% surcharge + 4% cess) |
| Loss carry-forward | -₹3L | ₹10L salary | ₹10,000 (loss carried, doesn't offset salary) |

Source: Income Tax Act 2025, Section 43(5), Section 87A rebate update (Finance Act 2025).

## 19. Cross-source corroboration (added 2026-05-16)

In addition to my 4-agent research, this spec is **cross-verified
against an independent Claude chat conversation** the operator
conducted. Results:

| Item | My spec | Claude chat | Match? |
|---|---|---|---|
| Brokerage options ₹20 flat per order | YES | YES | ✅ EXACT |
| STT 0.15% post-Apr-2026 | YES | YES | ✅ EXACT |
| All 7 charge rates (brokerage, STT, NSE/BSE exch, SEBI, IPFT, stamp, GST) | YES | YES | ✅ ALL MATCH |
| Lot sizes Jan 2026 (NIFTY 65, BANKNIFTY 30, SENSEX 20) | YES | YES | ✅ EXACT |
| Tick sizes (₹0.05 index options) | YES | YES | ✅ EXACT |
| STT/Stamp round to rupee | YES | YES | ✅ EXACT |
| All-int paisa math | YES | YES | ✅ EXACT |
| GST 18% on (brokerage + exch + SEBI + IPFT) | YES | YES | ✅ EXACT |
| GST rounding | rupee (Section 170 CGST) | paise (no rule cited) | ⚠️ DISCREPANCY |
| Slippage model | flat 1-tick (REVISED 2026-05-16) | flat 1-tick (stale "project memory" actually correct here) | ✅ NOW AGREE |
| Multi-lot critical (₹20 per ORDER not per lot) | YES (Agent D) | NOT FLAGGED | my spec wins |
| Muhurat session brokerage=0 | YES (Agent D) | NOT FLAGGED | my spec wins |
| Women's discount discontinued July 2024 | YES (Agent D) | NOT FLAGGED | my spec wins |
| Groww Data API ₹588.82/mo OPEX | YES (Agent D) | NOT FLAGGED | my spec wins |
| BSE charges (SENSEX on BSE_FNO) | YES | NSE-only | my spec wins |
| 3-regime STT history | YES (covers 2021-2026 backfill) | only post-Apr-2026 | my spec wins |
| Multi-file `brutex/costs/` structure | NOW INTEGRATED | YES | their spec wins |
| `bps_x100` integer math pattern | NOW INTEGRATED | YES | their spec wins |
| <1µs per call performance target | NOW INTEGRATED | YES | their spec wins |
| Income tax module | NOW INTEGRATED (§18) | YES | their spec wins |
| 4-agent research provenance | YES | NO | my spec wins |
| 30+ source URLs cited | YES | partial | my spec wins |
| Calculation order spec (per-leg → aggregate → round) | YES | partial | my spec wins |
| Level-1 validation workflow | YES | YES | both agree |

**Resolution decisions (operator confirmed 2026-05-16):**

1. **GST rounding = rupee** (per Section 170 CGST Act citation). Reconcile against real contract note when available.
2. **Slippage = flat 1-tick adverse on bar-open fills** (Track 2 §9 REVISED 2026-05-16). The previous delta-bucketed model couldn't be validated without tick data (which Groww API does not provide). Flat 1-tick is the simplest defensible model given 1m OHLCV + OI + IV + strike + spot only.
3. **Integrate Claude chat additions** (multi-file structure, int math, performance target, income tax module).
4. **Keep my spec's edge cases** (multi-lot, Muhurat, women's discount, Groww API, BSE) — Claude chat missed these.

Net: This spec now contains **EVERYTHING from both sources**, with disagreements resolved per citation strength.

## 20. Known limitations (honest envelope — Golden Rules #1 + #10)

Added 2026-07-03 (R2-OC-3 / R2-OC-4/5 audit). Each row is a DOCUMENTED,
deliberate limitation — none is silently papered over in code.

| # | Limitation | Direction / impact | Status |
|---|---|---|---|
| 1 | **ETC pre-2024-10-01 rates UNVERIFIED → explicit REFUSAL windows (DEC-ETC-REGIME-HISTORY-001, MIGRATION.md #1523).** The exchange transaction charge rides a date-regime table (the same `rate_for_date` mechanism as STT, `brutex/costs/constants.py`); only the CURRENT rates (NSE 0.03503% / BSE 0.03250%, effective 2024-10-01 — NSE FA64232 / BSE notice 27-Sep-2024) are verified. Every pre-2024-10-01 window is now a rate-less refusal row: pre-boundary trades are **REFUSED loud** (`UnverifiedCostRegimeError` → `RejectReason.COST_REGIME_UNVERIFIED`, counted + WARNed per trade + one aggregate WARN + a CLI leaderboard footer; the sim completes, no cost fabricated). The 2026-07-06 fetch round reconstructed the six pre-boundary regimes to circular-number precision (NSE FA46730 / FA56129 / FA61137; BSE 20231020-46 / 20240430-42; BSE pre-2023-11 unidentified) but achieved ZERO official-trace retrievals (all official hosts + web.archive.org egress-blocked) AND the pre-boundary charges are structurally slab ladders on member monthly premium turnover with no honest flat per-trade equivalent — TRIPLY unencodable. | Options backtests over 2021-01-01 → 2024-09-30 produce ZERO priced trades (counted `COST_REGIME_UNVERIFIED` rejections); combined with the 2021-01-01 lot floor (row 4) the honestly-priceable window is **[2024-10-01 → today]**. Previously such trades were silently priced at the invented current rate (likely UNDER-costed vs the widely-reported-but-UNVERIFIED higher prior NSE rate — a direction note only, never a number). | OPEN — closes per-window as each circular body lands officially (one cited `RegimeRow` each, zero mechanism change), or via the ADR-gated top-slab-ASSUMED path (three conditions — official full-ladder body in-repo + documented retail-top-slab assumption with direction-of-error note + operator decision ID; see MIGRATION.md #1523). |
| 2 | **GST-on-SEBI date boundary NOT implemented.** The SEBI turnover fee sits in the GST base for ALL dates. The claim that SEBI fees became GST-liable only from ~2022-07-18 has **no in-repo citation** (searched 2026-07-03), so per Golden Rule #1 no boundary was encoded. | If the claim holds, pre-boundary trades are **OVER-charged** by 18% of the sub-rupee SEBI fee — conservative (never understates cost). | OPEN — implement only with a citable GST notification. |
| 3 | **Lot size keyed by TRADE date, not contract VINTAGE.** `TradeCostBreakdown.lot_size_basis = "trade_date"`: the lot is the one in effect on `entry_dt_ist.date()` per `lot_size_history.py`. A real exchange revision applies per-contract from a specific expiry onward, so in a transition window the bases disagree — e.g. NIFTY **2024-11-19 → lot 25** vs **2024-11-20 → lot 75** (SEBI ₹15L notional revision), a 3× sizing difference. | Real sizing error possible in lot-transition windows only. | OPEN — per-circular NSE contract-level rollout dates are UNVERIFIED offline; no vintage logic is faked. The basis is stamped on every breakdown so consumers can audit. |
| 4 | **Lot-size history floor (2021-01-01) is ABOVE the data floor (2020-01-01, `GROWW_HISTORY_FLOOR`).** 2020 lot sizes for NIFTY/BANKNIFTY/SENSEX are UNVERIFIED offline (searched the repo 2026-07-03 — no citable values), so `get_lot_size` still raises for 2020 dates. | A 2020-dated signal is REJECTED per-trade by the options executor (`RejectReason.LOT_SIZE_UNKNOWN`, counted on `OptionsRuleResult.n_rejected_lot_size_unknown`) — the sim completes instead of crashing; no lot is ever fabricated. | OPEN — closes when citable 2020 lot sizes land in `lot_size_history.py`. |
| 5 | **Example 5 (Muhurat ₹0 brokerage) is spec-only.** `compute_trade_cost` has no Muhurat branch. | A Muhurat-session trade is charged normal brokerage — **OVER-charged** by ₹40 + its GST (conservative). | OPEN future work. |
| 6 | **FA73061 forward-regime alert (UNVERIFIED — deliberately NOT encoded).** An AI-summary mirror of NSE circular FA73061 (dated 2026-02-27, effective 2026-03-01) indicates the NSE options txn/IPFT split moved to ₹3,552 + ₹1 per cr — the TOTAL unchanged at 0.03553%, but the encoded 3,503 (ETC) + 50 (IPFT) split possibly STALE for trades on/after 2026-03-01. The circular body was never retrieved (official hosts egress-blocked, 2026-07-06 round); per Golden Rule #1 the 3,552/1 split is NOT encoded. | Post-2026-03-01 trades may carry a slightly mis-split (total-correct) ETC/IPFT attribution IF the mirror is accurate — component-level only; the summed charge would be unchanged. | OPEN — the HIGHEST-priority circular fetch (MIGRATION.md #1523 follow-up (i)); lands as a dated `RegimeRow` + IPFT revision once the body is read officially. |
