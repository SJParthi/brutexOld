"""Node-executed goldens for the pure /movers ↔ /db verify comparator.

DEC-MOVERS-DB-PROVENANCE-VERIFY-001. The /movers live-verify chip cross-checks
the DISPLAYED LTP/%CHNG cells of the visible top-K rows against an independent
``/api/v1/db/candles`` read of the same lake's 1-minute base. The comparison
lives in the PURE ``static/bx-verify.js`` module so it can be executed for real
under node (the ``bx-format.js`` / ``bx-movers-time.js`` golden precedent) —
these tests run the REAL shipped module (which resolves the REAL shipped
``bxFixed2`` via ``require("./bx-format.js")``), never a copy.

The load-bearing pins:

* **The string-``change_pct`` coercion** — the /db wire delivers ``change_pct``
  as a JSON STRING (``db_view._json_safe`` stringifies ``Decimal``); a
  comparator that fed the raw string to ``bxFixed2`` would pass it through
  unchanged (``bx-format.js`` line 34 string passthrough) and false-mismatch
  every full-precision value. Both sides MUST be Number-coerced first.
* **Formatter reuse** — the comparison formats both sides through the ONE
  shared ``bxFixed2`` (ROUND_HALF_UP), so an exact half-boundary can never
  false-mismatch against the rendered cell; structurally pinned by the
  absence of any re-implemented rounding in ``bx-verify.js``.
* **Honest verdicts** — a null / non-finite /db close is a TRUTHFUL mismatch
  (``db: "null"``), never a silent skip; an unverifiable displayed cell is a
  COUNTED skip, never a fake green (Golden Rule #1).
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from pathlib import Path
from typing import Any

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"
_VERIFY_JS = _STATIC / "bx-verify.js"
_FORMAT_JS = _STATIC / "bx-format.js"


class TestModuleWiring:
    def test_module_exists_and_exports_the_primitives(self) -> None:
        text = _VERIFY_JS.read_text(encoding="utf-8")
        assert "function bxNum(" in text
        assert "function bxParseInr(" in text
        assert "function bxDbProbeOk(" in text
        assert "function bxCutRow(" in text
        assert "function bxVerifyRow(" in text
        assert "module.exports = BXVerify;" in text

    def test_formatter_is_the_shared_bxfixed2_never_a_reimplementation(self) -> None:
        # The comparator resolves the ONE shared bxFixed2 (browser global or the
        # CommonJS sibling) and re-implements NO rounding of its own: no BigInt
        # digit math, no toFixed fallback — a re-implementation could diverge
        # from the rendered cell at a half-boundary and false-mismatch.
        text = _VERIFY_JS.read_text(encoding="utf-8")
        assert 'require("./bx-format.js").bxFixed2' in text
        assert "BigInt(" not in text
        assert ".toFixed(" not in text

    def test_module_source_has_no_dom_or_network_surface(self) -> None:
        # Pure string/number math only — no fetch, no DOM sink (the node golden
        # would not exercise either, so their absence keeps the executed teeth
        # honest — and no innerHTML can ever ride this module).
        text = _VERIFY_JS.read_text(encoding="utf-8")
        assert ".innerHTML" not in text
        assert "fetch(" not in text
        assert "document." not in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestNodeExecutedGolden:
    def _run_js(self, expr: str) -> object:
        script = (
            f"const v = require({json.dumps(str(_VERIFY_JS))});console.log(JSON.stringify({expr}));"
        )
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def _run_row(self, expr: str) -> dict[str, Any]:
        """Run a ``bxVerifyRow`` expression and return its typed dict result."""
        out = self._run_js(expr)
        assert isinstance(out, dict)
        return out

    def _fixed2(self, expr: str) -> object:
        script = (
            f"const f = require({json.dumps(str(_FORMAT_JS))});"
            f"console.log(JSON.stringify(f.bxFixed2({expr})));"
        )
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    # ── bxNum ──
    def test_bxnum_coerces_wire_forms(self) -> None:
        assert self._run_js("v.bxNum('1.2345')") == 1.2345
        assert self._run_js("v.bxNum(2.5)") == 2.5
        assert self._run_js("v.bxNum('')") is None
        assert self._run_js("v.bxNum(null)") is None
        assert self._run_js("v.bxNum(undefined)") is None
        assert self._run_js("v.bxNum('abc')") is None
        assert self._run_js("v.bxNum(0/0)") is None
        assert self._run_js("v.bxNum(1/0)") is None

    # ── bxParseInr ──
    def test_parse_inr_western_grouping_the_format_inr_law(self) -> None:
        # money.py:105 groups WESTERN ("₹1,234,567.89") — the movers wire form.
        assert self._run_js("v.bxParseInr('₹1,234,567.89')") == 1234567.89

    def test_parse_inr_lakh_grouping_is_grouping_agnostic(self) -> None:
        # The comma strip is grouping-agnostic — a lakh-grouped display cell
        # (the toLocaleString('en-IN') path for number-typed cells) parses to
        # the SAME number.
        assert self._run_js("v.bxParseInr('₹12,34,567.89')") == 1234567.89

    def test_parse_inr_strips_honesty_markers_and_signs(self) -> None:
        # The " *" (baseline-contiguity) / " ~" (strike-step) trailing markers
        # ride the displayed cell — the parse must shed them.
        assert self._run_js("v.bxParseInr('₹-0.05 *')") == -0.05
        assert self._run_js("v.bxParseInr('2.38 ~')") == 2.38
        assert self._run_js("v.bxParseInr('+461.30')") == 461.3
        assert self._run_js("v.bxParseInr('+1.99%')") == 1.99

    def test_parse_inr_placeholders_are_null(self) -> None:
        assert self._run_js("v.bxParseInr('—')") is None
        assert self._run_js("v.bxParseInr('?')") is None
        assert self._run_js("v.bxParseInr('')") is None
        assert self._run_js("v.bxParseInr(null)") is None

    # ── bxCutRow (rows arrive DESC — the desc=true day page) ──
    _ROWS = "[['2024-01-02 15:29:00',101],['2024-01-02 10:00:00',102],['2024-01-02 09:15:00',103]]"

    def test_cut_row_latest_null_time_is_the_first_desc_row(self) -> None:
        # Latest (movers j.time null) = the whole-day cut = the day's last
        # 1-minute bar = the FIRST row of the desc page.
        assert self._run_js(f"v.bxCutRow({self._ROWS}, 0, null)") == ["2024-01-02 15:29:00", 101]

    def test_cut_row_explicit_pick_is_first_row_at_or_before(self) -> None:
        # Mirrors the INCLUSIVE filter_to_ist_minute cut: first desc row whose
        # wall-clock minute is <= the pick.
        assert self._run_js(f"v.bxCutRow({self._ROWS}, 0, '10:00')") == [
            "2024-01-02 10:00:00",
            102,
        ]
        assert self._run_js(f"v.bxCutRow({self._ROWS}, 0, '09:59')") == [
            "2024-01-02 09:15:00",
            103,
        ]

    def test_cut_row_pick_before_first_bar_is_null(self) -> None:
        assert self._run_js(f"v.bxCutRow({self._ROWS}, 0, '09:00')") is None

    def test_cut_row_date_only_1day_timestamp_falls_to_latest_row(self) -> None:
        # A date-only timestamp ("YYYY-MM-DD", the 1day render) slices to ""
        # which compares <= any "HH:MM" — the daily row resolves to the latest
        # row. The PINNED, deliberate behavior (the driver always probes
        # interval=1minute; this is the belt).
        assert self._run_js("v.bxCutRow([['2024-01-02',9]], 0, '10:00')") == ["2024-01-02", 9]

    def test_cut_row_empty_rows_is_null(self) -> None:
        assert self._run_js("v.bxCutRow([], 0, null)") is None

    # ── bxDbProbeOk (the /db read-allowlist pre-check, round-2 refuter fold) ──
    def test_probe_ok_admits_every_real_read_allowlist_shape(self) -> None:
        # Mirrors db_view._validate_candle_coords: NSE/BSE × INDEX/CASH/FNO ×
        # _SYMBOL_RE — incl. the documented '&' (M&M, DEC-PREOPEN-AMPERSAND-001)
        # and literal-space (INDIA VIX, DEC-PREOPEN-SPACE-SYMBOL-001) admits.
        for exch, seg, sym in (
            ("NSE", "INDEX", "NIFTY"),
            ("BSE", "INDEX", "SENSEX"),
            ("NSE", "CASH", "M&M"),
            ("NSE", "INDEX", "INDIA VIX"),
            ("NSE", "FNO", "NSE-NIFTY-30Jan25-28500-CE"),
        ):
            expr = f"v.bxDbProbeOk({json.dumps(exch)}, {json.dumps(seg)}, {json.dumps(sym)})"
            assert self._run_js(expr) is True, (exch, seg, sym)

    def test_probe_ok_rejects_what_the_route_would_400(self) -> None:
        # A lake-LEGAL symbol outside the READ allowlist (";", "'", "=" — the
        # browser fixture's hostile-symbol shape), "..", a bad exchange, a bad
        # segment, and a non-string symbol are all pre-rejected — the verify
        # pass must never issue a probe that is guaranteed to 400.
        hostile = "EVIL&lt;img src=x onerror=window.__bx_xss=1&gt;' onmouseover='window.__bx_xss=1"
        for exch, seg, sym in (
            ("NSE", "INDEX", hostile),
            ("NSE", "INDEX", "A..B"),
            ("NSE", "INDEX", " LEADINGSPACE"),
            ("NSE", "INDEX", ""),
            ("", "INDEX", "NIFTY"),
            ("MCX", "INDEX", "NIFTY"),
            ("NSE", "", "NIFTY"),
            ("NSE", "COMMODITY", "NIFTY"),
            # prototype-chain probe: an inherited Object key is not a set hit
            ("hasOwnProperty", "INDEX", "NIFTY"),
            ("NSE", "constructor", "NIFTY"),
        ):
            expr = f"v.bxDbProbeOk({json.dumps(exch)}, {json.dumps(seg)}, {json.dumps(sym)})"
            assert self._run_js(expr) is False, (exch, seg, sym)
        assert self._run_js("v.bxDbProbeOk('NSE', 'INDEX', null)") is False
        assert self._run_js("v.bxDbProbeOk('NSE', 'INDEX', 42)") is False

    # ── bxVerifyRow ──
    def test_exact_match_both_cells(self) -> None:
        out = self._run_js(
            "v.bxVerifyRow({sym:'NIFTY', ltpStr:'₹23,622.90', pctStr:'1.99',"
            " checkPct:true, dbClose:23622.9, dbChangePct:'1.99'})"
        )
        assert out == {"mismatches": [], "checked": 2, "skipped": 0}

    def test_round_half_up_boundary_parity_both_sides(self) -> None:
        # 1.005 formats "1.01" under the shared ROUND_HALF_UP law on BOTH sides
        # — the exact boundary where IEEE toFixed diverges ("1.00").
        assert self._fixed2("1.005") == "1.01"
        out = self._run_js(
            "v.bxVerifyRow({sym:'X', ltpStr:'₹1.01', pctStr:'1.005',"
            " checkPct:true, dbClose:1.005, dbChangePct:'1.005'})"
        )
        assert out == {"mismatches": [], "checked": 2, "skipped": 0}

    def test_string_change_pct_number_coercion_the_d1_bug_pin(self) -> None:
        # The /db wire delivers change_pct as a STRING ("1.2345" — _json_safe
        # stringifies Decimal). A comparator that fed it raw to bxFixed2 would
        # get the string back unchanged ("1.2345" ≠ "1.23") → a guaranteed
        # false mismatch. Number-coercion on both sides makes it match.
        out = self._run_js(
            "v.bxVerifyRow({sym:'Z', ltpStr:'₹5.00', pctStr:'1.2345',"
            " checkPct:true, dbClose:5, dbChangePct:'1.2345'})"
        )
        assert out == {"mismatches": [], "checked": 2, "skipped": 0}

    def test_genuine_ltp_mismatch_names_cell_and_both_values(self) -> None:
        out = self._run_row(
            "v.bxVerifyRow({sym:'HDFC BANK', ltpStr:'₹100.00', pctStr:null,"
            " checkPct:true, dbClose:101.234, dbChangePct:null})"
        )
        assert out["mismatches"] == [
            {"sym": "HDFC BANK", "cell": "LTP", "shown": "100.00", "db": "101.23"}
        ]
        assert out["checked"] == 1  # the LTP compare
        assert out["skipped"] == 1  # the null pct

    def test_genuine_pct_mismatch_names_cell_and_both_values(self) -> None:
        out = self._run_row(
            "v.bxVerifyRow({sym:'NIFTY', ltpStr:'₹5.00', pctStr:'2.2822',"
            " checkPct:true, dbClose:5, dbChangePct:'0.2033'})"
        )
        assert out["mismatches"] == [
            {"sym": "NIFTY", "cell": "%CHNG", "shown": "2.28", "db": "0.20"}
        ]
        assert out["checked"] == 2
        assert out["skipped"] == 0

    def test_negative_zero_sign_law_matches_on_both_sides(self) -> None:
        # A tiny negative pct formats "-0.00" (the bxFixed2 sign law) — both
        # sides ride the same formatter, so the signs agree and never
        # false-mismatch.
        assert self._fixed2("-0.001") == "-0.00"
        out = self._run_js(
            "v.bxVerifyRow({sym:'S', ltpStr:'₹5.00', pctStr:'-0.001',"
            " checkPct:true, dbClose:5, dbChangePct:'-0.0004'})"
        )
        assert out == {"mismatches": [], "checked": 2, "skipped": 0}

    def test_non_finite_db_close_is_a_truthful_mismatch_not_a_crash(self) -> None:
        for bad in ("0/0", "null", "undefined"):
            out = self._run_row(
                f"v.bxVerifyRow({{sym:'N', ltpStr:'₹5.00', pctStr:null,"
                f" checkPct:true, dbClose:{bad}, dbChangePct:null}})"
            )
            assert out["mismatches"] == [{"sym": "N", "cell": "LTP", "shown": "5.00", "db": "null"}]

    def test_null_pct_is_a_counted_skip(self) -> None:
        out = self._run_js(
            "v.bxVerifyRow({sym:'P', ltpStr:'₹5.00', pctStr:null,"
            " checkPct:true, dbClose:5, dbChangePct:'1.0'})"
        )
        assert out == {"mismatches": [], "checked": 1, "skipped": 1}

    def test_check_pct_false_fno_skips_the_pct_cell(self) -> None:
        # F&O tabs verify LTP ONLY (the %CHNG baseline law is not proven
        # byte-equal there — DEC-MOVERS-VERIFY-FNO-PCT-001); even a wildly
        # different pct pair must NOT go red.
        out = self._run_js(
            "v.bxVerifyRow({sym:'F', ltpStr:'₹5.00', pctStr:'99.9',"
            " checkPct:false, dbClose:5, dbChangePct:'-99.9'})"
        )
        assert out == {"mismatches": [], "checked": 1, "skipped": 1}

    def test_db_null_change_pct_is_a_counted_skip_never_red(self) -> None:
        # /db has no baseline for that day (change_pct null) while movers has
        # one — a counted, disclosed skip; a red here would blame a baseline
        # the two sides derive differently.
        out = self._run_js(
            "v.bxVerifyRow({sym:'B', ltpStr:'₹5.00', pctStr:'1.0',"
            " checkPct:true, dbClose:5, dbChangePct:null})"
        )
        assert out == {"mismatches": [], "checked": 1, "skipped": 1}

    def test_unparseable_displayed_ltp_is_a_counted_skip(self) -> None:
        # Nothing displayed to verify ("—") — counted skip, never a fabricated
        # compare (Golden Rule #1).
        out = self._run_js(
            "v.bxVerifyRow({sym:'D', ltpStr:'—', pctStr:null,"
            " checkPct:false, dbClose:5, dbChangePct:null})"
        )
        assert out == {"mismatches": [], "checked": 0, "skipped": 2}

    def test_skipped_count_arithmetic_composes(self) -> None:
        # checked + skipped always equals the 2 candidate cells per row.
        for expr, checked, skipped in (
            (
                "{sym:'A', ltpStr:'₹1.00', pctStr:'1.0', checkPct:true, dbClose:1, dbChangePct:'1.0'}",
                2,
                0,
            ),
            (
                "{sym:'A', ltpStr:'₹1.00', pctStr:null, checkPct:true, dbClose:1, dbChangePct:null}",
                1,
                1,
            ),
            (
                "{sym:'A', ltpStr:'₹1.00', pctStr:'1.0', checkPct:false, dbClose:1, dbChangePct:'1.0'}",
                1,
                1,
            ),
            (
                "{sym:'A', ltpStr:'—', pctStr:null, checkPct:true, dbClose:1, dbChangePct:null}",
                0,
                2,
            ),
        ):
            out = self._run_row(f"v.bxVerifyRow({expr})")
            assert (out["checked"], out["skipped"]) == (checked, skipped), expr
            assert out["checked"] + out["skipped"] == 2, expr
