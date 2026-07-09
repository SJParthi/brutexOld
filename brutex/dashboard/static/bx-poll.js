/* bx-poll.js — the ONE shared dashboard poll-discipline core (CLAUDE.md §11 COMMON).
 *
 * DEC-OPERATOR-PAGE-REAL-001. The self-recovering poll discipline
 * (DEC-PULL-UI-STALENESS-001 — the 2026-07-06 38-minute frozen-tab incident
 * fix) previously lived as two inline copies (bx-rate-monitor.js +
 * bx-liveproof.js). This file factors the discipline into ONE reusable core
 * so bx-oneclick.js does not mint a THIRD copy:
 *
 *   - every failure mode is a COUNTED poll failure: fetch rejection AND a
 *     non-ok HTTP status AND a malformed body (non-JSON / non-object);
 *   - quick retries first (QUICK_RETRIES x QUICK_RETRY_MS — the /universe
 *     600 ms quick-retry envelope), then capped exponential backoff
 *     3 s -> 30 s doubling; the poller NEVER gives up;
 *   - `if (document.hidden)` guard FIRST (a background tab fires ZERO
 *     fetches) + a visibilitychange catch-up that calls poll() DIRECTLY
 *     (never a 0 ms timer — the pollSeq race, bx-rate-monitor precedent);
 *   - a monotonic pollSeq guard so a superseded response never renders or
 *     schedules;
 *   - a client-clock "updated Xs ago" staleness readout (amber past
 *     STALE_WARN_MS -> LOUD RED explicit "STALE" past STALE_RED_MS) whose
 *     1 s ticker itself short-circuits under document.hidden (F-m5).
 *
 * Honest envelope (Golden Rule #10): refactoring bx-rate-monitor.js /
 * bx-liveproof.js onto this core is OUT of this PR (named in the ledger) —
 * bx-oneclick.js is the first consumer. The pure primitives
 * (bxBackoffDelayMs / bxStalenessState) are node-golden-tested; the stateful
 * factories (bxCreatePoller / bxStalenessTicker) are browser-only and are
 * pinned by SOURCE-STRING tests, not executed (the tests/browser
 * Playwright job — #1557 — does not drive the oneclick pages).
 */
"use strict";

/**
 * Shared poll-discipline defaults (DEC-PULL-UI-STALENESS-001 values).
 *
 * @type {{POLL_MS: number, QUICK_RETRIES: number, QUICK_RETRY_MS: number,
 *         BACKOFF_BASE_MS: number, BACKOFF_MAX_MS: number,
 *         STALE_WARN_MS: number, STALE_RED_MS: number, AGE_TICK_MS: number,
 *         FETCH_TIMEOUT_MS: number}}
 */
var BX_POLL_DEFAULTS = {
  POLL_MS: 3000, // normal cadence
  QUICK_RETRIES: 2, // first failures get a fast retry (the /universe 600 ms envelope)
  QUICK_RETRY_MS: 600,
  BACKOFF_BASE_MS: 3000, // then 3 s doubling per consecutive failure...
  BACKOFF_MAX_MS: 30000, // ...capped at 30 s — the poller NEVER gives up
  STALE_WARN_MS: 10000, // amber "updated Xs ago"
  STALE_RED_MS: 30000, // LOUD RED explicit "STALE"
  AGE_TICK_MS: 1000, // the ~1 s client staleness ticker
  FETCH_TIMEOUT_MS: 10000 // abort bound armed through the BODY read
};

/**
 * Fold caller overrides over BX_POLL_DEFAULTS (pure).
 *
 * @param {object|undefined} opts - partial overrides.
 * @returns {object} a fresh options object with every default present.
 */
function bxPollOpts(opts) {
  var out = {};
  var k;
  for (k in BX_POLL_DEFAULTS) {
    if (Object.prototype.hasOwnProperty.call(BX_POLL_DEFAULTS, k)) {
      out[k] = BX_POLL_DEFAULTS[k];
    }
  }
  if (opts) {
    for (k in opts) {
      if (Object.prototype.hasOwnProperty.call(opts, k)) {
        out[k] = opts[k];
      }
    }
  }
  return out;
}

/**
 * Delay before the next poll after `failures` CONSECUTIVE counted failures
 * (pure — node-golden-tested).
 *
 * failures 1..QUICK_RETRIES -> QUICK_RETRY_MS (quick retries first); beyond
 * that, capped exponential backoff BACKOFF_BASE_MS * 2^n up to
 * BACKOFF_MAX_MS. The exponent is clamped BEFORE Math.pow (overflow-proof —
 * the bx-rate-monitor precedent). failures <= 0 -> the normal POLL_MS.
 *
 * @param {number} failures - consecutive counted failures (>= 0).
 * @param {object} [opts] - BX_POLL_DEFAULTS overrides.
 * @returns {number} the delay in milliseconds.
 */
function bxBackoffDelayMs(failures, opts) {
  var o = bxPollOpts(opts);
  if (!(typeof failures === "number") || failures <= 0) {
    return o.POLL_MS;
  }
  if (failures <= o.QUICK_RETRIES) {
    return o.QUICK_RETRY_MS;
  }
  var exp = Math.min(failures - o.QUICK_RETRIES - 1, 4);
  return Math.min(o.BACKOFF_BASE_MS * Math.pow(2, exp), o.BACKOFF_MAX_MS);
}

/**
 * Classify a client-clock age since the last SUCCESSFUL poll into the
 * staleness readout (pure — node-golden-tested).
 *
 * Levels: "fresh" (< STALE_WARN_MS) -> "warn" (amber) -> "stale" (LOUD RED
 * with explicit "STALE" wording). The action tail is FAILURE-gated
 * (bx-rate-monitor round-5 law): "— retrying" is claimed ONLY when a counted
 * failure actually scheduled one; at zero failures a slow in-flight poll
 * reads "— awaiting response".
 *
 * @param {number|null} ageMs - ms since the last successful poll (null = never).
 * @param {number} failures - consecutive counted failures.
 * @param {object} [opts] - BX_POLL_DEFAULTS overrides.
 * @returns {{level: string, text: string}} the readout state.
 */
function bxStalenessState(ageMs, failures, opts) {
  var o = bxPollOpts(opts);
  if (ageMs === null || ageMs === undefined || !(typeof ageMs === "number") || !isFinite(ageMs)) {
    return { level: "fresh", text: "—" };
  }
  var ageS = Math.max(0, Math.round(ageMs / 1000));
  var base = ageS + "s ago";
  if (ageMs < o.STALE_WARN_MS) {
    return { level: "fresh", text: base };
  }
  var tail = failures > 0 ? " — retrying" : " — awaiting response";
  if (ageMs >= o.STALE_RED_MS) {
    return { level: "stale", text: "STALE — " + base + tail };
  }
  return { level: "warn", text: base + tail };
}

/* ── Browser-only stateful core below (pinned by source-string tests). ── */

/**
 * Monotonic client clock (immune to NTP/wall jumps); Date.now fallback.
 *
 * @returns {number} milliseconds on the best available client clock.
 */
function bxNowMs() {
  return typeof performance !== "undefined" && performance.now ? performance.now() : Date.now();
}

/**
 * Bounded JSON fetch: a non-ok status THROWS (a counted failure, never a
 * silent skip) and the abort timer stays ARMED through the BODY read so a
 * headers-then-stalled-body response aborts too (bx-rate-monitor precedent).
 *
 * @param {string} url - the endpoint.
 * @param {number} timeoutMs - the abort bound.
 * @returns {Promise<object>} the parsed JSON payload.
 */
function bxFetchJson(url, timeoutMs) {
  var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
  var timer = setTimeout(function () {
    if (ctrl) {
      ctrl.abort();
    }
  }, timeoutMs);
  var init = ctrl ? { signal: ctrl.signal } : {};
  return fetch(url, init)
    .then(function (r) {
      if (!r.ok) {
        throw new Error("poll failed: HTTP " + r.status);
      }
      return r.json(); // abort timer still armed through the body read
    })
    .then(
      function (data) {
        clearTimeout(timer);
        return data;
      },
      function (err) {
        clearTimeout(timer);
        throw err;
      }
    );
}

/**
 * Build a self-recovering poller over ONE endpoint (browser-only).
 *
 * cfg: { url: string, onData(data), onFail?(failures), pollMs?: number,
 *        opts?: BX_POLL_DEFAULTS overrides, fetchJson?(url, timeoutMs) }.
 *
 * Discipline: document.hidden guard FIRST (zero fetches while hidden; the
 * chain re-arms at normal cadence), pollSeq re-entrancy token (a superseded
 * response never renders or schedules), every failure mode counted (fetch
 * rejection AND non-ok AND malformed body), quick-retry-then-backoff via
 * bxBackoffDelayMs, visibilitychange catch-up that calls poll() DIRECTLY.
 *
 * @param {object} cfg - the poller configuration (see above).
 * @returns {{start: Function, stop: Function, pollNow: Function,
 *            failures: Function, ageMs: Function}} the poller handle.
 */
function bxCreatePoller(cfg) {
  var o = bxPollOpts(cfg.opts);
  var pollMs = typeof cfg.pollMs === "number" ? cfg.pollMs : o.POLL_MS;
  var fetchJson = cfg.fetchJson || bxFetchJson;
  var pollSeq = 0; // re-entrancy token: a superseded response never renders or schedules
  var pollFailures = 0;
  var pollTimer = null;
  var stopped = false;
  var lastOkMs = null; // monotonic stamp of the last SUCCESSFUL poll
  var lastOkWallMs = null; // Date.now() twin (suspend guard — max() only OVER-reports)
  var bootMs = null;
  var bootWallMs = null;
  var visWired = false;

  function schedule(delayMs) {
    if (stopped) {
      return;
    }
    if (pollTimer) {
      clearTimeout(pollTimer);
    }
    pollTimer = setTimeout(poll, delayMs);
  }

  function poll() {
    // Hidden-tab pause (DEC-LEDGER-DELTA-O-PAGE-001 v29 pattern): the guard
    // returns BEFORE pollSeq is bumped and BEFORE any fetch — a hidden
    // period counts ZERO pollFailures and fires ZERO fetches; the
    // visibilitychange catch-up below repolls immediately on return.
    if (document.hidden) {
      schedule(pollMs);
      return;
    }
    var seq = ++pollSeq;
    fetchJson(cfg.url, o.FETCH_TIMEOUT_MS)
      .then(function (data) {
        if (seq !== pollSeq || stopped) {
          return;
        }
        if (!data || typeof data !== "object") {
          throw new Error("malformed payload"); // counted like any failure
        }
        pollFailures = 0;
        lastOkMs = bxNowMs();
        lastOkWallMs = Date.now();
        cfg.onData(data);
        schedule(pollMs);
      })
      .catch(function () {
        if (seq !== pollSeq || stopped) {
          return;
        }
        pollFailures += 1; // fetch rejection AND non-ok AND malformed body all land here
        if (cfg.onFail) {
          cfg.onFail(pollFailures);
        }
        schedule(bxBackoffDelayMs(pollFailures, o));
      });
  }

  function start() {
    stopped = false;
    bootMs = bxNowMs();
    bootWallMs = Date.now();
    if (!visWired) {
      visWired = true;
      document.addEventListener("visibilitychange", function () {
        // Catch-up poll on return: poll() DIRECTLY, never a 0 ms timer —
        // poll() bumps pollSeq synchronously so an in-flight failure
        // callback can never replace the instant re-poll with a backoff.
        if (!document.hidden && !stopped) {
          if (pollTimer) {
            clearTimeout(pollTimer);
            pollTimer = null;
          }
          poll();
        }
      });
    }
    poll(); // self-schedules from here on (success -> pollMs, failure -> backoff)
  }

  function stop() {
    stopped = true;
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function ageMs() {
    var ref = lastOkMs !== null ? lastOkMs : bootMs;
    var wallRef = lastOkWallMs !== null ? lastOkWallMs : bootWallMs;
    if (ref === null) {
      return null;
    }
    var age = bxNowMs() - ref;
    if (wallRef !== null) {
      age = Math.max(age, Date.now() - wallRef); // suspend guard: only OVER-reports
    }
    return age;
  }

  return {
    start: start,
    stop: stop,
    pollNow: poll,
    failures: function () {
      return pollFailures;
    },
    ageMs: ageMs
  };
}

/**
 * Wire the ~1 s "updated Xs ago" staleness ticker over a poller
 * (browser-only). The ticker itself short-circuits under document.hidden
 * (F-m5 — a hidden tab runs zero DOM writes; the readout is
 * timestamp-derived, so it is already correct the instant the tab returns).
 *
 * @param {object} poller - a bxCreatePoller handle (ageMs()/failures()).
 * @param {Function} render - render({level, text}) callback (textContent-only).
 * @param {object} [opts] - BX_POLL_DEFAULTS overrides.
 * @returns {{stop: Function}} the ticker handle.
 */
function bxStalenessTicker(poller, render, opts) {
  var o = bxPollOpts(opts);
  function tick() {
    if (document.hidden) {
      return; // the 1 s ticker short-circuits while hidden (F-m5)
    }
    render(bxStalenessState(poller.ageMs(), poller.failures(), o));
  }
  var timer = setInterval(tick, o.AGE_TICK_MS);
  tick();
  return {
    stop: function () {
      clearInterval(timer);
    }
  };
}

var BXPoll = {
  DEFAULTS: BX_POLL_DEFAULTS,
  opts: bxPollOpts,
  backoffDelayMs: bxBackoffDelayMs,
  stalenessState: bxStalenessState,
  nowMs: bxNowMs,
  fetchJson: bxFetchJson,
  createPoller: bxCreatePoller,
  stalenessTicker: bxStalenessTicker
};

/* Browsers get the global; a non-browser harness (the node golden) gets the
   CommonJS export (mirrors bx-format.js / bx-movers-time.js). */
if (typeof window !== "undefined") {
  window.BXPoll = BXPoll;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = BXPoll;
}
