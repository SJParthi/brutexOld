"""Source-string pins for the five one-click frontend JS files (DEC-OPERATOR-PAGE-REAL-001).

HONEST envelope (Golden Rule #10): every test below is a SUBSTRING assert
pinning the PRESENCE of the JS (constants / branches / discipline order /
export guards) in the shipped source — NOT its execution; this repo has no
headless JS runner. The pure cores ARE executed for real under node by the
sibling suites (test_dashboard_chart_math.py / test_dashboard_screener_math.py
/ test_dashboard_explain_lines.py); the DOM/poller layers are pinned here
only.

Fails-before: bx-poll.js / bx-chart.js / bx-screener.js / bx-explain.js /
bx-oneclick.js did not exist; without these pins a later edit could silently
(a) reintroduce a string-to-DOM sink (innerHTML et al. — the F-M2 XSS law),
(b) drop the document.hidden guard or the capped backoff (the 38-minute
frozen-tab incident class), (c) unbound the timeline ring (F-B2), or (d)
key-spread an event payload into markup instead of ONE JSON.stringify'd text
node.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"

_POLL = _STATIC / "bx-poll.js"
_CHART = _STATIC / "bx-chart.js"
_SCREENER = _STATIC / "bx-screener.js"
_EXPLAIN = _STATIC / "bx-explain.js"
_COPY = _STATIC / "bx-copy.js"
_ONECLICK = _STATIC / "bx-oneclick.js"

#: The file-conventions suite covers all six one-click JS files (bx-copy.js
#: joined with DEC-OPERATOR-PAGE-CLEANVIEW-001).
_ALL_FIVE = (_POLL, _CHART, _SCREENER, _EXPLAIN, _COPY, _ONECLICK)
_IDS = ("bx-poll", "bx-chart", "bx-screener", "bx-explain", "bx-copy", "bx-oneclick")

#: DEC-OPERATOR-PAGE-CONSOLIDATE-001 modules — the screener/strategy page
#: wiring extracted into shared mountable files + the pure portal URL codec.
#: Same file discipline as the five originals, their OWN decision id in the
#: header; createElement presence is ROLE-AWARE (bx-portal-state builds no
#: DOM and must NOT be forced to carry dead code).
_PORTAL_STATE = _STATIC / "bx-portal-state.js"
_SCREENER_PANEL = _STATIC / "bx-screener-panel.js"
_STRATEGY_PANEL = _STATIC / "bx-strategy-panel.js"
_OPERATOR_PORTAL = _STATIC / "bx-operator-portal.js"
_CONSOLIDATE_FILES = (_PORTAL_STATE, _SCREENER_PANEL, _STRATEGY_PANEL, _OPERATOR_PORTAL)
_CONSOLIDATE_IDS = (
    "bx-portal-state",
    "bx-screener-panel",
    "bx-strategy-panel",
    "bx-operator-portal",
)

#: Per-file DOM-builder expectation (the role-aware createElement law):
#: the panels BUILD nodes; the pure codec and the class-flipping portal
#: glue must never grow node-building code.
_CONSOLIDATE_BUILDS_DOM = {
    _PORTAL_STATE: False,
    _SCREENER_PANEL: True,
    _STRATEGY_PANEL: True,
    _OPERATOR_PORTAL: False,
}


def _js(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestFileDiscipline:
    """Every new JS file: header, strict mode, export guard, honest envelope."""

    @pytest.mark.parametrize("path", _ALL_FIVE, ids=_IDS)
    def test_file_exists_and_is_not_suspiciously_small(self, path: Path) -> None:
        assert path.is_file()
        assert len(path.read_bytes()) > 500  # the static-assets suspiciously-small guard

    @pytest.mark.parametrize("path", _ALL_FIVE, ids=_IDS)
    def test_use_strict_and_decision_id_header(self, path: Path) -> None:
        js = _js(path)
        assert '"use strict";' in js
        assert "DEC-OPERATOR-PAGE-REAL-001" in js  # the decision ID in the header

    @pytest.mark.parametrize("path", _ALL_FIVE, ids=_IDS)
    def test_honest_envelope_comment_present(self, path: Path) -> None:
        assert "Honest envelope (Golden Rule #10)" in _js(path)

    @pytest.mark.parametrize("path", _ALL_FIVE, ids=_IDS)
    def test_commonjs_export_guard_for_the_node_golden_harness(self, path: Path) -> None:
        js = _js(path)
        assert 'typeof module !== "undefined" && module.exports' in js

    @pytest.mark.parametrize("path", _ALL_FIVE, ids=_IDS)
    def test_zero_string_to_dom_sinks(self, path: Path) -> None:
        """The F-M2 XSS law: createElement(NS)/textContent ONLY — no sink exists."""
        js = _js(path)
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert sink not in js, f"{path.name} must never contain {sink}"

    def test_dom_builders_are_createelement_only(self) -> None:
        """The two DOM-writing files build nodes via createElement(NS) + textContent."""
        assert "createElementNS" in _js(_CHART)
        oneclick = _js(_ONECLICK)
        assert "textContent" in oneclick
        # bx-oneclick builds its own nodes through BXUI.el (createElement+textContent).
        assert "BXUI.el(" in oneclick


class TestConsolidateFileDiscipline:
    """The DEC-OPERATOR-PAGE-CONSOLIDATE-001 modules ride the SAME file
    discipline as the five originals (strict mode, decision id, honest
    envelope, CommonJS guard, zero raw string-to-DOM sink tokens), with the
    createElement expectation asserted PER FILE (role-aware — the pure URL
    codec builds no DOM)."""

    @pytest.mark.parametrize("path", _CONSOLIDATE_FILES, ids=_CONSOLIDATE_IDS)
    def test_file_exists_and_is_not_suspiciously_small(self, path: Path) -> None:
        assert path.is_file()
        assert len(path.read_bytes()) > 500

    @pytest.mark.parametrize("path", _CONSOLIDATE_FILES, ids=_CONSOLIDATE_IDS)
    def test_use_strict_and_decision_id_header(self, path: Path) -> None:
        js = _js(path)
        assert '"use strict";' in js
        assert "DEC-OPERATOR-PAGE-CONSOLIDATE-001" in js

    @pytest.mark.parametrize("path", _CONSOLIDATE_FILES, ids=_CONSOLIDATE_IDS)
    def test_honest_envelope_comment_present(self, path: Path) -> None:
        assert "Honest envelope (Golden Rule #10)" in _js(path)

    @pytest.mark.parametrize("path", _CONSOLIDATE_FILES, ids=_CONSOLIDATE_IDS)
    def test_commonjs_export_guard_for_the_node_golden_harness(self, path: Path) -> None:
        assert 'typeof module !== "undefined" && module.exports' in _js(path)

    @pytest.mark.parametrize("path", _CONSOLIDATE_FILES, ids=_CONSOLIDATE_IDS)
    def test_zero_string_to_dom_sinks(self, path: Path) -> None:
        """The F-M2 XSS law: createElement(NS)/textContent ONLY — a raw
        SUBSTRING ban, so even a comment cannot carry a sink token."""
        js = _js(path)
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert sink not in js, f"{path.name} must never contain {sink}"

    @pytest.mark.parametrize("path", _CONSOLIDATE_FILES, ids=_CONSOLIDATE_IDS)
    def test_role_aware_dom_builder_expectation(self, path: Path) -> None:
        js = _js(path)
        if _CONSOLIDATE_BUILDS_DOM[path]:
            assert "document.createElement(" in js
            assert "textContent" in js
        else:
            assert "createElement" not in js, (
                f"{path.name} builds no DOM nodes by design (pure codec / "
                "class-flipping glue) — it must never grow node-building code"
            )


class TestPollDiscipline:
    """bx-poll.js — the ONE shared poll core (DEC-PULL-UI-STALENESS-001 laws)."""

    def test_backoff_constants_are_the_house_values(self) -> None:
        js = _js(_POLL)
        assert "BACKOFF_BASE_MS: 3000" in js
        assert "BACKOFF_MAX_MS: 30000" in js
        assert "QUICK_RETRY_MS: 600" in js  # the /universe quick-retry envelope
        assert "STALE_WARN_MS: 10000" in js
        assert "STALE_RED_MS: 30000" in js

    def test_hidden_guard_is_the_first_statement_of_poll_before_any_fetch(self) -> None:
        """A background tab fires ZERO fetches — the guard precedes the fetch
        AND the pollSeq bump inside the poll body."""
        js = _js(_POLL)
        body = js[js.index("function poll() {") :]
        guard = body.index("if (document.hidden) {")
        seq_bump = body.index("++pollSeq")
        fetch_call = body.index("fetchJson(cfg.url")
        assert guard < seq_bump < fetch_call

    def test_pollseq_reentrancy_guard_present_on_both_settle_paths(self) -> None:
        js = _js(_POLL)
        assert js.count("if (seq !== pollSeq || stopped) {") == 2  # success AND failure

    def test_every_failure_mode_is_counted(self) -> None:
        js = _js(_POLL)
        # non-ok THROWS into the counted-failure path…
        assert 'throw new Error("poll failed: HTTP " + r.status);' in js
        # …and a malformed body does too.
        assert 'throw new Error("malformed payload");' in js
        assert "pollFailures += 1;" in js

    def test_visibilitychange_catchup_calls_poll_directly_never_a_timer(self) -> None:
        js = _js(_POLL)
        assert 'document.addEventListener("visibilitychange"' in js
        assert "poll() DIRECTLY, never a 0 ms timer" in js  # the seq-race law, named

    def test_staleness_ticker_short_circuits_while_hidden(self) -> None:
        """F-m5: the 1 s ticker itself runs zero work in a hidden tab."""
        js = _js(_POLL)
        ticker = js[js.index("function bxStalenessTicker(") :]
        tick_body = ticker[ticker.index("function tick() {") :]
        assert tick_body.index("if (document.hidden) {") < tick_body.index("render(")

    def test_loud_stale_wording_and_failure_gated_tail(self) -> None:
        js = _js(_POLL)
        assert '"STALE — "' in js  # LOUD RED explicit wording
        assert '" — retrying"' in js
        assert '" — awaiting response"' in js  # the failure-gated action tail

    def test_abort_timer_armed_through_the_body_read(self) -> None:
        js = _js(_POLL)
        assert "return r.json(); // abort timer still armed through the body read" in js

    def test_counted_failure_schedules_through_the_backoff_law(self) -> None:
        """The createPoller catch arm reschedules via ``bxBackoffDelayMs`` —
        the CORE pin the retired v29 in-place-poller pins map onto
        (DEC-DASHBOARD-POLL-HYGIENE-002 → DEC-FRONTEND-LATENCY-HARDENING-001
        supersession): every counted failure lands on the capped-backoff
        schedule, never a bare fixed retry and never a give-up."""
        js = _js(_POLL)
        assert "schedule(bxBackoffDelayMs(pollFailures, o));" in js
        # …and the success arm restores the normal cadence.
        assert "schedule(pollMs);" in js

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_backoff_ladder_node_golden(self) -> None:
        """Node-executed golden for the PURE backoff law (the shipped file).

        The capped-failure-backoff defect class (DEC-DASHBOARD-POLL-HYGIENE-002's
        v29 pins, retired at the #1720 supersession) stays covered HERE at the
        shared core: 0 failures → the normal cadence, the 600 ms quick-retry
        envelope (QUICK_RETRIES=2), then 3 s→30 s capped doubling, and a FINITE
        delay far past the cap — NEVER a give-up.
        """
        script = (
            f"const P = require({json.dumps(str(_POLL))});"
            "const ladder = [0,1,2,3,4,5,6,7,8,20,1000].map("
            "  (f) => P.backoffDelayMs(f));"
            "console.log(JSON.stringify(ladder));"
        )
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        ladder = json.loads(proc.stdout)
        assert ladder == [
            3000,  # 0 failures → the normal POLL_MS cadence
            600,  # quick retry 1
            600,  # quick retry 2 (QUICK_RETRIES = 2)
            3000,  # BACKOFF_BASE_MS · 2^0
            6000,
            12000,
            24000,
            30000,  # capped at BACKOFF_MAX_MS…
            30000,
            30000,  # …and stays capped
            30000,  # finite far past the cap — never a give-up
        ]


class TestOneclickWidgetLayer:
    """bx-oneclick.js — ring cap, payload rendering law, precedence, errorCard."""

    def test_timeline_ring_cap_constant_is_5000(self) -> None:
        """F-B2: the client event accumulation is BOUNDED."""
        js = _js(_ONECLICK)
        assert "var BX_TIMELINE_RING_CAP = 5000;" in js

    def test_honest_ring_label_wording(self) -> None:
        js = _js(_ONECLICK)
        assert "showing last 5,000 events — older events via" in js

    def test_event_payload_is_json_stringified_into_one_text_node(self) -> None:
        """F-M2: payload objects are NEVER key-spread into markup."""
        js = _js(_ONECLICK)
        assert "JSON.stringify(rec.payload)" in js
        # The line lands in the DOM via BXUI.el(..., text) => textContent.
        # (`visible[i]` since DEC-O1-GUARANTEE-HARDEN-001 — the render walks
        # the newest-500 capped window; the ONE-text-node law is unchanged.)
        assert 'window.BXUI.el("div", "oc-event-line", bxEventLineText(visible[i]))' in js

    def test_funnel_precedence_law_manifest_only_when_idle(self) -> None:
        """C-M2: the live poller wins while a sweep runs."""
        js = _js(_ONECLICK)
        assert "function bxFunnelWriteAllowed(source, sweepRunning)" in js
        assert "return !sweepRunning; // manifest fills only when idle (C-M2)" in js
        assert 'bxFunnelWriteAllowed("manifest", state.sweepRunning)' in js

    def test_funnel_provenance_labels(self) -> None:
        js = _js(_ONECLICK)
        assert '"live sweep " + String(runId)' in js
        assert '"completed run " + String(runId)' in js

    def test_oneshot_fetch_failures_route_through_errorcard_with_retry(self) -> None:
        """F-M4: rejection AND non-ok AND malformed body -> errorCard, never blank."""
        js = _js(_ONECLICK)
        assert "window.BXUI.errorCard(panel," in js
        assert "retry: function () {" in js
        assert 'throw new Error("malformed payload");' in js
        assert 'new Error("HTTP " + r.status);' in js

    def test_search_filters_the_ring_only(self) -> None:
        js = _js(_ONECLICK)
        assert "function bxRingFilter(ring, q, kind)" in js
        assert "// ring-scoped filter only" in js

    def test_mode_toggle_rides_the_one_shared_bx_explain_mechanism(self) -> None:
        js = _js(_ONECLICK)
        assert "window.BXExplain.applyMode(document.body, mode);" in js

    def test_ticker_strip_renders_via_textcontent_only(self) -> None:
        js = _js(_ONECLICK)
        assert 'strip.textContent = bits.join(" · ");' in js

    def test_idempotent_init_guard(self) -> None:
        js = _js(_ONECLICK)
        assert "if (window.__bxOneclickInit) {" in js

    def test_uses_the_shared_bx_poll_core_never_a_third_copy(self) -> None:
        js = _js(_ONECLICK)
        assert "window.BXPoll.createPoller(" in js
        # No local reimplementation of the backoff discipline:
        assert "BACKOFF_BASE_MS" not in js
        assert "Math.pow(2" not in js

    def test_awaiting_banner_uses_the_one_shared_constant(self) -> None:
        """C-NIT-3: the wording lives in bx-explain.js BX_AWAITING_TEXT only."""
        js = _js(_ONECLICK)
        assert "window.BXExplain.AWAITING_TEXT" in js

    def test_awaiting_banner_swaps_the_cta_while_a_sweep_runs(self) -> None:
        """Review round 2: the no-selected-run arm consults the module's own
        state.sweepRunning and renders the RUNNING variant — never a "press
        Run Sweep" call-to-action the running sweep itself refuses."""
        js = _js(_ONECLICK)
        assert "} else if (state.sweepRunning) {" in js
        assert "window.BXExplain.AWAITING_RUNNING_TEXT" in js

    def test_hero_fanout_names_the_selected_run_not_the_latest(self) -> None:
        """Review round 2: the manifest is run-scoped to the SELECTED run —
        its fan-out key says so (`suspectRunId`); the TRUE latest_run_id
        rides the ticker's runs-poller fan-out only."""
        js = _js(_ONECLICK)
        assert "suspectRunId: payload.run_id" in js
        assert "latestRunId: payload.run_id" not in js
        assert "latestRunId: runsPayload ? runsPayload.latest_run_id : null" in js

    def test_ring_label_and_poll_constants_unchanged_by_cleanview(self) -> None:
        """DEC-OPERATOR-PAGE-CLEANVIEW-001 byte-preservation: the honest ring
        cap label + the shared poll constants are display contracts the
        clean-view pass must NOT touch."""
        js = _js(_ONECLICK)
        assert (
            "var BX_TIMELINE_RING_LABEL =\n"
            '  "showing last 5,000 events — older events via “load earlier”";'
        ) in js
        assert "var BX_TIMELINE_RING_CAP = 5000;" in js
        poll = _js(_POLL)
        assert "BACKOFF_BASE_MS: 3000" in poll
        assert "BACKOFF_MAX_MS: 30000" in poll


class TestCopyCatalogWiring:
    """bx-copy.js presence + the sinks that consume it (source pins; the
    catalog's BEHAVIOR is node-golden-tested in
    test_dashboard_copy_catalog.py — DEC-OPERATOR-PAGE-CLEANVIEW-001)."""

    def test_copy_catalog_exports_the_api(self) -> None:
        js = _js(_COPY)
        assert "function bxCopyLookup(raw)" in js
        assert "function bxCopyApply(el, raw, opts)" in js
        assert "function bxCopyToken(kind, raw)" in js
        assert "window.BXCopy = BXCopy;" in js

    def test_oneclick_sinks_consume_the_catalog(self) -> None:
        """Pill/verify/source tokens + detail sinks route through BXCopy; the
        CSS class stays keyed on the RAW payload token (Law 2 + caller rule 1)."""
        js = _js(_ONECLICK)
        assert 'window.BXCopy.token("pill", rawState)' in js
        assert 'window.BXCopy.token("verify", rawVerify)' in js
        assert 'window.BXCopy.token("source", rawSource)' in js
        assert 'window.BXCopy.token("granularity", r.mae_granularity)' in js
        assert js.count('{ fallback: "waiting for run data" }') >= 2  # :533/:661 sinks
        # RAW-token CSS keying survives the display remap:
        assert '"oc-pill oc-pill-" + rawState.replace(/[^a-z]/g, "-")' in js
        assert '"oc-verify-state oc-verify-" + rawVerify' in js

    def test_remap_keeps_the_verbatim_server_string_in_the_title(self) -> None:
        """Law 2: every remapped string keeps 'server: <verbatim>' reachable."""
        copy_js = _js(_COPY)
        assert 'el.title = "server: " + s;' in copy_js
        oneclick = _js(_ONECLICK)
        assert '"server state: " + rawState' in oneclick
        assert '"server: " + rawVerify' in oneclick
        assert '"server: " + rawSource' in oneclick

    def test_chain_derived_from_surfaces_verbatim_tooltip_only(self) -> None:
        """J15: the chain link's derived_from is surfaced VERBATIM (tooltip)."""
        js = _js(_ONECLICK)
        assert 'chip.title = String(link.derived_from || "");' in js

    def test_partial_zero_never_fabricates_the_real_zero_outcome(self) -> None:
        """Review round 1: the run-outcome sentence ("finished with no
        surviving strategies") is GATED on status "ok" — a DAMAGED top100
        (partial, 0 readable rows) renders the honest count-unknown line and
        the rank-1 card never claims "no winners stored"."""
        js = _js(_ONECLICK)
        assert 'if (payload.row_count === 0 && payload.status === "ok") {' in js
        assert "winner count unknown — the stored list couldn't be read" in js
        assert '"winner list unreadable — count unknown"' in js
        assert "fillRankOneCard(payload.rows || [], payload.status);" in js

    def test_winner_note_is_a_storage_claim_never_a_run_outcome(self) -> None:
        """Review round 3: payload.row_count is len(rows) of the CAPPED,
        family-collapsed top-100 artifact (representatives truncated at
        TOP100_LIMIT=100), structurally NOT the survivor count — "N
        strategies survived" fabricated a run-outcome claim from a payload
        number (a >100-survivor sweep read "100 strategies survived" beside
        the hero's real survivor figure). The note is a plural-correct
        STORAGE claim; the damaged-PARTIAL arm — where the count is only the
        readable prefix — says "readable" instead of "stored"."""
        js = _js(_ONECLICK)
        assert "strategies survived" not in js
        assert '(payload.row_count === 1 ? " winning strategy" : " winning strategies")' in js
        assert '? nWinners + " readable"' in js
        assert ': nWinners + " stored"' in js

    def test_trades_note_pluralizes_the_one_trade_case(self) -> None:
        """Review round 3 copy edit: "Showing 1 trades" read ungrammatically
        on a realistic 1-trade page (both the known-total and total-unknown
        arms)."""
        js = _js(_ONECLICK)
        assert '(shown === 1 ? " trade" : " trades") + " (total unknown)"' in js
        assert '(payload.total === 1 ? " trade" : " trades")' in js

    def test_awaiting_banner_uses_the_server_detail_for_a_selected_run(self) -> None:
        """Review round 1: a SELECTED run in the awaiting state renders the
        server detail via the catalog ("Run record not written yet." — the
        record artifact is what's missing), never the false "No completed
        run yet" claim; the fixed constant survives for the genuinely-no-run
        state (the C-NIT-3 pin above)."""
        js = _js(_ONECLICK)
        assert "if (state.selectedRunId && window.BXCopy) {" in js

    def test_direct_write_arms_clear_any_stale_server_title(self) -> None:
        """Review round 1 (Law 2): a direct textContent write on a REUSED
        note/badge element clears the previous state's 'server: …' title —
        a wrong audit attribution is worse than none."""
        js = _js(_ONECLICK)
        assert js.count('removeAttribute("title")') >= 5
        copy_js = _js(_COPY)
        assert 'el.removeAttribute("title");' in copy_js


class TestChartAndExplainSourcePins:
    """The browser-only halves of bx-chart.js / bx-explain.js (source pins)."""

    def test_chart_dropped_arrow_note_constant(self) -> None:
        js = _js(_CHART)
        assert 'var BX_ARROW_DROPPED_NOTE = "trade outside chart window — widen range";' in js

    def test_chart_svg_builders_use_createelementns_and_setattribute_only(self) -> None:
        js = _js(_CHART)
        assert "document.createElementNS(BX_SVG_NS, tag)" in js
        assert "n.setAttribute(k, String(attrs[k]));" in js

    def test_explain_mode_classes_reuse_the_v29_jargon_convention(self) -> None:
        """C-MINOR-5: ONE mechanism — the v29 .jargon/.plain-eng span classes."""
        js = _js(_EXPLAIN)
        assert 'root.classList.toggle("share-mode", beginner);' in js
        assert 'root.classList.toggle("operator-mode", !beginner);' in js

    def test_explain_hover_writes_textcontent_only(self) -> None:
        js = _js(_EXPLAIN)
        assert "tip.textContent = line;" in js

    def test_screener_columns_config_is_exported_for_the_screener_page(self) -> None:
        js = _js(_SCREENER)
        assert "var BX_SCREENER_COLUMNS = [" in js
        assert "COLUMNS: BX_SCREENER_COLUMNS," in js


class TestNodePresenceOnCi:
    """T-M3: the node-golden suites must never silently skip on the runners."""

    def test_node_is_present_on_ci_runners(self) -> None:
        if not os.environ.get("GITHUB_ACTIONS"):
            pytest.skip("CI-only guard — locally node may legitimately be absent")
        assert shutil.which("node") is not None, (
            "node is required on CI runners: the bx-chart/bx-screener/bx-explain "
            "goldens execute the shipped JS for real (the bx-format.js precedent) "
            "and must not silently skip"
        )
