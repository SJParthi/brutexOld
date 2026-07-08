"""/db paging-in-place truth test (DEC-DASHBOARD-BROWSER-TRUTH-001, design §5).

ONE flowing test proving, in real Chromium against the fixture-backed REAL
``build_app`` server, that a candle-pager click takes the grid-only partial
path (``runCandleQuery(true)`` → ``patchGridRows`` tbody swap — db.html
:903-:917) and NEVER the full ``renderCandleView`` teardown:

* the pager range reads the EN-DASH ``1–10 / 15`` → ``11–15 / 15`` → back
  (15 seeded bars, Rows select at 10 — set BEFORE the symbol pick);
* expando pins (``el.__pin``) stamped on ``#cstart``, ``#csym`` and the
  ``#dbgrid .gridwrap > table`` node all SURVIVE both clicks (a rebuilt
  control bar / table would shed them — the DOM-identity proof);
* the ``data-cols`` stamp is unchanged and the bx-datepicker enhancement
  (``data-dp-done="1"``) survives (no MutationObserver re-scan / re-wire);
* the request ledger is EXACT: one ``/api/v1/db/candles?`` per page turn
  carrying the right ``offset``/``limit``, and the ``/api/v1/db/spot/symbols``
  cascade fetch happens EXACTLY once for the whole flow;
* first-cell content proves real row movement (page 2 differs, Prev restores
  the page-1 baseline byte-for-byte).

Synchronisation is payload-driven only: ``page.expect_response`` wraps every
page-turning interaction and ``expect(...)`` polls the painted DOM. The test
never waits FOR the transient ``is-loading`` dim (it may already be gone) —
it asserts the class is ABSENT after each settled paint.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import pytest

from tests.browser._fixtures import build_db_app
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

#: The spot symbol→interval cascade fetch (must fire exactly once).
SYMBOLS_URL: Final[str] = "/api/v1/db/spot/symbols"

#: The live grid table — the exact node ``patchGridRows`` requires (db.html :905).
GRID_TABLE: Final[str] = "#dbgrid .gridwrap > table"

#: First data cell of the current page (tbody is the ONLY part a patch swaps).
FIRST_CELL: Final[str] = f"{GRID_TABLE} tbody tr:first-child td:first-child"

#: DOM-identity pin targets: a control-bar teardown (full renderCandleView)
#: would replace #cstart/#csym; a full grid rebuild would replace the table.
PIN_SELECTORS: Final[tuple[str, ...]] = ("#cstart", "#csym", GRID_TABLE)

#: ``is-loading`` must be ABSENT once a paint settled (never waited FOR).
_IS_LOADING: Final[re.Pattern[str]] = re.compile(r"\bis-loading\b")


def _stamp_pins(page: Any) -> None:
    """Stamp ``el.__pin = 1`` expandos on the three DOM-identity targets."""
    for selector in PIN_SELECTORS:
        page.locator(selector).evaluate("el => { el.__pin = 1; }")


def _pin_values(page: Any) -> list[Any]:
    """Read the pins back — ``1`` iff the ORIGINAL node is still in the DOM."""
    return [page.locator(selector).evaluate("el => el.__pin") for selector in PIN_SELECTORS]


def _first_cell_text(page: Any) -> str:
    """The current page's first data-cell text (non-null by construction)."""
    text = page.locator(FIRST_CELL).text_content()
    assert isinstance(text, str) and text != "", "first grid cell must carry text"
    return text


def _candle_requests(session: PageSession) -> list[str]:
    """Every candle-page request URL so far, in ledger order."""
    return [url for url in session.requests if CANDLES_URL in url]


def test_db_paging_patches_grid_in_place(new_page: NewPageFactory, tmp_path: Path) -> None:
    """Next/Prev patch ONLY the tbody + pager — controls, table, datepicker survive."""
    fixture = build_db_app(tmp_path)
    session = new_page(fixture)
    page = session.page
    session.goto("/db")
    page.wait_for_selector("#tables .tbl")

    # Rows-per-page FIRST (design step 1): mode is still "table" with nothing
    # selected, so the change listener is a proven no-op — zero requests fire.
    page.select_option("#limit", "10")
    assert _candle_requests(session) == []

    # Enter the spot candle lake → the symbol→interval cascade prompt.
    with page.expect_response(lambda r: SYMBOLS_URL in r.url):
        page.click('#tables .tbl[data-candles="spot"]')
    page.wait_for_selector("#csym")

    # Pick the one seeded symbol → onSpotSymbolChange runs the FULL render
    # (the one sanctioned renderCandleView) and queries page 1 at limit=10.
    with page.expect_response(lambda r: CANDLES_URL in r.url):
        page.select_option("#csym", label=fixture.ground["symbol"])
    prange = page.locator("#dbgrid .pager .prange")
    expect(prange).to_have_text(fixture.ground["page1_range"])  # EN-DASH "1–10 / 15"
    expect(page.locator("#dbgrid")).not_to_have_class(_IS_LOADING)

    # Datepicker enhanced on the LIVE #cstart before pinning it (design step 3).
    # state="attached": the enhancement HIDES the native date input behind the
    # bx-datepicker UI, so the default visible-state wait would never resolve.
    page.wait_for_selector('input#cstart[data-dp-done="1"]', state="attached")
    _stamp_pins(page)
    data_cols = page.locator(GRID_TABLE).get_attribute("data-cols")
    assert data_cols is not None and data_cols.startswith("[")
    first_cell_page1 = _first_cell_text(page)

    # Baseline request ledger (design step 4): EXACTLY one page read carrying
    # limit=10 offset=0, EXACTLY one cascade fetch; pager at the left edge.
    baseline = _candle_requests(session)
    assert len(baseline) == 1
    assert "limit=10" in baseline[0] and "offset=0" in baseline[0]
    assert session.request_count(SYMBOLS_URL) == 1
    expect(page.locator("#dbprev")).to_be_disabled()
    expect(page.locator("#dbnext")).to_be_enabled()

    # ── Next › (design step 5) ────────────────────────────────────────────
    with page.expect_response(lambda r: CANDLES_URL in r.url) as next_info:
        page.click("#dbnext")
    expect(prange).to_have_text(fixture.ground["page2_range"])  # "11–15 / 15"
    expect(page.locator("#dbgrid")).not_to_have_class(_IS_LOADING)
    # Shell NOT rebuilt: every pin survives, columns + datepicker untouched.
    assert _pin_values(page) == [1, 1, 1]
    assert page.locator(GRID_TABLE).get_attribute("data-cols") == data_cols
    assert page.locator("#cstart").get_attribute("data-dp-done") == "1"
    expect(page.locator("#dbprev")).to_be_enabled()
    expect(page.locator("#dbnext")).to_be_disabled()
    first_cell_page2 = _first_cell_text(page)
    assert first_cell_page2 != first_cell_page1
    # Ledger exactness: ONE new page read (offset=10, limit=10), no re-cascade.
    after_next = _candle_requests(session)
    assert len(after_next) == 2
    assert "offset=10" in after_next[1] and "limit=10" in after_next[1]
    assert "offset=10" in next_info.value.url
    assert session.request_count(SYMBOLS_URL) == 1

    # ── ‹ Prev (design step 6) ────────────────────────────────────────────
    with page.expect_response(lambda r: CANDLES_URL in r.url) as prev_info:
        page.click("#dbprev")
    expect(prange).to_have_text(fixture.ground["page1_range"])
    expect(page.locator("#dbgrid")).not_to_have_class(_IS_LOADING)
    assert _pin_values(page) == [1, 1, 1]
    assert page.locator(GRID_TABLE).get_attribute("data-cols") == data_cols
    assert _first_cell_text(page) == first_cell_page1
    expect(page.locator("#dbprev")).to_be_disabled()
    expect(page.locator("#dbnext")).to_be_enabled()
    after_prev = _candle_requests(session)
    assert len(after_prev) == 3
    assert "offset=0" in after_prev[2] and "limit=10" in after_prev[2]
    assert "offset=0" in prev_info.value.url
    assert session.request_count(SYMBOLS_URL) == 1
    # Ledger cleanliness re-asserted here (fail WITH artifacts) — the harness
    # teardown re-checks for late messages.
    assert session.page_errors == []
    assert session.dialogs == []
    assert session.console == []
