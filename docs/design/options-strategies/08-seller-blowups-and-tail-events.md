# 08 — Seller Blow-Ups and Tail Events: What the Worst Days Did to Short Premium, and What an Honest Backtest Must Reproduce

**Status:** RESEARCH / DESIGN-READINESS ONLY — zero engine code; the sweep engine remains
SPOT-ONLY (DEC-ENGINE-SPOT-ONLY-001) and options remain REFERENCE_ONLY; any activation is a
future operator-gated DEC/ADR.
**Date:** 2026-07-19
**Provenance:** multi-agent deep-research session, docs-only branch
`claude/options-strategies-design`, deliberately NO PR (fleet-wide PR-creation freeze).

> Evidence tiers used throughout: **[REPO path:line]** (verified by reading the file in the
> research container) · **[WEB source]** (externally verified, URL given) · **[MEASURED]**
> (command + output shown from the research container) · **UNVERIFIED** (tagged, with what
> would verify it) · **PENDING-ON-BOX** (measurable on the production lake with the §2 recipe;
> NOT measurable in the research container — no lake exists there, proof in §2).

---

## 1. Thesis — the premium IS the crash compensation

Option selling harvests the variance risk premium: implied variance persistently exceeds
subsequently realized variance on equity indices, so the seller is paid the spread on average
(Carr & Wu 2009, *RFS* 22(3) — [OUP abstract](https://academic.oup.com/rfs/article-abstract/22/3/1311/1581057),
[NYU PDF](https://engineering.nyu.edu/sites/default/files/2019-01/CarrReviewofFinStudiesMarch2009-a.pdf);
Bakshi & Kapadia 2003, *RFS* 16(2) — [OUP abstract](https://academic.oup.com/rfs/article-abstract/16/2/527/1579962),
[UMass PDF](https://people.umass.edu/~nkapadia/docs/Bakshi_and_Kapadia_2003_RFS.pdf)). But that
premium is not free money — it is *compensation for bearing jump/crash risk*: Bakshi–Kapadia
find the writer's edge concentrates exactly where the danger concentrates (high-vol states),
and Broadie–Chernov–Johannes 2009 show apparent short-put "alpha" can be fully explained as
fair jump-risk compensation (canonical citation, UNVERIFIED this session — verify *RFS*
22(11) 4493–4529). Decades of small wins fund rare catastrophic losses; a short-vol book can
print a multi-year Sharpe > 2 and still carry negative full-cycle expectancy if the sample
missed its tail. The events below are therefore not anecdotes — they are the *payment
schedule* of the strategy class this design track exists to backtest. Every one ends with the
backtest-reproduction requirements and an honest verdict on whether BruteX's data and
machinery can reproduce it today.

---

## 2. Environment check — NO production lake in the research container [MEASURED]

```
$ echo "BRUTEX_LAKE_ROOT=[$BRUTEX_LAKE_ROOT]"   → BRUTEX_LAKE_ROOT=[]
$ ls ~/.brutex                                   → no ~/.brutex
$ ls /home/user/bruteX/tests/golden/fixtures     → only test fixtures (greeks_golden.json, futures_roll_golden.json, …)
$ find / -maxdepth 4 -type d -name 'bars'        → (nothing)
```

**Verdict:** no lake data exists in the research environment. Every "would-be-measured" figure
below is tagged **PENDING-ON-BOX** with a measurement recipe. The generic recipe, once on the
production box (M4-Pro, CLAUDE.md §8):

- **Spot/VIX 1-minute bars:** `Catalog.get_bars` — [REPO brutex/data/catalog.py:477] — over
  `bars/NSE/INDEX/NSE-NIFTY/1minute/<YYYY>/<MM>.parquet` (and `NSE-INDIAVIX`, symbol constant
  `INDIA_VIX_SYMBOL = "NSE-INDIAVIX"` at [REPO brutex/adapters/groww/constants.py:103]).
- **Option 1-minute bars (with solved IV):** the F&O candle lake
  `bars/NSE/FNO/<contract>/1minute/…` — column contract incl. `iv` ("Solved implied
  volatility (annualized)") at [REPO brutex/data/fno_candle_lake.py:152]; per-leg exact-minute
  reads via `LakeOptionBarSource.load_pair` [REPO brutex/options/lake_source.py:141].
- **Straddle-multiple recipe:** `CE.close + PE.close` at the pre-event reference minute vs.
  the event-day max of the same sum, same strike/expiry — one DuckDB query over two contract
  partitions.
- **Vendor-depth boundary confirmation:** the live doctor already carries a depth probe that
  confirms `GROWW_HISTORY_FLOOR` at the boundary [REPO brutex/doctor_live.py:417].

---

## 3. Data-capability baseline (repo-evidenced) — read before any event

| # | Capability / limit | Repo evidence (verified by reading) | Consequence for blow-up reproduction |
|---|---|---|---|
| B1 | **Vendor history floor = 2020-01-01** | `GROWW_HISTORY_FLOOR: Final[date] = date(2020, 1, 1)` — [REPO brutex/dashboard/pull_controller.py:163] | Events 1, 2, 4 (2018, 2019) are **structurally out of reach** of the Groww lake. They can only ever be chaos-fixture events, never golden-day replays. |
| B2 | **1-minute is the ONLY fetched granularity**; all TFs derived | CLAUDE.md §6 (DEC-1MINUTE-ONLY-DERIVE-1DAY-001, #853); resampler never fabricates absent minutes ([REPO brutex/data/tf_resampler.py:31] "NEVER fabricated", [:254] no stale-spot fabrication) | Intraday IV-explosion *pace* and halt holes are representable at 1-minute fidelity from 2020 on. Halted minutes appear as **absent bars** — never forward-filled. |
| B3 | **Option pull window = expiry − 30d → expiry** (futures − 60d) | `DEFAULT_OPTION_LOOKBACK_DAYS: Final[int] = 30`, `DEFAULT_FUTURES_LOOKBACK_DAYS: Final[int] = 60` — [REPO brutex/adapters/groww/contract_bounds.py:104,118] | Weekly options: full life captured. Monthly/quarterly options: only the last 30 days of life. A 2020 COVID study of far-dated options is bounded by this law (explicit operator dates can override — same file, per CLAUDE.md §6). |
| B4 | **Whether Groww still serves 2020-era expired option chains is UNVERIFIED** | ADR-0009 Phase-0 probe posture (CLAUDE.md §6 UNVERIFIED rows); no probe transcript for deep-history `get_expiries` in repo | Even inside the 2020 floor, the March-2020 option chain's availability is an **operator probe question**, not a fact. |
| B5 | **India VIX is lake-admitted, REFERENCE_ONLY, stamped on trades** | `INDIA_VIX_SYMBOL = "NSE-INDIAVIX"` ([REPO brutex/adapters/groww/constants.py:103]); vix_at_entry/exit stamping per DEC-VIX-STAMP-WIRE-001 (CLAUDE.md §2 Rule 3); **historical-candle availability on Groww = UNVERIFIED Phase-0 HARD-GATE** (CLAUDE.md §6) | VIX-conditioned entry gates and VIX-path reproduction are designed-for, but 1-minute VIX depth to 2020 must be probe-confirmed. |
| B6 | **Margin/SPAN history: ZERO verified rows — refusal-only** | [REPO brutex/engine/margin_history.py:8] ("charge-an-unverified-margin state is UNREPRESENTABLE"), [:233,238,253-270] (`status="unverified"`, `roi_pct=None`) | **Margin expansion — the #1 real-world seller killer — is NOT reproducible today.** Any seller design must treat margin as a named refusal until a dated SPAN table lands (its own DEC). |
| B7 | **Options cost regimes verified only from 2024-10-01** | options executor rejects `COST_REGIME_UNVERIFIED` date-keyed ([REPO brutex/options/executor.py:184,520,530,619,651]); futures STT 0.02% eff. 2024-10-01 encoded ([REPO brutex/costs/futures_costs.py:198-200]) | Honestly-costed short-option P&L exists only for **2024-10-01 → today**. Pre-boundary blow-up days (COVID, Jun-2024 election) can be replayed **gross**, with costs as counted refusals — exactly the DEC-ETC-REGIME-HISTORY-001 posture. |
| B8 | **Fills are worst-case-only** (adverse extreme of the decided bar; same-bar tie = SL-first; volume-0 bars reject) | [REPO brutex/execution/fill_mode.py:33,72] (`WORST_CASE = "worst_case"`, unconditional identity token; DEC-FILL-WORST-CASE-ONLY-001 / ADR-0023) | Gap-through-strike days are priced **pessimistically by construction** — the right default for seller honesty. But worst-case-of-a-1-minute-bar still under-prices a *halt reopen* gap if the halt minutes are absent (the fill lands on the first PRESENT bar's adverse extreme — which IS the reopen print; behavior is correct, latency is what must be asserted). |
| B9 | **Session filter is calendar-aware** (Muhurat kept, non-trading dates dropped) | [REPO brutex/data/session.py:16-18,251-258]; `MUHURAT_SESSIONS` incl. `2020-11-14 18:15–19:15` — [REPO brutex/calendars/nse.py:429-430] | Event-day session shapes (Budget Saturdays, Muhurat) are representable; the calendar is authority, not the wall clock. |
| B10 | **Open positions never survive a session change** — `session_gap` exit at prior session's last stored bar, adverse extreme | [REPO brutex/engine/trade_sim.py:65,161,270] (SESSION_GAP reason; finish() pricing) | Overnight-gap risk is NOT currently simulatable as *held-through* exposure in the spot engine — positions are intraday by construction (forced 15:20 exit, CLAUDE.md §7). A seller design wanting overnight short premium needs a NEW position-lifecycle DEC; today's engine can only reproduce **intraday** blow-ups. |
| B11 | **Bad-tick screen = reject close ≥4× or ≤1/4× prev in-session close** — wired on the ENGINE spot feed + spot prebuild, NOT on the options lake reader | `BAD_TICK_MAX_RATIO: Final[float] = 4.0` — [REPO brutex/data/bad_tick.py:76,141-166]; wired in `brutex/feed/replay.py` + `brutex/data/timeframe_materialize.py` (grep `screen_frame_bad_ticks`); `brutex/options/lake_source.py` contains NO screen import (verified) | **Design flag:** crash-day deep-OTM options genuinely move >4× minute-over-minute (a ₹2 put printing ₹10 is a *real* price). The current option read path is unscreened (correct for fidelity). Any future activation that routes option bars through a 4.0× screen would DELETE genuine blow-up prints — this must be a named exclusion in the options-activation DEC. |
| B12 | **Intra-session holes are invisible to every detector** | interior-gap detector operates on whole SESSIONS ([REPO brutex/data/interior_gap_detect.py:20,98-105] — tolerance 5 *sessions*); INDEX completeness demands 375 distinct minutes only for TODAY's settling window ([REPO brutex/data/today_completeness.py:35-36,367]) | A 45-minute circuit halt inside a stored historical day is silently a 330-minute day. Nothing flags it; nothing should "heal" it. A **halt-day golden fixture** must therefore pin the absent-minute count explicitly. |

---

## 4. THE EVENT TABLE

| # | Date(s) | Underlying move | India VIX action | Short-premium consequence | Source(s) |
|---|---|---|---|---|---|
| 1 | 5–6 Feb 2018 (Volmageddon) | US: S&P ~−4%; India: Sensex fell sharply 2 & 5 Feb (~570–839 pts, sources conflict), extended 6 Feb | CBOE VIX **+116% in one day** (18.84 → open 38.25); India VIX 6-Feb-2018: UNVERIFIED | XIV −~95% in a session ($1.9B → $63M), terminated; ~$4.1B short-vol ETP value gone — vega + forced liquidation, not delta | [sixfigureinvesting](https://www.sixfigureinvesting.com/2019/02/what-caused-the-february-5th-2018-volatility-spike-xiv-termination/), [CNBC](https://www.cnbc.com/2018/02/05/xiv-exchange-traded-security-linked-to-volatility-plummets-80-percent.html), [Bloomberg](https://www.bloomberg.com/news/articles/2019-02-06/the-day-the-vix-doubled-tales-of-volmageddon), [Wikipedia](https://en.wikipedia.org/wiki/Stock_market_crashes_in_India), [BusinessToday](https://www.businesstoday.in/business/market/story/sensex-nifty-crash-ltcg-tax-on-equities-102121-2018-02-05) |
| 2 | 21 Sep 2018 (IL&FS/DHFL) | Sensex −1,128 pts intraday, mostly recovered in-session; **DHFL −59.67% intraday** | UNVERIFIED | Single-name short puts marked through a −60% path; whip punished panic-covering index sellers | [BusinessToday](https://www.businesstoday.in/markets/stocks/story/sensex-nifty-crash-nbfcs-drive-market-down-110282-2018-09-21), [BusinessToday/DHFL](https://www.businesstoday.in/markets/company-stock/dewan-housing-finance-stock-fell-sensex-nifty-crash/story/282695.html) |
| 3 | 13 & 23 Mar 2020 (COVID) | 13 Mar: −10.07% at 09:20 → 45-min halt → strong recovery (~14% low-to-close range); 23 Mar: Nifty **−12.98%** to 7,610 | Touched **86.63 on 24 Mar** — above the 2008 GFC closing peak 85.13 | Both wings tested in one day (13 Mar); −13% day (23 Mar); margin expansion at maximum (SEBI PR 18/2020) | [BusinessToday](https://www.businesstoday.in/markets/stocks/story/sensex-nifty-crash-circuit-breaker-rules-252013-2020-03-13), [Business Standard halt](https://www.business-standard.com/article/markets/sensex-hits-10-lower-circuit-trading-halted-for-45-minutes-120032300237_1.html), [BS close](https://www.business-standard.com/article/markets/market-live-markets-sensex-nifty-bse-nse-coronavirus-hdfc-ril-120032300127_1.html), [BS VIX](https://www.business-standard.com/article/markets/covid-19-impact-india-vix-touches-86-63-level-breaches-2008-peaks-120032400788_1.html), [TickJournal](https://tickjournal.com/indices/india-vix/historical-data/) |
| 4 | 23 May 2019 (election results) | Intraday +2.6% (Sensex >40,000 first time) → closed **−0.8%** | Collapsed 28 → sub-24 at open; −40% through the day; premiums −>50% at the open (blog-grade) | The mirror: vol-crush pays sellers at the OPENING mark; the pace decides who captures it | [Business Standard](https://www.business-standard.com/amp/article/markets/lok-sabha-election-results-2019-markets-sensex-nifty-election-impact-stocks-119052300136_1.html), [BusinessToday](https://www.businesstoday.in/markets/stocks/story/lok-sabha-election-results-2019-sensex-nifty-log-losses-first-time-on-a-result-day-201115-2019-05-23), [ournifty.com](https://ournifty.com/election-results-nifty-options.html) |
| 5 | 1 Feb 2021 (Budget melt-UP) | Nifty **+4.7%** (+646.6 pts), biggest Budget-day gain in 20 yrs | **FELL ~8%** during the session | Call wings run over on a one-way trend day with FALLING IV — no vega offset for the loss | [INDIA New England/PTI](https://indianewengland.com/sensex-nifty-log-biggest-single-day-rally-in-four-years-investors-gain-%E2%82%B916-lakh-crore/), [cleartax](https://cleartax.in/s/budget-day-market-movement-history-in-india) |
| 6 | 3–4 Jun 2024 (election results) | 3 Jun: +3%+ (biggest rally in 3 yrs); 4 Jun: **Nifty −5.93%**, Sensex −4,390 pts | May run-up (~doubling, UNVERIFIED) → **−14.9% on 3 Jun** → **+27.7% to 26.74 on 4 Jun** | The two-day trap: crush + rally re-centered strikes at compressed IV, then a −5.9% gap through the put wings at IV +28% | [Business Standard](https://www.business-standard.com/markets/stock-market-news/modi-3-0-prediction-drives-biggest-rally-in-3-yrs-sensex-nifty-up-over-3-124060301011_1.html), [BusinessToday](https://www.businesstoday.in/markets/market-commentary/story/election-results-2024-sensex-nifty-tank-as-india-catches-up-with-bjp-led-nda-psu-adani-stocks-fall-432045-2024-06-04), [BT VIX](https://www.businesstoday.in/markets/market-commentary/story/election-results-2024-india-vix-jumps-india-alliance-exceeds-expectations-432101-2024-06-04), [Upstox](https://upstox.com/news/election-results/latest-updates/lok-sabha-election-results-2024-live-bjp-or-cong-trends-stock-market-reaction/article-92218/) |
| 7 | 23 Jul 2024 (Budget whip + STT hike) | Mid-speech plunge to 24,074 (−~1.8%), recovered to close −0.12% | **+17.52% intraday** around the speech | Stop-then-reversion cost on the put wing; PLUS the rules shock: options STT 0.0625%→0.1%, futures 0.0125%→0.02% eff. 1-Oct-2024 | [Business Standard live](https://www.business-standard.com/markets/news/stock-market-live-updates-23-july-budget-2024-25-nirmala-sitharaman-sensex-nifty-gift-nifty-124072300078_1.html), [Upstox](https://upstox.com/news/market-news/stocks/sensex-nifty-50-erase-intraday-gains-to-end-on-a-flat-note/article-197056/), [ICICI Direct](https://icicidirect.com/research/equity/finace/new-stt-rules-in-futures-and-options-trading), [BusinessToday](https://www.businesstoday.in/union-budget/story/budget-2024-net-impact-of-stt-hike-on-fo-will-be-largely-neutral-for-traders-say-analysts-438433-2024-07-23) |
| 8 | 5 Aug 2024 (yen-carry unwind) | Sensex −2,222 pts (intraday −2,686); Nifty −662 pts (≈−2.68%) | **>+60% intraday** — sharpest surge in 9 years; close ~+42%/~20.4 (UNVERIFIED-EXACT) | Disproportionality: −2.7% underlying → +60% vol; put wings marked at exploded IV at 09:15 with no pre-open recourse | [Business Standard](https://www.business-standard.com/markets/news/stock-market-crash-today-aug-5-why-sensex-nifty-fell-24300-key-reasons-124080500152_1.html), [Upstox](https://upstox.com/news/market-news/trading/stock-market-crash-nifty-below-24-100-sensex-falls-over-2000-pts-key-factors-behind-market-fall/article-107541/), [BIS Bulletin 90](https://www.bis.org/publ/bisbull90.pdf), [HDFC Sky](https://hdfcsky.com/blogs/market-wtf-by-sky/the-vix-spike-nobody-saw-coming) |
| 9 | 20 Nov 2024 + 1 Sep 2025 (SEBI expiry consolidation) | Not a price event | Not a price event | The sellers' habitat rezoned: BankNifty/FinNifty/MidcpNifty weeklies KILLED; expiry days standardized NSE→Tue, BSE→Thu | [Zerodha support](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/weekly-expiries-discontinued-for-certain-index-derivatives), [Sharekhan](https://www.sharekhan.com/financial-blog/blogs/sebi-fo-rules-impact-nse-to-discontinue-three-weekly-option-contracts), [NSE FAOP64506](https://nsearchives.nseindia.com/content/circulars/FAOP64506.pdf), [Kotak Neo](https://www.kotakneo.com/news/market-news/sebi-to-end-thursday-expiry/), [Arihant Plus](https://www.arihantplus.com/blogs/market-updates/tuesday-is-the-new-thursday-nse-and-bse-expiry-shift-from-sep-2025/), [Ventura](https://www.venturasecurities.com/blog/changes-in-expiry-nse-and-bse/) |
| 10 | 7 Apr 2025 (tariff "Black Monday") | Sensex intraday −5.2% (low 71,425), closed −2,226 pts; Nifty −742 pts (≈−3.24%) | **>+56% intraday**; close ~+65.7%/~22.8 (UNVERIFIED-EXACT) | Gap-through-put-strike at the open for anything sold ≤4% OTM the prior Friday; worst mark in the first hour, partial recovery after | [Angel One](https://www.angelone.in/news/market-updates/closing-bell-april-07-2025), [Business Standard](https://www.business-standard.com/markets/news/trade-war-trump-tariffs-shake-sensex-nifty-market-view-what-to-buy-now-125040700318_1.html), [INDmoney](https://www.indmoney.com/blog/stocks/us-tariff-war-crashes-nifty-fifty) |
| 11a | 7–10 May 2025 (India–Pak crisis) | Mild: Sensex opened ~−692 pts 7 May, recovered flat; Nifty −0.88% over 23 Apr → 6 May | UNVERIFIED (8–12 May path) | The control case: a war headline that never became a vol regime — headline-conditioning ≠ vol-conditioning | [Wikipedia](https://en.wikipedia.org/wiki/2025_India%E2%80%93Pakistan_crisis), [Business Standard](https://www.business-standard.com/markets/news/operation-sindoor-how-have-stock-markets-reacted-to-india-pak-war-in-past-125050700765_1.html) |
| 11b | Mar–Jul 2026 (West-Asia war vol cycle) | Worst single day of cycle: UNVERIFIED (PENDING-ON-BOX) | **+22% single-day (4 Mar 2026)**, +119% YTD; a **+26.49% to 17.33** print; by 3 Jul 2026 −60% from cycle high (close 13.47 on 30 Jun) ⇒ derived peak ≈ 33–34 (UNVERIFIED-EXACT) | A multi-month build-then-crush cycle — the richest sustained short-premium harvest window in the lake's recent history, fully in-window | [Business Standard 4-Mar](https://www.business-standard.com/markets/stock-market-news/india-vix-spikes-22-on-west-asia-jitters-d-st-volatility-soars-119-ytd-126030400226_1.html), [HDFC Sky](https://hdfcsky.com/news/india-vix-surges-26-49-percent-to-17-33), [BS 3-Jul](https://www.business-standard.com/markets/news/india-vix-down-5-on-friday-to-pre-west-asia-war-levels-down-60-from-high-126070300543_1.html), [HDFC Sky 30-Jun](https://hdfcsky.com/news/india-vix-slips-over-1-percent-to-close-at-13-47-after-early-rise-ahead-of-june-fo-expiry-june-30-2026) |

---

## 5. The events — mechanism, reproduction requirements, our-data verdict

> Convention per event: **(a)** what happened; **(b)** the short-premium lesson; **(c)**
> BACKTEST REPRODUCTION REQUIREMENTS; **(d)** our-data verdict. B-numbers refer to §3.

### Event 1 — 5–6 Feb 2018: global "Volmageddon" (the canonical short-vol extinction)

**What happened.** Mon 5 Feb 2018 (US session) bleeding into Tue 6 Feb 2018 (India). CBOE VIX
**+116% in one day** — the largest % and point move since 1993 inception; it opened 6 Feb at
38.25 vs 18.84 the prior day
([sixfigureinvesting](https://www.sixfigureinvesting.com/2019/02/what-caused-the-february-5th-2018-volatility-spike-xiv-termination/)).
The inverse-VIX ETN **XIV lost ~95% in one session** ($1.9B → $63M) and was terminated; ~$4.1B
of shareholder value across two short-vol ETPs vanished in a day
([CNBC](https://www.cnbc.com/2018/02/05/xiv-exchange-traded-security-linked-to-volatility-plummets-80-percent.html),
[Bloomberg "Volmageddon"](https://www.bloomberg.com/news/articles/2019-02-06/the-day-the-vix-doubled-tales-of-volmageddon)).
In India the sell-off was compounded by the Budget-2018 LTCG announcement — Sensex fell sharply
on 2 and 5 Feb (sources disagree on the 2-Feb magnitude: ~570 pts to 35,328 per
[Wikipedia](https://en.wikipedia.org/wiki/Stock_market_crashes_in_India) vs 839 pts per
[BusinessToday](https://www.businesstoday.in/business/market/story/sensex-nifty-crash-ltcg-tax-on-equities-102121-2018-02-05)
— the discrepancy is recorded, not resolved), Nifty ~10,676 on 5 Feb, with 6 Feb extending
losses on the global rout
([BusinessToday 6-Feb](https://www.businesstoday.in/markets/stocks/story/sensex-nifty-crash-stock-market-sell-off-buy-sell-us-101879-2018-02-06)).
**India VIX level/spike % on 6 Feb 2018: UNVERIFIED** (verify: NSE historical VIX archive,
https://www.nseindia.com/reports-indices-historical-vix). No circuit halts in India.

**Short-premium lesson.** The mechanism outranks the local numbers: a *product class*
(systematic short vol) was deleted overnight by a ~2× vol-of-vol move with only a −4%
underlying move. Sellers did not die from delta; they died from **vega + the
forced-liquidation feedback loop** (ETP rebalancing buying VIX futures into the close).
India's short-strangle equivalent: a modest index move with a violent IV repricing is
sufficient for ruin.

**Reproduction requirements.** (i) A day where the underlying moves ≤2% but IV doubles — the
sim must mark short options to an IV-exploded price *intraday*, not to a delta-only
approximation; (ii) vol-of-vol as a first-class risk axis.

**Our-data verdict: NOT reproducible from vendor data** (pre-2020 floor, B1). Reproducible
**in class** as a synthetic chaos fixture: hold underlying ±2%, multiply 1-minute option
closes by an IV-explosion path, assert the seller sim marks-to-the-stored-bar (it does — bars
are the truth, B2) and that no screen clips the reprice (B11 flag). India-VIX exact path:
UNVERIFIED forever at 1-minute (NSE publishes daily OHLC only; intraday VIX at 1-minute for
2018 has no known public source — UNVERIFIED).

### Event 2 — 21 Sep 2018: IL&FS contagion / DHFL day (stock-level gamma catastrophe)

**What happened.** Fri 21 Sep 2018; the IL&FS default cascade ran Sep–Oct 2018. Sensex swung
**−1,128 points intraday** (Nifty −368) then recovered most of it *within the session* as
managements issued denials; **DHFL fell −59.67% intraday** to ₹246.25; Yes Bank crashed on RBI
action the same day; week: Sensex −3.28%
([BusinessToday](https://www.businesstoday.in/markets/stocks/story/sensex-nifty-crash-nbfcs-drive-market-down-110282-2018-09-21),
[BusinessToday/DHFL](https://www.businesstoday.in/markets/company-stock/dewan-housing-finance-stock-fell-sensex-nifty-crash/story/282695.html)).
India VIX that day: **UNVERIFIED** (NSE VIX archive). No index-wide halts; stock-level price
bands don't halt F&O-listed names (F&O stocks have no daily price band, only operating
ranges — **UNVERIFIED as stated**, verify via NSE circular on price bands for F&O securities).

**Short-premium lesson.** The **single-name** seller lesson: a stock option writer (covered
call ≈ synthetic short put; short puts on "safe" NBFCs) faced a −60% intraday underlying
move — put premium multiples in the **tens to hundreds of ×** (exact multiple UNVERIFIED — no
archived option-chain source found; would require the NSE F&O bhavcopy archive for
21-Sep-2018). Also the *whip* lesson: an index seller who panic-covered at −1,100 points
watched the market close nearly flat — exit discipline on spikes is itself a modeled behavior.

**Reproduction requirements.** (i) Stock-level short-put positions marked through a −60%
intraday path at 1-minute; (ii) intraday V-shape — the sim must not assume monotone paths
inside a day (1-minute bars carry this natively); (iii) single-name IV explosion while index
IV stays modest.

**Our-data verdict: NOT reproducible from vendor data** (pre-2020, B1). In class: partially —
the lake DOES ingest ~212 F&O stock option chains from 2020 (CLAUDE.md §1 DEC-P0-063), so
post-2020 single-name crash days (e.g., Adani Jan-2023) are the measurable analogues.
PENDING-ON-BOX: scan the stock-option lake for the worst single-session CE/PE multiples
(recipe §2).

### Event 3 — March 2020 COVID crash: the two circuit days, the −13% day, VIX 86.6

**What happened.**
- **13 Mar:** Nifty **−10.07% (−966 pts to 8,625) at 09:20** → 45-minute market-wide halt
  (first in 12 years) → 15-min pre-open → resumed 10:20 → "smart recovery"
  ([BusinessToday](https://www.businesstoday.in/markets/stocks/story/sensex-nifty-crash-circuit-breaker-rules-252013-2020-03-13));
  closed strongly positive (exact close % UNVERIFIED-EXACT; PENDING-ON-BOX: one query over the
  13-Mar 1m partition). Intraday range ≈ 14% low-to-close — **the single most hostile session
  shape for a strangle seller in the modern record: both wings tested in one day.**
- **23 Mar:** Sensex hit the −10% lower circuit again, 45-minute halt; Nifty closed **−12.98%
  (−1,135 pts) at 7,610**; Sensex −3,935 pts, ₹13.88 trn erased
  ([Business Standard halt](https://www.business-standard.com/article/markets/sensex-hits-10-lower-circuit-trading-halted-for-45-minutes-120032300237_1.html),
  [Business Standard close](https://www.business-standard.com/article/markets/market-live-markets-sensex-nifty-bse-nse-coronavirus-hdfc-ril-120032300127_1.html)).
- **India VIX:** touched **86.63 on 24 Mar 2020**, exceeding the Nov-2008 GFC closing peak of
  85.13 ([Business Standard](https://www.business-standard.com/article/markets/covid-19-impact-india-vix-touches-86-63-level-breaches-2008-peaks-120032400788_1.html);
  all-time-high 86.64 per [TickJournal](https://tickjournal.com/indices/india-vix/historical-data/)).
  From ~10–12 in Feb 2020 that is a ~7–8× vol repricing in a month (derived; exact Feb base
  UNVERIFIED).

**Short-premium lesson.** At VIX ~80, ATM straddle premia price ~5% daily moves — sellers who
survived to late March were selling *rich* premium but with **margin expansion at maximum**:
SEBI announced emergency regulatory measures on 20 Mar 2020 (PR No. 18/2020 — increased
margins for volatile stocks + F&O position-limit curbs; document exists at
[sebi.gov.in](https://www.sebi.gov.in/sebi_data/attachdocs/mar-2020/1584706501261.pdf) but the
fetch was blocked in the research environment — **exact percentages UNVERIFIED**). Peak SPAN +
exposure + ad-hoc additional margins routinely more than doubled per-lot requirements vs
February (industry folklore level — **UNVERIFIED**, no primary archive fetched). A seller can
be *right on premium* and still be liquidated by margin. Circuit terms: two 45-minute halts
(13 & 23 Mar) + 15-min reopening pre-open auctions — in lake terms **~45–60 absent 1-minute
bars mid-morning**, then a reopen print far from the last pre-halt print.

**Reproduction requirements.**
1. **Halted minutes = absent bars** — the lake must show a hole 09:20→10:20 with NO fabricated
   bars. Guaranteed by design (B2 never-fabricate; B12 — nothing flags or fills intra-session
   holes). ✅ representable.
2. **Reopen gap-through-strike:** the first present bar after the halt must be able to fill an
   exit at its adverse extreme — the worst-case fill law already does exactly this (B8). ✅
   behavior exists; the *fixture must assert* the exit lands on the first post-halt bar, not
   an interpolated price.
3. **Overnight gap (−13% open-to-open class):** requires positions held overnight — **NOT
   reproducible in the current engine** (B10 session_gap law force-closes at the prior
   session's last bar). Honest limit: only an intraday-seller variant of 23-Mar is simulatable
   today.
4. **IV explosion pace:** requires the enriched `iv` column on 1-minute option bars (B2/B5,
   [REPO brutex/data/fno_candle_lake.py:152]) — representable **iff** the March-2020 expired
   chain is still served by Groww (B4 — UNVERIFIED, operator probe) and the expiry−30d window
   law covers the studied contracts (B3 — the March monthly + weeklies: yes, within 30d of
   their expiries).
5. **Margin expansion:** **NOT reproducible** (B6 — refusal-only margin table). Must be a
   named ABSTAIN in any 2020 seller replay.

**Our-data verdict: PARTIALLY reproducible** — spot 1m from 2020-01-01 covers all three days
(B1 boundary is exactly 2020: 13-Mar-2020 > floor ✅); option chain availability UNVERIFIED
(B4); overnight legs impossible (B10); margin impossible (B6). PENDING-ON-BOX: absent-minute
counts on 13/23-Mar partitions; VIX 1m path (subject to the B5 gate).

### Event 4 — 23 May 2019: election results day (the SELLER'S WIN — and the long-vol lesson inverted)

**What happened.** Thu 23 May 2019 (Lok Sabha results). Intraday +2.6% — Sensex crossed 40,000
and Nifty 12,000 for the first time ever — then reversed to close **−0.8%** (first negative
close on a results day since 1999)
([Business Standard](https://www.business-standard.com/amp/article/markets/lok-sabha-election-results-2019-markets-sensex-nifty-election-impact-stocks-119052300136_1.html),
[BusinessToday](https://www.businesstoday.in/markets/stocks/story/lok-sabha-election-results-2019-sensex-nifty-log-losses-first-time-on-a-result-day-201115-2019-05-23)).
India VIX collapsed **28 → sub-24 at the open; −40% through the day; option premiums fell
>50% at the open itself** (blog-grade source, treat as directional:
[ournifty.com](https://ournifty.com/election-results-nifty-options.html)). No halts.

**Short-premium lesson.** The mirror event — **vol-crush pays sellers instantly at the open**.
The pace matters: >50% premium evaporation in the first prints means a seller's realized P&L
is decided by the OPENING mark, and a backtest that marks positions only at bar closes several
minutes in will overstate what a seller could capture (competition to buy back at the open).
The inverse lesson: anyone SHORT the post-event crush (long straddles into results) lost half
the premium before they could act — event-vol richness is bidirectional.

**Reproduction requirements.** (i) Opening-minute option premium collapse — needs the 09:15
1-minute option bar to carry the crushed price (it would; B2); (ii) IV-crush pace measurable
per-minute via `iv` (B2); (iii) an event-calendar feature (results dates) so strategies can be
conditioned on scheduled events — currently NO event calendar exists in the repo beyond the
NSE holiday/Muhurat calendar (`calendars/nse.py`) — a design gap for the options track.

**Our-data verdict: NOT reproducible from vendor data** (2019 < 2020 floor, B1). The 2024 twin
(Event 6) is the in-lake replacement.

### Event 5 — 1 Feb 2021: Budget melt-UP (+4.7%) — the call-seller's day of ruin

**What happened.** Mon 1 Feb 2021 (Union Budget). Nifty **+4.7% (+646.6 pts to 14,281.20)** —
the biggest Budget-day gain in 20 years; Sensex **+2,314.84 pts to 48,600.61**
([INDIA New England/PTI](https://indianewengland.com/sensex-nifty-log-biggest-single-day-rally-in-four-years-investors-gain-%E2%82%B916-lakh-crore/),
[cleartax Budget-day history](https://cleartax.in/s/budget-day-market-movement-history-in-india)).
India VIX **FELL ~8% during the session**
([same PTI report](https://indianewengland.com/sensex-nifty-log-biggest-single-day-rally-in-four-years-investors-gain-%E2%82%B916-lakh-crore/)).
No halts (the +10% upper circuit was never approached).

**Short-premium lesson.** The melt-UP asymmetry: short calls / short strangle upper wings were
run over by a +4.7% one-way trend day **while implied vol fell** — a "hedge with long vega"
overlay did nothing; the loss was pure delta/gamma on the call wing. Also the trend-day shape:
no meaningful pullback → intraday stop-and-re-sell strategies re-sold into a rising market
repeatedly (a whipsaw-free grind is the worst shape for gamma-short re-entry rules).

**Reproduction requirements.** (i) A one-way +5%-class trend day at 1-minute (in-lake ✅ —
2021 > floor); (ii) call-side premium path with FALLING iv (needs enriched option bars — B3
window law: Feb-2021 weeklies fully inside expiry−30d ✅, chain availability B4 UNVERIFIED);
(iii) the sim's same-bar tie law (SL-first, B8) exercised on the call wing.

**Our-data verdict: REPRODUCIBLE for spot; option legs pending the B4 probe.** PENDING-ON-BOX:
1-Feb-2021 1m partition; ATM 04-Feb-2021-expiry CE path.

### Event 6 — 3–4 Jun 2024: election results (the modern seller catastrophe)

**What happened.** The run-up (May 2024) → Mon 3 Jun (exit-poll melt-up) → Tue 4 Jun 2024
(results crash). 3 Jun: biggest rally in 3 years, Sensex/Nifty **+3%+** on exit polls
([Business Standard](https://www.business-standard.com/markets/stock-market-news/modi-3-0-prediction-drives-biggest-rally-in-3-yrs-sensex-nifty-up-over-3-124060301011_1.html)).
4 Jun: **Nifty −5.93%**, Sensex −4,390 pts — worst day since COVID; investor wealth −₹30.5
lakh crore
([BusinessToday](https://www.businesstoday.in/markets/market-commentary/story/election-results-2024-sensex-nifty-tank-as-india-catches-up-with-bjp-led-nda-psu-adani-stocks-fall-432045-2024-06-04),
[Upstox](https://upstox.com/news/election-results/latest-updates/lok-sabha-election-results-2024-live-bjp-or-cong-trends-stock-market-reaction/article-92218/)).
**India VIX path (the whole point):** ran UP through May (widely reported ~doubling over ~3
weeks into the results — **run-up magnitude UNVERIFIED here**; verify: NSE VIX archive
May-2024) → **−14.9% on 3 Jun** (crush on exit-poll certainty) → **+27.7% to 26.74 on 4 Jun**
(intraday first ~+12% to 26.37 as trends turned)
([BusinessToday VIX](https://www.businesstoday.in/markets/market-commentary/story/election-results-2024-india-vix-jumps-india-alliance-exceeds-expectations-432101-2024-06-04)).
No circuit halts — −5.93% stayed inside the −10% band (nearest miss since 2020).

**Short-premium lesson.** The **two-day trap**. Sellers who wrote strangles on 3 Jun harvested
a vol crush AND a +3% rally — then were gapped −5.9% through their put wings the next morning
with IV +28%. Both legs of the classic seller edge (crush + range) inverted inside 24 hours.
The 3-Jun VIX *fall* is what made 4-Jun premia lethal: strikes were re-centered upward at
compressed IV the evening before. Widely reported retail option-seller wipeouts that week are
anecdotal (no auditable aggregate source found — UNVERIFIED as a number).

**Reproduction requirements.** (i) **Overnight re-strike trap:** a backtest must mark the
3-Jun strike selection against the 4-Jun open — for an *intraday-only* engine this event
degrades to "day-2 standalone", which UNDERSTATES the trap (B10 honest limit; a held-overnight
option-position model is a named future DEC); (ii) VIX-conditioned sizing (the May run-up was
a visible warning — VIX-level gates are stampable today, B5); (iii) event-calendar
conditioning (results date known in advance — same gap as Event 4); (iv) option-leg P&L incl.
the put wing going ~1,380 Nifty points ITM intraday (PENDING-ON-BOX: 06-Jun-2024-expiry
chain, straddle-multiple recipe §2 — the chain is post-2020 and weekly, so B3/B1 both
satisfied; the B4 probe still governs).

**Our-data verdict: REPRODUCIBLE (spot + VIX + weekly option chain, subject to B4), EXCEPT
the overnight-hold dimension (engine limit B10) and margin surcharges applied by brokers that
week (B6 + UNVERIFIED).** This is the **#1 candidate for the first seller golden-day
fixture**: fully in-window, both directions, no halt complications.

### Event 7 — 23 Jul 2024: Budget whip + the STT hike (a *rules* shock, not just a price shock)

**What happened.** Tue 23 Jul 2024. Mid-speech plunge to Nifty 24,074 (−~1.8% from highs) on
the capital-gains rejig, then a recovery of 405 pts to close **−0.12% at 24,479**; Sensex
recovered ~1,200 pts off the low to close −0.09%
([Business Standard live](https://www.business-standard.com/markets/news/stock-market-live-updates-23-july-budget-2024-25-nirmala-sitharaman-sensex-nifty-gift-nifty-124072300078_1.html),
[Upstox](https://upstox.com/news/market-news/stocks/sensex-nifty-50-erase-intraday-gains-to-end-on-a-flat-note/article-197056/)).
India VIX surged up to **+17.52% intraday** around the speech
([same Business Standard coverage](https://www.business-standard.com/markets/news/stock-market-live-updates-23-july-budget-2024-25-nirmala-sitharaman-sensex-nifty-gift-nifty-124072300078_1.html)).
**The structural payload — STT hike announced:** options STT **0.0625% → 0.1% of premium**;
futures **0.0125% → 0.02% of price**, **effective 1 Oct 2024**
([ICICI Direct](https://icicidirect.com/research/equity/finace/new-stt-rules-in-futures-and-options-trading),
[BusinessToday](https://www.businesstoday.in/union-budget/story/budget-2024-net-impact-of-stt-hike-on-fo-will-be-largely-neutral-for-traders-say-analysts-438433-2024-07-23)).

**Short-premium lesson.** Two lessons. (1) An intraday V-whip around a *scheduled* speech:
iron-condor sellers were stopped on the put wing at 24,074 and watched the market close back
inside the range — stop-then-reversion cost, at 1-minute pace. (2) **Cost regimes are DATED**:
any seller backtest that applies one flat STT/ETC across 2020→2026 mis-states net P&L; the
boundary is 2024-10-01. Repo tie-in (already built correctly): BruteX encodes futures STT
0.02% eff. 2024-10-01 ([REPO brutex/costs/futures_costs.py:198-200]) and the options executor
date-keys unverified cost windows into counted refusals
([REPO brutex/options/executor.py:184,619]) — the seller track must extend the same
regime-table discipline to option STT/ETC per leg (options ETC verified rows also start
2024-10-01 per DEC-ETC-REGIME-HISTORY-001, CLAUDE.md §1).

**Reproduction requirements.** (i) 1-minute whip with recovery (in-lake ✅); (ii) **date-keyed
cost regime application across the 2024-10-01 boundary** (machinery exists; needs option-STT
rows added under a cited DEC); (iii) a VIX intraday sub-day spike of <20% — a *moderate* vol
event class, useful as the "survivable day" control fixture.

**Our-data verdict: FULLY REPRODUCIBLE** (spot; option legs subject to B4; costs gross-only
pre-boundary by design). The natural "seller survives" control-day golden fixture.

### Event 8 — 5 Aug 2024: yen-carry unwind (gap-down + the sharpest VIX spike in 9 years)

**What happened.** Mon 5 Aug 2024 (after the BoJ hike 31 Jul + weak US payrolls 2 Aug).
Sensex **−2,222 pts to 78,759**; Nifty **−662 pts to 24,055** (−2.68% derived from the
sourced points: 662/24,717); Sensex intraday −2,686 pts; ₹15 lakh crore erased; Nikkei −12%
the same day
([Business Standard](https://www.business-standard.com/markets/news/stock-market-crash-today-aug-5-why-sensex-nifty-fell-24300-key-reasons-124080500152_1.html),
[Upstox](https://upstox.com/news/market-news/trading/stock-market-crash-nifty-below-24-100-sensex-falls-over-2000-pts-key-factors-behind-market-fall/article-107541/),
[BIS Bulletin 90](https://www.bis.org/publ/bisbull90.pdf)). India VIX **leaped >60% intraday**
(nearly touched +62% by midday) — the sharpest single-day surge in nine years
([HDFC Sky](https://hdfcsky.com/blogs/market-wtf-by-sky/the-vix-spike-nobody-saw-coming));
closing spike ~+42% to ~20.4 (**closing figure UNVERIFIED-EXACT**; verify NSE VIX archive
05-Aug-2024). No halts.

**Short-premium lesson.** The **disproportionality lesson**: a −2.7% underlying day produced a
+60% vol day — IV response is convex in stress, so strangle marks blew out far beyond what
delta implies. The Monday-morning gap (Nifty opened ~−2.5% — **UNVERIFIED-EXACT open gap**;
PENDING-ON-BOX: first 1m bar vs the 2-Aug close) meant put wings were marked against exploded
IV at 09:15 with no chance to adjust pre-open. This is also the canonical **weekend/gap-risk**
exhibit for any future overnight-seller design.

**Reproduction requirements.** (i) Opening 1-minute bar carrying the full gap (✅ B2);
(ii) per-minute IV path showing the convex explosion (enriched `iv`, B4-gated); (iii) VIX 1m
alongside (B5-gated); (iv) worst-case fills on the first bars (✅ B8); (v) again the overnight
dimension is out of engine scope (B10).

**Our-data verdict: REPRODUCIBLE intraday; the gap-exposure half needs the future
overnight-position DEC.** Second-best golden-fixture candidate after Event 6.

### Event 9 — Nov 2024 weekly-expiry consolidation (STRUCTURAL event: the sellers' habitat was rezoned)

**What happened.** SEBI measure effective **20 Nov 2024**; second wave effective
**1 Sep 2025**. Wave 1: one weekly-expiry index per exchange — NSE kept **Nifty** weeklies;
**BankNifty, FinNifty, MidcpNifty weeklies discontinued**; BSE kept **Sensex**; Bankex
weeklies gone
([Zerodha support](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/weekly-expiries-discontinued-for-certain-index-derivatives),
[Sharekhan](https://www.sharekhan.com/financial-blog/blogs/sebi-fo-rules-impact-nse-to-discontinue-three-weekly-option-contracts),
NSE circular [FAOP64506](https://nsearchives.nseindia.com/content/circulars/FAOP64506.pdf)).
Exact final-expiry dates per index: **UNVERIFIED here** (the circular is the verification
source; the fetch was proxy-blocked). Wave 2: from **1 Sep 2025** expiry DAYS were
standardized — **NSE → Tuesday, BSE → Thursday** (live contracts auto-adjusted after close
28 Aug 2025) ([Kotak Neo](https://www.kotakneo.com/news/market-news/sebi-to-end-thursday-expiry/),
[Arihant Plus](https://www.arihantplus.com/blogs/market-updates/tuesday-is-the-new-thursday-nse-and-bse-expiry-shift-from-sep-2025/),
[Ventura](https://www.venturasecurities.com/blog/changes-in-expiry-nse-and-bse/)).

**Short-premium lesson.** Not a price shock — a **regime discontinuity in the strategy
universe itself**: (1) BankNifty weekly selling (the highest-volume seller habitat
pre-Nov-2024) *ceased to exist* — any backtest spanning the boundary must not extrapolate a
dead contract class forward; (2) theta-day-of-week structure changed twice (Thu → [BSE Fri/Tue
interim] → NSE Tue / BSE Thu) — day-of-week seasonality learned pre-boundary is invalid after
it; (3) 0DTE liquidity migrated across exchanges/days.

**Reproduction requirements.** (i) An **expiry-calendar-as-data** design: the lake already
treats the vendor expiry calendar as authority (`registry.duckdb` via `get_expiries`,
CLAUDE.md §6) — so the discontinuities are *automatically present* in the stored chains
(contracts simply stop existing); (ii) the sweep/selection layer must key strategies to "the
front weekly of exchange X" rather than a hardcoded weekday — a structure-lock test: assert no
weekday literal governs expiry selection; (iii) a boundary test that a BankNifty-weekly
strategy's tradable universe goes to ZERO after its final weekly expiry (honest absence, never
a synthetic continuation).

**Our-data verdict: FULLY REPRODUCIBLE — the data reproduces it BY ITSELF** (vendor calendar
truth). The risk is in the *strategy code*, not the data: this event's "fixture" is a
structural unit test, not a golden day.

### Event 10 — 7 Apr 2025: tariff "Black Monday" (gap −5%, VIX +56–66%)

**What happened.** Mon 7 Apr 2025 (Trump tariff-escalation weekend). Sensex intraday low
71,425 (−3,939.6 pts, −5.2% at worst), closed **−2,226 pts**; Nifty closed **−742 pts
(≈ −3.24%)**; ₹19 lakh crore erased at the close (a ₹16 lakh crore intraday quote also
circulated) ([Angel One closing bell](https://www.angelone.in/news/market-updates/closing-bell-april-07-2025),
[Business Standard](https://www.business-standard.com/markets/news/trade-war-trump-tariffs-shake-sensex-nifty-market-view-what-to-buy-now-125040700318_1.html),
[INDmoney](https://www.indmoney.com/blog/stocks/us-tariff-war-crashes-nifty-fifty)). India VIX
**soared >56% intraday**
([Business Standard](https://www.business-standard.com/markets/news/trade-war-trump-tariffs-shake-sensex-nifty-market-view-what-to-buy-now-125040700318_1.html));
widely quoted closing +65.7% to ~22.8 (**closing figure UNVERIFIED-EXACT** — verify NSE VIX
archive 07-Apr-2025). No halts (−5.2% worst, inside the band).

**Short-premium lesson.** The Aug-2024 pattern repeated at larger gap scale: a ~−5%
opening-hour trough with IV +56% marks a short strangle at maximum pain in the first hour,
followed by a partial intraday recovery — sellers stopped at the lows realized the worst mark
of the day. Gap-through-put-strike at the open for anything sold ≤4% OTM the prior Friday.

**Reproduction requirements.** Identical to Event 8 (gap open, convex IV, worst-case first-bar
fills), plus a *recovery-leg* assertion (close well off the lows — stop-discipline cost
measurable at 1-minute).

**Our-data verdict: REPRODUCIBLE intraday (spot ✅; options B4-gated; VIX B5-gated; overnight
dimension B10-limited).**

### Event 11 — 2025–2026 vol events (sourced)

**11a. May 2025 India–Pakistan crisis (Operation Sindoor, 7–10 May 2025).** Strikes began
7 May; ceasefire 10 May ([Wikipedia](https://en.wikipedia.org/wiki/2025_India%E2%80%93Pakistan_crisis)).
The index event was **mild**: Sensex opened ~692 pts lower on 7 May and recovered to flat;
Nifty −0.88% over 23 Apr → 6 May
([Business Standard](https://www.business-standard.com/markets/news/operation-sindoor-how-have-stock-markets-reacted-to-india-pak-war-in-past-125050700765_1.html)).
Seller meaning: a *war headline* that never became a vol regime — the control case proving
headline-conditioning ≠ vol-conditioning. VIX path around 8–12 May: **UNVERIFIED** (NSE
archive).

**11b. West-Asia war vol cycle, Mar–Jul 2026.** India VIX **+22% single-day on West-Asia
jitters (4 Mar 2026)** with YTD volatility +119%
([Business Standard](https://www.business-standard.com/markets/stock-market-news/india-vix-spikes-22-on-west-asia-jitters-d-st-volatility-soars-119-ytd-126030400226_1.html));
a same-cycle single-day **+26.49% to 17.33** print
([HDFC Sky](https://hdfcsky.com/news/india-vix-surges-26-49-percent-to-17-33)); by
**3 Jul 2026** VIX had fallen −5% on the day to pre-war levels, **down 60% from the cycle
high** ([Business Standard](https://www.business-standard.com/markets/news/india-vix-down-5-on-friday-to-pre-west-asia-war-levels-down-60-from-high-126070300543_1.html)),
with a 30-Jun-2026 close of 13.47
([HDFC Sky](https://hdfcsky.com/news/india-vix-slips-over-1-percent-to-close-at-13-47-after-early-rise-ahead-of-june-fo-expiry-june-30-2026)).
Derived (UNVERIFIED-EXACT): the cycle peak was therefore ≈ 33–34 (13.47/0.4). Seller meaning:
a multi-month **build-then-crush cycle** — the richest sustained short-premium harvest window
in the lake's recent history, AND fully inside the vendor window with the post-Sep-2025
Tue/Thu expiry structure. **This is the natural first *multi-week* seller validation corpus.**
The underlying's worst single day in the cycle: **UNVERIFIED here** (PENDING-ON-BOX: min daily
return over Mar–Jun 2026 from the 1day partitions).

---

## 6. THE REPRODUCTION MATRIX

| Event-class | Data signature (what the lake must show) | Required sim behavior | Our-data verdict | Test type |
|---|---|---|---|---|
| **Overnight gap through strikes** (Events 3/23-Mar, 6, 8, 10) | First 1m bar of day D at ≫/≪ day D−1 last bar; option put/call wing deep ITM at 09:15 | Mark short legs at the gapped open; fills at the adverse extreme of the first present bar (B8 ✅); position HELD across the gap | **PARTIALLY** — spot/option bars carry the gap ✅ (2020+ only, B1); but the engine force-closes at session end (B10) so *held-through* exposure needs a new position-lifecycle DEC | Golden day fixture (Jun-4-2024 first) + a NEW engine-scope DEC before it can be end-to-end |
| **Intraday IV/VIX explosion pace** (Events 1-class, 8, 10) | Per-minute `iv` on option bars rising convexly while underlying moves ≤3%; VIX 1m series alongside | Mark-to-stored-bar (never model-priced); vega P&L emerges from bar prices; no screen may clip >4× option prints (B11 flag) | **PARTIALLY** — bars are the truth ✅; `iv` column exists ([REPO brutex/data/fno_candle_lake.py:152]); gated on B4 (expired-chain depth probe) + B5 (VIX 1m depth probe); option read path verified unscreened ✅ | Golden day fixture (Aug-5-2024) + a structure-lock test pinning "no bad-tick screen on option series" |
| **Circuit halt = absent bars + reopen jump** (Event 3) | ~45–60 missing 1m bars mid-session; reopen bar far from pre-halt bar; day still stored (~330 bars, no detector fires — B12) | No fabrication/fill of halted minutes (B2 ✅); exits/entries impossible during the hole; first post-halt bar takes the worst-case fill | **REPRODUCIBLE** for 13/23-Mar-2020 spot (post-floor ✅); option chain B4-gated | Golden day fixture with an explicit absent-minute-count pin (nothing else guards it, B12) + chaos twin (synthetic halt injected into a normal day) |
| **Vol-crush at the open** (Events 4, 6/3-Jun) | 09:15 option bars at ≤50% of prior close; VIX −15–40% | Seller P&L realized at the opening mark; buy-back latency honesty (can't assume fills better than the first bars' adverse extremes — B8 is conservative in the *right* direction here) | **REPRODUCIBLE** for 2020+ instances (3-Jun-2024 ✅); the 2019 archetype is out of reach (B1) | Golden day fixture (Jun-3-2024) |
| **One-way melt-up vs short calls** (Event 5) | +4–5% monotone 1m trend day; falling `iv`; call wing ITM | Delta/gamma loss with NO vega offset; same-bar SL-first tie law on the call wing (B8) | **REPRODUCIBLE** (1-Feb-2021 spot ✅; option legs B4-gated) | Golden day fixture |
| **Stock-level −60% gamma catastrophe** (Event 2 class) | Single-name 1m collapse; single-name IV explosion; index calm | Short single-stock puts marked through the collapse; per-name isolation (one name's corruption never aborts the run — the existing per-symbol quarantine posture) | **NOT for 2018** (B1); **class-reproducible** on post-2020 single-name crash days from the ~212-stock option lake | Chaos fixture (synthetic −60% path) + PENDING-ON-BOX scan for the worst real in-lake single-name day |
| **Margin expansion / forced deleveraging** (Events 3, 6, 8) | Dated SPAN+exposure+additional-margin table; broker ad-hoc surcharges | Margin-utilization path; liquidation-before-recovery mechanics | **NOT REPRODUCIBLE** — zero verified margin rows, refusal-only by design ([REPO brutex/engine/margin_history.py:8,238]); no vendor source in scope | Named ABSTAIN (the honest verdict); becomes testable only after a DEC lands a cited, dated margin table |
| **Cost-regime discontinuity** (Event 7) | Same trade priced differently across 2024-10-01 (STT 0.0625%→0.1% premium / futures 0.0125%→0.02%) | Date-keyed cost application; pre-boundary = counted refusal, never an invented rate (the executor already does this: [REPO brutex/options/executor.py:619]) | **REPRODUCIBLE by construction** for futures (encoded, [REPO brutex/costs/futures_costs.py:198]); option-STT rows need their own cited DEC | Golden cost-boundary test (one trade either side of 2024-10-01) |
| **Structural universe discontinuity** (Event 9) | BankNifty/FinNifty/MidcpNifty weekly contracts cease after Nov-2024; expiry weekday flips at 1-Sep-2025 | Strategy universe derives from the vendor expiry calendar, never a hardcoded weekday; dead contract classes go honestly absent | **FULLY REPRODUCIBLE** — the vendor calendar (registry.duckdb, CLAUDE.md §6) carries the discontinuity natively | Structural unit test (universe→zero after final weekly; no weekday literal in selection) |
| **Multi-week vol build-then-crush cycle** (Event 11b) | VIX 1m/1day rising ~2.5× over weeks then −60% decay; term premia rich then crushed | Position-sizing / entry-gating conditioned on VIX level & slope (stampable today, B5); rolling short-premium P&L across the cycle | **REPRODUCIBLE** (Mar–Jul 2026, fully in-window, post-migration expiry structure) — B4/B5 probes govern option/VIX depth | Multi-week backtest corpus (the seller track's first validation range) |

---

## 7. Cross-event design demands the seller track inherits

1. **Overnight position lifecycle is THE gap** — 4 of 11 events lose their teeth in an
   intraday-only engine (B10). The seller DEC must either (a) stay honestly intraday-only and
   SAY SO on every result surface, or (b) introduce held-overnight option positions as a new,
   separately-DEC'd lifecycle.
2. **Margin is a named ABSTAIN, forever, until a dated table lands** (B6). Never a fabricated
   proxy.
3. **Bars are the pricing truth; models never override stored prices** — the existing
   mark-to-stored-bar + worst-case-fill posture (B2/B8) is exactly right for seller honesty;
   keep it.
4. **The 4× bad-tick screen must be structurally excluded from option series** (B11) — one
   structure-lock test.
5. **Halt days need explicit absent-minute pins** — no detector will ever flag an
   intra-session hole (B12).
6. **Cost/expiry/margin regimes are all DATED data, not constants** (Events 7, 9) — the
   repo's regime-table + calendar-authority patterns already model this; extend, don't invent.
7. **Pre-2020 catastrophes live only as chaos fixtures** (B1) — synthetic paths asserting
   *mechanisms* (IV×k repricing, −60% single-name, vol-of-vol) with no false claim of
   historical replay.
8. **Tail-aware validation is mandatory** (from the VRP crash-risk framing): ~6 lake-years
   contain only a handful of true vol shocks, so a high-win-rate short-premium strategy can
   pass naive gates while its ruin event simply never occurred in-sample; never rank selling
   strategies by Sharpe/win-rate alone — tail metrics (worst-5-days, MAE, minute-path drawdown,
   already persisted by the results-depth pass, CLAUDE.md §1 #1617) must be first-class
   ranking inputs, and permutation/bootstrap resampling of a negative-skew series
   under-represents the tail exactly when the sample lacks one (the existing cascade gates,
   [REPO brutex/metrics/cascade.py:12-13,161-167], reduce but do not eliminate the hazard —
   a selling-specific synthetic gap/vol-shock stress leg is a named future gate).

---

## 8. Consolidated UNVERIFIED register

| # | Claim | Status | What would verify it |
|---|---|---|---|
| U1 | India VIX level/spike on 6 Feb 2018 | UNVERIFIED | NSE historical VIX archive (https://www.nseindia.com/reports-indices-historical-vix), date 06-02-2018 |
| U2 | Sensex 2-Feb-2018 fall magnitude (570 vs 839 pts — sources conflict) | CONFLICTING SOURCES (both cited in Event 1) | BSE EOD archive for 02-Feb-2018 |
| U3 | India VIX on 21 Sep 2018; DHFL put-premium multiples that day | UNVERIFIED | NSE VIX archive + NSE F&O bhavcopy 21-09-2018 |
| U4 | F&O-stock price-band/halt mechanics as stated (no daily band, operating ranges) | UNVERIFIED | NSE circular on price bands for securities in the derivatives segment |
| U5 | Nifty 13-Mar-2020 exact closing % after the halt recovery | UNVERIFIED-EXACT | PENDING-ON-BOX (1m partition, §2 recipe) or NSE EOD archive |
| U6 | SEBI PR 18/2020 exact margin percentages (fetch 403-blocked in the research environment) | UNVERIFIED (document existence sourced) | Fetch https://www.sebi.gov.in/sebi_data/attachdocs/mar-2020/1584706501261.pdf from an unblocked network |
| U7 | "Peak-COVID margins more than doubled per lot" | UNVERIFIED (industry folklore) | Archived NSE SPAN files / broker margin archives Mar-2020 |
| U8 | May-2024 VIX run-up "~doubled over ~3 weeks" | UNVERIFIED | NSE VIX archive, 22-Apr → 31-May-2024 |
| U9 | Aggregate retail option-seller losses around 4-Jun-2024 | UNVERIFIED (anecdotal press only) | No known auditable source; SEBI F&O loss studies cover FY-level, not event-level |
| U10 | India VIX closing spike/level 5-Aug-2024 (~+42% / ~20.4) and 7-Apr-2025 (~+65.7% / ~22.8) | UNVERIFIED-EXACT (intraday +60% / +56% ARE sourced) | NSE VIX archive for both dates |
| U11 | Nifty opening-gap sizes on 5-Aug-2024 / 7-Apr-2025 | UNVERIFIED-EXACT | PENDING-ON-BOX: first 1m bar vs prior close (§2 recipe) |
| U12 | Exact final weekly-expiry dates for BankNifty/FinNifty/MidcpNifty/Bankex (Nov 2024) | UNVERIFIED (rule + effective date 20-Nov-2024 ARE sourced) | NSE circular FAOP64506 (URL in Event 9; fetch blocked in the research environment) |
| U13 | VIX path 8–12 May 2025 (Sindoor week) and the 12-May relief-rally magnitude | UNVERIFIED | NSE VIX + EOD archives, 07–12-May-2025 |
| U14 | 2026 West-Asia cycle VIX peak ≈ 33–34 | DERIVED from two sourced points (13.47 close 30-Jun-2026; "down 60% from high" 03-Jul-2026), not directly sourced | NSE VIX archive Mar–Jun 2026 |
| U15 | Groww still serves 2020-era expired option chains (`get_expiries` depth) | UNVERIFIED — Phase-0-style probe question (B4) | Operator-run credentialed probe through the existing doctor-kit read-only client |
| U16 | India VIX 1-minute historical depth on Groww to 2020 | UNVERIFIED — CLAUDE.md §6 marks INDIA VIX candle availability a Phase-0 HARD-GATE | Same probe as U15, symbol NSE-INDIAVIX |
| U17 | Broadie–Chernov–Johannes 2009 specifics (thesis-paragraph jump-risk framing) | UNVERIFIED (canonical citation from prior knowledge) | Fetch *RFS* 22(11) 4493–4529 |
| U18 | All PENDING-ON-BOX figures (see §9) | MEASURABLE, not measured (no lake in the research environment — §2) | Run the §2 recipes on the production lake |

### 9. PENDING-ON-BOX measurement list

Each item is measurable on the production box with the §2 recipes (read-only), and none is
claimable until measured:

1. **13-Mar-2020 & 23-Mar-2020 absent-minute counts** — count distinct 1m bars vs the 375-bar
   session grid on the NSE-NIFTY / BSE-SENSEX 1minute partitions (the halt-hole pin, B12).
2. **13-Mar-2020 exact closing %** after the halt recovery (U5).
3. **Opening-gap sizes 5-Aug-2024 and 7-Apr-2025** — first 1m bar open vs prior-session last
   close (U11).
4. **Jun-2024 straddle multiples** — `CE.close + PE.close` at the 3-Jun reference minute vs
   the 4-Jun event-day max, same strike, 06-Jun-2024 expiry (§2 straddle recipe; B4-gated).
5. **1-Feb-2021 melt-up path** — the 1m partition + the ATM 04-Feb-2021-expiry CE path
   (B4-gated).
6. **Worst single-session single-name CE/PE multiples in the stock-option lake** (post-2020
   Event-2 analogues; ~212-underlying scan).
7. **Worst single day of the Mar–Jun 2026 West-Asia cycle** — min daily return over the 1day
   partitions.
8. **India VIX 1-minute depth + paths** for every event date ≥2020 (subject to the B5/U16
   probe).
9. **Groww expired-chain depth probe** (U15/B4) — the gate on every option-leg item above.

---

*Sources used (all cited inline): Business Standard, BusinessToday, Upstox, CNBC, Bloomberg,
sixfigureinvesting.com, Wikipedia, ICICI Direct, Angel One, INDmoney, HDFC Sky, Kotak Neo,
Arihant Plus, Ventura Securities, Zerodha Support, Sharekhan, BIS (Bulletin 90), ournifty.com
(blog-grade, flagged), cleartax, INDIA New England/PTI, NSE circular archive, SEBI document
archive, TickJournal; thesis sources: Carr & Wu 2009, Bakshi & Kapadia 2003 (URLs in §1).*

*Repo citations verified by reading in the research session:
`brutex/dashboard/pull_controller.py:163`, `brutex/adapters/groww/constants.py:103`,
`brutex/adapters/groww/contract_bounds.py:104-118`, `brutex/data/bad_tick.py:76,141-166`,
`brutex/data/interior_gap_detect.py:20,98-105`, `brutex/data/today_completeness.py:35-36,367`,
`brutex/data/tf_resampler.py:31,254`, `brutex/data/session.py:16-18,251-258`,
`brutex/calendars/nse.py:429-430`, `brutex/engine/trade_sim.py:65,161,270`,
`brutex/engine/margin_history.py:8,233-270`, `brutex/execution/fill_mode.py:33,72`,
`brutex/costs/futures_costs.py:27-29,198-203`, `brutex/options/executor.py:184,487-530,619,651`,
`brutex/options/lake_source.py:141` (no screen import), `brutex/data/fno_candle_lake.py:152`,
`brutex/data/catalog.py:477`, `brutex/doctor_live.py:417`,
`brutex/metrics/cascade.py:12-13,161-167`.*
