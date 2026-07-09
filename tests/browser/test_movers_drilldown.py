"""Movers row drill-down → /db deep-link truth (DEC-MOVERS-DRILL-DOWN-001).

Real-Chromium proof of the audit's finding #27 fix, against fixture-backed
REAL ``build_app`` servers (the substring pins in
``tests/unit/test_movers_drilldown_template.py`` /
``test_db_deeplink_template.py`` pin PRESENCE only — this module is the
execution truth):

* a /movers row click NAVIGATES to a ``/db?candles=…`` deep link carrying the
  row's (symbol, exchange, segment) coordinate, the selected interval and the
  RESOLVED payload day (the blank "latest day" input still links to the day
  shown) — and, because the SAME app serves both routes over the same lake,
  the landed /db resolves the spot cascade end-to-end (symbol selected, the
  one-day range applied, one candle request carrying ``start``/``end``);
* a direct ``page.goto`` spot deep link against the /db paging fixture drives
  the EXISTING cascade to the selected symbol + date (fails-before: at base
  1216789 ``#csym`` stays at the "— symbol —" placeholder and zero candle
  requests fire — the page was parameter-blind);
* an F&O deep link against a one-contract FNO fixture resolves
  underlying → expiry → contract through the REAL ``/api/v1/db/fno/*``
  cascade payloads (server-truth-gated at every level);
* an UNKNOWN spot symbol degrades to the honest "pick a symbol" prompt with
  ZERO candle requests — never a wrong or fabricated selection.

Fails-before framing: at 1216789 the row click navigates nowhere (rows inert
— ``wait_for_url`` times out) and every ``goto`` leg lands on the placeholder
prompt. All four convert to green only with the PR-D diff.

HONEST envelope: the Enter-key path is pinned by substring only (the click
path is the executed twin of the same ``moversDbUrl`` builder).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.browser._fixtures import (
    DB_DAY,
    DB_FNO_EXPIRY_ISO,
    DB_FNO_SYMBOL,
    DB_FNO_UNDERLYING,
    DB_INTERVAL,
    DB_SEGMENT,
    DB_SYMBOL,
    build_db_app,
    build_db_fno_app,
    build_movers_app,
)
from tests.browser._guards import browser_suite_enabled
from tests.browser.conftest import NewPageFactory, PageSession

if not browser_suite_enabled():
    # Direct-file invocation outside the opt-in (collect_ignore governs only
    # directory-shaped collection): a playwright-less box gets an HONEST skip
    # here instead of a collection ImportError; the conftest modifyitems hook
    # skips the collected items. With the suite ENABLED a missing playwright
    # stays a LOUD import error — never a silent green skip in CI.
    pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect

pytestmark = pytest.mark.browser

#: Candle page reads — the trailing ``?`` excludes ``/api/v1/db/candles/datasets``.
CANDLES_URL: Final[str] = "/api/v1/db/candles?"

#: The landed-page grid rows (db.html candleGridHtml → .gridwrap table).
GRID_ROW: Final[str] = "#dbgrid .gridwrap > table tbody tr"


def _candle_requests(session: PageSession) -> list[str]:
    """Every candle-page request URL so far, in ledger order."""
    return [url for url in session.requests if CANDLES_URL in url]


def test_movers_row_click_deep_links_and_lands_on_db(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """A /movers row click navigates to the /db deep link, which resolves end-to-end."""
    fixture = build_movers_app(tmp_path)
    session = new_page(fixture)
    page = session.page
    session.goto("/movers")
    # The seeded gainer row is a link (data-symbol stamped from the payload).
    page.wait_for_selector('#rows tr[data-symbol="NIFTY"]')
    page.click('#rows tr[data-symbol="NIFTY"]')
    page.wait_for_url(re.compile(r"/db\?"))

    # The deep link carries the row coordinate + interval + the RESOLVED
    # payload day (the date input was BLANK — "latest day" — so lastPayload.date
    # is the honest source; design §4).
    q = parse_qs(urlsplit(page.url).query)
    assert q["candles"] == ["spot"]
    assert q["symbol"] == ["NIFTY"]
    assert q["exchange"] == ["NSE"]
    assert q["segment"] == ["INDEX"]
    assert q["interval"] == ["1day"]  # the movers default TF
    assert q["date"] == [fixture.ground["day"]]

    # The SAME app serves /db over the same lake: the deep link drives the
    # spot cascade to the selected symbol, the one-day range, and ONE candle
    # page read carrying start=end=<day>.
    page.wait_for_selector("#csym")
    expect(page.locator("#csym")).to_have_value("NSE|INDEX|NIFTY")
    expect(page.locator("#cstart")).to_have_value(fixture.ground["day"])
    expect(page.locator("#cend")).to_have_value(fixture.ground["day"])
    page.wait_for_selector(GRID_ROW)
    urls = _candle_requests(session)
    assert len(urls) == 1
    assert "symbol=NIFTY" in urls[0]
    assert f"start={fixture.ground['day']}" in urls[0]
    assert f"end={fixture.ground['day']}" in urls[0]


def test_db_spot_deeplink_selects_symbol_interval_and_date(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """A direct spot deep link drives the existing cascade to symbol + date."""
    fixture = build_db_app(tmp_path)
    session = new_page(fixture)
    page = session.page
    session.goto(
        f"/db?candles=spot&exchange=NSE&segment={DB_SEGMENT}&symbol={DB_SYMBOL}"
        f"&interval={DB_INTERVAL}&date={DB_DAY}"
    )
    page.wait_for_selector("#csym")
    expect(page.locator("#csym")).to_have_value(f"NSE|{DB_SEGMENT}|{DB_SYMBOL}")
    expect(page.locator("#cstart")).to_have_value(DB_DAY)
    expect(page.locator("#cend")).to_have_value(DB_DAY)
    page.wait_for_selector(GRID_ROW)
    urls = _candle_requests(session)
    assert len(urls) == 1
    assert f"symbol={DB_SYMBOL}" in urls[0]
    assert f"interval={DB_INTERVAL}" in urls[0]
    assert f"start={DB_DAY}" in urls[0] and f"end={DB_DAY}" in urls[0]


def test_db_fno_deeplink_resolves_underlying_expiry_contract(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """An F&O deep link resolves the 3-level cascade from the contract groww_symbol."""
    fixture = build_db_fno_app(tmp_path)
    session = new_page(fixture)
    page = session.page
    session.goto(f"/db?candles=fno&symbol={DB_FNO_SYMBOL}&interval=1minute&date={DB_DAY}")
    page.wait_for_selector("#ccon")
    expect(page.locator("#cund")).to_have_value(DB_FNO_UNDERLYING)
    expect(page.locator("#cexp")).to_have_value(DB_FNO_EXPIRY_ISO)
    expect(page.locator("#ccon")).to_have_value(DB_FNO_SYMBOL)
    page.wait_for_selector(GRID_ROW)
    urls = _candle_requests(session)
    assert len(urls) == 1
    assert f"symbol={DB_FNO_SYMBOL}" in urls[0]
    assert "segment=FNO" in urls[0]
    assert f"start={DB_DAY}" in urls[0] and f"end={DB_DAY}" in urls[0]


def test_db_deeplink_unknown_symbol_degrades_to_honest_prompt(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """An unmatched symbol is NEVER selected — the honest prompt, zero candle reads."""
    fixture = build_db_app(tmp_path)
    session = new_page(fixture)
    page = session.page
    session.goto(f"/db?candles=spot&exchange=NSE&segment={DB_SEGMENT}&symbol=NOPE&date={DB_DAY}")
    page.wait_for_selector("#csym")
    # The membership gate refused the selection: placeholder stays, no query runs.
    expect(page.locator("#csym")).to_have_value("")
    assert _candle_requests(session) == []
    assert fixture.ground["bar_count"] > 0  # data EXISTS — the refusal is the gate, not emptiness
