/* bx-movers-time.js — the ONE shared /movers as-of TIME default + persist +
 * bucket-snap logic (CLAUDE.md §11 COMMON).
 *
 * DEC-MOVERS-ASOF-LATEST-DEFAULT-001 (the Latest default) +
 * DEC-MOVERS-TIME-SNAP-UX-001 (persist + bucket-snap). The /movers "Time"
 * dropdown used to pin its value to a per-timeframe 09:15-step grid's first
 * slot (pre-#1243 — the operator's observed 09:15 reset), then to a "" Latest
 * fallback whenever the picked minute was not a slot of the new timeframe
 * (bxResolveTime, now SUPERSEDED AND DELETED — zero consumers remain). Both
 * reset an explicit pick on a TF switch. The fix keeps ONE persistent as-of
 * INSTANT (minute-of-day) and SNAPS it to the bucket of the new timeframe
 * that CONTAINS it, so 1min@15:29 → 3min shows the completed 15:27-labeled
 * bucket, and 1→3→5→1min round-trips the instant exactly.
 *
 * THE MAPPING LAW (pinned by tests/unit/test_dashboard_movers_time_snap.py):
 * with OPEN=555 (09:15), last session minute 929 (15:29), for an intraday
 * timeframe of m minutes,
 *
 *   bucketStart(t, m) = 555 + floor((clamp(t, 555, 929) − 555) / m) · m
 *   bucketFinal(t, m) = min(bucketStart(t, m) + m − 1, 929)
 *
 * Each dropdown <option> carries label = HH:MM(bucketStart) — the bucket-START
 * minute the /db candle grid labels its bars with — and value =
 * HH:MM(bucketFinal), the minute actually SENT as ?time=. The backend as-of
 * cut is inclusive-≤ on the RAW 1-minute frame BEFORE resampling
 * (movers_snapshot.filter_to_ist_minute), so a bucket-START cutoff serves a
 * FORMING partial bucket while the bucket-FINAL cutoff serves the COMPLETED
 * bar — byte-equal to the stored /db row (the #1145 stored-vs-resample
 * equality; every non-last value is _stored_serves_cutoff-aligned, the
 * always-"15:29" last value rides the resample path on non-divisor TFs with
 * byte-equal values). Containing-bucket disclosure: the snap may extend the
 * effective as-of by up to m−1 minutes past the raw instant — the page's
 * as-of span label ("15:27–15:29") makes that visible.
 *
 * These are PURE, side-effect-free primitives the movers page's fillTimes()
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
/* The last tradable session minute (15:29) — every bucket-final cutoff clamps
 * here, so the last dropdown value is ALWAYS "15:29" on every timeframe. */
var BX_SESSION_LAST_MIN = BX_SESSION_CLOSE_MIN - 1; /* 929 */
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

/* Parse an "HH:MM" string to a minute-of-day int — the JS twin of
 * movers_snapshot._cutoff_minute, except it ABSTAINS (null) instead of
 * throwing: null for ""/null/undefined/malformed input (never NaN, never a
 * throw — a malformed server echo degrades to the Latest semantics). */
function bxCutoffMinute(hhmm) {
  if (typeof hhmm !== "string") return null;
  var m = /^(\d{1,2}):(\d{2})$/.exec(hhmm);
  if (m === null) return null;
  return parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
}

/* True iff a bar length (minutes) has an intraday as-of time-of-day choice
 * (the JS twin of movers_snapshot.is_intraday_interval). A non-finite /
 * non-positive / daily (>= 375) value is NOT intraday. */
function bxIsIntradayMinutes(minutes) {
  return typeof minutes === "number" && minutes > 0 && minutes < BX_DAILY_MIN_FLOOR;
}

/* Clamp a minute-of-day instant into the session [09:15, 15:29]. */
function bxClampInstant(instantMin) {
  return Math.min(Math.max(instantMin, BX_SESSION_OPEN_MIN), BX_SESSION_LAST_MIN);
}

/* The bucket-START minute (the /db row LABEL) of the m-minute bucket
 * CONTAINING the instant — bucketStart(t, m) per the mapping law above. */
function bxBucketStart(instantMin, minutes) {
  var t = bxClampInstant(instantMin);
  return (
    BX_SESSION_OPEN_MIN +
    Math.floor((t - BX_SESSION_OPEN_MIN) / minutes) * minutes
  );
}

/* The bucket-FINAL minute (the SENT ?time= cutoff) of the m-minute bucket
 * containing the instant — min(bucketStart + m − 1, 929). Sending this serves
 * the COMPLETED bucket (inclusive-≤ raw-1m cut), never a forming partial. */
function bxBucketFinal(instantMin, minutes) {
  return Math.min(bxBucketStart(instantMin, minutes) + minutes - 1, BX_SESSION_LAST_MIN);
}

/* Snap a persistent instant to the timeframe's SENT value: "" (Latest) when
 * instantMin is null (no explicit pick) OR the timeframe is daily/unknown,
 * else HH:MM(bucketFinal) — the value of the containing bucket's option. */
function bxSnapTime(instantMin, minutes) {
  if (instantMin === null || instantMin === undefined || !bxIsIntradayMinutes(minutes)) {
    return "";
  }
  return bxMinuteToHHMM(bxBucketFinal(instantMin, minutes));
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

/* The dropdown {label, value} pairs of a timeframe: label = the bucket-START
 * grid slot (what /db labels the bar — the ONE bxDefaultTimeOptions grid),
 * value = the bucket-FINAL sent cutoff (min(start + m − 1, 929)) — the LAST
 * pair's value is ALWAYS "15:29". Empty for daily/unknown. Values are
 * distinct by construction (each start is distinct and only the last final
 * clamps), so <select>.value = a snapped value selects exactly one option. */
function bxTimeOptionPairs(minutes) {
  return bxDefaultTimeOptions(minutes).map(function (label) {
    var start = bxCutoffMinute(label);
    return {
      label: label,
      value: bxMinuteToHHMM(Math.min(start + minutes - 1, BX_SESSION_LAST_MIN)),
    };
  });
}

/* Forming-bar verdict for the Latest as-of (DEC-MOVERS-TIME-SNAP-UX-001
 * item 4): given the payload's latest_time echo ("HH:MM") and the selected
 * timeframe's minutes, report whether that instant falls MID-bucket (the bar
 * on screen is a forming partial) and where the completed bar closes.
 * null (honest abstain — no forming claim) when the timeframe is
 * daily/unknown OR latestHHMM is absent/malformed. The instant is clamped
 * into the session first, so an out-of-session echo can never claim forming.
 * A finalized day (latest 15:29) is final==929 on EVERY timeframe → never
 * forming (the session-truncated last bucket closes at 15:29). */
function bxFormingInfo(latestHHMM, minutes) {
  if (!bxIsIntradayMinutes(minutes)) return null;
  var raw = bxCutoffMinute(latestHHMM);
  if (raw === null) return null;
  var t = bxClampInstant(raw);
  var fin = bxBucketFinal(t, minutes);
  return {
    forming: t !== fin,
    barLabel: bxMinuteToHHMM(bxBucketStart(t, minutes)),
    upTo: latestHHMM,
    closesAt: bxMinuteToHHMM(fin),
  };
}

var BXMoversTime = {
  SESSION_OPEN_MIN: BX_SESSION_OPEN_MIN,
  SESSION_CLOSE_MIN: BX_SESSION_CLOSE_MIN,
  SESSION_LAST_MIN: BX_SESSION_LAST_MIN,
  DAILY_MIN_FLOOR: BX_DAILY_MIN_FLOOR,
  minuteToHHMM: bxMinuteToHHMM,
  cutoffMinute: bxCutoffMinute,
  isIntradayMinutes: bxIsIntradayMinutes,
  defaultTimeOptions: bxDefaultTimeOptions,
  bucketStart: bxBucketStart,
  bucketFinal: bxBucketFinal,
  snapTime: bxSnapTime,
  timeOptionPairs: bxTimeOptionPairs,
  formingInfo: bxFormingInfo,
};

/* Browsers get the global; a non-browser harness (the node golden) gets the
 * CommonJS export (mirrors bx-format.js). */
if (typeof window !== "undefined") {
  window.BXMoversTime = BXMoversTime;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = BXMoversTime;
}
