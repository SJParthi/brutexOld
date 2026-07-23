"""Round-2 refuter EXECUTION probes (DEC-MOVERS-DB-PROVENANCE-VERIFY-001).

Break-it scenarios the committed suite does NOT execute: the spot INTRADAY
anchored verify under a live-pull lake append, the explicit-time pick anchor,
the flow-level %CHNG compare (green AND red arms), the 10 s timeout abort, a
READABLE special-char symbol ("M&M" — the URLSearchParams '&' trap), and the
FUTURES tab's segment=FNO / LTP-only verify. Each asserts the chip's verdict
is TRUTHFUL for the constructed lake — a red here on a matching view (or a
green on a mismatch) is a confirmed chip lie.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

from tests.browser._fixtures import AppFixture, _open_store, build_movers_app
from tests.browser._guards import browser_suite_enabled
from tests.browser.conftest import NewPageFactory, freeze_page_clock
from tests.integration.test_dashboard_movers import _DAY as MOVERS_DAY_DATE
from tests.integration.test_dashboard_movers import (
    _day_bar,
    _idx,
    _min_bar,
)
from tests.integration.test_dashboard_movers import (
    _write as movers_write,
)

if not browser_suite_enabled():
    pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect

pytestmark = pytest.mark.browser

_PREV_DAY = date(2024, 6, 12)
_CHIP = "#verifyChip"
_DB_CANDLES_GLOB = "**/api/v1/db/candles**"
_MOVERS_GLOB = "**/api/v1/movers**"

_SNAPSHOT_DATE = date(2024, 1, 2)
_RUN_ID = "refuter-r3"
_CSV_SHA = "0" * 64


def _fno_bar(
    *, hour: int, minute: int, close: str, oi: int, day: date = MOVERS_DAY_DATE
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


def _switch_interval(page: Any, interval: str) -> None:
    """Pick a display TF exactly as the operator does (change event → load)."""
    with page.expect_response(_MOVERS_GLOB):
        page.select_option("#interval", interval)


def _pick_time(page: Any, hhmm: str) -> None:
    with page.expect_response(_MOVERS_GLOB):
        page.select_option("#time", hhmm)


def _build_app_with(
    tmp_path: Path,
    *,
    extra_symbols: list[str] | None = None,
    seed: Any | None = None,
) -> AppFixture:
    """build_movers_app + optional extra INDEX symbols/lake rows (probe-local)."""
    fixture = build_movers_app(tmp_path)
    fixture.cleanup()  # close the inner store; reopen after our extra seeding
    if seed is not None:
        seed(tmp_path)
    store = _open_store(tmp_path)
    rows = [_idx(s) for s in ("NIFTY", "BANKNIFTY", *(extra_symbols or []))]
    store.upsert_many(
        rows, snapshot_date=_SNAPSHOT_DATE, run_id=_RUN_ID, csv_source_sha256=_CSV_SHA
    )
    from brutex.dashboard.prebuild_gate import PrebuildGate

    app = build_app(DashboardState(), instrument_store=store, prebuild_gate=PrebuildGate())
    return AppFixture(app=app, ground=dict(fixture.ground), cleanup=store.close)


# ── S1: spot intraday Latest — anchored verify survives a live-pull append ──


def test_spot_intraday_anchor_survives_live_pull_append(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """A lake append between the movers snapshot and the probe must NOT red.

    The movers payload is generation-cached; a direct lake write (a CLI pull
    mid-flight) moves /db immediately but not the movers bytes. The spot
    Latest anchor (latest_time) must pin the /db cut to the snapshot's own
    minute — a red here is a FALSE red on a matching view.
    """
    fixture = build_movers_app(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _switch_interval(session.page, "1minute")
    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")
    # The anchored source line names the resolved latest minute (09:17).
    expect(session.page.locator("#asof")).to_contain_text("latest (09:17)")

    # "Live pull": a NEWER 1-minute bar lands in the lake (no ledger touch →
    # the movers generation cache still serves the 09:17 snapshot).
    movers_write(
        tmp_path,
        "NIFTY",
        "1minute",
        [_min_bar(hour=9, minute=30, close="30000", volume=1)],
    )

    # ↻ Refresh → byte-identical movers payload (skip path) → re-verify.
    # /db now holds the 09:30 bar; the anchor must cut back to 09:17.
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator("#refresh").click()
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")
    expect(chip).to_have_text(re.compile(r"^✓ matches /db"))
    assert session.page_errors == []


# ── S2: explicit time pick — the anchored cut at a mid-session minute ──


def test_spot_explicit_time_pick_verifies_the_picked_minute(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """Explicit 09:16 pick: LTP is the 09:16 close on BOTH sides → green."""
    fixture = build_movers_app(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _switch_interval(session.page, "1minute")
    _pick_time(session.page, "09:16")
    # The NIFTY row now shows the 09:16 close, not the day's last.
    expect(session.page.locator("#rows td.name").first).to_be_visible()
    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")
    expect(chip).to_have_text(re.compile(r"^✓ matches /db"))
    assert session.page_errors == []


# ── S3: flow-level %CHNG — green arm (prior-day 1m base present) ──


def _seed_prior_day_minutes(tmp_path: Path) -> None:
    """Prior-day 1m bars whose last close EQUALS the 1day partition close."""
    movers_write(
        tmp_path,
        "NIFTY",
        "1minute",
        [
            _min_bar(hour=15, minute=29, close="20000", volume=50, day=_PREV_DAY),
        ],
    )


def test_pct_chng_compares_green_when_baselines_agree(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """With a prior-day 1m base equal to the 1day close, %CHNG checks green."""
    fixture = _build_app_with(tmp_path, seed=_seed_prior_day_minutes)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _switch_interval(session.page, "1minute")
    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")
    assert session.page_errors == []


def _seed_prior_day_minutes_diverged(tmp_path: Path) -> None:
    """Prior-day 1m last close 20002 ≠ the 1day partition close 20000."""
    movers_write(
        tmp_path,
        "NIFTY",
        "1minute",
        [
            _min_bar(hour=15, minute=29, close="20002", volume=50, day=_PREV_DAY),
        ],
    )


def test_pct_chng_red_when_1day_and_1m_baselines_diverge(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """movers %CHNG (1day baseline) vs /db %CHNG (1m baseline) divergence → red."""
    fixture = _build_app_with(tmp_path, seed=_seed_prior_day_minutes_diverged)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _switch_interval(session.page, "1minute")
    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("neg")
    expect(chip).to_have_text(re.compile(r"✗ /db MISMATCH — NIFTY %CHNG shown 10\.00 · /db 9\.99"))
    assert session.page_errors == []


# ── S4: 10 s timeout → honest "verify unavailable (timeout)" ──


def test_probe_timeout_reads_verify_unavailable_timeout(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    fixture = build_movers_app(tmp_path)
    session = new_page(fixture, expect_console_clean=False)
    freeze_page_clock(session.page)
    held: list[Any] = []

    def _hold(route: Any) -> None:
        held.append(route)

    session.page.route(_DB_CANDLES_GLOB, _hold)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)  # debounce → probes now pending
    expect(chip).to_have_text(re.compile(r"^verify: checking top "))
    session.page.clock.fast_forward(10_000)  # the 10 s killer aborts the pass
    expect(chip).to_have_text("verify unavailable (timeout)")
    expect(chip).to_have_class("muted")
    for route in held:
        route.abort()
    assert session.page_errors == []


# ── S5: a READABLE special-char symbol ("M&M") probes successfully ──


def _seed_m_and_m(tmp_path: Path) -> None:
    movers_write(
        tmp_path,
        "M&M",
        "1minute",
        [
            _min_bar(hour=9, minute=15, close="2800", volume=10),
            _min_bar(hour=9, minute=17, close="2856", volume=20),
        ],
    )
    movers_write(
        tmp_path,
        "M&M",
        "1day",
        [
            _day_bar(day=_PREV_DAY, open_="2790", close="2800", volume=10),
            _day_bar(day=MOVERS_DAY_DATE, open_="2800", close="2856", volume=30),
        ],
    )


def test_ampersand_symbol_probes_encoded_and_green(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """ "M&M" is /db-readable: the probe URL-encodes '&' and verifies green."""
    fixture = _build_app_with(tmp_path, extra_symbols=["M&M"], seed=_seed_m_and_m)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")
    # NIFTY + M&M probed (XSS row disclosed unreadable) — the M&M probe rode
    # URLSearchParams encoding, never a raw '&' that would truncate the query.
    expect(chip).to_have_text(re.compile(r"\(2 rows"))
    assert session.request_count("symbol=M%26M") == 1
    assert session.page_errors == []


# ── S6: the FUTURES tab — segment=FNO, LTP-only verify ──


def _seed_future(tmp_path: Path) -> None:
    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-27Jun24-FUT",
        interval="1minute",
        bars=[
            _fno_bar(hour=15, minute=29, close="20050", oi=1000, day=_PREV_DAY),
            _fno_bar(hour=9, minute=15, close="22010", oi=1200),
            _fno_bar(hour=9, minute=17, close="22060", oi=1300),
        ],
    )


def test_futures_tab_verifies_ltp_only_green(new_page: NewPageFactory, tmp_path: Path) -> None:
    fixture = _build_app_with(tmp_path, seed=_seed_future)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator('.tab[data-asset="futures"]').click()
    session.page.wait_for_selector("#rows td.name")
    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")
    expect(chip).to_have_text(re.compile(r"F&O LTP only"))
    assert session.request_count("segment=FNO") >= 1
    assert session.page_errors == []


# ── S7: the OPTIONS tab — segment=FNO, LTP-only verify ──


def _seed_option(tmp_path: Path) -> None:
    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-27Jun24-22000-CE",
        interval="1minute",
        bars=[
            _fno_bar(hour=15, minute=29, close="120.50", oi=5000, day=_PREV_DAY),
            _fno_bar(hour=9, minute=15, close="150.05", oi=5500),
            _fno_bar(hour=9, minute=17, close="180.45", oi=6000),
        ],
    )


def test_options_tab_verifies_ltp_only_green(new_page: NewPageFactory, tmp_path: Path) -> None:
    fixture = _build_app_with(tmp_path, seed=_seed_option)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator('.tab[data-asset="options"]').click()
    session.page.wait_for_selector("#rows td.name")
    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")
    expect(chip).to_have_text(re.compile(r"F&O LTP only"))
    assert session.request_count("segment=FNO") >= 1
    assert session.page_errors == []
