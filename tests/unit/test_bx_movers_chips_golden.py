"""Node-executed goldens for the pure /movers header-chip derivations.

DEC-MOVERS-HEADER-CHIPS-001. The /movers header chips (Put/Call Ratio ×2,
Expiry, Coverage) and the per-row Buildup pills derive their values in the
PURE ``static/bx-movers-chips.js`` module so they can be executed for real
under node (the ``bx-format.js`` / ``bx-movers-time.js`` / ``bx-verify.js``
golden precedent) — these tests run the REAL shipped module (which resolves
the REAL shipped ``bxFixed2`` via ``require("./bx-format.js")``), never a
copy.

The load-bearing pins:

* **Formatter reuse** — the chip ratio text rides the ONE shared ``bxFixed2``
  (ROUND_HALF_UP on the shortest decimal repr), so an exact half-boundary
  (1.005 → "1.01") renders exactly like every other 2dp cell; structurally
  pinned by the ABSENCE of any re-implemented rounding in the module.
* **Ratio-of-sums honesty** — ``bxPcrAggregate`` sums the RAW wire ints; a
  zero/absent call-side denominator yields a null ratio (an honest "—"),
  never Infinity, never a fabricated 0 (Golden Rule #1).
* **Buildup quadrants** — all four price×OI quadrants + the six
  null/zero/non-finite ABSTAIN arms (no four-quadrant claim on flat or
  missing input).
* **Expiry abstains** — a malformed ISO never becomes a wrong on-screen date.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from pathlib import Path
from typing import Any

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"
_CHIPS_JS = _STATIC / "bx-movers-chips.js"
_FORMAT_JS = _STATIC / "bx-format.js"


class TestModuleWiring:
    def test_module_exists_and_exports_the_primitives(self) -> None:
        text = _CHIPS_JS.read_text(encoding="utf-8")
        assert "function bxRatio2(" in text
        assert "function bxPcrAggregate(" in text
        assert "function bxCoverageChip(" in text
        assert "function bxExpiryChipText(" in text
        assert "function bxBuildup(" in text
        assert "module.exports = BXMoversChips;" in text

    def test_formatter_is_the_shared_bxfixed2_never_a_reimplementation(self) -> None:
        # The module resolves the ONE shared bxFixed2 (browser global or the
        # CommonJS sibling) and re-implements NO rounding of its own: no
        # BigInt digit math, no toFixed call — a re-implementation could
        # diverge from the rendered cells at a half-boundary.
        text = _CHIPS_JS.read_text(encoding="utf-8")
        assert 'require("./bx-format.js").bxFixed2' in text
        assert "BigInt(" not in text
        assert ".toFixed(" not in text

    def test_module_source_has_no_dom_or_network_surface(self) -> None:
        # Pure string/number math only — no fetch, no DOM sink (no innerHTML
        # can ever ride this module; the caller is textContent-only).
        text = _CHIPS_JS.read_text(encoding="utf-8")
        assert ".innerHTML" not in text
        assert "fetch(" not in text
        assert "document." not in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestNodeExecutedGolden:
    def _run_js(self, expr: str) -> Any:
        script = (
            f"const c = require({json.dumps(str(_CHIPS_JS))});console.log(JSON.stringify({expr}));"
        )
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

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

    # ── bxRatio2 ──
    def test_ratio_text_rides_the_shared_half_up_boundary(self) -> None:
        # 201/200 = 1.005 — THE audit boundary: IEEE toFixed says "1.00", the
        # shared ROUND_HALF_UP law (and therefore the chip) says "1.01".
        assert self._fixed2("1.005") == "1.01"
        out = self._run_js("c.bxRatio2(201, 200)")
        assert out == {"ratio": 1.005, "text": "1.01"}

    def test_ratio_zero_or_missing_denominator_is_null(self) -> None:
        assert self._run_js("c.bxRatio2(700, 0)") is None
        assert self._run_js("c.bxRatio2(700, null)") is None
        assert self._run_js("c.bxRatio2(700, undefined)") is None

    def test_ratio_negative_or_non_finite_inputs_are_null(self) -> None:
        assert self._run_js("c.bxRatio2(-1, 200)") is None
        assert self._run_js("c.bxRatio2(100, -200)") is None
        assert self._run_js("c.bxRatio2(0/0, 200)") is None
        assert self._run_js("c.bxRatio2(100, 1/0)") is None
        assert self._run_js("c.bxRatio2('100', 200)") is None

    def test_ratio_zero_numerator_over_real_denominator_is_exact_zero(self) -> None:
        # All-CE chain: puts 0 / calls real → an exact "0.00", never null (the
        # fold law: only a zero DENOMINATOR yields the sentinel).
        assert self._run_js("c.bxRatio2(0, 1000)") == {"ratio": 0, "text": "0.00"}

    # ── bxPcrAggregate ──
    _ROWS = json.dumps(
        [
            {
                "expiry": "2026-06-16",
                "put_oi": 300,
                "call_oi": 400,
                "put_volume": 48,
                "call_volume": 50,
                "legs_read": 100,
                "legs_total": 120,
                "oi_missing_legs": 20,
            },
            {
                "expiry": "2026-06-16",
                "put_oi": 90,
                "call_oi": 100,
                "put_volume": 48,
                "call_volume": 50,
                "legs_read": 65,
                "legs_total": 82,
                "oi_missing_legs": 7,
            },
        ]
    )

    def test_aggregate_sums_and_ratio_of_sums(self) -> None:
        out = self._run_js(f"c.bxPcrAggregate({self._ROWS})")
        assert out["chains"] == 2
        assert (out["putOi"], out["callOi"]) == (390, 500)
        assert (out["putVolume"], out["callVolume"]) == (96, 100)
        assert (out["legsRead"], out["legsTotal"], out["oiMissing"]) == (165, 202, 27)
        # RATIO OF SUMS — 390/500 = 0.78, 96/100 = 0.96 (the honest aggregate,
        # NOT the mean of the per-underlying ratios).
        assert out["oi"] == {"ratio": 0.78, "text": "0.78"}
        assert out["vol"] == {"ratio": 0.96, "text": "0.96"}

    def test_aggregate_zero_call_side_yields_null_ratio(self) -> None:
        rows = json.dumps([{"put_oi": 700, "call_oi": 0, "put_volume": 5, "call_volume": 0}])
        out = self._run_js(f"c.bxPcrAggregate({rows})")
        assert out["oi"] is None
        assert out["vol"] is None

    def test_aggregate_abstains_on_empty_or_non_array(self) -> None:
        assert self._run_js("c.bxPcrAggregate([])") is None
        assert self._run_js("c.bxPcrAggregate(null)") is None
        assert self._run_js("c.bxPcrAggregate(undefined)") is None
        assert self._run_js("c.bxPcrAggregate({})") is None

    def test_aggregate_skips_non_finite_cells_never_nan_poisons(self) -> None:
        rows = "[{put_oi: 100, call_oi: 0/0}, {put_oi: 10, call_oi: 200}, null]"
        out = self._run_js(f"c.bxPcrAggregate({rows})")
        assert (out["putOi"], out["callOi"]) == (110, 200)
        assert out["chains"] == 3

    # ── bxCoverageChip ──
    def test_coverage_full_is_not_amber_and_has_no_missing_clause(self) -> None:
        agg = self._run_js("c.bxCoverageChip({legsRead: 202, legsTotal: 202, oiMissing: 0})")
        assert agg == {"text": "202/202 contracts", "amber": False}

    def test_coverage_missing_oi_is_amber_with_the_clause(self) -> None:
        agg = self._run_js("c.bxCoverageChip({legsRead: 165, legsTotal: 202, oiMissing: 27})")
        assert agg == {"text": "165/202 contracts · 27 missing OI", "amber": True}

    def test_coverage_unread_only_is_amber_without_the_clause(self) -> None:
        agg = self._run_js("c.bxCoverageChip({legsRead: 165, legsTotal: 202, oiMissing: 0})")
        assert agg == {"text": "165/202 contracts", "amber": True}

    def test_coverage_null_aggregate_is_null(self) -> None:
        assert self._run_js("c.bxCoverageChip(null)") is None

    # ── bxExpiryChipText ──
    def test_expiry_unique_reads_words_and_tier(self) -> None:
        rows = '[{expiry: "2026-06-16"}, {expiry: "2026-06-16"}]'
        assert self._run_js(f"c.bxExpiryChipText({rows}, 'near')") == "16-Jun-2026 (near tier)"

    def test_expiry_varies_by_underlying(self) -> None:
        rows = '[{expiry: "2026-06-16"}, {expiry: "2026-06-18"}]'
        assert self._run_js(f"c.bxExpiryChipText({rows}, 'near')") == (
            "near tier (varies by underlying)"
        )

    def test_expiry_no_rows_is_tier_only(self) -> None:
        assert self._run_js("c.bxExpiryChipText([], 'far')") == "far tier"
        assert self._run_js("c.bxExpiryChipText(null, 'far')") == "far tier"

    def test_expiry_malformed_iso_abstains_to_tier_only(self) -> None:
        # Never a wrong on-screen date: junk / out-of-range parts abstain.
        for bad in ("junk", "2026-13-01", "2026-06-32", "2026-6-16", None):
            rows = json.dumps([{"expiry": bad}])
            assert self._run_js(f"c.bxExpiryChipText({rows}, 'near')") == "near tier", bad

    # ── bxBuildup — all four quadrants + the abstains ──
    def test_buildup_all_four_quadrants(self) -> None:
        assert self._run_js("c.bxBuildup(1.2, 500)") == {
            "label": "Long Buildup",
            "cls": "bu-long",
        }
        assert self._run_js("c.bxBuildup(-1.2, 500)") == {
            "label": "Short Buildup",
            "cls": "bu-short",
        }
        assert self._run_js("c.bxBuildup(1.2, -500)") == {
            "label": "Short Covering",
            "cls": "bu-cover",
        }
        assert self._run_js("c.bxBuildup(-1.2, -500)") == {
            "label": "Long Unwinding",
            "cls": "bu-unwind",
        }

    def test_buildup_abstains_on_missing_flat_or_non_finite_inputs(self) -> None:
        # The six abstain arms: null/flat/non-finite on EITHER side → null
        # (an honest "—" — no quadrant claim on flat or missing input).
        for expr in (
            "c.bxBuildup(null, 500)",
            "c.bxBuildup(1.2, null)",
            "c.bxBuildup(0, 500)",
            "c.bxBuildup(1.2, 0)",
            "c.bxBuildup(0/0, 500)",
            "c.bxBuildup(1.2, 1/0)",
        ):
            assert self._run_js(expr) is None, expr
