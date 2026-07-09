"""Node-executed goldens for the pure chart math in ``bx-chart.js`` (DEC-OPERATOR-PAGE-REAL-001).

Fails-before: the strategy page's candle chart / entry-exit arrows / equity
curve / funnel widths did not exist; a naive implementation would divide by a
zero domain (flat price / zero funnel total), float-accumulate the equity
curve, or SNAP an out-of-window trade to the nearest wrong candle. The
degenerate-domain CONTRACTS (F-B3) + the ``-1`` out-of-window sentinel (F-B1)
below define the module.

The repo has NO executable-JS harness (an honest, known limit), so this suite
executes the REAL shipped ``bx-chart.js`` under node (the bx-format.js
precedent, tests/unit/test_dashboard_halfup_format.py) and asserts against
Python integer/exact oracles. Golden inputs are chosen exactly-representable
(ints / paisa / power-of-two pixel boxes) OR the JS output string itself is
pinned — never an approx tolerance (T-NIT-1). Skipped (visibly) where node is
absent; ``test_dashboard_oneclick_frontend.py`` carries the CI node-presence
guard so the skip can never go silent on the runners.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from pathlib import Path
from typing import Any

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"
_CHART_JS = _STATIC / "bx-chart.js"
_TRADES_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "oneclick"
    / "runs"
    / "r-20260707-fixt01"
    / "trades"
    / "rank-0001.json"
)

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not installed")


def _node_call(expr: str, *argv: str) -> Any:
    """Run the REAL shipped bx-chart.js under node and return the JSON result.

    ``expr`` is evaluated with ``C`` bound to the module's exports and
    ``ARGS`` bound to the JSON-parsed argv payloads.
    """
    script = (
        f"const C = require({json.dumps(str(_CHART_JS))});"
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


class TestScaleLinear:
    """bxScaleLinear — incl. the F-B3 flat-domain contract."""

    def test_flat_domain_maps_every_input_to_range_midpoint(self) -> None:
        out = _node_call(
            "{flat: C.scaleLinear(ARGS[0], ARGS[1]).flat,"
            " vals: [0, 5, 123, -9].map(C.scaleLinear(ARGS[0], ARGS[1]))}",
            json.dumps([5, 5]),
            json.dumps([0, 100]),
        )
        assert out["flat"] is True
        assert out["vals"] == [50, 50, 50, 50]  # the range MIDPOINT, never NaN

    def test_normal_domain_is_exact_linear_interpolation(self) -> None:
        # Power-of-two domain/range => exactly-representable interpolation.
        out = _node_call(
            "{flat: C.scaleLinear(ARGS[0], ARGS[1]).flat,"
            " vals: [0, 2, 4, 8].map(C.scaleLinear(ARGS[0], ARGS[1]))}",
            json.dumps([0, 8]),
            json.dumps([0, 64]),
        )
        assert out["flat"] is False
        assert out["vals"] == [0, 16, 32, 64]


class TestCandleGeom:
    """bxCandleGeom — 0-row / 1-row / flat-price degenerate contracts (F-B3)."""

    def test_zero_rows_is_the_empty_flag_never_a_crash(self) -> None:
        out = _node_call("C.candleGeom(ARGS[0], 100, 50, 10)", json.dumps([]))
        assert out == {
            "candles": [],
            "empty": True,
            "flat": False,
            "xSlot": 0,
            "priceLo": None,
            "priceHi": None,
        }

    def test_single_candle_is_centered(self) -> None:
        rows = [{"ts": 1, "open": 10, "high": 12, "low": 8, "close": 11}]
        out = _node_call("C.candleGeom(ARGS[0], 100, 50, 10)", json.dumps(rows))
        assert out["empty"] is False
        assert len(out["candles"]) == 1
        # One slot spans the full inner width (80); its center is x = 50.
        assert out["candles"][0]["x"] == 50
        assert out["xSlot"] == 80

    def test_flat_price_lands_every_y_on_the_vertical_midpoint(self) -> None:
        rows = [{"ts": 1, "open": 10, "high": 10, "low": 10, "close": 10}]
        out = _node_call("C.candleGeom(ARGS[0], 100, 50, 10)", json.dumps(rows))
        assert out["flat"] is True  # the renderer shows the "flat range" note
        candle = out["candles"][0]
        # Range [h - pad, pad] = [40, 10] => midpoint 25 for every y.
        assert (candle["yOpen"], candle["yClose"], candle["yHigh"], candle["yLow"]) == (
            25,
            25,
            25,
            25,
        )

    def test_up_down_flag_tracks_close_vs_open(self) -> None:
        rows = [
            {"ts": 1, "open": 10, "high": 12, "low": 8, "close": 11},
            {"ts": 2, "open": 11, "high": 12, "low": 8, "close": 9},
        ]
        out = _node_call("C.candleGeom(ARGS[0], 128, 64, 0)", json.dumps(rows))
        assert [c["up"] for c in out["candles"]] == [True, False]


class TestEquitySeries:
    """bxEquitySeries — INTEGER-paisa cumsum vs a Python int oracle (F-n2)."""

    def test_zero_trades_is_the_empty_flag_never_a_degenerate_polyline(self) -> None:
        out = _node_call("C.equitySeries(ARGS[0])", json.dumps([]))
        assert out == {
            "points": [],
            "final_paisa": 0,
            "empty": True,
            "overflow": False,
            "invalid_rows": 0,
        }

    def test_fixture_trades_cumsum_matches_the_python_int_oracle(self) -> None:
        """The REAL 412-row fixture trades through the REAL JS vs pure-int Python."""
        payload = json.loads(_TRADES_FIXTURE.read_text(encoding="utf-8"))
        rows: list[dict[str, Any]] = payload["rows"]
        assert len(rows) == 412  # the committed corpus size (drift guard)
        out = _node_call("C.equitySeries(ARGS[0])", json.dumps(rows))
        # Python oracle: exact int cumsum of (pnl - costs).
        cum = 0
        expected: list[int] = []
        for row in rows:
            cum += int(row["pnl_paisa"]) - int(row["costs_paisa"])
            expected.append(cum)
        assert [p["cum_paisa"] for p in out["points"]] == expected
        assert out["final_paisa"] == expected[-1]
        assert out["overflow"] is False
        assert out["invalid_rows"] == 0

    def test_overflow_guard_stops_honestly_at_the_safe_integer_wall(self) -> None:
        msi = 2**53 - 1  # Number.MAX_SAFE_INTEGER — round-trips JSON exactly
        rows = [{"pnl_paisa": msi, "costs_paisa": 0}, {"pnl_paisa": msi, "costs_paisa": 0}]
        out = _node_call("C.equitySeries(ARGS[0])", json.dumps(rows))
        assert out["overflow"] is True
        assert out["final_paisa"] == msi  # truncated honestly, never a wrong float
        assert len(out["points"]) == 1

    def test_non_integer_paisa_rows_are_skipped_and_counted(self) -> None:
        rows = [
            {"pnl_paisa": 100, "costs_paisa": 0},
            {"pnl_paisa": 1.5, "costs_paisa": 0},  # not a paisa int — counted, never coerced
            {"pnl_paisa": 50, "costs_paisa": 25},
        ]
        out = _node_call("C.equitySeries(ARGS[0])", json.dumps(rows))
        assert out["invalid_rows"] == 1
        assert [p["cum_paisa"] for p in out["points"]] == [100, 125]


class TestFunnelPct:
    """bxFunnelPct — the zero-total awaiting contract (F-B3)."""

    def test_zero_total_is_all_zero_widths_plus_awaiting(self) -> None:
        out = _node_call("C.funnelPct(ARGS[0])", json.dumps([0, 0, 0]))
        assert out == {"widths": [0, 0, 0], "awaiting": True}

    def test_empty_counts_is_awaiting_with_no_widths(self) -> None:
        out = _node_call("C.funnelPct(ARGS[0])", json.dumps([]))
        assert out == {"widths": [], "awaiting": True}

    def test_widths_are_exact_ratios_of_the_first_stage(self) -> None:
        # Power-of-two counts => exactly-representable percentages.
        out = _node_call("C.funnelPct(ARGS[0])", json.dumps([200, 100, 50, 0]))
        assert out == {"widths": [100, 50, 25, 0], "awaiting": False}


class TestNearestBarIdx:
    """bxNearestBarIdx — the F-B1 out-of-window -1 sentinel."""

    _ROWS = json.dumps([{"ts": 100}, {"ts": 200}, {"ts": 300}])

    @pytest.mark.parametrize(
        ("ts", "expected"),
        [
            (50, -1),  # before the first bar -> sentinel (never snapped)
            (350, -1),  # after the last bar -> sentinel
            (240, 1),  # between two bars -> nearer index
            (250, 1),  # equidistant tie -> the EARLIER index (deterministic)
            (300, 2),  # exact match
            (100, 0),  # exact match at the first bar (inclusive boundary)
        ],
    )
    def test_index_and_sentinel_cases(self, ts: int, expected: int) -> None:
        out = _node_call("C.nearestBarIdx(ARGS[0], ARGS[1])", json.dumps(ts), self._ROWS)
        assert out == expected

    def test_empty_rows_is_the_sentinel(self) -> None:
        out = _node_call("C.nearestBarIdx(150, ARGS[0])", json.dumps([]))
        assert out == -1

    def test_out_of_window_trade_drops_the_arrow_with_the_loud_note(self) -> None:
        """F-B1b: a dropped arrow carries the pinned note, never a snapped index pair."""
        out = _node_call("C.tradeArrow(50, 240, ARGS[0])", self._ROWS)
        assert out["dropped"] is True
        assert out["entry_idx"] == -1
        assert out["note"] == "trade outside chart window — widen range"

    def test_in_window_trade_keeps_both_arrow_indices(self) -> None:
        out = _node_call("C.tradeArrow(120, 300, ARGS[0])", self._ROWS)
        assert out == {"entry_idx": 0, "exit_idx": 2, "dropped": False, "note": ""}


class TestPathD:
    """bxPathD — the fixed-2dp path golden (F-n1; JS output string pinned)."""

    def test_path_string_golden(self) -> None:
        points = [{"x": 1, "y": 2.25}, {"x": 2.5, "y": 3}]
        out = _node_call("C.pathD(ARGS[0])", json.dumps(points))
        assert out == "M1.00 2.25 L2.50 3.00"

    def test_empty_points_is_the_empty_string(self) -> None:
        out = _node_call("C.pathD(ARGS[0])", json.dumps([]))
        assert out == ""
