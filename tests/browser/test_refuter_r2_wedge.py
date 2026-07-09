"""Round-2 REFUTER PROOF (DEC-MOVERS-DB-PROVENANCE-VERIFY-001) — the
skip-render verify wedge.

These tests PIN THE CURRENT (defective) behavior in real Chromium: the
load() skip-render-when-unchanged fast path (movers.html — byte-identical
body for the byte-identical coordinate) refreshes the provenance line but
deliberately does NOT re-schedule the verify pass, so an explicit ↻ Refresh
can NEVER re-prove the chip while the snapshot is unchanged (e.g. any time
after market close). Two lies follow:

* a transient probe failure wedges the chip on "verify unavailable" for the
  rest of the session — every ↻ Refresh takes the skip path and re-probes
  nothing, even though /db answers fine again;
* a green chip goes STALE-GREEN: /db content that changed underneath
  (partition heal/quarantine/manual edit — anything that does not touch the
  window ledger, so the movers write-generation and therefore the payload
  bytes stay identical) is still reported "✓ matches /db" on every refresh.

The anti-vacuity legs prove the same pass DOES recover/flip on a real
re-render (a sort click), so the wedge is precisely the skip path.

If a fix lands (skip path re-schedules verify), BOTH `wedge` asserts here
FAIL — flip them into the fixed-behavior pins then.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.browser._fixtures import build_movers_app
from tests.browser._guards import browser_suite_enabled
from tests.browser.conftest import NewPageFactory, freeze_page_clock

if not browser_suite_enabled():
    pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect

pytestmark = pytest.mark.browser

_MOVERS_GLOB = "**/api/v1/movers**"
_DB_CANDLES_GLOB = "**/api/v1/db/candles**"

#: Deterministic load()-completed barrier: plant a sentinel note, click ↻,
#: wait until the note is neither the sentinel nor "Loading…" — the skip path
#: restores the real note as its LAST DOM write, and scheduleVerify (if it had
#: been called) paints the chip SYNCHRONOUSLY before load() returns.
_PLANT_SENTINEL = "() => { document.getElementById('noteText').textContent = 'BX-SENTINEL'; }"
_NOTE_SETTLED = (
    "() => { const t = document.getElementById('noteText').textContent;"
    " return t !== 'BX-SENTINEL' && t !== 'Loading…'; }"
)
_CHIP_TEXT = "() => document.getElementById('verifyChip').textContent"


def test_refuter_unavailable_wedge_survives_refresh(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """A transient probe failure wedges the chip across every ↻ Refresh."""
    fixture = build_movers_app(tmp_path)
    # aborted probes log net::ERR_FAILED console errors by design here
    session = new_page(fixture, expect_console_clean=False)
    freeze_page_clock(session.page)
    session.page.route(_DB_CANDLES_GLOB, lambda route: route.abort())
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    chip = session.page.locator("#verifyChip")
    session.page.clock.fast_forward(200)  # fire the frozen 150 ms debounce
    expect(chip).to_have_text("verify unavailable (network)")

    # /db heals — the next verify pass WOULD succeed.
    session.page.unroute(_DB_CANDLES_GLOB)

    # ↻ Refresh: byte-identical payload → the skip-render fast path.
    session.page.evaluate(_PLANT_SENTINEL)
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator("#refresh").click()
    session.page.wait_for_function(_NOTE_SETTLED)
    # WEDGE: verify was NOT re-scheduled (scheduleVerify would have painted
    # "verify: checking top 2 vs /db…" synchronously) …
    assert session.page.evaluate(_CHIP_TEXT) == "verify unavailable (network)"
    # … and no debounce timer is pending either.
    session.page.clock.fast_forward(1_000)
    assert session.page.evaluate(_CHIP_TEXT) == "verify unavailable (network)"
    assert session.request_count("/api/v1/db/candles") == 1  # only the aborted one

    # Anti-vacuity: a REAL re-render (sort click) re-verifies and goes green.
    session.page.locator("#head th").first.click()
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")
    assert session.page_errors == []


def test_refuter_stale_green_masks_db_divergence_across_refresh(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """A green chip stays green across ↻ Refresh though /db now disagrees."""
    fixture = build_movers_app(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    chip = session.page.locator("#verifyChip")
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")  # a genuine green against the real lake

    # /db now answers a DIFFERENT close for the same coordinate (stands in for
    # a heal/quarantine/manual lake edit — none of which touch the window
    # ledger, so the movers payload bytes stay identical).
    body = json.dumps(
        {
            "columns": ["timestamp", "close", "change_pct"],
            "rows": [["2024-06-13 15:29:00", 99999.0, "1.0"]],
        }
    )
    session.page.route(
        _DB_CANDLES_GLOB,
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )

    session.page.evaluate(_PLANT_SENTINEL)
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator("#refresh").click()
    session.page.wait_for_function(_NOTE_SETTLED)
    session.page.clock.fast_forward(1_000)
    # WEDGE (false green): the chip still claims "✓ matches /db".
    txt = session.page.evaluate(_CHIP_TEXT)
    assert txt.startswith("✓ matches /db"), txt

    # Anti-vacuity: a real re-render sees the divergence and goes LOUD red.
    session.page.locator("#head th").first.click()
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("neg")
    expect(chip).to_have_text(re.compile(r"✗ /db MISMATCH — NIFTY LTP"))
    assert session.page_errors == []
