"""Node-executed goldens for the fixed explain/arithmetic core in ``bx-explain.js`` (DEC-OPERATOR-PAGE-REAL-001).

Fails-before: the R36 "hover ANY number → one plain-English line" surface and
the R44 check-#4 line-by-line trade arithmetic did not exist; a naive build
would GENERATE text (violating R27 no-hallucination-in-UI) or float-accumulate
the paisa sums. The pins below prove: the table is a FIXED lookup (unknown
key → "" — never invented text), every screener column's explain key has an
entry, the arithmetic is integer-paisa exact vs a Python int oracle, and the
paisa→rupee conversion is pure string/integer math (no rounding step exists).

The repo has NO executable-JS harness, so this suite executes the REAL
shipped ``bx-explain.js`` (and ``bx-screener.js`` for the column keys) under
node — the bx-format.js precedent. Skipped (visibly) where node is absent;
the CI node-presence guard lives in ``test_dashboard_oneclick_frontend.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from pathlib import Path
from typing import Any

import pytest

from tests.golden.generate_oneclick_fixture import RUN_ID, TRADE_ROW_COUNT

_STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"
_EXPLAIN_JS = _STATIC / "bx-explain.js"
_SCREENER_JS = _STATIC / "bx-screener.js"
_TRADES_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "oneclick"
    / "sweep_runs"
    / RUN_ID
    / "trades"
    / "rank-0001.json"
)

#: The awaiting-banner wording (C-NIT-3) — ONE constant in bx-explain.js;
#: this Python pin + the JS constant + the v29 template default must stay
#: byte-identical (plain-English since DEC-OPERATOR-PAGE-CLEANVIEW-001).
_EXPECTED_AWAITING_TEXT = (
    "No completed run yet — press Run Sweep below. Every number on this "
    "page comes from a real run — nothing is ever invented."
)

#: The while-a-sweep-RUNS variant (review round 2 — the fixed wording above
#: told the operator to press Run Sweep for the whole first-run window).
_EXPECTED_AWAITING_RUNNING_TEXT = (
    "First sweep is running — results appear here the moment it completes. "
    "Every number on this page comes from a real run — nothing is ever invented."
)

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not installed")


def _node_call(expr: str, *argv: str) -> Any:
    """Run the REAL shipped bx-explain.js (+ bx-screener.js) under node."""
    script = (
        f"const E = require({json.dumps(str(_EXPLAIN_JS))});"
        f"const S = require({json.dumps(str(_SCREENER_JS))});"
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


class TestExplainLookup:
    """bxExplain — a FIXED lookup, never generated text (R27)."""

    def test_unknown_key_returns_the_empty_string_never_invented_text(self) -> None:
        out = _node_call(
            '["nope", "", null].map((k) => E.explain(k))',
        )
        assert out == ["", "", ""]

    def test_every_screener_column_explain_key_has_a_nonempty_line(self) -> None:
        """The R36 hover contract: no screener column can hover to nothing."""
        out = _node_call(
            "S.COLUMNS.map((c) => ({key: c.explain, line: E.explain(c.explain)}))",
        )
        missing = [entry["key"] for entry in out if not entry["line"]]
        assert missing == []
        assert len(out) >= 16  # the full column set is wired

    def test_every_contract_trade_row_field_has_a_line(self) -> None:
        keys = [
            "entry_ts",
            "entry_px",
            "exit_ts",
            "exit_px",
            "direction",
            "lots",
            "exit_reason",
            "pnl_paisa",
            "costs_paisa",
            "mae",
            "mfe",
            "vix_entry",
            "vix_exit",
        ]
        out = _node_call("ARGS[0].map((k) => E.explain(k))", json.dumps(keys))
        assert all(isinstance(line, str) and line for line in out)

    def test_awaiting_text_constant_matches_the_pinned_wording(self) -> None:
        """C-NIT-3: the banner wording lives in ONE constant — pinned verbatim."""
        out = _node_call("E.AWAITING_TEXT")
        assert out == _EXPECTED_AWAITING_TEXT

    def test_awaiting_running_text_constant_matches_the_pinned_wording(self) -> None:
        """Review round 2: the no-completed-run banner while a sweep RUNS —
        never a 'press Run Sweep' call-to-action the running sweep refuses."""
        out = _node_call("E.AWAITING_RUNNING_TEXT")
        assert out == _EXPECTED_AWAITING_RUNNING_TEXT


class TestPaisaToRupees:
    """bxPaisaToRupees — exact integer/string conversion (DEC-MONEY-2DP-001)."""

    @pytest.mark.parametrize(
        ("paisa", "expected"),
        [
            (123456, "1234.56"),
            (-5, "-0.05"),
            (0, "0.00"),
            (100, "1.00"),
            (-123456789, "-1234567.89"),
            (9, "0.09"),
            (2**53 - 1, "90071992547409.91"),  # the safe-integer ceiling, exact
        ],
    )
    def test_exact_conversion(self, paisa: int, expected: str) -> None:
        # The value rides inside a JSON array: a bare negative argv (e.g.
        # "-5") would be eaten by node's own option parser.
        out = _node_call("E.paisaToRupees(ARGS[0][0])", json.dumps([paisa]))
        assert out == expected

    def test_non_integer_input_returns_null_never_a_fabricated_number(self) -> None:
        out = _node_call("[1.5, NaN, Infinity].map((v) => E.paisaToRupees(v))")
        assert out == [None, None, None]


class TestTradeArithmetic:
    """bxTradeArithmetic — the R44 check-#4 decomposition vs a Python int oracle."""

    def test_fixture_trades_sums_match_the_python_int_oracle(self) -> None:
        payload = json.loads(_TRADES_FIXTURE.read_text(encoding="utf-8"))
        rows: list[dict[str, Any]] = payload["rows"]
        assert len(rows) == TRADE_ROW_COUNT
        out = _node_call("E.tradeArithmetic(ARGS[0])", json.dumps(rows))
        gross = sum(int(r["pnl_paisa"]) for r in rows)
        costs = sum(int(r["costs_paisa"]) for r in rows)
        assert out["gross_pnl_paisa"] == gross
        assert out["total_costs_paisa"] == costs
        assert out["net_paisa"] == gross - costs
        assert out["rows_counted"] == TRADE_ROW_COUNT
        assert out["invalid_rows"] == 0
        assert out["ok"] is True

    def test_claim_line_scopes_itself_to_the_loaded_rows(self) -> None:
        """C-M4b: the claim reads "sum of the N trades loaded below" — never
        an unreconcilable whole-run claim over a partial page."""
        rows = [{"pnl_paisa": 150, "costs_paisa": 50}, {"pnl_paisa": -30, "costs_paisa": 0}]
        out = _node_call("E.tradeArithmetic(ARGS[0])", json.dumps(rows))
        assert out["lines"] == [
            "Σ trade P&L = ₹1.20 across 2 trades",
            "− Σ costs = ₹0.50",
            "= net ₹0.70 — sum of the 2 trades loaded below",
        ]

    def test_invalid_rows_are_excluded_and_named_in_a_loud_line(self) -> None:
        rows = [{"pnl_paisa": 100, "costs_paisa": 0}, {"pnl_paisa": 0.5, "costs_paisa": 0}]
        out = _node_call("E.tradeArithmetic(ARGS[0])", json.dumps(rows))
        assert out["ok"] is False
        assert out["invalid_rows"] == 1
        assert out["rows_counted"] == 1
        assert out["lines"][-1] == (
            "1 row(s) had non-integer paisa values and were EXCLUDED from the sum"
        )

    def test_empty_rows_is_an_honest_zero_decomposition(self) -> None:
        out = _node_call("E.tradeArithmetic(ARGS[0])", json.dumps([]))
        assert out["net_paisa"] == 0
        assert out["rows_counted"] == 0
        assert "sum of the 0 trades loaded below" in out["lines"][2]

    def test_null_costs_rows_are_counted_at_zero_with_a_caveat_line(self) -> None:
        """[W2-4] DEC-ONECLICK-TOP100-OBJECT-ROOT-001: Ruling-18 NULL-BY-DESIGN
        ``costs_paisa: null`` previously read "net ₹0.00 across 0 trades" +
        a mislabeled non-integer line over REAL production rows. Null costs
        now count at ₹0 with their OWN honest caveat; ok stays True (a
        by-design null is not a defect)."""
        rows = [
            {"pnl_paisa": 150, "costs_paisa": None},
            {"pnl_paisa": -30, "costs_paisa": None},
            {"pnl_paisa": 80, "costs_paisa": None},
        ]
        out = _node_call("E.tradeArithmetic(ARGS[0])", json.dumps(rows))
        assert out["ok"] is True
        assert out["invalid_rows"] == 0
        assert out["null_cost_rows"] == 3
        assert out["rows_counted"] == 3
        assert out["gross_pnl_paisa"] == 200
        assert out["total_costs_paisa"] == 0
        assert out["net_paisa"] == 200
        assert out["lines"][0] == "Σ trade P&L = ₹2.00 across 3 trades"
        assert out["lines"][3] == (
            "3 row(s) carry null costs (by design until P5) — counted at ₹0.00, "
            "so the net is GROSS of those costs"
        )
        assert not any("non-integer" in line for line in out["lines"])

    def test_null_costs_and_genuine_invalid_rows_are_bucketed_separately(self) -> None:
        rows = [
            {"pnl_paisa": 100, "costs_paisa": None},  # by-design null — counted
            {"pnl_paisa": 0.5, "costs_paisa": 0},  # genuine non-integer — excluded
        ]
        out = _node_call("E.tradeArithmetic(ARGS[0])", json.dumps(rows))
        assert out["ok"] is False  # the genuine defect still flips ok
        assert out["null_cost_rows"] == 1
        assert out["invalid_rows"] == 1
        assert out["rows_counted"] == 1
        assert "null costs (by design until P5)" in out["lines"][3]
        assert "non-integer" in out["lines"][4]


class TestModeText:
    """bxModeText — the R33-toggle law (P4 owns the toggle; the table is P3's)."""

    def test_pro_mode_always_returns_the_technical_text(self) -> None:
        out = _node_call('E.modeText("pro", "PDH_CROSS_UP", "plain words")')
        assert out == "PDH_CROSS_UP"

    def test_beginner_mode_prefers_the_artifact_provided_plain_string(self) -> None:
        out = _node_call('E.modeText("beginner", "PDH_CROSS_UP", "closed above yesterday high")')
        assert out == "closed above yesterday high"

    def test_beginner_mode_without_a_plain_string_appends_the_honest_p3_note(self) -> None:
        """Never an invented sentence — the note admits the plain phrasing is absent."""
        out = _node_call('E.modeText("beginner", "PDH_CROSS_UP", null)')
        assert out == "PDH_CROSS_UP (plain-English version not written yet)"
