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
 * invented state). A fetch error leaves the last render in place — the
 * widget is read-only + best-effort and never throws into the page.
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
  // Auto-collapse the pill to the compact dot badge this long after a verdict
  // is first shown (DEC-UNIVERSE-UI-POLISH-002 — never occlude page content).
  var AUTO_MIN_MS = 8000;
  var WIDGET_ID = "bxLiveProof";
  var BANNER_ID = "bxLiveProofBanner";
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
      "#" + WIDGET_ID + " .bxlp-text{white-space:nowrap;overflow:hidden;" +
      "text-overflow:ellipsis;}" +
      "#" + WIDGET_ID + " .bxlp-sub{color:var(--muted,#787B86);}" +
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
    pill.appendChild(row1);
    pill.appendChild(row2);
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
  // Never-occlude collapse state (DEC-UNIVERSE-UI-POLISH-002).
  var minTimer = null;
  var minimizedSignature = "";
  var pendingMinSignature = "";

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
    if (!data || ((data.present !== true || data.enabled === false) && !resumeCritical)) {
      // Not wired / escape hatch off → no invented state, no widget.
      pill.classList.remove("shown");
      var banner = document.getElementById(BANNER_ID);
      if (banner) {
        banner.classList.remove("open");
      }
      return;
    }
    if (data.present !== true || data.enabled === false) {
      // Live-proof not wired but a CRITICAL pull auto-resume payload exists
      // (DEC-PULL-INTENT-RESUME-001): render ONLY the red banner — no invented
      // live-proof pill state.
      pill.classList.remove("shown");
      renderBanner(data);
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
    // Never-occlude collapse (DEC-UNIVERSE-UI-POLISH-002): auto-shrink the pill
    // to the dot badge after AUTO_MIN_MS; a NEW verdict re-expands + restarts.
    scheduleMinimize(pill, verdictSignature(data));
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
    poll();
    setInterval(poll, POLL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
