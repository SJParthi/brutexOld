"""Regression pins: the ``/movers`` rows are click-through deep links to ``/db``.

Guards **DEC-MOVERS-DRILL-DOWN-001** — the audit's finding #27 (HIGH): the
``renderRows`` loop emitted plain ``<tr>``/``<td>`` rows with no coordinate
stamp and no click path, so every movers row was an INERT dead end. The fix
stamps ``data-symbol``/``data-exchange``/``data-segment`` (esc()'d, gated on a
real payload ``symbol`` so the offline SAMPLE preview rows and the empty-state
row stay inert) plus ``tabindex="0"`` + an esc()'d ``aria-label`` (NO
``role="link"`` — the tr keeps its native ``row`` role for table semantics,
review MINOR-1 fold), and adds ONE delegated
``#rows`` click + Enter listener pair building the ``/db`` deep link via
``URLSearchParams`` — carrying the RESOLVED snapshot day (``lastPayload.date``)
so a blank "latest day" input still links to the actual day shown.

Static-template PRESENCE pins (no JS engine — the DEC-PULL-UI-STALENESS-001
honest-envelope precedent: "substring tests pin PRESENCE of the JS, not
execution"), matching the ``test_dashboard_movers_template_xss.py``
convention; the real-DOM execution proof lives in
``tests/browser/test_movers_drilldown.py``. Every positive pin FAILS at base
1216789 (zero occurrences of ``data-symbol`` in movers.html there).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from brutex.dashboard.app import _render_movers_html


def test_rows_stamp_escaped_coordinate_attrs() -> None:
    """The (symbol, exchange, segment) triple is stamped esc()'d on each linked row."""
    html = _render_movers_html()
    # esc() rides EVERY attribute value — the same sanctioned innerHTML+esc path
    # the cells use (DEC-DASHBOARD-XSS-HARDENING-001); never a raw interpolation.
    assert "data-symbol=\"' + esc(row.symbol)" in html
    assert 'data-exchange="\' + esc(row.exchange || "")' in html
    assert 'data-segment="\' + esc(row.segment || "")' in html


def test_link_rows_gated_on_payload_symbol() -> None:
    """Only rows with a REAL payload symbol become links — SAMPLE/empty rows stay inert."""
    html = _render_movers_html()
    assert "const link = row.symbol" in html
    # The no-symbol arm stamps NOTHING (the SAMPLE rows carry no `symbol` key).
    assert '"<tr" + link + ">"' in html


def test_linked_rows_carry_a11y_affordance() -> None:
    """tabindex=0 + esc()'d aria-label ride the same gated stamp; NO role override.

    Review MINOR-1 fold: ``role="link"`` is deliberately ABSENT — it would
    strip the tr's native ``row`` role and break table semantics for screen
    readers; keyboard reachability rides tabindex + the Enter twin instead,
    and the aria-label (esc()'d, the sanctioned attribute path) names the
    destination.
    """
    html = _render_movers_html()
    assert '" tabindex="0"' in html
    assert "aria-label=\"open ' + esc(row.symbol) + ' in the database candle viewer\"" in html
    assert 'role="link"' not in html
    assert "tbody tr[data-symbol] { cursor:pointer; }" in html


def test_row_click_honors_live_text_selection() -> None:
    """A click ending a text selection is NOT swallowed into a navigation.

    Review MINOR-2 fold: the delegated click handler bails when a live
    (non-collapsed) selection exists, so the operator can still select/copy
    cell text off a linked row.
    """
    html = _render_movers_html()
    assert "if (!window.getSelection().isCollapsed) return;" in html


def test_delegated_rows_listener_not_per_row_onclick() -> None:
    """ONE delegated #rows listener (the onPillsClick/onHeadClick pattern) — no onclick= attrs."""
    html = _render_movers_html()
    assert '$("rows").addEventListener("click", onRowClick)' in html
    # Delegation law: no inline onclick= HTML attribute anywhere in the template
    # (JS `.onclick =` property assignments — tabs/refresh — are a different shape).
    assert 'onclick="' not in html


def test_enter_key_makes_rows_keyboard_reachable() -> None:
    """The Enter keydown twin of the click handler rides the same delegation."""
    html = _render_movers_html()
    assert '$("rows").addEventListener("keydown"' in html
    assert 'e.key !== "Enter"' in html


def test_url_builder_uses_urlsearchparams_and_segment_routing() -> None:
    """moversDbUrl keys off data-segment (self-describing rows), never state.asset."""
    html = _render_movers_html()
    assert "function moversDbUrl(tr)" in html
    assert "new URLSearchParams()" in html
    assert 'p.set("candles", seg === "FNO" ? "fno" : "spot")' in html
    # Spot rows carry the exchange/segment coordinate; F&O rows are the contract
    # groww_symbol alone (the /db side resolves underlying/expiry server-truth-gated).
    assert (
        'if (seg !== "FNO") { p.set("exchange", tr.dataset.exchange || ""); p.set("segment", seg); }'
        in html
    )
    assert 'p.set("interval", $("interval").value)' in html
    assert '"/db?" + p.toString()' in html


def test_deep_link_carries_the_resolved_snapshot_day() -> None:
    """The date comes from the payload echo (lastPayload.date), not the raw input.

    The payload ``date`` is the RESOLVED snapshot day (the as-of line already
    trusts it), so a blank date input ("latest day") still deep-links to the
    actual day shown — the honest choice (design §4).
    """
    html = _render_movers_html()
    assert '(lastPayload && lastPayload.date) || $("date").value' in html
