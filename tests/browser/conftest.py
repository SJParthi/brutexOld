"""Harness for the browser truth suite (DEC-DASHBOARD-BROWSER-TRUTH-001).

Contract (final-design §2):

* **Collection guard (three layers, RECURSIVE)** — the suite is opt-in via
  ``BRUTEX_BROWSER_TESTS=1``. (1) When disabled, every playwright-importing
  test module — subdirectories included (round-4 review: the prior top-level
  glob would have let a future ``tests/browser/<sub>/test_x.py`` be collected
  by a bare ``pytest`` run) — is ``collect_ignore``d for directory-shaped
  runs and only the TOP-LEVEL playwright-free sentinel
  (``test_suite_visibility.py``) is collected, emitting an HONEST skip — so a
  bare ``pytest`` run (e.g. verify-m4pro's) neither crashes on a missing
  playwright install nor hides the suite's existence. (2) ``collect_ignore``
  does NOT govern paths passed explicitly on the command line, so
  :func:`pytest_collection_modifyitems` skip-marks directly-addressed browser
  items (ANY depth under ``tests/browser/``) when the switch is unset — a
  direct ``pytest tests/browser/test_db_paging.py`` can never silently run
  Chromium outside the opt-in. (3) Each playwright module carries an
  env-gated ``pytest.importorskip`` so the same direct invocation on a
  playwright-less box is an honest skip, never a collection ImportError —
  while an ENABLED run (CI) keeps a missing playwright a LOUD import error,
  never a silent green skip.
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

import contextlib
import json
import re
import threading
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

import pytest

from tests.browser._guards import (
    BROWSER_DIR,
    ENABLE_ENV_VAR,
    MAX_TIMEOUT_MS,
    browser_suite_enabled,
)


def playwright_test_modules(root: Path | None = None) -> list[str]:
    """Conftest-relative posix paths of every playwright-importing test module.

    RECURSIVE (round-4 review): a test module in a future subdirectory (e.g.
    ``tests/browser/pages/test_x.py``) must not escape the collection guard —
    only the exact TOP-LEVEL sentinel is exempt (a subdirectory clone of its
    name earns nothing). Returned as posix-relative strings, the shape
    ``collect_ignore`` expects (entries resolve against this conftest's own
    directory). Computed by glob so adding a page-test module — at any depth —
    needs no conftest edit (DYNAMIC).
    """
    base = Path(__file__).resolve().parent if root is None else root
    sentinel = base / "test_suite_visibility.py"
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("test_*.py") if p != sentinel)


# When the suite is disabled, ignore every playwright-importing module; ONLY
# the top-level sentinel stays collected (its honest skip keeps the suite's
# absence visible).
collect_ignore = playwright_test_modules() if not browser_suite_enabled() else []


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip directly-addressed browser items when the suite is disabled.

    ``collect_ignore`` governs only directory-shaped collection — pytest
    treats explicit command-line paths as init paths and collects them
    regardless, so a direct ``pytest tests/browser/test_db_paging.py`` would
    otherwise RUN real Chromium tests with the opt-in switch unset. Items at
    ANY depth under the suite are marked (round-4 review: the prior
    exact-parent check let a future subdirectory item through); only the
    exact TOP-LEVEL sentinel stays un-marked — its own in-test honest skip
    names the switch.
    """
    if browser_suite_enabled():
        return
    marker = pytest.mark.skip(
        reason=(
            f"browser truth suite disabled — set {ENABLE_ENV_VAR}=1 "
            "(directly-addressed path bypasses the collect_ignore guard)"
        )
    )
    sentinel = BROWSER_DIR / "test_suite_visibility.py"
    for item in items:
        item_path = getattr(item, "path", None)
        if item_path is None:
            continue
        path = Path(item_path).resolve()
        if path != sentinel and path.is_relative_to(BROWSER_DIR):
            item.add_marker(marker)


#: One default for the browser context AND every explicit expect() timeout —
#: sourced from the guard cap so the two can never drift.
DEFAULT_TIMEOUT_MS: Final[int] = MAX_TIMEOUT_MS


def freeze_page_clock(page: Any) -> None:
    """Install Playwright's fake clock and PAUSE it (COMMON — the clock tests).

    ``page.clock.install()`` alone does NOT freeze time: the installed clock
    keeps ticking in REAL time (the driver's ``_syncRealTime`` advances it
    from the wall clock and fires due timers), so on a loaded runner a
    wall-clock stall >= a page timer's period would fire a phantom poll/retry
    and break the exact request-ledger asserts. ``pause_at`` one minute ahead
    stops the tick entirely — callers freeze BEFORE ``goto``, so no page
    timers exist yet and the jump fires nothing; thereafter timers fire ONLY
    under ``page.clock.fast_forward`` (probe-verified: a paused clock fires
    zero interval ticks across a multi-second real-time gap, and ``expect``
    locator polling still works).
    """
    page.clock.install()
    page.clock.pause_at(datetime.now(UTC) + timedelta(minutes=1))


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
        # Deliberately NOT the house docstring+`...` stub: CodeQL
        # py/ineffectual-statement flags a bare ellipsis ExprStmt (alert #688)
        # and pyright rejects a docstring-only body (reportReturnType), so this
        # uses the typing-spec protocol placeholder BOTH checkers accept.
        raise NotImplementedError


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
        # Construction containment (round-2 review): ``live`` is appended only
        # at the very END, so a raise mid-construction (e.g. the session
        # browser died on an earlier test and ``new_context`` throws) would
        # otherwise leak the already-started uvicorn daemon thread + the
        # fixture's open duckdb store for the rest of the pytest process — the
        # exact leak class the teardown loop below contains for REGISTERED
        # sessions. Cleanup here is best-effort (suppressed) so a secondary
        # teardown failure can never mask the original construction error,
        # which re-raises verbatim.
        server: AppServer | None = None
        context: Any = None
        try:
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
        except BaseException:
            if context is not None:
                with contextlib.suppress(Exception):
                    context.close()
            if server is not None:
                with contextlib.suppress(Exception):
                    server.stop()
            with contextlib.suppress(Exception):
                fixture.cleanup()
            raise
        live.append((session, server, fixture))
        return session

    yield factory

    failed = _test_failed(request.node)
    problems: list[str] = []
    for index, (session, server, fixture) in enumerate(live):
        # Per-session containment: NOTHING in one session's artifact capture
        # may abort a later session's teardown (a raise here would leak the
        # later sessions' uvicorn threads + duckdb stores for the rest of the
        # pytest process).
        try:
            if failed:
                suffix = f"-{index}" if len(live) > 1 else ""
                artifact_dir = ARTIFACT_ROOT / (_sanitize_nodeid(request.node.nodeid) + suffix)
                artifact_dir.mkdir(parents=True, exist_ok=True)
                # Ledgers FIRST: they depend only on in-process lists — never
                # on the browser being alive — so a crashed page/context (the
                # very failure class artifacts exist to diagnose) can't cost
                # the most diagnostic artifact.
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
                # Trace + screenshot each individually guarded: either can
                # raise on a dead context/page, and one dead artifact source
                # must not lose the other.
                try:
                    session.context.tracing.stop(path=str(artifact_dir / "trace.zip"))
                except Exception as exc:  # context may already be gone — note it
                    (artifact_dir / "trace-error.txt").write_text(str(exc), encoding="utf-8")
                try:
                    session.page.screenshot(path=str(artifact_dir / "screenshot.png"))
                except Exception as exc:  # page may already be gone — note it
                    (artifact_dir / "screenshot-error.txt").write_text(str(exc), encoding="utf-8")
            else:
                session.context.tracing.stop()
        except Exception as exc:
            problems.append(f"session {index}: artifact capture failed: {exc!r}")
        # Each cleanup step guarded independently: a dead context must not
        # leak THIS session's server/store, and no step may skip a later one.
        for label, step in (
            ("context.close", session.context.close),
            ("server.stop", server.stop),
            ("fixture.cleanup", fixture.cleanup),
        ):
            try:
                step()
            except Exception as exc:
                problems.append(f"session {index}: {label} failed: {exc!r}")
        if not failed:
            if session.expect_console_clean and session.console:
                problems.append(f"session {index}: console not clean: {session.console}")
            if session.page_errors:
                problems.append(f"session {index}: pageerrors: {session.page_errors}")
            if session.dialogs:
                problems.append(f"session {index}: unexpected dialogs: {session.dialogs}")
    if problems:
        pytest.fail("browser harness cleanliness violated:\n" + "\n".join(problems))
