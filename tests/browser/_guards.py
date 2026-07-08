"""Playwright-free suite-discipline law for the browser truth suite.

This module is IMPORTABLE FROM THE MAIN CI TEST JOB (stdlib only — no
playwright, no fastapi): ``tests/unit/test_browser_suite_discipline.py``
imports it to police ``tests/browser/`` RECURSIVELY (every ``*.py`` under the
suite, subdirectories included — round-4 review; the prior top-level glob let
a future ``tests/browser/<sub>/`` module escape the law) on every push, so a
flakiness escape (a blind sleep, a retry plugin, an oversized timeout, a
skip/xfail bypass) cannot land even when the browser job itself is not
running.

ZERO-FLAKINESS LAW (DEC-DASHBOARD-BROWSER-TRUTH-001): retries are FORBIDDEN —
the suite polices itself. Synchronisation must be payload-driven
(``page.expect_response`` wrappers, ``expect(locator)`` polling, ``page.clock``),
never wall-clock sleeps, and every explicit LITERAL timeout — including a
literal ``set_default_timeout``/``set_default_navigation_timeout`` argument
and a literal dict-key ``{"timeout": …}`` (the ``**{…}`` call-spread smuggle —
round-4 review), in ANY digit- or dot-leading Python numeric-literal spelling
(decimal, underscored, float incl. dot-leading ``.5e9``, scientific ``6e4``,
hex/octal/binary ``0x186A0``), optionally paren-wrapped (``timeout=(60000)``),
unary-plus-prefixed (``timeout=+600000``) or formatter-wrapped across lines
(the value-checked patterns scan each file's WHOLE text, so a line-split
``set_default_timeout(\\n    60000\\n)`` is still seen) — is VALUE-checked
against :data:`MAX_TIMEOUT_MS`; a captured token the parser cannot read is a
violation OUTRIGHT (fail-closed; round-2 review fix — the digits-only capture
read ``timeout=6e4`` as 6 and ``0x186A0`` as 0 — hardened again by the
round-3 review, whose digit-leading capture + line scan never saw the
dot-leading / unary-plus / paren-wrapped / line-split shapes at all).

A SECONDS-denominated ``Event``/``Condition``-style ``.wait(<literal>)`` gets
its OWN cap (:data:`MAX_WAIT_SECONDS` — round-4 review: ``Event().wait(30.0)``
is a 30 s blind sleep, yet folding it into the ms cap would mis-validate it as
``30 < 10_000``); an argument-less ``.wait()`` (an unbounded block) is banned
outright.

SKIP/XFAIL shapes (round-4 review — the mark patterns alone let three literal
call forms through): the runtime ``pytest.xfail(…)`` call, ``unittest``'s
``SkipTest`` (raised or imported, any spelling carrying the bare name), and
``pytest.importorskip(…)`` of anything but the ONE sanctioned literal argument
``"playwright.sync_api"`` (the env-gated guard idiom the four playwright
modules carry — any other argument, e.g. an uninstalled package name, would
silently skip a whole module in the CI browser job, where pytest exits 0 on
skips; an argument the scan cannot read as that exact literal is flagged
fail-closed) are all banned, as are the pytest import-aliasing routes around
them (``from pytest import …`` / ``import pytest as …`` / ``getattr(pytest,
…)`` / ``__import__("pytest")`` / ``import_module("pytest")``, mirroring the
``time`` bans).

HONEST ENVELOPE (Golden Rule #10): the scan is regex over the suite sources,
not an AST/dataflow analysis — a timeout smuggled through a variable or an
arithmetic expression (``timeout=SLOW_MS``, ``timeout=6 * 10_000``), a
variable-indirected wait interval (``ev.wait(POLL_S)``), and a
seconds-denominated ``timeout=`` kwarg on a blocking stdlib API OTHER than
``.wait`` (``queue.get(timeout=30)``, ``thread.join(timeout=…)`` — parsed
against the ms cap, under which small-seconds literals pass) are beyond its
reach and left to code review; the sleep-capable ``time`` import forms it CAN
see (``from time import …``, ``import time as …``, ``getattr(time, …)``,
``__import__("time")`` / ``import_module("time")``, ``asyncio.sleep``) are
banned wholesale, so the LITERAL renaming routes around the direct
``time.sleep`` pattern are closed — while a dynamically-CONSTRUCTED import
string (``__import__("ti" "me")``) is likewise beyond a regex and left to
review; the ban closes the literal smuggles, not every conceivable one.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

#: The browser-suite package directory (the scan root).
BROWSER_DIR: Final[Path] = Path(__file__).resolve().parent

#: Opt-in switch — the conftest collection guard + the CI browser job key off it.
ENABLE_ENV_VAR: Final[str] = "BRUTEX_BROWSER_TESTS"

#: Hard cap for ANY explicit timeout in the suite (ms). Also the harness's
#: default context/expect timeout (one source of truth — CLAUDE.md §2 Rule 11).
MAX_TIMEOUT_MS: Final[int] = 10_000

#: Hard cap for a literal ``Event``/``Condition``-style ``.wait(…)`` argument
#: (SECONDS — those stdlib waits are seconds-denominated, unlike the
#: ms-denominated Playwright timeouts, so the ms cap would mis-validate them;
#: round-4 review). A bounded poll interval must be sub-second-scale (the
#: harness's own pacer uses 0.01 s); a longer literal is a blind sleep.
MAX_WAIT_SECONDS: Final[float] = 1.0


def browser_suite_enabled() -> bool:
    """True iff the operator/CI opted in via ``BRUTEX_BROWSER_TESTS=1``."""
    return os.environ.get(ENABLE_ENV_VAR) == "1"


#: A digit- or dot-leading literal token in a timeout position — deliberately
#: BROAD (first ``[0-9.]`` through any ``[A-Za-z0-9_.]`` run) so float
#: (``10.5``), DOT-LEADING float (``.5e9`` — round-3 review), scientific
#: (``6e4`` / ``1e9``), underscored (``1_000_000``) and hex/octal/binary
#: (``0x186A0``) spellings are all captured whole and VALUE-checked by
#: :func:`_timeout_literal_violates`; anything captured that cannot be parsed
#: is flagged fail-closed instead of silently passing.
_NUMERIC_LITERAL: Final[str] = r"([0-9.][\w.]*)"

#: Tolerated junk BETWEEN a timeout position and its literal: parentheses
#: (``timeout=(60000)``), unary plus (``timeout=+600000``) and ANY whitespace
#: — including newlines, because the value-checked patterns run over each
#: file's WHOLE text (not line-by-line), so a formatter-wrapped
#: ``set_default_timeout(\n    60000\n)`` is still captured (round-3 review;
#: the prior line scan structurally could not see a split literal).
_LITERAL_PREFIX: Final[str] = r"[(\s+]*"

#: ``(name, regex, reason)`` triples scanned over every ``*.py`` under
#: ``tests/browser/`` (RECURSIVE) EXCEPT this module (which necessarily
#: carries the pattern text itself). Patterns named in
#: :data:`_VALUE_CHECKED_PATTERNS` are special-cased twice: they run over each
#: file's WHOLE text (``\s`` spans newlines — a formatter-wrapped literal
#: argument is still seen; every other pattern is a line scan), and their
#: single capture group must fail its validator to be a violation — except a
#: captured token the validator cannot read, which is a violation OUTRIGHT
#: (fail-closed).
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
        "time_dynamic_import_evasion",
        r"__import__\s*\(\s*[\"']time[\"']|import_module\s*\(\s*[\"']time[\"']",
        "dynamic time import (__import__ / importlib.import_module) defeats "
        "the time.sleep pattern — use plain `import time` and direct member calls",
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
        "pytest_xfail_call",
        r"pytest\.xfail\s*\(",
        "the runtime xfail CALL is the same flakiness escape hatch as the mark "
        "— fix the test or delete it (round-4 review: it previously scanned "
        "clean unless its message happened to contain a retry keyword)",
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
        "unittest_skiptest",
        r"\bSkipTest\b",
        "unittest-style SkipTest (raised or imported) is a skip escape hatch — "
        "the conftest collection guard is the ONLY sanctioned disable path",
    ),
    (
        "pytest_import_evasion",
        r"from\s+pytest\s+import|import\s+pytest\s+as\s",
        "pytest import aliasing defeats the skip/xfail/importorskip patterns — "
        "use plain `import pytest` and direct member calls",
    ),
    (
        "pytest_getattr_evasion",
        r"getattr\s*\(\s*pytest\b",
        "dynamic pytest-member access defeats the skip/xfail patterns — call members directly",
    ),
    (
        "pytest_dynamic_import_evasion",
        r"__import__\s*\(\s*[\"']pytest[\"']|import_module\s*\(\s*[\"']pytest[\"']",
        "dynamic pytest import defeats the skip/xfail patterns — use plain "
        "`import pytest` and direct member calls",
    ),
    (
        "importorskip_unsanctioned",
        r"pytest\.importorskip\s*\(\s*([^)\n]*)",
        'pytest.importorskip of anything but the sanctioned literal "playwright.sync_api" '
        "silently skips a whole module in the CI browser job (pytest exits 0 on skips → "
        "green job with those Chromium proofs never executed); an argument the scan "
        "cannot read as that exact literal is flagged fail-closed",
    ),
    (
        "unbounded_wait",
        r"\.wait\s*\(\s*\)",
        "an argument-less .wait() blocks forever — bound it with a sub-second "
        f"literal poll interval (<= MAX_WAIT_SECONDS={MAX_WAIT_SECONDS})",
    ),
    (
        "oversized_timeout",
        rf"timeout\s*=\s*{_LITERAL_PREFIX}{_NUMERIC_LITERAL}",
        f"explicit literal timeout above the MAX_TIMEOUT_MS={MAX_TIMEOUT_MS} cap "
        "(an unparseable captured literal is flagged fail-closed)",
    ),
    (
        "oversized_dict_timeout",
        rf"[\"']timeout[\"']\s*:\s*{_LITERAL_PREFIX}{_NUMERIC_LITERAL}",
        f"explicit literal dict-key timeout above the MAX_TIMEOUT_MS={MAX_TIMEOUT_MS} cap "
        "— a **{'timeout': …} call spread is the same literal in a timeout position "
        "(round-4 review)",
    ),
    (
        "oversized_default_timeout",
        rf"set_default(?:_navigation)?_timeout\s*\(\s*{_LITERAL_PREFIX}{_NUMERIC_LITERAL}",
        f"literal set_default_timeout above the MAX_TIMEOUT_MS={MAX_TIMEOUT_MS} "
        "cap — it silently raises the effective cap for EVERY subsequent wait",
    ),
    (
        "overlong_wait",
        rf"\.wait\s*\(\s*(?:timeout\s*=\s*)?{_LITERAL_PREFIX}{_NUMERIC_LITERAL}",
        f"literal Event/Condition-style .wait() above MAX_WAIT_SECONDS={MAX_WAIT_SECONDS} — "
        "a SECONDS-denominated blind sleep the ms cap would mis-validate (round-4 "
        "review); only a sub-second bounded poll interval is sanctioned",
    ),
    (
        "network_idle_wait",
        r"networkidle",
        "network-idle waits are load-dependent and flaky — wait on a selector "
        "or a specific response instead",
    ),
)

#: Per-file allowlist keyed on the suite-RELATIVE posix path (TOP-LEVEL files
#: only by construction — a subdirectory clone of an allow-listed name earns
#: nothing; round-4 review): pattern names a specific module may legitimately
#: carry (design-pinned): the sentinel's honest opt-out skip, and the
#: conftest's skip MARKER for directly-addressed items when the suite is
#: disabled (``collect_ignore`` does not govern explicit command-line paths —
#: the marker is part of the collection guard, not a flakiness escape).
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
    """Every suite module the discipline law applies to (self excluded).

    RECURSIVE (round-4 review): the prior top-level ``glob("*.py")`` let a
    future ``tests/browser/<sub>/`` helper or test module carry violations
    invisibly; ``rglob`` closes that. Only THIS file is excluded (by resolved
    path, so a subdirectory module named ``_guards.py`` is still scanned).
    """
    self_path = Path(__file__).resolve()
    return sorted(p for p in BROWSER_DIR.rglob("*.py") if p.resolve() != self_path)


def _parse_numeric_literal(token: str) -> float | None:
    """Parse a captured numeric literal; ``None`` when unreadable.

    ``int(…, 0)`` covers decimal, underscored, and hex/octal/binary
    spellings; ``float`` covers float and scientific notation (``6e4``),
    including dot-leading floats (``.5e9`` — round-3 review).
    """
    cleaned = token.replace("_", "")
    try:
        return float(int(cleaned, 0))
    except ValueError:
        try:
            return float(cleaned)
        except ValueError:
            return None


def _timeout_literal_violates(token: str) -> bool:
    """True iff a captured ms-denominated timeout literal breaks the cap.

    A captured token the parser cannot read (e.g. ``1j``, or a sign-split
    ``1e+9`` whose exponent the capture cannot span) is a violation OUTRIGHT
    — fail-closed, never a silent pass (round-2 review: the previous
    digits-only capture parsed ``6e4`` as 6 and ``0x186A0`` as 0, positively
    validating 60 s / 100 s literals).
    """
    value = _parse_numeric_literal(token)
    return True if value is None else value > MAX_TIMEOUT_MS


def _wait_literal_violates(token: str) -> bool:
    """True iff a captured SECONDS-denominated ``.wait`` literal breaks its cap.

    Round-4 review: ``Event().wait(30.0)`` is a 30 s blind wall-clock sleep,
    but ``Event.wait`` takes SECONDS — folding it into the ms-denominated
    timeout check would mis-validate it (``30 < 10_000``), so it gets its own
    :data:`MAX_WAIT_SECONDS` cap. Unparseable tokens are violations outright
    (fail-closed, same contract as the timeout validator).
    """
    value = _parse_numeric_literal(token)
    return True if value is None else value > MAX_WAIT_SECONDS


#: The ONE sanctioned ``pytest.importorskip`` argument spelling (either quote
#: style): the env-gated playwright guard the four browser test modules carry.
_SANCTIONED_IMPORTORSKIP_ARGS: Final[frozenset[str]] = frozenset(
    {'"playwright.sync_api"', "'playwright.sync_api'"}
)


def _importorskip_arg_violates(token: str) -> bool:
    """True iff a captured ``pytest.importorskip`` argument is unsanctioned.

    Round-4 review: an UNGATED ``pytest.importorskip("absent_pkg")`` looks
    idiomatic (the four playwright modules use the call form) yet silently
    skips a whole module in the CI browser job. Only the exact literal
    ``"playwright.sync_api"`` is sanctioned — playwright is installed in the
    enabled CI job, so that literal can never mask a Chromium proof there;
    ANY other captured argument (another package, a variable, extra
    arguments) is a violation — fail-closed.
    """
    return token.strip() not in _SANCTIONED_IMPORTORSKIP_ARGS


#: Patterns whose single capture group is VALUE-checked instead of
#: shape-banned. These scan each file's WHOLE text (their ``\s`` runs span
#: newlines, so a formatter-wrapped argument is still seen — round-3 review);
#: every other pattern is a line scan.
_VALUE_CHECKED_PATTERNS: Final[dict[str, Callable[[str], bool]]] = {
    "oversized_timeout": _timeout_literal_violates,
    "oversized_dict_timeout": _timeout_literal_violates,
    "oversized_default_timeout": _timeout_literal_violates,
    "overlong_wait": _wait_literal_violates,
    "importorskip_unsanctioned": _importorskip_arg_violates,
}


def iter_violations() -> Iterator[str]:
    """Yield one human-readable line per forbidden-pattern hit in the suite.

    An empty iteration is the pass verdict. Deterministic order (sorted files,
    ascending line numbers, pattern order within a line) so failures are
    stable + diffable. Paths are reported suite-relative (subdirectory
    modules are scanned too — round-4 review). Patterns named in
    :data:`_VALUE_CHECKED_PATTERNS` run over each file's WHOLE text and flag
    only when their validator says so (fail-closed on unreadable captures);
    every other pattern is a line scan.
    """
    for path in _scan_targets():
        rel = path.relative_to(BROWSER_DIR).as_posix()
        allowed = FILE_ALLOWLIST.get(rel, frozenset())
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        hits: list[tuple[int, int, str]] = []
        for order, (name, pattern, _reason) in enumerate(FORBIDDEN_PATTERNS):
            if name in allowed:
                continue
            validator = _VALUE_CHECKED_PATTERNS.get(name)
            if validator is not None:
                for match in re.finditer(pattern, text):
                    if not validator(match.group(1)):
                        continue
                    lineno = text.count("\n", 0, match.start()) + 1
                    hits.append((lineno, order, lines[lineno - 1].strip()))
                continue
            for lineno, line in enumerate(lines, start=1):
                for _ in re.finditer(pattern, line):
                    hits.append((lineno, order, line.strip()))
        for lineno, order, snippet in sorted(hits):
            name, _pattern, reason = FORBIDDEN_PATTERNS[order]
            yield f"{rel}:{lineno}: [{name}] {reason} :: {snippet}"
