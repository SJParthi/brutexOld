/* bx-format.js — the ONE shared display-number formatter (CLAUDE.md §11 COMMON).
 *
 * DEC-STORED-DATA-VALIDATION-001 (L3-a): the movers + /db templates formatted
 * 2dp display cells with IEEE `Number.prototype.toFixed(2)`, which uses
 * round-half-to-even ON THE BINARY VALUE — at an exact decimal half-boundary
 * it diverges from the repo's pinned display law (ROUND_HALF_UP, i.e. ties
 * away from zero — `preopen_report.py::_round2` / the DEC-SURFACE-TRIAL-001
 * #1154 half-up cells): (1.005).toFixed(2) === "1.00" while the pinned law
 * says "1.01".
 *
 * bxFixed2(n) mirrors Python `Decimal(str(n)).quantize(Decimal("0.01"),
 * rounding=ROUND_HALF_UP)` by operating on the SHORTEST-ROUND-TRIP DECIMAL
 * STRING of the number (JS `String(n)` and Python `str(float)` produce the
 * same shortest repr for the same IEEE double), never on the binary
 * expansion: the digit after the kept 2 decides (>= 5 rounds AWAY FROM ZERO —
 * a remainder of exactly one '5' digit is the tie, and any further nonzero
 * digits are strictly above the tie, so "first digit >= 5 → up" is exact).
 *
 * Honest envelope (Golden Rule #10): a value whose String() form is
 * exponent-notation (|n| < 1e-6 or >= 1e21) falls back to toFixed(2) — below
 * 1e-6 both agree on "0.00"; >= 1e21 is outside any real display cell's
 * domain. Non-finite values pass through as their String() form.
 */
"use strict";

/**
 * Format a number to exactly 2 decimals with ROUND_HALF_UP (ties away from
 * zero) semantics on its shortest decimal representation.
 *
 * @param {number} n - the full-precision payload value.
 * @returns {string} the 2-decimal display string (e.g. "1.01", "-0.05").
 */
function bxFixed2(n) {
  if (typeof n !== "number" || !Number.isFinite(n)) return String(n);
  const s = String(n);
  if (s.indexOf("e") !== -1 || s.indexOf("E") !== -1) return n.toFixed(2);
  const neg = s.charAt(0) === "-";
  const digits = neg ? s.slice(1) : s;
  const dot = digits.indexOf(".");
  const intPart = dot === -1 ? digits : digits.slice(0, dot);
  const fracPart = dot === -1 ? "" : digits.slice(dot + 1);
  const keptFrac = (fracPart + "00").slice(0, 2);
  // HALF_UP on the decimal string: the first dropped digit decides — '5'
  // alone is the exact tie (away from zero), anything above is strictly up.
  const roundUp = fracPart.length > 2 && fracPart.charCodeAt(2) >= 53; /* '5' */
  let scaled = BigInt(intPart + keptFrac) + (roundUp ? 1n : 0n);
  let out = scaled.toString();
  while (out.length < 3) out = "0" + out;
  const body = out.slice(0, -2) + "." + out.slice(-2);
  /* Mirror toFixed's sign behavior: a negative input that rounds to zero
     still displays "-0.00" ((-0.001).toFixed(2) === "-0.00"). */
  return neg ? "-" + body : body;
}

/* Export for any non-browser harness (the Python-side node golden test);
   browsers simply use the global. */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { bxFixed2 };
}
