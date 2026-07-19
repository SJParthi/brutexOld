"""Shell-syntax pin over every workflow ``run:`` block.

Regression net for the LAP-8 fold seam (branch
``integration/club-lap8-lake-guards``, PR #1654): the member #1513 ci.yml
weave (commit 7c6c7bb80) dropped a ``fi`` when inserting the
DEC-SESSION-DENSITY-CENSUS-001 stanza after the ``sidecar_read`` stanza,
so the REQUIRED "Strict per-module 100% coverage gate" step's bash script
died with ``syntax error: unexpected end of file`` — and every gate AFTER
the seam silently never ran (CI run 29647133025, job 88089035226). No
member suite could catch a workflow-file weave defect, so this pin runs
``bash -n`` over EVERY bash-shelled ``run:`` block of EVERY workflow —
an unbalanced ``if``/``fi`` (or any other shell-syntax breakage) from a
future fold now fails a named unit test instead of a red required CI job.

Fails-before proven: on the pre-fix tree this test fails on ci.yml's
"Strict per-module 100% coverage gate" step with the exact CI error
(``line 2262: syntax error: unexpected end of file``).
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# GitHub Actions defaults `run:` steps to `bash -e {0}` on Linux runners
# when no shell is specified — so an unspecified shell IS bash here.
_DEFAULT_SHELL = "bash"


def _workflow_files() -> list[Path]:
    files = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))
    assert files, f"no workflow files under {WORKFLOWS_DIR}"
    return files


def _shell_of(step: dict[str, Any], job: dict[str, Any], doc: dict[str, Any]) -> str:
    """Resolve the effective shell: step > job defaults > workflow defaults."""
    for scope in (
        step,
        (job.get("defaults") or {}).get("run", {}),
        (doc.get("defaults") or {}).get("run", {}),
    ):
        shell = scope.get("shell")
        if shell is not None:
            return str(shell)
    return _DEFAULT_SHELL


def _bash_run_blocks(path: Path) -> list[tuple[str, str, str]]:
    """Every bash-shelled ``run:`` block as (job, step-name, script)."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    blocks: list[tuple[str, str, str]] = []
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), f"{path.name}: no jobs mapping"
    for job_name, job in jobs.items():
        assert isinstance(job, dict)
        for index, step in enumerate(job.get("steps") or []):
            assert isinstance(step, dict)
            run = step.get("run")
            if not isinstance(run, str):
                continue
            if not _shell_of(step, job, doc).startswith("bash"):
                continue
            label = str(step.get("name") or f"step[{index}]")
            blocks.append((str(job_name), label, run))
    return blocks


@pytest.mark.parametrize("workflow", _workflow_files(), ids=lambda p: p.name)
def test_every_bash_run_block_parses(workflow: Path) -> None:
    """``bash -n`` accepts every bash run block (no unbalanced if/fi etc.)."""
    failures: list[str] = []
    checked = 0
    for job_name, step_label, script in _bash_run_blocks(workflow):
        checked += 1
        with tempfile.NamedTemporaryFile("w", suffix=".sh", encoding="utf-8") as handle:
            handle.write(script)
            handle.flush()
            result = subprocess.run(
                ["bash", "-n", handle.name],
                capture_output=True,
                text=True,
                check=False,
            )
        if result.returncode != 0:
            failures.append(
                f"{workflow.name} :: job {job_name!r} :: step "
                f"{step_label!r}:\n{result.stderr.strip()}"
            )
    assert not failures, (
        "workflow run block(s) with broken bash syntax — a fold-seam "
        "weave defect (the LAP-8 dropped-fi class):\n" + "\n".join(failures)
    )
    assert checked >= 0  # a workflow with zero bash blocks is legal


def test_ci_yml_has_bash_blocks() -> None:
    """Anti-vacuous tooth: ci.yml genuinely contributes bash run blocks."""
    blocks = _bash_run_blocks(WORKFLOWS_DIR / "ci.yml")
    assert len(blocks) >= 10, f"only {len(blocks)} bash blocks found in ci.yml"
