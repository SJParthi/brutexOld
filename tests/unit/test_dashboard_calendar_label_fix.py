"""Pins the label-forwarded close-fix + the Movers/Reports heading accent bars
(DEC-DATEPICKER-LABEL-CLOSE-FIX-001).

* A loose ``<input type=date>`` nested in a ``<label>`` (Movers + Reports)
  re-fires drill clicks at the hidden native input; the shared picker's
  outside-close handler must treat its OWN backing input(s) as inside so the
  drill never self-closes before the user picks a day.
* The Movers + Reports main section headings get the same gradient accent bar
  (``h1::before``) the polished pages use, so they align to the card's left edge.
"""

from __future__ import annotations

from pathlib import Path

import brutex.dashboard
import pytest

_STATIC = Path(brutex.dashboard.__file__).resolve().parent / "static"
_JS = (_STATIC / "bx-datepicker.js").read_text(encoding="utf-8")

_ACCENT = "linear-gradient(180deg,var(--blue),var(--purple))"


def test_outside_close_ignores_the_pickers_own_input() -> None:
    """The label-forwarded synthetic click on our own hidden input must not close."""
    assert "e.target === fromInput" in _JS, "the from-input guard is missing"
    assert "!single && e.target === toInput" in _JS, "the to-input guard is missing"


def test_movers_reports_headings_have_the_accent_bar() -> None:
    """Movers + Reports section headings carry the gradient accent bar + a title."""
    pytest.importorskip("fastapi", reason="render needs the [ui] extra")
    pytest.importorskip("httpx", reason="TestClient needs httpx")
    from brutex.dashboard.app import DashboardState, build_app
    from fastapi.testclient import TestClient

    client = TestClient(build_app(DashboardState(), instrument_store=None))
    movers = client.get("/movers").text
    reports = client.get("/reports").text
    assert "h1::before" in movers and "📊 Markets Movers" in movers
    assert "h1::before" in reports
    assert movers.count(_ACCENT) >= 1, "movers heading accent bar missing"
    assert reports.count(_ACCENT) >= 1, "reports heading accent bar missing"


def test_doc_listeners_are_delegated_once() -> None:
    """The rebuild-leak fix's delegated design is pinned: instances REGISTER in
    the shared doc-target registry, and the document click/keydown listeners are
    attached exactly ONCE — never one per buildCalendar/buildSelect instance
    (the old per-instance pattern accumulated listeners + retained detached
    trees across rebuilds)."""
    assert "registerDocTarget" in _JS, "the shared doc-target registry is missing"
    assert _JS.count('document.addEventListener("click"') == 1, (
        "expected exactly ONE delegated document click listener"
    )
    assert _JS.count('document.addEventListener("keydown"') == 1, (
        "expected exactly ONE delegated document keydown listener"
    )
