"""Movers header-chips DOM-truth tests (DEC-MOVERS-HEADER-CHIPS-001).

Real-Chromium proofs against the fixture-backed REAL ``build_app`` app —
the EXECUTED layer behind ``test_dashboard_movers_header_chips.py``'s
substring pins and ``test_bx_movers_chips_golden.py``'s node goldens:

* the Options tab renders the labeled chips row with the COMPUTED 2dp PCR
  ratios (ratio of sums through the shared ``bxFixed2``) + the N/M coverage
  text off a REAL Parquet options lake;
* a null-OI leg turns the Coverage badge AMBER, and CLICKING it reveals the
  ``#chipDetail`` list NAMING the missing-OI contract (the additive
  ``oi_missing_symbols`` wire key, end-to-end);
* the expiry TABS write the retained hidden select — a "Next" click issues
  exactly one request carrying ``expiry=Next`` (query semantics
  byte-identical) and repaints;
* the Put/Call Ratio chip toggles the per-underlying PCR detail panel open
  and closed.

Console-clean is asserted by the harness on every passing test (the
zero-noise law).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

from tests.browser._fixtures import AppFixture, build_movers_app
from tests.browser._guards import browser_suite_enabled
from tests.browser.conftest import NewPageFactory, freeze_page_clock
from tests.integration.test_dashboard_movers import _DAY as MOVERS_DAY_DATE

if not browser_suite_enabled():
    pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect

pytestmark = pytest.mark.browser

_MOVERS_GLOB = "**/api/v1/movers**"

#: One near-tier NIFTY chain on the movers fixture day (expiry 2024-06-27 ≥
#: 2024-06-13 → the near tier).
_CE = "NSE-NIFTY-27Jun24-24000-CE"
_PE = "NSE-NIFTY-27Jun24-24000-PE"


def _obar(
    *, hour: int, minute: int, close: str, volume: int, oi: int | None, day: date = MOVERS_DAY_DATE
) -> CandleBar:
    """A 1-minute FNO bar at ``HH:MM`` IST on *day* (IST = UTC + 05:30)."""
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=oi,
    )


def _seed_chain(tmp_path: Path, *, ce_null_oi: bool = False) -> None:
    """CE (call) session vol 100, last OI 1000 (or all-null); PE (put) session
    vol 96, last OI 780 — so the header aggregate reads OI PCR 780/1000 = 0.78
    and Vol PCR 96/100 = 0.96 (the operator's own example figures)."""
    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol=_CE,
        interval="1minute",
        bars=[
            _obar(hour=9, minute=15, close="150.00", volume=40, oi=None if ce_null_oi else 900),
            _obar(hour=9, minute=17, close="180.00", volume=60, oi=None if ce_null_oi else 1000),
        ],
    )
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol=_PE,
        interval="1minute",
        bars=[
            _obar(hour=9, minute=15, close="80.00", volume=46, oi=700),
            _obar(hour=9, minute=17, close="75.00", volume=50, oi=780),
        ],
    )


def _chain_fixture(tmp_path: Path, *, ce_null_oi: bool = False) -> AppFixture:
    fixture = build_movers_app(tmp_path)
    _seed_chain(tmp_path, ce_null_oi=ce_null_oi)
    return fixture


def _open_options_tab(session: object) -> None:
    page = session.page  # type: ignore[attr-defined]
    with page.expect_response(_MOVERS_GLOB):
        page.locator('.tab[data-asset="options"]').click()
    page.wait_for_selector("#rows td.name")


# ── (a) the chips row renders the computed ratios + coverage ──────────────


def test_options_chips_render_computed_ratios_and_coverage(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    fixture = _chain_fixture(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _open_options_tab(session)

    expect(session.page.locator("#chips")).to_be_visible()
    # RATIO OF SUMS through the shared half-up bxFixed2: 780/1000 · 96/100.
    expect(session.page.locator("#chipPcrOi b")).to_have_text("0.78")
    expect(session.page.locator("#chipPcrVol b")).to_have_text("0.96")
    # Readable labels — no jargon micro-text.
    expect(session.page.locator("#chipPcrOi .lbl")).to_have_text("Put/Call Ratio (OI)")
    expect(session.page.locator("#chipExpiry b")).to_have_text("27-Jun-2024 (near tier)")
    cov = session.page.locator("#chipCoverage")
    expect(cov.locator("b")).to_have_text("2/2 contracts")
    # Full coverage — NOT amber.
    assert "amber" not in (cov.get_attribute("class") or "")
    # The explainer ships default-COLLAPSED with the options body active.
    assert session.page.locator("#explain").get_attribute("open") is None
    assert session.page_errors == []


# ── (b) a null-OI leg → amber badge → click reveals the named leg ──────────


def test_coverage_badge_amber_and_click_names_the_missing_oi_leg(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    fixture = _chain_fixture(tmp_path, ce_null_oi=True)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _open_options_tab(session)

    cov = session.page.locator("#chipCoverage")
    expect(cov.locator("b")).to_have_text("2/2 contracts · 1 missing OI")
    assert "amber" in (cov.get_attribute("class") or "")
    detail = session.page.locator("#chipDetail")
    expect(detail).to_be_hidden()
    cov.click()
    expect(detail).to_be_visible()
    # The reveal NAMES the null-OI contract (the additive oi_missing_symbols
    # key end-to-end) under its underlying + expiry.
    expect(detail).to_contain_text("missing OI: " + _CE)
    expect(detail).to_contain_text("NIFTY (2024-06-27)")
    # Second click collapses the reveal again.
    cov.click()
    expect(detail).to_be_hidden()
    assert session.page_errors == []


# ── (c) expiry tabs — identical query semantics, one request per click ─────


def test_expiry_tab_click_sends_the_same_query_token_and_repaints(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    fixture = _chain_fixture(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _open_options_tab(session)
    expect(session.page.locator("#expiryTabs")).to_be_visible()
    expect(session.page.locator('#expiryTabs .segbtn[data-val="Near"]')).to_have_class(
        re.compile(r"\bon\b")
    )

    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator('#expiryTabs .segbtn[data-val="Next"]').click()
    # The tab WROTE the retained hidden select — the wire token is the same
    # `expiry=Next` the dropdown used to send (byte-identical semantics).
    assert session.request_count("expiry=Next") == 1
    # Only the near expiry exists in the lake → the next tier repaints the
    # honest empty grid + the fallback chips naming the tier.
    session.page.wait_for_selector("#rows td.empty")
    expect(session.page.locator("#chipExpiry b")).to_have_text("next tier")
    expect(session.page.locator('#expiryTabs .segbtn[data-val="Next"]')).to_have_class(
        re.compile(r"\bon\b")
    )
    assert session.page_errors == []


# ── (d) the PCR chip toggles the per-underlying detail panel ───────────────


def test_pcr_chip_click_toggles_the_detail_panel(new_page: NewPageFactory, tmp_path: Path) -> None:
    fixture = _chain_fixture(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _open_options_tab(session)

    panel = session.page.locator("#pcrPanel")
    expect(panel).to_be_hidden()  # the detail opens via the chips now
    session.page.locator("#chipPcrOi").click()
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("PCR detail")
    session.page.locator("#chipPcrOi").click()
    expect(panel).to_be_hidden()
    assert session.page_errors == []
