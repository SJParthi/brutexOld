/* bx-chart.js — pure chart math + thin createElementNS SVG builders
 * (DEC-OPERATOR-PAGE-REAL-001, CLAUDE.md §11 COMMON).
 *
 * The strategy page's candle chart / entry-exit arrows / equity curve /
 * funnel widths are all computed by the PURE functions below (no DOM, no
 * I/O — node-golden-tested against Python oracles), then rendered by the
 * guarded createElementNS builders at the bottom (browser-only; ZERO
 * string-to-DOM sinks — every node is built via createElementNS +
 * setAttribute with numeric-generated strings only).
 *
 * Degenerate-domain CONTRACTS (F-B3 — these define the module):
 *   - bxScaleLinear: a collapsed domain (d0 === d1) maps EVERY input to the
 *     range MIDPOINT and the scale carries `.flat === true` (the renderer
 *     turns it into a visible "flat range" note; NaN coordinates are
 *     structurally impossible on this path).
 *   - bxCandleGeom: 0 rows => {candles: [], empty: true}; 1 row => one
 *     centered candle; a flat price range rides the flat scale midpoint.
 *   - bxEquitySeries: cumsum in INTEGER paisa (safe-integer summation with
 *     an overflow guard; rupee conversion happens ONCE at display via
 *     bx-explain.js bxPaisaToRupees — F-n2); [] => {points: [], empty: true}
 *     (the renderer shows an honest "no trades" state, never a degenerate
 *     polyline).
 *   - bxFunnelPct: total 0 => all-zero widths + {awaiting: true} (no ÷0).
 *   - bxNearestBarIdx: returns -1 when ts is NOT bracketed by
 *     [rows[0].ts, rows[last].ts] (F-B1); the overlay honors the sentinel —
 *     arrow DROPPED + a loud "trade outside chart window — widen range"
 *     note, NEVER a snapped wrong-candle arrow.
 *   - bxPathD: fixed 2dp coordinates to bound path-string size (F-n1;
 *     ECMA-262 Number->String is locale-independent — always ".").
 *
 * Honest envelope (Golden Rule #10): the pure functions are executed for
 * real under node; the SVG builders are browser-only and are pinned by
 * SOURCE-STRING tests (no headless JS runner in CI).
 */
"use strict";

/**
 * Linear scale factory with the flat-domain contract (F-B3).
 *
 * @param {number[]} domain - [d0, d1] input domain.
 * @param {number[]} range - [r0, r1] output range.
 * @returns {Function} scale(v) -> number; carries .flat / .domain / .range.
 */
function bxScaleLinear(domain, range) {
  var d0 = domain[0];
  var d1 = domain[1];
  var r0 = range[0];
  var r1 = range[1];
  var flat = d0 === d1;
  var mid = (r0 + r1) / 2;
  var scale = function (v) {
    if (flat) {
      return mid; // every input maps to the range MIDPOINT (flat contract)
    }
    return r0 + ((v - d0) / (d1 - d0)) * (r1 - r0);
  };
  scale.flat = flat;
  scale.domain = [d0, d1];
  scale.range = [r0, r1];
  return scale;
}

/**
 * Candle geometry for an SVG chart (pure data out — no DOM).
 *
 * Rows are {ts, open, high, low, close} with numeric fields, ascending ts.
 * 0 rows => {candles: [], empty: true}. 1 row => a single centered candle.
 * A flat price range (min low === max high) sets flat: true and every y
 * lands on the vertical midpoint (the flat-scale contract).
 *
 * @param {object[]} rows - the candle rows.
 * @param {number} w - drawing width (px).
 * @param {number} h - drawing height (px).
 * @param {number} pad - inner padding (px).
 * @returns {object} {candles: [{x, w, yOpen, yClose, yHigh, yLow, up}],
 *                    empty, flat, xSlot, priceLo, priceHi}.
 */
function bxCandleGeom(rows, w, h, pad) {
  if (!rows || rows.length === 0) {
    return { candles: [], empty: true, flat: false, xSlot: 0, priceLo: null, priceHi: null };
  }
  var lo = Infinity;
  var hi = -Infinity;
  var i;
  for (i = 0; i < rows.length; i++) {
    if (rows[i].low < lo) {
      lo = rows[i].low;
    }
    if (rows[i].high > hi) {
      hi = rows[i].high;
    }
  }
  var yScale = bxScaleLinear([lo, hi], [h - pad, pad]); // inverted: bigger price = higher
  var xSlot = (w - 2 * pad) / rows.length;
  var bodyW = Math.max(1, xSlot * 0.6);
  var candles = [];
  for (i = 0; i < rows.length; i++) {
    var r = rows[i];
    candles.push({
      x: pad + xSlot * (i + 0.5), // a single row lands centered by construction
      w: bodyW,
      yOpen: yScale(r.open),
      yClose: yScale(r.close),
      yHigh: yScale(r.high),
      yLow: yScale(r.low),
      up: r.close >= r.open
    });
  }
  return {
    candles: candles,
    empty: false,
    flat: yScale.flat,
    xSlot: xSlot,
    priceLo: lo,
    priceHi: hi
  };
}

/**
 * Equity curve series: cumulative sum of (pnl_paisa - costs_paisa) in
 * INTEGER paisa (F-n2 — never float accumulation; ONE final rupee
 * conversion happens at display via bx-explain.js bxPaisaToRupees).
 *
 * A row whose pnl_paisa/costs_paisa is not a safe integer is SKIPPED and
 * counted in invalid_rows (the renderer shows a loud note — never a
 * silently wrong curve). If a partial sum would leave the safe-integer
 * range, accumulation STOPS and overflow: true is set (honest truncation,
 * never a wrong number).
 *
 * @param {object[]} trades - contract trade rows ({pnl_paisa, costs_paisa}).
 * @returns {object} {points: [{i, cum_paisa}], final_paisa, empty,
 *                    overflow, invalid_rows}.
 */
function bxEquitySeries(trades) {
  if (!trades || trades.length === 0) {
    return { points: [], final_paisa: 0, empty: true, overflow: false, invalid_rows: 0 };
  }
  var points = [];
  var cum = 0;
  var invalid = 0;
  var overflow = false;
  for (var i = 0; i < trades.length; i++) {
    var pnl = trades[i].pnl_paisa;
    var costs = trades[i].costs_paisa;
    if (!Number.isSafeInteger(pnl) || !Number.isSafeInteger(costs)) {
      invalid += 1; // counted, never silently coerced (R27 no invented numbers)
      continue;
    }
    var next = cum + (pnl - costs);
    if (!Number.isSafeInteger(next)) {
      overflow = true; // stop honestly rather than accumulate a wrong float
      break;
    }
    cum = next;
    points.push({ i: points.length, cum_paisa: cum });
  }
  return {
    points: points,
    final_paisa: cum,
    empty: false,
    overflow: overflow,
    invalid_rows: invalid
  };
}

/**
 * Funnel stage widths as percentages of the FIRST stage (pure).
 *
 * Zero/absent total => all-zero widths + awaiting: true (no division by
 * zero, no fabricated bar). Widths are raw full-precision ratios — the
 * renderer formats/clamps for CSS while showing the real count beside it.
 *
 * @param {number[]} counts - ordered stage counts (counts[0] = the total).
 * @returns {object} {widths: number[], awaiting: boolean}.
 */
function bxFunnelPct(counts) {
  if (!counts || counts.length === 0 || !(counts[0] > 0)) {
    var zeros = [];
    for (var z = 0; z < (counts ? counts.length : 0); z++) {
      zeros.push(0);
    }
    return { widths: zeros, awaiting: true };
  }
  var widths = [];
  for (var i = 0; i < counts.length; i++) {
    widths.push((counts[i] / counts[0]) * 100);
  }
  return { widths: widths, awaiting: false };
}

/**
 * Index of the bar NEAREST to ts, or the -1 sentinel when ts is not
 * bracketed by [rows[0].ts, rows[last].ts] (F-B1 — the overlay must DROP
 * the arrow + show a loud note, never snap to a wrong candle).
 *
 * Rows are ascending by numeric .ts. An equidistant tie resolves to the
 * EARLIER index (deterministic).
 *
 * @param {number} ts - the trade timestamp (same unit as rows[].ts).
 * @param {object[]} rows - the candle rows (ascending ts).
 * @returns {number} the nearest index, or -1 (out of window / empty rows).
 */
function bxNearestBarIdx(ts, rows) {
  if (!rows || rows.length === 0) {
    return -1;
  }
  if (!(typeof ts === "number") || !isFinite(ts)) {
    return -1;
  }
  if (ts < rows[0].ts || ts > rows[rows.length - 1].ts) {
    return -1; // NOT bracketed — the sentinel (never snapped)
  }
  var lo = 0;
  var hi = rows.length - 1;
  while (hi - lo > 1) {
    var mid = (lo + hi) >> 1;
    if (rows[mid].ts <= ts) {
      lo = mid;
    } else {
      hi = mid;
    }
  }
  var dLo = ts - rows[lo].ts;
  var dHi = rows[hi].ts - ts;
  return dLo <= dHi ? lo : hi; // tie -> earlier index
}

/** The loud out-of-window note (ONE constant so UI + tests cannot drift). */
var BX_ARROW_DROPPED_NOTE = "trade outside chart window — widen range";

/**
 * Entry/exit arrow placement for ONE trade against the loaded candle rows
 * (pure). Either endpoint outside the window => dropped: true + the loud
 * note (F-B1b) — the renderer must NOT draw a snapped arrow.
 *
 * @param {number} entryTs - trade entry ts (same unit as rows[].ts).
 * @param {number} exitTs - trade exit ts.
 * @param {object[]} rows - the candle rows (ascending ts).
 * @returns {object} {entry_idx, exit_idx, dropped, note}.
 */
function bxTradeArrow(entryTs, exitTs, rows) {
  var entryIdx = bxNearestBarIdx(entryTs, rows);
  var exitIdx = bxNearestBarIdx(exitTs, rows);
  var dropped = entryIdx === -1 || exitIdx === -1;
  return {
    entry_idx: entryIdx,
    exit_idx: exitIdx,
    dropped: dropped,
    note: dropped ? BX_ARROW_DROPPED_NOTE : ""
  };
}

/**
 * SVG path "d" string from points, every coordinate fixed to 2dp (F-n1 —
 * bounds the path-string size; toFixed emits a locale-independent ".").
 *
 * @param {object[]} points - [{x, y}] in drawing coordinates.
 * @returns {string} the path data ("" for no points).
 */
function bxPathD(points) {
  if (!points || points.length === 0) {
    return "";
  }
  var parts = ["M" + points[0].x.toFixed(2) + " " + points[0].y.toFixed(2)];
  for (var i = 1; i < points.length; i++) {
    parts.push("L" + points[i].x.toFixed(2) + " " + points[i].y.toFixed(2));
  }
  return parts.join(" ");
}

/* ── Thin SVG builders (browser-only; createElementNS + setAttribute ONLY —
      zero string-to-DOM sinks; every attribute value is numeric-generated
      by the pure functions above, never server text). ── */

var BX_SVG_NS = "http://www.w3.org/2000/svg";

/**
 * Create one SVG element with plain attributes (createElementNS only).
 *
 * @param {string} tag - the SVG tag name.
 * @param {object} [attrs] - attribute map (values stringified).
 * @returns {Element} the namespaced SVG element.
 */
function bxSvgEl(tag, attrs) {
  var n = document.createElementNS(BX_SVG_NS, tag);
  if (attrs) {
    for (var k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) {
        n.setAttribute(k, String(attrs[k]));
      }
    }
  }
  return n;
}

/**
 * Build the candle nodes (wick <line> + body <rect>) for a bxCandleGeom
 * result. Colors ride CSS classes (bx-candle-up / bx-candle-down) so the
 * page theme owns them.
 *
 * @param {object} geom - a bxCandleGeom() result.
 * @returns {Element[]} the SVG nodes in draw order.
 */
function bxCandleNodes(geom) {
  var out = [];
  for (var i = 0; i < geom.candles.length; i++) {
    var c = geom.candles[i];
    var cls = c.up ? "bx-candle-up" : "bx-candle-down";
    var wick = bxSvgEl("line", {
      x1: c.x.toFixed(2),
      x2: c.x.toFixed(2),
      y1: c.yHigh.toFixed(2),
      y2: c.yLow.toFixed(2),
      "class": "bx-candle-wick " + cls
    });
    var top = Math.min(c.yOpen, c.yClose);
    var hgt = Math.max(1, Math.abs(c.yClose - c.yOpen));
    var body = bxSvgEl("rect", {
      x: (c.x - c.w / 2).toFixed(2),
      y: top.toFixed(2),
      width: c.w.toFixed(2),
      height: hgt.toFixed(2),
      "class": "bx-candle-body " + cls
    });
    out.push(wick);
    out.push(body);
  }
  return out;
}

/**
 * Build one entry/exit arrow node at a candle position. Callers MUST honor
 * the bxTradeArrow dropped sentinel and never call this for a dropped
 * arrow (the pure contract keeps the wrong-candle snap impossible).
 *
 * @param {number} x - arrow x (px).
 * @param {number} y - arrow tip y (px).
 * @param {string} kind - "entry" | "exit".
 * @param {string} direction - "long" | "short" (orients the triangle).
 * @returns {Element} the <path> arrow node.
 */
function bxArrowNode(x, y, kind, direction) {
  var up = (kind === "entry") === (direction === "long");
  var s = 6; // half-width of the triangle, px
  var base = up ? y + 2 * s : y - 2 * s; // tip sits AT y; base flips with orientation
  var d =
    "M" + x.toFixed(2) + " " + y.toFixed(2) +
    " L" + (x - s).toFixed(2) + " " + base.toFixed(2) +
    " L" + (x + s).toFixed(2) + " " + base.toFixed(2) + " Z";
  return bxSvgEl("path", { d: d, "class": "bx-arrow bx-arrow-" + kind + " bx-arrow-" + direction });
}

/**
 * Build the equity polyline <path> for a bxEquitySeries result scaled into
 * a w×h box. Empty series => null (render the honest empty state instead).
 *
 * @param {object} series - a bxEquitySeries() result.
 * @param {number} w - drawing width (px).
 * @param {number} h - drawing height (px).
 * @param {number} pad - inner padding (px).
 * @returns {Element|null} the <path> node or null when there is nothing.
 */
function bxEquityPathNode(series, w, h, pad) {
  if (series.empty || series.points.length === 0) {
    return null;
  }
  var lo = 0;
  var hi = 0;
  var i;
  for (i = 0; i < series.points.length; i++) {
    var v = series.points[i].cum_paisa;
    if (v < lo) {
      lo = v;
    }
    if (v > hi) {
      hi = v;
    }
  }
  var x = bxScaleLinear([0, Math.max(1, series.points.length - 1)], [pad, w - pad]);
  var y = bxScaleLinear([lo, hi], [h - pad, pad]);
  var pts = [];
  for (i = 0; i < series.points.length; i++) {
    pts.push({ x: x(i), y: y(series.points[i].cum_paisa) });
  }
  return bxSvgEl("path", { d: bxPathD(pts), "class": "bx-equity-line", fill: "none" });
}

var BXChart = {
  SVG_NS: BX_SVG_NS,
  ARROW_DROPPED_NOTE: BX_ARROW_DROPPED_NOTE,
  scaleLinear: bxScaleLinear,
  candleGeom: bxCandleGeom,
  equitySeries: bxEquitySeries,
  funnelPct: bxFunnelPct,
  nearestBarIdx: bxNearestBarIdx,
  tradeArrow: bxTradeArrow,
  pathD: bxPathD,
  svgEl: bxSvgEl,
  candleNodes: bxCandleNodes,
  arrowNode: bxArrowNode,
  equityPathNode: bxEquityPathNode
};

/* Browsers get the global; a non-browser harness (the node golden) gets the
   CommonJS export (mirrors bx-format.js / bx-movers-time.js). */
if (typeof window !== "undefined") {
  window.BXChart = BXChart;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = BXChart;
}
