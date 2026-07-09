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
import os
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"
_CHART_JS = _STATIC / "bx-chart.js"
_TRADES_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "oneclick"
    / "sweep_runs"
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
        }  # the empty fast path predates null_cost_rows — shape pinned as-is

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

    def test_null_costs_count_at_zero_never_into_the_invalid_bucket(self) -> None:
        """[W2-4] DEC-ONECLICK-TOP100-OBJECT-ROOT-001: Ruling-18 NULL-BY-DESIGN
        ``costs_paisa: null`` (every production trade row until P5) previously
        fell into the non-integer bucket — 3 real rows produced an EMPTY
        equity curve. Null costs now contribute ₹0 and are COUNTED in the
        additive ``null_cost_rows`` so the renderer labels the curve gross."""
        rows = [
            {"pnl_paisa": 100, "costs_paisa": None},
            {"pnl_paisa": 101, "costs_paisa": None},
            {"pnl_paisa": 102, "costs_paisa": None},
        ]
        out = _node_call("C.equitySeries(ARGS[0])", json.dumps(rows))
        assert out["invalid_rows"] == 0  # by-design null is NOT corrupt
        assert out["null_cost_rows"] == 3
        assert [p["cum_paisa"] for p in out["points"]] == [100, 201, 303]
        assert out["final_paisa"] == 303

    def test_absent_costs_key_counts_like_null(self) -> None:
        rows = [{"pnl_paisa": 100}, {"pnl_paisa": 50, "costs_paisa": 10}]
        out = _node_call("C.equitySeries(ARGS[0])", json.dumps(rows))
        assert out["null_cost_rows"] == 1
        assert [p["cum_paisa"] for p in out["points"]] == [100, 140]

    def test_non_integer_costs_still_fall_into_the_invalid_bucket(self) -> None:
        """The invalid bucket keeps its teeth for GENUINE non-integers."""
        rows = [{"pnl_paisa": 100, "costs_paisa": 0.5}]
        out = _node_call("C.equitySeries(ARGS[0])", json.dumps(rows))
        assert out["invalid_rows"] == 1
        assert out["null_cost_rows"] == 0
        assert out["points"] == []


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


def _node_call_tz(tz: str, expr: str, *argv: str) -> Any:
    """``_node_call`` with node's ambient timezone FORCED to *tz*.

    The [W3-R1] second-layer TZ goldens run the REAL shipped parser under a
    non-IST zone (node honors the ``TZ`` env var), proving candle/arrow
    placement never rides browser-local ``Date`` parsing of a naive string.
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
        env={**os.environ, "TZ": tz},
    )
    return json.loads(proc.stdout)


#: 2024-01-02 10:15:00 IST — epoch ms oracle computed in Python (tz-explicit).
_IST = timezone(timedelta(hours=5, minutes=30))
_NAIVE_IST_STR = "2024-01-02 10:15:00"
_NAIVE_IST_EPOCH_MS = int(datetime(2024, 1, 2, 10, 15, tzinfo=_IST).timestamp() * 1000)


class TestParseTsIst:
    """bxParseTsIst — the [W3-R1] TZ-safe parse (naive = IST, never local)."""

    def test_naive_api_string_is_pinned_to_ist(self) -> None:
        out = _node_call("C.parseTsIst(ARGS[0])", json.dumps(_NAIVE_IST_STR))
        assert out == _NAIVE_IST_EPOCH_MS

    def test_date_only_1day_bar_reads_midnight_ist(self) -> None:
        out = _node_call("C.parseTsIst(ARGS[0])", json.dumps("2024-01-02"))
        assert out == int(datetime(2024, 1, 2, tzinfo=_IST).timestamp() * 1000)

    def test_offset_carrying_trade_string_passes_through(self) -> None:
        """trade_store._us_to_iso emits "+00:00" — never double-shifted."""
        out = _node_call("C.parseTsIst(ARGS[0])", json.dumps("2024-01-02T04:45:00+00:00"))
        assert out == int(datetime(2024, 1, 2, 4, 45, tzinfo=UTC).timestamp() * 1000)

    def test_zulu_string_passes_through(self) -> None:
        out = _node_call("C.parseTsIst(ARGS[0])", json.dumps("2024-01-02T04:45:00Z"))
        assert out == int(datetime(2024, 1, 2, 4, 45, tzinfo=UTC).timestamp() * 1000)

    def test_number_passes_through_and_junk_is_null(self) -> None:
        assert _node_call("C.parseTsIst(1234)") == 1234
        assert _node_call("C.parseTsIst('not a time')") is None
        assert _node_call("C.parseTsIst('')") is None
        assert _node_call("C.parseTsIst(null)") is None
        assert _node_call("C.parseTsIst(NaN)") is None

    @pytest.mark.parametrize("tz", ["America/New_York", "Asia/Kolkata", "UTC"])
    def test_naive_parse_is_tz_invariant(self, tz: str) -> None:
        """The SAME epoch in every ambient zone — the pre-fix Date.parse of a
        naive string shifted by (local − IST) in a non-IST browser."""
        out = _node_call_tz(tz, "C.parseTsIst(ARGS[0])", json.dumps(_NAIVE_IST_STR))
        assert out == _NAIVE_IST_EPOCH_MS


class TestIstDateOfEpoch:
    """bxIstDateOfEpoch — the TZ-invariant session-day anchor."""

    def test_ist_date_differs_from_utc_date_after_1830_utc(self) -> None:
        ms = int(datetime(2024, 1, 2, 19, 30, tzinfo=UTC).timestamp() * 1000)
        assert _node_call("C.istDateOfEpoch(ARGS[0])", json.dumps(ms)) == "2024-01-03"

    def test_session_morning_instant_reads_the_session_date(self) -> None:
        assert (
            _node_call("C.istDateOfEpoch(ARGS[0])", json.dumps(_NAIVE_IST_EPOCH_MS)) == "2024-01-02"
        )

    def test_non_finite_is_null(self) -> None:
        assert _node_call("C.istDateOfEpoch(NaN)") is None
        assert _node_call("C.istDateOfEpoch('x')") is None

    @pytest.mark.parametrize("tz", ["America/New_York", "Asia/Kolkata"])
    def test_anchor_date_is_tz_invariant(self, tz: str) -> None:
        out = _node_call_tz(tz, "C.istDateOfEpoch(ARGS[0])", json.dumps(_NAIVE_IST_EPOCH_MS))
        assert out == "2024-01-02"


#: A REAL /api/v1/db/candles payload SHAPE (db_view.CANDLE_COLUMNS + the two
#: derived columns a spot page appends) — the [W3-R1] fixture-blindspot killer:
#: the column is named "timestamp"; the pre-fix mapper indexed ``idx.ts``.
_DB_CANDLES_SHAPED_PAYLOAD = {
    "columns": [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
        "change_pct",
        "open_gap_pct",
    ],
    "rows": [
        ["2024-01-02 10:16:00", 101.0, 102.0, 100.5, 101.5, 10, None, None, None],
        ["2024-01-02 10:15:00", 100.0, 101.0, 99.5, 100.5, 12, None, None, None],
        ["junk-not-a-time", 1.0, 2.0, 0.5, 1.5, 1, None, None, None],
    ],
}


class TestCandleRowsFromDbPayload:
    """bxCandleRowsFromDbPayload — the route-shape mapper ([W3-R1])."""

    def test_real_payload_shape_maps_and_sorts_ascending(self) -> None:
        out = _node_call(
            "C.candleRowsFromDbPayload(ARGS[0])", json.dumps(_DB_CANDLES_SHAPED_PAYLOAD)
        )
        assert out["missing"] is None
        assert [r["open"] for r in out["rows"]] == [100.0, 101.0]  # sorted by ts
        assert out["rows"][0]["ts"] == _NAIVE_IST_EPOCH_MS
        assert out["rows"][0] == {
            "ts": _NAIVE_IST_EPOCH_MS,
            "open": 100.0,
            "high": 101.0,
            "low": 99.5,
            "close": 100.5,
        }
        # The unparseable-timestamp row is SKIPPED, never a NaN coordinate.
        assert len(out["rows"]) == 2

    def test_missing_timestamp_column_is_named_never_a_silent_empty(self) -> None:
        payload = {"columns": ["ts", "open", "high", "low", "close"], "rows": [[1, 2, 3, 4, 5]]}
        out = _node_call("C.candleRowsFromDbPayload(ARGS[0])", json.dumps(payload))
        assert out == {"rows": [], "missing": "timestamp"}

    def test_empty_payload_maps_to_no_rows(self) -> None:
        out = _node_call("C.candleRowsFromDbPayload(ARGS[0])", json.dumps({}))
        # No columns at all: the FIRST required column is named.
        assert out == {"rows": [], "missing": "timestamp"}

    @pytest.mark.parametrize("tz", ["America/New_York", "Asia/Kolkata"])
    def test_arrow_placement_is_tz_invariant_end_to_end(self, tz: str) -> None:
        """The [W3-R1] second-layer golden: candles from NAIVE IST strings +
        a trade from OFFSET-carrying UTC strings land the SAME arrow indices
        in a New York browser as in an IST one (pre-fix the candle epochs
        shifted browser-local while the trade epochs did not — misplaced or
        dropped arrows)."""
        expr = (
            "(() => {"
            "  const rows = C.candleRowsFromDbPayload(ARGS[0]).rows;"
            "  const entry = C.parseTsIst(ARGS[1]);"
            "  const exit = C.parseTsIst(ARGS[2]);"
            "  return C.tradeArrow(entry, exit, rows);"
            "})()"
        )
        out = _node_call_tz(
            tz,
            expr,
            json.dumps(_DB_CANDLES_SHAPED_PAYLOAD),
            # 10:15 / 10:16 IST expressed as the trade store's UTC "+00:00" form.
            json.dumps("2024-01-02T04:45:00+00:00"),
            json.dumps("2024-01-02T04:46:00+00:00"),
        )
        assert out == {"entry_idx": 0, "exit_idx": 1, "dropped": False, "note": ""}
