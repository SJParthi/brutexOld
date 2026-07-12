"""Generate the frozen ``bxsnap2`` parity golden (``bxsnap2_parity_golden.json``).

DEC-BXSNAP2-PARITY-GOLDEN-001 (MIGRATION.md #1695). The OBSERVATIONAL parity
oracle for a FUTURE Rust ``arrow_table_to_bars`` port — see
``tests/golden/_bxsnap2_parity/__init__.py`` for the full rationale.

This script runs each :data:`~tests.golden._bxsnap2_parity.DIGEST_CASES` case
through the CURRENT Python path (``arrow_table_to_bars`` → ``list[CandleBar]`` →
``data_snapshot_hash``) and writes the captured digest + metadata to the frozen
fixture. It changes NO production behaviour; it only reads the current path.

Determinism: every case uses fixed IST/UTC timestamps + literal float prices, so
the captured hex is reproducible byte-for-byte on any platform.

Regenerate ONLY when the Python bar-materialization path *intentionally* changes
(a new decision-ID + a MIGRATION.md note that every dedup/cache run identity
goes cold — the v1→v2 ``bxsnap2`` precedent)::

    uv run python tests/golden/generate_bxsnap2_parity_golden.py

The golden test ``test_bxsnap2_parity_golden.py`` then asserts the current path
still reproduces this fixture EXACTLY.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Allow ``python tests/golden/generate_bxsnap2_parity_golden.py`` to import the
# helper package via its canonical ``tests.golden._bxsnap2_parity`` path
# (repo root on sys.path — the same import the golden test uses).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.golden._bxsnap2_parity import (
    DIGEST_CASES,
    PARITY_END,
    PARITY_EXCHANGE,
    PARITY_INTERVAL,
    PARITY_SEGMENT,
    PARITY_START,
    PARITY_SYMBOL,
    run_case,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "bxsnap2_parity_golden.json"


def build_fixture() -> dict[str, Any]:
    """Compute the frozen fixture payload from the current Python path."""
    cases: list[dict[str, Any]] = []
    for case in DIGEST_CASES:
        bars, digest = run_case(case)
        cases.append(
            {
                "name": case.name,
                "description": case.description,
                "expected_kept": case.expected_kept,
                "kept": len(bars),
                "digest": digest,
            }
        )
    return {
        "_comment": (
            "DEC-BXSNAP2-PARITY-GOLDEN-001 — observational parity oracle for a "
            "future Rust arrow_table_to_bars port. Frozen bxsnap2 digests of the "
            "current Python bar-materialization path. Regenerate ONLY on a "
            "deliberate Python-path change (new DEC id + MIGRATION note; every "
            "run identity goes cold). Changes NO production behaviour."
        ),
        "preimage_version": "bxsnap2",
        "scope": {
            "exchange": PARITY_EXCHANGE,
            "segment": PARITY_SEGMENT,
            "start": PARITY_START,
            "end": PARITY_END,
            "symbol": PARITY_SYMBOL,
            "interval": PARITY_INTERVAL,
        },
        "cases": cases,
    }


def main() -> None:
    """Write the frozen fixture to disk (pretty JSON, trailing newline)."""
    fixture = build_fixture()
    FIXTURE_PATH.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE_PATH} ({len(fixture['cases'])} cases)")


if __name__ == "__main__":
    main()
