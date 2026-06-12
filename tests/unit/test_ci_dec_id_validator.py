"""Regression tests for the DEC-ID PR-body validator (CI gate, row 18).

DEC-TEXTSCAN-COMMON-001: ``scripts/ci/validate_dec_ids.py`` had no
dedicated test file — its behaviour was only exercised indirectly by
live PR bodies. These tests invoke the REAL script as a subprocess
(exactly as the CI job does, and exactly like the sibling
``test_ci_no_hallucination_scanner.py``) and pin both directions plus
the code-span exclusions, so the consolidation of the span-stripping
helpers into ``scripts/ci/_textscan.py`` is regression-locked.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "validate_dec_ids.py"


def _scan(body: str, tmp_path: Path) -> int:
    """Run the real validator over *body* and return its exit code."""
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


def test_real_dec_id_resolves(tmp_path: Path) -> None:
    """A DEC-ID with a ledger entry passes."""
    assert _scan("Per DEC-COV-001 the coverage arc is tracked.", tmp_path) == 0


def test_bogus_dec_id_is_blocked(tmp_path: Path) -> None:
    """A DEC-ID without a ledger entry fails closed."""
    assert _scan("Per DEC-NONEXISTENT-999 this should block.", tmp_path) == 1


def test_inline_code_dec_id_is_ignored(tmp_path: Path) -> None:
    """An example ID inside inline backticks is documentation, not a citation."""
    assert _scan("The validator rejects e.g. `DEC-NONEXISTENT-999`.", tmp_path) == 0


def test_fenced_block_dec_id_is_ignored(tmp_path: Path) -> None:
    """An example ID inside a fenced block is documentation, not a citation."""
    body = "Sample output:\n\n```\nmissing: DEC-NONEXISTENT-999\n```\n"
    assert _scan(body, tmp_path) == 0


def test_body_without_dec_ids_passes(tmp_path: Path) -> None:
    """A body citing nothing has nothing to validate."""
    assert _scan("Pure internal refactor of the results store.", tmp_path) == 0
