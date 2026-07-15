/*
 * bx-liveproof.js — the ZERO-COMMAND live-proof status element
 * (DEC-DOCTOR-LIVE-AUTORUN-001).
 *
 * A self-contained, dependency-free, self-injecting widget every dashboard page
 * loads with a single <script defer src="/static/bx-liveproof.js"></script>
 * (the same site-wide pattern as bx-rate-monitor.js). It polls
 * GET /api/v1/liveproof every ~5 s and renders, in a fixed pill at the
 * bottom-LEFT of the page (the rate monitor owns the bottom-right):
 *
 *   - the startup PREFLIGHT verdict of the Mac live-proof kit's cheap stages
 *     (env / creds / totp / token-read+smoke / instrument master), which the
 *     dashboard runs BY ITSELF on boot — the operator runs no command;
 *   - the one-time PROBES verdict (the VIX hard-gate, riding the
 *     creds/token/spot-1m prerequisites), auto-run around the first REAL
 *     pull and then cached. The 2020 1m depth probe and the NSE bhavcopy
 *     cross-check are NOT autorun stages (operator decree 2026-07-03,
 *     DEC-DOCTOR-KIT-HONESTY-001 amendment — manual `brutex doctor --live`
 *     only), so neither can ever surface here; the rendering below is
 *     stage-name-generic and simply shows whatever stages the verdict
 *     carries;
 *   - a RED top banner whenever either verdict is fail/error, showing the
 *     kit's diagnostic lines VERBATIM (the golden-tested catalog is the only
 *     wording source — this file never re-words a diagnosis).
 *
 * HONEST rendering (Golden Rules #1/#10): green appears ONLY for a backend
 * verdict whose kind is "pass" (the stages truly ran against the real
 * vendor); "not_applicable" (no credentials in the NEVER-ATTEMPTED sandbox
 * context) renders grey with the backend's own headline "live-proof: not
 * applicable (no credentials)" and the relayed CredentialsError fix text —
 * never a fake green. Broken credentials on a REAL pull attempt (or after
 * live-proof history) arrive as kind "fail", so the red banner opens with
 * the refusal text — grey never papers over a broken live pipe. The pill
 * dot shows the WORST of the preflight and probes verdicts (worstClass),
 * and the pill HEADLINE is likewise ONE flow-scoped worst-of line
 * ("live-proof — preflight: PASS · probes: FAIL — vix[R5]",
 * DEC-DOCTOR-KIT-HONESTY-001) so a mixed run can never read as a
 * contradictory PASS-then-FAIL toast sequence.
 * When the autorun is not wired (present:false) or disabled via the
 * BRUTEX_AUTORUN_LIVEPROOF=0 escape hatch, the widget renders NOTHING (no
 * invented state). The widget is read-only + best-effort and never throws
 * into the page.
 *
 * STALENESS HONESTY (DEC-PULL-UI-STALENESS-001 ported from bx-rate-monitor.js,
 * Golden Rule #1): a non-ok status, a fetch rejection AND a malformed body are
 * ALL counted failures that feed a client-clock staleness readout ("updated Xs
 * ago" → amber past STALE_WARN_MS → LOUD "STALE" past STALE_RED_MS, which ALSO
 * recolours the pill dot so a green "pass" can NEVER keep reading fresh when the
 * backend has gone silent — even collapsed to the dot badge) and a
 * self-recovering capped exponential backoff (3 s → 30 s) that NEVER gives up;
 * the last real VERDICT is retained (it may still hold) — only the FRESHNESS
 * marking escalates. The document.hidden guard is preserved (a background tab
 * fires zero fetches, re-arms at normal cadence, and a visibilitychange
 * catch-up poll clears the age on return).
 *
 * The ✕ on the red banner only COLLAPSES it to the pill (which stays red);
 * a NEW failing verdict (a different checked_at) re-opens it. No action
 * buttons: this widget displays the proof, it never re-runs it (startup +
 * the first real pull are the only triggers).
 *
 * NEVER-OCCLUDE (DEC-UNIVERSE-UI-POLISH-002): the full pill at bottom-left
 * OVERLAPPED page content (the /universe instruments list's symbol rows sit
 * exactly there — operator screenshot 2026-07-03). The pill now AUTO-COLLAPSES
 * to a compact dot-only badge (`bxlp-min`) AUTO_MIN_MS after a verdict is
 * shown, so at steady state only a small status dot sits in the corner —
 * visible but unobtrusive. Hovering the badge expands it back to the full
 * pill (CSS-only), a click still opens the detail card, and a NEW verdict
 * (changed kind/checked_at signature) re-expands it + restarts the timer.
 * The RED top banner is untouched — a failing verdict stays loud.
 */
(function () {
  "use strict";

  var ENDPOINT = "/api/v1/liveproof";
  var POLL_MS = 5000;
  // Bounded-fetch deadline (DEC-WINDOWS-COUNTS-ONLY-001 M15 — the sibling
  // bx-rate-monitor.js fetchJson discipline): armed through the BODY read.
  var FETCH_TIMEOUT_MS = 10000;
  // Auto-collapse the pill to the compact dot badge this long after a verdict
  // is first shown (DEC-UNIVERSE-UI-POLISH-002 — never occlude page content).
  var AUTO_MIN_MS = 8000;
  // Client-side staleness + self-recovering backoff (DEC-PULL-UI-STALENESS-001,
  // ported from bx-rate-monitor.js). STALE_RED_MS == BACKOFF_MAX_MS so the
  // "retrying" tail reads true once the readout goes red.
  var STALE_WARN_MS = 15000;   // amber: ~3 missed 5 s polls
  var STALE_RED_MS = 30000;    // LOUD "STALE": 6x POLL_MS; == BACKOFF_MAX_MS
  var BACKOFF_BASE_MS = 3000;  // failure backoff base, doubling per consecutive failure
  var BACKOFF_MAX_MS = 30000;  // backoff cap: next attempt <= 30 s after the previous settle
  var FETCH_TIMEOUT_MS = 10000; // abort bound (headers AND body) → a wedged fetch is a counted failure
  var AGE_TICK_MS = 1000;      // the ~1 s client staleness ticker
  var WIDGET_ID = "bxLiveProof";
  var BANNER_ID = "bxLiveProofBanner";
  // The NON-critical amber NOTICE strip (DEC-AUTO-RESUME-ZERO-SURPRISES-001):
  // renders a resume payload whose level is "notice" (announce-on-launch of a
  // boot auto-resume / a pull stop request) as a dismissible amber top strip —
  // informational, never the red critical banner. The backend banner holds ONE
  // payload, so "notice" and "critical" are mutually exclusive by construction.
  var NOTICE_ID = "bxLiveProofNotice";
  var DETAIL_ID = "bxLiveProofDetail";

  // Idempotent guard: if two copies somehow load, only one widget is built.
  if (typeof document === "undefined" || document.getElementById(WIDGET_ID)) {
    return;
  }

  var KIND_CLASS = {
    pass: "ok",
    warn: "warn",
    fail: "bad",
    error: "bad",
    not_applicable: "na",
  };

  // Severity order for the pill dot: bad > warn > na > ok. The dot shows the
  // WORST of the preflight and probes verdicts, so a red probes verdict can
  // never hide behind a green preflight dot (a probes verdict that does not
  // exist yet — pending — never drags a green preflight down).
  var KIND_RANK = { ok: 0, na: 1, warn: 2, bad: 3 };

  function worstClass(a, b) {
    return (KIND_RANK[b] || 0) > (KIND_RANK[a] || 0) ? b : a;
  }

  // UNIFIED HEADLINE (DEC-DOCTOR-KIT-HONESTY-001): the pill's headline TEXT is
  // one flow-scoped worst-of line ("live-proof — preflight: PASS · probes:
  // FAIL — vix[R5]") so the two flows sharing one pill can never read as a
  // contradictory PASS-then-FAIL toast sequence. The pill DOT already renders
  // worst-of via worstClass — this fixes the TEXT/labels only. textContent
  // rendering throughout (CodeQL XSS gate: no markup sink, ever).
  var KIND_LABEL = {
    pass: "PASS",
    warn: "PASS w/ warnings",
    fail: "FAIL",
    error: "ERROR",
    not_applicable: "N/A (no credentials)",
  };

  function failingStageLabels(verdict) {
    // "vix[R5]"-style pointers derived from the verdict's own FAIL lines
    // (the golden catalog's "FAIL <stage>[<register>]:" prefix); a verdict
    // whose lines carry no such prefix falls back to its FAILed stage names.
    // Pointers only — the diagnosis wording stays the kit's catalog verbatim.
    var labels = [];
    var seen = {};
    (verdict.lines || []).forEach(function (line) {
      var m = /^FAIL ([a-z0-9-]+)(\[[^\]]+\])?:/.exec(line);
      if (m && !seen[m[1]]) {
        seen[m[1]] = true;
        labels.push(m[1] + (m[2] || ""));
      }
    });
    if (!labels.length) {
      (verdict.stages || []).forEach(function (s) {
        if (s.status === "FAIL" && !seen[s.name]) {
          seen[s.name] = true;
          labels.push(s.name);
        }
      });
    }
    return labels.join(",");
  }

  function flowStatus(block, pendingText) {
    // One flow's SHORT status label for the unified headline, honest per
    // state (verdict kind > running > pending — mirrors verdictSummary).
    var verdict = block && block.verdict;
    var state = block && block.state;
    if (verdict) {
      var label = KIND_LABEL[verdict.kind] || verdict.kind;
      if (verdict.kind === "fail" || verdict.kind === "error") {
        var stages = failingStageLabels(verdict);
        if (stages) {
          label += " — " + stages;
        }
      }
      if (state === "running") {
        label += " (re-checking…)";
      }
      return label;
    }
    if (state === "running") {
      return "checking…";
    }
    return pendingText;
  }

  function unifiedHeadline(data) {
    return (
      "live-proof — preflight: " +
      flowStatus(data.preflight, "not yet run") +
      " · probes: " +
      flowStatus(data.probes, "pending")
    );
  }

  function injectStyles() {
    if (document.getElementById("bxLiveProofStyles")) {
      return;
    }
    var css =
      "#" + WIDGET_ID + "{position:fixed;left:16px;bottom:16px;z-index:9000;cursor:pointer;" +
      "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;" +
      "background:var(--surface-2,#1A1F2C);color:var(--text,#E5E8EC);" +
      "border:1px solid var(--border-strong,#2B313F);border-radius:var(--r-md,10px);" +
      "padding:8px 12px;box-shadow:0 6px 20px rgba(0,0,0,.45);max-width:46vw;" +
      "opacity:.96;display:none;}" +
      "#" + WIDGET_ID + ".shown{display:block;}" +
      "#" + WIDGET_ID + ":hover{opacity:1;}" +
      // Never-occlude collapse (DEC-UNIVERSE-UI-POLISH-002): after AUTO_MIN_MS
      // the pill shrinks to a dot-only badge so it stops covering the content
      // above it (e.g. the /universe instruments list). Hover expands it back.
      "#" + WIDGET_ID + ".bxlp-min{padding:7px;border-radius:999px;}" +
      "#" + WIDGET_ID + ".bxlp-min .bxlp-text," +
      "#" + WIDGET_ID + ".bxlp-min .bxlp-sub{display:none;}" +
      "#" + WIDGET_ID + ".bxlp-min:hover{padding:8px 12px;border-radius:var(--r-md,10px);}" +
      "#" + WIDGET_ID + ".bxlp-min:hover .bxlp-text{display:inline;}" +
      "#" + WIDGET_ID + ".bxlp-min:hover .bxlp-sub{display:inline;}" +
      "#" + WIDGET_ID + " .bxlp-row{display:flex;align-items:center;gap:7px;line-height:1.6;}" +
      "#" + WIDGET_ID + " .bxlp-dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto;" +
      "background:var(--muted,#787B86);}" +
      "#" + WIDGET_ID + " .bxlp-dot.ok{background:var(--green,#26D9B0);" +
      "box-shadow:0 0 7px var(--green,#26D9B0);}" +
      "#" + WIDGET_ID + " .bxlp-dot.warn{background:var(--yellow,#FFB74D);}" +
      "#" + WIDGET_ID + " .bxlp-dot.bad{background:var(--red,#FF5C5C);" +
      "box-shadow:0 0 7px var(--red,#FF5C5C);}" +
      "#" + WIDGET_ID + " .bxlp-dot.na{background:var(--muted,#787B86);}" +
      // Staleness override (DEC-PULL-UI-STALENESS-001): once the client-clock age
      // crosses STALE_WARN_MS/STALE_RED_MS the DOT recolours amber/red REGARDLESS
      // of the verdict kind, so a green "pass" dot can never keep reading fresh
      // when the backend has gone silent — visible even collapsed to the dot badge.
      "#" + WIDGET_ID + ".bxlp-stale-warn .bxlp-dot{background:var(--yellow,#FFB74D)!important;" +
      "box-shadow:none!important;}" +
      "#" + WIDGET_ID + ".bxlp-stale .bxlp-dot{background:var(--red,#FF5C5C)!important;" +
      "box-shadow:0 0 7px var(--red,#FF5C5C)!important;}" +
      "#" + WIDGET_ID + ".bxlp-stale .bxlp-age{color:var(--red,#FF5C5C);}" +
      "#" + WIDGET_ID + ".bxlp-stale-warn .bxlp-age{color:var(--yellow,#FFB74D);}" +
      "#" + WIDGET_ID + " .bxlp-text{white-space:nowrap;overflow:hidden;" +
      "text-overflow:ellipsis;}" +
      "#" + WIDGET_ID + " .bxlp-sub{color:var(--muted,#787B86);}" +
      "#" + WIDGET_ID + " .bxlp-age{color:var(--muted,#787B86);font-size:10px;}" +
      "#" + BANNER_ID + "{position:fixed;top:0;left:0;right:0;z-index:9200;display:none;" +
      "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;" +
      "background:#2A1215;color:#FFD7D7;border-bottom:2px solid var(--red,#FF5C5C);" +
      "padding:10px 44px 10px 16px;max-height:45vh;overflow:auto;}" +
      "#" + BANNER_ID + ".open{display:block;}" +
      "#" + BANNER_ID + " .bxlp-bhead{font-weight:700;color:var(--red,#FF5C5C);" +
      "margin-bottom:6px;}" +
      "#" + BANNER_ID + " .bxlp-line{white-space:pre-wrap;line-height:1.55;}" +
      "#" + BANNER_ID + " .bxlp-close{position:absolute;top:6px;right:12px;cursor:pointer;" +
      "background:none;border:none;color:#FFD7D7;font-size:20px;line-height:1;}" +
      // Amber NOTICE strip (DEC-AUTO-RESUME-ZERO-SURPRISES-001) — the same
      // top-strip pattern as the red banner, informational colours. z-index
      // one below the red banner (the two are mutually exclusive anyway —
      // the backend holds ONE resume payload).
      "#" + NOTICE_ID + "{position:fixed;top:0;left:0;right:0;z-index:9190;display:none;" +
      "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;" +
      "background:#2A2412;color:#FFE9C2;border-bottom:2px solid var(--yellow,#FFB74D);" +
      "padding:10px 44px 10px 16px;max-height:35vh;overflow:auto;}" +
      "#" + NOTICE_ID + ".open{display:block;}" +
      "#" + NOTICE_ID + " .bxlp-nhead{font-weight:700;color:var(--yellow,#FFB74D);" +
      "margin-bottom:6px;}" +
      "#" + NOTICE_ID + " .bxlp-line{white-space:pre-wrap;line-height:1.55;}" +
      "#" + NOTICE_ID + " .bxlp-close{position:absolute;top:6px;right:12px;cursor:pointer;" +
      "background:none;border:none;color:#FFE9C2;font-size:20px;line-height:1;}" +
      "#" + DETAIL_ID + "{position:fixed;inset:0;z-index:9100;display:none;align-items:center;" +
      "justify-content:center;background:rgba(0,0,0,.55);" +
      "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}" +
      "#" + DETAIL_ID + ".open{display:flex;}" +
      "#" + DETAIL_ID + " .bxlp-card{background:var(--surface-2,#1A1F2C);" +
      "color:var(--text,#E5E8EC);border:1px solid var(--border-strong,#2B313F);" +
      "border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.6);padding:22px 26px;" +
      "min-width:420px;max-width:92vw;max-height:90vh;overflow:auto;font-size:12px;}" +
      "#" + DETAIL_ID + " h3{margin:0 0 8px 0;font-size:12px;text-transform:uppercase;" +
      "letter-spacing:.05em;color:var(--muted,#787B86);}" +
      "#" + DETAIL_ID + " .bxlp-line{white-space:pre-wrap;line-height:1.55;margin:2px 0;}" +
      "#" + DETAIL_ID + " .bxlp-kind.ok{color:var(--green,#26D9B0);font-weight:700;}" +
      "#" + DETAIL_ID + " .bxlp-kind.warn{color:var(--yellow,#FFB74D);font-weight:700;}" +
      "#" + DETAIL_ID + " .bxlp-kind.bad{color:var(--red,#FF5C5C);font-weight:700;}" +
      "#" + DETAIL_ID + " .bxlp-kind.na{color:var(--muted,#787B86);font-weight:700;}" +
      "#" + DETAIL_ID + " .bxlp-stages{color:var(--muted,#787B86);margin:4px 0 12px 0;}" +
      "#" + DETAIL_ID + " .bxlp-close{float:right;cursor:pointer;background:none;border:none;" +
      "color:var(--muted,#787B86);font-size:20px;line-height:1;}";
    var style = document.createElement("style");
    style.id = "bxLiveProofStyles";
    style.textContent = css;
    document.head.appendChild(style);
  }

  // Programmatic createElement + textContent only — zero HTML-injection sink
  // (every backend string, incl. the kit's diagnostic lines, is rendered as
  // text, never parsed as markup).
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) {
      node.className = cls;
    }
    if (text != null) {
      node.textContent = text;
    }
    return node;
  }

  function buildWidget() {
    var pill = el("div", null, null);
    pill.id = WIDGET_ID;
    pill.setAttribute("aria-live", "polite");
    pill.title =
      "Zero-command live-proof: the dashboard runs the doctor-live health stages " +
      "itself (startup preflight + first-pull probes). Click for details.";
    var row1 = el("div", "bxlp-row", null);
    row1.appendChild(el("span", "bxlp-dot", null)).id = "bxlpDot";
    row1.appendChild(el("span", "bxlp-text", "live-proof: …")).id = "bxlpText";
    var row2 = el("div", "bxlp-row", null);
    row2.appendChild(el("span", "bxlp-sub", "")).id = "bxlpProbes";
    // Client-clock freshness readout (DEC-PULL-UI-STALENESS-001): "updated Xs ago"
    // → "STALE — …" — textContent only (CodeQL XSS gate).
    var row3 = el("div", "bxlp-row", null);
    row3.appendChild(el("span", "bxlp-age bxlp-sub", "")).id = "bxlpAge";
    pill.appendChild(row1);
    pill.appendChild(row2);
    pill.appendChild(row3);
    pill.addEventListener("click", openDetail);
    document.body.appendChild(pill);

    var banner = el("div", null, null);
    banner.id = BANNER_ID;
    var close = el("button", "bxlp-close", "×");
    close.setAttribute("type", "button");
    close.setAttribute("aria-label", "Collapse to the status pill");
    close.addEventListener("click", function (ev) {
      ev.stopPropagation();
      dismissedSignature = lastFailSignature;
      banner.classList.remove("open");
    });
    banner.appendChild(close);
    var body = el("div", null, null);
    body.id = "bxlpBannerBody";
    banner.appendChild(body);
    document.body.appendChild(banner);

    // Amber NOTICE strip (DEC-AUTO-RESUME-ZERO-SURPRISES-001) — the same
    // build/collapse pattern as the red banner above; the ✕ dismisses only
    // the CURRENT notice message (a changed message re-opens).
    var notice = el("div", null, null);
    notice.id = NOTICE_ID;
    var nClose = el("button", "bxlp-close", "×");
    nClose.setAttribute("type", "button");
    nClose.setAttribute("aria-label", "Dismiss this notice");
    nClose.addEventListener("click", function (ev) {
      ev.stopPropagation();
      dismissedNoticeSignature = lastNoticeSignature;
      notice.classList.remove("open");
    });
    notice.appendChild(nClose);
    var nBody = el("div", null, null);
    nBody.id = "bxlpNoticeBody";
    notice.appendChild(nBody);
    document.body.appendChild(notice);

    var detail = el("div", null, null);
    detail.id = DETAIL_ID;
    var card = el("div", "bxlp-card", null);
    var dClose = el("button", "bxlp-close", "×");
    dClose.setAttribute("type", "button");
    dClose.setAttribute("aria-label", "Close");
    dClose.addEventListener("click", closeDetail);
    card.appendChild(dClose);
    var cardBody = el("div", null, null);
    cardBody.id = "bxlpDetailBody";
    card.appendChild(cardBody);
    detail.appendChild(card);
    detail.addEventListener("click", function (ev) {
      if (ev.target === detail) {
        closeDetail();
      }
    });
    document.body.appendChild(detail);
  }

  function openDetail() {
    var d = document.getElementById(DETAIL_ID);
    if (d && lastData) {
      renderDetail(lastData);
      d.classList.add("open");
    }
  }

  function closeDetail() {
    var d = document.getElementById(DETAIL_ID);
    if (d) {
      d.classList.remove("open");
    }
  }

  function fmtWhen(iso) {
    if (typeof iso !== "string" || !iso) {
      return "";
    }
    var t = new Date(iso);
    return isNaN(t.getTime()) ? iso : t.toLocaleString();
  }

  function verdictSummary(block, pendingLabel) {
    // → {cls, text} for one preflight/probes block, honest per state.
    var verdict = block && block.verdict;
    var state = block && block.state;
    if (verdict) {
      var cls = KIND_CLASS[verdict.kind] || "na";
      var text = verdict.headline;
      if (verdict.kind === "pass") {
        text += " (" + fmtWhen(verdict.checked_at_utc) + ")";
      }
      if (state === "running") {
        text += " · re-checking…";
      }
      return { cls: cls, text: text };
    }
    if (state === "running") {
      return { cls: "na", text: "live-proof: checking…" };
    }
    return { cls: "na", text: pendingLabel };
  }

  var lastData = null;
  var lastFailSignature = "";
  var dismissedSignature = "";
  // Amber notice dismiss state (DEC-AUTO-RESUME-ZERO-SURPRISES-001) — the
  // notice twin of the failSignature/dismissedSignature pair: the ✕ hides
  // only the EXACT current message; a NEW notice message re-opens the strip.
  var lastNoticeSignature = "";
  var dismissedNoticeSignature = "";
  // Never-occlude collapse state (DEC-UNIVERSE-UI-POLISH-002).
  var minTimer = null;
  var minimizedSignature = "";
  var pendingMinSignature = "";
  // Staleness + self-recovering backoff state (DEC-PULL-UI-STALENESS-001).
  var lastPollOkMs = null;      // monotonic client stamp of the last reachable poll
  var lastPollOkWallMs = null;  // wall-clock twin (suspend guard — see renderAge)
  var pollFailures = 0;         // consecutive failed polls → backoff exponent
  var pollTimer = null;         // the self-scheduling poll chain handle
  var pollSeq = 0;              // a superseded response never renders/schedules
  var bootMs = null;            // fallback age reference until the first success
  var bootWallMs = null;

  function verdictSignature(data) {
    // A stable id for the CURRENTLY SHOWN verdict set — a changed kind or
    // checked_at re-expands the collapsed badge and restarts the timer.
    var parts = [];
    ["preflight", "probes"].forEach(function (key) {
      var v = data[key] && data[key].verdict;
      if (v) {
        parts.push(key + "@" + v.kind + "@" + v.checked_at_utc);
      }
    });
    return parts.join("|");
  }

  function scheduleMinimize(pill, sig) {
    // Already collapsed for this exact verdict set → stay collapsed (the 5 s
    // poll re-renders; it must not re-expand an unchanged verdict).
    if (sig === minimizedSignature) {
      pill.classList.add("bxlp-min");
      return;
    }
    // A timer is already pending for this verdict set → let it run.
    if (sig === pendingMinSignature) {
      return;
    }
    // NEW verdict set: show the full pill, then collapse after AUTO_MIN_MS.
    pendingMinSignature = sig;
    pill.classList.remove("bxlp-min");
    if (minTimer) {
      clearTimeout(minTimer);
    }
    minTimer = setTimeout(function () {
      minimizedSignature = sig;
      pill.classList.add("bxlp-min");
    }, AUTO_MIN_MS);
  }

  function failSignature(data) {
    // A stable id for the CURRENT failing verdict set — the ✕ dismisses only
    // this exact set; any new failing verdict re-opens the banner.
    var parts = [];
    ["preflight", "probes"].forEach(function (key) {
      var v = data[key] && data[key].verdict;
      if (v && (v.kind === "fail" || v.kind === "error")) {
        parts.push(key + "@" + v.checked_at_utc);
      }
    });
    // Pull auto-resume refusal (DEC-PULL-INTENT-RESUME-001): a critical
    // resume payload rides the SAME red banner + dismiss signature — one
    // banner mechanism site-wide (§11 COMMON), never a second pill script.
    if (data.resume && data.resume.level === "critical") {
      parts.push("resume@" + data.resume.message);
    }
    return parts.join("|");
  }

  function renderBanner(data) {
    var banner = document.getElementById(BANNER_ID);
    var body = document.getElementById("bxlpBannerBody");
    if (!banner || !body) {
      return;
    }
    lastFailSignature = failSignature(data);
    if (!lastFailSignature) {
      banner.classList.remove("open");
      return;
    }
    while (body.firstChild) {
      body.removeChild(body.firstChild);
    }
    ["preflight", "probes"].forEach(function (key) {
      var v = data[key] && data[key].verdict;
      if (!v || (v.kind !== "fail" && v.kind !== "error")) {
        return;
      }
      body.appendChild(
        el("div", "bxlp-bhead", v.headline + " — " + key + " (" + fmtWhen(v.checked_at_utc) + ")")
      );
      (v.lines || []).forEach(function (line) {
        body.appendChild(el("div", "bxlp-line", line));
      });
    });
    if (data.resume && data.resume.level === "critical") {
      // The backend message is rendered as textContent (never markup) — the
      // same XSS-safe discipline as every other line in this widget.
      body.appendChild(el("div", "bxlp-bhead", "pull auto-resume — CRITICAL"));
      body.appendChild(el("div", "bxlp-line", data.resume.message));
    }
    if (lastFailSignature !== dismissedSignature) {
      banner.classList.add("open");
    }
  }

  function renderNotice(data) {
    // The NON-critical amber strip (DEC-AUTO-RESUME-ZERO-SURPRISES-001):
    // a resume payload with level "notice" (a boot auto-resume LAUNCH
    // announce / a pull stop request) opens it; anything else closes it.
    // textContent-only rendering — the backend message is NEVER markup
    // (the same XSS discipline as every other line in this widget).
    var strip = document.getElementById(NOTICE_ID);
    var body = document.getElementById("bxlpNoticeBody");
    if (!strip || !body) {
      return;
    }
    var isNotice = !!(data && data.resume && data.resume.level === "notice");
    lastNoticeSignature = isNotice ? "resume-notice@" + data.resume.message : "";
    if (!lastNoticeSignature) {
      strip.classList.remove("open");
      return;
    }
    while (body.firstChild) {
      body.removeChild(body.firstChild);
    }
    body.appendChild(el("div", "bxlp-nhead", "pull pipeline — notice"));
    body.appendChild(el("div", "bxlp-line", data.resume.message));
    if (lastNoticeSignature !== dismissedNoticeSignature) {
      strip.classList.add("open");
    }
  }

  function renderDetailBlock(body, label, block, pendingLabel) {
    body.appendChild(el("h3", null, label));
    var v = block && block.verdict;
    var summary = verdictSummary(block, pendingLabel);
    body.appendChild(el("div", "bxlp-kind " + summary.cls, summary.text));
    if (!v) {
      return;
    }
    var stages = (v.stages || [])
      .map(function (s) {
        return s.name + "=" + s.status;
      })
      .join(" · ");
    if (stages) {
      body.appendChild(el("div", "bxlp-stages", "stages: " + stages));
    }
    (v.lines || []).forEach(function (line) {
      body.appendChild(el("div", "bxlp-line", line));
    });
  }

  function renderDetail(data) {
    var body = document.getElementById("bxlpDetailBody");
    if (!body) {
      return;
    }
    while (body.firstChild) {
      body.removeChild(body.firstChild);
    }
    renderDetailBlock(body, "Startup preflight (auto-run on boot)", data.preflight,
      "live-proof: not yet run");
    renderDetailBlock(body, "One-time probes (auto-run on first real pull)", data.probes,
      "probes: pending — auto-run on the first real pull");
  }

  function render(data) {
    lastData = data;
    var pill = document.getElementById(WIDGET_ID);
    if (!pill) {
      return;
    }
    var resumeCritical = !!(data && data.resume && data.resume.level === "critical");
    // A "notice"-level resume payload (announce-on-launch / stop request —
    // DEC-AUTO-RESUME-ZERO-SURPRISES-001) must ALSO render when live-proof is
    // not wired, exactly like the critical arm below.
    var resumeNotice = !!(data && data.resume && data.resume.level === "notice");
    var resumeVisible = resumeCritical || resumeNotice;
    if (!data || ((data.present !== true || data.enabled === false) && !resumeVisible)) {
      // Not wired / escape hatch off → no invented state, no widget.
      pill.classList.remove("shown");
      var banner = document.getElementById(BANNER_ID);
      if (banner) {
        banner.classList.remove("open");
      }
      var noticeStrip = document.getElementById(NOTICE_ID);
      if (noticeStrip) {
        noticeStrip.classList.remove("open");
      }
      return;
    }
    if (data.present !== true || data.enabled === false) {
      // Live-proof not wired but a pull auto-resume payload exists
      // (DEC-PULL-INTENT-RESUME-001 critical / the zero-surprises notice):
      // render ONLY the banner strip(s) — no invented live-proof pill state.
      pill.classList.remove("shown");
      renderBanner(data);
      renderNotice(data);
      return;
    }
    pill.classList.add("shown");
    var pre = verdictSummary(data.preflight, "live-proof: not yet run");
    var dot = document.getElementById("bxlpDot");
    if (dot) {
      var dotCls = pre.cls;
      var probesVerdict = data.probes && data.probes.verdict;
      if (probesVerdict) {
        dotCls = worstClass(pre.cls, KIND_CLASS[probesVerdict.kind] || "na");
      }
      dot.className = "bxlp-dot " + dotCls;
    }
    var text = document.getElementById("bxlpText");
    if (text) {
      // The unified flow-scoped worst-of headline (never preflight-only —
      // a mixed run must not read as a contradictory PASS-then-FAIL toast).
      text.textContent = unifiedHeadline(data);
    }
    var probes = verdictSummary(
      data.probes,
      "probes: pending — auto-run on the first real pull"
    );
    var probesEl = document.getElementById("bxlpProbes");
    if (probesEl) {
      var v = data.probes && data.probes.verdict;
      probesEl.textContent = v || (data.probes && data.probes.state === "running")
        ? "probes: " + probes.text
        : probes.text;
    }
    renderBanner(data);
    renderNotice(data);
    // Never-occlude collapse (DEC-UNIVERSE-UI-POLISH-002): auto-shrink the pill
    // to the dot badge after AUTO_MIN_MS; a NEW verdict re-expands + restarts.
    scheduleMinimize(pill, verdictSignature(data));
  }

  // Monotonic client clock: performance.now() is immune to NTP/wall-clock jumps
  // (mirrors bx-rate-monitor.js; the Date.now() twin in renderAge covers the
  // suspend caveat — a wall jump can only OVER-report staleness, the safe way).
  function nowMs() {
    return (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now();
  }

  // Exponential backoff, overflow-proof: exponent clamped BEFORE Math.pow
  // (3000 * 2^4 = 48000 already exceeds the 30000 cap).
  function backoffDelayMs(failures) {
    var exp = Math.min(failures > 0 ? failures - 1 : 0, 4);
    return Math.min(BACKOFF_BASE_MS * Math.pow(2, exp), BACKOFF_MAX_MS);
  }

  function schedulePoll(delayMs) {
    if (pollTimer) { clearTimeout(pollTimer); }
    pollTimer = setTimeout(poll, delayMs);
  }

  // Page fan-out (DEC-FRONTEND-LATENCY-HARDENING-001, MIGRATION.md #1720): a
  // template that ALSO renders /api/v1/liveproof data (e.g. the v29 heartbeat
  // proof slot) subscribes via `(window.__bxLiveproofSubs ||= []).push(fn)` —
  // its inline script may run BEFORE this deferred file, so the arrays are
  // read at CALL time, never captured — instead of owning a SECOND poller on
  // the same endpoint. Failure twins ride __bxLiveproofFailSubs. A throwing
  // subscriber is isolated (it can never break this widget's poll chain).
  // This file stays dependency-free by design, so the tiny fan-out helper is
  // local (bx-rate-monitor.js carries the same-shape twin — deliberate, ledgered).
  function notifyPageSubs(listName, arg) {
    var subs = window[listName];
    if (!subs || !subs.length) { return; }
    for (var i = 0; i < subs.length; i++) {
      try { subs[i](arg); } catch (e) { /* subscriber isolation */ }
    }
  }

  // Poll-fetch discipline (DEC-WINDOWS-COUNTS-ONLY-001 M15, merged with the
  // display-truth self-scheduling chain — mirrors the sibling
  // bx-rate-monitor.js fetchJson): at most ONE request in flight (the
  // pollInFlight guard below — the visibilitychange catch-up can never stack a
  // second request onto the browser's ~6-per-origin connection pool), and the
  // monotonic pollSeq token (declared with the widget state above) discards a
  // stale response so it can never paint over a fresher one. HONEST envelope
  // (Golden Rule #10): on an ancient browser without AbortController the
  // deadline arm is inert (same as the sibling widget) — the in-flight release
  // then waits on the browser's own fetch failure.
  var pollInFlight = false;   // one-in-flight guard (M15) — never stack requests

  // Bounded JSON fetch (mirrors bx-rate-monitor.js): a non-ok status THROWS (a
  // counted failure) and the abort timer stays ARMED through the BODY read —
  // cleared only after r.json() settles — so a hung fetch OR a stalled body is
  // aborted at FETCH_TIMEOUT_MS and lands in the counted-failure backoff path;
  // the self-scheduling poll chain can never wedge on a dead connection.
  function fetchJson(url) {
    var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var killer = ctrl ? setTimeout(function () { ctrl.abort(); }, FETCH_TIMEOUT_MS) : null;
    var opts = { headers: { Accept: "application/json" } };
    if (ctrl) { opts.signal = ctrl.signal; }
    return fetch(url, opts).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }  // non-ok is a FAILED poll
      return r.json();   // killer still armed: a stalled BODY (or malformed JSON) aborts/throws too
    }).then(function (data) {
      if (killer) { clearTimeout(killer); }
      return data;
    }).catch(function (err) { if (killer) { clearTimeout(killer); } throw err; });
  }

  // Client-clock freshness readout (DEC-PULL-UI-STALENESS-001): "updated Xs ago"
  // → amber past STALE_WARN_MS → LOUD "STALE" past STALE_RED_MS, and the pill's
  // stale classes recolour the DOT so a green "pass" can never keep reading fresh
  // when the backend has gone silent. textContent only (CodeQL XSS gate).
  function renderAge() {
    var pill = document.getElementById(WIDGET_ID);
    if (!pill) { return; }
    var ageEl = document.getElementById("bxlpAge");
    var ref = lastPollOkMs !== null ? lastPollOkMs : bootMs;
    var wallRef = lastPollOkWallMs !== null ? lastPollOkWallMs : bootWallMs;
    var text = "";
    var cls = "";
    if (ref !== null) {
      var age = nowMs() - ref;
      // Suspend guard: performance.now() may pause across a system suspend, so fold
      // in the wall-clock age too — max() can only OVER-report (the safe direction).
      if (wallRef !== null) { age = Math.max(age, Date.now() - wallRef); }
      var ageS = Math.max(0, Math.round(age / 1000));
      var base = lastPollOkMs !== null ? "updated " + ageS + "s ago" : "no data yet (" + ageS + "s)";
      var tail = pollFailures > 0 ? " — retrying" : "";
      if (age >= STALE_RED_MS) { text = "STALE — " + base + tail; cls = "bxlp-stale"; }
      else if (age >= STALE_WARN_MS) { text = base + tail; cls = "bxlp-stale-warn"; }
      else { text = base; }
    }
    if (ageEl) { ageEl.textContent = text; }
    pill.classList.toggle("bxlp-stale", cls === "bxlp-stale");
    pill.classList.toggle("bxlp-stale-warn", cls === "bxlp-stale-warn");
  }

  function poll() {
    // Hidden-tab pause (DEC-LEDGER-DELTA-O-PAGE-001, the v29.html pattern): a
    // background tab fires ZERO fetches — the guard re-arms the self-scheduling
    // chain at normal cadence (a hidden tick is free) and the visibilitychange
    // listener below fires an immediate catch-up poll on return, so the pill is
    // never stale to a returning operator (Golden Rule #1). The guard returns
    // BEFORE pollSeq is bumped and BEFORE any fetch, so a hidden period counts
    // ZERO pollFailures — no spurious backoff/staleness while hidden.
    if (document.hidden) { schedulePoll(POLL_MS); return; }
    // One-in-flight guard (DEC-WINDOWS-COUNTS-ONLY-001 M15): the visibilitychange
    // catch-up can never stack a second request — the in-flight fetch's OWN
    // settle handler schedules the next tick, so the chain never dies here.
    if (pollInFlight) { return; }
    pollInFlight = true;
    var seq = ++pollSeq;   // a superseded response never renders or schedules
    fetchJson(ENDPOINT)
      .then(function (data) {
        pollInFlight = false;
        if (seq !== pollSeq) { return; }
        // A reachable backend (ANY 2xx JSON, wired or not) resets the freshness
        // clock; only a genuine PAYLOAD drives render() (present:false stays inert).
        pollFailures = 0;
        lastPollOkMs = nowMs();
        lastPollOkWallMs = Date.now();
        if (data && typeof data === "object") {
          render(data);
          // Fan the ONE /api/v1/liveproof poll out to page subscribers
          // (DEC-FRONTEND-LATENCY-HARDENING-001) — never a second poller.
          notifyPageSubs("__bxLiveproofSubs", data);
        }
        renderAge();   // stale marking clears on the very next reachable poll
        schedulePoll(POLL_MS);
      })
      .catch(function () {
        // Operator-incident parity with bx-rate-monitor.js (#1529): a non-ok status,
        // a fetch rejection AND a malformed body are ALL counted failures that feed
        // the staleness clock + back off 3 s -> 30 s; NEVER a stale green pill reading
        // fresh forever. The last real VERDICT is retained — only freshness escalates.
        pollInFlight = false;
        if (seq !== pollSeq) { return; }
        pollFailures += 1;
        notifyPageSubs("__bxLiveproofFailSubs", pollFailures);
        renderAge();
        schedulePoll(backoffDelayMs(pollFailures));
      });
  }

  function start() {
    injectStyles();
    buildWidget();
    bootMs = nowMs();
    bootWallMs = Date.now();
    poll();
    // The ~1 s client staleness ticker keeps the age readout live between polls
    // so a silent backend surfaces as STALE without waiting for the next attempt.
    setInterval(renderAge, AGE_TICK_MS);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) poll();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
