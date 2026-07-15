# ADR-0021 — Capability-Driven Dual-Vendor Pull-Pipeline Orchestration (Groww ∥ Dhan)

- **Status:** PROPOSED (operator sign-off pending; design-only — no code ships
  under this ADR)
- **Date:** 2026-07-15
- **Decision ID (reserved, not yet ledgered):** `DEC-DUAL-VENDOR-PIPELINE-001`
  — the MIGRATION.md entry lands only AFTER operator sign-off, with a
  coordinator-reserved entry number. This ADR file is the design record; the
  ledger row and the CLAUDE.md blurb land with the implementation PRs.
- **Deciders:** operator (sign-off pending) · orchestration-track owner ·
  coordinator · the Dhan feed track (adapter-contract declarations only)
- **Supersedes (draft-level):** the aborted same-day 2026-07-15 draft of this
  file (never committed, never PR'd — found only as an untracked leftover)
  — that draft predated operator RULINGS A and B below and recorded a
  UNIFORM step law with an OPTIONS prebuild stage on both vendors; both are
  now WRONG by ruling and this corrected design replaces them.
- **Sibling tracks (explicitly NOT this ADR's scope):** the Dhan adapter
  internals (a sibling session — its declared contract is quoted in §1.3 and
  marked as THEIR declaration, never this track's own probe), and the
  stock-universe removal down to the 3 indices + INDIA VIX (a sibling
  session). This ADR designs the ORCHESTRATION LAYER ONLY: sequencing,
  per-vendor isolation, lake write-domain coordination, scheduling, gates,
  and the one ruled implementation item this track owns (the Groww
  options-prebuild retirement, §7).

---

## 1. Context

### 1.1 Operator directives (2026-07-15, via coordinator + the Dhan feed track)

All directives are same-day 2026-07-15; the later rulings refine the earlier.

**BASE directive:** the Groww and Dhan pull pipelines run **CONCURRENTLY
end-to-end with ZERO cross-impact** — separate rate limiters, separate auth,
separate locks, separate failure domains; one vendor's throttle / outage /
halt must never impair, block, or slow the other. WITHIN each vendor the
order is STRICT. Instruments are ONLY the 3 indices (NIFTY, BANKNIFTY,
SENSEX) + INDIA VIX (a sibling owns removing the stock universe; this design
assumes the minimized scope). Design doc FIRST; operator sign-off BEFORE
implementing.

**RULING A (capability-driven, NOT uniform):** each feed adapter DECLARES
what it serves (segments, option shape, depth law) and the sequencer runs
that vendor's OWN graph — never a hardcoded uniform step law. The ruled
capability map:

- **Groww** = spot + index futures + FULL per-contract option chains
  (per-expiry/per-strike addressing via `get_expiries` → `get_contracts`,
  CLAUDE.md §6).
- **Dhan** = spot + rolling ATM±10 options ONLY. Dhan has **NO expired
  futures at all** — the capability does not exist on the vendor, so the
  futures stage is not even an optional/flagged node on the Dhan graph; it
  is structurally ABSENT (operator ruling).

**RULING B (prebuild scope):** **PREBUILD IS FOR SPOT AND FUTURES ONLY —
OPTIONS NEVER get derived-timeframe prebuild.** Options stay 1-minute-native:
fills always resolve on the next 1-minute bar worst-case, so derived option
timeframes are pure wasted cost. This retires the options arm of
DEC-PIPELINE-AUTOPREBUILD-001's terminal prebuild (§7) and with it the
documented options-lake materialisation cost (a MEASURED multiple of the
1-minute footprint — observed `schema_meta` multiplier, conservative 6.5×
fallback per DEC-LAKE-FOOTPRINT-MEASURED-001; the older "~2-3×" wording was
already retired in-tree — `brutex/dashboard/gated_prebuild.py:553-558`).

**RULING C (Dhan NATIVE daily — operator ruling 2026-07-15 via the
coordinator, confirmed by the Dhan track; arrived AFTER authoring, folded
at the same-day input fold):** Dhan 1-DAY spot is pulled **NATIVELY** from
Dhan's daily endpoint (`POST /v2/charts/historical`; depth = instrument
inception) and runs **FIRST** — Dhan NEVER derives `1day`. Default window
2020-01-01 → today; the daily endpoint's depth reaches instrument
inception — the ~5y rolling cap is INTRADAY-only (§1.3 item 5). The Dhan
prebuild derives **INTRADAY TFs ONLY**: `include_daily` is structurally
OFF for the Dhan tree — a NEW PINNED INVARIANT (the native Dhan `1day`
partition is never derived and never prebuild-overwritten; the seam
already exists — `materialize_lake_timeframes(include_daily=...)`,
`brutex/data/timeframe_materialize.py`). Groww is UNCHANGED — it still
derives `1day` per DEC-1MINUTE-ONLY-DERIVE-1DAY-001, which becomes
EXPLICITLY a PER-VENDOR law (Groww-only — a conscious, ledgered SCOPING
note, §4.1 supersession 5; NOT a retirement). NEW capability dimension:
`daily_source = native | derived` (Decision 4). The Dhan track records its
side as its OWN DEC in a dedicated PR, which also carries a
tolerance-banded OBSERVE-ONLY daily-vs-1min consistency check (WARN past a
documented tolerance, never rewriting either side) — theirs, unlanded.

**SEQUENCER RULING on RULING C (this design's own, already communicated to
the Dhan track):** the Dhan spot phase is **TWO stage callables**
(`run_spot_daily` → `run_spot`), NOT sub-steps of one — each gets its own
gate, its own stage event, and its own `stopped_after` token, so a
native-daily failure halts the chain BEFORE any 1-minute vendor calls,
observably. `daily_source=native` declares the extra stage;
`daily_source=derived` (Groww) simply never binds `run_spot_daily`.

**FINAL RULED GRAPHS:**

- **Groww:** SPOT (2020-01-01 → today, rolling) → SPOT PREBUILD → index
  FUTURES → FUTURES PREBUILD → per-contract OPTIONS (1-minute-native, NO
  prebuild) → GREEKS. (`daily_source=derived` — no SPOT-DAILY node.)
- **Dhan:** NATIVE 1-DAY SPOT (the vendor daily endpoint, depth to
  inception — RULING C) → 1-MINUTE SPOT (rolling ~5y floor law,
  intraday-only) → SPOT PREBUILD (INTRADAY TFs ONLY — `include_daily`
  OFF) → rolling ATM±10 OPTIONS (1-minute-native, NO prebuild) → GREEKS.

**RULED IMPLEMENTATION ITEM OWNED BY THIS TRACK:** today's Groww pipeline
prebuild MATERIALIZES option timeframes (DEC-PIPELINE-AUTOPREBUILD-001 —
`discover_option_contracts` + the options arm of the contract phase in the
terminal prebuild). That must be REMOVED/excluded for options, with a
superseding DEC entry, an ABSENCE pin test, and a reader audit (§7). The
Dhan side pins its own options-prebuild ABSENCE in its own PR (their file
ownership; coordinated).

### 1.2 Minimized universe + track boundaries

- Engine + pull universe for this design: **NSE-NIFTY, NSE-BANKNIFTY,
  BSE-SENSEX + NSE-INDIAVIX** (CLAUDE.md §2 Rule 3 — VIX is REFERENCE ONLY,
  never in the vocabulary; DEC-ENGINE-SPOT-ONLY-001 — the engine is
  spot-only). The ~750-stock universe removal is a sibling's deliverable;
  this design is written against the minimized scope and does not depend on
  the stocks being present or absent (the capability graphs are
  instrument-list-parametric).
- The Dhan ADAPTER (SDK client, paging, parsing, rate stack, auth refresher,
  lake sub-layout under its write domain) is the Dhan feed track's ownership.
  This ADR consumes their declared contract (§1.3) through the
  `VendorPullProfile` interface (§5) and never reaches into their internals.

### 1.3 The Dhan adapter contract (the Dhan feed track's DECLARATIONS, 2026-07-15)

**Evidence honesty (Golden Rule #10) — timeline preserved, base updated at
the 2026-07-15 review fold:** the task brief named two team memory files as
the Dhan track's evidence base —
`/tmp/claude/memory/team/project/dhan-api-verified-facts.md` and
`/tmp/claude/memory/team/project/dhan-track-state.md`. **Neither existed at
the authoring commit** (`578ad3b` @ 2026-07-15 10:36:12Z; verified then
against the live memory store, which DID hold sibling Dhan-era records —
e.g. `dhan-intraday-float-timestamp-2026-07-15.md` — confirming the track
was live); **both LANDED ~7 minutes later** (mtime 2026-07-15 10:43:03Z,
verified via `stat` at the same-day adversarial-review fold). They are NOW
the primary declared evidence base for this section and are cited below;
the original rows came from the Dhan feed track's RELAYED contract (declared
2026-07-15 via the coordinator, grounded in their club PR #1551, branch
`integration/club-x-dhan` @ `b6341fc3`) and the memory files CONFIRM the
relayed values (90-day intraday cap per request · 5 r/s + 100,000/day data
limits with no per-minute cap · 24h ROLLING JWT · rolling-only options with
NO per-expired-contract history · `DEC-DHAN-ROLLING-FLOOR-001`) with three
MATERIAL deltas folded in below: (i) their recorded gap list G2/G3 still
EXPECTS an options prebuild + a FUT node — COUNTERMANDED after item 9;
(ii) Dhan expired index FUTURES history is marked UNVERIFIED in their own
facts file (item 2); (iii) Dhan INDIA VIX intraday availability + securityId
are marked UNVERIFIED (item 10). Everything stays THEIR declaration — not
this track's own probe. Every row must be re-confirmed against their landed
code at WS-D binding time; UNVERIFIED items are marked.

1. **OPTIONS SHAPE:** no per-expired-contract option history exists on Dhan.
   Only ATM-relative ROLLING series (`POST /v2/charts/rollingoption`):
   {underlying} × {WEEK, MONTH} × expiry_code 0-3 × ATM±10 strikes ×
   {CALL, PUT} = **1008 keys** for the 3 indices; the resolved expiry/strike
   are per-ROW columns, not partition keys. Lake subtree:
   `<lake_root>/feeds/dhan/rolling_options/**` — deliberately NOT under the
   primary `options/` tree, so fixed-contract readers structurally cannot
   touch it (their declared layout, ratified by Decision 1).
2. **FUTURES:** capability ABSENT on Dhan (operator ruling — the node is
   dropped entirely from the Dhan graph, not flagged/optional). The vendor
   FACT carries the memory file's own status, not only the ruling:
   `dhan-api-verified-facts.md` marks Dhan expired index FUTURES history
   **UNVERIFIED** ("docs do not confirm; nothing in the stack pulls futures.
   Probe before building") — the graph consequence is identical (node
   dropped), and any future revisit needs BOTH a decisive probe AND a fresh
   operator ruling.
3. **PAGING + RATE:** the intraday endpoint hard-caps **90 days/request**
   (spot); rolling options use **30-day tiles**. Data-API limits: **5 r/s +
   100,000 requests/day, NO per-minute cap** — a different SHAPE from
   Groww's 8 r/s + 500/min governor (Groww numbers: CLAUDE.md §2 Rule 6,
   DEC-GROWW-RATE-PERMIN-001 + DEC-GROWW-RATE-CEILING-8-002). Dhan has its
   OWN `SessionRateController` + `DailyQuotaTracker` instance in their stack
   (their club-PR lineage); the sequencer must thread ONE Dhan controller
   across all Dhan phases (the Groww one-controller-per-chain precedent —
   see §1.4 rate row) and never share/blend with Groww's.
4. **LOCKS:** Dhan's cross-process flock keys on `<lake_root>/feeds/dhan/` —
   DISJOINT from Groww's primary-root lock trio, so a Groww chain and a Dhan
   chain run fully parallel; Dhan spot + Dhan options SERIALIZE with each
   other on that one feed-root flock (exactly what a strict in-order
   sequencer wants — this design mandates NO intra-vendor phase parallelism,
   §4).
5. **DEPTH LAWS:** the Dhan spot floor is the ROLLING law
   `dhan_history_floor(today) = today − 5y + 7d` (their
   `DEC-DHAN-ROLLING-FLOOR-001`, in their lineage — not on main yet); the
   options floor unifies to `max(rolling 5y floor, measured options floor)`
   (being implemented by them now). **TRUE 1-minute depth is UNVERIFIED**
   (vendor docs say 5y, the SDK docstring says 5 days — an operator probe is
   pending); the sequencer must NOT hard-assume 5y works (Decision 4 makes
   the floor resolver adapter-owned and honest-degrade). **Scope note
   (RULING C fold):** the rolling ~5y law governs the INTRADAY (1-minute)
   pull ONLY — the NATIVE DAILY pull's depth reaches instrument inception
   (default window 2020-01-01 → today), so the Dhan `1day` series is NOT
   bounded by the intraday cap.
6. **NO DURABLE WINDOW LEDGER on the Dhan spine yet** (their gap G4):
   per-window outcomes are in-memory; a re-run re-issues vendor calls
   whole-range (the keep-first dedup absorbs the writes — see §1.4 lake
   row). They asked THIS design to RULE whether incremental daily
   convergence is a prerequisite; Decision 7 (§9) is that ruling — they
   build to it.
7. **AUTH:** a **24h ROLLING JWT** (not a 06:00 IST calendar reset) via
   read-only SSM re-read (`/tickvault/prod/dhan/{client-id,access-token}`);
   TickVault/Lambda owns minting; mid-chain token expiry is a real event;
   their `DhanSdkRefresher` single-flight re-read is already built. (Groww
   contrast: 06:00 IST daily token + read-only SSM re-read —
   CLAUDE.md §6 Auth row, DEC-AWS-ONLY-CREDS-001.)
8. **MARKET HOURS:** the Groww 08:45–15:35 IST market-hours pull guard does
   NOT bind Dhan (its own account; `market_closed=None` in their stack). Any
   Dhan-side market-hours policy would be a NEW decision to surface (open
   question Q2, §18), not an inherited one. The Groww guard's rationale is
   Groww-account-specific by its own docstring (§1.4).
9. **GREEKS:** the Dhan Greeks pass reuses the COMMON kernel with iv_source
   served/solved and NO formula bump (the
   DEC-GREEKS-CORRECTNESS-AUDIT-001 lineage) and is CLI-only today; the
   sequencer needs a callable seam for it as the final Dhan phase
   (Decision 4 + §13). **Declaration-vs-main correction (review fold):**
   their declared symbol `greeks_enrich.compute_bar_overlay` + "provenance
   bit 27" (quoted verbatim from `dhan-track-state.md`) do NOT exist on main
   @ `d6ac9f8` — the in-tree kernel entries are
   `greeks_enrich.enrich_option_bars` (`brutex/data/greeks_enrich.py:555`) /
   `_compute_bar` (:786), and the packed provenance stamp is a **27-bit
   pack occupying bits 0-26** (statuses at bits 23-26;
   `brutex/data/greeks_provenance.py:133` "Total 27 bits < 2^31") — there is
   no "bit 27" field. The declared symbol is therefore branch-only /
   UNVERIFIED against main; the WS-D binding targets the REAL kernel names
   and must re-confirm whatever their club branch actually exports.
10. **INDIA VIX (UNVERIFIED on Dhan):** `dhan-api-verified-facts.md` marks
   Dhan VIX intraday availability UNVERIFIED and the INDIAVIX securityId
   (~21) community-observed UNVERIFIED (only NIFTY=13/IDX_I is doc-cited;
   resolve at runtime from the scrip master). The Dhan graph (§3.2) carries
   the marker; the graphs are instrument-list-parametric (§1.2), so the Dhan
   chain degrades honestly to 3 symbols if the probe refutes VIX.

**RULING-B/A COUNTERMAND of the Dhan track's recorded TODO (review fold —
routed via the coordinator, never assumed coordinated):**
`dhan-track-state.md`'s own gap list records **G2** as a strict sequencer
"SPOT→prebuild→FUT→prebuild→OPTIONS→prebuild→Greeks" and **G3** as
"prebuild-after-options UNWIRED in `dhan_rolling_pull.py`" — i.e. the Dhan
track's recorded plan still EXPECTS an OPTIONS PREBUILD and a FUT node. Both
are WRONG by the operator rulings this ADR records: RULING B — no options
prebuild on ANY vendor; RULING A + item 2 — no Dhan futures node at all.
This countermand must be actively pushed to the Dhan track through the
coordinator (correcting their G2/G3 TODO) BEFORE their sequencer work
starts; §1.1's "the Dhan side pins its own options-prebuild ABSENCE in its
own PR" is a coordination OBLIGATION, not an assumption — their recorded
plan currently contradicts it.

### 1.4 Verified BruteX orchestration facts (this track's own verification, 2026-07-15)

Every row below was re-verified against the working tree at branch point
`origin/main` @ `d6ac9f8` (drifted line numbers from earlier investigations
corrected in place):

- **Sequencer:** `run_full_pipeline`
  (`brutex/dashboard/pipeline_pull.py:967`) is a thin, vendor-agnostic,
  injected-callable sequencer: spot → metadata → historical → ONE terminal
  prebuild → movers warm. Gates: `spot_phase_ok` (:149),
  `metadata_phase_ok` (:186), `historical_phase_ok` (:218),
  `step_settle_verdict` (:332); strict env `BRUTEX_PIPELINE_STRICT_GATE`
  (`STRICT_GATE_ENV`, :271); the shared pull-ledger scope
  `shared_pull_ledger_scope` ContextVar entered at :1140; market-hours belt
  at the top + re-checks before the metadata/historical phases.
- **Six-step order** (DEC-PULL-SIX-STEP-ORDER-001): `PullStep` IntEnum rank
  1-6 (`brutex/adapters/ingest_common.py:1797`), `partition_spot_steps` by
  `Instrument.is_index` (:1831) — INDIA VIX is an IDX row, so it rides
  INDICES-SPOT; `StepOrderMonitor` observe-only (:1864). Steps 1-2 run
  inside `pull_all_spot`; steps 3-6 run inside the F&O spine via
  `(ledger-market, is_index)` buckets — `_STEP_FOR_BUCKET`
  (`brutex/dashboard/historical_pull.py:203-208`) + `_bucket_sort_key`
  (:211), consumed by the ordered bucket loop (:1495-1547:
  `ordered_buckets = sorted(...)` at :1495, `step = _STEP_FOR_BUCKET[...]`
  at :1504). An additive `step_filter` seam at that loop lets a
  futures-only vs options-only STAGE invocation reuse the same spine
  (Decision 3).
- **Rate:** `SessionRateController`
  (`brutex/dashboard/pull_controller.py:992`) is PER-INSTANCE, not a global
  singleton — constructed once per dashboard app
  (`brutex/dashboard/app.py:2268`), per headless nightly binding
  (`brutex/nightly/pipeline_binding.py:236`), per CLI pull
  (`brutex/cli.py:6396`), and per crossverify run
  (`brutex/crossverify/run.py:1245`). A second vendor gets its own instance
  by construction.
- **Groww single-flight trio**
  (`brutex/adapters/groww/pull_singleflight.py`), acquire order
  account → lake → lease with LIFO release
  (`brutex/dashboard/pull_jobs.py:517-531`): the account flock keyed
  `pbkdf2(api_key)` at `~/.brutex/locks/pull_account<digest>.flock`
  (`account_pull_singleflight_lock`, pull_singleflight.py:464; filename law
  :57 + :440, DEC-ACCOUNT-KEY-SINGLEFLIGHT-001); the lake-root flock at
  `<lake_root>/.locks/pull_singleflight.lock`
  (`pull_singleflight_lock_path` :316, `pull_singleflight_lock` :336,
  LOCK_NB → `PullAlreadyRunningError`) — NOT vendor-aware: the module
  docstring (:42-70) documents that it serialises ANY two pulls sharing one
  lake root; the cross-host lease at `<lake_root>/.locks/pull_lease.json`
  (:79, `PULL_LEASE_FILE_NAME` :558, DEC-ACCOUNT-LEASE-CROSSHOST-001). The
  Dhan feed-root flock at `<lake_root>/feeds/dhan/` is DISJOINT from all
  three by construction.
- **Job seams:** `BackgroundPullJob.__init__` takes injectable
  `lake_root_resolver`, `singleflight_factory`,
  `account_singleflight_factory`, `account_key_resolver`, `lease_factory`,
  `market_hours_guard` (`brutex/dashboard/pull_jobs.py:435-443`);
  `start(...)` takes `cross_process_lock` / `job_kind` /
  `market_hours_exempt` (:572-584) with the market-hours START gate at
  :688. Concurrent-job precedent in ONE app process: `_pull_job`
  (app.py:3094) + `_greeks_backfill_job` (:3115) + `_crossverify_job`
  (:3132, own lock handling on its worker) already coexist.
- **Lake writes:**
  `bars/<exchange>/<segment>/<symbol>/<interval>/<yyyy>/<mm>.parquet`
  (`brutex/data/partition.py:6` + :61 + :180); expired options at
  `options/<series>/<YYYY>-<MM>.parquet`
  (`brutex/data/option_parquet_writer.py`; layout law restated at
  `brutex/data/lake_heal.py:222-224`); lake root `resolve_lake_root`
  (`brutex/config/paths.py:50`) — **NO vendor dimension in paths or bar
  columns.** Per-partition cross-process flock `partition_write_lock`
  (`brutex/data/partition_lock.py:344`; path law
  `<lake_root>/.locks/<sha256(realpath(dest))>.lock`, :307 + :339,
  DEC-LAKE-PARTITION-FLOCK-001) held across the whole read-modify-write;
  keep-first same-timestamp conflict keeps the STORED bar + records a
  `BarConflict` (`brutex/data/parquet_writer.py:30` + the
  `merge_mode="keep-first"` law :560-617); atomic
  tmp → fsync → `os.replace` (`brutex/data/atomic_io.py`). **Consequence:**
  cross-vendor same-timestamp bars in ONE tree would be silently arbitrated
  to whichever landed first — irreversible provenance loss — the core
  reason vendors must not share a tree (Decision 1).
- **Window ledger already vendor-namespaced:** `WindowLedgerRecord.market`
  is the first key field (`brutex/data/window_ledger.py:291`;
  `WindowLedgerRecord` :281); `GROWW_COVERAGE_MARKET="groww"`
  (`brutex/adapters/ingest_common.py:304`) + the `groww-future` /
  `groww-option` bucket tokens; market-scoped consumers exist end-to-end —
  representative: the operator remediation `reopen_skipped(market=...)`
  (window_ledger.py:1022 — a market-FILTERED mutation helper, named
  correctly per the review fold, not a read query) beside the market-keyed
  progress/rollup reads.
- **Market-hours guard** (`brutex/calendars/market_hours.py`):
  `BLOCK_START=08:45` (:43) / `BLOCK_END=15:35` (:47) IST, pure clockless
  predicate, kill switch `BRUTEX_MARKET_HOURS_GUARD` (:51),
  `resolve_market_closed` binding; the mechanism takes NO vendor argument
  and the module docstring (:1-29) states the rationale explicitly: the
  GROWW token is shared with TickVault's live feed on the SAME account
  (DEC-GROWW-MARKET-HOURS-GUARD-001; CLAUDE.md §2 Rule 6). The rationale is
  Groww-account-specific — it does not transfer to Dhan by mechanism or by
  reasoning.
- **Today-completeness gate** (`brutex/data/today_completeness.py`):
  `classify_today_completeness` (:335) is a pure function of (bars, window,
  NSE calendar, clamp date) — structurally vendor-agnostic; the forming-bar
  heuristics are Groww-shaped (DEC-PULL-TODAY-COMPLETENESS-GATE-001); kill
  switch `BRUTEX_TODAY_COMPLETENESS_GATE` (:155); fail-open wrapper.
- **Prebuild:** `run_pipeline_prebuild_guarded`
  (`brutex/dashboard/pipeline_prebuild.py:860`) takes
  `include_spot` (:875) / `exclude_symbols` / `workers` / `lake_root` /
  `disk_guard`; the parallel path holds `derive_write_lock(lake_root)` once
  around the worker pool (:801) — lake-root-KEYED, so per-vendor lock
  domains keep two vendors' prebuilds parallel (Decision 5).
  `run_gated_prebuild` (`brutex/dashboard/gated_prebuild.py:526`) runs
  SPOT → FUTURES → OPTIONS with `materialize_spot=None` ⇒ a REPORTED spot
  no-op (`_run_spot_noop_phase`, :488) and the options callables `None` ⇒ a
  REPORTED options no-op (`_run_options_noop_phase`, :508; injection gate
  :600-613) — the options-None arm is EXACTLY the seam the options-prebuild
  retirement uses (§7; no engine rewrite). Current options WIRING sites are
  recorded in §7.1.
- **Binding:** `build_headless_pipeline`
  (`brutex/nightly/pipeline_binding.py:121`, injectable
  `store_opener`/`session_rate`/`now`/env); scope tokens
  full / spot-only / fno-only (`brutex/dashboard/pull_scope.py`,
  `KNOWN_PULL_SCOPES` :95 — documented extensible,
  DEC-PULL-SCOPED-ALL-ONECLICK-001).
- **Greeks:** `run_greeks_enrich_pass`
  (`brutex/data/greeks_enrich_pass.py:147`) is the COMMON lake-scoped
  fenced pass (DEC-CLI-GREEKS-ENRICH-001). Precision (review fold): the
  KERNEL (the `greeks_enrich` solve/enrich machinery) is vendor-agnostic;
  the PASS's target DISCOVERY is bound to the PRIMARY F&O candle-lake
  layout (`FNO_CANDLE_LAKE_SEGMENT` / `bars/<exch>/FNO` CE/PE contract
  dirs) and cannot discover `feeds/dhan/rolling_options/**` — so the Dhan
  seam (§1.3 item 9, Decision 4) must bind at KERNEL level with its own
  discovery over the feed domain, never by pointing this pass at the Dhan
  tree. Both need spot in the SAME lake domain for the spot join
  (satisfied: spot lands FIRST in every vendor graph, §4).
- **Lake scanners are anchored, `feeds/` is invisible (re-verified):**
  `lake_content_token` (`brutex/data/lake_heal.py:2086`) walks EXACTLY
  `BAR_DIR_NAME` + `OPTIONS_DIR_NAME` under the root (:2131-2134); the
  lake-invariants census/L3 scan streams the `bars/` tree + `options_root`
  series dirs (`brutex/data/lake_invariants.py:689-767` + :1464-1479); every
  `/db` discovery walker and movers reader anchors at
  `lake_root / "bars"` or `lake_root / "options"` (grep-verified across
  `brutex/` — representative: `brutex/dashboard/db_view.py:1219`,
  `movers_snapshot.py:886`, `movers_stored_read.py:131`). A repo-wide grep
  found NO root-recursive `*.parquet` walker over `lake_root` itself, and
  ZERO existing references to a `feeds/` path component. So
  `<lake_root>/feeds/dhan/**` is invisible to every primary scanner today —
  by construction, not by luck.
- **no-dhan-guard** (`scripts/ci/no-dhan-guard.sh`): strips
  `DEC-[A-Za-z0-9-]+` tokens then fails on any case-insensitive "dhan" in
  `brutex/`, `scripts/` (minus itself), `CLAUDE.md`, `README.md`,
  `docs/runbooks/` (scan targets :66-72; strip+scan :104). `docs/` is
  OTHERWISE EXEMPT — `docs/adr/`, `docs/MIGRATION.md`, `docs/TRACK1_*.md`
  are unscanned (:31-37; `docs/TRACK1_DHAN_ADAPTER.md` + ADR-0002/0003/0009
  exist in-tree today). The guard is a REQUIRED job wired into the ci-ok
  aggregate (its wiring DEC is cited in the workflow —
  `DEC-CI-OK-NODHAN-GATE-001`, which is a workflow-side citation, not a
  bare ledger-header entry). **This ADR is guard-safe under `docs/adr/`.**
  Dhan-named ORCHESTRATION code in `brutex/` later requires the guard's
  retirement — HANDLED BY THE DHAN TRACK'S LINEAGE (input fold, 2026-07-15;
  §15 prerequisite 1: their `DEC-DHAN-GUARD-RETIRE-002`, retirement code
  shipped in their club member #1492 under the umbrella
  `DEC-DHAN-SPOT-FEED-001`; this track does NO guard work).
- **ADR numbering:** highest on disk is 0019 (with legacy duplicate pairs at
  0016 and 0017); ADR-0020 is SPOKEN FOR by in-flight PR #1435 (coordinator
  ruling) → this file takes ADR-0021.
- **Engine determinism:** run identity includes `data_snapshot_hash`
  (CLAUDE.md §2 Rule 4, DEC-SNAPSHOT-HASH-FULL-001) — the engine reads
  exactly ONE vendor tree (the primary/Groww surfaces).
- **Backup — the TWO systems are DISTINCT (review-fold correction; the
  earlier single row conflated them):** the OFF-BOX restic backup
  (DEC-BACKUP-OFFBOX-001) is fail-safe WHOLE-ROOT
  (`brutex/backup/plan.py:9-14`) — a NESTED `feeds/dhan/` subtree IS backed
  up for free on that lane. The NIGHTLY R50 backup
  (DEC-SWEEP-NIGHTLY-AUTOMATION-001) is an explicit ALLOWLIST
  (`brutex/nightly/backup.py:130-136` — window_ledger · registry.duckdb ·
  instruments.duckdb · catalog.duckdb · audit, plus the sweeps/results
  namespaces; `bars/` is excluded BY LAW, :398-408) — an unlisted `feeds/`
  subtree is STRUCTURALLY UNREACHABLE to it. Bounded today (Dhan bytes are
  re-pullable, like `bars/`, and Dhan has no durable ledger yet — gap G4),
  but a FUTURE Dhan window ledger under `feeds/dhan/` is non-re-pullable
  state and needs a nightly-allowlist addition (recorded as a Decision 7 /
  WS-D obligation, §2.3).

---

## 2. Decision 1 — Lake layout: ONE physical lake root, per-vendor WRITE DOMAINS; Dhan under `<lake_root>/feeds/dhan/**`

This RATIFIES the layout the Dhan track already built in club PR #1551 (their
declaration, §1.3 item 1) as the orchestration-layer law.

- `$BRUTEX_LAKE_ROOT` stays the PRIMARY (Groww) lake BYTE-IDENTICALLY — zero
  migration, zero reader change; the engine, dashboards, movers, `/db`,
  completeness, and crossverify keep reading the primary surfaces unchanged.
  The ENGINE reads the PRIMARY tree ONLY (Golden Rule #4 determinism via
  `data_snapshot_hash`; Dhan-as-engine-input/failover is explicitly an
  out-of-scope future DEC — open question Q4).
- **BINDING INVARIANT — CONFIRMED BY THE DHAN TRACK (2026-07-15; upgraded
  from asked/assumed at the input fold):** every Dhan byte lands under
  `<lake_root>/feeds/dhan/**` — their club-member-#1492 "feed-root nesting
  law": every Dhan byte INCLUDING its own `bars/`, catalog, `.manifest/`
  and `.locks/` nests under `feeds/dhan/` (rolling options at
  `feeds/dhan/rolling_options/**`; Dhan SPOT 1-minute CONFIRMED under the
  feed root, never the primary tree), with Groww byte-identity structurally
  pinned in THEIR club test battery — their attribution and their pin, not
  this track's own byte-check. The exact sub-layout remains the Dhan
  track's ownership UNDER this invariant. The top-level `bars/` +
  `options/` trees remain Groww-exclusive.

### 2.1 Rationale rows

(a) **Provenance.** The primary partition grammar carries no vendor
dimension (`brutex/data/partition.py:6`; no vendor column in bar rows), and
the writers are keep-first: a cross-vendor same-timestamp bar pair in ONE
tree would be silently arbitrated to whichever landed first
(`brutex/data/parquet_writer.py:30` + :560-617 — the stored bar wins, a
`BarConflict` is recorded, the value is never overwritten). That is
irreversible provenance loss AND it makes cross-vendor cross-verification
impossible (you can no longer say which vendor produced a stored bar).

(b) **Engine determinism.** Run identity folds `data_snapshot_hash`
(CLAUDE.md §2 Rule 4). A mixed tree would make the engine's input a
race-ordered function of two vendors' pull timing — nondeterministic run
identity by construction. One primary tree keeps the engine's input surface
exactly what it is today.

(c) **Isolation for free.** The Groww lake-root single-flight flock is NOT
vendor-aware — it refuses ANY second pull on the same root
(`brutex/adapters/groww/pull_singleflight.py:42-70`, the documented one-root
serialization). The Dhan feed-root flock at `<lake_root>/feeds/dhan/` is a
DIFFERENT lock file by construction, so the two chains are concurrent
without touching a line of locking code. Same for the per-partition flocks:
they key on `sha256(realpath(dest))` (`brutex/data/partition_lock.py:339`),
and dest paths in disjoint domains never collide.

(d) **Nested, not sibling.** The whole-root OFF-BOX backup include set
(DEC-BACKUP-OFFBOX-001 — that lane only; the NIGHTLY backup is an allowlist
that does NOT cover `feeds/`, §1.4 backup row) covers `feeds/dhan/` for
free; one volume serves the
disk guard; and the primary scanners are all anchored at `bars/` +
`options/` (§1.4 scanner row — re-verified: `lake_content_token` walks
exactly those two trees; no root-recursive walker exists), so `feeds/` is
invisible to them without any exclusion patch.

(e) **Fixed-contract readers structurally excluded.** The rolling-options
subtree deliberately does NOT live under `options/` (the Dhan track's
declared placement), so the expired-options readers/healers
(`option_parquet_writer`, the options census arms) can never misinterpret
ATM-relative rolling rows as fixed-contract history.

### 2.2 Rejected alternatives

1. **Shared tree** (Dhan writes into `bars/`): provenance loss under
   keep-first (rationale (a)), the lake-root single-flight would refuse the
   concurrent chain outright, and engine run identity goes nondeterministic
   (rationale (b)). Rejected on all three independently.
2. **Vendor path segment inside `bars/`**
   (`bars/<vendor>/<exchange>/...`): touches the partition grammar
   (`partition.py`) + every walker/census/heal/manifest consumer + either a
   full-lake migration or a permanent Groww-exempt asymmetry — a
   high-blast-radius change with no benefit over rationale (c)'s free
   disjointness. Rejected.
3. **Sibling root outside the primary** (a second Dhan lake root on its own
   volume/env var): loses the free whole-root OFF-BOX backup coverage (the
   restic lane specifically — the nightly allowlist covers neither shape,
   §1.4 backup row), adds a second volume/config surface + a second
   disk-guard domain. Rejected; per-vendor disk volumes stay out of scope
   (§17).

### 2.3 Belt-and-braces

Where a Dhan window ledger lands (post their G4 work, Decision 7), it uses
`market` tokens `"dhan"` / `"dhan-option"` — the `GROWW_COVERAGE_MARKET`
precedent (`brutex/adapters/ingest_common.py:304`;
`brutex/data/window_ledger.py:291`) — so provenance is carried IN-BAND in
the ledger too, not only by path placement. That ledger is non-re-pullable
state under `feeds/dhan/` — it MUST join the NIGHTLY backup allowlist when
it lands (the nightly lane exists precisely to protect non-re-pullable
state; §1.4 backup row — a WS-D obligation).

### 2.4 Honest costs

- Extra disk for the overlapping spot span (bounded: 4 symbols, Dhan ~5y of
  1-minute).
- The Dhan subtree is INVISIBLE to existing dashboards until explicit read
  surfaces land (a named follow-up, not this ADR).
- Per-domain heal/census wiring for `feeds/dhan/**` is the Dhan track's
  follow-up (the primary L3 heal/census deliberately does not descend into
  `feeds/`).

---

## 3. Decision 2 — Capability-driven per-vendor stage graphs (RULING A) with a CANONICAL stage order

The canonical stage order that every vendor's chain is a SUBSET of:

```
SPOT-DAILY (capability-bound: daily_source=native only — RULING C)
     → SPOT-1M → SPOT-PREBUILD (include_daily per capability)
     → [METADATA] → FUTURES → FUTURES-PREBUILD
     → OPTIONS (1-minute-native, NO prebuild — RULING B) → GREEKS
```

A vendor's chain = exactly the stages its declared capability binds
(Decision 4). An absent capability produces NO node — not a skipped node,
not a flagged node; the stage simply does not exist on that vendor's graph
(Dhan: no metadata stage, no futures stage — dropped entirely per the
operator ruling; Groww: no SPOT-DAILY stage — `daily_source=derived`, its
`1day` is produced by SPOT-PREBUILD per DEC-1MINUTE-ONLY-DERIVE-1DAY-001).

### 3.1 The Groww graph

Note on METADATA (review fold): the operator's FINAL ruled graph text omits
a METADATA node; it is inserted here as the structural prerequisite of the
ruled per-contract options capability (`get_expiries` → `get_contracts` is
the addressing RULING A itself names) — a refinement of the ruled text, not
a deviation. Note on SPOT-DAILY (input fold): the Groww profile declares
`daily_source=derived`, so the canonical SPOT-DAILY stage is ABSENT from
this graph (no `run_spot_daily` binding) — the Groww `1day` is produced by
SPOT PREBUILD per DEC-1MINUTE-ONLY-DERIVE-1DAY-001, now explicitly
Groww-only (§4.1 supersession 5).

```
  ┌──────────────────────────────────────────────────────────────────────┐
  │ GROWW CHAIN (its own SessionRateController · 8 r/s + 500/min governor│
  │ · Groww account flock + primary lake-root flock + lease · market-    │
  │ hours guard ON — CLAUDE.md §2 Rule 6, DEC-GROWW-MARKET-HOURS-GUARD-001)│
  └──────────────────────────────────────────────────────────────────────┘
   SPOT  (NIFTY/BANKNIFTY/SENSEX + INDIAVIX; 1minute;
   │      2020-01-01 const floor → today, rolling — GROWW_HISTORY_FLOOR,
   │      CLAUDE.md §2 Rule 3)
   ├─ G1 gate (spot_phase_ok)
   ▼
   SPOT PREBUILD  (derived 19 intraday TFs + 1day, spot symbols only)
   ├─ G2 gate (prebuild outcome ok over the clean set)
   ▼
   METADATA  (get_expiries → get_contracts — the per-contract addressing
   │          prerequisite; Groww-shaped, CLAUDE.md §6)
   ├─ G3 gate (metadata_phase_ok)
   ▼
   INDEX FUTURES  (the F&O spine with step_filter selecting the futures
   │               buckets — INDICES_FUTURES; 1minute)
   ├─ G4 gate (futures stage report ok)
   ▼
   FUTURES PREBUILD  (derived TFs for FUT contracts only)
   ├─ G5 gate
   ▼
   PER-CONTRACT OPTIONS  (step_filter options buckets; 1-minute-native;
   │                      NO prebuild — RULING B)
   ├─ G6 gate (options stage report ok)
   ▼
   GREEKS  (the COMMON fenced pass — run_greeks_enrich_pass)
   ▼
   movers warm (PRIMARY chain only, unchanged)
```

### 3.2 The Dhan graph

```
  ┌──────────────────────────────────────────────────────────────────────┐
  │ DHAN CHAIN (its own SessionRateController + DailyQuotaTracker ·      │
  │ 5 r/s + 100k/day, NO per-minute cap · feed-root flock at             │
  │ <lake_root>/feeds/dhan/ · NO market-hours guard — the Dhan track's   │
  │ declared contract, §1.3 items 3/4/8)                                 │
  └──────────────────────────────────────────────────────────────────────┘
   NATIVE 1-DAY SPOT  (POST /v2/charts/historical — the vendor's DAILY
   │      endpoint; depth = instrument inception; default window
   │      2020-01-01 → today; Dhan NEVER derives 1day — RULING C.
   │      Runs FIRST as its OWN stage callable `run_spot_daily` with its
   │      own gate + stopped_after token: a native-daily failure halts
   │      BEFORE any 1-minute vendor calls, observably)
   ├─ G1 gate
   ▼
   SPOT  (NIFTY/BANKNIFTY/SENSEX + INDIAVIX; 1minute; rolling floor
   │      dhan_history_floor(today) = today − 5y + 7d — their
   │      `DEC-DHAN-ROLLING-FLOOR-001`; the ~5y cap is INTRADAY-only,
   │      RULING C; TRUE 1-minute depth UNVERIFIED, probe
   │      pending — the sequencer never hard-assumes 5y; INDIAVIX
   │      availability itself UNVERIFIED on Dhan — probe-gated,
   │      degrades to 3 symbols, §1.3 item 10)
   ├─ G2 gate
   ▼
   SPOT PREBUILD  (derived INTRADAY TFs ONLY inside the Dhan write
   │      domain — include_daily structurally OFF, the RULING C pinned
   │      invariant: the native 1day is never derived, never
   │      prebuild-overwritten)
   ├─ G3 gate
   ▼
   ROLLING ATM±10 OPTIONS  (their rolling pull; 1-minute-native;
   │                        feeds/dhan/rolling_options/**; NO prebuild)
   ├─ G4 gate
   ▼
   GREEKS  (callable seam over the COMMON kernel; CLI-only today —
            the seam is this design's ask, §1.3 item 9)

   (no METADATA node — rolling addressing needs no expiry/contract
    resolution; no FUTURES node — capability ABSENT on the vendor;
    no movers warm — no dashboard read surface for the Dhan domain yet)
```

### 3.3 Gate + boundary semantics

- **Fail-closed WITHIN a vendor:** a stage failure sets
  `stopped_after=<stage>`, later stages are skipped, loud per-vendor events
  fire (§10) + a CRITICAL alert; the OTHER vendor's chain is structurally
  unaffected (separate job, separate locks, separate controller —
  Decision 5).
- **Market-hours belt** is re-checked at stage boundaries WHERE THE VENDOR'S
  GUARD APPLIES — Groww only (the guard binding is a profile field,
  Decision 4; the Groww chain keeps today's belt semantics from
  `run_full_pipeline`, DEC-GROWW-MARKET-HOURS-GUARD-001).
- **Intra-vendor: strictly sequential BY DESIGN.** No phase parallelism —
  this matches the Dhan feed-root flock's natural serialization (§1.3
  item 4) and the operator's strict-order requirement. The intra-spine
  six-step machinery (PullStep / StepOrderMonitor / `pull.step.*` events,
  DEC-PULL-SIX-STEP-ORDER-001) is retained per vendor unchanged.
- **Cross-vendor: fully parallel.** Disjoint lock domains, separate
  controllers, separate jobs; the two vendors' PREBUILDS may overlap in
  time. The derive-lock invariant — the Dhan prebuild keys
  `derive_write_lock` on the DHAN feed root, never the primary root — is
  **VERIFIED-AS-BUILT by the Dhan track (2026-07-15 — their verification,
  not this track's own byte-check):** their `run_dhan_prebuild` binds
  `materialize_lake_timeframes(lake_root=dhan_root)` whose
  `derive_write_lock` resolves under the FEED root →
  `feeds/dhan/.locks/derive.lock`, leaving the primary root's `.locks/`
  untouched by a Dhan prebuild — the two vendors' spot prebuilds never
  serialize on one derive lock. Pinned by their
  `test_spot_prebuild_derive_lock_keys_on_dhan_feed_root` (asserts the
  lock file appears under `feeds/dhan/.locks/` and the primary root stays
  clean) in their follow-up PR (branch `claude/dhan-options-native-floor`,
  locally complete, push held for their merge slot; a sub-clause of their
  `DEC-DHAN-OPTIONS-NO-PREBUILD-001`, unlanded). The lock is
  lake-root-keyed today (`brutex/dashboard/pipeline_prebuild.py:801`).
- **Movers warm:** PRIMARY (Groww) chain only, unchanged
  (DEC-PIPELINE-AUTOPREBUILD-001 lineage). The Dhan chain skips it — there
  is no dashboard read surface over the Dhan domain yet (§2.4).

---

## 4. Decision 3 — ONE sequencer, additively generalized (Golden Rule #11 COMMON)

Extend `run_full_pipeline` (`brutex/dashboard/pipeline_pull.py:967`) with
ADDITIVE optional staged callables:

- `run_spot_daily` (RULING C — the native-daily stage, bound ONLY where the
  profile declares `daily_source=native`; its own gate + stage event +
  `stopped_after` token per the §1.1 sequencer ruling), `run_prebuild_spot`,
  `run_futures`, `run_prebuild_futures`, `run_options`, `run_greeks` —
  beside the existing `run_spot` / `run_metadata` / `run_prebuild`
  (terminal) / movers seams.
- A `None` staged callable = the stage is ABSENT from this vendor's graph —
  logged honestly with the capability reason (never a silent skip; the
  `_run_options_noop_phase` honesty posture,
  `brutex/dashboard/gated_prebuild.py:508`).
- **ALL-None defaults reproduce today's behavior BYTE-IDENTICAL** (pinned by
  a defaults-parity test in WS-B): a caller that passes only today's
  callables gets today's spot → metadata → historical → terminal prebuild →
  movers chain, byte-for-byte.
- The F&O spine gains an additive `step_filter` param (default = all steps ⇒
  byte-identical) at the ordered-bucket loop
  (`brutex/dashboard/historical_pull.py:1495-1547`), so a FUTURES-only stage
  invocation and an OPTIONS-only stage invocation REUSE the same spine —
  no second spine, no copied loop (Golden Rule #11).
- **NO second sequencer module.** The legacy full-chain shape remains the
  default until cutover.

### 4.1 Conscious supersessions (ledgered at sign-off — never silent)

1. **RULING B supersedes-IN-PART DEC-PIPELINE-AUTOPREBUILD-001:** the
   OPTIONS arm of the terminal prebuild is RETIRED (§7). The spot(+futures)
   prebuild law of that DEC stands unchanged.
2. **The staged graph supersedes-IN-PART the single-TERMINAL-prebuild
   placement** for the dual-vendor chain: prebuilds move BETWEEN stages
   (spot prebuild after spot, futures prebuild after futures). The terminal
   placement remains for the legacy default chain until cutover — both
   placements share the same prebuild engine
   (`run_pipeline_prebuild_guarded` with `include_spot` /
   contract-kind selection).
3. **The GREEKS stage placement supersedes-IN-PART
   DEC-PULL-SIX-STEP-ORDER-001's post-step law** (review fold — previously
   an unrecorded silent supersession): today Greeks runs INSIDE the
   historical phase via the `pull_contract_candles` seam with the
   strict-gate `PriorStepsVerdict` threading + `_GREEKS_STRICT_WITHHELD`
   (`brutex/dashboard/historical_pull.py:1842` + :2477), while §3.1 draws
   GREEKS as a SEQUENCER stage after options with its own gate. WS-B must
   pick and pin the Groww binding: EITHER `run_options` carries the spine's
   internal Greeks seam (then the Groww profile binds `run_greeks=None` and
   the §3.1 GREEKS node is the spine-internal pass, drawn for capability
   honesty) OR Greeks is EXTRACTED to a real staged callable — in which
   case the six-step post-step placement + the strict-gate verdict
   threading are superseded-in-part and the mapping of
   `PriorStepsVerdict`/`BRUTEX_PIPELINE_STRICT_GATE` onto the staged gates
   must be specified in the WS-B PR. Either choice is a CONSCIOUS,
   ledgered decision, never drift.
4. **The movers-warm gate is RE-SPECIFIED for the staged graph** (review
   fold — "movers warm unchanged" cannot be literally true once the
   terminal prebuild is abolished): today the warm fires iff the TERMINAL
   `PrebuildOutcome.ok` over the clean set
   (DEC-PREBUILD-CLEANSET-001; `brutex/dashboard/pipeline_pull.py:121`).
   Staged-graph law: the warm fires iff spot-prebuild ok ∧ futures-prebuild
   ok ∧ the options stage settled (there is no terminal prebuild to gate
   on) — a supersession-in-part of the DEC-PIPELINE-AUTOPREBUILD-001 warm
   arm, pinned in WS-B.
5. **DEC-1MINUTE-ONLY-DERIVE-1DAY-001 is SCOPED PER-VENDOR — Groww-only
   (RULING C, input fold; a conscious, ledgered SCOPING note, NOT a
   retirement):** the "1-minute is the ONLY fetched granularity; `1day` is
   DERIVED from it" law remains byte-true for the Groww chain (its graph,
   prebuild and goldens are untouched); the Dhan chain fetches its `1day`
   NATIVELY by operator ruling and NEVER derives it (`include_daily`
   structurally OFF on the Dhan tree — the pinned invariant). Another
   capability-declared difference (`daily_source = native | derived`,
   Decision 4); the scoping is recorded in the ledger entry at sign-off.

---

## 5. Decision 4 — `VendorPullProfile` / `VendorCapability`: the ONE new abstraction

A FROZEN per-vendor declaration consumed by the binder. Fields (each row
names its owner — the ORCHESTRATION layer never re-mints adapter facts):

| Field | Groww binding | Dhan binding | Owner |
|---|---|---|---|
| `vendor` token | `"groww"` | `"dhan"` | orchestration |
| Lake write domain | primary root (`bars/` + `options/`) | `<lake_root>/feeds/dhan/` | orchestration invariant (Decision 1) |
| Capability set | `{spot, futures, options: per_contract, metadata}` | `{spot, options: rolling_atm}` | operator ruling A |
| Daily (`1day`) source | `derived` — SPOT-PREBUILD derives `1day` from stored 1-minute (DEC-1MINUTE-ONLY-DERIVE-1DAY-001, Groww-only per §4.1 supersession 5) | `native` — the vendor daily endpoint, depth to inception, runs FIRST as `run_spot_daily`; prebuild `include_daily` structurally OFF (the RULING C pinned invariant) | operator ruling C |
| History-floor resolvers | const `GROWW_HISTORY_FLOOR` = 2020-01-01 (CLAUDE.md §2 Rule 3) | rolling `today − 5y + 7d` + unified options floor (`DEC-DHAN-ROLLING-FLOOR-001`; TRUE depth UNVERIFIED) | adapter |
| Paging profile | interval-tier chunks (CLAUDE.md §6 chunk row) | 90d spot / 30d rolling tiles (§1.3 item 3) | adapter |
| Rate factory | ONE `SessionRateController`, 8 r/s ceiling + 500/min governor (DEC-GROWW-RATE-CEILING-8-002, DEC-GROWW-RATE-PERMIN-001) | ONE `SessionRateController` + `DailyQuotaTracker`, 5 r/s + 100k/day, no per-minute cap (§1.3 item 3) | adapter (numbers), orchestration (one-instance-per-chain law) |
| Auth / reauth binding | 06:00 IST daily token, read-only SSM re-read (DEC-AWS-ONLY-CREDS-001; CLAUDE.md §6 Auth row) | 24h rolling JWT, SSM re-read via their single-flight refresher (§1.3 item 7) | adapter |
| Account-key resolver | Groww `api_key` fingerprint (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001) | Dhan client-id-keyed (their stack) | adapter |
| Lock domain | account + lake-root + lease trio at the primary root | the ONE feed-root flock at `feeds/dhan/` | adapter (mechanism), orchestration (disjointness invariant) |
| Market-hours guard binding | ON, LOCKED (DEC-GROWW-MARKET-HOURS-GUARD-001) | `None` (§1.3 item 8; any Dhan policy = NEW decision, Q2) | operator |
| Ledger / incrementality capability | durable window ledger, `market="groww"` family | ABSENT today (gap G4) — gates auto-arm per Decision 7 | adapter, ruled by orchestration |
| Greeks seam | the COMMON fenced pass binding | a callable over the COMMON kernel (CLI-only today; seam = this design's ask) | shared kernel; binding per vendor |
| Ledger market tokens | `groww` / `groww-future` / `groww-option` | `dhan` / `dhan-option` (Decision 1 §2.3) | orchestration convention |

The BINDER derives WHICH staged callables to pass to the ONE sequencer from
the capability set — this is how capability-driven graphs stay STRUCTURAL
without a graph engine: no capability ⇒ no callable ⇒ no node.
`daily_source=native` additionally binds `run_spot_daily` as the FIRST
stage; `derived` never binds it (RULING C + the §1.1 sequencer ruling). The
Groww profile binds today's spines byte-identically. The Dhan profile binds
the Dhan track's spines (their implementation, against this interface —
§12).

**Ownership collision to broker BEFORE WS-C (review fold — Golden Rule #11
COMMON):** `dhan-track-state.md` records that club PR #1551 already carries
a vendor binding/REGISTRY layer —
`brutex/feeds/{binding,registry,groww_binding,dhan_binding}.py` +
`brutex/data/feed_root.py` — squarely overlapping this profile/binder role
(their content is UNINSPECTED from this track; a coordination risk, not a
proven duplication). Two competing vendor-abstraction surfaces would violate
the ONE-source-of-truth law, and a feeds registry is orchestration-shaped
even though it lives in the adapter track's stack. WS-C must inspect that
layer first and either (a) declare `VendorPullProfile` the EVOLUTION/home of
it (WS-C consumes/renames), or (b) record why they are genuinely distinct
layers — decided in the WS-C PR with the coordinator brokering ownership,
never two silent parallel abstractions.

---

## 6. Decision 5 — Concurrency + isolation matrix (the zero-cross-impact rows)

| Dimension | Groww chain | Dhan chain | Isolation mechanism | Shared residual? |
|---|---|---|---|---|
| Job slot | its own `BackgroundPullJob` (per-vendor injected lock factories + guard — the ctor seams exist, `pull_jobs.py:435-443`) | its own `BackgroundPullJob` (same seams; precedent: the concurrent `_crossverify_job`, app.py:3132) | separate job objects + worker threads | the dashboard PROCESS if both run in-process (see residuals) |
| Rate / quota | ONE `SessionRateController` (8 r/s + 500/min — DEC-GROWW-RATE-PERMIN-001) | ONE `SessionRateController` + `DailyQuotaTracker` (5 r/s + 100k/day — §1.3 item 3) | per-instance controllers (`pull_controller.py:992`; construction sites §1.4) — zero shared budget | none: a Groww 429 storm steps ONLY Groww's AIMD ladder; a Dhan quota exhaustion halts ONLY the Dhan chain |
| Auth | Groww TOTP/daily-token SSM re-read (CLAUDE.md §6) | Dhan 24h JWT SSM re-read (§1.3 item 7) | separate credentials + separate reauth machinery; account single-flight keys per vendor account | none |
| Locks | account + lake-root + lease trio at the primary root | ONE feed-root flock at `feeds/dhan/` | disjoint files by construction (Decision 1 rationale (c)); per-partition flocks are path-keyed (`partition_lock.py:339`) so they never collide across domains | HONEST ASYMMETRY (review fold): Dhan declares NO account-level single-flight and NO cross-host lease — flock is single-host, yet §9 discloses TickVault CO-TENANCY on the Dhan account (shared 100k/day budget). Two hosts sharing the Dhan account have no account exclusion/cross-host detection. Accepted for v1 under the single-box envelope (CLAUDE.md §8) + named as a Dhan-track follow-up (the DEC-ACCOUNT-KEY-SINGLEFLIGHT-001 / DEC-ACCOUNT-LEASE-CROSSHOST-001 parity gap) — never claimed equivalent to the Groww posture |
| Pull-OBSERVABILITY singletons (review fold — the previously missing row) | the ONE `RateLimitPanel` (`brutex/dashboard/app.py:446`) fed by `make_rate_observer`/`RatePanelSampler` over the app's ONE `_session_rate` (:2268, :3779); `GET /api/v1/pull/rate` + the DEC-RATE-METER-LIFECYCLE-001 meter registration (`register_pull_meter`, `pull_controller.py:1140` — the "no in-process pull running ⇒ no meter registered" process invariant, with its documented eviction blast radius); the DEC-OPERATOR-HEALTH-TRUTH-001 health-hub pull lane + `last_pull` retained terminal keyed on the ONE `_pull_job` slot (app.py:3094; `pull_jobs.py:1561`); the process-global `MetricRegistry` pull-health gauges | **DARK in v1 — a NAMED gap** (the pull-plumbing twin of the §2.4 lake-read invisibility): the Dhan chain surfaces NONE of these Groww-slot readouts | **WS-D PIN:** the Dhan chain NEVER registers into the Groww panel / pull-rate meter / health-hub pull lane — registering would blend/evict the Groww readout, break the meter-lifecycle invariant, and mis-color the health-hub pull lane (three DECs of readout honesty). Per-vendor observability surfaces land in WS-E | the Dhan chain is observably dark on these surfaces until WS-E (disclosed; its own job snapshot + structlog stage events are the interim visibility) |
| Prebuild derive lock | `derive_write_lock(primary root)` | `derive_write_lock(dhan feed root)` — VERIFIED-AS-BUILT by the Dhan track (2026-07-15: `feeds/dhan/.locks/derive.lock`, primary `.locks/` untouched; their pin `test_spot_prebuild_derive_lock_keys_on_dhan_feed_root`, §3.3) | lake-root-keyed lock (`pipeline_prebuild.py:801`) → per-domain keys → the two prebuilds may overlap | host CPU (below) |
| Failure domain | per-vendor job thread/process; `MarketHoursHalt` / throttle / auth expiry / mass-empty halt the GROWW chain only | per-vendor job thread/process; quota exhaustion / JWT expiry halt the DHAN chain only | fail-closed within a vendor (§3.3); no shared exception path | the honest shared residuals below |
| Scheduling lane | dashboard job OR the nightly/CLI lane | dashboard job OR the nightly/CLI lane | the nightly/CLI lane MAY run the two chains as TWO OS PROCESSES for hard process isolation | — |

**HONEST SHARED RESIDUALS (disclosed, never claimed away — Golden
Rule #10):**

1. **The physical disk volume** — an ENOSPC `LakeWriteHalt` hits BOTH chains
   (one volume, Decision 1 rejected alternative 3 accepts this trade).
2. **Host CPU / RAM** — overlapping prebuilds and pulls share the box; the
   hardware envelope (CLAUDE.md §8) is not partitioned per vendor.
3. **The dashboard process** — if both jobs run in-process, a process crash
   kills both chains. The two-process nightly/CLI lane escapes this
   (WS-E).
4. **The Greeks CPU pool** — if the two chains' Greeks passes overlap they
   share the pool sizing (`greeks_pool_workers`, CLAUDE.md §8).
5. **Python's GIL** — both pulls are I/O-bound; polars/pyarrow release the
   GIL; the concurrent `_crossverify_job` beside a live pull is the in-tree
   precedent (DEC-CROSSVERIFY-PRODUCER-001). Not zero, disclosed.

---

## 7. Decision 6 — Groww options-prebuild RETIREMENT (RULING B; the ruled implementation item this track owns — WS-A)

**Today:** the pipeline's terminal prebuild MATERIALIZES derived timeframes
for OPTION contracts (DEC-PIPELINE-AUTOPREBUILD-001 —
`discover_option_contracts` + the options arm of the contract phase).

### 7.1 The current options wiring sites (verified 2026-07-15 @ `origin/main` d6ac9f8)

1. **Sequential pipeline path** — `run_pipeline_prebuild`
   (`brutex/dashboard/pipeline_prebuild.py:262`): option-contract discovery
   at :394 (`options = _first_seen(discover_option_contracts(inst,
   cascade))`) and the binding into the gated spine at :469-472
   (`options_contracts_for=` / `materialize_option=` →
   `PrebuildKind.OPTIONS`).
2. **Parallel pipeline path** — `run_pipeline_prebuild_parallel`
   (`pipeline_prebuild.py:588`): the discovery loop at :731 building
   `options_items`, and the `PrebuildKind.OPTIONS` phase group at :765-768.
3. **The gated spine** — `run_gated_prebuild`
   (`brutex/dashboard/gated_prebuild.py:526`): the options callables default
   `None` (:532-533) and the injection gate at :600-613 already routes
   `None` to `_run_options_noop_phase` (:508) — a REPORTED no-op, never a
   silent skip. `discover_option_contracts` is defined at :184.
4. **Entry points** — `run_pipeline_prebuild_guarded`
   (`pipeline_prebuild.py:860`), invoked from the dashboard pipeline
   (`brutex/dashboard/app.py:4969`) and the nightly headless binding
   (`brutex/nightly/pipeline_binding.py:346`). Neither entry passes options
   callables directly — they call the guarded pipeline entry, which wires
   options internally at sites 1-2. (No other production caller wires the
   options PREBUILD — grep-verified; the heal RECALL below is a distinct
   producer path, not a prebuild wiring.)
5. **The lake-heal derived RECALL is a SECOND production writer of derived
   option TFs** (review fold — the original audit missed it):
   `lake_heal._run_recall` (`brutex/data/lake_heal.py:1459`) re-derives the
   `1day` partition for EVERY symbol whose 1-minute was healed with NO kind
   gate on the DAILY arm — `derive_daily_for_targets` (:1493) runs for FNO
   CE/PE contracts too (`include_oi=t.segment=="FNO"`, the positional F&O
   daily resampler); only the INTRADAY rebuild is CE/PE-gated
   (`_prebuild_kind_from_coords` → `None`, :1361; `_rebuild_tf_months`
   returns `(0, 0)` at :1414-1417 WITHOUT quarantine-moving the stale
   months). So after WS-A, an L2/L3 heal touching an option contract's
   1-minute would still CREATE/REWRITE a derived option `1day` — violating
   RULING B through a path the pipeline-scoped absence pin cannot catch.
   Two adjacent in-tree defects recorded for WS-A: (a) the
   `_prebuild_kind_from_coords` docstring's justification — "options
   intraday is never materialised — `materialize_lake_timeframes` raises
   for OPTIONS by design" (:1366-1368) — is STALE/FALSE:
   `timeframe_materialize` fully supports `PrebuildKind.OPTIONS`
   (`brutex/data/timeframe_materialize.py:19-21` docstring +
   `intraday_resampler_for` :236 routing FUTURES **and** OPTIONS to the
   OI-aware resampler) since DEC-PIPELINE-AUTOPREBUILD-001, and the module's
   ONLY shape raise (:620) is the `months` + `include_daily` refusal — no
   OPTIONS raise exists; (b) post-retirement RESIDUE: today a healed option
   contract's poisoned materialised intraday TF months are skipped by
   `_rebuild_tf_months` and rebuilt LAZILY by the NEXT pipeline options
   prebuild after `remove_derive_stamp` (:1483) — after WS-A nothing
   rebuilds them, so stored derived option intraday TFs would diverge
   PERMANENTLY from their healed 1-minute source while `/db` keeps
   listing/serving them (it lists physically-stored partitions). Both are
   resolved by the WS-A scope in §7.2.

### 7.2 The change (WS-A)

- The pipeline prebuild STOPS materializing options: both pipeline paths
  (sites 1-2) stop discovering/binding option contracts, so the gated
  spine's options phase takes the EXISTING reported-no-op arm (site 3) —
  with the no-op note reworded to carry the RULING B capability reason
  ("options are 1-minute-native by design — derived option TFs retired"),
  never the misleading "not injected" wording on a by-design retirement
  (the note-surgery precedent of DEC-PULL-SCOPED-ALL-ONECLICK-001).
- **The heal-recall daily arm joins the WS-A scope** (§7.1 site 5):
  `_run_recall`'s `1day` re-derive is GATED for CE/PE (or a conscious
  exemption is ledgered in the superseding DEC — the default is the gate:
  RULING B says options never get derived TFs, and the recall exists to
  keep DERIVED data coherent with healed 1-minute — for options the
  coherent state is ABSENCE); the stale `_prebuild_kind_from_coords`
  docstring (§7.1 site 5 defect (a)) is corrected in the same PR.
- A SUPERSEDING DEC entry (supersedes-in-part
  DEC-PIPELINE-AUTOPREBUILD-001 — the options arm ONLY; the spot+futures
  prebuild law stands).
- An **ABSENCE PIN test, WIDENED beyond the pipeline** (review fold): (i) a
  pipeline run over a lake holding option contracts writes NO derived
  option timeframe partitions (asserted on the tree, not on a report
  field), and the options phase report reads the by-design note; AND (ii)
  an L2/L3 HEAL touching an option contract's 1-minute writes NO derived
  option `1day`/TF partition (the §7.1 site-5 path — the pipeline-scoped
  pin alone cannot catch it).
- A **READER AUDIT recorded here** (re-verified against the tree; the
  review fold REFUTED-IN-PART the original "movers never read derived
  option TFs" claim — corrected below):
  - the movers options SESSION reads are 1-minute-native BY DESIGN — the
    locked as-of + Greeks-overlay semantics (DEC-FNO-TF-DERIVE-OI-001;
    in-tree markers: `brutex/dashboard/movers_options.py:48`,
    `movers_futures.py:839`, `movers_stored_read.py:41`) — the SESSION path
    never reads derived option TFs;
  - **BUT the movers options prev-day BASELINES DO read the derived option
    `1day` FIRST** (the review-fold correction): the per-contract
    `resolve_prev_close(... segment=FNO, symbol=<option contract>)` /
    `resolve_prev_oi(...)` calls (`movers_options.py:1367` + :1370) read
    the contract's derived `1day` partition as the PRIMARY path
    (`movers_snapshot.py:1279`; `movers_futures.py:988` + :1028), with the
    bounded 1-minute walk-back (`_prior_session_last_from_minute`,
    `_PREV_SESSION_MAX_BACK_DAYS=14` — `movers_snapshot.py:1209` + :1212)
    firing ONLY when the `1day` is absent or holds no prior bar —
    `movers_stored_read.py:47-52` documents the dependency verbatim.
    Consequences the disposition below must close: (A) CORRECTNESS — after
    retirement, a live option contract that HAD a `1day` at cutover keeps
    accreting 1-minute while its `1day` freezes; the resolver prefers a
    PRESENT-but-stale `1day` whenever it holds ANY prior bar, so the movers
    options screen would serve a FROZEN retirement-eve close as prev-close
    → wrong %CHNG for up to the contract's remaining life, flagged only by
    the DEC-MOVERS-PREVCLOSE-CONTIGUITY-001 " *" marker (which stamps,
    never corrects); (B) LATENCY — contracts born after retirement pay the
    per-contract multi-day 1-minute walk-back (≤14 calendar days probed,
    month-bounded reads) on every fresh candidate compute — values
    converge honestly; the cost is disclosed here, absorbed by the
    candidate cache between generations;
  - **the DOCTOR is a consumer the original audit missed** (review fold):
    `check_derived_freshness` (`brutex/doctor.py:359`) walks
    `bars/*/*/*` symbol dirs SORTED (:398) — FNO CE/PE contract dirs are IN
    that walk — and probes the FIRST symbol carrying both a `1minute` base
    and stored derived TFs, FAILing loudly on any drift from a fresh
    re-derivation. A post-retirement frozen option TF can seat that probe
    ⇒ a recurring loud doctor FAIL across the transition window (up to
    ~30 days per contract — the `expiry − 30d` window law). The
    disposition below must keep the doctor green: either the stored option
    TFs are disposed of, or CE/PE symbol dirs are excluded from the
    freshness walk under the superseding DEC (with its own pin);
  - the ENGINE never reads options (CLAUDE.md §2 Rule 3 — F&O lake is
    REFERENCE_ONLY; DEC-ENGINE-SPOT-ONLY-001);
  - crossverify is 1-minute-only by law (DEC-CROSSVERIFY-PRODUCER-001);
  - `/db` lists only PHYSICALLY-STORED partitions (discovery walks the
    lake, `brutex/dashboard/db_view.py:1219` + :1239) — an absent derived
    option TF simply does not appear; no fabrication risk. A PRESENT frozen
    one, however, keeps being listed/served — another reason the
    disposition below is not optional.
- **Already-stored derived option TFs (written by the pre-ruling prebuild)
  are NOT zero-risk to leave inert UN-GATED** (the review fold falsified
  the earlier "leave INERT — zero risk" recommendation; Q1 rewritten
  accordingly). WS-A must ship ONE disposition — the DESIGN'S
  RECOMMENDATION is (a); the operator decides at Q1:
  - **(a) RECOMMENDED — reader-gate + leave the stored TFs inert:** gate
    the movers baseline resolvers
    (`resolve_prev_close` / `resolve_prev_oi`) to the 1-minute fallback
    for FNO CE/PE contracts — a READ-SIDE change, NO data destruction —
    leaving the stored bytes inert on disk, AND exclude CE/PE symbol dirs
    from the doctor freshness walk under the superseding DEC (with its own
    pin) so the doctor stays green. The frozen-baseline hazard (A) dies at
    the READ side. Trade-offs, disclosed: frozen bytes stay on disk
    permanently and `/db` keeps listing/serving them (a display-honesty
    residual over explicitly-stale derived data); two reader gates to pin
    instead of one migration step.
  - **(b) Quarantine-move (or delete) every option-contract derived-TF +
    derived-`1day` partition at retirement:** the hazard dies STRUCTURALLY,
    the doctor stays green with NO walk exclusion, and `/db` stops serving
    frozen TFs. Trade-offs: a destructive migration step — derived data is
    re-derivable from the stored 1-minute (nothing irreplaceable is
    destroyed; the quarantine-move keeps the house data-never-destroyed
    posture — the DEC-LAKE-HEAL-001 before-image pattern).
  - **(c) Freshness-check the `1day` before serving** (compare against the
    1-minute tail on every baseline resolve): the most moving parts + a
    per-read cost, and it leaves the doctor question unanswered; listed as
    an alternative only.
  - Leaving the bytes inert WITHOUT reader gating is REJECTED — it serves
    wrong %CHNG (A) and a recurring doctor FAIL.
- The Dhan side pins its own options-prebuild ABSENCE in its own PR (their
  file ownership; coordinated — and per the §1.3 COUNTERMAND their recorded
  G2/G3 TODO must first be corrected through the coordinator).

---

## 8. Decision 7 — G4 ruling: incrementality GATES the Dhan nightly (requested by the Dhan track)

**RULING (this design's):** incremental daily convergence on the Dhan
OPTIONS phase is **REQUIRED before any nightly/auto-armed Dhan chain**.

**Quota arithmetic** (re-derived at authoring time from the declared paging
+ quota shape, §1.3 item 3 — verify again at WS-D):

- Rolling-options whole-range re-issue: 1008 keys × ceil(1825 d / 30 d
  tiles) = 1008 × 61 tiles ≈ **61,000 requests ≈ 61% of the 100,000/day
  quota** — for ONE nightly re-pull, before spot and before any TickVault
  co-tenant usage of the same account budget.
- Even a 1-year-deep re-pull ≈ 1008 × 13 tiles ≈ **13,000 requests/night**.
- Dhan SPOT whole-range: 4 symbols × ceil(1825 / 90) = 4 × 21 ≈ **84
  requests** — interim-tolerable.

Hence: whole-range re-issue is tolerable for SPOT interim, **categorically
NOT for the nightly OPTIONS phase**.

**ACCEPTED by the Dhan track (2026-07-15, after this ruling was
communicated — the input fold):** they build `incremental_from` per rolling
key BEFORE any auto-arm. Their likely mechanism ORDER: (1) the cheap
lake-tail-derived `from_date` per key FIRST (walk each key's newest stored
partition → resume from its tail), with (2) the durable Groww-parity
window-ledger spine (their `DEC-FEED-SPINE-GENERALIZE-001`) as the LATER
upgrade; manual runs stay allowed with the request cost disclosed. The
sequencer:

- exposes a per-phase `incremental_from` seam (§12), and
- treats incrementality as a **CAPABILITY FLAG**: absent ⇒ the chain
  **refuses to AUTO-ARM** (scheduled/nightly lanes refuse loudly, naming
  this ruling); manual/operator-invoked runs stay ALLOWED with the request
  cost disclosed in the launch note.

---

## 9. Decision 8 — Scheduling

- **Groww:** market-hours guard ON, LOCKED — the shared Groww account with
  TickVault's live feed (CLAUDE.md §2 Rule 6;
  DEC-GROWW-MARKET-HOURS-GUARD-001 — unchanged). The Groww post-market
  auto-pull scheduler stays Groww-spot-exclusive
  (DEC-PULL-SCOPED-ALL-ONECLICK-001 lineage — unchanged).
- **Dhan:** NOT bound by the market-hours guard (its own account;
  `market_closed=None` in the Dhan stack — the Dhan track's declaration,
  §1.3 item 8). Any Dhan-side pull-window or intra-day quota-budget policy
  is a NEW decision — surfaced as sign-off question Q2. Context for the
  operator: the Dhan token is TickVault-minted per the SSM layout
  (§1.3 item 7), i.e. account CO-TENANCY with TickVault's Dhan usage
  exists, and the 100k/day quota is the SHARED budget. Default per the
  current ruling: NO Dhan guard.
- **Dhan auto-arm** is additionally BLOCKED by Decision 7 until
  incrementality lands.

---

## 10. Observability + ordering enforcement

Strict ordering is enforced **STRUCTURALLY** — the sequencer is sequential
code; a stage cannot start before its gate passes (there is no scheduler to
misorder) — and **OBSERVABLY**:

- NEW `pipeline.stage.started` / `pipeline.stage.settled` /
  `pipeline.stage.halted` structlog events carrying `vendor=` + `stage=`
  tokens (the `pull.step.*` naming family, DEC-PULL-SIX-STEP-ORDER-001).
- The chain logs its capability-derived PLANNED stage list at start (so an
  absent capability is visible as "not on this vendor's graph", never a
  silent hole).
- The per-vendor `stopped_after` vocabulary extends with the staged names —
  INCLUDING `spot_daily` (RULING C: a native-daily failure reads
  `stopped_after=spot_daily`, observably BEFORE any 1-minute vendor call —
  the §1.1 sequencer ruling's own-gate/own-token requirement).
- Per-vendor pull-progress job surfaces (already job-scoped —
  `BackgroundPullJob` snapshots). Precision (review fold): job-scoped
  covers the SNAPSHOTS only — the single-slot rate panel /
  `/api/v1/pull/rate` / health-hub pull lane stay Groww-slot surfaces, on
  which the Dhan chain is DARK in v1 (the Decision 5 observability row +
  its WS-D never-register pin; per-vendor surfaces land in WS-E).
- The intra-spine six-step machinery (PullStep / StepOrderMonitor /
  `pull.step.*` events) is retained PER VENDOR unchanged.
- ONE `BRUTEX_PIPELINE_STRICT_GATE` applies to BOTH vendors in v1
  (`pipeline_pull.py:271`); a per-vendor override is deferred, named.

---

## 11. Failure semantics

- A stage callable raising a vendor-halt exception (the vendor's
  `_GLOBAL_STOP_ERRORS` family — e.g. `MarketHoursHalt` on Groww; the Dhan
  quota/JWT halts on Dhan) halts THAT vendor's chain at that stage:
  `stopped_after=<stage>`, later stages skipped, `pipeline.stage.halted` +
  CRITICAL alert. The peer chain is structurally unaffected.
- `LakeWriteHalt` (disk-full/OOM at a write boundary) is NEVER swallowed by
  a stage callable (§12 invariant) — it halts the raising chain; because
  the volume is shared (Decision 5 residual 1), the peer chain will halt on
  ITS next write too — an honest shared-fate case, disclosed not designed
  away.
- A gate failure (report not ok) is a NON-exception stop: the chain
  finishes with `ok=False` + the failed stage named — the
  `historical_phase_ok`-family semantics (`pipeline_pull.py:149-218`)
  generalized per stage.
- Mid-chain auth expiry: handled INSIDE the vendor spine (Groww re-read;
  Dhan single-flight JWT re-read, §1.3 item 7) — the sequencer sees either
  a healed spine or a vendor-halt, never a shared failure path.

---

## 12. Sequencer interface sketch (FOR THE VENDOR-FEED TRACKS — the Dhan session implements against this)

Additive staged params on the ONE sequencer (all optional, all defaulted so
the legacy chain is byte-identical):

```python
def run_full_pipeline(
    *,
    run_spot: Callable[[], BulkSpotReport],
    run_metadata: Callable[[], MetadataPullReport] | None = None,
    run_historical: ... ,                    # legacy combined F&O phase (default chain)
    run_prebuild: ... | None = None,         # legacy TERMINAL prebuild (default chain)
    # NEW staged callables (None = stage ABSENT from this vendor's graph;
    # each returns a StageOutcome-satisfying report — the structural
    # protocol in invariant 4 below):
    run_spot_daily: Callable[[], StageOutcome] | None = None,   # RULING C: native-daily stage (daily_source=native binds it)
    run_prebuild_spot: Callable[[], PrebuildOutcome] | None = None,
    run_futures: Callable[[], CompositePullReport] | None = None,   # F&O spine, step_filter={INDICES_FUTURES,...}
    run_prebuild_futures: Callable[[], PrebuildOutcome] | None = None,
    run_options: Callable[[], CompositePullReport] | None = None,   # per-contract (Groww) or rolling (Dhan)
    run_greeks: Callable[[], GreeksEnrichOutcome] | None = None,
    vendor: str = "groww",                   # the stage-event token
    ...
) -> PipelinePullReport: ...
```

```python
@dataclass(frozen=True)
class VendorPullProfile:
    vendor: str                              # "groww" | "dhan"
    write_domain: Path                       # primary root | <root>/feeds/dhan
    capabilities: VendorCapability           # {spot, futures?, options_shape, metadata?, daily_source}
    daily_source: Literal["native", "derived"]  # RULING C — native binds run_spot_daily FIRST
    history_floor: Callable[[date], date]    # adapter-owned depth law
    paging: PagingProfile                    # adapter-owned chunk/tile law
    rate_factory: Callable[[], SessionRateController]   # ONE per chain
    quota_factory: Callable[[], DailyQuotaTracker] | None
    reauth_binding: ...                      # adapter-owned SSM re-read machinery
    account_key_resolver: Callable[[], str | None]
    lock_domain: LockDomain                  # trio-at-primary-root | feed-root flock
    market_hours_guard: Callable[[], bool] | None       # Groww: ON; Dhan: None
    incrementality: IncrementalityCapability | None     # None ⇒ auto-arm REFUSED (Decision 7)
    greeks_seam: Callable[..., GreeksEnrichOutcome] | None
    ledger_markets: tuple[str, ...]          # ("groww", ...) | ("dhan", "dhan-option")
```

Per-phase incrementality seam: each pull-stage callable factory receives
`incremental_from: Callable[[PhaseKey], date | None] | None` — `None` means
the vendor has NO incrementality capability (Decision 7 consequences apply);
a callable returning `None` for a key means "no prior coverage — pull from
the floor".

**Sketch disambiguations (review fold — the rules the Dhan session would
otherwise have to guess):**

- **Legacy XOR staged, fail-loud:** the legacy combined params
  (`run_historical` / terminal `run_prebuild`) and the NEW staged params
  (`run_futures` / `run_options` / `run_prebuild_*` / `run_greeks`) are
  MUTUALLY EXCLUSIVE — a caller passing members of BOTH families gets a
  typed refusal at call time (the fail-loud junk-mix law; no precedence, no
  silent winner). Legacy-only callers are byte-identical (Decision 3);
  staged-only callers get the staged graph.
- **Per-stage gate composition:** each staged callable gets its own gate in
  the canonical order (§3.3), generalizing the `spot_phase_ok` family; the
  ONE `BRUTEX_PIPELINE_STRICT_GATE` env applies per stage on BOTH vendors
  in v1 (§10) — the strict-gate verdict threading for Greeks follows the
  §4.1 supersession-3 choice.
- **One-line field sketches for the named types** (full shapes are WS-B/
  WS-C deliverables): `PhaseKey` = a frozen `(vendor, stage, instrument?)`
  token — the key `incremental_from` resolves coverage for;
  `VendorCapability` = a frozen set-like declaration
  `{spot: bool, futures: bool, options_shape: per_contract|rolling_atm|None,
  metadata: bool, daily_source: native|derived}`; `LockDomain` = the
  vendor's lock recipe
  (`trio_at_primary_root` | `feed_root_flock(path)`); `PagingProfile` = the
  adapter's chunk/tile law (`max_days_per_request` per phase kind);
  `IncrementalityCapability` = HOW `incremental_from` is derived
  (`durable_ledger(markets)` | `lake_tail_scan`) — `None` = absent
  (Decision 7).

**Invariants every vendor spine must honor (the contract this design
enforces at review time):**

1. ALL bytes land inside the vendor's lake WRITE DOMAIN (Decision 1).
2. ONE rate controller (+ quota tracker where declared) threaded across ALL
   of that vendor's phases — never shared with, or blended into, the peer
   vendor's.
3. Halts are HONEST typed exceptions (the vendor's global-stop family);
   stage callables NEVER swallow `LakeWriteHalt`.
4. **The stage-callable contract is a STRUCTURAL protocol, NOT the concrete
   Groww report types** (the Dhan track's interface improvement, adopted at
   the 2026-07-15 input fold): each stage callable returns an object
   satisfying a `StageOutcome`-shaped protocol naming the REQUIRED
   fields/semantics — an `ok` verdict, failed/still-failed counts, halt
   propagation (a vendor-halt / `LakeWriteHalt` is RAISED, never swallowed
   into the outcome — invariant 3), and a stopped-reason token. Groww's
   existing report shapes (`BulkSpotReport` / `MetadataPullReport` /
   `CompositePullReport` / `PrebuildOutcome` / `GreeksEnrichOutcome`)
   already satisfy it, so the sequencer's gate predicates (`spot_phase_ok`
   family, `pipeline_pull.py:149-218`) generalize over the protocol
   unchanged for Groww; Dhan's stage reports are `DhanWindowOutcome`-based
   (NOT `BulkSpotReport`) and satisfy the protocol directly or via THIN
   adapters. Their Dhan Greeks CLI pass is exposed as a callable seam
   returning a gate-compatible outcome shape (their commitment, §1.3
   item 9).
5. Stage-event tokens (`vendor=`, `stage=`) are emitted by the SEQUENCER,
   not re-minted per spine (§10).

---

## 13. Honest limits (Golden Rule #10)

- **Design-only.** Nothing in this ADR is implemented or verified-by-run;
  every "byte-identical default" claim is a WS-B test obligation, not a
  present-tense fact.
- The Dhan contract rows (§1.3) are the Dhan track's DECLARATIONS; the two
  named memory-file sources were absent at the authoring commit and landed
  ~7 minutes later — they are NOW cited as the primary declared evidence
  base (timeline + deltas recorded in §1.3, incl. the RULING-B/A
  countermand of their G2/G3 TODO). Still THEIR declarations — re-confirm
  at WS-D.
- Dhan TRUE 1-minute depth is UNVERIFIED (docs-vs-SDK contradiction; probe
  pending) — the rolling floor law may over-promise; the sequencer degrades
  honestly (adapter-owned floor resolver). Dhan INDIA VIX availability +
  securityId are likewise UNVERIFIED (§1.3 item 10 — probe-gated, honest
  3-symbol degrade).
- RULING C's Dhan native-daily depth ("instrument inception") is the ruled
  vendor shape as confirmed by the Dhan track — like every §1.3 row it is
  re-confirmed at WS-D; the tolerance-banded OBSERVE-ONLY daily-vs-1min
  consistency check is the Dhan track's dedicated-PR deliverable (theirs,
  unlanded), not this design's.
- The WS-A retirement carries REAL reader consequences until its
  disposition ships (§7.2): the movers options prev-day baselines read the
  derived option `1day` FIRST, and the doctor's freshness walk covers CE/PE
  dirs — "leave stored option TFs inert, zero risk" is FALSE (Q1
  rewritten).
- The shared residual failure domains (Decision 5) are real: disk volume,
  host CPU/RAM, in-process dashboard co-residency, Greeks pool, GIL.
- The quota arithmetic (§8) uses the DECLARED tile/quota shape; a vendor
  change moves the numbers — the ruling's structure (incrementality gates
  auto-arm) survives, the percentages may not.
- `feeds/dhan/**` is invisible to every primary dashboard/census surface
  until explicit read surfaces land — an accepted, named gap.
- ONE strict-gate env for both vendors in v1 (§10) — a per-vendor override
  is deferred.

---

## 14. Out of scope

- Dhan adapter internals (sibling track).
- Vocabulary / engine / fills — untouched (ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; CLAUDE.md §2 Rule 3).
- Stock-universe removal (sibling track).
- Dhan-as-engine-input / failover (future DEC — Q4).
- Per-vendor disk volumes.
- Dashboard read surfaces over the Dhan domain (named follow-up).

## 15. Prerequisites

1. The `no-dhan-guard` retirement is **HANDLED BY THE DHAN TRACK'S
   LINEAGE — this track does NO guard work** (input fold, 2026-07-15;
   supersedes this section's earlier "joint prerequisite" framing): the
   retirement code shipped in their club member #1492 under the umbrella
   `DEC-DHAN-SPOT-FEED-001` (their club entry #1797 sub-item (e); the
   retirement's own ledger entry lands as their #1801) — cited
   `DEC-DHAN-GUARD-RETIRE-002` (backticked; their lineage, not on main
   yet). By the time orchestration code lands in `brutex/`, club PR #1551
   will have retired the gate. This ADR itself is guard-safe under
   `docs/adr/` (the guard's scan scope, §1.4).
2. Coordinator reservations: the MIGRATION.md entry number for
   `DEC-DUAL-VENDOR-PIPELINE-001` + the ADR-0021 slot (this file).
3. The Dhan track's contract values landing via club PR #1551 + follow-ups.

## 16. Open questions for operator sign-off (numbered, answerable)

- **Q1 (REWRITTEN at the review fold; recommendation set at the input
  fold — the earlier "leave INERT, zero risk" was FALSE):** stored derived
  option TFs already on disk feed the movers prev-close/OI baselines
  `1day`-FIRST and sit in the doctor's freshness walk (§7.2) — leaving
  them inert UN-GATED serves a FROZEN retirement-eve prev-close (wrong
  %CHNG until contract expiry) and a recurring doctor FAIL. Choose the
  disposition: **(a) gate the baseline resolvers to the 1-minute fallback
  for FNO CE/PE + exclude CE/PE from the doctor freshness walk, leaving
  the stored bytes inert (RECOMMENDED — read-side, no data destruction;
  disclosed residuals: frozen bytes stay on disk and `/db` keeps listing
  them)** vs (b) quarantine-move/delete the option-contract derived
  partitions at retirement (the hazard dies structurally; re-derivable
  from stored 1-minute) vs (c) freshness-check the `1day` before serving
  (per-read cost; listed only)?
- **Q2** — Any Dhan-side pull-window / intra-day quota-budget policy,
  despite no inherited market-hours guard? (Context: TickVault co-tenancy
  on the Dhan account + the shared 100k/day budget, §9.) Default: none.
- **Q3** — Confirm the Decision 7 ruling: incrementality gates AUTO-ARM;
  manual runs stay allowed with cost disclosed.
- **Q4** — Confirm Dhan-as-engine-input/failover stays OUT of scope (a
  future DEC if ever).
- **Q5** — Confirm the sequencer surface = ADDITIVE extension of
  `run_full_pipeline` (Decision 3) vs a new module.

## 17. Implementation plan (workstreams; branch-now / land-after-coordination per the merge train)

- **WS-A (RULED, first PR after this design lands):** Groww
  options-prebuild retirement + the heal-recall CE/PE daily-arm gate + the
  stale `_prebuild_kind_from_coords` docstring correction + the WIDENED
  absence pin (pipeline AND heal paths) + the stored-option-TF disposition
  (Q1) keeping the movers baselines honest and the doctor green + the
  reader audit + the superseding DEC entry (§7).
- **WS-B:** staged sequencer seams — additive `run_full_pipeline` params
  (incl. `run_spot_daily`, RULING C) + the `StageOutcome` structural
  protocol (§12 invariant 4) + the F&O spine `step_filter`; byte-identical
  defaults PINNED.
- **WS-C:** `VendorPullProfile` / capability registry (incl.
  `daily_source`) + per-vendor job slots + per-vendor guard binding.
- **WS-D:** the Dhan chain binding against the profile (with the Dhan
  track): feed-root derive-lock domain (VERIFIED-AS-BUILT by the Dhan
  track, §3.3 — WS-D consumes their pin, it does not re-verify),
  `run_spot_daily` + `incremental_from` seams, greeks seam.
- **WS-E:** nightly/CLI dual-vendor lanes (two-process isolation) +
  stage-event observability surfaces.

## 18. References

- Operator directives + RULINGS A/B/C — 2026-07-15, via coordinator + the
  Dhan feed track (§1.1; RULING C — the Dhan native daily — arrived
  post-authoring and was folded the same day at the input fold, together
  with this design's own sequencer ruling on it).
- The Dhan track's 2026-07-15 input-fold answers: the feed-root nesting
  confirmation (Decision 1) · the derive-lock VERIFIED-AS-BUILT report +
  pin name (§3.3 / Decision 5) · the guard-retirement lineage (§15
  prerequisite 1) · the Decision 7 acceptance + mechanism order · the
  structural stage-outcome protocol (§12 invariant 4).
- The Dhan adapter contract — the Dhan feed track's declarations,
  2026-07-15; club PR #1551, `integration/club-x-dhan` @ `b6341fc3`; the
  team memory files `dhan-api-verified-facts.md` + `dhan-track-state.md`
  (absent at the authoring commit, landed ~7 min later — cited since the
  same-day review fold; timeline + deltas in §1.3).
- CLAUDE.md §2 Rules 3/4/6, §6 (Groww verified facts), §8 (hardware
  envelope).
- DEC-PIPELINE-AUTOPREBUILD-001 (the terminal prebuild being
  superseded-in-part) · DEC-PREBUILD-CLEANSET-001 ·
  DEC-LAKE-FOOTPRINT-MEASURED-001 · DEC-PULL-SIX-STEP-ORDER-001 ·
  DEC-LAKE-PARTITION-FLOCK-001 · DEC-ACCOUNT-KEY-SINGLEFLIGHT-001 ·
  DEC-ACCOUNT-LEASE-CROSSHOST-001 · DEC-GROWW-MARKET-HOURS-GUARD-001 ·
  DEC-GROWW-RATE-PERMIN-001 · DEC-GROWW-RATE-CEILING-8-002 ·
  DEC-AWS-ONLY-CREDS-001 · DEC-FNO-TF-DERIVE-OI-001 ·
  DEC-CROSSVERIFY-PRODUCER-001 · DEC-ENGINE-SPOT-ONLY-001 ·
  DEC-SNAPSHOT-HASH-FULL-001 · DEC-BACKUP-OFFBOX-001 ·
  DEC-SWEEP-NIGHTLY-AUTOMATION-001 ·
  DEC-PULL-TODAY-COMPLETENESS-GATE-001 ·
  DEC-CLI-GREEKS-ENRICH-001 · DEC-GREEKS-CORRECTNESS-AUDIT-001 ·
  DEC-PULL-SCOPED-ALL-ONECLICK-001 · DEC-PREBUILD-CLEANSET-001 ·
  DEC-MOVERS-PREVCLOSE-CONTIGUITY-001 · DEC-LAKE-HEAL-001 ·
  DEC-RATE-METER-LIFECYCLE-001 · DEC-OPERATOR-HEALTH-TRUTH-001 ·
  DEC-1MINUTE-ONLY-DERIVE-1DAY-001 (now scoped per-vendor Groww-only,
  §4.1 supersession 5).
- Future / not-yet-landed ids (backticked by convention):
  `DEC-DUAL-VENDOR-PIPELINE-001` (this design, at sign-off) ·
  `DEC-FEED-SPINE-GENERALIZE-001` + `DEC-DHAN-ROLLING-FLOOR-001` +
  `DEC-DHAN-OPTIONS-NO-PREBUILD-001` + `DEC-DHAN-GUARD-RETIRE-002` +
  `DEC-DHAN-SPOT-FEED-001` (the Dhan lineage, not on main) ·
  `DEC-CI-OK-NODHAN-GATE-001` (the guard's ci-ok wiring citation).
- Verified source files: `brutex/dashboard/pipeline_pull.py` ·
  `brutex/dashboard/pipeline_prebuild.py` ·
  `brutex/dashboard/gated_prebuild.py` ·
  `brutex/dashboard/historical_pull.py` · `brutex/dashboard/pull_jobs.py` ·
  `brutex/dashboard/pull_controller.py` ·
  `brutex/adapters/groww/pull_singleflight.py` ·
  `brutex/adapters/ingest_common.py` · `brutex/data/partition.py` ·
  `brutex/data/partition_lock.py` · `brutex/data/parquet_writer.py` ·
  `brutex/data/window_ledger.py` · `brutex/data/lake_heal.py` ·
  `brutex/data/lake_invariants.py` · `brutex/data/today_completeness.py` ·
  `brutex/data/greeks_enrich_pass.py` · `brutex/calendars/market_hours.py` ·
  `brutex/config/paths.py` · `brutex/nightly/pipeline_binding.py` ·
  `brutex/dashboard/pull_scope.py` · `scripts/ci/no-dhan-guard.sh` ·
  (review fold) `brutex/dashboard/movers_options.py` ·
  `brutex/dashboard/movers_snapshot.py` ·
  `brutex/dashboard/movers_futures.py` ·
  `brutex/dashboard/movers_stored_read.py` · `brutex/doctor.py` ·
  `brutex/data/timeframe_materialize.py` · `brutex/data/greeks_enrich.py` ·
  `brutex/data/greeks_provenance.py` · `brutex/nightly/backup.py` ·
  `brutex/backup/plan.py` · `brutex/dashboard/app.py`.
