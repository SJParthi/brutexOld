"""Harness selfcheck: every registry page renders console-clean in real Chromium.

Iterates :data:`~tests.browser._registry.PAGE_REGISTRY` (DYNAMIC — a new page
registered there is automatically covered) and proves, per page: the
fixture-backed REAL ``build_app`` app serves it over a real uvicorn socket,
the pinned ready selector appears, and the page produced ZERO console
messages / pageerrors / dialogs (the harness teardown enforces the ledgers).

Plus the route-policy-armed proof: an in-page fetch to a non-loopback origin
must die on ``route.abort()``'s OWN error code ``net::ERR_FAILED`` — a bare
promise rejection cannot distinguish the armed policy from plain DNS failure
(``.invalid`` never resolves, so an UNARMED context also rejects, with
``net::ERR_NAME_NOT_RESOLVED``), so the abort-specific code is what proves
the policy demonstrably live, not assumed.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from tests.browser._fixtures import build_db_app
from tests.browser._guards import browser_suite_enabled
from tests.browser._registry import PAGE_REGISTRY, PageSpec
from tests.browser.conftest import NewPageFactory

if not browser_suite_enabled():
    # Direct-file invocation outside the opt-in (collect_ignore governs only
    # directory-shaped collection): a playwright-less box gets an HONEST skip
    # here instead of a collection ImportError; the conftest modifyitems hook
    # skips the collected items. With the suite ENABLED a missing playwright
    # stays a LOUD import error — never a silent green skip in CI.
    pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("spec", PAGE_REGISTRY, ids=[spec.name for spec in PAGE_REGISTRY])
def test_page_renders_console_clean(
    spec: PageSpec, new_page: NewPageFactory, tmp_path: Path
) -> None:
    """Load the page, wait for its pinned data-ready DOM, stay ledger-clean."""
    session = new_page(spec.build(tmp_path))
    session.goto(spec.path)
    session.page.wait_for_selector(spec.ready_selector)
    if spec.nonempty_selector is not None:
        expect(session.page.locator(spec.nonempty_selector)).not_to_have_text("")
    # Ledger cleanliness is asserted NOW (fail in the test body, with trace
    # artifacts) and re-asserted by the harness teardown (late messages).
    assert session.page_errors == []
    assert session.dialogs == []
    assert session.console == []


def test_route_policy_blocks_external_egress(new_page: NewPageFactory, tmp_path: Path) -> None:
    """An external fetch dies on ``net::ERR_FAILED`` — the abort policy is live.

    Anti-vacuity: a rejected promise alone CANNOT distinguish armed from
    unarmed — ``example.invalid`` is a guaranteed-non-resolvable RFC 2606 TLD
    and the browser launches ``--no-proxy-server``, so even a disarmed
    context rejects the fetch (on DNS, ``net::ERR_NAME_NOT_RESOLVED``). The
    proof therefore pins ``route.abort()``'s OWN error code
    ``net::ERR_FAILED`` on the failed request (via ``expect_event`` — a
    bounded, payload-driven wait) and bans the DNS code from the console, so
    a silently-disarmed policy fails this test loudly (probe-verified: the
    unarmed context reads ``ERR_NAME_NOT_RESOLVED`` here, never
    ``ERR_FAILED``).

    ``expect_console_clean=False``: Chromium itself logs the blocked resource
    load to the console; the test instead asserts every console entry is the
    blocked-fetch noise and nothing else, and that no pageerror fired.
    """
    session = new_page(build_db_app(tmp_path), expect_console_clean=False)
    session.goto("/db")
    session.page.wait_for_selector("#tables .tbl")
    with session.page.expect_event(
        "requestfailed", lambda request: "example.invalid" in request.url
    ) as failed_info:
        verdict = session.page.evaluate(
            "() => fetch('https://example.invalid/x').then(() => 'resolved', () => 'rejected')"
        )
    assert verdict == "rejected"
    # route.abort()'s error code — the armed-vs-unarmed distinguisher.
    assert failed_info.value.failure == "net::ERR_FAILED"
    assert session.page_errors == []
    assert session.dialogs == []
    for entry in session.console:
        assert "example.invalid" in entry or "Failed to load resource" in entry, entry
    # The DNS failure code would mean the request left the route policy.
    assert not any("ERR_NAME_NOT_RESOLVED" in entry for entry in session.console)


class _DeadBrowser:
    """Session-browser stand-in whose ``new_context`` raises (browser died)."""

    def new_context(self) -> Any:
        raise RuntimeError("session browser is dead")


class TestFactoryConstructionContainment:
    """Round-2 review regression: a factory construction failure leaks nothing.

    The ``new_page`` factory registers a session for teardown only at the very
    END of construction, so a raise BETWEEN ``server.start()`` and that
    registration (the session-scoped browser died on an earlier test →
    ``new_context`` throws) previously left the already-started uvicorn daemon
    thread running and the fixture's duckdb store open for the rest of the
    pytest process. The class-scoped ``browser_session`` override injects
    exactly that failure against the REAL factory + a REAL fixture app.
    """

    @pytest.fixture
    def browser_session(self) -> _DeadBrowser:
        """Override the session browser with the dead stand-in (class-local)."""
        return _DeadBrowser()

    def test_construction_failure_stops_server_and_cleans_fixture(
        self, new_page: NewPageFactory, tmp_path: Path
    ) -> None:
        fixture = build_db_app(tmp_path)
        cleaned: list[str] = []
        real_cleanup = fixture.cleanup

        def _tracking_cleanup() -> None:
            cleaned.append("cleanup")
            real_cleanup()

        fixture.cleanup = _tracking_cleanup
        # Precondition: every earlier test's server thread was joined at its
        # own teardown — nothing alive to mis-attribute to this test.
        assert [t for t in threading.enumerate() if t.name == "bx-browser-uvicorn"] == []
        with pytest.raises(RuntimeError, match="session browser is dead"):
            new_page(fixture)
        # The construction error re-raised verbatim AND: the fixture's store
        # was closed exactly once, and the started uvicorn thread was stopped
        # (a leaked daemon thread would still be alive + enumerable here).
        assert cleaned == ["cleanup"]
        assert [t for t in threading.enumerate() if t.name == "bx-browser-uvicorn"] == []
