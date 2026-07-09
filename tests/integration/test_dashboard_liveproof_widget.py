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
    # (through the bounded fetchJson helper since DEC-WINDOWS-COUNTS-ONLY-001 M15).
    assert "fetchJson(ENDPOINT" in js.text
    assert 'method: "POST"' not in js.text
    # m1: the pill dot renders the WORST of preflight+probes — a red probes
    # verdict can never hide behind a green preflight dot.
    assert "worstClass(pre.cls" in js.text
    assert "KIND_RANK" in js.text
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


def test_poll_fetch_is_bounded_single_flight_and_ordered() -> None:
    """DEC-WINDOWS-COUNTS-ONLY-001 M15: the poll fetch discipline pins.

    HONESTY ENVELOPE: this repo has NO JavaScript execution harness — these are
    SOURCE-STRING pins (the ``test_dashboard_js_perf_frontend.py`` idiom), not a
    browser-executed proof. They pin the mechanisms the fix relies on:

    (a) the bounded ``fetchJson`` helper mirroring ``bx-rate-monitor.js`` — an
        ``AbortController`` deadline (``FETCH_TIMEOUT_MS``) armed through the
        BODY read (cleared only after ``r.json()`` settles, re-cleared on the
        catch path), so a hung server / stalled body aborts instead of
        accumulating pending requests onto the browser's ~6-per-origin pool;
    (b) the one-in-flight guard (``pollInFlight``) — poll() never stacks a
        second request behind a live one;
    (c) the monotonic ``pollSeq`` token — a stale straggler response is
        discarded, never painted over a fresher one;
    (d) the widget contract intact: best-effort catch (leave the last render
        in place), the hidden-tab guard + visibilitychange catch-up preserved
        (the ``test_dashboard_widget_visibility_pause.py`` suite pins their
        exact shape), and the poll cadence unchanged.

    FAILS on the pre-fix widget (bare ``fetch(ENDPOINT`` with none of these).
    """
    js = (
        Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static" / "bx-liveproof.js"
    ).read_text(encoding="utf-8")
    # (a) the bounded helper — abort deadline armed through the body read.
    assert "function fetchJson(url)" in js
    assert "var FETCH_TIMEOUT_MS = 10000;" in js
    assert 'typeof AbortController !== "undefined" ? new AbortController() : null' in js
    assert "setTimeout(function () { ctrl.abort(); }, FETCH_TIMEOUT_MS)" in js
    assert "opts.signal = ctrl.signal;" in js
    # Cleared only AFTER the body settles — and re-cleared on the catch path.
    assert "return r.ok ? r.json() : null;   // killer still armed" in js
    assert ".catch(function (err) { if (killer) { clearTimeout(killer); } throw err; })" in js
    # (b) one in flight, released on BOTH settle paths.
    assert "var pollInFlight = false;" in js
    assert "if (pollInFlight) return;" in js
    # 3 = the `var` declaration + the .then release + the .catch release.
    assert js.count("pollInFlight = false;") == 3, "both settle paths must release the guard"
    # (c) monotonic ordering token — a stale response never repaints.
    assert "var pollSeq = 0;" in js
    assert "var seq = ++pollSeq;" in js
    assert "if (seq !== pollSeq) return;" in js
    # (d) contract intact: bare unbounded fetch gone, best-effort catch kept.
    assert "fetch(ENDPOINT" not in js  # the old bare fetch is GONE
    assert "fetchJson(ENDPOINT" in js
    assert "leave the last render in place" in js
