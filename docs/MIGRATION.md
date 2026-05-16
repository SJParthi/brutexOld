# BruteX — Decision Ledger

> **Status:** Locked 2026-05-15. Living ledger of design decisions.
> Every PR justifies changes against the numbered rows below.
>
> **Project scope (locked):** Dhan + Indian spot indexes (NIFTY 50,
> BANKNIFTY, SENSEX) + India VIX (reference-only) — signal-only
> research (Track 1). Track 2 (options) is the realism leg that
> translates Track 1 winners into priced option trades.
>
> An earlier v1.0 spec targeted a different broker + the F&O segment;
> it was retired on 2026-05-15 after a 4-agent hostile-review pass
> surfaced 17 blocker / high-severity issues (spot is not tradable so
> brokerage maths was theatre; the original SLA claim was 5-15x
> optimistic; the prior broker had no public spot-index intraday
> history; SEBI segment + securityId assumptions were wrong). The
> blocker-by-blocker table and per-file wipe inventory are in `git
> log` and the commit message of `2e86bab` (the pivot commit).
>
> **Active sources of truth:**
> - `docs/TRACK1_BACKTEST_SPEC.md` — Track 1 master spec
> - `docs/TRACK1_DATA_INGEST_DESIGN.md` — Track 1 pipeline
> - `docs/track2/TRACK2_OPTIONS_SPEC.md` — Track 2 master spec
> - `docs/track2/TRACK2_DATA_INGEST_DESIGN.md` — Track 2 pipeline
> - `CLAUDE.md` — project rules + Dhan facts table

---

## Wipe inventory (summary)

The 2026-05-15 pivot deleted the prior broker's adapter and tests:
8 adapter source files, 4 SDK docs, 10 test files (broker-named
units + integration tests). Full path-by-path breakdown is in
commit `2e86bab`.

**Total: 8 source files + 4 docs + 10 tests = 22 files deleted.**

---

## 3. Decision ledger (forward-looking)

The rest of this file is the locked decision ledger. Every PR must
justify changes against this list. New decisions are appended at the
bottom; old decisions are amended in-place with a dated note when
superseded.

### Source & scope

1. Data source: **Dhan API only** (single-source vendor lock for v1)
2. Universe: **NIFTY 50, BANKNIFTY, SENSEX spot + India VIX volatility index** (was: 8 F&O indices + ~215 stocks; **amended 2026-05-15** from 3 → 4 instruments per user scope expansion, see decisions #71-72)
3. Tradability: **NONE — signal-only research** (was: F&O execution)
4. Range: **Rolling `2021-01-01 → today`** (**amended 2026-05-15** from fixed 2021-05-15 → 2026-05-15 window per user scope expansion, see decision #75)
5. Range source: `POST /v2/charts/intraday` (1m, 90-day chunks) + `POST /v2/charts/historical` (daily)
6. Native timeframes: 1/5/15/25/60 min + 1d (was: incorrectly claimed 21)
7. Resampled timeframes: **19 derived intraday TFs from 1m base** (**amended 2026-05-15** from 14 derived per user scope expansion to 21 total, see decisions #73-74)
8. Money unit: int64 paisa throughout
9. Exchange segment: `IDX_I` for ALL 4 Track 1 instruments (single Index segment per Dhan annexure verified 2026-05-15; there is no separate `BSE_I`)
10. securityId: never hardcoded; resolved from `api-scrip-master-detailed.csv` at startup

### Indicators (9)

11. EMA 20, EMA 200, Day High, Day Low, PDH, PDL, Daily Pivots (P, R1-R3, S1-S3), Static Daily Fib (12 levels), Candle metadata
12. VWAP dropped (no volume on spot)
13. Day high definition: `day_high[t] = max(day_high[t-1], high[t])` — no resample leak
14. PDH/PDL anchor: previous TRADING day (skips holidays)
15. Fib anchor: yesterday's H→L; extensions in trade direction

### Vocabulary

16. 52 atomic conditions, organised into 9 groups
17. K ∈ {2, 3, 4, 5} ANDed per strategy
18. AND only — no OR / mixed boolean
19. ~2.9M raw patterns → ~1.2-1.5M after conflict-graph pruning
20. Direction parameter (bull/bear) is separate from the 52 bits
21. Tolerances in basis points (bps), NOT absolute points

### Exit modes

22. Mode A (SL/TP-based): ~2.76M params per pattern
23. Mode B (signal-exit): ~500K params per pattern
24. Both modes brute-forced in parallel
25. 10-condition exit vocabulary for Mode B
26. Entry: bar OPEN of (signal_bar + 1)
27. Always candle CLOSE for signal; never bar touch
28. Forced 15:20 IST exit always active

### PnL & cost model

29. paper_pnl_paisa only (single column)
30. Brokerage, STT, GST, stamp duty: REMOVED (spot doesn't trade)
31. Slippage: ZERO
32. Position size: % of notional capital (Phase 2 will lot-round for options)

### Search pipeline

33. 5-stage smart-staged: Stage 1A → 1B → 2 → 3 → 4
34. Stage 1A: pattern discovery on NIFTY 15m only (~90 sec wall)
35. Stage 1B: expand top 50K patterns to all 45 inst-TF (~40 sec wall)
36. Stage 2: full param brute force on top 10K combos (~33B trials)
37. Stage 3: Bayesian CMA-ES refine top 100 (~15-20 min)
38. Stage 4: 4-filter cascade (DSR/CPCV/WFO/Lockbox) → final 3

### Statistical filters

39. DSR with effective-N (Bailey-LdP correlation clustering)
40. CPCV (Combinatorial Purged Cross-Validation)
41. WFO (Walk-Forward Optimization)
42. Lockbox: Dec 2025 - May 2026 held-out window

### Throughput & SLA

43. Target sustained throughput: 80-150K trials/sec on 10-core M4 Pro
44. Sweep wall time: **2-4 days on M4 Pro** (was: 10-13 hours, 5-15x optimistic)
45. CPU budget: 10 P-cores used; 4 E-cores reserved for OS + daily apps (14-core M4 Pro)
46. Memory tiling required: by (instrument, TF, day-window) bucket

### Dedup & audit

47. SHA256+Bloom+LMDB dedup
48. Timeframe IS part of the hash (different TF = different combo)
49. Bloom filter: ~5 GB sized for **survivors-only** (~150M-1B post-threshold-filter combos), giving FPR ≈ 1e-12 to 0. NOT sized for raw 33B trials (which would give FPR ≈ 56%). Math sanity-checked 2026-05-15 evening (decision #98)
50. LMDB: stores only result survivors, not every trial
51. Hash-chained JSONL audit with selective material-event logging
52. Audit replay test = the reproducibility oracle

### Storage

53. Result Parquet: ~50-100 GB per sweep (threshold-filtered)
54. failures.parquet: ~500 MB for audit completeness
55. ~80-160 sweeps fit in 8 TB SSD

### Verification (the 99.9% assurance path)

56. 9-layer test pyramid (Unit/Property/Golden/Integration/Bench/Stress/Smoke/Fuzz/Mutation)
57. 24 property invariants (hypothesis + proptest, 500 examples each)
58. 3 oracle tests: audit-log replay, Python-Rust parity, 52-condition spec table
59. O(1) ratio bench gate: `p99(N=1M) / p99(N=1K) ≤ 1.1`
60. Mutation testing: mutmut + cargo-mutants ≥ 85%
61. Coverage gate: 100% line + 100% branch (Python); 100% line + 95% branch (Rust)

### Indian microstructure

62. Pre-open 09:00-09:15 bars dropped at ingest
63. Muhurat dates excluded by hardcoded list (Samvat 2078-2083)
64. NIFTY 50 / SENSEX reconstitution dates flagged in audit
65. Circuit breaker halts: drop full day
66. Half-day sessions (Ram Navami etc.) honoured by calendar

### Engineering

67. Top-level package strategy: fresh `brutex/v2/` subpackage in Phase 2 (Track 1 lives in subpackage; old `brutex/*` modules deprecate progressively)
68. Rust hot kernel: extends existing `rust/fno-math/`
69. 8-12 week build timeline (Phase 0-4)
70. 4-agent hostile-review pattern at each phase end

### Late-day additions (2026-05-15, post user scope expansion)

71. **4 instruments**: NIFTY 50, BANKNIFTY, SENSEX, **India VIX** (added VIX)
72. India VIX integration: same code path as other 3 indices (segment `IDX_I`, `instrument=INDEX`, securityId resolved at runtime from master CSV; symbol `INDIA VIX` with space)
73. **21 TFs total**: 1m base (fetched) + 19 derived intraday (resampled) + 1d (fetched daily)
74. Derived TFs: `2m, 3m, 4m, 5m, 6m, 7m, 8m, 9m, 10m, 11m, 12m, 13m, 14m, 15m, 30m, 60m, 120m, 180m, 240m` (14 minute-increments + 5 longer)
75. Date range: **rolling `2021-01-01 → today`** (was fixed 2021-05-15 → 2026-05-15)
76. **Watermark table** in DuckDB (`watermarks` PK `(instrument_id, timeframe)`, lives next to existing `partitions` table)
77. **Bar identity hash** = `sha256(inst‖tf‖ts_us‖OHLC_paisa‖vol‖source_version)` — proves bit-identical bars across reruns
78. **Telegram delivery deferred** to a later phase. Track 1 uses structlog + dashboard primary
79. **4-layer dedup defence**: L1 Watermark check → L2 Bloom filter → L3 LMDB B+tree → L4 per-bar bar_hash
80. All `INDEX`-type instruments use `IDX_I` (single Index segment per Dhan annexure verified 2026-05-15; there is no separate `BSE_I`) — extends `VALID_SEGMENTS` from `{CASH, FNO}` to include `IDX_I`

### Evening additions (2026-05-15, post India VIX scope-correction + Dhan annexure)

81. **India VIX is REFERENCE ONLY** — fetched + stored exactly like the 3 spot indexes but NEVER part of brute-force combinations or the 52-condition vocabulary. Property P-25 (Phase 1) asserts no generated pattern mask references VIX.
82. **`vix_at_entry` + `vix_at_exit` columns** captured on every trade result row — snapshots of NIFTY VIX close at signal bar and exit bar respectively. Optional Stage 4 post-filter can slice winners by these columns.
83. **`BRUTE_FORCE_INDICES` (3) vs `REFERENCE_ONLY_INDICES` (1)** — `brutex/universe.py` partitions the 4 instruments. Engine code iterates `BRUTE_FORCE_INDICES` only.
84. **Dhan Data API rate limit verified**: **5 req/s + 100,000/day** from dhanhq.co/docs/v2 Rate Limit table. Adapter caps at 3 r/s effective (0.6 safety factor). First-run 92 chunks + daily 4 chunks = < 0.1% of daily quota.
85. **Dhan error code map locked** (`TRACK1_DATA_INGEST_DESIGN.md §10A`): Data API codes 800/804-814 + Trading API DH-901..DH-910 mapped to BruteX outcomes (AUTH_FAILED / BAD_REQUEST / FATAL / RATE_LIMIT / GENERIC_API_ERROR / TIMEOUT / NO_DATA). Single source of truth for `brutex/orchestrator/retry.py` retry/quarantine routing.
86. **Authentication: manual paste + auto-renew.** First-time setup = user copies Access Token + Client ID from `web.dhan.co` (24h validity) into `~/.brutex/dhan/access_token.json` (gitignored). Adapter calls `POST /v2/RenewToken` every ~22h (or when expiry < 25 min) to refresh. On renewal failure → FSM HALTED + CRITICAL alert. Full design in `TRACK1_DATA_INGEST_DESIGN.md §5A`. SENSEX securityId = 51 (verified from `web.dhan.co/Charts?exch=IDX&seg=I&secid=51` URL), exchangeSegment = `IDX_I`, instrument = `INDEX`. Allowed segment enum (verified from Dhan API docs UI): `{NSE_EQ, NSE_FNO, BSE_EQ, BSE_FNO, MCX_COMM, IDX_I}` — no `BSE_I`.
87. **All 4 securityIds verified + hardcoded** (user-confirmed 2026-05-15 evening). Previous "runtime CSV lookup, never hardcode" rule is **RELAXED for Track 1's fixed universe**. Master CSV resolver (`brutex/adapters/dhan/instruments.py`) is **DROPPED** from Phase 2 deliverables. Verified tuples now live in `brutex/adapters/dhan/constants.py` (Phase 2): NIFTY 50=`13`, BANKNIFTY=`25`, SENSEX=`51`, INDIA VIX=`21`, all on segment `IDX_I` with instrument type `INDEX`. Quarterly smoke test (`tests/smoke/test_dhan_secid_alive.py`) GETs each `web.dhan.co/Charts?exch=IDX&seg=I&secid=X` URL nightly to confirm IDs still resolve. Catastrophic-fallback approach (manual CSV fetch + constants update) documented in `TRACK1_BACKTEST_SPEC.md §5.4`. Rationale: 4-tuple is permanently locked + adding a 5th instrument is intentional friction + saves ~100k-row CSV download per startup.

### Late-evening additions (2026-05-15, 3-agent runtime audit + day/night envelope)

88. **Common runtime bootstrap** via `.claude/settings.json` SessionStart hook calling `scripts/ci/session_bootstrap.sh` (Phase 1 deliverable). 5-step, < 5s, read-only: git fetch + lake mount check + audit chain verify + watermark snapshot + last 5 MIGRATION decisions. Source: Agent A (Plan) audit.
89. **`brutex doctor` CLI** (Phase 1) runs all 10 C1-C10 invariants (TRACK1_DATA_INGEST_DESIGN.md §10.3) → single status table; exit 0/1; designed as the first command of any workday. Format documented in `TRACK1_RUNTIME_ARCHITECTURE.md §6.4`.
90. **MCP servers**: filesystem + github + duckdb + lmdb + prometheus. Filesystem and prometheus are community servers; duckdb + lmdb are thin Python wrappers (Phase 1 deliverable). Configured in `~/.claude/mcp.json` per project.
91. **Cowork per-task isolation**: git worktree per task + per-run LMDB sub-dir (`lmdb/work_items/<run_id>/`) + per-run audit JSONL (`audit/runs/<run_id>.jsonl`) + global `flock` on `~/.brutex/dhan.lock` for Dhan rate budget (5 r/s + 100K/day quota is global to user, not per run).
92. **`combo_hash` signature widening** (Phase 1 fix, gap G6 from runtime audit) — extend `brutex/strategies/combo.py` from current 4 inputs to spec-compliant 9 inputs (`strategy_mask_u64 ‖ direction_bit ‖ instrument_id ‖ timeframe_id ‖ mode_byte ‖ canonical_params ‖ data_version_hash ‖ indicator_version ‖ code_commit_hash`). Cheap to widen NOW; expensive to migrate cached results once Phase 3 lands. Source: Agent C cohesion audit.
93. **Result Parquet `dataclass`** codified in `brutex/strategies/result.py` (Phase 1 deliverable, gap G2) — codify schema BEFORE engine code lands, prevents drift like #92. Schema in `TRACK1_RUNTIME_ARCHITECTURE.md §3.7`.
94. **`WorkItem.content_hash` invariant property test** (Phase 1) — assert `content_hash == sha256(canonical(params))` for every stored item. Catches silent dedup-corruption class flagged by Agent C duplications table.
95. **Institutional verdict locked**: 5 BEATS / 7 MATCHES / 0 institutional FALLS-SHORT on the 12-pattern audit (Agent B). Honest gaps (GPU underuse, no distributed sweep, heuristic effective-N clustering, no regime-stratified Lockbox) tracked for future. Throughput target 80-150K trials/sec NOT YET MEASURED — Phase 3 micro-bench is the canary.
96. **Day vs Night hardware envelope split** (user-confirmed 2026-05-15 evening). Daytime (07:00-22:00 IST): 5-6 cores + 16-20 GB RAM + GPU idle (IntelliJ + Docker + Chrome + Claude Code competing). Nighttime (22:00-07:00 IST): 10 cores + 40-44 GB RAM + 20-core Metal GPU full. Throughput: 40-90K trials/sec day, 80-150K night CPU-only, 160-400K night with Metal GPU. Wall time for 33B-trial full sweep: ~3-4 days mixed, ~7 nights CPU-only night-only, ~3 nights with GPU. Auto-throttle in `brutex/orchestrator/scheduler.py` (Phase 2) detects mode via time-window + active-app scan. Resumable checkpoints every 100M trials. Documented in `TRACK1_RUNTIME_ARCHITECTURE.md §5A` and `TRACK1_PERFORMANCE_BUDGET.md §6A`.
97. **Single-Timeframe Invariant locked + 3 spec inconsistencies fixed** (2026-05-15 evening, 3-agent trial-count audit). User confirmed: "no multi timeframe — every (instrument × timeframe) is unique". Verified: zero of 52 atomic conditions read a second intraday TF; PDH/PDL/Fib/Pivots are scalar daily anchors broadcast across intraday bars (industry-standard pattern, NOT multi-TF). New §6.0 in TRACK1_BACKTEST_SPEC.md makes this explicit. Property P-25 (Phase 1) asserts the bar-mask evaluator only reads the chosen-TF Parquet plus the daily-anchor scalar table. Three spec inconsistencies fixed: (a) Stage 1B "45 inst-TF" → "63" (3 brute-force inst × 21 TFs; was stale leftover from 15-TF era); (b) Stage 1B trials "2.25M / 40 sec" → "3.15M / 56 sec" (consistent with 63 inst-TF); (c) §14 dedup hash comment "(one of 15)" → "(one of 21)". 33B Stage 2 figure unchanged (top-10K cap is independent of Stage 1B output). Naive (no funnel) trial count derived = 554 trillion → infeasible at any throughput → smart funnel reduces by 99.99994×. Source: 3 parallel agent audits (math re-derivation + consistency scan + multi-TF semantic check).
98. **2-4 day SLA is TARGET not GUARANTEE + Bloom math fix + canary gate + contingency SLA matrix** (2026-05-15 evening, 5-agent precision/throughput/benchmark audit). HONEST DISCLAIMER added to TRACK1_BACKTEST_SPEC.md §2.1: 80-150K trials/sec is NEVER MEASURED on real M4 Pro; memory-BW bound with zero margin; Phase 3 bench gate `tests/bench/test_full_sweep_extrapolation.py` is the SOLE validator. **Contingency SLA matrix:** ≥150K=2-3d / 80-149K=3-5d / 30-79K=5-12d (escalate AWS) / <30K=12-30d (HALT). NEW dashboard check **C11 `throughput.canary_fresh`** (in `brutex doctor`): RED if canary artifact > 14 days OR projected wall > published SLA — blocks workday. **Bloom filter math BUG FIXED:** prior spec claimed "5 GB at 0.1% FPR for 33B trials" — actual math: 5 GB / 33B = 1.21 bits/elem → FPR ≈ 56% (broken). Corrected: Bloom is sized for **survivors-only** (~150M-1B post-threshold-filter combos) → 40-267 bits/elem → FPR ≈ 1e-12 to 0. **Public benchmark gap honest:** no public M4 Pro "trials/sec for Python+Rust quant backtest" benchmark exists; closest analogs (vectorbt 50K sims/sec on M1, NEON byte-scan ~9× scalar, Erio-Harrison `simd_benchmark`) verify *plausibility* but not exactness. Probability 4-day claim holds in production: **55-65%** (Agent X worst-case audit). 18 precision risks identified (Agent 5 audit) — 4 CRITICAL (no `bar_hash` in code, no `int64 paisa` columns, resampler UTC-anchor bug, this Bloom math bug — first 3 already in Phase 1/2 plan). Source: Agents 5 + X + Y + Z parallel audit.
99. **AWS removed from escalation matrix + Metal GPU promoted to Tier 2 required scaling lever** (user-confirmed 2026-05-15 evening). No cloud, no sharding — Track 1 is LOCAL-ONLY. Apple 20-core Metal GPU (already in M4 Pro) is the only hardware escalation path. Updated contingency SLA matrix in TRACK1_BACKTEST_SPEC.md §2.1: added new top tier "≥ 300K t/s (CPU + GPU) → 1-2 days" with Phase 3.5 Metal kernel.
100. **Stage 2.5 — Bayesian adaptive early-stop locked** (2026-05-15 evening, Plan-agent design). Hybrid Bayesian posterior (Gaussian conjugate on 95th-percentile Sharpe) + Wald-style decision rule. Empirical-Bayes prior fit from Stage 1B Sharpe distribution. Prune if Pr(true Sharpe ≤ Stage-1B median) ≥ 0.95; promote if Pr(true Sharpe ≥ Stage-1B 99th pctile) ≥ 0.99. Hard floor 50K trials before decision + 10% of param surface. Expected 61% Stage-2 wall reduction (33B → ~13B effective). Verified by **P-25 + P-26 property invariants** (new) AND `tests/integration/test_stage2_5_no_winner_loss.py` A/B oracle (top-3 recall ≥ 95%; rolling 30d miss-rate budget ≤ 5% triggers auto α_prune raise to 0.98). References: Wald 1947 Sequential Analysis (Dover); Gelman BDA3 Ch. 2.5 (CRC 2013); Efron-Hastie 2016 Ch. 6 (Cambridge UP). Phase 2.5 implementation, 1 week effort, sits between Phase 2 and Phase 3 in build plan. Full design at `docs/TRACK1_OPTIMIZATIONS.md §2`.
101. **Phase 3.5 — Apple Metal GPU offload locked** (2026-05-15 evening, Plan-agent design). Local M4 Pro 20-core GPU only (NO CUDA / NO OpenCL / NO AWS). Workload split: bitmask AND (3-5× projected) + indicator precompute (5-10× projected) on GPU; trade simulation stays on CPU (branchy). Apple unified memory (`MTLResourceStorageModeShared`) = zero PCIe copy. Rust↔Metal via `metal` crate v0.27 + `objc2` v0.5, gated `#[cfg(target_os="macos", target_arch="aarch64")]`. Bitmask kernel threadgroup (32, 8) = 256 threads, batch 8192 strats × 94K bars (~850 KB per batch fits L2). CI gate `tests/bench/test_metal_kernel_speedup.py` asserts ≥ 2× CPU baseline + bit-exact numerical parity + audit-replay byte-identical Parquet. Fallback `BRUTEX_GPU_OFFLOAD=0` env flag → CPU-only path unchanged (no quality impact, just slower). Projected blended speedup ~2× → 33B wall 1-2 days CPU+GPU vs 2-4 days CPU-only. Phase 3.5 implementation, 3-4 weeks effort, sits after Phase 3 in build plan. Full design at `docs/TRACK1_OPTIMIZATIONS.md §3`.
102. **Combined optimization impact projected (Phase 2.5 + Phase 3.5 stacked)**: effective trials drop 33B → ~13B (Stage 2.5 prune); throughput rises 100K → 200-300K t/s (Phase 3.5 GPU); combined 33B-equivalent wall time drops to **~10-20 hours = 1 overnight session (22:00-07:00 IST) on M4 Pro alone, no cloud**. Subject to Phase 3 + 3.5 + 2.5 canary validation; SLA bands in spec §2.1 self-amend if measurements differ. Updated contingency matrix at `TRACK1_OPTIMIZATIONS.md §8` shows how Phase 2.5 + Phase 3.5 each shift the published SLA — combined they buy 3 full SLA tiers without hardware escalation.

### Late-evening additions (2026-05-16, 4-agent gap-filling deep research)

103. **Track 2 risk management spec written** (`docs/track2/TRACK2_RISK_MGMT.md`). Locks: SEBI position limits (₹1,500 cr EOD net / ₹10,000 cr EOD gross / ₹5,000 cr intraday net per client in delta-adjusted FutEq, effective 2025-10-01); margin stack (SPAN + Exposure + ELM + expiry-day +2% ELM + MTM + peak margin); hard rules (max loss/day 2-3%, per-trade 0.5-1% fixed-fractional, single-index cap 60%, DD ladder 10%/20%); tail-risk stress tests (−3%/−6%/−10% gap-down mandatory); liquidity floor (OI ≥ 10K + spread ≤ 5% premium); Dhan MIS 15:20 vs Zerodha MIS 15:26 (Track 1 forced 15:20 is Dhan-aligned, Zerodha-conservative); BANKNIFTY weekly DISCONTINUED post 2024-11-20 (monthly only); circuit-breaker matrix (10%/15%/20% halts can pre-empt 15:20 forced exit). 10 decision IDs DEC-RISK-001 through DEC-RISK-010.
104. **Track 2 tax handling spec written** (`docs/track2/TRACK2_TAX_HANDLING.md`). Locks: Section 43(5) non-speculative business income (transitions to Section 66 of Income Tax Act 2025 from 2026-04-01); new regime as simulator default (preserves PGBP business expenses); ICAI Revised 2025 Guidance Note turnover formula (sum of absolute squared-off P&L; no premium re-add; supersedes 2022 edition); Section 44AB audit threshold ₹10 Cr (F&O is 100% electronic so 5%-cash carve-out always applies); Section 44AD opt-out trap avoided (trader never opts in); 8-year carry-forward conditional on `filed_on_time` flag (Section 80 + 139(1)); 234C advance-tax buffer default 2% of pre-tax profit (configurable); set-off rules allow all heads EXCEPT salary. 8 decision IDs DEC-TAX-001 through DEC-TAX-008.
105. **`COSTS_VERIFIED.md §18` patched** — audit-requirement line corrected (the "profit < 6% of turnover" trigger was the 44AD trap, NOT 44AB threshold; clarified). Turnover formula reference added pointing to `TRACK2_TAX_HANDLING.md §3` for the ICAI Revised 2025 detail. Annual tax module comprehensive treatment moved to dedicated `TRACK2_TAX_HANDLING.md`; `§18` retains API stub + slab tables only.
106. **Coverage audit (Agent A, 2026-05-16)** — confirmed 11 of 16 originally-flagged "missing" Track 2 functionality items are already covered in existing locked specs (brute-force engine, replay engine, Parquet+DuckDB storage, stats engine with 30+ metrics, BSM Greeks, strike-chain replay via Dhan rolling option API, walk-forward 2yr/6mo/3mo × 16 windows, F&O tax classification, watchdog + LMDB resume, FastAPI dashboard, Telegram alerts). 4 items partial (White's Reality Check not by name but Romano-Wolf is the dominant alternative; position-sizing formula needed augmentation now in TRACK2_RISK_MGMT §3.1; VIX regime filter still reference-only by Golden Rule #3 — not gating; audit-replay UI/CLI deferred to forward-work). 1 item deliberately out-of-scope (SPAN/ELM live simulation per operator decision in `CALCULATOR_SPEC.md §"Out of scope"`).
107. **Brute-force engine deep research (Agent B, 2026-05-16)** — validates existing TRACK1_BACKTEST_SPEC.md architecture. Vectorized + u64 bitmask AND confirmed as correct pattern (chess engine "bitboard" idiom). DSR citation year already correct (Bailey & López de Prado, JPM 2014). Block bootstrap auto block-length ≈ 700 minutes (Politis-White 2004) for 1-min returns — to be added to stats engine implementation. Storage estimate ~2.5 TB compressed Parquet (33B trials × ~80 bytes/row after dict-encoding strategy_mask) — fits 8 TB SSD comfortably. Bloom-filter memory 40 GB for 33B masks at 1% FPR — borderline on 48 GB RAM, requires hierarchical bucketing (1024 × 40 MB buckets), already locked as approach in Stage-1 funnel pruning to ~150M-1B survivor combos per item 191 (bloom now sized for survivors-only).

108. **DEC-RISK-011 — INTRADAY-ONLY LOCK MADE EXPLICIT** (2026-05-16 evening, operator confirmation). Previously implied via Track 1 §7 "forced exit 15:20 IST" but never written as a single rule. Now explicit in `TRACK2_RISK_MGMT.md §2A`: Track 2 is intraday-only, MIS product only, NO NRML, NO overnight positions ever, NO held-to-expiry, NO weekend exposure, NO BTST. Forced exit 15:20:00 IST with no overrides. Three Phase-1 property invariants enforce this: P-INTRA-1 (entry.date == exit.date), P-INTRA-2 (exit_dt = 15:20:00 IST unless circuit-close), P-INTRA-3 (09:16:00 ≤ entry_dt ≤ 15:01:00). Material cost-model simplifications: (a) expiry-day +2% additional ELM is INAPPLICABLE (no position at SOD on expiry); (b) CALCULATOR_SPEC §4.2 `EXPIRY_*` branches are forward-compat stubs only — Track 2 hard-codes `trade_outcome = "NORMAL_CLOSE"`; (c) no overnight margin-funding cost; (d) no T+1 settlement risk. Tax implication: every P&L is realized same-day, audit log emits `held_overnight: false` on every trade.

109. **DEC-RISK-012 — 09:15 candle USED as first signal-eval bar** (2026-05-16 evening, operator decision). Diverges from `TRACK1_MICROSTRUCTURE.md §168` which excluded the 09:15 auction-influenced bar. Operator wants ALL available market data used; brute-force engine will discover empirically whether 09:16-entry strategies underperform 09:17-entry strategies, rather than prejudging via the auction-bar exclusion rule. Signal evaluation timeline locked in `TRACK2_RISK_MGMT.md §2A.2B`: at 09:16:00 IST (when bar "09:15" closes) signal evaluates using bar "09:15" data; if signal fires, entry executes at bar "09:16" OPEN price = 09:16:00 IST. Earliest possible entry execution = 09:16:00 IST. Latest possible entry execution = 15:01:00 IST (bar "15:00" close triggers signal; bar "15:01" OPEN executes). Forced-exit execution = 15:20:00 IST (bar "15:19" close triggers exit; bar "15:20" OPEN executes). Three additional property invariants P-INTRA-4/5/6 enforce these timestamps. Auction-bar caveat documented but not blocking — operator can revisit if empirical backtests show systematic degradation tied to the 09:15 bar.

---

## Acknowledgement

The 2026-05-15 pivot was driven by a 4-agent hostile-review pass.
The pattern is now memorialised in `CLAUDE.md §9` and
`TRACK1_PHASE_PLAN.md §0` as a non-negotiable gate at every phase
end. Track 2 Phase 0 went through 2 rounds of the same pattern
(cross-verification + hostile review) — see commits `6d848fa`,
`dd0af27`, `73e2613` and PR #16.
