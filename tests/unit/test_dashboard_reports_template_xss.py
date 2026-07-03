"""Regression: the ``/reports`` symbol chips render server fields via textContent.

Guards the DOM-XSS fix of **DEC-RESULTS-SURFACE-HONESTY-001**: the
``renderChips`` loop in ``brutex/dashboard/templates/reports.html`` built each
chip by string-concatenating the server-derived ``s.symbol`` / ``s.exchange``
into ``chip.innerHTML`` — a DOM-XSS sink (a hostile lake directory name such as
``<img src=x onerror=...>`` surfacing through
``/api/v1/reports/preopen/symbols`` would have executed). The fix builds the
chip via ``document.createElement`` + ``textContent`` (the repo law:
textContent-never-innerHTML for server-derived strings — the
``movers.html`` / ``db.html`` convention).

These are static-template assertions (no JS engine), matching the
``test_dashboard_movers_template_xss.py`` convention: they FAIL on the pre-fix
template (the ``chip.innerHTML = '<span>' + s.symbol + …`` concat) and pass
after the fix.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from brutex.dashboard.app import _render_reports_html


def test_reports_chip_no_longer_concatenates_into_innerhtml() -> None:
    """The vulnerable ``chip.innerHTML`` string-concat sink is GONE."""
    html = _render_reports_html()
    assert "chip.innerHTML" not in html, (
        "the chip must be built via createElement/textContent, never innerHTML"
    )
    # The exact pre-fix concat shapes must not reappear in any form.
    assert "'<span>' + s.symbol" not in html
    assert "s.exchange + '</span>'" not in html


def test_reports_chip_symbol_and_exchange_use_textcontent() -> None:
    """Both server-derived fields are assigned via ``textContent`` nodes."""
    html = _render_reports_html()
    assert "nameEl.textContent = s.symbol" in html
    assert "exchEl.textContent = s.exchange" in html
    # The badge is static vocabulary (OPT/SPOT) but rides the same safe build.
    assert 'badgeEl.textContent = s.option_pullable ? "OPT" : "SPOT"' in html
    assert "chip.append(nameEl, badgeEl, exchEl)" in html


def test_reports_no_server_symbol_field_reaches_an_innerhtml_write() -> None:
    """Defensive scan: no ``s.symbol`` / ``s.exchange`` in any innerHTML assignment."""
    html = _render_reports_html()
    for line in html.splitlines():
        if ".innerHTML" in line and "=" in line and ("s.symbol" in line or "s.exchange" in line):
            raise AssertionError(f"server field must not reach innerHTML: {line.strip()!r}")
