"""Hidden-tab polling pause in the two self-injecting widgets
(DEC-LEDGER-DELTA-O-PAGE-001, sub-part (e)).

``bx-rate-monitor.js`` (3 s ``/api/v1/pull/rate`` + ``/api/v1/universe/pull-progress``)
and ``bx-liveproof.js`` (5 s ``/api/v1/liveproof``) are injected into EVERY dashboard
template, so a wall of background tabs used to fire ~52 req/min of polls — each
pull-progress hit landing on the mid-pull ledger fold. Both widgets now carry the
exact ``templates/v29.html`` pattern: ``if (document.hidden) return;`` as the first
statement of ``poll()`` (a hidden tab fires ZERO fetches) plus ONE
``visibilitychange`` listener that fires an immediate catch-up tick on tab return
(never a stale pill for a returning operator — Golden Rule #1).

HONEST envelope (Golden Rule #10): these are SOURCE-STRING pins — the repo's JS
convention (no browser runtime in CI) — not a browser-executed behavioural test;
the guarded pattern is a verbatim copy of the production v29.html poller pattern.
First-paint behaviour is pinned unchanged: ``start()`` still builds the DOM, fires
one immediate ``poll()`` and keeps the same single ``setInterval`` cadence.
"""

from __future__ import annotations

from pathlib import Path

import brutex.dashboard
import pytest

_STATIC = Path(brutex.dashboard.__file__).resolve().parent / "static"
_WIDGET_FILES = ("bx-rate-monitor.js", "bx-liveproof.js")
_GUARD = "if (document.hidden) return;"
_LISTENER_REGISTRATION = 'document.addEventListener("visibilitychange", function () {'
_CATCH_UP = "if (!document.hidden) poll();"
_INTERVAL = "setInterval(poll, POLL_MS);"


def _widget_js(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


# ── T-e1: the guard is the FIRST statement of poll(), before any fetch ───────


@pytest.mark.parametrize("name", _WIDGET_FILES)
def test_poll_guard_is_first_statement_before_any_fetch(name: str) -> None:
    """``if (document.hidden) return;`` opens ``poll()`` — ahead of every fetch.

    The guard must be the first STATEMENT (only comment lines may precede it
    inside the body), so a hidden tab returns before either endpoint fetch —
    including the ``/api/v1/universe/pull-progress`` read that lands on the
    mid-pull ledger fold.
    """
    js = _widget_js(name)
    assert js.count("function poll() {") == 1, f"{name}: expected exactly one poll()"
    body_start = js.index("function poll() {") + len("function poll() {")
    assert _GUARD in js[body_start:], f"{name}: the document.hidden guard is missing"
    guard_pos = js.index(_GUARD, body_start)
    fetch_pos = js.index("fetch(", body_start)
    assert guard_pos < fetch_pos, f"{name}: the guard must precede poll()'s first fetch"
    # First-statement pin: nothing but comments between the brace and the guard.
    for line in js[body_start:guard_pos].splitlines():
        stripped = line.strip()
        assert stripped == "" or stripped.startswith("//"), (
            f"{name}: the guard must be poll()'s first statement; found {stripped!r}"
        )


@pytest.mark.parametrize("name", _WIDGET_FILES)
def test_no_fetch_outside_the_guarded_poll(name: str) -> None:
    """Every fetch in the widget sits AFTER the guard — no unguarded side-channel."""
    js = _widget_js(name)
    guard_pos = js.index(_GUARD)
    first_fetch = js.index("fetch(")
    assert guard_pos < first_fetch, f"{name}: a fetch appears before the document.hidden guard"


# ── T-e2: exactly one visibilitychange catch-up listener ─────────────────────


@pytest.mark.parametrize("name", _WIDGET_FILES)
def test_exactly_one_visibilitychange_catch_up_listener(name: str) -> None:
    """ONE listener, firing an immediate ``poll()`` when the tab becomes visible."""
    js = _widget_js(name)
    assert js.count('addEventListener("visibilitychange"') == 1, (
        f"{name}: expected exactly one visibilitychange listener"
    )
    assert _LISTENER_REGISTRATION in js, f"{name}: listener registration shape drifted"
    assert _CATCH_UP in js, f"{name}: the !document.hidden catch-up tick is missing"
    # The catch-up body belongs to the listener (registration precedes it).
    assert js.index(_LISTENER_REGISTRATION) < js.index(_CATCH_UP), (
        f"{name}: the catch-up tick must live inside the visibilitychange listener"
    )


# ── T-e3: scheduling guard only — interval + first paint unchanged ───────────


@pytest.mark.parametrize("name", _WIDGET_FILES)
def test_interval_and_first_paint_unchanged(name: str) -> None:
    """Still exactly one ``setInterval(poll, POLL_MS)`` and an immediate first poll.

    The pause is a guard INSIDE ``poll()`` — the interval keeps ticking (hidden
    ticks are free) and ``start()`` still renders immediately on load: DOM build,
    one synchronous ``poll()``, then the interval, then the listener.
    """
    js = _widget_js(name)
    assert js.count(_INTERVAL) == 1, f"{name}: setInterval count drifted"
    immediate = f"poll();\n    {_INTERVAL}"
    assert immediate in js, f"{name}: the immediate first-paint poll() drifted"
    assert js.index(_INTERVAL) < js.index(_LISTENER_REGISTRATION), (
        f"{name}: the listener registers after the interval (the v29 order)"
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
    assert _CATCH_UP not in ui, "the widget catch-up tick must not merge into bx-ui.js"
    for name in _WIDGET_FILES:
        js = _widget_js(name)
        assert "BXUI" not in js, f"{name}: widgets stay self-contained (no BXUI reach-in)"
        assert "initVisibilityPause" not in js, (
            f"{name}: the CSS-animation pause is not the polling pause"
        )
