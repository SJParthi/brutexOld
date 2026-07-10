/* bx-screener-panel.js — the mountable strategy-screener panel
 * (DEC-OPERATOR-PAGE-CONSOLIDATE-001; the screener.html inline wiring of
 * DEC-OPERATOR-PAGE-REAL-001, moved VERBATIM into a shared module so the
 * standalone /screener page AND the operator-portal winners section consume
 * ONE implementation).
 *
 * DOM LAW: rows/cells are built with document.createElement + textContent
 * ONLY — zero string-to-DOM sinks in this module (the movers COLS-array
 * STRUCTURE is adopted WITHOUT any string-to-markup row builder, F-M1).
 * Sorting: BXScreener.compare (full-precision relational compare; every tie
 * breaks rank-ascending — a TOTAL order). Filtering: BXScreener.applyFilters
 * (a null-VIX-band row under an active band filter is EXCLUDED-but-COUNTED,
 * F-m3). Money cells render via BXExplain.paisaToRupees (exact integer
 * paisa→2dp rupees); metrics render full precision, NEVER re-rounded.
 *
 * Injection seams (defaults reproduce the standalone page byte-for-byte):
 *   - columns    → window.BXScreener.COLUMNS (the ONE shared config);
 *   - getTop100  → window.BXOneclick.getTop100 (the promise memo — one
 *                  /api/v1/oneclick/top100 fetch per run selection);
 *   - onRowClick → navigate via the ONE BXPortalState.strategyHref builder
 *                  (the operator portal passes a drawer-opening callback).
 *
 * Hover explain is DELIBERATELY NOT bound here — bx-oneclick.js binds it
 * exactly once (presence-guarded on #bxTip); a second bind would double the
 * tooltip listeners (the single-bind law).
 *
 * Self-mount: at DOMContentLoaded iff a [data-bx-autopanel="screener"]
 * node exists (screener.html carries the attribute; v29 mounts explicitly
 * and lazily on first winners expand).
 *
 * Honest envelope (Golden Rule #10): the pure helpers (cell text · count
 * line · filter-option harvest) are executed for real by node goldens; the
 * DOM layer is browser-only and pinned by SOURCE-STRING tests (presence,
 * not execution — the #1557 Playwright job does not drive these pages).
 * Element lookup is document-global byId on the historic sc* ids —
 * uniqueness is enforced by the portal id-uniqueness fence.
 */
"use strict";

/**
 * Pure cell-text builder (the screener cell law, node-goldened).
 *
 * @param {object} row - a top-100 artifact row.
 * @param {object} col - a BXScreener.COLUMNS entry ({key, type, money…}).
 * @param {{fieldValue: function, vixBandCellText: function,
 *   money: function}} deps - injected formatters (node goldens are
 *   formatter-explicit; the browser wires the shared builders).
 * @returns {string} the rendered cell text — full precision for metrics,
 *   honest '—' for absent values (never a fabricated figure).
 */
function bxScreenerCellText(row, col, deps) {
  var v = deps.fieldValue(row, col.key);
  if (col.key === 'conditions') {
    return Array.isArray(v) ? v.join(' × ') : '—';
  }
  if (col.type === 'vix') {
    /* [W3-R4]: the ONE shared compact band cell — a null-lo/hi writer band
       reads "not stamped" (R31 honest NULL), never "null–null". */
    return deps.vixBandCellText(v);
  }
  if (v === null || v === undefined) {
    return '—';
  }
  if (col.money) {
    return deps.money(v);
  }
  return String(v); // full precision — metrics are NEVER re-rounded (R27)
}

/**
 * Pure count-line builder. The DENOMINATOR is the payload's row_count —
 * never an assumed 100 (T-M2); the NUMERATOR is the count actually
 * RENDERED — when the render cap truncated the table it says so ([W2-5]
 * DEC-ONECLICK-TOP100-OBJECT-ROOT-001: "500 of 500 shown" over a 200-row
 * table was a lie); the null-VIX exclusion is COUNTED loudly (F-m3).
 *
 * @param {number} cap - the rendered row count.
 * @param {number|null} rowCount - the payload row_count (null = unknown).
 * @param {number} sortedLen - the filtered+sorted row count.
 * @param {number} RENDER_CAP - the render cap in force.
 * @param {number} nullVixExcluded - the counted null-VIX exclusions.
 * @param {function} nullVixNote - the shared note builder
 *   (BXScreener.nullVixNote — golden-pinned wording).
 * @returns {string} the count line.
 */
function bxWinnersCountLine(cap, rowCount, sortedLen, RENDER_CAP, nullVixExcluded, nullVixNote) {
  var total = rowCount === null || rowCount === undefined ? '?' : String(rowCount);
  var line = String(cap) + ' of ' + total + ' shown';
  if (cap < sortedLen) {
    line += ' (render cap ' + String(RENDER_CAP) + ' — use Show all)';
  }
  if (nullVixExcluded > 0) {
    line += ' ' + nullVixNote(nullVixExcluded);
  }
  return line;
}

/**
 * Pure filter-option harvest (the absent-key law): tf/direction options
 * come from every loaded row; instrument options come ONLY from rows that
 * CARRY the additive key — a pre-instrument artifact offers no option, so
 * the exact-match filter can never go active against rows that cannot say.
 *
 * @param {object[]} rows - the loaded artifact rows.
 * @returns {{tf: string[], direction: string[], instrument: string[]}}
 *   deduped, sorted option values.
 */
function bxHarvestFilterOptions(rows) {
  var tfSeen = {};
  var dirSeen = {};
  var instSeen = {};
  var list = Array.isArray(rows) ? rows : [];
  for (var i = 0; i < list.length; i++) {
    tfSeen[String(list[i].tf)] = true;
    dirSeen[String(list[i].direction)] = true;
    var inst = list[i].instrument;
    if (typeof inst === 'string' && inst !== '') { instSeen[inst] = true; }
  }
  return {
    tf: Object.keys(tfSeen).sort(),
    direction: Object.keys(dirSeen).sort(),
    instrument: Object.keys(instSeen).sort()
  };
}

/* ── the DOM layer (browser only; the node harness gets the pure core) ── */

(function () {
  if (typeof document === "undefined") {
    return; // node harness: pure exports only
  }

  var RENDER_CAP = 200; // 100 rows render directly; the cap guards corrupt/oversized artifacts
  var state = {
    runId: null,
    rows: [],
    rowCount: null,
    sort: { field: 'rank', type: 'num', dir: 'asc' },
    showAll: false
  };
  // The winners load generation (adversarial-review fold, NEW-R2-1 — the
  // gbPollGen idiom): a rapid run switch fires overlapping loadRows and the
  // memo'd promises can resolve INVERTED (cold run A after warm run B) — a
  // stale run's rows must never paint under a newer run's strip.
  var loadGen = 0;
  var deps = {
    columns: null,
    getTop100: null,
    onRowClick: null,
    onRender: null
  };
  var mounted = false;

  function byId(id) { return document.getElementById(id); }

  function columns() {
    return deps.columns || window.BXScreener.COLUMNS;
  }

  function money(paisa) {
    if (!window.BXExplain) { return '—'; }
    var r = window.BXExplain.paisaToRupees(paisa);
    if (r === null) { return '—'; }
    return (Number(paisa) >= 0 ? '+₹' : '−₹') + r.replace(/^-/, '');
  }

  function cellText(row, col) {
    return bxScreenerCellText(row, col, {
      fieldValue: window.BXScreener.fieldValue,
      vixBandCellText: function (v) {
        return window.BXOneclick.vixBandCellText(v);
      },
      money: money
    });
  }

  function buildHead() {
    var head = byId('scHead');
    while (head.firstChild) { head.removeChild(head.firstChild); }
    var cols = columns();
    for (var i = 0; i < cols.length; i++) {
      var th = document.createElement('th');
      th.setAttribute('data-field', cols[i].key);
      th.setAttribute('data-type', cols[i].type);
      th.setAttribute('data-explain', cols[i].explain);
      var arrow = state.sort.field === cols[i].key ? (state.sort.dir === 'asc' ? ' ▲' : ' ▼') : '';
      th.textContent = cols[i].label + arrow;
      head.appendChild(th);
    }
  }

  function currentFilters() {
    var num = function (id) {
      var raw = (byId(id).value || '').trim();
      return raw === '' ? null : Number(raw);
    };
    var filters = {};
    var tf = byId('scTf').value;
    if (tf) { filters.tf = [tf]; }
    var dir = byId('scDir').value;
    if (dir) { filters.direction = dir; }
    var inst = byId('scInstrument').value;
    if (inst) { filters.instrument = inst; }
    var minWin = num('scMinWin');
    if (minWin !== null) { filters.min_win_rate = minWin; }
    var minPf = num('scMinPf');
    if (minPf !== null) { filters.min_pf = minPf; }
    var maxDd = num('scMaxDd');
    if (maxDd !== null) { filters.max_abs_dd_paisa = maxDd; }
    var lo = num('scVixLo');
    if (lo !== null) { filters.vix_lo = lo; }
    var hi = num('scVixHi');
    if (hi !== null) { filters.vix_hi = hi; }
    return filters;
  }

  function render() {
    buildHead();
    var applied = window.BXScreener.applyFilters(state.rows, currentFilters());
    var sorted = window.BXScreener.sortRows(
      applied.rows, state.sort.field, state.sort.type, state.sort.dir
    );
    var tbody = byId('scRows');
    while (tbody.firstChild) { tbody.removeChild(tbody.firstChild); }
    var cap = state.showAll ? sorted.length : Math.min(sorted.length, RENDER_CAP);
    var cols = columns();
    for (var i = 0; i < cap; i++) {
      var row = sorted[i];
      var tr = document.createElement('tr');
      tr.setAttribute('data-rank', String(row.rank));
      // Keyboard operability (adversarial-review fold, C-M3/E-M5): rows are
      // real tab stops activated by Enter/Space (the delegated keydown
      // handler shares the click path), and a focused row is what the
      // portal's close-focus-restore lands back on.
      tr.setAttribute('tabindex', '0');
      tr.setAttribute('role', 'button');
      // Winners-cascade stagger index (inert without the portal's cascade
      // class; CSS min() caps the stagger — see bx-oneclick-panels.css).
      tr.style.setProperty('--wn-i', String(i));
      for (var c = 0; c < cols.length; c++) {
        var tdEl = document.createElement('td');
        var text = cellText(row, cols[c]);
        if (cols[c].money) {
          var raw = window.BXScreener.fieldValue(row, cols[c].key);
          if (typeof raw === 'number') { tdEl.className = raw < 0 ? 'neg' : 'pos'; }
        }
        tdEl.setAttribute('data-explain', cols[c].explain);
        tdEl.textContent = text;
        tr.appendChild(tdEl);
      }
      tbody.appendChild(tr);
    }
    var wrap = byId('scShowAllWrap');
    wrap.hidden = state.showAll || sorted.length <= RENDER_CAP;
    byId('scCount').textContent = bxWinnersCountLine(
      cap, state.rowCount, sorted.length, RENDER_CAP,
      applied.null_vix_excluded, window.BXScreener.nullVixNote
    );
    // Post-render hook (adversarial-review fold, C-N1/NEW-R1-2): the tbody
    // is rebuilt from scratch, so the portal re-applies its you-are-here
    // row mark after EVERY render (async first mount AND filter/sort
    // re-renders alike).
    if (deps.onRender) {
      deps.onRender();
    }
  }

  function fillFilterOptions() {
    var opts = bxHarvestFilterOptions(state.rows);
    function fill(sel, keys) {
      while (sel.childNodes.length > 1) { sel.removeChild(sel.lastChild); }
      for (var k = 0; k < keys.length; k++) {
        var opt = document.createElement('option');
        opt.value = keys[k];
        opt.textContent = keys[k];
        sel.appendChild(opt);
      }
    }
    fill(byId('scTf'), opts.tf);
    fill(byId('scDir'), opts.direction);
    fill(byId('scInstrument'), opts.instrument);
  }

  /* One-shot presentation over the memo'd promise: skeleton while pending,
     BXUI.errorCard with Retry on any failure (F-M4 — a rejected memo
     self-evicts in bx-oneclick.js, so Retry genuinely refetches). */
  function loadRows(runId) {
    loadGen += 1; // NEW-R2-1: a newer load discards every older response
    var gen = loadGen;
    state.runId = runId;
    byId('scRunId').textContent = runId;
    var panel = byId('scPanel');
    if (panel && window.BXUI) {
      window.BXUI.skeleton(panel, 4, 1, {
        watchdogMs: 6000,
        watchdogText: 'still working — the server may be busy'
      });
    }
    var getTop100 = deps.getTop100 || window.BXOneclick.getTop100;
    getTop100(runId)
      .then(function (payload) {
        if (gen !== loadGen) { return; } // stale-run response — discarded
        if (!payload || typeof payload !== 'object') {
          throw new Error('malformed payload');
        }
        if (panel && window.BXUI) {
          window.BXUI.clear(panel);
        }
        var fx = byId('ocFixtureBadge');
        if (fx) { fx.hidden = !payload.fixture_root_active; }
        if (payload.status === 'ok' || payload.status === 'partial') {
          state.rows = payload.rows || [];
          state.rowCount = payload.row_count === undefined ? null : payload.row_count;
          // [11] sort-basis label: name the ACTUAL artifact preset (fallback
          // to the default name when the payload carries none) — label only.
          byId('scSortBasis').textContent =
            'sort basis: artifact preset '
            + (typeof payload.preset === 'string' && payload.preset
              ? payload.preset : 'min_dd_first (default)')
            + ' — differs from /results (Rank-by metric, descending);'
            + ' column clicks re-sort this page only';
          byId('scDetail').textContent =
            payload.status === 'partial'
              ? 'PARTIAL artifact: ' + String(payload.detail)
              : (payload.empty_note ? String(payload.empty_note) : 'top-100 artifact loaded');
          fillFilterOptions();
          render();
        } else {
          state.rows = [];
          state.rowCount = null;
          byId('scDetail').textContent = String(payload.detail || 'awaiting real run artifact');
          render();
        }
      })
      .catch(function (err) {
        if (gen !== loadGen) { return; } // stale-run failure — discarded
        if (panel && window.BXUI) {
          window.BXUI.errorCard(panel, {
            status: err && err.status,
            offline: !!(err && err.name === 'TypeError'),
            retry: function () { loadRows(runId); }
          });
        }
      });
  }

  function defaultOnRowClick(rank, runId) {
    // Row click → the strategy page (≤2 clicks law, R36) — the ONE
    // BXPortalState.strategyHref builder owns the href shape.
    window.location.href = window.BXPortalState.strategyHref(runId, rank);
  }

  var controller = {
    loadRows: loadRows,
    rerender: render,
    getState: function () { return state; }
  };

  function bxScreenerPanelMount(options) {
    var opts = options && typeof options === 'object' ? options : {};
    deps.columns = opts.columns || null;
    deps.getTop100 = opts.getTop100 || null;
    deps.onRowClick = opts.onRowClick || defaultOnRowClick;
    deps.onRender = opts.onRender || null;
    if (mounted) {
      return controller; // mount-once: listeners are never re-bound
    }
    mounted = true;
    buildHead();
    // Delegated th-click sort (ONE listener — survives head rebuilds).
    byId('scHead').addEventListener('click', function (ev) {
      var th = ev.target.closest('th');
      if (!th) { return; }
      var field = th.getAttribute('data-field');
      if (state.sort.field === field) {
        state.sort.dir = state.sort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        state.sort = { field: field, type: th.getAttribute('data-type'), dir: 'desc' };
      }
      render();
    });
    byId('scRows').addEventListener('click', function (ev) {
      var tr = ev.target.closest('tr');
      if (!tr || !state.runId) { return; }
      deps.onRowClick(tr.getAttribute('data-rank') || '1', state.runId);
    });
    // Keyboard activation (C-M3/E-M5): Enter/Space on a focused row shares
    // the click path; Space is prevented from page-scrolling.
    byId('scRows').addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' && ev.key !== ' ') { return; }
      var tr = ev.target && ev.target.closest ? ev.target.closest('tr') : null;
      if (!tr || !state.runId) { return; }
      ev.preventDefault();
      deps.onRowClick(tr.getAttribute('data-rank') || '1', state.runId);
    });
    ['scTf', 'scDir', 'scInstrument', 'scMinWin', 'scMinPf', 'scMaxDd', 'scVixLo', 'scVixHi'].forEach(function (id) {
      byId(id).addEventListener('input', render);
    });
    byId('scShowAll').addEventListener('click', function () {
      state.showAll = true;
      render();
    });
    if (opts.runId) {
      loadRows(String(opts.runId));
    }
    return controller;
  }

  function bxScreenerPanelMountFromLocation() {
    var ctl = bxScreenerPanelMount({});
    // run_id from the query string wins; else the latest discovered run.
    var params = new URLSearchParams(window.location.search);
    var rid = params.get('run_id');
    if (rid) {
      ctl.loadRows(rid);
    } else {
      window.BXOneclick.oneshotFetch('/api/v1/oneclick/runs', byId('scPanel'), function (runs) {
        if (runs.latest_run_id) {
          ctl.loadRows(runs.latest_run_id);
        } else {
          byId('scDetail').textContent = String(runs.detail || 'awaiting real run artifact');
        }
      });
    }
    return ctl;
  }

  window.BXScreenerPanel = {
    RENDER_CAP: RENDER_CAP,
    mount: bxScreenerPanelMount,
    mountFromLocation: bxScreenerPanelMountFromLocation
  };

  function autoMount() {
    if (document.querySelector('[data-bx-autopanel="screener"]')) {
      bxScreenerPanelMountFromLocation();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoMount);
  } else {
    autoMount();
  }
})();

/* Non-browser harness (the node golden) gets the PURE helpers only. */
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    bxScreenerCellText: bxScreenerCellText,
    bxWinnersCountLine: bxWinnersCountLine,
    bxHarvestFilterOptions: bxHarvestFilterOptions
  };
}
