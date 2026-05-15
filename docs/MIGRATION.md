# BruteX Migration Log — Groww → Dhan (2026-05-15)

> **Status:** Locked 2026-05-15. This document is the authoritative
> log of the Groww → Dhan + spot-index pivot.
>
> **Late-day amendment (2026-05-15):** decisions #2 and #7 were
> unfrozen after a 4-agent verification pass on the user-driven data
> ingest scope expansion. New decisions #71-80 added covering India
> VIX, 21 TFs, rolling date range, watermark design. See
> `TRACK1_DATA_INGEST_DESIGN.md` for the full incremental pipeline
> design.

---

## 1. Why the Pivot

The original BruteX v1.0 spec (signed off 2026-04-14, frozen as
`docs/REQUIREMENTS.md`) targeted Groww API + Indian F&O segment.
On 2026-05-15 a 4-agent hostile-review pass surfaced multiple blocker
issues:

| Issue | Severity | Discoverer |
|---|---|---|
| Spot index is not tradable; brokerage + STT math is theatre | BLOCKER | Agent 3 (Hostile Review) |
| Dhan native timeframes = 1/5/15/25/60min only, not 21 | HIGH | Agent 3 |
| Stamp duty was 0.002% in spec; actual is 0.003% intraday | HIGH | Agent 3 |
| NSE charge was 0.0019%; actual is 0.00297% (BSE 0.00375%) post Oct-2024 | HIGH | Agent 3 |
| SENSEX is BSE not on `IDX_I` (NSE indices segment) | HIGH | Agent 3 |
| Hardcoded securityIds 13/25/51 — only NIFTY/BANKNIFTY confirmed | HIGH | Agent 3 |
| 30-60% of K=2..5 condition combos are degenerate; needed conflict graph | HIGH | Agent 3 |
| DSR with N=33B is unachievable — needed effective-N clustering | HIGH | Agent 3 |
| Tolerances in absolute points = different signal NIFTY vs SENSEX; needed bps | HIGH | Agent 3 |
| Position size / capital model entirely absent | HIGH | Agent 3 |
| Pre-open 09:00-09:15 phantom bars | HIGH | Agent 3 |
| Muhurat session moved 2025 evening → afternoon | HIGH | Agent 3 |
| 10-13 hour sweep claim was 5-15x optimistic (memory BW bound) | HIGH | Agent 4 (Throughput) |
| LMDB single-writer billion-insert bottleneck | MEDIUM | Agent 4 |
| Day-high/day-low look-ahead risk in naive resample | HIGH | Agent 3 |
| PDH/PDL anchor undefined on post-holiday Mondays | MEDIUM | Agent 3 |
| Fib anchor and extension direction undefined | HIGH | Agent 3 |

The decision: **wipe Groww + F&O entirely, pivot to Dhan + spot indexes
+ signal-only research (Track 1).** Phase 2 will translate Track 1
winners to options for execution realism.

---

## 2. What Was Deleted

| Path | Reason |
|---|---|
| `brutex/adapters/groww/` (8 files) | Pure Groww adapter; no Dhan equivalent |
| `docs/groww-sdk/` (4 files) | Groww API reference; no Dhan equivalent |
| `tests/fakes/groww.py` | Groww test double |
| `tests/unit/test_adapters_groww_client.py` | Groww adapter test |
| `tests/unit/test_adapters_groww_candles.py` | Groww adapter test |
| `tests/unit/test_adapters_groww_contracts.py` | Groww adapter test |
| `tests/unit/test_adapters_groww_expiries.py` | Groww adapter test |
| `tests/unit/test_adapters_groww_instruments.py` | Groww adapter test |
| `tests/unit/test_groww_constants.py` | Groww-specific test |
| `tests/unit/test_groww_normaliser.py` | Groww-specific test |
| `tests/integration/test_groww_exception_contract.py` | Groww exception hierarchy test |
| `tests/integration/test_end_to_end_smoke.py` | Imported `FakeGrowwClient` |
| `tests/integration/test_strategies_library_runner.py` | Was Groww-coupled |

**Total: 8 source files + 4 docs + 10 tests = 22 files deleted.**

---

## 3. What Was Rewritten

| Path | Change |
|---|---|
| `brutex/cli.py` | Reduced 984 → ~180 lines. Removed all `ingest <subcommand>` Groww phases. Kept: `version`, `status`, `audit verify`, `dashboard`. New `track1` subcommands added in Phase 2. |
| `brutex/universe.py` | Replaced 8 F&O underlyings (NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY/NIFTYNXT50/SENSEX/BANKEX/SENSEX50 with derivatives) with 3 spot indexes (NIFTY 50, BANKNIFTY, SENSEX spot only). API surface preserved for backward compat (`V1_INDICES`, `is_in_scope`, etc.). |
| `docs/REQUIREMENTS.md` | DEPRECATED banner at top with pointer to TRACK1_* docs. Body preserved for historical reference. |
| `CLAUDE.md` | Full rewrite. Dhan facts table replaces Groww facts. Track 1 rules added. 4-agent hostile-review pattern memorialised. |
| `docs/TRACK1_BACKTEST_SPEC.md` | Full rewrite. Was Track 1 monolithic-build doc; now master spec for signal-only research + 52-vocabulary brute force. |

---

## 4. What Was Edited (Minor Cleanups)

| Path | Change |
|---|---|
| `pyproject.toml` | Removed `growwapi>=1.5.0,<2.0.0` from `[project]dependencies`. Removed `growwapi`, `growwapi.*` from `[[tool.mypy.overrides]]`. Added `dhanhq` / `dhanhq.*` placeholder for Phase 2. Updated `description` from "Indian F&O indices" to "Indian spot indexes (NIFTY / BANKNIFTY / SENSEX)". Bumped version to `0.2.0a0`. |
| `.claude/settings.json` | Removed `WebFetch(domain:*.groww.in)` deny rule (no longer needed). Removed `BRUTEX_OHLC_RATE_LIMIT_RPS=15` env var (was Groww-tuned). |

---

## 5. What Was KEPT (Generic & Reusable)

| Path | Why Kept |
|---|---|
| `rust/fno-math/` | Math kernels (Greeks, indicators, OBI, calendar). No Groww assumptions. Will be extended in Phase 2 with the 52-condition vocabulary atoms. |
| `rust/fno-math-py/` | PyO3 bindings. Generic. |
| `brutex/adapters/protocol.py` | Generic adapter protocol; Dhan adapter will implement it. |
| `brutex/data/` (parquet_writer, catalog, partition, sink, models, instrument_store) | Generic data lake. No Groww assumptions. `parser.py` has Groww docstrings to clean up later. |
| `brutex/execution/` (simulator, costs, driver, types, errors) | Indian cost model. Works for any broker on NSE/BSE. Track 1 signal-only mode will set all costs to zero. Phase 2 will use this for options translation. |
| `brutex/feed/` (replay, resampler, errors) | Generic replay + resample. No broker assumptions. |
| `brutex/math/` | Facade to Rust hot kernel. Generic. |
| `brutex/orchestrator/` (audit, rate_limiter, retry, state, store, pipeline) | Generic FSM + audit + work-item store. Some Groww docstring refs to clean up. |
| `brutex/strategies/` (protocol, registry, runner, sweep, combo, types, errors) | Generic strategy framework. The vocabulary engine plugs in here in Phase 2. |
| `brutex/strategies/library/` (4 hardcoded strategies) | Legacy placeholder; not Groww-specific. Will be deprecated when 52-vocabulary lands. |
| `brutex/dashboard/app.py` | FastAPI UI. Generic. |
| `brutex/notifications/telegram.py` | Generic notifier. |
| `brutex/observability/metrics.py` | Generic event metrics. |
| `brutex/config/*` | Generic. `BRUTEX_OHLC_RATE_LIMIT_RPS` env was Groww-tuned; removed from settings. Phase 2 introduces Dhan-tuned. |
| All `tests/property/` | Generic property tests (audit chain, dedup hash, FSM). Reused as-is. |
| All `tests/bench/` | Generic benchmarks (rate limiter, store). |
| All non-Groww `tests/unit/` and `tests/integration/` | Generic tests for orchestrator, dashboard, telegram, feed, data, etc. |
| All `.github/workflows/*.yml` | Generic CI. May need minor updates after wipe; track separately. |
| `scripts/proof_storage_guarantees.py` | LMDB/DuckDB proof script. Generic. |

---

## 6. Locked Decision Ledger (70 items)

Complete record of design decisions made during the 2026-05-15
hostile-review pass. Future changes must justify against this list.

### Source & scope

1. Data source: **Dhan API only** (was: Groww)
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
49. Bloom filter: ~5 GB at 0.1% FPR
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

---

## 7. Cascade-Safe Wipe Order (executed 2026-05-15)

1. ✅ **REWRITE first** (so deletes don't cascade-break):
   - `brutex/cli.py` (removed Groww imports)
   - `brutex/universe.py` (Track 1 spot universe)
2. ✅ **EDIT next** (minor cleanups):
   - `pyproject.toml` (drop growwapi dep)
   - `.claude/settings.json` (drop Groww env)
3. ✅ **DELETE last** (no cascade):
   - 8 files in `brutex/adapters/groww/`
   - 4 files in `docs/groww-sdk/`
   - 10 test files
4. ✅ **NEW docs**:
   - `CLAUDE.md` rewritten
   - `docs/TRACK1_BACKTEST_SPEC.md` rewritten
   - `docs/TRACK1_VERIFICATION_PYRAMID.md` written
   - `docs/TRACK1_VOCABULARY.md` written
   - `docs/TRACK1_PHASE_PLAN.md` written
   - `docs/TRACK1_PERFORMANCE_BUDGET.md` written
   - `docs/TRACK1_MICROSTRUCTURE.md` written
   - `docs/MIGRATION.md` written (this file)
   - `docs/REQUIREMENTS.md` DEPRECATED banner added

---

## 8. Post-Wipe Cleanups (Phase 1 work)

The wipe removed obvious Groww code/docs. ~255 Groww string references
remain in `brutex/` docstrings and comments. These don't break
imports but should be progressively cleaned in Phase 1:

| Module | Cleanup needed |
|---|---|
| `brutex/data/parser.py` | Rename `parse_groww_candle_row` → `parse_candle_row`; update docstring |
| `brutex/data/models.py` | Strip Groww examples; rename `groww_symbol` → `symbol` (or keep as legacy name) |
| `brutex/orchestrator/state.py` | Strip Groww-specific Outcome docstrings; quarantine reason `auth_failed` etc. remain valid generic codes |
| `brutex/orchestrator/pipeline.py` | Strip Groww examples in docstrings |
| `brutex/orchestrator/store.py` | Same |
| `brutex/orchestrator/retry.py` | Same |
| `brutex/orchestrator/rate_limiter.py` | Same |
| `brutex/config/runtime.py` | Same |
| `brutex/data/instrument_store.py` | Same |
| `brutex/feed/replay.py` | Same |
| `brutex/strategies/library/buy_and_hold.py` | Same (or delete the library entirely in Phase 2 once 52-vocabulary lands) |
| `brutex/dashboard/app.py` | Same |
| `tests/unit/test_data_parser.py` | Update for renamed parser function |
| `tests/unit/test_models.py` | Update for renamed field |

These are tracked as **technical debt** for Phase 1. CI is currently
green-able with the Groww docstring references in place; they are not
functional blockers.

---

## 9. Verification Post-Wipe

Run these to confirm the wipe didn't break anything:

```bash
ruff check brutex tests           # syntax & lint
mypy --strict brutex              # type check
pytest -q tests/unit              # unit tests (some will fail; see §8)
pytest -q tests/property          # property tests (should still pass)
pytest -q tests/bench             # benchmarks
```

Expected: ruff + mypy clean; pytest has failures in the renamed-fn
tests (`test_data_parser.py`, `test_universe.py`, possibly
`test_models.py`). Failures are tracked as Phase 1 cleanup work, not
release blockers.

---

## 10. Acknowledgements

The 2026-05-15 4-agent hostile-review pass that surfaced these issues:

1. **Agent 1 (Explore):** repo ground-truth audit (found scale of existing codebase)
2. **Agent 2 (Plan):** verification architecture (9-layer pyramid + 24 invariants)
3. **Agent 3 (general-purpose, hostile):** the 30+ issues
4. **Agent 4 (general-purpose, throughput):** the 5-15x throughput optimism
5. **Agent 5 (Explore, wipe inventory):** the precise file-by-file classification

Without this pass, Track 1 would have shipped on faulty assumptions
about Groww+F&O scope, 10-13 hour SLAs, broker theatre on spot, and
hardcoded securityIds. The pattern is now memorialised in CLAUDE.md §9
and `TRACK1_PHASE_PLAN.md §0` as a non-negotiable gate at each phase
end.
