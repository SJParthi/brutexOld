/* bx-movers-chips.js — the PURE /movers header-chip derivations
 * (DEC-MOVERS-HEADER-CHIPS-001; CLAUDE.md §11 COMMON).
 *
 * The /movers header used to compress the Options stats into one dense
 * micro-text line (PCR jargon + dot-separated coverage). The redesign renders
 * readable LABELED chips instead; these are the side-effect-free derivations
 * behind them — extracted here so a node-executed golden pins them without a
 * browser harness (the bx-format.js / bx-movers-time.js / bx-verify.js
 * precedent):
 *
 * * bxPcrAggregate — the header-level Put/Call aggregate over the payload's
 *   per-underlying `pcr` rows: PLAIN INT SUMS of the raw wire fields
 *   (put_oi/call_oi/put_volume/call_volume/legs_read/legs_total/
 *   oi_missing_legs), so the chip ratio is a RATIO OF SUMS — an OI-weighted
 *   aggregate across every chain on screen. HONEST: that is NOT the mean of
 *   the per-underlying ratios; the per-underlying truth stays in the PCR
 *   detail panel, one click away.
 * * bxRatio2 — the guarded ratio + its 2dp display text through the ONE
 *   shared bxFixed2 (ROUND_HALF_UP on the shortest decimal repr — the pinned
 *   display law; structurally NEVER re-implemented here: no toFixed, no
 *   digit math). A zero/absent denominator is an honest null — never
 *   Infinity, never a fabricated 0 (Golden Rule #1).
 * * bxCoverageChip — the "165/202 contracts · 27 missing OI" wording + the
 *   AMBER verdict (unread legs OR missing OI cells).
 * * bxExpiryChipText — "16-Jun-2026 (near tier)" when every chain shares ONE
 *   expiry; "<tier> tier (varies by underlying)" otherwise; a malformed ISO
 *   ABSTAINS to the tier-only wording (never a wrong date).
 * * bxBuildup — the Dhan-style price×OI quadrant: price↑OI↑ Long Buildup ·
 *   price↓OI↑ Short Buildup · price↑OI↓ Short Covering · price↓OI↓ Long
 *   Unwinding; a missing / non-finite / exactly-flat input is null (an
 *   honest "—" — no four-quadrant claim on flat or absent data).
 *
 * Every produced string is numeric-generated or a static literal — the
 * caller still routes values into the DOM via createElement/textContent only
 * (DEC-DASHBOARD-XSS-HARDENING-001).
 */
"use strict";

/* The ONE shared display rounder — resolved from the browser global
 * (movers.html loads bx-format.js first, order-pinned) or the CommonJS
 * sibling (the node golden). Structurally NEVER re-implemented here. */
var _bxChipsFixed2 =
  typeof bxFixed2 === "function"
    ? bxFixed2
    : typeof require === "function"
      ? require("./bx-format.js").bxFixed2
      : null;

/* A finite, non-negative number — else null (the PCR sums are counts). */
function _bxChipsNonNeg(v) {
  return typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;
}

/* Guarded two-side ratio: {ratio, text(2dp half-up)} or null. null when
 * either side is absent/non-finite/negative OR the denominator is zero —
 * the honest sentinel the PCR panel already renders as an em-dash. */
function bxRatio2(num, den) {
  var n = _bxChipsNonNeg(num);
  var d = _bxChipsNonNeg(den);
  if (n === null || d === null || d <= 0) return null;
  var ratio = n / d;
  return { ratio: ratio, text: _bxChipsFixed2(ratio) };
}

/* The raw wire fields summed across the payload's pcr rows (ints by the
 * pcr_row_json contract; a defensive non-finite/absent cell adds nothing —
 * never NaN-poisons the whole aggregate). */
var _BX_PCR_SUM_FIELDS = [
  "put_oi",
  "call_oi",
  "put_volume",
  "call_volume",
  "legs_read",
  "legs_total",
  "oi_missing_legs",
];

/* Aggregate the per-underlying pcr rows into the header-chip numbers, or
 * null for a non-array / empty input (no chains on screen — no chips). */
function bxPcrAggregate(pcrRows) {
  if (!Array.isArray(pcrRows) || pcrRows.length === 0) return null;
  var sums = {};
  _BX_PCR_SUM_FIELDS.forEach(function (key) {
    sums[key] = 0;
  });
  pcrRows.forEach(function (row) {
    if (row === null || typeof row !== "object") return;
    _BX_PCR_SUM_FIELDS.forEach(function (key) {
      var v = row[key];
      if (typeof v === "number" && Number.isFinite(v)) sums[key] += v;
    });
  });
  return {
    chains: pcrRows.length,
    putOi: sums.put_oi,
    callOi: sums.call_oi,
    putVolume: sums.put_volume,
    callVolume: sums.call_volume,
    legsRead: sums.legs_read,
    legsTotal: sums.legs_total,
    oiMissing: sums.oi_missing_legs,
    oi: bxRatio2(sums.put_oi, sums.call_oi),
    vol: bxRatio2(sums.put_volume, sums.call_volume),
  };
}

/* The Coverage chip: text + the AMBER verdict (unread legs OR missing OI
 * cells). null for a null/non-object aggregate. */
function bxCoverageChip(agg) {
  if (agg === null || typeof agg !== "object") return null;
  var text =
    agg.legsRead +
    "/" +
    agg.legsTotal +
    " contracts" +
    (agg.oiMissing > 0 ? " · " + agg.oiMissing + " missing OI" : "");
  return { text: text, amber: agg.legsRead < agg.legsTotal || agg.oiMissing > 0 };
}

var _BX_MONTH_WORDS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

/* "YYYY-MM-DD" → "DD-Mon-YYYY", or null on a malformed / out-of-range ISO
 * (the ABSTAIN arm — never a wrong date on screen). Static month table, no
 * Date() timezone traps. */
function _bxExpiryWords(iso) {
  if (typeof iso !== "string") return null;
  var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (m === null) return null;
  var month = parseInt(m[2], 10);
  var day = parseInt(m[3], 10);
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  return m[3] + "-" + _BX_MONTH_WORDS[month - 1] + "-" + m[1];
}

/* The Expiry chip text: ONE shared expiry across every pcr row reads
 * "16-Jun-2026 (near tier)"; >= 2 distinct expiries read
 * "near tier (varies by underlying)"; no rows / malformed ISO abstain to
 * the tier-only wording. */
function bxExpiryChipText(pcrRows, tierWord) {
  var tier = String(tierWord == null ? "" : tierWord) + " tier";
  if (!Array.isArray(pcrRows) || pcrRows.length === 0) return tier;
  var seen = {};
  var unique = [];
  pcrRows.forEach(function (row) {
    var iso = row === null || typeof row !== "object" ? null : row.expiry;
    var key = typeof iso === "string" ? iso : "";
    if (!Object.prototype.hasOwnProperty.call(seen, key)) {
      seen[key] = true;
      unique.push(key);
    }
  });
  if (unique.length !== 1) return tier + " (varies by underlying)";
  var words = _bxExpiryWords(unique[0]);
  return words === null ? tier : words + " (" + tier + ")";
}

/* The Dhan-style buildup quadrant off the row's price %-change + OI change.
 * null (an honest "—") when EITHER input is not a finite number or is
 * exactly 0 — a flat/missing side supports no quadrant claim. */
function bxBuildup(pctChange, oiDelta) {
  if (typeof pctChange !== "number" || !Number.isFinite(pctChange) || pctChange === 0) return null;
  if (typeof oiDelta !== "number" || !Number.isFinite(oiDelta) || oiDelta === 0) return null;
  if (pctChange > 0) {
    return oiDelta > 0
      ? { label: "Long Buildup", cls: "bu-long" }
      : { label: "Short Covering", cls: "bu-cover" };
  }
  return oiDelta > 0
    ? { label: "Short Buildup", cls: "bu-short" }
    : { label: "Long Unwinding", cls: "bu-unwind" };
}

var BXMoversChips = {
  bxRatio2: bxRatio2,
  bxPcrAggregate: bxPcrAggregate,
  bxCoverageChip: bxCoverageChip,
  bxExpiryChipText: bxExpiryChipText,
  bxBuildup: bxBuildup,
};

/* Browsers get the global; a non-browser harness (the node golden) gets the
 * CommonJS export (mirrors bx-format.js / bx-movers-time.js). */
if (typeof window !== "undefined") {
  window.BXMoversChips = BXMoversChips;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = BXMoversChips;
}
