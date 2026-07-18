/* Round-3 Breaker-B: REAL-Chromium validation of finding N3 —
 * state.ringSeen dedup index growth in brutex/dashboard/static/bx-oneclick.js.
 *
 * Loads the REAL static JS files (bx-ui.js, bx-poll.js, bx-explain.js,
 * bx-oneclick.js) verbatim from the repo (read-only) into a headless-Chromium
 * page shaped like /screener (ticker strip present, NO #ocTimeline host — the
 * "invisible accumulation" claim). All /api/v1/* endpoints are stubbed via
 * page.route(). The ONLY harness knob: BX_POLL_DEFAULTS.POLL_MS is lowered
 * (5 ms) via an inline script between bx-poll.js and bx-oneclick.js so the
 * REAL manifest-poller → pollEvents → ringAdd path runs fast enough to feed
 * 100k+ events in minutes. Nothing in bx-oneclick.js is altered.
 *
 * MODE growth : every events response carries EVENTS_PAGE_LIMIT=200 records
 *               with globally UNIQUE (ts_utc, payload) — models a live sweep
 *               streaming new events.
 * MODE control: every events response carries the SAME 200 records — the
 *               dedup path runs identically (ringAdd called each poll) but
 *               ringSeen stays at 200 keys. Heap(growth) − Heap(control)
 *               isolates the ringSeen (+ring-churn) cost from poll machinery.
 *
 * Metrics: window.gc() (--js-flags=--expose-gc) then
 * performance.memory.usedJSHeapSize (--enable-precise-memory-info), sampled
 * every SAMPLE_PAGES event pages.
 */
"use strict";
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const STATIC_DIR = "/home/user/bruteX/brutex/dashboard/static";
const MODE = process.argv[2] || "growth"; // growth | control
const TARGET_PAGES = parseInt(process.argv[3] || "500", 10); // pages of 200 events
const PAGE_LIMIT = 200; // mirrors EVENTS_PAGE_LIMIT in bx-oneclick.js
const SAMPLE_EVERY = 50; // sample heap every N pages

// Realistic event shape (oneclick events.jsonl contract-ish): ts_utc + kind +
// payload; the canonical dedupe key is bxEventLineText(rec) =
// ts + "  " + kind + "  " + JSON.stringify(payload).
function makeRecords(startSeq, unique) {
  const recs = [];
  for (let i = 0; i < PAGE_LIMIT; i++) {
    const seq = unique ? startSeq + i : i; // control: same 200 forever
    recs.push({
      ts_utc: "2026-07-08T10:" + String(Math.floor(seq / 60) % 60).padStart(2, "0") + ":" +
        String(seq % 60).padStart(2, "0") + "Z",
      run_id: "r-1",
      kind: "progress",
      payload: { seq: seq, tuples_done: seq * 17, stage: "stage3", note: "combo batch " + seq }
    });
  }
  return recs;
}

const HARNESS_HTML = `<!doctype html><html><head><meta charset="utf-8">
<script src="/static/bx-ui.js"></script>
<script src="/static/bx-poll.js"></script>
<script>
  // Harness knob ONLY: accelerate the poll cadence. BX_POLL_DEFAULTS is the
  // real global the real createPoller folds at poller creation; nothing else
  // (fetch timeout, backoff, code) is touched.
  BX_POLL_DEFAULTS.POLL_MS = 5;
</script>
<script src="/static/bx-explain.js"></script>
<script src="/static/bx-oneclick.js"></script>
</head><body>
<!-- /screener-shaped: ticker strip only; NO #ocTimeline / #ocRunSelect host -->
<div id="bxTickerStrip"></div>
</body></html>`;

(async () => {
  const browser = await chromium.launch({
    executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    headless: true,
    args: ["--enable-precise-memory-info", "--js-flags=--expose-gc", "--no-sandbox"]
  });
  const page = await browser.newPage();

  let eventsServed = 0;
  let eventPagesServed = 0;
  let nextSeq = 0;

  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    if (p === "/harness.html") {
      return route.fulfill({ contentType: "text/html", body: HARNESS_HTML });
    }
    if (p.startsWith("/static/")) {
      const f = path.join(STATIC_DIR, path.basename(p));
      if (fs.existsSync(f)) {
        return route.fulfill({ contentType: "application/javascript", body: fs.readFileSync(f, "utf8") });
      }
      return route.fulfill({ status: 404, body: "nope" });
    }
    if (p === "/api/v1/sweep-progress") {
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ running: true, done: 1, total: 100 }) });
    }
    if (p === "/api/v1/oneclick/runs") {
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ latest_run_id: "r-1", runs: [{ run_id: "r-1" }] }) });
    }
    if (p === "/api/v1/oneclick/manifest") {
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ status: "awaiting", run_id: "r-1" }) });
    }
    if (p === "/api/v1/oneclick/events") {
      const unique = MODE === "growth";
      const recs = makeRecords(nextSeq, unique);
      if (unique) nextSeq += PAGE_LIMIT;
      eventsServed += recs.length;
      eventPagesServed += 1;
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ records: recs, next_offset: nextSeq })
      });
    }
    if (p === "/api/v1/oneclick/top100") {
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ status: "awaiting", rows: [] }) });
    }
    if (p === "/api/v1/oneclick/trades") {
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ status: "awaiting", rows: [], total: 0 }) });
    }
    if (p === "/api/v1/oneclick/verify") {
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ checks: [] }) });
    }
    return route.fulfill({ status: 404, body: "unstubbed " + p });
  });

  await page.goto("http://bx.harness/harness.html");

  const vis = await page.evaluate(() => ({
    hidden: document.hidden, vs: document.visibilityState,
    hasGc: typeof window.gc === "function",
    hasMem: !!(performance.memory && performance.memory.usedJSHeapSize),
    initFlag: !!window.__bxOneclickInit
  }));
  console.log("env:", JSON.stringify(vis));
  if (vis.hidden) { console.error("FATAL: page hidden — poller will not fetch"); process.exit(2); }

  async function heapMB() {
    // 3 GC passes for stability, then precise heap.
    return await page.evaluate(() => {
      window.gc(); window.gc(); window.gc();
      return performance.memory.usedJSHeapSize / 1048576;
    });
  }

  // Baseline AFTER the pollers have started and the first events page landed
  // (so poller/JSON machinery is warm in both modes).
  await page.waitForFunction(() => !!window.__bxOneclickInit);
  const t0 = Date.now();
  while (eventPagesServed < 2) await new Promise((r) => setTimeout(r, 20));
  const base = await heapMB();
  const basePages = eventPagesServed;
  console.log(`mode=${MODE} baseline after ${basePages} pages (${basePages * PAGE_LIMIT} events): ${base.toFixed(2)} MB`);

  const samples = [];
  let lastSampled = basePages;
  while (eventPagesServed < TARGET_PAGES) {
    await new Promise((r) => setTimeout(r, 25));
    if (eventPagesServed - lastSampled >= SAMPLE_EVERY) {
      lastSampled = eventPagesServed;
      const h = await heapMB();
      samples.push({ pages: eventPagesServed, events: eventPagesServed * PAGE_LIMIT, heapMB: h });
      console.log(`  pages=${eventPagesServed} events=${eventPagesServed * PAGE_LIMIT} heap=${h.toFixed(2)} MB (+${(h - base).toFixed(2)})`);
    }
  }
  // settle + final sample
  await new Promise((r) => setTimeout(r, 200));
  const hEnd = await heapMB();
  const secs = (Date.now() - t0) / 1000;
  const events = eventPagesServed * PAGE_LIMIT;
  console.log(`FINAL mode=${MODE} pages=${eventPagesServed} events=${events} heap=${hEnd.toFixed(2)} MB delta=${(hEnd - base).toFixed(2)} MB wall=${secs.toFixed(1)}s`);
  const uniqueKeys = MODE === "growth" ? events - basePages * PAGE_LIMIT : PAGE_LIMIT;
  console.log(`per-key (naive, growth-only meaningful): ${(((hEnd - base) * 1048576) / Math.max(1, uniqueKeys)).toFixed(1)} B over ${uniqueKeys} keys`);

  fs.writeFileSync(
    path.join(__dirname, `n3_result_${MODE}.json`),
    JSON.stringify({ mode: MODE, base, samples, final: { pages: eventPagesServed, events, heapMB: hEnd }, wallSecs: secs }, null, 2)
  );
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
