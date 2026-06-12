"""Regression tests for the no-hallucination PR-body scanner (CI gate, row 17).

DEC-HONESTY-AUDIT-RECONCILE-001: the 2026-06-11 honesty audit found
``scripts/ci/validate_no_hallucination.py`` still keyed to Groww after the
vendor revert (`DEC-GROWW-DHAN-REVERT-001`) — an uncited claim about Dhan, the
LIVE vendor, passed the gate unscanned. These tests invoke the REAL script as
a subprocess (exactly as the CI job does) and pin both directions for both
vendors, so the keyword list can never silently lose the live vendor again.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "validate_no_hallucination.py"


def _scan(body: str, tmp_path: Path) -> int:
    """Run the real scanner over *body* and return its exit code."""
    body_file = tmp_path / "pr-body.md"
    body_file.write_text(body, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), str(body_file)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return result.returncode


class TestDhanCoverage:
    def test_uncited_dhan_claim_is_blocked(self, tmp_path: Path) -> None:
        # THE regression: before DEC-HONESTY-AUDIT-RECONCILE-001 the keyword
        # list had no 'dhan', so this exact body passed the gate unscanned.
        assert _scan("The Dhan rate limit is 5 requests per second.", tmp_path) == 1

    def test_dhanhq_variant_is_blocked(self, tmp_path: Path) -> None:
        assert _scan("DhanHQ returns columnar candle arrays.", tmp_path) == 1

    def test_cited_dhan_claim_passes(self, tmp_path: Path) -> None:
        body = (
            "The Dhan rate limit is 5 r/s per "
            "docs/dhan-ref/01-introduction-and-rate-limits.md "
            "(verified 2026-05-31, DEC-GROWW-DHAN-REVERT-001)."
        )
        assert _scan(body, tmp_path) == 0

    def test_api_dhan_co_counts_as_a_citation(self, tmp_path: Path) -> None:
        assert _scan("Dhan serves intraday candles at api.dhan.co endpoints.", tmp_path) == 0


class TestHistoricalGrowwCoverage:
    def test_uncited_groww_claim_still_blocked(self, tmp_path: Path) -> None:
        # The Groww keyword is deliberately KEPT so historical PR bodies
        # remain scannable after the vendor revert.
        assert _scan("Groww charges zero brokerage on delivery trades.", tmp_path) == 1

    def test_clean_body_passes(self, tmp_path: Path) -> None:
        assert _scan("Pure internal refactor of the results store.", tmp_path) == 0


class TestFencedBlockAlignment:
    """DEC-SCANNER-PARAGRAPH-ALIGN-001: fenced blocks must not shift the
    keyword-paragraph / citation-paragraph pairing.

    The old implementation split the original and the code-stripped text
    SEPARATELY and zipped by index; deleting a fenced block removed a
    whole paragraph from the stripped list, so every later keyword
    paragraph was citation-checked against the WRONG original. Proven
    live on PR #628: a fully-cited body was blocked with the "uncited"
    finding pointing at a bare section header.
    """

    def test_pr628_shape_cited_body_after_fence_passes(self, tmp_path: Path) -> None:
        # THE regression: a fenced paragraph followed by a CITED Dhan
        # paragraph and an innocent header. Pre-fix exit code was 1 with
        # the header flagged; the body is fully cited and must pass.
        body = (
            "The validator output was:\n\n"
            "```\nentry number 667 is claimed 2x\n```\n\n"
            "The Dhan rate limit is 5 r/s per CLAUDE.md §6 "
            "(DEC-GROWW-DHAN-REVERT-001).\n\n"
            "## Out of scope\n\n"
            "- Nothing else changes.\n"
        )
        assert _scan(body, tmp_path) == 0

    def test_uncited_claim_after_fence_is_still_blocked(self, tmp_path: Path) -> None:
        # The fix must not weaken detection: the same shape WITHOUT a
        # citation stays blocked.
        body = (
            "The validator output was:\n\n"
            "```\nentry number 667 is claimed 2x\n```\n\n"
            "The Dhan rate limit is 5 requests per second.\n"
        )
        assert _scan(body, tmp_path) == 1

    def test_keywords_inside_fence_do_not_trigger(self, tmp_path: Path) -> None:
        body = (
            "Quoted scanner example follows.\n\n"
            "```\nGroww rate limit exceeded\n```\n\n"
            "Internal refactor only.\n"
        )
        assert _scan(body, tmp_path) == 0

    def test_multi_paragraph_fence_does_not_leak_keywords(self, tmp_path: Path) -> None:
        # A fence CONTAINING blank lines spans multiple "paragraphs"; it
        # must be removed before splitting or its inner lines would be
        # keyword-scanned as prose.
        body = (
            "Quoted log:\n\n"
            "```\nGroww request 1\n\nGroww request 2\n```\n\n"
            "Internal refactor only.\n"
        )
        assert _scan(body, tmp_path) == 0

    def test_inline_code_citation_still_counts(self, tmp_path: Path) -> None:
        # Citations wrapped as inline code remain citations.
        assert _scan("The Dhan daily quota is 100k per `CLAUDE.md §6`.", tmp_path) == 0

    def test_fenced_citation_no_longer_satisfies_prose_claim(self, tmp_path: Path) -> None:
        # Deliberate behaviour pin: a citation that exists ONLY inside a
        # fenced block cannot satisfy the citation duty for prose claims
        # (DEC-SCANNER-PARAGRAPH-ALIGN-001).
        body = "The Dhan rate limit is 5 requests per second.\n\n```\nsee CLAUDE.md §6\n```\n"
        assert _scan(body, tmp_path) == 1
