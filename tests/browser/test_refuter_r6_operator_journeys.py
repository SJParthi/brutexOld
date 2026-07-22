"""Round-6 refuter OPERATOR-TRUTH journeys (PR #1358 club vehicle).

Five exact operator journeys over synthetic lakes, driven through the REAL
app (uvicorn + Chromium; the log-count leg via TestClient):

* (a) FINALIZED day (1m through 15:29, prebuilt by the production
  ``materialize_lake_timeframes``): switching all 21 TFs at the default
  (Latest) shows the IDENTICAL latest close everywhere, a green verify chip
  on every TF, and never an incomplete-day note.
* (b) EXPLICIT 15:27 pick @ 3minute: the served close equals the /db
  3minute 15:27 row byte-exactly; the chip stays truthful (green, anchored
  at the sent bucket-final 15:29); the as-of label shows the bucket span.
* (c) STALE-stored lake (the #1632 repro shape): the coarse TF falls back
  to the FRESH 1-minute derive (never the stale stored close), the
  provenance line counts the fallback under derived-from-1m, the chip is
  TRUTHFUL for the 1m-base contract it discloses, and the guard emits
  exactly ONE capped ``movers.stored_stale_fallback`` WARN.
* (d) OPTIONS tab: every chip value equals the number recomputed from the
  CAPTURED payload (PCR ratio-of-sums via the bxFixed2 law, coverage
  N/M + missing-OI), every buildup pill equals the sign(price)x sign(OI)
  quadrant of its own payload row, and the i explainer is collapsed.
* (e) INCOMPLETE day (1m ends 09:17): the Latest intraday view carries the
  stored-data note wording ("data through HH:MM - day incomplete in DB",
  the mid-bucket "completed <tf> bar would close HH:MM" clause only when
  mid-bucket), and the word "forming" appears NOWHERE on the page.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.prebuild_gate import PrebuildGate
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

from tests.browser._fixtures import AppFixture, _open_store, build_movers_app
from tests.browser._guards import browser_suite_enabled
from tests.browser.conftest import NewPageFactory, freeze_page_clock
from tests.integration.test_dashboard_movers import _DAY as MOVERS_DAY_DATE
from tests.integration.test_dashboard_movers import _idx, _min_bar
from tests.integration.test_dashboard_movers import _write as movers_write

if not browser_suite_enabled():
    pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect

pytestmark = pytest.mark.browser

_PREV_DAY = date(2024, 6, 12)
_CHIP = "#verifyChip"
_MOVERS_GLOB = "**/api/v1/movers**"
_SNAPSHOT_DATE = date(2024, 1, 2)
_RUN_ID = "refuter-r6-journeys"
_CSV_SHA = "0" * 64

#: The finalized-day fixture's session-final close: the ONE latest close every
#: timeframe must show under the Latest default.
_FINAL_CLOSE = "21998.35"
_FINAL_LTP = "₹21,998.35"


def _fixed2(n: float) -> str:
    """Python replica of the bxFixed2 display law (half-up on shortest repr)."""
    return str(Decimal(repr(n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _switch_interval(page: Any, interval: str) -> None:
    with page.expect_response(_MOVERS_GLOB):
        page.select_option("#interval", interval)


# ── journey (a)+(b) fixture: a FINALIZED day, production-prebuilt ─────────


def _seed_finalized_day(tmp: Path) -> None:
    """NIFTY 1m through 15:29 (finalized day) + prev-day baseline; prebuild
    all 19 intraday TFs + the derived 1day with the PRODUCTION materializer."""
    movers_write(
        tmp,
        "NIFTY",
        "1minute",
        [
            _min_bar(hour=15, minute=29, close="19998.50", volume=100, day=_PREV_DAY),
            _min_bar(hour=9, minute=15, close="20000.00", volume=100),
            _min_bar(hour=9, minute=16, close="20100.10", volume=100),
            _min_bar(hour=12, minute=0, close="21000.00", volume=50),
            _min_bar(hour=15, minute=27, close="21950.25", volume=80),
            _min_bar(hour=15, minute=28, close="21990.00", volume=90),
            _min_bar(hour=15, minute=29, close=_FINAL_CLOSE, volume=500),
        ],
    )
    result = materialize_lake_timeframes(
        lake_root=tmp,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        kind=PrebuildKind.SPOT,
        include_daily=True,
    )
    assert result.total_bars > 0  # the prebuild really wrote stored partitions


def _finalized_day_app(tmp: Path) -> AppFixture:
    _seed_finalized_day(tmp)
    store = _open_store(tmp)
    store.upsert_many(
        [_idx("NIFTY")],
        snapshot_date=_SNAPSHOT_DATE,
        run_id=_RUN_ID,
        csv_source_sha256=_CSV_SHA,
    )
    app = build_app(DashboardState(), instrument_store=store, prebuild_gate=PrebuildGate())
    return AppFixture(app=app, ground={}, cleanup=store.close)


# ── (a) finalized day: 21 TFs, one latest close, green chip, no note ──────


def test_a_finalized_day_all_21_tfs_identical_latest_close_and_green_chip(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    fixture = _finalized_day_app(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    tf_values = session.page.eval_on_selector_all(
        "#interval option", "els => els.map(e => e.value)"
    )
    assert len(tf_values) == 21, f"expected the 21-TF vocabulary, got {tf_values}"
    seen: dict[str, str | None] = {}
    for tf in tf_values:
        if session.page.locator("#interval").input_value() != tf:
            _switch_interval(session.page, tf)
        row = session.page.locator("#rows tr").first
        expect(row.locator("td.name")).to_have_text("NIFTY")
        ltp = row.locator("td").nth(1)
        expect(ltp).to_have_text(_FINAL_LTP)
        seen[tf] = ltp.text_content()
        # (a) no incomplete-day note anywhere on a finalized day.
        expect(session.page.locator("#asof")).not_to_contain_text("day incomplete")
        expect(session.page.locator("#asof")).not_to_contain_text("data through")
        # (a) verify chip green on EVERY timeframe.
        session.page.clock.fast_forward(200)
        chip = session.page.locator(_CHIP)
        expect(chip).to_have_class("pos")
        expect(chip).to_have_text(re.compile(r"^✓ matches /db"))
    assert set(seen.values()) == {_FINAL_LTP}, f"latest close diverged per TF: {seen}"
    assert session.page_errors == []


# ── (b) explicit 15:27 pick @ 3minute — byte-exact vs the /db row ─────────


def test_b_explicit_1527_pick_at_3minute_serves_the_db_row_byte_exact(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    fixture = _finalized_day_app(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _switch_interval(session.page, "3minute")
    # The operator picks the option LABELED 15:27 (the /db bar label); the
    # mapping law sends the bucket-final 15:29 as ?time=.
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.select_option("#time", label="15:27")
    assert session.page.locator("#time").input_value() == "15:29"
    row = session.page.locator("#rows tr").first
    expect(row.locator("td.name")).to_have_text("NIFTY")
    ltp = row.locator("td").nth(1)
    expect(ltp).to_have_text(_FINAL_LTP)
    # The as-of label shows the containing-bucket SPAN.
    expect(session.page.locator("#asof")).to_contain_text("15:27–15:29")

    # Independent /db read of the STORED 3minute partition: the 15:27 row.
    resp = session.page.request.get(
        session.base_url
        + "/api/v1/db/candles?exchange=NSE&segment=INDEX&symbol=NIFTY"
        + f"&interval=3minute&start={MOVERS_DAY_DATE}&end={MOVERS_DAY_DATE}"
        + "&desc=true&limit=500"
    )
    assert resp.ok
    body = resp.json()
    cols = body["columns"]
    ts_idx, close_idx = cols.index("timestamp"), cols.index("close")
    target = [r for r in body["rows"] if str(r[ts_idx])[11:16] == "15:27"]
    assert len(target) == 1, f"/db 3minute has no unique 15:27 row: {body['rows']}"
    db_close = Decimal(str(target[0][close_idx]))
    shown = Decimal(str(ltp.text_content()).replace("₹", "").replace(",", ""))
    assert db_close == shown == Decimal(_FINAL_CLOSE), (
        f"served close {shown} != /db 3minute 15:27 close {db_close}"
    )
    # Byte-exact 2dp display equality of both sides.
    assert f"{db_close:.2f}" == f"{shown:.2f}" == _FINAL_CLOSE

    # The chip stays truthful for the explicit pick (anchored at 15:29).
    session.page.clock.fast_forward(200)
    chip = session.page.locator(_CHIP)
    expect(chip).to_have_class("pos")
    expect(chip).to_have_text(re.compile(r"^✓ matches /db"))
    assert session.page_errors == []


# ── (c) stale-stored lake: fresh fallback + truthful chip + ONE warn ──────


def _seed_stale_stored_3minute(tmp: Path) -> None:
    """The #1632 repro shape: a stored 3minute close (21365) that provably
    drifted from the raw 1-minute base (fresh derive close 22000)."""
    movers_write(
        tmp,
        "NIFTY",
        "3minute",
        [_min_bar(hour=9, minute=15, close="21365", volume=700)],
    )


def test_c_stale_stored_lake_serves_fresh_with_truthful_chip(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    fixture = build_movers_app(tmp_path)
    _seed_stale_stored_3minute(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _switch_interval(session.page, "3minute")
    # The guard serves the FRESH derive (22000), never the stale stored 21365.
    row = session.page.locator("#rows tr").filter(has_text="₹22,000.00")
    expect(row).to_have_count(1)
    expect(row.locator("td.name")).to_have_text("NIFTY")
    expect(session.page.locator("#rows")).not_to_contain_text("21,365")
    # Provenance counts the fallback honestly: 0 stored-TF / 3 derived-from-1m.
    expect(session.page.locator("#srcText")).to_contain_text(
        "reads: 0 stored-TF / 3 derived-from-1m"
    )
    # /db at the DISPLAY TF still serves the stale stored bytes — the
    # independent evidence the stored partition really is stale.
    resp = session.page.request.get(
        session.base_url
        + "/api/v1/db/candles?exchange=NSE&segment=INDEX&symbol=NIFTY"
        + f"&interval=3minute&start={MOVERS_DAY_DATE}&end={MOVERS_DAY_DATE}"
        + "&desc=true&limit=500"
    )
    assert resp.ok
    body = resp.json()
    close_idx = body["columns"].index("close")
    stored_closes = {Decimal(str(r[close_idx])) for r in body["rows"]}
    assert stored_closes == {Decimal("21365")}, f"/db 3minute stored bytes: {stored_closes}"
    # The chip verifies against the 1-MINUTE BASE (its disclosed contract) —
    # the fresh served value matches it, so green is the TRUTHFUL verdict,
    # and the wording names the verification plane ("1m base").
    session.page.clock.fast_forward(200)
    chip = session.page.locator(_CHIP)
    expect(chip).to_have_class("pos")
    expect(chip).to_have_text(re.compile(r"^✓ matches /db"))
    expect(chip).to_contain_text("1m base")
    assert session.page_errors == []


def test_c_stale_fallback_emits_exactly_one_capped_warn(tmp_path: Path) -> None:
    """TestClient leg: the guard's WARN discipline — ONE capped, deduped
    ``movers.stored_stale_fallback`` for the one stale symbol, no suppressed
    summary, and the served payload row is the FRESH value."""
    from fastapi.testclient import TestClient
    from structlog.testing import capture_logs

    fixture = build_movers_app(tmp_path)
    _seed_stale_stored_3minute(tmp_path)
    try:
        with TestClient(fixture.app) as client, capture_logs() as logs:
            r = client.get(
                "/api/v1/movers",
                params={
                    "asset": "spot",
                    "interval": "3minute",
                    "date": MOVERS_DAY_DATE.isoformat(),
                },
            )
        assert r.status_code == 200
        payload = r.json()
        rows = payload.get("price_gainers") or payload.get("rows") or []
        nifty = [row for row in rows if row.get("symbol") == "NIFTY"]
        assert nifty and nifty[0]["ltp"] == "₹22,000.00", f"rows: {rows}"
        warns = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
        assert len(warns) == 1, f"expected exactly ONE stale-fallback warn: {warns}"
        assert warns[0]["symbol"] == "NIFTY"
        assert warns[0]["interval"] == "3minute"
        assert warns[0]["snapshot_day"] == MOVERS_DAY_DATE.isoformat()
        suppressed = [e for e in logs if e["event"] == "movers.stored_stale_fallback_suppressed"]
        assert suppressed == []
    finally:
        fixture.cleanup()


# ── (d) options tab: chips == payload numbers · buildup quadrants · ⓘ ─────

_EXPIRY_TOKEN = "27Jun24"
_CE_LONG = f"NSE-NIFTY-{_EXPIRY_TOKEN}-22000-CE"  # price↑ OI↑ → Long Buildup
_PE_SHORT = f"NSE-NIFTY-{_EXPIRY_TOKEN}-22000-PE"  # price↓ OI↑ → Short Buildup
_CE_COVER = f"NSE-NIFTY-{_EXPIRY_TOKEN}-22100-CE"  # price↑ OI↓ → Short Covering
_PE_UNWIND = f"NSE-NIFTY-{_EXPIRY_TOKEN}-22100-PE"  # price↓ OI↓ → Long Unwinding
_CE_NULL_OI = f"NSE-NIFTY-{_EXPIRY_TOKEN}-22200-CE"  # missing OI → "—" + amber


def _obar(
    *, hour: int, minute: int, close: str, oi: int | None, day: date = MOVERS_DAY_DATE
) -> CandleBar:
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=10,
        open_interest=oi,
    )


def _seed_quadrant_chain(tmp: Path) -> None:
    writer = ParquetWriter(lake_root=tmp)

    def fno(symbol: str, bars: list[CandleBar]) -> None:
        writer.write(exchange="NSE", segment="FNO", symbol=symbol, interval="1minute", bars=bars)

    fno(
        _CE_LONG,
        [
            _obar(hour=15, minute=29, close="100.00", oi=5000, day=_PREV_DAY),
            _obar(hour=9, minute=15, close="120.00", oi=5200),
            _obar(hour=9, minute=16, close="150.00", oi=6000),
        ],
    )
    fno(
        _PE_SHORT,
        [
            _obar(hour=15, minute=29, close="200.00", oi=4000, day=_PREV_DAY),
            _obar(hour=9, minute=15, close="180.00", oi=4200),
            _obar(hour=9, minute=16, close="150.00", oi=4500),
        ],
    )
    fno(
        _CE_COVER,
        [
            _obar(hour=15, minute=29, close="90.00", oi=3000, day=_PREV_DAY),
            _obar(hour=9, minute=15, close="95.00", oi=2800),
            _obar(hour=9, minute=16, close="99.00", oi=2500),
        ],
    )
    fno(
        _PE_UNWIND,
        [
            _obar(hour=15, minute=29, close="210.00", oi=6000, day=_PREV_DAY),
            _obar(hour=9, minute=15, close="205.00", oi=5500),
            _obar(hour=9, minute=16, close="200.00", oi=5000),
        ],
    )
    fno(
        _CE_NULL_OI,
        [
            _obar(hour=15, minute=29, close="50.00", oi=None, day=_PREV_DAY),
            _obar(hour=9, minute=15, close="55.00", oi=None),
            _obar(hour=9, minute=16, close="60.00", oi=None),
        ],
    )


def _options_app(tmp: Path) -> AppFixture:
    fixture = build_movers_app(tmp)
    _seed_quadrant_chain(tmp)
    return fixture


def _expected_buildup(pct: str | None, oi_chg: Any) -> str:
    """The sign(price)×sign(OI) quadrant off the payload row's own fields."""
    if pct is None or not isinstance(oi_chg, (int, float)):
        return "—"
    p = float(pct)
    if p == 0 or oi_chg == 0:
        return "—"
    if p > 0:
        return "Long Buildup" if oi_chg > 0 else "Short Covering"
    return "Short Buildup" if oi_chg > 0 else "Long Unwinding"


def test_d_options_chips_match_payload_and_buildup_quadrants(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    fixture = _options_app(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    with session.page.expect_response(_MOVERS_GLOB) as resp_info:
        session.page.locator('.tab[data-asset="options"]').click()
    payload = resp_info.value.json()
    session.page.wait_for_selector("#rows td.name")

    # ── ⓘ explainer collapsed by default, options body selected ──
    explain = session.page.locator("#explain")
    assert explain.evaluate("el => el.open") is False, "ⓘ explainer must boot collapsed"
    expect(explain.locator("summary")).to_have_text("ⓘ What am I looking at?")
    assert session.page.locator("#explainOptions").evaluate("el => el.hidden") is False
    assert session.page.locator("#explainSpot").evaluate("el => el.hidden") is True

    # ── chips == the numbers recomputed from the CAPTURED payload ──
    pcr = payload["pcr"]
    assert isinstance(pcr, list) and pcr, f"payload carries no pcr rows: {payload.keys()}"
    sums = {
        k: sum(r.get(k) or 0 for r in pcr)
        for k in (
            "put_oi",
            "call_oi",
            "put_volume",
            "call_volume",
            "legs_read",
            "legs_total",
            "oi_missing_legs",
        )
    }
    # Sanity-pin the seeded ground truth so a payload-side fault cannot pass
    # vacuously: puts 4500+5000, calls 6000+2500 (the null-OI leg adds 0).
    assert sums["put_oi"] == 9500 and sums["call_oi"] == 8500, sums
    assert sums["legs_read"] == sums["legs_total"] == 5
    assert sums["oi_missing_legs"] == 1

    oi_ratio = _fixed2(sums["put_oi"] / sums["call_oi"])
    vol_ratio = _fixed2(sums["put_volume"] / sums["call_volume"])
    expect(session.page.locator("#chipPcrOi b")).to_have_text(oi_ratio)
    expect(session.page.locator("#chipPcrVol b")).to_have_text(vol_ratio)
    # The titles carry the exact P/C sums (readable disclosure).
    oi_title = session.page.locator("#chipPcrOi").get_attribute("title")
    assert oi_title is not None
    assert f"P {sums['put_oi']:,} / C {sums['call_oi']:,}" in oi_title
    assert f"across {len(pcr)} chain(s)" in oi_title
    # Expiry chip: the ONE shared expiry, worded, with the live tier.
    expiries = {r["expiry"] for r in pcr}
    assert expiries == {"2024-06-27"}
    expect(session.page.locator("#chipExpiry b")).to_have_text("27-Jun-2024 (near tier)")
    # Coverage chip: N/M + missing-OI, AMBER, click names the missing leg.
    cov_text = f"{sums['legs_read']}/{sums['legs_total']} contracts"
    if sums["oi_missing_legs"]:
        cov_text += f" · {sums['oi_missing_legs']} missing OI"
    cov = session.page.locator("#chipCoverage")
    expect(cov.locator("b")).to_have_text(cov_text)
    assert "amber" in (cov.get_attribute("class") or "")
    cov.click()
    detail = session.page.locator("#chipDetail")
    expect(detail).to_be_visible()
    named = [s for r in pcr for s in (r.get("oi_missing_symbols") or [])]
    assert named == [_CE_NULL_OI], f"payload oi_missing_symbols: {named}"
    expect(detail).to_contain_text(_CE_NULL_OI)

    # ── buildup pills: quadrant per rendered row vs its OWN payload row ──
    cat_rows = payload["highest_oi"]
    dom_rows = session.page.locator("#rows tr")
    assert dom_rows.count() == len(cat_rows) == 4  # null-OI leg excluded here
    for i, prow in enumerate(cat_rows):
        cells = dom_rows.nth(i).locator("td")
        expect(cells.nth(0)).to_have_text(str(prow["name"]))
        expect(cells.nth(7).locator("span.bupill")).to_have_text(
            _expected_buildup(prow.get("pct_change"), prow.get("oi_chg"))
        )
    # The four quadrants are all genuinely exercised by the seeded chain.
    got = {
        str(r["name"]): _expected_buildup(r.get("pct_change"), r.get("oi_chg")) for r in cat_rows
    }
    # The wire `name` is the DECODED display form of the contract symbol.
    assert got == {
        "NIFTY 27-Jun-2024 22000 CE": "Long Buildup",
        "NIFTY 27-Jun-2024 22000 PE": "Short Buildup",
        "NIFTY 27-Jun-2024 22100 CE": "Short Covering",
        "NIFTY 27-Jun-2024 22100 PE": "Long Unwinding",
    }, got

    # The null-OI leg renders the honest "—" pill on the Top Volume view
    # (cached pill switch — no refetch).
    null_oi_name = "NIFTY 27-Jun-2024 22200 CE"
    session.page.locator('.pill[data-cat="volume"]').click()
    vol_rows = session.page.locator("#rows tr")
    payload_vol = payload["volume"]
    assert {str(r["name"]) for r in payload_vol} >= {null_oi_name}
    idx = [str(r["name"]) for r in payload_vol].index(null_oi_name)
    expect(vol_rows.nth(idx).locator("td").nth(7).locator("span.bupill")).to_have_text("—")
    assert session.page_errors == []


# ── (e) incomplete day: stored-data note wording, no "forming" ────────────


def test_e_incomplete_day_stored_data_note_and_no_forming_anywhere(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    fixture = build_movers_app(tmp_path)  # the fixture day's 1m ends 09:17
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")

    # Daily default view: no incomplete-day claim (daily abstains).
    expect(session.page.locator("#asof")).not_to_contain_text("day incomplete")

    # 5minute Latest: mid-bucket → the FULL stored-data note.
    _switch_interval(session.page, "5minute")
    asof = session.page.locator("#asof")
    expect(asof).to_contain_text("latest (09:17)")
    expect(asof).to_contain_text("data through 09:17 — day incomplete in DB")
    expect(asof).to_contain_text("; completed 5minute bar would close 09:19")

    # 3minute Latest: 09:17 IS the bucket-final → no "would close" clause.
    _switch_interval(session.page, "3minute")
    expect(asof).to_contain_text("data through 09:17 — day incomplete in DB")
    expect(asof).not_to_contain_text("would close")

    # The chip stays green — the served (stored-so-far) values match the 1m base.
    session.page.clock.fast_forward(200)
    expect(session.page.locator(_CHIP)).to_have_class("pos")

    # The "forming" concept is CUT: it must appear NOWHERE the operator reads.
    body_text = session.page.evaluate("document.body.innerText")
    assert "forming" not in body_text.lower(), body_text
    assert session.page_errors == []
