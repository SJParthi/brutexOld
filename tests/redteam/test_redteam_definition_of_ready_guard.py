"""Red-team tests for scripts/ci/definition-of-ready-guard.sh — the plan-first gate.

Per DEC-DEFINITION-OF-READY-001 (2026-06-05): every PR body must carry the 5
plan-first sections (Scenarios covered / Failure modes / Real-run proof /
Rollback plan / Out of scope). The guard fails closed (exit 1) when any
section is missing. Operator override via env var bypass; bypass logged to
scripts/ci/pre-merge-overrides.log. renovate/dependabot bots are author-exempt.

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
        """Only renovate[bot] / dependabot[bot] are allowed — not custom bots."""
        result = _run("Custom bot body.", pr_author="evil-bot[bot]")
        assert result.returncode == 1
