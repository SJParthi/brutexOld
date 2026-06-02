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
    assert "needs.conv-commits.result" in ci_text, (
        "conv-commits result not evaluated in the ci-ok aggregate loop"
    )


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
        assert forbidden in text, (
            f"auto-merge no longer guards against mergeable_state={forbidden!r}"
        )
    # And it must require mergeable == true.
    assert 'mergeable" != "true"' in text or 'mergeable}" != "true' in text, (
        "auto-merge no longer requires GitHub mergeable == true"
    )


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
    assert "mergeable-check.outputs.safe == 'true'" in guard, (
        "the squash-merge step is not gated on the mergeability check — G1 fix is bypassable"
    )
