"""Node goldens + wiring pins for the winner-instrument display (DEC-ONECLICK-INSTRUMENT-DISPLAY-001).

Fails-before (the expectation-audit finding): the engine writer has ALWAYS
emitted the additive per-entry ``instrument`` (``top100_artifact._entry_payload``
— the winner's OWN market from ``ResultRow.instrument``), but NO display
surface rendered it — the operator rank-1 card dropped it, the combos table's
Inst column hardcoded "—" with a stale "instrument is not a top100 row field"
comment, /strategy's identity strip omitted it, and /screener had no
instrument column or filter — so the operator could not tell WHICH index the
best strategy trades.

ONE pure builder now owns the copy (``bx-oneclick.js`` ``bxInstrumentText``,
exported on ``window.BXOneclick.instrumentText`` for the templates): a string
instrument renders verbatim; a pre-instrument artifact / null / non-string
value reads the honest "—" — never "undefined"/"null" as prose, never an
invented market (Golden Rule #1). The /screener column + filter live in the
config-driven ``BXScreener.COLUMNS`` / ``bxFilterRow`` (their node goldens sit
beside the other screener math in ``test_dashboard_screener_math.py``).

This suite executes the REAL shipped ``bx-oneclick.js`` under node (the
bx-chart.js precedent — the browser IIFE self-guards on ``document`` so the
CommonJS exports are the pure primitives only) and pins the template/JS
wiring by source string (honest envelope — the #1557 Playwright job does not
drive these pages). Skipped (visibly) where node is absent; the CI
node-presence guard lives in ``test_dashboard_oneclick_frontend.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from pathlib import Path
from typing import Any

import pytest

_DASHBOARD = Path(__file__).resolve().parents[2] / "brutex" / "dashboard"
_STATIC = _DASHBOARD / "static"
_TEMPLATES = _DASHBOARD / "templates"
_ONECLICK_JS = _STATIC / "bx-oneclick.js"

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not installed")


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


class TestInstrumentText:
    """bxInstrumentText — the ONE pure instrument cell/label builder."""

    def test_engine_instrument_strings_render_verbatim(self) -> None:
        for token in ("NSE-NIFTY", "NSE-BANKNIFTY", "BSE-SENSEX", "NIFTY"):
            assert _node_call("O.bxInstrumentText(ARGS[0])", json.dumps(token)) == token

    @pytest.mark.parametrize("value_js", ["null", "undefined", '""', "7", "{}", "[]", "true"])
    def test_absent_or_non_string_reads_the_honest_dash(self, value_js: str) -> None:
        """A pre-instrument artifact (no key ⇒ undefined) and every junk shape
        read "—" — never "undefined"/"null" prose, never an invented market."""
        assert _node_call(f"O.bxInstrumentText({value_js})") == "—"

    def test_helper_is_exported_for_the_templates(self) -> None:
        """strategy.html calls window.BXOneclick.instrumentText — the browser
        export and the node export must both carry the SAME function."""
        js = _ONECLICK_JS.read_text(encoding="utf-8")
        assert "instrumentText: bxInstrumentText," in js  # window.BXOneclick
        assert "bxInstrumentText: bxInstrumentText" in js  # node harness


class TestInstrumentWiring:
    """Source-string pins: every consumer surface reads the ONE builder."""

    def test_rank_one_card_sets_the_r1_instrument_hook(self) -> None:
        js = _ONECLICK_JS.read_text(encoding="utf-8")
        assert 'setOc("r1_instrument", bxInstrumentText(row.instrument));' in js

    def test_combos_inst_cell_renders_the_real_instrument(self) -> None:
        """The stale hardcoded "—" Inst cell (and its now-false comment) is
        KILLED — the cell renders the entry's instrument via the builder."""
        js = _ONECLICK_JS.read_text(encoding="utf-8")
        assert "td(bxInstrumentText(row.instrument)" in js
        assert "instrument is not a top100 row field" not in js  # killed token

    def test_v29_card_carries_the_instrument_hooks(self) -> None:
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        # The verdict meta strip AND the Returns panel both carry the hook.
        assert html.count('data-oc="r1_instrument"') >= 2

    def test_v29_inst_filter_offers_the_engine_tokens_not_display_names(self) -> None:
        """The combos fInst filter compares the Inst CELL text — its options
        must be the engine instrument tokens the cell now renders; the old
        v29-preview display names ("NIFTY 50") could never match."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        for token in ("NSE-NIFTY", "NSE-BANKNIFTY", "BSE-SENSEX"):
            assert f"<option>{token}</option>" in html
        assert "<option>NIFTY 50</option>" not in html  # killed preview token

    def test_strategy_identity_strip_reads_the_shared_builder(self) -> None:
        html = (_TEMPLATES / "strategy.html").read_text(encoding="utf-8")
        assert 'instrument <span id="stInstr" data-explain="instrument">—</span>' in html
        # The v29 drawer copy carries the same identity strip (portal parity).
        v29 = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert 'instrument <span id="stInstr" data-explain="instrument">—</span>' in v29
        # The fill line lives VERBATIM in the ONE shared strategy panel
        # module (DEC-OPERATOR-PAGE-CONSOLIDATE-001 — element lookup stays
        # document-global byId, so the pin survives character-for-character).
        panel = (_STATIC / "bx-strategy-panel.js").read_text(encoding="utf-8")
        assert (
            "byId('stInstr').textContent = window.BXOneclick.instrumentText(row.instrument);"
            in panel
        )

    def test_screener_filter_control_is_wired(self) -> None:
        html = (_TEMPLATES / "screener.html").read_text(encoding="utf-8")
        assert '<select id="scInstrument"><option value="">All</option></select>' in html
        # The v29 winners copy carries the same control (portal parity).
        v29 = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert '<select id="scInstrument"><option value="">All</option></select>' in v29
        # The filter wiring lives in the ONE shared screener panel module
        # (DEC-OPERATOR-PAGE-CONSOLIDATE-001 — moved verbatim).
        panel = (_STATIC / "bx-screener-panel.js").read_text(encoding="utf-8")
        assert "if (inst) { filters.instrument = inst; }" in panel
        # Options are harvested ONLY from rows carrying the additive key.
        assert "if (typeof inst === 'string' && inst !== '') { instSeen[inst] = true; }" in panel
        assert "'scInstrument'" in panel  # the input listener list

    def test_explain_table_carries_the_instrument_line(self) -> None:
        js = (_STATIC / "bx-explain.js").read_text(encoding="utf-8")
        assert "instrument:" in js
