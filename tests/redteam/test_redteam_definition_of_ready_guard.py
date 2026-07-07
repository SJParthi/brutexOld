"""Red-team tests for scripts/ci/definition-of-ready-guard.sh — the plan-first gate.

Per DEC-DEFINITION-OF-READY-001 (2026-06-05): every PR body must carry the 5
plan-first sections (Scenarios covered / Failure modes / Real-run proof /
Rollback plan / Out of scope). The guard fails closed (exit 1) when any
section is missing. Operator override via env var bypass; bypass logged to
scripts/ci/pre-merge-overrides.log. renovate/dependabot/github-actions bots are author-exempt
(the last per DEC-BENCH-ABS-BASELINE-001 — the bless-baseline bot PR).

This harness verifies the FAIL-CLOSED + OVERRIDE-AUDIT + BOT-EXEMPT contract.
Mirrors tests/redteam/test_redteam_zplus_pr_guard.py.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "ci" / "definition-of-ready-guard.sh"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

ALL_5_SECTIONS = """
## Definition of Ready

### Scenarios covered
Happy path, the empty-input edge, the failure path, and the concurrent case.

### Failure modes
On a bad input the loader raises; on a missing file it exits 2.

### Real-run proof
`make run` boots and serves HTTP 200 on /health.

### Rollback plan
`git revert` this commit — CI-only change, no state migration.

### Out of scope
No brutex/ source touched; engine bricks owned by the parallel session.
"""


@pytest.fixture
def override_log_path() -> Generator[Path]:
    """Isolate the override-log path so tests don't pollute the real one."""
    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as tmp:
        path = Path(tmp.name)
    yield path
    if path.exists():
        path.unlink()


def _run(
    body: str,
    *,
    override: bool = False,
    reason: str | None = None,
    log_path: Path | None = None,
    pr_author: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the guard with `body` on stdin and return CompletedProcess."""
    env = os.environ.copy()
    if override:
        env["OPERATOR_OVERRIDE"] = "true"
    if reason is not None:
        env["OPERATOR_OVERRIDE_REASON"] = reason
    if log_path is not None:
        env["OVERRIDE_LOG_PATH"] = str(log_path)
    if pr_author is not None:
        env["PR_AUTHOR"] = pr_author
    return subprocess.run(
        [str(GUARD)],
        input=body,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


class TestDefinitionOfReadyGuard:
    """Behavior of `definition-of-ready-guard.sh`."""

    def test_guard_script_is_executable(self) -> None:
        assert GUARD.exists(), "guard script must exist"
        assert os.access(GUARD, os.X_OK), "guard must be +x"

    def test_all_5_sections_passes(self) -> None:
        result = _run(ALL_5_SECTIONS)
        assert result.returncode == 0, f"expected pass, stderr={result.stderr}"

    def test_missing_one_section_blocks(self) -> None:
        body = ALL_5_SECTIONS.replace("Rollback plan", "")
        result = _run(body)
        assert result.returncode == 1
        assert "Rollback plan" in result.stderr

    def test_missing_all_sections_blocks(self) -> None:
        result = _run("Just a brief PR description with no plan.")
        assert result.returncode == 1
        for marker in (
            "Scenarios covered",
            "Failure modes",
            "Real-run proof",
            "Rollback plan",
            "Out of scope",
        ):
            assert marker in result.stderr

    def test_override_without_reason_rejected(self) -> None:
        result = _run(ALL_5_SECTIONS.replace("Rollback plan", ""), override=True)
        assert result.returncode == 5
        assert "OPERATOR_OVERRIDE_REASON" in result.stderr

    def test_override_with_reason_allows_and_logs(self, override_log_path: Path) -> None:
        body = ALL_5_SECTIONS.replace("Rollback plan", "")
        result = _run(
            body,
            override=True,
            reason="emergency hotfix; plan documented in the linked incident",
            log_path=override_log_path,
        )
        assert result.returncode == 0
        log_lines = override_log_path.read_text().splitlines()
        assert any("TOOL=definition-of-ready-guard" in line for line in log_lines)
        assert any("emergency hotfix" in line for line in log_lines)

    def test_case_insensitive_match(self) -> None:
        """Section markers are matched case-insensitively (grep -i)."""
        result = _run(ALL_5_SECTIONS.lower())
        assert result.returncode == 0

    def test_markers_in_table_cells_count(self) -> None:
        """Markers can appear inside a markdown table — substring match."""
        body = """
        | Section | Content |
        |---|---|
        | Scenarios covered | happy + edge + failure |
        | Failure modes | raises on bad input |
        | Real-run proof | make run -> 200 |
        | Rollback plan | git revert |
        | Out of scope | no source touched |
        """
        result = _run(body)
        assert result.returncode == 0


class TestBotExemption:
    """renovate/dependabot bots open only dep-bump PRs and structurally
    cannot carry a design plan — they are author-exempt, mirroring
    DEC-P0-058. Substantive safety remains in the ci-ok aggregate gate."""

    def test_renovate_bot_exempt(self, override_log_path: Path) -> None:
        result = _run(
            "Renovate PR body without a plan — should still pass.",
            pr_author="renovate[bot]",
            log_path=override_log_path,
        )
        assert result.returncode == 0
        lines = override_log_path.read_text().splitlines()
        assert any("EXEMPT=bot" in line and "renovate[bot]" in line for line in lines)

    def test_dependabot_exempt(self, override_log_path: Path) -> None:
        result = _run(
            "Dependabot PR body without a plan.",
            pr_author="dependabot[bot]",
            log_path=override_log_path,
        )
        assert result.returncode == 0
        lines = override_log_path.read_text().splitlines()
        assert any("EXEMPT=bot" in line and "dependabot[bot]" in line for line in lines)

    def test_non_bot_blocks(self) -> None:
        result = _run("Human PR body — no plan.", pr_author="some-developer")
        assert result.returncode == 1

    def test_arbitrary_bot_author_blocks(self) -> None:
        """Only the pinned bot authors are allowed — not custom bots."""
        result = _run("Custom bot body.", pr_author="evil-bot[bot]")
        assert result.returncode == 1

    def test_github_actions_bot_exempt(self, override_log_path: Path) -> None:
        """DEC-BENCH-ABS-BASELINE-001: the bless-baseline bot PR carries no
        human plan-first body — github-actions[bot] is author-exempt."""
        result = _run(
            "Bless-baseline bot PR body (measured candidate, no plan sections).",
            pr_author="github-actions[bot]",
            log_path=override_log_path,
        )
        assert result.returncode == 0
        lines = override_log_path.read_text().splitlines()
        assert any("EXEMPT=bot" in line and "github-actions[bot]" in line for line in lines)

    def test_github_actions_lookalike_blocks(self) -> None:
        """The exemption is EXACT — a lookalike author stays fail-closed."""
        assert _run("Lookalike body.", pr_author="github-actions").returncode == 1
        assert _run("Lookalike body.", pr_author="evil-github-actions[bot]").returncode == 1


def _dor_job_block() -> str:
    """Return the `definition-of-ready` job's YAML text up to the next job.

    The DoR job is followed by the `dec-id-validator` job (a sibling at the
    same indent). Slicing on that boundary keeps the assertions scoped to the
    DoR job and resilient to growth elsewhere in ci.yml.
    """
    text = CI_YML.read_text(encoding="utf-8")
    assert "definition-of-ready:" in text, "ci.yml lost the definition-of-ready job"
    block = text.split("definition-of-ready:", 1)[1]
    # Stop at the next top-level (4-space) job key so assertions can't leak into
    # an adjacent job. dec-id-validator is the documented next sibling.
    return block.split("\n  dec-id-validator:", 1)[0]


class TestDefinitionOfReadyWorkflowLiveBody:
    """DEC-DOR-LIVE-BODY-001 — the workflow must evaluate the LIVE PR body at
    run-time and never paint a merged/closed PR red.

    The guard script stays strict (proven by the classes above); these tests
    pin the *workflow* contract that feeds it a fresh body and skips a settled
    PR, so the stale-snapshot false-red can never reappear.
    """

    def test_dor_job_skips_merged_or_closed_pr(self) -> None:
        """The job `if:` skips an already merged or closed PR.

        A post-merge re-run replays the stale event body; skipping a settled PR
        means such a re-run can never repaint a green/merged PR red.
        """
        block = _dor_job_block()
        # The job-level if must gate on both closed-state and merged.
        assert "github.event.pull_request.state != 'closed'" in block, (
            "DoR job must skip a closed PR (DEC-DOR-LIVE-BODY-001)"
        )
        assert "github.event.pull_request.merged != true" in block, (
            "DoR job must skip a merged PR (DEC-DOR-LIVE-BODY-001)"
        )

    def test_dor_job_stays_pull_request_scoped(self) -> None:
        """The skip is ADDED to — not a replacement of — the pull_request gate.

        An OPEN pull_request is still evaluated, so the gate stays strict for
        in-flight work (it must still block a genuinely incomplete body).
        """
        block = _dor_job_block()
        assert "github.event_name == 'pull_request'" in block, (
            "DoR job must remain pull_request-scoped (open PRs stay gated)"
        )

    def test_dor_fetches_live_body_via_gh_cli(self) -> None:
        """The guard step fetches the LIVE body via `gh pr view`, not only the
        `github.event.pull_request.body` event snapshot."""
        block = _dor_job_block()
        assert "gh pr view" in block and "--json body" in block, (
            "DoR step must fetch the live PR body (DEC-DOR-LIVE-BODY-001), "
            "not rely solely on the stale event snapshot"
        )
        assert "GH_TOKEN: ${{ github.token }}" in block, (
            "gh CLI needs the workflow token to read the PR body"
        )

    def test_dor_falls_back_to_event_snapshot_not_fail_open(self) -> None:
        """A failed live fetch falls back to the event snapshot — fail-SAFE.

        The guard then still runs against *some* body and can still block; it
        must never silently pass (fail-open) when the live fetch fails.
        """
        block = _dor_job_block()
        assert "PR_BODY_FALLBACK: ${{ github.event.pull_request.body }}" in block, (
            "a live-fetch failure must fall back to the event snapshot"
        )
        # The guard is still invoked unconditionally after the body is resolved.
        assert "bash scripts/ci/definition-of-ready-guard.sh" in block, (
            "the guard must still run (and can still block) after body resolution"
        )
