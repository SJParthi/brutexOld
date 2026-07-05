/* ═══════════════════════════════════════════════════════════════════════════
   BruteX shared UI helpers — bx-ui.js (DEC-DASHBOARD-UX-ANIMATE-001)

   Tiny, framework-free, no build step. Pairs with static/bx-design.css.
   Idempotent + injection-safe: loading this file twice is a no-op.

   DOM DISCIPLINE (CodeQL guard): this file builds DOM via createElement +
   textContent ONLY. It contains ZERO string-to-DOM sinks of any kind, so no
   vendor/server-derived string can ever be parsed as markup through it.

   THE LAW this file enforces: "Loading…" is BANNED as a terminal state.
   Every async region shows a shimmer skeleton while pending, and every fetch
   goes through fetchWithTimeout() so a dead/hung endpoint resolves to an
   honest .bx-state error card with a Retry button — never a stuck literal.

   API (window.BXUI):
     esc(s)                        HTML-escape (for pages keeping legacy
                                   escape-then-markup render paths).
     num(n)                        en-IN thousands grouping (matches the
                                   per-page num() copies verbatim).
     el(tag, cls, text)            createElement + className + textContent.
     clear(node)                   remove all children (no markup parsing).
     fetchWithTimeout(url, ms, o)  fetch with an AbortController deadline
                                   (default 12000ms). Timeout rejects with an
                                   Error{name:"TimeoutError", timeout:true}.
     skeleton(panel, rows, cols, o)   shimmer ghost grid; optional honesty
                                   watchdog {watchdogMs, watchdogText} appends
                                   a caption if STILL pending after N ms.
     stateCard(panel, variant, o)  generic .bx-state block
                                   (variant: empty|error|offline|gated|busy;
                                   o: {icon,title,hint,retry,retryText,
                                       ctaText,ctaHref(app-relative only)}).
     errorCard(panel, o)           .bx-state.error/.offline with status +
                                   Retry ({status,offline,icon,title,hint,
                                   retry,retryText}).
     emptyState(panel, o)          .bx-state.empty (honest empty copy).
     setDotState(dot, state)       flips live-ok|live-warn|live-down classes —
                                   the truthful heartbeat dot driver.
     countUp(elm, finalText, o)    CHANGE-ONLY KPI settle: writes the FINAL
                                   exact string synchronously via textContent,
                                   then a transform-only .bx-countpop pop
                                   (value-pin safe — DEC-SURFACE-TRIAL-001).
                                   Opt-in {tween:true} plays the welcome.html
                                   donor easeOutCubic numeric tween instead
                                   (ONLY for surfaces where no test pins DOM
                                   text); the final frame writes the exact
                                   string. Identical value => zero DOM writes,
                                   zero re-animation (skip-render friendly).
     stagger(container, sig, o)    first-render-only entrance stagger: stamps
                                   --i on children (cap o.cap||16, extras get
                                   .bx-no-anim) + adds .bx-stagger (or
                                   .bx-stagger-rows with o.rows). The sig
                                   memo means an identical render signature
                                   NEVER re-animates.
     initVisibilityPause()         wire once: toggles .bx-paused on <html>
                                   while document.hidden — every ambient
                                   animation pauses in background tabs.
   ─────────────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";
  if (window.BXUI) { return; } /* idempotent: second injection is a no-op */

  function reduced() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function num(n) { return Number(n || 0).toLocaleString("en-IN"); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined && text !== null) { n.textContent = String(text); }
    return n;
  }

  function clear(node) {
    while (node.firstChild) { node.removeChild(node.firstChild); }
  }

  /* fetch with a hard deadline: a hung endpoint (mid-pull starvation) rejects
     into the caller's catch instead of pending forever behind a "Loading…". */
  function fetchWithTimeout(url, ms, opts) {
    var timeoutMs = (typeof ms === "number" && ms > 0) ? ms : 12000;
    var ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    var timedOut = false;
    var init = {};
    if (opts) {
      for (var k in opts) {
        if (Object.prototype.hasOwnProperty.call(opts, k)) { init[k] = opts[k]; }
      }
    }
    if (ctrl) { init.signal = ctrl.signal; }
    var timer = setTimeout(function () {
      timedOut = true;
      if (ctrl) { ctrl.abort(); }
    }, timeoutMs);
    return fetch(url, init).then(
      function (r) { clearTimeout(timer); return r; },
      function (err) {
        clearTimeout(timer);
        if (timedOut) {
          var e = new Error("request timed out after " + timeoutMs +
            "ms — the server may be busy (a pull/prebuild may be running)");
          e.name = "TimeoutError";
          e.timeout = true;
          throw e;
        }
        throw err;
      }
    );
  }

  /* Shimmer skeleton grid. Optional honesty watchdog: if the skeleton is
     STILL attached after watchdogMs, append a caption (e.g. "still working —
     the lake is busy (a pull/prebuild may be running)"). */
  function skeleton(panel, rows, cols, opts) {
    if (!panel) { return null; }
    rows = rows || 6;
    cols = cols || 1;
    opts = opts || {};
    clear(panel);
    var grid = el("div", "bx-skel-grid");
    grid.setAttribute("aria-hidden", "true");
    grid.style.gridTemplateColumns = "repeat(" + cols + ", minmax(0, 1fr))";
    for (var i = 0; i < rows * cols; i++) {
      var cell = el("span", "bx-skel bx-skel-cell");
      cell.style.animationDelay = ((i % cols) * 40) + "ms";
      grid.appendChild(cell);
    }
    panel.appendChild(grid);
    if (opts.watchdogMs && opts.watchdogText) {
      setTimeout(function () {
        if (!panel.contains(grid)) { return; } /* already resolved to data/error */
        var cap = el("div", "bx-skel-caption");
        cap.appendChild(el("span", "bx-live-dot live-warn"));
        cap.appendChild(el("span", null, opts.watchdogText));
        panel.appendChild(cap);
      }, opts.watchdogMs);
    }
    return grid;
  }

  /* Generic honest terminal-state block (createElement/textContent only). */
  function stateCard(panel, variant, opts) {
    if (!panel) { return null; }
    opts = opts || {};
    clear(panel);
    var box = el("div", "bx-state " + (variant || "empty"));
    if (opts.icon) { box.appendChild(el("div", "ico", opts.icon)); }
    if (opts.title) { box.appendChild(el("div", "headline", opts.title)); }
    if (opts.hint) { box.appendChild(el("div", "hint", opts.hint)); }
    if (typeof opts.retry === "function") {
      var btn = el("button", "bx-btn bx-state-retry", opts.retryText || "↻ Retry");
      btn.type = "button";
      btn.addEventListener("click", function () { opts.retry(); });
      box.appendChild(btn);
    }
    /* CTA links accept app-relative paths ONLY (defence-in-depth: a server
       string can never become a javascript: URL through this builder). */
    if (opts.ctaText && typeof opts.ctaHref === "string" && opts.ctaHref.charAt(0) === "/") {
      var a = el("a", "bx-btn bx-btn-primary", opts.ctaText);
      a.setAttribute("href", opts.ctaHref);
      box.appendChild(a);
    }
    panel.appendChild(box);
    return box;
  }

  function errorCard(panel, opts) {
    opts = opts || {};
    var title = opts.title ||
      (opts.offline ? "server unreachable" :
        (opts.status ? "couldn't load — error " + opts.status : "couldn't load"));
    return stateCard(panel, opts.offline ? "offline" : "error", {
      icon: opts.icon || "⚠",
      title: title,
      hint: opts.hint || "the server may be busy (a pull/prebuild may be running)",
      retry: opts.retry,
      retryText: opts.retryText
    });
  }

  function emptyState(panel, opts) {
    opts = opts || {};
    return stateCard(panel, "empty", {
      icon: opts.icon || "💭",
      title: opts.title,
      hint: opts.hint,
      retry: opts.retry,
      retryText: opts.retryText,
      ctaText: opts.ctaText,
      ctaHref: opts.ctaHref
    });
  }

  /* Truthful heartbeat: green pulses ONLY on success; amber = degraded/slow;
     red = down. Kills the always-green lying dot class of defects. */
  function setDotState(dot, state) {
    if (!dot) { return; }
    dot.classList.remove("live-ok", "live-warn", "live-down");
    if (state === "ok" || state === "warn" || state === "down") {
      dot.classList.add("live-" + state);
    }
  }

  /* CHANGE-ONLY count-up (donor: the welcome.html eased count-up, ported to
     textContent). Default mode is value-pin bulletproof: the FINAL exact
     string is written synchronously, motion is a transform-only settle pop.
     {tween:true} opts into the numeric easeOutCubic tween for surfaces where
     no test pins DOM text; its last frame writes the exact final string. */
  function countUp(elm, finalText, opts) {
    if (!elm) { return; }
    opts = opts || {};
    var next = String(finalText);
    var prev = elm.getAttribute("data-bx-prev");
    if (prev === next) { return; } /* identical value: zero writes, zero motion */
    elm.setAttribute("data-bx-prev", next);
    if (reduced()) { elm.textContent = next; return; }
    var digits = next.replace(/,/g, "");
    if (opts.tween && /^\d+$/.test(digits) && typeof requestAnimationFrame === "function") {
      var target = parseInt(digits, 10);
      var dur = opts.duration || 900;
      var fmt = (typeof opts.format === "function") ? opts.format
        : (next.indexOf(",") >= 0 ? num : String);
      var t0 = null;
      var step = function (ts) {
        if (t0 === null) { t0 = ts; }
        var p = Math.min((ts - t0) / dur, 1);
        var eased = 1 - Math.pow(1 - p, 3); /* easeOutCubic — the donor curve */
        elm.textContent = (p < 1) ? String(fmt(Math.round(target * eased))) : next;
        if (p < 1) { requestAnimationFrame(step); }
      };
      requestAnimationFrame(step);
      return;
    }
    elm.textContent = next; /* final exact string FIRST — value-pin safe */
    elm.classList.remove("bx-countpop");
    void elm.offsetWidth; /* restart the pop animation */
    elm.classList.add("bx-countpop");
  }

  /* First-render-only entrance stagger. Pass the render signature the page's
     existing skip-render memo already computes (e.g. the raw payload text or
     lastRenderSig): an identical signature is a no-op, so a skipped render
     NEVER re-animates (DEC-DASHBOARD-JS-PERF-001 friendly). Children beyond
     the cap (default 16) get .bx-no-anim and appear instantly. */
  function stagger(container, signature, opts) {
    if (!container) { return; }
    opts = opts || {};
    var sig = (signature == null) ? "" : String(signature);
    if (container.getAttribute("data-bx-stag") === sig) { return; }
    container.setAttribute("data-bx-stag", sig);
    var cap = opts.cap || 16;
    var kids = container.children;
    for (var i = 0; i < kids.length; i++) {
      if (i < cap) {
        kids[i].style.setProperty("--i", String(i));
        kids[i].classList.remove("bx-no-anim");
      } else {
        kids[i].classList.add("bx-no-anim");
      }
    }
    container.classList.add(opts.rows ? "bx-stagger-rows" : "bx-stagger");
  }

  /* Pause every ambient animation while the tab is hidden (richer feel at
     LOWER runtime cost). Wire once per page; repeat calls are no-ops. */
  var pauseWired = false;
  function initVisibilityPause() {
    if (pauseWired || !document.documentElement) { return; }
    pauseWired = true;
    var apply = function () {
      document.documentElement.classList.toggle("bx-paused", !!document.hidden);
    };
    document.addEventListener("visibilitychange", apply);
    apply();
  }

  window.BXUI = {
    version: "1.0",
    esc: esc,
    num: num,
    el: el,
    clear: clear,
    fetchWithTimeout: fetchWithTimeout,
    skeleton: skeleton,
    stateCard: stateCard,
    errorCard: errorCard,
    emptyState: emptyState,
    setDotState: setDotState,
    countUp: countUp,
    stagger: stagger,
    initVisibilityPause: initVisibilityPause
  };
})();
