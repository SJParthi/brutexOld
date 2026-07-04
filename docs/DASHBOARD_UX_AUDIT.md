# Dashboard UX Audit → Fix Record

**Decisions:** DEC-PREBUILD-GIL-VECTORIZE-001 (MIGRATION.md #1303) +
DEC-DASHBOARD-UX-ANIMATE-001 (MIGRATION.md #1304).
**Date:** 2026-07-04 · **Branch:** `claude/happy-goldberg-qqqgf6`.
**Scope law:** read-only dashboard UX (templates + two NEW static assets) plus one
data-lake write-path vectorization. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX,
CLAUDE.md §3). No scope/vocab/rate change. No rendered VALUE changes anywhere —
DEC-SURFACE-TRIAL-001 pins `/results` + `/reports` cell-for-cell.

This document is the audit→fix record for the full 12-template dashboard UX pass:
the P0 starvation diagnosis, the per-page findings, what shipped on the FREE
surface, and the implementation-ready packages for every open-PR-FENCED surface.

---

## 1. P0 diagnosis — blank Operator `/` during the pipeline prebuild

**Symptom (operator-reported):** mid-pipeline, a fresh navigation to `/` renders a
blank page; already-open sibling tabs keep polling (looking deceptively alive);
"Loading…" placeholders stick; the rate widget reads STALLED-at-0 (which is honest
— the prebuild fetches nothing).

**Root cause (live-repro'd, `scratchpad` harness `repro_server.py` + driver):**
`GET /` is a module-cached string and is never slow by itself; the `PrebuildGate`
gates only `/api/v1/movers`. The one-click pipeline runs
`materialize_lake_timeframes(include_daily=True)` **inside the dashboard server
process** on the `BackgroundPullJob` daemon thread. Per symbol it (a)
`pl.scan_parquet(whole 1-minute tree).collect()` on polars' global pool = ALL
cores, (b) runs 19 resamples, and (c) ran a **GIL-held pure-Python per-row
`iter_rows → dict → 4×Decimal(str(float)) → CandleBar` conversion** — once in the
derived-TF writer (`timeframe_materialize.py`, pre-fix lines 516–527) and once in
the spot daily resampler (`spot_daily_resample.py`, pre-fix lines 359–370, which
also did a per-row `astimezone(IST) → date → re-stamp` dance). Stack samples
caught the prebuild inside exactly these loops. Combined with polars saturating
every core, a fresh page load starves while established keep-alive connections
squeeze through — exactly the observed differential.

**Fix arms (fence-governed):**

| Arm | File(s) | Verdict |
|---|---|---|
| Vectorize the GIL-held loops | `brutex/data/timeframe_materialize.py`, `brutex/data/spot_daily_resample.py` | **SHIPPED** (DEC-PREBUILD-GIL-VECTORIZE-001, #1303). One COMMON batched converter `frame_to_candle_bars` (CLAUDE.md §11 COMMON — both former loop sites import it); prices rendered to decimal strings INSIDE polars (`cast(String)`, GIL released) + a memoised `str → Decimal` parse; the daily resampler's IST-date classify / forming-session filter / midnight-IST re-stamp became three columnar expressions. **Lake output byte-identical** — proven 3 ways (§5.2). |
| `POLARS_MAX_THREADS` cap at the CLI dashboard/pull entrypoints | `brutex/cli.py` | **FENCED — NOT SHIPPED.** Re-verified live 2026-07-04: open PRs #1112 (+200/−27 on cli.py), #1131, #1130, #1086, #1085 all touch `brutex/cli.py`. Follow-up recipe in §4.7. The decision was DRAFTED as `DEC-PREBUILD-STARVATION-CAP-001` naming this cap; the 4-agent hostile review found the cap unshipped-but-cited and the decision was RENAMED to what actually shipped (see MIGRATION.md #1303). No ID is reserved for the cap, so code can never again cite an unlanded claim. |
| `os.nice(+10)` on the BackgroundPullJob worker thread | `brutex/dashboard/pull_jobs.py` | **FENCED — NOT SHIPPED** (open PRs #1131 #1128 #1124; also on this task's hard-fence list). Follow-up in §4.8. |
| Subprocess isolation of `run_pipeline_prebuild_guarded` (the true cure) | `brutex/dashboard/*` | **NOT SHIPPED** — operator-deprioritized previously (MIGRATION.md #920 context); re-raised as follow-up §4.10. |

**Honest consequence:** with only the vectorization shipped, polars core
saturation remains — production starvation is **REDUCED, not eliminated** (§6).

---

## 2. Per-page findings (12 templates + shared widgets)

Legend: **[EDIT]** = shipped in this change · **[FENCED]** = open-PR-fenced,
REPORT-only package in §4. Evidence cites the pre-fix template source.

| Page | Defect | Evidence (pre-fix) | Fix | Before → after (plain words) |
|---|---|---|---|---|
| `/` (v29) **[EDIT]** | Blank mid-pipeline; navigation dead end (zero internal links); fictional ₹ mockup fronted by a 10 px badge; stale "v28" tab title; boot gated on the cdnjs `html2canvas` script — the app's only external boot dependency | P0 diagnosis (§1); `v29.html` line 5 title, line 6 CDN `<script defer>`; zero `href="/` matches | P0 arms (§1, server-side); operator nav links (`.bx-opnav` — deliberately not `topnav`, which is test-pinned absent on `/`); live "System heartbeat" hero (real pull rate / sweep progress / liveproof with skeleton → data / honest idle / honest error); persistent "◈ PREVIEW" ribbon; html2canvas lazy-loaded on first Export click; hidden-tab poller skip + `visibilitychange` refresh; `.bx-wrap--wide`; gutters normalized | Blank spinner during pulls → the page always paints and shows what the system is actually doing; dead end → one click to any page; fake profits front-and-center → real pipeline status front-and-center, mockup clearly ribboned |
| `/results` **[EDIT]** | Error/hang left terminal "loading…" with empty stat cards (R-1); "live" dot pulsed green even while offline (R-2); no max-width (thin slabs on ultra-wide, R-3); bottom widgets covered last rows (R-4); per-poll `onclick` rebinding (R-5); long conditions wrapped rows tall (R-6) | audit R-1..R-6; error path updated only the header text | Skeletons (4 ghost KPI cards + ghost grid, 10 s watchdog caption); `BXUI.fetchWithTimeout` 12 s; `.bx-state.error` + Retry; error-after-success keeps the last real table + "error N · showing last data"; truthful `BXUI.setDotState`; `main.bx-wrap`; dock padding; delegated sort listener; `.cond` CSS ellipsis + `title`; `BXUI.countUp` change-only (final exact string synchronously — **values cell-for-cell unchanged**, DEC-SURFACE-TRIAL-001 suite green unmodified) | Permanent "loading…" after a hiccup → clear "error — retry" card; lying green dot → a dot that tells the truth; thin stretched slabs → composed centered layout; covered rows → always visible |
| `/db` **[EDIT]** | Six bare `loading…` teardowns could sit forever; errors were bare red one-liners; fixed 280 px sidebar; widgets covered the pager | `db.html` `loading…` at :247/:594/:690/:872/:1003/:1150 | `BXUI.skeleton` ghosts + 10 s "still working — the lake is busy (a pull/prebuild may be running)" watchdog; `.bx-state` error/offline cards with Retry re-invoking the existing runner; fluid sidebar `clamp(240px,18vw,340px)`; `var(--gutter)`; tokenized badges; dock padding; `#dbgrid.is-loading` dim kept for grid-only updates | Frozen "loading candles…" → shimmering table that explains itself if slow; tiny red "error 500" → a real error card with Retry; covered pager → always clickable |
| `/coverage` **[EDIT]** | Errors froze KPIs as "—" with a GREEN dot; 4 serial awaits (one slow endpoint stalled all); flat pop-in | hub fetch chain, serial `await`s; no `!r.ok` path | `Promise.allSettled` parallel fetches; per-card shimmer; amber "error N · retrying" chips; truthful heartbeat dot; card stagger + gradient edges; `BXUI.countUp`; dock padding | Silent frozen dashes → visible retrying state; one slow endpoint freezing the page → the rest keep updating; flat hub → polished animated launchpad |
| `/coverage/spot` **[EDIT]** | Stuck `loading…` on any error; header overflow at 900–1300 px (20-word caveat in `.sub`); inline `#34d399/#f87171/#9aa4b2` shadow-hex chips; green "0 datasets" read as good | `spot_coverage.html` `loading…` at :134/:216/:229 | Skeleton + `BXUI.errorCard`; error-after-success keeps the last table + "stale — retrying" ribbon; caveat moved to a footnote; `.bx-chip.*` tokenized chips (chip TEXT byte-unchanged — `completenessChip` pin green); value-aware KPI colors; first-render-only row stagger; dock padding | Frozen "loading…" → shimmer then honest error with retry; broken header at laptop widths → clean header; misleading green zero → neutral zero |
| `/coverage/options` **[EDIT]** | Stuck `loading…` on errors; a RUNNING pull's only hint was a small text swap; heartbeat claimed "live" while the progress feed was down | `expired_options.html` `loading…` at :136/:197/:218; pull state fetched at :212–217 but untinted | Skeleton + error card; `.bx-ribbon` animated pull-active strip + fast-pulse dot while `running===true`; grey heartbeat when the feed is down; `BXUI.countUp`; illustrated empty + CTA to `/universe`; `data-accent=purple`; dock padding | Frozen "loading…" → honest states; invisible pull → a visible living ribbon while data flows in |
| `/movers` **[FENCED #1119]** | Stuck "Note: Loading…" forever mid-pull; left-locked `max-width:1340px`; gated state a whisper chip; sticky thead `top:64px` magic number; error-after-success silently swapped to sample rows | `movers.html` :38 (max-width), :386 (Loading note) | §4.1 package | Frozen "Loading…" → shimmer → honest retrying state; left-locked → centered fluid; cryptic gated chip → clear "prebuild running, auto-resumes" banner |
| `/universe` **[FENCED #1134 #1124 #1121 #1097]** | Sticky pullbar (`top:8px`, higher z) paints OVER the sticky header — click-blocks the nav; failed load shows a false "Loading the instrument master…"; a summary 500 shows "lake is empty (expected, not a bug)" | `universe.html` pullbar rule; catch-less load paths | §4.2 package | Nav blocked when scrolled → nav always usable; false "Loading…"/false "empty lake" on errors → honest "server unreachable — retrying" |
| `/coverage/windows` **[FENCED #1129]** | Stuck `loading…` on errors; "0 failed" glows red; long vendor errors stretch the table; full tbody innerHTML swap every 4 s loses scroll | `fetch_windows.html` :183/:341/:359 | §4.3 package | Frozen "loading…" → honest states; red-zero panic → calm neutral; scroll yanked every 4 s → the page holds still |
| `/completeness` **[FENCED #1097]** | The ONE family page missing the skip-render memo — full innerHTML rebuild of a ~750-row table every 4 s poll (scroll/selection lost); dead sticky theads (no scroll container); three `loading…` stubs on the family's slowest endpoint (~30 s mid-pull) | template poll loop; no `lastPayloadTxt` | §4.4 package | Page twitching every 4 s → rock-still until data changes; headerless data soup after row 30 → headers always visible; frozen "loading…" on a 30 s scan → an explained wait |
| `/reports` **[FENCED #1093]** | Left-locked `max-width:1100px`; first heading clipped under the sticky header; a symbols 500 renders the false "empty lake" message; failed download navigates to a raw-JSON error page | `reports.html` :52 (max-width); `r.json()` without `r.ok` | §4.5 package | Half-empty screen → content uses the screen; "lake is empty" lie on a 500 → "couldn't load — retrying"; raw JSON error page → in-page message. Sheet VALUES untouched (DEC-SURFACE-TRIAL-001) |
| `/welcome` **[FENCED #1093]** | Three stale headline facts — "2021" (floor is 2020, DEC-PULL-FLOOR-2020-001), "5yr" (6), "241 underlyings" (~750-lake / 241-tradable split); outside the shared design system; ambient blur animations never pause | `welcome.html` hero copy; no bx-theme link | §4.6 package | Wrong headline numbers → honest 2020 / 6 yr / ~750-lake facts; the odd page out → same design language; battery-burning backdrop → pauses when hidden |
| Bottom widgets **[FENCED: bx-rate-monitor.js #1128 #1124 #1119 · bx-liveproof.js #1131]** | The two fixed pills covered last rows/pager on every page; can collide on small screens | fixed-corner CSS in both widget JS files | Shipped now: **padding-only docking** — `bx-design.css` reserves `--dock-h` bottom space on adopting pages (widget JS untouched). §4.9 for the full dock | Content hidden under floating pills → last row and pager always visible |

---

## 3. What shipped (the FREE surface)

**Data path (DEC-PREBUILD-GIL-VECTORIZE-001, #1303):**
`brutex/data/timeframe_materialize.py` (NEW public `frame_to_candle_bars`,
`make_lake_write_tf._write` refactored onto it) +
`brutex/data/spot_daily_resample.py` (`resample_1m_to_daily` vectorized).
F&O daily (`fno_daily_resample`) deliberately NOT touched (outside the approved
surface; per-contract small) — its parity still rides the FUTURES leg of the new
integration golden.

**Design system (DEC-DASHBOARD-UX-ANIMATE-001, #1304):**
- `brutex/dashboard/static/bx-design.css` (NEW) — tokens (palette byte-identical
  to `bx-theme.css`), ONE motion vocabulary (`bxFadeInUp/Down`, `bxShimmer`,
  `bxGradShift`, `bxPulse`, `bxSpin`, `bxCountPop`, `bxFloat`, `bxRowIn`,
  `bxRiseIn` — every `@keyframes` transform/opacity only, plus the repo-sanctioned
  `background-position` gradient drift), components (`.bx-wrap`, `.bx-card`,
  `.bx-chip.*`, `.bx-btn*`, `.bx-table`/`.bx-tablewrap` with sticky-in-scroller
  theads, `.bx-skel*`, `.bx-state.{empty,error,offline,gated,busy}`,
  `.bx-live-dot.{live-ok,live-warn,live-down}`, `.bx-ribbon`, `.bx-stagger*`,
  `.bx-countpop`, `.bx-paused`), the canonical `.001ms`
  `prefers-reduced-motion` kill, and padding-only widget docking.
- `brutex/dashboard/static/bx-ui.js` (NEW) — `window.BXUI`:
  `esc/num/el/clear`, `fetchWithTimeout` (AbortController, 12 s default),
  `skeleton` (attach-guarded 10 s watchdog), `stateCard/errorCard/emptyState`
  (retry via addEventListener; `ctaHref` app-relative only), `setDotState`,
  `countUp` (change-only via `data-bx-prev`; default writes the FINAL exact
  string synchronously — value-pin-bulletproof), `stagger` (once per render
  signature), `initVisibilityPause`. **Zero string-to-DOM sinks** (test-pinned:
  no `innerHTML`/`outerHTML`/`insertAdjacentHTML`/`document.write`).
- Six FREE templates adopted additively (`v29`, `results`, `db`, `coverage`,
  `spot_coverage`, `expired_options`): bx-* classes ADDED alongside existing
  ones; **no test-pinned id/class/string renamed**; every bare `loading…`
  terminal placeholder removed — "Loading…" is BANNED as a terminal state.
- Tests: NEW `tests/integration/test_dashboard_ux_design.py` (8 pins that FAIL
  on pre-adoption templates) + `tests/unit/test_dashboard_static_assets.py`
  extended (+5) + the 53/53 extreme-permutation matrix (§5.3).

---

## 4. FENCED follow-up packages (implementation-ready, zero bytes changed here)

Each package maps 1:1 onto the shipped `bx-design.css`/`bx-ui.js` primitives, so
adoption is mechanical once the fence clears.

1. **movers.html** (fence #1119): `.bx-wrap` (kills the un-centered 1340 px lock);
   skeleton rows + `BXUI.fetchWithTimeout` kill the stuck "Note: Loading…" (:386);
   first-class `.bx-state.gated` banner (pulsing amber dot + countdown over
   `GATE_RETRY_MS`); sticky thead → `top:0` inside `.bx-tablewrap`;
   error-after-success keeps the last real table + amber "stale" chip (never
   sample-preview rows); `#asof` innerHTML badge → createElement; reduced-motion
   aligned to the `.001ms` form. DO-NOT-BREAK: ids `noteText/asof/note/rows/head/
   pills`; gated paint-once memo (`wasGated`/`lastGatedKey`); `lastRenderSig`;
   debounce; delegated listeners; `GATE_RETRY_MS`; the `📭 no movers` string only
   for a genuine non-gated empty.
2. **universe.html** (fences #1134 #1124 #1121 #1097): pullbar
   `top:8px → top:calc(var(--header-h) + 8px)` (stays ONE flat rule —
   test-pinned); `loadInstruments` catch → `BXUI.errorCard` (never the false
   "Loading the instrument master…"); a summary 500 → distinct "server
   unreachable — retrying" (never "lake is empty (expected, not a bug)");
   the 4 integrity panels → one `.bx-verdict-panel`; 10 s skeleton watchdog.
   DO-NOT-BREAK: the long pinned id list (`#tabs #universeHero #pullPipelineBtn
   #emptyBanner #verdictBanner #pullpanel #phaseProgress #quotaGauge …`);
   beam-gated-on-`.running`; capped stagger; verdict textContent strings.
3. **fetch_windows.html** (fence #1129): skeleton + error card (:183/:341/:359);
   filter chips → `.bx-chiprow` (ids `#mktfilter/#stfilter` + `data-mkt/data-st`
   + `wireChips` + `lastPayloadKey` fold byte-identical); `Last error` cell
   `max-width:36ch` ellipsis + `title`; KPI color-by-value (no red "0 failed");
   `body[data-accent=yellow]`; tbody in-place patching (extends
   DEC-DASHBOARD-JS-PERF-001 — scroll no longer lost every 4 s).
4. **completeness.html** (fence #1097): ADD the missing skip-render memo
   (`lastPayloadTxt`) — the one DEC-DASHBOARD-JS-PERF-001 hole in the family;
   both panels become real scroll regions (`max-height:min(56vh,640px)`) so the
   sticky theads work; `scroll-margin-top` on `.sectiontitle`; skeletons + an
   honest "full-lake scan running (self-throttled during pulls)" note after 3 s.
   DO-NOT-BREAK: chip verdict wording (`full/stable/partial/growing/failed/
   none`); category badges; honest caveat text.
5. **reports.html** (fence #1093): `.bx-wrap` (centers the 1100 px column);
   `scroll-margin-top` + `#syms{overflow-anchor:none; min-height:96px}`;
   `if(!r.ok) throw` before `r.json()` (a 500 never renders the false "empty
   lake"); fetch→blob download with inline button spinner; progress bar
   `width` → `transform:scaleX`. DO-NOT-BREAK: DEC-SURFACE-TRIAL-001 sheet
   values; skeleton chips (the donor pattern); `#msg/#count/#syms/#idxpick/
   #sweep-msg/#cascade-msg`; esc()/createElement chip discipline.
6. **welcome.html** (fence #1093): fix the three stale facts ("2021" → 2020 per
   DEC-PULL-FLOOR-2020-001; "5yr" → 6; "241 underlyings" → the honest ~750-lake /
   241-tradable split — Golden Rule #1); adopt bx-theme/bx-design + the canonical
   header; retire its private `countUp` for `BXUI.countUp` (welcome was the
   donor); harvest aurora/reveal styles into bx-design; pause orbs on
   `document.hidden`.
7. **`brutex/cli.py` polars cap** (fences #1112 #1131 #1130 #1086 #1085):
   `os.environ.setdefault("POLARS_MAX_THREADS", str(max(2, cpu_count − 2)))` at
   the CLI **dashboard/pull entrypoints only**, BEFORE the first polars import
   (setdefault → an operator env override always wins; env name for the override:
   the standard `POLARS_MAX_THREADS` itself). NEVER the engine/sweep path — it
   owns its own per-worker `=1` pin (`brutex/engine/sweep_parallel.py:356`). This
   is the diagnosis's PRIMARY remaining lever; ship under its OWN new decision ID
   when the fence clears (none reserved).
8. **`pull_jobs.py` worker deprioritization** (fences #1131 #1128 #1124):
   `os.nice(+10)` on the `BackgroundPullJob` worker thread so the prebuild yields
   to the event loop under contention.
9. **Widget dock v2** (fences: both widget JS files): rate-monitor adopts
   liveproof's proven `bxlp-min` auto-collapse; mobile stacking
   `@media(max-width:680px)`; shared z-scale; ids `bxRateMonitor/bxLiveProof`
   kept. Padding-only docking already shipped.
10. **app.py moves** (fenced by 9 open PRs): event-loop `generation`-probe →
    threadpool moves (app.py:4360/5026/5262/5286/5410); and the **true cure** —
    subprocess isolation of `run_pipeline_prebuild_guarded` (operator-
    deprioritized in the MIGRATION.md #920 era; re-raised here explicitly).

---

## 5. LAB evidence

**Method label (everything below): LAB measurement — synthetic payloads / seeded
lake on a 4-core container, NOT production traffic.**

### 5.1 P0 starvation numbers (repro harness)

Real `build_app` + uvicorn over a seeded GIANT symbol (562,500 one-minute bars ≈
6 years); `materialize_lake_timeframes(include_daily=True)` loop for 90 s;
`GET /` + `/api/v1/state` timed every 4 s + an in-server event-loop lag meter.
~1/100th of production scale.

| Metric (90 s GIANT prebuild) | BEFORE | AFTER (run 1 / run 2) |
|---|---|---|
| worst `GET /` latency | **1.227 s** (plus 1.082 s, 0.671 s events) | **0.074 s / 0.028 s** |
| loop-lag events > 50 ms | 89 | 76 / 67 |
| worst event-loop stall | **771 ms** (top-3: 640/701/771) | **287 ms / 171 ms** |
| prebuild passes completed | 2 | 2 (run 2 ended at 5 cumulative) |

Corroborating BEFORE datapoint (diagnosis run, same harness): worst `/` 1.07 s,
worst stall 803 ms. Conversion micro-bench (200k-row frame): **7.64 → 4.75
µs/row (1.61×)**, output `==` equal; the irreducible floor is per-bar
`CandleBar` + `datetime` construction (~2.7–3.1 µs/row), required by the sink's
`Sequence[CandleBar]` interface (sink/writer/models fenced or out of surface).

### 5.2 Byte-identity proof for the data writers (three layers)

1. **Pre/post golden hashes:** the pre-edit code was run over 4 seeded synthetic
   lakes covering the edge shapes (single bar; month boundary + post-18:30-UTC
   IST-date-roll minutes; multi-day multi-month; FNO futures with null-OI rows)
   via the real `materialize_lake_timeframes(include_daily=True)`; SHA-256 of all
   **124** written parquet files captured, then re-run on the edited code:
   **124/124 MATCH, 0 mismatches**.
2. **Permanent regression:**
   `tests/integration/test_timeframe_materialize_roundtrip.py::test_vectorized_conversion_writes_byte_identical_lake`
   (SPOT + FUTURES parametrized) compares the production (batched) lake
   byte-for-byte against a lake written through the SAME sink with bars built by
   the VERBATIM legacy per-row loops (kept in-test as the oracle).
3. **Semantic parity units:** `TestFrameToCandleBars` (7 tests incl. a 2000-value
   seeded-random + adversarial long-tail float sweep — `0.00499999999`,
   `14020.7999`, `.005` half-boundaries — proving `Decimal(polars cast-String) ==
   Decimal(str(x))` bar-for-bar) and `TestResampleVectorizedParity` (5 tests:
   month/year boundary, 18:31-UTC IST-date roll, forming-session cutoff, empty,
   float-noise; utcoffset+isoformat identity of the midnight-IST re-stamp).

### 5.3 Extreme-permutation matrix — 53/53 PASS

Real `build_app` + `InstrumentStore` over a tmp lake + injected `PrebuildGate`
through FastAPI `TestClient` (`raise_server_exceptions=False`). Permutations ×
the six pages: empty lake · mid-pull movers-gate (every non-movers endpoint
ungated; release → ungates) · per-endpoint 500 injection at 12 internal seams
(clean 500, server not wedged, recovers) · 1.2 s slow endpoint with a concurrent
sibling < 1 s · huge-payload caps (`window-ledger?limit=501` → 422,
`leaderboard?top=100000` → 422) · repeat-poll byte-identity (memo-skip
precondition). Verbatim tail: `53 passed in 11.82s`.

### 5.4 Visual + animation evidence (headless Chromium, 55 screenshots)

Before/after shots per page × {normal, empty, error, busy(stalled ~10.8 s)} at
1600×1000 + 2560×1000 wide + a reduced-motion capture; 0 page errors. Notable:
`results-busy` BEFORE = bare "loading…" + lying green dot, AFTER = shimmer
skeleton + amber watchdog caption + neutral dot; `coverage-error` BEFORE = silent
"—" with a GREEN dot under all-500s, AFTER = explicit "error 500 · retrying"
chips + RED dot. Animation audit (computed-style keyframe resolution): **0
violations** of transform/opacity-only across the five adopted pages' entrance
animations; v29 carries 2 PRE-EXISTING SVG stroke animations (`drawLine`,
`donutFill`) present verbatim on origin/main — not introduced here. Skeleton
lifecycle: with all APIs stalled, skeletons present + 0 "Loading…" literals; flip
to fixtures and drive one tick → **0 visible skeletons on all 6 pages**.
Rendered VALUES byte-identical before↔after on identical fixtures.

---

## 6. Honest envelope (Golden Rule #10)

- **No JS execution/coverage harness exists in this repo.** Client-side DOM
  behavior (skeleton→resolve, error-card render, countUp, stagger, memo-skip) is
  pinned as template/asset SOURCE assertions + `node --check` syntax + the LAB
  headless-browser evidence (§5.4, scratchpad harness, not CI). A CI
  headless-browser pass is a named follow-up; the 53/53 matrix executes SERVER
  behavior end-to-end only.
- **Template pins:** every pre-existing test-pinned id/class/string is preserved
  (grep-verified per page); bx-* classes are additive. New ids are listed in the
  per-page records.
- **Byte-equality goldens cover the data writers** (§5.2) — the lake output of
  the vectorized path is proven bit-identical, permanently regression-pinned.
- **Starvation is REDUCED, not eliminated:** only the GIL-share arm shipped. The
  polars thread cap (cli.py) and worker `os.nice` (pull_jobs.py) are fenced
  follow-ups; the true cure is subprocess isolation of the prebuild (§4.10).
  Prebuild wall time did NOT improve (passes/90 s unchanged) — the improvement is
  the GIL-HELD share, which is what starves the event loop. The BEFORE leg is
  n=1 + one corroborating diagnosis run; production (14 saturated cores, ~750
  symbols + F&O contracts, busy external SSD) is NOT reproduced in the LAB
  harness.
- **`/api/v1/state`, `/api/v1/sweep-progress`, `/api/v1/pull/rate`** carry live
  clock/staleness fields, so repeat-poll byte-identity is not their contract —
  v29's heartbeat gates on per-slot signatures instead (source-pinned).
- **Fenced surfaces untouched:** `app.py`, `pull_jobs.py`, `cli.py`, the six
  fenced templates, both widget JS files — zero bytes changed; §4 carries their
  packages. The `gated` state variant exists in `bx-design.css` but no touched
  template can enter it (the gate covers `/api/v1/movers` only — movers is
  fenced).
- Tiny/huge-viewport + reduced-motion cells are CSS-only contracts covered by
  source pins + the LAB captures, not executed responsive tests.
