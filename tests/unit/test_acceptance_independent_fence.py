"""The AST fence: ``independent.py`` may share DATA, never COMPUTE.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756), design §4.5. Breaking
the fence is a red BUILD, not a review comment: no ``brutex.*`` import except
``^[A-Z_0-9]+$`` names from the calendar DATA modules (each asserted
non-callable at runtime); no ``tests.*`` import; third-party = stdlib +
polars/duckdb/pyarrow only.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import brutex.acceptance.independent as independent_mod

FENCED = Path(independent_mod.__file__)

ALLOWED_BRUTEX_MODULES = {"brutex.calendars.nse", "brutex.calendars.observed_sessions"}
ALLOWED_THIRD_PARTY = {"polars", "duckdb", "pyarrow"}
UPPER = re.compile(r"^[A-Z_0-9]+$")


def _stdlib(module: str) -> bool:
    return module.split(".", maxsplit=1)[0] in sys.stdlib_module_names


def _imports(tree: ast.Module) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, []) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.append((node.module or "", [alias.name for alias in node.names]))
    return out


class TestIndependentFence:
    def test_no_production_compute_imports(self) -> None:
        tree = ast.parse(FENCED.read_text(encoding="utf-8"))
        for module, names in _imports(tree):
            if module.startswith("tests"):
                raise AssertionError(f"fence broken: tests import {module}")
            if module.startswith("brutex"):
                assert module in ALLOWED_BRUTEX_MODULES, f"fence broken: import {module}"
                assert names, f"fence broken: bare 'import {module}' not allowed"
                for name in names:
                    assert UPPER.fullmatch(name), (
                        f"fence broken: non-constant name {name!r} from {module}"
                    )
                continue
            if _stdlib(module):
                continue
            root = module.split(".")[0]
            assert root in ALLOWED_THIRD_PARTY, f"fence broken: third-party {module}"

    def test_imported_calendar_objects_are_data_not_compute(self) -> None:
        tree = ast.parse(FENCED.read_text(encoding="utf-8"))
        for module, names in _imports(tree):
            if module in ALLOWED_BRUTEX_MODULES:
                for name in names:
                    obj = getattr(independent_mod, name)
                    assert not callable(obj), f"fence broken: {name} from {module} is callable"

    def test_no_dynamic_import_escape(self) -> None:
        source = FENCED.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                name = getattr(target, "id", getattr(target, "attr", ""))
                assert name not in {"__import__", "import_module"}, (
                    "fence broken: dynamic import in independent.py"
                )

    def test_fence_test_is_load_bearing(self) -> None:
        # Self-check: a synthetic offending import IS flagged by the walker.
        tree = ast.parse("from brutex.dashboard.movers_snapshot import compute_snapshot\n")
        offenders = [module for module, _names in _imports(tree) if module.startswith("brutex")]
        assert offenders == ["brutex.dashboard.movers_snapshot"]
        assert offenders[0] not in ALLOWED_BRUTEX_MODULES
