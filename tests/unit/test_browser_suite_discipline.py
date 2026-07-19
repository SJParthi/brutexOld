"""Suite-discipline gate for the browser truth suite (DEC-DASHBOARD-BROWSER-TRUTH-001).

Runs in the MAIN CI test job (a plain unit test — playwright-free: it imports
only the stdlib-only ``tests.browser._guards``), so the zero-flakiness law and
the CI wiring are policed on EVERY push even though the browser job itself
runs separately:

(a) forbidden-pattern scan — no blind sleeps / retries / oversized timeouts /
    skip escapes anywhere under ``tests/browser/`` (RECURSIVE — subdirectory
    modules are scanned too; round-4 review), incl. the literal skip shapes
    the mark patterns alone missed (the ``pytest.xfail(…)`` call form,
    unittest-style ``SkipTest``, an unsanctioned ``pytest.importorskip``, the
    pytest import-aliasing routes) and the dict-key-timeout /
    seconds-denominated ``.wait`` smuggles;
(b) ci.yml wiring pins — the ``browser`` job exists, is wired into the
    ``ci-ok`` aggregate in ALL THREE places (``needs:`` + the echo block + the
    for-loop enumeration), carries EXACTLY ONE job-level ``if:`` — the
    DEC-CI-DOCS-PATHS-FILTER-001 docs-only paths-filter gate, whose skip is
    NOT a silent merge-green escape because ci-ok's skipped-only-when-proven
    law accepts a skipped browser job ONLY when detect-changes proved
    docs_only=true (any other conditional could merge green with a broken
    DOM), runs cov-free, and is runtime-bounded; the pytest STEP
    itself is shape-pinned (round-2 review): its run line must equal
    ``run: pytest tests/browser -q`` verbatim (a narrowed sentinel-only path
    would green the job with zero Chromium proofs), the opt-in env var must
    be scoped to THAT step (detached, the suite collects only the honest-skip
    sentinel → exit 0 on skips → green job), and the step carries no
    step-level ``if:``;
(c) main-CI-path exclusion — the main test job's pytest invocations never
    grow ``tests/browser`` (the browser suite is deliberately outside the
    coverage paths; its opt-in env var keeps bare ``pytest`` runs — e.g.
    verify-m4pro.yml's ``pytest -q`` — protected via the conftest collection
    guard + the always-collected honest-skip sentinel);
(d) collection-guard recursion — the conftest ``collect_ignore`` computation
    and the direct-path ``pytest_collection_modifyitems`` skip guard both
    cover subdirectory modules (round-4 review: their prior top-level-only
    shapes would have let a future ``tests/browser/<sub>/test_x.py`` be
    collected — and RUN — by a bare ``pytest`` with the opt-in unset).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest

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


def test_guard_patterns_catch_sneaky_evasion_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) Teeth: the realistic evasion shapes the scan claims to ban ARE caught.

    Round-1 review finding (DEC-DASHBOARD-BROWSER-TRUTH-001 fix set): the scan
    missed positional ``set_default_timeout`` literals and the sleep-capable
    ``time`` import forms. Round-2 review finding: the digits-only capture
    read ``timeout=6e4`` as 6, ``timeout=1e9`` as 1 and ``timeout=0x186A0``
    as 0 — positively VALIDATING 60 s / 100 s literals under the 10 s cap —
    so the scientific/hex shapes (plus the unparseable-literal fail-closed
    arm) are pinned here too. Round-3 review finding: the digit-leading
    capture + line scan never SAW a dot-leading float (``timeout=.5e9`` =
    ~5.8 days), a unary-plus literal (``timeout=+600000``), a paren-wrapped
    literal (``timeout=(60000)``), a formatter-split
    ``set_default_timeout(\\n    60000\\n)``, or a dynamic
    ``__import__("time")`` / ``import_module("time")`` sleep smuggle — all
    five shapes are pinned here too. Round-4 review findings: (i) three
    literal SKIP shapes scanned clean — the runtime ``pytest.xfail(…)`` call
    (caught before only when its message happened to carry a retry keyword),
    an ungated ``pytest.importorskip`` of an absent package (pytest exits 0
    on skips → a green browser job with zero Chromium proofs), and a raised
    unittest-style SkipTest — plus the pytest import-aliasing routes around
    them; (ii) a dict-key literal timeout (``**{"timeout": 60_000}``) and a
    SECONDS-denominated ``Event``-style ``.wait(30.0)`` (which the ms cap
    would mis-validate as ``30 < 10_000``) both scanned clean; (iii) the scan
    was top-level-only, so a subdirectory module escaped it entirely. All are
    pinned here too. This pins every added pattern against a synthetic suite
    tree so a pattern regression fails the MAIN test job.
    """
    sneaky = {
        "t_default_timeout.py": "page.set_default_timeout(600000)\n",
        "t_nav_default_timeout.py": "ctx.set_default_navigation_timeout(60_000)\n",
        "t_from_import_sleep.py": "from time import sleep\n",
        "t_import_time_as.py": "import time as t\n",
        "t_getattr_time.py": 'waiter = getattr(time, "sle" "ep")\n',
        "t_asyncio_sleep.py": "await asyncio.sleep(5)\n",
        "t_kwarg_timeout.py": "page.goto(url, timeout=999_999)\n",
        "t_sci_timeout_big.py": 'page.wait_for_selector("#x", timeout=1e9)\n',
        "t_sci_timeout.py": "page.goto(url, timeout=6e4)\n",
        "t_hex_timeout.py": "page.goto(url, timeout=0x186A0)\n",
        "t_sci_default_timeout.py": "page.set_default_timeout(1e9)\n",
        "t_unparseable_timeout.py": "page.goto(url, timeout=1j)\n",
        "t_dotfloat_timeout.py": "page.goto(url, timeout=.5e9)\n",
        "t_dotfloat_default_timeout.py": "page.set_default_timeout(.9e7)\n",
        "t_plus_timeout.py": "page.goto(url, timeout=+600000)\n",
        "t_plus_default_timeout.py": "page.set_default_timeout(+60_000)\n",
        "t_paren_timeout.py": 'page.wait_for_selector("#a", timeout=(60000))\n',
        "t_split_default_timeout.py": "page.set_default_timeout(\n    60000\n)\n",
        "t_dunder_import_time.py": '__import__("time").sleep(5)\n',
        "t_import_module_time.py": 'importlib.import_module("time").sleep(5)\n',
        "t_xfail_call.py": 'pytest.xfail("known upstream issue")\n',
        "t_importorskip_absent.py": 'pytest.importorskip("not_installed_pkg")\n',
        "t_skiptest_raise.py": 'raise unittest.SkipTest("revisit later")\n',
        "t_from_pytest_import.py": "from pytest import xfail\n",
        "t_import_pytest_as.py": "import pytest as pt\n",
        "t_getattr_pytest.py": 'escape = getattr(pytest, "sk" "ip")\n',
        "t_dunder_import_pytest.py": '__import__("pytest").skip("later")\n',
        "t_dict_timeout_spread.py": 'page.goto(url, **{"timeout": 60_000})\n',
        "t_dict_timeout_opts.py": "opts = {'timeout': 600000}\n",
        "t_event_wait.py": "threading.Event().wait(30.0)\n",
        "t_event_wait_kwarg.py": "done.wait(timeout=30.0)\n",
        "t_unbounded_wait.py": "ready.wait()\n",
        "sub/t_subdir_sleep.py": "time.sleep(60)\n",
    }
    for name, body in sneaky.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    monkeypatch.setattr(_guards, "BROWSER_DIR", tmp_path)
    flagged = {line.split(":", 1)[0] for line in _guards.iter_violations()}
    assert flagged == set(sneaky)


def test_guard_patterns_stay_clean_on_sanctioned_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) No false positives: capped literals + plain `import time` pass.

    Round-2 additions: under-cap scientific (``5e3`` = 5 000), float
    (``join(timeout=10.0)`` — the harness's own thread-join bound) and hex
    (``0x2328`` = 9 000) literals are VALUE-checked, not shape-banned.
    Round-3 additions: under-cap paren-wrapped (``(5_000)``), unary-plus
    (``+10000`` — exactly the cap) and formatter-split
    (``set_default_timeout(\\n    9_000\\n)``) literals stay clean too.
    Round-4 additions: plain ``import pytest``, the ONE sanctioned
    ``pytest.importorskip("playwright.sync_api")`` argument, a sub-second
    pacing ``.wait(0.01)`` (the harness's own poll idiom) and an under-cap
    dict-key ``{"timeout": 9_000}`` literal stay clean too (value-checked,
    not shape-banned).
    """
    (tmp_path / "t_clean.py").write_text(
        "import time\n"
        "import pytest\n"
        'pytest.importorskip("playwright.sync_api")\n'
        "from datetime import UTC, datetime, timedelta\n"
        "context.set_default_timeout(DEFAULT_TIMEOUT_MS)\n"
        "page.set_default_timeout(10_000)\n"
        "expect(locator).to_have_text(x, timeout=10000)\n"
        "page.wait_for_selector(sel, timeout=5e3)\n"
        "self._thread.join(timeout=10.0)\n"
        "page.goto(url, timeout=0x2328)\n"
        "page.wait_for_selector(sel, timeout=(5_000))\n"
        "page.goto(url, timeout=+10000)\n"
        "page.set_default_timeout(\n"
        "    9_000\n"
        ")\n"
        "pacer.wait(0.01)\n"
        'page.goto(url, **{"timeout": 9_000})\n'
        "deadline = time.monotonic() + 15.0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_guards, "BROWSER_DIR", tmp_path)
    assert list(_guards.iter_violations()) == []


def test_collect_ignore_module_list_is_recursive(tmp_path: Path) -> None:
    """(d) Round-4 review: the collect_ignore computation covers subdirectories.

    The prior top-level ``glob("test_*.py")`` would have let a future
    ``tests/browser/<sub>/test_x.py`` be COLLECTED by a bare ``pytest`` run
    (pyproject ``testpaths=["tests"]`` — e.g. verify-m4pro's ``pytest -q``)
    with the opt-in unset: an ImportError crash on a playwright-less box, or
    an unsanctioned Chromium run on a playwright-carrying one. Only the exact
    TOP-LEVEL sentinel is exempt — a subdirectory clone of its name is still
    ignored.
    """
    from tests.browser import conftest as browser_conftest

    (tmp_path / "test_top.py").write_text("", encoding="utf-8")
    (tmp_path / "test_suite_visibility.py").write_text("", encoding="utf-8")
    sub = tmp_path / "pages"
    sub.mkdir()
    (sub / "test_sub.py").write_text("", encoding="utf-8")
    (sub / "test_suite_visibility.py").write_text("", encoding="utf-8")
    assert browser_conftest.playwright_test_modules(tmp_path) == [
        "pages/test_sub.py",
        "pages/test_suite_visibility.py",
        "test_top.py",
    ]
    # And the REAL suite: every playwright module ignored, the sentinel never.
    real = browser_conftest.playwright_test_modules()
    assert "test_suite_visibility.py" not in real
    assert {
        "test_db_paging.py",
        "test_harness_selfcheck.py",
        "test_movers_gate.py",
        "test_universe_results.py",
    } <= set(real)


def test_modifyitems_guard_covers_subdirectory_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) Round-4 review: the direct-path skip guard covers subdirectories.

    The prior ``path.parent == BROWSER_DIR`` exact-parent check let a
    directly-addressed ``pytest tests/browser/<sub>/test_x.py`` run real
    Chromium with the opt-in unset — the exact class the round-1 hook closed
    for top-level files. Items at ANY depth under the suite are now marked;
    the exact top-level sentinel and items outside the suite are not; the
    enabled mode marks nothing.
    """
    from tests.browser import conftest as browser_conftest

    class _FakeItem:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.markers: list[object] = []

        def add_marker(self, marker: object) -> None:
            self.markers.append(marker)

    sub_item = _FakeItem(_guards.BROWSER_DIR / "pages" / "test_sub.py")
    top_item = _FakeItem(_guards.BROWSER_DIR / "test_db_paging.py")
    sentinel_item = _FakeItem(_guards.BROWSER_DIR / "test_suite_visibility.py")
    outside_item = _FakeItem(_guards.BROWSER_DIR.parent / "unit" / "test_other.py")
    items = [sub_item, top_item, sentinel_item, outside_item]

    monkeypatch.delenv(_guards.ENABLE_ENV_VAR, raising=False)
    browser_conftest.pytest_collection_modifyitems(cast("list[pytest.Item]", items))
    marked = [len(item.markers) for item in items]
    assert marked == [1, 1, 0, 0], f"disabled-mode marking drifted: {marked}"

    monkeypatch.setenv(_guards.ENABLE_ENV_VAR, "1")
    browser_conftest.pytest_collection_modifyitems(cast("list[pytest.Item]", items))
    marked = [len(item.markers) for item in items]
    assert marked == [1, 1, 0, 0], f"enabled mode must mark nothing: {marked}"


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


def test_browser_job_job_level_if_is_exactly_the_docs_only_gate() -> None:
    """(b) The ONLY job-level `if:` is the docs-only paths-filter gate.

    DEC-CI-DOCS-PATHS-FILTER-001: the browser job may skip ONLY on a PROVEN
    docs-only diff. Any OTHER conditional would be a silent merge-green
    escape hatch (the ci-ok loop treats skipped as pass) — so the job-level
    `if:` list is pinned to EXACTLY the one gate expression, and the
    fail-closed net is pinned too: ci-ok's skipped-only-when-proven law must
    enumerate the browser job, turning any skip that detect-changes did not
    prove docs-only into a RED aggregate.
    """
    text = _CI_YML.read_text(encoding="utf-8")
    block = _job_block(text, _guards.CI_BROWSER_JOB)
    job_level_ifs = [line for line in block.splitlines() if re.match(r"^    if\s*:", line)]
    assert job_level_ifs == [
        "    if: needs.detect-changes.outputs.docs_only != 'true'"
    ], (
        "the browser job's job-level `if:` must be EXACTLY the docs-only "
        "paths-filter gate (DEC-CI-DOCS-PATHS-FILTER-001) — any other "
        "conditional is a silent merge-green escape hatch because the ci-ok "
        f"loop treats skipped as pass: {job_level_ifs}"
    )
    ci_ok = _job_block(text, "ci-ok")
    assert '"browser:${{ needs.browser.result }}" \\' in ci_ok, (
        "ci-ok lost the skipped-only-when-proven enumeration entry for the "
        "browser job — a docs-only-gated browser skip would be silently green"
    )
    assert "detect-changes" in ci_ok.split("steps:", 1)[0], (
        "ci-ok `needs:` lost detect-changes — the skipped-only-when-proven "
        "law cannot read the docs_only output"
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


def _pytest_step_block(job_block: str) -> str:
    """The one browser-job STEP block carrying the pytest invocation."""
    steps = re.split(r"^      - ", job_block.split("steps:", 1)[1], flags=re.MULTILINE)
    hits = [step for step in steps if "pytest tests/browser" in step]
    assert len(hits) == 1, (
        f"expected exactly one step containing `pytest tests/browser`, got {len(hits)}"
    )
    return hits[0]


def test_browser_job_pytest_step_shape_pinned() -> None:
    """(b) Round-2 review: the pytest STEP's exact shape, not just substrings.

    Two verified mutations survived the presence/substring pins: (M11) a
    narrowed ``run: pytest tests/browser/test_suite_visibility.py -q`` — the
    sentinel-only run greens the job with ZERO Chromium proofs while still
    containing the ``pytest tests/browser`` substring; (M12) detaching the
    opt-in env var onto another step — the job-block substring still matches,
    but the suite then collects only the honest-skip sentinel (pytest exits 0
    on skips → green job → green ci-ok). A step-level ``if:`` on the pytest
    step is the same unpinned-step escape class. All three are closed by
    pinning the STEP itself.
    """
    text = _CI_YML.read_text(encoding="utf-8")
    step = _pytest_step_block(_job_block(text, _guards.CI_BROWSER_JOB))
    run_lines = [line.strip() for line in step.splitlines() if line.strip().startswith("run:")]
    assert run_lines == [_guards.CI_BROWSER_PYTEST_RUN_LINE], (
        "the browser job's pytest run line drifted from the exact pinned invocation "
        f"{_guards.CI_BROWSER_PYTEST_RUN_LINE!r} — a narrowed path (e.g. the sentinel "
        f"module only) greens the job with zero Chromium proofs executed: {run_lines}"
    )
    assert f'{_guards.ENABLE_ENV_VAR}: "1"' in step, (
        "the opt-in env var must be scoped to the pytest STEP itself — attached to any "
        "other step it still satisfies the job-block substring pin while the suite "
        "collects only the honest-skip sentinel (exit 0 on skips → green browser job "
        "with zero DOM tests)"
    )
    assert not re.search(r"^\s*if\s*:", step, flags=re.MULTILINE), (
        "the pytest step must not carry a step-level `if:` — a conditional pytest step "
        "is the same silent merge-green escape as the banned job-level `if:`"
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
