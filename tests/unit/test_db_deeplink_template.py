"""Regression pins: ``/db`` gains a boot-time deep-link driver (query params).

Guards **DEC-MOVERS-DRILL-DOWN-001** — the /db half of the movers row
drill-down: at base 1216789 the page was parameter-blind (``grep -c
URLSearchParams db.html`` → 0; the candle viewer was reachable ONLY by a
sidebar click). The fix adds ONE ``applyDeepLink()`` driver parsing
``?candles=spot|fno&symbol&exchange&segment&interval&date`` over the EXISTING
cascades, server-truth-gated at every level (an unmatched symbol / underlying
/ expiry / contract degrades to the honest "pick a …" prompt — never a wrong
or fabricated selection, Golden Rule #1), plus a backward-compatible
``prefInterval`` argument on the two existing picker handlers (the change
listeners pass no second arg — the manual path is byte-identical).

Static-template PRESENCE pins (no JS engine — the DEC-PULL-UI-STALENESS-001
honest-envelope precedent), matching the ``test_dashboard_db_ux.py``
convention; the real-DOM execution proof lives in
``tests/browser/test_movers_drilldown.py``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from brutex.dashboard.app import _render_db_html


def _apply_deep_link_span(html: str) -> str:
    """The applyDeepLink → boot() text span (the driver body + helpers' tail)."""
    start = html.index("async function applyDeepLink()")
    end = html.index("function boot()")
    return html[start:end]


def test_deep_link_parses_urlsearchparams() -> None:
    """The page reads location.search — parameter-blind no more (fails at base)."""
    html = _render_db_html()
    assert "new URLSearchParams(location.search)" in html


def test_apply_deep_link_defined_and_invoked_after_loadtables() -> None:
    """boot() calls applyDeepLink() AFTER loadTables() (order pin, boot body only)."""
    html = _render_db_html()
    assert "async function applyDeepLink()" in html
    boot_start = html.index("function boot() {")
    boot_body = html[boot_start : html.index("}", boot_start)]
    assert "loadTables()" in boot_body
    assert "applyDeepLink()" in boot_body
    assert boot_body.index("loadTables()") < boot_body.index("applyDeepLink()")


def test_unknown_kind_gate_preserves_todays_behavior() -> None:
    """No/unknown ?candles= → the driver returns before touching anything."""
    html = _render_db_html()
    assert 'if (kind !== "spot" && kind !== "fno") return;' in html


def test_date_param_is_regex_gated_before_state() -> None:
    """Only a \\d{4}-\\d{2}-\\d{2} date reaches cstate.start/cstate.end.

    cstate.start/end are the ONLY param-derived values that reach markup, and
    they render through the existing esc() value-attribute path — the regex
    gate runs FIRST.
    """
    html = _render_db_html()
    assert (
        r"if (/^\d{4}-\d{2}-\d{2}$/.test(date)) { cstate.start = date; cstate.end = date; }" in html
    )


def test_expiry_parse_is_server_truth_gated() -> None:
    """The DDMmmYY→ISO result is returned ONLY when ∈ cstate.fno.exps.

    The never-select-a-wrong-expiry law: a mis-parse can only fail to match
    and degrade to the honest "pick an expiry" prompt.
    """
    html = _render_db_html()
    assert 'return cstate.fno.exps.includes(iso) ? iso : "";' in html
    # The gate sits BEFORE the expiry selection call in the driver.
    span = _apply_deep_link_span(html)
    assert span.index("_fnoExpiryFor(symbol)") < span.index("await onFnoExpiryChange(exp)")
    assert "if (!exp) return;" in span


def test_underlying_resolved_by_longest_prefix_over_server_list() -> None:
    """The underlying comes from the SERVER's own list (longest prefix), never a grammar."""
    html = _render_db_html()
    assert "function _fnoUnderlyingFor(symbol)" in html
    assert 'symbol.startsWith(exch + "-" + u + "-")' in html
    assert "u.length > best.length" in html


def test_spot_and_contract_selection_are_membership_gated() -> None:
    """A symbol/contract the cascade payloads do not list is never selected."""
    html = _render_db_html()
    assert "cstate.spot.syms.some(s => _spotKey(s) === key)" in html
    assert "cstate.fno.cons.some(c => c.symbol === symbol)" in html


def test_picker_handlers_gain_backward_compatible_pref_interval() -> None:
    """Signature + fallback-chain pins: prefInterval wins only when STORED."""
    html = _render_db_html()
    assert "function onSpotSymbolChange(key, prefInterval)" in html
    assert "function onFnoContractChange(symbol, prefInterval)" in html
    chain = (
        "const pref = (prefInterval && cstate.datasets.find(x => x.interval === prefInterval))\n"
        '    || cstate.datasets.find(x => x.interval === "1minute") || cstate.datasets[0];'
    )
    assert html.count(chain) == 2, "both picker handlers must ride the same fallback chain"
    # The existing change listeners pass NO second arg — manual path byte-identical.
    assert 'csym.addEventListener("change", () => onSpotSymbolChange(csym.value));' in html
    assert 'ccon.addEventListener("change", () => onFnoContractChange(ccon.value));' in html


def test_no_param_value_reaches_innerhtml_in_the_driver() -> None:
    """Params feed only comparisons/cstate/existing fetch builders — never innerHTML."""
    html = _render_db_html()
    span = _apply_deep_link_span(html)
    assert "q.get(" in span
    assert "innerHTML" not in span
