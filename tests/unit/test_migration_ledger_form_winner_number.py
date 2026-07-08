"""DEC-FORM-WINNER-PERSIST-001 — eleventh-renumber ledger/citation consistency pin.

Regression net for the recurring review-finding class that has forced ELEVEN
renumbers of this branch's ledger entry (DEC-LEDGER-RENUMBER-001): live main
mints a new MIGRATION.md number while the branch is in flight, and merging
as-is would file TWO different decisions under ONE number — most recently
live main took #1557 for DEC-DASHBOARD-BROWSER-TRUTH-001 (PR #1233) while
this branch was in flight, so DEC-FORM-WINNER-PERSIST-001 moved #1557 → #1558
(the next free number above live-main max #1557; after the tenth move
#1543 → #1557 when main took #1543 for DEC-FUTURES-EXEC-TRANSLATION-001,
PR #1255). Three permanent pins:

1. the entry carries exactly #1558 (fails-before on the pre-fix colliding
   #1557, and on any future silent drift of the merged number);
2. the entry's provenance trail RECORDS the tenth AND eleventh renumbers
   naming each colliding decision, so the ledger stays the auditable single
   source of truth for its own numbering history;
3. every numbered CLAUDE.md citation of the DEC-ID cites the SAME number as
   the ledger header — kills the partial-update drift where a renumber lands
   in the ledger but a missed citation surface silently points readers at
   main's unrelated entry after merge.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = REPO_ROOT / "docs" / "MIGRATION.md"
CLAUDE_MD_PATH = REPO_ROOT / "CLAUDE.md"

#: The decision whose number keeps colliding with live main's fresh mints.
DEC_ID = "DEC-FORM-WINNER-PERSIST-001"

#: The post-renumber number — re-grepped free above live-main max #1557
#: (DEC-DASHBOARD-BROWSER-TRUTH-001) inside the round-11 merge-of-main commit.
EXPECTED_NUMBER = 1558

#: The decision live main filed under the tenth-round colliding number #1543.
TENTH_COLLIDING_DEC_ID = "DEC-FUTURES-EXEC-TRANSLATION-001"

#: The decision live main filed under the eleventh-round colliding number #1557.
ELEVENTH_COLLIDING_DEC_ID = "DEC-DASHBOARD-BROWSER-TRUTH-001"

#: A ledger entry HEADER: line-start number (optional disambiguation suffix)
#: followed by the bolded DEC-ID — mid-line cross-references never match.
_HEADER_RE = re.compile(r"^(\d+)[a-z]?\. \*\*" + re.escape(DEC_ID))

#: A numbered CLAUDE.md citation: the DEC-ID followed (within a short,
#: ``#``-free window, so a neighbouring entry's number is never captured)
#: by ``MIGRATION.md #<n>``. ``[^#]`` also spans the headline/blurb line
#: breaks without DOTALL.
_CITATION_RE = re.compile(re.escape(DEC_ID) + r"[^#]{0,80}MIGRATION\.md #(\d+)")


def _form_winner_entry() -> tuple[int, str]:
    """Return ``(number, header line)`` of the one DEC-FORM-WINNER-PERSIST-001 entry."""
    matches = [
        (int(match.group(1)), line)
        for line in MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
        if (match := _HEADER_RE.match(line)) is not None
    ]
    assert len(matches) == 1, f"expected exactly ONE {DEC_ID} ledger entry, found {len(matches)}"
    return matches[0]


def test_entry_is_numbered_1558() -> None:
    """The entry carries #1558 — the next number free on live main.

    Fails-before/passes-after: pre-fix the entry was #1557, which live main
    had ALREADY taken for DEC-DASHBOARD-BROWSER-TRUTH-001 (PR #1233) while
    this branch was in flight — merging as-is would have filed two different
    decisions under one number.
    """
    number, _ = _form_winner_entry()
    assert number == EXPECTED_NUMBER, (
        f"{DEC_ID} is numbered #{number}, expected #{EXPECTED_NUMBER} "
        f"(#1557 is taken on live main by {ELEVENTH_COLLIDING_DEC_ID})"
    )


def test_provenance_trail_records_tenth_and_eleventh_renumbers() -> None:
    """The entry's ledger trail names both renumber moves and their colliders.

    DEC-LEDGER-RENUMBER-001 discipline: every renumber is appended to the
    entry's own provenance note, never silently rewritten over.
    """
    _, line = _form_winner_entry()
    assert "renumbered a TENTH time #1543 → #1557" in line, (
        "the tenth-renumber provenance note is missing from the entry's ledger trail"
    )
    assert TENTH_COLLIDING_DEC_ID in line, (
        f"the ledger trail does not name the colliding decision {TENTH_COLLIDING_DEC_ID}"
    )
    assert "renumbered an ELEVENTH time #1557 → #1558" in line, (
        "the eleventh-renumber provenance note is missing from the entry's ledger trail"
    )
    assert ELEVENTH_COLLIDING_DEC_ID in line, (
        f"the ledger trail does not name the colliding decision {ELEVENTH_COLLIDING_DEC_ID}"
    )


def test_claude_md_citations_match_ledger_number() -> None:
    """Every numbered CLAUDE.md citation of the DEC-ID equals the ledger number.

    The two citation surfaces (the ``_Last updated:`` headline and the
    Active-branch blurb) must move in lockstep with the ledger on every
    renumber — a missed surface would cite main's unrelated #1557 entry.
    """
    number, _ = _form_winner_entry()
    text = CLAUDE_MD_PATH.read_text(encoding="utf-8")
    cited = [int(match.group(1)) for match in _CITATION_RE.finditer(text)]
    assert len(cited) >= 2, (
        f"expected >= 2 numbered CLAUDE.md citations of {DEC_ID} "
        f"(headline + Active-branch blurb), found {len(cited)}"
    )
    assert set(cited) == {number}, (
        f"CLAUDE.md cites MIGRATION.md #{sorted(set(cited))} for {DEC_ID}, "
        f"but the ledger entry is numbered #{number}"
    )
