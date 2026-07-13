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
import re
import subprocess
from pathlib import Path
from typing import Any

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
        rollup: dict[str, Any] = {
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
        rollup: dict[str, Any] = {
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


class TestIgnoreCheckNames:
    """DEC-MERGE-LIVE-ROLLUP-001: IGNORE_CHECK_NAMES drops named check runs
    from the count + failed + pending scan, so the auto-merge path can exclude
    its OWN structurally-pending self-check — while every REAL CI check still
    gates fully. Default empty = no behaviour change (proven by every other
    test in this file running with no IGNORE_CHECK_NAMES set)."""

    def test_ignored_pending_self_check_does_not_block(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """An IN_PROGRESS check whose name is ignored must NOT block the merge."""
        rollup: dict[str, Any] = {
            "statusCheckRollup": [
                {"name": f"check-{i}", "conclusion": "SUCCESS", "status": "COMPLETED"}
                for i in range(25)
            ],
            "mergeable": "MERGEABLE",
            "number": 99,
        }
        # The auto-merge workflow's own check run, structurally pending.
        rollup["statusCheckRollup"].append(
            {"name": "Auto-merge on green CI", "conclusion": None, "status": "IN_PROGRESS"}
        )
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(
            99,
            {"IGNORE_CHECK_NAMES": "Auto-merge on green CI"},
            extra_path=fake_bin,
        )
        assert result.returncode == 0, (
            "an ignored pending self-check must not block a green rollup; "
            f"got exit {result.returncode}\nstdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_ignored_self_check_does_not_mask_a_real_failure(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """Ignoring the self-check must NOT let a REAL failing check through."""
        rollup: dict[str, Any] = {
            "statusCheckRollup": [
                {"name": f"check-{i}", "conclusion": "SUCCESS", "status": "COMPLETED"}
                for i in range(25)
            ],
            "mergeable": "MERGEABLE",
            "number": 99,
        }
        rollup["statusCheckRollup"][7]["conclusion"] = "FAILURE"
        rollup["statusCheckRollup"].append(
            {"name": "Auto-merge on green CI", "conclusion": None, "status": "IN_PROGRESS"}
        )
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(
            99,
            {"IGNORE_CHECK_NAMES": "Auto-merge on green CI"},
            extra_path=fake_bin,
        )
        assert result.returncode == 1, (
            "a REAL failing check must still block even when the self-check is "
            f"ignored; got exit {result.returncode}"
        )
        assert "MERGE BLOCKED" in result.stdout or "MERGE BLOCKED" in result.stderr

    def test_ignored_check_does_not_inflate_rollup_below_min(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """The ignored check must NOT count toward the EXPECTED_MIN_CHECKS floor.

        20 real green checks + the ignored self-check = 21 entries, but after
        excluding the ignored one only 20 real checks remain — exactly the
        floor, so the merge is allowed. (Without the exclusion-from-count this
        would still pass; this pins that the ignored entry is not double-counted
        as a real check that could mask an under-registered rollup.)
        """
        rollup: dict[str, Any] = {
            "statusCheckRollup": [
                {"name": f"check-{i}", "conclusion": "SUCCESS", "status": "COMPLETED"}
                for i in range(19)
            ],
            "mergeable": "MERGEABLE",
            "number": 99,
        }
        # Only 19 real checks + the ignored one → 19 real < 20 floor → blocks.
        rollup["statusCheckRollup"].append(
            {"name": "Auto-merge on green CI", "conclusion": None, "status": "IN_PROGRESS"}
        )
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(
            99,
            {"IGNORE_CHECK_NAMES": "Auto-merge on green CI"},
            extra_path=fake_bin,
        )
        assert result.returncode == 7, (
            "with the ignored check excluded, only 19 real checks remain (< 20 "
            f"floor) so the rollup is incomplete and must block; got {result.returncode}"
        )


class TestM4proNonblocking:
    """DEC-PREMERGE-M4PRO-NONBLOCK-001: the hardcoded NONBLOCKING_CHECK_NAMES
    set (`m4pro-absolute-throughput` — the bench-m4pro.yml self-hosted job,
    NOT in ci.yml's `ci-ok` needs list / the required "CI OK (aggregate
    gate)" context; MIGRATION.md #382 + #1534) is UNIONED with the caller's
    IGNORE_CHECK_NAMES at all three scan sites, so an offline-runner
    pending-forever m4pro check can never block a merge — while every
    REQUIRED check still gates fully and a failed m4pro is surfaced as a
    WARN-only line that never affects the exit code."""

    def _green_rollup(self, count: int = 25) -> dict[str, Any]:
        """A rollup of `count` green required-style checks (clears the floor)."""
        return {
            "statusCheckRollup": [
                {"name": f"check-{i}", "conclusion": "SUCCESS", "status": "COMPLETED"}
                for i in range(count)
            ],
            "mergeable": "MERGEABLE",
            "number": 99,
        }

    def test_pending_m4pro_with_required_green_is_merge_ready(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """FAILS-BEFORE case: a pending m4pro check must NOT block a green PR.

        The self-hosted M4-Pro runner is offline, so `m4pro-absolute-
        throughput` sits QUEUED forever on any PR touching a bench trigger
        path — pre-fix, the fail-closed pending scan refused the merge
        indefinitely even though the check is NON-required.
        """
        rollup = self._green_rollup()
        rollup["statusCheckRollup"].append(
            {"name": "m4pro-absolute-throughput", "conclusion": None, "status": "QUEUED"}
        )
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(99, {}, extra_path=fake_bin)
        assert result.returncode == 0, (
            "a pending NON-required m4pro check must not block a green rollup; "
            f"got exit {result.returncode}\nstdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "all CI checks GREEN" in result.stdout

    def test_pending_ci_ok_aggregate_still_blocks(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """The REQUIRED aggregate gate pending must still block (fail-closed)."""
        rollup = self._green_rollup()
        rollup["statusCheckRollup"].append(
            {"name": "CI OK (aggregate gate)", "conclusion": None, "status": "IN_PROGRESS"}
        )
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(99, {}, extra_path=fake_bin)
        assert result.returncode == 1, (
            "a pending REQUIRED check must still block; "
            f"got exit {result.returncode}\nstdout={result.stdout!r}"
        )
        assert "PENDING checks" in result.stdout
        assert "CI OK (aggregate gate)" in result.stdout

    def test_failed_required_check_still_blocks(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """A FAILED required-style check must still block (never weakened)."""
        rollup = self._green_rollup()
        rollup["statusCheckRollup"].append(
            {"name": "CI OK (aggregate gate)", "conclusion": "FAILURE", "status": "COMPLETED"}
        )
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(99, {}, extra_path=fake_bin)
        assert result.returncode == 1, (
            "a FAILED required check must still block; "
            f"got exit {result.returncode}\nstdout={result.stdout!r}"
        )
        assert "MERGE BLOCKED" in result.stdout or "MERGE BLOCKED" in result.stderr
        assert "FAILED" in result.stdout or "FAILED" in result.stderr

    def test_failed_m4pro_warns_but_does_not_block(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """A FAILED m4pro check must warn loudly but never affect the exit code."""
        rollup = self._green_rollup()
        rollup["statusCheckRollup"].append(
            {"name": "m4pro-absolute-throughput", "conclusion": "FAILURE", "status": "COMPLETED"}
        )
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(99, {}, extra_path=fake_bin)
        assert result.returncode == 0, (
            "a FAILED non-blocking m4pro check must not block a green rollup; "
            f"got exit {result.returncode}\nstdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "Non-blocking check(s) in a failed state" in result.stdout, (
            f"expected the WARN-only line naming the failed m4pro check; got: {result.stdout!r}"
        )
        assert "m4pro-absolute-throughput" in result.stdout

    def test_caller_ignore_env_does_not_clobber_nonblocking(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """The auto-merge.yml caller shape: IGNORE_CHECK_NAMES is caller-SET
        to "Auto-merge on green CI", so a mere env DEFAULT for the m4pro
        exclusion would be clobbered. This pins the UNION: both the caller's
        ignored self-check AND the hardcoded non-blocking m4pro check are
        excluded from the pending scan in the same run."""
        rollup = self._green_rollup()
        rollup["statusCheckRollup"].append(
            {"name": "Auto-merge on green CI", "conclusion": None, "status": "IN_PROGRESS"}
        )
        rollup["statusCheckRollup"].append(
            {"name": "m4pro-absolute-throughput", "conclusion": None, "status": "QUEUED"}
        )
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(
            99,
            {"IGNORE_CHECK_NAMES": "Auto-merge on green CI"},
            extra_path=fake_bin,
        )
        assert result.returncode == 0, (
            "the caller-set IGNORE_CHECK_NAMES must UNION with (never replace) "
            "the hardcoded non-blocking set; "
            f"got exit {result.returncode}\nstdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_cancelled_m4pro_does_not_block(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """A CANCELLED m4pro check is a failed-state conclusion → warn, no block."""
        rollup = self._green_rollup()
        rollup["statusCheckRollup"].append(
            {"name": "m4pro-absolute-throughput", "conclusion": "CANCELLED", "status": "COMPLETED"}
        )
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(99, {}, extra_path=fake_bin)
        assert result.returncode == 0, (
            "a CANCELLED non-blocking m4pro check must not block; "
            f"got exit {result.returncode}\nstdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "Non-blocking check(s) in a failed state" in result.stdout

    def test_timed_out_m4pro_warns_but_does_not_block(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """A TIMED_OUT m4pro check is a failed-state conclusion → warn, no block.

        Round-4 NIT closure (mutant-verified): without this fixture, dropping
        'TIMED_OUT' from the WARN-block tuple passed the whole suite — blocking
        behavior is unaffected either way (the FAILED_CHECKS tuple keeps it
        non-blocking via the union), so only the WARN line's coverage of the
        TIMED_OUT conclusion had no teeth.
        """
        rollup = self._green_rollup()
        rollup["statusCheckRollup"].append(
            {"name": "m4pro-absolute-throughput", "conclusion": "TIMED_OUT", "status": "COMPLETED"}
        )
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(99, {}, extra_path=fake_bin)
        assert result.returncode == 0, (
            "a TIMED_OUT non-blocking m4pro check must not block a green rollup; "
            f"got exit {result.returncode}\nstdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "Non-blocking check(s) in a failed state" in result.stdout, (
            f"expected the WARN-only line naming the timed-out m4pro check; got: {result.stdout!r}"
        )
        assert "m4pro-absolute-throughput" in result.stdout

    def test_m4pro_does_not_count_toward_min_checks_floor(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """The excluded m4pro entry must NOT inflate the rollup count.

        19 real green checks + the excluded m4pro check = 20 raw entries,
        but after the union-exclusion only 19 real checks remain — below the
        EXPECTED_MIN_CHECKS floor of 20, so the gate must still block
        (the exact TestIgnoreCheckNames count-floor law, unioned in).
        """
        rollup = self._green_rollup(count=19)
        rollup["statusCheckRollup"].append(
            {"name": "m4pro-absolute-throughput", "conclusion": None, "status": "QUEUED"}
        )
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(99, {}, extra_path=fake_bin)
        assert result.returncode == 7, (
            "with the non-blocking check excluded, only 19 real checks remain "
            f"(< 20 floor) so the rollup is incomplete and must block; got {result.returncode}"
        )

    def test_nonblocking_constant_is_exactly_m4pro(self) -> None:
        """Source pin: the hardcoded non-blocking set is EXACTLY m4pro.

        Broadening NONBLOCKING_CHECK_NAMES weakens the merge gate for every
        added check name, so it must stay a conscious code change under a
        NEW DEC. Shapes this pin catches (round-3 hostile-review hardening —
        the round-1 pin missed the append + duplicate-assignment vectors):

        * LITERAL BROADENING — the one assignment's value must parse (with
          the script's own comma-split/strip/drop-empties idiom) to exactly
          ``{"m4pro-absolute-throughput"}``;
        * DUPLICATE / LINE-START (RE)ASSIGNMENT — the line-start assignment
          census (plain or ``+=``, indented or not) must be exactly the one
          expected literal, so a second ``NONBLOCKING_CHECK_NAMES="${...}"``
          assignment after it fails;
        * APPEND — ``NONBLOCKING_CHECK_NAMES+=`` must not appear anywhere
          in the source;
        * ENV-DEFAULT — the ``"${NONBLOCKING_CHECK_NAMES:-...}"`` idiom
          (which would let a caller env tweak broaden the set — the exact
          clobber trap IGNORE_CHECK_NAMES carries by design) must be absent;
        * ANY OTHER QUOTED USE — every non-literal occurrence must be the
          exact safe inline passthrough feeding a scan site's python3
          environment, never a second value-bearing assignment.

        HONEST envelope: a source-shape pin, not a proof — a sufficiently
        creative bypass (e.g. rebuilding the set inside the embedded
        python) still has to survive review AND the functional teeth in
        the rest of this class.
        """
        source = GUARD.read_text(encoding="utf-8")
        passthrough = 'NONBLOCKING_CHECK_NAMES="$NONBLOCKING_CHECK_NAMES"'

        # (a) Line-start assignment census (plain or append, indentation
        # allowed): exactly ONE, and it is a plain double-quoted literal
        # (no `$` expansion, no `+=`). Kills the second-assignment mutant
        # `NONBLOCKING_CHECK_NAMES="${EVIL:-a,b}"` and any line-start append.
        line_start_assignments = [
            line.strip()
            for line in source.splitlines()
            if re.match(r"\s*NONBLOCKING_CHECK_NAMES\+?=", line)
        ]
        assert len(line_start_assignments) == 1, (
            "expected exactly ONE line-start NONBLOCKING_CHECK_NAMES "
            f"assignment in {GUARD}; found {len(line_start_assignments)}: "
            f"{line_start_assignments!r}"
        )
        assignment = line_start_assignments[0]
        literal = re.fullmatch(r'NONBLOCKING_CHECK_NAMES="([^"$]*)"', assignment)
        assert literal is not None, (
            "the one line-start assignment must be a plain double-quoted "
            f"literal (no expansion, no append); got: {assignment!r}"
        )
        # The script's own comma-split/strip/drop-empties parsing idiom.
        names = {n.strip() for n in literal.group(1).split(",") if n.strip()}
        assert names == {"m4pro-absolute-throughput"}, (
            "the hardcoded non-blocking set must be EXACTLY "
            "{'m4pro-absolute-throughput'} — broadening it requires a code "
            f"change + a new DEC (DEC-PREMERGE-M4PRO-NONBLOCK-001); got {names!r}"
        )

        # (b) No append idiom ANYWHERE (even mid-line): `+=` grows the set.
        assert "NONBLOCKING_CHECK_NAMES+=" not in source, (
            "NONBLOCKING_CHECK_NAMES must never be appended to — broadening "
            "the non-blocking set requires a code change + a new DEC"
        )

        # (c) No env-default read: the constant stays genuinely HARDCODED.
        assert '"${NONBLOCKING_CHECK_NAMES' not in source, (
            "NONBLOCKING_CHECK_NAMES must stay HARDCODED — the env-default "
            "idiom would let a caller env override the non-blocking set"
        )

        # (d) Complete quoted-use census: besides the one literal, every
        # other occurrence must be the exact safe inline passthrough
        # (`NONBLOCKING_CHECK_NAMES="$NONBLOCKING_CHECK_NAMES"` before a
        # scan site's python3 call) — and at least one must exist, pinning
        # that the union wiring is present at all.
        quoted_uses = re.findall(r'NONBLOCKING_CHECK_NAMES\+?="[^"]*"', source)
        assert quoted_uses.count(assignment) == 1, (
            "the literal assignment must appear exactly once among quoted "
            f"uses; got {quoted_uses!r}"
        )
        others = [use for use in quoted_uses if use != assignment]
        assert others and all(use == passthrough for use in others), (
            "every non-literal quoted use must be the exact passthrough "
            f"{passthrough!r}; got: {sorted(set(others))!r}"
        )


# ────────────────────────────────────────────────────────────────────────
# DEC-P0-008 (PR #30): OPERATOR_OVERRIDE rate-limit (Z+ L7 COOLDOWN)
# ────────────────────────────────────────────────────────────────────────


class TestOverrideRateLimit:
    """Verify the override mechanism rate-limits + escalates per DEC-P0-008.

    Default thresholds:
      - Warn at 2 in 24h
      - Hard-fail at 5 in 24h OR 10 in 7d (unless ESCALATION token provided)
    """

    def _seed_log(self, log_path: Path, count: int, hours_ago_start: int = 0) -> None:
        """Write `count` synthetic override entries spread across recent hours."""
        from datetime import UTC, datetime, timedelta

        lines = []
        now = datetime.now(UTC)
        for i in range(count):
            ts = now - timedelta(hours=hours_ago_start + i)
            ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(f"{ts_str} PR=1{i:03d} REPO=test/test REASON=synthetic FAILED=test")
        log_path.write_text("\n".join(lines) + "\n")

    def test_below_warn_threshold_proceeds_silently(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """1 prior override in 24h → no warning, override proceeds."""
        log_path = tmp_path / "overrides.log"
        self._seed_log(log_path, count=1)
        rollup = {
            "statusCheckRollup": [
                {"name": "x", "conclusion": "FAILURE", "status": "COMPLETED"},
            ]
            + [
                {"name": f"c{i}", "conclusion": "SUCCESS", "status": "COMPLETED"} for i in range(25)
            ],
            "mergeable": "MERGEABLE",
            "number": 99,
        }
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(
            99,
            {"OPERATOR_OVERRIDE": "true", "OPERATOR_OVERRIDE_REASON": "low count"},
            extra_path=fake_bin,
            override_log_path=log_path,
        )
        assert result.returncode == 0, (
            f"Below warn threshold (1 override) must allow merge; got exit {result.returncode}.\n"
            f"stderr={result.stderr!r}"
        )

    def test_warn_threshold_proceeds_with_stderr_warning(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """2 prior overrides in 24h → warning printed but override proceeds."""
        log_path = tmp_path / "overrides.log"
        self._seed_log(log_path, count=2)
        rollup = {
            "statusCheckRollup": [
                {"name": "x", "conclusion": "FAILURE", "status": "COMPLETED"},
            ]
            + [
                {"name": f"c{i}", "conclusion": "SUCCESS", "status": "COMPLETED"} for i in range(25)
            ],
            "mergeable": "MERGEABLE",
            "number": 99,
        }
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(
            99,
            {"OPERATOR_OVERRIDE": "true", "OPERATOR_OVERRIDE_REASON": "at warn"},
            extra_path=fake_bin,
            override_log_path=log_path,
        )
        assert result.returncode == 0, "warn threshold must still allow"
        assert "WARNING" in result.stderr or "overrides in last 24h" in result.stderr, (
            f"expected warning in stderr, got: {result.stderr!r}"
        )

    def test_hard_24h_limit_blocks_without_escalation(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """5 prior overrides in 24h → hard fail with exit 8."""
        log_path = tmp_path / "overrides.log"
        self._seed_log(log_path, count=5)
        rollup = {
            "statusCheckRollup": [
                {"name": "x", "conclusion": "FAILURE", "status": "COMPLETED"},
            ]
            + [
                {"name": f"c{i}", "conclusion": "SUCCESS", "status": "COMPLETED"} for i in range(25)
            ],
            "mergeable": "MERGEABLE",
            "number": 99,
        }
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(
            99,
            {"OPERATOR_OVERRIDE": "true", "OPERATOR_OVERRIDE_REASON": "should fail"},
            extra_path=fake_bin,
            override_log_path=log_path,
        )
        assert result.returncode == 8, (
            f"5 overrides in 24h must exit 8 (rate-limit-exceeded); got {result.returncode}.\n"
            f"stderr={result.stderr!r}"
        )
        assert "rate limit" in result.stderr.lower()

    def test_hard_limit_bypassed_with_escalation_token(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """5 prior overrides + correct escalation token → proceeds with marker."""
        log_path = tmp_path / "overrides.log"
        self._seed_log(log_path, count=5)
        rollup = {
            "statusCheckRollup": [
                {"name": "x", "conclusion": "FAILURE", "status": "COMPLETED"},
            ]
            + [
                {"name": f"c{i}", "conclusion": "SUCCESS", "status": "COMPLETED"} for i in range(25)
            ],
            "mergeable": "MERGEABLE",
            "number": 99,
        }
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(
            99,
            {
                "OPERATOR_OVERRIDE": "true",
                "OPERATOR_OVERRIDE_REASON": "must escalate",
                "OPERATOR_OVERRIDE_ESCALATION": "I-acknowledge-this-bypasses-the-cooldown",
            },
            extra_path=fake_bin,
            override_log_path=log_path,
        )
        assert result.returncode == 0, (
            f"Correct escalation token must allow bypass; got exit {result.returncode}.\n"
            f"stderr={result.stderr!r}"
        )
        # Verify the new log entry carries the ESCALATED marker.
        new_log = log_path.read_text()
        assert "ESCALATED" in new_log, f"log must mark escalated entry; got: {new_log!r}"

    def test_wrong_escalation_token_still_blocks(
        self,
        fake_gh_returning,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """Escalation requires the EXACT literal token (friction by design)."""
        log_path = tmp_path / "overrides.log"
        self._seed_log(log_path, count=5)
        rollup = {
            "statusCheckRollup": [
                {"name": "x", "conclusion": "FAILURE", "status": "COMPLETED"},
            ]
            + [
                {"name": f"c{i}", "conclusion": "SUCCESS", "status": "COMPLETED"} for i in range(25)
            ],
            "mergeable": "MERGEABLE",
            "number": 99,
        }
        fake_bin = fake_gh_returning(json.dumps(rollup))
        result = _run_guard(
            99,
            {
                "OPERATOR_OVERRIDE": "true",
                "OPERATOR_OVERRIDE_REASON": "wrong token",
                "OPERATOR_OVERRIDE_ESCALATION": "yes do it",
            },
            extra_path=fake_bin,
            override_log_path=log_path,
        )
        assert result.returncode == 8, (
            f"Wrong escalation token must still block; got exit {result.returncode}"
        )
