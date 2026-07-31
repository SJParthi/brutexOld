"""Self-test for the suite-wide ``BRUTEX_RUST_STRICT`` hermeticity belt.

DEC-RUST-STRICT-NO-FALLBACK-001. ``brutex.config.rust_strict.strict_mode()``
re-reads the env on EVERY call and never memoises, so the shipped default
(strict ON when the var is unset) is a process-wide ambient. A test that
writes the kill switch ``"0"`` with a RAW ``os.environ`` assignment — instead
of ``monkeypatch`` — leaks it into every later test in the same pytest
process, and the damage is SILENT-GREEN in the wrong direction: a
strict-refusal test degrades to ``Failed: DID NOT RAISE RustFallbackRefused``
while its facade quietly served the Python oracle. That is precisely the
fall-back this DEC forbids, so the leak must be structurally impossible
rather than merely "no current test does it".

``tests/conftest.py`` carries TWO defences, and this file proves each one is
separately load-bearing:

1. ``_isolate_rust_strict_env`` — the function-scoped autouse belt (the
   ``_isolate_sweep_apriori_env`` snapshot-restore law one env var over). It
   contains the RAW-assignment leak above.

2. ``pytest_runtest_teardown`` under ``@pytest.hookimpl(trylast=True)`` — the
   ordering-immune backstop. The belt CANNOT contain a leak whose writer is a
   ``monkeypatch`` object pytest instantiated BEFORE the belt's own fixture:
   finalisers unwind LIFO by setup order, so that object's restore runs AFTER
   the belt's and re-writes whatever value it recorded. When a module pins
   ``"0"`` through its own ``pytest.MonkeyPatch.context()`` and a test in that
   module then writes the same var through the SHARED ``monkeypatch``, the
   shared object records ``"0"`` as the value to restore and the kill switch
   outlives the module. That is not hypothetical: it is the exact shape that
   reddened CI shards 3 and 4 on PR #1774 (writers ``test_ranking_cvar_gate``
   and ``test_exec_rust_seam``; victims ``test_rust_strict_env_belt`` and four
   ``test_money_rust::TestStrictMode`` cases, all ``DID NOT RAISE``). Fifteen
   modules repo-wide carry the shape, so the backstop is what bounds the blast
   radius of the next one to a single test.

Each proof runs the pair in a SUBPROCESS pytest over generated test files
(the ``test_hypothesis_ci_profile.py`` subprocess-pin idiom) so it is
ORDER-INDEPENDENT: it never relies on this file's own collection order, and
it keeps working if a shuffling plugin is ever added to the ``dev`` extra.
Each has an anti-vacuous twin that removes exactly the one defence under test
and shows the identical pair then fails — so neither pin can go green because
the leak stopped happening.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

from brutex.config.rust_strict import RUST_STRICT_ENV, strict_mode

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFTEST = _REPO_ROOT / "tests" / "conftest.py"

# The leaking writer: a RAW assignment that never restores — exactly the
# shape monkeypatch teardown cannot see.
_LEAKER = f"""
import os


def test_leaks_the_kill_switch_raw() -> None:
    os.environ["{RUST_STRICT_ENV}"] = "0"
"""

# The LIFO leaker: no raw assignment anywhere — every write goes through a
# monkeypatch, which is why the belt alone cannot contain it. ``_own_ctx_pin``
# is the module's own ``MonkeyPatch.context()`` pin (the real modules use this
# to hold a whole class in kill-switch-OFF mode); ``_fresh_seam`` merely
# DEPENDS on the shared ``monkeypatch``, which is what makes pytest build that
# object before the belt's fixture and therefore tear it down after. The test
# body's ``setenv`` is then recorded against a pre-pinned ``"0"``.
_LIFO_LEAKER = f"""
from collections.abc import Iterator

import pytest

RUST_STRICT_ENV = "{RUST_STRICT_ENV}"


@pytest.fixture(autouse=True)
def _own_ctx_pin() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(RUST_STRICT_ENV, "0")
        yield


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("BRUTEX_NOT_A_REAL_FLAG", raising=False)
    yield


def test_shared_monkeypatch_records_the_pinned_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RUST_STRICT_ENV, "0")
"""

# The victim: asserts the SHIPPED default (strict ON) still holds. Named to
# sort AFTER the leaker so the leak is genuinely upstream of it under
# pytest's definition/alphabetical collection order.
_VICTIM = """
import os

from brutex.config.rust_strict import RUST_STRICT_ENV, strict_mode


def test_sees_the_pristine_strict_default() -> None:
    assert os.environ.get(RUST_STRICT_ENV) is None
    assert strict_mode() is True
"""


def _conftest_without_the_backstop() -> str:
    """The real conftest with ONLY ``pytest_runtest_teardown`` removed.

    Cut by AST span rather than by text search so the twin can never silently
    become a no-op: if the hook is ever renamed or deleted this raises instead
    of shipping an identical file and a vacuously-green pin.
    """
    source = _CONFTEST.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == "pytest_runtest_teardown":
            first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            assert node.end_lineno is not None  # always set on a parsed module
            return "".join(lines[: first - 1] + lines[node.end_lineno :])
    raise AssertionError(
        "tests/conftest.py no longer defines pytest_runtest_teardown — the "
        "ordering-immune BRUTEX_RUST_STRICT backstop is gone (see this "
        "module's docstring for the CI shard 3/4 failures it prevents)"
    )


def _run_pair(
    work: Path,
    *,
    with_belt: bool,
    leaker: str = _LEAKER,
    with_backstop: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the leaker+victim pair in a fresh pytest, optionally under the belt."""
    if with_belt:
        if with_backstop:
            shutil.copy(_CONFTEST, work / "conftest.py")
        else:
            (work / "conftest.py").write_text(_conftest_without_the_backstop(), encoding="utf-8")
    (work / "test_a_leaker.py").write_text(leaker, encoding="utf-8")
    (work / "test_b_victim.py").write_text(_VICTIM, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            str(work),
        ],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=False,
        env=_child_env(),
    )


def _child_env() -> dict[str, str]:
    """A child env with the strict var stripped — the pristine shipped default."""
    env = dict(os.environ)
    env.pop(RUST_STRICT_ENV, None)
    # Keep the child's hypothesis profile aligned with the parent so the
    # copied conftest's profile load is a no-op difference.
    env.setdefault("HYPOTHESIS_PROFILE", os.environ.get("HYPOTHESIS_PROFILE", "default"))
    return env


class TestRustStrictEnvBelt:
    """The belt contains a raw kill-switch leak; without it the leak lands."""

    def test_belt_contains_a_raw_kill_switch_leak(self, tmp_path: Path) -> None:
        result = _run_pair(tmp_path, with_belt=True)
        assert result.returncode == 0, (
            f"the belt must contain the leak — pytest said:\n{result.stdout}\n{result.stderr}"
        )
        assert "2 passed" in result.stdout

    def test_without_the_belt_the_leak_propagates(self, tmp_path: Path) -> None:
        # ANTI-VACUOUS half: the same pair with NO conftest (hence no belt)
        # must FAIL at the victim, proving the belt is load-bearing and this
        # file is not asserting a tautology.
        result = _run_pair(tmp_path, with_belt=False)
        assert result.returncode != 0, (
            "without the belt the leak MUST reach the victim — pytest said:\n"
            f"{result.stdout}\n{result.stderr}"
        )
        assert "1 failed" in result.stdout

    def test_the_backstop_contains_a_lifo_monkeypatch_leak(self, tmp_path: Path) -> None:
        # The shape that actually reddened CI shards 3 and 4: no raw
        # assignment anywhere, so nothing here is a "don't do that" lint —
        # every write is a well-behaved monkeypatch, and the leak comes purely
        # from finaliser ORDER.
        result = _run_pair(tmp_path, with_belt=True, leaker=_LIFO_LEAKER)
        assert result.returncode == 0, (
            "the trylast backstop must contain the LIFO leak — pytest said:\n"
            f"{result.stdout}\n{result.stderr}"
        )
        assert "2 passed" in result.stdout

    def test_the_belt_alone_cannot_contain_the_lifo_leak(self, tmp_path: Path) -> None:
        # ANTI-VACUOUS, and the reason the backstop exists as a SEPARATE
        # defence: the belt is present and correct here — only the 20-line
        # trylast hook is removed — and the victim still fails. Deleting the
        # hook as "redundant with the belt" reds exactly this test.
        result = _run_pair(tmp_path, with_belt=True, leaker=_LIFO_LEAKER, with_backstop=False)
        assert result.returncode != 0, (
            "without the trylast backstop the LIFO leak MUST reach the victim "
            "(if this passes, the belt now contains it and this pin is stale) "
            f"— pytest said:\n{result.stdout}\n{result.stderr}"
        )
        assert "1 failed" in result.stdout

    def test_parent_process_sees_the_shipped_default(self) -> None:
        # The belt runs for THIS test too: whatever any earlier test in this
        # process did to the var, strict mode is back at the shipped default.
        assert strict_mode() is True
