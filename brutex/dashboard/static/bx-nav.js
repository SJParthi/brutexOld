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

  /* Reconcile the <head> page-specific assets (per-page <style> + <link
   * rel=stylesheet>) so the swapped body is themed correctly. The shared
   * /static/bx-theme.css + bx-nav.js are already present on every page, so we
   * only ADD the assets the incoming page declares that the current head lacks,
   * keyed by a stable signature; we never tear the head down (avoids a flash). */
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
  }

  /* Swap the parsed document into the live page. Returns true on success. */
  function applyDocument(html) {
    var doc = new DOMParser().parseFromString(html, "text/html");
    if (!doc || !doc.body) return false;
    var title = doc.querySelector("title");
    if (title) document.title = title.textContent || document.title;
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
