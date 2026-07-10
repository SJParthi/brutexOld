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
 * timers are bounded one-shot animation-class/visibility fallbacks (the
 * drawer's next-task class add on open + the after-slide re-hide on close).
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
    // C-M1 (adversarial-review fold): TRUE from restoreFromUrl until
    // finishRestore completes — the runs poller's first auto-selection
    // resolves BEFORE the portal's own latestRun() on the common no-run_id
    // deep-link path, and its onRunChanged must NOT strip the deep link's
    // rank while the restore is still in flight (the `reconciling` flag
    // alone cannot cover this window — the strip runs OUTSIDE it).
    restorePending: false,
    // NEW-R2-3 (adversarial-review fold): a close leaves a rank-less entry
    // identical to the pre-open one — the NEXT open replaces it instead of
    // pushing, so open/close cycles never accumulate duplicate entries.
    // Cleared on popstate (there the current entry is a REAL prior one).
    // Armed ONLY over a portal-PUSHED entry (the history.state stamp —
    // NEW-R2R2-2, fix round 2): a deep-link-LANDING entry is a real
    // navigation entry, and collapsing its reopen into it would leave Back
    // exiting the site from an open drawer.
    reopenReplaces: false,
    lastFocus: null
  };
  var winnersCtl = null;
  // NIT-2 (fix round 2): the ONE pending after-slide re-hide timer — each
  // close used to arm an independent, never-cancelled timer, so a rapid
  // open/close/open/close (<400 ms) let close #1's timer cut close #2's
  // slide-out short. Every open cancels it; a new close re-arms it.
  var rehideTimer = null;
  // NEW-R2-1 twin (adversarial-review fold): the rowCount read rides the
  // same memo'd promise as the winners table — and needs the same
  // stale-response guard, or a slow old run's row_count sets the surf
  // bounds under a newer run.
  var rowCountGen = 0;

  /* ── URL writes (all through the pure serializer; foreign params kept) ── */

  function writeUrl(patch, push) {
    if (state.reconciling) {
      return; // the popstate/restore reconciler never writes history
    }
    var search = window.BXPortalState.serialize(window.location.search, patch);
    var url = window.location.pathname + search + window.location.hash;
    if (push) {
      // The bxPortalPush stamp marks the entry as PORTAL-created —
      // closeDrawer arms the reopen-replace shortcut only over such an
      // entry (NEW-R2R2-2, fix round 2).
      history.pushState({ bxPortalPush: true }, "", url); // drawer OPEN — the ONLY pushState
    } else {
      // In-place rewrite: the entry's ORIGIN stamp survives every
      // replaceState (close/re-rank/expand/collapse), so a later close in
      // the same slot still knows whether the portal pushed it.
      history.replaceState(history.state, "", url);
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
      },
      onRender: function () {
        // C-N1/NEW-R1-2 (adversarial-review fold): the panel rebuilds its
        // tbody from scratch on EVERY render — re-apply the you-are-here
        // mark each time (the async first mount on combos-intercept/deep-
        // link opens AND filter/sort re-renders while the drawer is open).
        markActiveRow(state.rank);
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
    rowCountGen += 1; // a newer read discards every older response
    var gen = rowCountGen;
    if (!state.runId) {
      state.rowCount = null;
      updatePrevNext();
      return;
    }
    // Rides the SAME memo'd promise as every other consumer — no extra fetch.
    window.BXOneclick.getTop100(state.runId)
      .then(function (payload) {
        if (gen !== rowCountGen) { return; } // stale-run response — discarded
        var ok =
          payload &&
          typeof payload === "object" &&
          (payload.status === "ok" || payload.status === "partial");
        state.rowCount = ok && payload.row_count !== undefined ? payload.row_count : 0;
        updatePrevNext();
      })
      .catch(function () {
        if (gen !== rowCountGen) { return; } // stale-run failure — discarded
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
    // Re-add on the NEXT task so the one-shot ANIMATION restarts — no
    // reflow needed here (animations re-fire on rule re-match, unlike
    // transitions; the module's ONE sanctioned forced reflow lives in
    // openDrawer's un-hide arm, where a transition needs a committed
    // before-change style — fix round 2).
    setTimeout(function () {
      body.classList.add(cls);
      setTimeout(function () {
        body.classList.remove(cls);
      }, 600); // bounded one-shot fallback
    }, 0);
  }

  function openDrawer(rank) {
    mountDrawerOnce();
    if (rehideTimer !== null) {
      // NIT-2: a pending re-hide from an earlier close is moot once we are
      // open (and would truncate a LATER close's slide-out) — cancel it.
      clearTimeout(rehideTimer);
      rehideTimer = null;
    }
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
      // Managed hidden state (E-M1/S-N1 adversarial-review fold): the SHUT
      // drawer + backdrop carry the `hidden` attribute (UA display:none) so
      // a panels-CSS load failure degrades to an INVISIBLE drawer — never
      // an unstyled skeleton dumped at the document end. Un-hide first,
      // then ONE deliberate forced reflow per drawer open (fix round 2,
      // MINOR-1): an element not rendered at the previous style-change
      // event has no before-change style (CSS Transitions L1 §3), and the
      // next-task `.open` add usually lands in the SAME frame — the flush
      // commits the un-hidden, still-translated-shut style so the entrance
      // slide reliably transitions instead of popping in. `.open` (the
      // slide-in + the .open-scoped section-rise/pill-glow choreography)
      // still lands on the NEXT task (no rAF — T-N6); the CSS
      // visibility:hidden discipline stays as belt-and-braces.
      if (drawer) {
        drawer.removeAttribute("hidden");
      }
      if (back) {
        back.removeAttribute("hidden");
      }
      if (drawer) {
        void drawer.offsetWidth; // the ONE sanctioned reflow per drawer open
      }
      document.body.classList.add("bx-drawer-open"); // scroll lock
      setMainInert(true);
      state.lastFocus = document.activeElement; // focus restore target
      setTimeout(function () {
        if (state.rank === null) {
          return; // closed again before the frame — stay shut
        }
        if (drawer) {
          drawer.classList.add("open");
        }
        if (back) {
          back.classList.add("open");
        }
        var closeBtn = byId("ocDrawerClose");
        if (closeBtn) {
          closeBtn.focus(); // focusable only once visibility flips
        }
      }, 0);
      // NEW-R2-3: a re-open from the rank-less entry a prior close left
      // behind REPLACES that entry instead of pushing a duplicate.
      writeUrl({ winners: true, rank: rank }, !state.reopenReplaces); // the ONLY pushState-capable arm
      state.reopenReplaces = false;
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
    // Re-add `hidden` only AFTER the slide-out (bounded one-shot fallback,
    // > the .32s slide + the .34s visibility delay — also covers the
    // reduced-motion no-transition arm); a reopen inside the window keeps
    // the drawer visible (openDrawer CANCELS the pending timer — NIT-2 —
    // and the state.rank guard re-checks at fire time as belt).
    if (rehideTimer !== null) {
      clearTimeout(rehideTimer); // only the LATEST close owns the re-hide
    }
    rehideTimer = setTimeout(function () {
      rehideTimer = null;
      if (state.rank !== null) {
        return; // reopened — stays visible
      }
      var d = byId("ocStrategyDrawer");
      var b = byId("ocDrawerBack");
      if (d) {
        d.setAttribute("hidden", "");
      }
      if (b) {
        b.setAttribute("hidden", "");
      }
    }, 400);
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
    if (!state.reconciling && history.state && history.state.bxPortalPush === true) {
      // NEW-R2-3: the current entry now duplicates the pre-open one — the
      // next open replaces it (a reconciler close wrote nothing, so the
      // flag stays as-is there). Armed ONLY over a portal-PUSHED entry
      // (the history.state stamp — NEW-R2R2-2, fix round 2): a deep-link-
      // ORIGIN close rewrote the page's ONE real navigation entry, which
      // duplicates nothing beneath it — the reopen must PUSH so Back from
      // the reopened drawer closes the drawer instead of exiting the site.
      state.reopenReplaces = true;
    }
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
    if (state.drawerMounted) {
      // E-M4 (adversarial-review fold): bump the strategy panel's load
      // generation so the OLD run's in-flight callback chain stops dead —
      // no further dead fetches, no stale write into the hidden drawer.
      window.BXStrategyPanel.update({ runId: null });
    }
    if (!state.restorePending) {
      // C-M1 (adversarial-review fold): while a no-run_id deep-link restore
      // is in flight (the latestRun arm), the poller's first auto-selection
      // must NOT strip the deep link's rank — finishRestore owns the URL
      // until it completes.
      writeUrl({ rank: null, runId: null }, false);
    }
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
        if (state.runId && String(parsed.runId) !== String(state.runId)) {
          // NEW-R2-2 (adversarial-review fold): a not-yet-repainted combos
          // row for ANOTHER run (a just-switched page) falls through to its
          // REAL /strategy href — the drawer must never show the new run's
          // rank-N for a row that described the old run's strategy.
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
      // Back landed on a REAL prior entry — the next open must PUSH again
      // (the reopen-replaces shortcut applies only to the close-created
      // duplicate entry).
      state.reopenReplaces = false;
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
      state.restorePending = false; // the C-M1 latch releases HERE only
    }
  }

  function restoreFromUrl(parsed) {
    if (!parsed.winners && parsed.rank === null && parsed.runId === null) {
      return; // nothing to restore — the portal stays lazy
    }
    // C-M1 (adversarial-review fold): the latch is set BEFORE any async arm
    // resolves, so the runs poller's first auto-selection — usually issued
    // (and resolved) before the portal's own latestRun() — can never strip
    // the deep link's rank while the restore is in flight.
    state.restorePending = true;
    if (parsed.runId) {
      // 1) the URL's run wins: select it page-wide BEFORE opening anything
      //    (the reconciling flag suppresses the run-change handler's strip).
      state.runId = parsed.runId;
      try {
        state.reconciling = true;
        try {
          window.BXOneclick.selectRun(parsed.runId);
        } finally {
          state.reconciling = false;
        }
      } finally {
        // MINOR-2 (fix round 2): a sync throw out of selectRun (e.g. a
        // dead bx-poll global) must still release the latch — funneled
        // through finishRestore, the ONE release site. Arm-SCOPED on this
        // fully-synchronous arm only: a whole-body finally would release
        // while arm 3's latestRun is still in flight and re-open the exact
        // C-M1 poller-strip race the latch closes (NEW-R2R3-2 — rejected).
        finishRestore(parsed);
      }
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
    try {
      window.BXOneclick.latestRun()
        .then(function (runs) {
          // NIT-1/edges-F3 (fix round 2): a run adopted DURING the latch
          // window (the poller's latch-suppressed auto-selection) wins over
          // this second same-endpoint read — never clobbered back, so the
          // drawer/winners and the page surfaces stay on ONE run.
          if (!state.runId && runs && runs.latest_run_id) {
            state.runId = String(runs.latest_run_id);
          }
          finishRestore(parsed);
        })
        .catch(function () {
          finishRestore(parsed); // honest awaiting — never a fabricated run
        });
    } catch (err) {
      // MINOR-2 twin (fix round 2): a sync throw CONSTRUCTING the chain
      // (latestRun itself — e.g. a dead bx-poll global) never strands the
      // latch. Catch-only, deliberately NOT a finally: a finally here would
      // release while a HEALTHY chain is still in flight — the C-M1 race.
      finishRestore(parsed);
      throw err;
    }
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
