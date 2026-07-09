"""Round-3 refuter EXECUTION probes, batch 2 (DEC-MOVERS-DB-PROVENANCE-VERIFY-001).

Break-it scenarios the committed suite does NOT execute, each constructed so a
chip LIE (false green on mismatch / false red on match / a superseded verdict
painting over a newer view) fails the test:

* the CATEGORY-PILL switch mid-flight — the delegated pill handler re-renders
  from ``lastPayload`` WITHOUT ``cancelVerify()`` (unlike the asset-tab
  handler), so it leans entirely on the ``scheduleVerify()`` generation bump +
  abort; a POISONED (mismatching) response for the superseded pass is
  released AFTER the new pass lands green — it must never repaint the chip;
* the F&O EXPLICIT time pick — the anchored ``/db`` cut must ride the F&O
  payload's own ``time`` echo (``anchor = j.time``); a broken echo would cut
  at ``rows[0]`` (the day's LAST bar) and false-red a matching 09:15 view;
* the green chip's honest %CHNG-skip disclosure when /db carries NO baseline
  for the day (no prior-session 1-minute partition → ``change_pct`` null on
  the wire) — the pure-fn arm is golden-pinned; this pins the flow;
* MULTIPLE mismatching rows — the red names the FIRST offender and counts
  the rest ("(+1 more)"), never silently dropping the extra offenders.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.browser._fixtures import build_movers_app
from tests.browser._guards import browser_suite_enabled
from tests.browser.conftest import NewPageFactory, freeze_page_clock

if not browser_suite_enabled():
    pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect

from tests.browser.test_refuter_r3_probes import (
    _build_app_with,
    _pick_time,
    _seed_future,
    _switch_interval,
)

pytestmark = pytest.mark.browser

_CHIP = "#verifyChip"
_DB_CANDLES_GLOB = "**/api/v1/db/candles**"
_MOVERS_GLOB = "**/api/v1/movers**"

#: A poisoned /db body whose close can NEVER match any fixture LTP — if a
#: superseded pass's verdict ever paints, it paints an unmistakable red.
_POISON_BODY = json.dumps(
    {
        "columns": ["timestamp", "close", "change_pct"],
        "rows": [["2024-06-13 15:29:00", 1.0, None]],
    }
)


def test_pill_switch_mid_flight_discards_the_superseded_pass_verdict(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """A poisoned superseded-pass response must never repaint the chip.

    Sequence: the first verify pass's probes are HELD in flight; the operator
    clicks the "Top Losers" pill (applyCategory → renderRows → scheduleVerify:
    generation bump + abort — the pill path has NO cancelVerify belt); the new
    pass probes the REAL backend and lands green; only THEN are the held old
    probes fulfilled with a poisoned mismatching body. A red after that is a
    confirmed race lie — a superseded verdict painting over a newer view.
    """
    fixture = build_movers_app(tmp_path)
    # the superseded pass's aborted probes log net::ERR_FAILED by design
    session = new_page(fixture, expect_console_clean=False)
    freeze_page_clock(session.page)
    held: list[Any] = []

    def _hold_first_pass(route: Any) -> None:
        # The FIRST pass's probe is held (the gainers view carries ONE
        # /db-readable row — NIFTY; the XSS row is a disclosed skip); every
        # later probe (the post-pill pass) flows to the real backend.
        if len(held) < 1:
            held.append(route)
        else:
            route.fallback()

    session.page.route(_DB_CANDLES_GLOB, _hold_first_pass)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)  # debounce → first-pass probe pending
    expect(chip).to_have_text(re.compile(r"^verify: checking top "))
    assert len(held) == 1

    # Mid-flight: switch the metric pill (client-side re-render, no refetch).
    session.page.locator('.pill[data-cat="price_losers"]').click()
    # The re-render superseded the pass synchronously (generation bump).
    expect(chip).to_have_text(re.compile(r"^verify: checking top "))
    session.page.clock.fast_forward(200)  # new pass probes the REAL backend
    expect(chip).to_have_class("pos")
    expect(chip).to_have_text(re.compile(r"^✓ matches /db \(1 rows"))
    # The losers view leads with BANKNIFTY — the repaint really happened.
    expect(session.page.locator("#rows td.name").first).to_contain_text("BANKNIFTY")

    # NOW release the superseded pass's probes with a poisoned mismatch body
    # (the client already aborted these requests — fulfil is best-effort).
    for route in held:
        with contextlib.suppress(Exception):
            route.fulfill(status=200, content_type="application/json", body=_POISON_BODY)
    session.page.clock.fast_forward(500)
    # The chip must still wear the NEW pass's green — never the poisoned red.
    expect(chip).to_have_class("pos")
    expect(chip).to_have_text(re.compile(r"^✓ matches /db"))
    assert session.page_errors == []


def test_fno_explicit_time_pick_anchors_the_db_cut(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """Futures tab + explicit 09:15 pick: the /db cut rides j.time → green.

    The lake holds 09:15 (22010) and 09:17 (22060). The movers futures LTP at
    the 09:15 pick is 22010; an anchor chain broken on the F&O side (a missing
    ``time`` echo → un-anchored ``rows[0]`` cut) would compare 22060 instead —
    a false LOUD red on a matching view. Green proves the echo end-to-end.
    """
    fixture = _build_app_with(tmp_path, seed=_seed_future)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator('.tab[data-asset="futures"]').click()
    session.page.wait_for_selector("#rows td.name")
    _switch_interval(session.page, "1minute")  # daily disables the #time pick
    _pick_time(session.page, "09:15")
    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")
    expect(chip).to_have_text(re.compile(r"F&O LTP only"))
    assert session.page_errors == []


def test_green_discloses_pct_skips_when_db_has_no_baseline(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """No prior-session 1m partition → /db change_pct is null → counted skip.

    The default fixture stores NO prior-day 1-minute bars, so /db serves
    ``change_pct: null`` for the probed NIFTY row: the chip must stay green
    on the LTP check AND disclose the skipped %CHNG cell AND the unreadable
    XSS row — never a red for a baseline only one side holds, never an
    undisclosed shrink of the checked surface (Golden Rule #10).
    """
    fixture = build_movers_app(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")
    expect(chip).to_have_text(re.compile(r"· 1 skipped"))
    expect(chip).to_have_text(re.compile(r"· 1 not /db-readable"))
    assert session.page_errors == []


def test_multi_mismatch_names_first_and_counts_more(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """Both probed rows diverge → the red names one offender + "(+1 more)".

    The gainers view carries only ONE /db-readable row, so the pass switches
    to the Top Volume pill (NIFTY + BANKNIFTY both visible + readable) before
    asserting the multi-offender count.
    """
    fixture = build_movers_app(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.page.route(
        _DB_CANDLES_GLOB,
        lambda route: route.fulfill(status=200, content_type="application/json", body=_POISON_BODY),
    )
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    session.page.locator('.pill[data-cat="volume"]').click()
    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("neg")
    expect(chip).to_have_text(re.compile(r"^✗ /db MISMATCH — \S+ LTP shown [\d,.]+ · /db 1\.00"))
    expect(chip).to_have_text(re.compile(r"\(\+1 more\)"))
    assert session.page_errors == []
