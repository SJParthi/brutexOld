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
its dedicated pin was removed with it. DEC-DASHBOARD-POLL-HYGIENE-002 re-homed
that hardening onto the Operator page's ``pollRunSweep`` in ``v29.html`` —
pinned at the bottom of this file.)

HONEST envelope (Golden Rule #10): these are SOURCE-STRING pins — the repo has
no JS runtime in CI — not a browser-executed behavioural test. Visible-tab
cadence/data is byte-identical (the guard is a first-statement early return;
the ``setInterval`` cadence lines are unchanged where they existed).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TEMPLATES = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "templates"

#: The five uniform ``poll(); setInterval(poll, 4000)`` pages: the guard is the
#: first statement of ``poll()`` and the catch-up re-polls ``poll()`` on return.
_UNIFORM_POLLERS = (
    "coverage.html",
    "spot_coverage.html",
    "completeness.html",
    "fetch_windows.html",
    "expired_options.html",
)

_HIDDEN_GUARD = "if (document.hidden) return;"
_UNIFORM_CATCHUP = "if (!document.hidden) poll();"


def _tpl(name: str) -> str:
    text = (_TEMPLATES / name).read_text(encoding="utf-8")
    assert text, f"expected a non-empty template at {name}"
    return text


# ── The five uniform 4 s pollers: guard + cadence intact + catch-up ──────────


@pytest.mark.parametrize("name", _UNIFORM_POLLERS)
def test_uniform_poller_has_hidden_guard_before_first_fetch(name: str) -> None:
    """``if (document.hidden) return;`` opens ``poll()`` — ahead of every fetch.

    A hidden tab returns before either endpoint fetch fires (the whole point of
    the pause). The 4 s ``setInterval`` cadence line is left intact so a visible
    tab's behaviour is byte-identical.
    """
    js = _tpl(name)
    assert js.count("async function poll() {") == 1, f"{name}: expected one poll()"
    body = js.index("async function poll() {") + len("async function poll() {")
    assert _HIDDEN_GUARD in js[body:], f"{name}: the document.hidden guard is missing"
    guard_pos = js.index(_HIDDEN_GUARD, body)
    # The guard must precede poll()'s first network call.
    fetch_candidates = [
        pos
        for pos in (
            js.find("fetch(", body),
            js.find("doFetch(", body),
            js.find("Promise.allSettled(", body),
        )
        if pos != -1
    ]
    assert fetch_candidates, f"{name}: no network call found in poll()"
    assert guard_pos < min(fetch_candidates), f"{name}: the guard must precede poll()'s first fetch"
    # The interval cadence line still exists (visible-tab behaviour unchanged).
    assert "setInterval(poll, 4000);" in js, f"{name}: the 4 s poll cadence must survive"


@pytest.mark.parametrize("name", _UNIFORM_POLLERS)
def test_uniform_poller_has_visibilitychange_catch_up(name: str) -> None:
    """ONE ``visibilitychange`` listener fires an immediate ``poll()`` on return."""
    js = _tpl(name)
    assert _UNIFORM_CATCHUP in js, f"{name}: the !document.hidden catch-up poll() is missing"
    # The catch-up lives inside a visibilitychange listener registered AFTER the
    # interval (the v29/liveproof order).
    assert 'addEventListener("visibilitychange"' in js, f"{name}: no visibilitychange listener"
    assert js.index("setInterval(poll, 4000);") < js.index(_UNIFORM_CATCHUP), (
        f"{name}: the catch-up listener must register after the interval"
    )


# ── completeness.html: the NEW payload-diff gate (MAJOR-10) ───────────────────


def test_completeness_ships_payload_diff_gate() -> None:
    """The ~750-row options table no longer rebuilds every 4 s unconditionally."""
    js = _tpl("completeness.html")
    assert "let lastBoardTxt = null;" in js
    assert "const txt = await r.text();" in js
    assert "if (txt === lastBoardTxt) { liveTxt.textContent = lastLiveLine; return; }" in js
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


# ── v29.html: the Operator Run-Sweep poller (DEC-DASHBOARD-POLL-HYGIENE-002) ──
#
# The MAJOR-12 re-homed follow-up (the #1623 dropped-as-superseded note):
# DEC-OPERATOR-PAGE-REAL-001 moved Run-Sweep from /reports onto the Operator
# page (v29.html), so the original reports.html hardening is re-applied there —
# hidden-tab guard + counted-failure capped backoff (skip-N-ticks on the fixed
# setInterval, NEVER a give-up) + a visibilitychange catch-up scoped to an
# ARMED poll. pollSweepProgress (guard + catch-up pre-existing) gains the same
# backoff. HONEST envelope: SOURCE-STRING pins, not a browser-executed test.


def test_v29_run_sweep_poller_is_hidden_guarded() -> None:
    """``pollRunSweep`` opens with the hidden guard, ahead of its fetch."""
    js = _tpl("v29.html")
    assert js.count("async function pollRunSweep() {") == 1
    body = js.index("async function pollRunSweep() {")
    guard_pos = js.index(_HIDDEN_GUARD, body)
    fetch_pos = js.index("fetch('/api/v1/sweep-progress')", body)
    assert guard_pos < fetch_pos, (
        "v29.html: pollRunSweep's document.hidden guard must precede its fetch"
    )
    # The 1.2 s interval registration itself is unchanged (visible-tab cadence).
    assert "sweepPoll = setInterval(pollRunSweep, 1200);" in js


def test_v29_run_sweep_poller_has_counted_failure_backoff() -> None:
    """Failures count into a nextAllowedTs skip-tick gate — never a give-up.

    A non-ok response is a COUNTED failure (thrown, never rendered), the gate
    is checked at the top of the poller, and the delay rides the ONE shared
    ``bxSweepPollBackoffMs`` helper (delegating to the COMMON bx-poll.js core
    when loaded). No clearInterval lives in the failure path — the terminal
    clearInterval is the sweep-finished arm only.
    """
    js = _tpl("v29.html")
    assert "let runSweepFailures = 0;" in js
    assert "let runSweepNextTs = 0;" in js
    assert "if (Date.now() < runSweepNextTs) return;" in js
    assert "if (!r.ok) throw new Error('HTTP ' + r.status);" in js
    assert "runSweepNextTs = Date.now() + bxSweepPollBackoffMs(runSweepFailures);" in js
    assert "runSweepFailures = 0; runSweepNextTs = 0;" in js
    assert "'Could not read sweep progress (retrying): '" in js
    # The shared helper: COMMON-core delegation + the capped inline fallback.
    assert "function bxSweepPollBackoffMs(failures) {" in js
    assert "if (window.BXPoll) return window.BXPoll.backoffDelayMs(failures);" in js
    assert "30000" in js[js.index("function bxSweepPollBackoffMs") :][:600], (
        "v29.html: the inline fallback must cap at 30 s (never a give-up)"
    )


def test_v29_run_sweep_visibilitychange_catch_up_is_armed_scoped() -> None:
    """On tab return an ARMED run-sweep poll fires immediately (gate cleared).

    The catch-up is scoped to ``sweepPoll`` non-null so an idle Operator tab
    (no sweep started from this page) fires no fetch on a visibility flip.
    """
    js = _tpl("v29.html")
    needle = "if (!document.hidden && sweepPoll) { runSweepNextTs = 0; pollRunSweep(); }"
    assert needle in js, "v29.html: the armed-scoped run-sweep catch-up is missing"


def test_v29_sweep_progress_poller_gains_the_same_backoff() -> None:
    """pollSweepProgress (guard + catch-up pre-existing) adds the backoff gate.

    Both failure modes count (non-ok status AND rejection/malformed body), the
    heartbeat error hook still fires first, and the existing catch-up now also
    clears the backoff gate so a tab return always reads fresh.
    """
    js = _tpl("v29.html")
    assert "let sweepProgressFailures = 0;" in js
    assert "let sweepProgressNextTs = 0;" in js
    assert "if (Date.now() < sweepProgressNextTs) return;" in js
    assert (
        js.count("sweepProgressNextTs = Date.now() + bxSweepPollBackoffMs(sweepProgressFailures);")
        == 2
    ), "v29.html: both pollSweepProgress failure modes must count into the backoff"
    assert "sweepProgressFailures = 0; sweepProgressNextTs = 0;" in js
    # The 2 s cadence + the (gate-clearing) catch-up survive.
    assert "setInterval(pollSweepProgress, 2000);" in js
    assert "{ sweepProgressNextTs = 0; pollSweepProgress(); }" in js
