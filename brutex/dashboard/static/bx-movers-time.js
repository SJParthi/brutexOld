/* bx-movers-time.js — the ONE shared /movers as-of TIME default + persist logic
 * (CLAUDE.md §11 COMMON).
 *
 * DEC-MOVERS-ASOF-LATEST-DEFAULT-001. The /movers "Time" dropdown used to pin
 * its value to a per-timeframe 09:15-step grid's first/last slot, so a coarse
 * timeframe (whose grid never reaches 15:29) defaulted to a partial-bucket
 * close (3-minute → 15:27) while 1-minute reached 15:29 — a TF switch on a
 * finalized day showed DIFFERENT closes. The fix defaults the as-of to blank
 * ("" ⇒ the backend whole-day cut ⇒ the latest traded minute, identical across
 * every timeframe by construction — the resampler takes close=.last() on a
 * timeframe-independent cut), and preserves an explicit pick across TF switches
 * ONLY when it is still a valid slot for the new timeframe.
 *
 * These are the PURE, side-effect-free primitives the movers page's fillTimes()
 * uses; extracting them here lets a node-executed golden pin them without a
 * browser harness (mirroring bx-format.js + test_dashboard_halfup_format.py).
 * Every value produced is a numeric-generated "HH:MM" string (never server
 * data), so the caller's innerHTML grid stays XSS-safe
 * (DEC-DASHBOARD-XSS-HARDENING-001).
 */
"use strict";

/* NSE/BSE regular session bounds as minutes-from-midnight IST: 09:15 inclusive
 * → 15:30 exclusive (CLAUDE.md §7 — mirrors movers_snapshot._SESSION_*_MIN). */
var BX_SESSION_OPEN_MIN = 9 * 60 + 15; /* 555 */
var BX_SESSION_CLOSE_MIN = 15 * 60 + 30; /* 930 */
/* A timeframe whose bar length spans the whole session (1day = 375 min) has no
 * intraday time-of-day choice (mirrors movers_snapshot._DAILY_MIN_FLOOR). */
var BX_DAILY_MIN_FLOOR = BX_SESSION_CLOSE_MIN - BX_SESSION_OPEN_MIN; /* 375 */

/* Zero-padded "HH:MM" of a minute-of-day (the JS twin of
 * movers_snapshot.minute_to_hhmm — the exact grid string, so an injected
 * latest minute is always a selectable option). */
function bxMinuteToHHMM(minute) {
  var h = Math.floor(minute / 60);
  var m = minute % 60;
  return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0");
}

/* True iff a bar length (minutes) has an intraday as-of time-of-day choice
 * (the JS twin of movers_snapshot.is_intraday_interval). A non-finite /
 * non-positive / daily (>= 375) value is NOT intraday. */
function bxIsIntradayMinutes(minutes) {
  return typeof minutes === "number" && minutes > 0 && minutes < BX_DAILY_MIN_FLOOR;
}

/* The intraday "HH:MM" grid slots of a timeframe (09:15-step, ascending; empty
 * for a daily / unknown / non-positive timeframe). Numeric-generated strings
 * only — safe to interpolate into innerHTML. */
function bxDefaultTimeOptions(minutes) {
  if (!bxIsIntradayMinutes(minutes)) return [];
  var out = [];
  var t = BX_SESSION_OPEN_MIN;
  while (t < BX_SESSION_CLOSE_MIN) {
    out.push(bxMinuteToHHMM(t));
    t += minutes;
  }
  return out;
}

/* Resolve the as-of value to SELECT for a timeframe on a TF switch:
 *   - "" / falsy prev            → "" (Latest — the default that shows the
 *                                   latest close, identical across every TF);
 *   - a retained explicit prev   → prev IFF it is a valid grid slot for this
 *                                   timeframe, else "" (fall back to Latest).
 * Pure — never touches the DOM. */
function bxResolveTime(prev, minutes) {
  if (!prev) return "";
  return bxDefaultTimeOptions(minutes).indexOf(prev) !== -1 ? prev : "";
}

var BXMoversTime = {
  SESSION_OPEN_MIN: BX_SESSION_OPEN_MIN,
  SESSION_CLOSE_MIN: BX_SESSION_CLOSE_MIN,
  DAILY_MIN_FLOOR: BX_DAILY_MIN_FLOOR,
  minuteToHHMM: bxMinuteToHHMM,
  isIntradayMinutes: bxIsIntradayMinutes,
  defaultTimeOptions: bxDefaultTimeOptions,
  resolveTime: bxResolveTime,
};

/* Browsers get the global; a non-browser harness (the node golden) gets the
 * CommonJS export (mirrors bx-format.js). */
if (typeof window !== "undefined") {
  window.BXMoversTime = BXMoversTime;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = BXMoversTime;
}
