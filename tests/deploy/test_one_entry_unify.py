"""Static-assertion tests for the ONE-entry-point unification.

DEC-ONE-ENTRY-UNIFY-001 (docs/MIGRATION.md). ``make run``, ``./up.sh``, and
the committed IntelliJ ▶ ``BruteX`` run configuration must all execute the
SAME path — ``./up.sh`` (self-check → self-heal → start dashboard,
DEC-ONE-COMMAND-UP-001) — so the operator never chooses between entries.

These tests pin the unification contract:

- the Makefile ``run`` target delegates to ``./up.sh`` and does NOTHING else;
- the pre-unification direct body survives verbatim as ``run-raw`` (preflight
  + venv + ``.[data,ui,groww]`` + both Rust kernels + ``python -m brutex run``);
- the ``LOG_DIR`` → ``BRUTEX_LOG_DIR`` export is scoped to ``run-raw`` + the
  wipe targets, so the unified ``run`` logs to the runtime default
  (``~/.brutex/logs/brutex.log``) exactly like ``./up.sh`` and the IDE entry;
- the CI "make run smoke (cold-start boot)" job never INVOKES ``make run``
  (it reproduces the ``run-raw`` dependency set step-by-step — CI has no AWS
  credentials, and the gate pins the runtime dependency-set invariant, not
  the bootstrap's self-checks), so re-wiring ``run`` cannot break CI — and
  its branch-protection-pinned job NAME is unchanged;
- exactly ONE IDE run configuration is committed, named ``BruteX``, invoking
  ``./up.sh`` from the project root in a terminal;
- the ``up.sh`` fast path prints the rotating log-file location (the start
  path inherits the runtime's own ``[brutex] logs → …`` banner — no
  duplication);
- the README tells the one-entry story.

They follow the proven ``tests/deploy`` pattern (see
``test_up_bootstrap.py``): static assertions over checked-in file content —
nothing is executed against the machine, no network is touched.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _REPO_ROOT / "Makefile"
_UP_SH = _REPO_ROOT / "up.sh"
_README = _REPO_ROOT / "README.md"
_CLI = _REPO_ROOT / "brutex" / "cli.py"
_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_DRIFT_YML = _REPO_ROOT / ".github" / "workflows" / "branch-protection-drift.yml"
_RUN_CONFIG_DIRS = (
    _REPO_ROOT / ".idea" / "runConfigurations",
    _REPO_ROOT / ".run",
)
_SMOKE_JOB_NAME = "make run smoke (cold-start boot)"


@pytest.fixture(scope="module")
def makefile_text() -> str:
    """Return the full Makefile source."""
    assert _MAKEFILE.is_file(), f"missing Makefile: {_MAKEFILE}"
    return _MAKEFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def up_text() -> str:
    """Return the full source of ``up.sh``."""
    assert _UP_SH.is_file(), f"missing bootstrap script: {_UP_SH}"
    return _UP_SH.read_text(encoding="utf-8")


def _make_target_body(makefile: str, target: str) -> str:
    """Return the recipe body of Makefile *target* (lines until the next rule).

    Target-specific VARIABLE lines (``targets: export VAR := value`` — the
    scoped ``LOG_DIR`` export) contain ``=`` and are skipped: they declare no
    recipe. Mirrors ``tests/unit/test_run_builds_rust_kernels.py``.
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
        if ln and not ln[0].isspace() and re.match(r"^[A-Za-z0-9_.-]+\s*:", ln):
            break
        body.append(ln)
    return "\n".join(body)


def _recipe_lines(body: str) -> list[str]:
    """Return the executable recipe lines of a target body (tab-indented)."""
    out: list[str] = []
    for raw in body.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return out


# --------------------------------------------------------------------------- #
# Makefile — `run` delegates to ./up.sh; `run-raw` preserves the direct body.
# --------------------------------------------------------------------------- #
def test_run_target_delegates_to_up_sh_and_nothing_else(makefile_text: str) -> None:
    """``make run`` executes exactly one recipe line: ``./up.sh``."""
    recipe = _recipe_lines(_make_target_body(makefile_text, "run"))
    assert recipe == ["./up.sh"], (
        f"the unified `run` target must delegate to ./up.sh and NOTHING else "
        f"(DEC-ONE-ENTRY-UNIFY-001) — got {recipe!r}"
    )


def test_run_target_has_no_direct_build_steps(makefile_text: str) -> None:
    """The unified ``run`` recipe carries none of the old direct build steps."""
    recipe = "\n".join(_recipe_lines(_make_target_body(makefile_text, "run")))
    for banned in ("pip install", "maturin", "-m venv", "-m brutex run"):
        assert banned not in recipe, (
            f"`run` must not duplicate the direct body ({banned!r} found) — "
            "the direct chain lives in `run-raw`; up.sh self-heals the rest"
        )


def test_run_raw_preserves_the_direct_body(makefile_text: str) -> None:
    """``run-raw`` keeps the pre-unification chain: preflight + venv + deps + kernels + boot."""
    lines = makefile_text.splitlines()
    rule = next(
        (ln for ln in lines if re.match(r"^run-raw\b.*:", ln) and "=" not in ln),
        None,
    )
    assert rule is not None, "Makefile has no `run-raw:` rule"
    assert "preflight" in rule, "run-raw must keep the preflight prerequisite"
    body = _make_target_body(makefile_text, "run-raw")
    for required in (
        "$(PY) -m venv $(VENV)",
        '".[data,ui,groww]"',
        "import fno_math",
        "import trading_core",
        "maturin",
        "-m brutex run",
    ):
        assert required in body, f"run-raw lost a direct-body step: {required!r}"


def test_phony_declares_run_and_run_raw(makefile_text: str) -> None:
    """Both entries are declared .PHONY (a stray `run` file must never mask them)."""
    phony = next(ln for ln in makefile_text.splitlines() if ln.startswith(".PHONY:"))
    assert " run " in f"{phony} ", ".PHONY must include run"
    assert " run-raw " in f"{phony} ", ".PHONY must include run-raw"


def test_log_dir_export_is_scoped_to_raw_and_wipe(makefile_text: str) -> None:
    """The LOG_DIR export is target-specific — the unified `run` gets the runtime default.

    A GLOBAL ``export BRUTEX_LOG_DIR`` would make ``make run`` log to
    ``./.brutex-logs`` while ``./up.sh`` and the IDE entry log to
    ``~/.brutex/logs`` — three "identical" entries with two log locations.
    """
    assert "run-raw wipe wipe-dry: export BRUTEX_LOG_DIR := $(LOG_DIR)" in makefile_text
    for ln in makefile_text.splitlines():
        assert not ln.startswith("export BRUTEX_LOG_DIR"), (
            "BRUTEX_LOG_DIR must not be exported globally — it would divert the "
            "unified `run` (→ ./up.sh) away from the runtime default log path"
        )


# --------------------------------------------------------------------------- #
# CI — the smoke job never invokes `make run`, so the re-wiring cannot break it.
# --------------------------------------------------------------------------- #
def test_ci_never_invokes_make_run() -> None:
    """No workflow line RUNS ``make run`` — every mention is a comment or a name.

    The "make run smoke (cold-start boot)" job reproduces the ``run-raw``
    dependency set step-by-step (pip + kernels + ``python -m brutex run
    --no-browser``); CI has no AWS credentials, so pointing it at the unified
    ``make run`` → ./up.sh would trade a deterministic dependency-set gate for
    a bootstrap run whose AWS self-checks merely degrade. This pin makes the
    decoupling explicit: re-wiring `run` can never break the smoke job.
    """
    for workflow in sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        for lineno, ln in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            if "make run" not in ln:
                continue
            stripped = ln.strip()
            is_comment = stripped.startswith("#")
            is_name = "name:" in ln or f'"{_SMOKE_JOB_NAME}"' in ln
            assert is_comment or is_name, (
                f"{workflow.name}:{lineno} appears to INVOKE `make run` — the "
                f"smoke job must reproduce run-raw step-by-step, never call "
                f"make: {ln!r}"
            )


def test_ci_smoke_job_name_is_unchanged() -> None:
    """The branch-protection-pinned job name survives the unification untouched."""
    ci = _CI_YML.read_text(encoding="utf-8")
    assert f"name: {_SMOKE_JOB_NAME}" in ci, (
        "the run-smoke job name is pinned by branch protection and must not change"
    )
    drift = _DRIFT_YML.read_text(encoding="utf-8")
    assert f'"{_SMOKE_JOB_NAME}"' in drift, (
        "branch-protection-drift.yml must still pin the smoke job name"
    )


def test_ci_smoke_job_still_boots_the_runtime_directly() -> None:
    """The smoke job still installs the raw set + boots ``python -m brutex run``."""
    ci = _CI_YML.read_text(encoding="utf-8")
    assert 'pip install -e ".[data,ui]"' in ci
    assert "python -m brutex run --no-browser" in ci


# --------------------------------------------------------------------------- #
# IDE — exactly ONE committed run configuration: "BruteX" → ./up.sh.
# --------------------------------------------------------------------------- #
def _committed_run_configs() -> list[Path]:
    """Return every committed JetBrains run-config XML (both conventions)."""
    found: list[Path] = []
    for base in _RUN_CONFIG_DIRS:
        if base.is_dir():
            found.extend(sorted(base.glob("*.xml")))
    return found


def test_exactly_one_run_config_is_committed() -> None:
    """One config in the IDE Run dropdown — the operator never chooses."""
    configs = _committed_run_configs()
    assert len(configs) == 1, (
        f"exactly ONE run configuration must be committed "
        f"(DEC-ONE-ENTRY-UNIFY-001) — found: {[str(p) for p in configs]}"
    )
    assert configs[0].name == "BruteX.xml"


def test_run_config_is_named_brutex_and_invokes_up_sh() -> None:
    """The single config is named ``BruteX`` and runs ``./up.sh`` at the project root."""
    (config_path,) = _committed_run_configs()
    root = ET.fromstring(config_path.read_text(encoding="utf-8"))  # noqa: S314 — trusted repo file
    configuration = root.find("configuration")
    assert configuration is not None, "run-config XML has no <configuration>"
    assert configuration.get("name") == "BruteX"
    options = {
        opt.get("name"): opt.get("value")
        for opt in configuration.findall("option")
        if opt.get("name") is not None
    }
    assert options.get("SCRIPT_TEXT") == "./up.sh", (
        "the BruteX run configuration must invoke the unified ./up.sh entry"
    )
    assert options.get("SCRIPT_WORKING_DIRECTORY") == "$PROJECT_DIR$"
    assert options.get("EXECUTE_IN_TERMINAL") == "true", (
        "up.sh output (PASS/FAIL self-check lines, URL, logs) must be visible"
    )


def test_stale_one_click_config_is_gone() -> None:
    """The duplicate ``brutex (one-click · make run)`` config file is deleted."""
    stale = _REPO_ROOT / ".idea" / "runConfigurations" / "brutex_one_click.xml"
    assert not stale.exists(), "the duplicate one-click run config must stay deleted"


# --------------------------------------------------------------------------- #
# Startup visibility — URL + log-file location, one clear line each.
# --------------------------------------------------------------------------- #
def test_up_sh_fast_path_prints_the_log_location(up_text: str) -> None:
    """Both ``already ready`` fast paths surface the log location.

    Since DEC-LOGS-IN-PROJECT-001 the one clear ``logs → …`` line is printed by
    ``link_repo_logs`` (which ALSO refreshes the in-project ``<repo>/logs``
    symlink); each fast path invokes it right after the ``already ready`` line —
    the fuller wiring contract lives in tests/deploy/test_logs_in_project.py.
    """
    ready_sites = up_text.split("already ready — BruteX dashboard is running")[1:]
    assert len(ready_sites) >= 2, "both fast paths must print the already-ready line"
    for site in ready_sites:
        assert "link_repo_logs" in site.split("exit 0")[0], (
            "each `already ready` fast path must print one clear `logs → …` line "
            "via link_repo_logs before exiting"
        )


def test_up_sh_log_resolution_mirrors_the_runtime(up_text: str) -> None:
    """``resolve_log_file`` mirrors brutex/observability/logging.py precedence."""
    assert "resolve_log_file()" in up_text
    helper = up_text[up_text.index("resolve_log_file()") :]
    helper = helper[: helper.index("\n}")]
    assert "${BRUTEX_LOG_FILE:-}" in helper, "BRUTEX_LOG_FILE (full path) wins first"
    assert "${BRUTEX_LOG_DIR:-}" in helper, "then BRUTEX_LOG_DIR/brutex.log"
    assert "BRUTEX_LOG_DIR=" in helper, "then the persisted ~/.brutex/.env line"
    assert '"$BRUTEX_HOME/logs"' in helper, "finally the ~/.brutex/logs default"
    assert "brutex.log" in helper


def test_runtime_boot_prints_url_and_logs_lines() -> None:
    """The real boot path prints the dashboard URL + log location, one line each."""
    cli = _CLI.read_text(encoding="utf-8")
    assert "[brutex] dashboard → http://{host}:{port}" in cli
    assert "[brutex] logs → {log_path}" in cli


# --------------------------------------------------------------------------- #
# Documentation — the one-entry story.
# --------------------------------------------------------------------------- #
def test_readme_tells_the_one_entry_story() -> None:
    """README: IDE ▶ BruteX, `make run`, and `./up.sh` are all the same thing."""
    text = _README.read_text(encoding="utf-8")
    assert "DEC-ONE-ENTRY-UNIFY-001" in text
    assert "all three are the same thing" in text
    assert "make run-raw" in text, "the RAW direct entry must be documented"
    assert "brutex (one-click · make run)" in text, (
        "the README must tell operators how to remove the stale LOCAL config "
        "(repo changes cannot delete local IDE state)"
    )


def test_up_sh_header_names_all_three_entries(up_text: str) -> None:
    """up.sh declares itself the ONE entry point for make / IDE / direct runs."""
    assert "DEC-ONE-ENTRY-UNIFY-001" in up_text
    header = up_text[: up_text.index("set -u")]
    assert "`make run`" in header
    assert "IntelliJ" in header and "BruteX" in header
