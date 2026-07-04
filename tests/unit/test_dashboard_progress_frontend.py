"""DEC-PULL-PROGRESS-HONESTY-001 — frontend + route surfaces of the honest progress.

Whitebox substring asserts on the rendered ``universe.html`` (the file's own
convention — ``test_dashboard_universe_pull_consolidate.py``) + the static
``bx-rate-monitor.js`` widget source, plus a route-level pin that the pull-progress
snapshot's NEW keys are ADDITIVE (every pre-existing flat key still ships).

Fails-before framing: the page rendered a fabricated ``100%`` headline during the
F&O phase (grow-total pin), had NO ETA anywhere, and the rate widget carried no
time-to-done signal at all.
"""

from __future__ import annotations

from pathlib import Path

from brutex.dashboard.app import DashboardState, _render_universe_html, build_app
from fastapi.testclient import TestClient

_RATE_MONITOR_JS = (
    Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static" / "bx-rate-monitor.js"
)


# ── universe.html — ETA + "total unknown" rendering (textContent-only) ──────────


def test_universe_running_headline_gains_the_eta_suffix() -> None:
    html = _render_universe_html()
    # The fmtEta/etaSuffix helpers ship, and the running headline appends the suffix.
    assert "function fmtEta(sec)" in html
    assert "function etaSuffix(s)" in html
    assert "` · ETA ${eta}`" in html
    # "~Xh Ym" / "~Ym" — the two honest formats.
    assert "`~${h}h ${m}m`" in html
    assert "`~${m}m`" in html


def test_universe_eta_hidden_when_null_or_open_ended() -> None:
    html = _render_universe_html()
    # A non-number (null) ETA renders NOTHING …
    assert "if (typeof sec !== 'number' || !isFinite(sec) || sec < 0) return '';" in html
    # … and an open-ended phase never shows a linear ETA at all (the refuter pin).
    assert "if (s.open_ended) return '';" in html


def test_universe_open_ended_renders_total_unknown_not_a_pinned_percent() -> None:
    # Fails-before: the ONLY running headline was the percent one, which read a
    # fabricated 100% through the whole F&O phase. Now the open_ended branch says
    # the honest thing — units done, total unknown — via textContent.
    html = _render_universe_html()
    assert "units · total unknown · ${esc(s.current || '…')}`" in html
    assert "s.open_ended" in html
    # The counter mirrors it ("N / ?"), instead of a fake "N / N".
    assert "' / ?'" in html


def test_universe_of_total_stat_reads_question_mark_when_open_ended() -> None:
    html = _render_universe_html()
    assert "['of total', s.open_ended ? null : s.total, '']" in html
    assert "(v === null) ? '?'" in html


# ── bx-rate-monitor.js — the ETA pill row ───────────────────────────────────────


def test_rate_monitor_gains_a_hidden_by_default_eta_row() -> None:
    js = _RATE_MONITOR_JS.read_text(encoding="utf-8")
    assert 'PROGRESS_ENDPOINT = "/api/v1/universe/pull-progress"' in js
    assert 'etaRow.id = "bxrmEtaRow"' in js
    assert 'etaRow.style.display = "none"' in js  # hidden until a real ETA exists
    assert "function renderEta(progress)" in js
    # Hidden when not running / null ETA; the backend owns the open-ended verdict.
    assert "progress.running === true ? fmtEtaSeconds(progress.eta_seconds)" in js


def test_rate_monitor_eta_renders_via_textcontent_only() -> None:
    js = _RATE_MONITOR_JS.read_text(encoding="utf-8")
    # The widget's one text sink is setText (textContent) — the ETA rides it too.
    assert 'setText("bxrmEta", text)' in js
    assert "innerHTML" not in js  # the whole widget stays markup-parse-free


def test_rate_monitor_eta_format_matches_universe() -> None:
    js = _RATE_MONITOR_JS.read_text(encoding="utf-8")
    assert 'return h > 0 ? "~" + h + "h " + m + "m" : "~" + m + "m";' in js


# ── the route — additive snapshot keys ──────────────────────────────────────────


def test_pull_progress_route_ships_the_new_keys_additively() -> None:
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    body = client.get("/api/v1/universe/pull-progress").json()
    # NEW keys present …
    for key in ("started_at", "units_per_sec", "eta_seconds", "open_ended"):
        assert key in body
    # … honest idle values (no fabricated rate/ETA on an idle job) …
    assert body["eta_seconds"] is None
    assert body["units_per_sec"] == 0.0
    assert body["open_ended"] is False
    # … and every pre-existing flat key STILL ships (the additive contract).
    for key in (
        "state",
        "total",
        "completed",
        "failed",
        "gaps",
        "bars",
        "current",
        "detail",
        "percent",
        "running",
        "updated_at",
        "phase",
        "stalled",
        "stall_detail",
    ):
        assert key in body
