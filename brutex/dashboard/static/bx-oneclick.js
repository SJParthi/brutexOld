/* bx-oneclick.js — the operator/screener/strategy widget layer over
 * bx-poll.js (DEC-OPERATOR-PAGE-REAL-001).
 *
 * DOM DISCIPLINE (CodeQL guard): this file builds DOM via
 * createElement/createElementNS + textContent ONLY — ZERO string-to-DOM
 * sinks, so no artifact-derived string (conditions[], mask_hex, kind,
 * detail, exit_reason, family_id, run_id, stringified payload) can ever be
 * parsed as markup (F-M2). Event `payload` objects are rendered as
 * JSON.stringify(payload) into a SINGLE textContent node — never key-spread
 * into markup.
 *
 * Laws implemented here:
 *   - poll discipline via the ONE shared bx-poll.js core (no third copy);
 *   - funnel write PRECEDENCE (C-M2): the live /api/v1/state data-bind
 *     poller WINS while a sweep is ACTIVE; the manifest fill applies ONLY
 *     when idle AND for the explicitly selected run; the provenance label
 *     ("live run r-…" / "completed run r-… · manifest") is textContent-set;
 *   - the timeline is a RING BUFFER capped at 5,000 records (F-B2), loudly
 *     labeled; the search input filters the RING only (honest O(ring)
 *     client cost — window-scoped, NOT O(1)-total);
 *   - one-shot fetches route failure through BXUI.errorCard with a Retry
 *     button — fetch rejection AND non-ok AND malformed body all land there,
 *     never a false-empty grid; "Loading…" is never a terminal state (F-M4);
 *   - Beginner/Pro toggle rides the ONE shared bx-explain.js mechanism
 *     (v29 .jargon/.plain-eng convention — C-MINOR-5);
 *   - the ticker strip (R36 "always visible") renders sweep status + the
 *     latest run from real payloads only.
 *
 * Honest envelope (Golden Rule #10): the pure primitives (ring buffer,
 * precedence/provenance/line-text builders) are node-golden-testable; the
 * DOM layer is browser-only and pinned by SOURCE-STRING tests (presence,
 * not execution — no headless JS runner in CI). Load order: bx-ui.js,
 * bx-poll.js, bx-explain.js (and bx-chart.js on chart pages) must load
 * BEFORE this file.
 */
"use strict";

/** Timeline client ring capacity (F-B2 — bounded memory, loudly labeled). */
var BX_TIMELINE_RING_CAP = 5000;

/** The honest ring label (ONE constant so the substring pin cannot drift). */
var BX_TIMELINE_RING_LABEL =
  "showing last 5,000 events — older events via “load earlier”";

/**
 * Append records to the ring, trimming the OLDEST from the front past cap
 * (pure — mutates and returns `ring`).
 *
 * @param {object[]} ring - the ring array (oldest first).
 * @param {object[]} records - new records to append.
 * @param {number} cap - the capacity (BX_TIMELINE_RING_CAP).
 * @returns {object[]} the same ring, trimmed to cap.
 */
function bxRingPush(ring, records, cap) {
  for (var i = 0; i < records.length; i++) {
    ring.push(records[i]);
  }
  if (ring.length > cap) {
    ring.splice(0, ring.length - cap);
  }
  return ring;
}

/**
 * Filter the RING ONLY (honest window-scoped search — never a whole-file
 * search; pure).
 *
 * @param {object[]} ring - the ring array.
 * @param {string} q - case-insensitive substring over the rendered line.
 * @param {string} kind - exact kind match ("" = any).
 * @returns {object[]} the matching records (ring order).
 */
function bxRingFilter(ring, q, kind) {
  var needle = (q || "").toLowerCase();
  var out = [];
  for (var i = 0; i < ring.length; i++) {
    var rec = ring[i];
    if (kind && rec.kind !== kind) {
      continue;
    }
    if (needle && bxEventLineText(rec).toLowerCase().indexOf(needle) === -1) {
      continue;
    }
    out.push(rec);
  }
  return out;
}

/**
 * The ONE-text-node line for an event record: the payload object is
 * JSON.stringify'd into the SINGLE string (F-M2 — never key-spread into
 * markup; the caller assigns this to textContent).
 *
 * @param {object} rec - {ts_utc, run_id, kind, payload}.
 * @returns {string} the line text.
 */
function bxEventLineText(rec) {
  return String(rec.ts_utc) + "  " + String(rec.kind) + "  " + JSON.stringify(rec.payload);
}

/**
 * Funnel write-precedence law (C-M2, pure): the live source may always
 * write; the manifest fill applies ONLY while NO sweep is running.
 *
 * @param {string} source - "live" | "manifest".
 * @param {boolean} sweepRunning - whether sweep-progress reports running.
 * @returns {boolean} whether this source may write the funnel now.
 */
function bxFunnelWriteAllowed(source, sweepRunning) {
  if (source === "live") {
    return true;
  }
  return !sweepRunning; // manifest fills only when idle (C-M2)
}

/**
 * The funnel provenance label (pure; textContent-rendered beside the
 * funnel so two sources can never interleave silently).
 *
 * @param {string} source - "live" | "manifest".
 * @param {string} runId - the run the numbers belong to.
 * @returns {string} the label text.
 */
function bxFunnelProvenance(source, runId) {
  if (source === "live") {
    return "live run " + String(runId);
  }
  return "completed run " + String(runId) + " · manifest";
}

/* ── Browser-only widget layer below (pinned by source-string tests). ── */

(function () {
  if (typeof document === "undefined") {
    return; // node harness: pure exports only
  }
  if (window.__bxOneclickInit) {
    return; // idempotent: second injection is a no-op
  }
  window.__bxOneclickInit = true;

  var RUNS_URL = "/api/v1/oneclick/runs";
  var MANIFEST_URL = "/api/v1/oneclick/manifest";
  var EVENTS_URL = "/api/v1/oneclick/events";
  var PROGRESS_URL = "/api/v1/sweep-progress";
  var EVENTS_PAGE_LIMIT = 200;

  var state = {
    selectedRunId: null,
    sweepRunning: false,
    ring: [],
    ringSeen: {}, // canonical-line dedupe for the load-earlier pager
    eventsCursor: -1, // -1 = tail-anchored first page
    earlierCursor: 0 // forward cursor for the "load earlier" pager
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function setText(id, text) {
    var n = byId(id);
    if (n) {
      n.textContent = text; // textContent ONLY — the F-M2 rendering law
    }
  }

  /* One-shot fetch failure law (F-M4): rejection AND non-ok AND malformed
     body all land in BXUI.errorCard with a Retry button — never a
     false-empty/blank grid, "Loading…" never terminal. */
  function bxOneshotFetch(url, panel, render) {
    if (panel && window.BXUI) {
      window.BXUI.skeleton(panel, 4, 1, {
        watchdogMs: 6000,
        watchdogText: "still working — the server may be busy"
      });
    }
    window.BXUI.fetchWithTimeout(url, 12000)
      .then(function (r) {
        if (!r.ok) {
          var err = new Error("HTTP " + r.status);
          err.status = r.status;
          throw err;
        }
        return r.json();
      })
      .then(function (data) {
        if (!data || typeof data !== "object") {
          throw new Error("malformed payload");
        }
        render(data);
      })
      .catch(function (err) {
        if (panel && window.BXUI) {
          window.BXUI.errorCard(panel, {
            status: err && err.status,
            offline: !!(err && err.name === "TypeError"),
            retry: function () {
              bxOneshotFetch(url, panel, render);
            }
          });
        }
      });
  }

  /* ── Awaiting banner + run pill (manifest poller) ── */

  function renderManifest(payload) {
    var awaiting = byId("ocAwaiting");
    if (awaiting) {
      if (payload.status === "awaiting") {
        awaiting.textContent =
          window.BXExplain ? window.BXExplain.AWAITING_TEXT : "awaiting real run artifact";
        awaiting.hidden = false;
      } else {
        awaiting.hidden = true;
      }
    }
    var pill = byId("ocRunPill");
    if (pill && payload.pill) {
      pill.textContent = String(payload.pill.state);
      pill.setAttribute("title", "derived from: " + String(payload.pill.derived_from));
      pill.className = "oc-pill oc-pill-" + String(payload.pill.state).replace(/[^a-z]/g, "-");
    }
    var fx = byId("ocFixtureBadge");
    if (fx) {
      fx.hidden = !payload.fixture_root_active;
    }
    if (payload.status === "ok" || payload.status === "partial") {
      fillFunnelFromManifest(payload);
    }
  }

  /* Funnel fill with the C-M2 precedence law: applied ONLY when idle AND
     for the explicitly selected run; the live /api/v1/state data-bind
     poller (v29 inline) wins while a sweep is active. */
  function fillFunnelFromManifest(payload) {
    if (!bxFunnelWriteAllowed("manifest", state.sweepRunning)) {
      return;
    }
    if (!payload.manifest || payload.run_id !== state.selectedRunId) {
      return;
    }
    var actual = payload.manifest.actual;
    if (!actual || !actual.funnel || !actual.funnel.length) {
      return;
    }
    for (var i = 0; i < actual.funnel.length; i++) {
      var stage = actual.funnel[i];
      var node = document.querySelector(
        '[data-oc-funnel="' + String(stage.stage).replace(/[^a-z0-9_-]/gi, "") + '"]'
      );
      if (node) {
        node.textContent = Number(stage.count).toLocaleString("en-IN");
      }
    }
    setText("ocFunnelProvenance", bxFunnelProvenance("manifest", payload.run_id));
  }

  /* ── Chain map (data-driven renderer — the STATES come from the server
        payload; this renderer invents nothing) ── */

  function renderChainMap(links) {
    var host = byId("ocChainMap");
    if (!host || !window.BXUI) {
      return;
    }
    window.BXUI.clear(host);
    for (var i = 0; i < links.length; i++) {
      var link = links[i];
      var chip = window.BXUI.el("span", "oc-chain-link oc-chain-" + String(link.state), null);
      chip.appendChild(window.BXUI.el("span", "oc-chain-stage", String(link.stage)));
      if (link.count !== undefined && link.count !== null) {
        chip.appendChild(window.BXUI.el("span", "oc-chain-count", String(link.count)));
      }
      host.appendChild(chip);
    }
  }

  /* ── Timeline: capped ring + window-scoped search + load-earlier pager ── */

  function ringAdd(records) {
    var fresh = [];
    for (var i = 0; i < records.length; i++) {
      var key = bxEventLineText(records[i]);
      if (!state.ringSeen[key]) {
        state.ringSeen[key] = true;
        fresh.push(records[i]);
      }
    }
    bxRingPush(state.ring, fresh, BX_TIMELINE_RING_CAP);
    if (state.ring.length >= BX_TIMELINE_RING_CAP) {
      setText("ocTimelineLabel", BX_TIMELINE_RING_LABEL); // loud, honest cap label
    }
  }

  function renderTimeline() {
    var host = byId("ocTimeline");
    if (!host || !window.BXUI) {
      return;
    }
    var search = byId("ocTimelineSearch");
    var q = search ? search.value : "";
    var rows = bxRingFilter(state.ring, q, "");
    window.BXUI.clear(host);
    // Newest last (log order); each record is ONE text node (F-M2).
    for (var i = 0; i < rows.length; i++) {
      host.appendChild(window.BXUI.el("div", "oc-event-line", bxEventLineText(rows[i])));
    }
  }

  function pollEvents() {
    if (!state.selectedRunId) {
      return;
    }
    var url =
      EVENTS_URL +
      "?run_id=" + encodeURIComponent(state.selectedRunId) +
      "&offset=" + state.eventsCursor +
      "&limit=" + EVENTS_PAGE_LIMIT;
    window.BXPoll.fetchJson(url, 10000)
      .then(function (page) {
        if (!page || typeof page !== "object") {
          return;
        }
        if (page.records && page.records.length) {
          ringAdd(page.records);
          renderTimeline();
        }
        if (typeof page.next_offset === "number") {
          state.eventsCursor = page.next_offset;
        }
      })
      .catch(function () {
        /* the events poller rides the manifest poller's cadence; a failed
           tail read retries on the next tick — never an error loop */
      });
  }

  function loadEarlier() {
    if (!state.selectedRunId) {
      return;
    }
    var url =
      EVENTS_URL +
      "?run_id=" + encodeURIComponent(state.selectedRunId) +
      "&offset=" + state.earlierCursor +
      "&limit=" + EVENTS_PAGE_LIMIT;
    window.BXPoll.fetchJson(url, 10000)
      .then(function (page) {
        if (!page || typeof page !== "object" || !page.records) {
          return;
        }
        // Earlier records join the ring FRONT up to the cap (the label
        // states the honest 5,000 window — older history never silently
        // displaces the live tail).
        var room = BX_TIMELINE_RING_CAP - state.ring.length;
        var take = [];
        for (var i = 0; i < page.records.length && i < Math.max(0, room); i++) {
          var key = bxEventLineText(page.records[i]);
          if (!state.ringSeen[key]) {
            state.ringSeen[key] = true;
            take.push(page.records[i]);
          }
        }
        if (take.length) {
          state.ring = take.concat(state.ring);
        }
        if (typeof page.next_offset === "number") {
          state.earlierCursor = page.next_offset;
        }
        if (state.ring.length >= BX_TIMELINE_RING_CAP) {
          setText("ocTimelineLabel", BX_TIMELINE_RING_LABEL);
        }
        renderTimeline();
      })
      .catch(function () {
        /* one-shot pager click: the button stays; the next click retries */
      });
  }

  /* ── Ticker strip (R36 "always visible": sweep status · latest run) ── */

  function renderTicker(progress, runsPayload) {
    var strip = byId("bxTickerStrip");
    if (!strip) {
      return;
    }
    var bits = [];
    if (progress && progress.running) {
      bits.push("sweep: RUNNING");
      if (progress.done !== undefined && progress.total !== undefined) {
        bits.push(String(progress.done) + "/" + String(progress.total) + " tuples");
      }
    } else {
      bits.push("sweep: idle");
    }
    if (runsPayload && runsPayload.latest_run_id) {
      bits.push("latest run " + String(runsPayload.latest_run_id));
    } else {
      bits.push("no runs yet — awaiting real run artifact");
    }
    strip.textContent = bits.join(" · "); // ONE text node, textContent only
  }

  /* ── Beginner/Pro toggle (ONE shared mechanism — bx-explain.js) ── */

  function wireModeToggle() {
    var btn = byId("bxModeToggle");
    if (!btn || !window.BXExplain) {
      return;
    }
    var mode = window.BXExplain.storedMode();
    window.BXExplain.applyMode(document.body, mode);
    btn.textContent = mode === "beginner" ? "Beginner ✓" : "Pro ✓";
    btn.addEventListener("click", function () {
      mode = mode === "beginner" ? "pro" : "beginner";
      window.BXExplain.applyMode(document.body, mode);
      btn.textContent = mode === "beginner" ? "Beginner ✓" : "Pro ✓";
    });
  }

  /* ── Wiring ── */

  function init() {
    wireModeToggle();
    var tip = byId("bxTip");
    if (tip && window.BXExplain) {
      window.BXExplain.bindExplainHover(document.body, tip);
    }
    var earlierBtn = byId("ocTimelineEarlier");
    if (earlierBtn) {
      earlierBtn.addEventListener("click", loadEarlier);
    }
    var searchBox = byId("ocTimelineSearch");
    if (searchBox) {
      searchBox.addEventListener("input", renderTimeline); // ring-scoped filter only
    }

    var lastProgress = null;
    var lastRuns = null;

    // Sweep-progress poller: owns state.sweepRunning (the C-M2 precedence
    // input) + the ticker strip's sweep half.
    var progressPoller = window.BXPoll.createPoller({
      url: PROGRESS_URL,
      onData: function (data) {
        lastProgress = data;
        state.sweepRunning = !!(data && data.running);
        renderTicker(lastProgress, lastRuns);
      }
    });

    // Runs poller: run discovery + the ticker strip's run half.
    var runsPoller = window.BXPoll.createPoller({
      url: RUNS_URL,
      onData: function (data) {
        lastRuns = data;
        if (!state.selectedRunId && data.latest_run_id) {
          state.selectedRunId = data.latest_run_id;
        }
        renderTicker(lastProgress, lastRuns);
      }
    });

    // Manifest poller (selected run): pill + awaiting banner + idle funnel
    // fill; the events tail rides the same cadence.
    var manifestPoller = window.BXPoll.createPoller({
      url: MANIFEST_URL, // run_id appended per poll via fetchJson below
      fetchJson: function (_url, timeoutMs) {
        if (!state.selectedRunId) {
          return Promise.resolve({ status: "awaiting", detail: "no run selected" });
        }
        return window.BXPoll.fetchJson(
          MANIFEST_URL + "?run_id=" + encodeURIComponent(state.selectedRunId),
          timeoutMs
        );
      },
      onData: function (data) {
        renderManifest(data);
        if (data.chain_links) {
          renderChainMap(data.chain_links); // data-driven; states are server truth
        }
        pollEvents();
      }
    });

    progressPoller.start();
    runsPoller.start();
    manifestPoller.start();

    // Client staleness readout for the freshest of the pollers (bx-poll's
    // shared ticker — the 1 s tick short-circuits while hidden).
    var updated = byId("ocUpdated");
    if (updated) {
      window.BXPoll.stalenessTicker(manifestPoller, function (s) {
        updated.textContent = "updated " + s.text;
        updated.className = "oc-updated oc-updated-" + s.level;
      });
    }
  }

  // Expose the one-shot helper + init for the screener/strategy pages.
  window.BXOneclick = {
    TIMELINE_RING_CAP: BX_TIMELINE_RING_CAP,
    TIMELINE_RING_LABEL: BX_TIMELINE_RING_LABEL,
    ringPush: bxRingPush,
    ringFilter: bxRingFilter,
    eventLineText: bxEventLineText,
    funnelWriteAllowed: bxFunnelWriteAllowed,
    funnelProvenance: bxFunnelProvenance,
    oneshotFetch: bxOneshotFetch,
    renderChainMap: renderChainMap,
    selectRun: function (runId) {
      state.selectedRunId = runId;
      state.ring = [];
      state.ringSeen = {};
      state.eventsCursor = -1;
      state.earlierCursor = 0;
      renderTimeline();
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

/* Non-browser harness (the node golden) gets the PURE primitives only —
   the DOM layer above self-guards on document. */
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    BX_TIMELINE_RING_CAP: BX_TIMELINE_RING_CAP,
    BX_TIMELINE_RING_LABEL: BX_TIMELINE_RING_LABEL,
    bxRingPush: bxRingPush,
    bxRingFilter: bxRingFilter,
    bxEventLineText: bxEventLineText,
    bxFunnelWriteAllowed: bxFunnelWriteAllowed,
    bxFunnelProvenance: bxFunnelProvenance
  };
}
