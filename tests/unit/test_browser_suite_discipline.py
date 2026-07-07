"""Suite-discipline gate for the browser truth suite (DEC-DASHBOARD-BROWSER-TRUTH-001).

Runs in the MAIN CI test job (a plain unit test — playwright-free: it imports
only the stdlib-only ``tests.browser._guards``), so the zero-flakiness law and
the CI wiring are policed on EVERY push even though the browser job itself
runs separately:

(a) forbidden-pattern scan — no blind sleeps / retries / oversized timeouts /
    skip escapes anywhere in ``tests/browser/*.py``;
(b) ci.yml wiring pins — the ``browser`` job exists, is wired into the
    ``ci-ok`` aggregate in ALL THREE places (``needs:`` + the echo block + the
    for-loop enumeration), carries NO job-level ``if:`` escape (the aggregate
    treats skipped==pass, so a conditional browser job could merge green with
    a broken DOM), runs cov-free, and is runtime-bounded;
(c) main-CI-path exclusion — the main test job's pytest invocations never
    grow ``tests/browser`` (the browser suite is deliberately outside the
    coverage paths; its opt-in env var keeps bare ``pytest`` runs — e.g.
    verify-m4pro.yml's ``pytest -q`` — protected via the conftest collection
    guard + the always-collected honest-skip sentinel).
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.browser import _guards

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_YML = _REPO_ROOT / _guards.CI_WORKFLOW_PATH


def _job_block(text: str, job: str) -> str:
    """The YAML block of top-level job ``job`` (2-space indent) in ci.yml.

    Slices from the job key to the next top-level job key; comment banners
    between jobs (indented ``#`` lines) are harmless trailing content.
    """
    match = re.search(
        rf"^  {re.escape(job)}:\n(.*?)(?=^  [a-z0-9_-]+:[ \t]*$|\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"ci.yml lost the top-level `{job}:` job"
    return match.group(1)


def test_no_forbidden_patterns_in_browser_suite() -> None:
    """(a) The zero-flakiness law: zero forbidden-pattern hits in the suite."""
    violations = list(_guards.iter_violations())
    assert violations == [], "browser-suite discipline violations:\n" + "\n".join(violations)


def test_guards_cap_matches_law() -> None:
    """The timeout cap is the pinned design value (one source of truth)."""
    assert _guards.MAX_TIMEOUT_MS == 10_000


def test_browser_job_exists_with_required_pins() -> None:
    """(b) The browser job exists, bounded + opted-in + running the suite."""
    text = _CI_YML.read_text(encoding="utf-8")
    block = _job_block(text, _guards.CI_BROWSER_JOB)
    for pin in _guards.CI_BROWSER_JOB_PINS:
        assert pin in block, (
            f"the `{_guards.CI_BROWSER_JOB}` job in {_guards.CI_WORKFLOW_PATH} lost the "
            f"required pin {pin!r} (opt-in env var / timeout bound / suite invocation)"
        )
    assert f'{_guards.ENABLE_ENV_VAR}: "1"' in block, (
        'the browser job must set the opt-in env var to exactly "1" — the conftest '
        "collection guard requires that literal value"
    )


def test_browser_job_has_no_job_level_if_escape() -> None:
    """(b) No job-level `if:` — skipped==pass in ci-ok would merge green."""
    text = _CI_YML.read_text(encoding="utf-8")
    block = _job_block(text, _guards.CI_BROWSER_JOB)
    job_level_ifs = [line for line in block.splitlines() if re.match(r"^    if\s*:", line)]
    assert job_level_ifs == [], (
        "the browser job carries a job-level `if:` — a conditional browser job is a "
        "silent merge-green escape hatch because the ci-ok loop treats skipped as pass: "
        f"{job_level_ifs}"
    )


def test_browser_job_pytest_invocation_is_cov_free() -> None:
    """(b) The browser suite runs WITHOUT --cov (a truth gate, not coverage)."""
    text = _CI_YML.read_text(encoding="utf-8")
    block = _job_block(text, _guards.CI_BROWSER_JOB)
    pytest_lines = [line for line in block.splitlines() if "pytest tests/browser" in line]
    assert len(pytest_lines) == 1, (
        f"expected exactly one `pytest tests/browser` invocation in the browser job, "
        f"got {pytest_lines}"
    )
    assert "--cov" not in pytest_lines[0], (
        "the browser job's pytest invocation must stay cov-free — the DOM layer is a "
        f"truth gate, not a coverage surface: {pytest_lines[0]!r}"
    )


def test_browser_job_wired_into_ci_ok_all_three_places() -> None:
    """(b) ci-ok wiring: needs + echo block + for-loop enumeration."""
    text = _CI_YML.read_text(encoding="utf-8")
    block = _job_block(text, "ci-ok")
    needs = block.split("steps:", 1)[0]
    assert re.search(r"^      - browser\s*$", needs, flags=re.MULTILINE), (
        "ci-ok `needs:` lost the browser job — a red browser job would no longer "
        "block the aggregate"
    )
    result_ref = "${{ needs.browser.result }}"
    echo_lines = [
        line
        for line in block.splitlines()
        if result_ref in line and line.lstrip().startswith("echo ")
    ]
    assert len(echo_lines) == 1, (
        f"ci-ok's gate-results echo block lost the browser line: {echo_lines}"
    )
    loop_lines = [line for line in block.splitlines() if line.strip() == f'"{result_ref}" \\']
    assert len(loop_lines) == 1, (
        "ci-ok's `for result in` enumeration lost the browser result — the loop is the "
        f"actual pass/fail teeth, not the echo: {loop_lines}"
    )


def test_main_test_job_paths_exclude_browser_suite() -> None:
    """(c) The main (coverage-gated) test job never collects tests/browser."""
    text = _CI_YML.read_text(encoding="utf-8")
    block = _job_block(text, "test")
    assert _guards.MAIN_TEST_JOB_PYTEST_PATHS in block, (
        "the main test job's pytest path list drifted from "
        f"{_guards.MAIN_TEST_JOB_PYTEST_PATHS!r} — re-pin deliberately if this is "
        "an intentional path change"
    )
    assert "pytest -q tests/smoke tests/redteam" in block, (
        "the main test job's smoke+redteam invocation drifted — re-pin deliberately"
    )
    assert "tests/browser" not in block, (
        "the main test job must NOT collect tests/browser — the browser suite is "
        "cov-free by design and runs only in the dedicated browser job"
    )
