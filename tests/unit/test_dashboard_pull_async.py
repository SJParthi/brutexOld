"""Regression guard for the /universe single-symbol pull offload (DEC-DASHBOARD-PULL-ASYNC-001).

#366 fixed the dashboard freezing during a pull: the ``async`` route
``api_universe_pull`` called the blocking ``pull_candles(...)`` DIRECTLY on the
asyncio event loop, so a 20-30 s Dhan pull froze every other request (the
operator's logs showed ``GET /api/v1/db/tables latency_ms=32796`` queued for the
whole of a 32 s pull). The fix offloads it via
``await run_in_threadpool(pull_candles, ...)`` — the same pattern every other
route here already uses.

#366 merged WITHOUT this regression test (the bug-fix-regression gate was
advisory and did not block — the merge hole is closed separately by
DEC-AUTOMERGE-ALLGREEN-001). This test closes the *test* gap: it pins the
offload in the route source so the "blocking on the event loop" regression can
never silently return. The dashboard route has no headless-DOM/async-server
harness in this suite, so a source guard is the established pattern here
(mirrors ``test_dashboard_universe_perf.py``).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import brutex.dashboard.app as app_module


def _app_source() -> str:
    """Return the on-disk source of the dashboard app module."""
    src_file = inspect.getsourcefile(app_module)
    assert src_file is not None, "cannot locate brutex.dashboard.app source file"
    return Path(src_file).read_text(encoding="utf-8")


def test_single_symbol_pull_is_offloaded_off_the_event_loop() -> None:
    """The single-symbol pull route must run the blocking pull via run_in_threadpool.

    Guards DEC-DASHBOARD-PULL-ASYNC-001: a direct ``report = pull_candles(`` on
    the async event loop froze every other dashboard request for the pull's full
    duration. The fix offloads it to a worker thread.
    """
    src = _app_source()
    # The OLD blocking form must be gone — it froze the loop for the whole pull.
    assert "report = pull_candles(" not in src, (
        "api_universe_pull must NOT call pull_candles directly on the event loop — "
        "that froze every other request during a 20-30 s pull; offload via run_in_threadpool"
    )
    # The fix: the blocking single-symbol pull is awaited on the threadpool.
    assert "await run_in_threadpool(" in src, (
        "the single-symbol pull route must offload the blocking pull via run_in_threadpool"
    )
    assert "pull_candles," in src, (
        "run_in_threadpool must wrap pull_candles (the offloaded callable)"
    )
