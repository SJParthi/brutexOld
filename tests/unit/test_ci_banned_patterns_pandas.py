"""Subprocess pins for the widened banned-pandas check (scan-banned-patterns.sh #7).

DEC-TEST-APPARATUS-DEBTS-001(g): check #7 of ``scripts/ci/scan-banned-patterns.sh``
widened from ``brutex/math/**`` to ALL of brutex/ — the ``'brutex/*.py'
'brutex/**/*.py'`` pathspec PAIR, because git's ``brutex/**/*.py`` alone
requires an intermediate directory and silently misses the top-level
``brutex/*.py`` modules (cli.py, money.py, universe.py, ...) — anchored to real
import statements (``^\\s*(import\\s+pandas\\b|from\\s+pandas\\b)``) so prose
comments/docstrings mentioning pandas never false-positive.

These tests run the REAL script (the house subprocess-pin pattern of
``test_ci_dec_id_validator.py``) inside tmp_path fixture git repos — the script
is ``git ls-files``-driven and ``cd``s to ``git rev-parse --show-toplevel``, so
running it with ``cwd`` inside the fixture repo scans the fixture tree, never
the real checkout. Each fixture runs the WHOLE script, so its tree is built
neutral for every OTHER check the script enforces (no ``print(``, no bare
``except:``, no TODOs, no ``.env`` files, no token literals, no ``brutex/math/``
content, no tabs).

Honest envelope (Golden Rule #10, mirrored from the check's own comment): the
gate is TEXTUAL — multi-name one-liners (``import numpy, pandas``) and dynamic
imports (``importlib.import_module("pandas")``) escape; acceptable because
pandas is absent from the declared dependencies, so any escape is a loud
runtime ``ModuleNotFoundError``.

Coverage scoping, stated honestly: ``scripts/ci/`` is OUTSIDE
``[tool.coverage.run] source=["brutex"]`` — these subprocess pins ARE the
enforcement, matching every sibling scanner test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

_SCRIPT: Final[Path] = (
    Path(__file__).resolve().parents[2] / "scripts" / "ci" / "scan-banned-patterns.sh"
)

#: Deliberately split so this test file itself never contains a bannable
#: ``import pandas`` statement at the start of a line.
_PANDAS: Final[str] = "pan" + "das"


def _init_neutral_repo(tmp_path: Path) -> Path:
    """Create a git fixture repo whose tree is clean for every script check."""
    repo = tmp_path / "repo"
    (repo / "brutex" / "dashboard").mkdir(parents=True)
    (repo / "brutex" / "__init__.py").write_text(
        '"""Fixture package — neutral for every banned-pattern check."""\n',
        encoding="utf-8",
    )
    (repo / "brutex" / "dashboard" / "__init__.py").write_text(
        '"""Fixture subpackage."""\n', encoding="utf-8"
    )
    (repo / "brutex" / "dashboard" / "clean.py").write_text(
        '"""A clean module: polars only, structlog-style, no banned patterns."""\n\n'
        "import polars as pl\n\n\n"
        "def frame_height(df: pl.DataFrame) -> int:\n"
        '    """Return the frame height (no prints, no bare excepts, no tabs)."""\n'
        "    return df.height\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init", "--quiet"], cwd=repo, check=True, capture_output=True, timeout=60
    )
    return repo


def _run_script(repo: Path) -> subprocess.CompletedProcess[str]:
    """git-add the fixture tree, then run the REAL scan script against it.

    ``stdin=DEVNULL`` is load-bearing: for a check whose ``git ls-files`` glob
    matches nothing (e.g. ``brutex/math/**`` in these fixtures), ``xargs``
    still runs ``grep`` once with no file operands, which then reads stdin —
    an open stdin would hang the script.
    """
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, timeout=60)
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_pandas_import_in_brutex_dashboard_fails_check_7(tmp_path: Path) -> None:
    """An ``import pandas`` OUTSIDE brutex/math/ now fails — the widening's teeth."""
    repo = _init_neutral_repo(tmp_path)
    (repo / "brutex" / "dashboard" / "offender.py").write_text(
        f'"""Offending module."""\n\nimport {_PANDAS}\n\nx = {_PANDAS}.DataFrame\n',
        encoding="utf-8",
    )
    result = _run_script(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{_PANDAS} imported in brutex/" in result.stderr


def test_pandas_import_in_toplevel_brutex_module_fails_check_7(tmp_path: Path) -> None:
    """A TOP-LEVEL ``brutex/*.py`` offender is caught.

    Regression pin for the pathspec boundary: git's ``brutex/**/*.py`` alone
    requires an intermediate directory, so a top-level module (the real tree
    has 12 — cli.py, money.py, universe.py, ...) escaped the widened check
    entirely until the ``'brutex/*.py'`` pathspec was added alongside it.
    """
    repo = _init_neutral_repo(tmp_path)
    (repo / "brutex" / "toplevel_offender.py").write_text(
        f'"""Top-level offending module."""\n\nimport {_PANDAS}\n\nx = {_PANDAS}.DataFrame\n',
        encoding="utf-8",
    )
    result = _run_script(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{_PANDAS} imported in brutex/" in result.stderr


def test_prose_mention_of_pandas_passes_anchoring_pin(tmp_path: Path) -> None:
    """A comment/docstring mentioning pandas with leading text never trips the
    import-anchored regex (the false-positive class the anchoring exists for)."""
    repo = _init_neutral_repo(tmp_path)
    (repo / "brutex" / "dashboard" / "prose.py").write_text(
        f'"""Per CLAUDE.md section 4: no {_PANDAS} in hot paths — polars only."""\n\n'
        f"# we used to import {_PANDAS} here; polars replaced it long ago\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )
    result = _run_script(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No banned patterns found" in result.stdout


def test_indented_from_pandas_import_fails(tmp_path: Path) -> None:
    """An indented ``from pandas import ...`` (function-local import) is caught."""
    repo = _init_neutral_repo(tmp_path)
    (repo / "brutex" / "dashboard" / "lazy.py").write_text(
        f'"""Lazy-importing offender."""\n\n\ndef load() -> object:\n'
        f'    """Return the {_PANDAS} DataFrame type."""\n'
        f"    from {_PANDAS} import DataFrame\n\n"
        "    return DataFrame\n",
        encoding="utf-8",
    )
    result = _run_script(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{_PANDAS} imported in brutex/" in result.stderr


def test_clean_tree_passes(tmp_path: Path) -> None:
    """The neutral fixture tree passes the WHOLE script (all checks green)."""
    repo = _init_neutral_repo(tmp_path)
    result = _run_script(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No banned patterns found" in result.stdout
