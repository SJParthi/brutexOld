/* bx-explain.js — the FIXED hover-line phrase table + trade arithmetic +
 * Beginner/Pro mode helpers (DEC-OPERATOR-PAGE-REAL-001, CLAUDE.md §11 COMMON).
 *
 * R36: "hover ANY number → one plain-English line". R27 no-hallucination-in-
 * UI: every line below is a FIXED, hand-written LOOKUP (never generated
 * text); an unknown key returns "" — the UI shows nothing rather than an
 * invented sentence. The per-CONDITION plain-English phrase table (R33) is
 * P3's zone — bxModeText appends the honest "(plain-English phrasing lands
 * with the P3 phrase table)" note until it merges.
 *
 * Readability bar (amended R35 — the ESW playbook): one sentence per idea,
 * everyday anchors, role-framing ("the worst pain along the way"), no
 * compound nesting — a beginner scans one line and understands.
 *
 * Money law (DEC-MONEY-2DP-001): bxPaisaToRupees converts int64 paisa to an
 * EXACT 2dp rupee string with pure integer/string arithmetic (paisa IS the
 * 2dp grid — no float, no rounding step, no bxFixed2 needed on this path).
 * Statistical metrics (Sharpe/DSR/p-values) are NEVER rounded here.
 *
 * Honest envelope (Golden Rule #10): the pure primitives (bxExplain /
 * bxTradeArithmetic / bxPaisaToRupees / bxModeText) are node-golden-tested;
 * the DOM helpers (bxApplyMode / bxBindExplainHover) are browser-only and
 * are pinned by SOURCE-STRING tests (the tests/browser Playwright job —
 * #1557 — does not drive the oneclick pages). DOM
 * writes are textContent/classList only — zero string-to-DOM sinks.
 */
"use strict";

/**
 * The awaiting-banner wording — ONE constant so the substring pin and the
 * UI cannot drift (C-NIT-3). Matches the adapter's "awaiting real run
 * artifact" detail vocabulary.
 *
 * @type {string}
 */
var BX_AWAITING_TEXT =
  "awaiting real run artifact — run a sweep from the Run controls; " +
  "this page never shows invented numbers";

/**
 * The fixed metric → one-plain-English-line table (R36 hover lines; R27: a
 * LOOKUP, never generated text). Keys cover every BX_SCREENER_COLUMNS
 * explain key + the trade-row fields + the operator-page funnel/KPI hooks.
 *
 * @type {Object<string, string>}
 */
var BX_EXPLAIN = {
  rank: "This strategy's position on the leaderboard — #1 scored best under the ranking rules.",
  conditions: "The exact market conditions this strategy waits for before it takes a trade.",
  tf: "The candle size the strategy watches — 15m means each candle covers 15 minutes.",
  direction: "long bets the market goes up; short bets it goes down.",
  arm: "The stop-loss / take-profit pair this strategy uses to exit its trades.",
  net_pnl_paisa: "Every trade's profit and loss added up, after costs (shown in rupees).",
  sharpe: "Reward earned per unit of risk taken — higher means steadier gains for the same bumps.",
  profit_factor: "Rupees won for every rupee lost — above 1 means winners outweigh losers.",
  win_rate: "How often trades ended in profit — 0.61 means 61 of every 100 trades won.",
  max_dd_paisa: "The deepest fall from a profit peak — the worst pain along the way (rupees).",
  trades: "How many trades this strategy took across the whole test window.",
  dsr: "Deflated Sharpe — the odds the edge is real after counting how many combos were tried.",
  cpcv_pass_pct: "How many cross-validation slices this strategy stayed profitable in (percent).",
  vix_band: "The market-fear range (India VIX) where this strategy historically made its money.",
  family_id: "Strategies that traded almost identically are grouped — this names that family.",
  mask_hex: "The strategy's machine ID — its exact condition bits, written in hex.",
  entry_ts: "The minute this trade was entered (exchange time).",
  entry_px: "The price the trade entered at.",
  exit_ts: "The minute this trade was exited (exchange time).",
  exit_px: "The price the trade exited at.",
  lots: "How many lots this trade carried.",
  exit_reason: "Why the trade closed — target hit, stop hit, signal flipped, or the 15:20 forced exit.",
  pnl_paisa: "This trade's profit or loss (shown in rupees).",
  costs_paisa: "Charges applied to this trade — Track 1 is signal-only, so costs are zero.",
  mae: "The worst the trade went AGAINST you before it closed (adverse excursion).",
  mfe: "The best the trade went FOR you before it closed (favorable excursion).",
  vix_entry: "India VIX (market fear gauge) when the trade entered — blank means no VIX bar stored.",
  vix_exit: "India VIX when the trade exited — blank means no VIX bar stored.",
  enumerated: "Every strategy combination the run planned to look at.",
  tested: "Combinations actually tested against the data.",
  skipped: "Combinations skipped because an identical run already computed them (dedup).",
  equity_curve: "Running total of trade profits over time — per-trade closes, not the minute path."
};

/**
 * Look up the fixed plain-English line for a metric key.
 *
 * @param {string} key - the explain key (e.g. "sharpe").
 * @returns {string} the one-line explanation, or "" for an unknown key
 *          (never invented text — R27).
 */
function bxExplain(key) {
  var line = BX_EXPLAIN[key];
  return typeof line === "string" ? line : "";
}

/**
 * EXACT paisa → rupee 2dp string (pure integer/string arithmetic — paisa is
 * already the 2dp grid, so no rounding ever happens on this path).
 *
 * @param {number} paisa - an int64-safe integer paisa amount.
 * @returns {string|null} e.g. 123456 -> "1234.56", -5 -> "-0.05"; null for
 *          a non-safe-integer input (the caller renders "?" — never a
 *          fabricated number).
 */
function bxPaisaToRupees(paisa) {
  if (!Number.isSafeInteger(paisa)) {
    return null;
  }
  var neg = paisa < 0;
  var abs = neg ? -paisa : paisa;
  var rupees = Math.floor(abs / 100);
  var frac = String(abs % 100);
  while (frac.length < 2) {
    frac = "0" + frac;
  }
  return (neg ? "-" : "") + String(rupees) + "." + frac;
}

/**
 * The R44 check-#4 line-by-line arithmetic: Σ trade P&L − Σ costs = net,
 * decomposed in INTEGER paisa (never float accumulation). The claim line
 * SCOPES itself to the rows given ("sum of the N trades loaded below" —
 * C-M4b), so a partially-loaded list can never read as a whole-run claim.
 *
 * @param {object[]} rows - contract trade rows ({pnl_paisa, costs_paisa}).
 * @returns {object} {ok, rows_counted, invalid_rows, gross_pnl_paisa,
 *          total_costs_paisa, net_paisa, lines: string[]}.
 */
function bxTradeArithmetic(rows) {
  var gross = 0;
  var costs = 0;
  var counted = 0;
  var invalid = 0;
  for (var i = 0; i < (rows ? rows.length : 0); i++) {
    var p = rows[i].pnl_paisa;
    var c = rows[i].costs_paisa;
    if (!Number.isSafeInteger(p) || !Number.isSafeInteger(c)) {
      invalid += 1; // counted, never coerced (R27)
      continue;
    }
    gross += p;
    costs += c;
    counted += 1;
  }
  var net = gross - costs;
  var lines = [
    "Σ trade P&L = ₹" + bxPaisaToRupees(gross) + " across " + counted + " trades",
    "− Σ costs = ₹" + bxPaisaToRupees(costs),
    "= net ₹" + bxPaisaToRupees(net) + " — sum of the " + counted + " trades loaded below"
  ];
  if (invalid > 0) {
    lines.push(invalid + " row(s) had non-integer paisa values and were EXCLUDED from the sum");
  }
  return {
    ok: invalid === 0,
    rows_counted: counted,
    invalid_rows: invalid,
    gross_pnl_paisa: gross,
    total_costs_paisa: costs,
    net_paisa: net,
    lines: lines
  };
}

/**
 * Pick the display text for the active mode (R33-toggle). Beginner mode
 * renders the artifact-provided plain string IF present; otherwise the
 * technical label + the honest "(plain-English phrasing lands with the P3
 * phrase table)" note — NEVER an invented sentence (R33-table is P3's).
 *
 * @param {string} mode - "beginner" | "pro".
 * @param {string} techText - the technical label (always present).
 * @param {string|null} plainText - the artifact-provided plain string.
 * @returns {string} the text to render.
 */
function bxModeText(mode, techText, plainText) {
  if (mode !== "beginner") {
    return String(techText);
  }
  if (typeof plainText === "string" && plainText.length > 0) {
    return plainText;
  }
  return String(techText) + " (plain-English phrasing lands with the P3 phrase table)";
}

/* ── Browser-only DOM helpers (pinned by source-string tests). ── */

/** localStorage key for the persisted display mode. */
var BX_MODE_STORAGE_KEY = "bxDisplayMode";

/**
 * Apply the Beginner/Pro mode to a root element — the ONE shared mechanism
 * (C-MINOR-5): reuses v29's existing `.jargon` / `.plain-eng` span
 * convention via the `share-mode` / `operator-mode` classes (CSS owns the
 * show/hide), and persists the choice to localStorage.
 *
 * @param {Element} root - the element carrying the mode classes (body).
 * @param {string} mode - "beginner" | "pro".
 * @returns {undefined} nothing.
 */
function bxApplyMode(root, mode) {
  var beginner = mode === "beginner";
  root.classList.toggle("share-mode", beginner); // .plain-eng visible
  root.classList.toggle("operator-mode", !beginner); // .jargon visible
  try {
    localStorage.setItem(BX_MODE_STORAGE_KEY, beginner ? "beginner" : "pro");
  } catch (e) {
    /* private-mode storage denial: the toggle still works for this page view */
  }
}

/**
 * The persisted display mode ("pro" default — the operator page's native
 * audience; a stored "beginner" survives reloads).
 *
 * @returns {string} "beginner" | "pro".
 */
function bxStoredMode() {
  try {
    return localStorage.getItem(BX_MODE_STORAGE_KEY) === "beginner" ? "beginner" : "pro";
  } catch (e) {
    return "pro";
  }
}

/**
 * ONE delegated hover binder for every `[data-explain]` node under root
 * (R36 hover lines; C-MINOR-4 — the shared binder used by the strategy
 * page, the screener columns and the operator-page funnel/KPI nodes).
 * Writes the fixed lookup line into the tip via textContent ONLY, plus the
 * native title attribute; unknown keys hide the tip (never invented text).
 *
 * @param {Element} root - the delegation root.
 * @param {Element} tip - the positioned tip element (e.g. #bxTip).
 * @returns {undefined} nothing.
 */
function bxBindExplainHover(root, tip) {
  root.addEventListener("mouseover", function (ev) {
    var t = ev.target && ev.target.closest ? ev.target.closest("[data-explain]") : null;
    if (!t) {
      return;
    }
    var line = bxExplain(t.getAttribute("data-explain"));
    if (!line) {
      tip.hidden = true;
      return;
    }
    if (!t.getAttribute("title")) {
      t.setAttribute("title", line); // native tooltip too (fixed lookup text)
    }
    tip.textContent = line;
    tip.hidden = false;
    var r = t.getBoundingClientRect();
    tip.style.left = Math.round(r.left + window.scrollX) + "px";
    tip.style.top = Math.round(r.bottom + window.scrollY + 6) + "px";
  });
  root.addEventListener("mouseout", function (ev) {
    var t = ev.target && ev.target.closest ? ev.target.closest("[data-explain]") : null;
    if (t) {
      tip.hidden = true;
    }
  });
}

var BXExplain = {
  AWAITING_TEXT: BX_AWAITING_TEXT,
  MODE_STORAGE_KEY: BX_MODE_STORAGE_KEY,
  TABLE: BX_EXPLAIN,
  explain: bxExplain,
  paisaToRupees: bxPaisaToRupees,
  tradeArithmetic: bxTradeArithmetic,
  modeText: bxModeText,
  applyMode: bxApplyMode,
  storedMode: bxStoredMode,
  bindExplainHover: bxBindExplainHover
};

/* Browsers get the global; a non-browser harness (the node golden) gets the
   CommonJS export (mirrors bx-format.js / bx-movers-time.js). */
if (typeof window !== "undefined") {
  window.BXExplain = BXExplain;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = BXExplain;
}
