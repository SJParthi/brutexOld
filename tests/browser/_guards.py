"""Playwright-free suite-discipline law for the browser truth suite.

This module is IMPORTABLE FROM THE MAIN CI TEST JOB (stdlib only — no
playwright, no fastapi): ``tests/unit/test_browser_suite_discipline.py``
imports it to police ``tests/browser/`` on every push, so a flakiness escape
(a blind sleep, a retry plugin, an oversized timeout, a skip/xfail bypass)
can never land even when the browser job itself is not running.

ZERO-FLAKINESS LAW (DEC-DASHBOARD-BROWSER-TRUTH-001): retries are FORBIDDEN —
the suite polices itself. Synchronisation must be payload-driven
(``page.expect_response`` wrappers, ``expect(locator)`` polling, ``page.clock``),
never wall-clock sleeps, and every explicit timeout is capped at
:data:`MAX_TIMEOUT_MS`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

#: The browser-suite package directory (the scan root).
BROWSER_DIR: Final[Path] = Path(__file__).resolve().parent

#: Opt-in switch — the conftest collection guard + the CI browser job key off it.
ENABLE_ENV_VAR: Final[str] = "BRUTEX_BROWSER_TESTS"

#: Hard cap for ANY explicit timeout in the suite (ms). Also the harness's
#: default context/expect timeout (one source of truth — CLAUDE.md §2 Rule 11).
MAX_TIMEOUT_MS: Final[int] = 10_000


def browser_suite_enabled() -> bool:
    """True iff the operator/CI opted in via ``BRUTEX_BROWSER_TESTS=1``."""
    return os.environ.get(ENABLE_ENV_VAR) == "1"


#: ``(name, regex, reason)`` triples scanned line-by-line over every
#: ``tests/browser/*.py`` EXCEPT this module (which necessarily carries the
#: pattern text itself). ``oversized_timeout`` is special-cased: its captured
#: integer must exceed :data:`MAX_TIMEOUT_MS` to be a violation.
FORBIDDEN_PATTERNS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "wait_for_timeout",
        r"wait_for_timeout",
        "blind wall-clock wait — synchronise on a payload (expect_response / "
        "expect(locator) / page.clock) instead",
    ),
    (
        "time_sleep",
        r"\btime\.sleep\b",
        "blind wall-clock sleep — synchronise on a payload instead",
    ),
    (
        "retry_plugin",
        r"rerunfailures|\bflaky\b|\breruns?\b",
        "retry/rerun machinery is forbidden — the suite must be deterministic",
    ),
    (
        "mark_xfail",
        r"pytest\.mark\.xfail",
        "xfail is a flakiness escape hatch — fix the test or delete it",
    ),
    (
        "mark_skip",
        r"pytest\.mark\.skip",
        "skip/skipif marks are an escape hatch — the conftest collection guard "
        "is the ONLY sanctioned disable path",
    ),
    (
        "pytest_skip_call",
        r"pytest\.skip\s*\(",
        "runtime skips are an escape hatch (allow-listed ONLY for the honest "
        "always-collected sentinel test_suite_visibility.py)",
    ),
    (
        "oversized_timeout",
        r"timeout\s*=\s*([0-9][0-9_]*)",
        f"explicit timeout above the MAX_TIMEOUT_MS={MAX_TIMEOUT_MS} cap",
    ),
    (
        "network_idle_wait",
        r"networkidle",
        "network-idle waits are load-dependent and flaky — wait on a selector "
        "or a specific response instead",
    ),
)

#: Per-file allowlist: pattern names a specific module may legitimately carry.
#: The sentinel's honest opt-out skip is the ONLY entry (design-pinned).
FILE_ALLOWLIST: Final[dict[str, frozenset[str]]] = {
    "test_suite_visibility.py": frozenset({"pytest_skip_call"}),
}

# ── CI wiring pins (consumed by tests/unit/test_browser_suite_discipline.py) ──

#: The one CI workflow the browser job lives in (repo-root relative).
CI_WORKFLOW_PATH: Final[str] = ".github/workflows/ci.yml"

#: The browser job's YAML key (top-level job id).
CI_BROWSER_JOB: Final[str] = "browser"

#: Substrings that MUST appear inside the browser job's block: the opt-in env
#: var, the runtime bound, and the cov-free pytest invocation over the suite.
CI_BROWSER_JOB_PINS: Final[tuple[str, ...]] = (
    ENABLE_ENV_VAR,
    "timeout-minutes: 15",
    "pytest tests/browser",
)

#: The main test job's pytest path list — MUST NOT grow tests/browser (the
#: browser suite is cov-free by design; the main job runs with --cov).
MAIN_TEST_JOB_PYTEST_PATHS: Final[str] = "tests/unit tests/integration tests/property"


def _scan_targets() -> list[Path]:
    """Every suite module the discipline law applies to (self excluded)."""
    return sorted(p for p in BROWSER_DIR.glob("*.py") if p.name != Path(__file__).name)


def iter_violations() -> Iterator[str]:
    """Yield one human-readable line per forbidden-pattern hit in the suite.

    An empty iteration is the pass verdict. Deterministic order (sorted files,
    ascending line numbers) so failures are stable + diffable.
    """
    for path in _scan_targets():
        allowed = FILE_ALLOWLIST.get(path.name, frozenset())
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for name, pattern, reason in FORBIDDEN_PATTERNS:
                if name in allowed:
                    continue
                for match in re.finditer(pattern, line):
                    if name == "oversized_timeout":
                        value = int(match.group(1).replace("_", ""))
                        if value <= MAX_TIMEOUT_MS:
                            continue
                    yield f"{path.name}:{lineno}: [{name}] {reason} :: {line.strip()}"
