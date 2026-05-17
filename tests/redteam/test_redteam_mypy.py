"""Verify mypy actually catches type mismatches we plant.

Per docs/STANDARDS_100PCT.md §1 row 4: mypy --strict must be 0 errors.
This test PROVES the gate works by planting a known type mismatch
and asserting mypy returns non-zero.

If this test FAILS, mypy configuration is broken — accepting type
errors it should reject.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.redteam.conftest import run, tool_available


@pytest.mark.skipif(not tool_available("mypy"), reason="mypy not installed")
def test_mypy_catches_type_mismatch(fixtures_dir: Path) -> None:
    """mypy --strict MUST flag passing a `str` where `int` is required.

    The fixture has `add_ints("not an int", 1)` with a `# type: ignore`
    that we strip before running mypy.

    If mypy returns 0 (success), the type gate is broken.
    """
    fixture = fixtures_dir / "planted_mypy_type_mismatch.py"
    assert fixture.exists(), "redteam fixture missing"

    # The fixture itself contains the planted violation, no stripping needed.
    result = run(
        [
            "mypy",
            "--strict",
            "--no-incremental",
            "--cache-dir=/dev/null",
            str(fixture),
        ]
    )

    assert result.returncode != 0, (
        f"RED-TEAM FAILURE: mypy did not catch the planted type mismatch.\n"
        f"This means mypy --strict gate is broken.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # mypy should specifically mention 'incompatible type' or 'arg-type'
    output = (result.stdout + result.stderr).lower()
    assert "incompatible" in output or "arg-type" in output or "str" in output, (
        f"mypy returned non-zero but message unclear: {result.stdout}"
    )
