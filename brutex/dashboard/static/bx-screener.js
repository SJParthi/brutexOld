/* bx-screener.js — pure comparators/filters for the top-100 screener rows
 * (DEC-OPERATOR-PAGE-REAL-001, CLAUDE.md §11 COMMON).
 *
 * The /screener page (R36 STRATEGY SCREENER: filter/sort by win% · DD · PF ·
 * VIX band · TF · direction) sorts and filters the top100.json contract rows
 * ENTIRELY client-side with the pure primitives below — no DOM, no I/O, so a
 * node-executed golden pins them for real (bx-format.js precedent).
 *
 * Determinism laws:
 *   - bxCompare ends every tie with the RANK-ASCENDING tie-break (F-m2,
 *     golden-pinned) => a TOTAL order, so any engine's sort is stable-by-
 *     construction for these comparators.
 *   - Values are compared with RELATIONAL operators (never subtraction), so
 *     full-precision inputs (BigInt / lexicographic strings) keep their
 *     total order — Number-parsed JSON collapses 2**60 and 2**60+1 to the
 *     SAME double (the /results exact-int precedent), which is exactly why
 *     the comparator never converts through Number.
 *   - null/undefined metric values sort LAST regardless of direction
 *     (deterministic, then rank tie-break).
 *   - A vix_band: null row under an ACTIVE band filter is EXCLUDED-but-
 *     COUNTED (F-m3, R31 honest NULL): bxApplyFilters reports
 *     null_vix_excluded and bxNullVixNote renders the count line suffix.
 *
 * Honest envelope (Golden Rule #10): pure primitives node-golden-tested;
 * the page wiring (COLS render, th-click sort) is pinned by SOURCE-STRING
 * tests only (the tests/browser Playwright job — #1557 — does not
 * drive /screener).
 *
 * A-r4 defense-in-depth (DEC-ONECLICK-PERF-CLUB-001): every work surface is
 * BOUNDED independently of payload trust — bxApplyFilters/bxSortRows process
 * at most BX_SCREENER_INPUT_CAP rows, and the render plan (bxRenderCount /
 * bxCapNote) caps even the "Show all" view at BX_SCREENER_SHOW_ALL_CAP with
 * an honest "showing first N of M (render cap)" note. The server already
 * bounds the wire at TOP100_ROW_CAP=100 (#1373), so these caps are
 * unreachable through /api/v1/oneclick/top100 today; they exist so a FUTURE
 * path bypassing load_top100 can never resurrect the measured 10k=4.8 s /
 * 20k=14.1 s tab freeze. Truncation is of an out-of-contract payload and is
 * always NAMED on the count line, never silent.
 */
"use strict";

/** Rows rendered in the default (non-Show-all) view. */
var BX_SCREENER_RENDER_CAP = 200;
/** Rows rendered at most even under "Show all" (A-r4 — a bounded ceiling). */
var BX_SCREENER_SHOW_ALL_CAP = 1000;
/** Rows bxApplyFilters/bxSortRows will process at most (input bound). */
var BX_SCREENER_INPUT_CAP = 5000;

/**
 * Bound an untrusted rows array to BX_SCREENER_INPUT_CAP (pure).
 *
 * @param {*} rows - the payload rows (any shape — a non-array reads empty).
 * @returns {{rows: object[], dropped: number}} the first-N slice + the
 *          honest dropped count (0 when nothing was cut).
 */
function bxInputCapRows(rows) {
  if (!Array.isArray(rows)) {
    return { rows: [], dropped: 0 };
  }
  if (rows.length <= BX_SCREENER_INPUT_CAP) {
    return { rows: rows, dropped: 0 };
  }
  return {
    rows: rows.slice(0, BX_SCREENER_INPUT_CAP),
    dropped: rows.length - BX_SCREENER_INPUT_CAP
  };
}

/**
 * How many rows the table renders for this view (pure — the ONE cap law).
 *
 * @param {number} sortedLength - rows surviving filter+sort.
 * @param {boolean} showAll - the Show-all toggle.
 * @returns {number} min(sortedLength, the view's cap).
 */
function bxRenderCount(sortedLength, showAll) {
  var cap = showAll ? BX_SCREENER_SHOW_ALL_CAP : BX_SCREENER_RENDER_CAP;
  return Math.min(sortedLength, cap);
}

/**
 * The count-line suffix naming a render-cap truncation ("" when none).
 *
 * @param {number} shown - rows actually rendered.
 * @param {number} sortedLength - rows surviving filter+sort.
 * @param {boolean} showAll - the Show-all toggle.
 * @returns {string} the honest note or "".
 */
function bxCapNote(shown, sortedLength, showAll) {
  if (shown >= sortedLength) {
    return "";
  }
  if (!showAll) {
    return " (render cap " + BX_SCREENER_RENDER_CAP + " — use Show all)";
  }
  return (
    " (showing first " + shown + " of " + sortedLength + " (render cap " +
    BX_SCREENER_SHOW_ALL_CAP + "))"
  );
}

/**
 * The count-line suffix naming an INPUT-cap truncation ("" when none) — an
 * out-of-contract payload (> BX_SCREENER_INPUT_CAP rows) is never silently
 * shrunk.
 *
 * @param {number} dropped - rows cut by bxInputCapRows.
 * @param {number} payloadLength - the payload's original row count.
 * @returns {string} the honest note or "".
 */
function bxInputCapNote(dropped, payloadLength) {
  if (!(typeof dropped === "number") || dropped <= 0) {
    return "";
  }
  return (
    " (input capped: first " + BX_SCREENER_INPUT_CAP + " of " +
    payloadLength + " payload rows loaded)"
  );
}

/**
 * The canonical screener column config (ONE source of truth — screener.html
 * consumes this; test_dashboard_explain_lines.py asserts every explain key
 * has a BX_EXPLAIN entry). type: "num" | "str" | "vix". money: true means
 * the CELL renders paisa via bxPaisaToRupees (display-only 2dp — metrics
 * themselves are NEVER re-rounded).
 *
 * @type {Array<{key: string, label: string, type: string, explain: string,
 *               money: (boolean|undefined)}>}
 */
var BX_SCREENER_COLUMNS = [
  { key: "rank", label: "#", type: "num", explain: "rank" },
  { key: "conditions", label: "Strategy", type: "str", explain: "conditions" },
  { key: "instrument", label: "Inst", type: "str", explain: "instrument" },
  { key: "tf", label: "TF", type: "str", explain: "tf" },
  { key: "direction", label: "Dir", type: "str", explain: "direction" },
  { key: "arm", label: "SL/TP arm", type: "str", explain: "arm" },
  { key: "metrics.net_pnl_paisa", label: "Net P&L", type: "num", money: true, explain: "net_pnl_paisa" },
  { key: "metrics.win_rate", label: "Win %", type: "num", explain: "win_rate" },
  { key: "metrics.profit_factor", label: "PF", type: "num", explain: "profit_factor" },
  { key: "metrics.max_dd_paisa", label: "Max DD", type: "num", money: true, explain: "max_dd_paisa" },
  { key: "metrics.sharpe", label: "Sharpe", type: "num", explain: "sharpe" },
  { key: "metrics.dsr", label: "DSR", type: "num", explain: "dsr" },
  { key: "metrics.cpcv_pass_pct", label: "CPCV %", type: "num", explain: "cpcv_pass_pct" },
  { key: "metrics.trades", label: "Trades", type: "num", explain: "trades" },
  { key: "vix_band", label: "VIX band", type: "vix", explain: "vix_band" },
  /* DEC-RESULTS-CASCADE-VERDICT-JOIN-001: the Stage-4 cascade verdict —
     cascade.status_text is SERVER-built (oneclick_view.cascade_status_text);
     a pre-change artifact lacks the key entirely -> "—" (absent-excluded-
     but-counted under a filter, the #1631 law — never a fabricated PASS). */
  { key: "cascade.status_text", label: "Cascade", type: "str", explain: "cascade_status" },
  /* `DEC-OPTIONS-EXEC-FEEDBACK-001`: the post-sweep exec-sim verdicts.
     Dotted paths into the additive options_exec/futures_exec objects — a
     pre-change (or provider-None) artifact lacks the key entirely -> the
     cell renders "—" via bxFieldValue's undefined and sorting puts the row
     LAST (zero new mechanism; the absent-key law). */
  { key: "options_exec.net_after_cost_paisa", label: "Opt net", type: "num", money: true, explain: "options_net_after_cost" },
  { key: "futures_exec.net_pnl_paisa", label: "Fut net", type: "num", money: true, explain: "futures_net" },
  { key: "family_id", label: "Family", type: "str", explain: "family_id" },
  { key: "mask_hex", label: "Mask", type: "str", explain: "mask_hex" }
];

/**
 * Dotted-path field access ("metrics.sharpe"); missing hops -> undefined.
 *
 * @param {object} row - a top100 contract row.
 * @param {string} field - the dotted path.
 * @returns {*} the value or undefined.
 */
function bxFieldValue(row, field) {
  var parts = String(field).split(".");
  var v = row;
  for (var i = 0; i < parts.length; i++) {
    if (v === null || v === undefined) {
      return undefined;
    }
    v = v[parts[i]];
  }
  return v;
}

/**
 * The deterministic rank-ascending tie-break (F-m2) — the LAST word of
 * every comparator, so the order is total.
 *
 * @param {object} a - row a.
 * @param {object} b - row b.
 * @returns {number} -1 | 0 | 1 by ascending rank.
 */
function bxRankTie(a, b) {
  var ar = a && a.rank;
  var br = b && b.rank;
  if (ar < br) {
    return -1;
  }
  if (ar > br) {
    return 1;
  }
  return 0;
}

/**
 * Build a comparator over top100 rows for one column.
 *
 * Relational comparison only (BigInt/string safe — never a subtraction or
 * Number() coercion); null/undefined sorts LAST regardless of dir; every
 * tie falls through to the rank-ascending tie-break (F-m2).
 *
 * @param {string} field - dotted field path (e.g. "metrics.sharpe").
 * @param {string} type - "num" | "str" | "vix" (vix compares band.lo).
 * @param {string} dir - "asc" | "desc".
 * @returns {Function} comparator(a, b) -> -1 | 0 | 1.
 */
function bxCompare(field, type, dir) {
  var sign = dir === "desc" ? -1 : 1;
  return function (a, b) {
    var av = bxFieldValue(a, field);
    var bv = bxFieldValue(b, field);
    if (type === "vix") {
      av = av && typeof av === "object" ? av.lo : null;
      bv = bv && typeof bv === "object" ? bv.lo : null;
    }
    var aNull = av === null || av === undefined;
    var bNull = bv === null || bv === undefined;
    if (aNull || bNull) {
      if (aNull && bNull) {
        return bxRankTie(a, b);
      }
      return aNull ? 1 : -1; // nulls LAST regardless of direction
    }
    if (type === "str" || Array.isArray(av) || Array.isArray(bv)) {
      av = Array.isArray(av) ? av.join(" ∧ ") : String(av);
      bv = Array.isArray(bv) ? bv.join(" ∧ ") : String(bv);
    }
    if (av < bv) {
      return -1 * sign;
    }
    if (av > bv) {
      return 1 * sign;
    }
    return bxRankTie(a, b); // rank ascending on primary-metric ties (F-m2)
  };
}

/**
 * Sorted COPY of rows by one column (the comparator's rank tie-break makes
 * the order total, hence stable on every engine).
 *
 * A-r4 input bound: at most BX_SCREENER_INPUT_CAP rows are sorted — rows
 * past the cap of an out-of-contract payload are ignored (they could never
 * render anyway; the page names the input cap via bxInputCapNote).
 *
 * @param {object[]} rows - the top100 rows.
 * @param {string} field - dotted field path.
 * @param {string} type - "num" | "str" | "vix".
 * @param {string} dir - "asc" | "desc".
 * @returns {object[]} a new sorted array (input untouched).
 */
function bxSortRows(rows, field, type, dir) {
  return rows.slice(0, BX_SCREENER_INPUT_CAP).sort(bxCompare(field, type, dir));
}

/**
 * True iff a row's VIX band OVERLAPS the filter range [lo, hi] (boundaries
 * INCLUSIVE — a band touching the filter edge matches). A null/malformed
 * band NEVER matches (the caller counts it — F-m3).
 *
 * @param {object|null} band - {lo, hi} or null.
 * @param {number} lo - filter low.
 * @param {number} hi - filter high.
 * @returns {boolean} the overlap verdict.
 */
function bxVixOverlap(band, lo, hi) {
  if (!band || typeof band !== "object") {
    return false;
  }
  if (!(typeof band.lo === "number") || !(typeof band.hi === "number")) {
    return false;
  }
  return band.lo <= hi && band.hi >= lo;
}

/**
 * Non-VIX filter predicate for one row (pure).
 *
 * filters: { tf: string[]|null, direction: string|null,
 *            instrument: string|null (exact match; an absent-instrument row
 *            never matches an active filter),
 *            min_win_rate: number|null, min_pf: number|null,
 *            max_abs_dd_paisa: number|null (magnitude cap — max_dd_paisa is
 *            stored NEGATIVE, the cap compares |dd|),
 *            executability: ""|"priced"|"positive"|undefined (the
 *            options_exec selection — absent-key rows never match an active
 *            filter, `DEC-OPTIONS-EXEC-FEEDBACK-001`) }.
 * A null/absent filter key never excludes.
 *
 * @param {object} row - a top100 contract row.
 * @param {object|null} filters - the active filters.
 * @returns {boolean} true to keep the row.
 */
function bxFilterRow(row, filters) {
  if (!filters) {
    return true;
  }
  if (filters.tf && filters.tf.length > 0 && filters.tf.indexOf(row.tf) === -1) {
    return false;
  }
  if (filters.direction && row.direction !== filters.direction) {
    return false;
  }
  // Instrument is exact-match like direction (DEC-ONECLICK-INSTRUMENT-
  // DISPLAY-001): a row WITHOUT the additive `instrument` key (a
  // pre-instrument artifact) never matches an ACTIVE filter — but the page
  // only offers filter options harvested from the loaded rows, so on such
  // an artifact the dropdown stays empty and the filter is never active.
  if (filters.instrument && row.instrument !== filters.instrument) {
    return false;
  }
  // Executability (`DEC-OPTIONS-EXEC-FEEDBACK-001`): a row WITHOUT the
  // additive options_exec object never matches an ACTIVE filter — a
  // pre-change artifact (or a --no-options-sim run) filters to empty under
  // "priced", never to a fabricated verdict; a not_priced row never matches
  // either (presence of the KEY is not a priced verdict). Deliberately NO
  // excluded-count line (unlike the null-VIX RANGE filter, where a null band
  // is ambiguous evidence): this is a positive SELECTION whose whole point
  // is presence — an absent/unpriced object is a definitive non-match.
  if (filters.executability) {
    var oe = row.options_exec;
    var priced = !!oe && typeof oe === "object" && oe.status === "priced";
    if (filters.executability === "priced" && !priced) {
      return false;
    }
    if (
      filters.executability === "positive" &&
      !(priced && typeof oe.net_after_cost_paisa === "number" && oe.net_after_cost_paisa > 0)
    ) {
      return false;
    }
  }
  var m = row.metrics || {};
  if (typeof filters.min_win_rate === "number" && !(m.win_rate >= filters.min_win_rate)) {
    return false;
  }
  if (typeof filters.min_pf === "number" && !(m.profit_factor >= filters.min_pf)) {
    return false;
  }
  if (typeof filters.max_abs_dd_paisa === "number") {
    if (!(typeof m.max_dd_paisa === "number") || Math.abs(m.max_dd_paisa) > filters.max_abs_dd_paisa) {
      return false;
    }
  }
  return true;
}

/**
 * Apply every filter, with the F-m3 honest-NULL law: under an ACTIVE VIX
 * band filter (filters.vix_lo/vix_hi numeric) a row whose vix_band is null
 * is EXCLUDED-but-COUNTED in null_vix_excluded — never silently dropped.
 *
 * A-r4 input bound: at most BX_SCREENER_INPUT_CAP rows are examined — rows
 * past the cap of an out-of-contract payload are ignored (they could never
 * render anyway; the page names the input cap via bxInputCapNote).
 *
 * @param {object[]} rows - the top100 rows.
 * @param {object|null} filters - bxFilterRow filters + vix_lo/vix_hi.
 * @returns {{rows: object[], null_vix_excluded: number}} the kept rows +
 *          the honest count.
 */
function bxApplyFilters(rows, filters) {
  var kept = [];
  var nullVix = 0;
  var vixActive =
    !!filters && (typeof filters.vix_lo === "number" || typeof filters.vix_hi === "number");
  var lo = vixActive && typeof filters.vix_lo === "number" ? filters.vix_lo : -Infinity;
  var hi = vixActive && typeof filters.vix_hi === "number" ? filters.vix_hi : Infinity;
  var bound = Math.min(rows.length, BX_SCREENER_INPUT_CAP);
  for (var i = 0; i < bound; i++) {
    var row = rows[i];
    if (!bxFilterRow(row, filters)) {
      continue;
    }
    if (vixActive) {
      var band = row.vix_band;
      var bandKnown =
        !!band && typeof band === "object" &&
        typeof band.lo === "number" && typeof band.hi === "number";
      if (!bandKnown) {
        nullVix += 1; // excluded-but-COUNTED (F-m3, R31 honest NULL)
        continue;
      }
      if (!bxVixOverlap(band, lo, hi)) {
        continue;
      }
    }
    kept.push(row);
  }
  return { rows: kept, null_vix_excluded: nullVix };
}

/**
 * The count-line suffix naming the null-VIX-band exclusions (F-m3 —
 * golden-pinned wording; "" when nothing was excluded).
 *
 * @param {number} n - the null_vix_excluded count.
 * @returns {string} the parenthesised note or "".
 */
function bxNullVixNote(n) {
  if (!(typeof n === "number") || n <= 0) {
    return "";
  }
  if (n === 1) {
    return "(1 row has no VIX band — clear the filter to see it)";
  }
  return "(" + n + " rows have no VIX band — clear the filter to see them)";
}

var BXScreener = {
  COLUMNS: BX_SCREENER_COLUMNS,
  RENDER_CAP: BX_SCREENER_RENDER_CAP,
  SHOW_ALL_CAP: BX_SCREENER_SHOW_ALL_CAP,
  INPUT_CAP: BX_SCREENER_INPUT_CAP,
  fieldValue: bxFieldValue,
  rankTie: bxRankTie,
  compare: bxCompare,
  sortRows: bxSortRows,
  vixOverlap: bxVixOverlap,
  filterRow: bxFilterRow,
  applyFilters: bxApplyFilters,
  nullVixNote: bxNullVixNote,
  inputCapRows: bxInputCapRows,
  renderCount: bxRenderCount,
  capNote: bxCapNote,
  inputCapNote: bxInputCapNote
};

/* Browsers get the global; a non-browser harness (the node golden) gets the
   CommonJS export (mirrors bx-format.js / bx-movers-time.js). */
if (typeof window !== "undefined") {
  window.BXScreener = BXScreener;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = BXScreener;
}
