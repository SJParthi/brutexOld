"""Shared real-writer twin-lake fixtures for the acceptance-check suites.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). Everything here rides
the PRODUCTION writers via :mod:`brutex.acceptance.selftest` — never a
hand-written parquet shape (the #1631 fixture-only blindspot).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from brutex.acceptance.context import AcceptanceContext
from brutex.acceptance.selftest import build_twin_lake

#: Pinned clock: 2026-07-10 11:30 IST — a regular NSE Friday mid-morning.
NOW = datetime(2026, 7, 10, 6, 0, tzinfo=UTC)

#: The two twin sessions the pinned clock resolves (Wed + Thu, both regular).
DAY1 = date(2026, 7, 8)
DAY2 = date(2026, 7, 9)


def make_twin(root: Path, **kwargs: object) -> Path:
    """Build the standard twin lake under *root* and return it."""
    lake = root / "lake"
    build_twin_lake(lake, days=(DAY1, DAY2), **kwargs)  # type: ignore[arg-type]
    return lake


def make_ctx(lake: Path, **kwargs: object) -> AcceptanceContext:
    """A pinned-clock context over *lake* (tests override fields as needed)."""
    defaults: dict[str, object] = {
        "lake_root": lake,
        "seed": "2026-07-10",
        "sample_n": 50,
        "months": 6,
        "now_utc": lambda: NOW,
    }
    defaults.update(kwargs)
    return AcceptanceContext(**defaults)  # type: ignore[arg-type]
