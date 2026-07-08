#!/usr/bin/env python3
"""Citation ACCURACY scanner — the correctness upgrade of STANDARDS_100PCT.md §1 row 17.

DEC-TEST-APPARATUS-DEBTS-001(d). The sibling ``validate_no_hallucination.py``
checks that citations EXIST near external claims; this script checks that the
``file:line`` citations the normative docs actually carry are CORRECT, closing
the recurring drift class of DEC-WORKSPACE-VERIFY-002 (#609) /
DEC-CLAUDEMD-SPEC-LINE-RECONCILE-001 (#669) /
DEC-CLAUDEMD-ADAPTER-CITE-RECONCILE-001 (#846), where a doc row cited
``constants.py:112-118`` long after the symbol had moved.

Scanned documents (in-repo normative set)
-----------------------------------------
``CLAUDE.md``, ``docs/TRACK1_*.md``, ``docs/STANDARDS_100PCT.md``.
``docs/adr/*.md`` are deliberately NOT scanned as source documents — ADRs are
immutable point-in-time records; "reconciling" their internal line citations
would falsify history (DEC-P0-063-RECONCILE-COMPLETE-001 precedent).
Extra files passed as argv (e.g. a drafted PR body) are scanned as fully
normative additional documents.

Validation tiers (per citation ``<path>:<lines>``, comma lists per segment)
---------------------------------------------------------------------------
* **Tier 1** — the cited file resolves: repo-root-relative first, then a
  UNIQUE suffix match over ``git ls-files`` (``groww/constants.py`` →
  ``brutex/adapters/groww/constants.py``); an ambiguous suffix is a reported
  failure, never a guess. A deliberately abbreviated path containing ``...``
  (house shorthand, e.g. ``adr/0009-...-migration.md``) is resolved as a
  glob; unique match required.
* **Tier 2** — every cited line segment satisfies ``1 <= start <= end <=
  len(file)``.
* **Tier 3** — anchored-symbol proximity: backticked identifier spans in the
  SAME table row / paragraph as the citation (matching
  ``[A-Za-z_][A-Za-z0-9_.]*``) — at least one anchor must occur within
  ``±5`` lines of at least one cited segment. On failure the whole file is
  grepped and the ACTUAL location printed ("found at :NNN — update the
  citation") so every fix is mechanical.

Ledger references
-----------------
``MIGRATION.md [entry ]#NNN`` references are validated against the real
entry-number set parsed with ``validate_ledger_numbering.HEADER_RE`` (one
source of truth — imported, never copied). A bare ``#613`` resolves if ANY
form of 613 exists (``613``, ``613a``, ``613b`` — the renumber-suffix
convention of DEC-LEDGER-RENUMBER-001).

Tier-3 exclusions (documented scope decisions, NOT gate-narrowing)
------------------------------------------------------------------
* Citations whose TARGET is ``docs/adr/*.md`` skip Tier 3 (immutable records;
  Tier 1/2 still apply).
* CLAUDE.md's two HISTORICAL LEDGER-MIRROR regions skip Tier 3 entirely:
  (i) the header blurb — every line from ``_Last updated:`` to
  ``## 1. Project Identity``; (ii) within §1's ``Active branch:`` block,
  every line from the first ``Earlier:`` / ``Prior merged:`` marker to the
  end of that block. Those regions mirror MIGRATION.md ledger entries whose
  file:line cites are deliberately point-in-time; "reconciling" them would
  FALSIFY the historical record (the authoritative record is MIGRATION.md,
  which is never scanned for file:line). The normative §2/§6 rows stay
  fail-closed. Tier 1/2 still apply inside the mirror regions.

Residual limits (honest envelope per Golden Rule #10)
------------------------------------------------------
* Tier 3 passes on the ANY-anchor × ANY-segment disjunction: a citation is
  green as soon as ONE anchor from the row/paragraph occurs within ``±5``
  lines of ONE comma-list segment. Consequences, stated plainly: (a) a
  ≤5-line staleness always passes; (b) an ARBITRARILY stale range ALSO
  passes whenever any incidental mention of any anchor (e.g. a comment
  referencing the symbol) lands within ±5 of any segment — a multi-segment
  citation is NOT validated per segment, and a multi-anchor row is NOT
  validated per anchor. A green scan is therefore "no anchor-free drift
  detected", NOT a proof every cited line is right; per-anchor/per-segment
  validation is the named v2 ratchet.
* Citations with NO identifier anchor in their context get Tier 1/2 only
  (counted ``unanchored``, never a fake pass/fail verdict on Tier 3).
* An anchor that appears NOWHERE in the cited file is counted
  ``anchor-not-in-file`` and NOT failed: in multi-citation contexts (one
  table row citing several files) the anchor↔file assignment is ambiguous,
  and there is no "found at :NNN" line to mechanically fix toward. A fully
  RENAMED-away symbol therefore escapes Tier 3 — the doc row itself is
  expected to be rewritten when a symbol is renamed.
* Bare continuation cites (``:314`` with no path on the same match) and
  ledger refs line-wrapped between ``MIGRATION.md`` and ``#NNN`` are not
  extracted (line-oriented scan).
* Fenced code blocks are never scanned (examples are not citations).

Usage
-----
::

    python scripts/ci/validate_citation_accuracy.py [extra_doc ...]

Runs from anywhere inside the repo (root located via
``git rev-parse --show-toplevel``; suffix resolution via ``git ls-files``).

Exit codes
----------
0 — every citation accurate (or downgraded by the escape env).
1 — one or more inaccurate/unresolvable citations or ledger refs.
2 — script error (not a git repo, unreadable extra doc, ...).

False-positive escape
---------------------
``CITATION_ACCURACY_ALLOW_DRIFT=1`` reverts to warn-and-continue for
emergency operator override (requires an ADR per CLAUDE.md Golden Rule #12).
CI never sets it.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess  # nosec B404 — git invocation (root + ls-files) is the scanner's purpose
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Script-AND-module import resolution for the sibling helper — see the
# identical block in validate_dec_ids.py (DEC-TEXTSCAN-COMMON-001).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from validate_ledger_numbering import HEADER_RE

#: ``path:lines`` citation. The line part accepts COMMA-SEPARATED segment
#: lists (``constants.py:112-118,165``) — every segment is validated.
CITATION_RE = re.compile(
    r"(?P<path>[A-Za-z0-9_./-]+\.(?:py|rs|md|sh|toml|ya?ml|html|js))"
    r":(?P<lines>\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)"
)

#: ``MIGRATION.md entry #NNN`` / ``MIGRATION.md #NNN`` ledger reference.
MIGRATION_REF_RE = re.compile(r"MIGRATION\.md\s+(?:entry\s*)?#(\d+)")

#: A backticked span whose leading run matches this is a Tier-3 anchor.
ANCHOR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")

#: Minimum anchor length — keeps 2-3 char noise tokens (``EQ``, ``IDX``,
#: ``NSE``) from trivially satisfying Tier 3 everywhere.
MIN_ANCHOR_LEN = 4

#: Tier-3 proximity slack in lines, each side of every cited segment.
ANCHOR_SLACK = 5

ESCAPE_ENV = "CITATION_ACCURACY_ALLOW_DRIFT"

#: Number-only SUPPLEMENT to the imported HEADER_RE for harvesting ledger
#: entry NUMBERS. HEADER_RE requires ``DEC-[A-Z]...`` after the bold marker,
#: which misses the digit-initial DEC-ID class — the live ledger's
#: ``853. **DEC-1MINUTE-ONLY-DERIVE-1DAY-001`` parses as NO entry, so a
#: correct ``MIGRATION.md #853`` citation would false-fail. HEADER_RE stays
#: the primary (imported, never copied — one header definition); this
#: fallback harvests ONLY the entry number, and widening HEADER_RE itself in
#: validate_ledger_numbering.py is the named upstream follow-up.
_HEADER_NUMBER_FALLBACK_RE = re.compile(r"^(\d{1,5})[a-z]?\. \*\*DEC-")


@dataclass(frozen=True)
class Citation:
    """One extracted ``path:lines`` citation with its anchor context."""

    doc: str
    line_no: int
    raw_path: str
    raw_lines: str
    segments: tuple[tuple[int, int], ...]
    context: str


@dataclass
class ScanStats:
    """Aggregate counters for the honest summary line."""

    docs: int = 0
    citations: int = 0
    ledger_refs: int = 0
    anchored_pass: int = 0
    unanchored: int = 0
    anchor_not_in_file: int = 0
    tier3_mirror_skipped: int = 0
    tier3_adr_skipped: int = 0
    ellipsis_resolved: int = 0
    failures: list[str] = field(default_factory=list)


def _git(root: Path | None, *args: str) -> str:
    """Run a git command and return stdout (raises on failure)."""
    proc = subprocess.run(  # noqa: S603  # nosec B603 B607 — fixed argv, no shell
        ["git", *args],  # noqa: S607 — PATH-resolved git, matching sibling scanners
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return proc.stdout


def parse_segments(raw_lines: str) -> tuple[tuple[int, int], ...]:
    """Parse ``112-118,165`` into ``((112, 118), (165, 165))``."""
    segments: list[tuple[int, int]] = []
    for part in raw_lines.split(","):
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            segments.append((int(start_s), int(end_s)))
        else:
            segments.append((int(part), int(part)))
    return tuple(segments)


def fence_mask(lines: list[str]) -> list[bool]:
    """Per-line True = inside (or delimiting) a fenced code block."""
    mask: list[bool] = []
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            mask.append(True)
            in_fence = not in_fence
        else:
            mask.append(in_fence)
    return mask


def citation_context(lines: list[str], mask: list[bool], idx: int) -> str:
    """The anchor context for a citation on 0-based line *idx*.

    A markdown table row is its OWN context (anchors legitimately live in
    sibling cells of the same row); prose expands to the blank-line-delimited
    paragraph, never crossing a fence boundary.
    """
    if lines[idx].lstrip().startswith("|"):
        return lines[idx]
    start = idx
    while start > 0 and lines[start - 1].strip() and not mask[start - 1]:
        start -= 1
    end = idx
    while end + 1 < len(lines) and lines[end + 1].strip() and not mask[end + 1]:
        end += 1
    return "\n".join(lines[start : end + 1])


def extract_anchors(context: str) -> list[str]:
    """Backticked identifier anchors in *context* (paths/citations excluded)."""
    anchors: list[str] = []
    for span in re.findall(r"`([^`]+)`", context):
        if "/" in span or CITATION_RE.search(span):
            continue  # a path or another citation is never an anchor
        m = ANCHOR_RE.match(span)
        if m is None:
            continue
        token = m.group(0).rstrip(".")
        if len(token) >= MIN_ANCHOR_LEN and token not in anchors:
            anchors.append(token)
    return anchors


def anchor_variants(anchor: str) -> tuple[str, ...]:
    """Search needles for *anchor* — itself plus a dotted-path last component."""
    variants = [anchor]
    if "." in anchor:
        last = anchor.rsplit(".", 1)[-1]
        if len(last) >= MIN_ANCHOR_LEN and last not in variants:
            variants.append(last)
    return tuple(variants)


def claude_md_mirror_lines(lines: list[str]) -> set[int]:
    """1-based line numbers of CLAUDE.md's two Tier-3-excluded mirror regions.

    Region (i): ``_Last updated:`` header blurb up to ``## 1. Project
    Identity``. Region (ii): within §1's ``Active branch:`` block, from the
    first ``Earlier:`` / ``Prior merged:`` marker to the end of that block.
    See the module docstring for the falsify-the-historical-record rationale.
    """
    excluded: set[int] = set()
    n = len(lines)

    # Region (i) — header ledger mirror.
    header_start = next(
        (i for i, ln in enumerate(lines, 1) if ln.startswith("_Last updated")), None
    )
    if header_start is not None:
        header_end = next(
            (i for i, ln in enumerate(lines, 1) if i > header_start and ln.startswith("## ")),
            n + 1,
        )
        excluded.update(range(header_start, header_end))

    # Region (ii) — §1 Active-branch historical blurbs.
    sec1 = next((i for i, ln in enumerate(lines, 1) if re.match(r"^## 1\.", ln)), None)
    if sec1 is None:
        return excluded
    sec1_end = next(
        (i for i, ln in enumerate(lines, 1) if i > sec1 and ln.startswith("## ")), n + 1
    )
    block_start = next(
        (i for i in range(sec1 + 1, sec1_end) if lines[i - 1].startswith("- **Active branch:**")),
        None,
    )
    if block_start is None:
        return excluded
    block_end = next(
        (i for i in range(block_start + 1, sec1_end) if lines[i - 1].startswith(("- ", "---"))),
        sec1_end,
    )
    marker = next(
        (
            i
            for i in range(block_start, block_end)
            if "Earlier:" in lines[i - 1] or "Prior merged:" in lines[i - 1]
        ),
        None,
    )
    if marker is not None:
        excluded.update(range(marker, block_end))
    return excluded


class PathResolver:
    """Resolve cited paths repo-root-relative first, then by unique suffix."""

    def __init__(self, root: Path, tracked: list[str]) -> None:
        self.root = root
        self.tracked = tracked
        self.tracked_set = set(tracked)

    def resolve(self, raw: str, stats: ScanStats) -> tuple[str | None, str | None]:
        """Return ``(resolved_relpath, error)`` — exactly one is non-None."""
        if "..." in raw:
            pattern = raw.replace("...", "*")
            matches = sorted(
                t
                for t in self.tracked
                if fnmatch.fnmatch(t, pattern) or fnmatch.fnmatch(t, "*/" + pattern)
            )
            if len(matches) == 1:
                stats.ellipsis_resolved += 1
                return matches[0], None
            if not matches:
                return None, f"abbreviated path {raw!r} matches no tracked file"
            return (
                None,
                f"abbreviated path {raw!r} is ambiguous: " + ", ".join(matches[:5]),
            )
        if raw in self.tracked_set:
            return raw, None
        candidate = self.root / raw
        if candidate.is_file():
            # Containment gate: CITATION_RE admits `.` and `/`, so `../x.py`
            # (and an absolute `/abs/x.py`, which pathlib lets OVERRIDE root)
            # would otherwise resolve, read, and "validate" a NON-repo file —
            # the exact hallucinated-citation class this gate exists to catch
            # (plus an out-of-tree read the scanner has no business doing).
            try:
                inside = candidate.resolve().is_relative_to(self.root.resolve())
            except OSError:
                inside = False
            if inside:
                return raw, None
            return (
                None,
                f"path {raw!r} resolves OUTSIDE the repository root — "
                "normative docs may only cite in-repo files",
            )
        suffix_matches = sorted(t for t in self.tracked if t.endswith("/" + raw))
        if len(suffix_matches) == 1:
            return suffix_matches[0], None
        if not suffix_matches:
            return None, "file not found (repo-root-relative or unique tracked suffix)"
        return (
            None,
            "suffix is AMBIGUOUS — cite a longer path; candidates: "
            + ", ".join(suffix_matches[:5]),
        )


def load_ledger_numbers(ledger: Path) -> set[int] | None:
    """Entry-number set from MIGRATION.md via the imported HEADER_RE, or None."""
    try:
        text = ledger.read_text(encoding="utf-8")
    except OSError:
        return None
    numbers: set[int] = set()
    for line in text.splitlines():
        m = HEADER_RE.match(line) or _HEADER_NUMBER_FALLBACK_RE.match(line)
        if m is not None:
            numbers.add(int(m.group(1)))
    return numbers


def _preferred_hits(file_lines: list[str], needle: str, hits: list[int]) -> list[int]:
    """Up to 3 hit lines for the mechanical-fix hint, DEFINITION lines first.

    A symbol mentioned in 40 comments would otherwise bury the one
    ``class``/``def``/assignment line the citation should point at.
    """
    def_re = re.compile(
        rf"^\s*(?:class\s+{re.escape(needle)}\b|def\s+{re.escape(needle)}\b"
        rf"|{re.escape(needle)}\s*[:=])"
    )
    definitions = [ln for ln in hits if def_re.match(file_lines[ln - 1])]
    ordered = definitions + [ln for ln in hits if ln not in definitions]
    return ordered[:3]


def _check_tier3(
    citation: Citation,
    file_lines: list[str],
    stats: ScanStats,
) -> str | None:
    """Tier-3 anchored-symbol check. Returns a failure detail or None."""
    anchors = extract_anchors(citation.context)
    if not anchors:
        stats.unanchored += 1
        return None
    needles: list[str] = []
    for anchor in anchors:
        for variant in anchor_variants(anchor):
            if variant not in needles:
                needles.append(variant)
    hits: dict[str, list[int]] = {}
    for i, line in enumerate(file_lines, 1):
        for needle in needles:
            if needle in line:
                hits.setdefault(needle, []).append(i)
    if not hits:
        stats.anchor_not_in_file += 1
        return None
    for hit_lines in hits.values():
        for hit in hit_lines:
            for start, end in citation.segments:
                if start - ANCHOR_SLACK <= hit <= end + ANCHOR_SLACK:
                    stats.anchored_pass += 1
                    return None
    found = "; ".join(
        f"{needle} at :{','.join(str(ln) for ln in _preferred_hits(file_lines, needle, lines_))}"
        for needle, lines_ in sorted(hits.items())
    )
    return (
        f"no anchor within ±{ANCHOR_SLACK} of cited :{citation.raw_lines} "
        f"(anchors: {', '.join(anchors)}) — found {found}; update the citation"
    )


def scan_doc(
    doc: Path,
    doc_label: str,
    resolver: PathResolver,
    ledger_numbers: set[int] | None,
    stats: ScanStats,
    file_cache: dict[str, list[str] | None],
) -> None:
    """Scan one document, appending failures to *stats*."""
    text = doc.read_text(encoding="utf-8")
    lines = text.splitlines()
    mask = fence_mask(lines)
    mirror: set[int] = set()
    if doc_label == "CLAUDE.md":
        mirror = claude_md_mirror_lines(lines)
    stats.docs += 1

    for idx, line in enumerate(lines):
        if mask[idx]:
            continue  # fenced examples are not citations
        line_no = idx + 1

        for ref in MIGRATION_REF_RE.finditer(line):
            stats.ledger_refs += 1
            number = int(ref.group(1))
            if ledger_numbers is None:
                stats.failures.append(
                    f"{doc_label}:{line_no}: cites MIGRATION.md #{number} but "
                    "docs/MIGRATION.md was not found"
                )
            elif number not in ledger_numbers:
                stats.failures.append(
                    f"{doc_label}:{line_no}: cites MIGRATION.md #{number} — no ledger "
                    "entry with that number exists (bare numbers also match "
                    "suffixed forms like 613a)"
                )

        for m in CITATION_RE.finditer(line):
            stats.citations += 1
            raw_path, raw_lines = m.group("path"), m.group("lines")
            where = f"{doc_label}:{line_no}: cited {raw_path}:{raw_lines}"

            resolved, err = resolver.resolve(raw_path, stats)
            if resolved is None:
                stats.failures.append(f"{where} — {err}")
                continue

            if resolved not in file_cache:
                try:
                    file_cache[resolved] = (
                        (resolver.root / resolved)
                        .read_text(encoding="utf-8", errors="replace")
                        .splitlines()
                    )
                except OSError:
                    file_cache[resolved] = None
            file_lines = file_cache[resolved]
            if file_lines is None:
                stats.failures.append(f"{where} — resolved {resolved} is unreadable")
                continue

            segments = parse_segments(raw_lines)
            tier2_bad = [(s, e) for s, e in segments if not (1 <= s <= e <= len(file_lines))]
            if tier2_bad:
                bad = ", ".join(f"{s}-{e}" if s != e else str(s) for s, e in tier2_bad)
                stats.failures.append(
                    f"{where} — line segment(s) {bad} out of range "
                    f"({resolved} has {len(file_lines)} lines)"
                )
                continue

            if line_no in mirror:
                stats.tier3_mirror_skipped += 1
                continue
            if resolved.startswith("docs/adr/"):
                stats.tier3_adr_skipped += 1
                continue

            citation = Citation(
                doc=doc_label,
                line_no=line_no,
                raw_path=raw_path,
                raw_lines=raw_lines,
                segments=segments,
                context=citation_context(lines, mask, idx),
            )
            detail = _check_tier3(citation, file_lines, stats)
            if detail is not None:
                stats.failures.append(f"{where} — {detail}")


def default_docs(root: Path) -> list[Path]:
    """The in-repo normative doc set (only those that exist)."""
    docs: list[Path] = []
    claude = root / "CLAUDE.md"
    if claude.is_file():
        docs.append(claude)
    docs.extend(sorted((root / "docs").glob("TRACK1_*.md")))
    standards = root / "docs" / "STANDARDS_100PCT.md"
    if standards.is_file():
        docs.append(standards)
    return docs


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — see module docstring for the contract."""
    args = argv if argv is not None else sys.argv

    try:
        root = Path(_git(None, "rev-parse", "--show-toplevel").strip())
        tracked = _git(root, "ls-files").splitlines()
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"ERROR: git repo discovery failed: {exc}", file=sys.stderr)
        return 2

    docs: list[tuple[Path, str]] = [(doc, str(doc.relative_to(root))) for doc in default_docs(root)]
    for extra in args[1:]:
        extra_path = Path(extra)
        if not extra_path.is_file():
            print(f"ERROR: extra document not readable: {extra}", file=sys.stderr)
            return 2
        docs.append((extra_path, extra))

    if not docs:
        print("ℹ️  no documents to scan")
        return 0

    resolver = PathResolver(root, tracked)
    ledger_numbers = load_ledger_numbers(root / "docs" / "MIGRATION.md")
    stats = ScanStats()
    file_cache: dict[str, list[str] | None] = {}

    for doc, doc_label in docs:
        try:
            scan_doc(doc, doc_label, resolver, ledger_numbers, stats, file_cache)
        except OSError as exc:
            print(f"ERROR: cannot read {doc_label}: {exc}", file=sys.stderr)
            return 2

    print(
        f"citation accuracy: {stats.docs} doc(s), {stats.citations} citation(s), "
        f"{stats.ledger_refs} ledger ref(s) — {stats.anchored_pass} anchored-pass, "
        f"{stats.unanchored} unanchored (Tier 1/2 only), "
        f"{stats.anchor_not_in_file} anchor-not-in-file (counted, see docstring), "
        f"{stats.tier3_mirror_skipped} mirror-region Tier-3 skips, "
        f"{stats.tier3_adr_skipped} ADR-target Tier-3 skips, "
        f"{stats.ellipsis_resolved} abbreviated path(s) glob-resolved"
    )
    if not stats.failures:
        print("✅ every extracted citation is accurate")
        return 0

    stream = sys.stderr
    print(f"❌ {len(stats.failures)} inaccurate citation(s):", file=stream)
    for failure in stats.failures:
        print(f"  - {failure}", file=stream)
    print(
        "\nFix: update each citation to the printed 'found at :NNN' location "
        "(or the real file path); CLAUDE.md's historical ledger-mirror regions "
        "and docs/adr/ are already excluded from Tier 3 by design — do NOT "
        "rewrite history to satisfy this gate.",
        file=stream,
    )
    if os.environ.get(ESCAPE_ENV) == "1":
        print(
            f"WARNING: {ESCAPE_ENV}=1 override active — NOT failing (ADR required).",
            file=stream,
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
