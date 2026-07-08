"""Always-collected, playwright-free sentinel for the browser truth suite.

A bare ``pytest`` run (no ``BRUTEX_BROWSER_TESTS=1``) collects ONLY this
module from ``tests/browser/`` — the conftest guard ``collect_ignore``s every
playwright-importing module — and this test emits an HONEST skip naming the
opt-in switch, so the suite's absence is always visible, never silent
(Golden Rule #10). When enabled it passes trivially, proving the guard's
enabled branch collected the suite.
"""

from __future__ import annotations

import pytest

from tests.browser._guards import ENABLE_ENV_VAR, browser_suite_enabled

pytestmark = pytest.mark.browser


def test_browser_suite_visibility() -> None:
    """Honest skip when disabled; trivial pass when the suite is enabled."""
    if not browser_suite_enabled():
        pytest.skip(
            f"browser truth suite disabled — set {ENABLE_ENV_VAR}=1 "
            "(CI runs it in the dedicated browser job)"
        )
    assert browser_suite_enabled()
