/*
 * bx-rate-monitor.js — live pull-rate monitor widget
 * (DEC-LIVE-RATE-MONITOR-001; expanded view + req/10min + trust evidence:
 * DEC-RATE-MONITOR-EXPAND-001, MIGRATION.md #1004).
 *
 * A self-contained, dependency-free, self-injecting widget that any dashboard page
 * loads with a single <script defer src="/static/bx-rate-monitor.js"></script>. It polls
 * GET /api/v1/pull/rate every ~3 s and renders, in a fixed-position pill at the bottom-
 * right of the page (the COLLAPSED default — same small footprint as before, so it does
 * NOT reflow or cover any page content):
 *
 *   - a STALL light: green = flowing, red = stalled, amber = local (an ACTIVE pull in a
 *     zero-vendor-call LOCAL pipeline stage — the timeframe prebuild / movers warm,
 *     DEC-PIPELINE-PHASE-UI-HONESTY-001 — never a red "stalled" off the correctly-
 *     flatlined fetch rate), dark = idle (no pull running);
 *   - req/sec  (achieved / allowed, with the 8 r/s ceiling);
 *   - req/min  (rolling-60s issue count, with the 500/min cap);
 *   - req/10min (MEASURED rolling-600s issue count — the new metric);
 *   - req/hour (MEASURED rolling-3600s count, with the older "obs" extrapolation beside it).
 *
 * HONEST achieved (DEC-RATE-WIDGET-FNO-HONEST-001, extended by
 * DEC-RATE-WIDGET-FNO-MEASURED-001): req/sec "achieved" is the MEASURED COMPLETED rate from
 * the transport call-meter. The pull now REGISTERS the client pool's shared meter into the
 * session controller at every build site, so "achieved" reads the real measured completed
 * rate across the SPOT phase AND the metadata + F&O phases (once the pool is built). It falls
 * back to an honest 0/absent only before a meter is registered — e.g. a dry-run or a pre-pool
 * phase — never a fabricated completed-rate; in that fallback case the expanded view instead
 * shows "req/min rate", the controller's ADMISSION-derived per-minute average expressed as r/s
 * (recent_minute / 60), which is openly an ISSUED-calls average, NOT a measured completed
 * throughput. The stall light now reads the MEASURED completed rate wherever a meter is
 * registered (so an issued-but-all-failing F&O pull whose completed rate sits at/below the
 * floor reads STALLED, not a fake-alive "flowing"); only before a meter exists does it fall
 * back to whether ANY calls are being issued (req/min > 0).
 *
 * BIGGER, TRUSTWORTHY view (DEC-RATE-MONITOR-EXPAND-001): clicking the pill (or its
 * "expand" affordance) opens a LARGE fixed-position overlay panel — easy to read at a
 * glance — that ALSO shows the RAW EVIDENCE: the actual integer request counts in the
 * trailing 1s / 60s / 600s / 3600s windows, measured by the rate controller from REAL
 * per-second issue counts. The operator can see the arithmetic add up — e.g. req/sec is
 * literally the trailing-1s count, req/min the trailing-60s count, req/10min the
 * trailing-600s count, req/hour the trailing-3600s count — so the numbers are provably
 * real tracking, not fabricated. The overlay is fixed-position + dismissible (an ✕ button
 * and a click on the backdrop) and is built ONCE (idempotent), so it leaves every page's
 * normal collapsed view unchanged and never disturbs the existing layout.
 *
 * HONEST labelling (Golden Rule #1 + #10): when the backend reports flow_state="idle"
 * (or "done" — the pull just finished, DEC-RATE-STALL-PULL-GATE-001) the widget shows
 * that label + zeroed numbers on the dark light — it NEVER shows a stale "live" figure
 * once a pull has ended, and the backend never labels a finished pull "stalled" (red is
 * reserved for an ACTIVE pull whose req/sec flatlined). req/sec, req/min, req/10min and the MEASURED req/hour are all REAL
 * counts of issues in their window (from the rate controller's per-second issue ring);
 * the older req/hour "obs" figure carries an "obs" tag because it is the current
 * minute-rate projected to an hour, not a measured count. During the first hour of a
 * pull the MEASURED req/hour is the honest partial-hour count SO FAR (it grows as the
 * window fills, never inflated). The 8/sec + 500/min are the configured limits the
 * backend returns.
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
  var OVERLAY_ID = "bxRateMonitorOverlay";

  // Idempotent guard: if two pages somehow both load this, only one widget is built.
  if (typeof document === "undefined" || document.getElementById(WIDGET_ID)) {
    return;
  }

  function injectStyles() {
    if (document.getElementById("bxRateMonitorStyles")) {
      return;
    }
    var css =
      "#" + WIDGET_ID + "{position:fixed;right:16px;bottom:16px;z-index:9000;cursor:pointer;" +
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
      // Amber "local" (DEC-PIPELINE-PHASE-UI-HONESTY-001): an ACTIVE pull in a
      // zero-vendor-call LOCAL stage (prebuild / movers warm) — not stalled, not idle.
      "#" + WIDGET_ID + " .bxrm-dot.local{background:var(--yellow,#FFB74D);" +
      "box-shadow:0 0 7px var(--yellow,#FFB74D);}" +
      "#" + WIDGET_ID + " .bxrm-dot.idle{background:var(--muted,#787B86);}" +
      "#" + WIDGET_ID + " .bxrm-state{margin-left:auto;font-weight:700;}" +
      "#" + WIDGET_ID + " .bxrm-exp{margin-left:6px;color:var(--muted,#787B86);" +
      "font-size:9px;border:1px solid var(--border-strong,#2B313F);border-radius:4px;" +
      "padding:0 4px;}" +
      "#" + WIDGET_ID + " .bxrm-state.flowing{color:var(--green,#26D9B0);}" +
      "#" + WIDGET_ID + " .bxrm-state.stalled{color:var(--red,#FF5C5C);}" +
      "#" + WIDGET_ID + " .bxrm-state.local{color:var(--yellow,#FFB74D);}" +
      "#" + WIDGET_ID + " .bxrm-state.idle{color:var(--muted,#787B86);}" +
      "#" + WIDGET_ID + " .bxrm-row{display:flex;justify-content:space-between;gap:14px;" +
      "line-height:1.7;}" +
      "#" + WIDGET_ID + " .bxrm-row .k{color:var(--muted,#787B86);}" +
      "#" + WIDGET_ID + " .bxrm-row .v{color:var(--text-bright,#FFF);font-weight:600;}" +
      "#" + WIDGET_ID + " .bxrm-row .lim{color:var(--muted,#787B86);font-weight:400;}" +
      "#" + WIDGET_ID + " .bxrm-obs{color:var(--yellow,#FFB74D);font-size:9px;" +
      "vertical-align:super;}" +
      // ── The large expanded overlay (DEC-RATE-MONITOR-EXPAND-001). Fixed-position +
      //    hidden by default → zero impact on the collapsed page layout. ──
      "#" + OVERLAY_ID + "{position:fixed;inset:0;z-index:9100;display:none;" +
      "align-items:center;justify-content:center;background:rgba(0,0,0,.55);" +
      "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}" +
      "#" + OVERLAY_ID + ".open{display:flex;}" +
      "#" + OVERLAY_ID + " .bxrm-card{background:var(--surface-2,#1A1F2C);" +
      "color:var(--text,#E5E8EC);border:1px solid var(--border-strong,#2B313F);" +
      "border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.6);padding:24px 28px;" +
      "min-width:440px;max-width:92vw;max-height:90vh;overflow:auto;}" +
      "#" + OVERLAY_ID + " .bxrm-card .bxrm-ehead{display:flex;align-items:center;gap:10px;" +
      "font-size:13px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;" +
      "color:var(--muted,#787B86);margin-bottom:18px;}" +
      "#" + OVERLAY_ID + " .bxrm-card .bxrm-edot{width:14px;height:14px;border-radius:50%;}" +
      "#" + OVERLAY_ID + " .bxrm-card .bxrm-edot.flowing{background:var(--green,#26D9B0);" +
      "box-shadow:0 0 10px var(--green,#26D9B0);}" +
      "#" + OVERLAY_ID + " .bxrm-card .bxrm-edot.stalled{background:var(--red,#FF5C5C);" +
      "box-shadow:0 0 10px var(--red,#FF5C5C);}" +
      "#" + OVERLAY_ID + " .bxrm-card .bxrm-edot.local{background:var(--yellow,#FFB74D);" +
      "box-shadow:0 0 10px var(--yellow,#FFB74D);}" +
      "#" + OVERLAY_ID + " .bxrm-card .bxrm-edot.idle{background:var(--muted,#787B86);}" +
      "#" + OVERLAY_ID + " .bxrm-card .bxrm-estate{font-weight:800;font-size:15px;}" +
      "#" + OVERLAY_ID + " .bxrm-card .bxrm-estate.flowing{color:var(--green,#26D9B0);}" +
      "#" + OVERLAY_ID + " .bxrm-card .bxrm-estate.stalled{color:var(--red,#FF5C5C);}" +
      "#" + OVERLAY_ID + " .bxrm-card .bxrm-estate.local{color:var(--yellow,#FFB74D);}" +
      "#" + OVERLAY_ID + " .bxrm-card .bxrm-estate.idle{color:var(--muted,#787B86);}" +
      "#" + OVERLAY_ID + " .bxrm-card .bxrm-close{margin-left:auto;cursor:pointer;" +
      "background:none;border:none;color:var(--muted,#787B86);font-size:22px;" +
      "line-height:1;padding:0 4px;}" +
      "#" + OVERLAY_ID + " .bxrm-card .bxrm-close:hover{color:var(--text-bright,#FFF);}" +
      "#" + OVERLAY_ID + " .bxrm-grid{display:grid;grid-template-columns:1fr auto auto;" +
      "gap:8px 22px;align-items:baseline;font-size:18px;}" +
      "#" + OVERLAY_ID + " .bxrm-grid .gk{color:var(--muted,#787B86);font-size:13px;" +
      "text-transform:uppercase;letter-spacing:.04em;}" +
      "#" + OVERLAY_ID + " .bxrm-grid .gv{color:var(--text-bright,#FFF);font-weight:800;" +
      "font-size:24px;text-align:right;}" +
      "#" + OVERLAY_ID + " .bxrm-grid .gc{color:var(--green,#26D9B0);font-size:12px;" +
      "text-align:right;}" +
      "#" + OVERLAY_ID + " .bxrm-evidence{margin-top:20px;padding-top:16px;" +
      "border-top:1px solid var(--border-strong,#2B313F);font-size:12px;" +
      "color:var(--muted,#787B86);line-height:1.7;}" +
      "#" + OVERLAY_ID + " .bxrm-evidence b{color:var(--text-bright,#FFF);}";
    var style = document.createElement("style");
    style.id = "bxRateMonitorStyles";
    style.textContent = css;
    document.head.appendChild(style);
  }

  // DOM builder — programmatic createElement + textContent only (no markup parsing), so the
  // widget carries ZERO HTML-injection sink (CodeQL-clean: every value below is a static
  // string literal set via textContent/className/id, never parsed as markup).
  function el(tag, opts, kids) {
    var node = document.createElement(tag);
    if (opts) {
      if (opts.id) {
        node.id = opts.id;
      }
      if (opts.cls) {
        node.className = opts.cls;
      }
      if (opts.text != null) {
        node.textContent = opts.text;
      }
      if (opts.attrs) {
        for (var k in opts.attrs) {
          if (Object.prototype.hasOwnProperty.call(opts.attrs, k)) {
            node.setAttribute(k, opts.attrs[k]);
          }
        }
      }
    }
    if (kids) {
      for (var i = 0; i < kids.length; i++) {
        node.appendChild(kids[i]);
      }
    }
    return node;
  }

  // A compact pill row: a "k" label + a "v" value span (id valueId). When limId is given a
  // "/ <lim>" suffix is added. Returns the row div.
  function pillRow(label, valueId, limId) {
    var v = el("span", { cls: "v" }, [el("span", { id: valueId, text: "—" })]);
    if (limId) {
      v.appendChild(document.createTextNode(" "));
      v.appendChild(
        el("span", { cls: "lim" }, [
          document.createTextNode("/ "),
          el("span", { id: limId, text: "—" }),
        ])
      );
    }
    return el("div", { cls: "bxrm-row" }, [el("span", { cls: "k", text: label }), v]);
  }

  function buildWidget() {
    var head = el("div", { cls: "bxrm-head" }, [
      el("span", { id: "bxrmDot", cls: "bxrm-dot idle" }),
      el("span", { text: "Pull rate" }),
      el("span", { id: "bxrmState", cls: "bxrm-state idle", text: "idle" }),
      el("span", { cls: "bxrm-exp", text: "expand" }),
    ]);
    var w = el(
      "div",
      {
        id: WIDGET_ID,
        attrs: {
          "aria-live": "polite",
          title: "Live Groww pull rate — click to expand. green flowing, red stalled (active pull flatlined), dark idle/done",
        },
      },
      [
        head,
        pillRow("req/sec", "bxrmSec", "bxrmSecLim"),
        pillRow("req/min", "bxrmMin", "bxrmMinLim"),
        pillRow("req/10min", "bxrmTenMin", null),
        pillRow("req/hour", "bxrmHour", null),
      ]
    );
    w.addEventListener("click", openOverlay);
    document.body.appendChild(w);
    return w;
  }

  // One expanded-grid metric: a "gk" label + a big "gv" value + a small "gc" evidence count.
  function gridMetric(label, valueId, countId) {
    return [
      el("span", { cls: "gk", text: label }),
      el("span", { id: valueId, cls: "gv", text: "—" }),
      el("span", { id: countId, cls: "gc", text: "—" }),
    ];
  }

  function buildOverlay() {
    var closeBtn = el("button", {
      id: "bxrmClose",
      cls: "bxrm-close",
      text: "×",
      attrs: { "aria-label": "Close", type: "button" },
    });
    var ehead = el("div", { cls: "bxrm-ehead" }, [
      el("span", { id: "bxrmEDot", cls: "bxrm-edot idle" }),
      el("span", { text: "Live pull rate" }),
      el("span", { id: "bxrmEState", cls: "bxrm-estate idle", text: "idle" }),
      closeBtn,
    ]);
    var grid = el(
      "div",
      { cls: "bxrm-grid" },
      gridMetric("req/sec", "bxrmESec", "bxrmESecC")
        .concat(gridMetric("req/min rate", "bxrmEMinRate", "bxrmEMinRateC"))
        .concat(gridMetric("req/min", "bxrmEMin", "bxrmEMinC"))
        .concat(gridMetric("req/10min", "bxrmETenMin", "bxrmETenMinC"))
        .concat(gridMetric("req/hour", "bxrmEHour", "bxrmEHourC"))
    );
    var evidence = el("div", {
      id: "bxrmEvidence",
      cls: "bxrm-evidence",
      text:
        "Evidence (real counts from the rate controller's per-second issue ring): " +
        "the rates above ARE these trailing-window issue counts — req/sec = count in the " +
        "last 1s, req/min = last 60s, req/10min = last 600s, req/hour = last 3600s. " +
        "req/sec 'achieved' is the MEASURED completed rate wherever the client pool's meter is " +
        "registered (spot AND metadata/F&O once the pool is built), an honest 0 only before a " +
        "meter exists; 'req/min rate' is the admission per-minute average (issued ÷ 60), NOT a " +
        "completed rate.",
    });
    var card = el("div", { cls: "bxrm-card" }, [ehead, grid, evidence]);
    var o = el("div", { id: OVERLAY_ID }, [card]);
    o.addEventListener("click", function (ev) {
      // Click on the backdrop (outside the card) closes; clicks inside the card do not.
      if (ev.target === o) {
        closeOverlay();
      }
    });
    closeBtn.addEventListener("click", closeOverlay);
    document.body.appendChild(o);
    return o;
  }

  function openOverlay() {
    var o = document.getElementById(OVERLAY_ID);
    if (o) {
      o.classList.add("open");
      if (lastData) {
        render(lastData);
      }
    }
  }

  function closeOverlay() {
    var o = document.getElementById(OVERLAY_ID);
    if (o) {
      o.classList.remove("open");
    }
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

  function fmtCount(n, label) {
    if (typeof n !== "number" || !isFinite(n)) {
      return "—";
    }
    return "= " + Math.round(n) + " in " + label;
  }

  function setClass(el, state) {
    if (!el) {
      return;
    }
    // "idle" and "done" (pull just finished — DEC-RATE-STALL-PULL-GATE-001) both take
    // the dark idle styling; "local" (an active pull in a zero-vendor-call LOCAL stage —
    // DEC-PIPELINE-PHASE-UI-HONESTY-001) takes the AMBER styling; the state TEXT still
    // shows the honest backend word.
    el.classList.remove("flowing", "stalled", "local", "idle");
    el.classList.add(
      state === "flowing" || state === "stalled" || state === "local" ? state : "idle"
    );
  }

  function setText(id, value) {
    var el = document.getElementById(id);
    if (el) {
      el.textContent = value;
    }
  }

  var lastData = null;

  function render(data) {
    lastData = data;
    var state = data && typeof data.flow_state === "string" ? data.flow_state : "idle";
    // flow_detail (DEC-PIPELINE-PHASE-UI-HONESTY-001): non-empty only for the amber
    // "local" verdict — "local processing — <phase> (no vendor calls)". The compact
    // pill keeps the short state token; the expanded overlay shows the full detail.
    var detail =
      data && typeof data.flow_detail === "string" && data.flow_detail
        ? data.flow_detail
        : state;
    setClass(document.getElementById("bxrmDot"), state);
    setClass(document.getElementById("bxrmEDot"), state);
    var stateEl = document.getElementById("bxrmState");
    if (stateEl) {
      stateEl.textContent = state;
      stateEl.title = detail;
      setClass(stateEl, state);
    }
    var eStateEl = document.getElementById("bxrmEState");
    if (eStateEl) {
      eStateEl.textContent = detail;
      setClass(eStateEl, state);
    }

    // ── compact pill ──
    setText(
      "bxrmSec",
      fmtNum(data.req_per_sec_achieved) + " → " + fmtNum(data.req_per_sec_allowed)
    );
    setText("bxrmSecLim", fmtNum(data.ceiling_rps));
    setText("bxrmMin", fmtInt(data.req_per_min));
    // ceiling_per_min < 0 is the "no governor wired" sentinel — show a dash, not "-1".
    setText(
      "bxrmMinLim",
      typeof data.ceiling_per_min === "number" && data.ceiling_per_min >= 0
        ? String(data.ceiling_per_min)
        : "—"
    );
    setText("bxrmTenMin", fmtInt(data.req_per_10min));
    setText("bxrmHour", fmtInt(data.req_per_hour_measured));

    // ── expanded overlay (rates + raw evidence counts) ──
    setText(
      "bxrmESec",
      fmtNum(data.req_per_sec_achieved) + " → " + fmtNum(data.req_per_sec_allowed)
    );
    setText("bxrmESecC", fmtCount(data.req_per_sec_count, "1s"));
    // req/min rate (DEC-RATE-WIDGET-FNO-HONEST-001): the admission per-minute average as r/s
    // (issued ÷ 60) — an ISSUED-calls figure, openly NOT a measured completed rate.
    setText("bxrmEMinRate", fmtNum(data.req_per_min_rate) + " r/s");
    setText("bxrmEMinRateC", "admission avg (issued ÷ 60)");
    setText("bxrmEMin", fmtInt(data.req_per_min));
    setText("bxrmEMinC", fmtCount(data.req_per_min, "60s"));
    setText("bxrmETenMin", fmtInt(data.req_per_10min));
    setText("bxrmETenMinC", fmtCount(data.req_per_10min, "600s"));
    setText("bxrmEHour", fmtInt(data.req_per_hour_measured));
    setText("bxrmEHourC", fmtCount(data.req_per_hour_measured, "3600s"));
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
    buildOverlay();
    poll();
    setInterval(poll, POLL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
