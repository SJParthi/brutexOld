# O(1) Guarantee Audit — the honest per-path classification record

**Decision ID:** DEC-O1-GUARANTEE-HARDEN-001 · **Date:** 2026-07-13 ·
**Ledger:** MIGRATION.md #1790

**Scope:** the WHOLE product — (tier 1) the six Golden-Rule-#5 engine hot
paths, (tier 2) every DB/lake read path the dashboard serves, (tier 3) the
frontend (pollers + renders). Synthesized from four parallel read-only
audits (bench-infra/prior-art · engine · DB/lake · frontend) executed at
main tip 9201728 and re-verified on this branch.

**The anti-illusion rule (stated up front, applied to every row):** no path
may claim bare "O(1)" when its FIRST read is O(files)/O(rows) — such a path
is classified **O(1)-on-repeat** and its first-read cost is named in the
"Honest limit" column. "Genuine O(1)" is reserved for paths whose first call
is already constant. A cache that bounds *frequency* (a TTL/floor) does not
change the *class* of the underlying compute — both are stated. This is the
Golden-Rule-#10 posture of DEC-O1-CLAIMS-HONESTY-001 (#1528), applied
product-wide.

**Classes used:**

- **genuine-O(1)** — constant including the first call.
- **O(1)-on-repeat** — generation/stat-token/TTL cached; the first read (or
  first-per-revision read) pays the stated cost.
- **O(page)** — bounded by the requested page, not the store.
- **O(rows) / O(files) / O(dirs)** — linear in the named dimension; stated,
  never claimed away.
- **O(n log n)** — sort-bound; documented where it lives.

---

## 1. Classification table

Line cites verified on this branch (2026-07-13). "Bench gate" names the
tests/bench file and the tier it asserts at (see §3 for the tier split);
"none" means unit/route pins only.

### 1.1 Engine — the six Golden-Rule-#5 hot paths (+ their honest siblings)

| # | Path | Honest class | Evidence | Honest limit | Bench gate |
|---|------|--------------|----------|--------------|------------|
| E1 | Bar lookup — per-bar access (sweep hot path) | genuine-O(1) per access | `brutex/feed/replay.py:212-231` (`ReplayFeed.__iter__`); list-indexed `CandleBar` access, optional `LookAheadBarrier` (record=False, O(1) memory) | Load is O(window-files + window-rows) ONCE per launch via the window-indexed catalog query (`brutex/data/catalog.py:477-556`); per-bar merge is O(log S), S = symbol count (≤ a handful); corrupt row → loud raise at the read boundary (never a retry loop) | `test_bar_lookup_o1.py` (cache-tier) |
| E2 | Indicator value lookup (pivots/fib day-map + labelled get) | genuine-O(1) per lookup | `tests/bench/test_indicator_lookup_o1.py:1-33` docstring; constant-size label map (11 pivot / 12 fib entries) per call | Level arithmetic lives in the shared Rust core (golden-pinned separately); streaming indicators (VWAP/EMA/market-structure) are fixed-size state O(1)/bar | `test_indicator_lookup_o1.py` (cache-tier), `test_market_structure_o1.py`, `test_engine_stage1a_o1.py` |
| E3 | Combo dedup L1 — Bloom probe/add | genuine-O(1) | `brutex/orchestrator/bloom.py` (`might_contain` = k hash probes, lockless read; `add` under one lock) | FAIL_CLOSED at capacity (`BloomCapacityExceeded` — never silent FPR drift); bit-array bounded ≤ RAM/3, 16 GiB floor (`bloom.py:27-35`) | `test_bloom_o1.py` (strict-listed) |
| E4 | Combo dedup L2 — LMDB point get/put | O(1) effective; **O(log_B N) B+tree worst-case, honestly** | `brutex/data/lmdb_membership.py:235-238` (`__contains__` = one read-txn point get); `__len__` O(1) via `txn.stat()` (`:284-287`) | A 3-4-page constant walk at any realistic N — empirically flat under the ratio gate; `lmdb.MapFullError` → typed fatal, no O(N) degrade | `test_lmdb_membership_o1.py` (cache-tier per #1301) |
| E5 | Combo dedup composition — `SweepCache.seen_digest`/`record_digest` | genuine-O(1) per probe/record | `brutex/engine/sweep_cache.py:598-668` (Bloom-negative short-circuit; exact-FIRST durability ordering on record) | Resume pays `_rebuild_bloom_from_exact` O(persisted digests) EXACTLY ONCE at open (`sweep_cache.py:539-571`), FAIL_CLOSED + 25% auto-grow headroom (#1714) — **now linearity-pinned by this pass** | `test_engine_sweep_cache_o1.py`; NEW `test_sweep_cache_bloom_rebuild_cost.py` (linear pin: 10k→61.0 ms / 100k→78.0 ms = 1.28× COLD vs the 20× linear ceiling; the measured WARM band reaches ~8.7–9.2× — the ceiling's real headroom is ~2× over warm, NOT ~15× over cold; a quadratic regression reads ≥50–100×) |
| E6 | Ingest dedup twin — `OptionDedupIndex` probe/insert | genuine-O(1) | `brutex/data/option_dedup.py:209-252` | `_add_locked` is Bloom-FIRST then exact (`option_dedup.py:220-223`) — a durability-ordering asymmetry vs SweepCache's exact-first law (phantom Bloom entry on a mid-insert fault; no false dedup). Pre-existing in-source follow-up, deliberately NOT changed here | `test_option_dedup_o1.py` |
| E7 | Mask evaluation — u64 AND | genuine-O(1) per (bar, mask) | `brutex/engine/combo_mask.py:221` (`evaluate_mask` — `(bar_bits & mask) == mask`) | Batch `count_supermasks` is O(N_bars) per mask BY DEFINITION of the sweep workload — throughput-gated (50 M/s canary + M4-Pro absolute floor), not ratio-gated; bit positions ≥ `MAX_VOCAB_BITS` (128) rejected (`combo_mask.py:171-173`) | `test_engine_combo_mask_o1.py` (cache-tier), `test_throughput_canary.py`, `test_throughput_absolute.py` (M4PRO_ABS) |
| E8 | Mask evaluation — u128 two-limb | genuine-O(1) per (bar, mask) | `brutex/engine/combo_mask.py:356` (`_count_two_limb`); `ResidentSweepSimU128` MaskWord kernel (DEC-TRADE-SIM-U128-KERNEL-001) | Production-unreachable at the 60-bit vocabulary; every legal input rides u64 byte-identically | `test_engine_combo_mask_u128_o1.py` (cache-tier) |
| E9 | Result append — `ResultsStore.append` | genuine-O(1) amortized | `brutex/engine/results_store.py:585-680` (dict membership + insert; conflict branch = fixed 27-field diff) | Row validation O(fields) constant + a bounded ≤60-bit mask decode | `test_engine_results_store_o1.py` (cache-tier) |
| E10 | Result read — `ResultsStore.get` | **genuine-O(1)** (one dict probe) | `brutex/engine/results_store.py:690-692` (`return self._rows.get(run_id)`) | See §2 — the "1.87×" triage. Measured flat: 51.7 ns / 53.5 ns / 51.7 ns at 1k/10k/100k rows | `test_engine_results_store_o1.py` (cache-tier 3.0× per #1301); NEW deep-depth pin `test_get_ratio_is_o1_deep_depth` (100k-vs-1k) |
| E11 | `ResultsStore.flush` | O(rows) snapshot + O(n log n) sort, per tuple | `brutex/engine/results_store.py:735-763` | An END-of-tuple snapshot (single sweep call site), never per-append — no O(N²) accumulation; never claimed O(1) | none (snapshot op) |
| E12 | `ResultsStore.top_k` | **O(n log n)** full two-pass sort per call, documented | `brutex/engine/results_store.py:698-733` | Acceptable at the documented 10²–10⁴ finalist bound; /results caches the rank per revision (#1533). No mechanical guard against a future hot caller — named classification, not changed | none (route-layer cache is the enforcement) |
| E13 | `ResultsStore.load` | O(rows) per file | `brutex/engine/results_store.py:765-827` | Typed `ResultsStoreSchemaError` on damage (G14); skip-and-count readers — no O(N×corrupt) amplification | none (boundary op) |
| E14 | Audit append (hash chain, Golden Rule #8) | genuine-O(1) per entry | `brutex/orchestrator/audit.py:487` (`AuditWriter.append` — one hash-chain step + one line write) | Flat vs chain length | `test_orchestrator_audit_o1.py` (#1528 pin), `test_flight_recorder_o1.py` |
| E15 | Run identity — `combo_hash` (blake3) | O(payload bytes), payload bounded — O(1) in every sweep-scale dimension | `brutex/strategies/combo.py:173` (canonical-JSON of the 9-field identity → `fno_math.blake3_hash`) | Payload physics: hashing is linear in payload bytes and flat-vs-payload is NOT claimed — **now pinned** at-most-linear (measured 10.66× bytes → 10.59× time) + realistic-shape bound (futures `roll_schedule`, 80 ordinals → 3.54× base, pinned < 8×) | `test_blake3_combo_hash_o1.py` (flat-vs-grid-position + NEW payload pins) |
| E16 | `data_snapshot_hash` (run-identity input) | O(bars) ONCE per launch, by decree | DEC-SNAPSHOT-HASH-FULL-001 (full-content streamed digest — any single-bar change re-keys identity) | Reused as a fixed 64-char string by every `combo_hash` call — correctly not a per-combo cost | `test_snapshot_hash_bench.py` (cost pin) |

### 1.2 DB / lake read paths (dashboard)

| # | Path | Honest class | Evidence | Honest limit | Bench gate |
|---|------|--------------|----------|--------------|------------|
| D1 | `/db` candle pager — page DATA, legacy path | **O(rows)** per fresh click | `brutex/dashboard/db_view.py:3116-3143` (docstring says it plainly) | QUALIFY dedup + ORDER BY defeat row-group pruning; deep OFFSET adds O(row-groups up to offset) | `test_db_candle_query_o1.py`, `test_db_candle_query_production_scale_o1.py` |
| D2 | `/db` candle pager — stats-manifest path (#1614) | O(page-files) + **O(D) revalidation per consult** | prune plan `db_view.py:2430-2464`; cost stated `brutex/data/lake_stats_manifest.py:1095-1097` | Every consult re-stats all D month files; first star-select per file-state pays ONE O(D)-footer DESCRIBE (memoised); repeat PAGE clicks re-pay the O(D) sweep — only total/span is a true memo hit | `test_db_paging_o_page.py` (metadata-sweep tier + deterministic fs-op probes), `test_db_candles_baseline_bound_o1.py` |
| D3 | `/db` candle pager — page TOTAL + span | O(1)-on-repeat per write-generation | count memo `db_view.py:3398-3405`; `candle_count_key` `:3017` | First compute per generation = O(rows) `COUNT(DISTINCT)` unless the manifest validates (then O(D) stats) | `test_db_candle_count_cache_o1.py` |
| D4 | `/db` expired-options pager | same envelope as D1-D3 | `db_view.py:5111-5507`; `option_candle_count_key` `:5091` | Identical: O(rows) legacy page / O(page-files)+O(D) manifest page; total O(1)-on-repeat | `test_db_option_count_cache_o1.py`, `test_db_paging_o_page.py` |
| D5 | `/db` window-ledger pager (#1533/#1719) | O(page) render; **O(rows + delta-footers) per fresh stamp**; O(1)-on-repeat within a stamp | `db_view.py:6705-6864`; honest envelope in-source `:6754-6760` | Per-poll freshness probe = 1 base stat + O(deltas) listing; the legacy degrade (legacy-shape base / NULL cell / persistent duckdb.Error / conn=None) is a Python parse, itself cached per stamp — **now cost-pinned by this pass** | `test_db_window_ledger_page_o1.py` (cache-tier); NEW `test_window_ledger_degrade_memo_o1.py` (one parse per stamp on the degrade arm; delta append → exactly one re-parse) |
| D6 | Legacy ledger records/rollup (`window_ledger_rows_cached`, `spot_window_rollup`) | O(1)-on-repeat | `_LEDGER_READ_CACHE` `db_view.py:5870-5933` (LRU, single-flight, adaptive floor, SWR #1751) | First read per composite stamp = O(rows) Parquet parse; derived families compose two floors (staleness ≤ 120 s + 120 s, logged) | `test_ledger_delta_o_page_o1.py` |
| D7 | `/db` registry pagers — filtered COUNT | **O(1)-on-repeat** per registry revision (**hardened this pass — was wholly uncached**) | NEW `registry_count_key` `db_view.py:7867`, `_registry_total` `:7890`; token `lake_revision.py:388` (`registry_revision` — base + `.wal` stat); wired `app.py:1779,9024` | First click per revision = O(filtered rows) COUNT over the contracts table (~89k legs full-universe); token probe fault → uncached legacy (compute fresh, never stale); both token components are `include_ino=False` stat tokens, so a same-`(mtime_ns,size)` in-place overwrite within one mtime granule moves neither — the documented #1614 family residual (review round 2) | NEW `test_registry_count_cache_o1.py` (spy: 4 clicks → 1 COUNT; generation move → exactly +1; cached click flat at 2000-vs-200 legs) |
| D8 | `/db` registry pagers — page DATA | **O(page) result / O(filtered rows) scan** | `db_view.py:7972` (`list_registry_expiries`), `:8063` (`list_registry_contracts`) — DuckDB LIMIT/OFFSET | The D1-class caveat (review round 1): under the covering `ORDER BY` DuckDB scans the FILTERED rows into a top-(offset+limit) heap, so scan TIME is O(filtered rows) — O(page) in result/memory only; bounded per underlying. Store re-opened per request (cheap DuckDB open), deliberately unchanged | `test_registry_pagination_o1.py` |
| D9 | `/db` tables list (`table_summary`, #1751 G4t) | O(1)-on-repeat (SWR) | `db_view.py:655`; `_db_tables_cache` `app.py:1911` | Compute = COUNT(*) sweep over 3 DuckDB tables (tiny); wedge → honest 0-counts degrade, nothing cached on either degrade arm | NEW `test_db_tables_swr_o1.py` (5 polls → 1 sweep; master-revision move → +1; generation bump → +1) — closes a reader-C missing-gate finding |
| D10 | Dataset discovery (`list_candle_datasets`) | O(manifest-rows) manifest-first; **O(files) walk** on absent/stale manifest, once per generation | `db_view.py:1059-1152` (measured 29.7-min O(files) walk cited in-source `:1071-1073`); route memo + SWR `_db_datasets_cache` `app.py:1881` | The single worst first-read in the product; mitigations: manifest-first, generation memo, single-flight, post-pull warm, walk self-heals the manifest. Residual: the first-ever discovery pays it once | `test_lake_manifest_o1.py`, `test_db_tab_switch_discovery_o1.py`, `test_db_spot_cascade_fno_flat_o1.py` |
| D11 | `/movers` spot/futures/options candidates | O(1)-on-repeat per (coordinate, generation) | `_movers_cache` `app.py:1753` (LRU 256, single-flight, adaptive floor); compute month-bounded (`brutex/dashboard/movers_snapshot.py:845-891`) | First read per coordinate per generation = O(instruments × month files); 39-key warm grid primes defaults; hostile coordinate churn can evict the warm grid (bounded memory, recompute CPU — named follow-up, not changed) | `test_movers_cache_o1.py`, `test_movers_pcr_o1.py` |
| D12 | Freshness probe — `lake_revision`/`sweeps_revision` (SummaryCache `revision_probe`) | **O(dirs) per poll — NOT O(1)**, disclosed | `lake_revision.py:215` (`sweeps_revision` scandir digest); ledger component = 2 stats (`_ledger_revision` `:189`) | Paid on every generation read of every generation-keyed cache; pinned linear-in-dirs. The SummaryCache probe path is deliberately NOT TTL-memoised this pass (see D13 scope) | `test_movers_generation_probe_cost.py` (pins "the probe IS O(dirs), NOT O(1)") |
| D13 | Leaderboard + oneclick-runs freshness probes | O(dirs)/O(run dirs) per probe, **now TTL-memoised (≤1 probe/s per site, hardened this pass)** | NEW `RevisionProbeMemo` `lake_revision.py:495` (env `BRUTEX_REVISION_PROBE_TTL_S`, default 1.0 s; `0` disables = byte-identical legacy; junk fails LOUD via `resolve_revision_probe_ttl` `:455`); wired at exactly TWO sites: `app.py:7618` (leaderboard `sweep_results_revision`) + `app.py:7853` (oneclick `runs_revision_probe`) | HONEST: a ≤1 s freshness-detection staleness window is INTRODUCED, disclosed + kill-switched; a `None` (outage) probe is never memoised and evicts the prior entry — outage recovery immediate; the walk itself is still O(run dirs)/O(sweep dirs) per fresh probe and grows with history | NEW `test_revision_probe_ttl_o1.py` (6 polls → 1 sweep per route; post-TTL poll → exactly one re-probe) |
| D14 | `/coverage` spot+options summaries (SummaryCache) | O(1) hit; **O(files) refresh**, floor-throttled | `brutex/dashboard/summary_cache.py:1-52` (module docstring states it); adaptive ≤50%-duty floor `:107-133` | Floor bounds FREQUENCY only, never the refresh class; `/completeness` shares the SPOT entry (#895) | `test_summary_cache_o1.py` (strict-listed) |
| D15 | `/coverage/windows` counts + page | O(1)-on-repeat @ ledger stamp; O(rows + delta-footers) per fresh stamp | `make_window_payload_caches` `db_view.py:5936-5997` (SWR #1751) | Serve-last-known past floor bounds poll LATENCY; the fold still runs (≤1 revalidator per key) | `test_ledger_delta_o_page_o1.py` |
| D16 | `/completeness` + `/universe/completeness` + mapping-integrity | O(1)-on-repeat (write-gen + master_revision digest) | `app.py:2064-2068` (the three caches) | First read per composite token = O(datasets) + ledger projection; floored serve during a pull is bounded + logged | `test_nav_latency_composite_cache_o1.py` |
| D17 | `/universe` summary + instruments | O(1)-on-repeat per master revision | `brutex/dashboard/universe_cache.py:17-68` | The per-poll token probe is a COUNT+MAX SQL over the lean ~750-row table — tiny but not literally free; single-flighted cold miss | `test_universe_cache_o1.py` |
| D18 | `/results` sweep leaderboard | O(1)-on-repeat per `sweep_results_revision` | `_sweep_leaderboard_cache` `app.py:2107` (two-level; serve-last-known on probe outage) | Per-poll probe = O(sweep dirs) + 1 audit stat (now TTL-memoised, D13); first read per revision = O(stores × rows); composite index O(aggs files) once per its own token. ≤TTL mixed-generation window (review round 1 MINOR-2): the memoised sweep-scan token can pair with FRESH per-poll cascade/composite revisions for ≤TTL after a new sweep lands — graceful (the cascade join is per-item-tolerant; an unmatched verdict is excluded, never a crash), self-heals at TTL expiry | `test_ledger_delta_o_page_o1.py`, `test_cascade_join_o1.py`; NEW `test_revision_probe_ttl_o1.py` |
| D19 | oneclick runs listing | O(1)-on-repeat; probe O(run dirs) stats per fresh probe | `runs_revision`/`_runs_digest` `brutex/dashboard/oneclick_artifacts.py:884-959`; `discover_runs` parses ONLY the newest-limit manifests (stat-sort first, parse-spy-pinned) | The walk is deliberately UNCAPPED (F5 ruling) and grows with run history; this pass bounds its per-poll FREQUENCY (TTL memo, D13) — it does NOT cap the walk itself | `test_oneclick_o1.py` (B-2 primed-hit flat vs run count) |
| D20 | oneclick manifest / top100 / trades | O(1)-on-repeat per artifact stat token | `artifact_probe` `oneclick_artifacts.py:323-346`; trades sidecar page O(page) with single-entry parse memo | First read per artifact revision = O(artifact) parse; equity first read O(equity-file) then O(page) memo (#1665) | `test_oneclick_o1.py`, `test_top100_flat_read_o1.py`, `test_trade_page_o1.py` |
| D21 | oneclick events tail | **O(page)** genuine (byte-seek) | `tail_events` `oneclick_artifacts.py:1600` — seek(offset), ≤1 MiB window; EOF repeat poll short-circuits on the stat token | Filters apply to the WINDOW only — honestly not a whole-file search | `test_oneclick_o1.py` (events-tail flat + EOF-repeat pins) |
| D22 | oneclick verify | O(1)-on-repeat (3 stats + chain cache-peek) | route `app.py` verify block; key folds manifest+events+top100 tokens + verified-state | Chain verify = O(events file) on CLICK only, cached per events revision; a manifest-less run computes fresh per poll (cheap, disclosed) | `test_oneclick_o1.py` B-7 (measured 0.95× flat) |
| D23 | oneclick stat-OUTAGE path | genuine-O(1) serve-last-known | `OutageGate` `oneclick_artifacts.py:355` (LRU-bounded 512 both maps; ABSENT ≠ outage — a deleted artifact is never resurrected) | One WARN per outage per coordinate; recovery INFO | `test_oneclick_o1.py` (outage-serve-without-compute pin) |
| D24 | `lake_revision` token family | mixed — each named | `_ledger_revision` = 2 stats genuine-O(1) (`lake_revision.py:189`); `sweeps_revision` O(dirs) (`:215`); `sweep_results_revision` O(dirs)+1 stat (`:283`); `cascade_report_revision` 1 stat O(1) (`:318`); `rate_bridge_revision` 1 stat O(1) (`:344`); NEW `registry_revision` 2 stats O(1) (`:388`); `runs_revision` O(run dirs) | A `None` probe means "compute fresh, cache nothing" for lake_revision-family consumers vs "serve last known" for the outage-gated oneclick/leaderboard consumers — both honest, different postures; family-wide residual: a same-`(mtime_ns,size)` in-place overwrite within one mtime granule does not move a default (`include_ino=False`) stat token — the documented #1614 residual (review round 2) | probe cost pinned in `test_movers_generation_probe_cost.py`; composite one-stat pin per #1749 |
| D25 | Pre-open export rows memo (#1719 fix 2) | O(1)-on-repeat per (basis, range, symbols)@generation | `_preopen_rows_cache` `app.py:1833` (capacity 8) | Fresh compute = O(report rows) render + span-bounded month-file reads; CLOSE maps deliberately whole-partition (#1649a diagnostics) | NEW `test_preopen_export_memo_o1.py` (warm click: ZERO further `read_parquet` statements + ZERO row builds, bytes identical; `.zip` shares the memo coordinate) — closes a reader-C missing-gate finding |
| D26 | Corrupt-lake degraded read path | O(files) **stats** per degraded click; probe QUERIES latched | `_READABLE_SUBSET_CACHE` `db_view.py:1458-1494` (capped FIFO 64) | A persistent corrupt/alien file costs zero probe queries per repeat click; the stat sweep itself recurs (honest) | unit-pinned (spy call counts); no dedicated bench |
| D27 | Rate-bridge cache (#1522) | O(1)-on-repeat (2 s TTL + stat token) | `brutex/dashboard/rate_bridge_cache.py` | Post-TTL refresh under a 0.5 s deadline, non-blocking single-flight; at most one abandoned daemon per outage | `test_rate_bridge_cache_perf_o1.py` |

### 1.3 Frontend (templates + static JS)

| # | Path | Honest class | Evidence | Honest limit | Bench gate |
|---|------|--------------|----------|--------------|------------|
| F1 | Poller discipline — the shared `BXPoll` core | bounded per tick: hidden-guard-first, one-in-flight, counted failures, 3 s→30 s capped backoff, hung-fetch abort armed THROUGH the body read | `brutex/dashboard/static/bx-poll.js` (guard `:226-229`; `bxFetchJson` `:154-179`) | **Hardened this pass:** the 3 remaining templates whose ticks read bodies UNBOUNDED (coverage / spot_coverage / expired_options — a headers-then-stalled-body response wedged the one-in-flight chain until a tab flip) now ride the NEW COMMON `BXUI.fetchTextBounded` (`brutex/dashboard/static/bx-ui.js:145`); fetch_windows/completeness KEEP their self-contained local copies (deliberate — they do not load bx-ui.js) | node --check + source pins; NEW `TestSharedFetchTextBounded` node-EXECUTED golden (stalled-body → TimeoutError within the bound); `TestBoundedTickFetch` widened 2 → ALL FIVE templates |
| F2 | oneclick timeline render | O(new-records) per tick (skip-token) + **500-row render cap on a SLIDABLE viewport, truncation NAMED** | `bx-oneclick.js:414` (`BX_TIMELINE_RENDER_CAP = 500`), `:483` (`bxTimelineRenderRows`), `:506` (cap note), `:538` (render token); review round 1 viewport primitives `:426` (`bxTimelineClampOffset`), `:446` (`bxTimelineEarlierOffset`), `:462` (`bxTimelineTickOffset`); ring stays capped 5000 | **Hardened this pass:** was a full teardown+rebuild of up to 5,000 text nodes every ~3 s during a live sweep AND per search keystroke; now a dup-only tick renders nothing, search debounced 200 ms (house law). Review round 1 (MAJOR-A): the newest-500 slice previously DEFEATED the "load earlier" pager — a viewport offset now parks the render window on the fetched older rows (a live tick content-anchors it; run/filter change resets to newest); note reads "showing newest 500 of N events (render cap)" tail-anchored / "showing events X–Y of N (render cap 500)" parked | node-EXECUTED goldens (`TestTimelineRenderBounds`: slice endpoints, within-cap identity, cap-note copy, token stability + no-join-collision tooth; the viewport-pager golden — 3 loadEarlier clicks render the OLDEST loaded event, a live tick never re-anchors, offset-0 reset returns to the live tail) |
| F3 | universe.html interval loaders (windows line + six 15 s integrity loaders) | bounded latest-wins (**hardened this pass**) | NEW `boundedLatestJson` `brutex/dashboard/templates/universe.html:1125` — delegates to the existing `gbFetchJson` (abort armed through the body) + a per-loader generation claim + the per-loader NO-OVERLAP guard folded from main's #1763 client belt at the 2026-07-13 merge-reconcile (the interim inline `guardedPollJson` copy deleted) | Was: bare `fetch` with no abort bound + no ordering token (stale out-of-order paints, hung-socket accumulation); rendered output byte-identical, poll-hygiene pins pass unmodified | `TestUniverseBoundedLoaders` (7 bounded-call tokens + gen guard + killed bare-fetch tokens) |
| F4 | /screener render + filter/sort | bounded: input cap 5000, Show-all cap 1000, default render 200 — EVERY truncation NAMED on the count line | `brutex/dashboard/static/bx-screener.js:43-47,56-67,76-79,89-119` | Anti-illusion by construction (#1711 A-r4); backend half is TOP100_ROW_CAP=100 | node-goldened primitives (`bxRenderCount`/`bxCapNote`/`bxInputCapNote`) |
| F5 | Big-grid renders (completeness ~750 rows, fetch_windows, db grid) | O(rows-on-change) / O(page) — in-place tbody patch, shell never torn down | `completeness.html:279-348`; `db.html` `patchGridRows` | Whole tick behind a raw-text skip memo; unchanged payload = zero DOM writes | source pins (`test_dashboard_frontend_latency.py`) |
| F6 | /reports ~750 chips | build ONCE per symbol-set key; selection = O(chips) class sweep | `reports.html:300-340` | dual-listing key symbol+exchange | source pins |
| F7 | /results + movers renders | bounded full rebuild (server-clamped ≤500 rows), debounced 200 ms | `results.html:540-542,618-623`; movers skip-render sig | one-shot fetch bodies on results/movers remain headers-bounded only (one-shot, recoverable via reload/vis catch-up — documented, deliberately not changed) | source pins |
| F8 | Client memory | bounded everywhere found | timeline ring cap 5000 (+ render cap 500); SubUnit Set cap 4096; payload memos one string per endpoint; `OutageGate`-style maps LRU'd | no unbounded per-poll accumulation found (Breaker-B clean) | n/a |

---

## 2. The ResultsStore.get "1.87×" triage — VERDICT: NOT a regression

**The claim under triage:** a reported M4-Pro strict-tier O(1) bench failure
of `ResultsStore.get` at "1.87×".

**The path is a one-line dict probe.** `ResultsStore.get` is
`return self._rows.get(run_id)` (`brutex/engine/results_store.py:692`) over a
plain in-memory `dict[str, ResultRow]` — no Parquet parse, no schema probe,
no I/O of any kind. There is no scan on the path that COULD creep.

**Measured flat (2026-07-13 sandbox, best-of-5 × 200k iters):**
`t_1k = 51.7 ns · t_10k = 53.5 ns · t_100k = 51.7 ns` — ratios 1.034×
(10k/1k), 0.999× (100k/1k). Dead flat at 100× depth.

**The "1.87" figure has no trace in the repo** (grepped docs/tests/workflows:
only unrelated mockup/fixture floats). The ledgered history of THIS bench on
the M4-Pro strict tier is MIGRATION.md #1301
(DEC-O1-CACHE-TIER-MARGINAL-001): a **1.134×** singleton
(t_1k=31 ns → t_10k=35 ns, run 28691924626, 2026-07-04) against the
then-strict raw 1.1× ceiling, green on rerun — dict-probe cross-cache-tier
physics, not an algorithmic term (the foundational DEC-O1-CACHE-TIER-001
calibration measured dict lookup at 2.11× on the real M4-Pro when the large
working set spills L2/SLC into DRAM). #1301 rerouted the gate to
`cache_tier_ceiling(5.0)` → **3.0× on M4-Pro strict** — so a 1.87× reading
sits inside the established physics band (1.1×–2.11×+) and PASSES today's
gate with headroom, while the O(1) oracle keeps teeth (a genuine scan at
10k-vs-1k reads ≥10×). Any standing "1.87× red" must be from a head that
predates #1301's reroute — stale evidence.

**What this pass added:** the deep-depth pin
`test_get_ratio_is_o1_deep_depth` (100k-vs-1k, cache-tier ceiling) in
`tests/bench/test_engine_results_store_o1.py`, carrying the triage note
in-test so the next reader finds the verdict where the gate lives. No code
fix was needed; none was made.

---

## 3. Enforcement-split honesty (which gate actually blocks a merge)

- **The always-on, merge-blocking guarantee is the shared-CI ROBUST tier.**
  ci.yml's REQUIRED `bench` job runs the WHOLE `tests/bench` directory on
  every push + PR (feeding the `CI OK (aggregate gate)` required check) at
  the robust 3–10× ceilings of `tests/bench/_o1_tiers.py`. Census (measured
  review round 1, 2026-07-13, pre-QUAD tree): **121 bench test files** —
  **75** import `ratio_ceiling`, **19** use `cache_tier_ceiling` (3.0×
  strict), **2** use `metadata_sweep_ceiling` (+ the tiers file itself),
  **10** are `BRUTEX_M4PRO_ABS`-gated. Post-QUAD addendum (measured
  2026-07-13 at the merged tree, review round 4): the origin/main QUAD fold
  added `test_bar_screen_cost.py` + `test_lake_invariants_scan_space.py`,
  raising the total to **123**; both use bespoke cost/space assertions —
  none of the four `_o1_tiers` helper categories — so the 75/19/2/10
  sub-split is unchanged.
- **Which bench files actually EXECUTE on the bench job** (review round 1
  MAJOR fix — the section previously implied every file ran there): the job
  installs `.[dev,data,ui]` (the `ui` extra added by review round 1 — before
  it, the SIX route-level files carrying `importorskip("fastapi")` — the 3
  NEW gates `test_db_tables_swr_o1` / `test_preopen_export_memo_o1` /
  `test_revision_probe_ttl_o1` plus the 3 pre-existing
  `test_lake_census_route_o1` / `test_ledger_delta_o_page_o1` /
  `test_movers_pcr_o1` — were collected then SKIPPED there, enforcing
  nothing). With `ui` installed the whole directory genuinely runs except
  the 10 `BRUTEX_M4PRO_ABS`-gated files (verified locally with ui deps:
  371 passed / 11 skipped, every remaining skip the M4PRO_ABS class; the 6
  route files ran 3× stable). The former spurious `importorskip("openpyxl")`
  on the preopen gate was DELETED (the export renders via the repo's own
  hand-rolled `render_xlsx`; verified passing with openpyxl absent — no CI
  job installs openpyxl). Belt: the equivalent COST teeth also run on the
  unit job's spy tests (`test_dashboard_registry_routes` repeat-click-once,
  `test_dashboard_revision_probe_memo`, `test_dashboard_preopen_export_cache`
  warm-zero-statements, `test_dashboard_pull_contention_routes` #1751
  sweep-once), so a regression is double-covered.
- **The strict 1.1× tier + the absolute gates are post-merge and
  non-blocking BY DESIGN.** `bench-m4pro.yml` (self-hosted M4-Pro,
  `BRUTEX_M4PRO_STRICT=1` + `BRUTEX_M4PRO_ABS=1`) has deliberately NO
  pull_request trigger (DEC-M4PRO-PR-TRIGGER-001, MIGRATION.md #382), runs on
  path-filtered pushes + a daily cron, and its check is non-required +
  non-blocking at the pre-merge gate (DEC-PREMERGE-M4PRO-NONBLOCK-001,
  MIGRATION.md #1753). The 10 `BRUTEX_M4PRO_ABS` files (the ≥50 M ops/s
  absolute floor, sweep wall-time SLA, extrapolation, worker scaling, write
  A/B budgets, absolute baseline) SKIP entirely on shared CI.
- **Consequence, stated plainly:** a strict-tier (1.1×) or absolute-floor
  regression is caught post-merge at best (daily cron), or not at all while
  the runner is offline — the pending-forever shape is tolerated end-to-end
  per #1753. Only the robust-tier ratio law blocks merges in real time. The
  strict-tier pytest list covers ~13 files + 6 dedicated step-files; the
  other ~95 bench files execute at the robust tier only. This is the
  ledgered, deliberate trade (a bench regression is a post-merge red + issue,
  never a merge blocker) — recorded here so no one mistakes the strict tier
  for a merge gate.

---

## 4. What this pass hardened

Fixes (each one line; test files named):

- **F1 — bounded tick body reads on the 3 gap templates** (coverage /
  spot_coverage / expired_options): NEW COMMON `BXUI.fetchTextBounded`
  (abort armed THROUGH `r.text()`); the headers-then-stalled-body poller
  wedge class closed on its last 3 carriers —
  `tests/unit/test_dashboard_frontend_latency.py`
  (`TestSharedFetchTextBounded` node-executed golden + `TestBoundedTickFetch`
  widened to all five templates).
- **F2 — oneclick timeline render bounds**: skip-render mutation token (a
  dup-only events tick renders nothing), newest-500 render cap with the
  truncation NAMED, 200 ms search debounce —
  `test_dashboard_frontend_latency.py` (`TestTimelineRenderBounds`,
  node-executed goldens).
- **F3 — universe.html bounded latest-wins loaders**: `refreshWindowsLine` +
  the six integrity loaders ride ONE `boundedLatestJson` (gbFetchJson bound +
  per-loader generation claim) — `TestUniverseBoundedLoaders`.
- **H1 — registry pager COUNT cache**: the last wholly-uncached /db pager
  gains an O(1)-on-repeat filtered-COUNT memo keyed on the NEW
  `registry_revision` stat token (base + `.wal`) —
  `tests/unit/test_db_view_registry.py`,
  `tests/unit/test_dashboard_registry_routes.py`,
  `tests/bench/test_registry_count_cache_o1.py`.
- **H2 — revision-probe TTL memo**: NEW `RevisionProbeMemo` (default-ON,
  TTL 1.0 s, `BRUTEX_REVISION_PROBE_TTL_S`, `0` = byte-identical legacy;
  junk fails loud) bounds the leaderboard `sweep_results_revision` +
  oneclick `runs_revision_probe` O(dirs) walks to ≤1 per second per site —
  `tests/unit/test_dashboard_revision_probe_memo.py`,
  `tests/bench/test_revision_probe_ttl_o1.py`.
- **H3 — ResultsStore.get deep-depth pin** (§2) —
  `tests/bench/test_engine_results_store_o1.py::test_get_ratio_is_o1_deep_depth`.
- **H4 — missing bench gates landed** (a–g):
  (a) `test_registry_count_cache_o1.py`, (b) `test_db_tables_swr_o1.py`,
  (c) `test_preopen_export_memo_o1.py`,
  (d) `test_window_ledger_degrade_memo_o1.py`,
  (e) `test_blake3_combo_hash_o1.py` payload pins (measured linear
  10.66× bytes → 10.59× time; realistic `roll_schedule` shape < 8× base —
  flat-vs-payload is physics-impossible and honestly NOT claimed),
  (f) `test_revision_probe_ttl_o1.py`,
  (g) `test_sweep_cache_bloom_rebuild_cost.py` (resume Bloom rebuild linear:
  10k→61.0 ms / 100k→78.0 ms = 1.28× COLD vs the 20× linear ceiling — the measured
  WARM band reaches ~8.7–9.2×, so the real ceiling headroom is ~2× over warm,
  the reason the 20× ceiling was KEPT rather than tightened to 8×).

## 5. What this pass deliberately did NOT change (named follow-ups)

- **`MoversSnapshotCache` negative-result backoff** — by design nothing is
  stored on a raising compute, so a persistently-raising compute re-pays per
  poll; narrow in practice (computes degrade internally). Named follow-up:
  **DEC-MOVERS-CACHE-ERROR-BACKOFF-001**.
- **Movers LRU eviction churn** — `_movers_cache` capacity 256 can be
  thrashed by hostile coordinate churn, evicting the 39-key warm grid
  (bounded memory, recompute CPU). Note only; client params are already
  clamped, exposure limited.
- **`OptionDedupIndex._add_locked` Bloom-first ordering**
  (`brutex/data/option_dedup.py:220-223`) — the pre-existing in-source
  Rule-11 COMMON follow-up (swap to exact-first); durability nit, not a
  complexity issue.
- **fetch_windows/completeness keep their self-contained local
  bounded-fetch copies** — deliberate: they do not load bx-ui.js, and
  migrating them would add a new script dependency for zero behavioral win.
  One shared BXUI copy + two documented local copies.
- **`runs_revision`'s uncapped walk** — now TTL-bounded per POLL (H2), but
  each fresh probe is still O(run dirs) and grows monotonically with run
  history; a retention/prune policy is the real cap (F5 ruling stands).
- **The SummaryCache `revision_probe` path** (`lake_revision` →
  `sweeps_revision`, O(dirs)) is NOT wrapped by the H2 memo — H2 was scoped
  to the two named probe sites; folding the SummaryCache probe is a
  candidate second consumer if instrument×TF dirs grow.
- **`ResultsStore.top_k` O(n log n)** — documented (E12); the #1533 route
  cache is the enforcement; no engine change.
- **LMDB "O(1) point get" docstrings** — classification honesty recorded
  here (E4: O(log_B N) worst-case, effectively constant); wording-only,
  behaviorally moot.
- **universe.html `pollProgress` hidden-tab posture** — fetches mid-watch
  while hidden (browser-throttled); its gb/cv siblings skip. Plausibly
  deliberate for a live pull watch; recorded as an asymmetry, not changed.
- **app.py `_INDEX_HTML_TEMPLATE`** — dead, unreferenced legacy inline page
  with a fixed-cadence ws-reconnect; deletion candidate, not a runtime gap.
