# 10 — Seller Edge & Execution Evidence: the Variance Risk Premium, the Indian counterparty, and defense-time fill realism

> **Status: RESEARCH / DESIGN-READINESS ONLY — zero engine code.** The sweep engine remains
> **SPOT-ONLY** (DEC-ENGINE-SPOT-ONLY-001) and options remain **REFERENCE_ONLY**; any activation is
> a future operator-gated DEC/ADR.
> **Date:** 2026-07-19
> **Provenance:** multi-agent deep-research session, docs-only branch
> `claude/options-strategies-design`, draft PR #1725 (DEC-OPTION-SELLING-RESEARCH-001, #2210)
> carries this research pair.
>
> **Citation base — how to resolve every `[REPO path:line]` cite.** Chapters `01`–`10` + the
> README pin every `brutex/` / `rust/` / non-design `docs/` `path:line` citation to base SHA
> `184a48a4abb0ec2fd61906499a6a799a752abd7e` (the `origin/main` tip the branch was cut from) —
> resolve each with `git show 184a48a4:<path>`, NOT the head working tree, which has DRIFTED
> (the two `origin/main` main-sync merges `dbf2ee6`/`ade2427` shifted many cited line numbers;
> e.g. `catalog.py get_bars` :477→:501). Chapter 11 (STUDY.md) carries its own citation basis.
>
> **Evidence tiers used throughout:** **[REPO path:line]** (verified by reading the file in the
> research container) · **[WEB source]** (externally verified in-session, URL given) ·
> **[EXT-SNIPPET url]** (external claim surfaced via search-result snippets ONLY — the underlying
> page returned HTTP 403 through this environment's proxy; treat as SECONDARY, never quote onward
> as page-verified) · **[MEASURED]** (command + output from THIS container) · **[UNVERIFIED]**
> (tagged inline + consolidated in §7). Every number that must come from OUR lake is labelled
> **PENDING-ON-BOX** — the research container has NO lake (proof in §4.0).

---

## 1. The variance-risk-premium evidence review

### 1.1 The core global empirical fact

Implied (risk-neutral) variance persistently exceeds subsequently realized variance on equity
indices. The option/variance-swap seller is paid this spread on average; the buyer pays it as
insurance. This is one of the most replicated facts in empirical option pricing.

| Strand | Canonical work | Finding | Tier |
|---|---|---|---|
| Variance swaps | Carr & Wu, "Variance Risk Premiums", *Review of Financial Studies* 22(3), 2009, pp. 1311–1341 | Synthetic variance-swap rates (a specific option portfolio) are systematically ABOVE ex-post realized variance on five stock indexes (less uniformly on 35 single stocks) — a large negative variance risk premium for the long-variance side, i.e. positive carry for the seller | [WEB: [OUP RFS abstract](https://academic.oup.com/rfs/article-abstract/22/3/1311/1581057), [NYU PDF](https://engineering.nyu.edu/sites/default/files/2019-01/CarrReviewofFinStudiesMarch2009-a.pdf)] |
| Delta-hedged option returns | Bakshi & Kapadia, "Delta-Hedged Gains and the Negative Market Volatility Risk Premium", *RFS* 16(2), 2003, pp. 527–566 | Buy-option-and-delta-hedge portfolios UNDERPERFORM zero on average ⇒ a negative market volatility risk premium; underperformance is larger at higher volatility and near the money — i.e. the hedged option WRITER earns the mirror-image premium | [WEB: [OUP RFS](https://academic.oup.com/rfs/article-abstract/16/2/527/1579962), [UMass PDF](https://people.umass.edu/~nkapadia/docs/Bakshi_and_Kapadia_2003_RFS.pdf)] |
| Put expensiveness | Bondarenko, "Why Are Put Options So Expensive?" (SSRN; later *Quarterly Journal of Finance*) | Index puts appear systematically overpriced relative to rational-pricing benchmarks | [UNVERIFIED — well-known paper, not fetched this session; verify at SSRN abstract 375784] |
| Put-write benchmark history | Bondarenko, "Historical Performance of Put-Writing Strategies", CBOE-commissioned study, 2019 | Multi-decade PUT/WPUT index history: put-writing captured premium with lower volatility and shallower drawdowns than the S&P 500 | [WEB: [CBOE PDF](https://cdn.cboe.com/resources/education/research_publications/PutWriteCBOE19_v14_by_Prof_Oleg_Bondarenko_as_of_June_14.pdf), [SSRN](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID3393940_code246693.pdf?abstractid=3393940&mirid=1)] |
| Covered-call benchmark (BXM) | Whaley, *Journal of Derivatives* 2002; Ibbotson Associates 2004; Callan Associates 2006 | Ibbotson: BXM vs S&P 500 Jun-1998→Mar-2004 — slightly higher annualized return at ~2/3 the volatility. Callan: Jun-1988→Aug-2006 BXM 11.77%/yr at 9.29% stdev vs S&P 500 11.67%/yr at 13.89% stdev | [WEB: [BXM Wikipedia](https://en.wikipedia.org/wiki/CBOE_S%26P_500_BuyWrite_Index), [Callan summary](https://www.borntosell.com/covered-call-blog/evaluation-of-buy-write-strategy), [Cboe/Wilshire 2019 benchmark study](https://cdn.cboe.com/resources/spx/wilshire-options-based-benchmark-indexes-2019.pdf)] |
| VRP predicts returns | Bollerslev, Tauchen & Zhou, "Expected Stock Returns and Variance Risk Premia", *RFS*, 2009 | The IV−RV spread itself forecasts equity returns — the premium is time-varying, largest after stress | [UNVERIFIED — canonical citation from prior knowledge, not fetched this session; verify at RFS 22(11)] |
| Option-return decomposition | Broadie, Chernov & Johannes, "Understanding Index Option Returns", *RFS*, 2009 | Raw short-option returns/Sharpes are hard to interpret: high average short-put returns are compatible with fair compensation for jump/crash risk under plausible models — a warning against naive edge claims | [UNVERIFIED — canonical citation, not fetched this session] |
| Practitioner synthesis | Israelov & Nielsen (AQR), "Covered Calls Uncovered" / "Understanding the Volatility Risk Premium" | The VRP is compensation for bearing volatility/crash risk; harvesting it requires explicit risk budgeting, not naive Sharpe-maximizing | [UNVERIFIED — well-known AQR papers, not fetched this session] |

### 1.2 The caveats — the premium's price (design inputs, not footnotes)

1. **Crash risk IS the premium.** Short-premium P&L is short a jump: decades of small wins fund
   rare catastrophic losses. Bakshi–Kapadia explicitly find the writer's edge is largest exactly
   when volatility is high — the compensation concentrates where the danger concentrates [WEB:
   Bakshi–Kapadia above]. Broadie–Chernov–Johannes show apparent short-put "alpha" can be fully
   explained as jump-risk compensation [UNVERIFIED tier as above].
2. **Sharpe illusion.** Truncated-right / fat-left return distributions inflate in-sample Sharpe;
   a short-vol book can print a multi-year Sharpe > 2 and still have negative full-cycle
   expectancy if the sample missed its tail. Any Sharpe computed on a window that excludes a crash
   regime is upward-biased by construction. (General statistical point; the specific manipulation
   literature — Goetzmann et al. — is [UNVERIFIED] this session.)
3. **Skew / asymmetric premium.** The premium is not uniform across strikes: index put skew means
   the OTM-put wing carries the densest premium AND the densest crash exposure; symmetric
   structures (straddle/strangle) mix two differently-priced wings. Per-strike VRP measurement,
   not a single ATM number, is required for structure design. (Direction established by the
   skew/put-expensiveness strand above; per-strike NIFTY numbers are **PENDING-ON-BOX**.)
4. **Margin/path risk is invisible in expiry-to-expiry studies.** The CBOE indices are
   fully-collateralized and rebalanced monthly; a leveraged Indian seller can be stopped out
   intra-path (MTM + margin spiral) on a trade that ends profitable at expiry. Our backtest must
   simulate the path — we hold the 1-minute lake for exactly this — not just terminal payoffs.

### 1.3 India-specific evidence

**India VIX — the instrument we already store.** India VIX is computed by NSE from best bid-ask
quotes of OTM near/mid-month NIFTY option contracts using the CBOE VIX methodology (adapted;
cubic-spline interpolation for missing strikes); "VIX" is licensed from CBOE/S&P. It is the
30-calendar-day expected-volatility benchmark for the Indian market [WEB:
[NSE India VIX page](https://www.nseindia.com/static/products-services/indices-indiavix-index),
[NSE white paper PDF](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/white_paper_IndiaVIX.pdf)].
In OUR repo it is a first-class REFERENCE_ONLY series: `INDIA_VIX_SYMBOL: Final[str] = "NSE-INDIAVIX"`
[REPO brutex/adapters/groww/constants.py:103], stamped on every observable trade as
`vix_at_entry`/`vix_at_exit` (DEC-VIX-STAMP-WIRE-001; loaded via `build_vix_stamper`
[REPO brutex/engine/reference_wire.py:266] over `load_lake_slice`
[REPO brutex/engine/lake_slice.py:48]). It is NEVER a vocabulary bit (CLAUDE.md §2 Rule 3). This
gives VRP measurement a ready-made regime axis with zero new plumbing.

**Published Indian VRP evidence:**

| Claim | Evidence | Tier |
|---|---|---|
| A positive, statistically significant VRP exists on NIFTY (implied variance regularly exceeds realized), positive ~74% of days over a 5-year daily sample of NIFTY 50 + India VIX | "Does the variance risk premium (VRP) from NIFTY options drive excess returns in a volatility-selling strategy?", *Economic Sciences* (2025) | [WEB: [journal page](https://economic-sciences.com/index.php/journal/article/view/356)] — a single, non-top-tier journal; SUPPORTIVE, not established. The 74% figure and sample must be re-derived from OUR lake before any design claim |
| Same study: the premium is harvestable via short India-VIX futures / delta-hedged ATM straddle selling but "vulnerable to significant drawdowns during market stress" | same | [WEB, same caveat] |
| Older/other Indian academic VRP or IV-vs-RV studies (e.g. Garg & Vipul on India VIX forecasting) | not fetched this session | [UNVERIFIED — SSRN/Scopus sweep would verify; nothing here depends on them] |

**Honest summary:** the India-specific ACADEMIC evidence base is thin compared to the US. The
global mechanism (insurance demand → IV > subsequent RV) plus the Indian structural flow story
(§2) make a positive NIFTY/BANKNIFTY VRP the **default hypothesis** — but for THIS project it is
a hypothesis to be **measured from our own lake** (§4), not imported.

---

## 2. The SEBI retail-loss studies — and what they do and do not imply

### 2.1 The retail option-buying boom (the seller's structural counterparty)

NSE has been the world's largest derivatives exchange by contract count for five consecutive
years through CY2023 (FIA statistics); Indian equity index options reached **84.3 billion
contracts in 2023, +153% YoY** [WEB:
[NSE/BusinessWire](https://www.businesswire.com/news/home/20240118034299/en/NSE-is-the-Worlds-Largest-Derivative-Exchange-for-Fifth-Consecutive-Year),
[FIA](https://www.fia.org/fia/articles/global-futures-and-options-volume-hits-record-137-billion-contracts-2023),
[FIA explainer](https://www.fia.org/marketvoice/articles/explainer-meteoric-rise-indias-equity-derivatives-volume)].
The flow is dominated by small-notional, short-dated (weekly/0DTE) long-premium retail trades —
structurally the counterparty a systematic seller wants.

### 2.2 The loss studies — the empirical footprint of that flow

| Study | Window | Headline | Tier |
|---|---|---|---|
| SEBI "Analysis of Profit and Loss of Individual Traders dealing in Equity F&O Segment", Jan 2023 | FY22 | **89%** of individual equity-F&O traders lost money | [WEB: [SEBI report page](https://www.sebi.gov.in/reports-and-statistics/research/jan-2023/study-analysis-of-profit-and-loss-of-individual-traders-dealing-in-equity-fando-segment_67525.html)] (the widely-quoted ~₹1.1 lakh FY22 average loss was NOT confirmed in this session's fetches — [UNVERIFIED figure]) |
| SEBI updated study, Sep 2024 | FY22–FY24 | **92.8–93%** of >1 crore individual traders lost; aggregate loss **>₹1.8 lakh crore** (~₹2 lakh avg/trader); FY24 alone ~₹75,000 crore net individual loss; proprietary traders + FPIs on the other side booked FY24 gross profits of ~₹33,000 crore and ~₹28,000 crore respectively (largely algo-driven) | [WEB: [SEBI press release](https://www.sebi.gov.in/media-and-notifications/press-releases/sep-2024/updated-sebi-study-reveals-93-of-individual-traders-incurred-losses-in-equity-fando-between-fy22-and-fy24-aggregate-losses-exceed-1-8-lakh-crores-over-three-years_86906.html), [Business Standard](https://www.business-standard.com/markets/capital-market-news/sebi-study-exposes-massive-losses-for-individual-f-o-traders-in-india-124092400948_1.html)] |
| SEBI study, Jul 2025 | FY25 | **~91%** of individual traders lost; net individual loss **₹1,05,603 crore** (up from ₹74,812 crore FY24); unique individual F&O traders fell 61.4 lakh (Q1 FY25) → 42.7 lakh (Q4 FY25); index-options turnover −9% (premium) / −29% (notional) YoY | [WEB: [Business Standard](https://www.business-standard.com/markets/news/net-losses-of-traders-in-fo-widens-in-fy25-sebi-study-125070701221_1.html)] |

**What the studies DO imply for the seller edge:** a persistent aggregate transfer FROM
predominantly long-premium retail TO the (institutional/prop, largely option-writing and
market-making) other side is consistent with a fat, harvestable premium in Indian index options.

**What they do NOT imply:** proof that any specific selling strategy is profitable — the winners'
P&L includes market-making spread capture and cross-segment hedging, not pure VRP; and the loser
percentage says nothing about the tail borne by the sellers on the days retail's lottery tickets
pay off.

### 2.3 The regulatory risk is first-order, not a footnote

The same studies are SEBI's stated motivation for actively shrinking this market. The
Oct-1-2024 six-measure framework: one weekly-expiry index per exchange (from Nov 20, 2024 —
killed BANKNIFTY/FINNIFTY/MIDCPNIFTY weeklies), minimum contract value raised to ₹15–20 lakh
(from Nov 21, 2024), **+2% Extreme Loss Margin on SHORT option positions on expiry day** (a
direct tax on 0DTE selling), calendar-spread margin benefit removed on expiry day (Feb 10, 2025),
upfront premium collection from buyers, intraday position-limit monitoring (Apr 1, 2025)
[WEB: [Zerodha Z-Connect summary](https://zerodha.com/z-connect/business-updates/sebis-new-rules-for-index-derivatives-heres-whats-changing)].
FY25 already shows the intended shrinkage (trader count −30%, turnover −29% notional).

**DESIGN CONSEQUENCE:** every selling structure must carry a REGIME/date-validity axis (which
expiries existed when; what margin regime applied) — the 2021–2024 weekly-expiry paradise is
partly non-repeatable, and any backtest spanning Nov-2024 crosses a structural break. This
mirrors the repo's existing dated cost-regime refusal discipline (DEC-ETC-REGIME-HISTORY-001:
unverified windows REFUSE, never fabricate — [REPO brutex/costs/ per CLAUDE.md §1 #1524]).

---

## 3. Win-rate vs tail-loss profiles per structure — published priors only

| Structure | Published profile | Source / tier |
|---|---|---|
| Cash-secured put writing (CBOE PUT, monthly ATM, fully collateralized) | Multi-decade: equity-like returns at ~2/3 volatility; shallower drawdowns than the index — one summary: 10-yr max drawdown **−32.7% (PUT) vs −50.9% (S&P 500)** (window includes 2008) | [WEB: [PortfoliosLab PUT page](https://portfolioslab.com/symbol/%5EPUT) for the drawdown pair — secondary source, treat figures as indicative; [Bondarenko/CBOE 2019](https://cdn.cboe.com/resources/education/research_publications/PutWriteCBOE19_v14_by_Prof_Oleg_Bondarenko_as_of_June_14.pdf) and [Neuberger Berman PutWrite paper](https://www.nb.com/documents/public/en-us/uncovering_the_equity_index_putwrite_strategy_ria.pdf) for the primary studies] |
| Covered call (CBOE BXM) | Callan 1988–2006: 11.77%/yr at 9.29% stdev vs S&P 11.67%/yr at 13.89%; Cboe/Wilshire 2019: BXM/BXMD/PUT all superior risk/return vs S&P over 32+ years | [WEB: sources in §1.1 row 5] |
| Short strangle / short straddle (undefined-risk symmetric) | High win-rate / negative-skew profile widely reported by PRACTITIONER research (tastytrade/tastylive studies, Spintwig SPX short-strangle backtests: typical win rates 70–90% with occasional multiple-of-credit losses) — no peer-reviewed benchmark index exists | [UNVERIFIED — practitioner-grade, methodology opaque, survivorship/parameter-selection unknown; named only to set expectations. NOT design inputs] |
| Iron condor / credit spread (defined-risk) | No canonical benchmark index; profiles derive mechanically from the short-strike win rate minus wing cost | [UNVERIFIED — no published benchmark located this session] |
| 0DTE selling | Post-2022 US literature/exchange notes exist but nothing canonical located this session; India-specific expiry-day selling evidence is anecdotal + newly margin-taxed (+2% ELM, §2.3) | [UNVERIFIED] |

**The honest statement (binding for this design pack):** none of these profiles transfers to
NIFTY/BANKNIFTY/SENSEX weekly-cycle selling at Indian costs/margins/expiry microstructure. Win
rate, loss-given-tail, drawdown depth AND duration per structure are outputs OUR engine must
MEASURE from our own lake under the worst-case fill law (DEC-FILL-WORST-CASE-ONLY-001 — entry at
the adverse extreme [REPO per CLAUDE.md §1 #1825]), the verified cost tables, and the cascade
gates — not numbers to import from US monthly-cycle indices. Published profiles serve exactly one
purpose: **sanity-band priors** (if our short-strangle backtest shows a 99% win rate or a Sharpe
of 4, suspect the harness before believing the edge).

---

## 4. THE LAKE MEASUREMENT SPEC (VRP from our own data)

### 4.0 Container probe — [MEASURED]: this environment has NO lake

```
$ echo "BRUTEX_LAKE_ROOT=$BRUTEX_LAKE_ROOT"; ls ~/.brutex/lake 2>/dev/null; \
  ls /home/user/.brutex 2>/dev/null; find / -maxdepth 4 -type d -name 'bars' 2>/dev/null | head -5
BRUTEX_LAKE_ROOT=
---done---
```

Empty env var, no `~/.brutex`, no `bars/` directory anywhere at depth ≤4. The lake lives on the
operator's box. **Therefore NO VRP number is reported in this document**; instead the exact
measurement spec below, pinned to repo APIs, ready to execute on-box. Every resulting figure is
**PENDING-ON-BOX**.

### 4.1 Data prerequisites (verify first, on-box)

Option 1-minute partitions live at
`bars/{exchange}/FNO/{groww_symbol}/{interval}/{yyyy}/{mm}.parquet`
[REPO brutex/data/fno_candle_lake.py:62]; options are 1-minute-NATIVE (no derived TFs —
`is_option_contract_coords` gates the prebuild [REPO brutex/data/fno_candle_lake.py:66],
DEC-OPTIONS-PREBUILD-RETIRE-001). The `iv` overlay exists ONLY on Greeks-enriched partitions —
legacy 7-column partitions read `None`-filled overlays
[REPO brutex/data/greeks_enrich.py:1358-1362]; run `brutex greeks-backfill` first if coverage is
spotty (DEC-GREEKS-BULK-REENRICH-001). The stored `iv` is the repo's Black-76 solve with q=0 and
the stamped `rate_used`/`t_years_used` provenance [REPO brutex/data/models.py:156-167] — an
ATM-IV *proxy*, not India VIX itself.

### 4.2 The spec (read-only; execute on the operator's box)

**(i) Daily ATM-IV proxy series per index (NIFTY, BANKNIFTY, SENSEX):**
1. Enumerate CE/PE contract dirs per underlying; decode strike + expiry with `parse_fno_symbol`
   (right-anchored grammar) [REPO brutex/data/fno_symbol.py:219].
2. For each trading day `d`, choose the target expiry (per DTE bucket, see (iv)); read that day's
   stored enriched 1-minute rows per contract via `read_option_rows(lake_root, target)`
   [REPO brutex/data/greeks_enrich.py:1355] (month-file-bounded, schema-tolerant) — or, for a
   leaner scan, read the month parquet directly at the fno_candle_lake layout and select
   `timestamp, close, iv` (+ `spot_at_bar`).
3. ATM pick at the **15:25 IST** bar (a deliberate convention: 5 minutes before the 15:30 close
   [REPO brutex/calendars/nse.py:117,124], clear of last-minute expiry noise the repo's own
   expiry-decay note documents [REPO brutex/data/greeks_enrich.py:471]): derive the strike grid
   with `derive_strike_step` [REPO brutex/data/moneyness.py:106] over that day's ladder, keep
   contracts with `atm_offset(strike, spot, step) == 0` [REPO brutex/data/moneyness.py:238],
   spot = the stored `spot_at_bar` of that bar. `ATM_IV(d) = mean(iv_CE, iv_PE)` at that bar;
   record `n_legs∈{1,2}` and skip days with 0 legs (count them — never fabricate).
4. Output: one row per (index, day, expiry): `atm_iv, dte_sessions, legs, provenance`.

**(ii) Subsequent realized vol from spot 1-minute bars:**
1. Read the underlying spot 1-minute frame via `make_lake_read_1m`
   [REPO brutex/data/timeframe_materialize.py:963] (bad-tick-screened, price-sanity-gated) or the
   engine mouth `Catalog.get_bars` [REPO brutex/data/catalog.py:477]; a day-bounded alternative is
   `read_day_month_frame` [REPO brutex/data/lake_day_glob.py:106]; window form:
   `make_lake_read_1m_window` [REPO brutex/data/spot_daily_resample.py:1066].
2. Primary estimator (matches the VIX/close-to-close convention): σ_cc = stdev of daily log
   close-to-close returns over the DTE window × √252. Secondary (our granularity edge): realized
   variance from 1-minute log returns summed per session (375 bars/session, 09:15–15:30 grid),
   annualized; report both — the cc-vs-intraday gap is itself informative (overnight gaps are a
   big share of Indian index variance; the 1-minute estimator excludes them).
3. `RV(d→d+DTE)` computed over NSE trading sessions only (the repo calendar is authoritative —
   `calendars/nse.get_session`).

**(iii) VRP distribution:** `VRP_vol(d) = ATM_IV(d) − RV(d→expiry)` (vol points) and
`VRP_var(d) = ATM_IV² − RV²` (variance points, the Carr–Wu form). Report per index: N days, mean,
median, stdev, %positive, p5/p25/p75/p95, min (worst day), the 5 worst episodes with dates, and
the same stats on NON-overlapping windows only (overlapping daily samples autocorrelate ⇒ naive
t-stats are inflated — report Newey-West or non-overlap only). **All PENDING-ON-BOX.**

**(iv) Buckets + regimes:** repeat (iii) per DTE bucket {0–1, 2–7, 8–30, 31–60 sessions} and per
regime: India-VIX terciles from the stored `NSE-INDIAVIX` series
[REPO brutex/adapters/groww/constants.py:103; loader `load_lake_slice`,
brutex/engine/lake_slice.py:48], calendar year, pre/post the Nov-2024 SEBI break (§2.3), and
expiry-day vs non-expiry-day. Also split CE-only vs PE-only ATM IV (skew-direction check).

### 4.3 Known validity limits of the spec (state in every on-box report)

1. Stored `iv` starts only where enrichment ran and the lake floor is 2020-01-01
   (`GROWW_HISTORY_FLOOR` [REPO CLAUDE.md §2 Rule 3]) — the sample INCLUDES the Feb–Mar 2020
   COVID crash and the Jun-4-2024 election-day shock but only ~6 years total: rare-tail
   statistics will be undersampled by construction.
2. The ATM-IV proxy ≠ India VIX (single-strike Black-76 vs an OTM-strip model-free index) —
   correlate the two as a cross-check.
3. Vendor 1-minute bars are last-traded-price snapshots, not quote-mid — expiry-week
   deep-ITM/OTM prints are noisy (the repo's BELOW_INTRINSIC screen exists for this reason
   [REPO brutex/data/greeks_enrich.py:246-249]).

### 4.4 Seller-edge honesty verdict + overfit hazards

**What we can claim TODAY: nothing measured.** No lake in this container; zero India-VRP numbers
of our own exist anywhere in the repo. The global literature (§1) and the Indian structural story
(§2) justify option selling as the PRIMARY research subject — they do not justify a single
strategy, parameter, or expected return. Any design doc that quotes a win rate or Sharpe today is
importing someone else's market.

**What the engine will establish (and only the engine):** per-structure, per-index, per-DTE,
per-regime distributions of win rate, expectancy, loss-given-tail, margin-path drawdown, and the
measured VRP itself (§4.2) — under the worst-case fill law, verified Indian costs, and the frozen
run-identity discipline. Activation of any options execution beyond the existing REFERENCE_ONLY
translation layer is a FUTURE operator-gated DEC/ADR: the sweep engine is SPOT-ONLY today
(DEC-ENGINE-SPOT-ONLY-001 — `is_engine_sweep_symbol` admits exactly NIFTY/BANKNIFTY/SENSEX
[REPO CLAUDE.md §1 #1790]).

**Overfit hazards SPECIFIC to premium selling:**

1. **Rare-tail undersampling.** ~6 years of lake contain a handful of true vol shocks (Mar-2020,
   Jun-2024 election, budget days). A high-win-rate short-premium strategy can pass naive gates
   while its ruin event simply never occurred in-sample. Consequences: (a) never rank selling
   strategies by Sharpe/win-rate alone; (b) tail metrics (worst-5-days, MAE, minute-path
   drawdown — already persisted by the results-depth pass [REPO per CLAUDE.md §1 #1617]) must be
   first-class ranking inputs; (c) stress-window lockboxes should pin the known shock windows.
2. **The existing cascade gates are the counter — name them and use them all** [REPO
   brutex/metrics/cascade.py:12-13, stages enum :160-167]: **DSR** (#2, deflated Sharpe —
   directly attacks the Sharpe illusion), **CPCV** (#3, combinatorially purged CV),
   **Reality-Check/SPA** (#4, pool-level "is the best candidate real or data-snooped", p<0.05
   [REPO brutex/metrics/cascade.py:41]), **PBO** (#5, pool overfit probability), **Romano-Wolf**
   (#6, FWER rejection set), **Walk-Forward** (#7, ≥0.75 of walks), **Lockbox** (#8, held-out
   window, RELATIVE sharpe floor `training_sharpe × (1 − 0.30)` [REPO
   brutex/metrics/lockbox.py:125,477-478] — no absolute floor; the `0.05` at cascade.py:152 is
   the composite-score WEIGHT for the `lockbox_sharpe` term, not a gate), **Monte-Carlo permutation**
   (#9, p<0.01). Caveat for selling: permutation/bootstrap resampling of a negative-skew return
   series UNDER-represents the tail exactly when the sample lacks one — the gates reduce but do
   not eliminate hazard (1); a selling-specific stress leg (synthetic gap/vol-shock scenario
   replay) should be a named future gate rather than trusting resampling alone.
3. **Structure-count explosion.** Strikes × DTE × entry-time × exit-rule × wing-width makes the
   selling grid combinatorially enormous — the multiple-testing burden is HIGHER than the spot
   sweep's; SPA/PBO/RW pool gates and the trial-count-aware DSR must receive the TRUE trial
   count, never the post-filter survivor count.
4. **Regime non-stationarity.** The Nov-2024 SEBI break (§2.3) changed which expiries exist, lot
   sizes, and short-side expiry-day margin. A backtest that pools across the break without a
   regime axis is measuring a market that no longer exists; refusal-over-fabrication for
   unverified margin/cost windows (the DEC-ETC-REGIME-HISTORY-001 pattern) is the right template.
5. **Fill realism.** Retail-heavy weekly option books have wide, unstable spreads at the wings;
   last-traded-price 1-minute bars overstate fillability. The worst-case fill law is the correct
   conservative default; any relaxation is its own DEC. §5–§6 make this concrete.

---

## 5. Seller execution realism

**The one-sentence thesis:** a premium seller's execution problem is *time-asymmetric* — entries
are optional and happen when books are tight; defenses are forced and happen exactly when books
are widest or empty — and our lake (1-minute OHLC of *traded prints*, no quotes) is structurally
blind to that widening, so a design-ready seller sim must (i) price every DEFENSE action in a
strictly-worse fill class than entries, (ii) carry a deterministic deferred-fill law for
defense-on-absent-bar (never "reject" a close of an open short), and (iii) emit counters that
make the unmodelled residue visible instead of silently optimistic.

### 5.1 The defense-time liquidity problem — mechanism

A short-premium defense trigger (tested-side |Δ| breach, premium-multiple SL, spot touching the
short strike) fires by construction during a fast move and/or vol spike. Five structural reasons
the quoted market is worst at that instant:

| # | Mechanism | Basis |
|---|---|---|
| 1 | **Inventory-risk repricing.** Option market makers widen quotes as end-user order pressure raises inventory-risk exposure — the core finding of "Derivative Spreads: Evidence from SPX Options" (Cao, Jacobs, Ke): *"when order pressure from end-users increases, options market makers increase bid-ask spreads to cover higher inventory risk exposure."* | [EXT-SNIPPET https://www.aeaweb.org/conference/2024/program/paper/DFs2GZND]; same family: Eraker, "Market Maker Inventory, Bid-Ask Spreads" [EXT-SNIPPET http://eraker.marginalq.com/OIRM.pdf] |
| 2 | **Adverse-selection repricing.** In a fast market the probability that incoming flow is informed/toxic rises; classic Glosten–Milgrom logic prices that into the spread. | [EXT-SNIPPET https://www.schwab.com/learn/story/large-bidask-options-spreads-volatile-markets] — mechanism class standard in microstructure; specific Indian-options estimates UNVERIFIED (§7 U-E1) |
| 3 | **Hedge-cost passthrough.** An option MM hedges delta in the underlying/futures; when the underlying's own spread/impact widens and realized vol jumps, running the residual gamma/vega book costs more and is quoted back as width. Indian equity evidence that volatility is positively related to spreads: "A comprehensive study on bid-ask spread and its determinants in India" (Cogent Economics & Finance, 2021 — NSE data; **equities, not options** — stated honestly). | [EXT-SNIPPET https://www.tandfonline.com/doi/full/10.1080/23322039.2021.1898735] |
| 4 | **No quoting obligation in Indian index derivatives.** NSE's liquid index-option books have **no formally designated market makers with continuous-quote obligations**; liquidity is voluntary (HFT/prop acting as de-facto MMs). SEBI-permitted Liquidity Enhancement Schemes exist for *illiquid* derivatives, not benchmark index options. In stress, quotes may be pulled entirely — nothing compels a two-sided market. | [EXT-SNIPPET https://www.nseindia.com/static/market-data/liquidity-enhancement-scheme; https://www.business-standard.com/article/markets/sebi-allows-market-makers-for-equity-derivatives-111060300047_1.html; https://www.5paisa.com/stock-market-guide/stock-share-market/market-makers-in-indian-stock-market] |
| 5 | **Crowded-trigger correlation (seller-specific).** The short-premium crowd runs near-identical defense rules (multiple-of-credit SLs, Δ-thresholds — the folk grid the sibling defense-grammar section deliberately encodes). When the move comes, the crowd demands the SAME side (buy-to-close the tested short) in the SAME minutes — one-sided flow against a withdrawing book. A structural inference from the grammar itself, not a measured Indian quantity — [UNVERIFIED as a quantified Indian effect, §7 U-E2]; the Aug/Sep-2021 freak-trade prints (§5.2) are its observable footprint (SL-M order cascades into thin books). |

### 5.2 Indian evidence — forced flow meeting a thin book

**Freak trades, Aug–Sep 2021 (the natural experiment).** On 2021-08-16 NSE removed the Trade
Execution Range (TER) — the exchange-level band that had prevented execution beyond a price
range — and within days forced flow into thin option books printed at absurd levels:

- **2021-08-20, NIFTY 16450 CE (Aug expiry): spiked from ~₹100 to ₹803.05 in seconds.** A
  stop-loss-MARKET buy order in that book took "slippage of almost 650 points … a loss of
  ₹35,000 per lot." [EXT-SNIPPET https://www.paytmmoney.com/blog/freak-trade-nifty/; corroborated
  by https://marketscanner.in/2021/08/the-freak-trade-16450-ce/]
- **BANKNIFTY 36000 PE (9-Sep weekly): rose 2,029% from a low of ₹35.25 to ₹750.**
  [EXT-SNIPPET https://moneylife.in/article/explainer-freak-trades-in-the-nse-derivatives-segment-and-how-to-avoid-them/64930.html;
  https://www.5paisa.com/news/the-freak-show-on-the-nse-and-why-it-is-happening — the 5paisa
  snippet also reports a 20-Aug BANKNIFTY weekly PE surging to ₹2,080]
- Cause attribution across all sources: **market/SL-M orders walking an order book with no depth
  behind the touch** after the TER floor was removed; NSE responded from 2021-09-27 with revised
  protections. [EXT-SNIPPET https://www.moneylife.in/article/nse-finally-responds-to-freak-trades-issue-from-27th-september-announcement-comes-from-zerodha-founder/65175.html;
  https://zerodha.com/z-connect/trending/freak-trades-in-fo-removal-of-execution-range-the-importance-of-limit-orders-and-more]

Read for what it is: these prints are **defense-time fills that actually happened**. A short-call
holder whose SL-M triggered on 2021-08-20 genuinely bought at ₹803 against a ~₹100 fair value.
This simultaneously (i) proves the defense-time liquidity problem is real in Indian *index*
options — not just far-OTM stock options — and (ii) *validates the worst-case fill philosophy*: a
backtest that fills a defense buy at the bar HIGH is not paranoid; on these bars the HIGH was the
achieved market-order price.

**Brokers refuse market orders where books are thin — a liquidity map by revealed preference.**
Zerodha **blocks market and SL-M orders outright** for: all stock options ("because of
illiquidity … the bid and ask spread in the scrip is very high and can have an immediate adverse
effect on your P&L"), deep-ITM index options, and long-dated index options — leaving only
near-money, near-expiry index options market-order-eligible. Paytm Money likewise blocks market
orders on stock F&O.
[EXT-SNIPPET https://support.zerodha.com/category/trading-and-markets/margins/margin-leverage-and-product-and-order-types/articles/what-does-rms-blocked-for-optstk-mkt-mean;
…/articles/market-orders-blocked-for-deep-itm; …/articles/market-orders-blocked-for-long-dated-options;
https://www.paytmmoney.com/blog/freak-trade-nifty/]. Design consequence: **the wings a condor
buys and the far strikes a defense rolls to sit in exactly the book regions brokers consider too
thin for market orders.**

**Stress-day macro anchor.** 2024-06-04 (election result day): NIFTY −5.93% to 21,884.5 — worst
fall since March 2020 — with India VIX +23.65% closing at 25.89.
[EXT-SNIPPET https://www.angelone.in/news/market-updates/election-result-day-and-market-volatility;
https://www.business-standard.com/markets/news/stock-market-live-updates-june-4-sensex-nifty-lok-sabha-election-results-2024-psu-stocks-nse-124060400081_1.html].
Every short-strangle defense trigger in the folk grid would have fired that morning; no
India-published spread series for that day was sourceable in this environment (§7 U-E3).

**What could NOT be evidenced (stated plainly):** no published, page-verifiable **time series of
NIFTY/BANKNIFTY option bid-ask spreads conditioned on stress** was reachable from this
environment (paywalls/403s; NSE publishes impact-cost statistics for equities, not an options
quoted-spread history). The Indian defense-time-widening claim therefore rests on: mechanism
(§5.1, cross-market empirics), revealed broker policy, and the freak-trade prints — **not** on an
Indian quoted-spread dataset. That dataset does not exist in our lake either and cannot be
backfilled (§5.6).

### 5.3 Entry-time vs defense-time bid-ask asymmetry — what is quantifiable today

| Regime | Instrument region | Spread estimate | Tier / source |
|---|---|---|---|
| Normal day, entry-time | NIFTY/BANKNIFTY **ATM weekly** | "typically under 0.15%" of premium; a "2024 NSE study" reportedly showed index options executing "within 0.2% of mid-market" vs "1.2% for less liquid single-stock options" | **[EXT-SNIPPET-ONLY, UNCONFIRMED — §7 U-Q1]** https://www.strike.money/options/options-spread-strategy (page 403'd; provenance of the claimed NSE study unverifiable) |
| Normal day, entry-time | Near-money weekly index options generally | Liquid enough that brokers permit market orders (the only F&O region where Zerodha does) | [EXT-SNIPPET, Zerodha support pages above] — an ordinal fact, not a number |
| Normal day | **Far-OTM wings**, deep-ITM, long-dated, stock options | Thin enough that major brokers block market orders entirely | [EXT-SNIPPET, Zerodha/Paytm pages above] |
| Stress day | BANKNIFTY ATM | "when VIX hit 28.91 … BankNifty ATM spreads ballooned to ₹80–150" | **[EXT-SNIPPET-ONLY, UNCONFIRMED — §7 U-Q1]** (same strike.money page; the quoted date is even suspect) |
| Stress seconds | Index weekly options, forced (SL-M) flow | Achieved fills ~8×–21× prevailing premium (₹100→₹803.05; ₹35.25→₹750) | [EXT-SNIPPET, §5.2 freak trades — PRINTS, i.e. realized worst-case, not quoted spreads] |
| Any regime, from OUR lake | any contract | **Quoted spread: unmeasurable — the lake stores no quotes** (§5.4). The only in-lake proxy is per-bar `high−low` range of *traded* prints, which under-represents the quoted spread precisely on stress/no-print bars | [REPO brutex/data/fno_candle_lake.py:132-141] |

**Honest summary:** the entry-vs-defense asymmetry is firmly evidenced *ordinally*
(tight-enough-for-market-orders vs blocked-for-market-orders vs freak-print regimes) and
*mechanistically* (§5.1), but a defensible Indian *ratio* ("defense spreads are k× entry
spreads") **cannot be stated today** from any page-verified source or from our data. That ratio
is exactly what the §5.6 forward-capture corpus would measure. No lake exists in this research
environment to even measure print-side proxies:
`find / -maxdepth 4 -name "*.parquet" -path "*bars*"` → no results; `env | grep BRUTEX` shows no
`BRUTEX_LAKE_ROOT` [MEASURED — commands + empty output, this session]. Zero-volume-minute
frequency by moneyness/DTE is therefore UNMEASURED here and is a named measurement task for a
lake-mounted session (§7 U-M1).

### 5.4 Our data's honest limits — what the lake is and what the current fill law bounds

**What the lake actually is:**

- The F&O candle lake stores, per contract, **1-minute bars of traded prints**: `timestamp, open,
  high, low, close, volume, open_interest` (+ ten nullable stored-Greeks columns
  `iv…greeks_provenance_id`, + read-time derived columns) at
  `bars/<exchange>/FNO/<groww_symbol>/<interval>/<yyyy>/<mm>.parquet`
  [REPO brutex/data/fno_candle_lake.py:61-63,132-176]. **There are no bid/ask columns anywhere in
  the schema.** OHLC prices are 2-dp-snapped Decimal rupees [REPO fno_candle_lake.py:128-137].
- A minute in which the contract did not trade is **absent — honestly `None`, never the
  nearest-prior bar**: the executor's lake source matches entry/exit bars "on the EXACT minute
  instant … a minute the option did not trade is an honest `None`, never the nearest-prior bar"
  [REPO brutex/options/lake_source.py:31-33,150-156].
- A minute bar that exists with `volume == 0` is a stale/carried price and is **rejected
  unconditionally** today (`STALE_BAR`): "a minute bar with volume == 0 carries a stale/carried
  OHLC — nothing traded, so pricing a fill at its open (even 1-tick-slipped) fabricates an
  unachievable execution" [REPO brutex/options/executor.py:543-557; futures twin
  brutex/futures/executor.py:707-718].
- Vendor-side corroboration that quote data is a *different data class entirely*: GDFL's 1-second
  L1 snapshots are the only retail-accessible bid/ask-bearing record
  (`BuyPrice/BuyQty/SellPrice/SellQty` per row) [REPO docs/vendors/gdfl/VERDICT.md:57-59,72-75],
  and 1-second is the ceiling for Indian retail data [REPO VERDICT.md:28-40].

**The current fill law, verbatim (what a seller sim would inherit):** worst-case is the ONLY fill
law (DEC-FILL-WORST-CASE-ONLY-001): the **adverse-max law** — BUY fills at
`max(bar_high, trigger_level)`, SELL at `min(bar_low, trigger_level)`; open-anchored events at
the pure adverse extreme; same-bar SL+TP ties fill the STOP first; signal on bar N fills on bar
N+1 [REPO brutex/execution/fill_mode.py:8-24; brutex/engine/trade_sim.py:26-67]. The options
executor prices at the option's own bar extremes as "the worst-case adverse anchors, REQUIRED"
[REPO brutex/options/executor.py:580-589], after the reject ladder
(lot_size → cost_regime → no_data → stale_bar → tick_floor) [REPO executor.py:507-564]. A
`session_gap` law already exists for positions stranded by a data hole: close retroactively at
the prior session's last stored bar's adverse extreme, counted via the `exits_session_gap`
counter [REPO trade_store.py:210] under the session-gap law [REPO trade_sim.py:60-67].

**What worst-case-per-leg fills DO bound:**

1. **Achievability-within-prints.** Filling a defense buy at the decided bar's HIGH (with
   `volume > 0` required) charges a price at which *some trade actually printed that minute*. It
   bounds every fill inside the bar's printed range — including intra-minute adverse sequencing
   (whole-bar extreme ⊇ any post-trigger sub-minute path), and including the freak-print class:
   on 2021-08-20 the ₹803.05 print IS the bar high, so worst-case@HIGH *reproduces the real SL-M
   catastrophe* instead of smoothing it away (§5.2). For sellers this is the right default
   direction: never more optimistic than a print that occurred.
2. **Gap-through honesty.** `max(level, high)` on a short-option SL means a gap through the SL
   level fills at the (worse) printed extreme, never at the fictitious level
   [REPO fill_mode.py:10-24].
3. **No stale fabrication.** The `volume == 0` and absent-bar guards prevent pricing against a
   carried OHLC nobody traded [REPO executor.py:543-557; lake_source.py:31-33].
4. **Conservative TF interim.** Until a 1-minute execution grid, decision-TF bars make the
   adverse extreme *strictly more* pessimistic, never less [REPO trade_sim.py:69-73].

**What they CANNOT bound (each a permanent blind spot of print-only data):**

| # | Blind spot | Why it bites the SELLER hardest |
|---|---|---|
| C-1 | **Spread wider than bar range.** A bar can print `H−L = ₹3` while the book quotes ₹15 wide (prints cluster at one touch; the far touch never trades). A marketable defense buy can legally fill ABOVE the bar's HIGH — outside the worst-case envelope. Undetectable without quotes. | Stress bars — where defense triggers fire — are exactly where quoted width detaches from print range (§5.1–5.2). Worst-case@HIGH is then *not* worst-case. |
| C-2 | **Size.** Bar `volume` is total contracts traded in the minute, not depth at any price. One lot printing at HIGH does not mean N lots were fillable at HIGH. Sim fills are honest only as *per-lot marginal* prices. | Defense flow is size-inelastic (you must close the whole tested side) and the crowd demands the same side simultaneously (§5.1 #5). Multi-lot scaling is unmodellable without depth — permanent limit (GDFL is L1-only: [REPO docs/vendors/gdfl/INGEST_DESIGN.md:1002-1009]). |
| C-3 | **No-print minutes.** Absent bars / `volume==0` bars are precisely the far-OTM wings and stressed strikes a defense must trade (`D-WING` buys, `D-ROLL-U` re-opens). Today's law REJECTS the trade — sound for an optional entry, **unsound for a defense**: you cannot decline to close a short you already hold; the risk continues whether or not the sim can price the close. The executor's own survivorship honesty note says exit-bar availability "correlates with market conditions" [REPO executor.py:602-618]. | For a long-translation trade a reject = foregone trade (conservative). For a seller's defense a reject = silently *holding* an undefended short through the worst conditions — anti-conservative, the exact inversion of the law's intent. |
| C-4 | **Intra-gap extremes invisible.** OHLC bounds only observed prints; extremes between prints are invisible (the same fidelity law the GDFL design states for derived bars: [REPO INGEST_DESIGN.md:1010-1017]). | An SL trigger computed from the premium path may fire late (the true book crossed the level minutes earlier with no print). |
| C-5 | **Settlement price ≠ any bar close.** Expiry cash settlement uses the exchange's official settlement price (last-half-hour underlying average), which our lake can only proxy from spot 1-minute closes. | Hold-to-expiry short structures settle on a number the lake approximates, not stores. [UNVERIFIED exact current NSE settlement formula text — §7 U-E4] |

### 5.5 The strictly-worse defense fill class + deferred-fill law (design proposals for the FUTURE seller DEC — nothing changes today)

**Conclusion:** worst-case-per-leg over 1-minute traded prints is the correct *floor* and must be
kept; but (C-1)–(C-3) mean it is not a *ceiling* on defense costs. Therefore:

**LAW-1 — DEFENSE-FILL-CLASS (strictly-worse class for defense actions).** Every action whose
timing is *forced by the market* (SL, roll legs, wing-add, invert, stop-touch close, recenter's
closing half) fills at `adverse_extreme ± defense_penalty`, where `defense_penalty` is a
**sweepable, default-omitted axis** (e.g. `+p ticks` or `+s% of leg premium`, buy-side `+`,
sell-side `−`), riding `canonical_trade_params` exactly like the `fib_anchor`/`PriceSource` axes
so the default (penalty = 0) leaves run identity byte-identical (the
DEC-NIFTY-PRICE-SOURCE-AXIS-001 precedent, default-omitted keys). Entries and profit-takes
(timing *chosen*) keep the plain adverse extreme. Rationale: §5.1–5.3 evidence — the bar range
under-represents the executable price exactly when defense triggers fire; a penalty axis makes
the sensitivity of a strategy's expectancy to defense slippage a *measured sweep output* instead
of an assumption. Calibrating realistic `s` values is impossible from our data and waits on
§5.6's corpus — until then the axis is honest exactly because it is swept, not asserted.

**LAW-2 — DEFENSE-DEFER (deterministic deferred fill on absent/stale bar; never reject a
defense).** When a defense action on leg L decides at minute `t` and L's bar at `t` is absent or
`volume == 0`:
1. Walk forward to the FIRST bar `t' > t` of L with `volume > 0` **within the same session**;
   fill there under LAW-1 (buy@`max(high,level)+penalty`, sell@`min(low,level)−penalty`).
   Deterministic, look-ahead-clean (the walk consumes only bars ≥ the decision instant, mirroring
   the walker's forward-only law [REPO trade_sim.py:75-78]).
2. Count every skipped minute: `defense_deferred_minutes += (t'−t)`; stamp the fill
   `deferred=true, deferral_minutes=n`.
3. If NO such bar exists before the session's forced-exit boundary (session close −10 min,
   per-bar-resolved [REPO trade_sim.py:46-54]):
   - expiry day, index CE/PE → settle at the settlement-proxy law (register row 7) with reason
     `defense_expiry_settled`, counted;
   - otherwise → carry to the NEXT session's first `volume>0` bar under a `defense_session_gap`
     reason (the seller twin of the engine's `session_gap` law + counter
     [REPO trade_sim.py:60-67; counter trade_store.py:210]), counted `defense_carried_sessions`; interim MTM uses the
     last-known close with a `stale_mark_minutes` counter.
4. **Never** emit a `NO_DATA`/`STALE_BAR` *rejection* for a defense action — those reasons stay
   entry-only. A rejection would silently convert "couldn't price the close" into "held the naked
   short for free" (C-3).

**LAW-3 — MULTI-LEG NO-NETTING.** NSE has no guaranteed complex-order execution for these
structures at retail; every roll/recenter is legged. Each leg of a multi-leg action prices
independently at its own adverse extreme (+penalty per LAW-1); if legs defer to different `t'`
under LAW-2, the divergence is counted (`legging_bars`) and the interim one-sided exposure is
marked to market. No mid-price netting credit, ever.

**LAW-4 — DEGRADE LADDER for unfillable defenses.** If a `D-WING` buy or `D-ROLL-U` re-open leg
finds no `volume>0` bar within `W` minutes (sweepable), the action degrades deterministically to
the next-most-liquid defense (`wing-add → close-tested-side → close-all`), counted
`defense_degraded`. 06 E-15 treats ban periods as a refuse/ABSTAIN edge; LAW-4 here INTRODUCES
the ban-period-degradation concept (a `ban_active` ⇒ `D-ROLL-U` degrades-to-close-only rule not
yet in 05's defense grammar) — flagged as new, to be registered in 05's grammar by the executor
DEC.

### 5.6 What data would fix it — the GDFL forward-capture path

- GDFL's 1-second L1 snapshot records carry exactly the missing fields: `LastTradePrice,
  BuyPrice, BuyQty, SellPrice, SellQty, TradedQty, OpenInterest` per ~1-second row
  [REPO docs/vendors/gdfl/VERDICT.md:57-59,72-75].
- **Backfill is impossible — forward capture only.** "GDFL tick-class API history is days-scale …
  a 2020→now 1-second archive does NOT exist via the API; **forward capture from subscription day
  is the only 1s-class path**, and the capture leg is OUT of this design"
  [REPO docs/vendors/gdfl/INGEST_DESIGN.md:996-1001]; same verdict in headline form: "a 2022→now
  1-second-class BACKFILL is NOT available from GDFL; forward capture from subscription day is
  the only 1-second-class path" [REPO VERDICT.md:14-17,132-142]. The capture daemon itself
  "requires a FRESH ADR" [REPO INGEST_DESIGN.md:1111-1113] and stays out of the engine per
  DEC-LIVE-REMOVE-001 [REPO VERDICT.md:137-140].
- **Consequence, stated without varnish:** historical defense realism on the 2020→2024 lake is
  **bounded forever** at print-only fidelity — no future spend recovers the quoted spreads of
  2021-08-20 or 2024-06-04. What a forward-capture DEC (proposal handle:
  `DEC-GDFL-FORWARD-CAPTURE-001`, subordinate to the required fresh ADR) buys is a **calibration
  corpus going forward**: per-minute joins of (quoted spread `SellPrice−BuyPrice`, top-of-book
  sizes, print range `H−L`, India VIX, moneyness, DTE) that turn LAW-1's `defense_penalty` axis
  from a swept assumption into a fitted, regime-conditioned function — and quantify §5.3's
  missing "defense spreads are k× entry spreads" ratio for the FIRST time on Indian index
  options. It also measures C-1 directly (fraction of stress minutes where quoted width > print
  range) and C-3 (no-print minute frequency vs quoted-book presence: L1 quotes exist even in
  printless minutes).
- Limits even then: L1 only — one bid/ask level, no depth
  [REPO INGEST_DESIGN.md:1002-1009] — so C-2 (size) remains permanently unmodellable at retail;
  the corpus applies only to contracts captured from subscription day onward.

---

## 6. The seller EXECUTION-REALISM REGISTER (per action type)

Honesty classes: **F-A** = achieved-print-bounded (adverse extreme of a `volume>0` bar — a real
print occurred at/beyond the charged price); **F-B** = defense-penalized (class F-A + LAW-1 penalty;
bar range may still understate the book — C-1 residue disclosed); **F-C** = deferred (LAW-2
walk-forward engaged); **F-D** = settlement-proxy (official settlement price approximated from the
spot 1-minute lake); **F-E** = unboundable residue (size/impact beyond per-lot marginal — C-2;
disclosed, never modeled). Every class-F-B/F-C/F-D fill also reports its class on the trade row (the
`exits_session_gap`/`mae_granularity` counter precedent [REPO trade_store.py:210 (counter);
trade_sim.py:60-67 (law); CLAUDE.md §1 #1743 blurb]).

> **Reconciliation.** These F-A…F-E are per-ACTION EXECUTION-REALISM classes; the per-TRIAL
> FILL-HONESTY taxonomy of record is chapter [05](05-engine-design.md) §4.5's A/B/C (a
> whole-structure verdict over all legs at entry AND exit). An F-C deferral or F-E residue on any
> leg drives the trial's per-trial class toward 05-Class-B (bounded-price only) or 05-Class-C
> (refused); a trial is 05-Class-A only when every leg's fill is F-A/F-B-bounded at the simulated
> size. See 05 §4.5.

| # | Action (grammar ID) | Timing nature | Fill law | Class | Counters (per structure/run) |
|---|---|---|---|---|---|
| 1 | **Entry** — sell-to-open all legs (E-\* laws) | chosen | signal bar N → fill bar N+1 (engine timing lock [REPO trade_sim.py:33-36]); each leg sells @ its bar's LOW; ALL legs require a `volume>0` bar at the decided minute; any leg failing ⇒ whole entry **skipped** (foregone trade — the ONE place reject stays conservative; [REPO executor.py:543-557] law retained) | F-A (+F-E residue) | `entries`, `entry_skipped_no_data`, `entry_skipped_stale_bar`, `entry_leg_minute_mismatch`, per-leg `fill_bar_range_paisa` (the in-lake spread proxy), credit received |
| 2 | **Profit-take** (`D-TP`) — buy-to-close | chosen | trigger on bar N close → fill N+1; each leg buys @ HIGH; LAW-2 defer on absent/stale bar (profit-takes may defer — they are still closes of an open short) | F-A; F-C if deferred | `tp_fills`, `tp_deferred`, `tp_deferral_minutes` |
| 3 | **Stop-loss** (`D-SL-k`, `D-STOP-TOUCH`) — buy-to-close tested/all | **forced** | buy @ `max(level, high) + penalty` (adverse-max WITH level [REPO fill_mode.py:10-13] + LAW-1); same-bar tie with any TP: SL first (engine tie law [REPO trade_sim.py:43-45]); LAW-2 defer; LAW-3 legging when multi-leg | **F-B**; F-C if deferred | `sl_fills`, `sl_gap_through` (fill > level), `sl_excess_over_level_paisa`, `sl_deferred`, `sl_deferral_minutes`, `penalty_paisa_total` |
| 4 | **Roll** (`D-ROLL-U`, `D-INVERT`, `D-ROLL-T` [DTE-triggered roll-out — a distinct grammar ID to be registered in 05; 05:346's `D-DTE` is a scheduled plain close, NOT a roll]) — buy-to-close old leg + sell-to-open new leg | forced | old leg buy @ HIGH+penalty; new leg sell @ LOW−penalty; SAME decided bar when both printable, else LAW-2 per leg + LAW-3 legging accounting; new-leg unfillable within W minutes ⇒ LAW-4 degrade | **F-B**; F-C/legging | `rolls`, `roll_credit_recollected_paisa`, `roll_legging_bars`, `roll_deferred_legs`, `roll_degraded` (→ which rung), `invert_width_steps` |
| 5 | **Wing-add** (`D-WING`) — buy-to-open far-OTM wing | forced | buy @ HIGH+penalty; the WORST liquidity region by revealed broker policy (§5.2) so the LAW-4 window `W` is smallest here; unfillable ⇒ degrade to close-tested-side | **F-B**; F-C; heavy F-E residue | `wing_adds`, `wing_deferred`, `wing_unfillable_degraded`, `wing_fill_bar_range_paisa` |
| 6 | **Recenter** (`D-RECENTER`) | forced (close half) + chosen (reopen half) | closing half = row 3 law; reopening half = row 1 law but LAW-2-deferrable (a recenter's reopen is optional — degrade to stay-closed, counted) | F-B + F-A | `recenters`, `recenter_reopen_skipped` |
| 7 | **Expiry settlement** (hold-to-settle) + forced 15:20 alternative | scheduled | if closed pre-expiry: row 2/3 laws with the engine's forced-exit boundary (session close −10 min [REPO trade_sim.py:46-54]); if held to settle: index CE/PE cash-settle at intrinsic vs the **settlement-price proxy** computed from the spot 1-minute lake's last-30-min closes; proxy divergence from the official settlement price is undetectable in-lake — stamped `settlement_proxy=true` | **F-D** | `expiry_settlements`, `settlement_proxy_count`, `expiry_itm_legs` (STT/assignment handoff to the tax sections), `forced_1520_exits` |
| 8 | **Any defense on a no-print/stale bar** | forced | LAW-2 (walk-forward, session-gap carry, never reject) | **F-C** | `defense_deferred_total`, `max_single_deferral_minutes`, `defense_carried_sessions`, `stale_mark_minutes`, `defense_session_gap` |
| 9 | **Freak-class bar disclosure** (any fill on a bar whose extreme is a stress print) | n/a (annotation) | fills stand (worst-case is honest — §5.4) but the bar is flagged: `high/prev_close` or `low/prev_close` beyond a premium-appropriate threshold (NOT the spot engine's 4× bad-tick law — option premiums legitimately move >4×; the threshold is a design parameter [UNVERIFIED calibration — §7 U-M2]) | annotation | `freak_class_fill_count`, `freak_class_worst_multiple` |

**Cross-cutting emissions (run-level):** `penalty_paisa_total`, `f_class_b_fill_share`,
`f_class_c_fill_share`, the `fill_bar_range_paisa` distribution (the ONLY in-lake spread proxy —
reported, never presented as a quoted spread), and the standing survivorship disclosure inherited
verbatim from the executor's honesty note (accepted-set selection bias,
[REPO executor.py:602-618]) rewritten for sellers: *a seller backtest's UNDEFENDED residue
(class-F-C carries, degrades) must be reported beside P&L, because that residue concentrates in
exactly the adverse regimes.*

---

## 7. Consolidated UNVERIFIED register

### 7.1 Edge/VRP side

| # | Item | What would verify it |
|---|---|---|
| U1 | Bondarenko "Why Are Put Options So Expensive?" — exact findings/figures | Fetch SSRN abstract 375784 / *Quarterly Journal of Finance* 4(3) 2014 |
| U2 | Bollerslev–Tauchen–Zhou 2009 specifics (R², horizons) | Fetch RFS 22(11) 4463–4492 |
| U3 | Broadie–Chernov–Johannes 2009 specifics | Fetch RFS 22(11) 4493–4529 |
| U4 | Israelov–Nielsen / AQR VRP papers' specifics | Fetch AQR library / SSRN |
| U5 | Goetzmann et al. Sharpe-manipulation citation | Fetch "Sharpening Sharpe Ratios" (NBER w9116) |
| U6 | FY22 average individual loss ≈ ₹1.1 lakh (Jan-2023 SEBI study) | Read the SEBI Jan-2023 study PDF itself (page-level figure) |
| U7 | Short-strangle win-rate/tail profiles (tastylive/Spintwig class) | No peer-reviewed source exists; only reproducible via OUR engine — deliberately left unverified |
| U8 | 0DTE-selling literature (US or India) | Targeted literature sweep; low design priority — our engine measures it directly |
| U9 | Indian academic VRP corpus beyond the one 2025 *Economic Sciences* paper | SSRN/Scopus sweep "India VIX variance risk premium" |
| U10 | The *Economic Sciences* 2025 paper's methodology quality (74%-positive figure) | Full-text review; and independently re-derive from OUR lake (§4) — the re-derivation is the one that matters |
| U11 | ALL lake-derived VRP numbers | **PENDING-ON-BOX** — execute §4.2 on the operator's box (this container has no lake — §4.0) |

### 7.2 Execution-realism side

| ID | Claim left unverified | What would verify it |
|---|---|---|
| U-Q1 | All strike.money-sourced numbers: NIFTY/BANKNIFTY ATM spreads "<0.15%"; the "2024 NSE study" (0.2% vs 1.2% of mid); "VIX 28.91 → BankNifty ATM spreads ₹80–150" (quoted date also suspect) | Fetching the page outside the 403-ing proxy AND locating the primary NSE study it cites; better: the §5.6 forward-capture corpus |
| U-E1 | Quantified adverse-selection / inventory contribution to INDIAN option spread widening (the §5.1 mechanisms are cross-market/theory-anchored) | An Indian-options microstructure study with quote data (none page-verifiable found), or the §5.6 corpus |
| U-E2 | The crowded-defense-trigger correlation as a *measured* Indian effect (same-side flow bursts at SL cascade instants) | Order-flow/prints analysis on 1-second L1 capture around VIX-spike minutes |
| U-E3 | 2024-06-04 quoted-spread behavior in NIFTY/BANKNIFTY options (only index-level −5.93% / VIX +23.65% close 25.89 sourced) | A quote archive for that day (commercial tick vendors may sell it; GDFL cannot backfill — VERDICT.md:14-17) |
| U-E4 | Exact current NSE option settlement-price formula text (last-30-min underlying average assumed for the class-F-D proxy) | The current NSE/SEBI settlement circular, read directly |
| U-M1 | In-lake measurables: zero-volume-minute and absent-minute frequency by moneyness/DTE/regime; `high−low` range distribution on stress vs normal days | Re-running this analysis in a lake-mounted environment (none here: `find / -name '*.parquet' -path '*bars*'` → empty [MEASURED]) |
| U-M2 | The freak-class bar threshold for option premiums (register row 9) | Calibration on the U-M1 measurement + the Aug-2021 known freak bars as labeled positives |
| U-S1 | Freak-trade figures page-level exactness (₹803.05; ₹35.25→₹750 +2,029%; ₹2,080 print; ₹35,000/lot SL-M loss) — all snippet-tier, pages 403'd | Fetching Paytm Money / Moneylife / Zerodha Z-Connect / 5paisa articles outside the proxy |
| U-S2 | "NSE index derivatives have NO designated market makers with continuous-quote obligations" — assembled from LES-scope pages + secondary explainers; no single authoritative NSE statement fetched | NSE/SEBI primary circular set on market-making scope in equity derivatives |

### 7.3 Honest limits, restated

1. No Indian quoted-spread dataset exists in this repo, in our lake, or in any page-verified
   source reached from this environment.
2. The 2020–2024 backtest window can never gain quote fidelity (GDFL forward-only —
   [REPO VERDICT.md:14-17; INGEST_DESIGN.md:996-1001]).
3. Size/impact beyond per-lot marginal is permanently unmodellable at retail (L1 ceiling —
   [REPO INGEST_DESIGN.md:1002-1009]).
4. Every external figure in §5 tagged [EXT-SNIPPET] is snippet-tier because the proxy 403'd the
   primary pages — nothing there should be quoted onward as page-verified without re-fetching.
5. No VRP number of our own exists anywhere; §4's spec is the sole path to one, and every
   resulting figure is PENDING-ON-BOX.

### 7.4 Web sources

**Edge/VRP (fetched in-session unless tagged UNVERIFIED above):**
- https://academic.oup.com/rfs/article-abstract/22/3/1311/1581057 (Carr–Wu 2009)
- https://engineering.nyu.edu/sites/default/files/2019-01/CarrReviewofFinStudiesMarch2009-a.pdf
- https://academic.oup.com/rfs/article-abstract/16/2/527/1579962 (Bakshi–Kapadia 2003)
- https://people.umass.edu/~nkapadia/docs/Bakshi_and_Kapadia_2003_RFS.pdf
- https://cdn.cboe.com/resources/education/research_publications/PutWriteCBOE19_v14_by_Prof_Oleg_Bondarenko_as_of_June_14.pdf (Bondarenko/CBOE 2019)
- https://en.wikipedia.org/wiki/CBOE_S%26P_500_BuyWrite_Index (Whaley/Ibbotson/Callan BXM history)
- https://www.borntosell.com/covered-call-blog/evaluation-of-buy-write-strategy (Callan figures)
- https://cdn.cboe.com/resources/spx/wilshire-options-based-benchmark-indexes-2019.pdf
- https://portfolioslab.com/symbol/%5EPUT (PUT drawdown pair — secondary)
- https://www.nb.com/documents/public/en-us/uncovering_the_equity_index_putwrite_strategy_ria.pdf
- https://www.nseindia.com/static/products-services/indices-indiavix-index (India VIX methodology)
- https://nsearchives.nseindia.com/web/sites/default/files/inline-files/white_paper_IndiaVIX.pdf
- https://economic-sciences.com/index.php/journal/article/view/356 (Indian NIFTY VRP study)
- https://www.sebi.gov.in/reports-and-statistics/research/jan-2023/study-analysis-of-profit-and-loss-of-individual-traders-dealing-in-equity-fando-segment_67525.html (SEBI Jan-2023, 89% FY22)
- https://www.sebi.gov.in/media-and-notifications/press-releases/sep-2024/updated-sebi-study-reveals-93-of-individual-traders-incurred-losses-in-equity-fando-between-fy22-and-fy24-aggregate-losses-exceed-1-8-lakh-crores-over-three-years_86906.html (SEBI Sep-2024, 93% / ₹1.8L cr)
- https://www.business-standard.com/markets/news/net-losses-of-traders-in-fo-widens-in-fy25-sebi-study-125070701221_1.html (SEBI Jul-2025 study, 91% FY25 / ₹1.056L cr)
- https://zerodha.com/z-connect/business-updates/sebis-new-rules-for-index-derivatives-heres-whats-changing (Oct-2024 SEBI F&O framework)
- https://www.businesswire.com/news/home/20240118034299/en/NSE-is-the-Worlds-Largest-Derivative-Exchange-for-Fifth-Consecutive-Year
- https://www.fia.org/fia/articles/global-futures-and-options-volume-hits-record-137-billion-contracts-2023
- https://www.fia.org/marketvoice/articles/explainer-meteoric-rise-indias-equity-derivatives-volume

**Execution realism (ALL snippet-tier — pages 403'd through this environment's proxy):**
- https://www.paytmmoney.com/blog/freak-trade-nifty/ (Paytm Money freak-trade blog)
- https://moneylife.in/article/explainer-freak-trades-in-the-nse-derivatives-segment-and-how-to-avoid-them/64930.html (Moneylife freak-trades explainer)
- https://www.moneylife.in/article/nse-finally-responds-to-freak-trades-issue-from-27th-september-announcement-comes-from-zerodha-founder/65175.html (Moneylife NSE 27-Sep response)
- https://zerodha.com/z-connect/trending/freak-trades-in-fo-removal-of-execution-range-the-importance-of-limit-orders-and-more (Zerodha Z-Connect on TER removal)
- https://www.5paisa.com/news/the-freak-show-on-the-nse-and-why-it-is-happening (5paisa freak show)
- https://marketscanner.in/2021/08/the-freak-trade-16450-ce/ (MarketScanner 16450 CE)
- https://support.zerodha.com/category/trading-and-markets/margins/margin-leverage-and-product-and-order-types/articles/what-does-rms-blocked-for-optstk-mkt-mean (Zerodha support: stock-option market orders blocked)
- https://support.zerodha.com/category/trading-and-markets/margins/margin-leverage-and-product-and-order-types/articles/market-orders-blocked-for-deep-itm (Zerodha support: deep-ITM blocked)
- https://support.zerodha.com/category/trading-and-markets/margins/margin-leverage-and-product-and-order-types/articles/market-orders-blocked-for-long-dated-options (Zerodha support: long-dated blocked)
- https://www.angelone.in/news/market-updates/election-result-day-and-market-volatility (Angel One election-day volatility)
- https://www.business-standard.com/markets/news/stock-market-live-updates-june-4-sensex-nifty-lok-sabha-election-results-2024-psu-stocks-nse-124060400081_1.html (Business Standard 2024-06-04 live)
- https://www.aeaweb.org/conference/2024/program/paper/DFs2GZND (Cao/Jacobs/Ke, Derivative Spreads: Evidence from SPX Options, AEA 2024)
- http://eraker.marginalq.com/OIRM.pdf (Eraker, Market Maker Inventory, Bid-Ask Spreads)
- https://www.schwab.com/learn/story/large-bidask-options-spreads-volatile-markets (Schwab: wide bid/ask spreads in volatile markets)
- https://www.tandfonline.com/doi/full/10.1080/23322039.2021.1898735 (Cogent Econ & Fin 2021: bid-ask spread determinants in India)
- https://www.nseindia.com/static/market-data/liquidity-enhancement-scheme (NSE LES page)
- https://www.business-standard.com/article/markets/sebi-allows-market-makers-for-equity-derivatives-111060300047_1.html (Business Standard 2011: SEBI allows market makers for equity derivatives)
- https://www.5paisa.com/stock-market-guide/stock-share-market/market-makers-in-indian-stock-market (5paisa: market makers in the Indian market)
- https://www.strike.money/options/options-spread-strategy (strike.money options-spread page — UNCONFIRMED tier)
