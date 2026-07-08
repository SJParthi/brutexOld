/*!
 * bx-oneclick.js — shared client-side helpers for the P4 one-click sweep
 * display surfaces (DEC-INSTITUTIONAL-ONECLICK-001). Reused IDENTICALLY by
 * templates/results.html (stage 3) and, per the FINAL APPROVED SPEC, by
 * templates/v29.html / templates/strategy.html / templates/verify_me.html
 * in later stages — one place to fix a bug instead of N template copies
 * drifting.
 *
 * XSS HARD RULE (CI-enforced, universal-engineering-standards / CLAUDE.md
 * §11): every helper below builds the DOM via `textContent` / `createElement`
 * / attribute assignment ONLY — no raw-markup DOM-injection sink is ever
 * used. See tests/unit/test_bx_oneclick_static.py and
 * tests/unit/test_dashboard_results_oneclick_xss.py.
 *
 * Namespaced as `window.BXOneclick` (parallel to the existing
 * `window.BXUI` in bx-ui.js — this file does NOT require bx-ui.js to be
 * loaded; it is a fully standalone module so any page can adopt it alone).
 */
(function (global) {
  "use strict";

  // ── esc() / el() — DOM-builder primitives ──────────────────────────────
  // `esc` exists for the rare case a caller needs an escaped string inside
  // an attribute-built string (e.g. a `title` tooltip is set via property
  // assignment below, never via a markup string) — kept for parity with
  // bx-ui.js's helper of the same name so callers can drop either in.
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /**
   * Create an element with an optional class name and optional textContent.
   * NEVER accepts markup — `text` always goes through `.textContent`.
   */
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    if (!node) return;
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // ── fetchJSON — a small timeout-guarded fetch + JSON parse ─────────────
  function fetchJSON(url, opts) {
    var ms = (opts && opts.timeoutMs) || 12000;
    var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = null;
    var fetchOpts = {};
    if (controller) {
      fetchOpts.signal = controller.signal;
      timer = setTimeout(function () { controller.abort(); }, ms);
    }
    return fetch(url, fetchOpts).then(
      function (resp) {
        if (timer) clearTimeout(timer);
        return resp.json().then(function (body) {
          return { ok: resp.ok, status: resp.status, body: body };
        });
      },
      function (err) {
        if (timer) clearTimeout(timer);
        throw err;
      }
    );
  }

  // ── Beginner / Pro mode — persisted localStorage toggle ─────────────────
  // Mirrors v29.html's existing `.jargon` / `.plain-eng` CSS convention
  // (share-mode vs operator-mode there) but is a STANDALONE, cross-page
  // module here: any page can call `BXOneclick.mode.get()` /
  // `BXOneclick.mode.toggle()` and get the SAME persisted choice. Applies
  // two body classes — `bx-mode-beginner` / `bx-mode-pro` — that pages style
  // via CSS (`.jargon` hidden in beginner mode, `.plain-eng` hidden in pro
  // mode — the SAME class names v29.html already uses, so a page can share
  // markup with v29 verbatim).
  var MODE_KEY = "bx-oneclick-mode";
  var modeListeners = [];

  function getMode() {
    try {
      var stored = global.localStorage ? global.localStorage.getItem(MODE_KEY) : null;
      return stored === "pro" ? "pro" : "beginner"; // beginner is the honest, safe default
    } catch (e) {
      return "beginner";
    }
  }

  function applyModeToBody(mode) {
    var body = document.body;
    if (!body) return;
    body.classList.toggle("bx-mode-beginner", mode === "beginner");
    body.classList.toggle("bx-mode-pro", mode === "pro");
  }

  function setMode(mode) {
    var next = mode === "pro" ? "pro" : "beginner";
    try {
      if (global.localStorage) global.localStorage.setItem(MODE_KEY, next);
    } catch (e) { /* private-browsing / quota — mode still applies for this load */ }
    applyModeToBody(next);
    modeListeners.forEach(function (cb) {
      try { cb(next); } catch (e) { /* one bad listener must not break the rest */ }
    });
    return next;
  }

  function toggleMode() {
    return setMode(getMode() === "pro" ? "beginner" : "pro");
  }

  function onModeChange(cb) {
    if (typeof cb === "function") modeListeners.push(cb);
  }

  function initMode() {
    applyModeToBody(getMode());
  }

  // ── hover-any-number-for-plain-English-line affordance ──────────────────
  // R36: "hover ANY number → one plain-English line". A native `title`
  // tooltip (property assignment — never markup) is the same mechanism the
  // repo already uses (v29.html's chain-map chips, the SVG <title> chart
  // plan) — accessible, no custom popup JS/CSS needed, works identically on
  // every browser. `explain()` also marks the element with a `.bx-hoverable`
  // class (dotted-underline affordance, CSS-only) and makes it keyboard
  // reachable so the tooltip is discoverable without a mouse.
  function explain(node, text) {
    if (!node || !text) return node;
    node.title = String(text);
    node.classList.add("bx-hoverable");
    if (node.tabIndex < 0 || node.tabIndex === undefined) node.tabIndex = 0;
    return node;
  }

  //: A small, fixed, hand-written glossary — one line of plain English per
  //: metric key. Purely EDUCATIONAL copy (what the number MEANS), never a
  //: per-row derived claim about a specific combo's numbers (that stays
  //: R27-governed and lives in the caller, built only from verified
  //: numbers) — so this table itself never needs a "no invented text" audit.
  var METRIC_EXPLANATIONS = {
    sharpe: "Return per unit of risk taken — higher means steadier gains for the risk carried.",
    sharpe_ratio: "Return per unit of risk taken — higher means steadier gains for the risk carried.",
    sortino: "Like Sharpe, but only penalizes downside moves — ignores upside volatility.",
    sortino_ratio: "Like Sharpe, but only penalizes downside moves — ignores upside volatility.",
    calmar: "Annual return divided by the worst drawdown — how much pain per unit of gain.",
    calmar_ratio: "Annual return divided by the worst drawdown — how much pain per unit of gain.",
    profit_factor: "Gross profit divided by gross loss — above 1.0 means the strategy nets a winner.",
    win_rate: "The share of trades that closed profitably.",
    win_rate_pct: "The share of trades that closed profitably.",
    max_drawdown_pct: "The biggest peak-to-valley loss the equity curve suffered along the way.",
    recovery_factor: "Net profit divided by the max drawdown — how many drawdowns' worth of profit was made.",
    dsr: "Deflated Sharpe Ratio — the probability this edge is real skill, not a lucky draw from many trials.",
    spa_pvalue: "Superior Predictive Ability p-value — lower means the edge is less likely to be a statistical fluke.",
    cpcv_pass_pct: "Share of Combinatorial Purged Cross-Validation folds this combo passed — real robustness, not one lucky split.",
    net_pnl_paisa: "Total profit or loss across every trade, after costs.",
    avg_trade_pnl_paisa: "Average profit or loss per trade.",
    total_trades: "How many times this exact rule fired and completed a trade.",
    mae: "Maximum Adverse Excursion — the worst the trade's open P&L got before it closed.",
    mfe: "Maximum Favorable Excursion — the best the trade's open P&L got before it closed.",
    vix_entry: "India VIX (volatility index) reading at the moment this trade was entered.",
    vix_exit: "India VIX (volatility index) reading at the moment this trade was exited.",
    trending_pct: "Share of this combo's trading days classified as a trending regime.",
    sideways_pct: "Share of this combo's trading days classified as a sideways (range-bound) regime.",
    volatile_pct: "Share of this combo's trading days classified as a high-volatility regime.",
  };

  /** Attach the shared hover explanation for a known metric key, if any. */
  function explainMetric(node, metricKey) {
    var text = METRIC_EXPLANATIONS[metricKey];
    if (text) explain(node, text);
    return node;
  }

  // ── MAE/MFE granularity marker (TradeRowWire additive keys, R22) ────────
  // sweep-parallel-integration-contracts.md §1: a trade row's `mae`/`mfe`
  // are the 1-MINUTE-path values ONLY when `path_1m_status === "ok"`
  // (equivalently `mae_granularity === "1m"`); otherwise they are the
  // coarser sweep-TF values ("mae_granularity": "tf" — R22 requires the
  // 1-minute path because TF-level/close-only drawdown understates real
  // pain). A row from a fixture/build that predates these additive keys
  // carries neither field — that is treated IDENTICALLY to a known TF
  // fallback (honest-absence: never silently presented as a verified
  // 1-minute value just because the marker is missing).
  function maeGranularityMarker(row) {
    var granularity = row && row.mae_granularity;
    var status = row && row.path_1m_status;
    if (granularity === "1m" && status === "ok") {
      return { suffix: "", title: "MAE/MFE measured from the 1-minute price path.", warn: false };
    }
    var detail = status ? " (path_1m_status=" + String(status) + ")" : "";
    return {
      suffix: " ⚠",
      title:
        "MAE/MFE are TF-level values, NOT the 1-minute path" +
        detail +
        " — R22 requires the 1-minute path; TF-level drawdown understates real pain.",
      warn: true,
    };
  }

  // ── Exit-reason dual-language phrase table (R33) ─────────────────────────
  // A FIXED hand-written lookup (mirrors combo_decoder.py's condition
  // phrase table discipline) — never generated text, so no per-trade
  // hallucination is possible. Covers every exit_reason the fixtures /
  // contract emit: TP, SL, SIGNAL_FLIP, FORCED_1520.
  var EXIT_REASON_LABELS = {
    TP: { pro: "TP", simple: "hit its profit target" },
    SL: { pro: "SL", simple: "hit its stop loss" },
    SIGNAL_FLIP: { pro: "signal-flip", simple: "closed because the signal reversed" },
    FORCED_1520: { pro: "forced 15:20", simple: "closed automatically at the 3:20pm cutoff" },
  };

  function exitReasonLabel(code, mode) {
    var entry = EXIT_REASON_LABELS[code];
    if (!entry) return String(code == null ? "—" : code);
    return mode === "pro" ? entry.pro : entry.simple;
  }

  // ── Virtual list — windowed rendering over a large-but-bounded array ────
  // R26: "ALL N rows present, instant, virtual-scroll render" — the FULL
  // `items` array always lives in memory (nothing is dropped/paged away),
  // but only the rows intersecting the scroll viewport (+ a small overscan
  // buffer) are ever real DOM nodes at once. Fixed row height (simplest
  // correct implementation — every row the callers render is a uniform
  // list-row shape).
  function virtualList(scrollEl, opts) {
    var rowHeight = opts.rowHeight;
    var renderRow = opts.renderRow;
    var overscan = opts.overscan || 6;
    var items = opts.items || [];
    var spacer = el("div", "bx-vlist-spacer");
    spacer.style.position = "relative";
    clear(scrollEl);
    scrollEl.appendChild(spacer);
    var pool = {}; // index (string key) -> rendered element

    function render() {
      var scrollTop = scrollEl.scrollTop;
      var viewportH = scrollEl.clientHeight || 1;
      var first = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
      var last = Math.min(
        items.length - 1,
        Math.ceil((scrollTop + viewportH) / rowHeight) + overscan
      );
      Object.keys(pool).forEach(function (key) {
        var idx = Number(key);
        if (idx < first || idx > last || idx > items.length - 1) {
          var stale = pool[key];
          if (stale && stale.parentNode === spacer) spacer.removeChild(stale);
          delete pool[key];
        }
      });
      for (var i = first; i <= last; i++) {
        if (Object.prototype.hasOwnProperty.call(pool, String(i))) continue;
        var rowEl = renderRow(items[i], i);
        if (!rowEl) continue;
        rowEl.style.position = "absolute";
        rowEl.style.top = i * rowHeight + "px";
        rowEl.style.left = "0";
        rowEl.style.right = "0";
        spacer.appendChild(rowEl);
        pool[String(i)] = rowEl;
      }
    }

    function setItems(nextItems) {
      items = nextItems || [];
      spacer.style.height = items.length * rowHeight + "px";
      pool = {};
      clear(spacer);
      render();
    }

    scrollEl.addEventListener("scroll", render);
    if (items.length) setItems(items);
    return { setItems: setItems, refresh: render };
  }

  // ── safe stringify for opaque JSON payloads (never a markup sink) ───────
  function safeJson(v) {
    try { return JSON.stringify(v); } catch (e) { return String(v); }
  }

  global.BXOneclick = {
    esc: esc,
    el: el,
    clear: clear,
    fetchJSON: fetchJSON,
    mode: {
      get: getMode,
      set: setMode,
      toggle: toggleMode,
      onChange: onModeChange,
      init: initMode,
    },
    explain: explain,
    explainMetric: explainMetric,
    METRIC_EXPLANATIONS: METRIC_EXPLANATIONS,
    exitReasonLabel: exitReasonLabel,
    EXIT_REASON_LABELS: EXIT_REASON_LABELS,
    maeGranularityMarker: maeGranularityMarker,
    virtualList: virtualList,
    safeJson: safeJson,
  };
})(window);
