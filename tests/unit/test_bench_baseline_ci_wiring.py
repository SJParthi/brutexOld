"""Workflow-contract pins for the absolute-baseline gate wiring.

DEC-BENCH-ABS-BASELINE-001. yaml.safe_load pins (the verified
``test_redteam_fork_pr_harden.py`` idiom) over ``.github/workflows/ci.yml``
and ``bench-m4pro.yml``: the required ``bench`` job carries both stdlib gate
steps; the self-hosted runner NEVER holds a contents/PR-write token — its
inherited workflow-level token is ``issues: write`` only, for its own alert
steps (only the GitHub-hosted ``bless-baseline`` job holds contents +
pull-requests write, main-ref + event pinned); the
capture step can only arm on abstain/dispatch — never on a regression; and
the alert steps fire on ``cancelled()`` (a timeout-kill is never silent).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
BENCH_YML = REPO_ROOT / ".github" / "workflows" / "bench-m4pro.yml"


def _load(path: Path) -> dict[Any, Any]:
    """yaml.safe_load as a dict (NOTE: yaml parses a bare ``on:`` key as True)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    return steps


def _step_named(job: dict[str, Any], fragment: str) -> dict[str, Any]:
    for step in _steps(job):
        if fragment in str(step.get("name", "")):
            return step
    raise AssertionError(f"no step named like {fragment!r}")


class TestCiYmlWiring:
    def test_bench_job_runs_validate_on_every_run(self) -> None:
        bench = _load(CI_YML)["jobs"]["bench"]
        step = _step_named(bench, "Bench-baseline structural gate")
        assert "bench_baseline_gate.py validate" in step["run"]
        assert "if" not in step  # unconditional — the always-on structural half

    def test_bench_job_governance_step_is_pr_gated(self) -> None:
        bench = _load(CI_YML)["jobs"]["bench"]
        step = _step_named(bench, "Bench-baseline governance gate")
        assert step["if"] == "github.event_name == 'pull_request'"
        assert "bench_baseline_gate.py govern" in step["run"]
        assert step["env"]["GH_TOKEN"] == "${{ github.token }}"

    def test_bench_checkout_has_full_history_for_the_diff(self) -> None:
        bench = _load(CI_YML)["jobs"]["bench"]
        checkout = next(s for s in _steps(bench) if "checkout" in str(s.get("uses", "")))
        assert checkout["with"]["fetch-depth"] == 0

    def test_gate_steps_run_before_heavy_installs(self) -> None:
        """Fail-fast contract: both stdlib steps precede the pip install."""
        bench = _load(CI_YML)["jobs"]["bench"]
        names = [str(s.get("name", "")) for s in _steps(bench)]
        validate_idx = next(i for i, n in enumerate(names) if "structural gate" in n)
        install_idx = next(i for i, n in enumerate(names) if "Install deps" in n)
        assert validate_idx < install_idx

    def test_bench_is_a_required_ci_ok_member(self) -> None:
        ci_ok = _load(CI_YML)["jobs"]["ci-ok"]
        assert "bench" in ci_ok["needs"]


class TestBenchM4ProWiring:
    def test_paths_filter_covers_the_new_gate_files(self) -> None:
        data = _load(BENCH_YML)
        paths = data.get(True, data.get("on", {}))["push"]["paths"]
        for required in (
            "tests/bench/test_absolute_baseline.py",
            "tests/bench/_abs_baseline_registry.py",
            "tests/bench/baselines/**",
            "brutex/observability/bench_baseline.py",
            "brutex/config/hardware_envelope.py",
            "scripts/ci/bench_baseline_gate.py",
        ):
            assert required in paths, required

    def test_dispatch_inputs_exist(self) -> None:
        data = _load(BENCH_YML)
        inputs = data.get(True, data.get("on", {}))["workflow_dispatch"]["inputs"]
        assert set(inputs) == {"capture_candidate", "decision_id", "replace_entries"}

    def test_m4pro_job_timeout_is_fifteen(self) -> None:
        job = _load(BENCH_YML)["jobs"]["m4pro-absolute-throughput"]
        assert job["timeout-minutes"] == 15

    def test_m4pro_job_holds_no_write_token(self) -> None:
        """The self-hosted runner NEVER gets contents:write (fatal-flaw pin)."""
        data = _load(BENCH_YML)
        assert data["permissions"] == {"contents": "read", "issues": "write"}
        assert "permissions" not in data["jobs"]["m4pro-absolute-throughput"]

    def test_bless_job_is_the_only_write_holder_and_is_pinned(self) -> None:
        bless = _load(BENCH_YML)["jobs"]["bless-baseline"]
        # issues: write included: a job-level permissions block zeroes every
        # unspecified scope, and the bless-failure alert step calls
        # github.rest.issues.* — without it the alert 403s (review round 2).
        assert bless["permissions"] == {
            "contents": "write",
            "pull-requests": "write",
            "issues": "write",
        }
        assert bless["runs-on"] == "ubuntu-24.04"  # GitHub-hosted, never the runner
        assert bless["needs"] == "m4pro-absolute-throughput"
        condition = bless["if"]
        assert "candidate_ready == '1'" in condition
        assert "github.ref == 'refs/heads/main'" in condition
        assert "github.event_name == 'schedule'" in condition
        assert "github.event_name == 'workflow_dispatch'" in condition

    def test_bless_job_reads_the_pinned_artifact_paths(self) -> None:
        """download-artifact flattens the abs-bench/ upload to the job CWD."""
        bless = _load(BENCH_YML)["jobs"]["bless-baseline"]
        script = _step_named(bless, "Open bless PR")["with"]["script"]
        assert "readFileSync('candidate.json')" in script
        assert "readFileSync('pr-body.md')" in script

    def test_capture_step_arms_only_on_abstain_or_dispatch(self) -> None:
        job = _load(BENCH_YML)["jobs"]["m4pro-absolute-throughput"]
        step = _step_named(job, "Capture baseline candidate")
        condition = step["if"]
        assert "needs_capture == '1'" in condition
        assert "inputs.capture_candidate" in condition
        # NEVER armed by a regression outcome (a red compare fails the job).
        assert "REGRESSION" not in condition
        assert "failure()" not in condition

    def test_compare_step_retries_numeric_only(self) -> None:
        job = _load(BENCH_YML)["jobs"]["m4pro-absolute-throughput"]
        run = _step_named(job, "Absolute-baseline compare gate")["run"]
        assert '"$rc" -eq 1' in run  # the ONE bounded re-measure on rc==1
        assert '"$rc" -eq 2' not in run  # a structural red is NEVER retried

    def test_alert_steps_fire_on_cancelled_too(self) -> None:
        data = _load(BENCH_YML)
        failure_issue = _step_named(
            data["jobs"]["m4pro-absolute-throughput"], "Open issue on failure"
        )
        assert "cancelled()" in failure_issue["if"]
        bless_alert = _step_named(data["jobs"]["bless-baseline"], "Alert on bless failure")
        assert "cancelled()" in bless_alert["if"]

    def test_every_issue_writing_job_holds_issues_write(self) -> None:
        """Any job whose steps call ``github.rest.issues.*`` must hold an
        EFFECTIVE ``issues: write`` token: a job-level permissions block sets
        every unspecified scope to none (the workflow-level grant does NOT
        merge in), so a job with ``{contents: write}`` alone would 403 on
        ``issues.create`` — the exact silent-alert failure this pin refutes
        (review round 2)."""
        for path in (BENCH_YML, CI_YML):
            data = _load(path)
            workflow_perms = data.get("permissions", {})
            workflow_issues = (
                workflow_perms.get("issues") if isinstance(workflow_perms, dict) else None
            )
            for name, job in data["jobs"].items():
                writes_issues = any(
                    "github.rest.issues." in str(step.get("with", {}).get("script", ""))
                    for step in _steps(job)
                )
                if not writes_issues:
                    continue
                job_perms = job.get("permissions")
                effective = job_perms.get("issues") if job_perms is not None else workflow_issues
                assert effective == "write", (
                    f"{path.name}: job {name!r} runs an issues-writing step but its "
                    f"effective issues permission is {effective!r} — guaranteed 403"
                )

    def test_issue_write_pin_scans_real_alert_steps(self) -> None:
        """Anti-vacuity twin: the pin above must actually SEE the two
        bench-m4pro alert steps (a detector that matches nothing proves
        nothing)."""
        data = _load(BENCH_YML)
        writers = [
            name
            for name, job in data["jobs"].items()
            if any(
                "github.rest.issues." in str(step.get("with", {}).get("script", ""))
                for step in _steps(job)
            )
        ]
        assert "m4pro-absolute-throughput" in writers
        assert "bless-baseline" in writers

    def test_abstain_tracking_issue_is_distinct_and_non_critical(self) -> None:
        job = _load(BENCH_YML)["jobs"]["m4pro-absolute-throughput"]
        step = _step_named(job, "Track missing baseline")
        assert "needs_capture == '1'" in step["if"]
        script = step["with"]["script"]
        assert "absolute baseline missing/incomplete" in script
        assert "'critical'" not in script  # never confusable with a regression red

    def test_artifact_uploaded_with_retention(self) -> None:
        job = _load(BENCH_YML)["jobs"]["m4pro-absolute-throughput"]
        step = _step_named(job, "Upload absolute-bench artifact")
        assert step["if"] == "always()"
        assert step["with"]["retention-days"] == 90

    def test_no_pull_request_trigger(self) -> None:
        """DEC-M4PRO-PR-TRIGGER-001 preserved: fork code never nears the token."""
        data = _load(BENCH_YML)
        triggers = data.get(True, data.get("on", {}))
        assert "pull_request" not in triggers


class TestGitignore:
    def test_abs_bench_workdir_ignored(self) -> None:
        text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "abs-bench/" in text.splitlines()
