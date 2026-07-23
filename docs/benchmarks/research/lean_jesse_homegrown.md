# Research Notes — LEAN family: QuantConnect LEAN · Jesse · Homegrown pandas/numpy baseline

Date: 2026-07-18. Researcher: worker (benchmark head-to-head vs BruteX).
Method: WebSearch + WebFetch through the agent proxy. NOTE ON FETCH FAILURES: the proxy returned
HTTP 403 for direct fetches of quantconnect.com/docs, quantconnect.com/pricing, jesse.trade/pricing,
quantrocket.com/blog and quantrocket.medium.com — for those, facts come from WebSearch result
snippets that quote the named page; such items are labeled **VERIFIED-via-search-snippet(<url>)**
(the URL is the authoritative page the snippet quoted, not independently re-read). GitHub pages
fetched fine.

Label legend:
- **VERIFIED(url)** — page fetched and read directly.
- **VERIFIED-via-search-snippet(url)** — claim quoted by the search engine from the named page; direct fetch blocked (403).
- **UNVERIFIED** / **UNVERIFIED-general-knowledge** — no citation found this session; do not present as fact.

---

## ENGINE 1 — QuantConnect LEAN

### 1. Architecture
- Event-driven, modular, plug-in based engine; core written in **C#**; algorithms in **C# or Python 3.11**;
  data pushed to the algorithm as time-series events (not a simple time loop); parallel data feeds /
  order execution / results reporting; nearly all core functionality behind interfaces (data sources,
  brokerage, execution, risk). **VERIFIED-via-search-snippet(https://www.quantconnect.com/docs/v2/lean-engine)**
  and **VERIFIED(https://github.com/QuantConnect/Lean)** — repo self-describes as "an event-driven,
  professional-caliber algorithmic trading platform", language breakdown C# 94.2% / Python 5.6%,
  dedicated Algorithm.CSharp + Algorithm.Python project folders, Jupyter research environment,
  CLI for local + cloud-hybrid workflows.

### 2. Raw speed
- No official throughput number published by QuantConnect found this session. **UNVERIFIED** for any
  official bars/sec figure.
- Third-party comparison (QuantRocket, 2023): "For a large universe of securities, **Lean runs 12x
  slower than Zipline and 75x slower than Moonshot**; all 3 backtesters process the small universe
  in under 30 seconds." Being C# does not make it fast per-bar vs numpy-vectorized engines.
  **VERIFIED-via-search-snippet(https://www.quantrocket.com/blog/backtest-speed-comparison/)**
  (direct fetch 403; Medium mirror also 403; test code repo:
  https://github.com/quantrocket-codeload/backtest-speed-comparison). Vendor-authored by a
  competitor (QuantRocket makes Moonshot) — flag as potentially self-serving, but the code is public.
- Cloud backtest node speed is a paid axis: "Backtesting nodes that are more powerful can run faster
  backtests… more RAM handles… Options data… large universes."
  **VERIFIED-via-search-snippet(https://www.quantconnect.com/docs/v2/cloud-platform/organizations/resources)**

### 3. Scale ceiling / parameter optimization
- Cloud parameter optimization runs many full backtests in parallel on **paid distributed compute
  nodes**; marketing claim "what might take weeks on a local machine finishes in minutes";
  optimization jobs can run multiple backtests concurrently without owning multiple backtest nodes.
  **VERIFIED-via-search-snippet(https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/deployment
  + https://www.quantconnect.com/forum/discussion/8590/Parameter+Optimization)**
- Node pricing (third-party review, 2026): additional nodes "$24/month (L1-1, 1 CPU, 1GB RAM) to
  $1,000/month (GPU nodes, 24 cores / 128GB)". **VERIFIED-via-search-snippet(https://newyorkcityservers.com/blog/quantconnect-review)** — third-party figure, treat as approximate.
- Free tier: one free B-MICRO backtest node, 20-second launch delay, **200 backtests/day cap**.
  **VERIFIED-via-search-snippet(https://www.quantconnect.com/docs/v2/cloud-platform/organizations/resources
  — quoted via the newyorkcityservers review)**.
- Local LEAN CLI optimization exists (grid search locally): docs page "Local Platform / Optimization /
  Getting Started" surfaced in search. **VERIFIED-via-search-snippet(https://www.quantconnect.com/docs/v2/local-platform/optimization/getting-started)** — details not read (403).
- Nothing found suggesting exhaustive combinatorial sweeps at BruteX's ~33B-trial scale are a
  supported product motion; cloud optimization is per-backtest-priced parallel grid/genetic search.
  **UNVERIFIED-general-knowledge** (characterization).

### 4. Statistical rigor / overfitting controls
- **Research Guide** (launched 2019): displays a "power gauge" of number of backtests performed,
  number of parameters, and time invested — a ballpark **overfitting-risk estimate**, plus guidance
  to keep parameters under ~10. **VERIFIED-via-search-snippet(https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/research-guide)**
- Deflated Sharpe Ratio: discussed on QC forum since 2015 ("may turn into a great metric when we
  launch parameter optimization") — i.e. awareness, but **no evidence found this session that DSR,
  CPCV, SPA, or PBO ship as built-in engine gates**.
  **VERIFIED-via-search-snippet(https://www.quantconnect.com/forum/discussion/565/)**; the
  shipped-vs-not distinction beyond the Research Guide is **UNVERIFIED** (docs 403 — could not
  enumerate the full shipped metric set; treat "no shipped CPCV/SPA/PBO" as best-effort finding,
  not proven absence).
- QC announcement "Making Models that Fit the Signal, Not the Noise" exists (research/education on
  overfitting): https://www.quantconnect.com/announcements/15502/ — content not read. **UNVERIFIED** details.

### 5. Data handling
- Curated cloud data library ("Datasets market") across equities/options/futures/forex/crypto/alt data;
  landing page https://www.quantconnect.com/datasets/ (not read — 403). **UNVERIFIED** details of cost per dataset.
- **India**: "India Equities" dataset by **TrueData** — "2,053 Indian Equities, starts January 2010,
  minute to daily frequency", depends on the India Equity Security Master (splits/dividends/symbol
  changes). CRITICAL NUANCE quoted from the docs snippet: "**Historical data for backtesting is
  unavailable**. To trade India Equities live, use one of the brokerage data providers" (Zerodha,
  Samco supported for live). I.e. NSE support is real for LIVE trading but the cloud backtest
  history for India appears NOT provisioned — a major honesty point vs "supports India".
  **VERIFIED-via-search-snippet(https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/truedata/india-equities
  + https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/india-equity
  + https://www.quantconnect.com/docs/v2/lean-cli/live-trading/brokerages/zerodha)** — direct fetch 403;
  the backtesting-unavailable quote deserves re-verification before print. NSE index-spot 1-minute
  history (BruteX's exact domain) not evidenced at all: **UNVERIFIED/likely absent**.
- Integrity guards: security master handles splits/dividends/symbol-changes (survivorship-safe US
  equity universe is a QC selling point). **UNVERIFIED-general-knowledge** beyond the India security
  master line above.

### 6. Fill realism / cost model
- "Reality Modeling" framework: per-security **fill models** (per order type, returning OrderEvent;
  default ImmediateFillModel for non-equity under DefaultBrokerageModel), **slippage models**
  (VolumeShareSlippageModel, ConstantSlippageModel), **fee models** (ConstantFeeModel + many
  brokerage-specific fee models incl. Interactive Brokers, Alpaca), **buying-power models** (order
  rejected pre-submit if it would breach brokerage margin), settlement + short-availability models;
  philosophy = customizable per security. This is the strongest stock fill/cost model suite of the
  three engines here. **VERIFIED-via-search-snippet(https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/key-concepts
  and child pages /trade-fills/supported-models/immediate-model, /slippage, /transaction-fees/supported-models,
  /buying-power)**
- Indian cost regimes (STT/ETC/stamp with effective-date history) — no evidence LEAN ships them;
  brokerage fee models are US/global-broker-centric. **UNVERIFIED/likely absent**.

### 7. Reproducibility
- Expectation is deterministic ("same inputs should produce same outputs") but users have reported
  same-code different-results episodes on the forum; QC attributed one to a since-fixed data issue;
  ML models with random init are a named nondeterminism source. **No content-addressed run identity
  (nothing like BruteX's blake3 run_id / data snapshot hash) found.**
  **VERIFIED-via-search-snippet(https://www.quantconnect.com/forum/discussion/8518/ +
  /forum/discussion/13009/)**; absence of content-addressing: **UNVERIFIED (absence claim, best-effort)**.

### 8. Crash resilience / automation
- Cloud: managed backtest/optimization/live deployment on QC's infrastructure; local: LEAN CLI +
  Docker images, local-cloud hybrid workflows. **VERIFIED(https://github.com/QuantConnect/Lean)**
  (README mentions CLI + local/cloud-hybrid) — depth of crash-resume for a multi-day sweep:
  **UNVERIFIED** (nothing like durable per-tuple checkpoint/quarantine documented in what was readable).

### 9. Multi-asset breadth
- Breadth leader claim holds: US/global equities, options, futures, forex, CFD, crypto; repo
  self-describes stocks/options/forex; datasets market spans more.
  **VERIFIED(https://github.com/QuantConnect/Lean)** + **VERIFIED-via-search-snippet(https://www.quantconnect.com/datasets/)**.
- India honestly: LIVE-only via TrueData/Zerodha/Samco; cloud backtest history for India equities
  flagged unavailable (see §5). NSE/BSE index-spot minute backtest data: **UNVERIFIED/absent**.

### 10. License / cost
- Engine: **Apache-2.0**, free, self-hostable ("you aren't locked in to the QuantConnect platform").
  **VERIFIED(https://github.com/QuantConnect/Lean)**.
- Cloud: third-party 2026 reviews put the Researcher plan at **$60/month** entry, "serious individual
  trader… could easily spend $200-300/month"; nodes $24–$1,000/mo (see §3). Official pricing page
  403'd — treat tier names/numbers as **VERIFIED-via-search-snippet(https://www.newtrading.io/quantconnect-review/
  + https://newyorkcityservers.com/blog/quantconnect-review)**, third-party, re-verify before print.

### 11. Maturity / community
- **20.6k stars, 5.1k forks, 13,269 commits on master, active CI** as of 2026-07-18.
  **VERIFIED(https://github.com/QuantConnect/Lean)**. "180+ contributors" claim appears in
  lean.io marketing — **VERIFIED-via-search-snippet(https://www.lean.io/)** (approximate).

### 12. Learning curve
- Steep: large API surface (reality models, universe selection, security master concepts), C# core
  with Python bindings, cloud-vs-local duality; widely described as the most institutional and most
  complex of the open engines. **UNVERIFIED-general-knowledge** (consistent with third-party reviews
  surfaced above).

---

## ENGINE 2 — Jesse

### 1. Architecture
- Python framework, **crypto-focused by design** ("built from the ground up for crypto"); strategies
  in "clean Pythonic syntax"; multi-symbol + multi-timeframe; spot + futures; 300+ built-in
  indicators; self-hosted GUI dashboard. **VERIFIED(https://github.com/jesse-ai/jesse)** (README) +
  **VERIFIED-via-search-snippet(https://jesse.trade/ , https://pypi.org/project/jesse/)**.
- Note: GitHub language stats show JavaScript 85.1% / Python 14.1% — the bundled web GUI dominates
  the line count; the engine itself is Python. **VERIFIED(https://github.com/jesse-ai/jesse)** (stat),
  interpretation UNVERIFIED-general-knowledge.

### 2. Raw speed
- **No published throughput/speed benchmark found** for Jesse this session (its README makes no
  speed claim). **UNVERIFIED** — no number exists to cite.

### 3. Scale ceiling / optimization
- "Optimize mode": tune any strategy hyperparameter with **Optuna** + "easy cross-validation";
  plus a "benchmark" feature for batch backtests across timeframes/symbols.
  **VERIFIED(https://github.com/jesse-ai/jesse)** (README) +
  **VERIFIED-via-search-snippet(https://jesse.trade/)**.
- Single-machine, search-based (Optuna TPE etc.) — nothing found about distributed/exhaustive
  combinatorial sweeps. **UNVERIFIED-general-knowledge** (characterization).

### 4. Statistical rigor / overfitting controls
- README lists **Monte Carlo analysis** (trade shuffling + candle-based simulations for robustness)
  and Optuna cross-validation. **VERIFIED(https://github.com/jesse-ai/jesse)**.
- No DSR / CPCV / SPA / PBO found anywhere in README/marketing. **UNVERIFIED (absence claim, best-effort)**.

### 5. Data handling
- "Import candles" mode pulls historical candles from crypto exchanges into a local database
  (free/open-source mode). **VERIFIED-via-search-snippet(https://jesse.trade/help/faq/why-do-i-have-to-pay-for-live-i-thought-its-open-source)**
  (which lists import/backtest/optimize as the free modes). Storage backend (PostgreSQL) and any
  integrity guards: **UNVERIFIED** this session.
- **No Indian market data path at all** — crypto exchanges only. **UNVERIFIED-general-knowledge**
  (consistent with all sources; no NSE/BSE mention anywhere).

### 6. Fill realism / cost model
- Claims: "realistic backtesting that accounts for exchange fees, slippage, and market impact"
  (marketing wording via StratBase/CryptoAITools reviews) and README lists **partial-fill support**.
  **VERIFIED(https://github.com/jesse-ai/jesse)** for partial fills;
  fee/slippage specifics **VERIFIED-via-search-snippet(https://stratbase.ai/en/tools/jesse)** at
  marketing level only — model internals UNVERIFIED.

### 7. Reproducibility
- README claims backtesting "without look-ahead bias" (a correctness claim, not a reproducibility
  mechanism). **VERIFIED(https://github.com/jesse-ai/jesse)**. Deterministic run identity /
  content-addressing: nothing found — **UNVERIFIED (absence, best-effort)**.

### 8. Crash resilience / automation
- Live mode: real-time monitoring, multiple accounts, Telegram/Slack/Discord notifications; self-hosted.
  **VERIFIED(https://github.com/jesse-ai/jesse)**. Backtest crash-resume/checkpointing: nothing found —
  **UNVERIFIED (absence, best-effort)**.

### 9. Multi-asset breadth
- **Crypto only** (spot + futures/perps, multiple exchanges). No equities, no options, no India.
  **VERIFIED(https://github.com/jesse-ai/jesse)** (self-description "crypto trading bot").

### 10. License / cost
- Core (import/backtest/optimize): **free, open source** — repo license shows **MIT**
  (**VERIFIED(https://github.com/jesse-ai/jesse)**); note some marketing pages say MIT too.
- **Live Trade plugin is PAID**; Jesse announced a transition from lifetime licenses to a
  **subscription model**; tier prices surfaced in search: $899 (1 IP / 1 route), $999 (2 IPs / 10
  routes), $1,599 (5 IPs / 50 routes) — page fetch 403'd, and whether these are lifetime or annual
  is not clear from the snippet. **VERIFIED-via-search-snippet(https://jesse.trade/pricing +
  https://jesse.trade/blog/news/upcoming-new-pricing-model-and-a-special-discount-for-the-current-lifetime-plan)**
  — RE-VERIFY exact current prices/period before print. A free trial version of the live plugin
  exists. **VERIFIED-via-search-snippet(https://jesse.trade/help/faq/is-there-a-trial-version-of-the-live-trade-plugin-available)**

### 11. Maturity / community
- **8.2k stars, 1.2k forks, 3,390 commits** as of 2026-07-18. **VERIFIED(https://github.com/jesse-ai/jesse)**.
- JesseGPT AI assistant + YouTube channel + docs (educational focus). **VERIFIED(https://github.com/jesse-ai/jesse)**.

### 12. Learning curve
- Gentle for Python devs — one strategy class, GUI dashboard, opinionated crypto workflow; the
  easiest of the three to first-backtest. **UNVERIFIED-general-knowledge**.

---

## ENGINE 3 — Homegrown pandas/numpy backtester (the "roll your own" baseline)

All characterizations **UNVERIFIED-general-knowledge** unless cited — this is a class of software,
not a product.

- **Architecture**: typically a vectorized pandas/numpy script or small class: signal = array ops
  (shift/rolling/mask), equity curve = cumprod over returns. Fast to write (a day), zero dependency
  on any vendor.
- **Raw speed**: vectorized numpy is genuinely fast on small sweeps — the same architecture class as
  vectorbt, which third-party comparisons crown fastest for parameter grids
  (**VERIFIED-via-search-snippet(https://python.financial/ , https://hasanjaved.me/blog/best-python-backtesting-libraries-2026/)**
  — "VectorBT runs simulations faster than other libraries… array-based architecture and Numba").
  But homegrown code rarely gets the Numba/caching engineering, and per-bar event realism is absent.
- **Scale ceiling**: whatever one machine + one afternoon of multiprocessing gives; no distributed
  story, no memory budgeting, no dedup of repeated combos.
- **Statistical rigor**: essentially never ships DSR/CPCV/SPA/PBO; the researcher IS the multiple-
  comparisons problem. Published bias literature quantifies the damage: survivorship bias ≈ **0.9%/yr
  in mutual fund returns (Elton, Gruber & Blake 1996)** and **2–4%/yr for hedge funds (Rohleder,
  Scholz & Wilkens 2011)** — **VERIFIED-via-search-snippet(https://analystprep.com/study-notes/cfa-level-2/problems-in-backtesting/)**
  (secondary source quoting the papers).
- **Look-ahead bugs endemic**: the classic failure of hand-rolled vectorized backtests — using
  same-bar close for signals filled at same-bar open, unshifted rolling windows, end-of-day data in
  intraday decisions. Public writeups:
  - "Backtesting Biases and How To Avoid Them" (Auquan, Medium) —
    https://medium.com/auquan/backtesting-biases-and-how-to-avoid-them-776180378335 **VERIFIED (URL surfaced; content per snippet)**
  - "A Taxonomy of Backtest Lies: Survival Bias, Lookahead Bias…" (Susan Potter) —
    https://www.susanpotter.net/quant/backtest-bias-taxonomy/ **VERIFIED (URL surfaced)**
  - "How To Avoid Bias in Backtesting" — https://www.fortraders.com/blog/how-to-avoid-bias-in-backtesting
  - "The Seven Sins of Quantitative Investing" — https://portfoliooptimizationbook.com/book/8.2-seven-sins.html
  - Third-party framing (python.financial): event-driven engines are "slow but honest"; vectorized
    shortcuts are exactly where look-ahead hides. **VERIFIED-via-search-snippet(https://python.financial/)**
- **Data handling**: whatever CSV the researcher downloaded; no split/dividend security master, no
  calendar authority, no bad-tick screens, no completeness gates — survivorship + gap bugs by default.
- **Fill realism**: usually close-to-close or next-open fills at zero/flat cost; no per-security fee
  regimes, no partial fills, no volume-0 handling.
- **Reproducibility**: no run identity, no data snapshot hash; re-running after a data refresh
  silently changes results; notebook state leaks are common.
- **Crash resilience**: none — a multi-hour sweep that dies restarts from zero; no checkpoints,
  no quarantine, no audit trail.
- **Cost**: "free" up front; the hidden costs are engineer-months of debugging + the capital cost of
  deploying strategies validated by a biased harness (the bias literature above is the quantified
  version of that cost).
- **Learning curve**: trivially easy to start — that IS the trap.

---

## Direct published cross-engine comparisons captured
1. QuantRocket, "Why Backtests Run Fast or Slow: A Comparison of Zipline, Moonshot, and Lean" —
   https://www.quantrocket.com/blog/backtest-speed-comparison/ (code:
   https://github.com/quantrocket-codeload/backtest-speed-comparison). Headline: large universe —
   Lean 12x slower than Zipline, 75x slower than Moonshot; small universe — all under 30s.
   Competitor-authored; code public. **VERIFIED-via-search-snippet** (direct fetch 403 on both blog + Medium mirror).
2. python.financial, "The Python Backtesting Landscape (2026)" — https://python.financial/ —
   qualitative: vectorbt for sweeps, LEAN for institutional multi-asset cloud, Jesse/Freqtrade for
   crypto retail; backtrader in maintenance mode since 2023. **VERIFIED-via-search-snippet**.
3. Hasan Javed, "Best Python Backtesting Libraries in 2026" —
   https://hasanjaved.me/blog/best-python-backtesting-libraries-2026/ — qualitative speed ranking
   (vectorbt fastest). **VERIFIED-via-search-snippet**.
4. autotradelab, "Backtrader vs NautilusTrader vs VectorBT vs Zipline-reloaded" —
   https://autotradelab.com/blog/backtrader-vs-nautilusttrader-vs-vectorbt-vs-zipline-reloaded —
   not read; URL captured. **UNVERIFIED contents**.

## Cross-cutting notes for the coordinator (BruteX angles)
- LEAN is the only one of the three with any India story, and even that appears LIVE-only (TrueData
  minute+ feed; Zerodha/Samco brokerages) with the docs snippet stating historical backtesting data
  for India Equities is unavailable — i.e. NONE of the three can natively backtest NSE/BSE index
  spot 1-minute history; BruteX's Groww-fed lake is out of their box entirely. (Re-verify the India
  backtest-availability quote — fetch was 403.)
- None of the three ships DSR/CPCV/SPA/PBO as enforcement gates (LEAN: advisory Research Guide power
  gauge; Jesse: Monte Carlo shuffling; homegrown: nothing). Best-effort absence claims.
- None shows content-addressed run identity or hash-chained audit; LEAN determinism is expected-but-
  user-reported-flaky in forum threads; Jesse silent on the axis.
- Exhaustive-combinatorial scale (BruteX ~33B trials): LEAN = paid parallel cloud grid; Jesse =
  single-box Optuna search; homegrown = whatever fits an afternoon. No engine advertises exhaustive
  enumeration at that magnitude.
