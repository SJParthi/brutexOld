/* bx-health-hub.js — the unified operator HEALTH HUB widget
 * (DEC-OPERATOR-HEALTH-TRUTH-001, MIGRATION.md #1747).
 *
 * ONE BXPoll-disciplined poller over GET /api/v1/health/hub (the
 * DEC-FRONTEND-LATENCY-HARDENING-001 / #1720 law: one poller per endpoint —
 * never a bare setInterval; hidden tab fires zero fetches; failed ticks back
 * off 3 s -> 30 s; a 10 s fetch abort is armed through the body read), with
 * the house subscriber fan-outs so any future page consumer SUBSCRIBES
 * instead of minting a second poller:
 *
 *   (window.__bxHealthHubSubs ||= []).push(function (payload) { ... });
 *   (window.__bxHealthHubFailSubs ||= []).push(function (failures) { ... });
 *
 * Renders into the operator page's #bxHealthHub panel (absent panel = no-op;
 * the file is included on v29 only). Every DOM write is
 * textContent/createElement — NEVER innerHTML for data (the XSS guard).
 *
 * HONESTY LAW (mirrors the backend): a lane with no local signal renders the
 * explicit grey "not wired"; an unreadable signal renders amber "unknown";
 * a degraded lane LOOKS degraded — nothing is ever painted green without a
 * positive recorded verdict.
 *
 * Pure helpers (verdict -> dot/badge classes, age text, fold text) are
 * node-golden-tested via the CommonJS export below; the stateful widget is
 * browser-only and pinned by source-string tests (the house #1529/#1561
 * envelope — no headless JS runner in CI).
 */
"use strict";

/**
 * Lane verdict -> live-dot state class suffix ("ok" | "warn" | "down" | "").
 * Matches the heartbeat's bx-live-dot classes: ok=green, warn=amber,
 * down=red, "" = neutral grey (idle / not_wired / unknown-but-see-badge —
 * unknown gets the WARN dot: an unreadable signal must look degraded).
 *
 * @param {string} verdict - one of ok|warn|bad|unknown|idle|not_wired.
 * @returns {string} the dot state suffix.
 */
function bxHealthDotState(verdict) {
  if (verdict === "ok") { return "ok"; }
  if (verdict === "warn" || verdict === "unknown") { return "warn"; }
  if (verdict === "bad") { return "down"; }
  return ""; // idle / not_wired / anything else: neutral grey
}

/**
 * Lane token -> operator-facing label (unknown tokens pass through verbatim
 * so a future backend lane is never hidden).
 *
 * @param {string} lane - the machine lane token.
 * @returns {string} the display label.
 */
function bxHealthLaneLabel(lane) {
  var labels = {
    pull: "Pull outcome",
    nightly: "Nightly chain",
    backup_deadman: "Backup dead-man",
    doctor_daily: "Doctor daily",
    mutation: "Mutation gate"
  };
  return Object.prototype.hasOwnProperty.call(labels, lane) ? labels[lane] : lane;
}

/**
 * Verdict -> short badge text ("not_wired" reads "not wired"; others verbatim).
 *
 * @param {string} verdict - the machine verdict token.
 * @returns {string} badge text.
 */
function bxHealthBadgeText(verdict) {
  return verdict === "not_wired" ? "not wired" : String(verdict);
}

/**
 * age_s -> compact "Xs/Xm/Xh/Xd ago" text; null/absent -> "" (no fabricated age).
 *
 * @param {number|null|undefined} ageS - seconds since the lane's signal.
 * @returns {string} the age text or "".
 */
function bxHealthAgeText(ageS) {
  if (typeof ageS !== "number" || !isFinite(ageS) || ageS < 0) { return ""; }
  if (ageS < 90) { return Math.round(ageS) + "s ago"; }
  if (ageS < 5400) { return Math.round(ageS / 60) + "m ago"; }
  if (ageS < 172800) { return (ageS / 3600).toFixed(1) + "h ago"; }
  return (ageS / 86400).toFixed(1) + "d ago";
}

/**
 * Fold block -> the pill text ("OK — 3 ok · 2 not wired" style).
 *
 * @param {object|null|undefined} fold - {verdict, headline} from the payload.
 * @returns {string} pill text ("—" when absent/malformed).
 */
function bxHealthFoldText(fold) {
  if (!fold || typeof fold.verdict !== "string") { return "—"; }
  var head = fold.verdict === "bad" ? "RED" : fold.verdict === "warn" ? "AMBER" : "OK";
  var tail = typeof fold.headline === "string" && fold.headline ? " — " + fold.headline : "";
  return head + tail;
}

/* ── Browser-only widget below (source-string-pinned, not executed in CI). ── */

function bxHealthHubInit() {
  var panel = document.getElementById("bxHealthHub");
  var body = document.getElementById("bxHealthHubBody");
  var foldPill = document.getElementById("bxHealthFold");
  var updated = document.getElementById("bxHealthUpdated");
  if (!panel || !body) { return; } // not the operator page — no-op
  if (window.location.protocol === "file:") {
    // Standalone design review: no endpoint exists — resolve honestly.
    while (body.firstChild) { body.removeChild(body.firstChild); }
    body.textContent = "offline preview — live health lanes available when served by the dashboard";
    return;
  }
  if (!window.BXPoll) {
    // bx-poll.js failed to load: no un-disciplined fallback poller is minted
    // (the #1720 law) — render the honest degraded note instead.
    while (body.firstChild) { body.removeChild(body.firstChild); }
    body.textContent = "health hub unavailable — shared poll core (bx-poll.js) did not load";
    return;
  }

  function notifyPageSubs(listName, arg) {
    var subs = window[listName];
    if (!subs || !subs.length) { return; }
    for (var i = 0; i < subs.length; i++) {
      try { subs[i](arg); } catch (e) { /* subscriber isolation */ }
    }
  }

  var lastSig = "";

  function renderLane(lane) {
    var row = document.createElement("div");
    row.className = "hh-row";
    var dot = document.createElement("span");
    dot.className = "bx-live-dot";
    var dotState = bxHealthDotState(lane.verdict);
    if (dotState) { dot.classList.add("live-" + dotState); }
    row.appendChild(dot);
    var label = document.createElement("span");
    label.className = "hh-lane";
    label.textContent = bxHealthLaneLabel(String(lane.lane));
    row.appendChild(label);
    var badge = document.createElement("span");
    badge.className = "hh-badge hh-badge-" + String(lane.verdict);
    badge.textContent = bxHealthBadgeText(String(lane.verdict));
    row.appendChild(badge);
    var main = document.createElement("span");
    main.className = "hh-main";
    main.textContent = String(lane.headline || "");
    row.appendChild(main);
    var noteText = String(lane.detail || "");
    var age = bxHealthAgeText(lane.age_s);
    if (age) { noteText = noteText ? noteText + " · " + age : age; }
    if (noteText) {
      var note = document.createElement("span");
      note.className = "hh-note";
      note.textContent = noteText;
      row.appendChild(note);
    }
    return row;
  }

  function render(payload) {
    var lanes = Array.isArray(payload.lanes) ? payload.lanes : [];
    var sig = JSON.stringify(payload.lanes) + "|" + JSON.stringify(payload.fold);
    if (sig !== lastSig) { // unchanged tick -> zero DOM writes (skip-render discipline)
      lastSig = sig;
      if (foldPill) {
        foldPill.textContent = bxHealthFoldText(payload.fold);
        foldPill.classList.remove("hh-fold-ok", "hh-fold-warn", "hh-fold-bad");
        var v = payload.fold && payload.fold.verdict;
        if (v === "ok" || v === "warn" || v === "bad") { foldPill.classList.add("hh-fold-" + v); }
      }
      while (body.firstChild) { body.removeChild(body.firstChild); }
      for (var i = 0; i < lanes.length; i++) {
        var lane = lanes[i];
        if (lane && typeof lane === "object") { body.appendChild(renderLane(lane)); }
      }
    }
    if (updated) { updated.textContent = "updated just now"; }
    notifyPageSubs("__bxHealthHubSubs", payload);
  }

  var poller = window.BXPoll.createPoller({
    url: "/api/v1/health/hub",
    pollMs: 5000,
    onData: render,
    onFail: function (failures) {
      if (updated) {
        updated.textContent = "health feed unreachable — retrying (backoff capped at 30 s)";
      }
      notifyPageSubs("__bxHealthHubFailSubs", failures);
    }
  });
  window.BXPoll.stalenessTicker(poller, function (s) {
    if (!updated) { return; }
    if (s.level !== "fresh") { updated.textContent = "updated " + s.text; }
  });
  poller.start();
}

var BXHealthHub = {
  dotState: bxHealthDotState,
  laneLabel: bxHealthLaneLabel,
  badgeText: bxHealthBadgeText,
  ageText: bxHealthAgeText,
  foldText: bxHealthFoldText
};

/* Browsers boot the widget; a non-browser harness (the node golden) gets the
   CommonJS export (mirrors bx-poll.js / bx-format.js). */
if (typeof window !== "undefined" && typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bxHealthHubInit);
  } else {
    bxHealthHubInit();
  }
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = BXHealthHub;
}
