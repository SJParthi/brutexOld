/* bx-strategy-panel.js — the mountable chart-first strategy panel
 * (DEC-OPERATOR-PAGE-CONSOLIDATE-001; the strategy.html inline wiring of
 * DEC-OPERATOR-PAGE-REAL-001, moved VERBATIM into a shared module so the
 * standalone /strategy page AND the operator-portal strategy drawer consume
 * ONE implementation).
 *
 * DOM LAW: every artifact-derived string reaches the DOM via textContent;
 * the SVG is built with createElementNS ONLY (bx-chart.js builders). The
 * candle window: ?trade=N centers the /api/v1/db/candles fetch on that
 * trade's entry_ts session (F-B1a); a trade timestamp outside the loaded
 * window DROPS its arrow + shows the loud BXChart.ARROW_DROPPED_NOTE
 * (F-B1b) — never a snapped wrong-candle arrow.
 *
 * API: mount() binds listeners exactly ONCE (mount-once law — leak-free by
 * construction); every open / re-rank / run-switch goes through
 * update({runId, rank, tradeIdx}) which bumps the LOAD GENERATION, resets
 * the per-target state (trades/candles/equity/offset) and re-fetches.
 * Every async render arm opens with the `gen !== state.gen` guard (the
 * gbPollGen precedent) so a stale rank-3 response can never paint over a
 * newer rank-7 load, a closed drawer, or a switched run.
 *
 * Injection seams (defaults reproduce the standalone page byte-for-byte):
 *   - getTop100   → window.BXOneclick.getTop100 (the promise memo);
 *   - getManifest → window.BXOneclick.lastManifest (run-id-checked; a cold
 *                   start / foreign run falls back to ONE manifest one-shot);
 *   - animate     → false (the standalone page); the portal passes true and
 *                   the module adds the reveal classes AFTER values land via
 *                   textContent (motion covenants: value first, motion
 *                   second; a node whose text stays '—' gets NO pop class;
 *                   honest states render instantly un-animated).
 *
 * Hover explain is DELIBERATELY NOT bound here — bx-oneclick.js binds it
 * exactly once (presence-guarded on #bxTip; the single-bind law).
 *
 * Self-mount: at DOMContentLoaded iff a [data-bx-autopanel="strategy"]
 * node exists (strategy.html carries the attribute; v29's drawer mounts
 * explicitly via bx-operator-portal.js).
 *
 * Honest envelope (Golden Rule #10): the pure helpers (bxTfToInterval ·
 * bxEquityLabelText) are executed for real by node goldens; the DOM layer
 * is browser-only and pinned by SOURCE-STRING tests (presence, not
 * execution — the #1557 Playwright job does not drive these pages).
 * Element lookup is document-global byId on the historic st* ids —
 * uniqueness is enforced by the portal id-uniqueness fence (the ONE id
 * rename: the trades source badge is stTradesSource, freeing v29's own
 * rank-1 #ocTradesSource from the latent cross-page stomp).
 */
"use strict";

/* tf label ("5m"/"1h"/"1d") → the lake interval partition name. Unmapped
   labels return null and the chart shows an HONEST "cannot map" note —
   never a guessed partition. Pure — node-goldened. */
function bxTfToInterval(tf) {
  var s = String(tf || '');
  if (/^\d+minute$|^\d+hour$|^1day$/.test(s)) { return s; }
  var m = /^(\d+)m$/.exec(s);
  if (m) { return m[1] + 'minute'; }
  var h = /^(\d+)h$/.exec(s);
  if (h) { return h[1] + 'hour'; }
  if (s === '1d') { return '1day'; }
  return null;
}

/* The 3-way honest equity-label chooser (item 4C — the #48 overlap): the
   STORED minute-path series wins when the equity_ref sidecar served points;
   an ok-verdict sidecar that served ZERO rows is its own honest state (the
   series is EMPTY for this run, not unavailable — hostile-round fold); any
   other miss reads the per-trade fallback label. Pure — node-goldened. */
function bxEquityLabelText(hasSeries, okEmpty) {
  if (hasSeries) {
    return 'minute-path equity — the stored real series (R27)';
  }
  return okEmpty
    ? 'per-trade close equity (minute-path series empty for this run)'
    : 'per-trade close equity (minute-path unavailable for this run)';
}

/* ── the DOM layer (browser only; the node harness gets the pure core) ── */

(function () {
  if (typeof document === "undefined") {
    return; // node harness: pure exports only
  }

  var TRADES_LIMIT = 100;
  var state = {
    gen: 0, // the load generation — bumped by every update()
    runId: null,
    rank: 1,
    tradeIdx: null, // ?trade= (0-based row index on the current page)
    offset: 0,
    symbol: null,
    interval: null,
    trades: [],
    candles: [],
    equity: [], // stored minute-path points (item 4C — empty ⇒ per-trade fallback)
    equityTotal: null,
    // ok-verdict-but-zero-rows sidecar (hostile-round fold): distinguishes
    // "series EMPTY for this run" from "sidecar unavailable" in the label.
    equityOkEmpty: false,
    cascadeDone: null // one trades cascade per (rank, page) — animate only
  };
  var deps = {
    getTop100: null,
    getManifest: null,
    animate: false
  };
  var mounted = false;

  function byId(id) { return document.getElementById(id); }

  function money(paisa) {
    if (!window.BXExplain) { return '—'; }
    var r = window.BXExplain.paisaToRupees(paisa);
    if (r === null) { return '—'; }
    return (Number(paisa) >= 0 ? '+₹' : '−₹') + r.replace(/^-/, '');
  }

  /* TZ-SAFE parse ([W3-R1] second layer): the ONE shared BXChart.parseTsIst —
     a naive API string is IST wall-clock pinned to +05:30, an offset-carrying
     trade timestamp passes through — so arrow placement is TZ-invariant in
     every browser, never a browser-local Date parse of a naive string. */
  function parseTs(raw) {
    return window.BXChart.parseTsIst(raw);
  }

  /* Motion covenant #1/#2: values snap to truth via textContent FIRST; a
     one-shot scale pop is added only on a REAL value render (animate mode),
     and a node whose text stays '—' gets NO pop class. */
  function popRealValues(ids) {
    if (!deps.animate) { return; }
    for (var i = 0; i < ids.length; i++) {
      var n = byId(ids[i]);
      if (!n || n.textContent === '—') { continue; } // no pop on '—' (covenant #2)
      (function (node) {
        node.classList.add('bxd-pop');
        var cleared = false;
        function clearPop() {
          if (cleared) { return; } // idempotent — event + timer both call it
          cleared = true;
          node.classList.remove('bxd-pop');
          node.removeEventListener('animationend', clearPop);
        }
        node.addEventListener('animationend', clearPop);
        // Bounded one-shot fallback (fix round 3, R4-fresheyes-1 +
        // R4-refuteb-1 — the trades-cascade house idiom below): under
        // prefers-reduced-motion `animation: none` never fires animationend
        // (nor does a display:none'd subtree after a quick close), so the
        // listener-only cleanup stranded + accumulated one listener per
        // open. 500 ms > the .45s pop; the guard above makes the late
        // arm a no-op when the event already cleaned up.
        setTimeout(clearPop, 500);
      })(n);
    }
  }

  /* ── candle chart ── */

  function renderChart() {
    var host = byId('stChartHost');
    if (!host || !window.BXChart || !window.BXUI) { return; }
    window.BXUI.clear(host);
    var geom = window.BXChart.candleGeom(state.candles, 1000, 340, 10);
    if (geom.empty) {
      host.appendChild(window.BXUI.el('div', 'note',
        'no candles in the window — pull data on /universe or widen the range'));
      return;
    }
    var svg = window.BXChart.svgEl('svg', { viewBox: '0 0 1000 340', preserveAspectRatio: 'none' });
    // animate mode: the whole session materializes as ONE animated group
    // (O(1) animated elements over ≤500 nodes — covenant #4).
    var candleHost = svg;
    if (deps.animate) {
      candleHost = window.BXChart.svgEl('g', { 'class': 'bxd-chart-reveal' });
      svg.appendChild(candleHost);
    }
    var nodes = window.BXChart.candleNodes(geom);
    for (var i = 0; i < nodes.length; i++) { candleHost.appendChild(nodes[i]); }
    // Entry/exit arrows for the focused trade (or every loaded trade when
    // none is focused): the -1 sentinel DROPS the arrow + shows the loud
    // note — never a snapped wrong-candle arrow (F-B1).
    var arrowHost = svg;
    if (deps.animate) {
      arrowHost = window.BXChart.svgEl('g', { 'class': 'bxd-arrows-in' });
      svg.appendChild(arrowHost);
    }
    var focus = state.tradeIdx === null ? state.trades : [state.trades[state.tradeIdx]];
    var droppedAny = false;
    for (var t = 0; t < focus.length; t++) {
      var trade = focus[t];
      if (!trade) { continue; }
      var entryMs = parseTs(trade.entry_ts);
      var exitMs = parseTs(trade.exit_ts);
      var arrow = window.BXChart.tradeArrow(entryMs, exitMs, state.candles);
      if (arrow.dropped) { droppedAny = true; continue; }
      var eC = geom.candles[arrow.entry_idx];
      var xC = geom.candles[arrow.exit_idx];
      arrowHost.appendChild(window.BXChart.arrowNode(eC.x, eC.yLow + 10, 'entry', String(trade.direction)));
      arrowHost.appendChild(window.BXChart.arrowNode(xC.x, xC.yHigh - 10, 'exit', String(trade.direction)));
    }
    host.appendChild(svg);
    if (geom.flat) {
      host.appendChild(window.BXUI.el('div', 'note', 'flat price range in this window'));
    }
    var noteEl = byId('stArrowNote');
    if (noteEl) {
      // The honest dropped-arrow note renders instantly, un-animated
      // (covenant #2 — honest states are never behind a reveal).
      noteEl.hidden = !droppedAny;
      if (droppedAny) { noteEl.textContent = window.BXChart.ARROW_DROPPED_NOTE; }
    }
  }

  function loadCandles() {
    if (!state.symbol || !state.interval) {
      byId('stChartCoords').textContent =
        'chart unavailable — the run selection carries no mappable symbol/timeframe yet';
      return;
    }
    // F-B1a: the window centers on the focused trade's entry session; the
    // fallback is the last loaded trade's session (or none → honest note).
    var anchor = null;
    if (state.tradeIdx !== null && state.trades[state.tradeIdx]) {
      anchor = parseTs(state.trades[state.tradeIdx].entry_ts);
    } else if (state.trades.length) {
      anchor = parseTs(state.trades[0].entry_ts);
    }
    if (anchor === null) {
      byId('stChartCoords').textContent =
        'no trade timestamps yet — chart loads once trades exist (awaiting real run artifact)';
      return;
    }
    // The session-day anchor is the IST calendar date of the entry instant
    // (pure epoch arithmetic — TZ-invariant, never a browser-local Date).
    var day = window.BXChart.istDateOfEpoch(anchor);
    var parts = String(state.symbol).split('-');
    var exchange = parts.length > 1 ? parts[0] : 'NSE';
    var url = '/api/v1/db/candles' +
      '?exchange=' + encodeURIComponent(exchange) +
      '&segment=INDEX' +
      '&symbol=' + encodeURIComponent(state.symbol) +
      '&interval=' + encodeURIComponent(state.interval) +
      '&start=' + encodeURIComponent(day) +
      '&end=' + encodeURIComponent(day) +
      '&limit=500';
    byId('stChartCoords').textContent =
      state.symbol + ' · ' + state.interval + ' · session ' + day +
      (state.tradeIdx !== null ? ' · centered on trade #' + String(state.tradeIdx + 1) : '');
    var gen = state.gen;
    window.BXOneclick.oneshotFetch(url, byId('stChartError'), function (payload) {
      if (gen !== state.gen) { return; } // stale-target response — discarded
      /* [W3-R1]: the ONE route-shape-driven mapper (BXChart) — the route's
         column is "timestamp" (db_view.CANDLE_COLUMNS); the old inline
         idx-dot-ts read undefined on every REAL payload so no candle ever
         plotted (the fixture blindspot). A missing required column shows
         an honest note, never a silently empty chart. */
      var mapped = window.BXChart.candleRowsFromDbPayload(payload);
      if (mapped.missing !== null) {
        byId('stChartCoords').textContent =
          'candle payload is missing the "' + mapped.missing + '" column — cannot chart';
        return;
      }
      /* Honest skip count: an unreadable row (unparseable ts / corrupt OHLC)
         is COUNTED, never silently dropped — the ONE shared note builder. */
      var skippedNote = window.BXChart.skippedRowsNote(mapped.skipped);
      if (skippedNote !== '') {
        byId('stChartCoords').textContent += ' · ' + skippedNote;
      }
      state.candles = mapped.rows;
      renderChart();
    });
  }

  /* ── trades (inline at load — F-M5) ── */

  function renderTrades() {
    var tbody = byId('stTradesBody');
    if (!tbody) { return; }
    while (tbody.firstChild) { tbody.removeChild(tbody.firstChild); }

    function td(text, cls) {
      var cell = document.createElement('td');
      if (cls) { cell.className = cls; }
      cell.textContent = text;
      return cell;
    }

    for (var i = 0; i < state.trades.length; i++) {
      var r = state.trades[i];
      var tr = document.createElement('tr');
      tr.style.setProperty('--bxd-i', String(i)); // cascade stagger index (animate only)
      var pnl = typeof r.pnl_paisa === 'number' ? r.pnl_paisa : null;
      tr.appendChild(td(String(r.direction).toUpperCase(), null));
      tr.appendChild(td(String(r.entry_ts), null));
      tr.appendChild(td(String(r.entry_px), null));
      tr.appendChild(td(String(r.exit_ts), null));
      tr.appendChild(td(String(r.exit_px), null));
      tr.appendChild(td(r.lots === null || r.lots === undefined ? '—' : String(r.lots), null));
      tr.appendChild(td(String(r.exit_reason), null));
      tr.appendChild(td(pnl === null ? '—' : money(pnl), pnl !== null && pnl < 0 ? 'neg' : 'pos'));
      tr.appendChild(td(r.costs_paisa === undefined ? '—' : money(r.costs_paisa), null));
      /* Additive #1275 honesty flag: mae/mfe carry their granularity ("1m"|"tf"). */
      var gran = typeof r.mae_granularity === 'string' ? ' (' + r.mae_granularity + ')' : '';
      tr.appendChild(td(String(r.mae) + gran, null));
      tr.appendChild(td(String(r.mfe) + gran, null));
      tr.appendChild(td(r.vix_entry === null || r.vix_entry === undefined ? 'null' : String(r.vix_entry), null));
      tr.appendChild(td(r.vix_exit === null || r.vix_exit === undefined ? 'null' : String(r.vix_exit), null));
      var chartTd = document.createElement('td');
      var link = document.createElement('a');
      link.textContent = '📈 chart';
      // The ONE BXPortalState.strategyHref builder owns the href shape
      // (hostile run_ids never survive raw — encodeURIComponent inside).
      link.setAttribute('href',
        window.BXPortalState.strategyHref(state.runId || '', String(state.rank), i));
      chartTd.appendChild(link);
      tr.appendChild(chartTd);
      tbody.appendChild(tr);
    }
    // Trades cascade (animate mode): first render per (rank, page) only —
    // filter/pager revisits land instantly (covenant #2).
    if (deps.animate && state.trades.length) {
      var cascadeKey = String(state.rank) + '|' + String(state.offset);
      if (state.cascadeDone !== cascadeKey) {
        state.cascadeDone = cascadeKey;
        tbody.classList.add('bxd-cascade');
        setTimeout(function () { tbody.classList.remove('bxd-cascade'); }, 900);
      }
    }
  }

  function setEquityLabel(text) {
    var label = byId('stEquityLabel');
    if (label) { label.textContent = text; }
  }

  function renderEquity() {
    var host = byId('stEquityHost');
    if (!host || !window.BXChart || !window.BXUI) { return; }
    window.BXUI.clear(host);
    /* Item 4C (the #48 overlap): the STORED minute-path series wins when the
       equity_ref sidecar served points; the per-trade close cumsum is the
       honest fallback, relabeled so the two can never be confused. */
    if (state.equity.length) {
      var mp = window.BXChart.minutePathSeries(state.equity);
      if (!mp.empty) {
        // Covenant #2: the honesty label is set BEFORE any draw-in starts.
        setEquityLabel(bxEquityLabelText(true, false));
        var mpSvg = window.BXChart.svgEl('svg', { viewBox: '0 0 1000 220', preserveAspectRatio: 'none' });
        var mpPath = window.BXChart.equityPathNode(mp, 1000, 220, 8);
        if (mpPath) {
          if (deps.animate) {
            // Display-only draw-in: pathLength normalization + the CSS
            // dashoffset keyframe — zero path-length reads, zero JS layout
            // measurement (BXChart.equityPathNode byte-untouched).
            mpPath.setAttribute('pathLength', '1');
            mpPath.classList.add('bxd-equity-draw');
          }
          mpSvg.appendChild(mpPath);
        }
        host.appendChild(mpSvg);
        var mpBits = ['minute-path equity from the results-depth sidecar'];
        if (mp.invalid_rows) { mpBits.push(String(mp.invalid_rows) + ' invalid point(s) skipped'); }
        if (state.equityTotal !== null && state.equity.length < state.equityTotal) {
          mpBits.push('first ' + String(state.equity.length) + ' of ' + String(state.equityTotal) + ' sessions shown');
        }
        mpBits.push('final ' + money(mp.final_paisa));
        host.appendChild(window.BXUI.el('div', 'note', mpBits.join(' · ')));
        renderArith();
        return;
      }
    }
    // An ok-verdict sidecar that served ZERO rows is its own honest state —
    // the series is EMPTY for this run, not unavailable (hostile-round fold).
    var fallbackLabel = bxEquityLabelText(false, state.equityOkEmpty);
    setEquityLabel(fallbackLabel);
    var series = window.BXChart.equitySeries(state.trades);
    if (series.empty) {
      host.appendChild(window.BXUI.el('div', 'note', 'no trades yet — awaiting real run artifact'));
      return;
    }
    var svg = window.BXChart.svgEl('svg', { viewBox: '0 0 1000 220', preserveAspectRatio: 'none' });
    var path = window.BXChart.equityPathNode(series, 1000, 220, 8);
    if (path) {
      if (deps.animate) {
        // The labeled per-trade fallback draws in the SAME way — the label
        // above was set first, so the two can never be confused mid-motion.
        path.setAttribute('pathLength', '1');
        path.classList.add('bxd-equity-draw');
      }
      svg.appendChild(path);
    }
    host.appendChild(svg);
    var bits = [fallbackLabel];
    if (series.overflow) { bits.push('OVERFLOW: curve truncated at the safe-integer wall'); }
    if (series.invalid_rows) { bits.push(String(series.invalid_rows) + ' invalid row(s) skipped'); }
    // [W2-4] Ruling-18 null costs: counted at ₹0 — the curve is honestly
    // labeled gross of those unverified costs, never an empty chart.
    if (series.null_cost_rows) {
      bits.push(String(series.null_cost_rows) + ' row(s) carry null costs (by design until P5) — curve is gross of those costs');
    }
    // #12 vocabulary (audit re-break MINOR): the per-trade curve total is the
    // GROSS signal figure — never "net" while costs are structurally absent.
    bits.push('gross signal ' + money(series.final_paisa));
    host.appendChild(window.BXUI.el('div', 'note', bits.join(' · ')));
    renderArith();
  }

  function renderArith() {
    // R44 #4: the plain-English claim decomposes to line-by-line arithmetic
    // over the trades LOADED on this page (self-scoping — C-M4b).
    if (window.BXExplain) {
      var arith = window.BXExplain.tradeArithmetic(state.trades);
      byId('stArith').textContent = arith.lines.join(' · ');
    }
  }

  function loadEquity() {
    // The stored minute-path series (GET /api/v1/oneclick/equity — the
    // entry's equity_ref sidecar). Any non-ok verdict (awaiting/corrupt) or
    // fetch failure keeps the honest per-trade fallback — never a blank
    // chart, never a fabricated curve.
    var url = '/api/v1/oneclick/equity' +
      '?run_id=' + encodeURIComponent(state.runId) +
      '&rank=' + encodeURIComponent(String(state.rank));
    var gen = state.gen;
    window.BXPoll.fetchJson(url, 12000)
      .then(function (payload) {
        if (gen !== state.gen) { return; } // stale-target response — discarded
        if (payload && typeof payload === 'object' &&
            (payload.status === 'ok' || payload.status === 'partial')) {
          state.equity = payload.rows || [];
          state.equityTotal = payload.total === undefined ? null : payload.total;
          state.equityOkEmpty = state.equity.length === 0;
        } else {
          state.equity = [];
          state.equityTotal = null;
          state.equityOkEmpty = false;
        }
        renderEquity();
      })
      .catch(function () {
        if (gen !== state.gen) { return; } // stale-target response — discarded
        state.equity = [];
        state.equityTotal = null;
        state.equityOkEmpty = false;
        renderEquity();
      });
  }

  function loadTrades() {
    var url = '/api/v1/oneclick/trades' +
      '?run_id=' + encodeURIComponent(state.runId) +
      '&rank=' + encodeURIComponent(String(state.rank)) +
      '&offset=' + String(state.offset) +
      '&limit=' + String(TRADES_LIMIT);
    var gen = state.gen;
    window.BXOneclick.oneshotFetch(url, byId('stTradesError'), function (payload) {
      if (gen !== state.gen) { return; } // stale-target response — discarded
      var badge = byId('stTradesSource');
      if (badge) { badge.textContent = 'source: ' + String(payload.source); }
      var note = byId('stTradesNote');
      if (payload.status === 'ok' || payload.status === 'partial') {
        state.trades = payload.rows || [];
        var total = payload.total === null || payload.total === undefined
          ? 'unknown total' : 'of ' + String(payload.total);
        note.textContent = String(state.trades.length) + ' trade(s) shown ' + total +
          ' · offset ' + String(state.offset) +
          (payload.status === 'partial' ? ' — PARTIAL: ' + String(payload.detail) : '');
      } else {
        state.trades = [];
        note.textContent = String(payload.detail || 'awaiting real run artifact');
      }
      byId('stPage').textContent = 'page ' + String(Math.floor(state.offset / TRADES_LIMIT) + 1);
      renderTrades();
      renderEquity();
      loadCandles();
    });
  }

  /* ── the story card from the top-100 row ── */

  function loadRow() {
    var gen = state.gen;
    var getTop100 = deps.getTop100 || window.BXOneclick.getTop100;
    getTop100(state.runId)
      .then(function (payload) {
        if (gen !== state.gen) { return; } // stale-target response — discarded
        if (!payload || typeof payload !== 'object') {
          throw new Error('malformed payload');
        }
        if (payload.status !== 'ok' && payload.status !== 'partial') {
          byId('stName').textContent = String(payload.detail || 'awaiting real run artifact');
          return;
        }
        var rows = payload.rows || [];
        var row = null;
        for (var i = 0; i < rows.length; i++) {
          if (Number(rows[i].rank) === state.rank) { row = rows[i]; break; }
        }
        if (!row) {
          // The honest rank-miss state: never a 500, never a blank, never an
          // auto-redirect (bad-rank deep links land here).
          byId('stName').textContent =
            'rank ' + String(state.rank) + ' is not in the stored top-100 (' +
            String(payload.row_count) + ' rows stored)';
          return;
        }
        var m = row.metrics && typeof row.metrics === 'object' ? row.metrics : {};
        byId('stRank').textContent = '#' + String(row.rank);
        byId('stName').textContent = Array.isArray(row.conditions) ? row.conditions.join(' × ') : '—';
        /* The winner's OWN market (the writer's additive `instrument` key) —
           the ONE shared builder; a pre-instrument artifact reads the honest
           "—" (DEC-ONECLICK-INSTRUMENT-DISPLAY-001). */
        byId('stInstr').textContent = window.BXOneclick.instrumentText(row.instrument);
        byId('stTf').textContent = String(row.tf);
        byId('stDir').textContent = String(row.direction);
        byId('stArm').textContent = String(row.arm);
        byId('stMask').textContent = String(row.mask_hex);
        byId('stFamily').textContent = String(row.family_id);
        byId('stNet').textContent = m.net_pnl_paisa === undefined ? '—' : money(m.net_pnl_paisa);
        byId('stMaxDd').textContent = m.max_dd_paisa === undefined ? '—' : money(m.max_dd_paisa);
        byId('stSharpe').textContent = m.sharpe === undefined ? '—' : String(m.sharpe);
        byId('stDsr').textContent = m.dsr === undefined ? '—' : String(m.dsr);
        byId('stCpcv').textContent = m.cpcv_pass_pct === undefined ? '—' : String(m.cpcv_pass_pct);
        byId('stWinRate').textContent = m.win_rate === undefined ? '—' : String(m.win_rate);
        byId('stPf').textContent = m.profit_factor === undefined ? '—' : String(m.profit_factor);
        byId('stTrades').textContent = m.trades === undefined ? '—' : String(m.trades);
        /* [W3-R4]: the ONE shared band builder — a null-lo/hi writer band
           reads the honest not-stamped copy, never "between null–null". */
        byId('stVixBand').textContent = window.BXOneclick.vixBandText(row.vix_band);
        /* HIGH #14 (DEC-ONECLICK-WINNER-DETAIL-TRUTH-001): the SHIPPED
           results-depth aggregates, straight from entry.metrics — the pure
           BXOneclick builders format composites, money via the house
           formatter; anything absent reads the honest "—". */
        byId('stExpectancy').textContent = m.expectancy_paisa === undefined || m.expectancy_paisa === null ? '—' : money(m.expectancy_paisa);
        byId('stRecovery').textContent = m.recovery_factor === undefined || m.recovery_factor === null ? '—' : String(m.recovery_factor);
        byId('stLossStreak').textContent = m.max_consecutive_losses === undefined || m.max_consecutive_losses === null ? '—' : String(m.max_consecutive_losses);
        byId('stMae').textContent = window.BXOneclick.avgExtremeText(m.mae_avg_paisa, m.mae_worst_paisa, 'worst', money);
        byId('stMfe').textContent = window.BXOneclick.avgExtremeText(m.mfe_avg_paisa, m.mfe_best_paisa, 'best', money);
        byId('stMinuteDd').textContent = window.BXOneclick.minutePathDdText(m.max_dd_minute_path_paisa, m.max_dd_minute_path_pct, money);
        byId('stExitMix').textContent = window.BXOneclick.exitMixText(m.exits);
        /* worst5_days is a METRICS key (top100_artifact._metrics_payload —
           NOT under regime; the audit's part-1 refinement). */
        byId('stWorst5').textContent = window.BXOneclick.worst5DaysText(m.worst5_days, money);
        /* HIGH #13: the regime breakdown from entry.regime — the stale
           deferral is dead; the data ships. */
        var reg = row.regime && typeof row.regime === 'object' ? row.regime : {};
        byId('stTrendSplit').textContent = window.BXOneclick.trendSplitText(reg.by_trend, money);
        byId('stRegimeWonder').textContent = window.BXOneclick.regimeWonderText(reg.one_regime_wonder, reg.regime_wonder_axis);
        // Motion covenant #1/#2: values landed via textContent above; the
        // one-shot pop rides ONLY nodes that show a real value.
        popRealValues([
          'stNet', 'stMaxDd', 'stSharpe', 'stDsr', 'stCpcv', 'stWinRate',
          'stPf', 'stTrades', 'stExpectancy', 'stRecovery', 'stLossStreak',
          'stMae', 'stMfe', 'stMinuteDd', 'stExitMix', 'stWorst5'
        ]);
        state.interval = bxTfToInterval(row.tf);
        loadTrades();
        loadEquity();
      })
      .catch(function () {
        if (gen !== state.gen) { return; } // stale-target response — discarded
        byId('stName').textContent =
          'top-100 read failed — retry from the winners table or reload';
      });
  }

  function loadManifestSymbol(next) {
    // The manifest seam: the memo'd manifest (bx-oneclick's poller) serves
    // the symbol with ZERO extra fetches when it is for THIS run; a cold
    // start / foreign run pays ONE manifest one-shot (same honest awaiting
    // behavior as the standalone page when absent).
    var getManifest = deps.getManifest ||
      (window.BXOneclick && window.BXOneclick.lastManifest) ||
      function () { return null; };
    var cached = getManifest();
    if (cached && typeof cached === 'object' &&
        String(cached.run_id) === String(state.runId) && cached.manifest) {
      var cm = cached.manifest;
      if (cm.selection && Array.isArray(cm.selection.symbols) && cm.selection.symbols.length) {
        state.symbol = String(cm.selection.symbols[0]);
      }
      next();
      return;
    }
    var gen = state.gen;
    // The cold arm rides the in-flight /manifest dedupe (adversarial-review
    // fold, P-M1): a cold deep-link open no longer pays a SECOND manifest
    // fetch concurrently with the manifest poller's own — both share ONE
    // in-flight promise. A manifest failure still calls next() so the story
    // card loads and the chart shows its honest no-mappable-symbol note
    // (never a silently dead drawer).
    window.BXOneclick.getManifestOnce(state.runId, 12000)
      .then(function (payload) {
        if (gen !== state.gen) { return; } // stale-target response — discarded
        var m = payload && typeof payload === 'object' ? payload.manifest : null;
        if (m && m.selection && Array.isArray(m.selection.symbols) && m.selection.symbols.length) {
          state.symbol = String(m.selection.symbols[0]);
        }
        next();
      })
      .catch(function () {
        if (gen !== state.gen) { return; } // stale-target response — discarded
        next(); // honest: symbol stays null — the chart names the miss
      });
  }

  /* ── mount-once + update() (graft C.3 — the leak-free listener law) ── */

  function bxStrategyPanelMount(options) {
    var opts = options && typeof options === 'object' ? options : {};
    deps.getTop100 = opts.getTop100 || null;
    deps.getManifest = opts.getManifest || null;
    deps.animate = opts.animate === true;
    if (mounted) {
      return; // mount-once: listeners are never re-bound
    }
    mounted = true;
    byId('stPrev').addEventListener('click', function () {
      state.offset = Math.max(0, state.offset - TRADES_LIMIT);
      state.tradeIdx = null;
      loadTrades();
    });
    byId('stNext').addEventListener('click', function () {
      state.offset += TRADES_LIMIT;
      state.tradeIdx = null;
      loadTrades();
    });
  }

  /* The story-card / trades / chart / equity hosts painted for the PREVIOUS
     target (adversarial-review fold, NEW-R3-1): the gen guard stops stale
     WRITES but not stale RESIDUE — a reopen after a run switch must never
     show the old run's rank under the new pill during the fetch window.
     Values reset to the honest placeholder; every host resets to the
     templates' HONEST awaiting copy — NEVER a "loading…" progress claim
     (fix round 2, edges F1 + NEW-R2R1-1/-2, refuter-EXECUTED): loadRow's
     terminal arms (awaiting/corrupt · rank-miss · read failure),
     loadCandles' no-symbol/no-anchor arms and a trades-fetch failure all
     deliberately leave these hosts untouched, so a progress string here
     would read as an in-flight fetch FOREVER on exactly the honest states
     (Golden Rule #1); the success paths overwrite them anyway. The HOSTS
     reset to the templates' fresh-load copy byte-for-byte; the dash fields
     reset to the neutral '—' — three ids diverge from their template copy
     (stName "awaiting real run artifact" · stArith "" · stVixBand "no VIX
     band") and rest at '—' until their writers run: honest neutral
     placeholders, never a progress or data claim (fix round 3 prose
     precision, R3-edges-1). */
  function resetPanelDom() {
    var dashIds = [
      'stRank', 'stName', 'stInstr', 'stTf', 'stDir', 'stArm', 'stMask',
      'stFamily', 'stNet', 'stMaxDd', 'stSharpe', 'stDsr', 'stCpcv',
      'stWinRate', 'stPf', 'stTrades', 'stVixBand', 'stArith',
      'stExpectancy', 'stRecovery', 'stLossStreak', 'stMae', 'stMfe',
      'stMinuteDd', 'stExitMix', 'stWorst5', 'stTrendSplit', 'stRegimeWonder'
    ];
    for (var i = 0; i < dashIds.length; i++) {
      var n = byId(dashIds[i]);
      if (n) { n.textContent = '—'; }
    }
    var tbody = byId('stTradesBody');
    if (tbody) {
      while (tbody.firstChild) { tbody.removeChild(tbody.firstChild); }
    }
    var note = byId('stTradesNote');
    if (note) {
      note.textContent =
        "awaiting real run artifact — trades appear once the run's results-depth sidecars land";
    }
    if (window.BXUI) {
      var chart = byId('stChartHost');
      if (chart) {
        window.BXUI.clear(chart);
        chart.appendChild(window.BXUI.el('div', 'note',
          'no candles loaded yet — awaiting run + stored 1-minute data'));
      }
      var equity = byId('stEquityHost');
      if (equity) {
        window.BXUI.clear(equity);
        equity.appendChild(window.BXUI.el('div', 'note',
          'no trades yet — awaiting real run artifact'));
      }
    }
    /* NEW-R2R1-2: the five previously-omitted residue fields — an old
       run's coords/arrow-warning/equity-label/page/source badge must not
       survive a terminal reopen under the new rank pill. */
    var coords = byId('stChartCoords');
    if (coords) {
      coords.textContent =
        "chart window resolves from the trade (?trade=N centers on that trade's entry) or the run's last session";
    }
    var arrow = byId('stArrowNote');
    if (arrow) {
      arrow.hidden = true;
      arrow.textContent = '';
    }
    setEquityLabel('awaiting real run artifact');
    var page = byId('stPage');
    if (page) { page.textContent = 'page 1'; }
    var badge = byId('stTradesSource');
    if (badge) { badge.textContent = 'source: none'; }
  }

  function bxStrategyPanelUpdate(target) {
    var t = target && typeof target === 'object' ? target : {};
    state.gen += 1; // the load-generation bump — stale responses discarded
    if (t.runId !== undefined) { state.runId = t.runId; }
    state.rank = t.rank === undefined || t.rank === null ? 1 : t.rank;
    state.tradeIdx = t.tradeIdx === undefined ? null : t.tradeIdx;
    state.offset = 0;
    state.symbol = null;
    state.interval = null;
    state.trades = [];
    state.candles = [];
    state.equity = [];
    state.equityTotal = null;
    state.equityOkEmpty = false;
    if (!state.runId) { return; }
    resetPanelDom(); // NEW-R3-1: no previous-target residue while fetching
    byId('stRunId').textContent = String(state.runId);
    loadManifestSymbol(loadRow);
  }

  function bxStrategyPanelMountFromLocation() {
    // The standalone-page entry: the PINNED deep-link contract lives in the
    // pure BXPortalState.parseStrategyParams codec (run_id + rank + trade).
    var parsed = window.BXPortalState.parseStrategyParams(window.location.search);
    bxStrategyPanelMount({});
    if (parsed.runId) {
      bxStrategyPanelUpdate({ runId: parsed.runId, rank: parsed.rank, tradeIdx: parsed.tradeIdx });
    } else {
      var gen = state.gen;
      window.BXOneclick.oneshotFetch('/api/v1/oneclick/runs', null, function (runs) {
        if (gen !== state.gen) { return; } // a later explicit update wins
        if (runs.latest_run_id) {
          bxStrategyPanelUpdate({ runId: runs.latest_run_id, rank: parsed.rank, tradeIdx: parsed.tradeIdx });
        } else {
          byId('stName').textContent = String(runs.detail || 'awaiting real run artifact');
        }
      });
    }
  }

  window.BXStrategyPanel = {
    TRADES_LIMIT: TRADES_LIMIT,
    mount: bxStrategyPanelMount,
    update: bxStrategyPanelUpdate,
    mountFromLocation: bxStrategyPanelMountFromLocation,
    getState: function () { return state; }
  };

  function autoMount() {
    if (document.querySelector('[data-bx-autopanel="strategy"]')) {
      bxStrategyPanelMountFromLocation();
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
    bxTfToInterval: bxTfToInterval,
    bxEquityLabelText: bxEquityLabelText
  };
}
