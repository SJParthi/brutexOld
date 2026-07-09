"""Round-3 FIXED-BEHAVIOR PINS (DEC-MOVERS-DB-PROVENANCE-VERIFY-001) — the
skip-render path re-PROVES the verify chip.

Round-2 refuter history: these two tests originally PROVED (in real Chromium)
that the load() skip-render-when-unchanged fast path (movers.html —
byte-identical body for the byte-identical coordinate) refreshed the
provenance line but did NOT re-schedule the verify pass, so an explicit
↻ Refresh could never re-prove the chip while the snapshot bytes were
unchanged (any time between pulls — the server-side MoversSnapshotCache
serves identical JSON per write-generation). Two lies followed:

* FALSE-UNAVAILABLE WEDGE — one transient probe failure left the chip on
  "verify unavailable" for the rest of the session; every ↻ Refresh took the
  skip path and issued ZERO probes even after /db healed;
* STALE GREEN — a green chip kept claiming "✓ matches /db" across every
  refresh while /db content diverged underneath (heal/quarantine/manual edit
  — nothing that touches the window ledger moves the movers payload bytes).

The round-3 fix calls scheduleVerify() on the skip path (bounded: K ≤ 5
debounced probes per EXPLICIT user re-load; the page never polls when idle),
and per the round-2 docstring's own instruction the wedge asserts are FLIPPED
here into the fixed-behavior pins: a ↻ Refresh on the skip path re-paints
"verify: checking top N vs /db…" synchronously, re-probes, and lands the
TRUE verdict — green after /db heals, LOUD red after /db diverges.
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
#: restores the real note BEFORE calling scheduleVerify, and both run in the
#: same synchronous continuation, so once the note settles the chip is
#: guaranteed repainted to the "checking…" state.
_PLANT_SENTINEL = "() => { document.getElementById('noteText').textContent = 'BX-SENTINEL'; }"
_NOTE_SETTLED = (
    "() => { const t = document.getElementById('noteText').textContent;"
    " return t !== 'BX-SENTINEL' && t !== 'Loading…'; }"
)
_CHIP_TEXT = "() => document.getElementById('verifyChip').textContent"


def test_refresh_reproves_after_transient_probe_failure(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """↻ Refresh (skip path) recovers the chip after a transient probe failure."""
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
    assert session.request_count("/api/v1/db/candles") == 1  # the aborted probe

    # /db heals — the next verify pass succeeds.
    session.page.unroute(_DB_CANDLES_GLOB)

    # ↻ Refresh: byte-identical payload → the skip-render fast path, which now
    # re-schedules the verify pass (round-3 fix) instead of replaying the dead
    # "unavailable" verdict for the rest of the session.
    session.page.evaluate(_PLANT_SENTINEL)
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator("#refresh").click()
    session.page.wait_for_function(_NOTE_SETTLED)
    # FIXED: scheduleVerify() painted the "checking…" state synchronously …
    assert session.page.evaluate(_CHIP_TEXT).startswith("verify: checking top ")
    # … and firing the debounce re-probes the healed /db → a TRUE green.
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")
    expect(chip).to_have_text(re.compile(r"^✓ matches /db"))
    assert session.request_count("/api/v1/db/candles") == 2  # aborted + healed
    assert session.page_errors == []


def test_refresh_reproves_and_flips_red_on_db_divergence(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """↻ Refresh (skip path) re-probes and goes LOUD red when /db diverged."""
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
    # FIXED: the skip path re-scheduled the pass (synchronous "checking…" paint) …
    assert session.page.evaluate(_CHIP_TEXT).startswith("verify: checking top ")
    # … and the re-probe sees the divergence: a LOUD red naming the exact cell
    # + both values — never a replayed stale green (Golden Rule #1).
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("neg")
    expect(chip).to_have_text(re.compile(r"✗ /db MISMATCH — NIFTY LTP"))
    assert session.page_errors == []
