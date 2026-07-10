/* bx-operator-portal.js — the v29-only single-portal glue
 * (DEC-OPERATOR-PAGE-CONSOLIDATE-001).
 *
 * DOM layer ONLY — the pure URL core lives in bx-portal-state.js. Loaded
 * only by v29.html and presence-guarded on #ocWinnersSection, so the file
 * is inert on every other page.
 *
 * Owns:
 *   - the winners expand/collapse (LAZY: the screener panel mounts on the
 *     FIRST expand or a URL restore — a collapsed section costs zero row
 *     work);
 *   - the strategy drawer open/close/re-rank/prev-next over
 *     BXStrategyPanel.mount() once + update() (the gen guard discards
 *     stale responses);
 *   - the URL state machine (?winners=1&rank=N): drawer-open is the ONLY
 *     history.pushState; expand/collapse/close/re-rank/run-change are
 *     replaceState; close NEVER navigates (Back still closes the drawer
 *     via the popstate reconciler); the `reconciling` flag suppresses ALL
 *     history writes while the reconciler converges the DOM to the URL;
 *   - the run-change subscription via BXOneclick.onRunChange (NOT a DOM
 *     'change' listener — the runs poller's programmatic auto-selection
 *     sets select.value without firing 'change'); handler order: close
 *     drawer → strip rank (+run_id) → reload winners through the
 *     freshly-invalidated memo;
 *   - a11y: focus trap, `inert` on main.bx-wrap while open, scroll lock,
 *     Esc + backdrop close, focus restore, visibility flip after the close
 *     transition, ArrowLeft/ArrowRight prev/next outside form fields.
 *
 * No pollers, no intervals, no rAF loops — zero background work; the only
 * timers are bounded one-shot animation-class fallbacks.
 *
 * Honest envelope (Golden Rule #10): browser-only glue pinned by
 * SOURCE-STRING tests (presence, not execution — the #1557 Playwright job
 * does not drive the Operator page); the pure codec underneath IS executed
 * by node goldens. Deep-link URL `run_id` is READ-only here (write support
 * is the named follow-up DEC-OPERATOR-PORTAL-RUNID-001).
 */
"use strict";

(function () {
  if (typeof document === "undefined") {
    return; // node harness: nothing pure lives here
  }

  function byId(id) {
    return document.getElementById(id);
  }

  var state = {
    runId: null,
    rank: null, // the open drawer rank (null = closed)
    rowCount: null, // from the memo'd top100 payload (prev/next law)
    winnersOpen: false,
    winnersMounted: false,
    drawerMounted: false,
    reconciling: false, // suppresses ALL history writes while converging
    lastFocus: null
  };
  var winnersCtl = null;

  /* ── URL writes (all through the pure serializer; foreign params kept) ── */

  function writeUrl(patch, push) {
    if (state.reconciling) {
      return; // the popstate/restore reconciler never writes history
    }
    var search = window.BXPortalState.serialize(window.location.search, patch);
    var url = window.location.pathname + search + window.location.hash;
    if (push) {
      history.pushState(null, "", url); // drawer OPEN — the ONLY pushState
    } else {
      history.replaceState(null, "", url);
    }
  }

  /* ── winners section ── */

  function mountWinnersOnce() {
    if (state.winnersMounted) {
      return;
    }
    state.winnersMounted = true; // lazy: first expand pays the mount
    winnersCtl = window.BXScreenerPanel.mount({
      onRowClick: function (rank, runId) {
        void runId; // the portal's run is the page's selected run
        openDrawer(Math.max(1, parseInt(rank, 10) || 1));
      }
    });
    if (state.runId) {
      winnersCtl.loadRows(state.runId);
    }
  }

  function startCascade() {
    // Row cascade on section OPEN only (filter/sort re-renders never
    // re-cascade — the class is removed after the one-shot wave).
    var rows = byId("scRows");
    if (!rows) {
      return;
    }
    rows.classList.add("wn-cascade");
    setTimeout(function () {
      rows.classList.remove("wn-cascade");
    }, 900); // bounded one-shot fallback (≤24 × 14ms stagger + 320ms row)
  }

  function expandWinners() {
    if (!state.winnersOpen) {
      state.winnersOpen = true;
      var body = byId("ocWinnersBody");
      if (body) {
        body.classList.add("open");
      }
      var toggle = byId("ocWinnersToggle");
      if (toggle) {
        toggle.setAttribute("aria-expanded", "true");
        toggle.classList.add("expanded");
      }
      mountWinnersOnce();
      startCascade();
    }
    writeUrl({ winners: true }, false);
  }

  function collapseWinners() {
    closeDrawer(); // the drawer is force-closed FIRST (rank implies winners)
    state.winnersOpen = false;
    var body = byId("ocWinnersBody");
    if (body) {
      body.classList.remove("open");
    }
    var toggle = byId("ocWinnersToggle");
    if (toggle) {
      toggle.setAttribute("aria-expanded", "false");
      toggle.classList.remove("expanded");
    }
    writeUrl({ winners: false, rank: null }, false);
  }

  /* ── drawer ── */

  function setMainInert(on) {
    var main = document.querySelector("main.bx-wrap");
    if (!main) {
      return;
    }
    if (on) {
      main.setAttribute("inert", ""); // the drawer + backdrop are body-level siblings
    } else {
      main.removeAttribute("inert");
    }
  }

  function markActiveRow(rank) {
    var rows = byId("scRows");
    if (!rows) {
      return;
    }
    var active = rows.querySelectorAll("tr[aria-selected]");
    for (var i = 0; i < active.length; i++) {
      active[i].removeAttribute("aria-selected");
      active[i].classList.remove("wn-row-active");
    }
    if (rank !== null) {
      var tr = rows.querySelector('tr[data-rank="' + String(rank) + '"]');
      if (tr) {
        tr.setAttribute("aria-selected", "true");
        tr.classList.add("wn-row-active"); // you-are-here, tracks prev/next surfing
      }
    }
  }

  function updatePrevNext() {
    // Prev/next law: rowCount from the top100 payload. prev enabled iff
    // rank > 1; next enabled iff rank < rowCount; rowCount === 0 ⇒ both
    // disabled — surfing can never loop into further misses.
    var rc = state.rowCount === null ? 0 : state.rowCount;
    var prev = byId("ocDrawerPrev");
    var next = byId("ocDrawerNext");
    if (prev) {
      prev.disabled = state.rank === null || rc === 0 || state.rank <= 1;
    }
    if (next) {
      next.disabled = state.rank === null || rc === 0 || state.rank >= rc;
    }
  }

  function refreshRowCount() {
    if (!state.runId) {
      state.rowCount = null;
      updatePrevNext();
      return;
    }
    // Rides the SAME memo'd promise as every other consumer — no extra fetch.
    window.BXOneclick.getTop100(state.runId)
      .then(function (payload) {
        var ok =
          payload &&
          typeof payload === "object" &&
          (payload.status === "ok" || payload.status === "partial");
        state.rowCount = ok && payload.row_count !== undefined ? payload.row_count : 0;
        updatePrevNext();
      })
      .catch(function () {
        state.rowCount = 0; // honest: no rows known ⇒ both surf buttons disabled
        updatePrevNext();
      });
  }

  function mountDrawerOnce() {
    if (state.drawerMounted) {
      return;
    }
    state.drawerMounted = true;
    window.BXStrategyPanel.mount({ animate: true });
  }

  function swapDrawerBody(forward) {
    var body = document.querySelector("#ocStrategyDrawer .drawer-body");
    if (!body) {
      return;
    }
    var cls = forward ? "bxd-swap" : "bxd-swapRev";
    body.classList.remove("bxd-swap", "bxd-swapRev");
    // Re-add on the NEXT task so the one-shot restarts — never a forced
    // synchronous layout flush (zero JS layout measurement).
    setTimeout(function () {
      body.classList.add(cls);
      setTimeout(function () {
        body.classList.remove(cls);
      }, 600); // bounded one-shot fallback
    }, 0);
  }

  function openDrawer(rank) {
    mountDrawerOnce();
    var wasOpen = state.rank !== null;
    var prevRank = state.rank;
    state.rank = rank;
    var pill = byId("ocDrawerRankPill");
    if (pill) {
      pill.textContent = "#" + String(rank);
    }
    // Judge-3 MF#3: the open-full link stays hidden until a runId is
    // resolved — never a dead /strategy link window.
    var full = byId("ocDrawerOpenFull");
    if (full) {
      if (state.runId) {
        full.setAttribute(
          "href",
          window.BXPortalState.strategyHref(state.runId, rank)
        );
        full.hidden = false;
      } else {
        full.hidden = true;
      }
    }
    var drawer = byId("ocStrategyDrawer");
    var back = byId("ocDrawerBack");
    if (!wasOpen) {
      // Visibility discipline is CSS-owned (bx-oneclick-panels.css): the
      // shut drawer sits at visibility:hidden (untabbable) + translateX;
      // .open flips visibility instantly and slides in — and on close the
      // delayed visibility transition re-hides it only AFTER the slide-out
      // completes. No display toggling ⇒ no forced layout flush here.
      if (drawer) {
        drawer.classList.add("open");
      }
      if (back) {
        back.classList.add("open");
      }
      document.body.classList.add("bx-drawer-open"); // scroll lock
      setMainInert(true);
      state.lastFocus = document.activeElement; // focus restore target
      var closeBtn = byId("ocDrawerClose");
      if (closeBtn) {
        closeBtn.focus();
      }
      writeUrl({ winners: true, rank: rank }, true); // the ONLY pushState arm
    } else {
      swapDrawerBody(prevRank !== null && rank > prevRank);
      writeUrl({ rank: rank }, false); // re-rank is a replaceState
    }
    markActiveRow(rank);
    updatePrevNext();
    // The gen-guarded update: a stale previous-rank response can never
    // paint over this load (bx-strategy-panel state.gen law).
    window.BXStrategyPanel.update({ runId: state.runId, rank: rank, tradeIdx: null });
  }

  function closeDrawer() {
    if (state.rank === null) {
      return;
    }
    state.rank = null;
    var drawer = byId("ocStrategyDrawer");
    var back = byId("ocDrawerBack");
    if (drawer) {
      // visibility flips back to hidden AFTER the slide-out completes (the
      // CSS delayed-visibility transition) — untabbable when shut.
      drawer.classList.remove("open");
    }
    if (back) {
      back.classList.remove("open");
    }
    document.body.classList.remove("bx-drawer-open");
    setMainInert(false);
    markActiveRow(null);
    updatePrevNext();
    // Focus restore to the originating row/element (if still attached).
    if (state.lastFocus && document.contains(state.lastFocus)) {
      state.lastFocus.focus();
    }
    state.lastFocus = null;
    // Close is a replaceState removing rank — NEVER a history navigation
    // (a close gesture must never leave the page or resurrect an older
    // deep-linked rank); Back still closes via the popstate reconciler
    // because drawer-open is the only pushState.
    writeUrl({ rank: null }, false);
  }

  function surfPrev() {
    var rc = state.rowCount === null ? 0 : state.rowCount;
    if (state.rank === null || rc === 0 || state.rank <= 1) {
      return;
    }
    // From a MISS state (rank > rowCount) prev targets the LAST REAL rank.
    openDrawer(Math.min(state.rank - 1, rc));
  }

  function surfNext() {
    var rc = state.rowCount === null ? 0 : state.rowCount;
    if (state.rank === null || rc === 0 || state.rank >= rc) {
      return;
    }
    openDrawer(state.rank + 1);
  }

  /* ── the popstate reconciler (idempotent DOM convergence to the URL) ── */

  function applyUrl(parsed) {
    state.reconciling = true; // suppress ALL history writes while converging
    try {
      if (parsed.winners || parsed.rank !== null) {
        expandWinners();
      } else {
        collapseWinners();
      }
      if (parsed.rank !== null) {
        openDrawer(parsed.rank);
      } else {
        closeDrawer();
      }
    } finally {
      state.reconciling = false;
    }
  }

  /* ── run change (the observer seam — never a DOM 'change' listener) ── */

  function onRunChanged(runId) {
    if (String(runId) === String(state.runId)) {
      return; // the same run (e.g. the poller echoing a deep-link selectRun)
    }
    // Handler order (pinned): close drawer FIRST, then strip rank (+run_id)
    // from the URL so it never lies, then reload winners through the
    // freshly-invalidated memo (a failed reload leaves the honest
    // awaiting/errorCard state — never stale rows for the old run).
    closeDrawer();
    writeUrl({ rank: null, runId: null }, false);
    state.runId = runId;
    refreshRowCount();
    if (state.winnersMounted && winnersCtl) {
      winnersCtl.loadRows(runId);
    }
  }

  /* ── wiring ── */

  function wireHandlers() {
    var toggle = byId("ocWinnersToggle");
    if (toggle) {
      toggle.addEventListener("click", function () {
        if (state.winnersOpen) {
          collapseWinners();
        } else {
          expandWinners();
        }
      });
    }
    var jump = byId("ocWinnersJump");
    if (jump) {
      jump.addEventListener("click", function () {
        expandWinners();
        var section = byId("ocWinnersSection");
        if (section) {
          var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
          section.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
        }
      });
    }
    var closeBtn = byId("ocDrawerClose");
    if (closeBtn) {
      closeBtn.addEventListener("click", closeDrawer);
    }
    var back = byId("ocDrawerBack");
    if (back) {
      back.addEventListener("click", closeDrawer); // backdrop click closes
    }
    var prev = byId("ocDrawerPrev");
    if (prev) {
      prev.addEventListener("click", surfPrev);
    }
    var next = byId("ocDrawerNext");
    if (next) {
      next.addEventListener("click", surfNext);
    }
    document.addEventListener("keydown", function (ev) {
      if (state.rank === null) {
        return;
      }
      if (ev.key === "Escape") {
        closeDrawer();
        return;
      }
      // Arrow surf ONLY outside form fields (typing in a filter/select
      // must never surf the drawer).
      var tag =
        ev.target && ev.target.tagName ? String(ev.target.tagName).toLowerCase() : "";
      if (tag === "input" || tag === "select" || tag === "textarea") {
        return;
      }
      if (ev.key === "ArrowLeft") {
        surfPrev();
      } else if (ev.key === "ArrowRight") {
        surfNext();
      }
    });
    var drawer = byId("ocStrategyDrawer");
    if (drawer) {
      // Focus trap: Tab/Shift-Tab cycle within the dialog while open.
      drawer.addEventListener("keydown", function (ev) {
        if (ev.key !== "Tab") {
          return;
        }
        var focusables = drawer.querySelectorAll(
          'button:not([disabled]), a[href]:not([hidden]), input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (!focusables.length) {
          return;
        }
        var first = focusables[0];
        var last = focusables[focusables.length - 1];
        if (ev.shiftKey && document.activeElement === first) {
          ev.preventDefault();
          last.focus();
        } else if (!ev.shiftKey && document.activeElement === last) {
          ev.preventDefault();
          first.focus();
        }
      });
    }
    // Delegated combos View-link intercept: an UNMODIFIED left-click opens
    // the drawer at that rank; modified/middle clicks fall through to the
    // real /strategy href (the hrefs themselves stay byte-identical).
    var combosBody = byId("combosBody");
    if (combosBody) {
      combosBody.addEventListener("click", function (ev) {
        if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || ev.button !== 0) {
          return; // modified click: the real href navigates
        }
        var link = ev.target && ev.target.closest ? ev.target.closest("a") : null;
        if (!link || !combosBody.contains(link)) {
          return;
        }
        var href = link.getAttribute("href") || "";
        var q = href.indexOf("?");
        if (href.indexOf("/strategy") !== 0 || q === -1) {
          return;
        }
        var parsed = window.BXPortalState.parseStrategyParams(href.slice(q));
        if (!parsed.runId) {
          return;
        }
        ev.preventDefault();
        if (!state.runId) {
          state.runId = parsed.runId;
          refreshRowCount();
        }
        expandWinners();
        openDrawer(parsed.rank);
      });
    }
    // Run switch: the observer seam (the runs poller's programmatic
    // auto-selection fires NO DOM 'change' event — judge-3 MF#2).
    window.BXOneclick.onRunChange(onRunChanged);
    // Back/forward: converge the DOM to the URL (drawer-open is the only
    // pushState, so Back = close drawer; never a site exit from close).
    window.addEventListener("popstate", function () {
      applyUrl(window.BXPortalState.parse(window.location.search));
    });
  }

  /* ── deep-link restore (judge-1 MF#4 ordering: parse → resolve run →
        expand + lazy-mount winners → open drawer through the gen guard) ── */

  function finishRestore(parsed) {
    state.reconciling = true; // a URL restore never writes history
    try {
      refreshRowCount();
      if (parsed.winners || parsed.rank !== null) {
        expandWinners();
      }
      if (parsed.rank !== null && state.runId) {
        openDrawer(parsed.rank);
      }
    } finally {
      state.reconciling = false;
    }
  }

  function restoreFromUrl(parsed) {
    if (!parsed.winners && parsed.rank === null && parsed.runId === null) {
      return; // nothing to restore — the portal stays lazy
    }
    if (parsed.runId) {
      // 1) the URL's run wins: select it page-wide BEFORE opening anything
      //    (the reconciling flag suppresses the run-change handler's strip).
      state.runId = parsed.runId;
      state.reconciling = true;
      try {
        window.BXOneclick.selectRun(parsed.runId);
      } finally {
        state.reconciling = false;
      }
      finishRestore(parsed);
      return;
    }
    // 2) else the page's already-selected run…
    var select = byId("ocRunSelect");
    if (select && select.value) {
      state.runId = select.value;
      finishRestore(parsed);
      return;
    }
    // 3) …else ONE /runs one-shot resolves the latest run. The runs
    //    poller's later auto-selection of the SAME id is a no-op in
    //    onRunChanged; no runs at all ⇒ the honest awaiting states stand
    //    (the drawer shows the awaiting detail, winners the awaiting line).
    window.BXOneclick.latestRun()
      .then(function (runs) {
        if (runs && runs.latest_run_id) {
          state.runId = String(runs.latest_run_id);
        }
        finishRestore(parsed);
      })
      .catch(function () {
        finishRestore(parsed); // honest awaiting — never a fabricated run
      });
  }

  function init() {
    if (!byId("ocWinnersSection")) {
      return; // not the operator page — fully inert
    }
    wireHandlers();
    restoreFromUrl(window.BXPortalState.parse(window.location.search));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

/* Non-browser harness: this file is DOM glue only — nothing pure lives
   here (the codec is bx-portal-state.js); the guard keeps the house
   require() discipline uniform. */
if (typeof module !== "undefined" && module.exports) {
  module.exports = {};
}
