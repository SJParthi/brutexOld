"""CLAUDE.md §1 ↔ §3 active-branch consistency pin (DEC-LAKE-STATS-MANIFEST-001, ENDGAME round 1).

Regression net for a recurring documentation-drift class: §1's ``**Active
branch:**`` line and §3's ``**Active feature branch:**`` line are edited by
every feature PR, but an update-merge with ``main`` resolves only the
CONFLICTED line — the other silently takes main's value, leaving the merged
file naming TWO different "active" branches (the ENDGAME round-1 refuter
panel caught exactly this: §1 said ``claude/dazzling-pasteur-jf8s3w`` while
§3 still said ``claude/ecstatic-cannon-o3zbbe``).

This deterministic pin fails the suite the moment the two lines diverge, so
the drift can never outlive a PR's own CI again. It asserts CONSISTENCY only
— it deliberately does NOT hard-code a branch name, so it stays green across
every future feature branch without edits (CLAUDE.md §11 DYNAMIC).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

_CLAUDE_MD: Final[Path] = Path(__file__).resolve().parents[2] / "CLAUDE.md"

#: §1 line: ``- **Active branch:** `claude/...` (...``
_S1_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^- \*\*Active branch:\*\* `(?P<branch>[^`]+)`", re.MULTILINE
)

#: §3 line: ``- **Active feature branch:** `claude/...` ``
_S3_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^- \*\*Active feature branch:\*\* `(?P<branch>[^`]+)`", re.MULTILINE
)


def _single_branch(pattern: re.Pattern[str], text: str, label: str) -> str:
    """Return the one branch name ``pattern`` captures in ``text``.

    Fails loudly when the marker line is missing or duplicated — either
    would make "the" active branch ambiguous for a reader of CLAUDE.md.
    """
    matches = [m.group("branch") for m in pattern.finditer(text)]
    assert len(matches) == 1, (
        f"expected exactly one {label} line in CLAUDE.md, found {len(matches)}: {matches!r}"
    )
    return matches[0]


class TestActiveBranchConsistency:
    """§1 and §3 must name the SAME active branch (one source of truth)."""

    def test_section1_and_section3_name_the_same_branch(self) -> None:
        text = _CLAUDE_MD.read_text(encoding="utf-8")
        s1 = _single_branch(_S1_PATTERN, text, "§1 '**Active branch:**'")
        s3 = _single_branch(_S3_PATTERN, text, "§3 '**Active feature branch:**'")
        assert s1 == s3, (
            f"CLAUDE.md names two different active branches: §1 says {s1!r} "
            f"but §3 says {s3!r} — an update-merge resolved one line and "
            "silently took main's value for the other; edit the stale line"
        )

    def test_active_branch_is_a_claude_branch(self) -> None:
        """The shared name follows the repo's `claude/<slug>` convention."""
        text = _CLAUDE_MD.read_text(encoding="utf-8")
        s1 = _single_branch(_S1_PATTERN, text, "§1 '**Active branch:**'")
        assert s1.startswith("claude/"), f"unexpected active-branch form: {s1!r}"
