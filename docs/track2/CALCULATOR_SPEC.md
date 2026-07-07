# Track 2 — Per-Trade Cost Calculator Specification

**Status:** LOCKED 2026-05-16 (consolidation of `TRACK2_OPTIONS_SPEC.md`
§8 + §9 + `COSTS_VERIFIED.md` §3-§14 + §18). No code yet — this is the
build-target spec.

**Scope:** Per-trade cost computation for one round-trip options trade
(entry + exit) on a single instrument (NIFTY 50 / BANKNIFTY / SENSEX
options) under a single broker (Groww). The output of this calculator
feeds into:
- Per-trade `net_pnl` (gross PnL − slippage − all charges)
- Aggregator for annual income tax (see `COSTS_VERIFIED.md §18`)

**Out of scope for THIS spec (and why):**
- Spot index PnL (Track 1, signal-only, NO costs applied)
- Margin / peak-margin computation (we're backtesting outcomes, not capital)
- Dividend / corporate action handling (cash-settled options on indexes)
- Mark-to-market intraday rebalancing (Track 2 holds positions until exit signal)
- Carry forward of losses (handled in annual tax module, not per-trade)
- Multi-broker A/B comparison at scale (only Groww locked; Zerodha mentioned for comparability sanity-check)

**Single source of truth for verified values:** `COSTS_VERIFIED.md`.
This document defines the *algorithm*. The *numeric constants* live in
`COSTS_VERIFIED.md` and (eventually) `brutex/costs/groww_constants.py`.

---

## 1. Calculator inputs (per round-trip)

| Field | Type | Range | Notes |
|---|---|---|---|
| `entry_dt_ist` | `datetime` | 2021-01-01 09:15 → today 15:30 | IST. Determines STT-era, lot-size era. |
| `exit_dt_ist` | `datetime` | ≥ entry_dt_ist | Same day or later. Track 2 forced exit 15:20 IST. |
| `instrument` | `Literal["NIFTY", "BANKNIFTY", "SENSEX"]` | enum | Determines exchange (NIFTY/BANKNIFTY=NSE, SENSEX=BSE) and lot-size table. |
| `option_type` | `Literal["CE", "PE"]` | enum | NOT used by calculator (call vs put → no charge difference). Stored for audit only. |
| `strike` | `int64` (paisa) | ≥ 1 | NOT used by calculator. Stored for audit only. |
| `expiry_date` | `date` | ≥ entry_dt_ist.date() | NOT used by calculator. Stored for audit only. |
| `entry_bar_open_inr` | `int64` (paisa) | ≥ 5 (₹0.05 minimum) | Bar OPEN at entry. Slippage applied on top. |
| `exit_bar_open_inr` | `int64` (paisa) | ≥ 5 | Bar OPEN at exit. Slippage applied on top. |
| `side` | `Literal["long_call", "long_put", "short_call", "short_put"]` | enum | Determines which leg is "buy" (pays STT-no, pays stamp), which is "sell" (pays STT-yes, no stamp). |
| `n_lots` | `int` | ≥ 1 | Number of lots. Each lot's share-count looked up from `lot_size_history[instrument, entry_dt_ist.date()]`. |
| `broker` | `Literal["groww", "zerodha"]` | enum | Brokerage differs. Default `"groww"`. |
| `gst_split` | `Literal["intra_state", "inter_state"]` | enum | `"intra_state"` → CGST 9% + SGST 9% (each rounded separately). `"inter_state"` → IGST 18% (single rounding). Set once per trader account, not per trade. Default `"intra_state"` to match `TRACK2_OPTIONS_SPEC.md §8.2` worked example. |
| `held_to_expiry_exercise` | `bool` | default `False` | **DEPRECATED 2026-05-16** — superseded by `trade_outcome` enum below. Kept for backward-compat reading old fixtures; new fixtures use `trade_outcome`. |
| `intrinsic_value_paisa_at_expiry` | `int64` | ≥ 0 | Required only when `trade_outcome` ∈ {`EXPIRY_EXERCISE`, `EXPIRY_ASSIGN`}. Intrinsic value × n_shares. STT computed on this, not on premium. |
| `trade_outcome` | `Literal["NORMAL_CLOSE", "EXPIRY_EXERCISE", "EXPIRY_ASSIGN", "EXPIRY_WORTHLESS"]` | enum, default `"NORMAL_CLOSE"` | **ADDED 2026-05-16** per claude-chat prompt verification. State machine for how the trade resolved. Track 2 ALWAYS uses `"NORMAL_CLOSE"` (15:20 forced exit). The other 3 are reserved for forward-compatible held-to-expiry tracks. See §4.2 for branch logic. |

**No other inputs are needed.** Charges depend only on these.

---

## 2. Constants table (locked, source-cited)

All from `COSTS_VERIFIED.md`. Reproduced here for spec self-containment;
single source of truth remains `COSTS_VERIFIED.md` and the Python
constants module.

| Symbol | Value | Source |
|---|---|---|
| `TICK_PAISA` | `5` (= ₹0.05) | COSTS_VERIFIED §11 |
| `GROWW_BROKERAGE_PAISA` | `2000` (= ₹20 per executed order) | COSTS_VERIFIED §8.1 |
| `ZERODHA_BROKERAGE_PAISA` | `2000` (= ₹20 per executed order, also flat for F&O) | COSTS_VERIFIED §8.2 |
| `STT_RATE_PRE_OCT2024` | `0.000625` (= 0.0625% of sell premium, pre 2024-10-01) | `brutex/costs/constants.py` `STT_OPTIONS_PRE_OCT_2024_BPS_X100`; DEC-COST-003 (MIGRATION.md #128). Row added 2026-07-06 (DEC-TRACK2-COSTS-SPEC-RECONCILE-001) — the code has carried this third regime since the rate lock; the table previously showed only two. |
| `STT_RATE_PRE_APR2026` | `0.0010` (= 0.10% of sell premium, 2024-10-01 → 2026-03-31) | COSTS_VERIFIED §3 |
| `STT_RATE_POST_APR2026` | `0.0015` (= 0.15% of sell premium, from 2026-04-01) | COSTS_VERIFIED §3 (UNVERIFIED gazette text) |
| `STT_HIKE_EFFECTIVE_DATE` | `date(2026, 4, 1)` | Budget 2026 |
| `NSE_FNO_RATE` | `0.00035030` (= 0.03503%, both sides) | COSTS_VERIFIED §4 |
| `BSE_FNO_RATE` | `0.00032500` (= 0.03250%, both sides) | COSTS_VERIFIED §4 (UNVERIFIED for Bankex) |
| `SEBI_RATE` | `0.0000010` (= ₹10/cr = 0.0001%, both sides) | COSTS_VERIFIED §5 |
| `IPFT_NSE_RATE` | `0.0000050` (= ₹50/cr = 0.0005%, both sides) | COSTS_VERIFIED §6 |
| `IPFT_BSE_RATE` | `0.0000050` (= ₹50/cr, both sides — BSE applies same) | COSTS_VERIFIED §6 |
| `STAMP_DUTY_BUY_RATE` | `0.00003` (= 0.003%, buy leg only) | COSTS_VERIFIED §7 |
| `GST_RATE` | `0.18` (CGST 9% + SGST 9% OR IGST 18%) | COSTS_VERIFIED §9 |
| `CGST_RATE` | `0.09` | COSTS_VERIFIED §9 |
| `SGST_RATE` | `0.09` | COSTS_VERIFIED §9 |
| `IGST_RATE` | `0.18` | COSTS_VERIFIED §9 |

**Exchange routing:**
- `NIFTY` options → NSE → `NSE_FNO_RATE`, `IPFT_NSE_RATE`
- `BANKNIFTY` options → NSE → `NSE_FNO_RATE`, `IPFT_NSE_RATE`
- `SENSEX` options → BSE → `BSE_FNO_RATE`, `IPFT_BSE_RATE`

---

## 3. Lot size lookup (per-date)

Per `TRACK2_OPTIONS_SPEC.md §8.3`, lot sizes changed 7 times in
2021-2026. Build `lot_size_history.csv` keyed on
`(instrument, effective_from_date)`:

```
NIFTY,     2021-01-01, 75
NIFTY,     2021-07-01, 50      # NSE July 2021 expiry change
NIFTY,     2023-06-30, 50      # unchanged for NIFTY
NIFTY,     2024-04-26, 25      # NSE FAOP circular
NIFTY,     2024-11-20, 75      # SEBI ₹15L notional floor
NIFTY,     2025-07-01, 75      # transitional, unchanged
NIFTY,     2025-12-30, 75      # NSE FAOP70616 — actually changed BANKNIFTY this date
NIFTY,     2026-01-01, 65      # Jan 2026 circular (CURRENT)

BANKNIFTY, 2021-01-01, 25
BANKNIFTY, 2023-06-30, 15
BANKNIFTY, 2024-11-20, 30
BANKNIFTY, 2025-07-01, 35
BANKNIFTY, 2025-12-30, 30
BANKNIFTY, 2026-01-01, 30      # CURRENT

SENSEX,    2021-01-01, 10
SENSEX,    2024-11-20, 20
SENSEX,    2026-01-01, 20      # CURRENT
```

Lookup rule: `n_shares_per_lot = max(row.lot_size where row.effective_from_date ≤ entry_dt_ist.date())`.
Same lot size used at exit (lot doesn't change mid-trade).

---

## 4. Per-trade algorithm (deterministic, O(1))

All math in **`int64` paisa** to avoid float drift. Convert to rupees
only for display.

```
INPUT:  entry_dt, exit_dt, instrument, side, entry_open_paisa, exit_open_paisa,
        n_lots, broker, gst_split

# ── Step 1: apply slippage (flat 1-tick adverse, per Track 2 §9) ───────────
buy_fill_paisa  = (entry_open_paisa + TICK_PAISA) if side starts with "long"
                  else (exit_open_paisa + TICK_PAISA)
sell_fill_paisa = (entry_open_paisa - TICK_PAISA) if side starts with "short"
                  else (exit_open_paisa - TICK_PAISA)
# Long side: BUY at entry+1tick, SELL at exit-1tick
# Short side: SELL entry at entry-1tick, BUY cover at exit+1tick — adverse both
# legs (buy higher, sell lower; PnL inverts); §8 is authoritative
# (DEC-TRACK2-COSTS-SPEC-RECONCILE-001 — resolves the #963 MINOR-4 contradiction)

# ── Step 2: lookup lot size ──────────────────────────────────────────────
n_shares_per_lot = lot_size_lookup(instrument, entry_dt.date())
n_shares         = n_lots * n_shares_per_lot

# ── Step 3: notionals (in paisa) ─────────────────────────────────────────
buy_notional_paisa  = buy_fill_paisa  * n_shares
sell_notional_paisa = sell_fill_paisa * n_shares

# ── Step 4: brokerage (₹20 flat per executed order, both legs) ───────────
brokerage_paisa = (GROWW_BROKERAGE_PAISA if broker == "groww"
                   else ZERODHA_BROKERAGE_PAISA) * 2
# (each leg is one order; flat regardless of premium or lots)

# ── Step 5: STT (sell side only, on premium) ─────────────────────────────
stt_rate = STT_RATE_POST_APR2026 if entry_dt.date() >= STT_HIKE_EFFECTIVE_DATE
           else STT_RATE_PRE_APR2026
stt_paisa_raw = sell_notional_paisa * stt_rate
stt_paisa     = ROUND_TO_NEAREST_RUPEE(stt_paisa_raw)
# Statutory: STT rounded to nearest rupee per Rule 11 STT Rules

# ── Step 6: exchange transaction charge (both sides, on premium) ──────────
exch_rate = NSE_FNO_RATE if instrument in {"NIFTY", "BANKNIFTY"}
            else BSE_FNO_RATE
exch_buy_paisa  = ROUND_TO_NEAREST_PAISA(buy_notional_paisa  * exch_rate)
exch_sell_paisa = ROUND_TO_NEAREST_PAISA(sell_notional_paisa * exch_rate)
exch_paisa      = exch_buy_paisa + exch_sell_paisa
# Non-statutory: paise precision retained (commercial half-up rounding)

# ── Step 7: SEBI fee (both sides, on premium) ────────────────────────────
sebi_buy_paisa  = ROUND_TO_NEAREST_PAISA(buy_notional_paisa  * SEBI_RATE)
sebi_sell_paisa = ROUND_TO_NEAREST_PAISA(sell_notional_paisa * SEBI_RATE)
sebi_paisa      = sebi_buy_paisa + sebi_sell_paisa

# ── Step 8: IPFT (both sides, on premium) ────────────────────────────────
ipft_rate = IPFT_NSE_RATE if instrument in {"NIFTY", "BANKNIFTY"}
            else IPFT_BSE_RATE
ipft_buy_paisa  = ROUND_TO_NEAREST_PAISA(buy_notional_paisa  * ipft_rate)
ipft_sell_paisa = ROUND_TO_NEAREST_PAISA(sell_notional_paisa * ipft_rate)
ipft_paisa      = ipft_buy_paisa + ipft_sell_paisa

# ── Step 9: stamp duty (buy leg only) ────────────────────────────────────
stamp_paisa_raw = buy_notional_paisa * STAMP_DUTY_BUY_RATE
stamp_paisa     = ROUND_TO_NEAREST_RUPEE(stamp_paisa_raw)
# Statutory: stamp duty rounded to nearest rupee per Indian Stamp Rules 2019

# ── Step 10: GST (on brokerage + exchange + SEBI + IPFT only) ────────────
# NOT on STT, NOT on stamp duty (those are statutory levies, not services).

per_leg_brokerage = GROWW_BROKERAGE_PAISA  # ₹20 per leg
buy_gst_base_paisa  = per_leg_brokerage + exch_buy_paisa  + sebi_buy_paisa  + ipft_buy_paisa
sell_gst_base_paisa = per_leg_brokerage + exch_sell_paisa + sebi_sell_paisa + ipft_sell_paisa

if gst_split == "intra_state":
    # CGST 9% + SGST 9%, each rounded separately PER LEG
    cgst_buy_paisa  = ROUND_TO_NEAREST_RUPEE(buy_gst_base_paisa  * CGST_RATE)
    cgst_sell_paisa = ROUND_TO_NEAREST_RUPEE(sell_gst_base_paisa * CGST_RATE)
    sgst_buy_paisa  = ROUND_TO_NEAREST_RUPEE(buy_gst_base_paisa  * SGST_RATE)
    sgst_sell_paisa = ROUND_TO_NEAREST_RUPEE(sell_gst_base_paisa * SGST_RATE)
    gst_paisa = cgst_buy_paisa + cgst_sell_paisa + sgst_buy_paisa + sgst_sell_paisa
else:  # inter_state
    igst_buy_paisa  = ROUND_TO_NEAREST_RUPEE(buy_gst_base_paisa  * IGST_RATE)
    igst_sell_paisa = ROUND_TO_NEAREST_RUPEE(sell_gst_base_paisa * IGST_RATE)
    gst_paisa = igst_buy_paisa + igst_sell_paisa

# ── Step 11: total charges ───────────────────────────────────────────────
total_charges_paisa = brokerage_paisa + stt_paisa + exch_paisa + sebi_paisa
                    + ipft_paisa + stamp_paisa + gst_paisa

# ── Step 12: gross PnL (slippage already baked into fill prices) ─────────
gross_pnl_paisa = sell_notional_paisa - buy_notional_paisa
# (positive for profitable long; negative for losing long; short side inverts at construction)

# ── Step 13: net PnL ──────────────────────────────────────────────────────
net_pnl_paisa = gross_pnl_paisa - total_charges_paisa

# ── Output: full breakdown ────────────────────────────────────────────────
return TradeCostBreakdown(
    n_shares,
    buy_fill_paisa, sell_fill_paisa,
    buy_notional_paisa, sell_notional_paisa,
    brokerage_paisa,
    stt_paisa,
    exch_paisa, sebi_paisa, ipft_paisa,
    stamp_paisa, gst_paisa,
    total_charges_paisa,
    gross_pnl_paisa,
    net_pnl_paisa,
)
```

### 4.1 Rounding helper definitions

```
ROUND_TO_NEAREST_RUPEE(paisa_raw):
    # paisa_raw can be float-derived (rate × paisa); coerce to integer paisa first,
    # then round in rupees (i.e. find nearest multiple of 100 paisa).
    full_paisa = int(round(paisa_raw))            # banker's rounding to integer paisa
    rupees     = full_paisa // 100
    remainder  = full_paisa % 100
    if remainder >= 50:
        return (rupees + 1) * 100
    else:
        return rupees * 100
# NB: ties at exactly 50 paisa → round UP (commercial rounding, NOT banker's).
# Section 170 CGST / Rule 11 STT use commercial rounding.

ROUND_TO_NEAREST_PAISA(paisa_raw):
    # paisa_raw is a float-derived value (rate × notional_paisa).
    # Round to nearest integer paisa using commercial rounding (half-up).
    # NOT truncation — truncation would break the §8.2 worked example
    # (2.27695 INR truncates to ₹2.27 but spec requires ₹2.28).
    return int(paisa_raw + 0.5)  # commercial half-up for positive values
# Used for: exchange, SEBI, IPFT.
# Brokerage is already exact ₹20.00 = 2000 paisa, no rounding needed.

# IMPORTANT (rounding mode reconciliation):
# - ROUND_TO_NEAREST_RUPEE uses commercial half-up at the 0.50 boundary.
# - ROUND_TO_NEAREST_PAISA also uses commercial half-up at the 0.50 boundary.
# - Both match the §8.2 LOCKED worked example byte-for-byte.
# - This is NOT banker's rounding (half-to-even). Statutory texts
#   (Section 170 CGST, Rule 11 STT, Indian Stamp Rules 2019) do not
#   specify rounding mode, but commercial usage (and broker contract
#   notes from Zerodha + Angel One) consistently use half-up.
```

### 4.2 Trade outcome state machine (REVISED 2026-05-16)

The `trade_outcome` enum (§1) drives 4 distinct charge paths. Track 2
always sets `NORMAL_CLOSE`; the other 3 are forward-compat stubs.

> **IMPLEMENTATION STATUS (2026-07-06, DEC-TRACK2-COSTS-SPEC-RECONCILE-001):**
> Only `NORMAL_CLOSE` is implemented. `brutex/costs/calculator.py`'s Step-0
> guard raises `UnsupportedTradeOutcomeError` on any other outcome — that guard
> is a fail-loud REFUSAL, **NOT** an implementation of this state machine
> (MIGRATION.md #963 MAJOR-1: "CHANGE 1 is a fail-loud GUARD, NOT the §4.2
> state machine (still deferred)"). The three `EXPIRY_*` charge paths below are
> SPEC-ONLY forward-compat stubs per DEC-RISK-011 (MIGRATION.md #108) /
> DEC-EXEC-008 (#111), unreachable in Track 2 — the sole production caller
> (`brutex/options/executor.py`, DEC-OPTIONS-EXECUTOR-001 #568) hard-codes
> long-only sides + `trade_outcome="NORMAL_CLOSE"`. Implementing them requires
> a new DEC; nothing here authorizes it. Two drift observations inside the
> pseudo-code below (the `exit_dt` STT-era key; the flat "0.15% × premium"
> assign parenthetical) are REGISTERED as §16 items 18-19, deliberately NOT
> decided here.

```
                       ┌────────────────────────────────────────────────────┐
                       │             ENTRY (always charged)                 │
                       │   brokerage = ₹20 × n_orders_per_leg (§6b)         │
                       │   STT-on-entry: ONLY if side="short" (sell open)   │
                       │   exch/SEBI/IPFT on premium turnover               │
                       │   stamp duty: ONLY if side="long" (buy open)       │
                       │   GST: 18% on (brokerage + exch + SEBI + IPFT)     │
                       └────────────────────────────────────────────────────┘
                                            │
                       ┌────────────────────┴───────────────────────────────┐
                       ▼                                                    ▼
            ┌──────────────────────┐                            ┌─────────────────────┐
            │   NORMAL_CLOSE       │                            │   EXPIRY (3 sub-    │
            │  (Track 2 default)   │                            │       outcomes)     │
            ├──────────────────────┤                            ├─────────────────────┤
            │ Symmetric to ENTRY:  │                            │ See branches below  │
            │  brokerage ₹20 × n   │                            │                     │
            │  STT-on-exit: if     │                            │                     │
            │   side="long"        │                            │                     │
            │  exch/SEBI/IPFT on   │                            │                     │
            │   exit premium       │                            │                     │
            │  stamp: if short     │                            │                     │
            │  GST on services     │                            │                     │
            └──────────────────────┘                            └─────────────────────┘
                                                                          │
                ┌─────────────────────────────────────────────────────────┼────────────────────────────────┐
                ▼                                                         ▼                                ▼
   ┌────────────────────────────┐                       ┌────────────────────────────┐    ┌─────────────────────────────┐
   │   EXPIRY_EXERCISE          │                       │   EXPIRY_ASSIGN            │    │   EXPIRY_WORTHLESS          │
   │   (LONG ITM exercised)     │                       │   (SHORT ITM assigned)     │    │   (option expired OTM)      │
   ├────────────────────────────┤                       ├────────────────────────────┤    ├─────────────────────────────┤
   │ STT = 0.125%/0.15% on      │                       │ STT = 0 (already paid at   │    │ STT = 0                     │
   │   INTRINSIC value          │                       │   sell-open)               │    │   (long: lost premium;      │
   │ brokerage = ₹20 × 1 order  │                       │ brokerage = ₹20 × 1 order  │    │    short: kept premium —    │
   │   (Groww auto-settlement)   │                       │   (Groww auto-settlement)   │    │    STT already paid)        │
   │ exch/SEBI/IPFT on intrinsic│                       │ exch/SEBI/IPFT = 0         │    │ brokerage = ₹20 × 1 order   │
   │ stamp duty = 0 (no buy)    │                       │   (no trade event)         │    │   (Groww-specific: even on   │
   │ GST on brokerage only      │                       │ stamp duty = 0             │    │   worthless OTM — see §4.3) │
   │                            │                       │ GST on brokerage only      │    │ exch/SEBI/IPFT = 0          │
   └────────────────────────────┘                       └────────────────────────────┘    │ stamp duty = 0              │
                                                                                          │ GST on brokerage only       │
                                                                                          └─────────────────────────────┘
```

#### Pseudo-code per outcome

```
exit_charges_paisa = compute_exit_charges(trade_outcome, ...)

match trade_outcome:
  case NORMAL_CLOSE:
    # Track 2 standard path (steps 1-12 from §4 main algorithm)
    return compute_normal_exit(...)

  case EXPIRY_EXERCISE:
    # LONG ITM held to expiry, auto-exercised by exchange
    # OPEN (§16 item 18, NOT decided): this illustrative line keys the STT era
    # on exit_dt, but DEC-COST-002 (MIGRATION.md #127) locked the NORMAL_CLOSE
    # regime key to entry_dt_ist.date() (§4 Step 5 + §7). Whether an
    # expiry-settled event keys on exit_dt (the exercise occurs AT expiry) is
    # deferred to the DEC that implements this branch — illustrative, not
    # normative.
    stt_rate = 0.00125 if exit_dt < STT_HIKE_DATE else 0.0015
    stt = ROUND_TO_RUPEE(intrinsic_value_paisa * stt_rate)
    brokerage = GROWW_BROKERAGE_PAISA  # ₹20 per executed order (NOT per contract)
    exch = ROUND_TO_PAISA(intrinsic_value_paisa * exch_rate)
    sebi = ROUND_TO_PAISA(intrinsic_value_paisa * SEBI_RATE)
    ipft = ROUND_TO_PAISA(intrinsic_value_paisa * ipft_rate)
    stamp = 0  # no buy event
    gst = ROUND_TO_RUPEE((brokerage + exch + sebi + ipft) * 0.18)
    return stt + brokerage + exch + sebi + ipft + gst

  case EXPIRY_ASSIGN:
    # SHORT ITM assigned at expiry — short pays intrinsic loss from account,
    # but NO separate STT (was paid at sell-open as 0.15% × premium)
    # NOTE (§16 item 19, NOT decided): the sell-open premium-STT rate in the
    # parenthetical above is ERA-DEPENDENT, not a flat 0.15% — 0.0625% pre
    # 2024-10-01 / 0.10% to 2026-03-31 / 0.15% from 2026-04-01
    # (brutex/costs/constants.py STT_OPTIONS_*; DEC-COST-003, MIGRATION.md
    # #128). The DEC that implements this branch must apply the era rate.
    stt = 0
    brokerage = GROWW_BROKERAGE_PAISA  # ₹20 per order (NOT per contract)
    exch = sebi = ipft = stamp = 0  # no trade event
    gst = ROUND_TO_RUPEE(brokerage * 0.18)
    # CRITICAL: intrinsic loss is a CAPITAL loss (charged to account),
    # NOT a trading charge. Tracked separately in pnl.
    return stt + brokerage + gst

  case EXPIRY_WORTHLESS:
    # Option expired OTM — long lost premium (no exit transaction)
    # Short kept the premium (STT already paid at sell-open)
    stt = 0
    brokerage = GROWW_BROKERAGE_PAISA  # ₹20 EVEN on worthless — Groww gotcha (§4.3)
    exch = sebi = ipft = stamp = 0
    gst = ROUND_TO_RUPEE(brokerage * 0.18)
    return stt + brokerage + gst
```

### 4.3 Groww-specific gotcha — ₹20 brokerage on worthless options

Per agent research 2026-05-16: **Groww charges ₹20 per executed order
EVEN when an option expires worthless** (unlike Zerodha, which dropped
this to ₹0 in June 2024). This is a Groww-specific overhead that bites
short-OTM strategies — every short that expires worthless still costs
₹20 + 18% GST = ₹23.60.

For long-OTM strategies (buy a put, let it expire worthless), the
₹20 still fires on the auto-exercise/exit event.

**Source:** Groww Freshdesk + Chittorgarh review (cited in agent report).

**Implementation:** if `trade_outcome ∈ {EXPIRY_EXERCISE, EXPIRY_ASSIGN,
EXPIRY_WORTHLESS}` → brokerage = ₹20 per executed order (NOT per
contract).

### 4.4 Correction: prompt claimed "₹20 per contract" — WRONG

A Claude-chat-generated prompt (operator shared 2026-05-16) claimed
that Groww charges "₹20 per assigned/exercised CONTRACT" on expiry,
not per order. **This is incorrect.** Groww's actual wording is "₹20
per executed order" — verified via:
- Groww Freshdesk support article
- Groww brokerage calculator
- Chittorgarh, IPOGMPINDIA secondary sources (all converge)

A 100-lot held-to-expiry exercise costs ₹20 (per order), NOT ₹2,000.
The 100× discrepancy would have produced a massive cost over-counting
on held-to-expiry strategies. Caught before code-lock.

_Rounding helper definitions appear in §4.1 above (unchanged)._

---

## 5. Rounding rules table

| Component | Rate × premium gives | Round to | Mode | Why |
|---|---|---|---|---|
| Brokerage | ₹20.00 exact | exact (no round) | — | flat fee, no fraction |
| STT | premium × 0.10%/0.15% | **nearest rupee** | commercial half-up | Rule 11 STT Rules 2004 |
| Exchange (NSE/BSE) | premium × 0.0350%/0.0325% | nearest paise | commercial half-up | non-statutory; quoted in paise |
| SEBI fee | premium × 0.0001% | nearest paise | commercial half-up | non-statutory; quoted in paise |
| IPFT | premium × 0.0005% | nearest paise | commercial half-up | non-statutory; quoted in paise |
| Stamp duty | buy_premium × 0.003% | **nearest rupee** | commercial half-up | Indian Stamp Rules 2019 |
| CGST / SGST / IGST | base × 9%/9%/18% | **nearest rupee** | commercial half-up | Section 170 CGST Act |

**Rounding mode:** commercial half-up (ties at exactly 0.50 → round
UP). NOT banker's rounding (half-to-even). This matches §8.2 worked
example and broker contract notes from Zerodha + Angel One.

**Critical:** rupee-rounded items are rounded **per-leg** (per contract
note / invoice), then summed. This matches §8.2 worked example AND
real Groww contract note format.

---

## 6. GST base composition (LOCKED)

GST applies to **services provided**, NOT statutory levies.

| Included in GST base | Excluded from GST base |
|---|---|
| Brokerage | STT |
| Exchange transaction charge | Stamp duty |
| SEBI turnover fee | (everything else not listed left) |
| IPFT | |

Source: COSTS_VERIFIED.md §9, cross-cited ClearTax + Zerodha + Angel.

### 6b. Order-splitting / freeze quantity (LOCKED 2026-05-16)

Per agent research 2026-05-16 + ICICIdirect Dec-2025 freeze qty circular:
NSE/BSE cap maximum single-order quantity for index options. Exceeding
the cap → broker submits an ICEBERG ORDER, which splits into multiple
back-end orders. **Each slice = one executed order = ₹20 brokerage**.

| Instrument | Freeze qty (contracts/order) | Max lots/order (with current lot size) | Source |
|---|---:|---:|---|
| NIFTY | 1,800 | 1,755 (= floor(1800/65)×65 / 65) | ICICIdirect Dec 2025 |
| BANKNIFTY | 600 | 600 | NSE freeze table |
| SENSEX | ~1,000 | ~1,000 | **UNVERIFIED post-Mar-2026** (Angel mentions revision) |

```
# Calculator must compute order count:
freeze_lots = FREEZE_LOTS_TABLE[instrument]   # 1755 / 600 / 1000
n_orders_per_leg = ceil(n_lots / freeze_lots)
brokerage_paisa = GROWW_BROKERAGE_PAISA * 2 * n_orders_per_leg
# (× 2 for both buy + sell legs; × n_orders_per_leg for iceberg slices)
```

**Impact for Track 2 lot scales (typical 1-100 lots):**
- All NIFTY orders fit in 1 slice → `n_orders_per_leg = 1`
- All BANKNIFTY orders ≤ 600 lots fit → `n_orders_per_leg = 1`
- Most Track 2 sweep configurations never exceed freeze

**Impact when exceeded:**
- 2,000-lot NIFTY order → 2 slices → ₹80 round-trip brokerage (2 × 2 × ₹20)
- 5,000-lot NIFTY order → 3 slices → ₹120 round-trip brokerage
- All other charges scale linearly with notional regardless of slicing

**Phase 3 fixture F17 (NEW):** 2,000-lot NIFTY to exercise the slicing
branch. Expected `n_orders_per_leg = 2`, `brokerage_paisa = 8000`.

---

## 7. Effective-date switches

| Event | Trigger | Effect |
|---|---|---|
| STT hike 0.10% → 0.15% | `entry_dt.date() ≥ 2026-04-01` | Use `STT_RATE_POST_APR2026` (see §2). UNVERIFIED — depends on Finance Act 2026 gazette text. |
| Lot size change | `entry_dt.date() ≥ row.effective_from_date` | Use that row's `lot_size`. Same lot used at exit (no mid-trade change). |
| Tick size change | none through 2026 | `TICK_PAISA = 5` constant for entire 2021-2026 backfill window. |

---

## 8. Slippage application (Track 2 §9 — flat 1-tick)

| Side | Entry fill | Exit fill |
|---|---|---|
| Long (long_call / long_put) | `entry_bar_open + 1 tick` | `exit_bar_open - 1 tick` |
| Short (short_call / short_put) | `entry_bar_open - 1 tick` | `exit_bar_open + 1 tick` |

(Slippage is adverse — buy higher, sell lower.)

`1 tick = TICK_PAISA = 5` paisa = ₹0.05.

**Slippage cost per round-trip** (for retrospective display, NOT
double-counted):
```
slippage_paisa = 2 * TICK_PAISA * n_shares
             = 2 * 5 * n_shares
             = 10 * n_shares paisa
             = 0.10 INR * n_shares
```
- NIFTY: 0.10 × 65 = ₹6.50 per round-trip
- BANKNIFTY: 0.10 × 30 = ₹3.00 per round-trip
- SENSEX: 0.10 × 20 = ₹2.00 per round-trip

(This is already baked into the fill prices; the calculator does NOT
subtract it a second time as a separate line. Output struct exposes
`slippage_paisa` as informational.)

---

## 9. Per-broker variants

**Currently locked:** `groww`.

`zerodha` is wired up for comparability sanity-check only (Phase 6
"shadow-broker" diagnostic) — same ₹20/order brokerage means net cost
should match exactly when broker config is the only knob varied. If
they differ, calculator has a bug.

Future brokers (none in scope today) — each would add one row to
`COSTS_VERIFIED.md §8` and one constant; no algorithm change needed.

---

## 10. Annual aggregate (separate concern)

`compute_annual_tax(...)` in `brutex/costs/income_tax.py` consumes the
output ledger of `compute_trade_cost(...)`. See `COSTS_VERIFIED.md §18`
for full signature.

Per-trade calculator returns `int64 paisa` for both `total_charges`
and `net_pnl`. Annual aggregator sums these across all FY trades and
applies slab tax. Loss carry-forward is the aggregator's concern, not
this calculator's.

---

## 11. API surface (Python type hints, NO implementation)

```python
from dataclasses import dataclass
from datetime import datetime, date
from typing import Literal

Side       = Literal["long_call", "long_put", "short_call", "short_put"]
Instrument = Literal["NIFTY", "BANKNIFTY", "SENSEX"]
Broker     = Literal["groww", "zerodha"]
GstSplit   = Literal["intra_state", "inter_state"]

@dataclass(frozen=True)
class TradeInput:
    entry_dt_ist:        datetime
    exit_dt_ist:         datetime
    instrument:          Instrument
    side:                Side
    option_type:         Literal["CE", "PE"]
    strike_paisa:        int           # stored for audit only
    expiry_date:         date          # stored for audit only
    entry_bar_open_paisa: int          # int64 paisa
    exit_bar_open_paisa:  int
    n_lots:              int
    broker:              Broker = "groww"
    gst_split:           GstSplit = "intra_state"
    trade_outcome:       TradeOutcome = "NORMAL_CLOSE"   # §4.2 state machine
    intrinsic_value_paisa_at_expiry: int = 0     # required when EXPIRY_EXERCISE / EXPIRY_ASSIGN

# New enum:
TradeOutcome = Literal[
    "NORMAL_CLOSE",       # Track 2 default — buy + sell pair, no expiry held
    "EXPIRY_EXERCISE",    # LONG ITM auto-exercised at expiry (long pays STT on intrinsic)
    "EXPIRY_ASSIGN",      # SHORT ITM assigned at expiry (no STT, paid at open)
    "EXPIRY_WORTHLESS",   # Option expired OTM (long lost premium; short kept it)
]

@dataclass(frozen=True)
class TradeCostBreakdown:
    # quantities
    n_shares_per_lot:    int
    n_shares:            int
    # fill prices (after slippage)
    buy_fill_paisa:      int
    sell_fill_paisa:     int
    # notionals
    buy_notional_paisa:  int
    sell_notional_paisa: int
    # charges (each component, int paisa)
    brokerage_paisa:     int
    stt_paisa:           int
    exch_paisa:          int
    sebi_paisa:          int
    ipft_paisa:          int
    stamp_paisa:         int
    gst_paisa:           int
    # informational
    slippage_paisa:      int           # 2 * TICK * n_shares
    # roll-ups
    total_charges_paisa: int
    gross_pnl_paisa:     int           # incl slippage (slippage baked into fills)
    net_pnl_paisa:       int           # gross − charges
    # branch flags
    used_exercise_branch:    bool      # §4.2 STT-on-exercise was used
    n_orders_per_leg:        int       # §6b iceberg slicing (1 unless freeze qty exceeded)

def compute_trade_cost(trade: TradeInput) -> TradeCostBreakdown:
    """Pure function. No I/O, no global state. Deterministic. O(1)."""
    ...
```

---

## 12. Test fixture matrix (golden values for unit tests)

Phase 3 build acceptance: 100% pass rate on these fixtures.

### 12.1 The canonical NIFTY fixture (matches §8.2)

| Field | Value |
|---|---|
| entry_dt_ist | 2026-05-15 10:00 IST |
| exit_dt_ist | 2026-05-15 11:00 IST |
| instrument | NIFTY |
| side | long_call |
| entry_bar_open_paisa | 9_995 (= ₹99.95, so +1 tick = ₹100.00) |
| exit_bar_open_paisa | 12_005 (= ₹120.05, so -1 tick = ₹120.00) |
| n_lots | 1 |
| broker | groww |
| gst_split | intra_state |

Expected output (matches §8.2 worked example):

| Component | Expected paisa | INR |
|---|---|---|
| n_shares | 65 | — |
| buy_fill_paisa | 10_000 | ₹100.00 |
| sell_fill_paisa | 12_000 | ₹120.00 |
| buy_notional_paisa | 650_000 | ₹6,500 |
| sell_notional_paisa | 780_000 | ₹7,800 |
| brokerage_paisa | 4_000 | ₹40.00 |
| stt_paisa | 1_200 | ₹12 (= ₹11.70 → ₹12 rounded) |
| exch_paisa | 501 | ₹5.01 |
| sebi_paisa | 2 | ₹0.02 |
| ipft_paisa | 7 | ₹0.07 |
| stamp_paisa | 0 | ₹0 (= ₹0.195 → ₹0 rounded) |
| gst_paisa | 800 | ₹8 (= 2+2+2+2 per-leg rupee-rounded) |
| slippage_paisa | 650 | ₹6.50 |
| total_charges_paisa | 6_510 | ₹65.10 |
| gross_pnl_paisa | 130_000 | ₹1,300 |
| net_pnl_paisa | 123_490 | ₹1,234.90 |

### 12.2 Additional fixtures (Phase 3 acceptance set)

| # | Instrument | side | Premium move | n_lots | trade_date | STT era | Reason |
|---|---|---|---|---|---|---|---|
| F1 | NIFTY | long_call | ₹100 → ₹120 | 1 | 2026-05-15 | post-Apr | canonical (§12.1) |
| F2 | NIFTY | long_call | ₹100 → ₹80 | 1 | 2026-05-15 | post-Apr | losing trade (negative gross PnL) |
| F3 | NIFTY | short_call | ₹100 → ₹80 | 1 | 2026-05-15 | post-Apr | short profitable |
| F4 | NIFTY | short_call | ₹100 → ₹120 | 1 | 2026-05-15 | post-Apr | short losing |
| F5 | NIFTY | long_call | ₹100 → ₹120 | 1 | 2025-12-15 | pre-Apr (0.10%) | STT-era switch (lot 75 in 2025-Dec or 25 in 2025-Jul? check §8.3) |
| F6 | NIFTY | long_call | ₹100 → ₹120 | 10 | 2026-05-15 | post-Apr | 10-lot scaling (verify brokerage is still ₹40 total) |
| F7 | NIFTY | long_call | ₹50 → ₹60 | 1 | 2026-05-15 | post-Apr | deep-OTM low premium (8.9% friction expected) |
| F8 | BANKNIFTY | long_put | ₹200 → ₹250 | 1 | 2026-05-15 | post-Apr | BANKNIFTY (lot 30) NSE exchange |
| F9 | SENSEX | long_call | ₹100 → ₹120 | 1 | 2026-05-15 | post-Apr | SENSEX (lot 20) BSE exchange + BSE IPFT |
| F10 | NIFTY | long_call | ₹100 → ₹120 | 1 | 2024-05-15 | pre-Apr (0.10%) | lot size 25 (Apr-2024 era) |
| F11 | NIFTY | long_call | ₹100 → ₹120 | 1 | 2021-06-15 | pre-Apr (0.10%) | lot size 75 (pre-Jul-2021) |
| F12 | NIFTY | long_call | ₹100 → ₹120 | 1 | 2026-05-15 | post-Apr | gst_split=inter_state — verify IGST rounding differs |
| F13 | NIFTY | long_call | ₹100 → ₹100 | 1 | 2026-05-15 | post-Apr | zero gross PnL — should produce negative net PnL = -charges |
| F14 | NIFTY | long_call | ₹0.05 → ₹0.10 | 1 | 2026-05-15 | post-Apr | minimum premium edge case (5 paise) |
| F15 | NIFTY | long_call | ₹100 → ₹120 | 1 | 2026-03-31 | pre-Apr (0.10%) | day-before STT-hike boundary |
| F16 | NIFTY | long_call | ₹100 → ₹120 | 1 | 2026-04-01 | post-Apr (0.15%) | day-of STT-hike boundary |

Each fixture has a static `expected_*.json` file checked into
`tests/golden/calculator/`. Property test: every component value
matches byte-for-byte.

### 12.3 Multi-lot scaling table (NIFTY ATM, post-Apr-2026 STT 0.15%)

Same scenario as §12.1 (NIFTY ATM ₹100→₹120, lot=65), repeated for
lots 1..20 + 25. Demonstrates:
- Brokerage stays FLAT at ₹40 (per-order, not per-lot)
- Everything else scales linearly with notional
- Cost% drops from 5.01% (1 lot) → 1.52% (25 lots) → ~1.50% asymptote

| n | Shares | Gross PnL | Brkr | STT | Exch | SEBI | IPFT | Stamp | GST | **Charges** | Net PnL | Cost% |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 65 | 1,300.00 | 40.00 | 12 | 5.01 | 0.02 | 0.07 | 0 | 8 | **65.10** | 1,234.90 | 5.01% |
| 2 | 130 | 2,600.00 | 40.00 | 23 | 10.01 | 0.03 | 0.15 | 0 | 8 | **81.19** | 2,518.81 | 3.12% |
| 3 | 195 | 3,900.00 | 40.00 | 35 | 15.03 | 0.04 | 0.22 | 1 | 10 | **101.29** | 3,798.71 | 2.60% |
| 4 | 260 | 5,200.00 | 40.00 | 47 | 20.04 | 0.06 | 0.29 | 1 | 12 | **120.39** | 5,079.61 | 2.32% |
| 5 | 325 | 6,500.00 | 40.00 | 59 | 25.04 | 0.07 | 0.36 | 1 | 12 | **137.47** | 6,362.53 | 2.11% |
| 6 | 390 | 7,800.00 | 40.00 | 70 | 30.05 | 0.09 | 0.43 | 1 | 12 | **153.57** | 7,646.43 | 1.97% |
| 7 | 455 | 9,100.00 | 40.00 | 82 | 35.07 | 0.10 | 0.50 | 1 | 14 | **172.67** | 8,927.33 | 1.90% |
| 8 | 520 | 10,400.00 | 40.00 | 94 | 40.08 | 0.11 | 0.57 | 2 | 14 | **190.76** | 10,209.24 | 1.83% |
| 9 | 585 | 11,700.00 | 40.00 | 105 | 45.08 | 0.13 | 0.64 | 2 | 16 | **208.85** | 11,491.15 | 1.79% |
| 10 | 650 | 13,000.00 | 40.00 | 117 | 50.09 | 0.15 | 0.72 | 2 | 16 | **225.96** | 12,774.04 | 1.74% |
| 11 | 715 | 14,300.00 | 40.00 | 129 | 55.11 | 0.16 | 0.79 | 2 | 18 | **245.06** | 14,054.94 | 1.71% |
| 12 | 780 | 15,600.00 | 40.00 | 140 | 60.11 | 0.17 | 0.86 | 2 | 18 | **261.14** | 15,338.86 | 1.67% |
| 13 | 845 | 16,900.00 | 40.00 | 152 | 65.12 | 0.18 | 0.93 | 3 | 20 | **281.23** | 16,618.77 | 1.66% |
| 14 | 910 | 18,200.00 | 40.00 | 164 | 70.13 | 0.20 | 1.01 | 3 | 20 | **298.34** | 17,901.66 | 1.64% |
| 15 | 975 | 19,500.00 | 40.00 | 176 | 75.14 | 0.22 | 1.08 | 3 | 22 | **317.44** | 19,182.56 | 1.63% |
| 16 | 1,040 | 20,800.00 | 40.00 | 187 | 80.15 | 0.22 | 1.14 | 3 | 22 | **333.51** | 20,466.49 | 1.60% |
| 17 | 1,105 | 22,100.00 | 40.00 | 199 | 85.16 | 0.24 | 1.21 | 3 | 22 | **350.61** | 21,749.39 | 1.59% |
| 18 | 1,170 | 23,400.00 | 40.00 | 211 | 90.17 | 0.26 | 1.29 | 4 | 24 | **370.72** | 23,029.28 | 1.58% |
| 19 | 1,235 | 24,700.00 | 40.00 | 222 | 95.17 | 0.27 | 1.36 | 4 | 26 | **388.80** | 24,311.20 | 1.57% |
| 20 | 1,300 | 26,000.00 | 40.00 | 234 | 100.19 | 0.29 | 1.43 | 4 | 26 | **405.91** | 25,594.09 | 1.56% |
| **25** | **1,625** | **32,500.00** | **40.00** | **293** | **125.23** | **0.36** | **1.79** | **5** | **30** | **495.38** | **32,004.62** | **1.52%** |

### 12.4 Slippage broken out separately (informational)

Same scenarios with slippage as its own line. Raw bar opens: entry
₹99.95, exit ₹120.05. After ±1-tick slippage: fills ₹100.00 / ₹120.00.

| n | Raw Gross | + Slippage | + Charges | **Total Friction** | Net PnL | Friction% |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1,306.50 | 6.50 | 65.10 | **71.60** | 1,234.90 | 5.48% |
| 2 | 2,613.00 | 13.00 | 81.19 | **94.19** | 2,518.81 | 3.60% |
| 5 | 6,532.50 | 32.50 | 137.47 | **169.97** | 6,362.53 | 2.60% |
| 10 | 13,065.00 | 65.00 | 225.96 | **290.96** | 12,774.04 | 2.23% |
| 15 | 19,597.50 | 97.50 | 317.44 | **414.94** | 19,182.56 | 2.12% |
| 20 | 26,130.00 | 130.00 | 405.91 | **535.91** | 25,594.09 | 2.05% |
| 25 | 32,662.50 | 162.50 | 495.38 | **657.88** | 32,004.62 | 2.01% |

### 12.5 Per-lot breakdown formula (scales linearly except brokerage)

For arbitrary `n` lots on NIFTY ATM ₹100→₹120 post-Apr-2026:

```
shares_n     = 65 × n
buy_not_n    = 6,500 × n        (₹)
sell_not_n   = 7,800 × n        (₹)
gross_pnl_n  = 1,300 × n        (₹)

brokerage    = ₹40              (FLAT, regardless of n)
STT          = round(11.70 × n)        → nearest rupee
exch_total   = round(2.27695 × n) + round(2.73234 × n)   → nearest paisa, per leg
SEBI_total   = round(0.0065 × n) + round(0.0078 × n)
IPFT_total   = round(0.0325 × n) + round(0.039 × n)
stamp        = round(0.195 × n)        → nearest rupee
GST          = 2 × [round(0.09 × (20 + ...)) + round(0.09 × (20 + ...))]   per-leg, rupee
```

Why cost% drops: brokerage ₹40 is constant ⇒ amortized cost%
asymptotes to ~1.50% as n→∞ (the sum of all percentage-based rates
on a 20% gain).

### 12.6 BANKNIFTY / SENSEX scaling pointers

Same algorithm; different lot size + exchange routing.

| Instrument | Lot size | Exchange | Exch rate | IPFT rate | Tick |
|---|---:|---|---:|---:|---:|
| NIFTY | 65 | NSE | 0.03503% | 0.0005% | ₹0.05 |
| BANKNIFTY | 30 | NSE | 0.03503% | 0.0005% | ₹0.05 |
| SENSEX | 20 | BSE | 0.03250%* | 0.0005% | ₹0.05 |

*BSE Bankex options exchange rate is **UNVERIFIED** — see §16 item 3.

A BANKNIFTY equivalent table would replace `n × 65` with `n × 30` and
keep NSE rates. A SENSEX table would use `n × 20` and BSE rates.
Phase 3 build emits all 3 instrument × 20 lot count = 60 fixture
files automatically from a parametrized generator.

### 12.7 Long vs Short — side-symmetry verification (LOCKED 2026-05-16)

Operator question 2026-05-16: "is calculator same for both options
buying and selling?" Answer: **NO** for most cases — because STT is
on the SELL leg only and stamp is on the BUY leg only.

**Identity rule:** total charges depend on which leg has the higher
premium (more STT) and which has the lower premium (less stamp),
NOT on long/short label. Three identity classes:

| Comparison | Result |
|---|---|
| Same price pair, mirror outcomes (one wins, one loses) | **Different charges** (winner pays more) |
| Both profitable with same gross PnL | **Identical charges** (both sell at higher leg) |
| Both losing with same gross PnL but opposite directions | **Different charges** (depends on which leg is the actual sell) |

#### Worked examples (n=25 lots NIFTY, post-Apr-2026 STT 0.15%)

**Scenario A — Same price pair (₹100 / ₹120), one winner one loser:**

| Side | Direction | Gross | Brkr | STT | Stamp | Exch | GST | **Total** | Net |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| LONG winner buy@100, sell@120 | ↑ | +32,500 | 40 | 293 | 5 | 125.23 | 30 | **495.38** | +32,004.62 |
| SHORT loser sell@100, buy@120 | ↑ | -32,500 | 40 | 244 | 6 | 125.23 | 30 | **447.38** | -32,947.38 |

Difference: ₹48 less for short loser (STT base ₹100 vs ₹120).

**Scenario B — Both PROFITABLE (long ↑ premium vs short ↓ premium):**

| Side | Direction | Gross | Brkr | STT | Stamp | Exch | GST | **Total** | Net |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| LONG winner buy@100, sell@120 | ↑ | +32,500 | 40 | 293 | 5 | 125.23 | 30 | **495.38** | +32,004.62 |
| SHORT winner sell@120, buy@100 | ↓ | +32,500 | 40 | 293 | 5 | 125.23 | 30 | **495.38** | +32,004.62 |

**Identical.** Both sell at ₹120 → same STT.

**Scenario C — Both LOSING (long ↓ premium vs short ↑ premium):**

| Side | Direction | Gross | Brkr | STT | Stamp | Exch | GST | **Total** | Net |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| LONG loser buy@100, sell@80 | ↓ | -32,500 | 40 | 195 | 5 | 102.46 | 26 | **370.21** | -32,870.21 |
| SHORT loser sell@100, buy@120 | ↑ | -32,500 | 40 | 244 | 6 | 125.23 | 30 | **447.38** | -32,947.38 |

Difference: ₹77 more for short loser (sells at ₹100 vs long-loser sells at ₹80).

**Phase 3 fixtures F18-F23 (NEW):** 6 long/short symmetry fixtures
covering each scenario at n=1 / n=25 / n=100. Calculator MUST produce
exactly these outputs.

### 12.8 Position-side charges NOT in calculator (event-based ledger)

Per agent research 2026-05-16, the following side-specific costs may
apply to options sellers (shorts) but are NOT per-trade calculator
concerns:

| Cost | When | Where modeled |
|---|---|---|
| **Margin shortfall penalty** | 0.5%–5%/day on shortfall amount (SEBI 2021 regime) | Event-based ledger; only fires if backtest models capital |
| **ELM 2% on expiry-day short index** | Margin requirement, not P&L | Out of scope (no margin in Track 2 backtest) |
| **Peak-margin penalty** | Per random intraday snapshot | Event ledger |
| **Auto-square-off** | Groww ₹20/order when RMS-initiated | Event ledger |
| **Call & Trade** | Dealer-assisted orders | Event ledger (rare) |

Track 2 calculator does NOT model these — they fire on operational
events, not on simulated signal-to-fill paths. Per Golden Rule #3,
this is scope-locked: per-trade calculator = 7 charges + slippage,
nothing more.

---

## 13. Edge cases (explicit list)

| Case | Behavior |
|---|---|
| Zero gross PnL (entry_open == exit_open) | `net_pnl = -total_charges` (always negative; no charge-skip) |
| Negative gross PnL (losing trade) | Same compute path; charges applied regardless. `net_pnl` more negative than `gross_pnl`. |
| Premium below ₹0.05 (tick floor) | Reject input — premium < 1 tick is impossible on real options. Calculator raises `ValueError`. |
| Entry on lot-size transition date | Use the row whose `effective_from_date ≤ entry_dt.date()` (latest matching). At exit same lot size — they don't re-quantize mid-trade. |
| Entry on STT-hike date 2026-04-01 | Use `STT_RATE_POST_APR2026`. Day 2026-03-31 uses `STT_RATE_PRE_APR2026`. |
| Multi-lot brokerage | Brokerage is **flat per ORDER**, not per lot. 10-lot trade = same ₹40 brokerage as 1-lot. STT and exchange scale linearly with notional. |
| BSE Bankex exchange charge | Currently using `0.03250%`. UNVERIFIED — operator must confirm before code-lock. |
| Short side (premium received) | Buy leg is at exit (closing the short); sell leg is at entry (opening the short). PnL = sell_notional - buy_notional ≥ 0 means profit. STT still applies to sell (entry) leg. Stamp duty applies to buy (exit) leg of the short. |

---

## 14. What this calculator does NOT compute

| Excluded | Why | Where it lives |
|---|---|---|
| Margin / peak-margin requirement | Backtest doesn't simulate margin call | (operational, post-backtest) |
| Dividend adjustment | Cash-settled options on indexes — no dividend | N/A |
| Mark-to-market intraday | Position held until exit signal | N/A |
| Lot-size mid-trade quantization | Same lot used entry → exit | §13 |
| Annual income tax | Aggregates many trades; not per-trade | `compute_annual_tax(...)` per `COSTS_VERIFIED.md §18` |
| ITR form / TDS / advance tax | Operational concern | (out of scope) |
| Loss carry-forward (8 years) | Annual aggregator concern | `compute_annual_tax(...)` |
| Multi-broker comparison sweep | Sweep orchestrator concern | Phase 4 |
| Per-trade margin attribution | Capital-deployment metric | Phase 4 metrics |

---

## 15. Pipeline integration

```
ENTRY signal (Phase 3 strategy engine emits trade intent)
   ↓
strike-selection (Track 2 §6b rule R8/R9/R10) → option_type, strike, expiry
   ↓
fill at entry_bar_open + 1 tick
   ↓
[hold position; check exit conditions on each bar]
   ↓
EXIT signal (e.g., 15:20 forced or TP/SL hit)
   ↓
fill at exit_bar_open - 1 tick (for long)
   ↓
compute_trade_cost(...) → TradeCostBreakdown  ← THIS CALCULATOR
   ↓
ledger.append(trade_id, breakdown)            ← persistent ledger (Parquet)
   ↓
[at run end: aggregate ledger by FY]
   ↓
compute_annual_tax(...) → AnnualTaxResult
```

Calculator is **pure**: no I/O, no DB, no rate fetch, no random.
Determinism is the audit oracle (Golden Rule #7 idempotence).

---

## 16. Open items (block code-lock) — EXPANDED 2026-05-16

Per `TRACK2_OPTIONS_SPEC.md §8b` + 2026-05-16 deep research, items
still UNVERIFIED (3 original + 5 new):

| # | Item | Source of uncertainty | Impact | Blocker? |
|---|---|---|---|---|
| 1 | **Groww official rate card** (₹20 brokerage etc.) | groww.co/pricing returns HTTP 403 to all automated WebFetch | If actual brokerage ≠ ₹20, recompute all fixtures | YES |
| 2 | **Finance Act 2026 STT 0.15% gazette text** | Multiple secondary sources, no agent could pull final gazette PDF | If 0.15% wrong, recompute all post-Apr-2026 fixtures | YES (but date-switched, easy patch) |
| 3 | **BSE Bankex options exchange rate** | Conflicting: ₹3,250/cr vs ₹325/cr (10× discrepancy) | Only SENSEX fixture F9 affected | LOW (SENSEX only) |
| 4 | **STT-on-exercise rate post-Apr-2026** | ClearTax cites 0.15%; Finance Act gazette unconfirmed | Only fires if `held_to_expiry_exercise=True` (Track 2 never sets this) | LOW for Track 2; HIGH if future tracks model held-to-expiry |
| 5 | **SENSEX freeze quantity post-Mar-2026** | Angel One mentions revision, exact new cap not extracted | Only affects orders > ~1,000 lots SENSEX (rare) | LOW |
| 6 | **Groww Call & Trade exact rate** | Secondary sources conflict: ₹20 vs ₹50/order | Event-based, NOT per-trade — but if event ledger built, needs verified rate | LOW for calculator; MEDIUM for event ledger |
| 7 | **Groww API-vs-UI brokerage parity** | No source mentions a differential; assumed identical | If algo-API orders incur additional brokerage, ALL fixtures off | LOW (unverified but not flagged anywhere) |
| 8 | **Groww auto-SqOff trigger conditions** for short-options NRML overnight | Search snippets imply RMS-initiated only, not scheduled EOD | Only relevant for event ledger; not per-trade calculator | LOW |
| 15 | **Futures STT 0.05% post-Apr-2026** | Budget 2026 (Finance Bill 2026) per multiple secondary sources | Only matters if futures scope added; Track 2 options-only | LOW (informational; verified by 2026-05-16 agent) |
| 16 | **Groww ₹20 brokerage on EXPIRED WORTHLESS options** | Groww-specific gotcha vs Zerodha which dropped to ₹0 in June 2024 | Track 2 never holds to expiry (15:20 forced exit), but expiry branches must use ₹20 not ₹0 | LOW |
| 17 | **STT-on-exercise rate 0.125% pre-Apr-2026 vs 0.15% post** | Both rates cited but Finance Act 2026 gazette text not pulled | Only fires for `EXPIRY_EXERCISE` outcome (Track 2 never sets this) | LOW for Track 2; MEDIUM for future tracks |
| 18 | **`EXPIRY_*` STT-era key: `exit_dt` vs entry date** — §4.2's illustrative pseudo-code keys the exercise STT era on `exit_dt`, while DEC-COST-002 (MIGRATION.md #127) locked the NORMAL_CLOSE key to `entry_dt_ist.date()` (§4 Step 5, §7) | Exercise settles AT expiry so exit-keying may be intended — an undecided design question, not a typo. Registered 2026-07-06 (DEC-TRACK2-COSTS-SPEC-RECONCILE-001), deliberately NOT decided | Only fires if the `EXPIRY_*` branches are ever implemented (Track 2 never sets them, DEC-RISK-011) | LOW for Track 2; owned by the DEC that implements the expiry branches |
| 19 | **`EXPIRY_ASSIGN` premium-STT parenthetical is era-dependent** — §4.2's "(already paid at sell-open as 0.15% × premium)" hardcodes one era's rate; the actual sell-open rate is 0.0625% / 0.10% / 0.15% by era (`brutex/costs/constants.py` `STT_OPTIONS_*`, DEC-COST-003) | Spec note written against the post-Apr-2026 era only. Registered 2026-07-06 (DEC-TRACK2-COSTS-SPEC-RECONCILE-001), deliberately NOT decided | Documentation/impl of `EXPIRY_ASSIGN` only (unreachable in Track 2) | LOW for Track 2; the implementing DEC must apply the era rate |

**Items 1-3:** original 3 from `TRACK2_OPTIONS_SPEC §8b`.
**Items 4-8:** added 2026-05-16 from deep research on long/short
asymmetry + freeze quantity + hidden costs.
**Items 18-19:** added 2026-07-06 (DEC-TRACK2-COSTS-SPEC-RECONCILE-001) —
§4.2 expiry-branch drift observations REGISTERED without deciding them;
they are design questions for the (unauthorized, future) DEC that
implements the `EXPIRY_*` branches, not operator-verification items.

**All 8 items require operator manual verification.** Items 1, 2, 4
materially affect calculator output. Items 3, 5-8 affect edge cases
or related modules.

### 16.1 Verification action checklist

Operator manual steps before code-lock:

1. [ ] Visit `groww.co/pricing` in browser, screenshot, confirm ₹20/order.
2. [ ] Verify Finance Act 2026 gazette PDF: STT rate + effective date.
3. [ ] Verify BSE Membership Transaction Fees PDF for Bankex options.
4. [ ] Verify Finance Act 2026 STT-on-exercise rate (if same as sell-leg STT, single rate).
5. [ ] Check BSE notice board for SENSEX freeze qty post-Mar-2026.
6. [ ] Open Groww support ticket: confirm Call & Trade exact rate.
7. [ ] Open Groww support ticket: confirm no brokerage differential for API orders.
8. [ ] Open Groww support ticket: confirm auto-SqOff trigger conditions for NRML short index options.

Items 1, 2, 4 are CALCULATOR BLOCKERS. Items 3, 5-8 unblock related
work but calculator can ship without them.

---

## 17. Acceptance criteria for Phase 3 build

1. ✅ All **23 fixtures** pass byte-for-byte (`int64 paisa` exact match): F1-F16 base + F17 iceberg slicing + F18-F23 long-vs-short symmetry.
2. ✅ Hypothesis property tests:
   - `total_charges_paisa ≥ 0` (never negative)
   - `total_charges_paisa == sum_of_components` (idempotent decomp)
   - `net_pnl_paisa == gross_pnl_paisa - total_charges_paisa` (definitional)
   - For any two trades with identical inputs → identical breakdowns (determinism)
3. ✅ Performance: 10,000 calls/sec single-threaded (p99 < 100 µs).
4. ✅ Mutation score ≥ 85% on `compute_trade_cost` (mutmut Python).
5. ✅ Zero `float` in hot path; all `int64`.
6. ✅ Coverage: 100% line + 100% branch.

---

## 18. Amendment log

| Date | Change | Why |
|---|---|---|
| 2026-05-16 | Initial spec | Consolidate §8 + §9 + §18 of `TRACK2_OPTIONS_SPEC.md` into a single calculator build-target spec |
| 2026-05-16 | Fixed §4.1 rounding bug | TRUNC_TO_PAISA → ROUND_TO_NEAREST_PAISA. Truncation would have produced ₹5.00 exchange charge in canonical fixture; §8.2 LOCKED requires ₹5.01. Commercial half-up matches Zerodha + Angel contract notes. |
| 2026-05-16 | Added §12.3 multi-lot table | Operator request: verify cost scaling for 2..20 + 25 lots. Demonstrates brokerage is FLAT ₹40 regardless of lots; cost% asymptotes to ~1.50%. |
| 2026-05-16 | Added §12.4 slippage broken out | Operator clarity: show slippage as separate line even though it's baked into fill prices in main algorithm. |
| 2026-05-16 | Added §12.5 per-lot formula | Demonstrates O(1) linear scaling formula for any n. |
| 2026-05-16 | Added §12.6 BANKNIFTY/SENSEX pointer | Same algorithm, different lot/exchange routing. Phase 3 generator emits 60 fixtures (3 instruments × 20 lot counts) parametrically. |
| 2026-05-16 | Added §4.2 STT-on-exercise branch | Operator question on buy-vs-sell asymmetry triggered deep research. Agent found that LONG ITM held-to-expiry incurs different STT (0.125%→0.15% on intrinsic value, not on premium). Track 2 never sets this flag, but spec must support forward-compatibility. Added `held_to_expiry_exercise` + `intrinsic_value_paisa_at_expiry` inputs. |
| 2026-05-16 | Added §6b freeze quantity / order splitting | NSE freeze quantities: NIFTY 1,800 contracts (1,755 lots), BANKNIFTY 600 lots, SENSEX ~1,000 lots. Above freeze, broker submits iceberg → multiple back-end orders → ₹20 brokerage per slice. Calculator now computes `n_orders_per_leg = ceil(n_lots / freeze_lots)`. Added F17 fixture (2,000-lot NIFTY = 2 slices). |
| 2026-05-16 | Added §12.7 long-vs-short symmetry table | Three identity classes: (A) same price pair mirror outcomes → DIFFERENT, (B) both profitable same gross → IDENTICAL, (C) both losing same gross → DIFFERENT. Concrete worked examples at n=25. Added F18-F23 fixtures. |
| 2026-05-16 | Added §12.8 position-side costs NOT in calculator | Margin shortfall, ELM 2%, peak-margin, auto-SqOff, Call & Trade — all event-based, NOT per-trade. Scope-locked. |
| 2026-05-16 | Expanded §16 from 3 to 8 unverified items | Added: (4) STT-on-exercise rate, (5) SENSEX freeze qty post-Mar-2026, (6) Groww Call & Trade rate, (7) Groww API-vs-UI brokerage parity, (8) Groww auto-SqOff trigger conditions. Items 1-3 are CALCULATOR BLOCKERS; items 4-8 affect related modules. Added 8-step operator verification checklist (§16.1). |
| 2026-05-16 | Updated §11 API: 2 new inputs + 2 new outputs | `held_to_expiry_exercise` + `intrinsic_value_paisa_at_expiry` inputs; `used_exercise_branch` + `n_orders_per_leg` outputs for audit. |
| 2026-05-16 | Acceptance criteria §17: 16 → 23 fixtures | F17 iceberg + F18-F23 long-vs-short symmetry. |
| 2026-05-16 | §4.2 rewritten as 4-outcome state machine | Operator shared claude-chat-generated prompt 2026-05-16. Adopted prompt's 5-action enum (mapped to 4 outcomes: NORMAL_CLOSE + 3 expiry sub-cases). Backward-compatible via `trade_outcome="NORMAL_CLOSE"` default. |
| 2026-05-16 | §4.3 Groww ₹20-on-worthless gotcha documented | Agent confirmed Groww charges ₹20 even on options expiring worthless (unlike Zerodha which dropped to ₹0 in June 2024). Material for short-OTM held-to-expiry strategies. |
| 2026-05-16 | §4.4 Prompt-error correction recorded | Claude-chat prompt claimed "₹20 per CONTRACT" for exercise/assignment. Verified WRONG: actual is "per executed ORDER". Would have over-counted 100-lot exercise brokerage by ₹1,980. |
| 2026-05-16 | §16 expanded to 17 unverified items (added 15, 16, 17) | Futures STT 0.05% (informational), Groww ₹20-on-worthless, STT-on-exercise rate split. |
| 2026-05-16 | TradeOutcome enum added to §11 API | Replaces `held_to_expiry_exercise: bool`. Backward-compat via DEPRECATED label on the bool. New fixtures use enum. |
| 2026-07-06 | §4 Step-1 short-side slippage resolved to the §8 ADVERSE rule (DEC-TRACK2-COSTS-SPEC-RECONCILE-001) | §4's pseudocode disagreed with §8 on BOTH short legs (short sell = `exit_open + tick` — wrong bar; short cover buy = `exit_open − tick` — favorable) and carried a literal "# CHECK SIGN" inside a LOCKED spec (the #963 MINOR-4 contradiction). §8 (adverse — buy higher, sell lower; consistent with `TRACK2_OPTIONS_SPEC §9.2`'s per-action rule) is authoritative; "# CHECK SIGN" deleted; `brutex/costs/calculator.py`'s short SELL leg + the byte-exact test re-pinned to the resolved rule (branch stays production-unreachable — BUY-only DEC-EXEC-008 unchanged). §4.2 gained the implementation-status block (Step-0 guard ≠ state machine, MIGRATION.md #963); the `exit_dt` era-key + assign premium-STT drifts registered as §16 items 18-19 WITHOUT deciding them; §2 gained the pre-2024-10-01 0.0625% STT regime row. |

---

**END OF SPEC.**
