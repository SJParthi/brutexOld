# Breaker B repro harnesses (handed off 2026-07-09)
Real-Chromium (Playwright, /opt/pw-browsers/chromium*/chrome; flags --enable-precise-memory-info --js-flags=--expose-gc --no-sandbox) repros for two frontend leaks, loading the REAL brutex/dashboard/static JS verbatim via page.route interception.

## Finding 5 — bx-oneclick.js ringSeen unbounded (n3_ringseen.js)
ringAdd (bx-oneclick.js ~:747-760) inserts every event's canonical dedupe line into state.ringSeen (:157) BEFORE the 5000-cap ring trim; trimmed keys never evicted. Measured: 1,005,000 unique events → heap 1.46→148.32 MB (+146.9 MB, linear through forced GC, 153.6 B/event second-half slope); control (repeating ids, same poll count) +0.06 MB flat. Driver = tail poller; /screener + /strategy accumulate invisibly (no #ocTimeline). Run: node n3_ringseen.js growth 5025 ; node n3_ringseen.js control 2513. Fix class: evict ringSeen keys when bxRingPush trims, or bound ringSeen.

## Finding 7 — bx-datepicker.js permanent document listeners (n5_datepicker.js)
Each wireLooseInput→buildCalendar adds TWO never-removed document listeners (outside-click :145, Escape :146); db.html renderCandleView innerHTML-rebuilds #cstart/#cend per interaction; old listeners + closures retain detached wrap/trig/pop subtrees. Measured: 1,000 rebuilds → getEventListeners(document)=4,001 (+4/rebuild); DOM nodes 128→103,025 (+103/rebuild through forced GC); ≈50.9 KB/rebuild whole-renderer; document click dispatch 0.20→4.60 ms. Control (data-dp-done="1" pre-marked, wiring skipped) fully flat — innerHTML churn itself is GC-clean. Run: node n5_datepicker.js leak 1000 ; node n5_datepicker.js control 1000. Fix class: delegated/single document listeners OR remove old inputs' listeners on rebuild.

Post-fix expectation: growth/leak modes go flat (match control); dedup correctness for live ring entries preserved; datepicker behavior identical.
NOTE: harness executablePath is hardcoded to chromium-1194 — adjust to the local /opt/pw-browsers path. Needs a package.json with playwright installed (npm i playwright works against the preinstalled browser with PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1).
