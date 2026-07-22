# ADR-0021 — Capability-Driven Dual-Vendor Pull-Pipeline Orchestration (Groww ∥ Dhan)

- **Status:** ACCEPTED (2026-07-16 — the operator landed the ADR on main via
  PR #1555, personally marking it ready-for-review and updating the branch,
  and gave direct approval in the orchestration session ("accepted",
  2026-07-16). Previously: PROPOSED (2026-07-15).)
- **Date:** 2026-07-15
- **Decision ID (reserved, not yet ledgered):** `DEC-DUAL-VENDOR-PIPELINE-001`
  — the MIGRATION.md entry lands only AFTER operator sign-off, with a
  coordinator-reserved entry number. This ADR file is the design record; the
  ledger row and the CLAUDE.md blurb land with the implementation PRs.
  — LEDGERED 2026-07-16 as MIGRATION.md #2011 (the Status-ACCEPTED
  paperwork PR beside this Status flip).
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
inception — the ~5y rolling cap is INTRADAY-only (§1.3 item 5). (The
ruled default names the REQUEST shape; the window END is CLAMPED to the
previous trading day by the EXTENDED P3 ruling — §9, the NG-1 fold — the
native-daily window included.) The Dhan
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
EXPECTS an options prebuild + a FUT node — COUNTERMANDED after item 9
(since RESOLVED at the feed-track review fold — see the restamp there);
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
   options floor unifies to `max(rolling 5y floor, the options-floor
   constant)` — where that constant is the REQUESTED / research-PARTIAL
   value, explicitly UNVERIFIED in their own tree (their
   `rolling_option_constants.py:165-178`; the earlier "measured options
   floor" wording here was WRONG — feed-track review fold), being
   implemented by them now. **TRUE 1-minute depth is UNVERIFIED**
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
plan contradicted it when this paragraph was written. **RESTAMP
(feed-track review fold, 2026-07-15): RESOLVED** — the "currently
contradicts" clause is STALE: their READY follow-up PR ships the
options-prebuild ABSENCE pins + the no-futures DECs (their attribution;
their lineage, unlanded on main), so their plan no longer contradicts the
rulings; the countermand stands as history with its coordination
obligation DISCHARGED.

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
  **WIRED at the GROWW spine ONLY** (assurance fold, CONC-02 — verified:
  `today_completeness_gate=_today_gate` at
  `brutex/dashboard/pull_controller.py:2157` + `:3228`, resolved once per
  pull at :1817/:3034); no other spine consults it — the per-vendor
  consequence is PRECONDITION P3 (§15 item 6) + the Decision 4
  `today_completeness` profile field.
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
  tree. **SPOT-JOIN ROUTING (assurance fold, CAPA-01 — the earlier "spot
  lands FIRST in every vendor graph, so the join is satisfied" wording was
  a temporal-presence NON-SEQUITUR corrected here):** the spot join needs
  the READER to be BOUND to the vendor's own lake domain, not merely data
  to be present there — the join reader is SINGLE-LEVER, hard-anchored at
  the primary tree (`spot_close_by_ts_window`,
  `brutex/data/spot_join.py:83`, probing
  `lake_root / "bars" / <exch> / <segment> / …` at :113 with no domain
  routing), so a second-vendor Greeks pass would silently join against
  GROWW spot today. The fix is REQUIRED before any second-vendor Greeks
  binding: route the reader by the profile's domain + stamp a
  vendor/spot-source provenance bit + the Pin D1-3 runtime read-domain
  assertion (the assurance re-review fold, NG-5) — PRECONDITION P4
  (§15 item 7; assurance-register pointer VCA-011).
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
state; §1.4 backup row — ELEVATED at the assurance fold from a WS-D note
to the NUMBERED Pin D1-2, §2.5).

### 2.4 Honest costs

- Extra disk for the overlapping spot span (bounded: 4 symbols, Dhan ~5y of
  1-minute).
- The Dhan subtree is INVISIBLE to existing dashboards until a CONSUMED
  read surface lands (their `dhan_rolling_db_view.py` exists on their
  lineage as an UNCONSUMED /db first slice — feed-track review fold; a
  named follow-up, not this ADR).
- Per-domain heal/census wiring for `feeds/dhan/**` is the Dhan track's
  follow-up (the primary L3 heal/census deliberately does not descend into
  `feeds/`).

### 2.5 Sidecar disposition table + numbered pins (assurance fold, 2026-07-15)

**The ONE construction law, stated once:** the Dhan job, launcher, prebuild
and Greeks bindings are ALL constructed with
`lake_root = <primary>/feeds/dhan/` — so every `lake_root`-keyed sidecar
NESTS per-domain BY CONSTRUCTION (the assurance review's ISOL-03 downgrade
MAJOR→MINOR rests on exactly this: job-resolved-root sidecars are already
per-domain; only the two rows below marked nest-or-vendor-token were
excepted). Per-sidecar disposition:

| Sidecar / surface | Disposition |
|---|---|
| `bars/` + catalog + `.manifest/` + `.locks/` | **NEST** — verified per-domain (the Dhan track's feed-root nesting law, §2 binding invariant; lock disjointness Decision 1 rationale (c)) |
| window ledger | **SEPARATE** under `feeds/dhan/window_ledger/` with `market="dhan"`/`"dhan-option"` tokens (§2.3) — non-re-pullable state; **Pin D1-2** below |
| `.rate` (the CLI rate bridge, `<lake_root>/.rate/pull_rate.json`) | **nest-or-vendor-token** — a Dhan chain writes it (if ever) under its OWN root, never the primary bridge file |
| `.pull_intent` (`<lake_root>/.pull_intent.json` — `PULL_INTENT_FILENAME`, `brutex/dashboard/pull_intent.py:120`; `PullIntentStore` :306/:327) | **nest-or-vendor-token, with the ISOL-02 v1 RULING (assurance fold):** the sidecar is a ROOT-SINGLETON with NO vendor field (verified — `PullIntent` carries symbols/symbol/from_date/to_date/restarts/created_at/scope only), previously OMITTED from this ADR's sidecar enumeration; a Dhan launch reusing the primary-root launcher would CLOBBER a running Groww intent and a crash+boot would replay the WRONG vendor's scope. **RULING: v1 = the Dhan chain runs with NO intent store** — no boot-resume; manual re-run is the recovery (consistent with the G4 manual-runs posture, §1.3 item 6 / Decision 7); if/when Dhan gains resume-intent it gets its OWN `PullIntentStore` under `feeds/dhan/`. A TWO-VENDOR CRASH-RESUME pin is REQUIRED in WS-C/WS-D: a crash+boot with a Groww intent present resumes the GROWW scope only and never fires a Dhan launch (and the Dhan v1 chain, having no store, resumes nothing). **LAUNCHER GATING (assurance re-review fold, NG-2 — BLOCKER: the ISOL-02 ruling pinned the CHAIN's no-intent-store but NOT the shared LAUNCHER):** `persist_intent_for_launch` (`brutex/dashboard/pull_intent.py:481`, invoked by the shared dashboard launcher at `brutex/dashboard/app.py:5257`) writes the vendorless ROOT-SINGLETON **BEFORE the vendor graph runs** — a Dhan launch through that launcher would persist a Groww-SHAPED boot-resume intent regardless of the chain having no store (a crash+boot replays the WRONG vendor/scope). FIX: the PROFILE gates the LAUNCHER, not just the chain — `VendorPullProfile` gains **`no_intent_persist`** (Groww `False`, Dhan v1 `True`; Decision 4) and the launcher consults it BEFORE the persist: a `no_intent_persist` profile's launch NEVER invokes `persist_intent_for_launch` (and correspondingly wires no clear/restore hooks against the primary singleton). **MECHANISM (fold #4, NG2-EDGE-LAUNCH-MUTEX-COUPLING):** gating only the persist CALL is INSUFFICIENT — the shared launcher ENTERS the primary store's `launch_mutex` on `pull_intent_store is not None` (`app.py:5243`, held across `start()` INCLUDING the SSM credential read + lease enter/refresh — the ≈10 s held-span I/O envelope the launcher's own J-NIT-1 comment discloses) — so a `no_intent_persist` launch binds NO primary intent store at all: it reaches the launcher with `pull_intent_store=None`, structurally skipping mutex + persist + clear/restore hooks + banner; a Dhan launch can never CONTEND on the primary launch mutex (the zero-cross-impact requirement). The WS-C pin is driven through the REAL shared launcher — never a chain-only fake — asserting BOTH: the PRIMARY root's `.pull_intent.json` singleton is byte-UNTOUCHED by a Dhan launch (both arms: a prior Groww intent PRESENT survives byte-identical; no intent present ⇒ none is created) AND the primary launch mutex is NEVER ENTERED by it (fold #4). **P1 COUPLING (disclosed):** once P1 lands (§15 item 4), a Dhan pull is no longer account-flock-excluded from a concurrent Groww pull — a Groww crash+boot-resume could then read Dhan-written scope if persist defaulted ON; the two-vendor crash-resume pin CANNOT hold while persist defaults on, so the launcher gate is a companion requirement of P1, not polish |
| `.greeks_backfill` (checkpoint sidecar) + the `groww.vendor_floor.*` schema_meta rows | **N/A-for-Dhan** (Groww-shaped surfaces; the Dhan chain never touches them) |
| the Greeks SPOT-JOIN root | **the ONE surface that does NOT nest for free** — the reader is hard-anchored at the primary `bars/` tree (`spot_join.py:83`/:113) and must be ROUTED by the profile's domain (the CAPA-01 fix — PRECONDITION P4, §15 item 7) — and guarded at RUNTIME by **Pin D1-3** (the read-domain assertion, NG-5): routing binds the right reader, the assertion REFUSES the wrong one |

**Numbered pins:**

- **Pin D1-1 (RUNTIME write-domain refusal — the ISOL-04 promotion; a
  WS-B/WS-C requirement; mechanism CORRECTED at the assurance re-review
  fold, NG-3):** §12 invariant 1 is PROMOTED from review-time to
  a RUNTIME fail-loud pin — every staged write dest's realpath is resolved
  against the active profile's `write_domain` — the **ALLOWED-SUBTREE SET
  with FOREIGN-DOMAIN EXCLUSION** (the Decision 4 typing law): the dest
  must fall under one of THIS profile's subtrees AND under NO OTHER
  registered profile's — and a cross-domain write
  REFUSES with a typed exception (never a keep-first merge): a
  `feeds/dhan/**` profile landing under the primary `bars/`/`options/`
  trees, and vice-versa. **MECHANISM CORRECTION (NG-3):** a bare
  realpath-STARTSWITH check against a single primary-root `Path` is
  INSUFFICIENT — `feeds/dhan/**` NESTS under the primary root, so such a
  check FALSE-PASSES a feeds/dhan dest presented to the PRIMARY profile
  (nested roots are not disjoint prefixes); the primary profile's domain
  excludes the ENTIRE `feeds/` path-component prefix STRUCTURALLY —
  registration-INDEPENDENT ("the root MINUS the whole `feeds/` subtree",
  NEVER merely the registered-`feeds/`-subtrees complement: that form
  would INCLUDE an UNREGISTERED `feeds/<other>/` dest in the primary
  domain and FALSE-PASS a primary-profile write there — the same
  nested-root false-pass class this correction closes; the §1.4
  invisibility argument — zero `feeds/` references in any primary
  scanner — already relies on exactly the whole-prefix exclusion)
  (fold #4, NG3-EDGE-UNREGISTERED-SUBTREE). In-repo precedent:
  `OptionParquetWriter._assert_options_containment`
  (`brutex/data/option_parquet_writer.py:317` — verified; the assurance
  brief's `lake_heal` attribution corrected: `lake_heal.py:1197` only
  cites it). Why RUNTIME: the primary partition grammar carries no vendor
  dimension (`brutex/data/partition.py` — verified, no vendor path/column)
  and the keep-first writer does NOT raise on a cross-domain dest
  (`brutex/data/parquet_writer.py:560-617` — the stored bar silently wins),
  so a mis-rooted writer would silently arbitrate, never crash. A
  NEGATIVE-PIN battery is REQUIRED: both directions + the typed refusal +
  the **NESTED-ROOT fixture** (a `feeds/dhan/**` dest presented to the
  PRIMARY profile must REFUSE — exactly the false-pass shape a
  prefix-check mechanism would let through) + the **UNREGISTERED-FEEDS
  fixtures** (fold #4, NG3-EDGE): a PRIMARY-profile write under
  `feeds/<anything>/` REFUSES whether or not that subtree is REGISTERED —
  the fixture dest lives under an unregistered `feeds/<other>/` — and a
  FEEDS-profile write under a FOREIGN `feeds/<other>/` subtree refuses
  too.
- **Pin D1-2 (nightly-backup allowlist):** when the Dhan window ledger
  lands under `feeds/dhan/window_ledger/`, the NIGHTLY backup allowlist
  `_LAKE_MEMBERS` (`brutex/nightly/backup.py:130-136` — verified:
  window_ledger · registry.duckdb · instruments.duckdb · catalog.duckdb ·
  audit) MUST gain it — non-re-pullable data is structurally unreachable
  to the allowlist lane otherwise (§1.4 backup row; elevated from a WS-D
  note at the assurance fold).
- **Pin D1-3 (RUNTIME read-domain assertion on the Greeks spot-join — the
  READ-side twin of Pin D1-1; assurance re-review fold, NG-5; folded into
  PRECONDITION P4, §15 item 7):** the Greeks spot-join binding asserts at
  RUNTIME that its resolved spot-reader ROOT lies inside the invoking
  profile's domain — a second-vendor Greeks pass handed a PRIMARY-domain
  spot reader REFUSES with a typed exception (mirroring the write-domain
  refusal), never a silent cross-vendor join. The containment test is the
  SAME allowed-subtree-set + foreign-exclusion law as Pin D1-1 (a nested
  `feeds/` root must not false-pass a prefix check). Negative pins
  REQUIRED both directions (a Dhan pass × a primary-domain reader; a
  primary pass × a Dhan-domain reader). This is the runtime BELT under
  P4's routing fix: routing binds the right reader, the assertion refuses
  the wrong one.

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
   │      2020-01-01 → today with the END CLAMPED to the previous trading
   │      day — the EXTENDED P3 ruling, §9/NG-1: a mid-session END=today
   │      daily fetch would freeze a FORMING 1day bar that, being NATIVE
   │      (include_daily OFF), nothing ever re-derives; Dhan NEVER
   │      derives 1day — RULING C.
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
    no movers warm — no CONSUMED dashboard read surface for the Dhan
    domain yet: their `dhan_rolling_db_view.py` exists on their lineage
    as an UNCONSUMED /db first slice — feed-track review fold)
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
  is no CONSUMED dashboard read surface over the Dhan domain yet (§2.4).

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
- **The ABSENT-vs-FAILED stage contract (assurance fold, CAPA-02 — a real
  spec hole, now SPECIFIED):** an ABSENT stage (capability unbound /
  `None` callable) is **NEVER INVOKED** and scores **VACUOUSLY-SETTLED** —
  `ok=True` with a DISTINCT `absent` token, never conflated with `failed`;
  the strict settle gate does NOT withhold Greeks over absent stages. WS-B
  must make the sequencer STRUCTURALLY skip absent stages — verified on
  this tree: `run_full_pipeline` calls `metadata = run_metadata()`
  UNCONDITIONALLY (`brutex/dashboard/pipeline_pull.py:1172`, gated at
  :1188) and `step_settle_verdict` (:332) folds bare booleans, so an
  absent stage handed `False` would score "metadata unclean" — a
  FAILED-shaped verdict for a stage that does not exist on the vendor's
  graph. REQUIRED test: the Dhan-shaped PARTIAL-absent parity case —
  metadata + futures ABSENT; spot / prebuild / options / greeks PRESENT;
  Greeks NOT withheld; no crash — the existing all-None parity test does
  NOT cover it.
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
| Lake write domain (typed `WriteDomain` — an ALLOWED-SUBTREE SET with foreign-domain EXCLUSION; the NG-3 typing law below) | the primary subtrees (`bars/` + `options/` + the primary sidecars) — equivalently the root MINUS the ENTIRE `feeds/` subtree, a registration-INDEPENDENT structural prefix exclusion (fold #4, NG3-EDGE: the registered-subtrees-complement form would FALSE-PASS a primary write under an UNREGISTERED `feeds/<other>/`) | `{<lake_root>/feeds/dhan/**}` | orchestration invariant (Decision 1 + Pin D1-1; the WRITE law — read routing is per-consumer, guarded by Pin D1-3) |
| Capability set | `{spot, futures, options: per_contract, metadata}` | `{spot, options: rolling_atm}` | operator ruling A |
| Daily (`1day`) source | `derived` — SPOT-PREBUILD derives `1day` from stored 1-minute (DEC-1MINUTE-ONLY-DERIVE-1DAY-001, Groww-only per §4.1 supersession 5) | `native` — the vendor daily endpoint, depth to inception, runs FIRST as `run_spot_daily`; prebuild `include_daily` structurally OFF (the RULING C pinned invariant) | operator ruling C |
| History-floor resolvers | const `GROWW_HISTORY_FLOOR` = 2020-01-01 (CLAUDE.md §2 Rule 3) | rolling `today − 5y + 7d` + unified options floor (`DEC-DHAN-ROLLING-FLOOR-001`; TRUE depth UNVERIFIED) | adapter |
| Paging profile | interval-tier chunks (CLAUDE.md §6 chunk row) | 90d spot / 30d rolling tiles (§1.3 item 3) | adapter |
| Rate factory | ONE `SessionRateController`, 8 r/s ceiling + 500/min governor (DEC-GROWW-RATE-CEILING-8-002, DEC-GROWW-RATE-PERMIN-001) | ONE `SessionRateController` + `DailyQuotaTracker`, 5 r/s + 100k/day, no per-minute cap (§1.3 item 3) | adapter (numbers), orchestration (one-instance-per-chain law) |
| Auth / reauth binding | 06:00 IST daily token, read-only SSM re-read (DEC-AWS-ONLY-CREDS-001; CLAUDE.md §6 Auth row) | 24h rolling JWT, SSM re-read via their single-flight refresher (§1.3 item 7) | adapter |
| Account-key resolver | Groww `api_key` fingerprint (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001) | Dhan client-id-keyed — the TARGET binding, NOT shipped: WS-D PRECONDITION P1 (feed-track review fold; today their club jobs inherit the default GROWW-key resolver, `pull_jobs.py:154-177` — Decision 5 preamble) | adapter (P1 owned by the Dhan track) |
| Lock domain | account + lake-root + lease trio at the primary root | the ONE feed-root flock at `feeds/dhan/` | adapter (mechanism), orchestration (disjointness invariant) |
| Market-hours guard binding | ON, LOCKED (DEC-GROWW-MARKET-HOURS-GUARD-001) | `lambda: False` — the no-guard binding IS a `Callable[[], bool]`, NOT `None` (the assurance-fold type correction, CONC-01: the job ctor's type is non-Optional — `market_hours_guard: Callable[[], bool] = guard_active_default`, `pull_jobs.py:443` — so `None` would not even type-check; §1.3 item 8's `market_closed=None` is the SPINE's separate Optional predicate seam; any Dhan policy = NEW decision, Q2) | operator |
| Today-completeness binding (PRECONDITION P3) | the classifier GATE armed at the spine (DEC-PULL-TODAY-COMPLETENESS-GATE-001 — wired Groww-only today, `pull_controller.py:2157`/`:3228`) | `clamp_to_prev_session` v1 — EVERY Dhan pull window END (the NATIVE-DAILY `run_spot_daily` window INCLUDED — the NG-1 extension — beside the 1-minute spot/options windows) is CLAMPED to the previous trading day, derived from the BARE vendor-agnostic calendar back-walk (`previous_trading_session` with NO observed overlay for Dhan v1 — the §9 P3 ruling + the NG-4 mechanism constraint); threading the vendor-agnostic classifier through the Dhan spine is the NAMED upgrade path | operator ruling (assurance fold, CONC-02; extended NG-1/NG-4) |
| Launcher intent gating (`no_intent_persist`) | `False` — the shared launcher persists the boot-resume intent as today (DEC-PULL-INTENT-RESUME-001 lineage) | `True` v1 — the shared launcher NEVER invokes `persist_intent_for_launch` for a Dhan launch (the NG-2 launcher gate; §2.5 `.pull_intent` row — the profile gates the LAUNCHER, not just the chain); MECHANISM (fold #4, NG2-EDGE): the launch binds NO primary intent store — `pull_intent_store=None` at the launcher, structurally skipping mutex/persist/clear/restore/banner (the primary `launch_mutex` is entered on `pull_intent_store is not None`, `app.py:5243`, held across `start()` incl. ~10 s SSM/lease I/O — persist-call gating alone would still CONTEND on it) | orchestration (assurance re-review fold, NG-2) |
| Ledger / incrementality capability | durable window ledger, `market="groww"` family | ABSENT today (gap G4) — gates auto-arm per Decision 7 | adapter, ruled by orchestration |
| Greeks seam | the COMMON fenced pass binding | a callable over the COMMON kernel (CLI-only today; seam = this design's ask) — the binding's SPOT-JOIN reader is ROUTED by `write_domain` + stamps the vendor/spot-source provenance bit (PRECONDITION P4, §15 item 7) | shared kernel; binding per vendor |
| Ledger market tokens | `groww` / `groww-future` / `groww-option` | `dhan` / `dhan-option` (Decision 1 §2.3) | orchestration convention |

**WRITE-DOMAIN TYPING (assurance re-review fold, NG-3 — the single-`Path`
form REFUTED):** `write_domain` was sketched as a single `Path`, but
`feeds/dhan/**` NESTS under the primary root — nested roots are not
disjoint prefixes — so a realpath-STARTSWITH containment check against a
single primary-root `Path` gives a FALSE PASS for a `feeds/dhan/**` dest
presented to the PRIMARY profile. The domain is therefore typed as an
**ALLOWED-SUBTREE SET with explicit FOREIGN-DOMAIN EXCLUSION**: a dest is
in-domain iff its realpath falls under one of THIS profile's subtrees AND
under NO OTHER registered profile's subtree; the PRIMARY profile's domain
additionally excludes the ENTIRE `feeds/` path-component prefix
STRUCTURALLY — registration-INDEPENDENT ("the root MINUS the whole
`feeds/` subtree", never by enumerating the root's children and NEVER the
registered-subtrees complement: an UNREGISTERED `feeds/<other>/` dest
falls INSIDE that complement, so a primary-profile write there would
FALSE-PASS — the same false-pass class; §1.4's
zero-`feeds/`-references invisibility argument already relies on the
whole-prefix form) (fold #4, NG3-EDGE-UNREGISTERED-SUBTREE). ADJUDICATION
RECORDED: the alternative "keep a single `Path` + document the
composition" was considered and REFUTED by the nested-root false-pass
above — the struct/set form is adopted (Pin D1-1's mechanism updated to
match, §2.5). READ composition, documented in the same fold: `write_domain`
is the WRITE law ONLY — v1 requires NO cross-domain reads (each vendor's
Greeks pass joins its OWN domain's spot; the engine reads the primary tree
only; a future Groww-vs-Dhan cross-verify reader is the only NAMED
cross-domain consumer and is out of v1 scope) — read ROUTING is a
per-consumer binding (PRECONDITION P4) guarded at runtime by the Pin D1-3
read-domain assertion (§2.5, NG-5).

**FORCE-INJECTION requirement (assurance fold, CONC-01 — adjudicated on
carried-forward evidence and ACCEPTED on the merits, §15.1):** the
Groww-defaulting constructors are an injection hazard, verified on this
tree: `SessionRateController`'s default factory hardcodes the Groww
8 r/s + 500/min stack (`brutex/dashboard/pull_controller.py:1008-1010`
default `_build_live_controller` :917, injecting
`GROWW_RATE_LIMIT_RPS_CEILING` + `GROWW_RATE_LIMIT_PER_MIN` :986-987), and
`BackgroundPullJob` defaults the GROWW market-hours guard
(`market_hours_guard: Callable[[], bool] = guard_active_default`,
`brutex/dashboard/pull_jobs.py:443`). REQUIREMENT: the `VendorPullProfile`
**FORCE-INJECTS both** — the rate factory AND the guard binding are never
left to a ctor default — and WS-C adds a FAIL-LOUD construction assertion
that a non-Groww vendor NEVER resolves `_build_live_controller` or
`guard_active_default`: a Dhan job missing its rate factory, or carrying
the Groww guard, RAISES at construction.

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
never two silent parallel abstractions. **Update (feed-track review fold,
2026-07-15):** the Dhan track ACKNOWLEDGES `VendorPullProfile`/the
capability registry as the ABSTRACTION and their
`feeds/{registry,binding}` layer as the CONCRETE INSTANCE (§12.1) — the
brokering resolves toward shape (a); WS-C still performs the inspection
and records it.

---

## 6. Decision 5 — Concurrency + isolation matrix (the zero-cross-impact rows)

**PRECONDITIONED (feed-track review fold, 2026-07-15 — Golden Rule #10; the
earlier "no cross-vendor contention by construction" claim is CORRECTED to
shipped truth):** the zero-cross-impact guarantee below holds only once
WS-D PRECONDITION P1 (§15 item 4) lands — the Dhan client-id-keyed
`account_key_resolver` on BOTH Dhan jobs, owned by the Dhan track (queued,
their ~#1865-band, landed on main as DEC-DHAN-ACCOUNT-LOCK-ISOLATE-001 #2213, ledger work landing behind the club). SHIPPED TRUTH
today, per the Dhan track's review of their club #1551 lineage (their
attribution — this track cannot see their branch): the club's two Dhan
jobs pass NO `account_key_resolver`, so they inherit the DEFAULT resolver —
verified on THIS tree: `BackgroundPullJob.__init__` defaults
`account_key_resolver=None` → `_default_account_key_resolver`
(`brutex/dashboard/pull_jobs.py:438` + :468-470 → :154-177), which returns
`resolve_groww_aws_credentials().api_key` — the GROWW credential. A wet
Dhan pull therefore takes the GROWW ACCOUNT flock, and a Dhan+Groww pull
pair is MUTUALLY EXCLUSIVE today (contention, never corruption — the flock
refuses the second start).

| Dimension | Groww chain | Dhan chain | Isolation mechanism | Shared residual? |
|---|---|---|---|---|
| Job slot | its own `BackgroundPullJob` (per-vendor injected lock factories + guard — the ctor seams exist, `pull_jobs.py:435-443`) | the staged CHAIN job of this design — noting the club ships TWO Dhan jobs TODAY (their #1551 lineage; feed-track review fold): whether the chain job ABSORBS or COEXISTS with them is a WS-D reconciliation item settled with the Dhan track at binding time (same ctor seams; precedent RE-ANCHORED at the assurance fold onto `_pull_job`, app.py:3094 — the stronger `cross_process_lock=False` start-time precedent (DEC-SWEEP-LOCK-PULL-CONCURRENCY-001) AND deletion-proof: the removal track plans to delete crossverify entirely, which would dangle the earlier `_crossverify_job` app.py:3132 citation) | separate job objects + worker threads | the dashboard PROCESS if both run in-process (see residuals) |
| Rate / quota | ONE `SessionRateController` (8 r/s + 500/min — DEC-GROWW-RATE-PERMIN-001) | ONE `SessionRateController` + `DailyQuotaTracker` (5 r/s + 100k/day — §1.3 item 3) | per-instance controllers (`pull_controller.py:992`; construction sites §1.4) — zero shared budget | none: a Groww 429 storm steps ONLY Groww's AIMD ladder; a Dhan quota exhaustion halts ONLY the Dhan chain |
| Auth | Groww TOTP/daily-token SSM re-read (CLAUDE.md §6) | Dhan 24h JWT SSM re-read (§1.3 item 7) | separate credentials + separate reauth machinery. The account single-flight keys per vendor account ONLY ONCE PRECONDITION P1 lands (feed-track review fold — the earlier unconditional wording was WRONG): today the Dhan jobs inherit the default GROWW-key resolver (the preamble above + the Locks row) | until P1: the GROWW-keyed account flock is SHARED — a wet Dhan pull and a Groww pull are mutually exclusive (contention, never corruption); after P1: none |
| Locks | account + lake-root + lease trio at the primary root | feed-root flock at `feeds/dhan/` + (today, via the job DEFAULTS) the GROWW-keyed account flock + a feed-root cross-host lease | disjoint lock FILES by construction for the root/partition dimensions (Decision 1 rationale (c)); per-partition flocks are path-keyed (`partition_lock.py:339`) so they never collide across domains — but the ACCOUNT dimension is NOT disjoint today (the preamble) | SHIPPED TRUTH (feed-track review fold — REPLACES the earlier "honest asymmetry" note, which was wrong BOTH ways): per the Dhan track's review of their club #1551 lineage, the Dhan jobs DO enter an account single-flight — the WRONG one: inheriting the default GROWW-key resolver (verified on this tree, `pull_jobs.py:154-177` + :438/:468-470), so a wet Dhan pull takes the GROWW account flock and a Dhan+Groww pull pair is MUTUALLY EXCLUSIVE today — and they DO enter a cross-host lease (the default `lease_factory=pull_lease_guard`, `pull_jobs.py:439`, entered on the job's resolved root — the Dhan FEED root; per-domain, harmless-to-desirable). FIX = WS-D PRECONDITION P1 (§15 item 4), owned by the Dhan track. The narrower residual that DOES remain accepted for v1 under the single-box envelope (CLAUDE.md §8): the feed-root lease detects same-ROOT peers, not same-ACCOUNT peers — two hosts sharing the Dhan ACCOUNT (TickVault co-tenancy, §9) still have no account-level cross-host detection |
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
   precedent (DEC-CROSSVERIFY-PRODUCER-001 — a LANDED precedent that
   survives as history; the removal track plans to delete crossverify, so
   the LIVING citation is the pull-beside-sweep concurrency law on
   `_pull_job`, app.py:3094, DEC-SWEEP-LOCK-PULL-CONCURRENCY-001). Not
   zero, disclosed.

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
   (Feed-track review fold — the RULING C twin of this hazard: a
   `daily_source=native` write domain EXCLUDES the recall's daily arm —
   per the Dhan track's verified state ZERO heal/scan machinery is wired
   over any feed root today, so the hazard is INERT there, but
   `_run_recall`'s daily arm is segment-ungated and would derive-OVERWRITE
   the native Dhan `1day` the moment their heal-wiring follow-up executes;
   recorded as their obligation.)
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
  file ownership; coordinated — the §1.3 COUNTERMAND is RESOLVED at the
  feed-track review fold: their ready follow-up PR ships the absence pins
  + the no-futures DECs).

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

**Tile caveat (feed-track review fold):** the 30-day rolling tile is
itself the conservative-UNVERIFIED choice — if the rolling endpoint honors
a true 90-day cap, whole-range re-issue drops to 1008 × 21 ≈ **21,000
requests ≈ 21% of quota**. The ruling's conclusion is UNCHANGED either
way: a nightly whole-range re-issue remains unsustainable beside spot +
TickVault co-tenancy on the shared daily budget.

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
- **Dhan today-window law — the P3 RULING (assurance fold, CONC-02
  confirmed; EXTENDED at the assurance re-review fold, NG-1 + NG-4):** the
  today-completeness law must hold PER VENDOR. The gate
  is Groww-spine-only (verified, §1.4 today-completeness row:
  `pull_controller.py:2157`/`:3228`), the lake is keep-first, and Dhan has
  NO market-hours guard (Q2 default) — so a mid-session Dhan END=today
  1-minute pull would freeze still-settling bars FINAL, and the NATIVE-DAILY
  stage is the WORSE twin (NG-1): `run_spot_daily`'s END=today would fetch
  a FORMING same-day `1day` bar mid-session that keep-first freezes and
  that NOTHING ever overwrites (`include_daily` structurally OFF — the
  native `1day` has no derive-refresh path, unlike Groww's
  derive-refreshed `1day`), so it could NEVER self-heal. **RULING
  (extended): v1 = EVERY Dhan pull window END — the native-daily
  `run_spot_daily` window AND the 1-minute spot/options windows — is
  CLAMPED to the previous trading day** — an explicit `to_date=today` is
  clamped with an honest note, never wrong bytes; today's settled daily
  bar arrives on the NEXT day's run — an ACCEPTED one-day daily lag,
  disclosed; threading the vendor-agnostic today-completeness
  classifier (`classify_today_completeness` is a pure function, §1.4)
  through the Dhan spine is the NAMED upgrade path. **MECHANISM
  CONSTRAINT (NG-4 — re-aimed by the assurance session's precision
  correction from the settle-evidence framing):** the clamp's "previous
  trading day" derives from the vendor-agnostic trading-calendar
  BACK-WALK (`calendars/nse.previous_trading_session`,
  `brutex/calendars/nse.py:999`) with the observed-session overlay
  VENDOR-SCOPED per §12 invariant 6 — that invariant already forbade the
  contamination class but was UNWIRED to P3; it is now BOUND here: the
  overlay's detector keys on GROWW ledger evidence
  (`market == GROWW_COVERAGE_MARKET`,
  `brutex/calendars/observed_sessions.py:470`) and Dhan has NO ledger
  (G4), so for Dhan v1 the resolution uses NO overlay at all
  (`observed=frozenset()` — the bare calendar back-walk); a Groww-evidence
  observed-holiday must never move a Dhan clamp. The Groww-measured
  settle-EVIDENCE overlay (`docs/groww/probe/settle_evidence.json`,
  DEC-SETTLE-EVIDENCE-ADOPTION-001) only ever tunes the GROWW intraday
  cutoff — it never participates in any Dhan clamp. The
  `VendorPullProfile` carries the `today_completeness` binding field
  (Decision 4); WS-D ships the clamp/absence pins — per window class,
  INCLUDING the forming-daily-bar ABSENCE pin and the back-walk pin over
  weekend + holiday-adjacent days (§15 item 6). **CLAMP-ABSTAIN CONTRACT
  (fold #4, NG4-EDGE-2027-CLAMP-ABSTAIN):**
  `previous_trading_session(observed=frozenset())` ABSTAINS (returns
  `None`) past the NSE holiday-coverage floor (2026 today) — on abstain
  the clamp REFUSES/HALTS the stage LOUDLY with a typed error, NEVER a
  fallback to `today`: a today-fallback would silently re-open the NG-1
  unhealable forming-native-`1day` freeze exactly in the 2027 window; the
  WS-D battery gains the coverage-abstain refusal pin (a post-coverage
  window END ⇒ loud stage refusal, NO window issued). (The Dhan
  track has fold-queued their side; the explicit `to_date=today` arm was
  the residual this ruling closes.)
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
  the §1.1 sequencer ruling's own-gate/own-token requirement). The token is
  SEQUENCER-minted from stage position at the gate — vendor reports carry
  no stopped token (the §12 invariant-4 ruling, feed-track review fold).
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
    # EVERY stage callable is typed at the STRUCTURAL StageOutcome protocol
    # (invariant 4 — feed-track review fold: the earlier concrete-Groww-type
    # annotations contradicted the protocol mandate; the concrete report
    # types are SATISFYING INSTANCES, never the contract):
    run_spot: Callable[[], StageOutcome],    # Groww instance: BulkSpotReport
    run_metadata: Callable[[], StageOutcome] | None = None,   # Groww instance: MetadataPullReport
    run_historical: ... ,                    # legacy combined F&O phase (default chain)
    run_prebuild: ... | None = None,         # legacy TERMINAL prebuild (default chain)
    # NEW staged callables (None = stage ABSENT from this vendor's graph):
    run_spot_daily: Callable[[], StageOutcome] | None = None,   # RULING C: native-daily stage (daily_source=native binds it)
    run_prebuild_spot: Callable[[], StageOutcome] | None = None,   # instance on both vendors: PrebuildOutcome
    run_futures: Callable[[], StageOutcome] | None = None,   # F&O spine, step_filter={INDICES_FUTURES,...}; Groww instance: CompositePullReport
    run_prebuild_futures: Callable[[], StageOutcome] | None = None,   # Groww instance: PrebuildOutcome
    run_options: Callable[[], StageOutcome] | None = None,   # per-contract (Groww instance: CompositePullReport) or rolling (Dhan)
    run_greeks: Callable[[], StageOutcome] | None = None,    # instance on both vendors: the COMMON GreeksEnrichOutcome
    vendor: str = "groww",                   # the stage-event token
    ...
) -> PipelinePullReport: ...
```

```python
@dataclass(frozen=True)
class VendorPullProfile:
    vendor: str                              # "groww" | "dhan"
    write_domain: WriteDomain                # ALLOWED-SUBTREE SET + foreign-domain EXCLUSION (NG-3 — the single-Path form REFUTED: nested feeds/ roots are not disjoint prefixes; Decision 4 typing law)
    capabilities: VendorCapability           # {spot, futures?, options_shape, metadata?, daily_source}
    daily_source: Literal["native", "derived"]  # RULING C — native binds run_spot_daily FIRST
    history_floor: Callable[[date], date]    # adapter-owned depth law
    paging: PagingProfile                    # adapter-owned chunk/tile law
    rate_factory: Callable[[], SessionRateController]   # ONE per chain
    quota_factory: Callable[[], DailyQuotaTracker] | None
    reauth_binding: ...                      # adapter-owned SSM re-read machinery
    account_key_resolver: Callable[[], str | None]
    lock_domain: LockDomain                  # trio-at-primary-root | feed-root flock
    market_hours_guard: Callable[[], bool]   # Groww: guard_active_default; Dhan: lambda: False — the no-guard binding IS a Callable[[], bool], never None (CONC-01 type fix; the ctor type at pull_jobs.py:443 is non-Optional)
    today_completeness: TodayCompletenessBinding        # P3 — Groww: classifier_gate; Dhan v1: clamp_to_prev_session over EVERY window END, native-daily included (NG-1)
    no_intent_persist: bool                  # NG-2 — True ⇒ the shared launcher NEVER invokes persist_intent_for_launch (Dhan v1: True; Groww: False — §2.5 .pull_intent row)
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
  WS-C deliverables): `PhaseKey` = an OPAQUE, VENDOR-MINTED coverage-key
  token (feed-track review fold — the earlier fixed
  `(vendor, stage, instrument?)` tuple sketch is superseded: a fixed tuple
  cannot address the Dhan rolling space, 1008 keys of underlying ×
  WEEK/MONTH × expiry_code × strike offset × side, §1.3 item 1): the
  vendor's spine MINTS keys at its own granularity and the sequencer /
  `incremental_from` plumbing threads them opaquely — never constructed or
  interpreted by the orchestration layer;
  `VendorCapability` = a frozen set-like declaration
  `{spot: bool, futures: bool, options_shape: per_contract|rolling_atm|None,
  metadata: bool, daily_source: native|derived}`; `LockDomain` = the
  vendor's lock recipe
  (`trio_at_primary_root` | `feed_root_flock(path)`); `PagingProfile` = the
  adapter's chunk/tile law (`max_days_per_request` per phase kind);
  `IncrementalityCapability` = HOW `incremental_from` is derived
  (`durable_ledger(markets)` | `lake_tail_scan`) — `None` = absent
  (Decision 7); `TodayCompletenessBinding` = HOW the per-vendor
  today-completeness law holds (`classifier_gate` — the
  DEC-PULL-TODAY-COMPLETENESS-GATE-001 classifier armed at the spine —
  | `clamp_to_prev_session` — the Dhan v1 window-END clamp over EVERY
  window, the native-daily `run_spot_daily` window included, the §9 P3
  ruling + NG-1); `WriteDomain` = the profile's ALLOWED-SUBTREE SET plus
  the foreign-domain EXCLUSION law (NG-3; Decision 4 — a dest is
  in-domain iff its realpath falls under one of the profile's subtrees
  AND under NO other registered profile's; the primary domain = the root
  MINUS the ENTIRE `feeds/` subtree, registration-INDEPENDENT — fold #4,
  NG3-EDGE: the registered-complement form would false-pass an
  unregistered `feeds/<other>/` dest).

**Invariants every vendor spine must honor (the contract this design
enforces — invariant 1 at RUNTIME per Pin D1-1, the rest at review
time):**

1. ALL bytes land inside the vendor's lake WRITE DOMAIN (Decision 1) —
   **PROMOTED from review-time to a RUNTIME fail-loud pin (assurance
   fold, ISOL-04; the WS-B/WS-C requirement is Pin D1-1, §2.5):** every
   staged write dest's realpath is resolved against the active profile's
   `write_domain` — the ALLOWED-SUBTREE SET with foreign-domain EXCLUSION
   (the NG-3 typing: a single-Path prefix check FALSE-PASSES nested
   `feeds/` roots — Decision 4) — and a cross-domain write REFUSES with a
   typed
   exception — never a keep-first merge (`parquet_writer.py:560-617` does
   not raise on a cross-domain dest; `partition.py` carries no vendor
   dimension — verified); precedent
   `OptionParquetWriter._assert_options_containment`
   (`option_parquet_writer.py:317`); negative-pin battery REQUIRED (both
   directions + the typed refusal + the nested-root fixture, Pin D1-1).
2. ONE rate controller (+ quota tracker where declared) threaded across ALL
   of that vendor's phases — never shared with, or blended into, the peer
   vendor's.
3. Halts are HONEST typed exceptions (the vendor's global-stop family);
   stage callables NEVER swallow `LakeWriteHalt`.
4. **The stage-callable contract is a STRUCTURAL protocol, NOT the concrete
   Groww report types** (the Dhan track's interface improvement, adopted at
   the 2026-07-15 input fold): each stage callable returns an object
   satisfying a `StageOutcome`-shaped protocol naming the REQUIRED
   fields/semantics — an `ok` verdict, failed/still-failed counts, and halt
   propagation (a vendor-halt / `LakeWriteHalt` is RAISED, never swallowed
   into the outcome — invariant 3). **RULING (this design's own, recorded
   at the feed-track review fold): the SEQUENCER owns `stopped_after`** —
   the token is minted from stage POSITION at the gate, so the protocol
   carries NO stopped-reason token and vendor reports never synthesize one
   (the input fold's earlier "stopped-reason token" protocol field is
   superseded by this ruling). Groww's
   existing report shapes (`BulkSpotReport` / `MetadataPullReport` /
   `CompositePullReport` / `PrebuildOutcome` / `GreeksEnrichOutcome`)
   already satisfy it, so the sequencer's gate predicates (`spot_phase_ok`
   family, `pipeline_pull.py:149-218`) generalize over the protocol
   unchanged for Groww; Dhan's stage reports are `DhanWindowOutcome`-based
   (NOT `BulkSpotReport`) and — CORRECTED at the feed-track review fold
   (the unconditional "directly or via thin adapters" claim was FALSE for
   OPTIONS) — satisfy the protocol directly or via THIN adapters for
   SPOT / PREBUILD / GREEKS, but NOT for OPTIONS today: per their review of
   their club #1551 lineage (their evidence — this track cannot see their
   branch), the rolling spine's `except DhanError` (their
   `run_dhan_rolling_option_ingest.py:207`) lacks the
   `is_terminal_pull_stop` carve-out their own `errors.py` docstring
   promises (the SPOT spine HAS it) — a swallowed terminal halt (e.g. a
   stale token) on a wet whole-range pull would burn up to ~3×61k doomed
   requests against the SHARED 100k/day quota, and NO adapter can
   un-swallow an exception (invariant 3 is unsatisfiable from outside the
   spine). The carve-out is WS-D PRECONDITION P2 (§15 item 5) — REQUIRED
   before the Dhan `run_options` binding; their code fix, queued. Their
   Dhan Greeks CLI pass is exposed as a callable seam
   returning a gate-compatible outcome shape (their commitment, §1.3
   item 9).
5. Stage-event tokens (`vendor=`, `stage=`) are emitted by the SEQUENCER,
   not re-minted per spine (§10).
6. **The calendar observed-session overlay is VENDOR-SCOPED (assurance
   fold — register hazard
   `CAL-OBSERVED-OVERLAY-SINGLE-VENDOR-CONTAMINATION`):** the
   observed-holiday overlay consumed via
   `previous_trading_session(observed=)`
   (`brutex/calendars/nse.py:999-1001` — verified; the remove-only,
   evidence-gated `observed-not-official` sidecar of
   DEC-CALENDAR-OBSERVED-SESSIONS-001, whose DETECTOR keys on GROWW
   ledger evidence — `market == GROWW_COVERAGE_MARKET` mandatory) must be
   scoped PER VENDOR in the dual-vendor design: one vendor's
   observed-holiday overlay must NEVER contaminate the other vendor's
   calendar / prev-session resolution (a Dhan-evidence outage day must not
   move a Groww prev-close baseline, and vice-versa). A WS-D item.
   **WIRED to P3 at the assurance re-review fold (NG-4 — the invariant
   already forbade the class but was unwired to the clamp):** the Dhan v1
   clamp's prev-session resolution uses NO overlay at all
   (`observed=frozenset()` — the bare calendar back-walk: the overlay's
   detector keys on GROWW ledger evidence,
   `brutex/calendars/observed_sessions.py:470`, and Dhan has no ledger,
   G4); §9 + §15 item 6 carry the binding + the weekend/holiday-adjacent
   back-walk pin.

The Dhan track's CHANGES-REQUESTED review of this ADR CONFIRMED the §12
interface implementable against their club #1551 lineage. THEIR binding
sketch, recorded with attribution (every symbol is theirs, on their tree —
this track cannot see their branch; re-confirm at WS-D):

- `run_spot` = a closure over their `run_dhan_spot_pull` — deliberately
  NOT their `default_dhan_spot_pull`, which BUNDLES prebuild and would
  collapse the staged gates — plus a thin `failed_windows` → `StageOutcome`
  adapter.
- `run_prebuild_spot` = their `run_dhan_prebuild(include_daily=False)` —
  flipped + pinned by their native-daily PR (the RULING C pinned
  invariant).
- `run_options` = their `run_dhan_rolling_option_pull` + adapter —
  **BLOCKED on PRECONDITION P2** (the rolling-spine terminal-stop
  carve-out — invariant 4 above / §15 item 5).
- `run_greeks` = trivial — their pass already returns the COMMON
  `GreeksEnrichOutcome` (a `StageOutcome`-satisfying instance).
- `run_futures` / `run_metadata` = `None` (absent capabilities — RULING A).

WS-C acknowledgment (theirs, same review): the `VendorPullProfile` /
capability registry of Decision 4 is the ABSTRACTION and their
`brutex/feeds/{registry,binding}` layer is the CONCRETE INSTANCE — the §5
ownership-brokering item resolves toward shape (a); WS-C still performs
the inspection and records the consumption/rename.

---

## 13. Honest limits (Golden Rule #10)

- **Design-only.** Nothing in this ADR is implemented or verified-by-run;
  every "byte-identical default" claim is a WS-B test obligation, not a
  present-tense fact.
- The Dhan contract rows (§1.3) are the Dhan track's DECLARATIONS; the two
  named memory-file sources were absent at the authoring commit and landed
  ~7 minutes later — they are NOW cited as the primary declared evidence
  base (timeline + deltas recorded in §1.3, incl. the RULING-B/A
  countermand of their G2/G3 TODO — RESOLVED at the feed-track review
  fold: their ready PR ships the pins, §1.3 restamp). Still THEIR
  declarations — re-confirm at WS-D.
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
- **Zero-cross-impact is CONDITIONAL until the two WS-D PRECONDITIONS land
  (feed-track review fold; §15 items 4-5):** TODAY, per the Dhan track's
  review of their club lineage, a wet Dhan pull takes the GROWW account
  flock via the inherited default resolver (P1 — a Dhan+Groww pull pair is
  mutually exclusive, never corrupting) and the rolling-options spine can
  swallow a terminal halt into up to ~3×61k doomed requests on the shared
  100k/day quota (P2). Both fixes are the Dhan track's, queued. Two
  FURTHER preconditions landed at the assurance fold (§15 items 6-7): P3
  (the Dhan today-window clamp — data correctness, not cross-impact;
  since the re-review fold covering EVERY window END incl. native-daily,
  NG-1) and
  P4 (Greeks spot-join domain routing + the vendor provenance bit + the
  Pin D1-3 read-domain assertion —
  required before ANY second-vendor Greeks binding).
- **The Dhan v1 chain has NO boot-resume** (the ISOL-02 ruling, §2.5: no
  intent store — manual re-run is the recovery, consistent with the G4
  manual-runs posture; a crash never replays the wrong vendor's scope
  because there is nothing to replay — a claim TRUE only WITH the NG-2
  launcher gate, §2.5: without `no_intent_persist` the SHARED launcher's
  `persist_intent_for_launch` would have written a Groww-shaped intent
  for the boot-resume to replay, chain store or no chain store).
- **The Dhan v1 today-window is CLAMPED to the previous trading day on
  EVERY window — the native-daily `run_spot_daily` window INCLUDED** (P3,
  extended at the assurance re-review fold, NG-1): same-day Dhan bars are
  structurally absent in v1 — an honest freshness trade (never wrong
  bytes; today's settled daily bar arrives on the NEXT day's run — an
  accepted one-day daily lag). **CLAIM CORRECTION (Golden Rule #10):** the
  prior revision made this "structurally absent" claim while the clamp
  covered ONLY the 1-minute spot/options windows — an OVER-CLAIM: the
  native-daily stage's END=today would have fetched a FORMING same-day
  `1day` bar mid-session, keep-first would freeze it, and — the Dhan
  `1day` being NATIVE with `include_daily` structurally OFF — no derive
  would EVER overwrite it (unhealable, unlike Groww's derive-refreshed
  `1day`). The extended clamp MAKES the claim true; the correction is
  recorded here, not silently rewritten. The classifier-threading upgrade
  path removes the clamp.
- The quota arithmetic (§8) uses the DECLARED tile/quota shape; a vendor
  change moves the numbers — the ruling's structure (incrementality gates
  auto-arm) survives, the percentages may not.
- `feeds/dhan/**` is invisible to every primary dashboard/census surface
  until a CONSUMED read surface lands (their unconsumed
  `dhan_rolling_db_view.py` /db first slice exists on their lineage —
  feed-track review fold) — an accepted, named gap.
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
4. **WS-D PRECONDITION P1 (feed-track review fold — the zero-cross-impact
   precondition):** the Dhan client-id-keyed `account_key_resolver` on BOTH
   Dhan jobs — owned by the Dhan track (queued, their ~#1865-band, landed on main as DEC-DHAN-ACCOUNT-LOCK-ISOLATE-001 #2213, ledger
   work landing behind the club). Until it lands, a wet Dhan pull takes the
   GROWW account flock via the inherited default resolver (verified on
   this tree — `brutex/dashboard/pull_jobs.py:154-177` + :438/:468-470)
   and a Dhan+Groww pull pair is MUTUALLY EXCLUSIVE (Decision 5 preamble);
   the BASE directive's zero-cross-impact requirement holds only once P1
   lands.
5. **WS-D PRECONDITION P2 (feed-track review fold):** the Dhan rolling
   spine's `except DhanError` gains the `is_terminal_pull_stop` carve-out
   (their evidence on their club tree:
   `run_dhan_rolling_option_ingest.py:207`; the spot spine already has it;
   their own `errors.py` docstring promises it) — REQUIRED before the Dhan
   `run_options` binding (§12 invariant 4); their code fix, queued.
6. **WS-D PRECONDITION P3 (assurance fold, CONC-02 — the Dhan today-window
   clamp; EXTENDED at the assurance re-review fold, NG-1 + NG-4):** the
   today-completeness law must hold PER VENDOR — the gate is
   Groww-spine-only (verified: `today_completeness_gate=_today_gate`,
   `brutex/dashboard/pull_controller.py:2157` + `:3228`), so with
   keep-first + no Dhan market-hours guard (Q2) a mid-session Dhan
   END=today 1-minute pull would freeze still-settling bars FINAL — and
   the NATIVE-DAILY stage is the WORSE twin (NG-1, a freeze BLOCKER):
   `run_spot_daily`'s END=today would fetch a FORMING same-day `1day` bar
   mid-session that keep-first freezes and that NOTHING ever overwrites
   (`include_daily` structurally OFF — the native `1day` has no
   derive-refresh path, unlike Groww's), so it could NEVER self-heal.
   RULING (§9, extended): v1 = EVERY Dhan pull window END — the
   native-daily `run_spot_daily` window AND the 1-minute spot/options
   windows — is CLAMPED to the previous trading day (an explicit
   `to_date=today` is clamped with an honest note — never wrong bytes;
   today's settled daily bar arrives on the NEXT day's run — the accepted
   one-day daily lag); the vendor-agnostic classifier
   threading is the named upgrade path; the `VendorPullProfile` carries
   the `today_completeness` binding field (Decision 4). MECHANISM
   CONSTRAINT (NG-4): the clamp's "previous trading day" derives from the
   vendor-agnostic calendar BACK-WALK
   (`calendars/nse.previous_trading_session`,
   `brutex/calendars/nse.py:999`) with the observed-session overlay
   VENDOR-SCOPED per §12 invariant 6 (now WIRED to P3) — for Dhan v1 that
   means NO overlay at all (`observed=frozenset()`: the overlay's detector
   keys on GROWW ledger evidence, `market == GROWW_COVERAGE_MARKET` —
   `brutex/calendars/observed_sessions.py:470` — and Dhan has no ledger,
   G4); the Groww-measured settle-EVIDENCE overlay
   (`docs/groww/probe/settle_evidence.json`) only ever tunes the GROWW
   intraday cutoff — never any Dhan clamp. WS-D ships the clamp/absence
   pins per window class: the forming-daily-bar ABSENCE pin (a
   mid-session Dhan chain run stores NO today-dated `1day` bar) + the
   intraday clamp pin + the back-walk pin over weekend AND
   holiday-adjacent days + the coverage-abstain refusal pin (fold #4,
   NG4-EDGE: past the 2026 NSE holiday-coverage floor
   `previous_trading_session` returns `None` — the clamp REFUSES the
   stage loudly with a typed error, NEVER falls back to `today`, which
   would silently re-open the NG-1 unhealable forming-native-`1day`
   freeze in the 2027 window). (The Dhan track has fold-queued their
   side; the explicit `to_date=today` arm was the residual.)
7. **WS-D PRECONDITION P4 (assurance fold, CAPA-01; assurance-register
   pointer VCA-011; extended at the assurance re-review fold, NG-5 +
   NG-6) — REQUIRED before ANY second-vendor Greeks binding:**
   (i) the Greeks SPOT-JOIN reader is ROUTED by the profile's domain —
   verified single-lever today: `spot_close_by_ts_window`
   (`brutex/data/spot_join.py:83`) hard-anchors
   `lake_root / "bars" / …` (:113) with no domain routing; (ii) the
   Greeks provenance pack gains a VENDOR/SPOT-SOURCE bit — verified: the
   pack currently occupies bits 0-26 ("Total 27 bits < 2^31",
   `brutex/data/greeks_provenance.py:132-133`; statuses bits 23-26,
   `_STATUS_SHIFT=23` :141), leaving Int32 headroom through bit 30.
   **ENCODING LAW (NG-6):** (a) the bit is ADDITIVE-ONLY with
   **Groww = 0** and **NO greeks pack/formula version bump** — a
   formula-version bump lives inside the staleness-compare `INPUTS_MASK`
   (bits 0-19), so it would flip `stamp_is_current_inputs`
   (`brutex/data/greeks_enrich.py:279` — the
   DEC-GREEKS-BULK-REENRICH-001 reuse gate) FALSE for EVERY stored Groww
   Greek and fire a full-lake re-enrich; (b) the bit sits **≥ bit 27 —
   strictly ABOVE the status field** (statuses occupy bits 23-26,
   `_STATUS_SHIFT=23`) and is NEVER inserted mid-field (an insertion
   below bit 27 would shift the status/style decode of every stored
   stamp). REQUIRED GOLDEN: a stamp-equality pin — every existing stored
   `greeks_provenance_id` value is BYTE-UNCHANGED under the new pack
   (Groww=0 at ≥ bit 27 packs to the identical integer). PACK-WIDTH
   BUDGET (the NG-6 ceiling note): the Int32 headroom ceiling (**≤ bit
   30** — bit 31 is the sign) bounds the vendor/spot-source encoding; a
   THIRD vendor — or ANY pack-width growth past bit 30 — requires its
   OWN pack-width DEC, never a silent overrun. And (iii) the **Pin D1-3
   RUNTIME read-domain assertion** (§2.5, NG-5) — the READ-side twin of
   Pin D1-1: a second-vendor Greeks pass handed a PRIMARY-domain spot
   reader REFUSES with a typed exception; routing (i) binds the right
   reader, the assertion refuses the wrong one. Together (i)-(iii) make a
   mis-invoked pass (the primary root handed to the second-vendor Greeks
   stage) IMPOSSIBLE to stamp undetected — and refused at runtime before
   it stamps at all. §1.4 Greeks row carries the
   routing correction of the old "spot lands first" wording.

### 15.1 Assurance-review disposition record (2026-07-15, byte-verified vs `440e7152`)

The assurance session's review (verdict: **FIX-FIRST-BEFORE-WS-B-FREEZE**,
byte-verified against this file's then-head `440e7152`) is folded by this
revision; its file:line evidence was re-verified on this tree where
checkable (two citation corrections recorded: the containment precedent
lives at `OptionParquetWriter._assert_options_containment`,
`option_parquet_writer.py:317` — not `lake_heal`, which only cites it at
:1197; the spot-join probe line is :113 under the :83 def).

- **Folded (the six items):** ISOL-04 (→ Pin D1-1 + §12 invariant-1
  runtime promotion) · CAPA-01 (→ P4 + the §1.4/§2.5 routing correction +
  the provenance bit) · CONC-02 (→ P3 + the §9 clamp ruling + the
  Decision 4 `today_completeness` field) · ISOL-02 (→ the §2.5
  no-Dhan-intent-store v1 ruling + the sidecar enumeration + the
  two-vendor crash-resume pin) · CAPA-02 (→ the Decision 3
  ABSENT-vs-FAILED vacuously-settled contract + the PARTIAL-absent parity
  test) · CONC-01 (→ the Decision 4 force-injection requirement + WS-C
  fail-loud construction assertions + the §5/§12 `lambda: False` type
  fix). CONC-01 was adjudicated on CARRIED-FORWARD evidence (their
  adjudicator hit a retry cap) and is accepted on the merits — the
  evidence re-verified here (`pull_controller.py:1008-1010`/:917;
  `pull_jobs.py:443`).
- **Downgrades honored:** ISOL-03 MAJOR→MINOR (job-resolved-root sidecars
  are already per-domain by the §2.5 construction law; only
  `.rate`/`.pull_intent` excepted — now dispositioned in the §2.5 table);
  CONC-03/CONC-04 → MINOR future-scheduler defensive pins, NOT
  freeze-blockers (Decision 7 blocks Dhan auto-arm); ISOL-01
  already-ruled — the residual (the ledger-subpath doc-pin + the
  backup-allowlist add) is now Pin D1-2 (§2.5).
- **Refuted list NOT re-opened:** CAPA-03/CAPA-05, REPR-02/REPR-03,
  CONC-05, SIGN-01/SIGN-04/SIGN-05.
- **Assurance-covered positive list noted:** the review's byte-verified
  confirmations of this design's standing claims (write-domain
  disjointness of the lock files, the per-instance rate controllers, the
  anchored primary scanners, the Decision 5 shipped-truth rows) stand as
  independent coverage — recorded, not re-litigated.

**Assurance RE-review disposition (2026-07-15, vs this file's then-head
`96486abf` — folded by this revision):** verdict FIX-FIRST for the WS-B
freeze; the prior review's ten items verified **10/10 CLOSED**; SIX NEW
gaps (NG-1..NG-6; NG-1/NG-2 the freeze blockers) folded, two of them
re-aimed by the assurance session's own precision corrections BEFORE
folding (the corrections recorded per gap):

- **NG-1 (BLOCKER → §9 + §15 P3 + §1.1/§3.2 window notes + the §13 claim
  correction):** P3's clamp was scoped to the 1-minute windows only — the
  native `run_spot_daily` END=today would freeze a FORMING,
  never-derive-overwritten `1day` bar (unhealable: `include_daily`
  structurally OFF). The clamp now covers EVERY Dhan window END (the
  accepted one-day daily lag disclosed), a forming-daily-bar ABSENCE pin
  is required, and §13's "same-day Dhan bars structurally absent"
  over-claim is corrected in place (made true BY the extended clamp, said
  so).
- **NG-2 (BLOCKER → the §2.5 `.pull_intent` row + the Decision 4/§12
  `no_intent_persist` field):** the ISOL-02 ruling pinned the CHAIN's
  no-intent-store but NOT the shared LAUNCHER —
  `persist_intent_for_launch` (`pull_intent.py:481`, launcher call site
  `app.py:5257` — verified) writes the vendorless root-singleton BEFORE
  the vendor graph runs. The PROFILE now gates the LAUNCHER; the WS-C pin
  drives the REAL shared launcher asserting the PRIMARY root's
  `.pull_intent.json` is byte-UNTOUCHED by a Dhan launch; the P1
  coupling (persist-default-ON breaks the crash-resume pin once the
  account flock no longer excludes concurrency) is recorded.
- **NG-3 (MAJOR; mechanism CORRECTED in-session → the Decision 4 typing
  paragraph + Pin D1-1 + §12 invariant 1):** the single-`Path`
  `write_domain` was REFUTED — `feeds/dhan/**` NESTS under the primary
  root, so a realpath-STARTSWITH check FALSE-PASSES a feeds/dhan dest
  against the PRIMARY profile (nested roots are not disjoint prefixes).
  Retyped as an ALLOWED-SUBTREE SET with foreign-domain EXCLUSION
  (primary = root MINUS registered `feeds/` subtrees — that
  registered-complement PHRASING itself superseded at fold #4,
  NG3-EDGE: the exclusion is the WHOLE `feeds/` prefix,
  registration-independent); the nested-root
  negative-pin fixture is required; the earlier "single Path + documented
  composition" ruling is SUPERSEDED by that refutation (adjudication
  recorded at Decision 4). The v1 read-composition note stands: NO
  cross-domain reads in v1 — read routing is a per-consumer binding
  guarded by Pin D1-3.
- **NG-4 (MAJOR; mechanism CORRECTED in-session → §9 + §15 P3 + the §12
  invariant-6 wiring):** the contaminating source is the OBSERVED-SESSION
  overlay (`previous_trading_session(observed=)`, detector keyed on the
  GROWW ledger — `observed_sessions.py:470`), NOT the settle-evidence
  file (that framing dropped; the settle overlay only ever tunes the
  Groww intraday cutoff — the sentence kept). Invariant 6 already forbade
  the class but was UNWIRED to P3 — now bound: Dhan v1 resolves with NO
  overlay at all (the bare calendar back-walk; Dhan has no ledger, G4),
  with the weekend + holiday-adjacent back-walk pin required.
- **NG-5 (MAJOR → Pin D1-3 + P4 item (iii)):** the RUNTIME read-domain
  assertion on the Greeks spot-join — the READ-side twin of Pin D1-1; a
  second-vendor pass handed a primary-domain spot reader refuses with a
  typed exception.
- **NG-6 (MINOR, expanded → the P4 item (ii) encoding law):** the
  vendor/spot-source bit is additive-only with Groww=0 and NO
  formula/pack version bump (the `stamp_is_current_inputs` full-lake
  re-enrich hazard), sits ≥ bit 27 strictly above the status field
  (never mid-field), carries a stamp-equality GOLDEN (existing
  `greeks_provenance_id` values byte-unchanged), and is bounded by the
  ≤ bit 30 Int32 ceiling — a third vendor / any pack-width growth needs
  its own pack-width DEC.

**Assurance fold-#3 RE-review disposition (2026-07-15, vs this file's
then-head `98533528` — folded by this revision, fold #4):** verdict
FIX-FIRST; the fold-#3 six items (NG-1..NG-6) verified **6/6 CLOSED
byte-exact**; 1 MAJOR + 2 MINOR edge findings — ALL ADR-TEXT precision
fixes, no new mechanism — folded @ this commit:

- **NG3-EDGE-UNREGISTERED-SUBTREE (MAJOR, freeze-blocking → Pin D1-1 +
  the Decision 4 table row + the Decision 4 typing paragraph + the §12
  field sketch):** the fold-#3 text stated the PRIMARY write-domain THREE
  times in the COMPLEMENT form "root MINUS the REGISTERED `feeds/`
  subtrees" and called it equivalent to the allowed-set form — NOT
  equivalent for an UNREGISTERED `feeds/` subtree: the allowed-set form
  correctly refuses a dest under `feeds/<unregistered>/`, but the
  registered-complement form INCLUDES it in the primary domain, so a
  primary-profile write there FALSE-PASSES (the same nested-root
  false-pass class NG-3 closed). Fixed at all sites: the primary domain
  excludes the ENTIRE `feeds/` path-component prefix STRUCTURALLY —
  registration-INDEPENDENT ("root MINUS the whole `feeds/` subtree") —
  which §1.4's invisibility argument (zero `feeds/` references in any
  primary scanner) already relies on; Pin D1-1's required battery gains
  the UNREGISTERED-FEEDS negative fixtures (a primary-profile write under
  `feeds/<anything>/` refuses, registered or not; a feeds-profile write
  under a foreign `feeds/<other>/` refuses too).
- **NG4-EDGE-2027-CLAMP-ABSTAIN (MINOR → the §9 P3 mechanism + the §15
  item 6 pin list):** `previous_trading_session(observed=frozenset())`
  ABSTAINS (returns `None`) past the NSE holiday-coverage floor (2026
  today). RULED: on abstain the clamp REFUSES/HALTS the stage LOUDLY with
  a typed error — NEVER a fallback to `today` (a today-fallback would
  silently re-open the NG-1 unhealable forming-native-`1day` freeze
  exactly in the 2027 window); the coverage-abstain refusal pin joins the
  WS-D battery (a post-coverage window END ⇒ loud stage refusal, NO
  window issued).
- **NG2-EDGE-LAUNCH-MUTEX-COUPLING (MINOR → the §2.5 `.pull_intent` row +
  the Decision 4 `no_intent_persist` field + the §17 WS-C pin):** gating
  only the persist CALL is insufficient — the shared launcher ENTERS the
  primary store's `launch_mutex` on `pull_intent_store is not None`
  (`app.py:5243`, held across `start()` including the SSM credential read
  + lease I/O, the ≈10 s held-span envelope). RULED: a
  `no_intent_persist` launch binds NO primary intent store — it reaches
  the launcher with `pull_intent_store=None`, structurally skipping
  mutex/persist/clear/restore/banner — so a Dhan launch can never CONTEND
  on the primary launch mutex (zero-cross-impact); the WS-C pin asserts
  BOTH the byte-untouched primary `.pull_intent.json` singleton AND no
  primary-mutex entry.

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
- **RESOLVED (2026-07-16):** the questions above are resolved by the
  operator's landing decision (PR #1555 merged to main) + the 2026-07-15/16
  rulings recorded in-ADR (§§2-9, §15.1) and in the ledger
  (`DEC-DUAL-VENDOR-PIPELINE-001`, MIGRATION.md #2011; Q1's chosen
  disposition (a) shipped as DEC-OPTIONS-PREBUILD-RETIRE-001,
  MIGRATION.md #1831).

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
  defaults PINNED; the STRUCTURAL skip of absent stages + the
  ABSENT-vs-FAILED vacuously-settled contract with its Dhan-shaped
  PARTIAL-absent parity test (Decision 3, CAPA-02); the RUNTIME
  write-domain refusal (Pin D1-1, with WS-C) + its negative-pin battery
  incl. the NESTED-ROOT fixture (the NG-3 allowed-subtree-set +
  foreign-exclusion law).
- **WS-C:** `VendorPullProfile` / capability registry (incl.
  `daily_source` + `today_completeness` + `no_intent_persist`) +
  per-vendor job slots +
  per-vendor guard binding; the CONC-01 fail-loud construction assertions
  (a non-Groww vendor never resolves `_build_live_controller` /
  `guard_active_default`); the NG-2 launcher intent gate with its
  REAL-shared-launcher pin asserting BOTH the primary `.pull_intent.json`
  singleton byte-untouched by a Dhan launch AND no primary-launch-mutex
  entry (the fold-#4 NG2-EDGE store-unbound mechanism —
  `pull_intent_store=None`, §2.5); the two-vendor crash-resume pin
  (§2.5 `.pull_intent` row, with WS-D); the Pin D1-1 runtime refusal
  (with WS-B).
- **WS-D:** the Dhan chain binding against the profile (with the Dhan
  track): feed-root derive-lock domain (VERIFIED-AS-BUILT by the Dhan
  track, §3.3 — WS-D consumes their pin, it does not re-verify),
  `run_spot_daily` + `incremental_from` seams, greeks seam; gated on
  PRECONDITIONS P1-P4 (§15 items 4-7 — P3's clamp/absence pins land here
  for DAILY + INTRADAY incl. the forming-daily-bar absence pin, the
  NG-4 bare-back-walk binding + its weekend/holiday-adjacent pin, and
  P4's spot-join routing + provenance bit + the Pin D1-3 read-domain
  assertion) and settles the
  two-jobs-vs-chain reconciliation (Decision 5 job-slot row) at binding
  time — the §12.1 binding sketch is the Dhan track's confirmed starting
  point; the vendor-scoped observed-overlay invariant (§12 invariant 6)
  + the two-vendor crash-resume pin (with WS-C).
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
- The Dhan feed track's CHANGES-REQUESTED review of this ADR (2026-07-15,
  against their club #1551 lineage) — folded same-day as the feed-track
  review fold: the shipped-truth isolation rows + PRECONDITION P1 (the
  Groww-keyed default account resolver, verified on this tree at
  `brutex/dashboard/pull_jobs.py:154-177` + :438/:468-470/:439) ·
  PRECONDITION P2 (the rolling-spine terminal-stop carve-out, their
  evidence) · the §12 structural retyping + the opaque vendor-minted
  `PhaseKey` + the sequencer-owned `stopped_after` ruling · the §8
  tile caveat · the §1.3 G2/G3 restamp + options-floor-constant reword ·
  the unconsumed `dhan_rolling_db_view.py` read-surface note · the two-jobs
  job-slot clarification · the §12.1 binding-sketch appendix + their WS-C
  abstraction/instance acknowledgment.
- The assurance session's byte-verified review (2026-07-15, vs `440e7152`;
  verdict FIX-FIRST-BEFORE-WS-B-FREEZE) — folded as the §15.1 disposition
  record: Pin D1-1/D1-2 + the §2.5 sidecar disposition table ·
  PRECONDITIONS P3 (Dhan today-window clamp) + P4 (Greeks spot-join
  routing + vendor provenance bit; register pointer VCA-011) · the
  ABSENT-vs-FAILED vacuously-settled contract · the no-Dhan-intent-store
  v1 ruling · the CONC-01 force-injection requirement · the vendor-scoped
  observed-overlay invariant
  (`CAL-OBSERVED-OVERLAY-SINGLE-VENDOR-CONTAMINATION`) · the Decision-5
  precedent re-anchor onto `_pull_job`.
- The assurance session's RE-review (2026-07-15, vs `96486abf`; the prior
  ten items verified 10/10 CLOSED; verdict FIX-FIRST for the WS-B freeze)
  — its six NEW gaps NG-1..NG-6 (NG-3/NG-4 mechanism-corrected by the
  same session's precision corrections before folding) folded as the
  §15.1 re-review disposition record: the extended P3 (native-daily END
  clamp + the NG-4 bare-back-walk binding) · the NG-2
  `no_intent_persist` launcher gate · the NG-3 `WriteDomain`
  allowed-subtree-set retyping · Pin D1-3 · the NG-6 provenance-bit
  encoding law.
- The assurance session's fold-#3 RE-review (2026-07-15, vs `98533528`;
  the fold-#3 six items NG-1..NG-6 verified 6/6 CLOSED byte-exact;
  verdict FIX-FIRST — 1 MAJOR + 2 MINOR ADR-text precision edges) —
  folded as the §15.1 fold-#4 disposition record: the NG3-EDGE
  whole-`feeds/`-prefix structural (registration-INDEPENDENT) primary
  domain + the unregistered-feeds Pin D1-1 fixtures · the NG4-EDGE
  clamp-abstain refusal contract (never a today-fallback) · the NG2-EDGE
  store-unbound launcher mechanism (`pull_intent_store=None` — no
  primary-mutex entry).
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
  DEC-SWEEP-LOCK-PULL-CONCURRENCY-001 (the re-anchored Decision-5
  precedent) · DEC-CALENDAR-OBSERVED-SESSIONS-001 (the observed-overlay
  sidecar behind §12 invariant 6) ·
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
  `brutex/backup/plan.py` · `brutex/dashboard/app.py` ·
  (assurance fold) `brutex/data/spot_join.py` ·
  `brutex/dashboard/pull_intent.py` ·
  `brutex/data/option_parquet_writer.py` · `brutex/calendars/nse.py` ·
  (assurance re-review fold) `brutex/calendars/observed_sessions.py`.
