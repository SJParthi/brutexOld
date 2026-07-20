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

    def test_winners_false_alone_kills_a_lingering_rank(self) -> None:
        """E-N1 (adversarial-review fold): the coupling law's contrapositive
        — rank ⇒ winners ⊢ ¬winners ⇒ ¬rank. Previously a FULL no-op: the
        URL-carried rank survived the delete and re-added winners."""
        out = _node_call(
            "P.bxPortalSerialize(ARGS[0], ARGS[1])",
            json.dumps("?winners=1&rank=5"),
            json.dumps({"winners": False}),
        )
        assert out == ""

    def test_winners_false_with_an_explicit_rank_still_wins(self) -> None:
        """A rank set in the SAME patch is applied AFTER the contrapositive
        delete — it survives and re-forces winners (patch precedence)."""
        out = _node_call(
            "P.bxPortalSerialize(ARGS[0], ARGS[1])",
            json.dumps("?winners=1&rank=5"),
            json.dumps({"winners": False, "rank": 7}),
        )
        assert out == "?winners=1&rank=7"

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
    """bxWinnersCountLine — the [W2-5] honest numerator/denominator law,
    composed over the REAL BXScreener A-r4 cap-law primitives (capNote /
    inputCapNote / nullVixNote — the #1406 port at the merge-reconcile;
    ``S`` below IS the shipped bx-screener.js, never a stub)."""

    def test_uncapped_table(self) -> None:
        out = _node_call("SP.bxWinnersCountLine(100, 100, 100, false, 0, 100, 0, S)")
        assert out == "100 of 100 shown"

    def test_capped_table_names_the_cap(self) -> None:
        out = _node_call("SP.bxWinnersCountLine(200, 500, 500, false, 0, 500, 0, S)")
        assert out == "200 of 500 shown (render cap 200 — use Show all)"

    def test_show_all_is_itself_bounded_and_named(self) -> None:
        """A-r4: even Show-all caps at SHOW_ALL_CAP=1000 with the honest
        'showing first N of M' note — never an unbounded render."""
        out = _node_call("SP.bxWinnersCountLine(1000, 5000, 2000, true, 0, 5000, 0, S)")
        assert out == "1000 of 5000 shown (showing first 1000 of 2000 (render cap 1000))"

    def test_input_cap_truncation_is_named(self) -> None:
        """A-r4: an out-of-contract payload cut at INPUT_CAP=5000 is NAMED
        on the count line, never silently shrunk."""
        out = _node_call("SP.bxWinnersCountLine(200, 6000, 5000, false, 1000, 6000, 0, S)")
        assert out == (
            "200 of 6000 shown (render cap 200 — use Show all)"
            " (input capped: first 5000 of 6000 payload rows loaded)"
        )

    def test_null_row_count_reads_question_mark(self) -> None:
        out = _node_call("SP.bxWinnersCountLine(3, null, 3, false, 0, 3, 0, S)")
        assert out == "3 of ? shown"

    def test_null_vix_suffix_rides_the_shared_note(self) -> None:
        out = _node_call("SP.bxWinnersCountLine(5, 10, 5, false, 0, 5, 2, S)")
        assert out.startswith("5 of 10 shown ")
        golden = _node_call("S.nullVixNote(2)")
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
        · loadRow then+catch · loadManifestSymbol then+catch · the
        mountFromLocation runs fallback · the B1 loadBaseline arm — the
        S2 club-fold reconcile port of DEC-RESULTS-TRUST-HONESTY-001's
        buy-&-hold benchmark, 9 → 10) — count-locked. HONEST scope
        (COV-N2 reword; fix-round-2 reword — the earlier "the per-arm source
        pins around it do" over-claimed): the count pin catches a DROPPED
        guard and forces a conscious update when an arm is added; proving a
        NEW arm carries its guard takes a dedicated per-arm pin — the
        round-1 manifest-catch arm's lives in the test below."""
        js = _js(_STRATEGY_PANEL_JS)
        guards = js.count("if (gen !== state.gen) { return; }")
        assert guards == 10, f"expected 10 gen guards, found {guards}"
        # Every guard captures the generation BEFORE its fetch:
        assert js.count("var gen = state.gen;") >= 6
        # update() owns the ONE bump:
        assert "state.gen += 1;" in js

    def test_manifest_catch_arm_carries_its_own_gen_guard(self) -> None:
        """Fix round 2 (seccov NIT-1): the round-1 manifest CATCH arm's gen
        guard was covered only by the aggregate count(9) — pin it
        individually (the guard opens the arm, BEFORE the honest next())."""
        js = _js(_STRATEGY_PANEL_JS)
        arm = js[js.index("window.BXOneclick.getManifestOnce(state.runId, 12000)") :]
        arm = arm[: arm.index("function bxStrategyPanelMount(")]
        catch_arm = arm[arm.index(".catch(function () {") :]
        guard_at = catch_arm.index("if (gen !== state.gen) { return; }")
        next_at = catch_arm.index("next(); // honest: symbol stays null")
        assert guard_at < next_at

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

    def test_manifest_inflight_dedupe_is_settle_evicting(self) -> None:
        """P-M1 (adversarial-review fold): a cold deep-link drawer open and
        the manifest poller share ONE in-flight /manifest promise — the
        second concurrent caller never pays a duplicate fetch. The memo
        self-evicts on SETTLE (resolve AND reject), so it is an in-flight
        dedupe only — the poller always refetches fresh on its next tick,
        never a stale-manifest cache."""
        js = _js(_ONECLICK_JS)
        assert "var manifestInflight = { runId: null, promise: null };" in js
        assert "function getManifestOnce(runId, timeoutMs) {" in js
        assert "promise.then(evict, evict); // settle-evict — an in-flight dedupe only" in js
        assert "getManifestOnce: getManifestOnce," in js  # exported for the panel
        # The manifest poller rides the SAME dedupe:
        assert "return getManifestOnce(state.selectedRunId, timeoutMs);" in js

    def test_strategy_panel_cold_manifest_arm_rides_the_dedupe(self) -> None:
        """The panel's cold-start manifest one-shot consumes the shared
        in-flight promise (no second concurrent /manifest fetch), and a
        manifest FAILURE still advances to loadRow — the story card loads
        and the chart names the missing symbol (never a silently dead
        drawer)."""
        st = _js(_STRATEGY_PANEL_JS)
        assert "window.BXOneclick.getManifestOnce(state.runId, 12000)" in st
        assert "next(); // honest: symbol stays null — the chart names the miss" in st

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
        # A-r4 merge-reconcile (#1406 port): the cap constants live ONLY in
        # BXScreener — the panel's former duplicate RENDER_CAP export is
        # gone and must never return (ONE cap-law source of truth).
        assert "RENDER_CAP: RENDER_CAP," not in js
        assert "window.BXScreener.renderCount(sorted.length, state.showAll)" in js
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


# ---------------------------------------------------------------------------
# T-N2 — the hook-parity fence (ONE canonical inventory, asserted on BOTH
# copies — the house mirror-pin pattern)
# ---------------------------------------------------------------------------

#: The ONE canonical panel-hook inventory (judge-3 MF#5 — a single
#: inventory object, never two hand-kept lists). The screener block lives in
#: screener.html AND v29's #ocWinnersSection; the strategy block lives in
#: strategy.html AND v29's #ocStrategyDrawer. Drawer-head chrome
#: (ocDrawerPrev/RankPill/Next/OpenFull/Close) is portal-owned, v29-only —
#: deliberately OUTSIDE this inventory.
_PANEL_INVENTORY: dict[str, dict[str, tuple[str, ...]]] = {
    "screener": {
        "ids": (
            "scRunId",
            "scDetail",
            "scTf",
            "scDir",
            "scInstrument",
            "scMinWin",
            "scMinPf",
            "scMinTrades",
            "scMinDsr",
            "scMaxDd",
            "scVixLo",
            "scVixHi",
            "scCount",
            "scSortBasis",
            "scPanel",
            "scTable",
            "scHead",
            "scRows",
            "scShowAllWrap",
            "scShowAll",
        ),
        "strings": (
            ">Timeframe<",
            ">Direction<",
            ">Instrument<",
            ">Min win %<",
            ">Min PF<",
            ">Min trades<",
            ">Min DSR (tuple-N)<",
            ">Max |DD| (paisa)<",
            ">VIX band ≥<",
            ">VIX band ≤<",
            "awaiting real run artifact — run a sweep from the Operator page Run controls",
            "no rows loaded yet — awaiting real run artifact",
            "sort basis: artifact preset (default min_dd_first — smallest max-drawdown first)"
            " — differs from /results (Rank-by metric, descending); column clicks re-sort"
            " this page only",
            # A-r4 (#1406, merge-reconcile): "Show all" is itself BOUNDED at
            # BXScreener.SHOW_ALL_CAP — the copy says so on BOTH surfaces.
            "show all rows (itself render-capped at 1000 — an oversized/corrupt artifact never freezes the tab)",
        ),
        "killed": (),
    },
    "strategy": {
        "ids": (
            "stHeadCard",
            "stRank",
            "stName",
            "stRunId",
            "stInstr",
            "stTf",
            "stDir",
            "stArm",
            "stMask",
            "stFamily",
            "stChartCoords",
            "stChartError",
            "stChartHost",
            "stArrowNote",
            "stEquityLabel",
            "stEquityHost",
            "stTradesSection",
            "stTradesSource",
            "stTradesNote",
            "stTradesError",
            "stTradesBody",
            "stPrev",
            "stPage",
            "stNext",
            "stSignalOnly",
            "stNet",
            "stMaxDd",
            "stSharpe",
            "stDsr",
            "stCpcv",
            "stWinRate",
            "stPf",
            "stTrades",
            "stBench",
            "stVixBand",
            "stBenchDetail",
            "stArith",
            "stExpectancy",
            "stRecovery",
            "stLossStreak",
            "stMae",
            "stMfe",
            "stMinuteDd",
            "stExitMix",
            "stWorst5",
            "stTrendSplit",
            "stRegimeWonder",
            "stCascade",
            "stCascadeDeep",
            # DEC-RESILIENCE-STREAKS-001 (club-O re-port onto the refold):
            # the losing-streak resilience hero panel — both copies.
            "stResilienceSection",
            "stResWorstStreak",
            "stResWorstStreakSub",
            "stResCost",
            "stResCostSub",
            "stResUnderwater",
            "stResUnderwaterBadge",
            "stResWorstMonth",
            "stResWorstMonthSub",
            "stResWinSpread",
            "stResGuide",
            "stResLossesIn",
            "stResLocate",
        ),
        "explains": (
            "instrument",
            "gross_signal_pnl",
            "max_dd_paisa",
            "sharpe",
            "dsr",
            "cpcv_pass_pct",
            "win_rate",
            "profit_factor",
            "trades",
            "vix_band",
            "expectancy_paisa",
            "recovery_factor",
            "max_consecutive_losses",
            "mae_avg_paisa",
            "mfe_avg_paisa",
            "max_dd_minute_path_paisa",
            "exit_mix",
            "worst5_days",
            "by_trend",
            "one_regime_wonder",
            "cascade_status",
            # DEC-RESILIENCE-STREAKS-001 hover-explain hooks.
            "worst_streak_pnl",
            "longest_underwater",
            "worst_loss_month",
            "win_rate_year_spread",
            "resilience_guide",
        ),
        "strings": (
            "Edge — gross signal P&amp;L",
            "signal-only — costs modeled in the options-translation track",
            "source: none",
            "benchmark: awaiting stored baseline artifact (R27)",
            "awaiting real run artifact — trades appear once the run's results-depth sidecars land",
            "family drill-down (R34) is a named later PR",
            # DEC-RESILIENCE-STREAKS-001: the honest signal-only caveat + the
            # distinct-runs labels (by-COUNT vs by-₹) on BOTH copies.
            "gross of brokerage &amp; taxes · per-unit (no sizing)",
            "Worst cold streak",
            "Worst streak (by ₹)",
        ),
        "killed": (
            "Edge — net P&amp;L",
            "That streak cost",  # the anaphoric conflation header (MINOR-1)
            "works when VIX between",
            "minute-path equity lands with P3",
        ),
    },
}


def _v29_block(start_id: str, end_marker: str) -> str:
    html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
    start = html.index(f'id="{start_id}"')
    end = html.index(end_marker, start)
    return html[start:end]


class TestHookParityFence:
    """T-N2: BOTH copies of each panel block carry the SAME hooks —
    screener.html ↔ v29 #ocWinnersSection, strategy.html ↔ v29
    #ocStrategyDrawer — asserted against the ONE canonical inventory."""

    @pytest.mark.parametrize(
        ("panel", "template", "v29_start", "v29_end"),
        [
            ("screener", "screener.html", "ocWinnersSection", 'id="ocChainSection"'),
            ("strategy", "strategy.html", "ocStrategyDrawer", 'id="modalBack"'),
        ],
    )
    def test_both_copies_carry_the_inventory(
        self, panel: str, template: str, v29_start: str, v29_end: str
    ) -> None:
        inventory = _PANEL_INVENTORY[panel]
        page = (_TEMPLATES / template).read_text(encoding="utf-8")
        block = _v29_block(v29_start, v29_end)
        for html, where in ((page, template), (block, f"v29 #{v29_start} block")):
            for anchor in inventory["ids"]:
                assert f'id="{anchor}"' in html, f"{where}: missing #{anchor}"
            for token in inventory.get("explains", ()):
                assert f'data-explain="{token}"' in html, f"{where}: missing explain {token}"
            for text in inventory["strings"]:
                assert text in html, f"{where}: missing copy {text!r}"
            for dead in inventory["killed"]:
                assert dead not in html, f"{where}: killed token returned {dead!r}"

    def test_the_drawer_chrome_is_v29_only(self) -> None:
        """The portal-owned drawer head never leaks into strategy.html."""
        page = (_TEMPLATES / "strategy.html").read_text(encoding="utf-8")
        for chrome in (
            "ocDrawerPrev",
            "ocDrawerNext",
            "ocDrawerRankPill",
            "ocDrawerOpenFull",
            "ocDrawerClose",
            "ocDrawerBack",
        ):
            assert chrome not in page, chrome


# ---------------------------------------------------------------------------
# T-N3 — document-wide id uniqueness (EMPTY allowed baseline)
# ---------------------------------------------------------------------------


class TestIdUniqueness:
    def test_v29_has_zero_duplicate_ids(self) -> None:
        """T-N3 (judge-1 MF#5): every id in v29.html appears EXACTLY once —
        the allowed-baseline set is EMPTY (v29 had zero internal duplicates
        before the fold; the ONE cross-page collision — ocTradesSource — was
        cleared by the stTradesSource rename)."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        ids = re.findall(r'\bid="([^"]+)"', html)
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        assert dupes == [], f"duplicate ids in v29.html: {dupes}"

    @pytest.mark.parametrize("template", ["screener.html", "strategy.html"])
    def test_standalone_pages_stay_duplicate_free(self, template: str) -> None:
        html = (_TEMPLATES / template).read_text(encoding="utf-8")
        ids = re.findall(r'\bid="([^"]+)"', html)
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        assert dupes == [], f"duplicate ids in {template}: {dupes}"


# ---------------------------------------------------------------------------
# T-N4 — portal wiring source pins (browser glue — presence, not execution)
# ---------------------------------------------------------------------------

_PORTAL_JS = _STATIC / "bx-operator-portal.js"


class TestPortalWiring:
    def test_pushstate_exactly_on_the_drawer_open_arm(self) -> None:
        js = _js(_PORTAL_JS)
        assert js.count("history.pushState(") == 1  # drawer open — the ONLY push
        # NEW-R2-3: the open arm is the only push-CAPABLE arm; a re-open from
        # the rank-less entry a prior close left behind replaces instead
        # (open/close cycles never accumulate duplicate history entries).
        assert (
            "writeUrl({ winners: true, rank: rank }, !state.reopenReplaces);"
            " // the ONLY pushState-capable arm" in js
        )
        assert js.count("history.replaceState(") == 1  # the one shared write funnel

    def test_close_never_navigates(self) -> None:
        js = _js(_PORTAL_JS)
        assert "history.back()" not in js  # close is a replaceState, NEVER a nav
        assert "writeUrl({ rank: null }, false);" in js

    def test_reconciling_flag_suppresses_history_writes(self) -> None:
        js = _js(_PORTAL_JS)
        assert "if (state.reconciling) {" in js
        assert "'popstate'" in js or '"popstate"' in js
        assert "state.reconciling = true;" in js
        assert "state.reconciling = false;" in js

    def test_a11y_and_focus_machinery(self) -> None:
        js = _js(_PORTAL_JS)
        assert '"Escape"' in js
        assert 'setAttribute("inert", "")' in js
        assert 'removeAttribute("inert")' in js
        assert "bx-drawer-open" in js  # scroll lock class
        assert "state.lastFocus" in js  # focus restore
        assert 'ev.key !== "Tab"' in js  # the focus trap

    def test_arrow_surf_skips_form_fields(self) -> None:
        js = _js(_PORTAL_JS)
        assert 'tag === "input" || tag === "select" || tag === "textarea"' in js
        assert '"ArrowLeft"' in js
        assert '"ArrowRight"' in js

    def test_run_change_rides_the_observer_seam_only(self) -> None:
        """Judge-3 MF#2: onRunChange, NEVER a DOM 'change' listener (the runs
        poller's programmatic auto-selection fires no 'change' event)."""
        js = _js(_PORTAL_JS)
        assert "window.BXOneclick.onRunChange(onRunChanged);" in js
        assert "addEventListener('change'" not in js
        assert 'addEventListener("change"' not in js

    def test_run_change_handler_order_close_strip_reload(self) -> None:
        """Judge-1 MF#3: close drawer → strip rank(+run_id) → reload winners
        through the freshly-invalidated memo — token-order pinned."""
        js = _js(_PORTAL_JS)
        handler = js[js.index("function onRunChanged(runId) {") :]
        handler = handler[: handler.index("winnersCtl.loadRows(runId);") + 30]
        close_at = handler.index("closeDrawer();")
        strip_at = handler.index("writeUrl({ rank: null, runId: null }, false);")
        reload_at = handler.index("winnersCtl.loadRows(runId);")
        assert close_at < strip_at < reload_at

    def test_open_full_link_hidden_until_run_resolved(self) -> None:
        """Judge-3 MF#3: no dead /strategy link window."""
        js = _js(_PORTAL_JS)
        assert "full.hidden = false;" in js
        assert "full.hidden = true;" in js
        assert "window.BXPortalState.strategyHref(state.runId, rank)" in js
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert '<a id="ocDrawerOpenFull" href="/strategy" hidden>' in html

    def test_deep_link_resolves_the_run_before_opening(self) -> None:
        """Judge-1 MF#4 ordering: parse → resolve run → expand + lazy-mount
        winners → open drawer (through the panel's gen guard)."""
        js = _js(_PORTAL_JS)
        assert "restoreFromUrl(window.BXPortalState.parse(window.location.search));" in js
        restore = js[js.index("function restoreFromUrl(parsed) {") :]
        select_at = restore.index("window.BXOneclick.selectRun(parsed.runId);")
        finish_at = restore.index("finishRestore(parsed);")
        assert select_at < finish_at
        finish = js[js.index("function finishRestore(parsed) {") :]
        finish = finish[: finish.index("state.reconciling = false;")]
        assert finish.index("expandWinners();") < finish.index("openDrawer(parsed.rank);")
        # …and the latest-run fallback is the ONE /runs one-shot.
        assert "window.BXOneclick.latestRun()" in js

    def test_prev_next_disable_law(self) -> None:
        """Judge-2 MF#8: prev iff rank > 1 (miss surfs back to the last REAL
        rank); next iff rank < rowCount; rowCount 0 ⇒ both disabled."""
        js = _js(_PORTAL_JS)
        assert "prev.disabled = state.rank === null || rc === 0 || state.rank <= 1;" in js
        assert "next.disabled = state.rank === null || rc === 0 || state.rank >= rc;" in js
        assert "openDrawer(Math.min(state.rank - 1, rc));" in js  # the miss law

    def test_lazy_mount_and_mount_once_update(self) -> None:
        js = _js(_PORTAL_JS)
        assert "state.winnersMounted = true; // lazy: first expand pays the mount" in js
        assert "window.BXScreenerPanel.mount({" in js
        assert "window.BXStrategyPanel.mount({ animate: true });" in js
        assert (
            "window.BXStrategyPanel.update({ runId: state.runId, rank: rank, tradeIdx: null });"
            in js
        )

    def test_combos_intercept_leaves_modified_clicks_to_the_real_href(self) -> None:
        js = _js(_PORTAL_JS)
        assert "ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || ev.button !== 0" in js
        assert "ev.preventDefault();" in js

    def test_inert_on_every_other_page(self) -> None:
        js = _js(_PORTAL_JS)
        assert 'if (!byId("ocWinnersSection")) {' in js


# ---------------------------------------------------------------------------
# Adversarial-review fix-round pins (refuter-majority-confirmed findings —
# each class names the finding it kills; source pins per the #1557 envelope,
# node-executed where the logic is pure)
# ---------------------------------------------------------------------------


class TestDeepLinkRestoreLatch:
    """C-M1: on `/?winners=1&rank=N` WITHOUT run_id, the runs poller's /runs
    fetch is issued BEFORE the portal's latestRun() and usually resolves
    first — its onRunChanged ran OUTSIDE any reconciling window and stripped
    the deep link's rank via replaceState. The fix is a distinct
    restore-pending LATCH (the reconciling flag alone cannot cover the
    window)."""

    def test_latch_set_before_any_async_restore_arm(self) -> None:
        js = _js(_PORTAL_JS)
        restore = js[js.index("function restoreFromUrl(parsed) {") :]
        latch_at = restore.index("state.restorePending = true;")
        select_at = restore.index("window.BXOneclick.selectRun(parsed.runId);")
        latest_at = restore.index("window.BXOneclick.latestRun()")
        assert latch_at < select_at
        assert latch_at < latest_at

    def test_latch_releases_only_in_finish_restore_finally(self) -> None:
        js = _js(_PORTAL_JS)
        assert js.count("state.restorePending = false;") == 1
        finish = js[js.index("function finishRestore(parsed) {") :]
        finish = finish[: finish.index("function restoreFromUrl")]
        assert "state.restorePending = false; // the C-M1 latch releases HERE only" in finish
        assert finish.index("} finally {") < finish.index("state.restorePending = false;")

    def test_run_change_strip_is_guarded_by_the_latch(self) -> None:
        """The killing pin: the strip must sit INSIDE the latch guard so the
        poller's first auto-selection cannot rewrite the deep link away
        while the restore is in flight — the opened drawer ends with the
        deep-link URL + history entry intact."""
        js = _js(_PORTAL_JS)
        handler = js[js.index("function onRunChanged(runId) {") :]
        handler = handler[: handler.index("winnersCtl.loadRows(runId);") + 30]
        guard_at = handler.index("if (!state.restorePending) {")
        strip_at = handler.index("writeUrl({ rank: null, runId: null }, false);")
        assert guard_at < strip_at
        between = handler[guard_at + len("if (!state.restorePending) {") : strip_at]
        assert "}" not in between  # the strip is INSIDE the guard block

    def test_arm_a_sync_throw_releases_the_latch_through_finish_restore(self) -> None:
        """Fix round 2 (MINOR-2/NEW-R2R3-1): a sync throw out of selectRun
        (a dead bx-poll global) used to strand the latch TRUE forever —
        every later run switch then kept a stale rank/run_id in the URL.
        The release is ARM-SCOPED: arm A's fully-synchronous prefix runs in
        a finally funneled through finishRestore (the ONE release site — the
        count==1 pin above stands); the whole-body variant is REJECTED
        (NEW-R2R3-2 — it would release while arm 3's latestRun is in flight
        and re-open C-M1). EXECUTED twin: TestPortalDriveExecuted."""
        js = _js(_PORTAL_JS)
        restore = js[js.index("function restoreFromUrl(parsed) {") :]
        arm_a = restore[
            restore.index("if (parsed.runId) {") : restore.index(
                'var select = byId("ocRunSelect");'
            )
        ]
        select_at = arm_a.index("window.BXOneclick.selectRun(parsed.runId);")
        # the reconciling reset stays innermost; the release finally wraps it:
        reconcile_off_at = arm_a.index("state.reconciling = false;")
        finish_at = arm_a.index("finishRestore(parsed);")
        assert select_at < reconcile_off_at < finish_at
        # BOTH finallys present in the arm (inner reconciling + outer release):
        assert arm_a.count("} finally {") == 2

    def test_arm_c_sync_throw_guard_is_catch_only_and_rethrows(self) -> None:
        """Fix round 2 (MINOR-2 twin): a sync throw CONSTRUCTING arm 3's
        chain (latestRun itself — the same dead-global class) releases the
        latch via catch-only + rethrow — deliberately NOT a finally, which
        would release while a HEALTHY chain is still in flight (C-M1)."""
        js = _js(_PORTAL_JS)
        arm_c = js[js.index("window.BXOneclick.latestRun()") :]
        arm_c = arm_c[: arm_c.index("function init() {")]
        assert "} catch (err) {" in arm_c
        catch_body = arm_c[arm_c.index("} catch (err) {") :]
        finish_at = catch_body.index("finishRestore(parsed);")
        rethrow_at = catch_body.index("throw err;")
        assert finish_at < rethrow_at

    def test_latest_run_never_clobbers_a_latch_window_adopted_run(self) -> None:
        """Fix round 2 (NIT-1 ≡ edges F3): arm 3's `.then` adopts
        latest_run_id ONLY when no run was adopted during the latch window —
        the poller-selected run wins, so the drawer/winners and the page
        surfaces stay on ONE run (the mismatch did NOT self-heal: the runs
        poller auto-notifies only on FIRST selection)."""
        js = _js(_PORTAL_JS)
        assert "if (!state.runId && runs && runs.latest_run_id) {" in js
        assert "state.runId = String(runs.latest_run_id);" in js


class TestDrawerHiddenState:
    """E-M1/S-N1: the drawer + backdrop ship with the `hidden` attribute
    (UA display:none) managed by the portal — a panels-CSS load failure now
    degrades to an INVISIBLE drawer, never an unstyled static skeleton
    dumped at the document end."""

    def test_markup_ships_hidden_on_drawer_and_backdrop(self) -> None:
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert '<div class="drawer-back" id="ocDrawerBack" hidden></div>' in html
        assert 'aria-label="Strategy detail" hidden' in html

    def test_open_unhides_then_adds_open_on_the_next_task(self) -> None:
        """Un-hide first, then ONE deliberate forced reflow per drawer open
        (fix round 2, MINOR-1: an element not rendered at the previous
        style-change event has no before-change style and the next-task
        class add usually lands in the SAME frame — without the flush the
        entrance slide pops instead of animating); `.open` lands via a
        next-task one-shot (no rAF — T-N6); a close inside the window keeps
        the drawer shut (the state.rank guard)."""
        js = _js(_PORTAL_JS)
        open_fn = js[js.index("function openDrawer(rank) {") :]
        open_fn = open_fn[: open_fn.index("function closeDrawer()")]
        unhide_at = open_fn.index('drawer.removeAttribute("hidden");')
        assert 'back.removeAttribute("hidden");' in open_fn
        reflow_at = open_fn.index(
            "void drawer.offsetWidth; // the ONE sanctioned reflow per drawer open"
        )
        timer_at = open_fn.index("setTimeout(function () {")
        add_at = open_fn.index('drawer.classList.add("open");')
        assert unhide_at < reflow_at < timer_at < add_at
        assert "return; // closed again before the frame — stay shut" in open_fn

    def test_close_rehides_after_the_slide_out_with_a_reopen_guard(self) -> None:
        js = _js(_PORTAL_JS)
        close_fn = js[js.index("function closeDrawer() {") :]
        close_fn = close_fn[: close_fn.index("function surfPrev()")]
        assert 'd.setAttribute("hidden", "");' in close_fn
        assert 'b.setAttribute("hidden", "");' in close_fn
        assert "return; // reopened — stays visible" in close_fn
        assert "}, 400);" in close_fn  # bounded one-shot > slide (.32s) + delay (.34s)

    def test_rehide_timer_is_owned_and_cancelled(self) -> None:
        """Fix round 2 (NIT-2): each close used to arm an independent,
        never-cancelled re-hide — in a sub-400 ms open/close/open/close the
        FIRST close's timer cut the LAST close's slide-out short. ONE owned
        handle now: every open cancels a pending re-hide; a new close
        re-arms it (only the LATEST close owns the re-hide)."""
        js = _js(_PORTAL_JS)
        assert "var rehideTimer = null;" in js
        open_fn = js[js.index("function openDrawer(rank) {") :]
        open_fn = open_fn[: open_fn.index("function closeDrawer()")]
        assert "clearTimeout(rehideTimer);" in open_fn  # every open cancels
        close_fn = js[js.index("function closeDrawer() {") :]
        close_fn = close_fn[: close_fn.index("function surfPrev()")]
        assert "clearTimeout(rehideTimer); // only the LATEST close owns the re-hide" in close_fn
        assert "rehideTimer = setTimeout(function () {" in close_fn
        assert "rehideTimer = null;" in close_fn  # the fired timer releases the handle


class TestOpenChoreographyScopedUnderOpen:
    """P-N3/E-M2: CSS animations run even at visibility:hidden, so the
    UNscoped section-rise ladder + rank-pill glow played once invisibly at
    page load and were dead on every real open. Both are now scoped under
    `.strategy-drawer.open` — re-matching restarts them per open."""

    def test_section_rise_ladder_is_open_scoped(self) -> None:
        css = _js(_PANELS_CSS)
        assert ".strategy-drawer.open .drawer-body section { animation: bxRiseIn" in css
        assert "\n.strategy-drawer .drawer-body section { animation:" not in css  # dead form killed
        for n in range(2, 8):
            assert f".strategy-drawer.open .drawer-body section:nth-of-type({n})" in css, n

    def test_pill_glow_is_open_scoped_and_base_rule_carries_no_animation(self) -> None:
        css = _js(_PANELS_CSS)
        assert ".strategy-drawer.open .drawer-rank-pill::after { animation: bxdGlowRing" in css
        base = css[css.index(".drawer-rank-pill::after {") :]
        base = base[: base.index("}")]
        assert "animation" not in base  # the ::after base is geometry only


class TestWinnersCollapsedUntabbable:
    """C-M2 + E-M3: the collapsed winners body's 8 filter controls + rows +
    Show-all were clipped but still tabbable; and the sticky filter bar
    could never stick inside an overflow:hidden scroll container."""

    def test_collapsed_inner_is_visibility_hidden_with_delayed_flip(self) -> None:
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        # the drawer's own delayed-visibility idiom: instant on open,
        # delayed past the .38s fold on close — untabbable when shut.
        assert "visibility: hidden; transition: visibility 0s linear .4s; }" in html
        assert "#ocWinnersSection .winners-body.open .winners-body-inner" in html
        assert "visibility: visible; transition: visibility 0s; }" in html

    def test_unfold_clips_via_overflow_clip_so_sticky_engages(self) -> None:
        """overflow:clip clips exactly like hidden but creates NO scroll
        container, so .wn-filterbar's position:sticky engages against the
        PAGE scrollport; older engines fall back to the hidden form (the
        pre-fix never-sticks behavior — stated honestly in the comment)."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert "overflow: hidden; overflow: clip;" in html
        assert ".wn-filterbar { position:sticky;" in _js(_PANELS_CSS)


class TestWinnersRowKeyboardAccess:
    """C-M3/E-M5 (merged): winners rows are real tab stops activated by
    Enter/Space through the SAME onRowClick seam, and the focus-restore
    target is the activating row (previously <body> — a restore no-op).
    R6-a11y-1 (round 5): the rows must NOT carry role="button" — WAI-ARIA
    `button` is Children-Presentational (flattens the <td> cells + the
    tbody's required `row` children out of the accessibility tree) and
    `aria-selected` (markActiveRow) is not allowed on `button`."""

    def test_rows_are_focusable_without_role_button(self) -> None:
        sc = _js(_SCREENER_PANEL_JS)
        assert "tr.setAttribute('tabindex', '0');" in sc
        # Negative pin: the round-1 role="button" (invalid ARIA on a table
        # row) must never return — refuter-confirmed R6-a11y-1.
        assert "tr.setAttribute('role'" not in sc
        assert 'tr.setAttribute("role"' not in sc

    def test_enter_and_space_share_the_click_path(self) -> None:
        sc = _js(_SCREENER_PANEL_JS)
        assert "if (ev.key !== 'Enter' && ev.key !== ' ') { return; }" in sc
        keydown = sc[sc.index("addEventListener('keydown'") :]
        keydown = keydown[: keydown.index("});")]
        assert "ev.preventDefault();" in keydown  # Space must never page-scroll
        assert "deps.onRowClick(tr.getAttribute('data-rank') || '1', state.runId);" in keydown

    def test_focus_restore_reaches_the_activating_row(self) -> None:
        js = _js(_PORTAL_JS)
        assert "state.lastFocus = document.activeElement; // focus restore target" in js
        assert "state.lastFocus.focus();" in js


class TestExplainTipZLadder:
    """R6-a11y-1/2 (round 5): #bxTip on the portal page must paint ABOVE the
    strategy drawer stack — both are body-level siblings in the ROOT stacking
    context, so z-index alone decides (1200 painted the tip BEHIND the opaque
    z-1300 drawer, making every in-drawer [data-explain] hover invisible)."""

    def test_v29_tip_sits_above_the_drawer_and_backdrop(self) -> None:
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert "#bxTip { position: absolute; z-index: 1310;" in html
        css = _js(_PANELS_CSS)
        assert "z-index:1300;" in css  # the drawer layer this must beat
        assert "z-index:1290;" in css  # the backdrop layer

    def test_standalone_pages_keep_their_drawerless_1200(self) -> None:
        """screener.html / strategy.html host no drawer (no 1300 layer), so
        their 1200 stays correct — only the portal page needed the bump."""
        for name in ("screener.html", "strategy.html"):
            html = (_TEMPLATES / name).read_text(encoding="utf-8")
            assert "#bxTip { position:absolute; z-index:1200;" in html


class TestWinnersStaleResponseGuard:
    """NEW-R2-1: the winners table had ZERO stale-response guard — a rapid
    run switch could paint run A's rows under a run-B strip (and a stale
    run's row_count could set the surf bounds). Both reads now carry the
    gbPollGen-idiom load generation."""

    def test_load_rows_carries_the_load_generation_guard(self) -> None:
        sc = _js(_SCREENER_PANEL_JS)
        assert "var loadGen = 0;" in sc
        assert "loadGen += 1; // NEW-R2-1: a newer load discards every older response" in sc
        guards = sc.count("if (gen !== loadGen) { return; }")
        assert guards == 2, f"expected 2 loadGen guards (then + catch), found {guards}"

    def test_row_count_read_carries_its_own_guard(self) -> None:
        js = _js(_PORTAL_JS)
        assert "var rowCountGen = 0;" in js
        guards = js.count("if (gen !== rowCountGen) { return; }")
        assert guards == 2, f"expected 2 rowCountGen guards (then + catch), found {guards}"


class TestRunSwitchStopsTheOldRunChain:
    """E-M4 + NEW-R3-1: a run switch bumps the strategy panel's load
    generation (the old run's in-flight callback chain stops dead — no
    ~4 dead old-run fetches, no stale write into the hidden drawer), and
    update() clears the previously PAINTED body so a reopen never shows the
    old run/rank's residue during the new fetch window."""

    def test_run_change_bumps_the_strategy_panel_generation(self) -> None:
        js = _js(_PORTAL_JS)
        handler = js[js.index("function onRunChanged(runId) {") :]
        handler = handler[: handler.index("winnersCtl.loadRows(runId);") + 30]
        close_at = handler.index("closeDrawer();")
        bump_at = handler.index("window.BXStrategyPanel.update({ runId: null });")
        assert close_at < bump_at  # close first, then kill the old chain

    def test_update_clears_the_previous_targets_painted_dom(self) -> None:
        st = _js(_STRATEGY_PANEL_JS)
        assert "function resetPanelDom() {" in st
        update = st[st.index("function bxStrategyPanelUpdate(target) {") :]
        update = update[: update.index("function bxStrategyPanelMountFromLocation")]
        # AFTER the no-run early return (a null-run update never wipes the
        # standalone page's static awaiting copy) and BEFORE the fetch:
        no_run_at = update.index("if (!state.runId) { return; }")
        reset_at = update.index("resetPanelDom(); // NEW-R3-1: no previous-target residue")
        # B1 (S2 club-fold reconcile): the fetch entry now chains
        # loadBaseline() ahead of loadRow() inside the manifest callback —
        # the pin repoints to the same ONE fetch entry token; the ordering
        # law (no-run early return < reset < fetch) is unchanged.
        fetch_at = update.index("loadManifestSymbol(function () {")
        assert no_run_at < reset_at < fetch_at

    def test_reset_covers_story_trades_chart_and_equity(self) -> None:
        """Fix round 2 (edges F1 + NEW-R2R1-1/-2, refuter-EXECUTED MEDIUM):
        the reset copy is the templates' HONEST awaiting copy — never a
        'loading…' progress claim. loadRow's terminal arms (awaiting/
        corrupt · rank-miss · read failure), loadCandles' no-symbol/
        no-anchor arms and a trades-fetch failure all leave these hosts
        untouched FOREVER, so the round-1 'loading …' strings were a
        deterministic fabricated in-progress claim on exactly the honest
        states (Golden Rule #1). The three 'loading …' token pins are
        re-pointed to the honest copy — equal strength (literal-token
        presence INSIDE resetPanelDom) plus a file-wide killed-token pin.
        EXECUTED twin: TestPanelTerminalHonestCopy drives the real module.
        """
        st = _js(_STRATEGY_PANEL_JS)
        reset = st[st.index("function resetPanelDom() {") :]
        reset = reset[: reset.index("function bxStrategyPanelUpdate")]
        for token in (
            "'stName'",
            "'stWorst5'",
            "'stTrendSplit'",
            "byId('stTradesBody')",
            # the templates' honest awaiting copy (mirror of the markup):
            "trades appear once the run's results-depth sidecars land",
            "'no candles loaded yet — awaiting run + stored 1-minute data'",
            "'no trades yet — awaiting real run artifact'",
            # NEW-R2R1-2: the five previously-omitted residue fields —
            "byId('stChartCoords')",
            "or the run's last session",
            "arrow.hidden = true;",
            "setEquityLabel('awaiting real run artifact');",
            "'page 1'",
            "'source: none'",
        ):
            assert token in reset, token
        # The fake-progress class is KILLED file-wide (never re-introduced):
        for dead in ("'loading trades…'", "'loading chart…'", "'loading equity…'"):
            assert dead not in st, dead

    def test_reset_copy_mirrors_the_template_defaults(self) -> None:
        """The reset strings are the SAME literals both templates ship as
        their fresh-load defaults (reset state ≡ fresh-load state) — the
        hook-parity fence keeps the two templates aligned; this pin keeps
        the JS copy from drifting off them."""
        st = _js(_STRATEGY_PANEL_JS)
        for template in ("strategy.html", "v29.html"):
            html = (_TEMPLATES / template).read_text(encoding="utf-8")
            for copy in (
                "awaiting real run artifact — trades appear once the run's results-depth sidecars land",
                "no candles loaded yet — awaiting run + stored 1-minute data",
                "no trades yet — awaiting real run artifact",
                "chart window resolves from the trade (?trade=N centers on that trade's entry) or the run's last session",
                "source: none",
                ">page 1<",
            ):
                assert copy in html, (template, copy)
        for copy in (
            "awaiting real run artifact — trades appear once the run's results-depth sidecars land",
            "no candles loaded yet — awaiting run + stored 1-minute data",
            "no trades yet — awaiting real run artifact",
            "chart window resolves from the trade (?trade=N centers on that trade's entry) or the run's last session",
            "source: none",
            "page 1",
        ):
            assert copy in st, copy


class TestActiveRowRemark:
    """C-N1 + NEW-R1-2: the you-are-here row mark is re-applied on EVERY
    panel render (the async first mount on combos-intercept/deep-link opens
    AND filter/sort re-renders, which rebuild the tbody from scratch)."""

    def test_panel_fires_the_post_render_hook(self) -> None:
        sc = _js(_SCREENER_PANEL_JS)
        render = sc[sc.index("function render() {") : sc.index("function fillFilterOptions")]
        assert "if (deps.onRender) {" in render
        assert "deps.onRender();" in render
        assert "deps.onRender = opts.onRender || null;" in sc

    def test_portal_remarks_the_active_row_on_every_render(self) -> None:
        js = _js(_PORTAL_JS)
        mount = js[js.index("winnersCtl = window.BXScreenerPanel.mount({") :]
        mount = mount[: mount.index("markActiveRow(state.rank);") + 40]
        assert "onRender: function () {" in mount


class TestCombosInterceptRunCoherence:
    """NEW-R2-2: a not-yet-repainted combos row for ANOTHER run falls
    through to its REAL /strategy href — the mismatch check runs BEFORE the
    intercept commits (preventDefault)."""

    def test_a_foreign_run_link_falls_through_to_the_real_href(self) -> None:
        js = _js(_PORTAL_JS)
        combos = js[js.index('var combosBody = byId("combosBody");') :]
        combos = combos[: combos.index("window.BXOneclick.onRunChange")]
        mismatch_at = combos.index("String(parsed.runId) !== String(state.runId)")
        prevent_at = combos.index("ev.preventDefault();")
        assert mismatch_at < prevent_at


class TestHistoryEntryHygiene:
    """NEW-R2-3: N open/close cycles must not accumulate N identical
    rank-less history entries — a re-open from the close-created duplicate
    entry REPLACES it; popstate (a REAL prior entry) re-arms pushing.
    NEW-R2R2-2 (fix round 2): the arming is gated on the history.state
    ORIGIN stamp — a deep-link-LANDING entry is a real navigation entry
    (nothing beneath it duplicates it), so its reopen must PUSH or Back
    from the reopened drawer exits the site. Both directions are ALSO
    node-EXECUTED in TestPortalDriveExecuted."""

    def test_close_arms_the_reopen_replace_only_over_a_portal_pushed_entry(self) -> None:
        js = _js(_PORTAL_JS)
        close_fn = js[js.index("function closeDrawer() {") :]
        close_fn = close_fn[: close_fn.index("function surfPrev()")]
        assert "state.reopenReplaces = true;" in close_fn
        # a reconciler close wrote nothing AND a non-portal (deep-link
        # landing) entry never arms the replace shortcut:
        assert (
            "if (!state.reconciling && history.state && history.state.bxPortalPush === true) {"
            in close_fn
        )

    def test_open_push_stamps_the_entry_and_replace_preserves_it(self) -> None:
        """The origin stamp: the ONE pushState writes {bxPortalPush: true};
        every replaceState re-writes the CURRENT entry's state so the stamp
        survives close/re-rank/expand/collapse within the pushed slot."""
        js = _js(_PORTAL_JS)
        assert 'history.pushState({ bxPortalPush: true }, "", url);' in js
        assert 'history.replaceState(history.state, "", url);' in js

    def test_popstate_disarms_it(self) -> None:
        js = _js(_PORTAL_JS)
        pop = js[js.index('window.addEventListener("popstate"') :]
        pop = pop[: pop.index("});")]
        assert "state.reopenReplaces = false;" in pop

    def test_open_consumes_the_flag_once(self) -> None:
        js = _js(_PORTAL_JS)
        open_fn = js[js.index("function openDrawer(rank) {") :]
        open_fn = open_fn[: open_fn.index("function closeDrawer()")]
        assert "!state.reopenReplaces); // the ONLY pushState-capable arm" in open_fn
        assert "state.reopenReplaces = false;" in open_fn


class TestPanelsCssLinked:
    """COV-N1: every template that uses the Beginner/Pro block classes must
    LINK bx-oneclick-panels.css (the rules moved there — a class without its
    stylesheet is a silent no-op toggle)."""

    def test_every_jargon_block_template_links_the_panels_css(self) -> None:
        consumers = []
        for tpl in sorted(_TEMPLATES.glob("*.html")):
            html = tpl.read_text(encoding="utf-8")
            if "jargon-block" in html:
                consumers.append(tpl.name)
                assert "/static/bx-oneclick-panels.css" in html, tpl.name
        # the two known block consumers really are in the covered set:
        assert {"strategy.html", "v29.html"} <= set(consumers)

    def test_screener_links_the_panels_css_for_its_scpanel_rules(self) -> None:
        html = (_TEMPLATES / "screener.html").read_text(encoding="utf-8")
        assert "bx-scpanel" in html
        assert "/static/bx-oneclick-panels.css" in html


# ---------------------------------------------------------------------------
# T-N6 — no new pollers/timers (all four new files)
# ---------------------------------------------------------------------------


class TestNoNewPollersAllFour:
    def test_no_background_work_in_any_new_file(self) -> None:
        for path in (_PORTAL_STATE_JS, _SCREENER_PANEL_JS, _STRATEGY_PANEL_JS, _PORTAL_JS):
            js = _js(path)
            assert "createPoller(" not in js, path.name
            assert "setInterval(" not in js, path.name
            assert "requestAnimationFrame(" not in js, path.name


# ---------------------------------------------------------------------------
# T-N8 — motion-covenant pins
# ---------------------------------------------------------------------------

_PANELS_CSS = _STATIC / "bx-oneclick-panels.css"
_DESIGN_CSS = _STATIC / "bx-design.css"


class TestMotionCovenants:
    def test_no_infinite_keyframes_in_the_panels_css(self) -> None:
        assert "infinite" not in _js(_PANELS_CSS)

    def test_reveal_classes_ride_the_animate_gate_only(self) -> None:
        """Covenant #2: motion only on a REAL data render — the panel adds
        reveal classes behind deps.animate, and a node whose text stays '—'
        gets NO pop class."""
        js = _js(_STRATEGY_PANEL_JS)
        assert "if (!deps.animate) { return; }" in js
        assert "// no pop on '—' (covenant #2)" in js
        assert "deps.animate = opts.animate === true;" in js  # default FALSE

    def test_values_snap_before_motion(self) -> None:
        js = _js(_STRATEGY_PANEL_JS)
        load_row = js[js.index("function loadRow() {") :]
        fill_at = load_row.index("byId('stNet').textContent")
        pop_at = load_row.index("popRealValues([")
        assert fill_at < pop_at  # covenant #1: textContent first, pop second

    def test_reduced_motion_and_paused_blocks_present(self) -> None:
        css = _js(_PANELS_CSS)
        assert "@media (prefers-reduced-motion: reduce)" in css
        assert "html.bx-paused" in css

    def test_shared_keyframes_never_redefined(self) -> None:
        """R8: bxRowIn/bxRiseIn/bxCountPop live ONLY in bx-design.css."""
        css = _js(_PANELS_CSS)
        for name in ("bxRowIn", "bxRiseIn", "bxCountPop"):
            assert f"@keyframes {name}" not in css, name
            assert f"@keyframes {name}" in _js(_DESIGN_CSS), name

    def test_new_keyframes_exist_and_are_one_shot(self) -> None:
        css = _js(_PANELS_CSS)
        for name in (
            "bxdChartIn",
            "bxdArrowDrop",
            "bxdDraw",
            "bxdSwap",
            "bxdSwapRev",
            "bxdGlowRing",
        ):
            assert f"@keyframes {name}" in css, name

    def test_grid_rows_honesty_comment_lives_beside_the_unfold(self) -> None:
        """R9: the ONE sanctioned layout transition is stated honestly."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert "the ONE\n     sanctioned LAYOUT transition" in html.replace("\r\n", "\n")
        assert "grid-template-rows: 0fr;" in html
        assert "grid-template-rows: 1fr;" in html

    def test_equity_draw_in_is_pathlength_normalized(self) -> None:
        """Zero getTotalLength()/JS measurement — the draw-in is a CSS
        dashoffset over pathLength='1' (display-only attribute)."""
        js = _js(_STRATEGY_PANEL_JS)
        assert "setAttribute('pathLength', '1')" in js
        assert "getTotalLength" not in js
        css = _js(_PANELS_CSS)
        assert "stroke-dashoffset:1;" in css

    def test_reduced_motion_block_resets_the_equity_dashoffset(self) -> None:
        """Fix round 3 (NEW-R4C-1, MEDIUM): `.bxd-equity-draw`'s BASE rule IS
        the hidden state — pathLength=1 + dasharray:1 + dashoffset:1 maps
        every path position p ∈ [0,1) to dash-pattern position (p+1) mod 2
        ∈ [1,2), the GAP, so zero pixels paint — and visibility arrives ONLY
        via bxdDraw's `forwards` fill, which the reduce block's
        `animation: none !important` kills. The block must therefore carry
        the deterministic dashoffset reset, or reduced-motion users get a
        permanently INVISIBLE drawer equity curve (both arms, every open)."""
        css = _js(_PANELS_CSS)
        reduce_block = css[css.index("@media (prefers-reduced-motion: reduce)") :]
        reduce_block = reduce_block[: reduce_block.index("/* Covenant #6")]
        assert ".bxd-equity-draw { stroke-dashoffset: 0 !important; }" in reduce_block
        # the hazard pair the reset exists for stays true (base = hidden,
        # visibility = the forwards fill the reduce block disables):
        assert "animation: bxdDraw .8s var(--ease) .15s forwards;" in css
        assert "@keyframes bxdDraw { to { stroke-dashoffset:0; } }" in css

    def test_honest_states_render_un_animated(self) -> None:
        js = _js(_STRATEGY_PANEL_JS)
        # The dropped-arrow note + the equity label are set outside any
        # reveal gate (source-order pins: label before the draw-in class).
        render_equity = js[js.index("function renderEquity() {") :]
        label_at = render_equity.index("setEquityLabel(bxEquityLabelText(true, false));")
        draw_at = render_equity.index("mpPath.classList.add('bxd-equity-draw');")
        assert label_at < draw_at


# ---------------------------------------------------------------------------
# Fix round 2 — EXECUTED node goldens (promoted from the round-2 refuter
# harnesses that PROVED the stuck-"loading…" MEDIUM and the latch strand):
# the REAL shipped DOM layers run under a minimal DOM stub — an executed
# terminal-state golden is strictly stronger than a source pin. HONEST
# scope: a DOM STUB, not a browser (no CSS/layout/event semantics claimed —
# the #1557 envelope still applies to real-browser behavior).
# ---------------------------------------------------------------------------

#: Drives the REAL bx-strategy-panel.js DOM layer (argv: module path, case).
#: Seeds OLD-RUN residue in every host incl. the five NEW-R2R1-2 fields,
#: runs update() into the requested terminal/success arm and prints the
#: final host strings.
_PANEL_TERMINAL_DRIVE_JS = r"""
"use strict";
var MODULE = process.argv[2];
var CASE = process.argv[3];

function makeEl(tag) {
  var el = {
    tagName: String(tag || "div").toUpperCase(),
    textContent: "",
    children: [],
    className: "",
    hidden: false,
    appendChild: function (c) { this.children.push(c); return c; },
    removeChild: function (c) {
      var i = this.children.indexOf(c);
      if (i >= 0) { this.children.splice(i, 1); }
      return c;
    },
    addEventListener: function () {},
    setAttribute: function () {},
    removeAttribute: function () {},
    getAttribute: function () { return null; },
    classList: {
      add: function () {},
      remove: function () {},
      toggle: function (c, force) { return force === undefined ? false : !!force; }
    },
    style: { setProperty: function () {} }
  };
  Object.defineProperty(el, "firstChild", {
    get: function () { return this.children.length ? this.children[0] : null; }
  });
  return el;
}

var IDS = [
  "stPrev", "stNext", "stRunId", "stRank", "stName", "stInstr", "stTf",
  "stDir", "stArm", "stMask", "stFamily", "stNet", "stMaxDd", "stSharpe",
  "stDsr", "stCpcv", "stWinRate", "stPf", "stTrades", "stVixBand", "stArith",
  "stExpectancy", "stRecovery", "stLossStreak", "stMae", "stMfe",
  "stMinuteDd", "stExitMix", "stWorst5", "stTrendSplit", "stRegimeWonder",
  "stCascade", "stCascadeDeep", "stBench", "stBenchDetail",
  "stTradesBody", "stTradesNote", "stChartHost", "stEquityHost",
  "stChartCoords", "stChartError", "stTradesError", "stTradesSource",
  "stPage", "stEquityLabel", "stArrowNote"
];
var reg = {};
for (var i = 0; i < IDS.length; i++) { reg[IDS[i]] = makeEl("div"); }

/* Seed OLD-RUN residue in EVERY host the reset must clear (NEW-R2R1-2). */
reg.stName.textContent = "OLDRUN conditions";
reg.stTradesNote.textContent = "OLDRUN 37 trade(s) shown of 37 · offset 0";
var oldChart = makeEl("div");
oldChart.textContent = "OLDRUN chart";
reg.stChartHost.children.push(oldChart);
var oldEq = makeEl("div");
oldEq.textContent = "OLDRUN equity";
reg.stEquityHost.children.push(oldEq);
reg.stEquityLabel.textContent = "OLDRUN minute-path equity label";
reg.stChartCoords.textContent = "OLDRUN NSE-NIFTY · 5minute · session 2024-01-02";
reg.stArrowNote.hidden = false;
reg.stArrowNote.textContent = "OLDRUN dropped-arrow warning";
reg.stPage.textContent = "OLDRUN page 4";
reg.stTradesSource.textContent = "OLDRUN source: trade_store";

global.document = {
  readyState: "complete",
  getElementById: function (id) { return reg[id] || null; },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  createElement: function (tag) { return makeEl(tag); },
  addEventListener: function () {}
};

global.window = {
  BXPortalState: {
    strategyHref: function () { return "/strategy?x"; },
    parseStrategyParams: function () { return { runId: null, rank: 1, tradeIdx: null }; }
  },
  BXUI: {
    clear: function (host) { host.children.length = 0; host.textContent = ""; },
    el: function (tag, cls, text) {
      var e = makeEl(tag);
      e.className = cls || "";
      e.textContent = text === undefined ? "" : text;
      return e;
    }
  },
  BXChart: {
    parseTsIst: function () { return null; },
    istDateOfEpoch: function () { return "2026-01-01"; },
    candleGeom: function () { return { empty: true }; },
    candleRowsFromDbPayload: function () { return { missing: null, skipped: 0, rows: [] }; },
    skippedRowsNote: function () { return ""; },
    svgEl: function (t) { return makeEl(t); },
    candleNodes: function () { return []; },
    tradeArrow: function () { return { dropped: true }; },
    arrowNode: function () { return makeEl("g"); },
    minutePathSeries: function () { return { empty: true }; },
    equitySeries: function () { return { empty: true }; },
    equityPathNode: function () { return null; },
    ARROW_DROPPED_NOTE: "arrow dropped"
  },
  BXExplain: {
    paisaToRupees: function (p) { return String(p); },
    tradeArithmetic: function () { return { lines: ["arith"] }; }
  },
  BXPoll: {
    fetchJson: function () {
      // the equity sidecar route (success case only): an honest awaiting
      return Promise.resolve({ status: "awaiting", detail: "no sidecar" });
    }
  },
  BXOneclick: {
    oneshotFetch: function (url, errHost, cb) {
      // the trades page (success/nosymbol cases): ok with ZERO rows — the
      // single most common real state (run stored, sidecars awaiting)
      if (url.indexOf("/api/v1/oneclick/trades") === 0) {
        Promise.resolve().then(function () {
          cb({ status: "ok", source: "test", rows: [], total: 0 });
        });
      }
    },
    getManifestOnce: function () { return Promise.reject(new Error("unused")); },
    instrumentText: function (x) { return String(x || "—"); },
    vixBandText: function () { return "—"; },
    avgExtremeText: function () { return "—"; },
    minutePathDdText: function () { return "—"; },
    exitMixText: function () { return "—"; },
    worst5DaysText: function () { return "—"; },
    trendSplitText: function () { return "—"; },
    regimeWonderText: function () { return "—"; },
    // DEC-RESILIENCE-STREAKS-001 (club-O re-port): the resilience builders
    // are part of the real BXOneclick surface loadRow now consumes.
    resilienceCells: function () {
      return { worstStreak: "—", worstStreakSub: "", cost: "—", costSub: "",
               underwater: "—", underwaterBadge: "", badgeLevel: "na",
               worstMonth: "—", worstMonthSub: "", winSpread: "—", guide: "—" };
    },
    resilienceSelfLocate: function () { return { symbol: "·", text: "n/a", level: "na" }; }
  },
  location: { search: "" }
};

require(MODULE); // the DOM layer mounts against the stubs above

var getTop100;
if (CASE === "rankmiss") {
  getTop100 = function () {
    return Promise.resolve({
      status: "ok", row_count: 3,
      rows: [{ rank: 1 }, { rank: 2 }, { rank: 3 }]
    });
  };
} else if (CASE === "awaiting") {
  getTop100 = function () {
    return Promise.resolve({ status: "awaiting", detail: "awaiting real run artifact" });
  };
} else if (CASE === "reject") {
  getTop100 = function () { return Promise.reject(new Error("fetch down")); };
} else { // success | nosymbol — rank found; trades/equity fetches stubbed above
  getTop100 = function () {
    return Promise.resolve({
      status: "ok", row_count: 1,
      rows: [{ rank: 5, tf: "5m", direction: "long", arm: "a", mask_hex: "0x1",
               family_id: "f", conditions: ["c1"], metrics: {}, regime: {} }]
    });
  };
}

var manifestSymbols = CASE === "nosymbol" ? [] : ["NSE-NIFTY"];
var panel = global.window.BXStrategyPanel;
panel.mount({
  getTop100: getTop100,
  getManifest: function () {
    return { run_id: "r-1", manifest: { selection: { symbols: manifestSymbols } } };
  }
});
panel.update({ runId: "r-1", rank: 5 });

function hostText(el) {
  var bits = [];
  if (el.textContent) { bits.push(el.textContent); }
  for (var j = 0; j < el.children.length; j++) { bits.push(el.children[j].textContent); }
  return bits.join(" | ");
}

setTimeout(function () {
  console.log(JSON.stringify({
    caseName: CASE,
    stName: reg.stName.textContent,
    stTradesNote: reg.stTradesNote.textContent,
    stChartHost: hostText(reg.stChartHost),
    stEquityHost: hostText(reg.stEquityHost),
    stEquityLabel: reg.stEquityLabel.textContent,
    stChartCoords: reg.stChartCoords.textContent,
    stPage: reg.stPage.textContent,
    stTradesSource: reg.stTradesSource.textContent,
    stArrowNoteHidden: reg.stArrowNote.hidden,
    stArrowNoteText: reg.stArrowNote.textContent
  }));
}, 60);
"""

#: The templates' honest awaiting copy — the panel's reset targets.
_HONEST_TRADES_NOTE = (
    "awaiting real run artifact — trades appear once the run's results-depth sidecars land"
)
_HONEST_CHART_HOST = "no candles loaded yet — awaiting run + stored 1-minute data"
_HONEST_EQUITY_HOST = "no trades yet — awaiting real run artifact"
_HONEST_COORDS = (
    "chart window resolves from the trade (?trade=N centers on that trade's entry)"
    " or the run's last session"
)


def _run_panel_drive(tmp_path: Path, case: str) -> dict[str, Any]:
    script = tmp_path / "drive-panel.js"
    script.write_text(_PANEL_TERMINAL_DRIVE_JS, encoding="utf-8")
    proc = subprocess.run(  # nosec B603 — fixed argv, test-only
        ["node", str(script), str(_STRATEGY_PANEL_JS), case],
        capture_output=True,
        text=True,
        check=True,
    )
    out: dict[str, Any] = json.loads(proc.stdout)
    return out


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestPanelTerminalHonestCopy:
    """Fix round 2 (edges F1 + NEW-R2R1-1/-2 — the refuter-EXECUTED MEDIUM
    regression): the REAL bx-strategy-panel.js, driven for real, ends every
    loadRow terminal AND the success-path no-anchor/no-symbol chart arms
    with the templates' HONEST awaiting copy in every host — never a
    perpetual 'loading…' progress claim, and never the previous run's
    residue (the harness seeds OLD-RUN residue in ALL hosts, including the
    five previously-omitted NEW-R2R1-2 fields)."""

    def _assert_honest_common(self, out: dict[str, Any]) -> None:
        # No fabricated progress claim, no previous-run residue — anywhere:
        blob = json.dumps(out)
        assert "loading" not in blob, blob
        assert "OLDRUN" not in blob, blob
        assert out["stChartHost"] == _HONEST_CHART_HOST
        assert out["stEquityHost"] == _HONEST_EQUITY_HOST
        assert out["stArrowNoteHidden"] is True
        assert out["stArrowNoteText"] == ""

    @pytest.mark.parametrize(
        ("case", "expected_name"),
        [
            ("rankmiss", "rank 5 is not in the stored top-100 (3 rows stored)"),
            ("awaiting", "awaiting real run artifact"),
            ("reject", "top-100 read failed — retry from the winners table or reload"),
        ],
    )
    def test_every_loadrow_terminal_ends_on_the_honest_awaiting_copy(
        self, tmp_path: Path, case: str, expected_name: str
    ) -> None:
        out = _run_panel_drive(tmp_path, case)
        self._assert_honest_common(out)
        assert out["stName"] == expected_name
        assert out["stTradesNote"] == _HONEST_TRADES_NOTE
        assert out["stEquityLabel"] == "awaiting real run artifact"
        assert out["stChartCoords"] == _HONEST_COORDS
        assert out["stPage"] == "page 1"
        assert out["stTradesSource"] == "source: none"

    def test_success_with_zero_trades_ends_on_the_honest_no_anchor_arm(
        self, tmp_path: Path
    ) -> None:
        """The single most common real state (run stored, sidecars still
        awaiting): the story card loads, the trades page is ok/empty, and
        the chart host reads the honest awaiting copy beside the honest
        no-timestamps coords line — NOT 'loading chart…' forever
        (NEW-R2R2-1's executed repro, now the fixed assertion)."""
        out = _run_panel_drive(tmp_path, "success")
        blob = json.dumps(out)
        assert "loading" not in blob, blob
        assert "OLDRUN" not in blob, blob
        assert out["stTradesNote"] == "0 trade(s) shown of 0 · offset 0"
        assert out["stTradesSource"] == "source: test"
        assert out["stChartHost"] == _HONEST_CHART_HOST
        assert out["stChartCoords"] == (
            "no trade timestamps yet — chart loads once trades exist (awaiting real run artifact)"
        )
        assert out["stEquityHost"] == _HONEST_EQUITY_HOST
        assert (
            out["stEquityLabel"] == "per-trade close equity (minute-path unavailable for this run)"
        )
        assert out["stPage"] == "page 1"

    def test_no_mappable_symbol_ends_on_the_honest_unavailable_arm(self, tmp_path: Path) -> None:
        """A manifest with no mappable symbol: loadCandles' first honest arm
        names the miss and the chart host keeps the honest awaiting copy."""
        out = _run_panel_drive(tmp_path, "nosymbol")
        blob = json.dumps(out)
        assert "loading" not in blob, blob
        assert "OLDRUN" not in blob, blob
        assert out["stChartHost"] == _HONEST_CHART_HOST
        assert out["stChartCoords"] == (
            "chart unavailable — the run selection carries no mappable symbol/timeframe yet"
        )


#: Fix round 3 (R4-fresheyes-1 + R4-refuteb-1): drives the REAL
#: bx-strategy-panel.js pop path (argv: module path, mode, cycles) under a
#: LISTENER-COUNTING DOM stub. Mode "strand" NEVER dispatches animationend —
#: the prefers-reduced-motion `animation: none` semantics (and the
#: display:none'd-subtree close variant) — so only the bounded setTimeout
#: fallback can clean up; mode "fired" dispatches the event each cycle and
#: then lets the late timer run, exercising the double-removal guard.
_POP_STRAND_DRIVE_JS = r"""
"use strict";
var MODULE = process.argv[2];
var MODE = process.argv[3];
var CYCLES = parseInt(process.argv[4], 10);

function makeEl(tag) {
  var classes = {};
  var el = {
    tagName: String(tag || "div").toUpperCase(),
    textContent: "",
    children: [],
    className: "",
    hidden: false,
    listeners: {},
    appendChild: function (c) { this.children.push(c); return c; },
    removeChild: function (c) {
      var i = this.children.indexOf(c);
      if (i >= 0) { this.children.splice(i, 1); }
      return c;
    },
    addEventListener: function (type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    removeEventListener: function (type, fn) {
      var arr = this.listeners[type] || [];
      var i = arr.indexOf(fn);
      if (i >= 0) { arr.splice(i, 1); }
    },
    listenerCount: function (type) { return (this.listeners[type] || []).length; },
    fire: function (type) {
      var fns = (this.listeners[type] || []).slice();
      for (var i = 0; i < fns.length; i++) { fns[i]({}); }
    },
    setAttribute: function () {},
    removeAttribute: function () {},
    getAttribute: function () { return null; },
    classList: {
      add: function (c) { classes[c] = true; },
      remove: function (c) { delete classes[c]; },
      contains: function (c) { return classes[c] === true; },
      toggle: function (c, force) {
        var on = force === undefined ? !(classes[c] === true) : !!force;
        if (on) { classes[c] = true; } else { delete classes[c]; }
        return on;
      }
    },
    style: { setProperty: function () {} }
  };
  Object.defineProperty(el, "firstChild", {
    get: function () { return this.children.length ? this.children[0] : null; }
  });
  return el;
}

var IDS = [
  "stPrev", "stNext", "stRunId", "stRank", "stName", "stInstr", "stTf",
  "stDir", "stArm", "stMask", "stFamily", "stNet", "stMaxDd", "stSharpe",
  "stDsr", "stCpcv", "stWinRate", "stPf", "stTrades", "stVixBand", "stArith",
  "stExpectancy", "stRecovery", "stLossStreak", "stMae", "stMfe",
  "stMinuteDd", "stExitMix", "stWorst5", "stTrendSplit", "stRegimeWonder",
  "stCascade", "stCascadeDeep", "stBench", "stBenchDetail",
  "stTradesBody", "stTradesNote", "stChartHost", "stEquityHost",
  "stChartCoords", "stChartError", "stTradesError", "stTradesSource",
  "stPage", "stEquityLabel", "stArrowNote"
];
var reg = {};
for (var i = 0; i < IDS.length; i++) { reg[IDS[i]] = makeEl("div"); }

global.document = {
  readyState: "complete",
  getElementById: function (id) { return reg[id] || null; },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  createElement: function (tag) { return makeEl(tag); },
  addEventListener: function () {}
};

global.window = {
  BXPortalState: {
    strategyHref: function () { return "/strategy?x"; },
    parseStrategyParams: function () { return { runId: null, rank: 1, tradeIdx: null }; }
  },
  BXUI: {
    clear: function (host) { host.children.length = 0; host.textContent = ""; },
    el: function (tag, cls, text) {
      var e = makeEl(tag);
      e.className = cls || "";
      e.textContent = text === undefined ? "" : text;
      return e;
    }
  },
  BXChart: {
    parseTsIst: function () { return null; },
    istDateOfEpoch: function () { return "2026-01-01"; },
    candleGeom: function () { return { empty: true }; },
    candleRowsFromDbPayload: function () { return { missing: null, skipped: 0, rows: [] }; },
    skippedRowsNote: function () { return ""; },
    svgEl: function (t) { return makeEl(t); },
    candleNodes: function () { return []; },
    tradeArrow: function () { return { dropped: true }; },
    arrowNode: function () { return makeEl("g"); },
    minutePathSeries: function () { return { empty: true }; },
    equitySeries: function () { return { empty: true }; },
    equityPathNode: function () { return null; },
    ARROW_DROPPED_NOTE: "arrow dropped"
  },
  BXExplain: {
    paisaToRupees: function (p) { return String(p); },
    tradeArithmetic: function () { return { lines: ["arith"] }; }
  },
  BXPoll: {
    fetchJson: function () {
      return Promise.resolve({ status: "awaiting", detail: "no sidecar" });
    }
  },
  BXOneclick: {
    oneshotFetch: function (url, errHost, cb) {
      if (url.indexOf("/api/v1/oneclick/trades") === 0) {
        Promise.resolve().then(function () {
          cb({ status: "ok", source: "test", rows: [], total: 0 });
        });
      }
    },
    getManifestOnce: function () { return Promise.reject(new Error("unused")); },
    instrumentText: function (x) { return String(x || "—"); },
    vixBandText: function () { return "—"; },
    avgExtremeText: function () { return "—"; },
    minutePathDdText: function () { return "—"; },
    exitMixText: function () { return "—"; },
    worst5DaysText: function () { return "—"; },
    trendSplitText: function () { return "—"; },
    regimeWonderText: function () { return "—"; },
    // DEC-RESILIENCE-STREAKS-001 (club-O re-port): loadRow consumes these.
    resilienceCells: function () {
      return { worstStreak: "—", worstStreakSub: "", cost: "—", costSub: "",
               underwater: "—", underwaterBadge: "", badgeLevel: "na",
               worstMonth: "—", worstMonthSub: "", winSpread: "—", guide: "—" };
    },
    resilienceSelfLocate: function () { return { symbol: "·", text: "n/a", level: "na" }; }
  },
  location: { search: "" }
};

require(MODULE); // the DOM layer mounts against the stubs above

var panel = global.window.BXStrategyPanel;
panel.mount({
  animate: true, // the portal's own mount shape (bx-operator-portal.js)
  getTop100: function () {
    // TWO real-valued pop targets (stNet '+₹1000', stSharpe '1.5'); the
    // builder-fed cells read '—' and are covenant-#2 skipped.
    return Promise.resolve({
      status: "ok", row_count: 1,
      rows: [{ rank: 5, tf: "5m", direction: "long", arm: "a", mask_hex: "0x1",
               family_id: "f", conditions: ["c1"],
               metrics: { net_pnl_paisa: 1000, sharpe: 1.5 }, regime: {} }]
    });
  },
  getManifest: function () {
    return { run_id: "r-1", manifest: { selection: { symbols: ["NSE-NIFTY"] } } };
  }
});

var mid = [];
function cycle(k) {
  if (k >= CYCLES) {
    console.log(JSON.stringify({
      mode: MODE,
      cycles: CYCLES,
      mid: mid,
      finalNetListeners: reg.stNet.listenerCount("animationend"),
      finalSharpeListeners: reg.stSharpe.listenerCount("animationend"),
      finalHasPop: reg.stNet.classList.contains("bxd-pop"),
      stNetText: reg.stNet.textContent
    }));
    return;
  }
  panel.update({ runId: "r-1", rank: 5 });
  setTimeout(function () {
    // mid-pop probe (the fetch chain has settled; the 500 ms fallback has
    // not): the pop must be ENGAGED with exactly ONE live listener — never
    // the pre-fix k+1 accumulation.
    mid.push({
      listeners: reg.stNet.listenerCount("animationend"),
      hasPop: reg.stNet.classList.contains("bxd-pop")
    });
    if (MODE === "fired") { reg.stNet.fire("animationend"); }
    // past the 500 ms fallback window — in "strand" mode ONLY the timer can
    // have cleaned up; in "fired" mode the late timer must be a guarded
    // no-op (idempotent double removal).
    setTimeout(function () { cycle(k + 1); }, 560);
  }, 40);
}
cycle(0);
"""


def _run_pop_strand_drive(tmp_path: Path, mode: str, cycles: int) -> dict[str, Any]:
    script = tmp_path / "drive-pop.js"
    script.write_text(_POP_STRAND_DRIVE_JS, encoding="utf-8")
    proc = subprocess.run(  # nosec B603 — fixed argv, test-only
        ["node", str(script), str(_STRATEGY_PANEL_JS), mode, str(cycles)],
        capture_output=True,
        text=True,
        check=True,
    )
    out: dict[str, Any] = json.loads(proc.stdout)
    return out


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestPopStrandFallbackExecuted:
    """Fix round 3 (R4-fresheyes-1 LOW + R4-refuteb-1, refuter-EXECUTED):
    popRealValues' cleanup was the file's ONLY animationend-dependent path —
    under prefers-reduced-motion `animation: none` (and a display:none'd
    subtree after a quick close) the event NEVER fires, so listeners + the
    `.bxd-pop` class stranded unboundedly (executed pre-fix: 200 opens →
    200 listeners per popped node). The bounded 500 ms one-shot fallback
    (the module's own trades-cascade idiom) now clears both idempotently."""

    def test_strand_mode_never_accumulates_listeners_across_cycles(self, tmp_path: Path) -> None:
        out = _run_pop_strand_drive(tmp_path, "strand", 3)
        assert out["cycles"] == 3
        # every cycle ENGAGED the pop (class on, exactly ONE live listener —
        # the pre-fix blob reads k+1 here on cycle k):
        assert out["mid"] == [{"listeners": 1, "hasPop": True}] * 3
        # ...and with animationend NEVER fired, the fallback window cleared
        # the class AND the listener on every popped node:
        assert out["finalNetListeners"] == 0
        assert out["finalSharpeListeners"] == 0
        assert out["finalHasPop"] is False
        assert out["stNetText"] == "+₹1000"  # the real value stays painted

    def test_fired_mode_keeps_the_event_path_and_guards_the_late_timer(
        self, tmp_path: Path
    ) -> None:
        """Normal motion: animationend cleans up as before; the late 500 ms
        timer then runs against an already-cleared node and must be a
        guarded no-op (no crash, no double removal, nothing re-added)."""
        out = _run_pop_strand_drive(tmp_path, "fired", 2)
        assert out["mid"] == [{"listeners": 1, "hasPop": True}] * 2
        assert out["finalNetListeners"] == 0
        assert out["finalSharpeListeners"] == 0
        assert out["finalHasPop"] is False


#: Drives the REAL bx-operator-portal.js state machine (argv: portal-state
#: path, portal path, mode). Modes: latch-throw/latch-clean (the MINOR-2
#: sync-throw strand — the rank strip must run after the latch releases),
#: hist-deeplink/hist-push (the NEW-R2R2-2 origin-stamped history hygiene,
#: both directions).
_PORTAL_DRIVE_JS = r"""
"use strict";
var STATE_MODULE = process.argv[2];
var PORTAL_MODULE = process.argv[3];
var MODE = process.argv[4];

function makeEl() {
  var el = {
    textContent: "", value: "", disabled: false, hidden: false,
    children: [],
    listeners: {},
    appendChild: function (c) { this.children.push(c); return c; },
    removeChild: function (c) {
      var i = this.children.indexOf(c);
      if (i >= 0) { this.children.splice(i, 1); }
      return c;
    },
    addEventListener: function (type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    fire: function (type, ev) {
      var fns = (this.listeners[type] || []).slice();
      for (var i = 0; i < fns.length; i++) { fns[i](ev || {}); }
    },
    setAttribute: function () {},
    removeAttribute: function () {},
    getAttribute: function () { return null; },
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    classList: {
      add: function () {},
      remove: function () {},
      toggle: function (c, force) { return force === undefined ? false : !!force; }
    },
    contains: function () { return false; },
    focus: function () {},
    scrollIntoView: function () {},
    offsetWidth: 0
  };
  Object.defineProperty(el, "firstChild", {
    get: function () { return this.children.length ? this.children[0] : null; }
  });
  return el;
}

var reg = {};
function el(id) { if (!reg[id]) { reg[id] = makeEl(); } return reg[id]; }

global.document = {
  readyState: "complete",
  getElementById: function (id) {
    if (id === "ocRunSelect") { return null; } // no pre-selected run
    return el(id);
  },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  createElement: function () { return makeEl(); },
  addEventListener: function () {},
  body: makeEl(),
  activeElement: null,
  contains: function () { return false; }
};

var historyLog = [];
function recordWrite(kind, url) {
  historyLog.push([kind, url]);
  var q = url.indexOf("?");
  global.window.location.search = q === -1 ? "" : url.slice(q);
}
global.history = {
  state: null,
  pushState: function (s, t, url) { this.state = s; recordWrite("push", url); },
  replaceState: function (s, t, url) { this.state = s; recordWrite("replace", url); }
};

var capturedRunChange = null;
var screenerDeps = null;
global.window = {
  location: {
    search: MODE === "hist-push" ? "" : "?winners=1&rank=5&run_id=r-A",
    pathname: "/",
    hash: ""
  },
  addEventListener: function () {},
  matchMedia: function () { return { matches: false }; },
  BXScreenerPanel: {
    mount: function (opts) { screenerDeps = opts; return { loadRows: function () {} }; }
  },
  BXStrategyPanel: { mount: function () {}, update: function () {} },
  BXOneclick: {
    onRunChange: function (cb) { capturedRunChange = cb; },
    selectRun: function (runId) {
      if (MODE === "latch-throw") {
        // the sync-throw class: e.g. a dead bx-poll global inside
        // loadRunScoped's refreshTop100
        throw new TypeError("Cannot read properties of undefined (reading 'fetchJson')");
      }
      void runId;
    },
    latestRun: function () { return new Promise(function () {}); },
    getTop100: function () { return Promise.resolve({ status: "ok", row_count: 30 }); }
  }
};

require(STATE_MODULE); // sets window.BXPortalState (the REAL codec)

var initError = null;
try {
  require(PORTAL_MODULE);
} catch (e) {
  initError = String(e); // a browser logs the uncaught listener error and continues
}

setTimeout(function () {
  var initWrites = historyLog.slice();
  historyLog.length = 0;
  if (MODE === "latch-throw" || MODE === "latch-clean") {
    if (capturedRunChange) { capturedRunChange("r-B"); } // the poller's run switch
    setTimeout(function () {
      var stripped = historyLog.filter(function (h) {
        return h[0] === "replace" && h[1].indexOf("rank=") === -1;
      });
      console.log(JSON.stringify({
        mode: MODE,
        initError: initError,
        initWrites: initWrites,
        writesAfterRunSwitch: historyLog,
        rankStripRan: stripped.length > 0
      }));
    }, 30);
    return;
  }
  if (MODE === "hist-deeplink") {
    el("ocDrawerClose").fire("click");   // close #1 — deep-link-ORIGIN entry
    screenerDeps.onRowClick("7", "r-A"); // reopen #1 — must PUSH (NEW-R2R2-2)
    el("ocDrawerClose").fire("click");   // close #2 — now a PUSHED slot
    screenerDeps.onRowClick("8", "r-A"); // reopen #2 — must REPLACE (NEW-R2-3)
    console.log(JSON.stringify({
      mode: MODE,
      initError: initError,
      initWrites: initWrites,
      writes: historyLog,
      finalStamp: global.history.state
    }));
    return;
  }
  // hist-push: the push-origin direction (NEW-R2-3 preserved)
  if (capturedRunChange) { capturedRunChange("r-A"); }
  historyLog.length = 0;
  el("ocWinnersToggle").fire("click");   // expand — replaceState
  screenerDeps.onRowClick("3", "r-A");   // open — the ONE push (stamped)
  el("ocDrawerClose").fire("click");     // close — replace + arm
  screenerDeps.onRowClick("4", "r-A");   // reopen — must REPLACE, not push
  console.log(JSON.stringify({
    mode: MODE,
    initError: initError,
    writes: historyLog,
    finalStamp: global.history.state
  }));
}, 30);
"""


def _run_portal_drive(tmp_path: Path, mode: str) -> dict[str, Any]:
    script = tmp_path / "drive-portal.js"
    script.write_text(_PORTAL_DRIVE_JS, encoding="utf-8")
    proc = subprocess.run(  # nosec B603 — fixed argv, test-only
        ["node", str(script), str(_PORTAL_STATE_JS), str(_PORTAL_JS), mode],
        capture_output=True,
        text=True,
        check=True,
    )
    out: dict[str, Any] = json.loads(proc.stdout)
    return out


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestPortalDriveExecuted:
    """Fix round 2 — the REAL bx-operator-portal.js executed under the DOM
    stub (promoted from the round-2 refuter latch harness)."""

    def test_latch_releases_on_a_sync_selectrun_throw(self, tmp_path: Path) -> None:
        """MINOR-2/NEW-R2R3-1 killing golden: a deep link whose selectRun
        throws synchronously (a dead bx-poll global) must NOT strand the
        restorePending latch — the next run switch still strips the stale
        rank/run_id from the URL. Pre-fix (executed by the refuter): ZERO
        history writes after the run switch — the URL lied forever."""
        out = _run_portal_drive(tmp_path, "latch-throw")
        assert out["initError"] is not None and "TypeError" in out["initError"]
        assert out["initWrites"] == []  # the restore itself never writes history
        assert out["rankStripRan"] is True
        for _kind, url in out["writesAfterRunSwitch"]:
            assert "rank=" not in url, url

    def test_latch_clean_control_still_strips_after_restore(self, tmp_path: Path) -> None:
        out = _run_portal_drive(tmp_path, "latch-clean")
        assert out["initError"] is None
        assert out["initWrites"] == []
        assert out["rankStripRan"] is True

    def test_deep_link_origin_close_then_reopen_pushes_a_real_entry(self, tmp_path: Path) -> None:
        """NEW-R2R2-2 killing golden: a restore-opened drawer never pushed,
        so its close rewrote the page's ONE real navigation entry — the
        reopen must PUSH (Back then closes the drawer instead of exiting
        the site); the SECOND close/reopen cycle converges back to the
        NEW-R2-3 replace shortcut (the slot is now portal-pushed)."""
        out = _run_portal_drive(tmp_path, "hist-deeplink")
        assert out["initError"] is None
        assert out["initWrites"] == []  # the reconciling restore writes nothing
        kinds = [w[0] for w in out["writes"]]
        assert kinds == ["replace", "push", "replace", "replace"], out["writes"]
        close1, reopen1, close2, reopen2 = (w[1] for w in out["writes"])
        assert "rank=" not in close1
        assert "rank=7" in reopen1  # the PUSHED reopen carries the new rank
        assert "rank=" not in close2
        assert "rank=8" in reopen2  # converged: replace within the pushed slot
        assert out["finalStamp"] == {"bxPortalPush": True}

    def test_push_origin_cycles_never_accumulate_entries(self, tmp_path: Path) -> None:
        """The other direction (NEW-R2-3 PRESERVED): a normal row-click open
        pushes ONCE; close/reopen cycles replace within that slot — never a
        second push, never duplicate rank-less entries."""
        out = _run_portal_drive(tmp_path, "hist-push")
        assert out["initError"] is None
        kinds = [w[0] for w in out["writes"]]
        assert kinds == ["replace", "push", "replace", "replace"], out["writes"]
        assert kinds.count("push") == 1
        expand, opened, closed, reopened = (w[1] for w in out["writes"])
        assert "winners=1" in expand and "rank=" not in expand
        assert "rank=3" in opened
        assert "rank=" not in closed
        assert "rank=4" in reopened
        assert out["finalStamp"] == {"bxPortalPush": True}


# ---------------------------------------------------------------------------
# T-N5 — the honest-degrade matrix at the ROUTE level (fixture corpus +
# tmp lakes; source/route-level — no browser execution claimed)
# ---------------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")


def _degrade_client(tmp_path: Path) -> Any:
    import shutil as _shutil
    from types import SimpleNamespace

    from brutex.dashboard.app import DashboardState, build_app
    from fastapi.testclient import TestClient

    lake = tmp_path / "lake"
    (lake / "sweeps").mkdir(parents=True)
    fixture_runs = (
        Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "oneclick" / "sweep_runs"
    )
    _shutil.copytree(fixture_runs, lake / "sweep_runs")
    store = SimpleNamespace(path=lake / "instruments.duckdb")
    return TestClient(build_app(DashboardState(), instrument_store=store)), lake


class TestHonestDegradeMatrix:
    """T-N5: every degrade the portal can meet serves an honest verdict on
    the wire — never a 500, never a fabricated count. (The DOM layer's
    rendering of these verdicts is source-pinned; route truth is executed.)"""

    def test_empty_artifact_is_ok_zero_rows(self, tmp_path: Path) -> None:
        client, _ = _degrade_client(tmp_path)
        payload = client.get("/api/v1/oneclick/top100?run_id=r-empty01").json()
        assert payload["status"] == "ok"
        assert payload["row_count"] == 0
        assert payload["rows"] == []

    def test_corrupt_artifact_is_named_corrupt(self, tmp_path: Path) -> None:
        client, _ = _degrade_client(tmp_path)
        payload = client.get("/api/v1/oneclick/top100?run_id=r-corrupt01").json()
        assert payload["status"] == "corrupt"
        assert payload["row_count"] == 0

    def test_future_schema_reads_named_corrupt_never_500(self, tmp_path: Path) -> None:
        import json as _json

        client, lake = _degrade_client(tmp_path)
        path = lake / "sweep_runs" / "r-20260707-fixt01" / "top100.json"
        doc = _json.loads(path.read_text(encoding="utf-8"))
        doc["schema_version"] = int(doc["schema_version"]) + 1
        path.write_text(_json.dumps(doc), encoding="utf-8")
        resp = client.get("/api/v1/oneclick/top100?run_id=r-20260707-fixt01")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "corrupt"
        assert "schema_version" in payload["detail"]

    def test_partial_artifact_serves_partial_with_named_detail(self, tmp_path: Path) -> None:
        """COV-M1 (adversarial-review fold): the §10-promised `partial` case
        — ONE malformed entry degrades to a NAMED partial with the rows kept
        up to it (never a 500, never a silent drop); both panels branch on
        status === 'partial' client-side (source-pinned)."""
        import json as _json

        client, lake = _degrade_client(tmp_path)
        path = lake / "sweep_runs" / "r-20260707-fixt01" / "top100.json"
        doc = _json.loads(path.read_text(encoding="utf-8"))
        doc["entries"][-1] = "junk — not an object"  # the LAST entry breaks
        path.write_text(_json.dumps(doc), encoding="utf-8")
        resp = client.get("/api/v1/oneclick/top100?run_id=r-20260707-fixt01")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "partial"
        assert "is not an object — rows kept up to it" in payload["detail"]
        assert payload["row_count"] == len(payload["rows"])
        assert payload["row_count"] > 0  # the good rows survive
        # The client renders the PARTIAL arm loudly (the DOM layer is
        # source-pinned per the #1557 envelope):
        sc = _js(_SCREENER_PANEL_JS)
        assert "payload.status === 'ok' || payload.status === 'partial'" in sc
        assert "'PARTIAL artifact: ' + String(payload.detail)" in sc

    def test_one_row_artifact_serves_the_single_row(self, tmp_path: Path) -> None:
        import json as _json

        client, lake = _degrade_client(tmp_path)
        path = lake / "sweep_runs" / "r-short01" / "top100.json"
        doc = _json.loads(path.read_text(encoding="utf-8"))
        doc["entries"] = doc["entries"][:1]
        path.write_text(_json.dumps(doc), encoding="utf-8")
        payload = client.get("/api/v1/oneclick/top100?run_id=r-short01").json()
        assert payload["status"] == "ok"
        assert payload["row_count"] == 1
        assert payload["rows"][0]["rank"] == 1

    def test_absent_sidecars_read_awaiting_trades_and_equity(self, tmp_path: Path) -> None:
        """Pre-#1275 corpus: trades awaiting (source none) + equity awaiting
        → the module's fallback label arms take over client-side (pinned)."""
        client, _ = _degrade_client(tmp_path)
        trades = client.get("/api/v1/oneclick/trades?run_id=r-20260707-fixt01&rank=1").json()
        assert trades["status"] == "awaiting"
        assert trades["source"] == "none"
        equity = client.get("/api/v1/oneclick/equity?run_id=r-20260707-fixt01&rank=1").json()
        assert equity["status"] == "awaiting"

    def test_bad_rank_deep_link_is_honest_on_every_layer(self, tmp_path: Path) -> None:
        """?rank=999 over a smaller artifact: the route serves 200 with the
        REAL rows; the panel's exact-match loop misses and renders the
        pinned honest miss message (never a 500/blank/auto-redirect)."""
        client, _ = _degrade_client(tmp_path)
        page = client.get("/?winners=1&rank=999")
        assert page.status_code == 200
        strategy = client.get("/strategy?run_id=r-20260707-fixt01&rank=999")
        assert strategy.status_code == 200
        payload = client.get("/api/v1/oneclick/top100?run_id=r-20260707-fixt01").json()
        assert all(row["rank"] != 999 for row in payload["rows"])
        panel = _js(_STRATEGY_PANEL_JS)
        assert "is not in the stored top-100 (" in panel

    def test_hostile_run_serves_json_only(self, tmp_path: Path) -> None:
        client, _ = _degrade_client(tmp_path)
        resp = client.get("/api/v1/oneclick/top100?run_id=r-hostile01")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
