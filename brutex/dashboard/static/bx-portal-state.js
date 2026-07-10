/* bx-portal-state.js — the PURE operator-portal URL-state codec
 * (DEC-OPERATOR-PAGE-CONSOLIDATE-001).
 *
 * No DOM, no fetch, no timers — a pure string codec consumable by all
 * three pages (/ · /screener · /strategy) and by the node golden harness.
 *
 * Grammar: ``?winners=1&rank=N[&run_id=<id>][&trade=T]``
 *   - ``winners=1``  ⇒ the operator winners section is expanded;
 *   - ``rank=N``     ⇒ the strategy drawer is open at rank N (rank present
 *     implies winners — the SERIALIZER forces ``winners=1`` whenever a rank
 *     is set; the parser reads ``winners`` strictly);
 *   - ``run_id``     ⇒ READ-only deep-link support (the portal never WRITES
 *     it from in-page actions — DEC-OPERATOR-PORTAL-RUNID-001 is the named
 *     follow-up for the write side);
 *   - ``trade``      ⇒ reserved (codec supports it; portal v1 never writes).
 *
 * bxParseStrategyParams carries the PINNED /strategy deep-link contract
 * expressions VERBATIM (the ``/strategy?run_id=<id>&rank=<n>`` contract is
 * a STABLE contract consumed by /results-side link builders), and
 * bxStrategyHref is the ONE builder of every /strategy href in the
 * codebase (encodeURIComponent on every component — a hostile run_id never
 * survives raw into a URL).
 *
 * Honest envelope (Golden Rule #10): bxPortalSerialize preserves FOREIGN
 * query params byte-for-byte (raw-pair passthrough — never a decode/
 * re-encode round trip) in their original relative order, then appends the
 * portal-owned keys in ONE deterministic order (winners · rank · run_id ·
 * trade); double-applying the same patch is idempotent. The codec is
 * executed for real by node goldens (tests/unit/
 * test_dashboard_operator_portal.py); browser wiring on top of it is
 * source-string-pinned only (the #1557 Playwright job does not drive
 * these pages).
 */
"use strict";

/** The portal-owned query keys, in the ONE deterministic output order. */
var BX_PORTAL_KEYS = ["winners", "rank", "run_id", "trade"];

/** Normalize a search string: tolerate null/undefined and a leading '?'. */
function bxPortalRawPairs(search) {
  var raw = String(search === undefined || search === null ? "" : search);
  if (raw.charAt(0) === "?") {
    raw = raw.slice(1);
  }
  if (raw === "") {
    return [];
  }
  return raw.split("&");
}

/** Decode a raw pair's KEY name (value bytes are never touched here). */
function bxPortalPairKey(pair) {
  var eq = pair.indexOf("=");
  var rawKey = eq === -1 ? pair : pair.slice(0, eq);
  try {
    return decodeURIComponent(rawKey.replace(/\+/g, " "));
  } catch (err) {
    return rawKey; // malformed escape: compare the raw bytes honestly
  }
}

/** Decode a raw pair's value ('' for a bare key). */
function bxPortalPairValue(pair) {
  var eq = pair.indexOf("=");
  var rawVal = eq === -1 ? "" : pair.slice(eq + 1);
  try {
    return decodeURIComponent(rawVal.replace(/\+/g, " "));
  } catch (err) {
    return rawVal;
  }
}

/**
 * Parse the portal URL state from a search string.
 *
 * @param {string|null|undefined} search - window.location.search shape.
 * @returns {{winners: boolean, rank: number|null, runId: string|null,
 *   trade: number|null}} - ``winners`` is STRICTLY ``winners === '1'``;
 *   ``rank`` rides the pinned strategy clamp law; ``runId`` passes through
 *   verbatim (the server validates via is_safe_run_id); ``trade`` clamps
 *   to a non-negative integer.
 */
function bxPortalParse(search) {
  var pairs = bxPortalRawPairs(search);
  var seen = {};
  for (var i = 0; i < pairs.length; i++) {
    var key = bxPortalPairKey(pairs[i]);
    if (BX_PORTAL_KEYS.indexOf(key) !== -1 && !(key in seen)) {
      seen[key] = bxPortalPairValue(pairs[i]);
    }
  }
  var rawRank = "rank" in seen ? seen.rank : null;
  var rawTrade = "trade" in seen ? seen.trade : null;
  return {
    winners: ("winners" in seen ? seen.winners : null) === "1",
    rank: rawRank === null ? null : Math.max(1, parseInt(rawRank, 10) || 1),
    runId: "run_id" in seen ? seen.run_id : null,
    trade: rawTrade === null ? null : Math.max(0, parseInt(rawTrade, 10) || 0)
  };
}

/**
 * Serialize a PATCH over an existing search string.
 *
 * Foreign (non-portal) params are preserved byte-for-byte in their original
 * relative order; portal keys are appended in the deterministic
 * BX_PORTAL_KEYS order. ``null`` in the patch DELETES the key;
 * ``winners: false`` deletes 'winners' AND any lingering rank (the coupling
 * law's contrapositive — rank ⇒ winners, so ¬winners ⇒ ¬rank; a rank set in
 * the SAME patch still wins and re-forces winners); a set rank FORCES
 * ``winners=1``.
 *
 * @param {string|null|undefined} search - the current search string.
 * @param {{winners?: boolean, rank?: number|null, runId?: string|null,
 *   trade?: number|null}} patch - keys absent from the patch keep their
 *   current value.
 * @returns {string} '?…' or '' (empty when no params survive).
 */
function bxPortalSerialize(search, patch) {
  var p = patch && typeof patch === "object" ? patch : {};
  var pairs = bxPortalRawPairs(search);
  var foreign = [];
  var known = {};
  for (var i = 0; i < pairs.length; i++) {
    var key = bxPortalPairKey(pairs[i]);
    if (BX_PORTAL_KEYS.indexOf(key) === -1) {
      foreign.push(pairs[i]); // byte-for-byte passthrough, original order
    } else if (!(key in known)) {
      known[key] = bxPortalPairValue(pairs[i]);
    }
  }
  if ("winners" in p) {
    if (p.winners === true) {
      known.winners = "1";
    } else {
      delete known.winners;
      // The coupling law's contrapositive (rank ⇒ winners ⊢ ¬winners ⇒
      // ¬rank): a URL-carried rank must not survive winners:false and
      // silently re-add winners below. A rank set in the SAME patch is
      // applied AFTER this branch, so an explicit rank still wins.
      delete known.rank;
    }
  }
  if ("rank" in p) {
    if (p.rank === null) {
      delete known.rank;
    } else {
      known.rank = String(p.rank);
    }
  }
  if ("runId" in p) {
    if (p.runId === null) {
      delete known.run_id;
    } else {
      known.run_id = String(p.runId);
    }
  }
  if ("trade" in p) {
    if (p.trade === null) {
      delete known.trade;
    } else {
      known.trade = String(p.trade);
    }
  }
  if ("rank" in known) {
    known.winners = "1"; // rank present implies winners — the one coupling law
  }
  var out = foreign.slice();
  for (var k = 0; k < BX_PORTAL_KEYS.length; k++) {
    var name = BX_PORTAL_KEYS[k];
    if (name in known) {
      out.push(name + "=" + encodeURIComponent(known[name]));
    }
  }
  return out.length ? "?" + out.join("&") : "";
}

/**
 * The legacy /strategy deep-link contract — the PINNED expressions VERBATIM
 * (the exact source strings the writer→reader link-contract test asserts).
 *
 * @param {string|null|undefined} search - window.location.search shape.
 * @returns {{runId: string|null, rank: number, tradeIdx: number|null}}
 */
function bxParseStrategyParams(search) {
  var params = new URLSearchParams(
    String(search === undefined || search === null ? "" : search)
  );
  var state = { runId: null, rank: 1, tradeIdx: null };
  state.runId = params.get('run_id');
  state.rank = Math.max(1, parseInt(params.get('rank') || '1', 10) || 1);
  var tr = params.get('trade');
  state.tradeIdx = tr === null ? null : Math.max(0, parseInt(tr, 10) || 0);
  return state;
}

/**
 * The ONE parameterized /strategy href builder for the consolidated
 * surfaces — kills the inline copies (screener row-click default nav,
 * drawer open-full link, per-trade chart link). Exactly ONE pre-existing
 * inline copy survives: bx-oneclick.js's renderCombosRows "View" link
 * (encodeURIComponent on both components — identical output shape; its
 * unification is the ledgered follow-up DEC-COMBOS-TABLE-CONFIG-UNIFY-001).
 * Every component here rides encodeURIComponent so a hostile run_id never
 * survives raw into a URL.
 *
 * @param {string} runId - the run id (server-validated via is_safe_run_id).
 * @param {number|string} rank - the 1-based winner rank.
 * @param {number|string} [tradeIdx] - optional 0-based trade index.
 * @returns {string} '/strategy?run_id=…&rank=…[&trade=…]'.
 */
function bxStrategyHref(runId, rank, tradeIdx) {
  var href =
    '/strategy?run_id=' + encodeURIComponent(String(runId === undefined || runId === null ? "" : runId)) +
    "&rank=" + encodeURIComponent(String(rank));
  if (tradeIdx !== undefined && tradeIdx !== null) {
    href += "&trade=" + encodeURIComponent(String(tradeIdx));
  }
  return href;
}

/* Browser namespace (pure — safe on every page; no DOM is touched). */
if (typeof window !== "undefined") {
  window.BXPortalState = {
    PORTAL_KEYS: BX_PORTAL_KEYS,
    parse: bxPortalParse,
    serialize: bxPortalSerialize,
    parseStrategyParams: bxParseStrategyParams,
    strategyHref: bxStrategyHref
  };
}

/* Non-browser harness (the node golden) — the whole module is pure. */
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    BX_PORTAL_KEYS: BX_PORTAL_KEYS,
    bxPortalParse: bxPortalParse,
    bxPortalSerialize: bxPortalSerialize,
    bxParseStrategyParams: bxParseStrategyParams,
    bxStrategyHref: bxStrategyHref
  };
}
