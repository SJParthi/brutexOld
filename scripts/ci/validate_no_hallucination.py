#!/usr/bin/env python3
"""No-hallucination citation scanner — partial automation of STANDARDS_100PCT.md §1 row 17.

Closes the meta-gap that "every claim about external systems must be
traceable to a verified source" (Golden Rule #1 in CLAUDE.md) was
👁️ MANUAL with no automation. While full enforcement (the citation
must be CORRECT, not just present) requires reviewer judgement,
this script catches the most common failure mode:

* PR body mentions an external system (Dhan / NSE / BSE / SEBI /
  brokerage / API / rate limit) WITHOUT a nearby citation.

A "citation" is one of:

* A URL (``https://...`` or ``http://...``)
* An ISO-8601-ish date with "verified" nearby (e.g., ``verified 2026-05-15``)
* A reference to a tracked doc (``CLAUDE.md §6``, ``MIGRATION.md entry #NNN``,
  ``TRACK1_*.md §...``)
* A primary-source phrase (``dhanhq.co/docs``, ``nseindia.com``)

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

# Sentences containing these keywords are scrutinised for nearby citations.
# These are the external systems where Golden Rule #1 hallucination risk
# is highest (per CLAUDE.md §6 verified-facts table).
EXTERNAL_CLAIM_KEYWORDS = re.compile(
    r"\b("
    r"dhan(?:hq)?|"
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
    re.compile(r"dhanhq\.co/docs", re.IGNORECASE),
    re.compile(r"nseindia\.com", re.IGNORECASE),
    # Decision IDs (DEC-FOO-NNN) also count — those resolve to ledger entries.
    re.compile(r"\bDEC-[A-Z][A-Z0-9_-]*-\d{3,}\b"),
    # Source-attribution words right next to a claim.
    re.compile(r"\bper\s+(the\s+)?dhanhq?", re.IGNORECASE),
    re.compile(r"\bcited\s+(in|at)\b", re.IGNORECASE),
]


def strip_code_spans(text: str) -> str:
    """Remove fenced + inline code spans so example claims inside
    backticks don't trip the scanner.
    """
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", " ", text)
    return text


def split_into_paragraphs(text: str) -> list[str]:
    """Split text into paragraphs (blocks separated by blank lines)."""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def has_citation(paragraph: str) -> bool:
    """Return True if any CITATION_PATTERNS matches in the paragraph."""
    return any(p.search(paragraph) for p in CITATION_PATTERNS)


def find_uncited_external_claims(text: str) -> list[str]:
    """Return paragraphs that mention external systems but lack a citation.

    A paragraph is "uncited" if the STRIPPED version (code spans removed)
    contains an EXTERNAL_CLAIM_KEYWORDS match AND the ORIGINAL version
    contains no CITATION_PATTERNS match.

    Citations are checked on the ORIGINAL text (not stripped) because
    citation markers themselves are often inside backticks — e.g.,
    ``per `CLAUDE.md §6` `` legitimately wraps the citation as inline
    code, but the citation IS a citation regardless of its formatting.
    Keywords are checked on the STRIPPED text so example claims inside
    backticks (``the scanner looks for `dhan` keyword``) don't trigger.
    """
    paragraphs_original = split_into_paragraphs(text)
    paragraphs_stripped = split_into_paragraphs(strip_code_spans(text))
    # Pair them by index (split_into_paragraphs is deterministic — both
    # versions produce the same paragraph count + order).
    uncited: list[str] = []
    for original, stripped in zip(paragraphs_original, paragraphs_stripped, strict=False):
        if EXTERNAL_CLAIM_KEYWORDS.search(stripped) and not has_citation(original):
            uncited.append(original[:200])
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
        "(Dhan / NSE / BSE / etc.) without a nearby citation. "
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
        "  - Primary source (dhanhq.co/docs, nseindia.com)\n"
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
