"""Regression guard: the turnkey build path MUST build every native kernel.

Per DEC-RUN-TRADING-CORE-BUILD-001 (MIGRATION entry #479). The bug this
locks down: DEC-SHARED-CORE-001 Phase 2b (#441) added a module-top
``import trading_core`` in ``brutex/indicators/`` + ``brutex/vocabulary/``,
but ``make run`` / ``scripts/setup/bootstrap.sh`` only ever built the OTHER
kernel (``fno_math``). A clean clone therefore crashed with
``ModuleNotFoundError: No module named 'trading_core'`` the moment
``brutex.engine`` was imported.

This test encodes the invariant that was violated, so the bug class cannot
recur for THIS kernel OR any future one:

- ✅ DETECT: a kernel imported by ``brutex/`` but absent from the build path
  fails this test (exactly the #479 bug).
- ✅ VERIFY: asserts the build step exists in both turnkey entry points.
- ✅ PREVENT: failing test blocks CI, so a new ``rust/<crate>`` wheel that
  ``brutex/`` imports cannot ship without being wired into the build.

It is DYNAMIC (CLAUDE.md §11): the native kernels are DISCOVERED from
``rust/*/pyproject.toml`` (maturin crates), not hard-coded — add a third
PyO3 crate and import it, and this guard covers it automatically.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUST_DIR = REPO_ROOT / "rust"
MAKEFILE = REPO_ROOT / "Makefile"
BOOTSTRAP = REPO_ROOT / "scripts" / "setup" / "bootstrap.sh"
BRUTEX_PKG = REPO_ROOT / "brutex"


def _discover_maturin_kernels() -> dict[str, str]:
    """Return ``{python_import_name: crate_dir_name}`` for every maturin crate.

    A native kernel is any ``rust/<crate>/pyproject.toml`` whose build backend
    is maturin. The Python import name is the first dotted segment of
    ``[tool.maturin].module-name`` (e.g. ``trading_core._trading_core`` →
    ``trading_core``).
    """
    kernels: dict[str, str] = {}
    for pyproject in sorted(RUST_DIR.glob("*/pyproject.toml")):
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        backend = data.get("build-system", {}).get("build-backend", "")
        if backend != "maturin":
            continue
        module_name = data["tool"]["maturin"]["module-name"]
        import_name = module_name.split(".", 1)[0]
        kernels[import_name] = pyproject.parent.name
    return kernels


def _module_top_imports(import_name: str) -> bool:
    """True if any ``brutex/`` module imports *import_name* at module top.

    Matches a bare ``import <name>`` at the start of a (possibly indented) line
    — the convention BruteX uses for its build-prerequisite kernels
    (``combo.py`` ``import fno_math``; ``fib_levels.py`` ``import trading_core``).
    """
    needle_a = f"\nimport {import_name}\n"
    needle_b = f"\n    import {import_name}\n"
    for path in BRUTEX_PKG.rglob("*.py"):
        text = "\n" + path.read_text(encoding="utf-8")
        if needle_a in text or needle_b in text:
            return True
    return False


KERNELS = _discover_maturin_kernels()


def test_at_least_the_two_known_kernels_are_discovered() -> None:
    """Sanity: the two known PyO3 kernels are present (guards the discovery)."""
    assert "fno_math" in KERNELS, KERNELS
    assert "trading_core" in KERNELS, KERNELS


@pytest.mark.parametrize("import_name", sorted(KERNELS))
def test_kernel_is_imported_by_brutex(import_name: str) -> None:
    """Every discovered kernel is a real runtime dependency of ``brutex/``."""
    assert _module_top_imports(import_name), (
        f"{import_name} is a maturin crate but no brutex/ module imports it "
        "at module top — discovery is stale, update this guard."
    )


@pytest.mark.parametrize(("import_name", "crate_dir"), sorted(KERNELS.items()))
def test_make_run_builds_kernel(import_name: str, crate_dir: str) -> None:
    """``make run`` must build each kernel (cd rust/<crate> + maturin develop)."""
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert f"import {import_name}" in makefile, (
        f"Makefile `run` target never probes `import {import_name}` — a clean "
        f"clone will crash with ModuleNotFoundError: No module named "
        f"'{import_name}' (the DEC-RUN-TRADING-CORE-BUILD-001 bug)."
    )
    assert f"rust/{crate_dir}" in makefile and "maturin" in makefile, (
        f"Makefile `run` target never builds rust/{crate_dir} via maturin."
    )


@pytest.mark.parametrize(("import_name", "crate_dir"), sorted(KERNELS.items()))
def test_bootstrap_builds_kernel(import_name: str, crate_dir: str) -> None:
    """``bootstrap.sh`` must build each kernel (cd rust/<crate> + maturin)."""
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    assert f"import {import_name}" in bootstrap, (
        f"bootstrap.sh never probes `import {import_name}` — a fresh-machine "
        f"setup will leave {import_name} unbuilt."
    )
    assert f"rust/{crate_dir}" in bootstrap and "maturin" in bootstrap, (
        f"bootstrap.sh never builds rust/{crate_dir} via maturin."
    )
