"""Red-team contract pins for the alert-wiring observability pack
(DEC-AUDIT-ALERT-CLAMP-OBS-001, OPS-3 + OPS-6).

Two silent-alert failure modes are pinned here so they cannot re-rot:

* **OPS-6** — several scheduled security nets alert on ``failure()`` but
  NOT ``cancelled()``. A job killed by its own ``timeout-minutes`` reports
  CANCELLED, not FAILED (the exact class fixed for mutation.yml under
  DEC-MUTATION-ALERT-CANCELLED-001, live-probed run 28748795864), so a
  timeout-killed run died SILENTLY. Every targeted alert / evidence-upload
  step must now fire on ``cancelled()`` too.

* **OPS-3** — ``redteam-nightly.yml`` (the fork-PR / planted-bug security
  net) had ZERO failure alerting — only an ``if: always()`` step-summary
  echo — and ``permissions: contents: read`` could not even file an issue.
  It must now carry ``issues: write`` and a ``(failure() || cancelled())``
  issue-filer step, mirroring the doctor-daily.yml pattern.

Pure YAML-parse / text pins — no ``brutex`` import (zero coverage-gate
impact: ``--cov=brutex`` measures the package only), no network.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _load(name: str) -> dict[str, object]:
    return yaml.safe_load(_read(name))


# --------------------------------------------------------------------------- #
# OPS-6 — every targeted alert / evidence-upload step fires on cancelled() too.
# The condition text is pinned as a substring: a timeout-KILL reports
# cancelled(), so a step that only guards failure() dies silently.
# --------------------------------------------------------------------------- #
class TestOps6AlertOnCancelled:
    def test_safety_yml_all_three_alert_steps_include_cancelled(self) -> None:
        text = _read("safety.yml")
        # safety.yml has THREE identical "Open issue on failure" alert steps.
        assert text.count("if: (failure() || cancelled()) && github.event_name == 'schedule'") == 3
        # The old failure()-only form must be fully gone.
        assert "if: failure() && github.event_name == 'schedule'" not in text

    def test_security_audit_notify_job_includes_cancelled(self) -> None:
        text = _read("security-audit.yml")
        assert "if: (failure() || cancelled()) && github.event_name == 'schedule'" in text
        assert "if: failure() && github.event_name == 'schedule'" not in text

    def test_verify_m4pro_alert_step_includes_cancelled(self) -> None:
        text = _read("verify-m4pro.yml")
        assert "if: (failure() || cancelled()) && github.event_name == 'schedule'" in text
        assert "if: failure() && github.event_name == 'schedule'" not in text

    def test_gitleaks_upload_step_includes_cancelled(self) -> None:
        text = _read("gitleaks.yml")
        assert "if: failure() || cancelled()" in text
        # The bare failure()-only guard must be gone (the report upload is the
        # only evidence a timeout-killed gitleaks run leaves behind).
        assert "\n        if: failure()\n" not in text

    def test_rust_fuzz_smoke_upload_step_includes_cancelled(self) -> None:
        text = _read("rust-fuzz-smoke.yml")
        assert "if: failure() || cancelled()" in text
        assert "\n        if: failure()\n" not in text


# --------------------------------------------------------------------------- #
# OPS-3 — redteam-nightly.yml gains issues:write + a failure/cancelled filer.
# --------------------------------------------------------------------------- #
class TestOps3RedteamNightlyAlerting:
    def test_permissions_now_grant_issues_write(self) -> None:
        wf = _load("redteam-nightly.yml")
        perms = wf["permissions"]
        assert isinstance(perms, dict)
        assert perms.get("issues") == "write"
        # contents stays read (least privilege preserved).
        assert perms.get("contents") == "read"

    def test_has_a_failure_or_cancelled_issue_filer_step(self) -> None:
        wf = _load("redteam-nightly.yml")
        steps = wf["jobs"]["redteam"]["steps"]
        filer = [s for s in steps if "File or update" in str(s.get("name", ""))]
        assert len(filer) == 1, "redteam-nightly must have exactly one issue-filer step"
        cond = filer[0]["if"]
        # BOTH arms present — a timeout-KILL reports cancelled().
        assert "failure()" in cond
        assert "cancelled()" in cond
        # Step-scoped to the harness outcome (never a post-harness flake alarm)
        # and main-ref-gated (a red branch dispatch must not fire the main alarm).
        assert "steps.redteam.outcome != 'success'" in cond
        assert "refs/heads/main" in cond
        # It actually files an issue (github-script), not just an echo.
        assert filer[0]["uses"].startswith("actions/github-script")

    def test_harness_step_has_id_for_step_scoping(self) -> None:
        wf = _load("redteam-nightly.yml")
        steps = wf["jobs"]["redteam"]["steps"]
        harness = [s for s in steps if s.get("id") == "redteam"]
        assert len(harness) == 1
        assert "pytest tests/redteam/" in str(harness[0]["run"])

    def test_has_a_recovery_closer_step(self) -> None:
        wf = _load("redteam-nightly.yml")
        steps = wf["jobs"]["redteam"]["steps"]
        closer = [s for s in steps if "Close the tracking issue" in str(s.get("name", ""))]
        assert len(closer) == 1
        assert closer[0]["if"] == "success() && github.ref == 'refs/heads/main'"

    def test_filer_and_closer_share_one_stable_title(self) -> None:
        # A one-sided title edit would break close-on-green forever (the title
        # IS the dedupe key). Pin that exactly one stable title string appears
        # in both github-script bodies.
        text = _read("redteam-nightly.yml")
        title = "redteam-nightly: a claimed CI gate is silently broken (RED)"
        assert text.count(title) == 2  # filer + closer
