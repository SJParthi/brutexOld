"""CLAUDE.md documentation-drift pins (DEC-LAKE-STATS-MANIFEST-001, ENDGAME rounds 1-2).

Regression net for a recurring documentation-drift class — the
highest-authority file claiming a state its own tree disproves:

* ENDGAME round 1: §1's ``**Active branch:**`` line and §3's ``**Active
  feature branch:**`` line are edited by every feature PR, but an
  update-merge with ``main`` resolves only the CONFLICTED line — the other
  silently takes main's value, leaving the merged file naming TWO different
  "active" branches (the round-1 refuter panel caught exactly this: §1 said
  ``claude/dazzling-pasteur-jf8s3w`` while §3 still said
  ``claude/ecstatic-cannon-o3zbbe``).
* ENDGAME round 2: §2 Rule 5 still called the strict M4-Pro tier "DORMANT
  until the operator registers the runner" after ``bench-m4pro.yml``'s own
  header declared the runner LIVE (2026-07-07) and this branch's ledger
  recorded a strict-tier execution (actions run 28888100079); the
  compressed ``_Last updated:`` header narrated review history frozen at
  round 2 while the §1 blurb enumerated rounds 3-8.

These deterministic pins fail the suite the moment the drift returns, so it
can never outlive a PR's own CI again. Where possible they assert
CONSISTENCY / claim-shape rather than hard-coding values, so they stay green
across future branches without edits (CLAUDE.md §11 DYNAMIC).
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


def _normalized_region(text: str, start_marker: str, end_marker: str) -> str:
    """Whitespace-normalized slice of CLAUDE.md between two unique markers."""
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return " ".join(text[start:end].split())


class TestStrictTierClaimFreshness:
    """§2 Rule 5 must track the MEASURED strict-tier reality (endgame round 2).

    The rule's parenthetical claimed the ``BRUTEX_M4PRO_STRICT=1`` tier is
    "DORMANT until the operator registers the runner" — refuted by the very
    workflow it cites (``bench-m4pro.yml`` header: RUNNER IS LIVE,
    2026-07-07) and by MIGRATION.md #1543's CALIBRATION record of a
    strict-tier execution (actions run 28888100079). It also enumerated only
    the cache-tier 3× allowance, omitting the calibrated metadata-sweep 3×
    allowance (``METADATA_SWEEP_STRICT_CEILING``) the same run produced.
    """

    def test_rule5_states_the_live_runner_and_all_measured_allowances(self) -> None:
        text = _CLAUDE_MD.read_text(encoding="utf-8")
        rule5 = _normalized_region(text, "5. **O(1) discipline.**", "6. **Rate-limit safety.**")
        assert "DORMANT" not in rule5, (
            "CLAUDE.md §2 Rule 5 resurrected the stale strict-tier dormancy claim — "
            "the M4-Pro runner is REGISTERED and LIVE (bench-m4pro.yml header, "
            "2026-07-07; strict-tier execution proven by actions run 28888100079)"
        )
        for needle in (
            "REGISTERED and LIVE",
            "28888100079",
            "cache_tier_ceiling",
            "metadata_sweep_ceiling",
            "METADATA_SWEEP_STRICT_CEILING",
        ):
            assert needle in rule5, (
                f"CLAUDE.md §2 Rule 5 no longer states {needle!r} — the live-runner "
                "disclosure / measured-allowance enumeration drifted (endgame round 2)"
            )


class TestHeaderRoundNarrationRetired:
    """The compressed ``_Last updated:`` header must not narrate per-round history.

    Endgame round 2: the header's "review round 1 (...) then round 2 (...)"
    narration froze while the §1 blurb + MIGRATION.md #1543 moved on to
    rounds 3-8 — a within-file freshness drift baked into the header's shape.
    The header now carries a stale-proof POINTER to the two authoritative
    per-round records instead; these pins keep the narration from returning.
    """

    def test_header_points_at_the_authoritative_records(self) -> None:
        text = _CLAUDE_MD.read_text(encoding="utf-8")
        header = _normalized_region(text, "_Last updated:", "Earlier:")
        assert "review round 1 (which" not in header, (
            "the CLAUDE.md _Last updated: header resurrected per-round narration — "
            "it froze at round 2 once (endgame round 2); keep the pointer, not the story"
        )
        assert "per-round record lives ONLY in the §1 Active-branch blurb" in header, (
            "the CLAUDE.md _Last updated: header lost its pointer to the §1 blurb + "
            "MIGRATION.md per-round record (endgame round 2)"
        )
