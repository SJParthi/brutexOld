"""The /universe all-segments windows poller disarms on settle (DEC-LATENCY-HONESTY-BATCH-001).

Fails-before framing (audit finding M18): ``pullStarted`` was set ``true`` in
``startAndPoll`` BEFORE the start POST and NEVER cleared anywhere, so ONE pull
click — even a REFUSED start (the ``catch`` path) — armed the 5 s full-payload
``/coverage/windows`` interval poller FOREVER (until a page reload). The fix
paints one final settled line in ``finishLive`` and then clears the flag, so
the interval's ``if (!pullStarted) return;`` guard goes back to a no-op; a
refused/failed start lands in ``finishLive`` via the ``catch`` and therefore
also disarms.

HONEST envelope (Golden Rule #10, the file's own convention —
``test_dashboard_progress_frontend.py``): every test below is a SUBSTRING /
ordering assert pinning the PRESENCE of the JS in the shipped source — NOT its
execution; this repo has no headless JS runner.
"""

from __future__ import annotations

import re

from brutex.dashboard.app import _render_universe_html


def _finish_live_body(html: str) -> str:
    """The ``finishLive`` function body (up to the next top-level function)."""
    start = html.index("function finishLive()")
    end = html.index("function refreshVerdict()", start)
    return html[start:end]


def test_finish_live_disarms_the_windows_poller_after_one_final_refresh() -> None:
    # THE regression pin (fails before the fix): finishLive now clears
    # pullStarted, so the 5 s interval guard no-ops once the watch settles.
    html = _render_universe_html()
    body = _finish_live_body(html)
    assert "pullStarted = false;" in body
    # … and paints ONE final settled windows line BEFORE dropping the flag
    # (refreshWindowsLine reads the guard synchronously at entry).
    assert "refreshWindowsLine();" in body
    assert body.index("refreshWindowsLine();") < body.index("pullStarted = false;")


def test_interval_guard_and_arm_site_still_ship() -> None:
    # The poller stays gated on a started watch: the guard + the arm survive.
    html = _render_universe_html()
    assert "if (!pullStarted) return;" in html
    assert "pullStarted = true;" in html
    assert "setInterval(refreshWindowsLine, 5000);" in html


def test_pull_started_is_cleared_only_in_finish_live() -> None:
    # Exactly ONE clear site — finishLive (the `let pullStarted = false;`
    # declaration is excluded). A second clear (e.g. inside the poll loop)
    # would blank the line mid-watch; none exists.
    html = _render_universe_html()
    clears = re.findall(r"(?<!let )pullStarted = false;", html)
    assert len(clears) == 1
    assert "pullStarted = false;" in _finish_live_body(html)


def test_refused_start_path_reaches_finish_live() -> None:
    # A refused/failed start POST lands in the catch → finishLive — the same
    # settle path that now disarms the poller (no separate disarm needed).
    html = _render_universe_html()
    start = html.index("async function startAndPoll(")
    end = html.index("function terminalPullHeader(", start)
    body = html[start:end]
    assert "catch (e)" in body
    assert "finishLive();" in body
