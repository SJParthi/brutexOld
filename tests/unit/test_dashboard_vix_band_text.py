"""Node-executed goldens for the pure VIX-band text builders in ``bx-oneclick.js``.

[W3-R4] DEC-ONECLICK-TOP100-OBJECT-ROOT-001 (wave-3 fold). Fails-before: the
REAL engine writer emits ``vix_band`` as an OBJECT whose ``lo``/``hi`` are
``null`` for a null-VIX run (``top100_artifact._vix_band_payload`` — R31
honest NULL), but every band render site gated only on ``undefined``:

* the operator rank-1 card (``bx-oneclick.js`` ``vixBandText``) and the
  /strategy story card (an inline twin) rendered
  **"works when VIX between null–null"**;
* the /screener band column cell rendered **"null–null"**.

ONE pure builder pair now owns the copy (``bxVixBandText`` sentence +
``bxVixBandCellText`` compact cell, exported on ``window.BXOneclick`` for the
templates): a null/missing-bound band reads the honest
"VIX band not stamped for this run" (the house awaiting style), a PARTIAL
band (one real bound) states the bound it has and NAMES the missing one —
never a fabricated range, never "null" as prose.

This suite executes the REAL shipped ``bx-oneclick.js`` under node (the
bx-chart.js precedent — its browser IIFE self-guards on ``document`` so the
CommonJS exports are the pure primitives only). Skipped (visibly) where node
is absent; the CI node-presence guard lives in
``test_dashboard_oneclick_frontend.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from pathlib import Path
from typing import Any

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"
_ONECLICK_JS = _STATIC / "bx-oneclick.js"

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not installed")

#: The pinned honest copy (ONE constant in the JS — drift fails here).
_NOT_STAMPED = "VIX band not stamped for this run"

#: The REAL writer's UNSTAMPED band shape (``_vix_band_payload`` for a
#: null-VIX run — the exact object the reader passes through to every page).
_WRITER_UNSTAMPED_BAND = {
    "lo": None,
    "hi": None,
    "stamped": 0,
    "missing": 40,
    "win": None,
    "loss": None,
}


def _node_call(expr: str, *argv: str) -> Any:
    """Run the REAL shipped bx-oneclick.js under node and return the JSON result."""
    script = (
        f"const O = require({json.dumps(str(_ONECLICK_JS))});"
        "const ARGS = process.argv.slice(1).map((a) => JSON.parse(a));"
        f"console.log(JSON.stringify({expr}));"
    )
    proc = subprocess.run(  # nosec B603 — fixed argv, test-only
        ["node", "-e", script, *argv],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


class TestVixBandText:
    """bxVixBandText — the story-card sentence (operator + /strategy)."""

    def test_writer_unstamped_band_reads_the_honest_copy_never_null_null(self) -> None:
        out = _node_call("O.bxVixBandText(ARGS[0])", json.dumps(_WRITER_UNSTAMPED_BAND))
        assert out == _NOT_STAMPED
        assert "null" not in out

    def test_missing_lo_hi_keys_read_the_honest_copy(self) -> None:
        assert _node_call("O.bxVixBandText(ARGS[0])", json.dumps({})) == _NOT_STAMPED

    @pytest.mark.parametrize("band_js", ["null", "undefined", '"x"', "7"])
    def test_absent_or_non_object_band_reads_the_honest_copy(self, band_js: str) -> None:
        assert _node_call(f"O.bxVixBandText({band_js})") == _NOT_STAMPED

    def test_stamped_band_reads_the_range_sentence(self) -> None:
        out = _node_call("O.bxVixBandText(ARGS[0])", json.dumps({"lo": 12.5, "hi": 18.25}))
        assert out == "works when VIX between 12.5–18.25"

    def test_partial_lo_only_names_the_missing_upper_bound(self) -> None:
        out = _node_call("O.bxVixBandText(ARGS[0])", json.dumps({"lo": 12.5, "hi": None}))
        assert out == "works when VIX ≥ 12.5 (upper bound not stamped)"

    def test_partial_hi_only_names_the_missing_lower_bound(self) -> None:
        out = _node_call("O.bxVixBandText(ARGS[0])", json.dumps({"lo": None, "hi": 18.25}))
        assert out == "works when VIX ≤ 18.25 (lower bound not stamped)"

    def test_non_finite_bound_is_treated_as_not_stamped(self) -> None:
        """A NaN/Infinity bound (out-of-band junk) never renders as prose."""
        assert _node_call("O.bxVixBandText({lo: NaN, hi: Infinity})") == _NOT_STAMPED

    def test_constant_is_exported_for_the_templates(self) -> None:
        assert _node_call("O.BX_VIX_BAND_NOT_STAMPED") == _NOT_STAMPED


class TestVixBandCellText:
    """bxVixBandCellText — the compact /screener band-column cell."""

    def test_writer_unstamped_band_reads_not_stamped_never_null_null(self) -> None:
        out = _node_call("O.bxVixBandCellText(ARGS[0])", json.dumps(_WRITER_UNSTAMPED_BAND))
        assert out == "not stamped"

    def test_null_band_row_reads_not_stamped(self) -> None:
        """The fixture corpus's documented null-band row (R31)."""
        assert _node_call("O.bxVixBandCellText(null)") == "not stamped"

    def test_stamped_band_reads_the_compact_range(self) -> None:
        out = _node_call("O.bxVixBandCellText(ARGS[0])", json.dumps({"lo": 12.5, "hi": 18.25}))
        assert out == "12.5–18.25"

    def test_partial_bounds_stay_compact_and_named(self) -> None:
        assert (
            _node_call("O.bxVixBandCellText(ARGS[0])", json.dumps({"lo": 12.5, "hi": None}))
            == "≥ 12.5 (upper not stamped)"
        )
        assert (
            _node_call("O.bxVixBandCellText(ARGS[0])", json.dumps({"lo": None, "hi": 18.25}))
            == "≤ 18.25 (lower not stamped)"
        )
