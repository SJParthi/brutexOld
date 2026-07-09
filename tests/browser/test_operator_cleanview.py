"""Operator-page clean-view browser truth (DEC-OPERATOR-PAGE-CLEANVIEW-001).

Real-Chromium evidence for the operator's four clean-view complaints, driven
against the REAL ``build_app`` operator page over the committed one-click
fixture run:

* **jargon-free idle render** — the page's VISIBLE text (``innerText``)
  carries none of the banned developer tokens; the status hero reads
  ``Idle — ready to run`` on the muted tone; the rate widget collapses to
  its quiet pill; the awaiting banner's computed style is NEUTRAL (no gold
  gradient — empty ≠ error);
* **hero state machine** — rows 1 (running sweep), 2 (first stalled tick =
  active, review round 2), 3 (attention — stalled pull ≥ 2 ticks AND the
  immediately-red ``flow_state='failing'`` verdict), 4 (suspect run) and 6
  (idle) each render from route-fulfilled payloads, every poll cycle
  advanced deterministically via the frozen ``page.clock``;
* **STALE tier gating** — a feed outage on an IDLE page degrades the rate
  widget to the amber ``feed quiet`` readout (never the red STALE class),
  while the SAME outage during an ACTIVE pull still goes loud red;
* **pass-path evidence** — explicit ``page.screenshot`` calls persist
  ``after-{idle,running,suspect}.png`` under
  ``test-artifacts/browser/operator-cleanview/`` (the harness screenshots on
  failure only) plus a navigation→hero render-timing ledger.

Zero-flakiness law: payload-driven synchronisation only (``expect`` locator
polling + ``page.clock``); the widget endpoints are route-fulfilled by each
test; failure-path tests opt out of the console-clean assert (aborted/5xx
fetches legitimately log resource errors) — pageerror/dialog cleanliness is
still asserted unconditionally by the harness.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Final

import pytest
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore

from tests.browser._fixtures import AppFixture
from tests.browser._guards import browser_suite_enabled
from tests.browser.conftest import ARTIFACT_ROOT, NewPageFactory, freeze_page_clock

if not browser_suite_enabled():
    # Direct-file invocation outside the opt-in (collect_ignore governs only
    # directory-shaped collection): a playwright-less box gets an HONEST skip
    # here instead of a collection ImportError; the conftest modifyitems hook
    # skips the collected items. With the suite ENABLED a missing playwright
    # stays a LOUD import error — never a silent green skip in CI.
    pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Route, expect

pytestmark = pytest.mark.browser

_REPO: Final[Path] = Path(__file__).resolve().parents[2]
_FIXTURE_RUN: Final[str] = "r-20260707-fixt01"
_FIXTURE_DIR: Final[Path] = _REPO / "tests" / "fixtures" / "oneclick" / "sweep_runs" / _FIXTURE_RUN

#: Pass-path evidence directory (explicit screenshots — the harness only
#: captures on failure).
_EVIDENCE_DIR: Final[Path] = ARTIFACT_ROOT / "operator-cleanview"

#: Banned developer-jargon tokens for the VISIBLE-text scan (acceptance #1).
_JARGON_TOKENS: Final[tuple[str, ...]] = (
    "awaiting real run artifact",
    "merge-integration",
    "not wired",
    "not_wired",
    "results_store",
    "tuples",
    "BRUTEX_",
    "PROVISIONAL",
    "O(file)",
)

#: bx-rate-monitor.js POLL_MS / the heartbeat pull cadence / the shared
#: bx-poll.js cadence — one virtual-clock advance per poll cycle.
_WIDGET_POLL_MS: Final[int] = 3000
_HEARTBEAT_PULL_MS: Final[int] = 5000
_ONECLICK_POLL_MS: Final[int] = 3000


def build_operator_app(tmp_path: Path) -> AppFixture:
    """The REAL operator app over a single-run copy of the committed one-click
    fixture corpus (deterministic latest_run_id; manifest.suspect is False)."""
    (tmp_path / "sweeps").mkdir(parents=True, exist_ok=True)
    shutil.copytree(_FIXTURE_DIR, tmp_path / "sweep_runs" / _FIXTURE_RUN)
    store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    app = build_app(DashboardState(), instrument_store=store)
    return AppFixture(app=app, ground={"run_id": _FIXTURE_RUN}, cleanup=store.close)


def _fulfill_json(payload: dict[str, Any]) -> Any:
    """A page.route handler answering 200 JSON with *payload* (fixed)."""
    body = json.dumps(payload)

    def handle(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=body)

    return handle


def _screenshot(page: Any, name: str) -> None:
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(_EVIDENCE_DIR / name))


# ── acceptance #1 + #3 + #4: the idle page is calm, clean and jargon-free ──


def test_idle_page_is_calm_and_jargon_free(new_page: NewPageFactory, tmp_path: Path) -> None:
    """Idle render: hero row 6, collapsed rate pill, neutral awaiting style,
    ZERO jargon tokens in the visible text — plus the pass-path screenshot
    and the navigation→hero render timing ledger."""
    session = new_page(build_operator_app(tmp_path))
    page = session.page
    freeze_page_clock(page)
    t0 = time.monotonic()
    session.goto("/")
    hero = page.locator("#bxHeroLine")
    expect(hero).to_have_text("Idle — ready to run")
    hero_visible_ms = (time.monotonic() - t0) * 1000.0
    expect(hero).to_have_class("hero-muted")

    # The rate widget collapsed to its quiet pill (stubbed flow_state=idle),
    # no red stale class anywhere on it.
    widget = page.locator("#bxRateMonitor")
    expect(widget).to_have_class("bxrm-min")
    # No ALERT-tone heartbeat card while idle (they live inside the collapsed
    # System-details block; tone classes must still be non-bad).
    expect(page.locator(".hb-big.hb-bad")).to_have_count(0)
    # The diagnostics block ships collapsed and is never auto-opened.
    assert page.get_attribute("#bxDiagnostics", "open") is None

    # Acceptance #4: the awaiting banner is a calm empty state — no gold
    # gradient background (computed style, not source text).
    bg_image = page.eval_on_selector("#ocAwaiting", "el => getComputedStyle(el).backgroundImage")
    assert bg_image == "none", f"awaiting banner still carries a gradient: {bg_image!r}"

    # Acceptance #1: the page text carries none of the banned tokens —
    # scripts/styles removed, and the ONE exempt-by-design verbatim surface
    # (the timeline's raw event lines — the search-coherence contract)
    # excluded; everything else, the collapsed diagnostics included, is
    # scanned via textContent (stricter than innerText).
    visible = page.evaluate(
        "() => { const clone = document.body.cloneNode(true);"
        " clone.querySelectorAll('script, style, #ocTimeline')"
        "   .forEach((n) => n.remove());"
        " return clone.textContent; }"
    )
    for token in _JARGON_TOKENS:
        assert token not in visible, f"visible jargon survived: {token!r}"

    _screenshot(page, "after-idle.png")
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (_EVIDENCE_DIR / "timing-idle.json").write_text(
        json.dumps({"navigation_to_hero_text_ms": round(hero_visible_ms, 1)}),
        encoding="utf-8",
    )
    assert session.request_count("/api/v1/oneclick/runs") >= 1  # real corpus drove the page


# ── hero row 1: running sweep ───────────────────────────────────────────────


def test_hero_shows_running_sweep(new_page: NewPageFactory, tmp_path: Path) -> None:
    session = new_page(build_operator_app(tmp_path))
    page = session.page
    page.route(
        "**/api/v1/sweep-progress",
        _fulfill_json(
            {
                "state": "running",
                "running": True,
                "percent": 41.2,
                "enumerated": 1000,
                "evaluated": 412,
                "survivors": 3,
                "skipped": 0,
                "combos_per_sec": 123.4,
                "eta_seconds": 90,
            }
        ),
    )
    freeze_page_clock(page)
    session.goto("/")
    hero = page.locator("#bxHeroLine")
    expect(hero).to_contain_text("Sweep running — 41.2%")
    expect(hero).to_have_class("hero-ok")
    expect(page.locator("#bxHeroSub")).to_contain_text("412 of 1,000 tested · 3 winners so far")
    # The ticker strip reads plain English, not the artifact vocabulary.
    expect(page.locator("#bxTickerStrip")).to_contain_text("sweep running")
    _screenshot(page, "after-running.png")


# ── hero row 4: suspect run ─────────────────────────────────────────────────


def test_hero_flags_a_suspect_run(new_page: NewPageFactory, tmp_path: Path) -> None:
    session = new_page(build_operator_app(tmp_path))
    page = session.page
    page.route(
        "**/api/v1/oneclick/manifest*",
        _fulfill_json(
            {
                "status": "ok",
                "run_id": _FIXTURE_RUN,
                "pill": {"state": "suspect", "derived_from": "tripwire fired"},
                "fixture_root_active": False,
                "manifest": {"suspect": True, "run_id": _FIXTURE_RUN},
            }
        ),
    )
    freeze_page_clock(page)
    session.goto("/")
    # The manifest poller only carries a run_id once the runs poller selected
    # one; barrier on the populated run <select>, then advance the frozen
    # clock one poll cycle so the run-scoped manifest fetch actually fires.
    expect(page.locator("#ocRunSelect option")).to_have_count(1)
    with page.expect_response("**/api/v1/oneclick/manifest*"):
        page.clock.fast_forward(_ONECLICK_POLL_MS)
    hero = page.locator("#bxHeroLine")
    expect(hero).to_have_text("Latest run needs review — a self-check flagged its numbers")
    expect(hero).to_have_class("hero-warn")
    # The pill displays the plain-English remap while its CSS class stays
    # keyed on the RAW payload token (red styling preserved) and the verbatim
    # server state stays reachable in the title (Law 2).
    pill = page.locator("#ocRunPill")
    expect(pill).to_have_text("needs review")
    expect(pill).to_have_class("oc-pill oc-pill-suspect")
    title = page.get_attribute("#ocRunPill", "title") or ""
    assert "server state: suspect" in title
    # Review round 1: a manifest-carrying run HIDES the awaiting banner — the
    # author-origin display:flex used to defeat the hidden attribute, so
    # "No completed run yet" rendered beside this very pill/hero.
    expect(page.locator("#ocAwaiting")).to_be_hidden()
    _screenshot(page, "after-suspect.png")


# ── hero row 3: attention — stalled pull ────────────────────────────────────


def test_hero_attention_on_stalled_pull(new_page: NewPageFactory, tmp_path: Path) -> None:
    """A pull stalled for >= 2 heartbeat ticks is a red hero path — and the
    FIRST stalled tick reads paused-but-ACTIVE, never Idle (review round 2)."""
    session = new_page(build_operator_app(tmp_path), stub_widgets=False)
    page = session.page
    page.route("**/api/v1/pull/rate", _fulfill_json({"flow_state": "stalled"}))
    page.route("**/api/v1/universe/pull-progress", _fulfill_json({"running": False}))
    page.route("**/api/v1/liveproof", _fulfill_json({"preflight": None, "probes": None}))
    freeze_page_clock(page)
    session.goto("/")
    # First heartbeat pull tick fires at load: one stalled tick = an active
    # pull with paused flow (warn), NOT the false-calm idle row.
    hero = page.locator("#bxHeroLine")
    expect(hero).to_have_text("Data pull in progress")
    expect(hero).to_have_class("hero-warn")
    expect(page.locator("#bxHeroSub")).to_contain_text("data flow paused")
    # Two more cycles under the frozen clock push the stalled counter past
    # the >=2-tick gate — the red escalation.
    for _ in range(2):
        with page.expect_response("**/api/v1/pull/rate"):
            page.clock.fast_forward(_HEARTBEAT_PULL_MS)
    expect(hero).to_contain_text("Attention needed — the data pull has stopped moving")
    expect(hero).to_have_class("hero-bad")
    # The stalled widget is NOT collapsed (degraded is never hidden).
    cls = page.get_attribute("#bxRateMonitor", "class") or ""
    assert "bxrm-min" not in cls.split()


# ── hero row 3: attention — actively-FAILING pull (review round 2) ─────────


def test_hero_red_on_failing_pull(new_page: NewPageFactory, tmp_path: Path) -> None:
    """The DISTINCT server verdict flow_state='failing' (FLOW_FAILING — an
    ACTIVE pull whose every call fails) is red IMMEDIATELY on the hero and
    ALERT-toned on the heartbeat pull card — it previously fell through to
    the false-calm 'Idle — ready to run' row (review round 2)."""
    session = new_page(build_operator_app(tmp_path), stub_widgets=False)
    page = session.page
    page.route("**/api/v1/pull/rate", _fulfill_json({"flow_state": "failing"}))
    page.route("**/api/v1/universe/pull-progress", _fulfill_json({"running": False}))
    page.route("**/api/v1/liveproof", _fulfill_json({"preflight": None, "probes": None}))
    freeze_page_clock(page)
    session.goto("/")
    hero = page.locator("#bxHeroLine")
    expect(hero).to_have_text("Attention needed — the data pull is failing every call")
    expect(hero).to_have_class("hero-bad")
    # The heartbeat pull card agrees (ALERT tone, active wording) — never
    # "no data pull running" for an active-and-failing pull.
    expect(page.locator("#hbPull .hb-big")).to_have_text("pull running — every call is failing")


# ── acceptance #3: STALE red is reserved for an outage DURING an active pull ──


def test_rate_widget_idle_outage_amber_active_outage_red(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """An idle page with a dead feed degrades AMBER ("feed quiet"), never the
    red STALE class; the SAME outage after an active pull goes loud red.
    Console-clean opt-out: the failing polls legitimately log resource errors
    (pageerror/dialog cleanliness still asserted by the harness)."""
    session = new_page(build_operator_app(tmp_path), stub_widgets=False, expect_console_clean=False)
    page = session.page
    mode = {"value": "idle"}

    def serve_rate(route: Route) -> None:
        if mode["value"] == "fail":
            route.fulfill(status=503, content_type="application/json", body="{}")
        else:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"flow_state": mode["value"]}),
            )

    page.route("**/api/v1/pull/rate", serve_rate)
    page.route("**/api/v1/universe/pull-progress", _fulfill_json({"running": False}))
    page.route("**/api/v1/liveproof", _fulfill_json({"preflight": None, "probes": None}))
    freeze_page_clock(page)
    session.goto("/")
    page.wait_for_selector("#bxrmDot")
    expect(page.locator("#bxRateMonitor")).to_have_class("bxrm-min")  # quiet idle pill

    # 1 · idle + outage: age past the red threshold — amber "feed quiet",
    # the red class NEVER appears, and the degraded widget un-collapses.
    mode["value"] = "fail"
    for _ in range(13):  # ~39 s of virtual time: failures + age ticks
        page.clock.fast_forward(_WIDGET_POLL_MS)
    age = page.locator("#bxrmAge")
    expect(age).to_have_class("bxrm-stale-warn")
    expect(age).to_contain_text("feed quiet —")
    cls = page.get_attribute("#bxRateMonitor", "class") or ""
    assert "bxrm-stale" not in cls.split(), "idle outage must never go red"
    assert "bxrm-min" not in cls.split(), "a degraded widget must never stay collapsed"

    # 2 · recover into an ACTIVE pull (flowing), then kill the feed again:
    # the same staleness now escalates to the loud red STALE.
    mode["value"] = "flowing"
    with page.expect_response("**/api/v1/pull/rate"):
        page.clock.fast_forward(31_000)  # past the max backoff — a poll lands
    expect(page.locator("#bxrmState")).to_have_text("flowing")
    mode["value"] = "fail"
    for _ in range(13):
        page.clock.fast_forward(_WIDGET_POLL_MS)
    expect(age).to_have_class("bxrm-stale")
    expect(age).to_contain_text("STALE")
    expect(page.locator("#bxRateMonitor")).to_have_class("bxrm-stale")


# ── the copy catalog rides the real page (fixture-driven remap) ────────────


def test_trades_source_chip_remaps_with_server_title(
    new_page: NewPageFactory, tmp_path: Path
) -> None:
    """The trades source badge shows the plain-English token with the
    verbatim server token in its title (Law 2) — driven by the REAL trades
    route over the committed fixture (pre-results-depth top100 → 'none')."""
    session = new_page(build_operator_app(tmp_path))
    page = session.page
    freeze_page_clock(page)
    session.goto("/")
    # Barrier on the NOTE (its text provably CHANGES from the template
    # default once the trades render runs — the badge's rendered text equals
    # its template default, so the badge alone is not a render barrier).
    note = page.locator("#ocTradesNote")
    expect(note).to_have_text(
        "This run is from an older version — re-run the sweep to record trade detail."
    )
    badge = page.locator("#ocTradesSource")
    expect(badge).to_have_text("source: —")
    title = page.get_attribute("#ocTradesSource", "title") or ""
    assert title == "server: none"
    # The verbatim server string stays reachable on the note's title (Law 2).
    note_title = page.get_attribute("#ocTradesNote", "title") or ""
    assert note_title.startswith("server: awaiting real run artifact")
