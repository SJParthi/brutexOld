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
