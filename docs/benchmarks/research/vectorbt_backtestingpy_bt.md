# Research notes — vectorbt (+PRO), backtesting.py, bt
Date: 2026-07-18. Worker: research_vectorbt_family.
Method note: non-GitHub/PyPI sites (vectorbt.dev, vectorbt.pro, kernc.github.io, pmorissette.github.io, medium/blog benchmark sites, pyquantnews) returned HTTP 403 through the agent proxy — those claims were captured via WebSearch result snippets (labeled "VERIFIED-SNIPPET" = search engine surfaced the text attributed to that URL, page body not independently fetched) or via raw.githubusercontent.com / github.com / pypi.org fetches (fully VERIFIED).

---

## ENGINE 1: vectorbt (open-source) + vectorbt PRO

### 1. Architecture
- VERIFIED(https://github.com/polakowo/vectorbt): vectorized backtesting on "pandas, NumPy, and Numba", with an optional "Rust engine for precompiled speed without JIT overhead" (repo 95.6% Python / 4.2% Rust as of the 2026 v1.x line). "Packs thousands of configurations into NumPy arrays."
- VERIFIED(https://pypi.org/project/vectorbt/0.21.0/): the mature 0.x line "operates through pandas and NumPy objects, and is accelerated by Numba".
- Column-wise broadcasting: each parameter combo becomes a column of a wide 2D array; simulation is a Numba-compiled columnar loop. (Consistent with all sources; mechanism description UNVERIFIED as an exact quote.)

### 2. Raw speed
- VERIFIED(https://github.com/polakowo/vectorbt/discussions/209): maintainer polakowo, 2021-08-06: "vectorbt runs ~1000x faster than backtrader and yields the same results" (pairs-trading example, no parallelization). Same thread: TA-Lib indicator on 1e6 rows 27.2 ms vs vectorbt 48 ms — indicator calc alone is NOT faster; the win is batched backtest simulation.
- VERIFIED(https://pypi.org/project/vectorbt/0.21.0/): micro-benchmarks vs pandas — rolling apply 7.32 s (pandas) vs 86.2 ms (vectorbt); expanding 48.4 ms vs 8.82 ms; add 242 ms vs 3.32 ms.
- VERIFIED(https://github.com/polakowo/vectorbt): current README marketing "Run thousands of trading ideas before others finish one", "turns hours of grid search into seconds"; examples of "10,000 dual-SMA window combinations" and "1,000 random strategies". No absolute throughput number in README.
- UNVERIFIED (title-only; page 403): PyQuant News article titled "1,000,000 backtest simulations in 20 seconds with vectorbt" (https://www.pyquantnews.com/the-pyquant-newsletter/1000000-backtest-simulations-20-seconds-vectorbt).
- UNVERIFIED (search-summary attribution unclear, secondary blogs): "processes and fills a million simulated orders in ~70–100 ms on an Apple M1"; "VectorBT Pro 167x faster than Backtrader on 100M tick datasets" (appears in pineify.app / autotradelab.com blog ecosystem — low-quality, pages 403'd; do NOT use as a headline number).

### 3. Scale ceiling
- VERIFIED(https://github.com/polakowo/vectorbt/issues/406): real user report — two-EMA optimization, BTC-USDT 1h, ~39k bars × ~40,000 parameter combos → 24 GB peak RAM, 1–2 min; user extrapolates a 4-parameter sweep on 3-minute data to ~960 GB RAM ("most computers will not be able to process that"); a C++ loop equivalent used ~50 MB. No maintainer rebuttal captured on the page.
- VERIFIED-SNIPPET(https://vectorbt.pro/features/performance/ + https://github.com/polakowo/vectorbt/discussions/464): PRO's answer is a CHUNKING mechanism — "specify how arguments should be chunked... splits array-like arguments, passes each chunk... merges the results", e.g. "at most 1000 parameter combinations at a time" to avoid OOM.
- Net: practical open-source ceiling is RAM-bound at roughly 10^4–10^5 combos on minute-scale multi-year data on a 32–64 GB box (derived from #406 arithmetic — UNVERIFIED as a general law, VERIFIED anchor point above). Nothing near 10^9+ combos is published anywhere for vectorbt. No published example found beyond ~10^4–10^6 combos (the README's 10,000-combo demo; the 1M-sims article title).

### 4. Statistical rigor / overfitting controls
- **Deflated Sharpe Ratio: YES in open-source vectorbt** — VERIFIED(https://raw.githubusercontent.com/polakowo/vectorbt/master/vectorbt/returns/metrics.py): `def deflated_sharpe_ratio(*, est_sharpe, var_sharpe, nb_trials, backtest_horizon, skew, kurtosis)` — "Deflated Sharpe Ratio (DSR)" with a reference link. (Corrects the prior expectation of NO. It's a metrics function the user must feed `nb_trials` etc.; not an automatic sweep-wide gate.)
- **CPCV / purged CV: vectorbt PRO YES** — VERIFIED-SNIPPET(https://vectorbt.pro/tutorials/cross-validation/ + https://vectorbt.pro/features/optimization/): "walk-forward cross-validation with purging, as well as combinatorial CV with purging and embargoing, based on Marcos Lopez de Prado's Advances in Financial Machine Learning"; purged k-fold splitter with configurable purge/embargo windows; split analytics; "PBO-style reasoning" workflows (PBO itself appears as a workflow pattern, not clearly a one-call shipped metric — UNVERIFIED whether a `pbo()` API exists).
- Open-source vectorbt: walk-forward/robustness testing listed on PyPI v1.1.0 description ("Walk-forward optimization and robustness testing") — VERIFIED(https://pypi.org/project/vectorbt/); purged/combinatorial CV NOT found in open-source feature lists (VERIFIED-absent from README/PyPI; hard negative not provable).
- **SPA / White's Reality Check: NO evidence in either edition** (searched; only academic papers surfaced) — VERIFIED-absent-from-search.

### 5. Data handling
- VERIFIED(https://pypi.org/project/vectorbt/0.21.0/): 0.x data APIs for "ccxt, yfinance and python-binance" + custom data classes; periodic "fetch and process data periodically" updater.
- VERIFIED(https://github.com/polakowo/vectorbt): v1.x README shows `vbt.YFData.download()` (Yahoo Finance), TA-Lib/Pandas-TA integrations, synthetic data generation.
- vectorbt PRO advertises many more integrations (Alpaca, Polygon, databases, etc.) — UNVERIFIED (pages 403'd).
- Data-integrity guards: NONE found — it consumes whatever DataFrame/array you give it; no corrupt-tick screen, no calendar/session validation, no NaN quarantine (NaN handling is "skip/propagate" semantics in Numba loops). VERIFIED-absent from README/API surfaces examined; hard negative not provable.
- Look-ahead: vectorized signal generation makes look-ahead bugs the USER's responsibility (shift-your-signals discipline); no runtime look-ahead barrier found. UNVERIFIED-absent (no guard found in any doc surface examined).

### 6. Fill realism / cost model
- Portfolio.from_signals/from_orders support fees (percent + fixed), slippage (percent), size granularity, price selection (close vs custom), stop-loss/take-profit/trailing in PRO; open-source has fees/fixed_fees/slippage params. UNVERIFIED as exact quotes (API docs 403'd) but consistent across IBKR Campus guide + README examples; treat as high-confidence UNVERIFIED.
- Default fill = same-bar close of the signal bar unless the user shifts signals or sets price to next open — no built-in worst-case (adverse-extreme) fill model anywhere. UNVERIFIED-absent.

### 7. Reproducibility
- No content-hashed run identity, no run manifests, no bit-exact re-run contract found in any vectorbt/PRO doc surface. Numba-based determinism is per-process/per-version; caching exists (PRO) but is a performance feature. VERIFIED-absent from all fetched surfaces (hard negative not provable).

### 8. Crash resilience / automation
- VERIFIED(https://pypi.org/project/vectorbt/0.21.0/): 0.x shipped a "Telegram bot based on python-telegram-bot" + scheduled data-update workflows ("fetch and process data periodically, send Telegram notifications") — i.e., some automation plumbing exists.
- No checkpoint/resume of interrupted sweeps, no rate-limit governor for data vendors, no crash-recovery journal found. VERIFIED-absent from examined surfaces.

### 9. Multi-asset breadth
- Any instrument you can put in a DataFrame: crypto (ccxt/Binance), equities/ETFs (Yahoo), FX; multi-asset columnar portfolios with flexible broadcasting. VERIFIED(https://github.com/polakowo/vectorbt + PyPI pages). No exchange-calendar awareness for NSE/BSE-style sessions found (UNVERIFIED-absent).

### 10. License / cost
- Open source: **Fair-code — Apache 2.0 with Commons Clause** (free for use, cannot be sold as a primary product; PyPI 0.21.0 phrased it "Free for non-commercial use"). VERIFIED(https://github.com/polakowo/vectorbt, https://pypi.org/project/vectorbt/0.21.0/).
- vectorbt PRO: **$25/month; Annual Saver $20/mo; lifetime conversion by applying half of paid monthly amounts as credit, minimum $150** — VERIFIED-SNIPPET(https://vectorbt.pro/become-a-member/ + https://ko-fi.com/s/88d8ca176c). PRO is closed/private-repo access via subscription.

### 11. Maturity / community
- VERIFIED(https://github.com/polakowo/vectorbt): 8.4k stars, 1.1k forks; latest release v1.1.0 on 2026-07-05 (the 2026 v1.x line lists only 4 releases — a rewrite/reset of the release series; 0.x history lives on PyPI). Python 3.11–3.14 (PyPI).
- Single-maintainer-dominated project (polakowo) — UNVERIFIED (widely known, not re-verified this session).

### 12. Learning curve
- Steep: NumPy/Numba mental model, broadcasting semantics, and shift-discipline for look-ahead; power users only. (Consensus across comparison articles; UNVERIFIED one-liner.)

---

## ENGINE 2: backtesting.py (kernc)

### 1. Architecture
- Event-ish single-strategy simulator over one OHLCV DataFrame: `Strategy.next()` called bar-by-bar in pure Python with a vectorized indicator layer (`self.I`). Marketing says "Blazing fast execution"; repo is 99.4% Python. VERIFIED(https://github.com/kernc/backtesting.py, https://pypi.org/project/backtesting/). (Bar-loop mechanism from the source file fetched — VERIFIED(https://raw.githubusercontent.com/kernc/backtesting.py/master/backtesting/backtesting.py).)

### 2. Raw speed
- NO published absolute benchmark number found — only the "Blazing fast execution" feature bullet. VERIFIED-absent(https://github.com/kernc/backtesting.py). Being a Python bar loop, it is orders of magnitude slower than vectorbt per combo (UNVERIFIED comparison; no direct published head-to-head found this session).

### 3. Scale ceiling
- Single instrument, single strategy per Backtest; `optimize()` does exhaustive/randomized **grid** search (multiprocess) or **"sambo"** model-based optimization; `max_tries` caps evaluations (int or fraction of grid). VERIFIED(raw backtesting.py source). Realistic sweeps: 10^2–10^5 combos (each combo is a full Python bar-loop run) — UNVERIFIED estimate; no published large-sweep example found.

### 4. Statistical rigor / overfitting controls
- NONE: no DSR, no CPCV/purged CV, no SPA/White's RC, no PBO. Optimizer offers `constraint=` filtering and randomized grid subsampling; stats output is classic (Sharpe/Sortino/SQN/etc.). VERIFIED-absent from the full class source + PyPI feature list (hard negative not provable, but the API surface was read).

### 5. Data handling
- User-supplied OHLCV DataFrame; it DOES validate: raises on missing OHLC columns and "Some OHLC values are missing (NaN)"; warns on unsorted index, non-datetime index, and prices exceeding initial cash (no fractional trading). VERIFIED(https://raw.githubusercontent.com/kernc/backtesting.py/master/backtesting/backtesting.py). No vendor integrations (data acquisition is out of scope). "Supports any financial instrument with candlestick data" — VERIFIED(https://pypi.org/project/backtesting/).
- Look-ahead: the framework reveals data to `next()` progressively (structural protection for strategy logic); indicators via `self.I` are computed once but served truncated. VERIFIED mechanism from source (progressive reveal); no explicit "look-ahead barrier" doc claim quoted.

### 6. Fill realism / cost model
- VERIFIED(raw source docstrings): commission as percent / (fixed, relative) tuple / callable `func(order_size, price)`, "applied twice: at trade entry and at trade exit"; bid-ask `spread` param; `margin` for leverage; **market orders fill at NEXT bar's open** unless `trade_on_close=True` (then current bar close); limit/stop orders fill when conditions met; `hedging`, `exclusive_orders`, `finalize_trades` semantics as quoted. No adverse/worst-case fill model; intra-bar order of touching High vs Low is assumption-based (standard OHLC ambiguity — UNVERIFIED detail).

### 7. Reproducibility
- No run-identity hashing/content-addressed runs; randomized optimizer takes `random_state` for seeding. VERIFIED-absent (source read).

### 8. Crash resilience / automation
- None: no checkpoint/resume, no scheduling, no alerting; it's a library call. VERIFIED-absent (source + docs surfaces read).

### 9. Multi-asset breadth
- ONE instrument per backtest (no native multi-asset portfolio). Any candlestick instrument (stocks, forex, crypto, futures). VERIFIED(https://pypi.org/project/backtesting/) + source structure.

### 10. License / cost
- **AGPL-3.0+**, free. VERIFIED(https://github.com/kernc/backtesting.py, PyPI). (AGPL is itself a commercial-integration constraint worth flagging.)

### 11. Maturity / community
- VERIFIED(https://github.com/kernc/backtesting.py): 8.7k stars, 1.5k forks, 434 commits, 59 open issues. Latest PyPI release **0.6.5, 2025-07-30** — VERIFIED(https://pypi.org/project/backtesting/). Maintained but slow-cadence.

### 12. Learning curve
- Easiest of the three — small, well-documented API; a beginner ships a strategy in an afternoon. (Consensus; UNVERIFIED one-liner.)

---

## ENGINE 3: bt (pmorissette)

### 1. Architecture
- Tree-structured portfolio framework on pandas + ffn: strategies are trees of nodes; logic is composable "Algos"/"AlgoStacks"; the engine loops over dates calling `strategy.update(dt)` → `strategy.run()` (a synthetic t0−1 row is prepended; halts on bankruptcy). VERIFIED(https://raw.githubusercontent.com/pmorissette/bt/master/README.md + https://raw.githubusercontent.com/pmorissette/bt/master/bt/backtest.py).

### 2. Raw speed
- No benchmark numbers anywhere; the project itself states: "Due to the flexible nature of bt, a trade-off had to be made between usability and performance. Usability will always be the priority." VERIFIED(https://github.com/pmorissette/bt README/docs). Slowest class of the three for sweeps (UNVERIFIED comparative).

### 3. Scale ceiling
- No built-in parameter-sweep machinery at all — you hand-roll loops over Backtest objects and `bt.run(...)` them. Designed for comparing a handful of allocation strategies, not 10^4+ combos. VERIFIED-absent (no optimizer in README/docs/backtest.py). Daily-rebalance asset-allocation scale (dozens of assets × years) is the intended envelope.

### 4. Statistical rigor / overfitting controls
- NONE — stats come from ffn (Sharpe, drawdown, monthly tables, etc.); no DSR/CPCV/SPA/PBO anywhere. VERIFIED-absent from README + docs + backtest.py source (hard negative not provable).

### 5. Data handling
- User-supplied pandas price DataFrames (`bt.get` helper wraps ffn's Yahoo download); no integrity guards found beyond the synthetic baseline row; trusts the user's data. VERIFIED-absent from source read.

### 6. Fill realism / cost model
- VERIFIED(https://raw.githubusercontent.com/pmorissette/bt/master/bt/backtest.py): `commissions=` accepts a callable `fn(quantity, price) -> float` or a `bt.core.CostModel` instance; `integer_positions` (with documented rounding-to-zero pitfall); `initial_capital` default 1,000,000. **No slippage model documented.** Fills at the rebalance price of the bar being processed (close-based weights) — no next-bar/worst-case semantics (UNVERIFIED detail beyond absence of slippage).

### 7. Reproducibility
- No run-identity/hashing mechanism. VERIFIED-absent (source read).

### 8. Crash resilience / automation
- None (progress_bar flag is the extent of it). VERIFIED-absent.

### 9. Multi-asset breadth
- Its strength: multi-asset portfolios / asset allocation across anything priced in a DataFrame (equities, ETFs, cross-asset), tree sub-portfolios with own price indices, fixed-income extensions in docs. VERIFIED(README/docs).

### 10. License / cost
- **MIT**, free. VERIFIED(https://github.com/pmorissette/bt).

### 11. Maturity / community
- VERIFIED(https://github.com/pmorissette/bt): 2.9k stars, 492 forks; latest release **v1.2.0, 2026-04-25**. Maintained at a slow, stable cadence (same author as ffn).

### 12. Learning curve
- Moderate: the Algo/tree mental model is clean for allocation strategies but wrong-shaped for intraday signal research. (UNVERIFIED one-liner.)

---

## DIRECT PUBLISHED COMPARISONS captured
1. **vectorbt ~1000x backtrader** (pairs trading, same results, no parallelization) — maintainer statement. VERIFIED(https://github.com/polakowo/vectorbt/discussions/209).
2. TA-Lib vs vectorbt indicator micro-bench (27.2 ms vs 48 ms @1e6 rows — vectorbt SLOWER for bare indicators) — same thread, VERIFIED.
3. "Vectorbt vs Backtrader: Speed Benchmark for Mean Reversion" (trader-algoritmico.com) — exists, numbers NOT retrieved (403). UNVERIFIED numbers.
4. "Vectorbt vs Backtrader" (greyhoundanalytics.com/blog/vectorbt-vs-backtrader/) — exists, 403. UNVERIFIED numbers.
5. "Battle-Tested Backtesters: Comparing VectorBT, Zipline, and Backtrader" (Medium, trading.dude) — qualitative. Not fetched.
6. "VectorBT Pro 167x faster than Backtrader on 100M ticks" — appears only in low-quality SEO blogs (pineify.app/autotradelab.com search summaries); UNVERIFIED, do not headline.
7. PyQuant News: "1,000,000 backtest simulations in 20 seconds with vectorbt" — title-level claim, page 403. UNVERIFIED details.
8. vectorbt memory reality-check: GitHub issue #406 (24 GB @40k combos; 960 GB extrapolation; ~50 MB C++ equivalent) — VERIFIED(https://github.com/polakowo/vectorbt/issues/406). Useful as the head-to-head scale-ceiling contrast vs BruteX's O(1)-per-mask streaming design.

## Notes for coordinator (BruteX contrast hooks — from repo context, not web)
- BruteX-side facts (from CLAUDE.md, internal): ~33B trials in 2–4 days on M4 Pro; DSR/CPCV/SPA-class Stage-3/4 cascade; blake3 run identity incl. full-content data snapshot hash; bad-tick screen + price-sanity/finiteness/ts-range gates at read boundaries; worst-case-only fill model (DEC-FILL-WORST-CASE-ONLY-001); checkpoint/resume + quarantine + nightly zero-touch chain; rate-governed vendor pulls. None of the three researched engines has ANY of: content-hashed run identity, worst-case fills, data-integrity read gates (except backtesting.py's NaN/sort checks), crash-resume sweeps, or (except vectorbt PRO's purged/CPCV splitters and vectorbt's DSR function) sweep-wide overfitting statistics.
