/* Round-3 Breaker-B: REAL-Chromium validation of finding N5 —
 * bx-datepicker.js permanent document listeners + detached-subtree retention
 * per /db control-bar rebuild.
 *
 * Loads the REAL /static/bx-datepicker.js verbatim. The harness page carries
 * a #data container and a rebuild() that mirrors db.html's renderCandleView
 * (db.html:1458-1459): `dataEl.innerHTML = head + candleControlsHtml() +
 * '<div id="dbgrid">…'` — candleControlsHtml's qbar snippet (db.html:1294-1300,
 * the From/To `<input type="date" id="cstart|cend">` rows) is copied VERBATIM
 * with a minimal cstate/esc stub. The real MutationObserver in bx-datepicker
 * (bx-datepicker.js:195-200) then re-wires each fresh input → buildCalendar →
 * 2 document.addEventListener per input (:145-146), never removed.
 *
 * MODE leak   : plain inputs (production shape) — each rebuild re-wires.
 * MODE control: inputs pre-marked data-dp-done="1" so wireLooseInput skips
 *               (bx-datepicker.js:159) — identical innerHTML churn, zero
 *               datepicker wiring. Isolates the datepicker's own retention.
 *
 * Rebuilds are spaced by ≥2 macrotasks so the observer's setTimeout(0) auto()
 * runs per rebuild — modelling production, where each rebuild is a discrete
 * operator interaction (dataset/tab/cascade switch, error/prompt render).
 *
 * Metrics per checkpoint:
 *  - CDP DOMDebugger.getEventListeners(document) — REAL listener count;
 *  - CDP Memory.getDOMCounters — live+retained DOM nodes / jsEventListeners;
 *  - window.gc()×3 then performance.memory.usedJSHeapSize.
 */
"use strict";
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const STATIC_DIR = "/home/user/bruteX/brutex/dashboard/static";
const MODE = process.argv[2] || "leak"; // leak | control
const REBUILDS = parseInt(process.argv[3] || "500", 10);
const SAMPLE_EVERY = 100;

const HARNESS_HTML = `<!doctype html><html><head><meta charset="utf-8">
<script src="/static/bx-datepicker.js"></script>
<script>
"use strict";
/* ── verbatim-shaped copies from templates/db.html ── */
function esc(s) {                                    /* db.html:401 */
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
var cstate = { kind: "spot0", datasets: [], key: null, start: "", end: "", desc: false };
var DP_DONE = ${MODE === "control" ? "' data-dp-done=\"1\"'" : "\"\""}; // control-mode switch ONLY
/* db.html:1269-1300 candleControlsHtml — flat-dataset arm; the qbar From/To
   date-input rows are byte-identical apart from the control-mode marker. */
function candleControlsHtml() {
  var dsOpts = '<option value="x">x</option>';
  var picker = '<span class="lab">Dataset</span><select id="cds">' + dsOpts + '</select>';
  return '<div class="qbar">'
    + '<div class="qrow">' + picker
    + '<span class="lab">From</span><input type="date" id="cstart" min="2020-01-01"' + DP_DONE + ' value="' + esc(cstate.start) + '">'
    + '<span class="lab">To</span><input type="date" id="cend" min="2020-01-01"' + DP_DONE + ' value="' + esc(cstate.end) + '"></div>'
    + '<div class="qrow"><span class="lab">Sort</span>'
    + '<select id="cdir"><option value="asc">timestamp asc</option><option value="desc">timestamp desc</option></select></div>'
    + '</div>';
}
function candleHeadHtml() {                          /* db.html:1411 shape */
  return '<div class="datahead"><span class="dt">📈 candles</span><span class="sub">0 bars match</span></div>';
}
var dataEl = null;
var rebuilds = 0;
function rebuild() {                                 /* db.html:1458-1460 shape */
  dataEl.innerHTML = candleHeadHtml() + candleControlsHtml()
    + '<div id="dbgrid"><div class="empty">pick a symbol → interval</div></div>';
  rebuilds += 1;
}
window.addEventListener("DOMContentLoaded", function () {
  dataEl = document.getElementById("data");
});
</script>
</head><body><div id="data"></div></body></html>`;

(async () => {
  const browser = await chromium.launch({
    executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    headless: true,
    args: ["--enable-precise-memory-info", "--js-flags=--expose-gc", "--no-sandbox"]
  });
  const page = await browser.newPage();
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    if (p === "/harness.html") return route.fulfill({ contentType: "text/html", body: HARNESS_HTML });
    if (p.startsWith("/static/")) {
      const f = path.join(STATIC_DIR, path.basename(p));
      if (fs.existsSync(f)) return route.fulfill({ contentType: "application/javascript", body: fs.readFileSync(f, "utf8") });
    }
    return route.fulfill({ status: 404, body: "unstubbed " + p });
  });
  await page.goto("http://bx.harness/harness.html");
  const cdp = await page.context().newCDPSession(page);

  async function docListenerCount() {
    const { result } = await cdp.send("Runtime.evaluate", { expression: "document", objectGroup: "n5" });
    const { listeners } = await cdp.send("DOMDebugger.getEventListeners", { objectId: result.objectId });
    const byType = {};
    for (const l of listeners) byType[l.type] = (byType[l.type] || 0) + 1;
    await cdp.send("Runtime.releaseObjectGroup", { objectGroup: "n5" });
    return { total: listeners.length, byType };
  }
  async function domCounters() {
    await page.evaluate(() => { window.gc(); window.gc(); window.gc(); });
    return await cdp.send("Memory.getDOMCounters"); // {documents, nodes, jsEventListeners}
  }
  async function heapMB() {
    return await page.evaluate(() => { window.gc(); window.gc(); window.gc(); return performance.memory.usedJSHeapSize / 1048576; });
  }
  // Renderer-process RSS (KB): the detached DOM subtrees live mostly in Blink
  // (C++) memory that performance.memory does NOT count. One browser instance
  // runs at a time → scan /proc for our chromium's --type=renderer processes.
  function rendererRssKB() {
    let total = 0;
    for (const pid of fs.readdirSync("/proc").filter((d) => /^\d+$/.test(d))) {
      try {
        const cmd = fs.readFileSync(`/proc/${pid}/cmdline`, "utf8");
        if (!cmd.includes("--type=renderer") || !cmd.includes("chrome")) continue;
        const m = fs.readFileSync(`/proc/${pid}/status`, "utf8").match(/VmRSS:\s+(\d+) kB/);
        if (m) total += parseInt(m[1], 10);
      } catch (_) { /* raced exit */ }
    }
    return total;
  }

  // First rebuild + settle → the "1-rebuild" baseline (observer wired once).
  await page.evaluate(() => rebuild());
  await page.waitForTimeout(50);
  const base = { listeners: await docListenerCount(), dom: await domCounters(), heap: await heapMB(), rssKB: rendererRssKB() };
  console.log(`mode=${MODE} after 1 rebuild: docListeners=${base.listeners.total} ${JSON.stringify(base.listeners.byType)} nodes=${base.dom.nodes} jsListeners=${base.dom.jsEventListeners} heap=${base.heap.toFixed(2)}MB rss=${(base.rssKB/1024).toFixed(1)}MB`);

  const samples = [];
  for (let i = 1; i < REBUILDS; i++) {
    await page.evaluate(() => rebuild());
    // ≥2 macrotasks so the MutationObserver's setTimeout(0) auto() runs per
    // rebuild (production: rebuilds are discrete interactions).
    await page.evaluate(() => new Promise((r) => setTimeout(r, 0)));
    await page.evaluate(() => new Promise((r) => setTimeout(r, 0)));
    if ((i + 1) % SAMPLE_EVERY === 0) {
      const s = { rebuilds: i + 1, listeners: await docListenerCount(), dom: await domCounters(), heap: await heapMB(), rssKB: rendererRssKB() };
      samples.push(s);
      console.log(`  rebuilds=${s.rebuilds} docListeners=${s.listeners.total} ${JSON.stringify(s.listeners.byType)} nodes=${s.dom.nodes} jsListeners=${s.dom.jsEventListeners} heap=${s.heap.toFixed(2)}MB rss=${(s.rssKB/1024).toFixed(1)}MB`);
    }
  }
  const fin = { rebuilds: REBUILDS, listeners: await docListenerCount(), dom: await domCounters(), heap: await heapMB(), rssKB: rendererRssKB() };
  const dHeapKB = ((fin.heap - base.heap) * 1024) / Math.max(1, REBUILDS - 1);
  const dList = (fin.listeners.total - base.listeners.total) / Math.max(1, REBUILDS - 1);
  const dNodes = (fin.dom.nodes - base.dom.nodes) / Math.max(1, REBUILDS - 1);
  console.log(`FINAL mode=${MODE} rebuilds=${REBUILDS} docListeners=${fin.listeners.total} ${JSON.stringify(fin.listeners.byType)} nodes=${fin.dom.nodes} jsListeners=${fin.dom.jsEventListeners} heap=${fin.heap.toFixed(2)}MB`);
  const dRssKB = (fin.rssKB - base.rssKB) / Math.max(1, REBUILDS - 1);
  console.log(`per-rebuild: +${dList.toFixed(2)} document listeners, +${dNodes.toFixed(1)} retained DOM nodes, +${dHeapKB.toFixed(2)} KB JS heap, +${dRssKB.toFixed(2)} KB renderer RSS`);
  fs.writeFileSync(path.join(__dirname, `n5_result_${MODE}.json`), JSON.stringify({ mode: MODE, base, samples, fin }, null, 2));

  // Interactivity-cost probe (leak mode): time ONE document click dispatch —
  // every leaked click closure runs on every click.
  const clickMs = await page.evaluate(() => {
    const t0 = performance.now();
    document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    return performance.now() - t0;
  });
  console.log(`one document click dispatch: ${clickMs.toFixed(2)} ms with ${fin.listeners.total} document listeners`);
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
