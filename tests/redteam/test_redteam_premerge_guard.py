"""Red-team verification: pre-merge-check.sh actually blocks red CI.

Per DEC-P0-004's Z+ defensive promise: the `scripts/ci/pre-merge-check.sh`
script MUST refuse merge when CI is red. This test plants synthetic
"red CI" fixtures + invokes the script + asserts non-zero exit.

Without this test, the guard's correctness is asserted-by-hope. With it,
every CI run verifies the lock still locks.

Per `tests/redteam/` convention: each test uses subprocess to invoke
the real shell script + asserts behavior. No mocking the guard — that
would defeat the purpose.

Implements P1 follow-up #3 from DEC-P0-005 (recorded in MIGRATION #143).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts/ci/pre-merge-check.sh"


@pytest.fixture
def fake_gh_returning(tmp_path: Path) -> Path:
    """Factory: create a fake `gh` binary that returns specified JSON.

    The returned Path points to a directory we'll prepend to PATH so the
    guard script picks up our fake `gh` instead of the real one.
    """

    def _build(status_json: str) -> Path:
        bindir = tmp_path / "fake-bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(
            f"""#!/usr/bin/env bash
# Fake gh CLI for redteam testing — returns a fixed JSON.
if [[ "$1" == "pr" && "$2" == "view" ]]; then
    cat <<'JSON'
{status_json}
JSON
    exit 0
fi
echo "unexpected gh args: $*" >&2
exit 2
"""
        )
        gh.chmod(0o755)
        return bindir

    return _build  # type: ignore[return-value]


def _run_guard(
    pr_number: int,
    env_overrides: dict[str, str],
    extra_path: Path | None = None,
    *,
    override_log_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke pre-merge-check.sh with controlled env.

    Per DEC-P0-006: tests redirect the override log to a tmp path so they
    don't pollute the real audit trail (`scripts/ci/pre-merge-overrides.log`).
    The OVERRIDE_LOG_PATH env var hook in pre-merge-check.sh makes this
    explicit + auditable.
    """
    env = dict(os.environ)
    env.update(env_overrides)
    if extra_path is not None:
        env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"
    if override_log_path is not None:
        env["OVERRIDE_LOG_PATH"] = str(override_log_path)
    return subprocess.run(
        ["bash", str(GUARD), str(pr_number), "SJParthi/brutex"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


class TestGuardRefusesRedCI:
    """Verify the guard exits non-zero when CI is red."""

    def test_guard_refuses_when_a_check_failed(self, fake_gh_returning, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """Plant a CI rollup with one FAILURE → guard must exit non-zero."""
        rollup = {
            "statusCheckRollup": [
                {"name": f"check-{i}", "conclusion": "SUCCESS", "status": "COMPLETED"}
                for i in range(25)
            ],
            "mergeable": "MERGEABLE",
            "number": 99,
        }
        rollup["statusCheckRollup"][3]["conclusion"] = "FAILURE"

        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(99, {}, extra_path=fake_bin)
        assert result.returncode == 1, (
            f"Guard MUST refuse merge on failed check; got exit {result.returncode}.\n"
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "MERGE BLOCKED" in result.stdout or "MERGE BLOCKED" in result.stderr

    def test_guard_refuses_on_pending_checks(self, fake_gh_returning, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """Pending CI → guard must refuse (don't merge mid-run)."""
        rollup = {
            "statusCheckRollup": [
                {"name": f"check-{i}", "conclusion": "SUCCESS", "status": "COMPLETED"}
                for i in range(25)
            ],
            "mergeable": "MERGEABLE",
            "number": 99,
        }
        rollup["statusCheckRollup"][5]["status"] = "IN_PROGRESS"
        rollup["statusCheckRollup"][5]["conclusion"] = None

        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(99, {}, extra_path=fake_bin)
        assert result.returncode != 0, "Guard MUST refuse on pending check"

    def test_guard_refuses_on_merge_conflict(self, fake_gh_returning, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """mergeable=CONFLICTING → guard refuses with exit 6."""
        rollup = {
            "statusCheckRollup": [
                {"name": f"check-{i}", "conclusion": "SUCCESS", "status": "COMPLETED"}
                for i in range(25)
            ],
            "mergeable": "CONFLICTING",
            "number": 99,
        }
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(99, {}, extra_path=fake_bin)
        assert result.returncode == 6, f"Guard MUST exit 6 on CONFLICTING; got {result.returncode}"

    def test_guard_refuses_on_empty_rollup(self, fake_gh_returning, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """Empty CI rollup (PR #25 failure mode) → guard refuses with exit 7."""
        rollup = {"statusCheckRollup": [], "mergeable": "MERGEABLE", "number": 99}
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(99, {}, extra_path=fake_bin)
        assert result.returncode == 7, (
            f"Guard MUST exit 7 on empty rollup (the PR #25 failure mode); got {result.returncode}"
        )
        assert "rollup" in result.stderr.lower() or "registered" in result.stderr.lower()


class TestGuardAllowsGreenCI:
    """Verify the guard exits 0 when all CI is green."""

    def test_guard_allows_when_all_green(self, fake_gh_returning, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """All checks SUCCESS + COMPLETED + mergeable → exit 0."""
        rollup = {
            "statusCheckRollup": [
                {"name": f"check-{i}", "conclusion": "SUCCESS", "status": "COMPLETED"}
                for i in range(25)
            ],
            "mergeable": "MERGEABLE",
            "number": 99,
        }
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(99, {}, extra_path=fake_bin)
        assert result.returncode == 0, (
            f"Guard MUST allow merge on all-green CI; got exit {result.returncode}.\n"
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )


class TestOperatorOverride:
    """Verify the OPERATOR_OVERRIDE escape hatch works + requires reason."""

    def test_override_requires_reason(self, fake_gh_returning, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """OPERATOR_OVERRIDE=true without REASON must exit 5."""
        rollup = {"statusCheckRollup": [], "mergeable": "MERGEABLE", "number": 99}
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(
            99,
            {"OPERATOR_OVERRIDE": "true"},  # no REASON
            extra_path=fake_bin,
        )
        assert result.returncode == 5, (
            f"Guard MUST exit 5 when OPERATOR_OVERRIDE=true but REASON missing; got {result.returncode}"
        )
        assert "REASON" in result.stderr

    def test_override_with_reason_allows_failed_ci(self, fake_gh_returning, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """OPERATOR_OVERRIDE=true + REASON → bypass red CI."""
        rollup = {
            "statusCheckRollup": [
                {
                    "name": f"check-{i}",
                    "conclusion": "FAILURE" if i == 0 else "SUCCESS",
                    "status": "COMPLETED",
                }
                for i in range(25)
            ],
            "mergeable": "MERGEABLE",
            "number": 99,
        }
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(
            99,
            {"OPERATOR_OVERRIDE": "true", "OPERATOR_OVERRIDE_REASON": "redteam test"},
            extra_path=fake_bin,
            override_log_path=tmp_path / "test-overrides.log",
        )
        # Override should allow the merge to proceed.
        assert result.returncode == 0, (
            f"OPERATOR_OVERRIDE+REASON must allow merge; got exit {result.returncode}.\n"
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )


class TestGuardFailsClosed:
    """Verify the guard FAILS CLOSED on instrumentation problems."""

    def test_guard_fails_closed_when_gh_missing(self, tmp_path: Path) -> None:
        """No gh CLI in PATH → guard exits 4 (FAIL-CLOSED per DEC-P0-004)."""
        # PATH includes /bin + /usr/bin so bash works, but NOT system gh.
        # We rely on the default system having gh installed elsewhere
        # (e.g., /usr/local/bin or /opt) — confirm by checking the path
        # used does not contain a gh binary.
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        # Minimal PATH that has bash + basics but no gh.
        # On most systems gh is in /usr/local/bin or /opt/homebrew/bin;
        # we exclude both. /usr/bin + /bin have bash, sed, cat, python3.
        restricted_path = f"{empty_bin}:/usr/bin:/bin"
        # Sanity check our restricted env doesn't have gh.
        check = subprocess.run(
            ["bash", "-c", "command -v gh"],
            env={"PATH": restricted_path, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            check=False,
        )
        if check.returncode == 0:
            pytest.skip(
                f"Test environment has gh at {check.stdout.strip()} via /usr/bin; cannot test fail-closed path"
            )
        result = subprocess.run(
            ["bash", str(GUARD), "99", "SJParthi/brutex"],
            capture_output=True,
            text=True,
            env={"PATH": restricted_path, "HOME": str(tmp_path)},
            timeout=10,
            check=False,
        )
        assert result.returncode == 4, (
            f"Guard MUST exit 4 (FAIL-CLOSED) when gh missing; got {result.returncode}\n"
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "gh CLI not available" in result.stderr or "FAIL CLOSED" in result.stderr

    def test_guard_requires_pr_number(self) -> None:
        """Missing PR number → exit 2 (usage error)."""
        result = subprocess.run(
            ["bash", str(GUARD)],  # no PR num
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode == 2, f"Missing PR# must exit 2; got {result.returncode}"
        assert "Usage" in result.stderr or "PR_NUMBER" in result.stderr
