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
 * not execution — the tests/browser Playwright job (#1557) does not
 * drive the Operator page). Load order: bx-ui.js,
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

/** The honest null-VIX copy (ONE constant — the [W3-R4] house awaiting style). */
var BX_VIX_BAND_NOT_STAMPED = "VIX band not stamped for this run";

/**
 * The story-card VIX band sentence ([W3-R4] DEC-ONECLICK-TOP100-OBJECT-
 * ROOT-001 wave-3 fold; pure — textContent-rendered on the operator rank-1
 * card AND the /strategy story card).
 *
 * The REAL writer emits ``vix_band`` as an OBJECT whose ``lo``/``hi`` are
 * null for a null-VIX run (engine ``_vix_band_payload``) — the old
 * undefined-only gate rendered "works when VIX between null–null". A
 * null/missing bound now reads the honest not-stamped copy; a PARTIAL band
 * (one real bound) states the bound it has and names the missing one —
 * never a fabricated range, never "null" as prose.
 *
 * @param {*} band - the row's vix_band value (object | null | undefined).
 * @returns {string} the sentence.
 */
function bxVixBandText(band) {
  if (!band || typeof band !== "object") {
    return BX_VIX_BAND_NOT_STAMPED;
  }
  var loNum = typeof band.lo === "number" && isFinite(band.lo);
  var hiNum = typeof band.hi === "number" && isFinite(band.hi);
  if (loNum && hiNum) {
    return "works when VIX between " + String(band.lo) + "–" + String(band.hi);
  }
  if (loNum) {
    return "works when VIX ≥ " + String(band.lo) + " (upper bound not stamped)";
  }
  if (hiNum) {
    return "works when VIX ≤ " + String(band.hi) + " (lower bound not stamped)";
  }
  return BX_VIX_BAND_NOT_STAMPED;
}

/**
 * The compact /screener band-column cell for the SAME verdicts (pure; the
 * cell previously rendered "null–null" for a null-lo/hi writer band).
 *
 * @param {*} band - the row's vix_band value (object | null | undefined).
 * @returns {string} the cell text ("12.5–18.2" · "≥ 12.5 (upper not
 *   stamped)" · "≤ 18.2 (lower not stamped)" · "not stamped").
 */
function bxVixBandCellText(band) {
  if (!band || typeof band !== "object") {
    return "not stamped";
  }
  var loNum = typeof band.lo === "number" && isFinite(band.lo);
  var hiNum = typeof band.hi === "number" && isFinite(band.hi);
  if (loNum && hiNum) {
    return String(band.lo) + "–" + String(band.hi);
  }
  if (loNum) {
    return "≥ " + String(band.lo) + " (upper not stamped)";
  }
  if (hiNum) {
    return "≤ " + String(band.hi) + " (lower not stamped)";
  }
  return "not stamped";
}

/* ── Losing-streak resilience (sibling sidecar → /strategy hero panel +
   /operator rank-1 hooks; DEC-<resilience>). The MAX cold streak reads from
   row.metrics.max_consecutive_losses (the existing agg field — no rival
   column persisted); every other cell reads from row.resilience.* (the
   sibling block). NULL/absent (stats_live !== true) → every cell "—" + the
   honest unavailable guide, NEVER a fabricated streak (R27/Golden Rule #1).
   All ₹ are GROSS of costs, per-unit (no sizing) — the panel caveat. ── */

/** The honest "no resilience stats" copy (ONE constant — parity-failed/no-trades). */
var BX_RESILIENCE_UNAVAILABLE =
  "Resilience stats unavailable (re-simulation parity mismatch or no trades).";

/** A value or the honest em-dash when null/undefined (never a fabricated 0). */
function bxResDash(value) {
  return value === null || value === undefined ? "—" : String(value);
}

/**
 * ISO-8601 UTC → "YYYY-MM-DD HH:MM IST" (pure epoch arithmetic — the fixed
 * +05:30 IST offset, TZ-invariant like bx-chart's bxIstDateOfEpoch); "—" for
 * a null/unparseable input (never a NaN string).
 *
 * @param {*} isoUtc - the worst-streak start/end ISO-8601 UTC string.
 * @returns {string} the IST datetime, or "—".
 */
function bxIstDateTime(isoUtc) {
  if (typeof isoUtc !== "string" || isoUtc === "") {
    return "—";
  }
  var t = Date.parse(isoUtc);
  if (isNaN(t)) {
    return "—";
  }
  var d = new Date(t + 330 * 60000).toISOString();
  return d.slice(0, 10) + " " + d.slice(11, 16) + " IST";
}

/**
 * The plain-English hold-through guide (client-side, only shipped numbers;
 * R27 — never an invented cause). Branch order: 0-loss first (no streaks),
 * then still-underwater (lower-bound honesty), then the normal envelope read.
 *
 * @param {object} r - the row.resilience block (stats_live true).
 * @returns {string} the guide sentence.
 */
function bxResilienceGuide(r) {
  if (r.losing_streak_count === 0) {
    return (
      "This strategy had NO losing streaks in the backtest — treat any live " +
      "cold run as unmapped territory and size down."
    );
  }
  if (r.still_underwater_at_end === true) {
    return (
      "This strategy had NOT recovered by the backtest end — the " +
      bxResDash(r.longest_underwater_days) +
      "-day underwater figure is a lower bound, not a promise of recovery."
    );
  }
  return (
    "Inside its envelope: cold streaks typically run " +
    bxResDash(r.losing_streak_p50) +
    ", 95% stay at or under " +
    bxResDash(r.losing_streak_p95) +
    " — hold if you're inside it, reassess only past its worst ever."
  );
}

/**
 * The /strategy resilience panel cell strings (pure; textContent-rendered —
 * never a string-to-HTML sink). The MAX reads from row.metrics.max_consecutive_losses;
 * every other cell from row.resilience.* . A NULL/absent record
 * (stats_live !== true) → every cell "—" + the unavailable guide. Money
 * cells format via the injected fmtMoney (bx-explain owns the ONE exact
 * paisa→₹ converter, §11 COMMON); a missing formatter reads "—" (honest,
 * never raw paisa).
 *
 * @param {object} row - a top100 contract row.
 * @param {Function} [fmtMoney] - signed integer-paisa → ₹ string.
 * @returns {object} the cell strings for the panel.
 */
function bxResilienceCells(row, fmtMoney) {
  var m = row && row.metrics && typeof row.metrics === "object" ? row.metrics : {};
  var r = row && row.resilience && typeof row.resilience === "object" ? row.resilience : null;
  var money = typeof fmtMoney === "function" ? fmtMoney : function () {
    return "—";
  };
  if (!r || r.stats_live !== true) {
    return {
      live: false,
      worstStreak: "—",
      worstStreakSub: "—",
      cost: "—",
      costSub: "—",
      underwater: "—",
      underwaterBadge: "—",
      badgeLevel: "na",
      worstMonth: "—",
      worstMonthSub: "",
      winSpread: "—",
      guide: BX_RESILIENCE_UNAVAILABLE
    };
  }
  var hasWorst = r.worst_streak_pnl_paisa !== null && r.worst_streak_pnl_paisa !== undefined;
  var pct = r.worst_streak_pnl_pct;
  var pctText = pct === null || pct === undefined ? "—" : String(pct) + "%";
  var hasLen = r.worst_streak_len !== null && r.worst_streak_len !== undefined;
  // A missing/failed money formatter reads a clean "—" (never "— (pct%)").
  var costMoney = hasWorst ? money(r.worst_streak_pnl_paisa) : "—";
  return {
    live: true,
    worstStreak: bxResDash(m.max_consecutive_losses) + " in a row",
    worstStreakSub:
      "typical " + bxResDash(r.losing_streak_p50) + " · P95 " + bxResDash(r.losing_streak_p95),
    cost: hasWorst && costMoney !== "—" ? costMoney + " (" + pctText + ")" : "—",
    costSub: hasLen
      ? String(r.worst_streak_len) +
        " losses · " +
        bxIstDateTime(r.worst_streak_start) +
        " → " +
        bxIstDateTime(r.worst_streak_end)
      : "no losing runs recorded",
    underwater:
      bxResDash(r.longest_underwater_trades) +
      " trades · " +
      bxResDash(r.longest_underwater_days) +
      " days",
    underwaterBadge: r.still_underwater_at_end === true ? "STILL UNDERWATER at backtest end" : "● recovered",
    badgeLevel: r.still_underwater_at_end === true ? "bad" : "ok",
    worstMonth:
      r.worst_loss_month === null || r.worst_loss_month === undefined
        ? "no losing month"
        : String(r.worst_loss_month) + " (" + bxResDash(r.worst_loss_month_count) + " losses)",
    worstMonthSub:
      r.worst_loss_week === null || r.worst_loss_week === undefined
        ? ""
        : "worst week " + String(r.worst_loss_week),
    winSpread:
      r.win_rate_year_spread === null || r.win_rate_year_spread === undefined
        ? "—"
        : "swing " + String(r.win_rate_year_spread),
    guide: bxResilienceGuide(r)
  };
}

/**
 * The pure "you are N losses in" self-locate verdict (zero backend — compares
 * ONLY the shipped numbers: P95 from row.resilience.losing_streak_p95, MAX
 * from row.metrics.max_consecutive_losses). A live-streak counter is out of
 * Track-1 scope (signal-only, DEC-LIVE-REMOVE-001) — this locates the operator
 * against the backtest envelope only.
 *
 * @param {number} n - losses-in-a-row the operator typed.
 * @param {object} row - the top100 row.
 * @returns {{level: string, symbol: string, text: string}} the verdict.
 */
function bxResilienceSelfLocate(n, row) {
  var m = row && row.metrics && typeof row.metrics === "object" ? row.metrics : {};
  var r = row && row.resilience && typeof row.resilience === "object" ? row.resilience : null;
  var maxN =
    typeof m.max_consecutive_losses === "number" && isFinite(m.max_consecutive_losses)
      ? m.max_consecutive_losses
      : null;
  if (!r || r.stats_live !== true || maxN === null) {
    return { level: "na", symbol: "○", text: "resilience stats unavailable for this strategy" };
  }
  if (typeof n !== "number" || !isFinite(n) || n < 0 || Math.floor(n) !== n) {
    return { level: "na", symbol: "○", text: "enter a whole number of losses in a row" };
  }
  // A 0-loss strategy has p95 === null → the envelope collapses to max (0), so
  // n === 0 reads ok and any n ≥ 1 reads unprecedented (never the warn tier).
  var p95 =
    typeof r.losing_streak_p95 === "number" && isFinite(r.losing_streak_p95)
      ? r.losing_streak_p95
      : maxN;
  if (n <= p95) {
    return { level: "ok", symbol: "●", text: "inside envelope — historically normal, hold" };
  }
  if (n <= maxN) {
    return { level: "warn", symbol: "▲", text: "rare but seen — this strategy has been here and recovered" };
  }
  return {
    level: "bad",
    symbol: "✕",
    text: "unprecedented — deeper than anything in the backtest. Reassess."
  };
}

/**
 * The /operator rank-1 resilience hook strings (pure; setOc-rendered). Gated
 * on stats_live — the defense-in-depth MIRROR of the /strategy panel's gate,
 * so a NULL / absent / parity-failed record yields "—" for EVERY field and can
 * never render a fabricated 0 (never trusts the wire payload's per-field null
 * discipline alone). worst_streak_pnl_pct / longest_underwater_days come from
 * row.resilience.* (the sibling block).
 *
 * @param {object} row - the top100 rank-1 row.
 * @returns {{worstStreakPct: string, uwDays: string}} the cell strings.
 */
function bxResilienceRankOneCells(row) {
  var r = row && row.resilience && typeof row.resilience === "object" ? row.resilience : null;
  if (!r || r.stats_live !== true) {
    return { worstStreakPct: "—", uwDays: "—" };
  }
  var pct = r.worst_streak_pnl_pct;
  var days = r.longest_underwater_days;
  return {
    worstStreakPct: pct === null || pct === undefined ? "—" : String(pct) + "%",
    uwDays: days === null || days === undefined ? "—" : String(days)
  };
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
  var TOP100_URL = "/api/v1/oneclick/top100";
  var TRADES_URL = "/api/v1/oneclick/trades";
  var VERIFY_URL = "/api/v1/oneclick/verify";
  var PROGRESS_URL = "/api/v1/sweep-progress";
  var EVENTS_PAGE_LIMIT = 200;
  var TRADES_PAGE_LIMIT = 100;

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

  /* Fill EVERY [data-oc="<key>"] node (the v29 re-feed hooks). textContent
     only; a missing hook is a silent no-op so the widget stays inert on
     pages that do not carry the card. */
  function setOc(key, text) {
    var nodes = document.querySelectorAll('[data-oc="' + key.replace(/[^a-z0-9_-]/gi, "") + '"]');
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].textContent = text;
    }
  }

  /* Signed rupee display for an integer-paisa amount — EXACT via the shared
     bx-explain.js integer converter (never float math); a non-safe-int
     reads the honest em-dash, never a fabricated figure. */
  function paisaText(paisa) {
    if (!window.BXExplain) {
      return "—";
    }
    var rupees = window.BXExplain.paisaToRupees(paisa);
    if (rupees === null) {
      return "—";
    }
    return (Number(paisa) >= 0 ? "+₹" : "−₹") + rupees.replace(/^-/, "");
  }

  /* Full-precision metric passthrough: numbers render via String() (never
     rounded — money is the ONLY 2dp surface, R27/DEC-MONEY-2DP-001). */
  function metricText(value) {
    if (value === null || value === undefined) {
      return "—";
    }
    return String(value);
  }

  /* [11] sort-basis honesty label (DEC-ONECLICK-TOP100-OBJECT-ROOT-001):
     the one-click Top-100 order is the artifact's R30 preset (default
     min_dd_first — smallest max-drawdown first), which deliberately DIFFERS
     from /results (Rank-by metric, descending). Label only — neither sort
     is changed; a missing preset falls back honestly to the default name. */
  function sortBasisText(preset) {
    var name =
      typeof preset === "string" && preset ? preset : "min_dd_first (default)";
    return (
      "sort basis: artifact preset " +
      name +
      " — differs from /results (Rank-by metric, descending)"
    );
  }

  /* [W3-R4]: delegate to the ONE pure builder above — a null-lo/hi writer
     band reads the honest not-stamped copy, never "between null–null". */
  function vixBandText(band) {
    return bxVixBandText(band);
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
      fillManifestSummary(payload);
      renderProgressStrips(payload);
    }
  }

  /* Manifest-fed card/summary hooks: selection · actual counts · suspect.
     Every value comes from the CONTRACT manifest verbatim; a missing key
     keeps the honest em-dash (never an invented default). */
  function fillManifestSummary(payload) {
    var m = payload.manifest;
    if (!m || typeof m !== "object") {
      return;
    }
    var sel = m.selection && typeof m.selection === "object" ? m.selection : {};
    if (sel.from !== undefined && sel.to !== undefined) {
      setOc("date_range", String(sel.from) + " → " + String(sel.to));
    }
    if (Array.isArray(sel.symbols)) {
      setOc("sel_symbols", sel.symbols.join(", "));
    }
    var actual = m.actual && typeof m.actual === "object" ? m.actual : {};
    if (actual.enumerated !== undefined) {
      setOc("actual_enumerated", Number(actual.enumerated).toLocaleString("en-IN"));
    }
    if (actual.tested !== undefined) {
      setOc("actual_tested", Number(actual.tested).toLocaleString("en-IN"));
    }
    if (actual.skipped !== undefined) {
      setOc("actual_skipped", Number(actual.skipped).toLocaleString("en-IN"));
    }
    if (m.dedup_skips !== undefined) {
      setOc("dedup_skips", Number(m.dedup_skips).toLocaleString("en-IN"));
    }
    setOc("run_id", String(m.run_id === undefined ? payload.run_id : m.run_id));
    if (m.suspect === true) {
      setOc("suspect", "SUSPECT — tripwire fired; treat this run's numbers as unverified");
    } else if (m.suspect === false) {
      setOc("suspect", "no tripwire fired");
    }
  }

  /* Per-stage timing strips (R41 progress surface) from the manifest's
     per_stage_timings (contract: [{stage, seconds}] — a dict form is
     tolerated). Bar widths are proportions of the max stage; labels are
     textContent; nothing is invented for a missing/malformed entry. */
  function renderProgressStrips(payload) {
    var host = byId("ocProgressStrips");
    if (!host || !window.BXUI) {
      return;
    }
    var m = payload.manifest;
    var raw = m && typeof m === "object" ? m.per_stage_timings : null;
    var entries = [];
    var i;
    if (Array.isArray(raw)) {
      for (i = 0; i < raw.length; i++) {
        var e = raw[i];
        if (e && typeof e === "object" && typeof e.seconds === "number") {
          entries.push({ stage: String(e.stage), seconds: e.seconds });
        }
      }
    } else if (raw && typeof raw === "object") {
      for (var k in raw) {
        if (Object.prototype.hasOwnProperty.call(raw, k) && typeof raw[k] === "number") {
          entries.push({ stage: k, seconds: raw[k] });
        }
      }
    }
    window.BXUI.clear(host);
    if (!entries.length) {
      host.appendChild(
        window.BXUI.el("div", "oc-sub", "per-stage timings — awaiting real run artifact")
      );
      return;
    }
    var max = 0;
    for (i = 0; i < entries.length; i++) {
      if (entries[i].seconds > max) {
        max = entries[i].seconds;
      }
    }
    for (i = 0; i < entries.length; i++) {
      var row = window.BXUI.el("div", "oc-strip-row", null);
      row.appendChild(window.BXUI.el("span", "oc-strip-stage", entries[i].stage));
      var bar = window.BXUI.el("div", "oc-strip-bar", null);
      var fill = window.BXUI.el("div", "oc-strip-fill", null);
      var pct = max > 0 ? (entries[i].seconds / max) * 100 : 0;
      fill.style.width = pct.toFixed(1) + "%";
      bar.appendChild(fill);
      row.appendChild(bar);
      row.appendChild(window.BXUI.el("span", "oc-strip-secs", String(entries[i].seconds) + " s"));
      host.appendChild(row);
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

  /* ── Top-100 re-feed: rank-1 story card + the combos table rows ── */

  function conditionsText(row) {
    return Array.isArray(row.conditions) ? row.conditions.join(" × ") : "—";
  }

  function fillRankOneCard(rows) {
    if (!rows.length) {
      // T-M2: an EMPTY-but-valid artifact keeps the awaiting em-dashes —
      // the card NEVER indexes rows[0].
      setOc("r1_name", "no winners stored for this run");
      return;
    }
    var row = rows[0];
    var m = row.metrics && typeof row.metrics === "object" ? row.metrics : {};
    setOc("r1_rank", "#" + String(row.rank));
    setOc("r1_name", conditionsText(row));
    setOc("r1_tf", String(row.tf));
    setOc("r1_direction", String(row.direction));
    setOc("r1_arm", String(row.arm));
    setOc("r1_net", m.net_pnl_paisa === undefined ? "—" : paisaText(m.net_pnl_paisa));
    setOc("r1_max_dd", m.max_dd_paisa === undefined ? "—" : paisaText(m.max_dd_paisa));
    setOc("r1_win_rate", metricText(m.win_rate));
    setOc("r1_pf", metricText(m.profit_factor));
    setOc("r1_sharpe", metricText(m.sharpe));
    setOc("r1_dsr", metricText(m.dsr));
    setOc("r1_cpcv", metricText(m.cpcv_pass_pct));
    setOc("r1_trades", metricText(m.trades));
    setOc("r1_vix_band", vixBandText(row.vix_band));
    // Losing-streak resilience rank-1 hooks (the sibling block; the pure
    // builder is stats_live-gated, so a NULL/parity-failed record → "—",
    // never a fabricated 0 — defense-in-depth mirror of the /strategy panel).
    var rc = bxResilienceRankOneCells(row);
    setOc("r1_worst_streak_pct", rc.worstStreakPct);
    setOc("r1_uw_days", rc.uwDays);
  }

  /* Build the #combosBody rows from the top-100 artifact. Cells carry the
     SAME data-sort convention the pre-existing v29 sort/filter JS reads;
     every string reaches the DOM via textContent (F-M2). Columns with no
     contract source (Gross P&L / Charges / Score) read the honest em-dash,
     never a fabricated figure. */
  function renderCombosRows(payload) {
    var tbody = byId("combosBody");
    if (!tbody || !window.BXUI) {
      return;
    }
    window.BXUI.clear(tbody);
    var rows = payload.rows || [];

    function td(text, cls, sort) {
      var cell = document.createElement("td");
      if (cls) {
        cell.className = cls;
      }
      if (sort !== undefined) {
        cell.setAttribute("data-sort", String(sort));
      }
      cell.textContent = text;
      return cell;
    }

    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var m = row.metrics && typeof row.metrics === "object" ? row.metrics : {};
      var tr = document.createElement("tr");
      tr.appendChild(td("#" + String(row.rank), null, row.rank));
      tr.appendChild(td(String(row.mask_hex), null, undefined));
      tr.appendChild(td(conditionsText(row), null, undefined));
      tr.appendChild(td("—", null, undefined)); // Inst — instrument is not a top100 row field
      tr.appendChild(td(String(row.tf), null, undefined));
      tr.appendChild(td(String(row.direction), null, undefined));
      tr.appendChild(td(metricText(m.trades), null, m.trades === undefined ? 0 : m.trades));
      tr.appendChild(td("—", null, 0)); // Gross P&L — no contract source yet
      tr.appendChild(td("—", null, 0)); // Charges — Track 1 is signal-only
      var net = m.net_pnl_paisa;
      tr.appendChild(
        td(
          net === undefined ? "—" : paisaText(net),
          typeof net === "number" && net < 0 ? "neg" : "pos",
          net === undefined ? 0 : net
        )
      );
      tr.appendChild(td(metricText(m.sharpe), null, m.sharpe === undefined ? 0 : m.sharpe));
      tr.appendChild(td(metricText(m.dsr), null, m.dsr === undefined ? 0 : m.dsr));
      tr.appendChild(td("—", null, 0)); // Score — composite lands with P3
      var act = document.createElement("td");
      var link = document.createElement("a");
      link.className = "combos-action";
      link.textContent = "View";
      link.setAttribute(
        "href",
        "/strategy?run_id=" +
          encodeURIComponent(state.selectedRunId || "") +
          "&rank=" +
          encodeURIComponent(String(row.rank))
      );
      act.appendChild(link);
      tr.appendChild(act);
      tbody.appendChild(tr);
    }
  }

  function refreshTop100() {
    if (!state.selectedRunId) {
      return;
    }
    var url = TOP100_URL + "?run_id=" + encodeURIComponent(state.selectedRunId);
    window.BXPoll.fetchJson(url, 12000)
      .then(function (payload) {
        if (!payload || typeof payload !== "object") {
          return;
        }
        var note = byId("ocTop100Note");
        if (note) {
          if (payload.status === "ok" || payload.status === "partial") {
            note.textContent =
              String(payload.row_count) +
              " winner(s) stored" +
              (payload.empty_note ? " — " + String(payload.empty_note) : "") +
              (payload.status === "partial" ? " — PARTIAL: " + String(payload.detail) : "");
          } else {
            note.textContent = String(payload.detail || "awaiting real run artifact");
          }
        }
        if (payload.status === "ok" || payload.status === "partial") {
          setOc("top100_sort_basis", sortBasisText(payload.preset));
          fillRankOneCard(payload.rows || []);
          renderCombosRows(payload);
          if (payload.rows && payload.rows.length && payload.row_count !== undefined) {
            setOc("tested_rank_line", "#" + String(payload.rows[0].rank) + " of " + String(payload.row_count) + " stored winners");
          }
        }
      })
      .catch(function () {
        var note = byId("ocTop100Note");
        if (note) {
          note.textContent = "top-100 read failed — retrying on the next run refresh";
        }
      });
  }

  /* ── Trades (rank-1 on the operator page) + the equity curve ── */

  function renderTradeRows(tbody, rows) {
    window.BXUI.clear(tbody);

    function td(text, cls) {
      var cell = document.createElement("td");
      if (cls) {
        cell.className = cls;
      }
      cell.textContent = text;
      return cell;
    }

    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var tr = document.createElement("tr");
      var pnl = typeof r.pnl_paisa === "number" ? r.pnl_paisa : null;
      tr.className = pnl !== null && pnl >= 0 ? "win-row" : "loss-row";
      tr.appendChild(td(String(r.direction).toUpperCase(), null));
      tr.appendChild(td(String(r.entry_ts), "dim"));
      tr.appendChild(td(metricText(r.entry_px), null));
      tr.appendChild(td(String(r.exit_ts), "dim"));
      tr.appendChild(td(metricText(r.exit_px), null));
      tr.appendChild(td(metricText(r.lots), null));
      tr.appendChild(td(String(r.exit_reason), null));
      tr.appendChild(td(pnl === null ? "—" : paisaText(pnl), pnl !== null && pnl < 0 ? "neg" : "pos"));
      tr.appendChild(td(r.costs_paisa === undefined ? "—" : paisaText(r.costs_paisa), "dim"));
      // Additive #1275 honesty flag: mae/mfe carry their granularity ("1m"|"tf").
      var gran = typeof r.mae_granularity === "string" ? " (" + r.mae_granularity + ")" : "";
      tr.appendChild(td(metricText(r.mae) + gran, null));
      tr.appendChild(td(metricText(r.mfe) + gran, null));
      // R31 honest NULL: a missing VIX renders "null — not stamped", never 0.
      tr.appendChild(td(r.vix_entry === null || r.vix_entry === undefined ? "null" : metricText(r.vix_entry), null));
      tr.appendChild(td(r.vix_exit === null || r.vix_exit === undefined ? "null" : metricText(r.vix_exit), null));
      tbody.appendChild(tr);
    }
  }

  function renderEquity(rows) {
    var host = byId("ocEquityHost");
    if (!host || !window.BXUI || !window.BXChart) {
      return;
    }
    window.BXUI.clear(host);
    var series = window.BXChart.equitySeries(rows || []);
    if (series.empty) {
      host.appendChild(
        window.BXUI.el("div", "oc-sub", "no trades yet — awaiting real run artifact")
      );
      return;
    }
    var svg = window.BXChart.svgEl("svg", { viewBox: "0 0 1000 220", preserveAspectRatio: "none" });
    var path = window.BXChart.equityPathNode(series, 1000, 220, 8);
    if (path) {
      svg.appendChild(path);
    }
    host.appendChild(svg);
    var noteBits = ["per-trade close equity — minute-path equity lands with P3"];
    if (series.overflow) {
      noteBits.push("OVERFLOW: cumulative paisa left the safe-integer range — curve truncated");
    }
    if (series.invalid_rows) {
      noteBits.push(String(series.invalid_rows) + " invalid row(s) skipped");
    }
    /* [W2-4] Ruling-18 null costs: counted at Rs 0 — the curve is honestly
       labeled gross of those unverified costs, never an empty chart. */
    if (series.null_cost_rows) {
      noteBits.push(
        String(series.null_cost_rows) +
          " row(s) carry null costs (by design until P5) — curve is gross of those costs"
      );
    }
    noteBits.push("net " + paisaText(series.final_paisa));
    host.appendChild(window.BXUI.el("div", "oc-sub", noteBits.join(" · ")));
  }

  function refreshTrades() {
    if (!state.selectedRunId) {
      return;
    }
    // Failures render into the SIBLING error host (never over the table),
    // so Retry can rebuild without losing the static table skeleton.
    var panel = byId("ocTradesError");
    var url =
      TRADES_URL +
      "?run_id=" + encodeURIComponent(state.selectedRunId) +
      "&rank=1&offset=0&limit=" + TRADES_PAGE_LIMIT;
    window.BXPoll.fetchJson(url, 12000)
      .then(function (payload) {
        if (!payload || typeof payload !== "object") {
          throw new Error("malformed payload");
        }
        if (panel && window.BXUI) {
          window.BXUI.clear(panel); // a recovered poll clears the error card
        }
        var badge = byId("ocTradesSource");
        if (badge) {
          badge.textContent = "source: " + String(payload.source);
        }
        var note = byId("ocTradesNote");
        var tbody = byId("ocTradesBody");
        if (payload.status === "ok" || payload.status === "partial") {
          if (tbody) {
            renderTradeRows(tbody, payload.rows || []);
          }
          if (note) {
            var total = payload.total === null || payload.total === undefined
              ? "unknown total"
              : "of " + String(payload.total);
            note.textContent =
              String((payload.rows || []).length) + " trade(s) shown " + total +
              (payload.status === "partial" ? " — PARTIAL: " + String(payload.detail) : "");
          }
          renderEquity(payload.rows || []);
        } else {
          if (note) {
            note.textContent = String(payload.detail || "awaiting real run artifact");
          }
          renderEquity([]);
        }
      })
      .catch(function (err) {
        if (panel && window.BXUI) {
          window.BXUI.errorCard(panel, {
            status: err && err.status,
            offline: !!(err && err.name === "TypeError"),
            retry: refreshTrades
          });
        }
      });
  }

  /* ── R44 VERIFY-ME panel (7 checks, 5-state truth — server verdicts) ── */

  function renderVerify(payload) {
    var host = byId("ocVerifyChecks");
    if (!host || !window.BXUI) {
      return;
    }
    window.BXUI.clear(host);
    var checks = payload.checks || [];
    for (var i = 0; i < checks.length; i++) {
      var check = checks[i];
      var row = window.BXUI.el("div", "oc-verify-row", null);
      row.appendChild(
        window.BXUI.el("span", "oc-verify-state oc-verify-" + String(check.state), String(check.state))
      );
      row.appendChild(window.BXUI.el("span", "oc-verify-name", String(check.id) + " · " + String(check.name)));
      row.appendChild(window.BXUI.el("span", "oc-verify-detail", String(check.detail)));
      host.appendChild(row);
    }
    var fr = byId("ocVerifyFunnel");
    if (fr && payload.funnel_reconcile) {
      window.BXUI.clear(fr);
      var lines = payload.funnel_reconcile.lines || [];
      for (var j = 0; j < lines.length; j++) {
        fr.appendChild(window.BXUI.el("div", "oc-verify-line", String(lines[j])));
      }
    }
  }

  function refreshVerify(check) {
    if (!state.selectedRunId) {
      return;
    }
    var panel = byId("ocVerifyError");
    var url = VERIFY_URL + "?run_id=" + encodeURIComponent(state.selectedRunId);
    if (check) {
      url += "&check=" + encodeURIComponent(check); // O(file) chain replay — button-click ONLY
    }
    window.BXPoll.fetchJson(url, 20000)
      .then(function (payload) {
        if (!payload || typeof payload !== "object") {
          throw new Error("malformed payload");
        }
        if (panel && window.BXUI) {
          window.BXUI.clear(panel); // a recovered poll clears the error card
        }
        renderVerify(payload);
      })
      .catch(function (err) {
        if (panel && window.BXUI) {
          window.BXUI.errorCard(panel, {
            status: err && err.status,
            offline: !!(err && err.name === "TypeError"),
            retry: function () {
              refreshVerify(check);
            }
          });
        }
      });
  }

  /* One place refreshes every run-scoped one-shot surface. */
  function loadRunScoped() {
    refreshTop100();
    refreshTrades();
    refreshVerify(null);
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
    var runSelect = byId("ocRunSelect");
    if (runSelect) {
      runSelect.addEventListener("change", function () {
        window.BXOneclick.selectRun(runSelect.value);
      });
    }
    var verifyChainBtn = byId("ocVerifyChainBtn");
    if (verifyChainBtn) {
      verifyChainBtn.addEventListener("click", function () {
        refreshVerify("chain"); // the O(file) replay runs on CLICK only
      });
    }
    var verifyRefreshBtn = byId("ocVerifyRefreshBtn");
    if (verifyRefreshBtn) {
      verifyRefreshBtn.addEventListener("click", function () {
        refreshVerify(null);
      });
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

    // Runs poller: run discovery + the ticker strip's run half + the run
    // selector options (textContent-built <option> nodes).
    var runsPoller = window.BXPoll.createPoller({
      url: RUNS_URL,
      onData: function (data) {
        lastRuns = data;
        if (!state.selectedRunId && data.latest_run_id) {
          state.selectedRunId = data.latest_run_id;
          loadRunScoped(); // first selection: fetch the run-scoped one-shots
        }
        var select = byId("ocRunSelect");
        if (select && data.runs) {
          var ids = [];
          for (var i = 0; i < data.runs.length; i++) {
            ids.push(String(data.runs[i].run_id));
          }
          if (ids.join("|") !== select.getAttribute("data-oc-ids")) {
            select.setAttribute("data-oc-ids", ids.join("|"));
            while (select.firstChild) {
              select.removeChild(select.firstChild);
            }
            for (var j = 0; j < ids.length; j++) {
              var opt = document.createElement("option");
              opt.value = ids[j];
              opt.textContent = ids[j];
              select.appendChild(opt);
            }
          }
          if (state.selectedRunId) {
            select.value = state.selectedRunId;
          }
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
    VIX_BAND_NOT_STAMPED: BX_VIX_BAND_NOT_STAMPED,
    RESILIENCE_UNAVAILABLE: BX_RESILIENCE_UNAVAILABLE,
    ringPush: bxRingPush,
    ringFilter: bxRingFilter,
    eventLineText: bxEventLineText,
    funnelWriteAllowed: bxFunnelWriteAllowed,
    funnelProvenance: bxFunnelProvenance,
    vixBandText: bxVixBandText,
    vixBandCellText: bxVixBandCellText,
    resilienceCells: bxResilienceCells,
    resilienceRankOneCells: bxResilienceRankOneCells,
    resilienceSelfLocate: bxResilienceSelfLocate,
    resilienceGuide: bxResilienceGuide,
    istDateTime: bxIstDateTime,
    oneshotFetch: bxOneshotFetch,
    renderChainMap: renderChainMap,
    selectRun: function (runId) {
      state.selectedRunId = runId;
      state.ring = [];
      state.ringSeen = {};
      state.eventsCursor = -1;
      state.earlierCursor = 0;
      renderTimeline();
      loadRunScoped();
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
    BX_VIX_BAND_NOT_STAMPED: BX_VIX_BAND_NOT_STAMPED,
    BX_RESILIENCE_UNAVAILABLE: BX_RESILIENCE_UNAVAILABLE,
    bxRingPush: bxRingPush,
    bxRingFilter: bxRingFilter,
    bxEventLineText: bxEventLineText,
    bxFunnelWriteAllowed: bxFunnelWriteAllowed,
    bxFunnelProvenance: bxFunnelProvenance,
    bxVixBandText: bxVixBandText,
    bxVixBandCellText: bxVixBandCellText,
    bxResilienceCells: bxResilienceCells,
    bxResilienceRankOneCells: bxResilienceRankOneCells,
    bxResilienceSelfLocate: bxResilienceSelfLocate,
    bxResilienceGuide: bxResilienceGuide,
    bxIstDateTime: bxIstDateTime
  };
}
