/* BX shared date picker + custom-select component (DEC-UNIVERSE-DATEPICKER-SHARED-001).
   ONE source of truth for the TradingView/Go-to-style date picker (Date +
   Custom-range tabs; day->month->year drill nav) and custom <select> dropdowns,
   loaded by every dashboard page. Pure progressive enhancement: it drives the
   page's HIDDEN native controls (the value source); if it throws, the native
   controls still work.

   UNIVERSAL auto-wiring (no per-input markup needed):
     * a container with data-bxdp="range" data-from="<id>" data-to="<id>"
       becomes a single from->to RANGE picker;
     * EVERY other <input type="date"> becomes its own single-date picker;
     * a <select data-bxsel> becomes a custom dropdown.
   A MutationObserver re-runs the wiring so inputs injected later (e.g. the /db
   candle controls) are enhanced too. */
(function () {
  "use strict";
  /* HTML-escape any string before it is interpolated into innerHTML — the SAME
     helper the dashboard templates + bx-ui.js ship (CLAUDE.md §11 COMMON, the
     CodeQL/XSS guard). An <option> text like `NIFTY<img src=x onerror=…>`
     (a vendor-derived instrument/expiry/contract label populated the safe
     textContent way, whose `.text` returns the raw `<`) renders as inert text
     in the custom dropdown, never a live tag. */
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function ymd(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
  function parse(s) { if (!s) return null; var p = String(s).split("-"); if (p.length !== 3) return null; var d = new Date(+p[0], (+p[1]) - 1, +p[2]); return isNaN(d.getTime()) ? null : d; }
  function fmt(d) { return d ? d.toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short", year: "2-digit" }) : "—"; }
  function same(a, b) { return a && b && a.getFullYear() == b.getFullYear() && a.getMonth() == b.getMonth() && a.getDate() == b.getDate(); }
  function cl(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
  var MON = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  var MON3 = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  /* ONE delegated pair of document-level listeners for ALL picker/select
     instances (rebuild leak fix): each buildCalendar/buildSelect previously
     added its OWN document click/keydown listener and never removed it, so a
     page that REBUILDS its controls (the MutationObserver re-wires the fresh
     nodes) accumulated document listeners whose closures also retained every
     replaced instance's detached DOM tree (PR #1368's author measured this
     in headless Chromium — quoted here, not re-measured: 4,001 document
     listeners after 1,000 rebuilds; ~103 surviving detached nodes per
     rebuild; document click dispatch degraded 0.20 ms -> 4.60 ms).
     Instances now REGISTER here; the two document listeners are attached
     exactly ONCE, and an entry whose root has left the document is pruned on
     every document event AND on every auto() re-wire pass — releasing the
     replaced tree promptly with no behaviour change for live instances. */
  var docTargets = [];
  var docWired = false;
  function pruneDocTargets() {
    for (var i = docTargets.length - 1; i >= 0; i--) {
      if (!docTargets[i].root.isConnected) docTargets.splice(i, 1);
    }
  }
  function onDocClick(e) {
    for (var i = docTargets.length - 1; i >= 0; i--) {
      var t = docTargets[i];
      if (!t.root.isConnected) { docTargets.splice(i, 1); continue; }
      if (!t.isInside(e)) t.close();
    }
  }
  function onDocKeydown(e) {
    if (e.key != "Escape") return;
    for (var i = docTargets.length - 1; i >= 0; i--) {
      var t = docTargets[i];
      if (!t.root.isConnected) { docTargets.splice(i, 1); continue; }
      if (t.esc) t.close();
    }
  }
  function registerDocTarget(entry) {
    /* Stamp the entry on its root so a detached-then-REATTACHED wrap can be
       re-registered by reattachDocTargets() — pruning drops the entry while
       the wrap is detached, and the dataset.dpDone/dataset.cs enhance-dedup
       (correctly) blocks a second buildCalendar/buildSelect on reattach, so
       without the stamp the live node would lose its outside-click/Escape
       close forever. */
    entry.root._bxDocTarget = entry;
    docTargets.push(entry);
    if (docWired) return;
    docWired = true;
    document.addEventListener("click", onDocClick);
    document.addEventListener("keydown", onDocKeydown);
  }
  /* Re-push the stamped entry of every already-enhanced LIVE wrap that is
     missing from docTargets (a node pruned while detached, now reattached).
     Runs on every auto() pass — the same cadence as pruning — so doc-target
     registration is decoupled from the enhance-dedup. Document-wide by design
     (a reattach can land outside the MutationObserver scope that triggered
     the pass); O(enhanced wraps) per pass, duplicate-guarded. */
  function reattachDocTargets() {
    var wraps = document.querySelectorAll(".dp-wrap, .cs-wrap");
    for (var i = 0; i < wraps.length; i++) {
      var entry = wraps[i]._bxDocTarget;
      if (entry && wraps[i].isConnected && docTargets.indexOf(entry) === -1) docTargets.push(entry);
    }
  }

  /* Core: mount the picker into `mount` (the mount's own contents are NOT touched
     here — the wiring helper hides the native control before calling). */
  function buildCalendar(mount, fromInput, toInput, single) {
    var minD = parse(fromInput.min) || new Date(2020, 0, 1);
    var maxD = parse((single ? fromInput : toInput).max) || new Date(2099, 11, 31);
    var start = parse(fromInput.value) || (single ? new Date() : minD);
    var end = single ? start : (parse(toInput.value) || maxD);
    if (start < minD) start = minD; if (start > maxD) start = maxD;
    if (!single) { if (end < minD) end = minD; if (end > maxD) end = maxD; }
    var wrap = document.createElement("div"); wrap.className = "dp-wrap";
    var trig = document.createElement("button"); trig.type = "button"; trig.className = "dp-trigger"; trig.setAttribute("aria-label", "Choose date");
    var pop = document.createElement("div"); pop.className = "dp-pop";
    wrap.appendChild(trig); wrap.appendChild(pop); mount.appendChild(wrap);
    var mode = single ? "date" : "range", active = "from", level = "days", view = new Date(end.getFullYear(), end.getMonth(), 1);
    var presets = [
      ["Full history", function () { return [minD, maxD]; }],
      ["YTD", function () { return [new Date(maxD.getFullYear(), 0, 1), maxD]; }],
      ["1Y", function () { var s = new Date(maxD); s.setFullYear(s.getFullYear() - 1); return [s, maxD]; }],
      ["6M", function () { var s = new Date(maxD); s.setMonth(s.getMonth() - 6); return [s, maxD]; }],
      ["3M", function () { var s = new Date(maxD); s.setMonth(s.getMonth() - 3); return [s, maxD]; }],
      ["30D", function () { var s = new Date(maxD); s.setDate(s.getDate() - 30); return [s, maxD]; }]
    ];
    function setTrig() {
      /* An EMPTY backing input renders NO date ("—"), never today's date — an
         empty filter must not masquerade as a live one-day filter (the
         inert-filter illusion; DEC-DB-UX-PACK-001 Feature 2). The internal
         `start`/`end` defaults are only the calendar's navigation position. */
      var sTxt = fromInput.value ? fmt(start) : "—";
      if (single) {
        trig.innerHTML = '<span aria-hidden="true">📅</span> <b>' + sTxt + '</b> <span class="dp-car">▾</span>';
        return;
      }
      var eTxt = toInput.value ? fmt(end) : "—";
      trig.innerHTML = '<span aria-hidden="true">📅</span> <b>' + sTxt + '</b> <span class="dp-sep">→</span> <b>' + eTxt + '</b> <span class="dp-car">▾</span>';
    }
    function decade(y) { return y - (y % 20); }
    function headLabel() { if (level == "days") return MON[view.getMonth()] + " " + view.getFullYear(); if (level == "mons") return "" + view.getFullYear(); var d = decade(view.getFullYear()); return d + " - " + (d + 19); }
    function gridHtml() {
      if (level == "days") {
        var y = view.getFullYear(), m = view.getMonth(), lead = (new Date(y, m, 1).getDay() + 6) % 7;
        var h = '<div class="dp-dow"><span>Mo</span><span>Tu</span><span>We</span><span>Th</span><span>Fr</span><span>Sa</span><span>Su</span></div><div class="dp-grid days">';
        for (var i = 0; i < 42; i++) {
          var d = new Date(y, m, 1 - lead + i), inM = d.getMonth() == m, dd = cl(d), cls = "dp-day" + (inM ? "" : " out");
          if (dd < cl(minD) || dd > cl(maxD)) cls += " disabled";
          if (same(d, new Date())) cls += " today";
          if (mode == "range") { if (start && end && dd > cl(start) && dd < cl(end)) cls += " inrange"; if (same(d, start)) cls += " start"; if (same(d, end)) cls += " end"; }
          else { if (same(d, start)) cls += " sel"; }
          h += '<div class="' + cls + '" data-day="' + ymd(d) + '">' + d.getDate() + '</div>';
        }
        return h + '</div>';
      }
      if (level == "mons") {
        var yy = view.getFullYear(), h2 = '<div class="dp-grid mons">', n = new Date();
        for (var mm = 0; mm < 12; mm++) {
          var first = cl(new Date(yy, mm, 1)), last = cl(new Date(yy, mm + 1, 0)), c = "dp-cell";
          if (last < cl(minD) || first > cl(maxD)) c += " disabled";
          if (n.getFullYear() == yy && n.getMonth() == mm) c += " now";
          if (start.getFullYear() == yy && start.getMonth() == mm) c += " on";
          h2 += '<div class="' + c + '" data-mon="' + mm + '">' + MON3[mm] + '</div>';
        }
        return h2 + '</div>';
      }
      var base = decade(view.getFullYear()), h3 = '<div class="dp-grid yrs">', ny = new Date().getFullYear();
      for (var k = 0; k < 20; k++) {
        var Y = base + k, cc = "dp-cell";
        if (Y < minD.getFullYear() || Y > maxD.getFullYear()) cc += " disabled";
        if (ny == Y) cc += " now"; if (start.getFullYear() == Y) cc += " on";
        h3 += '<div class="' + cc + '" data-yr="' + Y + '">' + Y + '</div>';
      }
      return h3 + '</div>';
    }
    function fieldsHtml() {
      if (mode == "range") return '<div class="dp-fields"><div class="dp-field' + (active == "from" ? " act" : "") + '" data-f="from"><span class="dp-fi">📅</span>' + fmt(start) + '</div><span class="dp-farrow">→</span><div class="dp-field' + (active == "to" ? " act" : "") + '" data-f="to"><span class="dp-fi">📅</span>' + fmt(end) + '</div></div>';
      return '<div class="dp-fields"><div class="dp-field act" data-f="from"><span class="dp-fi">📅</span>' + fmt(start) + '</div></div>';
    }
    function quickHtml() { if (mode != "range") return ""; return '<div class="dp-quick">' + presets.map(function (p, i) { var r = p[1](), on = (start && end && same(r[0], start) && same(r[1], end)); return '<button class="dp-chip' + (on ? " on" : "") + '" data-p="' + i + '">' + p[0] + '</button>'; }).join("") + '</div>'; }
    function footHtml() { var days = Math.round((cl(end) - cl(start)) / 86400000) + 1, ro = mode == "range" ? ('<b>' + fmt(start) + '</b> → <b>' + fmt(end) + '</b> · <b>' + (days > 0 ? days.toLocaleString("en-IN") : 0) + '</b> days') : ('<b>' + fmt(start) + '</b>'); return '<div class="dp-foot"><div class="dp-readout">' + ro + '</div><div class="dp-actions"><button class="dp-btn" data-x="cancel">Cancel</button><button class="dp-btn apply" data-x="apply">Go to</button></div></div>'; }
    function render() {
      var tabs = single ? "" : '<div class="dp-tabs"><button class="dp-tab' + (mode == "range" ? " on" : "") + '" data-m="range">Custom range</button><button class="dp-tab' + (mode == "date" ? " on" : "") + '" data-m="date">Date</button></div><div class="dp-tabline"></div>';
      pop.innerHTML = tabs + fieldsHtml() + quickHtml() + '<div class="dp-cal"><div class="dp-head"><button class="dp-nav" data-nav="-1" aria-label="Previous">‹</button><button class="dp-hbtn" data-drill="1">' + headLabel() + '</button><button class="dp-nav" data-nav="1" aria-label="Next">›</button></div>' + gridHtml() + '</div>' + footHtml();
    }
    function setDay(s) {
      var d = parse(s); if (!d) return; var dd = cl(d); if (dd < cl(minD) || dd > cl(maxD)) return;
      if (mode == "date") { start = d; end = d; render(); return; }
      if (active == "from") { start = d; if (end && end < start) end = start; active = "to"; }
      else { end = d; if (end < start) { var t = start; start = end; end = t; } active = "from"; }
      render();
    }
    function close() { wrap.classList.remove("open"); }
    function open() { var f = (mode == "date" ? start : (active == "to" ? end : start)); level = "days"; view = new Date(f.getFullYear(), f.getMonth(), 1); render(); wrap.classList.add("open"); }
    pop.addEventListener("click", function (e) {
      e.stopPropagation();  /* inside clicks must NOT reach the outside-close handler (the popover re-renders, detaching e.target) */
      var t = e.target.closest("[data-m],[data-f],[data-p],[data-drill],[data-nav],[data-day],[data-mon],[data-yr],[data-x]"); if (!t) return;
      if (t.dataset.m) { mode = t.dataset.m; active = "from"; level = "days"; render(); return; }
      if (t.dataset.f) { active = t.dataset.f; level = "days"; var g = (active == "to" ? end : start); view = new Date(g.getFullYear(), g.getMonth(), 1); render(); return; }
      if (t.dataset.p != null) { var r = presets[+t.dataset.p][1](); start = r[0]; end = r[1]; active = "from"; render(); return; }
      if (t.dataset.drill) { level = (level == "days" ? "mons" : (level == "mons" ? "yrs" : "yrs")); render(); return; }
      if (t.dataset.nav) { var n = +t.dataset.nav; if (level == "days") view = new Date(view.getFullYear(), view.getMonth() + n, 1); else if (level == "mons") view = new Date(view.getFullYear() + n, view.getMonth(), 1); else view = new Date(view.getFullYear() + n * 20, view.getMonth(), 1); render(); return; }
      if (t.dataset.day != null && !t.classList.contains("disabled")) { setDay(t.dataset.day); return; }
      if (t.dataset.mon != null && !t.classList.contains("disabled")) { view = new Date(view.getFullYear(), +t.dataset.mon, 1); level = "days"; render(); return; }
      if (t.dataset.yr != null && !t.classList.contains("disabled")) { view = new Date(+t.dataset.yr, view.getMonth(), 1); level = "mons"; render(); return; }
      if (t.dataset.x == "cancel") { close(); return; }
      if (t.dataset.x == "apply") { fromInput.value = ymd(start); if (!single) toInput.value = ymd(end); setTrig(); close(); try { fromInput.dispatchEvent(new Event("change")); if (!single) toInput.dispatchEvent(new Event("change")); } catch (_) { } return; }
    });
    trig.addEventListener("click", function (e) { e.stopPropagation(); wrap.classList.contains("open") ? close() : open(); });
    /* A loose <input type=date> nested in a <label> re-fires drill clicks at the hidden native input; that label-forwarded click bubbles here with e.target === our own input (a sibling of wrap), tricking the outside-close. Treat the picker's own backing input(s) as INSIDE so the drill never self-closes on /movers + /reports (DEC-DATEPICKER-LABEL-CLOSE-FIX-001). The outside-close + Escape handlers ride the ONE delegated document listener pair (rebuild leak fix — see registerDocTarget). */
    registerDocTarget({
      root: wrap,
      isInside: function (e) { return wrap.contains(e.target) || e.target === fromInput || (!single && e.target === toInput); },
      close: close,
      esc: true
    });
    setTrig();
  }

  function wireRange(container) {
    if (container.dataset.dpDone) return;
    var f = document.getElementById(container.dataset.from), t = document.getElementById(container.dataset.to);
    if (!f || !t) return; container.dataset.dpDone = "1";
    Array.prototype.forEach.call(container.children, function (c) { c.style.display = "none"; });
    f.dataset.dpDone = "1"; t.dataset.dpDone = "1";
    buildCalendar(container, f, t, false);
  }
  function wireLooseInput(input) {
    if (input.dataset.dpDone || input.closest("[data-bxdp]")) return; input.dataset.dpDone = "1";
    var mount = document.createElement("span"); mount.className = "dp-mount";
    input.parentNode.insertBefore(mount, input.nextSibling);
    input.style.display = "none";
    buildCalendar(mount, input, null, true);
  }
  function buildSelect(sel) {
    if (!sel || sel.dataset.cs) return; sel.dataset.cs = "1"; sel.style.display = "none";
    var wrap = document.createElement("span"); wrap.className = "cs-wrap";
    var trig = document.createElement("button"); trig.type = "button"; trig.className = "cs-trigger"; trig.setAttribute("aria-label", "Choose option");
    var menu = document.createElement("div"); menu.className = "cs-menu";
    function label() { return sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : ""; }
    function setTrig() { trig.innerHTML = '<b>' + esc(label()) + '</b> <span class="cs-car">▾</span>'; }
    function build() { menu.innerHTML = Array.prototype.map.call(sel.options, function (o, i) { return '<div class="cs-opt' + (i == sel.selectedIndex ? " on" : "") + '" data-i="' + i + '">' + esc(o.text) + '</div>'; }).join(""); }
    function close() { wrap.classList.remove("open"); }
    menu.addEventListener("click", function (e) { var t = e.target.closest("[data-i]"); if (!t) return; sel.selectedIndex = +t.dataset.i; setTrig(); build(); close(); try { sel.dispatchEvent(new Event("change")); } catch (_) { } });
    trig.addEventListener("click", function (e) { e.stopPropagation(); build(); wrap.classList.toggle("open"); });
    wrap.appendChild(trig); wrap.appendChild(menu); sel.parentNode.insertBefore(wrap, sel.nextSibling); setTrig();
    registerDocTarget({
      root: wrap,
      isInside: function (e) { return wrap.contains(e.target); },
      close: close,
      esc: false
    });
  }

  function auto(root) {
    pruneDocTargets();  /* release replaced instances' registry entries (and their detached trees) on every re-wire pass */
    reattachDocTargets();  /* re-register enhanced wraps that were pruned while detached and have since REATTACHED */
    var scope = root && root.querySelectorAll ? root : document;
    Array.prototype.forEach.call(scope.querySelectorAll('[data-bxdp="range"]'), function (el) { try { wireRange(el); } catch (e) { } });
    Array.prototype.forEach.call(scope.querySelectorAll('input[type="date"]'), function (i) { try { wireLooseInput(i); } catch (e) { } });
    Array.prototype.forEach.call(scope.querySelectorAll("[data-bxsel]"), function (s) { try { buildSelect(s); } catch (e) { } });
  }

  window.BXDatePicker = {
    range: function (f, t, c) { if (c) { c.dataset.from = f.id; c.dataset.to = t.id; wireRange(c); } },
    single: function (i) { wireLooseInput(i); },
    select: function (s) { buildSelect(s); },
    auto: auto
  };

  /* Re-wire when the DOM changes (dynamic controls, e.g. /db candle filters). */
  function observe() {
    if (!window.MutationObserver) return;
    var pending = false;
    var obs = new MutationObserver(function () { if (pending) return; pending = true; setTimeout(function () { pending = false; auto(); }, 0); });
    obs.observe(document.body, { childList: true, subtree: true });
  }
  function start() { auto(); observe(); }
  if (document.readyState !== "loading") start(); else document.addEventListener("DOMContentLoaded", start);
})();
