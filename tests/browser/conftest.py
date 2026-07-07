"""Harness for the browser truth suite (DEC-DASHBOARD-BROWSER-TRUTH-001).

Contract (final-design §2):

* **Collection guard** — the suite is opt-in via ``BRUTEX_BROWSER_TESTS=1``.
  When disabled, every playwright-importing test module is ``collect_ignore``d
  and only the playwright-free sentinel (``test_suite_visibility.py``) is
  collected, emitting an HONEST skip — so a bare ``pytest`` run (e.g.
  verify-m4pro's) neither crashes on a missing playwright install nor hides
  the suite's existence.
* **Function-scoped uvicorn server per test** (127.0.0.1, port 0 — the
  TrustedHost loopback allowlist stays untouched), on a daemon thread with a
  bounded readiness poll and an asserted clean shutdown.
* **Session-scoped RAW sync Playwright** (no pytest-playwright plugin),
  Chromium headless with ``--no-proxy-server``; when the playwright pin's
  default browser build is absent (this container ships build 1194 under
  ``PLAYWRIGHT_BROWSERS_PATH``) the launch falls back to the newest installed
  ``chromium-*`` build resolved programmatically. NEVER runs
  ``playwright install`` locally.
* **new_page factory** — fresh server+context per call; route policy aborts
  ALL non-127.0.0.1 egress; the three widget poll endpoints are stubbed inert
  by default (``stub_widgets=False`` opts a test out — the staleness module);
  console/pageerror/dialog/request/response ledgers ride the returned
  :class:`PageSession`.
* **Failure artifacts** — trace zip + screenshot + ledgers saved to
  ``test-artifacts/browser/<sanitized nodeid>/`` ONLY when the test failed.
  On PASS the harness asserts an EMPTY console ledger (unless the session
  opted out) and zero pageerrors/dialogs — a silently-noisy page is a fail.
* **Timeouts** — ``DEFAULT_TIMEOUT_MS`` (= the guard cap) is the context AND
  ``expect`` default; synchronisation is payload-driven everywhere
  (``_guards.py`` polices the suite for wall-clock sleeps/retries).
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

import pytest

from tests.browser._guards import MAX_TIMEOUT_MS, browser_suite_enabled

if not browser_suite_enabled():
    # Ignore every playwright-importing module; ONLY the sentinel stays
    # collected (its honest skip keeps the suite's absence visible). Computed
    # by glob so adding a page-test module needs no conftest edit (DYNAMIC).
    collect_ignore = sorted(
        p.name
        for p in Path(__file__).resolve().parent.glob("test_*.py")
        if p.name != "test_suite_visibility.py"
    )
else:
    collect_ignore = []

#: One default for the browser context AND every explicit expect() timeout —
#: sourced from the guard cap so the two can never drift.
DEFAULT_TIMEOUT_MS: Final[int] = MAX_TIMEOUT_MS

#: Repo root (tests/browser/conftest.py → tests/browser → tests → root).
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Failure-artifact root (gitignored).
ARTIFACT_ROOT: Final[Path] = _REPO_ROOT / "test-artifacts" / "browser"

#: Inert 200-JSON stubs for the site-wide widget pollers (bx-rate-monitor's
#: two endpoints + bx-liveproof's), so their 3 s/5 s polls never touch the
#: real routes mid-test. Both widget scripts are defensive: these minimal
#: payloads render the honest idle/pending states with zero console noise.
WIDGET_STUBS: Final[dict[str, dict[str, Any]]] = {
    "/api/v1/pull/rate": {"flow_state": "idle"},
    "/api/v1/universe/pull-progress": {"running": False},
    "/api/v1/liveproof": {"preflight": None, "probes": None},
}


# ── failure detection (standard rep-hook pattern) ─────────────────────────


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Stamp each phase report on the item so teardown can see failure."""
    rep = yield
    setattr(item, f"_bx_rep_{rep.when}", rep)
    return rep


def _test_failed(node: Any) -> bool:
    for when in ("setup", "call"):
        rep = getattr(node, f"_bx_rep_{when}", None)
        if rep is not None and rep.failed:
            return True
    return False


# ── uvicorn app server (function-scoped, one per PageSession) ─────────────


class AppServer:
    """A real uvicorn server for one built app, on a daemon thread."""

    def __init__(self, app: Any) -> None:
        import uvicorn

        self._server: Any = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=0,
                ws="none",
                lifespan="on",
                log_level="warning",
            )
        )
        self._thread = threading.Thread(
            target=self._server.run, daemon=True, name="bx-browser-uvicorn"
        )

    def start(self) -> str:
        """Start + wait for readiness (bounded); return the base URL."""
        self._thread.start()
        pacer = threading.Event()  # never set — a bounded poll interval source
        deadline = time.monotonic() + 15.0
        while not self._server.started:
            if not self._thread.is_alive():
                raise RuntimeError("uvicorn server thread died during startup")
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn server failed to report started within 15 s")
            pacer.wait(0.01)
        port: int = self._server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10.0)
        if self._thread.is_alive():
            raise RuntimeError("uvicorn server failed to shut down within 10 s")


# ── the per-test page session (page + ledgers) ────────────────────────────


@dataclass
class PageSession:
    """One live page against one fixture app, with observability ledgers."""

    page: Any
    context: Any
    base_url: str
    console: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)
    #: When True (default) the harness FAILS the test at teardown if the
    #: console ledger is non-empty on an otherwise-passing test.
    expect_console_clean: bool = True

    def goto(self, path: str) -> None:
        """Navigate to *path* on the fixture app."""
        self.page.goto(self.base_url + path)

    def request_count(self, url_substring: str) -> int:
        """How many requests so far carry *url_substring* (URL ledger scan)."""
        return sum(1 for url in self.requests if url_substring in url)


# ── session-scoped raw sync Playwright ────────────────────────────────────


def _fallback_chromium_executable() -> Path:
    """Newest preinstalled ``chromium-*`` build under PLAYWRIGHT_BROWSERS_PATH.

    The container preinstalls Chromium builds (e.g. ``chromium-1194``) that may
    trail the playwright pin's expected build; running ``playwright install``
    locally is forbidden, so the harness relaunches on the newest installed
    build instead.
    """
    import os

    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root:
        raise RuntimeError(
            "default Chromium launch failed and PLAYWRIGHT_BROWSERS_PATH is unset — "
            "no fallback browser build to resolve"
        )
    candidates = sorted(
        Path(root).glob("chromium-*/chrome-linux/chrome"),
        key=lambda p: int(p.parts[-3].rsplit("-", 1)[1]),
    )
    if not candidates:
        raise RuntimeError(f"no chromium-*/chrome-linux/chrome build under {root!r}")
    return candidates[-1]


@pytest.fixture(scope="session")
def browser_session() -> Iterator[Any]:
    """Raw sync Playwright Chromium (headless, proxy-free), session-scoped."""
    from playwright.sync_api import Error, expect, sync_playwright

    expect.set_options(timeout=DEFAULT_TIMEOUT_MS)
    launch_args = ["--no-proxy-server"]
    pw = sync_playwright().start()
    try:
        try:
            browser = pw.chromium.launch(headless=True, args=launch_args)
        except Error as exc:
            if "Executable doesn't exist" not in str(exc):
                raise
            browser = pw.chromium.launch(
                headless=True,
                args=launch_args,
                executable_path=str(_fallback_chromium_executable()),
            )
        try:
            yield browser
        finally:
            browser.close()
    finally:
        pw.stop()


# ── the new_page factory ──────────────────────────────────────────────────


class NewPageFactory(Protocol):
    """The ``new_page`` fixture's factory contract.

    The ONE shared structural type every browser test module annotates its
    ``new_page`` parameter with (COMMON — Golden Rule #11). The fixture's
    return annotation is bound to this Protocol, so the contract is
    type-checked against the REAL factory and the two can never drift.
    """

    def __call__(
        self,
        fixture: Any,
        *,
        stub_widgets: bool = True,
        expect_console_clean: bool = True,
    ) -> PageSession:
        """Serve ``fixture`` on a fresh server+context; return its session."""


def _arm_route_policy(context: Any, *, stub_widgets: bool) -> None:
    """Abort ALL non-loopback egress; stub the widget pollers inert."""

    def handle(route: Any) -> None:
        parts = urlsplit(route.request.url)
        if parts.hostname != "127.0.0.1":
            route.abort()
            return
        if stub_widgets:
            stub = WIDGET_STUBS.get(parts.path)
            if stub is not None:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(stub),
                )
                return
        route.continue_()

    context.route("**/*", handle)


def _sanitize_nodeid(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", nodeid)


@pytest.fixture
def new_page(browser_session: Any, request: pytest.FixtureRequest) -> Iterator[NewPageFactory]:
    """Factory: fresh uvicorn server + browser context + page per call.

    Usage: ``session = new_page(build_db_app(tmp_path))`` then
    ``session.goto("/db")``. Keyword opts: ``stub_widgets=False`` serves the
    REAL widget endpoints (staleness module only); ``expect_console_clean=
    False`` skips the empty-console teardown assert (route-policy proof only —
    pageerror/dialog cleanliness is asserted unconditionally).
    """
    from tests.browser._fixtures import AppFixture

    live: list[tuple[PageSession, AppServer, AppFixture]] = []

    def factory(
        fixture: AppFixture,
        *,
        stub_widgets: bool = True,
        expect_console_clean: bool = True,
    ) -> PageSession:
        server = AppServer(fixture.app)
        base_url = server.start()
        context = browser_session.new_context()
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        _arm_route_policy(context, stub_widgets=stub_widgets)
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = context.new_page()
        session = PageSession(
            page=page,
            context=context,
            base_url=base_url,
            expect_console_clean=expect_console_clean,
        )

        def _on_dialog(dialog: Any) -> None:
            session.dialogs.append(dialog.message)
            dialog.dismiss()

        page.on("console", lambda msg: session.console.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda exc: session.page_errors.append(str(exc)))
        page.on("dialog", _on_dialog)
        page.on("request", lambda req: session.requests.append(req.url))
        page.on("response", lambda resp: session.responses.append(f"{resp.status} {resp.url}"))
        live.append((session, server, fixture))
        return session

    yield factory

    failed = _test_failed(request.node)
    problems: list[str] = []
    for index, (session, server, fixture) in enumerate(live):
        try:
            if failed:
                suffix = f"-{index}" if len(live) > 1 else ""
                artifact_dir = ARTIFACT_ROOT / (_sanitize_nodeid(request.node.nodeid) + suffix)
                artifact_dir.mkdir(parents=True, exist_ok=True)
                session.context.tracing.stop(path=str(artifact_dir / "trace.zip"))
                try:
                    session.page.screenshot(path=str(artifact_dir / "screenshot.png"))
                except Exception as exc:  # page may already be gone — note it
                    (artifact_dir / "screenshot-error.txt").write_text(str(exc), encoding="utf-8")
                (artifact_dir / "ledgers.json").write_text(
                    json.dumps(
                        {
                            "console": session.console,
                            "page_errors": session.page_errors,
                            "dialogs": session.dialogs,
                            "requests": session.requests,
                            "responses": session.responses,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            else:
                session.context.tracing.stop()
        finally:
            session.context.close()
            server.stop()
            fixture.cleanup()
        if not failed:
            if session.expect_console_clean and session.console:
                problems.append(f"session {index}: console not clean: {session.console}")
            if session.page_errors:
                problems.append(f"session {index}: pageerrors: {session.page_errors}")
            if session.dialogs:
                problems.append(f"session {index}: unexpected dialogs: {session.dialogs}")
    if problems:
        pytest.fail("browser harness cleanliness violated:\n" + "\n".join(problems))
