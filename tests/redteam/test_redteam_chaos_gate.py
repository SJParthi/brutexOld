"""Red-team tests for the chaos/race PR gate (DEC-CHAOS-RACE-001).

The operator's stricter-hardening decree added a ``chaos-race`` job that runs the
randomized-schedule + crash/restart + concurrent-reader suite on EVERY PR. These
meta-tests lock the wiring so a future edit cannot silently drop the gate (it would
otherwise sit green-by-absence while real chaos regressions slipped through). They
parse the workflow YAML directly — no network — mirroring
``test_redteam_automerge_safety.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load_jobs(path: Path) -> dict[str, Any]:
    """Return the ``jobs:`` mapping of a workflow YAML file."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = data["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def test_chaos_race_job_exists_and_runs_the_chaos_suite() -> None:
    jobs = _load_jobs(CI_YML)
    assert "chaos-race" in jobs, "the chaos-race job was removed (regression of DEC-CHAOS-RACE-001)"
    steps = jobs["chaos-race"]["steps"]
    run_text = "\n".join(str(s.get("run", "")) for s in steps)
    assert "tests/chaos" in run_text and "-m chaos" in run_text, (
        "chaos-race no longer runs `pytest -m chaos tests/chaos`"
    )


def test_chaos_race_is_a_required_ci_ok_gate() -> None:
    jobs = _load_jobs(CI_YML)
    needs = jobs["ci-ok"]["needs"]
    assert isinstance(needs, list)
    assert "chaos-race" in needs, (
        "chaos-race dropped from ci-ok.needs — a chaos regression would no longer block merge"
    )


def test_chaos_race_result_is_checked_in_the_aggregate_loop() -> None:
    ci_text = CI_YML.read_text(encoding="utf-8")
    assert "needs.chaos-race.result" in ci_text, (
        "chaos-race result not evaluated in the ci-ok aggregate loop"
    )


def test_chaos_suite_exists_and_is_marked() -> None:
    chaos_dir = REPO_ROOT / "tests" / "chaos"
    assert chaos_dir.is_dir(), "tests/chaos directory missing"
    test_files = list(chaos_dir.glob("test_*.py"))
    assert test_files, "no chaos test files present"
    joined = "\n".join(f.read_text(encoding="utf-8") for f in test_files)
    assert "pytest.mark.chaos" in joined, "chaos tests are not marked `chaos`"
