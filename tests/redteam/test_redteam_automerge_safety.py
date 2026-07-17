"""Red-team tests for the auto-merge safety wiring (DEC-AUTOMERGE-SAFETY-001).

The 5-agent workspace verification (DEC-WORKSPACE-VERIFY-001, MIGRATION.md
entry #398) found two automation-layer gaps in the merge machinery:

* **G1** — ``.github/workflows/auto-merge.yml`` merged on a green
  ``CI OK`` check alone and NEVER consulted GitHub mergeability, so a
  PR in a ``dirty`` (conflicting) state with green CI would have been
  auto-merged over the conflict (this is exactly what happened to
  PR #302 before it was manually rebased).
* **G3** — the ``conv-commits`` job RAN on every PR but was absent from
  ``ci-ok``'s ``needs:`` list, so a non-conforming commit message could
  not actually block merge despite the job being red.

These meta-tests lock both fixes in place so a future edit cannot
silently regress them. They parse the workflow YAML directly — no
network, no GitHub API — and assert the wiring is present.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
AUTO_MERGE_YML = REPO_ROOT / ".github" / "workflows" / "auto-merge.yml"
CHARTER_MD = REPO_ROOT / "docs" / "MERGE_READINESS_GUARANTEE.md"


def _load_jobs(path: Path) -> dict[str, Any]:
    """Return the ``jobs:`` mapping of a workflow YAML file."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = data["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def test_conv_commits_is_a_required_ci_ok_gate() -> None:
    """G3: ``conv-commits`` must be in ``ci-ok``'s ``needs:`` list."""
    jobs = _load_jobs(CI_YML)
    ci_ok = jobs["ci-ok"]
    assert isinstance(ci_ok, dict)
    needs = ci_ok["needs"]
    assert isinstance(needs, list)
    assert "conv-commits" in needs, (
        "conv-commits dropped from ci-ok.needs — a bad commit message "
        "would no longer block merge (regression of G3)"
    )


def test_conv_commits_result_is_checked_in_the_aggregate_loop() -> None:
    """G3: the aggregate verification loop must evaluate conv-commits."""
    ci_text = CI_YML.read_text(encoding="utf-8")
    # The ci-ok step builds a `for result in ...` list of `needs.X.result`
    # expressions; conv-commits must be one of them, else it sits in
    # needs: but its failure is never inspected.
    msg = "conv-commits result not evaluated in the ci-ok aggregate loop"
    assert "needs.conv-commits.result" in ci_text, msg


def test_auto_merge_has_a_mergeability_gate() -> None:
    """G1: auto-merge must consult GitHub mergeability before merging."""
    jobs = _load_jobs(AUTO_MERGE_YML)
    steps = jobs["auto-merge"]["steps"]
    assert isinstance(steps, list)
    step_ids = {s.get("id") for s in steps if isinstance(s, dict)}
    assert "mergeable-check" in step_ids, (
        "auto-merge lost its mergeable-check step — it would merge over "
        "a conflict / stale base again (regression of G1)"
    )


def test_auto_merge_rejects_dirty_and_behind_states() -> None:
    """G1: the mergeability gate must reject conflict / stale-base states."""
    text = AUTO_MERGE_YML.read_text(encoding="utf-8")
    # The gate rejects the genuinely dangerous mergeable_state values.
    for forbidden in ("dirty", "behind", "blocked"):
        msg = f"auto-merge no longer guards against mergeable_state={forbidden!r}"
        assert forbidden in text, msg
    # And it must require mergeable == true.
    msg = "auto-merge no longer requires GitHub mergeable == true"
    assert 'mergeable" != "true"' in text or 'mergeable}" != "true' in text, msg


def test_merge_step_depends_on_the_mergeability_gate() -> None:
    """G1: the actual squash-merge must be gated on the safe=='true' output."""
    jobs = _load_jobs(AUTO_MERGE_YML)
    steps = jobs["auto-merge"]["steps"]
    merge_step = next(
        s
        for s in steps
        if isinstance(s, dict) and str(s.get("name", "")).startswith("Squash-merge")
    )
    guard = str(merge_step.get("if", ""))
    msg = "the squash-merge step is not gated on the mergeability gate (G1 bypass)"
    assert "mergeable-check.outputs.safe == 'true'" in guard, msg


def test_auto_merge_runs_live_rollup_gate_before_merge() -> None:
    """DEC-MERGE-LIVE-ROLLUP-001: the final pre-merge step must invoke the
    canonical pre-merge-check.sh against the LIVE rollup, and the squash-merge
    must be gated on its green output — so a merge can NEVER fire on a possibly
    stale required-status match while the live rollup carries any red.
    """
    jobs = _load_jobs(AUTO_MERGE_YML)
    steps = jobs["auto-merge"]["steps"]
    step_ids = [str(s.get("id", "")) for s in steps if isinstance(s, dict)]
    assert "rollup-gate" in step_ids, (
        "auto-merge lost its final live-rollup gate (rollup-gate) — the merge "
        "decision would no longer re-check the complete live rollup at merge time"
    )
    rollup_step = next(
        s for s in steps if isinstance(s, dict) and str(s.get("id", "")) == "rollup-gate"
    )
    run = str(rollup_step.get("run", ""))
    assert "pre-merge-check.sh" in run, (
        "the live-rollup gate must invoke the canonical scripts/ci/pre-merge-check.sh "
        "(the single source of truth for no-merge-with-red-CI)"
    )
    # It must NOT silently bypass via OPERATOR_OVERRIDE from this automated path.
    assert "OPERATOR_OVERRIDE" not in run, (
        "the auto-merge live-rollup gate must not set OPERATOR_OVERRIDE — the "
        "automated path can never bypass the no-red guard"
    )
    # And the squash-merge step must depend on the gate's green output.
    merge_step = next(
        s
        for s in steps
        if isinstance(s, dict) and str(s.get("name", "")).startswith("Squash-merge")
    )
    guard = str(merge_step.get("if", ""))
    assert "rollup-gate.outputs.gated == 'true'" in guard, (
        "the squash-merge step is not gated on the live-rollup gate "
        "(DEC-MERGE-LIVE-ROLLUP-001 bypass)"
    )


def test_auto_merge_accepts_push_event_ci_completions() -> None:
    """DEC-AUTOMERGE-RACE-FIX-001: a branch with an open PR often has two
    concurrent CI runs (push + pull_request). The trigger guard must accept
    a ``push``-event CI completion, else a green PR can sit un-merged (the
    PR #304 failure)."""
    jobs = _load_jobs(AUTO_MERGE_YML)
    guard = str(jobs["auto-merge"]["if"])
    assert "workflow_run.event == 'push'" in guard, (
        "auto-merge no longer accepts push-event CI completions — concurrent "
        "CI runs can leave a green PR stranded (regression of the race fix)"
    )


def test_ci_check_resolves_the_pr_current_head() -> None:
    """DEC-AUTOMERGE-RACE-FIX-001: the ci-ok gate must re-resolve the PR's
    CURRENT head (via ``gh pr view ... headRefOid``) instead of trusting the
    triggering event's head_sha, so it checks the commit that will be
    merged even with concurrent/stale CI runs."""
    jobs = _load_jobs(AUTO_MERGE_YML)
    steps = jobs["auto-merge"]["steps"]
    ci_step = next(s for s in steps if isinstance(s, dict) and str(s.get("id", "")) == "ci-check")
    run = str(ci_step.get("run", ""))
    assert "gh pr view" in run and "headRefOid" in run, (
        "ci-check no longer re-resolves the PR's current head — it can gate "
        "on a stale/mismatched SHA (regression of the race fix)"
    )


def test_ci_check_requires_all_green_not_head_1() -> None:
    """DEC-AUTOMERGE-ALLGREEN-001 (operator decree 2026-06-04: "without green it
    should NEVER be merged").

    A PR head can carry TWO ``CI OK (aggregate gate)`` runs — a ``push`` run
    that SKIPS the PR-only gates and a ``pull_request`` run that runs them. The
    OLD ``... | head -1`` picked an arbitrary conclusion and merged on the green
    push run while the pull_request run was RED — exactly how #366 merged past a
    red bug-fix-regression gate. The ci-check must therefore (a) no longer use
    ``head -1``, (b) refuse on ANY failing check conclusion, and (c) require
    EVERY ``CI OK`` instance on the head to be success.
    """
    jobs = _load_jobs(AUTO_MERGE_YML)
    steps = jobs["auto-merge"]["steps"]
    ci_step = next(s for s in steps if isinstance(s, dict) and str(s.get("id", "")) == "ci-check")
    run = str(ci_step.get("run", ""))
    assert "head -1" not in run, (
        "ci-check still uses `head -1` on the CI OK conclusions — a green push "
        "run can mask a red pull_request run (regression of the #366 merge hole)"
    )
    # (a) refuse on ANY failing check conclusion (no green = no merge).
    msg = "ci-check no longer blocks on failing check conclusions (no green)"
    assert "failure" in run and "timed_out" in run, msg
    # (b) require EVERY CI OK (aggregate gate) instance to be success.
    assert "CI OK (aggregate gate)" in run and "grep -qvx success" in run, (
        "ci-check no longer requires EVERY CI OK run to be success — a single "
        "green run could mask a red one"
    )


def test_ci_check_requires_all_ci_runs_complete_with_pr_verdict() -> None:
    """DEC-AUTOMERGE-CLEAN-STRICT-001: the EXISTENCE variant of the #366
    hole. "Every CI OK instance is success" is conclusion-based and
    existence-blind: PR #571 merged while its pull_request-event CI run —
    the one enforcing the PR-only gates — was still in flight, because
    that run's ``CI OK`` check run did not exist yet (auto-merge run
    27232004649; CI runs 27231261106 push / 27231295196 pull_request,
    completed 37s AFTER the merge). ci-check must therefore (a) list the
    CI workflow's runs for the PR's current head, (b) refuse while any
    push/pull_request run is incomplete (self-healing: that run's own
    completion re-triggers the workflow), and (c) require a
    completed+success pull_request-event run to exist."""
    jobs = _load_jobs(AUTO_MERGE_YML)
    steps = jobs["auto-merge"]["steps"]
    ci_step = next(s for s in steps if isinstance(s, dict) and str(s.get("id", "")) == "ci-check")
    run = str(ci_step.get("run", ""))
    assert "actions/workflows/ci.yml/runs?head_sha=" in run, (
        "ci-check no longer lists the CI workflow runs for the head — the "
        "in-flight-CI existence hole (#571 merge-over-pending) is open again"
    )
    msg = "ci-check no longer refuses while a CI run is incomplete on the head"
    assert "still running" in run, msg
    assert "awk" in run and '"pull_request"' in run and '"success"' in run, (
        "ci-check no longer requires a completed+success pull_request-event "
        "CI run — the PR-only gates can be bypassed by a green push run again"
    )


def test_mergeable_null_is_parsed_without_field_shift() -> None:
    """DEC-AUTOMERGE-CLEAN-STRICT-001: an uncomputed ``mergeable: null``
    rendered as an EMPTY first TSV field; ``read -r`` stripped the leading
    tab and FIELD-SHIFTED ``mergeable_state``'s "unknown" into
    ``${mergeable}`` — the poll loop broke on attempt 1, the gate refused
    instantly with a misleading conflict message, and the
    DEC-AUTOMERGE-RACE-FIX-001 null-deferral was dead code (every sampled
    refusal echoed ``mergeable=unknown mergeable_state=``). The jq
    expression must default BOTH fields to the string "null" so the poll
    loop and the deferral behave as designed."""
    jobs = _load_jobs(AUTO_MERGE_YML)
    steps = jobs["auto-merge"]["steps"]
    m_step = next(
        s for s in steps if isinstance(s, dict) and str(s.get("id", "")) == "mergeable-check"
    )
    run = str(m_step.get("run", ""))
    assert '(.mergeable // "null")' in run and '(.mergeable_state // "null")' in run, (
        "mergeable-check lost the null-defaulting jq — an uncomputed "
        "mergeable field-shifts and instantly refuses again (dead deferral)"
    )


def test_workflow_dispatch_path_resolves_pr_metadata() -> None:
    """DEC-AUTOMERGE-CLEAN-STRICT-001: the original ``workflow_dispatch``
    path emitted ONLY ``pr_number``, so trust-check read empty
    AUTHOR/HEAD_REF/IS_DRAFT and always fell through to ``trusted=false``
    — the documented "manual escape hatch" was dead code (zero dispatch
    runs in history). The MANUAL_PR branch must resolve the PR's
    author/headRefName/isDraft so manual runs pass through the SAME
    trust + all-green + mergeability gates."""
    jobs = _load_jobs(AUTO_MERGE_YML)
    steps = jobs["auto-merge"]["steps"]
    find_step = next(s for s in steps if isinstance(s, dict) and str(s.get("id", "")) == "find-pr")
    run = str(find_step.get("run", ""))
    assert 'gh pr view "${MANUAL_PR}"' in run and "author,headRefName,isDraft" in run, (
        "find-pr's workflow_dispatch branch no longer resolves PR metadata — "
        "the manual escape hatch is dead code again (trusted=false on empty "
        "AUTHOR)"
    )


def test_auto_merge_refusal_list_includes_unknown() -> None:
    """DEC-AUTOMERGE-CLEAN-STRICT-001: the full refusal case pattern is
    ``dirty|behind|blocked|unknown`` — the charter §4.5 pre-G2 envelope. A
    silent drop of ``unknown`` (an uncomputed state) would let a merge
    proceed on a mergeability GitHub never finished evaluating."""
    text = AUTO_MERGE_YML.read_text(encoding="utf-8")
    assert "dirty|behind|blocked|unknown)" in text, (
        "auto-merge's mergeable_state refusal case-pattern no longer matches "
        "the charter §4.5 envelope (dirty|behind|blocked|unknown)"
    )


def test_mergeable_state_envelope_is_pinned_workflow_to_charter() -> None:
    """DEC-AUTOMERGE-CLEAN-STRICT-001 (DEC-WORKSPACE-VERIFY-002 follow-up
    F-4): the workflow's pre-G2 ``mergeable_state`` allowance (``clean`` /
    ``unstable`` / ``has_hooks``) and the charter's §4.5 envelope must move
    TOGETHER. The 2026-06-10 verification found the workflow allowing
    ``unstable``/``has_hooks`` while the charter demanded strict ``clean``
    — every sampled auto-merge (PRs #569/#571/#572) had in fact merged
    with ``mergeable_state=unstable``, so the charter was amended to
    document the allowance rather than the gate tightened (which would
    deadlock: the gate's own pending check run keeps the head's rollup
    non-passing). This test fails if EITHER side is edited without the
    other: both must cite the DEC-ID, and the charter must document the
    tolerated states the workflow actually allows."""
    workflow = AUTO_MERGE_YML.read_text(encoding="utf-8")
    charter = CHARTER_MD.read_text(encoding="utf-8")
    assert "DEC-AUTOMERGE-CLEAN-STRICT-001" in workflow, (
        "auto-merge.yml no longer cites DEC-AUTOMERGE-CLEAN-STRICT-001 — the "
        "mergeable_state allowance lost its link to the charter envelope"
    )
    assert "DEC-AUTOMERGE-CLEAN-STRICT-001" in charter, (
        "the charter no longer documents the DEC-AUTOMERGE-CLEAN-STRICT-001 "
        "envelope — the workflow's allowance is undocumented law again "
        "(regression of DEC-WORKSPACE-VERIFY-002 follow-up F-4)"
    )
    for state in ("unstable", "has_hooks"):
        assert state in charter, (
            f"the charter no longer documents the tolerated "
            f"mergeable_state={state!r} — workflow and charter have drifted "
            "apart again"
        )


def _merge_step_run() -> str:
    """Return the Squash-merge step's ``run:`` script from auto-merge.yml."""
    jobs = _load_jobs(AUTO_MERGE_YML)
    steps = jobs["auto-merge"]["steps"]
    merge_step = next(
        s
        for s in steps
        if isinstance(s, dict) and str(s.get("name", "")).startswith("Squash-merge")
    )
    return str(merge_step.get("run", ""))


def _merge_step_executable_commands() -> list[str]:
    """Logical EXECUTABLE commands of the Squash-merge ``run:`` block.

    Shell comment lines (first non-whitespace char ``#``) are stripped FIRST:
    the block's rationale comments quote the very tokens the behavioral pins
    match (``--auto``, ``enablePullRequestAutoMerge``, ``--delete-branch``),
    so asserting against the RAW block text was TAUTOLOGICAL — a mutant that
    deleted BOTH executable ``gh pr merge`` commands while keeping the
    comments passed every assertion, including the ordering one (it compared
    two COMMENT positions). Backslash-continued lines are then folded into
    ONE logical command each, so a flag on a continuation line attributes to
    the command that carries it.
    """
    commands: list[str] = []
    pending: list[str] = []
    for raw_line in _merge_step_run().splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue  # shell comment — never executable
        if not stripped and not pending:
            continue  # blank line between commands
        if stripped.endswith("\\"):
            pending.append(stripped[:-1].strip())
            continue
        pending.append(stripped)
        commands.append(" ".join(pending))
        pending = []
    if pending:  # a dangling continuation — keep it visible rather than drop it
        commands.append(" ".join(pending))
    return commands


class TestMergeQueueReadiness:
    """DEC-MERGE-QUEUE-001 (MIGRATION.md #1784): merge-queue readiness pins.

    Every branch-protection required context is produced by ci.yml, so the
    queue wiring (merge_group trigger + the ci-ok required-context name on
    queue refs) and the queue-aware auto-merge (enqueue-first with the
    pre-queue direct fallback; blocked-tolerance only under a live-detected
    merge_queue rule) are pinned here against silent regression.
    """

    def test_ci_workflow_triggers_on_merge_group(self) -> None:
        """DEC-MERGE-QUEUE-001 (MIGRATION.md #1784): every branch-protection
        required context is produced by ci.yml (21 static job names + the ci-ok
        dynamic name), so ci.yml MUST carry the ``merge_group`` trigger — without
        it no required check can ever report on a merge-queue ref and every
        queued PR deadlocks until the queue times it out."""
        data = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
        # PyYAML parses a bare `on:` key as boolean True.
        triggers = data.get(True, data.get("on", {}))
        assert "merge_group" in triggers, (
            "ci.yml lost its merge_group trigger — required checks can never "
            "report on merge-queue refs, deadlocking every queued PR "
            "(regression of DEC-MERGE-QUEUE-001)"
        )

    def test_ci_ok_mints_required_context_on_merge_group(self) -> None:
        """DEC-MERGE-QUEUE-001: the required context "CI OK (aggregate gate)" is
        the ci-ok job's DYNAMIC name. It must be minted on ``merge_group`` events
        too (the queue evaluates required checks against its own speculative
        merge commit) — while staying withheld from push/dispatch runs, so the
        DEC-CI-OK-PR-ONLY-NAME-001 non-PR shadowing hole stays closed."""
        jobs = _load_jobs(CI_YML)
        name = str(jobs["ci-ok"]["name"])
        assert "'CI OK (aggregate gate)'" in name, (
            "ci-ok no longer mints the required context name at all"
        )
        assert "github.event_name == 'merge_group'" in name, (
            "ci-ok's name expression no longer covers merge_group — the required "
            "aggregate context would never report on a queue ref (queue deadlock; "
            "regression of DEC-MERGE-QUEUE-001)"
        )
        assert "github.event_name == 'pull_request'" in name, (
            "ci-ok's name expression lost the pull_request producer — the "
            "required context would stop reporting on PR heads"
        )
        assert "'CI OK (non-PR aggregate)'" in name, (
            "ci-ok's non-PR fallback name is gone — push/dispatch runs would "
            "mint (and could shadow) the required context again (regression of "
            "DEC-CI-OK-PR-ONLY-NAME-001)"
        )

    def test_merge_step_attempts_enqueue_before_direct_fallback(self) -> None:
        """DEC-MERGE-QUEUE-001: the merge step must FIRST attempt to enable
        auto-merge (``gh pr merge --auto --squash`` = the GraphQL
        enablePullRequestAutoMerge mutation — the enqueue API on a
        merge-queue-ruled base) and keep the pre-queue direct squash-merge as
        the FALLBACK, so the workflow is correct both before AND after the
        merge-queue ruleset flips.

        The behavioral asserts run over the block's EXECUTABLE commands only
        (comments stripped, continuations folded via
        ``_merge_step_executable_commands``) — the block's rationale comments
        quote the same flags, so raw-text matching was a tautology
        (mutant-proven: deleting BOTH ``gh pr merge`` commands while keeping
        the comments passed the old pins, ordering assert included).
        """
        commands = _merge_step_executable_commands()
        enqueue_at = [
            i for i, cmd in enumerate(commands) if "gh pr merge" in cmd and "--auto" in cmd
        ]
        assert enqueue_at, (
            "the merge step lost the executable enable-auto-merge (enqueue) "
            "attempt (`gh pr merge ... --auto`) — with a merge queue ruling "
            "main, direct merges are refused and every green PR strands "
            "(regression of DEC-MERGE-QUEUE-001)"
        )
        # The fallback direct merge (with branch deletion) must still exist as
        # an executable command and come AFTER the enqueue attempt.
        fallback_at = [
            i for i, cmd in enumerate(commands) if "gh pr merge" in cmd and "--delete-branch" in cmd
        ]
        assert fallback_at, (
            "the executable direct squash-merge fallback (`gh pr merge ... "
            "--delete-branch`) is gone — on a queue-less repo (today's world) "
            "nothing would merge at all"
        )
        assert min(enqueue_at) < min(fallback_at), (
            "the enqueue attempt no longer precedes the direct-merge fallback "
            "in the EXECUTABLE command sequence — a queue-ruled base would eat "
            "a doomed direct merge first"
        )
        # DOCUMENTATION-COMMENT pin ONLY — deliberately SEPARATE from the
        # behavioral asserts above (the token lives in a rationale COMMENT of
        # the run block, never in executable code, so this must never
        # masquerade as a behavioral pin): the block keeps naming the GraphQL
        # mutation so the --auto attempt keeps its enqueue-mechanism trail.
        assert "enablePullRequestAutoMerge" in _merge_step_run(), (
            "the merge step no longer documents the enqueue mechanism — the "
            "--auto attempt lost its rationale trail (documentation-comment "
            "pin, not a behavioral one)"
        )

    def test_blocked_state_tolerated_only_under_live_queue_rule(self) -> None:
        """DEC-MERGE-QUEUE-001: ``mergeable_state=blocked`` is the queue-normal
        state of a fully-green PR (a queue refuses direct merges), so the
        mergeable-check may tolerate it — but ONLY when a ``merge_queue`` rule
        is live-detected on main via the rules-for-branch endpoint. Queue-less
        ``blocked`` (and dirty/behind/unknown always) must refuse exactly as
        before (fail-closed on a probe failure)."""
        jobs = _load_jobs(AUTO_MERGE_YML)
        steps = jobs["auto-merge"]["steps"]
        m_step = next(
            s for s in steps if isinstance(s, dict) and str(s.get("id", "")) == "mergeable-check"
        )
        run = str(m_step.get("run", ""))
        # The denylist case pattern itself is untouched (the sibling test
        # test_auto_merge_refusal_list_includes_unknown pins the literal too).
        assert "dirty|behind|blocked|unknown)" in run, (
            "the mergeable_state denylist pattern is gone from mergeable-check"
        )
        # The tolerance is nested INSIDE the blocked arm and gated on the live probe.
        assert '"${state}" == "blocked"' in run, (
            "the queue tolerance is no longer scoped to the blocked state only — "
            "dirty/behind/unknown could ride it (unsafe widening)"
        )
        assert "rules/branches/main" in run and "merge_queue" in run, (
            "the blocked-tolerance lost its live merge_queue rule probe — either "
            "queue-less blocked PRs merge-attempt again (unsafe) or queue-ruled "
            "green PRs strand (regression of DEC-MERGE-QUEUE-001)"
        )

    def test_merge_group_trigger_is_exactly_where_required_contexts_live(self) -> None:
        """DEC-MERGE-QUEUE-001: ``merge_group`` belongs ONLY on workflows that
        produce a branch-protection required context — today exactly ci.yml.
        Adding it elsewhere burns queue-lap minutes for checks the queue never
        waits on; and a required context moving OUT of ci.yml would need its new
        home wired too (this pin forces that conversation)."""
        workflows_dir = REPO_ROOT / ".github" / "workflows"
        with_trigger = set()
        for wf in sorted(workflows_dir.glob("*.yml")):
            data = yaml.safe_load(wf.read_text(encoding="utf-8"))
            triggers = data.get(True, data.get("on", {})) if isinstance(data, dict) else {}
            if isinstance(triggers, dict) and "merge_group" in triggers:
                with_trigger.add(wf.name)
        assert with_trigger == {"ci.yml"}, (
            f"merge_group triggers found on {sorted(with_trigger)!r} — expected "
            "exactly {'ci.yml'} (the one workflow producing every required "
            "context; DEC-MERGE-QUEUE-001)"
        )


def test_strict_clean_ratchet_is_recorded_on_both_surfaces() -> None:
    """DEC-AUTOMERGE-CLEAN-STRICT-001: the pre-G2 allowance is a RATCHET,
    not a license — both the workflow and the charter must keep the
    commitment to tighten the allow-list to ``clean`` only once server-side
    branch protection (G2) lands. Dropping the ratchet from either surface
    silently converts the honest envelope into a permanent loosening."""
    workflow = AUTO_MERGE_YML.read_text(encoding="utf-8")
    charter = CHARTER_MD.read_text(encoding="utf-8")
    for surface, text in (("auto-merge.yml", workflow), ("charter", charter)):
        assert "tighten the allow-list to" in text and "`clean` only" in text, (
            f"the {surface} lost the G2 strict-clean ratchet — the pre-G2 "
            "mergeable_state allowance must stay a tracked ratchet, not "
            "become permanent"
        )
