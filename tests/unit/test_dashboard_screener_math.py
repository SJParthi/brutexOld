"""Node-executed goldens for the pure screener math in ``bx-screener.js`` (DEC-OPERATOR-PAGE-REAL-001).

Fails-before: the /screener page's client-side sort/filter did not exist; a
naive comparator (a) breaks total order on metric ties (unstable row jumps on
re-render), (b) collapses full-precision values by coercing through Number
(``Number(2**60) === Number(2**60 + 1)`` — the /results exact-int precedent),
and (c) silently drops rows whose ``vix_band`` is null under a VIX filter
(R31 honest NULL violated). The goldens below pin the fixes: the
rank-ascending tie-break (F-m2), relational-only comparison, and the
excluded-but-COUNTED null-band law (F-m3).

The repo has NO executable-JS harness, so this suite executes the REAL
shipped ``bx-screener.js`` under node (the bx-format.js precedent) — incl.
the REAL committed top100 fixture with its documented tie pair (constants
imported from the generator module, the single source of truth). Skipped
(visibly) where node is absent; the CI node-presence guard lives in
``test_dashboard_oneclick_frontend.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tests.golden.generate_oneclick_fixture import (
    NULL_ARM_RANK,
    NULL_VIX_RANK,
    RUN_ID,
    TIE_RANKS,
)

_STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"
_SCREENER_JS = _STATIC / "bx-screener.js"
_TOP100_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "oneclick"
    / "sweep_runs"
    / RUN_ID
    / "top100.json"
)

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not installed")


def _node_call(expr: str, *argv: str) -> Any:
    """Run the REAL shipped bx-screener.js under node and return the JSON result.

    The JSON arguments are handed to node via TEMP FILES (node reads + parses
    each), not the process argv — the full 100-row fixture payload now exceeds
    the kernel's per-arg ``MAX_ARG_STRLEN`` (128 KiB) once every row carries its
    resilience block, so a bare argv would raise ``Argument list too long``.
    ``ARGS[i]`` stays the parsed value, so every call site is byte-unchanged.
    """
    with tempfile.TemporaryDirectory() as tmp:
        paths: list[str] = []
        for i, arg in enumerate(argv):
            path = Path(tmp) / f"arg{i}.json"
            path.write_text(arg, encoding="utf-8")
            paths.append(str(path))
        script = (
            f"const S = require({json.dumps(str(_SCREENER_JS))});"
            "const fs = require('fs');"
            "const ARGS = process.argv.slice(1).map((p) => JSON.parse(fs.readFileSync(p, 'utf8')));"
            f"console.log(JSON.stringify({expr}));"
        )
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            ["node", "-e", script, *paths],
            capture_output=True,
            text=True,
            check=True,
        )
    return json.loads(proc.stdout)


def _fixture_rows() -> list[dict[str, Any]]:
    """The committed fixture's rows THROUGH the real display reader.

    DEC-ONECLICK-TOP100-OBJECT-ROOT-001: the fixture is the writer's OBJECT
    root with ENGINE metric names; the screener consumes rows the reader has
    already validated + metric-mapped (``metrics.sharpe`` et al. are the
    reader's aliases), so the JS goldens run over EXACTLY the wire rows.
    """
    from brutex.dashboard import oneclick_artifacts as oc

    result = oc.load_top100(_TOP100_FIXTURE.parent)
    assert result.status == "ok", result.detail
    return [dict(row) for row in result.rows]


class TestComparatorTotalOrder:
    """bxCompare — total order, null-last, rank tie-break (F-m2)."""

    def test_sorted_fixture_matches_the_python_oracle_order(self) -> None:
        """Sharpe-desc over the REAL 100 fixture rows vs a Python oracle sort."""
        rows = _fixture_rows()
        out = _node_call(
            'S.sortRows(ARGS[0], "metrics.sharpe", "num", "desc").map((r) => r.rank)',
            json.dumps(rows),
        )
        expected = [
            r["rank"] for r in sorted(rows, key=lambda r: (-r["metrics"]["sharpe"], r["rank"]))
        ]
        assert out == expected

    def test_tie_pair_orders_rank_ascending_in_both_directions(self) -> None:
        """The F-m2 golden: the fixture's identical-sharpe pair keeps rank order.

        Tie-break is rank ASCENDING regardless of the sort direction, so the
        pair reads the same way under desc AND asc.
        """
        rows = _fixture_rows()
        lo, hi = TIE_RANKS
        assert (
            rows[lo - 1]["metrics"]["sharpe"] == rows[hi - 1]["metrics"]["sharpe"]
        )  # the fixture's documented tie
        for direction in ("desc", "asc"):
            ranks = _node_call(
                f'S.sortRows(ARGS[0], "metrics.sharpe", "num", {json.dumps(direction)})'
                ".map((r) => r.rank)",
                json.dumps(rows),
            )
            assert ranks.index(lo) < ranks.index(hi), direction

    def test_null_metric_values_sort_last_regardless_of_direction(self) -> None:
        rows = [
            {"rank": 1, "metrics": {"v": None}},
            {"rank": 2, "metrics": {"v": 5}},
            {"rank": 3, "metrics": {"v": 1}},
        ]
        for direction, expected in (("asc", [3, 2, 1]), ("desc", [2, 3, 1])):
            out = _node_call(
                f'S.sortRows(ARGS[0], "metrics.v", "num", {json.dumps(direction)})'
                ".map((r) => r.rank)",
                json.dumps(rows),
            )
            assert out == expected, direction

    def test_full_precision_teeth_bigint_2_pow_60_vs_plus_one(self) -> None:
        """The comparator never coerces through Number — BigInt inputs keep order.

        Teeth: Number-parsed JSON collapses 2**60 and 2**60+1 to the SAME
        double (asserted below), which is exactly why bxCompare uses
        relational operators instead of subtraction/Number().
        """
        script = (
            f"const S = require({json.dumps(str(_SCREENER_JS))});"
            "const rows = [{rank: 1, metrics: {v: 2n ** 60n}},"
            "              {rank: 2, metrics: {v: 2n ** 60n + 1n}}];"
            'const sorted = S.sortRows(rows, "metrics.v", "num", "desc");'
            "console.log(JSON.stringify({"
            "  first: sorted[0].rank,"
            "  numberCollapses: Number(2n ** 60n) === Number(2n ** 60n + 1n)"
            "}));"
        )
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)
        assert out["numberCollapses"] is True  # the trap the comparator avoids
        assert out["first"] == 2  # 2**60 + 1 correctly sorts FIRST under desc

    def test_string_and_conditions_array_fields_sort_deterministically(self) -> None:
        rows = [
            {"rank": 1, "conditions": ["B_COND", "PDH_CROSS_UP"]},
            {"rank": 2, "conditions": ["A_COND", "PDH_CROSS_UP"]},
        ]
        out = _node_call(
            'S.sortRows(ARGS[0], "conditions", "str", "asc").map((r) => r.rank)',
            json.dumps(rows),
        )
        assert out == [2, 1]


class TestVixOverlap:
    """bxVixOverlap — inclusive boundaries + the null-band never-matches law."""

    @pytest.mark.parametrize(
        ("band", "lo", "hi", "expected"),
        [
            ({"lo": 11, "hi": 17.5}, 17.5, 20, True),  # touching hi edge — inclusive
            ({"lo": 11, "hi": 17.5}, 17.6, 20, False),  # just past the edge
            ({"lo": 11, "hi": 17.5}, 5, 11, True),  # touching lo edge — inclusive
            ({"lo": 11, "hi": 17.5}, 5, 10.9, False),
            ({"lo": 11, "hi": 17.5}, 12, 13, True),  # filter inside the band
            (None, 5, 20, False),  # null band NEVER matches (caller counts it)
            ({"lo": None, "hi": 17.5}, 5, 20, False),  # malformed band
        ],
    )
    def test_overlap_boundaries(
        self, band: dict[str, Any] | None, lo: float, hi: float, expected: bool
    ) -> None:
        out = _node_call(
            "S.vixOverlap(ARGS[0], ARGS[1], ARGS[2])",
            json.dumps(band),
            json.dumps(lo),
            json.dumps(hi),
        )
        assert out is expected


class TestApplyFilters:
    """bxApplyFilters — predicates + the F-m3 excluded-but-COUNTED law."""

    def test_null_vix_band_row_is_excluded_but_counted_under_an_active_filter(self) -> None:
        """The REAL fixture's null-vix row (rank 100) never silently vanishes."""
        rows = _fixture_rows()
        assert rows[NULL_VIX_RANK - 1]["vix_band"] is None  # the fixture's documented edge
        out = _node_call(
            "S.applyFilters(ARGS[0], ARGS[1])",
            json.dumps(rows),
            json.dumps({"vix_lo": 0, "vix_hi": 100}),
        )
        assert out["null_vix_excluded"] == 1
        kept_ranks = {r["rank"] for r in out["rows"]}
        assert NULL_VIX_RANK not in kept_ranks
        assert len(out["rows"]) == 99  # every banded row overlaps the wide filter

    def test_no_vix_filter_keeps_the_null_band_row_with_a_zero_count(self) -> None:
        rows = _fixture_rows()
        out = _node_call("S.applyFilters(ARGS[0], ARGS[1])", json.dumps(rows), json.dumps({}))
        assert out["null_vix_excluded"] == 0
        assert len(out["rows"]) == 100

    def test_tf_direction_and_metric_floors_filter_rows(self) -> None:
        rows = _fixture_rows()
        filters = {"tf": ["5m"], "direction": "long", "min_win_rate": 0.5}
        out = _node_call("S.applyFilters(ARGS[0], ARGS[1])", json.dumps(rows), json.dumps(filters))
        expected = [
            r
            for r in rows
            if r["tf"] == "5m" and r["direction"] == "long" and r["metrics"]["win_rate"] >= 0.5
        ]
        assert [r["rank"] for r in out["rows"]] == [r["rank"] for r in expected]
        assert len(out["rows"]) > 0  # a non-vacuous predicate over the fixture

    def test_engine_named_metrics_without_the_mapping_drop_every_row(self) -> None:
        """The metric-drift BLAST RADIUS (DEC-ONECLICK-TOP100-OBJECT-ROOT-001):
        RAW engine-named rows (``max_drawdown_paisa`` — no ``max_dd_paisa``
        alias) fail the Max|DD| band filter's number check, so EVERY row is
        dropped — not merely "—" columns. The reader's ONE metric-key mapping
        is what keeps the filter alive; the mapped fixture keeps all rows."""
        raw_engine_rows = [
            {"rank": 1, "metrics": {"max_drawdown_paisa": -100}},
            {"rank": 2, "metrics": {"max_drawdown_paisa": -300}},
        ]
        dropped = _node_call(
            "S.applyFilters(ARGS[0], ARGS[1])",
            json.dumps(raw_engine_rows),
            json.dumps({"max_abs_dd_paisa": 1_000_000}),
        )
        assert dropped["rows"] == []  # the pre-fix failure mode, pinned
        mapped = _fixture_rows()  # the SAME filter over reader-mapped rows
        kept = _node_call(
            "S.applyFilters(ARGS[0], ARGS[1])",
            json.dumps(mapped),
            json.dumps({"max_abs_dd_paisa": 10_000_000_000}),
        )
        assert len(kept["rows"]) == 100  # every row survives the wide cap

    def test_max_abs_dd_cap_compares_the_magnitude_of_the_negative_dd(self) -> None:
        rows = [
            {"rank": 1, "metrics": {"max_dd_paisa": -100}},
            {"rank": 2, "metrics": {"max_dd_paisa": -300}},
        ]
        out = _node_call(
            "S.applyFilters(ARGS[0], ARGS[1])",
            json.dumps(rows),
            json.dumps({"max_abs_dd_paisa": 200}),
        )
        assert [r["rank"] for r in out["rows"]] == [1]

    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (0, ""),
            (1, "(1 row has no VIX band — clear the filter to see it)"),
            (3, "(3 rows have no VIX band — clear the filter to see them)"),
        ],
    )
    def test_null_vix_note_wording_golden(self, n: int, expected: str) -> None:
        """The F-m3 count-line suffix is pinned verbatim (UI cannot drift)."""
        out = _node_call("S.nullVixNote(ARGS[0])", json.dumps(n))
        assert out == expected


class TestArmRenderString:
    """Hostile-round F1 node golden: the arm column renders the BRIDGED string.

    Every display sink ``String()``s the arm field (screener.html's str-cell
    fallback / strategy.html stArm / bx-oneclick.js r1_arm); these goldens
    run the writer-DICT fixture rows THROUGH the real reader and pin that
    the JS-side ``String()`` yields the bridged money string, never
    ``"[object Object]"`` — plus the teeth proving the raw writer dict
    WOULD render exactly that without the bridge.
    """

    def test_reader_bridged_arm_renders_the_money_string(self) -> None:
        rows = _fixture_rows()
        out = _node_call(
            'ARGS[0].map((r) => String(S.fieldValue(r, "arm")))',
            json.dumps([rows[0], rows[NULL_ARM_RANK - 1]]),
        )
        assert out == ["SL ₹51.50 · TP ₹93.75", "SL — · TP —"]
        assert "[object Object]" not in out

    def test_raw_writer_arm_dict_would_render_object_object(self) -> None:
        """The pre-bridge failure mode, pinned: the SHIPPED writer's arm dict
        ``String()``s to "[object Object]" — what every real sweep card
        showed until the reader bridge (the fixture's old STRING arm had
        masked it)."""
        raw = {"rank": 1, "arm": {"sl_paisa": 80, "tp_paisa": 50}}
        out = _node_call('String(S.fieldValue(ARGS[0], "arm"))', json.dumps(raw))
        assert out == "[object Object]"
