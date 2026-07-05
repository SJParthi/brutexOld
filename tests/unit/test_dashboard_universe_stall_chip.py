"""Regression: the ``/universe`` pull panel surfaces the stall-suspect marker.

Guards **DEC-PULL-STALL-CHIP-001** (MIGRATION.md #1205). The pull-progress
tracker has emitted ``stalled`` / ``stall_detail`` on every
``/api/v1/universe/pull-progress`` payload since
DEC-PIPELINE-STALL-HEARTBEAT-001 (``PullProgress.to_dict`` lines 124-125), but
NO template or static JS consumed them — a stalled-suspect pull looked exactly
like a healthy one on the ``/universe`` screen. The fix adds a hidden amber
``#stallchip`` beside ``#pulltitle`` that the EXISTING ``pollProgress`` loop
toggles from the payload (no new poll loop), populated via ``textContent``
ONLY (CodeQL XSS guard), and hidden again the instant the marker clears or the
run settles (``finishLive``).

Every template/JS assertion here FAILS on the pre-fix ``universe.html`` (no
``#stallchip`` markup, no ``.chip.stall`` CSS, no ``updateStallChip`` hook) and
passes after it. The route test pins the pre-existing wire contract the chip
relies on: both fields are ALREADY in the ``/api/v1/universe/pull-progress``
JSON (the verification report's claim, now mechanised).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store route needs duckdb")

from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.dashboard.app import DashboardState, _render_universe_html, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

# ────────────────────────────────────────────────────────────────────────
# Template: chip markup + CSS gate
# ────────────────────────────────────────────────────────────────────────


def test_stall_chip_markup_sits_beside_pulltitle() -> None:
    """The hidden amber chip lives in the pull panel title row, next to #pulltitle."""
    html = _render_universe_html()
    title_row = re.search(r'<div class="title">.*?</div>', html)
    assert title_row is not None, "the #pullpanel title row is missing"
    row = title_row.group(0)
    assert '<b id="pulltitle">' in row, "#pulltitle must stay in the title row"
    assert 'id="stallchip"' in row, "the stall chip must sit in the SAME title row as #pulltitle"
    # The chip rides the existing .chip base class + a .stall variant, and it
    # renders EMPTY server-side (content arrives only via JS textContent).
    assert re.search(r'<span class="chip stall" id="stallchip"></span>', row), (
        "the chip must be an empty span with class 'chip stall' (content via textContent only)"
    )
    # Beside #pulltitle: the chip follows the title bold element directly.
    assert re.search(r'</b><span class="chip stall" id="stallchip">', row), (
        "the chip must sit immediately beside #pulltitle"
    )


def test_stall_chip_css_hidden_by_default_and_revealed_on() -> None:
    """.chip.stall is display:none by default; only .on reveals it (the .badge.on pattern)."""
    html = _render_universe_html()
    base_rule = re.search(r"\.chip\.stall \{[^}]*\}", html)
    assert base_rule is not None, "the .chip.stall CSS rule is missing"
    assert "display:none" in base_rule.group(0), (
        "the stall chip must be hidden by default — it may appear ONLY while stalled=true"
    )
    # Amber palette — mirrors .chip.ref (var(--yellow) on the amber wash).
    assert "var(--yellow)" in base_rule.group(0), "the stall chip must use the amber palette"
    assert "rgba(255,183,77,.14)" in base_rule.group(0), (
        "the stall chip must reuse the .chip.ref amber background wash"
    )
    assert re.search(r"\.chip\.stall\.on \{[^}]*display:inline-block[^}]*\}", html), (
        "the .on modifier must reveal the chip (the established .badge.on toggle convention)"
    )


# ────────────────────────────────────────────────────────────────────────
# JS: toggled from the EXISTING poll loop, textContent only
# ────────────────────────────────────────────────────────────────────────


def _update_stall_chip_body(html: str) -> str:
    """Extract the ``updateStallChip`` function body from the rendered page."""
    fn = re.search(r"function updateStallChip\(s\) \{(.*?)\n\}", html, re.DOTALL)
    assert fn is not None, "the updateStallChip JS helper is missing"
    return fn.group(1)


def test_js_helper_reads_payload_fields_via_textcontent_only() -> None:
    """updateStallChip toggles .on from s.stalled and writes s.stall_detail via textContent."""
    html = _render_universe_html()
    body = _update_stall_chip_body(html)
    assert "getElementById('stallchip')" in body, "the helper must target #stallchip"
    assert "s.stalled" in body, "the helper must read the payload's stalled flag"
    assert "s.stall_detail" in body, "the helper must read the payload's stall_detail text"
    assert "classList.toggle('on'" in body, "show/hide must ride the .on class toggle"
    assert "textContent" in body, "the detail must be written via textContent"
    assert "innerHTML" not in body, (
        "CodeQL XSS guard: the stall detail must NEVER go through innerHTML"
    )


def test_js_running_branch_updates_and_finish_clears() -> None:
    """pollProgress's running branch feeds the chip; finishLive hides it on settle."""
    html = _render_universe_html()
    poll = re.search(r"async function pollProgress\(label\) \{.*?\n\}", html, re.DOTALL)
    assert poll is not None, "pollProgress is missing"
    running = re.search(r"if \(s\.state === 'running'\) \{(.*?)return;", poll.group(0), re.DOTALL)
    assert running is not None, "the running branch of pollProgress is missing"
    assert "updateStallChip(s);" in running.group(1), (
        "the running branch must toggle the chip from the live snapshot"
    )
    finish = re.search(r"function finishLive\(\) \{(.*?)\n\}", html, re.DOTALL)
    assert finish is not None, "finishLive is missing"
    assert "updateStallChip(null);" in finish.group(1), (
        "finishLive must hide the chip — a settled run is never stalled-suspect"
    )


def test_no_new_poll_loop_added() -> None:
    """The chip rides the ONE existing pull-progress poll — no second fetch loop."""
    html = _render_universe_html()
    assert html.count("fetch('/api/v1/universe/pull-progress')") == 1, (
        "exactly ONE pull-progress fetch site may exist (the chip must reuse pollProgress)"
    )


# ────────────────────────────────────────────────────────────────────────
# Route: the payload the chip consumes ALREADY carries both fields (pinned)
# ────────────────────────────────────────────────────────────────────────


def _nifty() -> Instrument:
    """Build the admitted NIFTY index instrument fixture."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY",
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    """A TestClient over a real wired store; credentials redirected to a tmp file."""
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    with InstrumentStore.open(tmp_path / "instruments.duckdb") as store:
        store.upsert_many(
            [_nifty()],
            snapshot_date=date(2024, 1, 2),
            run_id="r1",
            csv_source_sha256="0" * 64,
        )
        with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
            yield c


def test_pull_progress_payload_carries_stall_fields(client: TestClient) -> None:
    """GET /api/v1/universe/pull-progress emits stalled + stall_detail (idle defaults).

    Pins the wire contract the chip consumes: ``PullProgress.to_dict`` emits
    both fields on EVERY snapshot (DEC-PIPELINE-STALL-HEARTBEAT-001), so the
    frontend can rely on their presence without a route change. The stalled
    projection itself (mark_stalled → snapshot → clear) is pinned in
    ``tests/unit/test_pull_progress.py`` and ``test_dashboard_pull_stall.py``.
    """
    resp = client.get("/api/v1/universe/pull-progress")
    assert resp.status_code == 200
    snap = resp.json()
    assert snap["stalled"] is False
    assert snap["stall_detail"] == ""
