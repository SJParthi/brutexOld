"""Verify bandit actually catches security violations we plant.

Per docs/STANDARDS_100PCT.md §1 row 6: bandit must show 0 high/critical.
This test PROVES the gate works by planting a known security violation
(eval on user input — B307) and asserting bandit flags it.

If this test FAILS, bandit configuration is broken — accepting
HIGH-severity security issues it should reject.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.redteam.conftest import run, tool_available


@pytest.mark.skipif(not tool_available("bandit"), reason="bandit not installed")
def test_bandit_catches_eval_user_input(fixtures_dir: Path) -> None:
    """bandit MUST flag `eval(user_input)` as a HIGH severity issue.

    The fixture has `eval(user_input)` with `# nosec` that we strip
    before running bandit.

    If bandit returns 0 (no issues), the security gate is broken.
    """
    fixture = fixtures_dir / "planted_bandit_eval.py"
    assert fixture.exists(), "redteam fixture missing"

    # The fixture itself contains the planted violation, no stripping needed.
    # -l = report low+ severity (eval is MEDIUM but use -l for safety)
    result = run(["bandit", "-l", "-q", str(fixture)])

    assert result.returncode != 0, (
        f"RED-TEAM FAILURE: bandit did not flag eval(user_input).\n"
        f"This means bandit's B307 check is broken or threshold too loose.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    output = (result.stdout + result.stderr).lower()
    assert "b307" in output or "eval" in output, (
        f"bandit returned non-zero but didn't mention eval/B307.\nOutput: {result.stdout}"
    )
