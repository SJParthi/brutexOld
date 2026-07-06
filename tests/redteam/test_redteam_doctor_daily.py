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
  PASS (never fake-green, never quietly hollow); the check-set contract
  is TWO-sided: a MISSING/renamed name reds via the per-name asserts and
  an ADDED check reds via the exact-7 row-count pin (a future check that
  fabricates PASS against the hermetic envelope can never sail through
  unclassified); ``--live`` (the sandbox-blind Mac kit) never runs;
  ``BRUTEX_SKIP_WHEEL_FRESHNESS`` is never set (the wheel handshakes are
  the cron's substantive teeth). The assert step's bash is FUNCTIONALLY
  executed here against fixture reports (the DEC-CI-FORK-PR-HARDEN-001
  executed-script precedent), so hollowing mutations (``exit 1`` ->
  ``exit 0``, a ``true`` body) fail loudly instead of surviving textual
  grep pins.
* **Alerting** — every alert step fires on ``cancelled()`` too
  (DEC-MUTATION-ALERT-CANCELLED-001: a job killed by its own
  ``timeout-minutes`` reports CANCELLED, live-probed run 28748795864)
  and BOTH arms — ``failure()`` AND ``cancelled()`` — are STEP-SCOPED
  to the doctor/assert outcomes (bare job-scoped forms are Golden-Rule
  #1 hazards: a post-doctor summary/upload flake OR a manual cancel of
  the tail steps on a GREEN doctor day must never file a 'doctor is
  RED' alarm over an all-green table); both red-path guards are pinned
  by EXACT string equality, not substrings — a one-token ``||`` ->
  ``&&`` mutation was empirically verified to hollow the entire alert
  channel while every substring pin stayed green; the tracking issue
  title is STABLE (SHA-free) AND byte-identical between the filer and
  the closer (the exact-title find is the other half of the dedupe key
  — a one-sided title edit would break close-on-green forever); red
  days fold into ONE open issue via ``createComment`` and a green day
  closes it; BOTH issue-lifecycle steps AND the Telegram step are
  ref-gated to ``refs/heads/main`` (the issue is repo-global state
  while concurrency serializes only per-ref, and a red branch
  ``workflow_dispatch`` — the natural debug move while main is red —
  must never ping the operator with a message indistinguishable from
  the main daily alarm); the Telegram step degrades gracefully when its
  secrets are unset and can NEVER fail the job.
* **Token hygiene** — checkout sets ``persist-credentials: false``
  (DEC-CI-FORK-PR-HARDEN-001 convention): the job's ``issues: write``
  GITHUB_TOKEN must never land in ``.git/config`` under the third-party
  pip/cargo build surface the later steps execute.

YAML-parse meta-tests plus a subprocess-executed bash harness for the
assert step — no network, no ``brutex`` import (zero coverage-gate
impact: ``--cov=brutex`` measures the package only).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCTOR_DAILY_YML = REPO_ROOT / ".github" / "workflows" / "doctor-daily.yml"

# The ONE red-path alert guard, pinned by EXACT equality (mirroring the
# closer's existing equality pin). Substring pins alone were empirically
# mutation-vulnerable: rewriting `(failure() || cancelled()) && (...)` to
# `(failure() && cancelled()) && (...)` — one token — keeps every
# substring assert green while failure() and cancelled() are mutually
# exclusive job states, so NO alert would EVER fire. Any future guard
# refinement must red this pin and be reclassified consciously. BOTH arms
# are step-scoped (a manual cancel AFTER a green doctor+assert is not a
# doctor verdict) and the guard is main-ref-gated (a red branch dispatch
# must never masquerade as the main daily alarm).
RED_ALERT_GUARD = (
    "((failure() || cancelled()) && "
    "(steps.doctor.outcome != 'success' || steps.assert.outcome != 'success')) "
    "&& github.ref == 'refs/heads/main'"
)

# The stable dedupe-key title, byte-identical in the filer AND the closer.
ISSUE_TITLE = "doctor-daily: brutex doctor is RED"

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
    # The push smoke is a HARD part of the trigger contract (deleting it
    # would defer a broken doctor-daily.yml / doctor.py merged to main to
    # the next 02:00 UTC cron — up to ~24h of undetected red).
    push = triggers["push"]
    assert push["branches"] == ["main"]
    assert set(push["paths"]) == {
        ".github/workflows/doctor-daily.yml",
        "brutex/doctor.py",
    }


def test_permissions_minimal() -> None:
    """contents: read + issues: write and nothing else."""
    permissions = _load()["permissions"]
    assert permissions == {"contents": "read", "issues": "write"}


def test_checkout_never_persists_credentials() -> None:
    """Checkout must set persist-credentials: false.

    DEC-CI-FORK-PR-HARDEN-001 convention (pre-merge-gate.yml,
    auto-merge.yml): the default ``persist-credentials: true`` writes the
    job's ``issues: write`` GITHUB_TOKEN into ``.git/config``, where the
    third-party build surface the later steps execute (PyPI build
    backends, cargo build.rs/proc-macros) could read it and tamper with
    the very alerting channel this workflow provides. Nothing here uses
    git credentials after checkout, so the hardened form is free.
    """
    checkouts = [
        s for s in _steps(_load()) if str(s.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkouts, "the doctor job must have a checkout step"
    for step in checkouts:
        assert step.get("with", {}).get("persist-credentials") is False


def test_concurrency_never_cancels_in_flight() -> None:
    """cancel-in-progress false + the EXACT per-ref group value.

    The group must be pinned exactly: the issue-lifecycle safety argument
    (main-ref-gated find-then-create is genuinely serialized) leans on
    every main run sharing the ONE ``doctor-daily-refs/heads/main``
    group. A per-run group (e.g. ``doctor-daily-${{ github.run_id }}``)
    would keep the substring pin green while giving ZERO serialization —
    two concurrent red main runs could both pass the listForRepo dedupe
    and mint two same-titled 'critical' issues.
    """
    concurrency = _load()["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    assert concurrency["group"] == "doctor-daily-${{ github.ref }}"


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
    """All 7 check verdicts are pinned: 5 SKIP-with-reason + 2 PASS.

    Plus the TWO-sided check-set pin: the per-name asserts red a
    REMOVED/RENAMED check, and the exact-7 row-count pin reds an ADDED
    one (functionally proven by
    ``test_assert_step_reds_an_added_unclassified_check``).
    """
    run = _assert_step_run()
    for name, want in EXPECTED_STATUSES.items():
        assert f"assert_status {name} {want}" in run, (
            f"envelope contract line for {name} -> {want} is missing"
        )
    assert '"$n" -ne 7' in run, "the exact-7 check-row count pin is missing"


# ---------------------------------------------------------------------------
# Functional teeth for the assert step (DEC-CI-FORK-PR-HARDEN-001 precedent:
# execute the REAL script, don't just grep for its invocation lines). A
# hollowing mutation — `exit 1` -> `exit 0`, an appended `|| true`, a `true`
# body — survives every textual pin but fails these subprocess runs.
# ---------------------------------------------------------------------------

# Mirrors DoctorReport.format()'s status -> glyph map (brutex/doctor.py).
_STATUS_GLYPH = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏸", "WARN": "⚠️"}


def _assert_step_run() -> str:
    """Return the envelope-honesty assert step's real run script."""
    step = next(s for s in _steps(_load()) if "assert_status" in str(s.get("run", "")))
    run = step["run"]
    assert isinstance(run, str)
    return run


def _render_report(statuses: dict[str, str]) -> str:
    """Render a fixture report in the exact DoctorReport.format() shape.

    Line shape: ``<glyph> <name:20s> <status:5s> <detail>`` — awk $2 is
    the check name and $3 the status, exactly what the workflow parses.
    """
    lines = ["brutex doctor — health check report", "=" * 40]
    for name, status in statuses.items():
        lines.append(f"{_STATUS_GLYPH[status]} {name:20s} {status:5s} fixture detail text")
    lines.append("=" * 40)
    lines.append("OVERALL: ✅ PASS")
    return "\n".join(lines) + "\n"


def _run_assert_step(tmp_path: Path, statuses: dict[str, str]) -> int:
    """Execute the assert step's real bash against a fixture report."""
    (tmp_path / "doctor-report.txt").write_text(_render_report(statuses), encoding="utf-8")
    # Executes the repo's OWN workflow bash (nothing attacker-controlled).
    proc = subprocess.run(
        ["bash", "-c", _assert_step_run()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode


def test_assert_step_greens_the_expected_hermetic_report(tmp_path: Path) -> None:
    """The shipped assert bash exits 0 on the exact expected report."""
    assert _run_assert_step(tmp_path, dict(EXPECTED_STATUSES)) == 0


def test_assert_step_reds_a_fabricated_envelope_pass(tmp_path: Path) -> None:
    """A PASS on an operator-envelope check in CI is a fabricated verdict."""
    statuses = dict(EXPECTED_STATUSES)
    statuses["audit_chain"] = "PASS"
    assert _run_assert_step(tmp_path, statuses) == 1


def test_assert_step_reds_a_warn_wheel_handshake(tmp_path: Path) -> None:
    """A WARN wheel handshake means the run proved nothing -> red."""
    statuses = dict(EXPECTED_STATUSES)
    statuses["wheel_freshness_fno_math"] = "WARN"
    assert _run_assert_step(tmp_path, statuses) == 1


def test_assert_step_reds_a_missing_check_name(tmp_path: Path) -> None:
    """A missing check name means the doctor check set changed -> red."""
    statuses = dict(EXPECTED_STATUSES)
    del statuses["wheel_freshness"]
    assert _run_assert_step(tmp_path, statuses) == 1


def test_assert_step_reds_an_added_unclassified_check(tmp_path: Path) -> None:
    """An ADDED check reds too — the check-set contract is two-sided.

    A future check appended to ``run_doctor()`` that fabricates PASS (or
    quietly SKIPs, hiding hollowed teeth) against the hermetic hosted
    envelope is the exact class the assert step exists to catch; without
    the row-count pin it would sail through green because the per-name
    asserts only query the 7 listed names.
    """
    statuses = dict(EXPECTED_STATUSES)
    statuses["vendor_reachability"] = "PASS"
    assert _run_assert_step(tmp_path, statuses) == 1


def test_alert_steps_fire_on_cancelled() -> None:
    """Every alert step's guard includes cancelled().

    DEC-MUTATION-ALERT-CANCELLED-001: a job killed by its own
    ``timeout-minutes`` reports CANCELLED, not FAILED — an alert guard
    of bare ``failure()`` dies silently on a timeout-kill.
    """
    for step in _alert_steps(_load()):
        assert "cancelled()" in str(step["if"])


def test_alert_guards_are_step_scoped_to_doctor_and_assert() -> None:
    """Both red-path guards equal RED_ALERT_GUARD exactly — never bare forms.

    Bare ``failure()`` is JOB-scoped: a transient post-doctor infra
    failure (summary write / artifact upload — a real recurring GHA
    failure mode) on a GREEN doctor day would file/extend the
    'brutex doctor is RED' critical issue whose own body shows an
    all-green report table — an alarm overclaiming a doctor verdict the
    run never produced (Golden Rule #1). Bare ``cancelled()`` is
    job-scoped the same way: a manual cancel of the summary/upload tail
    AFTER a green doctor+assert is not a doctor verdict either. Both
    arms therefore key on the doctor/assert outcomes (``!= 'success'``
    also covers a setup/bootstrap failure — the doctor step is then
    SKIPPED — and a timeout-kill mid-doctor — outcome 'cancelled').

    EXACT-equality pin (not substrings): a one-token ``||`` -> ``&&``
    guard mutation was empirically verified to pass every substring
    assert while disabling ALL red alerting (failure() and cancelled()
    are effectively mutually exclusive), as does a ``false && (...)``
    prefix. The closer already carries the same equality-pin pattern.
    """
    data = _load()
    assert _doctor_step(data).get("id") == "doctor"
    assert_step = next(s for s in _steps(data) if "assert_status" in str(s.get("run", "")))
    assert assert_step.get("id") == "assert"
    for step in _alert_steps(data):
        assert step["if"] == RED_ALERT_GUARD, (
            "red-path alert guard drifted from the pinned expression — any "
            "refinement must be reclassified here consciously"
        )


def test_issue_title_stable_and_lifecycle_wired() -> None:
    """SHA-free stable title, filer==closer title, dedupe + close-on-green."""
    raw = _raw()
    title_lines = [ln for ln in raw.splitlines() if "const title" in ln]
    assert len(title_lines) == 2, "filer + closer must each define the title constant"
    for line in title_lines:
        assert "context.sha" not in line, (
            "a per-SHA title mints one issue per red day — title must be stable"
        )
    # Pin the dedupe KEY's other half end-to-end, mirroring the labels-filter
    # count pin below: the closer finds the filer-created issue by EXACT
    # title match, so the two duplicated `const title` literals must stay
    # byte-identical — a one-sided edit (empirically verified to pass every
    # other pin) would leave the open 'critical' alarm never auto-closed on
    # green, silently breaking the issue body's own 'a green day auto-closes
    # it' promise (Golden Rule #1).
    titles = re.findall(r"const title = '([^']*)';", raw)
    assert len(titles) == 2, "filer + closer must each define exactly one title constant"
    assert set(titles) == {ISSUE_TITLE}, (
        "filer and closer title constants must both equal the pinned stable "
        "title byte-for-byte — a divergent closer title breaks close-on-green "
        "forever"
    )
    assert "createComment" in raw, "repeat reds must fold into one issue"
    # Pin the dedupe QUERY end-to-end: both lifecycle steps must list with
    # the SAME label filter, and the created issue must carry a superset of
    # those labels so the query can find it (a drifted filter would mint a
    # new 'critical' issue every red day and never auto-close any of them).
    assert raw.count("labels: 'bug,doctor',") == 2, (
        "filer + closer must both query with the labels: 'bug,doctor' filter"
    )
    assert "labels: ['bug', 'doctor', 'critical']," in raw, (
        "the created issue's labels must be a superset of the query filter"
    )
    assert "state: 'closed'" in raw, "a green day must close the tracking issue"
    close_step = next(
        s for s in _steps(_load()) if "state: 'closed'" in str(s.get("with", {}).get("script", ""))
    )
    assert close_step["if"] == "success() && github.ref == 'refs/heads/main'"


def test_issue_lifecycle_is_main_ref_gated() -> None:
    """Both issue-lifecycle steps only run on refs/heads/main.

    The tracking issue is repo-global mutable state keyed by a STABLE
    title, but the concurrency group serializes only per-ref. Ungated, a
    GREEN branch ``workflow_dispatch`` (the natural debug move while main
    is red) would falsely close the live main 'critical' alarm, and a RED
    branch dispatch racing a red main run could pass the find-then-create
    dedupe twice and mint two same-titled issues. The main-ref gate puts
    every issue-touching run in the ONE main concurrency group.
    """
    issue_steps = [
        s for s in _steps(_load()) if "listForRepo" in str(s.get("with", {}).get("script", ""))
    ]
    assert len(issue_steps) == 2, "expected exactly the filer + closer issue steps"
    for step in issue_steps:
        assert "github.ref == 'refs/heads/main'" in str(step["if"])


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
    # MAIN-REF-GATED like the issue steps: a red branch workflow_dispatch
    # (the natural debug move while main is red) must never ping the
    # operator with a message indistinguishable from the main daily alarm.
    assert "github.ref == 'refs/heads/main'" in str(telegram["if"])


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
