"""CLAUDE.md documentation-drift pins (DEC-LAKE-STATS-MANIFEST-001, ENDGAME rounds 1-2 + fix round 1).

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
* ENDGAME FIX round 1 (post-merge refuter panel): the wave-resync merge
  renumbered this DEC's ledger entry while a concurrent local merge had
  renumbered the in-tree citations to a THIRD number; the reconcile merge
  adopted the pushed tree verbatim and silently DROPPED the citation sweep,
  leaving 12 stale ``MIGRATION #<old>`` citations resolving to a DIFFERENT
  decision's entry. Also caught: a bare duplicated ``Earlier:`` branch line
  landed in §3, and the ledger entry's own renumber narration disagreed
  with its header.

These deterministic pins fail the suite the moment the drift returns, so it
can never outlive a PR's own CI again. Where possible they assert
CONSISTENCY / claim-shape rather than hard-coding values, so they stay green
across future branches without edits (CLAUDE.md §11 DYNAMIC) — the ledger
citation pin derives the EXPECTED entry number from ``docs/MIGRATION.md``
itself at test time, so it survives (and enforces) every future renumber.
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
        """The shared name follows a sanctioned branch-naming convention.

        Convention (going forward): coordinator-driven integration-train /
        vehicle branches use ``integration/<slug>``; everything else uses
        ``claude/<slug>``. The T1 sweep-core train (PR #1357) is the first
        ``integration/*`` vehicle — its CLAUDE.md honestly names the train
        branch itself as active, per Golden Rule #10, rather than faking a
        ``claude/`` name. Any other form is a merge artifact.
        """
        text = _CLAUDE_MD.read_text(encoding="utf-8")
        s1 = _single_branch(_S1_PATTERN, text, "§1 '**Active branch:**'")
        assert s1.startswith(("claude/", "integration/")), f"unexpected active-branch form: {s1!r}"


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
    2026-07-07) and by MIGRATION.md #1614's CALIBRATION record of a
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
    narration froze while the §1 blurb + MIGRATION.md #1614 moved on to
    rounds 3-8 — a within-file freshness drift baked into the header's shape.
    The header now carries a stale-proof POINTER to the two authoritative
    per-round records instead; these pins keep the narration from returning.

    2026-07-09 merge-reconcile (DEC-PREOPEN-PREVDAY-COMMON-002 — renamed from -001 — landing on
    top): a NEWER decision now heads the ``_Last updated:`` region and the
    DEC-LAKE-STATS-MANIFEST-001 blurb was demoted verbatim to the header
    chain's first ``Earlier:`` slot — so the pin anchors on THAT blurb's own
    ``(DEC-LAKE-STATS-MANIFEST-001, MIGRATION.md #`` marker (renumber-proof,
    position-independent) instead of assuming it still heads the file. The
    guarded law is unchanged: the compressed blurb carries the pointer, not
    the per-round story.
    """

    def test_header_points_at_the_authoritative_records(self) -> None:
        text = _CLAUDE_MD.read_text(encoding="utf-8")
        header = _normalized_region(
            text, "(DEC-LAKE-STATS-MANIFEST-001, MIGRATION.md #", "Earlier:"
        )
        assert "review round 1 (which" not in header, (
            "the CLAUDE.md _Last updated: header resurrected per-round narration — "
            "it froze at round 2 once (endgame round 2); keep the pointer, not the story"
        )
        assert "per-round record lives ONLY in the §1 Active-branch blurb" in header, (
            "the CLAUDE.md _Last updated: header lost its pointer to the §1 blurb + "
            "MIGRATION.md per-round record (endgame round 2)"
        )


_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_MIGRATION_MD: Final[Path] = _REPO_ROOT / "docs" / "MIGRATION.md"

#: The ledger entry HEADER for this branch's decision — ``NNNN. **DEC-...``.
_STATS_DEC_HEADER: Final[re.Pattern[str]] = re.compile(
    r"^(?P<num>\d+)\. \*\*DEC-LAKE-STATS-MANIFEST-001", re.MULTILINE
)

#: A ledger citation as the repo writes it: ``MIGRATION #NNN`` / ``MIGRATION.md #NNN``.
_LEDGER_CITATION: Final[re.Pattern[str]] = re.compile(r"MIGRATION(?:\.md)? #(?P<num>\d+)")

#: The files whose ledger citations belong to DEC-LAKE-STATS-MANIFEST-001,
#: mapped to the OTHER decisions' entry numbers they legitimately cite (the
#: allowlist). Every other ``MIGRATION #NNN`` citation in these files MUST
#: resolve to this DEC's own (live-derived) entry number. Extend a file's
#: allowlist ONLY for a citation that genuinely targets a different decision.
_STATS_CITATION_FILES: Final[dict[str, frozenset[int]]] = {
    ".github/workflows/bench-m4pro.yml": frozenset({236, 979}),
    # 1597 = DEC-MUTATION-RUNTIME-003 (merged in from main 2026-07-08): its
    # sharded-matrix / wedge-proofed-loop rationale comments in mutation.yml
    # and the 3-field pair-format note in the wiring pin's docstring cite
    # THAT decision's entry, not this one.
    # 1685 = DEC-SWEEP-COMPUTE-FOLD-DECOMP-001 (Slice 0 of the Rust sweep-core
    # conversion): its recorded compute.py pair-scope decision comment above
    # the mutmut pair list cites THAT decision's entry, not this one.
    # 1727 = the slice-9 program entry pair
    # (DEC-ORCH-DEAD-SCAFFOLD-DELETE-001, #1727b): the deleted
    # state/retry/store loop pairs' tombstone comments cite THAT deletion
    # record, not this one.
    # 1750 = DEC-RANKING-MUTATION-ZERO-SURVIVOR-001 (2026-07-11): the pass-1
    # runner-list rationale comment (tests/unit/test_ranking_gate_reasons.py
    # MUST stay in the runner) cites THAT decision's entry, not this one.
    # 1774 = DEC-PULL-TODAY-COMPLETENESS-GATE-001 (2026-07-12): the
    # data/today_completeness.py pair's rationale comment cites THAT
    # decision's entry, not this one.
    ".github/workflows/mutation.yml": frozenset({590, 615, 1597, 1685, 1727, 1750, 1774}),
    "brutex/data/lake_stats_manifest.py": frozenset({867}),
    "tests/bench/test_stats_manifest_write_ab.py": frozenset(),
    "tests/unit/test_claude_md_branch_consistency.py": frozenset(),
    "tests/unit/test_lake_stats_manifest.py": frozenset({1597}),
}


def _stats_manifest_entry_line() -> tuple[int, str]:
    """Return ``(entry_number, full_entry_line)`` for DEC-LAKE-STATS-MANIFEST-001.

    Derived LIVE from ``docs/MIGRATION.md`` so every pin below survives (and
    enforces) a future renumber with zero edits.
    """
    text = _MIGRATION_MD.read_text(encoding="utf-8")
    matches = list(_STATS_DEC_HEADER.finditer(text))
    assert len(matches) == 1, (
        f"expected exactly one DEC-LAKE-STATS-MANIFEST-001 entry header in "
        f"docs/MIGRATION.md, found {len(matches)}"
    )
    match = matches[0]
    line_end = text.index("\n", match.start())
    return int(match.group("num")), text[match.start() : line_end]


class TestStatsManifestLedgerCitations:
    """Every in-tree citation of this DEC's ledger entry must resolve to it.

    ENDGAME FIX round 1: the 2026-07-08 wave-resync renumbered the
    DEC-LAKE-STATS-MANIFEST-001 entry, but the reconcile merge (718734c9)
    adopted the pushed CLAUDE.md/MIGRATION.md renumber verbatim and silently
    dropped the concurrent local merge's repo-reference sweep — stranding 12
    ``MIGRATION #<old>`` citations across six files on a number that now
    resolves to a DIFFERENT decision (the futures translation executor).
    The repo's citation-accuracy scanner covers only the normative docs, so
    nothing in CI caught it. This pin derives the EXPECTED number from the
    ledger itself, making the citation set renumber-proof: the moment the
    entry moves again, the stale citations fail THIS suite.
    """

    def test_every_stats_manifest_citation_targets_the_live_entry(self) -> None:
        expected, _ = _stats_manifest_entry_line()
        violations: list[str] = []
        for rel_path, allowed in _STATS_CITATION_FILES.items():
            text = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
            for match in _LEDGER_CITATION.finditer(text):
                cited = int(match.group("num"))
                if cited != expected and cited not in allowed:
                    line = text.count("\n", 0, match.start()) + 1
                    violations.append(f"{rel_path}:{line} cites entry {cited}")
        assert not violations, (
            f"stale DEC-LAKE-STATS-MANIFEST-001 ledger citations — the live entry is "
            f"{expected} (derived from docs/MIGRATION.md): {violations!r}; repoint them, "
            "or extend _STATS_CITATION_FILES' allowlist ONLY for a citation that "
            "genuinely targets a different decision"
        )

    def test_citation_pin_is_not_vacuous(self) -> None:
        """Each pinned file really carries ≥1 citation of THIS DEC's entry."""
        expected, _ = _stats_manifest_entry_line()
        for rel_path in _STATS_CITATION_FILES:
            text = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
            cited = {int(m.group("num")) for m in _LEDGER_CITATION.finditer(text)}
            assert expected in cited, (
                f"{rel_path} no longer cites the DEC-LAKE-STATS-MANIFEST-001 entry "
                f"({expected}) — the citation this pin guards was removed; update "
                "_STATS_CITATION_FILES if that was deliberate"
            )


class TestLedgerRenumberNarrationCoherent:
    """The entry's renumber record must agree with itself (ENDGAME FIX round 1).

    The refuter caught the wave-resync leaving ONE entry telling two stories:
    the header said "renumbered from" one number while the Ledger bracket's
    ``Renumbered #a → #b → ...`` chain omitted that hop entirely (a
    mechanical last-number substitution instead of an appended hop). These
    pins assert the SHAPE of coherence, not literals: the chain exists, is
    strictly increasing, ends at the entry's own number, and contains the
    header's "renumbered from" hop.
    """

    def test_renumber_chain_ends_at_the_entry_number(self) -> None:
        number, entry = _stats_manifest_entry_line()
        chains = re.findall(r"Renumbered (#\d+(?: → #\d+)+)", entry)
        assert chains, (
            "the DEC-LAKE-STATS-MANIFEST-001 entry lost its 'Renumbered #a → #b' "
            "Ledger chain — the renumber history must stay recorded"
        )
        canonical = max(chains, key=len)
        hops = [int(n) for n in re.findall(r"#(\d+)", canonical)]
        assert hops == sorted(set(hops)), (
            f"the renumber chain {canonical!r} is not strictly increasing — "
            "renumbers only ever move UP (live-max+1 / disjoint wave block)"
        )
        assert hops[-1] == number, (
            f"the renumber chain {canonical!r} does not terminate at the entry's own "
            f"number {number} — a renumber edited the header but not the chain"
        )

    def test_header_renumber_note_is_a_chain_hop(self) -> None:
        number, entry = _stats_manifest_entry_line()
        header_note = re.search(
            r"\*\*DEC-LAKE-STATS-MANIFEST-001 \(renumbered from #(?P<num>\d+)", entry
        )
        assert header_note is not None, (
            "the DEC-LAKE-STATS-MANIFEST-001 header lost its '(renumbered from #N' "
            "note — keep the header note and the Ledger chain telling ONE story"
        )
        hop = int(header_note.group("num"))
        canonical = max(re.findall(r"Renumbered (#\d+(?: → #\d+)+)", entry), key=len)
        hops = [int(n) for n in re.findall(r"#(\d+)", canonical)]
        assert hop in hops, (
            f"the header says 'renumbered from #{hop}' but the Ledger chain "
            f"{canonical!r} never passed through #{hop} — the two narrations "
            "diverged again (the exact ENDGAME-FIX-round-1 incoherence)"
        )
        assert hop != number, (
            f"the header claims the entry was renumbered from its OWN number #{hop}"
        )


class TestSection3EarlierChainShape:
    """§3's ``Earlier:`` chain must stay well-formed (ENDGAME FIX round 1).

    The reconcile merge left a bare duplicated ``Earlier:
    `claude/stoic-davinci-7c7xp0``` line in §3 — no DEC parenthetical,
    out of the chain's reverse-chronological order, duplicating the real
    entry five lines below. The §1↔§3 active-branch pin above missed it
    because it asserts only the Active lines; these pins cover the chain.
    """

    @staticmethod
    def _section3(text: str) -> str:
        start = text.index("## 3. Branching and Commits")
        end = text.index("## 4. Coding Standards", start)
        return text[start:end]

    def test_no_duplicate_earlier_branch_entries(self) -> None:
        section = self._section3(_CLAUDE_MD.read_text(encoding="utf-8"))
        names = re.findall(r"^\s*Earlier: `(?P<branch>[^`]+)`", section, re.MULTILINE)
        duplicates = sorted({name for name in names if names.count(name) > 1})
        assert not duplicates, (
            f"CLAUDE.md §3 lists the same branch twice in its Earlier chain: "
            f"{duplicates!r} — a merge artifact; keep exactly one entry per branch "
            "(the one carrying its DEC parenthetical)"
        )

    def test_every_earlier_entry_carries_its_description(self) -> None:
        section = self._section3(_CLAUDE_MD.read_text(encoding="utf-8"))
        lines = section.splitlines()
        for index, line in enumerate(lines):
            match = re.match(r"^\s*Earlier: `[^`]+`(?P<rest>.*)$", line)
            if match is None:
                continue
            if "(" in match.group("rest"):
                continue
            following = next(
                (candidate.strip() for candidate in lines[index + 1 :] if candidate.strip()),
                "",
            )
            assert following.startswith("("), (
                f"CLAUDE.md §3 Earlier entry {line.strip()!r} carries no DEC "
                "parenthetical — a bare orphan line (merge artifact); either give it "
                "its (DEC-..., MIGRATION.md #N) description or delete it"
            )
