"""Planted-bug fixture: unused import for ruff F401.

This file intentionally contains a violation. Excluded from the
global ruff scan via pyproject.toml [tool.ruff] extend-exclude.

The verifier `test_redteam_ruff.py::test_ruff_catches_unused_import`
runs `ruff check --select=F401 --isolated` directly against this file
and asserts non-zero exit + F401 in the output.

If ruff says all-clean here, the F-rule enforcement is broken.

IMPORTANT: do not include the literal n-o-q-a string in any comment
in this file — ruff parses that as a suppression directive.
"""

import os  # ruff must flag this unused import


def _placeholder() -> str:
    return "planted-ruff-fixture"
