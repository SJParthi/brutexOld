/*
 * bx-rate-monitor.js — live pull-rate monitor widget (DEC-LIVE-RATE-MONITOR-001).
 *
 * A self-contained, dependency-free, self-injecting widget that any dashboard page
 * loads with a single <script defer src="/static/bx-rate-monitor.js"></script>. It polls
 * GET /api/v1/pull/rate every ~3 s and renders, in a fixed-position pill at the bottom-
 * right of the page:
 *
 *   - a STALL light: green = flowing, red = stalled, dark = idle (no pull running);
 *   - req/sec  (achieved / allowed, with the 8 r/s ceiling);
 *   - req/min  (rolling-60s issue count, with the 500/min cap);
 *   - req/hour (an OBSERVED extrapolation = req/min × 60 — labelled "obs").
 *
 * HONEST labelling (Golden Rule #1 + #10): when the backend reports flow_state="idle"
 * the widget shows "idle" + zeroed numbers — it NEVER shows a stale "live" figure once a
 * pull has ended. req/sec and req/min are MEASURED from the rate controller / call meter;
 * req/hour carries an "obs" tag because it is the current minute-rate projected to an
 * hour, not a measured trailing-hour count. The 8/sec + 500/min are the configured
 * limits the backend returns.
 *
 * The whole widget is best-effort: a fetch error / malformed payload leaves the last
 * render in place (or an honest "—") and never throws into the page. READ-ONLY — it
 * changes no pacing decision and is purely observational.
 */
(function () {
  "use strict";

  var ENDPOINT = "/api/v1/pull/rate";
  var POLL_MS = 3000;
  var WIDGET_ID = "bxRateMonitor";

  // Idempotent guard: if two pages somehow both load this, only one widget is built.
  if (typeof document === "undefined" || document.getElementById(WIDGET_ID)) {
    return;
  }

  function injectStyles() {
    if (document.getElementById("bxRateMonitorStyles")) {
      return;
    }
    var css =
      "#" + WIDGET_ID + "{position:fixed;right:16px;bottom:16px;z-index:9000;" +
      "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;" +
      "background:var(--surface-2,#1A1F2C);color:var(--text,#E5E8EC);" +
      "border:1px solid var(--border-strong,#2B313F);border-radius:var(--r-md,10px);" +
      "padding:8px 12px;box-shadow:0 6px 20px rgba(0,0,0,.45);min-width:208px;" +
      "opacity:.96;transition:opacity .15s ease;}" +
      "#" + WIDGET_ID + ":hover{opacity:1;}" +
      "#" + WIDGET_ID + " .bxrm-head{display:flex;align-items:center;gap:7px;" +
      "font-weight:700;letter-spacing:.04em;text-transform:uppercase;font-size:10px;" +
      "color:var(--muted,#787B86);margin-bottom:6px;}" +
      "#" + WIDGET_ID + " .bxrm-dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto;" +
      "background:var(--muted,#787B86);box-shadow:0 0 0 0 transparent;}" +
      "#" + WIDGET_ID + " .bxrm-dot.flowing{background:var(--green,#26D9B0);" +
      "box-shadow:0 0 7px var(--green,#26D9B0);}" +
      "#" + WIDGET_ID + " .bxrm-dot.stalled{background:var(--red,#FF5C5C);" +
      "box-shadow:0 0 7px var(--red,#FF5C5C);}" +
      "#" + WIDGET_ID + " .bxrm-dot.idle{background:var(--muted,#787B86);}" +
      "#" + WIDGET_ID + " .bxrm-state{margin-left:auto;font-weight:700;}" +
      "#" + WIDGET_ID + " .bxrm-state.flowing{color:var(--green,#26D9B0);}" +
      "#" + WIDGET_ID + " .bxrm-state.stalled{color:var(--red,#FF5C5C);}" +
      "#" + WIDGET_ID + " .bxrm-state.idle{color:var(--muted,#787B86);}" +
      "#" + WIDGET_ID + " .bxrm-row{display:flex;justify-content:space-between;gap:14px;" +
      "line-height:1.7;}" +
      "#" + WIDGET_ID + " .bxrm-row .k{color:var(--muted,#787B86);}" +
      "#" + WIDGET_ID + " .bxrm-row .v{color:var(--text-bright,#FFF);font-weight:600;}" +
      "#" + WIDGET_ID + " .bxrm-row .lim{color:var(--muted,#787B86);font-weight:400;}" +
      "#" + WIDGET_ID + " .bxrm-obs{color:var(--yellow,#FFB74D);font-size:9px;" +
      "vertical-align:super;}";
    var style = document.createElement("style");
    style.id = "bxRateMonitorStyles";
    style.textContent = css;
    document.head.appendChild(style);
  }

  function buildWidget() {
    var w = document.createElement("div");
    w.id = WIDGET_ID;
    w.setAttribute("aria-live", "polite");
    w.setAttribute("title", "Live Groww pull rate — green flowing, red stalled, dark idle");
    w.innerHTML =
      '<div class="bxrm-head">' +
      '<span class="bxrm-dot idle" id="bxrmDot"></span>' +
      "<span>Pull rate</span>" +
      '<span class="bxrm-state idle" id="bxrmState">idle</span>' +
      "</div>" +
      '<div class="bxrm-row"><span class="k">req/sec</span>' +
      '<span class="v"><span id="bxrmSec">—</span> ' +
      '<span class="lim">/ <span id="bxrmSecLim">—</span></span></span></div>' +
      '<div class="bxrm-row"><span class="k">req/min</span>' +
      '<span class="v"><span id="bxrmMin">—</span> ' +
      '<span class="lim">/ <span id="bxrmMinLim">—</span></span></span></div>' +
      '<div class="bxrm-row"><span class="k">req/hour<span class="bxrm-obs">obs</span></span>' +
      '<span class="v" id="bxrmHour">—</span></div>';
    document.body.appendChild(w);
    return w;
  }

  function fmtNum(n) {
    if (typeof n !== "number" || !isFinite(n)) {
      return "—";
    }
    return Number.isInteger(n) ? String(n) : n.toFixed(2);
  }

  function fmtInt(n) {
    if (typeof n !== "number" || !isFinite(n)) {
      return "—";
    }
    return String(Math.round(n));
  }

  function setClass(el, state) {
    if (!el) {
      return;
    }
    el.classList.remove("flowing", "stalled", "idle");
    el.classList.add(state === "flowing" || state === "stalled" ? state : "idle");
  }

  function render(data) {
    var state = data && typeof data.flow_state === "string" ? data.flow_state : "idle";
    setClass(document.getElementById("bxrmDot"), state);
    var stateEl = document.getElementById("bxrmState");
    if (stateEl) {
      stateEl.textContent = state;
      setClass(stateEl, state);
    }
    // achieved / allowed req/sec
    var secEl = document.getElementById("bxrmSec");
    if (secEl) {
      secEl.textContent =
        fmtNum(data.req_per_sec_achieved) + " → " + fmtNum(data.req_per_sec_allowed);
    }
    var secLim = document.getElementById("bxrmSecLim");
    if (secLim) {
      secLim.textContent = fmtNum(data.ceiling_rps);
    }
    var minEl = document.getElementById("bxrmMin");
    if (minEl) {
      minEl.textContent = fmtInt(data.req_per_min);
    }
    var minLim = document.getElementById("bxrmMinLim");
    if (minLim) {
      // ceiling_per_min < 0 is the "no governor wired" sentinel — show a dash, not "-1".
      minLim.textContent =
        typeof data.ceiling_per_min === "number" && data.ceiling_per_min >= 0
          ? String(data.ceiling_per_min)
          : "—";
    }
    var hourEl = document.getElementById("bxrmHour");
    if (hourEl) {
      hourEl.textContent = fmtInt(data.req_per_hour_observed);
    }
  }

  function poll() {
    fetch(ENDPOINT, { headers: { Accept: "application/json" } })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (data) {
          render(data);
        }
      })
      .catch(function () {
        /* best-effort — leave the last render in place on a transient error. */
      });
  }

  function start() {
    injectStyles();
    buildWidget();
    poll();
    setInterval(poll, POLL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
