"""Verify ruff actually catches violations we plant.

Per docs/STANDARDS_100PCT.md §1 row 5: ruff is supposed to enforce
E + W + F + I + N rules. This test PROVES it actually does by running
ruff against a fixture containing an unused-import bug and asserting
non-zero exit.

If this test FAILS, ruff configuration is broken — it's not catching
violations it claims to catch. The 'lint' CI gate is then a lie.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.redteam.conftest import run, tool_available


@pytest.mark.skipif(not tool_available("ruff"), reason="ruff not installed")
def test_ruff_catches_unused_import(fixtures_dir: Path) -> None:
    """ruff MUST flag the unused `os` import in the fixture.

    The fixture file `ruff_unused_import.py` has `import os` that is
    never used. ruff F401 should fire.

    If ruff says exit 0 here, the F-rule enforcement is broken.
    """
    fixture = fixtures_dir / "planted_ruff_unused_import.py"
    assert fixture.exists(), "redteam fixture missing"

    # The fixture itself contains the planted violation, no stripping needed.
    # --isolated ignores pyproject.toml (which excludes planted_*.py from the
    # global scan); we want the redteam invocation to see the violation.
    result = run(["ruff", "check", "--isolated", "--select=F401", "--no-fix", str(fixture)])

    # The meta-assertion: ruff MUST have flagged this.
    # exit code 1 = violations found
    assert result.returncode != 0, (
        f"RED-TEAM FAILURE: ruff did not catch the planted unused import.\n"
        f"This means ruff's F401 check is broken.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "F401" in result.stdout, (
        f"ruff returned non-zero but didn't mention F401 specifically.\n"
        f"Output: {result.stdout}"
    )


@pytest.mark.skipif(not tool_available("ruff"), reason="ruff not installed")
def test_ruff_format_catches_bad_indent(tmp_path: Path) -> None:
    """ruff format --check MUST flag misformatted code.

    Plants a file with intentional bad indentation and asserts
    `ruff format --check` returns non-zero.
    """
    bad = tmp_path / "bad_indent.py"
    bad.write_text(
        "def foo():\n"
        "    x   =   1\n"  # extra spaces
        "    return  x\n",  # double space
        encoding="utf-8",
    )

    result = run(["ruff", "format", "--check", str(bad)])

    assert result.returncode != 0, (
        f"RED-TEAM FAILURE: ruff format did not flag bad indentation.\n"
        f"This means ruff format gate is broken.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
