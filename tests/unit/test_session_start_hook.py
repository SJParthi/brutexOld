"""Smoke + behaviour tests for `.claude/hooks/session-start.sh`.

Per DEC-SESSION-START-HOOK-001 (MIGRATION entry #237) — the
SessionStart hook closes the gap that new Claude sessions previously
relied on discipline (not mechanical guarantee) to read CLAUDE.md +
recent MIGRATION decisions before editing.

Even though the hook is a shell script (not Python source) it carries
the same correctness obligations as any module:

- ✅ DETECT: a typo / `set -e` regression would silently break the hook
- ✅ VERIFY: per-field output contract (Branch, HEAD, CLAUDE.md, MIGRATION,
  Z+ markers) must be present so Claude can rely on it
- ✅ RECONCILE: tests live alongside the hook so future edits cross-check
- ✅ PREVENT: failing tests block CI

These tests run the hook in a subprocess + assert on stderr.
"""

from __future__ import annotations

import os
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "session-start.sh"


def _run_hook(
    *,
    cwd: Path | None = None,
    env_override: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the hook in a subprocess; return CompletedProcess.

    The hook is contracted to:
      - exit 0 always
      - write everything to stderr (stdout stays clean for downstream pipes)
    """
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [str(HOOK_PATH)],
        cwd=str(cwd or REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


# ────────────────────────────────────────────────────────────────────────
# File-level invariants
# ────────────────────────────────────────────────────────────────────────


class TestHookFileShape:
    def test_hook_exists(self) -> None:
        assert HOOK_PATH.exists(), f"missing {HOOK_PATH}"

    def test_hook_is_executable(self) -> None:
        assert os.access(HOOK_PATH, os.X_OK), f"{HOOK_PATH} is not executable"

    def test_hook_has_bash_shebang(self) -> None:
        first_line = HOOK_PATH.read_text(encoding="utf-8").splitlines()[0]
        assert first_line.startswith("#!"), "missing shebang"
        assert "bash" in first_line, "expected bash shebang"

    def test_hook_does_not_use_set_dash_e(self) -> None:
        """`set -e` would abort the hook on the first missing optional
        artifact (no `.venv/`, no `docs/MIGRATION.md` yet, etc.), turning
        a notification into a hard failure. The hook is contracted to be
        notification-only."""
        text = HOOK_PATH.read_text(encoding="utf-8")
        # `set -uo pipefail` is OK; `set -e` or `set -euo pipefail` is NOT.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("set ") and "-e" in stripped.split()[1:]:
                # Confirm it's literal -e or compound -euo / -eo
                tokens = stripped.split()
                for tok in tokens[1:]:
                    if tok.startswith("-") and "e" in tok and "no" not in tok:
                        pytest.fail(
                            f"hook uses `set -e` ({line.strip()!r}) which "
                            "would abort on missing optional artifacts"
                        )


# ────────────────────────────────────────────────────────────────────────
# Exit-code contract
# ────────────────────────────────────────────────────────────────────────


class TestExitCode:
    def test_exit_zero_in_repo(self) -> None:
        result = _run_hook()
        assert result.returncode == 0, (
            f"expected exit 0 in repo; got {result.returncode}, stderr={result.stderr[:500]!r}"
        )

    def test_exit_zero_in_non_repo_dir(self, tmp_path: Path) -> None:
        """Graceful exit when launched outside a git repo."""
        result = _run_hook(cwd=tmp_path)
        assert result.returncode == 0, f"expected exit 0 outside repo; got {result.returncode}"


# ────────────────────────────────────────────────────────────────────────
# stdout vs stderr split (downstream pipes must stay clean)
# ────────────────────────────────────────────────────────────────────────


class TestStdoutCleanContract:
    def test_stdout_is_empty(self) -> None:
        """Per the Claude Code hook contract: all output → stderr so the
        hook never corrupts a tool's stdout pipe."""
        result = _run_hook()
        assert result.stdout == "", (
            f"expected empty stdout (all output → stderr); got {result.stdout[:200]!r}"
        )

    def test_stderr_has_content(self) -> None:
        result = _run_hook()
        assert len(result.stderr) > 100, (
            f"expected non-trivial stderr; got {len(result.stderr)} bytes"
        )


# ────────────────────────────────────────────────────────────────────────
# Output content contract (each required field present)
# ────────────────────────────────────────────────────────────────────────


REQUIRED_OUTPUT_TOKENS = (
    "BruteX",
    "SessionStart hook",
    "DEC-SESSION-START-HOOK-001",
    "Branch:",
    "Commits ahead of main:",
    "Uncommitted files:",
    "HEAD:",
    "CLAUDE.md:",
    "MIGRATION decisions",
    "Environment:",
    # Automation & live-state section (DEC-SESSION-HOOK-LIVESTATE-001) — these
    # labels are printed unconditionally so a session always sees the live
    # surface even when the venv / CLI / logs are absent.
    "Automation & live state",
    "DEC-SESSION-HOOK-LIVESTATE-001",
    "Pipeline status:",
    "Audit chain:",
    "Alerting (Telegram):",
    "Automation commands:",
    "Pre-merge gates",
    "Z+ 6-marker",
    "Z+ 15-row matrix",
    "Z+ 7-row resilience matrix",
    "Z+ 7-layer audit",
    "4-agent hostile review",
    "Honest envelope",
    "Decision ID",
)


class TestOutputContent:
    @pytest.mark.parametrize("token", REQUIRED_OUTPUT_TOKENS)
    def test_token_present_in_stderr(self, token: str) -> None:
        result = _run_hook()
        assert token in result.stderr, f"missing required token {token!r} in hook output"

    def test_lists_latest_5_decisions_descending(self) -> None:
        """The hook shows MIGRATION decisions sorted by entry number
        DESCENDING (newest first). Verify by parsing the decisions block
        and asserting the printed entry numbers are non-increasing."""
        result = _run_hook()
        decisions_block_found = False
        printed_nums: list[int] = []
        for line in result.stderr.splitlines():
            stripped = line.strip()
            if stripped.startswith("Last 5 MIGRATION"):
                decisions_block_found = True
                continue
            if decisions_block_found:
                if stripped == "":
                    break
                # Lines look like:  "236. **DEC-M4PRO-ABSOLUTE-001"
                head = stripped.split(".", 1)[0].strip()
                if head.isdigit():
                    printed_nums.append(int(head))
        assert decisions_block_found, "decisions block not printed"
        assert len(printed_nums) >= 1, "no decision entries printed"
        # Non-increasing (descending).
        for prev, curr in pairwise(printed_nums):
            assert prev >= curr, f"decisions not in descending order: {printed_nums}"


# ────────────────────────────────────────────────────────────────────────
# UTF-8 truncation safety (regression: a multi-byte char at the `head -c 80`
# boundary must NOT emit invalid UTF-8 and break a text-mode consumer).
# ────────────────────────────────────────────────────────────────────────


class TestHeadCommitUtf8Truncation:
    """Regression for the `LAST_COMMIT` truncation bug.

    The hook prints ``git log -1 --format='%h %s'`` truncated to 80 bytes.
    A commit subject whose multi-byte UTF-8 character (e.g. the ``→`` arrow
    ``\\xe2\\x86\\x92`` a ledger-renumber commit used) straddles the byte-80
    boundary would leave a severed lead byte, so the hook stream failed to
    decode as UTF-8 and every text-mode (`text=True`) subprocess assertion
    raised ``UnicodeDecodeError`` — breaking CI on that branch. The fix drops
    the orphaned partial sequence (`iconv -c`), so the output is always valid
    UTF-8 regardless of the commit subject.
    """

    @staticmethod
    def _init_repo_with_boundary_arrow_subject(root: Path) -> None:
        """Create a git repo whose HEAD subject places ``→`` at byte 79.

        The subject is ``pad… →`` where ``pad`` is sized so the 3-byte arrow
        begins at byte offset 79 (`git log` prepends ``<hash> ``, 9 bytes) —
        exactly the boundary `head -c 80` severs mid-character.
        """
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            }
        )

        def _git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=str(root), env=env, check=True, capture_output=True)

        _git("init", "-q")
        # `git log -1 --format='%h %s'` = "<7-hex> " (8 bytes) + subject. Place
        # the arrow's first byte at absolute offset 79 → 71 subject bytes before
        # it, so `head -c 80` keeps only the arrow's lead byte `\xe2`.
        subject = ("x" * 71) + "→" + "tail"
        (root / "f.txt").write_text("x", encoding="utf-8")
        _git("add", "f.txt")
        _git("commit", "-q", "-m", subject)

    def test_boundary_arrow_subject_yields_valid_utf8(self, tmp_path: Path) -> None:
        self._init_repo_with_boundary_arrow_subject(tmp_path)
        # Capture RAW BYTES (text=False) so a truncated multi-byte sequence
        # would survive to be inspected rather than raising in the decoder.
        completed = subprocess.run(
            [str(HOOK_PATH)],
            cwd=str(tmp_path),
            env={**os.environ, "CLAUDE_REPO_ROOT": str(tmp_path)},
            capture_output=True,
            timeout=15,
            check=False,
        )
        assert completed.returncode == 0
        # The whole stderr stream MUST decode as UTF-8 (the bug left a severed
        # `\xe2` here). This is the assertion that fails pre-fix, passes post.
        completed.stderr.decode("utf-8")
        # And the HEAD line is still populated (truncation kept the prefix).
        text = completed.stderr.decode("utf-8")
        assert "HEAD:" in text
