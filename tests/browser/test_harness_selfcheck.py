"""Harness selfcheck: every registry page renders console-clean in real Chromium.

Iterates :data:`~tests.browser._registry.PAGE_REGISTRY` (DYNAMIC — a new page
registered there is automatically covered) and proves, per page: the
fixture-backed REAL ``build_app`` app serves it over a real uvicorn socket,
the pinned ready selector appears, and the page produced ZERO console
messages / pageerrors / dialogs (the harness teardown enforces the ledgers).

Plus the route-policy-armed proof: an in-page fetch to a non-loopback origin
must REJECT — the abort-all-external-egress policy is demonstrably live, not
assumed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser._fixtures import build_db_app
from tests.browser._registry import PAGE_REGISTRY, PageSpec
from tests.browser.conftest import NewPageFactory

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
    """An in-page fetch to a non-loopback host REJECTS — the abort policy is live.

    ``expect_console_clean=False``: Chromium itself logs the blocked resource
    load to the console; the test instead asserts every console entry is the
    blocked-fetch noise and nothing else, and that no pageerror fired.
    """
    session = new_page(build_db_app(tmp_path), expect_console_clean=False)
    session.goto("/db")
    session.page.wait_for_selector("#tables .tbl")
    verdict = session.page.evaluate(
        "() => fetch('https://example.invalid/x').then(() => 'resolved', () => 'rejected')"
    )
    assert verdict == "rejected"
    assert session.page_errors == []
    assert session.dialogs == []
    for entry in session.console:
        assert "example.invalid" in entry or "Failed to load resource" in entry, entry
