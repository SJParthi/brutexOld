"""Red-team tests for the fork-PR attack-surface hardening
(DEC-CI-FORK-PR-HARDEN-001).

BruteX is a PUBLIC repo. Two fork-reachable holes were closed:

* **H1 — spoofable branch-name trust.** ``auto-merge.yml`` classified a
  PR as trusted if its head BRANCH NAME started with ``claude/`` — an
  attacker-chosen string. A stranger's fork named ``claude/anything``
  would have been auto-squash-merged to main once CI went green. The
  rule is REMOVED; trust is now the explicit author allowlist OR a
  LIVE-API ``author_association`` ∈ {OWNER, MEMBER, COLLABORATOR}.
* **H2 — executing PR-controlled code with a base-repo token.**
  ``pre-merge-gate.yml`` (a ``workflow_run`` job with
  ``pull-requests: write``) checked out the PR's head SHA and executed
  ``scripts/ci/pre-merge-check.sh`` FROM THE PR TREE — a fork could
  rewrite that script and run arbitrary code with the job token. The
  checkout is now pinned to the base repo's ``main``; the PR number is
  passed as data only. ``auto-merge.yml`` gained the same base-pinned
  checkout for its rollup-gate script invocation.

The meta-tests parse the workflow YAML (no network). The functional
tests EXECUTE the real trust-check shell script extracted from the
workflow, with a stubbed ``gh`` on PATH, and assert the classifier's
actual verdicts — including the exact attack shape (a non-member fork
author on a ``claude/*``-named branch).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTO_MERGE_YML = REPO_ROOT / ".github" / "workflows" / "auto-merge.yml"
PRE_MERGE_GATE_YML = REPO_ROOT / ".github" / "workflows" / "pre-merge-gate.yml"


def _load_jobs(path: Path) -> dict[str, Any]:
    """Return the ``jobs:`` mapping of a workflow YAML file."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = data["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _trust_check_script() -> str:
    """Extract the real trust-check ``run:`` script from auto-merge.yml."""
    jobs = _load_jobs(AUTO_MERGE_YML)
    steps = jobs["auto-merge"]["steps"]
    step = next(s for s in steps if isinstance(s, dict) and str(s.get("id", "")) == "trust-check")
    run = step.get("run")
    assert isinstance(run, str) and run.strip(), "trust-check has no run script"
    return run


def _run_trust_check(
    tmp_path: Path,
    *,
    author: str,
    head_ref: str,
    association: str,
    is_draft: str = "false",
) -> str:
    """Execute the extracted trust-check script with a stubbed ``gh``.

    Returns the ``trusted=`` value written to ``$GITHUB_OUTPUT``.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    gh_stub = stub_dir / "gh"
    # The script's only gh call is: gh api repos/<repo>/pulls/<n> --jq ...
    # The stub answers with the association under test.
    gh_stub.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "${FAKE_ASSOCIATION}"\n',
        encoding="utf-8",
    )
    gh_stub.chmod(gh_stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    github_output = tmp_path / "github_output"
    github_output.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{stub_dir}:{env.get('PATH', '')}",
            "GITHUB_OUTPUT": str(github_output),
            "GH_TOKEN": "test-token",
            "REPO": "SJParthi/bruteX",
            "PR_NUMBER": "9999",
            "AUTHOR": author,
            "HEAD_REF": head_ref,
            "IS_DRAFT": is_draft,
            "FAKE_ASSOCIATION": association,
        }
    )
    result = subprocess.run(
        ["bash", "-c", _trust_check_script()],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"trust-check script exited {result.returncode}:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    lines = github_output.read_text(encoding="utf-8").splitlines()
    trusted = [ln.split("=", 1)[1] for ln in lines if ln.startswith("trusted=")]
    assert trusted, f"trust-check wrote no trusted= output; stdout: {result.stdout}"
    return trusted[-1]


# ── H1 meta-pins: the spoofable branch-name rule is gone ────────────────


def test_trust_check_has_no_branch_name_trust_rule() -> None:
    """H1: no ``claude/*`` (or any HEAD_REF-based) trust path may exist —
    a branch name is attacker-chosen on a public repo."""
    run = _trust_check_script()
    msg = (
        "auto-merge trust-check trusts branch names starting with claude/ "
        "again — a fork named claude/anything would be auto-merged "
        "(regression of DEC-CI-FORK-PR-HARDEN-001 H1)"
    )
    # The old rule's shapes: an `[[ "${HEAD_REF}" == claude/* ]]` test or a
    # `claude/*)` case arm. Neither may exist (log strings may mention
    # claude/* — only CONDITIONS are forbidden).
    assert "== claude/*" not in run, msg
    assert "claude/*)" not in run, msg
    # HEAD_REF must never appear inside a condition at all.
    for line in run.splitlines():
        stripped = line.strip()
        if stripped.startswith(("if ", "if[", "case ", "elif ")):
            assert "HEAD_REF" not in stripped, (
                f"trust-check branches on HEAD_REF ({stripped!r}) — branch "
                "names are attacker-chosen and must never grant trust"
            )


def test_trust_check_gates_on_author_association() -> None:
    """H1: the general trust rule must be a LIVE author_association lookup
    restricted to OWNER / MEMBER / COLLABORATOR."""
    run = _trust_check_script()
    assert "author_association" in run, (
        "trust-check no longer resolves the PR author_association — the "
        "general trust rule lost its API grounding"
    )
    assert "OWNER|MEMBER|COLLABORATOR" in run, (
        "trust-check no longer restricts trust to OWNER|MEMBER|COLLABORATOR associations"
    )
    for weak in ("FIRST_TIME_CONTRIBUTOR", "CONTRIBUTOR|", "|CONTRIBUTOR", "NONE|"):
        assert weak not in run.replace("OWNER|MEMBER|COLLABORATOR", ""), (
            f"trust-check accepts the weak association pattern {weak!r} — "
            "any past contributor (or nobody) would be trusted"
        )


# ── H1 functional: execute the real classifier ──────────────────────────


def test_classifier_rejects_claude_branch_fork_from_stranger(tmp_path: Path) -> None:
    """The exact attack: a non-member fork author names the branch
    claude/anything. The classifier must say NOT trusted."""
    trusted = _run_trust_check(
        tmp_path,
        author="evil-stranger",
        head_ref="claude/totally-legit-automation",
        association="FIRST_TIME_CONTRIBUTOR",
    )
    assert trusted == "false", (
        "a claude/*-named fork PR from a FIRST_TIME_CONTRIBUTOR was "
        "classified trusted — the spoofable branch-name hole is open again"
    )


def test_classifier_rejects_contributor_and_none(tmp_path: Path) -> None:
    """CONTRIBUTOR (has past merged commits) and NONE must NOT be trusted."""
    for association in ("CONTRIBUTOR", "NONE"):
        trusted = _run_trust_check(
            tmp_path,
            author="drive-by",
            head_ref="claude/spoof",
            association=association,
        )
        assert trusted == "false", (
            f"association {association} was classified trusted — only "
            "OWNER/MEMBER/COLLABORATOR may auto-merge"
        )


def test_classifier_accepts_owner_by_allowlist_without_api(tmp_path: Path) -> None:
    """SJParthi is on the explicit allowlist: trusted even if the API stub
    would answer NONE (the allowlist short-circuits before the API)."""
    trusted = _run_trust_check(
        tmp_path,
        author="SJParthi",
        head_ref="claude/real-automation",
        association="NONE",  # must not even be consulted
    )
    assert trusted == "true", (
        "the explicit author allowlist no longer trusts SJParthi — "
        "owner-authored claude/* automation PRs would strand un-merged"
    )


def test_classifier_accepts_owner_member_collaborator_via_api(tmp_path: Path) -> None:
    """A non-allowlisted author with a strong association is trusted."""
    for association in ("OWNER", "MEMBER", "COLLABORATOR"):
        trusted = _run_trust_check(
            tmp_path,
            author="some-teammate",
            head_ref="feature/x",
            association=association,
        )
        assert trusted == "true", (
            f"association {association} was NOT trusted — legitimate "
            "maintainer PRs would strand un-merged"
        )


def test_classifier_skips_draft_prs(tmp_path: Path) -> None:
    """Draft PRs stay untrusted regardless of author/association."""
    trusted = _run_trust_check(
        tmp_path,
        author="SJParthi",
        head_ref="claude/wip",
        association="OWNER",
        is_draft="true",
    )
    assert trusted == "false", "a draft PR was classified trusted"


# ── H2 meta-pins: no privileged workflow executes PR-controlled code ────


def test_pre_merge_gate_checks_out_base_not_pr_head() -> None:
    """H2: pre-merge-gate.yml must pin its checkout to the base repo's
    main — never the triggering PR's head SHA."""
    jobs = _load_jobs(PRE_MERGE_GATE_YML)
    steps = jobs["invoke-pre-merge-check"]["steps"]
    checkout = next(
        s for s in steps if isinstance(s, dict) and "actions/checkout" in str(s.get("uses", ""))
    )
    ref = str(checkout.get("with", {}).get("ref", ""))
    assert ref == "main", (
        f"pre-merge-gate checkout ref is {ref!r} — it must be the trusted "
        "base 'main', never a PR-controlled SHA "
        "(regression of DEC-CI-FORK-PR-HARDEN-001 H2)"
    )
    assert "head_sha" not in ref, "pre-merge-gate checks out the PR head again"


def test_auto_merge_checks_out_base_for_rollup_script() -> None:
    """H2: auto-merge.yml's rollup-gate runs scripts/ci/pre-merge-check.sh;
    the workflow must carry a base-pinned checkout so that script is the
    trusted copy (and exists at all — the workflow previously had NO
    checkout, so the gate could only fail closed on a missing file)."""
    jobs = _load_jobs(AUTO_MERGE_YML)
    steps = jobs["auto-merge"]["steps"]
    checkouts = [
        s for s in steps if isinstance(s, dict) and "actions/checkout" in str(s.get("uses", ""))
    ]
    assert checkouts, (
        "auto-merge.yml has no checkout step — its rollup-gate cannot find "
        "scripts/ci/pre-merge-check.sh (fail-closed inert auto-merge)"
    )
    for checkout in checkouts:
        ref = str(checkout.get("with", {}).get("ref", ""))
        assert ref == "main", (
            f"auto-merge checkout ref is {ref!r} — must be the trusted base "
            "'main', never a PR-controlled SHA"
        )


def test_no_workflow_run_job_checks_out_pr_head() -> None:
    """H2 sweep: NO workflow_run- or pull_request_target-triggered workflow
    may check out ``workflow_run.head_sha`` / a PR head ref. This is the
    anti-pattern sweep that found pre-merge-gate.yml; it pins the whole
    directory so the pattern cannot come back anywhere."""
    workflows_dir = REPO_ROOT / ".github" / "workflows"
    for path in sorted(workflows_dir.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = data.get(True, data.get("on", {}))  # yaml parses `on:` as True
        if not isinstance(triggers, dict):
            continue
        if "workflow_run" not in triggers and "pull_request_target" not in triggers:
            continue
        jobs = data.get("jobs", {})
        for job_name, job in jobs.items():
            for step in job.get("steps", []) or []:
                if not isinstance(step, dict):
                    continue
                if "actions/checkout" not in str(step.get("uses", "")):
                    continue
                ref = str((step.get("with") or {}).get("ref", ""))
                assert "head_sha" not in ref and "head.sha" not in ref and "head.ref" not in ref, (
                    f"{path.name} job {job_name!r} checks out a PR-controlled "
                    f"ref ({ref!r}) in a privileged trigger context — the "
                    "fork-PR code-execution hole is open again "
                    "(DEC-CI-FORK-PR-HARDEN-001)"
                )
