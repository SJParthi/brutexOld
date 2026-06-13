"""Regression guard: ``make run``'s install set must cover core runtime imports.

Per DEC-RUN-NUMPY-CORE-001. The bug this locks down: ``brutex.metrics.cascade``
(and ``spa`` / ``monte_carlo`` / ``lockbox``) import ``numpy`` UNCONDITIONALLY at
module top, and that chain is on the ``brutex`` CLI/engine import path
(``cli`` → ``engine.sweep_report`` → ``ranking`` → ``metrics.cascade``). But
``numpy`` was declared in the ``[dev]`` extra only, so a clean ``make run`` —
which installs ``.[data,ui]``, NOT ``[dev]`` — crashed with
``ModuleNotFoundError: No module named 'numpy'`` once the ``trading_core`` build
fix (#392) let the import chain get that far.

This test encodes the invariant: every distribution that ``make run`` must
provide for the engine to import has to be in the set it actually installs
(core ``dependencies`` + the ``data`` + ``ui`` extras).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
MAKEFILE = REPO_ROOT / "Makefile"


def _norm(spec: str) -> str:
    """Normalise a PEP 508 requirement to its bare distribution name.

    ``"numpy>=1.26,<3.0"`` → ``"numpy"``; ``"uvicorn[standard]>=0.29"`` →
    ``"uvicorn"``. PyPI treats ``_`` and ``-`` as equivalent, so we fold to
    ``-`` for stable comparison.
    """
    name = re.split(r"[<>=!~ ;\[]", spec, maxsplit=1)[0]
    return name.strip().lower().replace("_", "-")


def _project_table() -> dict[str, object]:
    """Return the parsed ``[project]`` table of pyproject.toml."""
    data: dict[str, object] = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project")
    assert isinstance(project, dict), "pyproject.toml has no [project] table"
    return project


def _str_list(value: object) -> list[str]:
    """Coerce a TOML value to a list of strings (empty if absent/other)."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _core_deps() -> set[str]:
    """Distribution names in the always-installed core ``dependencies``."""
    return {_norm(s) for s in _str_list(_project_table().get("dependencies"))}


def _make_run_install_set() -> set[str]:
    """Everything ``make run`` installs: core deps + the ``data`` + ``ui`` + ``groww`` extras."""
    project = _project_table()
    pkgs = _core_deps()
    extras = project.get("optional-dependencies")
    extras = extras if isinstance(extras, dict) else {}
    # `make run` runs `pip install -e ".[data,ui,groww]"` (the live Groww SDK + pyotp
    # are needed for the creds probe + the live pull — DEC-MAKE-RUN-GROWW-EXTRA-001).
    for extra in ("data", "ui", "groww"):
        for spec in _str_list(extras.get(extra)):
            pkgs.add(_norm(spec))
    return pkgs


def test_make_run_installs_data_ui_groww_extras() -> None:
    """Ground the test's assumption: `make run` really installs `.[data,ui,groww]`."""
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert '".[data,ui,groww]"' in makefile, (
        "make run no longer installs .[data,ui,groww] — update _make_run_install_set "
        "to match the new extras it installs."
    )


def test_numpy_is_in_make_run_install_set() -> None:
    """numpy must be installed by `make run` (core engine imports it at runtime)."""
    assert "numpy" in _make_run_install_set(), (
        "numpy is imported unconditionally by brutex.metrics.cascade (on the CLI "
        "import chain) but is NOT in `make run`'s install set — a clean clone "
        "will crash with ModuleNotFoundError: No module named 'numpy' "
        "(the DEC-RUN-NUMPY-CORE-001 bug)."
    )


def test_numpy_is_a_core_dependency_not_extra_only() -> None:
    """numpy belongs in core `dependencies` — metrics is core, not behind an extra."""
    assert "numpy" in _core_deps(), (
        "numpy must be in [project.dependencies] (not just an extra): "
        "brutex.metrics is core runtime code that imports it unconditionally."
    )
