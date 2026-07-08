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
    # The widget never re-runs the proof — it only polls the status route
    # (via the bounded fetchJson helper — DEC-PULL-UI-STALENESS-001 port).
    assert "fetchJson(ENDPOINT)" in js.text
    assert 'method: "POST"' not in js.text
    # m1: the pill dot renders the WORST of preflight+probes — a red probes
    # verdict can never hide behind a green preflight dot.
    assert "worstClass(pre.cls" in js.text
    assert "KIND_RANK" in js.text
    # Idempotent guard (only one widget ever built).
    assert "document.getElementById(WIDGET_ID)" in js.text
    assert uni.status_code == 200
    assert "/static/bx-liveproof.js" in uni.text


def test_widget_serves_staleness_and_backoff() -> None:
    """DEC-PULL-UI-STALENESS-001 (Defect 3b): the SERVED bx-liveproof.js carries
    the client-clock staleness readout + the self-recovering capped backoff poll
    ported from bx-rate-monitor.js, so a silent/unreachable backend can never keep
    a green "pass" pill reading fresh forever. HONEST envelope (Golden Rule #10):
    substring asserts pin PRESENCE of the JS, not its execution — this repo has no
    headless JS runner (the same limit #1529/#1530 accepted).
    """
    with TestClient(build_app(DashboardState())) as client:
        js = client.get("/static/bx-liveproof.js")
    assert js.status_code == 200
    body = js.text
    # Staleness constants + the ~1 s age ticker.
    assert "STALE_WARN_MS" in body
    assert "STALE_RED_MS" in body
    assert "AGE_TICK_MS" in body
    # The client-clock stamp + the "updated Xs ago" / "STALE" age readout.
    assert "lastPollOkMs" in body
    assert "lastPollOkWallMs" in body  # suspend-guard wall-clock twin
    assert "updated " in body
    assert "STALE — " in body
    assert "function renderAge" in body
    # Self-recovering capped exponential backoff that NEVER gives up + the bounded
    # fetch whose abort signal is actually WIRED (a non-ok/rejection/malformed body
    # is a counted failure feeding the staleness clock).
    assert "backoffDelayMs" in body
    assert "schedulePoll(backoffDelayMs(pollFailures))" in body
    assert "opts.signal = ctrl.signal" in body
    assert "pollFailures" in body
    # The hidden-tab guard is preserved AND textContent-only rendering is kept
    # (CodeQL XSS gate — no markup sink introduced).
    assert "document.hidden" in body
    assert "visibilitychange" in body
    assert "innerHTML" not in body


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


def test_pill_headline_is_flow_scoped_worst_of_never_preflight_only() -> None:
    """DEC-DOCTOR-KIT-HONESTY-001 fix 3: the pill headline is ONE flow-scoped
    worst-of line ("live-proof — preflight: PASS · probes: FAIL — depth[R6]")
    so the two flows sharing one pill can never read as a contradictory
    PASS-then-FAIL toast sequence. FAILS before the fix (the headline was the
    preflight summary alone — ``text.textContent = pre.text``)."""
    js = (
        Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static" / "bx-liveproof.js"
    ).read_text(encoding="utf-8")
    # The unified headline exists and is what the pill text renders.
    assert "function unifiedHeadline(data)" in js
    assert "text.textContent = unifiedHeadline(data)" in js
    assert '"live-proof — preflight: "' in js
    assert '" · probes: "' in js
    # Flow-scoped short labels + failing-stage pointers ("depth[R6]" style).
    assert "KIND_LABEL" in js
    assert "function failingStageLabels(verdict)" in js
    # The OLD contradictory preflight-only headline is gone.
    assert "text.textContent = pre.text" not in js
    # The pill DOT worst-of logic is untouched (not re-implemented).
    assert "worstClass(pre.cls" in js
    # The #1076 never-occlude auto-collapse is preserved alongside this fix.
    assert "AUTO_MIN_MS" in js
    assert "bxlp-min" in js
    # textContent-only rendering stays absolute (CodeQL XSS gate).
    assert "innerHTML" not in js
