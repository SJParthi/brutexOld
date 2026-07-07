"""Red-team: the silent-re-baseline is IMPOSSIBLE + the gate is undeletable.

DEC-BENCH-ABS-BASELINE-001. Executes the REAL ``govern`` subcommand of
``scripts/ci/bench_baseline_gate.py`` end-to-end inside SCRATCH git repos
with a stubbed ``gh`` on PATH (the ``test_redteam_fork_pr_harden.py``
idiom), proving every smuggle path exits non-zero and every ledgered path
exits zero; plus the gate-undeletable pin — defanging the ci.yml wiring
reds this always-on suite before the gate could silently vanish.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from brutex.observability import bench_baseline as bb

from tests.unit.test_bench_baseline import (
    FILENAME,
    NEW_DEC,
    RUN_URL_2,
    make_baseline_doc,
    make_entry_doc,
    make_provenance,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "bench_baseline_gate.py"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

GH_STUB_OK = """#!/bin/sh
# Stubbed gh: confirms any queried run as a successful bench-m4pro run of
# commit 0*40 (the repo PIN must refuse a foreign slug BEFORE any query).
echo '{"path": ".github/workflows/bench-m4pro.yml", "status": "completed", "conclusion": "success", "head_sha": "0000000000000000000000000000000000000000"}'
"""

GH_STUB_404 = """#!/bin/sh
echo 'HTTP 404: Not Found (https://api.github.com/...)' >&2
exit 1
"""

GH_STUB_FAILED_RUN = """#!/bin/sh
# Stubbed gh: the cited run exists but CONCLUDED failure (e.g. cancelled/red).
echo '{"path": ".github/workflows/bench-m4pro.yml", "status": "completed", "conclusion": "failure", "head_sha": "0000000000000000000000000000000000000000"}'
"""

#: The slug every fixture provenance URL lives on (tests/unit RUN_URL_*).
THIS_REPO_SLUG = "SJParthi/bruteX"


class Scratch:
    """A scratch git repo with a base commit + helpers to commit changes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.baselines = root / "tests" / "bench" / "baselines"
        self.migration = root / "docs" / "MIGRATION.md"

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0, f"git {args}: {proc.stderr}"
        return proc

    def commit_all(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)


@pytest.fixture
def scratch(tmp_path: Path) -> Scratch:
    """A fresh scratch repo whose base commit holds one blessed baseline."""
    repo = Scratch(tmp_path / "scratch")
    repo.baselines.mkdir(parents=True)
    repo.migration.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo.root)], check=True)
    repo.git("config", "user.email", "redteam@test")
    repo.git("config", "user.name", "redteam")
    repo.baselines.joinpath(FILENAME).write_text(json.dumps(make_baseline_doc()), encoding="utf-8")
    repo.migration.write_text("1531. **DEC-BENCH-ABS-BASELINE-001 — bootstrap**\n", "utf-8")
    repo.commit_all("base")
    repo.git("tag", "base")
    return repo


def run_govern(
    scratch_repo: Scratch,
    tmp_path: Path,
    *,
    gh_stub: str = GH_STUB_OK,
    repo_slug: str | None = THIS_REPO_SLUG,
) -> subprocess.CompletedProcess[str]:
    """The REAL govern subcommand inside the scratch repo, gh stubbed.

    *repo_slug* becomes ``$GITHUB_REPOSITORY`` (the repo pin); ``None``
    strips it to prove the gate fails CLOSED without the pin."""
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir(exist_ok=True)
    gh = stub_dir / "gh"
    gh.write_text(gh_stub, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env.get('PATH', '')}"
    if repo_slug is None:
        env.pop("GITHUB_REPOSITORY", None)
    else:
        env["GITHUB_REPOSITORY"] = repo_slug
    return subprocess.run(
        [sys.executable, str(SCRIPT), "govern", "--base", "base"],
        cwd=scratch_repo.root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def bump_cell(scratch_repo: Scratch, **cell_over: Any) -> None:
    """Commit a modified bar_lookup cell (value 55.0) onto the scratch HEAD."""
    entries = {entry: make_entry_doc(entry) for entry in sorted(bb.REQUIRED_ENTRY_KEYS)}
    entries["bar_lookup"] = make_entry_doc(
        "bar_lookup",
        value_ns_per_op=55.0,
        samples_ns_per_op=[55.0, 56.0, 57.0],
        **cell_over,
    )
    scratch_repo.baselines.joinpath(FILENAME).write_text(
        json.dumps(make_baseline_doc(entries=entries)), encoding="utf-8"
    )
    scratch_repo.commit_all("bump bar_lookup")


def add_ledger_line(scratch_repo: Scratch, line: str) -> None:
    with scratch_repo.migration.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


HAPPY_LEDGER = f"1522. **{NEW_DEC} — re-bless bar_lookup to 55.0 (measured kernel win)**"
FRESH_PROVENANCE = make_provenance(run_url=RUN_URL_2)


class TestSilentBumpImpossible:
    def test_bare_numeric_bump_no_ledger_fails(self, scratch: Scratch, tmp_path: Path) -> None:
        bump_cell(scratch)
        proc = run_govern(scratch, tmp_path)
        assert proc.returncode == 1
        assert "bootstrap" in proc.stdout + proc.stderr

    def test_ledgered_bump_with_fresh_run_passes(self, scratch: Scratch, tmp_path: Path) -> None:
        bump_cell(scratch, decision_id=NEW_DEC, provenance=FRESH_PROVENANCE)
        add_ledger_line(scratch, HAPPY_LEDGER)
        scratch.commit_all("ledger entry")
        proc = run_govern(scratch, tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "ledgered + live-verified" in proc.stdout

    def test_bump_reusing_old_dec_fails(self, scratch: Scratch, tmp_path: Path) -> None:
        """The base cell's id (bootstrap) re-used → refused even if ledgered."""
        bump_cell(scratch, provenance=FRESH_PROVENANCE)
        add_ledger_line(scratch, HAPPY_LEDGER)
        scratch.commit_all("ledger entry")
        proc = run_govern(scratch, tmp_path)
        assert proc.returncode == 1

    def test_dec_in_file_body_but_migration_untouched_fails(
        self, scratch: Scratch, tmp_path: Path
    ) -> None:
        bump_cell(scratch, decision_id=NEW_DEC, provenance=FRESH_PROVENANCE)
        proc = run_govern(scratch, tmp_path)
        assert proc.returncode == 1
        assert "MIGRATION.md added lines" in proc.stdout + proc.stderr

    def test_ledger_missing_value_token_fails(self, scratch: Scratch, tmp_path: Path) -> None:
        bump_cell(scratch, decision_id=NEW_DEC, provenance=FRESH_PROVENANCE)
        add_ledger_line(scratch, f"1522. **{NEW_DEC} — re-bless bar_lookup (no number)**")
        scratch.commit_all("ledger entry")
        proc = run_govern(scratch, tmp_path)
        assert proc.returncode == 1
        assert "55.0" in proc.stdout + proc.stderr

    def test_removal_unledgered_fails(self, scratch: Scratch, tmp_path: Path) -> None:
        entries = {
            entry: make_entry_doc(entry)
            for entry in sorted(bb.REQUIRED_ENTRY_KEYS - {"bar_lookup"})
        }
        scratch.baselines.joinpath(FILENAME).write_text(
            json.dumps(make_baseline_doc(entries=entries)), encoding="utf-8"
        )
        scratch.commit_all("drop bar_lookup cell")
        proc = run_govern(scratch, tmp_path)
        assert proc.returncode == 1
        assert "REMOVED without" in proc.stdout + proc.stderr

    def test_additive_bootstrap_file_passes(self, scratch: Scratch, tmp_path: Path) -> None:
        """A brand-new hardware key's first bless rides the standing DEC."""
        new_key = "newbox-8c8p0e-16g-arm64"
        doc = make_baseline_doc(
            hardware_key=new_key,
            fingerprint={
                "model_slug": "newbox",
                "arch": "arm64",
                "os_family": "darwin",
                "physical_cores": 8,
                "performance_cores": 8,
                "efficiency_cores": 0,
                "total_ram_gib": 16,
            },
        )
        scratch.baselines.joinpath(f"{new_key}.json").write_text(json.dumps(doc), "utf-8")
        scratch.commit_all("first bless for newbox")
        proc = run_govern(scratch, tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_corrupt_head_json_fails(self, scratch: Scratch, tmp_path: Path) -> None:
        scratch.baselines.joinpath(FILENAME).write_text("{corrupt", encoding="utf-8")
        scratch.commit_all("corrupt the baseline")
        proc = run_govern(scratch, tmp_path)
        assert proc.returncode == 1
        assert "invalid" in proc.stdout + proc.stderr

    def test_forged_run_url_fails_on_live_refutation(
        self, scratch: Scratch, tmp_path: Path
    ) -> None:
        """A ledgered bump citing a run GitHub says does not exist → refused."""
        bump_cell(scratch, decision_id=NEW_DEC, provenance=FRESH_PROVENANCE)
        add_ledger_line(scratch, HAPPY_LEDGER)
        scratch.commit_all("ledger entry")
        proc = run_govern(scratch, tmp_path, gh_stub=GH_STUB_404)
        assert proc.returncode == 1
        assert "not found" in proc.stdout + proc.stderr

    def test_foreign_repo_provenance_fails_despite_confirming_gh(
        self, scratch: Scratch, tmp_path: Path
    ) -> None:
        """Review round 1 (BLOCKER): a fully-ledgered bump whose provenance
        cites a COMPLETED run on an attacker-controlled repo (bruteX is
        public — any fork keeps bench-m4pro.yml at the required path with a
        chosen head_sha) must be refused even when the GitHub API would
        confirm that run. The pre-fix gate printed 'ledgered + live-verified'
        for exactly this input."""
        bump_cell(
            scratch,
            decision_id=NEW_DEC,
            provenance=make_provenance(
                run_url="https://github.com/evil-attacker/bruteX/actions/runs/42"
            ),
        )
        add_ledger_line(scratch, HAPPY_LEDGER)
        scratch.commit_all("ledger entry")
        proc = run_govern(scratch, tmp_path, gh_stub=GH_STUB_OK)
        assert proc.returncode == 1
        combined = proc.stdout + proc.stderr
        assert "foreign repo" in combined
        assert "evil-attacker/bruteX" in combined
        assert "ledgered + live-verified" not in combined

    def test_missing_github_repository_env_fails_closed(
        self, scratch: Scratch, tmp_path: Path
    ) -> None:
        """Review round 1 (BLOCKER): without $GITHUB_REPOSITORY the repo pin
        cannot be applied — a baseline change fails CLOSED (exit 2), never
        verified-by-default."""
        bump_cell(scratch, decision_id=NEW_DEC, provenance=FRESH_PROVENANCE)
        add_ledger_line(scratch, HAPPY_LEDGER)
        scratch.commit_all("ledger entry")
        proc = run_govern(scratch, tmp_path, repo_slug=None)
        assert proc.returncode == 2
        assert "GITHUB_REPOSITORY" in proc.stdout + proc.stderr

    def test_failed_run_conclusion_fails(self, scratch: Scratch, tmp_path: Path) -> None:
        """Review round 1: a completed run that CONCLUDED failure/cancelled
        is not valid provenance — only conclusion == success verifies."""
        bump_cell(scratch, decision_id=NEW_DEC, provenance=FRESH_PROVENANCE)
        add_ledger_line(scratch, HAPPY_LEDGER)
        scratch.commit_all("ledger entry")
        proc = run_govern(scratch, tmp_path, gh_stub=GH_STUB_FAILED_RUN)
        assert proc.returncode == 1
        assert "!= success" in proc.stdout + proc.stderr

    def test_stray_file_fails(self, scratch: Scratch, tmp_path: Path) -> None:
        scratch.baselines.joinpath("evil.py").write_text("x = 1", encoding="utf-8")
        scratch.commit_all("stray file")
        proc = run_govern(scratch, tmp_path)
        assert proc.returncode == 1
        assert "stray" in proc.stdout + proc.stderr

    def test_no_changes_passes(self, scratch: Scratch, tmp_path: Path) -> None:
        proc = run_govern(scratch, tmp_path)
        assert proc.returncode == 0
        assert "nothing to govern" in proc.stdout


class TestGateUndeletable:
    """Defanging any layer of the gate reds this always-on suite."""

    def test_ci_bench_job_carries_both_gate_steps(self) -> None:
        text = CI_YML.read_text(encoding="utf-8")
        assert "Bench-baseline structural gate" in text
        assert "Bench-baseline governance gate (no silent re-baseline)" in text
        assert "bench_baseline_gate.py validate" in text
        assert "bench_baseline_gate.py govern" in text

    def test_gate_script_and_core_exist(self) -> None:
        assert SCRIPT.is_file()
        assert (REPO_ROOT / "brutex" / "observability" / "bench_baseline.py").is_file()

    def test_no_override_env_exists_in_gate(self) -> None:
        """S9: the governance gate has NO escape-hatch env var."""
        text = SCRIPT.read_text(encoding="utf-8")
        for token in ("ALLOW_MISSING", "SKIP_GOVERN", "OVERRIDE"):
            assert token not in text
        core = (REPO_ROOT / "brutex" / "observability" / "bench_baseline.py").read_text(
            encoding="utf-8"
        )
        assert "os.environ" not in core  # the core is pure — no env reads at all
