# BruteX vs the field — backtesting-engine head-to-head (2026-07-18)

**Evidence rules.** `MEASURED` = executed on this repo at main commit `3ee742b3` on a disclosed 4-core Intel Xeon 2.80GHz cloud container (15 GiB RAM) — **not** the M4-Pro 14-core/48GB reference envelope; raw logs live beside this file (`MEASURED_2026-07-18.md`, `rawlogs/`). `V(url)` = cited public source. `V-snip(url)` = verified via search snippet of the named page (site 403'd through our proxy; re-verify before external publication). `UNV` = unverified — treated as unknown, never as fact.

## 1. BruteX — measured on this checkout (real-time proof)

| Quantity | Measured value | Producing test |
|---|---|---|
| Mask-AND canary | **701.3M ops/s**, flat (ratio 1.47×) | `test_throughput_canary.py` |
| 60-condition vocabulary evaluation | 382.5k evals/s (ratio 1.05×) | `test_throughput_canary.py` |
| Rust supermask kernel | **5.20B mask·bar ops/s** = 14.69× numpy, 0.192 ns/bar·mask | `test_supermask_kernel_bench.py` (940M-op workload) |
| Rust trade-sim | 12,441 combos/s @ 20k bars; **243× the Python oracle**; u128 within 7% of u64 | `test_trade_sim_throughput.py` |
| Full-sweep extrapolation | 78,798 trials/s → **33B trials ≈ 4.85 days on this 4-core box** | `test_full_sweep_extrapolation.py` |
| Dedup (Bloom / LMDB) | 641 ns probe, flat vs size; ~1.0 µs exact membership, flat | `test_bloom_o1.py`, `test_lmdb_membership_o1.py` |
| O(1) result lookup | 159.6 ns vs 161.2 ns at 100× depth (1.01×) | `test_results_store` O(1) gate |
| Bench suite census | **391 passed / 0 failed / 12 skipped** (11 M4-Pro-only absolute gates + 1 optional wheel), 15m15s | `pytest tests/bench -q` |
| Coverage claim check | ci-exact module gate reproduced at **100.00% line+branch** | ci.yml gate on `brutex/data/fno_symbol.py` |

Context: the documented M4-Pro SLA is 2–4 days for the ~33B-trial sweep (`docs/TRACK1_PERFORMANCE_BUDGET.md` — a documented claim, not re-run here). A 4-core cloud container independently extrapolating to 4.85 days **corroborates** that envelope; it does not reproduce it on reference hardware.

Overfitting controls are shipped code, not slideware: `brutex/metrics/{deflated_sharpe,cpcv,spa,pbo,cascade,monte_carlo,lockbox,wfo}.py` (verified paths), wired into the Stage-4 cascade verdicts surfaced on every result screen.

Honest self-disclosures: first-read lake paths are O(files) by design (`docs/O1_GUARANTEE_AUDIT.md`); the ADR-0020 Rust lake conversions are uniformity-not-speed ("expected payoff ≈ 0" banner); the 11 M4-Pro absolute gates and the strict 1.1× tier did **not** run here.

## 2. Raw speed & sweep scale, side by side

| Engine | Architecture | Best speed evidence | Realistic sweep ceiling |
|---|---|---|---|
| **BruteX** | Rust kernels + Python orchestration, streaming | 5.20B mask·bar/s kernel; 78.8k trials/s end-to-end — MEASURED | ~33B trials/run, RAM-flat streaming + O(1) dedup + checkpoint/resume |
| vectorbt (OSS) | vectorized numpy/numba | "~1000× faster than backtrader, same results" — maintainer, V(github.com/polakowo/vectorbt/discussions/209) | RAM-bound: 24 GB @ ~40k combos, 960 GB extrapolated for a 4-param sweep — V(vectorbt issues/406) |
| vectorbt PRO | + chunking, optional Rust engine | same class — V-snip(vectorbt.pro) | chunked, wall-clock-bound; no published sweep >~10^6 combos |
| NautilusTrader | event-driven, Rust core | no published throughput number — UNV-absent | per-run event replay; no mass-sweep mode |
| Backtrader | pure-Python event loop | ~1000× slower than vectorbt — V(same discussion) | `optstrategy` multiprocess; ~10^3–10^4 combos practical (inference) |
| Zipline-reloaded | Python event-driven + Pipeline | no citable number — UNV | no built-in optimizer; user loops |
| QuantConnect LEAN | C# event-driven | 12× slower than Zipline on a competitor-authored bench — V-snip(quantrocket.com/blog/backtest-speed-comparison/) | paid parallel cloud nodes ($24–$1,000/mo per node — V-snip) |
| Jesse | Python, crypto | no published number — UNV | single-machine Optuna |
| backtesting.py | Python bar loop | no published number ("blazing fast" marketing only) | multiprocess grid, ~10^2–10^5 combos |
| bt | pandas allocation trees | "usability will always be the priority" — V(README) | no sweep machinery at all |
| homegrown pandas | vectorized ad-hoc | fast on small sweeps (class property) | collapses on scale, discipline, and honesty |

## 3. Capability matrix

Legend: ✅ shipped/strong · ⚠️ partial/qualified · ❌ absent. Proof pointers in §1–§2 and the research notes beside this file.

| Engine | Speed | Scale | Overfit stats | Data integrity | Fill realism | Crash/rate resilience | Zero-click automation | Bit-exact repro | Breadth | Ease | Cost | Maturity/community |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **BruteX** | ✅ | ✅ | ✅ DSR+CPCV+SPA+PBO+cascade | ✅ | ✅ worst-case-only | ✅ | ✅ | ✅ blake3+goldens | ❌ 3 Indian indices by design | ❌ | ✅ self-hosted | ⚠️ private, zero community |
| vectorbt OSS | ✅ | ⚠️ RAM | ⚠️ DSR only | ❌ trusts your DataFrame | ⚠️ basic slippage | ❌ | ⚠️ | ❌ | ✅ | ⚠️ | ✅ fair-code | ✅ 8.4k★, active |
| vectorbt PRO | ✅ | ⚠️ chunked | ⚠️ + purged/CPCV-style CV | ❌ | ⚠️ | ❌ | ⚠️ | ❌ | ✅ | ⚠️ | ⚠️ $25/mo | ✅ |
| NautilusTrader | ⚠️ unpublished | ❌ no sweeps | ❌ | ⚠️ ns-deterministic, no quarantine | ✅ book-walk fills | ⚠️ live Redis state only | ⚠️ | ⚠️ deterministic, unhashed | ✅ | ❌ | ✅ LGPL | ✅ 24.8k★, very active |
| Backtrader | ❌ | ❌ | ❌ | ❌ | ⚠️ | ❌ | ❌ | ❌ | ⚠️ | ✅ | ✅ | ⚠️ dormant since 2023 |
| Zipline-reloaded | ⚠️ | ❌ | ❌ tear sheets only | ✅ point-in-time / ⚠️ bundle rot | ✅ volume-share slippage | ❌ | ⚠️ | ⚠️ | ⚠️ US-centric | ⚠️ | ✅ | ⚠️ one maintainer |
| LEAN | ⚠️ | ⚠️ paid nodes | ⚠️ advisory guide | ✅ curated data | ✅ strongest reality models | ✅ managed cloud | ✅ cloud | ⚠️ forum-reported nondeterminism | ✅ leader — ⚠️ India backtest data unavailable (live-only, V-snip) | ❌ | ⚠️ $60–300/mo realistic | ✅ 20.6k★ |
| Jesse | ⚠️ | ⚠️ Optuna | ⚠️ Monte Carlo only | ⚠️ | ⚠️ | ❌ | ⚠️ | ❌ | ❌ crypto only | ✅ | ⚠️ paid live plugin | ✅ 8.2k★ |
| backtesting.py | ⚠️ | ⚠️ | ❌ | ⚠️ validates NaN/OHLC input | ⚠️ next-bar-open default | ❌ | ❌ | ❌ | ⚠️ 1 instrument/run | ✅✅ | ⚠️ AGPL | ✅ 8.7k★ |
| bt | ❌ | ❌ | ❌ | ❌ | ⚠️ no slippage model | ❌ | ❌ | ❌ | ✅ allocation | ⚠️ | ✅ MIT | ⚠️ 2.9k★ |
| homegrown | ⚠️ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ⚠️ | ✅ (the trap) | ⚠️ hidden eng-months | ❌ |

## 4. The edge/permutation dimension — corrupt data, look-ahead, crash recovery

| Engine | Corrupt/bad data | Look-ahead protection | Crash recovery | Vendor rate limits |
|---|---|---|---|---|
| **BruteX** | bad-tick screen, price-sanity + non-finite OHLC rejection, ts-range panic guard, lake heal/quarantine (`data/bad_tick.py`, `price_sanity.py`, `lake_heal.py`) — MEASURED suite green | dual runtime barriers + suffix-independence property gates (`feed/lookahead.py`, `engine/sweep_lookahead.py`, P-06) | durable window ledger, sweep checkpoints, unkillable run core (`sweep_checkpoint.py`, `sweep_run_core.py`) | AIMD ladder + hard 500/min governor (`adapters/rate_common.py`) |
| vectorbt | ❌ trusts input | ❌ user shift-discipline | ❌ | ❌ |
| NautilusTrader | ⚠️ no documented quarantine | ⚠️ deterministic event model | ⚠️ live-state persistence only | ⚠️ adapter-level |
| Backtrader | ❌ | ❌ | ❌ | ❌ |
| Zipline-reloaded | ⚠️ bundle validation | ✅ point-in-time engine (its genuine strength) | ❌ | ❌ |
| LEAN | ✅ curated data pipeline | ⚠️ engine-managed clock | ✅ managed cloud restarts | ⚠️ vendor-side |
| Jesse / backtesting.py / bt / homegrown | ❌ (backtesting.py: input NaN validation only) | ⚠️ backtesting.py progressive reveal; others ❌ | ❌ | ❌ |

## 5. Winner verdicts (per use-case, evidence-grounded)

- **(a) Exhaustive brute-force strategy discovery — WINNER: BruteX.** It is the only engine in the field that even has the shape: measured streaming kernel throughput (5.20B mask·bar/s, 33B-trial extrapolation corroborated on commodity hardware), RAM-flat O(1) dedup, checkpoint/resume, and shipped overfitting gates on the results. The nearest alternative, vectorbt PRO, hits a documented 24 GB @ 40k-combo RAM wall (mitigated only by chunking), has no run identity, no resume, no SPA/PBO. Runner-up: vectorbt PRO.
- **(b) Single-strategy interactive research — WINNER: backtesting.py (simplicity) / vectorbt (power).** Honest: BruteX is wrong-shaped for this — it is grid-first, operator-grade machinery, not a notebook toy.
- **(c) Multi-asset / global breadth — WINNER: QuantConnect LEAN.** Equities/options/futures/forex/CFD/crypto across global markets with the strongest reality modeling. Caveat that matters here: its India story is live-only — the TrueData docs snippet says historical backtest data is unavailable (V-snip; re-verify).
- **(d) Production live trading — WINNER: NautilusTrader (self-hosted) or LEAN (managed).** BruteX has no live-execution surface **by design** (signal-only research; live trading is explicitly out of scope) — this is not a contest it enters.
- **Overall, for BruteX's actual purpose (exhaustive discovery on Indian index spot): BruteX wins**, because the job needs five properties at once — 33B-class streaming scale, statistical honesty (DSR/CPCV/SPA/PBO; the entire field ships at most DSR + PRO's CV), worst-case-only fills, bit-exact reproducibility (blake3 run identity + frozen goldens), and zero-click crash-resilient automation. No competitor ships more than one of the five.

**Where competitors genuinely win** (a credible verdict names the trade-offs): community and battle-testing (NautilusTrader 24.8k★, LEAN 20.6k★, vs a private zero-community repo); multi-asset and global-market breadth (LEAN, Nautilus, vectorbt); time-to-first-backtest (backtesting.py: an afternoon; BruteX: operator-grade internal machinery); live execution (Nautilus/LEAN; BruteX none by design); notebook interactivity and docs (vectorbt); hiring pool and third-party learning material (all of them).

## 6. Hostile self-review of this verdict

1. BruteX's numbers were measured on a 4-core cloud container — the 11 M4-Pro absolute gates and the strict 1.1× tier did **not** run; the 2–4-day SLA is corroborated by extrapolation, not reproduced.
2. The headline competitor speed numbers are not neutral: "~1000× backtrader" is vectorbt's own maintainer; "LEAN 12× slower than Zipline" is QuantRocket's competitor-authored bench. Both are cited as such, not laundered into facts.
3. BruteX has zero external users; its reliability evidence is its own CI (391/0 bench census, 100.00% coverage gate, frozen goldens) — strong, but not community battle-testing.
4. The overall verdict is deliberately purpose-scoped: BruteX cannot run a US-equity or crypto backtest at all. On any breadth-weighted scoring it loses to LEAN; it wins the race it was built for.
5. Several V-snip claims (LEAN India data, vectorbt PRO pricing/features, Jesse pricing) came through search snippets because the sites 403'd — re-verify before publishing externally.
