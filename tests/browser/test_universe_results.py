"""/universe + /results browser truth: renders, XSS neutralization, formatter law.

Four real-Chromium proofs (DEC-DASHBOARD-BROWSER-TRUTH-001):

* **/universe render + XSS** — the seeded instrument rows render from the REAL
  server payload, and a hostile ``<script>``-bearing display name
  (:data:`~tests.browser._fixtures.XSS_NAME2`) lands as INERT TEXT in the
  ``.nm`` cell: raw-string ``textContent``, zero injected elements, no
  ``window.__bx_xss`` stamp, zero pageerrors/dialogs.
* **/results render** — the leaderboard renders exactly the DISTINCT run_ids
  of the seeded sweep tree (cross-store dedup; the corrupt parquet is skipped,
  never a 500), with the fixture-derived instrument set and the one negative
  P&L cell carrying ``.neg`` + the pinned ``₹-0.01`` boundary display.
* **bxFixed2 golden** — the shipped ROUND_HALF_UP 2dp formatter
  (MIGRATION.md #1510(e)) proven in the REAL loaded ``bx-format.js`` inside
  real Chromium: ``1.005 → "1.01"`` etc., DIVERGING from IEEE
  ``toFixed(2)`` at the half boundaries (the bug the formatter exists to fix),
  plus the documented exponent-notation fallback + non-finite passthrough.
* **rate-widget flow states** — the ONLY test that serves the widget poll
  endpoints itself (``stub_widgets=False``): route-FULFILLED
  ``/api/v1/pull/rate`` payloads drive the REAL ``bx-rate-monitor.js`` dot
  through every ``flow_state`` verdict (``local``/``flowing``/``stalled``/
  ``failing``→stalled/``done``→idle/``idle``), each poll cycle advanced
  deterministically via ``page.clock``.

Honest envelope (Golden Rule #10): NO dedicated client staleness badge exists
anywhere in the dashboard — the flow-state light above IS the real existing
staleness surface, and these tests assert ONLY what the shipped JS does
(``#bxrmDot`` / ``#bxrmState`` classes + text; no invented "dimming").
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest
from playwright.sync_api import Route, expect

from tests.browser._fixtures import (
    XSS_NAME2,
    XSS_SYMBOL2,
    build_db_app,
    build_results_app,
    build_universe_app,
)
from tests.browser.conftest import WIDGET_STUBS, NewPageFactory
from tests.unit.test_surface_trial_results import _SPECS

pytestmark = pytest.mark.browser

#: bx-rate-monitor.js POLL_MS — one virtual-clock advance per widget poll cycle.
_WIDGET_POLL_MS: Final[int] = 3000


# ── /universe: real render + hostile display name stays inert text ────────


def test_universe_renders_and_neutralizes_hostile_display_name(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """Both seeded rows render; the ``<script>`` display name is inert text.

    The hostile string flowed through the REAL store → REAL
    ``/api/v1/universe/instruments`` payload → the ``.nm`` innerHTML sink
    (universe.html renderInstruments, esc()-guarded). A broken escape would
    inject a ``script`` element into the cell and/or diverge the
    ``textContent`` from the raw string — both tripwires asserted, plus the
    ``window.__bx_xss`` execution stamp and the pageerror/dialog ledgers.
    """
    session = new_page(build_universe_app(tmp_path))
    session.goto("/universe")
    page = session.page
    page.wait_for_selector("#instruments .inst")
    expect(page.locator("#instruments .inst")).to_have_count(2)

    nifty = page.locator('#instruments .inst[data-symbol="NIFTY"]')
    assert nifty.locator(".sym").text_content() == "NIFTY"
    assert nifty.locator(".nm").text_content() == "NIFTY 50"

    hostile = page.locator(f'#instruments .inst[data-symbol="{XSS_SYMBOL2}"]')
    assert hostile.locator(".sym").text_content() == XSS_SYMBOL2
    # The raw hostile string IS the text — escaped exactly once, never parsed.
    assert hostile.locator(".nm").text_content() == XSS_NAME2
    # No element was injected into the name cell (esc() left text only) and no
    # script/img landed anywhere in the hostile row.
    assert hostile.locator(".nm *").count() == 0
    assert hostile.locator("script").count() == 0
    assert hostile.locator("img").count() == 0
    # The payload's execution stamp never fired.
    assert page.evaluate("() => window.__bx_xss") is None
    assert session.page_errors == []
    assert session.dialogs == []


# ── /results: leaderboard renders the deduped seeded rows ─────────────────


def test_results_leaderboard_renders_deduped_seeded_rows(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """The leaderboard shows EXACTLY the distinct seeded run_ids, cell-checked.

    The fixture tree holds a cross-store duplicate run_id (counted once) and
    a corrupt parquet (skipped, never a 500) — so the row count equals the
    DISTINCT run_ids in the seed specs. The instrument cells and the single
    negative-P&L cell (net −1 paisa → ``₹-0.01`` with ``.neg``) are derived
    from the SAME ``_SPECS`` source the fixture was seeded from, so the DOM
    is compared against an independent ground truth, not itself.
    """
    fixture = build_results_app(tmp_path)
    session = new_page(fixture)
    session.goto("/results")
    page = session.page
    page.wait_for_selector("#panel table tbody tr")

    expect(page.locator("#panel table tbody tr")).to_have_count(fixture.ground["row_count"])

    instrument_cells = page.locator("#panel table tbody td .sym").all_text_contents()
    assert len(instrument_cells) == fixture.ground["row_count"]
    assert set(instrument_cells) == {spec["instrument"] for spec in _SPECS}
    expected_banknifty = sum(1 for spec in _SPECS if spec["instrument"] == "BANKNIFTY")
    assert instrument_cells.count("BANKNIFTY") == expected_banknifty

    # Exactly the seeded loss rows paint red; the −1-paisa boundary displays
    # as the pinned 2dp money form (DEC-MONEY-2DP-001 / #1154 boundary cell).
    negative = page.locator("#panel table tbody td .pnl.neg")
    expect(negative).to_have_count(sum(1 for spec in _SPECS if spec["net_pnl_paisa"] < 0))
    assert negative.first.text_content() == "₹-0.01"
    assert session.page_errors == []
    assert session.dialogs == []


# ── bxFixed2: ROUND_HALF_UP proven in the real loaded JS ──────────────────


def test_bx_fixed2_round_half_up_golden_in_real_chromium(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """The shipped ``bx-format.js`` rounds half-up where ``toFixed`` does not.

    Runs the golden vector through the REAL ``bxFixed2`` global loaded by
    /db (never a re-implementation), and pins the IEEE ``toFixed(2)``
    divergence at the exact half boundaries — the shipped display bug
    MIGRATION.md #1510(e) fixed ('1.005' must read "1.01", not "1.00").
    """
    session = new_page(build_db_app(tmp_path))
    session.goto("/db")
    page = session.page
    page.wait_for_selector("#tables .tbl")
    assert page.evaluate("() => typeof bxFixed2") == "function"

    got = page.evaluate(
        "() => ["
        "bxFixed2(1.005), bxFixed2(2.675), bxFixed2(-1.005), bxFixed2(-0.001),"
        "bxFixed2(0), bxFixed2(1.0049999), bxFixed2(10.005)"
        "]"
    )
    assert got == ["1.01", "2.68", "-1.01", "-0.00", "0.00", "1.00", "10.01"]

    # The IEEE half-even-on-binary divergence bxFixed2 exists to fix: the SAME
    # inputs through Number.prototype.toFixed read one paisa LOWER.
    ieee = page.evaluate("() => [(1.005).toFixed(2), (2.675).toFixed(2)]")
    assert ieee == ["1.00", "2.67"]

    # Documented honest fallback: exponent-notation String() forms defer to
    # toFixed (>= 1e21 above any display cell's domain; < 1e-6 agrees "0.00").
    fallback = page.evaluate(
        "() => [bxFixed2(1e21), (1e21).toFixed(2), bxFixed2(1e-7), (1e-7).toFixed(2)]"
    )
    assert fallback[0] == fallback[1]
    assert fallback[2] == fallback[3] == "0.00"

    # Non-finite passthrough (String() form, never a throw into the page).
    assert page.evaluate("() => [bxFixed2(0/0), bxFixed2(1/0), bxFixed2(-1/0)]") == [
        "NaN",
        "Infinity",
        "-Infinity",
    ]


# ── rate widget: route-fulfilled flow_state payloads drive the real dot ───


def _fulfill_json(body: str) -> Callable[[Route], None]:
    """A page.route handler that answers 200 JSON with *body* (fixed)."""

    def handle(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=body)

    return handle


def test_rate_widget_flow_state_drives_dot_classes(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """Every ``flow_state`` verdict paints the real ``#bxrmDot``/``#bxrmState``.

    The ONLY test where the widget endpoints are NOT stubbed inert
    (``stub_widgets=False``): this test route-fulfills ``/api/v1/pull/rate``
    itself with a test-owned ``flow_state`` and advances the widget's 3 s poll
    deterministically via ``page.clock`` — each cycle barriered on the actual
    response (payload-driven, zero wall-clock waits).

    Proven against the REAL ``bx-rate-monitor.js`` setClass law: ``local``
    (amber), ``flowing`` (green), ``stalled`` (red), ``failing`` → the red
    *stalled* styling with the honest backend word as text, ``done``/unknown →
    the dark *idle* styling, and a served ``idle``. The first served verdict
    is ``local`` (≠ the widget's built-in initial class) so the initial poll's
    round-trip is an observable render barrier, not an assumption.
    """
    session = new_page(build_universe_app(tmp_path), stub_widgets=False)
    page = session.page

    flow = {"state": "local"}

    def serve_rate(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"flow_state": flow["state"]}),
        )

    # Page-level fulfills, registered BEFORE goto: the rate payload is
    # test-sequenced; the other two pollers reuse the harness's inert stubs.
    page.route("**/api/v1/pull/rate", serve_rate)
    for path in ("/api/v1/universe/pull-progress", "/api/v1/liveproof"):
        page.route(f"**{path}", _fulfill_json(json.dumps(WIDGET_STUBS[path])))

    page.clock.install()
    session.goto("/universe")
    page.wait_for_selector("#bxrmDot")

    # Initial poll (fired at widget start, no timer needed) rendered 'local'.
    expect(page.locator("#bxrmDot")).to_have_class("bxrm-dot local")
    expect(page.locator("#bxrmState")).to_have_text("local")
    expect(page.locator("#bxrmState")).to_have_class("bxrm-state local")

    # (served payload state, expected styling class) — the setClass law.
    steps = [
        ("flowing", "flowing"),
        ("stalled", "stalled"),
        ("failing", "stalled"),  # issuing-but-all-failing takes the RED styling
        ("done", "idle"),  # pull just finished → dark idle styling
        ("idle", "idle"),
    ]
    for state, styling in steps:
        flow["state"] = state
        with page.expect_response("**/api/v1/pull/rate"):
            page.clock.fast_forward(_WIDGET_POLL_MS)
        expect(page.locator("#bxrmDot")).to_have_class(f"bxrm-dot {styling}")
        expect(page.locator("#bxrmState")).to_have_text(state)
        expect(page.locator("#bxrmState")).to_have_class(f"bxrm-state {styling}")

    assert session.request_count("/api/v1/pull/rate") == 1 + len(steps)
    assert session.page_errors == []
    assert session.dialogs == []
