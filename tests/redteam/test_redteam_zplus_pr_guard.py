"""Red-team tests for scripts/ci/zplus-pr-guard.sh — the Z+ PR-body marker guard.

Per DEC-P0-014 (PR #36): every PR body must carry 6 marker phrases. The
guard fails closed (exit 1) when any marker is missing. Operator override
via env var bypass; bypass logged to scripts/ci/pre-merge-overrides.log.

This harness verifies the FAIL-CLOSED + OVERRIDE-AUDIT contract. Mirrors
the pattern in tests/redteam/test_redteam_premerge_guard.py.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "ci" / "zplus-pr-guard.sh"

ALL_6_MARKERS = """
This PR carries the Z+ 15-row matrix and Z+ 7-row resilience matrix and a
full Z+ 7-layer audit. The 4-agent hostile review summary is below.
Honest envelope (Golden Rule #10): test-only change. Decision ID: DEC-P0-XXX.
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
    pr_has_security_label: bool = False,
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
    if pr_has_security_label:
        env["PR_HAS_SECURITY_LABEL"] = "true"
    # Use shell=False explicitly so input piping works the same on POSIX
    return subprocess.run(
        [str(GUARD)],
        input=body,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


class TestZPlusPrGuard:
    """Behavior of `zplus-pr-guard.sh`."""

    def test_guard_script_is_executable(self) -> None:
        assert GUARD.exists(), "guard script must exist"
        assert os.access(GUARD, os.X_OK), "guard must be +x"

    def test_all_6_markers_passes(self) -> None:
        result = _run(ALL_6_MARKERS)
        assert result.returncode == 0, f"expected pass, stderr={result.stderr}"

    def test_missing_one_marker_blocks(self) -> None:
        body = ALL_6_MARKERS.replace("Decision ID", "")
        result = _run(body)
        assert result.returncode == 1
        assert "Decision ID" in result.stderr

    def test_missing_all_markers_blocks(self) -> None:
        result = _run("Just a brief PR description with no markers.")
        assert result.returncode == 1
        # All 6 markers should be listed as missing.
        for marker in (
            "Z+ 15-row matrix",
            "Z+ 7-row resilience matrix",
            "Z+ 7-layer audit",
            "4-agent hostile review",
            "Honest envelope",
            "Decision ID",
        ):
            assert marker in result.stderr

    def test_override_without_reason_rejected(self) -> None:
        result = _run(ALL_6_MARKERS.replace("Decision ID", ""), override=True)
        assert result.returncode == 5
        assert "OPERATOR_OVERRIDE_REASON" in result.stderr

    def test_override_with_reason_allows_and_logs(self, override_log_path: Path) -> None:
        body = ALL_6_MARKERS.replace("Decision ID", "")
        result = _run(
            body,
            override=True,
            reason="emergency revert PR; rewriting matrices in follow-up",
            log_path=override_log_path,
        )
        assert result.returncode == 0
        # Audit log must have a line with TOOL=zplus-pr-guard.
        log_lines = override_log_path.read_text().splitlines()
        assert any("TOOL=zplus-pr-guard" in line for line in log_lines)
        assert any("emergency revert" in line for line in log_lines)

    def test_case_insensitive_match(self) -> None:
        """Marker phrases are matched case-insensitively (grep -i)."""
        body = ALL_6_MARKERS.lower()
        result = _run(body)
        assert result.returncode == 0

    def test_marker_in_table_cell_still_counts(self) -> None:
        """Marker can appear inside a markdown table — substring match."""
        body = """
        | Section | Content |
        |---|---|
        | matrices | Z+ 15-row matrix and Z+ 7-row resilience matrix |
        | audit | Z+ 7-layer audit |
        | review | 4-agent hostile review summary |
        | envelope | Honest envelope: test-only |
        | id | Decision ID: DEC-P0-XXX |
        """
        result = _run(body)
        assert result.returncode == 0


class TestBotExemption:
    """DEC-P0-058 (PR-N+5): renovate/dependabot bots are exempt from the
    6-marker check unconditionally — the pre-DEC-P0-058 security-label
    requirement defeated the "always automated" decree by gating every
    routine cargo/pip/github-actions bump on manual label application.

    Substantive CI safety remains in the CI-OK aggregate gate
    (auto-merge.yml requires every check green); the marker check is
    purely a discipline reminder for human/Claude PRs, which bots
    structurally cannot satisfy."""

    def test_renovate_bot_exempt(self, override_log_path: Path) -> None:
        """Renovate PR body without Z+ markers passes via author exemption."""
        result = _run(
            "Renovate PR body without Z+ markers — should still pass.",
            pr_author="renovate[bot]",
            log_path=override_log_path,
        )
        assert result.returncode == 0
        # Bot exemption MUST be audited.
        lines = override_log_path.read_text().splitlines()
        assert any("EXEMPT=bot" in line and "renovate[bot]" in line for line in lines)

    def test_dependabot_exempt(self, override_log_path: Path) -> None:
        """Dependabot PR body without Z+ markers passes via author exemption."""
        result = _run(
            "Dependabot PR body without markers.",
            pr_author="dependabot[bot]",
            log_path=override_log_path,
        )
        assert result.returncode == 0
        lines = override_log_path.read_text().splitlines()
        assert any("EXEMPT=bot" in line and "dependabot[bot]" in line for line in lines)

    def test_renovate_security_label_ignored(self, override_log_path: Path) -> None:
        """DEC-P0-058: security label is accepted as env var for backwards
        compat but ignored — bot author alone is sufficient."""
        result = _run(
            "Renovate body — security label set explicitly.",
            pr_author="renovate[bot]",
            pr_has_security_label=True,
            log_path=override_log_path,
        )
        assert result.returncode == 0

    def test_non_bot_blocks(self) -> None:
        """Non-bot author with no markers is BLOCKED regardless of label."""
        result = _run(
            "Human PR body — no markers.",
            pr_author="some-developer",
            pr_has_security_label=True,
        )
        assert result.returncode == 1

    def test_arbitrary_bot_author_blocks(self) -> None:
        """Only the pinned bot authors are allowed — not custom bots."""
        result = _run(
            "Custom bot body.",
            pr_author="evil-bot[bot]",
            pr_has_security_label=True,
        )
        assert result.returncode == 1

    def test_github_actions_bot_exempt(self, override_log_path: Path) -> None:
        """DEC-BENCH-ABS-BASELINE-001: the bless-baseline bot PR carries no
        Z+ matrices — github-actions[bot] is author-exempt (audit-logged)."""
        result = _run(
            "Bless-baseline bot PR body (measured candidate, no Z+ markers).",
            pr_author="github-actions[bot]",
            log_path=override_log_path,
        )
        assert result.returncode == 0
        lines = override_log_path.read_text().splitlines()
        assert any("EXEMPT=bot" in line and "github-actions[bot]" in line for line in lines)

    def test_github_actions_lookalike_blocks(self) -> None:
        """The exemption is EXACT — a lookalike author stays fail-closed."""
        assert _run("Lookalike body.", pr_author="github-actions").returncode == 1
        assert _run("Lookalike body.", pr_author="my-github-actions[bot]").returncode == 1
