"""Static-template pins — /results year strip + drill + legend (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

The ``test_dashboard_results_template_era.py`` convention: static-template
substring assertions (no JS engine — the honest envelope: these pin PRESENCE
of the markup/JS, not execution; the route integration suite drives the
payloads). Pins: the four new columns + the ▸ drill expander, the
textContent-only detail builder, the client-side class WHITELIST (no payload
string ever concatenated into a class name), the header chips, and the
verbatim legend text (including the H₀ honesty sentence).
"""

# The template renderer is module-private by design (the era-pin convention).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from brutex.dashboard.app import _render_results_html


def test_results_ships_the_temporal_columns() -> None:
    html = _render_results_html()
    assert '["temporal_judged_years", "Years", yearStrip]' in html
    assert '["max_year_share", "Yr share"' in html
    assert '["year_gate_passed", "Yr gate", yearGateBadge]' in html
    assert '["cascade_verdict", "Cascade", cascadeBadge]' in html
    assert 'class="tempo-btn" data-run="' in html


def test_year_strip_renders_hollow_zero_trade_cells() -> None:
    """A judged year with no trades renders the hollow chip + an honest 'dd —'
    tooltip (never a fabricated 0-dd) — the zero-illusion law."""
    html = _render_results_html()
    assert '": 0 trades · dd —"' in html
    assert 'let cls = "zero";' in html


def test_badge_classes_are_a_client_side_whitelist() -> None:
    """CSS classes come ONLY from literals chosen by boolean/sign checks — a
    payload string is never concatenated into a class attribute."""
    html = _render_results_html()
    for cls in ("b-pass", "b-fail", "b-abstain", "b-amb", "b-prov"):
        assert f'class="badge {cls}"' in html
    # The verdict string is COMPARED, never inserted as a class.
    assert 'if (v === "ambiguous")' in html
    assert 'if (v === "passed")' in html


def test_drill_builder_is_textcontent_only() -> None:
    """Every detail-table node is built via createElement/textContent — no
    payload string reaches innerHTML in the expander path."""
    html = _render_results_html()
    assert "tempoTable" in html
    assert "document.createElement" in html
    assert "td.textContent = num(v);" in html
    assert "note.textContent = text;" in html
    # The endpoint URL building escapes the run_id into the query string.
    assert "encodeURIComponent(runId)" in html


def test_escaped_payload_strings_in_cell_renderers() -> None:
    """Server strings in the innerHTML-composed table cells go through esc()."""
    html = _render_results_html()
    assert "esc(r.run_id)" in html
    assert "esc(r.cascade_rejected_at)" in html
    assert "(r.year_gate_reasons || []).map((t) => esc(t))" in html


def test_header_chips_present() -> None:
    html = _render_results_html()
    assert "board.temporal_coverage" in html
    assert "board.cascade_report" in html
    assert 'temporal stats: "' in html
    assert 'cascade report: "' in html


def test_legend_verbatim_substrings() -> None:
    """The legend ships the pinned honesty sentences VERBATIM (Q4)."""
    html = _render_results_html()
    assert 'id="tempoLegend"' in html
    assert "Year/quarter/month buckets: IST exit-date attribution" in html
    assert "understates boundary-spanning drawdowns" in html
    assert "Half-year DD is not stored" in html
    assert "Yr-gate chips are DISPLAY verdicts at current default thresholds" in html
    assert "it does NOT remove curve-fitting" in html
    assert "passes ~50% of" in html
    assert "— = no stored data / not judged." in html
