"""Test that ``python -m brutex`` has a working module entry point.

Covers the importable top-level of ``brutex/__main__.py`` (the
``if __name__ == "__main__"`` guard is excluded from coverage per
pyproject ``exclude_lines``). DEC-ONECLICK-RUN-001.
"""

from __future__ import annotations

import importlib


def test_main_module_imports_and_exposes_cli_main() -> None:
    """`import brutex.__main__` succeeds and re-exports the CLI entry point."""
    mod = importlib.import_module("brutex.__main__")
    from brutex.cli import main as cli_main

    assert mod.main is cli_main
