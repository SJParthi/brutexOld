"""Node goldens + wiring pins for the winner-detail truth pack (DEC-ONECLICK-WINNER-DETAIL-TRUTH-001).

Fails-before (the expectation-audit items 2/3/4): the /strategy story card
labeled the gross signal-only P&L "net P&L" (HIGH #12 — Track 1 never trades,
no cost is subtracted); the SHIPPED results-depth aggregates
(expectancy/recovery/streaks/MAE/MFE/minute-path DD/exit mix/worst-5-days —
all in ``entry.metrics``, ``top100_artifact._metrics_payload``) and the
regime breakdown (``entry.regime`` — one_regime_wonder/by_trend) were
NEVER rendered (HIGH #14/#13) while the card claimed they "land with the P3
data" — a stale deferral over data that ships in every artifact.

ONE pure builder set now owns the composite lines (``bx-oneclick.js`` —
exit mix · worst-5-days · trend split · regime wonder · avg/extreme pair ·
minute-path DD; money through an INJECTED house formatter so the node
goldens run formatter-explicit), consumed by the /strategy card AND the
operator card. Every builder degrades to the honest "—" — never an invented
number, never "null" prose (Golden Rule #1).

This suite executes the REAL shipped ``bx-oneclick.js`` under node (the
bx-chart.js precedent) and pins the template wiring by source string
(honest envelope — the #1557 Playwright job does not drive these pages).
Skipped (visibly) where node is absent; the CI node-presence guard lives in
``test_dashboard_oneclick_frontend.py``.
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
_CHART_JS = _STATIC / "bx-chart.js"

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not installed")

#: The node-side stand-in for the page's money formatter (formatter is
#: INJECTED, so the goldens pin builder logic, not ₹ formatting).
_FMT = "((p) => 'F' + String(p))"


def _node_call(expr: str, *argv: str) -> Any:
    """Run the REAL shipped bx-oneclick.js under node and return the JSON result."""
    script = (
        f"const O = require({json.dumps(str(_ONECLICK_JS))});"
        f"const C = require({json.dumps(str(_CHART_JS))});"
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


class TestExitMixText:
    """bxExitMixText — the writer's metrics.exits object → one honest line."""

    def test_writer_exits_object_renders_all_six_reasons(self) -> None:
        exits = {
            "stop_loss": 12,
            "take_profit": 30,
            "signal": 5,
            "forced_1520": 3,
            "end_of_data": 0,
            "session_gap": 2,
        }
        out = _node_call("O.bxExitMixText(ARGS[0])", json.dumps(exits))
        assert out == "SL 12 · TP 30 · signal 5 · 15:20 3 · EOD 0 · gap 2"

    def test_old_artifact_missing_session_gap_reads_question_mark(self) -> None:
        # Deploy-skew honesty (DEC-TRADESIM-OPEN-SESSION-GUARD-001): an OLD
        # top100.json predating the session_gap counter renders the new slot
        # as the honest "?" — counted-but-unreadable, never an invented 0.
        exits = {
            "stop_loss": 12,
            "take_profit": 30,
            "signal": 5,
            "forced_1520": 3,
            "end_of_data": 0,
        }
        out = _node_call("O.bxExitMixText(ARGS[0])", json.dumps(exits))
        assert out == "SL 12 · TP 30 · signal 5 · 15:20 3 · EOD 0 · gap ?"

    @pytest.mark.parametrize("bad", ["null", "undefined", "[]", '"x"', "7"])
    def test_absent_or_malformed_reads_the_honest_dash(self, bad: str) -> None:
        assert _node_call(f"O.bxExitMixText({bad})") == "—"

    def test_non_numeric_count_reads_question_mark_never_invented(self) -> None:
        out = _node_call("O.bxExitMixText(ARGS[0])", json.dumps({"stop_loss": "junk"}))
        assert out.startswith("SL ?")


class TestWorst5DaysText:
    """bxWorst5DaysText — metrics.worst5_days (a METRICS key — writer truth)."""

    def test_entries_render_date_and_formatted_pnl(self) -> None:
        worst5 = [["2024-01-02", -500], ["2024-03-08", -350]]
        out = _node_call("O.bxWorst5DaysText(ARGS[0], FMT)", json.dumps(worst5))
        assert out == "2024-01-02 F-500 · 2024-03-08 F-350"

    def test_valid_empty_list_reads_none_stored(self) -> None:
        assert _node_call("O.bxWorst5DaysText(ARGS[0], FMT)", json.dumps([])) == "none stored"

    @pytest.mark.parametrize("bad", ["null", "undefined", '"x"', "{}"])
    def test_absent_or_non_array_reads_the_honest_dash(self, bad: str) -> None:
        assert _node_call(f"O.bxWorst5DaysText({bad}, FMT)") == "—"

    def test_one_malformed_entry_abstains_whole_line(self) -> None:
        """Never a partial fabrication — a malformed entry abstains the line."""
        worst5 = [["2024-01-02", -500], ["2024-03-08"]]
        assert _node_call("O.bxWorst5DaysText(ARGS[0], FMT)", json.dumps(worst5)) == "—"


class TestTrendSplitText:
    """bxTrendSplitText — regime.by_trend, keys sorted deterministic."""

    def test_trend_map_renders_sorted_keys(self) -> None:
        by_trend = {"trending": 6_000, "sideways": 3_000}
        out = _node_call("O.bxTrendSplitText(ARGS[0], FMT)", json.dumps(by_trend))
        assert out == "sideways F3000 · trending F6000"

    @pytest.mark.parametrize("bad", ["null", "undefined", "{}", "[]", '"x"'])
    def test_absent_or_empty_reads_the_honest_dash(self, bad: str) -> None:
        assert _node_call(f"O.bxTrendSplitText({bad}, FMT)") == "—"

    def test_non_numeric_value_reads_question_mark(self) -> None:
        out = _node_call("O.bxTrendSplitText(ARGS[0], FMT)", json.dumps({"trending": None}))
        assert out == "trending ?"


class TestRegimeWonderText:
    """bxRegimeWonderText — the one-regime-wonder verdict sentence."""

    def test_true_with_axis_is_the_loud_warning(self) -> None:
        out = _node_call('O.bxRegimeWonderText(true, "year")')
        assert out == "ONE-REGIME WONDER — profits concentrate in a single year"

    def test_true_without_axis_falls_back_to_regime(self) -> None:
        assert (
            _node_call("O.bxRegimeWonderText(true, null)")
            == "ONE-REGIME WONDER — profits concentrate in a single regime"
        )

    def test_false_is_the_spread_verdict(self) -> None:
        assert _node_call("O.bxRegimeWonderText(false, null)") == "profits spread across regimes"

    @pytest.mark.parametrize("bad", ["null", "undefined", '"yes"', "1"])
    def test_unknown_flag_reads_the_honest_dash(self, bad: str) -> None:
        assert _node_call(f"O.bxRegimeWonderText({bad}, null)") == "—"


class TestAvgExtremeAndMinuteDd:
    """bxAvgExtremeText (MAE/MFE pair cells) + bxMinutePathDdText."""

    def test_both_legs_render(self) -> None:
        out = _node_call('O.bxAvgExtremeText(120, 400, "worst", FMT)')
        assert out == "avg F120 · worst F400"

    def test_single_leg_renders_alone(self) -> None:
        assert _node_call('O.bxAvgExtremeText(120, null, "worst", FMT)') == "avg F120"
        assert _node_call('O.bxAvgExtremeText(null, 900, "best", FMT)') == "best F900"

    def test_both_absent_reads_the_honest_dash(self) -> None:
        assert _node_call('O.bxAvgExtremeText(null, undefined, "worst", FMT)') == "—"

    def test_minute_path_dd_with_pct(self) -> None:
        """The wire pct is a 0-1 RATIO (minute_path.py ``drop / peak`` →
        top100_artifact.py verbatim) — the builder scales ×100. The prior
        fixture literal ``1.5`` was PERCENT-scale, exactly the
        value-plausible masking blindspot the hostile round named."""
        assert _node_call("O.bxMinutePathDdText(1500, 0.015, FMT)") == "F1500 (1.5%)"

    def test_minute_path_dd_ratio_scale_teeth(self) -> None:
        """Hostile-round MAJOR-1 teeth: a 30% DD renders "30%" (the sibling
        max_drawdown_pct scale on the same card) — never "0.3%"."""
        assert _node_call("O.bxMinutePathDdText(1500, 0.3, FMT)") == "F1500 (30%)"

    def test_minute_path_dd_precision_bounded_2dp(self) -> None:
        assert _node_call("O.bxMinutePathDdText(1500, 0.123456, FMT)") == "F1500 (12.35%)"

    def test_minute_path_dd_without_pct(self) -> None:
        assert _node_call("O.bxMinutePathDdText(1500, null, FMT)") == "F1500"

    @pytest.mark.parametrize("bad", ["null", "undefined", '"x"', "NaN"])
    def test_minute_path_dd_absent_reads_the_honest_dash(self, bad: str) -> None:
        assert _node_call(f"O.bxMinutePathDdText({bad}, 1.5, FMT)") == "—"


class TestMinutePathSeries:
    """bx-chart.js bxMinutePathSeries — the stored-series → chart bridge (4C)."""

    def test_points_map_and_final_is_last_close(self) -> None:
        rows = [
            {"session_date": "2026-07-01", "equity_close_paisa": 1_000},
            {"session_date": "2026-07-02", "equity_close_paisa": 1_500},
        ]
        out = _node_call("C.minutePathSeries(ARGS[0])", json.dumps(rows))
        assert out["empty"] is False
        assert [p["cum_paisa"] for p in out["points"]] == [1_000, 1_500]
        assert out["final_paisa"] == 1_500
        assert out["invalid_rows"] == 0

    def test_invalid_points_are_skipped_and_counted(self) -> None:
        rows = [
            {"equity_close_paisa": 1_000},
            {"equity_close_paisa": None},  # skipped + counted
            {"equity_close_paisa": 2.5},  # non-integer paisa — skipped
            {},
        ]
        out = _node_call("C.minutePathSeries(ARGS[0])", json.dumps(rows))
        assert [p["cum_paisa"] for p in out["points"]] == [1_000]
        assert out["invalid_rows"] == 3

    def test_empty_or_non_array_is_the_empty_flag(self) -> None:
        assert _node_call("C.minutePathSeries([])")["empty"] is True
        assert _node_call("C.minutePathSeries(null)")["empty"] is True


class TestWinnerDetailWiring:
    """Source-string pins: relabels + the new sections read the ONE builders."""

    def test_strategy_card_gross_signal_label_and_chip(self) -> None:
        """HIGH #12: the story card's P&L is 'gross signal P&L' + the
        persistent signal-only chip — never the bare 'net P&L' claim."""
        html = (_TEMPLATES / "strategy.html").read_text(encoding="utf-8")
        assert "Edge — gross signal P&amp;L" in html
        assert "signal-only — costs modeled in the options-translation track" in html
        assert 'id="stSignalOnly"' in html
        assert "Edge — net P&amp;L" not in html  # killed label

    def test_operator_hero_gross_signal_label(self) -> None:
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert (
            "gross signal P&amp;L (signal-only — costs modeled in the options-translation track)"
            in html
        )
        assert "Gross signal P&amp;L" in html
        assert 'data-explain="gross_signal_pnl"' in html
        assert "net P&amp;L · buy-&amp;-hold baseline lands with P3" not in html  # killed
        # Hostile-round fold: the killed-token pin EXTENDS to data-tooltip
        # attributes — the EDGE hover claimed "net trade P&L … − full costs"
        # while the card's own sub-label read gross/signal-only.
        assert "net trade P&L" not in html  # killed tooltip claim
        assert "full costs" not in html  # killed tooltip claim
        assert "gross signal P&L from the stored run artifact" in html  # tooltip truth
        # The baseline DATA ships on the #1617 artifact — only the RENDER is
        # the named follow-up; the stale P3 phase-naming is dead on v29 too.
        assert "buy-&amp;-hold baseline lands with P3" not in html
        assert "(lands with P3 — O-n)" not in html

    def test_strategy_deep_stats_section_reads_the_builders(self) -> None:
        html = (_TEMPLATES / "strategy.html").read_text(encoding="utf-8")
        for anchor in (
            'id="stExpectancy"',
            'id="stRecovery"',
            'id="stLossStreak"',
            'id="stMae"',
            'id="stMfe"',
            'id="stMinuteDd"',
            'id="stExitMix"',
            'id="stWorst5"',
        ):
            assert anchor in html, anchor
        assert "window.BXOneclick.exitMixText(m.exits)" in html
        assert "window.BXOneclick.worst5DaysText(m.worst5_days, money)" in html
        assert (
            "window.BXOneclick.avgExtremeText(m.mae_avg_paisa, m.mae_worst_paisa, 'worst', money)"
            in html
        )
        assert (
            "window.BXOneclick.minutePathDdText(m.max_dd_minute_path_paisa, m.max_dd_minute_path_pct, money)"
            in html
        )

    def test_deep_stats_beginner_summary_rides_the_block_toggle(self) -> None:
        html = (_TEMPLATES / "strategy.html").read_text(encoding="utf-8")
        assert 'class="plain-eng-block"' in html
        assert 'class="jargon-block"' in html
        assert "body.share-mode .jargon-block { display:none; }" in html

    def test_operator_card_regime_hooks(self) -> None:
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        for hook in ("r1_trend_split", "r1_regime_wonder", "r1_worst5"):
            assert f'data-oc="{hook}"' in html, hook
        js = _ONECLICK_JS.read_text(encoding="utf-8")
        assert 'setOc("r1_trend_split", bxTrendSplitText(reg.by_trend, paisaText));' in js
        assert 'setOc("r1_worst5", bxWorst5DaysText(m.worst5_days, paisaText));' in js

    def test_explain_table_carries_every_new_key(self) -> None:
        js = (_STATIC / "bx-explain.js").read_text(encoding="utf-8")
        for key in (
            "gross_signal_pnl:",
            "expectancy_paisa:",
            "recovery_factor:",
            "max_consecutive_losses:",
            "mae_avg_paisa:",
            "mfe_avg_paisa:",
            "max_dd_minute_path_paisa:",
            "exit_mix:",
            "worst5_days:",
            "by_trend:",
            "one_regime_wonder:",
        ):
            assert key in js, key


class TestRingSeenBoundedToWindow:
    """bxRingAdd — the timeline dedup set is BOUNDED by the ring window.

    Breaker B #5 fold (DEC-ONECLICK-WINNER-DETAIL-TRUTH-001): pre-fix,
    ``ringAdd`` inserted into ``ringSeen`` unconditionally BEFORE the
    5,000-cap trim and a trimmed-out event's key was NEVER evicted — a
    real-Chromium-measured 153.6 B/event GC-immune growth (+146.9 MB @ 1M
    events) on a long-lived operator tab during a multi-hour sweep (the
    finder's harness baselines; control run +0.06 MB class). Post-fix the
    ONE pure core ``bxRingAdd`` owns the dedup gate + the trimmed-key
    eviction, so ``ringSeen`` holds exactly the in-ring keys — WINDOW-scoped
    dedup: exact inside the window, and a key that LEFT the window may
    legitimately be re-admitted if re-sent.
    """

    def test_unique_id_flood_growth_flattens_to_the_ring_cap(self) -> None:
        # 5,025 unique-id events in 201 pages of 25 — the pre-fix seen set
        # read 5,025 keys and kept growing; post-fix it flattens AT the cap.
        out = _node_call(
            """(() => {
              const cap = O.BX_TIMELINE_RING_CAP;
              const ring = []; const seen = {};
              for (let p = 0; p < 201; p++) {
                const batch = [];
                for (let i = 0; i < 25; i++) {
                  const n = p * 25 + i;
                  batch.push({ ts_utc: 'T' + n, kind: 'k', payload: { n: n } });
                }
                O.bxRingAdd(ring, seen, batch, cap);
              }
              return { cap: cap, ring: ring.length, seen: Object.keys(seen).length };
            })()"""
        )
        assert out["cap"] == 5000
        assert out["ring"] == 5000
        assert out["seen"] <= out["cap"]  # the leak law: bounded by the window
        assert out["seen"] == 5000  # exactly the in-ring keys, nothing more

    def test_repeating_id_control_run_stays_at_the_distinct_count(self) -> None:
        # The control shape the growth run must flatten LIKE: 5,025 events
        # over 100 distinct ids — seen never exceeds the distinct count.
        out = _node_call(
            """(() => {
              const cap = O.BX_TIMELINE_RING_CAP;
              const ring = []; const seen = {};
              for (let p = 0; p < 201; p++) {
                const batch = [];
                for (let i = 0; i < 25; i++) {
                  const n = (p * 25 + i) % 100;
                  batch.push({ ts_utc: 'T' + n, kind: 'k', payload: { n: n } });
                }
                O.bxRingAdd(ring, seen, batch, cap);
              }
              return { ring: ring.length, seen: Object.keys(seen).length };
            })()"""
        )
        assert out == {"ring": 100, "seen": 100}

    def test_dedup_stays_exact_inside_the_window(self) -> None:
        out = _node_call(
            """(() => {
              const cap = O.BX_TIMELINE_RING_CAP;
              const ring = []; const seen = {};
              const a = { ts_utc: 'A', kind: 'k', payload: {} };
              const first = O.bxRingAdd(ring, seen, [a], cap);
              const second = O.bxRingAdd(ring, seen, [a, a], cap);
              return { first: first, second: second, ring: ring.length };
            })()"""
        )
        assert out == {"first": 1, "second": 0, "ring": 1}

    def test_key_that_left_the_window_may_be_readmitted(self) -> None:
        # The documented WINDOW-scoped semantic: after a cap-sized flood
        # pushes A out of the ring, a re-sent A is legitimately re-admitted
        # (never a whole-history dedup), and seen == the in-ring key set.
        out = _node_call(
            """(() => {
              const cap = O.BX_TIMELINE_RING_CAP;
              const ring = []; const seen = {};
              const a = { ts_utc: 'A', kind: 'k', payload: {} };
              O.bxRingAdd(ring, seen, [a], cap);
              const flood = [];
              for (let i = 0; i < cap; i++) {
                flood.push({ ts_utc: 'F' + i, kind: 'k', payload: {} });
              }
              O.bxRingAdd(ring, seen, flood, cap);
              const aEvicted = !seen[O.bxEventLineText(a)];
              const readmitted = O.bxRingAdd(ring, seen, [a], cap);
              const ringKeys = ring.map(O.bxEventLineText);
              const invariant =
                Object.keys(seen).length === new Set(ringKeys).size &&
                ringKeys.every((k) => seen[k] === true);
              return {
                aEvicted: aEvicted,
                readmitted: readmitted,
                seen: Object.keys(seen).length,
                invariant: invariant
              };
            })()"""
        )
        assert out == {"aEvicted": True, "readmitted": 1, "seen": 5000, "invariant": True}

    def test_ring_add_is_the_wired_core_and_documents_the_semantic(self) -> None:
        js = _ONECLICK_JS.read_text(encoding="utf-8")
        assert "function bxRingAdd(ring, seen, records, cap)" in js
        assert "WINDOW-SCOPED DEDUP SEMANTIC" in js
        # The DOM-layer ringAdd delegates to the ONE pure core.
        assert "bxRingAdd(state.ring, state.ringSeen, records, BX_TIMELINE_RING_CAP);" in js
        # The pre-fix leak shape (insert-before-trim into state.ringSeen at
        # the poll path) is DEAD — the poll path holds no direct insert.
        assert (
            js.count("state.ringSeen[key] = true;") == 1
        )  # load-earlier only (room-bounded, never trims)
