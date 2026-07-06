"""Red-team contract pins for the doctor-daily cron workflow
(DEC-DOCTOR-DAILY-CRON-001, MIGRATION.md #1521).

``.github/workflows/doctor-daily.yml`` runs the plain ``brutex doctor``
daily at 02:00 UTC (= 07:30 IST) on a hosted runner in a hermetic empty
envelope. These pins keep its safety properties from silently rotting:

* **Trigger discipline** — cron + dispatch + a paths-filtered push smoke
  on ``main`` ONLY; NEVER a ``pull_request`` trigger (a scheduled
  observability contract must never gate PRs or join ci-ok).
* **Envelope honesty** — the workflow ASSERTS the 5 operator-envelope
  checks stay SKIP-with-reason and BOTH wheel-freshness handshakes stay
  PASS (never fake-green, never quietly hollow); ``--live`` (the
  sandbox-blind Mac kit) never runs; ``BRUTEX_SKIP_WHEEL_FRESHNESS`` is
  never set (the wheel handshakes are the cron's substantive teeth).
* **Alerting** — every alert step fires on ``cancelled()`` too
  (DEC-MUTATION-ALERT-CANCELLED-001: a job killed by its own
  ``timeout-minutes`` reports CANCELLED, live-probed run 28748795864);
  the tracking issue title is STABLE (SHA-free) so red days fold into
  ONE open issue via ``createComment`` and a green day closes it; the
  Telegram step degrades gracefully when its secrets are unset and can
  NEVER fail the job.

Pure YAML-parse meta-tests — no network, no ``brutex`` import (zero
coverage-gate impact: ``--cov=brutex`` measures the package only).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCTOR_DAILY_YML = REPO_ROOT / ".github" / "workflows" / "doctor-daily.yml"

EXPECTED_STATUSES = {
    "audit_chain": "SKIP",
    "runtime_file": "SKIP",
    "audit_replay": "SKIP",
    "derived_freshness": "SKIP",
    "lake_uniqueness": "SKIP",
    "wheel_freshness": "PASS",
    "wheel_freshness_fno_math": "PASS",
}


def _load() -> dict[Any, Any]:
    """Parse the workflow YAML into a mapping.

    Keyed ``dict[Any, Any]`` because YAML 1.1 parses a bare ``on:`` key
    as the boolean ``True`` (same handling as the redteam siblings).
    """
    data = yaml.safe_load(DOCTOR_DAILY_YML.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _raw() -> str:
    """Return the raw workflow file text."""
    return DOCTOR_DAILY_YML.read_text(encoding="utf-8")


def _on(data: dict[Any, Any]) -> dict[str, Any]:
    """Return the trigger mapping (YAML 1.1 parses a bare ``on:`` as True)."""
    triggers = data.get("on", data.get(True))
    assert isinstance(triggers, dict), "workflow has no trigger mapping"
    return triggers


def _job(data: dict[Any, Any]) -> dict[str, Any]:
    """Return the single doctor job mapping."""
    jobs = data["jobs"]
    assert isinstance(jobs, dict) and list(jobs) == ["doctor"], (
        "doctor-daily must hold exactly the one 'doctor' job"
    )
    job = jobs["doctor"]
    assert isinstance(job, dict)
    return job


def _steps(data: dict[Any, Any]) -> list[dict[str, Any]]:
    """Return the doctor job's step list."""
    steps = _job(data)["steps"]
    assert isinstance(steps, list) and steps
    return [s for s in steps if isinstance(s, dict)]


def _doctor_step(data: dict[Any, Any]) -> dict[str, Any]:
    """Return the step whose run script invokes ``brutex doctor``."""
    for step in _steps(data):
        run = step.get("run")
        if isinstance(run, str) and "brutex doctor" in run:
            return step
    raise AssertionError("no step runs `brutex doctor`")


def _alert_steps(data: dict[Any, Any]) -> list[dict[str, Any]]:
    """Return the red-path alert steps (Telegram + issue filer)."""
    alerts = [s for s in _steps(data) if "failure()" in str(s.get("if", ""))]
    assert len(alerts) == 2, "expected exactly the Telegram + issue alert steps"
    return alerts


def test_workflow_exists_and_parses() -> None:
    """The workflow file exists and is valid YAML with the doctor job."""
    assert DOCTOR_DAILY_YML.exists(), "doctor-daily.yml is missing"
    data = _load()
    assert data["name"] == "doctor-daily"
    assert _job(data)["runs-on"] == "ubuntu-24.04"


def test_cron_is_0730_ist_plus_dispatch() -> None:
    """Schedule fires 02:00 UTC (= 07:30 IST, no DST) + manual dispatch."""
    triggers = _on(_load())
    schedule = triggers["schedule"]
    assert schedule == [{"cron": "0 2 * * *"}]
    assert "workflow_dispatch" in triggers
    # The IST translation must stay documented next to the cron.
    assert "07:30 IST" in _raw()


def test_no_pull_request_trigger_and_scoped_push() -> None:
    """NEVER a pull_request trigger; the push smoke stays paths-filtered.

    This workflow must never gate a PR (an offline-day red would wedge
    mergeable state and it must never join ci-ok). The push trigger is
    ONLY the nightly-sibling smoke: main-only, owned paths only.
    """
    triggers = _on(_load())
    assert "pull_request" not in triggers
    assert "pull_request_target" not in triggers
    push = triggers.get("push")
    if push is not None:
        assert push["branches"] == ["main"]
        assert set(push["paths"]) == {
            ".github/workflows/doctor-daily.yml",
            "brutex/doctor.py",
        }


def test_permissions_minimal() -> None:
    """contents: read + issues: write and nothing else."""
    permissions = _load()["permissions"]
    assert permissions == {"contents": "read", "issues": "write"}


def test_concurrency_never_cancels_in_flight() -> None:
    """cancel-in-progress false: a CANCELLED conclusion stays alert-worthy."""
    concurrency = _load()["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    assert "doctor-daily" in concurrency["group"]


def test_job_timeout_bounded() -> None:
    """The job carries an honest bounded timeout."""
    timeout = _job(_load())["timeout-minutes"]
    assert isinstance(timeout, int)
    assert 15 <= timeout <= 60


def test_doctor_step_has_own_timeout() -> None:
    """The doctor step is step-bounded, bash, and pipefail-guarded.

    Step timeout = step FAILURE (alertable); pipefail because GitHub's
    default ``bash -e`` lacks it and ``tee`` would mask the doctor's
    exit 1 otherwise.
    """
    step = _doctor_step(_load())
    assert step["timeout-minutes"] == 5
    assert step["shell"] == "bash"
    assert "set -o pipefail" in step["run"]


def test_doctor_is_hermetic_and_never_live() -> None:
    """Plain doctor only: hermetic paths, no --live, no wheel-check skip."""
    data = _load()
    step = _doctor_step(data)
    run = step["run"]
    assert "--live" not in run, "doctor --live is sandbox-blind — never in CI"
    assert "--audit-dir" in run
    assert "--runtime-file" in run
    # The hermetic lake root is injected so both lake checks SKIP
    # deterministically regardless of runner-image residue.
    assert "BRUTEX_LAKE_ROOT" in str(step.get("env", {}))
    assert "BRUTEX_LAKE_ROOT" in str(_steps(data))
    # The wheel handshakes are the cron's teeth — the skip env var must
    # never be SET (as an env key or a shell export; the workflow's own
    # comment naming it is fine).
    raw = _raw()
    assert "BRUTEX_SKIP_WHEEL_FRESHNESS:" not in raw
    assert "BRUTEX_SKIP_WHEEL_FRESHNESS=" not in raw
    for wf_step in _steps(data):
        assert "BRUTEX_SKIP_WHEEL_FRESHNESS" not in str(wf_step.get("env", {}))


def test_both_wheels_installed() -> None:
    """brutex.cli hard-requires both PyO3 wheels at import."""
    raw = _raw()
    assert "pip install ./rust/fno-math" in raw
    assert "pip install ./rust/trading-core" in raw


def test_envelope_honesty_contract_asserted() -> None:
    """All 7 check verdicts are pinned: 5 SKIP-with-reason + 2 PASS."""
    steps = _steps(_load())
    assert_step = next(s for s in steps if "assert_status" in str(s.get("run", "")))
    run = assert_step["run"]
    for name, want in EXPECTED_STATUSES.items():
        assert f"assert_status {name} {want}" in run, (
            f"envelope contract line for {name} -> {want} is missing"
        )


def test_alert_steps_fire_on_cancelled() -> None:
    """Every alert step's guard includes cancelled().

    DEC-MUTATION-ALERT-CANCELLED-001: a job killed by its own
    ``timeout-minutes`` reports CANCELLED, not FAILED — an alert guard
    of bare ``failure()`` dies silently on a timeout-kill.
    """
    for step in _alert_steps(_load()):
        assert "cancelled()" in str(step["if"])


def test_issue_title_stable_and_lifecycle_wired() -> None:
    """SHA-free stable title + comment dedupe + close-on-green."""
    raw = _raw()
    title_lines = [ln for ln in raw.splitlines() if "const title" in ln]
    assert title_lines, "issue steps must define a title constant"
    for line in title_lines:
        assert "context.sha" not in line, (
            "a per-SHA title mints one issue per red day — title must be stable"
        )
    assert "createComment" in raw, "repeat reds must fold into one issue"
    assert "'doctor'" in raw, "issue label 'doctor' is the dedupe key"
    assert "state: 'closed'" in raw, "a green day must close the tracking issue"
    close_step = next(
        s for s in _steps(_load()) if "state: 'closed'" in str(s.get("with", {}).get("script", ""))
    )
    assert close_step["if"] == "success()"


def test_telegram_degrades_gracefully() -> None:
    """Unset secrets => notice + exit 0; send failure non-fatal; env-mapped."""
    data = _load()
    telegram = next(s for s in _steps(data) if "TG_TOKEN" in str(s.get("env", {})))
    env = telegram["env"]
    assert "secrets.BRUTEX_TELEGRAM_BOT_TOKEN" in env["TG_TOKEN"]
    assert "secrets.BRUTEX_TELEGRAM_CHAT_ID" in env["TG_CHAT"]
    run = telegram["run"]
    assert "exit 0" in run, "missing-secret path must exit 0"
    assert "||" in run, "a failed send must be non-fatal"
    # `secrets.*` must never appear in an `if:` (unreliable there — route
    # through env), and the step must fire on the red path.
    for step in _steps(data):
        assert "secrets." not in str(step.get("if", ""))
    assert "cancelled()" in str(telegram["if"])


def test_continue_on_error_only_on_telegram() -> None:
    """Belt-and-braces continue-on-error is Telegram-only.

    Anywhere else it would mask a genuine red (a hollowed-out assert
    step with continue-on-error would fake-green the whole cron).
    """
    for step in _steps(_load()):
        if "TG_TOKEN" in str(step.get("env", {})):
            assert step.get("continue-on-error") is True
        else:
            assert "continue-on-error" not in step
