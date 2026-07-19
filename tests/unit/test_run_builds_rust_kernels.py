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

Two kernel classes (DEC-RUST-DATALAKE-IO-ROADMAP-001, MIGRATION.md #1796):

- REQUIRED — module-top imported by ``brutex/`` ⇒ a build PREREQUISITE, so
  the turnkey build path (``make run-raw`` + ``bootstrap.sh``) must build it
  (the original #479 invariant, unchanged).
- OPTIONAL — registered in ``OPTIONAL_KERNELS`` with a documented reason:
  NEVER module-top imported (consumed only through an import-GUARDED facade
  that degrades gracefully when the crate is absent), deliberately NOT in the
  turnkey build path, and built by its OWN dedicated CI lane. The inverse
  invariants are pinned below so the classification cannot silently rot: a
  future module-top import of an optional kernel FAILS here and forces its
  promotion into the required class (where the build-path guards then fire).
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

# OPTIONAL kernels: {import_name: (guarded_facade_relpath, ci_lane_relpath)}.
# Registration REQUIRES a documented reason — an unregistered new crate that
# nothing imports still fails test_kernel_is_imported_by_brutex loudly.
OPTIONAL_KERNELS: dict[str, tuple[str, str]] = {
    # DEC-RUST-DATALAKE-IO-ROADMAP-001 (MIGRATION.md #1796 PR-1 / ADR-0020):
    # brutex_lake is the default-OFF lake render crate. It is consumed ONLY
    # through the import-guarded facade brutex/data/lake_rust_render.py
    # (`importlib.import_module("brutex_lake")` inside the guard — an absent
    # or broken install latches the flag OFF with one loud WARN and the
    # Python render serves, byte-identical). It stays OPTIONAL (never a
    # build PREREQUISITE — a build failure must not brick the box) but since
    # DEC-BRUTEX-LAKE-BOX-WHEEL-001 it IS in the turnkey build path
    # BEST-EFFORT (warn-only `make run-raw` + `bootstrap.sh` blocks —
    # pinned by BOX_BUILT_OPTIONAL_KERNELS below); in NO cargo workspace,
    # gated by its own CI lane (.github/workflows/lake-rust-parity.yml).
    # A module-top `import brutex_lake` anywhere in brutex/ would make a
    # clean clone crash-prone again — the inverse pin below forbids exactly
    # that.
    "brutex_lake": (
        "brutex/data/lake_rust_render.py",
        ".github/workflows/lake-rust-parity.yml",
    ),
    # DEC-DATA-RUST-PURELAW-001 (MIGRATION.md #1940 / ADR-0020 slice T10):
    # brutex_data is the default-OFF data pure-law crate (the fno_symbol
    # structural grammar). It is consumed ONLY through the import-guarded
    # facade brutex/data/fno_symbol_rust.py
    # (`importlib.import_module("brutex_data")` inside the guard — an
    # absent or broken install latches BRUTEX_DATA_RUST OFF with one loud
    # WARN and the Python oracle serves, byte-identical). Deliberately NOT
    # a turnkey build prerequisite: in NO cargo workspace, built ONLY by
    # its own CI lane (.github/workflows/data-rust-parity.yml). A
    # module-top `import brutex_data` anywhere in brutex/ would reclassify
    # it REQUIRED — the inverse pin below forbids exactly that.
    "brutex_data": (
        "brutex/data/fno_symbol_rust.py",
        ".github/workflows/data-rust-parity.yml",
    ),
    # DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943 / slice T35):
    # brutex_core is the default-OFF config-leaf crate (money / hwenv /
    # paths / dotenv / backupenv laws). It is consumed ONLY through FIVE
    # default-OFF import-guarded facades that resolve it lazily via
    # `importlib.import_module("brutex_core")` — brutex/money_rust.py's
    # own guard, brutex/config/hardware_envelope_rust.py's, and the
    # shared brutex/config/seam_core.py ConfigRustSeam guard serving
    # paths_rust/dotenv_rust/backup_env_rust (an absent or broken
    # install latches `crate_unavailable` with one loud WARN and the
    # Python oracle serves, behavior-identical). It is deliberately NOT
    # a build prerequisite: never module-top imported, in NO cargo
    # workspace, built ONLY by its own CI lane
    # (.github/workflows/core-rust-parity.yml). A module-top
    # `import brutex_core` anywhere in brutex/ would make a clean clone
    # crash-prone again — the inverse pin below forbids exactly that.
    "brutex_core": (
        "brutex/money_rust.py",
        ".github/workflows/core-rust-parity.yml",
    ),
    # DEC-STRATEGIES-RUST-COMBO-001 (MIGRATION.md #1955): brutex_strategies is
    # the default-OFF combo canonical-JSON + unit-separator crate. It is
    # consumed ONLY through the import-guarded facade
    # brutex/strategies/combo_rust.py (`importlib.import_module("brutex_strategies")`
    # inside `_resolve_crate` — an absent or stale wheel latches the
    # `BRUTEX_STRATEGIES_RUST_COMBO` flag OFF with one loud WARN and the Python
    # oracle serves, byte-identical). It is deliberately NOT a build
    # prerequisite (a verified engine import cycle forbids a module-top import;
    # a clean clone runs the Python oracle with the flag OFF). It is built by
    # its OWN CI lane — the `rust-brutex-strategies` job + the wheel-install
    # steps in .github/workflows/ci.yml. A module-top `import brutex_strategies`
    # anywhere in brutex/ would make a clean clone crash-prone again — the
    # inverse pin (test_optional_kernel_never_module_top_imported) forbids it.
    "brutex_strategies": (
        "brutex/strategies/combo_rust.py",
        ".github/workflows/ci.yml",
    ),
}

REQUIRED_KERNELS = {
    name: crate_dir for name, crate_dir in KERNELS.items() if name not in OPTIONAL_KERNELS
}

# OPTIONAL kernels that are ALSO in the turnkey build path BEST-EFFORT
# (DEC-BRUTEX-LAKE-BOX-WHEEL-001): `make run-raw` + `bootstrap.sh` build them
# in the default path with a warn-only failure arm (the brutex_feed shape —
# `{ cd rust/<crate> && … maturin develop --release; } || echo/_warn`), so a
# broken toolchain / no-network box still boots while every healthy
# `make setup` / `make run-raw` produces the wheel. The build-vs-WHEEL_SET
# coupling is separately drift-pinned by
# tests/unit/test_nightly_box_update.py::test_wheel_set_is_the_makefile_mirror.
BOX_BUILT_OPTIONAL_KERNELS: dict[str, str] = {
    "brutex_lake": "rust/brutex-lake",
}


@pytest.mark.parametrize(("import_name", "crate_dir"), sorted(BOX_BUILT_OPTIONAL_KERNELS.items()))
def test_box_built_optional_kernel_is_registered_optional(import_name: str, crate_dir: str) -> None:
    """The box-built set is a SUBSET of OPTIONAL_KERNELS with matching dirs."""
    assert import_name in OPTIONAL_KERNELS, (
        f"{import_name} is box-built best-effort but not registered OPTIONAL — "
        "either register it (import-guarded facade + CI lane) or promote it to "
        "the required class."
    )
    assert KERNELS.get(import_name) == crate_dir.removeprefix("rust/"), (
        f"BOX_BUILT_OPTIONAL_KERNELS maps {import_name} to {crate_dir} but the "
        "maturin crate discovery disagrees — fix the stale registration."
    )


@pytest.mark.parametrize(("import_name", "crate_dir"), sorted(BOX_BUILT_OPTIONAL_KERNELS.items()))
def test_make_run_raw_builds_optional_kernel(import_name: str, crate_dir: str) -> None:
    """``make run-raw`` builds the optional kernel BEST-EFFORT (warn-only).

    DEC-BRUTEX-LAKE-BOX-WHEEL-001: the block must carry the feed-template
    shape — the ``import`` probe, ``cd rust/<crate> … maturin develop``, AND
    the ``|| echo`` warn-only arm AFTER the maturin invocation (a failure
    warns, never aborts the box boot — the seams are default-OFF and every
    facade degrades to the Python law).
    """
    run_body = _make_target_body(MAKEFILE.read_text(encoding="utf-8"), "run-raw")
    assert f"import {import_name}" in run_body, (
        f"Makefile `run-raw` never probes `import {import_name}` — the optional "
        f"kernel is no longer box-built (DEC-BRUTEX-LAKE-BOX-WHEEL-001)."
    )
    assert crate_dir in run_body and "maturin" in run_body, (
        f"Makefile `run-raw` never builds {crate_dir} via maturin."
    )
    # Warn-only shape: `|| echo` must follow the crate's maturin invocation
    # (the same after-maturin reading test_wheel_set_is_the_makefile_mirror's
    # run-raw parser applies — required = NOT warn-only).
    block = run_body.split(f"cd {crate_dir}", 1)
    assert len(block) == 2, f"no `cd {crate_dir}` in the run-raw recipe"
    after_maturin = block[1].split("maturin", 1)
    assert len(after_maturin) == 2, f"no maturin invocation after `cd {crate_dir}`"
    assert "|| echo" in after_maturin[1], (
        f"the {crate_dir} run-raw block is not warn-only (`|| echo` after "
        "maturin) — a build failure would abort the box boot; the optional "
        "kernel must stay best-effort."
    )


@pytest.mark.parametrize(("import_name", "crate_dir"), sorted(BOX_BUILT_OPTIONAL_KERNELS.items()))
def test_bootstrap_builds_optional_kernel(import_name: str, crate_dir: str) -> None:
    """``bootstrap.sh`` builds the optional kernel BEST-EFFORT (warn-only)."""
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    assert f"import {import_name}" in bootstrap, (
        f"bootstrap.sh never probes `import {import_name}` — the optional "
        f"kernel is no longer box-built (DEC-BRUTEX-LAKE-BOX-WHEEL-001)."
    )
    assert crate_dir in bootstrap and "maturin" in bootstrap, (
        f"bootstrap.sh never builds {crate_dir} via maturin."
    )
    # Warn-only shape: the failure arm is `_warn` (never `_err` + `exit 1` —
    # the required-kernel steps' abort shape) inside the kernel's own step.
    step = bootstrap.split(f"cd {crate_dir}", 1)
    assert len(step) == 2, f"no `cd {crate_dir}` in bootstrap.sh"
    failure_arm = step[1].split("fi", 2)[0]
    assert "_warn" in failure_arm, (
        f"the {crate_dir} bootstrap step is not warn-only (`_warn` on build "
        "failure) — a failure would abort bootstrap; the optional kernel must "
        "stay best-effort."
    )
    assert "_err" not in failure_arm, (
        f"the {crate_dir} bootstrap step carries the abort-shape `_err` — the "
        "optional kernel must warn, never abort."
    )


def test_known_kernels_are_discovered() -> None:
    """Sanity: the known PyO3 kernels are present (guards the discovery)."""
    assert "fno_math" in REQUIRED_KERNELS, KERNELS
    assert "trading_core" in REQUIRED_KERNELS, KERNELS
    assert "brutex_lake" in KERNELS, KERNELS
    assert "brutex_core" in KERNELS, KERNELS


@pytest.mark.parametrize("import_name", sorted(REQUIRED_KERNELS))
def test_kernel_is_imported_by_brutex(import_name: str) -> None:
    """Every REQUIRED kernel is a real runtime dependency of ``brutex/``."""
    assert _module_top_imports(import_name), (
        f"{import_name} is a maturin crate but no brutex/ module imports it "
        "at module top — discovery is stale: update this guard, or (only if "
        "the crate is deliberately import-guarded + built by its own CI lane) "
        "register it in OPTIONAL_KERNELS with a documented reason."
    )


@pytest.mark.parametrize("import_name", sorted(OPTIONAL_KERNELS))
def test_optional_kernel_is_discovered(import_name: str) -> None:
    """A stale OPTIONAL_KERNELS registration (crate deleted/renamed) fails."""
    assert import_name in KERNELS, (
        f"OPTIONAL_KERNELS registers {import_name} but no rust/*/pyproject.toml "
        "maturin crate exports that module — remove the stale registration."
    )


@pytest.mark.parametrize("import_name", sorted(OPTIONAL_KERNELS))
def test_optional_kernel_never_module_top_imported(import_name: str) -> None:
    """The inverse invariant: an optional kernel must stay import-guarded.

    A module-top ``import <name>`` would make the crate a hard runtime
    dependency that the turnkey build path never builds (exactly the #479
    bug). This pin forces the author of such an import to PROMOTE the kernel
    out of OPTIONAL_KERNELS — at which point the run-raw/bootstrap build
    guards below start firing for it.
    """
    assert not _module_top_imports(import_name), (
        f"{import_name} is registered OPTIONAL (import-guarded, not in the "
        "turnkey build path) but a brutex/ module now imports it at module "
        "top — a clean clone would crash. Either remove the module-top import "
        "(use the guarded facade) or promote the kernel out of "
        "OPTIONAL_KERNELS and wire it into make run-raw + bootstrap.sh."
    )


@pytest.mark.parametrize(
    ("import_name", "facade_relpath"),
    sorted((name, spec[0]) for name, spec in OPTIONAL_KERNELS.items()),
)
def test_optional_kernel_consumed_via_guarded_facade(import_name: str, facade_relpath: str) -> None:
    """The registered facade really guard-imports the optional kernel."""
    facade = REPO_ROOT / facade_relpath
    assert facade.is_file(), f"guarded facade {facade_relpath} does not exist"
    source = facade.read_text(encoding="utf-8")
    assert f'importlib.import_module("{import_name}")' in source, (
        f"{facade_relpath} no longer guard-imports {import_name} via "
        f'importlib.import_module("{import_name}") — the optional kernel is '
        "unconsumed (dead crate) or the guard moved; update OPTIONAL_KERNELS."
    )


@pytest.mark.parametrize(
    ("import_name", "lane_relpath"),
    sorted((name, spec[1]) for name, spec in OPTIONAL_KERNELS.items()),
)
def test_optional_kernel_built_by_its_own_ci_lane(import_name: str, lane_relpath: str) -> None:
    """An optional kernel is still BUILT somewhere: its dedicated CI lane."""
    crate_dir = KERNELS.get(import_name)
    assert crate_dir is not None, (
        f"OPTIONAL_KERNELS registers {import_name} but no maturin crate "
        "exports that module — remove the stale registration."
    )
    lane = REPO_ROOT / lane_relpath
    assert lane.is_file(), (
        f"{import_name} is optional (excluded from the turnkey build path) "
        f"but its registered CI lane {lane_relpath} does not exist — the "
        "crate would never be built anywhere."
    )
    lane_text = lane.read_text(encoding="utf-8")
    assert f"rust/{crate_dir}" in lane_text, (
        f"CI lane {lane_relpath} never references rust/{crate_dir} — the "
        "optional kernel's own lane no longer builds it."
    )


@pytest.mark.parametrize(("import_name", "crate_dir"), sorted(REQUIRED_KERNELS.items()))
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


@pytest.mark.parametrize(("import_name", "crate_dir"), sorted(REQUIRED_KERNELS.items()))
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
