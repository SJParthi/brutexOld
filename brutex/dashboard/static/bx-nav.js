/* BruteX dashboard in-place navigation (DEC-DASHBOARD-NAV-SWAP-001).
 *
 * Turns the top-nav full-page reloads into an in-place DOM content swap — the
 * dominant remaining nav latency that DEC-DASHBOARD-NAV-LATENCY-001 (ledger
 * #933) explicitly deferred as "a SEPARATE larger PR". This is the follow-up.
 *
 * Design (pure PROGRESSIVE ENHANCEMENT — never breaks nav):
 *   - The nav links stay real `<a href>` anchors (so the nav-consistency guard
 *     test + crawlers + middle-click + "open in new tab" all keep working, and
 *     a no-JS / failed-fetch user gets the normal full-page navigation).
 *   - A plain left-click on a same-origin top-nav link is intercepted: we
 *     `fetch` the target page (the SAME byte-identical HTML the server already
 *     serves — NO new route, NO new payload), parse it, and swap ONLY the
 *     <body> content + the page-specific <head> assets, then re-run the page's
 *     own inline/deferred scripts so each page boots exactly as it would on a
 *     full load. `history.pushState` keeps the URL + back/forward + deep links
 *     correct; `popstate` swaps back.
 *   - ANY failure (fetch error, non-OK status, parse failure, opt-out) falls
 *     through to a normal full navigation — the feature can only ever ADD speed,
 *     never remove the ability to navigate (Golden Rule #10 honest envelope).
 *
 * Scope: this is a FRONTEND delivery change only. The engine, the lake, the
 * routes, and every JSON/CSV payload are byte-identical (CLAUDE.md §3 scope
 * lock — ENGINE UNCHANGED, READ-ONLY dashboard).
 */
(function () {
  "use strict";

  // Guard: nothing to do without the History API or fetch (very old browsers
  // simply keep the full-reload behaviour). This is the no-op fallback path.
  if (!window.history || !window.history.pushState || !window.fetch) {
    return;
  }

  var SWAP_FLAG = "__bxNavSwap";
  // Mark the document once so a (defensive) double-include is a no-op.
  if (window[SWAP_FLAG]) {
    return;
  }
  window[SWAP_FLAG] = true;

  /* ---------------------------------------------------------------------------
   * Timer registry (BLOCKER: leaked pollers).
   *
   * The in-place body swap throws away the previous page's DOM but NOT its
   * live `setInterval`/`setTimeout` timers. Every dashboard page installs its
   * own pollers (coverage, universe's 6 intervals, …) — left running they keep
   * fetching the server forever and `runScripts` re-registers a fresh set on
   * every revisit, so memory + server load GROW with each navigation (the exact
   * opposite of the latency goal).
   *
   * Fix (page-agnostic, COMMON — no per-page edits): wrap the timer factories
   * ONCE here so we record every timer id installed since the last swap, then
   * clear them right before the next swap and reset the registry. Pages keep
   * calling the plain `setInterval`/`setTimeout` — the bookkeeping is invisible
   * to them. Idempotent + defensive: a missing factory or a throwing clear can
   * never break navigation. */
  var liveIntervals = [];
  var liveTimeouts = [];

  (function wrapTimers() {
    var realSetInterval = window.setInterval;
    var realSetTimeout = window.setTimeout;
    if (typeof realSetInterval === "function") {
      window.setInterval = function () {
        var id = realSetInterval.apply(window, arguments);
        liveIntervals.push(id);
        return id;
      };
    }
    if (typeof realSetTimeout === "function") {
      window.setTimeout = function () {
        var id = realSetTimeout.apply(window, arguments);
        liveTimeouts.push(id);
        return id;
      };
    }
  })();

  /* Tear down every timer installed since the last swap + announce teardown so
   * any future per-page cleanup can hook in. Called immediately BEFORE the body
   * is replaced (while the old DOM + its handlers are still attached). */
  function teardownPage() {
    try {
      window.dispatchEvent(new Event("bx:teardown"));
    } catch (e) {
      /* Event ctor can be unavailable in exotic sandboxes — never fatal. */
    }
    var i;
    for (i = 0; i < liveIntervals.length; i++) {
      try {
        window.clearInterval(liveIntervals[i]);
      } catch (e) {
        /* ignore — a stale/cleared id must not abort the teardown loop. */
      }
    }
    for (i = 0; i < liveTimeouts.length; i++) {
      try {
        window.clearTimeout(liveTimeouts[i]);
      } catch (e) {
        /* ignore — as above. */
      }
    }
    liveIntervals = [];
    liveTimeouts = [];
  }

  /* Is this an in-page top-nav anchor we should intercept? Only same-origin,
   * non-modified, left-click, http(s) navigations inside the canonical topnav.
   * Everything else (external links, downloads, target=_blank, hash-only,
   * modified clicks) is left to the browser's default full navigation. */
  function navTarget(event) {
    if (event.defaultPrevented) return null;
    if (event.button !== 0) return null; // left-click only
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return null;
    var el = event.target;
    while (el && el !== document.body) {
      if (el.tagName === "A") break;
      el = el.parentNode;
    }
    if (!el || el.tagName !== "A") return null;
    var a = /** @type {HTMLAnchorElement} */ (el);
    if (!a.closest || !a.closest("nav.topnav")) return null;
    if (a.target && a.target !== "" && a.target !== "_self") return null;
    if (a.hasAttribute("download")) return null;
    if (a.origin !== window.location.origin) return null;
    var href = a.getAttribute("href");
    if (!href || href.charAt(0) === "#") return null;
    // Same path + hash-only change → let the browser handle it normally.
    if (a.pathname === window.location.pathname && a.search === window.location.search) {
      return null;
    }
    return a.href;
  }

  /* Re-execute the scripts inside a freshly-swapped container. innerHTML never
   * runs <script> tags, so we clone each one into a live element the browser
   * will execute (inline scripts run synchronously in order; external src
   * scripts are re-requested — already cached). Without this every page's own
   * boot logic (polling, cascades, datepickers) would never start. */
  function runScripts(container) {
    var scripts = container.querySelectorAll("script");
    for (var i = 0; i < scripts.length; i++) {
      var old = scripts[i];
      var fresh = document.createElement("script");
      for (var j = 0; j < old.attributes.length; j++) {
        var attr = old.attributes[j];
        fresh.setAttribute(attr.name, attr.value);
      }
      if (old.src) {
        fresh.async = false; // preserve execution order for external scripts
      } else {
        fresh.textContent = old.textContent;
      }
      old.parentNode.replaceChild(fresh, old);
    }
  }

  /* Reconcile the <head> page-specific assets so the swapped body is themed AND
   * scripted correctly. We ADD only the assets the incoming page declares that
   * the current head lacks; we never tear the head down (avoids a flash).
   *
   * Two asset classes:
   *   - <style> + <link rel=stylesheet>: cloned 1:1, keyed by exact outerHTML.
   *   - <script src> (e.g. bx-datepicker.js, loaded in <head> on db/movers/
   *     reports/universe): innerHTML/appendChild of a CLONED <script> does NOT
   *     execute, so we build a FRESH executable <script> (the runScripts trick),
   *     deduped by `src` so a script already present from a prior page is not
   *     re-run. WITHOUT this, navigating to a datepicker page from one that did
   *     not already load it gives a styled-but-DEAD datepicker (MAJOR). Inline
   *     head <script> (no src) is left alone — pages keep their boot logic in
   *     the body, which runScripts handles, and an unkeyable inline head script
   *     would re-run on every swap. */
  function syncHeadAssets(newHead) {
    var have = {};
    var current = document.head.querySelectorAll("style,link[rel=stylesheet]");
    for (var i = 0; i < current.length; i++) {
      have[current[i].outerHTML] = true;
    }
    var incoming = newHead.querySelectorAll("style,link[rel=stylesheet]");
    for (var k = 0; k < incoming.length; k++) {
      var node = incoming[k];
      if (!have[node.outerHTML]) {
        document.head.appendChild(node.cloneNode(true));
        have[node.outerHTML] = true;
      }
    }
    syncHeadScripts(newHead);
  }

  /* Add any <head> <script src> the incoming page declares that the live head
   * is missing, as a fresh executable script (deduped by resolved src). */
  function syncHeadScripts(newHead) {
    var present = {};
    var live = document.head.querySelectorAll("script[src]");
    for (var i = 0; i < live.length; i++) {
      present[live[i].src] = true; // .src is the resolved absolute URL
    }
    var incoming = newHead.querySelectorAll("script[src]");
    for (var k = 0; k < incoming.length; k++) {
      var old = incoming[k];
      if (present[old.src]) continue;
      var fresh = document.createElement("script");
      for (var j = 0; j < old.attributes.length; j++) {
        var attr = old.attributes[j];
        fresh.setAttribute(attr.name, attr.value);
      }
      fresh.async = false; // preserve order against any sibling head scripts
      document.head.appendChild(fresh);
      present[fresh.src] = true;
    }
  }

  /* Swap the parsed document into the live page. Returns true on success. */
  function applyDocument(html) {
    var doc = new DOMParser().parseFromString(html, "text/html");
    if (!doc || !doc.body) return false;
    var title = doc.querySelector("title");
    if (title) document.title = title.textContent || document.title;
    // Tear down the OUTGOING page's timers + fire pagehide BEFORE we drop its
    // DOM, so its pollers stop and any per-page cleanup runs while its handlers
    // are still attached (BLOCKER: leaked pollers / MAJOR: stale listeners).
    teardownPage();
    syncHeadAssets(doc.head);
    // Replace the whole body content in one shot, then boot its scripts.
    document.body.innerHTML = doc.body.innerHTML;
    runScripts(document.body);
    window.scrollTo(0, 0);
    return true;
  }

  /* Fetch + swap a URL. On ANY failure, do a hard navigation (full reload) so
   * the user always gets there — the progressive-enhancement contract. */
  function navigate(url, push) {
    fetch(url, { headers: { "X-Requested-With": "bx-nav" }, credentials: "same-origin" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("status " + resp.status);
        var ct = resp.headers.get("content-type") || "";
        if (ct.indexOf("text/html") === -1) throw new Error("not html");
        return resp.text();
      })
      .then(function (html) {
        if (!applyDocument(html)) throw new Error("parse failed");
        if (push) {
          window.history.pushState({ bxNav: true }, "", url);
        }
      })
      .catch(function () {
        // Graceful fallback: a normal full navigation always works.
        window.location.href = url;
      });
  }

  /* bx-nav's OWN document-level listeners are registered exactly ONCE per page
   * lifetime: bx-nav.js loads from <head> (shared on every page, deduped by src
   * in syncHeadScripts so a swap never re-adds it) and the `SWAP_FLAG` guard
   * above hard-stops a second init. They are NOT touched by teardownPage (that
   * clears page pollers, not the nav controller) and survive every swap intact,
   * so they can never accumulate (MAJOR: no duplicated bx-nav handlers). */
  document.addEventListener("click", function (event) {
    var url = navTarget(event);
    if (!url) return;
    event.preventDefault();
    navigate(url, true);
  });

  // Back/forward: re-fetch + swap the popped URL (replaceState semantics: we do
  // not push again). A failure falls back to a full reload via navigate().
  window.addEventListener("popstate", function () {
    navigate(window.location.href, false);
  });

  // Seed the initial state so the very first back press has somewhere to go.
  try {
    window.history.replaceState(
      Object.assign({}, window.history.state, { bxNav: true }),
      "",
      window.location.href
    );
  } catch (e) {
    /* replaceState can throw in exotic sandboxes; the click+popstate path still
       works without the seed, so swallow and continue (no-op). */
  }
})();
