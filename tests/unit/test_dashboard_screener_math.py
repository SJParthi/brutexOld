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
    """Run the REAL shipped bx-screener.js under node and return the JSON result."""
    script = (
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


class TestInstrumentFilter:
    """DEC-ONECLICK-INSTRUMENT-DISPLAY-001 — the instrument column + filter.

    Fails-before: the screener had NO instrument column/filter, so the
    operator could not tell (or filter by) WHICH index a winner trades.
    The column is config-driven off BXScreener.COLUMNS; the filter is the
    direction-style exact match with the documented absent-key law (a
    pre-instrument artifact row never matches an ACTIVE filter — and the
    page only offers options harvested from rows that carry the key).
    """

    def test_columns_config_carries_the_instrument_column(self) -> None:
        cols = _node_call("S.COLUMNS")
        keys = [c["key"] for c in cols]
        assert "instrument" in keys
        col = cols[keys.index("instrument")]
        assert col == {
            "key": "instrument",
            "label": "Inst",  # the combos-table header vocabulary (audit item 1c)
            "type": "str",
            "explain": "instrument",
        }
        # Placed in the identity block, right after the strategy name.
        assert keys.index("instrument") == keys.index("conditions") + 1

    def test_matching_instrument_filter_keeps_every_fixture_row(self) -> None:
        rows = _fixture_rows()
        assert all(r["instrument"] == "NSE-NIFTY" for r in rows)  # fixture truth
        out = _node_call(
            "S.applyFilters(ARGS[0], ARGS[1])",
            json.dumps(rows),
            json.dumps({"instrument": "NSE-NIFTY"}),
        )
        assert len(out["rows"]) == 100

    def test_non_matching_instrument_filter_drops_every_fixture_row(self) -> None:
        out = _node_call(
            "S.applyFilters(ARGS[0], ARGS[1])",
            json.dumps(_fixture_rows()),
            json.dumps({"instrument": "NSE-BANKNIFTY"}),
        )
        assert out["rows"] == []

    def test_absent_instrument_row_never_matches_an_active_filter(self) -> None:
        """The documented pre-instrument-artifact law: no key ⇒ excluded under
        an ACTIVE filter, kept when the filter is inactive."""
        rows = [
            {"rank": 1, "instrument": "NSE-NIFTY", "metrics": {}},
            {"rank": 2, "metrics": {}},  # a pre-instrument artifact row
        ]
        active = _node_call(
            "S.applyFilters(ARGS[0], ARGS[1])",
            json.dumps(rows),
            json.dumps({"instrument": "NSE-NIFTY"}),
        )
        assert [r["rank"] for r in active["rows"]] == [1]
        inactive = _node_call("S.applyFilters(ARGS[0], ARGS[1])", json.dumps(rows), json.dumps({}))
        assert [r["rank"] for r in inactive["rows"]] == [1, 2]

    def test_instrument_column_sorts_deterministically_with_rank_tie_break(self) -> None:
        rows = [
            {"rank": 1, "instrument": "NSE-NIFTY"},
            {"rank": 2, "instrument": "BSE-SENSEX"},
            {"rank": 3, "instrument": "NSE-NIFTY"},
        ]
        out = _node_call(
            'S.sortRows(ARGS[0], "instrument", "str", "asc").map((r) => r.rank)',
            json.dumps(rows),
        )
        assert out == [2, 1, 3]  # alpha ascending; the NIFTY tie keeps rank order


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


class TestRenderCaps:
    """A-r4 node goldens (DEC-ONECLICK-PERF-CLUB-001): the cap/note math.

    Fails-before: "Show all" rendered ``sorted.length`` rows UNBOUNDED —
    Playwright-measured 4.8 s @ 10k / 14.1 s @ 20k rows of tab freeze — and
    applyFilters/sortRows processed the whole untrusted payload. The caps
    below bound every work surface with the truncation NAMED, never silent.
    """

    def test_cap_constants_are_exported_and_ordered(self) -> None:
        caps = _node_call("[S.RENDER_CAP, S.SHOW_ALL_CAP, S.INPUT_CAP]")
        assert caps == [200, 1000, 5000]
        assert caps[0] < caps[1] < caps[2]  # default < show-all < input bound

    @pytest.mark.parametrize(
        ("sorted_length", "show_all", "expected"),
        [
            (100, False, 100),  # the ≤100-row contract renders whole
            (100, True, 100),
            (250, False, 200),  # default view caps at RENDER_CAP
            (250, True, 250),  # show-all serves it in full (under its cap)
            (20_000, False, 200),
            (20_000, True, 1000),  # A-r4: Show all is ITSELF bounded
            (0, False, 0),
            (0, True, 0),
        ],
    )
    def test_render_count_law(self, sorted_length: int, show_all: bool, expected: int) -> None:
        out = _node_call(
            "S.renderCount(ARGS[0], ARGS[1])", json.dumps(sorted_length), json.dumps(show_all)
        )
        assert out == expected

    @pytest.mark.parametrize(
        ("shown", "sorted_length", "show_all", "expected"),
        [
            (100, 100, False, ""),  # nothing truncated — no note
            (1000, 1000, True, ""),
            (200, 250, False, " (render cap 200 — use Show all)"),
            (1000, 20_000, True, " (showing first 1000 of 20000 (render cap 1000))"),
        ],
    )
    def test_cap_note_wording_golden(
        self, shown: int, sorted_length: int, show_all: bool, expected: str
    ) -> None:
        out = _node_call(
            "S.capNote(ARGS[0], ARGS[1], ARGS[2])",
            json.dumps(shown),
            json.dumps(sorted_length),
            json.dumps(show_all),
        )
        assert out == expected

    def test_input_cap_rows_passes_contract_payloads_through_untouched(self) -> None:
        rows = [{"rank": i} for i in range(100)]
        out = _node_call("S.inputCapRows(ARGS[0])", json.dumps(rows))
        assert out == {"rows": rows, "dropped": 0}

    def test_input_cap_rows_truncates_and_counts(self) -> None:
        rows = [{"rank": i} for i in range(5100)]
        out = _node_call(
            "(function () { var c = S.inputCapRows(ARGS[0]);"
            " return [c.rows.length, c.dropped, c.rows[0].rank, c.rows[4999].rank]; })()",
            json.dumps(rows),
        )
        assert out == [5000, 100, 0, 4999]

    def test_input_cap_rows_non_array_reads_empty(self) -> None:
        out = _node_call("S.inputCapRows(ARGS[0])", json.dumps(None))
        assert out == {"rows": [], "dropped": 0}

    @pytest.mark.parametrize(
        ("dropped", "payload_length", "expected"),
        [
            (0, 100, ""),
            (-1, 100, ""),  # defensive — never a negative claim
            (100, 5100, " (input capped: first 5000 of 5100 payload rows loaded)"),
        ],
    )
    def test_input_cap_note_wording_golden(
        self, dropped: int, payload_length: int, expected: str
    ) -> None:
        # args ride ONE json array — a bare "-1" argv would read as a node flag
        out = _node_call(
            "S.inputCapNote(ARGS[0][0], ARGS[0][1])",
            json.dumps([dropped, payload_length]),
        )
        assert out == expected

    def test_sort_rows_is_input_bounded(self) -> None:
        """A-r4: rows past INPUT_CAP are never sorted (out-of-contract payload).

        The 5010-row array is generated IN node — a >128 KiB argv would trip
        the kernel's per-arg limit (the point is the bound, not the wire).
        """
        out = _node_call(
            "(function () { var rows = [];"
            ' for (var i = 0; i < 5010; i++) { rows.push({rank: i, tf: "5m"}); }'
            ' return S.sortRows(rows, "rank", "num", "asc").length; })()'
        )
        assert out == 5000

    def test_apply_filters_is_input_bounded(self) -> None:
        """A-r4: rows past INPUT_CAP are never examined (bounded filter work)."""
        out = _node_call(
            "(function () { var rows = [];"
            ' for (var i = 0; i < 5010; i++) { rows.push({rank: i, tf: "5m", metrics: {}}); }'
            " var a = S.applyFilters(rows, {});"
            " return [a.rows.length, a.null_vix_excluded]; })()"
        )
        assert out == [5000, 0]
class TestTrustFilterFloors:
    """DEC-RESULTS-TRUST-HONESTY-001 · B2a — the min_trades / min_dsr floors."""

    def test_min_trades_floor(self) -> None:
        rows = [
            {"rank": 1, "metrics": {"trades": 10}},
            {"rank": 2, "metrics": {"trades": 60}},
        ]
        out = _node_call(
            "S.applyFilters(ARGS[0], ARGS[1])",
            json.dumps(rows),
            json.dumps({"min_trades": 30}),
        )
        assert [r["rank"] for r in out["rows"]] == [2]

    def test_min_dsr_floor_and_missing_value_excluded(self) -> None:
        """A row without metrics.dsr cannot demonstrate an ACTIVE floor —
        excluded from that filtered view only (the tuple-N label law: the
        floored figure is the stored per-tuple DSR)."""
        rows = [
            {"rank": 1, "metrics": {"dsr": 0.97}},
            {"rank": 2, "metrics": {"dsr": 0.10}},
            {"rank": 3, "metrics": {}},
        ]
        out = _node_call(
            "S.applyFilters(ARGS[0], ARGS[1])",
            json.dumps(rows),
            json.dumps({"min_dsr": 0.5}),
        )
        assert [r["rank"] for r in out["rows"]] == [1]
        # No active floor keeps every row (a null/absent filter never excludes).
        out_all = _node_call("S.applyFilters(ARGS[0], ARGS[1])", json.dumps(rows), json.dumps({}))
        assert [r["rank"] for r in out_all["rows"]] == [1, 2, 3]

    def test_boundary_at_floor_is_kept(self) -> None:
        rows = [{"rank": 1, "metrics": {"trades": 30, "dsr": 0.5}}]
        out = _node_call(
            "S.applyFilters(ARGS[0], ARGS[1])",
            json.dumps(rows),
            json.dumps({"min_trades": 30, "min_dsr": 0.5}),
        )
        assert [r["rank"] for r in out["rows"]] == [1]

    def test_dsr_column_is_tuple_n_labeled(self) -> None:
        """The screener DSR column header carries the tuple-N honesty label."""
        cols = _node_call("S.COLUMNS")
        by_key = {c["key"]: c["label"] for c in cols}
        assert by_key["metrics.dsr"] == "DSR (tuple-N)"
