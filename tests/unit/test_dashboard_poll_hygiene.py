"""Hidden-tab poll-hygiene pins for the dashboard poll loops
(DEC-DASHBOARD-POLL-HYGIENE-001, MIGRATION.md #1623).

Nine dashboard poll surfaces used plain ``setInterval`` loops with NO
``document.hidden`` guard, so a BACKGROUNDED tab kept hammering its endpoints
forever. This change adds the repo's established inline guard idiom (the
``bx-liveproof.js`` / ``templates/v29.html`` pattern — ``if (document.hidden)
return;`` as the first statement of the poller + ONE ``visibilitychange``
listener that fires an immediate catch-up on tab return) to each page, WITHOUT
touching ``bx-ui.js`` (which the sibling
``test_dashboard_widget_visibility_pause.py`` pins as deliberately owning NO
polling concern). It also:

* adds the missing raw-text skip-render memo to ``completeness.html`` (the ONLY
  4 s-poller page that rebuilt its ~750-row options table every tick — the
  payload-diff gate is additionally pinned in
  ``test_dashboard_js_perf_frontend.py``).

(The reports.html sweep-poll hardening this pack originally shipped was dropped
once DEC-OPERATOR-PAGE-REAL-001 moved the Run-Sweep surface off ``/reports``;
its dedicated pin was removed with it. DEC-DASHBOARD-POLL-HYGIENE-002's v29
in-place sweep-poller hardening + its pins here were likewise RETIRED at the
2026-07-11 merge-reconcile: DEC-FRONTEND-LATENCY-HARDENING-001 (#1720) deleted
those pollers for the shared-poller subscription, and the four defect classes
stay pinned at the BXPoll core — hidden guard / through-body abort / capped
backoff / visibilitychange catch-up in
``test_dashboard_oneclick_frontend.py::TestPollDiscipline`` plus the v29
subscription pins in ``test_dashboard_frontend_latency.py``.)

SUPERSESSION (DEC-FRONTEND-LATENCY-HARDENING-001, MIGRATION.md #1720): the
five uniform 4 s pollers no longer own an inline ``if (document.hidden)``
guard + bare ``setInterval(poll, 4000)`` — their SCHEDULING moved onto the ONE
shared ``BXPoll.createPoller`` core (``bx-poll.js``), which provides the SAME
guarantees this suite pinned (hidden-tab guard BEFORE any fetch, catch-up on
tab return) PLUS a one-in-flight chain, a 10 s fetch abort and capped
3 s→30 s failure backoff. The pins below therefore now assert the NEW
mechanism with the SAME intent: a hidden tab fires zero fetches, the 4 s
visible cadence survives (``pollMs: 4000``), and a tab return re-polls
immediately (BXPoll wires its own visibilitychange catch-up in ``start()``;
the no-BXPoll degrade path keeps an explicit guarded interval + catch-up).

HONEST envelope (Golden Rule #10): these are SOURCE-STRING pins — the repo has
no JS runtime in CI — not a browser-executed behavioural test. bx-poll.js's
own discipline (hidden guard / backoff / catch-up) is pinned at its source by
``test_dashboard_oneclick_frontend.py`` and node-golden-tested for its pure
parts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TEMPLATES = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "templates"

#: The five uniform 4 s-poller pages, now scheduled through BXPoll.createPoller.
_UNIFORM_POLLERS = (
    "coverage.html",
    "spot_coverage.html",
    "completeness.html",
    "fetch_windows.html",
    "expired_options.html",
)


def _tpl(name: str) -> str:
    text = (_TEMPLATES / name).read_text(encoding="utf-8")
    assert text, f"expected a non-empty template at {name}"
    return text


# ── The five uniform 4 s pollers: BXPoll discipline + cadence + degrade ──────


@pytest.mark.parametrize("name", _UNIFORM_POLLERS)
def test_uniform_poller_rides_the_shared_bxpoll_core(name: str) -> None:
    """The tick is scheduled by ``BXPoll.createPoller`` — never a bare interval.

    BXPoll's ``poll()`` checks ``document.hidden`` BEFORE invoking the tick
    (zero fetches while hidden — the original intent of the inline guard this
    supersedes), runs a ONE-in-flight self-scheduling chain (a slow tick can
    never overlap the next one), and backs off 3 s→30 s on counted failures.
    """
    js = _tpl(name)
    assert '<script defer src="/static/bx-poll.js"></script>' in js, (
        f"{name}: the shared bx-poll.js core must be loaded"
    )
    assert "window.BXPoll.createPoller({" in js, f"{name}: the BXPoll migration is missing"
    # The tick keeps its OWN render/degrade semantics: the poller consumes it
    # through the custom fetchJson seam and a failed tick throws (→ backoff).
    # fetch_windows/completeness thread BXPoll's timeoutMs into the tick
    # (the DEC-FRONTEND-LATENCY-HARDENING-001 review MAJOR — their fetches are
    # bounded through the body read; pinned in TestBoundedTickFetch); the
    # other pollers keep the plain seam.
    assert (
        "return poll().then(function (ok) {" in js
        or "return poll(timeoutMs).then(function (ok) {" in js
    ), f"{name}: the tick must feed BXPoll through the custom fetchJson seam"
    assert "poll tick failed" in js or "surfaces failed" in js, (
        f"{name}: a failed tick must throw into BXPoll's counted-failure backoff"
    )
    # The old inline-guard mechanism is fully superseded — no bare interval.
    assert "setInterval(poll, 4000);" not in js, (
        f"{name}: the bare 4 s setInterval must not survive beside BXPoll"
    )


@pytest.mark.parametrize("name", _UNIFORM_POLLERS)
def test_uniform_poller_keeps_the_4s_cadence(name: str) -> None:
    """The visible-tab cadence is unchanged: ``pollMs: 4000``."""
    js = _tpl(name)
    assert "pollMs: 4000," in js, f"{name}: the 4 s cadence token (pollMs: 4000) must survive"


@pytest.mark.parametrize("name", _UNIFORM_POLLERS)
def test_uniform_poller_starts_at_domcontentloaded_with_guarded_degrade(name: str) -> None:
    """The poller starts once deferred scripts are guaranteed executed, and a
    missing bx-poll.js degrades to a legacy HIDDEN-GUARDED interval + catch-up
    (the page never goes pollerless, and even the degrade path keeps the
    zero-fetches-while-hidden law + the tab-return catch-up)."""
    js = _tpl(name)
    assert 'document.addEventListener("DOMContentLoaded", startPolling);' in js, (
        f"{name}: the poller must start at DOMContentLoaded"
    )
    assert "if (!document.hidden) void poll();" in js, (
        f"{name}: the degrade path lost its hidden guard / catch-up"
    )
    assert 'addEventListener("visibilitychange"' in js, (
        f"{name}: the degrade path lost its visibilitychange catch-up"
    )


# ── completeness.html: the NEW payload-diff gate (MAJOR-10) ───────────────────


def test_completeness_ships_payload_diff_gate() -> None:
    """The ~750-row options table no longer rebuilds every 4 s unconditionally."""
    js = _tpl("completeness.html")
    assert "let lastBoardTxt = null;" in js
    # The body text now arrives through the BOUNDED text fetch (the
    # DEC-FRONTEND-LATENCY-HARDENING-001 review MAJOR — abort armed through
    # the body read); the compare gate is still the raw response text.
    assert "return r.text().then(function (txt) {" in js
    assert "const txt = res.text;" in js
    # `return true` since DEC-FRONTEND-LATENCY-HARDENING-001: a skipped tick is
    # a SUCCESSFUL tick for BXPoll's failure counting (same skip semantics).
    assert "if (txt === lastBoardTxt) { liveTxt.textContent = lastLiveLine; return true; }" in js
    # The parse now rides the memoised raw text, not a second r.json().
    assert "const board = JSON.parse(txt);" in js
    assert "renderOptions(board.options.per_underlying || []);" in js


# ── universe.html: 7 guarded pollers + a single folded catch-up ──────────────


def test_universe_pollers_are_hidden_guarded() -> None:
    """The 5 s windows line + the six 15 s integrity loaders each guard on hidden.

    Their ``setInterval(…)`` registration strings are UNCHANGED (the guard lives
    inside each function body), so the existing universe-template pins survive.
    """
    js = _tpl("universe.html")
    guard = "if (document.hidden) return;   // hidden-tab pause (DEC-DASHBOARD-POLL-HYGIENE-001)"
    # refreshWindowsLine + loadCompleteness/MappingIntegrity/MasterIntegrity/
    # EconomicsIntegrity/ParseSkips/LakeUniqueness = 7 guarded pollers.
    assert js.count(guard) == 7, f"expected 7 hidden-tab guards, found {js.count(guard)}"
    # The pinned interval registrations are untouched.
    assert "setInterval(refreshWindowsLine, 5000)" in js
    assert "setInterval(loadCompleteness, 15000)" in js
    assert "setInterval(loadLakeUniqueness, 15000)" in js


def test_universe_visibilitychange_fires_the_catch_up_pollers() -> None:
    """On tab return the folded catch-up re-runs every hidden-paused poller."""
    js = _tpl("universe.html")
    for call in (
        "refreshWindowsLine();",
        "loadCompleteness();",
        "loadMappingIntegrity();",
        "loadMasterIntegrity();",
        "loadEconomicsIntegrity();",
        "loadParseSkips();",
        "loadLakeUniqueness();",
    ):
        assert call in js, f"universe.html: catch-up call {call!r} is missing"
    # The catch-up rides the EXISTING pull-progress visibilitychange listener —
    # this change adds NO new listener. universe.html carries exactly TWO
    # listeners total: the pull-progress one (which folds these catch-ups,
    # marked by the DEC-DASHBOARD-POLL-HYGIENE-001 comment) + the independent
    # greeks-backfill gb-poller listener that landed on main via
    # DEC-GREEKS-BULK-REENRICH-001 (#1558 round 9).
    assert js.count("addEventListener('visibilitychange'") == 2, (
        "universe.html must carry exactly the pull-progress + gb-poller "
        "visibilitychange listeners — the poll-hygiene catch-ups add no new one"
    )
    # And the folded integrity catch-ups live INSIDE the pull-progress listener
    # (before the gb-poller listener), not in a listener of their own.
    first_listener = js.index("addEventListener('visibilitychange'")
    second_listener = js.index("addEventListener('visibilitychange'", first_listener + 1)
    catch_up = js.index("loadLakeUniqueness();", first_listener)
    assert first_listener < catch_up < second_listener, (
        "the integrity catch-ups must ride the FIRST (pull-progress) listener"
    )


# ── movers.html: gated-retry hidden guard + catch-up (MAJOR-9) ───────────────


def test_movers_gated_retry_pauses_while_hidden() -> None:
    """The gated auto-retry no longer hammers /api/v1/movers from a background tab.

    The guard is scoped to the gated cycle (``wasGated``) so a normally-idle
    movers page is untouched, and the pinned ``gateTimer = setTimeout(load,
    GATE_RETRY_MS);`` scheduling string survives (two occurrences now).
    """
    js = _tpl("movers.html")
    assert "if (document.hidden && wasGated) {" in js, (
        "movers.html: the gated-retry hidden guard is missing"
    )
    assert js.count("gateTimer = setTimeout(load, GATE_RETRY_MS);") == 2, (
        "movers.html: the pinned gated-retry scheduling string must survive"
    )
    assert "if (!document.hidden && wasGated) { clearTimeout(gateTimer); load(); }" in js, (
        "movers.html: the gated catch-up listener is missing"
    )
