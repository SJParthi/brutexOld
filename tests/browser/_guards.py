"""Playwright-free suite-discipline law for the browser truth suite.

This module is IMPORTABLE FROM THE MAIN CI TEST JOB (stdlib only — no
playwright, no fastapi): ``tests/unit/test_browser_suite_discipline.py``
imports it to police ``tests/browser/`` on every push, so a flakiness escape
(a blind sleep, a retry plugin, an oversized timeout, a skip/xfail bypass)
can never land even when the browser job itself is not running.

ZERO-FLAKINESS LAW (DEC-DASHBOARD-BROWSER-TRUTH-001): retries are FORBIDDEN —
the suite polices itself. Synchronisation must be payload-driven
(``page.expect_response`` wrappers, ``expect(locator)`` polling, ``page.clock``),
never wall-clock sleeps, and every explicit LITERAL timeout — including a
literal ``set_default_timeout``/``set_default_navigation_timeout`` argument,
in ANY Python numeric-literal spelling (decimal, underscored, float,
scientific ``6e4``, hex/octal/binary ``0x186A0``) — is VALUE-checked against
:data:`MAX_TIMEOUT_MS`; a digit-leading token the parser cannot read is a
violation OUTRIGHT (fail-closed, round-2 review fix — the previous
digits-only capture read ``timeout=6e4`` as 6 and ``0x186A0`` as 0, silently
passing 60 s / 100 s literals under the 10 s cap).

HONEST ENVELOPE (Golden Rule #10): the scan is a line-regex over the suite
sources, not an AST/dataflow analysis — a timeout smuggled through a variable
or arithmetic expression (``timeout=SLOW_MS``) is beyond its reach and is
left to code review; the sleep-capable ``time`` import forms it CAN see
(``from time import …``, ``import time as …``, ``getattr(time, …)``,
``asyncio.sleep``) are banned wholesale so ``time.sleep`` cannot be smuggled
past the direct-call pattern by renaming.
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


#: A digit-leading literal token in a timeout position — deliberately BROAD
#: (first digit through any ``[A-Za-z0-9_.]`` run) so float (``10.5``),
#: scientific (``6e4`` / ``1e9``), underscored (``1_000_000``) and
#: hex/octal/binary (``0x186A0``) spellings are all captured whole and
#: VALUE-checked by :func:`_timeout_literal_violates`; anything captured that
#: cannot be parsed is flagged fail-closed instead of silently passing.
_NUMERIC_LITERAL: Final[str] = r"([0-9][\w.]*)"

#: ``(name, regex, reason)`` triples scanned line-by-line over every
#: ``tests/browser/*.py`` EXCEPT this module (which necessarily carries the
#: pattern text itself). The ``oversized_*`` patterns are special-cased:
#: their captured numeric literal is value-parsed and must exceed
#: :data:`MAX_TIMEOUT_MS` to be a violation — except an unparseable
#: digit-leading token, which is a violation OUTRIGHT (fail-closed).
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
        "time_import_evasion",
        r"from\s+time\s+import|import\s+time\s+as\s",
        "sleep-capable time import forms (from-import / aliasing) defeat the "
        "time.sleep pattern — use plain `import time` and direct member calls",
    ),
    (
        "time_getattr_evasion",
        r"getattr\s*\(\s*time\b",
        "dynamic time-member access defeats the time.sleep pattern — call members directly",
    ),
    (
        "asyncio_sleep",
        r"\basyncio\.sleep\b",
        "blind async wall-clock sleep — synchronise on a payload instead",
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
        rf"timeout\s*=\s*{_NUMERIC_LITERAL}",
        f"explicit literal timeout above the MAX_TIMEOUT_MS={MAX_TIMEOUT_MS} cap "
        "(an unparseable digit-leading literal is flagged fail-closed)",
    ),
    (
        "oversized_default_timeout",
        rf"set_default(?:_navigation)?_timeout\s*\(\s*{_NUMERIC_LITERAL}",
        f"literal set_default_timeout above the MAX_TIMEOUT_MS={MAX_TIMEOUT_MS} "
        "cap — it silently raises the effective cap for EVERY subsequent wait",
    ),
    (
        "network_idle_wait",
        r"networkidle",
        "network-idle waits are load-dependent and flaky — wait on a selector "
        "or a specific response instead",
    ),
)

#: Per-file allowlist: pattern names a specific module may legitimately carry
#: (design-pinned): the sentinel's honest opt-out skip, and the conftest's
#: skip MARKER for directly-addressed items when the suite is disabled
#: (``collect_ignore`` does not govern explicit command-line paths — the
#: marker is part of the collection guard, not a flakiness escape).
FILE_ALLOWLIST: Final[dict[str, frozenset[str]]] = {
    "test_suite_visibility.py": frozenset({"pytest_skip_call"}),
    "conftest.py": frozenset({"mark_skip"}),
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

#: The browser job's pytest STEP run line, pinned EXACTLY (round-2 review):
#: the substring pins above are presence checks, so a narrowed invocation
#: (``pytest tests/browser/test_suite_visibility.py -q`` — the sentinel-only
#: run) would keep every substring green while executing ZERO Chromium
#: proofs. The discipline test asserts the pytest step's run line equals this
#: verbatim, that the opt-in env var is scoped to THAT step, and that the
#: step carries no step-level ``if:``.
CI_BROWSER_PYTEST_RUN_LINE: Final[str] = "run: pytest tests/browser -q"

#: The main test job's pytest path list — MUST NOT grow tests/browser (the
#: browser suite is cov-free by design; the main job runs with --cov).
MAIN_TEST_JOB_PYTEST_PATHS: Final[str] = "tests/unit tests/integration tests/property"


def _scan_targets() -> list[Path]:
    """Every suite module the discipline law applies to (self excluded)."""
    return sorted(p for p in BROWSER_DIR.glob("*.py") if p.name != Path(__file__).name)


def _timeout_literal_violates(token: str) -> bool:
    """True iff a captured digit-leading timeout literal breaks the cap.

    Parses every Python numeric-literal family: ``int(…, 0)`` covers decimal,
    underscored, and hex/octal/binary spellings; ``float`` covers float and
    scientific notation (``6e4``). A digit-leading token NEITHER can read
    (e.g. ``1j``, or a sign-split ``1e+9`` whose exponent the capture cannot
    span) is a violation OUTRIGHT — fail-closed, never a silent pass
    (round-2 review: the previous digits-only capture parsed ``6e4`` as 6 and
    ``0x186A0`` as 0, positively validating 60 s / 100 s literals).
    """
    cleaned = token.replace("_", "")
    try:
        value = float(int(cleaned, 0))
    except ValueError:
        try:
            value = float(cleaned)
        except ValueError:
            return True
    return value > MAX_TIMEOUT_MS


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
                    if name.startswith("oversized_") and not _timeout_literal_violates(
                        match.group(1)
                    ):
                        continue
                    yield f"{path.name}:{lineno}: [{name}] {reason} :: {line.strip()}"
