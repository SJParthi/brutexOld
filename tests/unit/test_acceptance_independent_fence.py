"""The AST fence: ``independent.py`` may share DATA, never COMPUTE.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1797), design §4.5. Breaking
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

import brutex.acceptance as acceptance_pkg
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


# ── the PACKAGE-WIDE zero-vendor-call pin (round-4 C-F2) ─────────────────
#
# The runbook + rate.py claim "ZERO vendor calls, ever (AST-pinned)"; until
# this test the ONLY AST pin was the independent.py fence above (scope:
# independent.py's own imports). This pin makes the package-wide claim
# mechanical: the ONLY brutex.adapters.* imports anywhere in
# brutex/acceptance/ are the sanctioned PLANNING/CONSTANTS surfaces below —
# never a client / pool / live-client construction, never the SDK.

ACCEPTANCE_ROOT = Path(acceptance_pkg.__file__).parent

#: The sanctioned adapters surfaces: pure planning math + frozen constants.
ALLOWED_ADAPTER_IMPORTS: dict[str, set[str] | None] = {
    # None = any name from the module is fine (constants / pure planners).
    "brutex.adapters.groww.constants": None,
    "brutex.adapters.groww.contract_bounds": None,
    "brutex.adapters.groww.chunks": None,
    # option_ingest also holds the fetch spine — ONLY the pure planner may
    # be imported from it.
    "brutex.adapters.groww.option_ingest": {"plan_contract_windows"},
    # Typed contract dataclasses — data shapes, no I/O.
    "brutex.adapters.protocol": None,
}

#: Vendor-client construction surfaces that must NEVER appear in the package.
FORBIDDEN_TOKENS = (
    "GrowwAPI",
    "GrowwClient",
    "ClientPool",
    "build_live_client",
    "build_live_pool",
    "get_historical_candles",
)


class TestAcceptanceZeroVendorCallPin:
    def _package_files(self) -> list[Path]:
        files = sorted(ACCEPTANCE_ROOT.rglob("*.py"))
        assert len(files) >= 15  # the package really was walked
        return files

    def test_only_sanctioned_adapter_imports(self) -> None:
        for file in self._package_files():
            tree = ast.parse(file.read_text(encoding="utf-8"))
            for module, names in _imports(tree):
                if not module.startswith("brutex.adapters"):
                    continue
                assert module in ALLOWED_ADAPTER_IMPORTS, (
                    f"zero-vendor-call pin broken: {file.name} imports {module}"
                )
                allowed_names = ALLOWED_ADAPTER_IMPORTS[module]
                assert names, f"zero-vendor-call pin broken: bare 'import {module}' in {file.name}"
                if allowed_names is not None:
                    for name in names:
                        assert name in allowed_names, (
                            f"zero-vendor-call pin broken: {file.name} imports "
                            f"{name!r} from {module} (allowed: {sorted(allowed_names)})"
                        )

    def test_no_vendor_client_tokens_anywhere(self) -> None:
        for file in self._package_files():
            source = file.read_text(encoding="utf-8")
            for token in FORBIDDEN_TOKENS:
                assert token not in source, (
                    f"zero-vendor-call pin broken: {token!r} appears in {file.name}"
                )

    def test_vendor_pin_is_load_bearing(self) -> None:
        # Self-check: a synthetic client import IS refused by the walker law.
        tree = ast.parse("from brutex.adapters.groww.client import GrowwClient\n")
        offenders = [
            module
            for module, _names in _imports(tree)
            if module.startswith("brutex.adapters") and module not in ALLOWED_ADAPTER_IMPORTS
        ]
        assert offenders == ["brutex.adapters.groww.client"]
        # And a non-sanctioned NAME from a sanctioned module is refused too.
        allowed = ALLOWED_ADAPTER_IMPORTS["brutex.adapters.groww.option_ingest"]
        assert allowed is not None and "run_groww_option_ingest" not in allowed
