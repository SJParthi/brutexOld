/* bx-copy.js — the render-time plain-English copy catalog for the operator
 * page clean-view pass (DEC-OPERATOR-PAGE-CLEANVIEW-001), the display layer
 * over the DEC-OPERATOR-PAGE-REAL-001 artifact-contract strings.
 *
 * THE TWO LAWS:
 *   Law 1 — the wire is frozen. No byte of any /api/v1/* response changes;
 *   every rewording of a PAYLOAD-borne string happens HERE, at render time,
 *   through this ONE catalog. Backend modules are never touched.
 *   Law 2 — honesty is auditable, not hidden. Every remapped string keeps the
 *   VERBATIM server string reachable in the same element's title attribute,
 *   prefixed "server: ". A string the catalog cannot classify renders
 *   VERBATIM (fallback = pass-through, never suppression). Awaiting/empty is
 *   never zero; unknown is never green; fixture mode stays loud; the chain
 *   check's "assumed" caveat survives INLINE in reworded form.
 *
 * DOM DISCIPLINE: textContent-only — this module never builds markup and
 * carries zero string-to-DOM sinks. Callers keep CSS class derivation keyed
 * on the RAW payload token (oc-pill-<raw>, oc-verify-<raw>); the catalog
 * changes display TEXT only, never CSS keys. Callers may not upgrade or
 * downgrade a tier.
 *
 * EXEMPT-BY-DESIGN (rendered verbatim, never remapped): timeline event lines
 * ("<ts> <kind> <json>" — the search-coherence contract), the
 * verify funnel-reconcile arithmetic proof lines, pill.derived_from
 * (tooltip), chain stage words, verify check names.
 *
 * Honest envelope (Golden Rule #10): the catalog is a FIXED, hand-written
 * lookup (never generated text — R27's no-hallucination-in-UI law); the pure
 * core is node-golden-tested (tests/unit/test_dashboard_copy_catalog.py)
 * and the DOM sinks are pinned by source-string tests only (no headless JS
 * runner drives the pages).
 */
"use strict";

/** The tier vocabulary: "neutral" | "notice" | "alert" | null (pass-through). */

/* ── Exact token maps (kind-scoped; unknown token → raw verbatim) ────────── */

var BX_COPY_TOKENS = {
  /* manifest.pill.state — CSS class stays oc-pill-<raw> at the caller. */
  pill: {
    awaiting: { text: "no run yet", tier: "neutral" },
    pending: { text: "preparing results", tier: "neutral" },
    "artifacts partial": { text: "partial results", tier: "notice" },
    complete: { text: "complete", tier: "neutral" },
    suspect: { text: "needs review", tier: "alert" }
  },
  /* verify.checks[].state — chip class stays oc-verify-<raw> at the caller.
     "ready" is NEVER green (the operator-judged cap law). */
  verify: {
    pass: { text: "passed", tier: "neutral" },
    fail: { text: "FAILED", tier: "alert" },
    ready: { text: "your call", tier: "neutral" },
    awaiting: { text: "waiting on data", tier: "neutral" },
    not_wired: { text: "not available yet", tier: "neutral" }
  },
  /* trades.source — fixture stays LOUD (pairs with the badge law). */
  source: {
    results_store: { text: "real run data", tier: "neutral" },
    fixture: { text: "TEST DATA", tier: "notice" },
    none: { text: "—", tier: "neutral" }
  },
  /* trade-row mae/mfe granularity suffix (replaces " (1m)" / " (tf)"). */
  granularity: {
    "1m": { text: " · per-minute", tier: "neutral" },
    tf: { text: " · per-candle", tier: "neutral" }
  }
};

/* ── Exact detail strings (consulted before the ordered rules) ───────────── */

var BX_COPY_EXACT = {
  /* Portal-panel strings (the absorbed screener/strategy copy clean —
     DEC-OPERATOR-PAGE-CLEANVIEW-001 merge-up round): the panels' pure
     helpers keep these literals for their node goldens; the DOM render
     remaps them here. */
  "minute-path equity — the stored real series (R27)": {
    text: "equity curve — the stored minute-by-minute series",
    tier: "neutral"
  },
  "no trades yet — awaiting real run artifact": {
    text: "no trades yet — they appear after the first completed run",
    tier: "neutral"
  },
  "awaiting real run artifact — no lake root wired for trades": {
    text: "Trade detail isn't connected on this server yet.",
    tier: "neutral"
  },
  "invalid run_id/rank — nothing to read": {
    text: "That run/strategy reference isn't valid.",
    tier: "notice"
  },
  /* A REAL zero — a fact, not an error. */
  "0 winners stored for this run": {
    text: "the run finished with no surviving strategies",
    tier: "neutral"
  },
  "funnel reconciles to the digit": {
    text: "All counts add up exactly.",
    tier: "neutral"
  },
  "funnel does NOT reconcile — see the lines": {
    text: "Counts do NOT add up — see the arithmetic below.",
    tier: "alert"
  },
  "manifest funnel counts awaiting": {
    text: "Counts not available yet.",
    tier: "neutral"
  },
  "chain not verified yet — click Verify (algorithm assumed until P2 merges)": {
    text:
      "Not checked yet — press Replay to verify. Verification method is " +
      "provisional until the audit component ships.",
    tier: "neutral"
  },
};

/* ── Ordered pattern rules — most-specific first; $1/$2 = captures ───────── */

var BX_COPY_RULES = [
  {
    /* Health-hub FOLD pill ("OK — 3 ok · 2 not wired" — client-composed from
       the frozen fold headline): the "not wired" prose remaps in place. */
    re: /^((?:OK|AMBER|RED)\b.*?\d+) not wired\b(.*)$/,
    text: "$1 not available yet$2",
    tier: "neutral"
  },
  {
    /* Health-hub LANE headlines (DEC-OPERATOR-HEALTH-TRUTH-001 payload —
       the wire is frozen): every "not wired — <plain reason>" variant
       (doctor/mutation/nightly/backup …) remaps at render; the reason tail
       is already plain English and carries through verbatim. */
    re: /^not wired — (.*)$/,
    text: "Not set up on this machine yet — $1.",
    tier: "neutral"
  },
  {
    /* Health-hub lane DETAILS naming an unset BRUTEX_* env var: the setting
       name moves into title="server: …" (the fixture-badge law) — the note
       reads plain English. */
    re: /^BRUTEX_[A-Z_]+ is unset\b.*$/,
    text: "A required setting is missing on this machine — hover for the exact name.",
    tier: "neutral"
  },
  {
    re: /^awaiting real run artifact — top100 entry carries no results-depth reference/,
    text: "This run is from an older version — re-run the sweep to record trade detail.",
    tier: "neutral"
  },
  {
    re: /^awaiting real run artifact — results-depth sidecars absent for /,
    text: "Trade detail wasn't written for this strategy yet.",
    tier: "neutral"
  },
  {
    re: /^awaiting real run artifact — no leaderboard entry at rank (\d+)$/,
    text: "No stored winner at position $1.",
    tier: "neutral"
  },
  {
    re: /^awaiting real run artifact — events\.jsonl absent$/,
    text: "No activity log written for this run yet.",
    tier: "neutral"
  },
  {
    re: /^awaiting real run artifact — manifest\.json absent$/,
    text: "Run record not written yet.",
    tier: "neutral"
  },
  {
    re: /^awaiting real run artifact — top100\.json absent$/,
    text: "Winner list not written yet.",
    tier: "neutral"
  },
  {
    /* A run IS selected here — the fixture simply lacks that rank; the
       generic "waiting for a sweep run" wording would be false (review
       round 1). */
    re: /^awaiting real run artifact — no trades fixture for rank (\d+)$/,
    text: "No test-data trades stored for strategy #$1.",
    tier: "neutral"
  },
  {
    /* Generic — deliberately LAST of the awaiting family. */
    re: /^awaiting real run artifact\b/,
    text: "Waiting for a sweep run — nothing to show yet.",
    tier: "neutral"
  },
  {
    re: /^results-depth read failed: /,
    text: "Couldn't read the stored trade file — it may be damaged.",
    tier: "alert"
  },
  {
    re: /^top100 entry tuple ref .* is not three safe path segments$/,
    text: "This entry's trade reference is malformed — trade detail unavailable.",
    tier: "alert"
  },
  {
    re: /^limit clamped to (\d+) /,
    text: "showing the first $1 trades (server page limit)",
    tier: "neutral"
  },
  {
    re: /^trades fixture (?:unreadable\/corrupt|is not )/,
    text: "Test-data file is damaged.",
    tier: "alert"
  },
  {
    /* All THREE corrupt-top100 siblings of the SAME shared reader — the
       "unreadable/corrupt: <Exc>" arm was a catalog gap rendering filename
       jargon in the winner-list AND trades notes (review round 2; the
       manifest twin below was widened in round 1). */
    re: /^top100\.json (?:is not valid JSON|is not a JSON array|unreadable\/corrupt)/,
    text: "The winner list is damaged and couldn't be read.",
    tier: "alert"
  },
  {
    /* The trades partial twin of the top100 row rule (review round 1). */
    re: /^trade row (\d+) is not an object — rows kept up to it$/,
    text: "Part of the trade list couldn't be read — showing the readable entries.",
    tier: "notice"
  },
  {
    re: /^row (\d+) (?:is not an object|missing contract keys).*rows kept up to it$/,
    text: "Part of the winner list couldn't be read — showing the readable entries.",
    tier: "notice"
  },
  {
    /* All FOUR corrupt-manifest siblings (the "is not a JSON object" arm was
       a catalog gap rendering filename jargon — review round 1). */
    re: /^manifest\.json (?:is not valid JSON|is not a JSON object|missing contract keys|unreadable\/corrupt)/,
    text: "Run record damaged — couldn't be read.",
    tier: "alert"
  },
  {
    /* The chain caveat survives INLINE in reworded form (T-M1). */
    re: /^chain verified: (\d+) records \(algorithm assumed until P2 merges\)$/,
    text:
      "Activity log intact — $1 records checked. Verification method is " +
      "provisional until the audit component ships.",
    tier: "neutral"
  },
  {
    re: /^chain verified: (\d+) records \(bounded at (\d+)\)/,
    text:
      "Activity log intact for the first $2 records checked. Verification " +
      "method is provisional until the audit component ships.",
    tier: "neutral"
  },
  {
    /* The provisional caveat rides the ALERT verdicts too (review round 1 —
       under the ASSUMED algorithm a reported break could itself be a false
       alarm of the assumed algorithm; T-M1 covers EVERY verdict). */
    re: /^chain break at line (\d+)/,
    text:
      "Activity log BROKEN at record $1 — treat this run as unverified. " +
      "Verification method is provisional until the audit component ships.",
    tier: "alert"
  },
  {
    re: /^(?:unparseable record at line \d+|record at line \d+ carries no hash)/,
    text:
      "Activity log damaged partway through — treat this run as unverified. " +
      "Verification method is provisional until the audit component ships.",
    tier: "alert"
  },
  {
    re: /^events\.jsonl absent — nothing to verify/,
    text: "No activity log written for this run yet.",
    tier: "neutral"
  },
  {
    re: /^events\.jsonl unreadable: /,
    text:
      "Activity log file couldn't be read. Verification method is " +
      "provisional until the audit component ships.",
    tier: "alert"
  },
  {
    /* An empty log verifies NOTHING — never a pass look. */
    re: /^chain intact but 0 records — an empty events file verifies nothing/,
    text:
      "Activity log is empty — there's nothing to verify yet. Verification " +
      "method is provisional until the audit component ships.",
    tier: "neutral"
  },
  {
    re: /^not wired yet — lands at merge-integration/,
    text: "Not available yet — this check ships in a later update.",
    tier: "neutral"
  },
  {
    re: /^wired — operator eyeballs the chart vs /,
    text: "Ready — compare the chart against your broker and judge it yourself.",
    tier: "neutral"
  },
  {
    re: /^wired — the expand sums the trade pages/,
    text: "Ready — open the trades below and check the totals yourself.",
    tier: "neutral"
  },
  {
    re: /^awaiting trades — no results-depth sidecars/,
    text: "Waiting for this run's trade detail.",
    tier: "neutral"
  },
  {
    /* The ONE deterministic CascadeWireError for a lake never swept via
       sweep-all — the most likely outcome of pressing the Advanced
       "Re-verify latest run" button on a fresh lake. The raw string renders
       an absolute filesystem path + a backticked CLI command (review
       round 3); unknown cascade errors still pass through VERBATIM. */
    re: /^no cascade manifest at .+ — run `brutex sweep-all` first/,
    text: "No completed sweep to verify yet — run a sweep first (the Run Sweep button above).",
    tier: "neutral"
  }
];

/**
 * Fill $1/$2 placeholders from a regex match (pure).
 *
 * @param {string} template - display text carrying $1/$2 placeholders.
 * @param {Array} match - the RegExp exec result.
 * @returns {string} the filled display text.
 */
function bxCopyFill(template, match) {
  return template.replace(/\$([12])/g, function (_all, idx) {
    var got = match[Number(idx)];
    return got === undefined ? "" : String(got);
  });
}

/**
 * Classify one server string: exact map first, then the ordered rules, else
 * VERBATIM pass-through (Law 2 — never suppression).
 *
 * @param {string} raw - the verbatim server string.
 * @returns {{text: string, tier: (string|null), matched: boolean}} verdict.
 */
function bxCopyLookup(raw) {
  var s = raw === null || raw === undefined ? "" : String(raw);
  if (Object.prototype.hasOwnProperty.call(BX_COPY_EXACT, s)) {
    var hit = BX_COPY_EXACT[s];
    return { text: hit.text, tier: hit.tier, matched: true };
  }
  for (var i = 0; i < BX_COPY_RULES.length; i++) {
    var m = BX_COPY_RULES[i].re.exec(s);
    if (m) {
      return {
        text: bxCopyFill(BX_COPY_RULES[i].text, m),
        tier: BX_COPY_RULES[i].tier,
        matched: true
      };
    }
  }
  return { text: s, tier: null, matched: false };
}

/**
 * Render one server string into an element (textContent only). A remapped
 * string keeps the verbatim original in the element's title ("server: …" —
 * Law 2); an empty/absent raw uses opts.fallback when given.
 *
 * @param {Element|null} el - the target element (null = classify only).
 * @param {*} raw - the verbatim server string.
 * @param {{prefix: (string|undefined), fallback: (string|undefined)}=} opts
 * @returns {{text: string, tier: (string|null), matched: boolean}} verdict.
 */
function bxCopyApply(el, raw, opts) {
  var o = opts || {};
  var s = raw === null || raw === undefined ? "" : String(raw);
  var r;
  if (s === "" && typeof o.fallback === "string") {
    r = { text: o.fallback, tier: "neutral", matched: false };
  } else {
    r = bxCopyLookup(s);
  }
  if (el) {
    el.textContent = (o.prefix || "") + r.text;
    if (r.matched && s) {
      el.title = "server: " + s;
    } else if (typeof el.removeAttribute === "function") {
      // Not remapped ⇒ NO server attribution: clear any stale "server: …"
      // title a previous render left on this reused element (Law 2 — a
      // wrong audit trail is worse than none; review round 1).
      el.removeAttribute("title");
    }
  }
  return r;
}

/**
 * Exact-only token remap for one payload token kind. An unknown token comes
 * back VERBATIM with matched=false — the caller keeps its current styling
 * and its RAW-token CSS class key.
 *
 * @param {string} kind - "pill" | "verify" | "source" | "granularity".
 * @param {*} raw - the verbatim payload token.
 * @returns {{text: string, tier: (string|null), matched: boolean}} verdict.
 */
function bxCopyToken(kind, raw) {
  var s = raw === null || raw === undefined ? "" : String(raw);
  var map = Object.prototype.hasOwnProperty.call(BX_COPY_TOKENS, kind)
    ? BX_COPY_TOKENS[kind]
    : null;
  if (map && Object.prototype.hasOwnProperty.call(map, s)) {
    return { text: map[s].text, tier: map[s].tier, matched: true };
  }
  return { text: s, tier: null, matched: false };
}

/**
 * Every static display string the catalog can emit (token map texts + exact
 * map texts + rule templates, placeholders included) — the mechanical
 * anti-jargon pin iterates THIS list so no entry can hide.
 *
 * @returns {string[]} all display texts.
 */
function bxCopyDisplayTexts() {
  var out = [];
  var kind;
  var key;
  for (kind in BX_COPY_TOKENS) {
    if (Object.prototype.hasOwnProperty.call(BX_COPY_TOKENS, kind)) {
      for (key in BX_COPY_TOKENS[kind]) {
        if (Object.prototype.hasOwnProperty.call(BX_COPY_TOKENS[kind], key)) {
          out.push(BX_COPY_TOKENS[kind][key].text);
        }
      }
    }
  }
  for (key in BX_COPY_EXACT) {
    if (Object.prototype.hasOwnProperty.call(BX_COPY_EXACT, key)) {
      out.push(BX_COPY_EXACT[key].text);
    }
  }
  for (var i = 0; i < BX_COPY_RULES.length; i++) {
    out.push(BX_COPY_RULES[i].text);
  }
  return out;
}

var BXCopy = {
  lookup: bxCopyLookup,
  apply: bxCopyApply,
  token: bxCopyToken,
  displayTexts: bxCopyDisplayTexts,
  TOKENS: BX_COPY_TOKENS
};

/* Browsers get the global; a non-browser harness (the node golden) gets the
   CommonJS export (mirrors bx-explain.js / bx-format.js). */
if (typeof window !== "undefined") {
  window.BXCopy = BXCopy;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = BXCopy;
}
