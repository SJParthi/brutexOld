"""Workflow-contract pins for the #1631 gitleaks diff-bound + proof-bundle fix.

Regression test for **DEC-CI-SECRETS-SCAN-DIFFBOUND-001 (MIGRATION.md #1951)**
and **DEC-CI-PROOFBUNDLE-MKDIR-GATE-001 (MIGRATION.md #1953)** — the two
``.github/workflows/ci.yml`` changes shipped by PR #1631. Following the
verified ``test_bench_baseline_ci_wiring.py`` / ``test_redteam_fork_pr_harden.py``
idiom, these ``yaml.safe_load`` pins assert the shipped behaviour so a revert
of either hunk fails CI (STANDARDS_100PCT.md §1 row 10 — the regression test
that would catch the bug):

* The ``secrets`` (gitleaks) job's *Run gitleaks* step is EVENT-AWARE and
  DIFF-BOUNDED via ``--log-opts``:
    - ``pull_request`` bounds ``origin/<base>..HEAD`` and FAIL-CLOSES (``exit
      1``) when the base ref is absent/unfetched — a required security gate
      must never silently pass;
    - ``merge_group`` bounds the queue ``base..head`` range with the
      ``HEAD^1..HEAD^2`` merge-parent fallback, else FAIL-CLOSES — never scans
      the speculative merge commit's empty patch;
    - ``push`` retains the ``BEFORE_SHA..AFTER_SHA`` diff with the ``-1 HEAD``
      first-push tip-scan fallback;
    - the resolved ``$LOG_OPTS`` is passed to ``gitleaks detect``.
  Without diff-bounding, gitleaks 8.21.2 hardcodes an all-refs full-history
  walk under ``fetch-depth: 0`` and false-flags secrets on sibling branches.

* The ``proof-bundle`` job gates on ``${{ !cancelled() }}`` (not
  ``always()``) and its manifest step ``mkdir -p proof`` before writing —
  so a concurrency-cancelled/partial run cannot spuriously red the gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load(path: Path) -> dict[Any, Any]:
    """yaml.safe_load as a dict."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _step_named(job: dict[str, Any], name: str) -> dict[str, Any]:
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    for step in steps:
        if str(step.get("name", "")) == name:
            return step
    raise AssertionError(f"no step named {name!r} in job")


class TestSecretsScanDiffBound:
    """DEC-CI-SECRETS-SCAN-DIFFBOUND-001 (#1951)."""

    def _gitleaks_step(self) -> dict[str, Any]:
        secrets = _load(CI_YML)["jobs"]["secrets"]
        return _step_named(secrets, "Run gitleaks")

    def test_gitleaks_step_is_event_aware(self) -> None:
        env = self._gitleaks_step()["env"]
        # The event-aware diff-bounding reads exactly these context vars.
        assert env["EVENT_NAME"] == "${{ github.event_name }}"
        assert env["PR_BASE_REF"] == "${{ github.event.pull_request.base.ref }}"
        assert env["BEFORE_SHA"] == "${{ github.event.before }}"
        assert env["AFTER_SHA"] == "${{ github.sha }}"
        assert env["MG_BASE_SHA"] == "${{ github.event.merge_group.base_sha }}"
        assert env["MG_HEAD_SHA"] == "${{ github.event.merge_group.head_sha }}"

    def test_default_log_opts_is_tip_scan_not_all_refs(self) -> None:
        # The default (and push first-push fallback) is the single-tip scan —
        # this is what forecloses the all-refs full-history walk on every path.
        run = self._gitleaks_step()["run"]
        assert 'LOG_OPTS="-1 HEAD"' in run

    def test_pull_request_is_diff_bounded_and_fail_closed(self) -> None:
        run = self._gitleaks_step()["run"]
        assert '[ "$EVENT_NAME" = "pull_request" ]' in run
        # Bounded to the PR's own commits vs its base ref.
        assert 'LOG_OPTS="--no-merges origin/${PR_BASE_REF:-main}..HEAD"' in run
        # Fail-closed when the base ref is missing/unfetched: refuse, not pass.
        assert 'git cat-file -e "origin/${PR_BASE_REF:-main}^{commit}"' in run
        assert "not found — refusing to scan (fail-closed)" in run

    def test_push_is_diff_bounded_with_first_push_fallback(self) -> None:
        run = self._gitleaks_step()["run"]
        assert '[ "$EVENT_NAME" = "push" ]' in run
        assert 'LOG_OPTS="--no-merges ${BEFORE_SHA}..${AFTER_SHA}"' in run
        # An all-zero / unknown 'before' (new branch / force push) falls back
        # to the -1 HEAD tip scan asserted above.
        assert "0000000000000000000000000000000000000000" in run

    def test_merge_group_is_diff_bounded_with_parent_fallback_and_fail_closed(
        self,
    ) -> None:
        run = self._gitleaks_step()["run"]
        assert '[ "$EVENT_NAME" = "merge_group" ]' in run
        # Primary: the queue base..head incoming-commits range.
        assert 'LOG_OPTS="--no-merges ${MG_BASE_SHA}..${MG_HEAD_SHA}"' in run
        # Fallback: derive a non-empty range from the merge commit's parents.
        assert 'LOG_OPTS="--no-merges HEAD^1..HEAD^2"' in run
        # Fail-closed: never scan the speculative merge commit's empty patch.
        assert "refusing to scan a speculative merge commit" in run

    def test_gitleaks_detect_consumes_the_resolved_log_opts(self) -> None:
        run = self._gitleaks_step()["run"]
        assert "gitleaks detect" in run
        assert '--log-opts="$LOG_OPTS"' in run
        assert "--config .gitleaks.toml" in run


class TestProofBundleMkdirGate:
    """DEC-CI-PROOFBUNDLE-MKDIR-GATE-001 (#1953)."""

    def _proof_bundle(self) -> dict[str, Any]:
        return _load(CI_YML)["jobs"]["proof-bundle"]

    def test_proof_bundle_gates_on_not_cancelled(self) -> None:
        # Was `always()` — a cancelled producer leaves no `proof/` and would
        # spuriously red the Proof-bundle check across the wave.
        assert self._proof_bundle()["if"] == "${{ !cancelled() }}"

    def test_manifest_step_makes_proof_dir_before_writing(self) -> None:
        step = _step_named(self._proof_bundle(), "Generate proof manifest")
        run = step["run"]
        # `mkdir -p proof` must precede the `cat > proof/MANIFEST.txt` write.
        assert "mkdir -p proof" in run
        mkdir_idx = run.index("mkdir -p proof")
        cat_idx = run.index("cat > proof/MANIFEST.txt")
        assert mkdir_idx < cat_idx
