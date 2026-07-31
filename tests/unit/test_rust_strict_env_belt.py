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

``tests/conftest.py::_isolate_rust_strict_env`` is the belt (the
``_isolate_sweep_apriori_env`` snapshot-restore law one env var over). These
pins prove it CONTAINS a raw leak, and — the anti-vacuous half — that the
identical leak DOES propagate when the belt is absent.

The proof runs the pair in a SUBPROCESS pytest over generated test files
(the ``test_hypothesis_ci_profile.py`` subprocess-pin idiom) so it is
ORDER-INDEPENDENT: it never relies on this file's own collection order, and
it keeps working if a shuffling plugin is ever added to the ``dev`` extra.
"""

from __future__ import annotations

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


def _run_pair(work: Path, *, with_belt: bool) -> subprocess.CompletedProcess[str]:
    """Run the leaker+victim pair in a fresh pytest, optionally under the belt."""
    if with_belt:
        shutil.copy(_CONFTEST, work / "conftest.py")
    (work / "test_a_leaker.py").write_text(_LEAKER, encoding="utf-8")
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

    def test_parent_process_sees_the_shipped_default(self) -> None:
        # The belt runs for THIS test too: whatever any earlier test in this
        # process did to the var, strict mode is back at the shipped default.
        assert strict_mode() is True
