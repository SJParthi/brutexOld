"""Page-level pins for the operator-page real wiring (DEC-OPERATOR-PAGE-REAL-001).

Stage-3 surface: the v29 PREVIEW mockup is KILLED by a mechanical sweep and the
``/`` · ``/screener`` · ``/strategy`` pages are wired to the
``/api/v1/oneclick/*`` artifact contracts. This suite drives the REAL routes
via ``build_app`` + ``TestClient`` and pins:

* the three pages serve 200 (plus /reports returned to pre-open ONLY);
* the killed mockup literals are GONE — preview badge/ribbon markup AND their
  CSS selector blocks (F-m1), the fabricated CHARGES table, podium / rule-card
  / reel mockups, invented strategy names, hardcoded funnel counts, and a
  GENERIC zero-₹-followed-by-digit pin scoped to v29.html ONLY (C-B1);
* the honest awaiting states + the new one-click sections (run bar · run
  controls · chain map · progress strips · timeline · VERIFY-ME panel);
* the funnel two-writer precedence surfaces (C-M2): provenance label node +
  the manifest-only-when-idle law in bx-oneclick.js + the live writer's
  provenance stamp in the v29 inline poller;
* the strategy trades section renders INLINE, never tab-gated (F-M5), with
  the visible trades ``source`` badge (C-M5);
* one-shot failures route through the BXUI errorCard with Retry and
  "Loading…" is never a terminal element state (F-M4);
* the ticker strip is on all three pages (R36 "always visible");
* stale "52-condition" display copy is gone from EVERY template (66 declared / 64 active now);
* the hostile fixture's strings survive ONLY as JSON data through the routes
  and the new templates carry ZERO string-to-DOM assignment sinks (F-M2).

HONEST envelope (Golden Rule #10): template/JS assertions are SOURCE-STRING
pins (presence, not execution — the tests/browser Playwright job (#1557)
does not drive the oneclick pages); the
route assertions ARE executed end-to-end against the committed contract
fixture corpus.
"""

from __future__ import annotations

import re
import shutil
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.dashboard import oneclick_view
from brutex.dashboard.app import DashboardState, build_app
from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATES = _REPO / "brutex" / "dashboard" / "templates"
_STATIC = _REPO / "brutex" / "dashboard" / "static"
_FIXTURE_RUNS = _REPO / "tests" / "fixtures" / "oneclick" / "sweep_runs"

_RUN_ID = "r-20260707-fixt01"
_HOSTILE_RUN_ID = "r-hostile01"

#: The exact awaiting-banner wording (BX_AWAITING_TEXT twin — C-NIT-3).
_AWAITING = "awaiting real run artifact"


def _template(name: str) -> str:
    text = (_TEMPLATES / name).read_text(encoding="utf-8")
    assert text, f"expected a non-empty template at {name}"
    return text


def _static_js(name: str) -> str:
    """Read a shared static JS module (DEC-OPERATOR-PAGE-CONSOLIDATE-001:
    the screener/strategy page wiring moved VERBATIM out of the template
    inline scripts into mountable modules — the pins below keep their
    assertion text and only changed the file they grep)."""
    text = (_STATIC / name).read_text(encoding="utf-8")
    assert text, f"expected a non-empty static module at {name}"
    return text


class _MarkupHandlerScanner(HTMLParser):
    """Collect every raw inline ``on*=`` handler attribute in real MARKUP.

    CodeQL hardening (js/bad-tag-filter, code-scanning alert 1010): the
    prior regex pair — strip ``<script…>…</script>`` blocks with
    ``</script>`` spelled EXACTLY, then grep the remainder for handler
    attributes — missed end-tag spelling variants (``</script >``,
    ``</SCRIPT\\t>``, ``</script foo>``), so a variant-spelled end tag made
    the non-greedy stripper run on to the NEXT real ``</script>`` and
    over-strip genuine markup out of the scan. The stdlib
    ``html.parser.HTMLParser`` does the tag grammar natively (the
    frontend-latency suite precedent — its NIT-1 extractor carries the same
    rationale): ``<script>`` bodies arrive via ``handle_data`` (script
    CDATA content mode) and never surface as start tags, so JS comparisons
    like ``a < b`` — and handler-shaped STRINGS inside a script body —
    still cannot false-positive, while every end-tag spelling a browser
    honours terminates the element (variant matrix pinned by
    ``test_inline_handler_scan_catches_end_tag_spelling_variants``).
    This is a TEST-side reader of our own trusted templates, never a
    sanitizer. ``startswith("on")`` is deliberately BROADER than the
    retired ``\\son[a-z]+\\s*=`` regex (valueless ``<img onerror>`` and
    exotic handler spellings now flag too) — strictly stronger, corpus
    re-verified zero-hit.
    """

    def __init__(self) -> None:
        super().__init__()
        self.handler_attrs: list[str] = []

    def _scan_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, _value in attrs:
            # HTMLParser lowercases attribute names, so ONCLICK= is covered.
            if name.startswith("on") and len(name) > 2:
                self.handler_attrs.append(f"<{tag} … {name}=…>")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._scan_tag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._scan_tag(tag, attrs)


def _inline_handler_attrs(html: str) -> list[str]:
    """Every raw inline ``on*=`` handler attribute in ``html``'s markup."""
    parser = _MarkupHandlerScanner()
    parser.feed(html)
    parser.close()
    return parser.handler_attrs


def _client_no_lake() -> TestClient:
    return TestClient(build_app(DashboardState(), instrument_store=None))


def _client_with_corpus(tmp_path: Path) -> TestClient:
    lake = tmp_path / "lake"
    (lake / "sweeps").mkdir(parents=True)
    shutil.copytree(_FIXTURE_RUNS, lake / "sweep_runs")
    store = SimpleNamespace(path=lake / "instruments.duckdb")
    return TestClient(build_app(DashboardState(), instrument_store=store))


# ---------------------------------------------------------------------------
# Pages serve
# ---------------------------------------------------------------------------


class TestPagesServe:
    @pytest.mark.parametrize("path", ["/", "/screener", "/strategy"])
    def test_page_serves_200_html(self, path: str) -> None:
        resp = _client_no_lake().get(path)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "BruteX" in resp.text

    def test_reports_page_is_preopen_only(self) -> None:
        """R25: the Run Sweep + cascade cards are DELETED from /reports."""
        resp = _client_no_lake().get("/reports")
        assert resp.status_code == 200
        text = resp.text
        assert "Pre-Open" in text or "preopen" in text
        assert 'id="run-sweep"' not in text
        assert 'id="run-cascade"' not in text
        assert 'id="swSymbols"' not in text
        # The POST endpoints themselves stay byte-unchanged (only the page
        # moved) — the trigger URLs are gone from THIS page.
        assert "/api/v1/cascade/run" not in text

    def test_operator_page_carries_the_run_controls(self) -> None:
        """R25/O-b: the ONE canonical Run button now lives on the Operator page."""
        html = _template("v29.html")
        assert 'id="ocRunControls"' in html
        assert 'id="run-sweep"' in html
        assert 'id="run-cascade"' in html
        assert "/api/v1/sweep/run" in html
        # R2 selectors present; the backend-unsupported condition-subset is
        # DISABLED + labeled — never silently dropped.
        for control in (
            "swSymbols",
            "swIntervals",
            "swStart",
            "swEnd",
            "swK",
            "swDirections",
            "swSl",
            "swTp",
            "swTop",
        ):
            assert f'id="{control}"' in html, control
        assert 'id="swConditions"' in html
        assert "disabled" in html.split('id="swConditions"')[1][:120]
        assert "condition-subset — not wired yet, lands at merge-integration" in html
        # The preflight/estimator strip is HONEST about not being wired (P1).
        assert "estimator not wired yet — lands at merge-integration (P1)" in html
        # R3: the button never pulls — stated on the page.
        assert "this button NEVER pulls" in html or "NEVER pulls" in html


# ---------------------------------------------------------------------------
# The mechanical kill sweep (C-B1) — negative pins
# ---------------------------------------------------------------------------


class TestMockupKilled:
    def test_preview_badge_and_ribbon_markup_gone(self) -> None:
        html = _template("v29.html")
        assert "preview-badge" not in html
        assert "preview-ribbon" not in html
        assert "pr-mark" not in html
        assert "Preview Data" not in html

    def test_preview_css_selector_blocks_gone(self) -> None:
        """F-m1: the CSS rule blocks died with the markup."""
        html = _template("v29.html")
        assert ".preview-badge {" not in html
        assert ".preview-ribbon {" not in html
        assert "previewPulse" not in html

    def test_generic_zero_rupee_amount_pin_v29_only(self) -> None:
        """C-B1: v29.html contains ZERO ₹-amounts (a ₹ immediately followed by
        a digit) in its SOURCE — every money figure now arrives client-side
        from the artifact contracts. Scoped to v29.html ONLY (movers.html
        legitimately keeps its SAMPLE fallback — T-NIT-4)."""
        html = _template("v29.html")
        hits = re.findall(r"₹\s?[\d,]", html)
        assert hits == [], f"hardcoded ₹ amounts survived the kill sweep: {hits!r}"

    def test_fabricated_charges_table_deleted(self) -> None:
        """Track 1 is signal-only (CLAUDE.md §7 'Brokerage assumption: NONE')."""
        html = _template("v29.html")
        for token in (
            "chargesPanel",
            "proxySelect",
            "ch-gross",
            "ch-brok",
            "ch-stt",
            "ch-total",
            "chargesData",
        ):
            assert token not in html, f"charges mockup token survived: {token}"

    def test_podium_rule_cards_and_reel_mockups_deleted(self) -> None:
        html = _template("v29.html")
        # MARKUP-shaped tokens (the dead CSS selectors for the deleted nodes
        # may linger — the plan mandates CSS deletion only for the preview
        # badge/ribbon; the NODES must be gone).
        for token in (
            'class="podium-card',
            'class="rule-card ',
            'id="reelOverlay"',
            'class="reel-slide',
            "data-slot-final",
            "slotReveal",
            "data-countup",
        ):
            assert token not in html, f"mockup token survived: {token}"

    def test_invented_strategy_names_and_counts_gone(self) -> None:
        html = _template("v29.html")
        for token in (
            "SuperTrend",
            "Ichimoku",
            "EMA-cross",
            "MACD",
            "28.4M",
            "28.4 M",
            "12,847",
            "730 B",
            "730 BILLION",
            "#127",
            "#1247",
            "#1,247",
            "Dec 15, 2025",
            "+₹88,300",
            "₹95,600",
        ):
            assert token not in html, f"invented mockup literal survived: {token}"

    def test_hardcoded_funnel_widths_zeroed(self) -> None:
        """The staircase fills start at 0% (real widths are data-driven)."""
        html = _template("v29.html")
        assert re.search(r"--w:\s*(?!0%)[\d.]+%", html) is None, (
            "a hardcoded non-zero funnel width survived"
        )


# ---------------------------------------------------------------------------
# The new one-click surfaces
# ---------------------------------------------------------------------------


class TestFabricatedLiveTickerKilled:
    """Coordinator ruling (DEC-ONECLICK-TOP100-OBJECT-ROOT-001): the v24.4
    "LIVE" hook-banner ticker rendered 16 FABRICATED trade ticks behind a
    LIVE tag — a Golden-Rule-#1 violation. REMOVED outright (an absent
    widget beats a lying one); the HONEST #bxTickerStrip (sweep status +
    latest run id from the real runs payload, awaiting states) stays."""

    #: The fabricated hook-banner's distinctive literals — none may survive
    #: in ANY template (widget markup, CSS, JS observer hook, tick strings).
    _KILLED_TOKENS = (
        'id="liveTickerTrack"',
        "hook-banner .scroll-track",
        "tickerScroll",
        "+1.34%",
        "09:35\u219211:48",  # the first fabricated tick's time span
        "viral stopper",
    )

    @pytest.mark.parametrize(
        "template", ["v29.html", "screener.html", "strategy.html", "results.html"]
    )
    def test_fabricated_ticker_tokens_gone_everywhere(self, template: str) -> None:
        html = _template(template)
        for token in self._KILLED_TOKENS:
            literal = token.encode().decode("unicode_escape")
            assert literal not in html, f"fabricated LIVE-ticker token survived: {token}"

    def test_the_honest_ticker_strip_stays(self) -> None:
        """The REAL-data strip (bx-oneclick.js renderTicker — sweep status +
        latest run id, honest awaiting default) is the ONE ticker kept."""
        for template in ("v29.html", "screener.html", "strategy.html"):
            html = _template(template)
            assert 'id="bxTickerStrip"' in html, template
            assert "no runs yet — awaiting real run artifact" in html, template
        js = (_STATIC / "bx-oneclick.js").read_text(encoding="utf-8")
        assert "sweep: idle" in js  # derived states, never invented trades
        assert "latest run " in js


class TestOneclickSections:
    def test_awaiting_banner_present_with_the_shared_wording(self) -> None:
        html = _template("v29.html")
        assert 'id="ocAwaiting"' in html
        assert _AWAITING in html

    def test_new_sections_present(self) -> None:
        html = _template("v29.html")
        for anchor in (
            "ocRunBar",
            "ocRunPill",
            "ocRunSelect",
            "ocFixtureBadge",
            "ocUpdated",
            "ocChainMap",
            "ocProgressStrips",
            "ocTimeline",
            "ocTimelineSearch",
            "ocTimelineEarlier",
            "ocVerifyPanel",
            "ocVerifyChecks",
            "ocVerifyChainBtn",
            "ocVerifyFunnel",
            "ocEquityHost",
            "ocTradesBody",
            "ocTradesSource",
        ):
            assert f'id="{anchor}"' in html, f"missing one-click anchor #{anchor}"

    def test_data_oc_hooks_replace_the_hardcoded_card(self) -> None:
        html = _template("v29.html")
        for hook in (
            "r1_rank",
            "r1_name",
            "r1_instrument",
            "r1_net",
            "r1_max_dd",
            "r1_sharpe",
            "r1_dsr",
            "r1_cpcv",
            "r1_vix_band",
            "actual_enumerated",
            "actual_tested",
            "actual_skipped",
            "date_range",
        ):
            assert f'data-oc="{hook}"' in html, f"missing re-feed hook {hook}"

    def test_manifest_funnel_hooks_match_the_contract_stages(self) -> None:
        html = _template("v29.html")
        for stage in ("tested", "gates", "rank", "refine", "cascade"):
            assert f'data-oc-funnel="{stage}"' in html, stage

    def test_funnel_provenance_label_and_precedence_law(self) -> None:
        """C-M2: the provenance label sits beside the funnel; the manifest
        fill applies ONLY when idle; the LIVE writer stamps its provenance."""
        html = _template("v29.html")
        assert 'id="ocFunnelProvenance"' in html
        js = (_STATIC / "bx-oneclick.js").read_text(encoding="utf-8")
        assert "return !sweepRunning; // manifest fills only when idle (C-M2)" in js
        assert "funnelProvenance('live', funnel.sweep_id || '')" in html

    def test_winners_link_to_the_screener(self) -> None:
        html = _template("v29.html")
        assert 'href="/screener"' in html

    def test_v29_loads_the_oneclick_js_in_dependency_order(self) -> None:
        """The original 5-file law + the DEC-OPERATOR-PAGE-CONSOLIDATE-001
        extension (strictly stronger): the pure layers (bx-screener +
        bx-portal-state) precede bx-oneclick; the panel modules and the
        portal glue follow it."""
        html = _template("v29.html")
        order = [
            "/static/bx-ui.js",
            "/static/bx-poll.js",
            "/static/bx-explain.js",
            "/static/bx-chart.js",
            "/static/bx-screener.js",
            "/static/bx-portal-state.js",
            "/static/bx-oneclick.js",
            "/static/bx-screener-panel.js",
            "/static/bx-strategy-panel.js",
            "/static/bx-operator-portal.js",
        ]
        positions = [html.index(src) for src in order]
        assert positions == sorted(positions), "one-click JS load order drifted"

    def test_verify_panel_states_the_honest_truth_table(self) -> None:
        html = _template("v29.html")
        assert "VERIFY ME" in html
        assert "not_wired" in html
        assert "O(file)" in html  # the chain replay cost is stated on the button


# ---------------------------------------------------------------------------
# Screener + strategy page pins
# ---------------------------------------------------------------------------


class TestScreenerPage:
    def test_uses_the_one_shared_columns_config(self) -> None:
        """§11 COMMON: the panel consumes BXScreener.COLUMNS, no second list."""
        js = _static_js("bx-screener-panel.js")
        assert "BXScreener.COLUMNS" in js
        html = _template("screener.html")
        assert "/static/bx-screener.js" in html
        assert "/static/bx-screener-panel.js" in html

    def test_rows_are_createelement_only(self) -> None:
        js = _static_js("bx-screener-panel.js")
        assert "document.createElement('tr')" in js
        assert "document.createElement('td')" in js
        assert re.search(r"\.innerHTML\s*\+?=", js) is None
        assert re.search(r"\.innerHTML\s*\+?=", _template("screener.html")) is None

    def test_count_line_uses_the_payload_row_count_denominator(self) -> None:
        """T-M2: never a hardcoded 100."""
        js = _static_js("bx-screener-panel.js")
        assert "state.rowCount" in js
        assert "row_count" in js
        assert "' of ' + total + ' shown'" in js

    def test_count_line_numerator_is_the_rendered_count_naming_the_cap(self) -> None:
        """[W2-5] DEC-ONECLICK-TOP100-OBJECT-ROOT-001: '500 of 500 shown' over
        a RENDER_CAP-truncated 200-row table was a lie — the numerator is now
        the count actually rendered and a capped table SAYS so."""
        js = _static_js("bx-screener-panel.js")
        assert "String(cap) + ' of ' + total + ' shown'" in js
        assert "String(sorted.length) + ' of '" not in js  # the lying numerator is gone
        # A-r4 (DEC-ONECLICK-PERF-CLUB-001): the cap/note math lives in the
        # node-goldened BXScreener primitives — the shared panel consumes
        # them (the wording itself is pinned by
        # test_dashboard_screener_math.py). Re-pointed screener.html →
        # bx-screener-panel.js at the DEC-OPERATOR-PAGE-CONSOLIDATE-001
        # merge-reconcile (the IIFE moved into the panel, #1406 cap law
        # ported with it).
        assert "window.BXScreener.renderCount(sorted.length, state.showAll)" in js
        # The count line composes the capNote/inputCapNote primitives via
        # the node-goldened bxWinnersCountLine builder (its `screener`
        # param IS window.BXScreener at the ONE render call site —
        # executed-golden-proven in TestWinnersCountLine, stronger than a
        # call-site string).
        assert "line += screener.capNote(cap, sortedLen, showAll);" in js
        assert "line += screener.inputCapNote(inputDropped, payloadLen);" in js
        assert "applied.null_vix_excluded, window.BXScreener" in js
        assert "window.BXScreener.inputCapRows(" in js

    def test_sort_basis_label_names_the_artifact_preset(self) -> None:
        """[11] DEC-ONECLICK-TOP100-OBJECT-ROOT-001: the visible sort-basis
        label (textContent-only) names the artifact preset and the /results
        divergence — a LABEL, neither sort is changed."""
        html = _template("screener.html")
        assert 'id="scSortBasis"' in html
        assert "differs from /results (Rank-by metric, descending)" in html

    def test_null_vix_band_exclusion_is_counted(self) -> None:
        js = _static_js("bx-screener-panel.js")
        assert "null_vix_excluded" in js
        assert "nullVixNote" in js

    def test_row_click_reaches_the_strategy_page(self) -> None:
        """≤2 clicks: Operator → Winners → strategy (R36). The href shape
        lives in the ONE BXPortalState.strategyHref builder; the panel's
        DEFAULT row click navigates through it."""
        codec = _static_js("bx-portal-state.js")
        assert "/strategy?run_id=" in codec
        panel = _static_js("bx-screener-panel.js")
        assert "window.BXPortalState.strategyHref" in panel
        # The v29 drawer's open-full link consumes the SAME builder.
        assert 'id="ocDrawerOpenFull"' in _template("v29.html")
        assert "window.BXPortalState.strategyHref" in _static_js("bx-operator-portal.js")

    def test_render_cap_guard_present(self) -> None:
        js = _static_js("bx-screener-panel.js")
        assert "RENDER_CAP" in js
        assert 'id="scShowAll"' in _template("screener.html")

    def test_ticker_strip_and_awaiting_state(self) -> None:
        html = _template("screener.html")
        assert 'id="bxTickerStrip"' in html
        assert _AWAITING in html


class TestStrategyPage:
    def test_chart_first_layout_markers(self) -> None:
        html = _template("strategy.html")
        assert 'id="stChartHost"' in html
        assert "/static/bx-chart.js" in html
        assert "/api/v1/db/candles" in _static_js(
            "bx-strategy-panel.js"
        )  # the EXISTING candles API

    def test_trades_section_is_inline_never_tab_gated(self) -> None:
        """F-M5: the trade table container renders at load — the page carries
        NO tab-activation machinery at all."""
        html = _template("strategy.html")
        assert 'id="stTradesSection"' in html
        assert 'id="stTradesBody"' in html
        assert "tab-content" not in html
        assert "data-tab" not in html

    def test_trades_source_badge_visible(self) -> None:
        """C-M5: the fixture/results_store source is a VISIBLE badge — under
        the ONE renamed id (stTradesSource): v29 owns #ocTradesSource for its
        rank-1 trades tab, and bx-oneclick writes THAT unconditionally, so
        the strategy panel's badge carries its own st* id (the latent
        cross-page stomp is dead)."""
        html = _template("strategy.html")
        assert 'id="stTradesSource"' in html
        assert 'id="ocTradesSource"' not in html  # the rename is total here
        assert "source: none" in html
        assert "byId('stTradesSource')" in _static_js("bx-strategy-panel.js")
        # The v29 drawer copy carries the SAME renamed badge (its own
        # rank-1 #ocTradesSource stays, unstomped).
        v29 = _template("v29.html")
        assert 'id="stTradesSource"' in v29
        assert 'id="ocTradesSource"' in v29  # the operator rank-1 badge remains

    def test_null_cost_rows_get_the_gross_equity_caveat(self) -> None:
        """[W2-4] DEC-ONECLICK-TOP100-OBJECT-ROOT-001: Ruling-18 null-cost
        rows render a LABELED gross curve on /strategy AND the operator page
        (never an empty chart / a mislabeled non-integer error)."""
        panel = _static_js("bx-strategy-panel.js")
        assert "series.null_cost_rows" in panel
        assert "curve is gross of those costs" in panel
        js = (_STATIC / "bx-oneclick.js").read_text(encoding="utf-8")
        assert "series.null_cost_rows" in js
        assert "curve is gross of those costs" in js

    def test_out_of_window_trade_drops_the_arrow_loudly(self) -> None:
        """F-B1: the -1 sentinel is honored — never a snapped wrong candle."""
        panel = _static_js("bx-strategy-panel.js")
        assert "arrow.dropped" in panel
        assert "ARROW_DROPPED_NOTE" in panel
        js = (_STATIC / "bx-chart.js").read_text(encoding="utf-8")
        assert "trade outside chart window — widen range" in js

    def test_trade_centered_window(self) -> None:
        """F-B1a: ?trade= centers the candle fetch on that trade's entry
        (the ?trade= parse lives in the pure BXPortalState codec)."""
        assert "params.get('trade')" in _static_js("bx-portal-state.js")
        assert "state.tradeIdx" in _static_js("bx-strategy-panel.js")

    def test_equity_curve_sources_are_honestly_labeled(self) -> None:
        """C-MINOR-7, superseded by DEC-ONECLICK-WINNER-DETAIL-TRUTH-001 item
        4C: the STORED minute-path series (the /api/v1/oneclick/equity route)
        renders when the sidecar exists, and the per-trade cumsum FALLBACK is
        relabeled so the smoother curve never masquerades as the minute path."""
        panel = _static_js("bx-strategy-panel.js")
        assert "/api/v1/oneclick/equity" in panel
        assert "minute-path equity — the stored real series (R27)" in panel
        assert "per-trade close equity (minute-path unavailable for this run)" in panel
        # The stale "lands with P3" deferral is DEAD — the route ships.
        assert "minute-path equity lands with P3" not in panel
        # Hostile-round fold: an ok-verdict sidecar with ZERO rows reads its
        # OWN honest label (empty-for-this-run ≠ unavailable).
        assert "per-trade close equity (minute-path series empty for this run)" in panel
        assert "state.equityOkEmpty" in panel
        # Audit re-break MINOR: the per-trade fallback note total carries the
        # #12 gross/signal-only vocabulary — never a bare "net" claim.
        assert "bits.push('gross signal ' + money(series.final_paisa));" in panel
        assert "bits.push('net ' + money(series.final_paisa));" not in panel  # killed
        # BOTH panel hosts exist — the strategy page AND the v29 drawer copy
        # (the operator #ocEquityHost stays per-trade — the #48 ruling).
        assert 'id="stEquityHost"' in _template("strategy.html")
        v29 = _template("v29.html")
        assert 'id="stEquityHost"' in v29
        assert 'id="ocEquityHost"' in v29

    def test_vix_band_on_the_story_card(self) -> None:
        """R31 display duty — via the ONE shared builder ([W3-R4]): a
        null-lo/hi writer band reads the honest not-stamped copy, never
        "works when VIX between null–null"."""
        assert 'id="stVixBand"' in _template("strategy.html")
        panel = _static_js("bx-strategy-panel.js")
        assert "window.BXOneclick.vixBandText(row.vix_band)" in panel
        js = (_STATIC / "bx-oneclick.js").read_text(encoding="utf-8")
        assert "works when VIX between" in js
        assert "VIX band not stamped for this run" in js
        # The old inline undefined-only gate never returns — the panel
        # delegates to the shared builder, no hand-rolled copy anywhere.
        assert "works when VIX between" not in panel
        assert "works when VIX between" not in _template("strategy.html")

    def test_candle_mapper_reads_the_route_timestamp_column(self) -> None:
        """[W3-R1]: the chart mapper is the ONE shared route-shape mapper —
        the dead ``idx.ts`` read (the /api/v1/db/candles column is named
        "timestamp") can never return; parsing + the session-day anchor are
        TZ-safe (never a browser-local Date roundtrip)."""
        panel = _static_js("bx-strategy-panel.js")
        assert "window.BXChart.candleRowsFromDbPayload(payload)" in panel
        assert "window.BXChart.parseTsIst(raw)" in panel
        assert "window.BXChart.istDateOfEpoch(anchor)" in panel
        # The dead indexing EXPRESSION is gone (module AND template).
        assert "[idx.ts]" not in panel
        assert "toISOString().slice(0, 10)" not in panel
        html = _template("strategy.html")
        assert "[idx.ts]" not in html
        assert "toISOString().slice(0, 10)" not in html
        js = (_STATIC / "bx-chart.js").read_text(encoding="utf-8")
        assert '"timestamp", "open", "high", "low", "close"' in js
        assert "+05:30" in js  # naive strings pinned to IST, never browser-local

    def test_screener_band_cell_uses_the_shared_builder(self) -> None:
        """[W3-R4] screener band column: a null-lo/hi band cell reads
        "not stamped", never "null–null"."""
        js = _static_js("bx-screener-panel.js")
        assert "window.BXOneclick.vixBandCellText(v)" in js

    def test_beginner_pro_toggle_is_the_shared_mechanism(self) -> None:
        """C-MINOR-5: ONE mechanism — bxApplyMode over .jargon/.plain-eng."""
        for name in ("strategy.html", "screener.html", "v29.html"):
            html = _template(name)
            assert 'id="bxModeToggle"' in html, name

    def test_hover_explain_wiring(self) -> None:
        """Hover explain is bound EXACTLY ONCE — by bx-oneclick's
        presence-guarded init (#bxTip exists ⇒ bind); NEITHER panel module
        may carry a second bind (the single-bind law)."""
        html = _template("strategy.html")
        assert 'id="bxTip"' in html
        assert 'id="bxTip"' in _template("screener.html")
        assert 'id="bxTip"' in _template("v29.html")  # the portal gains the node
        assert "data-explain=" in html
        oneclick = (_STATIC / "bx-oneclick.js").read_text(encoding="utf-8")
        assert "bindExplainHover" in oneclick
        assert "bindExplainHover" not in _static_js("bx-strategy-panel.js")
        assert "bindExplainHover" not in _static_js("bx-screener-panel.js")
        assert "bindExplainHover" not in _static_js("bx-operator-portal.js")

    def test_regime_renders_and_family_stays_named_out_of_scope(self) -> None:
        """HIGH #13 (DEC-ONECLICK-WINNER-DETAIL-TRUTH-001): the regime
        breakdown RENDERS from entry.regime — the stale "lands with the P3
        data" deferral is DELETED (the data ships in the artifact); family
        drill-down (R34) stays a NAMED later PR."""
        html = _template("strategy.html")
        panel = _static_js("bx-strategy-panel.js")
        assert "land with the P3 data they visualize" not in html  # killed token
        assert "land with the P3 data they visualize" not in panel  # killed token
        assert 'id="stTrendSplit"' in html
        assert 'id="stRegimeWonder"' in html
        assert "window.BXOneclick.trendSplitText(reg.by_trend, money)" in panel
        assert (
            "window.BXOneclick.regimeWonderText(reg.one_regime_wonder, reg.regime_wonder_axis)"
            in panel
        )
        assert "family drill-down (R34) is a named later PR" in html

    def test_ticker_strip_present(self) -> None:
        assert 'id="bxTickerStrip"' in _template("strategy.html")


# ---------------------------------------------------------------------------
# F-M4: failure → errorCard + Retry; "Loading…" never terminal
# ---------------------------------------------------------------------------


class TestFailureLaw:
    def test_oneshot_failures_route_through_the_errorcard(self) -> None:
        js = (_STATIC / "bx-oneclick.js").read_text(encoding="utf-8")
        assert "errorCard" in js
        assert "retry:" in js
        for name in ("bx-screener-panel.js", "bx-strategy-panel.js"):
            panel = _static_js(name)
            assert "oneshotFetch" in panel, name
        # The templates hand their wiring to the self-mounting modules.
        assert 'data-bx-autopanel="screener"' in _template("screener.html")
        assert 'data-bx-autopanel="strategy"' in _template("strategy.html")

    @pytest.mark.parametrize("name", ["v29.html", "screener.html", "strategy.html"])
    def test_loading_is_never_a_terminal_element_state(self, name: str) -> None:
        html = _template(name)
        assert ">Loading…<" not in html
        assert ">Loading...<" not in html


# ---------------------------------------------------------------------------
# Stale copy sweep: "52-condition" → the 66-declared / 64-active wording
# ---------------------------------------------------------------------------


class TestVocabularyCopy:
    def test_no_template_still_says_52_condition(self) -> None:
        for path in sorted(_TEMPLATES.glob("*.html")):
            text = path.read_text(encoding="utf-8")
            assert "52-condition" not in text, path.name
            assert "52 condition" not in text, path.name

    def test_welcome_says_66_declared_64_active_u128(self) -> None:
        # The honest wording (TOMB-05 + the DEC-SUPERTREND-CONDITIONS-001
        # implemented-flip): 66 DECLARED bit positions, 64 ACTIVE — never a
        # bare count that reads dead bits as live, never a stale 62 that
        # reads the LIVE SuperTrend pair as declared-only.
        html = _template("welcome.html")
        assert "66-condition" in html
        assert "64 active" in html
        assert "62 active" not in html
        assert "60-condition" not in html
        assert "u128" in html
        assert "u64" not in html
        # The retired VWAP pair must not be advertised as an evaluated family.
        assert "Fibonacci + VWAP" not in html


# ---------------------------------------------------------------------------
# Route-driven: honest states + the hostile fixture (F-M2 positive escape)
# ---------------------------------------------------------------------------


class TestRoutesEndToEnd:
    def test_manifest_payload_carries_chain_links_for_the_chain_map(self, tmp_path: Path) -> None:
        """R41: the chain map's link states are SERVER truth (oneclick_view
        .chain_links wired into the manifest route; fixed stage order)."""
        client = _client_with_corpus(tmp_path)
        payload = client.get(f"/api/v1/oneclick/manifest?run_id={_RUN_ID}").json()
        links = payload["chain_links"]
        assert [link["stage"] for link in links] == list(oneclick_view.CHAIN_STAGES)
        states = {link["state"] for link in links}
        assert states <= {"green", "amber", "red", "pending"}

    def test_chain_links_unknown_run_is_awaiting_not_500(self, tmp_path: Path) -> None:
        client = _client_with_corpus(tmp_path)
        resp = client.get("/api/v1/oneclick/manifest?run_id=r-nope")
        assert resp.status_code == 200
        assert resp.json()["status"] == "awaiting"

    def test_hostile_strings_survive_only_as_json_data(self, tmp_path: Path) -> None:
        """F-M2 positive escape: the hostile fixture's <script>/onerror=/<svg
        onload=> strings pass through the routes JSON-encoded; the pages'
        rendering layer is sink-free by construction (no innerHTML/
        insertAdjacentHTML/document.write assignment in any new JS or
        template inline script), so the strings can only ever land as
        textContent data."""
        client = _client_with_corpus(tmp_path)
        top = client.get(f"/api/v1/oneclick/top100?run_id={_HOSTILE_RUN_ID}")
        assert top.status_code == 200
        body = top.text
        # JSON-encoded (the raw markup form never appears unescaped as HTML —
        # the response is application/json, and the templates render it via
        # textContent only).
        assert top.headers["content-type"].startswith("application/json")
        assert "<script>alert(1)</script>" in top.json()["rows"][0]["conditions"] or (
            "alert(1)" in body
        )
        # The rendering layer: zero string-to-DOM sinks in every new surface.
        # v29.html JOINS the scan (DEC-OPERATOR-PAGE-CONSOLIDATE-001): its
        # three static-string sink sites (confetti reset + the two toggle
        # labels) were refactored sink-free in the SAME change.
        # WIDENED (adversarial-review fold, S-M1 — refuter-validated: zero
        # false positives on this exact corpus, 8/8 named bypass classes
        # caught): outerHTML + bracket-access innerHTML/outerHTML +
        # createContextualFragment + .srcdoc= + setAttribute('on…') +
        # eval/new Function + javascript: URL literals join the ban. The
        # raw-substring ban in test_dashboard_oneclick_frontend.py already
        # covers the JS files' innerHTML/outerHTML word forms; this regex is
        # the templates' (inline-script) guard + the executable-sink net.
        # WIDENED AGAIN (fix round 2, seccov LOW-1 — the classic
        # completeness regress: a second hostile iteration found classes
        # outside round 1's named set; corpus re-verified 0 hits): the
        # setHTMLUnsafe sink, DOMParser/parseFromString document
        # construction, and the setAttribute('srcdoc'|'src', …) attribute
        # form of the srcdoc/src sinks (the `.srcdoc=` property form was
        # already caught; `setAttribute('href', …)` is deliberately NOT
        # banned — the ONE bxStrategyHref builder legitimately sets
        # component-encoded hrefs). NOTE: `el[v] = x` with a runtime
        # v="innerHTML" stays caught for the 9 JS files by the raw-substring
        # ban in test_dashboard_oneclick_frontend.py (the string literal
        # itself trips it) — a residual gap only for template inline scripts
        # and concatenated forms, stated honestly.
        sink = re.compile(
            r"\.(inner|outer)HTML\s*\+?=|\[[\"'](inner|outer)HTML[\"']\]\s*="
            r"|insertAdjacentHTML|document\.write\(|createContextualFragment"
            r"|\.srcdoc\s*=|setAttribute\(\s*[\"']on|\beval\(|new\s+Function\("
            r"|[\"']javascript:"
            r"|setHTMLUnsafe|new\s+DOMParser|parseFromString"
            r"|setAttribute\(\s*[\"'](srcdoc|src)[\"']"
        )
        # Fix round 2 (seccov LOW-1): raw inline HTML `on*=` handler
        # attributes are banned in the scanned templates' MARKUP (script
        # bodies are excluded by the parser's CDATA content mode — JS
        # comparisons like `a < b` must never false-positive; the JS-side
        # `setAttribute('on…')` arm above covers dynamic handler
        # injection). The one pre-existing benign specimen (v29's modal
        # 'Got it' onclick) was converted to a proper addEventListener
        # binding in the same change, so the corpus is genuinely zero-hit.
        # CodeQL hardening (js/bad-tag-filter, alert 1010): the former
        # regex `<script\b.*?</script>` stripper missed `</script >`-style
        # end-tag variants — replaced by the HTMLParser-based
        # _MarkupHandlerScanner (rationale on the class).
        for name in ("screener.html", "strategy.html", "v29.html"):
            html = _template(name)
            assert sink.search(html) is None, name
            assert _inline_handler_attrs(html) == [], name
        for js_name in (
            "bx-oneclick.js",
            "bx-poll.js",
            "bx-chart.js",
            "bx-screener.js",
            "bx-explain.js",
            "bx-portal-state.js",
            "bx-screener-panel.js",
            "bx-strategy-panel.js",
            "bx-operator-portal.js",
        ):
            assert sink.search((_STATIC / js_name).read_text(encoding="utf-8")) is None, js_name

    @pytest.mark.parametrize(
        "end_tag",
        [
            "</script>",
            "</script >",
            "</script\t\n>",
            "</SCRIPT>",
            "</Script >",
        ],
    )
    def test_inline_handler_scan_catches_end_tag_spelling_variants(self, end_tag: str) -> None:
        """CodeQL js/bad-tag-filter teeth (alert 1010): the retired regex
        stripper spelled the end tag EXACTLY `</script>`, so a handler
        hidden between a variant-spelled end tag and the NEXT real script
        block was over-stripped out of the old scan (evasion, reproduced on
        the pre-fix pattern for every whitespace-bearing spelling below;
        the case-only variants were already covered by its IGNORECASE).
        The parser-based scan must flag it for every one of these
        stdlib-version-stable end-tag spellings."""
        specimen = (
            f"<script>var a = 1 < 2;{end_tag}"
            '<img src=x onerror="alert(1)">'
            "<script>var b = 2;</script>"
        )
        assert _inline_handler_attrs(specimen) == ["<img … onerror=…>"], end_tag

    @pytest.mark.parametrize("end_tag", ["</script foo>", "</script/>"])
    def test_inline_handler_scan_recovers_past_divergent_end_tag_spellings(
        self, end_tag: str
    ) -> None:
        """HONEST residual (Golden Rule #10): attribute/slash-bearing end
        tags (`</script foo>`, `</script/>`) DO terminate the script
        element in a browser, but ``html.parser`` only honours them on
        stdlib patch levels carrying the CDATA end-tag conformance
        backport (present on e.g. 3.11.15; absent on 3.12.3) — on older
        patch levels the in-between handler rides as script data,
        unflagged. Tolerant arm: either outcome is accepted, but the scan
        must NEVER go blind past the specimen — the next GENUINE
        ``</script>`` always re-opens markup scanning, so a real handler
        after it is flagged on EVERY stdlib (the invariant that matters
        for scanning our own trusted templates, whose real scripts all end
        ``</script>``)."""
        specimen = (
            f"<script>var a = 1 < 2;{end_tag}"
            '<img src=x onerror="alert(1)">'
            "<script>var b = 2;</script>"
            '<div onclick="x">t</div>'
        )
        hits = _inline_handler_attrs(specimen)
        assert hits in (
            ["<div … onclick=…>"],  # pre-backport: divergent tag rode as data
            ["<img … onerror=…>", "<div … onclick=…>"],  # conformant stdlib
        ), (end_tag, hits)

    def test_inline_handler_scan_never_false_positives_on_script_bodies(self) -> None:
        """The CDATA false-positive guard survives the regex→parser move:
        JS comparisons AND handler-shaped STRINGS inside a script body never
        flag; a real markup handler outside one still does."""
        clean = (
            '<script>if (a < b && c > d) { s = "<div onclick=x>"; }</script><div class="k">t</div>'
        )
        assert _inline_handler_attrs(clean) == []
        assert _inline_handler_attrs('<DIV ONCLICK="x">t</DIV>') == ["<div … onclick=…>"]
        # Valueless handler attributes flag too (broader than the old regex,
        # which required `=` — strictly stronger, stated on the scanner).
        assert _inline_handler_attrs("<img src=x onerror>") == ["<img … onerror=…>"]

    def test_screener_and_strategy_serve_with_corpus_lake(self, tmp_path: Path) -> None:
        client = _client_with_corpus(tmp_path)
        for path in ("/screener", "/strategy", "/"):
            assert client.get(path).status_code == 200

    def test_trades_route_source_badge_value_flows(self, tmp_path: Path) -> None:
        """Production default (DEC-ONECLICK-SWEEP-RUNS-REBIND-001): the
        TradeStoreReader — a pre-#1275 top100 without results-depth
        references reads honest awaiting, source 'none' (never fabricated)."""
        client = _client_with_corpus(tmp_path)
        payload = client.get(f"/api/v1/oneclick/trades?run_id={_RUN_ID}&rank=1").json()
        assert payload["source"] == "none"
        assert payload["status"] == "awaiting"
        assert "pre-#1275" in payload["detail"]


# ---------------------------------------------------------------------------
# app.py docstring copy
# ---------------------------------------------------------------------------


def test_app_leaderboard_docstring_carries_no_stale_vocab_count() -> None:
    app_src = (_REPO / "brutex" / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert "the 52-condition vocabulary" not in app_src
    assert "the 60-condition vocabulary" not in app_src  # count-free scope prose now
    assert "the condition vocabulary" in app_src
