"""Round-2 refuter probes for DEC-MOVERS-HEADER-CHIPS-001.

S1  stale-response supersede guard — a STALLED ``expiry=Next`` response that
    lands AFTER a fast ``expiry=Far`` paint is DROPPED by load()'s loadGen
    re-entrancy guard (the gbPollGen precedent). EXECUTED pre-fix repro on
    the round-1 head: without the guard the stale payload repainted the
    table with next-tier rows while the selection said Far, and the Expiry
    chip cross-labeled the July expiry with the WRONG tier word
    ("25-Jul-2024 (far tier)") — renderChips() reads the live select for the
    tier word while the rows came from the superseded payload. ADOPTED as
    the regression pin of the fixed behaviour: the Far paint survives the
    late Next response un-clobbered.
S2  gated flip with the missing-OI reveal OPEN — #chipDetail must hide and
    clear alongside #chips (the round-1 P2 class, reveal edition).
S3  hostile payload inertness — oi_missing_symbols / display_underlying
    carrying live markup render as inert text via the coverage reveal
    (textContent-only law), console clean, no window.__xss.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.browser._guards import browser_suite_enabled
from tests.browser.conftest import NewPageFactory, freeze_page_clock
from tests.browser.test_movers_header_chips import (
    _chain_fixture,
    _obar,
    _open_options_tab,
)

if not browser_suite_enabled():
    pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect

pytestmark = pytest.mark.browser

_MOVERS_GLOB = "**/api/v1/movers**"

#: A SECOND (next-tier) NIFTY chain — 25-Jul-2024 is the following monthly.
_CE_JUL = "NSE-NIFTY-25Jul24-24000-CE"
_PE_JUL = "NSE-NIFTY-25Jul24-24000-PE"


def _two_tier_fixture(tmp_path: Path) -> Any:
    """Near chain (27Jun24) + next chain (25Jul24), both with full OI."""
    fixture = _chain_fixture(tmp_path)
    from brutex.data.parquet_writer import ParquetWriter

    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol=_CE_JUL,
        interval="1minute",
        bars=[_obar(hour=9, minute=15, close="210.00", volume=30, oi=500)],
    )
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol=_PE_JUL,
        interval="1minute",
        bars=[_obar(hour=9, minute=15, close="95.00", volume=20, oi=400)],
    )
    return fixture


# ── S1: stalled Next response lands after a fast Far paint ─────────────────


def test_s1_stale_tier_response_repaints_cross_labeled(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    fixture = _two_tier_fixture(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _open_options_tab(session)
    page = session.page

    # Pause the Next-tier response server-side of the page: stash the route,
    # fulfil it only AFTER the Far click has painted.
    pending: list[Any] = []

    def handler(route: Any) -> None:
        if "expiry=Next" in route.request.url:
            pending.append(route)
            return
        route.fallback()

    page.route(_MOVERS_GLOB, handler)

    with page.expect_request(lambda r: "expiry=Next" in r.url):
        page.locator('#expiryTabs .segbtn[data-val="Next"]').click()
    # Far is the EMPTY third tier — paints fast.
    with page.expect_response(lambda r: "expiry=Far" in r.url):
        page.locator('#expiryTabs .segbtn[data-val="Far"]').click()
    page.wait_for_function(
        "() => document.querySelector('#chipExpiry b')"
        " && document.querySelector('#chipExpiry b').textContent === 'far tier'"
    )
    assert len(pending) == 1, "the Next request never reached the paused route"

    # Release the stalled Next response and let it COMPLETE — the superseded
    # payload now lands client-side, where the loadGen supersede guard must
    # drop it (pre-fix it repainted here: 25-Jul rows + "25-Jul-2024
    # (far tier)" — the executed CONFIRMED repro this pin adopts).
    with page.expect_response(lambda r: "expiry=Next" in r.url) as released:
        pending[0].continue_()
    released.value.finished()
    # Settle window: the stale resume point is one microtask after the fetch
    # resolves — give the page a real slice so a regression WOULD repaint.
    page.wait_for_timeout(600)

    # The Far paint survives un-clobbered: tier word only (empty third tier),
    # no July expiry, no next-tier rows, selection + tab still Far.
    chip = page.locator("#chipExpiry b").text_content()
    assert chip == "far tier", f"stale Next payload repainted the Expiry chip: {chip!r}"
    body = page.locator("#rows").text_content() or ""
    assert "25-Jul-2024" not in body, f"stale next-tier rows repainted the table: {body[:220]!r}"
    assert "no movers in this snapshot" in body
    assert page.evaluate("() => document.getElementById('expiry').value") == "Far"
    far_on = page.locator('#expiryTabs .segbtn[data-val="Far"]').get_attribute("class") or ""
    assert "on" in far_on.split()
    assert session.page_errors == []


# ── S2: gated flip while the missing-OI reveal is OPEN ─────────────────────


def test_s2_gated_flip_hides_open_missing_oi_reveal(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    from brutex.dashboard.prebuild_gate import PrebuildGate

    fixture = _chain_fixture(tmp_path, ce_null_oi=True)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _open_options_tab(session)
    page = session.page

    cov = page.locator("#chipCoverage")
    cov.click()
    expect(page.locator("#chipDetail")).to_be_visible()

    gate: PrebuildGate = fixture.ground["gate"]
    gate.begin()
    gate.set_phase("prebuild")
    with page.expect_response(_MOVERS_GLOB):
        page.locator("#refresh").click()
    page.wait_for_selector("#rows td.empty")
    disp = page.evaluate("() => getComputedStyle(document.getElementById('chipDetail')).display")
    assert disp == "none", f"stale missing-OI reveal still displayed while GATED (display={disp!r})"
    text = page.evaluate("() => document.getElementById('chipDetail').textContent")
    assert text == "", f"stale reveal text survives the gated flip: {text!r}"
    assert session.page_errors == []


# ── S3: hostile payload strings render inert through the reveal ────────────


def test_s3_hostile_missing_oi_symbols_render_inert(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    fixture = _chain_fixture(tmp_path, ce_null_oi=True)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    page = session.page

    hostile_sym = '<img src=x onerror="window.__xss=1">'
    hostile_name = "<script>window.__xss=2</script>"

    def handler(route: Any) -> None:
        resp = route.fetch()
        body = json.loads(resp.text())
        if isinstance(body.get("pcr"), list) and body["pcr"]:
            row = body["pcr"][0]
            row["oi_missing_symbols"] = [hostile_sym]
            row["display_underlying"] = hostile_name
            row["expiry"] = "2026-02-31"  # impossible calendar date
        route.fulfill(response=resp, body=json.dumps(body))

    page.route(_MOVERS_GLOB, handler)
    _open_options_tab(session)

    cov = page.locator("#chipCoverage")
    expect(cov).to_have_class(re.compile(r"\bamber\b"))
    cov.click()
    detail = page.locator("#chipDetail")
    expect(detail).to_be_visible()
    # Inert text — the literal markup is VISIBLE as text, never parsed.
    expect(detail).to_contain_text(hostile_sym)
    expect(detail).to_contain_text(hostile_name)
    assert page.evaluate("() => window.__xss") is None
    assert page.evaluate("() => document.querySelectorAll('#chipDetail img').length") == 0
    # The impossible ISO renders through the words formatter (day<=31 gate).
    chip = page.locator("#chipExpiry b").text_content()
    assert chip is not None
    assert session.page_errors == []
    print("chipExpiry on impossible ISO:", chip)
