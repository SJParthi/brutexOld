"""Round-4 refuter EXECUTION probes — the /db candle deep-link boot handler
(DEC-MOVERS-DB-PROVENANCE-VERIFY-001, deliverable #2).

The ~70-line ``deepLinkCandles()`` handler was pinned by grep-substring tests
only, while its two sibling deliverables (source line, verify chip) carry real
Chromium execution proofs — a cascade field rename (``s.intervals``, the
``_spotKey`` shape) or a boot Promise-wiring regression would have broken the
operator's click-through with every gate still green. These arms execute the
handler's OWN ledgered claims end-to-end against the REAL ``build_app`` server:

* **spot grammar** — ``?candles=spot&…`` resolves the symbol through the real
  cascade, materialises its stored intervals, fires EXACTLY ONE candle query
  (the "inlined so the default-1minute query never double-fires" claim)
  carrying the requested coordinate + the date as inclusive start/end bounds,
  syncs the cascade select + both date inputs FROM cstate, and highlights the
  sidebar row (``markCandles``);
* **fno grammar** — ``?candles=fno&…`` materialises ONE descriptor directly
  (no spot-cascade fetch at all), queries ``segment=FNO`` at the pinned
  1-minute partition, and renders the contract's rows with the cascade
  selects in their documented prompt state;
* **unstored-TF fallback** — a valid-format but UNSTORED ``interval=3minute``
  falls to the 1-minute base, never an empty grid;
* **invalid params DROP** — a malformed ``date`` + junk ``interval`` are both
  dropped (no start/end on the wire, empty date inputs, 1-minute pick) and
  the boot never blocks;
* **bare /db** — no ``?candles=`` boots byte-identically: zero candle / spot-
  cascade requests, no auto-selected sidebar row, console clean (harness).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

from tests.browser._fixtures import DB_DAY, DB_INTERVAL, DB_SYMBOL, build_db_app
from tests.browser._guards import browser_suite_enabled
from tests.browser.conftest import NewPageFactory

if not browser_suite_enabled():
    pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect

pytestmark = pytest.mark.browser

#: Candle page reads — trailing ``?`` excludes ``/api/v1/db/candles/datasets``.
_CANDLES_URL: Final[str] = "/api/v1/db/candles?"

#: The spot symbol→interval cascade fetch (spot grammar: exactly once;
#: fno grammar + bare boot: never).
_SYMBOLS_URL: Final[str] = "/api/v1/db/spot/symbols"

#: First data cell of the rendered grid (test_db_paging's pinned shape).
_FIRST_CELL: Final[str] = "#dbgrid .gridwrap > table tbody tr:first-child td:first-child"

#: The fixture symbol's cascade key (db.html ``_spotKey`` — "exch|seg|sym").
_SPOT_KEY: Final[str] = f"NSE|INDEX|{DB_SYMBOL}"

#: The deep-linked F&O contract (decodable ``parse_fno_symbol`` grammar).
_FNO_SYMBOL: Final[str] = "NSE-NIFTY-27Jun24-FUT"
#: Bars seeded for it on DB_DAY (09:15 + 09:16 IST).
_FNO_BAR_COUNT: Final[int] = 2


def _spot_deep_link(*, interval: str = DB_INTERVAL, date: str = DB_DAY) -> str:
    return (
        f"/db?candles=spot&exchange=NSE&segment=INDEX&symbol={DB_SYMBOL}"
        f"&interval={interval}&date={date}"
    )


def _seed_fno(tmp_path: Path) -> None:
    """Two 1-minute FUT bars on DB_DAY through the production writer."""
    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol=_FNO_SYMBOL,
        interval="1minute",
        bars=[
            CandleBar(
                # 09:15/09:16 IST on DB_DAY = 03:45/03:46 UTC (+05:30, no DST).
                timestamp=datetime(2025, 1, 2, 3, 45 + i, tzinfo=UTC),
                open=Decimal("22000") + i,
                high=Decimal("22010") + i,
                low=Decimal("21990") + i,
                close=Decimal("22005") + i,
                volume=100 + i,
                open_interest=1000 + i,
            )
            for i in range(_FNO_BAR_COUNT)
        ],
    )


def test_spot_deep_link_boots_prefiltered_grid(new_page: NewPageFactory, tmp_path: Path) -> None:
    """The full spot grammar: one query, right coordinate, synced controls."""
    fixture = build_db_app(tmp_path)
    session = new_page(fixture)
    page = session.page
    with page.expect_response(lambda r: _CANDLES_URL in r.url) as info:
        session.goto(_spot_deep_link())
    # The wire carries the EXACT deep-linked coordinate; the date propagated
    # into cstate.start/end rides as the inclusive start/end bounds.
    url = info.value.url
    for token in (
        "exchange=NSE",
        "segment=INDEX",
        f"symbol={DB_SYMBOL}",
        f"interval={DB_INTERVAL}",
        f"start={DB_DAY}",
        f"end={DB_DAY}",
    ):
        assert token in url, f"missing {token!r} in {url!r}"
    # All 15 fixture bars sit on DB_DAY → one full page at the default limit.
    expect(page.locator("#dbgrid .pager .prange")).to_have_text("1–15 / 15")
    expect(page.locator(_FIRST_CELL)).not_to_have_text("")
    # Controls render synced FROM cstate: cascade symbol picked, dates filled.
    expect(page.locator("#csym")).to_have_value(_SPOT_KEY)
    expect(page.locator("#cstart")).to_have_value(DB_DAY)
    expect(page.locator("#cend")).to_have_value(DB_DAY)
    # markCandles highlighted the spot lake's sidebar row.
    expect(page.locator('#tables .tbl[data-candles="spot"]')).to_have_class("tbl sel")
    # Ledger exactness: the inlined resolve fired EXACTLY one candle query
    # (never the onSpotSymbolChange default-1minute double-fire) and EXACTLY
    # one cascade fetch.
    assert session.request_count(_CANDLES_URL) == 1
    assert session.request_count(_SYMBOLS_URL) == 1
    assert session.page_errors == []


def test_fno_deep_link_materialises_the_contract_grid(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """The fno grammar: direct descriptor, segment=FNO wire, rows painted."""
    _seed_fno(tmp_path)
    fixture = build_db_app(tmp_path)
    session = new_page(fixture)
    page = session.page
    with page.expect_response(lambda r: _CANDLES_URL in r.url) as info:
        session.goto(
            f"/db?candles=fno&exchange=NSE&segment=FNO&symbol={_FNO_SYMBOL}"
            f"&interval=1minute&date={DB_DAY}"
        )
    url = info.value.url
    for token in (
        "exchange=NSE",
        "segment=FNO",
        f"symbol={_FNO_SYMBOL}",
        "interval=1minute",
        f"start={DB_DAY}",
        f"end={DB_DAY}",
    ):
        assert token in url, f"missing {token!r} in {url!r}"
    expect(page.locator("#dbgrid .pager .prange")).to_have_text(
        f"1–{_FNO_BAR_COUNT} / {_FNO_BAR_COUNT}"
    )
    expect(page.locator(_FIRST_CELL)).not_to_have_text("")
    # The folded applyDeepLink auto-selects the underlying via the
    # server-vocabulary longest-prefix match (DEC-MOVERS-DRILL-DOWN-001 #1662
    # + fold #2127): NSE-NIFTY-27Jun24-FUT resolves to the NIFTY underlying.
    expect(page.locator("#cund")).to_have_value("NIFTY")
    # markCandles highlighted the F&O lake's sidebar row, not the spot one.
    expect(page.locator('#tables .tbl[data-candles="fno"]')).to_have_class("tbl sel")
    # The fno arm never loads the spot cascade; exactly one candle query.
    assert session.request_count(_SYMBOLS_URL) == 0
    assert session.request_count(_CANDLES_URL) == 1
    assert session.page_errors == []


def test_spot_deep_link_unstored_interval_falls_to_1minute(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """A valid-format but unstored TF deep-links to the 1-minute base."""
    fixture = build_db_app(tmp_path)
    session = new_page(fixture)
    page = session.page
    with page.expect_response(lambda r: _CANDLES_URL in r.url) as info:
        session.goto(_spot_deep_link(interval="3minute"))
    # 3minute passes the format gate but is not stored → the 1minute pick.
    assert "interval=1minute" in info.value.url
    assert "interval=3minute" not in info.value.url
    expect(page.locator("#dbgrid .pager .prange")).to_have_text("1–15 / 15")
    assert session.request_count(_CANDLES_URL) == 1
    assert session.page_errors == []


def test_spot_deep_link_invalid_date_and_interval_drop_the_params(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """Malformed date + junk interval each DROP; boot never blocks."""
    fixture = build_db_app(tmp_path)
    session = new_page(fixture)
    page = session.page
    with page.expect_response(lambda r: _CANDLES_URL in r.url) as info:
        session.goto(_spot_deep_link(interval="junk", date="13-06-2024"))
    url = info.value.url
    # Dropped date → no start/end bounds on the wire, empty date inputs.
    assert "start=" not in url and "end=" not in url
    # Dropped interval → the default 1-minute pick.
    assert "interval=1minute" in url
    expect(page.locator("#dbgrid .pager .prange")).to_have_text("1–15 / 15")
    expect(page.locator("#cstart")).to_have_value("")
    expect(page.locator("#cend")).to_have_value("")
    assert session.request_count(_CANDLES_URL) == 1
    assert session.page_errors == []


def test_bare_db_boot_fires_no_deep_link(new_page: NewPageFactory, tmp_path: Path) -> None:
    """No ``?candles=`` → byte-identical boot: zero candle/cascade requests."""
    fixture = build_db_app(tmp_path)
    session = new_page(fixture)
    page = session.page
    session.goto("/db")
    page.wait_for_selector("#tables .tbl")
    # The sidebar rendered with NO auto-selected lake row and NO grid query.
    expect(page.locator("#tables .tbl.sel")).to_have_count(0)
    assert session.request_count(_CANDLES_URL) == 0
    assert session.request_count(_SYMBOLS_URL) == 0
    assert session.page_errors == []
