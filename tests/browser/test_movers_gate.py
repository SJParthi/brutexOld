"""Movers prebuild-gate truth tests: paint-once, timer-disarm, recovery, XSS.

Real-DOM proofs (DEC-DASHBOARD-BROWSER-TRUTH-001, design decisions 3/4/5/10)
against the fixture-backed REAL ``build_app`` app with a LIVE injected
:class:`~brutex.dashboard.prebuild_gate.PrebuildGate` (same process — the test
drives ``begin``/``set_phase``/``set_progress``/``release`` server-side while
Chromium renders):

* **Paint-once** — while gated, ``gate.set_progress(1, 9)`` changes ONLY the
  ``#asof`` warm clause (``movers warm 1/9``); the gated-shell key
  (``asset|phase|note``) is unchanged, so the shell painted at first load is
  NOT rebuilt — proven by an expando probe stamped on the shell's empty row
  surviving the retry (and provably DESTROYED by the recovery repaint, so the
  probe has teeth).
* **Self-recovery** — the page clock is installed AND PAUSED before load
  (:func:`~tests.browser.conftest.freeze_page_clock` — ``install()`` alone
  keeps ticking in real time, so a loaded-runner stall >= 5 s would fire a
  phantom retry and break the exact ledger asserts); the 5 s retry is driven
  deterministically via ``page.clock.fast_forward(5000)`` wrapped in
  ``page.expect_response`` (no wall-clock waits); after ``gate.release(
  ok=True)`` the next retry renders the real ranked rows (anti-vacuity: zero
  ``.preview-badge`` — the JS catch-branch paints SAMPLE rows with preview
  badges, so a crashed render can never fake a pass) with the pinned
  NIFTY/BANKNIFTY cells.
* **Timer-disarm** — after recovery a DEAD 6 s clock advance fires NO movers
  request, and the ↻ Refresh click's response round-trip is the ordering
  barrier: the movers ledger grows by EXACTLY one (a still-armed gateTimer
  would give +2).
* **Timer-disarm on a direct-load race** (round-3 review, mutation B2) — a
  NON-timer-driven load (↻ Refresh) resolving non-gated while the 5 s gate
  retry is STILL PENDING must ``clearTimeout`` it; without the non-gated
  branch's disarm the stale timer fires a redundant duplicate load
  (dedicated test — the recovery flow above consumes the timer by firing it,
  so it cannot see this race).
* **XSS inertness** — the lake-legal hostile symbol (:data:`XSS_NAME`,
  HTML-entity smuggling arm + single-quote attribute breakout with a real
  event handler) flows through the REAL server payload into the name cell as
  INERT TEXT: ``textContent`` equals the raw string, the cell has no element
  children, hovering never stamps ``window.__bx_xss``, and no
  pageerror/dialog fires. The entity arm is the MUTATION TEETH: dropping the
  ``esc()`` escape from the renderRows sink decodes ``&lt;``/``&gt;`` and the
  ``textContent`` assert fails (an escape-drop mutation is verified KILLED).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from brutex.dashboard.prebuild_gate import PrebuildGate

from tests.browser._fixtures import XSS_NAME, build_movers_app
from tests.browser._guards import browser_suite_enabled
from tests.browser.conftest import NewPageFactory, freeze_page_clock

if not browser_suite_enabled():
    # Direct-file invocation outside the opt-in (collect_ignore governs only
    # directory-shaped collection): a playwright-less box gets an HONEST skip
    # here instead of a collection ImportError; the conftest modifyitems hook
    # skips the collected items. With the suite ENABLED a missing playwright
    # stays a LOUD import error — never a silent green skip in CI.
    pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect

pytestmark = pytest.mark.browser

#: URL glob for ``page.expect_response`` + ledger substring for request counts.
_MOVERS_GLOB = "**/api/v1/movers**"
_MOVERS_KEY = "/api/v1/movers"

#: movers.html gated status (phase set, no warm progress reported):
#: ``final stage: <phase> — running <elapsed> (auto-refreshes)``.
_GATED_STATUS = re.compile(r"^final stage: prebuild — running \d+s \(auto-refreshes\)$")

#: Same line once ``set_progress(1, 9)`` reports — ONLY the warm clause is new.
_GATED_STATUS_WARM = re.compile(
    r"^final stage: prebuild — movers warm 1/9 — running \d+s \(auto-refreshes\)$"
)

#: The gated server note names the ACTUAL phase (gated_movers_payload).
_GATED_NOTE_PREFIX = re.compile(
    r"^pull pipeline running \(current stage: prebuild\) — "
    r"movers stay gated until the final prebuild finishes"
)

#: Stamp / read the paint-once expando probe on the gated shell's empty row.
_STAMP_PROBE = "() => { document.querySelector('#rows tr').__bx_probe = 1; }"
_READ_PROBE = "() => document.querySelector('#rows tr').__bx_probe"


def test_gate_paint_once_recovery_and_timer_disarm(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """Gated shell paints once, recovers by itself, then disarms its timer."""
    fixture = build_movers_app(tmp_path)
    session = new_page(fixture)
    gate: PrebuildGate = fixture.ground["gate"]
    gate.begin()
    gate.set_phase("prebuild")
    # Freeze timer scheduling BEFORE load — install AND PAUSE the fake clock
    # (install() alone keeps ticking in real time, so a wall-clock stall
    # >= 5 s between fast_forwards would fire a phantom gate retry and break
    # the ==N ledger asserts). The 5 s retry then fires ONLY under
    # fast_forward — every wait below is payload/DOM-driven.
    freeze_page_clock(session.page)
    session.goto("/movers")

    # 1) Gated shell: honest status + note, one empty row, probe stamped.
    asof = session.page.locator("#asof")
    note = session.page.locator("#noteText")
    expect(asof).to_have_text(_GATED_STATUS)
    expect(note).to_have_text(_GATED_NOTE_PREFIX)
    shell_row = session.page.locator("#rows tr")
    expect(shell_row).to_have_count(1)
    expect(shell_row.locator("td.empty")).to_have_count(1)
    session.page.evaluate(_STAMP_PROBE)
    assert session.request_count(_MOVERS_KEY) == 1

    # 2) Paint-once: warm progress changes ONLY the #asof clause; the gated
    #    key (asset|phase|note) is unchanged so the shell is NOT rebuilt.
    gate.set_progress(1, 9)
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.clock.fast_forward(5_000)
    expect(asof).to_have_text(_GATED_STATUS_WARM)
    expect(note).to_have_text(_GATED_NOTE_PREFIX)
    assert session.page.evaluate(_READ_PROBE) == 1, "gated shell was rebuilt (paint-once broken)"
    assert session.request_count(_MOVERS_KEY) == 2

    # 3) Release → the very next auto-retry renders the REAL ranked table.
    gate.release(ok=True)
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.clock.fast_forward(5_000)
    expect(asof).to_have_text(fixture.ground["asof_recovered"])
    expect(note).to_have_text(re.compile("^" + re.escape(fixture.ground["note_prefix"])))
    # Anti-vacuity: the catch-branch paints SAMPLE rows under a preview badge —
    # a crashed recovery render must not fake a pass.
    expect(session.page.locator(".preview-badge")).to_have_count(0)
    # The recovery repaint rebuilds #rows — the probe is GONE, proving its
    # survival in step 2 was a real paint-once signal, not expando inertia.
    assert session.page.evaluate(_READ_PROBE) is None

    # Pinned gainer row: NIFTY LTP ₹22,000.00, pct +10.00% on a .pos cell
    # (spot columns: Name / LTP / Change / Change % / Value (Cr) / Volume).
    nifty_row = session.page.locator("#rows tr").filter(has_text=fixture.ground["nifty_price"])
    expect(nifty_row).to_have_count(1)
    expect(nifty_row.locator("td").nth(1)).to_have_text(fixture.ground["nifty_price"])
    nifty_pct = nifty_row.locator("td").nth(3)
    expect(nifty_pct).to_have_text(fixture.ground["nifty_pct"])
    # Full class-attribute match: directional money cells carry the shared
    # .num broker-grid discipline beside the .pos/.neg tint (bx-design.css).
    expect(nifty_pct).to_have_class("pos num")

    # Pill switch to Price Losers (client-side re-render from the cached
    # payload — no fetch): BANKNIFTY -10.00% on a .neg cell.
    session.page.locator('#pills .pill[data-cat="price_losers"]').click()
    loser_row = session.page.locator("#rows tr").filter(has_text=fixture.ground["banknifty_pct"])
    expect(loser_row).to_have_count(1)
    loser_pct = loser_row.locator("td").nth(3)
    expect(loser_pct).to_have_text(fixture.ground["banknifty_pct"])
    expect(loser_pct).to_have_class("neg num")
    assert session.request_count(_MOVERS_KEY) == 3, "pill switch must not refetch"

    # 4) Timer-disarm: a DEAD 6 s advance fires no movers request; the
    #    ↻ Refresh round-trip is the ordering barrier — request events are
    #    delivered in order, so a phantom timer-fired request would land in
    #    the ledger BEFORE the refresh response and read +2 here, not +1.
    session.page.clock.fast_forward(6_000)
    assert session.request_count(_MOVERS_KEY) == 3
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator("#refresh").click()
    assert session.request_count(_MOVERS_KEY) == 4, "gateTimer still armed after recovery"


def test_gate_timer_disarm_on_direct_load_race(new_page: NewPageFactory, tmp_path: Path) -> None:
    """A direct (non-timer) load landing while a gate retry is pending disarms it.

    Round-3 review: mutation B2 — deleting the NON-gated branch's
    ``clearTimeout(gateTimer)`` (movers.html ``load()``) — SURVIVED
    :func:`test_gate_paint_once_recovery_and_timer_disarm`, because there the
    recovery load arrives VIA the pending gate timer firing, which consumes
    it either way. The disarm's only observable scenario is this race: the
    gate is released server-side and a NON-timer-driven load (the ↻ Refresh
    click — ``$("refresh").onclick = load``) resolves non-gated while the 5 s
    gate retry is still pending. Without the disarm the stale timer later
    fires a redundant duplicate movers load — this test's dead-advance +
    barrier asserts read +2 instead of +1 (mutation B2 is verified KILLED).
    """
    fixture = build_movers_app(tmp_path)
    session = new_page(fixture)
    gate: PrebuildGate = fixture.ground["gate"]
    gate.begin()
    gate.set_phase("prebuild")
    freeze_page_clock(session.page)
    session.goto("/movers")

    # Gated first paint arms the 5 s gate retry; the frozen clock keeps it
    # PENDING (never fired) for the whole race below.
    expect(session.page.locator("#asof")).to_have_text(_GATED_STATUS)
    assert session.request_count(_MOVERS_KEY) == 1

    # Release server-side, then land a DIRECT load via ↻ Refresh while the
    # gate retry is still pending — the non-gated branch must DISARM it.
    gate.release(ok=True)
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator("#refresh").click()
    expect(session.page.locator("#asof")).to_have_text(fixture.ground["asof_recovered"])
    # Anti-vacuity: the real ranked render, not the catch-branch sample rows.
    expect(session.page.locator(".preview-badge")).to_have_count(0)
    assert session.request_count(_MOVERS_KEY) == 2

    # A dead 6 s advance walks PAST the pending retry's 5 s deadline; the
    # next ↻ Refresh round-trip is the ordering barrier — a stale still-armed
    # gate timer (the B2 mutant) fires a redundant load and reads +2, not +1.
    session.page.clock.fast_forward(6_000)
    assert session.request_count(_MOVERS_KEY) == 2, (
        "stale gateTimer fired after a direct non-gated load (clearTimeout disarm broken)"
    )
    with session.page.expect_response(_MOVERS_GLOB):
        session.page.locator("#refresh").click()
    assert session.request_count(_MOVERS_KEY) == 3, (
        "stale gateTimer fired after a direct non-gated load (clearTimeout disarm broken)"
    )


def test_hostile_symbol_renders_inert(new_page: NewPageFactory, tmp_path: Path) -> None:
    """The hostile lake symbol round-trips into the name cell as inert text."""
    fixture = build_movers_app(tmp_path)
    session = new_page(fixture)
    session.goto("/movers")
    session.page.wait_for_selector("#rows td.name")

    # The hostile index is the only +5.00% mover in the default gainers view.
    row = session.page.locator("#rows tr").filter(has_text=fixture.ground["hostile_pct"])
    expect(row).to_have_count(1)
    name_cell = row.locator("td.name")
    # textContent equals the RAW hostile string — rendered as text, not
    # markup. This assert carries the esc() MUTATION TEETH: without esc() the
    # innerHTML sink decodes the payload's smuggled `&lt;img…&gt;` entities
    # (`&` never double-escapes to `&amp;lt;`) and the textContent diverges —
    # verified by running this test against an `${esc(v)}` → `${v}` mutation
    # of movers.html renderRows (FAILS mutated, passes shipped).
    expect(name_cell).to_have_text(XSS_NAME)
    assert name_cell.evaluate("td => td.children.length") == 0
    # No injected element anywhere in the rendered table.
    expect(session.page.locator("#rows img, #rows script")).to_have_count(0)
    # The attribute-breakout payload carries a REAL onmouseover handler —
    # hovering the cell must not fire it.
    name_cell.hover()
    assert session.page.evaluate("() => window.__bx_xss === undefined") is True
    assert session.page_errors == []
    assert session.dialogs == []
