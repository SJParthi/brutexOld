"""Hidden-tab polling pause in the two self-injecting widgets
(DEC-LEDGER-DELTA-O-PAGE-001, sub-part (e)).

``bx-rate-monitor.js`` (3 s ``/api/v1/pull/rate`` + ``/api/v1/universe/pull-progress``)
and ``bx-liveproof.js`` (5 s ``/api/v1/liveproof``) are injected into EVERY dashboard
template, so a wall of background tabs used to fire ~52 req/min of polls — each
pull-progress hit landing on the mid-pull ledger fold. Both widgets carry the
``templates/v29.html`` pattern semantics: a ``document.hidden`` guard as the first
statement of ``poll()`` (a hidden tab fires ZERO fetches) plus ONE
``visibilitychange`` listener that fires an immediate catch-up ``poll()`` on tab
return (never a stale pill for a returning operator — Golden Rule #1).

Merge reconcile (main PR #1241, DEC-PULL-UI-STALENESS-001): ``bx-rate-monitor.js``
replaced its ``setInterval(poll, POLL_MS)`` with a SELF-SCHEDULING ``schedulePoll``
chain (success → POLL_MS, failure → capped exponential backoff) plus a 1 s
``renderAge`` staleness ticker, and its own visibilitychange handler already fires
``poll()`` directly on return. The hidden-tab guard is adapted to that structure:
``if (document.hidden) { schedulePoll(POLL_MS); return; }`` — the chain re-arms at
normal cadence (a hidden tick is free, ZERO fetches) so the never-gives-up
self-recovery property survives a hidden period, the guard sits BEFORE the
``pollSeq`` bump (an in-flight response still renders) and BEFORE any fetch (a
hidden period counts ZERO ``pollFailures`` — no spurious backoff while hidden).

Second reconcile (DEC-PULL-UI-STALENESS-001 port to ``bx-liveproof.js``,
display-truth batch): the live-proof widget ADOPTS the same #1241 shape — the
bounded ``fetchJson`` helper, the ``schedulePoll`` chain (success → POLL_MS,
failure → capped backoff, never gives up), the 1 s ``renderAge`` staleness ticker,
and the same ``if (document.hidden) { schedulePoll(POLL_MS); return; }`` guard — so
a silent/unreachable backend can never keep a green "pass" pill reading fresh. Its
visibilitychange catch-up stays the one-liner ``if (!document.hidden) poll();`` (a
direct poll() whose own schedulePoll clears the pending timer). Both widgets now
share the self-scheduling shape.

HONEST envelope (Golden Rule #10): these are SOURCE-STRING pins — the repo's JS
convention (no browser runtime in CI) — not a browser-executed behavioural test.
"""

from __future__ import annotations

from pathlib import Path

import brutex.dashboard
import pytest

_STATIC = Path(brutex.dashboard.__file__).resolve().parent / "static"
_WIDGET_FILES = ("bx-rate-monitor.js", "bx-liveproof.js")
_LISTENER_REGISTRATION = 'document.addEventListener("visibilitychange", function () {'

#: Per-widget hidden-tab guard (first statement of poll()). Both widgets share the
#: #1241 self-scheduling chain: re-arm at normal cadence, then return.
_GUARDS = {
    "bx-rate-monitor.js": "if (document.hidden) { schedulePoll(POLL_MS); return; }",
    "bx-liveproof.js": "if (document.hidden) { schedulePoll(POLL_MS); return; }",
}
#: Per-widget visibilitychange catch-up opener (an immediate poll() on return).
_CATCH_UPS = {
    "bx-rate-monitor.js": "if (!document.hidden) {",
    "bx-liveproof.js": "if (!document.hidden) poll();",
}


def _widget_js(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


def _first_network_call_pos(js: str, start: int) -> int:
    """Position of poll()'s first network call — raw ``fetch(`` or ``fetchJson(``."""
    candidates = [
        pos for pos in (js.find("fetchJson(", start), js.find("fetch(", start)) if pos != -1
    ]
    assert candidates, "no network call found after poll()'s opening brace"
    return min(candidates)


# ── T-e1: the guard is the FIRST statement of poll(), before any fetch ───────


@pytest.mark.parametrize("name", _WIDGET_FILES)
def test_poll_guard_is_first_statement_before_any_fetch(name: str) -> None:
    """The ``document.hidden`` guard opens ``poll()`` — ahead of every fetch.

    The guard must be the first STATEMENT (only comment lines may precede it
    inside the body), so a hidden tab returns before either endpoint fetch —
    including the ``/api/v1/universe/pull-progress`` read that lands on the
    mid-pull ledger fold. In ``bx-rate-monitor.js`` it must ALSO precede the
    ``pollSeq`` bump (an in-flight response keeps rendering) so a hidden tick
    can never discard a legitimate in-flight success (#1241 interaction).
    """
    js = _widget_js(name)
    guard = _GUARDS[name]
    assert js.count("function poll() {") == 1, f"{name}: expected exactly one poll()"
    body_start = js.index("function poll() {") + len("function poll() {")
    assert guard in js[body_start:], f"{name}: the document.hidden guard is missing"
    guard_pos = js.index(guard, body_start)
    fetch_pos = _first_network_call_pos(js, body_start)
    assert guard_pos < fetch_pos, f"{name}: the guard must precede poll()'s first fetch"
    if name == "bx-rate-monitor.js":
        seq_pos = js.index("++pollSeq", body_start)
        assert guard_pos < seq_pos, f"{name}: the guard must precede the pollSeq bump"
    # First-statement pin: nothing but comments between the brace and the guard.
    for line in js[body_start:guard_pos].splitlines():
        stripped = line.strip()
        assert stripped == "" or stripped.startswith("//"), (
            f"{name}: the guard must be poll()'s first statement; found {stripped!r}"
        )


@pytest.mark.parametrize("name", _WIDGET_FILES)
def test_no_fetch_outside_the_guarded_poll(name: str) -> None:
    """Every network CALL SITE in the widget sits AFTER the guard — no side-channel.

    Both widgets (post-#1241 / display-truth port) route every request through
    their bounded ``fetchJson`` helper: the ONE raw ``fetch(`` lives inside that
    helper's definition, and every ``fetchJson(`` CALL SITE sits after the guard
    inside the guarded ``poll()``.
    """
    js = _widget_js(name)
    guard_pos = js.index(_GUARDS[name])
    assert js.count("fetch(") == 1, f"{name}: raw fetch( must live ONLY in fetchJson"
    assert js.index("fetch(") > js.index("function fetchJson(url)"), (
        f"{name}: the raw fetch( must sit inside the fetchJson helper"
    )
    call_sites = [pos for pos in _find_all(js, "fetchJson(") if not js[:pos].endswith("function ")]
    assert call_sites, f"{name}: no fetchJson call sites found"
    assert all(pos > guard_pos for pos in call_sites), (
        f"{name}: a fetchJson call appears before the document.hidden guard"
    )


def _find_all(js: str, needle: str) -> list[int]:
    positions: list[int] = []
    pos = js.find(needle)
    while pos != -1:
        positions.append(pos)
        pos = js.find(needle, pos + 1)
    return positions


# ── T-e2: exactly one visibilitychange catch-up listener ─────────────────────


@pytest.mark.parametrize("name", _WIDGET_FILES)
def test_exactly_one_visibilitychange_catch_up_listener(name: str) -> None:
    """ONE listener, firing an immediate ``poll()`` when the tab becomes visible."""
    js = _widget_js(name)
    catch_up = _CATCH_UPS[name]
    assert js.count('addEventListener("visibilitychange"') == 1, (
        f"{name}: expected exactly one visibilitychange listener"
    )
    assert _LISTENER_REGISTRATION in js, f"{name}: listener registration shape drifted"
    assert catch_up in js, f"{name}: the !document.hidden catch-up tick is missing"
    # The catch-up body belongs to the listener (registration precedes it).
    catch_pos = js.index(catch_up)
    assert js.index(_LISTENER_REGISTRATION) < catch_pos, (
        f"{name}: the catch-up tick must live inside the visibilitychange listener"
    )
    if name == "bx-rate-monitor.js":
        # #1241 shape: the block clears any pending backoff timer then polls
        # DIRECTLY (never a 0 ms timer) — the immediate catch-up our hidden
        # pause relies on to clear the staleness readout within one RTT.
        block = js[catch_pos : js.index("});", catch_pos)]
        assert "clearTimeout(pollTimer)" in block, (
            f"{name}: the catch-up must clear the pending backoff timer"
        )
        assert "poll();" in block, f"{name}: the catch-up must invoke poll() directly"


# ── T-e3: scheduling guard only — cadence + first paint unchanged ────────────


@pytest.mark.parametrize("name", _WIDGET_FILES)
def test_interval_and_first_paint_unchanged(name: str) -> None:
    """Self-scheduling shape pinned for BOTH widgets; ``start()`` paints immediately.

    Post-#1241 (rate monitor) / display-truth port (live proof): NO
    ``setInterval(poll`` anywhere — the poll is a self-scheduling ``schedulePoll``
    chain (success → ``POLL_MS``, failure → capped backoff, never gives up) that
    the hidden guard re-arms at ``POLL_MS``; the only interval is the 1 s
    ``renderAge`` staleness ticker, and ``start()`` fires one immediate ``poll()``
    before registering the visibilitychange listener.
    """
    js = _widget_js(name)
    assert "setInterval(poll" not in js, (
        f"{name}: the self-scheduling schedulePoll chain replaces setInterval(poll…)"
    )
    assert js.count("setInterval(renderAge, AGE_TICK_MS);") == 1, (
        f"{name}: the 1 s staleness ticker drifted"
    )
    start_body = js[js.index("function start() {") :]
    first_poll = start_body.index("poll();")
    assert first_poll < start_body.index("setInterval(renderAge"), (
        f"{name}: start() must fire the immediate first poll() before the age ticker"
    )
    assert start_body.index("setInterval(renderAge") < start_body.index(_LISTENER_REGISTRATION), (
        f"{name}: the listener registers after the age ticker (the #1241 order)"
    )
    # The chain re-arms on every outcome: success, failure AND hidden.
    assert js.count("schedulePoll(POLL_MS);") == 2, (
        f"{name}: expected the success re-arm + the hidden-tab re-arm"
    )
    assert "schedulePoll(backoffDelayMs(pollFailures));" in js, (
        f"{name}: the failure backoff re-arm drifted"
    )


# ── T-e4: bx-ui.js untouched — no false COMMON merge ─────────────────────────


def test_bx_ui_visibility_pause_stays_css_only() -> None:
    """``bx-ui.js initVisibilityPause`` is deliberately NOT reused for polling.

    It pauses CSS animations (toggles ``bx-paused`` on ``<html>``) — a different
    mechanism. The widgets stay self-contained IIFEs: they never reach into
    ``BXUI``, and bx-ui.js gains no polling concern.
    """
    ui = (_STATIC / "bx-ui.js").read_text(encoding="utf-8")
    assert "initVisibilityPause" in ui
    assert "bx-paused" in ui, "the CSS-animation pause class is missing"
    assert ui.count('addEventListener("visibilitychange"') == 1, (
        "bx-ui.js must keep exactly its ONE CSS-pause listener"
    )
    assert "POLL_MS" not in ui, "no polling concern may leak into bx-ui.js"
    assert _CATCH_UPS["bx-liveproof.js"] not in ui, (
        "the widget catch-up tick must not merge into bx-ui.js"
    )
    for name in _WIDGET_FILES:
        js = _widget_js(name)
        assert "BXUI" not in js, f"{name}: widgets stay self-contained (no BXUI reach-in)"
        assert "initVisibilityPause" not in js, (
            f"{name}: the CSS-animation pause is not the polling pause"
        )
