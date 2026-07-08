/*!
 * bx-oneclick-chart.js — shared hand-rolled SVG candle + entry/exit-arrow +
 * equity-curve renderer for the P4 one-click sweep display surfaces
 * (DEC-INSTITUTIONAL-ONECLICK-001, R36/R24/R31/R44 check-3).
 *
 * Mounted on TWO surfaces with the IDENTICAL rendering code (never two
 * implementations that could silently diverge): templates/strategy.html
 * (stage 4) and the VERIFY-ME "trade_chart_match" mini-widget
 * (templates/verify_me.html, a later stage) — see the FINAL APPROVED SPEC's
 * ``chart_arrows_approach``.
 *
 * NO new charting dependency: this hand-rolls plain SVG (createElementNS +
 * setAttribute + numeric coordinates only), the SAME hand-authored-SVG
 * pattern templates/v29.html already uses for its equity curve
 * (viewBox + polyline/polygon, no <canvas>, no CDN). Checked pyproject.toml /
 * package.json first — no charting library ships anywhere in this repo.
 *
 * Data sources (both already exist — NO new backend added by this file):
 *   - GET /api/v1/db/candles (brutex.dashboard.db_view.query_candles) — OHLCV
 *     bars, row-major arrays keyed by the response's own ``columns`` list.
 *   - GET /api/v1/sweep/trades (brutex.dashboard.oneclick_artifacts.read_trades_page)
 *     — entry/exit ts/px, direction, exit_reason, pnl_paisa, mae, mfe,
 *     vix_entry/vix_exit.
 *
 * XSS HARD RULE (CI-enforced, universal-engineering-standards / CLAUDE.md
 * §11): every value this module writes into the DOM is either a raw SVG
 * attribute (numeric coordinate / fixed hex colour / fixed dasharray — never
 * a sink for arbitrary text) or reaches an SVG <title> tooltip's
 * ``.textContent`` — no raw-markup DOM-injection sink is ever used anywhere
 * in this file. See tests/unit/test_dashboard_strategy_template_xss.py.
 *
 * Namespaced as ``window.BXChart`` (parallel to ``window.BXOneclick`` in
 * bx-oneclick.js — this file does not require bx-oneclick.js to be loaded,
 * though the pages that mount it load both).
 */
(function (global) {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";

  function svgEl(tag) {
    return document.createElementNS(SVG_NS, tag);
  }

  function clearNode(node) {
    if (!node) return;
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // ── tf ("3m"/"1h"/"1d" — the top100/trades contract's own timeframe label)
  //    → the lake's canonical candle interval label ("3minute"/"1hour"/"1day",
  //    brutex.data.timeframes.LAKE_INTERVAL_LABELS) — a pure string mapping,
  //    zero I/O. Returns null for an unrecognised tf (caller renders an honest
  //    "unknown timeframe" state rather than guessing).
  function tfToInterval(tf) {
    if (!tf) return null;
    var m = /^([0-9]+)(m|h|d)$/.exec(String(tf).trim());
    if (!m) return null;
    var n = m[1];
    var unit = m[2];
    if (unit === "m") return n + "minute";
    if (unit === "h") return n + "hour";
    return n + "day"; // "1d" -> "1day"
  }

  // ── instrument ("NIFTY"/"BANKNIFTY"/"SENSEX" — the top100 row's own field)
  //    → the candle-lake exchange coordinate. NIFTY/BANKNIFTY-BRUTEX SCOPE
  //    (CLAUDE.md §3) is NSE-listed; SENSEX is BSE-listed — the ONLY exception
  //    (mirrors v29.html's own "SENSEX is BSE-listed — run it separately with
  //    exchange BSE" copy). Every scoped instrument is an INDEX-segment spot
  //    series, so the segment coordinate is always "INDEX" here.
  function exchangeForInstrument(instrument) {
    return String(instrument || "").trim().toUpperCase() === "SENSEX" ? "BSE" : "NSE";
  }

  function segmentForInstrument() {
    return "INDEX";
  }

  // ── IST wall-clock normalisation — matches db_view.query_candles's own
  //    rendering ("the bar timestamp as a clean IST wall-clock (no +05:30
  //    suffix, DATE-ONLY for 1day)"): bars are stored UTC and displayed
  //    UTC+330min. A trade's entry_ts/exit_ts carries its OWN offset (+05:30
  //    in every fixture, but this converts correctly regardless), so parsing
  //    with `new Date()` (which resolves any valid offset to a UTC instant)
  //    then re-applying the identical +330min shift produces the BYTE-SAME
  //    wall-clock string the candle row already carries — independent of the
  //    viewer's own browser timezone (UTC getters only, never local getters).
  function istWallClock(iso) {
    var d = new Date(iso);
    if (!iso || isNaN(d.getTime())) return null;
    var shifted = new Date(d.getTime() + 330 * 60000);
    function pad(n) {
      return n < 10 ? "0" + n : String(n);
    }
    var y = shifted.getUTCFullYear();
    var mo = pad(shifted.getUTCMonth() + 1);
    var da = pad(shifted.getUTCDate());
    var hh = pad(shifted.getUTCHours());
    var mm = pad(shifted.getUTCMinutes());
    var ss = pad(shifted.getUTCSeconds());
    var date = y + "-" + mo + "-" + da;
    return { date: date, datetime: date + " " + hh + ":" + mm + ":" + ss };
  }

  function sessionDateOf(iso) {
    var w = istWallClock(iso);
    return w ? w.date : null;
  }

  // ── locate the candle row a trade timestamp falls on ────────────────────
  // `rows` is the candle response's row-major array (ascending timestamp —
  // callers never pass desc=true), `tsIdx` the index of the "timestamp"
  // column. Tries an EXACT match first (datetime for intraday, date-only for
  // a 1day interval), then snaps to the nearest bar AT OR BEFORE the target
  // (string comparison is lexicographically time-ordered for these
  // zero-padded ISO-like formats) — `exact:false` on the returned hit so the
  // caller can mark the marker with the "~" qualifier. Returns `null` (never
  // a fabricated bar) when no bar in the loaded page covers the timestamp at
  // all — e.g. a genuine data gap before every loaded bar.
  function findBarIndex(rows, tsIdx, targetIso) {
    var target = istWallClock(targetIso);
    if (!target || tsIdx < 0) return null;
    for (var i = 0; i < rows.length; i++) {
      var v = String(rows[i][tsIdx]);
      if (v === target.datetime || v === target.date) return { index: i, exact: true };
    }
    var best = -1;
    for (var j = 0; j < rows.length; j++) {
      if (String(rows[j][tsIdx]) <= target.datetime) best = j;
      else break;
    }
    if (best < 0) return null;
    return { index: best, exact: false };
  }

  // ── OHLC candlestick + entry/exit-arrow chart ────────────────────────────
  // `svg` — an <svg> element already in the DOM (viewBox is set here).
  // `candleBody` — the raw GET /api/v1/db/candles response ({columns, rows}).
  // `trades` — the trade rows (from GET /api/v1/sweep/trades) whose
  //   entry_ts/exit_ts fall within THIS candle page's own session — callers
  //   filter by session date before calling (never all trades globally).
  // `opts.focusTrade` — one trade (usually the same one driving the window)
  //   whose MAE/MFE excursion is drawn as two dashed reference lines.
  // Returns a small stats object (never throws) so the caller can render an
  // honest empty state when `bars === 0`.
  function renderCandleChart(svg, candleBody, trades, opts) {
    opts = opts || {};
    clearNode(svg);
    var columns = (candleBody && candleBody.columns) || [];
    var rows = (candleBody && candleBody.rows) || [];
    var tsIdx = columns.indexOf("timestamp");
    var oIdx = columns.indexOf("open");
    var hIdx = columns.indexOf("high");
    var lIdx = columns.indexOf("low");
    var cIdx = columns.indexOf("close");
    if (!rows.length || tsIdx < 0 || oIdx < 0 || hIdx < 0 || lIdx < 0 || cIdx < 0) {
      return { bars: 0, entriesDrawn: 0, exitsDrawn: 0 };
    }

    var W = 1000;
    var H = 360;
    var marginL = 6;
    var marginR = 6;
    var marginT = 26;
    var marginB = 20;
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("preserveAspectRatio", "none");

    var n = rows.length;
    var plotW = W - marginL - marginR;
    var plotH = H - marginT - marginB;
    var barSlot = plotW / n;
    var bodyW = Math.max(1, Math.min(barSlot * 0.62, 9));

    var hi = -Infinity;
    var lo = Infinity;
    for (var k = 0; k < n; k++) {
      var hh = Number(rows[k][hIdx]);
      var ll = Number(rows[k][lIdx]);
      if (isFinite(hh) && hh > hi) hi = hh;
      if (isFinite(ll) && ll < lo) lo = ll;
    }
    if (!isFinite(hi) || !isFinite(lo)) return { bars: 0, entriesDrawn: 0, exitsDrawn: 0 };
    // Widen the price axis a touch for the entry/exit triangle markers, which
    // sit just outside the candle body (never clipped by the viewBox).
    var span = hi - lo || Math.max(1, hi * 0.01) || 1;
    var padTop = span * 0.14;
    var padBot = span * 0.14;
    var yMax = hi + padTop;
    var yMin = lo - padBot;

    function xOf(i) {
      return marginL + i * barSlot + barSlot / 2;
    }
    function yOf(price) {
      var t = (price - yMin) / (yMax - yMin || 1);
      return marginT + plotH * (1 - t);
    }

    for (var i = 0; i < n; i++) {
      var r = rows[i];
      var o = Number(r[oIdx]);
      var h = Number(r[hIdx]);
      var l = Number(r[lIdx]);
      var c = Number(r[cIdx]);
      var up = c >= o;
      var x = xOf(i);
      var colour = up ? "#26D9B0" : "#FF5C5C";

      var wick = svgEl("line");
      wick.setAttribute("x1", String(x));
      wick.setAttribute("x2", String(x));
      wick.setAttribute("y1", String(yOf(h)));
      wick.setAttribute("y2", String(yOf(l)));
      wick.setAttribute("stroke", colour);
      wick.setAttribute("stroke-width", "1");
      svg.appendChild(wick);

      var bodyTop = yOf(Math.max(o, c));
      var bodyBot = yOf(Math.min(o, c));
      var body = svgEl("rect");
      body.setAttribute("x", String(x - bodyW / 2));
      body.setAttribute("y", String(bodyTop));
      body.setAttribute("width", String(bodyW));
      body.setAttribute("height", String(Math.max(1, bodyBot - bodyTop)));
      body.setAttribute("fill", colour);
      var title = svgEl("title");
      // The ONLY per-bar text sink is this native SVG <title> tooltip's
      // textContent, built from real numbers only — never markup.
      title.textContent =
        String(r[tsIdx]) +
        "  O " + fmtPx(o) + " H " + fmtPx(h) + " L " + fmtPx(l) + " C " + fmtPx(c);
      body.appendChild(title);
      svg.appendChild(body);
    }

    var stats = { bars: n, entriesDrawn: 0, exitsDrawn: 0 };

    var focus = opts.focusTrade;
    if (focus) {
      drawExcursionLine(svg, focus.mae, Number(focus.entry_px), yOf, W, marginL, marginR, "#FF5C5C", "MAE (worst adverse excursion)");
      drawExcursionLine(svg, focus.mfe, Number(focus.entry_px), yOf, W, marginL, marginR, "#26D9B0", "MFE (best favorable excursion)");
    }

    (trades || []).forEach(function (t) {
      var drew = drawTradeMarkers(svg, rows, tsIdx, t, xOf, yOf);
      if (drew.entryDrawn) stats.entriesDrawn++;
      if (drew.exitDrawn) stats.exitsDrawn++;
    });

    return stats;
  }

  function fmtPx(v) {
    return isFinite(v) ? v.toFixed(2) : "—";
  }

  function drawExcursionLine(svg, excursion, entryPx, yOf, W, marginL, marginR, colour, label) {
    if (excursion == null || !isFinite(Number(excursion)) || !isFinite(entryPx)) return;
    var y = yOf(entryPx + Number(excursion));
    var line = svgEl("line");
    line.setAttribute("x1", String(marginL));
    line.setAttribute("x2", String(W - marginR));
    line.setAttribute("y1", String(y));
    line.setAttribute("y2", String(y));
    line.setAttribute("stroke", colour);
    line.setAttribute("stroke-width", "1");
    line.setAttribute("stroke-dasharray", "4,4");
    line.setAttribute("opacity", "0.55");
    var title = svgEl("title");
    title.textContent = label + ": " + Number(excursion).toFixed(2);
    line.appendChild(title);
    svg.appendChild(line);
  }

  // Draws one trade's entry triangle (below the price for LONG, above for
  // SHORT — the direction the position was opened), one exit triangle
  // (coloured by pnl_paisa's sign; amber when pnl_paisa is honestly null),
  // and a thin dashed connector between the two. A marker whose bar was only
  // approximately located (findBarIndex's `exact:false`) gets a white dashed
  // outline — the "~" qualifier from the spec, rendered visually rather than
  // as text (the SVG <title> tooltip also prefixes "~"). Never draws a marker
  // for a timestamp with no covering bar in the loaded page (returns
  // `{drawn:false}` for that side, honestly).
  function drawTradeMarkers(svg, rows, tsIdx, trade, xOf, yOf) {
    var result = { entryDrawn: false, exitDrawn: false };
    var entryPx = Number(trade.entry_px);
    var exitPx = Number(trade.exit_px);
    var size = 6;

    var entryHit = trade.entry_ts ? findBarIndex(rows, tsIdx, trade.entry_ts) : null;
    if (entryHit && isFinite(entryPx)) {
      var ex = xOf(entryHit.index);
      var ey = yOf(entryPx);
      var isLong = String(trade.direction || "").toLowerCase() === "long";
      var tri = svgEl("polygon");
      var pts = isLong
        ? [ex, ey + size, ex - size, ey + size * 2.3, ex + size, ey + size * 2.3]
        : [ex, ey - size, ex - size, ey - size * 2.3, ex + size, ey - size * 2.3];
      tri.setAttribute("points", pts.map(String).join(","));
      tri.setAttribute("fill", isLong ? "#26D9B0" : "#FF5C5C");
      if (!entryHit.exact) {
        tri.setAttribute("stroke", "#FFFFFF");
        tri.setAttribute("stroke-width", "0.75");
        tri.setAttribute("stroke-dasharray", "2,1.5");
      }
      var t1 = svgEl("title");
      t1.textContent =
        (entryHit.exact ? "" : "~") +
        "ENTRY " + String(trade.direction || "—").toUpperCase() +
        " @ " + fmtPx(entryPx) + " · " + String(trade.entry_ts || "—");
      tri.appendChild(t1);
      svg.appendChild(tri);
      result.entryDrawn = true;
      result.entryX = ex;
      result.entryY = ey;
    }

    var exitHit = trade.exit_ts ? findBarIndex(rows, tsIdx, trade.exit_ts) : null;
    if (exitHit && isFinite(exitPx)) {
      var xx = xOf(exitHit.index);
      var yy = yOf(exitPx);
      var pnl = trade.pnl_paisa;
      var colour = pnl == null ? "#FFC857" : Number(pnl) >= 0 ? "#26D9B0" : "#FF5C5C";
      var tri2 = svgEl("polygon");
      var pts2 = [xx, yy - size, xx - size, yy - size * 2.3, xx + size, yy - size * 2.3];
      tri2.setAttribute("points", pts2.map(String).join(","));
      tri2.setAttribute("fill", colour);
      if (!exitHit.exact) {
        tri2.setAttribute("stroke", "#FFFFFF");
        tri2.setAttribute("stroke-width", "0.75");
        tri2.setAttribute("stroke-dasharray", "2,1.5");
      }
      var t2 = svgEl("title");
      t2.textContent =
        (exitHit.exact ? "" : "~") +
        "EXIT (" + String(trade.exit_reason || "—") + ") @ " + fmtPx(exitPx) +
        " · " + String(trade.exit_ts || "—") +
        " · P&L " + (pnl != null ? "₹" + (Number(pnl) / 100).toFixed(2) : "—") +
        " · MAE " + (trade.mae != null ? Number(trade.mae).toFixed(2) : "—") +
        " · MFE " + (trade.mfe != null ? Number(trade.mfe).toFixed(2) : "—") +
        " · VIX in " + (trade.vix_entry != null ? Number(trade.vix_entry).toFixed(2) : "—") +
        " · VIX out " + (trade.vix_exit != null ? Number(trade.vix_exit).toFixed(2) : "—");
      tri2.appendChild(t2);
      svg.appendChild(tri2);
      result.exitDrawn = true;
      result.exitX = xx;
      result.exitY = yy;
    }

    if (result.entryDrawn && result.exitDrawn) {
      var line = svgEl("line");
      line.setAttribute("x1", String(result.entryX));
      line.setAttribute("y1", String(result.entryY));
      line.setAttribute("x2", String(result.exitX));
      line.setAttribute("y2", String(result.exitY));
      line.setAttribute("stroke", "#5B8DEF");
      line.setAttribute("stroke-width", "1");
      line.setAttribute("stroke-dasharray", "3,3");
      line.setAttribute("opacity", "0.7");
      svg.appendChild(line);
    }
    return result;
  }

  // ── equity curve — a REAL cumulative pnl_paisa series over the trades
  //    array the caller passes (its OWN "current page", per the spec —
  //    O(trades-on-page), never a hidden full-history fetch). Honestly breaks
  //    the polyline into separate segments around any trade whose pnl_paisa
  //    is null (gap-register #18 NULL-by-design) rather than pretending it
  //    was zero. Returns `{points:0}` (never throws / never draws a fake
  //    flat line) when every row is null or the array is empty — the caller
  //    renders the honest "equity curve not yet available" state for that.
  function renderEquityCurve(svg, trades) {
    clearNode(svg);
    var W = 1000;
    var H = 220;
    var marginL = 6;
    var marginR = 6;
    var marginT = 14;
    var marginB = 14;
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("preserveAspectRatio", "none");
    var list = trades || [];
    if (!list.length) return { points: 0, hadNull: false, final: null };

    var cum = 0;
    var series = [];
    var hadNull = false;
    for (var i = 0; i < list.length; i++) {
      var pnl = list[i].pnl_paisa;
      if (pnl == null) {
        hadNull = true;
        series.push(null);
        continue;
      }
      cum += Number(pnl);
      series.push(cum);
    }
    var vals = series.filter(function (v) {
      return v != null;
    });
    if (!vals.length) return { points: 0, hadNull: hadNull, final: null };

    var vMin = Math.min(0, Math.min.apply(null, vals));
    var vMax = Math.max(0, Math.max.apply(null, vals));
    var padV = (vMax - vMin) * 0.1 || 100;
    vMin -= padV;
    vMax += padV;
    var n = series.length;

    function xOf(i) {
      return n <= 1 ? marginL : marginL + (i / (n - 1)) * (W - marginL - marginR);
    }
    function yOf(v) {
      var t = (v - vMin) / (vMax - vMin || 1);
      return marginT + (1 - t) * (H - marginT - marginB);
    }

    var zero = svgEl("line");
    zero.setAttribute("x1", String(marginL));
    zero.setAttribute("x2", String(W - marginR));
    zero.setAttribute("y1", String(yOf(0)));
    zero.setAttribute("y2", String(yOf(0)));
    zero.setAttribute("stroke", "#3A4152");
    zero.setAttribute("stroke-width", "1");
    svg.appendChild(zero);

    var pts = [];
    function flush() {
      if (pts.length > 1) {
        var poly = svgEl("polyline");
        poly.setAttribute("points", pts.join(" "));
        poly.setAttribute("fill", "none");
        poly.setAttribute("stroke", "#FFC857");
        poly.setAttribute("stroke-width", "2.2");
        poly.setAttribute("stroke-linecap", "round");
        poly.setAttribute("stroke-linejoin", "round");
        svg.appendChild(poly);
      } else if (pts.length === 1) {
        var dot = svgEl("circle");
        var xy = pts[0].split(",");
        dot.setAttribute("cx", xy[0]);
        dot.setAttribute("cy", xy[1]);
        dot.setAttribute("r", "3");
        dot.setAttribute("fill", "#FFC857");
        svg.appendChild(dot);
      }
      pts = [];
    }
    for (var i2 = 0; i2 < n; i2++) {
      var v = series[i2];
      if (v == null) {
        flush();
        continue;
      }
      pts.push(xOf(i2) + "," + yOf(v));
    }
    flush();

    return { points: vals.length, hadNull: hadNull, final: cum };
  }

  global.BXChart = {
    SVG_NS: SVG_NS,
    svgEl: svgEl,
    clearNode: clearNode,
    tfToInterval: tfToInterval,
    exchangeForInstrument: exchangeForInstrument,
    segmentForInstrument: segmentForInstrument,
    istWallClock: istWallClock,
    sessionDateOf: sessionDateOf,
    findBarIndex: findBarIndex,
    renderCandleChart: renderCandleChart,
    renderEquityCurve: renderEquityCurve,
  };
})(window);
