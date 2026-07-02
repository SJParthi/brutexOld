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
 *   - the one-time PROBES verdict (2020 1m depth / VIX hard-gate / bhavcopy
 *     cross-check), auto-run around the first REAL pull and then cached;
 *   - a RED top banner whenever either verdict is fail/error, showing the
 *     kit's diagnostic lines VERBATIM (the golden-tested catalog is the only
 *     wording source — this file never re-words a diagnosis).
 *
 * HONEST rendering (Golden Rules #1/#10): green appears ONLY for a backend
 * verdict whose kind is "pass" (the stages truly ran against the real
 * vendor); "not_applicable" (no credentials — the sandbox/dry-run state)
 * renders grey with the backend's own headline "live-proof: not applicable
 * (no credentials)" and the relayed CredentialsError fix text — never a fake
 * green. When the autorun is not wired (present:false) or disabled via the
 * BRUTEX_AUTORUN_LIVEPROOF=0 escape hatch, the widget renders NOTHING (no
 * invented state). A fetch error leaves the last render in place — the
 * widget is read-only + best-effort and never throws into the page.
 *
 * The ✕ on the red banner only COLLAPSES it to the pill (which stays red);
 * a NEW failing verdict (a different checked_at) re-opens it. No action
 * buttons: this widget displays the proof, it never re-runs it (startup +
 * the first real pull are the only triggers).
 */
(function () {
  "use strict";

  var ENDPOINT = "/api/v1/liveproof";
  var POLL_MS = 5000;
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
    if (!data || data.present !== true || data.enabled === false) {
      // Not wired / escape hatch off → no invented state, no widget.
      pill.classList.remove("shown");
      var banner = document.getElementById(BANNER_ID);
      if (banner) {
        banner.classList.remove("open");
      }
      return;
    }
    pill.classList.add("shown");
    var pre = verdictSummary(data.preflight, "live-proof: not yet run");
    var dot = document.getElementById("bxlpDot");
    if (dot) {
      dot.className = "bxlp-dot " + pre.cls;
    }
    var text = document.getElementById("bxlpText");
    if (text) {
      text.textContent = pre.text;
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
