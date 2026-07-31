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
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PR_TEMPLATE = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"

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


def _zplus_job_block() -> str:
    """Return the `zplus-pr-guard` job's YAML text up to the next job.

    The Z+ job is followed by the `definition-of-ready` job (a sibling at the
    same indent). Slicing on that boundary keeps the assertions scoped to the
    Z+ job and resilient to growth elsewhere in ci.yml.
    """
    text = CI_YML.read_text(encoding="utf-8")
    assert "zplus-pr-guard:" in text, "ci.yml lost the zplus-pr-guard job"
    block = text.split("  zplus-pr-guard:", 1)[1]
    return block.split("\n  definition-of-ready:", 1)[0]


def _dec_id_job_block() -> str:
    """Return the `dec-id-validator` job's YAML text up to the next job.

    The validator is followed by `bugfix-regression-scan` (a sibling at the
    same indent).
    """
    text = CI_YML.read_text(encoding="utf-8")
    assert "dec-id-validator:" in text, "ci.yml lost the dec-id-validator job"
    block = text.split("  dec-id-validator:", 1)[1]
    return block.split("\n  bugfix-regression-scan:", 1)[0]


class TestPrBodyGatesReadLiveBody:
    """The two snapshot-bound PR-body gates must read the LIVE body.

    Root cause pinned here (measured on PR #1754, 2026-07-25): ci.yml's
    ``on: pull_request:`` carries NO ``types:``, so it defaults to
    ``[opened, synchronize, reopened]`` — an ``edited`` body never re-fires
    CI — and ``rerun_failed_jobs`` replays the ORIGINAL event payload. A gate
    bound to ``github.event.pull_request.body`` therefore stays red FOREVER
    on a body that was corrected after dispatch, with no operator action able
    to clear it short of an unrelated push.

    ``definition-of-ready`` was already fixed this way by
    DEC-DOR-LIVE-BODY-001 (MIGRATION.md #983); these tests extend the same
    contract to its two siblings so the class cannot reappear.

    Note the direction of the change: reading live makes the gates STRICTER,
    not weaker. A body that LOSES a required marker after dispatch now goes
    red on the next run, where the stale snapshot would have kept it green.
    """

    def test_zplus_fetches_live_body_via_gh_cli(self) -> None:
        """The Z+ step fetches the LIVE body, not only the event snapshot."""
        block = _zplus_job_block()
        assert "gh pr view" in block and "--json body" in block, (
            "Z+ guard step must fetch the live PR body — a snapshot-bound gate "
            "cannot see a corrected body (edited never re-fires CI)"
        )
        assert "GH_TOKEN: ${{ github.token }}" in block, (
            "gh CLI needs the workflow token to read the PR body"
        )

    def test_zplus_falls_back_to_event_snapshot_not_fail_open(self) -> None:
        """A failed live fetch falls back to the snapshot — fail-SAFE."""
        block = _zplus_job_block()
        assert "PR_BODY_FALLBACK: ${{ github.event.pull_request.body }}" in block, (
            "a live-fetch failure must fall back to the event snapshot"
        )
        assert "bash scripts/ci/zplus-pr-guard.sh" in block, (
            "the guard must still run (and can still block) after body resolution"
        )

    def test_zplus_stays_pull_request_scoped(self) -> None:
        """The live fetch is ADDED to — not a replacement of — the PR gate."""
        assert "github.event_name == 'pull_request'" in _zplus_job_block(), (
            "Z+ job must remain pull_request-scoped (open PRs stay gated)"
        )

    def test_dec_id_validator_fetches_live_body_via_gh_cli(self) -> None:
        """The DEC-ID citation scan reads the LIVE body."""
        block = _dec_id_job_block()
        assert "gh pr view" in block and "--json body" in block, (
            "DEC-ID validator must fetch the live PR body — a corrected "
            "citation is invisible to a snapshot-bound gate"
        )
        assert "GH_TOKEN: ${{ github.token }}" in block, (
            "gh CLI needs the workflow token to read the PR body"
        )

    def test_dec_id_validator_falls_back_to_event_snapshot(self) -> None:
        """A failed live fetch falls back to the snapshot — fail-SAFE."""
        block = _dec_id_job_block()
        assert "PR_BODY_FALLBACK: ${{ github.event.pull_request.body }}" in block, (
            "a live-fetch failure must fall back to the event snapshot"
        )
        assert "python3 scripts/ci/validate_dec_ids.py" in block, (
            "the validator must still run (and can still block) after body resolution"
        )

    def test_dec_id_validator_keeps_appropriateness_inputs(self) -> None:
        """The appropriateness heuristic's inputs survive the live-body change.

        HONEST residual (Golden Rule #10): PR_TITLE stays the event snapshot.
        It feeds only the appropriateness half — the validator SKIPS that half
        (exit 0) when PR_TITLE is unset — so a stale title can never break the
        citation-EXISTENCE half this change is about.
        """
        block = _dec_id_job_block()
        for var in ("PR_TITLE:", "PR_AUTHOR:", "BASE_REF:"):
            assert var in block, f"dec-id-validator lost {var} (appropriateness input)"
        assert "PR_FILES" in block, "dec-id-validator lost the changed-file list"

    def test_ci_pull_request_trigger_has_no_edited_type(self) -> None:
        """Pin the trigger fact this whole class exists because of.

        If someone later adds ``types: [... edited ...]``, an edited body WOULD
        re-fire CI and the live fetch becomes belt-and-braces rather than the
        only mechanism. This test does not forbid that — it fails loudly so the
        rationale above is re-read and updated rather than silently rotting.
        """
        text = CI_YML.read_text(encoding="utf-8")
        head = text.split("\nenv:", 1)[0]
        pr_trigger = head.split("  pull_request:", 1)[1].split("\n  merge_group:", 1)[0]
        assert "types:" not in pr_trigger, (
            "ci.yml's pull_request trigger gained a `types:` list — re-read "
            "TestPrBodyGatesReadLiveBody's docstring and update it"
        )


class TestRepoTemplateSatisfiesItsOwnGuard:
    """The repo's PR template must pass the fail-closed gate it is judged by.

    Found the hard way on PR #1774: the guard matches HEADING LITERALS
    byte-for-byte, and ``.github/PULL_REQUEST_TEMPLATE.md`` scaffolded only
    2 of the 6 required markers. An author who filled the template in
    faithfully still went RED, and the failure names the *missing marker*
    rather than "your template is short four sections" — so the diagnosis
    costs a round trip every time. The template now carries all six; these
    pins keep it that way.
    """

    def test_template_passes_the_real_guard(self) -> None:
        """Drive the SHIPPED guard over the SHIPPED template, non-bot author.

        Not a re-implementation of the marker list — the real script, so a
        marker added to the guard without a matching template section fails
        here rather than on some future contributor's PR.
        """
        body = PR_TEMPLATE.read_text(encoding="utf-8")
        result = _run(body, pr_author="a-human")
        assert result.returncode == 0, (
            "the PR template no longer satisfies scripts/ci/zplus-pr-guard.sh — "
            "add the missing section to the template (do NOT relax the guard): "
            f"{result.stdout}{result.stderr}"
        )

    def test_template_carries_every_marker_the_guard_requires(self) -> None:
        """Second, independent check: parse the guard's own marker list.

        ``test_template_passes_the_real_guard`` would also pass if the guard
        were accidentally neutered. This one reads REQUIRED_MARKERS out of the
        script source, so a guard that stops requiring a marker is visible as
        a diff here rather than a silent loosening.
        """
        guard_src = GUARD.read_text(encoding="utf-8")
        block = guard_src.split("REQUIRED_MARKERS=(", 1)[1].split(")", 1)[0]
        lines = [line.strip() for line in block.splitlines()]
        markers = [ln.strip('"') for ln in lines if ln.startswith('"')]
        assert len(markers) == 6, f"expected 6 required markers, parsed {markers}"
        template = PR_TEMPLATE.read_text(encoding="utf-8")
        missing = [m for m in markers if m.lower() not in template.lower()]
        assert not missing, f"PR template is missing required marker(s): {missing}"
