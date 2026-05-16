"""Planted-bug fixture: type mismatch for mypy --strict.

This file intentionally contains a violation. Excluded from the
global mypy scan via pyproject.toml mypy overrides.

The verifier `test_redteam_mypy.py::test_mypy_catches_type_mismatch`
runs `mypy --strict` directly against this file and asserts non-zero
exit + 'incompatible' or 'arg-type' in the output.

If mypy says "Success" here, the type gate is broken.
"""

from __future__ import annotations


def add_ints(a: int, b: int) -> int:
    return a + b


def _planted_mismatch() -> int:
    # type: ignore intentionally absent — mypy must flag this
    return add_ints("not an int", 1)
