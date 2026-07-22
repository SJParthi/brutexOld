# 02 — Indian F&O Microstructure: the verified-facts register (2020 → 2026-07)

> **Status: RESEARCH / DESIGN-READINESS ONLY — zero engine code.** The sweep
> engine remains **SPOT-ONLY** (DEC-ENGINE-SPOT-ONLY-001) and options remain
> **REFERENCE_ONLY**; any activation of an options-strategy engine is a
> **future operator-gated DEC/ADR**.
> **Date:** 2026-07-19.
> **Provenance:** multi-agent deep-research session, docs-only branch
> `claude/options-strategies-design`; draft PR #1725
> (DEC-OPTION-SELLING-RESEARCH-001, #2210) carries this research pair; every
> repo citation is `file:line` at the branch base SHA (see
> `docs/design/options-strategies/README.md`).

This chapter merges the three microstructure research parts — (1) expiry
calendars / strike grids / lot sizes, (2) settlement / STT / exercise, (3)
margins / limits / bans / freeze / circuits — into ONE verified-facts
register a future options-strategy engine must model. Companion chapters:
[01-strategy-taxonomy.md](01-strategy-taxonomy.md) (what the strategies are),
[08-seller-blowups-and-tail-events.md](08-seller-blowups-and-tail-events.md)
(what goes wrong), [09-capital-margin-and-sizing.md](09-capital-margin-and-sizing.md)
(how the §4 margin facts become sizing math),
[10-seller-edge-and-execution-evidence.md](10-seller-edge-and-execution-evidence.md)
(whether the edge survives the §3 cost stack).

**Sourcing method (honesty preamble).** Most primary exchange/regulator hosts
(SEBI, NSE archives, NSE Clearing, Zerodha, ICICI, taxguru) **403 direct
fetches through the research proxy**. Claims below therefore rest on
web-search result captures citing those URLs, cross-checked across ≥2
independent outlets wherever possible; primary circular PDFs are linked so a
later verifier with normal egress can confirm bytes. Every dated claim
carries a source URL, a repo `path:line` (read in-session), or an explicit
**UNVERIFIED** tag resolving in the §7 consolidated register. Nothing here is
"guaranteed"; §7 is the boundary of what can honestly be claimed today.

---

## 1. The expiry-regime timeline 2020 → 2026 (THE load-bearing table)

A backtest over 2020–2026 cannot use a formula ("last Thursday") for
anything: expiry weekdays changed **nine+ times**, weeklies were culled to
one-per-exchange, lot sizes changed on a three-tier effective-date structure,
and strike schemes were revised by circular. Everything in this section is a
**dated regime window**.

Legend: "weekly day" = the weekday each weekly contract expired; "monthly
day" = the day-of-month rule for monthly (and, where offered,
quarterly/half-yearly) contracts; "lot" = index lot size in units. `—` =
product did not exist / not applicable. Holiday rule (all periods, both
exchanges): if the expiry day is a trading holiday, expiry advances to the
**previous** trading day
([Sahi 2026 schedule](https://www.sahi.com/blogs/nse-vs-bse-expiry-shake-up-what-traders)).

### 1.1 NIFTY (NSE) — the only NSE weekly survivor after Nov-2024

| Date range | Weekly day | Monthly day | Lot | Source |
|---|---|---|---|---|
| 2020-01-01 → 2021-07 expiry | Thursday (weeklies since 2019-02-11) | last Thursday | 75 | [NSE FAOP39894 via SMIFS PDF](https://www.smifs.com/files/downloads/636826435803084840_FAOP39894%2007Jan2019%20Introduction%20of%20Weekly%20Option%20Contracts%20on%20NIFTY%2050%20Index.pdf), [Business Standard 2019](https://www.business-standard.com/article/markets/introduction-of-weekly-settlement-for-nifty-contracts-a-boost-for-punters-119013100022_1.html), [NSE/FAOP/47856 mirror](https://www.rkglobal.net/upload/637601378287808566_Revised%20Market%20Lot.pdf) |
| Jul-2021 expiry → Apr-2024 | Thursday | last Thursday | 50 (monthlies from Jul-2021 expiry; weeklies from Aug-2021) | [NSE/FAOP/47856 mirror](https://www.rkglobal.net/upload/637601378287808566_Revised%20Market%20Lot.pdf), [Zerodha bulletin 291849](https://zerodha.com/marketintel/bulletin/291849/revision-in-lot-size-of-nifty-and-stock-fo-contracts) |
| contracts trading from 2024-04-26 → Nov-2024 | Thursday | last Thursday | 25 | [Motilal Oswal Apr-2024](https://www.motilaloswal.com/learning-centre/2024/4/revised-lot-size-for-nifty-fin-nifty-and-nifty-midcap-contracts-and-what-does-it-mean-for-you) |
| new contracts from 2024-11-20 → Dec-2025 | Thursday (→ 2025-08-31) | last Thursday (→ 2025-08-31) | 75 (existing weekly/monthly ran out at 25; qtr/half-yearly flipped 2024-12-26 EOD) | [NSE FAOP64625](https://nsearchives.nseindia.com/content/circulars/FAOP64625.pdf), [5paisa](https://www.5paisa.com/news/nse-revises-lot-sizes-for-key-index-derivatives-starting-november-2024) |
| 2025-09-01 → 2025-12-30 | **Tuesday** | **last Tuesday** | 75 | [SEBI CIR/2025/76](https://www.sebi.gov.in/legal/circulars/may-2025/final-settlement-day-expiry-day-for-equity-derivatives-contracts_94189.html), [Business Standard 2025-08-28](https://www.business-standard.com/markets/news/nse-bids-adieu-to-thursday-expiry-as-dates-swap-come-into-effect-explained-125082800635_1.html) |
| Jan-2026 cycle → today (2026-07-19) | Tuesday | last Tuesday | **65** (qtr/half-yearly flipped 2025-12-30 EOD) | [NSE 176/2025 = FAOP70616](https://nsearchives.nseindia.com/content/circulars/FAOP70616.pdf), [Angel One](https://www.angelone.in/news/market-updates/nse-revises-lot-sizes-for-index-derivatives-from-december-2025-nifty-lot-size-to-reduce-to-65), [Zerodha bulletin 429705](https://zerodha.com/marketintel/bulletin/429705/revision-in-lot-size-of-index-derivative-contracts-from-december-30-2025) |

### 1.2 BANKNIFTY (NSE) — weekly DEAD since 2024-11-13

| Date range | Weekly day | Monthly day | Lot | Source |
|---|---|---|---|---|
| 2020-01-01 → Jun-2023 expiry | Thursday (weeklies since 2016-05-27) | last Thursday | 25 confirmed ≥ early-2023; **2020–mid-2021 value UNVERIFIED (20 vs 25 — §7 E-1)** | [Business Standard 2016 (weekly launch)](https://www.business-standard.com/article/pti-stories/nse-to-launch-weekly-options-contracts-on-bank-nifty-index-116050501487_1.html), [Business Today 2023-03-31](https://www.businesstoday.in/amp/markets/top-story/story/nse-reduces-bank-nifty-lot-size-to-15-from-july-2023-monthly-expiry-375658-2023-03-31) |
| Jul-2023 expiry → 2023-09-01 | Thursday | last Thursday | 15 | [Business Today](https://www.businesstoday.in/amp/markets/top-story/story/nse-reduces-bank-nifty-lot-size-to-15-from-july-2023-monthly-expiry-375658-2023-03-31), [Upstox](https://upstox.com/market-talk/bank-niftys-lot-size-reduced/) |
| 2023-09-04 → 2024-02 | **Wednesday** (first Wed weekly: 2023-09-06) | last Thursday | 15 | [NSE circular 119/2023 via Zerodha bulletin 358889](https://zerodha.com/marketintel/bulletin/358889/revision-in-expiry-day-for-banknifty-weekly-options-contracts), [India Infoline](https://www.indiainfoline.com/news/markets/nse-shifts-bank-niftys-weekly-expiry-to-wednesday-effective-september-4-2023) |
| 2024-03-01 → 2024-11-13 | Wednesday | **last Wednesday** (first: 2024-03-27) | 15 → 30 (new contracts from 2024-11-20) | [Paytm Money](https://www.paytmmoney.com/blog/revision-in-expiry-day-of-nifty-bank-monthly-and-quarterly-contract-cycle/), [NSE FAOP64625](https://nsearchives.nseindia.com/content/circulars/FAOP64625.pdf) |
| 2024-11-14 → 2024-12-31 | **NONE — weekly discontinued (last: 2024-11-13)** | last Wednesday | 30 (qtr/half-yearly flipped 2024-12-24 EOD) | [Zeebiz](https://www.zeebiz.com/market-news/news-bank-nifty-weekly-expiry-ends-today-volume-future-options-turnover-volatility-nifty-bank-nifty-indices-325884), [Motilal Oswal](https://www.motilaloswal.com/learning-centre/2024/10/nse-discontinues-weekly-derivatives-on-bank-nifty-nifty-midcap-select-and-finnifty) |
| 2025-01-01 → 2025-08-31 | NONE | **last Thursday** (realigned) | 30; then 35 from the Jul-2025 expiry (new contracts from 2025-04-25) | [Bigul (Jan-2025 realignment)](https://bigul.co/blog/market-update/nse-revises-major-changes-in-banknifty-finnifty-expiry-days-effective-january-2025), [Business Standard 2025-03-31](https://www.business-standard.com/markets/capital-market-news/nse-revises-market-lot-sizes-for-nifty-bank-nifty-midcap-select-f-o-contracts-125033100440_1.html) |
| 2025-09-01 → today | NONE | **last Tuesday** | 35 → **30** (qtr/half-yearly 2025-12-30 EOD; monthlies from Jan-2026 cycle) | [SEBI CIR/2025/76](https://www.sebi.gov.in/legal/circulars/may-2025/final-settlement-day-expiry-day-for-equity-derivatives-contracts_94189.html), [NSE FAOP70616](https://nsearchives.nseindia.com/content/circulars/FAOP70616.pdf) |

### 1.3 SENSEX (BSE) — the only BSE weekly survivor

| Date range | Weekly day | Monthly day | Lot | Source |
|---|---|---|---|---|
| 2020-01-01 → 2023-05-12 | (contracts listed, near-zero liquidity; Thursday-era spec) | last Thursday | 15 (pre-relaunch) | [Business Today 2023-05-15](https://www.businesstoday.in/amp/markets/top-story/story/bse-relaunches-sensex-bankex-derivative-contracts-changes-expiry-cycle-reduces-lot-size-381394-2023-05-15) — pre-relaunch liquidity ≈ nil; see §7 E-4 |
| 2023-05-15 (relaunch) → 2024-12-31 | **Friday** | last Friday | 10 → 20 (new contracts from 2024-11-20; BSE mirrored SEBI) | [Business Today](https://www.businesstoday.in/amp/markets/top-story/story/bse-relaunches-sensex-bankex-derivative-contracts-changes-expiry-cycle-reduces-lot-size-381394-2023-05-15), [Business Standard](https://www.business-standard.com/markets/news/bse-relaunches-sensex-bankex-derivatives-contracts-to-boost-trading-123051501206_1.html), [HDFC Sec (Nov-2024 lots)](https://www.hdfcsec.com/blog/details/change-in-the-lot-size-of-index-derivative-for-weekly-monthly-quarterly-and-half-yearly-expiry-) |
| 2025-01-01 → 2025-08-31 | **Tuesday** (2025-01-03 Friday weekly kept as a one-off exception) | last Tuesday | 20 | [Angel One (BSE circular 2024-11-28)](https://www.angelone.in/news/market-updates/bse-revised-expiry-days-for-sensex-bankex-and-sensex-index-derivatives), [Bajaj Broking](https://www.bajajbroking.in/blog/bse-revises-expiry-days-for-index-derivatives-contracts-from-2025) |
| 2025-09-01 → today | **Thursday** | **last Thursday** | 20 (no later BSE revision found — §7 E-5) | [SEBI CIR/2025/76](https://www.sebi.gov.in/legal/circulars/may-2025/final-settlement-day-expiry-day-for-equity-derivatives-contracts_94189.html), [Zeebiz 2026-07-16 (live Thursday SENSEX weekly)](https://www.zeebiz.com/market-news/video-gallery-nifty-bank-nifty-prediction-16-july-2026-sensex-weekly-expiry-398915) |

### 1.4 The other index products a full-universe backtest will meet in the lake

| Instrument | Life + regime breaks | Lots | Source |
|---|---|---|---|
| FINNIFTY (NSE) | Launched 2021-01-11 with **Thursday** weekly; **Tuesday** from 2021-10-14 EOD (weekly index *futures* discontinued same date); weekly options DEAD after 2024-11-19; monthly last Tuesday → last Thursday (2025-01-01) → last Tuesday (2025-09-01) | 40 (launch) → 25 (from 2024-04-26) → 65 (2024-11-20) → 60 (Jan-2026 cycle) | [NSE launch note](https://www.nseindia.com/nse-to-launch-derivatives-on-the-nifty-financial-services-index-from-january-11-2021), [Upstox (Oct-2021 change)](https://upstox.com/announcements/risk-management/nse-update-on-finnifty-derivative-contracts/), [Motilal Oswal](https://www.motilaloswal.com/learning-centre/2024/10/nse-discontinues-weekly-derivatives-on-bank-nifty-nifty-midcap-select-and-finnifty), [FAOP70616](https://nsearchives.nseindia.com/content/circulars/FAOP70616.pdf) |
| MIDCPNIFTY (NSE) | Launched 2022-01-24, **Wednesday** weekly; **Monday** from mid-Aug-2023 (NSE circular of 2023-07-12; secondary sources give 16 vs 17 Aug cutover — §7 E-2); weekly DEAD after 2024-11-18; monthly last Monday → last Thursday (2025-01-01) → last Tuesday (2025-09-01) | 75 (launch) → 50 (2024-04-26) → 120 (2024-11-20) → 140 (Apr-2025 review, from Jul-2025 expiry) → 120 (Jan-2026 cycle) | [ICICI Direct (launch)](https://www.icicidirect.com/knowledge-center/article/nse-to-launch-futures-and-options-for-nifty-midcap-index-from-24th-jan), [Fyers (Aug-2023)](https://fyers.in/notice-board/revision-in-expiry-day-for-midcpnifty-with-effect-from-17th-august-2023/), [Zerodha bulletin 355145](https://zerodha.com/marketintel/bulletin/355145/revision-in-expiry-day-for-midcpnifty-fo-contracts), [FAOP70616](https://nsearchives.nseindia.com/content/circulars/FAOP70616.pdf) |
| NIFTYNXT50 (NSE) | Launched 2024-04-24, **monthly only** (3 serial months), last Friday; → last Thursday (2025-01-01) → last Tuesday (2025-09-01). Never had weeklies | 10 (launch) → 25 (2024-11-20), unchanged through the Jan-2026 revision | [NSE press](https://www.nseindia.com/static/resources/nse-to-launch-derivatives-on-nifty-next-50-index-niftynxt50-from-april-242024), [Zeebiz](https://www.zeebiz.com/market-news/news-nifty-next-50-futures-options-derivatives-contracts-to-be-launched-on-this-date-nse-circular-niftynxt50-latest-news-update-285074) |
| BANKEX (BSE) | Relaunched 2023-05-15, **Friday** weekly; **Monday** from 2023-10-16 (existing contracts re-dated 2023-10-13 EOD); weekly DEAD after 2024-11-18; monthly last Monday → last Tuesday (2025-01-01) → last Thursday (2025-09-01) | 20 → 15 (2023-05-15) → 30 (2024-11-20); later BSE revisions UNVERIFIED (§7 E-5) | [Business Standard](https://www.business-standard.com/markets/news/from-october-16-bankex-derivatives-expiry-date-to-shift-to-monday-123101300210_1.html), [Zerodha bulletin 362215](https://zerodha.com/marketintel/bulletin/362215/revision-in-expiry-day-for-sp-bse-bankex-fo-contracts) |
| SENSEX 50 (BSE) | Weekly discontinued after 2024-11-14 (near-zero liquidity throughout) | 60 from 2024-11-20 per BSE lot circular era; earlier UNVERIFIED | [Groww blog](https://groww.in/blog/nse-to-discontinue-weekly-derivative-contracts), [Zerodha support](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/weekly-expiries-discontinued-for-certain-index-derivatives) |
| Single-stock F&O (NSE, ~180–220 names) | **Monthly only, always** (no stock weeklies ever existed in this window); last Thursday → last Tuesday (2025-09-01). Included in the aborted Monday move (§1.6) | Per-stock lots, revised by periodic NSE reviews (e.g. the Jul-2021 review also revised stock lots) — treat as vendor data | [Groww help (weekly vs monthly)](https://groww.in/help/stocks,-f&o,-ipo-&-mtf/sx-fno/what-are-weekly-and-monthly-options--99), [Zerodha bulletin 291849](https://zerodha.com/marketintel/bulletin/291849/revision-in-lot-size-of-nifty-and-stock-fo-contracts), [Sahi](https://www.sahi.com/blogs/nse-vs-bse-expiry-shake-up-what-traders) |

### 1.5 Regime-break master list (the dates a backtest's expiry resolver must know)

2021-10-14 (FINNIFTY→Tue) · 2023-05-15 (BSE relaunch, SENSEX Fri) ·
mid-Aug-2023 (MIDCPNIFTY→Mon) · 2023-09-04 (BANKNIFTY weekly→Wed) ·
2023-10-16 (BANKEX→Mon) · 2024-03-01 (BANKNIFTY monthly→last Wed) ·
2024-11-13/14/18/19 (weekly deaths) · 2024-11-20 (SEBI lot reset +
one-weekly-per-exchange + 2% expiry ELM) · 2025-01-01 (NSE monthlies→last
Thu; BSE all→Tue) · 2025-04-25/Jul-2025 expiry (BANKNIFTY 35, MIDCPNIFTY
140) · 2025-09-01 (NSE→Tue, BSE→Thu, stocks→last Tue) ·
2025-10-03/2025-12-30/Jan-2026 cycle (lot reset NIFTY 65 etc. — Circular
176/2025 dated 2025-10-03, §4.5; qtr/half-yearly flipped 2025-12-30 EOD) ·
2026-08-03 (FUTURE: NSE F&O close 15:40 — §1.9).

### 1.6 The weekly-expiry history in prose

**Pre-SEBI proliferation (2016 → Oct 2024).** BANKNIFTY weeklies launched
2016-05-27 ([Business Standard/PTI](https://www.business-standard.com/article/pti-stories/nse-to-launch-weekly-options-contracts-on-bank-nifty-index-116050501487_1.html));
NIFTY weeklies 2019-02-11 (NSE circular FAOP39894 of 2019-01-07,
[mirror PDF](https://www.smifs.com/files/downloads/636826435803084840_FAOP39894%2007Jan2019%20Introduction%20of%20Weekly%20Option%20Contracts%20on%20NIFTY%2050%20Index.pdf)).
So the whole 2020→2026 backtest window has NIFTY + BANKNIFTY weeklies live
at its start. FINNIFTY (2021), MIDCPNIFTY (2022) and the BSE relaunch pair
(2023) spread expiries across the week until, by mid-2024, **every trading
weekday hosted at least one index 0DTE**: Mon BANKEX+MIDCPNIFTY, Tue
FINNIFTY, Wed BANKNIFTY, Thu NIFTY, Fri SENSEX. The 2023 weekday moves
(BANKNIFTY Thu→Wed, MIDCPNIFTY Wed→Mon, BANKEX Fri→Mon) were competitive
de-clashing between NSE and BSE, each announced by exchange circular with
roughly one month of notice and applied by **re-dating existing contracts
EOD** — i.e., a listed contract's expiry timestamp can change after listing
(a hard modelling fact; see §6).

**SEBI's Oct-2024 framework — the weekly cull.** SEBI circular
**SEBI/HO/MRD/TPD-1/P/CIR/2024/132, dated 2024-10-01** ("Measures to
Strengthen Equity Index Derivatives Framework…",
[SEBI page](https://www.sebi.gov.in/legal/circulars/oct-2024/measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html),
[mirror PDF](https://www.cse-india.com/upload/upload/Oct_011024.pdf))
contains six measures (full margin-side table in §4.3); the two that
reshape the calendar and contract grid:

- **Rationalization of weekly index derivatives** — each exchange may offer
  weekly-expiry contracts on **only one of its benchmark indices**,
  effective **2024-11-20**. NSE kept **NIFTY**; BSE kept **SENSEX**.
- **Recalibration of contract size** — a derivative contract must have a
  value **not less than ₹15 lakh at introduction**, with the lot size fixed
  so the contract value at review sits **within ₹15–20 lakh** (previously
  ₹5–10 lakh), effective for new contracts from **2024-11-20** (one press
  outlet renders the first affected session as Nov 21 —
  [Business Standard](https://www.business-standard.com/markets/news/new-f-o-rule-to-come-into-effect-from-thursday-nov-21-here-s-what-changes-124112000292_1.html)).
- Other measures + effective dates (per multi-outlet reporting; exact
  circular text unfetched — §7 E-3): additional **2% ELM on short options on
  expiry day** (2024-11-20); **upfront collection of option premium** from
  buyers and **removal of calendar-spread margin benefit on expiry day**
  (Feb-2025; exact governing date flagged §7 M-4); **intraday monitoring of
  position limits** (2025-04-01).
  ([Zerodha Z-Connect](https://zerodha.com/z-connect/business-updates/sebis-new-rules-for-index-derivatives-heres-whats-changing),
  [SCC Online](https://www.scconline.com/blog/post/2024/10/03/measures-to-strengthen-equity-index-derivatives-framework-legal-news/))

Last weekly expiries: **BANKNIFTY 2024-11-13 (Wed), SENSEX 50 2024-11-14,
MIDCPNIFTY 2024-11-18 (Mon), BANKEX 2024-11-18 (Mon), FINNIFTY 2024-11-19
(Tue)** ([Motilal Oswal](https://www.motilaloswal.com/learning-centre/2024/10/nse-discontinues-weekly-derivatives-on-bank-nifty-nifty-midcap-select-and-finnifty),
[Zeebiz](https://www.zeebiz.com/market-news/news-bank-nifty-weekly-expiry-ends-today-volume-future-options-turnover-volatility-nifty-bank-nifty-indices-325884),
[Zerodha support](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/weekly-expiries-discontinued-for-certain-index-derivatives)).
From 2024-11-20 the weekly-option universe is exactly **{NIFTY on NSE,
SENSEX on BSE}** — a structural break any options-strategy backtest must
treat as a scope wall, not a data gap.

**The Jan-2025 tidy-up.** With weeklies gone, both exchanges realigned
surviving monthlies effective **2025-01-01**: NSE moved BANKNIFTY (last
Wed), FINNIFTY (last Tue), MIDCPNIFTY (last Mon) and NIFTYNXT50 (last Fri)
monthlies+quarterlies to **last Thursday**
([Bigul](https://bigul.co/blog/market-update/nse-revises-major-changes-in-banknifty-finnifty-expiry-days-effective-january-2025));
BSE moved SENSEX weeklies Fri→**Tue** and all BSE monthlies to **last
Tuesday** (announced 2024-11-28;
[Angel One](https://www.angelone.in/news/market-updates/bse-revised-expiry-days-for-sensex-bankex-and-sensex-index-derivatives)).

**The aborted Monday move and the 2025 standardization.** NSE announced
moving ALL its index AND stock derivative expiries Thursday→**Monday**
effective 2025-04-04, then **deferred it indefinitely on 2025-03-28** after
SEBI's consultation paper of 2025-03-27 proposing that every equity
derivative expire only on **Tuesday or Thursday**
([Business Standard](https://www.business-standard.com/markets/news/nse-defers-switching-to-monday-expiry-after-sebi-s-consultation-paper-125032800183_1.html),
[Zerodha bulletin 407122 (withheld)](https://zerodha.com/marketintel/bulletin/407122/change-in-expiry-day-of-nse-derivative-contracts-from-april-04-2025)).
The final rule is SEBI circular **SEBI/HO/MRD/MRD-TPD-1/P/CIR/2025/76,
dated 2025-05-26** ("Final Settlement Day (Expiry Day) for Equity
Derivatives Contracts",
[SEBI page](https://www.sebi.gov.in/legal/circulars/may-2025/final-settlement-day-expiry-day-for-equity-derivatives-contracts_94189.html)):
every equity derivative (weekly, monthly, quarterly, half-yearly) expires
**only on Tuesday or Thursday**, one day per exchange. **NSE chose Tuesday;
BSE chose Thursday** — the two exchanges swapped ends of the week —
effective for contracts from **2025-09-01**, with contracts already listed
to expire on/before 2025-08-31 running out on their old days
([Kotak Neo](https://www.kotakneo.com/news/market-news/sebi-to-end-thursday-expiry/),
[5paisa](https://www.5paisa.com/news/sebi-reshuffles-equity-derivatives-expiry-days-to-streamline-trading),
[Business Standard 2025-08-28](https://www.business-standard.com/markets/news/nse-bids-adieu-to-thursday-expiry-as-dates-swap-come-into-effect-explained-125082800635_1.html)).
NSE **monthly** expiry therefore moved last-Thursday → **last Tuesday**
(stocks included); BSE monthly → **last Thursday**.

**Current regime as of 2026-07-19.** NIFTY weekly = every **Tuesday**; all
NSE monthlies (index + stock) = **last Tuesday**; SENSEX weekly = every
**Thursday**; BSE monthlies = **last Thursday**. Corroborated into July
2026 by a 2026-07-16 market piece referencing that Thursday's SENSEX weekly
expiry ([Zeebiz](https://www.zeebiz.com/market-news/video-gallery-nifty-bank-nifty-prediction-16-july-2026-sensex-weekly-expiry-398915))
and 2026 expiry-calendar pages
([Sahi](https://www.sahi.com/blogs/nse-vs-bse-expiry-shake-up-what-traders),
[fatafatniftylevels](https://fatafatniftylevels.in/holiday-expiry-2026.php)).
No 2026 change to expiry days or the weekly universe surfaced in research;
2026-H1 is tagged in §7 E-6 as "no change FOUND", not "no change".

### 1.7 Tenor availability — monthly / quarterly / half-yearly

- **NIFTY**: at any time, **4 weekly + 3 monthly + 3 quarterly
  (Mar/Jun/Sep/Dec cycle) + 8 half-yearly (Jun/Dec cycle)** option
  expiries — ≈5-year max tenure ("long-dated options"), per NSE's product
  page ([NSE NIFTY 50 F&O](https://www.nseindia.com/static/products-services/equity-derivatives-nifty50)).
  Futures: 3 serial months only. The weekly count was larger in earlier
  years (up to 7 serial weeklies circulated pre-2024) — exact per-period
  weekly-count history UNVERIFIED (§7 E-7).
- **BANKNIFTY**: monthly + quarterly + half-yearly exist — evidenced by
  NSE's own lot-transition circulars naming BANKNIFTY quarterly/half-yearly
  contracts (transition dates 2024-12-24 and 2025-12-30 EOD;
  [5paisa](https://www.5paisa.com/news/nse-revises-lot-sizes-for-key-index-derivatives-starting-november-2024),
  [FAOP70616](https://nsearchives.nseindia.com/content/circulars/FAOP70616.pdf)).
  No weeklies since Nov-2024.
- **FINNIFTY / MIDCPNIFTY / NIFTYNXT50**: 3 serial monthlies; no weeklies
  since Nov-2024 (NXT50 never had them). Whether FINNIFTY/MIDCPNIFTY list
  quarterlies is UNVERIFIED (§7 E-7) — the Jan-2026 lot circular's
  quarterly/half-yearly transition clauses were reported only for NIFTY and
  BANKNIFTY.
- **SENSEX (BSE)**: the 2023-relaunch spec listed 7 weekly + 3 monthly + 3
  quarterly + 8 semi-annual contracts
  ([Bigul spec guide](https://bigul.co/blog/indices/sp-bse-sensex-and-bankex-derivatives-a-comprehensive-guide));
  the Jan-2025 BSE circular explicitly re-dated SENSEX quarterly and
  semi-annual contracts to Tuesdays
  ([Angel One](https://www.angelone.in/news/market-updates/bse-revised-expiry-days-for-sensex-bankex-and-sensex-index-derivatives)),
  so the quarterly/semi-annual ladder exists on BSE too. Current exact
  counts UNVERIFIED (§7 E-7).
- **Single stocks**: **monthly only** — a 3-month trading cycle
  (near/mid/far); the new far-month contract enters on the trading day
  after near-month expiry. No stock weeklies at any point in 2020–2026
  ([Groww help](https://groww.in/help/stocks,-f&o,-ipo-&-mtf/sx-fno/what-are-weekly-and-monthly-options--99),
  [mstock](https://www.mstock.com/mlearn/stock-market-courses/option-basics/option-expiry-importance-of-duration-and-rollovers)).

Backtest consequence: "the weekly chain" is only ever a
NIFTY/BANKNIFTY(-until-2024)/FINNIFTY/MIDCPNIFTY/SENSEX/BANKEX phenomenon
within dated windows; long-dated (≥ quarterly) liquidity is thin and
concentrated in NIFTY — a strategy family that sells 45-DTE monthlies
(see [01-strategy-taxonomy.md](01-strategy-taxonomy.md)) has a very
different data-availability envelope than a 0DTE weekly writer.

### 1.8 Strike grids — intervals, schemes, and why the ladder must be vendor DATA

What the exchanges publish:

- **NIFTY**: 50-point interval near ATM, coarser (100-point) for far
  strikes — the long-standing base grid
  ([Upstox Uplearn](https://upstox.com/uplearn/structure-courses/basics-of-options/strike-prices/)).
  NSE revised the NIFTY strike **scheme** (interval bands + strike counts)
  effective **2025-11-17** for monthly/quarterly contracts, leaving
  0DTE/weekly contracts on the prior scheme, first new-scheme expiry
  2025-12-22 ([TeamLease RegTech summary of the NSE notice](https://teamleaseregtech.com/updates/article/49219/nse-notified-regarding-the-changes-in-the-strike-scheme-and-strike-int/)) —
  exact new band parameters UNVERIFIED (§7 E-8).
- **BANKNIFTY**: 100-point base interval; NSE revised the BANKNIFTY strike
  scheme effective **2025-01-20** (a 50-point near-ATM interval per the
  summarized circular,
  [NSE circular mirror](https://www.steelcitynettrade.com/Circulars/Revision%20in%20Scheme%20of%20Strikes%20in%20BANKNIFTY%20Options.pdf)) —
  exact scheme counts UNVERIFIED (§7 E-8).
- **Long-dated NIFTY options**: strike interval revised **1000 → 1500
  points** (Dec-2025;
  [Zerodha bulletin 437296](https://zerodha.com/marketintel/bulletin/437296/revision-of-strike-interval-in-long-dated-nifty-options)) —
  i.e. even the *far-tenor* grid moves by circular.
- **SENSEX**: 100-point interval; the 2023-era spec listed ≥41 strikes
  (20-1-20) ([Bigul spec guide](https://bigul.co/blog/indices/sp-bse-sensex-and-bankex-derivatives-a-comprehensive-guide)).
- **Stocks**: strike intervals follow a **price-banded scheme**
  (lower-priced underlyings get finer steps) published in NSE annexures and
  revised by circular; exact current band table UNVERIFIED here (§7 E-8).
- **Intraday additions**: exchanges add strikes as the underlying moves
  (the scheme defines counts around ATM which are replenished; fresh
  strikes can appear during a session). Mechanism is standard practice but
  no primary circular wording was fetchable in-session — UNVERIFIED as to
  exact trigger rules (§7 E-8).

**The honest conclusion — and BruteX already encodes it.** Between 2020 and
2026 the grid changed on at least four dated occasions (BANKNIFTY scheme
2025-01-20; NIFTY scheme 2025-11-17; long-dated interval 2025-12; plus
per-band stock revisions), intervals differ near-vs-far, and strikes are
added intraday. **The strike ladder is dynamic and circular-driven; an
engine must treat it as vendor data — never a formula.** BruteX's read side
already lives by this law: `brutex/data/moneyness.py:11-16` declares the
grid "DYNAMIC — derived from the real strike ladder of an (underlying,
expiry) … NOT hardcoded", and `derive_strike_step`
(`brutex/data/moneyness.py:106-141`) infers the step as the *minimum
positive gap* over the observed ladder, with `resolve_strike_step`
(`brutex/data/moneyness.py:181-235`) preferring the authoritative vendor
chain (the metadata-pull registry) over the sparse lake-observed ladder and
labelling confidence (`REGISTRY` vs `OBSERVED`). The vendor chain itself
comes from `groww.get_expiries → groww.get_contracts`
(`brutex/adapters/groww/contracts.py:10-14`) — expiries and strikes are
*fetched*, never derived. The same philosophy already governs futures
expiries: `brutex/data/futures_series.py:14-23` makes the vendor-parsed
lake dirs primary and demotes the computed calendar to an "OBSERVE-ONLY
cross-check".

One counter-example inside the repo proves why the formula path fails:
`brutex/options/strike_grid.py:26-30` hardcodes NIFTY 50 / BANKNIFTY 100 /
SENSEX 100 and its docstring (`brutex/options/strike_grid.py:12-13`)
asserts "Strike steps are CONSTANTS for the 5-year backfill window — no
SEBI changes to step sizes in 2021-2026". That assertion is **falsified**
by the BANKNIFTY 2025-01-20 and NIFTY 2025-11-17 scheme revisions above.
The future options-engine design should retire or demote `strike_grid.py`
constants to a fallback and standardize on the `moneyness.py`/registry
data-driven path (a doc/DEC-level recommendation only — no change proposed
here).

### 1.9 Lot-size history and the notional-band mechanism

**Why lots move: the SEBI notional band.** Lots are the exchanges' tuning
knob to keep contract notional inside a SEBI-mandated band: ₹5–10 lakh
before Nov-2024; **≥₹15 lakh at introduction / ₹15–20 lakh at review**
after SEBI CIR/2024/132
([Business Standard](https://www.business-standard.com/markets/news/new-f-o-rule-to-come-into-effect-from-thursday-nov-21-here-s-what-changes-124112000292_1.html)).
Index levels drift, so NSE runs periodic reviews (observed cadence ≈
half-yearly: Jul-2021, Mar-2023→Jul-2023, Apr-2024, Nov-2024,
Mar-2025→Jul-2025, Oct-2025→Jan-2026) and each revision has a **three-way
effective-date structure**: (i) *new* contracts introduced after the
circular date carry the new lot; (ii) existing weekly/monthly contracts run
out on the old lot; (iii) existing quarterly/half-yearly contracts are
*mutated in place* on a named EOD (e.g. 2024-12-24 BANKNIFTY / 2024-12-26
NIFTY; 2025-12-30 both)
([5paisa](https://www.5paisa.com/news/nse-revises-lot-sizes-for-key-index-derivatives-starting-november-2024),
[FAOP70616 via Angel One](https://www.angelone.in/news/market-updates/nse-revises-lot-sizes-for-index-derivatives-from-december-2025-nifty-lot-size-to-reduce-to-65)).
Consequence: **during every transition window, different expiries of the
same underlying trade with different lots simultaneously** (e.g.
Nov-2024→Jan-2025: NIFTY weeklies at 25 beside new-listed contracts at 75).
Lot is therefore a **per-contract attribute, not a per-date attribute** —
a date-keyed lookup is only an approximation (see §6).

Index lot sequences (verified anchors):

| Index | Sequence 2020 → 2026-07 | Key sources |
|---|---|---|
| NIFTY | 75 → **50** (Jul-2021 expiry; weeklies Aug-2021) → **25** (contracts trading from 2024-04-26) → **75** (new from 2024-11-20) → **65** (Jan-2026 cycle; qtr/hy 2025-12-30 EOD) | [NSE/FAOP/47856 mirror](https://www.rkglobal.net/upload/637601378287808566_Revised%20Market%20Lot.pdf), [Motilal](https://www.motilaloswal.com/learning-centre/2024/4/revised-lot-size-for-nifty-fin-nifty-and-nifty-midcap-contracts-and-what-does-it-mean-for-you), [FAOP64625](https://nsearchives.nseindia.com/content/circulars/FAOP64625.pdf), [FAOP70616](https://nsearchives.nseindia.com/content/circulars/FAOP70616.pdf) |
| BANKNIFTY | (20 or 25 — §7 E-1) → 25 → **15** (Jul-2023 monthly expiry) → **30** (2024-11-20) → **35** (new from 2025-04-25; first monthly Jul-2025) → **30** (Jan-2026 cycle; qtr/hy 2025-12-30 EOD) | [Business Today](https://www.businesstoday.in/amp/markets/top-story/story/nse-reduces-bank-nifty-lot-size-to-15-from-july-2023-monthly-expiry-375658-2023-03-31), [Business Standard 2025-03-31](https://www.business-standard.com/markets/capital-market-news/nse-revises-market-lot-sizes-for-nifty-bank-nifty-midcap-select-f-o-contracts-125033100440_1.html), [Sahi](https://www.sahi.com/blogs/nse-hikes-lot-sizes-for-bank-nifty), [FAOP70616](https://nsearchives.nseindia.com/content/circulars/FAOP70616.pdf) |
| SENSEX | 15 → **10** (2023-05-15 relaunch) → **20** (2024-11-20) → 20 (no later change found) | [Business Today](https://www.businesstoday.in/amp/markets/top-story/story/bse-relaunches-sensex-bankex-derivative-contracts-changes-expiry-cycle-reduces-lot-size-381394-2023-05-15), [HDFC Sec](https://www.hdfcsec.com/blog/details/change-in-the-lot-size-of-index-derivative-for-weekly-monthly-quarterly-and-half-yearly-expiry-) |
| FINNIFTY | 40 → **25** (2024-04-26) → **65** (2024-11-20) → **60** (Jan-2026 cycle) | [Motilal](https://www.motilaloswal.com/learning-centre/2024/4/revised-lot-size-for-nifty-fin-nifty-and-nifty-midcap-contracts-and-what-does-it-mean-for-you), [Zerodha/X](https://x.com/zerodhaonline/status/1974363648974881113) |
| MIDCPNIFTY | 75 → **50** (2024-04-26) → **120** (2024-11-20) → **140** (Jul-2025) → **120** (Jan-2026 cycle) | ditto + [Business Standard 2025-03-31](https://www.business-standard.com/markets/capital-market-news/nse-revises-market-lot-sizes-for-nifty-bank-nifty-midcap-select-f-o-contracts-125033100440_1.html) |
| NIFTYNXT50 | 10 (2024-04-24 launch) → **25** (2024-11-20) → unchanged (lot size UNVERIFIED — §7 M-8, matching §4.5) | [FAOP64625 era reports](https://groww.in/blog/nse-increases-lot-sizes-for-index-derivatives) |
| BANKEX | 20 → **15** (2023-05-15) → **30** (2024-11-20) → §7 E-5 | [Business Today](https://www.businesstoday.in/amp/markets/top-story/story/bse-relaunches-sensex-bankex-derivative-contracts-changes-expiry-cycle-reduces-lot-size-381394-2023-05-15), [Paytm Money](https://www.paytmmoney.com/blog/changes-in-lot-sizes-of-derivatives-contracts-on-nse-and-bse/) |

Stock lots: revised in the same periodic reviews (the Jul-2021 circular
title is literally "Revision in lot size of Nifty **and stock** F&O
contracts", [Zerodha bulletin 291849](https://zerodha.com/marketintel/bulletin/291849/revision-in-lot-size-of-nifty-and-stock-fo-contracts));
whether the ₹15–20L band now applies to stock derivatives or only index
derivatives is UNVERIFIED (§7 E-9) — the SEBI Oct-2024 measure text as
reported targets *index* derivatives. (The repo-side audit of BruteX's
date-keyed lot table against this history lives in §5.2.)

### 1.10 Expiry-day session shape, 0DTE reality, and the Aug-2026 change

**Session shape.** Normal market: **09:15–15:30 IST** (repo single source:
`brutex/calendars/nse.py:117` `SESSION_OPEN_IST = time(9, 15)`,
`brutex/calendars/nse.py:124` `SESSION_CLOSE_IST = time(15, 30)`). An
expiring contract trades until the 15:30 close of its expiry day — there is
no separate early cutoff. BruteX's Greeks enrichment already pins the
expiry instant to 15:30 IST (`brutex/data/greeks_enrich.py:239-240`
`EXPIRY_HOUR_IST = 15`, `EXPIRY_MINUTE_IST = 30`). The final settlement
price is NOT the 15:29:59 print — see §2.5.

**The Aug-2026 change (FUTURE; affects BruteX session constants).**
Effective **2026-08-03** (two weeks after this report's date), NSE
**extends the F&O market close 15:30 → 15:40**, with the derivatives
closing-price VWAP window moving to **15:10–15:40**, implemented alongside
the cash market's new Closing Auction Session (CAS)
([Groww blog](https://groww.in/blog/nse-extends-f-and-o-trading-hours-by-10-minutes-new-timings-effective-from-august-3-2026),
[Flattrade](https://flattrade.in/kosh/nse-extends-fo-trading-hours-till-340-pm-from-august-3-2026-what-it-means-for-traders/),
[Teji Mandi](https://tejimandi.com/blog/feature-articles/nse-extends-fo-market-closing-time-to-340-pm-from-aug-3)).
FLAG for the design: BruteX's session model (`SESSION_CLOSE_IST = 15:30`,
the 375-bar day, the 15:20 forced-exit law, `EXPIRY_HOUR/MINUTE_IST =
15:30`) will be wrong for NSE **derivatives** sessions from that date;
whether the cash index session (BruteX's spot-engine input) also changes
shape under CAS is UNVERIFIED (§7 E-11). Any options-engine DEC must make
session close per-(segment, date-range) data, not a constant.

**0DTE reality.** Post-Nov-2024 there are exactly **two index 0DTE days per
week** (NIFTY Tue on NSE, SENSEX Thu on BSE) versus five in early 2024.
Expiry-day short options carry the additional 2% ELM (SEBI CIR/2024/132,
§4.3), and expiry-day calendar-spread margin benefit is removed (Feb-2025,
§4.3) — expiry-day economics differ structurally from other days, so a
strategy engine must key margin/cost regimes off "is expiry day for this
contract", a per-contract-calendar fact. SEBI's own studies of expiry-day
concentration motivated the framework
([SEBI study report](https://taxguru.in/sebi/sebi-releases-study-equity-derivatives-trends.html)).
See [11-expiry-day-tail-study/](11-expiry-day-tail-study/) for the
empirical expiry-day tail work.

---

## 2. Settlement + exercise mechanics

### 2.1 Exercise style — EUROPEAN ONLY. The "American early-exercise" axis is REFUTED.

**Verdict (web-verified):** every exchange-traded option in the Indian
equity-derivatives market — index options AND single-stock options, NSE and
BSE — has been **European-style for the entire BruteX data window
(2020-01-01 → today)**. There is **no early exercise and therefore no
early-assignment risk anywhere in the product set.**

| Fact | Evidence |
|---|---|
| SEBI let exchanges choose option style on stocks | SEBI circular **CIR/DNPD/6/2010** (2010-10-27), "European Style Stock Options" — [sebi.gov.in](https://www.sebi.gov.in/legal/circulars/oct-2010/european-style-stock-options_6846.html) |
| NSE switched ALL stock options American → European from the **January 2011** contracts (last American expiries Nov/Dec 2010); from 2011-01-27 "no interim exercise available … all in-the-money stock options contracts would get automatically exercised on the expiry day" | [Business Standard](https://www.business-standard.com/article/markets/nse-to-offer-european-style-stock-options-from-today-111010300043_1.html), [BusinessToday](https://www.businesstoday.in/magazine/focus/story/nse-to-introduce-european-options-from-january-2011-20197-2011-02-03), [TaxGuru copy of the circular](https://taxguru.in/sebi/sebi-circular-on-european-style-stock-options-cirdnpd62010-october-27-2010.html) |
| NSE Clearing states today: "For index options contracts and options contracts on individual securities, exercise style is European style" | [NSE Clearing — Settlement Mechanism](https://www.nseindia.com/products-services/equity-derivatives-settlement-mechanism) |
| The repo already assumes this for pricing | `docs/track2/PLAN_FINALIZATION_2026-05-16.md:182` ("All 3 indexes EUROPEAN, cash-settled → BSM closed-form is EXACT (no American adjustment)") and `:213` |

**Design consequence:** a future options-strategy engine **needs NO
early-exercise/early-assignment state machine, no American-pricing
adjustment, no assignment-before-expiry event in its trade lifecycle**. The
ONLY exercise event is the expiry-day auto-exercise (§2.2). This deletes an
entire engine-design axis (per-bar assignment probability modeling, ex-ante
assignment hazard on short legs, American binomial pricing) that US-market
designs must carry.

Settlement-style split (web-verified):

- **Index options (NIFTY/BANKNIFTY/SENSEX/FINNIFTY/…): CASH-settled.**
  Exercise settlement = cash debit/credit on T+1 (T = expiry) —
  [NSE Clearing Settlement Mechanism](https://www.nseindia.com/products-services/equity-derivatives-settlement-mechanism).
- **Single-stock derivatives (futures + options): PHYSICALLY settled**
  since the October 2019 expiry. Phase-in: SEBI's framework review of
  2018-04-11 (SEBI/HO/MRD/DP/CIR/P/2018/67) started physical settlement on
  an initial stock list (46 stocks, from the July 2018 expiry — **the
  46-stock count is widely reported but the circular body was not re-read
  here: count UNVERIFIED-exact**, §7 S-1); SEBI circular of **2018-12-31**
  made it mandatory for ALL stock derivatives, phased by market-cap rank:
  bottom 50 stocks from the **April 2019** expiry, next 50 from **July
  2019**, all remaining from **October 2019** —
  [SEBI circular](https://www.sebi.gov.in/legal/circulars/dec-2018/physical-settlement-of-stock-derivatives_41482.html),
  [Zerodha Z-Connect Oct-2019 policy update](https://zerodha.com/z-connect/general/policy-on-settlement-of-compulsory-delivery-derivative-contracts-update-oct-2019),
  [TaxGuru summary](https://taxguru.in/sebi/mandatory-physical-settlement-stock-derivatives.html).
  So for the whole BruteX window (2020→) **all stock F&O is physically
  settled** — no cash-settled stock-option era exists inside the window.

### 2.2 ITM auto-exercise and the dead CTM/DNE window

Current state (post-March-2023): **every ITM option — even ₹0.05 ITM — is
automatically exercised at expiry. There is no opt-out.** History of the
opt-out (all web-verified):

| Date | Event | Source |
|---|---|---|
| 2017 (some sources say 2019 — **introduction year UNVERIFIED-exact**, §7 S-2) | NSE introduces the **"Do Not Exercise" (DNE)** facility for **CTM** strikes — motivated by the then-brutal STT-on-exercise base (full settlement value, §3.2) | [Fincash](https://www.fincash.com/l/nse-reintroduces-do-not-exercise-facility), [BusinessToday](https://www.businesstoday.in/markets/top-story/story/nse-to-scrap-do-not-exercise-dne-facility-how-can-it-affect-options-traders-374539-2023-03-23) |
| 2019-09-01 | STT-on-exercise base changed to INTRINSIC value (§3.2) — the original DNE rationale for index options disappears | [Rupeezy](https://rupeezy.in/blog/stt-calculation-changed-for-options) |
| Oct 2021 | NSE withdraws DNE | [Angel One](https://www.angelone.in/knowledge-center/futures-and-options/nse-withdraws-do-not-exercise-facility-for-brokers) |
| 2022-04-28 | DNE **reinstated** for stock options (physical-delivery risk pushback) | [Fincash](https://www.fincash.com/l/nse-reintroduces-do-not-exercise-facility) |
| **2023-03-30 expiry** | DNE **discontinued for good** (NSE Clearing circular; last usable date 2023-03-29) | [BusinessToday](https://www.businesstoday.in/markets/top-story/story/nse-to-scrap-do-not-exercise-dne-facility-how-can-it-affect-options-traders-374539-2023-03-23), [Upstox](https://upstox.com/market-talk/nse-discontinues-dne-facility/), [Outlook Business](https://www.outlookbusiness.com/news/nse-to-discontinue-do-not-exercise-dne-facility-for-stock-options-from-friday-how-should-traders-approach-the-new-setup-news-274319) |

**CTM definition** (needed if a backtest models the 2020→Mar-2023 window
faithfully): CTM = the **3 ITM strikes immediately below the final
settlement price for calls / immediately above it for puts** —
[Nirmal Bang FAQ (Annexure-1)](https://www.nirmalbang.com/Annexure-1.pdf).
Between 2020 and Mar-2023, a CTM long could be DNE'd by its broker (default
broker policies varied — e.g. Zerodha auto-DNE'd CTM stock options when
funds were short). A backtest engine that assumes universal auto-exercise
in 2020-2023 is *slightly* wrong for CTM longs whose brokers DNE'd them;
**materiality: LOW for a strategy engine that closes positions itself, and
ZERO from Apr 2023 onward.** Recommended design stance: model auto-exercise
unconditionally, and record the 2020→2023-03 CTM/DNE caveat as an
honest-limits note rather than an engine branch (§7 S-11).

### 2.3 Physical-delivery obligations per leg type (stock options only)

Web-verified via [Zerodha Varsity](https://zerodha.com/varsity/chapter/quick-note-on-physical-settlement-2/)
and [ICICI Direct](https://www.icicidirect.com/futures-and-options/articles/physical-delivery-of-futures-and-options-stock-contracts):

| Expiring position (stock underlying) | Obligation | Cash flow at strike |
|---|---|---|
| Long ITM call | **TAKE delivery** (receive shares) | pay strike × lot |
| Short ITM call (assigned) | **GIVE delivery** (deliver shares) | receive strike × lot |
| Long ITM put | **GIVE delivery** | receive strike × lot |
| Short ITM put (assigned) | **TAKE delivery** | pay strike × lot |
| Long/short futures | take/give delivery at final settlement price | — |
| Any OTM option | expires worthless, no delivery | — |

A short-seller who must GIVE delivery without holding the shares enters the
auction/short-delivery mechanics of the cash segment (auction penalty
exposure) — brokers therefore square off or demand huge margins (§2.4 ramp;
broker RMS in §4.7). **STT on the physical-delivery leg:** the delivery is
taxed like equity delivery — the repo records "Physical settlement = 0.10%
of contract value ✅ VERIFIED — stock derivs only; index F&O is
cash-settled" (`docs/track2/PLAN_FINALIZATION_2026-05-16.md:333`). The
exact buyer-vs-seller split of that 0.1% on delivery (delivery equity STT
is 0.1% on BOTH sides) is **UNVERIFIED here** (§7 S-4) — verify against the
CBDT 2018 clarification + an NSE FATAX circular before encoding.

**Netting of offsetting positions at expiry.** Verified via
[Zerodha Varsity](https://zerodha.com/varsity/chapter/quick-note-on-physical-settlement-2/)
and NSE's net-settlement guidelines
([NSE Clearing circular PDF mirror](https://www.steelcitynettrade.com/Circulars/Guidelines%20for%20Net%20Settlement%20of%20Capital%20Market%20segment%20and%20Futures%20&%20Options%20(F&O).pdf)):
delivery obligations in the SAME underlying, same expiry, are **netted at
client level** — e.g. long futures + long ITM put → take + give → net ZERO
delivery; long futures + short ITM call → netted; long ITM call + short ITM
put → **NOT offsetting** (both are TAKE — they ADD). Cross-segment (F&O
delivery vs cash-market position) netting exists as margin benefit per
NSE's Oct-2019+ net-settlement guidelines. **Design consequence:** an
options-strategy engine that models multi-leg stock-option structures held
to expiry must compute the NET delivery obligation across legs (and any
hedging future), not per-leg outcomes — a real engine axis for stock-option
strategies, and entirely ABSENT for index strategies (cash settle, per-leg
cash flows simply sum).

### 2.4 Expiry-week delivery-margin ramp (stock derivatives)

Physical-delivery margins on **long ITM stock options** (and potential
delivery positions) are levied from expiry-minus-4 days, as a percentage of
the applicable delivery margin (VaR + ELM + adhoc):

| Day (EOD; E−n = expiry minus n trading days — NOT §7 register IDs) | % of delivery margin |
|---|---|
| E−4 | **10%** |
| E−3 | **25%** |
| E−2 | **45%** |
| E−1 | **70%** |
| E (expiry) | 100% (settlement obligations) |

Sources: [Groww help — physical delivery margin](https://groww.in/help/stocks,-f&o,-ipo-&-mtf/sx-fno/what-is-groww-s-policy-on-delivery-margins-for-delivery-settlement-in-equity-derivatives--82),
[ProStocks FAQ](https://www.prostocks.com/faq/21-margin/220-how-additional-physical-delivery-margin-on-long-in-the-money-stock-options-contract-itm-works-during-friday-friday-before-expiry-thursday-to-expiry-thursday.html),
[Zerodha Varsity](https://zerodha.com/varsity/chapter/quick-note-on-physical-settlement-2/),
[NSE Clearing — Margins](https://www.nseclearing.in/risk-management/equity-derivatives/margins).
The originating NSE Clearing circular number is **UNVERIFIED-exact**
(§7 S-5). Brokers additionally impose their own stricter policies (Zerodha
blocks fresh long ITM stock-option entries in the expiry week unless fully
funded; forced square-off on margin shortfall —
[Zerodha support](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/policy-on-physical-settlement)).
**Design consequence:** a stock-option strategy backtest that HOLDS
positions into expiry week must model a capital-requirement step-function
(10/25/45/70/100%), or it will overstate deployable capital and
return-on-margin in expiry week. Index strategies are unaffected.

### 2.5 Final settlement price (FSP) — what the seller cannot see at 15:29

- **Index options**: exercise-settled at the **closing value of the
  underlying index on expiry day**, where each constituent's closing price
  is its **last-30-minutes VWAP (15:00–15:30)** — so the FSP is the index
  recomputed off constituent VWAPs, *not* the 15:29:59 spot print; settled
  in cash T+1
  ([NSE Clearing — Settlement Price](https://www.nseclearing.in/clearing-settlement/equity-derivatives/settlement-price),
  [NSE Clearing — Settlement Mechanism](https://www.nseclearing.in/clearing-settlement/equity-derivatives/settlement-mechanism),
  [OptionX explainer](https://optionx.trade/blogs/expiry-day-vwap-settlement-index-options-trading)).
  Divergence between last-traded spot and FSP is typically single-digit
  NIFTY points but can reach tens of points on volatile expiries
  (secondary-source estimate — magnitudes UNVERIFIED, §7 E-10/S-8).
- **Stock derivatives**: final settlement price = closing price of the
  underlying in the capital-market segment on expiry day (same
  last-half-hour closing construct). Same sources.

**Engine consequence:** an option held to expiry must be settled against a
**VWAP-constructed FSP series**, which is *not* any bar close in the
1-minute lake; intrinsic-at-expiry computed off the 15:29 close is an
approximation whose error is exactly the spot-vs-FSP gap. From 2026-08-03
the derivatives VWAP window moves to 15:10–15:40 (§1.10).

### 2.6 Assignment risk BEFORE expiry — n/a. Expiry-day PIN RISK — the real problem.

**Before expiry: no assignment risk exists.** European style (§2.1) means a
short option can never be assigned before the expiry-day auto-exercise. The
seller's pre-expiry risks are purely mark-to-market + margin, not
assignment.

**At expiry, the seller's irreducible information problem at 15:29:**

1. **The settle is not the last trade.** Final settlement = the expiry-day
   CLOSING value of the underlying (§2.5). At 15:29 the closing
   auction/averaging window is still completing — the seller of a strike
   within the noise band of the evolving close **cannot know** whether the
   strike finishes ITM or OTM. The spot LTP at 15:29 can sit on one side of
   the strike while the computed close lands on the other.
2. **No opt-out since Mar-2023 (§2.2):** even ₹0.05 ITM auto-exercises;
   the short is assigned mechanically.
3. **Consequence — index options:** a pinned short strike flips between
   "keep full premium" and "pay (settle − strike) × lot" on a settle move
   of one tick. Cash-settled, so the damage is bounded and linear; the
   assigned short pays no STT (§3.3). This is a P&L DISCONTINUITY at the
   strike, not a tail event.
4. **Consequence — stock options (much worse):** an assigned short call
   must GIVE delivery (§2.3) — a naked pinned short call near the close can
   create a short-delivery/auction exposure plus the full delivery-margin
   stack; brokers force-close such positions on expiry afternoon at
   degraded liquidity. Zerodha documents charging extra brokerage (₹20/lot)
   on netted physical contracts and imposing expiry-week entry blocks
   ([Zerodha support](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/policy-on-physical-settlement)).
5. **Design consequence for a future options-strategy engine:** any
   strategy family that can be short a near-settle strike at close needs
   (i) an expiry-day exit-policy axis (e.g. forced buy-back at T-minus-N
   minutes — the spot engine's 15:20 forced-exit precedent, CLAUDE.md §7
   "Track 1 forced exit 15:20 IST", generalizes), and (ii) a settle-price
   model with an honest error bar: from 1-minute bars the engine can only
   approximate the last-half-hour close construct — the residual
   settle-vs-LTP noise IS the pin-risk band and should be
   swept/stress-tested, not assumed zero. Worst-case discipline
   (DEC-FILL-WORST-CASE-ONLY-001 precedent) suggests: resolve a pinned
   short ITM at the ADVERSE plausible settle within the band. See
   [08-seller-blowups-and-tail-events.md](08-seller-blowups-and-tail-events.md)
   for the empirical tail cases.

---

## 3. STT / CTT and the levy stack — effective-date windows

### 3.1 Master STT table (all rows web-verified; effective dates are statutory)

Base and payer conventions per the
[NSE STT page](https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax):
option SALE → **seller** pays, on **premium**; option EXERCISE →
**purchaser** (the long) pays, on **intrinsic**; futures SALE → seller
pays, on **traded price × qty**.

| Window (inclusive) | Options SALE (on premium, seller) | Options EXERCISE (purchaser) | Futures SALE (on turnover, seller) | Statute / source |
|---|---|---|---|---|
| 2020-01-01 → 2023-03-31 | **0.05%** | 0.125% × intrinsic | **0.01%** | pre-FA-2023 rates — [NSE circular NSE/FATAX/56235 (2023-04-01)](https://archives.nseindia.com/content/circulars/FATAX56235.pdf) states the old→new pairs; [Wikipedia STT](https://en.wikipedia.org/wiki/Securities_Transaction_Tax), [ClearTax](https://cleartax.in/s/securities-transaction-tax-stt) |
| 2023-04-01 → 2024-09-30 | **0.0625%** | 0.125% × intrinsic | **0.0125%** | Finance Act 2023 (assent 2023-03-31, effective 2023-04-01, a 25% hike) — [NSE FATAX/56235](https://archives.nseindia.com/content/circulars/FATAX56235.pdf), [Kitco/Reuters](https://www.kitco.com/news/off-the-wire/2023-03-24/update-2-india-hikes-securities-transaction-tax-futures-options) |
| 2024-10-01 → 2026-03-31 | **0.10%** | 0.125% × intrinsic | **0.02%** | Finance (No. 2) Act 2024 (Budget Jul-2024, effective 2024-10-01) — [Zerodha Z-Connect](https://zerodha.com/z-connect/business-updates/revision-in-exchange-transaction-charges-and-securities-transaction-tax-from-october-1-2024), [ICICI Direct](https://icicidirect.com/research/equity/finace/new-stt-rules-in-futures-and-options-trading) |
| 2026-04-01 → | **0.15%** | **0.15%** × intrinsic | **0.05%** | Finance Act 2026 (Budget 2026-27, effective 2026-04-01) — [Outlook Money](https://www.outlookmoney.com/invest/stt-hike-from-april-1-2026-budget-what-it-means-for-futures-and-options-traders), [ICICI Direct](https://www.icicidirect.com/futures-and-options/articles/stt-changes-in-budget-2026-what-f-o-traders-need-to-know), [PL India](https://www.plindia.com/news/stt-hike-on-fo-in-april-2026-what-can-traders-expect/) |

Notes:

- The options-sale step 0.05% → 0.0625% is **effective 2023-04-01**
  (Finance Act 2023). Futures 0.0125% → 0.02% from 2024-10-01 is
  confirmed; note the ADDITIONAL earlier step 0.01% → 0.0125% at 2023-04-01
  that summaries often omit.
- **Exercise STT stayed 0.125% across 2020→2026-03-31** (unchanged by
  FA-2023 and FA-2024) and rose to 0.15% only from 2026-04-01 (matches the
  repo's DEC-COST-004 encoding — `brutex/costs/constants.py:82-89`).
- No STT on option BUY (premium) and none on an OTM expiry (no
  transaction) — corroborated by the repo's EXPIRY_WORTHLESS branch
  (`docs/track2/CALCULATOR_SPEC.md:380-387`).
- **CTT** (Commodity Transaction Tax) applies only to commodity derivatives
  (MCX etc.) — **out of BruteX scope**: the COMMODITY segment is excluded
  from ingest (CLAUDE.md §6 "Backtesting segments" row;
  `brutex/adapters/groww/constants.py:91-95` per that table — segment scope
  verified there). CTT rates are therefore NOT tabled here (UNVERIFIED,
  irrelevant to the design — §7 S-9).

### 3.2 The intrinsic-value pivot of 2019-09-01 (why the pre-2019 horror stories don't apply)

Until 2019-08-31, exercised options were taxed **0.125% on the FULL
settlement value** (settlement price × lot) — producing the infamous cases
where a profitable ITM long expired into a net loss. Finance (No. 2) Act
2019 changed the base to **intrinsic value = settlement price − strike**,
effective **2019-09-01** —
[Rupeezy](https://rupeezy.in/blog/stt-calculation-changed-for-options),
[TradeSmart](https://tradesmartonline.in/help/trading/how-is-the-stt-computed-in-case-an-in-the-money-option-contract-expires/),
[Zerodha "STT Trap"](https://zerodha.com/z-connect/trending/stt-options-nse-bse-mcx-sx).
**The entire BruteX data window (2020→) is post-pivot**, so an options
engine only ever needs the intrinsic basis. The old full-settlement basis
matters ONLY as history (it explains why DNE existed, §2.2). Pre-2020 STT
regimes (e.g. the option-sale 0.017% era) are out of window (data floor
2020-01-01, `GROWW_HISTORY_FLOOR`) — §7 S-10.

### 3.3 The worked example — hold-to-expiry (exercise) vs sell at 15:29

Deep-ITM NIFTY call, strike **24,000**, final settlement **25,000**, lot
**75** (NIFTY lot 75 was in force 2024-11-20 → 2025-12-31 —
`brutex/options/lot_size_history.py:58-63`), trade dated in the
**2024-10-01 → 2026-03-31 regime** (sale 0.10%, exercise 0.125%). At 15:29
on expiry a zero-time-value deep-ITM call trades ≈ intrinsic, premium ≈
₹1,000. Intrinsic value = 1,000 × 75 = **₹75,000**.

| Route | STT computation | STT |
|---|---|---|
| **A. Hold → auto-exercise** | 0.125% × ₹75,000 (intrinsic) | **₹93.75** (books as ₹94, nearest-rupee per STT Rules Rule 11 — `docs/track2/COSTS_VERIFIED.md:44`) |
| **B. Sell at 15:29 @ ₹1,000** | 0.10% × ₹75,000 (premium, seller pays) | **₹75.00** |
| **STT difference** | | **₹18.75/lot in favor of selling** (≈ ₹19 on rounded books) |

Full-friction honesty — STT is NOT the whole story. Selling adds
trade-level charges the exercise route may not carry:

| Component | Route B (sell) | Route A (exercise) |
|---|---|---|
| STT | 75.00 | 93.75 → 94 |
| NSE ETC 0.03503% × 75,000 | 26.27 | **model-dependent** (see below) |
| SEBI 0.0001% | 0.08 | " |
| IPFT 0.0005% | 0.38 | " |
| Brokerage (Groww ₹20/order) | 20.00 | 20.00 (`docs/track2/CALCULATOR_SPEC.md:355,384` — ₹20 fires even on expiry events) |
| GST 18% on (brokerage+ETC+SEBI+IPFT) | ≈ 8.41 | ≈ 3.60 (brokerage-only base) if no trade charges |
| **Total** | **≈ ₹130** | **≈ ₹118** (no-trade-charge model) / **≈ ₹150** (repo's illustrative model) |

The repo's illustrative EXPIRY_EXERCISE branch charges ETC/SEBI/IPFT **on
intrinsic** (`docs/track2/CALCULATOR_SPEC.md:356-358`, explicitly marked
illustrative-not-normative at `docs/track2/CALCULATOR_SPEC.md:347-352`);
whether NSE actually levies transaction charges on exercised (non-traded)
contracts is **UNVERIFIED** (§7 S-3) — under "no trade charges on
exercise," **exercising is ≈ ₹13 CHEAPER overall** despite the higher STT;
under the repo's model, selling is ≈ ₹19 cheaper. Either way the deltas are
tens of rupees per ₹75,000 of intrinsic (≈ 2-4 bp) — **the post-2019
asymmetry is small and spread-dominated** (a deep-ITM 15:29 sale usually
crosses a wide bid-ask, easily > ₹18.75/lot of edge). Pre-2019
counterfactual for contrast: 0.125% × full settlement value (25,000 × 75 =
₹18,75,000) = **₹2,343.75** vs ₹37.50 sale STT at 0.05% — a ₹2,306 gap;
that world is gone.

**Regime flip from 2026-04-01:** sale 0.15% on premium vs exercise 0.15% on
intrinsic — for a zero-time-value deep-ITM the STT asymmetry **vanishes**
(both 0.15% × ≈ same base; e.g. lot 65: ₹97.50 either way), and since
selling still pays ETC/SEBI/IPFT + spread, **holding to expiry becomes the
cheaper exit for deep-ITM index options under the current regime** (subject
to the UNVERIFIED exercise-charge question, §7 S-3). An engine sweeping
"exit at 15:29 vs hold to settle" will find the answer is REGIME-DEPENDENT
— the exit-policy axis must be date-regime-aware.

**Option-WRITING corollary:** the writer pays sale-STT at ENTRY (the
opening leg IS the sale) — the repo's DEC-COST-002 entry-date regime keying
(`brutex/costs/constants.py:486-504`) is exactly right for writers. An
assigned short pays **NO STT at expiry** (exercise STT is purchaser-side;
`docs/track2/CALCULATOR_SPEC.md:363-378` models this correctly: `stt = 0`,
intrinsic loss is a capital item, not a charge). A short expiring worthless
pays no further STT but — Groww-specific — still pays ₹20 brokerage + GST
on the expiry event (`docs/track2/CALCULATOR_SPEC.md:390-397`).

### 3.4 Other levies + brokerage conventions (external cross-check of the repo rows)

| Levy | Rate (options) | Repo encoding | External cross-check |
|---|---|---|---|
| NSE exchange transaction charge (ETC) | 0.03503% of premium (₹35.03/lakh), both sides, eff. 2024-10-01 | `brutex/costs/constants.py:150-155` | [NSE circular FA64232 (PDF)](https://nsearchives.nseindia.com/content/circulars/FA64232.pdf), [Zerodha Z-Connect](https://zerodha.com/z-connect/business-updates/revision-in-exchange-transaction-charges-and-securities-transaction-tax-from-october-1-2024), [Bajaj Broking](https://www.bajajbroking.in/blog/nse-and-bse-revise-transaction-charges-from-october-2024) — CONFIRMED |
| BSE exchange charge (Sensex/Bankex options) | 0.0325% of premium (₹3,250/cr), eff. 2024-10-01 | `brutex/costs/constants.py:157-162` | same sources — CONFIRMED |
| SEBI turnover fee | 0.0001% (₹10/cr) both sides | `brutex/costs/constants.py:173-175` | [NSE STT/charges pages](https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax); `docs/track2/COSTS_VERIFIED.md:33` — CONFIRMED (secondary) |
| Stamp duty | 0.003% of premium, **buy side only**, uniform India since 2020-07-01 | `brutex/costs/constants.py:195-198` | `docs/track2/COSTS_VERIFIED.md:35,540` (Indian Stamp Act 2020 / PIB 1635399) — CONFIRMED (repo-verified; not re-fetched) |
| NSE IPFT | 0.0005% of premium both sides | `brutex/costs/constants.py:182-184` | ⚠ **forward alert**: NSE FA73061 (eff. 2026-03-01) reportedly re-split txn/IPFT to ₹3,552 + ₹1/cr, TOTAL unchanged 0.03553% — UNVERIFIED, deliberately not encoded (`brutex/costs/constants.py:139-148`; `docs/track2/COSTS_VERIFIED.md:736`) — §7 S-6 |
| BSE IPFT | 0 (assumed) | `brutex/costs/constants.py:186-188` | **UNVERIFIED-assumed-0** (`docs/track2/COSTS_VERIFIED.md:273`) — §7 S-7 |
| GST | 18% on (brokerage + ETC + SEBI + IPFT); never on STT/stamp | `brutex/costs/constants.py:205-211`; `docs/track2/COSTS_VERIFIED.md:38` | CONFIRMED (Section 170 CGST rounding per DEC-COST-001) |
| Brokerage — options | Groww/Zerodha flat **₹20 per executed ORDER** (never per lot/contract; ₹20 fires even on worthless expiry at Groww — Zerodha dropped that in Jun-2024) | `brutex/costs/constants.py:45-53`; `docs/track2/CALCULATOR_SPEC.md:390-419` | CONFIRMED (repo's 4-agent research; 100-lot exercise = ₹20, not ₹2,000 — `docs/track2/CALCULATOR_SPEC.md:407-419`) |
| Brokerage — futures | Groww **min(₹20, 0.03% × turnover)** per order | `brutex/costs/futures_costs.py:23-26,192` | repo-verified (COSTS_VERIFIED §8.1 per `futures_costs.py` header) |
| Rounding law | statutory items nearest-rupee; repo applies adverse CEIL everywhere post-DEC-FILL-WORST-CASE-ONLY-001 | `docs/track2/COSTS_VERIFIED.md:106-133` | worked oracles enforced by `tests/golden/test_costs_verified_examples.py` |

---

## 4. Margins · position limits · bans · freeze quantities · circuits

The repo's own prior research on this domain is
`docs/track2/TRACK2_RISK_MGMT.md` — a **DRAFT (2026-05-16)** that itself
says "Verify primary-source citations marked UNVERIFIED before code-lock"
(`docs/track2/TRACK2_RISK_MGMT.md:3-4`); where this section confirms,
corrects, or contradicts that draft it says so explicitly.

### 4.1 The margin stack for option WRITERS (what a short option must post)

| # | Component | Current parameter | Source |
|---|---|---|---|
| 1 | **SPAN (Initial) margin** | Portfolio scan-risk over the standard SPAN scenario array (worst loss across price/vol shock scenarios); NSE Clearing publishes risk-parameter files intraday and recomputes member/client margins from them | NSE Clearing NSCCL-SPAN page <https://www.nseclearing.in/risk-management/equity-derivatives/nsccl-span>; repo prior-research: "16 scenario array, recalibrated 6× intraday" at `docs/track2/TRACK2_RISK_MGMT.md:78-79` (16-scenario count + 6×/day cadence NOT independently re-verified this session — §7 M-1) |
| 2 | **Price Scan Range (PSR) — index products** | 6 standard deviations **scaled up by √2** (2-day margin period of risk), subject to a floor of **9.3% of underlying price** | NSE Clearing NSCCL-SPAN parameters (search-captured from <https://www.nseclearing.in/risk-management/equity-derivatives/nsccl-span>) |
| 3 | **PSR — single-stock products** | 6σ scaled by √2, floor **14.2%** of underlying price | same as above |
| 4 | **Exposure margin — index F&O** | **3% of notional** (futures: on the position; options: charged on SHORT positions only, 3% of notional of open short positions) | NSE Clearing Margins page (search-captured from <https://www.nseclearing.in/risk-management/equity-derivatives/margins>). **Corrects the commonly-quoted "~2%"** — the currently-published exchange minimum is 3%; it also corrects `docs/track2/TRACK2_RISK_MGMT.md:80-81` which says "typically 3-5%" |
| 5 | **Exposure margin — stock F&O** | **higher of 5% or 1.5σ** of notional of gross open futures positions + gross SHORT option positions | same NSE Clearing Margins page. **Corrects the commonly-quoted "~3.5%"** |
| 6 | **Extreme Loss Margin (ELM)** | Separate add-on per instrument set by the clearing corporation; for futures calendar spreads ELM is levied on **1/3 of the far-month contract value** | NSE Clearing Margins page (search-captured); repo `docs/track2/TRACK2_RISK_MGMT.md:82-83` |
| 7 | **Expiry-day additional ELM** | **+2% of contract value (2% × strike × lot)** on ALL short index option contracts of the expiring series — both those open at start-of-day AND those opened intraday on expiry day; applies even if hedged | Effective **2024-11-20** per the SEBI Oct-2024 circular; broker implementations: Zerodha support <https://support.zerodha.com/category/trading-and-markets/margins/margin-leverage-and-product-and-order-types/articles/additional-elm-for-index-expiry>, Navia <https://support.navia.co.in/support/solutions/articles/1000326062>; repo `docs/track2/TRACK2_RISK_MGMT.md:84-87` agrees |
| 8 | **Net Option Value (NOV)** | Only SELL option positions are margined; long-option value is credited as an offset in portfolio margining ("in offset scenarios only sell positions are margined and offsetting benefits for buy positions are given") | NSE Clearing Margins page (search-captured) |
| 9 | **MTM** | Daily cash settlement; not a margin component but consumes free collateral headroom | repo `docs/track2/TRACK2_RISK_MGMT.md:88-89` (standard mechanism; not separately web-verified — §7 M-2) |
| 10 | **Peak margin** | Margin must be maintained INTRADAY, not just EOD — clearing corps take **4 random intraday snapshots**; the max across snapshots is the day's peak margin obligation | SEBI peak-margin framework (SEBI/HO/MRD/DRMNP/CIR/P/2020/235 per `docs/track2/TRACK2_RISK_MGMT.md:95,556`); snapshot mechanism confirmed via <https://www.paytmmoney.com/blog/sebi-peak-margin-norms/> and <https://www.brameshtechanalysis.com/2021/06/01/all-about-phase-3-of-sebis-peak-margin-rule/> |

**Long options = premium only.** A long option costs its full premium and
attracts no SPAN/exposure margin (it is an asset, not an obligation); since
**2025-02-01** the premium must be collected **upfront** from option buyers
by trading/clearing members (SEBI Oct-2024 circular measure; "TMs to
collect Initial Margin (IM) and Extreme Loss Margin (ELM) upfront from
February 01, 2025" + upfront option-premium collection — search-captured
from the SEBI circular mirror
<https://www.cse-india.com/upload/upload/Oct_011024.pdf> and
<https://taxguru.in/sebi/sebi-enhances-equity-index-derivatives-framework-investor-protection.html>).
Before that date brokers commonly funded intraday premium via leverage
products; from Feb-2025 the buyer pays full premium at order time —
**verified, effective 2025-02-01**.

**Spread margin benefits (verticals / calendars).**

- **Portfolio offsets are delta-based inside SPAN**: "The margin on
  calendar spread shall be calculated on the basis of delta of the
  portfolio consisting of futures and options contracts in each month … a
  near month option with a delta of 100 and a far month option with –100
  bears a spread charge equivalent to the calendar spread charge for long
  100 near-month / short 100 far-month futures" (NSE Clearing Margins page,
  search-captured). So verticals (same expiry) net inside the scan-risk
  array; calendars net across months but pay a **Calendar Spread Charge**
  per spread formed.
- Calendar spread treatment is granted **until expiry of the near-month
  contract** (NSE Clearing Margins page, search-captured) — and since
  **Feb-2025, NOT on the expiry day itself** (§4.3).
- The exact calendar-spread-charge percentage per month-gap was NOT pinned
  this session — UNVERIFIED (§7 M-3); closable from the NSE Clearing SPAN
  risk-parameter page / a daily SPAN parameter file.
- Practical writer consequence: margin for a short strangle/straddle is far
  below 2× naked-leg margin only to the extent SPAN's scenario array nets
  them; a defined-risk vertical's margin approaches max-loss only under
  broker-side (not exchange-mandated) treatment — quantitative benefit
  magnitudes are portfolio- and day-specific and can ONLY be reproduced
  from the day's SPAN parameter file (exactly why the repo forbids
  hard-coded margin constants — `docs/track2/TRACK2_RISK_MGMT.md:109-121`).
  Sizing consequences live in
  [09-capital-margin-and-sizing.md](09-capital-margin-and-sizing.md).

### 4.2 Peak-margin regime history (2020-2021) — verified

| Phase | Window | Peak-margin requirement (% of SPAN+exposure at snapshot) |
|---|---|---|
| 1 | Dec 2020 – Feb 2021 | 25% |
| 2 | Mar 2021 – May 2021 | 50% |
| 3 | Jun 2021 – Aug 2021 | 75% |
| 4 | **Sep 1, 2021 onward** | **100%** |

4 random system-driven intraday snapshots; the highest snapshot margin is
the peak obligation. Sources:
<https://www.paytmmoney.com/blog/sebi-peak-margin-norms/>,
<https://www.business-standard.com/article/markets/trading-volumes-likely-to-be-hit-as-75-peak-margin-kicks-in-today-121060100047_1.html>,
<https://www.brameshtechanalysis.com/2021/06/01/all-about-phase-3-of-sebis-peak-margin-rule/>.
Backtest-history implication: any historical simulation of writer capital
efficiency **before Sep-2021 lives under a different intraday margin
regime** than after; the repo's data window starts 2020-01-01
(`brutex/engine/margin_history.py:152`), which straddles all four phases.

### 4.3 The SEBI Oct-2024 six-measure framework (SEBI/HO/MRD/TPD-1/P/CIR/2024/132, 2024-10-01) — margin view

Circular landing page:
<https://www.sebi.gov.in/legal/circulars/oct-2024/measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html>
(host 403s the proxy; content verified via the CSE mirror PDF + taxguru +
broker summaries — §7 E-3).

| # | Measure | Effective date | Writer relevance |
|---|---|---|---|
| 1 | **Rationalization of weekly index products** — ONE weekly-expiry index per exchange (NSE: NIFTY weekly; BSE: SENSEX weekly; BANKNIFTY/FINNIFTY/MIDCPNIFTY/NIFTYNXT50 weeklies discontinued) | **2024-11-20** | Shrinks the weekly premium-selling universe to 2 indices; a backtest spanning the boundary must drop discontinued weekly chains (repo agrees: `docs/track2/TRACK2_RISK_MGMT.md:428-438`) |
| 2 | **Minimum contract size raised to ₹15 lakh** (lot sizes re-fixed so contract value ≈ ₹15-20 lakh; NSE implementation circular FAOP64625) | **2024-11-20** (new lot sizes) | ~3× larger minimum ticket per short lot; capital-per-lot discontinuity in any lot-denominated history |
| 3 | **Additional 2% ELM on expiry-day short index options** (open at SOD or opened intraday on the expiring series) | **2024-11-20** | Direct margin add-on to the most popular writing day; see §4.1 row 7 |
| 4 | **Upfront collection of option premium from buyers** (IM + ELM + premium upfront) | **2025-02-01** | Removes buyer-side intraday leverage; indirectly affects expiry-day flow the writer trades against |
| 5 | **Removal of calendar-spread margin benefit ON EXPIRY DAY** (positions in the expiring series get no cross-expiry offset that day) | SEBI circular text targets **Feb-2025**; most implementation sources state operational effectivity **2025-02-10** (NSE circular CMPT66047 <https://nsearchives.nseindia.com/content/circulars/CMPT66047.pdf>, Kotak/ICICI summaries); repo draft says 2025-02-01 (`docs/track2/TRACK2_RISK_MGMT.md:424-425`) — **exact governing date flagged, §7 M-4** | Expiry-day calendar shorts must post full naked margin that day — a jump in capital use for calendar writers |
| 6 | **Intraday monitoring of position limits** for index derivatives | SEBI initially targeted **2025-04-01**; the Mar-2025 circular (<https://www.sebi.gov.in/legal/circulars/mar-2025/intraday-monitoring-of-position-limits-for-index-derivatives_93123.html>) confirmed monitoring from Apr 1 2025 with ≥4 random daily snapshots and **no penalties until further directions** (<https://www.businesstoday.in/markets/top-story/story/sebi-sets-new-guidelines-for-intraday-index-derivative-monitoring-from-april-1-delays-penalties-469885-2025-03-28>); superseded/operationalized by the Sep-2025 framework effective **2025-10-01** (<https://www.sebi.gov.in/legal/circulars/sep-2025/framework-for-intraday-position-limits-monitoring-for-equity-index-derivatives_96376.html>) | Irrelevant at retail backtest sizes but defines the *regime* a realistic large-book simulation must respect |

**Post-framework extension (writer-relevant):** the calendar-spread
expiry-day benefit removal is being extended to **single-stock derivatives
from 2026-05-05** (SEBI circular dated 2026-02-05 per Zerodha Z-Connect
<https://zerodha.com/z-connect/updates/removal-of-calendar-spread-margin-benefit-for-single-stock-derivatives-on-expiry-day-from-may-5-2026>).

### 4.4 Position limits

**The 2025 delta-based (FutEq) rework — SEBI/HO/MRD/TPD-1/P/CIR/2025/79
(2025-05-29).** Mirror:
<https://www.cse-india.com/upload/upload/May_292025.pdf> (host 403s;
content via search captures of the circular +
<https://www.businesstoday.in/markets/story/sebi-equity-fo-rules-futeq-method-mwpl-risk-management-478321-2025-05-29>
+ ICICI Direct/K&K summaries).

| Limit (per entity, PER INDEX) | Value | Notes / effective date |
|---|---|---|
| Index OPTIONS — net end-of-day FutEq OI | **₹1,500 crore** | Delta-adjusted ("Futures-Equivalent") net position — multi-capture-confirmed |
| Index OPTIONS — gross FutEq OI (long side; short side separately) | **₹10,000 crore** | Neither gross long nor gross short may exceed it |
| Index OPTIONS — intraday | net ₹5,000 cr / gross ₹10,000 cr FutEq | Repo draft carries these intraday numbers (`docs/track2/TRACK2_RISK_MGMT.md:39-40`); web capture confirms the EOD pair directly, the intraday pair via the repo draft + Sep-2025 framework — §7 M-5 for the intraday pair's primary quote |
| Index FUTURES | category-specific; e.g. higher of **15% of futures OI or ₹500 crore** for institutional categories | Search-captured; per-category table needs the primary circular (§7 M-5) |
| Glide path | Exchanges notify breaching entities from **2025-07-01**; monitoring/cure regime runs to **2025-12-05**; new MWPL definitions + FutEq metric effective **2025-10-01** | Search-captured from circular text + BusinessToday |
| Monitoring snapshots | ≥4 random intraday snapshots/day (Sep-2025 framework, effective 2025-10-01); repo draft adds "one between 14:45-15:30 IST" + "15-minute cure period" (`docs/track2/TRACK2_RISK_MGMT.md:41-42`) — that finer detail is repo-draft-sourced, not re-verified (§7 M-5) | |

The **unit change is the design-relevant fact**: from 2025-10-01
position-limit compliance is measured in delta-adjusted futures-equivalent
terms, not notional (repo draft encodes exactly this switch as date-gated,
`docs/track2/TRACK2_RISK_MGMT.md:44-47,492-494`, DEC-RISK-009 at
`docs/track2/TRACK2_RISK_MGMT.md:522`).

**Pre-2025 limits (what the old world was).**

- Index options, TM / FPI (Cat I & II) / MF level: **higher of ₹500 crore
  or 15% of total market OI in index option contracts** (NSE
  position-limits page, search-captured from
  <https://www.nseclearing.in/risk-management/equity-derivatives/position-limits>).
- Single-stock derivatives, client/sub-account level: gross open position
  across all F&O on one underlying ≤ **higher of 1% of free-float market
  cap (shares) or 5% of market OI** in that underlying's derivatives (same
  source family, search-captured).
- Trading-member proprietary limit in a stock's derivatives: **20% of that
  stock's MWPL** (NSE sec-ban/position-limit pages, search-captured from
  <https://www.nseindia.com/static/products-services/equity-derivatives-risk-management-sec-ban>).
- Retail-client-level index-derivatives limits before 2025 were
  disclosure-oriented rather than hard rupee caps — the precise pre-2025
  client-level index rule text was NOT pinned this session (§7 M-6).

**MWPL + the BAN period (single stocks ONLY).**

| Rule | Value | Source |
|---|---|---|
| MWPL definition (classic) | **lower of** 30× average daily traded shares (preceding month) **or** 20% of non-promoter (free-float) shares | NSE sec-ban page (search-captured); <https://www.mastertrust.co.in/blog/mwpl-open-position-limit-ban-period> |
| Ban ENTRY | aggregate OI across ALL exchanges in a scrip's F&O > **95% of MWPL** → scrip enters ban from next day | NSE sec-ban page (search-captured) |
| During ban | **only position-DECREASING trades** (offsets); no new positions, no increases | same |
| Ban EXIT | normal trading resumes when aggregate OI falls to **≤ 80% of MWPL** | same |
| Violation penalty | **1% of the value of the increased position, min ₹5,000, max ₹1,00,000** | NSE violations page (search-captured from <https://www.nseindia.com/products-services/equity-derivatives-violations>) |
| Indices | **NO ban period** — MWPL applies to single-stock derivatives only; NIFTY/BANKNIFTY/SENSEX F&O are exempt from the ban mechanism | search-captured (multiple broker/NSE sources); repo draft agrees: `docs/track2/TRACK2_RISK_MGMT.md:64-68` — CONFIRMED |
| 2025 change | the May-2025 circular ALSO redefines stock MWPL in FutEq/delta terms effective 2025-10-01 (BusinessToday: "tweaks ban rules"); exact new MWPL formula NOT pinned (§7 M-7) | <https://www.businesstoday.in/markets/story/sebi-equity-fo-rules-futeq-method-mwpl-risk-management-478321-2025-05-29> |

Backtest consequence for stock-option writing: a strategy holding short
stock options through a ban entry cannot legally ADD; it can only reduce —
and the ban calendar is a real, dated, per-underlying historical dataset
(NSE publishes the daily ban list) that BruteX holds **nowhere today**
(§5.1).

### 4.5 Freeze quantities (max single-order size)

Exchange facts:

- NSE freeze limits are denominated in **UNITS (contracts), not lots**, and
  are revised periodically by circular (NSE FAOP68834
  <https://nsearchives.nseindia.com/content/circulars/FAOP68834.pdf>;
  revisions observed 2025-05-02, 2025-09-01, 2025-11-03, 2025-12-01 —
  Zerodha bulletins 412395 & 432323, Angel One, Goodreturns/NSE FAOP71540).
- Last cross-verified value set (effective **2025-09-01**, per
  <https://www.angelone.in/news/market-updates/nse-changes-quantity-freeze-limits-for-index-derivatives-starting-september-1-2025>):

| Index | Freeze qty (units) | Lots at then-current lot size |
|---|---|---|
| NIFTY | **1,800** | 24 lots @ lot 75 |
| BANKNIFTY | **900** (raised from 600) | ~25 lots @ lot 35 |
| FINNIFTY | 1,800 | ~27 lots @ lot 65 |
| MIDCPNIFTY | 2,800 | 20 lots @ lot 140 |
| NIFTYNXT50 | 600 | 24 lots @ lot 25 (lot size UNVERIFIED — §7 M-8) |

- **Historical NIFTY 1,800 CONFIRMED**: 1,800 units has been the NIFTY
  freeze across several lot-size regimes (lot 50 → 36 lots; lot 75 → 24
  lots). NSE lot sizes were revised again by Circular 176/2025
  (2025-10-03): NIFTY 75→**65**, BANKNIFTY 35→**30**, FINNIFTY 65→60,
  MIDCPNIFTY 140→120, phasing in around 2025-12-30 for new far-month
  contracts
  (<https://zerodha.com/marketintel/bulletin/429705/revision-in-lot-size-of-index-derivative-contracts-from-december-30-2025>,
  <https://www.angelone.in/news/market-updates/nse-revises-lot-sizes-for-index-derivatives-from-december-2025-nifty-lot-size-to-reduce-to-65>).
  At lot 65 the *effective* NIFTY freeze becomes 1,755 units (largest lot
  multiple ≤ 1,800 — ICICI Direct freeze article). A further freeze
  revision effective 2025-12-01 (NSE/FAOP/71540) exists but its exact
  values were not extracted (§7 M-8).
- BSE: **SENSEX freeze = 50 lots/order** (Zerodha official:
  <https://x.com/zerodhaonline/status/1665649469906354176>; May-2023 BSE
  regime: 500 units at lot 10 = 50 lots,
  <https://zerodha.com/marketintel/bulletin/351407/single-order-quantity-limit-in-bse-fo-contracts-from-29th-may-2023>;
  the repo draft records 50 lots = 1,000 contracts at lot 20 per BSE
  Apr-2025 circular 20250401-8, `docs/track2/TRACK2_RISK_MGMT.md:57` +
  DEC-RISK-016 at `docs/track2/TRACK2_RISK_MGMT.md:528`). BANKEX ~40 lots
  (600 units @ lot 15, May-2023 — stale, §7 M-8).

**Freeze × multi-lot backtest positions (order slicing).** An order above
the freeze limit is rejected/frozen at the exchange gateway, so a position
larger than the freeze must be built as **multiple child orders ≤ freeze
qty** — i.e., a backtest that sizes above freeze must model (a)
deterministic slicing (⌈qty / freeze⌉ child orders), (b) per-child fill
prices (each child walks the book separately — at 1-minute bar granularity
this is unobservable and must be a disclosed worst-case assumption,
consistent with the engine's DEC-FILL-WORST-CASE-ONLY-001 adverse-fill
posture), and (c) per-child brokerage (flat per-order charges multiply).
The repo draft already states the minimum honest rule: "The backtest
simulator must reject any combo trade requesting more than the freeze
quantity per leg" (`docs/track2/TRACK2_RISK_MGMT.md:60-62`) —
rejection-not-slicing is the simpler v1 law.

**Repo drift to fix at design time:** the draft's NSE freeze table is
headed "Freeze qty (lots)" with NIFTY 1,800 / BANKNIFTY 900
(`docs/track2/TRACK2_RISK_MGMT.md:53-56`) — those NSE numbers are
**units**, not lots (NSE FAOP68834). The draft itself caught the identical
units-vs-lots conflation for SENSEX (DEC-RISK-016,
`docs/track2/TRACK2_RISK_MGMT.md:528`) but left the NSE rows mislabeled.

### 4.6 Circuits

**Single-stock price bands.**

- Non-F&O stocks: fixed daily bands of **5% / 10% / 20%** by surveillance
  category; orders outside the band are rejected; a band-locked stock
  simply stops trading beyond the band (Zerodha support
  <https://support.zerodha.com/category/trading-and-markets/trading-faqs/general/articles/what-does-circuit-limits-i-e-price-bands-mean>).
- **F&O-list stocks: NO fixed daily band.** They carry a **10% dynamic
  (operating) band** that is **flexed in 5% increments** in the direction
  of movement after a **15-minute cooling-off period** whenever the band
  edge is approached — trading continues (search-captured from the NSE
  price-band FAQ
  <https://www.nseindia.com/trade/price-band-operating-range-flex-faqs> and
  broker explainers) — CONFIRMED.
- Recent enhancement (NSE): **sliding** dynamic bands — when the band
  flexes in the trend direction the opposite band moves by the same amount,
  and resting orders outside the new band are CANCELLED by the exchange;
  Limit-Price-Protection behavior for options during cooling-off was also
  changed
  (<https://www.paytmmoney.com/blog/nse-enhances-stability-new-dynamic-price-bands-changes-in-limit-price-protection-for-options-during-cooling-off-period/>;
  exact circular ids/dates — §7 M-9).

**Option-premium operating ranges.** Options have **no daily price band**;
instead each contract gets a **delta-based, daily-computed operating
range**; orders priced outside it hit a "price freeze"/rejection, and the
range is relaxed on genuine market moves (5%-at-a-time flexing, same
cooling-off machinery) (NSE equity-derivatives price-bands page
<https://www.nseindia.com/static/products-services/equity-derivatives-price-bands>
+ the NSE flexing FAQ, search-captured). Design note: a deep-OTM premium
can legitimately multiply many-fold intraday — the operating range
mechanism means some of those prints occur in stair-steps after flex
events, which 1-minute bars record but a naive continuous-fill simulator
would mis-time.

**Index market-wide circuit breakers (MWCB).** Triggered at **±10% / ±15% /
±20%** on either benchmark (BSE Sensex or Nifty 50, computed daily off the
previous close); a trigger halts **ALL equity AND equity-derivatives
trading nationwide** — option writing included — with resumption via a
**pre-open call auction** (NSE circuit-breakers page
<https://www.nseindia.com/products-services/equity-market-circuit-breakers>,
search-captured). Halt matrix (as encoded in the repo draft,
`docs/track2/TRACK2_RISK_MGMT.md:447-456`, consistent with the NSE page
family; exact per-cell times not independently re-quoted this session —
§7 M-10):

| Trigger | Time | Halt |
|---|---|---|
| 10% | before 13:00 | 45 min |
| 10% | 13:00–14:30 | 15 min |
| 10% | after 14:30 | no halt |
| 15% | before 13:00 | 1h 45m |
| 15% | 13:00–14:00 | 45 min |
| 15% | after 14:00 | rest of day |
| 20% | any time | rest of day |

**What a circuit-locked underlying does to option quotes/trades
(practice).**

- **Index MWCB:** options stop trading with the market (coordinated halt)
  — a short option cannot be bought back during the halt; it reprices
  discontinuously at the post-auction reopen. The repo draft's DEC-RISK-015
  already defines a synthetic-exit convention for this (last traded price
  of the halt bar + 50 bps penalty, calibrated to the 2024-06-04
  election-day crash; `docs/track2/TRACK2_RISK_MGMT.md:458-472,527`); see
  [08-seller-blowups-and-tail-events.md](08-seller-blowups-and-tail-events.md).
- **Banned stock (MWPL):** options keep trading but only position-reducing
  orders are legal; liquidity typically thins (broker explainers, e.g.
  <https://www.nirmalbang.com/knowledge-center/future-and-options-ban-impact.html>
  — qualitative).
- **F&O stock at a dynamic-band edge:** the underlying keeps trading after
  each 15-min cooling/flex cycle; option quotes gap between flex events and
  LPP restrictions bind during cooling-off (Paytm Money article above).
  Quantitative quote behavior in these windows is NOT claimable from
  1-minute OHLC data — honest limit: any options backtest over such
  episodes is assumption-driven, and the assumption must be disclosed per
  trade (the engine's existing correct-or-absent / worst-case posture
  generalizes).

### 4.7 Margin-shortfall penalties + forced square-off (RMS reality)

Exchange penalty structure — verified:

| Condition | Penalty on shortfall |
|---|---|
| Shortfall < ₹1 lakh AND < 10% of applicable margin | **0.5% per day** |
| Shortfall ≥ ₹1 lakh OR ≥ 10% of applicable margin | **1% per day** |
| Shortfall continues > 3 consecutive days | **5% per day** from the 4th day |
| > 5 shortfall days in a calendar month | **5% per day** for each further day that month |

Sources: NSE Clearing penalties page (search-captured from
<https://www.nseclearing.in/clearing-settlement/equity-derivatives/penalties>),
<https://zerodha.com/z-connect/general/margins-margin-penalties-when-trading-with-leverage>,
Bajaj/Kotak broker explainers. **Repo drift:** the draft's table at
`docs/track2/TRACK2_RISK_MGMT.md:97-101` compresses this to "0.5% day 1-3,
5% day 4+", omitting the 0.5%-vs-1% tier split — fix at design time.

Retail RMS reality for writers:

- Peak-margin snapshots mean an intraday adverse move can create a
  *snapshot* shortfall even if EOD margin is fine — penalties accrue on the
  peak figure (§4.2 sources).
- Broker RMS acts before the exchange does: the repo draft records Groww's
  policy as auto-close "when margin shortfall > ₹5,000 OR mark-to-market
  risk-of-shortfall is detected", sourced to groww.co/risk-management-policy
  (`docs/track2/TRACK2_RISK_MGMT.md:404-407`) — repo-draft-sourced, not
  re-verified live this session (§7 M-11). Groww MIS auto-square-off runs
  at 15:20 (admin SQ from 15:19 NSE / 15:18 BSE;
  `docs/track2/TRACK2_RISK_MGMT.md:397-401`), which is exactly why the
  engine's 15:20 forced-exit law is broker-aligned
  (`docs/track2/TRACK2_RISK_MGMT.md:409-414`).
- Design consequence: a writer backtest that reports "margin-true ROI"
  needs, at minimum, a per-day margin series + a shortfall-penalty model +
  an RMS-liquidation trigger; today the repo can honestly deliver only the
  "unverified" verdict (§5.1).

---

## 5. Repo reconciliation — what BruteX already encodes (all `file:line` verified by reading)

### 5.1 Master domain table

The doctrine everywhere: a fact that is not citation-grounded is NEVER
fabricated — refusal rows + typed errors + skip-and-count rejections
(DEC-ETC-REGIME-HISTORY-001 = MIGRATION.md entry #1524,
`docs/MIGRATION.md:6958`; no runtime escape hatch,
`brutex/costs/constants.py:126-136`).

| Domain | What exists in-repo today | Evidence (file:line) |
|---|---|---|
| **Expiry ladder** | Vendor-data-primary law already live: expiries/contracts FETCHED via `get_expiries`/`get_contracts`, never derived; futures ownership from vendor-parsed lake dirs with the computed calendar demoted to observe-only | `brutex/adapters/groww/contracts.py:10-14`; `brutex/data/futures_series.py:14-23` |
| **Strike grid** | Data-driven step derivation with confidence labels (registry-preferred over lake-observed) — the right law; PLUS a falsified hardcoded-constants counter-example to retire | `brutex/data/moneyness.py:11-16,106-141,181-235` (right); `brutex/options/strike_grid.py:12-13,26-30` (falsified — §1.8) |
| **Lot sizes** | Date-keyed `(effective_date, lot)` tables for the 3 engine indices with O(log N) bisect + refuse-outside-history (`LOT_SIZE_UNKNOWN` counted rejects, never an invented lot); per-lot audit in §5.2 | `brutex/options/lot_size_history.py:58-77,96-123`; `brutex/options/executor.py:76,166-170,498-507` |
| **Exercise style** | European assumed for pricing (BSM exact, no American adjustment) — matches §2.1's verdict | `docs/track2/PLAN_FINALIZATION_2026-05-16.md:182,213` |
| **Settlement/exercise MECHANICS** | Spec-level only: `EXPIRY_EXERCISE`/`EXPIRY_ASSIGN`/`EXPIRY_WORTHLESS` exist as a TradeOutcome enum + pseudo-code; the shipped calculator handles NORMAL_CLOSE only ("Track 2 never holds to expiry") | `docs/track2/CALCULATOR_SPEC.md:319-388`; `docs/track2/COSTS_VERIFIED.md:249` |
| **Final settlement price** | NOT stored anywhere — no `settlement_price` producer in `brutex/` (searched); expiry instant pinned 15:30 IST for Greeks T | `brutex/data/greeks_enrich.py:239-240`; absence: §5.5 |
| **STT / levies** | Battle-tested date-keyed `RegimeRow`/`rate_for_date` machinery + typed refusal (`UnverifiedCostRegimeError` → `COST_REGIME_UNVERIFIED` counted rejects); component audit in §5.3-5.4 | `brutex/costs/constants.py:313-381,486-504`; `brutex/costs/types.py:130-160`; `brutex/options/executor.py:184,530` |
| **SPAN margin** | **Refusal-only regime table** — ZERO verified rows; `margin_for_date` raises typed `UnverifiedMarginRegimeError` for every date; ROI surfaces render `status="unverified"`; override paths are DATA only (cited row or ADR-stamped ASSUMED row) | `brutex/engine/margin_history.py:1-28,56-83,86-121,152,160-173,176-225,243-303` |
| **Margin no-hardcode law** | Track-2 draft FORBIDS hard-coded margin constants ("must be computed live from broker SPAN file at backtest time … flag both in audit log with `margin_method`") — cited verbatim by margin_history's docstring | `docs/track2/TRACK2_RISK_MGMT.md:109-121`; `brutex/engine/margin_history.py:13-16` |
| **Freeze quantity** | Fully plumbed as DATA: master column `freeze_quantity` → parsed → model field → leg-economics fold → persisted → displayed; validated positive-when-set (FR-EXEC-005); `None` for index/non-F&O rows. **No order-slicing or rejection ENGINE logic exists** (the executor trio that would have consumed it was deleted, DEC-EXECUTION-TRIO-DELETE-001) | `brutex/adapters/groww/constants.py:48`; `brutex/adapters/groww/instruments.py:207`; `brutex/models.py:138`; `brutex/execution/types.py:72-100`; `brutex/universe.py:891-931`; `brutex/data/instrument_store.py:577`; `brutex/data/economics_integrity.py:5`; `brutex/dashboard/universe_query.py:314-317`; `brutex/dashboard/templates/universe.html:1118` |
| **Ban lists / MWPL** | **Nothing.** Repo grep for `ban` finds only vendor "soft-ban" prose + unrelated ingest machinery; no MWPL data, no ban-calendar sidecar, no ban-aware gate | `brutex/adapters/groww/pull_singleflight.py:10` (vendor context); `brutex/adapters/empty_circuit_breaker.py:4` (vendor context); prose-only `docs/track2/TRACK2_RISK_MGMT.md:64-68` |
| **Exchange circuits / price bands** | **Nothing executable.** Every `circuit` hit is the internal ingest circuit breakers (unrelated); the MWCB matrix + DEC-RISK-015 synthetic-exit convention are draft prose | `brutex/adapters/empty_circuit_breaker.py:1`; `brutex/adapters/all_filtered_breaker.py:1`; `docs/track2/TRACK2_RISK_MGMT.md:442-479` |
| **Position limits** | **Nothing executable.** FutEq switch (DEC-RISK-009), limits tables, snapshots — draft prose only; no code computes delta-adjusted position values | `docs/track2/TRACK2_RISK_MGMT.md:27-47,483-494,522` |
| **Peak margin / penalties** | Draft prose only (with the §4.7 tier omission noted); no penalty model in code | `docs/track2/TRACK2_RISK_MGMT.md:93-105` |
| **Expiry-day margin rules** | Draft prose only (+2% ELM, calendar-spread removal, weekly rationalization); the repo's live expiry machinery (`brutex/options/`, movers expiry tiers) tracks expiry *calendars*, not expiry-day *margin* rules | `docs/track2/TRACK2_RISK_MGMT.md:418-438` |
| **Session shape** | 09:15–15:30 constants + 15:20 forced exit — will need per-(segment, date-range) data from 2026-08-03 (§1.10) | `brutex/calendars/nse.py:117,124`; CLAUDE.md §7 |

**Net statement (honest):** across the five §4 domains BruteX today encodes
exactly ONE as data (freeze quantity, per instrument, unconsumed by any
engine) and ONE as a refusal-only mechanism (SPAN margin). Ban calendars,
MWPL, circuit bands, MWCB halts, position limits, peak margin, and
shortfall penalties exist — where they exist at all — only as the DRAFT
prose of `docs/track2/TRACK2_RISK_MGMT.md`, which carries its own
verify-before-code-lock banner and the specific drifts itemized in §4
(freeze units-vs-lots at `:53-56`; exposure "3-5%" at `:80-81`;
penalty-tier compression at `:97-101`; calendar-spread date at `:424-425`).

### 5.2 Audit of the repo's lot table against §1.9 (the date-keyed lot law)

`brutex/options/lot_size_history.py:58-77` holds per-underlying
`(effective_date, lot)` tables for NIFTY/BANKNIFTY/SENSEX with an O(log N)
bisect lookup (`brutex/options/lot_size_history.py:96-123`); the options
translation executor consults it per trade — a date outside recorded
history is a counted `LOT_SIZE_UNKNOWN` reject, never an invented lot
(`brutex/options/executor.py:76,166-170,498-507`).
`brutex/engine/margin_history.py:86-121` applies the same refusal-row
pattern to per-lot SPAN margin. Audit:

| Repo entry (`lot_size_history.py`) | External verdict |
|---|---|
| NIFTY `(2021-07-29, 50)`, `(2024-04-26, 25)`, `(2024-11-20, 75)`, `(2026-01-01, 65)` | Dates match the monthly-series reality (Jul-2021 monthly expiry; contracts trading 2024-04-26; new contracts 2024-11-20; Jan-2026 cycle). Transition-window mixed lots not representable — see §6 item 3. |
| BANKNIFTY `(2021-01-01, 25)` | **25 in Jan-2021 is UNVERIFIED** (§7 E-1): externally confirmed only that the lot was 25 *immediately before* Jul-2023. A 20→25 transition during 2020–21 could make early rows wrong. |
| BANKNIFTY `(2023-06-30, 15)` | External: "from the July-2023 monthly expiry" — same economic instant (Jun-2023 monthly expiry EOD ≈ 2023-06-29/30). OK as approximation. |
| BANKNIFTY `(2025-07-01, 35)`, `(2025-12-30, 30)` | Matches the Apr-2025 review (first affected monthly = Jul-2025) and the FAOP70616 Dec-30 transition. But new contracts already carried 35 from 2025-04-25 — a date-keyed table serves 30 for a May-listed Aug-expiry contract, which is wrong per-contract. |
| SENSEX `(2021-01-01, 10)` | **Conflicts** with the relaunch reporting (pre-2023-05-15 lot was 15, cut *to* 10 at relaunch). Immaterial for P&L (pre-2023 BSE F&O liquidity ≈ nil) but factually stale. |
| Docstring row "≤ Jun 2021 BANKNIFTY 25" (`brutex/options/lot_size_history.py:10-19`) | Same §7 E-1 uncertainty. |

FINNIFTY/MIDCPNIFTY/NIFTYNXT50/BANKEX and all ~200 stock underlyings have
**no** lot history in the repo — a full-universe options engine needs
either extended tables or (better) per-contract lots from archived vendor
instrument masters; the live Groww master already carries `lot_size` per
row (CLAUDE.md §6 instrument-master schema row;
`docs/groww/api-reference/02-instruments.md:112-134` per that table), and
the loader folds derivative-leg economics onto each underlying
(DEC-GROWW-LEG-ECONOMICS-001, MIGRATION.md #696) — but that is the
*current* lot only, not history.

### 5.3 OPTIONS cost components (calculator = `brutex/costs/calculator.py`, rates = `brutex/costs/constants.py`)

| Component | Encoded rate | Encoded window | Status | file:line |
|---|---|---|---|---|
| Groww brokerage | ₹20 flat/order | all dates | VERIFIED (per-ORDER, not per-lot) | `brutex/costs/constants.py:45-48`; `docs/track2/COSTS_VERIFIED.md:28,315-320` |
| STT sale (premium) | 0.0625% | **date.min → 2024-09-30** | ENCODED but **known-imprecise before 2023-04-01** (true rate 0.05%; repo flags it — `brutex/costs/constants.py:390-394`, MIGRATION #1524 follow-up (iii)). §3.1's NSE FATAX/56235 citation now closes the verification gap; encoding it needs its own DEC. Direction: over-costs 2020→Mar-2023 trades (conservative). | `brutex/costs/constants.py:70-71,385-395` |
| STT sale | 0.10% | 2024-10-01 → 2026-03-31 | VERIFIED | `brutex/costs/constants.py:73-75,396-400` |
| STT sale | 0.15% | 2026-04-01 → | VERIFIED (DEC-COST-003; externally re-confirmed §3.1) | `brutex/costs/constants.py:77-80,401-405` |
| STT exercise (intrinsic) | 0.125% / 0.15% | date.min → 2026-03-31 / 2026-04-01 → | VERIFIED for the data window (post-2019-09 intrinsic basis, §3.2); forward-compat stub — "Track 2 never sets this" | `brutex/costs/constants.py:82-95,411-425,507-525` |
| NSE ETC (premium, both sides) | 0.03503% | **2024-10-01 →** | VERIFIED (NSE FA64232; SEBI true-to-label 2024/92; externally re-confirmed) | `brutex/costs/constants.py:150-155,443-450` |
| NSE ETC | — | **date.min → 2024-09-30** | **REFUSAL row** (slab-ladder era, circulars identified never retrieved: FA46730/FA56129/FA61137) | `brutex/costs/constants.py:431-453` |
| BSE ETC (Sensex/Bankex) | 0.03250% | 2024-10-01 → | VERIFIED (BSE notice 27-Sep-2024) | `brutex/costs/constants.py:157-162,455-478` |
| BSE ETC | — | date.min → 2024-09-30 | **REFUSAL row** | `brutex/costs/constants.py:455-467` |
| SEBI turnover fee | 0.0001% (₹10/cr) | all dates (no regime table) | VERIFIED | `brutex/costs/constants.py:173-175` |
| NSE IPFT (options) | 0.0005% (₹50/cr) | all dates | VERIFIED (NSE/FA/56129) — but see the FA73061 forward alert (§3.4, §7 S-6) | `brutex/costs/constants.py:182-184` |
| BSE IPFT | 0 | all dates | **UNVERIFIED-assumed-0** (§7 S-7) | `brutex/costs/constants.py:186-188`; `docs/track2/COSTS_VERIFIED.md:273` |
| Stamp duty (buy premium) | 0.003% | all dates (uniform since 2020-07-01, Indian Stamp Act) | VERIFIED | `brutex/costs/constants.py:195-198` |
| GST | 18% on (brokerage+ETC+SEBI+IPFT), rounded ONCE | all dates | VERIFIED (DEC-COST-001) | `brutex/costs/constants.py:205-211`; `docs/track2/COSTS_VERIFIED.md:46` |
| Rounding law | statutory items nearest-rupee; repo applies adverse CEIL everywhere post-DEC-FILL-WORST-CASE-ONLY-001 | — | worked oracles enforced by `tests/golden/test_costs_verified_examples.py` | `docs/track2/COSTS_VERIFIED.md:106-133` |

**Honestly-priceable options window today: [2024-10-01 → today]** — every
earlier options trade is a counted `COST_REGIME_UNVERIFIED` rejection
because of the ETC refusal rows (`docs/track2/COSTS_VERIFIED.md:731`).
Combined with the lot-history floor (2021-01-01,
`docs/track2/COSTS_VERIFIED.md:734`) this is THE binding cost constraint
any future options-strategy engine inherits: **a 2020-2024 options backtest
cannot be honestly costed until the pre-Oct-2024 NSE/BSE ETC circular
bodies land** (or the ADR-gated top-slab-ASSUMED path is taken —
`brutex/costs/constants.py:126-136`).

### 5.4 FUTURES cost components (`brutex/costs/futures_costs.py`)

| Component | Encoded | Status | file:line |
|---|---|---|---|
| Groww futures brokerage | min(₹20, 0.03% × turnover)/order | VERIFIED | `brutex/costs/futures_costs.py:23-26,192` |
| Futures STT | 0.02% (2024-10-01→) / 0.05% (2026-04-01→) | VERIFIED | `brutex/costs/futures_costs.py:198-204,214-238` |
| Futures STT pre-2024-10-01 | — | **REFUSAL row** (0.0125% appears in `docs/track2/TRACK2_TAX_HANDLING.md:274` WITHOUT a VERIFIED stamp). §3.1 now provides the external citations (0.0125% for 2023-04→2024-09; 0.01% before) to close it via a DEC. | `brutex/costs/futures_costs.py:214-224,43-46` |
| Futures ETC / NSE futures IPFT / futures stamp | — | **ALL-REFUSAL tables** (every date refuses; figures exist only in the §14 proposal block of COSTS_VERIFIED) | `brutex/costs/futures_costs.py:241-311` |
| SEBI fee / GST | reused from options stack | VERIFIED | `brutex/costs/futures_costs.py:33-36` |
| INDEX futures | **COST-FREE by decree** (signal-only; net == gross) — only a cost-bearing STOCK future would hit the refusal windows | `brutex/costs/scope.py:1-43` (DEC-COST-SCOPE-INDEX-SIGNAL-ONLY-001); `brutex/costs/futures_costs.py:65-67` |

### 5.5 What the repo does NOT encode (relevant to a future options engine)

- **No settlement/exercise MECHANICS**: `EXPIRY_EXERCISE`/`EXPIRY_ASSIGN`/
  `EXPIRY_WORTHLESS` exist only as a spec-level TradeOutcome enum +
  pseudo-code (`docs/track2/CALCULATOR_SPEC.md:319-388`); the shipped
  calculator handles NORMAL_CLOSE only ("Track 2 never holds to expiry" —
  `docs/track2/COSTS_VERIFIED.md:249`). Two spec-registered open questions
  bind the future DEC: regime keying for expiry events (entry vs exit date
  — `docs/track2/CALCULATOR_SPEC.md:347-352`) and the era-dependent
  sell-open STT note (`docs/track2/CALCULATOR_SPEC.md:366-371`).
- **No physical-settlement model at all** (no delivery-margin ramp, no
  netting, no delivery STT) — consistent with index-only Track-2 scope; a
  stock-options engine would need all of §2.3-2.4.
- **No auto-exercise/settlement-price model**: the lake stores 1-minute
  bars; the final settlement price (last-half-hour index close, §2.5) is
  NOT a stored field anywhere (searched: no `settlement_price` producer in
  `brutex/`) — a hold-to-expiry engine must either derive a proxy from the
  1m spot bars (an approximation to be disclosed) or ingest the exchange's
  published settlement prices (new data surface, its own DEC).
- **No ban-calendar / MWPL / circuit / position-limit / peak-margin /
  penalty machinery** (§5.1 rows).
- **Internal doc drift notes (report-only, not fixed here):**
  `docs/track2/TRACK2_TAX_HANDLING.md:276` still marks the 2026-04-01
  rates "UNVERIFIED — Budget 2026 sources conflict" while
  `docs/track2/COSTS_VERIFIED.md:272` + `brutex/costs/constants.py:77-95`
  locked them (DEC-COST-003/004) — and §3.1's external check confirms the
  locked values; the TAX_HANDLING row is the stale copy. Likewise the
  §4.5/§4.7 TRACK2_RISK_MGMT drifts and the falsified
  `brutex/options/strike_grid.py:12-13` docstring (§1.8).

---

## 6. Design-readiness implications (consolidated; for the future operator-gated options-engine DEC)

1. **Expiry ladder = vendor data.** Nine+ distinct weekday-regime breaks in
   6 years, one aborted (Monday) move, and in-place re-dating of
   already-listed contracts (FINNIFTY 2021-10-14 EOD; BANKEX 2023-10-13
   EOD) mean *no formula ("last Thursday") survives the window*. The repo's
   existing law generalizes: vendor-parsed expiries primary, computed
   calendar observe-only (`brutex/data/futures_series.py:14-23`); the
   registry `get_expiries`/`get_contracts` chain
   (`brutex/adapters/groww/contracts.py:10-14`) is the source of truth.
2. **Strike ladder = vendor data.** Already the repo law
   (`brutex/data/moneyness.py:11-16,106-141`); the hardcoded
   `brutex/options/strike_grid.py:12-13` "constants 2021-2026" claim is
   falsified (§1.8) and should be superseded in the design.
3. **Lot = per-CONTRACT vendor attribute; date-keyed tables are the
   fallback.** Every lot revision has a three-tier effective structure
   (§1.9), so transition windows have mixed lots across coexisting
   expiries. The `lot_size_history.py` law (date-keyed,
   refuse-outside-history) is the right *fallback* shape but: (a) its
   BANKNIFTY 2020–21 and SENSEX pre-2023 rows need primary-circular
   verification (§7 E-1/E-4); (b) it starts 2021-01-01 while the data floor
   is 2020-01-01 (`brutex/options/executor.py:166-170` already treats 2020
   as honest LOT_SIZE_UNKNOWN); (c) it covers 3 of ~230 underlyings.
   Preferred design: lot from the archived per-day instrument master /
   contract record, with the date table as cross-check.
4. **The Nov-2024 weekly cull is a scope wall.** A weekly-option strategy
   family is only backtestable on {BANKNIFTY ≤ 2024-11-13, FINNIFTY ≤
   2024-11-19, MIDCPNIFTY/BANKEX ≤ 2024-11-18} and on {NIFTY, SENSEX}
   throughout — the engine's instrument×tenor availability matrix must be
   dated, or survivorship-of-products bias creeps in silently.
5. **Session close becomes per-date data on 2026-08-03** (§1.10) — and
   expiry settlement needs an FSP model distinct from the last bar close
   (§2.5).
6. **European-only kills the early-exercise axis** (§2.1) — no assignment
   hazard modeling pre-expiry.
7. **Index vs stock is THE settlement fork**: index = cash, per-leg cash
   flows sum; stock = physical with netting (§2.3), delivery-margin ramp
   (§2.4), delivery STT (§2.3) — a stock-options strategy engine is a
   materially bigger build than an index-options one. A staged future DEC
   could reasonably scope index-options strategies FIRST (all three engine
   underlyings NIFTY/BANKNIFTY/SENSEX are index-option families; matches
   `brutex/costs/scope.py` which already prices index options as the
   cost-bearing Track-2 surface).
8. **STT is a 4-regime, date-keyed, leg-role-asymmetric tax** (§3.1):
   seller-on-premium at open (for writers) or close (for longs),
   purchaser-on-intrinsic at exercise, zero on buys and worthless expiries.
   The repo's `rate_for_date` regime machinery
   (`brutex/costs/constants.py:313-381`) is the right shape and already
   battle-tested; new rows land as cited `RegimeRow`s + a DEC, zero
   mechanism change.
9. **The honestly-priceable window is [2024-10-01 → today]** until the
   pre-boundary ETC circulars land (§5.3) — any options-strategy backtest
   proposal must state this or fund the circular retrieval first.
10. **Exit-at-15:29 vs hold-to-settle is a sweepable, regime-dependent
    axis** (§3.3): the STT asymmetry favoring pre-expiry sale is ₹18.75 per
    ₹75k intrinsic under the Oct-2024 regime, vanishes from Apr-2026, and
    reverses once trade-level charges + spread are counted.
11. **Pin risk needs a settle-model-with-error-bar, not a point settle**
    (§2.6).
12. **Extend the refusal-only precedent, never invent margin.**
    `brutex/engine/margin_history.py:86-121`'s RegimeRow/refusal template
    is the correct citizen shape for writer margin: per-day SPAN either
    comes from ingested exchange SPAN-parameter archives (a new lake
    dataset + its own DEC) or a disclosed deterministic SPAN replica with a
    `margin_method` audit stamp (`docs/track2/TRACK2_RISK_MGMT.md:118-121`)
    — until then, ROI verdicts stay `unverified` exactly as today.
13. **Regime tables are date-keyed, and this chapter supplies the dates**:
    peak-margin phases (2020-12→2021-09), +2% expiry ELM (2024-11-20),
    lot/contract-size steps (2024-11-20, ~2025-12-30), premium-upfront
    (2025-02-01), calendar-spread expiry-day removal (2025-02-01/10 — pin
    via CMPT66047 before code-lock), FutEq limits
    (2025-07-01→2025-10-01→2025-12-05), stock-calendar extension
    (2026-05-05), STT boundaries (2023-04-01, 2024-10-01, 2026-04-01).
    Each becomes one row in a DEC-ETC-REGIME-HISTORY-001-style table with
    its circular as the citation.
14. **Freeze is already data — the missing piece is a per-leg gate**: v1
    law = reject any leg above `freeze_quantity`
    (`docs/track2/TRACK2_RISK_MGMT.md:60-62`); v2 = deterministic slicing
    with per-child worst-case fills + per-order costs. Freeze values are
    time-varying (NSE revises them), so a HISTORICAL backtest needs a dated
    freeze table, not the master's current snapshot — the master carries
    only "now" (`brutex/adapters/groww/instruments.py:207`).
15. **Ban calendar = a new small lake sidecar** (per-day per-underlying ban
    flags from NSE's published ban list) gating stock-option strategies to
    reduce-only during bans; index strategies need no ban logic (verified
    exempt, §4.4).
16. **Circuit events = session anomalies, not bars**: MWCB days (rare,
    datable — e.g. 2024-06-04 taken as calibration in DEC-RISK-015) and
    dynamic-band flex windows should be flagged per-session so trades over
    them carry a disclosed synthetic-exit assumption — the same
    honest-envelope pattern the engine already uses for Muhurat sessions
    and session-gap exits.
17. **Position limits are realism garnish at retail scale** — but any
    post-2025-10-01 simulation claiming institutional realism must compute
    FutEq (delta-adjusted) exposure, which requires the Greeks overlay the
    lake already stores (delta per option bar via the Greeks enrichment) —
    a genuine synergy: BruteX's stored per-bar delta makes FutEq
    computation feasible without new vendor data.

---

## 7. Consolidated UNVERIFIED register

One row per open item, namespaced by source part (E = expiry/grids/lots,
S = settlement/STT, M = margins/limits/circuits). Every row carries its
best-known state and the concrete action that would verify it. Nothing in
this table may be encoded as fact until closed (Golden Rule #1).

| # | Claim / item | Best-known state | What would verify it |
|---|---|---|---|
| E-1 | BANKNIFTY lot during 2020 → mid-2021 (20 vs 25; possible 20→25 change at the Jul-2021 review) | Repo table says 25 from 2021-01-01 (`brutex/options/lot_size_history.py:67`); external sources confirm 25 only as of Mar-2023 ("reduced to 15 **from 25**"). Web searches found no 2021 BANKNIFTY revision either way. | NSE circular NSE/FAOP/47856 (Feb-2021, [mirror](https://www.rkglobal.net/upload/637601378287808566_Revised%20Market%20Lot.pdf)) fetched with working egress — it lists every index lot in that review. |
| E-2 | MIDCPNIFTY Wednesday→Monday exact cutover day (2023-08-16 vs -17) and its launch-day weekly cadence (weekly options from day 1?) | Circular dated 2023-07-12; Fyers says "w.e.f. 17th August 2023", one search summary said the 16th. Launch 2022-01-24 with Wednesday expiry is multi-source but the initial weekly-vs-monthly-only product mix is unconfirmed. | NSE circular 119/2023 + the MIDCPNIFTY launch circular (FAOP51222-era) from NSE archives. |
| E-3 | Exact SEBI CIR/2024/132 measure wording + all six effective dates (esp. 2024-11-20 vs the press "Nov 21" first-session rendering) | Multi-outlet consistent: Nov-20 (contract size, one-weekly, 2% ELM), Feb-1-2025 (premium upfront, spread benefit), Apr-1-2025 (intraday limits). SEBI + all mirrors 403'd direct fetch. | Read the [SEBI circular](https://www.sebi.gov.in/legal/circulars/oct-2024/measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html) or [CSE mirror PDF](https://www.cse-india.com/upload/upload/Oct_011024.pdf). |
| E-4 | Pre-May-2023 SENSEX/BANKEX contract regime (expiry weekday, lot 15/20, liquidity ≈ nil) | Business Today relaunch article implies prior lots 15/20 and a Thursday-era cycle; pre-relaunch BSE F&O volume was negligible (widely reported, unquantified here). | BSE contract-spec notices pre-2023; BSE monthly derivatives turnover stats 2020–2023. |
| E-5 | BSE lot revisions after Nov-2024 (SENSEX 20, BANKEX 30 still current in 2026-07?) and any BSE strike-scheme changes | No revision surfaced in searches; SENSEX ≈ 20 × index ≈ within the ₹15–20L band so no forced change. 2026 lot listicles ([Sahi](https://www.sahi.com/blogs/nifty-lot-size-2026-bank-nifty-sensex)) list SENSEX 20. | BSE notices archive (bseindia.com DispNewNoticesCirculars) for 2025–2026 lot circulars. |
| E-6 | "No expiry-day / weekly-universe / lot changes in 2026-H1 beyond the Jan-2026 lot cycle" | Nothing surfaced in searches up to 2026-07; corroborating 2026 calendar pages exist. Absence of evidence only. | NSE/BSE circular archives Jan–Jul 2026; SEBI circular listing page. |
| E-7 | Per-period weekly-contract COUNTS (7 vs 4 serial NIFTY weeklies), FINNIFTY/MIDCPNIFTY quarterly availability, current SENSEX tenor counts | NSE NIFTY page (current): 4 weekly + 3 monthly + 3 quarterly + 8 half-yearly. Historical counts and non-benchmark quarterly ladders unconfirmed. | NSE/BSE contract-specification pages + historical circulars; archived contract masters. |
| E-8 | Exact strike-scheme parameters: BANKNIFTY 2025-01-20 revision details; NIFTY 2025-11-17 new band table; stock price-band interval table; intraday strike-addition trigger rules | Search summaries were partially garbled (e.g. "100 to 25 points" for NIFTY is implausible as the base near-ATM step); direction + effective dates are solid, parameters are not. | The primary NSE circulars (strike-scheme revision PDFs) + NSE "Strike Scheme" annexure for stocks. |
| E-9 | Whether the ₹15–20L notional band applies to STOCK derivatives (vs the older ₹5–10L band) and the formal stock-lot review cadence | SEBI Oct-2024 measure as reported is index-only; stock lots demonstrably revised in the same NSE reviews. Cadence "≈ half-yearly" is inferred from observed circulars, not a stated rule. | SEBI stock-derivative contract-size framework circulars (2018 physical-settlement framework + any 2024–25 amendment). |
| E-10 | Spot-vs-FSP divergence magnitudes ("5–15 pts calm, 50–100+ volatile") | Magnitudes from one trading blog ([OptionX](https://optionx.trade/blogs/expiry-day-vwap-settlement-index-options-trading)). (The companion claim "stock physical settlement since Oct-2019" — tagged unsourced in the part-1 research — is now web-sourced in §2.1 via the SEBI Dec-2018 circular + Zerodha/TaxGuru.) | Empirical: compute FSP-vs-close from constituent data; SEBI physical-settlement circular (2018/2019) for the companion claim. |
| E-11 | Whether the cash/index session shape (BruteX's spot input) changes under CAS on 2026-08-03, and the exact index-FSP construction under the 15:10–15:40 derivatives window | Derivatives close 15:40 + VWAP 15:10–15:40 is multi-outlet; the cash CAS session's effect on index closing-value computation is not yet clear from secondary sources. | NSE circulars on CAS + revised F&O timings (mid-2026); NSE Clearing settlement-price page post-August-2026. |
| E-12 | The repo's own docstring claim "lot sizes changed 7+ times during the 2021-2026 backfill window" (`brutex/options/lot_size_history.py:3-5`) | Consistent with this research for the 3 tracked underlyings combined (NIFTY 4 changes + BANKNIFTY ≥5 + SENSEX ≥2), but the repo's per-row sources (e.g. "Zerodha Marketintel #353422", "NSE Jan-2026 circular") were not independently fetched. | Fetch the cited bulletins/circulars; reconcile row-by-row. |
| S-1 | Exact initial physical-settlement stock count/date ("46 stocks from July 2018") | Widely reported; circular body not re-read this session — count UNVERIFIED-exact. | Read SEBI/HO/MRD/DP/CIR/P/2018/67 (2018-04-11) body + the NSE follow-on circular. |
| S-2 | DNE facility introduction year (2017 vs 2019 — sources conflict) | Secondary sources split; discontinuation (2023-03-30) and reinstatement (2022-04-28) are multi-source. | The original NSE Clearing circular introducing DNE for CTM. |
| S-3 | Whether NSE/BSE levy exchange transaction charges (ETC/SEBI/IPFT) on EXERCISED (non-traded) contracts | The repo's illustrative branch says yes on intrinsic (`docs/track2/CALCULATOR_SPEC.md:356-358`, marked illustrative); real-world contract notes commonly show STT+brokerage only. Flips the §3.3 verdict by ≈ ₹30/lot. | A real contract note for an exercised position (the repo's own Level-1 workflow, `docs/track2/COSTS_VERIFIED.md:280-309`), or an NSE FATAX circular naming exercise-value charges. |
| S-4 | Buyer-vs-seller split and exact base of the 0.1% STT on PHYSICAL delivery of stock derivatives | Repo has "0.10% of contract value VERIFIED" at `docs/track2/PLAN_FINALIZATION_2026-05-16.md:333` but not the split (delivery equity STT is 0.1% BOTH sides). | CBDT clarification (Aug-2018) + NSE FATAX circular text. |
| S-5 | The NSE Clearing circular NUMBER behind the 10/25/45/70% delivery-margin ramp | The schedule itself is multi-broker-corroborated (§2.4); broker pages don't cite the circular. | Fetch the NCL circular from nseclearing.in. |
| S-6 | NSE FA73061 (eff. 2026-03-01) ₹3,552 + ₹1/cr txn/IPFT re-split (total unchanged 0.03553%) | Reported; deliberately NOT encoded (`brutex/costs/constants.py:139-148`; `docs/track2/COSTS_VERIFIED.md:736`). | The circular body, official transport — already MIGRATION #1524 follow-up (i). |
| S-7 | BSE IPFT exactly 0 vs near-zero | Repo treats as 0, flagged UNVERIFIED (`brutex/costs/constants.py:186-188`; `docs/track2/COSTS_VERIFIED.md:273`). | BSE members' transaction-fees page. |
| S-8 | Exact index-close computation formula on expiry (constituent-VWAP last-30-min details; whether index options use the index CLOSE or a separately computed settlement value in all cases) | Mechanism multi-source at the level stated in §2.5; formula details not primary-quoted. | NSE Clearing settlement-price page body + contract specification documents. |
| S-9 | CTT rates (commodity) | Out of scope (COMMODITY segment excluded from ingest — `brutex/adapters/groww/constants.py:91-95`); not researched. | N/A unless commodity scope ever opens (its own DEC). |
| S-10 | Pre-2020 STT regimes (e.g. option-sale 0.017% era) | Out of window (data floor 2020-01-01, `GROWW_HISTORY_FLOOR`); not needed. | N/A for the current data window. |
| S-11 | Broker-by-broker DNE default policies 2020→Mar-2023 (affects faithful CTM modeling in that window) | Varied by broker (§2.2); LOW materiality for a self-closing strategy engine. | Archived broker policy pages. |
| M-1 | SPAN "16-scenario array, recalibrated 6× intraday" cadence | Repo-draft-sourced (`docs/track2/TRACK2_RISK_MGMT.md:78-79`); 16-scenario is standard CME-SPAN but the NSE page text + the exact intraday file schedule were not captured this session. | Read <https://www.nseclearing.in/risk-management/equity-derivatives/nsccl-span> directly (host 403s this proxy) or an NSE Clearing SPAN methodology PDF. |
| M-2 | Daily MTM cash-settlement mechanics as margin-headroom consumer | Standard mechanism; repo-draft-sourced only (`docs/track2/TRACK2_RISK_MGMT.md:88-89`). | NSE Clearing settlement pages. |
| M-3 | Exact calendar-spread-charge rate (% per month-gap) in NSE SPAN | Not pinned; only the delta-based mechanism + ELM-on-⅓-far-month-value were captured. | NSE Clearing "SPAN Risk Parameters" page / a daily SPAN parameter file. |
| M-4 | Governing effective date of the index calendar-spread expiry-day removal: SEBI text (Feb-2025; repo draft says 2025-02-01) vs implementation sources (2025-02-10, NSE CMPT66047) | Conflicting secondary captures. | Read SEBI/HO/MRD/TPD-1/P/CIR/2024/132 §on calendar spread + NSE CMPT66047 primary text. |
| M-5 | May-2025 circular details: full per-category index-futures limit table; the intraday net ₹5,000 cr / gross ₹10,000 cr FutEq pair as primary text; the "one snapshot 14:45-15:30" + 15-min cure details | EOD option limits (₹1,500/₹10,000 cr) verified via multiple captures; the rest is repo-draft- or single-capture-sourced. | Read SEBI/HO/MRD/TPD-1/P/CIR/2025/79 (21 pp) + the Sep-2025 monitoring circular primary text. |
| M-6 | Pre-2025 CLIENT-level (retail) index-derivatives position-limit rule text (disclosure threshold vs hard cap) | Not pinned; TM/FPI/MF ₹500 cr/15% verified. | NSE position-limits page archive (pre-2025 snapshot). |
| M-7 | Post-2025-10-01 redefined stock MWPL formula (FutEq terms) | Change confirmed to exist; formula not captured. | SEBI 2025/79 circular §MWPL; NSE Clearing "Recent changes in Equity Derivative segment…" deck (403'd this session). |
| M-8 | Current-as-of-2026 freeze values (post NSE/FAOP/71540, eff. 2025-12-01) + NIFTYNXT50 lot size + current BANKEX freeze + any SENSEX lot change after Apr-2025 | Last cross-verified set is 2025-09-01 (NSE) / Apr-2025 (BSE, 50 lots). | Latest NSE FAOP + BSE quantity-freeze circulars (quarterly refresh cadence). |
| M-9 | Exact NSE circular ids/dates for the sliding-dynamic-band + options LPP-during-cooling-off enhancements | Mechanism verified via broker/RegTech summaries. | NSE circular archive (Paytm Money article names the change; TeamLease RegTech lists the circular). |
| M-10 | MWCB per-cell halt matrix times | Matrix reproduced from `docs/track2/TRACK2_RISK_MGMT.md:447-456` and consistent with NSE-page family summaries; per-cell values not re-quoted verbatim from the NSE page this session. | Read <https://www.nseindia.com/products-services/equity-market-circuit-breakers> directly. |
| M-11 | Groww RMS auto-close trigger ("shortfall > ₹5,000 OR MTM risk") | Repo-draft-sourced citation to groww.co/risk-management-policy (`docs/track2/TRACK2_RISK_MGMT.md:404-407`); not re-fetched live. | Fetch the groww.co risk-management policy page. |
| M-12 | Historical evolution of SPAN PSR/exposure parameters (pre-2020 3σ-era values; the SEBI margin-framework-review circular that introduced 6σ·√2 + the 9.3%/14.2% floors) | Current values verified; history not. | SEBI "Review of Margin Framework for Cash & Derivatives" circular (2020) + NSE Clearing circular archive. |

---

## 8. Key sources (session-fetched or search-captured; primary hosts 403 the proxy — see the sourcing preamble above §1)

**Statutory / exchange (primary or mirrored):**
[SEBI CIR/DNPD/6/2010 (European stock options)](https://www.sebi.gov.in/legal/circulars/oct-2010/european-style-stock-options_6846.html) ·
[SEBI physical-settlement circular Dec-2018](https://www.sebi.gov.in/legal/circulars/dec-2018/physical-settlement-of-stock-derivatives_41482.html) ·
[SEBI CIR/2024/132 (Oct-2024 framework)](https://www.sebi.gov.in/legal/circulars/oct-2024/measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html) + [CSE mirror](https://www.cse-india.com/upload/upload/Oct_011024.pdf) ·
[SEBI CIR/2025/76 (Tue/Thu expiry)](https://www.sebi.gov.in/legal/circulars/may-2025/final-settlement-day-expiry-day-for-equity-derivatives-contracts_94189.html) ·
[SEBI CIR/2025/79 (FutEq limits) mirror](https://www.cse-india.com/upload/upload/May_292025.pdf) ·
[SEBI Mar-2025 intraday monitoring](https://www.sebi.gov.in/legal/circulars/mar-2025/intraday-monitoring-of-position-limits-for-index-derivatives_93123.html) ·
[SEBI Sep-2025 intraday framework](https://www.sebi.gov.in/legal/circulars/sep-2025/framework-for-intraday-position-limits-monitoring-for-equity-index-derivatives_96376.html) ·
[NSE FAOP39894 (NIFTY weeklies) mirror](https://www.smifs.com/files/downloads/636826435803084840_FAOP39894%2007Jan2019%20Introduction%20of%20Weekly%20Option%20Contracts%20on%20NIFTY%2050%20Index.pdf) ·
[NSE/FAOP/47856 (Jul-2021 lots) mirror](https://www.rkglobal.net/upload/637601378287808566_Revised%20Market%20Lot.pdf) ·
[NSE FAOP64625 (Nov-2024 lots)](https://nsearchives.nseindia.com/content/circulars/FAOP64625.pdf) ·
[NSE FAOP70616 (Jan-2026 lots)](https://nsearchives.nseindia.com/content/circulars/FAOP70616.pdf) ·
[NSE FAOP68834 (freeze)](https://nsearchives.nseindia.com/content/circulars/FAOP68834.pdf) ·
[NSE CMPT66047 (calendar-spread removal)](https://nsearchives.nseindia.com/content/circulars/CMPT66047.pdf) ·
[NSE FATAX/56235 (FA-2023 STT)](https://archives.nseindia.com/content/circulars/FATAX56235.pdf) ·
[NSE FA64232 (Oct-2024 ETC)](https://nsearchives.nseindia.com/content/circulars/FA64232.pdf) ·
[NSE STT page](https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax) ·
[NSE Clearing settlement mechanism](https://www.nseindia.com/products-services/equity-derivatives-settlement-mechanism) ·
[NSE Clearing settlement price](https://www.nseclearing.in/clearing-settlement/equity-derivatives/settlement-price) ·
[NSE Clearing NSCCL-SPAN](https://www.nseclearing.in/risk-management/equity-derivatives/nsccl-span) ·
[NSE Clearing margins](https://www.nseclearing.in/risk-management/equity-derivatives/margins) ·
[NSE Clearing penalties](https://www.nseclearing.in/clearing-settlement/equity-derivatives/penalties) ·
[NSE Clearing position limits](https://www.nseclearing.in/risk-management/equity-derivatives/position-limits) ·
[NSE sec-ban/MWPL](https://www.nseindia.com/static/products-services/equity-derivatives-risk-management-sec-ban) ·
[NSE violations (ban penalty)](https://www.nseindia.com/products-services/equity-derivatives-violations) ·
[NSE circuit breakers](https://www.nseindia.com/products-services/equity-market-circuit-breakers) ·
[NSE price-band flex FAQ](https://www.nseindia.com/trade/price-band-operating-range-flex-faqs) ·
[NSE derivatives price bands](https://www.nseindia.com/static/products-services/equity-derivatives-price-bands) ·
[NSE NIFTY 50 F&O product page](https://www.nseindia.com/static/products-services/equity-derivatives-nifty50).

**Secondary (brokers / press; used for cross-checks and where primary
403'd):** Zerodha Z-Connect / Varsity / Marketintel bulletins
(291849 · 358889 · 355145 · 362215 · 407122 · 412395 · 429705 · 432323 ·
437296 · 351407), Zerodha support pages ·
[Zerodha "STT Trap"](https://zerodha.com/z-connect/trending/stt-options-nse-bse-mcx-sx) ·
[Zerodha Oct-2024 charges revision](https://zerodha.com/z-connect/business-updates/revision-in-exchange-transaction-charges-and-securities-transaction-tax-from-october-1-2024) ·
[Zerodha Oct-2019 delivery policy](https://zerodha.com/z-connect/general/policy-on-settlement-of-compulsory-delivery-derivative-contracts-update-oct-2019) ·
[Zerodha stock calendar-spread extension 2026](https://zerodha.com/z-connect/updates/removal-of-calendar-spread-margin-benefit-for-single-stock-derivatives-on-expiry-day-from-may-5-2026) ·
Business Standard (2016 · 2019 · 2021 · 2023 · 2024 · 2025 pieces cited
inline) · Business Today · BusinessToday DNE scrap · Motilal Oswal ·
5paisa · Angel One · HDFC Securities · Paytm Money · Upstox · India
Infoline · Bigul · Kotak Neo · Sahi · Zeebiz · Groww blog/help ·
Flattrade · Teji Mandi · ICICI Direct · Fincash ·
[Nirmal Bang CTM FAQ](https://www.nirmalbang.com/Annexure-1.pdf) ·
[Rupeezy STT-on-exercise](https://rupeezy.in/blog/stt-calculation-changed-for-options) ·
[ClearTax STT](https://cleartax.in/s/securities-transaction-tax-stt) ·
[Wikipedia STT](https://en.wikipedia.org/wiki/Securities_Transaction_Tax) ·
[Kitco/Reuters FA-2023 hike](https://www.kitco.com/news/off-the-wire/2023-03-24/update-2-india-hikes-securities-transaction-tax-futures-options) ·
[Outlook Money Apr-2026 hike](https://www.outlookmoney.com/invest/stt-hike-from-april-1-2026-budget-what-it-means-for-futures-and-options-traders) ·
[PL India Apr-2026](https://www.plindia.com/news/stt-hike-on-fo-in-april-2026-what-can-traders-expect/) ·
[TradeSmart STT computation](https://tradesmartonline.in/help/trading/how-is-the-stt-computed-in-case-an-in-the-money-option-contract-expires/) ·
[TaxGuru physical-settlement summary](https://taxguru.in/sebi/mandatory-physical-settlement-stock-derivatives.html) ·
[TaxGuru Oct-2024 framework summary](https://taxguru.in/sebi/sebi-enhances-equity-index-derivatives-framework-investor-protection.html) ·
[SCC Online Oct-2024 legal note](https://www.scconline.com/blog/post/2024/10/03/measures-to-strengthen-equity-index-derivatives-framework-legal-news/) ·
[SEBI expiry-day study via TaxGuru](https://taxguru.in/sebi/sebi-releases-study-equity-derivatives-trends.html) ·
[OptionX FSP explainer](https://optionx.trade/blogs/expiry-day-vwap-settlement-index-options-trading) ·
[TeamLease RegTech NIFTY strike-scheme note](https://teamleaseregtech.com/updates/article/49219/nse-notified-regarding-the-changes-in-the-strike-scheme-and-strike-int/) ·
[SteelCity BANKNIFTY strike-scheme mirror](https://www.steelcitynettrade.com/Circulars/Revision%20in%20Scheme%20of%20Strikes%20in%20BANKNIFTY%20Options.pdf) ·
[SteelCity net-settlement guidelines mirror](https://www.steelcitynettrade.com/Circulars/Guidelines%20for%20Net%20Settlement%20of%20Capital%20Market%20segment%20and%20Futures%20&%20Options%20(F&O).pdf) ·
[Paytm Money peak margin](https://www.paytmmoney.com/blog/sebi-peak-margin-norms/) ·
[Bramesh peak-margin phase 3](https://www.brameshtechanalysis.com/2021/06/01/all-about-phase-3-of-sebis-peak-margin-rule/) ·
[BS 75% peak margin](https://www.business-standard.com/article/markets/trading-volumes-likely-to-be-hit-as-75-peak-margin-kicks-in-today-121060100047_1.html) ·
[Zerodha margin penalties](https://zerodha.com/z-connect/general/margins-margin-penalties-when-trading-with-leverage) ·
[Groww delivery-margin help](https://groww.in/help/stocks,-f&o,-ipo-&-mtf/sx-fno/what-is-groww-s-policy-on-delivery-margins-for-delivery-settlement-in-equity-derivatives--82) ·
[ProStocks delivery-margin FAQ](https://www.prostocks.com/faq/21-margin/220-how-additional-physical-delivery-margin-on-long-in-the-money-stock-options-contract-itm-works-during-friday-friday-before-expiry-thursday-to-expiry-thursday.html) ·
[Angel One Sep-2025 freeze](https://www.angelone.in/news/market-updates/nse-changes-quantity-freeze-limits-for-index-derivatives-starting-september-1-2025) ·
[ICICI Direct freeze](https://www.icicidirect.com/futures-and-options/articles/revised-quantity-freeze-limits-for-nse-index-derivatives) ·
[Goodreturns Dec-2025 freeze](https://www.goodreturns.in/news/nse-revises-quantity-freeze-limits-for-index-f-o-contracts-from-dec-1-check-updated-limits-impact-1473127.html) ·
[Zerodha SENSEX 50-lots](https://x.com/zerodhaonline/status/1665649469906354176) ·
[BusinessToday FutEq/MWPL](https://www.businesstoday.in/markets/story/sebi-equity-fo-rules-futeq-method-mwpl-risk-management-478321-2025-05-29) ·
[ICICI Direct intraday monitoring](https://www.icicidirect.com/futures-and-options/articles/intraday-monitoring-of-position-limits-for-index-derivatives-a-new-sebi-initiative) ·
[K&K position-limits guide](https://ksandk.com/newsletter/sebi-position-limits-index-derivatives-guide/) ·
[BusinessToday Apr-1 monitoring, penalties delayed](https://www.businesstoday.in/markets/top-story/story/sebi-sets-new-guidelines-for-intraday-index-derivative-monitoring-from-april-1-delays-penalties-469885-2025-03-28) ·
[Mastertrust MWPL explainer](https://www.mastertrust.co.in/blog/mwpl-open-position-limit-ban-period) ·
[Nirmal Bang ban impact](https://www.nirmalbang.com/knowledge-center/future-and-options-ban-impact.html) ·
[Paytm Money sliding bands + LPP](https://www.paytmmoney.com/blog/nse-enhances-stability-new-dynamic-price-bands-changes-in-limit-price-protection-for-options-during-cooling-off-period/) ·
[Zerodha price-band FAQ](https://support.zerodha.com/category/trading-and-markets/trading-faqs/general/articles/what-does-circuit-limits-i-e-price-bands-mean) ·
[Navia expiry-ELM](https://support.navia.co.in/support/solutions/articles/1000326062) ·
[Zerodha expiry-ELM](https://support.zerodha.com/category/trading-and-markets/margins/margin-leverage-and-product-and-order-types/articles/additional-elm-for-index-expiry) ·
[Zerodha physical-settlement policy](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/policy-on-physical-settlement) ·
[Zerodha Varsity physical settlement](https://zerodha.com/varsity/chapter/quick-note-on-physical-settlement-2/) ·
[ICICI Direct physical delivery](https://www.icicidirect.com/futures-and-options/articles/physical-delivery-of-futures-and-options-stock-contracts) ·
[ICICI Direct new STT rules](https://icicidirect.com/research/equity/finace/new-stt-rules-in-futures-and-options-trading) ·
[ICICI Direct Budget-2026 STT](https://www.icicidirect.com/futures-and-options/articles/stt-changes-in-budget-2026-what-f-o-traders-need-to-know) ·
[Fincash DNE reinstatement](https://www.fincash.com/l/nse-reintroduces-do-not-exercise-facility) ·
[Angel One DNE withdrawal](https://www.angelone.in/knowledge-center/futures-and-options/nse-withdraws-do-not-exercise-facility-for-brokers) ·
[Upstox DNE discontinued](https://upstox.com/market-talk/nse-discontinues-dne-facility/) ·
[Outlook Business DNE](https://www.outlookbusiness.com/news/nse-to-discontinue-do-not-exercise-dne-facility-for-stock-options-from-friday-how-should-traders-approach-the-new-setup-news-274319) ·
[BS NSE European launch](https://www.business-standard.com/article/markets/nse-to-offer-european-style-stock-options-from-today-111010300043_1.html) ·
[BusinessToday European options 2011](https://www.businesstoday.in/magazine/focus/story/nse-to-introduce-european-options-from-january-2011-20197-2011-02-03) ·
[TaxGuru CIR/DNPD/6/2010 copy](https://taxguru.in/sebi/sebi-circular-on-european-style-stock-options-cirdnpd62010-october-27-2010.html) ·
[Bajaj Broking Oct-2024 charges](https://www.bajajbroking.in/blog/nse-and-bse-revise-transaction-charges-from-october-2024) ·
[mstock rollovers](https://www.mstock.com/mlearn/stock-market-courses/option-basics/option-expiry-importance-of-duration-and-rollovers) ·
[Upstox strike prices](https://upstox.com/uplearn/structure-courses/basics-of-options/strike-prices/) ·
[Bigul SENSEX/BANKEX spec guide](https://bigul.co/blog/indices/sp-bse-sensex-and-bankex-derivatives-a-comprehensive-guide) ·
[fatafatniftylevels 2026 calendar](https://fatafatniftylevels.in/holiday-expiry-2026.php) ·
[Flattrade lot revision](https://flattrade.in/kosh/nse-revises-derivatives/) ·
[Groww NSE weekly discontinuation blog](https://groww.in/blog/nse-to-discontinue-weekly-derivative-contracts) ·
[Groww lot-increase blog](https://groww.in/blog/nse-increases-lot-sizes-for-index-derivatives) ·
[Groww F&O hours extension blog](https://groww.in/blog/nse-extends-f-and-o-trading-hours-by-10-minutes-new-timings-effective-from-august-3-2026) ·
[Flattrade F&O 15:40 close](https://flattrade.in/kosh/nse-extends-fo-trading-hours-till-340-pm-from-august-3-2026-what-it-means-for-traders/) ·
[Teji Mandi F&O close](https://tejimandi.com/blog/feature-articles/nse-extends-fo-market-closing-time-to-340-pm-from-aug-3) ·
[Groww weekly-vs-monthly help](https://groww.in/help/stocks,-f&o,-ipo-&-mtf/sx-fno/what-are-weekly-and-monthly-options--99) ·
[Fyers MIDCPNIFTY expiry notice](https://fyers.in/notice-board/revision-in-expiry-day-for-midcpnifty-with-effect-from-17th-august-2023/) ·
[HDFC Sec Nov-2024 lots](https://www.hdfcsec.com/blog/details/change-in-the-lot-size-of-index-derivative-for-weekly-monthly-quarterly-and-half-yearly-expiry-) ·
[Paytm Money BANKNIFTY expiry revision](https://www.paytmmoney.com/blog/revision-in-expiry-day-of-nifty-bank-monthly-and-quarterly-contract-cycle/) ·
[Paytm Money NSE/BSE lot changes](https://www.paytmmoney.com/blog/changes-in-lot-sizes-of-derivatives-contracts-on-nse-and-bse/) ·
[Bajaj Broking BSE expiry revision](https://www.bajajbroking.in/blog/bse-revises-expiry-days-for-index-derivatives-contracts-from-2025) ·
[Angel One BSE expiry revision](https://www.angelone.in/news/market-updates/bse-revised-expiry-days-for-sensex-bankex-and-sensex-index-derivatives) ·
[Angel One Jan-2026 lots](https://www.angelone.in/news/market-updates/nse-revises-lot-sizes-for-index-derivatives-from-december-2025-nifty-lot-size-to-reduce-to-65) ·
[Sahi BANKNIFTY lots](https://www.sahi.com/blogs/nse-hikes-lot-sizes-for-bank-nifty) ·
[Sahi 2026 lots listicle](https://www.sahi.com/blogs/nifty-lot-size-2026-bank-nifty-sensex) ·
[Sahi expiry shake-up](https://www.sahi.com/blogs/nse-vs-bse-expiry-shake-up-what-traders) ·
[Upstox BANKNIFTY lot cut](https://upstox.com/market-talk/bank-niftys-lot-size-reduced/) ·
[Upstox FINNIFTY Oct-2021](https://upstox.com/announcements/risk-management/nse-update-on-finnifty-derivative-contracts/) ·
[India Infoline BANKNIFTY Wed](https://www.indiainfoline.com/news/markets/nse-shifts-bank-niftys-weekly-expiry-to-wednesday-effective-september-4-2023) ·
[ICICI Direct MIDCPNIFTY launch](https://www.icicidirect.com/knowledge-center/article/nse-to-launch-futures-and-options-for-nifty-midcap-index-from-24th-jan) ·
[NSE FINNIFTY launch](https://www.nseindia.com/nse-to-launch-derivatives-on-the-nifty-financial-services-index-from-january-11-2021) ·
[NSE NIFTYNXT50 launch](https://www.nseindia.com/static/resources/nse-to-launch-derivatives-on-nifty-next-50-index-niftynxt50-from-april-242024) ·
[Zeebiz NIFTYNXT50](https://www.zeebiz.com/market-news/news-nifty-next-50-futures-options-derivatives-contracts-to-be-launched-on-this-date-nse-circular-niftynxt50-latest-news-update-285074) ·
[Zeebiz BANKNIFTY last weekly](https://www.zeebiz.com/market-news/news-bank-nifty-weekly-expiry-ends-today-volume-future-options-turnover-volatility-nifty-bank-nifty-indices-325884) ·
[Zeebiz 2026-07-16 SENSEX weekly](https://www.zeebiz.com/market-news/video-gallery-nifty-bank-nifty-prediction-16-july-2026-sensex-weekly-expiry-398915) ·
[BS BANKEX Monday](https://www.business-standard.com/markets/news/from-october-16-bankex-derivatives-expiry-date-to-shift-to-monday-123101300210_1.html) ·
[BS BSE relaunch](https://www.business-standard.com/markets/news/bse-relaunches-sensex-bankex-derivatives-contracts-to-boost-trading-123051501206_1.html) ·
[BS NSE defers Monday](https://www.business-standard.com/markets/news/nse-defers-switching-to-monday-expiry-after-sebi-s-consultation-paper-125032800183_1.html) ·
[BS Nov-21 first session](https://www.business-standard.com/markets/news/new-f-o-rule-to-come-into-effect-from-thursday-nov-21-here-s-what-changes-124112000292_1.html) ·
[BS Mar-2025 lots](https://www.business-standard.com/markets/capital-market-news/nse-revises-market-lot-sizes-for-nifty-bank-nifty-midcap-select-f-o-contracts-125033100440_1.html) ·
[BS Thursday adieu](https://www.business-standard.com/markets/news/nse-bids-adieu-to-thursday-expiry-as-dates-swap-come-into-effect-explained-125082800635_1.html) ·
[BS 2016 BANKNIFTY weekly launch](https://www.business-standard.com/article/pti-stories/nse-to-launch-weekly-options-contracts-on-bank-nifty-index-116050501487_1.html) ·
[BS 2019 NIFTY weekly](https://www.business-standard.com/article/markets/introduction-of-weekly-settlement-for-nifty-contracts-a-boost-for-punters-119013100022_1.html) ·
[Business Today BANKNIFTY 15](https://www.businesstoday.in/amp/markets/top-story/story/nse-reduces-bank-nifty-lot-size-to-15-from-july-2023-monthly-expiry-375658-2023-03-31) ·
[Business Today BSE relaunch](https://www.businesstoday.in/amp/markets/top-story/story/bse-relaunches-sensex-bankex-derivative-contracts-changes-expiry-cycle-reduces-lot-size-381394-2023-05-15) ·
[Motilal Oswal Apr-2024 lots](https://www.motilaloswal.com/learning-centre/2024/4/revised-lot-size-for-nifty-fin-nifty-and-nifty-midcap-contracts-and-what-does-it-mean-for-you) ·
[Motilal Oswal weekly discontinuation](https://www.motilaloswal.com/learning-centre/2024/10/nse-discontinues-weekly-derivatives-on-bank-nifty-nifty-midcap-select-and-finnifty) ·
[Bigul Jan-2025 realignment](https://bigul.co/blog/market-update/nse-revises-major-changes-in-banknifty-finnifty-expiry-days-effective-january-2025) ·
[Kotak Neo Thursday-end](https://www.kotakneo.com/news/market-news/sebi-to-end-thursday-expiry/) ·
[5paisa Nov-2024 lots](https://www.5paisa.com/news/nse-revises-lot-sizes-for-key-index-derivatives-starting-november-2024) ·
[5paisa expiry reshuffle](https://www.5paisa.com/news/sebi-reshuffles-equity-derivatives-expiry-days-to-streamline-trading) ·
[Zerodha weekly-discontinued support](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/weekly-expiries-discontinued-for-certain-index-derivatives) ·
[Zerodha FINNIFTY lot X-post](https://x.com/zerodhaonline/status/1974363648974881113).

---

*Assembled from the three microstructure research parts (expiry/grids/lots ·
settlement/STT/exercise · margins/limits/bans/freeze/circuits) of the
2026-07-19 multi-agent session. All repo citations re-checked at the branch
base SHA; all UNVERIFIED tags preserved and consolidated in §7.*



