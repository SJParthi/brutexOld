"""Round-6 refuter CROSS-FEATURE probes (PR #1358 club vehicle).

The four features (freshness guard #1632, time-snap #1633, provenance/verify
#1634, header chips #1635) coexist on one page — these probes execute the
interaction seams no per-feature refuter could see:

* R6-P1 — a STALE stored 3minute partition (the #1632 reachable class) under
  an EXPLICIT bucket-final time pick (#1633's sent value rides the stored-first
  path via _stored_serves_cutoff alignment): the guard must serve the FRESH
  derive, the as-of label must show the bucket SPAN, the provenance line must
  count the fallback under derived-from-1m, and the verify chip (#1634) must
  land GREEN against the 1m base — a red here (or a stale ₹ on screen) is a
  confirmed cross-feature defect.
* R6-P2 — a STALE stored 1day bar (the #1632 ledger repro shape) on the BOOT
  default daily view (fold-in C default date): fresh values + green chip +
  honest read-mode counts.
* R6-P3 — an EXPLICIT time pick must SURVIVE an expiry-TAB switch (#1635 seg
  tabs write the hidden select and call load() directly): the Next-tier query
  must still carry ?time=, the Expiry chip must re-label for the new tier from
  the NEW payload, and the verify pass must re-prove GREEN for the tier view.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import re
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.prebuild_gate import PrebuildGate
from brutex.data.parquet_writer import ParquetWriter

from tests.browser._fixtures import XSS_NAME, AppFixture, _open_store, build_movers_app
from tests.browser._guards import browser_suite_enabled
from tests.browser.conftest import NewPageFactory, freeze_page_clock
from tests.browser.test_refuter_r3_probes import _fno_bar, _pick_time, _switch_interval
from tests.integration.test_dashboard_movers import _DAY as MOVERS_DAY_DATE
from tests.integration.test_dashboard_movers import _day_bar, _idx, _min_bar
from tests.integration.test_dashboard_movers import _write as movers_write

if not browser_suite_enabled():
    pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect

pytestmark = pytest.mark.browser

_PREV_DAY = date(2024, 6, 12)
_CHIP = "#verifyChip"
_MOVERS_GLOB = "**/api/v1/movers**"

_SNAPSHOT_DATE = date(2024, 1, 2)
_RUN_ID = "refuter-r6"
_CSV_SHA = "0" * 64


def _rebuild_app(tmp_path: Path, extra_symbols: list[str] | None = None) -> AppFixture:
    """build_movers_app + extra INDEX rows, store reopened after extra seeding."""
    fixture = build_movers_app(tmp_path)
    fixture.cleanup()
    store = _open_store(tmp_path)
    rows = [_idx(s) for s in ("NIFTY", "BANKNIFTY", *(extra_symbols or []))]
    store.upsert_many(
        rows, snapshot_date=_SNAPSHOT_DATE, run_id=_RUN_ID, csv_source_sha256=_CSV_SHA
    )
    gate = PrebuildGate()
    app = build_app(DashboardState(), instrument_store=store, prebuild_gate=gate)
    ground = dict(fixture.ground)
    ground["gate"] = gate
    return AppFixture(app=app, ground=ground, cleanup=store.close)


def _reseed_fresh_1day_midnight(tmp_path: Path, symbols: list[str], *, day: date) -> None:
    """RE-SEED (not append) each FRESH symbol's stored ``1day`` partition so the
    daily-freshness guard (DEC-MOVERS-DAILY-FRESHNESS-001, MIGRATION.md #1931)
    serves it ``stored``.

    The shared movers fixture writes its 1-day bars stamped at 09:15 IST
    (``test_dashboard_movers._day_bar``), while #1931's verify-equality guard
    compares each stored day-D bar against the PRODUCTION midnight-IST
    derivation (``resample_1m_to_daily``) — so every fixture day-D bar reads
    ``stale`` and the read-mode count degrades to ``0 stored / N derived``.
    Re-writing day-D through the SAME production derivation (byte-equal to the
    guard's own re-derive) flips those symbols to ``stored``; the prior-day
    baseline row is preserved verbatim for the prev-close (the 1-minute base
    holds only day-D, so it is never itself re-derived).
    """
    import polars as pl

    from brutex.dashboard.movers_snapshot import (
        _read_cut_1m_frame,
        _read_daily_partition,
        _row_to_bar,
    )
    from brutex.data.spot_daily_resample import resample_1m_to_daily

    for symbol in symbols:
        base = _read_cut_1m_frame(
            tmp_path, exchange="NSE", segment="INDEX", symbol=symbol, day=day, cutoff_min=None
        )
        assert base is not None, f"no 1-minute base for {symbol!r}"
        # The production midnight-IST derivation — byte-equal to what #1931's
        # guard re-derives at read time, so the stored day-D bar compares equal.
        derived = resample_1m_to_daily(base, before=day + timedelta(days=1))
        cur = _read_daily_partition(tmp_path, exchange="NSE", segment="INDEX", symbol=symbol)
        prior_bars = []
        if cur is not None:
            prior = (
                cur.filter(
                    pl.col("timestamp").dt.convert_time_zone("Asia/Kolkata").dt.date() != day
                )
                .sort("timestamp")
                .rename({"timestamp": "ts_utc"})
            )
            prior_bars = [_row_to_bar(r) for r in prior.iter_rows(named=True)]
        # Full RE-SEED (never append): a keep-first merge would keep the stale
        # 09:15-IST day-D bar, so drop the partition and rewrite it.
        part_dir = tmp_path / "bars" / "NSE" / "INDEX" / symbol / "1day"
        if part_dir.exists():
            shutil.rmtree(part_dir)
        movers_write(tmp_path, symbol, "1day", prior_bars + derived)


# ── R6-P1: stale stored 3minute × explicit bucket-final pick × verify ──


def test_r6_stale_stored_tf_explicit_pick_serves_fresh_and_green(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    fixture = build_movers_app(tmp_path)
    # STALE stored 3minute partition for NIFTY: the 09:15 bucket close reads
    # 21365 while the raw 1m base says 22000 (the #1632 CLI same-day-growth
    # class). Written AFTER the base fixture so it is the partition's only row.
    movers_write(
        tmp_path,
        "NIFTY",
        "3minute",
        [_min_bar(hour=9, minute=15, close="21365", volume=700)],
    )
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    _switch_interval(session.page, "3minute")
    # Explicit pick: the 09:15-labeled bucket carries the bucket-FINAL sent
    # value 09:17 (feature #1633's mapping law).
    _pick_time(session.page, "09:17")

    # Feature #1632 × #1633: the stored-first path IS consulted (bucket-final
    # aligned cutoff) — the freshness guard must fall back to the fresh derive.
    row = session.page.locator("#rows tr").filter(has_text="₹22,000.00")
    expect(row).to_have_count(1)
    expect(row.locator("td.name")).to_have_text("NIFTY")
    expect(session.page.locator("#rows")).not_to_contain_text("21,365")

    # Feature #1633: the as-of label shows the containing-bucket SPAN.
    expect(session.page.locator("#asof")).to_contain_text("09:15–09:17")

    # Feature #1634: the provenance line counts the fallback honestly.
    expect(session.page.locator("#srcText")).to_contain_text(
        "reads: 0 stored-TF / 3 derived-from-1m"
    )

    # Feature #1634: the verify chip proves the FRESH values against /db 1m.
    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")
    expect(chip).to_have_text(re.compile(r"^✓ matches /db"))
    assert session.page_errors == []


# ── R6-P2: stale stored 1day × boot default daily view × verify ──


def test_r6_stale_stored_1day_boot_daily_serves_fresh_and_green(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    def _seed(tmp: Path) -> None:
        movers_write(
            tmp,
            "STALEDAY",
            "1minute",
            [
                _min_bar(hour=9, minute=15, close="30000", volume=10),
                _min_bar(hour=9, minute=16, close="31000", volume=10),
                _min_bar(hour=9, minute=17, close="33000", volume=10),
            ],
        )
        movers_write(
            tmp,
            "STALEDAY",
            "1day",
            [
                _day_bar(day=_PREV_DAY, open_="29900", close="30000", volume=30),
                # STALE: close 31700 ≠ the 1m-derived 33000 (the #1632 repro shape).
                _day_bar(day=MOVERS_DAY_DATE, open_="30000", close="31700", volume=30),
            ],
        )

    _seed(tmp_path)
    fixture = _rebuild_app(tmp_path, extra_symbols=["STALEDAY"])
    # RE-SEED the FRESH symbols' 1day partitions via the production midnight-IST
    # derivation so #1931 serves them ``stored`` (the 3 stored-TF reads); only
    # STALEDAY stays divergent (stored close 31700 ≠ the 1m-derived 33000), the
    # single derived-from-1m read.
    _reseed_fresh_1day_midnight(
        tmp_path, ["NIFTY", "BANKNIFTY", XSS_NAME], day=MOVERS_DAY_DATE
    )
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")

    # The daily boot view must serve the CANONICAL re-derive (33000), never the
    # stale stored close (31700).
    row = session.page.locator("#rows tr").filter(has_text="₹33,000.00")
    expect(row).to_have_count(1)
    expect(row.locator("td.name")).to_have_text("STALEDAY")
    expect(session.page.locator("#rows")).not_to_contain_text("31,700")
    # +10% vs the prior 1day close 30000 — computed off the FRESH close.
    expect(row.locator("td").nth(3)).to_have_text("+10.00%")

    # Read-mode honesty: 3 stored (NIFTY/BANKNIFTY/XSS fresh) + 1 derived.
    expect(session.page.locator("#srcText")).to_contain_text(
        "reads: 3 stored-TF / 1 derived-from-1m"
    )

    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")
    expect(chip).to_have_text(re.compile(r"^✓ matches /db"))
    assert session.page_errors == []


# ── R6-P3: explicit time pick survives an expiry-TAB switch (options) ──


def _seed_two_tier_options(tmp_path: Path) -> None:
    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-27Jun24-22000-CE",
        interval="1minute",
        bars=[
            _fno_bar(hour=15, minute=29, close="120.50", oi=5000, day=_PREV_DAY),
            _fno_bar(hour=9, minute=15, close="150.05", oi=5500),
            _fno_bar(hour=9, minute=16, close="160.25", oi=5700),
            _fno_bar(hour=9, minute=17, close="180.45", oi=6000),
        ],
    )
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-25Jul24-22000-CE",
        interval="1minute",
        bars=[
            _fno_bar(hour=15, minute=29, close="295.00", oi=900, day=_PREV_DAY),
            _fno_bar(hour=9, minute=15, close="300.00", oi=1000),
            _fno_bar(hour=9, minute=16, close="310.00", oi=1100),
            _fno_bar(hour=9, minute=17, close="320.00", oi=1200),
        ],
    )


def test_r6_expiry_tab_switch_keeps_explicit_time_pick(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    _seed_two_tier_options(tmp_path)
    fixture = _rebuild_app(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator('.tab[data-asset="options"]').click()
    session.page.wait_for_selector("#rows td.name")
    _switch_interval(session.page, "1minute")
    _pick_time(session.page, "09:16")
    # Near tier at the 09:16 cut: the near contract's 09:16 close.
    expect(session.page.locator("#rows")).to_contain_text("₹160.25")

    # Feature #1635 × #1633: the expiry TAB click must carry the pick along.
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator('#expiryTabs .segbtn[data-val="Next"]').click()
    next_reqs = [u for u in session.requests if "/api/v1/movers" in u and "expiry=Next" in u]
    assert len(next_reqs) == 1, f"expected exactly one Next-tier request, got {next_reqs}"
    assert "time=09%3A16" in next_reqs[0], (
        f"the explicit time pick was DROPPED by the expiry tab switch: {next_reqs[0]}"
    )

    # The Next-tier view at the 09:16 cut (310.00), never the near row.
    expect(session.page.locator("#rows")).to_contain_text("₹310.00")
    expect(session.page.locator("#rows")).not_to_contain_text("₹160.25")
    # The Expiry chip re-labels from the NEW payload + the LIVE tier word.
    expect(session.page.locator("#chipExpiry")).to_contain_text("25-Jul-2024 (next tier)")
    # The as-of label keeps the explicit pick.
    expect(session.page.locator("#asof")).to_contain_text("09:16")

    # Feature #1634: the verify pass re-proves the tier view green.
    chip = session.page.locator(_CHIP)
    session.page.clock.fast_forward(200)
    expect(chip).to_have_class("pos")
    expect(chip).to_have_text(re.compile(r"F&O LTP only"))

    # EMPTY tier composition (Far — no third expiry): honest chips + n/a chip
    # + a provenance line without a deep link, never a stale Next-tier paint.
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator('#expiryTabs .segbtn[data-val="Far"]').click()
    session.page.wait_for_selector("#rows td.empty")
    expect(session.page.locator("#chips")).to_be_visible()
    expect(session.page.locator("#chipExpiry")).to_contain_text("far tier")
    expect(session.page.locator("#src")).to_be_visible()
    expect(session.page.locator("#srcLinkWrap")).to_be_hidden()
    session.page.clock.fast_forward(200)
    expect(chip).to_have_text("verify: n/a (no rows)")
    assert session.page_errors == []


# ── R6-P4: 5xx error path — chips + provenance + verify chip all cleared ──


def test_r6_server_error_clears_chips_provenance_and_verify(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    _seed_two_tier_options(tmp_path)
    fixture = _rebuild_app(tmp_path)
    # The routed 500 itself logs a console "Failed to load resource" — inherent
    # to the scenario (the r3 timeout-probe opt-out precedent), so the
    # console-clean law is waived; pageerror/dialog cleanliness still asserted.
    session = new_page(fixture, expect_console_clean=False)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator('.tab[data-asset="options"]').click()
    session.page.wait_for_selector("#rows td.name")
    # The full composition is painted: chips + src line + a settled chip.
    expect(session.page.locator("#chips")).to_be_visible()
    expect(session.page.locator("#src")).to_be_visible()
    session.page.clock.fast_forward(200)
    expect(session.page.locator(_CHIP)).to_have_class("pos")

    # Route the NEXT movers request to a 500 (the PCR lake-corruption shape).
    def _boom(route: Any) -> None:
        route.fulfill(
            status=500,
            content_type="application/json",
            body='{"detail": "lake corruption: negative OI (underlying NIFTY)"}',
        )

    session.page.route(_MOVERS_GLOB, _boom)
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator("#refresh").click()
    session.page.wait_for_selector("#rows td.empty")
    # Every feature surface is cleared — no stale chips / provenance / verdict.
    expect(session.page.locator("#chips")).to_be_hidden()
    expect(session.page.locator("#chipDetail")).to_be_hidden()
    expect(session.page.locator("#src")).to_be_hidden()
    expect(session.page.locator("#pcrPanel")).to_be_hidden()
    expect(session.page.locator("#asof")).to_have_text("server error · HTTP 500")
    expect(session.page.locator("#noteText")).to_contain_text("lake corruption")

    # Recovery: unroute → refresh repaints chips + src + a green chip.
    session.page.unroute(_MOVERS_GLOB)
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator("#refresh").click()
    session.page.wait_for_selector("#rows td.name")
    expect(session.page.locator("#chips")).to_be_visible()
    expect(session.page.locator("#src")).to_be_visible()
    session.page.clock.fast_forward(200)
    expect(session.page.locator(_CHIP)).to_have_class("pos")
    assert session.page_errors == []


# ── R6-P5: gated flip hides the provenance line + verify chip too ──


def test_r6_gated_flip_hides_provenance_and_verify_chip(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    _seed_two_tier_options(tmp_path)
    fixture = _rebuild_app(tmp_path)
    session = new_page(fixture)
    freeze_page_clock(session.page)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator('.tab[data-asset="options"]').click()
    session.page.wait_for_selector("#rows td.name")
    expect(session.page.locator("#src")).to_be_visible()
    session.page.clock.fast_forward(200)
    expect(session.page.locator(_CHIP)).to_have_class("pos")

    gate: PrebuildGate = fixture.ground["gate"]
    gate.begin()
    gate.set_phase("prebuild")
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator("#refresh").click()
    session.page.wait_for_selector("#rows td.empty")
    # The gated shell must not keep a stale provenance line / verify verdict.
    expect(session.page.locator("#src")).to_be_hidden()
    expect(session.page.locator("#chips")).to_be_hidden()
    gate.release(ok=True)
    session.page.clock.fast_forward(5_000)
    session.page.wait_for_selector("#rows td.name")
    expect(session.page.locator("#src")).to_be_visible()
    session.page.clock.fast_forward(200)
    expect(session.page.locator(_CHIP)).to_have_class("pos")
    assert session.page_errors == []
