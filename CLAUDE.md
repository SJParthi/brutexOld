# BruteX — Claude Code Project Rules

> Persistent instructions for Claude Code working in this repository.
> Every Claude session in `bruteX/` must read and honor this file.
>
> **Authority order:**
> 1. This file (`CLAUDE.md` — 12 Golden Rules)
> 2. Defaults
>
> The previous DEC-PROTOCOL-001 layer (tickvault adaptation doc +
> auto-loaded protocol bundle) remains RETIRED (DEC-P0-026, PR #48).
> The ONLY co-development surface re-established since is the shared
> `rust/trading-core/` crate — the single source of truth for the
> evaluate-time indicators (pivots + Fibonacci) + the 52-condition
> vocabulary, consumed bit-identically by BOTH BruteX (via the PyO3
> `trading_core` wheel) and TickVault (native Rust) per
> DEC-SHARED-CORE-001 (MIGRATION.md #441). The frozen 400-case golden
> answer-key is the contract both sides satisfy; no other
> adaptation/protocol coupling exists.

_Last updated: 2026-07-11 (DEC-OPERATOR-ACCEPTANCE-VERIFY-001, MIGRATION.md #1756 —
`brutex verify-acceptance`: the ONE operator-runnable acceptance verifier — 7 composed
checks (uniqueness census · derived-TF correctness incl. the NEW 1day compare · zero-vendor-call
incremental-pull re-plan idempotence · disk-provable rate evidence · movers page-vs-DB truth via
the ONE AST-FENCED independent recompute · stored-data sanity · on-box latency probes) over the
REAL lake, PASS/FAIL/ABSTAIN(named-reason) with mandatory evidence blocks + required coverage{}
disclosure, a GATING <10 s teeth smoke (3 planted poisons on a real-writer twin — a verifier that
cannot demonstrate detection withholds ALL real-lake verdicts, exit 2) + the CI-mandatory 20-poison
full battery, the frozen `brutex-acceptance-report-v1` machine+human report under
`<lake>/acceptance/<run_id>/` (atomic LATEST pointer + SweepAuditWriter hash chain + run-over-run
deltas/abstain streaks carried in the report chain), verify-family exits 0/1/2/3/4 (precedence
2>3>4>0; structural abstains {no_per_call_persistence, no_per_call_log, no_oi_compare_seam} are
budget/streak-EXEMPT so the standing by-design gaps never turn the nightly permanently amber).
ADDITIVE ONLY — NEW `brutex/acceptance/` package + thin CLI shim + ci.yml/mutation.yml gates;
ZERO edits to serving paths/engine/pull/movers/db_view/ledger/doctor; compose-never-reimplement
(the fenced movers leg is the single sanctioned exception, DEC-SURFACE-TRIAL-001 precedent).
HONEST: rate.per_call_history is a PERMANENT named ABSTAIN (no per-call record persists — F3
needs an operator ruling); tf.oi stands ABSTAIN until F2; sampled PASSes say so on the verdict
line. HOSTILE-REVIEW ROUND 1 folded pre-merge (refuter-confirmed findings fixed: the §12.7
TOCTOU law WIRED for real · per-day Muhurat 1day handling · the full moneyness sentinel
grammar · expiry-capped covered-span replan · instruments.duckdb opened READ-ONLY only ·
glob=False oracle reads · bounded 16 MiB evidence tails, chain-verify dropped from the
evidence sub · `--months` made real for tf.daily/sanity + the census scoped to sampled
coordinates — the per-round record lives in MIGRATION.md #1756, never this header).
HOSTILE-REVIEW ROUND 2 folded pre-merge (the coverage-gate test-stem BLOCKER renamed ·
the registry.duckdb schema-drift ABSTAIN guard · the replan oracle's correct-or-abstain
non-default-window-shape routing (explicit-dates F&O + overlapping-anchor false-FAILs dead,
the out-of-coverage double-fetch FAIL kept) · the pull TOCTOU retry genuinely re-reads ·
Muhurat-date 1day bars disclosed UNVERIFIED + counted once · the instrument-master read
became an in-memory SNAPSHOT so the in-process #1482/#1646 drift ladder stays live ·
seed-rotated F&O contract sampling + bounded/pruned coverage ledger — per-round record in
MIGRATION.md #1756, never this header).
HOSTILE-REVIEW ROUND 3 folded pre-merge (stdout-pure `--json`/table via the #1630 logging
seam · the evidence-tree symlink refusal hoisted before the audit write · BOTH serving-path
residuals disclosed (registry + `.manifest/`) · the engine set made code-true (INDIAVIX +
NSE-NIFTY-*-FUT dirs full-history) · off-chain `--out` · loud SUSPECT on a failed report
write · report.txt renders limits/routed/Muhurat — per-round record in MIGRATION.md #1756,
never this header).
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; the per-item record lives in the §1
Active-branch blurb + MIGRATION.md #1756.)
Earlier: 2026-07-11 (DEC-FRONTEND-LATENCY-HARDENING-001, MIGRATION.md #1720 —
the dashboard FRONTEND latency-hardening pack: the v29 operator page's DUPLICATE
pollers consolidate onto ONE BXPoll-disciplined consumer per endpoint (subscriber
fan-outs added to bx-oneclick.js / bx-rate-monitor.js / bx-liveproof.js; the
measured BEFORE on the idle operator page was 336 requests/120 s with
sweep-progress / pull-rate / liveproof each polled by TWO-THREE summed pollers;
measured AFTER, same harness: 240 requests/120 s — −96, −28.6%),
the six fixed-interval template pollers (coverage · spot_coverage · fetch_windows
· completeness · expired_options 4 s + launch 2.5 s) migrate onto the ONE shared
`BXPoll.createPoller` core via a custom-fetchJson tick seam (renders
byte-identical; failed ticks back off 3 s→30 s; DOMContentLoaded start;
guarded-legacy degrade), the 8 /screener filter inputs + /results Top-N debounce
200 ms, completeness/spot_coverage adopt the in-place tbody row-patch idiom (ONE
shared esc()'d row builder, shell built once), and /reports' ~750 chips build
ONCE with class-toggle selection. SKIPPED honestly: the bx-format/bx-movers-time
defer (end-of-body includes with genuine parse-time consumers + a standing
non-defer pin) and the universe.html integrity-interval catch-up (already shipped
by #1623). NO measured hidden-tab win claimed (idle-page hidden was already
0 req/s, measured); the `pollRunSweep` hidden-guard gap is source-asserted only.
CLIENT-side only — templates + static JS + tests + docs; ZERO route/payload
change, byte-stable wire; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; the
per-item record lives in the §1 Active-branch blurb + MIGRATION.md #1720.)
Earlier: 2026-07-11 (DEC-DB-LATENCY-HARDENING-001, MIGRATION.md #1719 —
the DB/query-latency hardening pack, READ-side only: (1) the `/db`
window-ledger pager rides a DuckDB LIMIT/OFFSET fast path over the #1533
delta-folded ledger scan source (`db_view.query_window_ledger_page` —
measured 7,321–8,778 ms cold at 400k rows → 564.8 ms; the O(rows) Python
record fold per stamp is GONE; the byte-identical legacy parse stays the
degrade for legacy-shape / NULL-derivable-cell / persistent-duckdb-error /
no-conn); (2) the pre-open export rows are memoised per (basis, range,
symbols) at the write generation (repeat click 326–574 ms / 206 DuckDB
statements → 4.2–18.6 ms / 0 statements, bytes identical) + the IEP (open)
reads are span-bounded (the CLOSE maps deliberately stay whole-partition —
the #1649a hole diagnostics need the unbounded view); (3) the `/db` candles
%CHNG baseline binds the page's own month files instead of the whole glob
(72 files bound twice per click → 2; the #863 timestamp bounds pruned ROW
GROUPS, this bounds the Parquet FOOTER opens too — merged-tree mechanism =
main's #1670 `_bars_file_windows`, this pack's interim arm superseded at the
merge-reconcile, behavior pins re-run green). HONEST: the
window-ledger fresh-stamp click stays ONE O(total rows + delta footers)
columnar scan (never literal O(page) — the repeat click is the O(1)
handoff); the ONE behavior-visible class is corrupt-lake-only — a corrupt
file provably OUTSIDE a bounded read's candidates no longer blanks it (a
strictly-narrower degrade, named in #1719). ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change;
healthy-lake payloads byte-identical (fast-equals-legacy pinned);
`db_view.py` + `preopen_report.py` 100% line+branch on their ci.yml gates;
the per-fix record lives in the §1 Active-branch blurb + MIGRATION.md
#1719 — this header carries the one-line summary, never the story).
Earlier: 2026-07-08 (DEC-MOVERS-EXPLICIT-BUCKET-ASOF-001, MIGRATION.md #1601 —
an explicit `/movers` bucket-anchor TIME pick now resolves to the COMPLETED bucket
(broker-matching close) instead of a PARTIAL forming bucket. The TIME dropdown on a
coarse timeframe offers only the 09:15-anchored bucket-ANCHOR grid, but the backend
treated a picked anchor as an as-of INSTANT, so 3-minute @ 15:27 showed the partial
15:27 minute close (₹27,932.80) rather than the completed 15:27–15:29 bucket close
(₹27,940.85 = the broker's = 2min@15:29 = 1min@latest); the stored derived-TF partitions
were always CORRECT, only the read-time as-of resolution was wrong. FIX: ONE COMMON as-of
seam `movers_snapshot.resolve_asof_cutoff(interval, time)` (consumed by all three tabs)
resolves an anchor pick FORWARD to the bucket's final minute `min(anchor + minutes − 1,
15:29)`, and the downstream `<= cutoff` `filter_to_ist_minute` caps at the day's latest
available minute (NO look-ahead). TF=1minute identity, "Latest"/blank whole-day
byte-identical (DEC-MOVERS-ASOF-LATEST-DEFAULT-001), non-anchor picks keep as-of-instant
semantics, prev-close %CHNG untouched; additive `asof_through` payload echo + note
provenance. Value TF-invariance (DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001) now holds PER
shared *resolved* as-of. READ-ONLY dashboard — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX);
no lake/schema/scope/vocab/rate change; movers_snapshot/movers_futures/movers_options 100%
line+branch; ruff/ruff-format/`mypy --strict` clean, pyright at the pre-change baseline.
Review-round history is deliberately not narrated in this header — each DEC's
per-round record lives ONLY in the §1 Active-branch blurb + its own
MIGRATION.md entry.
Earlier: 2026-07-09 (DEC-MOVERS-FUTURES-HYPHEN-PARSE-001, MIGRATION.md
#1646 — the `/movers` Futures contract-symbol parse is RIGHT-anchored via the
ONE canonical grammar: `movers_futures.parse_future_symbol` now DELEGATES to
`data/fno_symbol.parse_fno_symbol` (FUT-restricted; the local left-anchored
twin — `_parse_expiry_token` + its constants — DELETED), so a hyphenated
underlying (`NSE-BAJAJ-AUTO-30Sep25-FUT`) parses to `BAJAJ-AUTO` instead of
the `parts[1]` truncation `BAJAJ` that made `_underlying_spot_ltp` probe the
nonexistent `bars/NSE/CASH/BAJAJ/` — spot + premium were SILENTLY dropped for
every hyphenated F&O underlying (executed real-lake repro: spot 8010.00
present under the correct key, served row `spot=None`) and the wrong rows were
persisted as warm-grid cache hits. HONEST disclosed delta (sanctioned, junk
tokens only): the deleted twin was shape-stricter than the canonical
`strptime("%d%b%y")`, so a junk lake dir like `NSE-NIFTY-30SEP25-FUT` flips
reject→accept — exactly what options//db/Greeks already accepted; every
well-formed vendor `DDMmmYY` symbol byte-identical, non-hyphenated rows
byte-parity-pinned end-to-end, `&`-names (M&M — no hyphen in the grammar) were
and remain correct. `movers_options` already delegated; `futures_warm_grid`
needed zero change. SAME-PR FOLD-IN (W3 spot-drift ladder): the movers
underlying-spot probe now walks the #1482 (DEC-GREEKS-SPOTJOIN-STOCK-001)
drift-recovery ladder on an exact-key MISS — NEW
`movers_futures._SpotLtpResolver`, ONE per-candidates-compute instance in
BOTH the futures and options loops: exact probe byte-unchanged FIRST; the
COMMON `lake_keys.spot_recovery_candidates(..., stock_drift=)` ladder only on
a miss, fed by a LAZY at-most-once `spot_drift.load_stock_drift_map`
(master-verified; ambiguity POISONED + capped warns; no-master fail-softs to
today's honest no-spot; MemoryError re-raises), the recovery OUTCOME memoised
per (exchange, underlying) with ONE rung-attributed
`movers.spot_key_recovered` INFO — so a spelling-drifted stock spot dir
(chain `BAJAJAUTO` vs on-disk `BAJAJ-AUTO`, executed repro) no longer blanks
futures spot/premium or reads `?` options moneyness; a resolving lake never
builds the map (spy-pinned byte-identity); NO `%26`→`&` recovery — the #1482
pledge stands; read-time recovery only, nothing healed on disk. READ-ONLY
dashboard display fix — ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change;
`movers_futures.py` + `movers_options.py` BOTH 100% line+branch.)
Earlier: 2026-07-09 (DEC-GROWW-MARKET-HOURS-GUARD-001, MIGRATION.md #1692 —
renumbered #1633 → #1673 → #1692 (#1633 → #1673 at the 2026-07-10 merge-reconcile —
main's in-flight renumber cascade holds the reservation band through #1672 — then
#1673 → #1692 per the coordinator reservation map, #1673 reserved for PR #1369) —
the NSE market-hours pull guard: NO Groww VENDOR pulls between 08:45 (inclusive)
and 15:35 (exclusive) IST on a trading day; an in-flight nightly pull STOPS
gracefully at 08:45 and start/resume is REFUSED until 15:35, keeping the SHARED
Groww account free for TickVault's live feed (the shared TOTP token + 500/min
governor, §2 Rule 6). NEW pure leaf `brutex/calendars/market_hours.py` —
`BLOCK_START=08:45`/`BLOCK_END=15:35`, the clockless predicate `pull_allowed_now`,
`pull_allowed_default` (binding `now_ist()` + the NON-raising `is_trading_day()`;
an out-of-holiday-coverage WEEKDAY reads as a trading day → BLOCKS = the FAIL-SAFE
posture protecting the shared account; a weekend → ALLOWED), the fail-LOUD env
kill switch `BRUTEX_MARKET_HOURS_GUARD` (default ENABLED), `guard_active_default`,
and a `now`-injectable `MarketHoursGuard`. NEW `MarketHoursHalt(BruteXError)` in
`data/errors.py` added to BOTH ingest spines' `_GLOBAL_STOP_ERRORS` +
`pull_jobs.is_pull_halt` (propagates past per-symbol/contract quarantine, fires the
CRITICAL Telegram `emit_pull_halt_alert`). START GATES: `BackgroundPullJob.start`
defers a `JOB_KIND_PULL` in-window UNLESS `market_hours_exempt=True` — the THREE
vendor-free exemptions are the sweep (`job_kind != PULL`), the dashboard
Greeks-backfill start and every dashboard dry-run pull start (both pass
`market_hours_exempt`); `cmd_fetch` defers a LIVE pull at the START gate with a
clean EXIT_OK (dry-run/inspect EXEMPT). MID-RUN (the CRITICAL requirement): a
`market_closed` predicate threaded to the PER-WINDOW seam of both spines raises
`MarketHoursHalt` (the durable window ledger leaves PLANNED/FAILED windows
RESUMABLE — no new checkpoint); `run_until_clean` gains a `market_closed` hook →
NEW `stop_market_hours` verdict; `pull_all_spot`/`pull_contract_candles` catch it
mid-round + belt-break later steps + the minute phase (loud
`pull.step.halted reason="market_hours"`); the METADATA phase
(`pull_expiries_contracts`) gets a per-cell belt (raises `MarketHoursHalt` before
each `get_expiries`/`get_contracts`; `run_two_phase` re-raises it, not
record-and-resume); `run_full_pipeline` re-checks the belt BEFORE metadata AND
before historical + wraps `run_metadata` in `try/except MarketHoursHalt`, skipping
the rest of the vendor chain with `stopped_after="market_hours"`; and the CLI
`brutex fetch` path is MID-RUN-guarded too — `cmd_fetch` threads `market_closed`
into the spot spine + the between-rounds `run_until_clean`, catches
`MarketHoursHalt` (drains + persists the ledger → resumable), and exits EXIT_OK.
The mid-run env kill switch is resolved ONCE at pull start via
`resolve_market_closed()` (fail-LOUD on junk at START; the bound predicate reads
ONLY the live clock per call — so a junk env written MID-pull can never crash a
long run with a per-window re-parse); the one-shot START gates keep
`guard_active_default`. Production entrypoints (dashboard routes + the nightly
headless binding — the MOTIVATING 01:30 case covered) bind `resolve_market_closed()`;
a dry-run / un-wired caller passes `None` (byte-identical). NOT edited:
`_start_auto_resume_on_boot`/`cmd_dashboard` (auto-resume covered transitively by
the start-gates; conflict-free with PR #1343). HONEST: the CLI + metadata paths
are now mid-run-guarded (not merely start-gated); the Groww F&O guard is
per-CONTRACT granularity — a contract already started at 08:45 finishes its few
small remaining windows before the next contract's belt stops the pull (an
accepted bounded overrun, never a whole extra symbol); a pull interrupted at 08:45
DEFERS to the NEXT nightly, NOT a same-day 15:35 re-arm (named follow-up).
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; pull-CONTROL only (no fetch/rate/pool/
governor change); `market_hours.py` + `data/errors.py` + `universe_phases.py` +
`metadata_pull.py` + `pipeline_pull.py` + nightly binding 100% line+branch, the
CLI/dashboard touched lines covered by the whole-package `--cov=brutex` gate;
ruff/ruff-format clean, `mypy --strict` clean, pyright at the pre-change baseline; the
per-round record lives ONLY in the §1 Active-branch blurb + MIGRATION.md #1692 —
this header carries the one-line summary, never the round-by-round story).
Earlier: 2026-07-08 (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 + DEC-FUTURES-ADDRESSING-PIN-001 +
DEC-STAGE1A-ROLL-RESET-001 + the round-1–8 review-fix packs DEC-FUTURES-LAUNCH-REVIEW-FIXES-001…-008,
MIGRATION.md #1700–#1709 + #1710 — the ADR-0012 NIFTY-futures sweep LAUNCH
capstone (PR-5): the futures track is addressed by ONE frozen series alias **`NSE-NIFTY-FUT`**
(symbol_id 4 in the append-only `_SYMBOL_ID_TABLE`; the two pre-launch shapes
`NIFTY-FUT-YYYY-MM-DD` + `NSE-NIFTY-<DDMmmYY>-FUT` are RETIRED with a did-you-mean refusal —
supersedes the #1178 dual-acceptance envelope, satisfies ADR-0016 §6 + the #1138 [C16]
reservation in one PR). NEW `brutex/data/futures_series.py` resolves the alias from the VENDOR
lake dirs (scan + `parse_fno_symbol`, never `strftime`) into disjoint IST-date ownership windows
(`(E_{k-1}, E_k]` — expiry day owned by the EXPIRING contract, the `resolve_near_month` `>=`
law); interior holes REFUSE loudly naming the missing month(s) — INCLUDING a window that STARTS
inside a hole (the first-selected-segment predecessor gap gate, review-round-1 #1703) — a head
span stays UNOWNED (WARNed; never a backward ownership extension serving far-month bars), ANY
positive tail overhang WARNs, the calendar is an OBSERVE-ONLY cross-check (drift WARNed with
evidence, 2027+ coverage abstains — never load-bearing). NEW `brutex/feed/series_load.py` is the
ONE alias-aware sweep-slice loader every sweep mouth now uses (spot path = the old inline
reconcile+ReplayFeed block moved verbatim; the alias is stitched per contract with the exact
`astimezone(IST).date()` ownership cut stage1a keys on; a zero-bar boundary sliver holding NO
expectable session instant SKIPS with one WARN — the loud EMPTY refusal stands for spans the
NSE calendar says should hold bars, review-round-1 #1703). Stage-1A B2 CLOSED
(DEC-STAGE1A-ROLL-RESET-001): a `roll_schedule` of owning-expiry day-ordinals keys the VWAP
session per contract and a key change triggers a TOTAL hard reset of EVERY stateful member
(VWAP, MarketStructureState, EMA20/200, prior-history, day extremes, pivots/daily-history/
anchors — D11: no level ever crosses the roll basis gap; post-roll state == fresh-builder state,
proven bit-for-bit); a bar beyond the schedule raises (never a silent constant-key fallback);
`apply_1day_mask` honestly clears ToD 44-47 + VWAP 52-53 on volume-bearing futures `1day` grids
(D12; spot deliberately NOT flipped — named follow-up). `roll_schedule` conditionally enters
`canonical_trade_params` under a hard instrument⟺schedule `__post_init__` invariant (presence ⟺
the alias, both directions — the #1062 bug class contained; spot run_ids byte-identical, the
unmodified golden suite is the proof); recount compares recorded-vs-re-derived schedules with
evidence. ENGINE surface now EXACTLY {NIFTY/BANKNIFTY/SENSEX spot} ∪ {NSE/FNO/NSE-NIFTY-FUT};
all else REFERENCE_ONLY; no vocab/rate/lake-schema change. Honest: resolution is O(FNO dirs)
once per launch, NOT O(1); D6 map, D14's DSR effective-N half (only MIN_TRADES=30 gates at
launch), the #1013 D8-stamping consumption, futures→options translation (PR-6), and the spot
1day mask audit are NAMED follow-ups. Cascade manifest schedule-carriage is CLOSED by the
round-1 review-fix pack (#1703): the automatic Stage-4 cascade threads
`roll_schedule_by_instrument`, the manifest carries `roll_schedules` for the MANUAL replay, and
a schedule-less futures pool SKIPS loudly — never a schedule-blind re-score).
Earlier: 2026-07-10 (DEC-ONECLICK-PERF-CLUB-001,
MIGRATION.md #1711 — coordinator-ASSIGNED from the reservation map
(next free above the #1264 reservation tail #1710; live-main max #1665
at assignment — gaps are legal; renumbered from the interim provisional
#1666) —
the queued post-#1373 oneclick perf/hygiene club on
`claude/oneclick-perf-club` (built on PR #1373's head 3c478831; the PR
opens after #1373 lands): (F1) `/api/v1/oneclick/verify` — the ONE
oneclick route with no revision cache — joins the stat-token family (key
folds manifest + events + top100 tokens + the chain verified-state; a
primed poll is three stats + the chain cache-peek + one hit; NEW B-7
bench pin,
measured 0.95× flat vs a 1000× manifest); (C-r2-5) ALL FIVE cached
oneclick routes ride the NEW `serve_revision_cached` chokepoint — a stat
OUTAGE (now distinguished from honest ABSENCE by `artifact_probe`) serves
the LAST-KNOWN payload with ONE `oneclick.probe_outage` WARN per outage
per COORDINATE (a whole-volume flap warns once per touched coordinate;
never a silent per-poll O(artifact) re-parse; recovery logs + resumes
normal keying; `OutageGate` LRU-bounded 512); (F4) `discover_runs`
stat-stamps + sorts FIRST and parses only the newest-limit manifests
(identical selection; the F5 uncapped runs_revision stat walk stands);
(A-r4) the /screener render/filter/sort surfaces are client-side BOUNDED
via node-goldened BXScreener primitives (Show all itself capped at 1000;
input cap 5000; every truncation NAMED on the count line) — the backend
half was #1373's TOP100_ROW_CAP; F2b (serialized-response caches)
deliberately SKIPPED as optional polish per the severity correction.
DISPLAY zone only — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX);
`oneclick_artifacts.py` 100% line+branch on the ci.yml-exact gate; the
per-item record lives in the §1 Active-branch blurb + MIGRATION.md #1711.)
Earlier: 2026-07-09 (DEC-ONECLICK-WINNER-DETAIL-TRUTH-001 +
DEC-ONECLICK-INSTRUMENT-DISPLAY-001, MIGRATION.md #1664–#1665 —
renumbered from #1632/#1633→#1633/#1634→#1664/#1665: #1633–#1636 were
#1323's reserved block (landed on main 2026-07-10), #1664/#1665
coordinator-assigned from the in-flight reservation map —
the strategy-detail honesty pack on `claude/oneclick-instrument-display`
(built on PR #1352's head; the PR opens after #1352 lands),
viewer-reads-storage ONLY (every rendered value verified against
`top100_artifact.py`): (#1664) the winner's `instrument` reaches every
one-click surface — ONE pure `bxInstrumentText` builder feeds the operator
rank-1 card (`r1_instrument`), the combos Inst cell (the hardcoded "—" +
FALSE "not a top100 row field" comment KILLED; `fInst` options repointed to
the ENGINE tokens), the /strategy identity strip (`stInstr`), and /screener
(config-driven `instrument` column + a direction-style exact-match filter
whose options are harvested ONLY from rows carrying the key); the
metric-key-map audit concern was VERIFIED already covered by #1631 with
zero residue — no duplicate work. (#1665) the gross relabel — the signal-only
P&L reads "Gross signal P&L" + the persistent "signal-only — costs modeled
in the options-translation track" chip on the /strategy card AND the operator
hero (killed-token-pinned); the SHIPPED results-depth aggregates render in
the NEW /strategy Deep-stats section (expectancy · recovery factor · loss
streak · MAE/MFE pairs · minute-path DD · exit mix · worst-5-days — worst5
is a METRICS key, the audit refinement) via six pure node-goldened builders
with honest "—" degrades + Beginner block-toggle summary; the regime
breakdown (one_regime_wonder/axis + by_trend) renders on BOTH cards and the
stale "lands with the P3 data" deferral is DELETED; the equity SERIES
sidecar gains `GET /api/v1/oneclick/equity` (the #48 overlap — Protocol
`equity_page` on all three readers, the ONE `_resolve_sidecars` chain,
display-zone parquet read with the honest O(equity-file)-first-read /
O(page)-memo envelope, clamp [1,5000] NAMED, null lows pass through) and the
/strategy equity host renders the stored series labeled "minute-path equity —
the stored real series (R27)" with the per-trade cumsum RELABELED as the
honest fallback (the operator host stays per-trade — the #48 follow-up); the
`/strategy?run_id=&rank=` deep-link contract is PINNED for the /results-side
producer; nav.py NAV_LINKS gains /completeness · /screener · /strategy
(12 template navs updated, the unreachable-page class closed); the two
chart-mapper finder NITs land (`bxBarPrice` — no `Number(null)→0` candles;
skipped rows COUNTED + surfaced "N row(s) skipped (unreadable)"). HIGH #40
composite_score /results join stays DEFERRED (results-storage seam,
coordinator brokering). DISPLAY zone + one additive display route — ENGINE
UNCHANGED (NIFTY/BANKNIFTY/SENSEX); engine writers byte-untouched; both
oneclick modules 100% line+branch standalone; ruff/ruff-format/`mypy
--strict`/pyright clean; `node --check` clean. Any per-round record lives
ONLY in the §1 Active-branch blurb + the MIGRATION.md entry — this header
carries pointers, never round narration.)
Earlier: 2026-07-09 (DEC-PREOPEN-PREVDAY-COMMON-002, MIGRATION.md #1649a — renamed from -001 at the 2026-07-10 merge-reconcile (main's #1634/PR #1323 claimed -001 for the minute-basis calendar-cross-check ADVANCE; -002 is the COMMON-ization remainder + hole-shape observability + the #1649b Muhurat pack) —
renumbered #1623→#1631→#1649a across the 2026-07-09 merge-reconciles (main took
#1623 for DEC-DASHBOARD-POLL-HYGIENE-001, then #1631/#1632 for
DEC-ONECLICK-TOP100-OBJECT-ROOT-001 + DEC-DASHBOARD-VISUAL-OVERHAUL-001; the
613a/613b both-suffixed pair convention keeps the companion entries adjacent);
the /reports preopen MINUTE-basis calendar-contiguity fix + the #1539-named
follow-up CLOSED: the minute-basis PREV. CLOSE anchored on the symbol's OWN
1-day series with ZERO real-calendar check (its docstring called a real NSE
session absent from BOTH series "a genuine holiday" — the confirmed repro
fabricated pct_chng=+10.0 for 2024-01-04 across the missing Wednesday
2024-01-03, and select_top_movers would have crowned it); the minute guard is
now the exact structural mirror of the daily guard — the checked calendar is
the prior-session AUTHORITY, refusal = honest row ABSENCE ("correct-or-absent,
never wrong"; never a fabricated %CHNG, never a 500), surfaced as ONE
`preopen.prev_close_noncontiguous` WARN per (exchange, segment, symbol)
dataset triple (capped 20/build + one suppressed summary) carrying a
machine `hole_shape` + shape-matched detail (the hostile-review
fold — the original one-line "re-pull" advice was WRONG remediation in two
reproduced shapes; since the DEC-PREVSESSION-MUHURAT-COHERENCE-001 amendment
fold, MIGRATION.md #1649b, the split is FOUR-WAY): muhurat_session_bar (the
calendar-confirmed Diwali boundary — a HEALTHY lake, "no action needed",
cross-referencing the #1616 engine anchor law + /movers' prev_close_muhurat
display stamp) / extra_nonsession_bar (a non-Muhurat unexpected non-session
bar — honestly suspicious, inspect it) / prebuild_lag (the 1day series lags
the minute series — run the pipeline prebuild; diagnostic only, the guard
never anchors on minute presence) / missing_session (the genuine hole —
re-pull kept verbatim); the found<expected arms are two-way on the daily
basis (no minute series read). The same #1649b fold ends the annual /movers
false-hole flag: a day-after-Diwali Muhurat baseline now reads
contiguous-True + EMIT-ON-TRUE `prev_close_muhurat`/`oi_baseline_muhurat`
+ " †" + ONE INFO per build (never " *"/WARN), and /db gains the static
%CHNG anchor footnote. CONSOLIDATION: `nse.previous_trading_session` gains the
REMOVE-ONLY kw-only `observed` overlay kwarg (default frozenset() —
every existing caller byte-identical, parity-pinned) and
`preopen_report._previous_trading_day` becomes a THIN delegator over it
(private walk DELETED; delegation-equivalence pinned over the full covered
span; the module-local `calendar.coverage_exhausted` event RETIRED for the
COMMON `calendar.previous_session_coverage_exhausted` — a sanctioned,
consumer-free rename). SANCTIONED: hole rows disappear from minute-basis
exports (remediation now shape-matched — re-pull ONLY for a genuine
missing_session, the pipeline prebuild for a 1day-lag, none for a Muhurat
extra bar); coverage-abstain +
Muhurat-bar / unflagged-drift anchors now SKIP on the minute basis too
(parity with daily; an overlay-flagged day is recovered). HONEST: the
pure-minute no-1day fallback arm stays deliberately calendar-blind (a chosen,
pinned limit); the prior-session memo is O(unique report days) per build, NOT
O(1)-total. EVIDENCE: the finder's repro KILLED on the fixed tree; PRE-fix
goldens (weekend + Holi-2024 + Budget-Saturday contiguous fixture) byte-equal;
the `!=`→`==` mutant proven dead on both guards. READ-ONLY dashboard — ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change;
preopen_report.py + calendars/nse.py 100% line+branch; ruff/ruff-format clean,
`mypy --strict` clean, pyright 0 errors at the pre-change baseline).
Earlier: 2026-07-08 (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001, MIGRATION.md
#1594 — the automatic lake healer is COMPLETED for the
`options/<series>/<YYYY>-<MM>.parquet` expired-options table: foreign-month-row
+ overlapping-partition detection+heal on the SAME proven quarantine →
writer-locked-rewrite → hash-chained-audit → one-alert-per-batch machinery,
closing the DEC-LAKE-HEAL-001 #1175 named follow-up. Scanner: NEW
`OptionForeignMonthRows` + pure `classify_option_keys` (UTC month routing —
timestamp-only law, side is key payload; overlap via memoised canonical-
sibling reads — ONE per affected month per series, so a multi-month stray
costs one read per home month; clean file pays ZERO extra reads; `months=`
scope now bounds the
options tree BY FILENAME; NEW `include_bars=False` for the bounded L2 census;
census OPTIONS row gains REAL foreign/overlap counts, drift stays honestly
N/A). Writer: `OptionParquetWriter.heal_partition(remove_keys=…)` — RAW
stored-value keyed removal (naive-column immune, keyed never positional) +
wholly-removed quarantine-MOVE; NEW `read_partition_frame` +
`merge_partition_frame` over the ONE `_merge_frame_locked` union core (golden
byte-parity pinned). Healer: NEW `_heal_option_foreign` — copy-forward FIRST
(keep-first union home; canonical wins, drifted overlap copies removed never
restated), remove SECOND; 5-class heal order drift → foreign → dup →
option-foreign → option-dup; crash-convergent, observe/mass-rail/halt/audit/
alert/sentinel machinery reused with option files in the mass-rail
denominator; NO catalog/raw_hash/recall for options (uncatalogued; NOTHING
derives from `options/`). Wiring: L1 reconcile-piggyback is STRUCTURALLY N/A
for options (catalog-walk only — documented); L2 `heal_after_pull` gains
`include_options` (default False) wired True at the F&O post-pull seam ONLY;
L3 startup sweep rides automatically (zero change). Review-hardened across NINE refuter-confirmed
hostile-review rounds (a silent-row-loss merge-baseline BLOCKER, symlink/
TOCTOU containment MAJORs, isolation-escape crashes, surviving mutants,
log-injection guards — every fix pre-fix/mutant-verified); the per-round
record lives ONLY in the §1 Active-branch blurb + MIGRATION.md #1594 —
this header deliberately carries the pointer, never the story.
HONEST: no production
pull currently WRITES `options/` (legacy table; L2 opt-in is opportunistic,
L3 + census are the always-on cover); cross-file value drift resolved
deterministically (canonical/path-ordered-first wins, originals in
quarantine); rows move VERBATIM, never validated. ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; `lake_invariants.py`/`option_parquet_writer.py`/
`lake_heal.py`/`partition.py`/`errors.py`/`catalog_reconcile.py` ALL 100%
line+branch; ruff/ruff-format clean,
mypy `--strict` + pyright at the pre-change baseline).
Earlier: 2026-07-09 (DEC-MOVERS-UNDERLYING-CLASS-001, MIGRATION.md #1650 —
the `/movers` page gains an `All | Indices | Stocks` underlying-class filter on
all THREE tabs (Spot / Futures / Options), mirroring DEC-MOVERS-MIN-VOLUME-001
(#1034) EXACTLY: ONE COMMON `brutex/dashboard/movers_underlying_class.py`
(`normalize_underlying_class` blank/unknown → `all`; `candidate_passes_class`
predicate; `filter_by_underlying_class`; EMIT-ON-non-default note/echo helpers)
applied at RANK time in `build_*_payload` over the SAME cached candidate set —
never a re-scan. Classification is viewer-reads-storage-only: SPOT from the lake
segment (`INDEX`→index / `CASH`→stock, the stored `is_index` reflection);
FUTURES/OPTIONS from a `{underlying → kind}` map REUSING the six-step pull
order's `primary_fno_underlying` + `is_index` classification (#1527, never
re-minted). Unresolvable → `unknown`: in `All`, EXCLUDED from Indices AND
Stocks, and COUNTED (`excluded_unclassified` echo + note suffix). Each candidate
gains a DEFAULTED additive `underlying_kind` field (never row-emitted), so the
default `All` payload is BYTE-IDENTICAL (pinned all three tabs). DELIBERATE
DEVIATION (ledgered honestly): the class is a RANK-time filter over the SHARED
cached set, so — unlike the #917 expiry tier — it is NOT in `candidate_key`
(threading it would triple-cache an identical set); cache-independence is proven
by a route test (Indices vs Stocks over one coordinate → different rows,
`movers_cache.size()==1`). ADVERSARIAL-REVIEW FIX ROUND folded (same branch,
pre-merge, MIGRATION.md #1650): the F&O class lookup now NORMALISES the parsed
token into the map's `primary_fno_underlying` key space (a hyphenated/aliased
underlying — `BAJAJ-AUTO` — previously misclassified `unknown` and dropped from
BOTH classes; `parse_future_symbol` made right-anchored + hyphen-preserving,
mirroring `parse_fno_symbol` — sanctioned display/spot-probe corrections); the
kind map AND the stamped F&O candidate cache keys fold the `master_revision`
CONTENT token via ONE `_underlying_kinds_cached` helper (a keyless
load-instruments now refreshes classification with NO generation bump; the
post-pull warmer threads the kinds + shares the composite key — warmed rows were
previously all-`unknown`); full frozen default-payload equality pinned on all
three tabs; the head's CI pyright red (a mis-annotated `-> TestClient` test
helper) fixed — the per-round record lives ONLY in the §1 Active-branch blurb +
MIGRATION.md #1650; this header carries the one-line summary, never the
round-by-round story. DISPLAY-zone read-only — ENGINE UNCHANGED
(NIFTY/BANKNIFTY/SENSEX), F&O lake REFERENCE_ONLY, no scope/vocab/rate change;
`movers_underlying_class.py` + `movers_snapshot.py` + `movers_futures.py` +
`movers_options.py` ALL 100% line+branch; ruff/ruff-format clean, `mypy
--strict`/pyright clean incl. `app.py`.)
Earlier: 2026-07-09 (DEC-ONECLICK-TOP100-OBJECT-ROOT-001, MIGRATION.md #1631 —
the one-click `top100.json` READER-ADOPTS-WRITER fix for the 3/3
refuter-confirmed BLOCKER: the SHIPPED engine writer
(`top100_artifact.write_top100`, default-ON in `cmd_sweep_all`) emits an
OBJECT root `{schema_version, run_id, preset, source, baselines, entries}`
while the display reader (`oneclick_artifacts.load_top100`) demanded a bare
ARRAY — every REAL sweep read "corrupt · 0 strategies" on /operator ·
/screener · /strategy while CI stayed green on hand-written bare-array
fixtures (the fixture-only blindspot). The reader now parses the writer's
object root (unknown FUTURE schema_version → NAMED corrupt, never a crash;
the retired pre-#1621 bare-array form → NAMED corrupt — deliberately NO dual
shape), adds per-row SUB-KEY shape validation (named partial degrades), and
applies the ONE additive reader-boundary metric map `TOP100_METRIC_KEY_MAP`
(`max_drawdown_paisa`→`max_dd_paisa` · `sharpe_ratio`→`sharpe` ·
`deflated_sharpe`→`dsr` · `total_trades`→`trades`; the writer-absent
`cpcv_pass_pct` stays ABSENT → honest "—", never a fabricated 0 — the
screener-band-filter drops-every-row blast radius node-pinned); `preset` is
surfaced for the pages' sort-basis labels. Fixture corpus REGENERATED from
the writer shape (object root + engine metric names; SHAs refreshed
deliberately); NEW writer→reader golden drives the ACTUAL `build_top100` →
`write_top100` → `load_top100` → routes chain. Fold [11]: visible sort-basis
labels on /results AND the one-click surfaces (labels only, neither sort
changed; the preset tie-break divergence itself is RULED design-sanctioned —
inert axes wire-disclosed per #1617 Ruling 10, the /results UI sends no
`?preset=`, negative-pinned). Folds [W2-4]/[W2-5]: Ruling-18
`costs_paisa:null` no longer folds into the non-integer bucket (equity +
R44 arithmetic count null costs at ₹0 with the honest "gross of those
costs" caveat — 3 real rows previously read an EMPTY curve / "0 trades");
the /screener count line's numerator is the RENDERED count, naming the
render cap. Coordinator ruling folded: the /operator v24.4 "LIVE"
hook-banner ticker (16 hardcoded fabricated trade ticks) is DELETED
outright — the honest `#bxTickerStrip` (real runs payload) is the one
ticker kept, killed-token-pinned. Fold [12]: `runs_revision` folds per-run
manifest stat tokens so an in-place `suspect` rewrite moves the cached
runs-listing key (O(run dirs) stats per poll, stated). Wave-3 fold: [W3-R1]
the /strategy candle chart was DEAD (mapper read `idx.ts`; the real
/api/v1/db/candles column is `timestamp`) — fixed via the ONE route-shape
mapper `bxCandleRowsFromDbPayload` + TZ-safe `bxParseTsIst` (naive API
strings pinned to +05:30 IST; arrow placement node-proven TZ-invariant under
TZ=America/New_York) with a REAL-route integration golden; [W3-R4] a
null-VIX writer band no longer renders "between null–null" on any of the
three band sites — ONE shared builder pair reads the honest "VIX band not
stamped for this run" / partial-bound copy; [W3-R3] the /strategy arm
"[object Object]" repro is VERIFIED dead (covered by the f1d271d4 reader
bridge; route-level proof added). DISPLAY zone only —
ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), engine writer byte-untouched;
both oneclick modules 100% line+branch standalone; ruff/ruff-format/`mypy
--strict`/pyright clean. Any per-round record lives ONLY in the §1 Active-branch blurb
+ the MIGRATION.md entry — this header carries pointers, never round narration.)
Earlier: 2026-07-07 (DEC-LAKE-STATS-MANIFEST-001, MIGRATION.md #1614 —
write-time PER-FILE lake STATS manifest: NEW leaf
`brutex/data/lake_stats_manifest.py` (one atomically-replaced JSON shard per
dataset at `<lake_root>/.manifest/stats/…` — per-file DISTINCT-identity-key
row counts + timestamp bounds + `mtime_ns:size` stat tokens captured under
the still-held partition flock), the per-file stats/paging EXTENSION of the
shipped DEC-LAKE-MANIFEST-O1-001 (#1480) dataset-DISCOVERY manifest —
claiming the #867/#1025 named future work: the `/db` candle +
expired-options pagers serve totals / pages / stored-life spans
MANIFEST-FIRST in O(page-files), served ONLY after full per-request
revalidation (parse + key echo + sum invariant · file-SET equality ·
per-file stat-token equality · month-bounds sanity; ANY failure ⇒ the
byte-identical legacy live scan + one fallback log + read-side reseed;
post-query TOCTOU re-stat; a manifest total is NEVER paired with a degraded
page). Write hooks at all five partition-locked chokepoints (spot writer
both branches + heal rewrite/quarantine + option writer merge/heal +
`lake_heal._quarantine_move`) are best-effort + incremental + LAZILY
accreted (absent shard = one cheap probe: key digest + one failed `os.stat`,
probed FIRST — round 2; the prebuild storm costs one probe per write;
seeding is read-side only), keyed on the NEW explicit
`WriteResult.file_rewritten` (bytes RENDERED, never `bars_added`). Kill
switch `BRUTEX_LAKE_STATS_MANIFEST=off` ⇒ byte-identical legacy scans
(modulo the sanctioned unconditional ORDER BY tie-order pin).
HONEST: a hit is O(page-files) Parquet work + an O(D) per-consult
revalidation stat sweep (D = the ONE dataset's month files) — NOT literal
O(1); the FIRST star-select click per (generation × file-state) also pays
one O(D)-footer union-order DESCRIBE (memoised); the first view per dataset
pays today's O(files) once (the seed), and a permanently-REJECTED seed
(un-healed poison / 0-row parquet) is LATCHED per (dataset, generation) so
the doomed scan never repays per click — review-hardened across the refuter
rounds + the post-merge ENDGAME reconciles (LakeWriteHalt-safe reads ·
probe-first hooks · bounded 16 MiB shard read · FIFO-shard hang fix · the
EXECUTED m4pro run 28888100079 A/B gate + 3.0× paging strict-tier
recalibration · the wired-and-executed zero-survivor mutation gate); the
per-round record lives ONLY in the §1 Active-branch blurb + MIGRATION.md
#1614 — this header deliberately stopped narrating rounds after freezing at
round 2 once (endgame round 2); a same-`(mtime_ns,size)` in-place
overwrite stays the documented stat-token residual. Results-safe — never in run identity / partition bytes /
`data_snapshot_hash`; rendered Parquet bytes byte-identical (ON/OFF
parity-pinned). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; all five touched
modules 100% line+branch).
Earlier: 2026-07-08 (DEC-SWEEP-LOG-VOLUME-GUARD-001, MIGRATION.md #1630 —
the sweep CLI log-volume guard: `brutex sweep`/`sweep-all`/`sweep-daemon` now
configure the ONE COMMON structlog seam
(`observability/logging.configure_logging` — the seam `cmd_dashboard` already
uses) at INFO by default via a single `main()`-dispatch guard, with the
fail-loud `BRUTEX_LOG_LEVEL` override (DEBUG/INFO/WARNING/ERROR,
case-insensitive; junk → `CliUserError` exit 1, never a silent fallback) — a
bare sweep process previously ran structlog UNCONFIGURED (default wrapper
filters at NOTSET) and streamed the engine's per-trade DEBUG events
(`trade_sim.entry`/`trade_sim.exit`) unfiltered to stderr: measured 870 MB on a
~200-session composite-e2e fixture, a disk-fill → `LakeWriteHalt` run-killer on
the §8 2-4 day grid. Sink-level threshold ONLY — no event re-levelled,
WARNING/ERROR untouched, and the Golden-Rule-#8 audit JSONL hash chain
(`SweepAuditWriter`/`AuditWriter`) is direct file appends, NOT structlog, so
audit records are byte-untouched (pinned). An already-configured process (the
dashboard's in-process sweep job via `run_sweep_from_request`) is deferred to —
its rotating file sink survives; the guard validates the env var first, so junk
still fails loud there too. R4 FOLD: the guard covers the PARALLEL path —
`sweep_parallel._worker_init` applies the SAME bound inside every SPAWN
worker via the NEW `configure_process_logging_best_effort` (spawn children
never inherit the parent's structlog config; `BRUTEX_LOG_LEVEL` propagates
via env; junk → INFO fallback worker-side, never a dead worker; REAL
2-worker spawn regression via capfd), and the dashboard-launched sweep
inherits it automatically. OPERATOR NOTE: `BRUTEX_LOG_LEVEL=ERROR` silences
the WARNING-level watchdog/quarantine events — INFO or WARNING recommended.
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
scope/vocab/rate change; `cli.py` + `observability/logging.py` +
`engine/sweep_parallel.py` 100% line+branch.)
Earlier: 2026-07-08 (DEC-ONECLICK-SWEEP-RUNS-REBIND-001, MIGRATION.md #1621 —
the P4 merge-integration rebind of the one-click display adapter onto the
SHIPPED #1275 results-depth artifacts: the ONE discovery seam
(`oneclick_artifacts.runs_root`) repoints `<results_root>/runs/` → the
gap-register Ruling-4 `sweep_runs/` SIBLING root — derived from the SAME
imported `trade_store.SWEEP_RUNS_DIRNAME` and mirror-PINNED byte-equal to the
ONE engine seam `trade_store.sweep_run_dir`; deliberately NO legacy `runs/`
fallback probe (a dual root could silently serve stale artifacts) — the
committed fixture corpus migrated `runs/` → `sweep_runs/` byte-identically
instead, with NEW `fixture_results_root` (`<fixture_root>/sweeps` pseudo-lake
law) keeping the fixture override self-contained. The trades seam's PRODUCTION
default becomes the NEW `TradeStoreReader` paging the REAL
`trade_store.read_trade_page` sidecars (top100 additive `run_id` +
`equity_ref.tuple` → `sidecar_paths_from_ref`, every segment safe-validated —
a hostile traversal ref never touches disk; reader order injected > fixture
env > TradeStoreReader > no-lake AwaitingTradesReader). HONEST: a pre-#1275
top100 / absent stamp → loud awaiting; stamp/read damage → NAMED corrupt,
never a 500 or a fabricated count; an unknown combo pages total=0; limit
clamps INSIDE read_trade_page at PAGE_LIMIT_MAX=500, the clamp NAMED in
detail; lots/costs_paisa pass through as honest None (Ruling 18) rendered
"—"; additive trade_seq/path_1m_status/mae_granularity surfaced (mae/mfe
carry a "(1m)"/"(tf)" suffix, textContent-only). The P2 manifest/events
writers are still NOT shipped — the awaiting states + the ASSUMED chain
algorithm stand. DISPLAY zone only — ENGINE UNCHANGED
(NIFTY/BANKNIFTY/SENSEX), engine modules imported never edited; both oneclick
modules 100% line+branch standalone; ruff/ruff-format/`mypy
--strict`/pyright clean.)
Earlier: 2026-07-07 (DEC-OPERATOR-PAGE-REAL-001, MIGRATION.md #1561 —
the Operator page (/) is wired to the sweep-oneclick ARTIFACT CONTRACTS and
the v29 PREVIEW mockup is KILLED by a MECHANICAL sweep (every hardcoded
₹/count/date/invented-strategy-name site re-fed via `data-oc=` hooks,
em-dashed, or deleted — the fabricated CHARGES table deleted outright,
Track 1 is signal-only; a generic zero-₹-outside-allowlist pin guards the
class): real funnel/badges/rank-1 card fed from
`<lake>/sweeps/runs/<run_id>/` {manifest.json, top100.json, events.jsonl}
through ONE clearly-marked integration adapter
(`brutex/dashboard/oneclick_artifacts.py` + the pure `oneclick_view.py`
builders) — LOUD "awaiting real run artifact" states, named
corrupt/partial/EMPTY degrades, NEVER a 500 or fabricated counts; trades via
a typed `TradesReader` seam whose PRODUCTION default is the honest
`AwaitingTradesReader` (the file-reading fixture reader activates ONLY under
the fail-loud `BRUTEX_ONECLICK_FIXTURE_ROOT` with a loud badge + visible
`source` chip; ResultsStore rebind at merge-integration; fixtures live in
tests/, production shows awaiting states). NEW
`/api/v1/oneclick/{runs,manifest,top100,events,trades,verify}` — stat-token
`MoversSnapshotCache` reads, all bench-pinned (`test_oneclick_o1.py`, 6 pin
groups measured 0.98×–1.00× flat + deterministic teeth). HONEST O-envelope:
the cached hit is O(1) vs run count/history/sibling tree/trades total and the
events tail is O(page) (byte-offset seek, 1 MiB window, EOF stat-token
short-circuit) — but the FIRST read per artifact revision parses the
O(artifact) file, the manifest fresh compute adds one O(page) events tail
for the chain map, and the timeline search is WINDOW-scoped (5,000-record
client ring), never O(1)-total. The Run button MOVES to the Operator page
(EXISTING `POST /api/v1/sweep/run`; the condition-subset selector ships
DISABLED "lands at merge-integration (P1)"; the preflight strip is STATIC
honest text — no route serves the estimator yet) and /reports returns to
pre-open ONLY; NEW /screener (client filter/sort over top100,
createElement-only rows; R26 virtual-scroll deviation LEDGERED — a ≤100-row
artifact renders directly, no in-CI ms measured — the #1557 Playwright
browser-truth job does not drive /screener) +
/strategy (createElementNS candles off the EXISTING /api/v1/db/candles,
trade-centered window, arrows dropped LOUDLY when out-of-window, trades
inline at load, equity = client int-paisa cumsum labeled "per-trade close");
R44 VERIFY-ME panel: 7 checks, 5-state truth (operator-judged cap at READY;
the chain check permanently reads "algorithm assumed until P2 merges" — its
golden proves generator↔verifier SELF-consistency only; `check=chain` =
O(file) on click only). The events chain law / events path /
per_door_kills+funnel shapes / interim trades layout are registered contract
PROPOSALS behind named seams. XSS: textContent-only, payloads stringified
into ONE text node, hostile-twin route-driven. NAMED out-of-PR: the
bx-rate-monitor/bx-liveproof→bx-poll refactor; regime/family display (P3).
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; display zone ONLY — zero
engine/orchestrator/results-storage/preflight edits; both new modules 100%
line+branch standalone + two new static ci.yml gates).
Earlier: 2026-07-06 (DEC-GREEKS-BULK-REENRICH-001, MIGRATION.md #1558 —
the bulk Greeks v1→v2 re-enrichment BACKFILL closing the #1188 named follow-up:
NEW `brutex/data/greeks_backfill.py` — a stamp-driven target-DISCOVERY +
batching layer feeding the EXISTING `enrich_greeks_for_targets` driver
UNMODIFIED (its value-checked reuse gate + honest NO_RATE/NO_SPOT/
BELOW_INTRINSIC/BAD_INPUT/AT_EXPIRY demotions do ALL the correctness work; a
Greek is NEVER fabricated), so EXPIRED contracts frozen at formula v1 AND
CLI-`brutex fetch`-only lakes are now re-stamped to the current formula/inputs.
Scanner (manifest-first enumeration, 1minute-only, per-contract
`greeks_provenance_id` column probe via the NEW exported
`stamp_is_current_inputs`/`current_inputs_pack` predicate — ONE pack shared
with the driver gate, property-pinned) → content-addressed plan (`backfill_id`
sha256 over formula ‖ rate-source ‖ modes ‖ sorted target keys) → bounded
batches with a DURABLE advisory checkpoint (`SweepCheckpointStore` reused
verbatim at `<lake>/.greeks_backfill/checkpoint.json`; crash → resume; deleted
on completion — the stamps are the durable done-record). Surfaces: `POST
/api/v1/universe/greeks-backfill` (+ O(1) progress route, a dedicated
`BackgroundPullJob` — account lock skipped, lake-root single-flight KEPT for
the WET run so a backfill and a pull on the same lake serialise — since fix
round 2 keyed on the STORE-derived WRITE root, and a dry-run start is
lock-free) + a `universe.html` panel
(textContent-only), and `brutex greeks-backfill` with a FULLY INERT
`--dry-run` (zero writes / checkpoint / Telegram — whole-tree sha-pinned; the
one documented read side effect is the shared `.manifest/` cache self-heal);
exactly ONE best-effort Telegram completion summary
(`greeks_backfill_complete`, ✅/⚠️ honesty heads, MarkdownV2-escaped);
`LakeWriteHalt`/`GreeksEnrichPoolBroken` never caught below the job/CLI
boundary (checkpoint survives → next run resumes). HONEST: the scan is
O(files) NEVER O(1); no wall-clock SLA claimed; the stamp prefilter is blind
to a rate/spot VALUE drift under unchanged source kinds — `--force` routes
every contract through the driver's value-checked gate, which catches exactly
that class. NO formula bump (the enrich goldens stay frozen).
ADVERSARIAL-REVIEW FIX ROUND folded (same branch, pre-merge): the CLI wet run
HOLDS the lake-root `pull_singleflight_lock` (contention → loud refusal;
`--dry-run` lock-free); the scan/probe fire per-contract/per-file watchdog
liveness beats + an honest "scanning…" job label (no false CRITICAL stall
alert on a deep-lake backfill); `force`+`limit` REFUSED at scanner/CLI/route
(chipping is default-stamp-mode-only); `SweepCheckpoint.from_dict` rejects a
valid-JSON non-object payload as `SweepCheckpointError` (never a wedging
`AttributeError`); a schema-poisoned stamp column + an out-of-range month
filename fold into `probe_failed`/skip instead of crashing the scan; the
`expired_only` expiry==today boundary regression-pinned.
FIX ROUND 2 folded (refuter-majority-confirmed, MIGRATION.md #1558): BOTH
dashboard jobs' cross-process flock re-keyed onto the STORE-derived WRITE
root (`app._job_lake_root_resolver` — a `--lake-root X`/env-divergent launch
no longer yields disjoint lock files that let a CLI backfill race a dashboard
writer on the SAME lake); the dashboard DRY-RUN backfill skips the lake lock
(`BackgroundPullJob.start(cross_process_lock=False)` — the CLI posture, a
zero-write scan never refuses a real pull); the manifest-miss enumeration
WALK (`db_view._walk_candle_datasets`) beats the stall watchdog per symbol
dir (round 1's beats covered the probe only — honesty-corrected in the
ledger); `SweepCheckpointStore` maps EVERY corruption class to
`SweepCheckpointError` (1 MiB size cap pre-read, `RecursionError` nesting
bomb, same-id `total_combos` mismatch refused, parent-mkdir inside the
mapping — a stray file at `.greeks_backfill` logs + continues per the
advisory contract); every bar-writing wet batch touches
`<lake>/.greeks_backfill/revision.json` and `lake_revision` stats it as a
THIRD component, so a CLI backfill's Greeks reach a running dashboard's
caches on the next poll; the wet report's scan-census carry gains killing
tests.
FIX ROUND 3 folded (refuter-majority-confirmed, MIGRATION.md #1558): the
durable checkpoint FREEZES at the last fully-clean prefix boundary (`record`
only while the run's CUMULATIVE `errored == 0` — an errored contract keeps its
stale stamps, so an identical-plan crash-resume could silently SKIP it and
report a false-clean `errored=0` exit-0 ✅ run; the "plan shrinks so the stale
checkpoint self-ignores" resume claim is corrected for the errored-prefix
case); the two now-concurrent `BackgroundPullJob`s keep INDEPENDENT
watchdog liveness — MECHANISM SUPERSEDED at the merge-reconcile (merge
040b2aec): the branch's interim multi-registrant beat registry (and round
4's `bind_progress_beat_scope` scoping over it) was resolved AWAY in favour
of main's DEC-PULL-BEAT-PERRUN-001 (#1537) per-run pull-id-KEYED beat
channels — `progress_beat.py` at HEAD is main's, byte-identical, and the
clobber/clear false-CRITICAL class stays closed by keyed registration
(see the #1558 supersession note); a stray FILE at
`.greeks_backfill` (ENOTDIR) reads the stable `absent` token instead of
muting the ENTIRE composite `lake_revision` probe.
FIX ROUND 4 folded (refuter-majority-confirmed, MIGRATION.md #1558): the
`backfill_id` binds a rate-source VALUE fingerprint (exact fixed rate / sha256
of the loaded CSV curve), so a crashed FORCE run's surviving checkpoint
self-ignores across a rate-value change instead of exempting the recorded
prefix from the driver's value-checked gate (false-clean green run; a
SPOT-heal inside the crash window stays a DOCUMENTED residue — delete the
checkpoint after a heal) [COMPLETED by FIX ROUND 10 below for the rate
A→B→A round-trip / intervening-writer class the fingerprint cannot see]; a concurrent dry-run backfill's O(files) scan can
no longer mask a genuinely-WEDGED pull's HUNG→CRITICAL→WEDGED chain — SHIPPED
as main's DEC-PULL-BEAT-PERRUN-001 (#1537) pull-id-keyed channels after the
merge-reconcile superseded the interim `bind_progress_beat_scope` design
(which no longer exists in the tree): `BackgroundPullJob._run` runs its
worker inside `pull_id_scope` with its beat registered on its OWN run id, an
unattributed beat is DROPPED + WARNed (loud false stalled-SUSPECT, never a
masked true stall — keyed routing, NOT a fail-open broadcast), and the
round-3 "the WEDGED dual signal backstops the masking" claim stays RETRACTED
as vacuous — WEDGED is gated on the HUNG edge (see the #1558 supersession
note); a null-stamped row with non-finite OHLC
(driver-skippable by IVX-2 design) classifies the NEW `stale_unfixable`
census bucket — never re-targeted (the census converges instead of a forever
re-feed reported `already_current`), surfaced with ⚠️ Telegram head + note +
CLI stderr; `--pause-s`/`BRUTEX_GREEKS_BACKFILL_PAUSE_S` refuse non-finite
values (`inf` crashed the wet run mid-batch, `nan` silently disabled the
pause); the #1175-style bench pins land
(`tests/bench/test_greeks_backfill_scan_cost.py` — flat per-file probe cost,
stamp-column projection spy, O(1) checkpoint bytes) and the ledger's "TESTS
(7 types touched)" header is honesty-corrected.
FIX ROUNDS 5+6 folded (refuter-majority-confirmed, MIGRATION.md #1558):
eager env validation on BOTH surfaces (CLI `CliUserError` one-liner + route
400 BEFORE the O(files) scan/lock); driver-unreachable NON-canonical month
filenames (`2024/1.parquet`) fold into `probe_failed` + ONE loud
rename-to-canonical WARN, never an eternal false-green re-target; the
fixability gate generalised to NON-null stale stamps on driver-skippable
rows; `int(inf)` `OverflowError` mapped at the stamp probe AND
`SweepCheckpoint.from_dict` (one poisoned file/checkpoint no longer kills
the scan/run).
FIX ROUND 7 folded (refuter-majority-confirmed, MIGRATION.md #1558): EVERY
suspicious-file targeting judgment now runs over the DRIVER's row view
(`_driver_row_view` — own-UTC-month bound + the COMMON `dedup_bars_frame`
keep-first, `read_option_rows`' exact per-file discipline) instead of the
RAW physical rows — a stale stamp carried ONLY by a dropped
duplicate-timestamp/foreign-month row re-targeted the contract forever
while the driver read `already_current` under an EXIT_OK ✅ false green
(both directions escaped: a kept non-finite row shadowed by a finite dup,
and a dropped dup carrying the ONLY stale stamp); driver-invisible-only
staleness folds into `stale_unfixable` + the NEW
`stale_on_driver_invisible_rows` WARN naming lake_heal's row-removing heals;
the LEGACY 7-column arm rides the SAME driver-actionability gate instead of
an unconditional stale-null target (a legacy all-non-finite month file — the
COMMON shape on the CLI-`brutex fetch`-only lake, gap b of #1188 — looped
identically; an EMPTY legacy partition honestly classifies nothing-to-do);
the unfixable wordings on note/Telegram/CLI-stderr widened to
"driver-unactionable rows"; the clean fast path stays the bench-pinned
single stamp-column read.
FIX ROUND 8 folded (refuter-majority-confirmed, MIGRATION.md #1558): the
scanner's month-file reachability predicate now mirrors the driver's EXACTLY
— canonical zero-padded name AND `is_file()` — so a canonical-NAMED
dataset-style DIRECTORY (`2024/01.parquet/`, probe-readable by polars yet
invisible to `ist_span_partition_files`' `is_file()` gate) folds into
`probe_failed` + the driver-unreachable WARN instead of the eternal-re-feed
✅/exit-0 false green escaping through filesystem TYPE; and the
merge-absorbed DEC-CLI-GREEKS-ENRICH-001 enrich pass (`brutex enrich-greeks`
+ the `brutex fetch` post-ledger tail) — the SECOND sanctioned ledger-less
Greeks-overlay writer — now touches the SHARED cross-process freshness
marker via the NEW wheel-free COMMON `brutex/data/greeks_revision.py`
(`greeks_backfill` re-exports/delegates byte-identically) whenever it wrote
bars, closing the round-2 staleness class on that surface and reconciling
`lake_revision`'s falsified "the ONE sanctioned production writer" honesty
claim to name BOTH marker-carrying writers.
FIX ROUND 9 folded (refuter-majority-confirmed, MIGRATION.md #1558): the
schema-poisoned stamp fold moved from the raising-`int()` VALUE gate to a
DTYPE gate (NEW `_stamp_dtype_poisoned` — ANY non-integer
`greeks_provenance_id` dtype folds into `probe_failed` at BOTH the raw probe
and the verdict, BEFORE the null branch): a COERCIBLE-but-non-integer stamp
(utf8 `"7"`, Boolean, fractional float) sailed past the rounds-1/6
coercion-trio catches and detonated the un-fenced `is_in` driver-view
filter — ONE junk file killed the WHOLE scan (raw CLI traceback on wet AND
`--dry-run`; dashboard job error, no Telegram) and a fractional float
mis-classified `stale_unfixable` — the now-unreachable trio catches removed,
whole-float columns now fold `probe_failed` (sanctioned narrowing); the
`universe.html` backfill panel poll is SELF-RECOVERING (the
DEC-PULL-UI-STALENESS-001 posture: quick retries then a 3 s→30 s capped
exponential backoff, NEVER a give-up — the bounded give-up that painted
"live progress feed dropped" after ~3 s of blips on a multi-hour wet run
and re-enabled the run-starting button is REMOVED; a non-ok/malformed poll
never settles the panel; a hidden tab fires zero fetches with a
visibilitychange catch-up; and `gbAttachIfRunning()` re-attaches on page
load AND on a started:false refusal — the previously-FALSE "re-open the
page" remedy now works); this file's + #1558's rounds-3/4 progress-beat
mechanism claims reconciled to the shipped #1537 keyed-channel design (see
the supersession notes).
FIX ROUND 10 folded (refuter-majority-confirmed, MIGRATION.md #1558):
checkpoint ADOPTION is freshness-gated (the round-4 fingerprint attests
plan-vs-checkpoint rate EQUALITY only — it cannot see an INTERVENING overlay
writer): a rate A→B→A round trip (a crashed force run under A, a rate-B force
run whose errored first batch FREEZES the id-A checkpoint while genuinely
rewriting the recorded prefix under rate B, then the natural revert to A —
or, with NO second crash, an intervening `brutex enrich-greeks` / pull-tail
enrich pass under the changed rate env) reproduced id-A exactly and ADOPTED
the stale checkpoint, skipping the rate-B residue into a false-clean ✅
`errored=0` exit with the wrong-rate Greeks stamped CURRENT forever; a loaded
checkpoint now self-ignores LOUDLY — and the stale file is DELETED — when the
shared overlay freshness marker was last touched by a FOREIGN writer at/after
the checkpoint's `updated_at` (own-plan touches identity-exempt, so a
legitimate same-plan resume still adopts) or the window ledger moved at/after
it (`_checkpoint_superseded_reason` + the additive engine
`SweepCheckpointStore.discard_adoption()`; every ambiguity biases to the
fresh correct-by-stamps start; NEW documented residue: a mid-pass-crashed
intervening writer leaves no signal — delete the checkpoint, the spot-heal
remediation); and the round-9 `universe.html` gb-poller gains the TWO
sibling-parity pieces round 9 omitted — every gb fetch (poll, start POST,
attach probe) rides ONE `gbFetchJson` helper arming the sibling's
FEED_FETCH_TIMEOUT_MS hung-fetch abort bound through the BODY read (a
headers-then-stalled-body response previously threw nothing: catch/backoff
unreachable, nothing scheduled, panel wedged at "running · …" with the Run
button disabled until a tab flip/reload) plus the `gbPollGen` re-entrancy
guard (a visibilitychange catch-up SUPERSEDES an in-flight chain instead of
forking it; a stale late response never overwrites a newer status line).
ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; F&O lake REFERENCE_ONLY;
`greeks_backfill.py`/`greeks_enrich.py`/`greeks_enrich_pass.py`/
`greeks_revision.py`/`sweep_alerts.py`/`sweep_checkpoint.py`
+ `pull_jobs.py`/`lake_revision.py` ALL 100% line+branch (`progress_beat.py`
is NOT touched by this branch — byte-identical to main's #1537 keyed design,
dropped from this list at the round-9 honesty reconcile);
every touched `cli.py`/`app.py`/`db_view.py` line+branch covered).
Earlier: 2026-07-08 (DEC-SWEEP-STALL-OOM-GUARDS-001, MIGRATION.md #1608 —
the parallel grid-sweep (`brutex/engine/sweep_parallel.py`) gains a
HEAVY-GRID-AWARE stall watchdog + a distinct `BrokenProcessPool` QUARANTINE,
closing two round-3 meta-refuter run-killers, additive resilience (a CLEAN run
is byte-identical; frozen sweep goldens + P-06/suffix-independence UNMODIFIED).
(1) the stall watchdog no longer fires a flat 300 s: a legitimately-slow k=5
1-minute tuple (`C(60,5) = 5.46M` masks × hundreds of thousands of bars) runs
as one worker future FAR longer than 5 min with no sub-tuple progress, so at
the grid tail — only slow heads in flight, none completing within 300 s — the
old bound aborted a multi-hour grid near completion. The wait budget is now
SCALED by the in-flight `mask·bar` work (`_effective_stall_seconds = max(base,
work / STALL_MIN_MASK_BARS_PER_SEC)`; `work = C(n_unique, k) × N_bars` stored
per in-flight `_Dispatched`), so a tuple that legitimately needs N minutes gets
a proportional bound while a genuine hang STILL aborts (the bound is FINITE — a
wedged worker makes zero progress — HONEST: the detection latency SCALES with
`C(n_unique,k) × N_bars`, ~4× a heavy tuple's legitimate runtime). Env knobs
`BRUTEX_SWEEP_STALL_SECONDS` + `BRUTEX_SWEEP_STALL_MASK_BARS_PER_SEC`
(positive-float, invalid→default+LOUD; the constructor refuses non-positive/
non-finite values); `stall_seconds`/`stall_min_mask_bars_per_sec` default
`None`→env-resolved so `run_parallel_grid`/the CLI pick it up with no call-site
change. HONEST (Golden Rule #10): `2.5e8 mask·bar/s` is a CONSERVATIVE
hang-detection floor (kernel ~1e9, ~4× headroom), setting only the
false-abort-margin ↔ wedge-detection-latency trade-off, never results. (2) a
worker OOM is QUARANTINED durably + REVERSIBLY + VISIBLY: `_drive` catches
`BrokenProcessPool` DISTINCTLY (`future.result()` runs BEFORE `pending.pop` so
the broken future stays quarantinable) and `_await_next_result` catches a
worker's surviving Python-level `MemoryError` (the cgroup/rlimit OOM shape —
pool NOT broken) as the single-tuple twin; already-FINISHED sibling futures are
DRAINED (real results applied, never discarded); the poison tuple(s) land in
the DURABLE quarantine sidecar `<root>/.sweep_quarantine.json` (fsync'd BEFORE
the raise) — deliberately NOT `SweepCache.record` (which would permanently +
invisibly drop them from every identical resume — the adversarial-review MAJOR
corrected pre-merge) and NO checkpoint advance (`had_failure` latch) — then the
run fails LOUDLY with the NEW `SweepWorkerPoolBrokenError` naming tuple(s) +
sidecar + remediation (reduce `BRUTEX_SWEEP_WORKERS` / k, or raise
`BRUTEX_SWEEP_MEM_BUDGET_BYTES`; then DELETE the sidecar entries and resume —
the tuples are then genuinely RECOMPUTED, the ONE reversible lever;
regression-pinned). A resume gates the sidecar FIRST and reports the skips as
the DISTINCT `tuples_quarantined_prior` counter (on `SupervisedGridSummary` +
the COMPLETE audit payload — NEVER folded into `skipped_cache`), so the
completeness hole is always visible; distinct
`SUPERVISOR_TUPLE_POOL_BROKEN`/`SUPERVISOR_TUPLE_QUARANTINED_PRIOR` audit
actions + ERROR/WARNING logs make the loss LOUD. Fail-loud is chosen over blind
pool-rebuild on the merits + the memory-budget-division interaction: the
per-tuple rank budget divides across workers (`memory_budget_bytes` = `total ×
fraction / workers`) [CORRECTED by DEC-SWEEP-MEM-BUDGET-WORKERS-WIRE-001
(MIGRATION.md #1671): as originally wired the divisor was never passed
(`workers` defaulted 1) — the claim was false until #1671 wired it; see that
entry], so rebuilding into the next heavy tuple would just
re-OOM. HONEST residual (named follow-up, NOT claimed): a deterministically-
WEDGED non-OOM tuple still aborts via `SweepStallError` with a frozen
checkpoint, so an identical resume re-dispatches IT — the stall path has no
quarantine (pre-existing). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; F&O lake
REFERENCE_ONLY; no scope/vocab/rate/lake/audit-schema change (the new actions
appear only on broken-pool/resume-after-quarantine runs, never on a clean run);
`sweep_parallel.py` 100% line+branch (128 tests) + the touched
`sweep_supervisor.py` (additive defaulted summary field) 100% line+branch;
ruff/ruff-format/`mypy --strict`/`pyright` clean.
Earlier: 2026-07-07 (DEC-SWEEP-NIGHTLY-AUTOMATION-001, MIGRATION.md #1612 —
the R9 nightly zero-click chain: ONE `brutex nightly run` executes COLLISION →
DISK → BACKUP → PULL → PREBUILD → SWEEP over EXISTING public entry points only
(a headless `run_full_pipeline` binding mirroring the dashboard closures; the
sweep via `cmd_sweep_all` on the daemon key-list precedent, the three
unattended escape flags structurally absent AND dispatch-hard-refused —
STRICTER than the daemon, which refuses only `--allow-out-of-scope`), under a
NEW nightly flock + acquire-and-HELD pull locks (the LAKE pull lock held
through the sweep leg = the R4/G10 sweep-vs-pull exclusion, mechanical;
holder-note honesty on refusals — a pull refused mid-nightly reads
"(holder: nightly:<id>)"), with the R42 break law (first failing link stops;
later links BLOCKED-because-<link>; tripwires — link-transition, DURABLE-audit
read-back reconcile, pull coherence, snapshot re-read, sweep-count identity —
⇒ SUSPECT ⇒ cron-RED exit 2) and the R50 sealed-snapshot backup
(hash-of-DESTINATION manifest written LAST, full pre-seal verify,
`.partial`→one-rename seal; restore = verify-EVERYTHING-then-write, destination
ledger deltas CLEARED of manifest-extraneous files, `ledger.parquet` strictly
LAST, `--yes` gated, CI-drilled every push) + launchd install with the
MANDATORY `launchctl list` verification (installed-but-unverified FAILS — the
TickVault lesson) + the morning Telegram summary/refusal (per-run dedupe keys;
unknown counts read "?", never 0). HONEST: R48 decay diff deferred to v1.1 by
operator directive — a recorded DIVERGENCE from the living requirements (R48
homes in P6; the v1.1 item is R49; the P3 top100.json probe is the seam);
sweep-liveness is a dual-mtime HEURISTIC until P2's results-root flock; the
first post-deploy nightly re-sweeps everything (`code_commit_sha` re-key,
documented not fixed); `bars/` stays OUT of the automated backup (the manual
rsync/Time-Machine path retained) and night-N sweep results are captured night
N+1 (≤24 h exposure); launchd cannot wake a sleeping Mac (next-wake catch-up;
pmset opt-in documented, never run); backup/restore honestly O(files+bytes) —
probes bench-gated flat, backup bench-asserted LINEAR; `--dry-run` runs probes
ONLY (no pipeline even in spine dry-run mode — those write synthetic bars),
tree-oracle-pinned zero mutation. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
F&O lake REFERENCE_ONLY; no scope/vocab/rate change; every new module + the
three touched COMMON modules 100% line+branch).
Earlier: 2026-07-08 (DEC-SWEEP-DATA-EDGE-GUARDS-001, MIGRATION.md #1616 —
two surgical engine data-edge guards for sweep-result CORRECTNESS (lens-D of the
one-click-sweep gap register): (1) a Muhurat-safe prev-day anchor at the
Stage-1A roll seam — `_roll_to_new_day` rolled `_prev_day_ohlc = self._today_ohlc`
on every IST-date change with NO calendar guard, so a Diwali Muhurat evening
session's ~1-hour OHLC became the NEXT regular day's pivot / PDH-PDL / Fib /
day-type anchor (bits 6-29 + 48-51), poisoning 6 days across 2020-2025 with zero
warnings; the fix leaves BOTH `_prev_day_ohlc` AND `_daily_history` untouched
when the completed day is a `has_muhurat_session` date (the prior REGULAR
session's anchor carries through, mirroring `lookup_prev_day_ohlc` + the weekly
anchors) + ONE `stage1a.muhurat_prev_day_anchor_skip` WARN, look-ahead-clean,
parity-pinned across all 6 real Muhurat boundaries; and (2) a bounded bad-tick
screen at the engine read boundary — NEW `brutex/data/bad_tick.py` (`is_bad_tick`
+ `BadTickScreen` + `bad_tick_screen_enabled`) wired into
`feed/replay.py::arrow_table_to_bars` (the ONE sweep-feed choke point) SKIPS-and-
COUNTS a self-consistent scaled bar (close `>= 4x` OR `<= 1/4x` the previous
IN-SESSION kept close — the `x10` fat-finger class the positivity+ordering checks
miss), first-bar-of-session exempt, one `feed.bad_tick_skipped` WARN per session,
default-ON `BRUTEX_BAD_TICK_SCREEN` kill switch, O(1)/bar, never value-editing /
never fabricating. Threshold 4.0 derived from NSE circuit-breaker physics
(10/15/20% daily bands → single-minute moves << 0.25x; regular-day goldens
byte-identical). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; F&O lake
REFERENCE_ONLY; no scope/vocab/rate/lake-schema change; `stage1a.py` +
`bad_tick.py` + `replay.py` ALL 100% line+branch; ruff/ruff-format/`mypy
--strict`/pyright clean. The P-06 look-ahead + suffix-independence suites pass
UNMODIFIED.)
Earlier: 2026-07-08 (DEC-VIX-STAMP-WIRE-001, MIGRATION.md #1645 —
India-VIX trade stamping wired end-to-end: `vix_at_entry`/`vix_at_exit` on
every OBSERVABLE production trade + the persisted nullable
`vix_mean`/`vix_min`/`vix_max` finalist trio (REFERENCE ONLY — never in the
60-vocab, `_RANKABLE_FIELDS`, or the BLAKE3 run identity; a missing VIX bar
stamps `None`, never fabricated); the FIVE sanctioned unstamped
DISCARDED-stream sites are literal `reference_stamper=None`, AST-pinned with
exact counts (the round-6 review unstamped the cascade candidate
re-simulation — stamps provably discarded at the CandidateInput scalar
conversion — and DELETED the two manual-replay `vix_stamper_for_manifest`
builds that paid a full VIX lake read per replay/click for zero output);
redo-honest trio (None→value backfill · the typed tuple-skip
`VixTrioConflictError` · `sweep-verify --recount` stamps like the launcher and
attributes VIX-only divergences non-corrupt via `results_store.VIX_FIELDS`,
the ONE public trio-column source, + `dataclasses.fields()`-driven re-pair
gates). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules 100%
line+branch).
Earlier: 2026-07-08 (DEC-MUTATION-RUNTIME-003, MIGRATION.md #1597 —
the weekly deep mutation gate is made GREEN end-to-end: both cargo-mutants
jobs restructured onto sharded matrices (6 fno-math + 7 trading-core shards;
worst shard 65 min vs the 120 min cap), trading-core WIDENED to
evaluator+python+spec under `--features python,serde`, the python mutmut loop
wedge-proofed (a timeout-wrapped rate_common runner + fail-fast audit tests)
and fully DE-SURVIVORED — compute 354 · audit 254 · rate_limiter 165 ·
store 146 · partition 108 · parser 33 · spec 91 · rate_common 369 mutants,
ALL zero-survivor (latent main-side debt killed too: 2 rate_limiter sentinel
+ 3 partition probe-order + 70 audit survivors) — with timeout-vs-survivor
HONEST alert titles; rehearsal run 28922792812 green 15/15 (python loop
68m52s). COMPLETION PROOF = the first green SCHEDULED `mutation.yml` run
(next 2026-07-13 04:00 UTC) — STANDARDS row 3 stays 🟡 until it lands.
rate_common is GATED, its ceilings/floors/governor values byte-untouched
(§2 Rule 6); ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
scope/vocab/rate-cap change).
Earlier: 2026-07-06 (DEC-BACKUP-OFFBOX-001, MIGRATION.md #1596 —
automated incremental OFF-BOX backup of the lake + ledger + DuckDB masters + sweeps +
hash-chained audit logs (restic → S3 Object-Lock / B2 immutable; blueprint
`docs/REMOTE_CONTROL_AND_ZERO_TOUCH_COMPARISON.md:304` rows 28/30): NEW `brutex/backup/`
package + `brutex/config/backup_env.py` + `brutex backup {run,check,restore-drill,status}`
CLI, built OUTWARD from a 23-row failure-mode matrix — every failure = a deterministic
triple (exit code + `backup.*` structlog event + alert-or-explicit-no-alert); the phantom-
`/Volumes` mount gate runs BEFORE any restic verb (critically before `forget --prune` — the
one path by which a backup system could destroy data); an O(1) injectable-clock DEAD-MAN
(verdicts FRESH/RUNNING/HUNG/OVERDUE/NEVER_SUCCEEDED/STATE_CORRUPT/CLOCK_SKEW; CRITICAL
Telegram via 4 new `SweepAlerter` methods riding PHASE_BLOCKED — ZERO `telegram.py` changes;
durable cross-run re-alert throttle in the checker-owned `deadman.json`; daily deep
remote cross-check; healthchecks ping every tick — the external keystone lane); a monthly
byte-verified RESTORE DRILL (1 MiB known-bytes canary + stratified sample + the audit-JSONL
byte-PREFIX law); 3 launchd templates GENERATED by `schedule_render.py` + golden-pinned
byte-equal under `docs/runbooks/backup-templates/`; NEW `docs/runbooks/backup-offbox.md`
(12-step install incl. the mandatory live negative dead-man drill + cron fallback) +
ADR-0017's manual-acceptance register M1-M8; automates + supersedes the manual tar §2 of
`docs/runbooks/lake-backup-restore.md` (restore-ORDER law + consistency argument stand
verbatim). HONEST: a dead box is silent on-box until the M6 external watcher; RPO = the
schedule interval (sweeps/audit are the non-re-pullable loss window); subset verification
is sampling. Ops tooling ONLY — ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; F&O lake stays
REFERENCE_ONLY; no scope/vocab/rate-cap/lake-schema/stored-bar change; touched modules 100%
line+branch).
Earlier: 2026-07-07 (the 11-item POLISH PACK, MIGRATION.md #1582–#1592 —
DEC-SESSION-CLOSE-BOUNDARY-PIN-001 + DEC-DAILY-CUTOFF-GROUNDED-001 +
DEC-NTM-REVALIDATE-CROSSPROC-001 + DEC-RETRY-COUNTER-SPLIT-001 +
DEC-GROWW-EPOCH-VERDICT-001 + DEC-CLI-DEPTH-FLOORS-001 +
DEC-ACCOUNT-LEASE-CROSSHOST-001 + DEC-DERIVE-LOCK-VISIBILITY-001 +
DEC-RESUME-BANNER-AUTOCLEAR-001 + DEC-QUOTA-VERDICT-MOOT-001 +
DEC-DAILY-OPEN-PREDICATE-ALIGN-001: (1) the
at-close 15:30:00 boundary divergence (`daily_derive_cutoff` strict `>` vs
`clamp_window_end` `>=`) is ruled DELIBERATE and drift-PINNED cross-module
(the boundary tightening refused, the #1267 pattern; clamp byte-untouched;
NOT the daily-OPEN predicate item — that is item (11), closed by
IMPLEMENTATION, never wontfix); (2) the prebuild's
session-close daily cutoff is REPAIRED (the #1401 M1 regression had left
DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001 production-dead — route/parallel
orchestrator/heal recall all now ride ONE law via NEW `nse.now_ist` threaded
as `daily_cutoff_now`) and gains the data-grounded DEFER-only variant (NEW
`session_last_bar_complete` + `daily_derive_cutoff_grounded`, result ≤ clock
always, abstains on every unknown, DENSE-index-gated per #1031, kill switch
`BRUTEX_DAILY_CUTOFF_GROUNDED`; a post-close pipeline run now derives the run
day's 1day bar SAME-RUN, an incomplete dense session DEFERS — never a partial
daily bar; complete sessions byte-identical grounded-vs-clock, pinned); (3)
the NTM SWR background refresh gains a CROSS-PROCESS single-flight (advisory
flock sidecar `.ntm_revalidate.lock` + double-checked freshness under the ONE
COMMON `_ntm_cache_is_fresh`; unavailable lock fails OPEN to the pre-fix
refresh — never worse; closes the #1287 follow-up); (4) the conflated
`in_pull_fetches` ledger counter is SPLIT into persisted `empty_retries` vs
`transient_retries` (default `None` = honest unknown on old rows, the
optional-column template's third use; spine invariant pinned `empty +
transient + (1 if bars) == in_pull_fetches`; ACCEPTS the follow-up #924 +
#1355 twice declined as display-only relabels — the in-CALL 6× ladder still
NOT persisted, the #924 caveat stands); (5) the defensive epoch-timestamp
branch gains a deterministic session-plausibility VERDICT (NEW
`epoch_verdict.py`, unix-utc vs ist-naive against the real NSE calendar, ONE
`groww.parse.epoch_timestamp_verdict` WARN per epoch payload; round-1 review
fold: out-of-coverage dates abstain SILENTLY via the non-warning
`is_holiday_data_available` pre-check — the strict-calendar routing would
have stormed up to 2,048 `calendar.holiday_data_unavailable` WARNs per
out-of-coverage payload per window) + a live-probe
runbook — parsing byte-unchanged, the Unix-UTC assumption stays honestly
UNVERIFIED; (6) the CLI `brutex fetch` gains the vendor-depth-floor learner +
learned-floor planning clamp (`max(GROWW_HISTORY_FLOOR, learned floor)` via
the existing `symbol_windows` seam, SAME schema_meta sidecar as the dashboard
— either entrypoint's learned floor clamps both SERIALLY; dry-run
byte-identical; round-1 review MAJOR fold: the sidecar is a TRANSIENT
open→op→close per get/set (`_TransientCliFloorStore`) — the original
whole-pull rw DuckDB hold blocked a `brutex serve` boot mid-fetch and
always-failed under a running dashboard; while a dashboard process holds
`instruments.duckdb` the per-op degrade WARNs and self-heals, never a
blocked fetch); (7)
the account pull single-flight gains its CROSS-HOST arm — an advisory
heartbeat lease on shared lake storage (`.locks/pull_lease.json`, 30 s
refresh, 180 s self-heal, kill switch `BRUTEX_PULL_LEASE=off`) refuses a
start on a FRESH foreign-host lease; honestly DETECTION, NOT mutual
exclusion (TOCTOU/clock-skew/hostname-collision limits stated); every
synchronous lease lake-I/O op (enter read + first refresh, exit clear —
they run while BOTH flocks are held, and the exit clear runs BEFORE the
flock releases) is deadline-BOUNDED under the COMMON `run_with_deadline`
guard (`BRUTEX_PULL_LEASE_IO_DEADLINE_S`, default 5 s; round-1 review
MAJOR fold — a wedged volume previously hung `clear()` forever and refused
every future pull until restart; wedged op = one `pull_lease.io_wedged`
WARN + stale-TTL self-heal, at most one zombie daemon per op); (8) the
movers `PrebuildGate` honors the derive lock with HONEST attribution — a
CLI-process derive yields the `gated:true` payload with
`source="cross_process"`, and (round-1 review fold) the dashboard's OWN
out-of-pipeline derive (the lake-heal L2/L3 recall — flock contends across
open file descriptions, so the flock probe alone could not tell it apart)
reads `source="local_derive"` via the zero-I/O
`derive_lock_held_by_this_process` registry probe consulted FIRST, never a
fabricated "another process" claim (probe OSError fails OPEN, one WARN per
outage; closes the #1288 follow-up); (9) the intent-resume `ResumeBanner` auto-clears
on the next REAL pull launch that actually STARTS (it had NO production clear
path — a stale CRITICAL banner survived its own remediation; round-3 review
fold: the clear is STARTED-gated via the NEW `clear_banner_after_start` AFTER
`BackgroundPullJob.start` — a REFUSED start (contention / foreign-host lease)
launches nothing and clears nothing); (10) the #889 verdict-driven
quota phone-alert follow-up is CLOSED AS MOOT (Groww backtesting has NO
daily quota + ZERO production `DailyQuotaTracker` constructions,
AST-pinned) and the doubly-dead informational quota surface is DELETED
(`GET /api/v1/quota` + panel fields + `/universe` gauge — all only ever
served the constant sentinel), the vendor-neutral enforcement seam kept
dormant logic-byte-identical; (11) the TWO daily-open-substitution surfaces
now share ONE per-session predicate (MIGRATION.md #1592,
DEC-DAILY-OPEN-PREDICATE-ALIGN-001 — the named follow-up the #1267/#1232
drift pin deferred to): NEW pure `spot_daily_resample.classify_daily_open`
(substituted iff a NON-Muhurat session's first present 1-minute bar is NOT
exactly 09:15 IST) drives the prebuild detector directly AND the
/completeness · /universe · /coverage census's DuckDB CTE as its
mirror-pinned SQL twin (strict `>` → `<>` + a `_muhurat_exclusion_sql`
NOT-IN baked from `MUHURAT_SESSIONS`) — a Muhurat session no longer counts
read-side (was a yearly false positive on a healthy lake), a stray
pre-09:15 first bar now counts on both (the strict-`>` under-count closed);
same-lake same-verdict equivalence proven + pinned, the old divergence pin
consciously rewritten to an alignment pin; the ONE remaining deliberate
divergence is POPULATION not predicate (census = ALL stored sessions,
detector = the forming-session cutoff), documented + pinned; also corrects
#1582's earlier misattribution (this item was never closed-wontfix — it is
closed by IMPLEMENTATION). ALL ELEVEN: ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate-cap change; fetch
PACING/RATE byte-identical everywhere, with exactly THREE SANCTIONED
behavior changes (each honestly scoped in its own ledger entry): the
same-run post-close 1day derive (#1583 — previously deferred to the
next run), the learned-floor-clamped CLI planning (#1587 — a persisted
learned floor plans FEWER 1-minute windows by design; dry-run
byte-identical), and the fresh-foreign-lease pull REFUSAL (#1588);
every other item's fetch behaviour is byte-identical (the counter
split #1585 explicitly so); touched modules 100% line+branch).
Earlier: 2026-07-06 (DEC-RATE-METER-LIFECYCLE-001, MIGRATION.md #1593 —
the #1056 pull-rate CallMeter gains an explicit LIFECYCLE: registered by the
pull build sites at pull start, cleared by (a) `register_pull_meter`'s NEW
`dry_run=` gate (a dry-run DEREGISTERS — the dry-run GrowwClients carry a REAL
unpaced CallMeter, so the old "no `.meter` no-op" claims were FALSE), (b)
`BackgroundPullJob(on_run_end=SessionRateController.clear_meter)` in the job's
identity-gated finally, and (c) the OFF-job `pull_candles` spine's OWN paired,
identity-gated clear (round 4 — `clear_meter(expected=)` compare-and-clear via
`_pull_meter_scope`; supersedes round 2's route-level unconditional `finally`
clear, which a round-4 reviewer proved evicted a concurrent JOB pull's LIVE
meter whenever the direct route 400'd before registering — a malformed date;
the route's dry-run arm still passes NO holder) — so "no in-process pull
running ⇒ no meter registered" is a process invariant covering every
registration path, a raise-before-register clears nothing, and a concurrent
pull's later registration is never evicted (the 96-agent audit's C0 cross-pull
leak + the dry-run fabricated "flowing" closed; the dry-run gate's clear stays
unconditional with its concurrent-off-job-live-pull blast radius now honestly
DISCLOSED, not denied — round-4 MINOR x2). Plus: per-spine mutant-killing wiring tests (all four
register sites — the audit's C1/C2 surviving-mutant gap); a kind-aware
`/api/v1/pull/rate` probe (`BackgroundPullJob.start(job_kind=)` — a 2-4-day
dashboard SWEEP reads honest idle, never a site-wide red "stalled"; the
`_pull_live` duty-degrade probe DELIBERATELY stays sweep-inclusive, #920); a
900 s `BRIDGE_MAX_AGE_S` TTL in the merged projection (a SIGKILLed CLI's stuck
`pull_active=True` bridge file decays to idle instead of a permanent red; the
12 s-900 s died-mid-run alert window preserved); the false bx-rate-monitor.js
"obs rendered" claim + dead CSS removed; six stale pre-#1056 doc passages
reconciled + (round 3) the scoped "no in-process pull running ⇒ no meter
registered" wording swept across every remaining claim site and locked by a
grep doc-pin test. Observe-only — ZERO pacing change, fetch byte-identical; ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules 100% line+branch;
ruff/ruff-format/mypy --strict/pyright clean).
Earlier: 2026-07-06 (DEC-FORM-WINNER-PERSIST-001, MIGRATION.md #1581 —
the `&`-underlying (%26) form-winner is now DURABLE across pulls and processes:
one `schema_meta` row per underlying (`groww.form_winner.<key>` → the KEY-level
winning form STRING — the winner's ROLE applied to the key's own
`underlying_symbol_forms` tuple (moved into `symbol_form_preference.py`,
re-exported from `contracts.py`), so a candle-phase win and a metadata win
share ONE value space — review-fixed from the raw phase-local string) written
through the NEW `PersistentSymbolFormPreference`
(write-on-change, read-once-per-preference-instance single-flight — one
instance per pull run, MULTI-form keys only on
BOTH paths — the ~750 plain underlyings pay ZERO store I/O on order AND record
(review-fixed: the ungated persist wrote ~212 junk rows per pull process); the
seed `get_meta` runs OUTSIDE the map lock, bounded by the COMMON
`run_with_deadline` (2 s, `BRUTEX_FORM_WINNER_READ_DEADLINE_S`) so a wedged
sidecar read can never stall the fan-out (review-fixed: it ran INSIDE the map
lock), and the persist `set_meta` is bounded by the same guard (5 s,
`BRUTEX_FORM_WINNER_WRITE_DEADLINE_S` — round-2 review fix: a non-raising
wedged write would have hung the recording fetch worker forever and queued
every later persist behind the persist lock); store errors WARN-only —
a pull is never crashed; `BRUTEX_FORM_WINNER_PERSIST=off` is the write-side
kill switch; SUPERSEDES the "cross-pull persistence is a NAMED follow-up"
clause of DEC-SYMBOL-FORM-PREFERENCE-001), keyed by the PRIMARY F&O underlying
in EVERY phase (`resolve_expiries`/`resolve_contracts` gain the
`preference_key` normalization `candles.py` already had; the metadata pull
passes the CELL key, never the queried spelling form) and wired at every
production pull seam (ONE instance per `/pull-pipeline` run shared by the
metadata + historical phases; the standalone metadata/composite routes AND the
`/pull-historical` direct-selection grammar — the latter MISSED in round 1 and
wired by the round-2 review fix; ALL FOUR seams route-pinned as of round 5
(pipeline identity-sharing + dry-run store=None + metadata/composite
real-store persist — previously only the direct-selection seam was);
`run_groww_option_ingest`'s fresh-per-pass default stands down — also fixing
the within-one-pull amnesia across market buckets/until-clean rounds; a
dry-run threads store=None, byte-identical to the process-local base). The
two structurally identical set_meta/get_meta Protocols consolidate into ONE
`brutex/data/meta_kv.py` `MetaKV` (Golden Rule #11 COMMON;
`VendorFloorMetaStore` is now a back-compat alias). PLUS the live `&`-family
probe TOOLING: `brutex doctor --live --ampersand-probe` (stage 14, ≤64-call
budget) probes BOTH symbol forms of every master-DERIVED `&`-underlying
against BOTH F&O APIs through the kit's read-only client, writes the
`ampersand_verdict.json` evidence sidecar + per-call transcript lines, SEEDS
the durable map per-underlying through the ONE COMMON `record_winner` write
path (the `seeded` count READ-BACK-VERIFIED — a kill-switched/failed write is
never claimed, `amp.seed_shortfall` names the cause), and — iff DECISIVE —
writes the machine-readable
`groww.form_probe.verdict` stamp, whose ONLY writer in the repo is that stage
(test-pinned; `form_probe_status()` reads it back — missing/corrupt =
honestly UNVERIFIED, "verified" is DATA not prose; a data-beside-a-RAISED-
sibling name reads `*_UNCONFIRMED` — it seeds its winner but NEVER counts
CLEAR/decisive, so throttle-contaminated evidence cannot stamp). HONEST
(Golden Rule
#1/#10): the probe was NOT run for this change — no Groww credentials in the
build environment — so ZERO transcript exists, ZERO stamp was written, and
the #898 `%26`-encoding hypothesis stays vendor-UNVERIFIED byte-INTACT until
a decisive operator-run probe lands its transcript; the map itself needs no
probe (production pulls self-learn from ordinary fallback wins). ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change; touched
modules 100% line+branch).
Earlier: 2026-07-07 (DEC-SOAK-REAL-TIER-001, MIGRATION.md #1580 —
the REAL multi-hour soak tier + the honest reclassification of the ubuntu
nightly: `stress-nightly.yml` is a **~5-min ubuntu-24.04 REGRESSION GATE**
whose guard step now REFUSES a soak-scale dispatch (`STRESS_EVENTS >
200000`) — the retracted "24h soak via workflow_dispatch
`STRESS_EVENTS=10_000_000`" #208 claim was FALSE per Golden Rule #10 (the
60-min job timeout hard-kills a 10M run at ~1/17th done; #208 corrected in
place, strikethrough + CORRECTED notes). The real multi-hour soak is a
SEPARATE tier — NEW `.github/workflows/soak-selfhosted.yml` on the
self-hosted M4-Pro (§8): weekly cron (default 6 h) + `workflow_dispatch
(soak_minutes)` fenced 1..1200 (20 h — the self-hosted `GITHUB_TOKEN`
expires at 24 h, so a longer run's failure watchdog could never authenticate
to file its issue, SEC9-1), with a duration-DERIVED `timeout-minutes`
(`soak_minutes + 30 + soak_minutes/6` — fixed install slack +
duration-linear teardown, SOAK-R7-4) so the job can never outrun its own
clock, driving NEW `tests/stress/test_soak_sustained.py` (the NEW `soak`
pytest marker — excluded from the per-PR ci.yml stress step AND
stress-nightly via `-m 'not soak'`; the previously-dead `STRESS_DURATION_S`
knob is now actually honored) under TWO NEW pure observability modules:
`brutex/observability/process_probe.py` (the ONE COMMON cross-platform
RSS+fd source of truth — Linux `/proc/self/status` VmRSS primary,
`ru_maxrss` PEAK fallback documented as an absolute-CEILING backstop for the
macOS tier, `/dev/fd` fd fallback; `None` = honest abstain, never a
fabricated 0; `test_memory_growth._read_rss_kib` delegates to it) +
`brutex/observability/soak_drift.py` (`SoakDriftMonitor` — RSS-ceiling /
fd-growth / p99-latency-drift rules modelled on the in-repo
`pull_health.py`, routed to a NEW real-time `SweepAlerter.soak_drift`
Telegram alert). Ledger provenance: renumbered
#1512→#1534→#1535→#1536→#1537→#1540→#1541→#1552→#1566→#1580 across
eight-plus main collisions (#1580 chosen clear of live-main max #1560 AND the
in-flight PR #1264/#1244 wave holding #1558–#1567, the PR #1236 high-jump
precedent — the validator allows gaps); ships via the replacement-PR remedy
(the PR #1095→#1097 / #1220→#1235 precedent) on
`claude/soak-real-tier-rebuild` after commit 2757d005's `fix(test+docs):`
scope (`+` illegal in a conventional-commit scope, no override, history
rewrite forbidden) made the original `claude/serene-maxwell-0irkzn` (PR
#1231) permanently unmergeable. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
no Groww scope/vocab/rate change; CI + observability + tests + docs only;
touched modules 100% line+branch.
Earlier: 2026-07-07 (DEC-FIB-BULLISH-RETRACE-001, MIGRATION.md #1560 —
the bullish Fibonacci retracement labels get their OWN PDL-anchored kernel
fields: `FibLevels.get_by_bullish_label` had aliased bullish 23/38/62/78 onto
the REVERSED bearish (PDH-anchored) fields, but Fib ratios are asymmetric
(0.236 + 0.786 = 1.022 ≠ 1.000) — the 23/78 rungs were wrong by 0.022×range
(565 paisa on the NIFTY 07-May-2026 golden anchor; the 38/62 pair coincides
only as an IEEE observation, never relied on). The shared `rust/trading-core`
kernel (DEC-SHARED-CORE-001 — Python grows NO math copy) adds
`retrace_bull_{23,38,62,78}_paisa = round_half_even(pdl + ratio·range)` (NO
bull_50 — the bullish midpoint IS the shared exact-integer `retrace_50`); the
`compute_fib_levels` FFI projection grows 15→19 (NEW `FIB_COMPUTE_LIST_LEN`,
bull tail APPENDED last) while `FIB_LEVELS_LEN` stays 15 — the FROZEN
`evaluate_with_levels` evaluator ABI (the two were an accidental 15-tuple
pun, now documented distinct constants; the evaluator zeroes + never reads
the bull fields). The catching test's unconditional `pytest.skip` is REMOVED
(bullish 78% chart-exact 24,258.00 = 2_425_800 paisa; frozen kernel goldens
[2_411_665, 2_415_417, 2_421_483, 2_425_800]) and the module docstring's
FALSE "verified bit-exact" bullish claim corrected honestly: 78 IS
chart-verified, 23/38/62 are formula-derived kernel-frozen NOT
screenshot-verified (Golden Rule #10). Byte-identity + NO indicator_version
bump — the recorded decision: `get_by_bullish_label`/`FibDirection` have
ZERO production consumers (census: tests only), the `near_fib_236/786`
vocabulary bits read the untouched bearish fields, and tests/golden incl.
the frozen 400-case vocab_golden_vectors.json pass with the fixture
byte-untouched. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
scope/vocab/rate-cap change; `fib_levels.py` 100% line+branch).
Earlier: 2026-07-07 (DEC-FUTURES-EXEC-TRANSLATION-001, MIGRATION.md #1543 —
the futures translation executor (Phase 5, R13/R14/R15) — an INDEX winner's
trades re-priced on the FRONT-MONTH-AT-TRADE-TIME futures contract, the options
executor's futures twin: NEW `brutex/futures/` (executor with the PINNED reject
order lot_size → cost_regime → no_contract → no_data → stale_bar[opt-in] and
the two pure date gates BEFORE any I/O; registry-calendar front-month
resolution via the SHARED `select_near_month` — never a last-Thursday formula,
lake-discovery fallback; exact-minute lake source with keep-FIRST dedup +
corrupt-partition quarantine-skip; R4 disk-verified preflight naming missing
months, no Phase-1 file edited; R45 independent Decimal-pipeline reference sim
+ per-run differential; R13 annotation-never-reranks projection + R42
content-hash-stamped atomic sidecar) + `brutex/costs/futures_costs.py`
(VERIFIED-rates-only: Groww min(₹20, 0.03%) futures brokerage + futures STT
0.02%@2024-10-01/0.05%@2026-04-01 encoded; futures ETC/NSE-IPFT/stamp + the
pre-2024-10-01 futures STT are explicit REFUSAL windows — the
DEC-ETC-REGIME-HISTORY-001 precedent, no runtime escape hatch; INDEX futures
COST-FREE per DEC-COST-SCOPE-INDEX-SIGNAL-ONLY-001, net == gross). R15
OPTIMISTIC=PRECISE fill only (open ± one tick adverse; volume-0 printed bars
FILL by default; gap-opens fill at the REAL open; the worst leg is DEFERRED,
structurally absent); STOCK winners gracefully DEFERRED naming the future
scope decree; a missing minute bar is a counted NO_DATA — never a nearest-bar
substitute. HONEST: not yet wired into cmd_sweep_all/the dashboard (the seams
are exposed; binding is the orchestrator's later step); R47 margin-ROI not
built (no in-repo SPAN history). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
options path untouched (R12); 197 tests, all 8 new modules 100% line+branch).
Earlier: 2026-07-07 (DEC-TRACK2-COSTS-SPEC-RECONCILE-001 + DEC-BROKER-DOCS-AWSONLY-RECONCILE-001 +
DEC-GPU-GATE-RECONCILE-001 + DEC-CLOUD-AUTOFIX-RETIRE-001 + DEC-PHASE1-SIGNOFF-RECONCILE-001,
MIGRATION.md #1552–#1556 — the doc/spec/ops debt reconciliation pack, DOCS-first:
(1) the Track-2 costs spec's short-side slippage contradiction RESOLVED to the §8
ADVERSE rule end-to-end — spec §4 Step 1 rewritten, "# CHECK SIGN" deleted, the
production-unreachable `calculator.py::_apply_slippage` short SELL leg re-pinned
`entry_open + tick` → `entry_open − tick` (byte-exact test 10_005 → 9_995; BUY-only
DEC-EXEC-008 + intraday DEC-RISK-011 locks UNTOUCHED, `executor.py` not edited), the
§4.2 expiry state machine honestly labelled a fail-loud STUB in-spec, and the two
§4.2 pseudo-code drifts REGISTERED as §16 items 18/19 — never silently decided;
(2) the broker (Groww) token/credential docs refreshed to the AWS-only / fail-fast /
mid-run-re-read truth (#1065/#1072/#1092) — the stale `BRUTEX_TOKEN_BROKER_READONLY`
instructions + REMOVED local-mint-fallback prose replaced across the two runbooks, the
deploy guide, the minter runbook, `TOKEN_BROKER_CONTRACT.md` (TickVault claims left
"per its own repo"), ADR-0013/0014 Status-line superseded-in-part notes, and the
scoreboard's re-mint cell → re-read (`groww.token.reread`); DOCS-only, auth code CITED
never edited, rate caps untouched; (3) ADR-0004 GPU gate reconciled — Status PROPOSED
→ ACCEPTED (CPU-first arm EXECUTED + MEASURED #411/#413/#417), the never-run §6
tri-platform benchmark RETIRED as scheduled work + RETAINED as a dormant FAIL-CLOSED
precondition, mechanically pinned by NEW `tests/unit/test_gpu_gate_drift_pin.py`
(no MLX/CuPy/`BRUTEX_GPU_OFFLOAD` in `brutex/`+`rust/`; #1492-G3 pattern);
(4) the dormant cloud auto-fix snippet (claude_automation.md §3) RETIRED outright —
DELETE, never wired, a #1180-class privileged-trigger hazard — and the fork-PR redteam
sweep EXTENDED to comment-family triggers (`test_no_issue_comment_privileged_workflows`,
GITHUB_TOKEN carve-out; revival needs a fresh DEC + the ledgered precondition checklist);
(5) the Phase-1 sign-off checklist closed by SUPERSESSION — all 11 boxes ticked with
per-item citations, the "10 boxes" count defect fixed, the env-var trailer superseded by
#1065; NO operator ack pre-claimed (requested on the PR). ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change; the ONE code delta lives inside the
DEC-EXEC-008-stubbed short branch, `calculator.py` stays 100% line+branch.)
Earlier: 2026-07-07 (DEC-RUN-IDENTITY-GUARD-PARITY-001 +
DEC-OPTION-DEDUP-BLOOM-VERIFY-PIN-001 + DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 +
DEC-TRADE-SIM-U128-KERNEL-001 + DEC-EXECUTION-TRIO-DELETE-001,
MIGRATION.md #1546–#1550 (renumbered from #1541–#1545 after PR #1234 took #1541) — the 5-item engine cleanup pack (reapplied onto
current main after the original 2026-07-06 lineage was wiped), ONE change set:
(1) the combo `\x1f` reject-only guard is MIRRORED into both previously-
unguarded run-identity joins (`refinement_run_id` + `_walk_forward_run_hash`)
via ONE COMMON `strategies.combo.reject_unit_separator`, plus `allow_nan=False`
at both engine canonical-JSON sites (+ the stage1a schedule-hash third mirror)
— byte-frozen run-identity goldens (with an independent pip-blake3
payload-byte oracle proving `fno_math.blake3_hash ≡ blake3`) landed FIRST and
pass unchanged; LOW/defence-in-depth, NOT a live bug. (2) the option-dedup
bloom-before-exact-confirm ordering VERIFIED already correct — ZERO executable
change (AST-proven); no-phantom + bloom-negative-short-circuit pins added at
both public probe entries (`add_if_new`/`unique_option_bars`) — the PRODUCTION
consumer of the verified ordering is the engine sweep's
`ComboDedup(OptionDedupIndex)` dedup tier (`sweep_launch.py`/`cli.py`); the
original sink-path pin covered the since-deleted `OptionBarSink` orchestration
(removed on main by DEC-INGEST-AUDIT-WIRING-001 #1500 — dropped in the rebase),
with the mark-after-durable-write LAW recorded as history in #1547. (3) the
hand-synced Python `VwapState` +
`MarketStructureState`/`Trend` twins DELETED — the `rust/trading-core`
pyclasses are the SINGLE SOURCE (additive `last_swing_high/low` getters
delegating to the EXISTING core accessors, core untouched — TickVault-safe;
crate 0.3.0→0.3.1, so the LIVE `_indicator_version()` string moves with the
bump — NO math change, new runs re-key via `indicator_version` exactly as the
§2-Rule-4 formula intends; the frozen twin fixture + SHA never regenerated,
replayed Rust-only; P-06 preserved at strength; `MarketStructureSignal`
stays). (4) NEW `ResidentSweepSimU128` fno-math kernel via a width-generic
`MaskWord` core (the existing Rust suite passed UNMODIFIED — the
frozen-behaviour gate; native `Vec<u128>` FFI per the recorded pyo3-0.29-abi3
probe transcript) — >64-bit masks/bar_bits are now Rust-served, parity-pinned
vs the `simulate_trades` oracle, with lazy wide-kernel promotion (reasons
re-scoped `bar_bits_beyond_u128`/`mask_beyond_u128`/`wheel_missing_u128` +
`wide_kernel_promoted`); HONEST: future-proofing, production-UNREACHABLE at
today's 60-bit vocabulary — every currently-legal u64 input byte-identical,
the frozen u64 golden corpus untouched, u64 bench non-regression measured.
(5) the deprecated `IndianCostModel`/`ExecutionSimulator`/`BacktestDriver`
trio DELETED with its 8 test surfaces + golden fixture + dead ci.yml gates
(grep-proven zero production consumers; `types.py` kept whole —
`InstrumentMeta` live — and `errors.py` semantics untouched, its five
subclasses honestly ledgered as orphaned; landed as ONE atomic commit — the
wiped first lineage's two-commit accident is recorded as history in #1550) —
the pack's ONLY behaviour-visible item (import surface). ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX; run identity bit-identical for all currently-legal
inputs (golden-proven); no scope/vocab/rate change; touched modules 100%
line+branch; ruff/ruff-format clean, mypy `--strict` clean, pyright at the
pre-change baseline; cargo suites green (fno-math 565, trading-core 162);
cargo-mutants evidence partial under the ledgered #1511 Rust timeout —
stated, not papered over).
Earlier: 2026-07-07 (DEC-TEST-APPARATUS-DEBTS-001, MIGRATION.md #1541 —
the test-apparatus debt pack: nine long-ledgered apparatus gaps closed as ONE
change set, each landed fix-the-violation or measured-green: (a) a repo-wide
doctest gate (pkgutil walk, measured 55-example teeth floor; the 3 broken
tf_resampler examples fixed — the ONLY brutex/ source edit, docstring-only);
(b) EXPLICIT hypothesis determinism on CI (the fully-explicit repo-registered
`ci` profile, `HYPOTHESIS_PROFILE=ci` in ci/verify-m4pro/mutation; HONEST:
GHA was ALREADY implicitly derandomized by hypothesis ≥6.113's built-in
auto-loaded `ci` profile — the real sanctioned flip is the reverse: no-env
scheduled arms (stress-nightly et al.) now deliberately load the random
default, overriding the auto-load; the false "500 examples /
HYPOTHESIS_SEED" pyramid claim corrected; non-tautological pins — AST
source pin + CI-env-stripped subprocess selection pin);
(c) a fix-reversion ci-ok hard gate — a fix:-titled PR's shipped tests must
FAIL with the fix reverted to merge-base in a scratch worktree (closes the
#1154 surviving-fixture class); (d) a citation ACCURACY scanner (ci-ok hard
gate, push+PR) — file:line cites in the normative docs resolve Tier 1/2/3 and
MIGRATION #NNN refs resolve; the live 29-finding census (13 in this file's §2
Rule 6 / §6 rows — the 4th #609/#669/#846 recurrence) reconciled to fixpoint
in the same change (round-2 honesty: fixpoint = scanner-green, NOT per-line
proof — two §6 rows the any-anchor×any-segment Tier-3 envelope blessed while
stale, plus one anchor-appeasing wrong-target re-point, were re-fixed on the
merits; the envelope is now stated plainly in the scanner's residual-limits
docstring, and out-of-root `../`/absolute cites now fail loudly instead of
reading + blessing a non-repo file); (e) a DEC-ID appropriateness heuristic in
validate_dec_ids.py (source-touching non-exempt PRs must cite a resolving
DEC-ID or touch the ledger; STANDARDS row 18 stays PARTIAL); (f) jscpd 3%→1%
(measured 0.37% baseline); (g) the pandas import ban widened to ALL of
brutex/ (zero offenders; the `brutex/*.py` + `brutex/**/*.py` pathspec PAIR —
round-2 caught git's `**` glob alone missing the 12 top-level modules); (h) P-10/P-12 promoted to real cost-calculator
properties (STT sell-leg-only; brokerage = the VERIFIED flat ₹20/order law —
the stale min(₹20, 0.03%) wording corrected); (i) the sweep worker-scaling
A/B bench enumerated in bench-m4pro.yml — EXECUTING GREEN on the live
registered M4 Pro runner (in-PR reconcile: the first real run failed on the
missing polars [data] extra, fixed a54c9a0a; re-run fully green, run
28851211762 — step measured 20 s vs the generous 25-min timeout; VERDICT:
KEEP CAP=8). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
scope/vocab/rate change; apparatus/CI/docs only).
Earlier: 2026-07-07 (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001,
MIGRATION.md #1539 — the /movers prev-close calendar-contiguity honesty guard:
a previous-day close/OI baseline that provably skipped a nearer NSE session (a
lake hole — the confirmed repro printed 2.2822% where the true Friday-based
figure is 0.2033%) is now STAMPED (`prev_close_session`/`prev_close_contiguous`
+ the INDEPENDENT `oi_baseline_*` twins), warned ONCE (capped 20/build, deduped
per (symbol, kind)), and marked " *" on the screen — while the SERVED VALUE is
untouched in every branch: NEW COMMON `calendars/nse.previous_trading_session`
(strict back-walk, coverage-floor ABSTAIN — never a guessed session; Muhurat =
NON-session documented limit [SUPERSEDED for the movers VERDICT layer by
DEC-PREVSESSION-MUHURAT-COHERENCE-001, MIGRATION.md #1649b: the walk still
treats Muhurat a NON-session, but a found-after-expected Muhurat baseline now
reads contiguous-True + `prev_close_muhurat`/`oi_baseline_muhurat` — a healthy
lake, never a false hole]), enriched resolvers `resolve_prev_close`/
`resolve_prev_oi` with byte-identical `.value` seams over the historical
`_prev_day_close`/`_prev_day_oi`, ONE pure `prev_baseline_provenance`, and
EMIT-ON-FALSE row projection (contiguous + abstained payloads byte-for-byte
the pre-fix wire, golden-pinned both arms + a frozen additive-key-NAME set).
HONEST: makes the wrong %change VISIBLE, does not fix the number — re-pulling
the missing session does; `preopen_report._previous_trading_day` was NOT
refactored there (named follow-up DEC-PREOPEN-PREVDAY-COMMON-001 — since
ADVANCED by main's #1634 and CLOSED by MIGRATION.md #1649a,
DEC-PREOPEN-PREVDAY-COMMON-002). READ-ONLY dashboard — ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules 100% line+branch).
Earlier: 2026-07-07 (DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001,
MIGRATION.md #1538 — the `/movers` SPOT "Top Value" (turnover) is now
TIMEFRAME-INVARIANT: it is re-sourced from the RAW 1-minute session frame
(post as-of cut, PRE-resample) via the NEW `spot_session_value_paisa`, exactly
mirroring the already-TF-invariant futures/options `_read_future_session`,
instead of the resampled display-timeframe bars — so the SAME (symbol, day,
as-of) turnover no longer CHANGES with the selected display timeframe
(reproduced NSE-NIFTY 2024-01-02 whole-day: value 1m 19583300 ≠ 5m 19833500 ≠
1day 20100000 paisa, now all byte-identical to the raw-1m answer). Three edits,
ALL in `brutex/dashboard/movers_snapshot.py` (ZERO other source files): extract
the shared raw-1m read+cut into `_read_cut_1m_frame` (§11 COMMON —
`_read_session_bars` becomes a byte-identical wrapper, preserving the
DEC-PIPELINE-AUTOPREBUILD-001 stored-vs-resample display-bar equality), the NEW
no-`interval`-arg `spot_session_value_paisa` (HLC3 × volume via the ONE COMMON
`session_traded_value_paisa`, per-bar `round_money` 2dp BEFORE × volume,
`Decimal(str(...))` matching `_read_future_session`, `None` on missing 1-minute
— never fabricates, Golden Rule #1), and a `value_paisa: int | None = None`
injection into `compute_snapshot` (`None`/omitted falls back to the legacy
display-bar reduction byte-identically, so the existing 320000/318660 goldens
pass UNMODIFIED — the injection-gate mutant killer) wired from
`compute_spot_candidates` inside the existing `skip_errors` try. HONEST
envelope: a spot-ranking CORRECTNESS fix (Top Value becomes a genuine session
property, mirroring futures/options) that CHANGES the displayed spot Top Value
on coarse timeframes versus today — a SANCTIONED correction, not a silent
break; value(1minute) byte-unchanged; a mid-session as-of legitimately keeps
1day whole-day (the bar IS the day), so invariance is scoped PER shared
effective as-of; disclosed I/O — one extra month-bounded raw-1m read on the
daily/stored-first paths + a double-read on the resample-fallback path (bounded
O(one month), candidate-cache-memoised per write-generation); futures/options
value output BYTE-UNCHANGED (`session_traded_value_paisa` +
`filter_to_ist_minute` + the `_read_future_session` 6-tuple untouched; diff-stat
shows ONLY `movers_snapshot.py` in source; the F&O suites re-run UNMODIFIED
green). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; READ-ONLY dashboard ranking;
no lake/schema/scope/vocab/rate change; touched module 100% line+branch;
ruff/ruff-format/`mypy --strict`/pyright clean.)
Earlier: 2026-07-06 (DEC-CLI-GREEKS-ENRICH-001, MIGRATION.md #1536 —
the CLI `brutex fetch` pull path now runs the SAME fenced Greeks-enrichment
pass the dashboard runs — post-drain Seam A (fetch lane drained + window
ledger persisted FIRST, single-flight lock still HELD), default-ON with the
fail-loud `BRUTEX_CLI_GREEKS_ENRICH` kill switch, inert under
`--dry-run`/`--inspect` via `_no_persist`, halts (`LakeWriteHalt`/
`GreeksEnrichPoolBroken`) alert-then-re-raise, a fenced failure NEVER fails
the pull — and a NEW `brutex enrich-greeks` subcommand delivers the #1188
bulk v1→v2 re-enrich (exit 0 iff fully clean — cron-visible). ONE COMMON
wheel-free `brutex/data/greeks_enrich_pass.py` owns the fence (MOVED from
`historical_pull`, whose `_enrich_greeks_from_minute` is now a thin
delegator — dashboard behavior byte-identical, outcome types aliased to the
SAME objects) + the chain-union lake-walk target discovery (CE/PE only,
zero Parquet opens). HONEST: NOT O(1) — discovery is an O(option partition
dirs) walk and the pass re-READS the scoped stored bars every run (the
stamp-driven `already_current` gate makes re-runs cheap by skipping writes
+ solves, never reads); the CLI L2 heal seam landed INDEPENDENTLY on
main as DEC-CLI-LAKE-HEAL-L2-001 (#1493) and the merged fetch path runs it
BEFORE this enrich seam — the dashboard heal-L2-before-Greeks-enrich
ordering law holds on the CLI too; the six-step strict gate
(DEC-PULL-SIX-STEP-ORDER-001 #1527, merged in flight) sits BETWEEN the
dashboard's own F&O L2 heal (`_heal_lake_after_pull`) and the enrich
delegator inside `pull_contract_candles` — `_GREEKS_STRICT_WITHHELD` is a
distinct instance of the COMMON `GreeksEnrichOutcome`; the CLI never calls
that seam — its Seam A enrich invokes the COMMON fence directly, so the
strict gate cannot touch it. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
F&O lake REFERENCE_ONLY; no scope/vocab/rate change; touched modules 100%
line+branch).
Earlier: 2026-07-07 (DEC-MOVERS-ASOF-LATEST-DEFAULT-001, MIGRATION.md #1535 —
the `/movers` Spot default as-of TIME resolves to the SAME latest available
minute across EVERY timeframe, so 1m ↔ 2m ↔ 3m ↔ 3hour ↔ 1day always show the
identical latest close; READ-ONLY dashboard, ENGINE UNCHANGED —
NIFTY/BANKNIFTY/SENSEX).
Earlier: 2026-07-06 (DEC-BENCH-ABS-BASELINE-001, MIGRATION.md #1534 —
the committed per-hardware ABSOLUTE benchmark baseline + fail-loud compare gate
on bench-m4pro + mechanical no-silent-re-baseline governance (closes the
STANDARDS_100PCT.md §11 P1 "benchmark baseline JSON + `--benchmark-compare
--fail` gate" backlog item, the deviation from the literal flag ledgered): NEW
pure-stdlib zero-brutex-import `brutex/observability/bench_baseline.py` (the
comparator/schema/capture/governance core + the ONE hardware-key format law) +
`hardware_fingerprint`/`hardware_key` on the COMMON `config/hardware_envelope.py`
+ the env-gated 6-entry measurement suite (`tests/bench/test_absolute_baseline.py`
over `_abs_baseline_registry.py`, house best-of-N-MIN idiom) + committed
baselines at `tests/bench/baselines/<hardware_key>.json` (`tolerance_pct`
structurally capped so a 2x slowdown can NEVER pass any governed config; a
MORE-than-4x speedup FAILS as SUSPICIOUS_SPEEDUP, more-than-2x WARNs "baseline
stale") + `scripts/ci/bench_baseline_gate.py` (validate/compare/capture/govern/
key; exit 0 ok-or-LOUD-abstain / 1 numeric with ONE bounded fresh-process
re-measure / 2 structural never-retried). bench-m4pro gains
measure→compare→capture→artifact + a GitHub-hosted `bless-baseline` job (the
ONLY contents/PR-write holder — the candidate travels runner→artifact→verbatim
API commit with no edit surface); ci.yml's REQUIRED `bench` job gains schema
`validate` on every run + the `govern` entry-cell diff gate on PRs with LIVE
`gh api` provenance-run verification pinned to THIS repository. GOVERNANCE LAW
per cell class: a MODIFIED cell needs a NEW ledgered DEC id + the literal entry
key + the literal value token + a FRESH live-verified run; an ADDED cell rides
the standing bootstrap authorization; a REMOVED cell / top-level change needs a
ledgered note; a DELETED file needs its filename in the added lines; NO
override env. BOOTSTRAP: ZERO measured numbers ship (`tests/bench/baselines/`
holds only README.md); the first baseline is measured by the REAL M4 Pro and
auto-blessed via a bot PR the operator nudges + merges (GITHUB_TOKEN PRs do not
trigger CI; NO PAT). HONEST: bench-m4pro stays NON-required (a regression is
caught post-merge as a red run + issue, not a merge blocker); the gate is
dormant until the runner is online AND the first bless merges; the gate catches
strictly-GREATER-than-tolerance drift ONLY (strict `>` boundary law — a
regression exactly AT tolerance passes, test-pinned) — at-or-below-tolerance
drift accumulates until a ledgered
re-bless (the 1.1x ratio tiers + the single-canary-op 50 M/s floor are
complements, NOT a sub-tolerance net); a STANDING red — numeric
SUSPICIOUS_SPEEDUP or STRUCTURAL alike — dies at compare before a dispatch
capture can run, recovery is removal-first (`docs/runbooks/bench-baseline.md`).
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change;
`bench_baseline.py` + the touched `hardware_envelope.py` 100% line+branch).
Earlier: 2026-07-06 (DEC-LEDGER-DELTA-O-PAGE-001, MIGRATION.md #1533 —
mid-pull ledger-read O(page) discipline restored: (a) the /coverage/windows +
pull-progress DuckDB fast paths now FOLD pending window-ledger deltas
latest-wins IN SQL — NEW `lake_read.dedup_qualify_latest_sql`/
`dedup_latest_source_sql` (the keep-LAST twin of `dedup_qualify_sql`; bound
base-path equality ordering, never LIKE — the `/deltas/`-path + `'l'>'d'`
filename traps live-verified dead; demotions survive, filters apply OUTSIDE
the fold so no ghost rows) assembled at ONE `db_view._ledger_scan_source`
seam (per-attempt re-listing = the save-time prune-race self-heal); the count
caches key on the composite (base-mtime + delta-name-set) stamp token at
generation=0 (delta appends AND cross-process CLI writes invalidate;
mark_dirty storms never thrash); the schema probe stays BASE-only (legacy
12-col lakes defer wholesale — the union_by_name NULL-fill crash is
unreachable; base-less delta-only lakes ride the fast path); a
persistently-bad delta degrades to the legacy fold / Python-fold counts,
never a 500 or blanked phases; SANCTIONED: mid-pull rows converge to the
steady-state DuckDB order + counts_since_pull_start now appears mid-pull.
(b) the three db_view ledger record caches collapse onto ONE reused
`MoversSnapshotCache` (single-flight — 8 concurrent cold pollers pay 1 parse;
the #920 adaptive floor EXTRACTED to `summary_cache.effective_adaptive_floor`
and delegated byte-equal, cap 120 s; stale serves bounded + logged; the
base-less delta-only /db grid now renders). (c) /results keys on NEW
`lake_revision.sweep_results_revision` (sweeps dir digest + the audit chain's
file stat — a SWEEP_START append now invalidates; probe-None computes fresh
UNCACHED). (d) `build_sweep_leaderboard` splits into the revision-cached
`load_sweep_scan` (frozen `SweepScan`, ALL honesty fields) + the
byte-order-preserved rank (golden vs `ResultsStore.top_k`); `_snapshot_eras`
streams `iter_entries` (O(1) memory; audit.py untouched). (e)
`bx-rate-monitor.js` + `bx-liveproof.js` pause polling in hidden tabs (the
v29 `document.hidden` guard + visibilitychange catch-up). (f) NEW
`tests/bench/test_ledger_delta_o_page_o1.py` — 7 measured pins. HONEST: the
fold is ONE O(total rows + delta-file footers) columnar QUALIFY per FRESH
stamp — NOT literal O(page); delta compaction + the AuditCursor resume are
NAMED future work. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; steady-state
(zero-delta) SQL text + payloads byte-identical (spy-pinned); touched modules
100% line+branch).
Earlier: 2026-07-06 (DEC-SWEEP-EXIT-INDEX-FASTPATH-001 + DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 / ADR-0017,
MIGRATION.md #1531–#1532 — the sweep-metrics Rust tail is now ON BY DEFAULT:
`SWEEP_RUST_METRICS_DEFAULT=True` (the exact "separate later ADR" flip #1186
deferred; revert = ONE constant; `BRUTEX_SWEEP_RUST_METRICS=0` is the
per-process operator kill switch — `_FALSY` beside `_TRUTHY` on
`sweep_lookahead`, consumed via its public `env_flag_tristate`, never
copied), licensed by TWO safety layers —
ORIGINALLY authored + proven fully green under the prior default-OFF before
the flip, though the pushed branch is ONE reconstructed post-wipe commit so
no shipped-first commit boundary exists (see ADR-0017 Status / #1532): (a) the
exit_idx-native FAST PATH (#1186's named follow-up, DONE) — both trade
simulators return each mask's certified exit-bar indices
(`simulate_trades_with_exit_indices` / `simulate_masks_with_exit_indices`
over the NEW stdlib-only COMMON `brutex/engine/exit_index.py`:
`build_ts_index` consolidating the three inline dict copies +
`bars_strictly_increasing` producer certificate + `exit_indices_consistent`
consumer belt) so the kernel arm consumes them O(trades) with NO per-mask
O(N_bars) `exit_ts`→index dict build (an inconsistent stream DEMOTES to the
dict, never to the Python oracle — results byte-identical; measured 6.1×
sandbox tail ratio, bench-gated ≥1.15×; the sweep spine threads the pairs
through the ONE `_iter_mask_metrics`; `_reattribute_chunk_failure` stays on
the oracle); and (b) a per-process BURN-IN SELF-CHECK — the first 16
kernel-served masks are bit-compared (`struct.pack` floats, signed zeros
included) against the Python `_drawdown_stats` oracle; a mismatch SHIPS THE
ORACLE VALUE and AUTOREVERTS the kernel for the process
(`kernel_autoreverted` ERROR + latch; `kernel_disabled` short-circuit). The
frozen `_GOLDEN_CONTENT_HASH`/`_GOLDEN_RUN_ID` stay UNCHANGED — the default
(no-env) arm reproduces them byte-for-byte with exactly ONE engaged event
(golden-pinned; a burn-in-poisoned kernel lands on the pinned bytes with one
autorevert event). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
scope/vocab/rate change; whole-engine unit gate 100.00% line+branch (1928
tests); ruff/ruff-format clean, mypy `--strict` clean, pyright at the
pre-change baseline).
Earlier: 2026-07-06 (DEC-PULL-UI-STALENESS-001 + DEC-PULL-HEADER-SUBUNIT-001,
MIGRATION.md #1529–#1530 — the pull-UI staleness pack, FRONTEND-ONLY (zero Python
source change; tests + docs excepted): both poll surfaces (the site-wide
`bx-rate-monitor.js` widget + the `/universe` pull-progress header) gain a
CLIENT-clock "updated Xs ago" readout on a ~1 s ticker (amber → LOUD RED explicit
"STALE" past justified thresholds — widget warn 10 s / red 30 s = 10× cadence =
backoff cap; universe warn 5 s past the retained 600 ms quick-retry envelope /
red 15 s) + a SELF-RECOVERING poll (fetch rejection AND non-ok AND malformed body
all counted failures; quick retries then 3 s→30 s capped exponential backoff,
NEVER gives up — the 2026-07-06 38-minute frozen-tab incident fix; the bounded
give-up that painted "live progress feed dropped" via innerHTML and stopped
polling forever is REMOVED, its pinned tokens kept with genuine semantics; a
non-ok poll can no longer settle the header as "— nothing to pull"; the stale
state clears automatically on the next successful poll), and the `/universe`
header + a new `#subunit` counter show per-expiry sub-unit progress ("expiry
30Sep25 · sub-unit N this unit") derived ENTIRELY client-side from
`s.current`/`s.completed` transitions (distinct-label count per completed/total
anchor, Set-capped 4096; the `data/fno_symbol.py` grammar mirrored right-anchored
so hyphenated underlyings never confuse the parse; spot/empty labels degrade to
omission, never garbage). textContent-only DOM; substring tests pin PRESENCE of
the JS, not execution (honest envelope — no headless JS runner); ZERO backend
change; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX).
Earlier: 2026-07-06 (DEC-O1-CLAIMS-HONESTY-001, MIGRATION.md #1528 — the O(1)-claims honesty +
preopen-discovery pack: the /reports preopen symbol discovery rides the COMMON #896 generation-keyed dataset
cache (per-basis keys, warmed post-pull off the warm's one walk, pullability deliberately live), FNO pruned +
interval-bounded at walk descent (sanctioned: F&O contract partitions no longer report symbols), the manifest
rebuild always stamps the PRE-walk ledger token (mid-walk race → the stale-stamped base still LANDS — it is
record_dataset's write-side convergence seed, the round-6 restore of an interim SKIP design — plus one
observe-only db_view.manifest_rebuilt_stale INFO; the load-side token gate stays the freshness authority),
welcome.html + Golden Rule #5 reworded to the TRUE two-tier O(1) enforcement of
tests/bench/_o1_tiers.py (robust 3–10× shared-CI / strict 1.1× M4-Pro via BRUTEX_M4PRO_STRICT=1 / measured 3×
cache-tier — words only, NO gate weakened), and two new bench pins (u128 two-limb mask + orchestrator audit
append). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules 100% line+branch).
Earlier: 2026-07-06 (DEC-PULL-SIX-STEP-ORDER-001, MIGRATION.md #1527 —
the operator's STRICT six-step pull order: every Groww pull executes
INDICES-SPOT → STOCKS-SPOT → INDICES-FUTURES → STOCKS-FUTURES →
INDICES-OPTIONS → STOCKS-OPTIONS (empty groups skipped), enforced at the TWO
existing execution chokepoints with ONE pool/controller build each — an
always-on `ingest_common.partition_spot_steps` group loop INSIDE
`pull_all_spot` (the COMMON `PullStep` IntEnum, value==rank 1-6;
`progress_counts(symbols=)` group-scoped until-clean) and a 4-key
`(ledger-market, is_index)` F&O bucket sort with STEP-major until-clean
(`_contract_market` KEPT verbatim — ledger namespaces byte-unchanged, no
re-pull; unresolved underlyings order stock-side, WARNed + counted, never
dropped); post-steps (Greeks → prebuild → movers) fire only after ALL
applicable steps SETTLE (settle-based, never `bars > 0`) — cleanset default
byte-compatible with DEC-PREBUILD-CLEANSET-001, NEW opt-in
`BRUTEX_PIPELINE_STRICT_GATE=1` all-or-nothing barrier (fail-loud parse
resolved once at launch → clean 400; pure `step_settle_verdict` +
`PriorStepsVerdict` threaded to the Greeks seam — a withheld pass reads
"withheld (strict gate) — re-attempted next pull", never FAILED). COMMON
`pull.step.started/settled/halted` events + an observe-only thread-safe
`StepOrderMonitor` (`raise_on_violation=True` is the test oracle); the
2026-07-05 log-shaped ordering regression + a fetch-count parity bench pin
"reorders, never re-fetches". HONEST: an ORDERING change, NOT a speedup —
~seconds added per pull (bounded analysis, not measured); in async-pipeline
mode a step's k→k+1 settle is PRODUCER-side (fetch-issue order strict; tail
writes drain terminally but are INVISIBLE to the producer-fed
`StepOrderMonitor` — `pull.step.late_write` observes only producer-side
writes and stays 0 on a healthy async pull; submit-time step stamping
through `FetchPipeline` is a named follow-up, NOT claimed)
while the POST-step barrier is fully drain-guarded; the mass-empty
breaker's trip evidence is now per-STEP-GROUP pass on BOTH spines — F&O
per-(market, index/stock) BUCKET, SPOT per indices/stocks group (a
disclosed narrowing, #1527 R6+R9 — evidence resets at the group boundary;
the ~4-series INDICES-SPOT group on an incremental pull is below every trip
threshold, so throttle-poisoned ENGINE index windows can settle SKIPPED
before the stocks group's breaker trips — remediation `brutex
reopen-skipped`; thresholds/canary/bounded-settle machinery unchanged); the
CLI `brutex fetch`
spot path is the NAMED follow-up DEC-PULL-SIX-STEP-ORDER-CLI-001, NOT
claimed. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; F&O lake REFERENCE_ONLY;
touched modules 100% line+branch).
Earlier: 2026-07-06 (DEC-ETC-REGIME-HISTORY-001, MIGRATION.md #1524 —
the options exchange-transaction-charge (ETC) regime tables STOP charging a
fabricated rate in the pre-2024-10-01 window: the two `(date.min, <current
rate>)` UNVERIFIED placeholder rows are DELETED and replaced by explicit
rate-less REFUSAL rows (`RegimeRow.rate_bps_x100 = None`) — the ONE COMMON
`rate_for_date` raises the NEW typed `UnverifiedCostRegimeError` (window +
identified circulars + remediation), and the options executor SKIPS-AND-COUNTS
per trade (`RejectReason.COST_REGIME_UNVERIFIED`,
`OptionsRuleResult.n_rejected_cost_regime_unverified`, per-trade
`options.reject.cost_regime_unverified` WARN + ONE aggregate
`options.sim.unverified_cost_window` WARN + a CLI leaderboard footer), exactly
like the LOT_SIZE_UNKNOWN precedent — the sim completes, is loud, and NEVER
invents a rate. VERDICT BASIS (the 2026-07-06 4-strategy fetch round,
HARD-LINE Golden Rule #1): ZERO official-trace retrievals (every official
host + web.archive.org egress-blocked) — the six pre-boundary regimes were
reconstructed to circular-number precision but reconstruction is NOT
verification, and they are structurally member-monthly-turnover SLAB ladders
with no honest flat per-trade equivalent. Exactly TWO rate rows survive (NSE
0.03503% / BSE 0.03250%, both eff. 2024-10-01, VERIFIED on the pre-existing
COSTS_VERIFIED §2 rows 3-4). NO runtime escape hatch; override paths are DATA
only (a cited RegimeRow, or the ADR-gated top-slab-ASSUMED path). SANCTIONED
HONEST BREAKAGE: options trades 2021-01-01→2024-09-30 are now counted refusals
(previously silently priced at the invented current rate — likely
UNDER-costed); the honestly-priceable window is [2024-10-01 → today] until
circulars land; the FA73061 forward alert (NSE split 3,552+1/cr eff.
2026-03-01, total unchanged) stays UNVERIFIED + NOT encoded (§20 row 6,
highest-priority fetch). Costs + options-executor + CLI-footer + tests + docs
only — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap
change; brutex.costs + executor 100% line+branch; the CLI leaderboard
renderer's pre-existing `# pragma: no cover` LIFTED after the round-1 review
caught the honesty footer riding it with ZERO executing tests —
`_render_options_leaderboard` is now directly unit-tested at 100% line+branch
with fail-loud direct attribute access instead of the silent `getattr`
default, tests/unit/test_cli_options_leaderboard.py).
Earlier: 2026-07-06 (DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001, MIGRATION.md #1522 —
the `GET /api/v1/pull/rate` CLI-bridge file read moved OFF the uvicorn event loop: a NEW
`brutex/dashboard/rate_bridge_cache.py` `RateBridgeCache` memoises the decoded bridge
snapshot behind a 2 s TTL + an `mtime_ns:size:ino` stat content token (NEW public
`lake_revision.rate_bridge_revision` over the shared `_stat_token` core, `include_ino`
opt-in — every pre-existing token byte-unchanged), refreshes post-TTL under the COMMON
`run_with_deadline` freeze guard (0.5 s; stat AND read inside the guard) with a
NON-BLOCKING single-flight (followers serve last-known immediately; at most ONE abandoned
daemon per outage, late-return self-heal; a guard SPAWN failure — `Thread.start()` under
thread exhaustion — is caught by the owner: flag released, ONE `refresh_spawn_failed` WARN
per outage, served `unverified`, next poll retries — never a route 500 nor a permanent
wedge, the hostile-review MAJOR fold; KeyboardInterrupt/SystemExit propagate with the flag
released), and is called via `run_in_threadpool` through a
`build_app(rate_bridge_cache=)` injection seam. TWO ADDITIVE payload keys stamped in ONE
place: `bridge_source` ∈ {live, cached, unverified, unwired} + `bridge_probe_age_s` — no
stale illusion (the cache stores the SNAPSHOT, never the view; ages recompute per request
from its own `wall_time`, TTL 2 s < STALE_AFTER_S 12 s test-pinned). Every pre-existing
payload key byte-identical; teeth verified (the same wedged read blocked the pre-change
route 8.02 s, bounded < 2 s fixed). HONEST: liveness/latency only, no throughput claim;
the wedged daemon cannot be killed (#999 stance); the route-level bound holds GIVEN a free
AnyIO worker token (the DEFAULT 40-token `run_in_threadpool` limiter is shared with the
sibling UNdeadlined lake offloads — pool-wide wedge protection is a named follow-up); the
CLI WRITE-side publish hang is a named follow-up. READ-side dashboard only — ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
`rate_bridge_cache.py` + `lake_revision.py` 100% line+branch).
Earlier: 2026-07-06 (DEC-GREEKS-SPOTJOIN-STOCK-001, MIGRATION.md #1482 —
the Greeks spot-join drift-recovery ladder gains the evidence-grounded STOCK
arm, CLOSING the SPOTJOIN-2 named follow-up of #1188: NEW
`brutex/data/spot_drift.py` scans the REAL `bars/<exch>/{INDEX,CASH}` symbol
dirs once per enrich pass (O(dirs), parent-only, partial-tolerant) and
verifies each against the instrument master's IDX/EQ `symbol_name`s
(`<lake_root>/instruments.duckdb`; empty master → zero lake I/O) under ONE
pure fold (`lake_keys.stock_drift_fold` — upper + drop space/hyphen, the
EXACT mirror-pinned twin of `universe.normalize_underlying_symbol`; the
own-`<EXCH>-` prefix strip is DISK-SIDE ONLY with raw-fold precedence). An
entry needs a real dir + a UNIQUE master fold-match — a ≥2-master fold is
POISONED even with one dir (capped `spot_drift.ambiguous_master` WARNs);
same-instrument fragments are ALL offered ordered (master-exact first, then
lexicographic — the probe decides, so a dataless clean dir never shadows a
data-carrying fragment). The map rides `spot_recovery_candidates(stock_drift=)`
as the APPENDED last rung (2-arg calls byte-identical, prefix invariant
pinned), threaded per-pass-stably through `_default_seams`/
`_GreeksWorkerContext` (plain picklable dict; workers never scan; sequential
≡ parallel bytes golden-pinned); the ONE `spot_key_recovered` log gains an
additive `rung∈{alias_ladder,stock_drift}` field. No master / confirmed read
error / no drift → the honest NO_SPOT (never a fabricated spot, never a
failed pull; MemoryError always re-raises raw); the held-open-store
integration pin proves the loader works beside a live dashboard
`InstrumentStore` handle. HONEST: NO `%26`→`&` recovery (stored-key encoding
UNVERIFIED — the lake_keys pledge stands); read-time recovery ONLY —
`lake_heal`/`detect_legacy_drift` untouched; the `/db` moneyness twin is the
NAMED follow-up DEC-DB-MONEYNESS-STOCK-EVIDENCE-001 (until it lands a drifted
stock lake shows recovered Greeks but `/db` moneyness UNKNOWN). ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; `lake_keys.py`/`spot_drift.py`/
`greeks_enrich.py` ALL 100% line+branch).Earlier: 2026-07-05 (DEC-STORED-DATA-VALIDATION-001 + DEC-MUTATION-ALERT-CANCELLED-001,
MIGRATION.md #1510–#1511 — the stored-data validation pack, the round-2
adversarial audit's confirmed defects fixed as ONE change set: (a) price
POSITIVITY is enforced at the ENGINE READ BOUNDARY
(`feed/replay.py::arrow_table_to_bars`, which gains `source=` + names the
partition + bar ts on a corrupt row), NOT the universal `CandleBar` constructor
— a zero/negative but internally-ORDERED legacy stored bar (reproduced: day_low
−1000 into Stage-1A) now raises LOUDLY when READ into the engine, while the bar
container stays permissive so the legitimate downstream corrupt-input
classifiers that build a non-positive bar on purpose (Greeks BAD_INPUT, the
executor `require_tradeable_price` guard, the Rust trade-sim out-of-envelope
fallback) keep working (O(1)/bar, bench-neutral). (b) NEW COMMON `brutex/data/price_sanity.py` (ONE vectorized
positivity+ordering mask) wired at the prebuild's 1-minute read boundary
(`make_lake_read_1m`) — a high<low 1m bar can no longer LAUNDER into
self-consistent-looking derived buckets across the 19 TFs + 1day; the raised
`ResampleInputError` rides the EXISTING per-symbol prebuild quarantine (symbol
named, derived TFs not written, rows never silently dropped/fixed). (c) the
lake census/heal scanner reads the OHLC columns in its SAME per-file pass
(`BAR_SCAN_COLUMNS`) and emits an OBSERVE-ONLY `PriceSanityRows` finding
(census `price_files`, engine verdict flips; the healer FILTERS it — strictly
row-removing, never value-editing). (d) the vendor OHLC clamp is counted +
WARNed once per window (`groww.parse.ohlc_clamped`, observe-only, parse
byte-unchanged). (e) movers/db 2dp display cells ride ONE shared
`static/bx-format.js::bxFixed2` (string-based ROUND_HALF_UP — the
`preopen_report._round2` twin; node-executed golden), fixing the IEEE
`toFixed(2)` half-boundary divergence ('1.005' → "1.01", not "1.00"). (f) the
mutation workflow's alert steps fire on `cancelled()` too — a timeout-KILLED
cargo-mutants job (live-probed run 28748795864, both Rust jobs killed at
exactly 120 min) now alerts instead of dying silently; the Rust jobs'
chronic timeout itself is honestly ledgered, NOT papered over with an
unevidenced budget raise. Sanctioned behavior change: a previously-silently-
accepted CORRUPT stored bar now fails loudly; clean lakes byte-unaffected
(ONE corrupt golden FIXTURE — a fabricated negative low — repaired + regenerated,
honest note in #1510; every other golden unmodified). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
scope/vocab/rate change; touched modules 100% line+branch).
Earlier: 2026-07-06 (DEC-SUSPICION-SWEEP-FIXES-001, MIGRATION.md #1492 —
the 19-agent suspicion-sweep fix + hardening pack, 3 confirmed defects + 4 gap pins in ONE
change set: **(D1, MED)** the SPOT pull's AIMD ladder is no longer blind to client-absorbed
429s — the spot build sites bind NO controller feedback hooks into the Groww client and
`run_ingest._with_paced_client` rebound pace ONLY, so the client's bounded fast-retry
(`max_attempts=6`) swallowed 429s against a NO-OP `on_rate_limit` (~18 vendor rejections per
designed 3-hit step-down; sub-exhaustion bursts invisible — empirically proven: 0 ladder hits
for 2 vendor 429s pre-fix). The FULL hook triple (pace + on_rate_limit + on_success) is now
bound at that ONE COMMON spine seam (every production spot pull — dashboard bulk + single +
CLI — flows through `run_groww_spot_ingest`), via NEW `GrowwClient.with_rate_hooks` /
`ClientPool.with_rate_hooks` (re-fanned to every leaf); the spine's own per-window hooks stand
DOWN when the client self-reports (NEW `client_self_reports_rate_limits`/`_successes`
predicates — the feedback twins of `client_self_paces`; `on_timeout` stays spine-owned, the
client never fires the timeout ladder). **(D2, LOW)** the F&O spine no longer double-counts a
propagated 429: the client fires `on_rate_limit` per occurrence INCLUDING the final one before
the exhausted error propagates, and `ingest_option_contract` fired once more → 7 ladder hits
per exhausted window instead of 6 (proven: 3 hits for 2 occurrences pre-fix) — the spine hook
now stands down on a self-reporting client (same predicates). Groww rate CAPS untouched —
FEEDBACK wiring only, never the ceiling/governor values (§2 Rule 6). **(D3, MED)** the /db
window-ledger route no longer re-parses the whole ledger Parquet (~841k rows measured) per
paging click — it rides the mtime-keyed `db_view.window_ledger_rows_cached` (the SHARED
`_ledger_records_cached` parse + a memoised projection through the ONE row-shape source, NEW
`window_ledger.ledger_record_row`, which `to_rows` also delegates to); the route's stale
"thousands / small sidecar" honesty note rewritten (#864 blurb reconciled below). **(G1)**
sweep-preflight FRESHNESS gate: `cmd_sweep_all` now refuses (loud, remediation-naming) when a
requested derived TF's stored partition drifts from a fresh 1-minute re-derivation (NEW
`tf_autoderive.lake_derived_freshness_verdicts` — sharing ONE lake reader with the doctor via
extracted `read_lake_bars_frame` — folded through the pure
`sweep_preflight.preflight_derived_freshness`; abstains never block: non-derived intervals /
no stored partition / no 1m base). **(G2)** a calendar-suite enumeration pin: no MUHURAT_SESSIONS
entry on/after 2025 may close after 15:30 IST (a future evening session would silently re-arm
the `clamp_window_end` partial-settle hazard — adding one now fails loudly, forcing a
Muhurat-aware clamp decision). **(G3)** an AST drift pin: the epoch-anchored legacy
`feed.resample_bars` is imported by NO production module (definition + feed re-export only) —
the dual-resampler split can never silently reach a production path (DEC-RESAMPLE-ANCHOR-002).
**(G4)** `rate_common.py`'s stale "operator-DIRECTED 5 r/s ceiling
(GROWW_RATE_LIMIT_RPS_CEILING)" recovery docstring reconciled to the production 8 r/s
(DEC-GROWW-RATE-CEILING-8-002; the vendor-neutral `CEILING_RPS=5` DEFAULT unchanged). ENGINE
UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate-cap change; touched modules 100%
line+branch).
Earlier: 2026-07-06 (DEC-HW-ENVELOPE-CONSOLIDATE-001 + DEC-THREAD-BUDGET-BOUNDS-001,
MIGRATION.md #1490–#1491 — the hardware-envelope CONSOLIDATION + thread-bounding pack:
(1) the TWO hardware-envelope modules (#1360's `brutex/hardware_envelope.py` + #1401's
`brutex/config/hardware_envelope.py`) are consolidated into ONE at
`brutex/config/hardware_envelope.py` (Golden Rule #11 COMMON) — the #1360 RAM-derived
budget API (`detect_total_ram_bytes` / `UNDETECTED_RAM` / the fail-loud no-floor-clamp
`memory_budget_bytes` consumed by `sweep_mem_budget` + `bloom`) is ported VERBATIM, the
old module DELETED, every consumer repointed, the interim two-module RAM-delegation pin
of the #1167 conflict resolution SUPERSEDED (its stripped envelope-mode
`memory_budget_bytes` duplicate stays DEAD — zero consumers), and the third sizing copy
`greeks_enrich.greeks_pool_workers` now DELEGATES to the COMMON `cpu_pool_workers`
byte-unchanged (the `auto_sweep_workers` identity, pinned across cpu counts).
(2) the polars GLOBAL thread pool is bounded ONCE per process at the ONE entrypoint
(`brutex.cli` dispatch, right after the byte-exact-stdout-pinned polars-free `version`
fast path and before the first `import polars` any dispatch can trigger — CLI fetch AND
the in-process uvicorn dashboard both enter there; outcomes log once per process) and
every concurrent-reader DuckDB connection (`InstrumentStore.open` / `RegistryStore.open`
/ `coverage_reconcile` / `movers_warm`) is bounded at creation (`SET threads TO
<budget>`, `physical_cores // DEFAULT_EXPECTED_LIBRARY_POOLS(2)`) — the per-connection
DuckDB caps ARE the contention fix, while the POLARS default is FULL physical width
(divisor 1 — the #1401 prebuild's worker threads share the one global pool; halving it
would waste half the box in the phase that matters most, and the movers PrebuildGate
serialises the heavy reader load away; the bound still caps polars at PHYSICAL cores
vs its own all-LOGICAL default); `data/catalog.py`'s engine connection deliberately
NOT bounded; kill switches `BRUTEX_POLARS_THREADS` /
`BRUTEX_DUCKDB_THREADS`; an operator-preset `POLARS_MAX_THREADS` is respected and an
already-imported polars gets ONE loud too-late WARN per process, never a pretend-bound.
Parallelism-only — results byte-identical; measured A/B on the small CI fixture
~NEUTRAL (1.14×) as predicted — a CONTENTION fix whose win shows at production
concurrency, NO M4-Pro speedup claimed (Golden Rule #10). The Groww fetch pool / rate
ceiling / governor + the sweep + Greeks per-worker determinism pins +
`data/catalog.py` are UNTOUCHED. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched
modules 100% line+branch).
Earlier: 2026-07-05 (DEC-RAM-BUDGETS-001, MIGRATION.md #1360 —
RAM-scaled memory budgets: NEW stdlib-only `brutex/hardware_envelope.py`
(`detect_total_ram_bytes` — POSIX sysconf primary, macOS `sysctl hw.memsize`
fallback UNVERIFIED-on-real-macOS, any failure → the honest `0` = "unknown"
sentinel, cached once per process; `memory_budget_bytes` — env override wins
fail-loud, sentinel → floor EXACTLY, detected → `int(total × fraction /
workers)`). The TWO §8-keyed hand-picked byte budgets are now derived through
it: the sweep rank-region default (`sweep_mem_budget.DEFAULT_MEM_BUDGET_BYTES`
8 GiB → floor of a 1/6 fraction; `BRUTEX_SWEEP_MEM_BUDGET_BYTES` override
byte-unchanged, #1352 contract preserved) and the Bloom bit-array wall
(`bloom.MAX_BLOOM_BYTES` 16 GiB → derived once at import from a 1/3 fraction
over NEW floor `MAX_BLOOM_BYTES_FLOOR` + NEW fail-loud env
`BRUTEX_MAX_BLOOM_BYTES`; `BloomTooLargeError` semantics unchanged, refusal
still before any allocation; results-safe — the Bloom is an L1 prefilter over
the LMDB exact ground truth, never in run identity). Regression-pinned: a
simulated 48 GiB §8 box derives EXACT parity with both old constants; less
RAM → smaller budgets (the portability win); undetected RAM → the old
constants bit-for-bit. Everything else in the sweep's classification stays
FIXED (LMDB virtual maps, FAIL_CLOSED capacities, determinism pins,
back-pressure queues — reasons in #1360). HONEST: a PORTABILITY + headroom
change, NOT a measured speedup; detection is TOTAL physical RAM, not
available/free. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules
100% line+branch).
Earlier: 2026-07-03 (DEC-INGEST-BOUNDARY-INTEGRITY-001, MIGRATION.md #1294 —
the ingest data-integrity pack, four boundary gaps as ONE change set: (a) the
ingest-boundary session filter is now CALENDAR-AWARE (`brutex/data/session.py`
`filter_to_trading_session` via `calendars/nse.get_session`, wired at BOTH ingest
spines) — a bar on a NON-TRADING date (weekend/holiday leak) is dropped + COUNTED
with EXACTLY ONE WARN per window, and an evening-muhurat / special-weekend bar is
now KEPT (a sanctioned behavior change — the date-blind 09:15-15:30 filter wrongly
dropped it); regular-day kept-set pinned byte-identical to the legacy filter;
out-of-coverage dates (post-2026) abstain to the legacy fixed window; the daily
path stays deliberately unfiltered; resamplers untouched by design (they derive
from the stored, already-filtered 1-minute). (b) the parser's DEFENSIVE
epoch-timestamp branch WARNs once per pull (`groww.parse.epoch_timestamp`; vendor
epoch semantics UNVERIFIED — assumed Unix-UTC; parsing unchanged). (c) an
ALL-FILTERED window (raw bars > 0, session-filter survivors == 0) is demoted to a
re-pullable FAILED + `ALL_FILTERED` on the existing #1031 degraded machinery
(bounded SKIPPED settle; SPOT gated on `detect_completeness_gaps` — the dry-run
synthetic adapter would false-red ungated; F&O UNgated, its dry-run returns empty
payloads — the F&O spine's FIRST demotion, all-filtered ONLY, the #1031
observe-only stance for liquidity heuristics preserved; ORTHOGONAL to sink dedup —
a full-dedup re-pull stays SUCCESS). (d) `SinkAppendResult.bars_net_new` (#1031
computed it, never persisted) is threaded into the durable window ledger
(`WindowLedgerRecord.bars_net_new`, optional-column back-compat: an OLD ledger
loads `None` — honest unknown, NEVER a fabricated 0; `/db` grid column added).
ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules 100% line+branch).
Earlier: 2026-07-03 (DEC-PIPELINE-AUTOPREBUILD-001 + DEC-FNO-EXPIRY-SCOPE-001,
MIGRATION.md #1145–#1146 — the pipeline pack. (1) #1145: derived timeframes (the 19
intraday TFs + the derived `1day`, over SPOT + FUTURES + OPTIONS) are now written ONLY
by the pull pipeline's ONE automatic FINAL prebuild stage — run on EVERY completed pull
over the CLEAN symbol set (every target minus the UNCAPPED quarantined names, excluded
+ headlined; DEC-PREBUILD-CLEANSET-001, MIGRATION.md #1221, supersedes the #1145
strictly-after-a-fully-clean-fetch all-or-nothing gate) — every MID-PULL derived write
is REMOVED
(`pull_controller._resample_daily_after_pull` + the `BRUTEX_AUTO_DERIVE_TFS` autoderive
+ `historical_pull._derive_daily_from_minute`) and the manual "Pre-build timeframes"
button + `derive-timeframes` route are DELETED; the movers warm runs AFTER the prebuild
gated on `PrebuildOutcome.ok` over that clean set (the `fetch_clean` arm dropped by
DEC-PREBUILD-CLEANSET-001; supersedes the early-spot-warm arm of
DEC-PIPELINE-SEGMENT-MOVERS-001); `/api/v1/movers` is honestly GATED while the prebuild
runs (`PrebuildGate`, 200 `gated:true` "prebuild in progress" payload with ZERO lake
reads, 5 s self-recovering auto-retry, dry-run never gates); spot movers read the
prebuilt partitions STORED-FIRST (pinned byte-equality vs the 1-minute resample +
honest `stored_reads`/`resampled_reads` provenance); the completion headline NAMES
quarantined symbols ("N symbol(s) quarantined: A, B (+K more)", job finish detail = the
note) and `/coverage/windows` labels empty-vs-failed (`window_status_label`, both
payload paths). HONEST envelope: a fully-materialised OPTIONS lake costs ~2-3× its
1-minute footprint (operator-ACCEPTED, decree items #19+#21); the FUTURES/OPTIONS
movers SESSION readers deliberately stay 1-minute-native (the locked as-of + Greeks-
overlay semantics of DEC-FNO-TF-DERIVE-OI-001 — materialisation-invariance pinned
instead); a CLI-pulled lake gets derived TFs on the next dashboard pipeline run; the
1m→1d derivation of DEC-1MINUTE-ONLY-DERIVE-1DAY-001 STAYS, relocated into the prebuild
phase. (2) #1146: F&O expiry-level date scoping — an expiry is pulled iff within
`[from → to]` inclusive; out-of-scope = ZERO vendor calls + ZERO ledger rows + one
`fno.expiry_out_of_scope` log per (underlying, expiry), stored bars untouched; scope
SELECTS expiries, never reshapes windows (the 30d/60d law unchanged); the default
no-dates pull is byte-identical. Both: ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX).
Earlier: 2026-06-21 (DEC-FNO-MONEYNESS-LABEL-001, MIGRATION.md #869 — the
per-option moneyness + strike-distance is now shown as ONE combined cell
(**"ITM -1" / "ITM -10" / "OTM +1" / "OTM +10" / "ATM"**) instead of the split
bucket + ATM±N columns of #865 — `brutex/data/moneyness.py` `_format_label`
reformatted to `"<bucket> <signed depth>"` (sign tracks the BUCKET: ITM negative,
OTM positive, so calls AND puts read alike; depth = `|atm_offset|` strikes). The
movers screen + the `/db` F&O candle view each collapse the two columns into one
"moneyness" column (`OptionMover.moneyness_label`; `FNO_CANDLE_COLUMNS` /
`FNO_MONEYNESS_COLUMNS` now a single derived `moneyness` label). Pure DISPLAY reshape
over the SAME read-time-derived values; ENGINE stays NIFTY/BANKNIFTY/SENSEX, NO
storage/scope/vocab/rate change. Earlier: 2026-06-21 (DEC-SPOT-COMPLETENESS-2020-FLOOR-001,
MIGRATION.md #868 — the `/completeness` + `/universe` SPOT 1-minute verdict is HONESTLY
downgraded `full` → `partial` when the symbol's covered data does NOT begin in January 2020
(a late listing / late vendor data, even one starting later WITHIN 2020); one COMMON helper
`_spot_dimension_verdict_with_floor` so only symbols with the solid full-2020 history read
`full`; read/rollup-side only — `completeness.py`, ENGINE UNCHANGED). Earlier: 2026-06-21
(DEC-DB-CANDLE-COUNT-CACHE-001, MIGRATION.md #867 —
the `/db` candle-pager TOTAL (`COUNT(*)` over `read_parquet(glob)`, O(files) — a
Parquet-footer read per file, recomputed on EVERY paging click) is now memoised behind
a generation-stamped cache: `db_view.query_candles` gains an optional `count_cache`
(`CandleCountCache` Protocol) + `generation`, keyed by `candle_count_key(coords,start,
end)` (excludes limit/offset/sort/columns — the paging-invariance the cache exploits);
`build_app` REUSES the COMMON `MoversSnapshotCache` LRU and the `/api/v1/db/candles`
route passes `_summary_cache.generation` (a pull bumps it → recount, never stale —
Golden Rule #1). HONEST envelope: a cached-total HIT is O(1) in partition depth (bench
flat; COUNT runs once per dataset+generation), but paging is NOT O(1) overall — the
page DATA query (`ORDER BY ts DESC LIMIT` over the glob) is itself O(files) and ~2.4×
larger, so this removes only ONE of two O(files) footer scans (~16% on a deep
partition); true O(page) paging needs a write-time lake manifest (explicit FUTURE work,
NOT claimed). `db_view.py` at 100% line+branch; read-only viewer, ENGINE UNCHANGED.
Earlier: 2026-06-20 (DEC-UNIVERSE-BACKEND-CACHE-001, MIGRATION.md #866 —
the `/universe` BACKEND summary + instruments reads are now O(1) on a repeat poll:
NEW `brutex/dashboard/universe_cache.py` `UniverseSnapshotCache` memoises the three
lean store reads (`fno_eligible_underlyings` + `list_index_equity_instruments` +
`reference_only_security_ids`) + the derived `summary` / per-`kind` instrument rows,
keyed by a content-revision token = `(count(*), max(fetched_at))` over the
`instruments` table ITSELF (NOT the `instrument_runs` audit sidecar, which lags an
upsert — the stale-illusion trap this avoids, Golden Rule #1). Every `upsert_many`
stamps a fresh `fetched_at`, so any master change moves the token → rebuild; an empty
store keys at the stable `(0, "")`. Wired into `build_app` as a `universe_cache` param
(mirrors `summary_cache`/`movers_cache`); the routes call `_universe_cache.summary` /
`.instruments`. Honest envelope: HIT is O(1) (bench ratio 1.02× for a 100× deeper
universe; cold read scales 13×→166×), the first read after a load still pays the real
O(candidates) scan (once per revision). `universe_cache.py` at 100% line+branch;
read-only dashboard accelerator, ENGINE UNCHANGED (`InstrumentStore` untouched). This
COMPLETES on the backend the `/universe` FRONTEND O(1) work of #863. Earlier: 2026-06-20
(DEC-FNO-MONEYNESS-001, MIGRATION.md #865 — per-option
**moneyness (ITM/ATM/OTM) + a signed ATM±N strike offset** are now DERIVED at read
time (never stored — time-varying: the strike is fixed but the spot moves) for the
ENTIRE F&O universe (all ~212 stocks + indices), from the NEW shared engine
`brutex/data/moneyness.py` (`classify` / `derive_strike_step` [the **DYNAMIC** grid =
the min positive strike gap, never a hardcoded NSE rule] / `atm_offset` / `moneyness_view`).
Surfaced on BOTH the options-movers screen (as-of snapshot — `OptionMover` +
`compute_option_mover(strike_step=)` + two `movers.html` columns) AND the `/db` F&O
candle viewer (PER-BAR — `db_view.query_candles` appends `moneyness`+`atm_offset` to every
`segment="FNO"` page, classifying each bar against the underlying spot joined by timestamp;
a future reads `NA`, an option with no spot reads `UNKNOWN`). `fno_candle_lake.py`'s
`FNO_CANDLE_COLUMNS` advertises the two as `source="derived"`. Read-only — the brute-force
ENGINE stays NIFTY/BANKNIFTY/SENSEX; NO storage/scope/vocab/rate change. Earlier: 2026-06-20
(DEC-DB-VIEWER-PAGINATE-ALL-001, MIGRATION.md #864 —
EVERY `/db` tab is now O(page): the last two whole-table reads — the F&O registry
(expiries/contracts) + the window-ledger sidecar — are SQL-`LIMIT`/`OFFSET` paginated,
completing the candle/option/F&O/DuckDB-table pages already bounded. The registry is
fully O(page) (DuckDB `LIMIT/OFFSET` over its index + a cheap FILTERED `COUNT(*)`;
bench: first-page 1.04× for 10× more legs); the window-ledger is O(page) render+serialize
over an O(windows) read [RECONCILED by DEC-SUSPICION-SWEEP-FIXES-001, MIGRATION.md #1492:
the "small sidecar" claim was STALE — a pre-planned full-universe options pull holds ~841k
ledger rows, and this route re-PARSED the whole Parquet per paging click; the read now rides
the mtime-keyed `db_view.window_ledger_rows_cached` (ONE parse per ledger flush, O(1) repeat
click; the FIRST read per generation stays O(rows) — honest envelope, still NOT claimed
O(page)-read)]. `registry_store.py` + `db_view.py` at 100% line+branch;
read-only viewer, ENGINE UNCHANGED. Earlier: 2026-06-20 (DEC-DB-UNIVERSE-O1-LATENCY-001, MIGRATION.md #863 —
the `/db` + `/universe` dashboard pages made INSTANT per interaction with ZERO
correctness change: `/db`'s candle pager no longer scans the WHOLE multi-year
1-minute partition to baseline the derived %-change columns on every paging click —
`db_view.py::query_candles` now reads the prior-day last-1-minute close for ONLY the
page's own date span + the single bar before it (`_intraday_prev_close_by_page_date`,
byte-for-byte identical to the old full scan, proven exhaustively + a flat-latency
bench), and the `/db` + `/universe` FRONTENDS update in place (grid-only `#dbgrid`
refresh on paging instead of a full control-bar+datepicker teardown; a per-tab
instrument cache + debounced search + in-place completeness patch). Honest envelope:
O(page date-span) / O(visible), NOT a literal O(1)-total. Engine + 52-vocab + scope
UNCHANGED — read-only viewer only. Earlier: 2026-06-20 (DEC-LIVE-REMOVE-001,
MIGRATION.md #862 — the **entire LIVE feed feature is REMOVED in full**: the
`brutex/live/` capture package + `python -m brutex.live` CLI, the dashboard `/live`
page + `/api/v1/live/*` + `/api/v1/db/live/*` routes + the `dashboard/live_*` surface,
the `live_ticks`/`live_candles_1m` lake tables, the Movers "Live" tab + the /db "Live
lake" section + the nav `/live` link (44 files). The production historical/backtest
PULL + `rate_common` + the brute-force ENGINE are UNTOUCHED — `adapters/groww/live.py`
`build_live_client` + `pull_controller._build_live_controller` are the PRODUCTION pull
client/rate-controller, NOT the feed, deliberately kept). Earlier: 2026-06-20 (DEC-FNO-CANDLE-LAKE-001,
MIGRATION.md #861 — the F&O (futures + options) historical-candle lake is now a
FIRST-CLASS documented `/db` table (a "📊 F&O candles" row, decoded per contract) +
registered in the master `schema_meta` (`fno_candle_lake.*`), FORMALIZING data ALREADY
stored by the Groww pull at `bars/<exch>/FNO/<groww_symbol>/<interval>/…` — NOT a new
store; lake-view only, ENGINE UNCHANGED). Earlier: 2026-06-20 (DEC-MOVERS-O1-LATENCY-001,
MIGRATION.md #860 — the `/movers` page made near-O(1) per interaction: the `top_n`-independent
lake scan is split from the cheap rank + cached per coordinate (`MoversSnapshotCache`,
generation-invalidated), and the cold per-symbol intraday read is bounded to the day's
`1minute/<yyyy>/<mm>.parquet` month file (`lake_day_glob`, byte-equivalent for session-filtered
symbols); read-only lake/dashboard, ENGINE UNCHANGED). Earlier: 2026-06-20 (DEC-OPTIONS-COMPLETENESS-REAL-YEARS-001,
MIGRATION.md #859 — the `/completeness` OPTIONS table shows only the years that ACTUALLY have
expiries + an honest `partial` for late-listed underlyings (JIOFIN/ETERNAL): a 0-expiry
pre-listing year no longer inflates `years` to a fake 2020→2026 span nor keeps it perpetually
`growing`; read/rollup-side only — `brutex/dashboard/completeness.py`, no pull/engine change).
Earlier: 2026-06-20 (DEC-1MINUTE-ONLY-DERIVE-1DAY-001,
MIGRATION.md #853 —
§1/§3/§6 facts: **1-minute is now the ONLY granularity fetched from Groww** (spot
+ futures + options); `1day` (PDH/PDL anchor) + the 19 intraday TFs are DERIVED
from the stored 1-minute and written to the lake (`spot_daily_resample`), never
separately vendor-fetched. This supersedes the operator-chosen granularity dropdown
of DEC-PULL-1DAY-FIRST-001 (#723, removed) + re-introduces the 1m→1d derivation
removed by DEC-REALDATA-ONLY-NO-1M-TO-1D-001 (#852) as the always-on PRIMARY path.
The "Verify vs NSE" bhavcopy cross-check is REMOVED in full; the F&O historical
DEFAULT window is per-contract `expiry − 3mo → expiry`. The brute-force ENGINE stays
NIFTY/BANKNIFTY/SENSEX. Earlier: 2026-06-19 (DEC-CLAUDEMD-ADAPTER-CITE-RECONCILE-001,
MIGRATION.md
#846 — §6 symbol-resolution rows refreshed after the Track-1 data-quality fix
series #799/#800/#801 merged: the `Symbol/key format` + `Track 1 brute-force
universe` rows now cite the master-authoritative `by_exchange_symbol` +
`groww_symbol_by_pair` resolution (DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001 #845, which
resolves hyphenated `BAJAJ-AUTO` + verbose-symbol `MIDCPNIFTY`) instead of the
stale "via `by_groww_symbol`" wording + drifted `adapter.py` line citations; the
`Historical OHLC` row's `adapter.py` line pointer refreshed to the relocated
`fetch_ohlcv`. No substantive §6 fact changed — the merged trio (1-day resample
#842, until-clean-all-outstanding #844, symbol-resolve #845) is lake/adapter/ingest
only, the brute-force ENGINE stays NIFTY/BANKNIFTY/SENSEX). Earlier: 2026-06-13
(DEC-PULL-1DAY-FIRST-001, MIGRATION.md #723 —
§1/§6 spot-interval facts: the spot pull granularity is now operator-chosen
per pull via the `/universe` dropdown with a VERIFY-FIRST **1-day-only**
default (`resolve_fetch_intervals`; 1-minute / both after the
OHLCV-vs-bhavcopy cross-check), no longer force-fetching 1-minute alongside
1-day). Earlier: 2026-06-13 (DEC-PULL-FLOOR-2020-001, MIGRATION.md #722 —
§3 locked scope range moved `2021-01-01 → today` → `2020-01-01 → today`:
the DATA pull floor (`GROWW_HISTORY_FLOOR`) lowered to the full
probe-verified Groww 1-minute depth, superseding the conservative
2021-01-01 floor of DEC-DHAN-PULL-SCOPE-001). Earlier: 2026-06-12
(DEC-CLAUDEMD-SPEC-LINE-RECONCILE-001,
MIGRATION.md #669 — renumbered from #667 by PR #629's duplicate-repair;
§2 Rule 4's run-identity citation refreshed
`TRACK1_BACKTEST_SPEC.md:654` → `:743`; spec growth had moved the
blake3 formula block and the stale line pointed mid-section). Earlier:
2026-06-10 (DEC-AUTOMERGE-CLEAN-STRICT-001, MIGRATION.md
#612 — §2 Rule 13(a) `mergeable_state` invariant aligned with the
charter §4.5 pre-G2 envelope per DEC-WORKSPACE-VERIFY-002 follow-up
F-4). Earlier: 2026-06-10 (DEC-WORKSPACE-VERIFY-002, MIGRATION.md #609 —
§6 prior-vendor `constants.py` line citations refreshed after the
5-agent workspace verification found 4 rows drifted by 7 lines; §1/§3
active branch refreshed). Earlier: 2026-06-03 (DEC-SHARED-CORE-001 Phase 2b,
MIGRATION.md
#441 — BruteX's 52-condition vocab now delegates to the shared
`rust/trading-core/` crate; §1/§5 reconciled). Earlier: 2026-06-01
(DEC-DASHBOARD-OMIT-AUDIT-001 — vendor docs reconciled Groww → the prior
vendor after the 5-agent workspace verification found §1/§6 stale).
DEC-P0-026
(PR #48) retired the tickvault co-development layer; DEC-SHARED-CORE-001
then re-established a single shared `trading-core` crate (indicators +
52-vocab) consumed by BruteX + TickVault. Vendor history: broker → Groww
migration (2026-05-15), Groww → prior-vendor pivot proposed then
reverted (ADR-0002, 2026-05-31 — the prior-vendor revert, now
superseded), then a full
re-migration back to Groww — **DEC-DHAN-GROWW-REMIGRATE-001 (2026-06-12,
ADR-0009) supersedes the revert and is authoritative: the live vendor is
Groww (the prior-vendor adapter package + reference docs were removed in
the ADR-0009 §5 cutover PR series).** Track 1 spec locked after 4-agent
hostile-review pass._

---

## 1. Project Identity

- **Name:** BruteX
- **Purpose:** Extreme drill-down brute-force backtesting engine for
  Indian **spot indexes** (NIFTY 50 / BANKNIFTY / SENSEX) + **India
  VIX** (volatility index). Track 1 is signal-only research; the
  Options Track translates winning signals to options for execution
  realism.
- **Data source (Track 1):** **Groww Trade API** via the `growwapi`
  Python SDK (injected `GrowwAPI` client; TOTP auth) per
  DEC-DHAN-GROWW-REMIGRATE-001 / ADR-0009 (which supersedes the
  ADR-0002 prior-vendor revert). One unified surface for
  spot AND options:
  1. `groww.get_historical_candles(...)` — fetch **`1minute` ONLY** for
     spot (DEC-1MINUTE-ONLY-DERIVE-1DAY-001, MIGRATION.md #853): 1-minute is
     the SINGLE granularity fetched from Groww, and `1day` (the PDH/PDL daily
     anchor) + the 19 intraday timeframes are **DERIVED deterministically from
     the stored 1-minute**, never separately vendor-fetched. The derived `1day`
     is resampled to midnight-IST daily bars (`brutex/data/spot_daily_resample.py`)
     and written to the lake's 1-day partition by the pull pipeline's automatic
     FINAL prebuild stage — no longer mid-pull (DEC-PIPELINE-AUTOPREBUILD-001,
     MIGRATION.md #1145; `_resample_daily_after_pull` removed). This supersedes
     the operator-chosen granularity dropdown of DEC-PULL-1DAY-FIRST-001 (#723,
     removed) and re-introduces the 1m→1d derivation removed by
     DEC-REALDATA-ONLY-NO-1M-TO-1D-001 (#852) — now as the always-on PRIMARY
     path, since 1-minute is the sole fetched granularity (the derived 1-day is
     openly labelled derived, not vendor-official; Golden Rule #1).

  Expired options + futures (Options Track / lake) use the same
  `get_historical_candles` after resolving contracts via
  `groww.get_expiries(...)` → `groww.get_contracts(...)`, also at **`1minute`
  ONLY** with a DEFAULT window SPLIT by contract type — the STRICT law
  `[expiry − lookback → expiry] ∩ envelope`: FUTURES `expiry −
  60 days → expiry` (DEC-FUTURES-LOOKBACK-60D-001, MIGRATION.md #1003 —
  supersedes the 3-calendar-month default of DEC-FNO-LOOKBACK-SPLIT-001 #894),
  OPTIONS `expiry − 30 days → expiry` (explicit operator dates
  override either default; a NOT-YET-OPEN far-future contract — `expiry −
  lookback` on/after the envelope end — is SKIPPED entirely, zero vendor
  calls + zero ledger rows, logged `fno.window_not_open`, until the calendar
  reaches `expiry − lookback`; never a substitute window and never the full
  2020→today range — DEC-FNO-FAR-EXPIRY-WINDOW-001, MIGRATION.md #1127);
  the F&O `1day` is likewise
  DERIVED from the pulled 1-minute (DEC-1MINUTE-ONLY-DERIVE-1DAY-001). See §6
  for the full verified fact table.
- **Out of scope (Track 1 engine):** Live feed, WebSocket, order
  placement, portfolio, funds, multi-user SaaS. (Per DEC-P0-063 the LAKE
  additionally ingests F&O single-stock spot+options as REFERENCE_ONLY;
  the BRUTE-FORCE ENGINE remains the 3 indexes — see §2 Rule 3.) The dashboard
  + lake **LIVE-feed SURFACE that once existed** (`brutex/live/`, the `/live`
  page + `/api/v1/live/*` routes, the `live_ticks`/`live_candles_1m` tables, the
  Movers "Live" tab + /db "Live lake" section) was **REMOVED in full —
  DEC-LIVE-REMOVE-001 (MIGRATION.md #862)** — bringing the codebase in line with
  this out-of-scope rule. (The `live` = real/production naming-trap survivors —
  `adapters/groww/live.py` `build_live_client`/`build_live_pool`,
  `pull_controller._build_live_controller` — are the HISTORICAL pull's client +
  rate controller, NOT the feed, and stay.)
- **Active branch:** `claude/verify-acceptance` (**DEC-OPERATOR-ACCEPTANCE-VERIFY-001, MIGRATION.md #1756** —
  the operator acceptance verifier `brutex verify-acceptance`: NEW ADDITIVE `brutex/acceptance/` package
  (model/report/context/sampling/independent/selftest/run + checks/{unique,tf,pull,rate,movers,sanity,latency})
  + a thin `# pragma: no cover` cli shim + ci.yml per-module 100% gates + 4 modules in the weekly mutmut loop.
  Three structural laws: one-verdict-one-evidence-block (empty evidence REFUSED at the model), no-verdict-without-work
  (`units_checked == 0` ⇒ forced ABSTAIN(nothing_to_check) in the constructor — the #935/#1014 dormant-feature trap
  closed), and teeth-before-trust (the always-on smoke plants 3 poisons in a real-writer twin lake and requires the
  COMPOSED checks to catch each; TEETH_FAILED ⇒ exit 2 with real-lake verdicts WITHHELD; the 20-poison full battery is
  CI-mandatory per merge with a comparator-INVERSION tooth). Compose-never-reimplement: the census fold, the
  sweep-preflight TF seam + the NEW 1day compare composition, the REAL `plan_windows`/`plan_contract_windows` re-plan
  on a pinned-clock copy (anti-vacuous planner tail tooth included; explicitly NOT `brutex fetch --dry-run`),
  disk-only rate evidence (constants pin · #1692 market-hours conformance · bridge point sample · rotating-log window
  · audit pair-bound trend under an ALWAYS-ABSTAIN verdict · the PERMANENT structural
  ABSTAIN(no_per_call_persistence)), the AST-FENCED `independent.py` movers leg (raw parquet + hand IST/#1601
  bucket-anchor/Fraction %CHNG/half-up-2dp/HLC3-paisa/hand calendar back-walk; fence = a red BUILD), the
  price/bad-tick/tick-grid/session/interior-gap/VIX-silent-empty/completeness sanity battery, and best-of-N-MIN
  latency probes vs on-box absolute budgets (never the CI 1.1x tier). Daily-rotating seeded sampling (engine set —
  3 indexes + INDIAVIX + the NSE-NIFTY-*-FUT engine-track contract dirs — ALWAYS in scope: unique.engine scans them
  full-history, tf force-includes the engine futures contracts; coverage{} REQUIRED per check; coverage ledger makes
  accumulation visible). Report:
  `<lake>/acceptance/<run_id>/report.json` schema-self-validated before writing + report.txt (ABSTAINs FIRST) +
  evidence spill + LATEST + the SweepAuditWriter hash chain verified by the EXISTING `brutex audit verify`; deltas +
  abstain streaks carried in the report chain. Exits 0/1/2/3/4 (2>3>4>0). HOSTILE-REVIEW ROUND 1 folded
  (same branch, pre-merge — 3-refuter panel, every CONFIRMED finding fixed with a fails-before regression;
  refuted claims got no code change): the §12.7 stat-fingerprint retry-once TOCTOU law is WIRED into
  unique/tf/pull/sanity (`checks.run_fingerprinted_check` — a mid-check real-writer append retries once then
  ABSTAINs `input_moved_during_check`, never a PASS over a torn read); tf.daily skips Muhurat sessions PER-DAY
  (counted `muhurat_days_skipped`; a multi-year engine lake keeps a real 1day verdict — the whole-history
  abstain/exit-4-forever knock-on is dead); the movers moneyness grammar admits the FULL production vocabulary
  incl. the honest `?`/`—`/bare-ITM/OTM sentinels; the pull replan threads production's expiry-capped
  `contract_windows` + re-plans per MERGED covered span (disjoint operator ranges never false-FAIL; all-expired
  lakes read `no_tail_eligible_series`) with the tail tooth reusing the ONE replan copy; `checks=()` is refused
  in the core; the decorative `pull.progress_snapshot` PASS is DELETED (counters = replan evidence); market-hours
  folds only `dry_run is False` journal records (heartbeats never count); instruments.duckdb is opened READ-ONLY
  only (older-schema stores survive byte-identical; RW-held/foreign ⇒ named ABSTAIN); oracle reads ride
  `glob=False`; report.txt repr()s vendor-influenceable cells; BRUTEX_LAKE_ROOT is run-scoped + restored;
  rotating-log/audit evidence reads are bounded 16 MiB tails with the full-chain verify DROPPED from the
  evidence-only sub; RecursionError bombs fold, MemoryError re-raises raw; `--months` genuinely bounds
  tf.daily/sanity and the census scans sampled coordinates × months (never the whole-tree glob); enumerations
  memoised per run; latency's first call relabeled `first_call_ms` (never claimed cold); the report golden
  regenerated as a reviewed act. HOSTILE-REVIEW ROUND 2 folded (same branch, pre-merge — 3-refuter panel,
  every CONFIRMED finding fixed with a fails-before regression): the coverage_gate_discovery BLOCKER killed
  (the 7 checks-package test files RENAMED `test_acceptance_check_*` → `test_acceptance_checks_*` per the
  discovery grammar + the ci.yml pairings repointed — never an exclude entry); the movers options leg probes
  registry.duckdb READ-ONLY first — a schema-version drift ABSTAINs `registry_schema_drift` BEFORE the
  route's write-capable `RegistryStore.open` can drop/restamp it (absent = proceed, empirically pinned the
  route creates nothing; matching = proceed, the no-net-bytes residual disclosed in limits+runbook); the
  replan oracle goes correct-or-abstain — the hard replan==0 law binds DEFAULT-law series only, while
  explicit-dates F&O rows / overlapping-anchor ranges / in-coverage re-tiling mismatches route to the named
  `non_default_window_shape` (both refuter false-FAIL repros dead; a key ESCAPING the covered spans still
  hard-FAILs — the double-fetch tooth pinned); the pull TOCTOU retry genuinely RE-READS the ledger
  (`on_retry=drop_ledger_memo` — the void `retried:True` stamp over a memoised snapshot is dead); Muhurat
  1day bars are counted ONCE per distinct IST date and disclosed UNVERIFIED (`muhurat_days_unverified` +
  the limits/runbook line — corruption on those ~6 dates is honestly out of scope); the instrument master is
  read as a short-lived in-memory SNAPSHOT (no held file handle — the in-process DuckDB same-file
  config-conflict that silently degraded the #1482/#1646 movers drift ladder is dead, leg A is the
  production wire again); the F&O contract sample seed-ROTATES (`sample_fno_contracts` — the fixed head-6
  is dead) and contracts join the coverage-ledger population (capped 5000, truncation disclosed); the
  coverage ledger read is bounded 16 MiB + pruned-on-save to the live population; NITs: symlinked
  `<lake>/acceptance` write refusal, BaseException-safe env restore in ctx.close, the vacuous-tail
  O(eligible×records) note, the moneyness grammar tightened to `[1-9]\d*` depths.
  HOSTILE-REVIEW ROUND 3 folded (same branch, pre-merge — every CONFIRMED finding fixed with a
  fails-before regression): the CLI dispatch runs the #1630 logging seam for `verify-acceptance`
  (unconfigured structlog's stdout PrintLogger buried the human table + broke `--json`; logs now
  STDERR, stdout = exactly the table/JSON — the round-2 test that sliced from `{` fixed to full-stdout
  parse); ONE evidence-tree symlink refusal HOISTED before ANY write (the audit JSONL no longer writes
  through a symlinked `<lake>/acceptance` before the report-side guard); the read-only headline
  discloses BOTH serving-path residuals (registry re-stamp + a possible `<lake>/.manifest/`
  discovery-cache refresh, exactly as production); the engine set is code-true (C-F1 — INDIAVIX +
  every `NSE-NIFTY-*-FUT` contract dir join unique.engine full-history, tf force-includes the engine
  futures contracts; `sampling.engine_futures_contracts` via the canonical `parse_fno_symbol`);
  `--out` reports are OFF-CHAIN (LATEST/deltas/streaks track only the default chain — the dangling
  pointer + silent streak reset are dead); a report-write failure restamps overall SUSPECT/exit-2 +
  one loud stderr line (the "→ exit 0" table on a 2-exit process is dead); `--sample < 0` refused at
  CLI AND core; report.txt renders every limits line + the routed non_default_window_shape count +
  the Muhurat-unverified dates (the ABSTAIN-prominence law extended); the open_by_design fold's
  future-dated-phantom scope + the never-planned-window merged-span twin are named in limits.
  HOSTILE-REVIEW ROUND 4 folded (same branch, pre-merge — every CONFIRMED finding fixed with a
  fails-before regression): the cross-process single-flight flock for the WHOLE run
  (`<lake>/.locks/acceptance_singleflight.lock`, the house `_flock_singleflight` shape — two
  CONCURRENT runs previously corrupted the audit hash chain PERMANENTLY; the second run now
  refuses SUSPECT exit 2 naming the holder); the `pull.partition_presence` half-open-span fix
  (the old `we − 1day` end fed `[d, d)` for a production 1-day window — a HEALTHY lake read
  exit 3, and battery poisons 6-8 rode the false positive: a net_new_law sabotage SURVIVED
  TEETH_PROVEN, measured — the fixed span makes those teeth real again, sabotage-flip
  battery-pinned); the symlink refusal extended to the fixed audit CHILD paths
  (`acceptance/audit` + the chain file — the `sym_inner` write-through is dead); TWO per-leg
  unique battery teeth on a non-engine CASH twin symbol (`census_nonengine_dup` +
  `engine_oldmonth_dup` — an always-PASS sabotage of either leg now flips the battery) + the
  movers comparator-seam teeth scope disclosed (the composed-wiring killer is the unit gate's
  `TestMoversRealWire::test_tamper_tooth_volume_cell`); the `streak_carry` report block (a
  subset/TEETH_FAILED run carries absent sub-checks' streaks forward VERBATIM — the measured
  3→1 streak reset that suppressed the exit-4 law is dead); the package-wide zero-vendor-call
  AST pin now EXISTS (sanctioned planning/constants imports only — the runbook's "AST-pinned"
  claim is mechanical); `--json` emits a minimal JSON error object on the early-SUSPECT paths;
  the pyright `base.update` annotation fix.
  447 tests; every module re-measured 100.00%
  line+branch on the ci.yml-exact gates; full battery 17/17 detected, 0 false FAILs on the clean twin;
  ruff/ruff-format clean; `mypy --strict` clean (the one pre-existing env-specific cma stub note aside, 0 new);
  pyright 0 errors on the new package (HONEST baseline correction, round-4 C-F1: the earlier
  "net −12 on the touched test files" compared against this branch's OWN round-1 interim state,
  not origin/main — every error in a new file is new; after the round-4 `base: dict[str, object]`
  annotation fix the FULL `pyright brutex tests` run measures 0 errors at this head under
  pyright 1.1.411, so the branch adds 0 vs origin/main — the round-4 refuter's 1-error main
  reading at test_window_ledger.py:990 did not reproduce at the fixed head, and that file is
  byte-untouched by this branch). ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate
  change; the per-item record lives in MIGRATION.md #1756.)
  Earlier: `claude/frontend-latency-hardening-r2` (**DEC-FRONTEND-LATENCY-HARDENING-001, MIGRATION.md #1720** —
  the dashboard FRONTEND latency-hardening pack, CLIENT-side only (templates + static JS + tests + docs; ZERO
  route/payload/backend change — byte-stable wire): (Fix 1) the v29 operator page's DUPLICATE pollers consolidate onto
  ONE BXPoll-disciplined consumer per endpoint — bx-oneclick.js's progressPoller fans out to
  `window.__bxSweepProgressSubs`/`__bxSweepProgressFailSubs` (call-time-read arrays, throwing subscribers isolated) +
  exposes `__bxSweepProgressPollNow`; the [data-sweep] panel, heartbeat sweep slot and Run-controls renderer all
  SUBSCRIBE (the Run click flips `watching` + one shared pollNow — progress-bar cadence 3 s, was 1.2 s, disclosed);
  bx-rate-monitor.js / bx-liveproof.js gain the same-shape `__bxPullRateSubs`/`__bxLiveproofSubs` fan-outs feeding the
  heartbeat pull/proof slots (the notify helper stays LOCAL to each dependency-free widget — a ledgered §11 deviation);
  the killed tokens (`setInterval(pollSweepProgress, 2000)` · `sweepPoll = setInterval(pollRunSweep, 1200)` ·
  `setInterval(pollPull, 5000)` · `setInterval(pollProof, 10000)` · `setInterval(pollState, 2000)`) are
  negative-pinned and /api/v1/state rides BXPoll at its unchanged 2 s. MEASURED BEFORE (coordinator harness, headless
  Chromium, origin/main @ a268bd4c, idle `/`, 120 s visible): 336 requests — sweep-progress 99 / pull-rate 63 /
  liveproof 36 = the summed duplicates this closes. MEASURED AFTER (same harness, head 31e9022c — validity
  scoped PER FILE: the five files the page-`/` measurement depends on — v29.html + bx-oneclick.js +
  bx-rate-monitor.js + bx-liveproof.js + bx-poll.js — are byte-identical to this branch's head, verified
  empty diff): 240 requests/120 s = 2.000 req/s (−96, −28.6%) — sweep-progress 99→39, pull/rate 63→39, liveproof
  36→24, state 60→60, universe/pull-progress 39→39, oneclick/runs 39→39; the same six endpoints on both runs.
  HONEST: hidden-tab was 0 req/s measured on BOTH SHAs — no hidden-tab
  win claimed; the pollRunSweep no-guard/no-backoff/un-timed-fetch gap is source-asserted (armed only mid-sweep,
  unmeasured). (Fix 2) the six fixed-interval template pollers (coverage · spot_coverage · fetch_windows ·
  completeness · expired_options 4 s + launch 2.5 s) migrate onto the ONE shared `BXPoll.createPoller` core through a
  custom-fetchJson tick seam — every render/degrade byte-identical, failed ticks throw into the counted 3 s→30 s
  backoff (coverage's 4-fetch composite backs off only at 0/4), cadence tokens unchanged (`pollMs: 4000`/2500),
  DOMContentLoaded start, a missing bx-poll.js degrades to the legacy guarded interval; fetch_windows' chip/pager
  clicks ride `poller.pollNow()` and its page-snap-back recursion returns the inner promise. (Fix 3) the 8 /screener
  filter inputs + /results' Top-N input debounce 200 ms (the v29/movers precedent). (Fix 5) completeness (renderSpot +
  the ~750-row renderOptions) + spot_coverage (renderSpot) adopt the fetch_windows/db patchGridRows idiom — ONE
  esc()'d row builder shared by full rebuild AND tbody patch; the 3 new tbody sinks are the sanctioned narrow-target
  pattern (`_SANCTIONED_EXTRA_SINKS` extended); MEASURED null result, framed honestly: the in-place tbody patch
  preserves table-node identity across re-renders (no scroll/selection loss, less GC churn — structural probe main
  false/false → branch true/true) while render WALL-TIME is UNCHANGED at 750 rows (renderSpot 70.1→69.3 ms /
  renderOptions 52.3→54.0 ms second-render medians, inside main's own ±4-9% run-to-run variance — NO ms improvement
  claimed). (Fix 6) /reports' chips build ONCE per symbol-SET key;
  All/Clear/F&O/index-pick are now an O(chips) `.on` class sweep (`applySelection`), XSS build path byte-preserved.
  SKIPPED honestly: Fix 4 defer (end-of-BODY includes, movers' genuine parse-time consumers, the standing
  test_dashboard_halfup_format non-defer pin — the skip itself now pinned deliberate) and Fix 7 (already shipped by
  DEC-DASHBOARD-POLL-HYGIENE-001 #1623). PINS UPDATED intent-preserved: poll_hygiene's five uniform-poller pins →
  the BXPoll mechanism; js_perf's cadence pin → `pollMs: 4000`; ux_design's v29 fetch-deadline pin → the
  BXPoll/subscription mechanism. TESTS: NEW tests/unit/test_dashboard_frontend_latency.py (node --check on EVERY
  static/*.js + killed-token/subscription/debounce/tbody/chips pins + a node-EXECUTED fan-out isolation golden);
  every touched template's inline scripts parse clean under node. HONEST envelope: source-string pins + node syntax
  gates, no in-CI browser measurement — BEFORE/AFTER measured out-of-CI by the coordinator harness (AFTER on
  31e9022c; the five measured-page files byte-identical to this head). HOSTILE-REVIEW FIX ROUND folded
  (pre-open, FIX-FIRST verdict — the per-item record lives in MIGRATION.md #1720): the fetch_windows/
  completeness tick fetches are now BOUNDED through the body read via a per-template `fetchTextBounded`
  honoring the seam's timeoutMs (the MAJOR — a headers-then-stalled-body tick would have wedged BXPoll's
  one-in-flight chain forever; the ledger's "bare un-timed fetches closed" claim was ahead of the code, now
  true + pinned); /reports' chip map keys symbol+exchange (the dual-listed stale-highlight MINOR); the
  touched templates' inline scripts gain an executable node --check pin; completeness.html joins the
  innerHTML-sink baseline guard at 7. ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change.)
  Earlier: `claude/db-latency-hardening` (**DEC-DB-LATENCY-HARDENING-001, MIGRATION.md #1719** —
  the DB/query-latency hardening pack, READ-side only, three evidence-driven fixes off the 2026-07-11
  profiling run (a 1,178-file real-writer lake, 400k-row ledger + 5 deltas, real routes via
  `build_app` + `TestClient`): **(1)** the `/db` window-ledger pager — NEW
  `db_view.query_window_ledger_page`, a DuckDB LIMIT/OFFSET projection over the SAME #1533
  delta-folded `_ledger_scan_source` the `/coverage/windows` fast paths scan, total from ONE
  stamp-memoised COUNT(*) (the NEW `wledger-*` key families on the injected window-payload caches);
  measured 7,321–8,778 ms cold at 400k rows → 564.8 ms (the 7,177 ms O(rows) Python record fold per
  stamp is GONE); a legacy-shape base / a NULL in a legacy-DERIVABLE cell / a persistent
  `duckdb.Error` (torn-read retry-once first) / `conn=None` each degrade to the byte-identical
  legacy cached parse (`window_ledger_rows_cached`, byte-untouched — the fenced #1533 cache
  families were NOT restructured); **(2)** the pre-open exports — both routes memoise
  `build_preopen_rows` on the NEW dedicated `_preopen_rows_cache` keyed
  `(basis, [from, to], symbols-filter)` at `_summary_cache.generation` (repeat click 326–574 ms /
  206 DuckDB statements binding 616 files → 4.2–18.6 ms / 0 statements, bytes identical; the `.zip`
  shares the `.xlsx`'s coordinate) + the IEP (open) readers gain an additive `span=` binding only
  the report span's month files via NEW `_existing_month_files_in_span` (±1-day pad law, domain
  saturation, non-grammar strays conservatively kept) through `_execute_parquet_resilient`'s NEW
  bounded `files=` arm — the CLOSE maps deliberately stay whole-partition (the #1649a contiguity
  guards' `found=nearest_lake_session` diagnostics + the calendar back-walk depth need the
  unbounded view); the #1649a pre-fix goldens pass UNMODIFIED; **(3)** the `/db` candles %CHNG
  baseline — `_intraday_last_close_with_predecessor`'s two reads bind explicit file lists, so a
  page click binds 2 baseline files instead of 72 (warm p50 30.7 → 21.1 ms; the residual is the
  #1614 manifest revalidation sweep, out of scope). FIX-3 MECHANISM SUPERSEDED at the 2026-07-11
  merge-reconcile (main 652301b5, PR #1386): main's DEC-DB-BASELINE-MANIFEST-ROUTE-001 (#1670)
  landed the SAME footer-open fix in-flight with a richer mechanism (`_bars_file_windows` —
  manifest-exact per-file bounds + month-grammar fallback + predecessor step-back, ALSO covering
  the moneyness spot join) — the merged tree serves #1670's mechanism, this pack's interim
  `_predecessor_candidate_files` helper was DELETED at the reconcile, and this pack's
  behavior-level pins + bench re-ran GREEN over it unchanged; the bounded-`files=` resilient-read
  arm CONVERGED with #1670/#1668's (main's kwarg + two-stage probe + readable-subset latch
  adopted); fixes 1 + 2 untouched, `_existing_month_files_in_span` stays (the fix-2 consumer). SANCTIONED (corrupt lakes
  only): a corrupt file provably OUTSIDE a bounded read's candidates no longer blanks that read —
  correct-or-absent preserved, blast radius strictly narrower; a corrupt CANDIDATE still blanks
  (both directions pinned). The D3 route spy tightened 1 → 0 Python parses (byte-parity half
  unmodified). HONEST: the window-ledger fresh-stamp click is ONE O(total rows + delta footers)
  columnar scan, never literal O(page); the export render stays O(report rows) per click; candidate
  enumeration is an O(existing files) listing. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no
  lake/schema/scope/vocab/rate change; no new required wire fields; `db_view.py` +
  `preopen_report.py` 100% line+branch on their ci.yml gates (two new unit files joined the db_view
  gate); ruff/ruff-format clean, `mypy --strict` clean, pyright at the pre-change baseline;
  fast-equals-legacy + bounded-equals-unbounded parity pinned exhaustively; benches
  `test_db_window_ledger_page_o1.py` + `test_db_candles_baseline_bound_o1.py`.)
  Earlier: `claude/movers-bucket-asof-fix` (**DEC-MOVERS-EXPLICIT-BUCKET-ASOF-001,
  MIGRATION.md #1601** — an explicit `/movers` bucket-anchor TIME pick now resolves to the
  COMPLETED bucket (broker-matching close) instead of a PARTIAL forming bucket. The TIME
  dropdown on a coarse timeframe offers only the 09:15-anchored bucket-ANCHOR grid, but the
  backend treated a picked anchor as an as-of INSTANT, so 3-minute @ 15:27 cut at 15:27 and
  showed the partial 15:27 minute close (₹27,932.80) instead of the completed 15:27–15:29
  bucket close (₹27,940.85 = the broker's = 2min@15:29 = 1min@latest). The stored derived-TF
  partitions were always CORRECT; only the read-time as-of resolution was wrong. FIX: ONE
  COMMON as-of seam `movers_snapshot.resolve_asof_cutoff(interval, time)` (consumed by all
  three tabs — spot/futures/options) resolves an anchor pick FORWARD to the bucket's final
  minute `min(anchor + minutes − 1, 15:29)`, and the downstream `<= cutoff`
  `filter_to_ist_minute` caps at the day's latest available minute (NO look-ahead). Laws:
  TF=1minute identity; "Latest"/blank whole-day byte-identical (DEC-MOVERS-ASOF-LATEST-DEFAULT-001
  preserved); non-anchor picks keep as-of-instant semantics; prev-close %CHNG untouched. Honest
  provenance: additive `asof_through` payload echo + " HH:MM bucket · through HH:MM" note
  (default `None` → byte-identical). Value TF-invariance (DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001)
  now holds PER shared *resolved* as-of (a sanctioned narrowing). READ-ONLY dashboard — ENGINE
  UNCHANGED (NIFTY/BANKNIFTY/SENSEX); no lake/schema/scope/vocab/rate change; the three touched
  movers modules 100% line+branch; ruff/ruff-format/`mypy --strict` clean, pyright at baseline).
  Earlier: `claude/movers-futures-hyphen-parse` (**DEC-MOVERS-FUTURES-HYPHEN-PARSE-001, MIGRATION.md
  #1646** — the `/movers` Futures contract-symbol parse is RIGHT-anchored via the ONE canonical grammar:
  `parse_future_symbol` DELEGATES to `data/fno_symbol.parse_fno_symbol` (FUT-restricted; the left-anchored local twin
  DELETED), so a hyphenated underlying (`NSE-BAJAJ-AUTO-30Sep25-FUT`) parses to `BAJAJ-AUTO` — not the `parts[1]`
  truncation `BAJAJ` whose nonexistent-spot-key probe silently dropped spot + premium for every hyphenated F&O
  underlying and let the warm grid cache the wrong rows (executed real-lake repro). Disclosed sanctioned delta:
  junk lake-dir expiry tokens the shape-stricter twin rejected (`30SEP25` etc.) now accept exactly as
  options//db/Greeks already did; well-formed vendor symbols + non-hyphenated rows byte-identical (pinned);
  `&`-names were and remain correct (pinned). SAME-PR FOLD-IN (W3 spot-drift ladder): the movers underlying-spot
  probe gains the #1482 (DEC-GREEKS-SPOTJOIN-STOCK-001) drift-recovery ladder — NEW `movers_futures._SpotLtpResolver`,
  ONE per-candidates-compute instance in BOTH the futures loop and the options moneyness loop: the exact-key
  `_underlying_spot_ltp` probe (byte-unchanged) runs FIRST; only an exact MISS walks the COMMON
  `lake_keys.spot_recovery_candidates(..., stock_drift=)` ladder over the same two spot segments, fed by a LAZY
  at-most-once `spot_drift.load_stock_drift_map` (master-verified unique-fold map; ambiguity POISONED + capped
  warns; no-master / read error fail-softs to today's honest no-spot; MemoryError re-raises), with the recovery
  OUTCOME memoised per (exchange, underlying) + ONE rung-attributed `movers.spot_key_recovered` INFO per compute —
  so a spelling-drifted stock spot dir (chain `BAJAJAUTO` vs the master's on-disk `BAJAJ-AUTO`; executed repro on
  the REAL computes) no longer blanks futures spot/premium or reads a silent `?` options moneyness. A resolving
  lake never builds the map (spy-pinned byte-identity, index symbols untouched); NO `%26`→`&` recovery (the #1482
  pledge stands); read-time recovery ONLY — nothing healed on disk. READ-ONLY dashboard — ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX;
  `movers_futures.py` + `movers_options.py` BOTH 100% line+branch; ruff/ruff-format/`mypy --strict`/pyright clean.)
  Earlier: `claude/groww-market-hours-guard` (**DEC-GROWW-MARKET-HOURS-GUARD-001, MIGRATION.md #1692** —
  renumbered #1633 → #1673 → #1692: #1633 → #1673 at the 2026-07-10 merge-reconcile (main's slot-wave/renumber
  cascade reserved #1633–#1649 — see the DEC-MOVERS-UNDERLYING-CLASS-001 #1650 renumber history — and the in-flight
  cascade holds the band through #1672), then #1673 → #1692 per the coordinator reservation map (#1673 is reserved
  for PR #1369) —
  the NSE market-hours pull guard: NO Groww VENDOR pulls between 08:45 (inclusive) and 15:35 (exclusive) IST on a
  trading day; an in-flight nightly pull STOPS gracefully at 08:45 and start/resume is REFUSED until 15:35, keeping
  the SHARED Groww account free for TickVault's live feed. NEW pure leaf `brutex/calendars/market_hours.py`
  (`BLOCK_START=08:45`/`BLOCK_END=15:35`; the clockless `pull_allowed_now`; `pull_allowed_default` binding `now_ist()`
  + the NON-raising `is_trading_day()` — out-of-coverage weekday BLOCKS = fail-safe, weekend ALLOWED; the fail-LOUD
  env kill switch `BRUTEX_MARKET_HOURS_GUARD` default ENABLED; `guard_active_default`; a `now`-injectable
  `MarketHoursGuard`) + NEW `MarketHoursHalt(BruteXError)` in `data/errors.py` (in BOTH spines' `_GLOBAL_STOP_ERRORS`
  + `pull_jobs.is_pull_halt` → fires the CRITICAL Telegram pull-halt alert). START GATES: `BackgroundPullJob.start`
  defers a `JOB_KIND_PULL` in-window UNLESS `market_hours_exempt=True` — the THREE vendor-free exemptions are the
  sweep (`job_kind != PULL`), the dashboard Greeks-backfill start + every dashboard dry-run pull start (both pass
  `market_hours_exempt`; the over-block fix — Greeks-backfill runs under `JOB_KIND_PULL`, so job_kind alone was not
  enough); `cmd_fetch` defers a LIVE pull at the START gate with a clean EXIT_OK (dry-run/inspect EXEMPT). MID-RUN
  (the CRITICAL requirement): a `market_closed` predicate threaded to the PER-WINDOW seam of both
  spines raises `MarketHoursHalt` (durable ledger leaves PLANNED/FAILED windows RESUMABLE); `run_until_clean` gains a
  `market_closed` hook → `stop_market_hours`; `pull_all_spot`/`pull_contract_candles` catch it mid-round + belt-break
  later steps + the minute phase; the METADATA phase (`pull_expiries_contracts`) gets a per-cell belt (raises
  `MarketHoursHalt` before each `get_expiries`/`get_contracts`; `run_two_phase` re-raises it, not record-and-resume);
  `run_full_pipeline` re-checks the belt BEFORE metadata AND before historical + wraps `run_metadata` in a
  `try/except MarketHoursHalt`, skipping the rest of the vendor chain with `stopped_after="market_hours"`; and the
  CLI `brutex fetch` path is MID-RUN-guarded too (`cmd_fetch` threads `market_closed` into the spot spine + the
  between-rounds `run_until_clean`, catches `MarketHoursHalt`, drains + persists the ledger → resumable, exits
  EXIT_OK). The mid-run env kill switch is resolved ONCE at pull start via `resolve_market_closed()` (fail-LOUD on
  junk at START; the bound predicate reads ONLY the live clock per call, so a junk env written MID-pull can never
  crash a long run with a per-window re-parse); the one-shot START gates keep `guard_active_default`. Production
  wiring binds `resolve_market_closed()` (dashboard routes + the nightly headless binding — the MOTIVATING
  01:30 case covered); a dry-run / un-wired caller passes `None` (byte-identical). NOT edited:
  `_start_auto_resume_on_boot`/`cmd_dashboard` (auto-resume covered transitively; conflict-free with PR #1343).
  HONEST: the CLI + metadata paths are now mid-run-guarded (not merely start-gated); the Groww F&O guard is
  per-CONTRACT granularity — a contract already started at 08:45 finishes its few small remaining windows before the
  next contract's belt stops the pull (an accepted bounded overrun, never a whole extra symbol); a pull interrupted
  at 08:45 DEFERS to the NEXT nightly, NOT a same-day 15:35 re-arm (named follow-up); out-of-coverage default is
  BLOCK-in-window (fail-safe). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; pull-CONTROL only, no fetch/rate/pool/
  governor change; `market_hours.py` + `data/errors.py` + `universe_phases.py` + `metadata_pull.py` +
  `pipeline_pull.py` + nightly binding 100% line+branch; ruff/ruff-format clean, `mypy --strict`
  clean, pyright at the pre-change baseline. LAND-PASS FIX ROUND (2026-07-10, same branch, pre-merge): the async
  `fetch_pipeline._is_pool_isolatable` now treats `MarketHoursHalt` as a POOL-WIDE trip (the #1038 drift-pin's demand — the
  async twin of the spines' `_GLOBAL_STOP_ERRORS`; dedicated unit pin, module re-verified 100%); suite-wide WALL-CLOCK
  hermeticity — `tests/conftest.py` runs the suite under the documented kill switch (`BRUTEX_MARKET_HOURS_GUARD=0`,
  `setdefault`) because the real-clock START gate made every un-injected `BackgroundPullJob.start` test FAIL inside
  08:45-15:35 IST (reproduced 2026-07-10; the #1380 hermeticity philosophy applied to the clock — the guard's own tests
  inject env/clock/guards explicitly and stay armed on both arms); two stale `#1624` docstring citations corrected to
  this entry's number (now #1692 after the coordinator-map renumber); the fold's duplicated #1632 ledger entry deduped).
  Earlier: `claude/magical-cray-g6agaw` (**DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 +
  DEC-FUTURES-ADDRESSING-PIN-001 + DEC-STAGE1A-ROLL-RESET-001, MIGRATION.md #1700–#1702** — the ADR-0012
  NIFTY-futures sweep LAUNCH capstone: the ONE pinned series alias `NSE-NIFTY-FUT` (symbol_id 4; retired shapes get a
  did-you-mean refusal), the vendor-truth roll resolver (`brutex/data/futures_series.py` — interior holes refuse
  naming months, head/tail clamp+WARN, calendar observe-only), the ONE alias-aware sweep-slice loader
  (`brutex/feed/series_load.py` — exact IST-date ownership cut, spot path byte-identical), the Stage-1A B2 closure
  (roll-aware `session_key` + TOTAL per-contract hard reset + the D12 `apply_1day_mask`), and `roll_schedule` in
  `canonical_trade_params` under the instrument⟺schedule invariant. ENGINE surface = 3 spot indices +
  NSE/FNO/NSE-NIFTY-FUT; spot byte-identical (unmodified goldens green); mypy `--strict` clean, pyright at the
  pre-change baseline, ruff/ruff-format clean).
  Earlier: `claude/oneclick-perf-club` (**DEC-ONECLICK-PERF-CLUB-001,
  MIGRATION.md #1711** — the entry number is coordinator-ASSIGNED from the
  reservation map (2026-07-10 — the next free number above the #1264
  reservation tail #1710; live-main max #1665 at assignment, live-max+1
  spoken for by in-flight reservations — gaps are legal; renumbered from
  the interim provisional #1666, the #1664/#1665 reservation-map
  lesson) — the queued
  post-#1373 oneclick perf/hygiene club, built on PR #1373's
  `claude/oneclick-instrument-display` head 3c478831 (the PR opens only after
  #1373 merges; BUILD+TEST+PUSH-branch-only until then). **F1 (TOP):**
  `GET /api/v1/oneclick/verify` was the ONE oneclick route with NO revision
  cache — `load_manifest` + `reconcile_funnel` + the one-row trades probe ran
  per REQUEST; the whole payload is now stat-token cached like the siblings
  (key = manifest token [primary probe] + events + top100 tokens + the chain
  verified-state — a `check=chain` click re-keys exactly once, then plain polls
  serve the clicked payload; a primed poll is three `os.stat` probes + the
  raising chain cache-peek + one hit (r2 sweep M1 — the chain state is a
  cache-peek, never a stat); HONEST: a sidecar rewrite moving none of
  the three tokens does not move the key — the #1617 pass lands sidecars
  BEFORE top100.json, so the trades-wired flag refreshes with the top100
  token; a manifest-less run computes fresh per poll, cheaply). NEW B-7 bench
  pin (`TestVerifyPollFlatVsManifestSize`, measured 2026-07-10 sandbox:
  26.5 µs/poll @ 4 doors vs 25.2 µs @ 4000 doors → 0.95× flat vs the 6.89 ms
  fresh compute the pre-fix route paid per poll; `ratio_ceiling(8.0)` + the
  `_must_not_run` no-recompute + outage-serve teeth). **C-r2-5 (same root):**
  the stat-OUTAGE doctrine — `artifact_probe`/`ArtifactProbe` distinguish
  honest ABSENCE (FileNotFoundError/NotADirectoryError → fresh UNCACHED, cheap,
  last-known key FORGOTTEN so a deleted artifact is never resurrected) from a
  probe OUTAGE (any other OSError → serve the LAST-KNOWN payload via the ONE
  `serve_revision_cached` chokepoint + `OutageGate` [thread-safe, both maps
  LRU-bounded at OUTAGE_GATE_CAPACITY=512 — the events coordinate folds
  client-typed page params, so unbounded dicts would be a hostile-poll leak]
  with ONE `oneclick.probe_outage` WARN per outage per COORDINATE (r2
  MINOR-2 honesty — a whole-volume flap warns once per distinct coordinate
  touching the volume, a verify poll probes 3: primary + events + top100
  siblings; never per poll) and a
  `oneclick.probe_recovered` INFO on recovery — the pre-fix rev-None arm was
  an UNCACHED full O(artifact) re-parse per poll with ZERO logs, Breaker-C
  measured ~450×); ALL FIVE cached oneclick routes (runs · manifest · top100 ·
  events · verify) rewired through the chokepoint; the peek rides the NEW
  `CachedPayloadMissingError`/`raise_no_cached_payload` sentinel over the NEW
  `PayloadCache` Protocol (leaf module never imports the dashboard cache);
  `runs_revision_probe` is the outage-aware twin of `runs_revision` over ONE
  extracted `_runs_digest` (§11 COMMON, byte-identical). **F4:**
  `discover_runs` stat-stamps every dir, selects the newest-limit on
  `(mtime_ns, name)` (IDENTICAL selection/order to the pre-fix sort), and
  parses ONLY the selected manifests — the pre-fix shape parsed EVERY run's
  manifest then truncated (67× under churn); the stat sweep stays O(run dirs)
  and `runs_revision`'s uncapped stat walk stands (the F5 ruling — this bounds
  PARSES, not stats); `TestDiscoverRunsParseBound` pins 50-of-60 parses via a
  counting spy. **A-r4 (client defense-in-depth):** the /screener "Show all"
  render is BOUNDED (BX_SCREENER_SHOW_ALL_CAP=1000) and applyFilters/sortRows
  are input-bounded (BX_SCREENER_INPUT_CAP=5000, applied at payload ingestion
  via `bxInputCapRows`) through node-goldened BXScreener primitives
  (`bxRenderCount`/`bxCapNote`/`bxInputCapNote` — every truncation NAMED on
  the count line: "showing first N of M (render cap 1000)"); the backend half
  was CLOSED by #1373's TOP100_ROW_CAP=100 (>100-row inputs unreachable
  TODAY) — this guards FUTURE bypass paths against the Playwright-measured
  10k=4.8 s / 20k=14.1 s tab freeze; the [W2-5] rendered-numerator law stands
  (route pin repointed onto the shared primitives). **F2b SKIPPED** (optional
  polish per the Breaker-A severity correction — the row cap was the real
  fix; do-not-over-invest ruling, ledgered). TESTS/GATES:
  `oneclick_artifacts.py` 100.00% line+branch on the ci.yml-EXACT standalone
  gate (probe/gate/chokepoint covered by the unit file alone);
  `oneclick_view.py` untouched; the oneclick battery + route
  outage/recovery regressions green; ruff/ruff-format clean; `mypy --strict`
  clean; pyright 0 errors on touched files; `node --check` clean. DISPLAY
  zone only — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX); zero new routes; the
  verify wire shape byte-identical (caching + logging only); the only
  payload-visible change class is the /screener count-line notes + the
  bounded Show-all render.)
  Earlier: `claude/oneclick-instrument-display` (**DEC-ONECLICK-WINNER-DETAIL-TRUTH-001 +
  DEC-ONECLICK-INSTRUMENT-DISPLAY-001, MIGRATION.md #1664–#1665** — renumbered from
  #1632/#1633→#1633/#1634→#1664/#1665 across the 2026-07-09 + 2026-07-10 main-folds (main took #1632 with
  DEC-DASHBOARD-VISUAL-OVERHAUL-001, PR #1354; #1633–#1636 were #1323's reserved block; #1664/#1665
  coordinator-assigned from the in-flight reservation map) — the
  strategy-detail honesty pack,
  viewer-reads-storage only, built on PR #1352's `claude/oneclick-top100-object-root` head 31a7cf0a (ONE PR, opens
  after #1352 lands). #1665 (the umbrella): the gross relabel — every signal-only P&L label on the /strategy story
  card + the operator hero/kpi/Returns/verdict-narrative reads "Gross signal P&L" with the persistent
  "signal-only — costs modeled in the options-translation track" chip (killed-token pins on the bare "net P&L"
  labels; combos gross/charges/net split untouched — net == gross while costs are structurally absent, noted);
  the SHIPPED results-depth aggregates render in the NEW /strategy Deep-stats section
  (expectancy_paisa · recovery_factor · max_consecutive_losses · mae_avg/worst · mfe_avg/best ·
  max_dd_minute_path_paisa/pct · exits mix · worst5_days — a METRICS key per the audit refinement, NOT regime)
  via SIX pure node-goldened bx-oneclick builders (`bxExitMixText`/`bxWorst5DaysText`/`bxTrendSplitText`/
  `bxRegimeWonderText`/`bxAvgExtremeText`/`bxMinutePathDdText`; money via an INJECTED house formatter; honest "—"
  degrades, a malformed worst5 entry abstains the whole line) under the NEW block-level `jargon-block`/
  `plain-eng-block` Beginner/Pro twins + 11 new BX_EXPLAIN hover lines; the regime breakdown
  (one_regime_wonder + regime_wonder_axis + by_trend) renders on the /strategy card AND the operator card
  (`r1_trend_split`/`r1_regime_wonder`/`r1_worst5`), the stale "lands with the P3 data" deferral DELETED
  (negative-pinned; family drill-down R34 stays a named later PR); the equity SERIES sidecar (`entry.equity_ref` →
  `<stem>.equity.parquet`, previously served by NO route) gains `GET /api/v1/oneclick/equity` through the typed
  seam — the `TradesReader` Protocol grows `equity_page` on all three readers (Awaiting/Fixture → honest awaiting,
  never a synthesized curve; `TradeStoreReader` resolves rank → `equity_ref.tuple` via the ONE refactored
  `_resolve_sidecars` chain shared with trades), the display-zone `_load_equity_records` parquet read is honestly
  O(equity-file) on the FIRST read per (revision, combo) — the stamp indexes no equity spans — with a single-entry
  memo making repeat pages O(page), limit clamps [1, 5000] NAMED (one row/session ≈ 250/yr), a no-1m session's
  null `equity_intraday_low_paisa` passes through, polars-missing/unreadable files read the NAMED corrupt (never a
  500), and `build_equity_payload` DELEGATES to the ONE page projector (§11 COMMON); the /strategy equity host
  consumes the route — the stored series renders labeled "minute-path equity — the stored real series (R27)" via
  NEW `bx-chart.js::bxMinutePathSeries` (same series shape as the cumsum builder — ONE `equityPathNode` renders
  either; invalid points skipped + counted) with the per-trade close cumsum RELABELED "per-trade close equity
  (minute-path unavailable for this run)" as the honest fallback (the OPERATOR equity host deliberately stays
  per-trade — the #48 owner ruling is the named follow-up); the `/strategy?run_id=<id>&rank=<n>` deep-link
  contract is verified already-honored and CONTRACT-PINNED (`test_strategy_link_contract_run_id_rank_params` —
  the display-truth session builds the /results link against it); nav.py `NAV_LINKS` gains
  /completeness · /screener · /strategy (the served-but-unreachable class; all 12 guarded template navs updated,
  aria-current on each page's own link) and the nav consistency guard enforces the 10-link canon; the two
  round-2 finder NITs land in `bxCandleRowsFromDbPayload` — NEW `bxBarPrice` (a null/blank/boolean/non-finite
  OHLC SKIPS the row; `Number(null)→0` never plots a fabricated 0-price candle — the trap itself pinned) + the
  `skipped` counter surfaced via `bxSkippedRowsNote` ("N row(s) skipped (unreadable)") on the /strategy coords
  line. HIGH #40 (composite_score persist/join for /results) stays DEFERRED — a results-storage seam under
  coordinator brokering; the score is already on the one-click wire. TESTS: the writer→reader golden extended
  (equity route off REAL `write_tuple_sidecars` bytes — awaiting-before/ok-after + the honest null low on the
  wire; the link-contract pin), `TestEquityPage` (12) + `TestBuildEquityPayload` keep BOTH oneclick modules
  100.00% line+branch on the ci.yml-exact gates, `TestEquityRoute` route degrades (400/422 bounds), NEW
  `tests/unit/test_dashboard_winner_detail.py` node goldens + wiring pins. #1664 (folded first): the winner's
  `instrument` on every surface — ONE pure `bxInstrumentText` builder feeding the operator rank-1 card
  (`r1_instrument` hooks), the combos Inst cell (hardcoded "—" + the FALSE "instrument is not a top100 row field"
  comment KILLED; the dead `fInst` filter's v29-preview display names repointed to the ENGINE tokens
  NSE-NIFTY/NSE-BANKNIFTY/BSE-SENSEX), the /strategy identity strip (`stInstr`), and /screener (config-driven
  `instrument` column labeled "Inst" after the strategy name + the direction-style exact-match filter with the
  documented absent-key law — options harvested ONLY from rows carrying the key, so a pre-instrument artifact's
  dropdown stays empty); the audit's metric-key concern VERIFIED covered by #1631 with zero residue (every metrics
  consumer flows through the ONE `/api/v1/oneclick/top100` route → `load_top100` → `TOP100_METRIC_KEY_MAP`;
  proof = the existing writer→reader golden + wire-type assertions + the node blast-radius pin — no duplicate
  work). HONEST: DOM wiring pinned by source strings, not executed (the #1557 envelope); the fixture corpus is
  byte-untouched; the screener instrument filter is exact-match, not a normalizing join. DISPLAY zone + one
  additive display route — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX); engine writers byte-untouched;
  `oneclick_artifacts.py` + `oneclick_view.py` 100% line+branch standalone; ruff/ruff-format clean, `mypy
  --strict` clean, pyright 0 new; `node --check` clean on the four touched JS files.)
  Earlier: `claude/preopen-prevday-common` (**DEC-PREOPEN-PREVDAY-COMMON-002, MIGRATION.md #1649a** — RENAMED from -001 at the 2026-07-10 merge-reconcile: main's #1634 (PR #1323) claimed -001 for the minute-basis calendar-cross-check ADVANCE while this branch was in flight; -002 delivers #1634's named OPEN work (the observed-overlay COMMON-ization + thin delegator) + the hole-shape WARN observability + the #1649b Muhurat coherence pack — renumbered
  #1623→#1631→#1649a across the 2026-07-09 merge-reconciles (main took #1623 for DEC-DASHBOARD-POLL-HYGIENE-001 (PR #1335),
  then #1631/#1632 for DEC-ONECLICK-TOP100-OBJECT-ROOT-001 (PR #1352) + DEC-DASHBOARD-VISUAL-OVERHAUL-001; the coordinator
  slot is #1649, and the 613a/613b both-suffixed pair convention — a bare number may not coexist with suffixed forms —
  keeps this entry + its same-branch amendment adjacent as #1649a/#1649b); the
  /reports preopen MINUTE-basis calendar-contiguity fix + the #1539-named follow-up consolidation: the minute-basis
  PREV. CLOSE guard (`_minute_prev_close_contiguous`) anchored on `trading_dates[idx-1]` — the symbol's OWN 1-day
  series — with ZERO real-NSE-calendar check (its docstring claimed a session absent from BOTH series is "a genuine
  holiday"; the confirmed repro: a lake holding 2024-01-02 + 2024-01-04 in BOTH series with the REAL Wednesday session
  2024-01-03 absent from BOTH emitted prev_close=100.0 → a FABRICATED pct_chng=+10.0 that `select_top_movers` would
  have crowned, while the DAY basis on the identical lake skipped). THE FIX: the minute guard is now the exact
  structural mirror of `_daily_prev_close_contiguous` — the checked calendar (`_previous_trading_day`) is the
  prior-session AUTHORITY; the anchor is valid ONLY when the nearest earlier 1-day date IS the calendar's prior
  session AND that session has minute data; refusal = honest row ABSENCE riding the existing skip `continue`
  ("correct-or-absent, never wrong" — never a fabricated %CHNG, never a raise/500). Both guards gain the
  Ellipsis-sentinel `expected_session` seam + the `on_hole(expected, nearest_lake_session)` observability seam;
  `build_preopen_rows` threads the observed overlay into BOTH bases through ONE function-local O(unique days)
  prior-session memo (spy-pinned one-walk-per-day; NOT an O(1)-total claim) and surfaces holes as
  `preopen.prev_close_noncontiguous` WARNs (deduped per (exchange, segment, symbol) dataset triple — the
  hostile-review fold widened the bare-symbol key that silenced a dual-listed symbol's second exchange — cap
  20/build, ONE `preopen.prev_close_noncontiguous_suppressed` summary) carrying a machine `hole_shape`
  + shape-matched detail (the review fold on the original unconditional "re-pull" advice, WRONG in two reproduced
  shapes; FOUR-WAY since the DEC-PREVSESSION-MUHURAT-COHERENCE-001 amendment fold, MIGRATION.md #1649b):
  muhurat_session_bar (the calendar-confirmed Diwali boundary — a HEALTHY lake, "no action needed", cross-referencing
  the #1616 engine anchor law + /movers' prev_close_muhurat display stamp) / extra_nonsession_bar (a non-Muhurat
  unexpected non-session bar — honestly suspicious, inspect it; the old "e.g. a Diwali Muhurat session" guess is
  gone) / prebuild_lag (1day lags minute —
  run the pipeline prebuild; DIAGNOSTIC only, the guard never anchors on minute presence — a partial-day close
  would be a fabrication) / missing_session (genuine hole — re-pull kept verbatim); the daily basis carries no
  minute evidence, so only its found<expected diagnostic degrades; the golden generator now REFUSES to overwrite existing
  fixtures without BRUTEX_REGEN_PREOPEN_GOLDEN=1 (the pre-fix capture stays the oracle). THE SAME #1649b AMENDMENT
  FOLD (DEC-PREVSESSION-MUHURAT-COHERENCE-001, this branch, the recorded TWO-LAW design — engine anchor law #1616
  vs display baseline law): /movers stops false-flagging the day-after-Diwali boundary as a lake hole — a
  found-after-expected Muhurat baseline reads contiguous-True + the EMIT-ON-TRUE `prev_close_muhurat`/
  `oi_baseline_muhurat` keys + " †" + at most ONE `movers.prev_baseline_muhurat` INFO per build (never the false
  EMIT-ON-FALSE pair/" *"/WARN — the SANCTIONED wire delta, pinned before/after; every non-Muhurat row
  byte-identical; the 39-key warm grid fires ZERO noncontiguous WARNs on a healthy Muhurat lake), and /db gains the
  static "%CHNG anchors on the nearest stored prior bar (including a Diwali Muhurat session's close)" footnote
  (per-row marker = named follow-up DEC-DB-MUHURAT-PREVCLOSE-MARK-001; the operator-gated
  DEC-PREOPEN-MUHURAT-SERVE-001 stays behind a bhavcopy evidence gate). CONSOLIDATION (the #1535/#1539-named follow-up CLOSED):
  `nse.previous_trading_session` gains kw-only `observed: AbstractSet[date] = frozenset()` — REMOVE-ONLY
  (`is_trading_day_checked(probe) and probe not in observed`; plain data, no overlay-loader import into nse.py, no
  cycle; default-empty parity pinned unit + hypothesis so every existing caller is byte-identical) — and
  `preopen_report._previous_trading_day` becomes a THIN delegator over it (name+signature KEPT, private walk DELETED;
  delegation-equivalence pinned against a verbatim in-test copy of the old walk over every day 2020-01-02→2026-12-31
  × observed sets; the module-local `calendar.coverage_exhausted` event RETIRED for the COMMON
  `calendar.previous_session_coverage_exhausted` — a sanctioned, grep-verified consumer-free observability rename,
  its one test pin reconciled). SANCTIONED (each pinned): minute-basis hole rows DISAPPEAR from exports (remediation
  shape-matched: re-pull / prebuild / none); coverage-abstain + Muhurat-bar / unflagged-drift anchor days now SKIP on
  the minute basis (parity with
  daily; an overlay-flagged day is recovered — the dedicated spurious-skip pin). HONEST: the pure-minute no-1day
  fallback arm stays deliberately calendar-blind (a CHOSEN, pinned, ledgered limit). EVIDENCE: the finder's repro
  KILLED on the fixed tree ("row SKIPPED (no fabrication) … VERDICT: NOT CONFIRMED"); PRE-fix-captured goldens
  (contiguous weekend + Holi-2024-cluster + Budget-Saturday fixture, day+minute .xlsx + .zip) byte-equal post-fix;
  the `!=`→`==` expected-check mutant proven dead on BOTH guards; the fixture audit of test_preopen_report.py +
  test_surface_trial_reports.py found ZERO non-real-session dates. READ-ONLY dashboard — ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; no lake/schema/scope/vocab/rate change; `preopen_report.py` (307/90 pre-fold; 323/98
  post-REVIEW-ROUND-1) + `calendars/nse.py`
  (204/52) BOTH 100.00% line+branch on their static gates; ruff/ruff-format clean; `mypy --strict` clean (403 files);
  pyright 0 errors at the pre-change baseline, +2 test-file `reportPrivateUsage` warnings — the house private-import
  test pattern — plus 2 more for the round-1 `_hole_shape`/`_hole_detail` private-helper imports.)
  Earlier: `claude/cool-cerf-tq8c27` (**DEC-LAKE-HEAL-OPTIONS-FOREIGN-001, MIGRATION.md #1594** — the
  automatic lake healer COMPLETED for the `options/<series>/<YYYY>-<MM>.parquet` expired-options table
  (`OptionParquetWriter`, dedup key `(side, timestamp)`): FOREIGN-MONTH-ROW + OVERLAPPING-PARTITION detection+heal on
  the SAME proven machinery — quarantine-COPY of original bytes BEFORE any rewrite → rewrite THROUGH the writer's
  normal locked path (`heal_partition(remove_keys=…)`, RAW stored-value keyed removal — naive-column immune, KEYED
  never positional; in-process + cross-process partition locks reused; `lake_write_guard` so ENOSPC/OOM →
  `LakeWriteHalt`) → hash-chained audit JSONL (`AUDIT_HEAL_REASSIGN`, `tree="options"`) → ONE Telegram alert per
  batch. Keep-first everywhere: a foreign `(side,ts)` key already present in its canonical month file is REMOVED
  (canonical wins, drifted values never restated — preserved in quarantine); an absent key is REASSIGNED home through
  `merge_partition_frame` (keep-first at the destination); copy-forward FIRST, remove SECOND (crash-convergent,
  twin-lake pinned). 5-class heal order drift → foreign → dup → option-foreign → option-dup; observe short-circuit,
  mass-heal rail (option files in the scanned-file DENOMINATOR), `LakeWriteHalt` re-raise, per-finding isolation all
  inherited. Scanner: `OptionForeignMonthRows` + pure `classify_option_keys` (timestamp-only routing law — side is
  key PAYLOAD so a null side rides verbatim and the scanner count can never diverge from the healable set; null ts
  abstains); overlap via memoised canonical-sibling reads — ONE per affected month per series (a multi-month stray
  costs one read per home month, never a literal single read); clean option file pays ZERO extra reads;
  `months=` bounds the options tree BY FILENAME; census OPTIONS row + `brutex lake audit-unique` + `/universe` chip
  carry REAL foreign/overlap numbers (drift stays `-`, honestly N/A). Wiring honesty: L1 reconcile-piggyback heal is
  STRUCTURALLY N/A for `options/` (the reconcile walks the CATALOG; the options tree is uncatalogued by design); L2
  `heal_after_pull(include_options=True)` wired at the F&O post-pull seam ONLY (bounded, months-scoped by FILENAME —
  a CLEAN out-of-scope file is never read; HONEST round-2 NIT: a FLAGGED in-scope file still costs targeted
  canonical-sibling reads + heal writes in its foreign rows' HOME months, possibly outside the span; no current
  production pull WRITES `options/` — opportunistic census of a legacy table); L3 startup
  sweep already scanned `include_options=True` so the new findings ride automatically (sentinel gate G9-pinned);
  recall stays `None` for options findings (NOTHING derives from `options/`; bars recall machinery reused as-is in
  mixed batches — no new recall invented). STRICTLY row-removing/row-reassigning — never value-editing.
  HOSTILE-REVIEW FOLDS (round 1, refuter-confirmed; every fix pre-fix/mutant-verified): (1) BLOCKER —
  `merge_partition_frame` baselined `added` on RAW physical destination rows, so a dup-POISONED canonical (an
  `OptionIntraFileDup` state this healer exists to fix) collapsed inside the keep-first union and CANCELLED a
  genuinely-new moved key: `added==0` skipped the rewrite while the source removal still ran — the reassigned key
  existed in NEITHER lake file and a second sweep was CLEAN (silent permanent loss, empirically reproduced pre-fix).
  FIXED on the bars twin's law (DEC-LAKE-WRITE-UNIQUE-001): `_merge_frame_locked` baselines `added` on the UNIQUE
  stored `(side, timestamp)` count and FORCE-rewrites a physically-dup-poisoned destination even at `added==0`
  (self-heal on touch), emitting the SAME pinned `lake.partition.dedup_healed` event (kept/dropped — closing the
  unobservable-collapse MINOR too; sanctioned no-quarantine merge-touch self-heal, the bars precedent); production
  `write()` byte-unchanged (writer-written partitions carry no dups; G10 parity green). (2) MAJOR —
  `_option_file_month` gated on `str.isdigit()` then called `int()` unguarded; `'²²'.isdigit()` is True but
  `int('²²')` raises, so ONE crafted `options/` filename crashed `scan_lake_invariants` pre-read — 500-ing the
  `/universe` lake-uniqueness route, leaving the L3 boot sweep permanently inert (ALL healing off) and discarding
  L2 batches. FIXED: both `int()` calls guarded (the bars `_file_month` twin) AND only the writer's canonical
  zero-padded `f"{y:04d}-{m:02d}"` stem parses — a parsable-but-non-canonical `2024-6` would claim the SAME month
  as its canonical twin file (the hand-planted twin-file blind spot NIT) and now abstains to dup-only. (3)
  MAJOR/MINOR test gaps — the multi-destination-month reassignment loop + the per-month overlap grouping loop were
  never driven with >1 month (truncation mutants `[:1]` survived the suite = silent multi-month loss/undercount);
  NEW two-destination-month heal pins (options AND the bars twin — the same pre-existing bars gap) + the
  overlap-across-two-canonical-months scan pin, ALL three mutants applied + verified KILLED. NITs: symlinked series
  dirs are SKIPPED loudly (`lake.invariant.symlink_skipped` — the heal primitives unlink/rmdir/create INSIDE a
  series dir, so a followed symlink would mutate files OUTSIDE the lake root); the foreign-routing predicate is
  ONE `is_foreign_timestamp` (§11 COMMON, 4 inline copies killed); the new options events carry `series=repr(…)`
  (the #907 raw-name convention). HOSTILE-REVIEW FOLDS (round 2, refuter-confirmed; all 6 regressions verified
  failing pre-fix): (R2-1·MAJOR) the round-1 scan-time symlink skip was the ONLY control and was BYPASSABLE — both
  vectors empirically reproduced mutating files OUTSIDE the lake root from the default AUTO-ON sweep: (V1) a
  symlinked `options/` ROOT (`is_dir()` follows it; the series dirs inside are REAL so the per-series probe never
  fired) and (V2) a scan→heal TOCTOU (the probe is scan-time only; the heal re-derives dest + re-reads/unlinks src
  with no re-check), with NO containment check at the writer mutation boundary. FIXED with two rails: containment
  ENFORCED at the mutation boundary — NEW `OptionParquetWriter._assert_options_containment(dest)` under the locks
  inside BOTH heal-side entries (`heal_partition` before any read/unlink/rewrite; `merge_partition_frame` via
  `_merge_frame_locked(containment=True)`) refusing with the quarantinable `OptionWriterError` unless
  `dest.resolve().parent` sits under `lake_root.resolve()/options` (the `options` component deliberately NOT
  symlink-resolved — resolving it would re-anchor the containment root to the attacker's target; a lake ROOT reached
  via a symlink still PASSES, pinned no-false-positive; refusal = isolated counted error, batch continues, an
  errored L3 batch skips the sentinel so the next boot re-attempts) — PLUS the options-ROOT symlink is skipped
  loudly at scan (same `lake.invariant.symlink_skipped` event). The production `write()` path stays deliberately
  unguarded (`_safe_segment` already rejects traversal; a deliberately-symlinked options tree keeps a working pull —
  the HEALER refuses instead). HONEST: closes the demonstrated scan→heal window; a sub-lock race against the atomic
  rename inside ONE call would need dirfd-relative I/O — out of scope, NOT claimed. (R2-2·NITs) the L2
  "zero-reads-out-of-scope" overclaim + the untrue "healer's own dup pass quarantines" comment + the
  `_merge_partition` "byte-for-byte" docstring all reworded honestly; `is_foreign_timestamp` added to
  `lake_invariants.__all__`; the option-foreign month split rewritten O(months × rows) → O(rows) dict-group (the
  bars twin's shape). HOSTILE-REVIEW FOLDS (round 3, refuter-confirmed; every fix reverted-mutant-verified):
  (R3-1·MAJOR) a source file truncated to ZERO rows (schema preserved) in the scan→heal window crashed the WHOLE
  heal batch: `_option_foreign_frame` built its row mask as a Python list, `mask == []` made `pl.Series([])` dtype
  `Null`, and polars `filter` raised `TypeError` — NOT in `_PER_FINDING_HEAL_ERRORS` and not `LakeWriteHalt`, so it
  escaped the per-finding isolation loop (remaining findings unhealed, no HealReport/stats/alert/AUDIT_HEAL_BATCH
  line), violating the module's own "stale finding → empty-foreign skip" envelope (the bars twin `_heal_foreign` is
  dict-based and immune — options-specific). FIXED: the mask Series is explicitly `dtype=pl.Boolean`, so the stale
  finding takes the documented `foreign.height == 0` SKIP (`test_option_foreign_stale_zero_row_file_skipped` —
  truncate-after-scan, asserts skipped=1/errors=0/no exception/read-only bytes; fails against the reverted mask).
  (R3-2·MINOR surviving mutant) NOTHING pinned the L2 option census's `include_bars=False` — an independent probe
  flipping it to `True` survived all 122 healer+seam tests while rescanning the WHOLE bars tree UNFILTERED from the
  F&O seam (beyond the per-target-coordinates envelope, duplicating in-span bars findings + double-counting bars
  files into the mass-rail denominator, diluting the >50% refusal rail). PINNED twice: a census call-shape spy
  (`include_bars is False`, `include_options is True`, NO coordinate kwargs) + an in-span NON-target bars poison in
  the heals-in-span test that must stay unfound/unhealed/uncounted (`files_scanned` stays 2) — the mutant re-applied
  post-fix and verified KILLED by both. (R3-3·MINORs, docs honesty) the F&O seam docstring
  (`historical_pull._heal_lake_after_pull`) still carried a THIRD verbatim copy of the "zero reads out of scope"
  claim R2-2 had already retracted as overbroad — reworded to the `heal_after_pull` honest wording (clean
  out-of-scope never read; a FLAGGED in-scope file costs sibling reads + auto-mode reassignment writes in its
  foreign rows' HOME months, possibly outside the span); this blurb's "ONE memoised per-series canonical-sibling
  read" singular was literally false for a multi-month stray — corrected to ONE read per affected month per series
  (the ledger already said it right). HOSTILE-REVIEW FOLDS (round 4, refuter-confirmed; the fix pre-fix-verified, the
  NIT mutant re-applied + verified killed): (R4-1·MINOR) the options scanner followed a SYMLINKED PARTITION FILE out
  of the lake — the round-1/round-2 probes checked only the `options/` ROOT + the series DIRS, never the
  `<YYYY-MM>.parquet` itself, and `read_partition_frame` had NO containment check (the round-2 rail guarded only the
  WRITE/unlink boundaries) — so a linked file was scanned, classified `OptionForeignMonthRows`, and healed: the
  consistent read followed the link OUTSIDE the lake and the copy-forward leg IMPORTED the outside rows into an
  in-lake canonical file (that dest PASSED containment — its parent is a REAL in-lake series dir) while the
  source-removal leg was refused — probe-reproduced (outside `close=666.0` created in-lake; outside file
  byte-identical; three consecutive sweeps each findings=1 healed=0 errors=1 — permanently non-converging, the L3
  sentinel never stamps). Impact bounded (nothing outside the lake mutated; planting the symlink needs lake-write
  access which equally permits planting a real parquet) — hence MINOR. FIXED at BOTH ends: the scan loop mirrors the
  dir probes with a per-file `is_symlink()` skip (same loud `lake.invariant.symlink_skipped` event, counted
  `option_files_skipped` — never scanned ⇒ never healed) + defense-in-depth, `read_partition_frame` runs
  `_assert_options_containment` under its locks (the source READ joins the two mutation legs under the rail; refusal
  = the same isolated counted `OptionWriterError`). Regressions (all 3 verified FAILING pre-fix):
  `test_symlinked_partition_file_skipped` (scanner) + `TestMutationContainment.test_read_refuses_symlinked_partition_file`
  (writer) + `TestOptionSymlinkContainment.test_symlinked_source_file_read_refused_never_imports_outside_rows` (the
  probe verbatim as a file-level scan→heal TOCTOU — one isolated error, nothing imported, outside untouched, no
  quarantine). (R4-2·NITs) the `_count_foreign_rows` "(O(1) fast path)" shorthand reworded (two VECTORIZED O(rows)
  min/max scans — the win is skipping the O(rows) PYTHON loop, never O(1)); the round-3 singular-sibling-read honesty
  fix propagated to the two remaining in-module `lake_invariants.py` docstring copies (module header +
  `scan_lake_invariants`); the reassignment months `sorted(rows_by_month)` was pinned only by fixture-order luck (the
  F3 test plants May before July — a `sorted→list` mutant SURVIVED it): NEW
  `test_option_reassign_months_sorted_not_insertion_order` plants July physically FIRST and pins the ascending
  audit/log `months` list (mutant re-applied + verified KILLED while the F3 test still passed under it); the ledger
  #1594 F5 clause's new-log-event miscount corrected in place. HOSTILE-REVIEW FOLDS (round 5, refuter-confirmed; the
  MAJOR regressions verified FAILING pre-fix, all three pause mutants applied + verified KILLED, every NIT regression
  verified failing against its reverted fix): (R5-1·MAJOR) a symlink-LOOP `dest` made `_assert_options_containment`'s
  `dest.resolve()` raise a bare `RuntimeError("Symlink loop from …")` on Python ≤3.12 — NOT in
  `_PER_FINDING_HEAL_ERRORS` and not `LakeWriteHalt`, so it ESCAPED the per-finding isolation loop and aborted the
  WHOLE heal batch (no HealReport, no stats bump, no on_report/Telegram alert, later findings unhealed — the same
  isolation-escape class the round-3 zero-row fold eliminated). NO TOCTOU needed: the scanner symlink-skips only the
  flagged SOURCE file, never the reassignment TARGET, so a pre-planted loop as the foreign rows' canonical home-month
  file crashed the default AUTO-ON heal from the L2 F&O seam + the L3 startup sweep (reproduced end-to-end, Python
  3.12.3); DoS-of-healing only — the crash preceded any mutation. FIXED at the one chokepoint: both `resolve()` calls
  guarded `(OSError, RuntimeError)` → the documented quarantinable `OptionWriterError`, covering all THREE heal-side
  entries; regressions pin the writer refusal (nothing created) + the end-to-end batch continuation (errors=1, a
  later same-batch dup still heals, on_report fires). (R5-2·MINOR surviving mutants) BOTH `per_file_pause` arms in
  `_canonical_option_keys` were UNPINNED — deleting either passed every covering suite despite the scan docstring's
  "pause after every read incl. the targeted canonical read" promise (the only pause test used a clean fixture with
  zero canonical reads — unthrottled L3 canonical-read bursts would ship silently); pinned by two tick-count tests
  (success arm: bars+options foreign fixture, ticks == scanned + 2 canonical reads; error arm: garbage sibling —
  the bars twin `_canonical_timestamps` rides the same pin), all THREE mutants re-applied + verified killed.
  (R5-3·NITs, each reverted-fix-verified): the L2 months-scope bound (pure filename parse) now runs BEFORE the
  per-file symlink probe — an out-of-scope symlinked option file is silently scope-skipped, no longer warned+counted
  (in-scope + unbounded-L3 behavior unchanged); the targeted canonical-sibling overlap read now takes the SAME
  symlink skip as the main glob loop (warned loudly, spy-pinned never read, `overlap_rows=None` honest abstain);
  `heal_findings` serials a same-second quarantine dir (`<ts>-NN`) so a second batch under the second-granular stamp
  can never overwrite the first batch's forensic before-image (pre-existing DEC-LAKE-HEAL-001 machinery; honest:
  a same-second cross-PROCESS exists()-then-use race remains theoretically possible, not claimed closed).
  HOSTILE-REVIEW FOLDS (round 6, refuter-confirmed; the containment regressions verified FAILING pre-fix —
  cross-series contamination reproduced healed=1/errors=0; the census mutant re-applied at BOTH lines + verified
  KILLED): (R6-1·MINOR) the round-2/4 rail refused only a dest whose RESOLVED parent ESCAPES `<lake_root>/options/`
  — an IN-LAKE symlink swap passed it: post-scan, the flagged SERIES_A/2024-01.parquet swapped to a symlink at
  `options/SERIES_B/2024-03.parquet` (a DIFFERENT series, still inside the lake) let the heal read SERIES_B's rows
  through the link and the copy-forward leg CREATE SERIES_A/2024-03.parquet permanently holding SERIES_B's rows
  (cross-series contamination; SERIES_B itself intact, nothing escaped the lake — hence MINOR). FIXED at the one
  chokepoint: `_assert_options_containment` ALSO refuses when the dest or its series-dir parent `is_symlink()`
  (the healer only ever targets REAL partition files in REAL series dirs — ANY symlink at either component is a
  swap; ancestors above the series dir deliberately unprobed so the symlinked-lake-ROOT mount shape keeps passing,
  pinned, and an in-lake-redirected `options` component already fails the escape check); covers all THREE
  heal-side entries; the round-2 "closes the demonstrated scan→heal window" claim now covers the in-lake vector
  too (residual: a sub-check race inside ONE call needs dirfd-relative I/O — out of scope, NOT claimed). 3
  regressions (the writer file+dir in-lake twins + the end-to-end scan→swap→heal: errors=1 isolated, healed=0,
  no SERIES_A/2024-03.parquet created, SERIES_B byte-identical, the symlink left in place), ALL verified failing
  pre-fix. (R6-2·MINOR surviving mutant) the census `overlap_rows` None-guards — the options fold
  `(finding.overlap_rows or 0) > 0` + the pre-existing bars twin — survived dropping the `or 0` across the whole
  covering set (every census fixture fed only 0/1) while `overlap_rows=None` is a REACHABLE production state
  (unreadable/symlinked canonical sibling → the honest abstain) and the census backs `brutex lake audit-unique` +
  the doctor check + the `/universe` chip (one unreadable sibling would have TypeError-crashed all three under the
  mutant); pinned by two garbage-canonical census-fold tests (foreign_files=1, overlap_files=0, no raise — options
  + bars), the mutant re-applied at both lines + verified KILLED by each pin. (R6-3·NITs) the ledger #1594
  `TestOptionForeignHeal` count corrected 13 → 17; the stale §3 "Active feature branch" line reconciled to this
  §1 line (pre-existing drift). HOSTILE-REVIEW FOLDS (round 7, refuter-confirmed; BOTH mutants verified surviving
  the FULL covering set pre-pin and KILLED post-pin — each mutant applied against the new suite fails EXACTLY the
  one new pin, 321 sibling tests green): (R7-1·MINOR surviving mutant) `_option_foreign_frame`'s `keep="first"` —
  the "first physical copy of a duplicated foreign key wins" law its own docstring claims — had NO teeth:
  `keep="last"` passed everything because no fixture planted a physically DUPLICATED foreign `(side, ts)` key
  with DIVERGENT values in one source file (G7's dup rides the HOME key, collapsed by `heal_partition`'s own
  `unique()`, never by this frame), so the mutant silently flipped WHICH physical copy's OHLCV is reassigned home
  (nondeterministic-winner drift — the round-3 `include_bars=False` surviving-mutant class). PINNED:
  `test_option_foreign_duplicated_key_first_physical_copy_wins` — a June source carrying the SAME July `(CE, ts)`
  key TWICE (closes 111.0 then 222.0): July must read `close == [111.0]` (FIRST copy, moved ONCE), source cleaned,
  the ONE quarantine copy byte-identical holding ALL THREE physical rows, second sweep clean. (R7-2·MINOR
  surviving mutant) the deliberate "production `write()` stays UNGUARDED — an operator who symlinks the options
  tree to another volume on purpose keeps a working pull (the healer refuses instead)" stance had ZERO
  accept-side teeth: flipping `_merge_frame_locked`'s `containment` default to `True` passed every
  writer/sink/healer/seam test (`TestMutationContainment` pinned every REFUSAL arm, nothing pinned the ACCEPT
  side — a "harden everything" refactor would have silently turned that layout into per-contract
  `OptionWriterError` quarantines of the whole options pull). PINNED:
  `test_write_through_symlinked_options_root_keeps_working_pull` — `<lake>/options` symlinked to an outside real
  dir; `write()` lands the FRESH render AND the MERGE (dest-exists read-modify-write) arm THROUGH the symlink at
  its target, no refusal, the layout preserved. (R7-3·NIT, log-injection) the RAW attacker-influenceable series
  dir name (`_safe_segment` rejects only `/`, `\`, `..` — control chars ride through) was embedded UNESCAPED in
  the structlog `partition=` field of all four option-tree events (the `lake_heal.partition_healed` +
  `lake_heal.rows_reassigned` option arms; the two `lake.invariant.option_*` scanner events) while the sibling
  `series=` field already rode `repr()` — the LOG `partition` fields now ride `repr()` too (all four
  test-pinned); the `HealAction.partition` / audit-subject copies deliberately stay RAW (JSON-encoded machine
  surfaces, byte-pinned) and the bars `partition` fields stay raw (catalogued `PartitionCoords.relpath()`
  coordinates, not vendor-derived dir names; the round-7 "the shared `lake.partition.dedup_healed` writer event
  keeps its bars-twin `str(dest)` shape" exclusion is SUPERSEDED by round 8 for the OPTION writer's emission only).
  ROUND 8 (refuter-confirmed MINOR + NITs; all 4 regressions verified failing pre-fix): (R8-1·MINOR, log-injection
  completeness) round 7 guarded the option FINDING events but left the SIBLING skip events embedding the SAME
  vendor-influenceable series dir name un-repr'd — the five option-tree `lake.invariant.symlink_skipped` /
  `lake.invariant.scan_skip` sites (series dir · partition file · the targeted canonical-sibling read, symlink AND
  unreadable arms) now log `path=repr(str(…))`, and the option writer's `lake.partition.dedup_healed` `partition=`
  joins the convention (`repr(str(dest))` — its dest embeds the series dir, `_safe_segment` strips no control
  chars; event name + field names unchanged). Deliberately still RAW + pinned: the `<lake_root>/options` ROOT skip
  (operator-configured root + fixed literal, no vendor component), the BARS writer's `dedup_healed` twin
  (catalogued-coordinate dest) and the bars-tree skips (validated `parse_partition_path` coordinates). En-route
  honesty note [REFUTED + SUPERSEDED by round 15: only an UNBALANCED bracket raised "invalid glob pattern"
  (scan-skipped); a BALANCED set — and `*`/`?` — silently glob-read the WRONG files; every raw-path option read now
  passes `glob=False`, so any metachar name reads its literal file]: a `[`-carrying series name made `pl.read_parquet`
  glob-interpret the path — the round-8 fixtures use bracket-free ESC names. (R8-2·NIT, census cross-contamination)
  `build_lake_census` keyed the option findings into the SHARED per-segment dicts under the `"OPTIONS"` sentinel
  label, so a BARS finding whose `coords.segment` read `"OPTIONS"` (`PartitionCoords` is unvalidated; census input
  is ANY `LakeScanReport`, even though `parse_partition_path` rejects that segment at today's scan boundary) merged
  into the OPTIONS row — inflating dup/foreign/overlap and driving `clean` NEGATIVE (1 − 3 = −2 pre-fix); the
  OPTIONS accumulators are now TYPE-keyed (`isinstance`, dedicated sets), a rogue-segment bars finding lands in NO
  segment row while staying counted in `findings_total`. ROUND 9 (refuter-confirmed MINOR + NIT; the regression
  verified failing pre-fix): (R9-1·MINOR, log-injection completeness) the healer's per-finding error handler
  `lake_heal.partition_error` — precisely the attacker-TRIGGERABLE log site (it fires on the round-2
  `OptionWriterError` containment refusal, a corrupt planted partition, or a `PartitionLockTimeout`) — still logged
  `partition=_finding_token(finding)` RAW, and `_finding_token` returned bare `str(finding.path)` for the option
  findings, whose path embeds the SAME vendor-influenceable `options/<series>/` dir name rounds 7-8 repr()-guarded
  everywhere else (`_safe_segment` strips no control chars — a newline-named series dir forged a fake line in a
  rendered structlog stream); `_finding_token` now returns `repr(str(finding.path))` for
  `OptionForeignMonthRows`/`OptionIntraFileDup`, closing BOTH consumer sites in one place (the `partition_error`
  `partition=` field AND the observe-mode `lake_heal.observe_only` `detail=` list); bars tokens stay RAW
  (catalogued coordinates), the `LegacyKeyDrift` token keeps its pinned raw `exchange/segment/drifted_symbol`
  shape, and the sibling `error=str(exc)` field keeps the repo-wide accepted raw-exception-message convention (per
  the finding). Regression: a REAL-writer-seeded series keyed with an embedded newline + bare ESC (bracket-free —
  `[` is a polars glob metachar, the round-8 honesty note), foreign-poisoned raw, corrupted AFTER the scan — the
  `partition_error` `partition=` AND the observe `detail=` both pinned to the repr shape with no raw
  `\n`/`\x1b`. (R9-2·NIT, docs honesty) the `log_lake_finding` docstring claimed BOTH option events carry a
  repr()'d `series` field — the dup event emits NO `series` field (only its `partition` token is repr()'d);
  reworded to the emitted truth. HOSTILE-REVIEW FOLDS (rounds 10-12, REBUILT 2026-07-08 — an environment rollback
  lost the original round-10/11/12 commits; every defect RE-REPRODUCED at the rolled-back HEAD before re-fixing,
  every mutant re-applied + verified killed): (R10·MINORs, test-only surviving-mutant pins) the reassignment
  `HealAction.dropped` (= `dropped_duplicates + dropped_removed` — the exact value `emit_lake_heal_alert` sums into
  the operator alert) had NO dup-component teeth (every covering fixture healed dup-free sources, so
  `dropped=result.dropped_removed` and the dups-only/zeroed variants survived — the alert under-reports dropped
  rows); pinned by ONE source carrying a duplicated HOME key + a reassignable foreign row + an overlap row →
  `dropped == 3` EXACTLY plus the sum-over-actions aggregation; AND the census OPTIONS row's
  `files=option_files_scanned` flipped to scanned+skipped survived (skip fixtures never asserted `files`) — pinned
  scanned-only (one scanned-clean + one skipped-garbage file → `files == 1`, `clean == 1`; a skipped file inflates
  neither, on `brutex lake audit-unique`/doctor/the `/universe` chip). (R11·MAJOR, DoS-of-healing)
  `lake_content_token` `stat()`-ed every `*.parquet` under `bars/` + `options/`, so ONE dangling symlink — or a file
  deleted between the glob and its stat — raised `FileNotFoundError` before the scan even started: the guarded
  `StartupSweeper.run_sync` degraded to `lake_heal.sweep_failed` on EVERY boot and the L3 sweep went PERMANENTLY
  INERT (loud, never healing; reproduced end-to-end); the walk now `lstat()`s each entry (never follows — a dangling
  link contributes its OWN link metadata deterministically; the scanner symlink-skips such files anyway) under a
  per-entry `OSError` guard skipping a raced-away entry LOUDLY (`lake_heal.token_stat_skipped`, repr()'d path — an
  options entry embeds the vendor series dir name), the token deterministic for identical lake content; regressions:
  dangling links in BOTH trees → token returned + the sweep heals a sibling poison; a monkeypatched mid-walk
  `FileNotFoundError` → loud skip, token EQUAL to the genuinely-absent-file token. (R12·MAJOR, isolation-escape) a
  hand-planted options file whose `side` dtype is `List(Utf8)`/`Struct` read back UNHASHABLE values, so the
  `(side, timestamp)` key-set builds (`_option_foreign_keys` · `_canonical_option_keys` · the healer's removal-key
  `frozenset`) raised `TypeError` — neither a `parquet_read_skip_errors` member nor in `_PER_FINDING_HEAL_ERRORS` —
  crashing the WHOLE scan (ALL healing inert) and, planted in the scan→heal window, aborting the WHOLE heal batch
  (the round-3/round-5 isolation-escape class; both reproduced); NEW public `option_key_dtype_error` (in `__all__`,
  §11 COMMON — the `is_foreign_timestamp` precedent) validates BOTH key columns BEFORE classification (`side`
  String/Null; `timestamp` Datetime — naive routes as-if-UTC — or Null), wired at the scan loop + the targeted
  canonical-sibling read (loud `lake.invariant.scan_skip`, repr()'d path, counted `option_files_skipped`, never
  classified) AND at `_heal_option_foreign` (isolated `OptionWriterError` refusal — batch continues,
  report/stats/alert still emitted); 6 regressions + the gate-removal mutant verified killed. HOSTILE-REVIEW FOLDS
  (round 13, refuter-confirmed MAJOR + MINOR — ONE root, both regressions verified failing pre-fix with the exact
  `TypeError: unhashable type: 'list'` at the removal mask): (R13·MAJOR, isolation-escape) the round-12 gate covered
  the healer's CONSISTENT read but NOT `OptionParquetWriter.heal_partition`'s OWN re-read — the healer's locks are
  deliberately SEQUENTIAL, so the per-month copy-forward merge legs run BETWEEN the gated read and the re-read (a
  WIDER swap window than round 12 closed), and the keyed-removal mask hashed the re-read stored values UNGATED: an
  in-window swap of the flagged source to a `List(Utf8)`/`Struct`-side parquet (a regular in-lake file — passes
  polars unique/sort + every containment rail) raised bare `TypeError` — NOT in `_PER_FINDING_HEAL_ERRORS` — and
  aborted the WHOLE heal batch (no HealReport/stats/on_report alert/AUDIT_HEAL_BATCH line, the later same-batch dup
  unhealed; the round-3/5/12 isolation-escape class, reproduced end-to-end, contradicting the round-12
  "batch continues" claim). FIXED at the one hashing site: when `remove_keys` is non-empty the writer runs the SAME
  `option_key_dtype_error` gate on its OWN re-read (function-local import — `lake_invariants` imports nothing from
  the writer, no cycle) → the documented isolated `OptionWriterError`; reachable ONLY via the foreign-heal path (the
  dup heal passes `remove_keys=frozenset()` so its Python hashing never runs; production `write()` byte-unchanged).
  Regressions: the writer-level nested-dtype `remove_keys` refusal (byte-untouched, no quarantine copy) + the
  end-to-end mid-heal swap via a `merge_partition_frame` spy rewriting the source BEFORE delegating (errors=1
  isolated, the later same-batch CALL-series dup still heals, on_report fires, swapped source byte-untouched).
  (R13·NITs) `_option_file_month`'s "only the writer's canonical stem parses" claim was falsifiable for a
  5+-digit/year-0 stem (`f"{12024:04d}" == "12024"` round-trips the canonical check yet no datetime month can route
  there); the year is now bounded to `datetime.MINYEAR..MAXYEAR` (1..9999), 5 new stem-matrix rows incl. both
  boundaries verified failing pre-fix; the census-OPTIONS-`files`-with-skips NIT was found ALREADY pinned by the
  round-10 `test_census_options_files_count_scanned_only` (the `+ option_files_skipped` mutant re-applied 2026-07-08
  + verified killed by it — no change needed, reported honestly). HOSTILE-REVIEW FOLDS (round 14, refuter-confirmed
  MAJOR — the regression verified failing pre-fix with the exact bare `KeyError: 'side'`): (R14·MAJOR,
  isolation-escape) the round-12 gate was NOT TOTAL over its input shapes — `option_key_dtype_error` indexed
  `frame.schema["side"]` / `["timestamp"]` directly, a bare `KeyError` on an ABSENT column (in neither
  `_PER_FINDING_HEAL_ERRORS` nor `LakeWriteHalt`), and the healer's consistent-read site (`_heal_option_foreign`)
  is the ONE consumer fed a FULL `read_partition_frame` read with NO column selection — so a scan→heal swap of the
  flagged source to a valid parquet MISSING `side` or `timestamp` (e.g. a bars-style timestamp-only file) escaped
  the per-finding isolation loop and aborted the WHOLE heal batch (no HealReport/stats/on_report alert/
  AUDIT_HEAL_BATCH line, later same-batch findings unhealed, the L3 sentinel unstamped; reproduced end-to-end — the
  round-3/5/12/13 isolation-escape class the round-12 gate itself INTRODUCED for this shape: pre-gate the first
  key-column touch was polars `unique(subset=…)`, whose `ColumnNotFoundError` IS a `PolarsError` = the documented
  isolated refusal). FIXED at the ONE COMMON chokepoint: the gate now `schema.get()`s both key columns and returns
  an "option side/timestamp column missing — unclassifiable" verdict, converting the swap into the documented
  isolated `OptionWriterError` at BOTH consumer sites at once (the healer's consistent read AND the writer's
  round-13 `remove_keys` re-read); every OTHER call site is verified safe — the scanner + `_canonical_option_keys`
  read via `_read_key_columns(path, UNIQUE_KEY_OPTIONS)` (missing column → `PolarsError` → loud scan skip, so no
  finding is ever produced for such a file — reaching the healer REQUIRES the post-scan swap) and the writer's gate
  runs only after its own `unique(subset=…)` already succeeded. Regressions: the pure-gate
  missing-side / missing-timestamp / neither-column verdicts (never a raise, side checked first — deterministic)
  + the parametrized end-to-end side-less AND timestamp-less swaps (errors=1 isolated, the later same-batch
  CALL-series dup still heals, on_report fires, the verdict names the missing column, swapped source
  byte-untouched, canonical sibling untouched, no quarantine dir). (R14·NIT) the ledger #1594
  `TestOptionForeignHeal` count corrected AGAIN per the round-6/rebuild precedent (said 21; the class carried 22
  pre-round-14 — the round-13 mid-heal test was added without bumping the count — now 23 with the new swap pin).
  HOSTILE-REVIEW FOLDS (round 15, refuter-confirmed 2 MAJORs — ONE root; both empirically reproduced pre-fix through
  the real writer/scanner on polars 1.42.1, all 9 new regressions verified FAILING pre-fix): (R15·MAJORs,
  glob-metachar series names) `pl.read_parquet`'s default `glob=True` treats the PATH as a glob and `_safe_segment`
  legally admits glob metachars in a series dir name (rejects only `/ \ ..`, maps `|`) — TWO raw-path read sites rode
  the default: (a) `_merge_frame_locked`'s existing-destination read: a merge into `options/X[12]/2024-07.parquet`
  with REAL siblings `X1`/`X2` glob-matched the SIBLINGS as "existing", so the rewrite PERMANENTLY DROPPED the
  literal partition's own stored row (silent row LOSS — no quarantine, the merge leg never quarantines), IMPORTED the
  siblings' rows cross-series into the flagged series, and falsely emitted `lake.partition.dedup_healed` against them
  — firing from the default AUTO-ON healer's copy-forward leg AND the production `write()` merge arm with NO
  symlink/TOCTOU (static persistent state the round-2/4/6 containment rail cannot see — no symlink, the path sits
  literally inside `options/`), violating the round-1 no-silent-row-loss law + the round-6 "cross-series
  contamination closed" claim; (b) the shared scanner seam `_read_key_columns`: scan classification of a glob-named
  series described OTHER files' bytes — a dup-poisoned sibling produced a phantom finding against the CLEAN literal
  file (whose literal-read heal skips as stale → `healed != findings` → the L3 sentinel never stamps, permanent boot
  re-sweeps) + wrong census/`brutex lake audit-unique`/`/universe` numbers, and the targeted `_canonical_option_keys`
  overlap read rode the SAME seam (phantom overlaps a literal keyed removal could never converge on); `*`/`?` behave
  identically (`A*S` alongside `ABS`). FIXED at both raw-path sites with `glob=False`; already immune + unchanged:
  `read_partition_frame`/`heal_partition` + the bars writer's merge read (BytesIO); the bars `_read_key_columns` twin
  covered for free. REFUTES the round-8 en-route honesty note (reworded in place — unbalanced-bracket-only).
  Regressions (all 9 verified failing pre-fix): `TestGlobMetacharSeries` (writer, parametrized `X[12]`/`A*S`/`A?S`
  merge + the production `write()` arm — literal row preserved, zero cross-series import, no false `dedup_healed`,
  siblings byte-identical) + the two scanner literal-bytes/literal-sibling-overlap pins + the healer end-to-end
  bracket-series heal (converges, second sweep clean; `TestOptionForeignHeal` 23 → 24). (R15·NITs) the FOUR
  load-bearing `YYYY-MM` stem f-string copies (writer dest · `_option_file_month` canonical check ·
  `_canonical_option_keys` sibling path · the healer's reassignment dest) collapse onto ONE NEW pure
  `partition.option_partition_stem` (§11 COMMON, MINYEAR/MAXYEAR padding pinned); the duplicated "unclassifiable
  option partition" refusal message collapses onto ONE NEW `lake_invariants.option_unclassifiable_message` (in
  `__all__`, both gate consumers); the production merge's extra O(month-rows) `unique()` baseline pass per flush
  documented honestly in `_merge_frame_locked` (constant factor, accepted un-benched — the merge read dominates);
  HONEST: two combined-suite failures observed ONCE were unreproducible on re-run (suspected sandbox transient — no
  evidenced code defect, nothing claimed fixed). HOSTILE-REVIEW FOLDS (round 16, refuter-confirmed MINOR + 2 NITs;
  the surviving mutant re-applied + verified KILLED by exactly the new pin, the error-repr regression verified
  failing pre-fix): (R16-1·MINOR surviving mutant) the L3 `run_startup_sweep` denominator fold `files_scanned =
  scan.files_scanned + scan.option_files_scanned` had NO teeth — dropping the option term passed the ENTIRE 122-test
  covering suite, because the only rail pin (`test_mass_heal_refused_over_option_foreign_files`) computed the
  denominator inside the TEST body and called `heal_findings()` directly, never through the L3 fold; under the
  mutant an OPTIONS-ONLY lake read `files_scanned=0`, the `>= MASS_HEAL_MIN_FILES` (20) small-lake guard DISARMED
  the >50% refusal rail, and the default AUTO-ON boot sweep HEALED a mass-poisoned options tree the rail exists to
  refuse (reproduced under the mutant: healed=11, refused_mass_heal=False, quarantine created). PINNED at the L3
  level (`test_startup_sweep_mass_rail_counts_option_files_in_denominator`): the SAME 21-file/11-poisoned
  options-only fixture — extracted to ONE `_seed_mass_poisoned_option_series` helper, the inline copy killed (§11
  COMMON) — driven through `run_startup_sweep` against the REAL un-monkeypatched 20-file threshold (that threshold
  IS the mutant's escape hatch), asserting refused_mass_heal=True, healed=0, tree byte-untouched, sentinel NOT
  stamped, and the refusal event's files_scanned==21 (bars 0 + options 21); the mutant re-applied post-pin fails
  EXACTLY this test, 122 siblings green. The production line itself is UNCHANGED (pre-existing + correct) — this
  closes the missing teeth of the advertised "option files in the mass-rail DENOMINATOR … L3 rides automatically"
  claim. (R16-2·NIT, log-injection completeness) `lake_heal.partition_error`'s `error=str(exc)` re-admitted the RAW
  vendor-derived `options/<series>/` path AROUND the round-9 token guard — `OptionWriterError` messages interpolate
  the dest (`corrupt existing partition <dest>: …`, the containment refusals, the unclassifiable-partition
  verdicts), so a newline/ESC series name forged log lines through the error PAYLOAD at the one
  attacker-triggerable site; the field now rides `repr(str(exc))` (superseding the round-9 "the sibling `error=`
  field keeps the raw convention" clause for THIS site only — every other lake_heal `error=` field keeps the
  repo-wide raw-exception-message convention, operator-controlled or catalogued subjects); the round-9 regression
  extends to pin the repr'd error text (repr shape, no raw `\n`/`\x1b` — verified failing pre-fix). (R16-3·NIT) the
  reassignment audit/log `months` list was a FIFTH `YYYY-MM` stem f-string copy bypassing the round-15
  `partition.option_partition_stem` COMMON helper — collapsed onto the helper (byte-identical output, the existing
  months-list pins unchanged). HOSTILE-REVIEW FOLDS (round 17, refuter-confirmed 2 MINORs + NITs; every regression
  verified failing pre-fix): (R17-1·MINOR, quarantine containment) the quarantine BEFORE-image write was the ONE
  heal-side mutation outside the rounds-2/4/6 symlink-containment program — `heal_findings` derived
  `quarantine_root` with only an `exists()` probe and the writers' heal legs mkdir+`atomic_write_via`'d through
  whatever `<lake>/.quarantine` pointed at, so pre-planting `.quarantine` as a symlink (the round-2 V1 lake-write
  capability class) sent EVERY default-AUTO-ON batch's forensic before-image OUTSIDE the lake root BEFORE the
  in-lake source was rewritten/unlinked — voiding the "the quarantine copy IS the forensic before-image" guarantee
  that justifies the options no-raw_hash decision (and `os.replace` could clobber files under the link target);
  pre-existing DEC-LAKE-HEAL-001 machinery shared with the bars healer — this change set's own containment program
  made the asymmetry a gap. FIXED at the ONE batch chokepoint: `heal_findings` refuses the WHOLE batch untouched
  when `<lake>/.quarantine` `is_symlink()` (loud `lake_heal.quarantine_symlink_refused` — path raw,
  operator-controlled root + fixed literal — plus an `AUDIT_HEAL_BATCH` `refused_quarantine_symlink` line, the
  on_report alert seam still fires; EVERY finding counts an error so the L3 sentinel never stamps (`heal_errors`)
  and the next boot re-attempts; a ZERO-finding batch is deliberately NOT refused — no before-image is ever
  written, the `ordered` gate test-pinned), and the `-NN` serial loop also serials past a DANGLING symlink at the
  stamp path (`exists()` alone read False and the before-images would have written THROUGH the link). Honest
  envelope: a probe→write swap inside ONE batch is the same sub-call TOCTOU class as the documented same-second
  cross-process quarantine race — not claimed closed; `OptionParquetWriter.heal_partition` still writes wherever
  its *quarantine_to* points (docstring states it — the batch-level refusal is the rail for every production
  caller, all of which derive *quarantine_to* inside `heal_findings`). 3 regressions (whole-batch refusal over
  bars+option poison: nothing lands outside, sources byte-untouched, errors==findings, alert+audit fire,
  idempotent no-seam re-run · the zero-finding no-refusal gate pin · the dangling-stamp `-01` serial with the link
  untouched and its target never created), ALL verified failing pre-fix. (R17-2·MINOR, log-injection completeness)
  the round-16 error-repr fold missed the two options-tree `lake.invariant.scan_skip` sites still logging
  `error=str(exc)` where the exception MESSAGE embeds the SAME vendor-influenceable `options/<series>/` path RAW —
  a polars `ColumnNotFoundError` dumps the resolved plan (`Parquet SCAN [<path>]`) and a polars-raised
  `FileNotFoundError` interpolates the path un-repr'd (both empirically verified) — so a newline/ESC series name
  forged log lines through the escaped `path=` field's own SIBLING at the glob-loop read skip AND the targeted
  canonical-sibling read skip; both now log `error=repr(str(exc))` (path-free dtype-gate verdicts + the bars
  scan_skip stay raw — our-own/catalogued subjects). The finding's claimed THIRD site
  (`lake_heal.token_stat_skipped`) was REFUTED as a demonstrated forge channel (CPython's `OSError.__str__`
  repr()s its filename — refuter-verified) but joins the escaped convention as belt-and-braces, the comment
  stating the refutation honestly; 2 new regressions (keyless-parquet fixtures in a newline+ESC series dir pin the
  escaped error payload at both sites) verified failing pre-fix. NIT: the L2 seam integration-test comment
  overclaimed "never even read" for assertions pinning only bytes+mtime — reworded to the honest envelope
  (bytes+mtime prove never WRITTEN; the never-read claim rides the pure-filename months bound, pinned by the
  census call-shape spy + the read-spy scanner units). HOSTILE-REVIEW FOLDS (round 18, refuter-confirmed MAJOR —
  reproduced end-to-end pre-fix; every regression verified failing pre-fix): an OUT-OF-RANGE (in-dtype) `Datetime`
  VALUE in an options partition crashed the WHOLE scan/heal — the rounds-12/13/14 gate validated only the DTYPE, so a
  hand-planted `2**62`-µs stamp (≈ year 146,000) passed it and the shared `_count_foreign_rows` `min()` raised
  `OverflowError` from OUTSIDE the per-file read try/except, propagating out of `scan_lake_invariants` entirely: the
  L3 boot sweep crashed with the sentinel NEVER stamped (every boot re-ran the full O(files) scan and re-crashed —
  lake-wide healing, bars AND options, permanently inert), the `/universe` lake-uniqueness route 500'd on every census
  cache miss, the L2 F&O post-pull options census died, and via a scan→heal TOCTOU swap the heal-side `to_list()`
  PANICKED as `pyo3_runtime.PanicException` — a BaseException escaping BOTH `_PER_FINDING_HEAL_ERRORS` AND the L3
  `except Exception` blanket, killing the sweep daemon past the "boot never crashes" contract. Empirical fault matrix
  (polars 1.42.1, probe-verified): tz-aware positive overflow → `OverflowError` from `min`/`max`; tz-naive and/or
  NEGATIVE overflow → `ValueError`; `to_list` → the panic in EVERY shape. FIXED at the COMMON chokepoints:
  `option_key_dtype_error` VALUE-probes `min()`/`max()` (months are contiguous — a convertible min + max bounds every
  value between them; two vectorized O(rows) scans, honestly NOT O(1)) and verdicts "timestamp value out of datetime
  range" — loud counted scan skip, isolated `OptionWriterError` refusal at the healer's consistent read AND the
  writer's keyed re-read, canonical-sibling `overlap_rows=None` abstain; NEW COMMON
  `errors.datetime_out_of_range_errors()` (the FULL probe set incl. `ValueError`, with the documented consumption law
  — only around code whose sole Python-side failure mode is the stored-timestamp conversion) +
  `datetime_out_of_range_escape_errors()` (`ValueError` deliberately EXCLUDED from broad guards — around arbitrary
  heal/sweep code it is the genuine-code-bug class and keeps propagating, Golden Rule #1); the BARS twin closed in the
  same stroke (the bars scan loop's classification runs under the probe set — the identical whole-scan DoS one tree
  over, pre-fix-verified; `_canonical_timestamps` abstains on a panicking sibling; `reconcile_catalog_from_lake`'s
  GAP-#9 per-file skip widened — ONE planted bars file crashed the reconcile that runs at the start of EVERY sweep
  load); defense-in-depth: an options-loop residual conversion-fault guard, `_PER_FINDING_HEAL_ERRORS` += the escape
  set, and the L3 blanket is now `_SWEEP_GUARD_ERRORS = (Exception, *escape)` so the panic can never kill the daemon
  again. 14 new regression tests (16 parametrized cases) across 5 suites (options + bars scan survival & sibling abstains ·
  gate verdict aware AND naive · the out-of-range scan→heal swap isolated with the later same-batch dup still healing · BOTH escape-set
  members pinned by parametrized residual faults · `run_sync` panic survival · the L3 end-to-end sentinel-stamps pin ·
  the writer keyed re-read refusal · the reconcile skip pin · two error-set unit pins). Round-18 NITs: the three bars
  `YYYY-MM` months-list f-string copies in `lake_heal` collapsed onto the round-15 `partition.option_partition_stem`
  COMMON helper (byte-identical output); `_engine_offender`'s option arms documented as production-unreachable
  TOTALITY arms (the `_engine_relevant` filter excludes option findings; pinned by direct unit calls); the
  hand-planted naive-timestamp source with a tz-aware canonical sibling documented as the honest never-converging
  envelope in `_heal_option_foreign` (loud isolated `PolarsError` refusal each sweep, nothing mutated, no loss — a tz
  cast would EDIT stored values against the row-moving-only law, deliberately not mechanised).
  The #1175
  CLI-`brutex fetch` L2 seam follow-up has since LANDED on main for the BARS tree (DEC-CLI-LAKE-HEAL-L2-001 #1493 —
  `cmd_fetch` reuses `pull_controller._heal_after_pull` verbatim); that shared glue does NOT pass `include_options`,
  so the CLI seam stays bars-only (the CLI pull is spot-only; the options tree keeps its L3 + census cover) — wiring
  `include_options` through the shared glue is the remaining named follow-up. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; F&O
  lake REFERENCE_ONLY; no scope/vocab/rate change; `lake_invariants.py` (515/176) + `option_parquet_writer.py`
  (165/34) + `lake_heal.py` (621/180) + `catalog_reconcile.py` (75/20) + `errors.py` (50/0) + `partition.py` (83/24)
  ALL 100% line+branch; ruff/ruff-format clean, mypy
  `--strict` + pyright at the pre-change baseline).
  Earlier: `claude/movers-underlying-class` (**DEC-MOVERS-UNDERLYING-CLASS-001, MIGRATION.md #1650** —
  the `/movers` page gains an `All | Indices | Stocks` underlying-class filter on all THREE tabs
  (Spot / Futures / Options), mirroring DEC-MOVERS-MIN-VOLUME-001 (#1034) EXACTLY: ONE COMMON
  `brutex/dashboard/movers_underlying_class.py` (`normalize_underlying_class` blank/unknown → `all`;
  `candidate_passes_class`; `filter_by_underlying_class`; EMIT-ON-non-default note/echo helpers) applied at
  RANK time in `build_*_payload` over the SAME cached candidate set — never a re-scan. Classification is
  viewer-reads-storage-only: SPOT from the lake segment (`INDEX`→index / `CASH`→stock, the stored `is_index`
  reflection); FUTURES/OPTIONS from a `{underlying → kind}` map REUSING the six-step pull order's
  `primary_fno_underlying` + `is_index` classification (#1527, never re-minted). Unresolvable → `unknown`: in
  `All`, EXCLUDED from Indices AND Stocks, and COUNTED. Each candidate gains a DEFAULTED additive
  `underlying_kind` field (never row-emitted), so the default `All` payload is BYTE-IDENTICAL (pinned all
  three tabs). DELIBERATE DEVIATION (ledgered): the class is a RANK-time filter over the SHARED cached set,
  so — unlike the #917 expiry tier — it is NOT in `candidate_key`; cache-independence is proven by a route
  test (Indices vs Stocks over one coordinate → different rows, `movers_cache.size()==1`).
  ADVERSARIAL-REVIEW FIX ROUND folded (same branch, pre-merge — #1650, renumber history #1631→#1632→#1633→#1650 as PRs #1352/#1354 landed holding the prior claims and the slot wave reserved #1633–#1649): the F&O class lookup normalises the
  hyphen-preserving parsed token into the map's `primary_fno_underlying` key space (`BAJAJ-AUTO` previously
  misclassified `unknown`; `parse_future_symbol` right-anchored, mirroring `parse_fno_symbol`); the kind map
  + the stamped F&O candidate keys fold the `master_revision` CONTENT token via ONE
  `_underlying_kinds_cached` helper (a keyless load-instruments refreshes with NO generation bump; the
  warmer threads the kinds + shares the key — warmed rows were all-`unknown`); full default-payload
  equality pinned; the head's CI pyright red (mis-annotated test helper) fixed. DISPLAY-zone
  read-only — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX); touched modules 100% line+branch; ruff/ruff-format
  clean, `mypy --strict`/pyright clean incl. `app.py`).
  Earlier: `claude/oneclick-top100-object-root` (**DEC-ONECLICK-TOP100-OBJECT-ROOT-001, MIGRATION.md #1631** —
  the one-click `top100.json` READER-ADOPTS-WRITER blocker fix: the display reader
  (`oneclick_artifacts.load_top100`) now parses the SHIPPED engine writer's OBJECT root
  (`{schema_version, run_id, preset, source, baselines, entries}` — `write_top100`, default-ON in
  `cmd_sweep_all`) instead of demanding a bare ARRAY, so a REAL sweep no longer reads
  "corrupt · 0 strategies" on /operator · /screener · /strategy (the fixture-only blindspot — CI was
  green on hand-written bare arrays no production writer ever emitted). Unknown future schema_version /
  missing entries / the retired bare-array form → NAMED corrupt (never a crash, never a dual shape);
  per-row SUB-KEY shape validation with named partial degrades; the ONE additive metric-key map
  `TOP100_METRIC_KEY_MAP` (`max_drawdown_paisa`→`max_dd_paisa`, `sharpe_ratio`→`sharpe`,
  `deflated_sharpe`→`dsr`, `total_trades`→`trades`; absent `cpcv_pass_pct` stays absent → honest "—",
  never a fabricated 0); fixtures REGENERATED from the writer shape; NEW writer→reader golden
  (engine `build_top100`→`write_top100` → display `load_top100` → routes). Folds: [11] visible
  sort-basis labels on /results + the one-click surfaces (labels only — the preset tie-break
  divergence is RULED design-sanctioned per #1617 Ruling 10, `?preset=` UI exposure negative-pinned);
  [W2-4] — Ruling-18 null costs count at ₹0 with the honest gross-curve caveat instead of excluding
  every real trade; [W2-5] — the /screener count line names the render cap; the fabricated v24.4
  "LIVE" hook-banner ticker is DELETED by coordinator ruling (the honest #bxTickerStrip stays); [12] —
  `runs_revision` folds per-run manifest stat tokens (an in-place `suspect` rewrite moves the cached
  runs-listing key). Wave-3 fold: [W3-R1] the /strategy candle chart was DEAD ON ARRIVAL — the inline
  mapper indexed `idx.ts` while the real /api/v1/db/candles column is `timestamp`; ONE route-shape mapper
  `bx-chart.js::bxCandleRowsFromDbPayload` + the TZ-safe `bxParseTsIst`/`bxIstDateOfEpoch` (naive API
  strings = IST wall-clock pinned to +05:30 — never browser-local Date parsing; offset-carrying trade
  timestamps pass through) prove candle mapping AND arrow placement TZ-invariant via node goldens under
  TZ=America/New_York + the NEW real-route integration golden (test_strategy_chart_route_shape.py);
  [W3-R4] the null-VIX writer band (`{lo:null, hi:null}`) rendered "works when VIX between null–null" —
  ONE shared builder pair (`bxVixBandText`/`bxVixBandCellText` on BXOneclick) now reads the honest
  "VIX band not stamped for this run" (partial bands name the missing bound) across the operator card,
  /strategy story card and /screener band column; [W3-R3] the finder's /strategy arm "[object Object]"
  repro predates f1d271d4 — VERIFIED dead at this head (the reader-boundary bridge feeds every consumer;
  route-level wire proof + template pin added, no second arm source exists.)
  Earlier: `claude/dazzling-pasteur-jf8s3w` (**DEC-LAKE-STATS-MANIFEST-001, MIGRATION.md #1614** — the
  write-time PER-FILE lake STATS manifest, the per-file stats/paging EXTENSION of the shipped
  DEC-LAKE-MANIFEST-O1-001 (#1480) dataset-discovery manifest — claiming the #867/#1025 named future work ("true
  O(page) needs a write-time lake manifest"): NEW leaf `brutex/data/lake_stats_manifest.py` (stdlib + `atomic_io` +
  `partition_lock` + `lake_manifest` only — cycle discipline; one atomically-replaced 0o600 JSON shard per dataset
  carrying per-file DISTINCT-identity-key row counts, timestamp bounds, and `mtime_ns:size` stat tokens captured
  under the still-held partition flock). The `/db` candle + expired-options pagers serve totals / pages /
  stored-life spans MANIFEST-FIRST — the page query binds ONLY the files the (bounds, direction, offset, limit)
  window needs via a prefix-sum planner + rebased OFFSET — and ONLY after full per-request revalidation
  (parse+version+key echo+sum invariant · file-SET equality walk · per-file stat-token equality · month-bounds
  sanity vs the rel-path UTC month); ANY failure ⇒ the byte-identical legacy live scan + one
  `lake.stats_manifest.fallback` log + best-effort read-side reseed; post-query TOCTOU re-stat of every touched
  file; the degrade law reruns total+page+span together on the full legacy path — a manifest total is never paired
  with a degraded page. Five write hooks (spot writer both branches keyed on the NEW `WriteResult.file_rewritten` —
  bytes RENDERED, never `bars_added` · heal rewrite/quarantine · option writer merge/heal ·
  `lake_heal._quarantine_move` shard unlink) are ALL inside the already-held partition locks, best-effort (never
  raise into the pull; `LakeWriteHalt` from `.locks/` creation deliberately propagates), incremental (one row
  upsert, never a rescan), and LAZILY accreted (absent shard = one cheap probe — a key digest + one failed
  `os.stat`, probed FIRST via the public `partition_stats_tracked` so an unviewed write never stats dest / folds
  frame bounds [round 2]; seeding is read-side only — the prebuild's ~10⁵-10⁶ derived-TF writes cost one probe
  each). Kill switch `BRUTEX_LAKE_STATS_MANIFEST=off` ⇒ hooks +
  reads no-op ⇒ byte-identical legacy scans (modulo the sanctioned tie-order pin below, which applies
  unconditionally); shard flock via the COMMON `partition_write_lock` with fail-loud
  `STATS_LOCK_TIMEOUT_S=5.0` (env `BRUTEX_STATS_LOCK_TIMEOUT_S`; 5+75 < 90 s write-deadline pin). SANCTIONED
  behavior change: both pagers' ORDER BY gains the physical `filename, file_row_number` tiebreaker so an options
  CE+PE same-minute tie has ONE total order on the pruned AND the full path. HONEST (Golden Rule #10): a hit is
  O(page-files) Parquet work + an O(D) per-consult revalidation stat sweep (D = the dataset's month files, ~80) —
  NOT literal O(1) (only the repeat TOTAL/span lookup is O(1) via the generation cache — a repeat PAGE click
  re-pays the O(D) revalidation + O(page-files) read; the FIRST star-select click per (generation × file-state)
  additionally pays ONE O(D)-footer union-order DESCRIBE, memoised on the file-state fingerprint — measured 72/72
  files on that first click, 1 thereafter [the bench's 1-file figure is the warmed-memo click]); the FIRST view
  per dataset/invalidation pays today's
  O(files) scan once, and a permanently-REJECTED seed (un-healed poison / an out-of-band 0-row parquet — the
  distinct `empty_file` reason) is LATCHED per (dataset, generation) so the doomed grouped scan never repays per
  click, and the degrade-law LEGACY recount memoises under its own `legacytotal|` generation key so a
  persistently-unprunable page (heterogeneous star union) recounts ONCE per generation, never per click [round 2];
  a viewed dataset's write pays one bounded shard flock + small fsync'd JSON rewrite (the wall-clock A/B gate
  asserting the median overhead under the 12 ms merge-budget floor is wired into the self-hosted bench-m4pro
  workflow AND FIRST EXECUTED on the real M4-Pro tier 2026-07-07, actions run 28888100079: the A/B
  merge-budget gate GREEN — the measured median overhead lives in that run's log; the SAME run measured the
  O(page) paging click's strict wall-clock ratio at 1.53× (t_small=2.272 ms, t_large=3.485 ms — this entry's
  OWN documented O(D) statx sweep, 66 extra month files), so the paging gate's strict-tier ceiling was
  recalibrated 1.1× → 3.0× (`METADATA_SWEEP_STRICT_CEILING` in `tests/bench/_o1_tiers.py`, the
  DEC-O1-CACHE-TIER-001 precedent; the shared-CI 8.0× default unchanged) while the deterministic
  file-touch/fs-op-count probes — all green on the same run — remain the PRIMARY O(page) teeth
  [round 4 history: the gate previously executed in NO workflow while this line presented it as an operative
  pin — since wired AND executed, the ENDGAME round-1 reconcile of the stale "DORMANT / NOT yet executed"
  wording that outlived the run]; the
  deterministic byte-parity/anti-storm teeth run in shared CI; unviewed = one probe, pinned); a same-`(mtime_ns,size)`
  in-place overwrite is the documented stat-token residual; the new module's mutation gate is WIRED into the
  weekly Mutation workflow's per-module ZERO-survivor mutmut loop AND was EXECUTED in-session [round 8 — the
  refuter caught the prior "rides the weekly Mutation workflow (not verified in-session)" wording while NO
  `--paths-to-mutate` in mutation.yml named the module, an unwired gate presented as a deferred one: now wired
  (`TestMutationWorkflowWiring` pins the pair) and measured — 473 pre-hardening mutants → 84 pending, killed by
  the `TestMutationHardening` pins; round-8 enumeration 456 mutants / 0 survived / 0 suspicious. ENDGAME
  round 2 re-measured 462 enumerated — 196/462 machine-verified 0-survivors in-session (the run was stopped at the session budget; the interim-shape run reached 353/462 with exactly ONE
  survivor — the annotation mutant below, since eliminated; every restructured line's mutants were also
  hand-applied + KILLED; the WIRED weekly Mutation gate enforces the full zero-survivor law) after PURGING the round-8 fd-sentinel / bounded-read / dead-`"unknown"`
  line pragmas — the refuter proved they suppressed KILLABLE, NON-equivalent mutants (a `+1` fd sentinel
  closes STDOUT on the absent path; a shortened read bound truncates an exactly-at-cap shard) while three doc
  surfaces called the whole residue "provably-equivalent": the loader now returns `DatasetStats | str`
  (the exactly-one-of-two contract STRUCTURAL in the type — the defensive dead `"unknown"` literal is gone),
  the fd handoff is a plain `owned` boolean pinned by an `os.close` spy (`test_loader_fd_close_discipline`:
  stuck-True double-closes, stuck-False leaks — an interim `fd: int | None` sentinel was itself caught by the
  re-enumeration, its annotation-only `int & None` mutant unkillable since a local annotation is never
  evaluated [PEP 526]), and the read bound is a cap-sized read + ONE-byte over-cap probe (still race-free,
  still ≤ cap+1 bytes) pinned by the inside-the-JSON-padded exact-at-cap boundary test; the ONLY remaining
  pragmas are the `field=` names feeding messages the loader's `except` clauses DISCARD — the genuinely
  equivalent residue]. REVIEW ROUND 1 (refuter panel — every CONFIRMED finding fixed with
  a fails-before regression): the option-writer CI 100% gate joined its new suite (was RED at 94.74%); the
  read-side seed catches `LakeWriteHalt` (a disk-full seed can never 500 a /db read; write-hook propagation
  untouched); deep-nested-JSON shards fail closed to the `parse` clause (RecursionError caught); the `file_set`
  fallback rel lists are capped at 20 + true counts; provenance `db.stats_manifest.read` raised DEBUG→INFO;
  `seed_dataset_stats` documented honestly as LAST-WRITER-WINS (clobber detected + reseeded next read, pinned);
  the `BRUTEX_STATS_LOCK_TIMEOUT_S` × 90 s write-deadline interaction documented + doc-pinned (not clamped — leaf
  discipline); the spec §10 hypothesis paging-equivalence + Σ-pages==total + concurrent-writer thread-fuzz
  properties landed (multi-process soak / crash-loop stress / CLI cross-process leg stay named follow-ups).
  REVIEW ROUND 2 (refuter panel — every CONFIRMED finding fixed at root cause): the degrade-law recount memo +
  the probe-first hooks + the star-DESCRIBE disclosure above; the spec §10 bench (c) lake-size-flatness gate
  landed (a served click's os.stat/os.scandir counts + touched files EXACTLY equal on a bare vs
  48-dataset-padded lake — deterministic op-count teeth, no wall-clock flake); the seed-skip `missing` list
  capped at the now-public `FALLBACK_REL_LOG_CAP` + true count; the kill-switch docstrings' "byte-identical"
  overclaim softened; the #1614 GATES counts refreshed.
  ROUND 3: the OverflowError timestamp class + plain-ValueError/UnicodeDecodeError shard-byte escapes fail
  closed; the option-writer distinct-key merge baseline. ROUND 4: a star-projection column name carrying `"`
  (an out-of-band parquet) now joins the fail-closed degrade ladder — legacy rerun + one `star_columns`
  fallback log instead of a persistent per-click HTTP 400 on both pagers; the shard read is BOUNDED
  (`MAX_SHARD_BYTES` 16 MiB, ~1000× a real shard — an out-of-band multi-GB shard fails closed `oversized` /
  corrupt-unlink instead of being read whole where a MemoryError would raise into the pull); the A/B +
  O(page) benches wired into bench-m4pro.yml (workflow-wiring pin test) + this blurb's stale "pins" wording
  fixed; the route-level integration now proves manifest-served TOTALS on the wire via a fresh app (the
  control run's shared count cache had cushioned the equality).
  ROUND 6: a NON-REGULAR inode planted at the shard path (FIFO/device/socket) no longer HANGS the reader or
  the partition-locked write/heal hooks forever — the loader opens `O_RDONLY | O_NONBLOCK` + `S_ISREG`-checks
  the OPEN descriptor, the NEW fail-closed `not_regular` clause (read side legacy-falls-back + the seed's
  `os.replace` heals the inode, wire-pinned; write side corrupt-unlinks; SIGALRM-watchdog regressions pin the
  pre-fix >5 s hang on BOTH sites); the OPTIONS pager's `db.stats_manifest.read` provenance is now
  content-asserted (a swapped `page_source` label mutant passed the whole suite before; the spot pin gains its
  `kind` assertion). ROUND 7: the #1025 route-level count-cache pin regains its wire-level anti-STALENESS
  teeth (two genuinely NEW `(side,timestamp)` option bars written through the PRODUCTION writer before the
  generation bump — a stale serve answers the old total and fails — with the no-recount law still pinned); a
  DIRECTORY planted at the shard path is healed via `shutil.rmtree` (`dir_shard_removed`, warn-only contract
  kept) instead of re-emitting the `corrupt_unlinked`+`unlink_failed` warn pair on every later write; the
  EMPTY-planned page (offset past the in-bound end) now runs the SAME post-query TOCTOU re-stat via the ONE
  extracted `_stats_tokens_intact` helper (both call sites — no copy).
  ENDGAME (post-merge refuter reconciles): round 1 — the stale "DORMANT / NOT yet executed" A/B-gate wording
  reconciled to the EXECUTED m4pro run 28888100079 (A/B gate GREEN; the paging strict ceiling recalibrated
  1.1× → 3.0× `METADATA_SWEEP_STRICT_CEILING` off its measured 1.53×), + the §1↔§3 active-branch consistency
  pin. Round 2 — the killable-mutant pragma purge above; §2 Rule 5's stale "strict tier DORMANT until the
  operator registers the runner" claim reconciled to the LIVE runner (+ the metadata-sweep allowance now named
  beside the cache-tier one); the frozen `_Last updated:` header's per-round narration replaced with a
  stale-proof pointer to this blurb + MIGRATION.md #1614; bench-m4pro.yml's paging-gate comments repointed
  from the stale "ratio_ceiling() 1.1× tier" to the real `metadata_sweep_ceiling` 3.0× — each with a
  deterministic doc-pin regression (`test_claude_md_branch_consistency.py` + `TestBenchWorkflowWiring`).
  Round 3 — the Unicode-digit month-rel hole (MINOR, the ONLY tamper class that SERVED instead of failing
  closed): the month-grammar regexes' Unicode-aware `\d` + `int()` accepted a planted `٢٠٢٣/١٢.parquet` as
  (2023, 12) with valid bounds, passing ALL FOUR clauses while sorting lexicographically AFTER every ASCII rel
  — the confirmed repro served a chronologically MISORDERED /db page (Jan-2024 where legacy serves Dec-2023);
  fixed at root cause with `re.ASCII` grammar + a clause-1 STRUCTURAL chronology law (every rel must parse,
  new fail-closed `rel_grammar` clause; the parsed (year, month) sequence must be strictly increasing —
  subsuming the lexicographic sorted+unique check), each regression pinned fails-before incl. the end-to-end
  misordered-page repro; plus the #1614 round-2 GATES exactness reconcile (474/148 + 221 → the measured
  476/148 + 222; round-3 tree measures 482/152, 232 tests).
  Results-safe — never in run identity (blake3) / partition bytes / `data_snapshot_hash`; rendered
  Parquet bytes byte-identical (ON/OFF parity bench). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  `lake_stats_manifest.py` + `parquet_writer.py` + `option_parquet_writer.py` +
  `lake_heal.py` + `db_view.py` ALL 100% line+branch; ruff/ruff-format clean, `mypy --strict`
  clean, pyright 0 errors in every touched/new file).
  Earlier: `claude/sweep-log-level-guard` (**DEC-SWEEP-LOG-VOLUME-GUARD-001, MIGRATION.md #1630** —
  the sweep CLI log-volume guard: ONE `main()`-dispatch guard configures the COMMON
  `observability/logging.configure_logging` seam at INFO by default for
  `sweep`/`sweep-all`/`sweep-daemon`, with the fail-loud `BRUTEX_LOG_LEVEL` override
  (DEBUG/INFO/WARNING/ERROR; junk → loud exit 1) — an unconfigured sweep process streamed per-trade
  DEBUG events unfiltered to stderr (measured 870 MB on a ~200-session fixture; disk-fill →
  `LakeWriteHalt` run-killer on the 2-4 day grid). Sink-level threshold only; audit JSONL hash chain
  untouched (structlog-free, pinned); an already-configured process (the dashboard sweep job) is
  deferred to; R4 FOLD — the SAME bound is applied INSIDE every spawn worker at
  `sweep_parallel._worker_init` via `configure_process_logging_best_effort` (spawn workers never inherit
  the parent config; real 2-worker spawn regression), the dashboard sweep inherits it automatically, and
  `BRUTEX_LOG_LEVEL=ERROR` is documented as an opt-in hazard (silences watchdog/quarantine WARNINGs).
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules 100% line+branch).
  Earlier: `claude/oneclick-sweep-runs-rebind` (**DEC-ONECLICK-SWEEP-RUNS-REBIND-001, MIGRATION.md #1621** — the
  P4 merge-integration rebind, built ON TOP of PR #1280's `claude/operator-page-real-rebuild` head (the eventual PR
  targets main only after #1280 merges): the one-click discovery seam repoints `<results_root>/runs/` → the Ruling-4
  `sweep_runs/` SIBLING root (mirror-pinned to `trade_store.sweep_run_dir`; NO legacy `runs/` fallback — the fixture
  corpus migrated instead, `fixture_results_root` keeping the fixture override self-contained), and the trades seam's
  PRODUCTION default becomes the NEW `TradeStoreReader` over the SHIPPED #1275 results-depth sidecars
  (`read_trade_page`; top100 additive `run_id` + `equity_ref.tuple` → `sidecar_paths_from_ref`, hostile refs never
  touch disk; pre-#1275 top100 / absent stamp → loud awaiting, damage → NAMED corrupt, PAGE_LIMIT_MAX=500 clamp named,
  Ruling-18 None lots/costs rendered "—", additive `trade_seq`/`path_1m_status`/`mae_granularity` surfaced with a
  "(1m)"/"(tf)" suffix). P2 manifest/events writers still NOT shipped — awaiting states + the ASSUMED chain algorithm
  stand. DISPLAY zone only — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), engine imported never edited; both oneclick
  modules 100% line+branch standalone; ruff/ruff-format/`mypy --strict`/pyright clean.)
  Earlier: `claude/operator-page-real-rebuild` (**DEC-OPERATOR-PAGE-REAL-001, MIGRATION.md #1561** — the Operator
  page (/) is wired to the sweep-oneclick ARTIFACT CONTRACTS and the v29 PREVIEW mockup is KILLED by a MECHANICAL
  sweep (every hardcoded ₹/count/date/invented-strategy-name site re-fed via `data-oc=` hooks, em-dashed, or
  deleted — the fabricated CHARGES table deleted outright, Track 1 is signal-only; a generic
  zero-₹-outside-allowlist pin guards the class). Real funnel/badges/rank-1 card fed from
  `<lake>/sweeps/runs/<run_id>/` {manifest.json, top100.json, events.jsonl} through ONE clearly-marked integration
  adapter (`brutex/dashboard/oneclick_artifacts.py` + the pure `oneclick_view.py` payload builders) — LOUD
  "awaiting real run artifact" states, named corrupt/partial/EMPTY degrades (a valid-but-empty top100 is status ok,
  `row_count` = `len(rows)` never an assumed 100), NEVER a 500 or a fabricated count; trades ride a typed
  `TradesReader` seam whose PRODUCTION default is the honest `AwaitingTradesReader` — the file-reading
  `FixtureTradesReader` activates ONLY under the fail-loud `BRUTEX_ONECLICK_FIXTURE_ROOT` (loud badge + a visible
  per-table `source` chip; ResultsStore paged-API rebind at merge-integration; fixtures live in tests/, a
  production lake with no artifacts shows awaiting states — the HONEST fixture-vs-real boundary). NEW
  `/api/v1/oneclick/{runs,manifest,top100,events,trades,verify}` — stat-token-keyed `MoversSnapshotCache` reads,
  bench-pinned (`tests/bench/test_oneclick_o1.py`, 6 pin groups measured 2026-07-07 at 0.98×–1.00× flat +
  deterministic no-recompute/no-reparse teeth). HONEST O-envelope (Golden Rule #10): the cached hit is O(1) vs run
  count / run-history depth / sibling-tree size / trades total, the events tail is O(page) (byte-offset seek +
  1 MiB window; EOF repeat polls short-circuit on the stat token) — but the FIRST read per artifact revision
  parses the O(artifact) file, the manifest fresh compute additionally pays one run-dir probe + one O(page) events
  tail for the chain map, every repeat poll pays O(1) `os.stat` token syscalls, and the timeline search is
  WINDOW-scoped over a capped 5,000-record client ring — never O(1)-total. The Run button MOVES to the Operator
  page (wired to the EXISTING `POST /api/v1/sweep/run` + `/api/v1/cascade/run`; the condition-subset multi-select
  ships DISABLED "lands at merge-integration (P1)" — shown, never faked live; the preflight/estimator strip is
  STATIC honest text — the fixture adapter exists but no route serves it yet) and `/reports` returns to pre-open
  ONLY. NEW `/screener` (client filter/sort over top100, createElement-only rows, full-precision BigInt/string
  comparator with the rank-ascending tie-break, null-VIX rows excluded-but-COUNTED under a band filter; the R26
  virtual-scroll deviation is LEDGERED — a fixed ≤100-row artifact renders directly in one pass with a
  RENDER_CAP=200 guard, NO in-CI ms measurement claimed — the #1557 Playwright browser-truth job, which landed
  on main mid-flight, does not drive /screener) + `/strategy` (chart-first:
  createElementNS SVG candles off the EXISTING `/api/v1/db/candles`, trade-centered window, an out-of-window trade
  DROPS its arrow with a loud note, trades inline at load — ≤2-click law; equity = client int-paisa cumsum LABELED
  "per-trade close equity — minute-path equity lands with P3"; vix_band on the story card; ONE shared Beginner/Pro
  mechanism). R44 VERIFY-ME panel: 7 checks under a 5-state truth table (pass/fail/READY/awaiting/not_wired —
  operator-judged checks cap at READY server-side; not-wired capabilities never read green; `check=chain` runs the
  O(file) hash-chain verify on CLICK only, cached per events revision, and every chain verdict permanently carries
  "algorithm assumed until P2 merges" — the golden proves generator↔verifier SELF-consistency only). The events
  chain law + `runs/<id>/events.jsonl` path + `per_door_kills`/`actual.funnel` shapes + the interim fixture-only
  trades layout are registered contract PROPOSALS isolated behind named seams — never drifted unilaterally. XSS:
  every artifact-derived string DOM-bound via textContent/createElement(NS) only, event payloads JSON.stringify'd
  into ONE text node, the hostile fixture twin driven through the routes; one-shot fetch failures → errorCard +
  Retry, never a blank grid. Display copy: stale "52-condition" → 60, "u64" → u128 (display surfaces only). NAMED
  OUT-OF-PR: the bx-rate-monitor.js/bx-liveproof.js refactor onto the shared bx-poll.js core; regime/family
  display (P3); the real trades rebind + P2 chain confirmation + the preflight render (merge-integration). ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX, F&O lake REFERENCE_ONLY, no scope/vocab/rate change; DISPLAY zone only —
  zero engine/orchestrator/ranking/results-storage/preflight edits, sweep/cascade POST endpoints byte-unchanged;
  `oneclick_artifacts.py` (359/94) + `oneclick_view.py` (165/72) BOTH 100% line+branch STANDALONE + two NEW static
  ci.yml per-module gates; ruff/ruff-format clean, `mypy --strict` clean, pyright at the pre-change baseline
  (0 new); node-executed JS goldens + `node --check` clean on all five new JS files).
  Earlier: `claude/peaceful-newton-fehcc3` (**DEC-GREEKS-BULK-REENRICH-001, MIGRATION.md #1558** — the bulk
  Greeks v1→v2 re-enrichment BACKFILL closing the two #1188 named gaps: EXPIRED option contracts frozen at formula v1 +
  CLI-`brutex fetch`-only lakes that never enriched. NEW `brutex/data/greeks_backfill.py` — a stamp-driven
  target-DISCOVERY + batching layer feeding the EXISTING `enrich_greeks_for_targets` driver UNMODIFIED (value-checked
  reuse gate + every honest demotion ride through; a Greek is NEVER fabricated; NO formula bump — the enrich goldens
  stay frozen). Scanner: manifest-first enumeration, `1minute`-only, per-contract `greeks_provenance_id` column probe
  through the NEW exported `stamp_is_current_inputs`/`current_inputs_pack` (ONE pack shared with the driver gate),
  honest census (unstamped/v1/inputs-drift/current/futures/undecodable/probe_failed), targets sorted
  (underlying, exchange, symbol), `force`/`expired_only`/`limit`/`per_file_pause` modes. Runner: bounded batches
  (default 25; fail-loud `BRUTEX_GREEKS_BACKFILL_BATCH`/`_PAUSE_S`), DURABLE advisory checkpoint =
  `SweepCheckpointStore` reused verbatim at `<lake>/.greeks_backfill/checkpoint.json` (corrupt/foreign-id → ignored
  loudly; record failure → logged + continue; DELETED on completion — the stamps are the durable done-record);
  `LakeWriteHalt`/`GreeksEnrichPoolBroken` never caught below the job/CLI boundary. Surfaces: `POST
  /api/v1/universe/greeks-backfill` + O(1) progress route on a dedicated `BackgroundPullJob` (account lock skipped —
  zero vendor calls; lake-root single-flight KEPT so a backfill and a pull on one lake serialise) + a `universe.html`
  panel (textContent-only); `brutex greeks-backfill` with a FULLY INERT `--dry-run` (zero writes/checkpoint/Telegram,
  whole-tree sha-pinned; the one documented read side effect is the shared `.manifest/` cache self-heal); exactly ONE
  Telegram completion summary (`greeks_backfill_complete` ✅/⚠️ + `emit_greeks_backfill_complete_alert`). HONEST: the
  scan is O(files) NEVER O(1); no wall-clock SLA; the stamp prefilter is blind to rate/spot VALUE drift under
  unchanged source kinds — `--force` routes everything through the driver's value-checked gate. ADVERSARIAL-REVIEW FIX
  ROUND folded (same branch, pre-merge; details in #1558): CLI wet run HOLDS the lake-root single-flight (refusal on
  contention; `--dry-run` lock-free); scan/probe per-contract/per-file watchdog liveness beats + honest scanning label
  (the DEC-PIPELINE-STALL-HEARTBEAT-001 false-CRITICAL class closed on the new phase); `force`+`limit` REFUSED at
  scanner/CLI/route (chipping is default-stamp-mode-only); `SweepCheckpoint.from_dict` rejects non-object JSON with
  `SweepCheckpointError`; schema-poisoned stamp columns + out-of-range month filenames fold into `probe_failed`/skip
  instead of crashing the scan; expiry==today boundary pinned (kills the `>=`→`>` mutant). FIX ROUND 3 folded
  (refuter-majority-confirmed, #1558): the checkpoint freezes at the last fully-clean prefix boundary (an errored
  contract is always re-fed on resume — never a false-clean `errored=0` skip); two concurrent jobs keep independent
  watchdog liveness (the interim multi-registrant beat registry was SUPERSEDED at merge 040b2aec by main's
  DEC-PULL-BEAT-PERRUN-001 #1537 pull-id-keyed channels — see the #1558 supersession note); a stray file at
  `.greeks_backfill` reads `absent` instead of muting the whole `lake_revision` composite. FIX ROUND 4 folded
  (refuter-majority-confirmed, #1558): the `backfill_id` binds a rate-source VALUE fingerprint — a crashed force run's
  checkpoint self-ignores across a rate-value change, so the recorded prefix is re-routed through the driver's
  value-checked gate (spot-heal-in-crash-window = documented residue) [COMPLETED by FIX ROUND 10 for the A→B→A
  round-trip / intervening-writer class the fingerprint cannot see]; a concurrent backfill scan no longer masks a
  wedged pull's HUNG→WEDGED chain (shipped as #1537's keyed channels after the merge-reconcile superseded the interim
  `bind_progress_beat_scope` design — `BackgroundPullJob._run` runs inside `pull_id_scope` with its beat keyed on its
  own run id; the round-3 broadcast's "WEDGED dual-signal backstop" honesty claim stays retracted as vacuous); NEW
  `stale_unfixable` census bucket for
  null-stamped non-finite-OHLC rows (driver-skippable IVX-2 class — excluded from targeting so the census CONVERGES,
  ⚠️ Telegram/note/stderr); non-finite `--pause-s`/`_PAUSE_S` refused eagerly; #1175-style bench pins added
  (flat per-file probe · stamp-column projection spy · O(1) checkpoint bytes). FIX ROUNDS 5+6 folded
  (refuter-majority-confirmed, #1558): eager env validation on both surfaces (CLI one-liner + route 400 BEFORE the
  scan/lock); non-canonical month filenames → `probe_failed` + rename-to-canonical WARN (never an eternal re-target);
  the fixability gate generalised to NON-null stale stamps on driver-skippable rows; `int(inf)` `OverflowError`
  mapped at the stamp probe + `SweepCheckpoint.from_dict`. FIX ROUND 7 folded (refuter-majority-confirmed, #1558):
  every suspicious-file targeting judgment runs over the DRIVER's row view (`_driver_row_view` — own-UTC-month bound
  + the COMMON `dedup_bars_frame` keep-first, `read_option_rows`' exact per-file discipline), never the RAW rows — a
  stale stamp only a dropped duplicate-timestamp/foreign-month row carries folds into `stale_unfixable` + the
  `stale_on_driver_invisible_rows` WARN naming lake_heal (previously an eternal already_current/EXIT_OK-✅
  false-green re-feed, both directions: kept-NaN shadowed by a finite dup AND a dropped dup with the only stale
  stamp); the LEGACY 7-column arm rides the same driver-actionability gate instead of an unconditional stale-null
  target (the CLI-fetch-only-lake shape; empty legacy partition = honestly nothing-to-do); unfixable wordings widened
  to "driver-unactionable rows"; the clean fast path stays the bench-pinned single stamp-column read. FIX ROUND 8
  folded (refuter-majority-confirmed, #1558): `_month_files` mirrors the driver's reachability predicate EXACTLY —
  canonical zero-padded name AND `is_file()` — so a canonical-NAMED dataset-style DIRECTORY (`2024/01.parquet/`,
  probe-readable by polars yet invisible to `ist_span_partition_files`' `is_file()` gate) folds into `probe_failed` +
  the driver-unreachable WARN instead of an eternal-re-feed ✅/EXIT_OK false green (the rounds-5/7 class escaping
  through filesystem TYPE); and the merge-absorbed DEC-CLI-GREEKS-ENRICH-001 enrich pass (`brutex enrich-greeks` + the
  `brutex fetch` post-ledger tail via `run_greeks_enrich_pass`) — the SECOND sanctioned ledger-less Greeks-overlay
  writer — touches the SHARED cross-process freshness marker on every bar-writing pass through the NEW wheel-free
  COMMON `brutex/data/greeks_revision.py` (`greeks_backfill` re-exports/delegates byte-identically; best-effort, a
  zero-write pass fabricates no signal), closing the round-2 dashboard-staleness class on that surface and
  reconciling `lake_revision`'s falsified "the ONE sanctioned production writer" honesty claim. FIX ROUND 9 folded
  (refuter-majority-confirmed, #1558): the poisoned-stamp fold moved to a DTYPE gate (`_stamp_dtype_poisoned` — any
  non-integer `greeks_provenance_id` dtype → `probe_failed` at both the raw probe and the verdict, before the null
  branch; a coercible-but-non-integer stamp — utf8 `"7"`, Boolean, fractional float — previously sailed past the
  rounds-1/6 raising-`int()` catches and detonated the un-fenced driver-view `is_in`, ONE junk file killing the WHOLE
  scan; the unreachable coercion-trio catches removed, whole-float columns fold `probe_failed` — sanctioned
  narrowing); the `universe.html` backfill panel poll never gives up (quick retries then 3 s→30 s capped backoff —
  the DEC-PULL-UI-STALENESS-001 posture; non-ok/malformed polls never settle; hidden tab fires zero fetches +
  visibilitychange catch-up; `gbAttachIfRunning()` re-attaches on page load and on a started:false refusal — the
  button re-enables only on an observed terminal state); the rounds-3/4 progress-beat mechanism claims reconciled to
  the shipped #1537 keyed-channel design (supersession notes in #1558). FIX ROUND 10 folded
  (refuter-majority-confirmed, #1558): checkpoint ADOPTION is freshness-gated — the round-4 fingerprint attests
  plan-vs-checkpoint rate EQUALITY only, so a rate A→B→A round trip (a rate-B force run's errored first batch froze
  the id-A checkpoint while genuinely rewriting the recorded prefix under rate B; the revert to A reproduced id-A and
  ADOPTED the stale checkpoint — a false-clean ✅ `errored=0` skip of the residue, stamped current forever; same
  vector crash-free via an intervening enrich pass under a changed rate env) now self-ignores LOUDLY + deletes the
  stale file when the shared overlay marker's LAST writer is foreign at/after the checkpoint's `updated_at` (own-plan
  touches identity-exempt — legitimate same-plan resumes still adopt) or the window ledger moved at/after it
  (`_checkpoint_superseded_reason` + the additive engine `SweepCheckpointStore.discard_adoption()`; ambiguity biases
  to the fresh correct-by-stamps start; NEW residue documented: a mid-pass-crashed intervening writer leaves no
  signal — delete the checkpoint, the spot-heal remediation); and the gb-poller gains the sibling-parity pieces
  round 9 omitted: ONE `gbFetchJson` helper arming the FEED_FETCH_TIMEOUT_MS hung-fetch abort bound through the BODY
  read on all three gb call sites (a stalled body previously wedged the panel forever on a visible tab — nothing
  thrown, nothing scheduled) + the `gbPollGen` re-entrancy guard (catch-ups supersede in-flight chains; stale late
  responses never repaint). ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; F&O lake REFERENCE_ONLY; `greeks_backfill.py`/`greeks_enrich.py`/
  `greeks_enrich_pass.py`/`greeks_revision.py`/`sweep_alerts.py`/`sweep_checkpoint.py` ALL 100% line+branch;
  ruff/ruff-format/`mypy --strict`/pyright clean).
  Earlier: `claude/sweep-stall-oom-guards` (**DEC-SWEEP-STALL-OOM-GUARDS-001, MIGRATION.md #1608** — the
  parallel grid-sweep (`brutex/engine/sweep_parallel.py`) gains a HEAVY-GRID-AWARE stall watchdog (the wait budget
  scales by the in-flight `mask·bar` work `C(n_unique, k) × N_bars` so a legitimately-slow k=5 tuple is not
  false-aborted near grid completion, while a genuine hang still aborts on the FINITE bound; env
  `BRUTEX_SWEEP_STALL_SECONDS` + `BRUTEX_SWEEP_STALL_MASK_BARS_PER_SEC`) + a durable REVERSIBLE OOM QUARANTINE
  (a broken pool — and a worker's surviving Python-level `MemoryError`, the cgroup/rlimit shape — records the
  poison tuple(s) in the sidecar `<root>/.sweep_quarantine.json`, drains finished siblings' real results, then
  fails LOUDLY with `SweepWorkerPoolBrokenError` naming tuple(s) + sidecar + remediation; a resume skips them
  VISIBLY as the DISTINCT `tuples_quarantined_prior` summary counter — never `skipped_cache` — and deleting the
  sidecar entries genuinely recomputes them; deliberately NO cache-record/checkpoint advance, so no permanent
  invisible loss and no identical-resume livelock; HONEST residual: a WEDGED non-OOM tuple still resume-livelocks
  via the `SweepStallError` path — pre-existing, named follow-up). Additive resilience — a CLEAN run is
  byte-identical, frozen sweep goldens + P-06/suffix-independence UNMODIFIED; `sweep_parallel.py` +
  touched `sweep_supervisor.py` 100% line+branch. ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change).
  Earlier: `claude/intelligent-hamilton-qf3w38` (**DEC-SWEEP-NIGHTLY-AUTOMATION-001,
  MIGRATION.md #1612** — the R9 nightly zero-click chain + R50 backup/restore + launchd scheduling:
  NEW `brutex/nightly/` package (chain/probes/pipeline_binding/sweep_binding/backup/restore/report/
  schedule/run) driving ONLY existing public entry points; `brutex nightly
  run/backup/restore/install/uninstall/status`; the nightly single-flight flock + holder-note seams on
  the ONE `_flock_singleflight` core (existing callers byte-identical); two SweepAlerter methods
  (`nightly_summary` INFO/WARN/CRITICAL + the instant `nightly_refused` CRITICAL, per-run dedupe keys,
  "?" for unknown counts); the R42 break law property-pinned over every failure position; SUSPECT ⇒
  exit 2 (cron-red); the CI restore drill (real-writer lake → snapshot → mutilate + extra post-snapshot
  delta → restore → byte-parity + deltas-clear + ledger-LAST + audit chain green) on every push; the
  headless pipeline binding parity-pinned against the dashboard closures (ONE shared
  SessionRateController; `nightly_metadata_years` ≡ `app._resolve_metadata_years`); the sweep leg's
  kwargs signature-drift-pinned vs `cmd_sweep_all` with the escape-flag trio structurally absent.
  HONEST: R48 decay diff deferred to v1.1 by operator directive (recorded divergence — R48 homes in P6,
  the file's v1.1 item is R49); liveness heuristic until P2's flock; post-deploy full re-sweep
  (`code_commit_sha` re-key) documented not fixed; `bars/` excluded from the automated backup; ≤24 h
  snapshot exposure (backup runs PRE-pull — the rollback point); launchd wake limits + pmset opt-in
  documented; backup honestly O(files+bytes). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; F&O lake
  REFERENCE_ONLY; no scope/vocab/rate change; touched modules 100% line+branch; ruff/ruff-format clean,
  `mypy --strict` clean, pyright at the 39-error pre-change baseline, 0 new).
  Earlier: `claude/elegant-johnson-cgufi7` (**DEC-SWEEP-RESULTS-DEPTH-001, MIGRATION.md #1617** (renumbered
  #1538→#1557→#1581→#1598→#1610→#1617 — the first three hops across the 2026-07-08 origin/main merges (main's #1538 is DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001, main's #1557 is DEC-DASHBOARD-BROWSER-TRUTH-001), the fourth to the then-fresh tail #1598, the fifth to the then-reserved PR-#1275 band number #1610, and the final hop to #1617 = main-max(#1616, DEC-SWEEP-DATA-EDGE-GUARDS-001) + 1 on merge round 5 of the 2026-07-08 conflict-resolution treadmill) — Phase-3
  "results depth" for the one-click institutional sweep, delivered as ONE fenced RUN-END post-hoc pass
  (`brutex/engine/results_depth_pass.py`) that re-simulates ONLY the persisted finalists on the pure-Python oracle
  (per-combo PARITY TOOTH vs the stored row — a mismatch is counted + excluded, never a silently wrong trade list),
  captures every trade with exit reason + real fill prices (additive `TradeSimWalker(detail_sink=)`, default `None`
  byte-identical), 1-minute-path MAE/MFE + intraday-path equity (R22; honest `no_1m_data` when 1m is absent), VIX
  stamps via the FIRST production `TradeReferenceStamper.from_vix_bars` construction (R31; NULL + counted when the
  UNVERIFIED VIX series is absent), folds per-combo aggregates + regime/VIX-band/entry-hour/event lenses (R46/R51 —
  event flags are ONLY the four calendar-verifiable classes; weekday Budget/election days honestly untagged), writes
  the deterministic per-tuple sidecar trio + stamp under the per-run `sweep_runs/<run_id>/results-depth/` subtree
  (gap-register Ruling 4 — a SIBLING root beside the `**/*.parquet`-globbed sweeps tree, built EXCLUSIVELY through
  the ONE `trade_store.sweep_run_dir` seam; idempotent stamp-skip resume, R42), buy-&-hold baselines from the
  sweep's own bars (R27), groups behavioral twin
  families (R34), and builds the global cross-bucket top-100 artifact at the binding contract path
  `sweep_runs/<grid_identity>/top100.json` + stage stamp (R21/R26 — `load_top100` O(1) by construction, ≤100
  entries; any damage → None → deterministic rebuild to identical bytes). The R30 preset law lives in ONE home
  `brutex/ranking/presets.py` (`min_dd_first` default = smallest max-drawdown → composite tiebreak, None-after-scored
  → fewest consecutive losses → run_id ASC; alternates composite/PF/win%/Calmar DESC on the RAW value;
  `top100_artifact` DELEGATES via `sort_by_preset`, byte-identical order — its golden passes unmodified); R33 ships
  as the FIXED hand-written 60-entry `brutex/ranking/phrases.py` table (import-time cross-pin vs `NAME_TO_BIT`,
  golden-digest-locked wording, O(k) mechanical assembly — never generated text); R47 ships as the refusal-only
  `brutex/engine/margin_history.py` regime table (the DEC-ETC-REGIME-HISTORY-001 template — ZERO verified SPAN rows
  exist anywhere, so every winner honestly reads `margin_roi: unverified`; override paths are DATA only). WP6
  wiring: ONE comment-fenced CLI adapter in `cmd_sweep_all` — the fail-loud `BRUTEX_RESULTS_DEPTH` kill switch
  resolves EAGERLY pre-sweep (junk → clean `EXIT_USER_ERROR` before any compute), the pass runs post-cascade wired
  to the WP3 providers (phrases reasons + margin ROI at a 1-lot SIZING hypothetical — the refusal-only mechanism is
  all-UNVERIFIED regardless, never a trade-row value), reference bars (per-instrument 1m/1day +
  `NSE-INDIAVIX` 1m) load best-effort through the SAME Catalog/ReplayFeed path, and a pass failure prints LOUD +
  logs `cli.results_depth.pass_failed` but NEVER fails the sweep — integration-proven over a REAL lake through
  `cmd_sweep_all` (post-cascade order pinned, artifacts + `reasons_wired`/`margin_roi_wired` flags, kill switch,
  junk env, fence). HONEST: the pass is O(persisted finalists) oracle re-sims at run-end, NOT free and NOT on the
  ~33B-trial hot path (which receives ZERO diffs — the frozen `_GOLDEN_CONTENT_HASH`/`_GOLDEN_RUN_ID` pins pass
  UNMODIFIED); trade pages are O(page) with a disclosed O(row-groups-up-to-offset) deep-offset scan (the db_view
  envelope); the parallel WP3 session shipped nothing, so WP3 was repaired inside WP6 (ledgered #1617); the
  contracts memory-file additive-key update (`trade_seq`/`path_1m_status`/`mae_granularity` + the top100 object
  root) is a coordinator obligation. RULINGS-CONFORMANCE AMENDMENT (2026-07-08, same branch — the gap-register
  coordinator rulings folded in pre-merge, ledgered in #1617): Ruling 4 — every run-end artifact relocated to the
  `sweep_runs/` SIBLING root via the ONE `sweep_run_dir` seam (the `<sweeps>/results-depth/` + `<sweeps>/runs/`
  layouts SUPERSEDED; branch-introduced goldens regenerated, origin/main goldens untouched); Ruling 18 —
  `lots`/`costs_paisa` NULL-BY-DESIGN `None` until P5's futures translation sidecar (the fabricated `1`/`0`
  constants REMOVED); Ruling 5 — `ResultsStore.load` wraps EVERY decode failure into the NEW typed
  `ResultsStoreSchemaError` naming the store path (G14) + presence-tolerant optional-column reads defaulting `None`
  (the WindowLedgerRecord precedent), guarded by the NEW frozen pre-extension golden fixture; Ruling 1 — top-1000
  storage-side pins (no silent cap, tie law intact, flush/load round trip); Ruling 12 — NEW
  `brutex.ranking.gate_reasons`, the reason-returning twin of `passes_gates` over the STABLE `GATE_REASON_ORDER`
  wire vocabulary (fail-closed `non_finite_input`; property-pinned `passes_gates(m) == (gate_reasons(m) == ())`;
  P2 folds the per-door counters); Ruling 10 — `/api/v1/sweep/leaderboard` gains the OPTIONAL `?preset=` token
  (absent/blank = legacy ordering byte-identical; known token orders via the ONE presets law with non-persisted
  axes surfaced as `preset_inert_axes`, never faked; unknown = clean 400) + the C3 `store_error_payload` degrade
  (typed store fault → honest 200 error markers, never a 500/blank illusion); Ruling 7 — capped
  `results_depth.vix_miss` WARNs (cap 20, misses stay counted) + the integration proof the CLI's NSE-INDIAVIX lake
  coordinate is REAL (production-writer bars found + every captured trade stamped); the lake-read bypass allowlist
  admits `trade_store`/`results_depth_pass` with explicit not-lake-bars reasons; `minute_path` max-dd pct now the
  honest INDEPENDENT max ratio (review NIT (a)); baseline JSON `allow_nan=False`. ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change; every
  new/touched module 100% line+branch; ruff/ruff-format clean, `mypy --strict` clean + pyright 0 new errors vs the
  origin/main baseline).
  Earlier: `claude/data-edge-guards` (**DEC-SWEEP-DATA-EDGE-GUARDS-001, MIGRATION.md #1616** — two
  surgical engine data-edge guards for sweep-result CORRECTNESS (lens-D of the one-click-sweep gap register):
  (1) a Muhurat-safe prev-day anchor at the Stage-1A roll seam (`brutex/engine/stage1a.py::_roll_to_new_day`
  rolled `_prev_day_ohlc = self._today_ohlc` on every IST-date change with NO calendar guard, so a Diwali
  Muhurat evening session's ~1-hour OHLC became the NEXT regular day's pivot / PDH-PDL / Fib / day-type anchor,
  bits 6-29 + 48-51, poisoning 6 days across 2020-2025 — 2020-11-14 / 2021-11-04 / 2022-10-24 / 2023-11-12 /
  2024-11-01 / 2025-10-21 — with zero warnings; the fix leaves BOTH `_prev_day_ohlc` AND `_daily_history`
  untouched when the just-completed day is a `calendars/nse.has_muhurat_session` date, so the prior REGULAR
  session's anchor carries through exactly as `indicators/prev_day_ohlc.lookup_prev_day_ohlc` + the weekly Fib
  anchors already skip Muhurat sessions, plus ONE `stage1a.muhurat_prev_day_anchor_skip` WARN per skipped
  session; look-ahead-clean, parity-pinned across all 6 real boundaries); and (2) a bounded bad-tick screen at
  the engine read boundary — NEW `brutex/data/bad_tick.py` (`is_bad_tick` pure predicate + `BadTickScreen`
  stateful single-pass screen + `bad_tick_screen_enabled` kill-switch reader) wired into
  `brutex/feed/replay.py::arrow_table_to_bars` (the ONE sweep-feed choke point:
  `ReplayFeed._load_bars` → `Catalog.get_bars` → `arrow_table_to_bars`) that SKIPS-and-COUNTS a self-consistent
  SCALED bar (close `>= 4x` OR `<= 1/4x` the previous IN-SESSION kept close — the `x10` decimal-shift /
  fat-finger class that passes `price_sanity`'s positivity + ordering checks), first bar of each session exempt
  (no reopen-gap false trip), a skipped spike never advances the baseline (a lone spike cannot cascade-flag its
  healthy successor), exactly one `feed.bad_tick_skipped` WARN per session (flushed at the session boundary /
  stream end with the total skipped count + first offender), default-ON `BRUTEX_BAD_TICK_SCREEN` kill switch,
  O(1)/bar, never value-editing / never fabricating. THRESHOLD 4.0 derived from NSE circuit-breaker physics
  (index-wide 10/15/20% daily halt bands → single-MINUTE moves are a small fraction, well under a couple of
  percent; the largest single-DAY COVID-2020 NIFTY moves were ≈ −13%/+9%; an order-of-magnitude ×10/÷10
  corruption is ratio 10.0/0.1, so 4.0 sits ~2 orders above real single-minute returns yet below the ×10 class;
  regular-day bars move `<< 0.25x`/minute so every golden is byte-identical). HONEST LIMITS: the Muhurat guard
  fixes the prev-day ANCHOR roll — a ROLLING_WEEKLY/WEEKLY_STATIC sweep still hits the weekly resolver's
  PRE-EXISTING non-trading-`today` refusal on a Muhurat bar-day (separate; PREV_DAY is the sweep default and is
  fully fixed); the bad-tick screen is a heuristic bounded-ratio guard (a sub-4× ×2 scaling is deliberately not
  caught; a corrupt FIRST bar of a session is not screened by this guard). ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; F&O lake REFERENCE_ONLY; no scope / 52→60-vocab / rate-cap / lake-schema / stored-bar
  change; `stage1a.py` + `bad_tick.py` + `replay.py` ALL 100% line+branch; ruff/ruff-format/`mypy
  --strict`/pyright clean; the P-06 look-ahead + suffix-independence property suites pass UNMODIFIED.)
  Earlier: `claude/peaceful-hamilton-kyeu07` (**DEC-VIX-STAMP-WIRE-001, MIGRATION.md #1645** — the
  dormant India-VIX trade stamping WOKEN end-to-end: the lake's `NSE/INDEX/INDIAVIX` series (1-minute ∪ derived 1-day,
  ONE window-bounded read per launch via NEW `brutex/engine/lake_slice.py::load_lake_slice` +
  `brutex/engine/reference_wire.py::build_vix_stamper`) stamps `vix_at_entry`/`vix_at_exit` on EVERY OBSERVABLE production trade, and
  persisted finalist rows gain the derived nullable `vix_mean`/`vix_min`/`vix_max` trio (`aggregate_trade_vix`, the
  ONE fsum-overflow-safe fold; results_store's PUBLIC `VIX_FIELDS` is the one cross-module column source) — VIX stays
  REFERENCE ONLY: never in the 60-vocab, never in `_RANKABLE_FIELDS`, never in the BLAKE3 run identity; a missing VIX
  bar stamps `None`, NEVER fabricated. The stamper seam is threaded/bound across every production trade-sim site
  (`ResidentTradeSim.reference_stamper` + the hoisted Rust-batch post-stamp converter — kernel byte-untouched; spawn
  pools carry a picklable stamper, sequential ≡ parallel bytes pinned); the ONLY unstamped trade streams are the FIVE
  named DISCARDED/unobservable streams (§2 Rule 3), each an explicit literal `reference_stamper=None` enumerated by
  the exact-count AST pin (`tests/unit/test_vix_stamper_binding_pin.py`) that also REFUSES silently-omitted kwargs +
  positively pins the entry-point binds AND the once-per-launch builder-call counts. The trio survives redo honestly:
  None→value backfills, a value-vs-value clash raises the typed tuple-skip `VixTrioConflictError` (the VIX lake moved,
  NOT non-determinism — grid drivers tuple-skip, stage-3 cell-skips), and `sweep-verify --recount` stamps like the
  launcher + attributes a VIX-only divergence as the non-corrupt `recount_vix_divergence` (moved/backfill/pre-VIX-
  schema shapes; the sentinel/sidecar re-pair gates are `dataclasses.fields()`-driven — drift-proof, never a hand
  list). Round-6 review fixes: the cascade candidate re-simulation's stamps were provably DISCARDED at the
  CandidateInput scalar conversion (report bytes pinned identical either way), so the cascade chain's dead stamper
  seam was DELETED along with the two manual-replay `vix_stamper_for_manifest` builds that paid a full window-bounded
  VIX lake read per `brutex cascade` run / dashboard click for zero observable output (the fifth sanctioned
  literal-None site now lives inside `build_candidate_input`; revert lever = add a VIX surface to the cascade report
  FIRST); miss-WARNs are memoised + capped (cap+1 log lines, exact `miss_count` tally). ENGINE semantics UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate-cap change; touched modules 100% line+branch; ruff/ruff-format clean,
  `mypy --strict` clean, pyright 0 new vs the pre-change baseline).
  Earlier: `claude/jolly-tesla-miygwt` (**DEC-MUTATION-RUNTIME-003, MIGRATION.md #1597** — the weekly deep
  mutation gate made GREEN end-to-end: both cargo-mutants jobs restructured onto sharded matrices (6 fno-math +
  7 trading-core shards; worst shard 65 min vs the 120 min job cap), trading-core WIDENED to evaluator+python+spec
  under `--features python,serde` (the Golden-Rule-#4 run-identity `src/spec.rs` + `src/python.rs` now gated), the
  python mutmut per-module loop wedge-proofed (a timeout-wrapped `rate_common` runner + fail-fast audit tests) and
  fully DE-SURVIVORED — compute 354 · audit 254 · rate_limiter 165 · store 146 · partition 108 · parser 33 · spec 91 ·
  rate_common 369 mutants, ALL zero-survivor; latent main-side debt killed: 2 rate_limiter sentinel + 3 partition
  probe-order + 70 audit survivors — plus timeout-vs-survivor HONEST alert titles on the fan-in verdict job.
  Rehearsal run 28922792812 fully green 15/15 (python loop 68m52s, worst Rust shard 65m). HONEST: the COMPLETION
  PROOF is the first green SCHEDULED `mutation.yml` run (next 2026-07-13 04:00 UTC) — STANDARDS row 3 stays 🟡 until
  it lands, never claimed early. `rate_common` is GATED, its ceilings/floors/governor values byte-untouched (§2
  Rule 6); ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate-cap change).
  Earlier: `claude/serene-euler-y5ra0z` (**DEC-BACKUP-OFFBOX-001, MIGRATION.md #1596** — automated
  incremental off-box backup: restic → S3 Object-Lock / B2 immutable over the fail-safe WHOLE-ROOT include set
  (`<lake_root>` + `~/.brutex/audit`; `.quarantine/` INCLUDED per DEC-LAKE-HEAL-001 #1175), NEW `brutex/backup/`
  package + `brutex backup {run,check,restore-drill,status}` CLI, the O(1) dead-man freshness check (CRITICAL
  Telegram + durable re-alert throttle + daily deep remote cross-check + healthchecks ping lane), the monthly
  byte-verified restore drill (canary + stratified sample + audit byte-PREFIX law), 3 golden-pinned launchd
  templates rendered by `schedule_render.py`, `docs/runbooks/backup-offbox.md` (12-step install + cron fallback +
  live negative dead-man drill) + ADR-0017 manual-acceptance register. Ops tooling ONLY — ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; F&O lake stays REFERENCE_ONLY; no scope/vocab/rate-cap change).
  Earlier: `claude/blissful-curie-gp94rk` (**the 11-item POLISH PACK, MIGRATION.md #1582–#1592** —
  DEC-SESSION-CLOSE-BOUNDARY-PIN-001 (the at-close `>` vs `>=` divergence ruled deliberate + drift-pinned) ·
  DEC-DAILY-CUTOFF-GROUNDED-001 (the #1401 M1 session-close-cutoff regression repaired + the data-grounded
  DEFER-only daily cutoff, `BRUTEX_DAILY_CUTOFF_GROUNDED`) · DEC-NTM-REVALIDATE-CROSSPROC-001 (cross-process
  flock single-flight for the NTM SWR refresh) · DEC-RETRY-COUNTER-SPLIT-001 (`in_pull_fetches` split into
  persisted `empty_retries`/`transient_retries`, old rows load `None`; accepts the #924/#1355 twice-declined
  follow-up) · DEC-GROWW-EPOCH-VERDICT-001 (deterministic epoch-interpretation verdict WARN + live-probe
  runbook; parsing byte-unchanged, Unix-UTC stays UNVERIFIED) · DEC-CLI-DEPTH-FLOORS-001 (CLI fetch wires the
  vendor-depth-floor learner + learned-floor planning clamp, shared schema_meta sidecar with the dashboard) ·
  DEC-ACCOUNT-LEASE-CROSSHOST-001 (advisory cross-host heartbeat pull lease on shared lake storage — detection,
  NOT mutual exclusion; `BRUTEX_PULL_LEASE=off`) · DEC-DERIVE-LOCK-VISIBILITY-001 (movers PrebuildGate honors
  the cross-process derive lock, `source="cross_process"`) · DEC-RESUME-BANNER-AUTOCLEAR-001 (the resume red
  banner auto-clears on the next real pull launch that actually STARTS — started-gated
  `clear_banner_after_start`, a refused start clears nothing) · DEC-QUOTA-VERDICT-MOOT-001 (the quota phone-alert
  follow-up closed as MOOT + the dead informational quota surface deleted, enforcement seam kept dormant) ·
  DEC-DAILY-OPEN-PREDICATE-ALIGN-001 (#1592 — the two daily-open-substitution surfaces share the ONE
  per-session predicate `spot_daily_resample.classify_daily_open`: Muhurat abstains on BOTH, `!=` 09:15 on
  BOTH — the /completeness census's strict-`>` flips to `<>` + a `MUHURAT_SESSIONS` NOT-IN exclusion, its SQL
  CTE mirror-pinned equivalent to the classifier; the remaining divergence is POPULATION (forming-session
  cutoff) not predicate, documented + pinned; closes the #1267/#1232 named follow-up BY IMPLEMENTATION and
  corrects #1582's wontfix misattribution).
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate-cap change; touched modules 100% line+branch;
  ruff/ruff-format clean, `mypy --strict` + pyright at the pre-change baseline).
  Earlier: `claude/upbeat-tesla-mfrovq` (**DEC-RATE-METER-LIFECYCLE-001, MIGRATION.md #1593** — the #1056
  pull-rate CallMeter LIFECYCLE + rate-widget truth pack, the 96-agent hostile-audit fixes as ONE observe-only change
  set (ZERO pacing change, fetch behavior byte-identical): (a) C0 — NEW `SessionRateController.clear_meter` fired by
  `BackgroundPullJob(on_run_end=…)` inside the identity-gated finally (after `on_finally`, own try/except, before the
  single-flight release; job-lock → controller-lock order pinned), PLUS the OFF-job `pull_candles` spine pairing its
  own build-site registration with an identity-gated clear (round 4 — `clear_meter(expected=)` compare-and-clear via
  `_pull_meter_scope`, success AND raise; supersedes round 2's route-level unconditional `finally` clear on
  `POST /api/v1/universe/pull`, which a round-4 reviewer REPRODUCED evicting a concurrent JOB pull's LIVE meter
  whenever the route 400'd before `pull_candles` ever registered — registration is once per phase START, so the
  job's F&O phase ran meterless and an issued-but-all-failing pull read FLOWING again) so "no in-process pull
  running ⇒ no meter registered" is a process invariant covering every registration path — a dead pull's meter is
  unreadable in every later process state, a raise-before-register clears nothing, and a concurrent pull's later
  registration is never evicted; the intra-pull
  `register_meter(None)`-keeps-prior guard byte-unchanged + re-pinned. (b) the dry-run finding —
  `register_pull_meter(…, dry_run=)` clears-and-never-registers at ALL FOUR build sites (the metadata/F&O dry-run
  GrowwClients carry a REAL unpaced CallMeter, `client.py` `default_factory=CallMeter`, so the #1056 "no `.meter`
  silent no-op" in-code/ledger claims were FALSE and are rewritten); a warm-process dry-run reads the same honest
  `meter_present=False` as a cold one, never a fabricated MEASURED "flowing"; round 2 — the direct route's dry-run
  arm passes `session_rate=None`, so an operator's concurrent `?dry_run=true` on the off-job route can never evict a
  LIVE pull's registered meter; round 4 — the gate's clear stays UNCONDITIONAL (no handle to compare) and its
  reverse blast radius is now honestly DISCLOSED, not denied: a JOB dry-run started while a direct off-job REAL pull
  is in flight evicts that pull's meter for its remainder (degrades to `meter_present=False`, never fabricates — the
  false "no live pull can be concurrent with it in-process" premise rewritten, round-4 MINOR x2). (c) C1/C2 — per-spine mutant-killing
  wiring tests drive the REAL `pull_all_spot`/`pull_candles`/`pull_expiries_contracts`/`pull_contract_candles` and
  assert `peek_meter() is` the exact injected CallMeter + dry-run eviction twins (every severed site / dropped kwarg
  now fails a named test; each mutant re-applied + captured failing). (d) C5 — `BackgroundPullJob.start(job_kind=)`
  (`JOB_KIND_PULL`/`JOB_KIND_SWEEP`) + the atomic `running_job_kind` property; the `/api/v1/pull/rate` probe is
  kind-aware so a 2-4-day dashboard sweep reads honest idle, never a site-wide red "stalled";
  `_pull_active_probe`/`_pull_live` DELIBERATELY stay sweep-INCLUSIVE (the #920 duty-degrade floors still engage —
  comment-pinned + regression-tested). (e) C3 — `BRIDGE_MAX_AGE_S=900.0` in `build_pull_rate_view_merged`: a bridge
  older than 15 min (or non-finite-aged) is DEAD EVIDENCE ≡ absent, so a SIGKILLed CLI's stuck `pull_active=True`
  stamp can't fabricate a permanent red on an idle dashboard; `<=` keeps the 12 s-900 s died-mid-run alert window;
  fresh-bridge projection pinned field-for-field byte-identical. (f) C4 — bx-rate-monitor.js's false "obs rendered
  beside it" claim rewritten (`req_per_hour_observed` is wire-only back-compat, deliberately NOT rendered) + the dead
  `.bxrm-obs` CSS deleted. (g) C6 — six stale pre-#1056 doc passages + `register_pull_meter`'s "silent NO-OP"
  docstring rewritten to registered-meter-lifecycle semantics; `build_app` gains an injectable `session_rate` test
  seam (default byte-identical, pinned); round 3 — the falsified "no job running ⇒ no meter registered" over-claim
  swept from its three remaining committed sites (`rate_sampler` ×2 + `pull_jobs`' `on_run_end` doc) to the scoped
  "no in-process pull running" law + a grep doc-pin (`TestMeterLifecycleInvariantDocPin`) over every
  `brutex/dashboard/*.py` so it can never re-drift. HONEST: the sampler stays contractually observe-only; a dry-run <60 s after
  a real pull can still read the decaying `recent_minute` as "flowing" ≤60 s (admission fallback — documented, not
  mechanised); the job-finish clear can lose to at most ONE final ≤2 s sampler snapshot (pull_active flips False in
  the same path); a wedged-but-alive CLI beyond 900 s also decays to idle (Telegram pull-health + the pull journal
  remain the persistent alarm); the CLI bridge still carries no `failed_rps` (#1211 named follow-up). ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules 100% line+branch; ruff/ruff-format/`mypy --strict`/`pyright`
  clean).
  Earlier: `claude/sleepy-hawking-l87497` (**DEC-FORM-WINNER-PERSIST-001,
  MIGRATION.md #1581** — the durable `&`-form winner map: `schema_meta` rows
  (`groww.form_winner.<key>` → the KEY-level winning form STRING — the winner's
  ROLE applied to the key's own `underlying_symbol_forms` tuple, so the metadata
  and candle phases share ONE value space; `underlying_symbol_forms` moved into
  `symbol_form_preference.py`, `contracts.py` re-exports byte-compatibly) via the
  NEW `PersistentSymbolFormPreference` over the shared `brutex/data/meta_kv.py`
  `MetaKV` Protocol (the `vendor_floor` durable-learned-value pattern;
  `VendorFloorMetaStore` aliased); MULTI-form keys only on BOTH paths (the ~750
  plain underlyings pay ZERO store I/O — order AND record); the seed `get_meta`
  runs OUTSIDE the map lock under the COMMON `run_with_deadline` bound (2 s,
  `BRUTEX_FORM_WINNER_READ_DEADLINE_S`) so a wedged sidecar read can never stall
  the fan-out; the `preference_key` PRIMARY-underlying normalization on
  `resolve_expiries`/`resolve_contracts`; ONE shared instance wired per pipeline run
  across the metadata + historical phases (the spine's fresh-per-pass default stands
  down); the `brutex doctor --live --ampersand-probe` stage-14 probe TOOLING whose
  decisive run is the ONLY writer of the `groww.form_probe.verdict` stamp — the
  stage's `seeded` count is READ-BACK-VERIFIED (a kill-switched or failed winner
  write is never claimed; `amp.seed_shortfall` names the cause) and a
  data-beside-a-RAISED-sibling name reads `*_UNCONFIRMED` (seeds its winner, never
  counts CLEAR/decisive — a throttled side is not a refuted side). HONEST:
  the live probe was NOT run here (no creds in the build environment) — the #898
  `%26` UNVERIFIED wording stands byte-intact; the stamp flips it only with a real
  transcript. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change;
  touched modules 100% line+branch; ruff/ruff-format clean, mypy `--strict` clean,
  pyright at the pre-change baseline).
  Earlier: `claude/peaceful-wright-r7l8l9` (**DEC-FIB-BULLISH-RETRACE-001, MIGRATION.md #1560** — the
  bullish Fibonacci retracement fix: `FibLevels.get_by_bullish_label` had aliased the bullish 23/38/62/78 labels onto
  the REVERSED bearish (PDH-anchored) fields, but Fib ratios are asymmetric (0.236 + 0.786 = 1.022 ≠ 1.000) — bullish
  23/78 were wrong by 0.022×range (565 paisa on the NIFTY 07-May-2026 golden anchor; the 38/62 numeric coincidence
  `0.382 + 0.618 = 1.000` is an IEEE observation, never relied on). The shared `rust/trading-core` kernel
  (DEC-SHARED-CORE-001 — Python grows NO math copy) gains `retrace_bull_{23,38,62,78}_paisa = round_half_even(pdl +
  ratio·range)` token-mirrored to the bearish lines (NO bull_50 — the bullish midpoint IS the shared exact-integer
  `retrace_50`; 0%/100% = pdl/pdh; extensions stay `ext_up_*`); the `compute_fib_levels` FFI projection grows 15→19
  (NEW `FIB_COMPUTE_LIST_LEN`, original 15 order untouched, bull tail APPENDED last) while `FIB_LEVELS_LEN` stays 15 —
  the FROZEN `evaluate_with_levels` evaluator ABI (an accidental 15-tuple pun, now two documented distinct constants;
  the evaluator zeroes + never reads the bull fields). The catching test's unconditional `pytest.skip` REMOVED
  (bullish 78% chart-exact 2_425_800 paisa; frozen kernel goldens raw[15:] == [2_411_665, 2_415_417, 2_421_483,
  2_425_800]); the module docstring's FALSE "verified bit-exact" bullish claim corrected honestly (78 chart-verified;
  23/38/62 formula-derived kernel-frozen, NOT screenshot-verified — Golden Rule #10); NEW Rust proptest
  `bullish_retracements_are_not_bearish_aliases` (the asymmetry teeth killing a revert-to-alias mutant) + the bull
  ladder ordering pin; post-round-1-review TWO native `fib.rs` exact-value unit pins added
  (`bullish_retracement_exact_values_nifty_anchor` + `bullish_bearish_asymmetry_gap_exact_banknifty_anchor`) plus the
  15/17→21 stale level-count docstring reconciles (`indicators/__init__.py`, `weekly_anchors.py`,
  `fib_definitions.py`). Byte-identity + NO indicator_version bump — the recorded decision:
  `get_by_bullish_label`/`FibDirection` have ZERO production consumers (census: tests only), the `near_fib_236/786`
  bits read the untouched bearish fields, tests/golden incl. the frozen 400-case vocab_golden_vectors.json pass with
  the fixture byte-untouched (`git diff origin/main -- tests/golden/fixtures/` EMPTY). ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate-cap change; `fib_levels.py` 100% line+branch; ruff/ruff-format clean,
  `mypy --strict` clean, pyright at the pre-change baseline).
  Earlier: `claude/wonderful-wright-17gyqj` (**DEC-FUTURES-EXEC-TRANSLATION-001, MIGRATION.md #1543** —
  the futures translation executor, Phase 5 of the one-click sweep law (R13/R14/R15) — the options executor's futures
  twin, a NEW POST-sweep translation layer: an INDEX winner's trades are re-priced on the FRONT-MONTH-AT-TRADE-TIME
  futures contract (nearest UNEXPIRED monthly as of each trade's ENTRY date, resolved from the REAL `registry.duckdb`
  calendar via the SHARED `movers_futures.select_near_month` — NEVER a computed last-Thursday formula; graceful
  lake-partition-discovery fallback; expiry-day trades stay on the EXPIRING contract, post-expiry tail → M+1, M/M+1
  co-trading = deterministic earliest-expiry-≥-entry, never liquidity-picked). NEW `brutex/futures/` (executor ·
  contract_resolver · lake_source · preflight · reference_sim · leaderboard) + `brutex/costs/futures_costs.py`. R15
  fills are OPTIMISTIC = PRECISE ONLY — minute-bar OPEN ± one tick ADVERSE per side (the LONG arm golden-pinned
  byte-equal to `calculator._apply_slippage(...,"long")`; the SHORT arm computed adverse-both-sides, NOT inheriting
  the documented `# CHECK SIGN` ambiguity), gap-opens fill at the REAL open (never an SL/TP fantasy), volume-0
  PRINTED bars are REAL + TRADABLE (STALE_BAR strictly opt-in), a missing entry/exit minute is a counted NO_DATA
  (never a nearest-bar substitute), the pessimistic/worst leg is DEFERRED — structurally absent. PINNED reject order
  lot_size → cost_regime → no_contract → no_data → stale_bar with both pure DATE gates before any registry/lake I/O
  (never-called-seam pinned); count-dict tally projected 1:1 onto `n_rejected_*` (no catch-all). COSTS are
  VERIFIED-rates-only (the DEC-ETC-REGIME-HISTORY-001 refusal precedent, NO runtime escape hatch): Groww futures
  brokerage min(₹20, 0.03%×turnover) (PLAN_FINALIZATION §8.1 row 2) + futures STT 0.02% eff. 2024-10-01 / 0.05% eff.
  2026-04-01 (TAX_HANDLING §8.1 + DEC-COST-003) are encoded; EVERY futures-ETC window + NSE futures IPFT + futures
  stamp + pre-2024-10-01 futures STT ship as rate-less REFUSAL rows (figures live in source TEXT only — the §14
  proposal block is not a verification) ⇒ a cost-bearing stock future counts COST_REGIME_UNVERIFIED (exercised via
  the SYNTHETIC cited-regime/lot test fixture — no NATURAL input reaches the gate today: index futures are cost-free
  and skip it, a natural stock future trips LOT_SIZE_UNKNOWN first; the designed forward path, naturally reachable
  only once a cost-bearing stock-futures scope decree + cited RegimeRows land); INDEX futures
  are COST-FREE (DEC-COST-SCOPE-INDEX-SIGNAL-ONLY-001 — net == gross; the ±tick is R15 fill realism, not a statutory
  cost). Lots via the ONE date-keyed derivative-lot history (2020-dated / stock signals ⇒ counted LOT_SIZE_UNKNOWN,
  never invented). STOCK winners → ExecutionKind.STOCK_SPOT_DEFERRED with an honest note NAMING the future scope
  decree (R11); routing lives ABOVE the instrument-agnostic executor. R22 MAE/MFE from the contract's OWN minute
  path; R31 VIX passthrough (missing = honest None, counted); R45 differential vs an independent naive
  Decimal-pipeline reference (AST-pinned to share only the primitive readers; run_id-seeded deterministic sample;
  mismatch ⇒ SUSPECT + loud); R42 content-hash-stamped atomic sidecar (an UNKEYED sha256 integrity checksum —
  corrupted/torn fails its stamp, rebuilt byte-identical; NOT tamper-evidence: no secret key, a rewriter could
  restamp); R4 preflight disk-verifies each trade month's front-month partition(s) across the roll seam and
  refuses naming exact months — no Phase-1 file edited (the pure helper is exposed for the shim). ROLL-9
  suffix-independence property-gated (fills+MAE/MFE bit-identical under absent/mutated/extreme post-exit suffixes);
  R13/R30 rank-independence property-gated (annotation additive-only under ONE `futures_exec` key). O(1) honest
  (R29): per-trade resolve+fill flat vs signal AND contract count (bench-pinned at the `_o1_tiers` ceiling); first
  loads O(contracts)/O(bars-in-contract) + MAE/MFE O(bars-in-trade) STATED, never hidden. HONEST: NOT yet wired into
  `cmd_sweep_all`/the dashboard (deliberate no-existing-file-edit boundary — the orchestrator binds later); R47
  margin-ROI not built (no in-repo date-keyed SPAN history — nothing rendered, nothing invented). ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; options path untouched (R12); F&O lake REFERENCE_ONLY; no scope/vocab/rate change; 197
  tests (unit/integration/property/golden/bench/chaos), all 8 new modules 100.00% line+branch (760/194);
  ruff/ruff-format clean; `mypy --strict` clean; pyright at the 38-error pre-change baseline, 0 new).
  Earlier: `claude/stoic-davinci-7c7xp0` (**DEC-TRACK2-COSTS-SPEC-RECONCILE-001 +
  DEC-BROKER-DOCS-AWSONLY-RECONCILE-001 + DEC-GPU-GATE-RECONCILE-001 + DEC-CLOUD-AUTOFIX-RETIRE-001 +
  DEC-PHASE1-SIGNOFF-RECONCILE-001, MIGRATION.md #1552–#1556** — the doc/spec/ops debt reconciliation
  pack, DOCS-first: the Track-2 short-side slippage contradiction resolved to the §8 ADVERSE rule
  (spec + the production-unreachable `_apply_slippage` short SELL sign + byte-exact test re-pinned;
  "# CHECK SIGN" deleted; §4.2 expiry machine labelled a fail-loud STUB in-spec, its two pseudo-code
  drifts registered as §16 items 18/19; BUY-only DEC-EXEC-008 + DEC-RISK-011 locks untouched); the
  broker token/credential docs (runbooks + deploy guide + minter + `TOKEN_BROKER_CONTRACT.md` +
  ADR-0013/0014 status notes + scoreboard cell) refreshed to the AWS-only / fail-fast /
  mid-run-re-read truth (#1065/#1072/#1092 — docs-only, rate caps untouched); ADR-0004 GPU gate
  reconciled (PROPOSED→ACCEPTED, §6 benchmark retired-as-scheduled + retained as a dormant
  fail-closed precondition, NEW drift pin `tests/unit/test_gpu_gate_drift_pin.py`); the dormant
  cloud auto-fix snippet RETIRED (DELETE) + the redteam sweep extended to comment-family triggers;
  the Phase-1 sign-off checklist closed by supersession (11 boxes, honest no-backdate clause).
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change; touched costs module
  stays 100% line+branch).
  Earlier: `claude/zen-meitner-8780fo` (**DEC-RUN-IDENTITY-GUARD-PARITY-001 +
  DEC-OPTION-DEDUP-BLOOM-VERIFY-PIN-001 + DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 +
  DEC-TRADE-SIM-U128-KERNEL-001 + DEC-EXECUTION-TRIO-DELETE-001, MIGRATION.md #1546–#1550 (renumbered from #1541–#1545 after PR #1234 took #1541)** — the
  5-item engine cleanup pack (reapplied onto current main after the original lineage was wiped):
  (1) the combo `\x1f` reject-only guard mirrored into BOTH
  previously-unguarded run-identity joins via ONE COMMON `strategies.combo.reject_unit_separator`
  (+ `allow_nan=False` at the engine canonical-JSON sites incl. the stage1a third mirror) —
  byte-frozen run-identity goldens landed FIRST and pass unchanged (LOW hardening, no live bug);
  (2) the option-dedup bloom-before-exact-confirm ordering VERIFIED already correct — pins +
  docs only, ZERO executable change (AST-proven); the PRODUCTION consumer is the engine sweep's
  `ComboDedup(OptionDedupIndex)` dedup tier (the `OptionBarSink` pin was dropped in the rebase —
  main's #1500 deleted that dead orchestration; the mark-after-durable-write LAW recorded as
  history in #1547);
  (3) the hand-synced Python `VwapState` + `MarketStructureState`/`Trend` twins DELETED — the
  `rust/trading-core` pyclasses are the single source (additive `last_swing_high/low` getters,
  core untouched — TickVault-safe; crate 0.3.0→0.3.1 moves the live `_indicator_version()`
  string with NO math change; twin fixture + SHA never regenerated; P-06 preserved);
  (4) NEW `ResidentSweepSimU128` fno-math kernel via a width-generic `MaskWord` core (existing
  Rust suite passed UNMODIFIED; native `Vec<u128>` FFI per the recorded pyo3 probe; lazy
  wide-kernel promotion; `bar_bits_beyond_u128`/`mask_beyond_u128`/`wheel_missing_u128` +
  `wide_kernel_promoted` observability) — HONEST: future-proofing, production-unreachable at the
  60-bit vocabulary, u64 golden corpus byte-untouched; (5) the deprecated
  `IndianCostModel`/`ExecutionSimulator`/`BacktestDriver` trio DELETED in full (8 test surfaces +
  golden fixture + dead ci.yml gates; `types.py` whole + `errors.py` semantics untouched, five
  subclasses honestly orphaned; ONE atomic commit — the wiped lineage's two-commit shape recorded
  as history in #1550) — the
  pack's ONLY behaviour-visible item. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; run identity
  bit-identical for all currently-legal inputs (golden-proven); no scope/vocab/rate change;
  touched modules 100% line+branch; ruff/ruff-format clean, mypy `--strict` clean, pyright at the
  pre-change baseline; cargo-mutants partial under the ledgered #1511 timeout).
  Earlier: `claude/ecstatic-cannon-o3zbbe` (**DEC-TEST-APPARATUS-DEBTS-001,
  MIGRATION.md #1541** — the test-apparatus debt pack: repo-wide doctest gate (measured
  55-example floor; the 3 broken tf_resampler examples fixed — docstring-only, the sole
  brutex/ source edit) · EXPLICIT hypothesis `ci` profile on CI (GHA was already implicitly
  derandomized by hypothesis's built-in auto-CI profile; the real flip: no-env scheduled arms
  incl. stress-nightly now deliberately random) · the fix-reversion ci-ok hard gate (`bugfix-reversion-check`) · the citation
  ACCURACY scanner (`citation-accuracy-scan`, push+PR; the 29-finding live census reconciled
  to fixpoint in-PR) · the DEC-ID appropriateness heuristic (row 18 stays PARTIAL) · jscpd
  3%→1% (0.37% measured) · the brutex/-wide pandas import ban (zero offenders) · P-10/P-12
  promoted to real cost-model properties (flat ₹20/order brokerage law; stale min() wording
  corrected) · the sweep worker-scaling bench enumerated in bench-m4pro.yml, EXECUTING GREEN
  on the live registered M4 Pro runner (in-PR reconcile: first real run failed on the missing
  polars [data] extra, fixed a54c9a0a; re-run fully green, run 28851211762 — step measured
  20 s vs the generous 25-min timeout; VERDICT: KEEP CAP=8). ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; apparatus/CI/docs only; ruff/ruff-format/mypy --strict/pyright at
  the pre-change baseline).
  Earlier: `claude/eloquent-shannon-bdjnuv` (carrying BOTH movers fixes — folded from
  `claude/trusting-einstein-bobai2`, PR #1246) (**DEC-MOVERS-PREVCLOSE-CONTIGUITY-001,
  MIGRATION.md #1539** — the /movers prev-close calendar-contiguity honesty guard: the spot/futures/options
  previous-day close + futures/options previous-day OI baselines (which silently served the NEAREST-prior lake bar
  with zero calendar check — the confirmed repro: a Fri-hole lake baselined Mon on Thu's close and printed 2.2822%
  where the true figure is 0.2033%) are now judged against the NEW COMMON `calendars/nse.previous_trading_session`
  (strict `is_trading_day_checked` back-walk over weekends/holidays, Budget weekend sessions honoured, coverage-floor
  ABSTAIN with one `calendar.previous_session_coverage_exhausted` warning — never a guessed session; Muhurat day =
  documented NON-session limit [the WALK's stance stands, but the VERDICT layer's false-hole reading of a
  day-after-Diwali Muhurat baseline is SUPERSEDED by DEC-PREVSESSION-MUHURAT-COHERENCE-001, MIGRATION.md #1649b —
  contiguous-True + the EMIT-ON-TRUE `prev_close_muhurat`/`oi_baseline_muhurat` stamp + " †", never " *"/WARN];
  NSE≡BSE so the exchange arg is inert parity today). The enriched resolvers
  `movers_snapshot.resolve_prev_close` → `PrevDayBaseline(value, found_date)` + `movers_futures.resolve_prev_oi` →
  `PrevOiBaseline(value, found_date)` keep byte-identical `.value` seams (`_prev_day_close`/`_prev_day_oi` wrappers —
  every historical pin green unmodified; `_prior_session_last_from_minute` appends its found date, [0]/[1] stable);
  ONE pure `prev_baseline_provenance` yields (session_iso, contiguous|null), stamped as defaulted additive fields on
  MoverSnapshot/FuturesMover/OptionMover with close + OI judged INDEPENDENTLY (the null-OI-1day divergence pinned);
  row projectors EMIT-ON-FALSE so contiguous AND abstained payloads are byte-for-byte the pre-fix wire (golden-pinned
  both arms on all three tabs + a FROZEN additive-key-NAME set); one capped (20/build) per-(symbol,kind)-deduped
  `movers.prev_baseline_noncontiguous` WARN + a single suppressed summary; `movers.html` appends a strict-`=== false`
  " *" marker (pre-esc, the " ~" precedent) + a note legend, substring-presence pinned (honest envelope). HONEST:
  makes the wrong %change VISIBLE — stamped + warned + marked — it does NOT fix the number (re-pulling the missing
  session does; no bar is ever fabricated); `preopen_report._previous_trading_day` was deliberately NOT refactored
  there — the named follow-up DEC-PREOPEN-PREVDAY-COMMON-001, since ADVANCED by main's #1634 and CLOSED by
  MIGRATION.md #1649a, DEC-PREOPEN-PREVDAY-COMMON-002 (the thin-delegator consolidation). READ-ONLY dashboard — ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  no lake/schema/scope/vocab/rate change; movers_snapshot/movers_futures/movers_options/calendars-nse ALL 100%
  line+branch (679-test family run); ruff/ruff-format clean, `mypy --strict` + pyright at the pre-change baseline).
  Earlier (same branch): (**DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001, MIGRATION.md #1538**
  — the `/movers` SPOT "Top Value" (turnover) is now TIMEFRAME-INVARIANT: re-sourced from the RAW 1-minute session
  frame (post as-of cut, PRE-resample) via the NEW `spot_session_value_paisa`, mirroring the already-TF-invariant
  futures/options `_read_future_session`, instead of the resampled display-timeframe bars — so the SAME (symbol, day,
  as-of) turnover no longer CHANGES with the selected display timeframe (reproduced NSE-NIFTY 2024-01-02 whole-day:
  1m 19583300 ≠ 5m 19833500 ≠ 1day 20100000 paisa, now all byte-identical to the raw-1m answer). Three edits ALL in
  `brutex/dashboard/movers_snapshot.py`: extract the shared raw-1m read+cut into `_read_cut_1m_frame` (§11 COMMON,
  `_read_session_bars` a byte-identical wrapper), the NEW no-`interval`-arg `spot_session_value_paisa` (HLC3 × volume
  via the ONE COMMON `session_traded_value_paisa`, `None` on missing 1-minute — never fabricates), and a
  `value_paisa: int | None = None` injection into `compute_snapshot` (`None`/omitted falls back byte-identically, the
  320000/318660 goldens pass UNMODIFIED). HONEST: a SANCTIONED display-value correction on coarse timeframes;
  value(1minute) byte-unchanged; futures/options value BYTE-UNCHANGED; READ-ONLY dashboard ranking; ENGINE UNCHANGED
  — NIFTY/BANKNIFTY/SENSEX; touched module 100% line+branch; ruff/ruff-format/`mypy --strict`/pyright clean.)
  Earlier: `claude/intelligent-euler-dwf2ab` (**DEC-CLI-GREEKS-ENRICH-001, MIGRATION.md #1536** — the CLI
  `brutex fetch` pull path now runs the SAME fenced Greeks-enrichment pass the dashboard runs, and a NEW
  `brutex enrich-greeks` subcommand delivers the #1188 bulk re-enrich — closing the two
  DEC-GREEKS-CORRECTNESS-AUDIT-001 named follow-ups ("a CLI-pulled lake never enriches" + "bulk re-enrich"). ONE
  COMMON wheel-free module `brutex/data/greeks_enrich_pass.py` (the MOVED dashboard fence `run_greeks_enrich_pass` +
  `GreeksEnrichOutcome`/`GREEKS_NOT_RUN` + the wheel-free chain-union lake discovery
  `discover_option_windows`/`lake_target_builder` + the `has_option_candles` pre-gate + the fail-loud
  `resolve_cli_greeks_enrich` env gate); `historical_pull._enrich_greeks_from_minute` is now a thin delegator —
  dashboard behavior BYTE-IDENTICAL (event names, the 13 `done` fields in order, logger name via `log=_LOG`, the halt
  re-raise, outcome types aliased to the SAME objects; the existing dashboard suite passes unmodified). The fetch
  seam is Seam A — post-drain, ledger persisted, single-flight lock still HELD, default-ON
  (`BRUTEX_CLI_GREEKS_ENRICH=0` kill switch; a junk value fails loud pre-pull `EXIT_USER_ERROR`; inert under
  `--dry-run`/`--inspect` via `_no_persist`), scoped pulled-underlyings × pulled-window, deliberately NOT gated on
  `bars_appended > 0` (async-pipeline counts reconcile AFTER the seam); halts alert-then-re-raise via the COMMON
  `emit_pull_halt_alert` (`_one_fetch_pass` pattern), any other failure is fenced and NEVER fails the pull; a
  pure-spot lake skips in O(exchange dirs) with one DEBUG event. `brutex enrich-greeks` (whole lake or
  `--underlying`/`--start`/`--end` slice, dates CLI-inclusive → half-open internally; `--dry-run` = wheel-free
  discovery counts, no lock) takes the pull single-flight lock + `keep_awake` and exits `EXIT_OK` iff `ok is True
  and errored_targets == 0` (the `lake audit-unique` cron-visible precedent — a deliberate, documented divergence
  from the never-fail pull path). Discovery bounds year dirs via the canonical `brutex.data.partition` constants
  (§11 COMMON — imported, never re-minted). Review round-1 fixes folded: discovery's year/month names are
  ASCII-digit-gated (`_is_canonical_digits` — bare `isdigit()` admitted non-decimal Unicode digits like `²024` whose
  `int()` raise killed the whole pass, and silently accepted Arabic-Indic decimals; both now skip per the
  junk-tolerance contract); the fetch seam's `+1 day` widening SATURATES at `date.max` (`_next_day_saturating` — a
  degenerate `--end 9999-12-31` pull previously died on an uncaught post-drain `OverflowError`); `enrich-greeks`
  refuses `--end 9999-12-31` cleanly (`EXIT_USER_ERROR`, never a traceback) and refuses a nonexistent `--lake-root`
  loudly via the COMMON `_require_lake_dir` (previously a green `errored_targets=0` illusion + a materialised
  phantom `.locks/`). Review round-4 fix folded: the `enrich-greeks` clean-done summary echoes `contracts=N` (the
  fenced build's actual target count, captured by wrapping the builder — never a second discovery walk; the `?` leg is
  a DEFENSIVE belt only — with the real pass `ok=True` implies the build already ran and a pre-build failure takes the
  FAILED branch, so it is production-unreachable and guards a future pass-contract change, never a fabricated 0) and
  an explicit `--underlying` matching ZERO stored option contracts
  prints one loud stderr WARNING naming the sorted tokens (matching is exact + case-sensitive — `nifty` never
  matches stored `NIFTY`; exit 0 unchanged, the pass genuinely completed; a whole-lake zero stays quiet) — closing
  the silent-green typo class one level below the `_require_lake_dir` guard. HONEST: NOT O(1) — discovery is
  O(option partition dirs + month entries)
  and the pass re-READS the scoped stored bars every run (`already_current` skips writes/solves, not reads; a
  full-floor fetch over a dashboard-populated lake re-reads the whole stored index-options history inside the lock —
  levers: window scoping, `BRUTEX_CLI_GREEKS_ENRICH=0`, `BRUTEX_GREEKS_WORKERS=1`); the CLI L2 heal seam landed INDEPENDENTLY on main as
  DEC-CLI-LAKE-HEAL-L2-001 (#1493) while this change was in flight — the merge orders this enrich seam strictly
  AFTER that heal (heal in `cmd_fetch`'s drain `finally` before `end_pull_ledger`, enrich after it; a heal-halt
  `LakeWriteHalt` re-raises so the enrich never runs over an un-healed slice — the dashboard's
  heal-L2-BEFORE-Greeks-enrich ordering law holds on the CLI path too); SIX-STEP RECONCILE (DEC-PULL-SIX-STEP-ORDER-001
  #1527, merged in flight): the dashboard call-site law is preserved with main's strict gate BETWEEN heal and enrich —
  `_mark_complete` → `_heal_lake_after_pull` (integrity, ALWAYS runs) → the opt-in `BRUTEX_PIPELINE_STRICT_GATE=1`
  barrier (an unclean prior step / F&O phase WITHHOLDS the pass, `_GREEKS_STRICT_WITHHELD` — now a distinct instance of
  the COMMON `GreeksEnrichOutcome`, identity-keyed so it never collides with `GREEKS_NOT_RUN`) →
  `_enrich_greeks_from_minute` (still the thin delegator into the COMMON fence); the CLI never calls that seam
  (`pull_contract_candles` — `prior_steps=None` covers the OTHER non-pipeline callers: the /db selection route,
  tests) — its Seam A enrich invokes the COMMON fence directly, so the strict gate cannot touch it (the CLI spot
  six-step path is
  main's NAMED follow-up DEC-PULL-SIX-STEP-ORDER-CLI-001, not this change's); `futures_skipped` reads 0 on CLI passes (FUT filtered at discovery —
  documented asymmetry); the bulk lever is manual (no cron). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX, F&O lake
  REFERENCE_ONLY, no scope/vocab/rate change, zero vendor calls added; touched modules 100% line+branch;
  ruff/ruff-format clean, mypy `--strict` + pyright at the pre-change baseline).
  Earlier: `claude/movers-asof-default-latest-minute` (**DEC-MOVERS-ASOF-LATEST-DEFAULT-001, MIGRATION.md #1535** —
  the `/movers` Spot default as-of TIME resolves to the SAME latest available minute across EVERY timeframe, so
  1m ↔ 2m ↔ 3m ↔ 3hour ↔ 1day always show the identical latest close; READ-ONLY dashboard, ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX).
  Earlier: `claude/happy-allen-y29nc6` (**DEC-BENCH-ABS-BASELINE-001,
  MIGRATION.md #1534** — committed per-hardware ABSOLUTE benchmark baseline + fail-loud compare
  gate on bench-m4pro + mechanical no-silent-re-baseline governance: NEW pure-stdlib COMMON core
  `brutex/observability/bench_baseline.py` (schema/comparator/capture/govern + the ONE
  hardware-key format law) + `hardware_envelope.py` gains `cpu_model_string`/`HardwareFingerprint`/
  `hardware_key` (the BRUTEX_* sizing overrides provably cannot move the fingerprint;
  `BRUTEX_BENCH_BASELINE_KEY` is the one explicit, slug-validated key override) + the 6-entry measurement registry
  (`tests/bench/_abs_baseline_registry.py`, best-of-N-MIN per DEC-CANARY-MINTIME-001) + per-hardware
  baselines at `tests/bench/baselines/<key>.json` (`tolerance_pct` capped `[0,100)` + its failure
  multiplier `1 + t/100` float-exactly < 2.0 — a 2x slowdown can NEVER pass; strict-`>` boundary;
  > 4x speedup FAILS) + `scripts/ci/bench_baseline_gate.py`
  (validate/compare/capture/govern/key; 0 ok-or-LOUD-abstain / 1 numeric-retry-once / 2
  structural-never-retried). bench-m4pro gains measure→compare→capture→artifact + the GitHub-hosted
  `bless-baseline` bot-PR job (the ONLY contents/PR-write holder — the self-hosted runner's inherited
  token is issues:write only, for its alert steps; main-ref + schedule/dispatch pinned);
  ci.yml's required `bench` job gains the always-on schema validate + the PR governance diff gate
  (a blessed-cell bump needs a NEW human-written ledger entry quoting the entry key + literal value
  token + a LIVE-verified fresh run). BOOTSTRAP fabrication-free: ZERO numbers ship — the first
  baseline is measured on the real M4 Pro and blessed via one operator nudge+merge; until then
  compare ABSTAINS green-but-loud. Ratio tiers + the 50 M/s floor UNTOUCHED (complements). ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change; new brutex modules 100%
  line+branch; ruff/ruff-format/mypy --strict/pyright clean).
  Earlier: `claude/peaceful-planck-dnsjg9` (**DEC-LEDGER-DELTA-O-PAGE-001, MIGRATION.md #1533** — mid-pull
  ledger-read O(page) discipline restored across (a)-(f): (a) the /coverage/windows + pull-progress DuckDB fast paths
  fold pending window-ledger deltas latest-wins IN SQL via NEW `lake_read.dedup_qualify_latest_sql`/
  `dedup_latest_source_sql` (the keep-LAST twin of `dedup_qualify_sql` — `ORDER BY (filename = <bound base path>) ASC,
  filename DESC, file_row_number DESC`, bound equality never LIKE; live-verified on the pinned DuckDB incl. the
  `/deltas/`-lake-path + `'l'>'d'` filename traps, demotion survival and ghost-row safety), assembled at the ONE
  `db_view._ledger_scan_source` seam (fresh delta listing per attempt — the existing retry-once machinery IS the
  save-time flock-prune-race self-heal); the count caches re-key on the composite (base-mtime + delta-name-set) stamp
  token at FIXED generation=0 (delta appends AND cross-process CLI writes invalidate; mid-pull mark_dirty storms never
  thrash); the schema probe stays BASE-only (a legacy 12-col lake defers WHOLESALE — the union_by_name NULL-fill →
  `int(None)` crash is structurally unreachable; a base-less delta-only first-pull checkpoint rides the fast path,
  deltas writer-pinned at the full 14-column schema); a persistently-malformed delta degrades to the fold-aware legacy
  pyarrow path (window payload) / the resilient Python-fold counts (phase progress — never blanked mid-pull);
  `phase_progress_payload`'s O(records) Python delta branch DELETED; SANCTIONED mid-pull changes, pinned + ledgered:
  rows converge to the steady-state DuckDB within-tier order, and counts_since_pull_start + pull_start_utc now ALSO
  appear mid-pull. (b) `_WINDOW_CACHE`/`_WINDOW_ROWS_CACHE`/`_SPOT_ROLLUP_CACHE` collapse onto ONE reused
  `MoversSnapshotCache` (`_LEDGER_READ_CACHE`: single-flight — 8 concurrent cold pollers pay exactly 1 parse; the #920
  adaptive floor EXTRACTED to `summary_cache.effective_adaptive_floor` with `SummaryCache._plan` + `movers_cache`
  delegating byte-equal — §11 COMMON, no second copy; floor cap 120 s, each stale serve bounded to min(max(2 s, the
  serving entry's own compute duration), 120 s) of ledger movement — possibly spanning several ≥2 s delta flushes, a
  TIME bound never a stamp-count bound — ALWAYS logged `movers_cache.stale_served` on the key that served it; a
  derived rows/rollup family additionally ages under its OWN floor once the records floor passes, so its worst-case
  staleness COMPOSES records floor + derived floor, each ≤ 120 s; NEW
  `movers_cache.revision_generation` digest bridge (homed in the movers_cache LEAF — a db_view → lake_revision
  edge closes a CodeQL py/cyclic-import cycle via lake_revision's lazy window_ledger_store →
  coverage_reconcile → db_view imports; AST-pinned); the base-less delta-only /db grid inconsistency FIXED). (c)
  `api_sweep_leaderboard` keys on NEW `lake_revision.sweep_results_revision` (the sweeps dir-mtime digest + the audit
  chain's `(mtime_ns:size)` stat — a SWEEP_START append moves NO dir mtime, the stat closes that gap; a CLI `brutex
  sweep-all` + a store delete + an audit-only append ALL invalidate; a probe outage computes fresh UNCACHED;
  `on_success=mark_dirty` KEPT — redundant for /results, zero drift elsewhere). (d) `build_sweep_leaderboard` splits
  into the revision-cached `load_sweep_scan` (NEW frozen `SweepScan` carrying EVERY payload-honesty field) + the
  byte-order-preserved rank (two-pass sort moved verbatim; golden vs `ResultsStore.top_k` incl. the raw-int precision
  teeth); a filter/rank click within one revision performs zero store loads + zero audit reads (spy-pinned);
  `_snapshot_eras` streams `iter_entries` (O(1) memory; audit.py ZERO diff — `verify_chain` untouched; the AuditCursor
  incremental resume is a NAMED follow-up). (e) `bx-rate-monitor.js` + `bx-liveproof.js` gain the v29
  `if (document.hidden) return;` poll guard + the visibilitychange immediate catch-up (a hidden tab fires ZERO
  fetches; never a stale pill on return; source-pinned, honestly not a browser run). (f) NEW
  `tests/bench/test_ledger_delta_o_page_o1.py` — 7 pins, every number MEASURED 2026-07-06 in the docstrings: one-page
  mid-pull poll flat-vs-depth 1.24× @ 1k→10k with 5 live deltas (cache_tier_ceiling — the page query is an ADMITTED
  O(total-rows) columnar scan); ONE GROUP BY per stamp + ONE recount per real delta append + ZERO Python parses;
  phase-progress 1.00× flat; the fresh-fold delta-file footer term measured 2.44× for 10× the files (compaction the
  named future bound); 8-thread cold-stamp single-flight; the cold 12-store/200-audit-entry scan 21.8 ms vs a 2 s
  ceiling + zero-rescan clicks + top_k order parity; /results polls flat under real delta + mark_dirty churn. HONEST
  (Golden Rule #10): the fold is ONE O(total physical rows + delta-file footers) columnar QUALIFY hash-partition per
  FRESH stamp — NOT literal O(page) (the `query_candles` envelope); the first Python parse per stamp stays O(rows);
  each /results poll pays one O(dirs)+O(1-stat) probe. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX, F&O lake
  REFERENCE_ONLY, no scope/vocab/rate change; steady-state (zero-delta) SQL text + payloads byte-identical
  (spy-pinned); touched modules 100% line+branch; ruff/ruff-format clean, `mypy --strict` + pyright at the pre-change
  baseline).
  Earlier: `claude/amazing-darwin-hb4caw` (**DEC-SWEEP-EXIT-INDEX-FASTPATH-001 +
  DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 / ADR-0017, MIGRATION.md #1531–#1532** — the sweep-metrics Rust tail is now
  ON BY DEFAULT (`SWEEP_RUST_METRICS_DEFAULT=True`, the exact "separate later ADR" flip #1186 deferred; revert = ONE
  constant; `BRUTEX_SWEEP_RUST_METRICS=0` is the per-process kill switch), licensed by two safety layers — ORIGINALLY
  authored + proven fully green under the prior default-OFF before the flip, though the pushed branch is ONE
  reconstructed post-wipe commit so no shipped-first commit boundary exists (see ADR-0017 Status / #1532): the
  exit_idx-native FAST PATH — both trade simulators return each
  mask's certified exit-bar indices (NEW stdlib-only COMMON `brutex/engine/exit_index.py`: `build_ts_index`
  consolidating the three inline dict copies + `bars_strictly_increasing` producer certificate +
  `exit_indices_consistent` consumer belt) so the kernel arm consumes them O(trades) with NO per-mask O(N_bars)
  `exit_ts`→index dict build (inconsistent stream DEMOTES to the dict, never to the Python oracle — results
  byte-identical; measured 6.1× sandbox tail ratio, bench-gated ≥1.15×) — and a per-process BURN-IN SELF-CHECK: the
  first 16 kernel-served masks are bit-compared (`struct.pack` floats, signed zeros) against the Python
  `_drawdown_stats` oracle; a mismatch SHIPS THE ORACLE VALUE and AUTOREVERTS the kernel for the process (ERROR +
  latch + `kernel_disabled` short-circuit). The frozen `_GOLDEN_CONTENT_HASH`/`_GOLDEN_RUN_ID` stay UNCHANGED — the
  default (no-env) arm reproduces them byte-for-byte with exactly ONE engaged event (golden-pinned). ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate change; whole-engine unit gate 100.00% line+branch (1928
  tests); ruff/ruff-format clean, mypy `--strict` clean, pyright at the pre-change baseline).
  Earlier: `claude/laughing-dirac-ytxj3k` (**DEC-PULL-UI-STALENESS-001 + DEC-PULL-HEADER-SUBUNIT-001,
  MIGRATION.md #1529–#1530** — the pull-UI staleness pack, FRONTEND-ONLY (zero Python source change): the rate
  widget + the `/universe` pull-progress header gain a client-clock "updated Xs ago" staleness readout (amber →
  LOUD RED "STALE") + a self-recovering poll (every failure mode counted; quick retries then 3 s→30 s capped
  exponential backoff, never gives up — the 38-minute frozen-tab incident fix; a non-ok poll can no longer settle
  the header), and the `/universe` header + `#subunit` counter show per-expiry sub-unit progress derived entirely
  client-side from `s.current` transitions (fno_symbol grammar mirrored right-anchored; spot/empty → omission).
  textContent-only; substring tests pin presence not execution (honest envelope); ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX).
  Earlier: `claude/kind-goldberg-wln56a` (**DEC-O1-CLAIMS-HONESTY-001, MIGRATION.md #1528** —
  the O(1)-claims honesty + preopen-discovery pack: the `/reports` preopen symbol discovery memoised in the
  COMMON #896 generation-keyed `_db_datasets_cache` (per-basis `preopen_datasets_key`, seeded post-pull by
  `warm_discovery_memos` off its ONE unfiltered walk, `_MEMOS_SEEDED` 5→7; pullability tagging deliberately
  live — the keyless `load-instruments` `master_revision` trap); `resolve_report_datasets` discovers FILTERED
  at the source (`exclude_segments={FNO}` prunes the contract tree at walk descent + `intervals={required}`;
  SANCTIONED: an F&O contract partition is no longer offered as a report symbol) with a NEW `datasets=`
  pre-walked seam; the `db_view.list_candle_datasets` manifest rebuild always stamps the PRE-walk ledger token
  (mid-walk race → the stale-stamped base still LANDS — it is `record_dataset`'s write-side convergence
  seed, the round-6 review restore of an interim SKIP design — plus one observe-only
  `db_view.manifest_rebuilt_stale` INFO; the load-side token gate stays the freshness authority); welcome.html + Golden Rule #5 reworded to the TRUE
  two-tier O(1) enforcement of `tests/bench/_o1_tiers.py` (robust 3–10× shared-CI / strict 1.1× M4-Pro via
  `BRUTEX_M4PRO_STRICT=1` / measured 3× cache-tier — words only, NO gate weakened); two NEW bench pins
  (u128 two-limb mask + `AuditWriter.append` flat vs chain length, prefill via the COMMON
  `tests/fakes/audit_chain.py`). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; dashboard read-side + docs +
  bench only; touched modules 100% line+branch; ruff/ruff-format clean, mypy `--strict` + pyright at the
  pre-change baseline).
  Earlier: `claude/ecstatic-pascal-7vjsju` (**DEC-PULL-SIX-STEP-ORDER-001, MIGRATION.md #1527** — the
  operator's STRICT six-step pull order: every Groww pull executes INDICES-SPOT → STOCKS-SPOT → INDICES-FUTURES →
  STOCKS-FUTURES → INDICES-OPTIONS → STOCKS-OPTIONS (empty groups skipped), enforced at the TWO existing execution
  chokepoints with ONE pool/controller build each (the ONE `SessionRateController` + 500/min governor spans all steps
  as today): (a) SPOT — always-on `ingest_common.partition_spot_steps` group loop INSIDE `pull_all_spot` (the COMMON
  `PullStep` IntEnum, value==rank 1-6; rollup fold zero-change, proven; `WindowLedger.progress_counts(symbols=)`
  group-scopes each until-clean — indices exit `stop_clean`, no wasted round); (b) F&O — 4-key
  `(ledger-market, is_index)` bucket sort + STEP-major until-clean (`_contract_market` KEPT verbatim, ledger
  namespaces byte-unchanged — no re-pull; write/read-symmetric `primary_fno_underlying` classification, unresolved →
  stock-side + WARN + `class_unresolved`; `underlying_kinds=None` — the CLI, tests — degrades to the legacy 2-bucket
  order byte-identically; REVIEW ROUND 1: the `/pull-historical` DIRECT-selection grammars now build + thread the
  whole-universe kinds map, so BOTH production dashboard routes run the six-step order); metadata stays between
  steps 2 and 3 (not a numbered step). POST-steps (Greeks → prebuild →
  movers) fire only after ALL applicable steps SETTLE (settle-based, never `bars > 0`): cleanset default
  byte-compatible with DEC-PREBUILD-CLEANSET-001; NEW opt-in `BRUTEX_PIPELINE_STRICT_GATE=1` literal all-or-nothing
  barrier (fail-loud parse once at launch → clean 400; pure `step_settle_verdict` + `PriorStepsVerdict` threaded
  sequencer → composite → `pull_contract_candles` so even the Greeks seam sees spot+metadata cleanliness — withheld
  reads "withheld (strict gate) — re-attempted next pull", never FAILED; REVIEW ROUND 1: `spot_phase_ok` /
  `historical_phase_ok` fold the MASS-EMPTY halt causes — a spot mass-empty halt now stops the chain like auth/quota
  and can never read settled to the barrier — and the composite folds its phase-(a) re-run gate into the forwarded
  verdict so Greeks/prebuild/movers agree). COMMON `pull.step.started/settled/halted`
  events + observe-only `StepOrderMonitor` (test oracle `raise_on_violation=True`); the 2026-07-05 log-shaped
  ordering regression + fetch-count parity bench pin "reorders, never re-fetches". HONEST: ~seconds added per pull,
  NOT a speedup (bounded analysis, not measured); async-mode step settles are PRODUCER-side (tail writes drain
  terminally but are INVISIBLE to the producer-fed `StepOrderMonitor` — `pull.step.late_write` observes only
  producer-side writes, stays 0 on a healthy async pull; submit-time step stamping through `FetchPipeline` is a
  named follow-up, NOT claimed; the settle events' `bars`/`clean` are producer-side counts, deferred
  0s in the default async mode — disclosed on the event as `bars_deferred=true`, real totals land post-drain; the
  SPOT settle event's sync counts SUM every until-clean pass and its `clean` folds the group's terminal quarantines —
  round-2 review, F&O-twin parity); the mass-empty breaker's trip evidence is per-STEP-GROUP pass on BOTH spines —
  F&O per-(market, index/stock) BUCKET, SPOT per indices/stocks group (a disclosed narrowing, #1527 R6+R9 — evidence
  resets at the group boundary; the ~4-series INDICES-SPOT group on an incremental pull sits below every trip
  threshold, so a vendor soft-throttle's clean empties can settle ENGINE index windows SKIPPED with no halt before
  the stocks group's breaker trips — remediation `brutex reopen-skipped`; sharing one breaker across a phase's groups
  deliberately NOT done, the groups run sequentially so cross-group evidence cannot protect the FIRST group;
  thresholds/canary/bounded-settle machinery unchanged); `historical_pull.py` gains a dedicated STATIC ci.yml
  per-module 100% gate (round-3 R10 — its suites are stem-inverted `test_pull_historical*.py`, so the discovery gate
  warn-skips it; measured 100.00% over the curated five-file suite); on a
  mid-pull global stop a never-started later group's symbols read honest unknown-ZEROS in the per-symbol rollup (not
  synthesized FAILED — the ledger stays the retry authority); the CLI spot path is the NAMED follow-up
  DEC-PULL-SIX-STEP-ORDER-CLI-001. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; every touched module 100% line+branch;
  ruff/ruff-format/`mypy --strict`/`pyright` clean).
  Earlier: `claude/relaxed-noether-7k30p2` (**DEC-ETC-REGIME-HISTORY-001, MIGRATION.md #1524** — the
  pre-2024-10-01 options ETC fix: the two `(date.min, <current rate>)` UNVERIFIED placeholder rows are replaced by
  explicit rate-less REFUSAL rows on the NEW `RegimeRow` model (frozen slots; `rate_bps_x100: int | None`; every row
  carries its citation or its honest gap; import-time `validate_regime_table`); the ONE COMMON `rate_for_date`
  (allocation-free bisect) raises the NEW typed `UnverifiedCostRegimeError` on a refusal row, and the options executor
  prechecks the date BEFORE any lake I/O and skips-and-counts (`RejectReason.COST_REGIME_UNVERIFIED`; reject order
  pinned lot_size → cost_regime → no_data → stale_bar → tick_floor; ONE aggregate `options.sim.unverified_cost_window`
  WARN; CLI leaderboard footer derived from `etc_unverified_windows()` DATA). NEW `exchange_for_instrument` kills the
  two copy-pasted SENSEX branches (unknown instrument now fail-loud). Exactly TWO verified ETC rate rows exist (NSE
  3_503 / BSE 3_250, eff. 2024-10-01, COSTS_VERIFIED §2 rows 3-4); every pre-boundary regime (NSE FA46730/FA56129/
  FA61137, BSE 20231020-46/20240430-42 — identified, NEVER officially retrieved) stays UNVERIFIED text-only; FA73061
  (eff. 2026-03-01) is a documented UNVERIFIED follow-up, NOT encoded. Goldens relocated 2024-05-02 → 2024-10-03 with
  every literal hand-re-derived + a boundary-exact 2024-10-01 golden + a pre-boundary refusal golden (zero lake reads
  proven). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; no scope/vocab/rate-cap change; brutex.costs + executor 100%
  line+branch; the CLI leaderboard renderer's `# pragma: no cover` LIFTED post-round-1-review —
  `_render_options_leaderboard` directly unit-tested at 100% line+branch, `getattr` default → fail-loud direct access
  (tests/unit/test_cli_options_leaderboard.py); ruff/ruff-format clean, `mypy --strict` clean, pyright 0 errors).
  Earlier: `claude/vibrant-knuth-itozu6` (**DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001, MIGRATION.md #1522** —
  the `GET /api/v1/pull/rate` CLI-bridge file read (`<lake_root>/.rate/pull_rate.json`, previously a synchronous
  `Path.read_bytes()` executed ON the uvicorn event loop on EVERY ~3 s site-wide widget poll — a slow/stalled external
  lake volume froze the WHOLE dashboard) is moved off the loop behind a NEW `brutex/dashboard/rate_bridge_cache.py`
  `RateBridgeCache`: a 2 s-TTL + `mtime_ns:size:ino` content-token memo (NEW public `lake_revision.rate_bridge_revision`
  over the shared `_stat_token` core, `include_ino` opt-in — default tokens byte-unchanged) whose post-TTL refresh
  (stat + conditional read, BOTH inside the guard) runs under the COMMON `run_with_deadline` freeze guard (0.5 s) with a
  NON-BLOCKING single-flight (followers serve last-known immediately; `_inflight` cleared only by the refresh daemon's
  finally → EXACTLY ONE abandoned daemon per outage, late-return self-heal), called via `run_in_threadpool` (the app.py
  offload doctrine) through a `build_app(rate_bridge_cache=)` injection seam; TWO ADDITIVE honest-freshness payload keys
  stamped in ONE place (`stamp_bridge_freshness`): `bridge_source` ∈ {live, cached, unverified, unwired} +
  `bridge_probe_age_s` — NO stale illusion (the cache stores the SNAPSHOT, never the view: ages recompute per request
  from its own `wall_time`, so the 12 s `STALE_AFTER_S` verdict machinery is unfooled; TTL 2 s < 12 s test-pinned); env
  kill switches `BRUTEX_RATE_BRIDGE_TTL_S` / `BRUTEX_RATE_BRIDGE_READ_DEADLINE_S` (fail-loud parse). Every
  pre-existing payload key byte-identical (frozen 17-key set ∪ the 2 additive keys, pinned; direct-uncached-computation
  parity pinned); teeth: the same wedged read blocked the PRE-change route 8.02 s and is bounded < 2 s fixed. HONEST:
  a liveness/latency fix, NO throughput claim; steady-state O(1) zero-syscall + one threadpool hop, post-TTL one stat,
  miss one ~300-byte read; the wedged daemon cannot be killed (the #999 stance — free loop + bounded latency +
  at-most-one zombie); the CLI WRITE-side bridge-publish hang is a named follow-up, NOT fixed. READ-side dashboard
  only — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX); `rate_bridge_cache.py` + `lake_revision.py` 100% line+branch;
  ruff/ruff-format/`mypy --strict`/pyright at the pre-change baseline).
  Earlier: `claude/zealous-maxwell-w257yf` (**DEC-GREEKS-SPOTJOIN-STOCK-001, MIGRATION.md #1482** — the
  Greeks spot-join drift-recovery ladder gains the evidence-grounded STOCK arm, CLOSING the SPOTJOIN-2 named follow-up
  of #1188. NEW `brutex/data/spot_drift.py` (scan the real `bars/<exch>/{INDEX,CASH}` spot dirs once per enrich pass +
  the PURE `build_stock_drift_map` verifier + the fail-soft `load_stock_drift_map` loader over
  `<lake_root>/instruments.duckdb`); `lake_keys.py` gains the pure `stock_drift_fold` (the mirror-pinned twin of
  `universe.normalize_underlying_symbol`) + the `StockDriftMap` alias + the kw-only default-`None` `stock_drift` param
  on `spot_recovery_candidates` (APPENDED last rung; 2-arg calls byte-identical; prefix invariant pinned);
  `greeks_enrich.py` threads the parent-built map through `read_spot_join_with_recovery` (additive
  `rung∈{alias_ladder,stock_drift}` field on the ONE `spot_key_recovered` log) → `_default_seams` →
  `_GreeksWorkerContext` (defaulted-LAST picklable dict field) → `_run_unit`/`_greeks_worker`/`_parallel_pickle_ok`/
  `_run_parallel`, built ONCE per pass in `enrich_greeks_for_targets` gated `ordered and read_spot is None` (public
  signature UNCHANGED; injected seams / empty targets / dry-run / CLI fetch ⇒ zero scans). Verification rule: a
  candidate needs a REAL on-disk dir + a UNIQUE master fold-match; ≥2-master folds POISONED (capped WARNs); fragment
  dirs offered ORDERED (master-exact first) — the probe decides; unverified evidence never mapped (Golden Rule #1).
  Fail-soft: no master → `None` (honest NO_SPOT preserved byte-identically), confirmed read errors (`CatalogError` /
  `duckdb.Error` / `OSError`) → WARN + `None`, MemoryError re-raises raw, unexpected defects reach the #1188
  `fno.greeks_enrich.error` fence. Sequential ≡ parallel bytes golden-pinned (real 2-worker spawn over a drifted
  2-stock lake + real master); the held-open-store integration pin closes the #935/#1014 dormant-feature trap. HONEST:
  NO `%26`→`&` recovery (stored-key encoding UNVERIFIED); read-time recovery ONLY (`lake_heal`/`detect_legacy_drift`
  untouched); genuine abbreviations stay alias-map territory; the `/db` moneyness twin is the NAMED follow-up
  DEC-DB-MONEYNESS-STOCK-EVIDENCE-001 — until it lands a drifted stock lake shows recovered Greeks but a `/db`
  moneyness UNKNOWN (honest split-brain). ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX, F&O lake REFERENCE_ONLY, no
  scope/vocab/rate change; `lake_keys.py` (75/44) + `spot_drift.py` (143/42, NEW) + `greeks_enrich.py` (539/130) ALL
  100% line+branch; ruff/ruff-format clean, `mypy --strict` + `pyright` at the pre-change baseline).  Earlier: `claude/stored-data-validation` (**DEC-STORED-DATA-VALIDATION-001 +
  DEC-MUTATION-ALERT-CANCELLED-001, MIGRATION.md #1510–#1511** — the stored-data validation pack: price POSITIVITY at
  the ENGINE READ BOUNDARY (`arrow_table_to_bars`, partition-naming corrupt-bar errors) — NOT the general bar
  constructor, which stays permissive for the Greeks BAD_INPUT / executor-guard / trade-sim-fallback classifiers; the NEW COMMON
  vectorized `brutex/data/price_sanity.py` gate at the prebuild's `make_lake_read_1m` boundary (corrupt symbol
  quarantined + named via the existing `ResampleInputError` machinery, derived TFs not written); the lake census
  scanner's OBSERVE-ONLY `PriceSanityRows` finding over the same per-file pass (healer filters it — never
  value-editing); the `groww.parse.ohlc_clamped` per-window clamp counter; the shared `bx-format.js` ROUND_HALF_UP
  2dp display formatter for movers/db; and the mutation workflow alerting on `cancelled()` so a timeout-killed
  mutation job is visible. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; clean lakes byte-unaffected; touched modules
  100% line+branch; ruff/ruff-format clean, mypy `--strict` + pyright at the pre-change baseline).
  Earlier: `claude/suspicion-sweep-fixes` (**DEC-SUSPICION-SWEEP-FIXES-001, MIGRATION.md #1492** — the
  19-agent suspicion-sweep fix pack: D1 the spot AIMD ladder now sees every client-absorbed 429 (the FULL controller
  hook triple bound at the ONE COMMON `run_ingest._with_paced_client` spine seam both spot build sites feed; NEW
  `with_rate_hooks` on `GrowwClient`+`ClientPool`; spine hooks stand down via the NEW `client_self_reports_*`
  predicates — rate CAPS untouched, feedback wiring only); D2 the F&O spine's propagated-429 double count suppressed
  (the `client_self_paces` stand-down twin); D3 the /db window-ledger route reads the mtime-cached
  `window_ledger_rows_cached` instead of re-parsing ~841k rows per paging click (honesty note rewritten); G1 the
  sweep-all derived-TF FRESHNESS refusal gate (`lake_derived_freshness_verdicts` + `preflight_derived_freshness`);
  G2 the future-Muhurat clamp-hazard enumeration pin; G3 the no-production-`resample_bars` AST pin; G4 the
  rate_common 5→8 ceiling docstring reconcile. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; touched modules 100%
  line+branch; ruff/ruff-format clean, `mypy --strict` clean, pyright at the 36-error pre-change baseline, 0 new).
  Earlier: `claude/hw-thread-bounds` (**DEC-HW-ENVELOPE-CONSOLIDATE-001 + DEC-THREAD-BUDGET-BOUNDS-001,
  MIGRATION.md #1490–#1491** — ONE hardware-envelope module at `brutex/config/hardware_envelope.py` (the #1360 sibling
  `brutex/hardware_envelope.py` DELETED, consumers repointed, the #1360 RAM-budget contract ported verbatim, the
  interim #1167-resolution RAM-delegation pin superseded, `greeks_pool_workers` delegated to the COMMON
  `cpu_pool_workers` byte-unchanged) + the polars global pool bounded once at the `brutex.cli` dispatch before the
  first polars import (default = FULL physical width — prebuild throughput priority) and every concurrent-reader
  DuckDB connection bounded at creation (`physical_cores // 2` per connection — the contention fix), kill switches
  `BRUTEX_POLARS_THREADS`/`BRUTEX_DUCKDB_THREADS`;
  parallelism-only, byte-identical results; ENGINE UNCHANGED).
  Earlier: `claude/hw-pr5-ram-budgets` (**DEC-RAM-BUDGETS-001, MIGRATION.md #1360** — RAM-scaled memory
  budgets: NEW stdlib-only `brutex/hardware_envelope.py` (total-physical-RAM detection, cached once per process with a
  test reset seam, any failure → the honest `0` "unknown" sentinel; ONE COMMON `memory_budget_bytes` derivation — env
  override wins fail-loud, sentinel → floor EXACTLY, detected → `int(total × fraction / workers)`, never clamped to
  the floor on the detected path). Derived call sites (BOTH exact-parity at the §8 48 GiB box, regression-pinned):
  `sweep_mem_budget.DEFAULT_MEM_BUDGET_BYTES` (8 GiB floor, fraction 1/6; the `BRUTEX_SWEEP_MEM_BUDGET_BYTES` #1352
  override contract byte-unchanged) + `bloom.MAX_BLOOM_BYTES` (16 GiB floor `MAX_BLOOM_BYTES_FLOOR`, fraction 1/3,
  derived ONCE at import via `resolve_max_bloom_bytes`, NEW fail-loud env `BRUTEX_MAX_BLOOM_BYTES`;
  `BloomTooLargeError` refusal still BEFORE any allocation; results-safe — the Bloom is an L1 prefilter over the LMDB
  exact ground truth, never in run identity). Every other candidate deliberately KEPT FIXED (LMDB virtual-map sizes,
  FAIL_CLOSED capacity trip-points, determinism pins like `ROW_GROUP_SIZE` + trade-sim chunk bounds, back-pressure
  queue depths — full classification in #1360). HONEST: PORTABILITY + headroom, NOT a measured speedup; detection is
  TOTAL physical RAM not available/free; the macOS `sysctl` path is UNVERIFIED on real macOS (mocked unit seam only);
  ADR-0016's "48 GB safety ASSERTED, NOT MEASURED" caveat stays OPEN. ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  `hardware_envelope.py`/`sweep_mem_budget.py`/`bloom.py`/`sweep_launch.py` ALL 100% line+branch; ruff/ruff-format
  clean, mypy `--strict` + pyright at the pre-change baseline).
  Earlier: `claude/nice-wright-qmb2bz` (docs-debt + observability pack). Prior merged: (**DEC-INGEST-BOUNDARY-INTEGRITY-001, MIGRATION.md #1294** — the
  ingest data-integrity pack, four boundary gaps as ONE change set: **(a)** the ingest-boundary session filter is
  CALENDAR-AWARE (NEW `brutex/data/session.py::filter_to_trading_session` judging each bar against its OWN IST date's
  real NSE session via `calendars/nse.get_session`, wired at BOTH spine tails) — non-trading-date bars (weekend/holiday
  leaks) dropped + COUNTED with EXACTLY ONE `*.session_filter.dropped` WARN per window; evening-muhurat +
  special-weekend bars now KEPT (sanctioned behavior change); regular-day kept-set golden-pinned byte-identical to the
  legacy filter; out-of-coverage dates abstain to the legacy fixed window; daily path deliberately unfiltered;
  resamplers untouched by design (single choke point covers all 21 TFs); **(b)** the parser's defensive epoch branch
  emits ONE `groww.parse.epoch_timestamp` WARN per pull (thread-safe pull-id guard + reset seam; vendor epoch semantics
  UNVERIFIED — assumed Unix-UTC, an IST-naive epoch would shift bars +05:30, hence the loudness; parsing byte-unchanged);
  **(c)** an ALL-FILTERED window (raw > 0, survivors == 0) demotes re-pullable FAILED + `session.ALL_FILTERED` on the
  #1031 degraded machinery (bounded SKIPPED settle; SPOT gated on `detect_completeness_gaps` [dry-run synthetic bars
  land on the window-start day → false-red ungated], F&O UNgated [its dry-run returns empty payloads] — the F&O spine's
  FIRST demotion, all-filtered ONLY, #1031's observe-only liquidity-heuristic stance preserved; orthogonal to sink
  dedup — a full-dedup re-pull stays SUCCESS, pinned both spines; genuine EMPTY byte-unchanged); **(d)**
  `SinkAppendResult.bars_net_new` persisted into the window ledger (`WindowLedgerRecord.bars_net_new`, the
  optional-column back-compat template appended LAST; an OLD ledger loads `None` — honest unknown, NEVER a fabricated
  0; deferred/failed writes carry a KNOWN 0; `/db` window-ledger grid gains the column). ENGINE UNCHANGED
  (NIFTY/BANKNIFTY/SENSEX), F&O lake REFERENCE_ONLY, no scope/vocab/rate change; every touched module 100% line+branch;
  ruff/ruff-format clean, mypy/pyright at the pre-change baseline (0 new diagnostics).
  Prior merged: **DEC-GREEKS-CORRECTNESS-AUDIT-001, MIGRATION.md #1188** —
  the 7-lens adversarial correctness audit of the Greeks enrichment value chain
  (`brutex/data/greeks_enrich.py` + `brutex/options/risk_free.py`) + its fix round. AUDITED CLEAN: the
  near-expiry `T_FLOOR` boundary · kernel failure-band recovery · provenance pack/unpack losslessness
  (300k-tuple sweep) · counter honesty · the 6.5% ASSUMPTION labelling · an INDEPENDENT stdlib Black-76
  cross-check (3,822 price + 3,321 solve points) — **zero wrong stored numbers**. CONFIRMED defects FIXED:
  (1) the reuse fast path was blind to input VALUES not in the stamp — FIVE lenses converged
  (RF-1/PROV-1/CTR-1/B76-1/G-RACE-2 rate value · SPOTJOIN-1 spot value · PROV-3 style) — the gate now
  value-checks stored `rate_used`/`spot_at_bar`/style and recomputes on any mismatch (a coverage-shrunk
  CSV / vanished spot honestly DEMOTES to NO_RATE/NO_SPOT; RF-5's dead `setdefault` memo killed);
  (2) IVX-1 — NEW `DEGENERATE_TIME_VALUE_REL=1e-9` (kernel `VERIFY_REL_TOL` mirror, preflight-validated
  CE+PE over 292,500 combos): `close ≤ floor + 1e-9·close` stamps BELOW_INTRINSIC instead of an ARBITRARY
  solved IV from a [0.001, 0.29] indeterminacy band (subsumes IVX-4); (3) G-RACE-1 — `read_option_rows`
  month-bounds each file to its OWN UTC month, so a foreign-month poisoned dup can never overwrite
  canonical OHLCV via the latest-wins write; (4) SPOTJOIN-2 — a spot-key drift-recovery ladder in the
  default reader (index aliases BOTH directions + env `BRUTEX_FNO_INDEX_ALIASES` parsed locally · the
  `<EXCH>-<underlying>` #911 form · `lake_keys.spot_key_candidates`; ONE `spot_key_recovered` log; stock
  drift pairs NOT invented — named follow-up (closed by DEC-GREEKS-SPOTJOIN-STOCK-001 #1482 — the
  evidence-grounded `stock_drift` map)); (5) CTR-2 — `LakeWriteHalt` re-raises past the per-target
  quarantine (the fenced wrapper still never fails the pull); (6) IVX-2/IVX-3 — NaN-row per-bar skip
  (`bars_skipped`), invalid-spot → NO_SPOT, non-positive close → BAD_INPUT before the floor screen; plus
  PROV-2 loud pack band validation, RF-2/RF-3 `CsvRateSource` construction validation +
  UnicodeDecodeError→RiskFreeError, CTR-6 `null_reasons` per-status telemetry on `target_enriched`,
  SPOTJOIN-3 2dp `round_money` spot snap (spot only, never a Greek), and `GREEKS_FORMULA_VERSION` 1→2
  (enrich golden regenerated — the diff is EXACTLY the formula bit, zero value drift). HONEST envelope:
  the v2 re-enrich reaches ONLY contracts inside a live pull window (expired contracts keep v1 stamps —
  bulk re-enrich a named follow-up); a CLI-pulled lake never enriches (follow-up) [CLI-ENRICH RECONCILE —
  DEC-CLI-GREEKS-ENRICH-001, MIGRATION.md #1536: BOTH follow-ups are now CLOSED — the CLI fetch path runs the
  same fenced pass post-drain (default-ON, `BRUTEX_CLI_GREEKS_ENRICH` kill switch) and `brutex enrich-greeks`
  is the bulk v1→v2 re-enrich lever; the CLI L2 heal seam is ALSO closed — DEC-CLI-LAKE-HEAL-L2-001 (#1493),
  ordered BEFORE the CLI enrich seam];
  rust/** kernel out of scope this round. Both touched modules 100% line+branch (340/90 + 117/32); ruff/`mypy --strict`/pyright
  clean; ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), F&O lake REFERENCE_ONLY, no scope/vocab/rate change.
  Prior merged: **DEC-CI-FORK-PR-HARDEN-001,
  MIGRATION.md #1180** — the PUBLIC repo's fork-PR attack surface CLOSED; CI-workflow +
  redteam-test + docs ONLY, ZERO `brutex/` code touched: (1) `auto-merge.yml`'s trust gate
  no longer trusts the SPOOFABLE `claude/*` head-branch NAME (a stranger's fork named
  `claude/anything` would have been auto-squash-merged to main on green CI) — trust is now
  the explicit author allowlist (SJParthi, dependabot[bot], renovate[bot]) OR a LIVE-API
  `author_association` ∈ {OWNER, MEMBER, COLLABORATOR}, decision logged; every other
  auto-merge gate byte-for-byte unchanged. (2) `pre-merge-gate.yml` (a `workflow_run` job
  with `pull-requests: write`) no longer checks out the PR head SHA and executes
  `scripts/ci/pre-merge-check.sh` FROM THE PR TREE (arbitrary code with the job token) —
  checkout pinned to base `main` + `persist-credentials: false`, the PR number passed as
  DATA only (verified: the script reads only the gh API, never the working tree);
  `auto-merge.yml` gains the same base-pinned checkout for its rollup-gate script, also
  fixing the latent no-checkout defect that left the DEC-MERGE-LIVE-ROLLUP-001 rollup gate
  fail-closed INERT (every auto-merge since #944 refused with gated=false on the missing
  script). Sweep: pre-merge-gate.yml was the ONLY privileged-trigger job checking out a
  PR-controlled ref; a directory-wide redteam pin keeps it that way (NEW
  `tests/redteam/test_redteam_fork_pr_harden.py`, 10 tests incl. FUNCTIONAL execution of
  the real trust-check script with a stubbed `gh`: claude/*-named fork from a
  non-member → trusted=false, allowlist/OWNER/MEMBER/COLLABORATOR → trusted=true). HONEST
  (Golden Rule #10): server-side branch protection (require PR + `CI OK (aggregate gate)`
  + up-to-date branch + restricted merge; Actions → "Require approval for all outside
  collaborators") is an OWNER GitHub-settings task — exact click-path in ledger #1180's
  own honest-limits follow-up (the prior "#1176" citation here was DRIFTED — #1176 is
  DEC-OPTIONS-EXEC-COST-AUDIT-R2-001, unrelated; reconciled 2026-07-06 under
  DEC-CLOUD-AUTOFIX-RETIRE-001, MIGRATION.md #1555), NOT claimed done. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate change.
  Prior merged: **DEC-LAKE-HEAL-001, MIGRATION.md #1175** — the
  lake gains ONE bounded invariant scanner (`brutex/data/lake_invariants.py`: intra-file dup ·
  UTC-month/foreign-row · #911 legacy-key drift via the COMMON `brutex/data/lake_keys.py` move)
  + a fully AUTOMATIC keep-FIRST healer (`brutex/data/lake_heal.py`, default AUTO-ON, escape
  hatch `BRUTEX_LAKE_HEAL=observe`): quarantine-COPY to `<lake>/.quarantine/<ts>/` → rewrite
  THROUGH the normal write path (NEW public `ParquetWriter.heal_partition` — locks/P9/overlay/
  raw_hash rotation all reused) → hash-chained audit JSONL + one Telegram alert per batch;
  detection at L1 (free reconcile piggyback on `ReconcileResult.findings`, healed at the
  `cascade_wire` reconcile chokepoint), L2 (post-pull, pulled-symbols × span-months, wired
  at the post-drain seam in `pull_controller` + `historical_pull` — BEFORE the Greeks enrichment
  + the pipeline's final prebuild derivations (the mid-pull daily derive was removed by
  DEC-PIPELINE-AUTOPREBUILD-001 #1145, so the heal now guards those readers); `LakeWriteHalt` re-raises
  into the pull-halt handling), L3 (one-time content-token-stamped startup sweep, throttled
  daemon, boot can never crash); derived recall = 1day re-derive (latest-wins) + intraday-TF
  DELETE-and-rebuild (keep-first would keep poison); census surfaces: `brutex lake
  audit-unique` (exit 0/1/2), a bounded doctor check, a `/universe` chip — each answering
  whether the ENGINE's index data (NIFTY/BANKNIFTY/SENSEX) carries dupes. HONEST: scan is
  O(files) NOT O(1) (bench-pinned); options tree = census + `(side,ts)` dup heal only
  (foreign/overlap heal a named follow-up) [CLOSED by DEC-LAKE-HEAL-OPTIONS-FOREIGN-001,
  MIGRATION.md #1594 — the options tree now gets foreign-month + overlap detection/heal
  too]; STRICTLY row-removing/row-reassigning, never
  value-editing; the CLI `brutex fetch` path has no L2 seam (covered by L1/L3/census, a named
  follow-up) [CLOSED — DEC-CLI-LAKE-HEAL-L2-001]. All new modules + every touched module 100% line+branch; the #1067 pin suites
  pass unmodified. ENGINE UNCHANGED, F&O lake REFERENCE_ONLY, no scope/vocab/rate change.
  Prior merged: **DEC-PIPELINE-AUTOPREBUILD-001 + DEC-FNO-EXPIRY-SCOPE-001,
  MIGRATION.md #1145–#1146** — the pipeline pack. **#1145:** ONE automatic timeframe PREBUILD phase — the 19 derived
  intraday TFs + the derived `1day`, over SPOT + FUTURES + OPTIONS — is now the pull pipeline's FINAL data stage, run
  strictly AFTER a fully-clean 1-minute fetch [the fully-clean `fetch_clean` gate arm is SUPERSEDED by
  DEC-PREBUILD-CLEANSET-001 #1221: the prebuild now runs on EVERY completed-chain pull over the CLEAN symbol set —
  `run_prebuild` receives the UNCAPPED `derive_quarantined_name_set` as an `exclude_symbols` exclusion set, so one
  quarantined symbol no longer starves the other ~N−1 of derived TFs; a fully-quarantined pull degrades honestly
  (`pipeline.prebuild.all_excluded` warning + an ok 0-built outcome)] (NEW `dashboard/pipeline_prebuild.py` `run_pipeline_prebuild(_guarded)`
  binding the SHARED `materialize_lake_timeframes(include_daily=True)`; `gated_prebuild`'s halt-on-first-phase gate
  REMOVED — per-symbol quarantine, every phase always runs, `halted_after` deleted; OPTIONS materialised for REAL via
  `discover_option_contracts` + one common `_run_contract_phase`, superseding DEC-PREBUILD-SELECT-001's on-demand no-op).
  EVERY mid-pull derived write is REMOVED (`pull_controller._resample_daily_after_pull` + the `BRUTEX_AUTO_DERIVE_TFS`
  pull-settle autoderive [DEC-TF-MATERIALIZE-002] + `historical_pull._derive_daily_from_minute`; `tf_autoderive.py`
  trimmed to the doctor's `compare_derived_freshness`) and the MANUAL surface is DELETED (the universe "Pre-build
  timeframes" button + the 188-line `POST /api/v1/universe/derive-timeframes` route; `sweep_preflight` + `doctor`
  repointed at the pipeline stage). The movers warm moves AFTER the prebuild and fires iff `fetch_clean AND
  PrebuildOutcome.ok` [the `fetch_clean` arm dropped by DEC-PREBUILD-CLEANSET-001 #1221 — the warm now fires iff
  `PrebuildOutcome.ok` over the clean set, so a dirty-fetch day still warms the CLEAN symbols, honestly omitting the
  headlined quarantined ones] — all three segments together (supersedes the early-spot-warm arm of
  DEC-PIPELINE-SEGMENT-MOVERS-001 #951); while the prebuild runs, `/api/v1/movers` is honestly GATED (NEW
  `prebuild_gate.PrebuildGate` injected into `build_app`; a 200 `gated:true` "prebuild in progress" payload with ZERO
  lake reads; `movers.html` renders it via `textContent` + a 5 s auto-retry so the screen self-recovers; release in a
  `finally` — clean run, early stop AND a raised phase all release; a dry-run never gates; the gate is PROCESS-LOCAL).
  Spot movers read the prebuilt partitions STORED-FIRST (NEW `movers_stored_read.read_stored_day_frame`, month-bounded;
  BYTE-EQUALITY vs the 1-minute resample PINNED; honest `stored_reads`/`resampled_reads` provenance in the payload;
  graceful 1-minute fallback; under the merged unified as-of semantic of DEC-HARDENING-LEFTOVERS-001 #1149 the stored
  FULL-bucket read serves ONLY a whole-day or bucket-final-aligned as-of cutoff [`_stored_serves_cutoff`] — a
  MID-bucket as-of always takes the 1-minute resample, the only source that can build the partial forming bucket
  honestly, never a leak of minutes past the as-of instant). Completion honesty: the pipeline headline NAMES quarantined symbols
  (`derive_quarantined_symbols` — spot failures + historical `start_dates − phase1_complete` folded to underlyings;
  capped 10 names + the TRUE count, `"N symbol(s) quarantined: A, B (+K more)"`; the job finish detail = the note,
  500-char cap) and `/coverage/windows` labels empty-vs-failed via ONE `db_view.window_status_label` ("vendor confirmed
  no trades (N clean attempt(s))" / "— settled" / "FAILED (will re-pull)") on BOTH payload paths. LOUD
  `pipeline.prebuild.*` / `pipeline.movers_warm.*` / `movers.gate.*` structlog events, all test-pinned. HONEST ENVELOPE
  (Golden Rule #10): a fully-materialised OPTIONS lake costs **~2-3× its 1-minute footprint** — operator-ACCEPTED
  (decree items #19+#21), stated at every enabled surface; the FUTURES/OPTIONS movers SESSION readers are deliberately
  NOT switched to stored-TF reads (`_read_future_session` is the LOCKED 1-minute-native as-of-cutoff snapshot per
  DEC-FNO-TF-DERIVE-OI-001, carrying the stored Greeks overlay [iv/delta, DEC-GREEKS-ENGINE-PR3B-001] — a stored-TF
  read cannot be byte-equal and would silently NULL the Greeks; MATERIALISATION-INVARIANCE is pinned instead, plus the
  pin that their prev-day close/OI BASELINES are served from the prebuilt `1day`); a CLI-pulled lake gets its derived
  TFs on the NEXT dashboard pipeline run (`brutex fetch` never derived mid-pull — verified). Supersedes the MID-PULL
  `1day`-derive arm of DEC-1MINUTE-ONLY-DERIVE-1DAY-001 #853 — the 1m→1d derivation itself STAYS, relocated into the
  prebuild phase (§1/§2 Rule 3/§6 reconciled). **#1146 (operator req #30):** expiry-LEVEL date scoping for the F&O
  historical pull — an expiry is IN SCOPE iff within the requested `[from → to]` window (BOTH endpoints inclusive; NEW
  pure `adapters/groww/expiry_scope.py`); an IN-scope expiry keeps the UNTOUCHED 30d/60d window law
  (DEC-FNO-FAR-EXPIRY-WINDOW-001 #1127); an OUT-of-scope expiry costs ZERO vendor calls + ZERO ledger rows + ONE
  `fno.expiry_out_of_scope` log per (underlying, expiry) and NEVER deletes stored data; scope SELECTS expiries, never
  reshapes windows — threaded route → `composite_pull` → `pull_contract_candles` at the ONE chokepoint before
  `_rebuild_contracts`; SPOT keeps its verbatim dates; the metadata phase stays unscoped (full calendar cached); the
  default no-dates pull is byte-identical + 10 permutations (a–j) pinned end-to-end. Both: ENGINE UNCHANGED
  (NIFTY/BANKNIFTY/SENSEX), F&O lake REFERENCE_ONLY, no vocab/rate/scope change; NEW + touched modules ALL 100%
  line+branch; ruff/ruff-format/`mypy --strict`/`pyright` clean.
  Prior merged: **DEC-SURFACE-TRIAL-001, MIGRATION.md #1154** — the `/results` +
  `/reports` **execution-grade SURFACE TRIAL** + the #1069-review leftover folds. The trial: fixtures written with the
  REAL repo writers (`ResultsStore` sweep stores incl. a cross-store duplicate run_id + a corrupt Parquet;
  `ParquetWriter` 1-day/1-minute lakes incl. a missing-prior-session hole + exact ±0.005% half-up boundary prices),
  the REAL routes driven via `build_app`+`TestClient`, and EVERY displayed number recomputed INDEPENDENTLY test-side
  (hand-rolled integer ₹-grouping — no `format_inr`/Decimal; `fractions.Fraction` %CHNG + an away-from-zero half-up
  rounder — no `decimal`; single-pass negated-key ranking; brute-force top-mover picks) then asserted equal
  cell-for-cell against the wire JSON / sheet XML. **VERDICT: NO DIVERGENCE FOUND — every trialed number matched**
  (money boundaries `₹-0.01`/odd-paisa lakh/`2**60`-paisa; exact-int-not-float ranking with a genuine
  `float(2**60)==float(2**60+1)` teeth fixture; run_id tie-break; dedup totals; top-slice invariance; CHNG/%CHNG law +
  contiguity/first-session skips; ±0.005 half-up cells; abs-max/inclusive-min_pct/alphabetical-tie top-mover; fills
  from the FULL-precision sign incl. the pinned "displays 0.00 but fills green" sub-boundary edge; minute-basis
  last-close anchor — the ANCHORED prior day now carries 2 bars with distinct closes so an arg_max→arg_min mutation
  of the prev-close read is verified KILLED (review round-1 fix: the original fixture's 2-bar day was unanchored and
  the mutation survived) — + hole skip) — the 13-test trial suite is the permanent regression net
  (`tests/unit/test_surface_trial_results.py` + `test_surface_trial_reports.py`; `/operator` confirmed a nav alias of
  `/`, not a route). Leftover folds: **MINOR-1 FIXED** — the `SummaryCache` revision-probe `os.stat` is hoisted
  OUTSIDE the cache lock (`_probe_token` no-lock + `_reconcile_revision_locked` under it) so a probe wedged on a
  stalled external volume can never block the pull's `mark_dirty` write funnel (regression: a stalled-probe test
  proving mark_dirty completes + an out-of-order-token test pinning the over-invalidate-never-miss envelope);
  **MINOR-2 DISPROVEN** for the reviewed sequence (an OSError probe outage never resets the baseline — recovery
  compares against the PRE-outage token and bumps; the narrower cold-start-during-outage variant is DOCUMENTED as an
  honest-envelope note, deliberately not mechanised); NIT-1 the movers as-of docstrings now claim MINUTE-granular
  honesty (the inclusive 13:45 bucket-open bar aggregates through 13:45:59 — never "no data after 13:45"); NIT-2 the
  stale pre-#1069 bucket-label-cutoff test comment rewritten to the raw-1m mechanism; NIT-3 the #1149 blurb's "all
  five touched modules" conflation corrected below. READ-side + tests + docs only; ENGINE UNCHANGED
  (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate change.
  Prior merged: **DEC-HARDENING-LEFTOVERS-001, MIGRATION.md #1149** — the
  hardening-leftovers pack, four areas, read-side dashboard + test + comment surfaces ONLY: (1) **cross-process lake
  cache invalidation** — a CLI `brutex fetch` into the same lake now refreshes every generation-keyed dashboard cache
  on the next poll, no restart: NEW COMMON `brutex/dashboard/lake_revision.py` derives an O(1) content token from ONE
  `os.stat` of the durable window-ledger Parquet (every production pull — dashboard AND CLI — checkpoints it,
  DEC-CLI-LEDGER-CHECKPOINT-001 #884), and `SummaryCache` gains a `revision_probe` seam (baseline-no-bump on first
  observation, bump-once on a token change, `None` = no signal) wired in `build_app`; HONEST: the token moves only when
  a writer touches the ledger — an out-of-band raw-Parquet write that bypasses it is NOT detected (the write-time lake
  manifest stays the named future work, #867/#1025). (2) **movers as-of unification** — the SPOT intraday snapshot
  leaked bars PAST the as-of minute (it cut on the RESAMPLED bucket-open minute, so an as-of 10:00 TF=5m read included
  10:01–10:04); all three tabs now cut the RAW 1-minute frame at the as-of IST minute via ONE COMMON
  `movers_snapshot.filter_to_ist_minute` (spot cuts BEFORE resampling — an intended, operator-visible spot change
  pinned by a regression; futures is a byte-identical refactor onto the helper; options rides the futures reader),
  killing the copy-pasted Int32 minute-of-day expression (§11 COMMON). (3) **flaky bench guard** — the two
  write-consumer A/B wall-clock timing tests are env-gated behind the sibling `BRUTEX_M4PRO_ABS=1` whole-test skipif
  (shared-GHA GIL noise flipped their cross-arm median comparisons); the deterministic byte-parity/settle/thread-leak
  oracles stay unguarded. (4) **NSE 2026 calendar honesty, comments only** — the "2026-02-01 Sunday Budget session
  missing" claim is REFUTED (already a verified `SPECIAL_WEEKEND_TRADING_SESSIONS` entry); the vendor-observed
  Jan-2026 drift (20 vendor sessions vs our derived 21) implies ONE additional Jan-2026 holiday whose date is
  UNVERIFIED and deliberately NOT fabricated — closable only by the official NSE 2026 circular or a live vendor probe.
  The five coverage-verified dashboard modules (`lake_revision.py` + `summary_cache.py` + `movers_snapshot.py` +
  `movers_futures.py` + the UNtouched-but-re-verified `movers_options.py`) 100% line+branch; the other touched
  `brutex/` files (`app.py` wiring + the `calendars/nse.py` comment-only note) ride their existing suites
  (NIT-3 wording fix, DEC-SURFACE-TRIAL-001 — the prior "all five touched modules" conflated the coverage-verified
  set with the six-member touched set); ruff/ruff-format/`mypy --strict`/`pyright` clean; ENGINE
  UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate change.
  Prior merged: **DEC-FETCH-PER-WINDOW-GATE-HONESTY-001, MIGRATION.md #1132** — the
  fetch-per-window CI bench gate (`tests/bench/test_fetch_per_window_guarantee.py`, #1026) now measures the PRODUCTION
  quantity instead of a flattering lab quantity: a verified forensic audit of a real production pull measured `2.59`
  vendor fetches per `(contract, interval)` window while the gate read `1.198` and PASSED — because its replay range
  (30 days) tiled into ONE 30-day vendor chunk (the 1-minute tier of `docs/groww/api-reference/03-backtesting.md`),
  whereas the REAL default windows do not fit one chunk: option `expiry−30d → expiry+1d` = 31-day span → 2 chunks
  (DEC-FNO-LOOKBACK-SPLIT-001 #894 / the strict window law DEC-FNO-FAR-EXPIRY-WINDOW-001 #1127), future `expiry−60d →
  expiry+1d` = 61-day span → 3 chunks (DEC-FUTURES-LOOKBACK-60D-001). The gate measured only the empty-retry
  amplification, structurally blind to chunk tiling, so its "8h fits" arithmetic did not hold in production. REWRITE
  (bench-test + docs ONLY — ZERO `brutex/` source change; the pull's fetch behaviour is byte-identical): the gate now
  pins TWO quantities — (1) the ORIGINAL single-chunk LAB quantity stays independently pinned at ≤ 1.2 (#1026's actual
  lever, the adaptive empty-retry bounding; measured `194/162 = 1.198`, teeth test kept at baseline `2.580`), and (2) a
  NEW PRODUCTION-shaped replay derives every window from the REAL `default_fno_minute_window` law + the REAL
  `plan_candle_windows` chunk planner (162 options → 2 chunks each, 1 future → 3) through the real ingest spine,
  measuring `391/163 = 2.399` fpw against a ceiling DERIVED from the replayed chain (`(162·2·1.2 + 3)/163 ≈ 2.404`)
  with a structural floor (`≈ 2.006`) + an option-chunks-≥2 pin so the gate can never silently degrade back to the
  flattering single-chunk shape. HONEST 8h ARITHMETIC (Golden Rule #10): at the worst-case 3 r/s (CLAUDE.md §2 Rule 6),
  8h = 86,400 fetches covers `86,400/2.399 ≈ 36,018` windows — BELOW the audited 48,145-window production scope
  (#1026's forensic audit), so the 8h pull does NOT fit at the honest rate; the TRUE achievable wall-clock for that
  scope is `≈ 10.7 h`, and the gate ASSERTS the infeasibility (`86,400/fpw < 48,145`) so a genuine structural
  improvement fails the pin LOUDLY and forces a conscious re-derivation (the forcing function the stale "1.2 / fits
  8h" claim lacked). The 48,145 reference is the PRE-#1127 audited scope — the far-contract skip has since shrunk the
  per-pull scope by an UNMEASURED amount (a fresh production audit would be needed to claim a number, NOT claimed).
  ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), F&O lake REFERENCE_ONLY, no scope/vocab/rate change; ruff/ruff-format/
  `mypy --strict`/`pyright` clean on the touched test; the #1026 blurb below is reconciled where it claimed "≤ 1.2 /
  FITS".
  Prior merged: **DEC-FETCH-PARSER-HARDENING-001, MIGRATION.md #1038** — PR-B of
  the triaged fetch-path edge-case sweep (sibling of PR-A DEC-INTERIOR-GAP-DETECT-001): three CONFIRMED non-crash
  fetch-path resilience gaps closed. HONEST SEVERITY: LOW-probability vendor-corruption / resilience guards — NONE is a
  crash or a HIGH bug (the alleged non-`Mapping`-payload HIGH was DISPROVEN against the real `growwapi` SDK). SCOPE NOTE
  (Golden Rule #2): the original plan's #5 (negative-price reject) + #8 (nan/inf loud-reject) were DROPPED because main
  merged DEC-ADVERSARIAL-HARDENING-001 (#1029, PR #993) FIRST under the authoritative OPPOSITE philosophy (negative price
  treated like zero → graceful SKIP via `has_no_price`; `to_price` already rejects non-finite) — so they are SUPERSEDED;
  `candle_fields.py`/`models.py`/`parser.py` are reset to main verbatim, never re-implemented or fought. THREE kept fixes:
  (1) future-timestamp drop (#9) — NEW pure `drop_bars_after(bars, window_end)` in `adapters/ingest_common.py` drops a bar
  whose IST date is STRICTLY AFTER the window end (on-boundary KEPT), wired into BOTH the spot + F&O tails; (2) lake-OOM on
  the MERGE write (#4) — `parquet_writer._merge_write`'s read-modify-write body wrapped in the COMMON `lake_write_guard()`
  so an ENOSPC/OOM during the MERGE (not just the FRESH render of #885) maps to a graceful `LakeWriteHalt`, a non-ENOSPC
  OSError NOT mis-escalated; (3) corrupt-partition async isolation (#10) — NEW `fetch_pipeline._is_pool_isolatable(exc)` +
  `_process_one` QUARANTINES a per-symbol `BruteXError` (e.g. a `WriterError` from one corrupt partition) → logs
  `fetch_pipeline.window_quarantined` + advances WITHOUT tripping the pool, while a global-stop (`LakeWriteHalt`/quota) or a
  non-`BruteXError` genuine code bug STILL trips (mirrors the sync spine's per-symbol quarantine; a unit test pins the
  predicate against `run_ingest._GLOBAL_STOP_ERRORS` so they can't drift). HONEST envelope: no data lost (a quarantined
  window's ledger row stays unsettled → re-pulled, keep-first idempotent sink); ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX),
  F&O lake REFERENCE_ONLY, no scope/vocab/rate change; all 5 touched source files at 100% line+branch; ruff/ruff-format/
  `mypy --strict`/`pyright` clean. Also de-flaked the pre-existing main hung-write test (widened the 0.2 s write-deadline +
  1.0 s drain-join proportionally, same fix as PR-A). NEW `TestDropBarsAfter` + future-bar-drop tests (both spines) +
  `test_merge_loop_oom_halts` + `TestIsPoolIsolatable` + the end-to-end quarantine integration test.
  Prior merged: **DEC-MOVERS-CALENDAR-TIERS-001, MIGRATION.md #1034** —
  the `/movers` Options Near/Next/Far + Weekly/Monthly tiers are now driven by the AUTHORITATIVE vendor expiry calendar
  (`registry.duckdb`) instead of the SPARSE lake-partition set, so a sparsely-pulled lake no longer mislabels a tier (the
  operator REPORTED a wrong Far expiry on a lake missing intermediate July weeklies). FIX 1 (PRIMARY · correctness): NEW
  `movers_options.load_registry_expiries` reads the FULL per-underlying calendar via `RegistryStore.list_expiries`
  (populated by the metadata phase from the vendor `get_expiries`); `_tier_expiry_by_underlying` gains a `calendar_expiries`
  map + a pure `_tier_universe` that classifies tier+track over the calendar's `>= on_date` set when present (`_monthly_expiries`
  max-per-month is correct once fed the full July {07,14,21,28-Jul} → 28-Jul monthly, 07/14/21 weekly). GRACEFUL FALLBACK
  (Golden Rule #1): an absent/empty/unreadable `registry.duckdb` (a CLI-only lake, or a `RegistryError`) → the current
  lake-partition derivation, so those lakes are never broken. HONEST NO-DATA: a calendar tier with ZERO pulled candles sets
  `OptionsCandidates.calendar_only_expiry` + an honest "on the vendor calendar … but NO option contract candles pulled" note,
  never a wrong serial expiry. The route memoises the calendar per write-generation (`_options_calendar_cache`, REUSES
  `MoversSnapshotCache`) keyed per exchange. FIX 2 (DEC-MOVERS-SORT-RESET-001 · frontend): the metric-pill onclick now resets
  `state.sort` + `renderHead()` so a manual VOLUME sort + the stale ▼ arrow reset on a metric-tab switch (the asset-tab handler
  already did). FIX 3 (DEC-MOVERS-MIN-VOLUME-001 · opt-in floor): a NEW `?min_volume=` floor (route default 0 = byte-identical)
  filters the "Top Volume" ranking `volume >= min_volume` via ONE COMMON `normalize_min_volume` + `value_meets_volume_floor`
  shared by all three `rank_volume`s (spot/futures/options), threaded route → `build_*_payload` at RANK time (re-ranks the SAME
  cached candidate set, never a re-scan), echoed in the payload + note + a `movers.html` input. FIX 4 (DEC-MOVERS-WARM-GRID-001
  · the slowness fix): the post-pull warmer pre-warms the WHOLE default-day grid (NEW pure `spot_warm_grid`/`futures_warm_grid`/
  `options_warm_grid` = 3 + 9 + 27 = 39 keys, {NSE,BSE,all}×{near,next,far}×{all,weekly,monthly}) REUSING the EXACT
  `candidate_key` + `compute_*_candidates` + the FIX-1 calendar, so any default-day options coordinate is an O(1) cache hit;
  `DEFAULT_MOVERS_CACHE_CAPACITY` raised 64 → 256 so the grid PLUS live coordinates fit. HONEST envelope: calendar tiers are
  AUTHORITATIVE only when the metadata phase ran (graceful lake fallback proven); `min_volume=0` byte-identical; the warmer is
  best-effort + O(1)-cached per generation (the FIRST open per coordinate still pays one real scan). READ-ONLY dashboard +
  pull-PLANNING warm — no engine/lake-schema/stored-bar/scope/vocab/rate-cap change, F&O lake stays REFERENCE_ONLY. The five
  touched movers modules ALL 100% line+branch (389 movers tests green); ruff/ruff-format/`mypy --strict`/`pyright brutex tests`
  clean. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX).
  Prior merged: **DEC-INTERIOR-GAP-DETECT-001 / DEC-PARTIAL-WINDOW-REPULL-001 /
  DEC-SINK-DEDUP-COUNT-001, MIGRATION.md #1031** — PR-A of the triaged fetch-path edge-case sweep: a SUCCESS spot window
  with a CONFIRMED interior multi-session hole OR a trailing truncation is DEMOTED to a re-pullable ledger status
  (BOUNDED — it settles SKIPPED after `EMPTY_TERMINAL_THRESHOLD` flagged pulls so the universe pull still converges, and
  the stored bars are always KEPT), and the sink reports the DEDUPED bars-added count as a SEPARATE `bars_net_new` field.
  HONEST SEVERITY: these are MED/LOW data-completeness-honesty fixes — NONE is a crash or a HIGH bug (the adversarial triage
  DISPROVED the alleged HIGH non-`Mapping`-payload whole-pull-crash against the actually-installed `growwapi` SDK). THREE
  fixes: (1) interior-gap (#1, MED) — NEW pure `brutex/data/interior_gap_detect.py` flags ONLY a contiguous run of
  bar-LESS NSE trading sessions `> INTERIOR_GAP_TOLERANCE_SESSIONS` (default 5) strictly INTERIOR to the covered span, so
  a low-liquidity-but-continuous window is NEVER false-flagged (signal = a long contiguous hole, NOT density;
  out-of-calendar years ABSTAIN); (2) partial-window promote (#3, MED) — `partial_window_detect.observe_partial_window`
  now RETURNS its verdict (back-compat) so the spot tail DEMOTES a confirmed truncation, all the prior
  covered-year/liquidity/live-edge/at-expiry guards preserved; (3) dup-bars accounting (#6, LOW) — NEW
  `WriteResult.bars_added` (deduped) summed into a NEW `SinkAppendResult.bars_net_new` (`data/sink.py`), while
  `bars_appended` STAYS the input count — so a re-pull of already-stored bars reports `bars_net_new=0` with NO downstream
  `>0`-gate change. MECHANISM: a NEW `degraded` flag threaded `classify_window_status` →
  `WindowLedger.record(degraded=, degraded_reason=)` records a demoted SUCCESS as FAILED+reason and BUMPS the
  `empty_streak` (REUSING the genuine-EMPTY bounded-terminal machinery); the DEMOTION is OPT-IN (a
  `detect_completeness_gaps` flag set True ONLY at the real-vendor production sites — the dashboard + CLI live pull;
  a `--dry-run`/synthetic pull leaves it OFF so it never false-reds) AND gated to a DENSE INDEX
  (`instrument.is_index`) on the FETCHED `1minute` interval — the ~750 illiquid REFERENCE_ONLY spot underlyings + the
  derived `1day` are WARNed (observe-only) but never re-pulled. HONEST envelope: SPOT-INDEX spine ONLY (the F&O
  `option_ingest` partial-window observe stays observe-only — an explicit non-goal); ENGINE UNCHANGED
  (NIFTY/BANKNIFTY/SENSEX), F&O lake REFERENCE_ONLY, no scope/vocab/rate change; all 7 touched files at 100% line+branch;
  ruff/ruff-format/`mypy --strict`/`pyright` clean. TWO hostile-review MAJORs folded (the dedup-gate flip → separate
  `bars_net_new`; the illiquid-universe demotion → `instrument.is_index` gate). NEW tests: `test_interior_gap_detect.py`
  (17) + `TestDegradedDemotion` + `TestBarsAddedDedup` + `TestParquetBarSinkDedupCount` + `TestIngestSpotCompletenessRepull`
  (each fails-before / passes-after, incl. the non-index-not-demoted regression). Prior merged: **DEC-FETCH-PER-WINDOW-GUARANTEE-001, MIGRATION.md #1026** —
  adaptive strike-chain bounding drives the F&O option-pull fetches-per-window from a MEASURED `2.54` toward `~1.2`,
  making the operator's worst-case-`3 r/s` 8-hour pull a MEASURED CI GATE — with NO data lost. THE ARITHMETIC: at the AIMD
  soft-floor `3 r/s`, `3×3600×8h = 86,400` fetches are available in 8h; a production pull spent `2.54` fetches per
  `(contract, interval)` window (122,243/48,145), so the target fit only if fetches-per-window dropped toward ~1.2.
  MEASURED ROOT (re-derived in the bench): modelling `avg = (1−E)·1 + E·3` against 2.54 gives an empty-window fraction
  `E ≈ 0.77` — the **3× in-pull EMPTY-retry** firing on every clean-but-empty deep-OTM strike that never traded is the
  DOMINANT amplifier (NOT just a "15.8% deep-OTM" subset). FIX (pull-PLANNING / per-contract retry-budget only — NO
  lake-schema/stored-bar/rate-cap/vocab change; F&O lake stays REFERENCE_ONLY): NEW pure
  `brutex/adapters/groww/strike_chain_bound.py` `build_empty_retry_budget` groups the CE/PE chain by
  `(underlying_symbol, expiry)` (NOT expiry alone — so a mixed all-cached pull never pools two underlyings sharing one
  monthly expiry into a bogus median; hostile-review MAJOR folded), finds each chain's MEDIAN strike (a robust
  ATM/liquidity-center proxy needing NO live spot fetch), and gives a strike OUTSIDE a `±band`-STRIKES envelope
  (grid-rank, default `±12`, env `BRUTEX_FNO_STRIKE_BAND`) a REDUCED empty-retry budget (default `1`, env
  `BRUTEX_FNO_DEEP_EMPTY_RETRY`, clamped `[1, full]`) — so a confirmed-DEAD far-OTM strike costs ONE fetch to confirm
  empty instead of three. A too-small chain, every FUT, and every in-band option keep the FULL budget; the map is SPARSE
  so a `None` map is byte-identical to today. Threaded an optional `empty_retry_by_symbol` through
  `run_groww_option_ingest` → `ingest_option_contract`'s EXISTING `empty_retry_attempts`, built in `historical_pull`
  over the whole chain. CORRECTNESS — NO DATA LOST (Golden Rule #1): the reduction keys ONLY off a CLEAN 200-zero-candles
  return, NEVER off a raised retryable error — by construction the empty-retry loop re-iterates only when the fetch
  RETURNS empty bars, while a transient (429/timeout/5xx `GrowwServerError` #883/empty-body-throttle `GrowwEmptyResponseError`
  #854/network) is RAISED into the SEPARATE, UNTOUCHED `max_attempts` ladder and never decrements the empty budget; the
  first non-empty fetch always wins, so a deep strike that DOES return data is stored exactly as before; a near-money
  strike is never reduced; "if uncertain, fetch" (`< 1` clamps UP to 1). RE-PULL NET: a reduced-budget empty makes
  `fetch_count == 1 < CONFIRMED_EMPTY_MIN_FETCHES(2)` so the OLD-gap fast-settle does NOT fire — the window stays
  re-fetchable on a later idempotent pull (keep-first sink + durable ledger), so even a vendor-bug 200-empty is
  recoverable [SETTLE-RECONCILE — DEC-DEAD-STRIKE-LEDGER-SETTLE-001, MIGRATION.md #1172 (pack #1171): SUPERSEDED for an OLD window —
  the old-gap settle is now BUDGET-AWARE, accumulating clean-EMPTY confirmations ACROSS pulls (`prior.empty_streak +
  in_pull_fetches` vs the SAME 2-confirmation bar), so TWO independent pulls' clean empties settle a reduced-budget old
  window SKIPPED; a repeated vendor-bug 200-empty on an OLD window is thereafter recoverable via `brutex
  reopen-skipped`, not an automatic re-pull; RECENT windows keep the conservative streak, SUCCESS resets, a
  suppressed/transient empty never counts]; the #919 all-empty + #974 partial-window detectors stay ACTIVE. THE GUARANTEE = a MEASURED CI BENCH GATE
  (`tests/bench/test_fetch_per_window_guarantee.py`): replays a representative NIFTY chain end-to-end through the REAL
  spine against a fetch-COUNTING SDK and ASSERTS fetches-per-window ≤ 1.2 AND `86,400/fpw` ≥ the chain's windows (a
  regression FAILS CI), plus a teeth test that the SAME chain WITHOUT bounding is ABOVE the ceiling and stores the SAME
  bars. MEASURED before/after: baseline `2.580` fpw (8h covers 33,485 windows) → bounded `1.198` fpw (8h covers 72,148
  windows), `0.46×` the fetches, 34→34 bars (no data lost). 8h-FEASIBILITY VERDICT: FITS at 3 r/s for the measured scope
  (~72k window coverage ≥ ~48k target; the liquid-only universe ~42k < 86,400). [GATE-HONESTY RECONCILE —
  DEC-FETCH-PER-WINDOW-GATE-HONESTY-001, MIGRATION.md #1132: the `≤ 1.2` figure + this FITS verdict were measured on a
  replay whose windows fit ONE 30-day chunk — a LAB quantity; the production DEFAULT windows tile into 2 chunks
  (options) / 3 (futures) and a forensic audit measured the real pull at `2.59` fpw. The gate now ALSO replays the
  production shape (`2.399` fpw vs a chain-derived ceiling `≈ 2.404`) and the honest verdict is DOES-NOT-FIT: 8h @
  3 r/s covers ≈ 36,018 windows < the audited 48,145-window scope (true wall-clock ≈ 10.7 h). The empty-retry lever
  itself (this entry's actual fix) remains real + independently pinned at ≤ 1.2 on the single-chunk quantity.]
  HONEST (Golden Rule #10): a HEURISTIC
  band not an oracle (a live strike outside the band loses 2 glitch-recovery attempts but is STILL fetched once + stored
  if it returns data); the 8h projection is `total_fetches/3 r/s`, a wall-clock LOWER bound assuming the controller
  sustains 3 r/s (Rule 6 — provable), NOT a claim every real pull finishes in 8h; the empty-RETRY is bounded, NOT a
  plan-time strike SKIP (a walk-outward short-circuit is incompatible with the parallel fan-out + would slow the pull,
  so the dominant measured lever was chosen). `strike_chain_bound.py` (88/24) + `option_ingest.py` (310/82) +
  `historical_pull.py` (257/54) ALL 100% line+branch; NEW unit/property + a per-permutation correctness suite
  (clean-empty→bounded · 429/timeout/5xx/empty-body-throttle→retried+NOT counted · near-money→never reduced ·
  transient-empty-then-data→strike not skipped + data captured · re-pull→re-fetches · #919 detector still fires) + the
  bench gate; ruff/ruff-format/`mypy --strict`/`pyright` clean on every touched file. ENGINE UNCHANGED
  (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate change.
  Prior merged: **DEC-LAKE-READ-O1-AUDIT-001, MIGRATION.md #1025** —
  a full audit of every BruteX DB-FETCH / lake-READ entry point for O(1)/O(page) latency. The verdict: the prior
  O(1)/O(page) PRs (#860/#863/#864/#867/#893/#895/#896/#933) have ALREADY bounded essentially every hot read — the
  engine bar stream is O(1)/bar (catalog-window-indexed `Catalog.get_bars`), `LakeOptionBarSource.load_pair` is O(1)/leg
  after two read-through caches, the spot 1m→1d resample read is pulled-window-month-bounded (`ist_span_partition_files`),
  every movers per-symbol read is month/day-glob-bounded (`lake_day_glob`) + the candidate scan is split-and-cached
  (`MoversSnapshotCache`), the completeness SPOT scan rides `_summary_cache`, the `/universe` reads are O(1) behind
  `master_revision`, and the `/db` candle/window/discovery/registry reads are all cached or `LIMIT`-pushdown-bounded.
  The ONE gap found + fixed: `db_view.query_option_candles` (the `/db` expired-options tab) computed its page `total`
  via `SELECT COUNT(*) FROM read_parquet(<series-glob>)` on EVERY paging click with NO count cache — the EXACT O(files)
  footer-scan `query_candles` fixed in #867 but the options pager never got. FIX (READ-ONLY `/db` viewer accelerator —
  ENGINE UNCHANGED NIFTY/BANKNIFTY/SENSEX, NO store/schema/scope/vocab/rate/write-path change): a NEW pure
  `db_view.option_candle_count_key(series, start, end)` (the options twin of `candle_count_key`; excludes
  `limit`/`offset`/`descending`/`columns` — the paging-invariance — with an `opt|` prefix so it NEVER collides with a
  spot key) + `query_option_candles` gains the OPTIONAL `count_cache`/`generation` (REUSING the #867 `CandleCountCache`
  Protocol, NO new class); the `/api/v1/db/options` route passes the SAME COMMON `_candle_count_cache` (`MoversSnapshotCache`)
  the spot pager uses + `_summary_cache.generation`, so a prev/next click is an O(1) hit + a pull bumps the generation →
  recount (never stale, Golden Rule #1). MEASURED: a deep 72-month series 19.02 ms/click → 13.76 ms/click (1.38×, the win
  grows with depth). HONEST envelope (identical to #867): a cached-total HIT is O(1) in series depth (bench-pinned FLAT,
  COUNT-once proven); the FIRST view per generation + the page DATA query stay O(files) (true O(page) needs a write-time
  lake manifest, NOT claimed); the empty series early-returns `total:0` without caching. `db_view.py` 100% line+branch
  (718/0 + 232/0); NEW `test_dashboard_db_option_count_cache.py` (11) + bench `test_db_option_count_cache_o1.py` + a
  `/db/options` route integration test; mypy --strict / pyright / ruff clean.
  Prior merged: **DEC-RATE-WIDGET-FNO-HONEST-001, MIGRATION.md #1022** —
  the live PULL RATE widget's "achieved r/s" is now HONEST during the metadata + F&O phases, and the stall verdict is no
  longer blind to an issued-but-failing pull. ROOT (confirmed by deep log+code audit): the SPOT phase feeds the widget the
  transport `CallMeter`'s MEASURED completed-rate, but the metadata + F&O phases have no rate-observe seam, so
  DEC-PULL-RATE-WIDGET-VISIBILITY-001 (#1000)'s `RatePanelSampler` kept the widget live by SYNTHESIZING
  `meter_snapshot = {"effective_rps": recent_minute / 60}` — the controller's ADMISSION count ÷ 60 — into the panel's
  "achieved" (measured-completed) slot. That (a) OVER-reported vs the true completed rate (log: 204/min admitted shown as
  3.4 r/s while real completed ~169/min ≈ 2.82 r/s) and (b) BLINDED `build_pull_rate_view`'s `stalled = effective_rps <=
  floor` to an issued-but-ALL-FAILING pull (it would read "flowing 3 r/s" even with every fetch failing — a fake-alive
  illusion, Golden Rule #1). FIX (the HONEST MINIMUM — option 3, because the F&O `CallMeter` is genuinely UNREACHABLE from
  the sampler seam: it is built fresh inside `build_live_pool`→`_build_live_client`, the pragma-no-cover live client factory
  invoked deep in `run_groww_option_ingest`, and the `SessionRateController` holds ONLY the `RateController`, never that
  meter — so option 2 would need plumbing a publish callback through the 4-layer pragma-no-cover F&O spine, not feasible
  cleanly): the sampler STOPS fabricating a completed-rate and pushes an explicit `meter_present=False` sentinel;
  `build_pull_rate_view` gains a `meter_present` param (default `True` — the spot/meter-fed + CLI-bridge contract is
  byte-unchanged) that splits the verdict — `meter_present=True` (spot) reads the MEASURED `effective_rps` (so an
  issued-but-failing pull whose completed-rate sits at/below the floor reads STALLED — the fake-alive case is now CAUGHT),
  `meter_present=False` (metadata/F&O) keeps `req_per_sec_achieved` an HONEST 0 + falls back to the controller's ADMISSION
  signal (`recent_minute == 0` ⇒ stalled/idle, any admissions ⇒ flowing); the admission per-minute average is surfaced in a
  NEW distinctly-labelled `req_per_min_rate` field (`bx-rate-monitor.js` shows it as "req/min rate · admission avg
  (issued ÷ 60)", explicitly NOT a measured completed rate), never the "achieved" slot. The `RateLimitPanel` gains a
  `meter_present` flag (default `True`) threaded `update_throughput` ← `make_rate_observer` (a real spot meter snapshot has
  no `meter_present` key → defaults `True`; the sampler's `{"meter_present": False}` flips it) → the `/api/v1/pull/rate`
  route (`panel_meter_present` into `build_pull_rate_view_merged`; the CLI bridge stays `bridge_meter_present=True`).
  HONEST: READ-ONLY (no pacing/rate-cap change, fetch rate byte-unchanged); the widget stays LIVE in F&O (the #1000
  visibility win preserved) but now tells the TRUTH — "achieved" is the measured completed rate where a meter exists, an
  honest 0 (with a separate admission average) where it does not, never a fabricated middle figure; the F&O phase detects
  an IDLE pull (no admissions) but NOT an issued-but-all-failing one (that needs publishing the F&O meter — an explicit
  FOLLOW-UP, NOT claimed); the SPOT phase DOES catch issued-but-failing now (regression-pinned). `pull_rate_view.py` +
  `rate_sampler.py` BOTH 100% line+branch; touched `app.py` lines covered; ruff/ruff-format/`mypy --strict`/`pyright` clean.
  ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate change.
  Prior: **DEC-EMPTY-DETECTOR-PRODUCTION-WIRING-001, MIGRATION.md #1014** —
  the whole-underlying "silent-empty / silent-vanish" detector (DEC-SUSPICIOUS-EMPTY-FLAG-001 #919) is WAKED in the real
  production F&O pull. It was DEAD in every live pull: its only call site in `run_groww_option_ingest` is gated
  `if fetch_pipeline is None:` (the correct in-async-mode skip — a producer-resolved per-pair `IngestReport` reads
  `bars_appended == 0` BEFORE the background consumer folds the real counts), and the production pull
  (`dashboard/historical_pull.py::pull_contract_candles`) ALWAYS wires a non-None `fetch_pipeline` (default-ON), so
  the BAJAJ-AUTO/M&M failure mode the detector exists to catch was uncatchable in production. FIX (COMMON · OBSERVE-ONLY ·
  best-effort): NEW `option_ingest.emit_underlying_all_empty(pairs, contract_list, *, pull_id=None)` — the ONE guarded
  scan→log helper (runs `scan_underlying_all_empty` then `_log_underlying_all_empty`, wrapped so a scan error logs
  `pull.underlying_all_empty_error` and NEVER breaks the pull; `pull_id` defaults to `current_pull_id()`); the in-driver
  synchronous call is refactored onto it (byte-identical). `pull_contract_candles` calls it POST-DRAIN, right after
  `_reconcile_phase_with_pipeline` folds the REAL durable per-pair `bars_appended` back into `minute.merged_pairs` (the
  only point those counts exist), guarded `if fetch_pipeline is not None:` (the sync path already emitted in-driver — no
  double-warn). HONEST: pure DETECT+WARN — no return/ledger/engine change; the #919 false-positive guards are reused
  verbatim; F&O lake stays REFERENCE_ONLY; ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate change. NEW
  `test_silent_vanish_underlying_warns_in_production_pipeline_pull` (a LIVE pipeline-wired pull over an all-empty NIFTY
  chain emits EXACTLY one `pull.underlying_all_empty` naming NIFTY) + 4 `emit_underlying_all_empty` unit tests;
  `option_ingest.py` + `historical_pull.py` stay 100% line+branch; ruff/ruff-format/`mypy --strict`/`pyright` clean.
  Prior: **DEC-UNIVERSE-PULL-DATERANGE-001, MIGRATION.md #1007** —
  the `/universe` "Pull · full pipeline" section RE-GAINS a from/to spot DATE PICKER, now PROPERLY WIRED through the
  `/pull-pipeline` route (resolving the honesty concern of DEC-UNIVERSE-PULL-CONSOLIDATE-001 #920, which had removed the
  picker BECAUSE the route ignored from/to). The backend already supported explicit dates everywhere else
  (`resolve_candle_range` floor `GROWW_HISTORY_FLOOR=2020-01-01` + prev-day cap + `from>to`→ValueError; `pull_all_spot(from_date,
  to_date, …)`; `default_date_range` UI pre-fill); the ONLY gap was the `/pull-pipeline` route hard-coding `from_date=None,
  to_date=None` + `_resolve_metadata_years(None, None)`. FIX (READ-ONLY / pull-PLANNING only — NO lake-schema/stored-bar/rate/
  vocab/engine change): (1) FRONTEND `universe.html` — the static "window · 2020 → prev-day (fixed)" chip becomes a `.daterange`
  group with `id="pipeFrom"`/`id="pipeTo"` (`type="date" min="2020-01-01"`, max=today-IST, value pre-filled by the SHARED
  `_fill_default_dates`→`default_date_range`), styled like the `/db` picker; a `pipelineDateQuery()` helper appends
  `&from_date=`/`&to_date=` to the pull URL ONLY when non-blank (blank ⇒ default), folded into both the scoped + bare onclick
  URLs (DOM built via static template/`textContent`, NEVER `innerHTML` — CodeQL XSS guard). (2) BACKEND `/pull-pipeline` route
  — NEW `from_date`/`to_date` Query params, validated EAGERLY via `resolve_candle_range(...)` in a `try/except ValueError→400`,
  threaded into the SPOT phase `pull_all_spot(from_date, to_date, …)` + `years = _resolve_metadata_years(from_date, to_date)`;
  the F&O HISTORICAL phase deliberately keeps its per-contract default lookback (futures expiry−60d / options expiry−30d) — an
  over-pull-safety scoping decision (a wide explicit range over the whole options chain would pull far too much). DEFAULT-
  PRESERVATION (pinned): blank ⇒ no params ⇒ route passes None ⇒ byte-identical to the 2020→prev-day default. `app.py` stays
  100% line+branch (the validation try/except runs in the route tests; the `_work` body rides the existing `# pragma: no cover`);
  NEW route tests pin blank-default / explicit-threaded / reversed-or-non-ISO-400 / below-2020-clamped, and the prior
  picker-removed assertions are flipped to picker-re-added. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate change.
  Prior: **DEC-PULL-RATE-WIDGET-VISIBILITY-001, MIGRATION.md #1000** —
  the live PULL RATE widget no longer goes BLANK mid-pull. TWO confirmed gaps closed (observe-only — NO pacing change):
  (1) FRONTEND — the self-injecting widget `<script defer src="/static/bx-rate-monitor.js">` (DEC-LIVE-RATE-MONITOR-001)
  loaded ONLY on `v29.html`+`universe.html`, so it was ABSENT from the pages a pull is WATCHED on (coverage/spot_coverage/
  fetch_windows/completeness/db/movers/results/reports/expired_options); now injected before `</head>` in EVERY template
  that lacked it (self-contained + idempotent, guard at `bx-rate-monitor.js:33`, so site-wide is safe). (2) BACKEND (the
  important one) — the `RateLimitPanel` feeding `/api/v1/pull/rate` was fed ONLY on the SPOT phase's per-window `on_window`
  observe (`make_window_observer`); the METADATA (`pull_expiries_contracts`) + long F&O/historical (`run_groww_option_ingest`)
  phases had NO rate-observe seam, so ~12 s (`STALE_AFTER_S`) after the ~42 s spot phase the route reported `idle` for the
  ENTIRE ~38-min F&O phase even while the SHARED `RateController` was pacing real fetches. FIX (ONE COMMON observe-only
  mechanism — REUSE `make_rate_observer`, NO copy; a periodic snapshot chosen OVER threading an `on_window` seam through the
  4-layer F&O spine because it ALSO covers the metadata phase + needs ONLY the shared `SessionRateController`, not the F&O
  call-meter built inside the candle factory): NEW `brutex/dashboard/rate_sampler.py::RatePanelSampler` — a context-managed
  daemon that every 2 s reads the controller via the NEW `SessionRateController.peek()` (returns it IFF a real pull built one,
  else `None` — NEVER builds, so a dry-run conjures no live controller + the panel stays honestly idle) and pushes its
  `snapshot()` to the panel through `make_rate_observer(state)`; achieved r/s = the controller's MEASURED `recent_minute / 60`
  (a real per-minute average, openly so — the snapshot lacks the call-meter `effective_rps`) so the flow/stall verdict is
  honest not a fake-0 "stalled". The `/pull-pipeline` route wraps the whole `run_full_pipeline` in the sampler. HONEST:
  observe-only (reads controller, writes panel; touches no pacing — fetch rate byte-unchanged); O(1) + best-effort per tick
  (a tick error is swallowed/logged, never crashes the pull); the `_work` wiring rides the existing `# pragma: no cover`,
  the sampler PRIMITIVE is separately tested at 100%. NEW `test_dashboard_rate_sampler.py` (8) + `peek()` tests +
  `TestFnoPhaseLiveness` (the bug PINNED: idle-after-spot regression + the FIX proving `/api/v1/pull/rate` returns
  `flowing` during the F&O phase) + every-template widget test. `rate_sampler.py` + `pull_controller.py` BOTH 100% line+branch
  (48/4 + 501/118); ruff/ruff-format/`mypy --strict`/`pyright` clean on every touched file. ENGINE UNCHANGED
  (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate change. Prior on `claude/fetch-hung-write-deadline`:
  **DEC-FETCH-HUNG-WRITE-DEADLINE-001, MIGRATION.md #999** — the
  durable lake write (`sink.append` → `os.fsync` + atomic `os.replace`) is now BOUNDED by a wall-clock DEADLINE so a
  HUNG write (a wedged fsync/replace on a stalled external SSD / NFS mount that NEVER raises) can never permanently
  stall the fetch lane. The async fetch pipeline (#951/#998) decouples the write onto a bounded-queue consumer, but the
  write ITSELF had NO timeout — `run_with_deadline` bounds only the FETCH call, `partition_lock`'s 120 s bound only the
  `flock` ACQUIRE — so a non-raising hang blocked the consumer forever, filled its queue, and wedged the PRODUCER on
  `submit` back-pressure at 0 r/s while the controller still "allowed" 8 (the back-pressure loop breaks only on
  `_stopped`/`_draining`, which a non-raising hang never sets). FIX (REUSE the COMMON `run_with_deadline` freeze guard,
  NO copy): NEW `brutex/adapters/groww/write_deadline.py::append_with_deadline` runs `sink.append` under
  `GROWW_WRITE_DEADLINE_S` (90 s, env-tunable `BRUTEX_GROWW_WRITE_DEADLINE_S`) and on a hang raises the TRANSIENT
  `WriteDeadlineExceeded` (a `DataLakeError`, NOT a `LakeWriteHalt` — NOT a pull-wide stop); BOTH spines'
  `_process_window_tail` (spot `ingest.py` + F&O `option_ingest.py`) catch it ALONGSIDE `PartitionLockTimeout` in ONE
  defer block — record the window FAILED, return a transient, DEFER to the next pull — so the consumer keeps processing
  and the producer is RELEASED (per-window graceful deferral, the pipeline is NOT tripped; the defer folds onto
  `tail_transients` so the across-pull retry re-pulls it). HONEST (Golden Rule #10): the abandoned write runs in a daemon
  thread, so a wedged C-level fsync CANNOT be killed — the guarantee is "the FETCH LANE is never blocked + the window is
  deferred + re-pulled", NOT "the zombie write thread is terminated"; a persistently-wedged volume defers the same
  window every pull until it recovers, never silently losing data (idempotent keep-first sink + durable ledger). The two
  `lock_deferred` log events unify to `write_deferred` (carrying `error_type` to distinguish the lock-acquire vs the
  hung-write case). NEW `test_groww_write_deadline.py` (16) + an ADVERSARIAL `TestHungWriteDoesNotBlockFetchLane` (a
  hung-first-write pull that PROVES forward progress within a 20 s budget, the FAILED-deferred window, durable later
  windows, clean drain — and DEADLOCKS without the fix, verified by reverting); `write_deadline.py` + `errors.py` 100%
  line+branch, both spines + `constants.py` stay 100%; ruff/ruff-format/`mypy --strict`/`pyright` clean. ENGINE UNCHANGED
  (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate change. Prior on `claude/pull-health-wiring-jlzt1n`:
  **DEC-PULL-HEALTH-WIRING-001, MIGRATION.md #935** — the
  live pull-health monitor shipped DORMANT in #893 (DEC-PULL-HEALTH-MONITOR-001 — the `PullHealthMonitor` + the
  `_observe_rate(..., monitor=None)` seam, but NO production build site constructed one, so it watched NOTHING on a
  real pull) is now BOUND at the dashboard live-pull build site, so it actually runs on every real pull — the same
  dormant-feature fix #934 just applied to the raw-data-integrity hook. NEW best-effort
  `pull_controller._build_pull_health_monitor(controller)` wires a `PullHealthMonitor` to the process-global
  `MetricRegistry` (`/metrics` gauges) + a `SweepAlerter.pull_degraded` Telegram alert callback (silent no-op when
  Telegram is disabled) + the resolved `resolve_pool_size()`, returning `None` for a dry-run (no controller) and
  swallowing any construction error → `None` so binding it can NEVER block/slow/crash a pull; BOTH `pull_all_spot` +
  `pull_candles` build it after `_live_rate_runtime` and thread it into their `_observe_rate(..., _pull_health_monitor)`
  tick (the seam already accepted `monitor` since #893 — this is the BINDING, not the seam). HONEST: thresholds are
  operator-tunable HEURISTICS not vendor limits, a breach is an OBSERVATION + alert not auto-remediation; wires the
  DASHBOARD pull path (the only path with a rate-observe tick) — the CLI `brutex fetch` path has no `_observe_rate`
  seam, so a CLI monitor is a SEPARATE follow-up, NOT claimed. `_build_pull_health_monitor` 100% line+branch (all
  three branches); NEW `test_dashboard_pull_health_wiring.py` (5 tests incl. an end-to-end breach → one `pull_degraded`
  push through the real `_observe_rate` seam); ruff/ruff-format/`mypy --strict`/`pyright` clean on the touched file.
  ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate change. Prior merged — **DEC-DASHBOARD-NAV-LATENCY-001, MIGRATION.md #933** — the
  three remaining UNCACHED dashboard pollers (`/api/v1/coverage`, `/api/v1/universe/completeness`,
  `/api/v1/universe/mapping-integrity`) are now memoised behind a COMPOSITE (write-generation,
  instrument-master-revision) token, so a repeat poll is an O(1) handoff instead of a full O(datasets)+ledger
  projection — the LOW-RISK nav-latency wins. NEW COMMON public `master_revision(store)` helper in
  `universe_cache.py` (the ONE instrument-master content fingerprint `(count, max(fetched_at))`, which
  `UniverseSnapshotCache._revision` now delegates to) folded — alongside `_summary_cache.generation` — into the
  cache KEY of three dedicated generation-stamped `MoversSnapshotCache` instances, so a pull (generation bump) OR
  a keyless `load-instruments` (master-revision move, which does NOT bump the generation) yields a new key →
  recompute, never stale (Golden Rule #1). The JSON + CSV mapping-integrity routes share ONE cached report. PLUS
  `defer` on the render-blocking `html2canvas` CDN `<script>` in `v29.html` (only used in the PNG-export click
  handler, so safe) so it no longer blocks first paint. HONEST: a repeat poll is O(1) in lake/ledger depth, NOT a
  literal O(1) total (the first poll after a pull/load pays one real projection); READ-ONLY accelerator, byte-identical
  payloads; the DOMINANT remaining nav lag (full-page reload → in-place DOM swap) is a SEPARATE larger PR, NOT in this
  one. `universe_cache.py` 100% line+branch (65/12); NEW `test_dashboard_nav_latency_cache.py` (4 tests) +
  `master_revision` unit test; 491 dashboard integration tests green; ruff/ruff-format/`mypy --strict`/`pyright` clean.
  ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate change. Prior merged —
  **DEC-PULL-HEALTH-MONITOR-001, MIGRATION.md #932** —
  the live Groww pull now SELF-INSTRUMENTS its health: a NEW `brutex/observability/pull_health.py`
  `PullHealthMonitor` holds the hysteresis `SLADetector` (UNCHANGED), mirrors the rate-controller +
  call-meter snapshots already produced each `_observe_rate` tick into Prometheus gauges, and routes a
  breach/recovery to a best-effort Telegram `pull_degraded` alert + structlog — five rules
  (`pull.achieved_below_allowed` < 0.6 · `pull.pool_idle_high` > 0.5 · `pull.write_latency_spike` >
  1500 ms · `pull.rate_storm` recent_hits > 20 VENDOR-gated · `pull.stalled` < 0.5 r/s for K samples
  CRITICAL, via a NEW `DwellProvider` closure that layers the dwell on the detector's None-skip contract
  with NO change to `SLADetector`). `sweep_alerts.py` gains ONE `pull_degraded(*, rule, achieved, allowed,
  detail)` mirroring `rate_breach`, whose message DISTINGUISHES "OUR pipeline is the limiter — check write
  latency" from "VENDOR 429-gated — expected, the pull continues". Wired into the `_observe_rate` seam
  (optional `monitor` param, best-effort, no new thread). HONEST: thresholds are operator-tunable
  heuristics not vendor limits; a breach is an OBSERVATION not auto-remediation; the monitor's production
  binding at the live build site is a flagged FAST-FOLLOW (the SEAM + WATCH logic ship, the binding does
  not). `pull_health.py` + `sweep_alerts.py` BOTH 100% line+branch; ruff/`mypy --strict`/`pyright` clean;
  ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX). Prior on `claude/serene-hamilton-jlzt1n` —
  **DEC-GROWW-RATE-CEILING-8-002, MIGRATION.md #922** — the
  production per-second ceiling RAISED 5 → 8 `GROWW_RATE_LIMIT_RPS_CEILING` (operator-directed 2026-06-24,
  supersedes DEC-GROWW-RATE-CEILING-5-001): now that Lever 2's 4 connections generate ~6.4 r/s of demand toward the
  ceiling, 8 is a fair test of vendor headroom; 8×60 = 480/min stays under the 500/min governor so the HARD cap can't be
  overshot; a CEILING raise only ALLOWS a higher rate — the AIMD ladder still steps DOWN on 429s and recovers only as
  far as the vendor tolerates, so worst-case = 5; SUSTAINED 8 is UNVERIFIED (the log shows the vendor 429ing at 5),
  revert is one constant; one test updated (480 ≤ 500), 99 constants/rate/pool/live tests GREEN, ruff + mypy clean.
  Prior on this branch — DEC-FETCH-WRITE-OVERLAP-001 #921 (Lever 2, MERGED #883): the Groww pull was achieving only
  ~3.2 r/s against a controller
  ceiling held at 5.0 — a 9-agent deep-research workflow PROVED from the fresh log (`cf1afd97-brutex.log`, 3.33 h,
  37,381 fetches, re-derived twice) that this is OUR code, not Groww: time-weighted rate 4.96 r/s (held 5.0 for 96.3%,
  only 65 429s all at `current_rps=5.0`, burst `rps_1s_max=10`), yet achieved 3.20 → a 1.76 r/s SLACK a binding limiter
  cannot have. ROOT: only 2 SDK connections, each on a ~625 ms per-contract cycle (~185 ms fetch + ~440 ms serial
  non-fetch work dominated by the lake Parquet append) → 2/0.625 = 3.2 r/s demand < 5 allowed. **DEC-FETCH-WRITE-OVERLAP-001,
  MIGRATION.md #921 — Lever 2 (PROVEN, shipped):** `GROWW_CLIENT_POOL_SIZE` 2 → 4 (4/0.625 = 6.4 r/s demand > 5 → the
  SHARED rate controller binds at the 5.0 ceiling, consuming the slack with ZERO new concurrency surface; MAX stays 4 =
  the sanctioned one-TOTP-token envelope; Rule 6 per-second ceiling UNTOUCHED at 5.0); the stale "Little's Law → 2 is
  sufficient" comment corrected with the proven 625 ms-cycle arithmetic. **Lever 1 (DESIGNED + adversarially reviewed,
  benchmark-gated NEXT):** a COMMON sharded `write_consumer` producer/consumer primitive moving the ~440 ms append+record
  OFF the fetch worker (the only lever that unlocks 8) — merges ONLY if `bench_write_rate(W) > 3.2/s` is proven (a SINGLE
  writer would serialize appends at ~3/s = a NET REGRESSION); folds the #919 back-channel-block fix +
  one-consumer-per-interval lifecycle + ledger-settle-only-after-durable-write. The 5 → 8 flip is ONE constant
  (`GROWW_RATE_LIMIT_RPS_CEILING`), gated on a clean overlap-at-5 run then KEPT only if 8 nets HIGHER throughput (the #906
  trap); `8×60 = 480 < 500/min` governor PROVEN-safe. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX). `constants.py` ruff +
  `mypy --strict` clean; 21 constants tests GREEN (NEW default-is-4 + slack-arithmetic regression); 58 groww
  pool/live/reauth tests GREEN. Prior merged: DEC-SUMMARY-CACHE-ADAPTIVE-FLOOR-001, MIGRATION.md #920 — the dashboard's `SummaryCache` TTL floor is now
  ADAPTIVE — `max(base 2 s, the last scan's own duration)` capped at 120 s — so an expensive full-lake
  completeness/coverage scan self-throttles to a ≤50% duty cycle during a pull instead of re-firing back-to-back on
  every ~4 s poll and starving the concurrent pull in the SAME process — DEC-SUMMARY-CACHE-ADAPTIVE-FLOOR-001,
  MIGRATION.md #920, the operator-approved item (b) "the tiny completeness-cache fix so the dashboard stops freezing
  during pulls". PROVEN from the operator's logs: the shared `SPOT` scan grew to 28–32 s ≫ the fixed 2 s floor, so a
  poll re-triggered it continuously and the pull's fetch rate HALVED (~6 → ~3 RPS) as the scan latency climbed into the
  options phase. FIX (READ-ONLY dashboard cache; NO engine/lake/pull change): `_Entry` gains `compute_duration` (clamped
  ≥ 0), `get_or_refresh` times the scan, `_plan` throttles for `min(max(base, duration), 120 s)` — a cheap scan keeps
  the snappy 2 s floor (idle byte-unchanged), a 30 s scan auto-throttles to ~30 s. HONEST: the live HEADLINE is the O(1)
  `CoverageCounters` (unaffected); only the DETAILED aggregate goes its-own-duration stale during a pull; reduces the
  collision (~50% CPU-duty recovery) but does NOT eliminate disk-IO contention (process isolation was DROPPED by the
  operator, not claimed); self-tuning, no magic constant. `summary_cache.py` 100% line+branch (86/12, 0 missing); NEW
  adaptive-floor tests + 299 coverage/completeness/dashboard regression tests green; ruff/`mypy --strict`/`pyright`
  clean. Prior merged: DEC-SUSPICIOUS-EMPTY-FLAG-001, MIGRATION.md #919 — an OBSERVE-ONLY pull-side diagnostic surfaces
  a whole F&O underlying whose every cleanly-fetched contract came back 100% EMPTY — the BAJAJ-AUTO / M&M "silent-vanish"
  failure mode — as ONE `pull.underlying_all_empty` WARNING, so a whole stock can never silently blend into a normal
  no-data gap again — the operator's "a whole stock should never silently
  vanish". PULL-side (not read-side) because only the pull knows "tried-and-empty" vs "never-tried"; mirrors the #907
  observe-only triad — a pure `scan_underlying_all_empty` (groups pairs by `underlying_symbol`, counts CLEAN fetches
  via `_is_clean_fetch` = `windows≥1 ∧ skipped==0 ∧ still_failed==0`, flags `attempted≥3 ∧ stored_bars==0`) + immutable
  `UnderlyingEmptyReport`/`AllEmptyScan` + a guarded `_log_underlying_all_empty` (capped 20, `repr()`-named) + ONE
  best-effort `try/except` wiring line in `run_groww_option_ingest`. HONEST: pure DETECT+WARN — no return/ledger/engine
  change, no auto-recovery; false-positive-guarded (skip/transient/empty-clamp excluded so a re-pull never flags)
  [LOWLEG-RECONCILE — DEC-SILENT-VANISH-LOWLEG-001, MIGRATION.md #1167 (pack #1171): the original "a 1-2-leg illiquid name never
  flags" clause is SUPERSEDED — a 1-2-clean-fetch underlying whose EVERY pair was a clean empty, with ZERO non-clean
  AND ZERO terminally-quarantined siblings, NOW flags marked `low_leg=True`; every other #919 false-positive guard
  (skip/transient/empty-clamp/quarantined sibling → disqualified) is regression-pinned; observe-only WARN unchanged];
  ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX). `option_ingest.py` 100% line+branch (247/68,
  0 missing); NEW `TestSuspiciousEmptyFlag` (14 tests); 75 option-ingest tests green; ruff/`mypy --strict`/`pyright`
  clean. Prior merged: DEC-SCOREBOARD-REFRESH-002, MIGRATION.md #918 — the customer-facing Product Scoreboard
  (`docs/PRODUCT_SCOREBOARD.md` + the shareable self-contained `docs/product_scoreboard.html` one-pager) REFRESHED
  through ledger #917 under plain-English value themes (finished / current / up-next / future), every claim
  traced to a REAL merged ledger entry, the operator's "provide the
  finished/current/future plans as an extreme easy-understandable table-level comparison that attracts millions". The
  Finished themes gain the cycle's merged rows (#867/#916 speed · #915/#917 movers · #883/#912/#913 resilience); the
  Current section is re-scoped #912→#917; a NEW "Up next" section names the in-flight suspicious-empty flag + the live
  vendor probe; the `.html` (stalled at #911) is REGENERATED to mirror the `.md` exactly (CSS preserved verbatim,
  tag-balance verified). HONEST: every row traces to a real ledger entry (the data-gathering agent's "#1003 GPU"
  citation was DROPPED as a hallucination, Golden Rule #1); ENGINE scope unchanged (NIFTY/BANKNIFTY/SENSEX, stated in
  the scoreboard); in-flight/future items labelled as such, never shipped; DOCS-ONLY — no `brutex/` code touched.
  Prior merged: DEC-MOVERS-FUTURES-TIER-001, MIGRATION.md #917 — the `/movers` Futures tab's Near / Next / Far
  expiry-tier selector now ACTUALLY filters: each tier selects that serial-month future per underlying (near = front
  month, next = 2nd serial, far = 3rd) and is keyed INDEPENDENTLY in the candidate cache, so a Next/Far click can never
  be served stale Near rows; the operator's "execute only this — Futures Near/Next/Far wiring", the P1 flagged
  follow-up of #915. ROOT
  (file:line): the page already SHOWED the dropdown (`movers.html:243`) + SENT `?expiry=` (`:314`), but the route's
  futures branch IGNORED it — `compute_futures_candidates` always picked the front month via `select_near_month` and the
  futures `candidate_key` carried NO `expiry_tier`, so all three tiers collapsed to one cached near result (a Next/Far
  click silently returned Near rows). FIX (READ-ONLY dashboard; NO engine/scope/vocab/rate change, ENGINE stays
  NIFTY/BANKNIFTY/SENSEX): NEW `select_expiry_tier` (live contracts (expiry ≥ on_date) sorted earliest-first → the
  tier's 0-based rank `{near:0,next:1,far:2}`, or honest `None` when the underlying lists fewer serial months —
  `select_near_month` is now the `near` special case, byte-equivalent) + NEW COMMON `normalize_futures_tier` (the
  futures twin of `normalize_options_selectors`; blank/unknown ⇒ near); the route threads the normalised tier into BOTH
  `candidate_key(expiry_tier=)` AND `compute_futures_candidates(expiry_tier=)`, the warmer keys at near. FRONTEND
  UNCHANGED (it already showed + sent the selector). HONEST: the stale-cache bug class is closed (the tier is part of
  the futures cache key, proven by a Near-then-Next route test asserting a DIFFERENT contract); a tier with no live
  contract at its rank is honestly SKIPPED (counted, never faked); default ⇒ near is byte-equivalent to the prior
  near-month-only behaviour. `movers_futures.py` 100% line+branch (235/50, 0 missing); NEW `TestSelectExpiryTier` +
  `TestNormalizeFuturesTier` unit tests + `TestFuturesExpiryTier` route integration (incl. the cache-independence
  regression); 381 movers tests green; ruff/ruff-format/`mypy --strict`/`pyright` clean. ENGINE UNCHANGED, no
  scope/vocab/rate change. Prior merged: DEC-GROWW-GOVERNOR-ATOMIC-001, MIGRATION.md #913 (the per-minute rate governor
  now RESERVES its issue slot ATOMICALLY under the SAME lock that observes a free slot, closing a latent
  concurrent-overshoot of the HARD 500/min cap — DORMANT at the production 5 r/s ceiling where `5×60 = 300/min <
  500/min`; adapter-only, overshoot now IMPOSSIBLE, proven by a 16-thread `Barrier` regression; ENGINE UNCHANGED). Prior
  merged: DEC-CANDLE-PARTIAL-ZERO-SKIP-001, MIGRATION.md #912 (a vendor
  candle carrying a ZERO price among real prices — e.g. `open: 0` with a real high/low/close — is now gracefully
  SKIPPED instead of silently STORED as a corrupted bar (a fabricated zero price + a zero low/high clamp), the
  symmetric twin of the partial-ABSENT skip: `assemble_candle`'s all-empty guard uses `has_no_price` (absent OR zero)
  but its partial guard used `is_absent`, so a mix of zero + real prices leaked through and was stored; the partial
  guard now uses the SAME `has_no_price` predicate, never storing junk, never fabricating a bar). Prior merged:
  DEC-COMPLETENESS-SPOT-KEY-RECOVER-001, MIGRATION.md #911 (the read-side FIX of
  #907's M&M diagnostic — a lake spot partition whose on-disk directory key DRIFTED from the scored `symbol_name` (a
  legacy `"NSE-M&M"` / `"M&M "`) is RECOVERED to its canonical scored symbol on BOTH the `/completeness` scoreboard AND
  the `/universe` strip via ONE COMMON `_resolve_spot_category`, surfacing real on-disk data instead of silently
  dropping it; a clean+stale-fragment collision is rank-merged so the best real data wins order-independent; recovers
  only data that exists, never fabricates; the #907 diagnostic still flags the raw drift for cleanup). Prior merged:
  DEC-GROWW-REAUTH-DEADLINE-001, MIGRATION.md #910 (the token
  re-mint is now WRAPPED in the freeze-guard wall-clock deadline, so even a HUNG re-mint at 06:00 IST raises a
  retryable timeout — the window quarantines + re-pulls — instead of freezing the whole pool; `run_with_deadline`
  MOVED to a cycle-proof stdlib-only `deadline.py` + re-exported from `client.py`; `SdkRefresher.reauth` wraps the
  under-lock mint, publish only after a clean return, timeout → retryable out of the released lock; adversarially
  proven no abandoned-daemon data race / no half-state / bounded 8-thread hang; CLOSES the #909 MINOR-1 fast-follow).
  Prior merged: DEC-GROWW-TOKEN-REAUTH-001, MIGRATION.md #909 (the Groww adapter auto-RE-MINTS the daily access token on an
  auth-expiry mid-pull — thread-safe, single-flight via a generation counter, bounded `max_reauths` — so the 06:00 IST
  token reset self-heals instead of killing the overnight pull; PROVEN from the operator's logs: token died at exactly
  06:00 IST with 3,764 quarantines while the TOTP credential was correct; NEW `reauth.py` `SdkRefresher` + `client.py`
  reauth branch + the visible `groww.token.reauth` self-heal log; CLOSES the §6 "unattended TOTP token lifetime"
  Phase-0 gate). Prior merged: DEC-SCOREBOARD-REFRESH-001, MIGRATION.md #908 (the customer-facing Product Scoreboard
  `docs/PRODUCT_SCOREBOARD.md` + `docs/product_scoreboard.html` REFRESHED through ledger #907 under six plain-English
  value themes, citation column switched to the authoritative `MIGRATION.md #NNN` entry; DOCS ONLY). Prior merged: DEC-COMPLETENESS-RECONCILE-DIAG-001,
  MIGRATION.md #907 (an OBSERVE-ONLY `/completeness` diagnostic pinpointing why a stock — the operator's "M&M" — reads
  empty: a lake-key ↔ scored-key drift surfaced as ONE `completeness.symbol_reconcile_mismatch` WARNING naming both
  sides via `repr()`; `SPOT_PROBE_SEGMENTS`-filtered so a pure F&O backfill stays SILENT (the hostile-review MAJOR,
  folded) + internally guarded so a malformed row never breaks the page; scoreboard payload BYTE-UNCHANGED, DIAGNOSES
  not yet FIXES — the live `repr()` enables an evidence-grounded key-normalization fix next; `completeness.py` 100%
  line+branch (257/86), `app.py` stays 100%, 85 tests green). Prior merged: DEC-GROWW-RATE-CEILING-5-001, MIGRATION.md #906 (the production Groww
  per-second rate ceiling LOWERED 8 → 5 r/s — a live-log audit showed the controller pinned at the ~3 r/s soft floor
  absorbing ~870 vendor 429-retries while the 5–8 headroom was never usable throughput; single source-of-truth
  constant `GROWW_RATE_LIMIT_RPS_CEILING` `8.0`→`5.0`, both injection sites read it, AIMD ladder/governor/floors
  PROVABLY unchanged, `5×60 = 300 ≤ 500` property-tested; `constants.py` 100% (49/4) + `rate_common.py` 100%
  (140/36); SUPERSEDES DEC-GROWW-RATE-CEILING-8-001). Prior merged: DEC-FNO-PULL-ORDER-LOOKBACK-001, MIGRATION.md #905 (the historical F&O pull
  now pulls every FUTURES (FUT) contract BEFORE the CE/PE options AND defaults ALL F&O to a UNIFORM 30-day lookback
  — futures dropped from 3 months to 30 days; pull-PLANNING + ORDER only, NEW `_MARKET_PULL_ORDER` map +
  `sorted(by_market.items())`, `default_fno_minute_window` collapsed to one `DEFAULT_FNO_LOOKBACK_DAYS`=30, dead
  `subtract_calendar_months` removed; `historical_pull.py` 100% (211/46) + `contract_bounds.py` 100% (79/20)).
  Prior merged: DEC-ENGINE-TUPLE-SKIP-RESILIENT-001,
  MIGRATION.md #904 (when one grid TUPLE errors mid-sweep the engine SKIPS it, finishes the rest, exits NON-ZERO
  with a `tuples_failed` count + `TUPLE_FAILED` audit naming the combo — never silent, deterministic redo;
  per-combo isolation rejected as an illusion; BLOCKER-fixed + 3-agent-verified, 100% line+branch). Prior merged:
  DEC-MOVERS-OPTIONS-VALUE-DEDUP-001, MIGRATION.md #903 (the `/movers` Options screen reads each contract's
  1-minute frame ONCE — it reuses the HLC3 turnover the SHARED `_read_future_session` already returns instead of
  a second `_session_value_paisa` read; byte-identical value, half the per-contract I/O; `movers_options.py` 100%
  line+branch). Prior merged: DEC-PRODUCT-SCOREBOARD-001, MIGRATION.md #902 (a plain-English customer-facing "Product Scoreboard" —
  `docs/PRODUCT_SCOREBOARD.md` + a self-contained shareable `docs/product_scoreboard.html` one-pager — of
  finished / current / future work, themed what-it-does → why-you-care → PR, every claim traced to the ledger;
  docs only). Prior merged: DEC-WINDOW-LEDGER-WRITE-HALT-001, MIGRATION.md #901 (a disk-full /
  OOM during the durable window-ledger save now HALTS the pull gracefully via the lake-guarded `atomic_write_via`
  — ENOSPC→`LakeWriteHalt("disk_full")` / MemoryError→out_of_memory, every other error raw — reaching the
  CLI/dashboard halt handlers + their CRITICAL Telegram alert; `window_ledger_store.py` + `atomic_io.py` both
  100% line+branch, hostile-reviewer SHIP). Prior merged: DEC-MOVERS-FUTURES-VALUE-001,
  MIGRATION.md #900 (the `/movers` Futures tab gained a "Top Value" (turnover) ranking + a "Value (Cr)" column via
  the SHARED HLC3 `session_traded_value_paisa`, completing the value metric across spot+options+futures;
  `movers_futures.py`+`movers_options.py`+`movers_snapshot.py` all 100% line+branch, 307 movers tests green).
  Prior merged: DEC-MOVERS-VWAP-VALUE-001, MIGRATION.md #899 (the `/movers` "Top Value" (turnover) metric now uses the
  per-bar HLC3 typical price `(high+low+close)/3 × volume` instead of `close × volume`, so the ranking tracks a
  broker's intraday turnover far more closely — the operator's "for top
  value alone it is not precise to broker right? if yes then solve this". ROOT: the old `Σ round_money(close) × volume`
  priced a whole minute's volume at its CLOSING tick, systematically diverging from a broker's tick-summed turnover
  whenever the close sat away from the minute's VWAP. FIX (READ-ONLY ranking): NEW ONE COMMON helper
  `movers_snapshot.session_traded_value_paisa(rows)` (`Σ round_money((high+low+close)/3) × volume` → paisa) shared by
  BOTH the spot snapshot (`compute_snapshot`) AND the options screen (`_session_value_paisa`, which now also selects
  high/low) — killing the two duplicated close×volume loops; each bar's typical price is round_money-snapped to 2dp
  BEFORE × volume (DEC-MONEY-2DP-001); ranking/display unchanged. HONEST: a precision IMPROVEMENT (closest-to-broker
  from 1-minute OHLCV), NOT a bit-exact match (a broker sums actual trade price×qty per TICK — without ticks the HLC3
  ×volume sum is the best VWAP proxy, residual divergence remains); degenerates to the old answer for a zero-range bar;
  Spot+Options only (futures has no value tab). `movers_snapshot.py` + `movers_options.py` BOTH 100% line+branch
  (210/46 + 216/46); NEW golden proves HLC3 (318660 ≠ old 320000); 304 movers tests green; ruff/`mypy --strict`/
  `pyright` clean. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged:
  DEC-GROWW-AMPERSAND-FALLBACK-001, MIGRATION.md #898 (F&O expiries/contracts for `&`-containing underlyings (M&M,
  M&MFIN, L&TFH, GVT&D, J&KBANK) now TRY a second `%26`-encoded symbol form when the plain form returns empty, so the
  whole-underlying chain failure is recovered. ROOT CAUSE (deep investigation + #870): the NSE `trading_symbol` `M&M`
  is handed verbatim to the `growwapi` SDK's `get_expiries`/`get_contracts`; the `&` is the HTTP query-string
  separator, sent UN-ENCODED, so the vendor returns a clean-but-EMPTY 200 — and because the break is at the
  chain-discovery ROOT (0 expiries → 0 `get_contracts` → 0 contracts) the ENTIRE underlying is missed. FIX
  (operator-chosen "try-both-forms"): NEW pure `underlying_symbol_forms` (plain FIRST, then `replace("&","%26")`) +
  generic `_fetch_first_nonempty_form`; the exact encoding is UNVERIFIED (needs a live Groww probe) — a HYPOTHESIS,
  NOT probe-confirmed; try-both is no-worse-than-before. The OPTIONS-path twin of the SPOT-only #845. `contracts.py`
  100% line+branch (92/34). Prior merged: DEC-DB-SPOT-CASCADE-001, MIGRATION.md #897 (the `/db` SPOT candle picker is now a symbol→interval CASCADE
  (the spot twin of the F&O cascade), replacing the flat ~750-row dropdown so the operator can pick an underlying
  symbol + a stored timeframe — the operator's "even for candles i can't
  select the underlying symbol or timeframe … we need the same as fno candle". NEW pure `spot_cascade.py`
  (`build_spot_cascade` → `SpotCascadeIndex.symbols()` O(1); `SpotSymbol` leaf carrying its STORED intervals inline;
  `symbol_to_json`) groups the non-F&O datasets by `(exchange, segment, symbol)`, sorted indices-first then alpha
  (the `INDEX` segment floats NIFTY/BANKNIFTY/SENSEX to the top, DATA-DRIVEN); ONE route `GET /api/v1/db/spot/symbols`
  via `_spot_cascade_or_none()` memoised behind `_summary_cache.generation` in a dedicated `_spot_cascade_cache`
  (REUSES the COMMON `MoversSnapshotCache` — no copy of `FnoCascadeCache`); the `db.html` frontend gains
  `cstate.spot` + `loadSpotCascade`/`onSpotSymbolChange`/`spotCascadeHtml` + a `csym` select, a symbol pick
  MATERIALISES the per-interval descriptors so the SHARED `query_candles` path is byte-unchanged. STORED-ONLY
  intervals (1m + derived 1d) — `query_candles` reads a physical partition + returns empty for an absent one, so
  offering all 21 TFs would be a phantom (mirrors F&O). HONEST: O(1)-cached per generation (cold build is one
  O(datasets) walk); picker-only (query path unchanged); only the expired-OPTIONS lake keeps the flat dropdown;
  `spot_cascade.py` imports nothing from `dashboard.*`/`data.*` (no import cycle). `spot_cascade.py` 100% line+branch
  (32/2, 6 unit); `app.py` stays 100%; NEW 4 integration tests; ruff/`mypy --strict`/`pyright` clean. ENGINE
  UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged: DEC-DB-TAB-SWITCH-O1-001,
  MIGRATION.md #896 (the `/db` candle tab switch (spot ↔ F&O ↔ option) is now O(1):
  the `/api/v1/db/candles/datasets` discovery endpoint — which walked the WHOLE `bars/` tree THREE times on EVERY tab
  switch — is memoised behind the shared write generation, so a switch within one generation is a cached handoff
  instead of a triple full-lake dir-walk — DEC-DB-TAB-SWITCH-O1-001, MIGRATION.md #896, the operator's "to navigate
  from F&O candles to candles also taking so much time". ROOT CAUSE (Explore-agent diagnosis): `_datasets()` ran THREE
  O(files) `bars/` dir-walks with NO cache (`list_candle_datasets` spot + `list_fno_candle_datasets` + `list_option_datasets`)
  on every tab switch; the merged O(page) work (#817/#818/#821) fixed within-tab PAGING but never the tab-SWITCH
  discovery. FIX (READ-ONLY viewer): wrap `_datasets()` in `_db_datasets_cache.get_or_compute("db_candle_datasets",
  generation=_summary_cache.generation, compute=_datasets)` — REUSING the COMMON generation-stamped `MoversSnapshotCache`
  (the SAME generic memo `_candle_count_cache`/`_window_counts_cache` use; NO new class, NO copy of `FnoCascadeCache`).
  Repeat switch within one generation = O(1) handoff; a pull bumps the generation → rediscover; build still runs on the
  threadpool worker on a miss. HONEST: O(1) on repeat, NOT a literal O(1) total (first switch after a pull pays one
  real triple walk); same `_datasets()` output, only discovery ORDERING changed (spy test: `list_fno_candle_datasets`
  runs ONCE across three loads, payloads byte-identical); the frontend full-DOM-rebuild + the spot symbol→interval
  CASCADE picker (#4b, the F&O-cascade twin) are flagged FAST-FOLLOWS. `app.py` stays 100% line+branch; NEW
  integration test; ruff/`mypy --strict`/`pyright` clean. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no
  scope/vocab/rate-cap change. Prior merged: DEC-COMPLETENESS-O1-LATENCY-001,
  MIGRATION.md #895 (the `/completeness` endpoints — scoreboard + CSV +
  `/universe/completeness` — are now O(1) on a repeat load: the O(datasets) full-lake spot scan is routed through the
  SAME generation-stamped `_summary_cache` SPOT entry `/coverage/spot` already uses, instead of the prior UNCACHED
  direct call that re-ran the whole Parquet-footer aggregate on every load —
  the operator's 7–9 s page + the flagged FAST-FOLLOW of #893. ROOT CAUSE: the three completeness
  endpoints share `app._completeness_inputs`, which called `summarize_spot_datasets(store.connection, lake)` (a
  per-dataset `COUNT(*)`/`MIN`/`MAX` `read_parquet` footer aggregate, O(datasets)) DIRECTLY with NO cache — yet the
  sibling `/coverage/spot` wraps the BYTE-IDENTICAL call in `_summary_cache.get_or_refresh(SPOT, …)`. FIX (READ-ONLY
  dashboard): a one-expression DRY change — the scan now goes through `_summary_cache.get_or_refresh(SPOT, …)`, sharing
  the SAME SPOT cache entry (open either page → the other is warm); the ledger rollup `spot_window_rollup` (its OWN
  mtime cache) stays PER-REQUEST + OUTSIDE the SummaryCache (the C8 freshness contract — a ledger flush still refreshes
  the chips); the cheap `list_index_equity_instruments`/`list_coverage` SELECTs stay per-request (never stale). HONEST:
  repeat load is O(1) on the SPOT scan, NOT a literal O(1) total — the first read after a pull still pays one real
  O(datasets) scan + the cheap SELECTs run every request; correctness unchanged (same scan → same projection, only the
  cache ORDERING). `app.py` stays 100% line+branch (no new app branch — the hit/miss/throttle decision lives in the
  100%-covered `SummaryCache`); NEW integration test proves the scan runs ONCE across two completeness loads + one
  `/coverage/spot` load with identical payloads; coverage route suites green; ruff/`mypy --strict`/`pyright` clean.
  FOLDED hostile-review MAJOR: sharing the SPOT entry across two concurrently-polled routes widened a LATENT cold-start
  race in `SummaryCache._plan` (the `kind in self._refreshing` branch returned `None` → `self._entries[kind]` KeyError
  → 500 on a never-yet-populated kind); FIX gates the short-circuit on `entry is not None` so a cold reader recomputes
  (a transient idempotent double-scan, never a 500) — `summary_cache.py` stays 100% line+branch with a new
  cold-concurrent regression test, the warm fast path byte-unchanged.
  ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged: DEC-FNO-LOOKBACK-SPLIT-001,
  MIGRATION.md #894 (the F&O default historical-pull lookback is SPLIT by
  contract type: FUTURES default to 3 calendar months, OPTIONS to a tighter 30 days —
  the operator's "only for futures by default try to fetch it for 3 months but for options only
  one month, that is max 30 days". When no explicit from/to dates are given, `default_fno_minute_window` defaulted
  EVERY F&O contract to the last `DEFAULT_FNO_LOOKBACK_MONTHS = 3` months of its trading life; an option's liquid life
  is short + the chain is huge (every strike × CE/PE), so 3 months over-pulls illiquid early-life option bars. FIX
  (pull-PLANNING window only, F&O lake stays REFERENCE_ONLY): NEW `DEFAULT_OPTION_LOOKBACK_DAYS = 30` (a fixed 30-DAY
  cap per "max 30 days", NOT a calendar month) + `default_fno_minute_window` gains `is_option: bool = False` branching
  the lookback START — `expiry − 30 days` for an option vs `subtract_calendar_months(expiry, 3)` for a future — with
  the SAME `max(full_start, …)` / `min(full_end, expiry + 1 day)` clamp + the SAME empty-window + `None`-expiry guards;
  `historical_pull._resolve_minute_windows` passes `is_option=contract.contract_type in {"CE","PE"}` (types are exactly
  FUT/CE/PE; explicit operator dates still override via the `explicit_dates` branch). HONEST: changes ONLY the DEFAULT
  planning window when no dates are given (an explicit from/to range is byte-for-byte unchanged); 30 is a fixed
  day-count not one calendar month; both bounds stay clamped into `[full_start, full_end)`; pull-side only — no lake
  schema / stored-bar / rate / vocab / engine change. `contract_bounds.py` at 100% line+branch (87/20, 32 tests) +
  `historical_pull.py` at 100% line+branch (210/46, 40 tests); ruff/`mypy --strict`/`pyright` clean. ENGINE UNCHANGED
  (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged: DEC-COVERAGE-WINDOWS-O1-001, MIGRATION.md #893
  (the `/coverage/windows` "Fetch windows" page is now O(page):
  DuckDB `LIMIT`/`OFFSET` + a generation-cached `GROUP BY` replace the unconditional O(340,330) Python
  count+filter+sort that ran on EVERY tab/filter click + 4 s poll —
  the operator's "navigating section to section is very slow / O(1) latency everywhere" fix. A 3-agent investigation +
  the attached `brutex.log` proved the server `latency_ms` degraded tens-of-ms → 1.5–4 s as the ledger grew, because
  `build_window_payload` looped over every record for counts, then filtered every record, then sorted, then sliced —
  no SQL pushdown (the mtime cache saved the READ, not the PROCESSING). FIX (READ-ONLY viewer, modelled on the proven
  `/db`+`/movers`+`/universe` patterns): NEW `_build_window_payload_duckdb` reads the ledger Parquet via DuckDB — the
  ROW page is a `WHERE` (market-bucket + status, PLANNED excluded unless `?status=PLANNED`) + severity-first `ORDER BY`
  (CASE from `_WINDOW_STATUS_ORDER`) + `LIMIT`/`OFFSET` (O(page)); `counts` come from ONE `GROUP BY market, status`
  (`_window_counts_total`) memoised behind the COMMON generation-stamped `MoversSnapshotCache` (`_window_counts_cache`,
  keyed by `_summary_cache.generation`), and `matched` is DERIVED from the cached counts (no extra scan).
  `build_window_payload` gains optional `conn`/`offset`/`count_cache`/`generation`; the route injects them + a NEW
  `offset` param. HONEST: a cached-counts HIT is O(1) in partition depth + the page is O(page) (DuckDB still reads
  footers, NOT a literal O(1) total); a `None` conn / no Parquet keeps the LEGACY pyarrow path byte-for-byte
  (back-compat + the `window_status.json` fallback); the DuckDB path returns the SAME payload (fast-path-equals-legacy
  test); fixes the SERVER latency the log measured — a frontend in-place-`<tbody>`/debounce pager + the even-slower
  `/completeness` 7–9 s endpoints are flagged FAST-FOLLOWS, NOT claimed. `db_view.py` at 100% line+branch (NEW
  `TestBuildWindowPayloadDuckDB`); 322 covering-file + 31 route tests green; ruff/`mypy --strict`/`pyright` clean.
  ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged: DEC-NTM-CACHE-FIRST-001,
  MIGRATION.md #892 (the `/universe` NTM-constituent load is now CACHE-FIRST +
  fail-fast, so every load after the first is INSTANT instead of re-blocking ~30–160 s on the slow niftyindices.com
  host, the operator's "why is this taking so much time / why AGAIN"
  fix. Root cause: `instrument_loader._resolve_constituent_csv` was LIVE-FIRST — it ALWAYS attempted the slow live
  niftyindices fetch (`_default_opener`, 30 s × 3 retries ≈ 90 s) and used the on-disk last-good cache ONLY on a live
  FAILURE, so EVERY `/universe` load re-paid the full slow fetch even with a good cache present, all under
  `_LOAD_LOCK` (the operator's `latency_ms=162854.5` ≈ 163 s "stuck" page). FIX (READ-ONLY load): rewrite
  `_resolve_constituent_csv` CACHE-FIRST returning `(csv_text, source ∈ {cache_fresh, live, cache_stale, none})` — a
  cache YOUNGER than a TTL (`_ntm_cache_ttl_seconds`, env `BRUTEX_NTM_CACHE_TTL_SECONDS`, default 7 days; NTM
  rebalances only semi-annually CLAUDE.md §7) is served INSTANTLY with ZERO network call (the O(1) hot path); only a
  cold/stale cache hits ONE bounded live fetch (env-tunable `BRUTEX_NSE_FETCH_TIMEOUT_SECONDS` /
  `BRUTEX_NSE_FETCH_MAX_RETRIES`), now paid AT MOST ONCE per TTL not every load — the headline win is the cache-first
  ORDERING, NOT a timeout cut: the per-attempt default stays the operator-validated 30 s (a 10 s attempt previously
  read-timed-out on this host; 30 s was the deliberate fix) with retries trimmed 3 → 2 (30 s × 2 ≈ 61.5 s worst, down
  from 90 s), patient enough to succeed-once + seed the cache → instant forever; a live fail/empty with a stale cache falls back to it; nothing
  available → honest `none` degrade to the F&O base set. NEW `ntm_cache.load_cached_constituent_with_age` (+
  `CachedConstituents`) reads body+mtime once (age clamped ≥ 0 for clock skew); the `_ntm_admission` note carries the
  honest `source`/`from_cache`. HONEST: O(1)-latency on the STEADY-STATE load (fresh-cache hit = one stat+read, no
  network), NOT a literal O(1) first-ever boot (the cold/post-TTL refresh pays the bounded fetch ONCE); correctness
  unchanged (same CSV parsed/matched, only the ORDER + the live bound changed); a non-positive TTL explicitly
  DISABLES the fast path (always refresh) yet still serves a stale cache, so worst case is "as patient as before",
  never worse; no background threads / no new global state — fully synchronous + deterministic; the operator's manual
  "drop the CSV at ~/.brutex/cache/ntm_constituents.csv" now makes the very next load instant. `ntm_cache` +
  `nse_constituents` at 100% line+branch; `instrument_loader` 100% across its covering files; NEW tests — fresh-cache
  served with a `_must_not_fetch_opener` that ASSERTS the network is never called, stale-cache refreshed/empty/raise
  fallbacks (TTL=0 forces stale), no-cache+empty→`none`, the age helper edge-cases, the 3 env-tunable bound helpers;
  262 universe/constituents/loader tests green; ruff/`mypy --strict brutex`/`pyright brutex tests` clean. ENGINE
  UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged: DEC-ATOMIC-WRITE-FINISH-001,
  MIGRATION.md #891 (the LAST hand-rolled atomic-write copies — config runtime + credentials + NTM cache +
  window-ledger — share the ONE `atomic_write_simple` core (gaining an optional `chmod` for the credential 0600
  save), each preserving its OWN error contract (no LakeWriteHalt mis-escalation); +`fsync`-before-rename +
  temp-cleanup-on-any-error wins; the atomic-write COMMON consolidation is COMPLETE; deferred: the window_ledger
  ENOSPC→LakeWriteHalt graceful-halt guard-EXTEND). Prior merged: DEC-ATOMIC-WRITE-CONSOLIDATE-001,
  MIGRATION.md #890 (the engine sidecar writers share the ONE atomic-write
  core via a NEW guard-free `atomic_write_simple`, killing the hand-rolled `mkstemp`/`fsync`/`os.replace`/cleanup
  copies — the #885 COMMON follow-up: #885 centralised the
  LAKE writers' atomic write but left the others as noted debt. `results_store._atomic_write` +
  `sweep_checkpoint._atomic_write` were byte-near-identical copies (and `trial_stats`/`tuple_complete`/`sweep_heal`
  delegate to results_store, so 5 engine writers rode 2 copies). The blocker to reusing `atomic_write_via`: it bakes
  in `lake_write_guard` (ENOSPC/OOM → `LakeWriteHalt`), SEMANTICALLY WRONG for the engine (which wraps raw `OSError`
  into its OWN `ResultsStoreError`/`SweepCheckpointError` + is NOT wired for the lake global-stop). FIX: `atomic_io.py`
  refactored to a shared `_atomic_write_core` exposed as `atomic_write_via` (lake-guarded, the 2 Parquet writers,
  byte-unchanged) + a NEW `atomic_write_simple` (guard-FREE — every error incl. raw ENOSPC propagates as-is); the 2
  engine writers delegate to `atomic_write_simple`. HONEST: ZERO engine error-semantics change (same raw OSError →
  same domain-error contracts/tests preserved; disk-full NOT mis-escalated to LakeWriteHalt); a tiny durability win
  (core `finally` cleans the temp on ANY error vs the old `except OSError`); lake `atomic_write_via` byte-equivalent
  (#885 invariants re-verified); consolidates the ENGINE copies ONLY — the non-lake `runtime`/`credentials`(+chmod)/
  `ntm_cache`(returns False) + the `window_ledger` guard-extend remain the EXPLICIT next follow-up, NOT claimed.
  `atomic_io.py` at 100% line+branch (new atomic_write_simple tests); results_store/sweep_checkpoint stay 100%
  (cleanup tests repointed to `atomic_io.os.replace` + `.brutex-write-*` prefix); 268 engine+atomic_io+lake-writer
  tests green; ruff/`mypy --strict brutex` (254 files)/pyright clean. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no
  scope/vocab/rate-cap change. Prior merged: DEC-TELEGRAM-PULL-HALT-DASHBOARD-001, MIGRATION.md #889 (the DASHBOARD
  pull path ALSO fires the CRITICAL
  pull-halt Telegram alert — the dashboard twin of #888 — so a halt reaches the operator regardless of which
  entrypoint pulls: #888 wired ONLY the CLI `cmd_fetch`
  path; the dashboard `/pull-pipeline` (primary pull UI) had no halt alert. A `LakeWriteHalt`/`QuotaInvariantError`
  propagates uncaught out of `pull_all_spot` to the ONE chokepoint `BackgroundPullJob._run`'s `except Exception`
  (`brutex/dashboard/pull_jobs.py`) where every backgrounded dashboard pull lands. (1) the CLI's `_emit_pull_halt_alert`
  body is MOVED to a COMMON `emit_pull_halt_alert(exc)` in `sweep_alerts.py` (ONE source of truth — `cli._emit_pull_halt_alert`
  is now a thin delegating wrapper, CLI byte-unchanged); (2) `_run`'s except now calls it GUARDED by a NEW `_is_pull_halt(exc)`
  (isinstance of the spine's global-stop set), so it fires ONLY for a pull-wide halt — a generic per-symbol worker error
  NEVER alerts — and the reported error state is byte-unchanged. HONEST: best-effort/silent-no-op-when-disabled;
  `pull_jobs.py` stays WHEEL-FREE at import (heavy `quota_tracker`/`data.errors` classes lazy-imported inside `_is_pull_halt`);
  the dashboard QUOTA stop is a VERDICT not a raise so it does NOT fire this hook (operator already sees it on the UI) —
  a verdict-driven phone alert is a flagged FOLLOW-UP (route bodies are pragma-no-cover), NOT claimed. `sweep_alerts.py`
  + `pull_jobs.py` both at 100% line+branch (new emit_pull_halt_alert + halt-fires-once / generic-no-alert tests); 147
  notifications+dashboard+CLI tests green; ruff/mypy --strict/pyright clean. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX),
  no scope/vocab/rate-cap change. Prior merged: DEC-TELEGRAM-PULL-HALT-001, MIGRATION.md #888 (a pull-wide HALT —
  disk-full / OOM / quota — fires
  a CRITICAL Telegram alert so the operator is told in REAL TIME, not just in a log; the observability capstone of
  the resilience batch: the just-shipped batch makes a pull HALT
  gracefully on a full disk / OOM (`LakeWriteHalt` #885) or a quota stop, but the operator only learned of it from a
  log line (§11 AUTOMATED wants it ALERTED, "ZERO clicks per day"). NEW `SweepAlerter.pull_halted(*, reason)` alert
  (CRITICAL `PHASE_BLOCKED`, reason-scoped dedupe), wired into the CLI `cmd_fetch` pull path: `_one_fetch_pass` wraps
  the `run_groww_spot_ingest_with_retry` call in `try/except (LakeWriteHalt, DailyQuotaExceeded, QuotaInvariantError)`
  (the SAME global-stop set the spine carves out) that fires a NEW `_emit_pull_halt_alert(exc)` then RE-RAISES — so
  the existing halt handling + non-zero exit is byte-unchanged. HONEST: fire-and-forget, best-effort — the alerter
  swallows every error + is a silent no-op when Telegram is DISABLED (the default), so it can NEVER block/mask/crash
  the pull; the notifier is async (no network I/O on the pull thread); body is MarkdownV2-escaped; this wires the CLI
  pull path ONLY — the DASHBOARD pull path's Telegram wiring is an explicit FOLLOW-UP, NOT claimed done.
  `sweep_alerts.py` at 100% line+branch (new `pull_halted` unit + a real-notifier wire test); the CLI wiring covered
  by 3 new `test_cli_retry_storm.py` tests (LakeWriteHalt-raising fake ingest proves alert-once-then-re-raise +
  `_emit_pull_halt_alert` both branches); 137 notifications + CLI tests green; ruff/mypy --strict/pyright clean.
  ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged: DEC-LAKE-PARTITION-FLOCK-001,
  MIGRATION.md #887 (a cross-process advisory `fcntl.flock` now serialises
  concurrent writers to the SAME lake month partition, closing a SILENT data-LOSS race,
  gap #5 of the operator's critical-risks resilience-hardening
  batch: the two lake Parquet writers do a READ-modify-WRITE on a month partition guarded ONLY by a per-partition
  `threading.Lock` (intra-process), so two pulls sharing `BRUTEX_LAKE_ROOT` — a CLI `brutex fetch` + a dashboard
  pull — writing the same partition lost-update SILENTLY: P1 reads `[T1,T2,T3]`, P2 reads `[T1,T2,T3]`, P1 writes
  `[…,T4]`, P2's `os.replace` clobbers it → T4 gone, no error (keep-first only dedups WITHIN one process; across
  processes the last `os.replace` wins). NEW `brutex/data/partition_lock.py` (`partition_write_lock(lake_root,
  dest)` + `partition_lock_path`) takes a blocking exclusive `flock` held ACROSS the whole read-modify-write so
  the later process reads AFTER the earlier `os.replace`; BOTH writers nest it inside their existing per-partition
  `threading.Lock` (same lock ORDER everywhere → no cross-partition deadlock). HONEST envelope: the lock is on a
  STABLE sidecar `<lake_root>/.locks/<sha256(dest)>.lock` (NEVER the partition file — `os.replace` swaps its
  inode; `.locks` is outside `bars/`+`options/` and not a `*.parquet`, so no scanner sees it); PER PARTITION
  (different partitions never contend — fan-out parallelism kept); NO stale-lock recovery needed (the kernel
  releases an flock on fd-close/process death); O(1) per partition write (one open+flock+close, NOT per bar); a
  disk-full at lock creation maps to `LakeWriteHalt` (reuses #885's `lake_write_guard`); POSIX (Linux+macOS),
  Windows OUT OF SCOPE; it serialises CONCURRENT writers, NOT a cross-partition transaction. `partition_lock.py`
  at 100% line+branch; both writers stay 100% line+branch with NEW deterministic regression tests probing a
  SEPARATE fd DURING the merge READ to assert the lock is HELD (FAIL without the flock); chaos/sink/ingest/
  lake-scan suites green; ruff/mypy --strict/pyright clean. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no
  scope/vocab/rate-cap change. Prior merged: DEC-MOVERS-CORRUPT-SKIP-001, MIGRATION.md #886 (a single corrupt /
  unreadable Parquet file now SKIPS
  that one instrument on the `/movers` screens — logged + counted — instead of 500-ing the WHOLE page,
  gap #4 of the operator's critical-risks resilience-hardening
  batch: a multi-agent audit flagged this the MOST production-likely HIGH — the spot/futures/options candidate
  loops (`movers_snapshot.compute_spot_candidates`, `movers_futures.compute_futures_candidates`,
  `movers_options.compute_options_candidates`) read each instrument's Parquet with NO per-instrument guard, so
  ONE corrupt / version-incompatible file raised out of the loop and — since the route recomputes per call —
  re-500'd EVERY `/movers` open until the file was manually quarantined. NEW `brutex/dashboard/movers_resilient.py`
  with `movers_read_skip_errors()` (the ONE place defining the unreadable-file set — empirically confirmed
  `polars.exceptions.PolarsError` + `OSError`, COMMON) + `log_movers_skip()`; each of the THREE candidate loops
  wraps its per-instrument read in `try/except` that on an unreadable file logs `movers.parquet_skip`
  (source+symbol+reason), increments the screen's existing `skipped` tally, and `continue`s — mirroring
  `db_view._aggregate_parquet_resilient` (the proven coverage-side pattern). HONEST envelope: NARROW catch — a
  genuine code bug (`ValueError`/`KeyError`/…) STILL propagates, never silently swallowed; a skipped instrument
  is OMITTED + counted, never a fabricated price (the file stays for the operator to quarantine); pure
  read-time DETECT-and-skip — no write surface, no engine/lake/scope change. `movers_resilient.py` at 100%
  line+branch; the 3 candidate readers all stay 100% line+branch with NEW integration tests writing a GOOD
  symbol + a SECOND whose on-disk Parquet is overwritten with garbage so the REAL `pl.read_parquet` raises a
  `ComputeError` — the screen still ranks the good one, reports the corrupt one `skipped`, logs the skip, and
  does NOT 500 (one per spot/futures/options); ruff/mypy --strict/pyright clean. READ-ONLY dashboard — ENGINE
  UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged: DEC-LAKE-WRITE-HALT-001,
  MIGRATION.md #885 (disk-full / OOM at the lake-write boundary now HALTS the
  pull gracefully — a `LakeWriteHalt` global stop — instead of crashing raw or quarantining per-symbol, via a
  shared `atomic_io` write helper, gap #3 of the operator's
  critical-risks resilience-hardening batch: NEW `brutex/data/errors.py` `LakeWriteHalt(DataLakeError)`
  (`reason` = `disk_full`/`out_of_memory`) + NEW `brutex/data/atomic_io.py` (`lake_write_guard` maps
  `ENOSPC`/`MemoryError`→`LakeWriteHalt`, every other error UNCHANGED; `atomic_write_via` = the ONE
  tmp→fsync→`os.replace`→cleanup). BOTH lake writers delegate to it; every allocation/failure site is guarded
  (a 4-agent hostile review folded two MAJORs — the spot FRESH render + the steady-state MERGE READ are both
  guarded, and `mkstemp` itself runs inside the guard) + `LakeWriteHalt` added to BOTH ingest spines'
  `_GLOBAL_STOP_ERRORS` so a full disk propagates past the per-symbol/contract quarantine and halts (temp
  always cleaned first — no half-written partition, no fabricated data). HONEST: graceful DETECTION + halt,
  NOT a memory CAP (a true RSS ceiling needs setrlimit/cgroups — OUT OF SCOPE); a non-`ENOSPC` `OSError`
  re-raises as-is (never mis-escalated); only the 2 data-lake writers consolidated (the other 5 atomic-write
  copies remain noted COMMON debt). `atomic_io.py`
  + `errors.py` 100% line+branch; the 2 writers + 2 ingest spines stay 100%; ruff/mypy --strict/pyright clean.
  ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior merged:
  DEC-CLI-LEDGER-CHECKPOINT-001, MIGRATION.md #884 (the `brutex fetch` CLI now checkpoints the durable
  window ledger MID-pull per window, not only at pull end, so a crash recovers in seconds instead of
  re-pulling the whole span — gap #2 of the operator's
  critical-risks resilience-hardening batch: `cmd_fetch` threads the SAME throttled `make_live_ledger_flush`
  the dashboard uses as the ingest engine's per-WINDOW `on_window` hook, wrapped in a NEW single-flight
  `window_ledger_store.make_locked_flush` (the `on_window` fires from PARALLEL worker threads + the throttled
  flush is not itself thread-safe); the CLI wants the flush ONLY so it does NOT use `make_live_sync` (which
  no-ops without an `on_ledger_sync`). A `None` ledger (`--dry-run`/`--inspect`) → no-op (the lock wraps a
  no-op), persistence-free contract preserved. Recovery bounded to one throttle interval (NOT bit-for-bit
  mid-window — a re-pull re-fetches; tmp→atomic-rename + keep-first sink absorb the duplicate). O(1) lock
  per window; the adaptive flush throttle still bounds the write rate. `window_ledger_store.py` 100%
  line+branch (177/50, incl. an 8-thread single-flight test); CLI fetch + window-ledger suites (72) green;
  ruff/mypy --strict/pyright clean. CLI/ledger-store only — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no
  scope/vocab/rate-cap change. Prior merged: DEC-GROWW-SERVER-ERROR-RETRY-001, MIGRATION.md #883 (a transient
  Groww 5xx server error is now a RETRYABLE `GrowwServerError`, not a terminal quarantine — gap #1: NEW
  `GrowwServerError(GrowwError)` in
  `brutex/adapters/groww/errors.py`; `_CODE_TO_ERROR` maps `500`/`502`/`503` → it (504 deliberately stays a
  retryable `GrowwTimeoutError` — the timeout ladder already governs it) + a phrase-only `_SERVER_ERROR_HINTS`
  checked AFTER the rate-limit/timeout hints (priority preserved); added to `is_retryable` but NOT
  `is_rate_limit` (a 5xx is no quota signal — it rides the GENTLE timeout ladder, never steps the per-second
  rate ceiling down). The three consumption sites (`client.py`/`ingest.py`/`option_ingest.py`) pick it up via
  `is_retryable`/`is_rate_limit` with ZERO extra code; a real `404` stays terminal. `errors.py` 100%
  line+branch (48 stmts/16 branches); groww client/ingest/retry suites (140) green; ruff/mypy --strict/pyright
  clean. Adapter-only — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX), no scope/vocab/rate-cap change. Prior
  merged: DEC-OPTIONS-LAKE-READER-001, MIGRATION.md #882 (the options executor's `OptionBarSource` seam now
  has a PRODUCTION lake reader — gap-analysis item 1: NEW
  `brutex/options/lake_source.py` `LakeOptionBarSource` resolves a leg's `(instrument, expiry, strike,
  option_type, entry_ts, exit_ts)` to its F&O-candle-lake contract — DISCOVERED by scanning +
  `parse_fno_symbol`-matching the `bars/<exch>/FNO/` dirs (cached per underlying, robust, not a reconstructed
  string) — reads its 1-minute bars into a cached `{instant→OptionBar}` map, and returns the entry+exit
  `OptionBarPair` matched on the EXACT UTC minute, or `None` (never a fabricated price). `side`/`strike` from
  the query; `spot`/`iv` honest `None` (the candle lake stores neither). O(1) per `load_pair` after the
  per-contract read. `lake_source.py` 100% line+branch (13 unit) + 2 executor-integration tests pricing a
  real round-trip through `materialize_trade`; options suite (53) + F&O smoke (77) green; ruff/mypy
  --strict/pyright clean (a hostile-review BLOCKER on a mis-coded `# type: ignore` was folded — row typed
  `Mapping[str, Any]` like `movers_snapshot._row_to_bar`). REFERENCE_ONLY F&O lake; ENGINE UNCHANGED
  (NIFTY/BANKNIFTY/SENSEX). Prior merged: DEC-DB-FNO-COLUMNS-001, MIGRATION.md #881 (PR #835: the `/db` F&O candle view gains four read-time
  derived value columns: `spot` · `intrinsic` · `time_value` ·
  `days_to_expiry` appended after `moneyness`, ALL riding the SAME single per-bar underlying-spot join
  `moneyness` already does (no extra read). `db_view._fno_moneyness_rows` → `_fno_derived_rows` (5-tuple
  from ONE join) + pure `_option_intrinsic`; a future/undecodable reads `—`/`None` values but still gets
  `days_to_expiry`; an option with no spot reads `?`/`None` (never a fabricated price). `FNO_CANDLE_COLUMNS`
  advertises the four as `source="derived"`; the candle grid renders them column-generically. The stated
  part-2 follow-up to the #880 cascade. `/db` F&O moneyness/schema/datasets/candle-lake suites green;
  ruff/pyright/mypy --strict clean. READ-ONLY viewer; ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX). Prior merged:
  DEC-DB-FNO-CASCADE-001, MIGRATION.md #880 (PR #834: the `/db` F&O dataset
  selector was ONE flat dropdown of thousands of contract partitions (impossible to find a precise strike —
  the operator's screenshot). NEW pure `brutex/dashboard/fno_cascade.py` `build_fno_cascade` groups the
  decoded F&O datasets into a 3-level `FnoCascadeIndex` (O(1) `underlyings`/`expiries`/`contracts`,
  futures-first then numeric-strike sort, interval-aggregated, `unparsed` surfaced) + a generation-stamped
  `FnoCascadeCache` + 3 lazy routes `GET /api/v1/db/fno/{underlyings,expiries,contracts}` (COMMON
  `_fno_cascade_or_none` helper) + the `db.html` 3 dependent selects (the contract pick materialises the
  dataset so the SHARED candle query path is byte-unchanged; spot/option keep the flat dropdown). Derived
  from the LAKE partitions (not the empty `contracts` registry). `fno_cascade.py` at 100% line+branch
  (14 unit + 8 route/frontend integration); broader `/db` suite (229) green; ruff/pyright/mypy --strict
  clean. READ-ONLY viewer; richer F&O columns (days_to_expiry/spot/intrinsic/time_value) are the stated NEXT
  PR. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX). Prior merged: DEC-FNO-WINDOW-PLAN-001, MIGRATION.md #879
  (PR #833: the F&O (options + futures) pull now PRE-PLANS every
  `(contract, interval)` window as `PLANNED` BEFORE any fetch, so the `/coverage/windows` Options +
  Futures tabs show a real planned total + units, exactly like spot — the operator's "why no planned
  items + units for futures and options?". Root cause: a `PLANNED` row was only ever seeded by spot's
  `plan_windows` (all 4 call sites are `market=GROWW_COVERAGE_MARKET`); F&O wrote a row ONLY at fetch
  time, so both tabs read 0. NEW shared `option_ingest._clamped_window_key` (ONE source of truth for the
  derivative ledger key — `ingest_option_contract` refactored onto it so the planned key is BYTE-IDENTICAL
  to the fetched key: the "planned == fetched" no-ghost invariant) + NEW `plan_contract_windows` (the F&O
  twin of spot `plan_windows`: idempotent plan + `prune_superseded`) WIRED into `run_groww_option_ingest`
  (auto-covers options `groww-option` + futures `groww-future`). The VIEW needed ZERO change —
  `build_window_payload` already buckets + counts PLANNED per namespace. `option_ingest.py` at 100%
  line+branch (50 pre-existing + 9 new + 1 e2e integration); ruff/pyright/mypy --strict clean. READ-ONLY
  F&O lake (REFERENCE_ONLY); ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX). Prior merged:
  DEC-PULL-MOVERS-WARM-001, MIGRATION.md #878 (PR #832: BINDING the gated phase-4
  warmer in the `/pull-pipeline` route. After a fully-successful fetch it pre-computes the default
  spot/futures/options movers snapshots for the LATEST AVAILABLE day (`latest_movers_date` — never
  'today', so never an empty illusion) into the SAME `MoversSnapshotCache` at the post-pull generation,
  reusing the EXACT `candidate_key`/`compute_*_candidates` the `/api/v1/movers` route uses. NEW pure
  `brutex/dashboard/movers_warm.py` (`latest_movers_date` + best-effort `warm_movers_snapshots`) at 100%
  line+branch; a warm failure NEVER fails the successful pull (movers also still auto-refresh on next
  open via the generation bump). READ-ONLY accelerator; ruff/pyright/mypy --strict clean. ENGINE UNCHANGED.
  Prior merged: DEC-PULL-SELECTED-PIPELINE-001, MIGRATION.md #877 (PR #831: the `/pull-pipeline` route + a new
  "▶ Pull selected — full pipeline" button run the gated spot→expiries+contracts→historical chain over ONLY
  the ticked instruments (`?symbols=`; no selection → whole universe, no regression), via the SHARED
  `resolve_pull_targets` resolver (COMMON; `/retry-failed` refactored onto it). `run_full_pipeline` gains an
  OPTIONAL gated 4th phase `run_movers` + `PipelinePullReport.movers_warmed` that runs ONLY after a
  fully-successful fetch (`historical_phase_ok`) — the operator's "never run movers unless the fetch
  succeeded". The WARMER is NOT yet bound in the route (a today-coordinate warm would cache empty snapshots
  right after a prev-day pull — an illusion avoided; movers already auto-refresh via the generation bump).
  `pipeline_pull.py` at 100% line+branch; ruff/pyright/mypy --strict clean. ENGINE UNCHANGED. Prior merged:
  DEC-MOVERS-COMPLETENESS-001, MIGRATION.md #876 (PR #830: the `/movers` page defaults Top-N to 100 and offers ALL
  21 Track-1 timeframes (1m + 19 derived intraday + 1d), every one derived on-the-fly from the stored
  1-minute via `resample_1m_to_tf` — NO pre-materialised partition needed; the only prerequisite is
  1-minute spot + F&O candles. Backend already supported all 21 (`_INTERVAL_MINUTES` ← `LAKE_INTERVAL_LABELS`);
  the change is the dropdown (6→21) + the `TF_MIN` JS map + the three Top-N defaults 50→100 + the route cap
  `le=200`→`500`. Per-metric formulas re-verified correct. READ-ONLY display polish; movers_snapshot/futures/options
  stay 100% line+branch; ruff/pyright/mypy --strict clean. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX). Prior merged:
  DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001, MIGRATION.md #875 (PR #829: the economics sibling of #874 — a parse-time
  audit proving `lot_size` is uniform PER (underlying, expiry) across the whole `instrument.csv` (a within-expiry
  mismatch is RED), with cross-expiry lot revisions (legit NSE quarterly — AMBUJACEM 1050→1200) shown as
  INFORMATIONAL, never RED. The HONEST design (real-CSV-grounded, no illusion): lot/tick POSITIVITY is already
  upstream-guaranteed by `Instrument.__post_init__` (not re-checked — dead code), and tick_size legitimately
  varies by strike/premium band within an expiry (NOT checked — would false-flag 350 groups). NEW pure
  `brutex/data/economics_integrity.py` `audit_economics_integrity` (one O(legs) sweep, O(1) lookups,
  O(expiry-groups) space; excludes `*NSETEST*`; capped samples) + an ingest hook `_persist_economics_integrity`
  (structlog + persist to `schema_meta`) + routes `GET /api/v1/universe/economics-integrity[.csv]` (CSV via NEW
  `economics_integrity_csv.py` reusing #873's `FormulaSafeWriter`) + a green/red `/universe` "Economics integrity
  (lot size)" panel. Both new modules at 100% line+branch (8 unit + 2 property + 3 CSV + 1 golden + 3 integration);
  instrument_loader stays 100%; ruff/pyright/mypy --strict clean. REAL-FILE PROOF: the actual 164,248-row
  `instrument.csv` → GREEN, 101,792 legs over 684 (underlying,expiry) groups, 0 within-expiry mismatches, 45
  legit cross-expiry revisions surfaced informational. READ-ONLY, DETECT-only — assurance/observability, NOT a
  backtest-pull blocker. NO pull/engine/scope/vocab change — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX). Prior merged:
  DEC-INSTRUMENT-MASTER-INTEGRITY-001 #874 (PR #828, parse-time leg↔spot mapping audit, real-CSV-verified) +
  DEC-MAPPING-INTEGRITY-EXPORT-001 #873 (PR #827, downloadable mapping-integrity CSV + golden gate + shared CSV guard) +
  DEC-ONE-CLICK-PIPELINE-001 #872 (PR #826, one-click full-universe pull pipeline spot→metadata→historical, gated) +
  DEC-FUTURES-PULL-ENABLE-001 #871 (PR #825, formally enable futures pulling, supersede never-pull decree) +
  DEC-MAPPING-INTEGRITY-RECONCILE-001 #870 (PR #823, real-time spot↔options mapping-integrity audit) +
  DEC-FNO-MONEYNESS-LABEL-001 #869 (PR #824, /movers + /db moneyness + strike-distance as one cell) +
  DEC-SPOT-COMPLETENESS-2020-FLOOR-001 #868 (PR #822, spot late-start → partial, Jan-2020 floor) +
  DEC-DB-CANDLE-COUNT-CACHE-001 #867 (PR #821, /db candle-pager total cache) +
  DEC-UNIVERSE-BACKEND-CACHE-001 #866 (PR #820, /universe backend O(1) content-token cache) +
  DEC-FNO-MONEYNESS-001 #865 (PR #819, per-option moneyness ITM/ATM/OTM + signed ATM±N) +
  DEC-DB-VIEWER-PAGINATE-ALL-001 #864 (PR #818, every /db tab O(page)) +
  DEC-DB-UNIVERSE-O1-LATENCY-001 #863 (PR #817, /db+/universe instant) +
  DEC-LIVE-REMOVE-001 #862 (PR #816, entire LIVE feed removed) +
  DEC-FNO-CANDLE-LAKE-001 #861 (PR #814, F&O candle lake first-class `/db` table + schema_meta) +
  DEC-MOVERS-O1-LATENCY-001 #860 (PR #815, /movers near-O(1) snapshot cache + month-bounded
  reads) + DEC-OPTIONS-COMPLETENESS-REAL-YEARS-001 #859 (PR #813, /completeness OPTIONS shows
  real expiry years + honest `partial`) + DEC-UNIVERSE-CONSTITUENTS-TICK-IN-PLACE-001 #858
  (PR #812))
---

## 2. Golden Rules (never violate)

1. **No hallucination.** Every factual claim about Groww, NSE, BSE, NIFTY
   composition, brokerage, taxes, or any external system must be
   traceable to a verified source. If unsure, say "unverified" and stop.
2. **Single source of truth.** All Track 1 specs live under
   `docs/TRACK1_*.md` + `docs/track2/TRACK2_*.md`. Every PR references
   a section + decision ID from `docs/MIGRATION.md`. The legacy
   `docs/REQUIREMENTS.md` is a retired stub (banner only) — do not
   read or derive new requirements from it.
3. **Scope lock.** Track 1 = Groww + spot indexes + India VIX (NIFTY 50,
   BANKNIFTY, SENSEX, INDIA VIX). **India VIX is REFERENCE ONLY** — it
   is fetched + stored and stamped as `vix_at_entry` / `vix_at_exit` on
   every **OBSERVABLE** trade (DEC-VIX-STAMP-WIRE-001, MIGRATION.md
   #1645 — persisted finalist rows additionally carry the derived
   nullable `vix_mean`/`vix_min`/`vix_max` trio; the ONLY unstamped
   trade streams are the FIVE named DISCARDED/unobservable streams —
   the Stage-4 cascade candidate re-simulation (stamps would be dropped
   at the CandidateInput scalar conversion; report bytes pinned
   identical — the round-6 review DELETED the cascade chain's dead
   stamper seam plus the two manual-replay `vix_stamper_for_manifest`
   builds that paid a full VIX lake read per replay/click for zero
   output), its Monte-Carlo permutation filter, `run_persisted_sweep`'s
   K-of-N inner stream, the CLI options-sim replay (stamps would be
   dropped at the SpotSignal conversion) and `brutex sweep`'s
   text-report replay (report bytes pinned identical) — each an
   explicit literal `reference_stamper=None` enumerated by an AST pin
   whose exact-count laws also REFUSE a silently-omitted kwarg
   and positively pin the entry-point binds (round-2 panel hardening);
   the trio survives redo honestly — `None → value` backfills, a
   value-vs-value clash raises the typed tuple-skip
   `VixTrioConflictError` (the VIX lake moved, NOT non-determinism) and
   `sweep-verify --recount` stamps like the launcher + attributes a
   VIX-only divergence as the non-corrupt `recount_vix_divergence`;
   a missing VIX bar stamps `None`, never fabricated), but is
   NEVER part of brute-force pattern combinations or the 52-condition
   vocabulary (nor `_RANKABLE_FIELDS`, nor the BLAKE3 run identity). 21 timeframes — **`1m` is the ONLY
   granularity fetched** and the 19 derived intraday TFs + `1d` are all
   DERIVED from it (DEC-1MINUTE-ONLY-DERIVE-1DAY-001, MIGRATION.md #853; the
   `1d` daily anchor is resampled to midnight-IST from the stored 1-minute and
   written to the lake's 1-day partition — since DEC-PIPELINE-AUTOPREBUILD-001
   (MIGRATION.md #1145) by the pull pipeline's automatic FINAL prebuild stage,
   never mid-pull, never separately vendor-fetched; since
   DEC-PREBUILD-CLEANSET-001 (MIGRATION.md #1221) that stage runs on every
   completed pull over the CLEAN symbol set — quarantined symbols excluded +
   headlined, no longer gated on a fully-clean fetch).
   Rolling `2020-01-01 → today` range (the data pull floor
   — `GROWW_HISTORY_FLOOR`; DEC-PULL-FLOOR-2020-001, 2026-06-13, MIGRATION.md
   entry #722 — supersedes the prior conservative `2021-01-01` floor of
   DEC-DHAN-PULL-SCOPE-001 now that the Phase-0 granularity probe confirmed
   Groww serves 1-minute data from `2020-01-01T09:15:00`). No futures, no
   options, no F&O contracts. Do not silently expand scope.

   **Engine-scope amendment (DEC-NIFTY-DYNAMIC-STRATEGY-001 / ADR-0012,
   2026-06-29, MIGRATION.md entry #1006; companion clause owned by PR-1
   DEC-VOCAB-U128-CODEGEN-001 #1010):** the operator has AUTHORIZED the
   brute-force ENGINE to ALSO run on **NIFTY futures** for the dedicated
   dynamic-strategy track (a session-anchored intraday VWAP is feasible on
   futures because they carry traded volume, unlike a spot index per
   DEC-VENDOR-VOLUME-NORMALIZE-001). This is operator-authorized, NOT a silent
   expansion. The 3 spot indices (NIFTY 50 / BANKNIFTY / SENSEX) remain the
   index engine surface; **all OTHER F&O — every non-NIFTY-futures contract,
   the ~212 F&O stocks, the NTM constituents, India VIX — stays REFERENCE_ONLY**
   (lake-only, never in the 52→60-condition vocabulary). The vocabulary itself
   grew 52→60 conditions on a u128 mask in PR-1. **PR-2 (DEC-NIFTY-INDICATORS-001,
   #1011) supplied the indicator math for the VWAP-position (bits 52/53) + pivot
   R5/S5 (bits 54/55) conditions + the two new Fibonacci anchors (rolling-weekly /
   weekly-static, NO new bits). PR-2b (DEC-NIFTY-BOS-CHOCH-001, #1012) then
   supplied the BOS/CHoCH market-structure math (bits 56-59) — a look-ahead-clean
   fractal swing-detection state machine (`indicators/market_structure.py` +
   `rust/trading-core/src/indicators/market_structure.rs`, a swing confirmed only
   k bars later, proven P-06-clean by a dedicated `LookAheadBarrier` test) — so
   ALL 60 conditions are now `implemented=true` (`IMPLEMENTED_MASK = (1<<60)-1`)
   and the data-independent indicator vocabulary is COMPLETE.** **PR-5
   (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 / DEC-FUTURES-ADDRESSING-PIN-001 /
   DEC-STAGE1A-ROLL-RESET-001, MIGRATION.md #1700–#1702) LAUNCHED the futures
   sweep: the track is addressed by the ONE pinned series alias
   `NSE-NIFTY-FUT` (DEC-FUTURES-ADDRESSING-PIN-001 — supersedes the
   pre-launch dual-acceptance envelope; the retired `NIFTY-FUT-YYYY-MM-DD` /
   `NSE-NIFTY-<DDMmmYY>-FUT` shapes get a did-you-mean refusal), roll-resolved
   at launch by `brutex/data/futures_series.py` into the per-contract
   `bars/NSE/FNO/NSE-NIFTY-<DDMmmYY>-FUT` lake partitions (vendor-parsed
   expiries govern ownership; expiry day belongs to the EXPIRING contract) and
   stitched by `brutex/feed/series_load.py` with a hard per-contract Stage-1A
   reset at every roll (D11 — no level ever crosses the basis gap). The
   engine sweep surface is EXACTLY {the 3 spot index triples} ∪
   {NSE/FNO/NSE-NIFTY-FUT}.**

   **Scope expansion (DEC-P0-063, 2026-05-22, MIGRATION.md entry
   #200):** Track 1's INGEST scope was formally expanded to additionally
   cover **200 NSE F&O eligible single stocks (spot + options across the
   vendor's expiry/contract addressing — Groww `get_expiries` →
   `get_contracts`)** for the LAKE. The **BRUTE-FORCE ENGINE is
   UNCHANGED** — it still operates only on the 3 brute-force indexes
   (NIFTY/BANKNIFTY/SENSEX); the 200 stocks join India VIX in
   `REFERENCE_ONLY` (mirrors decision #83). Stock options sit in the
   lake ready for future brute-force expansion (separate decision; not
   authorized here). The "4 index instruments / no F&O contracts"
   wording above describes the ENGINE; the LAKE is the expanded surface
   per DEC-P0-063. The §3 / cross-spec stale-text reconciliation is
   COMPLETE (DEC-P0-063-RECONCILE-COMPLETE-001 — a very-thorough survey
   found the "~15" estimate stale: the DEC-SPEC-CONSISTENCY series
   #262-#266 had already reconciled 11 locations; the 2 genuinely-stale
   remainders were fixed and 1 immutable ADR record correctly left
   as-is). DEC-P0-063 remains authoritative for the stock-options
   expansion.

   **Amended by DEC-INSTRUMENT-UNIVERSE-FAST-001 (2026-05-29, MIGRATION.md
   entry #160):** lean ingest — the `instruments` table now persists ONLY
   the ~241 unique universe underlyings (every admitted index + every
   F&O-eligible single-stock equity); the ~89,000 FUT/CE/PE legs are
   PARSED for eligibility then DISCARDED, recoverable on demand via
   `get_contracts` (contracts inherit lot/tick/freeze from the underlying
   row). The BRUTE-FORCE ENGINE is UNAFFECTED. So "the LAKE is the expanded
   surface" is narrowed: the lake = the universe underlyings; the legs are
   derived/recoverable, not stored.

   **Leg-economics fold (DEC-GROWW-LEG-ECONOMICS-001, 2026-06-13, MIGRATION.md
   entry #696):** before the legs are discarded, each persisted underlying is
   **stamped with its tradable F&O lot + freeze** harvested from its derivative
   legs (`universe.build_underlying_economics` →
   `dashboard/instrument_loader._fold_leg_economics`) — RELIANCE → 500, NIFTY →
   75 — SUPERSEDING the IDX/EQ row's cash placeholder (`lot_size` 1 / 0); the
   spot `tick_size` is preserved (a spot tick differs from its future's). This is
   the Groww twin of the retired-vendor fold; without it every F&O equity kept
   the placeholder `lot_size = 1`, which left `has_placeholder_stock_economics`
   permanently True and made the dashboard auto-heal re-load the master on every
   startup (a silent loop this closes).

   **Re-amended by DEC-NTM-GROWW-REPOINT-001 (2026-06-13, MIGRATION.md entry
   #694):** the lean universe ALSO persists the matched **NIFTY Total Market
   constituents** as REFERENCE_ONLY rows — the ~540 non-F&O names on top of the
   ~212 F&O-eligible stocks + the admitted indices, widening the lake to **~750
   underlyings**. The constituents are fetched best-effort from niftyindices
   (vendor-agnostic) and matched to Groww NSE cash-equity rows by ISIN / ticker
   (`dashboard/instrument_loader.py` `_ntm_admission`). The **BRUTE-FORCE ENGINE
   remains UNCHANGED** (NIFTY/BANKNIFTY/SENSEX) — NTM constituents are
   lake/REFERENCE_ONLY only, NEVER in the 52-condition vocabulary (the hard
   scope guard; an F&O-eligible name that is ALSO an NTM constituent stays
   tradable, `reference_only=FALSE`). On a network miss with no cache the load
   degrades honestly to the F&O-eligible base set, never invents constituents.
4. **Reproducibility first.** Every run is identified by `blake3(
   strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖ canonical_params
   ‖ data_snapshot_hash ‖ indicator_version ‖ code_commit_hash)`. No
   computation without that hash recorded. `data_snapshot_hash` is the
   FULL-CONTENT streamed `bxsnap2` digest of every loaded bar's 7 fields
   (`stage3_wire.data_snapshot_hash`, DEC-SNAPSHOT-HASH-FULL-001 — any
   single-bar difference re-keys the identity; supersedes the sampled
   count+endpoints fingerprint). (Was `sha256(...)` pre-DEC-CALIBRATE-001;
   corrected 2026-05-24 to match the spec's run-identity formula — now at
   `TRACK1_BACKTEST_SPEC.md:760` after spec growth moved it from :743
   (earlier :654; DEC-CLAUDEMD-SPEC-LINE-RECONCILE-001) — which has used blake3 since
   the v2 spec lock 2026-05-16. See MIGRATION.md entry #204.)
5. **O(1) discipline.** Bar lookup, indicator value lookup, combo dedup
   (Bloom + LMDB), strategy mask evaluation (u64 AND), and result
   append must all be O(1). The decree TARGET is
   `p99(N=1M) / p99(N=1K) ≤ 1.1` per hot path; ENFORCEMENT is the
   two-tier gate defined once in `tests/bench/_o1_tiers.py`
   (DEC-O1-CLAIMS-HONESTY-001 wording reconcile — no gate value
   changed): every O(1) bench asserts a robust/mean-based **3–10×**
   ratio ceiling on the shared 2-core GitHub runners (DEC-P0-020 —
   the raw 1.1× is too noisy to gate there without flaking), and the
   decreed strict **1.1×** tier is enforced where
   `BRUTEX_M4PRO_STRICT=1` on the dedicated self-hosted M4-Pro runner
   (DEC-O1-STRICT-TIER-001; the runner is REGISTERED and LIVE —
   2026-07-07, `bench-m4pro.yml` header + actions run 28888100079 — so
   the strict tier EXECUTES on that workflow's path-triggered pushes,
   while the shared-CI tier remains the gate on every push), with a
   measured **3×** cache-tier allowance for ratio tests whose big/small
   workloads cross a CPU cache tier (DEC-O1-CACHE-TIER-001 —
   memory-tier physics, not an algorithmic regression;
   `cache_tier_ceiling` never returns looser than the shared default)
   and a measured **3×** metadata-sweep allowance for ratio tests whose
   big/small workloads differ by a feature's own documented O(D)
   metadata sweep (DEC-LAKE-STATS-MANIFEST-001 —
   `metadata_sweep_ceiling` / `METADATA_SWEEP_STRICT_CEILING` in
   `tests/bench/_o1_tiers.py`, calibrated off run 28888100079's
   measured 1.53×; likewise never looser than the shared default). Any
   regression past its tier's ceiling fails CI.
6. **Rate-limit safety.** Groww publishes type-level limits (the
   **"Live Data"** group — Market Quote / LTP / OHLC — is **10 r/s,
   300/min**; `docs/groww/api-reference/01-introduction.md:130-134`).
   The **backtesting `get_historical_candles` per-second cap + daily
   quota are UNDOCUMENTED** and are an OPEN Phase-0 probe gate
   (`brutex/adapters/groww/constants.py:141-143`
   `GROWW_RATE_LIMIT_RPS_UNVERIFIED=10.0`,
   `GROWW_DAILY_QUOTA_UNVERIFIED=None`; ADR-0009 §3.1 risk #4 / §4) — the
   `_UNVERIFIED` suffix carries the honesty requirement in the
   identifier. The adapter paces with the vendor-neutral adaptive
   token-bucket controller (`brutex/adapters/rate_common.py:372`
   `RateController`, injected `ceiling_rps`; the 429/timeout ladders step
   down to a deep floor of **1 r/s** under sustained pressure and recover
   up to the ceiling once it clears). On TOP of the per-second bucket the
   controller now runs an **operator-confirmed 500 req/min HARD governor**
   (a rolling-60s issue-count cap, injected `ceiling_per_min` =
   `GROWW_RATE_LIMIT_PER_MIN=500`; **no daily quota** for backtesting —
   both operator-confirmed 2026-06-13, NOT vendor-documented;
   DEC-GROWW-RATE-PERMIN-001). The governor only ever LOWERS the issue
   rate (it caps; it never raises a ceiling), so it is a safe addition;
   never raise the per-second ceiling OR the per-minute cap without
   explicit user approval. The PRODUCTION per-second ceiling injected at
   BOTH live build sites (dashboard `_build_live_controller` + CLI live
   pull) is now the operator-DIRECTED **8 r/s**
   (`GROWW_RATE_LIMIT_RPS_CEILING=8.0`, operator-approved 2026-06-24, RAISED
   5 → 8, DEC-GROWW-RATE-CEILING-8-002 — supersedes DEC-GROWW-RATE-CEILING-5-001)
   — raised back to 8 (with EXPLICIT, repeated operator approval) once
   DEC-FETCH-WRITE-OVERLAP-001 Lever 2 (pool 2→4) gave the pull enough connections to
   GENERATE demand toward the ceiling (at 2 connections the achieved rate capped at
   ~3.2 r/s so the 5–8 range was pure waste; at 4 connections demand reaches ~6.4 so 8
   is a FAIR test of vendor headroom). 8×60 = 480/min stays UNDER the 500/min governor,
   so the HARD per-minute cap can never be overshot (DEC-GROWW-GOVERNOR-ATOMIC-001). A
   CEILING raise only ever ALLOWS a higher rate — it does NOT force it: the adaptive
   AIMD ladder still steps DOWN on a 429 burst (soft floor 3, deep floor 1) and recovers
   UP only as far as the vendor tolerates, so the worst case at 8 is identical to 5 (the
   controller self-corrects). HONEST (Golden Rule #10): a live-log audit showed the vendor
   429ing AT the 5.0 ceiling, so SUSTAINED 8 is UNVERIFIED — the controller settles wherever
   the vendor allows; revert to 5 is this one constant if 8 only burns 429-retries without a
   throughput gain (the #906 trap). The per-second backtesting cap itself stays officially
   UNVERIFIED (`GROWW_RATE_LIMIT_RPS_UNVERIFIED`). (The vendor-neutral `rate_common.CEILING_RPS=5`
   DEFAULT is unchanged.)
7. **Idempotence.** Every ingest chunk and every backtest combo must be
   rerun-safe. Same inputs → same outputs, bit-for-bit. The audit-log
   replay test is the oracle.
8. **Audit trail.** Every material action (ingest chunk, combo run,
   error, kill) appends one line to a hash-chained JSONL audit log.
   Breaking the chain fails CI.
9. **Hostile review before code.** Major design changes get a
   parallel-agent hostile review (Plan + general-purpose + Explore)
   before implementation. See the 4-agent pattern in
   `docs/TRACK1_PHASE_PLAN.md §0`.
10. **Honest limits.** If an SLA cannot be met under the current
    hardware envelope, SAY SO. No "millions in minutes" claims without
    naming the hardware AND citing public benchmarks. The 4-agent
    review on 2026-05-15 caught a 10x-overoptimistic throughput claim;
    do not repeat that pattern.
11. **COMMON · DYNAMIC · SCALABLE · AUTOMATED** (operator decree 2026-05-16).
    Every module obeys all 5:
    - **COMPREHENSIVE** — streaming + batch interface, bit-exact oracle
      test, look-ahead invariant test, serialize/restore.
    - **COMMON** — one source of truth (this repo). Every utility
      lives in exactly one place; downstream modules import. No
      copy-paste between modules.
    - **DYNAMIC** — add new indicator/TF/condition/mask without
      re-architecting. Plugin-style state classes, u64 mask vocab.
    - **SCALABLE** — same code path Day 1 (12.6M) → Phase 10 (730B).
      O(1) per-bar / per-mask / per-dedup / per-write.
    - **AUTOMATED** — 100% automated after one-time bootstrap.
      Release Please + auto-merge + watchdog + Telegram + auto-resume.
      Operator monitoring: ZERO clicks per day.
    See `docs/TRACK1_BACKTEST_SPEC.md` §"COMMON · DYNAMIC · SCALABLE ·
    AUTOMATED" for the full rule + enforcement matrix + violations list.
12. **100% ALWAYS STANDARDS** (operator decree 2026-05-16 evening).
    Every branch, every PR, every sub-PR, every commit, every task,
    every sub-task, every plan, every doc, every spec, every agent run
    obeys 18 standing requirements:
    coverage 100% line + 100% branch; mutation ≥ 85%; type-check
    (mypy strict AND pyright); static analysis (ruff + pylint + bandit);
    zero duplication; functionality + scenario completeness; bug-fix
    regression coverage; 7 test types (unit + integration + property +
    golden + bench + stress + mutation); CI every push + nightly soak;
    O(1) per hot path (p99 ratio ≤ 1.1); throughput canary ≥ 50 M/s;
    Bloom + LMDB 4-tier dedup; audit JSONL hash-chain on every
    material action; observability 4 pillars (tracking + logging +
    dashboard + alerting); 7 explicit SLAs; deep-research protocol
    (4-agent for major, 2-agent for compliance, 1-agent hostile for
    refactor); decision ID for every locked decision in MIGRATION.md.
    See `docs/STANDARDS_100PCT.md` for the full enforcement matrix +
    tooling per gate + auto-driver-test discipline. Any PR with a
    red gate is BLOCKED; operator override requires ADR.

    **Honest envelope (Golden Rule #10 + DEC-P0-040 / DEC-P0-042
    rebaselining):** The 18-requirement decree above is the **target**.
    The actual CI enforcement matrix in `docs/STANDARDS_100PCT.md` §1
    is **16 of 21 sub-rows 🟢 ENFORCED + 1 🟡 PARTIAL + 1 📋 ROADMAP +
    3 👁️ MANUAL** (~76% fully enforced, per the summary count in
    `docs/STANDARDS_100PCT.md` §1, post-DEC-COV-CLI-HARNESS-001
    2026-06-12 — coverage rows 1/2 flipped 🟢 when the omit list was
    deleted at a measured package-wide 100.00% line+branch; the one
    🟡 is row 3 pending DEC-MUTATION-RUNTIME-003's verification run
    (the first green SCHEDULED `mutation.yml` run; supersedes -002's
    never-verified run — MIGRATION.md #1597).
    Earlier: row 3 honestly re-classified 🟡 while the orchestrator
    mutation pass exceeded its weekly budget, DEC-MUTATION-RUNTIME-001;
    the prior "15/2 (~71%)" wording here was the pre-re-classification
    snapshot, caught by the 2026-06-11 honesty audit per this file's own
    that-doc-wins rule and reconciled by DEC-HONESTY-AUDIT-RECONCILE-001).
    The earlier "9 of 21 / ~22%" wording here was a stale 2026-05-16
    snapshot from before the DEC-STANDARDS-AUDIT-001 → DEC-STRESS-001
    ratchet arc (audit + CROSSWALK + PYLINT-DROP + DOCTOR + STRESS
    closed 6 rows); reconciled by DEC-DOC-STANDARDS-RECONCILE-001
    (MIGRATION.md entry #591). `docs/STANDARDS_100PCT.md` §1 remains the
    single source of truth — if the two ever disagree, that doc wins and
    this line is the stale copy to fix. Per Golden Rule #10 no PR
    may claim "100% standards compliant" without first auditing against
    that matrix. The decree-vs-reality gap is a living ratchet, not a
    bypass — every gap row tracks a dedicated follow-up PR in
    MIGRATION.md. PRs that touch a gap row MUST update the row's
    status. See `docs/TRACK1_REQUIREMENTS_FINAL.md` §2 (DEC-P0-040)
    for the full enumeration.

13. **MERGE-READINESS CHARTER** (operator decree 2026-05-29,
    DEC-MERGE-GUARANTEE-001). Every PR / sub-PR / task / sub-task — in
    this session and every new session — obeys
    `docs/MERGE_READINESS_GUARANTEE.md`: the priority law
    (Correctness > Reliability > Recoverability > Observability >
    Performance > Scalability), the §3 merge-ready gate matrix, and the
    §4 Definition of Done. Two hard invariants: (a) **never merge over a
    red or pending gate** — every ✅ gate green, verified not assumed,
    and `mergeable_state` within the charter §4.5 envelope (strict
    `clean` is the G2-gated target; until server-side branch protection
    lands, `dirty`/`behind`/`blocked`/`unknown` are refused and
    pending-only `unstable` is tolerated —
    DEC-AUTOMERGE-CLEAN-STRICT-001); (b) **honesty** — per Golden
    Rule #10, never claim "100% / fully / guaranteed" for a 🟡/📋/🚫
    tier row; state what is green, partial, and out of scope. The
    charter is enforced on every surface via §8 (this file for sessions,
    `.github/PULL_REQUEST_TEMPLATE.md` for PRs, the `CI OK` required
    check for merges).

---

## 3. Branching and Commits

- **Active feature branch:** `claude/verify-acceptance`
  (DEC-OPERATOR-ACCEPTANCE-VERIFY-001, MIGRATION.md #1756 — the operator acceptance verifier
  `brutex verify-acceptance`: 7 composed checks over the real lake with PASS/FAIL/ABSTAIN(named)
  verdicts + evidence blocks, the gating teeth smoke + CI 20-poison battery, the frozen
  `brutex-acceptance-report-v1` report + hash-chained audit trail, verify-family exits 0/1/2/3/4;
  hostile-review round 1 folded pre-merge (TOCTOU law wired · per-day Muhurat · full moneyness
  grammar · expiry-capped covered-span replan · read-only instruments.duckdb · bounded evidence
  tails · --months made real — per-round record in MIGRATION.md #1756);
  hostile-review round 2 folded pre-merge (coverage-gate test-stem rename · registry schema-drift
  ABSTAIN guard · correct-or-abstain non-default-window-shape replan · pull retry re-reads ·
  Muhurat-date 1day bars disclosed unverified · in-memory master snapshot keeps the drift ladder
  live · seed-rotated F&O sampling + bounded/pruned coverage ledger — per-round record in
  MIGRATION.md #1756);
  hostile-review round 3 folded pre-merge (stdout-pure --json/table via the #1630 logging seam ·
  evidence-tree symlink refusal hoisted before the audit write · both serving-path residuals
  disclosed (registry + .manifest/) · engine set made code-true (INDIAVIX + NSE-NIFTY-*-FUT dirs
  full-history) · off-chain --out · loud SUSPECT on a failed report write · report.txt renders
  limits/routed/Muhurat — per-round record in MIGRATION.md #1756);
  hostile-review round 4 folded pre-merge (whole-run single-flight flock — concurrent runs no
  longer corrupt the audit hash chain · the pull.partition_presence half-open-span fix (healthy
  1-day windows read exit 3; battery poisons 6-8 made real teeth again) · symlink refusal
  extended to the audit child paths · per-leg unique battery teeth on a non-engine twin symbol ·
  the streak_carry block (subset/TEETH_FAILED runs never reset streaks) · the package-wide
  zero-vendor-call AST pin · --json on early-SUSPECT paths · the pyright baseline honesty
  correction — per-round record in MIGRATION.md #1756);
  additive `brutex/acceptance/` package only — ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  matches §1's Active branch line)
  Earlier: `claude/frontend-latency-hardening-r2`
  (DEC-FRONTEND-LATENCY-HARDENING-001, MIGRATION.md #1720 — the dashboard
  FRONTEND latency-hardening pack: v29 duplicate pollers consolidated onto ONE
  BXPoll-disciplined consumer per endpoint (measured BEFORE: 336 req/120 s idle
  with sweep-progress/pull-rate/liveproof double-polled → measured AFTER 240
  req/120 s, −28.6%, same harness), six template pollers
  migrated onto the shared bx-poll.js core (renders byte-identical), /screener +
  /results inputs debounced 200 ms, in-place tbody row patches on
  completeness/spot_coverage, /reports chips build-once + class-toggle
  selection; the bx-format/bx-movers-time defer + the universe catch-up were
  SKIPPED honestly (parse-time consumers + standing pin; already shipped by
  #1623); CLIENT-side only — byte-stable wire; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line)
  Earlier: `claude/db-latency-hardening`
  (DEC-DB-LATENCY-HARDENING-001, MIGRATION.md #1719 — the DB/query-latency
  hardening pack, READ-side only: the /db window-ledger DuckDB page fast path
  (7.3–8.8 s cold at 400k rows → 0.56 s, zero Python parses), the pre-open
  export rows memo (repeat click 206 DuckDB statements → 0, bytes identical)
  + span-bounded IEP reads, and the /db candles %CHNG baseline file bound
  (72 files bound twice per click → 2); healthy-lake payloads byte-identical;
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; matches §1's Active branch line —
  see §1 + MIGRATION.md #1719 for the full blurb)
  Earlier: `claude/movers-bucket-asof-fix`
  (DEC-MOVERS-EXPLICIT-BUCKET-ASOF-001, MIGRATION.md #1601 — an explicit `/movers`
  bucket-anchor TIME pick resolves to the COMPLETED bucket (broker-matching close)
  via the ONE COMMON `movers_snapshot.resolve_asof_cutoff` seam shared by all three
  tabs; READ-ONLY dashboard, ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/movers-futures-hyphen-parse`
  (DEC-MOVERS-FUTURES-HYPHEN-PARSE-001, MIGRATION.md #1646 — the `/movers`
  Futures contract-symbol parse right-anchored by DELEGATING to the canonical
  `data/fno_symbol.parse_fno_symbol`; hyphenated underlyings keep their full
  name so spot + premium resolve; + the W3 fold-in: the movers spot probe
  (futures premium + options moneyness) walks the #1482 spot-drift recovery
  ladder on an exact-key miss via the per-compute
  `movers_futures._SpotLtpResolver`; READ-ONLY dashboard fix; ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; see §1 Active branch for the full
  blurb)
  Earlier: `claude/groww-market-hours-guard`
  (DEC-GROWW-MARKET-HOURS-GUARD-001, MIGRATION.md #1692 — renumbered #1633 → #1673 → #1692 (the reserved-band collision, #1633–#1672 held by main's cascade; then the coordinator reservation map — #1673 reserved for PR #1369); the NSE
  market-hours pull guard: no Groww VENDOR pulls 08:45–15:35 IST on trading
  days, keeping the shared account free for TickVault's live feed; matches
  §1's Active branch line)
  Earlier: `claude/magical-cray-g6agaw`
  (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 + DEC-FUTURES-ADDRESSING-PIN-001 +
  DEC-STAGE1A-ROLL-RESET-001, MIGRATION.md #1700–#1710 — the ADR-0012
  NIFTY-futures sweep LAUNCH capstone; see §1 Active branch for the full blurb)
  Earlier: `claude/oneclick-perf-club`
  (DEC-ONECLICK-PERF-CLUB-001, MIGRATION.md #1711 — coordinator-assigned from
  the reservation map (next free above the #1264 reservation tail #1710;
  live-main max #1665 at assignment — gaps are legal; renumbered from the
  interim provisional #1666) — the queued post-#1373 oneclick
  perf/hygiene club, built
  on PR #1373's head 3c478831 (the PR opens after #1373 lands): F1 the
  /api/v1/oneclick/verify revision cache (the ONE uncached oneclick route;
  key folds manifest + events + top100 tokens + the chain verified-state;
  NEW B-7 bench pin measured 0.95× flat) · C-r2-5 the stat-outage
  serve-last-known doctrine on ALL FIVE cached oneclick routes
  (`artifact_probe` absent≠outage + `OutageGate` + the ONE
  `serve_revision_cached` chokepoint; one WARN per outage per coordinate,
  recovery INFO,
  never a silent per-poll re-parse) · F4 `discover_runs` stat-sorts first and
  parses only the newest-limit manifests (identical selection; 50-of-60
  parse-spy pin) · A-r4 the /screener client-side bounds (Show all capped at
  1000, input cap 5000, node-goldened cap/note math — every truncation NAMED)
  · F2b deliberately SKIPPED as optional polish; DISPLAY zone only; ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; see §1 Active branch for the full
  blurb)
  Earlier: `claude/oneclick-instrument-display`
  (DEC-ONECLICK-WINNER-DETAIL-TRUTH-001 + DEC-ONECLICK-INSTRUMENT-DISPLAY-001,
  MIGRATION.md #1664–#1665, renumbered #1632/#1633→#1633/#1634→#1664/#1665 —
  #1633–#1636 were #1323's reserved block, #1664/#1665 coordinator-assigned
  from the in-flight reservation map — the strategy-detail
  honesty pack,
  viewer-reads-storage only: the winner's `instrument` on every one-click
  surface (ONE `bxInstrumentText` builder; combos Inst cell + `fInst`
  engine-token repoint; /screener "Inst" column + filter); the gross
  signal-P&L relabel + persistent signal-only chip on the /strategy card AND
  the operator hero; the results-depth aggregates (expectancy · recovery ·
  streaks · MAE/MFE · minute-path DD · exit mix · worst-5-days) + the regime
  breakdown rendered from the stored artifact (the stale "lands with P3"
  deferral DELETED); NEW `GET /api/v1/oneclick/equity` over the equity_ref
  sidecar + the /strategy minute-path equity render with the honest
  per-trade fallback (the operator host stays per-trade — the #48
  follow-up); the `/strategy?run_id=&rank=` link contract pinned; nav gains
  /completeness · /screener · /strategy; the two chart-mapper NITs
  (no `Number(null)→0` candles; skipped rows counted + surfaced); HIGH #40
  composite_score /results join DEFERRED (cross-session seam); built on PR
  #1352's head, PR opens after #1352 lands; DISPLAY zone + one additive
  display route; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; see §1 Active
  branch for the full blurb)
  Earlier: `claude/preopen-prevday-common`
  (DEC-PREOPEN-PREVDAY-COMMON-002, MIGRATION.md #1649a — renamed from -001 (main's #1634/PR #1323 claimed -001 for the minute-basis cross-check ADVANCE; -002 = the COMMON-ization remainder + observability + the #1649b Muhurat pack); renumbered
  #1623→#1631→#1649a across the 2026-07-09 merge-reconciles (main took #1623
  for DEC-DASHBOARD-POLL-HYGIENE-001, then #1631/#1632 for
  DEC-ONECLICK-TOP100-OBJECT-ROOT-001 + DEC-DASHBOARD-VISUAL-OVERHAUL-001;
  both-suffixed pair per the 613a/613b precedent); the /reports preopen
  minute-basis calendar-contiguity fix (a real-session lake hole no longer
  fabricates a %CHNG — correct-or-absent) + the #1539-named follow-up CLOSED:
  `_previous_trading_day` delegates to the COMMON
  `nse.previous_trading_session`, which gains the remove-only `observed`
  overlay kwarg; PLUS the same-branch amendment fold
  DEC-PREVSESSION-MUHURAT-COHERENCE-001, MIGRATION.md #1649b — the two-law
  day-after-Diwali coherence ruling: /movers stops false-flagging a healthy
  Muhurat-boundary lake as a lake hole (contiguous-True + EMIT-ON-TRUE
  `prev_close_muhurat`/`oi_baseline_muhurat` + " †" + one INFO, never
  " *"/WARN), preopen names the boundary definitively (`muhurat_session_bar`;
  `extra_nonsession_bar` reworded honestly suspicious), /db gains the static
  %CHNG anchor footnote; READ-ONLY dashboard; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; see §1 Active branch for the full blurb)
  Earlier: `claude/cool-cerf-tq8c27`
  (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001, MIGRATION.md #1594 — the options-tree
  foreign-month + overlap heal; see §1 Active-branch chain for the full blurb)
  Earlier: `claude/movers-underlying-class`
  (DEC-MOVERS-UNDERLYING-CLASS-001, MIGRATION.md #1650 — renumber history
  #1631→#1632→#1633→#1650 (PRs #1352/#1354 took the first two; the slot wave
  reserved #1633–#1649); the `/movers` page gains
  an `All | Indices | Stocks` underlying-class filter on all THREE tabs, a
  rank-time filter over the SHARED cached candidate set (the #1034 min_volume
  template); SPOT class from the lake segment, FUTURES/OPTIONS from the six-step
  pull order's `primary_fno_underlying` + `is_index` map (#1527, reused); default
  `All` byte-identical; ADVERSARIAL-REVIEW FIX ROUND folded — normalize-at-lookup
  for hyphenated/aliased F&O underlyings, right-anchored `parse_future_symbol`,
  `master_revision`-keyed kind-map + F&O candidate memos (warmer included), the
  CI pyright fix; DISPLAY zone only; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/oneclick-top100-object-root`
  (DEC-ONECLICK-TOP100-OBJECT-ROOT-001, MIGRATION.md #1631 — the one-click
  `top100.json` reader-adopts-writer blocker fix: object root + the ONE
  reader-boundary metric-key map + writer→reader golden + fixture corpus
  regenerated from the writer shape; folds [11] sort-basis labels,
  [W2-4] null-cost gross-equity honesty, [W2-5] screener render-cap count,
  [12] runs-listing manifest-token cache key, [W3-R1] the /strategy candle
  chart timestamp-key + TZ-safe arrow placement, [W3-R4] honest null
  VIX-band copy on all three band sites ([W3-R3] arm repro verified dead);
  DISPLAY zone only; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  see §1 Active branch for the full blurb)
  Earlier: `claude/dazzling-pasteur-jf8s3w`
  (DEC-LAKE-STATS-MANIFEST-001, MIGRATION.md #1614 — the write-time
  PER-FILE lake STATS manifest; matches §1's Earlier chain)
  Earlier: `claude/sweep-log-level-guard`
  (DEC-SWEEP-LOG-VOLUME-GUARD-001, MIGRATION.md #1630 — the sweep CLI
  log-volume guard: INFO default + fail-loud `BRUTEX_LOG_LEVEL` override at the
  `main()` sweep dispatch AND inside every spawn worker
  (`sweep_parallel._worker_init`, R4 fold); audit JSONL hash chain untouched;
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/oneclick-sweep-runs-rebind`
  (DEC-ONECLICK-SWEEP-RUNS-REBIND-001, MIGRATION.md #1621 — the P4
  merge-integration rebind: oneclick discovery repointed to the Ruling-4
  `sweep_runs/` sibling root + the production `TradeStoreReader` over the
  #1275 results-depth sidecars; DISPLAY zone only; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX; see §1 Active branch for the full blurb)
  Earlier: `claude/peaceful-newton-fehcc3`
  (DEC-GREEKS-BULK-REENRICH-001, MIGRATION.md #1558 — the bulk Greeks
  v1→v2 re-enrichment backfill: expired contracts + CLI-only lakes
  re-stamped to the current formula/inputs; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/operator-page-real-rebuild`
  (DEC-OPERATOR-PAGE-REAL-001, MIGRATION.md #1561 — the Operator page (/)
  wired to the sweep-oneclick artifact contracts + NEW /screener + /strategy;
  DISPLAY zone only; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/sweep-stall-oom-guards`
  (DEC-SWEEP-STALL-OOM-GUARDS-001, MIGRATION.md #1608 — the parallel grid-sweep
  gains a heavy-grid-aware stall watchdog + a distinct `BrokenProcessPool`
  quarantine; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/intelligent-hamilton-qf3w38`
  (DEC-SWEEP-NIGHTLY-AUTOMATION-001, MIGRATION.md #1612 — the R9 nightly
  zero-click chain + R50 backup/restore + launchd scheduling; ENGINE
  UNCHANGED — NIFTY/BANKNIFTY/SENSEX; see §1 Active-branch chain for the
  full blurb)
  Earlier: `claude/elegant-johnson-cgufi7`
  (DEC-SWEEP-RESULTS-DEPTH-001, MIGRATION.md #1617 — renumbered
  #1538→#1557→#1581→#1598→#1610→#1617; the final hop past main's #1616
  (DEC-SWEEP-DATA-EDGE-GUARDS-001) on merge round 5, 2026-07-08)
  Earlier: `claude/data-edge-guards`
  (DEC-SWEEP-DATA-EDGE-GUARDS-001, MIGRATION.md #1616 — two surgical engine
  data-edge guards for sweep-result correctness: (1) a Muhurat-safe prev-day
  anchor at the Stage-1A roll seam + (2) a bounded bad-tick screen at the engine
  read boundary (`arrow_table_to_bars`); ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX;
  see §1 Active branch for the full blurb)
  Earlier: `claude/peaceful-hamilton-kyeu07`
  (DEC-VIX-STAMP-WIRE-001, MIGRATION.md #1645 — India-VIX trade stamping wired
  end-to-end: `vix_at_entry`/`vix_at_exit` on every OBSERVABLE trade + the
  persisted nullable `vix_mean`/`vix_min`/`vix_max` finalist trio, REFERENCE
  ONLY; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/jolly-tesla-miygwt`
  (DEC-MUTATION-RUNTIME-003, MIGRATION.md #1597 — the weekly deep mutation
  gate made green end-to-end: sharded cargo-mutants matrices + zero-survivor
  python mutmut loop; see §1 Active branch for the full blurb)
  Earlier: `claude/serene-euler-y5ra0z`
  (DEC-BACKUP-OFFBOX-001, MIGRATION.md #1596 — automated incremental off-box
  backup: restic → S3 Object-Lock / B2 immutable, NEW `brutex/backup/` package
  + `brutex backup {run,check,restore-drill,status}` CLI + the O(1) dead-man
  freshness check + the monthly byte-verified restore drill; ops tooling ONLY —
  ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX; see §1 Active-branch chain for
  the full blurb)
  Earlier: `claude/blissful-curie-gp94rk`
  (the 11-item POLISH PACK, MIGRATION.md #1582–#1592; see §1 Active-branch
  chain for the full blurb)
  Earlier: `claude/upbeat-tesla-mfrovq`
  (DEC-RATE-METER-LIFECYCLE-001, MIGRATION.md #1593 — the #1056 pull-rate
  CallMeter lifecycle + rate-widget truth pack; see §1 Active-branch chain for
  the full blurb)
  Earlier: `claude/sleepy-hawking-l87497`
  (DEC-FORM-WINNER-PERSIST-001, MIGRATION.md #1581 — the durable `&`-form
  winner map persisted via `schema_meta` + the `--ampersand-probe` doctor
  stage; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/peaceful-wright-r7l8l9`
  (DEC-FIB-BULLISH-RETRACE-001, MIGRATION.md #1560 — the bullish Fibonacci
  retracement fix: PDL-anchored `retrace_bull_{23,38,62,78}_paisa` kernel
  fields replace the wrong bearish alias; ENGINE UNCHANGED —
  NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/stoic-davinci-7c7xp0`
  (DEC-TRACK2-COSTS-SPEC-RECONCILE-001 + DEC-BROKER-DOCS-AWSONLY-RECONCILE-001 +
  DEC-GPU-GATE-RECONCILE-001 + DEC-CLOUD-AUTOFIX-RETIRE-001 +
  DEC-PHASE1-SIGNOFF-RECONCILE-001, MIGRATION.md #1552–#1556 — the doc/spec/ops
  debt reconciliation pack; ENGINE UNCHANGED — NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/zen-meitner-8780fo`
  (DEC-RUN-IDENTITY-GUARD-PARITY-001 + DEC-OPTION-DEDUP-BLOOM-VERIFY-PIN-001 +
  DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 + DEC-TRADE-SIM-U128-KERNEL-001 +
  DEC-EXECUTION-TRIO-DELETE-001, MIGRATION.md #1546–#1550)
  Earlier: `claude/ecstatic-cannon-o3zbbe` (DEC-TEST-APPARATUS-DEBTS-001,
  MIGRATION.md #1541 — the test-apparatus debt pack: nine long-ledgered
  apparatus gaps closed as ONE change set; apparatus/CI/docs only; see §1
  Active branch chain for the full blurb)
  Earlier: `claude/eloquent-shannon-bdjnuv` (carrying BOTH movers fixes)
  (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001, MIGRATION.md #1539 — the `/movers`
  prev-close/OI baselines gain an honest calendar-contiguity verdict: a
  baseline anchored on a non-adjacent NSE session is stamped
  (`prev_close_*`/`oi_baseline_*` EMIT-ON-FALSE keys), warned once (capped),
  and marked " *" in the UI — the served number itself unchanged; READ-ONLY
  dashboard, ENGINE UNCHANGED NIFTY/BANKNIFTY/SENSEX)
  Earlier (same branch):
  (DEC-MOVERS-SPOT-VALUE-TF-INVARIANT-001, MIGRATION.md #1538 — the `/movers`
  SPOT "Top Value" (turnover) is TIMEFRAME-INVARIANT: re-sourced from the RAW
  1-minute session frame (post as-of cut, PRE-resample) via the NEW
  `spot_session_value_paisa`, mirroring the already-TF-invariant
  futures/options session value; READ-ONLY dashboard ranking, ENGINE UNCHANGED
  NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/intelligent-euler-dwf2ab`
  (DEC-CLI-GREEKS-ENRICH-001, MIGRATION.md #1536 — the CLI `brutex fetch`
  pull path now runs the SAME fenced Greeks-enrichment pass the dashboard
  runs + the NEW `brutex enrich-greeks` bulk re-enrich; see §1 Active
  branch chain for the full blurb)
  Earlier: `claude/movers-asof-default-latest-minute`
  (DEC-MOVERS-ASOF-LATEST-DEFAULT-001, MIGRATION.md #1535 — the `/movers` Spot
  default as-of TIME resolves to the SAME latest available minute across EVERY
  timeframe, so 1m ↔ 2m ↔ 3m ↔ 3hour ↔ 1day always show the identical latest
  close; READ-ONLY dashboard, ENGINE UNCHANGED NIFTY/BANKNIFTY/SENSEX)
  Earlier: `claude/happy-allen-y29nc6` (DEC-BENCH-ABS-BASELINE-001, MIGRATION.md
  #1534), `claude/peaceful-planck-dnsjg9` (DEC-LEDGER-DELTA-O-PAGE-001,
  MIGRATION.md #1533) and `claude/amazing-darwin-hb4caw`
  (DEC-SWEEP-EXIT-INDEX-FASTPATH-001 + DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 /
  ADR-0017, MIGRATION.md #1531–#1532)
- **Never push to `main`** without explicit user instruction.
- **Commit style:** Conventional Commits.
  - `docs(track1): add 52-condition vocabulary appendix`
  - `feat(adapter/groww): implement intraday OHLC chunking`
  - `fix(engine): O(1) regression in bar lookup`
  - `test(property): add DSR effective-N invariant`
  - `chore(claude): update CLAUDE.md rules`
- **Never skip hooks** (`--no-verify` is forbidden unless the user
  explicitly requests).
- **Never force-push** shared branches.
- **Never amend** published commits; always create new commits.

---

## 4. Coding Standards (when code phase starts)

- **Language:** Python 3.12 primary; Rust (PyO3) for hot kernels; no
  other languages without ADR.
- **Formatters:** `ruff format` (not black); `ruff check --fix`.
- **Types:** `mypy --strict` AND `pyright` both must pass.
- **Linters:** `ruff`, `pylint` (warnings-as-errors), `bandit` (security).
- **Tests:** `pytest`, `hypothesis` (property), `pytest-benchmark`
  (SLA), `mutmut` (mutation), `cargo test`, `proptest`,
  `cargo-mutants`, `cargo fuzz`.
- **Coverage gates:** 100% line + 100% branch on `brutex/` package.
  Mutation score ≥ 85% (Python AND Rust).
- **Docstrings:** every public symbol. NumPy style.
- **No `print`** — always `structlog`.
- **No bare `except`** — specific exceptions only.
- **No global mutable state** — inject dependencies.
- **No `pandas` in hot paths** — use `polars` or `numpy` + `numba`
  or Rust.
- **No look-ahead** — at bar N, the engine may only access bars 0..N.
  Enforced TODAY by TWO complementary runtime guards: (1) the
  feed-boundary guard (`ReplayFeed.ensure_no_lookahead`, raises
  `LookAheadError`; wired into the strategy runner — the deprecated
  execution driver, the historical second wiring site, was DELETED with
  the simulator/driver trio per DEC-EXECUTION-TRIO-DELETE-001
  (MIGRATION.md #1550) — integration-tested) — a *timestamp*-based check
  on emitted signals; and
  (2) the named **Property P-06 (instrumented bar accessor)** — now LIVE
  per **DEC-LOOKAHEAD-BARRIER-001** (`brutex/feed/lookahead.py`
  `LookAheadBarrier`): an *index*-based accessor that raises `LookAheadError`
  the moment an `access(i)` reaches index `i > N`. Guard (1) is the real
  strategy-look-ahead defence — it catches a future *signal*. Guard (2), as
  wired into the runner event loop (a default `record=False`
  O(1)-memory barrier on every sweep eval), asserts the **advance/access
  cursor invariant**: both sites `advance_to(step)` then `guard(step, …)` at
  the SAME `step`, so it fires on a feed/barrier desync or a wiring regression
  — it does NOT interpose on the arbitrary bar reads a strategy makes off its
  own state, so it is NOT a second strategy-look-ahead guard at that site
  (DEC-LOOKAHEAD-HONESTY-001). The barrier's index-`> N` teeth are genuinely
  exercised by an `access` at a future index — done by the property tests and
  the manual `brutex verify-lookahead` recheck (which re-confirms forward feed
  ordering + the cursor invariant over a lake slice, not strategy consumption).
  So the two guards are **complementary** (a Phase-2 strengthening of the
  feed-boundary guard), NOT literally redundant coverage of the same failure at
  the same site, and NOT a perf path; the accessor is O(1) per bar access (NOT
  an O(1)-total claim). Promoted from the prior `pytest.skip` ROADMAP stub (was
  corrected per DEC-WORKSPACE-VERIFY-001 entry #398 D2 /
  DEC-DOC-DRIFT-RECONCILE-001). The brute-force **SWEEP path** (which the two
  runner-wired guards above never covered) now has its own three-layer
  defence (DEC-SWEEP-LOOKAHEAD-GUARD-001, MIGRATION.md #1181): structural
  causality (audit-verified — Stage-1A updates prior-bar history AFTER
  evaluation; the trade sim enters at the next bar's open) + PERMANENT
  suffix-independence property gates
  (`tests/property/test_sweep_suffix_independence.py`: Stage-1A bar_bits, the
  resample path, and full-spine trades over bars `0..N` are bit-identical
  whether bars `N+1..` are absent, mutated, or extreme — the layer that
  catches a leak through DERIVED state) + an OPT-IN instrumented barrier
  (`brutex.engine.sweep_lookahead`, env `BRUTEX_SWEEP_LOOKAHEAD_GUARD=1`,
  default OFF) wrapping the sweep's bar window in an index-guarded sequence
  that REUSES `LookAheadBarrier` — honest envelope: it catches an indexed /
  sliced / reversed read AHEAD of the forward consumption front (strongest on
  the first sequential pass), NOT derived-state leaks; OFF returns the
  identical bars object, so the hot path is byte-identical + cost-unchanged
  (identity contract + A/B bench + the existing sweep O(1) gates).
- **Money display = 2 decimals, money-only** (DEC-MONEY-2DP-001) — every
  paisa→rupee display goes through `brutex.money` (`format_inr` /
  `round_money` / `paisa_to_rupees`). NEVER round statistical metrics (Sharpe,
  DSR, p-values, CPCV) or `iv` — they keep full precision or strategy ranking
  breaks. Money amounts (costs + display) are int64 paisa; OHLC/spot/option
  **prices** are stored as Decimal rupees (Parquet Float64), NOT paisa — and
  **snapped to the 2-decimal tick grid at the lake-write boundary**
  (`round_money`, `ROUND_HALF_UP` — never truncation; spot mirrors options) so
  the lake never holds vendor float-noise like `14020.7999`
  (DEC-SPOT-PRICE-2DP-001). See §6 (DEC-PAISA-DOC-RECONCILE-001).

---

## 5. Directory Conventions

```
bruteX/
├── CLAUDE.md                       ← this file
├── README.md
├── .claude/
│   ├── settings.json
│   └── agents/
├── docs/
│   ├── TRACK1_BACKTEST_SPEC.md     ← Track 1 master spec
│   ├── TRACK1_VERIFICATION_PYRAMID.md
│   ├── TRACK1_VOCABULARY.md        ← 52 atomic conditions
│   ├── TRACK1_PHASE_PLAN.md        ← 8-12 week build plan
│   ├── TRACK1_PERFORMANCE_BUDGET.md
│   ├── TRACK1_MICROSTRUCTURE.md
│   ├── TRACK1_DASHBOARD_DESIGN.md  ← v29 locked design spec + data contract
│   ├── dashboard/mockups/          ← v28.html + v29.html (canonical mockups)
│   ├── MIGRATION.md                ← decision ledger
│   ├── ENGINEERING_REQUIREMENTS_CHARTER.md ← ultra-deep requirements (scope-tagged; aspirational, CLAUDE.md wins)
│   ├── REQUIREMENTS.md             ← RETIRED (stub)
│   ├── REQUIREMENTS_SIMPLE.md      ← RETIRED (stub)
│   ├── ROADMAP.md                  ← RETIRED (stub)
│   ├── adapters.md                 ← RETIRED (stub)
│   ├── adr/                        ← architecture decisions
│   └── runbooks/
├── brutex/                         ← Python package
│   ├── adapters/groww/             ← Groww Trade API adapter (growwapi SDK)
│   ├── calendars/                  ← NSE/BSE trading calendar
│   ├── config/
│   ├── costs/                      ← brokerage/tax cost models
│   ├── dashboard/                  ← FastAPI UI
│   ├── data/                       ← Parquet + DuckDB lake (generic)
│   ├── engine/                     ← brute-force combo/mask sweep engine
│   ├── execution/                  ← shared execution types + typed errors
│   │                                 for the lean instrument store (the
│   │                                 deprecated costs/simulator/driver trio
│   │                                 was DELETED — DEC-EXECUTION-TRIO-DELETE-001)
│   ├── feed/                       ← replay + resampler
│   ├── indicators/                 ← evaluate-time indicators (pivots/fib/EMA)
│   ├── math/                       ← thin facade to Rust hot kernel
│   │                                 (fno_math + trading_core re-export;
│   │                                 DEC-STRUCTURE-CLEANUP-001)
│   ├── metrics/                    ← Sharpe/DSR/SPA/CPCV statistics
│   ├── notifications/
│   ├── observability/
│   ├── options/                    ← expiry/strike/lot-size rules (lake)
│   ├── orchestrator/               ← FSM + audit + rate limiter
│   ├── ranking/                    ← composite ranking + rejection gates
│   ├── strategies/                 ← 52-vocabulary engine (Phase 3)
│   └── vocabulary/                 ← 52-condition evaluator (→ trading_core)
├── rust/                           ← Rust hot kernels (shared core + compat)
│   ├── fno-math/                   ← compat types + NSE calendar; Greeks/
│   │                                 indicators removed in DEC-P0-025
│   └── trading-core/               ← SHARED 52-vocab + evaluate-time
│                                     indicators (pivots/fib); bit-identical
│                                     core for BruteX (PyO3 wheel) + TickVault
│                                     (native) — DEC-SHARED-CORE-001 #441
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── property/                   ← hypothesis + proptest (24 invariants)
│   ├── golden/                     ← byte-equality fixtures
│   ├── bench/                      ← O(1) ratio tests
│   ├── stress/                     ← nightly ~5-min gate + weekly soak (#1580)
│   ├── smoke/                      ← <30s pre-push
│   └── fakes/                      ← test doubles
└── .github/workflows/
```

---

## 6. Verified Groww Facts (cite these, don't re-derive)

> **DEC-DHAN-GROWW-REMIGRATE-001 (2026-06-12, ADR-0009):** full
> re-migration of the Track-1 backtesting data source back to the
> **Groww Trade API** (`growwapi` Python SDK). This SUPERSEDES the
> ADR-0002 prior-vendor revert; the prior-vendor adapter package + its
> reference docs were removed in the ADR-0009 §5 cutover PR series. The
> facts below are repo-sourced: HARD-VERIFIED from
> `brutex/adapters/groww/constants.py` (and sibling modules) + the Groww
> SDK reference at `docs/groww/api-reference/`. See `docs/adr/` ADR-0009
> for the migration rationale + the accepted-risk register. Rows whose value is an OPEN Phase-0 probe
> gate are stamped **UNVERIFIED** (ADR-0009 §3.2 / §4) — never encoded as
> fact in code until a probe transcript lands. Every row traces to
> file:line.

| Fact | Value | Source |
|------|-------|--------|
| Vendor / transport | **Groww Trade API — `growwapi` Python SDK** (injected `GrowwAPI` client; no raw HTTP) | `adr/0009-...-migration.md:43-44`; `groww/adapter.py:1-17` |
| Historical OHLC (spot + options) | `groww.get_historical_candles(exchange, segment, groww_symbol, start_time, end_time, candle_interval)` — single method for all instruments | `groww/api-reference/03-backtesting.md:138-200`; `groww/adapter.py:151` (`fetch_ohlcv` → `fetch_candle_windows`) |
| F&O candle lake (`/db` logical table) | **`fno_candles`** — the FUTURES + OPTIONS historical OHLC the pull ALREADY writes to `bars/<exch>/FNO/<groww_symbol>/<interval>/<yyyy>/<mm>.parquet` (shared `ParquetBarSink`; OHLCV + `open_interest` first-class non-null; 2-dp tick grid). NOT a separate store — the SAME lake as spot with `segment="FNO"`; FORMALIZED as a first-class `/db` row (decoded per contract via `fno_symbol.parse_fno_symbol`) + registered in `schema_meta` (`fno_candle_lake.*`) — DEC-FNO-CANDLE-LAKE-001 #861 | `data/fno_candle_lake.py`; `data/fno_symbol.py`; `dashboard/db_view.py` (`list_fno_candle_datasets`); `adapters/groww/option_ingest.py` (`sink.append(segment="FNO", …)`) |
| Options contract resolution | `groww.get_expiries(...)` → `groww.get_contracts(...)` → feed each Groww symbol to `get_historical_candles` (all listed strikes, not relative-to-spot), **`1minute` ONLY** with a DEFAULT window SPLIT by contract type — the STRICT law `[expiry − lookback → expiry] ∩ envelope`: FUTURES `expiry − 60 days → expiry` (DEC-FUTURES-LOOKBACK-60D-001 #1003, supersedes the 3-calendar-month default of #894), OPTIONS `expiry − 30 days → expiry` (explicit operator dates override either; a NOT-YET-OPEN far-future contract is honestly SKIPPED — fetched not at all, zero ledger rows, until the calendar reaches `expiry − lookback` — never a substitute window, never the full range — DEC-FNO-FAR-EXPIRY-WINDOW-001 #1127; `1day` DERIVED from the pulled 1-minute; an operator `/pull-pipeline` date range additionally SCOPES which expiries are pulled at all — expiry ∈ `[from → to]` inclusive, out-of-scope = zero vendor calls + zero ledger rows + one `fno.expiry_out_of_scope` log, stored bars untouched, windows never reshaped — DEC-FNO-EXPIRY-SCOPE-001 #1146; the candle PULL ORDER is the strict six-step law — futures before options, indices before stocks within each: INDICES-FUTURES → STOCKS-FUTURES → INDICES-OPTIONS → STOCKS-OPTIONS, ledger namespaces byte-unchanged — DEC-PULL-SIX-STEP-ORDER-001 #1527) — DEC-1MINUTE-ONLY-DERIVE-1DAY-001 #853 + DEC-FNO-LOOKBACK-SPLIT-001 #894 | `groww/api-reference/03-backtesting.md:41-134`; `adapters/groww/contract_bounds.py` (`default_fno_minute_window`, `DEFAULT_OPTION_LOOKBACK_DAYS`, `DEFAULT_FUTURES_LOOKBACK_DAYS`); `adapters/groww/expiry_scope.py`; `dashboard/historical_pull.py` |
| Backtesting segments | `CASH`, `FNO` ONLY (`COMMODITY` excluded; indices ride the `CASH`/`IDX` row, mapped to the neutral `INDEX` segment) | `groww/constants.py:91-95`; `groww/instruments.py:154-169`; `groww/api-reference/03-backtesting.md:8` |
| Instrument types | `EQ, IDX, FUT, CE, PE` | `groww/constants.py:95`; `groww/api-reference/04-annexures.md:100-108` |
| Historical depth | **"available from 2020"** (Equities, Indices, FNO) | `groww/constants.py:106-108`; `groww/api-reference/03-backtesting.md:145` |
| 1-minute granularity at the 2020 boundary | **UNVERIFIED** — Phase-0 probe gate (rows may not truly start Jan-2020 at 1m) | `groww/constants.py:107`; `adr/0009-...-migration.md:113-114` |
| Candle intervals (SDK supports) | `1/2/3/5/10/15/30 minute`, `1hour`, `4hour`, `1day`, `1week`, `1month` | `groww/constants.py:57-70`; `groww/api-reference/04-annexures.md:85-98` |
| Candle interval — Track 1 spot | **`1minute` ONLY fetched** (DEC-1MINUTE-ONLY-DERIVE-1DAY-001 #853 — `resolve_fetch_intervals` always returns `("1minute",)`); `1day` (PDH/PDL anchor) + the 19 intraday TFs are DERIVED from the stored 1-minute, never separately vendor-fetched — since DEC-PIPELINE-AUTOPREBUILD-001 #1145 written ONLY by the pull pipeline's automatic FINAL prebuild stage (`materialize_lake_timeframes(include_daily=True)`; the midnight-IST `1day` via the shared `spot_daily_resample.make_lake_write_1day`; since DEC-PREBUILD-CLEANSET-001 #1221 run on every completed pull over the CLEAN symbol set — quarantined symbols excluded via `exclude_symbols`, not gated on a fully-clean fetch), NEVER mid-pull (`_resample_daily_after_pull` + the `BRUTEX_AUTO_DERIVE_TFS` autoderive + the manual `derive-timeframes` route are REMOVED). The granularity dropdown + the bhavcopy cross-check of DEC-PULL-1DAY-FIRST-001 (#723) are REMOVED | `dashboard/pull_controller.py` (`GROWW_FETCH_INTERVALS`, `resolve_fetch_intervals`); `dashboard/pipeline_prebuild.py`; `data/timeframe_materialize.py`; `data/spot_daily_resample.py` |
| Chunk window — by interval tier | **1/2/3/5m → 30d · 10/15/30m → 90d · 1h+/1d/1w/1mo → 180d** per call (richer than a single split) | `groww/constants.py:76-89`; `groww/chunks.py:34-60`; `groww/api-reference/03-backtesting.md:238-246` |
| Candle response shape | **ROW-arrays** — `{"candles": [[ts, open, high, low, close, volume, open_interest], ...]}`; OI `null` for non-FNO | `groww/parser.py:1-18,104-124`; `groww/api-reference/03-backtesting.md:202-236` |
| **Price unit** | **RUPEES (float)** from Groww — kept as exact `Decimal` rupees through the lake (Parquet `Float64`); int64 paisa is the cost/display unit only, NOT price storage (DEC-PAISA-DOC-RECONCILE-001) | `data/candle_fields.py` (`to_price`→`Decimal`); `data/models.py:26-35`; `groww/api-reference/03-backtesting.md:202` |
| Timestamp unit | **Native-IST string** `"yyyy-MM-ddTHH:mm:ss"` (or epoch seconds defensively) — localized to IST (fixed +05:30, no DST), stored UTC, displayed IST | `groww/parser.py:33-67`; `groww/constants.py:109-110`; `groww/api-reference/03-backtesting.md:198-207` |
| Symbol/key format | **`groww_symbol` string** `EXCH-SYMBOL[-…]` (e.g. `NSE-NIFTY`, `BSE-SENSEX`) — O(1) resolution: a BARE trading symbol (incl. a hyphenated stock like `BAJAJ-AUTO` / a verbose-symbol index like `MIDCPNIFTY`) resolves via `by_exchange_symbol` + the master's `groww_symbol_by_pair` (the real groww_symbol, never reconstructed); an already-prefixed FUT/CE/PE contract resolves via `by_groww_symbol`. NOT a numeric ID (DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001, MIGRATION.md entry #845) | `groww/instruments.py:240-243,524-531`; `groww/adapter.py:162-174`; `groww/api-reference/02-instruments.md:119`, `03-backtesting.md:154-156` |
| Options instrument type | **`CE` / `PE`** (Call / Put); `FUT` for futures | `groww/constants.py:95`; `groww/api-reference/03-backtesting.md:16-22`; `04-annexures.md:107-108` |
| Exchanges (in scope) | **NSE, BSE** (MCX excluded with the COMMODITY segment) | `groww/constants.py:92`; `groww/api-reference/04-annexures.md:39-43` |
| Rate limit — backtesting | **500 req/min, NO daily quota** (operator-confirmed 2026-06-13, NOT vendor-documented; DEC-GROWW-RATE-PERMIN-001). The per-SECOND cap stays **UNVERIFIED** (`get_historical_candles` r/s UNDOCUMENTED; Phase-0 probe gate); the production per-second ceiling injected at both live build sites is the operator-DIRECTED **8 r/s** (`GROWW_RATE_LIMIT_RPS_CEILING=8.0`, operator-approved 2026-06-24, RAISED 5 → 8, DEC-GROWW-RATE-CEILING-8-002 supersedes DEC-GROWW-RATE-CEILING-5-001 — 8×60 = 480/min stays under the 500/min governor so the HARD cap can't be overshot; raised back to 8 once DEC-FETCH-WRITE-OVERLAP-001 Lever 2 (pool 2→4) gave enough connections to generate demand toward the ceiling; a CEILING raise only ALLOWS a higher rate, the AIMD ladder still steps DOWN on 429s and recovers only as far as the vendor tolerates; SUSTAINED 8 is UNVERIFIED — the log shows the vendor 429ing at 5 — revert to 5 is one constant; NOT a vendor-verified limit). The published "Live Data" group is 10 r/s · 300/min but is NOT confirmed to cover backtesting | `groww/constants.py:141-143,154,177` (`GROWW_RATE_LIMIT_RPS_UNVERIFIED=10.0`, `GROWW_RATE_LIMIT_PER_MIN=500`, `GROWW_RATE_LIMIT_RPS_CEILING=8.0`, `GROWW_DAILY_QUOTA_UNVERIFIED=None`); `groww/api-reference/01-introduction.md:130-134` |
| Adapter effective cap | **Vendor-neutral adaptive token bucket** — injected `ceiling_rps` (the production live ceiling is the operator-DIRECTED **8 r/s**, `GROWW_RATE_LIMIT_RPS_CEILING`, DEC-GROWW-RATE-CEILING-8-002; the vendor-neutral DEFAULT stays 5), steps down to a 1 r/s floor on 429/timeout and recovers to the ceiling once clear — PLUS an injected `ceiling_per_min` HARD 500/min rolling-window governor on top (DEC-GROWW-RATE-PERMIN-001) | `adapters/rate_common.py:372-426`; `groww/constants.py:177`; `dashboard/pull_controller.py:984`; `cli.py:4955-4956` + this file §2 rule 6 |
| Auth | **TOTP flow** — `GrowwAPI.get_access_token(api_key, totp=pyotp.TOTP(secret).now())`; creds are **AWS-SSM-only** (DEC-AWS-ONLY-CREDS-001, MIGRATION.md #1065): the api-key + TOTP secret + the shared daily access token are read fail-fast from AWS Parameter Store SecureStrings (`/tickvault/prod/groww/{api-key,totp-secret,access-token}`, region `ap-south-1`; non-secret path/region overrides `BRUTEX_TOKEN_PARAM_*` / `BRUTEX_TOKEN_AWS_REGION`) — NEVER env vars, NEVER a credentials file, NEVER a webpage (all three legacy paths REMOVED). The API-key+secret flow is REFUSED (daily manual approval violates §11 AUTOMATED). The daily access token (resets 06:00 IST) **self-heals mid-pull** on an auth-expiry — thread-safe, single-flight, bounded `max_reauths` (the #909 reauth seam) — by **RE-READING the Lambda-minted token from SSM and adopting it** (logs `groww.token.reauth` then `groww.token.reread`): production always resolves the READ-ONLY `ParameterStoreTokenStore`, so `SdkRefresher.reauth` takes the `readonly_reread` branch and the BruteX-side mint is deliberately UNREACHABLE — **BruteX NEVER mints on the read-only broker** (a stray local mint could invalidate TickVault's shared token; DEC-BROKER-READONLY-REAUTH-REREAD-001 #1072 + DEC-REAUTH-COLDSTART-READONLY-001 #1092, mechanism-superseding the local-mint reauth of DEC-GROWW-TOKEN-REAUTH-001 #909, which survives only for explicitly non-read-only local-dev configs). A persistently-stale token is remediated by re-deploying the Lambda minter (`deploy/aws_token_minter/deploy.sh`), never by a BruteX-side mint | `config/credentials_aws.py` (`load_groww_aws_credentials`); `groww/token_store_aws.py`; `groww/constants.py:487-509`; `groww/reauth.py` (`SdkRefresher`); `groww/client.py` (`_call` reauth branch); `groww/api-reference/01-introduction.md:34-96` |
| Instrument master URL | `https://growwapi-assets.groww.in/instruments/instrument.csv` (single CSV; SSRF-allowlisted host) | `groww/constants.py:24-26`; `groww/api-reference/02-instruments.md:8` |
| Instrument master schema | **21 columns** in fixed order; keys `exchange, exchange_token, trading_symbol, groww_symbol, name, instrument_type, segment, series, isin, underlying_symbol, …, lot_size, tick_size, freeze_quantity` | `groww/constants.py:28-53`; `groww/api-reference/02-instruments.md:112-134` |
| Exception hierarchy | `GrowwError, GrowwBadRequestError, GrowwAuthError, GrowwNotFoundError, GrowwRateLimitError, GrowwTimeoutError, GrowwFeedError, GrowwEmptyResponseError` + `is_retryable()` (maps the SDK's `GrowwBaseException`/`GrowwAPIException` tree by class name; an empty/non-JSON body `json.JSONDecodeError` → **retryable** `GrowwEmptyResponseError`, so a transient throttle is re-pulled instead of a terminal "not on Groww" quarantine — DEC-POSTPULL-DATAQUALITY-BATCH-001 #854) | `groww/errors.py`; `groww/api-reference/05-exceptions.md:9-70` |
| F&O-eligible stock detection | EQUITY rows whose `underlying_symbol` appears in the FNO contract set (`build_fno_eligible_underlyings`); **212** single stocks verified in the operator instrument master (`EXPECTED_FNO_STOCK_COUNT` — lake scope) | `groww/constants.py:104-105`; `brutex/universe.py:595-608` |
| NIFTY Total Market lake admission | **REFERENCE_ONLY** — ~750 NSE constituents (large+mid+small+micro) fetched best-effort from niftyindices, matched to Groww NSE cash-equity rows by ISIN / ticker; the ~540 non-F&O matches are admitted reference-only (the ~212 F&O names stay tradable). The fetch is **cache-on-success + bounded-retry + cache-fallback** (live → last-good cache → honest F&O-base degrade), so a transient niftyindices 404 falls back to the cached ~752 instead of collapsing the universe to the ~211 F&O base (this cache/retry predates the batch; regression-pinned in DEC-POSTPULL-DATAQUALITY-BATCH-001 #854). **BRUTE-FORCE engine UNCHANGED** — lake/reference only, never in the 52-vocab | `dashboard/instrument_loader.py` `_ntm_admission` (DEC-NTM-GROWW-REPOINT-001); `universe_constituents.py` `resolve_constituents` |
| Track 1 brute-force universe | **`NSE-NIFTY`, `NSE-BANKNIFTY`, `BSE-SENSEX`** (all `IDX`, mapped to the neutral `INDEX` segment); resolved master-authoritatively via `by_exchange_symbol` + `groww_symbol_by_pair` (the bare trading symbol → the real groww_symbol, DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001). (LAKE option-pullability for F&O indices/stocks keys on a vendor-canonical `underlying_security_id` token-join (numeric `feed_token`) + a sourced NSE-index alias `NIFTYJR`→`NIFTYNXT50` / `NIFTYMIDSELECT`→`MIDCPNIFTY` (env-extensible `BRUTEX_FNO_INDEX_ALIASES`), with a `universe.load.opt_reclassified` audit — DEC-POSTPULL-DATAQUALITY-BATCH-001 #854; the ENGINE still the 3 indexes) | `groww/constants.py:97-100`; `groww/instruments.py:241-243`; `groww/adapter.py:162-174` |
| NIFTY-futures ENGINE series alias | **`NSE-NIFTY-FUT`** (symbol_id 4) — the ONE pinned ADR-0012 futures-track addressing label (DEC-FUTURES-ADDRESSING-PIN-001), deliberately NOT a lake dir: resolved at launch by `resolve_futures_series` from the vendor-parsed `bars/NSE/FNO/NSE-NIFTY-<DDMmmYY>-FUT` contract dirs into disjoint IST-date ownership windows (expiry day owned by the EXPIRING contract, the `resolve_near_month` `>=` law) + the `roll_schedule` of owning-expiry ordinals the engine hard-resets on; interior contract holes REFUSE loudly naming the missing month(s), head/tail unowned spans clamp+WARN, the expiry calendar is an OBSERVE-only cross-check (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001). `parse_fno_symbol("NSE-NIFTY-FUT")` is `None`, so no vendor contract symbol can collide with the alias | `brutex/universe.py` (`NIFTY_FUTURES_SERIES_SYMBOL`); `brutex/data/futures_series.py`; `brutex/feed/series_load.py`; `brutex/strategies/spec.py` (`_SYMBOL_ID_TABLE`) |
| INDIA VIX symbol | **`NSE-INDIAVIX`** — reference-only (`buy/sell_allowed=0`), never in the 52-vocab | `groww/constants.py:101-102` |
| INDIA VIX historical-candle availability | **UNVERIFIED** — Phase-0 HARD-GATE; if absent on Groww the migration is rescinded before cutover | `adr/0009-...-migration.md:90,111-112` |
| Token lifetime + backtest-vs-live drift | **Token lifetime VERIFIED (production logs 2026-06-23):** the daily access token resets at **06:00 IST** — handled UNATTENDED by the mid-pull reauth self-heal (on the production read-only broker a RE-READ of the Lambda-minted SSM token, `groww.token.reread` — DEC-BROKER-READONLY-REAUTH-REREAD-001 #1072 over the #909 seam), so the Phase-0 "unattended TOTP token lifetime" gate is CLOSED. The prior-vendor↔Groww 1m bar drift stays **UNVERIFIED** (Phase-0 probe gate) | `adr/0009-...-migration.md:88-92,113-124`; `groww/reauth.py` |

## 7. Verified Indian Market Microstructure Facts

| Fact | Value |
|------|-------|
| Trading hours | 09:15 - 15:30 IST (NSE + BSE equity) |
| Pre-open auction | 09:00 - 09:15 IST (exclude from backtest bars) |
| Spot index ticks from | 09:15:00 IST (post-auction opening) |
| Track 1 forced exit | 15:20 IST |
| Muhurat session (Diwali) | 1 hour per year; date list in `TRACK1_MICROSTRUCTURE.md` |
| NIFTY 50 rebalance | Semi-annual (March, September) — ~10 events in 2021-2026 |
| Stamp duty (equity intraday) | 0.003% buy side (signal-only mode: NOT applied) |
| Brokerage assumption (Track 1) | **NONE** — spot is not tradable |

---

## 8. Hardware Envelope (for honest SLA claims)

**Track 1 reference:** Apple M4 Pro, 14-core CPU (10P + 4E), 48 GB RAM,
8 TB external SSD. Daily apps run concurrently (10 cores used by sweep,
2 reserved).

This envelope is now **auto-detected at RUNTIME**, never hardcoded — the ONE
COMMON config leaf `brutex/config/hardware_envelope.py`
(DEC-HARDWARE-ENVELOPE-001 #1401 + DEC-RAM-BUDGETS-001 #1360, consolidated by
DEC-HW-ENVELOPE-CONSOLIDATE-001 #1490) probes cores (macOS `sysctl` P/E
split, Linux `sched_getaffinity` cgroup/CI-aware, fallback `os.cpu_count()`)
AND total physical RAM (sysconf-first, the honest `0` "unknown" sentinel) and
sizes every CPU-bound stage off it (`cpu_pool_workers(reserve=2,
prefer_performance=True)` → 8 on this machine; `memory_budget_bytes` for the
RAM-scaled byte budgets). The engine sweep pool (`auto_sweep_workers`), the
Greeks enrichment pool (`greeks_pool_workers`), and the pull pipeline's
timeframe PREBUILD (parallelised across cores via a `ThreadPoolExecutor`,
byte-identical lake output) all size off it; the polars global pool (pinned to FULL
physical width at the CLI entrypoint) + every concurrent-reader DuckDB
connection (`physical_cores // 2` per connection — the contention fix) are
bounded off it too (DEC-THREAD-BUDGET-BOUNDS-001 #1491, kill switches
`BRUTEX_POLARS_THREADS`/`BRUTEX_DUCKDB_THREADS`).
`BRUTEX_CPU_WORKERS` / `BRUTEX_PREBUILD_WORKERS` / `BRUTEX_SWEEP_WORKERS=1`
override. The Groww FETCH pool + the per-second rate ceiling + the 500/min
governor are a REAL vendor limit (a single TOTP token; §2 Rule 6) and are
deliberately NOT hardware-scaled.

**Production sweep SLA:** **2-4 days wall time** for ~33 billion trials
on this envelope, after Stage 1A → 1B → 2 → 3 → 4 pipeline. Public
benchmarks (vectorbt, NautilusTrader, Jesse) constrain the upper bound;
see `docs/TRACK1_PERFORMANCE_BUDGET.md` for the full memory-bandwidth
analysis.

Larger machines (64-128 core EPYC / Threadripper) scale linearly and
bring sweep to 12-24 hours; the spec MUST state SLA per hardware
envelope.

---

## 9. When in Doubt

1. Re-read `docs/TRACK1_BACKTEST_SPEC.md`.
2. If answer not there → ask the user with `AskUserQuestion`.
3. Never guess on scope. Never expand scope silently.
4. Never propose "improvements" outside the requested task.
5. Before major design choices, spawn a 4-agent hostile review (see
   `docs/TRACK1_PHASE_PLAN.md §0` for the pattern).

---

## 10. Session Etiquette

- Start of every session: read this file + `TRACK1_BACKTEST_SPEC.md` +
  open the locked-decisions log in `docs/MIGRATION.md`.
- Use TodoWrite for any multi-step task.
- Split long writes into multiple Write tool calls (avoid stream idle
  timeouts on docs >300 lines).
- Report honestly on failures; do not paper over errors.
- After any major refactor: run `ruff check brutex && mypy --strict
  brutex && pytest -q` before considering the change complete.
