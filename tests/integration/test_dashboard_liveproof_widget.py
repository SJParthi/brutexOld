"""The site-wide ``bx-liveproof.js`` status widget (DEC-DOCTOR-LIVE-AUTORUN-001).

The zero-command autorun's UI surface: a self-injecting pill + red-banner
widget every dashboard page loads (the same site-wide pattern as
``bx-rate-monitor.js``), polling ``GET /api/v1/liveproof``. These tests pin
the widget's honesty markers (no fake green, the operator-decreed
``not applicable`` sentence, textContent-only rendering) and that EVERY
dashboard template references it — the verdict must be visible wherever the
operator lands, because he never runs a command to see it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")

from brutex.dashboard.app import DashboardState, build_app
from fastapi.testclient import TestClient

_TEMPLATES = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "templates"


def test_widget_script_served_and_honest() -> None:
    """The widget JS serves from /static and carries the honesty contract."""
    with TestClient(build_app(DashboardState())) as client:
        js = client.get("/static/bx-liveproof.js")
        uni = client.get("/universe")
    assert js.status_code == 200
    assert "/api/v1/liveproof" in js.text
    # Honest rendering markers (Golden Rules #1/#10): the not-wired /
    # disabled states render NOTHING (no invented state), green is keyed to
    # the backend "pass" kind only, and the kit's lines render as textContent
    # (never parsed as markup — zero HTML-injection sink).
    assert "present !== true" in js.text
    assert "enabled === false" in js.text
    assert "textContent" in js.text
    assert "innerHTML" not in js.text
    # The widget never re-runs the proof — it only polls the status route.
    assert "fetch(ENDPOINT" in js.text
    assert 'method: "POST"' not in js.text
    # Idempotent guard (only one widget ever built).
    assert "document.getElementById(WIDGET_ID)" in js.text
    assert uni.status_code == 200
    assert "/static/bx-liveproof.js" in uni.text


def test_widget_referenced_on_every_dashboard_template() -> None:
    """EVERY dashboard page loads the live-proof widget (site-wide, like the
    rate monitor — DEC-PULL-RATE-WIDGET-VISIBILITY-001 precedent)."""
    html_files = sorted(_TEMPLATES.glob("*.html"))
    assert html_files, "expected dashboard templates to exist"
    for path in html_files:
        text = path.read_text(encoding="utf-8")
        assert "/static/bx-liveproof.js" in text, path.name


def test_bare_app_reports_not_wired() -> None:
    """Without an injected autorun the status route reads present: false —
    the widget then renders nothing (never a fabricated verdict)."""
    with TestClient(build_app(DashboardState())) as client:
        assert client.get("/api/v1/liveproof").json() == {"present": False}
