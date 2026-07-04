# Dashboard frontend (JS) performance audit — DEC-DASHBOARD-JS-PERF-001

> **Status:** Shipped 2026-07-04 (branch `claude/happy-goldberg-qqqgf6`).
> Full-surface read-only audit of every dashboard template's inline JS +
> the `static/` widgets, followed by the scoped fix set (a)–(d) below.
> Audited at pre-fix HEAD `aa034f2` — **all `file:line` evidence in the
> findings tables references that tree** (the fixes move lines).

---

## What shipped in this PR (scope a–d + security pin e)

All fixes are **CLIENT-side JS only**, inside the templates' own inline
`<script>` blocks. **Backend payloads are FROZEN** — no route, payload
field, or querystring changed; the backend `generation`/etag echo is a
REPORTED follow-up, not part of this PR.

| # | scope | pages | mechanism |
|---|---|---|---|
| a | skip-render-when-unchanged on the 4 s pollers | `coverage.html`, `fetch_windows.html`, `expired_options.html`, `spot_coverage.html` | each tick reads the response body as TEXT once and compares it against the text last actually rendered; on an exact match the tick skips `JSON.parse` + all DOM writes EXCEPT the `liveTxt` heartbeat (stays honest every tick). Every error path (`!r.ok`, fetch/parse throw) resets the memo so the next good body always renders. Chip clicks null the memo (manual refresh always renders). |
| b | debounced filters | `v29.html` (+ `movers.html` number inputs under d) | the 5 combos-filter controls bind ONCE (`'input'`, was a `'change'`+`'input'` double-bind) through a shared ~200 ms trailing debounce; `#comboMeta` innerHTML template → build-once `createElement`+`textContent` count patch; reality-banner innerHTML gated behind a state key (assign only when the branch/text actually changes); both 2 s pollers (`[data-bind]`/`[data-sweep]`) get cached NodeLists + a stringify-compare skip gate. |
| c | in-place tbody grids + delegated listeners | `db.html` | `patchGridRows` swaps ONLY `tbody.innerHTML` (the SAME esc()'d `gridRowsHtml` strings the full rebuild uses — narrower target) when the live table's `data-cols` fingerprint matches; pager disabled-state + range patched via `textContent`; `.gridwrap` survives so horizontal scroll is preserved; any shape change falls back to the old full rebuild. ONE delegated pager listener (`pagerState`/`setGridPager`; the four per-render `wire*Pager` helpers deleted) + ONE delegated sidebar listener. Limit-selector table-mode branch now takes the gridOnly path like its 3 siblings. |
| d | movers skip/in-place + debounce | `movers.html` | `lastRenderSig` client-side skip (full query params + raw body text; gated on `state.sort.idx === null` so a client-sorted view never skips); gated-shell paint-once (`wasGated`/`lastGatedKey` keyed by asset+note — the 5 s auto-retry + `textContent` note discipline of DEC-PIPELINE-AUTOPREBUILD-001 byte-preserved); in-place `updateHeadSort` (sort click no longer rebuilds the thead); delegated `onPillsClick`/`onHeadClick` (DEC-MOVERS-SORT-RESET-001 semantics identical); ~200 ms debounce on the `#topn`/`#minvol` number inputs. |
| e | security pin | all 7 touched templates | `tests/integration/test_dashboard_js_perf_frontend.py` bounds each template's count of `innerHTML` ASSIGNMENT sinks by the count measured at the pre-fix baseline `aa034f2` — the ONE sanctioned delta is `db.html`'s `tbody.innerHTML = gridRowsHtml(...)` (same esc()'d builder strings, narrower target). No NEW innerHTML sink for vendor/user-derived strings. |

**The ONE declared behavior exception (otherwise values/pixels
unchanged):** `v29.html`'s combos filter read the WRONG columns
(`children[2/3/4/8/9]` — Charges' negative `data-sort` compared as Min
Smoothness meant ANY filter interaction hid EVERY row). The indices are
repointed to `[3/4/5/10/11]` per the real thead order — a bundled latent-BUG
correction, declared here and in the ledger entry, pinned both directions
(indices AND thead order) by
`test_v29_filter_column_indices_match_the_combos_thead`. Impact envelope:
v29.html is a design mockup over 20 static sample rows; nothing else on the
page and no backend/value surface changes.

## Honesty envelope

- **This repo has NO JavaScript coverage harness.** The inline `<script>`
  behavior cannot be exercised or covered by the repo's coverage gates. The
  JS behavior is pinned via (1) the existing route/template integration
  suites that render these templates and (2) TEMPLATE-SOURCE
  (DOM-structure / template-string) assertions in
  `tests/integration/test_dashboard_js_perf_frontend.py`, which fail on the
  pre-fix templates and pass after. **No JS coverage is claimed.**
- The skip-render guards are strictly opt-out-of-work: on every payload
  CHANGE the pages pay the same full render as before, by design. The
  steady state (byte-identical body per tick — the dominant production case,
  since the backend caches serve byte-identical JSON per write-generation)
  is where the win lives.
- The LAB evidence below is a **synthetic-payload measurement, not
  production traffic** (method + caveats stated with the table).
- The guard helper is deliberately duplicated per poller page — these are
  self-contained templates with no shared JS module; a tiny identical
  pattern per page was the sanctioned trade-off (noted, not hidden).

---

## Findings — Fixed in this PR (owned pages)

Owned pages: `db.html`, `movers.html`, `v29.html`, `coverage.html`,
`fetch_windows.html`, `expired_options.html`, `spot_coverage.html`, plus
the `static/` widgets (`bx-rate-monitor.js`, `bx-liveproof.js`,
`bx-datepicker.js` — findings recorded, ALL static-JS fixes deferred by
name; no static file edited). Rows whose *fix* column says **DEFERRED**
are named follow-ups on owned pages, kept out of this PR's scope
deliberately; everything else in this table shipped.

| page | finding | evidence (file:line @ aa034f2) | cost class | fix | risk |
|---|---|---|---|---|---|
| db.html | Grid-only updates rebuild the ENTIRE #dbgrid (thead + tbody + pager) as one innerHTML string on every paging/sort/date click; .gridwrap scroll container re-created so horizontal scroll resets. All four view families: updateCandleGrid/updateTableGrid/updateRegistryGrid/updateLedgerGrid. | db.html:1327 (also :897, :671, :577; shared gridAndPager :731-769) | unkeyed-row-rebuild | FIXED (c): gridAndPager split into pure builders (byte-parity proven); `patchGridRows` sets only tbody.innerHTML + patches #dbprev/#dbnext.disabled + .prange via textContent when the live table's `data-cols` matches; full-rebuild fallback otherwise. | low — output HTML unchanged; fallback keeps old behaviour on any column-set change |
| db.html | Pager click listeners re-attached on every grid render (4 duplicated wire*Pager helpers; a missed re-wire silently yields a dead pager). | db.html:1273-1274, :870-871, :658-659, :564-565 | listener-churn | FIXED (c): ONE delegated click listener on dataEl at boot (mirrors the proven delegated copy handler :1352) + `pagerState`/`setGridPager`; the four wire*Pager helpers deleted. | low — same pattern already proven on the same container |
| db.html | Limit selector's table-mode branch does a FULL view teardown (selectTable → renderTableView) where the other three modes pass gridOnly=true. | db.html:1338 (contrast :1335-1337) | full-DOM-rebuild-on-control-change | FIXED (c): `else if (selected) { dbOffset = 0; runTableQuery(selected, true); }` — mirrors the three adjacent branches. | low |
| db.html | No skip-render-when-unchanged anywhere: Refresh always rebuilds the whole sidebar, replays countUp per table, re-attaches 4×N row listeners, re-paints the active view even when the payload is byte-identical; no /api/v1/db payload echoes the server's generation. | db.html:1340-1346, :487-503; app.py:5490 (generation server-side only) | missing-skip-render-when-unchanged | PARTIAL: sidebar listener churn fixed (delegation, below); the per-view payload-compare repaint skip + sidebar skip-rebuild are DEFERRED (named); backend generation echo = DEFERRED (backend frozen). | low — comparison is strictly opt-out-of-work |
| db.html | loadTables rebuilds the entire sidebar list + re-attaches one click listener per row via four querySelectorAll loops on every call (boot + every Refresh). | db.html:487-503 | listener-churn | FIXED (c): ONE delegated click listener on tablesEl branching on data-table/data-candles/data-ledger/data-registry; the four per-row loops deleted (countUp badge loop stays). | low |
| db.html | Every F&O cascade level change (underlying/expiry pick) triggers full renderCandleView teardown of head + control bar + grid, destroying the From/To date inputs and firing the bx-datepicker MutationObserver re-scan per pick. | db.html:990 / :1000 / :978 → :1283; bx-datepicker.js:184 | full-DOM-rebuild-on-control-change | DEFERRED — dependent-select invariants by hand (audit MED risk). | med |
| db.html | Accidental O(exps × counts) quadratic: fnoExpiryCountsText does a linear .find over cstate.fno.counts for EVERY expiry option rendered (300+ expiries for a NIFTY 2020→2026 chain). | db.html:1139 (called from map at :1169) | O(rows^2) | DEFERRED — Map-based lookup swap (easy rider, outside scope). | low |
| movers.html | Gated-mode auto-retry does a full thead+tbody innerHTML rebuild every 5 s even when the gated payload is byte-identical, just to redisplay an unchanged "prebuild in progress" shell. | movers.html:383 (j.gated branch), :387 (GATE_RETRY_MS=5000 :231) | full-DOM-rebuild-on-poll | FIXED (d): `wasGated`/`lastGatedKey` — the gated shell paints once per (asset, note) key; later identical retries refresh only the #asof/#noteText textContent lines; reset on every non-gated load + in the catch. | low — the gated shell is static by construction |
| movers.html | Missing skip-render-when-unchanged on every load(): unconditional renderHead+renderRows even when the payload is identical (server memoises per generation); payload exposes NO generation field although the route has it in hand. | movers.html:372-395; app.py:4360; movers_snapshot.py:467-479 | missing-skip-render-when-unchanged | FIXED (d), client-side only: `lastRenderSig` = queryParams + "\|" + raw body text; skip when unchanged AND `state.sort.idx === null`; nulled on gated/failed loads + the catch. Backend generation echo = DEFERRED. | low |
| movers.html | Unkeyed whole-table rebuild: renderRows re-serializes the ENTIRE tbody (≤500 rows × ≤11 cols) per sort click / pill switch / load; a sort click ALSO rebuilds the whole thead just to move the ▲/▼ arrow. | movers.html:317, :299, :292 | unkeyed-row-rebuild | FIXED (d): tbody innerHTML swap KEPT (audit-sanctioned at ≤500 rows, preserves esc() discipline); thead updated in place on sort (`updateHeadSort`: classList.toggle + th.textContent from the static ASSETS config); full renderHead only on asset-tab switch + load renders. | low |
| movers.html | Listener churn: renderPills reattaches every pill onclick per call and RECURSES into itself per pill click; renderHead reattaches every th.onclick per render. | movers.html:274, :280, :296 | listener-churn | FIXED (d): one delegated click listener each on #pills and #head at init (`onPillsClick`/`onHeadClick`); pill click toggles .on in place, no recursion; DEC-MOVERS-SORT-RESET-001 semantics identical. | low |
| movers.html | No debounce + no stale-response guard on the 8 controls firing load(): rapid #topn/#minvol spinner clicks or fast tab/pill toggling launch overlapping fetches that can resolve OUT OF ORDER. | movers.html:411, :372, :391 | missing-debounce + stale-response-race | PARTIAL (d): ~200 ms trailing debounce on #topn/#minvol (`DEBOUNCE_MS`/`debouncedLoad`). The monotonic loadSeq stale-response guard is DEFERRED (named). | low |
| movers.html | Repeated regex parsing: sort comparator calls numify() TWICE per comparison inside Array.sort (~9,000 regex parses at n=500). | movers.html:310, :330, :302 | comparator-regex-reparse | DEFERRED — Schwartzian transform (identical ordering). | low |
| movers.html | POSITIVE (no fix): no steady-state setInterval poller (only the bounded gated retry); no text search box; no forced synchronous layout reads. | movers.html:231+387, :404-412 | none (absence-of-smell) | No action; do not add an auto-refresh poller without the skip-render guard. | informational |
| v29.html | pollState (2 s /api/v1/state poller) has no skip-render: every tick re-runs document.querySelectorAll('[data-bind]') over the WHOLE document and rewrites ~13 textContent nodes even when byte-identical. | v29.html:3946-3951, :3963; app.py:438-439 (snapshot_at) | missing-skip-render-on-poll + full-document-querySelectorAll-per-tick | FIXED (b): `[data-bind]` NodeList cached once; `JSON.stringify(funnel)` compared against the last-RENDERED payload — identical ⇒ zero DOM writes. Client-side only (backend frozen; the stringify compare subsumes the on-wire snapshot_at). | low |
| v29.html | pollSweepProgress (2 s poller) likewise rewrites every [data-sweep] textContent per tick with no unchanged gate + full-document query. | v29.html:3994-4001, :4006; sweep_progress.py:165 (updated_at) | missing-skip-render-on-poll | FIXED (b): same pattern (`sweepBoundEls` + `lastSweepPayload`); the #sweepProgressPanel un-hide stays BEFORE the gate. | low |
| v29.html | v27ReelAutoAdvance installs a 100 ms setInterval at page load running for the ENTIRE page lifetime; while reel mode is open, each 4 s advance scans all 9 slides' getBoundingClientRect. | v29.html:3756, :3733-3737, :3745-3749 | always-on-timer + periodic-forced-layout-read | DEFERRED — start/stop on reel-mode enter/exit. | low but outside scope |
| v29.html | Combos filter controls double-bound AND undebounced: 5 controls get BOTH 'change' and 'input' listeners calling applyFilters — select changes run the full row filter twice; each keystroke runs the per-row loop + a #comboMeta innerHTML rewrite. | v29.html:3518-3521, :3495-3516 | undebounced-input-filter + duplicate-listener-fire | FIXED (b): bind 'input' only through a ~200 ms trailing debounce (`applyFiltersDebounced`); #comboMeta becomes a build-once structure with a textContent count patch; Reset stays synchronous. | low |
| v29.html | The 5 requirement sliders re-run updatePassCount per 'input' tick of a drag and unconditionally reassign the reality-banner innerHTML (~600-char string) per tick even when branch + text are unchanged. | v29.html:3588-3598, :3573-3585 | undebounced-input + repeated-innerHTML-reparse | FIXED (b): banner state key (zero / high:win:count / normal) in a closure var; innerHTML assigned only when the key differs; per-tick lbl.textContent stays live. | low |
| v29.html | CORRECTNESS bug in applyFilters: reads the WRONG columns (children[2]=Strategy as Instrument, [3]=Inst as TF, [4]=TF as Dir, [8]=CHARGES as Min Smoothness — any interaction hid EVERY row — [9]=Net P&L as Min Real Edge). | v29.html:3503-3507 vs header order :3033-3046 | correctness (wrong-column-filter) | FIXED (b, declared exception): repointed to children[3/4/5] + [10]/[11].dataset.sort; pinned both directions by `test_v29_filter_column_indices_match_the_combos_thead`. | low — behavior becomes what the UI already claims |
| v29.html | Column-sort comparator does DOM reads + regex inside the comparator: O(n log n) dataset/textContent reads per sort click. | v29.html:3485-3490 | O(n log n)-DOM-reads-in-comparator | DEFERRED — decorate-sort-undecorate. | low |
| v29.html | Confetti churn: fireConfetti mints 80 styled divs per call; reel observer re-fires on EVERY s7/s9 re-entry; auto-advance wraps forever; cleanup wipes newer overlapping bursts. | v29.html:3416-3417, :3427-3439, :3749 | periodic-node-churn | DEFERRED — dataset.fired guard + per-burst piece tracking. | low |
| v29.html | Per-row click listeners instead of delegation: every #combosBody row + 6 podium buttons get individual listeners — churn-RISK once wired to live sweep data (no re-attach occurs today). | v29.html:3528-3532, :3640-3650 | listener-churn-risk | DEFERRED — becomes mandatory when live data lands. | low |
| v29.html | Poll over-fetch: pollState downloads the FULL DashboardState JSON every 2 s but consumes only 13 scalar sweep_funnel fields. | v29.html:3938; app.py:755-800 | oversized-poll-payload | DEFERRED — needs a lean backend route or ETag (backend frozen); frontend mitigation is the skip gate (shipped). | med — cross-file backend change |
| v29.html | Gold-podium tilt calls getBoundingClientRect on EVERY mousemove over the card. | v29.html:3698-3706 | layout-read-per-mousemove | DEFERRED — cache rect on mouseenter / rAF coalesce. | low |
| coverage.html | Massive payload over-fetch per 4 s tick: fetches /api/v1/coverage/windows with NO params → route default limit=20,000 rows shipped every 4 s while the hub reads ONLY b.count (limit-invariant in BOTH backend paths). | coverage.html:202; app.py:4113; db_view.py:3569/3699 | payload-over-fetch-on-poll | DEFERRED — `?limit=1` is a request-shape change (outside this PR's zero-request-shape-change envelope; flagged trivially-safe rider). | low |
| coverage.html | Remaining 3 polls also over-fetch: full rows arrays read for .headline only; full scoreboard for 4 aggregate numbers. | coverage.html:183, :193, :211; coverage_view.py:279-334, :361-381 | payload-over-fetch-on-poll | DEFERRED — needs backend ?headline=1 / ?summary=1 (backend frozen). | med |
| coverage.html | setInterval(poll, 4000) with no overlap guard + no visibility gating: slow endpoints stack concurrent requests; hidden tabs keep polling forever. | coverage.html:227, :180 | poll-overlap-no-visibility-gate | DEFERRED — self-rescheduling setTimeout chain + visibility guard (named). | low |
| coverage.html | Serial fetch waterfall: the 4 fetches awaited one after another. | coverage.html:183-222 | serial-fetch-waterfall | DEFERRED — Promise.allSettled (named). | low |
| coverage.html | No skip-render-when-unchanged: every tick re-assigns 9 textContent nodes + parses 4 JSON bodies even when nothing changed; no payload exposes a generation field. | coverage.html:186-223; app.py:4166 | missing-skip-render-token | FIXED (a): per-endpoint raw response-text memos (`lastPayload.{opt,spot,win,cmp}`) + the no-op-preserving `setText(el, s)` write guard; liveTxt heartbeat kept. Backend generation surfacing DEFERRED. | low — strictly no-op-preserving |
| coverage.html | POSITIVE: ZERO innerHTML; ZERO addEventListener/oninput; element refs bound once; no tables; no layout reads — the best-behaved template. | coverage.html:168-176, :101-102 | none-clean-baseline | None (reference pattern). | informational |
| fetch_windows.html | Full-container innerHTML rebuild every 4 s tick even when unchanged: poll() → renderWindows re-serializes thead + EVERY row (up to the 20,000-row route default) into panel.innerHTML; KPI/status writes also unconditional. | fetch_windows.html:350, :342, :295, :335-343 | full-DOM-rebuild-on-poll | FIXED (a): `lastPayloadKey` = marketFilter + "\|" + statusFilter + "\|" + raw text; on match write only the honest `lastLiveLine` heartbeat and return BEFORE JSON.parse + render + KPI writes; chip clicks null the memo. | low — exact raw-text compare, byte-identical render on change |
| fetch_windows.html | Unkeyed whole-table rebuild including the immutable 10-col thead per render. | fetch_windows.html:295-302, :272-294 | unkeyed-row-rebuild | DEFERRED — shell-once + tbody-only swap; mostly subsumed by the skip guard in steady state. | low |
| fetch_windows.html | No request sequencing / in-flight guard: chip clicks race the 4 s interval; a slow older 20k-row response can overwrite the newer filter's render. | fetch_windows.html:261-266, :330, :350 | overlapping-fetch-stale-render | DEFERRED — pollSeq monotonic counter (named). | low |
| fetch_windows.html | 4 s poller keeps running in hidden/background tabs. | fetch_windows.html:350 | background-tab-poll | DEFERRED — document.hidden guard (named). | low |
| fetch_windows.html | Unbounded client render vs the route default limit=20,000: no ?limit= param, renderWindows maps ALL rows into one HTML string. | fetch_windows.html:330, :281-294; app.py:4113-4114 | O(rows)-full-page-render | DEFERRED — explicit smaller ?limit= + "show more" (visible UI change). | med |
| expired_options.html | Full-container innerHTML rebuild every 4 s tick regardless of change: renderRows re-serializes thead + every row into panel.innerHTML; the coverage set is near-static between pulls so ~99% of ticks re-parse an identical table. | expired_options.html:175 (from :191 on the :206 setInterval) | full-DOM-rebuild-on-poll | FIXED (a): `lastCoverageTxt` raw-text memo gates the KPI writes + renderRows; the SECOND fetch (pull-progress) + liveTxt stay OUTSIDE the gate (they encode live pull state); `lastHeadline`/`lastRowCount` keep the skipped-tick heartbeat honest. | low — pure memoization guard |
| expired_options.html | Unkeyed whole-table rebuild: static 6-col thead re-created per render. | expired_options.html:165-179 | unkeyed-row-rebuild | DEFERRED — subsumed by skip-render in steady state. | low |
| expired_options.html | Missing skip-render token: payload is only {headline, rows} though the route rides the generation-stamped _summary_cache. | coverage_view.py:379-382; app.py:4203-4204, :4166 | missing-skip-render-token | Backend additive "generation" field DEFERRED (backend frozen); the client-side raw-text compare (shipped) delivers the same win now. | low-med |
| expired_options.html | Two serial fetches per tick: awaits /coverage/options then /universe/pull-progress serially. | expired_options.html:184, :194 | redundant-fetch-per-tick | DEFERRED — Promise.all / poll pull-progress less often (named). | low |
| expired_options.html | No in-flight guard: setInterval schedules a new async poll every 4 s with no overlap protection. | expired_options.html:206 | poll-overlap | DEFERRED — self-rescheduling chain or inFlight flag (named). | low |
| expired_options.html | KPI/status writes unconditional per tick (3 textContent + rebuilt liveTxt string). | expired_options.html:188-190, :197-199 | minor-unconditional-write | FIXED (a, rider): the 3 KPI writes moved inside the payload-changed branch; liveTxt stays outside (encodes the second endpoint's live pull state). | low |
| spot_coverage.html | Full-container innerHTML rebuild every 4 s tick regardless of change: renderSpot re-serializes the ENTIRE table (thousands of tr at ~750 underlyings × stored intervals) even when the JSON is byte-identical (the steady state). | spot_coverage.html:194, :211, :219 | full-DOM-rebuild-on-poll | FIXED (a): `lastPayloadTxt` raw-text memo at the top of poll(); on match only the `lastLiveLine` heartbeat writes. | low — pure client-side memo |
| spot_coverage.html | No skip-render signal used or exposed. WARNING: the completeness-chip rollup deliberately lives OUTSIDE _summary_cache (the C8 freshness contract) — a generation-ONLY server token would freeze chips across ledger flushes. | coverage_view.py:330-333; app.py:3655-3667 | missing-skip-render | The client-side raw-text compare IS the correct gate here (covers lake AND ledger changes) — shipped (a), stated in an in-file comment. A server token (must composite generation + ledger mtime) = DEFERRED, explicitly NOT a generation-only field. | low (client compare); med for a server token (stale-chip trap) |
| spot_coverage.html | Unkeyed whole-table rebuild: static 8-th thead re-serialized per render; sticky header torn down each tick. | spot_coverage.html:194-198 | unkeyed-row-rebuild | DEFERRED — subsumed by skip-render in steady state. | low |
| spot_coverage.html | setInterval + async poll with no overlap/visibility guard (backend cold scan documented up to ~30 s during pulls). | spot_coverage.html:219, :201-216 | poll-overlap | DEFERRED — self-chaining loop + document.hidden guard (named). | low |
| spot_coverage.html | Unconditional textContent KPI writes each tick. | spot_coverage.html:207-212 | unconditional-kpi-writes | FIXED (a): folded under the same text-compare gate; liveTxt heartbeat stays outside. | low |
| static/bx-rate-monitor.js | Every 3 s tick unconditionally rewrites all ~14 text nodes even when the payload is unchanged, INCLUDING 10 setText calls into the hidden overlay. | bx-rate-monitor.js:413, :393-406, :376-390 | missing-skip-render (in-place writes) | DEFERRED — no static-JS change in this PR. | low |
| static/bx-liveproof.js | renderBanner does a FULL child teardown + rebuild of the banner body on every 5 s poll whenever a fail signature exists — even when failSignature is unchanged; the rebuild also re-triggers bx-datepicker's body-wide MutationObserver rescan. | bx-liveproof.js:536, :402-428 | full-child-rebuild-on-poll | DEFERRED. | low |
| static/bx-datepicker.js | Unescaped page-provided option.text interpolated into innerHTML for the custom select trigger + menu options — the only latent markup sink in static/; safe only while option text is template-authored. | bx-datepicker.js:157-158 | latent-unescaped-innerHTML-sink (safety, not perf) | DEFERRED — flagged for the XSS-hardening lane. | low today; regression trap |
| static/bx-datepicker.js | Per wired picker: two document-level listeners added and never removed; N pickers ⇒ N document-click handlers per click. | bx-datepicker.js:131-132, :162 | listener-accumulation | DEFERRED. | low |
| static/bx-datepicker.js | MutationObserver on document.body {childList, subtree} for the page's life: every mutation burst triggers a full document triple-scan in auto(). | bx-datepicker.js:184-185, :166-171 | observer-rescan-churn | DEFERRED — fixing the liveproof banner rebuild removes the main trigger. | low |

### Owned pages — render-discipline + generation-token notes (as audited)

- **db.html** — Disciplined and XSS-safe: string-concat innerHTML but EVERY
  dynamic value passes esc() before interpolation; in-place patches
  deliberately textContent; delegated click-to-copy on dataEl. Perf cost was
  innerHTML GRANULARITY (whole-grid/whole-sidebar), not safety.
  **Generation token:** none on the wire — `_summary_cache.generation`
  exists server-side only.
- **movers.html** — Disciplined + XSS-safe innerHTML-template style; all
  row/cell data through esc(); every SERVER-echoed free-text (note, as-of)
  via textContent (DEC-DASHBOARD-XSS-HARDENING-001). **Generation token:**
  none — the route computes `_summary_cache.generation` one line above the
  JSONResponse but never echoes it.
- **v29.html** — No esc() helper and none needed as written: both live
  pollers write exclusively textContent; NO server-fetched string ever
  reaches an innerHTML sink. **Generation token:** BOTH pollers already
  have one on the wire (`sweep_funnel.snapshot_at`, `updated_at`) — the
  shipped stringify-compare gate subsumes them client-side.
- **coverage.html** — Fully XSS-safe by construction: ALL updates are
  textContent onto refs bound once; zero innerHTML, zero listeners. The
  reference pattern for the heavier templates. **Generation token:** none
  in any of the 4 payloads.
- **fetch_windows.html** — Escape-before-innerHTML, XSS-safe: every payload
  value through esc(); exactly 2 innerHTML sinks both on #panel; chips
  wired once. **Generation token:** none — `_summary_cache.generation`
  keys the counts cache server-side, never on the wire.
- **expired_options.html** — XSS-safe: every interpolated value through
  esc(); ZERO event listeners. **Generation token:** none — payload
  {headline, rows} only.
- **spot_coverage.html** — Escape-before-innerHTML, XSS-safe; safety is
  CONVENTION not structure. **Generation token:** none; NOTE the C8 trap
  (chip rollup outside `_summary_cache`) — the safe cheap gate today is the
  client-side raw-text compare (shipped).
- **static JS** — bx-rate-monitor + bx-liveproof: zero innerHTML,
  createElement+textContent only; bx-datepicker: 5 innerHTML sites (the
  only innerHTML in static/), two interpolating option.text unescaped.
  **Generation token:** none in any widget payload.

---

## Fenced follow-ups (REPORTED only — no edits proposed or made here)

Fence: `universe.html` + `completeness.html` are owned by open PR #1097;
`reports.html` + `welcome.html` + `app.py` by open PR #1093;
`results.html` is cell-for-cell pinned by the DEC-SURFACE-TRIAL-001
surface-trial suites; the backend is frozen for this PR. The findings
below are recorded for those future PRs.

**The backend generation-echo field** is itself a fenced follow-up: every
skip gate in this PR is a CLIENT-side payload compare because no payload
may change shape here. A future backend PR can echo the already-computed
`_summary_cache.generation` (or, for `/coverage/spot`, a composite
generation + ledger-mtime token — see the C8 warning above) and let the
clients compare one small token instead of the full body text.

| page | finding | evidence (file:line @ aa034f2) | cost class | fix (future PR) | risk |
|---|---|---|---|---|---|
| universe.html | renderLiveStats full-container innerHTML rebuild of #tfgrid EVERY 600 ms pull-progress tick even when values unchanged (~1.7 rebuilds/s for the entire pull); sibling renderPhases already demonstrates the correct create-once/update-in-place pattern. | universe.html:1854 + :1865, from pollProgress :1776; PROGRESS_MS=600 :1556 | full-DOM-rebuild-on-poll | Mirror buildPhaseRow/updatePhaseRow (:1703-1747): build 5 tiles once, textContent updates; short-circuit on unchanged tuple. | low |
| universe.html | Scroll-preservation in renderInstruments is INERT + forces a mid-render synchronous layout: el.scrollTop READ after the innerHTML wipe → forced flush + always restores clamped 0. | universe.html:1166 → :1171 → :1214 | layout-thrash | Hoist `const prevScroll = el.scrollTop;` above the wipe. | low perf / med UX |
| universe.html | renderInstruments full unkeyed rebuild of ~750 rows on every tab switch, debounced search keystroke, F&O toggle, constituents tick. | universe.html:1153-1216 | unkeyed-row-rebuild | Keyed in-place reconcile (rows carry data-symbol); .hidden toggle for filtered rows; DocumentFragment batching. | low-med |
| universe.html | Listener churn: 3 fresh closure handlers PER ROW per rebuild ≈ 2,250 new closures per ~750-row rebuild. | universe.html:1198, :1199, :1209 | listener-churn | One delegated click + keydown on #instruments. | low |
| universe.html | refreshCov patches per-row strips via cov.outerHTML for EVERY rendered row on every COV change — ~750 serialize+re-parse ops per 15 s tick mid-pull. | universe.html:1149 (setInterval :1918) | unkeyed-row-rebuild | Diff per symbol; patch only changed entries via textContent spans. | low |
| universe.html | Skip-render for the completeness poll is a client-side O(payload) JSON.stringify signature of the entire ~750-symbol map every 15 s; the other six pollers have NO skip at all. | universe.html:1008-1009; app.py:3607-3616 | missing-generation-token-skip | Future backend+frontend: composite (generation, master_revision) payload field or ETag/304. | low |
| universe.html | refreshWindowsLine poller never stops: pullStarted set true and NEVER reset → after the first pull the 5 s interval fetches /coverage/windows forever for one textContent line. | universe.html:1611, :1620, :1587 | unbounded-poller | Reset pullStarted in finishLive(), or gate on state === 'running'. | low |
| universe.html | Five 15 s setIntervals registered back-to-back with identical periods, no stagger → synchronized burst of 5-6 concurrent requests every 15 s. | universe.html:1918-1926 + :1914 | poller-burst | Stagger initial offsets or consolidate behind one status endpoint. | low |
| completeness.html | Full innerHTML rebuild of ALL THREE panels every 4 s tick, zero unchanged check — up to ~1000 rows re-serialized/re-parsed/re-laid-out per tick. | completeness.html:297-316, :239, :261, :290 | full-DOM-rebuild-on-poll | Text-compare skip (this PR's pattern) or the backend generation gate. | low |
| completeness.html | Missing skip token despite available backend token: payload {spot, options} only. | completeness.html:299-304; completeness.py:859-865; app.py:3688 | missing-generation-skip | Additive "generation" field + early-return gate. | low |
| completeness.html | Unkeyed whole-table rebuild incl. static theads; sticky-header CSS re-instantiated each tick. | completeness.html:261-265, :290-294 | unkeyed-row-rebuild | Static skeletons + tbody-only swaps; keep the empty-state branches. | low |
| completeness.html | setInterval with no in-flight guard/backoff against the historically slowest endpoint (7-9 s cold, ~30 s during a pull) → stacked overlapping fetches, out-of-order triple rebuilds. | completeness.html:316 + :297-313 | poll-overlap | Self-rescheduling setTimeout chain or inFlight flag; error backoff. | med latent |
| completeness.html | No visibility gating: 4 s poll + triple rebuild continues in hidden tabs. | completeness.html:315-316 | background-poll | Skip when hidden; immediate poll on visible transition. | low |
| reports.html | Unkeyed full chip-grid rebuild on every bulk-selection action: renderChips wipes + rebuilds ALL ~750 chips (≈3,000 DOM nodes) though the chip SET never changes after loadSymbols. | reports.html:347 + :341-375, :388-399, :419 | unkeyed-row-rebuild | Build chips once; bulk actions patch classList.toggle('on', ...) in place. | low |
| reports.html | Listener churn: fresh chip.onclick closure per chip per renderChips pass. | reports.html:368 | listener-churn | One delegated click on #syms keyed by data-symbol. | low |
| reports.html | Missing skip on the sweep poller: renderSweep per 1200 ms tick unconditionally rewrites textContent + sweepFill.style.width (re-triggering its CSS transition) though the payload exposes updated_at. | reports.html:527-540; sweep_progress.py:165 | missing-skip-render-token-gate | Gate on p.updated_at; hoist the one-time display='block'. | low |
| reports.html | Poller never terminates on the server-restart edge + no error backoff: a restart-to-idle mid-sweep polls FOREVER with sweepBtn stuck disabled. | reports.html:532-539, :568 | unbounded-poll-on-terminal-state | Clear/back off after N consecutive idle ticks post-start; re-enable sweepBtn; visibility pause. | low-med (stuck-UI hazard) |
| reports.html | Deliberate forced synchronous reflow per selection change: `void countEl.offsetWidth` to restart the count 'pop' animation. | reports.html:330 | layout-thrash | Restart animation via rAF class re-add / Web Animations API. | low (intentional) |
| reports.html | Two external self-injecting polling widgets loaded (site-wide per DEC-PULL-RATE-WIDGET-VISIBILITY-001 #1000). | reports.html:167-168 | external-poller | Audited under static JS above; no template change implied. | informational |
| welcome.html | innerHTML inside a requestAnimationFrame loop: countUp re-parses each stat counter's markup per frame (~84 frames × 4 counters) — one-shot per counter, bounded. | welcome.html:309 (:305-312) | innerHTML-in-rAF-loop | Dedicated text node textContent + persistent suffix span. | low |
| welcome.html | Raw innerHTML with interpolated suffix and no esc(); today sources are static template attrs — fragile if ever server-derived. | welcome.html:297-299 (:303, :309) | unescaped-innerHTML-interpolation (latent) | textContent + pre-built suffix span; or esc() the suffix. | low |
| welcome.html | Continuous compositor cost: three 60vw blurred mix-blend-mode orbs animating transform forever + infinite shimmer/pulse + backdrop-filter on the sticky bar. prefers-reduced-motion correctly kills all of it. | welcome.html:40-52, :73-76, :84-85 | continuous-compositor-animation (GPU) | Reduce blur/orb size; visibility pause; static gradient post-hero. | low (cosmetic) |
| welcome.html | The static landing page carries the two permanent site-wide pollers (3 s + 5 s) forever, with no skip-fetch token. | welcome.html:186-187 | idle-poll-chatter | Widget-level idle backoff / visibility pause / ETag — belongs to the shared widgets. | low |

### Fenced pages — render-discipline notes (for the follow-up PRs)

- **universe.html** — deliberate XSS-safe discipline (one esc() on every
  broker/server string; hot in-place paths textContent + createElement with
  explicit CodeQL-guard comments). innerHTML is a perf concern here, not a
  safety one. No generation token across all ~11 fetched endpoints.
- **completeness.html** — consistent escape-before-innerHTML; NO event
  listeners at all. Natural skip token is `_summary_cache.generation`
  (+ the #1149 lake_revision probe covers CLI pulls).
- **reports.html** — XSS-safe via the stricter createElement+textContent
  discipline; the 4 innerHTML sites are static literals. `updated_at` IS
  exposed by /api/v1/sweep-progress but ignored.
- **welcome.html** — only two DOM writes, raw innerHTML with a
  template-authored suffix (safe in practice, latent); both injected
  widgets are exemplary.

---

## LAB evidence — before/after render cost

**Label: LAB measurement — synthetic payloads, not production traffic.**

### Method

- **Harness:** headless Chromium (chromium-1194, driven by Playwright
  Python 1.61 with an explicit `executable_path`). The harness + raw
  results lived in the working scratchpad and are NOT committed — the
  numbers below are the recorded output of that run.
- **Arms:** BEFORE = templates from commit `aa034f26` (`git show`);
  AFTER = the working tree on `claude/happy-goldberg-qqqgf6`.
- **Serving:** each template served as a local page (the templates carry
  no Jinja constructs — verified — so they parse verbatim). EVERY
  `/api/v1/*` fetch stubbed to a FIXED, byte-identical JSON payload per
  tick (minimal-valid payloads crafted from each page's JS field reads:
  100-row tables for spot_coverage / fetch_windows / movers / db-ledger,
  50 rows for expired_options; the db ledger stub echoes the requested
  `offset`). `/static/*.js` widgets + the html2canvas CDN aborted in BOTH
  arms so only the template's own JS is measured.
- **Instrumentation** (installed before any page script runs): one
  MutationObserver on `document` counting records + DOM nodes churned
  (added+removed, INCLUDING subtrees) + characterData/attribute records;
  an `addEventListener` wrap counting listener attachments; render-CPU ms
  by wrapping each page's own global render functions (coverage.html
  renders inline in `poll()` → no wrappable fn, reported n/a).
- **Tick driving:** auto-`setInterval` disabled; ONE warm-up tick, counters
  reset, then N=10 ticks awaited directly (v29's IIFE-scoped pollers:
  interval clamped to 250 ms, 11 measured ticks per poller). db.html:
  window-ledger view, 5 real `#dbnext` clicks with an identical column
  set. v29 filter: 20 rapid `input` events on `#fSharpe`. Zero
  `pageerror` events in every scenario, both arms.

### Results (totals over the measured window; per-tick in parentheses)

Steady-state poll ticks, byte-identical payload each tick (N=10):

| Page | Metric | BEFORE | AFTER | Delta |
|---|---|---|---|---|
| coverage.html | mutation records | 90 (9.0/tick) | 10 (1.0/tick) | **-89%** |
| | DOM nodes churned | 180 (18/tick) | 20 (2/tick) | -89% |
| | render-fn ms | n/a (inline in poll) | n/a | — |
| spot_coverage.html | mutation records | 50 (5.0/tick) | 10 (1.0/tick) | **-80%** |
| | DOM nodes churned | 38,480 (3,848/tick) | 20 (2/tick) | **-99.9%** |
| | renderSpot ms | 21.6 (2.16 ms/tick) | 0.0 (skipped) | **-100%** |
| expired_options.html | mutation records | 50 (5.0/tick) | 10 (1.0/tick) | **-80%** |
| | DOM nodes churned | 14,400 (1,440/tick) | 20 (2/tick) | **-99.9%** |
| | renderRows ms | 7.7 (0.77 ms/tick) | 0.0 (skipped) | **-100%** |
| fetch_windows.html | mutation records | 60 (6.0/tick) | 10 (1.0/tick) | **-83%** |
| | DOM nodes churned | 46,580 (4,658/tick) | 20 (2/tick) | **-99.9%** |
| | renderWindows ms | 26.1 (2.61 ms/tick) | 0.0 (skipped) | **-100%** |
| movers.html (repeat `load()`, same coordinate) | mutation records | 60 (6.0/tick) | 30 (3.0/tick) | -50% |
| | DOM nodes churned | 26,300 (2,630/tick) | 40 (4/tick) | **-99.8%** |
| | renderHead+renderRows+renderPills ms | 56.5 (5.65 ms/tick) | 0.0 (skipped) | **-100%** |
| v29.html pollers (11×2 stubbed ticks) | mutation records | 330 (30/tick-pair) | 0 | **-100%** |
| | DOM nodes churned | 660 (60/tick-pair) | 0 | -100% |

The AFTER residual 1–3 records/tick is the deliberately-ungated heartbeat
(`liveTxt`/`noteText` textContent writes — re-assigning the same string
still replaces the text node).

v29.html filter debounce (20 rapid `input` events on `#fSharpe`):

| Metric | BEFORE | AFTER |
|---|---|---|
| `applyFilters` executions | **20** | **1** (trailing 200 ms debounce) |
| mutation records in window | 37 | 23 |
| DOM nodes churned (comboMeta innerHTML rebuilds) | 280 | 15 (one-time structure build, then textContent) |
| attribute records (row show/hide — the real filter work) | 17 | 17 (identical outcome) |

(Static observation, not measured above: BEFORE double-bound
`change`+`input`, so a real `<select>` change ran the filter **twice**;
AFTER binds `input` once.)

db.html window-ledger pager (5 `#dbnext` clicks, identical column set):

| Metric | BEFORE (whole-grid innerHTML) | AFTER (tbody-only patch) |
|---|---|---|
| updateLedgerGrid ms | 31.8 (6.36 ms/click) | 11.9 (2.38 ms/click) — **2.7× faster** |
| DOM nodes churned | 8,725 (1,745/click) | 8,510 (1,702/click) |
| pager listener re-attachments | 10 (2/click) | **0** (one delegated listener) |
| mutation records | 20 | 26 |

Honest reading of db.html: the 100-row tbody is the page CONTENT and must
be rebuilt on a real page change either way, so total node churn is
near-equal by construction. The measured wins are the ~43 nodes/click of
thead+pager+gridwrap that now SURVIVE (record count rises slightly because
4 small targeted patches replace 1 giant innerHTML record), the 2.7×
render-fn time, zero listener re-wiring, and — unquantified here — the
preserved `.gridwrap` scroll container/scroll position.

### Caveats

1. **LAB measurement — synthetic payloads, not production traffic.**
   Payload shapes are minimal-valid; production bodies are larger, which
   would WIDEN the skip-render deltas, not shrink them.
2. The steady-state scenario (byte-identical body every tick) is the BEST
   case for the skip-render guards — it is also the DOMINANT production
   case (the backend caches — `SummaryCache`/`MoversSnapshotCache` — serve
   byte-identical JSON per write-generation). On every payload CHANGE the
   AFTER pages pay the same full render as BEFORE, by design.
3. Wall-clock per tick includes the route-stub round trip (equal in both
   arms), so `renderMs` + mutation/node counts are the honest render-cost
   signals; no ms figure here is a production-latency claim.
4. Node churn counts added+removed nodes including subtrees; a single
   `innerHTML` swap is ONE mutation record with a large subtree, so records
   and node counts must be read together (the db.html row shows why).
5. `/static` widget scripts + CDN are aborted in both arms; their cost is
   out of scope of this decision's diff.
6. v29 poller tick counts are 11 (not exactly 10) per arm; per-tick figures
   divide by the measured 11.
7. coverage.html has no wrappable render function; its render-CPU delta is
   bounded by the mutation/node counts only.
8. Chromium 1194 driven by Playwright 1.61 (one minor-version mismatch,
   explicit `executable_path`); zero page errors in both arms.

---

## Test pinning (the permanent net)

- NEW `tests/integration/test_dashboard_js_perf_frontend.py` (31 tests):
  per-page skip-render guard fragments + the intact 4 s cadence + the
  raw-text compare (a); the debounce wiring + the pinned filter-column
  correction, indices AND thead order (b); `patchGridRows` + the shared
  `gridRowsHtml` + the delegated pager + the deleted `wire*Pager` helpers
  (c); the movers skip/gate/sort/delegation/debounce mechanisms + the
  DEC-PIPELINE-AUTOPREBUILD-001 textContent + 5 s retry pins (d); and the
  per-template innerHTML ASSIGNMENT-SINK count ceiling vs the `aa034f2`
  baseline (e — the one sanctioned delta is db.html's tbody patch).
- Existing suites re-run green (route/template integration): coverage,
  ux-polish, coverage-invalidate, responsive-under-write, pull-rate-monitor,
  nav-consistency, db_view, movers (+gate/futures/options), movers XSS,
  prebuild gate, calendar-label, movers discovery/exchange/latest-date/
  poison-skip memos, db ux/pagination/query/cascades/moneyness/candle-lake,
  latency-o1, a11y, mobile-ux, reports XSS — 679 tests total in the final
  gate, 0 failures.
- Reminder: these pins assert TEMPLATE SOURCE + rendered-route behavior;
  they are not JS execution coverage (none exists in this repo).
