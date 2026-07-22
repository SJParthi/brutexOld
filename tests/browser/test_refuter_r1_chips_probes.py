"""Round-1 refuter probes for DEC-MOVERS-HEADER-CHIPS-001 — the fix pins.

Hostile renders against the shipped header-chips feature. P1 and P2 were
CONFIRMED failing on the original head (the author-origin ``.seg`` /
``.chiprow`` display rules beat the UA stylesheet's non-important
``[hidden]{display:none}`` in the cascade, so ``el.hidden = true`` was
INERT) and now pin the ``#chips[hidden]/#expiryTabs[hidden]/
#trackTabs[hidden]`` guard rule; P3–P5 executed green on the original head
and stay as regression pins.

P1  spot boot        — the expiry/track SEG TABS must be hidden on Spot
                       (was: visible AND clickable on the boot tab).
P2  gated mid-flight — a payload flip to GATED must hide the stale chips row
                       (was: stale PCR/coverage chips painted beside the
                       gated shell).
P3  all-missing OI   — every leg null-OI: chips still honest, reveal names
                       both legs, PCR OI chip reads em-dash (0/0 ratio).
P4  huge leg list    — >50 null-OI legs: server cap + "+K more" tail.
P5  rapid tier tabs  — Next then Far mid-verify: one request each, last
                       click wins, the hidden <select> mirrors the tab.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.browser._fixtures import build_movers_app
from tests.browser._guards import browser_suite_enabled
from tests.browser.conftest import NewPageFactory, freeze_page_clock
from tests.browser.test_movers_header_chips import (
    _CE,
    _PE,
    _chain_fixture,
    _obar,
    _open_options_tab,
)

if not browser_suite_enabled():
    pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect

pytestmark = pytest.mark.browser

_MOVERS_GLOB = "**/api/v1/movers**"


# ── P1: seg tabs must be HIDDEN on the Spot tab ────────────────────────────


def test_p1_seg_tabs_hidden_on_spot_boot(new_page: NewPageFactory, tmp_path: Path) -> None:
    fixture = _chain_fixture(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    # Spot is the boot tab: the Near/Next/Far + All/Monthly/Weekly tab groups
    # must NOT render (renderPills sets .hidden = true).
    for box in ("#expiryTabs", "#trackTabs"):
        el = session.page.locator(box)
        disp = session.page.evaluate(
            "sel => getComputedStyle(document.querySelector(sel)).display", box
        )
        has_hidden = session.page.evaluate("sel => document.querySelector(sel).hidden", box)
        assert has_hidden is True, f"{box}: hidden property not set on spot"
        assert disp == "none", (
            f"{box}: hidden attr set but computed display={disp!r} (CSS override)"
        )
        expect(el).to_be_hidden()
    assert session.page_errors == []


# ── P2: chips row must disappear when the payload flips to GATED ──────────


def test_p2_chips_row_hides_on_gated_flip(new_page: NewPageFactory, tmp_path: Path) -> None:
    from brutex.dashboard.prebuild_gate import PrebuildGate

    fixture = _chain_fixture(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _open_options_tab(session)
    expect(session.page.locator("#chips")).to_be_visible()

    gate: PrebuildGate = fixture.ground["gate"]
    gate.begin()
    gate.set_phase("prebuild")
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator("#refresh").click()
    session.page.wait_for_selector("#rows td.empty")
    disp = session.page.evaluate("() => getComputedStyle(document.getElementById('chips')).display")
    assert disp == "none", f"stale chips row still displayed while GATED (display={disp!r})"
    expect(session.page.locator("#chips")).to_be_hidden()
    assert session.page_errors == []


# ── P3: EVERY leg missing OI — honest chips + both legs named ─────────────


def test_p3_all_legs_missing_oi(new_page: NewPageFactory, tmp_path: Path) -> None:
    from brutex.data.parquet_writer import ParquetWriter

    fixture = build_movers_app(tmp_path)
    writer = ParquetWriter(lake_root=tmp_path)
    for sym, px in ((_CE, "150.00"), (_PE, "80.00")):
        writer.write(
            exchange="NSE",
            segment="FNO",
            symbol=sym,
            interval="1minute",
            bars=[
                _obar(hour=9, minute=15, close=px, volume=40, oi=None),
                _obar(hour=9, minute=17, close=px, volume=60, oi=None),
            ],
        )
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    # All-null OI may honestly empty the default highest_oi ranking — accept
    # either a ranked row or the honest empty cell.
    page = session.page
    with page.expect_response(_MOVERS_GLOB):
        page.locator('.tab[data-asset="options"]').click()
    page.wait_for_selector("#rows td.name, #rows td.empty")

    cov = session.page.locator("#chipCoverage")
    expect(cov.locator("b")).to_have_text("2/2 contracts · 2 missing OI")
    assert "amber" in (cov.get_attribute("class") or "")
    # OI ratio has a 0 denominator → honest em-dash, never Infinity/NaN.
    expect(session.page.locator("#chipPcrOi b")).to_have_text("—")
    cov.click()
    detail = session.page.locator("#chipDetail")
    expect(detail).to_be_visible()
    expect(detail).to_contain_text(_CE)
    expect(detail).to_contain_text(_PE)
    assert session.page_errors == []


# ── P4: >50 missing-OI legs — server cap 50 + client "+K more" tail ────────


def test_p4_huge_missing_oi_list_capped(new_page: NewPageFactory, tmp_path: Path) -> None:
    from brutex.data.parquet_writer import ParquetWriter

    fixture = build_movers_app(tmp_path)
    writer = ParquetWriter(lake_root=tmp_path)
    # 56 null-OI CE legs + 1 good PE leg on one NIFTY chain.
    for k in range(56):
        strike = 20000 + 50 * k
        writer.write(
            exchange="NSE",
            segment="FNO",
            symbol=f"NSE-NIFTY-27Jun24-{strike}-CE",
            interval="1minute",
            bars=[_obar(hour=9, minute=15, close="10.00", volume=5, oi=None)],
        )
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol=_PE,
        interval="1minute",
        bars=[_obar(hour=9, minute=15, close="80.00", volume=46, oi=700)],
    )
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _open_options_tab(session)

    cov = session.page.locator("#chipCoverage")
    expect(cov.locator("b")).to_have_text("57/57 contracts · 56 missing OI")
    cov.click()
    detail = session.page.locator("#chipDetail")
    expect(detail).to_be_visible()
    expect(detail).to_contain_text("(+6 more not listed)")
    assert session.page_errors == []


# ── P5: rapid tier tab switches mid-verify — last click wins, console clean ─


def test_p5_rapid_tier_switch_mid_verify(new_page: NewPageFactory, tmp_path: Path) -> None:
    fixture = _chain_fixture(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _open_options_tab(session)
    page = session.page

    # Click Next then IMMEDIATELY Far — no waits between (mid-verify race).
    page.locator('#expiryTabs .segbtn[data-val="Next"]').click()
    page.locator('#expiryTabs .segbtn[data-val="Far"]').click()
    # Both requests fire; wait until the far response has painted.
    page.wait_for_function(
        "() => document.querySelector('#chipExpiry b') && "
        "document.querySelector('#chipExpiry b').textContent === 'far tier'"
    )
    assert session.request_count("expiry=Next") == 1
    assert session.request_count("expiry=Far") == 1
    expect(page.locator('#expiryTabs .segbtn[data-val="Far"]')).to_have_class(re.compile(r"\bon\b"))
    # The hidden select mirrors the tab (query semantics preserved).
    assert page.evaluate("() => document.getElementById('expiry').value") == "Far"
    assert session.page_errors == []
