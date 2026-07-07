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

DEC-CLOUD-AUTOFIX-RETIRE-001 extends the directory sweep to the
comment-family triggers (``issue_comment`` et al.) the original
workflow_run/pull_request_target filter was blind to: no such workflow
may carry write permissions, a non-``GITHUB_TOKEN`` secret, or a
PR-controlled checkout without a #1180-pattern trust gate first — the
retired cloud auto-fix runbook design had exactly that shape.
"""

from __future__ import annotations

import os
import re
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


def _workflow_triggers(workflow: dict[Any, Any]) -> dict[Any, Any]:
    """The parsed ``on:`` value, NORMALIZED to a mapping across GitHub
    Actions' three equivalent spellings — mapping (``on: {issue_comment:
    …}``), flow list (``on: [issue_comment]``), and scalar (``on:
    issue_comment``). Without the normalization a sweep that membership-
    tests a dict fails OPEN for the scalar/list spellings (the round-1
    hostile-review bypass: the retired auto-fix shape rewritten with
    ``on: [issue_comment]`` produced ZERO violations). ONE common helper
    so every directory sweep in this file shares the closure (§11 COMMON).
    yaml parses the bare key ``on:`` as boolean ``True``."""
    triggers = workflow.get(True, workflow.get("on", {}))
    if isinstance(triggers, str):
        return {triggers: {}}
    if isinstance(triggers, list):
        return {trigger: {} for trigger in triggers}
    if isinstance(triggers, dict):
        return triggers
    return {}


#: GitHub Actions loads BOTH workflow filename extensions.
_WORKFLOW_GLOBS = ("*.yml", "*.yaml")

#: 18 workflows exist today (re-counted 2026-07-07); fewer than this floor = vacuous scan.
_MIN_EXPECTED_WORKFLOWS = 10


def _glob_workflow_files(workflows_dir: Path) -> list[Path]:
    """Every GitHub Actions workflow file under ``workflows_dir`` — BOTH
    accepted filename extensions. GitHub loads ``.yml`` AND ``.yaml``
    workflows equally, so a sweep globbing only ``*.yml`` fails OPEN for a
    ``claude-autofix.yaml`` landing — the round-2 hostile-review bypass,
    the file-EXTENSION twin of the round-1 ``on:``-spelling bypass. ONE
    common helper so every directory sweep in this file shares the closure
    (§11 COMMON)."""
    return sorted(path for pattern in _WORKFLOW_GLOBS for path in workflows_dir.glob(pattern))


def _repo_workflow_files() -> list[Path]:
    """The repo's workflow files, LIVENESS-asserted: a tree move (or a glob
    typo) that empties the scan must fail LOUDLY — an empty directory sweep
    would otherwise pass vacuously (the ``test_scan_is_not_vacuous``
    pattern of the sibling ADR-0004 GPU drift pin)."""
    files = _glob_workflow_files(REPO_ROOT / ".github" / "workflows")
    assert len(files) >= _MIN_EXPECTED_WORKFLOWS, (
        f"only {len(files)} workflow file(s) found under .github/workflows — "
        "the redteam directory sweep went vacuous; repoint it at the new layout"
    )
    return files


#: Checkout ``ref:`` spellings that place PR-CONTROLLED code in the tree:
#: the event-payload expressions (``head_sha`` / ``head.sha`` / ``head.ref``)
#: AND the canonical server-side PR ref namespace ``refs/pull/<n>/head`` —
#: the spelling a comment-triggered job actually uses (an ``issue_comment``
#: payload carries NO ``pull_request`` context, only the issue number, so a
#: real auto-fix job checks the PR out via
#: ``refs/pull/${{ github.event.issue.number }}/head``). The
#: event-expression-only set missed it (round-2 reconstruction-review fix:
#: the canonical spelling returned ZERO violations).
_PR_CONTROLLED_REF_MARKERS = ("head_sha", "head.sha", "head.ref", "refs/pull/")


def _is_pr_controlled_ref(ref: str) -> bool:
    """True iff a checkout ``ref:`` value names PR-controlled code. ONE
    common predicate shared by the ``workflow_run``/``pull_request_target``
    sweep and the comment-trigger sweep (§11 COMMON)."""
    return any(marker in ref for marker in _PR_CONTROLLED_REF_MARKERS)


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
    for path in _repo_workflow_files():
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = _workflow_triggers(data)
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
                assert not _is_pr_controlled_ref(ref), (
                    f"{path.name} job {job_name!r} checks out a PR-controlled "
                    f"ref ({ref!r}) in a privileged trigger context — the "
                    "fork-PR code-execution hole is open again "
                    "(DEC-CI-FORK-PR-HARDEN-001)"
                )


# ── Comment-trigger sweep: no privileged issue_comment workflows ─────────
# (DEC-CLOUD-AUTOFIX-RETIRE-001, extending DEC-CI-FORK-PR-HARDEN-001.)
# The sweep above filters ONLY workflow_run/pull_request_target, so a
# comment-triggered workflow — which also runs in BASE-repo context with
# attacker-authored input on a public repo — was structurally invisible
# to it. The retired runbook §3 cloud auto-fix snippet had exactly that
# shape (issue_comment + secrets.ANTHROPIC_API_KEY + contents:write, no
# author-trust gate); these pins make the shape mechanically un-landable
# under ANY of GitHub Actions' three `on:` spellings — mapping, flow
# list, and scalar — via the shared `_workflow_triggers` normalization,
# and under BOTH accepted workflow filename extensions (`.yml`/`.yaml`)
# via the shared `_glob_workflow_files` glob (round-2 hostile-review fix:
# a `.yml`-only glob failed OPEN for a `claude-autofix.yaml` landing).
# "Un-landable" is bounded by the DISCLOSED static-shape residual in
# `_job_has_leading_trust_gate` — the gate exemption matches the gate's
# SHAPE, it never executes the gate script. A job declaring NO
# `permissions:` block is flagged outright (round-3 hostile-review fix:
# the repo-DEFAULT GITHUB_TOKEN it inherits is read/WRITE on an
# un-hardened repo, so `_job_write_scopes`' declared-only inspection
# read [] while the effective token could `gh pr merge` — zero
# violations, fail-OPEN). Secret references are matched in BOTH GitHub
# Actions expression spellings — dot (`secrets.NAME`) AND index/bracket
# (`secrets['NAME']` / `secrets["NAME"]`) — via `_SECRET_REF_RE`
# (round-4 reconstruction-review fix: the dot-only regex failed OPEN for
# a read-permissions comment job exfiltrating a foreign secret via the
# bracket respelling — the secret-access twin of the `on:`-spelling /
# `.yaml`-extension / `write-all` bypasses).

_COMMENT_TRIGGERS = ("issue_comment", "pull_request_review_comment", "discussion_comment")
_PRIVILEGED_WRITE_SCOPES = ("contents", "pull-requests", "issues", "id-token")
# BOTH GitHub Actions expression spellings of secret access: dot
# (`secrets.NAME`) and index/bracket (`secrets['NAME']` / `secrets["NAME"]`,
# whitespace-tolerant) — round-4 reconstruction-review fix. The quote class
# is `+` because `yaml.safe_dump` DOUBLES a single quote inside a
# single-quoted scalar (`secrets[''NAME'']`), which a single-quote match
# would miss on a re-serialized node.
_SECRET_REF_RE = re.compile(r"\bsecrets(?:\.|\[\s*['\"]+)([A-Za-z_][A-Za-z0-9_]*)")

# The retired docs/runbooks/claude_automation.md §3 snippet, VERBATIM (the
# in-memory negative fixture proving the sweep has teeth — kept here so the
# retired shape can never regress silently after the runbook text is gone).
_RETIRED_AUTOFIX_SNIPPET = """\
name: Claude auto-fix
on:
  pull_request: { types: [opened, synchronize] }
  issue_comment: { types: [created] }
permissions: { contents: write, pull-requests: write, issues: write }
jobs:
  claude:
    if: ${{ vars.CLAUDE_AUTOFIX_ENABLED == 'true' }}   # flip the repo VARIABLE to enable
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
        with: { fetch-depth: 0 }
      - uses: anthropics/claude-code-action@v1
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          prompt: "If CI is failing on this PR, diagnose + fix the root cause and push; never weaken a gate."
"""


def _job_foreign_secrets(node: Any) -> list[str]:
    """Secret names referenced anywhere in a job/step mapping — in EITHER
    GitHub Actions expression spelling, dot (``secrets.NAME``) or
    index/bracket (``secrets['NAME']`` / ``secrets["NAME"]``; round-4
    reconstruction-review fix — the dot-only regex was fail-OPEN for the
    bracket respelling) — EXCLUDING the implicit ``secrets.GITHUB_TOKEN``
    in both spellings — the carve-out: every workflow run receives that
    token implicitly and benign comment-triggered jobs (label bots,
    slash-command acks) legitimately pass it at read-level, so forbidding
    it would false-positive every future benign job."""
    names = _SECRET_REF_RE.findall(yaml.safe_dump(node))
    return sorted({name for name in names if name != "GITHUB_TOKEN"})


def _job_write_scopes(workflow: dict[Any, Any], job: dict[str, Any]) -> list[str]:
    """Privileged scopes a job holds at ``write`` level (job-level
    ``permissions:`` overrides workflow-level; ``write-all`` grants all).
    DECLARED permissions only — a job with NO ``permissions:`` block at
    either level returns ``[]`` here yet runs on the repo-DEFAULT
    ``GITHUB_TOKEN``; that statically-unboundable case is handled
    separately via ``_job_declares_permissions`` (round-3
    reconstruction-review fix)."""
    perms = job.get("permissions", workflow.get("permissions", {}))
    if perms == "write-all":
        return list(_PRIVILEGED_WRITE_SCOPES)
    if not isinstance(perms, dict):
        return []
    return [s for s in _PRIVILEGED_WRITE_SCOPES if str(perms.get(s, "")) == "write"]


def _job_declares_permissions(workflow: dict[Any, Any], job: dict[str, Any]) -> bool:
    """True iff the job's ``GITHUB_TOKEN`` scope is EXPLICITLY declared —
    a ``permissions:`` key present at job or workflow level (any value:
    a mapping, ``read-all``, even an empty ``{}`` disabling every scope).
    A job with NEITHER inherits the repository-DEFAULT token, which on an
    un-hardened repo is read/WRITE — ``_job_write_scopes``'s declared-
    permissions inspection reads ``[]`` for it while its EFFECTIVE token
    can ``gh pr merge`` with no checkout and no foreign secret (round-3
    reconstruction-review fix: an ``issue_comment`` job with no
    ``permissions:`` block and an implicit-token ``gh pr merge`` step
    produced ZERO violations — the sweep's write-permission guarantee was
    silently DECLARED-permissions-only, depending on the still-open #1180
    owner task of setting the repo default token read-only)."""
    return "permissions" in job or "permissions" in workflow


def _job_has_leading_trust_gate(job: dict[str, Any], *, gate_every_step: bool = False) -> bool:
    """True iff a DEC-CI-FORK-PR-HARDEN-001-pattern trust-check step (a
    live ``author_association`` lookup restricted to
    OWNER|MEMBER|COLLABORATOR) appears BEFORE any privileged step (a
    checkout, or a step referencing a non-``GITHUB_TOKEN`` secret) AND
    every privileged step is conditioned on the gate's VERDICT — an
    ``if:`` referencing ``steps.<gate id>.outputs.`` (the real #1180
    shape: ``if: steps.trust-check.outputs.trusted == 'true'``). The
    substring match ALONE was spoofable (round-1 reconstruction-review
    fix): a no-op ``run: echo "author_association
    OWNER|MEMBER|COLLABORATOR"`` first step exempted the WHOLE job from
    all three checks fail-open, so the gate must now carry an ``id:``
    (an id-less verdict is unreferencable) and every subsequent
    privileged step must reference it. With ``gate_every_step=True``
    (the caller passes it for a job holding a privileged WRITE scope)
    EVERY post-gate step — not merely the checkout/foreign-secret ones —
    must carry the ``if:`` condition, because a write-scope job can act
    through the implicit ``GITHUB_TOKEN`` alone (``gh pr merge``),
    invisible to the privileged-step classifier (round-2
    reconstruction-review fix: an id-carrying gate-SHAPED ``echo``
    exempted a ``contents: write`` job whose UNCONDITIONED step merged
    via the implicit token — zero violations). DISCLOSED residual (the
    sibling GPU-pin convention): the gate script's CONTENT is still
    matched by substring, never executed — a gate-SHAPED script whose
    every gated step IS ``if:``-conditioned on its output is accepted on
    shape (at runtime a verdict-less shaped gate leaves the condition
    falsy so the gated steps cannot run; a shaped script that ALSO
    writes a ``trusted=true`` output is the residual static
    shape-matching cannot close); the teeth for the real script's
    verdict live in the ``_run_trust_check`` functional tests above."""
    gate_ref: str | None = None
    for step in job.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        run = str(step.get("run", ""))
        if gate_ref is None and "author_association" in run and "OWNER|MEMBER|COLLABORATOR" in run:
            step_id = str(step.get("id", "")).strip()
            if not step_id:
                return False
            gate_ref = f"steps.{step_id}.outputs."
            continue
        privileged = (
            gate_every_step
            or "actions/checkout" in str(step.get("uses", ""))
            or bool(_job_foreign_secrets(step))
        )
        if not privileged:
            continue
        if gate_ref is None or gate_ref not in str(step.get("if", "")):
            return False
    return gate_ref is not None


def _comment_trigger_violations(name: str, workflow: dict[Any, Any]) -> list[str]:
    """Privilege violations for ONE parsed workflow, or ``[]`` when it has
    no comment-family trigger. A comment-triggered job may not (a) hold
    write permissions, (b) reference a secret other than the implicit
    ``secrets.GITHUB_TOKEN``, or (c) check out a PR-controlled ref (the
    ``_is_pr_controlled_ref`` markers — the event-payload expressions AND
    the canonical ``refs/pull/<n>/head`` namespace an ``issue_comment``
    job actually uses; round-2 reconstruction-review fix, teeth-pinned by
    ``test_issue_comment_sweep_flags_pr_controlled_checkout`` — check (c)
    was previously an untested dead branch blind to that spelling) —
    unless an id-carrying #1180-pattern trust-check step precedes the
    privileged steps AND every one of them is ``if:``-conditioned on the
    gate's ``steps.<id>.outputs.`` verdict; for a job holding a WRITE
    scope that means EVERY post-gate step (the ledgered
    revival-precondition shape; ``_job_has_leading_trust_gate`` — the
    bare substring exemption was spoofable, round-1 reconstruction-review
    fix; the write-scope every-step rule is the round-2 fix). Check (d)
    (round-3 reconstruction-review fix): a job declaring NO
    ``permissions:`` block at either level is flagged outright — it runs
    on the repo-DEFAULT ``GITHUB_TOKEN`` (read/WRITE on an un-hardened
    repo), so its effective scope is statically unboundable and the
    write-permission guarantee would otherwise silently depend on the
    still-open #1180 owner default-token setting; it is held to the same
    write-scope every-step trust-gate bar. The ``on:`` value is
    normalized via ``_workflow_triggers`` so the scalar/list trigger
    spellings can never bypass the pin (round-1 hostile-review fix)."""
    triggers = _workflow_triggers(workflow)
    if not any(trigger in triggers for trigger in _COMMENT_TRIGGERS):
        return []
    violations: list[str] = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        write_scopes = _job_write_scopes(workflow, job)
        default_token = not _job_declares_permissions(workflow, job)
        gate_every_step = bool(write_scopes) or default_token
        if _job_has_leading_trust_gate(job, gate_every_step=gate_every_step):
            continue
        if default_token:
            violations.append(
                f"{name} job {job_name!r} declares no permissions: block on a "
                "comment trigger — it runs on the repo-DEFAULT GITHUB_TOKEN "
                "(read/WRITE on an un-hardened repo), statically unboundable"
            )
        for scope in write_scopes:
            violations.append(
                f"{name} job {job_name!r} holds '{scope}: write' on a comment trigger"
            )
        for secret in _job_foreign_secrets(job):
            violations.append(
                f"{name} job {job_name!r} references secrets.{secret} on a comment trigger"
            )
        for step in job.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            if "actions/checkout" not in str(step.get("uses", "")):
                continue
            ref = str((step.get("with") or {}).get("ref", ""))
            if _is_pr_controlled_ref(ref):
                violations.append(
                    f"{name} job {job_name!r} checks out PR-controlled ref "
                    f"{ref!r} on a comment trigger"
                )
    return violations


def test_no_issue_comment_privileged_workflows() -> None:
    """DEC-CLOUD-AUTOFIX-RETIRE-001 sweep: NO comment-family-triggered
    workflow (``issue_comment`` / ``pull_request_review_comment`` /
    ``discussion_comment`` — all run in base-repo context on
    attacker-authored input) may combine that trigger with write
    permissions, a non-``GITHUB_TOKEN`` secret, a PR-controlled
    checkout, or an UNDECLARED ``permissions:`` block (the repo-default
    token — round-3 fix), unless an id-carrying
    DEC-CI-FORK-PR-HARDEN-001-pattern trust check precedes the
    privileged steps and every one of them is ``if:``-conditioned on its
    verdict (EVERY post-gate step, for a job holding a write scope or
    running on the default token). The retired cloud auto-fix design
    would have slipped past the workflow_run/pull_request_target-only
    sweep above; this pin closes that blind spot directory-wide."""
    violations: list[str] = []
    for path in _repo_workflow_files():
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        violations.extend(_comment_trigger_violations(path.name, data))
    assert not violations, (
        "privileged comment-triggered workflow(s) found — the class "
        "DEC-CLOUD-AUTOFIX-RETIRE-001 retired is back:\n" + "\n".join(violations)
    )


def test_issue_comment_sweep_flags_retired_autofix_snippet() -> None:
    """Teeth: the EXACT retired runbook §3 snippet (issue_comment trigger +
    ANTHROPIC_API_KEY secret + contents/pull-requests/issues write, no
    trust gate — DEC-CLOUD-AUTOFIX-RETIRE-001) must be flagged, proving
    the sweep would catch the shape if it ever landed as a workflow."""
    data = yaml.safe_load(_RETIRED_AUTOFIX_SNIPPET)
    violations = _comment_trigger_violations("claude-autofix.yml", data)
    joined = "\n".join(violations)
    assert violations, (
        "the retired autofix snippet produced NO violations — the "
        "comment-trigger sweep has no teeth"
    )
    for expected in (
        "'contents: write'",
        "'pull-requests: write'",
        "'issues: write'",
        "secrets.ANTHROPIC_API_KEY",
    ):
        assert expected in joined, (
            f"the retired autofix snippet's {expected} was not flagged; violations were:\n{joined}"
        )


def test_issue_comment_sweep_flags_scalar_and_list_on_spellings() -> None:
    """Round-1 hostile-review regression pin: GitHub Actions equally accepts
    ``on: issue_comment`` (scalar) and ``on: [issue_comment]`` (flow list)
    alongside the mapping form — before ``_workflow_triggers`` the sweep
    returned ZERO violations for either spelling (fail-OPEN: the retired
    auto-fix shape could land by changing the trigger spelling alone).
    All three spellings must now be flagged; a non-comment scalar trigger
    stays clean."""
    for on_spelling in ("on: issue_comment", "on: [issue_comment]"):
        workflow = yaml.safe_load(
            f"""
name: respelled autofix
{on_spelling}
permissions: {{ contents: write, pull-requests: write, issues: write }}
jobs:
  claude:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: echo autofix
        env: {{ ANTHROPIC_API_KEY: "${{{{ secrets.ANTHROPIC_API_KEY }}}}" }}
"""
        )
        violations = _comment_trigger_violations("respelled.yml", workflow)
        joined = "\n".join(violations)
        assert violations, (
            f"{on_spelling!r} produced NO violations — the comment-trigger "
            "sweep is fail-open for that `on:` spelling again"
        )
        for expected in ("'contents: write'", "secrets.ANTHROPIC_API_KEY"):
            assert expected in joined, (
                f"{on_spelling!r}: {expected} was not flagged; violations were:\n{joined}"
            )
    benign_scalar = yaml.safe_load(
        """
name: plain push build
on: push
permissions: { contents: write }
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
"""
    )
    assert _comment_trigger_violations("push.yml", benign_scalar) == [], (
        "a scalar NON-comment trigger (`on: push`) was flagged — the normalization over-reaches"
    )


def test_issue_comment_sweep_carve_outs() -> None:
    """The two deliberate non-violations: (a) the ``secrets.GITHUB_TOKEN``
    carve-out — a read-only comment-triggered job using ONLY the implicit
    token is benign; (b) the #1180 trust-gate exemption — a job whose
    FIRST step is the REAL auto-merge.yml trust-check script carrying an
    ``id:``, with EVERY subsequent privileged step ``if:``-conditioned on
    its ``steps.trust-check.outputs.`` verdict (the ledgered
    revival-precondition shape) is exempt."""
    benign = yaml.safe_load(
        """
name: comment ack
on:
  issue_comment: { types: [created] }
permissions: { contents: read }
jobs:
  ack:
    runs-on: ubuntu-latest
    steps:
      - run: gh api repos/$REPO/issues/$NUM/comments --silent
        env: { GH_TOKEN: "${{ secrets.GITHUB_TOKEN }}" }
"""
    )
    assert _comment_trigger_violations("benign.yml", benign) == [], (
        "the implicit secrets.GITHUB_TOKEN carve-out regressed — a benign "
        "read-only comment-triggered job is being flagged"
    )

    gated = yaml.safe_load(_RETIRED_AUTOFIX_SNIPPET)
    steps = gated["jobs"]["claude"]["steps"]
    steps.insert(0, {"id": "trust-check", "run": _trust_check_script()})
    for step in steps[1:]:
        step["if"] = "steps.trust-check.outputs.trusted == 'true'"
    assert _comment_trigger_violations("gated.yml", gated) == [], (
        "a job trust-gated by the real #1180 trust-check script before any "
        "privileged step, every privileged step conditioned on its verdict, "
        "is still flagged — the revival-precondition shape would be "
        "un-landable"
    )


def test_issue_comment_sweep_flags_spoofed_trust_gate() -> None:
    """Round-1 reconstruction-review regression pin: the trust-gate
    exemption was a bare substring heuristic — ANY early ``run:`` step
    merely CONTAINING ``author_association`` + ``OWNER|MEMBER|COLLABORATOR``
    (e.g. a no-op ``echo``) exempted the WHOLE job from all three checks,
    so the full retired auto-fix shape behind a spoofed gate produced ZERO
    violations (fail-OPEN, and undisclosed). The exemption now requires
    the gate step to carry an ``id:`` AND every subsequent privileged step
    to carry an ``if:`` referencing ``steps.<id>.outputs.``; a gate-shaped
    no-op echo, an id-less REAL gate, and a gated job with even ONE
    unconditioned privileged step must all be flagged."""
    spoofed = yaml.safe_load(_RETIRED_AUTOFIX_SNIPPET)
    spoofed["jobs"]["claude"]["steps"].insert(
        0, {"run": 'echo "author_association OWNER|MEMBER|COLLABORATOR"'}
    )
    violations = _comment_trigger_violations("spoofed.yml", spoofed)
    joined = "\n".join(violations)
    assert violations, (
        "a no-op echo containing the gate substrings exempted the whole "
        "job — the trust-gate exemption is spoofable (fail-open) again"
    )
    for expected in (
        "'contents: write'",
        "'pull-requests: write'",
        "'issues: write'",
        "secrets.ANTHROPIC_API_KEY",
    ):
        assert expected in joined, (
            f"spoofed gate: {expected} was not flagged; violations were:\n{joined}"
        )

    idless = yaml.safe_load(_RETIRED_AUTOFIX_SNIPPET)
    idless_steps = idless["jobs"]["claude"]["steps"]
    idless_steps.insert(0, {"run": _trust_check_script()})  # REAL script, no id:
    for step in idless_steps[1:]:
        step["if"] = "steps.trust-check.outputs.trusted == 'true'"
    assert _comment_trigger_violations("idless.yml", idless), (
        "an id-less gate step (verdict unreferencable — nothing can truly "
        "be conditioned on it) still exempted the job"
    )

    half_gated = yaml.safe_load(_RETIRED_AUTOFIX_SNIPPET)
    half_steps = half_gated["jobs"]["claude"]["steps"]
    half_steps.insert(0, {"id": "trust-check", "run": _trust_check_script()})
    half_steps[1]["if"] = "steps.trust-check.outputs.trusted == 'true'"
    # …the checkout is gated, but the secret-bearing action step is not.
    assert _comment_trigger_violations("half-gated.yml", half_gated), (
        "a job with ONE unconditioned privileged step after the gate was "
        "exempted — the per-step conditioning requirement regressed"
    )


def test_issue_comment_sweep_flags_pr_controlled_checkout() -> None:
    """Round-2 reconstruction-review regression pin: check (c) — the
    PR-controlled-checkout arm of the comment-trigger sweep — was a DEAD
    branch (no test drove it) AND blind to the CANONICAL ``issue_comment``
    PR-checkout spelling ``refs/pull/<n>/head`` (a comment payload carries
    no ``pull_request`` event context, only the issue number, so a real
    comment-triggered job checks the PR out via the server-side ref
    namespace — the probe returned ZERO violations). Both the refs/pull/
    namespace and the event-expression spellings must be flagged; a
    base-pinned ``ref: main`` checkout stays clean (read-level
    permissions, no foreign secrets — isolating check (c))."""
    for ref in (
        "refs/pull/${{ github.event.issue.number }}/head",
        "${{ github.event.pull_request.head.sha }}",
    ):
        workflow = yaml.safe_load(
            f"""
name: comment checkout
on:
  issue_comment: {{ types: [created] }}
permissions: {{ contents: read }}
jobs:
  probe:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
        with: {{ ref: "{ref}" }}
"""
        )
        violations = _comment_trigger_violations("prref.yml", workflow)
        joined = "\n".join(violations)
        assert violations and "checks out PR-controlled ref" in joined, (
            f"a comment-triggered checkout of {ref!r} produced no "
            f"PR-controlled-ref violation — check (c) is fail-open again; "
            f"violations were:\n{joined}"
        )
    pinned = yaml.safe_load(
        """
name: comment checkout pinned
on:
  issue_comment: { types: [created] }
permissions: { contents: read }
jobs:
  probe:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
        with: { ref: main }
"""
    )
    assert _comment_trigger_violations("pinned.yml", pinned) == [], (
        "a base-pinned `ref: main` comment-triggered checkout was flagged — check (c) over-reaches"
    )


def test_issue_comment_sweep_flags_shaped_gate_implicit_token_merge() -> None:
    """Round-2 reconstruction-review regression pin: an id-carrying
    gate-SHAPED no-op ``echo`` (the two substrings, never a real verdict)
    exempted a ``contents: write``/``pull-requests: write`` job whose
    UNCONDITIONED step merges via the IMPLICIT ``GITHUB_TOKEN`` — no
    checkout, no foreign secret, so the privileged-step classifier never
    saw it and the probe produced ZERO violations. A write-scope job is
    now exempt ONLY when EVERY post-gate step is ``if:``-conditioned on
    the gate verdict; the unconditioned implicit-token merge must be
    flagged via the job's write scopes."""
    shaped = yaml.safe_load(
        """
name: shaped gate merge
on:
  issue_comment: { types: [created] }
permissions: { contents: write, pull-requests: write }
jobs:
  merge:
    runs-on: ubuntu-latest
    steps:
      - id: gate
        run: echo "author_association OWNER|MEMBER|COLLABORATOR"
      - run: gh pr merge "$PR" --squash
        env: { GH_TOKEN: "${{ secrets.GITHUB_TOKEN }}" }
"""
    )
    violations = _comment_trigger_violations("shaped.yml", shaped)
    joined = "\n".join(violations)
    assert violations, (
        "an id-carrying gate-shaped echo exempted a write-scope job whose "
        "unconditioned step merges via the implicit GITHUB_TOKEN — the "
        "write-scope exemption is fail-open again"
    )
    for expected in ("'contents: write'", "'pull-requests: write'"):
        assert expected in joined, (
            f"shaped-gate implicit-token merge: {expected} was not flagged; "
            f"violations were:\n{joined}"
        )


_UNDECLARED_PERMISSIONS_MERGE = """\
name: default token merge
on:
  issue_comment: { types: [created] }
jobs:
  merge:
    runs-on: ubuntu-latest
    steps:
      - run: gh pr merge "$PR" --squash
        env: { GH_TOKEN: "${{ secrets.GITHUB_TOKEN }}" }
"""


def test_issue_comment_sweep_flags_undeclared_permissions_default_token() -> None:
    """Round-3 reconstruction-review regression pin: ``_job_write_scopes``
    inspects only the DECLARED ``permissions:`` mapping, so a comment-
    triggered job declaring NO ``permissions:`` block at all — which
    inherits the repository-DEFAULT ``GITHUB_TOKEN``, read/WRITE on an
    un-hardened repo — could ``gh pr merge`` via the implicit token with
    no checkout and no foreign secret: ZERO violations (fail-OPEN, and
    undisclosed — the sweep's write-permission guarantee silently
    depended on the operator having set the repo default token
    read-only, itself a still-open #1180 owner task). An undeclared-
    permissions comment job is now flagged outright (its effective token
    scope is statically unboundable) and held to the write-scope
    every-post-gate-step trust-gate bar; an explicit read-level
    declaration stays clean (isolating the rule from the write-scope
    check), and a fully trust-gated default-token job stays exempt (the
    ledgered revival-precondition shape)."""
    undeclared = yaml.safe_load(_UNDECLARED_PERMISSIONS_MERGE)
    violations = _comment_trigger_violations("default-token.yml", undeclared)
    joined = "\n".join(violations)
    assert violations and "declares no permissions: block" in joined, (
        "a comment-triggered job with NO permissions: block (repo-default "
        "GITHUB_TOKEN — potentially write) produced no violation — the "
        f"declared-permissions blind spot is open again; violations were:\n{joined}"
    )

    declared_read = yaml.safe_load(_UNDECLARED_PERMISSIONS_MERGE)
    declared_read["permissions"] = {"contents": "read"}
    assert _comment_trigger_violations("declared-read.yml", declared_read) == [], (
        "an explicit read-level permissions: declaration was flagged — the "
        "undeclared-permissions rule over-reaches"
    )

    gated = yaml.safe_load(_UNDECLARED_PERMISSIONS_MERGE)
    gated_steps = gated["jobs"]["merge"]["steps"]
    gated_steps.insert(0, {"id": "trust-check", "run": _trust_check_script()})
    for step in gated_steps[1:]:
        step["if"] = "steps.trust-check.outputs.trusted == 'true'"
    assert _comment_trigger_violations("gated-default.yml", gated) == [], (
        "an undeclared-permissions job fully trust-gated (id-carrying real "
        "gate, EVERY post-gate step conditioned on its verdict) is still "
        "flagged — the revival-precondition exemption regressed for the "
        "default-token case"
    )

    half_gated = yaml.safe_load(_UNDECLARED_PERMISSIONS_MERGE)
    half_steps = half_gated["jobs"]["merge"]["steps"]
    half_steps.insert(0, {"id": "trust-check", "run": _trust_check_script()})
    # …the merge step is NOT conditioned on the gate verdict.
    assert _comment_trigger_violations("half-gated-default.yml", half_gated), (
        "an undeclared-permissions job whose post-gate step is UNCONDITIONED "
        "was exempted — the default-token case escapes the write-scope "
        "every-step rule"
    )


def test_workflow_sweep_covers_yaml_extension(tmp_path: Path) -> None:
    """Round-2 hostile-review regression pin: GitHub Actions accepts BOTH
    ``.yml`` and ``.yaml`` workflow filenames — before ``_glob_workflow_files``
    both directory sweeps globbed only ``*.yml``, so the retired auto-fix
    shape landed as ``claude-autofix.yaml`` produced ZERO violations
    (fail-OPEN: the file-extension twin of the ``on:``-spelling bypass).
    The shared glob must return both extensions and nothing else, and the
    ``.yaml``-borne retired shape must be flagged end-to-end."""
    (tmp_path / "benign.yml").write_text("name: benign\non: push\njobs: {}\n", encoding="utf-8")
    yaml_path = tmp_path / "claude-autofix.yaml"
    yaml_path.write_text(_RETIRED_AUTOFIX_SNIPPET, encoding="utf-8")
    (tmp_path / "README.md").write_text("not a workflow\n", encoding="utf-8")
    names = [path.name for path in _glob_workflow_files(tmp_path)]
    assert names == ["benign.yml", "claude-autofix.yaml"], (
        "the workflow glob no longer covers both GitHub Actions filename "
        f"extensions (scanned {names}) — a .yaml workflow bypasses every "
        "directory sweep in this file again"
    )
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert _comment_trigger_violations(yaml_path.name, data), (
        "the retired auto-fix shape in a .yaml file produced NO violations — "
        "the file-extension bypass is open again"
    )


def test_issue_comment_sweep_flags_write_all_permissions() -> None:
    """Round-2 hostile-review regression pin: GitHub Actions accepts
    ``permissions: write-all`` — at the WORKFLOW level and at the JOB
    level — as the grant-every-scope-write spelling. A regression in the
    ``write-all`` sentinel branch of ``_job_write_scopes`` would fail OPEN
    (zero violations) for a respelled auto-fix — the permissions twin of
    the ``on:``-spelling bypass. Both levels must flag ALL four privileged
    scopes. The benign non-mapping spellings stay clean: ``permissions:
    read-all`` grants no write, and an EMPTY ``on:`` has no comment
    trigger (the ``_workflow_triggers`` non-str/list/dict fallback)."""
    workflow_level = yaml.safe_load(
        """
name: respelled autofix
on: [issue_comment]
permissions: write-all
jobs:
  claude:
    runs-on: ubuntu-latest
    steps:
      - run: echo autofix
"""
    )
    job_level = yaml.safe_load(
        """
name: respelled autofix
on:
  issue_comment: { types: [created] }
jobs:
  claude:
    runs-on: ubuntu-latest
    permissions: write-all
    steps:
      - run: echo autofix
"""
    )
    for level, workflow in (("workflow-level", workflow_level), ("job-level", job_level)):
        violations = _comment_trigger_violations("write-all.yml", workflow)
        joined = "\n".join(violations)
        assert violations, (
            f"{level} 'permissions: write-all' produced NO violations — the "
            "comment-trigger sweep is fail-open for the write-all spelling"
        )
        for scope in _PRIVILEGED_WRITE_SCOPES:
            assert f"'{scope}: write'" in joined, (
                f"{level} write-all: scope {scope!r} was not flagged; violations were:\n{joined}"
            )
    read_all = yaml.safe_load(
        """
name: read only
on: [issue_comment]
permissions: read-all
jobs:
  ack:
    runs-on: ubuntu-latest
    steps:
      - run: echo ok
"""
    )
    assert _comment_trigger_violations("read-all.yml", read_all) == [], (
        "'permissions: read-all' (no write anywhere) was flagged — the "
        "non-mapping permissions fallback over-reaches"
    )
    empty_on = yaml.safe_load("name: no triggers\non:\njobs: {}\n")
    assert _comment_trigger_violations("empty-on.yml", empty_on) == [], (
        "a workflow with an EMPTY `on:` was flagged — the trigger "
        "normalization fallback over-reaches"
    )


def test_issue_comment_sweep_flags_bracket_notation_secret_reference() -> None:
    """Round-4 reconstruction-review regression pin: ``_SECRET_REF_RE``
    matched ONLY dot-notation secret access (``secrets.NAME``), while
    GitHub Actions expression syntax equally accepts index/bracket
    notation (``secrets['NAME']`` / ``secrets["NAME"]``) — a
    comment-triggered job declaring read-only ``permissions:`` (so checks
    (a) write-scopes and (d) undeclared-permissions both stand down) but
    exfiltrating a foreign secret via the bracket spelling produced ZERO
    violations (fail-OPEN: the secret-access twin of the ``on:``-spelling
    / ``.yaml``-extension / ``write-all`` bypasses this file already
    closed). Both quote styles must be flagged; the implicit-token
    carve-out must equally cover the bracket spelling of
    ``secrets['GITHUB_TOKEN']`` (a benign read-only comment job passing
    the implicit token by bracket stays clean)."""
    for spelling in ("secrets['ANTHROPIC_API_KEY']", 'secrets["ANTHROPIC_API_KEY"]'):
        workflow = yaml.safe_load(
            f"""
name: bracket exfil
on:
  issue_comment: {{ types: [created] }}
permissions: {{ contents: read }}
jobs:
  exfil:
    runs-on: ubuntu-latest
    steps:
      - run: echo probe
        env:
          KEY: ${{{{ {spelling} }}}}
"""
        )
        violations = _comment_trigger_violations("bracket.yml", workflow)
        joined = "\n".join(violations)
        assert violations and "secrets.ANTHROPIC_API_KEY" in joined, (
            f"a read-permissions comment-triggered job referencing {spelling} "
            "produced no foreign-secret violation — the bracket-notation "
            f"secret-access bypass is open again; violations were:\n{joined}"
        )
    benign = yaml.safe_load(
        """
name: bracket token ack
on:
  issue_comment: { types: [created] }
permissions: { contents: read }
jobs:
  ack:
    runs-on: ubuntu-latest
    steps:
      - run: gh api repos/$REPO/issues/$NUM/comments --silent
        env: { GH_TOKEN: "${{ secrets['GITHUB_TOKEN'] }}" }
"""
    )
    assert _comment_trigger_violations("bracket-token.yml", benign) == [], (
        "the implicit secrets.GITHUB_TOKEN carve-out does not cover the "
        "bracket spelling — a benign read-only comment-triggered job is "
        "being flagged"
    )
