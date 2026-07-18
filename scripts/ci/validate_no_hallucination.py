#!/usr/bin/env python3
"""No-hallucination citation scanner — partial automation of STANDARDS_100PCT.md §1 row 17.

Closes the meta-gap that "every claim about external systems must be
traceable to a verified source" (Golden Rule #1 in CLAUDE.md) was
👁️ MANUAL with no automation. While full enforcement (the citation
must be CORRECT, not just present) requires reviewer judgement,
this script catches the most common failure mode:

* PR body mentions an external system (Groww / NSE / BSE / SEBI /
  brokerage / API / rate limit) WITHOUT a nearby citation.

A "citation" is one of:

* A URL (``https://...`` or ``http://...``)
* An ISO-8601-ish date with "verified" nearby (e.g., ``verified 2026-05-15``)
* A reference to a tracked doc (``CLAUDE.md §6``, ``MIGRATION.md entry #NNN``,
  ``TRACK1_*.md §...``)
* A primary-source phrase (``groww.in/trade-api/docs/docs``, ``nseindia.com``)

Usage
-----
::

    python scripts/ci/validate_no_hallucination.py [pr_body_file]

If ``pr_body_file`` is omitted, reads PR body from stdin (the CI
workflow pipes ``${{ github.event.pull_request.body }}`` in).

Exit codes
----------
0 — every external-claim sentence has a nearby citation marker,
    OR the PR body has no external claims (doc-only / config PRs).
1 — one or more sentences mention an external system without a
    citation marker.
2 — script error.

Heuristic scope (honest envelope per Golden Rule #10)
------------------------------------------------------
This script does NOT verify that citations are CORRECT — only that
they EXIST near the claim. False-positives are inevitable (e.g., a
sentence that legitimately doesn't need a citation because the
claim is about internal code, not an external system). Reviewer
judgement remains the final layer.

False-positive escape
---------------------
``HALLUCINATION_SCAN_ALLOW_MISSING=1`` reverts to warn-and-continue.
CI never sets this; reviewer signs off via ADR.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Script-AND-module import resolution for the shared helper — see the
# identical block in validate_dec_ids.py (DEC-TEXTSCAN-COMMON-001).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _textscan import strip_fenced_blocks, strip_inline_code

# Sentences containing these keywords are scrutinised for nearby citations.
# These are the external systems where Golden Rule #1 hallucination risk
# is highest (per CLAUDE.md §6 verified-facts table).
EXTERNAL_CLAIM_KEYWORDS = re.compile(
    r"\b("
    r"groww(?:hq)?|"
    r"nse(?:india)?|"
    r"bse(?:india)?|"
    r"sebi|"
    r"rbi|"
    r"brokerage|"
    r"stamp\s+duty|"
    r"securityid|"
    r"security[_\s]id|"
    r"rate[_\s]limit|"
    r"exchange[_\s]segment|"
    r"renaissance|"
    r"two\s+sigma|"
    r"aqr|"
    r"de[_\s]?shaw|"
    r"jane\s+street|"
    r"citadel"
    r")\b",
    re.IGNORECASE,
)

# Citation markers — presence of ANY of these near a claim is enough.
# Each pattern is a substring or anchored regex.
CITATION_PATTERNS = [
    re.compile(r"https?://"),
    re.compile(r"\bverified\s+\d{4}-\d{2}-\d{2}", re.IGNORECASE),
    re.compile(r"\bverified\b.*\d{4}-\d{2}-\d{2}", re.IGNORECASE),
    re.compile(r"CLAUDE\.md\s*§", re.IGNORECASE),
    re.compile(r"MIGRATION\.md\s+(?:entry\s*)?#\d+", re.IGNORECASE),
    re.compile(r"TRACK[12]_[A-Z_]+\.md", re.IGNORECASE),
    re.compile(r"STANDARDS_100PCT\.md\s*§", re.IGNORECASE),
    re.compile(r"groww\.in/trade-api/docs", re.IGNORECASE),
    # Groww is the LIVE vendor (DEC-DHAN-GROWW-REMIGRATE-001 / ADR-0009): a
    # reference to the operator-supplied Groww API docs counts as a citation.
    re.compile(r"docs/groww/api-reference/", re.IGNORECASE),
    # 2026-07-18 CLAUDE.md split: the old single-file sections now live
    # verbatim under docs/claude/NN-topic.md — a reference to one of those
    # canonical homes counts as a citation.
    re.compile(r"docs/claude/\d{2}-[a-z0-9-]+\.md", re.IGNORECASE),
    re.compile(r"nseindia\.com", re.IGNORECASE),
    # Decision IDs (DEC-FOO-NNN) also count — those resolve to ledger entries.
    re.compile(r"\bDEC-[A-Z][A-Z0-9_-]*-\d{3,}\b"),
    # Source-attribution words right next to a claim.
    re.compile(r"\bper\s+(the\s+)?groww", re.IGNORECASE),
    re.compile(r"\bcited\s+(in|at)\b", re.IGNORECASE),
]


def split_into_paragraphs(text: str) -> list[str]:
    """Split text into paragraphs (blocks separated by blank lines)."""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def has_citation(paragraph: str) -> bool:
    """Return True if any CITATION_PATTERNS matches in the paragraph."""
    return any(p.search(paragraph) for p in CITATION_PATTERNS)


def find_uncited_external_claims(text: str) -> list[str]:
    """Return paragraphs that mention external systems but lack a citation.

    Fenced code blocks are removed FIRST, then the text is split into
    paragraphs ONCE, and each paragraph is checked against itself:
    keywords on the inline-code-stripped view, citations on the
    paragraph as written. Splitting a single fence-free text is what
    guarantees the keyword view and the citation view describe the SAME
    paragraph (DEC-SCANNER-PARAGRAPH-ALIGN-001 — the previous
    implementation split the original and the stripped text separately
    and zipped BY INDEX; deleting a fenced block removed a whole
    paragraph from the stripped list and shifted every later pairing,
    so a keyword paragraph could be "checked" against an unrelated
    original — proven live on PR #628, where the flagged paragraph was
    a bare section header).

    Citations inside INLINE backticks still count (``per `CLAUDE.md §6` ``
    wraps the citation as code, but it IS a citation). Citations inside
    FENCED blocks do NOT count — a quoted log or example cannot satisfy
    the citation duty for prose claims; quote logs as inline code or cite
    in prose. Keywords inside any code span never trigger.
    """
    uncited: list[str] = []
    for paragraph in split_into_paragraphs(strip_fenced_blocks(text)):
        keyword_view = strip_inline_code(paragraph)
        if EXTERNAL_CLAIM_KEYWORDS.search(keyword_view) and not has_citation(paragraph):
            uncited.append(paragraph[:200])
    return uncited


def read_pr_body(args: list[str]) -> str:
    """Read PR body from a file path (if given) or stdin."""
    if len(args) >= 2:
        return Path(args[1]).read_text(encoding="utf-8")
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on PASS, 1 on uncited claims, 2 on script error."""
    argv = argv if argv is not None else sys.argv

    try:
        body = read_pr_body(argv)
    except OSError as exc:
        print(f"⚠️  could not read PR body: {exc}", file=sys.stderr)
        return 2

    if not body.strip():
        print("ℹ️  PR body empty — no claims to scan")
        return 0

    uncited = find_uncited_external_claims(body)

    if not uncited:
        print("✅ every external-system claim in the PR body has a nearby citation")
        return 0

    # Uncited external claims found. Fail-closed unless override is set.
    if os.environ.get("HALLUCINATION_SCAN_ALLOW_MISSING") == "1":
        for snippet in uncited:
            print(
                f"⚠️  uncited external-system claim: {snippet!r} "
                "[HALLUCINATION_SCAN_ALLOW_MISSING=1, not failing]",
                file=sys.stderr,
            )
        return 0

    print(
        "❌ MERGE BLOCKED — the PR body mentions external systems "
        "(Groww / NSE / BSE / etc.) without a nearby citation. "
        "Per CLAUDE.md Golden Rule #1 (no hallucination): every factual "
        "claim about external systems must be traceable to a verified "
        "source.\n"
        "\n"
        "Uncited paragraphs (first 200 chars each):",
        file=sys.stderr,
    )
    for snippet in uncited:
        print(f"  - {snippet}", file=sys.stderr)
    print(
        "\nFix: add ONE of these citation markers near the claim:\n"
        "  - A URL (https://...)\n"
        "  - 'verified YYYY-MM-DD' phrase\n"
        "  - Reference to CLAUDE.md §N or MIGRATION.md entry #NNN\n"
        "  - Reference to TRACK1_*.md / STANDARDS_100PCT.md\n"
        "  - A DEC-FOO-NNN identifier resolving to a ledger entry\n"
        "  - Primary source (groww.in/trade-api/docs/docs, nseindia.com)\n"
        "\nOR re-run with HALLUCINATION_SCAN_ALLOW_MISSING=1 + ADR "
        "if the false-positive is legitimate (e.g., a claim about INTERNAL "
        "BruteX code that happens to mention 'rate_limit' as an internal "
        "variable name).\n"
        "\nSee STANDARDS_100PCT.md §1 row 17 (partial automation via "
        "scripts/ci/validate_no_hallucination.py).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
