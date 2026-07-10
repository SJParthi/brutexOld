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
 * SOURCE-STRING tests (the tests/browser Playwright job — #1557 —
 * does not drive /strategy).
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
  var nullCosts = 0;
  for (var i = 0; i < trades.length; i++) {
    var pnl = trades[i].pnl_paisa;
    var costs = trades[i].costs_paisa;
    /* Ruling-18 NULL-BY-DESIGN costs ([W2-4] DEC-ONECLICK-TOP100-OBJECT-
       ROOT-001): every production trade row carries costs_paisa:null until
       P5's futures translation — folding null into the non-integer bucket
       EXCLUDED every real trade (empty equity, "0 trades"). A null cost is
       a KNOWN-unverified cost, not a corrupt number: it contributes ₹0 and
       is COUNTED (null_cost_rows) so the renderer labels the curve
       "gross of null costs" — never silently, never fabricated. */
    var nullCost = costs === null || costs === undefined;
    if (nullCost) {
      costs = 0;
    }
    if (!Number.isSafeInteger(pnl) || !Number.isSafeInteger(costs)) {
      invalid += 1; // counted, never silently coerced (R27 no invented numbers)
      continue;
    }
    if (nullCost) {
      nullCosts += 1;
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
    invalid_rows: invalid,
    null_cost_rows: nullCosts
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

/**
 * TZ-SAFE timestamp parse for the BruteX display APIs ([W3-R1] second layer,
 * DEC-ONECLICK-TOP100-OBJECT-ROOT-001 wave-3 fold).
 *
 * The two payload families this page mixes carry DIFFERENT forms:
 *   - /api/v1/db/candles bar timestamps are IST WALL-CLOCK NAIVE strings
 *     ("YYYY-MM-DD HH:MM:SS", or "YYYY-MM-DD" for a 1day bar —
 *     db_view._format_candle_timestamp, the CLAUDE.md §6 display-IST law);
 *   - /api/v1/oneclick/trades entry_ts/exit_ts are ISO-8601 UTC strings WITH
 *     an explicit offset (trade_store._us_to_iso → "…+00:00").
 * Naive Date.parse treats an offset-less string as BROWSER-LOCAL time, so in
 * a non-IST browser the candle epochs shifted by (local − IST) while the
 * trade epochs did not — misplaced/dropped arrows. This parser pins a naive
 * string to the fixed +05:30 IST offset (NSE has no DST) and passes
 * offset/Z-carrying strings through untouched, so every epoch is
 * TZ-INVARIANT (node-golden-proven under TZ=America/New_York).
 *
 * @param {*} raw - a number (epoch ms, passed through), or a timestamp string.
 * @returns {number|null} epoch milliseconds, or null (unparseable — the
 *   caller skips the row / shows the honest note, never a NaN coordinate).
 */
function bxParseTsIst(raw) {
  if (typeof raw === "number") {
    return isFinite(raw) ? raw : null;
  }
  if (typeof raw !== "string" || raw === "") {
    return null;
  }
  var s = raw.replace(" ", "T");
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
    s += "T00:00:00"; // a date-only 1day bar reads midnight IST
  }
  if (!/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(s)) {
    s += "+05:30"; // naive = IST wall-clock (never browser-local)
  }
  var t = Date.parse(s);
  return isNaN(t) ? null : t;
}

/**
 * The IST calendar date ("YYYY-MM-DD") of an epoch-ms instant (pure epoch
 * arithmetic — never a browser-local Date roundtrip, so the session-day
 * anchor for the candle fetch is TZ-invariant).
 *
 * @param {number} ms - epoch milliseconds.
 * @returns {string|null} the IST date, or null for a non-finite input.
 */
function bxIstDateOfEpoch(ms) {
  if (typeof ms !== "number" || !isFinite(ms)) {
    return null;
  }
  return new Date(ms + 330 * 60000).toISOString().slice(0, 10);
}

/** The /api/v1/db/candles columns the chart needs (db_view.CANDLE_COLUMNS). */
var BX_DB_CANDLE_REQUIRED_COLUMNS = ["timestamp", "open", "high", "low", "close"];

/**
 * Map ONE REAL /api/v1/db/candles payload ({columns, rows}) to the chart's
 * candle rows ([{ts, open, high, low, close}], ascending ts) — the route-
 * shape-driven mapper ([W3-R1] DEC-ONECLICK-TOP100-OBJECT-ROOT-001 wave-3
 * fold: the page previously indexed ``idx.ts`` while the route's column is
 * named "timestamp", so NO candle ever plotted on a real payload — the
 * fixture-blindspot class again; this mapper is node-golden-tested against
 * the REAL route payload shape).
 *
 * @param {object} payload - the /api/v1/db/candles response body.
 * @returns {object} {rows: [...], missing: string|null, skipped: number} —
 *   ``missing`` names the first absent required column (the caller shows an
 *   honest note; an empty payload maps to {rows: [], missing: null,
 *   skipped: 0}); ``skipped`` COUNTS the unreadable rows (unparseable
 *   timestamp OR corrupt OHLC) so the caller can say "N row(s) skipped
 *   (unreadable)" instead of silently plotting fewer candles.
 */
function bxCandleRowsFromDbPayload(payload) {
  var cols = (payload && payload.columns) || [];
  var idx = {};
  for (var c = 0; c < cols.length; c++) {
    idx[cols[c]] = c;
  }
  for (var q = 0; q < BX_DB_CANDLE_REQUIRED_COLUMNS.length; q++) {
    if (idx[BX_DB_CANDLE_REQUIRED_COLUMNS[q]] === undefined) {
      return { rows: [], missing: BX_DB_CANDLE_REQUIRED_COLUMNS[q], skipped: 0 };
    }
  }
  var out = [];
  var skipped = 0;
  var raw = (payload && payload.rows) || [];
  for (var r = 0; r < raw.length; r++) {
    var ts = bxParseTsIst(raw[r][idx.timestamp]);
    if (ts === null) {
      skipped += 1; // unparseable row skipped — never a NaN coordinate
      continue;
    }
    var open = bxBarPrice(raw[r][idx.open]);
    var high = bxBarPrice(raw[r][idx.high]);
    var low = bxBarPrice(raw[r][idx.low]);
    var close = bxBarPrice(raw[r][idx.close]);
    if (open === null || high === null || low === null || close === null) {
      skipped += 1; // corrupt OHLC skipped — Number(null) coerces to 0, never plot it
      continue;
    }
    out.push({ ts: ts, open: open, high: high, low: low, close: close });
  }
  out.sort(function (a, b) {
    return a.ts - b.ts;
  });
  return { rows: out, missing: null, skipped: skipped };
}

/**
 * One OHLC cell as a FINITE number, or null for a corrupt value (pure).
 *
 * ``Number(null)`` coerces to 0 and ``Number(undefined)`` to NaN — either
 * would plot a fabricated 0-price / NaN candle; a null/blank/non-finite
 * value therefore SKIPS the row (the caller counts it). A numeric STRING is
 * accepted (the pre-fix ``Number()`` behaviour for legitimate values).
 *
 * @param {*} value - one raw OHLC cell.
 * @returns {number|null} the finite price or null.
 */
function bxBarPrice(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") {
    return null;
  }
  var n = Number(value);
  return isFinite(n) ? n : null;
}

/**
 * The honest skipped-rows note for the chart coords line (pure; "" when
 * nothing was skipped — ONE constant wording so the pin cannot drift).
 *
 * @param {number} n - the mapper's skipped count.
 * @returns {string} "N row(s) skipped (unreadable)" or "".
 */
function bxSkippedRowsNote(n) {
  if (!(typeof n === "number") || !isFinite(n) || n <= 0) {
    return "";
  }
  return String(n) + " row(s) skipped (unreadable)";
}

/**
 * Build the chartable series from STORED minute-path equity points
 * (DEC-ONECLICK-WINNER-DETAIL-TRUTH-001 item 4C — the /api/v1/oneclick/equity
 * rows ``{session_date, equity_close_paisa, …}``; pure). Output is the SAME
 * ``{empty, points[], final_paisa, invalid_rows}`` shape ``bxEquitySeries``
 * yields, so ``bxEquityPathNode`` renders either source. A non-safe-integer
 * / non-finite ``equity_close_paisa`` point is SKIPPED and COUNTED — never a
 * fabricated coordinate.
 *
 * @param {*} rows - the equity route's rows array.
 * @returns {object} {empty, points: [{cum_paisa}], final_paisa, invalid_rows}.
 */
function bxMinutePathSeries(rows) {
  var points = [];
  var invalid = 0;
  var list = Array.isArray(rows) ? rows : [];
  for (var i = 0; i < list.length; i++) {
    var v = list[i] && list[i].equity_close_paisa;
    if (typeof v === "number" && isFinite(v) && Math.floor(v) === v &&
        Math.abs(v) <= 9007199254740991) {
      points.push({ cum_paisa: v });
    } else {
      invalid += 1;
    }
  }
  return {
    empty: points.length === 0,
    points: points,
    final_paisa: points.length ? points[points.length - 1].cum_paisa : 0,
    invalid_rows: invalid
  };
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
  DB_CANDLE_REQUIRED_COLUMNS: BX_DB_CANDLE_REQUIRED_COLUMNS,
  parseTsIst: bxParseTsIst,
  istDateOfEpoch: bxIstDateOfEpoch,
  candleRowsFromDbPayload: bxCandleRowsFromDbPayload,
  barPrice: bxBarPrice,
  skippedRowsNote: bxSkippedRowsNote,
  scaleLinear: bxScaleLinear,
  candleGeom: bxCandleGeom,
  equitySeries: bxEquitySeries,
  minutePathSeries: bxMinutePathSeries,
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
