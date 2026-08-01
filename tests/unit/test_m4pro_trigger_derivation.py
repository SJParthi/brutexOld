"""Layer-0 trigger-derivation pins for the strict M4-Pro bench gate.

DEC-O1-ENFORCEMENT-REACH-001 / ADR-0027 §D2. A gate that never fires grades
nothing. ``test_o1_claim_backing.py`` is layer 1 — it proves a claim cites a
live bench. This file is layer 0 underneath it: it proves the workflow that
runs that bench is actually *triggered* by a change to it.

Hand-maintaining a ``paths:`` list against an execution list is the same drift
class the repo already pins for ``WHEEL_SET`` ↔ Makefile and
``_EXPECTED_COMPILED_SYMBOLS`` ↔ crate, so the same treatment applies: derive,
do not curate.

THREE directions, not two. The ADR §D2 text names only the first two; the
third is the one that catches a *bench whose assertion is env-gated off*:

1. **execution ⊆ paths** — every ``tests/bench/*.py`` the job's steps invoke
   must appear in the ``push.paths`` filter.
2. **imported sources ⊆ paths** — every ``brutex.*`` module those benches
   import must be covered too, or editing the measured code does not re-run
   the gate that measures it.
3. **BRUTEX_M4PRO_ABS references ⊆ a job that sets it** — a bench whose
   *absolute-tier* assertion sits behind ``_m4pro_abs_active()`` and which no
   job runs with that env set has a **dormant assertion**. Read this precisely:
   the file still runs — ``ci.yml`` invokes the whole ``tests/bench`` directory
   at the robust ratio tier — so its ratio assertions are live. What never
   executes anywhere is the absolute-throughput line. A skip is not a failure,
   so nothing goes red and the dormancy is invisible. This is exactly the shape
   that let ADR-0027's own Honest Envelope credit a canary that never ran.

Every failure message lists the uncovered rows so the fix is one line each,
never a scavenger hunt.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_YML = REPO_ROOT / ".github" / "workflows" / "bench-m4pro.yml"
BENCH_DIR = REPO_ROOT / "tests" / "bench"

#: The env var whose presence turns an absolute-tier assertion from skipped
#: into live. Owned by ``tests/bench/_o1_tiers.py``.
ABS_ENV: str = "BRUTEX_M4PRO_ABS"

#: Compiled extension modules that are not ``brutex.*`` but ARE built from
#: in-repo crate sources, so a crate edit must re-trigger the gate.
_CRATE_OF_MODULE: dict[str, str] = {
    "fno_math": "rust/fno-math",
    "trading_core": "rust/trading-core",
}

_PYTEST_TARGET_RE = re.compile(r"(tests/bench/[A-Za-z0-9_./-]+\.py)")

#: Benches whose ABSOLUTE-tier assertion is dormant: they reference
#: ``BRUTEX_M4PRO_ABS`` but no workflow job invokes them with it set, so that
#: one assertion never executes anywhere. Their RATIO assertions are live —
#: ``ci.yml`` runs the whole ``tests/bench`` directory at the robust tier.
#:
#: This is a RATCHET, not an allowlist: the pin below asserts the live set is a
#: SUBSET of this constant, so a NEW dormant bench fails immediately, and the
#: companion pin fails when an entry here becomes armed — forcing the constant
#: to shrink rather than rot. Same treatment CLAUDE.md §2 rule 12 gives the
#: mutation floors.
#:
#: Arming these is a hardware decision, not a test decision: each asserts a
#: floor that can only be validated on the real M4 Pro, and arming an unvetted
#: floor would ship a claim nobody has measured (Golden Rule #10). Tracked by
#: DEC-O1-ENFORCEMENT-REACH-001; ADR-0027 Honest Envelope #3 is corrected to
#: name this set rather than credit one of them as live.
_DORMANT_ABSOLUTE_TIER: frozenset[str] = frozenset(
    {
        "tests/bench/test_audit_capture_overhead.py",
        "tests/bench/test_feed_rust_bench.py",
        "tests/bench/test_instrument_bulk_upsert.py",
        "tests/bench/test_metrics_recency_tail_cost.py",
        "tests/bench/test_prebuild_parallel_bench.py",
        "tests/bench/test_trade_sim_throughput.py",
        "tests/bench/test_write_consumer_overlap_bench.py",
    }
)


def _load_workflow() -> dict[Any, Any]:
    """Parse the workflow.

    NOTE: PyYAML resolves a bare ``on:`` key to the boolean ``True`` (YAML 1.1
    treats ``on`` as a truthy scalar), so the trigger block is ``data[True]``,
    not ``data["on"]``. The repo's other workflow pins hit the same edge.
    """
    data = yaml.safe_load(BENCH_YML.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "workflow did not parse as a mapping"
    return data


def _push_paths() -> list[str]:
    """The ``push.paths`` filter, as declared."""
    trigger = _load_workflow()[True]
    assert isinstance(trigger, dict)
    paths = trigger["push"]["paths"]
    assert isinstance(paths, list) and paths, "push.paths is empty or missing"
    return [str(entry) for entry in paths]


def _jobs() -> dict[str, Any]:
    jobs = _load_workflow()["jobs"]
    assert isinstance(jobs, dict) and jobs
    return jobs


def _run_scripts(job: dict[str, Any]) -> list[str]:
    """Every ``run:`` block in a job, in step order."""
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    return [str(step["run"]) for step in steps if isinstance(step, dict) and "run" in step]


def _executed_bench_files() -> set[str]:
    """Repo-relative paths of every ``tests/bench/*.py`` the workflow invokes.

    Derived by scanning the shell of each ``run:`` step for bench-file
    arguments. This intentionally reads the *shell*, not a curated list: a new
    ``pytest tests/bench/foo.py`` line is picked up with no test edit.
    """
    found: set[str] = set()
    for job in _jobs().values():
        if not isinstance(job, dict):
            continue
        for script in _run_scripts(job):
            found.update(_PYTEST_TARGET_RE.findall(script))
    assert found, "no bench files parsed out of the workflow — the scan broke"
    return found


def _covers(entry: str, target: str) -> bool:
    """Does one ``paths:`` entry cover this repo-relative file?

    Supports the two forms the workflow actually uses: an exact file path and
    a ``dir/**`` prefix glob. A bare ``*`` inside a segment is deliberately NOT
    supported — the workflow does not use that form, and pretending to handle
    it would make this predicate lie about coverage it cannot verify.
    """
    if entry == target:
        return True
    if entry.endswith("/**"):
        return target.startswith(entry[:-2])
    return False


def _covered(target: str, paths: list[str]) -> bool:
    return any(_covers(entry, target) for entry in paths)


def _imported_sources(bench_file: str) -> set[str]:
    """Repo-relative source paths a bench module imports.

    Resolves ``from brutex.a.b import x`` to ``brutex/a/b.py`` (or the package
    ``__init__.py``), and maps the two compiled extension modules to their
    crate directories. Plain ``import brutex.a.b`` is handled too.
    """
    source = (REPO_ROOT / bench_file).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=bench_file)

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import — tests/bench has no package, so
            # this cannot name a brutex source; skip rather than mis-resolve.
            if node.level == 0 and node.module:
                modules.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)

    resolved: set[str] = set()
    for module in modules:
        root = module.split(".")[0]
        if root in _CRATE_OF_MODULE:
            resolved.add(_CRATE_OF_MODULE[root] + "/Cargo.toml")
            continue
        if root != "brutex":
            continue
        rel = Path(*module.split("."))
        if (REPO_ROOT / rel).with_suffix(".py").is_file():
            resolved.add(str(rel.with_suffix(".py")))
        elif (REPO_ROOT / rel / "__init__.py").is_file():
            resolved.add(str(rel / "__init__.py"))
        # A module that resolves to neither is not an in-repo source (a stub,
        # a namespace shim); it has no file to trigger on, so it is not a gap.
    return resolved


class TestExecutionSubsetOfPaths:
    """Direction 1 — every executed bench file re-triggers the gate."""

    def test_every_executed_bench_appears_in_push_paths(self) -> None:
        paths = _push_paths()
        missing = sorted(f for f in _executed_bench_files() if not _covered(f, paths))
        assert not missing, (
            "bench-m4pro.yml EXECUTES these files but is not TRIGGERED by a "
            "change to them — editing any of them silently skips the gate that "
            "grades it. Add one `paths:` line per row:\n  - "
            + "\n  - ".join(f'"{f}"' for f in missing)
        )

    def test_the_scan_finds_a_known_bench(self) -> None:
        """Anti-vacuity: a broken scan returning {} would pass the pin above."""
        executed = _executed_bench_files()
        assert "tests/bench/test_throughput_absolute.py" in executed
        assert len(executed) >= 15, f"scan found only {len(executed)} bench files"


class TestImportedSourcesSubsetOfPaths:
    """Direction 2 — editing the measured code re-triggers the measurement."""

    def test_every_imported_brutex_source_appears_in_push_paths(self) -> None:
        paths = _push_paths()
        gaps: dict[str, set[str]] = {}
        for bench in sorted(_executed_bench_files()):
            if not (REPO_ROOT / bench).is_file():
                continue
            for src in _imported_sources(bench):
                if not _covered(src, paths):
                    gaps.setdefault(src, set()).add(bench)
        if gaps:
            lines = [
                f'  - "{src}"   # imported by {", ".join(sorted(by))}'
                for src, by in sorted(gaps.items())
            ]
            raise AssertionError(
                "bench-m4pro.yml MEASURES these sources but is not TRIGGERED "
                "by a change to them — a regression lands unmeasured. Add one "
                "`paths:` line per row:\n" + "\n".join(lines)
            )

    def test_import_resolution_finds_a_known_source(self) -> None:
        """Anti-vacuity: an ast walk that resolves nothing would pass above."""
        resolved = _imported_sources("tests/bench/test_engine_combo_mask_o1.py")
        assert "brutex/engine/combo_mask.py" in resolved


class TestAbsoluteTierBenchesAreActuallyRun:
    """Direction 3 — an env-gated assertion that no job arms is not a gate."""

    def _benches_referencing_abs_env(self) -> set[str]:
        found: set[str] = set()
        for path in sorted(BENCH_DIR.glob("test_*.py")):
            if ABS_ENV in path.read_text(encoding="utf-8"):
                found.add(str(path.relative_to(REPO_ROOT)))
        return found

    def _benches_run_with_abs_env(self) -> set[str]:
        """Bench files invoked by a step that has the env var in scope."""
        workflow = _load_workflow()
        workflow_env = workflow.get("env", {}) or {}
        armed: set[str] = set()
        for job in _jobs().values():
            if not isinstance(job, dict):
                continue
            job_env = job.get("env", {}) or {}
            steps = job.get("steps", [])
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, dict) or "run" not in step:
                    continue
                step_env = step.get("env", {}) or {}
                in_scope = ABS_ENV in workflow_env or ABS_ENV in job_env or ABS_ENV in step_env
                # An inline `BRUTEX_M4PRO_ABS=1 pytest ...` counts too.
                script = str(step["run"])
                if in_scope or f"{ABS_ENV}=" in script:
                    armed.update(_PYTEST_TARGET_RE.findall(script))
        return armed

    def _dormant_today(self) -> set[str]:
        armed = self._benches_run_with_abs_env()
        # _o1_tiers.py itself defines the gate helper; it is not a bench.
        return self._benches_referencing_abs_env() - armed - {"tests/bench/_o1_tiers.py"}

    def test_no_new_bench_joins_the_dormant_absolute_set(self) -> None:
        """The ratchet: dormancy may shrink, never grow."""
        new = sorted(self._dormant_today() - _DORMANT_ABSOLUTE_TIER)
        assert not new, (
            f"these benches gate an assertion behind {ABS_ENV} but NO workflow "
            "job runs them with it set, so that assertion never executes "
            "anywhere. Either invoke them from the bench-m4pro job (which sets "
            f"{ABS_ENV} at job level) or, if the floor cannot be validated yet, "
            "add them to _DORMANT_ABSOLUTE_TIER with a stated reason:\n"
            "  - " + "\n  - ".join(new)
        )

    def test_the_dormant_constant_shrinks_when_a_bench_is_armed(self) -> None:
        """The other half of the ratchet: a stale entry must be removed."""
        stale = sorted(_DORMANT_ABSOLUTE_TIER - self._dormant_today())
        assert not stale, (
            "these are listed as dormant but a workflow job now runs them with "
            f"{ABS_ENV} set — the absolute assertion is LIVE. Delete them from "
            "_DORMANT_ABSOLUTE_TIER so the ratchet keeps its teeth:\n"
            "  - " + "\n  - ".join(stale)
        )

    def test_the_dormant_set_is_not_a_blanket_exemption(self) -> None:
        """Anti-vacuity: the ratchet must not cover every bench that exists."""
        referencing = self._benches_referencing_abs_env()
        armed = self._benches_run_with_abs_env() & referencing
        assert armed, "no abs-gated bench is armed at all — the arm scan broke"
        assert referencing > _DORMANT_ABSOLUTE_TIER, (
            "the dormant set covers every abs-gated bench; that is an exemption, not a ratchet"
        )

    def test_the_arm_scan_finds_the_known_absolute_bench(self) -> None:
        """Anti-vacuity: an empty `armed` set would make the pin above trivial."""
        armed = self._benches_run_with_abs_env()
        assert "tests/bench/test_throughput_absolute.py" in armed
