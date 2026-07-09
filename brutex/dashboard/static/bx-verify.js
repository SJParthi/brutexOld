/* bx-verify.js — the PURE /movers ↔ /db live-verify comparison primitives
 * (DEC-MOVERS-DB-PROVENANCE-VERIFY-001; CLAUDE.md §11 COMMON).
 *
 * The /movers page cross-checks the DISPLAYED LTP (all three tabs) + %CHNG
 * (spot) of the visible top-K rows against an INDEPENDENT read of the SAME
 * lake's 1-minute base via the EXISTING /api/v1/db/candles API. These are the
 * side-effect-free comparison primitives that verify does — extracted here so
 * a node-executed golden can pin them without a browser harness (the
 * bx-format.js / bx-movers-time.js precedent).
 *
 * FORMATTER LAW (the load-bearing invariant): every numeric comparison
 * formats BOTH sides through the ONE shared bxFixed2 (bx-format.js —
 * ROUND_HALF_UP on the shortest decimal repr), never a re-implementation that
 * could false-mismatch, and never a raw float ===. The /db wire delivers
 * `change_pct` as a JSON STRING (db_view._json_safe stringifies Decimal), so
 * both sides are Number-coerced FIRST (bxNum) — feeding the raw string to
 * bxFixed2 would pass it through unchanged (bx-format.js line 34) and
 * guarantee a false mismatch.
 *
 * Honest envelope (Golden Rule #10): pure string/number math only — no fetch,
 * no DOM; a null / non-finite /db close is reported as a TRUTHFUL mismatch
 * (db: "null"), never silently skipped; an unverifiable displayed cell
 * ("—" / null pct / a non-contiguous baseline the caller opted out of) is a
 * COUNTED skip, never a fake green.
 */
"use strict";

/* The ONE shared display rounder — resolved from the browser global (movers.html
 * loads bx-format.js first, order-pinned) or the CommonJS sibling (the node
 * golden). Structurally NEVER re-implemented here. */
var _bxVerifyFixed2 =
  typeof bxFixed2 === "function"
    ? bxFixed2
    : typeof require === "function"
      ? require("./bx-format.js").bxFixed2
      : null;

/* Coerce a wire value (number OR the /db string-Decimal form) to a finite
 * number, or null. null / undefined / "" / non-numeric junk → null. */
function bxNum(v) {
  if (v === null || v === undefined || v === "") return null;
  var n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/* Parse a movers-side DISPLAY money/percent string to its number, or null.
 * Handles the format_inr wire form ("₹1,234,567.89" — WESTERN grouping,
 * money.py:105) AND a lakh-grouped form ("₹12,34,567.89") identically (the
 * comma strip is grouping-agnostic), a leading "+", a trailing "%", and the
 * trailing " *" (baseline-contiguity) / " ~" (strike-step) honesty markers.
 * The placeholder cells "—" / "?" and blanks are null (nothing displayed to
 * verify). A number passes through when finite. */
function bxParseInr(s) {
  if (s === null || s === undefined) return null;
  if (typeof s === "number") return Number.isFinite(s) ? s : null;
  var t = String(s).replace(/[\s*~]+$/, "").trim();
  if (t === "" || t === "—" || t === "?") return null;
  t = t.replace(/[₹,\s%]/g, "").replace(/^\+/, "");
  if (t === "") return null;
  var n = Number(t);
  return Number.isFinite(n) ? n : null;
}

/* Resolve the /db row corresponding to the movers as-of cut. *rows* is the
 * /api/v1/db/candles positional page in DESC timestamp order (one day,
 * desc=true); *tsIdx* is columns.indexOf("timestamp"); *time* is the movers
 * explicit "HH:MM" pick or null/"" for Latest (= the whole-day cut).
 *
 *   - Latest → the first (latest) row: the SAME bar the movers whole-day cut's
 *     last kept 1-minute close comes from.
 *   - explicit "HH:MM" → the first row whose IST wall-clock minute is <= the
 *     pick (desc order — mirrors the INCLUSIVE filter_to_ist_minute cut).
 *   - a pick BEFORE the first stored bar → null (no corresponding row).
 *
 * A date-only timestamp ("YYYY-MM-DD", the 1day render) slices to "" which
 * compares <= any "HH:MM", so a daily row resolves to the latest row — the
 * pinned, deliberate behavior (a 1day probe is never issued by the verify
 * driver, which always probes interval=1minute; this is the belt). */
function bxCutRow(rows, tsIdx, time) {
  if (!Array.isArray(rows) || rows.length === 0) return null;
  if (time === null || time === undefined || time === "") return rows[0];
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    if (!Array.isArray(row)) continue;
    var ts = row[tsIdx] === null || row[tsIdx] === undefined ? "" : String(row[tsIdx]);
    if (ts.slice(11, 16) <= time) return row;
  }
  return null;
}

/* Verify ONE movers row against its resolved /db row.
 *
 *   o.sym          — the row's symbol (for the mismatch report)
 *   o.ltpStr       — the DISPLAYED LTP wire string ("₹23,622.90")
 *   o.pctStr       — the raw full-precision pct_change wire string (or null)
 *   o.checkPct     — false ⇒ the % cell is a COUNTED skip (F&O tabs — the
 *                    prev-close baseline law is not proven byte-equal there;
 *                    also the caller's non-contiguous-baseline opt-out)
 *   o.dbClose      — the /db row's `close` cell (number on the wire)
 *   o.dbChangePct  — the /db row's `change_pct` cell (STRING on the wire, or
 *                    null when /db has no baseline for that day)
 *
 * → { mismatches: [{sym, cell: "LTP"|"%CHNG", shown, db}], checked, skipped }
 *
 * LTP: shown = bxFixed2(bxParseInr(ltpStr)) vs db = bxFixed2(bxNum(dbClose));
 * a null/non-finite db close is a TRUTHFUL mismatch (db: "null") — a corrupt
 * or missing stored cell must go red, never silently green. %CHNG: compared
 * only when checkPct && pctStr parses; a null /db change_pct (no /db
 * baseline) is a COUNTED skip, disclosed by the caller — never a red for a
 * baseline the two sides derive differently. */
function bxVerifyRow(o) {
  var fx = _bxVerifyFixed2;
  var mismatches = [];
  var checked = 0;
  var skipped = 0;
  var shownLtp = bxParseInr(o.ltpStr);
  if (shownLtp === null) {
    skipped += 1;
  } else {
    checked += 1;
    var dbLtp = bxNum(o.dbClose);
    if (dbLtp === null) {
      mismatches.push({ sym: o.sym, cell: "LTP", shown: fx(shownLtp), db: "null" });
    } else if (fx(shownLtp) !== fx(dbLtp)) {
      mismatches.push({ sym: o.sym, cell: "LTP", shown: fx(shownLtp), db: fx(dbLtp) });
    }
  }
  var shownPct = o.checkPct ? bxNum(o.pctStr) : null;
  if (!o.checkPct || shownPct === null) {
    skipped += 1;
  } else {
    var dbPct = bxNum(o.dbChangePct);
    if (dbPct === null) {
      skipped += 1; /* no /db baseline for that day — a counted, disclosed skip */
    } else {
      checked += 1;
      if (fx(shownPct) !== fx(dbPct)) {
        mismatches.push({ sym: o.sym, cell: "%CHNG", shown: fx(shownPct), db: fx(dbPct) });
      }
    }
  }
  return { mismatches: mismatches, checked: checked, skipped: skipped };
}

var BXVerify = {
  bxNum: bxNum,
  bxParseInr: bxParseInr,
  bxCutRow: bxCutRow,
  bxVerifyRow: bxVerifyRow,
};

/* Browsers get the global; a non-browser harness (the node golden) gets the
 * CommonJS export (mirrors bx-format.js / bx-movers-time.js). */
if (typeof window !== "undefined") {
  window.BXVerify = BXVerify;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = BXVerify;
}
