"""The shared JS half-up 2dp formatter (DEC-STORED-DATA-VALIDATION-001, L3-a).

Fails-before (round-2 audit): ``movers.html`` + ``db.html``'s PCT columns
formatted display cells with IEEE ``Number.prototype.toFixed(2)``, which
diverges from the pinned ROUND_HALF_UP display law at exact half-boundaries —
``(1.005).toFixed(2) === "1.00"`` while the pinned law
(``preopen_report._round2`` / the DEC-SURFACE-TRIAL-001 #1154 half-up cells)
says ``"1.01"``.

The repo has NO executable-JS harness (an honest, known limit), so this suite
pins the fix in two layers:

* **String pins** — both templates load the ONE shared ``/static/bx-format.js``
  (CLAUDE.md §11 COMMON) BEFORE their inline scripts, and the diverging
  2dp display sites now call ``bxFixed2`` (``movers.html`` carries ZERO
  remaining ``toFixed(2)``; ``db.html``'s PCT_COLS line calls ``bxFixed2``).
* **Node-executed golden** — when ``node`` is available (it is on the GitHub
  CI runners), the REAL ``bx-format.js`` is executed against boundary values
  and every output is asserted equal to Python's
  ``Decimal(str(v)).quantize(Decimal("0.01"), ROUND_HALF_UP)`` — the exact
  semantics of ``preopen_report._round2``. Skipped (visibly) where node is
  absent — the string pins above still gate.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

_DASHBOARD = Path(__file__).resolve().parents[2] / "brutex" / "dashboard"
_STATIC_JS = _DASHBOARD / "static" / "bx-format.js"
_MOVERS = _DASHBOARD / "templates" / "movers.html"
_DB = _DASHBOARD / "templates" / "db.html"

#: The golden boundary values — every documented divergence class:
#: exact half-boundaries (both signs), the audit's '1.00500' case, IEEE
#: sub-half noise (0.1+0.2), integers, negatives rounding to zero, and a
#: representative non-boundary spread.
_GOLDEN_VALUES: tuple[float, ...] = (
    1.005,  # THE audit boundary: toFixed → "1.00", pinned law → "1.01"
    2.675,  # classic IEEE-vs-decimal boundary → "2.68"
    -1.005,  # ties away from zero on the negative side → "-1.01"
    0.125,  # → "0.13" (toFixed(2) gives "0.13" too — agreement pinned)
    0.135,  # → "0.14"
    0.1 + 0.2,  # 0.30000000000000004 → "0.30"
    5.0,  # integer → "5.00"
    0.0,  # → "0.00"
    -0.001,  # negative rounding to zero → "-0.00" (toFixed-compatible)
    1e-7,  # exponent-notation fallback → "0.00"
    99.994999,  # below the half → "99.99"
    99.995,  # exactly at the half → "100.00"
    -42.555,  # → "-42.56"
    12345.675,  # carries through the integer part → "12345.68"
)


def _python_half_up_2dp(value: float) -> str:
    """The pinned display law: ``preopen_report._round2`` semantics on str(float)."""
    quantized = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # Mirror the JS formatter's toFixed-compatible "-0.00" for a negative
    # input that rounds to zero (Decimal keeps the sign too).
    return str(quantized)


class TestSharedFormatterWiring:
    def test_static_formatter_exists_and_defines_bxfixed2(self) -> None:
        text = _STATIC_JS.read_text(encoding="utf-8")
        assert "function bxFixed2(" in text
        assert "ROUND_HALF_UP" in text  # the law is named at the definition

    @pytest.mark.parametrize("template", [_MOVERS, _DB], ids=["movers", "db"])
    def test_template_loads_shared_formatter_before_inline_script(self, template: Path) -> None:
        text = template.read_text(encoding="utf-8")
        include = text.index('<script src="/static/bx-format.js"></script>')
        first_inline = text.index("<script>\n")
        assert include < first_inline, (
            "bx-format.js must load (non-defer) BEFORE the inline script that "
            "calls bxFixed2 — a defer'd load could race the first render"
        )

    def test_movers_num_and_pct_sites_use_bxfixed2(self) -> None:
        text = _MOVERS.read_text(encoding="utf-8")
        assert 'else if (type === "num") v = bxFixed2(v);' in text
        assert '(n>0?"+":"") + bxFixed2(n) + "%"' in text
        # The IEEE 2dp rounder is GONE from movers.html entirely (num4/Greeks
        # 4dp display stays deliberately — not a 2dp money/pct cell).
        assert "toFixed(2)" not in text

    def test_db_pct_cols_use_bxfixed2(self) -> None:
        text = _DB.read_text(encoding="utf-8")
        assert "if (PCT_COLS.has(col)) return Number.isFinite(n) ? bxFixed2(n) : cell;" in text


class TestNodeExecutedGolden:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_bxfixed2_matches_python_half_up_on_boundaries(self) -> None:
        """Execute the REAL shipped JS against the golden boundary values."""
        script = (
            f"const {{ bxFixed2 }} = require({json.dumps(str(_STATIC_JS))});"
            "const values = JSON.parse(process.argv[1]);"
            "console.log(JSON.stringify(values.map(bxFixed2)));"
        )
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            ["node", "-e", script, json.dumps(list(_GOLDEN_VALUES))],
            capture_output=True,
            text=True,
            check=True,
        )
        js_results = json.loads(proc.stdout)
        expected = [_python_half_up_2dp(v) for v in _GOLDEN_VALUES]
        assert js_results == expected

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_the_audit_boundary_diverges_from_tofixed(self) -> None:
        """Teeth: prove bxFixed2 fixes the EXACT divergence the audit found."""
        script = "console.log((1.005).toFixed(2));"
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.stdout.strip() == "1.00"  # IEEE toFixed — the OLD behavior
        assert _python_half_up_2dp(1.005) == "1.01"  # the pinned law
