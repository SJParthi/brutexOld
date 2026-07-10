"""Operator-portal consolidation suite (DEC-OPERATOR-PAGE-CONSOLIDATE-001).

/screener + /strategy fold into the Operator page (/) as a single portal —
an expandable "All 100 winners" section + an in-page strategy drawer — while
both standalone pages stay alive as thin deep-link targets consuming the
SAME extracted modules (``bx-screener-panel.js`` / ``bx-strategy-panel.js``)
over the pure URL codec (``bx-portal-state.js``).

This suite carries:

* **T-N1** — node goldens executing the REAL shipped ``bx-portal-state.js``
  codec (parse/serialize/strategy-params/href): the executable twin of the
  pinned ``/strategy?run_id=&rank=`` deep-link contract strings;
* **pure-helper node goldens** — the panel modules' extracted pure cores
  (``bxTfToInterval`` · ``bxEquityLabelText`` · ``bxScreenerCellText`` ·
  ``bxWinnersCountLine`` · ``bxHarvestFilterOptions``), formatter-explicit;
* **T-N7** — the load-generation guard count pin on every async render arm
  of the strategy panel (the gbPollGen precedent — a stale rank-3 response
  can never paint over a newer rank-7 load / a closed drawer / a switched
  run);
* the **bx-oneclick seam pins** — the getTop100 promise memo (one
  /api/v1/oneclick/top100 fetch per run selection, rejected-promise
  self-evict, selectRun + poller-auto-selection invalidation), the
  onRunChange observer registry, lastManifest and latestRun.

HONEST envelope (Golden Rule #10): the codec + pure helpers are EXECUTED
for real under node; the DOM/portal layers are pinned by SOURCE-STRING
presence only (the #1557 Playwright job does not drive /, /screener or
/strategy). Skipped (visibly) where node is absent; the CI node-presence
guard lives in ``test_dashboard_oneclick_frontend.py``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from pathlib import Path
from typing import Any

import pytest

_DASHBOARD = Path(__file__).resolve().parents[2] / "brutex" / "dashboard"
_STATIC = _DASHBOARD / "static"
_TEMPLATES = _DASHBOARD / "templates"
_PORTAL_STATE_JS = _STATIC / "bx-portal-state.js"
_SCREENER_PANEL_JS = _STATIC / "bx-screener-panel.js"
_STRATEGY_PANEL_JS = _STATIC / "bx-strategy-panel.js"
_SCREENER_JS = _STATIC / "bx-screener.js"
_ONECLICK_JS = _STATIC / "bx-oneclick.js"

_NODE = shutil.which("node")

#: The node-side stand-in for the page's money formatter (formatter is
#: INJECTED, so the goldens pin builder logic, not ₹ formatting).
_FMT = "((p) => 'F' + String(p))"


def _node_call(expr: str, *argv: str) -> Any:
    """Run the REAL shipped portal/panel modules under node → JSON result."""
    script = (
        f"const P = require({json.dumps(str(_PORTAL_STATE_JS))});"
        f"const SP = require({json.dumps(str(_SCREENER_PANEL_JS))});"
        f"const ST = require({json.dumps(str(_STRATEGY_PANEL_JS))});"
        f"const S = require({json.dumps(str(_SCREENER_JS))});"
        f"const FMT = {_FMT};"
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


def _js(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T-N1 — the pure URL codec (node-executed)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestPortalParse:
    """bxPortalParse — the ?winners=1&rank=N grammar."""

    def test_empty_search_is_all_defaults(self) -> None:
        out = _node_call("P.bxPortalParse(ARGS[0])", json.dumps(""))
        assert out == {"winners": False, "rank": None, "runId": None, "trade": None}

    def test_question_mark_only_equals_empty(self) -> None:
        assert _node_call("P.bxPortalParse(ARGS[0])", json.dumps("?")) == _node_call(
            "P.bxPortalParse(ARGS[0])", json.dumps("")
        )

    def test_winners_flag_is_strictly_the_literal_1(self) -> None:
        assert _node_call("P.bxPortalParse(ARGS[0])", json.dumps("?winners=1"))["winners"] is True
        for junk in ("?winners=0", "?winners=true", "?winners=yes", "?winners="):
            out = _node_call("P.bxPortalParse(ARGS[0])", json.dumps(junk))
            assert out["winners"] is False, junk

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("3", 3),
            ("0", 1),  # the pinned clamp law: || 1 then max(1, …)
            ("-3", 1),
            ("abc", 1),
            ("7.9", 7),  # parseInt truncation
            ("9e99", 9),  # parseInt reads the leading digits
            ("", 1),
        ],
    )
    def test_rank_clamp_matrix(self, raw: str, expected: int) -> None:
        out = _node_call("P.bxPortalParse(ARGS[0])", json.dumps(f"?rank={raw}"))
        assert out["rank"] == expected

    def test_run_id_passes_through_verbatim(self) -> None:
        out = _node_call("P.bxPortalParse(ARGS[0])", json.dumps("?run_id=r-20260707-fixt01"))
        assert out["runId"] == "r-20260707-fixt01"

    @pytest.mark.parametrize(("raw", "expected"), [("0", 0), ("4", 4), ("-2", 0), ("junk", 0)])
    def test_trade_clamps_non_negative(self, raw: str, expected: int) -> None:
        out = _node_call("P.bxPortalParse(ARGS[0])", json.dumps(f"?trade={raw}"))
        assert out["trade"] == expected


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestPortalSerialize:
    """bxPortalSerialize — patch semantics + foreign-param byte preservation."""

    def test_expand_winners(self) -> None:
        out = _node_call(
            "P.bxPortalSerialize(ARGS[0], ARGS[1])", json.dumps(""), json.dumps({"winners": True})
        )
        assert out == "?winners=1"

    def test_winners_false_deletes_the_key(self) -> None:
        out = _node_call(
            "P.bxPortalSerialize(ARGS[0], ARGS[1])",
            json.dumps("?winners=1&rank=4"),
            json.dumps({"winners": False, "rank": None}),
        )
        assert out == ""

    def test_rank_null_deletes_rank_but_keeps_winners(self) -> None:
        out = _node_call(
            "P.bxPortalSerialize(ARGS[0], ARGS[1])",
            json.dumps("?winners=1&rank=4"),
            json.dumps({"rank": None}),
        )
        assert out == "?winners=1"

    def test_rank_set_forces_winners(self) -> None:
        out = _node_call(
            "P.bxPortalSerialize(ARGS[0], ARGS[1])", json.dumps(""), json.dumps({"rank": 7})
        )
        assert out == "?winners=1&rank=7"

    def test_foreign_params_are_preserved_byte_for_byte(self) -> None:
        """A pre-encoded foreign pair (foo=b%26r) survives VERBATIM — never
        a decode/re-encode round trip."""
        out = _node_call(
            "P.bxPortalSerialize(ARGS[0], ARGS[1])",
            json.dumps("?foo=b%26r&winners=1"),
            json.dumps({"rank": 2}),
        )
        assert out == "?foo=b%26r&winners=1&rank=2"

    def test_deterministic_key_order(self) -> None:
        """Portal keys always serialize winners → rank → run_id → trade
        regardless of the input order."""
        out = _node_call(
            "P.bxPortalSerialize(ARGS[0], ARGS[1])",
            json.dumps("?trade=2&rank=5&winners=1"),
            json.dumps({}),
        )
        assert out == "?winners=1&rank=5&trade=2"

    def test_idempotent_double_apply(self) -> None:
        patch = {"winners": True, "rank": 9}
        once = _node_call(
            "P.bxPortalSerialize(ARGS[0], ARGS[1])",
            json.dumps("?foo=1&rank=3"),
            json.dumps(patch),
        )
        twice = _node_call(
            "P.bxPortalSerialize(ARGS[0], ARGS[1])", json.dumps(once), json.dumps(patch)
        )
        assert once == twice == "?foo=1&winners=1&rank=9"

    def test_round_trip_parse_of_serialized_state(self) -> None:
        search = _node_call(
            "P.bxPortalSerialize(ARGS[0], ARGS[1])",
            json.dumps(""),
            json.dumps({"winners": True, "rank": 12, "runId": "r-x", "trade": 3}),
        )
        out = _node_call("P.bxPortalParse(ARGS[0])", json.dumps(search))
        assert out == {"winners": True, "rank": 12, "runId": "r-x", "trade": 3}

    def test_hostile_run_id_is_encoded_never_raw(self) -> None:
        out = _node_call(
            "P.bxPortalSerialize(ARGS[0], ARGS[1])",
            json.dumps(""),
            json.dumps({"runId": "r-<script>&x=1"}),
        )
        assert "<script>" not in out
        assert "&x=1" not in out  # the & is encoded — never a param split


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestParseStrategyParams:
    """The executable twin of the PINNED /strategy deep-link contract."""

    def test_full_deep_link(self) -> None:
        out = _node_call(
            "P.bxParseStrategyParams(ARGS[0])", json.dumps("?run_id=r-abc&rank=5&trade=2")
        )
        assert out == {"runId": "r-abc", "rank": 5, "tradeIdx": 2}

    def test_defaults_match_the_pinned_law(self) -> None:
        out = _node_call("P.bxParseStrategyParams(ARGS[0])", json.dumps(""))
        assert out == {"runId": None, "rank": 1, "tradeIdx": None}

    @pytest.mark.parametrize(
        ("raw", "expected"), [("0", 1), ("-9", 1), ("abc", 1), ("3", 3), ("", 1)]
    )
    def test_rank_clamp_parity_with_the_source_pin(self, raw: str, expected: int) -> None:
        out = _node_call("P.bxParseStrategyParams(ARGS[0])", json.dumps(f"?rank={raw}"))
        assert out["rank"] == expected

    def test_source_pin_expressions_live_verbatim_in_the_codec(self) -> None:
        js = _js(_PORTAL_STATE_JS)
        assert "state.runId = params.get('run_id');" in js
        assert "state.rank = Math.max(1, parseInt(params.get('rank') || '1', 10) || 1);" in js
        assert "params.get('trade')" in js


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestStrategyHref:
    """bxStrategyHref — the ONE /strategy href builder."""

    def test_basic_href(self) -> None:
        out = _node_call("P.bxStrategyHref(ARGS[0], ARGS[1])", json.dumps("r-abc"), json.dumps(7))
        assert out == "/strategy?run_id=r-abc&rank=7"

    def test_trade_arm(self) -> None:
        out = _node_call(
            "P.bxStrategyHref(ARGS[0], ARGS[1], ARGS[2])",
            json.dumps("r-abc"),
            json.dumps(7),
            json.dumps(0),
        )
        assert out == "/strategy?run_id=r-abc&rank=7&trade=0"

    def test_hostile_run_id_never_survives_raw(self) -> None:
        out = _node_call(
            "P.bxStrategyHref(ARGS[0], ARGS[1])",
            json.dumps("r-<script>alert(1)</script>"),
            json.dumps(1),
        )
        assert "<script>" not in out
        assert "%3Cscript%3E" in out

    def test_rank_is_encoded(self) -> None:
        out = _node_call(
            "P.bxStrategyHref(ARGS[0], ARGS[1])", json.dumps("r-x"), json.dumps("2&evil=1")
        )
        assert "&evil" not in out
        assert "2%26evil%3D1" in out


# ---------------------------------------------------------------------------
# Pure-helper node goldens (the panels' extracted cores)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestTfToInterval:
    """bxTfToInterval — tf label → lake interval; junk → null honesty."""

    @pytest.mark.parametrize(
        ("tf", "expected"),
        [
            ("5m", "5minute"),
            ("1h", "1hour"),
            ("1d", "1day"),
            ("1minute", "1minute"),  # passthrough
            ("3hour", "3hour"),
            ("1day", "1day"),
            ("junk", None),
            ("", None),
            (None, None),
        ],
    )
    def test_matrix(self, tf: object, expected: object) -> None:
        out = _node_call("ST.bxTfToInterval(ARGS[0])", json.dumps(tf))
        assert out == expected


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestEquityLabelText:
    """bxEquityLabelText — the 3-way honest label chooser (exact strings)."""

    def test_minute_path_series(self) -> None:
        out = _node_call("ST.bxEquityLabelText(ARGS[0], ARGS[1])", "true", "false")
        assert out == "minute-path equity — the stored real series (R27)"

    def test_ok_empty_sidecar(self) -> None:
        out = _node_call("ST.bxEquityLabelText(ARGS[0], ARGS[1])", "false", "true")
        assert out == "per-trade close equity (minute-path series empty for this run)"

    def test_unavailable_sidecar(self) -> None:
        out = _node_call("ST.bxEquityLabelText(ARGS[0], ARGS[1])", "false", "false")
        assert out == "per-trade close equity (minute-path unavailable for this run)"


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestScreenerCellText:
    """bxScreenerCellText — injected-deps cell law."""

    _DEPS = (
        "{ fieldValue: S.fieldValue,"
        " vixBandCellText: ((v) => 'VIX[' + JSON.stringify(v) + ']'),"
        " money: FMT }"
    )

    def test_conditions_join(self) -> None:
        row = {"conditions": ["a", "b"]}
        col = {"key": "conditions", "type": "str"}
        out = _node_call(
            f"SP.bxScreenerCellText(ARGS[0], ARGS[1], {self._DEPS})",
            json.dumps(row),
            json.dumps(col),
        )
        assert out == "a × b"

    def test_vix_column_rides_the_injected_builder(self) -> None:
        row = {"vix_band": {"lo": 1, "hi": 2}}
        col = {"key": "vix_band", "type": "vix"}
        out = _node_call(
            f"SP.bxScreenerCellText(ARGS[0], ARGS[1], {self._DEPS})",
            json.dumps(row),
            json.dumps(col),
        )
        assert out == 'VIX[{"lo":1,"hi":2}]'

    def test_money_column_rides_the_injected_formatter(self) -> None:
        row = {"metrics": {"net_pnl_paisa": 1250}}
        col = {"key": "metrics.net_pnl_paisa", "type": "num", "money": True}
        out = _node_call(
            f"SP.bxScreenerCellText(ARGS[0], ARGS[1], {self._DEPS})",
            json.dumps(row),
            json.dumps(col),
        )
        assert out == "F1250"

    def test_null_reads_the_honest_dash(self) -> None:
        out = _node_call(
            f"SP.bxScreenerCellText(ARGS[0], ARGS[1], {self._DEPS})",
            json.dumps({"tf": None}),
            json.dumps({"key": "tf", "type": "str"}),
        )
        assert out == "—"

    def test_full_precision_passthrough_never_rounded(self) -> None:
        row = {"metrics": {"sharpe": 1.23456789012345}}
        col = {"key": "metrics.sharpe", "type": "num"}
        out = _node_call(
            f"SP.bxScreenerCellText(ARGS[0], ARGS[1], {self._DEPS})",
            json.dumps(row),
            json.dumps(col),
        )
        assert out == "1.23456789012345"


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestWinnersCountLine:
    """bxWinnersCountLine — the [W2-5] honest numerator/denominator law."""

    _NOTE = "S.nullVixNote"

    def test_uncapped_table(self) -> None:
        out = _node_call(f"SP.bxWinnersCountLine(100, 100, 100, 200, 0, {self._NOTE})")
        assert out == "100 of 100 shown"

    def test_capped_table_names_the_cap(self) -> None:
        out = _node_call(f"SP.bxWinnersCountLine(200, 500, 500, 200, 0, {self._NOTE})")
        assert out == "200 of 500 shown (render cap 200 — use Show all)"

    def test_null_row_count_reads_question_mark(self) -> None:
        out = _node_call(f"SP.bxWinnersCountLine(3, null, 3, 200, 0, {self._NOTE})")
        assert out == "3 of ? shown"

    def test_null_vix_suffix_rides_the_shared_note(self) -> None:
        out = _node_call(f"SP.bxWinnersCountLine(5, 10, 5, 200, 2, {self._NOTE})")
        assert out.startswith("5 of 10 shown ")
        golden = _node_call(f"{self._NOTE}(2)")
        assert out == "5 of 10 shown " + golden


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestHarvestFilterOptions:
    """bxHarvestFilterOptions — the absent-key instrument law + dedup/sort."""

    def test_instrument_options_only_from_rows_carrying_the_key(self) -> None:
        rows = [
            {"tf": "5m", "direction": "long", "instrument": "NSE-NIFTY"},
            {"tf": "5m", "direction": "short"},  # pre-instrument row: no option
            {"tf": "1h", "direction": "long", "instrument": ""},  # blank: no option
        ]
        out = _node_call("SP.bxHarvestFilterOptions(ARGS[0])", json.dumps(rows))
        assert out["instrument"] == ["NSE-NIFTY"]

    def test_dedup_and_sort(self) -> None:
        rows = [
            {"tf": "5m", "direction": "short", "instrument": "NSE-NIFTY"},
            {"tf": "1h", "direction": "long", "instrument": "BSE-SENSEX"},
            {"tf": "5m", "direction": "long", "instrument": "NSE-NIFTY"},
        ]
        out = _node_call("SP.bxHarvestFilterOptions(ARGS[0])", json.dumps(rows))
        assert out == {
            "tf": ["1h", "5m"],
            "direction": ["long", "short"],
            "instrument": ["BSE-SENSEX", "NSE-NIFTY"],
        }

    def test_non_array_is_empty(self) -> None:
        out = _node_call("SP.bxHarvestFilterOptions(null)")
        assert out == {"tf": [], "direction": [], "instrument": []}


# ---------------------------------------------------------------------------
# T-N7 — the load-generation guard (source pin, count-locked)
# ---------------------------------------------------------------------------


class TestLoadGenerationGuard:
    def test_every_async_render_arm_opens_with_the_gen_guard(self) -> None:
        """T-N7: the `gen !== state.gen` guard on EVERY async render arm of
        the strategy panel (loadCandles · loadTrades · loadEquity then+catch
        · loadRow then+catch · loadManifestSymbol · the mountFromLocation
        runs fallback) — count-locked so a new fetch arm cannot land
        guardless and a refactor cannot silently drop one."""
        js = _js(_STRATEGY_PANEL_JS)
        guards = js.count("if (gen !== state.gen) { return; }")
        assert guards == 8, f"expected 8 gen guards, found {guards}"
        # Every guard captures the generation BEFORE its fetch:
        assert js.count("var gen = state.gen;") >= 6
        # update() owns the ONE bump:
        assert "state.gen += 1;" in js

    def test_mount_once_update_api(self) -> None:
        """Graft C.3: listeners bind once in mount(); every open/re-rank
        goes through update() — leak-free by construction."""
        js = _js(_STRATEGY_PANEL_JS)
        assert "mount: bxStrategyPanelMount" in js
        assert "update: bxStrategyPanelUpdate" in js
        assert "return; // mount-once: listeners are never re-bound" in js


# ---------------------------------------------------------------------------
# bx-oneclick seam pins (the one-fetch-N-consumers memo + run observer)
# ---------------------------------------------------------------------------


class TestOneclickSeam:
    def test_gettop100_memo_is_keyed_and_self_evicting(self) -> None:
        js = _js(_ONECLICK_JS)
        assert "var top100Memo = { runId: null, promise: null };" in js
        assert "if (top100Memo.runId === runId && top100Memo.promise) {" in js
        # a REJECTED promise self-evicts so Retry genuinely refetches:
        assert "a REJECTED promise self-evicts so Retry genuinely refetches" in js
        assert "top100Memo = { runId: null, promise: null };" in js

    def test_selectrun_invalidates_and_notifies(self) -> None:
        js = _js(_ONECLICK_JS)
        select_run = js[js.index("selectRun: function (runId) {") :]
        body = select_run[: select_run.index("loadRunScoped();") + 60]
        assert "top100Memo = { runId: null, promise: null };" in body
        assert "notifyRunChange(runId);" in select_run[:600]

    def test_runs_poller_first_auto_selection_notifies(self) -> None:
        """Judge-3 MF#2: the programmatic FIRST auto-selection (no DOM
        'change' event fires) invalidates the memo AND notifies observers."""
        js = _js(_ONECLICK_JS)
        arm = js[js.index("if (!state.selectedRunId && data.latest_run_id) {") :]
        arm = arm[: arm.index("notifyRunChange(state.selectedRunId);") + 40]
        assert "top100Memo = { runId: null, promise: null };" in arm
        assert "loadRunScoped();" in arm  # invalidate BEFORE the scoped fetches

    def test_public_seam_exports(self) -> None:
        js = _js(_ONECLICK_JS)
        assert "getTop100: getTop100," in js
        assert "onRunChange: function (cb) {" in js
        assert "lastManifest: function () {" in js
        assert "latestRun: latestRun," in js

    def test_refresh_top100_consumes_the_memo(self) -> None:
        js = _js(_ONECLICK_JS)
        refresh = js[js.index("function refreshTop100() {") :]
        refresh = refresh[: refresh.index("function renderTradeRows")]
        assert "getTop100(state.selectedRunId)" in refresh
        # Its pinned render body stays byte-identical (the setOc lines):
        assert 'setOc("top100_sort_basis", sortBasisText(payload.preset));' in refresh
        assert (
            'setOc("tested_rank_line", "#" + String(payload.rows[0].rank) + " of " + String(payload.row_count) + " stored winners");'
            in refresh
        )

    def test_last_manifest_is_fed_by_render_manifest(self) -> None:
        js = _js(_ONECLICK_JS)
        assert "lastManifestPayload = payload; // the BXOneclick.lastManifest() source" in js

    def test_each_subscriber_runs_in_its_own_try_catch(self) -> None:
        js = _js(_ONECLICK_JS)
        notify = js[js.index("function notifyRunChange(runId) {") :]
        notify = notify[: notify.index("}\n  }") + 6]
        assert "try {" in notify
        assert "one bad subscriber never breaks the rest" in notify


class TestNoNewBackgroundWork:
    def test_extracted_modules_carry_no_pollers_or_timers(self) -> None:
        """T-N6 (the commit-1 half): the codec + panel modules run zero
        background work — no poller, no interval, no rAF loop (the trades
        cascade uses ONE bounded setTimeout fallback, which is not a loop).
        Hidden-tab silence is inherited from the untouched existing pollers."""
        for path in (_PORTAL_STATE_JS, _SCREENER_PANEL_JS, _STRATEGY_PANEL_JS):
            js = _js(path)
            assert "createPoller(" not in js, path.name
            assert "setInterval(" not in js, path.name
            assert "requestAnimationFrame(" not in js, path.name


class TestPanelModuleWiring:
    def test_screener_panel_exposes_the_mount_api(self) -> None:
        js = _js(_SCREENER_PANEL_JS)
        assert "window.BXScreenerPanel = {" in js
        assert "RENDER_CAP: RENDER_CAP," in js
        assert "mount: bxScreenerPanelMount," in js
        assert "mountFromLocation: bxScreenerPanelMountFromLocation" in js
        assert 'data-bx-autopanel="screener"' in js  # the self-mount gate

    def test_strategy_panel_exposes_the_mount_api(self) -> None:
        js = _js(_STRATEGY_PANEL_JS)
        assert "window.BXStrategyPanel = {" in js
        assert "TRADES_LIMIT: TRADES_LIMIT," in js
        assert 'data-bx-autopanel="strategy"' in js  # the self-mount gate

    def test_panel_default_seams_reach_the_shared_singletons(self) -> None:
        sc = _js(_SCREENER_PANEL_JS)
        assert "deps.getTop100 || window.BXOneclick.getTop100" in sc
        st = _js(_STRATEGY_PANEL_JS)
        assert "deps.getTop100 || window.BXOneclick.getTop100" in st
        assert "window.BXOneclick.lastManifest" in st

    def test_trades_chart_link_rides_the_one_href_builder(self) -> None:
        """The per-trade chart deep link is built by BXPortalState.strategyHref
        (the third inline copy is dead)."""
        js = _js(_STRATEGY_PANEL_JS)
        assert "window.BXPortalState.strategyHref(state.runId || '', String(state.rank), i)" in js
        assert re.search(r"'/strategy\?run_id=' \+", js) is None  # no inline copy
