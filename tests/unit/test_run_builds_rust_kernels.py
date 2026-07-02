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

import re
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
    """True if any ``brutex/`` module imports *import_name*.

    Matches both ``import <name>`` and ``from <name> import …`` at the start of
    a (possibly indented) line — covering the convention BruteX uses for its
    build-prerequisite kernels (``combo.py`` ``import fno_math``;
    ``fib_levels.py`` ``import trading_core``) AND any future ``from``-style
    site (agent-3 hostile-review hardening, DEC-RUN-TRADING-CORE-BUILD-001).
    """
    pattern = re.compile(
        rf"^[ \t]*(?:import {re.escape(import_name)}\b"
        rf"|from {re.escape(import_name)}(?:\.\w+)* import )",
        re.MULTILINE,
    )
    for path in BRUTEX_PKG.rglob("*.py"):
        if pattern.search(path.read_text(encoding="utf-8")):
            return True
    return False


def _make_target_body(makefile: str, target: str) -> str:
    """Return the recipe body of Makefile *target* (lines until the next target).

    Lets the build-step assertions check the step is wired INSIDE the target's
    recipe, not merely present somewhere in the file (agent-3 hostile-review
    hardening — closes the decoupled-substring gap). Target-specific VARIABLE
    lines (``target: export VAR := value`` — DEC-ONE-ENTRY-UNIFY-001 scoped the
    ``LOG_DIR`` export to ``run-raw``/``wipe`` that way) contain ``=`` and are
    skipped: they declare no recipe.
    """
    lines = makefile.splitlines()
    start = next(
        (
            i
            for i, ln in enumerate(lines)
            if re.match(rf"^{re.escape(target)}\b.*:", ln) and "=" not in ln
        ),
        None,
    )
    assert start is not None, f"Makefile has no `{target}:` target"
    body: list[str] = []
    for ln in lines[start + 1 :]:
        # A new target (non-indented `name:`) ends the current recipe.
        if ln and not ln[0].isspace() and re.match(r"^[A-Za-z0-9_.-]+\s*:", ln):
            break
        body.append(ln)
    return "\n".join(body)


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
def test_make_run_raw_builds_kernel(import_name: str, crate_dir: str) -> None:
    """``make run-raw`` must build each kernel (cd rust/<crate> + maturin develop).

    The unified ``make run`` delegates to ``./up.sh`` (DEC-ONE-ENTRY-UNIFY-001),
    whose Step 3 builds both kernels itself (pinned by tests/deploy/
    test_up_bootstrap.py); the RAW direct entry ``run-raw`` preserves the
    pre-unification body and must keep the build steps — this guard now pins
    that target so the #479 bug class cannot recur on the direct path either.
    """
    run_body = _make_target_body(MAKEFILE.read_text(encoding="utf-8"), "run-raw")
    assert f"import {import_name}" in run_body, (
        f"Makefile `run-raw` target never probes `import {import_name}` — a clean "
        f"clone will crash with ModuleNotFoundError: No module named "
        f"'{import_name}' (the DEC-RUN-TRADING-CORE-BUILD-001 bug)."
    )
    assert f"rust/{crate_dir}" in run_body and "maturin" in run_body, (
        f"Makefile `run-raw` target never builds rust/{crate_dir} via maturin."
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
