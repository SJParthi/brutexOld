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

import os
import shutil
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"

_POLL = _STATIC / "bx-poll.js"
_CHART = _STATIC / "bx-chart.js"
_SCREENER = _STATIC / "bx-screener.js"
_EXPLAIN = _STATIC / "bx-explain.js"
_ONECLICK = _STATIC / "bx-oneclick.js"

_ALL_FIVE = (_POLL, _CHART, _SCREENER, _EXPLAIN, _ONECLICK)
_IDS = ("bx-poll", "bx-chart", "bx-screener", "bx-explain", "bx-oneclick")

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
        assert 'window.BXUI.el("div", "oc-event-line", bxEventLineText(rows[i]))' in js

    def test_funnel_precedence_law_manifest_only_when_idle(self) -> None:
        """C-M2: the live poller wins while a sweep runs."""
        js = _js(_ONECLICK)
        assert "function bxFunnelWriteAllowed(source, sweepRunning)" in js
        assert "return !sweepRunning; // manifest fills only when idle (C-M2)" in js
        assert 'bxFunnelWriteAllowed("manifest", state.sweepRunning)' in js

    def test_funnel_provenance_labels(self) -> None:
        js = _js(_ONECLICK)
        assert '"live run " + String(runId)' in js
        assert '"completed run " + String(runId) + " · manifest"' in js

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
