# 09 — Capital, Margin & Position Sizing for Option Sellers

**Status:** RESEARCH / DESIGN-READINESS ONLY — zero engine code; the sweep engine remains
SPOT-ONLY (DEC-ENGINE-SPOT-ONLY-001) and options remain REFERENCE_ONLY; any activation is a
future operator-gated DEC/ADR.
**Date:** 2026-07-19
**Provenance:** multi-agent deep-research session, docs-only branch
`claude/options-strategies-design`, deliberately NO PR (fleet-wide PR-creation freeze).

**Evidence legend:**
- **[REPO path:line]** — read directly in the research session from the working tree.
- **[WEB source]** — externally verified via named source/URL (URLs consolidated in §7).
  Environment caveat: many primary hosts (sebi.gov.in, nseindia.com archives, nseclearing.in,
  zerodha.com) returned HTTP 403 through the research proxy, so several primary circulars are
  cited at **search-capture/mirror depth**, not full-document depth — flagged per row.
- **[ARITH]** — an arithmetic identity derived from tagged inputs; derivation shown.
- **[UNVERIFIED]** — cannot be claimed with current data; the closing action is named (§6).

**Environment measurement disclosure:** NO data lake exists in the research environment —
`BRUTEX_LAKE_ROOT` was unset and `~/.brutex/lake` absent (`ls` → exit 2, verified in-session).
**Nothing in this doc is "measured from the lake"**; every stress statistic is externally
sourced or described as *computable in production* from the 2020→today 1-minute store the pull
maintains (CLAUDE.md §2 Rule 3).

**Sibling docs:** margin/limits/bans/freeze/circuit *mechanics* in full detail live in the
microstructure doc of this series (doc 02); the 2024-25 regulatory measures and product-landscape
regime windows live in the regulatory-landscape doc. This doc summarizes both only as far as
capital, margin modeling, and sizing require.

---

## 1. Margin mechanics for option WRITERS (summary — full detail in doc 02)

### 1.1 The repo already took a position — read it first

Three repo artifacts are load-bearing priors for everything below:

1. **`brutex/engine/margin_history.py` — the shipped REFUSAL-WINDOW margin channel (R47).**
   Its module docstring is the house law in miniature:
   > "Recon found NO sourceable SPAN history anywhere — nothing in code, no NSE SPAN-archive
   > integration, and `docs/track2/TRACK2_RISK_MGMT.md` §2.3 explicitly FORBIDS hard-coded
   > margin constants (\"must be computed live from broker SPAN file at backtest time\"). So
   > every shipped table is ONE refusal row from 2020-01-01: `margin_for_date` raises the typed
   > `UnverifiedMarginRegimeError` for every date in the data window, and the ROI surface
   > (`roi_on_margin` / `margin_roi_payload`) renders `status="unverified"` — R47's mechanism
   > delivered WITHOUT an invented number. NO runtime escape hatch; the ONLY sanctioned override
   > paths are DATA (the locked ETC rule): (a) an exchange-archive/circular-cited
   > `MarginRegimeRow`, or (b) an ADR-stamped row with `source="ASSUMED per ADR-00XX"`."
   > [REPO brutex/engine/margin_history.py:12-23]

   The all-refusal v1 tables ship for exactly the three ENGINE instruments
   [REPO brutex/engine/margin_history.py:160-173], and `roi_on_margin` degrades to
   `MarginRoiVerdict(status="unverified", roi_pct=None, …)` instead of raising on display
   surfaces [REPO brutex/engine/margin_history.py:263-270]. This **is V1** in §5 — it already
   exists.

2. **`docs/track2/TRACK2_RISK_MGMT.md` — the prior margin/risk research pass** (in-repo, itself
   source-cited, carrying its own "verify UNVERIFIED cites before code-lock" banner
   [REPO docs/track2/TRACK2_RISK_MGMT.md:3-4]). Reused rows: the margin-stack summary
   [REPO :76-91]; the SEBI peak-margin penalty schedule [REPO :93-105]; §2.3's
   order-of-magnitude table + the two sanctioned margin methods ("(a) pull a SPAN file per
   backtest day from exchange archives, OR (b) use a deterministic SPAN replica (Black-Scholes
   shock grid + VIX bucket), flag both in audit log with `margin_method` field") [REPO :107-121];
   the Groww margin-shortfall auto-close (> ₹5,000) [REPO :404-407]; the expiry-day rules block
   [REPO :419-431]; the honest-limits row keeping SPAN estimates non-hard-values [REPO :542-544].
   Where the microstructure research CORRECTED this draft, the corrections are carried below.

3. **`brutex/options/lot_size_history.py` — the citation-grounded per-date lot table** all
   sizing arithmetic must key on [REPO brutex/options/lot_size_history.py:8-19, 58-77].

### 1.2 The margin stack a short option position posts

Condensed from the microstructure research (doc 02 carries the full 10-row table + sources):

| # | Component | Current parameter (as sourced) | Evidence |
|---|---|---|---|
| 1 | **SPAN (Initial) margin** | Portfolio scan-risk over the SPAN scenario array; NSE Clearing publishes risk-parameter files intraday. Repo prior-research: "16-scenario array, recalibrated 6× intraday" [REPO docs/track2/TRACK2_RISK_MGMT.md:78-79] — the 16-count + cadence were NOT independently re-verified ([UNVERIFIED] — §6 CM-U16) | [WEB NSE Clearing NSCCL-SPAN page, search-captured] |
| 2 | **Price Scan Range** | Index: 6σ scaled by √2 (2-day MPOR), floor **9.3%** of underlying; single-stock: 6σ·√2, floor **14.2%** | [WEB NSE Clearing NSCCL-SPAN parameters, search-captured] |
| 3 | **Exposure margin** | Index F&O: **3% of notional** (options: on SHORT positions only). Stock F&O: **higher of 5% or 1.5σ**. **Corrects** the repo draft's "typically 3-5%" [REPO docs/track2/TRACK2_RISK_MGMT.md:80-81] | [WEB NSE Clearing Margins page, search-captured] |
| 4 | **ELM** | Separate CC add-on; for futures calendar spreads levied on 1/3 of far-month contract value | [WEB NSE Clearing Margins page, search-captured; REPO docs/track2/TRACK2_RISK_MGMT.md:82-83] |
| 5 | **Expiry-day additional ELM** | **+2% of contract value** on ALL short index options of the expiring series — open at SOD AND opened intraday; applies even if hedged. Effective **2024-11-20** (SEBI Oct-2024 circular) | [REPO docs/track2/TRACK2_RISK_MGMT.md:84-87, 421-423] + [WEB Zerodha Z-Connect; Zerodha support; Navia support]. Primary-text scope wording [UNVERIFIED — CM-U18] |
| 6 | **Net Option Value** | Only SELL positions are margined; long-option value credits as an offset in portfolio margining | [WEB NSE Clearing Margins page, search-captured] |
| 7 | **MTM** | Daily cash settlement — not a margin component but consumes free collateral headroom | [REPO docs/track2/TRACK2_RISK_MGMT.md:88-89] |
| 8 | **Peak margin** | Margin maintained INTRADAY — 4 random snapshots/day, max snapshot = the day's obligation; **100% of SPAN+exposure since 2021-09-01** (phased 25%→50%→75%→100% from Dec-2020) | [WEB Paytm Money; Bramesh; Business Standard Jun-2021; REPO docs/track2/TRACK2_RISK_MGMT.md:95, 104-105] |

Long options = premium only (no SPAN/exposure), and since **2025-02-01** the premium must be
collected **upfront** from buyers — verified at mirror depth [WEB CSE mirror of SEBI Oct-2024
circular (Oct_011024.pdf); taxguru summary]. Spread/portfolio offsets are delta-based inside
SPAN (verticals net inside the scan array; calendars net across months but pay a Calendar Spread
Charge whose exact rate is [UNVERIFIED — CM-U17]); the calendar-spread treatment is granted only
until near-month expiry — and since Feb-2025 NOT on the expiry day itself (§5.3 dated rows).

**Backtest-history implication (verified):** the repo's data window starts 2020-01-01
[REPO brutex/engine/margin_history.py:152] — it straddles all four peak-margin phases, so any
writer-capital-efficiency simulation before Sep-2021 lives under a different intraday margin
regime than after.

---

## 2. Margin per structure — current-regime, order-of-magnitude estimates

### 2.1 Lot sizes first (the multiplier every number keys on)

Per-date table shipped in code (citation-grounded) [REPO brutex/options/lot_size_history.py:58-77]:
current regime **NIFTY 65 / BANKNIFTY 30 / SENSEX 20** (Jan-2026 series onward)
[REPO brutex/options/lot_size_history.py:63,71,76-77].

- **External cross-check (from the sibling regulatory research):** NSE circular 176/2025 /
  FAOP70616 (2025-10-03) — NIFTY 75→65, BANKNIFTY 35→30, effective for the Jan-2026 series
  (last old-lot weekly 2025-12-23, first new-lot weekly 2026-01-06, monthly from 2026-01-27)
  [WEB NSE FAOP70616 via Zerodha bulletin 429705; Angel One]. This substantially closes the
  intra-repo drift flag: `docs/track2/TRACK2_RISK_MGMT.md` §2.3's "Lot size (2026)" NIFTY 75 /
  BANKNIFTY 35 [REPO docs/track2/TRACK2_RISK_MGMT.md:112-117] reflects the Jul→Dec-2025
  transitional regime, and the code table's 65/30/20 carries the later circular. Residual: the
  cross-check is search-capture depth, and the code encodes day-granular dates
  (`date(2026,1,1)` / `date(2025,12,30)`) vs the exchange's per-series transition — a ±1-series
  approximation [UNVERIFIED as to full-document confirmation — CM-U1]. Sensitivity of 65-vs-75
  is ~15% on every NIFTY per-lot figure below; all NIFTY arithmetic states the lot used.
- The 7-transition history means **any per-lot margin/credit series is non-stationary by
  construction** — sizing must consume `get_lot_size(instrument, date)` per trade date, never a
  constant ("Using a single lot would mis-size positions by 3-7× in places"
  [REPO brutex/options/lot_size_history.py:4-6]).

### 2.2 The estimates table (NIFTY, 1 lot; other indices qualitative below)

| # | Structure (canonical seller shape) | Margin basis | Est. margin, 1 NIFTY lot | Evidence tier |
|---|---|---|---|---|
| 1 | **Naked short put** (or call), 1 leg, ~ATM–20Δ | Full SPAN+exposure+ELM on one short leg | ≈ ₹0.85–1.1 lakh | [ARITH] derived: a 2-leg strangle margins ₹1.45–2L (rows 2-3) and SPAN charges the second leg only partially, so 1 leg ≈ 55–70% of a strangle; standalone broker-calculator confirmation [UNVERIFIED — CM-U2] |
| 2 | **Short strangle** (~20Δ wings) | Both short legs, partial SPAN offset | **₹1.45 lakh** (Zerodha Varsity worked example); "often upwards of ₹1.5–2 lakh per lot" | [WEB Zerodha Varsity iron-condor chapter — figure surfaced via search, page 403'd from the proxy (CM-U14); WEB Samco short-strangle article] |
| 3 | **Short straddle** (ATM both legs) | Same stack; ATM legs carry the largest scan risk → ≥ strangle | ≈ ₹1.5–2 lakh | [WEB Choice India straddle article + Samco; consistent with repo ₹1.0–1.5L @ its assumed lot 75 [REPO docs/track2/TRACK2_RISK_MGMT.md:112-117]] |
| 4 | **Iron condor** (strangle + bought wings) | Defined-risk portfolio; SPAN spread benefit | **₹44,303** in the same Varsity worked example (vs ₹1.45L unhedged = **−69.5%**); an "80% margin reduction" figure also circulates | [WEB Zerodha Varsity (via search); WEB share.market / navigationtrading for the 80% claim — two different examples, both quoted, neither generalizable (CM-U15)] |
| 5 | **Bull put spread** (credit vertical) | Defined-risk; margin ≈ scan risk of the pair, floored near max-loss | ≈ ₹20–30k (order of magnitude) | [ARITH] one condor ≈ two verticals → ₹44,303/2 ≈ ₹22k; exact rupee figure per width [UNVERIFIED — CM-U3]; the max-loss LAW is exact: max_loss = (width − net_credit) × lot [WEB Fidelity / Zerodha Varsity bull-put-spread chapters] |
| 6 | **Cash-secured put** (a sizing DISCIPLINE, not an exchange margin class) | Seller voluntarily reserves full assignment notional | ≈ ₹16.4 lakh notional at S=25,000×65 (illustrative) | [ARITH] S×lot; India index options are cash-settled — the CSP discipline = notional-cap sizing (§4 law N) |
| 7 | **Covered call** (index proxy: long futures + short call) | Futures margin + short call, SPAN offset between legs | futures leg alone ≈ low-₹1L class; combined [UNVERIFIED — CM-U5] | index futures margin ≈ 11-13% of notional historically [UNVERIFIED — CM-U5]; true stock covered calls need physical shares (out of engine scope — stocks REFERENCE_ONLY, CLAUDE.md §2 Rule 3) |
| 8 | **Ratio write / naked ratio** (1×2) | Undefined risk on the extra short leg — margins like a naked short + a spread | ≥ row 1 + row 5 | [ARITH] composition; exact SPAN portfolio number [UNVERIFIED — CM-U3] |
| 9 | **Calendarized short premium** (short near / long far) | SPAN calendar-spread benefit EXCEPT on expiry day (removed) | small until expiry week; **full de-benefit on expiry day** | [WEB ICICI Direct + Zerodha Z-Connect; REPO docs/track2/TRACK2_RISK_MGMT.md:424-425] — §5.3 |
| 10 | **0DTE selling** (expiring contract, intraday) | Rows 1-5 PLUS the +2% ELM add-on (applies to shorts opened intraday on the expiring contract too) | add ≈ ₹32.5k per lot of extra ELM at S=25,000×65 (the sibling regulatory research computes ≈ ₹35-40k at Nov-2024 sizes, 2% of ~₹18L notional — same identity, different declared inputs) | [ARITH] 2% × S × lot; scope repo-stated [REPO docs/track2/TRACK2_RISK_MGMT.md:84-87] + SEBI-framework-confirmed [WEB Zerodha Z-Connect] |

**BANKNIFTY / SENSEX qualitatively:** post the SEBI ₹15-lakh minimum-contract-size rule
(2024-11-20, SEBI/HO/MRD-PoD2/CIR/P/2024/00181 as cited in-repo
[REPO brutex/options/lot_size_history.py:16] — note the sibling research flags that citation as
cosmetically drifted: the Nov-2024 change flows from CIR/2024/132, while /00181 is the Dec-2024
periodic-review methodology circular), all three index contracts sit in the same notional band,
so per-lot short-ATM margins are the same order: repo band ₹1.5–2.0L for BANKNIFTY and SENSEX
short ATM [REPO docs/track2/TRACK2_RISK_MGMT.md:112-117]. BANKNIFTY carries structurally higher
vol → higher SPAN scan range per unit notional; SENSEX (BSE/ICCL-cleared) margins come from a
different CC with the same SPAN methodology family — per-lot parity with NIFTY is an
expectation, not a verified figure [UNVERIFIED — CM-U4].

**Honesty banner for the whole table:** every rupee figure is a dated point-in-time example or a
band, NOT a model input. SPAN is recalibrated intraday and is vol-regime-dependent
[REPO docs/track2/TRACK2_RISK_MGMT.md:78-79]; the repo's own §2.3 forbids hard-coding any of
these [REPO :118-121]. These numbers exist ONLY to size the *design* (what capital class each
structure occupies), exactly how TRACK2 §11.3 scopes them ("not for use as hard values")
[REPO :542-544].

### 2.3 Premium-vs-margin yield arithmetic (credit as % of margin)

All rows are **[ARITH] identities on declared assumptions** — *credit-to-margin ratios*, i.e.
GROSS carry if everything expires worthless. They are emphatically **not expected returns** (the
seller's expected edge is a small fraction of gross credit; left-tail losses are multiples of it
— §3). Declared assumptions: illustrative spot **S = 25,000** (ILLUSTRATIVE ONLY — not a market
claim for any date); lot **65** [REPO brutex/options/lot_size_history.py:63]; margins from §2.2;
ATM weekly straddle premium ≈ 0.8·S·σ·√T (Brenner–Subrahmanyam approximation, standard result)
with annualized σ = 12% assumed for the week ([UNVERIFIED as a current-regime figure — CM-U6];
production reads realized/implied vol from the lake + the stamped `vix_at_entry`, which the
engine already carries per CLAUDE.md §2 Rule 3).

| Structure (weekly tenor) | Credit per lot [ARITH] | Margin (§2.2) | Gross credit/margin per week |
|---|---|---|---|
| ATM short straddle | ≈ 0.8·S·0.12·√(5/365) ≈ 1.12% of S → ₹280 × 65 ≈ **₹18.2k** | ₹1.5–2.0L | **≈ 9–12%** |
| ~20Δ short strangle | each leg ≈ 0.15–0.20% of S → total **₹4.9–6.5k** | ₹1.45–1.8L | **≈ 3–4.5%** |
| Iron condor (same shorts + wings) | strangle credit minus wing cost — sourced comparisons put net credit well under half the strangle's ("profit potential … less than a third of the strangle" [WEB sharpetwo / navigationtrading comparisons]) → ≈ ₹1.6–2.2k | ≈ ₹44k | **≈ 3.5–5%** — comparable ratio at ~30% of the capital, with capped tails |
| Bull put spread (width W, credit c) | c × lot | ≈ (W − c) × lot (max-loss floor) | **c/(W−c)** exactly — e.g. a 100-pt width collecting 20 pts yields 25% on max-loss; margin-based ratio [UNVERIFIED — CM-U3] |
| Naked short put ~20Δ | ≈ 0.15–0.20% of S → ₹2.4–3.3k | ₹0.85–1.1L | **≈ 2.5–3.5%** |

Two structural readings the design must carry: (i) **defined-risk structures roughly preserve
the credit/margin RATIO while dividing capital-at-risk by ~3** (the SPAN spread benefit) — why
the iron condor is the retail-capital workhorse; (ii) the ATM straddle's headline 9-12%/week
gross is the classic seller's illusion — the structure with the fattest left tail and the
strongest margin-spiral coupling (§3). Any dashboard rendering these ratios must label them
GROSS-CREDIT ratios, never returns (Golden Rule #10).

### 2.4 Portfolio-margining scope limits (what a seller CANNOT net)

- **Sourced magnitude of the hedge benefit:** unhedged NIFTY short strangle **₹1.45L** vs
  wing-completed iron condor **₹44,303** — a **69.5% reduction** [WEB Zerodha Varsity, via
  search]; a separate comparison class claims ~80% [WEB share.market / navigationtrading].
  Treat "65–80% margin relief for wing-hedged shorts" as the sourced planning band. The Varsity
  framing ("Before the new NSE margin framework, executing an iron condor was not very viable
  for retail") means the hedge benefit is a **2020-era regime feature**, not eternal — its
  historical depth inside the 2020→today window is [UNVERIFIED — CM-U7]: a 2020/2021 backtest
  assuming today's benefit may understate the margin actually demanded then.
- **No cross-underlying netting in F&O SPAN:** a short NIFTY strangle and an opposing BANKNIFTY
  position are margined as separate underlying portfolios [UNVERIFIED at circular level —
  practitioner-consensus reading; CM-U10].
- **Cash–derivatives cross-margining exists as a SEBI framework** but is clearing-member
  mediated and, for retail selling books at discount brokers, effectively not passed through;
  specific benefit percentages [UNVERIFIED — CM-U10]. Design posture: **assume zero
  cross-margin relief outside a single underlying's option/futures portfolio.**
- Position-limit ceilings are a separate binding cap layer: EOD net ₹1,500 cr / gross
  ₹10,000 cr FutEq per entity per index (framework effective 2025-10-01, delta-adjusted units)
  [REPO docs/track2/TRACK2_RISK_MGMT.md:27-45; WEB SEBI 2025/79 mirror + BusinessToday];
  per-order freeze quantities (NSE denominated in UNITS, not lots — the microstructure research
  corrects the repo draft's "lots" heading at [REPO docs/track2/TRACK2_RISK_MGMT.md:53-56];
  NIFTY 1,800 units / BANKNIFTY 900 units per the 2025-09-01 set [WEB NSE FAOP68834; Angel One];
  SENSEX 50 lots BSE-side [REPO docs/track2/TRACK2_RISK_MGMT.md:57]). Irrelevant at retail
  sizing, structural for the sizing law's upper clamp (§4 law X).

---

## 3. The drawdown-to-margin spiral

The seller's real ruin path is not one bad trade; it is the **pro-cyclical coupling of margin to
the very stress that produces the loss**. The causal machine, each edge evidenced:

```
        vol spike (index gaps / IV explodes)
              │
   ┌──────────┼──────────────────────────────┐
   ▼          ▼                              ▼
 (E1) SPAN parameters widen        (E2) short option MARKS rise
  — scan ranges are vol-driven;      — MTM debits equity daily
  files recalibrated intraday        (and intraday for RMS)
   │          │                              │
   ▼          ▼                              ▼
 (E3) margin REQUIRED per lot RISES   (E4) equity + free margin FALL
   └──────────────┬──────────────────────────┘
                  ▼
 (E5) margin shortfall → broker RMS force-reduces at the WORST prices
  — Groww auto-close at shortfall > ₹5,000 [REPO TRACK2_RISK_MGMT.md:404-407;
    not re-fetched live — CM-U12]
  — peak-margin snapshots make the shortfall itself a penalized event
    (tiered ladder below) [REPO TRACK2_RISK_MGMT.md:93-105]
  — spreads are widest exactly then (expiry-day last-hour spread
    inflation noted in-repo [REPO TRACK2_RISK_MGMT.md:426-427])
                  ▼
 (E6) forced buy-backs push marks further against the remaining book → loop
```

**The penalty ladder that arms E5** (the microstructure research CORRECTS the repo draft's
compressed "0.5% day 1-3, 5% day 4+" at [REPO docs/track2/TRACK2_RISK_MGMT.md:97-101]):

| Condition | Penalty on shortfall |
|---|---|
| Shortfall < ₹1 lakh AND < 10% of applicable margin | **0.5% per day** |
| Shortfall ≥ ₹1 lakh OR ≥ 10% of applicable margin | **1% per day** |
| Shortfall continues > 3 consecutive days | **5% per day** from the 4th day |
| > 5 shortfall days in a calendar month | **5% per day** for each further day that month |

[WEB NSE Clearing penalties page, search-captured; Zerodha Z-Connect margin-penalties article]

**Step-function amplifiers** (margin jumps with NO price move at all): the expiry-morning
**+2% ELM** on expiring shorts (2024-11-20) and the **calendar-benefit removal on expiry day**
(Feb-2025; extended to single-stock derivatives 2026-05-05 [WEB SEBI Feb-2026 circular listing;
Zerodha Z-Connect]) — §5.3. A calendarized short-premium book can wake up on expiry day owing
near-full margin on a position that was 65-80% relieved the evening before.

**Sourced magnitude anchor for E1/E3:** in March 2020, initial margin on major equity index
futures roughly **doubled in under a month** globally — CME E-mini S&P 500 IM $6,300 (Jan) →
$12,000 (Mar-23) across six successive increases; Eurex EuroStoxx-50 IM likewise doubled
[WEB FIA, "For some equity index futures, initial margin requirements have doubled in 2020"].
The **NSE-specific March-2020 multiplier is [UNVERIFIED — CM-U8]** (no dated NSE table found;
closable ONLY by the dated SPAN parameter files themselves — which is the point of §5.4).

**Price anchors inside our own backtest window** (both externally verified; both inside the
2020-01-01→today pull floor, CLAUDE.md §2 Rule 3): NIFTY **−12.98%** on 2020-03-23
[WEB Stable Investor; BusinessToday 2020-03-23] and **−5.93%** on 2024-06-04 (election-result
day) [WEB Business Standard 2024-06-04; Arthgyaan]. A short strangle sized to "comfortable"
margin at T−1 of either date meets E1–E6 simultaneously.

### 3.1 What data would let BruteX actually SIMULATE the spiral

| Needed input | What it feeds | Availability verdict |
|---|---|---|
| **Dated SPAN risk-parameter history** (per-day, ideally per-intraday-file: price/vol scan ranges, short-option minimum, spread charges, ELM/exposure rates) | E1/E3 — margin_required(t) per structure per day | NSE Clearing publishes daily files (`nsccl.<YYYYMMDD>.s.parallel.spn.gz`, member FTP path documented) [WEB NSE Clearing NSCCL-SPAN page]; **public archive retention depth for PAST dates is [UNVERIFIED — CM-U11]** — the §5.4 upgrade DEC gates on probing it |
| Broker RMS policy history (shortfall thresholds, square-off ordering, MIS/NRML multipliers over time) | E5 — WHEN and WHAT gets force-closed | Groww current policy repo-cited [REPO TRACK2_RISK_MGMT.md:404-407]; historical policy series [UNVERIFIED — CM-U12, likely unclosable] |
| Intraday option quote/spread depth at stress moments | E5/E6 exit prices | the option lake is 1-minute OHLC + OI (no depth/spread) — spread inflation is modelable only as a disclosed assumption layer [REPO docs/track2/TRACK2_RISK_MGMT.md:426-427] |
| Peak-margin snapshot times | penalty accrual | SEBI methodology = 4 random snapshots/day [REPO TRACK2_RISK_MGMT.md:104-105; WEB Paytm Money]; actual drawn times unknowable → simulate as random draws, disclosed |

### 3.2 The honest verdict (binding for this design)

**WITHOUT dated SPAN history the spiral CANNOT be simulated — only BOUNDED and ANNOTATED.**
Concretely:

- `margin_required(t)` is unknowable per day → equity-vs-margin crossing times are unknowable →
  forced-reduction P&L is unknowable. Any simulated "margin call on 2020-03-23" would be a
  fabricated number chain (Golden Rule #1).
- What IS honestly computable from OUR data: the **equity path** of the structure (option-lake
  marks) and a **stress-scan bound** (§4 law S) — so every trade/day can be ANNOTATED with
  "stress-bound loss ≥ X% of a declared capital base; margin-pressure PLAUSIBLE beyond here",
  never "margin call occurred".
- Even WITH end-of-day SPAN files, intraday recalibration
  [REPO TRACK2_RISK_MGMT.md:78-79] + broker add-ons above exchange minimums + RMS discretion
  mean broker-level FORCED-EXIT TIMING remains approximate. The upgrade ladder is therefore:
  refusal (today) → EOD-dated margin series (SPAN ingest) → bounded intraday (interpolation,
  disclosed) — never a claimed tick-accurate margin call.
- This is precisely the posture `margin_history.py` already codifies ("R47's mechanism delivered
  WITHOUT an invented number" [REPO brutex/engine/margin_history.py:19-20]); this chapter
  inherits it.

---

## 4. Position-sizing laws for the engine to sweep

The house already enumerates four sizing methods (fixed-rupee / fixed-fractional /
vol-targeting off `vix_at_entry` / fractional-Kelly-only — full Kelly REJECTED)
[REPO docs/track2/TRACK2_RISK_MGMT.md:307-316] plus a risk-rule table (per-trade 0.5-1%, daily
kill-switch 2-3%, drawdown ladder 10%/20%) [REPO :295-305]. The seller-specific layer below
slots UNDER those, answering the one question they don't: **what is "risk" for a short-premium
structure?**

| ID | Law | Formula (per trade at entry date t) | Computable from OUR data today? |
|---|---|---|---|
| **D** | **Defined-risk sizing** (verticals, condors, flies) | `max_loss_per_lot = (width − net_credit) × lot(t)`; `n_lots = floor(B_risk · equity / max_loss_per_lot)` | **YES — EXACT.** width from strikes, net_credit from the option lake's 1-minute marks at entry, lot from `get_lot_size` [REPO brutex/options/lot_size_history.py:96-123]. The engine's worst-case fill law (DEC-FILL-WORST-CASE-ONLY-001, CLAUDE.md §1) composes conservatively with it |
| **N** | **Notional cap** (undefined-risk fallback; also the cash-secured-put discipline) | `n_lots = floor(C_notional · equity / (S(t) × lot(t)))` | **YES — EXACT** (spot lake + lot table only) |
| **S** | **Stress-loss cap** (the recommended undefined-risk primary) | `m(t) = max(K · σ̂(t), m_floor)`; per structure, `stress_loss_per_lot = lot(t) × structure_loss_at(S(t)·(1−m), S(t)·(1+m)) − net_credit×lot(t)`; e.g. strangle: `lot × max(K_put − S(1−m), S(1+m) − K_call, 0)`; `n_lots = floor(B_stress · equity / stress_loss_per_lot)` | **YES for the PRICE-move arm** — σ̂(t) = trailing daily vol from the 2020→today 1-minute→1-day spot series (computable in production; NOT measured here — no lake in the research environment). `m_floor` should dominate K·σ̂: the window contains −12.98% (2020-03-23) and −5.93% (2024-06-04) [WEB §3], so `m_floor ∈ {6%, 13%}` are the two honest presets ("worst post-election print" / "worst COVID print"). **Honest limit:** an AT-EXPIRY / through-strike price bound — it does NOT bound pre-expiry MTM under an IV explosion (vega loss). For intraday-only variants, substitute worst intraday adverse excursion from the 1-minute series (also computable) |
| **H** | **Portfolio heat cap** | `Σ_open stress_or_max_loss / equity ≤ H` | YES given D/S per position — a pure fold |
| **P** | **Per-structure concentration caps** | max lots per structure class per expiry; mirrors the repo's ≤60% single-index cap [REPO TRACK2_RISK_MGMT.md:304] | YES — bookkeeping |
| **M** | **Margin-proxy cap** | `n_lots = floor(C_margin · equity / margin_proxy_per_lot(t))` | **ONLY under the V2 annotation model (§5.2)** — and then ONLY as an annotated feasibility screen, never a ranked/sized-by input under V1. REFUSED as an exact law today (no SPAN history) |
| **X** | **Structural clamps** (always-on, not swept) | `qty ≤ freeze_qty` per order leg (NSE freeze denominated in UNITS, not lots — the doc-02 correction of [REPO TRACK2_RISK_MGMT.md:51-58]; freeze values are time-varying, so a HISTORICAL backtest needs a dated freeze table, not the master's current snapshot [REPO brutex/adapters/groww/instruments.py:207]; current values post-FAOP71540 [UNVERIFIED — CM-U19]); FutEq position-limit headroom [REPO TRACK2_RISK_MGMT.md:27-45] | YES — constants + delta-adjusted fold (delta available from the lake's Greeks overlay, GREEKS_FORMULA_VERSION 2, CLAUDE.md §1 — a genuine synergy: stored per-bar delta makes FutEq computation feasible without new vendor data) |

**Honestly computable vs refused — the one-line verdicts:** D, N, S(price-arm), H, P, X are
computable today from the spot+option lake + the lot table, with zero external dependencies.
**M (margin-based sizing) and any margin-denominated ROI ranking are REFUSED** until a
SPAN-history ingest lands — the exact `margin_history.py` posture, extended from "annotate
winners" to "size positions". S's vega blindness is the disclosed gap: a stress-sized strangle
can still breach a broker margin line intraday on an IV spike without the spot ever reaching the
scan boundary — written into the law's docstring, not papered over.

**Sweepable axes proposal** (mirrors the `fib_anchor`/`PriceSource` cell-coordinate axis
pattern, DEC-NIFTY-PRICE-SOURCE-AXIS-001, CLAUDE.md §1 — default-omitted so baseline run_ids
stay byte-identical): `sizing_law ∈ {D, N, S}` × `B_risk ∈ {0.25%, 0.5%, 1%, 2%}` ×
`K ∈ {3, 4, 6}` ∪ `{m_floor=6%, m_floor=13%}` × `H ∈ {2%, 4%, 6%}`. Run-identity discipline:
the sizing law + its parameters MUST enter `canonical_params` (the blake3 run-identity formula,
CLAUDE.md §2 Rule 4) exactly like `fill_mode` does — a sizing change is a different run, never a
silent re-key.

**Where sizing does NOT belong:** the Track-1 signal engine ranks signal-only P&L (costs
modeled in the translation track; "Gross signal P&L" labeling per
DEC-ONECLICK-WINNER-DETAIL-TRUTH-001, CLAUDE.md §1). Sizing laws belong in the
TRANSLATION/EXECUTOR layer (the options-executor twin of `brutex/futures/executor.py`, which
already threads `n_lots` [REPO brutex/futures/executor.py:400,634]) — so sizing sweeps rank
NET-of-costs sized P&L in the translation artifacts, leaving the spot signal row untouched
(R13, §5.2).

---

## 5. THE MARGIN MODEL DESIGN — V1 refusal-window vs V2 conservative-bound annotation

### 5.1 V1 — the house refusal-window pattern (EXISTS TODAY)

Mechanism (all read in-session):

- `MarginRegimeRow(start, span_per_lot_paisa, source)` where `span_per_lot_paisa=None` **is** an
  explicit refusal window; "the charge-an-unverified-margin state is UNREPRESENTABLE"
  [REPO brutex/engine/margin_history.py:5-8, 86-103].
- v1 ships ONE all-refusal row per engine instrument from 2020-01-01 [REPO :152-173];
  `margin_for_date` raises `UnverifiedMarginRegimeError` for every date [REPO :199-224];
  `roi_on_margin`/`margin_roi_payload` render `status="unverified", roi_pct=None` with the
  refusal detail [REPO :243-303].
- Override paths are DATA-ONLY: a cited row or an ADR-stamped `source="ASSUMED per ADR-00XX"`
  row [REPO :20-23]. No runtime escape hatch.
- The parent precedent is the cost-regime refusal idiom: `RegimeRow` / `validate_regime_table` /
  `rate_for_date` raising `UnverifiedCostRegimeError`
  [REPO brutex/costs/constants.py:220-224, 276, 313-373], consumed by the options executor as
  the counted `COST_REGIME_UNVERIFIED` reject [REPO brutex/options/executor.py:184, 530].

**Seller extension needed (design-only):** today's table is keyed `instrument → per-lot SPAN` —
a seller needs it keyed `(instrument, structure_class)` (naked / strangle / straddle /
defined-risk-vertical / condor / calendar) because the hedge benefit (§2.4) makes per-lot margin
structure-dependent by ~3×. Same refusal semantics, wider key. Every structure-margin claim
surface then reads UNVERIFIED until dated rows land.

### 5.2 V2 — the conservative-bound ANNOTATION model (proposed)

A computable-today `margin_proxy` that is **declared ANNOTATION-ONLY** under the house
annotate-never-reranks law:

- The law, verbatim in code: "**R13 — annotation-never-reranks:** the pure spot signal row is
  NEVER mutated; the futures leg is a downstream annotation. Nothing here writes to the results
  store or influences ranking." [REPO brutex/futures/executor.py:14-16]; MAE/MFE likewise
  "annotation-only, never a ranking input" [REPO brutex/futures/executor.py:383]. Ledger form:
  "R13 annotation-never-reranks: ranking/preset order/family collapse byte-untouched — a
  cascade-FAILED rank-1 STAYS rank-1 and is flagged" [REPO docs/MIGRATION.md:7167]; second
  precedent "annotation-only — it does NOT change which combos pass the in-sample gates or the
  ranking" [REPO docs/MIGRATION.md:5198].

**Proposed exact formula** (the SPAN-shaped replica TRACK2 §2.3(b) already sanctions as
method (b), "flag … in audit log with `margin_method` field"
[REPO docs/track2/TRACK2_RISK_MGMT.md:118-121]):

```
margin_proxy_per_lot(t) =
    premium_mark(t) × lot(t)                        # net-premium component (SPAN-like)
  + lot(t) × max over i of loss_i(t)                # scan-risk component
      where loss_i = structure loss at spot shocks
      S(t)·(1 ± f_i·m(t)),  f_i ∈ {1/3, 2/3, 1},    # SPAN-like scan ladder
      m(t) = max(K·σ̂(t), m_floor)                   # σ̂ from OUR daily series;
                                                    # m_floor covers −12.98% (2020-03-23)
  + e_rate × S(t) × lot(t)                          # exposure/ELM floor arm
```

with `margin_proxy = max(scan_arm, exchange_minimum_estimate)` where
`exchange_minimum_estimate = e_rate × notional` uses a DECLARED rate. **Honesty rules on that
arm:** the microstructure research search-captured a candidate `e_rate` — index exposure margin
**3% of notional on short positions** [WEB NSE Clearing Margins page, search-captured] — but
that capture is not full-document depth and the ELM component rate remains open
[UNVERIFIED — CM-U13]; under the margin_history override law any adopted `e_rate` may only ship
as an ADR-stamped ASSUMED (or full-document-cited) value — otherwise **ship V2 with the scan
arm + premium arm alone and say so**. For defined-risk structures, additionally clamp
`margin_proxy ≤ max_loss_per_lot + net_credit×lot` (no exchange charges more than max loss plus
premium on a fully hedged vertical — [ARITH] on the defined-risk bound; the §2.2 broker
snapshots are consistent).

**V2's honesty caveats (must render beside every number):**

1. NOT the exchange number: no real PSR/VSR, no calendar charge, no intraday recalibration, no
   CC/broker add-ons. Direction of error is UNKNOWN per-day without validation — "conservative"
   is only defensible AFTER the max() floor arm and a validation pass against live
   broker-calculator snapshots on a handful of dates (a cheap operator task); until then the
   label is `margin_proxy (UNVERIFIED vs exchange)`.
2. NEVER a ranking input, never in run identity, never in P&L — R13 wording verbatim; enforced
   the way the house already enforces it (rank-order byte-untouched pins).
3. The proxy uses OUR σ̂ — it inherits the same vega blindness as sizing law S (§4) and says so.
4. The dated regime steps of §5.3 (+2% expiry ELM; calendar de-benefit; peak-margin phases) CAN
   be encoded exactly (cited percentages/dates, not SPAN outputs) — the only V2 components that
   can carry a VERIFIED tag from day one.

### 5.3 The dated margin-RULES table (encodable NOW, before any SPAN number exists)

Margin is a DATED, REGIME-STEPPED quantity exactly like the repo's cost regimes (`RegimeRow`
refusal-window tables [REPO brutex/costs/constants.py:220-373]). These rows are the natural
first rows of a future dated margin-rules table — each one row in a
DEC-ETC-REGIME-HISTORY-001-style table with its circular as the citation:

| Rule step | Effective | Scope | Evidence |
|---|---|---|---|
| Peak-margin phases 25% → 50% → 75% → **100%** of SPAN+exposure at intraday snapshots | 2020-12 → 2021-03 → 2021-06 → **2021-09-01** | all F&O | [WEB Paytm Money; Business Standard Jun-2021; Bramesh; REPO TRACK2_RISK_MGMT.md:95] |
| +2% additional ELM on short index options on the expiring contract (open at SOD **or opened intraday**) | 2024-11-20 | index options, expiry day | [REPO docs/track2/TRACK2_RISK_MGMT.md:84-87, 421-423] + [WEB Zerodha Z-Connect; Zerodha/Navia support]; primary-text scope wording [UNVERIFIED — CM-U18] |
| **Calendar-spread margin benefit REMOVED on expiry day** (near-leg expiring) — a hedged calendar's margin steps to ~full overnight | repo says 2025-02-01 [REPO docs/track2/TRACK2_RISK_MGMT.md:424-425]; implementation sources state operational effect **2025-02-10** (NSE circular CMPT66047) [WEB ICICI Direct; NSE CMPT66047]. **Date discrepancy flagged [UNVERIFIED — CM-U9]** — closable by SEBI/HO/MRD/TPD-1/P/CIR/2024/132 §text | index derivatives | both cites |
| Upfront collection of option premium from buyers (the LONG wings of spreads must be fully paid — kills wing leverage) | **2025-02-01** (verified at mirror depth) | all F&O | [WEB CSE mirror of the SEBI Oct-2024 circular (Oct_011024.pdf); taxguru; Zerodha Z-Connect] |
| Same calendar-benefit removal extended to **single-stock derivatives** on expiry day | **2026-05-05** | stock F&O | [WEB SEBI Feb-2026 circular listing (sebi.gov.in); Zerodha Z-Connect announcement] |

### 5.4 RECOMMENDATION (adopted with one refinement) + the upgrade DEC path

1. **V1 for every ROI/margin CLAIM surface** — `margin_roi` stays on the refusal channel
   exactly as shipped; the structure-keyed extension (§5.1) keeps sellers' margin-ROI columns
   reading `unverified` until dated rows exist. No invented numbers ever rank or report as
   returns.
2. **V2 ships as a clearly-labelled sibling annotation** (`margin_proxy`,
   `margin_method="stress_replica_v0"`, audit-logged per TRACK2 §2.3) — used for (i) the §4
   law-M feasibility screen (annotation: "this sized book plausibly exceeds capital X"),
   (ii) the §3 spiral BOUND annotations ("stress-bound ≥ margin_proxy headroom on date D"),
   never for ranking. It slots beside the existing `margin_roi_payload` wire shape
   [REPO brutex/engine/margin_history.py:282-303] as a second, differently-named provider — the
   refusal channel is never overridden.
3. **The upgrade DEC (proposed name: DEC-SPAN-HISTORY-INGEST-001):** ingest NSE Clearing daily
   SPAN risk-parameter files (naming convention `nsccl.<YYYYMMDD>.s.parallel.spn.gz`, sourced
   [WEB NSE Clearing NSCCL-SPAN page]; BSE/ICCL publishes an equivalent risk-parameter surface
   for SENSEX [WEB ICCL derivatives risk-parameter page]) → parse per-day parameters → populate
   dated `MarginRegimeRow`s per (instrument, structure_class) → V1's refusal windows convert to
   verified windows date-by-date, and V2 demotes to a cross-check. **Gate:** public archive
   retention depth for past dates is [UNVERIFIED — CM-U11] — the DEC's phase-0 is a probe of
   `nsearchives.nseindia.com`/member-FTP availability for 2020→today; if history is NOT
   retrievable, the honest terminal state is: **V1 refusal for the past, verified rows only
   from ingest-start forward, V2 annotation for the past — permanently labelled.**

### 5.5 Integration constraints inherited from the platform (design-binding, not proposals)

- Engine is SPOT-ONLY; every sizing/margin surface here lives in the options
  TRANSLATION/annotation layer, activation gated on a future DEC/ADR
  (DEC-ENGINE-SPOT-ONLY-001; CLAUDE.md §2 Rule 3).
- Money = int64 paisa; statistical metrics keep full precision — `roi_pct` already obeys this
  ("never rounded" [REPO brutex/engine/margin_history.py:26-27]); `margin_proxy` must too.
- Sizing params → `canonical_params` → blake3 run identity (CLAUDE.md §2 Rule 4); annotations
  (V2, spiral bounds) → NEVER in run identity (R13).
- Every refusal/annotation is a counted, typed, visible state — the `COST_REGIME_UNVERIFIED`
  skip-and-count reject and the `MarginRoiVerdict` degrade are the two templates
  [REPO brutex/options/executor.py:184,530; brutex/engine/margin_history.py:243-270].
- Regime boundaries come from DATA channels, not hardcoded dates, wherever a data channel
  exists (the registry/instrument-master backbone — lot sizes ride the master + the
  leg-economics fold, CLAUDE.md §2 DEC-GROWW-LEG-ECONOMICS-001; the hardcoded cited tables are
  cross-checks/fallbacks, per the regulatory-landscape doc's architectural conclusion).

---

## 6. Consolidated UNVERIFIED register

Everything in this doc that cannot be claimed with current data. (Deduplicated across the three
underlying research sections; sibling docs carry the wider microstructure/regulatory registers.)

| # | Item | Why unverified | What would verify it |
|---|---|---|---|
| CM-U1 | Current (2026-07) exact lot sizes 65/30/20 | code-table + FAOP70616 search-capture chain vs the repo doc's transitional 75/35/20 [REPO docs/track2/TRACK2_RISK_MGMT.md:112-117]; per-series vs day-granular transition approximation | NSE FAOP70616 / circular 176/2025 full text; NSE contract-spec page |
| CM-U2 | Naked single-leg NIFTY short margin ₹0.85–1.1L | derived from strangle figures, no standalone source | one dated broker margin-calculator snapshot (Zerodha/Samco) |
| CM-U3 | Bull put spread / ratio-write exact rupee margin per width | derived (half-condor / composition); width-dependent | broker basket-calculator snapshot |
| CM-U4 | BANKNIFTY/SENSEX per-lot margins ≈ NIFTY parity | notional-parity inference; different CC for SENSEX (ICCL) | per-index calculator snapshots |
| CM-U5 | Covered-call (futures+short-call) combined margin; index futures margin ≈ 11-13% of notional | no session source | broker calculator; NSE margin page |
| CM-U6 | σ = 12% weekly-IV assumption in §2.3 yield arithmetic | illustrative only; regime-dependent | India VIX / lake-realized vol per date (computable in production) |
| CM-U7 | Hedge-benefit percentage stability across 2020→2026 (the "65-80%" band's historical depth) | Varsity example is one dated snapshot; the NSE portfolio-margin framework changed ~2020 | dated SPAN files (the §5.4 ingest DEC) |
| CM-U8 | NSE-specific March-2020 margin multiplier | only global (CME/Eurex) doubling sourced (FIA) | dated NSE SPAN files for Feb–Apr 2020 |
| CM-U9 | Index calendar-benefit removal exact effective date (2025-02-01 repo/SEBI-text vs 2025-02-10 NSE CMPT66047 operational) | secondary sources disagree | SEBI/HO/MRD/TPD-1/P/CIR/2024/132 + NSE CMPT66047 primary text |
| CM-U10 | Cross-margin specifics (no-cross-underlying-netting at circular level; cash–F&O offset percentages) | practitioner-consensus reading only | SEBI cross-margining circulars; NSE Clearing SPAN methodology doc |
| CM-U11 | NSE SPAN file public archive retention depth (2020→today) | page names the daily file; history depth unprobed (page 403'd via the proxy) | probe nsearchives/member FTP — phase-0 of DEC-SPAN-HISTORY-INGEST-001 |
| CM-U12 | Broker RMS policy HISTORY (thresholds/timing 2020→2026); Groww ₹5,000 auto-close trigger not re-fetched live | not publicly archived; repo-draft-sourced only | groww.co risk-management policy page (current); history likely unclosable — model as a disclosed assumption layer |
| CM-U13 | The `e_rate` for V2's exposure/ELM floor arm | index 3%-of-notional exposure margin search-captured from the NSE Clearing Margins page but not full-document verified; ELM component rate open | NSE Clearing Margins page direct read / exposure+ELM rate table; else an ADR-stamped ASSUMED row |
| CM-U14 | Zerodha Varsity figures (₹1.45L / ₹44,303) read via search-result rendering, not the page | page returned HTTP 403 through the environment proxy | direct fetch of zerodha.com/varsity/chapter/iron-condor/ |
| CM-U15 | "80% margin reduction" iron-condor claim | secondary comparison articles; conflicts with the 69.5% worked example | broker basket calculator, same strikes, dated |
| CM-U16 | SPAN "16-scenario array, recalibrated 6× intraday" cadence | repo-draft-sourced [REPO docs/track2/TRACK2_RISK_MGMT.md:78-79]; 16-scenario is standard CME-SPAN but NSE page text + intraday file schedule not captured | NSE Clearing NSCCL-SPAN page direct read / SPAN methodology PDF |
| CM-U17 | Exact calendar-spread-charge rate (% per month-gap) in NSE SPAN | only the delta-based mechanism + ELM-on-⅓-far-month were captured | NSE Clearing SPAN risk-parameter page / a daily SPAN parameter file |
| CM-U18 | Primary-text scope wording of the +2% expiry-day ELM (index-only; ELM base; stock-option treatment) | circular unfetchable (403); broker implementations + repo draft agree on mechanics | SEBI CIR/2024/132 full text or an implementing NSE/CC margin circular |
| CM-U19 | Current (2026) freeze-quantity values post NSE/FAOP/71540 (eff. 2025-12-01) | last cross-verified set is 2025-09-01 (NSE) / Apr-2025 (BSE) | latest NSE FAOP + BSE quantity-freeze circulars (quarterly refresh cadence) |

**Honest-limits summary (binding):** with current data BruteX can compute EXACT defined-risk
sizing, notional sizing, and price-move stress bounds; it can BOUND but never simulate the
margin spiral; it must REFUSE margin-denominated ROI and margin-based sizing as claims. Nothing
in this doc licenses a margin number into ranking, run identity, or a return claim.

---

## 7. Sources (external, consolidated)

- Zerodha Varsity — Iron Condor chapter (₹1.45L strangle vs ₹44,303 condor; via search
  rendering, page 403 direct): https://zerodha.com/varsity/chapter/iron-condor/
- Zerodha margin calculator (F&O SPAN): https://zerodha.com/margin-calculator/SPAN/
- Samco — Short Strangle Options Strategy In India:
  https://www.samco.in/knowledge-center/articles/short-strangle-options-strategy/
- Choice India — Straddle Option Strategy: https://choiceindia.com/blog/straddle-option-strategy
- SteadyOptions / SharpeTwo / NavigationTrading — condor vs strangle margin+credit comparisons:
  https://steadyoptions.com/articles/iron-condor-vs-short-strangle-r731/ ·
  https://sharpetwo.com/blog/iron-condor-vs-short-strangle/ ·
  https://navigationtrading.com/blog/short-strangle-vs-iron-condor-which-is-better/
- share.market — Iron Condor Strategy (NIFTY):
  https://www.share.market/buzz/futures-and-options/iron-condor-strategy-explained/
- SEBI Oct-2024 circular SEBI/HO/MRD/TPD-1/P/CIR/2024/132 (page 403'd; mirrored):
  https://www.sebi.gov.in/legal/circulars/oct-2024/measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html ·
  mirror https://www.cse-india.com/upload/upload/Oct_011024.pdf ·
  https://taxguru.in/sebi/sebi-enhances-equity-index-derivatives-framework-investor-protection.html
- Zerodha Z-Connect — SEBI's new rules for index derivatives:
  https://zerodha.com/z-connect/business-updates/sebis-new-rules-for-index-derivatives-heres-whats-changing
- Zerodha support — additional ELM for index expiry:
  https://support.zerodha.com/category/trading-and-markets/margins/margin-leverage-and-product-and-order-types/articles/additional-elm-for-index-expiry ·
  Navia: https://support.navia.co.in/support/solutions/articles/1000326062
- ICICI Direct — calendar spreads after SEBI's new rules; single-stock expiry-day removal:
  https://www.icicidirect.com/futures-and-options/articles/calendar-spreads-in-f-o-after-sebi-s-new-rules-what-you-need-to-know ·
  https://www.icicidirect.com/futures-and-options/articles/removal-of-calendar-spread-margin-benefit-for-single-stock-derivatives-on-expiry-day
- NSE CMPT66047 (calendar-spread removal implementation):
  https://nsearchives.nseindia.com/content/circulars/CMPT66047.pdf
- SEBI — Review of Calendar Spread margin benefit in Single stock derivatives on expiry day
  (Feb-2026):
  https://www.sebi.gov.in/legal/circulars/feb-2026/review-of-calendar-spread-margin-benefit-in-single-stock-derivatives-on-expiry-day_99533.html ·
  Zerodha Z-Connect:
  https://zerodha.com/z-connect/updates/removal-of-calendar-spread-margin-benefit-for-single-stock-derivatives-on-expiry-day-from-may-5-2026
- NSE Clearing — NSCCL SPAN / margins / penalties:
  https://www.nseclearing.in/risk-management/equity-derivatives/nsccl-span ·
  https://www.nseclearing.in/risk-management/equity-derivatives/margins ·
  https://www.nseclearing.in/clearing-settlement/equity-derivatives/penalties ·
  https://www.nseindia.com/static/products-services/equity-derivatives-span-risk-parameter-files
- ICCL (BSE) — derivatives risk parameter files:
  https://www.icclindia.com/riskmgmt/RiskparameterNEW.aspx
- Peak margin: https://www.paytmmoney.com/blog/sebi-peak-margin-norms/ ·
  https://www.brameshtechanalysis.com/2021/06/01/all-about-phase-3-of-sebis-peak-margin-rule/ ·
  https://www.business-standard.com/article/markets/trading-volumes-likely-to-be-hit-as-75-peak-margin-kicks-in-today-121060100047_1.html ·
  Zerodha margin penalties:
  https://zerodha.com/z-connect/general/margins-margin-penalties-when-trading-with-leverage
- FIA — "For some equity index futures, initial margin requirements have doubled in 2020":
  https://www.fia.org/marketvoice/articles/some-equity-index-futures-initial-margin-requirements-have-doubled-2020
- NIFTY −12.98% on 2020-03-23: https://stableinvestor.com/2021/03/biggest-nifty-1-day-falls.html ·
  https://www.businesstoday.in/markets/market-perspective/story/share-market-live-sensex-nifty-dalal-street-stock-outlook-news-on-march-23-yes-bank-aster-dm-healthcare-welspun-torrent-power-252839-2020-03-23
- NIFTY −5.93% on 2024-06-04:
  https://www.business-standard.com/markets/news/stock-market-live-updates-june-4-sensex-nifty-lok-sabha-election-results-2024-psu-stocks-nse-124060400081_1.html ·
  https://arthgyaan.com/blog/what-does-the-election-result-day-teach-us-about-risk-in-our-portfolio.html
- Fidelity — Bull put spread mechanics:
  https://www.fidelity.com/learning-center/investment-products/options/options-strategy-guide/bull-put-spread
- Lot revisions: NSE FAOP70616 https://nsearchives.nseindia.com/content/circulars/FAOP70616.pdf
  via https://zerodha.com/marketintel/bulletin/429705/revision-in-lot-size-of-index-derivative-contracts-from-december-30-2025 ·
  https://www.angelone.in/news/market-updates/nse-revises-lot-sizes-for-index-derivatives-from-december-2025-nifty-lot-size-to-reduce-to-65
- Freeze quantities: NSE FAOP68834
  https://nsearchives.nseindia.com/content/circulars/FAOP68834.pdf ·
  https://www.angelone.in/news/market-updates/nse-changes-quantity-freeze-limits-for-index-derivatives-starting-september-1-2025
- Position limits (FutEq): SEBI 2025/79 mirror https://www.cse-india.com/upload/upload/May_292025.pdf ·
  https://www.businesstoday.in/markets/story/sebi-equity-fo-rules-futeq-method-mwpl-risk-management-478321-2025-05-29
